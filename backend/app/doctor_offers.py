"""Doctor case offers (OFFER-1..5): a doctor accepts a case before it is assigned to them.

The match tool only ranks doctors (`app.agents.tools.rank_candidates`); this module, run by the
worker after the case is saved, offers the case and assigns it to the first doctor who accepts.

Rounds (OFFER-3): each round has a fresh `offer_id` and goes to the next best FREE doctors who have
not declined (re-ranked every round, so a doctor who went off duty is skipped): the top
DOCTOR_OFFER_FANOUT_URGENT doctors at once for ESI 1-2 (human-review cases are ESI 2), one doctor for
ESI 3. A delayed internal task (`doctor_offer_timeout`, DOCTOR_OFFER_TIMEOUT_ESI1/2/3 seconds) ends
the round. A decline by every doctor of the round, or the timeout, starts the next round. There is
no round limit: rounds go on until no free doctor who has not been asked is left.

One case per doctor (OFFER-4): a doctor never holds two cases, nor an offer while they hold a case
or another offer. Each doctor offered a case is locked to it first (`dynamo_service.claim_doctor`,
a conditional write on the doctor), so two cases offered at the same moment can't both reach the
same doctor; accepting turns the lock into a CASE lock, and the doctor is released when they decline,
their round ends without them, another doctor takes the case, or the case is closed, handed over or
reassigned. An accept or admin assignment while the doctor holds another case is refused (BUSY).

Waiting cases (OFFER-5): when nobody (free) is left the case waits: `doctor_offer_status` EXHAUSTED,
`no_doctor_since` / `no_doctor_reason`. The patient is told to stay on the chat (and to call the
emergency numbers if it gets worse), administrators are alerted (`app.admin_alerts`, ADMIN-1), and
the case is offered again as soon as a doctor is free (`offer_waiting_cases`, run whenever a doctor
is released or comes on duty; a `waiting_case_recheck` task every WAITING_CASE_RECHECK_SECONDS is the
safety net). Doctors who declined the case are not asked again; an administrator can still assign
anyone free by hand (`assign_by_admin`).

Concurrency: every case change is a conditional write (`update_case_if`) that expects the round's
`offer_id`, so of two doctors accepting at once exactly one wins, a late accept or a stale timeout
changes nothing, and a decline racing a timeout starts one next round, not two.

The offer reaches doctors as a WhatsApp template with Accept / Decline quick replies (payloads
`OFFER:ACCEPT|DECLINE:<case_id>:<offer_id>`, handled by `handle_button`) and in the portal: the
case is visible to the offered doctors while the offer is pending (`auth.can_see_case`), so the
`case_upserted` event brings it to them (`POST /api/v1/cases/{id}/offer/accept|decline`).

Patient safety: for ESI 1-2 nothing waits for the offer; the contact call, the location request and
the first reply go out at once, and the patient gets a second message when a doctor accepts.
"""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from app import admin_alerts
from app.config import settings
from app.models.schemas import ChatMessage, DoctorProfile, EmergencyCase, InboundWhatsAppMessage, InternalTask
from app.services import events_service
from app.services.dynamo_service import OPEN_CASE_STATUSES, dynamo_service
from app.services.whatsapp_service import whatsapp_service
from app.tasks import task_handler

TIMEOUT_TASK = "doctor_offer_timeout"
RECHECK_TASK = "waiting_case_recheck"
LOCAL_EMERGENCY_NUMBERS = "1122 / 115 (Pakistan) or 112 (India)"
# A PENDING round this far past its expiry is treated as timed out when the offer is started again
# (e.g. the worker died before it queued the timeout task). Offer locks last as long.
STALE_ROUND_GRACE_SECONDS = 30

# Results of accept_offer / decline_offer.
ACCEPTED, ALREADY_YOURS, TAKEN, CLOSED, NOT_FOUND = "ACCEPTED", "ALREADY_YOURS", "TAKEN", "CLOSED", "NOT_FOUND"
DECLINED, NOT_PENDING = "DECLINED", "NOT_PENDING"
BUSY = "BUSY"  # the doctor already holds another case (OFFER-4)

# Why a case waits (OFFER-5).
NO_DOCTOR_FREE, ALL_DECLINED = "NO_DOCTOR_FREE", "ALL_DECLINED"


class DoctorBusyError(RuntimeError):
    """An administrator tried to assign a case to a doctor who holds another one (OFFER-4)."""

    def __init__(self, doctor: DoctorProfile, busy_case_id: Optional[str]):
        super().__init__(f"{doctor.name} is busy with case {busy_case_id or 'another case'}")
        self.doctor = doctor
        self.busy_case_id = busy_case_id


