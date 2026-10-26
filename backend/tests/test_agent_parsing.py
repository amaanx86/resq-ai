"""AGENT-7: structured output is validated and the ESI clamped; free-text JSON parsing is robust."""
import uuid

import pytest

from app.agents.core import TriageDecision, TriageEngine, normalize_specialty, parse_triage_json
from app.agents.tools import local_tool_backend
from test_agent_support import ScriptedModel, decision_args, model_factory, tool_use

GOOD = ('{"esi_level": 2, "confidence_score": 0.9, "detected_language": "Hindi", "matched_specialty": "Trauma", '
        '"clinical_summary": "Road accident with bleeding.", "reasoning": "Trauma markers."}')


@pytest.mark.parametrize("text", [
    GOOD,
    f"```json\n{GOOD}\n```",
    f"```\n{GOOD}\n```",
    f"Here is my assessment:\n{GOOD}\nStay safe.",
    f'{{"assessment": {GOOD}}}',
    f"Notes {{not json}} then {GOOD}",
    "json" + GOOD + "``",  # what the old str.strip("```json") parsing mangled
])
def test_parse_triage_json_extracts_the_object(text):
    d = parse_triage_json(text)
    assert d.esi_level == 2 and d.matched_specialty == "Trauma" and d.is_emergency is True


@pytest.mark.parametrize("raw,expected", [(0, 1), (-3, 1), (1, 1), (5, 5), (7, 5), ("2", 2), ("ESI 3", 3), (2.6, 3)])
def test_esi_is_clamped_to_1_5(raw, expected):
    d = TriageDecision.model_validate({**decision_args(1), "esi_level": raw})
    assert d.esi_level == expected


@pytest.mark.parametrize("raw,expected", [(95, 0.95), (150, 1.0), (-1, 0.0), ("high", 0.5), (0.4, 0.4)])
def test_confidence_is_clamped(raw, expected):
    d = TriageDecision.model_validate({**decision_args(1), "confidence_score": raw})
    assert d.confidence_score == pytest.approx(expected)


@pytest.mark.parametrize("raw,expected", [
    ("cardiology", "Cardiology"), ("Emergency Medicine", "General Emergency"), ("Respiratory", "Pulmonology"),
    ("Neurological emergency", "Neurology"), ("Dermatology", "General Emergency"), (None, None), ("none", None),
])
def test_specialty_normalisation(raw, expected):
    assert normalize_specialty(raw) == expected


@pytest.mark.parametrize("text", ["", "no json here", '{"foo": 1}', '{"esi_level": "abc", "clinical_summary": "x", "reasoning": "y"}'])
def test_parse_triage_json_rejects_garbage(text):
    with pytest.raises(ValueError):
        parse_triage_json(text)


def test_is_emergency_follows_esi_not_the_model():
    assert TriageDecision.model_validate({**decision_args(3), "is_emergency": True}).is_emergency is False
    assert TriageDecision.model_validate(decision_args(2)).is_emergency is True


@pytest.mark.asyncio
async def test_strands_structured_output_out_of_range_esi_is_clamped():
    model = ScriptedModel([[tool_use("TriageDecision", {**decision_args(9, None), "esi_level": 9})]])
    engine = TriageEngine("m", None, "us-east-1", use_models=True, model_factory=model_factory({"m": model}))
    a = await engine.run(case_id=f"CASE-P{uuid.uuid4().hex[:6]}", transcript="hello", language="English",
                         user_phone="+1", patient_name="P", emergency_contact_phone=None, backend=local_tool_backend)
    assert a.esi_level == 5 and a.is_emergency is False and a.tool_results == []


@pytest.mark.asyncio
async def test_strands_invalid_structured_output_is_retried_by_the_agent():
    """A schema-invalid tool call is sent back to the model, which corrects it."""
    model = ScriptedModel([
        [tool_use("TriageDecision", {"esi_level": "unknown", "clinical_summary": "x", "reasoning": "y"}, "t1")],
        [tool_use("TriageDecision", decision_args(4, None), "t2")],
    ])
    engine = TriageEngine("m", None, "us-east-1", use_models=True, model_factory=model_factory({"m": model}))
    a = await engine.run(case_id=f"CASE-P{uuid.uuid4().hex[:6]}", transcript="mild rash", language="English",
                         user_phone="+1", patient_name="P", emergency_contact_phone=None, backend=local_tool_backend)
    assert a.esi_level == 4 and a.triage_engine == "bedrock:m" and len(model.calls) == 2
