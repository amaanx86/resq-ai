"""Doctors on WhatsApp (DOC-0..3, DOC-6): a doctor handles their case from their own WhatsApp.

A doctor holds one case at a time (OFFER-4), so everything a doctor sends to the ResQ number belongs
to that case:
- A message starting with "@resq" is a command (`app.doctor_commands`), never relayed.
- Anything else, text, a photo or a voice note, goes to the patient as it is, from the ResQ number
  ("👨‍⚕️ *Dr X*: ..."), through the same code as the portal (`app.case_messages`), so the portal
  chat shows it live and patient memory gets it. No model sees or changes it.
- The patient's messages on that case are forwarded to the doctor ("*Ali*: ..."): text, photos (with
  the AI summary), voice notes (with the transcript) and the shared location as a map pin.
- On accept (or admin assignment) the doctor gets a case card: the case, how to reply, the @resq
  commands and the portal link (optional), then what the patient said so far.

Neither side sees the other's personal number. The portal stays as it is and is optional.

Routing (`handle_message`, called by `app.pipeline._route` for every message from a registered
doctor's number): the doctor's case is re-checked on every message (open, still assigned to them).
No case but an offer pending: "accept first" with the buttons. No case at all: if their last case
closed less than DOCTOR_CASE_CLOSED_GRACE_HOURS ago, "case closed, not delivered"; otherwise the
message is handled as a patient's, so a doctor can still report their own emergency ("@resq
emergency ..." does that at any time).

24-hour window (DOC-0, DOC-6): WhatsApp delivers free-form messages to the doctor only within 24 hours
of their last message to the ResQ number (`last_inbound_at`; tapping Accept counts). Outside it, a
patient message reaches the doctor as the template WHATSAPP_DOCTOR_WAITING_TEMPLATE_NAME with a
**Show messages** button (`DOC:SHOW:<case>`); the tap (or any message from the doctor) brings the
messages they missed.
"""
import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from app import case_messages
from app.config import settings
from app.models.schemas import ChatMessage, DoctorProfile, EmergencyCase, InboundWhatsAppMessage, InternalTask
from app.services import events_service
from app.services.audio_service import audio_service
from app.services.dynamo_service import OPEN_CASE_STATUSES, dynamo_service
from app.services.whatsapp_service import whatsapp_service
from app.tasks import task_handler

IDLE_TASK = "doctor_idle_reminder"
DOCTOR_PREFIX = "DOC"
# WhatsApp allows free-form messages for 24 hours after the doctor's last message; keep a margin.
WINDOW_HOURS = 23
CATCH_UP_MESSAGES = 5
MISSED_MESSAGES_MAX = 10
COMMAND_HINT = "Start a message with *@resq* for actions, e.g. _@resq video_, _@resq ambulance called_, _@resq close_."
UNSUPPORTED_TEXT = "Only text, photos and voice notes reach the patient. Please send one of those."


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stable_id(prefix: str, *parts: str) -> str:
    """Same scheme as app.pipeline._stable_id: an SQS retry overwrites instead of duplicating."""
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:10].upper()
    return f"{prefix}-{digest}"


def _parse(iso: Optional[str]) -> Optional[datetime]:
    if not iso:
        return None
    try:
        when = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _normalize_ts(ts: Optional[str]) -> str:
    when = _parse(ts)
    return (when or _now()).astimezone(timezone.utc).isoformat()


def portal_case_url(case_id: str) -> str:
    return f"{settings.PORTAL_BASE_URL.rstrip('/')}/?case={case_id}"


# ---------------------------------------------------------------------------- state
def window_open(doctor: DoctorProfile) -> bool:
    """True if WhatsApp still delivers free-form messages to the doctor (DOC-0)."""
    last = _parse(doctor.last_inbound_at)
    return bool(last and _now() - last < timedelta(hours=WINDOW_HOURS))


