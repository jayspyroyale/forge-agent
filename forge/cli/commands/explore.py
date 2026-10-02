"""`forge explore TASK`: several real implementations, verified in isolation and compared.

`forge explorations list | show` inspects earlier runs.
"""

import asyncio
from typing import Annotated

import typer
from rich.markup import escape

from forge.cli.approval import make_cli_approver
from forge.cli.commands.run import verbosity_of
from forge.cli.explore_render import ExplorationRenderer, candidate_line, results_table
from forge.cli.output import console, fail, print_plain
from forge.cli.settings import load_cli_config
from forge.exploration.controller import MAX_APPROACHES, ExplorationController
from forge.exploration.isolation import ExplorationError
from forge.exploration.store import ExplorationStore, RunNotFoundError
from forge.models.errors import ModelError
from forge.security.permissions import PermissionEngine
from forge.workspace import Workspace

explorations_app = typer.Typer(help="Inspect earlier exploration runs.", no_args_is_help=True)


def explore(
    ctx: typer.Context,
    task: Annotated[str, typer.Argument(help="What you want done.")],
    approaches: Annotated[
        int | None, typer.Option("--approaches", "-n", help=f"How many candidates to implement (1-{MAX_APPROACHES}).")
    ] = None,
    from_head: Annotated[
        bool, typer.Option("--from-head", help="Start candidates from the last commit, without your uncommitted changes.")
    ] = False,
    keep: Annotated[bool, typer.Option("--keep", help="Keep candidate workspaces for inspection afterwards.")] = False,
    provider: Annotated[str | None, typer.Option("--provider", "-p", help="Provider name.")] = None,
    model: Annotated[str | None, typer.Option("--model", "-m", help="Model name.")] = None,
    max_steps: Annotated[int | None, typer.Option("--max-steps", help="Maximum model calls per candidate.")] = None,
    profile: Annotated[str | None, typer.Option("--profile", help="Configuration profile to use.")] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Approve actions that need approval (never overrides 'deny').")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show tool output and more detail.")] = False,
    debug: Annotated[bool, typer.Option("--debug", help="Show everything, including tracebacks.")] = False,
) -> None:
    """Implement a task several ways in isolated copies, verify each one, and compare them."""
    config = load_cli_config(
        ctx,
        **{
            "profile": profile,
            "model.provider": provider,
            "model.name": model,
            "agent.max_steps": max_steps,
            "exploration.approaches": approaches,
            "ui.verbose": True if verbose else None,
            "ui.debug": True if debug else None,
        },
    ).config
    verbosity = verbosity_of(config)
    renderer = ExplorationRenderer(console, verbosity)
    controller = ExplorationController(
        config,
        permissions=PermissionEngine(config.permissions, make_cli_approver(auto_approve=yes)),
        on_event=renderer,
        candidate_events=renderer.candidate_handler,
    )
    try:
        run = asyncio.run(controller.explore(task, include_uncommitted=not from_head, keep_workspaces=keep))
    except (ExplorationError, ModelError) as error:
        raise fail(str(error), debug=verbosity == "debug")
    except KeyboardInterrupt:
        console.print("\n[yellow]Exploration interrupted. Candidate workspaces were removed; records are kept.[/yellow]")
        raise typer.Exit(code=130)

    console.print()
    console.print(results_table(run))
    console.print(f"[dim]Details: forge explorations show {run.run_id}[/dim]")
    if not any(result.status == "completed" for result in run.candidates):
        raise typer.Exit(code=1)


@explorations_app.command("list")
def list_runs() -> None:
    """List exploration runs in this project, newest first."""
    store = ExplorationStore(Workspace("."))
    runs = store.list_runs()
    if not runs:
        console.print("[dim]No exploration runs yet. Try: forge explore \"your task\"[/dim]")
        return
    for summary in runs:
        selected = f" · selected {summary.selected}" if summary.selected else ""
        console.print(
            f"[bold]{summary.run_id}[/bold] [dim]{summary.created_at[:19]} · {summary.status} · "
            f"{summary.candidates} candidates{selected}[/dim]  {escape(summary.task[:80])}"
        )


@explorations_app.command("show")
def show_run(
    run_id: Annotated[str, typer.Argument(help="Run id (see `forge explorations list`), or 'latest'.")] = "latest",
    candidate: Annotated[str | None, typer.Option("--candidate", "-c", help="Show one candidate in detail.")] = None,
    patch: Annotated[bool, typer.Option("--patch", help="With --candidate: print its diff.")] = False,
) -> None:
    """Show an exploration run: plans, candidates, and their measured evidence."""
    store = ExplorationStore(Workspace("."))
    try:
        run = store.latest() if run_id == "latest" else store.load(run_id)
        if candidate is not None:
            result = run.candidate(candidate)
    except (RunNotFoundError, KeyError) as error:
        raise fail(str(error).strip("'\""))
    if candidate is None:
        console.print(f"[bold]{escape(run.task)}[/bold] [dim](run {run.run_id}, {run.status})[/dim]")
        for note in run.notes:
            console.print(f"[dim]planner: {escape(note)}[/dim]")
        console.print(results_table(run))
        return

    console.print(f"[bold]Candidate {result.candidate_id}: {escape(result.plan.title)}[/bold]")
    console.print(f"[dim]{escape(result.plan.summary)}[/dim]")
    console.print(candidate_line(result))
    for delta in result.files:
        stats = "binary" if delta.binary else f"+{delta.additions} -{delta.deletions}"
        console.print(f"  {escape(delta.path)} [dim]{delta.kind} {stats}[/dim]")
    for error in result.errors:
        console.print(f"  [red]{escape(error)}[/red]")
    if patch:
        console.print()
        print_plain(store.patch(run.run_id, result.candidate_id) or "(no changes)")
