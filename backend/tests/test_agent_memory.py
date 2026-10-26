"""MEM-1: long-term patient memory on AgentCore Memory.

- Request shapes of the AgentCore data-plane calls, ID sanitising, failure and timeout handling.
- The engine: history can only make a case MORE urgent, an emergency never waits for memory,
  and memory off (mock) or failing gives exactly the old flow.
- The pipeline and the doctor chat store events; the case keeps the recalled history.
"""
import asyncio
import time
import uuid
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from app import pipeline
from app.agents.core import TriageEngine
from app.agents.memory import (
    AgentCorePatientMemory,
    NoPatientMemory,
    actor_id_for,
    build_patient_memory,
    facts_namespace,
    session_id_for,
)
from app.agents.tools import local_tool_backend
from app.main import app
from app.services import memory_service
from app.services.dynamo_service import dynamo_service
from test_agent_support import ScriptedModel, all_tools_turn, classification_turn, model_factory
from test_pipeline_support import fakes, inbound, new_phone  # noqa: F401  (fixture import)


# ====================================================================== helpers
class FakeMemoryClient:
    """Stands in for boto3's bedrock-agentcore client."""

    def __init__(self, records: List[str] = None, fail: Exception = None, delay: float = 0.0):
        self.records = records or []
        self.fail = fail
        self.delay = delay
        self.retrieve_calls: List[Dict[str, Any]] = []
        self.events: List[Dict[str, Any]] = []

    def retrieve_memory_records(self, **kwargs):
        self.retrieve_calls.append(kwargs)
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise self.fail
        return {"memoryRecordSummaries": [{"content": {"text": t}, "memoryRecordId": f"r{i}"}
                                          for i, t in enumerate(self.records)]}

    def create_event(self, **kwargs):
        if self.fail:
            raise self.fail
        self.events.append(kwargs)
        return {"event": {"eventId": "e1"}}


class FakeMemory:
    """A PatientMemory with canned facts."""
    name = "fake"
    enabled = True

    def __init__(self, facts: List[str] = None, delay: float = 0.0, fail: bool = False):
        self.facts = facts or []
        self.delay = delay
        self.fail = fail
        self.recalls: List[tuple] = []

    async def recall(self, phone, query):
        self.recalls.append((phone, query))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("memory exploded")
        return list(self.facts)

    async def remember(self, phone, case_id, entries, dedupe_key=None):
        return True


def _engine(turns, memory, use_models=True):
    model = ScriptedModel(turns, model_id="claude")
    engine = TriageEngine("claude", None, "us-east-1", use_models=use_models,
                          model_factory=model_factory({"claude": model}), memory=memory)
    return engine, model


async def _run(engine, transcript="halka sa seene me khichao hai"):
    return await engine.run(case_id=f"CASE-M{uuid.uuid4().hex[:8].upper()}", transcript=transcript,
                            language="Urdu", user_phone="+923001234567", patient_name="P",
                            emergency_contact_phone="+923331122334", backend=local_tool_backend)


def _prompt_texts(model: ScriptedModel) -> List[str]:
    out = []
    for call in model.calls:
        for message in call["messages"]:
            for block in message.get("content", []):
                if "text" in block:
                    out.append(block["text"])
    return out


# ====================================================================== IDs and client
def test_ids_match_agentcore_patterns():
    assert actor_id_for("+92 300-1234567") == "patient-923001234567"
    assert actor_id_for("") == "patient-unknown"
    assert session_id_for("CASE-1A2B3C4D") == "CASE-1A2B3C4D"
    assert session_id_for("CASE#weird id") == "CASE-weird-id"
    assert facts_namespace("+923001234567") == "/patients/patient-923001234567/facts"


def test_memory_is_off_in_mock_mode_and_without_an_id():
    assert isinstance(build_patient_memory("mem-123", "us-east-1", mock=True), NoPatientMemory)
    assert isinstance(build_patient_memory(None, "us-east-1", mock=False), NoPatientMemory)
    assert isinstance(build_patient_memory("mem-123", "us-east-1", mock=False), AgentCorePatientMemory)
    # The test process runs in mock mode, so the app singleton is off.
    assert memory_service.patient_memory.enabled is False


