"""Approach plans: what each exploration candidate is going to try.

A plan is written before any code exists, so it is the model's *intent*.
Everything Forge later reports about a candidate (tests, diff, cost) is
measured from what actually happened, never taken from the plan.
"""

from typing import Literal

from pydantic import BaseModel, Field

Complexity = Literal["low", "medium", "high"]


class ModelChoice(BaseModel):
    """Which provider and model a candidate uses (None fields: the configured default)."""

    provider: str | None = None
    name: str | None = None
    base_url: str | None = None

    def label(self, default_provider: str = "?", default_name: str | None = None) -> str:
        return f"{self.provider or default_provider}:{self.name or default_name or 'default'}"


class ApproachPlan(BaseModel):
    id: str  # "A", "B", ...
    title: str
    summary: str
    files: list[str] = Field(default_factory=list)  # expected files or components
    assumptions: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)  # new dependencies the approach expects
    complexity: Complexity = "medium"
    # Lineage: generation 2+ plans are informed by earlier candidates' evidence.
    generation: int = 1
    parents: list[str] = Field(default_factory=list)
    # The model that will implement it (None: the configured default).
    model: ModelChoice | None = None


def candidate_brief(task: str, plan: ApproachPlan) -> str:
    """The task text given to a candidate's agent: the user's task plus the approach to implement."""

    def bullet(items: list[str]) -> str:
        return "; ".join(items) if items else "none stated"

    lineage = ""
    if plan.parents:
        lineage = f"\nThis approach builds on what Forge measured for candidates {', '.join(plan.parents)}.\n"
    return (
        f"{task}\n\n"
        f"[Forge exploration] You are implementing candidate {plan.id}, one of several independent attempts "
        "at this task in separate copies of the project. Implement THIS approach, even if you would have "
        "chosen another:\n\n"
        f"Approach {plan.id}: {plan.title}\n"
        f"{plan.summary}\n"
        f"{lineage}"
        f"- Expected files or components: {bullet(plan.files)}\n"
        f"- Assumptions: {bullet(plan.assumptions)}\n"
        f"- Known risks: {bullet(plan.risks)}\n"
        f"- Expected new dependencies: {bullet(plan.dependencies)}\n\n"
        "Keep the change focused on the task. Forge verifies every candidate itself and compares them "
        "using measured evidence (checks, diff size, dependencies, cost), so report honestly what you did."
    )


def candidate_ids(count: int, start: int = 0) -> list[str]:
    """A, B, ..., Z, then AA, AB, ..."""
    ids = []
    for number in range(start, start + count):
        label = ""
        number += 1
        while number:
            number, remainder = divmod(number - 1, 26)
            label = chr(ord("A") + remainder) + label
        ids.append(label)
    return ids
