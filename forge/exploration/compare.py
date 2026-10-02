"""Comparing candidates with evidence and the user's priorities.

Three separate things, never mixed:

    MEASURED          numbers taken from Forge's records of each candidate (checks Forge ran,
                      files it hashed, tokens the provider reported, time it measured, permission
                      decisions). A model cannot change them.
    MODEL-ASSESSMENT  a reviewer model's opinion on what cannot be measured (maintainability,
                      scalability, ...). Optional, always labelled, only used through its own factors.
    POLICY            hard constraints (pass/fail eligibility, never part of the score) and weights.

Scoring is deterministic: each factor becomes a value between 0 and 1,
relative to the other eligible candidates, and the total is the weighted
sum with weights normalized to 1. Ties are broken by correctness, then the
smaller diff, then the lower cost, then the candidate id, and reported as
ties.
"""

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

from forge.config.schema import SelectionConstraints, SelectionWeights

if TYPE_CHECKING:  # results.py stores a Comparison, so it imports this module
    from forge.exploration.results import CandidateResult

Factor = Literal["correctness", "safety", "cost", "speed", "simplicity", "minimal_diff", "maintainability", "scalability"]
MODEL_FACTORS: dict[str, tuple[str, ...]] = {
    "maintainability": ("maintainability", "readability", "architectural_fit"),
    "scalability": ("scalability",),
}
ASSESSMENT_CRITERIA = (
    "maintainability",
    "architectural_fit",
    "readability",
    "simplicity_of_design",
    "appropriateness",
    "scalability",
)
TIE_MARGIN = 0.01
_OUTCOME_VALUE = {"verified": 1.0, "unverified": 0.5, "failed": 0.0}


class Measured(BaseModel):
    """What Forge measured for one candidate. Built only from its records."""

    candidate_id: str
    status: str
    checks: dict[str, str] = Field(default_factory=dict)  # configured check kinds only
    files_changed: int = 0
    additions: int = 0
    deletions: int = 0
    new_dependencies: list[str] = Field(default_factory=list)
    tokens: int | None = None
    cost_usd: float | None = None
    duration_seconds: float = 0.0
    tool_calls: int = 0
    retries: int = 0  # verification rounds that failed before the last one
    denials: int = 0
    dangerous_attempts: int = 0
    verification_incomplete: bool = False  # a configured check was not run (or not allowed) on the final code
    errors: list[str] = Field(default_factory=list)

    @property
    def lines_changed(self) -> int:
        return self.additions + self.deletions

    @property
    def tests(self) -> str | None:
        return self.checks.get("test")


class Assessment(BaseModel):
    """MODEL-ASSESSMENT of one candidate: scores from 1 (poor) to 5 (excellent), with the reviewer's reasons."""

    candidate_id: str
    reviewer: str
    scores: dict[str, int]
    rationale: str = ""


class FactorScore(BaseModel):
    factor: str
    value: float  # 0..1, higher is better
    weight: float  # normalized
    source: Literal["measured", "model", "neutral"]  # neutral: no data, 0.5 for everyone
    detail: str = ""


class CandidateScore(BaseModel):
    candidate_id: str
    eligible: bool
    violations: list[str] = Field(default_factory=list)
    factors: list[FactorScore] = Field(default_factory=list)
    total: float = 0.0


class Comparison(BaseModel):
    measured: list[Measured]
    assessments: list[Assessment] = Field(default_factory=list)
    scores: list[CandidateScore]
    weights: dict[str, float]
    constraints: SelectionConstraints
    ranking: list[str]  # eligible candidates, best first
    recommended: str | None
    tied_with: list[str] = Field(default_factory=list)  # candidates within the tie margin of the recommended one
    reasons: list[str] = Field(default_factory=list)  # why the recommended candidate (+)
    tradeoffs: list[str] = Field(default_factory=list)  # where others are better (-)
    notes: list[str] = Field(default_factory=list)

    def score(self, candidate_id: str) -> CandidateScore:
        return next(score for score in self.scores if score.candidate_id == candidate_id)

    def measured_for(self, candidate_id: str) -> Measured:
        return next(item for item in self.measured if item.candidate_id == candidate_id)


