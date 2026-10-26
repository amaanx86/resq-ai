"""Inbound adapters: raw provider payloads -> InboundWhatsAppMessage.

Both providers deliver the same WhatsApp Cloud API webhook structure
(`entry[].changes[].value.messages[]`); they differ only in the outer wrapper:

* Meta Cloud API: the webhook HTTP body is `{"object": "whatsapp_business_account", "entry": [...]}`.
* AWS End User Messaging Social: the event destination (SNS topic) receives
  `{"context": {...}, "whatsAppWebhookEntry": "<JSON string of ONE entry>", "aws_account_id": ...,
  "message_timestamp": ..., "messageId": ...}`. Through SNS -> SQS the SQS body is either the SNS
  notification envelope (`{"Type": "Notification", "Message": "<that event as a string>", ...}`) or,
  with raw message delivery enabled on the subscription, the event itself.

`parse_entry` holds the shared part; the public functions only unwrap. Besides messages, an entry
can carry WhatsApp Calling API events (`field: "calls"`): `value.calls[]` (connect / terminate) and
call progress in `value.statuses[]` (`type: "call"`). Those become `InboundCallEvent`s, so every
parse function returns `InboundEvent`s (a message or a call event); the worker routes by type.

The queue also carries `InternalTask`s the backend schedules for itself (`parse_queue_message`).
"""
import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Union

from pydantic import ValidationError

from app.config import settings
from app.models.schemas import InboundCallEvent, InboundWhatsAppMessage, InternalTask

SUPPORTED_AUDIO_TYPES = ("audio", "voice")
# Photos (IMAGE-1). WhatsApp sends image/jpeg or image/png; a photo sent "as a document" keeps its
# original format and is read too when it is an image.
SUPPORTED_IMAGE_MIME_PREFIX = "image/"
CALL_EVENTS = ("connect", "terminate")

# What a webhook entry yields, and what a queue body yields.
InboundEvent = Union[InboundWhatsAppMessage, InboundCallEvent]
QueueItem = Union[InboundWhatsAppMessage, InboundCallEvent, InternalTask]


class UnrecognizedMessageError(ValueError):
    """The payload is not a Meta webhook, an EUM Social event or a serialized InboundWhatsAppMessage."""


# ---------------------------------------------------------------------------
# Shared WhatsApp Cloud API parsing (inner structure, identical for both providers)
# ---------------------------------------------------------------------------
def to_e164(phone: Optional[str]) -> str:
    """WhatsApp sends `from`/`wa_id` as digits without '+'. Internally we always use '+<digits>'."""
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    return f"+{digits}" if digits else ""


