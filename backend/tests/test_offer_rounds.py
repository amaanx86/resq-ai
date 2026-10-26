"""OFFER-3..5: offer rounds of three for ESI 1-2, one case per doctor (the doctor lock), and cases
that wait for a free doctor (app/doctor_offers.py, docs/design/doctor-whatsapp.md).

Uses the `Offers` helper of test_doctor_offers: each test has only its own doctors, and internal tasks
are recorded instead of queued."""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import boto3
import pytest
from moto import mock_aws

from app import admin_alerts, doctor_offers, pipeline
from app.config import settings
from app.models.schemas import DoctorProfile, EmergencyCase, InboundWhatsAppMessage, InternalTask
from app.services.dynamo_service import DynamoDBService, create_resq_table, dynamo_service
from test_doctor_offers import Offers, get
from test_pipeline_support import now_iso


@pytest.fixture
def offers(monkeypatch):
    return Offers(monkeypatch)


def doc(doctor_id: str) -> DoctorProfile:
    return dynamo_service.get_doctor(doctor_id)


def offered_names(case: EmergencyCase):
    return [doc(d).name for d in case.offered_doctor_ids]


async def recheck(case: EmergencyCase):
    return await doctor_offers.handle_recheck(InternalTask(
        task_id="r", kind=doctor_offers.RECHECK_TASK, payload={"case_id": case.case_id, "since": case.no_doctor_since},
        run_at=now_iso()))


# ============================================================================ OFFER-3: rounds
def test_fanout_and_timeouts_per_esi():
    assert [doctor_offers.offer_fanout(e) for e in (1, 2, 3, 4, None)] == [3, 3, 1, 1, 1]
    assert [doctor_offers.offer_timeout_seconds(e) for e in (1, 2, 3)] == [60, 60, 300]
    assert settings.DOCTOR_OFFER_FANOUT_URGENT == 3 and not hasattr(settings, "DOCTOR_OFFER_MAX_ROUNDS")


@pytest.mark.asyncio
async def test_esi2_goes_to_three_doctors_then_the_next_three(offers):
    doctors = [offers.doctor(n, handled=i) for i, n in enumerate("ABCDEFG")]
    case = await doctor_offers.start_offer(offers.case(esi=2))
    assert offered_names(case) == ["Dr. A", "Dr. B", "Dr. C"]
    assert offers.timeouts[-1][2] == 60
    assert offers.sent_to(doctors[0].phone_number)[0][1][4] == "60 seconds"
    await offers.timeout(case.case_id, case.offer_id)
    second = get(case.case_id)
    assert offered_names(second) == ["Dr. D", "Dr. E", "Dr. F"]                 # the next three
    await offers.timeout(case.case_id, second.offer_id)
    third = get(case.case_id)
    assert offered_names(third) == ["Dr. G"]                                    # whoever is left
    await offers.timeout(case.case_id, third.offer_id)
    final = get(case.case_id)
    assert final.doctor_offer_status == "EXHAUSTED" and final.no_doctor_reason == doctor_offers.ALL_DECLINED
    assert [r["outcome"] for r in final.offer_history] == ["TIMED_OUT"] * 3
    assert set(final.declined_doctor_ids) == {d.doctor_id for d in doctors}


@pytest.mark.asyncio
async def test_there_is_no_round_limit(offers):
    for i in range(5):
        offers.doctor(f"R{i}", handled=i)
    case = await doctor_offers.start_offer(offers.case(esi=3))                 # one doctor per round
    for _ in range(5):
        current = get(case.case_id)
        assert current.doctor_offer_status == "PENDING"
        await offers.timeout(case.case_id, current.offer_id)
    final = get(case.case_id)
    assert len(final.offer_history) == 5 and final.doctor_offer_status == "EXHAUSTED"


@pytest.mark.asyncio
async def test_human_review_case_goes_to_three_doctors(offers):
    for n in "ABCD":
        offers.doctor(n)
    review = offers.case(esi=2).model_copy(update={"needs_human_review": True, "status": "ACTIVE"})
    case = await doctor_offers.start_offer(dynamo_service.save_case(review))
    assert len(case.offered_doctor_ids) == 3 and offers.timeouts[-1][2] == settings.DOCTOR_OFFER_TIMEOUT_ESI2