def measure(result: "CandidateResult") -> Measured:
    evidence = result.evidence
    checks: dict[str, str] = {}
    incomplete = False
    retries = denials = dangerous = tool_calls = 0
    if evidence is not None:
        for check in evidence.checks:
            if check.detail == "not configured":
                continue
            checks[check.kind] = check.outcome
            incomplete = incomplete or check.outcome == "unverified"
        rounds = evidence.verification_rounds
        retries = sum(1 for round_ in rounds[:-1] if any(r.failing for r in round_))
        tool_calls = len(evidence.tool_usage)
        denials = sum(1 for usage in evidence.tool_usage if usage.denied)
        dangerous = sum(1 for usage in evidence.tool_usage if usage.risk == "dangerous")
    return Measured(
        candidate_id=result.candidate_id,
        status=result.status,
        checks=checks,
        files_changed=result.files_changed,
        additions=result.additions,
        deletions=result.deletions,
        new_dependencies=list(result.dependencies_added),
        tokens=result.total_tokens,
        cost_usd=result.cost_usd,
        duration_seconds=result.duration_seconds,
        tool_calls=tool_calls,
        retries=retries,
        denials=denials,
        dangerous_attempts=dangerous,
        verification_incomplete=incomplete and result.files_changed > 0,
        errors=list(result.errors),
    )


def violations(item: Measured, constraints: SelectionConstraints) -> list[str]:
    if item.files_changed == 0 and item.status == "completed":
        return ["changed nothing"]  # nothing to apply; the other rules are moot
    found = []
    if constraints.require_completed and item.status != "completed":
        found.append(f"did not complete ({item.status.replace('_', ' ')})")
    if constraints.tests_must_pass and item.tests is not None and item.tests != "verified":
        found.append("tests did not pass" if item.tests == "failed" else "tests were not verified")
    if constraints.security_checks_must_pass:
        if item.dangerous_attempts:
            found.append(f"attempted {item.dangerous_attempts} dangerous action(s)")
        failed = [kind for kind in ("lint", "typecheck") if item.checks.get(kind) == "failed"]
        if failed:
            found.append(f"{' and '.join(failed)} failed")
        missing = [kind for kind in ("lint", "typecheck") if item.checks.get(kind) == "unverified"]
        if missing:
            found.append(f"{' and '.join(missing)} were not verified")
    if constraints.no_new_dependencies and item.new_dependencies:
        found.append(f"adds dependencies ({', '.join(item.new_dependencies)})")
    if constraints.max_files_changed is not None and item.files_changed > constraints.max_files_changed:
        found.append(f"changes {item.files_changed} files (limit {constraints.max_files_changed})")
    if constraints.max_cost_usd is not None:
        if item.cost_usd is None:
            found.append("cost unknown (a cost limit is set; configure [pricing] for this model)")
        elif item.cost_usd > constraints.max_cost_usd:
            found.append(f"cost ${item.cost_usd:.4f} (limit ${constraints.max_cost_usd:.4f})")
    return found


def compare(
    results: list["CandidateResult"],
    weights: SelectionWeights,
    constraints: SelectionConstraints,
    assessments: list[Assessment] | None = None,
) -> Comparison:
    measured = [measure(result) for result in results]
    assessments = assessments or []
    normalized = weights.normalized()
    by_id = {item.candidate_id: item for item in measured}
    eligible = [item for item in measured if not violations(item, constraints)]
    pool = eligible or measured  # relative scores are computed among eligible candidates when there are any

    notes = []
    scores = []
    for item in measured:
        factors = _factors(item, pool, assessments, normalized, notes)
        total = round(sum(f.value * f.weight for f in factors), 6)
        problems = violations(item, constraints)
        scores.append(CandidateScore(candidate_id=item.candidate_id, eligible=not problems, violations=problems, factors=factors, total=total))

    def order(score: CandidateScore):
        item = by_id[score.candidate_id]
        correctness = next(f.value for f in score.factors if f.factor == "correctness")
        cost = item.cost_usd if item.cost_usd is not None else float("inf")
        return (-score.total, -correctness, item.lines_changed, cost, score.candidate_id)

    ranked = sorted((score for score in scores if score.eligible), key=order)
    ranking = [score.candidate_id for score in ranked]
    recommended = ranking[0] if ranking else None
    tied = [s.candidate_id for s in ranked[1:] if ranked and ranked[0].total - s.total <= TIE_MARGIN]
    comparison = Comparison(
        measured=measured,
        assessments=assessments,
        scores=scores,
        weights=normalized,
        constraints=constraints,
        ranking=ranking,
        recommended=recommended,
        tied_with=tied,
        notes=sorted(set(notes)),
    )
    if recommended is not None:
        comparison.reasons, comparison.tradeoffs = explain(comparison, recommended)
    return comparison


