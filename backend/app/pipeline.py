"""Inbound message pipeline (INFRA-6, FLOW-1, FLOW-2, AGENT-5, SPEECH-1, DATA-1/2/4).

`process_inbound_message` handles one `InboundWhatsAppMessage`, whichever provider delivered
it. The worker calls it for each SQS message; in pure mock mode `queue_service` calls it inline.

Routing, in order:
0. A registered doctor's WhatsApp (DOC-1): one of our action buttons (payload `OFFER:...`, `CALL:...`
   or `DOC:...`) goes to that action's handler in `DOCTOR_ACTIONS`; an "@resq ..." message is a
   command; anything else goes to the patient of the one case the doctor holds (`app.doctor_chat`).
   No user profile, no case, no triage. A doctor without a case (none closed recently) is handled as
   a patient below, so they can report their own emergency. A patient tapping **Request a call**
   (`CALLME:<case>`) asks their doctor to call them (`app.direct_calls`). Any other button tap
   counts as typing the button's title.
1. Unknown sender: create a PENDING_DETAILS profile (no placeholder contact). The onboarding
   prompt is sent after any emergency reply, so an emergency is never delayed.
2. Emergency-contact registration in the patient's own words, typed or in a voice note (transcribed
   first, the transcript is reused below): for a user still PENDING_DETAILS or without an open case,
   a message that could be one is read by `app.registration` (Bedrock, keywords in mock mode). A
   valid contact is saved; if the message also reports an emergency it is then routed as usual
   (triaged, with the contact now on file), otherwise that is all. A registration without a usable
   number gets a short reply asking for it, unless it reports an emergency.
3. Sender has an open case (ACTIVE / DOCTOR_HANDOVER): the message goes to that case's chat
   for the doctor, and to the doctor's own WhatsApp once a doctor took the case (DOC-2); no new
   case (FLOW-2). A shared location is saved on the case, shown to the
   doctor and, for an ESI 1-2 case, sent to the emergency contact as a WhatsApp template. If the
   emergency protocol never ran on that case (a human-review case, or ESI >= 3) a text, voice or
   photo follow-up is also triaged under the SAME case ID, and an ESI 1-2 result escalates the case
   in place. Otherwise the message gets the plain handover.
4. Unsupported types and pure greetings get a short reply.
5. Everything else, and every voice note and photo, is triaged.

Photos (IMAGE-1): the photo is stored in the media vault (`images/`) and described by the image
analysis agent (`app.agents.image_agent`), as a voice note is transcribed. The description and the
patient's caption are triaged as text, and the doctor sees the photo with the description. No
analysis (mock / Floci, or the models failed): the caption alone is triaged, or, without a caption,
a human-review case is opened (a doctor must look at the photo).

Doctors (OFFER-1..5): no doctor is assigned by triage. Every open case that needs one (ESI 1-3, human
review) is offered to free on-duty doctors after it is saved (`app.doctor_offers.start_offer`); the
first to accept gets it and the patient is told. The first reply never waits for that. With no free
doctor the patient is told to wait (and to call the emergency numbers if it gets worse), the case is
offered again as soon as a doctor is free, and administrators are alerted (`app.admin_alerts`).

No ambulance is dispatched automatically. ESI 1-2 cases ask the patient for their location (a
WhatsApp "Send location" button); the assigned doctor calls the ambulance and records it with
`mark_ambulance_called`.

Idempotency: each message is claimed under `msg:{provider}:{provider_message_id}` with a lease
longer than the SQS visibility timeout. The case ID is derived from the same key, so a retry after
a partial failure reuses the case ID, and the agent's per-case tool idempotency
(`tool:{case_id}:{tool}`) stops a second contact call. Bot replies stored in the chat have a
deterministic ID and timestamp, so a retry overwrites them instead of adding a second one.

Patient memory (MEM-1): what the patient said (triaged messages, follow-ups, handover messages) and
each triage outcome are stored as AgentCore Memory events for the patient, in the background and
after the reply; `app/agents/memory.py`. Off in mock/Floci mode.

Per-sender serialization: routing + case creation run under a `sender:{phone}` lease, so two
messages from one sender processed at the same time can't both see "no open case" and open two
cases (two contact calls). If the lease is held, the worker gets IN_PROGRESS and leaves the SQS
message for a retry; inline (mock) mode waits and retries instead, so the demo never drops one.
"""
import asyncio
import hashlib
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from app.models.schemas import (
    ChatMessage,
    DoctorProfile,
    EmergencyCase,
    InboundWhatsAppMessage,
    TriageAssessment,
    UserProfile,
)
from app.services.dynamo_service import dynamo_service, OPEN_CASE_STATUSES
from app.services.audio_service import audio_service
from app.services.whatsapp_service import whatsapp_service
from app.services import events_service
from app.services.memory_service import remember_in_background
from app.agents.triage_agent import triage_agent
from app.agents.image_agent import ImageAnalysis, image_analyzer, photo_text
from app.agents.tools import match_specialist_doctor_tool
from app import direct_calls, doctor_chat, doctor_commands, doctor_offers, doctor_verification
from app.registration import DEFAULT_CONTACT_NAME, Registration, registration_reader
from app.registration import worth_checking as could_be_registration

GREETINGS = {
    "hi", "hello", "hey", "hii", "salam", "salaam", "aoa", "assalam o alaikum", "assalamualaikum",
    "assalam alaikum", "asalam o alaikum", "as salam o alaikum", "namaste", "namaskar", "start",
    "register", "good morning", "good evening",
}
LOCAL_EMERGENCY_NUMBERS = "1122 / 115 (Pakistan) or 112 (India)"
LOCATION_REQUEST_TEXT = ("📍 *Share your location* so the doctor can send an ambulance to you. "
                         "Tap the button below.")

# `msg:` lease: longer than the SQS visibility timeout (300 s in Terraform), so a redelivery
# while the first attempt is still running sees IN_PROGRESS instead of processing it again.
MESSAGE_LEASE_SECONDS = int(os.getenv("MESSAGE_LEASE_SECONDS", "900"))
# `sender:` lease around routing + case creation. An expired lease (crashed worker) is reclaimable.
SENDER_LEASE_SECONDS = int(os.getenv("SENDER_LEASE_SECONDS", "300"))
# Inline (mock) mode: wait for the sender lease instead of returning IN_PROGRESS.
SENDER_WAIT_ATTEMPTS = int(os.getenv("SENDER_WAIT_ATTEMPTS", "60"))
SENDER_WAIT_DELAY_SECONDS = float(os.getenv("SENDER_WAIT_DELAY_SECONDS", "0.25"))
# Suggested SQS redelivery delay when the sender lease is busy (the worker applies it).
SENDER_BUSY_RETRY_SECONDS = int(os.getenv("SENDER_BUSY_RETRY_SECONDS", "5"))

# Doctor button actions (WA-0): payload prefix -> handler(msg, doctor, payload parts after the
# prefix). OFFER-1 / CALL-3 / DOC-4 add their handlers here. A prefix without a handler gets a short reply.
DoctorActionHandler = Callable[[InboundWhatsAppMessage, DoctorProfile, List[str]], Awaitable[Dict[str, Any]]]
DOCTOR_ACTION_PREFIXES = ("OFFER", "CALL", "DOC", doctor_verification.PREFIX)
DOCTOR_ACTIONS: Dict[str, DoctorActionHandler] = {"OFFER": doctor_offers.handle_button,
                                                  "DOC": doctor_commands.handle_button,
                                                  doctor_verification.PREFIX: doctor_verification.handle_button}


