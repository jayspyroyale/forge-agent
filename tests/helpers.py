"""Small helpers shared by tests (imported explicitly, unlike conftest fixtures)."""

import asyncio
from pathlib import Path
from typing import Any

from forge.models.types import ToolCall
from forge.tools.base import ToolContext, ToolResult
from forge.tools.builtin import create_default_tools
from forge.tools.executor import ToolExecutor
from forge.workspace import Workspace


def run_tool(root: Path, name: str, **arguments: Any) -> ToolResult:
    """Run a built-in tool through the real executor, the same path the agent uses."""
    executor = ToolExecutor(create_default_tools(), ToolContext(workspace=Workspace(root)))
    return asyncio.run(executor.execute(ToolCall(id="test", name=name, arguments=arguments)))
