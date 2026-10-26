"""Long-term patient memory on Amazon Bedrock AgentCore Memory (MEM-1).

What it stores: the patient's own words (triaged voice notes and texts, handover messages), the
triage outcome of each case, and the doctor's chat messages. Each is an AgentCore Memory event with
actor = the patient (`actor_id_for(phone)`) and session = the case. The memory's semantic strategy
extracts durable facts, in English, asynchronously ("known cardiac patient", "on warfarin", "lives on the 3rd
floor") into the namespace `/patients/{actorId}/facts` (terraform/agentcore_memory.tf).

What reads it: `TriageEngine.run` (core.py) recalls the top facts for the new message while the
first classification runs. The facts never lower urgency: they are only used to re-check a non-
emergency result, which is replaced only when the re-check is MORE urgent. They are also returned
on the assessment (`patient_history`) for the doctor.

Best effort everywhere: a missing memory ID, mock/Floci mode, an AWS error or a timeout means no
history, never a failed or delayed triage.

Imports nothing from `app.services` (the AgentCore Runtime container imports this module).
"""
import asyncio
import re
import uuid
from datetime import datetime, timezone
from typing import Any, List, Optional, Protocol, Sequence, Tuple

# Must match namespace_templates of the SEMANTIC strategy in terraform/agentcore_memory.tf.
PATIENT_FACTS_NAMESPACE = "/patients/{actorId}/facts"
MAX_EVENT_TEXT = 9000      # API limit is 100k; voice-note transcripts are far shorter
MAX_QUERY_TEXT = 2000      # retrieval query (API limit 10k)
ROLES = ("USER", "ASSISTANT", "TOOL", "OTHER")

Entry = Tuple[str, str]    # (text, role)


def actor_id_for(phone: str) -> str:
    """AgentCore actor IDs allow [a-zA-Z0-9-_/:] only, so "+923001234567" -> "patient-923001234567"."""
    digits = re.sub(r"\D", "", phone or "")
    return f"patient-{digits or 'unknown'}"


def session_id_for(case_id: str) -> str:
    """One memory session per case (pattern [a-zA-Z0-9][a-zA-Z0-9-_]*, max 100)."""
    sid = re.sub(r"[^a-zA-Z0-9_-]", "-", case_id or "").strip("-_") or "no-case"
    return sid[:100]


def facts_namespace(phone: str) -> str:
    return PATIENT_FACTS_NAMESPACE.replace("{actorId}", actor_id_for(phone))


class PatientMemory(Protocol):
    name: str
    enabled: bool

    async def recall(self, phone: str, query: str) -> List[str]: ...

    async def remember(self, phone: str, case_id: str, entries: Sequence[Entry],
                       dedupe_key: Optional[str] = None) -> bool: ...


class NoPatientMemory:
    """Memory off (mock, Floci, tests, or AGENTCORE_MEMORY_ID unset)."""
    name = "none"
    enabled = False

    async def recall(self, phone: str, query: str) -> List[str]:
        return []

    async def remember(self, phone: str, case_id: str, entries: Sequence[Entry],
                       dedupe_key: Optional[str] = None) -> bool:
        return False


class AgentCorePatientMemory:
    """AgentCore Memory data plane (`bedrock-agentcore` CreateEvent / RetrieveMemoryRecords)."""
    name = "agentcore"
    enabled = True

    def __init__(self, memory_id: str, region: str, client: Any = None, top_k: int = 5,
                 timeout_seconds: float = 2.0):
        if not memory_id:
            raise ValueError("AGENTCORE_MEMORY_ID is not set")
        self.memory_id = memory_id
        self.top_k = max(1, min(100, int(top_k)))
        self.timeout = timeout_seconds
        if client is None:
            import boto3
            from botocore.config import Config
            client = boto3.client(
                "bedrock-agentcore", region_name=region,
                config=Config(connect_timeout=2, read_timeout=max(2, int(timeout_seconds) + 1),
                              retries={"max_attempts": 1}),
            )
        self.client = client

    # ---------------- read ----------------
    def _recall_sync(self, phone: str, query: str) -> List[str]:
        res = self.client.retrieve_memory_records(
            memoryId=self.memory_id,
            namespace=facts_namespace(phone),
            searchCriteria={"searchQuery": query[:MAX_QUERY_TEXT], "topK": self.top_k},
            maxResults=self.top_k,
        )
        facts: List[str] = []
        for record in res.get("memoryRecordSummaries") or []:
            text = ((record.get("content") or {}).get("text") or "").strip()
            if text and text not in facts:
                facts.append(text)
        return facts[: self.top_k]

    async def recall(self, phone: str, query: str) -> List[str]:
        if not phone or not (query or "").strip():
            return []
        try:
            return await asyncio.wait_for(asyncio.to_thread(self._recall_sync, phone, query), self.timeout)
        except Exception as e:
            print(f"[PatientMemory] recall failed for {actor_id_for(phone)} ({type(e).__name__}: {e}); "
                  "triaging without history.")
            return []

    # ---------------- write ----------------
    def _remember_sync(self, phone: str, case_id: str, entries: Sequence[Entry],
                       dedupe_key: Optional[str]) -> None:
        payload = [{"conversational": {"content": {"text": text[:MAX_EVENT_TEXT]},
                                       "role": role if role in ROLES else "OTHER"}}
                   for text, role in entries if (text or "").strip()]
        if not payload:
            return
        kwargs = dict(
            memoryId=self.memory_id,
            actorId=actor_id_for(phone),
            sessionId=session_id_for(case_id),
            eventTimestamp=datetime.now(timezone.utc),
            payload=payload,
        )
        if dedupe_key:
            # Same token on an SQS retry: the service returns the first event instead of adding one.
            kwargs["clientToken"] = uuid.uuid5(uuid.NAMESPACE_URL, f"resq-memory:{dedupe_key}").hex
        self.client.create_event(**kwargs)

    async def remember(self, phone: str, case_id: str, entries: Sequence[Entry],
                       dedupe_key: Optional[str] = None) -> bool:
        try:
            await asyncio.wait_for(asyncio.to_thread(self._remember_sync, phone, case_id, entries, dedupe_key),
                                   self.timeout)
            return True
        except Exception as e:
            print(f"[PatientMemory] event for case {case_id} not stored ({type(e).__name__}: {e}).")
            return False


def build_patient_memory(memory_id: Optional[str], region: str, *, mock: bool, top_k: int = 5,
                         timeout_seconds: float = 2.0) -> PatientMemory:
    """AgentCore Memory when a memory ID is configured on real AWS; otherwise memory is off."""
    if mock:
        return NoPatientMemory()
    if not memory_id:
        print("[PatientMemory] AGENTCORE_MEMORY_ID not set: patient memory off.")
        return NoPatientMemory()
    try:
        memory = AgentCorePatientMemory(memory_id, region, top_k=top_k, timeout_seconds=timeout_seconds)
        print(f"[PatientMemory] AgentCore Memory {memory_id} (top {memory.top_k}, {timeout_seconds}s timeout).")
        return memory
    except Exception as e:
        print(f"[PatientMemory] WARNING: could not create the AgentCore Memory client ({e}); memory off.")
        return NoPatientMemory()
