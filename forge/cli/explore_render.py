"""How an exploration looks in the terminal: plans, each candidate's progress, and the results.

Like `render.py`, nothing here computes evidence; it displays Forge's records.
"""

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from forge.agent.events import TaskStarted
from forge.cli.output import Verbosity, plural
from forge.cli.render import TaskRenderer
from forge.exploration.events import (
    CandidateFinished,
    CandidateStarted,
    ExplorationDecision,
    ExplorationEvent,
    ExplorationStarted,
    PlansReady,
)
from forge.exploration.results import CandidateResult, ExplorationRun

_STATUS = {
    "completed": ("green", "✓"),
    "verification_failed": ("red", "✗"),
    "max_steps": ("yellow", "■"),
    "failed": ("red", "✗"),
    "budget_exhausted": ("yellow", "■"),
    "crashed": ("red", "✗"),
}
_CHECK = {"verified": "[green]PASS[/green]", "failed": "[red]FAIL[/red]", "unverified": "[yellow]-[/yellow]"}


class CandidateRenderer(TaskRenderer):
    """A candidate's agent events. The (long) candidate brief is not repeated."""

    def _on_TaskStarted(self, event: TaskStarted) -> None:
        pass


class ExplorationRenderer:
    def __init__(self, console: Console, verbosity: Verbosity = "normal") -> None:
        self.console = console
        self.verbosity = verbosity

    def candidate_handler(self, candidate_id: str) -> CandidateRenderer:
        return CandidateRenderer(self.console, self.verbosity)

    def __call__(self, event: ExplorationEvent) -> None:
        handler = getattr(self, f"_on_{type(event).__name__}", None)
        if handler is not None:
            handler(event)

    def _on_ExplorationStarted(self, event: ExplorationStarted) -> None:
        self.console.print(f"[bold cyan]Exploring:[/bold cyan] {escape(event.task)}")
        if event.baseline_kind == "git":
            start = "Git HEAD"
            if event.uncommitted:
                start += f" + your {plural(event.uncommitted, 'uncommitted file')} (copied; your files are never modified)"
        else:
            start = "a copy of the project (not a Git repository)"
        self.console.print(f"[dim]Run {event.run_id} · up to {plural(event.approaches, 'approach', )} · starting from {start}[/dim]")
        if event.left_out:
            self.console.print(f"[yellow]! {plural(event.left_out, 'uncommitted file')} left out (--from-head): candidates do not see them[/yellow]")

    def _on_PlansReady(self, event: PlansReady) -> None:
        table = Table(title="Candidate plans" if event.round == 1 else f"More candidate plans (round {event.round})")
        for column in ("ID", "Approach", "Complexity", "Expected files", "New deps"):
            table.add_column(column, overflow="fold")
        for plan in event.plans:
            model = f"\n[dim]model: {escape(plan.model.label())}[/dim]" if plan.model else ""
            lineage = f"\n[dim]derived from {', '.join(plan.parents)}[/dim]" if plan.parents else ""
            table.add_row(
                plan.id,
                f"[bold]{escape(plan.title)}[/bold]\n[dim]{escape(plan.summary)}[/dim]{model}{lineage}",
                plan.complexity,
                escape(", ".join(plan.files[:4]) or "-"),
                escape(", ".join(plan.dependencies) or "-"),
            )
        self.console.print(table)
        for note in event.notes:
            self.console.print(f"[dim]planner: {escape(note)}[/dim]")

    def _on_CandidateStarted(self, event: CandidateStarted) -> None:
        self.console.print()
        self.console.rule(f"[bold]Candidate {event.candidate_id}[/bold] · {escape(event.plan.title)}", align="left")
        if self.verbosity != "normal":
            self.console.print(f"[dim]workspace: {escape(event.workspace)}[/dim]")

    def _on_CandidateFinished(self, event: CandidateFinished) -> None:
        self.console.print(f"  {candidate_line(event.result)}")

    def _on_ExplorationDecision(self, event: ExplorationDecision) -> None:
        style = "cyan" if event.action == "continue" else "dim"
        self.console.print(f"\n[{style}]» {escape(event.reason)}[/{style}]")


def candidate_line(result: CandidateResult) -> str:
    color, mark = _STATUS.get(result.status, ("red", "?"))
    parts = [f"[{color}]{mark} {result.candidate_id}: {result.status.replace('_', ' ')}[/{color}]"]
    test = result.checks.get("test")
    if test:
        parts.append(f"tests {_CHECK[test]}")
    parts.append(f"{plural(result.files_changed, 'file')} +{result.additions} -{result.deletions}")
    if result.total_tokens is not None:
        parts.append(f"{result.total_tokens:,} tokens")
    if result.cost_usd is not None:
        parts.append(f"${result.cost_usd:.4f}")
    parts.append(f"{result.duration_seconds:.1f}s")
    if result.errors and result.status in ("crashed", "failed"):
        parts.append(f"[dim]{escape(result.errors[0][:120])}[/dim]")
    return " · ".join(parts)


def results_table(run: ExplorationRun) -> Table:
    table = Table(title=f"Candidates (run {run.run_id})")
    for column in ("ID", "Approach", "Status", "Tests", "Build", "Lint", "Types", "Files", "Diff", "New deps", "Tokens", "Cost", "Time"):
        table.add_column(column, overflow="fold")
    for result in run.candidates:
        color, _ = _STATUS.get(result.status, ("red", "?"))
        checks = result.checks
        table.add_row(
            result.candidate_id,
            escape(result.plan.title),
            f"[{color}]{result.status.replace('_', ' ')}[/{color}]",
            *(_CHECK.get(checks.get(kind, ""), "-") for kind in ("test", "build", "lint", "typecheck")),
            str(result.files_changed),
            f"+{result.additions} -{result.deletions}",
            escape(", ".join(result.dependencies_added) or "-"),
            f"{result.total_tokens:,}" if result.total_tokens is not None else "-",
            f"${result.cost_usd:.4f}" if result.cost_usd is not None else "-",
            f"{result.duration_seconds:.1f}s",
        )
    return table
