"""ADMIN-1: administrators are alerted when a case has no doctor (app/admin_alerts.py).

The portal alert is derived from the case in the browser; here: the Amazon Connect call to the
administrator numbers for ESI 1-2 (queued once per waiting period, repeated until acknowledged), the
acknowledgement, and the announcement call itself."""
import uuid
from types import SimpleNamespace

import pytest

from app import admin_alerts, doctor_offers
from app.config import settings
from app.models.schemas import InternalTask
from app.services import telephony_service as telephony_module
from app.services.dynamo_service import dynamo_service
from test_doctor_offers import Offers, get
from test_pipeline_support import now_iso

ADMINS = "+923001110001, +923001110002;+923001110001,  "


class FakePhone:
    def __init__(self, fail=()):
        self.calls = []
        self.fail = set(fail)

    async def place_announcement_call(self, phone, message, language="English"):
        self.calls.append((phone, message, language))
        if phone in self.fail:
            return {"status": "FAILED", "contact_phone": phone, "contact_id": None, "error": "Connect throttled"}
        return {"status": "CALL_INITIATED", "contact_phone": phone, "contact_id": f"CONN-{len(self.calls)}",
                "simulated": True}


@pytest.fixture
def offers(monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_ALERT_PHONE_NUMBERS", ADMINS)
    return Offers(monkeypatch)


@pytest.fixture
def phone(monkeypatch):
    fake = FakePhone()
    monkeypatch.setattr(telephony_module, "telephony_service", fake)
    return fake


def admin_tasks(offers):
    return [t for t in offers.tasks if t[0] == admin_alerts.ADMIN_CALL_TASK]


async def run_call(payload):
    return await admin_alerts.handle_admin_call(InternalTask(task_id=uuid.uuid4().hex, kind=admin_alerts.ADMIN_CALL_TASK,
                                                             payload=payload, run_at=now_iso()))


async def waiting_case(offers, esi=1):
    case = await doctor_offers.start_offer(offers.case(esi=esi))
    assert case.doctor_offer_status == "EXHAUSTED"
    return get(case.case_id)


def notes(case_id):
    return [m.content for m in dynamo_service.get_case_messages(case_id)]


# ---------------------------------------------------------------------------- queueing
def test_admin_numbers_are_cleaned():
    assert admin_alerts.admin_numbers() == []                                   # unset by default
    settings_before = settings.ADMIN_ALERT_PHONE_NUMBERS
    try:
        settings.ADMIN_ALERT_PHONE_NUMBERS = ADMINS + ", 12, +92 300-1110003"
        assert admin_alerts.admin_numbers() == ["+923001110001", "+923001110002", "+923001110003"]
    finally:
        settings.ADMIN_ALERT_PHONE_NUMBERS = settings_before


@pytest.mark.asyncio
@pytest.mark.parametrize("esi", [1, 2])
async def test_esi_1_2_without_a_doctor_queues_the_admin_call_once(offers, esi):
    case = await waiting_case(offers, esi)
    assert admin_tasks(offers) == [(admin_alerts.ADMIN_CALL_TASK,
                                    {"case_id": case.case_id, "since": case.no_doctor_since, "attempt": 1}, 0)]
    assert case.admin_call_period == case.no_doctor_since
    assert any("calling the administrators (2 numbers)" in n for n in notes(case.case_id))
    assert await admin_alerts.start(case) is False                              # once per waiting period
    await doctor_offers.start_offer(case)                                       # still nobody
    assert len(admin_tasks(offers)) == 1


@pytest.mark.asyncio
async def test_esi_3_without_a_doctor_gets_no_call(offers):
    case = await waiting_case(offers, esi=3)
    assert admin_tasks(offers) == [] and case.admin_call_period is None
    assert not any("calling the administrator" in n for n in notes(case.case_id))


@pytest.mark.asyncio
async def test_an_esi_3_case_escalated_while_waiting_calls_the_admins(offers):
    case = await waiting_case(offers, esi=3)
    escalated = dynamo_service.update_case(case.case_id, {"esi_level": 1})
    await doctor_offers.start_offer(escalated, restart=True)
    assert [t[1]["case_id"] for t in admin_tasks(offers)] == [case.case_id]


@pytest.mark.asyncio
async def test_without_numbers_only_the_portal_alert_runs(offers, monkeypatch, phone):
    monkeypatch.setattr(settings, "ADMIN_ALERT_PHONE_NUMBERS", "")
    case = await waiting_case(offers)
    assert admin_tasks(offers) == []
    assert any("no administrator phone number is configured" in n for n in notes(case.case_id))
    assert (await run_call({"case_id": case.case_id, "since": case.no_doctor_since, "attempt": 1}))["outcome"] == "no_numbers"
    assert phone.calls == []


@pytest.mark.asyncio
async def test_a_new_waiting_period_calls_again(offers):
    a = offers.doctor("A")
    case = await doctor_offers.start_offer(offers.case(esi=1))
    await doctor_offers.decline_offer(case.case_id, case.offer_id, a)            # period 1
    first = get(case.case_id)
    b = offers.doctor("B")
    await doctor_offers.offer_waiting_cases()
    await doctor_offers.decline_offer(case.case_id, get(case.case_id).offer_id, b)   # period 2
    second = get(case.case_id)
    assert second.no_doctor_since != first.no_doctor_since
    assert [t[1]["since"] for t in admin_tasks(offers)] == [first.no_doctor_since, second.no_doctor_since]


# ---------------------------------------------------------------------------- the call task
@pytest.mark.asyncio
async def test_the_call_task_calls_every_admin_and_queues_the_next_call(offers, phone):
    case = await waiting_case(offers)
    payload = admin_tasks(offers)[0][1]
    res = await run_call(payload)
    assert res["outcome"] == "called" and res["status"] == "CALLED"
    assert [c[0] for c in phone.calls] == ["+923001110001", "+923001110002"]
    message, language = phone.calls[0][1], phone.calls[0][2]
    assert language == "English" and "ESI level 1 case has no doctor assigned" in message
    assert "No doctor is available" in message and "check the ResQ portal as soon as possible" in message
    after = get(case.case_id)
    assert [(c["phone"], c["status"], c["attempt"]) for c in after.admin_alert_calls] == \
        [("+923001110001", "CALL_INITIATED", 1), ("+923001110002", "CALL_INITIATED", 1)]
    assert all(c["since"] == case.no_doctor_since for c in after.admin_alert_calls)
    assert any("Administrator alert call 1/3" in n and "placed" in n for n in notes(case.case_id))
    assert admin_tasks(offers)[-1] == (admin_alerts.ADMIN_CALL_TASK, {**payload, "attempt": 2},
                                       settings.ADMIN_ALERT_REPEAT_SECONDS)


@pytest.mark.asyncio
async def test_repeats_stop_after_the_last_call(offers, phone):
    case = await waiting_case(offers)
    payload = admin_tasks(offers)[0][1]
    for attempt in (1, 2, 3):
        await run_call({**payload, "attempt": attempt})
    assert len(phone.calls) == 6 and len(get(case.case_id).admin_alert_calls) == 6
    assert [t[1]["attempt"] for t in admin_tasks(offers)] == [1, 2, 3]          # nothing after the 3rd
    assert (await run_call({**payload, "attempt": 4}))["outcome"] == "max_calls"


@pytest.mark.asyncio
async def test_the_same_attempt_never_calls_twice(offers, phone):
    case = await waiting_case(offers)
    payload = admin_tasks(offers)[0][1]
    await run_call(payload)
    replay = await run_call(payload)                                           # SQS redelivery
    assert replay["replay"] is True and len(phone.calls) == 2
    assert len(get(case.case_id).admin_alert_calls) == 2


@pytest.mark.asyncio
async def test_acknowledging_stops_the_calls(offers, phone):
    case = await waiting_case(offers)
    payload = admin_tasks(offers)[0][1]
    acked = await admin_alerts.acknowledge(case.case_id, "admin@resq.dev")
    assert acked.admin_alert_ack_by == "admin@resq.dev" and acked.admin_alert_ack_at
    assert any("acknowledged the no-doctor alert" in n for n in notes(case.case_id))
    res = await run_call(payload)
    assert res == {"case_id": case.case_id, "outcome": "acknowledged", "by": "admin@resq.dev"}
    assert phone.calls == []
    assert (await admin_alerts.acknowledge(case.case_id, "other@resq.dev")).admin_alert_ack_by == "admin@resq.dev"


@pytest.mark.asyncio
async def test_the_call_task_is_stale_once_a_doctor_takes_the_case(offers, phone):
    case = await waiting_case(offers)
    payload = admin_tasks(offers)[0][1]
    doctor = offers.doctor("Late")
    await doctor_offers.assign_by_admin(case.case_id, doctor, "admin@resq.dev")
    assert (await run_call(payload))["outcome"] == "stale" and phone.calls == []
    assert await admin_alerts.acknowledge(case.case_id, "admin@resq.dev") is None   # not waiting any more


@pytest.mark.asyncio
async def test_the_call_task_is_stale_for_a_closed_case_or_another_period(offers, phone):
    case = await waiting_case(offers)
    payload = admin_tasks(offers)[0][1]
    assert (await run_call({**payload, "since": "2020-01-01T00:00:00+00:00"}))["outcome"] == "stale"
    dynamo_service.update_case(case.case_id, {"status": "RESOLVED"})
    assert (await run_call(payload))["outcome"] == "stale" and phone.calls == []


@pytest.mark.asyncio
async def test_a_failed_call_is_recorded_and_noted(offers, monkeypatch):
    fake = FakePhone(fail={"+923001110002"})
    monkeypatch.setattr(telephony_module, "telephony_service", fake)
    case = await waiting_case(offers)
    res = await run_call(admin_tasks(offers)[0][1])
    assert res["status"] == "CALLED"                                            # one got through
    calls = get(case.case_id).admin_alert_calls
    assert [c["status"] for c in calls] == ["CALL_INITIATED", "FAILED"] and calls[1]["error"] == "Connect throttled"
    assert any("+923001110002: failed (Connect throttled)" in n for n in notes(case.case_id))


@pytest.mark.asyncio
async def test_all_calls_failing_still_queues_the_next_attempt(offers, monkeypatch):
    fake = FakePhone(fail={"+923001110001", "+923001110002"})
    monkeypatch.setattr(telephony_module, "telephony_service", fake)
    case = await waiting_case(offers)
    res = await run_call(admin_tasks(offers)[0][1])
    assert res["status"] == "FAILED" and admin_tasks(offers)[-1][1]["attempt"] == 2


def test_spoken_message_says_the_level_and_why():
    case = SimpleNamespace(esi_level=2, no_doctor_reason="ALL_DECLINED")
    text = admin_alerts.spoken_message(case)
    assert "ESI level 2 case has no doctor assigned" in text and "All doctors declined" in text
    case.no_doctor_reason = "NO_DOCTOR_FREE"
    assert "No doctor is available" in admin_alerts.spoken_message(case)


# ---------------------------------------------------------------------------- the portal endpoint
@pytest.mark.asyncio
async def test_acknowledge_endpoint(offers):
    from httpx import ASGITransport, AsyncClient
    from app.main import app
    case = await waiting_case(offers)
    other = offers.case(esi=2)                                                  # not waiting
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(f"/api/v1/cases/{case.case_id}/no-doctor-alert/ack")
        assert r.status_code == 200 and r.json()["admin_alert_ack_by"] == "local@resq.dev"
        assert (await client.post(f"/api/v1/cases/{other.case_id}/no-doctor-alert/ack")).status_code == 409
        assert (await client.post("/api/v1/cases/CASE-NOPE/no-doctor-alert/ack")).status_code == 404


def test_acknowledge_is_an_admin_only_route():
    from app import auth
    assert any(r.match("/api/v1/cases/CASE-1/no-doctor-alert/ack") for r in auth._ADMIN_PATH_RES)


# ---------------------------------------------------------------------------- the announcement call
@pytest.mark.asyncio
async def test_announcement_call_simulated_in_mock_mode():
    res = await telephony_module.TelephonyService().place_announcement_call("+923001110001", "Hello admin")
    assert res["status"] == "CALL_INITIATED" and res["simulated"] is True
    assert res["spoken_message"] == "Hello admin" and res["language"] == "English"


@pytest.mark.asyncio
async def test_announcement_call_uses_the_same_flow_with_only_the_message(monkeypatch):
    service = telephony_module.TelephonyService()
    sent = {}

    class Connect:
        def start_outbound_voice_contact(self, **kwargs):
            sent.update(kwargs)
            return {"ContactId": "contact-1"}
    service.mock_mode, service.connect = False, Connect()
    res = await service.place_announcement_call("+923001110001", "Urgent alert", "English")
    assert res == {"status": "CALL_INITIATED", "provider": "Amazon Connect", "simulated": False,
                   "contact_phone": "+923001110001", "contact_id": "contact-1", "language": "English"}
    assert sent["ContactFlowId"] == settings.CONNECT_FLOW_ID and sent["DestinationPhoneNumber"] == "+923001110001"
    assert sent["Attributes"] == {"Purpose": "admin_alert", "Language": "English", "SpokenMessage": "Urgent alert"}

    class Broken:
        def start_outbound_voice_contact(self, **kwargs):
            raise RuntimeError("no permission")
    service.connect = Broken()
    failed = await service.place_announcement_call("+923001110001", "Urgent alert")
    assert failed["status"] == "FAILED" and failed["error"] == "no permission"


@pytest.mark.asyncio
async def test_emergency_contact_call_keeps_its_attributes(monkeypatch):
    service = telephony_module.TelephonyService()
    sent = {}

    class Connect:
        def start_outbound_voice_contact(self, **kwargs):
            sent.update(kwargs)
            return {"ContactId": "c"}
    service.mock_mode, service.connect = False, Connect()
    await service.call_emergency_contact("+923331234567", "Ahmed", "chest pain", "English")
    assert set(sent["Attributes"]) == {"PatientName", "EmergencyDetails", "Language", "SpokenMessage"}
    assert sent["Attributes"]["PatientName"] == "Ahmed" and "chest pain" in sent["Attributes"]["SpokenMessage"]
