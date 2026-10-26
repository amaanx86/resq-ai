"""OFFER-1 / OFFER-2: doctors accept a case before it is assigned to them (app/doctor_offers.py).

Each test starts with no doctors but its own (the seeded demo doctors are removed for the test, and
conftest restores the store afterwards), so rounds and fan-outs are exact. ESI 1-2 rounds go to up to
three doctors at once (OFFER-3), ESI 3 rounds to one. Internal tasks (timeouts, re-checks, admin
calls) are recorded instead of queued; tests run the handlers themselves."""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import boto3
import pytest
from moto import mock_aws

from app import admin_alerts, doctor_chat, doctor_offers, pipeline
from app.config import settings
from app.models.schemas import DoctorProfile, EmergencyCase, InboundWhatsAppMessage, InternalTask
from app.services.dynamo_service import DynamoDBService, create_resq_table, dynamo_service
from test_pipeline_support import FakeWhatsApp, new_phone, now_iso


class Offers:
    def __init__(self, monkeypatch):
        self.whatsapp = FakeWhatsApp()
        self.timeouts = []
        self.tasks = []   # (kind, payload, delay) of every other internal task
        monkeypatch.setattr(doctor_offers, "whatsapp_service", self.whatsapp)
        monkeypatch.setattr(doctor_chat, "whatsapp_service", self.whatsapp)
        monkeypatch.setattr(dynamo_service, "_mock_doctors", {})
        monkeypatch.setattr(dynamo_service, "_mock_doctor_phones", {})

        async def record_timeout(case, seconds):
            self.timeouts.append((case.case_id, case.offer_id, seconds))
        monkeypatch.setattr(doctor_offers, "_schedule_timeout", record_timeout)

        async def record_task(kind, task_payload, delay_seconds):
            self.tasks.append((kind, dict(task_payload), delay_seconds))
        monkeypatch.setattr(doctor_offers, "_schedule", record_task)

        async def record_admin_task(task_payload, delay_seconds):
            self.tasks.append((admin_alerts.ADMIN_CALL_TASK, dict(task_payload), delay_seconds))
        monkeypatch.setattr(admin_alerts, "_schedule", record_admin_task)

        async def no_idle_reminder(*a, **k):
            return None
        monkeypatch.setattr(doctor_chat, "schedule_idle_reminder", no_idle_reminder)
        self.specialty = f"Spec{uuid.uuid4().hex[:6]}"

    def doctor(self, name: str, handled: int = 0) -> DoctorProfile:
        return dynamo_service.register_doctor(DoctorProfile(
            doctor_id=f"DOC-{name}-{uuid.uuid4().hex[:4]}", name=f"Dr. {name}", phone_number=new_phone(),
            license_no="L", specialty=self.specialty, bio="b", cases_handled=handled))

    def case(self, esi: int = 2, status: str = None) -> EmergencyCase:
        return dynamo_service.save_case(EmergencyCase(
            case_id=f"CASE-{uuid.uuid4().hex[:10].upper()}", user_phone=new_phone(), patient_name="Ahmed",
            transcript="t", detected_language="Urdu", esi_level=esi, severity="CRITICAL",
            status=status or ("ACTIVE" if esi == 3 else "DOCTOR_HANDOVER"), matched_specialty=self.specialty,
            clinical_summary="Chest pain for 20 minutes."))

    def sent_to(self, phone: str):
        return [c for c in self.whatsapp.calls if c[1] and c[1][0] == phone]

    async def timeout(self, case_id: str, offer_id: str):
        return await doctor_offers.handle_timeout(InternalTask(
            task_id="t", kind=doctor_offers.TIMEOUT_TASK, payload={"case_id": case_id, "offer_id": offer_id},
            run_at=now_iso()))


@pytest.fixture
def offers(monkeypatch):
    return Offers(monkeypatch)


def get(case_id: str) -> EmergencyCase:
    return dynamo_service.get_case(case_id)


