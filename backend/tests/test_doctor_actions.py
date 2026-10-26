"""WA-0: doctor lookup by WhatsApp number (in-memory and DynamoDB branches) and the pipeline
routing doctor button taps (`OFFER:*` / `CALL:*`) before the patient flow."""
import uuid

import boto3
import pytest
from moto import mock_aws

from app import pipeline
from app.config import settings
from app.models.schemas import DoctorProfile, InboundWhatsAppMessage
from app.services.dynamo_service import DynamoDBService, create_resq_table, dynamo_service, phone_key
from test_pipeline_support import fakes, new_phone, now_iso  # noqa: F401


def _doctor(phone: str, doctor_id: str = None) -> DoctorProfile:
    return DoctorProfile(doctor_id=doctor_id or f"DOC-TEST-{uuid.uuid4().hex[:6]}", name="Dr. Test",
                         phone_number=phone, license_no="L-1", specialty="Cardiology", bio="b")


def _tap(phone: str, payload: str, text: str = "Accept") -> InboundWhatsAppMessage:
    return InboundWhatsAppMessage(provider="meta", provider_message_id=f"wamid.{uuid.uuid4().hex}",
                                  sender_phone=phone, timestamp=now_iso(), type="button",
                                  button_payload=payload, button_text=text)


def test_phone_key_normalizes():
    assert phone_key("+92 300-123 4567") == phone_key("923001234567") == "+923001234567"
    assert phone_key("") == phone_key(None) == ""


# ---------------------------------------------------------------------------- in-memory branch
def test_doctor_by_phone_in_memory():
    phone = new_phone()
    doc = dynamo_service.register_doctor(_doctor(phone))
    assert dynamo_service.get_doctor_by_phone(phone.lstrip("+")) == doc
    assert dynamo_service.get_doctor_by_phone(new_phone()) is None
    # Seeded demo doctors are found too.
    assert dynamo_service.get_doctor_by_phone("+923001234567").doctor_id == "DOC-CARDIO-01"


def test_stale_pointer_never_matches_in_memory():
    old, new = new_phone(), new_phone()
    doc = dynamo_service.register_doctor(_doctor(old))
    dynamo_service.register_doctor(doc.model_copy(update={"phone_number": new}))
    assert dynamo_service.get_doctor_by_phone(old) is None
    assert dynamo_service.get_doctor_by_phone(new).doctor_id == doc.doctor_id


# ---------------------------------------------------------------------------- DynamoDB branch
@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setattr(settings, "MOCK_AWS", False)
    monkeypatch.setattr(settings, "AWS_ENDPOINT_URL", None)
    monkeypatch.setattr(settings, "SEED_DEMO_DATA", False)
    with mock_aws():
        create_resq_table(boto3.client("dynamodb", region_name=settings.AWS_REGION), settings.DYNAMODB_TABLE_NAME)
        svc = DynamoDBService()
        assert not svc.mock_mode
        yield svc


def _pointer(svc: DynamoDBService, phone: str):
    return svc.table.get_item(Key={"PK": f"DOCTORPHONE#{phone_key(phone)}", "SK": "PROFILE"}).get("Item")


def test_doctor_by_phone_in_dynamodb_uses_the_pointer(db):
    phone = "+92 321 5550001"
    doc = db.register_doctor(_doctor(phone))
    assert _pointer(db, phone)["doctor_id"] == doc.doctor_id
    other_task = DynamoDBService()                        # another process: empty in-memory mirror
    assert other_task.get_doctor_by_phone("923215550001") == doc
    assert other_task.get_doctor_by_phone("+923215550002") is None


def test_doctor_saved_before_the_pointer_is_found_and_backfilled(db):
    doc = _doctor("+923215550003")
    db.table.put_item(Item=db._doctor_item(doc))          # written by older code: no pointer
    assert _pointer(db, doc.phone_number) is None
    assert DynamoDBService().get_doctor_by_phone(doc.phone_number) == doc
    assert _pointer(db, doc.phone_number)["doctor_id"] == doc.doctor_id


