"""Portal event fan-out (BRIDGE-1): SNS topic -> per-API-task SQS queue round trip with moto,
plus the in-process fallback when no topic exists."""
import asyncio

import boto3
import pytest
from moto import mock_aws

from app.models.schemas import ChatMessage
from app.services import events_service
from app.services.events_service import PortalEvents, chat_event


@pytest.fixture
def aws(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        sns = boto3.client("sns", region_name="us-east-1")
        sqs = boto3.client("sqs", region_name="us-east-1")
        yield sns, sqs, sns.create_topic(Name="resq-portal-events")["TopicArn"]


@pytest.fixture
def delivered(monkeypatch):
    got = []

    async def capture(event):
        got.append(event)
    monkeypatch.setattr(events_service, "deliver_locally", capture)
    return got


def _msg(case_id="CASE-EVT-1"):
    return ChatMessage(message_id="MSG-1", case_id=case_id, sender="PATIENT", sender_name="P", content="hello doctor")


@pytest.mark.asyncio
async def test_sns_to_per_task_queue_round_trip(aws, delivered):
    sns, sqs, topic = aws
    api_task = PortalEvents(sns_client=sns, sqs_client=sqs, topic_arn=topic)
    worker = PortalEvents(sns_client=sns, sqs_client=sqs, topic_arn=topic)   # publisher in another service
    await asyncio.to_thread(api_task._setup_queue)
    assert api_task.queue_url and "resq-api-events-" in api_task.queue_url
    subs = sns.list_subscriptions_by_topic(TopicArn=topic)["Subscriptions"]
    assert [s["Endpoint"] for s in subs] == [api_task.queue_arn]

    await worker.publish(chat_event(_msg()))
    assert delivered == []                       # publisher does not deliver locally in fan-out mode
    assert await api_task.poll_once(wait_seconds=0) == 1
    assert delivered[0]["type"] == "chat_message"
    assert delivered[0]["message"]["content"] == "hello doctor"
    assert await api_task.poll_once(wait_seconds=0) == 0   # deleted after delivery

    url = api_task.queue_url
    await api_task.stop()
    assert sns.list_subscriptions_by_topic(TopicArn=topic)["Subscriptions"] == []
    assert url not in sqs.list_queues().get("QueueUrls", [])


@pytest.mark.asyncio
async def test_two_api_tasks_each_get_the_event(aws, delivered):
    sns, sqs, topic = aws
    tasks = [PortalEvents(sns_client=sns, sqs_client=sqs, topic_arn=topic) for _ in range(2)]
    for t in tasks:
        await asyncio.to_thread(t._setup_queue)
    await tasks[0].publish(chat_event(_msg("CASE-EVT-2")))
    for t in tasks:
        assert await t.poll_once(wait_seconds=0) == 1
    assert [e["case_id"] for e in delivered] == ["CASE-EVT-2", "CASE-EVT-2"]
    for t in tasks:
        await t.stop()


def test_decode_accepts_raw_and_sns_envelope():
    import json
    event = {"type": "case_upserted", "case_id": "C1", "case": {}}
    assert PortalEvents._decode(json.dumps(event)) == event
    envelope = {"Type": "Notification", "Message": json.dumps(event)}
    assert PortalEvents._decode(json.dumps(envelope)) == event
    assert PortalEvents._decode("garbage") is None


@pytest.mark.asyncio
async def test_no_topic_delivers_in_process(delivered):
    local = PortalEvents(auto_init=False)
    assert local.fanout is False
    await local.publish(chat_event(_msg("CASE-EVT-3")))
    assert delivered[0]["case_id"] == "CASE-EVT-3"