def _iso_timestamp(raw: Any, fallback: Optional[str] = None) -> str:
    """WhatsApp message timestamps are Unix seconds (as a string). Returns ISO 8601 UTC."""
    try:
        return datetime.fromtimestamp(int(str(raw)), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        pass
    if fallback:
        try:
            # EUM Social message_timestamp has nanoseconds ("...43.271279391Z"); trim to micro.
            text = fallback.replace("Z", "+00:00")
            if "." in text:
                head, tail = text.split(".", 1)
                frac, _, tz = tail.partition("+")
                text = f"{head}.{frac[:6]}" + (f"+{tz}" if tz else "")
            return datetime.fromisoformat(text).astimezone(timezone.utc).isoformat()
        except ValueError:
            pass
    return datetime.now(timezone.utc).isoformat()


def _sender_name(contacts: Iterable[Dict[str, Any]], wa_id: str) -> Optional[str]:
    contacts = [c for c in (contacts or []) if isinstance(c, dict)]
    for contact in contacts:
        if contact.get("wa_id") == wa_id:
            return (contact.get("profile") or {}).get("name") or None
    if len(contacts) == 1:
        return (contacts[0].get("profile") or {}).get("name") or None
    return None


def _parse_message(
    msg: Dict[str, Any],
    contacts: List[Dict[str, Any]],
    provider: str,
    fallback_timestamp: Optional[str] = None,
) -> Optional[InboundWhatsAppMessage]:
    message_id = msg.get("id")
    sender = msg.get("from")
    if not message_id or not sender:
        print(f"[Messaging] Skipping malformed WhatsApp message without id/from: {msg}")
        return None

    msg_type = msg.get("type")
    fields: Dict[str, Any] = {"type": "unsupported"}
    if msg_type == "text":
        fields = {"type": "text", "text": (msg.get("text") or {}).get("body", "")}
    elif msg_type in SUPPORTED_AUDIO_TYPES:
        # Voice notes arrive as type "audio" with audio.voice=true; older payloads used "voice".
        media = msg.get(msg_type) or msg.get("audio") or {}
        if media.get("id"):
            fields = {
                "type": "audio",
                "media_id": str(media["id"]),
                "media_mime_type": media.get("mime_type"),
            }
        else:
            print(f"[Messaging] Audio message {message_id} has no media id; treating as unsupported.")
    elif msg_type in ("image", "document"):
        media = msg.get(msg_type) or {}
        mime = (media.get("mime_type") or "").lower()
        if media.get("id") and (msg_type == "image" or mime.startswith(SUPPORTED_IMAGE_MIME_PREFIX)):
            fields = {
                "type": "image",
                "media_id": str(media["id"]),
                "media_mime_type": media.get("mime_type") or "image/jpeg",
                "caption": (media.get("caption") or "").strip() or None,
            }
        else:
            print(f"[Messaging] {msg_type.capitalize()} message {message_id} is not a readable image; "
                  "treating as unsupported.")
    elif msg_type == "location":
        location = msg.get("location") or {}
        try:
            fields = {
                "type": "location",
                "latitude": float(location["latitude"]),
                "longitude": float(location["longitude"]),
                "location_name": location.get("name") or None,
                "location_address": location.get("address") or None,
            }
        except (KeyError, TypeError, ValueError):
            print(f"[Messaging] Location message {message_id} has no valid coordinates; treating as unsupported.")
    elif msg_type == "button":
        # Template quick-reply button: the payload is the one set in the template send.
        button = msg.get("button") or {}
        if button.get("payload") or button.get("text"):
            fields = {"type": "button", "button_payload": button.get("payload") or None,
                      "button_text": button.get("text") or None}
    elif msg_type == "interactive":
        # Interactive reply button or list row: the id is the one set in the interactive send.
        interactive = msg.get("interactive") or {}
        reply = interactive.get(interactive.get("type") or "") or {}
        if interactive.get("type") in ("button_reply", "list_reply") and (reply.get("id") or reply.get("title")):
            fields = {"type": "button", "button_payload": reply.get("id") or None,
                      "button_text": reply.get("title") or None}
        else:
            print(f"[Messaging] Interactive message {message_id} of type {interactive.get('type')!r} "
                  "is not a button reply; treating as unsupported.")

    return InboundWhatsAppMessage(
        provider=provider,
        provider_message_id=str(message_id),
        sender_phone=to_e164(sender),
        sender_name=_sender_name(contacts, str(sender)),
        timestamp=_iso_timestamp(msg.get("timestamp"), fallback_timestamp),
        **fields,
    )


def _optional_int(raw: Any) -> Optional[int]:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _optional_iso(raw: Any) -> Optional[str]:
    return _iso_timestamp(raw) if raw not in (None, "") else None


def _first_error(item: Dict[str, Any]) -> Optional[str]:
    errors = [e for e in (item.get("errors") or []) if isinstance(e, dict)]
    if not errors:
        return None
    err = errors[0]
    return " ".join(str(p) for p in (err.get("code"), err.get("title"), err.get("message")) if p) or None


def _parse_call(
    call: Dict[str, Any],
    contacts: List[Dict[str, Any]],
    metadata: Dict[str, Any],
    provider: str,
    fallback_timestamp: Optional[str] = None,
) -> Optional[InboundCallEvent]:
    """`value.calls[]` item (connect / terminate) -> InboundCallEvent."""
    call_id, event = call.get("id"), call.get("event")
    if not call_id or event not in CALL_EVENTS:
        print(f"[Messaging] Skipping WhatsApp call event without id or with event {event!r}: {call_id}")
        return None
    direction = call.get("direction")
    # The WhatsApp user is the caller on a user-initiated call and the callee otherwise.
    user = call.get("to") if direction == "BUSINESS_INITIATED" else call.get("from")
    business = call.get("from") if direction == "BUSINESS_INITIATED" else call.get("to")
    if not user:
        print(f"[Messaging] Skipping WhatsApp call event {call_id} without a user phone.")
        return None
    session = call.get("session") or {}
    return InboundCallEvent(
        provider=provider,
        call_id=str(call_id),
        event=event,
        user_phone=to_e164(user),
        user_name=_sender_name(contacts, str(user)),
        business_phone=to_e164(business or metadata.get("display_phone_number")) or None,
        phone_number_id=metadata.get("phone_number_id"),
        direction=direction,
        timestamp=_iso_timestamp(call.get("timestamp"), fallback_timestamp),
        sdp_type=session.get("sdp_type"),
        sdp=session.get("sdp"),
        status=call.get("status"),
        duration_seconds=_optional_int(call.get("duration")),
        start_time=_optional_iso(call.get("start_time")),
        end_time=_optional_iso(call.get("end_time")),
        biz_opaque_callback_data=call.get("biz_opaque_callback_data"),
        error=_first_error(call),
    )


def _parse_call_status(
    status: Dict[str, Any],
    metadata: Dict[str, Any],
    provider: str,
    fallback_timestamp: Optional[str] = None,
) -> Optional[InboundCallEvent]:
    """`value.statuses[]` item with `type: "call"` (RINGING / ACCEPTED / REJECTED of a
    business-initiated call) -> InboundCallEvent. Message delivery receipts are not call events."""
    if status.get("type") != "call" or not status.get("id") or not status.get("recipient_id"):
        return None
    return InboundCallEvent(
        provider=provider,
        call_id=str(status["id"]),
        event="status",
        user_phone=to_e164(status["recipient_id"]),
        business_phone=to_e164(metadata.get("display_phone_number")) or None,
        phone_number_id=metadata.get("phone_number_id"),
        direction="BUSINESS_INITIATED",
        timestamp=_iso_timestamp(status.get("timestamp"), fallback_timestamp),
        status=status.get("status"),
        biz_opaque_callback_data=status.get("biz_opaque_callback_data"),
        error=_first_error(status),
    )


def _log_failed_delivery(status: Dict[str, Any], provider: str) -> None:
    """WhatsApp can accept a send and fail it later (e.g. 131053 for media it cannot process); the
    only trace is a `failed` delivery receipt, so it is logged with Meta's details."""
    if status.get("status") != "failed" or status.get("type") == "call":
        return
    errors = [e for e in (status.get("errors") or []) if isinstance(e, dict)]
    details = ((errors[0].get("error_data") or {}).get("details") if errors else None) or ""
    print(f"[WhatsApp:{provider}] Delivery of {status.get('id')} to {status.get('recipient_id')} failed: "
          f"{_first_error(status) or 'no error given'}{f' ({details})' if details else ''}")


def parse_entry(
    entry: Dict[str, Any], provider: str, fallback_timestamp: Optional[str] = None
) -> List[InboundEvent]:
    """Parses one webhook `entry` object into messages and call events. Message delivery receipts
    (status-only changes) yield nothing."""
    results: List[InboundEvent] = []
    for change in (entry or {}).get("changes") or []:
        value = (change or {}).get("value") or {}
        contacts = value.get("contacts") or []
        metadata = value.get("metadata") or {}
        for msg in value.get("messages") or []:
            if not isinstance(msg, dict):
                continue
            parsed = _parse_message(msg, contacts, provider, fallback_timestamp)
            if parsed:
                results.append(parsed)
        for call in value.get("calls") or []:
            if isinstance(call, dict):
                event = _parse_call(call, contacts, metadata, provider, fallback_timestamp)
                if event:
                    results.append(event)
        for status in value.get("statuses") or []:
            if isinstance(status, dict):
                _log_failed_delivery(status, provider)
                event = _parse_call_status(status, metadata, provider, fallback_timestamp)
                if event:
                    results.append(event)
    return results


# ---------------------------------------------------------------------------
# Meta Cloud API (HTTP webhook)
# ---------------------------------------------------------------------------
def parse_meta_webhook(body: Dict[str, Any]) -> List[InboundEvent]:
    """Meta webhook JSON body -> messages and call events. Non-WhatsApp objects and delivery
    receipts yield []."""
    if not isinstance(body, dict):
        return []
    if body.get("object") not in (None, "whatsapp_business_account"):
        return []
    results: List[InboundEvent] = []
    for entry in body.get("entry") or []:
        results.extend(parse_entry(entry, "meta"))
    return results


def verify_meta_signature(raw_body: bytes, header: Optional[str]) -> bool:
    """Checks Meta's X-Hub-Signature-256 ("sha256=<hex HMAC of the raw body with the app secret>").

    Without WHATSAPP_APP_SECRET: permissive locally (mock / Floci, e.g. ngrok dev without a Meta
    app secret), but False against real AWS (MOCK_AWS=False and no AWS_ENDPOINT_URL), so an
    unconfigured production deployment refuses unsigned events instead of accepting anyone's.
    """
    secret = settings.WHATSAPP_APP_SECRET
    if not secret:
        if not settings.MOCK_AWS and not settings.AWS_ENDPOINT_URL:
            print("[Webhook] Refusing inbound Meta webhook: WHATSAPP_APP_SECRET is not set while running "
                  "against real AWS (MOCK_AWS=False, no AWS_ENDPOINT_URL), so X-Hub-Signature-256 can't be "
                  "verified. Set WHATSAPP_APP_SECRET to the Meta app secret.")
            return False
        return True
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body or b"", hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[len("sha256="):].strip().lower())


