import pytest
import asyncio
from app.agents.triage_agent import triage_agent
from app.services.dynamo_service import dynamo_service
from app.models.schemas import DoctorRegistration

@pytest.mark.asyncio
async def test_urdu_cardiac_emergency_triage():
    """Verify that an Urdu heart attack voice transcript activates the Level 1 emergency protocol"""
    transcript = "Mere uncle ko dil ka daura par raha hai, seene me shadeed dard hai, please jaldi ambulance bhejein!"
    assessment = await triage_agent.evaluate_and_dispatch(
        transcript=transcript,
        user_phone="+923009988776",
        patient_name="Uncle Tariq",
        emergency_contact_phone="+923331122334",
        detected_language="Urdu",
        case_id="CASE-TEST-URDU-CARDIAC"
    )

    assert assessment.is_emergency is True
    assert assessment.esi_level == 1
    assert assessment.matched_specialty == "Cardiology"
    assert assessment.emergency_contact_called is True
    # DATA-2: the real IDs the tools returned, and one result per tool (no ambulance tool: the
    # doctor calls the ambulance). OFFER-1: the match tool ranks doctors, it assigns nobody.
    assert assessment.contact_call_id
    match = next(r for r in assessment.tool_results if r["tool"] == "match_specialist_doctor")
    assert match["status"] == "OFFER_PENDING" and match["candidates"][0]["specialty"] == "Cardiology"
    assert {r["tool"] for r in assessment.tool_results} == {"alert_emergency_contact", "match_specialist_doctor"}
    assert assessment.triage_engine == "heuristic" and assessment.degraded is False

@pytest.mark.asyncio
async def test_hindi_trauma_emergency_triage():
    """Verify that a Hindi accident transcript activates the Level 2 Trauma protocol"""
    transcript = "Bhaiya highway par car accident ho gaya hai, sar se bahut khoon beh raha hai, jaldi ambulance bhejo!"
    assessment = await triage_agent.evaluate_and_dispatch(
        transcript=transcript,
        user_phone="+919811234567",
        patient_name="Amit Verma",
        emergency_contact_phone="+919876543210",
        detected_language="Hindi",
        case_id="CASE-TEST-HINDI-TRAUMA"
    )

    assert assessment.is_emergency is True
    assert assessment.esi_level == 2
    assert assessment.matched_specialty == "Trauma"
    assert assessment.emergency_contact_called is True

@pytest.mark.asyncio
async def test_english_stroke_emergency_triage():
    """Verify that an English acute stroke transcript matches Neurology"""
    transcript = "My mother suddenly collapsed, her face is drooping on one side and she cannot speak clearly, please help!"
    assessment = await triage_agent.evaluate_and_dispatch(
        transcript=transcript,
        user_phone="+14155552671",
        patient_name="Eleanor Vance",
        emergency_contact_phone="+14155559812",
        detected_language="English",
        case_id="CASE-TEST-EN-STROKE"
    )

    assert assessment.is_emergency is True
    assert assessment.esi_level == 2
    assert assessment.matched_specialty == "Neurology"
    assert assessment.emergency_contact_called is True

@pytest.mark.asyncio
async def test_non_emergency_filtering():
    """Verify that a mild headache / cold does NOT alarm emergency services"""
    transcript = "Mujhe kal se halka sa zukam aur sar dard hai, koi tablet bata dein please."
    assessment = await triage_agent.evaluate_and_dispatch(
        transcript=transcript,
        user_phone="+923214455667",
        patient_name="Maria Bibi",
        emergency_contact_phone="+923001122334",
        detected_language="Urdu",
        case_id="CASE-TEST-URDU-COLD"
    )

    # Must NOT trigger emergency tools
    assert assessment.is_emergency is False
    assert assessment.esi_level >= 4
    assert assessment.emergency_contact_called is False
    assert assessment.tool_results == []

@pytest.mark.asyncio
async def test_esi3_urgent_runs_no_emergency_tools():
    """AGENT-5: ESI 3 (urgent but stable) is kept for review but triggers no emergency tools"""
    transcript = "Mere bete ko kal raat se ulti aur pet me dard ho raha hai."
    assessment = await triage_agent.evaluate_and_dispatch(
        transcript=transcript,
        user_phone="+923215550001",
        patient_name="Bilal",
        emergency_contact_phone="+923005550002",
        detected_language="Urdu",
        case_id="CASE-TEST-URDU-ESI3"
    )

    assert assessment.esi_level == 3
    assert assessment.is_emergency is False
    assert assessment.emergency_contact_called is False
    assert assessment.tool_results == []

def test_doctor_registration_and_availability():
    """Verify doctor registration and active availability toggle"""
    new_doc_data = DoctorRegistration(
        name="Dr. Haris Mehmood, MD",
        phone_number="+923451122334",
        license_no="PMC-11928-E",
        specialty="Cardiology",
        bio="Cardiologist specializing in acute arrhythmia and intensive coronary care.",
        is_active=True
    )
    doc = dynamo_service.register_doctor(
        dynamo_service._mock_doctors.get("DOC-CARDIO-01")
    )
    assert doc is not None

    # Toggle availability
    updated = dynamo_service.update_doctor_status("DOC-CARDIO-01", False)
    assert updated.is_active is False

    # Toggle back to active
    updated_back = dynamo_service.update_doctor_status("DOC-CARDIO-01", True)
    assert updated_back.is_active is True
