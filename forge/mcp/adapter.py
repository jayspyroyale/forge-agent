"""Turning MCP tools into ordinary Forge tools.

An MCP tool becomes a `Tool` named `<server>.<tool>` (for example
`github.create_issue`), so tools from different servers never collide with
each other or with built-in tools. From there it is registered in the same
`ToolRegistry` and runs through the same `ToolExecutor`: argument checks,
risk assessment, and the permission engine all apply.

Trust: a server describes its own tools, so nothing it says can lower their
risk. The risk comes from Forge's configuration (default: `execute`, which
asks for approval). A server's `destructiveHint` can only raise it.
"""

import re
from typing import Any

from pydantic import BaseModel, ConfigDict

from forge.config import McpServerSettings
from forge.mcp.client import McpClient, McpError, McpTimeoutError
from forge.models.types import ToolDefinition
from forge.security.risk import RiskAssessment, RiskLevel
from forge.tools.base import Tool, ToolContext, ToolError, ToolResult

TOOL_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
MAX_OUTPUT_CHARS = 20_000
MAX_DESCRIPTION_CHARS = 1_000


class McpArguments(BaseModel):
    """Arguments are passed to the server as given; the server validates them against its schema."""

    model_config = ConfigDict(extra="allow")


class McpTool(Tool):
    Args = McpArguments
    context_source = "mcp"

    def __init__(
        self,
        client: McpClient,
        server: str,
        remote_name: str,
        description: str,
        input_schema: dict[str, Any],
        risk: RiskLevel,
        timeout: float,
    ) -> None:
        self.client = client
        self.server = server
        self.remote_name = remote_name
        self.name = f"{server}.{remote_name}"  # type: ignore[misc]
        self.description = f"[MCP server '{server}'] {description}".strip()  # type: ignore[misc]
        self.input_schema = input_schema
        self.risk = risk  # type: ignore[misc]
        self.timeout = timeout

    def definition(self) -> ToolDefinition:
        return ToolDefinition(name=self.name, description=self.description, parameters=self.input_schema)

    def assess_risk(self, args: McpArguments, context: ToolContext) -> RiskAssessment:
        return RiskAssessment(level=self.risk, reasons=[f"external tool from MCP server '{self.server}'"])

    def context_reference(self, arguments: dict[str, Any]) -> str | None:
        return self.name

    def execute(self, args: McpArguments, context: ToolContext) -> ToolResult:
        arguments = args.model_dump()
        missing = [key for key in self.input_schema.get("required", []) if key not in arguments]
        if missing:
            raise ToolError(f"Missing required argument(s) for '{self.name}': {', '.join(missing)}")
        try:
            result = self.client.call_tool(self.remote_name, arguments, timeout=self.timeout)
        except McpTimeoutError as error:
            raise ToolError(str(error)) from None
        except McpError as error:
            raise ToolError(f"MCP tool '{self.name}' failed: {error}") from None
        text = result.text
        if len(text) > MAX_OUTPUT_CHARS:
            text = text[:MAX_OUTPUT_CHARS] + f"\n[... {len(text) - MAX_OUTPUT_CHARS} characters truncated ...]"
        if result.is_error:
            return ToolResult.fail(f"MCP tool '{self.name}' reported an error", output=text, mcp_server=self.server)
        return ToolResult.ok(text, mcp_server=self.server)


class SkippedTool(BaseModel):
    name: str
    reason: str


def adapt_tools(
    client: McpClient, server: str, settings: McpServerSettings, raw_tools: list[Any]
) -> tuple[list[McpTool], list[SkippedTool]]:
    """Validate the server's tool descriptions and wrap the usable ones. Malformed ones are skipped, not fatal."""
    tools: list[McpTool] = []
    skipped: list[SkippedTool] = []
    seen: set[str] = set()
    for raw in raw_tools:
        name = raw.get("name") if isinstance(raw, dict) else None
        label = str(name) if name is not None else "<unnamed>"
        problem = _problem(raw, name, seen)
        if problem:
            skipped.append(SkippedTool(name=label, reason=problem))
            continue
        if settings.tools is not None and name not in settings.tools:
            skipped.append(SkippedTool(name=label, reason="not in this server's `tools` allowlist"))
            continue
        seen.add(name)
        schema = raw.get("inputSchema") or {"type": "object", "properties": {}}
        tools.append(
            McpTool(
                client,
                server,
                name,
                str(raw.get("description") or "")[:MAX_DESCRIPTION_CHARS],
                schema,
                _risk(settings, name, raw.get("annotations")),
                settings.timeout,
            )
        )
    return tools, skipped


def _problem(raw: Any, name: Any, seen: set[str]) -> str | None:
    if not isinstance(raw, dict):
        return "not an object"
    if not isinstance(name, str) or not TOOL_NAME.fullmatch(name):
        return "missing or unsupported name (letters, digits, '_' and '-' only)"
    if name in seen:
        return "duplicate name"
    schema = raw.get("inputSchema", {"type": "object"})
    if not isinstance(schema, dict) or schema.get("type", "object") != "object":
        return "inputSchema is not a JSON Schema object"
    if "properties" in schema and not isinstance(schema["properties"], dict):
        return "inputSchema.properties is not an object"
    if not isinstance(schema.get("required", []), list):
        return "inputSchema.required is not a list"
    return None


def _risk(settings: McpServerSettings, name: str, annotations: Any) -> RiskLevel:
    level = RiskLevel(settings.tool_risk.get(name, settings.risk))
    destructive = isinstance(annotations, dict) and annotations.get("destructiveHint") is True
    if destructive and level.rank < RiskLevel.EXECUTE.rank:
        level = RiskLevel.EXECUTE  # a server may raise its own risk, never lower it
    return level
