"""Triage agent facade used by the pipeline.

Backends (settings.AGENT_BACKEND):
- "local": the Strands agent in `core.py` runs in-process with the tools in `tools.py`.
- "agentcore": the same agent runs on Bedrock AgentCore Runtime (`agent_runtime/app.py`) and reaches
  the tools through the AgentCore Gateway. The runtime always answers with its full assessment, even
  when a tool FAILED; that decision is kept (no second Bedrock run) and only the FAILED tools are
  retried in-process. Only a transport error, timeout or invalid payload falls back to the local
  agent, with `degraded=True`. The tools are idempotent per case in DynamoDB, so neither path can
  repeat a call or handover the runtime already made.
- A tool whose per-case claim is still IN_PROGRESS elsewhere (e.g. the runtime timed out while the
  gateway was calling the contact) returns DUPLICATE_SUPPRESSED. It is reported as pending (status
  "IN_PROGRESS", `pending: True`) and counts as called, never as "no call".

Mock mode (MOCK_AWS=True, or Floci, which doesn't emulate Bedrock): no Bedrock call at all; the
keyword classifier produces the same structured decision and drives the same tools.
"""
from typing import Any, Dict, List, Optional

from app.config import settings
from app.models.schemas import TriageAssessment
from app.agents.core import (  # noqa: F401  (re-exported for callers and tests)
    DISPATCH_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    TriageDecision,
    TriageEngine,
    heuristic_clinical_eval,
    new_case_id,
    parse_triage_json,
)
from app.agents.core import REQUIRED_EMERGENCY_TOOLS, TriageRun
from app.agents.tools import idempotency_key, local_tool_backend
from app.services.dynamo_service import dynamo_service
from app.services.memory_service import patient_memory

PENDING_STATUS = "IN_PROGRESS"
_SUCCESS_STATUS = {"alert_emergency_contact": "CALL_INITIATED", "match_specialist_doctor": "OFFER_PENDING"}


