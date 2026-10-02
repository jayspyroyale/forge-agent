"""Choosing a candidate: manual, assisted, or autonomous.

    manual       Forge shows the comparison; the user picks (or picks nothing).
    assisted     Forge recommends the best eligible candidate and explains why; the user confirms
                 or picks another.
    autonomous   Forge picks the best eligible candidate by the configured policy. If no candidate
                 is eligible, nothing is selected.

People may pick a candidate that breaks a hard constraint (it is their call);
the selection then records which constraints it breaks. Forge never does.
"""

from collections.abc import Callable
from typing import Literal

from forge.exploration.compare import Comparison, explain
from forge.exploration.results import Selection

Mode = Literal["manual", "assisted", "autonomous"]
# Both get the comparison and return a candidate id, or None for "none of them".
Chooser = Callable[[Comparison], str | None]


class SelectionError(ValueError):
    pass


def select(comparison: Comparison, mode: Mode, chooser: Chooser | None = None) -> Selection:
    recommended = comparison.recommended
    if mode == "autonomous":
        chosen, decided_by = recommended, "policy"
    else:
        if chooser is None:  # nobody to ask: record the recommendation, select nothing
            return Selection(candidate_id=None, mode=mode, decided_by="user", recommended=recommended)
        chosen, decided_by = chooser(comparison), "user"

    if chosen is None:
        return Selection(candidate_id=None, mode=mode, decided_by=decided_by, recommended=recommended)  # type: ignore[arg-type]
    chosen = _resolve(comparison, chosen)
    reasons, tradeoffs = explain(comparison, chosen)
    violations = comparison.score(chosen).violations
    if violations:
        tradeoffs.insert(0, f"chosen although it breaks constraints: {'; '.join(violations)}")
    return Selection(
        candidate_id=chosen,
        mode=mode,
        decided_by=decided_by,  # type: ignore[arg-type]
        recommended=recommended,
        reasons=reasons,
        tradeoffs=tradeoffs,
    )


def _resolve(comparison: Comparison, candidate_id: str) -> str:
    for item in comparison.measured:
        if item.candidate_id.lower() == candidate_id.strip().lower():
            return item.candidate_id
    known = ", ".join(item.candidate_id for item in comparison.measured)
    raise SelectionError(f"No candidate '{candidate_id}'. Candidates: {known}")