# ====================================================================== helpers
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def idempotency_key(provider: str, provider_message_id: str) -> str:
    return f"msg:{provider}:{provider_message_id}"


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:10].upper()
    return f"{prefix}-{digest}"


def case_id_for(msg: InboundWhatsAppMessage) -> str:
    """Deterministic case ID per inbound message, so SQS retries reuse it."""
    return _stable_id("CASE", msg.provider, msg.provider_message_id)


def _normalize_ts(ts: Optional[str]) -> str:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
    except Exception:
        return _now_iso()


def maps_url(latitude: float, longitude: float) -> str:
    return f"https://maps.google.com/?q={latitude:.6f},{longitude:.6f}"


def _normalize_phone(phone: str) -> str:
    phone = (phone or "").strip().replace(" ", "")
    return phone if phone.startswith("+") else f"+{phone}"


def is_greeting(text: str) -> bool:
    norm = re.sub(r"[^\w\s]", " ", (text or "").lower())
    norm = re.sub(r"\s+", " ", norm).strip()
    return norm in GREETINGS


async def _safe_send(method_name: str, *args: Any) -> Optional[Dict[str, Any]]:
    """WhatsApp replies never fail the pipeline: the case is already saved by then, and a
    retry would only repeat work. Failures are logged."""
    try:
        return await getattr(whatsapp_service, method_name)(*args)
    except Exception as e:
        print(f"[Pipeline] WhatsApp {method_name} failed: {e}")
        return None


async def _publish_message(msg: ChatMessage) -> None:
    try:
        await events_service.publish_chat_message(msg)
    except Exception as e:
        print(f"[Pipeline] Event publish failed for message {msg.message_id}: {e}")


async def _publish_case(case: EmergencyCase) -> None:
    try:
        await events_service.publish_case(case)
    except Exception as e:
        print(f"[Pipeline] Event publish failed for case {case.case_id}: {e}")


def _detect_language(text: str) -> str:
    try:
        language, _ = audio_service.detect_language(text)
        return language or "English"
    except Exception as e:
        print(f"[Pipeline] Language detection failed: {e}")
        return "English"


async def _image_to_text(msg: InboundWhatsAppMessage, user_phone: str) -> Dict[str, Any]:
    """IMAGE-1: stores the photo and has the image agent describe it. `transcript` is the photo in
    words (`photo_text`); `ok` when there is something to triage: an analysis or a caption."""
    caption = (msg.caption or "").strip() or None
    image_key = None
    try:
        image = await whatsapp_service.fetch_media_to_s3(msg.media_id, user_phone, kind="image",
                                                         mime_type=msg.media_mime_type)
        image_key = image.key
        data = await asyncio.to_thread(audio_service.read_media_bytes, image.key)
        analysis = await image_analyzer.analyze(data, msg.media_mime_type, caption)
    except Exception as e:
        analysis = ImageAnalysis(ok=False, error=f"photo could not be retrieved: {e}")
    language = _detect_language(caption) if caption else "English"
    ok = analysis.ok or bool(caption)
    return {"ok": ok, "transcript": photo_text(caption, analysis) if ok else "", "language": language,
            "audio_s3_key": None, "image_s3_key": image_key, "image_analysis": analysis.as_dict(),
            "caption": caption, "error": None if analysis.ok else analysis.error}


def _photo_chat_text(speech: Dict[str, Any]) -> str:
    """The patient's photo as the doctor reads it in the chat (the photo itself is shown above it)."""
    analysis = speech.get("image_analysis") or {}
    text = speech.get("transcript") or photo_text(speech.get("caption"), ImageAnalysis(
        ok=False, error=speech.get("error") or analysis.get("error")))
    if analysis.get("ok") and analysis.get("clinical_summary"):
        text += f"\nAI summary: {analysis['clinical_summary']}"
    return f"📷 {text}"


async def _message_to_text(msg: InboundWhatsAppMessage, user_phone: str) -> Dict[str, Any]:
    """Returns {"ok", "transcript", "language", "audio_s3_key", "error"} for any message type
    (photos add "image_s3_key", "image_analysis" and "caption")."""
    if msg.type == "image":
        return await _image_to_text(msg, user_phone)
    if msg.type == "text":
        text = msg.text or ""
        return {"ok": True, "transcript": text, "language": _detect_language(text), "audio_s3_key": None, "error": None}
    if msg.simulated_transcript is not None:
        text = msg.simulated_transcript
        language = msg.simulated_language or _detect_language(text)
        return {"ok": True, "transcript": text, "language": language, "audio_s3_key": None, "error": None}
    audio_key = None
    try:
        audio = await whatsapp_service.fetch_media_to_s3(msg.media_id, user_phone)
        audio_key = audio.key
        result = await audio_service.transcribe_audio(audio.s3_uri)
    except Exception as e:
        return {"ok": False, "transcript": "", "language": "Unknown", "audio_s3_key": audio_key,
                "error": f"voice note could not be retrieved or transcribed: {e}"}
    if not result.ok or not (result.transcript or "").strip():
        return {"ok": False, "transcript": "", "language": result.language or "Unknown",
                "audio_s3_key": audio_key, "error": result.error or "empty transcript"}
    return {"ok": True, "transcript": result.transcript, "language": result.language,
            "audio_s3_key": audio_key, "error": None}


# ====================================================================== entry point
async def process_inbound_message(msg: InboundWhatsAppMessage, received_at: Optional[str] = None) -> Dict[str, Any]:
    """Processes one inbound message exactly once.

    Returns {"status": "PROCESSED", "action", "case_id", "provider_message_id"};
    {"status": "DUPLICATE", ...} when it was already processed; {"status": "IN_PROGRESS"} when
    another worker holds the message claim or the sender lease (the caller must not delete the
    SQS message; `retry_after_seconds`, when present, suggests when to retry).
    Raises on failure after releasing the claim, so the SQS message is retried.
    """
    key = idempotency_key(msg.provider, msg.provider_message_id)
    if not dynamo_service.claim_idempotency_key(key, lease_seconds=MESSAGE_LEASE_SECONDS):
        record = dynamo_service.get_idempotency_record(key) or {}
        result = record.get("result") or {}
        status = "DUPLICATE" if record.get("status") == "COMPLETE" else "IN_PROGRESS"
        print(f"[Pipeline] {status}: {key} already claimed, skipping.")
        return {"status": status, "provider_message_id": msg.provider_message_id,
                "case_id": result.get("case_id"), "action": result.get("action")}

    s_key = sender_lease_key(_normalize_phone(msg.sender_phone))
    try:
        holds_sender = await _claim_sender_lease(s_key)
    except Exception:
        dynamo_service.release_idempotency_key(key)
        raise
    if holds_sender is None:
        # Another message from this sender is being routed right now. Let it finish first, so
        # this one then sees its case (handover) instead of opening a second case.
        dynamo_service.release_idempotency_key(key)
        print(f"[Pipeline] IN_PROGRESS: {s_key} is busy with another message; {key} will be retried.")
        return {"status": "IN_PROGRESS", "provider_message_id": msg.provider_message_id,
                "case_id": None, "action": None, "reason": "sender_busy",
                "retry_after_seconds": SENDER_BUSY_RETRY_SECONDS}

    try:
        result = await _route(msg, received_at or _now_iso())
    except Exception:
        dynamo_service.release_idempotency_key(key)
        raise
    finally:
        if holds_sender:
            dynamo_service.release_idempotency_key(s_key)
    dynamo_service.complete_idempotency_key(key, {"case_id": result.get("case_id"), "action": result["action"]})
    return {"status": "PROCESSED", "provider_message_id": msg.provider_message_id, **result}


