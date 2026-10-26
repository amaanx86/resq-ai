"""Inbound message worker (INFRA-6): `python -m app.worker`.

Long-polls `resq-webhook-queue`, turns each SQS body into queue items
(`app.messaging.inbound.parse_queue_message`: a message published by the Meta webhook route or
the simulator, or an SNS-wrapped End User Messaging Social event) and routes them by type (WA-0):
- `InboundWhatsAppMessage` -> the pipeline (`app.pipeline.process_inbound_message`).
- `InboundCallEvent` (WhatsApp Calling API) -> `app.tasks.handle_call_event`.
- `InternalTask` (scheduled by the backend itself) -> `app.tasks.run_internal_task` once due;
  a task received before its `run_at` (delays over the 900 s SQS limit) is re-queued.
Call events and internal tasks skip the message claim, the sender lease and per-sender ordering.

- The SQS message is deleted only when every message in it was processed (or was a duplicate).
  On failure it stays, becomes visible again after the visibility timeout, and moves to the
  DLQ after maxReceiveCount receives.
- Idempotent on the WhatsApp message ID (`app.pipeline`), so a duplicate delivery never
  calls the emergency contact twice.
- Messages from the same sender run one after another, also across batches (different senders
  still run concurrently), so a quick second message is routed after the first one's case exists.
  Across workers the pipeline's per-sender lease does the same; a busy sender's message comes back
  as IN_PROGRESS and is made visible again after a short delay.
- `run` keeps polling while earlier messages are still being processed (up to
  WORKER_MAX_IN_FLIGHT sender groups at once), so a call event is picked up within one poll even
  while a slow voice note is being transcribed and triaged.
- Visibility heartbeat: while a message is processed its SQS visibility is extended every
  HEARTBEAT_SECONDS, so a slow pipeline (Transcribe + agent) is not redelivered mid-run.
- SIGTERM/SIGINT: stop polling, finish the messages in hand, exit (ECS stopTimeout).
- Never seeds demo data.
"""
import os

if __name__ == "__main__":
    # The worker must never seed demo data. Set before any `app.*` import creates the singletons.
    os.environ["SEED_DEMO_DATA"] = "False"

import asyncio  # noqa: E402
import signal  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set  # noqa: E402

from app.models.schemas import InboundCallEvent, InboundWhatsAppMessage, InternalTask  # noqa: E402
from app.tasks import seconds_until  # noqa: E402

WAIT_SECONDS = int(os.getenv("WORKER_WAIT_SECONDS", "20"))
BATCH_SIZE = int(os.getenv("WORKER_BATCH_SIZE", "5"))
# Sender groups (each one or more SQS messages) `run` processes at once.
MAX_IN_FLIGHT = int(os.getenv("WORKER_MAX_IN_FLIGHT", "20"))
# An internal task received this close to its run_at runs now instead of being re-queued.
TASK_EARLY_TOLERANCE_SECONDS = 1.0
# Visibility heartbeat: every HEARTBEAT_SECONDS, push the message's visibility out to
# VISIBILITY_EXTENSION_SECONDS from now (must stay well above HEARTBEAT_SECONDS).
HEARTBEAT_SECONDS = float(os.getenv("WORKER_HEARTBEAT_SECONDS", "60"))
VISIBILITY_EXTENSION_SECONDS = int(os.getenv("WORKER_VISIBILITY_EXTENSION_SECONDS", "300"))


def _sent_at(sqs_message: Dict[str, Any]) -> Optional[str]:
    """SQS SentTimestamp (ms epoch) -> ISO 8601: when the message reached the queue (DATA-1)."""
    try:
        ms = int(sqs_message.get("Attributes", {}).get("SentTimestamp"))
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()
    except Exception:
        return None


