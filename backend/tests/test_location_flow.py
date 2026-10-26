"""Location sharing instead of automatic dispatch: ESI 1-2 cases ask the patient for their location,
a shared location goes on the case, to the doctor's chat and (emergencies only) to the emergency
contact as a WhatsApp template, and the doctor records "ambulance called"."""
import uuid

import pytest
from fastapi.testclient import TestClient

from app import pipeline
from app.main import app
from app.models.schemas import InboundWhatsAppMessage, UserProfile
from app import case_messages
from app.routers import cases as cases_router
from app.services.dynamo_service import dynamo_service
from test_pipeline_support import FakeWhatsApp, accept_as, fakes, inbound, new_phone, now_iso  # noqa: F401

CONTACT = "+923331234567"


def registered(contact_phone=CONTACT) -> str:
    phone = new_phone()
    dynamo_service.save_user(UserProfile(phone_number=phone, name="Ahmed Khan", emergency_contact_name="Brother Ali",
                                         emergency_contact_phone=contact_phone))
    return phone


def location(phone, lat=24.8607, lng=67.0011, name="Saddar", address="Abdullah Haroon Rd, Karachi",
             message_id=None) -> InboundWhatsAppMessage:
    return InboundWhatsAppMessage(
        provider="meta", provider_message_id=message_id or f"wamid.{uuid.uuid4().hex}", sender_phone=phone,
        timestamp=now_iso(), type="location", latitude=lat, longitude=lng, location_name=name,
        location_address=address)


def sent(fakes, method):
    return [c for c in fakes.whatsapp.calls if c[0] == method]


async def emergency_case(fakes, phone, esi=1):
    fakes.triage.esi = esi
    res = await pipeline.process_inbound_message(inbound(phone, type="audio", transcript="dil ka daura"))
    return res["case_id"]


# ---------------------------------------------------------------- asking for the location
@pytest.mark.asyncio
async def test_esi_1_2_reply_has_no_ambulance_claim_and_asks_for_the_location(fakes):
    phone = registered()
    case_id = await emergency_case(fakes, phone)
    reply = fakes.whatsapp.texts()[0]
    assert "EMERGENCY DETECTED" in reply and "arranges the ambulance" in reply
    assert "dispatched" not in reply.lower()
    [request] = sent(fakes, "send_location_request")
    assert request[1][0] == phone and "Share your location" in request[1][1]
    # Sent right after the reply, and stored in the chat after it.
    names = [c[0] for c in fakes.whatsapp.calls]
    assert names.index("send_text_message") < names.index("send_location_request")
    case = dynamo_service.get_case(case_id)
    assert case.location_requested_at and case.dispatched_at
    bots = sorted((m for m in dynamo_service.get_case_messages(case_id) if m.sender == "BOT"), key=lambda m: m.timestamp)
    # The offer note (OFFER-1) is written at about the same time, so look the two messages up.
    reply_msg = next(m for m in bots if "EMERGENCY DETECTED" in m.content)
    button_msg = next(m for m in bots if "[Button: Send location]" in m.content)
    assert reply_msg.timestamp < button_msg.timestamp


@pytest.mark.asyncio
async def test_no_doctor_free_tells_the_patient_to_wait_and_call_if_worse(fakes, monkeypatch):
    from app.agents import tools
    monkeypatch.setattr(tools, "rank_candidates", lambda *a, **k: [])   # nobody on duty
    case_id = await emergency_case(fakes, registered())
    reply = fakes.whatsapp.texts()[0]
    assert "no doctor is free right now" in reply and "Stay on this chat" in reply
    assert "If it gets worse, call 1122" in reply
    case = dynamo_service.get_case(case_id)
    assert case.doctor_offer_status == "EXHAUSTED" and case.no_doctor_reason == "NO_DOCTOR_FREE"


# ---------------------------------------------------------------- the patient shares it
@pytest.mark.asyncio
async def test_shared_location_goes_to_the_case_the_doctor_and_the_emergency_contact(fakes):
    phone = registered()
    case_id = await emergency_case(fakes, phone)
    doctor = (await accept_as(case_id)).assigned_doctor_name
    res = await pipeline.process_inbound_message(location(phone))
    assert res["action"] == "location_shared" and res["case_id"] == case_id and res["contact_alert"] == "SENT"

    case = dynamo_service.get_case(case_id)
    assert (case.location_latitude, case.location_longitude) == (24.8607, 67.0011)
    assert case.location_name == "Saddar" and case.location_address == "Abdullah Haroon Rd, Karachi"
    assert case.location_shared_at and case.location_alert_status == "SENT"

    link = "https://maps.google.com/?q=24.860700,67.001100"
    [alert] = sent(fakes, "send_emergency_location_alert")
    assert alert[1] == (CONTACT, "Ahmed Khan", link)
    msgs = dynamo_service.get_case_messages(case_id)
    assert any(m.sender == "PATIENT" and link in m.content and "Saddar" in m.content for m in msgs)
    assert any(m.sender_name == "System" and "Location sent to Brother Ali" in m.content for m in msgs)
    assert f"shared with {doctor} and Brother Ali" in fakes.whatsapp.texts()[-1]
    assert any(e["type"] == "case_upserted" and e["case"].location_shared_at for e in fakes.events)
    assert len(fakes.triage.calls) == 1  # a location is never triaged


@pytest.mark.asyncio
async def test_duplicate_location_delivery_alerts_the_contact_once(fakes):
    phone = registered()
    await emergency_case(fakes, phone)
    msg = location(phone)
    await pipeline.process_inbound_message(msg)
    again = await pipeline.process_inbound_message(msg)
    assert again["status"] == "DUPLICATE"
    assert len(sent(fakes, "send_emergency_location_alert")) == 1


