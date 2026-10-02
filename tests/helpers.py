"""Small helpers shared by tests (imported explicitly, unlike conftest fixtures)."""

import asyncio
from pathlib import Path
from typing import Any

from forge.models.types import ToolCall
from forge.security.permissions import PermissionEngine
from forge.security.policy import PermissionPolicy
from forge.tools.base import ToolContext, ToolResult
from forge.tools.builtin import create_default_tools
from forge.tools.executor import ToolExecutor
from forge.workspace import Workspace


def run_tool(root: Path, name: str, **arguments: Any) -> ToolResult:
    """Run a built-in tool through the real executor, the same path the agent uses.

    Permissions are fully open here: these helpers test what tools do, while
    tests/test_permissions.py tests who may run them.
    """
    permissions = PermissionEngine(PermissionPolicy.permissive())
    executor = ToolExecutor(create_default_tools(), ToolContext(workspace=Workspace(root)), permissions)
    return asyncio.run(executor.execute(ToolCall(id="test", name=name, arguments=arguments)))
