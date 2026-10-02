"""`forge tools list | describe | run`: use tools directly, without a model."""

import asyncio
import json
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import ValidationError
from rich.markup import escape
from rich.table import Table

from forge.cli.approval import make_cli_approver
from forge.cli.output import console, fail, print_plain
from forge.config import ForgeConfig
from forge.models.types import ToolCall
from forge.security.permissions import PermissionEngine
from forge.tools.base import ToolContext
from forge.tools.builtin import create_default_tools
from forge.tools.executor import ToolExecutor
from forge.tools.registry import ToolNotFoundError
from forge.workspace import Workspace, WorkspaceError

tools_app = typer.Typer(help="Inspect and run Forge tools directly, without a model.", no_args_is_help=True)


@tools_app.command("list")
def list_tools() -> None:
    """List the built-in tools."""
    table = Table(title="Tools")
    table.add_column("Name", style="bold")
    table.add_column("Risk")
    table.add_column("Description")
    for tool in create_default_tools().list_tools():
        table.add_row(tool.name, tool.risk.value, tool.description)
    console.print(table)


@tools_app.command("describe")
def describe_tool(name: Annotated[str, typer.Argument(help="Tool name.")]) -> None:
    """Show a tool's description and argument schema."""
    try:
        tool = create_default_tools().get(name)
    except ToolNotFoundError as error:
        raise fail(str(error))
    definition = tool.definition()
    console.print(f"[bold]{escape(definition.name)}[/bold] [dim]({tool.risk.value})[/dim]")
    console.print(escape(definition.description))
    console.print_json(json.dumps(definition.parameters))


@tools_app.command("run")
def run_tool(
    name: Annotated[str, typer.Argument(help="Tool name.")],
    arguments: Annotated[
        list[str] | None, typer.Argument(help="Arguments as key=value pairs, e.g. path=README.md.")
    ] = None,
    json_arguments: Annotated[str | None, typer.Option("--json", help="Arguments as a JSON object.")] = None,
    workspace: Annotated[
        Path | None, typer.Option("--workspace", "-w", help="Workspace root (default: current directory).")
    ] = None,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Approve actions that need approval (never overrides 'deny').")
    ] = False,
) -> None:
    """Run one tool directly and print its result. Permission rules still apply."""
    try:
        parsed = parse_tool_arguments(arguments or [], json_arguments)
        config = ForgeConfig.from_env()
        context = ToolContext(workspace=Workspace(workspace or Path.cwd()), config=config)
    except (ValueError, ValidationError, WorkspaceError) as error:
        raise fail(str(error), code=2)

    permissions = PermissionEngine(config.permissions, make_cli_approver(auto_approve=yes))
    executor = ToolExecutor(create_default_tools(), context, permissions)
    result = asyncio.run(executor.execute(ToolCall(id="cli", name=name, arguments=parsed)))

    if result.output:
        print_plain(result.output)
    if not result.success:
        raise fail(result.error or "unknown error")


def parse_tool_arguments(pairs: list[str], json_arguments: str | None) -> dict[str, Any]:
    """Merge --json and key=value arguments. Values that parse as JSON (numbers, true) are converted."""
    parsed: dict[str, Any] = {}
    if json_arguments:
        try:
            loaded = json.loads(json_arguments)
        except json.JSONDecodeError as error:
            raise ValueError(f"--json is not valid JSON: {error}") from None
        if not isinstance(loaded, dict):
            raise ValueError("--json must be a JSON object")
        parsed.update(loaded)
    for pair in pairs:
        key, separator, raw_value = pair.partition("=")
        if not separator or not key:
            raise ValueError(f"Expected key=value, got '{pair}'")
        try:
            parsed[key] = json.loads(raw_value)
        except json.JSONDecodeError:
            parsed[key] = raw_value
    return parsed