def sender_lease_key(phone: str) -> str:
    return f"sender:{phone}"


def _inline_mode() -> bool:
    """True when there is no SQS queue (pure mock mode): nobody would retry an IN_PROGRESS."""
    try:
        from app.services.queue_service import queue_service  # lazy: queue_service imports this module
        return bool(queue_service.inline)
    except Exception:
        return True


async def _claim_sender_lease(s_key: str) -> Optional[bool]:
    """True: lease taken (release it after routing). None: busy, the worker must retry later.
    False: inline mode waited and gave up; it proceeds without the lease (never drop a message)."""
    if dynamo_service.claim_idempotency_key(s_key, lease_seconds=SENDER_LEASE_SECONDS):
        return True
    if not _inline_mode():
        return None
    for _ in range(max(0, SENDER_WAIT_ATTEMPTS)):
        await asyncio.sleep(SENDER_WAIT_DELAY_SECONDS)
        if dynamo_service.claim_idempotency_key(s_key, lease_seconds=SENDER_LEASE_SECONDS):
            return True
    print(f"[Pipeline] WARNING: {s_key} still busy after waiting; processing without the sender lease.")
    return False


def doctor_action_prefix(payload: Optional[str]) -> Optional[str]:
    """"OFFER:ACCEPT:<case>:<offer>" -> "OFFER"; None for payloads that are not doctor actions."""
    prefix = (payload or "").split(":", 1)[0]
    return prefix if prefix in DOCTOR_ACTION_PREFIXES and ":" in (payload or "") else None


async def _doctor_action(msg: InboundWhatsAppMessage, doctor: DoctorProfile) -> Dict[str, Any]:
    prefix = doctor_action_prefix(msg.button_payload)
    parts = (msg.button_payload or "").split(":")[1:]
    handler = DOCTOR_ACTIONS.get(prefix or "")
    print(f"[Pipeline] Doctor {doctor.doctor_id} tapped {msg.button_payload!r}.")
    if handler is None:
        await _safe_send("send_text_message", doctor.phone_number,
                         "This button is no longer active. Please open the ResQ-AI portal.")
        return {"action": "doctor_action_unhandled", "case_id": None}
    return await handler(msg, doctor, parts)


def _button_as_text(msg: InboundWhatsAppMessage) -> InboundWhatsAppMessage:
    """A button tap that is not a doctor action counts as typing the button's title."""
    return msg.model_copy(update={"type": "text", "text": msg.button_text or msg.button_payload or ""})


async def _route(msg: InboundWhatsAppMessage, received_at: str) -> Dict[str, Any]:
    phone = _normalize_phone(msg.sender_phone)
    # DOC-1: a registered doctor's WhatsApp. Their buttons are actions, their messages go to their
    # one case (or are commands); a doctor without a case is handled as a patient below.
    doctor = dynamo_service.get_doctor_by_phone(phone)
    if doctor is not None and not doctor_verification.number_confirmed(doctor):
        # DOC-8: the number is not confirmed yet. Only the Yes / No answer counts; a PENDING number is
        # asked again, a DECLINED one (its owner is not the doctor) is handled like anyone else.
        is_answer = msg.type == "button" and (msg.button_payload or "").startswith(f"{doctor_verification.PREFIX}:")
        if is_answer:
            return await _doctor_action(msg, doctor)
        if doctor.phone_status == doctor_verification.PENDING:
            return await doctor_verification.handle_unconfirmed(msg, doctor)
        doctor = None
    if doctor is not None:
        doctor = doctor_chat.touch(doctor, _normalize_ts(msg.timestamp))
        if msg.type == "button" and doctor_action_prefix(msg.button_payload):
            return await _doctor_action(msg, doctor)
        as_text = _button_as_text(msg) if msg.type == "button" else msg
        handled, patient_msg = await doctor_chat.handle_message(as_text, doctor)
        if handled is not None:
            return handled
        if patient_msg is not None and patient_msg is not as_text:
            msg = patient_msg  # rewritten, e.g. "@resq emergency <what is happening>"
    if msg.type == "button":
        if doctor_action_prefix(msg.button_payload):
            print(f"[Pipeline] {phone} tapped {msg.button_payload!r} but is not a registered doctor.")
        elif (msg.button_payload or "").startswith(f"{direct_calls.PATIENT_CALL_PREFIX}:"):
            return await direct_calls.handle_patient_button(msg, phone, msg.button_payload.split(":")[1:])
        msg = _button_as_text(msg)
    user = dynamo_service.get_user(phone)
    new_user = user is None
    if new_user:
        user = UserProfile(
            phone_number=phone,
            name=(msg.sender_name or "").strip() or f"Caller {phone[-4:]}",
            onboarding_status="PENDING_DETAILS",
            emergency_contact_name=None,
            emergency_contact_phone=None,
        )
        dynamo_service.save_user(user)
        print(f"[Pipeline] New user {phone} created (PENDING_DETAILS).")

    try:
        result = await _dispatch(msg, user, received_at, new_user)
    except Exception:
        if new_user:
            await _safe_send("send_onboarding_prompt", phone)
        raise
    if new_user and result["action"] != "registered" and not result.get("registered"):
        # After the emergency reply (if any), never before it.
        await _safe_send("send_onboarding_prompt", phone)
    return result


async def _dispatch(msg: InboundWhatsAppMessage, user: UserProfile, received_at: str,
                    new_user: bool = False) -> Dict[str, Any]:
    open_case = dynamo_service.get_open_case_for_user(user.phone_number)

    # SQS retry of the very message that opened this case (the first attempt failed after saving
    # it): finish that triage again in place, rather than treating the message as its own follow-up.
    if (open_case is not None and open_case.provider == msg.provider
            and open_case.provider_message_id == msg.provider_message_id):
        return await _triage(msg, user, received_at)

    # FLOW-1: emergency contact in the patient's own words, typed or spoken
    speech: Optional[Dict[str, Any]] = None
    registered: Optional[UserProfile] = None
    if msg.type in ("text", "audio") and (user.onboarding_status == "PENDING_DETAILS" or open_case is None):
        if msg.type == "audio":
            speech = await _message_to_text(msg, user.phone_number)   # reused by the routes below
        said = (msg.text or "") if msg.type == "text" else (speech["transcript"] if speech["ok"] else "")
        found = await _read_registration(said, user)
        if found is not None and found.complete:
            if not found.reports_emergency:
                return await _register(user, found, open_case, msg)
            # Also an emergency: saved first so the triage below calls this contact; the patient's
            # confirmation waits until after the emergency reply.
            await _register(user, found, open_case, msg, confirm=False)
            user = registered = dynamo_service.get_user(user.phone_number) or user
        elif found is not None and found.is_registration and not found.reports_emergency:
            await _safe_send("send_registration_format_hint", user.phone_number, found.phone_problem)
            return {"action": "registration_hint", "case_id": None}

    result = await _route_message(msg, user, open_case, received_at, new_user, speech)
    if registered is not None:
        await _safe_send("send_registration_confirmation", user.phone_number, registered.name,
                         registered.emergency_contact_name, registered.emergency_contact_phone)
        result["registered"] = True
    return result