@pytest.mark.asyncio
async def test_escalation_restarts_the_round_with_three_doctors(offers):
    a, b, c, d = (offers.doctor(n, handled=i) for i, n in enumerate("ABCD"))
    case = await doctor_offers.start_offer(offers.case(esi=3))
    assert case.offered_doctor_ids == [a.doctor_id] and offers.timeouts[-1][2] == 300
    escalated = dynamo_service.update_case(case.case_id, {"esi_level": 1})
    after = await doctor_offers.start_offer(escalated, restart=True)
    assert after.offered_doctor_ids == [a.doctor_id, b.doctor_id, c.doctor_id]   # A is kept (same case)
    assert after.offer_history[0]["outcome"] == "SUPERSEDED" and offers.timeouts[-1][2] == 60
    assert all(doc(x).busy_case_id == case.case_id for x in after.offered_doctor_ids)
    assert doc(d.doctor_id).busy_case_id is None


# ============================================================================ OFFER-4: one case per doctor
@pytest.mark.asyncio
async def test_offer_accept_decline_and_timeout_move_the_doctor_lock(offers):
    a, b, c, d = (offers.doctor(n, handled=i) for i, n in enumerate("ABCD"))
    case = await doctor_offers.start_offer(offers.case(esi=1))
    for x in (a, b, c):
        locked = doc(x.doctor_id)
        assert (locked.busy_case_id, locked.busy_kind) == (case.case_id, "OFFER")
        assert locked.busy_until > case.offer_expires_at                       # offer + grace
    assert doc(d.doctor_id).busy_case_id is None
    await doctor_offers.decline_offer(case.case_id, case.offer_id, a)
    assert doc(a.doctor_id).busy_case_id is None                               # free as soon as they decline
    outcome, accepted = await doctor_offers.accept_offer(case.case_id, case.offer_id, b)
    assert outcome == doctor_offers.ACCEPTED
    assert (doc(b.doctor_id).busy_case_id, doc(b.doctor_id).busy_kind) == (case.case_id, "CASE")
    assert doc(c.doctor_id).busy_case_id is None                               # the other doctors are free


@pytest.mark.asyncio
async def test_a_doctor_on_a_case_is_not_offered_another(offers):
    a, b, c = offers.doctor("A"), offers.doctor("B", handled=1), offers.doctor("C", handled=2)
    first = await doctor_offers.start_offer(offers.case(esi=3))
    await doctor_offers.accept_offer(first.case_id, first.offer_id, a)
    second = await doctor_offers.start_offer(offers.case(esi=1))
    assert second.offered_doctor_ids == [b.doctor_id, c.doctor_id]               # A holds a case
    third = await doctor_offers.start_offer(offers.case(esi=1))
    assert third.doctor_offer_status == "EXHAUSTED"                             # B and C hold an offer
    assert third.no_doctor_reason == doctor_offers.NO_DOCTOR_FREE


@pytest.mark.asyncio
async def test_two_cases_at_the_same_moment_never_share_a_doctor(offers):
    doctors = [offers.doctor(n) for n in "ABCD"]
    cases = await asyncio.gather(*(doctor_offers.start_offer(offers.case(esi=2)) for _ in range(3)))
    offered = [d for c in cases for d in c.offered_doctor_ids]
    assert len(offered) == len(set(offered)) == 4                               # every doctor once
    assert sorted(offered) == sorted(d.doctor_id for d in doctors)
    # How the 4 doctors split depends on how the claims interleave (3+1+0, 2+1+1, ...); a case
    # waits exactly when it got nobody.
    for c in cases:
        assert (c.doctor_offer_status == "EXHAUSTED") == (not c.offered_doctor_ids)
        assert len(c.offered_doctor_ids) <= 3
    for d in doctors:
        holder = dynamo_service.get_doctor(d.doctor_id).busy_case_id
        assert holder == next(c.case_id for c in cases if d.doctor_id in c.offered_doctor_ids)


