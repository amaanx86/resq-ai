"""Worker + queue against a moto SQS: delete on success only, DLQ redrive, idempotent
end-to-end processing, graceful stop, and publish in SQS mode (INFRA-6)."""
import json

import boto3
import pytest
from moto import mock_aws

from app import pipeline
from app.config import settings
from app.models.schemas import InboundWhatsAppMessage
from app.services.dynamo_service import dynamo_service
from app.services.queue_service import QueueService
from app.worker import Worker
from test_pipeline_support import fakes, inbound, new_phone  # noqa: F401


@pytest.fixture
def sqs(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        yield boto3.client("sqs", region_name="us-east-1")


def parse_json(body: str):
    return [InboundWhatsAppMessage.model_validate_json(body)]


def visible_count(client, url) -> int:
    # VisibilityTimeout=0 on receive so a peek doesn't hide the message.
    return len(client.receive_message(QueueUrl=url, MaxNumberOfMessages=10, VisibilityTimeout=0).get("Messages", []))


def make_queue(sqs_client) -> QueueService:
    url = sqs_client.create_queue(QueueName="resq-test-queue", Attributes={"VisibilityTimeout": "0"})["QueueUrl"]
    return QueueService(client=sqs_client, queue_url=url)


def test_queue_service_auto_creates_queue_and_dlq_with_redrive(sqs):
    q = QueueService(client=sqs)
    assert q.queue_url.endswith(settings.WEBHOOK_QUEUE_NAME)
    attrs = sqs.get_queue_attributes(QueueUrl=q.queue_url, AttributeNames=["All"])["Attributes"]
    redrive = json.loads(attrs["RedrivePolicy"])
    assert redrive["deadLetterTargetArn"].endswith(settings.WEBHOOK_DLQ_NAME)
    assert int(redrive["maxReceiveCount"]) >= 3
    # A second service finds the existing queue instead of recreating it.
    assert QueueService(client=sqs).queue_url == q.queue_url


@pytest.mark.asyncio
async def test_publish_in_sqs_mode_sends_serialized_message(sqs):
    q = make_queue(sqs)
    msg = inbound(new_phone(), text="hi")
    res = await q.publish(msg)
    assert res["status"] == "QUEUED" and res["sqs_message_id"]
    got = sqs.receive_message(QueueUrl=q.queue_url, MessageAttributeNames=["All"])["Messages"][0]
    assert InboundWhatsAppMessage.model_validate_json(got["Body"]) == msg
    assert got["MessageAttributes"]["provider_message_id"]["StringValue"] == msg.provider_message_id


@pytest.mark.asyncio
async def test_worker_deletes_message_on_success(sqs):
    q = make_queue(sqs)
    seen = []

    async def ok(msg, received_at=None):
        seen.append((msg, received_at))
        return {"status": "PROCESSED", "case_id": "CASE-1", "action": "triaged"}

    await q.publish(inbound(new_phone(), text="hi"))
    worker = Worker(queue=q, process=ok, parse=parse_json)
    assert await worker.poll_once(wait_seconds=0) == 1
    assert len(seen) == 1 and seen[0][1]            # received_at from SentTimestamp
    assert visible_count(sqs, q.queue_url) == 0


@pytest.mark.asyncio
async def test_worker_keeps_message_on_failure(sqs):
    q = make_queue(sqs)

    async def boom(msg, received_at=None):
        raise RuntimeError("transient failure")

    await q.publish(inbound(new_phone(), text="hi"))
    worker = Worker(queue=q, process=boom, parse=parse_json)
    await worker.poll_once(wait_seconds=0)
    assert visible_count(sqs, q.queue_url) == 1     # still there: SQS retries, then the DLQ


@pytest.mark.asyncio
async def test_worker_keeps_unparseable_and_in_progress_messages(sqs):
    q = make_queue(sqs)
    sqs.send_message(QueueUrl=q.queue_url, MessageBody="not json at all")

    async def busy(msg, received_at=None):
        return {"status": "IN_PROGRESS"}

    worker = Worker(queue=q, process=busy, parse=parse_json)
    await worker.poll_once(wait_seconds=0)
    assert visible_count(sqs, q.queue_url) == 1

    sqs.purge_queue(QueueUrl=q.queue_url)
    await q.publish(inbound(new_phone(), text="hi"))
    await worker.poll_once(wait_seconds=0)
    assert visible_count(sqs, q.queue_url) == 1


@pytest.mark.asyncio
async def test_worker_end_to_end_duplicate_delivery_creates_one_case(sqs, fakes):
    q = make_queue(sqs)
    phone = new_phone()
    msg = inbound(phone, type="audio", transcript="dil ka daura")
    body = msg.model_dump_json()
    sqs.send_message(QueueUrl=q.queue_url, MessageBody=body)
    sqs.send_message(QueueUrl=q.queue_url, MessageBody=body)   # SQS at-least-once duplicate

    worker = Worker(queue=q, process=pipeline.process_inbound_message, parse=parse_json)
    while await worker.poll_once(wait_seconds=0):
        pass
    assert visible_count(sqs, q.queue_url) == 0
    cases = [c for c in dynamo_service.list_all_cases() if c.user_phone == phone]
    assert len(cases) == 1
    assert fakes.triage.tools_fired.count("match_specialist_doctor") == 1
    assert cases[0].received_at is not None


@pytest.mark.asyncio
async def test_worker_releases_messages_when_stopping(sqs):
    q = QueueService(client=sqs, queue_url=sqs.create_queue(
        QueueName="resq-stop-queue", Attributes={"VisibilityTimeout": "60"})["QueueUrl"])

    async def never(msg, received_at=None):
        raise AssertionError("must not process after stop")

    await q.publish(inbound(new_phone(), text="hi"))
    worker = Worker(queue=q, process=never, parse=parse_json)
    worker.request_stop()
    assert await worker.poll_once(wait_seconds=0) == 0
    assert visible_count(sqs, q.queue_url) == 1     # visible again despite the 60 s timeout


def test_worker_entrypoint_never_seeds_demo_data():
    """`python -m app.worker` must not seed, even when SEED_DEMO_DATA=True leaks into its env."""
    import os
    import subprocess
    import sys
    code = "\n".join([
        "import runpy",
        "try:",
        "    runpy.run_module('app.worker', run_name='__main__')",
        "except SystemExit:",
        "    pass",
        "from app.services.dynamo_service import dynamo_service",
        "print('DOCTORS=%d' % len(dynamo_service.list_doctors()))",
    ])
    env = dict(os.environ, SEED_DEMO_DATA="True", MOCK_AWS="True", PYTHONIOENCODING="utf-8")
    env.pop("AWS_ENDPOINT_URL", None)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env,
                         cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))), timeout=120,
                         encoding="utf-8")
    assert "DOCTORS=0" in out.stdout, out.stdout + out.stderr
    assert "No SQS queue configured" in out.stdout + out.stderr