# --- factors --------------------------------------------------------------------------------


def _factors(item: Measured, pool: list[Measured], assessments: list[Assessment], weights: dict[str, float], notes: list[str]) -> list[FactorScore]:
    factors = [
        FactorScore(factor="correctness", value=_correctness(item), weight=weights["correctness"], source="measured", detail=_checks_detail(item)),
        FactorScore(
            factor="safety",
            value=max(0.0, 1.0 - 0.5 * item.dangerous_attempts - 0.15 * item.denials),
            weight=weights["safety"],
            source="measured",
            detail=f"{item.dangerous_attempts} dangerous, {item.denials} denied",
        ),
        _cost(item, pool, weights["cost"], notes),
        _relative("speed", item.duration_seconds, [p.duration_seconds for p in pool], weights["speed"], f"{item.duration_seconds:.1f}s"),
        _relative(
            "simplicity",
            item.files_changed + 2 * len(item.new_dependencies) + 1,
            [p.files_changed + 2 * len(p.new_dependencies) + 1 for p in pool],
            weights["simplicity"],
            f"{item.files_changed} files, {len(item.new_dependencies)} new deps",
        ),
        _relative(
            "minimal_diff",
            item.lines_changed + 1,
            [p.lines_changed + 1 for p in pool],
            weights["minimal_diff"],
            f"+{item.additions} -{item.deletions}",
        ),
    ]
    for factor, criteria in MODEL_FACTORS.items():
        assessment = next((a for a in assessments if a.candidate_id == item.candidate_id), None)
        values = [assessment.scores[c] for c in criteria if assessment and c in assessment.scores]
        if values:
            mean = sum(values) / len(values)
            factors.append(
                FactorScore(factor=factor, value=(mean - 1) / 4, weight=weights[factor], source="model", detail=f"{mean:.1f}/5 (model assessment)")
            )
        else:
            if weights[factor] > 0:
                notes.append(f"{factor} has weight but no model assessment; every candidate counts as neutral (0.5)")
            factors.append(FactorScore(factor=factor, value=0.5, weight=weights[factor], source="neutral", detail="not assessed"))
    return factors


def _correctness(item: Measured) -> float:
    if item.status in ("crashed", "failed"):
        return 0.0
    values = [_OUTCOME_VALUE[outcome] for outcome in item.checks.values()]
    value = sum(values) / len(values) if values else 0.5  # no checks configured: nothing proven either way
    return value if item.status == "completed" else value * 0.5


def _checks_detail(item: Measured) -> str:
    if not item.checks:
        return "no checks configured"
    return ", ".join(f"{kind} {outcome}" for kind, outcome in item.checks.items())


def _cost(item: Measured, pool: list[Measured], weight: float, notes: list[str]) -> FactorScore:
    if all(p.cost_usd is not None for p in pool) and item.cost_usd is not None:
        return _relative("cost", item.cost_usd, [p.cost_usd for p in pool], weight, f"${item.cost_usd:.4f}")  # type: ignore[misc]
    if all(p.tokens is not None for p in pool) and item.tokens is not None:
        if weight > 0:
            notes.append("cost is compared by tokens: not every candidate's price is known")
        return _relative("cost", item.tokens, [p.tokens for p in pool], weight, f"{item.tokens:,} tokens")  # type: ignore[misc]
    return FactorScore(factor="cost", value=0.5, weight=weight, source="neutral", detail="unknown")