# ---------------------------------------------------------------------------
# AWS End User Messaging Social (SNS event destination)
# ---------------------------------------------------------------------------
def _loads(raw: Union[str, bytes, Dict[str, Any]]) -> Any:
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")
    if isinstance(raw, str):
        return json.loads(raw)
    return raw


def _unwrap_sns(event: Any) -> Any:
    """Strips SNS wrappers: a Lambda `Records[]` batch, a Lambda `Sns` record, or an SNS envelope."""
    if isinstance(event, dict) and isinstance(event.get("Records"), list):
        return [_unwrap_sns(record) for record in event["Records"]]
    if isinstance(event, dict) and isinstance(event.get("Sns"), dict):
        event = event["Sns"]
    if isinstance(event, dict) and "Message" in event and ("Type" in event or "TopicArn" in event):
        if event.get("Type") not in (None, "Notification"):
            # SubscriptionConfirmation / UnsubscribeConfirmation carry no WhatsApp data.
            print(f"[Messaging] Ignoring SNS {event.get('Type')} message.")
            return None
        return _loads(event["Message"])
    return event


def _is_eum_event(event: Any) -> bool:
    return isinstance(event, dict) and "whatsAppWebhookEntry" in event


def _parse_eum_payload(event: Dict[str, Any]) -> List[InboundEvent]:
    entry = _loads(event.get("whatsAppWebhookEntry") or "{}")
    entries = entry if isinstance(entry, list) else [entry]
    results: List[InboundEvent] = []
    for item in entries:
        results.extend(parse_entry(item, "eum_social", event.get("message_timestamp")))
    return results


