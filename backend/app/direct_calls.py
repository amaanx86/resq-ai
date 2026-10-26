"""Direct WhatsApp calls (the CALL-3 fallback).

While a patient's call cannot ring in the portal (WhatsApp calling is off for the business number
and CALL-1 is not built: `portal_calls_available()`), the assigned doctor calls the patient from
their own WhatsApp instead:

- The patient's "doctor accepted" message carries a **Request a call** reply button (payload
  `CALLME:<case_id>`, routed by the pipeline to `handle_patient_button`). A WhatsApp call to the
  business number (`connect` event, `app.tasks.handle_call_event`) does the same, if one arrives.
- `request_call` records `call_requested_at`, notes it in the case chat and sends the doctor an
  **Open chat & call** link.
- The link is ours, not a bare `wa.me` link, so the patient's number never appears in a message:
  `GET /api/v1/direct-call/<token>` (`app.routers.direct_call`; no login, the 32-byte token is the
  secret, DIRECT_CALL_LINK_TTL_SECONDS, item `DIRECTCALL#<token>` / `LINK`). The doctor's "case
  accepted" WhatsApp carries one too, and the portal's **Call on WhatsApp** button calls
  `start_direct_call` directly.
- `start_direct_call` records `direct_call_at/_by` (DIRECT_CALL_CHOSEN in the case chat), tells the
  patient the doctor is calling from their own number, and returns the `wa.me` URL. WhatsApp opens
  the chat; the doctor taps the call icon. The call itself is outside ResQ.

Privacy: the patient sees the doctor's personal number; the doctor's message and the portal button
say so before they open the chat.
"""
import asyncio
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

from app.config import settings
from app.doctor_offers import LOCAL_EMERGENCY_NUMBERS, _note, _publish, _send
from app.models.schemas import DoctorProfile, EmergencyCase, InboundWhatsAppMessage
from app.services.dynamo_service import OPEN_CASE_STATUSES, dynamo_service, phone_key

PATIENT_CALL_PREFIX = "CALLME"
REQUEST_CALL_TITLE = "Request a call"
# A second request or direct call within this many seconds sends no new WhatsApp messages
# (a double tap, or the doctor opening the chat again).
REPEAT_WINDOW_SECONDS = 120

# Link use results.
OK, UNKNOWN, EXPIRED, NOT_ASSIGNED, CLOSED = "OK", "UNKNOWN", "EXPIRED", "NOT_ASSIGNED", "CLOSED"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def portal_calls_available() -> bool:
    """True once a patient's WhatsApp call rings in the doctor's portal (CALL-1). Not built yet, and
    WhatsApp calling is off for the business number, so every call goes the direct-call way."""
    return False


def wa_me_url(phone: str) -> str:
    return f"https://wa.me/{''.join(ch for ch in str(phone or '') if ch.isdigit())}"


def link_url(token: str) -> str:
    return f"{settings.PORTAL_BASE_URL.rstrip('/')}/api/v1/direct-call/{token}"


def _recent(iso: Optional[str]) -> bool:
    if not iso:
        return False
    try:
        return (_now() - datetime.fromisoformat(iso)).total_seconds() < REPEAT_WINDOW_SECONDS
    except ValueError:
        return False


# ---------------------------------------------------------------------------- links
async def create_link(case: EmergencyCase, doctor: DoctorProfile) -> str:
    """A new direct-call link for the case's doctor; returns its URL."""
    token = secrets.token_urlsafe(32)
    expires = _now() + timedelta(seconds=settings.DIRECT_CALL_LINK_TTL_SECONDS)
    await asyncio.to_thread(dynamo_service.save_direct_call_link, token,
                            {"case_id": case.case_id, "doctor_id": doctor.doctor_id,
                             "created_at": _now().isoformat()}, expires)
    return link_url(token)


async def create_link_or_none(case: EmergencyCase, doctor: DoctorProfile) -> Optional[str]:
    """create_link for messages that also work without the link."""
    try:
        return await create_link(case, doctor)
    except Exception as e:
        print(f"[DirectCall] Could not create a direct-call link for {case.case_id}: {e}")
        return None


async def use_link(token: str) -> Tuple[str, Optional[str]]:
    """The doctor opened a direct-call link. Returns (OK, wa.me URL) or (UNKNOWN | EXPIRED |
    NOT_ASSIGNED | CLOSED, None). A link only works for the doctor still assigned to an open case."""
    link = await asyncio.to_thread(dynamo_service.get_direct_call_link, token)
    if not link:
        return UNKNOWN, None
    if datetime.fromisoformat(link["expires_at"]) < _now():
        return EXPIRED, None
    case = dynamo_service.get_case(link["case_id"])
    if case is None or case.status not in OPEN_CASE_STATUSES:
        return CLOSED, None
    if case.assigned_doctor_id != link["doctor_id"]:
        return NOT_ASSIGNED, None
    doctor = dynamo_service.get_doctor(link["doctor_id"])
    if doctor is None:
        return NOT_ASSIGNED, None
    return OK, await start_direct_call(case, doctor, via="WhatsApp link")


