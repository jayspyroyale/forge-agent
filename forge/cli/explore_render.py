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
        self.console.print(f"[dim]Run {event.run_id} · up to {event.approaches} approach{'es' if event.approaches != 1 else ''} · starting from {start}[/dim]")
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


def show_candidates(console: Console, run: ExplorationRun) -> None:
    """One card per candidate. Without a comparison (an interrupted run), the raw results."""
    if run.comparison is None:
        for result in run.candidates:
            console.print(f"  {candidate_line(result)}")
        return
    titles = {result.candidate_id: result.plan.title for result in run.candidates}
    for item in run.comparison.measured:
        score = run.comparison.score(item.candidate_id)
        color, mark = _STATUS.get(item.status, ("red", "?"))
        verdict = f"score {score.total:.2f}" if score.eligible else "[red]ineligible[/red]"
        recommended = " [bold green]← recommended[/bold green]" if item.candidate_id == run.comparison.recommended else ""
        console.print(
            f"[bold]Candidate {item.candidate_id}[/bold] {escape(titles.get(item.candidate_id, ''))} "
            f"[{color}]{mark} {item.status.replace('_', ' ')}[/{color}] · {verdict}{recommended}"
        )
        checks = "   ".join(
            f"{label}: {_CHECK.get(item.checks.get(kind, ''), '-')}"
            for kind, label in (("test", "Tests"), ("build", "Build"), ("lint", "Lint"), ("typecheck", "Types"))
        )
        console.print(f"  {checks}")
        risk = "low" if not (item.denials or item.dangerous_attempts) else f"{item.dangerous_attempts} dangerous, {item.denials} denied"
        deps = escape(", ".join(item.new_dependencies)) if item.new_dependencies else "none"
        console.print(f"  Files: {item.files_changed} (+{item.additions} -{item.deletions})   New deps: {deps}   Risk: {risk}")
        tokens = f"{item.tokens:,}" if item.tokens is not None else "-"
        cost = f"${item.cost_usd:.4f}" if item.cost_usd is not None else "unknown"
        retries = f"   Retries: {item.retries}" if item.retries else ""
        console.print(f"  Tokens: {tokens}   Cost: {cost}   Time: {item.duration_seconds:.1f}s   Tool calls: {item.tool_calls}{retries}")
        if not score.eligible:
            console.print(f"  [red]{item.candidate_id} ineligible: {escape('; '.join(score.violations))}[/red]")
        console.print()


def show_comparison(console: Console, run: ExplorationRun) -> None:
    """MEASURED evidence, then MODEL-ASSESSMENT (if any), then the policy and the recommendation."""
    comparison = run.comparison
    console.rule("[bold]MEASURED[/bold] [dim](from Forge's own records)[/dim]", align="left")
    show_candidates(console, run)
    if comparison is None:
        return

    if comparison.assessments:
        reviewer = comparison.assessments[0].reviewer
        console.rule(f"[bold]MODEL-ASSESSMENT[/bold] [dim]by {escape(reviewer)}: an opinion, not a measurement (1-5)[/dim]", align="left")
        for assessment in comparison.assessments:
            scores = ", ".join(f"{key.replace('_', ' ')} {value}" for key, value in assessment.scores.items())
            console.print(f"[bold]{assessment.candidate_id}[/bold] {escape(scores)}")
            if assessment.rationale:
                console.print(f"  [dim]{escape(assessment.rationale)}[/dim]")
        console.print()

    weights = ", ".join(f"{factor} {round(100 * weight)}%" for factor, weight in comparison.weights.items() if weight > 0)
    console.print(f"[dim]Priorities: {weights}[/dim]")
    for note in comparison.notes:
        console.print(f"[dim]note: {escape(note)}[/dim]")
    titles = {result.candidate_id: result.plan.title for result in run.candidates}
    if comparison.recommended:
        console.print(f"\n[bold green]Recommended: Candidate {comparison.recommended}[/bold green] · {escape(titles.get(comparison.recommended, ''))}")
        explain_choice(console, comparison.reasons, comparison.tradeoffs)
    else:
        console.print("\n[bold yellow]No candidate meets the selection constraints.[/bold yellow]")


def explain_choice(console: Console, reasons: list[str], tradeoffs: list[str]) -> None:
    if reasons:
        console.print("Reasons:")
        for reason in reasons:
            console.print(f"  [green]+[/green] {escape(reason)}")
    if tradeoffs:
        console.print("Tradeoffs:")
        for tradeoff in tradeoffs:
            console.print(f"  [yellow]-[/yellow] {escape(tradeoff)}")
