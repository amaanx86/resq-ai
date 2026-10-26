"""Contract tests for the inbound WhatsApp adapters (INFRA-7).

Fixtures in tests/fixtures/ are recorded-style samples:
* meta_webhook_*.json: Meta WhatsApp Cloud API webhook bodies (POST /api/v1/webhook).
* eum_social_event_*.json: AWS End User Messaging Social event-destination payloads, as published
  to SNS (and as the SQS body with raw message delivery). Format per the AWS doc "Message and event
  format in AWS End User Messaging Social": a `context` header (MetaWabaIds, MetaPhoneNumberIds),
  `whatsAppWebhookEntry` (ONE webhook entry as a JSON *string*), `aws_account_id`,
  `message_timestamp`, `messageId`.
* eum_social_sns_notification_*.json: the SNS notification envelope SQS receives without raw delivery.
Each EUM Social fixture carries the same WhatsApp entry as the matching Meta fixture.
"""
import copy
import hashlib
import hmac
import json
from pathlib import Path

import pytest

from app.config import settings
from app.messaging.inbound import (
    UnrecognizedMessageError,
    parse_eum_social_event,
    parse_meta_webhook,
    parse_queue_message,
    verify_meta_signature,
)
from app.models.schemas import InboundWhatsAppMessage

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def raw(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def without_provider(messages):
    return [m.model_dump(exclude={"provider"}) for m in messages]


def meta_body_with(message: dict, contacts=None) -> dict:
    body = load("meta_webhook_text.json")
    value = body["entry"][0]["changes"][0]["value"]
    value["messages"] = [message]
    if contacts is not None:
        value["contacts"] = contacts
    return body


# ---------------------------------------------------------------------------
# Same message, both providers, every wrapper -> identical InboundWhatsAppMessage
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("kind", ["voice_note", "text"])
def test_meta_and_eum_social_produce_identical_messages(kind):
    from_meta = parse_meta_webhook(load(f"meta_webhook_{kind}.json"))
    eum_event = load(f"eum_social_event_{kind}.json")
    sns_envelope = {
        "Type": "Notification",
        "MessageId": "b1d0c7a0-0000-4000-8000-000000000001",
        "TopicArn": "arn:aws:sns:us-east-1:123456789012:resq-whatsapp-inbound-events",
        "Message": json.dumps(eum_event),
        "Timestamp": "2026-10-11T11:00:01.512Z",
    }

    variants = {
        "eum event dict": parse_eum_social_event(eum_event),
        "eum event string": parse_eum_social_event(json.dumps(eum_event)),
        "sns envelope": parse_eum_social_event(sns_envelope),
        "lambda sns records": parse_eum_social_event({"Records": [{"EventSource": "aws:sns", "Sns": sns_envelope}]}),
        "queue: raw message delivery": parse_queue_message(json.dumps(eum_event)),
        "queue: sns envelope": parse_queue_message(json.dumps(sns_envelope)),
        "queue: serialized model": parse_queue_message(from_meta[0].model_dump_json()),
    }

    assert len(from_meta) == 1
    assert from_meta[0].provider == "meta"
    for label, messages in variants.items():
        assert without_provider(messages) == without_provider(from_meta), label
    assert variants["sns envelope"][0].provider == "eum_social"
    assert variants["queue: serialized model"][0].provider == "meta"


def test_recorded_sns_notification_fixture_matches_meta():
    from_sns = parse_queue_message(raw("eum_social_sns_notification_voice_note.json"))
    from_meta = parse_meta_webhook(load("meta_webhook_voice_note.json"))
    assert without_provider(from_sns) == without_provider(from_meta)
    assert from_sns[0].provider == "eum_social"


def test_voice_note_fields():
    [msg] = parse_meta_webhook(load("meta_webhook_voice_note.json"))
    assert msg == InboundWhatsAppMessage(
        provider="meta",
        provider_message_id="wamid.HBgMOTIzMDAxMjM0NTY3FQIAEhgUM0E4RjcyQjJCMDI1QkU2NzI0QTcA",
        sender_phone="+923001234567",
        sender_name="Ahmed Khan",
        timestamp="2026-10-11T11:00:00+00:00",
        type="audio",
        media_id="1048234539876154",
        media_mime_type="audio/ogg; codecs=opus",
    )


def test_text_fields():
    [msg] = parse_eum_social_event(load("eum_social_event_text.json"))
    assert msg.provider == "eum_social"
    assert msg.type == "text"
    assert msg.text.startswith("Mere uncle ko dil ka daura")
    assert msg.sender_phone == "+923001234567"
    assert msg.media_id is None
    assert msg.timestamp == "2026-10-11T11:00:42+00:00"


# ---------------------------------------------------------------------------
# Message types
# ---------------------------------------------------------------------------
def test_legacy_voice_type_is_audio():
    body = meta_body_with({
        "from": "923001234567", "id": "wamid.VOICE1", "timestamp": "1791716400",
        "type": "voice", "voice": {"id": "555", "mime_type": "audio/ogg; codecs=opus"},
    })
    [msg] = parse_meta_webhook(body)
    assert msg.type == "audio"
    assert msg.media_id == "555"


@pytest.mark.parametrize("message", [
    {"type": "document", "document": {"id": "777", "mime_type": "application/pdf", "filename": "report.pdf"}},
    {"type": "image", "image": {"mime_type": "image/jpeg"}},
    {"type": "location", "location": {"name": "no coordinates"}},
    {"type": "location", "location": {"latitude": "north", "longitude": 67.0}},
    {"type": "sticker", "sticker": {"id": "888", "mime_type": "image/webp"}},
    {"type": "reaction", "reaction": {"message_id": "wamid.X", "emoji": "👍"}},
    {"type": "unsupported", "errors": [{"code": 131051, "title": "Message type unknown"}]},
    {"type": "audio", "audio": {}},
])
def test_unsupported_types_are_kept_not_dropped(message):
    body = meta_body_with({"from": "923001234567", "id": "wamid.UNSUP", "timestamp": "1791716400", **message})
    [msg] = parse_meta_webhook(body)
    assert msg.type == "unsupported"
    assert msg.provider_message_id == "wamid.UNSUP"
    assert msg.sender_phone == "+923001234567"
    assert msg.text is None and msg.media_id is None


LOCATION_MESSAGE = {
    "from": "923001234567", "id": "wamid.LOC1", "timestamp": "1791716400", "type": "location",
    "location": {"latitude": 24.8607, "longitude": 67.0011, "name": "Saddar", "address": "Abdullah Haroon Rd, Karachi"},
}


def test_location_message_fields_identical_for_both_providers():
    [meta] = parse_meta_webhook(meta_body_with(dict(LOCATION_MESSAGE)))
    eum_event = load("eum_social_event_text.json")
    entry = json.loads(eum_event["whatsAppWebhookEntry"])
    entry["changes"][0]["value"]["messages"] = [dict(LOCATION_MESSAGE)]
    eum_event["whatsAppWebhookEntry"] = json.dumps(entry)
    [eum] = parse_eum_social_event(eum_event)

    assert meta.type == "location"
    assert (meta.latitude, meta.longitude) == (24.8607, 67.0011)
    assert (meta.location_name, meta.location_address) == ("Saddar", "Abdullah Haroon Rd, Karachi")
    assert meta.text is None and meta.media_id is None
    assert without_provider([meta]) == without_provider([eum])


def test_location_without_name_or_address():
    message = {**LOCATION_MESSAGE, "location": {"latitude": "24.86", "longitude": "67"}}
    [msg] = parse_meta_webhook(meta_body_with(message))
    assert msg.type == "location" and (msg.latitude, msg.longitude) == (24.86, 67.0)
    assert msg.location_name is None and msg.location_address is None


def test_statuses_only_yield_no_messages():
    assert parse_meta_webhook(load("meta_webhook_statuses.json")) == []
    assert parse_eum_social_event(load("eum_social_event_statuses.json")) == []
    assert parse_queue_message(raw("eum_social_event_statuses.json")) == []


def test_multiple_messages_and_entries():
    body = load("meta_webhook_text.json")
    second_entry = copy.deepcopy(load("meta_webhook_voice_note.json")["entry"][0])
    body["entry"].append(second_entry)
    value = body["entry"][0]["changes"][0]["value"]
    extra = copy.deepcopy(value["messages"][0])
    extra["id"] = "wamid.SECOND"
    value["messages"].append(extra)

    messages = parse_meta_webhook(body)
    assert [m.provider_message_id for m in messages][:2] == [value["messages"][0]["id"], "wamid.SECOND"]
    assert [m.type for m in messages] == ["text", "text", "audio"]


def test_sender_name_matched_by_wa_id_and_optional():
    msg = {"from": "923001234567", "id": "wamid.N", "timestamp": "1791716400", "type": "text", "text": {"body": "hi"}}
    contacts = [
        {"profile": {"name": "Someone Else"}, "wa_id": "15550001111"},
        {"profile": {"name": "Ahmed Khan"}, "wa_id": "923001234567"},
    ]
    assert parse_meta_webhook(meta_body_with(msg, contacts))[0].sender_name == "Ahmed Khan"
    assert parse_meta_webhook(meta_body_with(msg, []))[0].sender_name is None


def test_non_whatsapp_meta_object_ignored():
    assert parse_meta_webhook({"object": "page", "entry": [{"id": "1"}]}) == []
    assert parse_meta_webhook({}) == []


def test_malformed_message_without_id_skipped():
    body = meta_body_with({"from": "923001234567", "type": "text", "text": {"body": "x"}})
    assert parse_meta_webhook(body) == []


# ---------------------------------------------------------------------------
# Queue bodies
# ---------------------------------------------------------------------------
def test_queue_serialized_simulator_message_round_trip():
    original = InboundWhatsAppMessage(
        provider="simulator",
        provider_message_id="sim-123",
        sender_phone="+923009988776",
        timestamp="2026-10-11T11:00:00+00:00",
        type="audio",
        simulated_transcript="Mere uncle ko dil ka daura par raha hai",
        simulated_language="Urdu",
    )
    assert parse_queue_message(original.model_dump_json()) == [original]
    assert parse_queue_message(original.model_dump_json().encode("utf-8")) == [original]


def test_queue_raw_meta_webhook_body():
    from_queue = parse_queue_message(raw("meta_webhook_voice_note.json"))
    assert from_queue == parse_meta_webhook(load("meta_webhook_voice_note.json"))


def test_queue_sns_subscription_confirmation_ignored():
    body = json.dumps({
        "Type": "SubscriptionConfirmation",
        "MessageId": "x",
        "TopicArn": "arn:aws:sns:us-east-1:123456789012:resq-whatsapp-inbound-events",
        "Message": "You have chosen to subscribe to the topic ...",
        "SubscribeURL": "https://sns.us-east-1.amazonaws.com/?Action=ConfirmSubscription",
    })
    assert parse_queue_message(body) == []


@pytest.mark.parametrize("body", [
    "not json at all",
    json.dumps({"hello": "world"}),
    json.dumps(["a", "list"]),
    json.dumps({"provider": "meta", "provider_message_id": "x"}),  # invalid model
    json.dumps({"Type": "Notification", "TopicArn": "arn:x", "Message": json.dumps({"other": "event"})}),
])
def test_queue_unrecognized_bodies_raise(body):
    with pytest.raises(UnrecognizedMessageError):
        parse_queue_message(body)


def test_eum_event_timestamp_fallback_when_inner_missing():
    event = load("eum_social_event_text.json")
    entry = json.loads(event["whatsAppWebhookEntry"])
    del entry["changes"][0]["value"]["messages"][0]["timestamp"]
    event["whatsAppWebhookEntry"] = json.dumps(entry)
    [msg] = parse_eum_social_event(event)
    assert msg.timestamp == "2026-10-11T11:00:43.118402+00:00"


# ---------------------------------------------------------------------------
# X-Hub-Signature-256
# ---------------------------------------------------------------------------
def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_signature_skipped_when_app_secret_unset(monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_APP_SECRET", "")
    assert verify_meta_signature(b"{}", None) is True


def test_signature_good_and_bad(monkeypatch):
    secret = "meta-app-secret-123"
    monkeypatch.setattr(settings, "WHATSAPP_APP_SECRET", secret)
    body = raw("meta_webhook_voice_note.json").encode("utf-8")

    assert verify_meta_signature(body, _sign(secret, body)) is True
    assert verify_meta_signature(body, _sign(secret, body).upper().replace("SHA256=", "sha256=")) is True
    assert verify_meta_signature(body, _sign("wrong-secret", body)) is False
    assert verify_meta_signature(body + b" ", _sign(secret, body)) is False
    assert verify_meta_signature(body, None) is False
    assert verify_meta_signature(body, "") is False
    assert verify_meta_signature(body, _sign(secret, body).replace("sha256=", "sha1=")) is False
