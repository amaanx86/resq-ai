"""DATA-1 / DATA-3: real KPIs (no padding unless DEMO_MODE), ESI split, response time, monthly counters."""
import pytest
import boto3
from fastapi import FastAPI
from fastapi.testclient import TestClient
from moto import mock_aws

from app.config import settings
from app.models.schemas import EmergencyCase, UserProfile
from app.routers import analytics
from app.services.dynamo_service import DynamoDBService, create_resq_table

MONTH = "2026-03"


def _case(case_id, esi, *, language="Urdu", specialty=None, doctor=None, received=None, dispatched=None,
          created=f"{MONTH}-15T10:00:00+00:00", status=None) -> EmergencyCase:
    return EmergencyCase(
        case_id=case_id, user_phone="+923000000000", patient_name="P", transcript="t",
        detected_language=language, esi_level=esi,
        severity={1: "CRITICAL", 2: "CRITICAL", 3: "MODERATE"}.get(esi, "LOW"),
        status=status or ("DOCTOR_HANDOVER" if esi <= 2 else "ACTIVE" if esi == 3 else "DISMISSED"),
        matched_specialty=specialty, assigned_doctor_name=doctor,
        received_at=received, dispatched_at=dispatched, created_at=created,
    )


def _sample_cases():
    return [
        _case("C1", 1, specialty="Cardiology", doctor="Dr. A",
              received=f"{MONTH}-15T10:00:00+00:00", dispatched=f"{MONTH}-15T10:00:04+00:00"),
        _case("C2", 2, language="Hindi", specialty="Trauma", doctor="Dr. B",
              received=f"{MONTH}-15T11:00:00+00:00", dispatched=f"{MONTH}-15T11:00:06+00:00"),
        _case("C3", 3, language="English", specialty="General Emergency"),
        _case("C4", 5),
        # Another month: must not leak into MONTH
        _case("C5", 1, specialty="Neurology", doctor="Dr. C", created="2026-02-10T09:00:00+00:00"),
    ]


def _assert_sample_summary(s):
    assert s.total_emergency_cases == 2          # ESI 1-2
    assert s.total_urgent_cases == 1             # ESI 3
    assert s.total_false_alarms == 1             # ESI 4-5
    assert s.avg_response_time_seconds == pytest.approx(5.0)
    assert s.cases_by_specialty == {"Cardiology": 1, "Trauma": 1, "General Emergency": 1}
    assert s.monthly_doctor_calls == {"Dr. A": 1, "Dr. B": 1}
    assert s.languages_handled == {"Urdu": 2, "Hindi": 1, "English": 1}
    assert s.demo_mode is False


@pytest.fixture
def mem():
    svc = DynamoDBService()  # conftest forces mock mode: in-memory store
    assert svc.mock_mode
    return svc


def test_empty_month_has_no_padding(mem):
    s = mem.get_analytics_summary("2030-01")
    assert s.total_emergency_cases == 0
    assert s.total_urgent_cases == 0
    assert s.total_false_alarms == 0
    assert s.monthly_doctor_calls == {} and s.cases_by_specialty == {} and s.languages_handled == {}
    assert s.avg_response_time_seconds is None
    assert s.total_registered_users == len(mem._mock_users)
    assert s.demo_mode is False


def test_in_memory_split_and_average(mem):
    for c in _sample_cases():
        mem.save_case(c)
    _assert_sample_summary(mem.get_analytics_summary(MONTH))
    assert mem.get_analytics_summary("2026-02").total_emergency_cases == 1


def test_in_memory_counters_only_on_first_create(mem):
    case = _case("C-RESAVE", 1, specialty="Cardiology", doctor="Dr. A")
    mem.save_case(case)
    mem.save_case(case)
    mem.save_case(case.model_copy(update={"status": "RESOLVED"}))
    mem.update_case("C-RESAVE", {"status": "DOCTOR_HANDOVER"})
    m = mem.get_month_metrics(MONTH)
    assert m["cases_total"] == 1 and m["esi_1_2"] == 1 and m["doc#Dr. A"] == 1


def test_average_ignores_incomplete_or_negative_timings(mem):
    mem.save_case(_case("T1", 1, received=f"{MONTH}-01T00:00:00Z", dispatched=f"{MONTH}-01T00:00:03Z"))
    mem.save_case(_case("T2", 1, received=f"{MONTH}-01T00:00:00+00:00"))                       # never dispatched
    mem.save_case(_case("T3", 1, received=f"{MONTH}-01T00:00:09+00:00", dispatched=f"{MONTH}-01T00:00:00+00:00"))
    assert mem.get_analytics_summary(MONTH).avg_response_time_seconds == pytest.approx(3.0)


def test_demo_mode_pads_and_flags(mem, monkeypatch):
    monkeypatch.setattr(settings, "DEMO_MODE", True)
    s = mem.get_analytics_summary("2030-01")
    assert s.demo_mode is True
    assert s.total_emergency_cases >= 48 and s.total_registered_users >= 142
    assert s.avg_response_time_seconds == 3.8
    assert s.cases_by_specialty


