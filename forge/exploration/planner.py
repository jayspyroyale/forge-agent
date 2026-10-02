"""Generating meaningfully different approaches before any code is written.

One model call (no tools) asks for N structured plans as JSON. Forge then
checks them: malformed entries are dropped, and near-duplicates (titles or
summaries that are almost the same) are removed, because exploring the
same idea twice wastes budget. If too few distinct plans remain, the
planner asks once more for the missing ones, listing what already exists.
"""

import difflib
import json
import re
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from forge.agent.prompts import (
    APPROACH_PLANNER,
    APPROACH_PLANNER_EXISTING,
    APPROACH_PLANNER_REQUEST,
    APPROACH_PLANNER_RETRY,
)
from forge.context.retrieval import find_relevant_files
from forge.exploration.plans import ApproachPlan, candidate_ids
from forge.models.base import ModelProvider
from forge.models.budget import BudgetExhausted
from forge.models.errors import ModelError
from forge.models.types import Message, Usage
from forge.workspace import Workspace

SIMILAR_TITLE = 0.85
SIMILAR_SUMMARY = 0.9
MAX_OVERVIEW_FILES = 60
_JSON_BLOCK = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class PlanningError(Exception):
    pass


class PlanningResult(BaseModel):
    plans: list[ApproachPlan]
    usage: Usage | None = None
    notes: list[str] = Field(default_factory=list)  # what the planner dropped and why


class _PlanFields(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    summary: str = Field(min_length=1, max_length=2_000)
    files: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    complexity: str = "medium"


class ApproachPlanner:
    def __init__(self, provider: ModelProvider, attempts: int = 2) -> None:
        self.provider = provider
        self.attempts = attempts

    async def plan(
        self,
        task: str,
        count: int,
        *,
        overview: str = "",
        existing: list[ApproachPlan] | None = None,
        start_index: int = 0,
    ) -> PlanningResult:
        """Up to `count` new, distinct plans. `existing` plans are avoided (adaptive exploration)."""
        existing = list(existing or [])
        accepted: list[ApproachPlan] = []
        notes: list[str] = []
        usage: Usage | None = None
        messages: list[Message] = []

        for attempt in range(self.attempts):
            missing = count - len(accepted)
            if missing <= 0:
                break
            if attempt == 0:
                messages = [
                    Message.system(APPROACH_PLANNER.format(count=missing)),
                    Message.user(_request(task, overview, existing + accepted)),
                ]
            try:
                response = await self.provider.generate(messages)
            except BudgetExhausted:
                raise
            except ModelError as error:
                if accepted or existing:
                    notes.append(f"planner call failed: {error}")
                    break
                raise PlanningError(f"The model could not plan approaches: {error}") from error
            if response.usage is not None:
                usage = response.usage if usage is None else usage + response.usage

            raw, problem = _parse(response.content)
            for fields in raw:
                if len(accepted) >= count:
                    break
                plan = ApproachPlan(id="?", **_normalize(fields))
                duplicate_of = _duplicate(plan, existing + accepted)
                if duplicate_of is not None:
                    notes.append(f"dropped '{plan.title}': too similar to '{duplicate_of.title}'")
                    continue
                accepted.append(plan)
            if problem:
                notes.append(problem)

            if len(accepted) < count:
                messages = [
                    *messages,
                    Message.assistant(response.content),
                    Message.user(
                        APPROACH_PLANNER_RETRY.format(problem=problem or f"only {len(accepted)} distinct approaches so far")
                        + "\n"
                        + APPROACH_PLANNER_EXISTING.format(approaches=_listing(existing + accepted))
                        + f"\nPropose {count - len(accepted)} more."
                    ),
                ]

        if not accepted and not existing:
            raise PlanningError("The model did not propose any usable approach. " + "; ".join(notes))
        if len(accepted) < count:
            notes.append(f"{len(accepted)} distinct approach(es) instead of the {count} requested")
        ids = candidate_ids(len(accepted), start=start_index)
        plans = [plan.model_copy(update={"id": ids[index]}) for index, plan in enumerate(accepted)]
        return PlanningResult(plans=plans, usage=usage, notes=notes)


def project_overview(workspace: Workspace, task: str) -> str:
    """A short, deterministic description of the project for the planner (paths only, no file contents)."""
    files = []
    for path in workspace.walk_files():
        files.append(workspace.relative(path))
        if len(files) >= MAX_OVERVIEW_FILES:
            files.append("...")
            break
    relevant = find_relevant_files(workspace, task, limit=8)
    lines = ["Files:", *(f"- {path}" for path in files)]
    if relevant:
        lines += ["Probably relevant (by name and text search):", *(f"- {item.path}" for item in relevant)]
    return "\n".join(lines)


def _request(task: str, overview: str, existing: list[ApproachPlan]) -> str:
    existing_text = APPROACH_PLANNER_EXISTING.format(approaches=_listing(existing)) if existing else ""
    return APPROACH_PLANNER_REQUEST.format(task=task, overview=overview or "(none)", existing=existing_text)


def _listing(plans: list[ApproachPlan]) -> str:
    return "\n".join(f"- {plan.title}: {plan.summary}" for plan in plans) or "- (none)"


def _parse(text: str) -> tuple[list[dict[str, Any]], str | None]:
    """The plan objects in a reply, and a description of what was wrong with it (if anything)."""
    candidates = [match.group(1) for match in _JSON_BLOCK.finditer(text)] + [text]
    data = None
    for candidate in candidates:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start == -1 or end <= start:
            continue
        try:
            data = json.loads(candidate[start : end + 1])
            break
        except json.JSONDecodeError:
            continue
    if not isinstance(data, dict) or not isinstance(data.get("approaches"), list):
        return [], "the reply was not a JSON object with an 'approaches' list"
    valid: list[dict[str, Any]] = []
    invalid = 0
    for item in data["approaches"]:
        try:
            valid.append(_PlanFields.model_validate(item).model_dump())
        except ValidationError:
            invalid += 1
    return valid, (f"{invalid} approach(es) were malformed and ignored" if invalid else None)


def _normalize(fields: dict[str, Any]) -> dict[str, Any]:
    complexity = str(fields.get("complexity", "medium")).strip().lower()
    fields["complexity"] = complexity if complexity in ("low", "medium", "high") else "medium"
    for key in ("files", "assumptions", "risks", "dependencies"):
        fields[key] = [str(item).strip() for item in fields.get(key, []) if str(item).strip()][:20]
    fields["title"] = fields["title"].strip()
    fields["summary"] = fields["summary"].strip()
    return fields


def _duplicate(plan: ApproachPlan, others: list[ApproachPlan]) -> ApproachPlan | None:
    for other in others:
        if _ratio(plan.title, other.title) >= SIMILAR_TITLE or _ratio(plan.summary, other.summary) >= SIMILAR_SUMMARY:
            return other
    return None


def _ratio(a: str, b: str) -> float:
    normalize = lambda text: " ".join(re.findall(r"[a-z0-9]+", text.lower()))  # noqa: E731
    return difflib.SequenceMatcher(a=normalize(a), b=normalize(b)).ratio()