# ---------------------------------------------------------------------------- one round
@pytest.mark.asyncio
async def test_offer_skips_the_busy_doctor_and_the_free_one_accepts_it(offers):
    busy, free = offers.doctor("Busy", handled=1), offers.doctor("Free", handled=40)
    dynamo_service.save_case(offers.case().model_copy(update={"case_id": f"CASE-{uuid.uuid4().hex[:8]}",
                                                              "assigned_doctor_id": busy.doctor_id}))
    case = await doctor_offers.start_offer(offers.case(esi=2))
    assert case.doctor_offer_status == "PENDING" and case.offered_doctor_ids == [free.doctor_id]
    assert case.assigned_doctor_id is None
    assert offers.timeouts == [(case.case_id, case.offer_id, settings.DOCTOR_OFFER_TIMEOUT_ESI2)]
    [offer] = offers.sent_to(free.phone_number)
    assert offer[0] == "send_doctor_case_offer"
    assert offer[1][1:5] == (2, offers.specialty, "Chest pain for 20 minutes.", "60 seconds")
    assert offer[1][5:] == (f"OFFER:ACCEPT:{case.case_id}:{case.offer_id}", f"OFFER:DECLINE:{case.case_id}:{case.offer_id}")

    outcome, accepted = await doctor_offers.accept_offer(case.case_id, case.offer_id, free)
    assert outcome == doctor_offers.ACCEPTED
    assert (accepted.assigned_doctor_id, accepted.assigned_doctor_name) == (free.doctor_id, "Dr. Free")
    assert accepted.doctor_offer_status == "ACCEPTED" and accepted.accepted_at and accepted.offer_started_at
    assert accepted.offer_history[-1]["outcome"] == "ACCEPTED"
    assert dynamo_service.get_doctor(free.doctor_id).cases_handled == 41          # counted on accept
    patient = offers.sent_to(case.user_phone)
    assert patient and "Dr. Free" in patient[0][1][1] and "accepted your case" in patient[0][1][1]
    link = [c for c in offers.sent_to(free.phone_number) if c[0] == "send_case_link"]
    assert link and f"?case={case.case_id}" in link[0][1][3]
    assert any("accepted the case" in m.content for m in dynamo_service.get_case_messages(case.case_id))


@pytest.mark.asyncio
async def test_start_offer_is_idempotent_and_rearms_the_timer(offers):
    offers.doctor("A")
    case = await doctor_offers.start_offer(offers.case())
    again = await doctor_offers.start_offer(get(case.case_id))
    assert again.offer_id == case.offer_id and len(again.offer_history) == 1
    assert [t[1] for t in offers.timeouts] == [case.offer_id, case.offer_id]      # re-armed, same round


@pytest.mark.asyncio
async def test_nothing_is_offered_for_closed_or_assigned_cases(offers):
    offers.doctor("A")
    dismissed = await doctor_offers.start_offer(offers.case(status="DISMISSED"))
    assert dismissed.doctor_offer_status is None and offers.timeouts == []


# ---------------------------------------------------------------------------- next rounds
@pytest.mark.asyncio
async def test_decline_moves_the_case_to_the_next_doctor(offers):
    first, second = offers.doctor("First"), offers.doctor("Second", handled=5)
    case = await doctor_offers.start_offer(offers.case(esi=3))                 # ESI 3: one doctor per round
    assert case.offered_doctor_ids == [first.doctor_id]
    outcome, after = await doctor_offers.decline_offer(case.case_id, case.offer_id, first)
    assert outcome == doctor_offers.DECLINED
    assert after.offered_doctor_ids == [second.doctor_id] and after.offer_id != case.offer_id
    assert first.doctor_id in after.declined_doctor_ids
    assert [r["outcome"] for r in after.offer_history] == ["DECLINED", "PENDING"]
    # The first doctor cannot take it back with the old offer.
    assert (await doctor_offers.accept_offer(case.case_id, case.offer_id, first))[0] == doctor_offers.TAKEN