@pytest.mark.asyncio
async def test_recall_queries_the_patient_facts_namespace():
    client = FakeMemoryClient(records=["Known cardiac patient", "On warfarin", "Known cardiac patient"])
    memory = AgentCorePatientMemory("mem-123", "us-east-1", client=client, top_k=3)
    facts = await memory.recall("+923001234567", "seene me dard")

    assert facts == ["Known cardiac patient", "On warfarin"]          # de-duplicated
    call = client.retrieve_calls[0]
    assert call["memoryId"] == "mem-123"
    assert call["namespace"] == "/patients/patient-923001234567/facts"
    assert call["searchCriteria"] == {"searchQuery": "seene me dard", "topK": 3}


@pytest.mark.asyncio
async def test_remember_sends_one_conversational_event_per_case():
    client = FakeMemoryClient()
    memory = AgentCorePatientMemory("mem-123", "us-east-1", client=client)
    ok = await memory.remember("+923001234567", "CASE-1", [("seene me dard", "USER"), ("ESI 1", "ASSISTANT"),
                                                          ("", "USER"), ("note", "DOCTOR")], dedupe_key="meta:wamid.1")
    assert ok is True
    event = client.events[0]
    assert event["actorId"] == "patient-923001234567" and event["sessionId"] == "CASE-1"
    assert event["payload"] == [
        {"conversational": {"content": {"text": "seene me dard"}, "role": "USER"}},
        {"conversational": {"content": {"text": "ESI 1"}, "role": "ASSISTANT"}},
        {"conversational": {"content": {"text": "note"}, "role": "OTHER"}},   # unknown role, empty text dropped
    ]
    # Deterministic token: an SQS retry of the same message does not add a second event.
    await memory.remember("+923001234567", "CASE-1", [("seene me dard", "USER")], dedupe_key="meta:wamid.1")
    assert client.events[1]["clientToken"] == event["clientToken"]


@pytest.mark.asyncio
async def test_memory_errors_and_timeouts_are_swallowed():
    failing = AgentCorePatientMemory("mem-123", "us-east-1", client=FakeMemoryClient(fail=RuntimeError("AccessDenied")))
    assert await failing.recall("+923001234567", "dard") == []
    assert await failing.remember("+923001234567", "CASE-1", [("x", "USER")]) is False

    slow = AgentCorePatientMemory("mem-123", "us-east-1", client=FakeMemoryClient(records=["x"], delay=0.5),
                                  timeout_seconds=0.05)
    started = time.monotonic()
    assert await slow.recall("+923001234567", "dard") == []
    assert time.monotonic() - started < 0.4


# ====================================================================== engine
@pytest.mark.asyncio
async def test_history_escalates_a_non_emergency():
    memory = FakeMemory(facts=["Known cardiac patient (MI in 2025)"])
    engine, model = _engine([classification_turn(3), classification_turn(2), all_tools_turn()], memory)
    assessment = await _run(engine)

    assert assessment.esi_level == 2 and assessment.emergency_contact_called is True
    assert "Escalated from ESI 3 to ESI 2" in assessment.reasoning
    assert assessment.patient_history == ["Known cardiac patient (MI in 2025)"]
    assert any("Known cardiac patient (MI in 2025)" in t for t in _prompt_texts(model))


@pytest.mark.asyncio
async def test_history_never_lowers_urgency():
    memory = FakeMemory(facts=["Patient often reports mild symptoms"])
    engine, _ = _engine([classification_turn(3), classification_turn(5, None)], memory)
    assessment = await _run(engine)

    assert assessment.esi_level == 3 and assessment.emergency_contact_called is False
    assert "Escalated" not in assessment.reasoning
    assert assessment.patient_history == ["Patient often reports mild symptoms"]


@pytest.mark.asyncio
async def test_emergency_never_waits_for_memory():
    memory = FakeMemory(facts=["anything"], delay=5)
    engine, model = _engine([classification_turn(1), all_tools_turn()], memory)
    started = time.monotonic()
    assessment = await _run(engine)

    assert time.monotonic() - started < 3
    assert assessment.esi_level == 1 and assessment.emergency_contact_called is True
    assert assessment.patient_history == []                       # not ready, not awaited
    assert not any("remembered from this patient" in t for t in _prompt_texts(model))


