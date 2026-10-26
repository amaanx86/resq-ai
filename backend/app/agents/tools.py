"""Emergency tools: the business logic behind the agent's tools.

These are plain async callables. Three callers share them:
- the in-process Strands agent (through `LocalToolBackend` and the `@tool` wrappers in `core.py`),
- the AgentCore Gateway Lambda target (`gateway_tools/handler.py`),
- the pipeline and portal directly (`match_specialist_doctor_tool`, `generate_video_session_tool`).

Doctor matching (OFFER-1): `match_specialist_doctor_tool` only ranks the on-duty doctors; it
assigns nobody. The worker offers the case to them (`app.doctor_offers`) and the first doctor to
accept gets it. The tool stays database-only, so the Gateway Lambda needs no WhatsApp access.

Safety rules enforced here, whatever the caller (AGENT-1 / AGENT-5):
- `alert_emergency_contact_tool` refuses unless the ESI is 1 or 2.
- Each tool runs at most once per case: idempotency key `tool:{case_id}:{tool_name}` in DynamoDB.
  A repeat call returns the stored result (`idempotent_replay: True`) instead of acting again, so a
  looping model, an SQS redelivery or an AgentCore -> local fallback can't call the contact twice.

There is no ambulance tool: the assigned doctor calls the ambulance with the location the patient
shares on WhatsApp (`app.pipeline`).
"""
import asyncio
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Tuple

from app.services.telephony_service import telephony_service
from app.services.dynamo_service import dynamo_service
from app.services.chime_service import chime_service

DISPATCH_ESI_LEVELS = (1, 2)
GENERAL_EMERGENCY_SPECIALTIES = ("General Emergency", "Emergency Medicine")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def idempotency_key(case_id: str, tool_name: str) -> str:
    return f"tool:{case_id}:{tool_name}"


