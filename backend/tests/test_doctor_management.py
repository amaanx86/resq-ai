"""Administrators manage doctors and cases, and doctors confirm their WhatsApp number:

- DOC-8 (app/doctor_verification.py): a newly registered doctor (or a changed number) confirms the
  number with the template's Yes / No quick replies before it gets offers or patients' messages.
- PATCH / DELETE /api/v1/doctors/{id}: edit and unregister a doctor (app/routers/doctors.py).
- DELETE /api/v1/cases/{id}: delete a closed case, its chat, media and analytics (app/routers/cases.py).
"""
import uuid

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

from app import doctor_offers, doctor_verification, pipeline
from app.config import settings
from app.main import app
from app.models.schemas import ChatMessage, DoctorProfile, EmergencyCase, InboundWhatsAppMessage
from app.routers import doctors as doctors_router
from app.services import cognito_service as cognito
from app.services import whatsapp_service as whatsapp_module
from app.services.audio_service import audio_service
from app.services.dynamo_service import dynamo_service
from test_auth_portal import auth_on, bearer, make_token  # noqa: F401  (auth_on is a fixture)
from test_doctor_offers import Offers, get
from test_pipeline_support import FakeWhatsApp, fakes, inbound, new_phone, now_iso  # noqa: F401 (fixture)


def tap(phone, payload, title="Yes, it's me"):
    return InboundWhatsAppMessage(provider="meta", provider_message_id=f"wamid.{uuid.uuid4().hex}",
                                  sender_phone=phone, timestamp=now_iso(), type="button",
                                  button_payload=payload, button_text=title)


def registration(phone=None, specialty="Cardiology"):
    return {"name": "Dr. Confirm", "phone_number": phone or new_phone(), "license_no": "PMC-1",
            "specialty": specialty, "bio": "", "email": f"dr.{uuid.uuid4().hex[:8]}@example.com"}


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def wa(monkeypatch):
    fake = FakeWhatsApp()
    monkeypatch.setattr(doctor_verification, "whatsapp_service", fake)
    return fake


@pytest.fixture
def only_new_doctors(monkeypatch):
    """No demo doctors: a waiting case can only go to the doctors a test registers."""
    monkeypatch.setattr(dynamo_service, "_mock_doctors", {})
    monkeypatch.setattr(dynamo_service, "_mock_doctor_phones", {})
    return Offers(monkeypatch)


def sent(wa, name):
    return [c for c in wa.calls if c[0] == name]


# ============================================================================ DOC-8: number confirmation
def test_registering_asks_the_doctor_to_confirm(client, wa):
    phone = new_phone()
    doc = client.post("/api/v1/doctors/register", json=registration(phone)).json()
    assert doc["phone_status"] == doctor_verification.PENDING
    asks = sent(wa, "send_doctor_number_confirmation")
    assert asks == [("send_doctor_number_confirmation",
                     (phone, "Dr. Confirm", f"DOCV:YES:{doc['doctor_id']}", f"DOCV:NO:{doc['doctor_id']}"), {})]
    saved = dynamo_service.get_doctor(doc["doctor_id"])
    assert saved.phone_confirmation_sent_at and saved.phone_confirmation_status == "SENT"


def test_a_failed_request_is_recorded(client, monkeypatch):
    class Failing(FakeWhatsApp):
        async def send_doctor_number_confirmation(self, *args):
            return {"status": "ERROR", "detail": "template not approved"}
    monkeypatch.setattr(doctor_verification, "whatsapp_service", Failing())
    doc = client.post("/api/v1/doctors/register", json=registration()).json()
    assert dynamo_service.get_doctor(doc["doctor_id"]).phone_confirmation_status == "FAILED"


@pytest.mark.asyncio
async def test_template_falls_back_to_reply_buttons(monkeypatch):
    calls = []

    class Provider:
        async def send_template(self, *args, **kwargs):
            calls.append(("template", args, kwargs))
            return {"status": "ERROR", "detail": "132001 template does not exist"}

        async def send_reply_buttons(self, to_phone, body, buttons, header=None, footer=None):
            calls.append(("buttons", to_phone, buttons))
            return {"status": "SENT"}

    monkeypatch.setattr(whatsapp_module.whatsapp_service, "provider", Provider())
    res = await whatsapp_module.whatsapp_service.send_doctor_number_confirmation("+923001112223", "Dr. X", "Y", "N")
    assert calls[0][1][1] == settings.WHATSAPP_DOCTOR_CONFIRM_TEMPLATE_NAME
    assert calls[0][2]["quick_reply_payloads"] == ["Y", "N"]
    assert calls[1] == ("buttons", "+923001112223", [{"id": "Y", "title": "Yes, it's me"}, {"id": "N", "title": "No, not me"}])
    assert res["channel"] == "interactive" and "132001" in res["template_error"]


