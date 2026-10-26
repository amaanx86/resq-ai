"""AGENT-6 / OFFER-1 / OFFER-4: ranking of the FREE active doctors (a doctor with an open case or a
pending offer is skipped: one case per doctor), with the General Emergency / any-active fallbacks. The tool assigns nobody and never touches cases_handled (the doctor
who accepts the offer gets the case, app.doctor_offers). Works through dynamo_service's public
methods only."""
import uuid

import pytest

from app.agents import tools
from app.agents.tools import match_specialist_doctor_tool, rank_candidates
from app.models.schemas import DoctorProfile, EmergencyCase
from app.services.dynamo_service import dynamo_service


def _doc(doc_id, specialty, active=True, handled=0):
    return DoctorProfile(doctor_id=doc_id, name=f"Dr. {doc_id}", phone_number="+920000000", license_no="L",
                         specialty=specialty, bio="b", is_active=active, cases_handled=handled)


class FakeDirectory:
    """Stands in for dynamo_service's doctor/case queries (the tool's only data access)."""

    def __init__(self, doctors, open_cases=None):
        self.doctors = {d.doctor_id: d for d in doctors}
        self.open_cases = dict(open_cases or {})
        self.increments = []
        self.excluded = []

    def get_available_doctors_by_specialty(self, specialty):
        return [d for d in self.doctors.values() if d.specialty.lower() == specialty.lower() and d.is_active]

    def list_doctors(self):
        return list(self.doctors.values())

    def open_case_loads(self, exclude_case_id=None):
        self.excluded.append(exclude_case_id)
        return dict(self.open_cases)

    def increment_doctor_cases_handled(self, doctor_id):
        self.increments.append(doctor_id)


@pytest.fixture
def directory(monkeypatch):
    def install(doctors, open_cases=None):
        fake = FakeDirectory(doctors, open_cases)
        for name in ("get_available_doctors_by_specialty", "list_doctors", "open_case_loads",
                     "increment_doctor_cases_handled"):
            monkeypatch.setattr(dynamo_service, name, getattr(fake, name))
        return fake
    return install


def _ids(res):
    return [c["doctor_id"] for c in res["candidates"]]


@pytest.mark.asyncio
async def test_ranks_only_free_specialists_and_assigns_nobody(directory):
    fake = directory(
        [_doc("CARD-A", "Cardiology", handled=2), _doc("CARD-B", "Cardiology", handled=50),
         _doc("CARD-C", "Cardiology", handled=1)],
        open_cases={"CARD-A": 3, "CARD-B": 0, "CARD-C": 1},
    )
    res = await match_specialist_doctor_tool("Cardiology", "+92300", "chest pain")
    assert res["status"] == "OFFER_PENDING"
    assert _ids(res) == ["CARD-B"]                        # busy doctors are skipped (one case per doctor)
    assert res["candidates"][0] == {"doctor_id": "CARD-B", "doctor_name": "Dr. CARD-B",
                                    "specialty": "Cardiology", "match_tier": "specialty"}
    assert res["requested_specialty"] == "Cardiology" and "doctor_id" not in res
    assert fake.increments == []                          # counted only when a doctor accepts


@pytest.mark.asyncio
async def test_free_doctors_are_ordered_by_lifetime_cases(directory):
    directory([_doc("N-1", "Neurology", handled=9), _doc("N-2", "Neurology", handled=3),
               _doc("N-3", "Neurology", handled=0)], open_cases={"N-3": 1})
    assert _ids(await match_specialist_doctor_tool("Neurology", "+1", "s")) == ["N-2", "N-1"]


@pytest.mark.asyncio
async def test_everyone_busy_returns_no_doctor_available(directory):
    directory([_doc("N-1", "Neurology"), _doc("GE-1", "General Emergency")], open_cases={"N-1": 1, "GE-1": 1})
    res = await match_specialist_doctor_tool("Neurology", "+1", "s")
    assert res["status"] == "NO_DOCTOR_AVAILABLE" and res["candidates"] == []


def test_lock_on_another_case_makes_a_doctor_busy_until_it_expires(directory):
    from datetime import datetime, timedelta, timezone
    later = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
    earlier = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    offered = _doc("C-OFFER", "Cardiology").model_copy(update={"busy_case_id": "CASE-X", "busy_kind": "OFFER",
                                                                 "busy_until": later})
    expired = _doc("C-EXPIRED", "Cardiology").model_copy(update={"busy_case_id": "CASE-X", "busy_kind": "OFFER",
                                                                   "busy_until": earlier})
    stale = _doc("C-STALE", "Cardiology").model_copy(update={"busy_case_id": "CASE-OLD", "busy_kind": "CASE"})
    fake = directory([offered, expired, stale])
    # A CASE lock counts only through the open cases (a closed case's lock is stale); an offer lock
    # counts until it expires; a lock on the case being offered never does.
    assert [d.doctor_id for d, _ in rank_candidates("Cardiology")] == ["C-EXPIRED", "C-STALE"]
    assert [d.doctor_id for d, _ in rank_candidates("Cardiology", case_id="CASE-X")] == \
        ["C-EXPIRED", "C-OFFER", "C-STALE"]
    assert fake.excluded[-1] == "CASE-X"                  # its own pending offer does not load them