def touch(doctor: DoctorProfile, at: Optional[str] = None) -> DoctorProfile:
    """Records a message from the doctor's WhatsApp (it opens their 24-hour window)."""
    try:
        return dynamo_service.update_doctor_fields(doctor.doctor_id, {"last_inbound_at": at or _now().isoformat()}) or doctor
    except Exception as e:
        print(f"[DoctorChat] Could not record the last message of {doctor.doctor_id}: {e}")
        return doctor


def doctor_case(doctor: DoctorProfile) -> Optional[EmergencyCase]:
    """The open case the doctor holds, re-checked (open, still assigned to them). Falls back to a
    look at the open cases when the lock is missing (cases accepted before OFFER-4)."""
    if doctor.busy_case_id and doctor.busy_kind == "CASE":
        case = dynamo_service.get_case(doctor.busy_case_id)
        if case and case.status in OPEN_CASE_STATUSES and case.assigned_doctor_id == doctor.doctor_id:
            return case
    mine = [c for c in dynamo_service.list_active_cases() if c.assigned_doctor_id == doctor.doctor_id]
    return mine[0] if mine else None


def offered_case(doctor: DoctorProfile) -> Optional[EmergencyCase]:
    """The case currently offered to the doctor (pending round, not declined), if any."""
    candidates = []
    if doctor.busy_case_id and doctor.busy_kind == "OFFER":
        case = dynamo_service.get_case(doctor.busy_case_id)
        candidates = [case] if case else []
    if not candidates:
        candidates = dynamo_service.list_active_cases()
    for case in candidates:
        if (case.status in OPEN_CASE_STATUSES and case.doctor_offer_status == "PENDING"
                and doctor.doctor_id in (case.offered_doctor_ids or [])
                and doctor.doctor_id not in (case.declined_doctor_ids or [])):
            return case
    return None


def recently_closed(doctor: DoctorProfile) -> Optional[EmergencyCase]:
    """The doctor's last case if it closed less than DOCTOR_CASE_CLOSED_GRACE_HOURS ago."""
    closed = _parse(doctor.last_case_closed_at)
    if not (doctor.last_case_id and closed):
        return None
    if _now() - closed > timedelta(hours=settings.DOCTOR_CASE_CLOSED_GRACE_HOURS):
        return None
    return dynamo_service.get_case(doctor.last_case_id)