@pytest.mark.asyncio
async def test_timeout_moves_the_case_on_and_a_stale_timeout_is_ignored(offers):
    first, second = offers.doctor("First"), offers.doctor("Second", handled=5)
    case = await doctor_offers.start_offer(offers.case(esi=3))
    res = await offers.timeout(case.case_id, case.offer_id)
    assert res["outcome"] == "next_round"
    after = get(case.case_id)
    assert after.offered_doctor_ids == [second.doctor_id] and first.doctor_id in after.declined_doctor_ids
    assert after.offer_history[0]["outcome"] == "TIMED_OUT"
    # The same timeout delivered again (SQS at-least-once) changes nothing.
    assert (await offers.timeout(case.case_id, case.offer_id))["outcome"] == "stale"
    assert get(case.case_id).offer_id == after.offer_id
    # A timeout after the accept changes nothing either.
    await doctor_offers.accept_offer(case.case_id, after.offer_id, second)
    assert (await offers.timeout(case.case_id, after.offer_id))["outcome"] == "stale"
    assert get(case.case_id).assigned_doctor_id == second.doctor_id


@pytest.mark.asyncio
async def test_all_decline_lets_the_case_wait_and_tells_the_patient(offers):
    first, second = offers.doctor("First"), offers.doctor("Second", handled=5)
    case = await doctor_offers.start_offer(offers.case(esi=2))
    assert case.offered_doctor_ids == [first.doctor_id, second.doctor_id]       # both at once (ESI 2)
    _, mid = await doctor_offers.decline_offer(case.case_id, case.offer_id, first)
    assert mid.doctor_offer_status == "PENDING"                                 # the round goes on
    _, final = await doctor_offers.decline_offer(case.case_id, case.offer_id, second)
    assert final.doctor_offer_status == "EXHAUSTED" and final.offered_doctor_ids == []
    assert final.assigned_doctor_id is None
    assert final.no_doctor_reason == doctor_offers.ALL_DECLINED and final.no_doctor_since
    [text] = [c[1][1] for c in offers.sent_to(case.user_phone)]
    assert text == doctor_offers.waiting_text(2)
    assert "No doctor is free right now" in text and "Stay on this chat" in text and "1122" in text
    assert "ambulance" not in text                                              # wait, not "call one now"
    notes = [m.content for m in dynamo_service.get_case_messages(case.case_id)]
    assert any("every free doctor declined" in n and "administrators are alerted" in n for n in notes)
    assert (doctor_offers.RECHECK_TASK, {"case_id": case.case_id, "since": final.no_doctor_since},
            settings.WAITING_CASE_RECHECK_SECONDS) in offers.tasks


@pytest.mark.asyncio
async def test_nobody_on_duty_exhausts_without_a_second_patient_message(offers, monkeypatch):
    from app.agents import tools
    monkeypatch.setattr(tools, "rank_candidates", lambda *a, **k: [])
    case = await doctor_offers.start_offer(offers.case())
    assert case.doctor_offer_status == "EXHAUSTED" and case.offer_history == []
    assert case.no_doctor_reason == doctor_offers.NO_DOCTOR_FREE
    assert offers.sent_to(case.user_phone) == []          # the first reply already said so