def _relative(factor: str, value: float, values: list[float], weight: float, detail: str) -> FactorScore:
    """Lower is better: the best candidate gets 1.0, others best/value."""
    best = min(values) if values else value
    if value <= 0:
        score = 1.0
    else:
        score = max(0.0, min(1.0, best / value)) if best > 0 else (1.0 if value == best else 0.0)
    return FactorScore(factor=factor, value=round(score, 6), weight=weight, source="measured", detail=detail)


# --- explanation ----------------------------------------------------------------------------


def explain(comparison: Comparison, chosen: str) -> tuple[list[str], list[str]]:
    """Plain-language reasons for `chosen` (+) and where other eligible candidates are better (-)."""
    me = comparison.measured_for(chosen)
    others = [m for m in comparison.measured if m.candidate_id != chosen]
    eligible_others = [m for m in others if comparison.score(m.candidate_id).eligible]
    weights = comparison.weights
    reasons: list[str] = []
    tradeoffs: list[str] = []

    if me.checks and all(outcome == "verified" for outcome in me.checks.values()):
        kinds = ", ".join(me.checks)
        reasons.append("all tests passed" if list(me.checks) == ["test"] else f"all checks passed ({kinds})")
    for other in eligible_others:
        if me.cost_usd is not None and other.cost_usd and me.cost_usd < other.cost_usd * 0.9:
            reasons.append(f"{round(100 * (1 - me.cost_usd / other.cost_usd))}% cheaper than {other.candidate_id}")
        elif me.cost_usd is None and me.tokens is not None and other.tokens and me.tokens < other.tokens * 0.9:
            reasons.append(f"{round(100 * (1 - me.tokens / other.tokens))}% fewer tokens than {other.candidate_id}")
    if eligible_others and all(me.lines_changed < o.lines_changed for o in eligible_others):
        reasons.append(f"smallest diff (+{me.additions} -{me.deletions})")
    if not me.new_dependencies and any(o.new_dependencies for o in others):
        reasons.append("no new dependencies")
    if eligible_others and all(me.duration_seconds < o.duration_seconds for o in eligible_others):
        reasons.append(f"fastest ({me.duration_seconds:.1f}s)")
    for other in others:
        problems = comparison.score(other.candidate_id).violations
        if problems:
            reasons.append(f"{other.candidate_id} is not eligible: {problems[0]}")

    for other in eligible_others:
        if weights["minimal_diff"] > 0 and other.lines_changed < me.lines_changed:
            tradeoffs.append(f"{other.candidate_id} has a smaller diff (+{other.additions} -{other.deletions})")
        if weights["cost"] > 0 and other.cost_usd is not None and me.cost_usd is not None and other.cost_usd < me.cost_usd * 0.9:
            tradeoffs.append(f"{other.candidate_id} was {round(100 * (1 - other.cost_usd / me.cost_usd))}% cheaper")
        if weights["speed"] > 0 and other.duration_seconds < me.duration_seconds * 0.8:
            tradeoffs.append(f"{other.candidate_id} was faster ({other.duration_seconds:.1f}s vs {me.duration_seconds:.1f}s)")
        if not other.new_dependencies and me.new_dependencies:
            tradeoffs.append(f"{other.candidate_id} adds no dependencies")
        for factor in MODEL_FACTORS:
            mine = next(f for f in comparison.score(chosen).factors if f.factor == factor)
            theirs = next(f for f in comparison.score(other.candidate_id).factors if f.factor == factor)
            if mine.source == theirs.source == "model" and theirs.value > mine.value:
                tradeoffs.append(f"{other.candidate_id} has stronger {factor} (model assessment: {theirs.detail.split(' ')[0]} vs {mine.detail.split(' ')[0]})")
    if comparison.tied_with:
        tradeoffs.append(f"tied with {', '.join(comparison.tied_with)} on score; chosen by the tie-break (correctness, smaller diff, lower cost)")
    return reasons, tradeoffs