class Worker:
    def __init__(
        self,
        queue: Any = None,
        process: Optional[Callable[..., Awaitable[Dict[str, Any]]]] = None,
        parse: Optional[Callable[[str], List[Any]]] = None,
        run_task: Optional[Callable[[InternalTask], Awaitable[Dict[str, Any]]]] = None,
        handle_call: Optional[Callable[[InboundCallEvent], Awaitable[Dict[str, Any]]]] = None,
    ):
        if queue is None:
            from app.services.queue_service import queue_service
            queue = queue_service
        if process is None:
            from app.pipeline import process_inbound_message
            process = process_inbound_message
        if parse is None:
            from app.messaging.inbound import parse_queue_message
            parse = parse_queue_message
        if run_task is None or handle_call is None:
            from app import tasks
            run_task = run_task or tasks.run_internal_task
            handle_call = handle_call or tasks.handle_call_event
        self.queue = queue
        self.process = process
        self.parse = parse
        self.run_task = run_task
        self.handle_call = handle_call
        self._stop = asyncio.Event()
        # Sender groups in hand, and per sender its latest group (ordering across batches).
        self._in_flight: Set["asyncio.Task[None]"] = set()
        self._sender_tails: Dict[str, "asyncio.Task[None]"] = {}

    def request_stop(self, *_: Any) -> None:
        if not self._stop.is_set():
            print("[Worker] Stop requested: finishing messages in hand, then exiting.")
        self._stop.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def _change_visibility(self, receipt_handle: str, seconds: int) -> bool:
        """ChangeMessageVisibility through the queue's SQS client (queue_service only has
        `release`, which sets 0). Never raises: a failed heartbeat only risks a redelivery, which
        the pipeline's idempotency handles."""
        try:
            client = getattr(self.queue, "client", None)
            if client is None:
                return False
            client.change_message_visibility(
                QueueUrl=self.queue.queue_url, ReceiptHandle=receipt_handle, VisibilityTimeout=int(seconds))
            return True
        except Exception as e:
            print(f"[Worker] Could not change message visibility to {seconds}s: {e}")
            return False

    async def _heartbeat(self, receipt_handle: str) -> None:
        """Keeps extending the visibility of a message in hand until cancelled."""
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            await asyncio.to_thread(self._change_visibility, receipt_handle, VISIBILITY_EXTENSION_SECONDS)

    async def handle(self, sqs_message: Dict[str, Any]) -> bool:
        """Processes one SQS message. Deletes it and returns True only on success."""
        sqs_id = sqs_message.get("MessageId")
        receipt = sqs_message.get("ReceiptHandle")
        heartbeat = asyncio.create_task(self._heartbeat(receipt)) if receipt else None
        try:
            items = self.parse(sqs_message.get("Body", ""))
            received_at = _sent_at(sqs_message)
            for msg in items:
                if isinstance(msg, InboundCallEvent):
                    result = await self.handle_call(msg)
                    print(f"[Worker] call {msg.call_id} {msg.event} -> {result.get('status')}")
                    continue
                if isinstance(msg, InternalTask):
                    await self._run_task(msg)
                    continue
                result = await self.process(msg, received_at=received_at)
                if result.get("status") == "IN_PROGRESS":
                    # Another worker holds the message claim (the retry will see COMPLETE), or the
                    # sender is busy with another message (retry soon, not after the full timeout).
                    retry_after = result.get("retry_after_seconds")
                    reason = result.get("reason") or "message claimed"
                    print(f"[Worker] {msg.provider_message_id} in progress elsewhere ({reason}); "
                          f"leaving SQS message {sqs_id}.")
                    if retry_after is not None and receipt:
                        if heartbeat:
                            heartbeat.cancel()
                            heartbeat = None
                        await asyncio.to_thread(self._change_visibility, receipt, int(retry_after))
                    return False
                print(f"[Worker] {msg.provider}:{msg.provider_message_id} -> {result.get('status')} "
                      f"{result.get('action') or ''} {result.get('case_id') or ''}".rstrip())
        except Exception as e:
            receives = sqs_message.get("Attributes", {}).get("ApproximateReceiveCount", "?")
            print(f"[Worker] Failed SQS message {sqs_id} (receive #{receives}); it will be retried: {e!r}")
            return False
        finally:
            if heartbeat:
                heartbeat.cancel()
        await asyncio.to_thread(self.queue.delete, sqs_message["ReceiptHandle"])
        return True

    async def _run_task(self, task: InternalTask) -> None:
        """Runs a due internal task, or re-queues it until its run_at (raises on failure)."""
        remaining = seconds_until(task.run_at)
        if remaining > TASK_EARLY_TOLERANCE_SECONDS:
            await self.queue.publish_task(task)
            print(f"[Worker] task {task.kind} {task.task_id} not due for {remaining:.0f}s; re-queued.")
            return
        result = await self.run_task(task)
        print(f"[Worker] task {task.kind} {task.task_id} -> {result.get('status')}")

    def _senders(self, sqs_message: Dict[str, Any]) -> Set[str]:
        """Normalized sender phones of the WhatsApp messages in one SQS message (empty if it can't
        be parsed, and for call events and internal tasks, which are not ordered per sender)."""
        try:
            return {"+" + "".join(ch for ch in (m.sender_phone or "") if ch.isdigit())
                    for m in self.parse(sqs_message.get("Body", ""))
                    if isinstance(m, InboundWhatsAppMessage)}
        except Exception:
            return set()

    def group_by_sender(self, messages: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
        """Splits a batch into groups such that messages sharing a sender are in one group, in
        receive order. Unparseable messages get a group of their own."""
        groups: List[Dict[str, Any]] = []  # [{"senders": set, "idx": [positions]}]
        for i, m in enumerate(messages):
            senders = self._senders(m)
            merged = {"senders": set(senders), "idx": [i]}
            rest = []
            for g in groups:
                if senders & g["senders"]:
                    merged["senders"] |= g["senders"]
                    merged["idx"] += g["idx"]
                else:
                    rest.append(g)
            groups = rest + [merged]
        groups.sort(key=lambda g: min(g["idx"]))
        return [[messages[i] for i in sorted(g["idx"])] for g in groups]

    async def _handle_in_order(self, group: List[Dict[str, Any]],
                               after: Optional[List["asyncio.Task[None]"]] = None) -> None:
        if after:
            # The same sender's messages from an earlier batch go first.
            await asyncio.gather(*after, return_exceptions=True)
        for m in group:
            await self.handle(m)

    def _start(self, messages: List[Dict[str, Any]]) -> List["asyncio.Task[None]"]:
        """Starts one task per sender group. Different senders run concurrently (one slow
        transcription doesn't hold up an emergency or a call); one sender's messages run in order,
        also behind that sender's messages from earlier batches that are still in progress."""
        started = []
        for group in self.group_by_sender(messages):
            senders = set().union(*(self._senders(m) for m in group))
            after = [t for t in {self._sender_tails.get(s) for s in senders} if t is not None and not t.done()]
            task = asyncio.create_task(self._handle_in_order(group, after))
            for sender in senders:
                self._sender_tails[sender] = task
            self._in_flight.add(task)
            task.add_done_callback(self._finished)
            started.append(task)
        return started

    def _finished(self, task: "asyncio.Task[None]") -> None:
        self._in_flight.discard(task)
        for sender in [s for s, t in self._sender_tails.items() if t is task]:
            del self._sender_tails[sender]

    async def _receive(self, max_messages: int, wait_seconds: int) -> List[Dict[str, Any]]:
        messages = await asyncio.to_thread(self.queue.receive, max_messages, wait_seconds)
        if messages and self.stopping:
            for m in messages:
                self.queue.release(m["ReceiptHandle"])
            return []
        return messages or []

    async def poll_once(self, wait_seconds: int = WAIT_SECONDS) -> int:
        """Receives one batch and processes it to the end. Returns the number of SQS messages."""
        messages = await self._receive(BATCH_SIZE, wait_seconds)
        if messages:
            await asyncio.gather(*self._start(messages))
        return len(messages)

    async def _poll_without_waiting(self) -> None:
        """Receives one batch and starts it without waiting for it to finish (`run`)."""
        if len(self._in_flight) >= MAX_IN_FLIGHT:
            await asyncio.wait(self._in_flight, return_when=asyncio.FIRST_COMPLETED)
            return
        messages = await self._receive(min(BATCH_SIZE, MAX_IN_FLIGHT - len(self._in_flight)), WAIT_SECONDS)
        if messages:
            self._start(messages)

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self.request_stop)
            except (NotImplementedError, RuntimeError, ValueError):
                signal.signal(sig, lambda *_: loop.call_soon_threadsafe(self.request_stop))

    async def run(self) -> None:
        self._install_signal_handlers()
        if getattr(self.queue, "inline", False):
            print("[Worker] No SQS queue configured (pure mock mode). Set AWS_ENDPOINT_URL "
                  "(Floci) or MOCK_AWS=False with WEBHOOK_QUEUE_URL.")
            raise SystemExit(2)
        print(f"[Worker] Polling {self.queue.queue_url} (batch {BATCH_SIZE}, wait {WAIT_SECONDS}s).")
        while not self.stopping:
            try:
                await self._poll_without_waiting()
            except Exception as e:
                print(f"[Worker] Poll error: {e!r}")
                await asyncio.sleep(2)
        if self._in_flight:
            print(f"[Worker] Finishing {len(self._in_flight)} message group(s) in hand.")
            await asyncio.gather(*self._in_flight, return_exceptions=True)
        print("[Worker] Stopped cleanly.")


def main() -> None:
    from app.config import settings
    print(f"[Worker] Starting ResQ-AI worker ({settings.ENVIRONMENT}, MOCK_AWS={settings.MOCK_AWS}, "
          f"agent={settings.AGENT_BACKEND}, whatsapp={settings.WHATSAPP_PROVIDER}).")
    from app.admin_alerts import admin_numbers
    admins = admin_numbers()
    if admins:
        print(f"[Worker] No-doctor alerts call {len(admins)} administrator number(s) for ESI 1-2 cases.")
    else:
        print("[Worker] WARNING: no administrator phone number (portal Administrators tab or "
              "ADMIN_ALERT_PHONE_NUMBERS): a case without a doctor is shown in the portal only, no "
              "administrator is called.")
    asyncio.run(Worker().run())


if __name__ == "__main__":
    main()
