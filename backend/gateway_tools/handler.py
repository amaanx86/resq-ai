"""AWS Lambda target behind the AgentCore Gateway: runs the ResQ emergency tools.

Gateway -> Lambda contract (AgentCore Gateway docs, "Add Lambda targets"):
- `event` is the tool's arguments object (the inputSchema properties), nothing else.
- The tool name is in `context.client_context.custom["bedrockAgentCoreToolName"]` as
  `<target_name>___<tool_name>`; we strip everything up to the `___` delimiter.
- The return value must be JSON-serialisable; the Gateway hands it to the MCP client as the tool result.

The tool implementations are `app.agents.tools` (the same code the in-process agent uses), so the
safety rules apply here too: the emergency-contact call refuses unless ESI is 1-2, and each tool runs
at most once per case (DynamoDB idempotency key `tool:{case_id}:{tool}`).

Environment (set by Terraform): MOCK_AWS=False, SEED_DEMO_DATA=False, DYNAMODB_TABLE_NAME,
CONNECT_INSTANCE_ID, CONNECT_FLOW_ID, CONNECT_SOURCE_PHONE_NUMBER, PORTAL_BASE_URL,
S3_AUDIO_BUCKET (AWS_REGION is provided by Lambda).
"""
import asyncio
import inspect
import json
import os
from typing import Any, Dict, Optional

os.environ.setdefault("SEED_DEMO_DATA", "False")

from app.agents.tools import TOOL_FUNCTIONS  # noqa: E402  (env defaults must be set first)

TOOL_NAME_DELIMITER = "___"


def resolve_tool_name(event: Dict[str, Any], context: Any) -> Optional[str]:
    custom = None
    client_context = getattr(context, "client_context", None)
    if client_context is not None:
        custom = getattr(client_context, "custom", None)
    if isinstance(custom, dict) and custom.get("bedrockAgentCoreToolName"):
        full = custom["bedrockAgentCoreToolName"]
    else:
        # Local testing convenience: {"tool_name": "...", ...} or {"__tool": "..."}.
        full = (event or {}).get("tool_name") or (event or {}).get("__tool")
    if not full:
        return None
    return full.split(TOOL_NAME_DELIMITER, 1)[-1]


def _arguments_for(fn, event: Dict[str, Any]) -> Dict[str, Any]:
    params = inspect.signature(fn).parameters
    args = {k: v for k, v in (event or {}).items() if k in params}
    # Optional values the Gateway can't send as null (inline schemas have no "null" type).
    if "contact_phone" in params and "contact_phone" not in args:
        args["contact_phone"] = None
    return args


async def _run(tool_name: str, event: Dict[str, Any]) -> Dict[str, Any]:
    fn = TOOL_FUNCTIONS[tool_name]
    return await fn(**_arguments_for(fn, event))


def lambda_handler(event, context):
    tool_name = resolve_tool_name(event or {}, context)
    if not tool_name or tool_name not in TOOL_FUNCTIONS:
        return {"tool": tool_name, "status": "FAILED", "error": f"unknown tool {tool_name!r}",
                "available_tools": sorted(TOOL_FUNCTIONS)}
    try:
        result = asyncio.run(_run(tool_name, event or {}))
    except TypeError as e:  # missing required argument
        return {"tool": tool_name, "status": "FAILED", "error": f"bad arguments: {e}"}
    print(f"[GatewayTools] {tool_name} -> {result.get('status')}")
    # Round-trip through JSON so the Gateway never sees a non-serialisable value.
    return json.loads(json.dumps(result, default=str))
