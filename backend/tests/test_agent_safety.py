"""AGENT-1 / AGENT-5 safety: the emergency-contact call refuses ESI 3-5 and runs at most once per
case, ESI 3 calls no tools, a missing emergency contact skips the call, and no ambulance tool exists
(the doctor calls the ambulance)."""
import uuid

import pytest

from app.agents import tools
from app.agents.core import REQUIRED_EMERGENCY_TOOLS, TriageEngine
from app.agents.tools import (
    TOOL_FUNCTIONS,
    alert_emergency_contact_tool,
    local_tool_backend,
)
from app.agents.triage_agent import triage_agent
from app.services.telephony_service import telephony_service
from test_agent_support import (
    ScriptedModel,
    all_tools_turn,
    classification_turn,
    model_factory,
    tool_use,
)


def _case() -> str:
    return f"CASE-T{uuid.uuid4().hex[:8].upper()}"


@pytest.fixture
def telephony_spy(monkeypatch):
    calls = {"call": 0}
    real_call = telephony_service.call_emergency_contact

    async def call(**kwargs):
        calls["call"] += 1
        return await real_call(**kwargs)

    monkeypatch.setattr(telephony_service, "call_emergency_contact", call)
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize("esi", [3, 4, 5, 0, 7, None, "three"])
async def test_contact_call_refuses_non_emergency_esi(esi, telephony_spy):
    call = await alert_emergency_contact_tool(case_id=_case(), esi_level=esi, contact_phone="+921111",
                                              patient_name="P", emergency_details="x")
    assert call["status"] == "REFUSED_ESI"
    assert telephony_spy == {"call": 0}


@pytest.mark.asyncio
@pytest.mark.parametrize("esi", [1, 2, "2"])
async def test_contact_call_runs_for_esi_1_and_2(esi, telephony_spy):
    call = await alert_emergency_contact_tool(case_id=_case(), esi_level=esi, contact_phone="+921111",
                                              patient_name="P", emergency_details="x")
    assert call["status"] == "CALL_INITIATED"
    assert call["contact_id"].startswith("CONN-")
    assert telephony_spy["call"] == 1


def test_there_is_no_ambulance_tool():
    """Nothing dispatches an ambulance automatically: the doctor calls it (pipeline + portal)."""
    assert "dispatch_ambulance" not in TOOL_FUNCTIONS
    assert set(REQUIRED_EMERGENCY_TOOLS) == {"alert_emergency_contact", "match_specialist_doctor"}


@pytest.mark.asyncio
async def test_repeat_call_for_same_case_does_not_call_twice(telephony_spy):
    case_id = _case()
    args = dict(case_id=case_id, esi_level=1, contact_phone="+921111", patient_name="P", emergency_details="x")
    first = await alert_emergency_contact_tool(**args)
    second = await alert_emergency_contact_tool(**args)

    assert telephony_spy == {"call": 1}
    assert second["idempotent_replay"] is True
    assert second["contact_id"] == first["contact_id"]