# ---------------------------------------------------------------------------- ESI rules
@pytest.mark.asyncio
async def test_esi1_fans_out_and_first_accept_wins(offers):
    a, b, c, d = (offers.doctor(n, handled=i) for i, n in enumerate("ABCD"))
    case = await doctor_offers.start_offer(offers.case(esi=1))
    assert case.offered_doctor_ids == [a.doctor_id, b.doctor_id, c.doctor_id]   # top 3 at once
    assert offers.timeouts[-1][2] == settings.DOCTOR_OFFER_TIMEOUT_ESI1
    # One decline does not end the round; the others still can accept.
    _, after = await doctor_offers.decline_offer(case.case_id, case.offer_id, a)
    assert after.offer_id == case.offer_id and after.doctor_offer_status == "PENDING"
    assert (await doctor_offers.accept_offer(case.case_id, case.offer_id, a))[0] == doctor_offers.TAKEN  # declined
    assert (await doctor_offers.accept_offer(case.case_id, case.offer_id, c))[0] == doctor_offers.ACCEPTED
    assert (await doctor_offers.accept_offer(case.case_id, case.offer_id, b))[0] == doctor_offers.TAKEN
    taken = [x for x in offers.sent_to(b.phone_number) if x[0] == "send_text_message"]
    assert taken and "accepted by another doctor" in taken[0][1][1]


@pytest.mark.asyncio
async def test_esi3_uses_the_long_timeout(offers):
    offers.doctor("A")
    case = await doctor_offers.start_offer(offers.case(esi=3))
    assert offers.timeouts[-1][2] == settings.DOCTOR_OFFER_TIMEOUT_ESI3 == 300
    assert offers.sent_to(dynamo_service.get_doctor(case.offered_doctor_ids[0]).phone_number)[0][1][4] == "5 minutes"


@pytest.mark.asyncio
async def test_two_doctors_accepting_at_once_exactly_one_wins(offers):
    a, b, c = offers.doctor("A"), offers.doctor("B", handled=1), offers.doctor("C", handled=2)
    case = await doctor_offers.start_offer(offers.case(esi=1))
    results = await asyncio.gather(*(doctor_offers.accept_offer(case.case_id, case.offer_id, d) for d in (a, b, c)))
    outcomes = [r[0] for r in results]
    assert outcomes.count(doctor_offers.ACCEPTED) == 1 and outcomes.count(doctor_offers.TAKEN) == 2
    winner = [d for d, r in zip((a, b, c), results) if r[0] == doctor_offers.ACCEPTED][0]
    assert get(case.case_id).assigned_doctor_id == winner.doctor_id


@pytest.mark.asyncio
async def test_doctor_with_a_pending_offer_is_not_offered_a_second_case(offers):
    a, b = offers.doctor("A"), offers.doctor("B", handled=10)
    first = await doctor_offers.start_offer(offers.case(esi=3))
    second = await doctor_offers.start_offer(offers.case(esi=3))
    assert first.offered_doctor_ids == [a.doctor_id] and second.offered_doctor_ids == [b.doctor_id]


@pytest.mark.asyncio
async def test_accept_on_a_closed_case_is_refused(offers):
    a = offers.doctor("A")
    case = await doctor_offers.start_offer(offers.case())
    dynamo_service.update_case(case.case_id, {"status": "RESOLVED"})
    assert (await doctor_offers.accept_offer(case.case_id, case.offer_id, a))[0] == doctor_offers.CLOSED
    assert (await offers.timeout(case.case_id, case.offer_id))["outcome"] == "stale"


@pytest.mark.asyncio
async def test_stale_pending_round_is_timed_out_when_started_again(offers):
    first, second = offers.doctor("First"), offers.doctor("Second", handled=5)
    case = await doctor_offers.start_offer(offers.case(esi=3))
    long_ago = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    dynamo_service.update_case(case.case_id, {"offer_expires_at": long_ago})
    after = await doctor_offers.start_offer(get(case.case_id))
    assert after.offered_doctor_ids == [second.doctor_id] and after.offer_history[0]["outcome"] == "TIMED_OUT"


@pytest.mark.asyncio
async def test_admin_assigns_an_exhausted_case_by_hand(offers, monkeypatch):
    from app.agents import tools
    monkeypatch.setattr(tools, "rank_candidates", lambda *a, **k: [])
    case = await doctor_offers.start_offer(offers.case())
    monkeypatch.undo()
    doctor = offers.doctor("Manual")
    updated = await doctor_offers.assign_by_admin(case.case_id, doctor, "admin@resq.dev")
    assert updated.assigned_doctor_id == doctor.doctor_id and updated.doctor_offer_status == "ACCEPTED"
    assert updated.offer_history[-1]["outcome"] == "ASSIGNED_BY_ADMIN"