# ---------------------------------------------------------------------------- the call
async def start_direct_call(case: EmergencyCase, doctor: DoctorProfile, via: str) -> str:
    """The doctor is about to call the patient from their own WhatsApp: record it, tell the patient
    (once per REPEAT_WINDOW_SECONDS) and return the wa.me URL that opens the patient's chat."""
    repeat = case.direct_call_by == doctor.name and _recent(case.direct_call_at)
    updated = dynamo_service.update_case(case.case_id, {"direct_call_at": _now().isoformat(),
                                                        "direct_call_by": doctor.name}) or case
    if not repeat:
        print(f"[DirectCall] {case.case_id}: {doctor.doctor_id} is calling the patient from their own WhatsApp ({via}).")
        await _note(case.case_id, f"📞 {doctor.name} opened the patient's WhatsApp chat to call them from "
                                  f"their own number ({via}). The call itself is not recorded by ResQ-AI.")
        text = (f"📞 *{doctor.name}*, your ResQ-AI doctor, is calling you now on WhatsApp from their own "
                "number. Please answer.")
        await _send("send_text_message", case.user_phone, text)
        await _note(case.case_id, text, sender_name="ResQ-AI")
        await _publish(updated)
    return wa_me_url(case.user_phone)


async def request_call(case: EmergencyCase, source: str) -> Dict[str, Any]:
    """The patient wants to talk to their doctor (`source`: "button" or "call"). The doctor gets an
    Open chat & call link; the patient is told what happens next."""
    if case.status not in OPEN_CASE_STATUSES:
        await _send("send_text_message", case.user_phone,
                    "This case is closed. If you need help again, send a voice note at any time.")
        return {"action": "call_request_closed", "case_id": case.case_id}
    doctor = dynamo_service.get_doctor(case.assigned_doctor_id) if case.assigned_doctor_id else None
    if doctor is None:
        await _send("send_text_message", case.user_phone,
                    "A doctor has not taken your case yet. You will get a message as soon as one does. "
                    f"If it gets worse, call {LOCAL_EMERGENCY_NUMBERS} now.")
        return {"action": "call_request_no_doctor", "case_id": case.case_id}
    if _recent(case.call_requested_at):
        await _send("send_text_message", case.user_phone,
                    f"We have already asked *{doctor.name}* to call you. Please keep your phone close.")
        return {"action": "call_request_repeat", "case_id": case.case_id}

    updated = dynamo_service.update_case(case.case_id, {"call_requested_at": _now().isoformat()}) or case
    what = "tried to call the ResQ-AI number" if source == "call" else "asked for a call"
    await _note(case.case_id, f"📞 The patient {what}. Calls cannot ring in the portal yet, so {doctor.name} "
                              "was sent a link to call them from their own WhatsApp.")
    await _publish(updated)
    link = await create_link_or_none(updated, doctor)
    if link:
        sent = await _send(
            "send_case_link", doctor.phone_number,
            f"📞 {updated.patient_name} (case {updated.case_id}, ESI {updated.esi_level}) asks you to call them.\n\n"
            "Tap *Open chat & call* to open their WhatsApp chat, then tap the call icon. You call from your own "
            f"number, so the patient will see it.\n\n{link}",
            "Open chat & call", link, header="Patient asks for a call")
        if sent.get("status") == "ERROR":
            await _note(case.case_id, f"⚠️ The call request could not reach {doctor.name} on WhatsApp "
                                      f"({sent.get('detail') or 'error'}); use Call on WhatsApp in the portal.")
    text = (f"📞 We have asked *{doctor.name}* to call you. They will call you on WhatsApp from their own "
            f"number in a moment. If it gets worse before then, call {LOCAL_EMERGENCY_NUMBERS}.")
    await _send("send_text_message", case.user_phone, text)
    await _note(case.case_id, text, sender_name="ResQ-AI")
    return {"action": "call_requested", "case_id": case.case_id}


# ---------------------------------------------------------------------------- patient side
async def send_doctor_assigned(case: EmergencyCase, text: str) -> Dict[str, Any]:
    """The "doctor accepted / assigned" message to the patient. While calls cannot reach the portal
    it carries the Request a call button (plain text if WhatsApp rejects the interactive message)."""
    if portal_calls_available():
        return await _send("send_text_message", case.user_phone, text)
    result = await _send("send_reply_buttons", case.user_phone, text,
                         [{"id": f"{PATIENT_CALL_PREFIX}:{case.case_id}", "title": REQUEST_CALL_TITLE}])
    if result.get("status") == "ERROR":
        return await _send("send_text_message", case.user_phone, text)
    return result


async def handle_patient_button(msg: InboundWhatsAppMessage, phone: str, parts: list) -> Dict[str, Any]:
    """`CALLME:<case_id>` from the patient's WhatsApp (routed by the pipeline)."""
    case = dynamo_service.get_case(parts[0]) if parts else None
    if case is None or phone_key(case.user_phone) != phone_key(phone):
        await _send("send_text_message", phone,
                    "This button is no longer active. If you need help, send a voice note describing the emergency.")
        return {"action": "call_request_unknown", "case_id": None}
    return await request_call(case, source="button")


async def handle_incoming_call(user_phone: str) -> Dict[str, Any]:
    """A WhatsApp call to the business number that the portal cannot answer: the caller's open case
    gets a call request; without one, the caller is told how to get help."""
    case = dynamo_service.get_open_case_for_user(phone_key(user_phone))
    if case is None:
        await _send("send_text_message", user_phone,
                    "ResQ-AI cannot take calls on this number. Send a voice note describing the emergency and "
                    f"a doctor will help you. In immediate danger, call {LOCAL_EMERGENCY_NUMBERS}.")
        return {"action": "call_without_case", "case_id": None}
    return await request_call(case, source="call")
