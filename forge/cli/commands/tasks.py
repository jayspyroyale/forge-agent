"""`forge tasks list | show | undo`: review recorded tasks and revert them safely."""

from pathlib import Path
from typing import Annotated

import typer
from rich.markup import escape
from rich.table import Table

from forge.cli.output import console, fail
from forge.cli.render import render_report
from forge.git.repo import GitRepository
from forge.tasks.store import TaskNotFoundError, TaskStore
from forge.tasks.undo import apply_undo, plan_undo
from forge.workspace import Workspace

tasks_app = typer.Typer(help="Review and undo tasks Forge has run in this workspace.", no_args_is_help=True)


@tasks_app.command("list")
def list_tasks() -> None:
    """List tasks recorded in this workspace, newest first."""
    summaries = TaskStore(Workspace(Path.cwd())).list_tasks()
    if not summaries:
        console.print("No recorded tasks in this workspace.")
        return
    table = Table(title="Tasks")
    for column in ("ID", "Started (UTC)", "Status", "Files", "Task"):
        table.add_column(column)
    for summary in summaries:
        table.add_row(
            summary.task_id, summary.started_at[:19].replace("T", " "), summary.status, str(summary.files_changed), summary.task[:60]
        )
    console.print(table)


@tasks_app.command("show")
def show_task(task_id: Annotated[str, typer.Argument(help="Task ID (see `forge tasks list`).")]) -> None:
    """Show a recorded task's proof of work."""
    store = TaskStore(Workspace(Path.cwd()))
    try:
        evidence = store.load_evidence(task_id)
    except TaskNotFoundError as error:
        raise fail(str(error))
    console.print(f"[bold cyan]Task:[/bold cyan] {escape(evidence.task)}")
    console.print(f"[dim]started {evidence.started_at:%Y-%m-%d %H:%M:%S} UTC · {evidence.provider or '?'} · {evidence.model or '?'}[/dim]")
    for usage in evidence.tool_usage:
        mark = "✓" if usage.success else ("⊘" if usage.denied else "✗")
        console.print(f"  [dim]step {usage.step}[/dim] {mark} {escape(usage.name)} [dim]({usage.risk or '?'})[/dim]")
    render_report(console, evidence)


@tasks_app.command("undo")
def undo_task(
    task_id: Annotated[str, typer.Argument(help="Task ID (see `forge tasks list`).")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Apply the undo. Without this, only show the plan.")] = False,
) -> None:
    """Revert a task's file changes where that is provably safe. Never changes Git state."""
    workspace = Workspace(Path.cwd())
    store = TaskStore(workspace)
    repo = GitRepository.discover(workspace.root)
    try:
        plan = plan_undo(store, task_id, repo)
    except TaskNotFoundError as error:
        raise fail(str(error))

    if not plan.actions:
        console.print("This task changed no files.")
        return
    for action in plan.actions:
        color = "yellow" if action.action == "skip" else "cyan"
        console.print(f"  [{color}]{action.action:7}[/{color}] {escape(action.path)} [dim]({escape(action.reason)})[/dim]")
    if not yes:
        console.print("\n[dim]Dry run. Re-run with --yes to apply. Current contents are backed up first.[/dim]")
        return
    performed = apply_undo(plan, store, repo)
    console.print(f"\n[green]Reverted {len(performed)} file(s).[/green]")
    if plan.skipped:
        console.print(f"[yellow]Skipped {len(plan.skipped)} file(s); see reasons above.[/yellow]")
