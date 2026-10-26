"""Patient memory singleton (MEM-1) for the API and the worker. See app/agents/memory.py.

`remember_in_background` stores an event without delaying the caller: the pipeline and the
doctor-chat route call it after the patient or doctor has their reply. The worker and the API each
run one long-lived event loop, so the tasks finish after the request; a task still pending at
shutdown is dropped (memory is best effort).
"""
import asyncio
from typing import Optional, Sequence, Set

from app.config import settings
from app.agents.memory import Entry, PatientMemory, build_patient_memory

patient_memory: PatientMemory = build_patient_memory(
    settings.AGENTCORE_MEMORY_ID,
    settings.AWS_REGION,
    mock=settings.MOCK_AWS or bool(settings.AWS_ENDPOINT_URL),  # Floci does not emulate AgentCore
    top_k=settings.AGENTCORE_MEMORY_TOP_K,
    timeout_seconds=settings.AGENTCORE_MEMORY_TIMEOUT_SECONDS,
)

_pending: Set[asyncio.Task] = set()


def remember_in_background(phone: str, case_id: str, entries: Sequence[Entry],
                           dedupe_key: Optional[str] = None) -> Optional[asyncio.Task]:
    if not patient_memory.enabled or not phone or not case_id:
        return None
    entries = [(text, role) for text, role in entries if (text or "").strip()]
    if not entries:
        return None
    task = asyncio.get_running_loop().create_task(
        patient_memory.remember(phone, case_id, entries, dedupe_key=dedupe_key))
    _pending.add(task)  # keep a reference until done, or the task can be garbage-collected
    task.add_done_callback(_pending.discard)
    return task