@pytest.mark.asyncio
async def test_yes_confirms_and_offers_the_waiting_case(only_new_doctors, wa):
    offers = only_new_doctors
    case = await doctor_offers.start_offer(offers.case(esi=1))
    assert case.doctor_offer_status == "EXHAUSTED"
    phone = new_phone()
    doctor = dynamo_service.register_doctor(DoctorProfile(
        doctor_id="DOC-NEW-1", name="Dr. New", phone_number=phone, license_no="L", specialty=offers.specialty,
        bio="", phone_status=doctor_verification.PENDING))
    await doctor_offers.offer_waiting_cases()
    assert get(case.case_id).offered_doctor_ids == []                         # pending: never offered
    res = await pipeline.process_inbound_message(tap(phone, f"DOCV:YES:{doctor.doctor_id}"))
    assert res["action"] == "doctor_number_confirmed"
    me = dynamo_service.get_doctor(doctor.doctor_id)
    assert me.phone_status == doctor_verification.CONFIRMED and me.phone_confirmed_at
    assert get(case.case_id).offered_doctor_ids == [doctor.doctor_id]         # offered right away
    assert any("number is confirmed" in c[1][1] for c in sent(wa, "send_text_message"))
    again = await pipeline.process_inbound_message(tap(phone, f"DOCV:YES:{doctor.doctor_id}"))
    assert again["action"] == "doctor_number_confirmed"                       # idempotent
    assert dynamo_service.get_doctor(doctor.doctor_id).phone_confirmed_at == me.phone_confirmed_at


@pytest.mark.asyncio
async def test_no_declines_and_the_number_is_a_strangers(fakes):
    phone = new_phone()
    doctor = dynamo_service.register_doctor(DoctorProfile(
        doctor_id="DOC-NEW-2", name="Dr. Typo", phone_number=phone, license_no="L", specialty="Cardiology",
        bio="", phone_status=doctor_verification.PENDING))
    res = await pipeline.process_inbound_message(tap(phone, f"DOCV:NO:{doctor.doctor_id}", "No, not me"))
    assert res["action"] == "doctor_number_declined"
    assert dynamo_service.get_doctor(doctor.doctor_id).phone_status == doctor_verification.DECLINED
    assert any("will not get ResQ-AI case messages" in t for t in fakes.offer_whatsapp.texts())
    # Their next message is a patient's: they get the patient flow, nothing about doctors.
    fakes.triage.esi = 5
    res = await pipeline.process_inbound_message(inbound(phone, text="mujhe halka sar dard hai"))
    assert not str(res.get("action", "")).startswith("doctor_")
    assert dynamo_service.get_user(phone) is not None
    # Changing their mind on the old message still works.
    res = await pipeline.process_inbound_message(tap(phone, f"DOCV:YES:{doctor.doctor_id}"))
    assert res["action"] == "doctor_number_confirmed"


@pytest.mark.asyncio
async def test_a_pending_doctor_is_asked_again_and_opens_no_case(fakes):
    phone = new_phone()
    doctor = dynamo_service.register_doctor(DoctorProfile(
        doctor_id="DOC-NEW-3", name="Dr. Pending", phone_number=phone, license_no="L", specialty="Cardiology",
        bio="", phone_status=doctor_verification.PENDING))
    res = await pipeline.process_inbound_message(inbound(phone, text="hello?"))
    assert (res["action"], res["case_id"], res["doctor_id"]) == ("doctor_number_unconfirmed", None, doctor.doctor_id)
    buttons = [c for c in fakes.offer_whatsapp.calls if c[0] == "send_reply_buttons"]
    assert buttons and [b["id"] for b in buttons[-1][1][2]] == [f"DOCV:YES:{doctor.doctor_id}", f"DOCV:NO:{doctor.doctor_id}"]
    assert dynamo_service.get_user(phone) is None
    # Any other doctor button (an old offer) is not acted on either.
    res = await pipeline.process_inbound_message(tap(phone, "OFFER:ACCEPT:CASE-X:OFR-X", "Accept"))
    assert res["action"] == "doctor_number_unconfirmed"