def _now() -> datetime:
    return datetime.now(timezone.utc)


def offer_timeout_seconds(esi_level: Optional[int]) -> int:
    if esi_level == 1:
        return settings.DOCTOR_OFFER_TIMEOUT_ESI1
    if esi_level == 2:
        return settings.DOCTOR_OFFER_TIMEOUT_ESI2
    return settings.DOCTOR_OFFER_TIMEOUT_ESI3


def offer_fanout(esi_level: Optional[int]) -> int:
    """Doctors offered a case at once: the top few for ESI 1-2, one for ESI 3."""
    return max(1, settings.DOCTOR_OFFER_FANOUT_URGENT) if (esi_level or 5) <= 2 else 1


def answer_within(seconds: int) -> str:
    """60 -> "60 seconds", 300 -> "5 minutes"."""
    if seconds >= 120 and seconds % 60 == 0:
        return f"{seconds // 60} minutes"
    return f"{seconds} seconds"


def payload(action: str, case_id: str, offer_id: str) -> str:
    return f"OFFER:{action}:{case_id}:{offer_id}"


def portal_case_url(case_id: str) -> str:
    return f"{settings.PORTAL_BASE_URL.rstrip('/')}/?case={case_id}"


def waiting_text(esi_level: Optional[int]) -> str:
    """What the patient is told while no doctor is free (OFFER-5)."""
    if (esi_level or 5) <= 2:
        worse = f"If it gets worse, call {LOCAL_EMERGENCY_NUMBERS}."
    else:
        worse = f"If it gets worse, go to the nearest emergency department or call {LOCAL_EMERGENCY_NUMBERS}."
    return ("⏳ No doctor is free right now. Stay on this chat: your case will go to a doctor as soon as "
            f"one is free. {worse}")


def is_waiting(case: Optional[EmergencyCase]) -> bool:
    """An open case that no free doctor took (OFFER-5)."""
    return bool(case and case.status in OPEN_CASE_STATUSES and case.doctor_offer_status == "EXHAUSTED"
                and not case.assigned_doctor_id)


def _close_current_round(case: EmergencyCase, outcome: str, **extra: Any) -> List[Dict[str, Any]]:
    """offer_history with the current round's outcome set."""
    history = [dict(r) for r in case.offer_history or []]
    if history and history[-1].get("offer_id") == case.offer_id and history[-1].get("outcome") == "PENDING":
        history[-1].update(outcome=outcome, at=_now().isoformat(), **extra)
    return history


