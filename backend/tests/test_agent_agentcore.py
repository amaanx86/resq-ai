"""AGENT-2 (code): AgentCore Runtime client, runtime app, Gateway Lambda handler, SigV4 MCP auth."""
import inspect
import io
import json
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import boto3
import httpx
import pytest
from botocore.response import StreamingBody
from botocore.stub import ANY, Stubber

from app.agents import tools
from app.agents.agentcore_client import AgentCoreRuntimeClient, runtime_session_id
from app.agents.gateway_backend import GatewayToolBackend, SigV4HttpxAuth, parse_mcp_tool_result
from app.agents.triage_agent import triage_agent
from app.models.schemas import TriageAssessment

BACKEND = Path(__file__).resolve().parent.parent
ARN = "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/resq_triage-abc123"


def _case() -> str:
    return f"CASE-A{uuid.uuid4().hex[:8].upper()}"


def _client():
    return boto3.client("bedrock-agentcore", region_name="us-east-1",
                        aws_access_key_id="test", aws_secret_access_key="test")


def _body(data: dict) -> StreamingBody:
    raw = json.dumps(data).encode()
    return StreamingBody(io.BytesIO(raw), len(raw))


def _assessment(**overrides) -> dict:
    data = TriageAssessment(
        is_emergency=True, esi_level=1, confidence_score=0.97, detected_language="Urdu",
        clinical_summary="Suspected MI.", matched_specialty="Cardiology",
        emergency_contact_called=True,
        contact_call_id="CONN-RUNTIME1", reasoning="r",
        triage_engine="agentcore/bedrock:claude",
        tool_results=[{"tool": "alert_emergency_contact", "status": "CALL_INITIATED", "contact_id": "CONN-RUNTIME1",
                       "backend": "gateway"}],
    ).model_dump(mode="json")
    data.update(overrides)
    return data


def test_runtime_session_id_is_deterministic_and_long_enough():
    sid = runtime_session_id("CASE-1")
    assert len(sid) >= 33 and sid == runtime_session_id("CASE-1") and sid != runtime_session_id("CASE-2")


@pytest.mark.asyncio
async def test_client_invokes_runtime_with_contract_payload():
    client = _client()
    case_id = _case()
    expected_payload = {"case_id": case_id, "transcript": "dil ka daura", "language": "Urdu",
                        "user_phone": "+92300", "patient_name": "P", "emergency_contact_phone": "+92333"}
    with Stubber(client) as stub:
        stub.add_response(
            "invoke_agent_runtime",
            {"response": _body(_assessment()), "contentType": "application/json", "statusCode": 200},
            {"agentRuntimeArn": ARN, "runtimeSessionId": runtime_session_id(case_id),
             "payload": json.dumps(expected_payload).encode(), "contentType": "application/json",
             "accept": "application/json"},
        )
        rt = AgentCoreRuntimeClient(ARN, "us-east-1", client=client)
        assessment = await rt.invoke(case_id=case_id, transcript="dil ka daura", language="Urdu",
                                     user_phone="+92300", patient_name="P", emergency_contact_phone="+92333")
        stub.assert_no_pending_responses()
    assert assessment.contact_call_id == "CONN-RUNTIME1"
    assert assessment.triage_engine == "agentcore/bedrock:claude"