@pytest.mark.asyncio
async def test_tiers_follow_specialty_then_general_emergency_then_any_active(directory):
    directory([
        _doc("TRAUMA-OFF", "Trauma", active=False),
        _doc("TRAUMA-ON", "Trauma", handled=7),
        _doc("GE-BUSY", "General Emergency", handled=0),
        _doc("EM-FREE", "Emergency Medicine", handled=40),
        _doc("CARD-ON", "Cardiology"),
    ], open_cases={"GE-BUSY": 2, "EM-FREE": 0})
    res = await match_specialist_doctor_tool("Trauma", "+1", "accident")
    assert _ids(res) == ["TRAUMA-ON", "EM-FREE", "CARD-ON"]   # never the off-duty or the busy one
    assert [c["match_tier"] for c in res["candidates"]] == ["specialty", "general_emergency", "any_active"]


@pytest.mark.asyncio
async def test_falls_back_to_any_active_doctor_never_an_off_duty_one(directory):
    directory([_doc("PULMO-OFF", "Pulmonology", active=False, handled=0), _doc("CARD-ON", "Cardiology", handled=99)])
    res = await match_specialist_doctor_tool("Pulmonology", "+1", "breathless")
    assert _ids(res) == ["CARD-ON"] and res["candidates"][0]["match_tier"] == "any_active"


def test_rank_candidates_skips_excluded_doctors(directory):
    directory([_doc("C-1", "Cardiology"), _doc("C-2", "Cardiology", handled=5), _doc("GE-1", "General Emergency")])
    assert [d.doctor_id for d, _ in rank_candidates("Cardiology", exclude=["C-1"])] == ["C-2", "GE-1"]
    assert rank_candidates("Cardiology", exclude=["C-1", "C-2", "GE-1"]) == []


@pytest.mark.asyncio
async def test_no_active_doctor_returns_no_doctor_available(directory):
    directory([_doc("X-OFF", "Cardiology", active=False)])
    res = await match_specialist_doctor_tool("Cardiology", "+1", "s")
    assert res["status"] == "NO_DOCTOR_AVAILABLE" and res["candidates"] == []


@pytest.mark.asyncio
async def test_same_case_is_ranked_once(directory):
    fake = directory([_doc("C-1", "Cardiology"), _doc("C-2", "Cardiology")])
    case_id = f"CASE-D{uuid.uuid4().hex[:8].upper()}"
    first = await match_specialist_doctor_tool("Cardiology", "+1", "s", case_id=case_id)
    fake.open_cases["C-1"] = 5                            # the ranking would change now (C-1 busy)...
    second = await match_specialist_doctor_tool("Cardiology", "+1", "s", case_id=case_id)
    assert _ids(second) == _ids(first) and second["idempotent_replay"] is True   # ...but it is stored


@pytest.mark.asyncio
async def test_open_case_load_from_the_real_store_counts_assigned_and_offered_cases():
    """End to end against dynamo_service (in-memory here): ACTIVE / DOCTOR_HANDOVER cases assigned to a
    doctor or currently offered to them count; closed cases and finished offers do not."""
    specialty = f"TestSpecialty{uuid.uuid4().hex[:6]}"
    busy, offered, free, off = (_doc(f"{specialty}-BUSY", specialty), _doc(f"{specialty}-OFFERED", specialty),
                                _doc(f"{specialty}-FREE", specialty, handled=500),
                                _doc(f"{specialty}-OFF", specialty, active=False))
    for d in (busy, offered, free, off):
        dynamo_service.register_doctor(d)

    def case(status, **fields):
        dynamo_service.save_case(EmergencyCase(
            case_id=f"CASE-{uuid.uuid4().hex[:8].upper()}", user_phone="+1", patient_name="P", transcript="t",
            detected_language="English", esi_level=2, severity="CRITICAL", status=status, **fields))

    case("ACTIVE", assigned_doctor_id=busy.doctor_id)
    case("DOCTOR_HANDOVER", assigned_doctor_id=busy.doctor_id)
    case("RESOLVED", assigned_doctor_id=free.doctor_id)
    case("DOCTOR_HANDOVER", doctor_offer_status="PENDING", offered_doctor_ids=[offered.doctor_id])
    case("DOCTOR_HANDOVER", doctor_offer_status="EXHAUSTED", offered_doctor_ids=[free.doctor_id])

    declined = _doc(f"{specialty}-DECLINED", specialty)
    dynamo_service.register_doctor(declined)
    case("DOCTOR_HANDOVER", doctor_offer_status="PENDING", offered_doctor_ids=[declined.doctor_id],
         declined_doctor_ids=[declined.doctor_id])

    assert dynamo_service.count_open_cases_for_doctor(busy.doctor_id) == 2
    assert dynamo_service.count_open_cases_for_doctor(offered.doctor_id) == 1
    assert dynamo_service.count_open_cases_for_doctor(free.doctor_id) == 0
    assert dynamo_service.count_open_cases_for_doctor(declined.doctor_id) == 0   # declined: free again
    res = await match_specialist_doctor_tool(specialty, "+1", "s")
    ids = _ids(res)
    assert free.doctor_id in ids and declined.doctor_id in ids
    assert offered.doctor_id not in ids and busy.doctor_id not in ids and off.doctor_id not in ids


def test_tool_module_never_reads_mock_doctors():
    import inspect
    assert "_mock_doctors" not in inspect.getsource(tools)