@pytest.mark.asyncio
async def test_failed_call_releases_the_claim_so_a_retry_can_call(monkeypatch):
    attempts = []

    async def flaky(**kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            return {"status": "FAILED", "error": "Connect down"}
        return {"status": "CALL_INITIATED", "contact_id": "CONN-RETRY01"}

    monkeypatch.setattr(telephony_service, "call_emergency_contact", flaky)
    args = dict(case_id=_case(), esi_level=2, contact_phone="+921111", patient_name="P", emergency_details="x")
    assert (await alert_emergency_contact_tool(**args))["status"] == "FAILED"
    assert (await alert_emergency_contact_tool(**args))["contact_id"] == "CONN-RETRY01"
    assert (await alert_emergency_contact_tool(**args))["idempotent_replay"] is True
    assert len(attempts) == 2


@pytest.mark.asyncio
async def test_model_looping_on_the_contact_call_calls_once(telephony_spy):
    """A model that keeps calling alert_emergency_contact across turns still calls once."""
    case_id = _case()
    model = ScriptedModel([
        classification_turn(1),
        all_tools_turn(),
        [tool_use("alert_emergency_contact", {"emergency_details": "Again."}, "tu-call-2")],
        [tool_use("alert_emergency_contact", {"emergency_details": "Again."}, "tu-call-3")],
    ])
    engine = TriageEngine("primary", None, "us-east-1", use_models=True, model_factory=model_factory({"primary": model}))
    assessment = await engine.run(case_id=case_id, transcript="seene me dard", language="Urdu", user_phone="+920000",
                                  patient_name="P", emergency_contact_phone="+921111", backend=local_tool_backend)
    assert telephony_spy["call"] == 1
    assert assessment.emergency_contact_called is True
    # A second, fresh run for the same case (e.g. AgentCore failed and the worker fell back) replays.
    again = await TriageEngine("x", None, "us-east-1", use_models=False).run(
        case_id=case_id, transcript="dil ka daura", language="Urdu", user_phone="+920000",
        patient_name="P", emergency_contact_phone="+921111", backend=local_tool_backend)
    assert telephony_spy == {"call": 1}
    assert again.contact_call_id == assessment.contact_call_id


@pytest.mark.asyncio
async def test_esi3_model_classification_runs_no_tools(telephony_spy):
    """ESI 3 from the model: the dispatch phase never starts, even if the model wanted tools."""
    model = ScriptedModel([classification_turn(3, "General Emergency"), all_tools_turn()])
    engine = TriageEngine("primary", None, "us-east-1", use_models=True, model_factory=model_factory({"primary": model}))
    assessment = await engine.run(case_id=_case(), transcript="pet me dard", language="Urdu", user_phone="+920000",
                                  patient_name="P", emergency_contact_phone="+921111", backend=local_tool_backend)
    assert assessment.esi_level == 3
    assert assessment.is_emergency is False
    assert assessment.tool_results == []
    assert telephony_spy == {"call": 0}
    assert len(model.calls) == 1  # classification only


@pytest.mark.asyncio
async def test_esi3_heuristic_calls_no_tools(monkeypatch):
    called = []

    async def spy(tool_name, arguments):
        called.append(tool_name)
        return {"status": "FAILED"}

    monkeypatch.setattr(local_tool_backend, "call", spy)
    assessment = await triage_agent.evaluate_and_dispatch(
        transcript="Kal se tez bukhar aur ulti ho rahi hai", user_phone="+920001", patient_name="P",
        emergency_contact_phone="+921111", detected_language="Urdu", case_id=_case())
    assert assessment.esi_level == 3
    assert called == []


@pytest.mark.asyncio
async def test_missing_emergency_contact_skips_the_call(telephony_spy):
    assessment = await triage_agent.evaluate_and_dispatch(
        transcript="Mere abbu ko dil ka daura para hai", user_phone="+920002", patient_name="P",
        emergency_contact_phone=None, detected_language="Urdu", case_id=_case())
    assert assessment.is_emergency is True
    assert {r["tool"]: r["status"] for r in assessment.tool_results}["match_specialist_doctor"] == "OFFER_PENDING"
    assert assessment.emergency_contact_called is False
    assert assessment.contact_call_id is None
    contact = next(r for r in assessment.tool_results if r["tool"] == "alert_emergency_contact")
    assert contact["status"] == "SKIPPED_NO_CONTACT"
    assert telephony_spy == {"call": 0}


@pytest.mark.asyncio
async def test_case_id_is_generated_when_missing():
    assessment = await triage_agent.evaluate_and_dispatch(
        transcript="car accident, khoon beh raha hai", user_phone="+920003", patient_name="P",
        emergency_contact_phone="+921111", detected_language="Hindi")
    case_ids = {r.get("case_id") for r in assessment.tool_results}
    assert len(case_ids) == 1 and next(iter(case_ids)).startswith("CASE-")


@pytest.mark.asyncio
async def test_model_supplied_arguments_cannot_change_case_or_esi(telephony_spy, monkeypatch):
    """The model only chooses wording; case ID, ESI, patient and contact come from code."""
    seen = []
    real = tools.alert_emergency_contact_tool

    async def capture(**kwargs):
        seen.append(kwargs)
        return await real(**kwargs)

    monkeypatch.setitem(tools.TOOL_FUNCTIONS, "alert_emergency_contact", capture)
    case_id = _case()
    model = ScriptedModel([
        classification_turn(2, "Trauma"),
        [tool_use("alert_emergency_contact", {"emergency_details": "Accident.", "case_id": "CASE-OTHER",
                                              "esi_level": 1, "contact_phone": "+929999"})],
    ])
    engine = TriageEngine("primary", None, "us-east-1", use_models=True, model_factory=model_factory({"primary": model}))
    await engine.run(case_id=case_id, transcript="accident", language="English", user_phone="+920000",
                     patient_name="P", emergency_contact_phone="+921111", backend=local_tool_backend)
    assert seen and seen[0]["case_id"] == case_id and seen[0]["esi_level"] == 2
    assert seen[0]["contact_phone"] == "+921111"


@pytest.mark.parametrize("language", ["Urdu", "Hindi"])
def test_urdu_and_hindi_calls_are_spoken_in_devanagari(language):
    """Polly has no Urdu voice: the Connect flow reads Urdu/Hindi with Kajal (hi-IN), which needs Devanagari."""
    from app.services.telephony_service import TelephonyService
    text = TelephonyService.spoken_message("Ali", "chest pain", language)
    assert "Ali" in text and "chest pain" in text
    assert not any("؀" <= ch <= "ۿ" for ch in text)  # no Arabic-script letters
    assert any("ऀ" <= ch <= "ॿ" for ch in text)


@pytest.mark.parametrize("contact, patient_language, expected", [
    ("+923331122334", "Hindi", "Urdu"),     # Urdu voice note labelled Hindi, family in Pakistan
    ("+923331122334", "Unknown", "Urdu"),
    ("+919812345678", "Urdu", "Hindi"),
    ("+923331122334", "English", "English"),
    ("+971501234567", "Hindi", "Hindi"),    # elsewhere: the patient's language
    (None, "Urdu", "Urdu"),
])
def test_call_language_follows_the_contacts_country(contact, patient_language, expected):
    from app.agents.core import call_language
    assert call_language(contact, patient_language) == expected


@pytest.mark.asyncio
async def test_pakistani_contact_gets_an_urdu_call_when_the_patient_was_labelled_hindi(monkeypatch):
    calls = []

    async def call(**kwargs):
        calls.append(kwargs)
        return {"status": "CALL_INITIATED", "contact_id": "C-1", "simulated": True}

    monkeypatch.setattr(telephony_service, "call_emergency_contact", call)
    assessment = await triage_agent.evaluate_and_dispatch(
        case_id=_case(), transcript="car accident, khoon beh raha hai", user_phone="+920004",
        patient_name="P", emergency_contact_phone="+923331122334", detected_language="Hindi")
    assert calls and calls[0]["language"] == "Urdu"
    contact = next(r for r in assessment.tool_results if r["tool"] == "alert_emergency_contact")
    assert contact["language"] == "Urdu"
