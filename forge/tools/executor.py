"""The single place where tool calls requested by a model are turned into actions.

    ToolCall -> look up tool -> validate arguments -> execute -> ToolResult

`execute` never raises: every problem becomes a failed `ToolResult`, which the
agent passes back to the model as information it can react to.
"""

import asyncio

from pydantic import ValidationError

from forge.models.types import ToolCall
from forge.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult
from forge.tools.registry import ToolNotFoundError, ToolRegistry
from forge.workspace import WorkspaceError


class ToolExecutor:
    def __init__(self, registry: ToolRegistry, context: ToolContext) -> None:
        self.registry = registry
        self.context = context

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

        # Tools are ordinary blocking functions; run them in a worker thread so
        # the async agent loop is never blocked by slow file or process work.
        return await asyncio.to_thread(self._run, tool, args)

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