# ---------------------------------------------------------------------------- WhatsApp buttons (pipeline)
def _tap(phone: str, payload: str) -> InboundWhatsAppMessage:
    return InboundWhatsAppMessage(provider="meta", provider_message_id=f"wamid.{uuid.uuid4().hex}",
                                  sender_phone=phone, timestamp=now_iso(), type="button",
                                  button_payload=payload, button_text="Accept")


@pytest.mark.asyncio
async def test_whatsapp_accept_button_assigns_the_case(offers):
    a = offers.doctor("A")
    case = await doctor_offers.start_offer(offers.case())
    res = await pipeline.process_inbound_message(_tap(a.phone_number, f"OFFER:ACCEPT:{case.case_id}:{case.offer_id}"))
    assert res["action"] == "offer_accepted" and res["case_id"] == case.case_id
    assert get(case.case_id).assigned_doctor_id == a.doctor_id
    # A late tap on the same (now accepted) button.
    late = await pipeline.process_inbound_message(_tap(a.phone_number, f"OFFER:ACCEPT:{case.case_id}:{case.offer_id}"))
    assert late["action"] == "offer_already_yours"


@pytest.mark.asyncio
async def test_whatsapp_decline_and_late_accept_replies(offers):
    a, b = offers.doctor("A"), offers.doctor("B", handled=3)
    case = await doctor_offers.start_offer(offers.case())
    res = await pipeline.process_inbound_message(_tap(a.phone_number, f"OFFER:DECLINE:{case.case_id}:{case.offer_id}"))
    assert res["action"] == "offer_declined"
    assert "another doctor" in [c for c in offers.sent_to(a.phone_number) if c[0] == "send_text_message"][-1][1][1]
    late = await pipeline.process_inbound_message(_tap(a.phone_number, f"OFFER:ACCEPT:{case.case_id}:{case.offer_id}"))
    assert late["action"] == "offer_taken"
    assert doctor_offers.TAKEN_TEXT in [c[1][1] for c in offers.sent_to(a.phone_number) if c[0] == "send_text_message"]


# ---------------------------------------------------------------------------- portal endpoints
@pytest.mark.asyncio
async def test_portal_accept_and_decline_endpoints(offers):
    from httpx import ASGITransport, AsyncClient
    from app.main import app
    a, b = offers.doctor("A"), offers.doctor("B", handled=3)
    case = await doctor_offers.start_offer(offers.case(esi=3))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(f"/api/v1/cases/{case.case_id}/offer/decline", json={"doctor_id": a.doctor_id})
        assert r.status_code == 200 and r.json()["offered_doctor_ids"] == [b.doctor_id]
        r = await client.post(f"/api/v1/cases/{case.case_id}/offer/accept", json={"doctor_id": a.doctor_id})
        assert r.status_code == 409                               # no longer offered to A
        r = await client.post(f"/api/v1/cases/{case.case_id}/offer/accept", json={"doctor_id": b.doctor_id})
        assert r.status_code == 200 and r.json()["assigned_doctor_id"] == b.doctor_id
        r = await client.post(f"/api/v1/cases/{case.case_id}/offer/accept", json={})
        assert r.status_code == 400                               # local admin without a doctor_id


