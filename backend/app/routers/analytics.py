import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, HTTPException, Query
from app.models.schemas import AnalyticsSummary, EmergencyCase
from app.services.dynamo_service import dynamo_service

router = APIRouter(prefix="/api/v1/analytics", tags=["Analytics & KPIs"])

_MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


@router.get("/summary", response_model=AnalyticsSummary)
def get_analytics_summary(month: Optional[str] = Query(None, description="YYYY-MM; defaults to the current UTC month")):
    """Executive KPIs for one month, from the METRIC#<YYYY-MM> counters (real data unless DEMO_MODE)."""
    if month and not _MONTH.match(month):
        raise HTTPException(status_code=422, detail="month must be YYYY-MM")
    return dynamo_service.get_analytics_summary(month)


def _iso(t: datetime) -> str:
    return t.isoformat()


def _tool(name: str, start: datetime, seconds: float, **output: Any) -> Dict[str, Any]:
    return {"tool": name, "status": output.pop("status", "SUCCESS"), **output,
            "started_at": _iso(start), "finished_at": _iso(start + timedelta(seconds=seconds))}


def _demo_cases(now: datetime) -> List[EmergencyCase]:
    """Five demo scenarios with the audit fields a real pipeline run produces (DATA-1, DATA-2)."""
    cases: List[EmergencyCase] = []

    # Case 1: Urdu cardiac emergency (ESI 1), protocol done in 4.6 s, location shared with the
    # doctor and the emergency contact, ambulance not called yet
    received = now - timedelta(minutes=14)
    tools_start = received + timedelta(seconds=2.9)
    dispatched = received + timedelta(seconds=4.6)
    cases.append(EmergencyCase(
        case_id="CASE-DEMO-01",
        user_phone="+923009988776",
        patient_name="Zubair Ahmed (Uncle Tariq)",
        emergency_contact_phone="+923331122334",
        transcript="Mere uncle ko dil ka daura par raha hai, seene me shadeed dard hai, left arm me bhi dard ja raha hai!",
        detected_language="Urdu",
        esi_level=1,
        severity="CRITICAL",
        status="DOCTOR_HANDOVER",
        matched_specialty="Cardiology",
        assigned_doctor_id="DOC-CARDIO-01",
        assigned_doctor_name="Dr. Sarah Khan, MD",
        contact_call_id="CONN-99214-PK",
        location_requested_at=_iso(dispatched + timedelta(seconds=1)),
        location_latitude=24.860700,
        location_longitude=67.001100,
        location_name="Saddar",
        location_address="Abdullah Haroon Rd, Saddar, Karachi",
        location_shared_at=_iso(dispatched + timedelta(seconds=40)),
        location_alert_status="SENT",
        clinical_summary="Suspected acute myocardial infarction: severe chest pain radiating to the left arm.",
        reasoning="Crushing chest pain with left-arm radiation in an older male is a STEMI pattern; "
                  "time-critical, needs immediate resuscitation capability (ESI 1). Cardiology handover.",
        confidence_score=0.94,
        triage_engine="heuristic",
        provider="simulator",
        provider_message_id="demo-wamid-01",
        tool_results=[
            _tool("alert_emergency_contact", tools_start, 1.4, call_id="CONN-99214-PK", contact_phone="+923331122334"),
            _tool("match_specialist_doctor", tools_start, 0.6, doctor_id="DOC-CARDIO-01",
                  doctor_name="Dr. Sarah Khan, MD", specialty="Cardiology"),
        ],
        received_at=_iso(received),
        dispatched_at=_iso(dispatched),
        created_at=_iso(dispatched + timedelta(seconds=0.3)),
    ))

    # Case 2: Hindi road-traffic trauma (ESI 2), protocol done in 5.2 s, location shared, ambulance
    # called by the doctor
    received = now - timedelta(minutes=28)
    tools_start = received + timedelta(seconds=3.4)
    dispatched = received + timedelta(seconds=5.2)
    cases.append(EmergencyCase(
        case_id="CASE-DEMO-02",
        user_phone="+919811234567",
        patient_name="Amit Verma",
        emergency_contact_phone="+919876543210",
        transcript="Bhaiya highway par car accident ho gaya hai, sar se bahut khoon beh raha hai, jaldi ambulance bhejo!",
        detected_language="Hindi",
        esi_level=2,
        severity="CRITICAL",
        status="DOCTOR_HANDOVER",
        matched_specialty="Trauma",
        assigned_doctor_id="DOC-TRAUMA-03",
        assigned_doctor_name="Dr. James Mitchell, FACS",
        contact_call_id="CONN-33120-IN",
        location_requested_at=_iso(dispatched + timedelta(seconds=1)),
        location_latitude=28.556200,
        location_longitude=77.100000,
        location_address="NH 48, near Mahipalpur, New Delhi",
        location_shared_at=_iso(dispatched + timedelta(seconds=55)),
        location_alert_status="SENT",
        ambulance_called_at=_iso(dispatched + timedelta(minutes=2)),
        ambulance_called_by="Dr. James Mitchell, FACS",
        clinical_summary="High-speed road traffic collision with heavy scalp bleeding; possible head injury.",
        reasoning="Major mechanism of injury with active haemorrhage from the head; high-risk, "
                  "should not wait (ESI 2). Trauma surgeon handover.",
        confidence_score=0.91,
        triage_engine="heuristic",
        provider="simulator",
        provider_message_id="demo-wamid-02",
        tool_results=[
            _tool("alert_emergency_contact", tools_start, 1.8, call_id="CONN-33120-IN", contact_phone="+919876543210"),
            _tool("match_specialist_doctor", tools_start, 0.5, doctor_id="DOC-TRAUMA-03",
                  doctor_name="Dr. James Mitchell, FACS", specialty="Trauma"),
        ],
        received_at=_iso(received),
        dispatched_at=_iso(dispatched),
        created_at=_iso(dispatched + timedelta(seconds=0.3)),
    ))

    # Case 3: English acute stroke (ESI 2), protocol done in 3.9 s, waiting for the location
    received = now - timedelta(minutes=42)
    tools_start = received + timedelta(seconds=2.5)
    dispatched = received + timedelta(seconds=3.9)
    cases.append(EmergencyCase(
        case_id="CASE-DEMO-03",
        user_phone="+14155552671",
        patient_name="Eleanor Vance",
        emergency_contact_phone="+14155559812",
        transcript="My mother suddenly collapsed, her face is drooping on one side and she cannot speak clearly, please help!",
        detected_language="English",
        esi_level=2,
        severity="CRITICAL",
        status="ACTIVE",
        matched_specialty="Neurology",
        assigned_doctor_id="DOC-NEURO-02",
        assigned_doctor_name="Dr. Rajesh Sharma, MD",
        contact_call_id="CONN-88120-US",
        location_requested_at=_iso(dispatched + timedelta(seconds=1)),
        clinical_summary="Sudden collapse with facial droop and slurred speech: suspected acute ischaemic stroke.",
        reasoning="FAST-positive (face, speech) with sudden onset; thrombolysis window is time-critical (ESI 2). "
                  "Neurology handover.",
        confidence_score=0.93,
        triage_engine="heuristic",
        provider="simulator",
        provider_message_id="demo-wamid-03",
        tool_results=[
            _tool("alert_emergency_contact", tools_start, 1.4, call_id="CONN-88120-US", contact_phone="+14155559812"),
            _tool("match_specialist_doctor", tools_start, 0.4, doctor_id="DOC-NEURO-02",
                  doctor_name="Dr. Rajesh Sharma, MD", specialty="Neurology"),
        ],
        received_at=_iso(received),
        dispatched_at=_iso(dispatched),
        created_at=_iso(dispatched + timedelta(seconds=0.3)),
    ))

    # Case 4: Urdu mild cold (ESI 5), advisory only, no emergency protocol
    received = now - timedelta(hours=2)
    cases.append(EmergencyCase(
        case_id="CASE-DEMO-04",
        user_phone="+923214455667",
        patient_name="Maria Bibi",
        emergency_contact_phone="+923001122334",
        transcript="Mujhe kal se halka sa zukam aur sar dard hai, koi tablet bata dein please.",
        detected_language="Urdu",
        esi_level=5,
        severity="LOW",
        status="DISMISSED",
        clinical_summary="Mild common cold with headache since yesterday; no red-flag symptoms.",
        reasoning="Stable adult with minor upper-respiratory symptoms; no resources needed (ESI 5). "
                  "Self-care advice sent, no emergency protocol.",
        confidence_score=0.88,
        triage_engine="heuristic",
        provider="simulator",
        provider_message_id="demo-wamid-04",
        received_at=_iso(received),
        created_at=_iso(received + timedelta(seconds=3.1)),
    ))

    # Case 5: Hindi high fever (ESI 3), urgent care, reviewed by a doctor, no emergency protocol
    received = now - timedelta(hours=3, minutes=10)
    cases.append(EmergencyCase(
        case_id="CASE-DEMO-05",
        user_phone="+919922334455",
        patient_name="Priya Nair",
        emergency_contact_phone="+919900112233",
        transcript="Kripya jaldi aaiye, beti ko teen din se tez bukhar hai aur ulti ho rahi hai.",
        detected_language="Hindi",
        esi_level=3,
        severity="MODERATE",
        status="ACTIVE",
        matched_specialty="General Emergency",
        clinical_summary="Child with three days of high fever and vomiting; risk of dehydration.",
        reasoning="Stable but needs several resources (labs, IV fluids) (ESI 3). Urgent-care advice, no emergency protocol.",
        confidence_score=0.82,
        triage_engine="heuristic",
        provider="simulator",
        provider_message_id="demo-wamid-05",
        received_at=_iso(received),
        created_at=_iso(received + timedelta(seconds=3.4)),
    ))
    return cases


@router.post("/seed-demo")
def seed_demo_cases():
    """Seeds realistic multilingual demo scenarios. Idempotent: cases that already exist (e.g. from a
    previous start against a persistent Floci table) are left alone, so metric counters don't move."""
    now = datetime.now(timezone.utc)
    created = 0
    demo = _demo_cases(now)
    for case in demo:
        if dynamo_service.get_case(case.case_id):
            continue
        dynamo_service.save_case(case)
        created += 1
    return {"status": "SEEDED", "cases_count": len(demo), "created": created}
