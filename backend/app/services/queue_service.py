"""Inbound WhatsApp message queue (INFRA-6).

Every inbound message (Meta webhook, End User Messaging Social via SNS, portal simulator)
becomes an `InboundWhatsAppMessage` on `resq-webhook-queue`. The worker (`python -m app.worker`)
long-polls the queue and runs `app.pipeline.process_inbound_message`.

Modes:
- **SQS** (Floci or real AWS): `publish` sends the message and returns at once (`QUEUED`).
  Locally (AWS_ENDPOINT_URL set) the queue and its DLQ are auto-created with a redrive
  policy, as is done for the DynamoDB table. In real AWS Terraform creates them; set
  WEBHOOK_QUEUE_URL (no lookup) or the name is resolved with GetQueueUrl.
- **Inline** (pure mock mode: MOCK_AWS=True and no endpoint, i.e. tests and the no-AWS demo):
  there is no queue and no worker, so `publish` runs the pipeline in-process (`PROCESSED`).

Besides WhatsApp messages the queue carries (WA-0):
- **Call events** (`publish_call_event`): WhatsApp Calling API webhooks from the Meta route. EUM
  Social call events reach the queue through SNS like its messages.
- **Internal tasks** (`publish_internal`): work the backend schedules for itself, e.g. an offer
  timeout, sent with SQS `DelaySeconds` (at most 900 s; a longer delay is re-queued by the worker
  until `run_at`). Inline mode schedules them on the event loop instead (`loop.call_later`), so
  the no-AWS demo still runs them; nothing persists them across a restart there.
"""
import asyncio
import json
import math
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set

import boto3

from app.config import settings
from app.models.schemas import InboundCallEvent, InboundWhatsAppMessage, InternalTask
from app.tasks import seconds_until

