"""Direct WhatsApp calls (app/direct_calls.py): while calls cannot ring in the portal, the patient asks
for a call and the assigned doctor calls them from their own WhatsApp through an Open chat & call link."""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

from app import direct_calls, doctor_chat, doctor_offers, pipeline, tasks
from app.config import settings
from app.main import app
from app.models.schemas import (DoctorProfile, EmergencyCase, InboundCallEvent, InboundWhatsAppMessage,
                                UserProfile)
from app.services.dynamo_service import DynamoDBService, create_resq_table, dynamo_service
from test_pipeline_support import FakeWhatsApp, new_phone, now_iso


@pytest.fixture
def wa(monkeypatch):
    fake = FakeWhatsApp()
    monkeypatch.setattr(doctor_offers, "whatsapp_service", fake)
    monkeypatch.setattr(pipeline, "whatsapp_service", fake)
    monkeypatch.setattr(doctor_chat, "whatsapp_service", fake)
    return fake


def doctor() -> DoctorProfile:
    return dynamo_service.register_doctor(DoctorProfile(
        doctor_id=f"DOC-DC-{uuid.uuid4().hex[:6]}", name="Dr. Direct", phone_number=new_phone(),
        license_no="L", specialty=f"Spec{uuid.uuid4().hex[:6]}", bio="b"))


def case(assigned: DoctorProfile = None, status: str = "DOCTOR_HANDOVER") -> EmergencyCase:
    return dynamo_service.save_case(EmergencyCase(
        case_id=f"CASE-{uuid.uuid4().hex[:10].upper()}", user_phone=new_phone(), patient_name="Ahmed",
        transcript="t", detected_language="Urdu", esi_level=2, severity="HIGH", status=status,
        assigned_doctor_id=assigned.doctor_id if assigned else None,
        assigned_doctor_name=assigned.name if assigned else None))


def sent_to(wa: FakeWhatsApp, phone: str):
    return [c for c in wa.calls if c[1] and c[1][0] == phone]


def token_of(url: str) -> str:
    return url.rsplit("/", 1)[1]


def button(phone: str, payload: str) -> InboundWhatsAppMessage:
    return InboundWhatsAppMessage(provider="meta", provider_message_id=f"wamid.{uuid.uuid4().hex}",
                                  sender_phone=phone, timestamp=now_iso(), type="button",
                                  button_payload=payload, button_text=direct_calls.REQUEST_CALL_TITLE)


# ---------------------------------------------------------------------------- accept
@pytest.mark.asyncio
async def test_accept_gives_the_patient_a_call_button_and_the_doctor_a_call_link(wa):
    doc = doctor()
    c = dynamo_service.save_case(case().model_copy(update={"matched_specialty": doc.specialty}))
    offered = await doctor_offers.start_offer(c)
    outcome, _ = await doctor_offers.accept_offer(c.case_id, offered.offer_id, doc)
    assert outcome == doctor_offers.ACCEPTED
    [(name, args, _)] = sent_to(wa, c.user_phone)
    assert name == "send_reply_buttons" and "accepted your case" in args[1]
    assert args[2] == [{"id": f"CALLME:{c.case_id}", "title": "Request a call"}]
    [link_msg] = [x for x in sent_to(wa, doc.phone_number) if x[0] == "send_case_link"]
    assert "/api/v1/direct-call/" in link_msg[1][1] and "will see your number" in link_msg[1][1]


# ---------------------------------------------------------------------------- patient asks for a call
@pytest.mark.asyncio
async def test_request_a_call_button_sends_the_doctor_an_open_chat_and_call_link(wa):
    doc = doctor()
    c = case(assigned=doc)
    res = await pipeline.process_inbound_message(button(c.user_phone, f"CALLME:{c.case_id}"))
    assert res["action"] == "call_requested"
    [(name, args, _)] = sent_to(wa, doc.phone_number)
    assert name == "send_case_link" and args[2] == "Open chat & call" and "/api/v1/direct-call/" in args[3]
    assert c.user_phone.lstrip("+") not in args[1]             # the patient's number is not in the message
    assert any("asked *Dr. Direct* to call you" in t for t in wa.texts())
    assert dynamo_service.get_case(c.case_id).call_requested_at
    assert any("asked for a call" in m.content for m in dynamo_service.get_case_messages(c.case_id))

    again = await pipeline.process_inbound_message(button(c.user_phone, f"CALLME:{c.case_id}"))
    assert again["action"] == "call_request_repeat"
    assert len(sent_to(wa, doc.phone_number)) == 1              # no second link for a double tap


@pytest.mark.asyncio
async def test_request_a_call_before_a_doctor_accepted_or_from_another_phone(wa):
    c = case()
    res = await pipeline.process_inbound_message(button(c.user_phone, f"CALLME:{c.case_id}"))
    assert res["action"] == "call_request_no_doctor" and "has not taken your case yet" in wa.texts()[-1]
    stranger = new_phone()
    res = await pipeline.process_inbound_message(button(stranger, f"CALLME:{c.case_id}"))
    assert res["action"] == "call_request_unknown"
    assert dynamo_service.get_case(c.case_id).call_requested_at is None