def test_stale_pointer_never_matches_in_dynamodb(db):
    old, new = "+923215550004", "+923215550005"
    doc = db.register_doctor(_doctor(old))
    db.register_doctor(doc.model_copy(update={"phone_number": new}))
    fresh = DynamoDBService()
    assert fresh.get_doctor_by_phone(old) is None
    assert fresh.get_doctor_by_phone(new).doctor_id == doc.doctor_id


def test_seeding_writes_doctor_pointers(db, monkeypatch):
    monkeypatch.setattr(settings, "SEED_DEMO_DATA", True)
    DynamoDBService()
    assert _pointer(db, "+923001234567")["doctor_id"] == "DOC-CARDIO-01"
    # The pointer items are not doctors, users or cases.
    assert {d.doctor_id for d in db.list_doctors()} == {"DOC-CARDIO-01", "DOC-NEURO-02", "DOC-TRAUMA-03", "DOC-PULMO-04"}


# ---------------------------------------------------------------------------- pipeline routing
def test_doctor_action_prefix():
    assert pipeline.doctor_action_prefix("OFFER:ACCEPT:CASE-1:OFR-1") == "OFFER"
    assert pipeline.doctor_action_prefix("CALL:DIRECT:CASE-1") == "CALL"
    assert pipeline.doctor_action_prefix("OFFER") is None
    assert pipeline.doctor_action_prefix("ROW-1") is None
    assert pipeline.doctor_action_prefix(None) is None


@pytest.mark.asyncio
async def test_doctor_tap_goes_to_its_handler_without_a_user_or_case(fakes, monkeypatch):
    phone = new_phone()
    doc = dynamo_service.register_doctor(_doctor(phone))
    calls = []

    async def offer_handler(msg, doctor, parts):
        calls.append((doctor.doctor_id, parts))
        return {"action": "offer_accepted", "case_id": parts[1]}

    monkeypatch.setitem(pipeline.DOCTOR_ACTIONS, "OFFER", offer_handler)
    res = await pipeline.process_inbound_message(_tap(phone, "OFFER:ACCEPT:CASE-1:OFR-1"))
    assert res["status"] == "PROCESSED" and res["action"] == "offer_accepted" and res["case_id"] == "CASE-1"
    assert calls == [(doc.doctor_id, ["ACCEPT", "CASE-1", "OFR-1"])]
    assert dynamo_service.get_user(phone) is None         # no patient profile for the doctor
    assert fakes.triage.calls == [] and fakes.whatsapp.names() == []


@pytest.mark.asyncio
async def test_doctor_tap_without_a_handler_gets_a_short_reply(fakes):
    phone = new_phone()
    dynamo_service.register_doctor(_doctor(phone))
    res = await pipeline.process_inbound_message(_tap(phone, "CALL:DIRECT:CASE-1"))
    assert res["action"] == "doctor_action_unhandled"
    assert fakes.whatsapp.names() == ["send_text_message"]
    assert "no longer active" in fakes.whatsapp.texts()[0]
    assert dynamo_service.get_user(phone) is None


@pytest.mark.asyncio
async def test_action_payload_from_a_non_doctor_is_plain_text(fakes, monkeypatch):
    async def offer_handler(msg, doctor, parts):
        raise AssertionError("not a doctor")

    monkeypatch.setitem(pipeline.DOCTOR_ACTIONS, "OFFER", offer_handler)
    phone = new_phone()
    res = await pipeline.process_inbound_message(_tap(phone, "OFFER:ACCEPT:CASE-1:OFR-1", text="hello"))
    assert res["action"] == "greeting"                    # "hello" was treated as typed text
    assert dynamo_service.get_user(phone) is not None


@pytest.mark.asyncio
async def test_patient_button_tap_counts_as_typing_its_title(fakes):
    phone = new_phone()
    res = await pipeline.process_inbound_message(
        _tap(phone, "ROW-7", text="Mere abbu ko dil ka daura par raha hai"))
    assert res["action"] not in ("unsupported", "greeting") and res["case_id"]
    assert fakes.triage.calls and "dil ka daura" in str(fakes.triage.calls[0])
