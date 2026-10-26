"""No-doctor alerts (ADMIN-1): administrators are told when a case has no doctor.

A case waits for a doctor (`app.doctor_offers`, OFFER-5: status EXHAUSTED, `no_doctor_since`) when
nobody free took it. Then:
- **Portal, every ESI level:** nothing to send. Administrators already receive every case, and the
  portal shows each waiting case in a red banner with an alarm and a browser notification, derived
  from the case (`frontend/src/components/AdminAlerts.jsx`).
- **Phone, ESI 1-2 only:** an Amazon Connect call to every administrator added in the portal with
  "receive alert calls" on (ADMIN-2, `app/routers/admins.py`) and every number in
  ADMIN_ALERT_PHONE_NUMBERS ("An ESI level 1 case has no doctor assigned ... please check the ResQ
  portal"), through the same
  contact flow as the emergency-contact call (it only reads `Language` and `SpokenMessage`). The call
  runs as the internal task `admin_no_doctor_call` in the worker (the API task role has no Connect
  permission, and a decline in the portal can start the waiting period in the API). It repeats every
  ADMIN_ALERT_REPEAT_SECONDS, at most ADMIN_ALERT_MAX_CALLS per waiting period, until an
  administrator acknowledges the alert (`acknowledge`), a doctor takes the case or it is closed.

Idempotency: the call is queued once per waiting period (`admin_call_period`, a conditional write),
and each attempt runs once (`tool:<case>:admin_call:<since>:<attempt>`), so an SQS redelivery never
calls twice.
"""
import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.config import settings
from app.models.schemas import ChatMessage, EmergencyCase, InternalTask
from app.services import events_service
from app.services.dynamo_service import OPEN_CASE_STATUSES, dynamo_service, phone_key
from app.tasks import task_handler

ADMIN_CALL_TASK = "admin_no_doctor_call"
CALL_ESI_LEVELS = (1, 2)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def admin_numbers() -> List[str]:
    """The numbers to call: administrators added in the portal who receive alert calls (ADMIN-2),
    then ADMIN_ALERT_PHONE_NUMBERS; E.164, without duplicates or blanks."""
    numbers = []
    try:
        numbers = [phone_key(a.phone_number) for a in dynamo_service.list_admins() if a.receive_alert_calls]
    except Exception as e:
        print(f"[AdminAlerts] Could not read the administrators (using ADMIN_ALERT_PHONE_NUMBERS only): {e}")
    numbers += [phone_key(n) for n in (settings.ADMIN_ALERT_PHONE_NUMBERS or "").replace(";", ",").split(",")]
    return list(dict.fromkeys(n for n in numbers if len(n) > 4))


def _waiting(case: Optional[EmergencyCase]) -> bool:
    return bool(case and case.status in OPEN_CASE_STATUSES and case.doctor_offer_status == "EXHAUSTED"
                and not case.assigned_doctor_id and case.no_doctor_since)


def calls_admins(case: EmergencyCase) -> bool:
    return (case.esi_level or 5) in CALL_ESI_LEVELS


def spoken_message(case: EmergencyCase) -> str:
    """What the administrator hears (English; the contact flow plays it twice)."""
    why = ("All doctors declined it or did not answer." if case.no_doctor_reason == "ALL_DECLINED"
           else "No doctor is available.")
    return (f"This is ResQ AI with an urgent alert. An ESI level {case.esi_level} case has no doctor assigned. "
            f"{why} Please check the ResQ portal as soon as possible.")


async def _note(case_id: str, text: str) -> None:
    try:
        msg = dynamo_service.save_chat_message(ChatMessage(
            message_id=f"MSG-{uuid.uuid4().hex[:8]}", case_id=case_id, sender="BOT", sender_name="System",
            content=text))
        await events_service.publish_chat_message(msg)
    except Exception as e:
        print(f"[AdminAlerts] Chat note for {case_id} failed: {e}")


async def _publish(case: EmergencyCase) -> None:
    try:
        await events_service.publish_case(case)
    except Exception as e:
        print(f"[AdminAlerts] Event publish failed for {case.case_id}: {e}")


async def _schedule(task_payload: Dict[str, Any], delay_seconds: float) -> None:
    from app.services.queue_service import queue_service  # lazy, like app.doctor_offers
    await queue_service.publish_internal(ADMIN_CALL_TASK, task_payload, delay_seconds=delay_seconds)


