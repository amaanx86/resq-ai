"""The doctor's messages to the patient, whichever way the doctor sends them (DOC-2, DOC-3).

The portal (`POST /api/v1/cases/{id}/messages`, `.../messages/media`) and the doctor's own WhatsApp
(`app.doctor_chat`) both go through these functions, so the patient gets the same message, the
case chat the same `DOCTOR` entry (shown live in the portal) and patient memory the same note.
"""
import asyncio
import uuid
from typing import Optional

from app.config import settings
from app.models.schemas import ChatMessage, EmergencyCase
from app.services import events_service, media_convert
from app.services.audio_service import OUTBOUND_FOLDER, audio_service
from app.services.dynamo_service import dynamo_service
from app.services.memory_service import remember_in_background
from app.services.whatsapp_service import whatsapp_service


class MediaSendError(Exception):
    """A doctor's photo or voice note could not be sent. `status`: the HTTP status the portal returns
    (400 empty, 413 too large, 415 not a photo / voice note, 502 storage or WhatsApp failure)."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def doctor_label(sender_name: str) -> str:
    return f"👨‍⚕️ *{sender_name}*"


async def _publish(msg: ChatMessage) -> None:
    try:
        await events_service.publish_chat_message(msg)
    except Exception as e:
        print(f"[CaseMessages] Event publish failed for {msg.message_id}: {e}")


async def send_doctor_text(case: EmergencyCase, sender_name: str, text: str, *,
                           message_id: Optional[str] = None, timestamp: Optional[str] = None,
                           provider: Optional[str] = None, provider_message_id: Optional[str] = None) -> ChatMessage:
    """Saves the doctor's text in the case chat and sends it to the patient's WhatsApp. A stable
    `message_id` (a WhatsApp message retried by SQS) overwrites instead of adding a second entry."""
    fields = {"timestamp": timestamp} if timestamp else {}
    msg = ChatMessage(
        message_id=message_id or f"MSG-{uuid.uuid4().hex[:6]}",
        case_id=case.case_id,
        sender="DOCTOR",
        sender_name=sender_name,
        content=text,
        provider=provider,
        provider_message_id=provider_message_id,
        **fields,
    )
    dynamo_service.save_chat_message(msg)
    await _publish(msg)
    result = await whatsapp_service.send_text_message(case.user_phone, f"{doctor_label(sender_name)}: {text}")
    if (result or {}).get("status") == "ERROR":
        print(f"[CaseMessages] Doctor message to {case.user_phone} on {case.case_id} failed: {result.get('detail')}")
    # Patient memory (MEM-1): the doctor's notes are the most reliable facts about the patient.
    remember_in_background(case.user_phone, case.case_id, [(f"Doctor {sender_name}: {text}", "ASSISTANT")],
                           dedupe_key=msg.message_id)
    return msg


async def send_doctor_media(case: EmergencyCase, sender_name: str, data: bytes, content_type: Optional[str],
                            caption: str = "", *, message_id: Optional[str] = None, timestamp: Optional[str] = None,
                            provider: Optional[str] = None, provider_message_id: Optional[str] = None) -> ChatMessage:
    """A doctor's photo or voice note to the patient (IMAGE-1). Photos go out as JPEG / PNG, voice
    notes as mono OGG/Opus whatever was recorded (`app.services.media_convert`). The file is kept in
    the media vault (`outbound/<case>/`) and the chat message links it. Raises MediaSendError
    (nothing is saved then)."""
    if not data:
        raise MediaSendError(400, "The file is empty")
    if len(data) > settings.MEDIA_UPLOAD_MAX_BYTES:
        raise MediaSendError(413, f"The file is larger than {settings.MEDIA_UPLOAD_MAX_BYTES // (1024 * 1024)} MB")
    content_type = media_convert.base_type(content_type)
    kind = "image" if content_type.startswith("image/") else "audio" if content_type.startswith("audio/") else None
    if kind is None:
        raise MediaSendError(415, "Only photos and voice notes can be sent")
    try:
        if kind == "image":
            data, content_type, ext = await asyncio.to_thread(media_convert.prepare_image, data, content_type)
        else:
            data, content_type, ext = await asyncio.to_thread(media_convert.prepare_audio, data, content_type)
    except media_convert.UnsupportedMedia as e:
        raise MediaSendError(415, f"This file cannot be sent on WhatsApp: {e}")

    caption = (caption or "").strip()
    key = audio_service.build_media_key(f"{OUTBOUND_FOLDER}/{case.case_id}", case.user_phone, ext)
    try:
        await asyncio.to_thread(audio_service.upload_media_to_s3, data, key, content_type)
    except Exception as e:
        raise MediaSendError(502, f"Could not store the file: {e}")

    label = doctor_label(sender_name)
    what = "photo" if kind == "image" else "voice note"
    if kind == "audio":
        # A voice note has no caption: say who it is from (and the doctor's text) just before it.
        await whatsapp_service.send_text_message(case.user_phone, f"{label}: {caption or 'Voice note from your doctor'}")
    result = await whatsapp_service.send_media_message(
        case.user_phone, kind, key, data, content_type,
        caption=(f"{label}: {caption}" if caption else label) if kind == "image" else None)
    if result.get("status") == "ERROR":
        raise MediaSendError(502, f"WhatsApp did not accept the {what}: {result.get('detail') or 'send failed'}")

    icon = "📷 [Photo]" if kind == "image" else "🎤 [Voice note]"
    fields = {"timestamp": timestamp} if timestamp else {}
    msg = ChatMessage(
        message_id=message_id or f"MSG-{uuid.uuid4().hex[:6]}",
        case_id=case.case_id,
        sender="DOCTOR",
        sender_name=sender_name,
        content=f"{icon}: {caption}" if caption else icon,
        audio_s3_key=key if kind == "audio" else None,
        image_s3_key=key if kind == "image" else None,
        provider=provider,
        provider_message_id=provider_message_id,
        **fields,
    )
    dynamo_service.save_chat_message(msg)
    await _publish(msg)
    if caption:
        remember_in_background(case.user_phone, case.case_id, [(f"Doctor {sender_name}: {caption}", "ASSISTANT")],
                               dedupe_key=msg.message_id)
    return msg