@pytest.mark.asyncio
async def test_a_button_for_another_doctor_is_stale(fakes):
    phone = new_phone()
    dynamo_service.register_doctor(DoctorProfile(
        doctor_id="DOC-NEW-4", name="Dr. Four", phone_number=phone, license_no="L", specialty="Cardiology",
        bio="", phone_status=doctor_verification.PENDING))
    res = await pipeline.process_inbound_message(tap(phone, "DOCV:YES:DOC-SOMEONE-ELSE"))
    assert res["action"] == "doctor_number_stale"
    assert dynamo_service.get_doctor("DOC-NEW-4").phone_status == doctor_verification.PENDING
    res = await pipeline.process_inbound_message(tap(phone, "DOCV:MAYBE:DOC-NEW-4"))
    assert res["action"] == "doctor_number_stale"


def test_confirmation_can_be_switched_off(client, wa, monkeypatch):
    monkeypatch.setattr(settings, "DOCTOR_NUMBER_CONFIRMATION_ENABLED", False)
    doc = client.post("/api/v1/doctors/register", json=registration()).json()
    assert doc["phone_status"] is None and sent(wa, "send_doctor_number_confirmation") == []


def test_legacy_and_confirmed_doctors_count_as_confirmed():
    assert doctor_verification.number_confirmed(DoctorProfile(
        doctor_id="D", name="n", phone_number="+1", license_no="l", specialty="s", bio=""))
    for status, ok in ((doctor_verification.CONFIRMED, True), (doctor_verification.PENDING, False),
                       (doctor_verification.DECLINED, False)):
        assert doctor_verification.number_confirmed(SimpleDoctor(status)) is ok


class SimpleDoctor:
    def __init__(self, status):
        self.phone_status = status


def test_admins_cannot_assign_a_doctor_with_an_unconfirmed_number(client, wa):
    doc = client.post("/api/v1/doctors/register", json=registration()).json()
    case = dynamo_service.save_case(EmergencyCase(
        case_id=f"CASE-{uuid.uuid4().hex[:8]}", user_phone=new_phone(), patient_name="P", transcript="t",
        detected_language="English", esi_level=2, severity="HIGH", clinical_summary="s", status="ACTIVE",
        doctor_offer_status="EXHAUSTED"))
    res = client.post(f"/api/v1/cases/{case.case_id}/assign", json={"doctor_id": doc["doctor_id"]})
    assert res.status_code == 409 and "not confirmed" in res.json()["detail"]


def test_resend_confirmation(client, wa):
    doc = client.post("/api/v1/doctors/register", json=registration()).json()
    url = f"/api/v1/doctors/{doc['doctor_id']}/confirm-number"
    assert client.post(url).status_code == 200
    assert len(sent(wa, "send_doctor_number_confirmation")) == 2
    dynamo_service.update_doctor_fields(doc["doctor_id"], {"phone_status": doctor_verification.DECLINED})
    res = client.post(url)
    assert res.status_code == 200 and res.json()["phone_status"] == doctor_verification.PENDING   # asked afresh
    dynamo_service.update_doctor_fields(doc["doctor_id"], {"phone_status": doctor_verification.CONFIRMED})
    assert client.post(url).status_code == 409
    assert client.post("/api/v1/cases/DOC-NOPE/confirm-number").status_code == 404
    assert client.post("/api/v1/doctors/DOC-NOPE/confirm-number").status_code == 404
    assert client.post("/api/v1/doctors/DOC-CARDIO-01/confirm-number").status_code == 409   # demo: legacy


# ============================================================================ edit a doctor
class FakeCognito:
    def __init__(self):
        self.updated, self.removed, self.error = [], [], None

    def invite_doctor(self, *args):
        return cognito.INVITED

    def update_doctor(self, *args):
        self.updated.append(args)
        if self.error:
            raise self.error
        return True

    def remove_doctor(self, email):
        self.removed.append(email)
        if self.error:
            raise self.error
        return True

    def portal_user_status(self, email):
        return cognito.INVITED


@pytest.fixture
def fake_cognito(monkeypatch):
    f = FakeCognito()
    monkeypatch.setattr(doctors_router, "cognito_service", f)
    return f


