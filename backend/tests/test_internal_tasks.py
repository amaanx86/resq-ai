"""WA-0: internal tasks (delayed work the backend schedules for itself) and call events on the
inbound queue, and the worker routing them outside the message pipeline."""
import asyncio
import time
from datetime import datetime, timedelta, timezone

import boto3
import pytest
from moto import mock_aws

from app import tasks
from app.messaging.inbound import parse_queue_message
from app.models.schemas import InboundCallEvent, InternalTask
from app.services.queue_service import MAX_SQS_DELAY_SECONDS, QueueService
from app.worker import Worker
from test_pipeline_support import inbound, new_phone


def _in(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _task(kind: str = "test_kind", run_in: float = 0, **payload) -> InternalTask:
    return InternalTask(task_id=f"t-{kind}-{run_in}", kind=kind, payload=payload, run_at=_in(run_in))


def _call(event: str = "connect") -> InboundCallEvent:
    return InboundCallEvent(provider="eum_social", call_id="wacid.TEST", event=event,
                            user_phone="+923001234567", timestamp=_in(0), sdp="v=0\r\n")


class RecordingQueue:
    """Hands out queued batches one receive at a time; records deletes and re-queued tasks."""
    inline = False
    queue_url = "fake-queue"
    client = None

    def __init__(self, *batches):
        self.batches = [list(b) for b in batches]
        self.deleted, self.requeued, self._n = [], [], 0

    def receive(self, n, w):
        if not self.batches:
            return []
        out = []
        for body in self.batches.pop(0):
            out.append({"MessageId": str(self._n), "ReceiptHandle": f"rh{self._n}", "Body": body,
                        "Attributes": {"SentTimestamp": "1760000000000"}})
            self._n += 1
        return out

    def delete(self, rh):
        self.deleted.append(rh)

    def release(self, rh):
        pass

    async def publish_task(self, task):
        self.requeued.append(task)


async def _never(*args, **kwargs):
    raise AssertionError("must not be called")


# ---------------------------------------------------------------------------- SQS
@pytest.fixture
def sqs(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        yield boto3.client("sqs", region_name="us-east-1")


@pytest.mark.asyncio
async def test_publish_internal_is_delayed_on_sqs_and_round_trips(sqs):
    url = sqs.create_queue(QueueName="resq-task-queue")["QueueUrl"]
    q = QueueService(client=sqs, queue_url=url)
    task = await q.publish_internal("doctor_offer_timeout", {"case_id": "CASE-1", "offer_id": "OFR-1"},
                                    delay_seconds=1)
    assert q.receive(10, 0) == []                         # not visible during the delay
    time.sleep(1.3)
    got = q.receive(10, 0)
    assert len(got) == 1
    assert got[0]["MessageAttributes"]["kind"]["StringValue"] == "doctor_offer_timeout"
    assert parse_queue_message(got[0]["Body"]) == [task]
    assert task.payload == {"case_id": "CASE-1", "offer_id": "OFR-1"}


@pytest.mark.asyncio
async def test_long_delays_are_capped_at_the_sqs_limit_and_keep_run_at():
    sent = []

    class Client:
        def send_message(self, **kwargs):
            sent.append(kwargs)
            return {"MessageId": "m1"}

    q = QueueService(client=Client(), queue_url="q")
    task = await q.publish_internal("later", delay_seconds=3600)
    assert sent[0]["DelaySeconds"] == MAX_SQS_DELAY_SECONDS
    assert 3590 < (datetime.fromisoformat(task.run_at) - datetime.now(timezone.utc)).total_seconds() <= 3600

    await q.publish_internal("now")
    assert sent[1]["DelaySeconds"] == 0


@pytest.mark.asyncio
async def test_call_event_is_published_with_its_discriminator(sqs):
    url = sqs.create_queue(QueueName="resq-call-queue")["QueueUrl"]
    q = QueueService(client=sqs, queue_url=url)
    event = _call()
    res = await q.publish_call_event(event)
    assert res["status"] == "QUEUED"
    got = q.receive(10, 0)[0]
    assert got["MessageAttributes"]["kind"]["StringValue"] == "call_event"
    assert parse_queue_message(got["Body"]) == [event]


# ---------------------------------------------------------------------------- inline (mock mode)
@pytest.mark.asyncio
async def test_inline_mode_runs_internal_tasks_after_their_delay(monkeypatch):
    ran = []

    async def handler(task):
        ran.append(task.payload["n"])
        return {"handled": task.payload["n"]}

    monkeypatch.setitem(tasks._handlers, "inline_test", handler)
    q = QueueService(auto_init=False)
    assert q.inline
    await q.publish_internal("inline_test", {"n": 1}, delay_seconds=0.05)
    await q.publish_internal("inline_test", {"n": 2}, delay_seconds=30)
    await asyncio.sleep(0.01)
    assert ran == []                                      # not yet due
    await asyncio.sleep(0.15)
    assert ran == [1]
    assert q.cancel_scheduled() == 1                      # the 30 s one never fires


@pytest.mark.asyncio
async def test_inline_call_event_is_handled_at_once():
    res = await QueueService(auto_init=False).publish_call_event(_call())
    # A patient's call goes the direct-call way until CALL-1 (this caller has no open case).
    assert res["status"] == "DONE" and res["action"] == "call_without_case"


# ---------------------------------------------------------------------------- task dispatch
@pytest.mark.asyncio
async def test_run_internal_task_dispatches_by_kind(monkeypatch):
    async def handler(task):
        return {"case_id": task.payload["case_id"]}

    monkeypatch.setitem(tasks._handlers, "kind_a", handler)
    assert await tasks.run_internal_task(_task("kind_a", case_id="C1")) == {"status": "DONE", "case_id": "C1"}
    with pytest.raises(tasks.UnknownTaskKind):
        await tasks.run_internal_task(_task("no_such_kind"))


def test_task_handler_decorator_registers(monkeypatch):
    monkeypatch.setattr(tasks, "_handlers", {})

    @tasks.task_handler("decorated")
    async def handler(task):
        return None

    assert tasks._handlers == {"decorated": handler}


# ---------------------------------------------------------------------------- worker routing
@pytest.mark.asyncio
async def test_worker_runs_a_due_task_and_deletes_it():
    ran = []

    async def run_task(task):
        ran.append(task.task_id)
        return {"status": "DONE"}

    q = RecordingQueue([_task(run_in=0).model_dump_json()])
    await Worker(queue=q, process=_never, parse=parse_queue_message, run_task=run_task,
                 handle_call=_never).poll_once(wait_seconds=0)
    assert ran == ["t-test_kind-0"] and q.deleted == ["rh0"] and q.requeued == []


@pytest.mark.asyncio
async def test_worker_requeues_a_task_that_is_not_due_yet():
    task = _task(run_in=1800)
    q = RecordingQueue([task.model_dump_json()])
    await Worker(queue=q, process=_never, parse=parse_queue_message, run_task=_never,
                 handle_call=_never).poll_once(wait_seconds=0)
    assert q.requeued == [task] and q.deleted == ["rh0"]


@pytest.mark.asyncio
async def test_worker_keeps_a_task_whose_handler_fails_or_is_unknown():
    q = RecordingQueue([_task("no_such_kind").model_dump_json()])
    await Worker(queue=q, process=_never, parse=parse_queue_message,
                 run_task=tasks.run_internal_task, handle_call=_never).poll_once(wait_seconds=0)
    assert q.deleted == []                                # retried, then the DLQ


@pytest.mark.asyncio
async def test_worker_sends_call_events_to_the_call_handler_not_the_pipeline():
    seen = []

    async def handle_call(event):
        seen.append(event.event)
        return {"status": "IGNORED"}

    q = RecordingQueue([_call("connect").model_dump_json(), _call("terminate").model_dump_json()])
    await Worker(queue=q, process=_never, parse=parse_queue_message, run_task=_never,
                 handle_call=handle_call).poll_once(wait_seconds=0)
    assert sorted(seen) == ["connect", "terminate"] and sorted(q.deleted) == ["rh0", "rh1"]


@pytest.mark.asyncio
async def test_call_events_are_not_grouped_with_a_sender():
    phone = new_phone()
    w = Worker(queue=RecordingQueue(), process=_never, parse=parse_queue_message, run_task=_never,
               handle_call=_never)
    body = {"Body": _call().model_copy(update={"user_phone": phone}).model_dump_json()}
    assert w._senders(body) == set()
    assert w._senders({"Body": inbound(phone, text="hi").model_dump_json()}) == {phone}


# ---------------------------------------------------------------------------- run(): no head-of-line blocking
@pytest.mark.asyncio
async def test_run_handles_a_call_while_a_slow_message_is_still_processing():
    release = asyncio.Event()
    events = []

    async def slow_process(msg, received_at=None):
        events.append("message started")
        await release.wait()
        events.append("message done")
        return {"status": "PROCESSED", "action": "x", "case_id": None}

    async def handle_call(event):
        events.append("call handled")
        return {"status": "IGNORED"}

    q = RecordingQueue([inbound(new_phone(), text="slow").model_dump_json()], [_call().model_dump_json()])
    w = Worker(queue=q, process=slow_process, parse=parse_queue_message, run_task=_never,
               handle_call=handle_call)
    await w._poll_without_waiting()                       # batch 1: starts, does not wait
    await asyncio.sleep(0.01)
    await w._poll_without_waiting()                       # batch 2: the call
    await asyncio.sleep(0.01)
    assert events == ["message started", "call handled"]
    release.set()
    await asyncio.gather(*w._in_flight)
    assert events[-1] == "message done" and sorted(q.deleted) == ["rh0", "rh1"]


@pytest.mark.asyncio
async def test_run_keeps_one_senders_messages_in_order_across_batches():
    phone = new_phone()
    order = []

    async def proc(msg, received_at=None):
        order.append(f"{msg.text} start")
        await asyncio.sleep(0.03 if msg.text == "first" else 0)
        order.append(f"{msg.text} end")
        return {"status": "PROCESSED", "action": "x", "case_id": None}

    q = RecordingQueue([inbound(phone, text="first").model_dump_json()],
                       [inbound(phone, text="second").model_dump_json()])
    w = Worker(queue=q, process=proc, parse=parse_queue_message, run_task=_never, handle_call=_never)
    await w._poll_without_waiting()
    await w._poll_without_waiting()
    await asyncio.gather(*w._in_flight)
    assert order == ["first start", "first end", "second start", "second end"]
    assert w._sender_tails == {} and w._in_flight == set()