@pytest.mark.asyncio
async def test_a_round_skips_a_doctor_whose_lock_was_taken_meanwhile(offers, monkeypatch):
    a, b = offers.doctor("A"), offers.doctor("B", handled=1)
    real_claim = dynamo_service.claim_doctor

    def claim(doctor_id, case_id, kind, until=None, replace_case_id=None):
        if doctor_id == a.doctor_id:
            return None                                                         # lost the race
        return real_claim(doctor_id, case_id, kind, until=until, replace_case_id=replace_case_id)
    monkeypatch.setattr(dynamo_service, "claim_doctor", claim)
    case = await doctor_offers.start_offer(offers.case(esi=3))
    assert case.offered_doctor_ids == [b.doctor_id]


@pytest.mark.asyncio
async def test_accept_while_holding_another_case_is_busy(offers):
    a = offers.doctor("A")
    held = await doctor_offers.start_offer(offers.case(esi=3))
    await doctor_offers.accept_offer(held.case_id, held.offer_id, a)
    # A second case offered to A anyway (e.g. a round started before the lock existed).
    other = dynamo_service.save_case(offers.case(esi=2).model_copy(update={
        "doctor_offer_status": "PENDING", "offer_id": "OFR-X", "offered_doctor_ids": [a.doctor_id]}))
    outcome, case = await doctor_offers.accept_offer(other.case_id, "OFR-X", doc(a.doctor_id))
    assert outcome == doctor_offers.BUSY and get(other.case_id).assigned_doctor_id is None
    assert doc(a.doctor_id).busy_case_id == held.case_id
    assert "already handling a case with Ahmed" in doctor_offers.busy_text(doc(a.doctor_id))


@pytest.mark.asyncio
async def test_busy_accept_replies_on_whatsapp_and_409_in_the_portal(offers):
    from httpx import ASGITransport, AsyncClient
    from app.main import app
    a = offers.doctor("A")
    held = await doctor_offers.start_offer(offers.case(esi=3))
    await doctor_offers.accept_offer(held.case_id, held.offer_id, a)
    other = dynamo_service.save_case(offers.case(esi=2).model_copy(update={
        "doctor_offer_status": "PENDING", "offer_id": "OFR-Y", "offered_doctor_ids": [a.doctor_id]}))
    res = await doctor_offers.handle_button(None, doc(a.doctor_id), ["ACCEPT", other.case_id, "OFR-Y"])
    assert res["action"] == "offer_busy"
    assert "already handling" in offers.whatsapp.texts()[-1] and "@resq close" in offers.whatsapp.texts()[-1]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(f"/api/v1/cases/{other.case_id}/offer/accept", json={"doctor_id": a.doctor_id})
    assert r.status_code == 409 and "already handling" in r.json()["detail"]


@pytest.mark.asyncio
async def test_a_stale_case_lock_does_not_block_the_doctor(offers):
    a = offers.doctor("A")
    closed = dynamo_service.save_case(offers.case(esi=3).model_copy(update={"status": "RESOLVED"}))
    dynamo_service.claim_doctor(a.doctor_id, closed.case_id, "CASE")             # the release was missed
    case = await doctor_offers.start_offer(offers.case(esi=3))
    assert case.offered_doctor_ids == [a.doctor_id]
    assert (await doctor_offers.accept_offer(case.case_id, case.offer_id, a))[0] == doctor_offers.ACCEPTED


@pytest.mark.asyncio
async def test_an_expired_offer_lock_does_not_block_the_doctor(offers):
    a = offers.doctor("A")
    gone = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    dynamo_service.claim_doctor(a.doctor_id, "CASE-OTHER", "OFFER", until=gone)
    case = await doctor_offers.start_offer(offers.case(esi=3))
    assert case.offered_doctor_ids == [a.doctor_id] and doc(a.doctor_id).busy_case_id == case.case_id


@pytest.mark.asyncio
async def test_admin_cannot_assign_a_busy_doctor(offers):
    from httpx import ASGITransport, AsyncClient
    from app.main import app
    a = offers.doctor("A")
    held = await doctor_offers.start_offer(offers.case(esi=3))
    await doctor_offers.accept_offer(held.case_id, held.offer_id, a)
    other = offers.case(esi=2)
    with pytest.raises(doctor_offers.DoctorBusyError) as err:
        await doctor_offers.assign_by_admin(other.case_id, doc(a.doctor_id), "admin@resq.dev")
    assert err.value.busy_case_id == held.case_id
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(f"/api/v1/cases/{other.case_id}/assign", json={"doctor_id": a.doctor_id})
    assert r.status_code == 409 and held.case_id in r.json()["detail"] and "Ahmed" in r.json()["detail"]
    assert get(other.case_id).assigned_doctor_id is None