def confirmed_doctor(client, wa, **kw):
    doc = client.post("/api/v1/doctors/register", json=registration(**kw)).json()
    return dynamo_service.update_doctor_fields(doc["doctor_id"], {"phone_status": doctor_verification.CONFIRMED})


def test_edit_name_licence_bio_and_specialty(client, wa, fake_cognito):
    doc = confirmed_doctor(client, wa)
    url = f"/api/v1/doctors/{doc.doctor_id}"
    res = client.patch(url, json={"name": "Dr. Renamed", "license_no": "PMC-9", "bio": " Trauma lead ",
                                  "specialty": "Trauma"})
    assert res.status_code == 200
    body = res.json()
    assert (body["name"], body["license_no"], body["bio"], body["specialty"]) == ("Dr. Renamed", "PMC-9", "Trauma lead", "Trauma")
    assert body["email"] == doc.email and body["phone_status"] == doctor_verification.CONFIRMED   # unchanged
    assert fake_cognito.updated == [(doc.email, "Dr. Renamed", doc.doctor_id, "Trauma")]
    assert doc.doctor_id in [d.doctor_id for d in dynamo_service.get_available_doctors_by_specialty("Trauma")]
    assert client.patch(url, json={"name": "  "}).status_code == 422
    assert client.patch(url, json={"specialty": ""}).status_code == 422
    assert client.patch("/api/v1/doctors/DOC-NOPE", json={"name": "X"}).status_code == 404
    fake_cognito.error = RuntimeError("Cognito is down")
    assert client.patch(url, json={"name": "Dr. Again"}).status_code == 200    # the edit still saves


def test_a_new_number_must_be_confirmed_again(client, wa, fake_cognito):
    doc = confirmed_doctor(client, wa)
    old = doc.phone_number
    res = client.patch(f"/api/v1/doctors/{doc.doctor_id}", json={"phone_number": "0333 765 4321"})
    assert res.status_code == 200
    assert res.json()["phone_number"] == "+923337654321"
    assert res.json()["phone_status"] == doctor_verification.PENDING
    assert sent(wa, "send_doctor_number_confirmation")[-1][1][0] == "+923337654321"
    assert dynamo_service.get_doctor_by_phone("+923337654321").doctor_id == doc.doctor_id
    assert dynamo_service.get_doctor_by_phone(old) is None                   # the old number is a stranger's
    same = client.patch(f"/api/v1/doctors/{doc.doctor_id}", json={"phone_number": "+92 333 7654321"})
    assert same.status_code == 200
    assert len(sent(wa, "send_doctor_number_confirmation")) == 2            # same number: nothing new
    assert client.patch(f"/api/v1/doctors/{doc.doctor_id}", json={"phone_number": "12"}).status_code == 422


def test_a_number_cannot_be_taken_from_another_doctor(client, wa):
    a = confirmed_doctor(client, wa)
    b = confirmed_doctor(client, wa)
    res = client.patch(f"/api/v1/doctors/{b.doctor_id}", json={"phone_number": a.phone_number})
    assert res.status_code == 409 and a.name in res.json()["detail"]


@pytest.mark.asyncio
async def test_the_number_cannot_change_while_the_doctor_holds_a_case(only_new_doctors, client, wa):
    offers = only_new_doctors
    doctor = offers.doctor("A")
    case = await doctor_offers.start_offer(offers.case(esi=2))
    await doctor_offers.accept_offer(case.case_id, case.offer_id, doctor)
    res = client.patch(f"/api/v1/doctors/{doctor.doctor_id}", json={"phone_number": new_phone()})
    assert res.status_code == 409 and case.case_id in res.json()["detail"]
    assert client.patch(f"/api/v1/doctors/{doctor.doctor_id}", json={"bio": "ok"}).status_code == 200


# ============================================================================ unregister a doctor
@pytest.mark.asyncio
async def test_unregister_a_doctor(only_new_doctors, client, fake_cognito, fakes):
    offers = only_new_doctors
    doctor = dynamo_service.update_doctor_fields(offers.doctor("A").doctor_id, {"email": "a@example.com"})
    res = client.delete(f"/api/v1/doctors/{doctor.doctor_id}")
    assert res.status_code == 200 and res.json() == {"doctor_id": doctor.doctor_id, "removed": True}
    assert fake_cognito.removed == ["a@example.com"]
    assert dynamo_service.get_doctor(doctor.doctor_id) is None
    assert dynamo_service.get_doctor_by_phone(doctor.phone_number) is None
    assert client.delete(f"/api/v1/doctors/{doctor.doctor_id}").status_code == 404
    # Their number is a patient's now.
    fakes.triage.esi = 5
    res = await pipeline.process_inbound_message(inbound(doctor.phone_number, text="halka sar dard"))
    assert not str(res.get("action", "")).startswith("doctor_")