def test_seed_demo_cases_idempotent_and_realistic(mem, monkeypatch):
    monkeypatch.setattr(analytics, "dynamo_service", mem)
    first = analytics.seed_demo_cases()
    second = analytics.seed_demo_cases()
    assert first["created"] == first["cases_count"] == 5
    assert second["created"] == 0
    total = sum(m.get("cases_total", 0) for m in mem._mock_metrics.values())
    assert total == 5

    cases = {c.case_id: c for c in mem.list_all_cases()}
    for cid in ("CASE-DEMO-01", "CASE-DEMO-02", "CASE-DEMO-03"):
        c = cases[cid]
        assert c.esi_level in (1, 2) and c.received_at and c.dispatched_at
        assert c.clinical_summary and c.reasoning and c.tool_results
        assert {t["tool"] for t in c.tool_results} == {"alert_emergency_contact", "match_specialist_doctor"}
        assert c.location_requested_at
    assert cases["CASE-DEMO-01"].location_shared_at and cases["CASE-DEMO-01"].ambulance_called_at is None
    assert cases["CASE-DEMO-02"].ambulance_called_at and cases["CASE-DEMO-02"].location_alert_status == "SENT"
    assert cases["CASE-DEMO-05"].esi_level == 3 and cases["CASE-DEMO-05"].location_requested_at is None
    assert cases["CASE-DEMO-04"].dispatched_at is None


def test_summary_route_validates_month(mem, monkeypatch):
    monkeypatch.setattr(analytics, "dynamo_service", mem)
    app = FastAPI()
    app.include_router(analytics.router)
    client = TestClient(app)
    assert client.get("/api/v1/analytics/summary?month=2026-13").status_code == 422
    body = client.get(f"/api/v1/analytics/summary?month={MONTH}").json()
    assert body["total_emergency_cases"] == 0 and body["avg_response_time_seconds"] is None
    assert client.get("/api/v1/analytics/summary").status_code == 200


# ---------------------------------------------------------------- DynamoDB (moto)
@pytest.fixture
def ddb(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setattr(settings, "MOCK_AWS", False)
    monkeypatch.setattr(settings, "SEED_DEMO_DATA", False)
    monkeypatch.setattr(settings, "AWS_ENDPOINT_URL", None)
    with mock_aws():
        # Real-AWS mode only checks the table (Terraform owns it), so create it first.
        create_resq_table(boto3.client("dynamodb", region_name=settings.AWS_REGION), settings.DYNAMODB_TABLE_NAME)
        svc = DynamoDBService()
        assert not svc.mock_mode and svc.table is not None
        yield svc


def _metric_item(svc, month=MONTH):
    return svc.table.get_item(Key={"PK": f"METRIC#{month}", "SK": "SUMMARY"}).get("Item")


def test_dynamodb_counters_increment_once_per_new_case(ddb):
    case = _case("D1", 1, specialty="Cardiology", doctor="Dr. A")
    ddb.save_case(case)
    ddb.save_case(case)                                           # re-save
    ddb.save_case(case.model_copy(update={"status": "RESOLVED"}))  # status change via save
    ddb.update_case("D1", {"status": "DOCTOR_HANDOVER"})
    item = _metric_item(ddb)
    assert item["cases_total"] == 1 and item["esi_1_2"] == 1
    assert item["lang#Urdu"] == 1 and item["spec#Cardiology"] == 1 and item["doc#Dr. A"] == 1

    ddb.save_case(_case("D2", 4, language="English"))
    item = _metric_item(ddb)
    assert item["cases_total"] == 2 and item["esi_4_5"] == 1 and item["lang#English"] == 1


def test_dynamodb_summary_reads_metrics_and_gsi_without_case_scan(ddb, monkeypatch):
    for c in _sample_cases():
        ddb.save_case(c)
    ddb.save_user(UserProfile(phone_number="+923001112222", name="U1"))
    ddb.save_user(UserProfile(phone_number="+923001113333", name="U2"))

    scans = []
    real_scan = ddb.table.scan

    def spy_scan(**kwargs):
        scans.append(kwargs)
        return real_scan(**kwargs)

    monkeypatch.setattr(ddb.table, "scan", spy_scan)
    s = ddb.get_analytics_summary(MONTH)
    _assert_sample_summary(s)
    assert s.total_registered_users == 2
    # The only scan is the paginated user COUNT; cases come from the metric record + GSI_Analytics.
    assert all(k.get("Select") == "COUNT" for k in scans)
    assert _metric_item(ddb, "2026-02")["esi_1_2"] == 1


def test_dynamodb_month_without_metric_record_is_rebuilt_from_gsi(ddb):
    legacy = _case("L1", 2, specialty="Trauma", created="2025-11-03T08:00:00+00:00",
                   received="2025-11-03T07:59:55+00:00", dispatched="2025-11-03T08:00:00+00:00")
    from app.services.dynamo_service import _to_item
    ddb.table.put_item(Item={"PK": "CASE#L1", "SK": "METADATA", "GSI2PK": "MONTH#2025-11",
                             "GSI2SK": legacy.created_at, "data": _to_item(legacy)})
    assert _metric_item(ddb, "2025-11") is None
    s = ddb.get_analytics_summary("2025-11")
    assert s.total_emergency_cases == 1 and s.cases_by_specialty == {"Trauma": 1}
    assert s.avg_response_time_seconds == pytest.approx(5.0)
