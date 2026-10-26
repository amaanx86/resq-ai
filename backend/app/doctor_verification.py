"""Doctor number confirmation (DOC-8): a doctor's WhatsApp number is confirmed by the doctor before
it gets anything about a patient.

Case offers and, after an accept, the patient's messages go to the doctor's WhatsApp number, so a
typo in the number would send them to a stranger. When an administrator registers a doctor (or
changes their number), the doctor gets the template `WHATSAPP_DOCTOR_CONFIRM_TEMPLATE_NAME`
("... was added to ResQ-AI as the WhatsApp number of Dr. X. Is this your number?") with two quick
replies, **Yes, it's me** / **No, not me** (payload `DOCV:YES|NO:<doctor_id>`); interactive reply
buttons if the template send fails (only within 24 hours of the doctor's last message).

`DoctorProfile.phone_status`:
- None: registered before this existed (and the demo doctors): treated as confirmed.
- PENDING: asked, no answer yet. No offers (`rank_candidates`), no admin assign (409), no WhatsApp
  routing: a message from the number gets the question again.
- CONFIRMED: normal.
- DECLINED: the owner said "not me". As PENDING, and the number's messages are handled as a
  patient's; the roster shows it so an administrator can fix the number.

Accepting is idempotent; a doctor confirming while on duty is offered a waiting case right away.
"""
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.config import settings
from app.models.schemas import DoctorProfile, InboundWhatsAppMessage
from app.services.dynamo_service import dynamo_service
from app.services.whatsapp_service import whatsapp_service

PREFIX = "DOCV"
PENDING, CONFIRMED, DECLINED = "PENDING", "CONFIRMED", "DECLINED"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def number_confirmed(doctor: Any) -> bool:
    """True if the doctor's number may get offers and patients' messages."""
    from app.agents.tools import number_confirmed as confirmed  # one rule, shared with the ranking
    return confirmed(doctor)


def payloads(doctor_id: str) -> List[str]:
    return [f"{PREFIX}:YES:{doctor_id}", f"{PREFIX}:NO:{doctor_id}"]


async def send_confirmation(doctor: DoctorProfile, added_by: Optional[str] = None) -> Dict[str, Any]:
    """Asks the doctor to confirm their number; records when. Never raises."""
    yes, no = payloads(doctor.doctor_id)
    result: Dict[str, Any]
    try:
        result = await whatsapp_service.send_doctor_number_confirmation(doctor.phone_number, doctor.name, yes, no)
    except Exception as e:
        result = {"status": "ERROR", "detail": str(e)}
    status = "SENT" if result.get("status") != "ERROR" else "FAILED"
    try:
        dynamo_service.update_doctor_fields(doctor.doctor_id, {"phone_confirmation_sent_at": _now(),
                                                               "phone_confirmation_status": status})
    except Exception as e:
        print(f"[DoctorVerify] Could not record the confirmation request for {doctor.doctor_id}: {e}")
    print(f"[DoctorVerify] Number confirmation for {doctor.doctor_id} ({doctor.phone_number}): {status}"
          f"{' by ' + added_by if added_by else ''}.")
    return {**result, "confirmation": status}


async def start(doctor: DoctorProfile, added_by: Optional[str] = None) -> Optional[DoctorProfile]:
    """The number is new (registration, or an administrator changed it): PENDING, then the question."""
    updated = dynamo_service.update_doctor_fields(doctor.doctor_id, {"phone_status": PENDING,
                                                                     "phone_confirmed_at": None})
    await send_confirmation(updated or doctor, added_by)
    return dynamo_service.get_doctor(doctor.doctor_id)


async def handle_button(msg: InboundWhatsAppMessage, doctor: DoctorProfile, parts: List[str]) -> Dict[str, Any]:
    """`DOCV:YES|NO:<doctor_id>` from the doctor's number (pipeline.DOCTOR_ACTIONS)."""
    answer, doctor_id = (parts + ["", ""])[:2]
    if doctor_id != doctor.doctor_id or answer not in ("YES", "NO"):
        await _reply(doctor, "This button is no longer active.")
        return {"action": "doctor_number_stale", "case_id": None}
    if answer == "YES":
        already = doctor.phone_status in (None, CONFIRMED)
        updated = dynamo_service.update_doctor_fields(doctor.doctor_id, {"phone_status": CONFIRMED,
                                                                         "phone_confirmed_at": doctor.phone_confirmed_at or _now()})
        if not already:
            print(f"[DoctorVerify] {doctor.doctor_id} confirmed {doctor.phone_number}.")
        await _reply(doctor, f"✅ Thank you, {doctor.name}. This number is confirmed: ResQ-AI case offers and your "
                             "patients' messages will come here. Send *@resq help* to see what you can do from WhatsApp.")
        if not already and (updated or doctor).is_active:
            from app import doctor_offers  # lazy: doctor_offers imports the pipeline's helpers
            await doctor_offers.doctor_came_on_duty(doctor.doctor_id)
        return {"action": "doctor_number_confirmed", "case_id": None, "doctor_id": doctor.doctor_id}
    dynamo_service.update_doctor_fields(doctor.doctor_id, {"phone_status": DECLINED, "phone_confirmed_at": None})
    print(f"[DoctorVerify] WARNING: the owner of {doctor.phone_number} says they are not {doctor.name} "
          f"({doctor.doctor_id}); no offers go to this number.")
    await _reply(doctor, "Thank you, and sorry for the trouble. This number will not get ResQ-AI case messages; "
                         "we have told the ResQ-AI administrators.")
    return {"action": "doctor_number_declined", "case_id": None, "doctor_id": doctor.doctor_id}


async def handle_unconfirmed(msg: InboundWhatsAppMessage, doctor: DoctorProfile) -> Dict[str, Any]:
    """A message from a number that is still PENDING: the question again (the window is open now)."""
    yes, no = payloads(doctor.doctor_id)
    try:
        await whatsapp_service.send_reply_buttons(
            doctor.phone_number,
            f"Before you get ResQ-AI cases, please confirm: is this the WhatsApp number of *{doctor.name}*?",
            [{"id": yes, "title": "Yes, it's me"}, {"id": no, "title": "No, not me"}])
    except Exception as e:
        print(f"[DoctorVerify] Could not ask {doctor.phone_number} again: {e}")
    return {"action": "doctor_number_unconfirmed", "case_id": None, "doctor_id": doctor.doctor_id}


async def _reply(doctor: DoctorProfile, text: str) -> None:
    try:
        await whatsapp_service.send_text_message(doctor.phone_number, text)
    except Exception as e:
        print(f"[DoctorVerify] Reply to {doctor.phone_number} failed: {e}")


def enabled() -> bool:
    return settings.DOCTOR_NUMBER_CONFIRMATION_ENABLED
