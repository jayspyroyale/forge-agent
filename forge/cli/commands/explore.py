"""`forge explore TASK`: several real implementations, verified in isolation, compared, selected, applied.

`forge explorations list | show | compare | select | apply` works with earlier runs.
"""

import asyncio
from typing import Annotated

import typer
from rich.markup import escape
from rich.prompt import Prompt

from forge.cli.approval import make_cli_approver
from forge.cli.commands.run import verbosity_of
from forge.cli.explore_render import ExplorationRenderer, candidate_line, explain_choice, show_comparison
from forge.cli.output import console, fail, print_plain
from forge.cli.settings import load_cli_config
from forge.config import ForgeConfig
from forge.exploration.apply import ApplyError, apply_candidate, preview_apply
from forge.exploration.compare import Comparison
from forge.exploration.controller import MAX_APPROACHES, ExplorationController
from forge.exploration.isolation import ExplorationError
from forge.exploration.results import ExplorationRun
from forge.exploration.selection import SelectionError, select
from forge.exploration.store import ExplorationStore, RunNotFoundError
from forge.models.errors import ModelError
from forge.security.permissions import PermissionEngine
from forge.workspace import Workspace

explorations_app = typer.Typer(help="Inspect, compare, select, and apply earlier exploration runs.", no_args_is_help=True)

ModeOption = Annotated[
    str | None,
    typer.Option("--mode", help="Selection: manual (you choose), assisted (Forge recommends, you confirm), autonomous (Forge chooses)."),
]


