"""ResQ-AI triage agent on Amazon Bedrock AgentCore Runtime.

Serves the AgentCore HTTP contract on port 8080 (POST /invocations, GET /ping) through
`bedrock_agentcore.runtime.BedrockAgentCoreApp`. The worker calls it with
`bedrock-agentcore:InvokeAgentRuntime` (see app/agents/agentcore_client.py).

Request payload:  {"case_id", "transcript", "language", "user_phone", "patient_name", "emergency_contact_phone"}
Response:         HTTP 200 with the TriageAssessment JSON (app/models/schemas.py), FAILED tool
                  results included, plus "tool_failures": [tool names] when any tool failed.
                  HTTP 400 {"error"} only for an invalid request.

The agent is the same Strands agent as the in-process one (app/agents/core.py). Its tools run on
the AgentCore Gateway (MCP over HTTPS, SigV4-signed with the runtime role; Lambda target
`gateway_tools.handler.lambda_handler`), not in this container.

Patient memory (MEM-1): with AGENTCORE_MEMORY_ID set, the agent recalls the patient's facts from
AgentCore Memory (app/agents/memory.py); they can only raise urgency and come back as
"patient_history". The runtime only reads memory; the worker and the API write the events.

Environment: AWS_REGION, BEDROCK_MODEL_ID, BEDROCK_FALLBACK_MODEL_ID, AGENTCORE_GATEWAY_URL,
AGENTCORE_MEMORY_ID (optional), MOCK_AWS=False, SEED_DEMO_DATA=False, DYNAMODB_TABLE_NAME.

Run locally (needs AWS credentials, Bedrock access and a reachable gateway):
    cd backend && python -m agent_runtime.app
"""
import os

os.environ.setdefault("SEED_DEMO_DATA", "False")
os.environ.setdefault("MOCK_AWS", "False")

from bedrock_agentcore.runtime import BedrockAgentCoreApp  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402

from app.config import settings  # noqa: E402
from app.agents.core import TriageEngine, new_case_id  # noqa: E402
from app.agents.gateway_backend import GatewayToolBackend  # noqa: E402
from app.agents.memory import build_patient_memory  # noqa: E402

app = BedrockAgentCoreApp()

engine = TriageEngine(
    primary_model_id=settings.BEDROCK_MODEL_ID,
    fallback_model_id=settings.BEDROCK_FALLBACK_MODEL_ID,
    region=settings.AWS_REGION,
    use_models=not settings.MOCK_AWS,
    memory=build_patient_memory(
        settings.AGENTCORE_MEMORY_ID, settings.AWS_REGION, mock=settings.MOCK_AWS,
        top_k=settings.AGENTCORE_MEMORY_TOP_K, timeout_seconds=settings.AGENTCORE_MEMORY_TIMEOUT_SECONDS,
    ),
)
_backend = None


def tool_backend() -> GatewayToolBackend:
    """One MCP session per container, opened on first use (the gateway is only needed for ESI 1-2)."""
    global _backend
    if _backend is None:
        _backend = GatewayToolBackend(settings.AGENTCORE_GATEWAY_URL or "", settings.AWS_REGION)
    return _backend


class LazyGatewayBackend:
    """Connects to the gateway on the first tool call, so non-emergencies never need it."""
    name = "gateway"

    async def call(self, tool_name: str, arguments: dict) -> dict:
        return await tool_backend().call(tool_name, arguments)


REQUIRED_FIELDS = ("transcript", "user_phone")


@app.entrypoint
async def invoke(payload: dict) -> dict:
    missing = [f for f in REQUIRED_FIELDS if not (payload or {}).get(f)]
    if missing:
        return JSONResponse({"error": f"missing fields: {', '.join(missing)}"}, status_code=400)
    case_id = payload.get("case_id") or new_case_id()
    print(f"[AgentRuntime] triage {case_id} ({payload.get('language', 'Unknown')})")
    assessment = await engine.run(
        case_id=case_id,
        transcript=payload["transcript"],
        language=payload.get("language") or "Unknown",
        user_phone=payload["user_phone"],
        patient_name=payload.get("patient_name") or "Patient",
        emergency_contact_phone=payload.get("emergency_contact_phone"),
        backend=LazyGatewayBackend(),
    )
    assessment.triage_engine = f"agentcore/{assessment.triage_engine}"
    failed = [r.get("tool") for r in assessment.tool_results if r.get("status") == "FAILED"]
    out = assessment.model_dump(mode="json")
    if failed:
        # Still HTTP 200 with the full assessment: the classification and the tools that did run are
        # real (the contact may already have been called). The worker keeps this decision, retries
        # only the FAILED tools in-process, and never re-classifies (no second Bedrock run).
        print(f"[AgentRuntime] {case_id}: tools failed via the gateway: {failed}")
        out["tool_failures"] = failed
        out["reasoning"] = f"{out['reasoning']} [Tool failures via the AgentCore Gateway: {', '.join(failed)}]"
    return out


if __name__ == "__main__":
    app.run(port=int(os.getenv("PORT", "8080")))
