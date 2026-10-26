"""Tool backend for the AgentCore Runtime: calls the emergency tools on the AgentCore Gateway over MCP.

Inbound auth on the Gateway is AWS_IAM, so every MCP HTTP request is signed with SigV4
(service name `bedrock-agentcore`, the runtime's role credentials). Strands' MCPClient accepts any
`httpx.Auth` as `auth_provider`; `SigV4HttpxAuth` below signs with botocore, the same approach as
AWS's `mcp-proxy-for-aws` (`aws_iam_streamablehttp_client(..., aws_service="bedrock-agentcore")`)
without the extra dependency. The runtime role needs `bedrock-agentcore:InvokeGateway` on the gateway.

Gateway tool names arrive as `<target>___<tool>`; the backend maps them back by suffix, so the
Terraform target name does not have to be hard-coded here.

Imports nothing from `app.services`.
"""
import asyncio
import json
import threading
import uuid
from typing import Any, Dict, Optional

import httpx

GATEWAY_SIGNING_SERVICE = "bedrock-agentcore"
TOOL_NAME_DELIMITER = "___"


class SigV4HttpxAuth(httpx.Auth):
    """Signs httpx requests with AWS SigV4 (botocore), refreshing role credentials per request."""

    requires_request_body = True

    def __init__(self, region: str, service: str = GATEWAY_SIGNING_SERVICE, session: Any = None):
        import boto3
        self.region = region
        self.service = service
        self.session = session or boto3.Session()

    def _sign(self, request: httpx.Request) -> None:
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest

        credentials = self.session.get_credentials()
        if credentials is None:
            raise RuntimeError("No AWS credentials available to sign AgentCore Gateway requests")
        frozen = credentials.get_frozen_credentials()
        # Sign only stable headers; the transport may add/alter others (connection, user-agent).
        headers = {"host": request.url.netloc.decode() if isinstance(request.url.netloc, bytes) else request.url.netloc}
        for name in ("content-type", "accept", "mcp-session-id", "mcp-protocol-version"):
            if name in request.headers:
                headers[name] = request.headers[name]
        aws_request = AWSRequest(method=request.method, url=str(request.url), data=request.content or b"", headers=headers)
        SigV4Auth(frozen, self.service, self.region).add_auth(aws_request)
        for name in ("Authorization", "X-Amz-Date", "X-Amz-Security-Token", "X-Amz-Content-SHA256"):
            if name in aws_request.headers:
                request.headers[name] = aws_request.headers[name]

    def auth_flow(self, request: httpx.Request):
        self._sign(request)
        yield request


def parse_mcp_tool_result(tool_name: str, result: Any) -> Dict[str, Any]:
    """MCP tool result (Strands MCPToolResult dict) -> the tool's own JSON dict."""
    if not isinstance(result, dict):
        return {"tool": tool_name, "status": "FAILED", "error": f"unexpected MCP result {result!r}"}
    structured = result.get("structuredContent")
    if isinstance(structured, dict) and structured:
        data = structured
    else:
        data = None
        for block in result.get("content") or []:
            text = block.get("text") if isinstance(block, dict) else None
            if not text:
                continue
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                data = parsed
                break
        if data is None:
            texts = " ".join(b.get("text", "") for b in (result.get("content") or []) if isinstance(b, dict))
            return {"tool": tool_name, "status": "FAILED", "error": texts or "empty MCP tool result"}
    if result.get("status") == "error" or result.get("isError"):
        data = {**data, "status": data.get("status") if data.get("status") not in (None, "", "SUCCESS") else "FAILED"}
    data.setdefault("tool", tool_name)
    return data


class GatewayToolBackend:
    """ToolBackend that calls the Lambda tools behind the AgentCore Gateway (MCP, SigV4)."""

    name = "gateway"

    def __init__(self, gateway_url: str, region: str, client_factory: Optional[Any] = None):
        if not gateway_url:
            raise ValueError("AGENTCORE_GATEWAY_URL is not set")
        self.gateway_url = gateway_url
        self.region = region
        self._client_factory = client_factory or self._default_client
        self._client = None
        self._names: Dict[str, str] = {}
        self._lock = threading.Lock()

    def _default_client(self):
        from strands.tools.mcp import MCPClient
        return MCPClient(url=self.gateway_url, auth_provider=SigV4HttpxAuth(self.region), startup_timeout=15)

    def _ensure(self):
        with self._lock:
            if self._client is None:
                client = self._client_factory()
                client.start()
                names: Dict[str, str] = {}
                token = None
                while True:
                    page = client.list_tools_sync(pagination_token=token)
                    for t in page:
                        full = getattr(t, "tool_name", None) or getattr(getattr(t, "mcp_tool", None), "name", "")
                        short = full.split(TOOL_NAME_DELIMITER, 1)[-1]
                        names[short] = full
                    token = getattr(page, "pagination_token", None)
                    if not token:
                        break
                self._client, self._names = client, names
                print(f"[GatewayToolBackend] Connected to {self.gateway_url}; tools: {sorted(names)}")
            return self._client, self._names

    def reset(self) -> None:
        with self._lock:
            client, self._client = self._client, None
        if client is not None:
            try:
                client.stop(None, None, None)
            except Exception:
                pass

    async def call(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        client, names = await asyncio.to_thread(self._ensure)
        full_name = names.get(tool_name)
        if not full_name:
            return {"tool": tool_name, "status": "FAILED", "error": f"tool {tool_name} not exposed by the gateway"}
        # Drop None values: the inline schemas have no "null" type.
        args = {k: v for k, v in arguments.items() if v is not None}
        try:
            result = await client.call_tool_async(tool_use_id=f"resq-{uuid.uuid4().hex[:12]}", name=full_name, arguments=args)
        except Exception:
            self.reset()
            raise
        return parse_mcp_tool_result(tool_name, result)