def explore(
    ctx: typer.Context,
    task: Annotated[str, typer.Argument(help="What you want done.")],
    approaches: Annotated[
        int | None, typer.Option("--approaches", "-n", help=f"How many candidates to implement (1-{MAX_APPROACHES}).")
    ] = None,
    mode: ModeOption = None,
    apply: Annotated[
        bool | None,
        typer.Option("--apply/--no-apply", help="Apply the selected candidate without asking (or never apply). Default: ask."),
    ] = None,
    review: Annotated[
        bool | None, typer.Option("--review/--no-review", help="Ask a model to review the diffs (maintainability, scalability).")
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
    """Implement a task several ways in isolated copies, verify each one, compare them, and apply the best."""
    config = load_cli_config(
        ctx,
        **{
            "profile": profile,
            "model.provider": provider,
            "model.name": model,
            "agent.max_steps": max_steps,
            "exploration.approaches": approaches,
            "exploration.selection_mode": mode,
            "exploration.review": None if review is None else ("always" if review else "never"),
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
    show_comparison(console, run)
    selected = _select_and_apply(controller, run, config, apply)
    console.print(f"[dim]Details: forge explorations show {run.run_id}[/dim]")
    if not selected and not any(result.status == "completed" for result in run.candidates):
        raise typer.Exit(code=1)


def _select_and_apply(controller: ExplorationController, run: ExplorationRun, config: ForgeConfig, apply: bool | None) -> bool:
    assert run.comparison is not None
    mode = config.exploration.selection_mode
    selection = select(run.comparison, mode, chooser=_chooser(mode))
    controller.record_selection(run, selection)
    if selection.candidate_id is None:
        console.print("[dim]No candidate selected. Nothing was applied to your project.[/dim]")
        return False

    by = "Forge selected (autonomous policy)" if selection.decided_by == "policy" else "You selected"
    console.print(f"\n[bold]{by}: Candidate {selection.candidate_id}[/bold]")
    if selection.candidate_id != selection.recommended or selection.decided_by == "policy":
        explain_choice(console, selection.reasons, selection.tradeoffs)

    if apply is False or (apply is None and mode == "autonomous"):
        console.print(f"[dim]Not applied. To apply it: forge explorations apply {run.run_id}[/dim]")
        return True
    _apply(controller.store, run, selection.candidate_id, confirm=apply is None)
    return True


def _chooser(mode: str):
    if mode == "autonomous":
        return None

    def choose(comparison: Comparison) -> str | None:
        ids = [item.candidate_id for item in comparison.measured]
        try:
            if mode == "assisted" and comparison.recommended:
                answer = Prompt.ask(
                    f"Use candidate {comparison.recommended}? [bold]y[/bold]es, [bold]n[/bold]o, or another candidate id",
                    default="y",
                    console=console,
                ).strip()
                if answer.lower() in ("y", "yes"):
                    return comparison.recommended
                if answer.lower() in ("n", "no", "none", ""):
                    return None
                return answer
            answer = Prompt.ask(f"Choose a candidate ({', '.join(ids)}) or 'none'", default="none", console=console).strip()
            return None if answer.lower() in ("none", "n", "") else answer
        except (EOFError, KeyboardInterrupt):
            return None

    def safe(comparison: Comparison) -> str | None:
        while True:
            answer = choose(comparison)
            if answer is None or answer.upper() in {item.candidate_id for item in comparison.measured}:
                return answer
            console.print(f"[yellow]No candidate '{escape(answer)}'.[/yellow]")

    return safe


def _apply(store: ExplorationStore, run: ExplorationRun, candidate_id: str, confirm: bool) -> None:
    workspace = store.workspace
    preview = preview_apply(run, candidate_id, workspace)
    if not preview.ok:
        console.print(f"[red]Cannot apply candidate {candidate_id}:[/red]")
        for conflict in preview.conflicts:
            console.print(f"  [red]✗ {escape(conflict)}[/red]")
        console.print("[dim]Nothing was changed. Resolve the conflicts, or inspect the patch with forge explorations show.[/dim]")
        raise typer.Exit(code=1)
    console.print(f"Applying candidate {candidate_id} changes {len([i for i in preview.items if i.action != 'skip'])} file(s):")
    for item in preview.items:
        console.print(f"  {item.action:<6} {escape(item.path)} [dim]{escape(item.note)}[/dim]")
    if confirm:
        try:
            answer = Prompt.ask("Apply to your project now?", choices=["y", "n"], default="n", console=console)
        except (EOFError, KeyboardInterrupt):
            answer = "n"
        if answer != "y":
            console.print(f"[dim]Not applied. Later: forge explorations apply {run.run_id} {candidate_id}[/dim]")
            return
    try:
        record, _ = apply_candidate(run, candidate_id, workspace, store)
    except ApplyError as error:
        raise fail(str(error))
    console.print(f"[green]✓ Applied candidate {candidate_id}[/green] to {len(record.files)} file(s).")
    console.print(f"[dim]Proof of work: forge tasks show {record.task_id} · Undo: forge tasks undo {record.task_id}[/dim]")


# --- forge explorations ... ----------------------------------------------------------------------


def _store() -> ExplorationStore:
    return ExplorationStore(Workspace("."))


def _load(store: ExplorationStore, run_id: str) -> ExplorationRun:
    try:
        return store.latest() if run_id == "latest" else store.load(run_id)
    except RunNotFoundError as error:
        raise fail(str(error))


@explorations_app.command("list")
def list_runs() -> None:
    """List exploration runs in this project, newest first."""
    runs = _store().list_runs()
    if not runs:
        console.print('[dim]No exploration runs yet. Try: forge explore "your task"[/dim]')
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
    """Show an exploration run: plans, candidates, measured evidence, and the selection."""
    store = _store()
    run = _load(store, run_id)
    if candidate is None:
        console.print(f"[bold]{escape(run.task)}[/bold] [dim](run {run.run_id}, {run.status})[/dim]")
        for note in run.notes:
            console.print(f"[dim]note: {escape(note)}[/dim]")
        show_comparison(console, run)
        if run.selection is not None:
            chosen = run.selection.candidate_id or "nothing"
            console.print(f"\nSelected: [bold]{chosen}[/bold] [dim]({run.selection.mode}, by {run.selection.decided_by})[/dim]")
        if run.applied is not None:
            console.print(f"Applied: candidate {run.applied.candidate_id} · task {run.applied.task_id}")
        return
    try:
        result = run.candidate(candidate)
    except KeyError as error:
        raise fail(str(error).strip("'\""))
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


@explorations_app.command("compare")
def compare_run(
    ctx: typer.Context,
    run_id: Annotated[str, typer.Argument(help="Run id, or 'latest'.")] = "latest",
    review: Annotated[bool, typer.Option("--review", help="Also ask a model to review the diffs.")] = False,
    profile: Annotated[str | None, typer.Option("--profile", help="Compare with this profile's priorities.")] = None,
) -> None:
    """Compare a run's candidates again with the current priorities and constraints."""
    config = load_cli_config(ctx, profile=profile).config
    controller = ExplorationController(config)
    run = _load(controller.store, run_id)
    try:
        asyncio.run(controller.evaluate(run, review=review or None))
    except ModelError as error:
        raise fail(str(error))
    show_comparison(console, run)


@explorations_app.command("select")
def select_candidate(
    run_id: Annotated[str, typer.Argument(help="Run id, or 'latest'.")],
    candidate: Annotated[str, typer.Argument(help="Candidate id.")],
) -> None:
    """Record your choice of candidate for a run (does not apply it)."""
    store = _store()
    run = _load(store, run_id)
    if run.comparison is None:
        raise fail("This run has no comparison yet. Run: forge explorations compare " + run.run_id)
    try:
        run.selection = select(run.comparison, "manual", chooser=lambda _: candidate)
    except SelectionError as error:
        raise fail(str(error))
    store.save(run)
    console.print(f"Selected candidate {run.selection.candidate_id}.")
    explain_choice(console, run.selection.reasons, run.selection.tradeoffs)


@explorations_app.command("apply")
def apply_run(
    run_id: Annotated[str, typer.Argument(help="Run id, or 'latest'.")] = "latest",
    candidate: Annotated[str | None, typer.Argument(help="Candidate id (default: the selected one).")] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Apply without asking.")] = False,
) -> None:
    """Apply a candidate's changes to your project (only if the files it touches are unchanged since)."""
    store = _store()
    run = _load(store, run_id)
    chosen = candidate or (run.selection.candidate_id if run.selection else None)
    if chosen is None:
        raise fail("No candidate selected for this run. Pass one: forge explorations apply RUN CANDIDATE")
    try:
        run.candidate(chosen)
    except KeyError as error:
        raise fail(str(error).strip("'\""))
    _apply(store, run, chosen.upper(), confirm=not yes)