@pytest.mark.asyncio
async def test_a_newer_location_replaces_the_old_one(fakes):
    phone = registered()
    case_id = await emergency_case(fakes, phone)
    await pipeline.process_inbound_message(location(phone))
    await pipeline.process_inbound_message(location(phone, lat=24.9, lng=67.1, name=None, address=None))
    case = dynamo_service.get_case(case_id)
    assert (case.location_latitude, case.location_longitude, case.location_name) == (24.9, 67.1, None)
    assert len(sent(fakes, "send_emergency_location_alert")) == 2


@pytest.mark.asyncio
async def test_failed_template_send_is_flagged_for_the_doctor(fakes):
    async def rejected(*args):
        fakes.whatsapp.calls.append(("send_emergency_location_alert", args, {}))
        return {"status": "ERROR", "detail": "template not approved"}

    fakes.whatsapp.send_emergency_location_alert = rejected
    phone = registered()
    case_id = await emergency_case(fakes, phone)
    res = await pipeline.process_inbound_message(location(phone))
    assert res["contact_alert"] == "FAILED"
    assert dynamo_service.get_case(case_id).location_alert_status == "FAILED"
    note = [m for m in dynamo_service.get_case_messages(case_id) if m.sender_name == "System"][-1]
    assert "Could not send the location" in note.content and "template not approved" in note.content
    assert "Brother Ali" not in fakes.whatsapp.texts()[-1]


@pytest.mark.asyncio
async def test_no_emergency_contact_skips_the_alert(fakes):
    phone = new_phone()  # unregistered: no contact
    case_id = await emergency_case(fakes, phone)
    res = await pipeline.process_inbound_message(location(phone))
    assert res["contact_alert"] == "SKIPPED_NO_CONTACT"
    assert sent(fakes, "send_emergency_location_alert") == []
    assert dynamo_service.get_case(case_id).location_shared_at


@pytest.mark.asyncio
async def test_location_on_an_esi3_case_reaches_the_doctor_only(fakes):
    phone = registered()
    case_id = await emergency_case(fakes, phone, esi=3)
    assert sent(fakes, "send_location_request") == []
    res = await pipeline.process_inbound_message(location(phone))
    assert res["action"] == "location_shared" and res["contact_alert"] is None
    case = dynamo_service.get_case(case_id)
    assert case.location_shared_at and case.location_alert_status is None
    assert sent(fakes, "send_emergency_location_alert") == []
    assert len(fakes.triage.calls) == 1  # a location is never triaged


@pytest.mark.asyncio
async def test_location_without_an_open_case_gets_a_reply_and_no_case(fakes):
    phone = registered()
    res = await pipeline.process_inbound_message(location(phone))
    assert res["action"] == "location_no_case" and res["case_id"] is None
    assert "no open ResQ-AI case" in fakes.whatsapp.texts()[-1]
    assert [c for c in dynamo_service.list_all_cases() if c.user_phone == phone] == []


# ---------------------------------------------------------------- the doctor calls the ambulance
@pytest.mark.asyncio
async def test_mark_ambulance_called_records_it_and_tells_the_patient_once(fakes):
    phone = registered()
    case_id = await emergency_case(fakes, phone)
    doctor = (await accept_as(case_id)).assigned_doctor_name
    await pipeline.process_inbound_message(location(phone))
    texts_before = len(fakes.whatsapp.texts())

    case = await pipeline.mark_ambulance_called(case_id, called_by="doctor@hospital.org")
    assert case.ambulance_called_at and case.ambulance_called_by == "doctor@hospital.org"
    [text] = fakes.whatsapp.texts()[texts_before:]
    assert f"{doctor} has called an ambulance to the location you shared" in text
    assert any(m.sender == "BOT" and "Ambulance called" in m.content
               for m in dynamo_service.get_case_messages(case_id))
    again = await pipeline.mark_ambulance_called(case_id, called_by="someone-else")
    assert again.ambulance_called_by == "doctor@hospital.org"
    assert len(fakes.whatsapp.texts()) == texts_before + 1


@pytest.mark.asyncio
async def test_ambulance_called_before_any_location_asks_for_it_again(fakes):
    phone = registered()
    case_id = await emergency_case(fakes, phone)
    await pipeline.mark_ambulance_called(case_id, called_by="doctor@hospital.org")
    assert "called an ambulance for you" in fakes.whatsapp.texts()[-1]
    assert len(sent(fakes, "send_location_request")) == 2


def test_ambulance_called_endpoint(fakes, monkeypatch):
    _wa = FakeWhatsApp()
    monkeypatch.setattr(cases_router, "whatsapp_service", _wa)
    monkeypatch.setattr(case_messages, "whatsapp_service", _wa)  # the portal send code
    with TestClient(app) as client:
        phone = new_phone()
        case = client.post("/api/v1/webhook/simulate-voice", json={"user_phone": phone, "voice_text": "dil"}).json()["case"]
        r = client.post(f"/api/v1/cases/{case['case_id']}/ambulance-called")
        assert r.status_code == 200 and r.json()["ambulance_called_at"]
        assert r.json()["ambulance_called_by"] == "local@resq.dev"
        assert client.post("/api/v1/cases/CASE-NOPE/ambulance-called").status_code == 404

        other = client.post("/api/v1/webhook/simulate-voice", json={"user_phone": new_phone(), "voice_text": "dil"}).json()["case"]
        client.post(f"/api/v1/cases/{other['case_id']}/resolve")
        assert client.post(f"/api/v1/cases/{other['case_id']}/ambulance-called").status_code == 409