def _latest_results(tool_results: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    latest: Dict[str, Dict[str, Any]] = {}
    for r in tool_results or []:
        if r.get("tool"):
            latest[r["tool"]] = r
    return latest


def _resolve_suppressed(case_id: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """DUPLICATE_SUPPRESSED means another caller holds the tool's claim for this case. Use its stored
    result if it has finished since; otherwise report the tool as pending, not as not-run."""
    tool = result.get("tool")
    try:
        record = dynamo_service.get_idempotency_record(idempotency_key(case_id, tool)) or {}
    except Exception as e:
        print(f"[TriageAgent] Could not read the {tool} claim for {case_id}: {e}")
        record = {}
    if record.get("status") == "COMPLETE" and record.get("result"):
        return {**result, **record["result"], "idempotent_replay": True}
    return {**result, "status": PENDING_STATUS, "pending": True, "suppressed_status": result.get("status"),
            "reason": "Started by an earlier attempt for this case and still in progress; not repeated."}


class TriageAgent:
    def __init__(self):
        self.model_id = settings.BEDROCK_MODEL_ID
        self.fallback_model_id = settings.BEDROCK_FALLBACK_MODEL_ID
        self.mock_mode = settings.MOCK_AWS or bool(settings.AWS_ENDPOINT_URL)
        self.backend = (settings.AGENT_BACKEND or "local").lower()
        self.engine = TriageEngine(
            primary_model_id=self.model_id,
            fallback_model_id=self.fallback_model_id,
            region=settings.AWS_REGION,
            use_models=not self.mock_mode,
            memory=patient_memory,  # off in mock/Floci; used by the in-process path and the fallback
        )
        self.tool_backend = local_tool_backend
        self.agentcore = None

        if self.mock_mode:
            why = "Floci does not emulate Bedrock" if settings.AWS_ENDPOINT_URL else "MOCK_AWS=True"
            print(f"[TriageAgent] MOCK mode ({why}): keyword classifier, no Bedrock calls; tools run in-process.")
            if self.backend == "agentcore":
                print("[TriageAgent] WARNING: AGENT_BACKEND=agentcore ignored in mock mode.")
        elif self.backend == "agentcore":
            if not settings.AGENTCORE_RUNTIME_ARN:
                print("[TriageAgent] WARNING: AGENT_BACKEND=agentcore but AGENTCORE_RUNTIME_ARN is not set; "
                      "using the in-process Strands agent.")
            else:
                try:
                    from app.agents.agentcore_client import AgentCoreRuntimeClient
                    self.agentcore = AgentCoreRuntimeClient(settings.AGENTCORE_RUNTIME_ARN, settings.AWS_REGION)
                    print(f"[TriageAgent] AgentCore Runtime backend: {settings.AGENTCORE_RUNTIME_ARN} "
                          f"(fallback: in-process Strands agent).")
                except Exception as e:
                    print(f"[TriageAgent] WARNING: could not create the AgentCore client ({e}); using the in-process agent.")
        if not self.mock_mode and self.agentcore is None:
            print(f"[TriageAgent] Strands agent in-process on Bedrock: {self.model_id} (fallback {self.fallback_model_id}).")

    async def evaluate_and_dispatch(
        self,
        transcript: str,
        user_phone: str,
        patient_name: str,
        emergency_contact_phone: Optional[str],
        detected_language: str = "Urdu",
        case_id: Optional[str] = None,
    ) -> TriageAssessment:
        """Classifies the transcript (ESI 1-5) and, for ESI 1-2 only, runs the emergency tools.
        The returned assessment carries the tool results and the real contact-call/doctor IDs."""
        case_id = case_id or new_case_id()

        if self.agentcore is not None:
            try:
                assessment = await self.agentcore.invoke(
                    case_id=case_id, transcript=transcript, language=detected_language,
                    user_phone=user_phone, patient_name=patient_name,
                    emergency_contact_phone=emergency_contact_phone,
                )
            except Exception as e:
                print(f"[TriageAgent] AgentCore invocation FAILED for {case_id}: {e}. "
                      f"Falling back to the in-process agent (degraded).")
                assessment = await self.engine.run(
                    case_id=case_id, transcript=transcript, language=detected_language,
                    user_phone=user_phone, patient_name=patient_name,
                    emergency_contact_phone=emergency_contact_phone, backend=self.tool_backend,
                    force_degraded=True, note=f"AgentCore runtime unavailable ({type(e).__name__}); triaged in-process.",
                )
                return self._reconcile(assessment, case_id)
            if not assessment.triage_engine:
                assessment.triage_engine = "agentcore"
            retried = await self._retry_failed_tools(assessment, case_id, transcript, detected_language,
                                                     user_phone, patient_name, emergency_contact_phone)
            return self._reconcile(assessment, case_id, changed=retried)

        assessment = await self.engine.run(
            case_id=case_id, transcript=transcript, language=detected_language,
            user_phone=user_phone, patient_name=patient_name,
            emergency_contact_phone=emergency_contact_phone, backend=self.tool_backend,
        )
        return self._reconcile(assessment, case_id)

    async def _retry_failed_tools(self, assessment: TriageAssessment, case_id: str, transcript: str,
                                  language: str, user_phone: str, patient_name: str,
                                  emergency_contact_phone: Optional[str]) -> List[str]:
        """AgentCore answered but some emergency tools FAILED on the gateway: run just those tools
        in-process with the runtime's ESI decision (no re-classification). Results are appended, so
        the FAILED gateway attempts stay in the audit trail."""
        if assessment.esi_level not in (1, 2):
            return []
        latest = _latest_results(assessment.tool_results)
        failed = [t for t in REQUIRED_EMERGENCY_TOOLS if latest.get(t, {}).get("status") == "FAILED"]
        if not failed:
            return []
        print(f"[TriageAgent] AgentCore tools FAILED for {case_id}: {failed}; retrying them in-process.")
        run = TriageRun(case_id=case_id, transcript=transcript, language=language, user_phone=user_phone,
                        patient_name=patient_name, emergency_contact_phone=emergency_contact_phone,
                        backend=self.tool_backend)
        run.decision = TriageDecision(
            esi_level=assessment.esi_level, confidence_score=assessment.confidence_score,
            detected_language=assessment.detected_language, matched_specialty=assessment.matched_specialty,
            clinical_summary=assessment.clinical_summary, reasoning=assessment.reasoning,
        )
        for tool in failed:
            assessment.tool_results.append(await run.call_tool(tool, {}, driver="retry"))
        assessment.reasoning = f"{assessment.reasoning} [Retried in-process after AgentCore tool failure: {', '.join(failed)}]"
        return failed

    @staticmethod
    def _reconcile(assessment: TriageAssessment, case_id: str, changed: Optional[List[str]] = None) -> TriageAssessment:
        """Re-derives the contact/doctor fields of the tools whose result changed here (retried, or
        DUPLICATE_SUPPRESSED resolved to a stored result / pending). A claim still IN_PROGRESS
        elsewhere counts as pending: called is True, the ID is not known yet."""
        changed = set(changed or [])
        results = []
        for r in assessment.tool_results or []:
            if r.get("status") == "DUPLICATE_SUPPRESSED":
                r = _resolve_suppressed(case_id, r)
                changed.add(r.get("tool"))
            results.append(r)
        assessment.tool_results = results
        latest = {t: r for t, r in _latest_results(results).items() if t in changed}

        def state(tool: str):
            r = latest.get(tool) or {}
            return r, r.get("status") == _SUCCESS_STATUS[tool], r.get("status") == PENDING_STATUS

        call, called, call_pending = state("alert_emergency_contact")
        if call:
            assessment.emergency_contact_called = called or call_pending
            assessment.contact_call_id = call.get("contact_id") if called else None
        pending = [t for t, r in latest.items() if r.get("status") == PENDING_STATUS]
        if pending:
            assessment.reasoning = (f"{assessment.reasoning} [Still in progress from an earlier attempt "
                                    f"for this case: {', '.join(pending)}]")
        return assessment

    def _heuristic_mock_clinical_eval(self, transcript: str, language: str) -> TriageDecision:
        """Offline multilingual keyword classifier (see core.heuristic_clinical_eval)."""
        return heuristic_clinical_eval(transcript, language)


triage_agent = TriageAgent()