async def _send(method: str, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    try:
        return await getattr(whatsapp_service, method)(*args, **kwargs) or {}
    except Exception as e:
        print(f"[DoctorChat] WhatsApp {method} failed: {e}")
        return {"status": "ERROR", "detail": str(e)}


async def _note(case_id: str, text: str) -> None:
    try:
        msg = dynamo_service.save_chat_message(ChatMessage(
            message_id=f"MSG-{uuid.uuid4().hex[:8]}", case_id=case_id, sender="BOT", sender_name="System",
            content=text))
        await events_service.publish_chat_message(msg)
    except Exception as e:
        print(f"[DoctorChat] Chat note for {case_id} failed: {e}")


async def tell_doctor(doctor: DoctorProfile, text: str) -> Dict[str, Any]:
    return await _send("send_text_message", doctor.phone_number, text)


# ---------------------------------------------------------------------------- routing (DOC-1)
async def handle_message(msg: InboundWhatsAppMessage, doctor: DoctorProfile
                         ) -> Tuple[Optional[Dict[str, Any]], Optional[InboundWhatsAppMessage]]:
    """A message from a registered doctor's WhatsApp that is not one of our buttons. Returns
    (result, None) when handled here, or (None, message) when it must go the patient way (the
    doctor has no case; the message may be rewritten, e.g. "@resq emergency <text>")."""
    from app import doctor_commands  # lazy: doctor_commands imports this module

    text = (msg.text or "") if msg.type == "text" else ""
    command_text = doctor_commands.command_text(text)
    if command_text is not None:
        emergency = doctor_commands.emergency_text(command_text)
        if emergency is not None:
            # The doctor reports their own emergency: the patient way, without the prefix.
            return None, msg.model_copy(update={"text": emergency or text})
        return await doctor_commands.handle(msg, doctor, command_text), None

    case = doctor_case(doctor)
    if case is not None:
        if not settings.DOCTOR_WHATSAPP_RELAY_ENABLED:
            await tell_doctor(doctor, "Please reply to your patient in the ResQ-AI portal: "
                                      f"{portal_case_url(case.case_id)}")
            return {"action": "doctor_relay_disabled", "case_id": case.case_id}, None
        return await relay_to_patient(msg, doctor, case), None

    offer = offered_case(doctor)
    if offer is not None:
        from app import doctor_offers
        await _send("send_reply_buttons", doctor.phone_number,
                    f"Please accept the case first ({offer.patient_name}, ESI {offer.esi_level}). Your message was "
                    "not sent to the patient.",
                    [{"id": doctor_offers.payload("ACCEPT", offer.case_id, offer.offer_id), "title": "Accept"},
                     {"id": doctor_offers.payload("DECLINE", offer.case_id, offer.offer_id), "title": "Decline"}])
        return {"action": "doctor_accept_first", "case_id": offer.case_id}, None

    closed = recently_closed(doctor)
    if closed is not None:
        await tell_doctor(doctor, f"Your case with {closed.patient_name} is closed, so your message was not "
                                  "delivered. If you need emergency help yourself, send *@resq emergency* followed "
                                  "by what is happening.")
        return {"action": "doctor_case_closed", "case_id": closed.case_id}, None
    return None, msg


# ---------------------------------------------------------------------------- doctor -> patient (DOC-2/3)
async def relay_to_patient(msg: InboundWhatsAppMessage, doctor: DoctorProfile, case: EmergencyCase) -> Dict[str, Any]:
    """The doctor's own words (or photo / voice note) to the patient, unchanged."""
    await flush_missed(case, doctor)
    ids = dict(message_id=_stable_id("MSG", msg.provider, msg.provider_message_id),
               timestamp=_normalize_ts(msg.timestamp), provider=msg.provider,
               provider_message_id=msg.provider_message_id)
    if msg.type == "text" and (msg.text or "").strip():
        await case_messages.send_doctor_text(case, doctor.name, msg.text.strip(), **ids)
        print(f"[DoctorChat] {doctor.doctor_id} -> patient of {case.case_id}: text.")
        return {"action": "doctor_relay", "case_id": case.case_id}
    if msg.type in ("image", "audio") and msg.media_id:
        try:
            media = await whatsapp_service.fetch_media_to_s3(msg.media_id, case.user_phone, kind=msg.type,
                                                             mime_type=msg.media_mime_type)
            stored = audio_service.get_audio_object(media.key)
            if stored is None:
                raise case_messages.MediaSendError(502, "the file could not be read back from the media vault")
            body = stored[0]
            data = bytes(body) if isinstance(body, (bytes, bytearray)) else body.read()
            content_type = msg.media_mime_type or stored[1] or ("image/jpeg" if msg.type == "image" else "audio/ogg")
            await case_messages.send_doctor_media(case, doctor.name, data, content_type, msg.caption or "", **ids)
        except case_messages.MediaSendError as e:
            await tell_doctor(doctor, f"⚠️ Your {'photo' if msg.type == 'image' else 'voice note'} was not sent to "
                                      f"{case.patient_name}: {e.detail}")
            return {"action": "doctor_relay_failed", "case_id": case.case_id}
        except Exception as e:
            print(f"[DoctorChat] Media from {doctor.doctor_id} for {case.case_id} failed: {e}")
            await tell_doctor(doctor, f"⚠️ Your {'photo' if msg.type == 'image' else 'voice note'} could not be "
                                      f"sent to {case.patient_name}. Please try again.")
            return {"action": "doctor_relay_failed", "case_id": case.case_id}
        print(f"[DoctorChat] {doctor.doctor_id} -> patient of {case.case_id}: {msg.type}.")
        return {"action": "doctor_relay", "case_id": case.case_id}
    await tell_doctor(doctor, UNSUPPORTED_TEXT)
    return {"action": "doctor_relay_unsupported", "case_id": case.case_id}


# ---------------------------------------------------------------------------- patient -> doctor (DOC-2/3)
def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def forward_patient_message(case: EmergencyCase, chat: ChatMessage,
                                  location: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Sends a patient message saved on the case to the assigned doctor's WhatsApp. Returns SENT,
    TEMPLATE (outside the 24-hour window), FAILED, or None (no doctor / relay off). Never raises."""
    try:
        if not settings.DOCTOR_WHATSAPP_RELAY_ENABLED:
            return None
        current = dynamo_service.get_case(case.case_id) or case
        if not current.assigned_doctor_id or current.status not in OPEN_CASE_STATUSES:
            return None
        doctor = dynamo_service.get_doctor(current.assigned_doctor_id)
        if doctor is None:
            return None
        if not window_open(doctor):
            return await _waiting_template(current, doctor, chat)
        if current.doctor_missed_since and location is None:
            # The window is open again (e.g. the doctor tapped a button): the messages they missed,
            # this one included (it is saved already), in order.
            await flush_missed(current, doctor)
            return "SENT"
        return await _forward(doctor, current, chat, location)
    except Exception as e:
        print(f"[DoctorChat] Forwarding {chat.message_id} of {case.case_id} failed: {e}")
        return "FAILED"


async def _forward(doctor: DoctorProfile, case: EmergencyCase, chat: ChatMessage,
                   location: Optional[Dict[str, Any]] = None) -> str:
    who = f"*{case.patient_name}*"
    results: List[Dict[str, Any]] = []
    if chat.image_s3_key and (stored := audio_service.get_audio_object(chat.image_s3_key)):
        body = stored[0]
        data = bytes(body) if isinstance(body, (bytes, bytearray)) else body.read()
        results.append(await _send("send_media_message", doctor.phone_number, "image", chat.image_s3_key, data,
                                   stored[1] or "image/jpeg", caption=_clip(f"{who}: {chat.content}", 1000)))
    elif chat.audio_s3_key and (stored := audio_service.get_audio_object(chat.audio_s3_key)):
        body = stored[0]
        data = bytes(body) if isinstance(body, (bytes, bytearray)) else body.read()
        results.append(await _send("send_media_message", doctor.phone_number, "audio", chat.audio_s3_key, data,
                                   stored[1] or "audio/ogg"))
        results.append(await _send("send_text_message", doctor.phone_number, f"{who}: {chat.content}"))
    elif location:
        results.append(await _send("send_location", doctor.phone_number, location["latitude"],
                                   location["longitude"], name=f"{case.patient_name}'s location",
                                   address=location.get("address")))
        results.append(await _send("send_text_message", doctor.phone_number, f"{who}: {chat.content}"))
    else:
        results.append(await _send("send_text_message", doctor.phone_number, f"{who}: {chat.content}"))
    failed = [r for r in results if r.get("status") == "ERROR"]
    if failed:
        await _note(case.case_id, f"⚠️ Could not forward the patient's message to {doctor.name}'s WhatsApp "
                                  f"({failed[0].get('detail') or 'error'}).")
        return "FAILED"
    return "SENT"


async def _waiting_template(case: EmergencyCase, doctor: DoctorProfile, chat: ChatMessage) -> str:
    """DOC-6: the doctor's window is closed; tell them with the template (at most once per
    DOCTOR_WAITING_TEMPLATE_INTERVAL_SECONDS) and keep the messages for the Show messages tap."""
    changes: Dict[str, Any] = {}
    if not case.doctor_missed_since:
        changes["doctor_missed_since"] = chat.timestamp
    last = _parse(case.doctor_waiting_template_at)
    if last and (_now() - last).total_seconds() < settings.DOCTOR_WAITING_TEMPLATE_INTERVAL_SECONDS:
        if changes:
            dynamo_service.update_case(case.case_id, changes)
        return "TEMPLATE"
    result = await _send("send_doctor_patient_waiting", doctor.phone_number, case.patient_name,
                         f"{DOCTOR_PREFIX}:SHOW:{case.case_id}")
    changes["doctor_waiting_template_at"] = _now().isoformat()
    dynamo_service.update_case(case.case_id, changes)
    if result.get("status") == "ERROR":
        await _note(case.case_id, f"⚠️ {doctor.name} has not messaged the ResQ-AI number in 24 hours, so WhatsApp "
                                  f"cannot deliver the patient's messages, and the reminder template failed "
                                  f"({result.get('detail') or 'error'}). They can read them in the portal.")
        return "FAILED"
    await _note(case.case_id, f"✉️ {doctor.name}'s WhatsApp window is closed: they were sent a reminder to open the "
                              "patient's messages.")
    return "TEMPLATE"


async def flush_missed(case: EmergencyCase, doctor: DoctorProfile) -> int:
    """Sends the doctor the patient messages they missed while their window was closed (DOC-6)."""
    current = dynamo_service.get_case(case.case_id) or case
    since = current.doctor_missed_since
    if not since:
        return 0
    dynamo_service.update_case(case.case_id, {"doctor_missed_since": None})
    missed = sorted((m for m in dynamo_service.get_case_messages(case.case_id)
                     if m.sender == "PATIENT" and (m.timestamp or "") >= since),
                    key=lambda m: m.timestamp or "")[-MISSED_MESSAGES_MAX:]
    if missed:
        await tell_doctor(doctor, f"Messages from {current.patient_name} you missed:")
        for chat in missed:
            await _forward(doctor, current, chat)
    return len(missed)


async def notify_doctor(case: EmergencyCase, text: str) -> None:
    """A short notice to the case's doctor (e.g. the case escalated). Best effort."""
    if not (settings.DOCTOR_WHATSAPP_RELAY_ENABLED and case.assigned_doctor_id):
        return
    doctor = dynamo_service.get_doctor(case.assigned_doctor_id)
    if doctor and window_open(doctor):
        await tell_doctor(doctor, text)


# ---------------------------------------------------------------------------- case card (DOC-2)
def case_card_text(case: EmergencyCase, header: str, link: str, call_link: Optional[str] = None) -> str:
    """The case for the doctor's WhatsApp: who, how urgent, what we know, how to reply."""
    lines = [f"*{header}: {case.patient_name}*, ESI {case.esi_level}, {case.matched_specialty or 'General Emergency'}"]
    if case.clinical_summary:
        lines.append(_clip(case.clinical_summary, 300))
    analysis = case.image_analysis or {}
    if analysis.get("ok") and analysis.get("clinical_summary"):
        lines.append(f"📷 Photo: {_clip(analysis['clinical_summary'], 160)}")
    if case.patient_history:
        lines.append("🗂 History: " + _clip("; ".join(case.patient_history[:3]), 200))
    if case.location_latitude is not None and case.location_longitude is not None:
        lines.append(f"📍 Location: https://maps.google.com/?q={case.location_latitude:.6f},{case.location_longitude:.6f}")
    elif (case.esi_level or 5) <= 2:
        lines.append("📍 Location: asked for, not shared yet.")
    if case.ambulance_called_at:
        lines.append(f"🚑 Ambulance called by {case.ambulance_called_by or 'the doctor'}.")
    lines += ["", f"💬 *Reply here to talk to {case.patient_name}*: your messages, photos and voice notes go to "
                  "them from the ResQ-AI number.", COMMAND_HINT]
    if call_link:
        lines.append(f"📞 To call them from your own WhatsApp (they will see your number): {call_link}")
    lines.append(f"Portal (optional): {link}")
    return "\n".join(lines)


async def send_case_card(case: EmergencyCase, doctor: DoctorProfile, header: str = "Case accepted",
                         catch_up: bool = True) -> None:
    """The case card on the doctor's WhatsApp, then (`catch_up`) what the patient said so far."""
    from app import direct_calls
    link = portal_case_url(case.case_id)
    call_link = await direct_calls.create_link_or_none(case, doctor)
    await _send("send_case_link", doctor.phone_number, _clip(case_card_text(case, header, link, call_link), 1024),
                "Open case", link, header=header)
    if not catch_up:
        return
    said = sorted((m for m in dynamo_service.get_case_messages(case.case_id) if m.sender == "PATIENT"),
                  key=lambda m: m.timestamp or "")[-CATCH_UP_MESSAGES:]
    if said:
        body = "\n".join(f"• {_clip(m.content, 300)}" for m in said)
        await tell_doctor(doctor, _clip(f"What {case.patient_name} said so far:\n{body}", 4000))


async def case_closed(case: EmergencyCase, closed_by: Optional[str] = None) -> None:
    """Tells the case's doctor the case is closed and they are free for new cases (DOC-2)."""
    if not case.assigned_doctor_id:
        return
    doctor = dynamo_service.get_doctor(case.assigned_doctor_id)
    if doctor and window_open(doctor):
        by = f" by {closed_by}" if closed_by and closed_by != doctor.name else ""
        await tell_doctor(doctor, f"✅ The case with {case.patient_name} was closed{by}. You are free for new cases.")


# ---------------------------------------------------------------------------- idle reminder
async def schedule_idle_reminder(case: EmergencyCase, doctor: DoctorProfile,
                                 delay_seconds: Optional[float] = None) -> None:
    """After DOCTOR_IDLE_REMINDER_SECONDS without messages, ask the doctor whether they are still on
    the case (a forgotten open case keeps them from new offers). Best effort."""
    if settings.DOCTOR_IDLE_REMINDER_SECONDS <= 0:
        return
    from app.services.queue_service import queue_service
    try:
        await queue_service.publish_internal(
            IDLE_TASK, {"case_id": case.case_id, "doctor_id": doctor.doctor_id},
            delay_seconds=settings.DOCTOR_IDLE_REMINDER_SECONDS if delay_seconds is None else delay_seconds)
    except Exception as e:
        print(f"[DoctorChat] Could not schedule the idle reminder for {case.case_id}: {e}")


@task_handler(IDLE_TASK)
async def handle_idle_reminder(task: InternalTask) -> Dict[str, Any]:
    """Reminds the doctor of a quiet open case, then checks again later; stops once the case is
    closed or no longer theirs."""
    case_id, doctor_id = task.payload.get("case_id"), task.payload.get("doctor_id")
    case = dynamo_service.get_case(case_id) if case_id else None
    doctor = dynamo_service.get_doctor(doctor_id) if doctor_id else None
    if (case is None or doctor is None or case.status not in OPEN_CASE_STATUSES
            or case.assigned_doctor_id != doctor_id):
        return {"case_id": case_id, "outcome": "stale"}
    stamps = [m.timestamp for m in dynamo_service.get_case_messages(case_id) if m.timestamp]
    last = max((_parse(s) for s in stamps if _parse(s)), default=None) or _parse(case.accepted_at) or _now()
    quiet = (_now() - last).total_seconds()
    threshold = settings.DOCTOR_IDLE_REMINDER_SECONDS
    if quiet < threshold:
        await schedule_idle_reminder(case, doctor, delay_seconds=threshold - quiet)
        return {"case_id": case_id, "outcome": "not_idle"}
    await tell_doctor(doctor, f"Are you still treating {case.patient_name}? When you are done, send *@resq close*. "
                              "If you cannot continue, send *@resq hand over* and another doctor takes the case. "
                              "You get no new cases while this one is open.")
    await schedule_idle_reminder(case, doctor)
    return {"case_id": case_id, "outcome": "reminded"}
