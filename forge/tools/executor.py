"""The single place where tool calls requested by a model are turned into actions.

    ToolCall -> look up tool -> validate arguments -> assess risk
             -> permission engine (allow / ask / deny) -> execute -> ToolResult

A tool's `execute` is only reached after the permission engine allows the
call. `execute` here never raises: every problem (unknown tool, bad
arguments, denial, tool failure) becomes a failed `ToolResult`, which the
agent passes back to the model as information it can react to.
"""

import asyncio

from pydantic import ValidationError
from forge.concurrency import run_blocking

from forge.models.types import ToolCall
from forge.security.permissions import PermissionEngine, PermissionRequest
from forge.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult
from forge.tools.registry import ToolNotFoundError, ToolRegistry
from forge.workspace import WorkspaceError
from forge.security.secret_scan import redact_data


class ToolExecutor:
    def __init__(
        self,
        registry: ToolRegistry,
        context: ToolContext,
        permissions: PermissionEngine | None = None,
    ) -> None:
        self.registry = registry
        self.context = context
        # Without an explicit engine: the default policy and nobody to ask,
        # so reads are allowed and anything that needs approval is refused.
        self.permissions = permissions or PermissionEngine()

    async def execute(self, call: ToolCall) -> ToolResult:
        try:
            tool = self.registry.get(call.name)
        except ToolNotFoundError:
            available = ", ".join(self.registry.names()) or "none"
            return ToolResult.fail(f"Unknown tool '{call.name}'. Available tools: {available}.")

        try:
            args = tool.Args.model_validate(call.arguments)
        except ValidationError as error:
            return ToolResult.fail(f"Invalid arguments for '{tool.name}': {summarize_validation_error(error)}")

        try:
            risk = tool.assess_risk(args, self.context)
        except WorkspaceError as error:
            return ToolResult.fail(str(error))

        request = PermissionRequest(
            tool_name=tool.name,
            arguments=call.arguments,
            risk=risk,
            approval_key=tool.approval_key(args),
        )
        outcome = self.permissions.authorize(request)
        permission_info = {"risk": risk.level.value, "decided_by": outcome.decided_by}
        if not outcome.allowed:
            return ToolResult.fail(
                f"Permission denied: {outcome.reason}. The action was not performed.",
                denied=True,
                permission=permission_info,
            )

        # Tools are ordinary blocking functions; run them in a worker thread so
        # the async agent loop is never blocked by slow file or process work.
        result = await run_blocking(self._run, tool, args, on_cancel=self.context.cancelled.set)
        result.metadata["permission"] = permission_info
        return ToolResult.model_validate(redact_data(result.model_dump()))

    def _run(self, tool: Tool, args: ToolArgs) -> ToolResult:
        try:
            return tool.execute(args, self.context)
        except (ToolError, WorkspaceError) as error:
            return ToolResult.fail(str(error))
        except Exception as error:  # a bug in a tool must not crash the agent
            return ToolResult.fail(f"Tool '{tool.name}' failed unexpectedly: {type(error).__name__}: {error}")


def summarize_validation_error(error: ValidationError) -> str:
    parts = []
    for item in error.errors():
        location = ".".join(str(part) for part in item["loc"]) or "arguments"
        parts.append(f"{location}: {item['msg']}")
    return "; ".join(parts)
