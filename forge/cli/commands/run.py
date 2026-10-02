"""`forge run TASK` (also `forge "TASK"`): run the agent once and show its proof of work."""

import asyncio
from typing import Annotated

import typer
from pydantic import ValidationError

from forge.agent.runtime import run_task
from forge.agent.state import AgentStatus
from forge.cli.approval import make_cli_approver
from forge.cli.output import Verbosity, console, fail
from forge.cli.render import TaskRenderer, render_report
from forge.config import ForgeConfig
from forge.models.errors import ModelError
from forge.security.permissions import PermissionEngine
from forge.workspace import WorkspaceError


def execute_task(
    config: ForgeConfig,
    task: str,
    *,
    permissions: PermissionEngine,
    verbosity: Verbosity = "normal",
) -> bool:
    """Run one task with live output and a completion report. Returns True if it completed.

    Shared by `forge run` and the interactive session. Errors are printed,
    never raised, so an interactive session can continue after a failure.
    """
    renderer = TaskRenderer(console, verbosity)
    try:
        outcome = asyncio.run(run_task(config, task, permissions=permissions, on_event=renderer))
    except (ModelError, WorkspaceError) as error:
        fail(str(error), debug=verbosity == "debug")
        return False
    render_report(console, outcome.evidence)
    return outcome.state.status == AgentStatus.COMPLETED


def verbosity_from(verbose: bool, debug: bool) -> Verbosity:
    if debug:
        return "debug"
    return "verbose" if verbose else "normal"


def run(
    task: Annotated[str, typer.Argument(help="What you want the agent to do.")],
    provider: Annotated[
        str | None, typer.Option("--provider", "-p", help="Provider name (see `forge models list`).")
    ] = None,
    model: Annotated[str | None, typer.Option("--model", "-m", help="Model name.")] = None,
    max_steps: Annotated[int | None, typer.Option("--max-steps", help="Maximum number of model calls.")] = None,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Approve actions that need approval (never overrides 'deny').")
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show tool output and more detail.")] = False,
    debug: Annotated[bool | None, typer.Option("--debug", help="Show everything, including tracebacks.")] = None,
) -> None:
    """Run the agent on a task in the current directory."""
    try:
        config = ForgeConfig.from_env(provider=provider, model=model, max_steps=max_steps, debug=debug)
    except ValidationError as error:
        raise fail(str(error), prefix="Invalid configuration")

    permissions = PermissionEngine(config.permissions, make_cli_approver(auto_approve=yes))
    completed = execute_task(config, task, permissions=permissions, verbosity=verbosity_from(verbose, config.debug))
    if not completed:
        raise typer.Exit(code=1)