@pytest.mark.asyncio
async def test_no_review_without_history_or_when_memory_fails():
    for memory in (FakeMemory(facts=[]), FakeMemory(facts=["x"], fail=True)):
        engine, model = _engine([classification_turn(4, None)], memory)
        assessment = await _run(engine)
        assert assessment.esi_level == 4 and assessment.patient_history == []
        assert len(model.calls) == 1                                # the classification only


@pytest.mark.asyncio
async def test_keyword_classifier_is_never_reviewed():
    """Mock mode (no models): history is attached for the doctor but cannot change the decision."""
    memory = FakeMemory(facts=["Known cardiac patient"])
    engine, model = _engine([], memory, use_models=False)
    assessment = await _run(engine, transcript="halka sa zukam hai")
    assert assessment.esi_level == 5 and assessment.triage_engine == "heuristic"
    assert assessment.patient_history == ["Known cardiac patient"]
    assert model.calls == []


# ====================================================================== pipeline and doctor chat
@pytest.fixture
def remembered(monkeypatch):
    calls: List[tuple] = []

    def record(phone, case_id, entries, dedupe_key=None):
        calls.append((phone, case_id, list(entries), dedupe_key))

    monkeypatch.setattr(pipeline, "remember_in_background", record)
    from app import case_messages
    monkeypatch.setattr(case_messages, "remember_in_background", record)
    return calls


@pytest.mark.asyncio
async def test_pipeline_remembers_patient_words_and_triage_outcome(fakes, remembered):
    phone = new_phone()
    original = fakes.triage.evaluate_and_dispatch

    async def with_history(*args, **kwargs):
        assessment = await original(*args, **kwargs)
        assessment.patient_history = ["Known cardiac patient"]
        return assessment

    fakes.triage.evaluate_and_dispatch = with_history
    msg = inbound(phone, type="audio", transcript="seene me dard hai")
    res = await pipeline.process_inbound_message(msg)

    case = dynamo_service.get_case(res["case_id"])
    assert case.patient_history == ["Known cardiac patient"]
    patient = [c for c in remembered if c[2][0][1] == "USER"]
    triage = [c for c in remembered if c[2][0][1] == "ASSISTANT"]
    assert patient == [(phone, case.case_id, [("seene me dard hai", "USER")], f"meta:{msg.provider_message_id}")]
    assert triage and triage[0][2][0][0].startswith("ResQ-AI triage on ") and "ESI 1" in triage[0][2][0][0]

    # Handover message on the open (ESI 1-2) case is remembered too.
    await pipeline.process_inbound_message(inbound(phone, text="woh warfarin lete hain"))
    assert remembered[-1][2] == [("woh warfarin lete hain", "USER")] and remembered[-1][1] == case.case_id


@pytest.mark.asyncio
async def test_untranscribable_voice_note_is_not_remembered(fakes, remembered):
    from app.models.schemas import TranscriptionResult
    fakes.audio.transcription = TranscriptionResult(ok=False, error="unavailable")
    await pipeline.process_inbound_message(inbound(new_phone(), type="audio", media_id="m-1"))
    assert remembered == []


def test_doctor_message_is_remembered(fakes, remembered):
    phone = new_phone()
    res = asyncio.run(pipeline.process_inbound_message(inbound(phone, type="audio", transcript="dil ka daura")))
    remembered.clear()
    r = TestClient(app).post(f"/api/v1/cases/{res['case_id']}/messages",
                             json={"content": "Patient has a stent since 2024.", "sender_name": "Dr. Khan"})
    assert r.status_code == 200
    assert remembered == [(phone, res["case_id"], [("Doctor Dr. Khan: Patient has a stent since 2024.", "ASSISTANT")],
                           r.json()["message_id"])]


@pytest.mark.asyncio
async def test_remember_in_background_is_a_no_op_when_memory_is_off():
    assert memory_service.remember_in_background("+923001234567", "CASE-1", [("x", "USER")]) is None
