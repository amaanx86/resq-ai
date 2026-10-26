"""Portal event fan-out for the WebSocket bridge (BRIDGE-1).

The worker and the API publish here whenever a case or chat message is saved; every API task
delivers the event to its own WebSocket clients (`app.routers.bridge`).

Design: SNS topic `resq-portal-events`; each API task creates its own SQS queue
(`resq-api-events-<host>-<random>`), subscribes it to the topic on startup (raw message
delivery), long-polls it, and unsubscribes + deletes it on shutdown. The topic ARN comes from
PORTAL_EVENTS_TOPIC_ARN; locally (Floci, AWS_ENDPOINT_URL set) the topic is created by name.
With no topic (pure mock mode, tests) events are delivered in-process straight to the local
ConnectionManager. When a topic is in use the publisher does NOT also deliver locally: its own
queue brings the event back, so every task (including the publisher) delivers exactly once.

Event shapes (also what WebSocket clients receive):
  {"type": "chat_message", "case_id": "...", "message": <ChatMessage JSON>}
  {"type": "case_upserted", "case_id": "...", "case": <EmergencyCase JSON>}
"""
import asyncio
import json
import re
import socket
import uuid
from typing import Any, Dict, Optional

import boto3

from app.config import settings
from app.models.schemas import ChatMessage, EmergencyCase

POLL_WAIT_SECONDS = 10
EVENT_QUEUE_RETENTION_SECONDS = 300  # stale events are useless to a WebSocket client


def chat_event(message: ChatMessage) -> Dict[str, Any]:
    return {"type": "chat_message", "case_id": message.case_id, "message": json.loads(message.model_dump_json())}


def case_event(case: EmergencyCase) -> Dict[str, Any]:
    return {"type": "case_upserted", "case_id": case.case_id, "case": json.loads(case.model_dump_json())}


async def deliver_locally(event: Dict[str, Any]) -> None:
    """Hands an event to this process's WebSocket clients."""
    from app.routers.bridge import manager  # lazy: avoid router <-> service import cycle
    await manager.deliver_event(event)


