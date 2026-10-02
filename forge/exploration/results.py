"""The records of an exploration run and of each candidate in it.

Everything in a `CandidateResult` except `plan` is measured by Forge: the
status and proof of work come from the candidate's own `run_task`, the file
changes from hashing its workspace, the dependencies from its manifests.
"""

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

from forge.evidence import TaskEvidence
from forge.exploration.changes import FileDelta
from forge.exploration.isolation import Baseline
from forge.exploration.plans import ApproachPlan
from forge.models.types import Usage

CandidateStatus = Literal["completed", "verification_failed", "max_steps", "failed", "budget_exhausted", "crashed"]
RunStatus = Literal["planning", "running", "completed", "cancelled", "failed"]


class PermissionSummary(BaseModel):
    approved_by_user: int = 0
    denied: int = 0
    dangerous_attempts: int = 0  # actions classified dangerous (all denied unless policy allowed them)


class CandidateResult(BaseModel):
    candidate_id: str
    plan: ApproachPlan
    status: CandidateStatus
    task_id: str | None = None
    evidence: TaskEvidence | None = None
    files: list[FileDelta] = Field(default_factory=list)
    dependencies_added: list[str] = Field(default_factory=list)
    usage: Usage | None = None
    cost_usd: float | None = None
    duration_seconds: float = 0.0
    permissions: PermissionSummary = Field(default_factory=PermissionSummary)
    errors: list[str] = Field(default_factory=list)
    provider: str | None = None
    model: str | None = None
    workspace: str | None = None  # where it ran; only still present if kept
    workspace_kind: str | None = None
    kept: bool = False

    @property
    def files_changed(self) -> int:
        return len(self.files)

    @property
    def additions(self) -> int:
        return sum(delta.additions or 0 for delta in self.files)

    @property
    def deletions(self) -> int:
        return sum(delta.deletions or 0 for delta in self.files)

    @property
    def checks(self) -> dict[str, str]:
        """Check kind -> verified | failed | unverified, from the candidate's proof of work."""
        if self.evidence is None:
            return {}
        return {check.kind: check.outcome for check in self.evidence.checks}

    @property
    def total_tokens(self) -> int | None:
        return self.usage.total_tokens if self.usage is not None else None


def _now() -> datetime:
    return datetime.now(UTC)


class ExplorationRun(BaseModel):
    run_id: str
    task: str
    status: RunStatus = "planning"
    created_at: datetime = Field(default_factory=_now)
    finished_at: datetime | None = None
    baseline: Baseline
    plans: list[ApproachPlan] = Field(default_factory=list)
    planning_usage: Usage | None = None
    candidates: list[CandidateResult] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    error: str | None = None

    def candidate(self, candidate_id: str) -> CandidateResult:
        for result in self.candidates:
            if result.candidate_id.lower() == candidate_id.lower():
                return result
        raise KeyError(f"No candidate '{candidate_id}' in run {self.run_id}")