@pytest.mark.asyncio
async def test_unregistering_declines_a_pending_offer(only_new_doctors, client, fake_cognito):
    offers = only_new_doctors
    a, b = offers.doctor("A"), offers.doctor("B", handled=1)
    case = await doctor_offers.start_offer(offers.case(esi=3))               # ESI 3: one doctor at a time
    assert case.offered_doctor_ids == [a.doctor_id]
    assert client.delete(f"/api/v1/doctors/{a.doctor_id}").status_code == 200
    assert get(case.case_id).offered_doctor_ids == [b.doctor_id]              # the next round goes to B


@pytest.mark.asyncio
async def test_a_doctor_holding_a_case_cannot_be_unregistered(only_new_doctors, client, fake_cognito):
    offers = only_new_doctors
    doctor = offers.doctor("A")
    case = await doctor_offers.start_offer(offers.case(esi=2))
    await doctor_offers.accept_offer(case.case_id, case.offer_id, doctor)
    res = client.delete(f"/api/v1/doctors/{doctor.doctor_id}")
    assert res.status_code == 409 and case.case_id in res.json()["detail"]
    assert dynamo_service.get_doctor(doctor.doctor_id) is not None


def test_unregistering_keeps_the_doctor_when_cognito_fails(client, wa, fake_cognito):
    doc = confirmed_doctor(client, wa)
    fake_cognito.error = RuntimeError("Cognito is down")
    assert client.delete(f"/api/v1/doctors/{doc.doctor_id}").status_code == 502
    assert dynamo_service.get_doctor(doc.doctor_id) is not None


# ============================================================================ delete a case
def closed_case(status="RESOLVED", **kw):
    case = dynamo_service.save_case(EmergencyCase(
        case_id=f"CASE-{uuid.uuid4().hex[:8]}", user_phone=new_phone(), patient_name="P", transcript="t",
        detected_language="Urdu", esi_level=2, severity="HIGH", clinical_summary="s", status=status,
        audio_s3_key=f"voice-notes/x/{uuid.uuid4().hex}.ogg", **kw))
    audio_service.upload_media_to_s3(b"ogg", case.audio_s3_key, "audio/ogg")
    photo = f"images/x/{uuid.uuid4().hex}.jpg"
    audio_service.upload_media_to_s3(b"jpg", photo, "image/jpeg")
    for i, key in enumerate((None, photo)):
        dynamo_service.save_chat_message(ChatMessage(message_id=f"MSG-{i}", case_id=case.case_id, sender="PATIENT",
                                                     sender_name="P", content=f"m{i}", image_s3_key=key))
    return case, photo


@pytest.mark.parametrize("status", ["RESOLVED", "DISMISSED"])
def test_delete_a_closed_case(client, status):
    case, photo = closed_case(status)
    month = case.created_at[:7]
    before = dict(dynamo_service._mock_metrics.get(month, {}))
    res = client.delete(f"/api/v1/cases/{case.case_id}")
    assert res.status_code == 200
    assert res.json() == {"case_id": case.case_id, "deleted": True, "messages": 2, "media": 2}
    assert dynamo_service.get_case(case.case_id) is None
    assert dynamo_service.get_case_messages(case.case_id) == []
    assert audio_service.get_audio_object(case.audio_s3_key) is None and audio_service.get_audio_object(photo) is None
    after = dynamo_service._mock_metrics.get(month, {})
    assert after.get("cases_total", 0) == before.get("cases_total", 0) - 1
    assert after.get("esi_1_2", 0) == before.get("esi_1_2", 0) - 1
    assert client.get(f"/api/v1/cases/{case.case_id}").status_code == 404
    assert client.delete(f"/api/v1/cases/{case.case_id}").status_code == 404


