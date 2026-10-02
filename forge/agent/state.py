"""The data an agent carries through a task.

Only Forge's own code writes to this state. The model's words end up in
`messages` and `final_answer`; everything else (tool history, verification
results) is recorded by the runtime from what actually happened.
"""

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from forge.models.types import Message, ToolCall, Usage
from forge.tools.base import ToolResult
from forge.verification.checks import VerificationResult


class AgentStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"  # the model gave a final answer (and verification, if any, passed)
    VERIFICATION_FAILED = "verification_failed"  # finished, but Forge's checks still fail
    MAX_STEPS = "max_steps"  # stopped by the step limit
    FAILED = "failed"  # stopped by an error (for example the model provider failed)
    BUDGET_EXHAUSTED = "budget_exhausted"
    CANCELLED = "cancelled"


class ToolExecution(BaseModel):
    """One tool call the agent made, and what came back."""

    step: int
    call: ToolCall
    result: ToolResult


class VerificationRound(BaseModel):
    """One run of the project's checks, started by Forge after the agent changed files."""

    step: int
    results: list[VerificationResult]

    @property
    def passed(self) -> bool:
        """No check failed. Skipped checks (e.g. permission denied) do not count as failures."""
        return not any(result.failing for result in self.results)


def new_task_id() -> str:
    return uuid.uuid4().hex[:12]


def _now() -> datetime:
    return datetime.now(UTC)


class AgentState(BaseModel):
    task: str
    task_id: str = Field(default_factory=new_task_id)
    started_at: datetime = Field(default_factory=_now)
    finished_at: datetime | None = None
    messages: list[Message] = Field(default_factory=list)
    step: int = 0
    status: AgentStatus = AgentStatus.RUNNING
    tool_history: list[ToolExecution] = Field(default_factory=list)
    verification_rounds: list[VerificationRound] = Field(default_factory=list)
    final_answer: str | None = None
    error: str | None = None
    # Total across all model calls; None until a provider reports usage.
    usage: Usage | None = None

    def changed_paths(self) -> list[str]:
        """Files changed by successful tool calls, in first-changed order."""
        paths: dict[str, None] = {}
        for execution in self.tool_history:
            if execution.result.success:
                for path in execution.result.metadata.get("changed_paths", []):
                    paths[path] = None
        return list(paths)

    def last_mutation_step(self) -> int:
        """The last step with a tool call that changed, or may have changed, files (edits and commands)."""
        steps = [
            execution.step
            for execution in self.tool_history
            # A command can change files even when it exits with an error.
            if (execution.result.success and execution.result.metadata.get("changed_paths"))
            or "exit_code" in execution.result.metadata
        ]
        return max(steps, default=0)

    def change_count(self) -> int:
        """How many successful file-changing tool calls happened. Used to decide when to re-verify."""
        return sum(
            1
            for execution in self.tool_history
            if execution.result.success and execution.result.metadata.get("changed_paths")
        )