# Local queue attributes (Terraform owns the real ones). The visibility timeout must exceed
# the worst-case pipeline time: Transcribe polling (TRANSCRIBE_TIMEOUT_SECONDS) + the agent.
LOCAL_VISIBILITY_TIMEOUT = int(os.getenv("WEBHOOK_QUEUE_VISIBILITY_TIMEOUT", "180"))
LOCAL_MAX_RECEIVE_COUNT = int(os.getenv("WEBHOOK_QUEUE_MAX_RECEIVE_COUNT", "5"))
# SQS DelaySeconds limit on a standard queue.
MAX_SQS_DELAY_SECONDS = 900


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class QueueService:
    def __init__(self, client: Any = None, queue_url: Optional[str] = None, auto_init: bool = True):
        """`client`/`queue_url` let tests inject a moto/Stubber client. Otherwise the client is
        created from settings when an SQS endpoint is expected (Floci or real AWS)."""
        self.client = client
        self.queue_url = queue_url
        self.dlq_url: Optional[str] = None
        # Inline mode: pending internal-task timers and running tasks (kept so they aren't GC'd).
        self._timers: Set[asyncio.TimerHandle] = set()
        self._running: Set["asyncio.Task[Any]"] = set()

        if self.client is None and auto_init:
            self._init_from_settings()
        elif self.client is not None and not self.queue_url:
            self.queue_url = self._resolve_queue_url(create=True)

        if self.client is not None and self.queue_url:
            print(f"[Queue] SQS mode: publishing to {self.queue_url}")
        else:
            self.client = None
            print("[Queue] Inline mode (no SQS): inbound messages are processed in-process (mock/tests only).")

    # ------------------------------------------------------------------ setup
    def _init_from_settings(self) -> None:
        wants_sqs = bool(settings.WEBHOOK_QUEUE_URL) or bool(settings.AWS_ENDPOINT_URL) or not settings.MOCK_AWS
        if not wants_sqs:
            return
        local = bool(settings.AWS_ENDPOINT_URL)
        try:
            kwargs: Dict[str, Any] = {"region_name": settings.AWS_REGION}
            if local:
                kwargs.update(
                    endpoint_url=settings.AWS_ENDPOINT_URL,
                    aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
                    aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
                )
            self.client = boto3.client("sqs", **kwargs)
            self.queue_url = settings.WEBHOOK_QUEUE_URL or self._resolve_queue_url(create=local)
        except Exception as e:
            self.client = None
            self.queue_url = None
            if not settings.MOCK_AWS and not local:
                # Real AWS: processing inline in the API would silently bypass the worker (and the
                # API task role has no Transcribe/Bedrock permissions). Fail fast instead.
                raise RuntimeError(
                    f"[Queue] MOCK_AWS=False but the webhook queue is unavailable ({e}). "
                    "Set WEBHOOK_QUEUE_URL (Terraform output) and grant sqs:SendMessage/ReceiveMessage."
                ) from e
            print(f"[Queue] Warning: SQS unavailable, falling back to inline processing: {e}")

    def _resolve_queue_url(self, create: bool) -> str:
        name = settings.WEBHOOK_QUEUE_NAME
        try:
            return self.client.get_queue_url(QueueName=name)["QueueUrl"]
        except Exception:
            if not create:
                raise
        print(f"[Queue] Queue '{name}' not found. Auto-creating it with DLQ '{settings.WEBHOOK_DLQ_NAME}'...")
        self.dlq_url = self.client.create_queue(
            QueueName=settings.WEBHOOK_DLQ_NAME,
            Attributes={"MessageRetentionPeriod": "1209600"},
        )["QueueUrl"]
        dlq_arn = self.client.get_queue_attributes(
            QueueUrl=self.dlq_url, AttributeNames=["QueueArn"]
        )["Attributes"]["QueueArn"]
        url = self.client.create_queue(
            QueueName=name,
            Attributes={
                "VisibilityTimeout": str(LOCAL_VISIBILITY_TIMEOUT),
                "ReceiveMessageWaitTimeSeconds": "20",
                "RedrivePolicy": json.dumps(
                    {"deadLetterTargetArn": dlq_arn, "maxReceiveCount": str(LOCAL_MAX_RECEIVE_COUNT)}
                ),
            },
        )["QueueUrl"]
        print(f"[Queue] Created '{name}' (redrive to DLQ after {LOCAL_MAX_RECEIVE_COUNT} receives).")
        return url

    # ------------------------------------------------------------------ API side
    @property
    def inline(self) -> bool:
        return self.client is None

    @property
    def mode(self) -> str:
        return "inline" if self.inline else "sqs"

    async def publish(self, msg: InboundWhatsAppMessage, received_at: Optional[str] = None) -> Dict[str, Any]:
        """Queues the message. Inline mode runs the pipeline now and returns its result.

        Returns {"status": "QUEUED", "provider_message_id", "sqs_message_id"} or the pipeline
        result {"status": "PROCESSED"|"DUPLICATE"|..., "case_id", "action", ...}.
        """
        if self.inline:
            from app.pipeline import process_inbound_message  # lazy: pipeline imports many services
            return await process_inbound_message(msg, received_at=received_at or _now_iso())

        res = await asyncio.to_thread(
            self.client.send_message,
            QueueUrl=self.queue_url,
            MessageBody=msg.model_dump_json(),
            MessageAttributes={
                "provider": {"DataType": "String", "StringValue": msg.provider},
                "provider_message_id": {"DataType": "String", "StringValue": msg.provider_message_id},
            },
        )
        return {
            "status": "QUEUED",
            "provider_message_id": msg.provider_message_id,
            "sqs_message_id": res.get("MessageId"),
        }

    async def publish_call_event(self, event: InboundCallEvent) -> Dict[str, Any]:
        """Queues a WhatsApp call event. Inline mode handles it now and returns the result."""
        if self.inline:
            from app.tasks import handle_call_event  # lazy, like the pipeline above
            return await handle_call_event(event)
        res = await asyncio.to_thread(
            self.client.send_message,
            QueueUrl=self.queue_url,
            MessageBody=event.model_dump_json(),
            MessageAttributes={
                "kind": {"DataType": "String", "StringValue": "call_event"},
                "call_id": {"DataType": "String", "StringValue": event.call_id},
            },
        )
        return {"status": "QUEUED", "call_id": event.call_id, "sqs_message_id": res.get("MessageId")}

    async def publish_internal(self, kind: str, payload: Optional[Dict[str, Any]] = None,
                               delay_seconds: float = 0) -> InternalTask:
        """Schedules an internal task to run in about `delay_seconds` (at-least-once)."""
        task = InternalTask(
            task_id=uuid.uuid4().hex,
            kind=kind,
            payload=payload or {},
            run_at=(datetime.now(timezone.utc) + timedelta(seconds=max(0.0, delay_seconds))).isoformat(),
        )
        await self.publish_task(task)
        return task

    async def publish_task(self, task: InternalTask) -> Optional[str]:
        """Queues an existing task to run at its `run_at`. Returns the SQS message ID (None inline).
        The worker also uses this to re-queue a task that is not due yet."""
        delay = max(0.0, seconds_until(task.run_at))
        if self.inline:
            self._schedule_inline(task, delay)
            return None
        res = await asyncio.to_thread(
            self.client.send_message,
            QueueUrl=self.queue_url,
            MessageBody=task.model_dump_json(),
            DelaySeconds=min(MAX_SQS_DELAY_SECONDS, int(math.ceil(delay))),
            MessageAttributes={"kind": {"DataType": "String", "StringValue": task.kind}},
        )
        print(f"[Queue] Internal task {task.kind} {task.task_id} queued (runs in {delay:.0f}s).")
        return res.get("MessageId")

    def _schedule_inline(self, task: InternalTask, delay: float) -> None:
        loop = asyncio.get_running_loop()

        def fire() -> None:
            self._timers.discard(handle)
            running = loop.create_task(self._run_inline(task))
            self._running.add(running)
            running.add_done_callback(self._running.discard)

        handle = loop.call_later(delay, fire)
        self._timers.add(handle)

    async def _run_inline(self, task: InternalTask) -> None:
        from app.tasks import run_internal_task
        try:
            await run_internal_task(task)
        except Exception as e:
            # No queue to retry it: log, as the worker would before the redelivery.
            print(f"[Queue] Inline internal task {task.kind} {task.task_id} failed: {e!r}")

    def cancel_scheduled(self) -> int:
        """Inline mode: cancels internal tasks that have not fired yet. Returns how many."""
        count = len(self._timers)
        for handle in list(self._timers):
            handle.cancel()
        self._timers.clear()
        return count

    # ------------------------------------------------------------------ worker side
    def receive(self, max_messages: int = 5, wait_seconds: int = 20) -> List[Dict[str, Any]]:
        if self.inline:
            return []
        res = self.client.receive_message(
            QueueUrl=self.queue_url,
            MaxNumberOfMessages=max_messages,
            WaitTimeSeconds=wait_seconds,
            AttributeNames=["SentTimestamp", "ApproximateReceiveCount"],
            MessageAttributeNames=["All"],
        )
        return res.get("Messages", [])

    def delete(self, receipt_handle: str) -> None:
        self.client.delete_message(QueueUrl=self.queue_url, ReceiptHandle=receipt_handle)

    def release(self, receipt_handle: str) -> None:
        """Makes a received-but-unprocessed message visible again at once (graceful shutdown)."""
        try:
            self.client.change_message_visibility(
                QueueUrl=self.queue_url, ReceiptHandle=receipt_handle, VisibilityTimeout=0
            )
        except Exception as e:
            print(f"[Queue] Could not release message: {e}")


queue_service = QueueService()