class PortalEvents:
    def __init__(self, sns_client: Any = None, sqs_client: Any = None, topic_arn: Optional[str] = None,
                 auto_init: bool = True):
        self.sns = sns_client
        self.sqs = sqs_client
        self.topic_arn = topic_arn
        self._auto_init = auto_init and sns_client is None
        self._initialized = not self._auto_init
        self.queue_url: Optional[str] = None
        self.queue_arn: Optional[str] = None
        self.subscription_arn: Optional[str] = None
        self._poll_task: Optional[asyncio.Task] = None
        self._stopping = False

    # ------------------------------------------------------------------ setup
    def _ensure_init(self) -> None:
        if self._initialized:
            return
        self._initialized = True
        local = bool(settings.AWS_ENDPOINT_URL)
        if not (settings.PORTAL_EVENTS_TOPIC_ARN or local or not settings.MOCK_AWS):
            print("[Events] In-process mode (no SNS topic): events go straight to local WebSocket clients.")
            return
        try:
            kwargs: Dict[str, Any] = {"region_name": settings.AWS_REGION}
            if local:
                kwargs.update(endpoint_url=settings.AWS_ENDPOINT_URL,
                              aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
                              aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY)
            self.sns = boto3.client("sns", **kwargs)
            self.sqs = boto3.client("sqs", **kwargs)
            if settings.PORTAL_EVENTS_TOPIC_ARN:
                self.topic_arn = settings.PORTAL_EVENTS_TOPIC_ARN
            elif local:
                # CreateTopic is idempotent: returns the existing ARN if the topic exists.
                self.topic_arn = self.sns.create_topic(Name=settings.PORTAL_EVENTS_TOPIC_NAME)["TopicArn"]
            else:
                print("[Events] Warning: MOCK_AWS=False but PORTAL_EVENTS_TOPIC_ARN is not set. "
                      "Events from the worker will not reach the portal; using in-process delivery only.")
            if self.topic_arn:
                print(f"[Events] SNS fan-out mode via topic {self.topic_arn}")
        except Exception as e:
            print(f"[Events] Warning: SNS/SQS unavailable, using in-process delivery only: {e}")
            self.sns = self.sqs = None
            self.topic_arn = None

    @property
    def fanout(self) -> bool:
        self._ensure_init()
        return bool(self.sns is not None and self.topic_arn)

    # ------------------------------------------------------------------ publish
    async def publish(self, event: Dict[str, Any]) -> None:
        if not self.fanout:
            await deliver_locally(event)
            return
        try:
            await asyncio.to_thread(self.sns.publish, TopicArn=self.topic_arn, Message=json.dumps(event))
        except Exception as e:
            # Best effort: at least this task's clients get it.
            print(f"[Events] SNS publish failed ({e}); delivering in-process only.")
            await deliver_locally(event)

    # ------------------------------------------------------------------ API consumer
    def _queue_name(self) -> str:
        host = re.sub(r"[^A-Za-z0-9_-]", "-", socket.gethostname())[:40]
        return f"resq-api-events-{host}-{uuid.uuid4().hex[:8]}"

    def _setup_queue(self) -> None:
        name = self._queue_name()
        self.queue_url = self.sqs.create_queue(
            QueueName=name,
            Attributes={"MessageRetentionPeriod": str(max(EVENT_QUEUE_RETENTION_SECONDS, 60))},
        )["QueueUrl"]
        self.queue_arn = self.sqs.get_queue_attributes(
            QueueUrl=self.queue_url, AttributeNames=["QueueArn"]
        )["Attributes"]["QueueArn"]
        policy = {
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Principal": {"Service": "sns.amazonaws.com"},
                "Action": "sqs:SendMessage",
                "Resource": self.queue_arn,
                "Condition": {"ArnEquals": {"aws:SourceArn": self.topic_arn}},
            }],
        }
        try:
            self.sqs.set_queue_attributes(QueueUrl=self.queue_url, Attributes={"Policy": json.dumps(policy)})
        except Exception as e:
            print(f"[Events] Could not set queue policy on {name} (ok on emulators): {e}")
        self.subscription_arn = self.sns.subscribe(
            TopicArn=self.topic_arn, Protocol="sqs", Endpoint=self.queue_arn,
            Attributes={"RawMessageDelivery": "true"}, ReturnSubscriptionArn=True,
        )["SubscriptionArn"]
        print(f"[Events] API task queue {name} subscribed to {self.topic_arn}")

    async def start(self) -> None:
        if not self.fanout:
            return
        try:
            await asyncio.to_thread(self._setup_queue)
        except Exception as e:
            print(f"[Events] Warning: could not create/subscribe this task's event queue: {e}. "
                  "Worker events will not reach this task's WebSocket clients.")
            return
        self._stopping = False
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def poll_once(self, wait_seconds: int = POLL_WAIT_SECONDS) -> int:
        """Receives one batch from this task's queue and delivers it. Returns the count."""
        res = await asyncio.to_thread(
            self.sqs.receive_message, QueueUrl=self.queue_url, MaxNumberOfMessages=10, WaitTimeSeconds=wait_seconds,
        )
        msgs = res.get("Messages", [])
        for m in msgs:
            event = self._decode(m.get("Body", ""))
            if event:
                try:
                    await deliver_locally(event)
                except Exception as e:
                    print(f"[Events] Local delivery failed: {e}")
        if msgs:
            entries = [{"Id": str(i), "ReceiptHandle": m["ReceiptHandle"]} for i, m in enumerate(msgs)]
            await asyncio.to_thread(self.sqs.delete_message_batch, QueueUrl=self.queue_url, Entries=entries)
        return len(msgs)

    @staticmethod
    def _decode(body: str) -> Optional[Dict[str, Any]]:
        try:
            data = json.loads(body)
            if isinstance(data, dict) and data.get("Type") == "Notification" and "Message" in data:
                data = json.loads(data["Message"])  # raw delivery not honoured: SNS envelope
            return data if isinstance(data, dict) and "type" in data else None
        except Exception:
            return None

    async def _poll_loop(self) -> None:
        while not self._stopping:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[Events] Poll error: {e}")
                await asyncio.sleep(2)

    async def stop(self) -> None:
        self._stopping = True
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except (asyncio.CancelledError, Exception):
                pass
            self._poll_task = None
        if self.sns is not None and self.subscription_arn:
            try:
                await asyncio.to_thread(self.sns.unsubscribe, SubscriptionArn=self.subscription_arn)
            except Exception as e:
                print(f"[Events] Unsubscribe failed: {e}")
            self.subscription_arn = None
        if self.sqs is not None and self.queue_url:
            try:
                await asyncio.to_thread(self.sqs.delete_queue, QueueUrl=self.queue_url)
                print(f"[Events] Deleted this task's event queue {self.queue_url}")
            except Exception as e:
                print(f"[Events] Queue delete failed: {e}")
            self.queue_url = None


_events = PortalEvents()


async def publish_chat_message(message: ChatMessage) -> None:
    """Fan out a saved chat message to /ws/bridge/{case_id} subscribers on every API task."""
    await _events.publish(chat_event(message))


async def publish_case(case: EmergencyCase) -> None:
    """Fan out a created/updated case to /ws/cases subscribers on every API task."""
    await _events.publish(case_event(case))


async def start_api_consumer() -> None:
    """API lifespan startup: create + subscribe this task's queue and start polling."""
    await _events.start()


async def stop_api_consumer() -> None:
    """API lifespan shutdown: stop polling, unsubscribe and delete this task's queue."""
    await _events.stop()