def parse_eum_social_event(raw: Union[str, bytes, Dict[str, Any]]) -> List[InboundEvent]:
    """EUM Social event (raw, inside an SNS envelope, or a Lambda SNS record batch) -> messages
    and call events."""
    unwrapped = _unwrap_sns(_loads(raw))
    events = unwrapped if isinstance(unwrapped, list) else [unwrapped]
    results: List[InboundEvent] = []
    for event in events:
        if event is None:
            continue
        if not _is_eum_event(event):
            raise UnrecognizedMessageError("SNS message is not an End User Messaging Social WhatsApp event")
        results.extend(_parse_eum_payload(event))
    return results


# ---------------------------------------------------------------------------
# Queue bodies (what the worker reads from SQS)
# ---------------------------------------------------------------------------
def parse_queue_message(body: Union[str, bytes]) -> List[QueueItem]:
    """Any SQS body on the webhook queue -> messages, call events and internal tasks.

    Accepts a serialized InboundWhatsAppMessage (published by the Meta webhook route or the
    simulator), a serialized InboundCallEvent (Meta webhook route), a serialized InternalTask
    (`queue_service.publish_internal`), an SNS notification envelope wrapping an EUM Social event,
    the raw EUM Social event (SNS raw message delivery), or a raw Meta webhook body. Delivery
    receipts yield []. Raises UnrecognizedMessageError for anything else, so the message retries
    and lands in the DLQ.
    """
    try:
        data = _loads(body)
    except (ValueError, TypeError) as exc:
        raise UnrecognizedMessageError(f"Queue message is not JSON: {exc}") from exc

    if isinstance(data, dict) and "provider_message_id" in data and "provider" in data:
        try:
            return [InboundWhatsAppMessage.model_validate(data)]
        except ValidationError as exc:
            raise UnrecognizedMessageError(f"Invalid InboundWhatsAppMessage: {exc}") from exc

    if isinstance(data, dict) and data.get("kind") == "call_event":
        try:
            return [InboundCallEvent.model_validate(data)]
        except ValidationError as exc:
            raise UnrecognizedMessageError(f"Invalid InboundCallEvent: {exc}") from exc

    if isinstance(data, dict) and "task_id" in data and "kind" in data:
        try:
            return [InternalTask.model_validate(data)]
        except ValidationError as exc:
            raise UnrecognizedMessageError(f"Invalid InternalTask: {exc}") from exc

    if isinstance(data, dict) and data.get("object") == "whatsapp_business_account":
        return parse_meta_webhook(data)

    if isinstance(data, dict) and (_is_eum_event(data) or "Message" in data or "Records" in data or "Sns" in data):
        return parse_eum_social_event(data)

    raise UnrecognizedMessageError("Unrecognized queue message format")
