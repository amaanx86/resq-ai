"""AGENT-3: primary model failure -> fallback model -> keyword classifier (degraded). Mock mode never
touches Bedrock."""
import uuid

import pytest

from app.agents.core import TriageEngine, default_model_factory
from app.agents.tools import local_tool_backend
from app.agents.triage_agent import triage_agent
from test_agent_support import (
    FailingModel,
    ScriptedModel,
    all_tools_turn,
    classification_turn,
    model_factory,
)


def _case() -> str:
    return f"CASE-F{uuid.uuid4().hex[:8].upper()}"


async def _run(engine, transcript="Mere uncle ko dil ka daura par raha hai"):
    return await engine.run(case_id=_case(), transcript=transcript, language="Urdu", user_phone="+923000000001",
                            patient_name="P", emergency_contact_phone="+923331122334", backend=local_tool_backend)


@pytest.mark.asyncio
async def test_primary_failure_uses_fallback_model():
    primary = FailingModel()
    fallback = ScriptedModel([classification_turn(1), all_tools_turn()], model_id="nova")
    engine = TriageEngine("claude", "nova", "us-east-1", use_models=True,
                          model_factory=model_factory({"claude": primary, "nova": fallback}))
    assessment = await _run(engine)

    assert primary.calls, "primary model was not tried"
    assert fallback.calls, "fallback model was not called after the primary failed"
    assert assessment.triage_engine == "bedrock:nova"
    assert assessment.degraded is False
    assert assessment.emergency_contact_called is True
    # The tool phase also prefers the model that worked, so the tools were agent-driven.
    assert {r["driver"] for r in assessment.tool_results} == {"agent"}


@pytest.mark.asyncio
async def test_both_models_failing_degrades_to_keyword_classifier():
    primary, fallback = FailingModel(), FailingModel(model_id="nova")
    engine = TriageEngine("claude", "nova", "us-east-1", use_models=True,
                          model_factory=model_factory({"claude": primary, "nova": fallback}))
    assessment = await _run(engine)

    assert primary.calls and fallback.calls
    assert assessment.triage_engine == "heuristic"
    assert assessment.degraded is True
    assert assessment.esi_level == 1 and assessment.matched_specialty == "Cardiology"
    # The keyword classifier drives the same tools (code-driven).
    assert assessment.emergency_contact_called
    assert {r["tool"]: r["status"] for r in assessment.tool_results}["match_specialist_doctor"] == "OFFER_PENDING"
    assert {r["driver"] for r in assessment.tool_results} == {"code"}
    assert "Model errors" in assessment.reasoning


@pytest.mark.asyncio
async def test_tool_phase_failure_still_runs_tools_via_safety_net():
    """Classification worked but the tool-calling turn failed on both models: code runs the tools."""
    primary = ScriptedModel([classification_turn(2, "Neurology")], model_id="claude")
    original_stream = primary.stream

    async def stream(messages, tool_specs=None, system_prompt=None, **kwargs):
        if tool_specs and any(t["name"] == "alert_emergency_contact" for t in tool_specs):
            raise RuntimeError("throttled")
        async for event in original_stream(messages, tool_specs, system_prompt, **kwargs):
            yield event

    primary.stream = stream
    engine = TriageEngine("claude", "nova", "us-east-1", use_models=True,
                          model_factory=model_factory({"claude": primary, "nova": FailingModel(model_id="nova")}))
    assessment = await _run(engine, transcript="face drooping")
    assert assessment.triage_engine == "bedrock:claude"
    assert assessment.emergency_contact_called is True
    assert {r["driver"] for r in assessment.tool_results} == {"code"}


@pytest.mark.asyncio
async def test_model_skipping_a_tool_is_completed_by_code():
    model = ScriptedModel([classification_turn(1), all_tools_turn()[:1]], model_id="claude")  # only the call
    engine = TriageEngine("claude", None, "us-east-1", use_models=True, model_factory=model_factory({"claude": model}))
    assessment = await _run(engine)
    drivers = {r["tool"]: r["driver"] for r in assessment.tool_results}
    assert drivers == {"alert_emergency_contact": "agent", "match_specialist_doctor": "code"}


@pytest.mark.asyncio
async def test_mock_mode_never_builds_a_bedrock_model(monkeypatch):
    def explode(model_id):
        raise AssertionError(f"Bedrock model {model_id} built in mock mode")

    assert triage_agent.mock_mode is True
    assert triage_agent.engine.use_models is False
    monkeypatch.setattr(triage_agent.engine, "model_factory", explode)
    assessment = await triage_agent.evaluate_and_dispatch(
        transcript="My father has crushing chest pain", user_phone="+14155550000", patient_name="P",
        emergency_contact_phone="+14155550001", detected_language="English", case_id=_case())
    assert assessment.triage_engine == "heuristic"
    assert assessment.degraded is False


def test_default_model_factory_builds_strands_bedrock_models_for_both_ids():
    """Claude and Nova share one Converse code path through Strands BedrockModel (no network)."""
    from strands.models import BedrockModel

    factory = default_model_factory("us-east-1")
    primary, fallback = factory("anthropic.claude-test"), factory("amazon.nova-pro-v1:0")
    assert isinstance(primary, BedrockModel) and isinstance(fallback, BedrockModel)
    assert primary.get_config()["model_id"] == "anthropic.claude-test"
    assert fallback.get_config()["model_id"] == "amazon.nova-pro-v1:0"
    assert factory("anthropic.claude-test") is primary  # cached


def test_default_model_factory_sends_no_temperature_to_claude():
    """Sonnet 5.5 on Bedrock: `temperature` -> ValidationException "deprecated for this model"."""
    factory = default_model_factory("us-east-1")
    assert "temperature" not in factory("us.anthropic.claude-sonnet-5-5").get_config()
    assert factory("us.amazon.nova-pro-v1:0").get_config()["temperature"] == 0.0