async def _read_registration(said: str, user: UserProfile) -> Optional[Registration]:
    """The registration in this message, or None if it cannot be one. Never raises: a failure here
    must not stop an emergency from being triaged."""
    if not said.strip() or is_greeting(said) or not could_be_registration(said):
        return None
    try:
        return await registration_reader.read(said, user.phone_number)
    except Exception as e:
        print(f"[Pipeline] Reading a registration from {user.phone_number} failed: {type(e).__name__}: {e}")
        return None


async def _route_message(msg: InboundWhatsAppMessage, user: UserProfile, open_case: Optional[EmergencyCase],
                         received_at: str, new_user: bool, speech: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    # FLOW-2: handover to the doctor already on the case (re-triaged if the protocol never ran)
    if open_case is not None:
        if msg.type == "location":
            return await _location_shared(msg, user, open_case)
        if needs_followup_triage(open_case) and msg.type in ("text", "audio", "image"):
            return await _followup(msg, user, open_case, speech)
        return await _handover(msg, user, open_case, speech)

    if msg.type == "location":
        await _safe_send("send_text_message", user.phone_number,
                         "Thanks, we received your location, but you have no open ResQ-AI case. If you need "
                         "help, send a voice note or type what is happening.")
        return {"action": "location_no_case", "case_id": None}

    if msg.type == "unsupported":
        await _safe_send("send_text_message", user.phone_number,
                         "ResQ-AI can only read voice notes, photos and text messages. Please send a voice "
                         "note or type what is happening.")
        return {"action": "unsupported", "case_id": None}

    if msg.type == "text":
        if is_greeting(msg.text or ""):
            if user.onboarding_status == "PENDING_DETAILS":
                # New users get the prompt from _route; existing pending users get it again here.
                if not new_user:
                    await _safe_send("send_onboarding_prompt", user.phone_number)
                return {"action": "greeting", "case_id": None}
            await _safe_send("send_text_message", user.phone_number,
                             "Hello from ResQ-AI. In an emergency, send a voice note or describe what is "
                             f"happening and a doctor is alerted automatically. Life-threatening? Call {LOCAL_EMERGENCY_NUMBERS}.")
            return {"action": "greeting", "case_id": None}

    return await _triage(msg, user, received_at, speech)


# ====================================================================== FLOW-1
async def _register(user: UserProfile, found: Registration, open_case: Optional[EmergencyCase],
                    msg: InboundWhatsAppMessage, confirm: bool = True) -> Dict[str, Any]:
    contact_name = found.contact_name or DEFAULT_CONTACT_NAME
    contact_phone = found.contact_phone
    # Re-read so a concurrent active_case_id update is not overwritten.
    current = dynamo_service.get_user(user.phone_number) or user
    name = found.patient_name or current.name
    current.name = name
    current.emergency_contact_name = contact_name
    current.emergency_contact_phone = contact_phone
    current.onboarding_status = "COMPLETE"
    dynamo_service.save_user(current)
    print(f"[Pipeline] User {user.phone_number} registered (contact {contact_name} {contact_phone}, {found.engine}).")
    if confirm:
        await _safe_send("send_registration_confirmation", user.phone_number, name, contact_name, contact_phone)

    if open_case is not None:
        updated = dynamo_service.update_case(open_case.case_id, {"emergency_contact_phone": contact_phone}) or open_case
        note = dynamo_service.save_chat_message(ChatMessage(
            # Not the message's own ID: an emergency in the same message is saved under that one.
            message_id=_stable_id("MSG", msg.provider, msg.provider_message_id, "contact"),
            case_id=open_case.case_id,
            sender="BOT",
            sender_name="System",
            content=f"Patient registered their emergency contact: {contact_name} ({contact_phone}). "
                    "No automated call was placed for this case.",
            provider=msg.provider,
            provider_message_id=msg.provider_message_id,
            timestamp=_normalize_ts(msg.timestamp),  # deterministic: a retry overwrites, never duplicates
        ))
        await _publish_message(note)
        await _publish_case(updated)
    return {"action": "registered", "case_id": open_case.case_id if open_case else None}


# ====================================================================== FLOW-2
async def _save_patient_followup(msg: InboundWhatsAppMessage, user: UserProfile,
                                 case: EmergencyCase, speech: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Transcribes (if needed, and not already done), stores and publishes the patient's message in
    the case chat. Returns the speech result."""
    if msg.type == "unsupported":
        speech = {"ok": True, "transcript": "[Unsupported message type]", "audio_s3_key": None}
    elif speech is None:
        speech = await _message_to_text(msg, user.phone_number)

    if msg.type == "text" or msg.type == "unsupported":
        content = speech["transcript"]
    elif msg.type == "image":
        content = _photo_chat_text(speech)
    elif speech["ok"]:
        content = f"🎤 [Voice Note]: \"{speech['transcript']}\""
    else:
        content = f"🎤 [Voice Note - transcription unavailable: {speech['error']}]"

    chat = dynamo_service.save_chat_message(ChatMessage(
        message_id=_stable_id("MSG", msg.provider, msg.provider_message_id),
        case_id=case.case_id,
        sender="PATIENT",
        sender_name=user.name,
        content=content,
        audio_s3_key=speech.get("audio_s3_key"),
        image_s3_key=speech.get("image_s3_key"),
        provider=msg.provider,
        provider_message_id=msg.provider_message_id,
        timestamp=_normalize_ts(msg.timestamp),
    ))
    await _publish_message(chat)
    # DOC-2/3: the doctor on the case gets it on their WhatsApp too.
    await doctor_chat.forward_patient_message(case, chat)
    if speech["ok"] and msg.type in ("text", "audio", "image"):
        remember_in_background(user.phone_number, case.case_id, [(speech.get("transcript") or "", "USER")],
                               dedupe_key=f"{msg.provider}:{msg.provider_message_id}")
    return speech


async def _handover(msg: InboundWhatsAppMessage, user: UserProfile, case: EmergencyCase,
                    speech: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    await _save_patient_followup(msg, user, case, speech)
    print(f"[Pipeline] Handover: message from {user.phone_number} added to case {case.case_id} (no triage).")
    return {"action": "handover", "case_id": case.case_id}


# ====================================================================== location
async def _alert_contact_with_location(msg: InboundWhatsAppMessage, user: UserProfile, case: EmergencyCase,
                                       link: str) -> Tuple[str, str]:
    """Sends the location to the emergency contact as a WhatsApp template (the contact usually never
    messaged this number, so free text would not be delivered). Returns (status, note for the doctor).
    Claimed per shared location, so an SQS retry does not message the contact twice."""
    contact_phone = case.emergency_contact_phone or user.emergency_contact_phone
    if not contact_phone:
        return "SKIPPED_NO_CONTACT", "📍 No emergency contact is registered, so the location was not sent to anyone else."
    key = f"location_alert:{case.case_id}:{msg.provider}:{msg.provider_message_id}"
    if not dynamo_service.claim_idempotency_key(key):
        return case.location_alert_status or "SENT", ""
    result = await _safe_send("send_emergency_location_alert", contact_phone, user.name, link) or {}
    who = f"{user.emergency_contact_name or 'the emergency contact'} ({contact_phone})"
    if str(result.get("status", "")).startswith("SENT"):
        dynamo_service.complete_idempotency_key(key, {"status": "SENT"})
        return "SENT", f"📍 Location sent to {who} on WhatsApp."
    dynamo_service.release_idempotency_key(key)
    detail = result.get("detail") or result.get("status") or "send failed"
    print(f"[Pipeline] Location alert to {contact_phone} for {case.case_id} failed: {detail}")
    return "FAILED", (f"⚠️ Could not send the location to {who} on WhatsApp ({detail}). "
                      "Please share it with them by phone.")


async def _location_shared(msg: InboundWhatsAppMessage, user: UserProfile, case: EmergencyCase) -> Dict[str, Any]:
    """The patient shared a location on an open case: save it on the case (the latest share wins),
    show it to the doctor in the chat and, when the ESI 1-2 protocol ran, send it to the emergency
    contact. The doctor calls the ambulance with it."""
    link = maps_url(msg.latitude, msg.longitude)
    place = ", ".join(p for p in (msg.location_name, msg.location_address) if p)
    shared_at = _normalize_ts(msg.timestamp)
    updated = dynamo_service.update_case(case.case_id, {
        "location_latitude": msg.latitude,
        "location_longitude": msg.longitude,
        "location_name": msg.location_name,
        "location_address": msg.location_address,
        "location_shared_at": shared_at,
    }) or case
    chat = dynamo_service.save_chat_message(ChatMessage(
        message_id=_stable_id("MSG", msg.provider, msg.provider_message_id),
        case_id=case.case_id,
        sender="PATIENT",
        sender_name=user.name,
        content=f"📍 Shared location{f': {place}' if place else ''}\n{link}",
        provider=msg.provider,
        provider_message_id=msg.provider_message_id,
        timestamp=shared_at,
    ))
    await _publish_message(chat)
    await doctor_chat.forward_patient_message(updated, chat, location={
        "latitude": msg.latitude, "longitude": msg.longitude, "address": place or None})

    status, note = None, ""
    if updated.dispatched_at:
        status, note = await _alert_contact_with_location(msg, user, updated, link)
        updated = dynamo_service.update_case(case.case_id, {"location_alert_status": status}) or updated
    await _publish_case(updated)

    shared_with = updated.assigned_doctor_name or "your doctor"
    if status == "SENT":
        shared_with += f" and {user.emergency_contact_name or 'your emergency contact'}"
    reply = f"✅ Thanks, your location has been shared with {shared_with}."
    if updated.dispatched_at:
        reply += " Stay where you are if it is safe, and keep your phone close."
    bot = await _send_and_store_reply(msg, user, updated, reply)
    if note:
        system = dynamo_service.save_chat_message(ChatMessage(
            message_id=_stable_id("MSG", msg.provider, msg.provider_message_id, "location-alert"),
            case_id=case.case_id,
            sender="BOT",
            sender_name="System",
            content=note,
            timestamp=(datetime.fromisoformat(bot.timestamp) + timedelta(milliseconds=1)).isoformat(),
        ))
        await _publish_message(system)
    print(f"[Pipeline] Location shared on case {case.case_id} (contact alert: {status or 'not an emergency'}).")
    return {"action": "location_shared", "case_id": case.case_id, "contact_alert": status}


async def _request_location(msg: InboundWhatsAppMessage, user: UserProfile, case: EmergencyCase,
                            after: str) -> None:
    """ESI 1-2: asks the patient for their location with WhatsApp's "Send location" button. Stored
    in the chat right after `after`, so the doctor sees what the patient was asked. The caller sets
    `location_requested_at` in its own case write and asks only once per case."""
    await _safe_send("send_location_request", user.phone_number, LOCATION_REQUEST_TEXT)
    note = dynamo_service.save_chat_message(ChatMessage(
        message_id=_stable_id("MSG", msg.provider, msg.provider_message_id, "location-request"),
        case_id=case.case_id,
        sender="BOT",
        sender_name="ResQ-AI",
        content=f"{LOCATION_REQUEST_TEXT}\n[Button: Send location]",
        timestamp=(datetime.fromisoformat(after) + timedelta(milliseconds=1)).isoformat(),
    ))
    await _publish_message(note)


def needs_followup_triage(case: EmergencyCase) -> bool:
    """An open case where the emergency protocol (contact call, specialist) never ran and may be
    needed: a human-review case (transcription failed) or an ESI >= 3 case. Follow-ups on it are
    triaged again."""
    return (bool(case.needs_human_review) or (case.esi_level or 0) >= 3) and not case.dispatched_at


def _merge_transcript(case: EmergencyCase, transcript: str) -> str:
    if case.needs_human_review:
        return transcript  # the stored transcript is only the "transcription failed" placeholder
    return f"{case.transcript}\n\n[Follow-up] {transcript}" if case.transcript else transcript


_VOICE_NOTE_RE = re.compile(r'^🎤 \[Voice Note\]: "(.*)"$', re.S)
_AI_SUMMARY_RE = re.compile(r"\nAI summary: .*$", re.S)


def _patient_said(message: ChatMessage) -> Optional[str]:
    """The patient's words in a stored chat message; None for a voice note that could not be
    transcribed or an unsupported message type."""
    content = (message.content or "").strip()
    if content.startswith("📷"):
        # A photo: its description and caption; nothing when neither could be read.
        if "Image analysis unavailable" in content and "Patient's caption" not in content:
            return None
        return _AI_SUMMARY_RE.sub("", content[len("📷"):]).strip() or None
    if content.startswith("🎤"):
        match = _VOICE_NOTE_RE.match(content)
        return (match.group(1).strip() or None) if match else None
    if not content or content == "[Unsupported message type]":
        return None
    return content


def followup_triage_text(case: EmergencyCase, current_message_id: str, transcript: str) -> str:
    """Triage input for a follow-up: everything the patient said on this case, oldest first, then
    the new message. "Now he is sweating" must be judged together with the earlier "chest pain",
    not on its own. The stored case transcript (`_merge_transcript`) is unchanged."""
    messages = sorted(dynamo_service.get_case_messages(case.case_id), key=lambda m: m.timestamp or "")
    earlier = [said for m in messages
               if m.sender == "PATIENT" and m.message_id != current_message_id
               and (said := _patient_said(m))]
    if not earlier:
        return transcript
    lines = [f"[Earlier message] {said}" for said in earlier]
    return "\n".join(lines + [f"[Latest message] {transcript}"])


async def _followup(msg: InboundWhatsAppMessage, user: UserProfile, case: EmergencyCase,
                    speech: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Follow-up on an open case without the emergency protocol: keep it in the chat AND triage it
    under the same case ID. ESI 1-2 escalates the case in place (contact call, doctor, location
    request); ESI 3
    updates a human-review case; ESI 4-5 only stays in the chat (never auto-downgraded).
    The agent's tools are idempotent per case ID, so re-running them is safe."""
    speech = await _save_patient_followup(msg, user, case, speech)
    if not speech["ok"] or not (speech.get("transcript") or "").strip():
        print(f"[Pipeline] Follow-up on {case.case_id} could not be transcribed; kept in the chat for the doctor.")
        return {"action": "handover", "case_id": case.case_id}

    transcript, language = speech["transcript"], speech.get("language") or case.detected_language
    assessment: TriageAssessment = await triage_agent.evaluate_and_dispatch(
        transcript=followup_triage_text(case, _stable_id("MSG", msg.provider, msg.provider_message_id), transcript),
        user_phone=user.phone_number,
        patient_name=user.name,
        emergency_contact_phone=user.emergency_contact_phone,
        detected_language=language,
        case_id=case.case_id,
    )
    esi = assessment.esi_level
    tool_results = list(case.tool_results or []) + list(assessment.tool_results or [])
    audit = {
        "clinical_summary": assessment.clinical_summary,
        "reasoning": assessment.reasoning,
        "confidence_score": assessment.confidence_score,
        "triage_engine": assessment.triage_engine,
        "degraded": assessment.degraded,
        "tool_results": tool_results,
        "transcript": _merge_transcript(case, transcript),
        "detected_language": assessment.detected_language or language,
        "patient_history": assessment.patient_history or case.patient_history,
    }
    if speech.get("image_s3_key") and not case.image_s3_key:
        # The case's first photo, shown on the case for the doctor.
        audit["image_s3_key"] = speech["image_s3_key"]
        audit["image_analysis"] = speech.get("image_analysis")

    if esi <= 2:
        updates: Dict[str, Any] = {
            **audit,
            "esi_level": esi,
            "severity": "CRITICAL",
            "status": "DOCTOR_HANDOVER",
            "needs_human_review": False,
            "matched_specialty": assessment.matched_specialty or case.matched_specialty,
            "contact_call_id": assessment.contact_call_id or case.contact_call_id,
            "emergency_contact_phone": case.emergency_contact_phone or user.emergency_contact_phone,
        }
        if not case.dispatched_at:
            updates["dispatched_at"] = _now_iso()
        ask_location = not (case.location_requested_at or case.location_shared_at)
        if ask_location:
            updates["location_requested_at"] = _now_iso()
        updated = dynamo_service.update_case(case.case_id, updates) or case
        print(f"[Pipeline] Case {case.case_id} ESCALATED in place by a follow-up: ESI {case.esi_level} -> {esi}.")
        # A pending round was sized for the old ESI (timeout, fan-out): start a new one.
        updated = await doctor_offers.start_offer(updated, restart=True)
        await _publish_case(updated)
        await doctor_chat.notify_doctor(updated, f"⚠️ {updated.patient_name}'s case was escalated to ESI {esi}: "
                                                 f"{updated.clinical_summary or ''}".strip())
        reply = await _send_and_store_reply(msg, user, updated, _reply_for(updated, assessment, user))
        if ask_location:
            await _request_location(msg, user, updated, after=reply.timestamp)
        _remember_triage(msg, user, updated)
        return {"action": "escalated", "case_id": case.case_id}

    if esi == 3 and case.needs_human_review:
        updates = {
            **audit,
            "esi_level": 3,
            "severity": "MODERATE",
            "status": "ACTIVE",
            "needs_human_review": False,
            "matched_specialty": assessment.matched_specialty or case.matched_specialty or "General Emergency",
        }
        updated = dynamo_service.update_case(case.case_id, updates) or case
        print(f"[Pipeline] Human-review case {case.case_id} triaged by a follow-up: ESI 3, stays ACTIVE.")
        await _publish_case(updated)
        await _send_and_store_reply(msg, user, updated, _reply_for(updated, assessment, user))
        _remember_triage(msg, user, updated)
        return {"action": "followup_triaged", "case_id": case.case_id}

    # ESI 3 on an ESI >= 3 case, or ESI 4-5: the doctor already reviews it; keep it in the chat.
    print(f"[Pipeline] Follow-up on {case.case_id} triaged ESI {esi}: kept in the chat, case unchanged.")
    return {"action": "handover", "case_id": case.case_id}


def _remember_triage(msg: InboundWhatsAppMessage, user: UserProfile, case: EmergencyCase) -> None:
    """MEM-1: the triage outcome as an assistant turn, so the memory can learn e.g. "suspected MI
    in October 2026" next to the patient's own words."""
    day = (case.received_at or case.created_at or _now_iso())[:10]
    outcome = "emergency protocol run" if case.dispatched_at else "no emergency protocol"
    note = (f"ResQ-AI triage on {day}: ESI {case.esi_level}, {case.matched_specialty or 'no specialty'}, "
            f"{outcome}. {case.clinical_summary or ''}").strip()
    remember_in_background(user.phone_number, case.case_id, [(note, "ASSISTANT")],
                           dedupe_key=f"{msg.provider}:{msg.provider_message_id}:triage")


def _bot_reply_timestamp(msg: InboundWhatsAppMessage, case: Optional[EmergencyCase] = None) -> str:
    """Deterministic per inbound message (so a retry overwrites the stored reply), and after the
    patient's message: the case's creation time when that is later, else message time + 1 ms."""
    patient_ts = _normalize_ts(msg.timestamp)
    after = (datetime.fromisoformat(patient_ts) + timedelta(milliseconds=1)).isoformat()
    if case is not None and case.created_at:
        try:
            created = datetime.fromisoformat(case.created_at.replace("Z", "+00:00")).astimezone(timezone.utc)
            if created > datetime.fromisoformat(patient_ts):
                return created.isoformat()
        except Exception:
            pass
    return after


async def _send_and_store_reply(msg: InboundWhatsAppMessage, user: UserProfile, case: EmergencyCase,
                                reply: str, use_case_time: bool = False) -> ChatMessage:
    await _safe_send("send_text_message", user.phone_number, reply)
    bot = dynamo_service.save_chat_message(ChatMessage(
        message_id=_stable_id("MSG", msg.provider, msg.provider_message_id, "reply"),
        case_id=case.case_id,
        sender="BOT",
        sender_name="ResQ-AI",
        content=reply,
        timestamp=_bot_reply_timestamp(msg, case if use_case_time else None),
    ))
    await _publish_message(bot)
    return bot


# ====================================================================== triage
async def _triage(msg: InboundWhatsAppMessage, user: UserProfile, received_at: str,
                  speech: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    case_id = case_id_for(msg)
    speech = speech or await _message_to_text(msg, user.phone_number)

    assessment: Optional[TriageAssessment] = None
    if not speech["ok"]:
        case = await _human_review_case(msg, user, case_id, speech, received_at)
    else:
        case, assessment = await _triaged_case(msg, user, case_id, speech, received_at)

    case = _persist_new_case(case)
    # OFFER-1: offer the case to doctors (ESI 1-3, human review); nothing for a dismissed case.
    case = await doctor_offers.start_offer(case)
    reply = _human_review_reply(case) if assessment is None else _reply_for(case, assessment, user)

    if case.status in OPEN_CASE_STATUSES:
        current = dynamo_service.get_user(user.phone_number) or user
        current.active_case_id = case.case_id
        dynamo_service.save_user(current)

    if msg.type == "audio":
        patient_text = (f"🎤 [Voice Note]: \"{speech['transcript']}\"" if speech["ok"]
                        else "🎤 [Voice Note - transcription unavailable, please listen to the recording]")
    elif msg.type == "image":
        patient_text = _photo_chat_text(speech)
    else:
        patient_text = speech["transcript"]
    first = dynamo_service.save_chat_message(ChatMessage(
        message_id=_stable_id("MSG", msg.provider, msg.provider_message_id),
        case_id=case.case_id,
        sender="PATIENT",
        sender_name=user.name,
        content=patient_text,
        audio_s3_key=speech.get("audio_s3_key"),
        image_s3_key=speech.get("image_s3_key"),
        provider=msg.provider,
        provider_message_id=msg.provider_message_id,
        timestamp=_normalize_ts(msg.timestamp),
    ))
    await _publish_case(case)
    await _publish_message(first)

    bot_reply = await _send_and_store_reply(msg, user, case, reply, use_case_time=True)
    if case.esi_level <= 2 and case.status in OPEN_CASE_STATUSES:
        await _request_location(msg, user, case, after=bot_reply.timestamp)
    if speech["ok"]:
        remember_in_background(user.phone_number, case.case_id, [(speech["transcript"], "USER")],
                               dedupe_key=f"{msg.provider}:{msg.provider_message_id}")
        _remember_triage(msg, user, case)
    print(f"[Pipeline] Case {case.case_id} saved: ESI {case.esi_level}, {case.status}"
          f"{' (needs human review)' if case.needs_human_review else ''}.")
    return {"action": "human_review" if case.needs_human_review else "triaged", "case_id": case.case_id}


# Set by other code paths (video: chime_service; the patient's shared location; the doctor's
# "ambulance called"; the doctor offer) and never part of a freshly built triage case.
_PRESERVED_ON_RETRY = {"created_at", "video_room_url", "video_meeting_id", "video_meeting",
                       "video_patient_token", "resolved_at", "location_latitude", "location_longitude",
                       "location_name", "location_address", "location_shared_at", "location_alert_status",
                       "ambulance_called_at", "ambulance_called_by",
                       # The doctor offer (app.doctor_offers) starts after the first save.
                       "assigned_doctor_id", "assigned_doctor_name", "doctor_offer_status", "offer_id",
                       "offered_doctor_ids", "offer_expires_at", "declined_doctor_ids", "offer_history",
                       "offer_started_at", "accepted_at"}


def _persist_new_case(case: EmergencyCase) -> EmergencyCase:
    """One save_case per new case (save_case also bumps the monthly METRIC counters on first
    creation). On an SQS retry the deterministic case ID may already exist from the failed
    attempt: then update the triage fields in place, so fields written by other code paths
    (video meeting, etc.) are never wiped by a freshly constructed model."""
    existing = dynamo_service.get_case(case.case_id)
    if existing is None:
        return dynamo_service.save_case(case)
    print(f"[Pipeline] Case {case.case_id} already exists (retry); updating it in place.")
    return dynamo_service.update_case(case.case_id, case.model_dump(exclude=_PRESERVED_ON_RETRY)) or existing


async def _human_review_case(msg: InboundWhatsAppMessage, user: UserProfile, case_id: str,
                             speech: Dict[str, Any], received_at: str) -> EmergencyCase:
    """SPEECH-1: transcription failed (IMAGE-1: or a photo without caption could not be analysed).
    Treat as possibly urgent, never run the emergency protocol blind. A General Emergency doctor is
    offered the case (ESI 2 timing) once it is saved."""
    error = speech.get("error") or "unknown error"
    photo = msg.type == "image"
    if photo:
        summary = "Photo could not be analysed automatically; a clinician must look at it."
        transcript = (f"[Image analysis failed: {error}] The patient sent a photo without a caption that could "
                      "not be analysed. Look at the photo and contact the patient.")
        failed = "image analysis"
    else:
        summary = "Voice note could not be transcribed automatically; a clinician must listen to the recording."
        transcript = (f"[Transcription failed: {error}] The patient sent a voice note that could not be "
                      "transcribed. Listen to the recording and contact the patient.")
        failed = "transcription"
    doctor: Dict[str, Any] = {}
    try:
        doctor = await match_specialist_doctor_tool(
            required_specialty="General Emergency",
            patient_phone=user.phone_number,
            clinical_summary=summary,
            case_id=case_id,
        ) or {}
    except Exception as e:
        print(f"[Pipeline] Doctor match failed for human-review case {case_id}: {e}")
        doctor = {"tool": "match_specialist_doctor", "status": "ERROR", "error": str(e)}

    case = EmergencyCase(
        case_id=case_id,
        user_phone=user.phone_number,
        patient_name=user.name,
        emergency_contact_phone=user.emergency_contact_phone,
        audio_s3_key=speech.get("audio_s3_key"),
        image_s3_key=speech.get("image_s3_key"),
        image_analysis=speech.get("image_analysis"),
        transcript=transcript,
        detected_language="Unknown" if photo else (speech.get("language") or "Unknown"),
        esi_level=2,
        severity="HIGH",
        status="ACTIVE",
        matched_specialty="General Emergency",
        clinical_summary=summary,
        reasoning=f"Automatic triage skipped because {failed} failed ({error}). No contact call "
                  f"was triggered; a doctor must review the {'photo' if photo else 'recording'}.",
        triage_engine=f"none ({failed} failed)",
        tool_results=[{"tool": "match_specialist_doctor", **doctor}] if doctor else [],
        needs_human_review=True,
        provider=msg.provider,
        provider_message_id=msg.provider_message_id,
        received_at=received_at,
        location_requested_at=_now_iso(),
    )
    return case


def _human_review_reply(case: EmergencyCase) -> str:
    photo = (case.transcript or "").startswith("[Image analysis failed")
    verb = "look at" if photo else "listen to"
    if case.assigned_doctor_name:
        doctor = f"{case.assigned_doctor_name} has been alerted and will {verb} it now."
    elif case.doctor_offer_status == "PENDING":
        doctor = f"A doctor is being alerted to {verb} it now."
    else:
        doctor = (f"No doctor is free right now to {verb} it. Stay on this chat: your case will go to a doctor as "
                  "soon as one is free.")
    if photo:
        head = "⚠️ *ResQ-AI received your photo but could not analyse it automatically.*"
        ask = "Please also send a voice note, or type what is happening."
    else:
        head = "⚠️ *ResQ-AI received your voice note but could not understand it automatically.*"
        ask = "Please send the voice note again, or type what is happening."
    return (
        f"{head}\n\n"
        f"{doctor}\n\n"
        f"{ask}\n"
        f"If this is life-threatening, call {LOCAL_EMERGENCY_NUMBERS} right away."
    )


async def _triaged_case(msg: InboundWhatsAppMessage, user: UserProfile, case_id: str,
                        speech: Dict[str, Any], received_at: str) -> Tuple[EmergencyCase, TriageAssessment]:
    transcript, language = speech["transcript"], speech["language"]
    assessment: TriageAssessment = await triage_agent.evaluate_and_dispatch(
        transcript=transcript,
        user_phone=user.phone_number,
        patient_name=user.name,
        emergency_contact_phone=user.emergency_contact_phone,
        detected_language=language,
        case_id=case_id,
    )
    esi = assessment.esi_level
    tool_results = list(assessment.tool_results or [])
    specialty = assessment.matched_specialty

    if esi <= 2:
        status, severity = "DOCTOR_HANDOVER", "CRITICAL"
    elif esi == 3:
        status, severity = "ACTIVE", "MODERATE"
        # AGENT-5: no emergency protocol for ESI 3, but a doctor reviews the case (offered after
        # the save, with the ESI 3 timeout). The ranking is recorded for the audit trail.
        specialty = specialty or "General Emergency"
        try:
            doc = await match_specialist_doctor_tool(
                required_specialty=specialty,
                patient_phone=user.phone_number,
                clinical_summary=assessment.clinical_summary,
                case_id=case_id,
            ) or {}
            tool_results.append({"tool": "match_specialist_doctor", **doc})
        except Exception as e:
            print(f"[Pipeline] Doctor ranking failed for ESI 3 case {case_id}: {e}")
    else:
        status, severity = "DISMISSED", "LOW"

    case = EmergencyCase(
        case_id=case_id,
        user_phone=user.phone_number,
        patient_name=user.name,
        emergency_contact_phone=user.emergency_contact_phone,
        audio_s3_key=speech.get("audio_s3_key"),
        image_s3_key=speech.get("image_s3_key"),
        image_analysis=speech.get("image_analysis"),
        transcript=transcript,
        detected_language=assessment.detected_language or language,
        esi_level=esi,
        severity=severity,
        status=status,
        matched_specialty=specialty,
        contact_call_id=assessment.contact_call_id,
        clinical_summary=assessment.clinical_summary,
        reasoning=assessment.reasoning,
        confidence_score=assessment.confidence_score,
        triage_engine=assessment.triage_engine,
        degraded=assessment.degraded,
        tool_results=tool_results,
        patient_history=list(assessment.patient_history or []),
        provider=msg.provider,
        provider_message_id=msg.provider_message_id,
        received_at=received_at,
        dispatched_at=_now_iso() if esi <= 2 else None,
        location_requested_at=_now_iso() if esi <= 2 else None,
    )
    return case, assessment


def _esi3_doctor_line(case: EmergencyCase) -> str:
    if case.assigned_doctor_name:
        return f"{case.assigned_doctor_name} will review your message and may contact you here."
    if case.doctor_offer_status == "EXHAUSTED":
        return ("No doctor is free right now: a doctor will review your message as soon as one is free, and may "
                "contact you here.")
    return "A doctor will review your message and may contact you here."


def _reply_for(case: EmergencyCase, assessment: TriageAssessment, user: UserProfile) -> str:
    esi = case.esi_level
    doctor = case.assigned_doctor_name
    if esi <= 2:
        lines = [f"🚨 *EMERGENCY DETECTED (ESI Level {esi})*", ""]
        if assessment.emergency_contact_called:
            who = user.emergency_contact_name or "your emergency contact"
            lines.append(f"✅ *Emergency contact*: automated call placed to {who} ({user.emergency_contact_phone}).")
        elif not user.emergency_contact_phone:
            lines.append("⚠️ *Emergency contact*: none registered, so nobody was called. Send us your "
                         "emergency contact's name and number, typed or in a voice note, to register one.")
        else:
            lines.append("⚠️ *Emergency contact*: we could not place the call automatically.")
        if doctor:
            lines.append(f"✅ *Doctor*: {doctor} ({case.matched_specialty or 'Emergency Medicine'}) is handling "
                         "your case and will arrange an ambulance.")
            lines += ["", "The doctor is reviewing your message on the portal and may start a video call. "
                          "Keep your phone close; anything you send now goes straight to the doctor."]
        elif case.doctor_offer_status == "PENDING":
            lines.append(f"⏳ *Doctor*: a {case.matched_specialty or 'emergency'} specialist is being contacted "
                         "now. You will get a message as soon as a doctor accepts your case; the doctor arranges "
                         "the ambulance.")
            lines += ["", f"If it gets worse before then, call {LOCAL_EMERGENCY_NUMBERS}. Keep your phone close; "
                          "anything you send now is shown to the doctor."]
        else:
            # OFFER-5: no doctor is free; the case waits and goes to the first one who is.
            lines.append("⏳ *Doctor*: no doctor is free right now. Stay on this chat: your case will go to a "
                         "doctor as soon as one is free, and the doctor arranges the ambulance.")
            lines += ["", f"If it gets worse, call {LOCAL_EMERGENCY_NUMBERS}. Keep your phone close; anything you "
                          "send now is shown to the doctor."]
        return "\n".join(lines)
    if esi == 3:
        return (
            "🩺 *ResQ-AI Triage: Urgent (ESI Level 3)*\n\n"
            "Your symptoms need prompt medical attention but do not appear immediately life-threatening, "
            "so no ambulance has been sent.\n\n"
            "Please go to the nearest urgent care or emergency department within the next few hours.\n"
            f"{_esi3_doctor_line(case)}\n\n"
            f"If symptoms get worse (chest pain, trouble breathing, fainting, heavy bleeding), call {LOCAL_EMERGENCY_NUMBERS} "
            "or send a voice note immediately."
        )
    described = ("Based on your photo" if case.image_s3_key and case.transcript.startswith("[Photo")
                 else f"Based on your description (\"{case.transcript}\")")
    return (
        "ℹ️ *ResQ-AI Medical Triage Advisory*\n\n"
        f"{described}, this does not appear to be an emergency, "
        "so no ambulance has been sent.\n\n"
        "Please visit an outpatient clinic or your regular physician if symptoms persist. "
        f"If anything changes or gets worse, send a voice note or call {LOCAL_EMERGENCY_NUMBERS}."
    )


# ====================================================================== ambulance (doctor)
async def mark_ambulance_called(case_id: str, called_by: str) -> Optional[EmergencyCase]:
    """The doctor called an ambulance for the patient (nothing is dispatched automatically):
    records who and when, tells the patient on WhatsApp and pushes the update to the portal.
    Idempotent: a second call returns the case unchanged and sends nothing."""
    case = dynamo_service.get_case(case_id)
    if not case or case.ambulance_called_at:
        return case
    updated = dynamo_service.update_case(case_id, {"ambulance_called_at": _now_iso(),
                                                   "ambulance_called_by": called_by}) or case
    where = "to the location you shared" if case.location_shared_at else "for you"
    text = (f"🚑 *Ambulance called*: {case.assigned_doctor_name or 'Your doctor'} has called an ambulance "
            f"{where}. Stay where you are if it is safe, and keep your phone close.")
    note = dynamo_service.save_chat_message(ChatMessage(
        message_id=f"MSG-{uuid.uuid4().hex[:6]}",
        case_id=case_id,
        sender="BOT",
        sender_name="ResQ-AI",
        content=f"{text}\n(Recorded by {called_by}.)",
    ))
    await _publish_message(note)
    await _publish_case(updated)
    await _safe_send("send_text_message", case.user_phone, text)
    if not case.location_shared_at:
        await _safe_send("send_location_request", case.user_phone, LOCATION_REQUEST_TEXT)
    return updated


# ====================================================================== resolve (FLOW-2)
async def resolve_case(case_id: str, resolved_by: Optional[str] = None) -> Optional[EmergencyCase]:
    """Marks the case RESOLVED, clears the user's active_case_id, tells the patient and publishes."""
    case = dynamo_service.get_case(case_id)
    if not case:
        return None
    if case.status == "RESOLVED":
        return case
    updated = dynamo_service.update_case(case_id, {"status": "RESOLVED", "resolved_at": _now_iso()}) or case

    user = dynamo_service.get_user(case.user_phone)
    if user and user.active_case_id == case_id:
        user.active_case_id = None
        dynamo_service.save_user(user)

    note = dynamo_service.save_chat_message(ChatMessage(
        message_id=f"MSG-{uuid.uuid4().hex[:6]}",
        case_id=case_id,
        sender="BOT",
        sender_name="System",
        content=f"✅ Case resolved{f' by {resolved_by}' if resolved_by else ''}.",
    ))
    await _publish_message(note)
    await _publish_case(updated)
    await _safe_send("send_text_message", case.user_phone,
                     "✅ Your ResQ-AI case has been closed by the doctor. If you need help again, "
                     "send a new voice note at any time.")
    # OFFER-4: the doctor (and anyone with a pending offer) is free again, and is offered a waiting case.
    await doctor_chat.case_closed(updated, resolved_by)
    await doctor_offers.release_case_doctors(updated, closed=True)
    return updated