async def start(case: EmergencyCase) -> bool:
    """Queues the administrator calls for the case's current waiting period: ESI 1-2 only, once per
    period, not after an acknowledgement. True if they were queued now. Never raises."""
    try:
        if not _waiting(case) or not calls_admins(case) or case.admin_alert_ack_at:
            return False
        since = case.no_doctor_since
        if case.admin_call_period == since:
            return False
        updated = dynamo_service.update_case_if(case.case_id, {"admin_call_period": since},
                                                expect={"admin_call_period": case.admin_call_period,
                                                        "no_doctor_since": since})
        if updated is None:
            return False  # queued by someone else, or the waiting period changed
        numbers = admin_numbers()
        if not numbers:
            print("[AdminAlerts] WARNING: no administrator phone number (portal Administrators tab or "
                  f"ADMIN_ALERT_PHONE_NUMBERS); only the portal shows the no-doctor alert for {case.case_id}.")
            await _note(case.case_id, "🔔 No doctor: administrators are alerted in the portal "
                                      "(no administrator phone number is configured for a call).")
            return False
        await _schedule({"case_id": case.case_id, "since": since, "attempt": 1}, 0)
        await _note(case.case_id, f"🔔 No doctor: calling the administrator{'s' if len(numbers) > 1 else ''} "
                                  f"({len(numbers)} number{'s' if len(numbers) > 1 else ''}).")
        return True
    except Exception as e:
        print(f"[AdminAlerts] Could not queue the administrator call for {case.case_id}: {e}")
        return False


async def _call_all(case: EmergencyCase, numbers: List[str], attempt: int) -> Dict[str, Any]:
    from app.services.telephony_service import telephony_service  # lazy: boto3 client
    message = spoken_message(case)
    results = await asyncio.gather(*(telephony_service.place_announcement_call(n, message, "English")
                                     for n in numbers))
    at = _now()
    calls = [{"since": case.no_doctor_since, "attempt": attempt, "phone": n, "status": r.get("status", "FAILED"),
              "contact_id": r.get("contact_id"), "simulated": r.get("simulated", False),
              "error": r.get("error"), "at": at} for n, r in zip(numbers, results)]
    dynamo_service.update_case_if(case.case_id, {}, append={"admin_alert_calls": calls})
    placed = [c for c in calls if c["status"] != "FAILED"]
    lines = ", ".join(f"{c['phone']}: {'placed' if c['status'] != 'FAILED' else 'failed (' + (c['error'] or 'error') + ')'}"
                      for c in calls)
    await _note(case.case_id, f"📞 Administrator alert call {attempt}/{settings.ADMIN_ALERT_MAX_CALLS}: {lines}.")
    updated = dynamo_service.get_case(case.case_id)
    if updated:
        await _publish(updated)
    return {"tool": "admin_no_doctor_call", "status": "CALLED" if placed else "FAILED",
            "attempt": attempt, "calls": calls}


@task_handler(ADMIN_CALL_TASK)
async def handle_admin_call(task: InternalTask) -> Dict[str, Any]:
    """One round of administrator calls for a waiting case, then the next one queued. Does nothing
    once the case has a doctor, is closed, waits in a new period, or the alert was acknowledged."""
    from app.agents.tools import run_once  # lazy: the tools import the AWS service singletons
    case_id, since = task.payload.get("case_id"), task.payload.get("since")
    attempt = int(task.payload.get("attempt") or 1)
    case = dynamo_service.get_case(case_id) if case_id else None
    if not _waiting(case) or case.no_doctor_since != since:
        return {"case_id": case_id, "outcome": "stale"}
    if case.admin_alert_ack_at:
        return {"case_id": case_id, "outcome": "acknowledged", "by": case.admin_alert_ack_by}
    if attempt > settings.ADMIN_ALERT_MAX_CALLS:
        return {"case_id": case_id, "outcome": "max_calls"}
    numbers = admin_numbers()
    if not numbers:
        return {"case_id": case_id, "outcome": "no_numbers"}
    result = await run_once(case_id, f"admin_call:{since}:{attempt}", lambda: _call_all(case, numbers, attempt))
    if attempt < settings.ADMIN_ALERT_MAX_CALLS:
        try:
            await _schedule({"case_id": case_id, "since": since, "attempt": attempt + 1},
                            settings.ADMIN_ALERT_REPEAT_SECONDS)
        except Exception as e:
            print(f"[AdminAlerts] Could not queue call {attempt + 1} for {case_id}: {e}")
    return {"case_id": case_id, "outcome": "called", "attempt": attempt, "status": result.get("status"),
            "replay": bool(result.get("idempotent_replay"))}


async def acknowledge(case_id: str, by: str) -> Optional[EmergencyCase]:
    """An administrator takes care of a waiting case: stops the repeat calls (and the portal alarm)
    for this waiting period. Returns the case; None if it is not waiting for a doctor."""
    case = dynamo_service.get_case(case_id)
    if not _waiting(case):
        return None
    if case.admin_alert_ack_at:
        return case
    updated = dynamo_service.update_case_if(case_id, {"admin_alert_ack_by": by, "admin_alert_ack_at": _now()},
                                            expect={"no_doctor_since": case.no_doctor_since,
                                                    "admin_alert_ack_at": None})
    if updated is None:
        current = dynamo_service.get_case(case_id)
        return current if _waiting(current) else None
    print(f"[AdminAlerts] {by} acknowledged the no-doctor alert of {case_id}.")
    await _note(case_id, f"🔕 {by} acknowledged the no-doctor alert and is handling the case.")
    await _publish(updated)
    return updated
