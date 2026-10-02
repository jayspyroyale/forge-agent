"""Events the exploration controller emits. Like agent events, they are only displayed, never interpreted."""

from collections.abc import Callable

from pydantic import BaseModel

from forge.exploration.plans import ApproachPlan
from forge.exploration.results import CandidateResult, ExplorationRun


class ExplorationEvent(BaseModel):
    pass


class ExplorationStarted(ExplorationEvent):
    run_id: str
    task: str
    approaches: int
    baseline_kind: str
    uncommitted: int  # uncommitted files every candidate starts from
    left_out: int  # uncommitted files candidates do not see (--from-head)


class PlansReady(ExplorationEvent):
    plans: list[ApproachPlan]
    notes: list[str]
    round: int = 1


class CandidateStarted(ExplorationEvent):
    candidate_id: str
    plan: ApproachPlan
    workspace: str


class CandidateFinished(ExplorationEvent):
    result: CandidateResult


class ExplorationDecision(ExplorationEvent):
    """Adaptive exploration deciding whether to try more candidates (and why)."""

    action: str  # "continue" | "stop"
    reason: str


class ExplorationFinished(ExplorationEvent):
    run: ExplorationRun


ExplorationEventHandler = Callable[[ExplorationEvent], None]