def _esi(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _refuse_unless_dispatchable(tool_name: str, esi_level: Any) -> Optional[Dict[str, Any]]:
    esi = _esi(esi_level)
    if esi not in DISPATCH_ESI_LEVELS:
        print(f"[Tools] REFUSED {tool_name}: ESI {esi_level} is not 1-2.")
        return {
            "tool": tool_name,
            "status": "REFUSED_ESI",
            "esi_level": esi,
            "reason": "Emergency tools only run for ESI 1-2.",
        }
    return None


async def run_once(case_id: str, tool_name: str, action: Callable[[], Awaitable[Dict[str, Any]]]) -> Dict[str, Any]:
    """Runs `action` at most once per (case, tool). Failed runs release the claim so a retry can act."""
    key = idempotency_key(case_id, tool_name)
    claimed = await asyncio.to_thread(dynamo_service.claim_idempotency_key, key)
    if not claimed:
        record = await asyncio.to_thread(dynamo_service.get_idempotency_record, key) or {}
        if record.get("status") == "COMPLETE" and record.get("result"):
            print(f"[Tools] {tool_name} already ran for {case_id}; returning the stored result.")
            return {**record["result"], "idempotent_replay": True}
        print(f"[Tools] {tool_name} already in progress for {case_id}; not running it again.")
        return {"tool": tool_name, "status": "DUPLICATE_SUPPRESSED", "case_id": case_id,
                "reason": "This tool is already running for this case."}
    try:
        result = await action()
    except Exception as e:
        await asyncio.to_thread(dynamo_service.release_idempotency_key, key)
        print(f"[Tools] {tool_name} raised for {case_id}: {e}")
        return {"tool": tool_name, "status": "FAILED", "case_id": case_id, "error": str(e)}
    if result.get("status") == "FAILED":
        await asyncio.to_thread(dynamo_service.release_idempotency_key, key)
    else:
        await asyncio.to_thread(dynamo_service.complete_idempotency_key, key, result)
    return result


async def alert_emergency_contact_tool(
    case_id: str,
    esi_level: int,
    contact_phone: Optional[str],
    patient_name: str,
    emergency_details: str,
    language: str = "Urdu",
) -> Dict[str, Any]:
    """Calls the registered emergency contact through Amazon Connect. ESI 1-2 only, once per case.
    No registered contact: returns SKIPPED_NO_CONTACT without calling anyone."""
    refusal = _refuse_unless_dispatchable("alert_emergency_contact", esi_level)
    if refusal:
        return refusal
    if not contact_phone:
        return {"tool": "alert_emergency_contact", "status": "SKIPPED_NO_CONTACT", "case_id": case_id,
                "reason": "No emergency contact registered for this patient."}

    async def _call() -> Dict[str, Any]:
        started = _now()
        res = await telephony_service.call_emergency_contact(
            contact_phone=contact_phone,
            patient_name=patient_name,
            emergency_details=emergency_details,
            language=language,
        )
        out = {
            "tool": "alert_emergency_contact",
            "status": res.get("status", "FAILED"),
            "case_id": case_id,
            "contact_phone": contact_phone,
            "contact_id": res.get("contact_id"),
            "provider": res.get("provider"),
            "simulated": res.get("simulated", False),
            "language": language,
            "started_at": started,
            "finished_at": _now(),
        }
        if res.get("error"):
            out["error"] = res["error"]
        return out

    return await run_once(case_id, "alert_emergency_contact", _call)


def doctor_is_busy(doctor: Any, loads: Dict[str, int], case_id: Optional[str] = None,
                   now: Optional[str] = None) -> bool:
    """One case per doctor (OFFER-4): True if the doctor holds another open case or a pending offer
    for another case. `loads` is `open_case_loads` without `case_id`. A lock whose case is closed or
    no longer theirs (a missed release) does not count; nor does an expired offer lock."""
    if loads.get(doctor.doctor_id, 0) > 0:
        return True
    locked_to = getattr(doctor, "busy_case_id", None)
    if not locked_to or locked_to == case_id:
        return False
    if getattr(doctor, "busy_kind", None) == "OFFER":
        return (getattr(doctor, "busy_until", None) or "") > (now or _now())
    return False  # a CASE lock on a case that no longer loads them: stale


def number_confirmed(doctor: Any) -> bool:
    """DOC-8 (app/doctor_verification.py): only doctors whose WhatsApp number is confirmed (or who were
    registered before confirmation existed) get offers. Kept here: the Gateway Lambda imports tools.py."""
    return getattr(doctor, "phone_status", None) in (None, "CONFIRMED")


def rank_candidates(required_specialty: str, exclude: Iterable[str] = (),
                    case_id: Optional[str] = None) -> List[Tuple[Any, str]]:
    """FREE active doctors to offer a case to, best first, as (doctor, tier) pairs (AGENT-6, OFFER-1).

    Tiers in order: the specialty, then General Emergency, then any active doctor. Within a tier:
    fewest lifetime cases, then ID. A doctor who holds another open case or a pending offer for
    another case is skipped (OFFER-4: one case per doctor), and so is one whose WhatsApp number is
    not confirmed yet (DOC-8). `exclude`: doctor IDs that declined or let
    an offer for this case time out. `case_id`: the case being offered; its own pending offer does
    not make its doctors busy.
    """
    specialty = (required_specialty or "General Emergency").strip()
    skip = set(exclude or ())
    tiers = [
        ("specialty", dynamo_service.get_available_doctors_by_specialty(specialty)),
        ("general_emergency", [d for s in GENERAL_EMERGENCY_SPECIALTIES
                               for d in dynamo_service.get_available_doctors_by_specialty(s)]),
        ("any_active", dynamo_service.list_doctors()),
    ]
    ranked: List[Tuple[Any, str]] = []
    seen = set(skip)
    loads = dynamo_service.open_case_loads(exclude_case_id=case_id) if case_id else dynamo_service.open_case_loads()
    now = _now()
    for tier, doctors in tiers:
        # De-duplicate (the GSI and the in-memory store can both return a doctor; tiers overlap).
        fresh = list({d.doctor_id: d for d in doctors if d.is_active and d.doctor_id not in seen
                      and number_confirmed(d) and not doctor_is_busy(d, loads, case_id, now)}.values())
        fresh.sort(key=lambda d: (d.cases_handled, d.doctor_id))
        ranked += [(d, tier) for d in fresh]
        seen |= {d.doctor_id for d in fresh}
    return ranked


async def match_specialist_doctor_tool(
    required_specialty: str,
    patient_phone: str,
    clinical_summary: str,
    case_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Ranks the free on-duty doctors to offer the case to (OFFER-1, OFFER-4): the required specialty
    (Cardiology, Neurology, Trauma, Pulmonology, General Emergency) first, then General Emergency,
    then any active doctor; a doctor already holding a case or an offer is skipped. Assigns nobody:
    the first doctor to accept the offer gets the case. With a case_id it runs once per case.

    Returns keys: status (OFFER_PENDING | NO_DOCTOR_AVAILABLE), candidates (doctor_id, doctor_name,
    specialty, match_tier), requested_specialty.
    """
    async def _match() -> Dict[str, Any]:
        started = _now()
        ranked = await asyncio.to_thread(rank_candidates, required_specialty)
        result: Dict[str, Any] = {
            "tool": "match_specialist_doctor",
            "status": "OFFER_PENDING" if ranked else "NO_DOCTOR_AVAILABLE",
            "case_id": case_id,
            "specialty": required_specialty,
            "requested_specialty": required_specialty,
            "candidates": [{"doctor_id": d.doctor_id, "doctor_name": d.name, "specialty": d.specialty,
                            "match_tier": tier} for d, tier in ranked],
            "started_at": started,
            "finished_at": _now(),
        }
        if not ranked:
            print(f"[Tools] No active doctor available for {required_specialty}.")
        return result

    if case_id:
        return await run_once(case_id, "match_specialist_doctor", _match)
    return await _match()


async def generate_video_session_tool(
    case_id: str,
    doctor_name: str
) -> Dict[str, Any]:
    """Creates (or reuses) the Amazon Chime video room for the case and returns the join links."""
    session = await chime_service.create_emergency_video_session(case_id, doctor_name)
    return {
        "tool": "generate_video_session",
        "status": "SESSION_CREATED",
        "case_id": case_id,
        "meeting_id": session["meeting_id"],
        "patient_join_url": session["patient_join_url"],
        "doctor_join_url": session["doctor_join_url"]
    }


# Name -> implementation, shared by LocalToolBackend and the Gateway Lambda handler.
TOOL_FUNCTIONS: Dict[str, Callable[..., Awaitable[Dict[str, Any]]]] = {
    "alert_emergency_contact": alert_emergency_contact_tool,
    "match_specialist_doctor": match_specialist_doctor_tool,
    "generate_video_session": generate_video_session_tool,
}


class LocalToolBackend:
    """Runs the tools in-process (AGENT_BACKEND=local, mock/Floci, and the worker's fallback path)."""
    name = "local"

    async def call(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        fn = TOOL_FUNCTIONS.get(tool_name)
        if fn is None:
            return {"tool": tool_name, "status": "FAILED", "error": f"unknown tool {tool_name}"}
        return await fn(**arguments)


local_tool_backend = LocalToolBackend()