@pytest.mark.asyncio
async def test_triage_agent_uses_agentcore_when_configured(monkeypatch):
    client = _client()
    with Stubber(client) as stub:
        stub.add_response("invoke_agent_runtime", {"response": _body(_assessment()), "contentType": "application/json", "statusCode": 200},
                          {"agentRuntimeArn": ARN, "runtimeSessionId": ANY, "payload": ANY,
                           "contentType": "application/json", "accept": "application/json"})
        monkeypatch.setattr(triage_agent, "agentcore", AgentCoreRuntimeClient(ARN, "us-east-1", client=client))
        assessment = await triage_agent.evaluate_and_dispatch(
            transcript="dil ka daura", user_phone="+92300", patient_name="P", emergency_contact_phone="+92333",
            detected_language="Urdu", case_id=_case())
    assert assessment.contact_call_id == "CONN-RUNTIME1"
    assert assessment.degraded is False


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["client_error", "runtime_error_body"])
async def test_agentcore_failure_falls_back_to_local_agent_degraded(monkeypatch, failure):
    client = _client()
    with Stubber(client) as stub:
        params = {"agentRuntimeArn": ARN, "runtimeSessionId": ANY, "payload": ANY,
                  "contentType": "application/json", "accept": "application/json"}
        if failure == "client_error":
            stub.add_client_error("invoke_agent_runtime", "ThrottlingException", "slow down", 429, expected_params=params)
        else:
            stub.add_response("invoke_agent_runtime",
                              {"response": _body({"error": "emergency tools failed: alert_emergency_contact"}),
                               "contentType": "application/json", "statusCode": 200},
                              params)
        monkeypatch.setattr(triage_agent, "agentcore", AgentCoreRuntimeClient(ARN, "us-east-1", client=client))
        assessment = await triage_agent.evaluate_and_dispatch(
            transcript="Mere uncle ko dil ka daura par raha hai", user_phone="+92300", patient_name="P",
            emergency_contact_phone="+92333", detected_language="Urdu", case_id=_case())
    assert assessment.degraded is True
    assert assessment.emergency_contact_called is True  # local tools ran
    assert {r["backend"] for r in assessment.tool_results} == {"local"}
    assert "AgentCore runtime unavailable" in assessment.reasoning


@pytest.mark.asyncio
async def test_runtime_entrypoint_returns_assessment_json(monkeypatch):
    import agent_runtime.app as runtime

    monkeypatch.setattr(runtime, "tool_backend", lambda: tools.local_tool_backend)
    out = await runtime.invoke({"case_id": _case(), "transcript": "car accident, khoon", "language": "Hindi",
                                "user_phone": "+91981", "patient_name": "Amit", "emergency_contact_phone": "+91987"})
    assessment = TriageAssessment.model_validate(out)
    assert assessment.esi_level == 2 and assessment.emergency_contact_called
    assert assessment.triage_engine.startswith("agentcore/")
    assert {r["backend"] for r in assessment.tool_results} == {"gateway"}  # the runtime's backend label


@pytest.mark.asyncio
async def test_runtime_returns_200_assessment_with_failed_tools(monkeypatch):
    """A FAILED gateway tool no longer turns the answer into a 502: the runtime's decision and the
    tools that did run come back as a normal assessment, with the failures flagged."""
    import agent_runtime.app as runtime

    class HalfDown:
        name = "gateway"

        async def call(self, tool_name, arguments):
            if tool_name == "alert_emergency_contact":
                raise ConnectionError("gateway unreachable")
            return await tools.local_tool_backend.call(tool_name, arguments)

    monkeypatch.setattr(runtime, "tool_backend", lambda: HalfDown())
    out = await runtime.invoke({"case_id": _case(), "transcript": "dil ka daura", "user_phone": "+92300",
                                "emergency_contact_phone": "+92333"})
    assert isinstance(out, dict)  # not a JSONResponse: HTTP 200
    assert out["tool_failures"] == ["alert_emergency_contact"]
    assessment = TriageAssessment.model_validate(out)
    assert assessment.esi_level == 1 and assessment.is_emergency
    assert assessment.emergency_contact_called is False
    assert {r["tool"]: r["status"] for r in assessment.tool_results}["match_specialist_doctor"] == "OFFER_PENDING"
    assert {r["tool"]: r["status"] for r in assessment.tool_results}["alert_emergency_contact"] == "FAILED"
    assert "Tool failures via the AgentCore Gateway: alert_emergency_contact" in assessment.reasoning
    bad = await runtime.invoke({"transcript": ""})
    assert bad.status_code == 400


