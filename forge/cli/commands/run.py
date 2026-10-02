"""`forge run TASK` (also `forge "TASK"`): run the agent once and show its proof of work."""

import asyncio
from typing import Annotated

import typer

from forge.agent.runtime import run_task
from forge.agent.state import AgentStatus
from forge.cli.approval import make_cli_approver
from forge.cli.output import Verbosity, console, fail
from forge.cli.render import TaskRenderer, render_report
from forge.cli.settings import load_cli_config
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


def verbosity_of(config: ForgeConfig) -> Verbosity:
    if config.ui.debug:
        return "debug"
    return "verbose" if config.ui.verbose else "normal"


def run(
    ctx: typer.Context,
    task: Annotated[str, typer.Argument(help="What you want the agent to do.")],
    provider: Annotated[
        str | None, typer.Option("--provider", "-p", help="Provider name (see `forge models list`).")
    ] = None,
    model: Annotated[str | None, typer.Option("--model", "-m", help="Model name.")] = None,
    max_steps: Annotated[int | None, typer.Option("--max-steps", help="Maximum number of model calls.")] = None,
    profile: Annotated[str | None, typer.Option("--profile", help="Configuration profile to use.")] = None,
    no_verify: Annotated[bool, typer.Option("--no-verify", help="Don't run the project's checks after changes.")] = False,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Approve actions that need approval (never overrides 'deny').")
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show tool output and more detail.")] = False,
    debug: Annotated[bool, typer.Option("--debug", help="Show everything, including tracebacks.")] = False,
) -> None:
    """Run the agent on a task in the current directory."""
    loaded = load_cli_config(
        ctx,
        **{
            "profile": profile,
            "model.provider": provider,
            "model.name": model,
            "agent.max_steps": max_steps,
            "agent.verification": "off" if no_verify else None,
            "ui.verbose": True if verbose else None,
            "ui.debug": True if debug else None,
        },
    )
    config = loaded.config
    permissions = PermissionEngine(config.permissions, make_cli_approver(auto_approve=yes))
    if not execute_task(config, task, permissions=permissions, verbosity=verbosity_of(config)):
        raise typer.Exit(code=1)
