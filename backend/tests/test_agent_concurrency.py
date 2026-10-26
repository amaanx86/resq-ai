"""AGENT-4: the emergency tools run at the same time (stubbed delays), on both the code-driven path
(mock mode / safety net) and the Strands agent path (ConcurrentToolExecutor)."""
import asyncio
import time
import uuid

import pytest

from app.agents.core import TriageEngine
from app.agents.tools import local_tool_backend
from app.services.dynamo_service import dynamo_service
from app.services.telephony_service import telephony_service
from test_agent_support import ScriptedModel, all_tools_turn, classification_turn, model_factory

DELAY = 0.3


@pytest.fixture
def slow_tools(monkeypatch):
    """Each tool's slow part takes DELAY seconds; records (start, end) per tool."""
    spans = {}

    async def slow_call(**kwargs):
        start = time.perf_counter()
        await asyncio.sleep(DELAY)
        spans["alert_emergency_contact"] = (start, time.perf_counter())
        return {"status": "CALL_INITIATED", "contact_id": "CONN-SLOW0001"}

    real_loads = dynamo_service.open_case_loads

    def slow_loads():  # synchronous, like boto3: must run in a thread to overlap
        start = time.perf_counter()
        time.sleep(DELAY)
        spans.setdefault("match_specialist_doctor", (start, time.perf_counter()))
        return real_loads()

    monkeypatch.setattr(telephony_service, "call_emergency_contact", slow_call)
    monkeypatch.setattr(dynamo_service, "open_case_loads", slow_loads)
    return spans


def _assert_overlap(spans, elapsed):
    assert set(spans) == {"alert_emergency_contact", "match_specialist_doctor"}
    latest_start = max(s for s, _ in spans.values())
    earliest_end = min(e for _, e in spans.values())
    assert latest_start < earliest_end, f"tools did not overlap: {spans}"
    assert elapsed < DELAY * 1.8, f"took {elapsed:.2f}s; sequential would be >= {DELAY * 2:.1f}s"


async def _run(engine):
    return await engine.run(case_id=f"CASE-C{uuid.uuid4().hex[:8].upper()}", transcript="Pulmonology breathless",
                            language="English", user_phone="+14155550100", patient_name="P",
                            emergency_contact_phone="+14155550101", backend=local_tool_backend)


@pytest.mark.asyncio
async def test_code_driven_tools_overlap(slow_tools):
    # "breathless" -> ESI 1 Pulmonology (one slow open-case read for the ranking)
    engine = TriageEngine("unused", None, "us-east-1", use_models=False)
    start = time.perf_counter()
    assessment = await _run(engine)
    elapsed = time.perf_counter() - start
    assert assessment.contact_call_id == "CONN-SLOW0001"
    _assert_overlap(slow_tools, elapsed)


@pytest.mark.asyncio
async def test_strands_agent_runs_tool_calls_concurrently(slow_tools):
    model = ScriptedModel([classification_turn(1, "Pulmonology"), all_tools_turn("Pulmonology")])
    engine = TriageEngine("m", None, "us-east-1", use_models=True, model_factory=model_factory({"m": model}))
    start = time.perf_counter()
    assessment = await _run(engine)
    elapsed = time.perf_counter() - start
    assert {r["driver"] for r in assessment.tool_results} == {"agent"}
    _assert_overlap(slow_tools, elapsed)