@pytest.mark.asyncio
async def test_admin_assignment_frees_the_previous_doctor_and_the_offered_ones(offers):
    a, b, c = offers.doctor("A"), offers.doctor("B", handled=1), offers.doctor("C", handled=2)
    case = await doctor_offers.start_offer(offers.case(esi=1))
    assert set(case.offered_doctor_ids) == {a.doctor_id, b.doctor_id, c.doctor_id}
    manual = offers.doctor("Manual", handled=9)
    assigned = await doctor_offers.assign_by_admin(case.case_id, manual, "admin@resq.dev")
    assert assigned.assigned_doctor_id == manual.doctor_id and doc(manual.doctor_id).busy_kind == "CASE"
    assert all(doc(x.doctor_id).busy_case_id is None for x in (a, b, c))
    # Reassigning frees the first doctor and tells them.
    again = await doctor_offers.assign_by_admin(case.case_id, doc(a.doctor_id), "admin@resq.dev")
    assert again.assigned_doctor_id == a.doctor_id and doc(manual.doctor_id).busy_case_id is None
    assert any("reassigned to another doctor" in t for t in offers.whatsapp.texts())
    # Assigning the same doctor again changes nothing.
    assert (await doctor_offers.assign_by_admin(case.case_id, doc(a.doctor_id), "x")).assigned_doctor_id == a.doctor_id


@pytest.mark.asyncio
async def test_resolving_the_case_frees_the_doctor_and_remembers_it(offers, monkeypatch):
    monkeypatch.setattr(pipeline, "whatsapp_service", offers.whatsapp)
    a = offers.doctor("A")
    case = await doctor_offers.start_offer(offers.case(esi=3))
    await doctor_offers.accept_offer(case.case_id, case.offer_id, a)
    await pipeline.resolve_case(case.case_id, resolved_by="Dr. A")
    freed = doc(a.doctor_id)
    assert freed.busy_case_id is None and freed.last_case_id == case.case_id and freed.last_case_closed_at


@pytest.mark.asyncio
async def test_resolving_a_case_with_a_pending_offer_frees_the_offered_doctors(offers, monkeypatch):
    monkeypatch.setattr(pipeline, "whatsapp_service", offers.whatsapp)
    a, b = offers.doctor("A"), offers.doctor("B", handled=1)
    case = await doctor_offers.start_offer(offers.case(esi=2))
    await pipeline.resolve_case(case.case_id)
    assert doc(a.doctor_id).busy_case_id is None and doc(b.doctor_id).busy_case_id is None
    assert doc(a.doctor_id).last_case_id is None                               # they never held it


@pytest.mark.asyncio
async def test_hand_over_frees_the_doctor_and_offers_the_case_to_others(offers):
    a, b = offers.doctor("A"), offers.doctor("B", handled=1)
    case = await doctor_offers.start_offer(offers.case(esi=3))
    await doctor_offers.accept_offer(case.case_id, case.offer_id, a)
    handed = await doctor_offers.hand_over(case.case_id, doc(a.doctor_id))
    assert handed.assigned_doctor_id is None and handed.offered_doctor_ids == [b.doctor_id]
    assert a.doctor_id in handed.declined_doctor_ids                             # not asked again
    assert doc(a.doctor_id).busy_case_id is None and doc(a.doctor_id).last_case_id == case.case_id
    assert "HANDED_OVER" in [r["outcome"] for r in handed.offer_history]
    assert any("hand your case over" in t for t in offers.whatsapp.texts())
    # Not theirs any more: a second hand over does nothing.
    assert await doctor_offers.hand_over(case.case_id, doc(a.doctor_id)) is None


