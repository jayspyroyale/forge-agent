"""How a running task and its completion report look in the terminal.

The agent emits events; `TaskRenderer` decides what to show for each one at
the chosen verbosity. `render_report` prints the proof of work. Nothing here
computes anything: every number shown comes from Forge's own records.
"""

from rich.console import Console
from rich.markup import escape

from forge.agent.events import (
    AgentEvent,
    ModelRequested,
    ModelResponded,
    TaskStarted,
    ToolFinished,
    ToolStarted,
    VerificationFinished,
    VerificationStarted,
)
from forge.agent.state import AgentStatus
from forge.cli.output import Verbosity, plural, short_arguments
from forge.evidence import TaskEvidence

PREVIEW_LINES = 15
_OUTCOME_STYLE = {"verified": ("green", "✓"), "failed": ("red", "✗"), "unverified": ("yellow", "-")}


class TaskRenderer:
    def __init__(self, console: Console, verbosity: Verbosity = "normal") -> None:
        self.console = console
        self.verbosity = verbosity

    @property
    def verbose(self) -> bool:
        return self.verbosity in ("verbose", "debug")

    def __call__(self, event: AgentEvent) -> None:
        handler = getattr(self, f"_on_{type(event).__name__}", None)
        if handler is not None:
            handler(event)

    def _on_TaskStarted(self, event: TaskStarted) -> None:
        self.console.print(f"[bold cyan]Task:[/bold cyan] {escape(event.task)}")

    def _on_ModelRequested(self, event: ModelRequested) -> None:
        if self.verbose:
            self.console.print(f"[dim]· step {event.step}: asking the model ({event.message_count} messages)[/dim]")

    def _on_ModelResponded(self, event: ModelResponded) -> None:
        response = event.response
        # Text that accompanies tool calls is the model explaining what it is doing.
        if response.tool_calls and response.content.strip():
            text = response.content.strip()
            if not self.verbose and len(text) > 200:
                text = text[:199] + "…"
            self.console.print(f"[dim italic]{escape(text)}[/dim italic]")
        if self.verbosity == "debug" and response.usage is not None:
            usage = response.usage
            self.console.print(
                f"[dim]  tokens: {usage.input_tokens} in, {usage.output_tokens} out · finish: {response.finish_reason}[/dim]"
            )

    def _on_ToolStarted(self, event: ToolStarted) -> None:
        self.console.print(
            f"[cyan]→ {escape(event.call.name)}[/cyan] [dim]{escape(short_arguments(event.call.arguments))}[/dim]"
        )

    def _on_ToolFinished(self, event: ToolFinished) -> None:
        result = event.result
        metadata = result.metadata
        if not result.success:
            if metadata.get("denied"):
                self.console.print(f"  [yellow]⊘ {escape(result.error or 'denied')}[/yellow]")
            else:
                self.console.print(f"  [red]✗ {escape(result.error or 'failed')}[/red]")
            if self.verbose and result.output:
                self._preview(result.output)
            return

        if metadata.get("changed_paths"):
            self.console.print(f"  [green]✎ {escape(result.output)}[/green]")
        elif "exit_code" in metadata:
            self.console.print(f"  [green]✓ exit 0[/green] [dim]({metadata.get('duration_seconds', 0):.1f}s)[/dim]")
        elif self.verbose:
            self.console.print("  [green]✓[/green]")
        if self.verbose:
            self._preview(result.output)
        if self.verbosity == "debug":
            self.console.print(f"[dim]  metadata: {escape(str(metadata))}[/dim]")

    def _on_VerificationStarted(self, event: VerificationStarted) -> None:
        command = f" [dim]{escape(event.command)}[/dim]" if self.verbose else ""
        self.console.print(f"[magenta]⧗ verifying {escape(event.name)}[/magenta]{command}")

    def _on_VerificationFinished(self, event: VerificationFinished) -> None:
        result = event.result
        if result.passed:
            self.console.print(f"  [green]✓ passed[/green] [dim]({result.duration_seconds:.1f}s)[/dim]")
        else:
            self.console.print(f"  [red]✗ {escape(result.status)}[/red] [dim]({result.duration_seconds:.1f}s)[/dim]")
            if self.verbose and result.summary:
                self._preview(result.summary, tail=True)

    def _preview(self, text: str, tail: bool = False) -> None:
        lines = text.rstrip("\n").splitlines()
        shown = lines[-PREVIEW_LINES:] if tail else lines[:PREVIEW_LINES]
        for line in shown:
            self.console.print(f"    [dim]{escape(line)}[/dim]", soft_wrap=True)
        hidden = len(lines) - len(shown)
        if hidden > 0:
            self.console.print(f"    [dim]… {hidden} more lines[/dim]")