# ---------------------------------------------------------------------------- the link
@pytest.mark.asyncio
async def test_link_redirects_to_the_patient_chat_and_tells_the_patient_once(wa):
    doc = doctor()
    c = case(assigned=doc)
    url = await direct_calls.create_link(c, doc)
    assert url.startswith(f"{settings.PORTAL_BASE_URL.rstrip('/')}/api/v1/direct-call/")
    outcome, target = await direct_calls.use_link(token_of(url))
    assert outcome == direct_calls.OK and target == f"https://wa.me/{c.user_phone.lstrip('+')}"
    updated = dynamo_service.get_case(c.case_id)
    assert updated.direct_call_at and updated.direct_call_by == "Dr. Direct"
    assert sum("is calling you now on WhatsApp" in t for t in wa.texts()) == 1
    await direct_calls.use_link(token_of(url))                 # the doctor opens it again
    assert sum("is calling you now on WhatsApp" in t for t in wa.texts()) == 1


@pytest.mark.asyncio
async def test_dead_links(wa, monkeypatch):
    doc, other = doctor(), doctor()
    assert (await direct_calls.use_link("nope"))[0] == direct_calls.UNKNOWN
    c = case(assigned=doc)
    url = await direct_calls.create_link(c, doc)
    dynamo_service.update_case(c.case_id, {"assigned_doctor_id": other.doctor_id})
    assert (await direct_calls.use_link(token_of(url)))[0] == direct_calls.NOT_ASSIGNED
    closed = case(assigned=doc, status="RESOLVED")
    assert (await direct_calls.use_link(token_of(await direct_calls.create_link(closed, doc))))[0] == direct_calls.CLOSED
    monkeypatch.setattr(settings, "DIRECT_CALL_LINK_TTL_SECONDS", -1)
    expired = await direct_calls.create_link(case(assigned=doc), doc)
    assert (await direct_calls.use_link(token_of(expired)))[0] == direct_calls.EXPIRED
    assert not wa.texts()                                       # nobody was told anything


def test_link_routes_and_portal_button(wa):
    doc = doctor()
    c = case(assigned=doc)
    with TestClient(app) as client:
        res = client.post(f"/api/v1/cases/{c.case_id}/direct-call")
        assert res.status_code == 200 and res.json()["url"] == f"https://wa.me/{c.user_phone.lstrip('+')}"
        assert "portal" in dynamo_service.get_case_messages(c.case_id)[-2].content
        assert client.post(f"/api/v1/cases/{case().case_id}/direct-call").status_code == 409   # no doctor yet

        url = asyncio.run(direct_calls.create_link(c, doc))
        res = client.get(url.split(settings.PORTAL_BASE_URL.rstrip("/"))[1], follow_redirects=False)
        assert res.status_code == 302 and res.headers["location"].startswith("https://wa.me/")
        res = client.get("/api/v1/direct-call/unknown-token", follow_redirects=False)
        assert res.status_code == 404 and "not valid" in res.text


# ---------------------------------------------------------------------------- a WhatsApp call
@pytest.mark.asyncio
async def test_a_call_to_the_business_number_becomes_a_call_request_once(wa):
    doc = doctor()
    c = case(assigned=doc)
    user = dynamo_service.get_user(c.user_phone)
    assert user is None or user.active_case_id is None
    dynamo_service.save_user(UserProfile(phone_number=c.user_phone, name="Ahmed", active_case_id=c.case_id))
    event = InboundCallEvent(provider="eum_social", call_id=f"wacid.{uuid.uuid4().hex}", event="connect",
                             user_phone=c.user_phone, direction="USER_INITIATED", timestamp=now_iso(), sdp="v=0")
    res = await tasks.handle_call_event(event)
    assert res["status"] == "DONE" and res["action"] == "call_requested"
    [(name, args, _)] = sent_to(wa, doc.phone_number)
    assert name == "send_case_link" and "/api/v1/direct-call/" in args[3]
    assert any("tried to call" in m.content for m in dynamo_service.get_case_messages(c.case_id))
    assert (await tasks.handle_call_event(event))["status"] == "DUPLICATE"
    terminate = event.model_copy(update={"event": "terminate"})
    assert (await tasks.handle_call_event(terminate))["status"] == "IGNORED"


# ---------------------------------------------------------------------------- DynamoDB branch
def test_links_in_dynamodb(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setattr(settings, "MOCK_AWS", False)
    monkeypatch.setattr(settings, "AWS_ENDPOINT_URL", None)
    monkeypatch.setattr(settings, "SEED_DEMO_DATA", False)
    with mock_aws():
        create_resq_table(boto3.client("dynamodb", region_name=settings.AWS_REGION), settings.DYNAMODB_TABLE_NAME)
        svc = DynamoDBService()
        assert not svc.mock_mode
        expires = datetime.now(timezone.utc) + timedelta(hours=1)
        svc.save_direct_call_link("tok", {"case_id": "CASE-1", "doctor_id": "DOC-1"}, expires)
        assert svc.get_direct_call_link("tok") == {"case_id": "CASE-1", "doctor_id": "DOC-1",
                                                   "expires_at": expires.isoformat()}
        item = svc.table.get_item(Key={"PK": "DIRECTCALL#tok", "SK": "LINK"})["Item"]
        assert int(item["ttl"]) == int(expires.timestamp())
        assert svc.get_direct_call_link("other") is None