@pytest.mark.asyncio
async def test_hand_over_with_nobody_else_lets_the_case_wait(offers):
    a = offers.doctor("A")
    case = await doctor_offers.start_offer(offers.case(esi=2))
    await doctor_offers.accept_offer(case.case_id, case.offer_id, a)
    handed = await doctor_offers.hand_over(case.case_id, doc(a.doctor_id))
    assert handed.doctor_offer_status == "EXHAUSTED" and handed.no_doctor_reason == doctor_offers.ALL_DECLINED


# ============================================================================ OFFER-5: waiting cases
@pytest.mark.asyncio
async def test_a_waiting_case_is_offered_when_a_doctor_closes_theirs(offers, monkeypatch):
    monkeypatch.setattr(pipeline, "whatsapp_service", offers.whatsapp)
    a = offers.doctor("A")
    held = await doctor_offers.start_offer(offers.case(esi=3))
    await doctor_offers.accept_offer(held.case_id, held.offer_id, a)
    waiting = await doctor_offers.start_offer(offers.case(esi=1))
    assert waiting.doctor_offer_status == "EXHAUSTED"
    await pipeline.resolve_case(held.case_id, resolved_by="Dr. A")
    offered = get(waiting.case_id)
    assert offered.doctor_offer_status == "PENDING" and offered.offered_doctor_ids == [a.doctor_id]
    assert offered.no_doctor_since == waiting.no_doctor_since                  # kept for the waiting time


@pytest.mark.asyncio
async def test_waiting_cases_go_out_lowest_esi_first_then_oldest(offers):
    a = offers.doctor("A")
    held = await doctor_offers.start_offer(offers.case(esi=3))
    await doctor_offers.accept_offer(held.case_id, held.offer_id, a)
    urgent_old = await doctor_offers.start_offer(offers.case(esi=3))
    critical = await doctor_offers.start_offer(offers.case(esi=1))
    critical_later = await doctor_offers.start_offer(offers.case(esi=1))
    assert [c.doctor_offer_status for c in (urgent_old, critical, critical_later)] == ["EXHAUSTED"] * 3
    assert [c.case_id for c in dynamo_service.list_waiting_cases()][:3] == \
        [critical.case_id, critical_later.case_id, urgent_old.case_id]
    await doctor_offers.hand_over(held.case_id, doc(a.doctor_id))               # frees A
    # A declined `held` by handing it over; the first waiting case is the oldest ESI 1.
    assert get(critical.case_id).offered_doctor_ids == [a.doctor_id]
    assert get(critical_later.case_id).doctor_offer_status == "EXHAUSTED"
    assert get(urgent_old.case_id).doctor_offer_status == "EXHAUSTED"


@pytest.mark.asyncio
async def test_a_decliner_is_not_asked_again_but_another_free_doctor_is(offers):
    a = offers.doctor("A")
    case = await doctor_offers.start_offer(offers.case(esi=3))
    await doctor_offers.decline_offer(case.case_id, case.offer_id, a)
    assert get(case.case_id).doctor_offer_status == "EXHAUSTED"
    assert await doctor_offers.offer_waiting_cases() == []                       # only A is free: declined
    assert (await recheck(get(case.case_id)))["outcome"] == "still_waiting"
    b = offers.doctor("B")
    assert (await recheck(get(case.case_id)))["outcome"] == "offered"
    assert get(case.case_id).offered_doctor_ids == [b.doctor_id]


@pytest.mark.asyncio
async def test_recheck_reschedules_while_waiting_and_is_stale_after(offers):
    case = await doctor_offers.start_offer(offers.case(esi=2))
    waiting = get(case.case_id)
    offers.tasks.clear()
    assert (await recheck(waiting))["outcome"] == "still_waiting"
    assert offers.tasks == [(doctor_offers.RECHECK_TASK, {"case_id": case.case_id, "since": waiting.no_doctor_since},
                             settings.WAITING_CASE_RECHECK_SECONDS)]
    stale = await doctor_offers.handle_recheck(InternalTask(
        task_id="r", kind=doctor_offers.RECHECK_TASK, payload={"case_id": case.case_id, "since": "another period"},
        run_at=now_iso()))
    assert stale["outcome"] == "stale"
    dynamo_service.update_case(case.case_id, {"status": "RESOLVED"})
    assert (await recheck(waiting))["outcome"] == "stale"