def _no_reclassify(monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("the worker must not re-classify an assessment AgentCore returned")
    monkeypatch.setattr(triage_agent.engine, "classify", boom)


@pytest.mark.asyncio
@pytest.mark.parametrize("contract", ["v2_200", "legacy_502_partial"])
async def test_agentcore_assessment_with_failed_tool_is_kept_and_only_that_tool_retried(monkeypatch, contract):
    case_id = _case()
    runtime_assessment = _assessment(
        emergency_contact_called=False, contact_call_id=None,
        tool_results=[
            {"tool": "alert_emergency_contact", "status": "FAILED", "error": "gateway unreachable", "backend": "gateway"},
            {"tool": "match_specialist_doctor", "status": "OFFER_PENDING", "backend": "gateway",
             "candidates": [{"doctor_id": "DOC-CARDIO-01", "doctor_name": "Dr. Sarah Khan, MD"}]},
        ])
    if contract == "v2_200":
        response = {"response": _body({**runtime_assessment, "tool_failures": ["alert_emergency_contact"]}),
                    "contentType": "application/json", "statusCode": 200}
    else:
        response = {"response": _body({"error": "emergency tools failed: alert_emergency_contact", "case_id": case_id,
                                       "partial_assessment": runtime_assessment}),
                    "contentType": "application/json", "statusCode": 502}
    client = _client()
    retried = []
    real_call = tools.local_tool_backend.call

    async def spy_call(tool_name, arguments):
        retried.append(tool_name)
        return await real_call(tool_name, arguments)

    monkeypatch.setattr(triage_agent.tool_backend, "call", spy_call)
    _no_reclassify(monkeypatch)
    with Stubber(client) as stub:
        stub.add_response("invoke_agent_runtime", response,
                          {"agentRuntimeArn": ARN, "runtimeSessionId": ANY, "payload": ANY,
                           "contentType": "application/json", "accept": "application/json"})
        monkeypatch.setattr(triage_agent, "agentcore", AgentCoreRuntimeClient(ARN, "us-east-1", client=client))
        assessment = await triage_agent.evaluate_and_dispatch(
            transcript="dil ka daura", user_phone="+92300", patient_name="P", emergency_contact_phone="+92333",
            detected_language="Urdu", case_id=case_id)
    assert retried == ["alert_emergency_contact"]     # only the failed tool, nothing re-run
    assert assessment.degraded is False               # the runtime's decision stands
    assert assessment.triage_engine == "agentcore/bedrock:claude"
    assert assessment.esi_level == 1
    assert assessment.emergency_contact_called is True and assessment.contact_call_id
    assert ("match_specialist_doctor", "OFFER_PENDING", "gateway") in [   # kept from the runtime
        (r["tool"], r["status"], r.get("backend")) for r in assessment.tool_results]
    statuses = [(r["tool"], r["status"], r.get("backend")) for r in assessment.tool_results]
    assert ("alert_emergency_contact", "FAILED", "gateway") in statuses       # audit trail kept
    assert ("alert_emergency_contact", "CALL_INITIATED", "local") in statuses


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"<html>bad gateway</html>", b'{"esi_level": "not-a-number"}'])
async def test_agentcore_invalid_payload_falls_back(monkeypatch, body):
    client = _client()
    with Stubber(client) as stub:
        stub.add_response("invoke_agent_runtime",
                          {"response": StreamingBody(io.BytesIO(body), len(body)), "contentType": "application/json",
                           "statusCode": 200},
                          {"agentRuntimeArn": ARN, "runtimeSessionId": ANY, "payload": ANY,
                           "contentType": "application/json", "accept": "application/json"})
        monkeypatch.setattr(triage_agent, "agentcore", AgentCoreRuntimeClient(ARN, "us-east-1", client=client))
        assessment = await triage_agent.evaluate_and_dispatch(
            transcript="Mere uncle ko dil ka daura par raha hai", user_phone="+92300", patient_name="P",
            emergency_contact_phone="+92333", detected_language="Urdu", case_id=_case())
    assert assessment.degraded is True and "AgentCore runtime unavailable" in assessment.reasoning