# ---------------------------------------------------------------------------- WhatsApp payloads
@pytest.mark.asyncio
async def test_offer_template_falls_back_to_reply_buttons():
    from app.messaging.providers import MockProvider
    from app.services.whatsapp_service import WhatsAppService

    class RejectsTemplates(MockProvider):
        async def send_payload(self, to_phone, payload):
            if payload["type"] == "template":
                self.sent.append(payload)
                return self._result("ERROR", to_phone, detail="template not approved")
            return await super().send_payload(to_phone, payload)

    provider = RejectsTemplates()
    res = await WhatsAppService(provider).send_doctor_case_offer(
        "+923001112223", 1, "Cardiology", "Chest pain", "45 seconds", "OFFER:ACCEPT:C:O", "OFFER:DECLINE:C:O")
    template, interactive = provider.sent
    assert template["template"]["name"] == settings.WHATSAPP_OFFER_TEMPLATE_NAME
    assert template["template"]["components"][0]["parameters"][0] == {"type": "text", "text": "1"}
    assert template["template"]["components"][1] == {"type": "button", "sub_type": "quick_reply", "index": "0",
                                                     "parameters": [{"type": "payload", "payload": "OFFER:ACCEPT:C:O"}]}
    assert template["template"]["components"][2]["index"] == "1"
    assert interactive["interactive"]["type"] == "button"
    assert [b["reply"]["id"] for b in interactive["interactive"]["action"]["buttons"]] == ["OFFER:ACCEPT:C:O", "OFFER:DECLINE:C:O"]
    assert res["channel"] == "interactive" and res["template_error"] == "template not approved"


def test_template_url_buttons_follow_quick_replies():
    from app.messaging.payloads import template_message
    t = template_message("+1", "n", "en", ["a"], quick_reply_payloads=["P"], url_suffixes=["CASE-1"])
    assert t["template"]["components"][2] == {"type": "button", "sub_type": "url", "index": "1",
                                              "parameters": [{"type": "text", "text": "CASE-1"}]}


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


def test_conditional_case_updates_in_dynamodb(db):
    case = db.save_case(EmergencyCase(case_id="CASE-DB1", user_phone="+1", patient_name="P", transcript="t",
                                      detected_language="Urdu", esi_level=1, severity="CRITICAL",
                                      status="DOCTOR_HANDOVER"))
    assert case.offer_id is None
    # expect None matches a missing / null field
    opened = db.update_case_if("CASE-DB1", {"doctor_offer_status": "PENDING", "offer_id": "O1",
                                            "offered_doctor_ids": ["A", "B"]}, expect={"offer_id": None})
    assert opened.offer_id == "O1"
    assert db.update_case_if("CASE-DB1", {"offer_id": "O2"}, expect={"offer_id": None}) is None   # stale
    # decline: member + not_member + append
    declined = db.update_case_if("CASE-DB1", {}, expect={"offer_id": "O1"}, member=("offered_doctor_ids", "A"),
                                 not_member=("declined_doctor_ids", "A"), append={"declined_doctor_ids": ["A"]})
    assert declined.declined_doctor_ids == ["A"]
    assert db.update_case_if("CASE-DB1", {}, expect={"offer_id": "O1"}, member=("offered_doctor_ids", "A"),
                             not_member=("declined_doctor_ids", "A"), append={"declined_doctor_ids": ["A"]}) is None
    assert db.update_case_if("CASE-DB1", {"assigned_doctor_id": "C"}, member=("offered_doctor_ids", "C")) is None
    won = db.update_case_if("CASE-DB1", {"assigned_doctor_id": "B", "doctor_offer_status": "ACCEPTED"},
                            expect={"offer_id": "O1", "doctor_offer_status": "PENDING", "assigned_doctor_id": None},
                            member=("offered_doctor_ids", "B"), not_member=("declined_doctor_ids", "B"))
    assert won.assigned_doctor_id == "B"
    assert db.update_case_if("CASE-DB1", {"assigned_doctor_id": "A"},
                             expect={"doctor_offer_status": "PENDING"}) is None
    fresh = DynamoDBService().get_case("CASE-DB1")
    assert (fresh.assigned_doctor_id, fresh.declined_doctor_ids, fresh.doctor_offer_status) == ("B", ["A"], "ACCEPTED")
    assert db.update_case_if("CASE-MISSING", {"offer_id": "X"}) is None
