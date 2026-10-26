"""Client for the triage agent deployed on Bedrock AgentCore Runtime (AGENT_BACKEND=agentcore).

Contract: the runtime answers HTTP 200 with a TriageAssessment, FAILED tool results included. Only
transport errors, timeouts, HTTP errors without an assessment and invalid payloads raise, which
sends the caller (`TriageAgent`) to its in-process fallback.
"""
import asyncio
import json
import uuid
from typing import Any, Dict, Optional

from app.models.schemas import TriageAssessment

MIN_SESSION_ID_LENGTH = 33  # InvokeAgentRuntime: runtimeSessionId length 33..256


def runtime_session_id(case_id: str) -> str:
    """Deterministic per case (a retry of the same case reuses the session), >= 33 characters."""
    sid = f"resq-triage-{case_id}-{uuid.uuid5(uuid.NAMESPACE_URL, 'resq:' + case_id).hex}"
    if len(sid) < MIN_SESSION_ID_LENGTH:
        sid = sid.ljust(MIN_SESSION_ID_LENGTH, "0")
    return sid[:256]


class AgentCoreRuntimeClient:
    def __init__(self, runtime_arn: str, region: str, client: Any = None, qualifier: Optional[str] = None):
        if not runtime_arn:
            raise ValueError("AGENTCORE_RUNTIME_ARN is not set")
        self.runtime_arn = runtime_arn
        self.qualifier = qualifier
        if client is None:
            import boto3
            from botocore.config import Config
            client = boto3.client(
                "bedrock-agentcore", region_name=region,
                config=Config(read_timeout=150, connect_timeout=5, retries={"max_attempts": 1}),
            )
        self.client = client

    def _invoke_sync(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        kwargs = dict(
            agentRuntimeArn=self.runtime_arn,
            runtimeSessionId=runtime_session_id(payload["case_id"]),
            payload=json.dumps(payload).encode("utf-8"),
            contentType="application/json",
            accept="application/json",
        )
        if self.qualifier:
            kwargs["qualifier"] = self.qualifier
        res = self.client.invoke_agent_runtime(**kwargs)
        status = res.get("statusCode", 200)
        body = res["response"]
        raw = body.read() if hasattr(body, "read") else body
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8")
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            raise RuntimeError(f"AgentCore runtime returned HTTP {status} with a non-JSON body: {str(raw)[:500]}")
        if isinstance(data, dict) and isinstance(data.get("partial_assessment"), dict):
            # Older runtime contract (HTTP 502 + partial_assessment when a tool failed): the
            # classification and the tools that ran are still real, so keep them.
            data = {**data["partial_assessment"], "tool_failures": data.get("tool_failures") or []}
        elif status and int(status) >= 400:
            raise RuntimeError(f"AgentCore runtime returned HTTP {status}: {raw[:500]}")
        if not isinstance(data, dict) or "esi_level" not in data:
            err = data.get("error") if isinstance(data, dict) else None
            raise RuntimeError(f"AgentCore runtime error: {err or 'response is not a triage assessment'}")
        return data

    async def invoke(self, *, case_id: str, transcript: str, language: str, user_phone: str,
                     patient_name: str, emergency_contact_phone: Optional[str]) -> TriageAssessment:
        payload = {
            "case_id": case_id,
            "transcript": transcript,
            "language": language,
            "user_phone": user_phone,
            "patient_name": patient_name,
            "emergency_contact_phone": emergency_contact_phone,
        }
        data = await asyncio.to_thread(self._invoke_sync, payload)
        # A 200 assessment is accepted even when some tools FAILED (they are in tool_results); the
        # caller retries those tools without re-classifying. A body that does not validate raises.
        return TriageAssessment.model_validate(data)