@pytest.mark.asyncio
async def test_fallback_after_timeout_reports_in_progress_call_as_pending(monkeypatch):
    """AgentCore timed out while the gateway still holds the contact-call claim: the local fallback
    gets DUPLICATE_SUPPRESSED, which must read as a pending call, never as 'nobody was called'."""
    from app.services.dynamo_service import dynamo_service

    case_id = _case()
    assert dynamo_service.claim_idempotency_key(tools.idempotency_key(case_id, "alert_emergency_contact"))

    class TimedOut:
        async def invoke(self, **kwargs):
            raise TimeoutError("Read timeout on endpoint URL")

    monkeypatch.setattr(triage_agent, "agentcore", TimedOut())
    assessment = await triage_agent.evaluate_and_dispatch(
        transcript="Mere uncle ko dil ka daura par raha hai", user_phone="+92300", patient_name="P",
        emergency_contact_phone="+92333", detected_language="Urdu", case_id=case_id)
    call = [r for r in assessment.tool_results if r["tool"] == "alert_emergency_contact"]
    assert len(call) == 1 and call[0]["status"] == "IN_PROGRESS" and call[0]["pending"] is True
    assert call[0]["suppressed_status"] == "DUPLICATE_SUPPRESSED"
    assert assessment.emergency_contact_called is True and assessment.contact_call_id is None
    assert assessment.degraded is True
    assert {r["tool"]: r["status"] for r in assessment.tool_results}["match_specialist_doctor"] == "OFFER_PENDING"
    assert "Still in progress" in assessment.reasoning and "alert_emergency_contact" in assessment.reasoning


def test_suppressed_tool_uses_the_stored_result_once_the_claim_completed():
    from app.services.dynamo_service import dynamo_service

    case_id = _case()
    key = tools.idempotency_key(case_id, "alert_emergency_contact")
    dynamo_service.claim_idempotency_key(key)
    dynamo_service.complete_idempotency_key(key, {"tool": "alert_emergency_contact", "status": "CALL_INITIATED",
                                                  "contact_id": "CONN-GW1"})
    raw = TriageAssessment.model_validate(_assessment(
        emergency_contact_called=False, contact_call_id=None,
        tool_results=[{"tool": "alert_emergency_contact", "status": "DUPLICATE_SUPPRESSED", "case_id": case_id}]))
    out = triage_agent._reconcile(raw, case_id)
    assert out.emergency_contact_called is True and out.contact_call_id == "CONN-GW1"
    assert out.tool_results[0]["idempotent_replay"] is True


def test_runtime_does_not_import_the_service_layer():
    code = ("import sys, os; os.environ['MOCK_AWS']='True'; import agent_runtime.app; "
            "bad=[m for m in sys.modules if m.startswith('app.services') or m.startswith('fastapi')]; "
            "print(bad); sys.exit(1 if bad else 0)")
    result = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, capture_output=True, text=True,
                            env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"})
    assert result.returncode == 0, result.stdout + result.stderr


# ---------------------------------------------------------------- Gateway Lambda handler
def _context(tool_name):
    return SimpleNamespace(client_context=SimpleNamespace(custom={
        "bedrockAgentCoreToolName": f"resq-gateway-tools___{tool_name}", "bedrockAgentCoreGatewayId": "gw"}))


def test_lambda_handler_routes_by_client_context_tool_name():
    from gateway_tools.handler import lambda_handler

    case_id = _case()
    event = {"case_id": case_id, "esi_level": 1, "contact_phone": "+92333", "patient_name": "P",
             "emergency_details": "Chest pain."}
    first = lambda_handler(event, _context("alert_emergency_contact"))
    second = lambda_handler(event, _context("alert_emergency_contact"))
    assert first["status"] == "CALL_INITIATED" and second["idempotent_replay"] is True
    json.dumps(first)  # JSON-serialisable for the Gateway

    refused = lambda_handler({**event, "case_id": _case(), "esi_level": 3}, _context("alert_emergency_contact"))
    assert refused["status"] == "REFUSED_ESI"
    skipped = lambda_handler({"case_id": case_id, "esi_level": 1, "patient_name": "P", "emergency_details": "x"},
                             _context("alert_emergency_contact"))
    assert skipped["status"] == "SKIPPED_NO_CONTACT"
    assert lambda_handler({}, _context("rm_rf"))["status"] == "FAILED"
    assert lambda_handler({}, _context("dispatch_ambulance"))["status"] == "FAILED"  # removed tool
    assert lambda_handler({"case_id": case_id}, _context("alert_emergency_contact"))["status"] == "FAILED"