@pytest.mark.asyncio
async def test_a_waiting_case_is_not_written_or_messaged_again_while_nobody_is_free(offers):
    case = await doctor_offers.start_offer(offers.case(esi=2))
    first = get(case.case_id)
    messages = len(dynamo_service.get_case_messages(case.case_id))
    tasks = len(offers.tasks)
    again = await doctor_offers.start_offer(first)
    assert again.no_doctor_since == first.no_doctor_since
    assert len(dynamo_service.get_case_messages(case.case_id)) == messages and len(offers.tasks) == tasks
    assert offers.sent_to(case.user_phone) == []


@pytest.mark.asyncio
async def test_a_second_waiting_period_tells_the_patient_again(offers):
    a = offers.doctor("A")
    case = await doctor_offers.start_offer(offers.case(esi=3))
    await doctor_offers.decline_offer(case.case_id, case.offer_id, a)          # period 1 (patient told)
    first = get(case.case_id)
    b = offers.doctor("B")
    await recheck(first)
    offered = get(case.case_id)
    await doctor_offers.decline_offer(case.case_id, offered.offer_id, doc(b.doctor_id))   # period 2
    second = get(case.case_id)
    assert second.no_doctor_since > first.no_doctor_since
    waits = [t for t in offers.whatsapp.texts() if t.startswith("⏳ No doctor is free")]
    assert len(waits) == 2 and "emergency department" in waits[0]                # the ESI 3 wording


@pytest.mark.asyncio
async def test_doctor_coming_on_duty_gets_the_waiting_case(offers):
    from httpx import ASGITransport, AsyncClient
    from app.main import app
    off = dynamo_service.register_doctor(DoctorProfile(
        doctor_id=f"DOC-OFF-{uuid.uuid4().hex[:4]}", name="Dr. Off", phone_number="+923001110000", license_no="L",
        specialty=offers.specialty, bio="b", is_active=False))
    case = await doctor_offers.start_offer(offers.case(esi=1))
    assert case.doctor_offer_status == "EXHAUSTED"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(f"/api/v1/doctors/{off.doctor_id}/status", params={"is_active": "true"})
    assert r.status_code == 200
    assert get(case.case_id).offered_doctor_ids == [off.doctor_id]


@pytest.mark.asyncio
async def test_a_newly_registered_doctor_gets_the_waiting_case(offers):
    from httpx import ASGITransport, AsyncClient
    from app.main import app
    case = await doctor_offers.start_offer(offers.case(esi=2))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post("/api/v1/doctors/register", json={
            "name": "Dr. New", "phone_number": "+923004445556", "license_no": "L", "specialty": offers.specialty,
            "bio": "b", "email": f"new{uuid.uuid4().hex[:6]}@resq.dev"})
    assert r.status_code == 200, r.text
    doctor_id = r.json()["doctor_id"]
    assert r.json()["phone_status"] == "PENDING"
    assert get(case.case_id).offered_doctor_ids == []                         # number not confirmed yet (DOC-8)
    res = await pipeline.process_inbound_message(InboundWhatsAppMessage(
        provider="meta", provider_message_id=f"wamid.{uuid.uuid4().hex}", sender_phone="+923004445556",
        timestamp=now_iso(), type="button", button_payload=f"DOCV:YES:{doctor_id}", button_text="Yes, it's me"))
    assert res["action"] == "doctor_number_confirmed"
    assert get(case.case_id).offered_doctor_ids == [doctor_id]


@pytest.mark.asyncio
async def test_a_decline_frees_the_doctor_for_a_waiting_case(offers):
    a, b = offers.doctor("A"), offers.doctor("B", handled=1)
    first = await doctor_offers.start_offer(offers.case(esi=2))                 # A and B
    waiting = await doctor_offers.start_offer(offers.case(esi=1))
    assert waiting.doctor_offer_status == "EXHAUSTED"
    await doctor_offers.decline_offer(first.case_id, first.offer_id, a)
    assert get(waiting.case_id).offered_doctor_ids == [a.doctor_id]             # A is free again
    assert get(first.case_id).doctor_offer_status == "PENDING"                 # B still has it


# ============================================================================ the doctor lock in DynamoDB
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


