"""Proof of work: what actually happened during a task.

`TaskEvidence` is built only from Forge's own records (the agent state's tool
history and verification rounds, which the runtime fills in), never from the
model's text. A model can *say* "all tests pass"; the evidence shows whether
Forge ran the tests and what they returned.

The report sorts each kind of check into:

    VERIFIED    Forge ran it after the last change, and it passed
    FAILED      Forge ran it, and it failed (or timed out, or could not start)
    UNVERIFIED  not configured, not run, or not allowed to run
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from forge.agent.state import AgentState
from forge.models.types import Usage
from forge.tasks.snapshot import ChangeReport
from forge.verification.checks import CHECK_KINDS, CheckKind, VerificationResult

Outcome = Literal["verified", "failed", "unverified"]


class ToolUsage(BaseModel):
    step: int
    name: str
    success: bool
    risk: str | None = None
    decided_by: str | None = None
    denied: bool = False
    changed_paths: list[str] = Field(default_factory=list)


class CommandRecord(BaseModel):
    """A command the agent ran itself through run_command (not a Forge verification)."""

    step: int
    command: str
    exit_code: int | None
    timed_out: bool


class CheckReport(BaseModel):
    kind: CheckKind
    outcome: Outcome
    detail: str
    results: list[VerificationResult] = Field(default_factory=list)


class TaskEvidence(BaseModel):
    task_id: str
    task: str
    status: str
    steps: int
    started_at: datetime
    finished_at: datetime | None
    duration_seconds: float | None
    provider: str | None = None
    model: str | None = None
    tool_usage: list[ToolUsage]
    commands_run: list[CommandRecord]
    files_changed: list[str]
    verification_rounds: list[list[VerificationResult]]
    checks: list[CheckReport]
    usage: Usage | None
    final_answer: str | None
    error: str | None
    # What the task changed in the workspace, compared with a snapshot taken at
    # task start (task vs pre-existing changes, line counts, Git HEAD).
    changes: ChangeReport | None = None
    # Room for later layers (for example cost estimates) without schema changes.
    extra: dict[str, Any] = Field(default_factory=dict)

    def by_outcome(self, outcome: Outcome) -> list[CheckReport]:
        return [check for check in self.checks if check.outcome == outcome]

    @property
    def verified(self) -> bool:
        """At least one check passed after the last change, and nothing failed."""
        return bool(self.by_outcome("verified")) and not self.by_outcome("failed")


def build_evidence(
    state: AgentState,
    configured_kinds: set[CheckKind],
    provider: str | None = None,
    model: str | None = None,
    changes: ChangeReport | None = None,
) -> TaskEvidence:
    duration = None
    if state.finished_at is not None:
        duration = round((state.finished_at - state.started_at).total_seconds(), 3)

    return TaskEvidence(
        task_id=state.task_id,
        task=state.task,
        status=state.status,
        steps=state.step,
        started_at=state.started_at,
        finished_at=state.finished_at,
        duration_seconds=duration,
        provider=provider,
        model=model,
        tool_usage=[_tool_usage(execution) for execution in state.tool_history],
        commands_run=_commands(state),
        # Prefer what the workspace comparison saw (includes changes made by commands).
        files_changed=changes.changed_paths if changes is not None else state.changed_paths(),
        verification_rounds=[round_.results for round_ in state.verification_rounds],
        checks=_check_reports(state, configured_kinds),
        usage=state.usage,
        final_answer=state.final_answer,
        error=state.error,
        changes=changes,
    )


def _tool_usage(execution) -> ToolUsage:
    metadata = execution.result.metadata
    permission = metadata.get("permission", {})
    return ToolUsage(
        step=execution.step,
        name=execution.call.name,
        success=execution.result.success,
        risk=permission.get("risk"),
        decided_by=permission.get("decided_by"),
        denied=bool(metadata.get("denied")),
        changed_paths=list(metadata.get("changed_paths", [])) if execution.result.success else [],
    )


def _commands(state: AgentState) -> list[CommandRecord]:
    records = []
    for execution in state.tool_history:
        metadata = execution.result.metadata
        if execution.call.name == "run_command" and "exit_code" in metadata:
            records.append(
                CommandRecord(
                    step=execution.step,
                    command=metadata.get("command", ""),
                    exit_code=metadata.get("exit_code"),
                    timed_out=bool(metadata.get("timed_out")),
                )
            )
    return records


def _check_reports(state: AgentState, configured_kinds: set[CheckKind]) -> list[CheckReport]:
    last_round = state.verification_rounds[-1].results if state.verification_rounds else []
    changed_since_last_round = bool(state.verification_rounds) and (
        _last_change_step(state) > state.verification_rounds[-1].step
    )
    reports = []
    for kind in CHECK_KINDS:
        results = [result for result in last_round if result.kind == kind]
        reports.append(_report_for(kind, results, kind in configured_kinds, changed_since_last_round))
    return reports


def _report_for(
    kind: CheckKind, results: list[VerificationResult], configured: bool, stale: bool
) -> CheckReport:
    if not configured:
        return CheckReport(kind=kind, outcome="unverified", detail="not configured")
    if not results:
        return CheckReport(kind=kind, outcome="unverified", detail="not run")
    names = ", ".join(result.name for result in results)
    failing = [result for result in results if result.failing]
    if failing:
        statuses = ", ".join(f"{result.name} {result.status}" for result in failing)
        return CheckReport(kind=kind, outcome="failed", detail=statuses, results=results)
    if any(result.status == "skipped" for result in results):
        return CheckReport(kind=kind, outcome="unverified", detail=f"{names} skipped (not allowed to run)", results=results)
    if stale:
        return CheckReport(kind=kind, outcome="unverified", detail=f"{names} passed, but files changed afterwards", results=results)
    return CheckReport(kind=kind, outcome="verified", detail=f"{names} passed", results=results)


def _last_change_step(state: AgentState) -> int:
    steps = [
        execution.step
        for execution in state.tool_history
        if execution.result.success and execution.result.metadata.get("changed_paths")
    ]
    return max(steps, default=0)