@pytest.mark.parametrize("status", ["ACTIVE", "DOCTOR_HANDOVER"])
def test_an_open_case_cannot_be_deleted(client, status):
    case, _ = closed_case(status)
    res = client.delete(f"/api/v1/cases/{case.case_id}")
    assert res.status_code == 409 and "close it" in res.json()["detail"]
    assert dynamo_service.get_case(case.case_id) is not None


def test_delete_media_is_best_effort():
    assert audio_service.delete_media([None, "", "voice-notes/missing.ogg"]) == 0


# ============================================================================ access rules
@pytest.mark.parametrize("method,path,payload", [
    ("patch", "/api/v1/doctors/DOC-CARDIO-01", {"name": "Me"}),
    ("delete", "/api/v1/doctors/DOC-CARDIO-01", None),
    ("post", "/api/v1/doctors/DOC-CARDIO-01/confirm-number", None),
    ("delete", "/api/v1/cases/CASE-DEMO-01", None),
])
def test_doctors_cannot_manage_doctors_or_delete_cases(auth_on, client, method, path, payload):  # noqa: F811
    doctor = bearer(make_token(groups=["Doctors"], doctor_id="DOC-CARDIO-01"))
    res = getattr(client, method)(path, headers=doctor, **({"json": payload} if payload else {}))
    assert res.status_code == 403


def test_doctors_still_read_their_own_profile(auth_on, client):  # noqa: F811
    doctor = bearer(make_token(groups=["Doctors"], doctor_id="DOC-CARDIO-01"))
    assert client.get("/api/v1/doctors/DOC-CARDIO-01", headers=doctor).status_code == 200


# ============================================================================ DynamoDB branch
def test_doctor_edit_unregister_and_case_delete_in_dynamodb(monkeypatch):
    from app.services.dynamo_service import DynamoDBService, create_resq_table
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setattr(settings, "MOCK_AWS", False)
    monkeypatch.setattr(settings, "AWS_ENDPOINT_URL", None)
    monkeypatch.setattr(settings, "SEED_DEMO_DATA", False)
    with mock_aws():
        create_resq_table(boto3.client("dynamodb", region_name=settings.AWS_REGION), settings.DYNAMODB_TABLE_NAME)
        svc = DynamoDBService()
        assert not svc.mock_mode
        svc.register_doctor(DoctorProfile(doctor_id="DOC-1", name="Dr. One", phone_number="+923001112222",
                                          license_no="L", specialty="Cardiology", bio="", cases_handled=4))
        doc = svc.update_doctor_profile("DOC-1", {"specialty": "Trauma", "phone_number": "+923009998888"})
        assert doc.specialty == "Trauma" and doc.cases_handled == 4
        svc._mock_doctors.clear()
        svc._mock_doctor_phones.clear()
        assert [d.doctor_id for d in svc.get_available_doctors_by_specialty("Trauma")] == ["DOC-1"]
        assert svc.get_available_doctors_by_specialty("Cardiology") == []
        assert svc.get_doctor_by_phone("+923009998888").doctor_id == "DOC-1"
        assert svc.get_doctor_by_phone("+923001112222") is None
        assert svc.update_doctor_profile("DOC-NOPE", {"name": "X"}) is None
        assert svc.delete_doctor("DOC-1") is True
        assert svc.get_doctor("DOC-1") is None and svc.get_doctor_by_phone("+923009998888") is None
        assert svc.delete_doctor("DOC-1") is False

        case = svc.save_case(EmergencyCase(case_id="CASE-DEL", user_phone="+923001110000", patient_name="P",
                                           transcript="t", detected_language="Urdu", esi_level=1, severity="CRITICAL",
                                           clinical_summary="s", status="RESOLVED"))
        for i in range(3):
            svc.save_chat_message(ChatMessage(message_id=f"MSG-{i}", case_id="CASE-DEL", sender="PATIENT",
                                              sender_name="P", content=f"m{i}"))
        month = case.created_at[:7]
        assert svc.get_month_metrics(month)["cases_total"] == 1
        deleted, messages = svc.delete_case("CASE-DEL")
        assert deleted.case_id == "CASE-DEL" and len(messages) == 3
        svc._mock_cases.clear()
        svc._mock_messages.clear()
        assert svc.get_case("CASE-DEL") is None and svc.get_case_messages("CASE-DEL") == []
        assert svc.get_month_metrics(month)["cases_total"] == 0
        assert svc.delete_case("CASE-DEL") is None