def _doctor(svc, doctor_id="D1", phone="+923001234000") -> DoctorProfile:
    return svc.register_doctor(DoctorProfile(doctor_id=doctor_id, name="Dr. D", phone_number=phone,
                                             license_no="L", specialty="Cardiology", bio="b"))


def test_doctor_lock_conditions_in_dynamodb(db):
    _doctor(db)
    soon = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    assert db.claim_doctor("D1", "C1", "OFFER", until=soon).busy_case_id == "C1"
    assert db.claim_doctor("D1", "C2", "OFFER", until=soon) is None              # busy with C1's offer
    assert db.claim_doctor("D1", "C1", "CASE").busy_kind == "CASE"              # same case: accept
    assert db.claim_doctor("D1", "C2", "OFFER", until=soon) is None              # holds C1
    assert db.claim_doctor("D1", "C2", "OFFER", replace_case_id="C9") is None    # not the stale lock
    assert db.claim_doctor("D1", "C2", "CASE", replace_case_id="C1").busy_case_id == "C2"   # stale C1 replaced
    assert db.release_doctor("D1", "C1") is None                                 # not locked to C1
    released = db.release_doctor("D1", "C2", closed=True)
    assert released.busy_case_id is None and released.last_case_id == "C2" and released.last_case_closed_at
    db.claim_doctor("D1", "C3", "OFFER", until=past)
    assert db.claim_doctor("D1", "C4", "OFFER", until=soon).busy_case_id == "C4"   # expired offer lock
    assert db.claim_doctor("MISSING", "C1", "OFFER", until=soon) is None
    fresh = DynamoDBService().get_doctor("D1")
    assert (fresh.busy_case_id, fresh.busy_kind, fresh.cases_handled) == ("C4", "OFFER", 0)
    assert db.update_doctor_fields("D1", {"last_inbound_at": "2026-10-21T10:00:00+00:00", "bogus": 1}).last_inbound_at


def test_doctor_lock_survives_other_doctor_updates_in_dynamodb(db):
    _doctor(db)
    db.claim_doctor("D1", "C1", "CASE")
    db.increment_doctor_cases_handled("D1")
    db.update_doctor_status("D1", False)
    fresh = DynamoDBService().get_doctor("D1")
    assert (fresh.busy_case_id, fresh.cases_handled, fresh.is_active) == ("C1", 1, False)


def test_waiting_cases_and_loads_in_dynamodb(db):
    def case(case_id, esi, **fields):
        return db.save_case(EmergencyCase(case_id=case_id, user_phone="+1", patient_name="P", transcript="t",
                                          detected_language="Urdu", esi_level=esi, severity="HIGH",
                                          status="DOCTOR_HANDOVER", **fields))
    case("W3", 3, doctor_offer_status="EXHAUSTED", no_doctor_since="2026-10-21T10:00:00+00:00")
    case("W1", 1, doctor_offer_status="EXHAUSTED", no_doctor_since="2026-10-21T11:00:00+00:00")
    case("P1", 1, doctor_offer_status="PENDING", offered_doctor_ids=["A", "B"], declined_doctor_ids=["B"])
    assert [c.case_id for c in db.list_waiting_cases()] == ["W1", "W3"]
    assert db.open_case_loads() == {"A": 1}
    assert db.open_case_loads(exclude_case_id="P1") == {}


def test_doctor_by_phone_scan_runs_at_most_once_a_minute(db, monkeypatch):
    calls = []
    real = db.list_doctors

    def counting():
        calls.append(1)
        return real()
    monkeypatch.setattr(db, "list_doctors", counting)
    for _ in range(5):
        assert db.get_doctor_by_phone("+923009999999") is None                   # a patient
    assert len(calls) == 1
    legacy = DoctorProfile(doctor_id="LEGACY", name="Dr. L", phone_number="+923007777777", license_no="L",
                           specialty="Cardiology", bio="b")
    db.table.put_item(Item=db._doctor_item(legacy))                             # saved without a pointer
    db._doctor_phone_scan = (0.0, {})                                           # a minute later
    assert db.get_doctor_by_phone("+923007777777").doctor_id == "LEGACY"
    assert db.table.get_item(Key={"PK": "DOCTORPHONE#+923007777777", "SK": "PROFILE"}).get("Item")
