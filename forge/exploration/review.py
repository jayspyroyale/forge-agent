"""MODEL-ASSESSMENT: a reviewer model's opinion on what Forge cannot measure.

The reviewer sees each candidate's plan and diff (not its test results) and
scores maintainability, architectural fit, readability, simplicity of
design, appropriateness, and scalability from 1 to 5. Its output is stored
as `Assessment`s, separate from measured evidence, and only feeds the
factors that are explicitly model-assessed (maintainability, scalability).
A reply that cannot be parsed simply produces no assessment.
"""

import json
import re

from pydantic import BaseModel

from forge.agent.prompts import CANDIDATE_REVIEW_REQUEST, CANDIDATE_REVIEWER
from forge.exploration.compare import ASSESSMENT_CRITERIA, Assessment
from forge.exploration.results import CandidateResult
from forge.models.base import ModelProvider
from forge.models.errors import ModelError
from forge.models.types import Message, Usage

MAX_DIFF_CHARS = 6_000


class ReviewResult(BaseModel):
    assessments: list[Assessment]
    usage: Usage | None = None
    note: str | None = None


async def review_candidates(
    provider: ModelProvider, task: str, candidates: list[CandidateResult], patches: dict[str, str]
) -> ReviewResult:
    reviewable = [c for c in candidates if c.files]
    if not reviewable:
        return ReviewResult(assessments=[], note="nothing to review: no candidate changed files")
    blocks = []
    for candidate in reviewable:
        diff = patches.get(candidate.candidate_id, "")
        if len(diff) > MAX_DIFF_CHARS:
            diff = diff[:MAX_DIFF_CHARS] + "\n[... diff truncated ...]"
        blocks.append(f"Candidate {candidate.candidate_id}: {candidate.plan.title}\n{candidate.plan.summary}\n```diff\n{diff}\n```")
    messages = [
        Message.system(CANDIDATE_REVIEWER),
        Message.user(CANDIDATE_REVIEW_REQUEST.format(task=task, candidates="\n\n".join(blocks))),
    ]
    try:
        response = await provider.generate(messages)
    except ModelError as error:
        return ReviewResult(assessments=[], note=f"model review failed: {error}")

    reviewer = f"{provider.name}:{_model(provider)}"
    known = {c.candidate_id for c in reviewable}
    assessments = []
    for item in _reviews(response.content):
        candidate_id = str(item.get("candidate", "")).strip().upper()
        scores = item.get("scores")
        if candidate_id not in known or not isinstance(scores, dict):
            continue
        clean = {
            key: int(value)
            for key, value in scores.items()
            if key in ASSESSMENT_CRITERIA and isinstance(value, int | float) and 1 <= value <= 5
        }
        if clean:
            assessments.append(
                Assessment(candidate_id=candidate_id, reviewer=reviewer, scores=clean, rationale=str(item.get("rationale", ""))[:500])
            )
            known.discard(candidate_id)
    note = None if assessments else "the model review could not be used (no valid scores)"
    return ReviewResult(assessments=assessments, usage=response.usage, note=note)


def _reviews(text: str) -> list[dict]:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    reviews = data.get("reviews") if isinstance(data, dict) else None
    return [item for item in reviews if isinstance(item, dict)] if isinstance(reviews, list) else []


def _model(provider: ModelProvider) -> str:
    try:
        return provider.model
    except Exception:
        return "default"
