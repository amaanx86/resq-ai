"""Internal tasks and WhatsApp call events (WA-0): everything on the inbound queue that is not a
WhatsApp message.

Both run outside `app.pipeline.process_inbound_message`: no message claim, no sender lease and
no per-sender ordering, so a call never waits behind a voice note being triaged.

Internal tasks are work the backend schedules for itself with
`queue_service.publish_internal(kind, payload, delay_seconds)`, e.g. a doctor offer timeout.
A feature registers its handler with `@task_handler("<kind>")` in its own module and adds the
module to `HANDLER_MODULES`, which is imported on first use (so this module imports nothing
heavy). Delivery is at-least-once: every handler must be idempotent.

An unknown kind raises `UnknownTaskKind`, so the SQS message is retried (a newer worker in a
rolling deploy may know it) and then lands in the DLQ.
"""
import importlib
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, Optional

from app.models.schemas import InboundCallEvent, InternalTask

TaskHandler = Callable[[InternalTask], Awaitable[Optional[Dict[str, Any]]]]

# Modules that register task handlers; imported once, on the first task.
HANDLER_MODULES: tuple = ("app.doctor_offers", "app.admin_alerts", "app.doctor_chat")

_handlers: Dict[str, TaskHandler] = {}
_modules_loaded = False


def seconds_until(iso: str) -> float:
    """Seconds from now until an ISO 8601 time (negative when it has passed)."""
    try:
        when = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return 0.0
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (when - datetime.now(timezone.utc)).total_seconds()


class UnknownTaskKind(RuntimeError):
    """No handler is registered for an internal task's kind."""


def task_handler(kind: str) -> Callable[[TaskHandler], TaskHandler]:
    """Registers `fn` as the handler of internal tasks of this kind."""
    def register(fn: TaskHandler) -> TaskHandler:
        _handlers[kind] = fn
        return fn
    return register


def _load_handler_modules() -> None:
    global _modules_loaded
    if not _modules_loaded:
        for name in HANDLER_MODULES:
            importlib.import_module(name)
        _modules_loaded = True


async def run_internal_task(task: InternalTask) -> Dict[str, Any]:
    """Runs one due task. Returns {"status": "DONE", ...handler result}; raises on failure (the
    SQS message is retried)."""
    _load_handler_modules()
    handler = _handlers.get(task.kind)
    if handler is None:
        raise UnknownTaskKind(f"No handler for internal task kind {task.kind!r} (task {task.task_id})")
    result = await handler(task) or {}
    return {"status": "DONE", **result}


async def handle_call_event(event: InboundCallEvent) -> Dict[str, Any]:
    """WhatsApp Calling API event. Until CALL-1 answers calls in the portal, a patient's call
    (`connect`, user-initiated) goes the direct-call way: their doctor is asked to call them back
    from their own WhatsApp (`app.direct_calls`), once per call ID. Other events are logged."""
    print(f"[Calls] {event.provider} call {event.call_id}: {event.event}"
          f"{' ' + event.status if event.status else ''} user={event.user_phone} "
          f"direction={event.direction or '-'} sdp={'yes' if event.sdp else 'no'}"
          f"{' error=' + event.error if event.error else ''}")
    from app import direct_calls  # lazy: keeps this module light
    if (event.event != "connect" or event.direction == "BUSINESS_INITIATED"
            or direct_calls.portal_calls_available()):
        return {"status": "IGNORED", "reason": "nothing to do for this call event"}
    from app.services.dynamo_service import dynamo_service
    key = f"call:{event.call_id}:direct"
    if not dynamo_service.claim_idempotency_key(key):
        return {"status": "DUPLICATE", "call_id": event.call_id}
    try:
        result = await direct_calls.handle_incoming_call(event.user_phone)
    except Exception:
        dynamo_service.release_idempotency_key(key)
        raise
    dynamo_service.complete_idempotency_key(key, result)
    return {"status": "DONE", "call_id": event.call_id, **result}