def test_tool_schemas_match_the_tool_signatures():
    schemas = json.loads((BACKEND / "gateway_tools" / "tool_schemas.json").read_text(encoding="utf-8"))
    assert {s["name"] for s in schemas} == set(tools.TOOL_FUNCTIONS)
    for schema in schemas:
        params = inspect.signature(tools.TOOL_FUNCTIONS[schema["name"]]).parameters
        props = schema["inputSchema"]["properties"]
        assert set(props) == set(params), schema["name"]
        required_by_code = {n for n, p in params.items() if p.default is inspect.Parameter.empty and n != "contact_phone"}
        assert set(schema["inputSchema"]["required"]) == required_by_code, schema["name"]
        assert schema["description"] and schema["inputSchema"]["type"] == "object"
        for prop in props.values():
            assert prop["type"] in ("string", "number", "integer", "boolean", "array", "object")


# ---------------------------------------------------------------- Gateway MCP client (SigV4)
def test_sigv4_auth_signs_requests_for_bedrock_agentcore():
    session = boto3.Session(aws_access_key_id="AKIDTEST", aws_secret_access_key="secret",
                            aws_session_token="token", region_name="us-east-1")
    auth = SigV4HttpxAuth("us-east-1", session=session)
    request = httpx.Request("POST", "https://gw-123.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp",
                            content=b'{"jsonrpc":"2.0"}', headers={"content-type": "application/json"})
    signed = next(auth.auth_flow(request))
    authz = signed.headers["Authorization"]
    assert authz.startswith("AWS4-HMAC-SHA256 Credential=AKIDTEST/")
    assert "/us-east-1/bedrock-agentcore/aws4_request" in authz
    assert "SignedHeaders=content-type;host;x-amz-date;x-amz-security-token" in authz
    assert signed.headers["X-Amz-Security-Token"] == "token"


def test_parse_mcp_tool_result_variants():
    payload = {"tool": "alert_emergency_contact", "status": "CALL_INITIATED", "contact_id": "CONN-1"}
    assert parse_mcp_tool_result("alert_emergency_contact", {"status": "success", "content": [{"text": json.dumps(payload)}]}) == payload
    assert parse_mcp_tool_result("x", {"status": "success", "structuredContent": payload, "content": []})["contact_id"] == "CONN-1"
    err = parse_mcp_tool_result("x", {"status": "error", "content": [{"text": "Lambda timed out"}]})
    assert err["status"] == "FAILED" and "timed out" in err["error"]


@pytest.mark.asyncio
async def test_gateway_backend_maps_prefixed_tool_names_and_drops_nulls():
    calls = []

    class FakeMCP:
        def start(self):
            return self

        def list_tools_sync(self, pagination_token=None):
            return [SimpleNamespace(tool_name=f"resq-gateway-tools___{n}") for n in tools.TOOL_FUNCTIONS]

        async def call_tool_async(self, tool_use_id, name, arguments):
            calls.append((name, arguments))
            return {"status": "success", "content": [{"text": json.dumps({"status": "SKIPPED_NO_CONTACT"})}]}

    backend = GatewayToolBackend("https://gw.example.com/mcp", "us-east-1", client_factory=FakeMCP)
    res = await backend.call("alert_emergency_contact", {"case_id": "C", "esi_level": 1, "contact_phone": None})
    assert res["status"] == "SKIPPED_NO_CONTACT" and res["tool"] == "alert_emergency_contact"
    assert calls == [("resq-gateway-tools___alert_emergency_contact", {"case_id": "C", "esi_level": 1})]
    missing = await backend.call("unknown_tool", {})
    assert missing["status"] == "FAILED"
