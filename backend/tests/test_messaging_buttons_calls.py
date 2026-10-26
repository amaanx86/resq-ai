"""WA-0 parser tests: button taps and WhatsApp Calling API events, Meta and EUM Social shapes.

Fixtures (tests/fixtures/):
* meta_webhook_button_reply.json: template quick-reply tap (`type: "button"`).
* meta_webhook_interactive_reply.json: interactive reply-button tap (`interactive.button_reply`).
* meta_webhook_call_*.json: `field: "calls"` webhooks (connect with an SDP offer, terminate, and a
  `type: "call"` status), in the shape of Meta's Calling API docs. Replace them with payloads
  captured from a real phone in the CALL-0 spike.
The EUM Social variants wrap the same entry the way the event destination does
(`whatsAppWebhookEntry` = the entry as a JSON string), also inside an SNS envelope.
"""
import json
from pathlib import Path

import pytest

from app.messaging.inbound import parse_eum_social_event, parse_meta_webhook, parse_queue_message
from app.models.schemas import InboundCallEvent, InboundWhatsAppMessage, InternalTask

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def eum_event(meta_body: dict) -> dict:
    return {
        "context": {"MetaWabaIds": [{"wabaId": "102290129340398"}],
                    "MetaPhoneNumberIds": [{"metaPhoneNumberId": "106540352242922"}]},
        "whatsAppWebhookEntry": json.dumps(meta_body["entry"][0]),
        "aws_account_id": "123456789012",
        "message_timestamp": "2026-10-15T09:00:00.123456789Z",
        "messageId": "5f0c2b1e-0000-4000-8000-000000000042",
    }


def sns_envelope(event: dict) -> dict:
    return {"Type": "Notification", "MessageId": "b1d0c7a0-0000-4000-8000-000000000009",
            "TopicArn": "arn:aws:sns:us-east-1:123456789012:resq-whatsapp-inbound-events",
            "Message": json.dumps(event)}


def all_shapes(fixture: str):
    """The same webhook through every path it can take into the worker."""
    meta = load(fixture)
    event = eum_event(meta)
    return {
        "meta webhook": parse_meta_webhook(meta),
        "queue: raw meta body": parse_queue_message(json.dumps(meta)),
        "eum event": parse_eum_social_event(event),
        "queue: eum raw delivery": parse_queue_message(json.dumps(event)),
        "queue: eum in sns envelope": parse_queue_message(json.dumps(sns_envelope(event))),
    }


def without_provider(items):
    return [i.model_dump(exclude={"provider"}) for i in items]


# ---------------------------------------------------------------------------- buttons
@pytest.mark.parametrize("fixture, payload, text", [
    ("meta_webhook_button_reply.json", "OFFER:ACCEPT:CASE-1A2B3C4D5E:OFR-0001", "Accept"),
    ("meta_webhook_interactive_reply.json", "OFFER:DECLINE:CASE-1A2B3C4D5E:OFR-0001", "Decline"),
])
def test_button_taps_parse_identically_from_every_shape(fixture, payload, text):
    shapes = all_shapes(fixture)
    reference = shapes["meta webhook"]
    assert len(reference) == 1
    msg = reference[0]
    assert isinstance(msg, InboundWhatsAppMessage)
    assert msg.type == "button" and msg.button_payload == payload and msg.button_text == text
    assert msg.sender_phone == "+923001234567" and msg.sender_name == "Ahmed Khan"
    assert msg.provider_message_id.startswith("wamid.")
    for name, parsed in shapes.items():
        assert without_provider(parsed) == without_provider(reference), name
    assert shapes["eum event"][0].provider == "eum_social"


def test_button_tap_round_trips_through_the_queue_body():
    msg = parse_meta_webhook(load("meta_webhook_button_reply.json"))[0]
    assert parse_queue_message(msg.model_dump_json()) == [msg]