def render_report(console: Console, evidence: TaskEvidence) -> None:
    """The completion report: the model's summary, then Forge's own evidence."""
    if evidence.final_answer:
        console.print()
        console.print(evidence.final_answer, markup=False, highlight=False, soft_wrap=True)

    console.print()
    console.rule(style="dim")
    _status_line(console, evidence)
    _changes(console, evidence)
    _verification(console, evidence)
    _totals(console, evidence)


def _status_line(console: Console, evidence: TaskEvidence) -> None:
    steps = plural(evidence.steps, "step")
    status = evidence.status
    if status == AgentStatus.COMPLETED:
        console.print(f"[bold green]✓ Completed in {steps}[/bold green]")
    elif status == AgentStatus.VERIFICATION_FAILED:
        console.print(f"[bold red]✗ Finished in {steps}, but verification failed[/bold red]")
    elif status == AgentStatus.MAX_STEPS:
        console.print(f"[bold yellow]■ Stopped after {steps}: reached the step limit (max_steps)[/bold yellow]")
    else:
        console.print(f"[bold red]✗ Failed after {steps}[/bold red]")
    if evidence.error and status != AgentStatus.COMPLETED:
        console.print(f"  [dim]{escape(evidence.error)}[/dim]")


def _changes(console: Console, evidence: TaskEvidence) -> None:
    changes = evidence.changes
    if changes is None or not changes.changes:
        console.print("\n[bold]Changed:[/bold] [dim]nothing[/dim]")
    else:
        console.print("\n[bold]Changed:[/bold]")
        for change in changes.changes:
            stats = "" if change.additions is None else f" [green]+{change.additions}[/green] [red]-{change.deletions}[/red]"
            note = " [dim](also had your earlier edits)[/dim]" if change.origin == "mixed" else ""
            kind = "" if change.kind == "modified" else f" [dim]{change.kind}[/dim]"
            console.print(f"  {escape(change.path)}{kind}{stats}{note}")
    if changes is not None and changes.pre_existing:
        console.print(f"  [dim]{plural(len(changes.pre_existing), 'file')} you had already changed: left untouched[/dim]")


def _verification(console: Console, evidence: TaskEvidence) -> None:
    console.print("\n[bold]Verification:[/bold]")
    for check in evidence.checks:
        color, mark = _OUTCOME_STYLE[check.outcome]
        console.print(f"  [{color}]{mark} {check.kind:<9}[/{color}] [dim]{escape(check.detail)}[/dim]")


def _totals(console: Console, evidence: TaskEvidence) -> None:
    parts = []
    if evidence.changes is not None and evidence.changes.changes:
        parts.append(f"diff +{evidence.changes.additions} -{evidence.changes.deletions}")
    parts.append(plural(evidence.steps, "step"))
    if evidence.usage is not None:
        parts.append(f"{evidence.usage.total_tokens} tokens")
    if evidence.duration_seconds is not None:
        parts.append(f"{evidence.duration_seconds:.1f}s")
    console.print()
    console.print("[dim]" + " · ".join(parts) + "[/dim]")
    console.print(f"[dim]Task {evidence.task_id} · forge tasks show {evidence.task_id}[/dim]")