# ---------------------------------------------------------------------------- side effects (best effort)
async def _send(method: str, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    try:
        return await getattr(whatsapp_service, method)(*args, **kwargs) or {}
    except Exception as e:
        print(f"[Offers] WhatsApp {method} failed: {e}")
        return {"status": "ERROR", "detail": str(e)}


async def _note(case_id: str, text: str, sender: str = "BOT", sender_name: str = "System") -> None:
    try:
        msg = dynamo_service.save_chat_message(ChatMessage(
            message_id=f"MSG-{uuid.uuid4().hex[:8]}", case_id=case_id, sender=sender,
            sender_name=sender_name, content=text))
        await events_service.publish_chat_message(msg)
    except Exception as e:
        print(f"[Offers] Chat note for {case_id} failed: {e}")


async def _publish(case: EmergencyCase) -> None:
    try:
        await events_service.publish_case(case)
    except Exception as e:
        print(f"[Offers] Event publish failed for case {case.case_id}: {e}")


async def _schedule(kind: str, task_payload: Dict[str, Any], delay_seconds: float) -> None:
    """Queues an internal task (`app.tasks`). Raises if it can't be queued."""
    from app.services.queue_service import queue_service  # lazy: queue_service imports the pipeline lazily too
    await queue_service.publish_internal(kind, task_payload, delay_seconds=delay_seconds)


async def _schedule_timeout(case: EmergencyCase, seconds: int) -> None:
    try:
        await _schedule(TIMEOUT_TASK, {"case_id": case.case_id, "offer_id": case.offer_id}, seconds)
    except Exception as e:
        # Without the timer the round would never end: fail loudly so the caller (worker) retries.
        print(f"[Offers] Could not schedule the timeout of {case.offer_id} for {case.case_id}: {e}")
        raise


async def _schedule_recheck(case: EmergencyCase) -> None:
    """The waiting case's safety net (OFFER-5). Best effort: a release or a doctor coming on duty
    offers the case too."""
    try:
        await _schedule(RECHECK_TASK, {"case_id": case.case_id, "since": case.no_doctor_since},
                        settings.WAITING_CASE_RECHECK_SECONDS)
    except Exception as e:
        print(f"[Offers] Could not schedule the waiting re-check of {case.case_id}: {e}")


# ---------------------------------------------------------------------------- doctor locks (OFFER-4)
def _stale_lock(doctor: DoctorProfile, case_id: str) -> Optional[str]:
    """The case of the doctor's CASE lock if that case is closed or no longer theirs (a release was
    missed), so the lock may be replaced; None otherwise."""
    if not doctor.busy_case_id or doctor.busy_case_id == case_id or doctor.busy_kind != "CASE":
        return None
    held = dynamo_service.get_case(doctor.busy_case_id)
    if held is None or held.status not in OPEN_CASE_STATUSES or held.assigned_doctor_id != doctor.doctor_id:
        return doctor.busy_case_id
    return None


def _claim(doctor: DoctorProfile, case_id: str, kind: str, until: Optional[str] = None) -> Optional[DoctorProfile]:
    current = dynamo_service.get_doctor(doctor.doctor_id) or doctor
    return dynamo_service.claim_doctor(doctor.doctor_id, case_id, kind, until=until,
                                       replace_case_id=_stale_lock(current, case_id))


async def _release(case_id: str, doctor_ids: Iterable[str], closed: bool = False) -> List[str]:
    """Frees the doctors locked to this case; returns the ones that were. Best effort."""
    released: List[str] = []
    for doctor_id in dict.fromkeys(d for d in doctor_ids if d):
        try:
            if await asyncio.to_thread(dynamo_service.release_doctor, doctor_id, case_id, closed):
                released.append(doctor_id)
        except Exception as e:
            print(f"[Offers] Releasing {doctor_id} from {case_id} failed: {e}")
    return released


async def offer_waiting_cases(exclude_case_id: Optional[str] = None) -> List[str]:
    """A doctor became free: offers the waiting cases again (OFFER-5), lowest ESI first, then the
    longest waiting, while free doctors are left. Returns the cases that got a new round. Never raises."""
    from app.agents.tools import rank_candidates
    started: List[str] = []
    try:
        waiting = await asyncio.to_thread(dynamo_service.list_waiting_cases)
        for case in waiting:
            if case.case_id == exclude_case_id:
                continue
            if not await asyncio.to_thread(rank_candidates, "General Emergency", (), case.case_id):
                break  # nobody free at all
            updated = await start_offer(case)
            if updated.doctor_offer_status == "PENDING":
                started.append(case.case_id)
    except Exception as e:
        print(f"[Offers] Offering waiting cases failed: {e}")
    if started:
        print(f"[Offers] Waiting cases offered again: {', '.join(started)}.")
    return started


async def release_case_doctors(case: EmergencyCase, closed: bool = False) -> List[str]:
    """The case is closed (or handed back): frees its doctor and anyone with a pending offer for it,
    then offers the waiting cases to them. Returns the released doctor IDs."""
    ids = list(case.offered_doctor_ids or []) + ([case.assigned_doctor_id] if case.assigned_doctor_id else [])
    released = await _release(case.case_id, [d for d in ids if d != case.assigned_doctor_id])
    if case.assigned_doctor_id:
        released += await _release(case.case_id, [case.assigned_doctor_id], closed=closed)
    if released:
        await offer_waiting_cases(exclude_case_id=case.case_id)
    return released


async def doctor_came_on_duty(doctor_id: str) -> List[str]:
    """A doctor switched on duty (or was registered): offer them a waiting case."""
    return await offer_waiting_cases()


# ---------------------------------------------------------------------------- rounds
async def start_offer(case: EmergencyCase, restart: bool = False) -> EmergencyCase:
    """Starts offering an open case that has no doctor yet. Idempotent (an SQS retry of the message
    that opened the case changes nothing): a pending or accepted offer is left alone, except
    - `restart`: a pending round is superseded by a new one with the case's current ESI (a
      follow-up escalated the case, so the round's timeout and fan-out are now wrong);
    - a pending round long past its expiry (its timeout task was lost) is timed out now;
    - otherwise a pending round only gets its timeout task queued again.
    A waiting case (EXHAUSTED) gets a new round only if a free doctor who has not declined it exists.
    Returns the case as it is afterwards."""
    if case.status not in OPEN_CASE_STATUSES or case.assigned_doctor_id:
        return case
    if case.doctor_offer_status == "PENDING":
        expired = case.offer_expires_at and (
            datetime.fromisoformat(case.offer_expires_at) + timedelta(seconds=STALE_ROUND_GRACE_SECONDS) < _now())
        if expired:
            return await next_round(case, "TIMED_OUT", also_exclude=case.offered_doctor_ids)
        if restart:
            return await next_round(case, "SUPERSEDED")
        # A retry of the message that started the round: re-arm its timer in case the first attempt
        # died before queueing it (a duplicate timer is harmless: the timeout checks the offer_id).
        remaining = (datetime.fromisoformat(case.offer_expires_at) - _now()).total_seconds() if case.offer_expires_at else 0
        await _schedule_timeout(case, max(0, int(remaining)))
        return case
    if case.doctor_offer_status == "ACCEPTED":
        return case
    if case.doctor_offer_status == "EXHAUSTED":
        from app.agents.tools import rank_candidates
        free = await asyncio.to_thread(rank_candidates, case.matched_specialty or "General Emergency",
                                       case.declined_doctor_ids or [], case.case_id)
        if not free:
            # Still nobody: a follow-up may have made it ESI 1-2, which calls the administrators.
            await admin_alerts.start(case)
            return case
    return await next_round(case, None)


async def _claim_round(case: EmergencyCase, ranked: Sequence[Tuple[DoctorProfile, str]], count: int,
                       until: str) -> List[DoctorProfile]:
    """Locks the best `count` doctors of `ranked` to the case's next round (OFFER-4); a doctor whose
    lock fails (taken by another case meanwhile) is skipped."""
    doctors: List[DoctorProfile] = []
    for doctor, _ in ranked:
        if len(doctors) >= count:
            break
        if await asyncio.to_thread(_claim, doctor, case.case_id, "OFFER", until):
            doctors.append(doctor)
    return doctors


async def next_round(case: EmergencyCase, closing_outcome: Optional[str],
                     also_exclude: Iterable[str] = ()) -> EmergencyCase:
    """Ends the current round with `closing_outcome` (None: there is none) and offers the case to the
    next free doctors, or lets it wait (EXHAUSTED). `also_exclude`: doctors to add to the declined
    list (a timed-out round's doctors). The previous round's doctors who are not offered again are
    released and offered the waiting cases."""
    declined = list(dict.fromkeys([*(case.declined_doctor_ids or []), *also_exclude]))
    history = _close_current_round(case, closing_outcome) if closing_outcome else list(case.offer_history or [])
    previous = list(case.offered_doctor_ids or []) if case.doctor_offer_status == "PENDING" else []
    specialty = case.matched_specialty or "General Emergency"
    from app.agents.tools import rank_candidates  # lazy: the tools import the AWS service singletons
    ranked = await asyncio.to_thread(rank_candidates, specialty, declined, case.case_id)
    timeout = offer_timeout_seconds(case.esi_level)
    now = _now()
    expires_at = now + timedelta(seconds=timeout)
    doctors = await _claim_round(case, ranked, offer_fanout(case.esi_level),
                                 (expires_at + timedelta(seconds=STALE_ROUND_GRACE_SECONDS)).isoformat())
    if not doctors:
        updated = await _exhaust(case, history, declined)
        if await _release(case.case_id, previous):
            await offer_waiting_cases(exclude_case_id=case.case_id)
        return updated

    used = sum(1 for r in history if r.get("outcome") != "SUPERSEDED")
    offer_id = f"OFR-{uuid.uuid4().hex[:10].upper()}"
    expires = expires_at.isoformat()
    doctor_ids = [d.doctor_id for d in doctors]
    history.append({"offer_id": offer_id, "doctor_ids": doctor_ids, "sent_at": now.isoformat(),
                    "expires_at": expires, "esi_level": case.esi_level, "outcome": "PENDING"})
    updated = dynamo_service.update_case_if(case.case_id, {
        "doctor_offer_status": "PENDING",
        "offer_id": offer_id,
        "offered_doctor_ids": doctor_ids,
        "offer_expires_at": expires,
        "declined_doctor_ids": declined,
        "offer_history": history,
        "offer_started_at": case.offer_started_at or now.isoformat(),
    }, expect={"offer_id": case.offer_id, "assigned_doctor_id": None})
    if updated is None:
        # Another worker moved the offer on first (accept, decline or timeout won the race): free the
        # doctors claimed here unless the winner offered them the case too.
        print(f"[Offers] Round for {case.case_id} not started: the offer changed concurrently.")
        current = dynamo_service.get_case(case.case_id) or case
        keep = set(current.offered_doctor_ids or []) if current.doctor_offer_status == "PENDING" else set()
        keep |= {current.assigned_doctor_id} if current.assigned_doctor_id else set()
        await _release(case.case_id, [d for d in doctor_ids if d not in keep])
        return current

    await _schedule_timeout(updated, timeout)
    names = ", ".join(d.name for d in doctors)
    print(f"[Offers] {case.case_id} round {used + 1}: {offer_id} to {names} ({timeout}s).")
    await _publish(updated)
    results = await asyncio.gather(*(_offer_to(updated, d, timeout) for d in doctors))
    channels = ", ".join(f"{d.name}: {r}" for d, r in zip(doctors, results))
    await _note(case.case_id, f"🩺 Case offered to {names} (round {used + 1}, {answer_within(timeout)} to accept). "
                              f"WhatsApp: {channels}.")
    if await _release(case.case_id, [d for d in previous if d not in doctor_ids]):
        await offer_waiting_cases(exclude_case_id=case.case_id)
    return updated


async def _offer_to(case: EmergencyCase, doctor: DoctorProfile, timeout: int) -> str:
    """Sends one doctor the offer; returns how it went out (template / interactive / failed)."""
    result = await _send(
        "send_doctor_case_offer", doctor.phone_number, case.esi_level,
        case.matched_specialty or "General Emergency", case.clinical_summary or "", answer_within(timeout),
        payload("ACCEPT", case.case_id, case.offer_id), payload("DECLINE", case.case_id, case.offer_id))
    if result.get("status") == "ERROR":
        return f"not delivered ({result.get('detail') or 'error'}), portal only"
    return result.get("channel") or "sent"


async def _exhaust(case: EmergencyCase, history: List[Dict[str, Any]], declined: List[str]) -> EmergencyCase:
    """Nobody (free) is left to offer the case to: it waits (OFFER-5). A new waiting period tells the
    patient to stay on the chat (unless nobody was ever offered the case: the first reply already
    said so), alerts the administrators (ADMIN-1) and queues the re-check. A case already waiting
    keeps its waiting period: nothing is sent again."""
    reason = ALL_DECLINED if declined else NO_DOCTOR_FREE
    already = case.doctor_offer_status == "EXHAUSTED"
    if already and declined == list(case.declined_doctor_ids or []) and history == list(case.offer_history or []):
        await admin_alerts.start(case)  # e.g. a follow-up made the waiting case ESI 1-2
        return case
    changes: Dict[str, Any] = {
        "doctor_offer_status": "EXHAUSTED",
        "offered_doctor_ids": [],
        "offer_expires_at": None,
        "declined_doctor_ids": declined,
        "offer_history": history,
    }
    if not already:
        changes.update(no_doctor_since=_now().isoformat(), no_doctor_reason=reason,
                       admin_alert_ack_by=None, admin_alert_ack_at=None)
    updated = dynamo_service.update_case_if(case.case_id, changes,
                                            expect={"offer_id": case.offer_id, "assigned_doctor_id": None})
    if updated is None:
        return dynamo_service.get_case(case.case_id) or case
    await _publish(updated)
    if already:
        await admin_alerts.start(updated)
        return updated

    rounds = len(history)
    print(f"[Offers] {case.case_id}: no free doctor took it ({reason}, {rounds} round(s)); the case waits.")
    why = (f"every free doctor declined or did not answer ({rounds} offer round(s))" if reason == ALL_DECLINED
           else "no doctor is free (nobody on duty, or all busy)")
    await _note(case.case_id, f"⚠️ No doctor for this case: {why}. It is offered again as soon as a doctor is "
                              "free; administrators are alerted and can assign a doctor.")
    if history:
        text = waiting_text(case.esi_level)
        await _send("send_text_message", case.user_phone, text)
        await _note(case.case_id, text, sender_name="ResQ-AI")
    await _schedule_recheck(updated)
    await admin_alerts.start(updated)
    return updated


@task_handler(RECHECK_TASK)
async def handle_recheck(task: InternalTask) -> Dict[str, Any]:
    """A waiting case's safety net: offers it if a free doctor exists, and checks again later while it
    still waits. Stops once the case has a doctor, is closed, or started another waiting period."""
    case_id, since = task.payload.get("case_id"), task.payload.get("since")
    case = dynamo_service.get_case(case_id) if case_id else None
    if not is_waiting(case) or case.no_doctor_since != since:
        return {"case_id": case_id, "outcome": "stale"}
    updated = await start_offer(case)
    if is_waiting(updated) and updated.no_doctor_since == since:
        await _schedule_recheck(updated)
        return {"case_id": case_id, "outcome": "still_waiting"}
    return {"case_id": case_id, "outcome": "offered", "doctor_offer_status": updated.doctor_offer_status}


# ---------------------------------------------------------------------------- answers
async def accept_offer(case_id: str, offer_id: Optional[str], doctor: DoctorProfile) -> Tuple[str, Optional[EmergencyCase]]:
    """The doctor accepts. Exactly one accept per round wins (conditional write); it assigns the
    doctor, counts the case in their cases_handled and tells the patient. `offer_id` None (portal):
    the current round. Returns (ACCEPTED | ALREADY_YOURS | TAKEN | CLOSED | NOT_FOUND | BUSY, case)."""
    case = dynamo_service.get_case(case_id)
    if case is None:
        return NOT_FOUND, None
    if case.assigned_doctor_id == doctor.doctor_id:
        return ALREADY_YOURS, case
    if case.status not in OPEN_CASE_STATUSES:
        return CLOSED, case
    offer_id = offer_id or case.offer_id
    if offer_id != case.offer_id:
        return TAKEN, case
    if (case.doctor_offer_status != "PENDING" or doctor.doctor_id not in (case.offered_doctor_ids or [])
            or doctor.doctor_id in (case.declined_doctor_ids or [])):
        return TAKEN, case
    # One case per doctor: lock them to this case before taking it.
    if not await asyncio.to_thread(_claim, doctor, case_id, "CASE"):
        print(f"[Offers] {doctor.doctor_id} tried to accept {case_id} while holding another case.")
        return BUSY, case
    now = _now().isoformat()
    updated = dynamo_service.update_case_if(case_id, {
        "doctor_offer_status": "ACCEPTED",
        "assigned_doctor_id": doctor.doctor_id,
        "assigned_doctor_name": doctor.name,
        "accepted_at": now,
        "offer_expires_at": None,
        "offer_history": _close_current_round(case, "ACCEPTED", doctor_id=doctor.doctor_id),
    }, expect={"offer_id": offer_id, "doctor_offer_status": "PENDING", "assigned_doctor_id": None},
       member=("offered_doctor_ids", doctor.doctor_id), not_member=("declined_doctor_ids", doctor.doctor_id))
    if updated is None:
        current = dynamo_service.get_case(case_id) or case
        if current.assigned_doctor_id == doctor.doctor_id:
            return ALREADY_YOURS, current
        await _release(case_id, [doctor.doctor_id])
        return TAKEN, current

    await asyncio.to_thread(dynamo_service.increment_doctor_cases_handled, doctor.doctor_id)
    print(f"[Offers] {case_id} accepted by {doctor.doctor_id} ({offer_id}).")
    await _publish(updated)
    await _note(case_id, f"✅ {doctor.name} accepted the case.")
    specialty = updated.matched_specialty or doctor.specialty
    patient_text = (f"✅ *{doctor.name}* ({specialty}) has accepted your case and is reviewing it now. "
                    "Anything you send here goes straight to the doctor."
                    + (" The doctor will arrange an ambulance." if (updated.esi_level or 5) <= 2 else ""))
    await _tell_patient_assigned(updated, patient_text)
    await _tell_doctor_assigned(updated, doctor, "Case accepted")
    others = [d for d in updated.offered_doctor_ids if d != doctor.doctor_id]
    await _release(case_id, others)
    await asyncio.gather(*(_tell_taken(d, case_id) for d in others if d not in (updated.declined_doctor_ids or [])))
    if others:
        await offer_waiting_cases(exclude_case_id=case_id)
    return ACCEPTED, updated


async def _tell_patient_assigned(case: EmergencyCase, text: str) -> None:
    """The patient's "your doctor" message, with the Request a call button (app.direct_calls)."""
    from app import direct_calls  # lazy: direct_calls imports this module
    await direct_calls.send_doctor_assigned(case, text)
    await _note(case.case_id, text, sender_name="ResQ-AI")


async def _tell_doctor_assigned(case: EmergencyCase, doctor: DoctorProfile, header: str) -> None:
    """The doctor's case card on WhatsApp (DOC-2): the case, how to reply to the patient, the
    @resq commands, the optional portal link; then what the patient said so far."""
    from app import doctor_chat  # lazy: doctor_chat imports this module
    await doctor_chat.send_case_card(case, doctor, header=header, catch_up=True)
    await doctor_chat.schedule_idle_reminder(case, doctor)


async def _direct_call_line(case: EmergencyCase, doctor: DoctorProfile) -> str:
    """Text with an Open chat & call link for the doctor's WhatsApp ("" if it could not be made)."""
    from app import direct_calls
    link = await direct_calls.create_link_or_none(case, doctor)
    return (f"\n\nTo call the patient from your own WhatsApp (they will see your number): {link}"
            if link else "")


async def _tell_taken(doctor_id: str, case_id: str) -> None:
    other = dynamo_service.get_doctor(doctor_id)
    if other:
        await _send("send_text_message", other.phone_number,
                    f"Case {case_id} was accepted by another doctor. No action needed.")


async def decline_offer(case_id: str, offer_id: Optional[str], doctor: DoctorProfile) -> Tuple[str, Optional[EmergencyCase]]:
    """The doctor declines; they are free again (and may be offered a waiting case). When every
    doctor of the round has declined, the next round starts.
    Returns (DECLINED | NOT_PENDING | NOT_FOUND, case)."""
    case = dynamo_service.get_case(case_id)
    if case is None:
        return NOT_FOUND, None
    offer_id = offer_id or case.offer_id
    if case.status not in OPEN_CASE_STATUSES or offer_id != case.offer_id:
        return NOT_PENDING, case
    updated = dynamo_service.update_case_if(
        case_id, {}, expect={"offer_id": offer_id, "doctor_offer_status": "PENDING"},
        member=("offered_doctor_ids", doctor.doctor_id), not_member=("declined_doctor_ids", doctor.doctor_id),
        append={"declined_doctor_ids": [doctor.doctor_id]})
    if updated is None:
        return NOT_PENDING, dynamo_service.get_case(case_id) or case
    print(f"[Offers] {case_id}: {doctor.doctor_id} declined ({offer_id}).")
    await _note(case_id, f"↩️ {doctor.name} declined the case.")
    await _release(case_id, [doctor.doctor_id])
    if all(d in updated.declined_doctor_ids for d in updated.offered_doctor_ids):
        updated = await next_round(updated, "DECLINED")
    else:
        await _publish(updated)
    await offer_waiting_cases(exclude_case_id=case_id)
    return DECLINED, updated


@task_handler(TIMEOUT_TASK)
async def handle_timeout(task: InternalTask) -> Dict[str, Any]:
    """The round's time is up: its doctors count as declined and the next round starts. A timeout of
    an older round (already accepted, declined or superseded) or of a closed case does nothing."""
    case_id, offer_id = task.payload.get("case_id"), task.payload.get("offer_id")
    case = dynamo_service.get_case(case_id) if case_id else None
    if (case is None or case.offer_id != offer_id or case.doctor_offer_status != "PENDING"
            or case.status not in OPEN_CASE_STATUSES):
        return {"case_id": case_id, "offer_id": offer_id, "outcome": "stale"}
    waiting = [d for d in case.offered_doctor_ids if d not in case.declined_doctor_ids]
    await _note(case_id, f"⏱️ Offer {offer_id} expired without an answer.")
    updated = await next_round(case, "TIMED_OUT", also_exclude=waiting)
    return {"case_id": case_id, "offer_id": offer_id, "outcome": "next_round",
            "doctor_offer_status": updated.doctor_offer_status, "new_offer_id": updated.offer_id}


async def assign_by_admin(case_id: str, doctor: DoctorProfile, assigned_by: str) -> Optional[EmergencyCase]:
    """An administrator assigns the case by hand (e.g. while it waits); ends any pending round.
    Raises DoctorBusyError when the doctor holds another case (OFFER-4)."""
    case = dynamo_service.get_case(case_id)
    if case is None:
        return None
    if case.assigned_doctor_id == doctor.doctor_id:
        return case
    if not await asyncio.to_thread(_claim, doctor, case_id, "CASE"):
        current = dynamo_service.get_doctor(doctor.doctor_id) or doctor
        raise DoctorBusyError(current, current.busy_case_id)
    previous_doctor = case.assigned_doctor_id
    history = _close_current_round(case, "SUPERSEDED") if case.doctor_offer_status == "PENDING" else case.offer_history
    updated = dynamo_service.update_case(case_id, {
        "doctor_offer_status": "ACCEPTED",
        "assigned_doctor_id": doctor.doctor_id,
        "assigned_doctor_name": doctor.name,
        "accepted_at": _now().isoformat(),
        "offer_expires_at": None,
        "offer_history": list(history or []) + [{"offer_id": None, "doctor_ids": [doctor.doctor_id],
                                                  "sent_at": _now().isoformat(), "outcome": "ASSIGNED_BY_ADMIN",
                                                  "doctor_id": doctor.doctor_id, "by": assigned_by}],
    })
    if updated is None:
        await _release(case_id, [doctor.doctor_id])
        return None
    await asyncio.to_thread(dynamo_service.increment_doctor_cases_handled, doctor.doctor_id)
    await _publish(updated)
    await _note(case_id, f"👤 {assigned_by} assigned the case to {doctor.name}.")
    await _tell_patient_assigned(
        updated, f"✅ *{doctor.name}* is now handling your case. Anything you send here goes straight to the doctor.")
    await _tell_doctor_assigned(updated, doctor, "Case assigned")
    # Free whoever held or was offered the case before.
    others = [d for d in (case.offered_doctor_ids if case.doctor_offer_status == "PENDING" else [])
              if d != doctor.doctor_id]
    released = await _release(case_id, others)
    if previous_doctor and previous_doctor != doctor.doctor_id:
        released += await _release(case_id, [previous_doctor])
        before = dynamo_service.get_doctor(previous_doctor)
        if before:
            await _send("send_text_message", before.phone_number,
                        f"Case {case_id} ({case.patient_name}) was reassigned to another doctor. You are free "
                        "for new cases.")
    if released:
        await offer_waiting_cases(exclude_case_id=case_id)
    return updated


async def hand_over(case_id: str, doctor: DoctorProfile) -> Optional[EmergencyCase]:
    """The assigned doctor gives the case back (DOC-4 `@resq hand over`): they are freed, the patient
    is told another doctor is being found, and a new offer round starts without them. None if the
    case is not (or no longer) theirs and open."""
    case = dynamo_service.get_case(case_id)
    if case is None or case.status not in OPEN_CASE_STATUSES or case.assigned_doctor_id != doctor.doctor_id:
        return None
    history = list(case.offer_history or []) + [{"offer_id": None, "doctor_ids": [doctor.doctor_id],
                                                  "sent_at": _now().isoformat(), "outcome": "HANDED_OVER",
                                                  "doctor_id": doctor.doctor_id}]
    updated = dynamo_service.update_case_if(case_id, {
        "assigned_doctor_id": None,
        "assigned_doctor_name": None,
        "doctor_offer_status": None,
        "offered_doctor_ids": [],
        "offer_history": history,
        "video_doctor_token": None,
        "video_doctor_token_for": None,
    }, expect={"assigned_doctor_id": doctor.doctor_id}, append={"declined_doctor_ids": [doctor.doctor_id]})
    if updated is None:
        return None
    await _release(case_id, [doctor.doctor_id], closed=True)
    print(f"[Offers] {case_id} handed over by {doctor.doctor_id}.")
    await _note(case_id, f"↩️ {doctor.name} handed the case over; it is offered to other doctors.")
    text = ("ℹ️ Your doctor had to hand your case over. We are finding another ResQ-AI doctor for you now; "
            "keep sending messages here. "
            + (f"If it gets worse, call {LOCAL_EMERGENCY_NUMBERS}." if (case.esi_level or 5) <= 2 else ""))
    await _send("send_text_message", case.user_phone, text.strip())
    await _note(case_id, text.strip(), sender_name="ResQ-AI")
    updated = await start_offer(updated)
    await _publish(updated)
    await offer_waiting_cases(exclude_case_id=case_id)
    return updated


# ---------------------------------------------------------------------------- WhatsApp buttons
TAKEN_TEXT = "This case has already been taken or reassigned. Thank you."


def busy_text(doctor: DoctorProfile) -> str:
    held = dynamo_service.get_case(doctor.busy_case_id) if doctor.busy_case_id else None
    who = f" with {held.patient_name}" if held else ""
    return (f"You are already handling a case{who}. Close it (@resq close) or hand it over (@resq hand over) "
            "before you take another one.")


async def handle_button(msg: InboundWhatsAppMessage, doctor: DoctorProfile, parts: List[str]) -> Dict[str, Any]:
    """`OFFER:ACCEPT|DECLINE:<case_id>:<offer_id>` from a doctor's WhatsApp (pipeline.DOCTOR_ACTIONS)."""
    if len(parts) != 3 or parts[0] not in ("ACCEPT", "DECLINE"):
        await _send("send_text_message", doctor.phone_number, "This button is no longer active. Please open the ResQ-AI portal.")
        return {"action": "offer_unknown_button", "case_id": None}
    action, case_id, offer_id = parts
    if action == "ACCEPT":
        outcome, case = await accept_offer(case_id, offer_id, doctor)
        if outcome in (TAKEN, NOT_FOUND):
            await _send("send_text_message", doctor.phone_number, TAKEN_TEXT)
        elif outcome == CLOSED:
            await _send("send_text_message", doctor.phone_number, f"Case {case_id} is already closed. Thank you.")
        elif outcome == ALREADY_YOURS:
            await _send("send_text_message", doctor.phone_number,
                        f"Case {case_id} is already yours. Reply here to talk to the patient, or open it in the "
                        f"portal: {portal_case_url(case_id)}")
        elif outcome == BUSY:
            await _send("send_text_message", doctor.phone_number,
                        busy_text(dynamo_service.get_doctor(doctor.doctor_id) or doctor))
        return {"action": f"offer_{outcome.lower()}", "case_id": case_id}
    outcome, _ = await decline_offer(case_id, offer_id, doctor)
    await _send("send_text_message", doctor.phone_number,
                "Thanks, the case goes to another doctor." if outcome == DECLINED else TAKEN_TEXT)
    return {"action": f"offer_{outcome.lower()}", "case_id": case_id}