def test_list_reply_is_a_button_tap_and_other_interactive_types_are_unsupported():
    body = load("meta_webhook_interactive_reply.json")
    message = body["entry"][0]["changes"][0]["value"]["messages"][0]
    message["interactive"] = {"type": "list_reply", "list_reply": {"id": "ROW-1", "title": "Chest pain"}}
    msg = parse_meta_webhook(body)[0]
    assert (msg.type, msg.button_payload, msg.button_text) == ("button", "ROW-1", "Chest pain")

    message["interactive"] = {"type": "nfm_reply", "nfm_reply": {"response_json": "{}"}}
    assert parse_meta_webhook(body)[0].type == "unsupported"


def test_empty_button_is_unsupported():
    body = load("meta_webhook_button_reply.json")
    body["entry"][0]["changes"][0]["value"]["messages"][0]["button"] = {}
    assert parse_meta_webhook(body)[0].type == "unsupported"


# ---------------------------------------------------------------------------- calls
def test_call_connect_carries_the_sdp_offer_from_every_shape():
    shapes = all_shapes("meta_webhook_call_connect.json")
    reference = shapes["meta webhook"]
    assert len(reference) == 1
    call = reference[0]
    assert isinstance(call, InboundCallEvent)
    assert call.event == "connect" and call.direction == "USER_INITIATED"
    assert call.call_id.startswith("wacid.")
    assert call.user_phone == "+923001234567" and call.user_name == "Ahmed Khan"
    assert call.business_phone == "+15550783881" and call.phone_number_id == "106540352242922"
    assert call.sdp_type == "offer" and call.sdp.startswith("v=0\r\n") and "opus" in call.sdp
    assert call.timestamp.startswith("2026-")
    for name, parsed in shapes.items():
        assert without_provider(parsed) == without_provider(reference), name
    assert shapes["eum event"][0].provider == "eum_social"


def test_call_terminate_has_status_duration_and_callback_data():
    call = parse_meta_webhook(load("meta_webhook_call_terminate.json"))[0]
    assert isinstance(call, InboundCallEvent)
    assert (call.event, call.status, call.duration_seconds) == ("terminate", "Completed", 90)
    assert call.biz_opaque_callback_data == "CASE-1A2B3C4D5E"
    assert call.start_time < call.end_time and call.sdp is None


def test_call_status_is_a_call_event_but_message_receipts_are_not():
    call = parse_meta_webhook(load("meta_webhook_call_status.json"))[0]
    assert isinstance(call, InboundCallEvent)
    assert (call.event, call.status, call.direction) == ("status", "RINGING", "BUSINESS_INITIATED")
    assert call.user_phone == "+923001234567"
    # Plain delivery receipts still yield nothing.
    assert parse_meta_webhook(load("meta_webhook_statuses.json")) == []
    assert parse_eum_social_event(load("eum_social_event_statuses.json")) == []


def test_business_initiated_connect_takes_the_user_from_to():
    body = load("meta_webhook_call_connect.json")
    call = body["entry"][0]["changes"][0]["value"]["calls"][0]
    call.update({"direction": "BUSINESS_INITIATED", "from": "15550783881", "to": "923001234567",
                 "session": {"sdp_type": "answer", "sdp": "v=0\r\n"}})
    parsed = parse_meta_webhook(body)[0]
    assert parsed.user_phone == "+923001234567" and parsed.business_phone == "+15550783881"
    assert parsed.sdp_type == "answer"


def test_call_errors_and_unknown_events():
    body = load("meta_webhook_call_terminate.json")
    call = body["entry"][0]["changes"][0]["value"]["calls"][0]
    call.update({"status": "Failed", "errors": [{"code": 138021, "title": "Call failed", "message": "ICE timeout"}]})
    assert parse_meta_webhook(body)[0].error == "138021 Call failed ICE timeout"

    call["event"] = "something_new"
    assert parse_meta_webhook(body) == []


def test_call_event_round_trips_through_the_queue_body():
    call = parse_meta_webhook(load("meta_webhook_call_connect.json"))[0]
    body = call.model_dump_json()
    assert json.loads(body)["kind"] == "call_event"
    assert parse_queue_message(body) == [call]


def test_internal_task_round_trips_through_the_queue_body():
    task = InternalTask(task_id="t1", kind="doctor_offer_timeout", payload={"case_id": "CASE-1"},
                        run_at="2026-10-15T09:00:45+00:00")
    assert parse_queue_message(task.model_dump_json()) == [task]
