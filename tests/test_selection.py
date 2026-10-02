"""Evidence-based comparison, policy selection (manual, assisted, autonomous), and safe apply."""

import asyncio
from datetime import UTC, datetime

import pytest
from exploration_helpers import BUG, FIX, Scripts, edit, explore_config, init_repo, permissive, plans_reply, tree_hashes
from pydantic import ValidationError

from forge.config.schema import SelectionConstraints, SelectionWeights
from forge.evidence import CheckReport, TaskEvidence, ToolUsage
from forge.exploration.apply import ApplyError, apply_candidate, preview_apply
from forge.exploration.changes import FileDelta
from forge.exploration.compare import Assessment, compare, measure
from forge.exploration.controller import ExplorationController
from forge.exploration.plans import ApproachPlan
from forge.exploration.results import CandidateResult
from forge.exploration.review import review_candidates
from forge.exploration.selection import SelectionError, select
from forge.models.providers.fake import FakeModelProvider
from forge.models.types import Usage
from forge.tasks.store import TaskStore
from forge.tasks.undo import apply_undo, plan_undo
from forge.workspace import Workspace

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def candidate(
    cid,
    *,
    status="completed",
    tests="verified",
    lint=None,
    files=1,
    adds=1,
    dels=1,
    deps=(),
    tokens=1_000,
    cost=None,
    duration=10.0,
    denied=0,
    dangerous=0,
    answer="Done. All tests pass!",
):
    def report(kind, outcome):
        if outcome is None:
            return CheckReport(kind=kind, outcome="unverified", detail="not configured")
        return CheckReport(kind=kind, outcome=outcome, detail=f"{kind} {outcome}")

    usage = [
        ToolUsage(step=1, name="run_command", success=False, risk="dangerous", denied=True) for _ in range(dangerous)
    ]
    usage += [ToolUsage(step=1, name="write_file", success=False, risk="write", denied=True) for _ in range(denied)]
    evidence = TaskEvidence(
        task_id=f"task{cid}",
        task="t",
        status=status,
        steps=3,
        started_at=NOW,
        finished_at=NOW,
        duration_seconds=duration,
        tool_usage=usage,
        commands_run=[],
        files_changed=[],
        verification_rounds=[],
        checks=[report("test", tests), report("build", None), report("typecheck", None), report("lint", lint)],
        usage=Usage(input_tokens=tokens),
        final_answer=answer,
        error=None,
    )
    deltas = [
        FileDelta(
            path=f"f{n}.py",
            kind="modified",
            additions=adds if n == 0 else 0,
            deletions=dels if n == 0 else 0,
            baseline_hash="x",
            final_hash="y",
        )
        for n in range(files)
    ]
    return CandidateResult(
        candidate_id=cid,
        plan=ApproachPlan(id=cid, title=f"Plan {cid}", summary="s"),
        status=status,
        evidence=evidence,
        files=deltas,
        dependencies_added=list(deps),
        usage=Usage(input_tokens=tokens),
        cost_usd=cost,
        duration_seconds=duration,
    )


def weights(**values):
    base = dict.fromkeys(
        ("correctness", "safety", "cost", "speed", "simplicity", "minimal_diff", "maintainability", "scalability"), 0
    )
    base.update(values)
    return SelectionWeights(**base)


DEFAULT = SelectionWeights()
RULES = SelectionConstraints()


# --- Measured evidence --------------------------------------------------------------------------


def test_measured_values_come_from_records_not_from_the_model():
    honest = candidate("A", tests="failed", answer="Tests fail.")
    lying = candidate("A", tests="failed", answer="All 40 tests pass, verified!")
    assert measure(honest) == measure(lying)
    item = measure(lying)
    assert item.tests == "failed" and item.checks == {"test": "failed"}
    assert (item.files_changed, item.additions, item.tokens) == (1, 1, 1_000)


def test_correctness_dominates_by_default():
    comparison = compare(
        [candidate("A", tests="failed", status="verification_failed", cost=0.01), candidate("B", cost=0.5)],
        DEFAULT,
        RULES,
    )
    assert comparison.recommended == "B"
    assert comparison.ranking == ["B"]


# --- Weights ------------------------------------------------------------------------------------


def test_weights_change_the_winner():
    cheap_big = candidate("A", cost=0.10, adds=200, files=4)
    pricey_small = candidate("B", cost=0.50, adds=3)
    by_cost = compare([cheap_big, pricey_small], weights(correctness=10, cost=90), RULES)
    by_diff = compare([cheap_big, pricey_small], weights(correctness=10, minimal_diff=90), RULES)
    assert by_cost.recommended == "A"
    assert by_diff.recommended == "B"


def test_weights_are_normalized_and_validated():
    assert sum(
        SelectionWeights(correctness=50, safety=20, cost=10, speed=10, simplicity=10).normalized().values()
    ) == pytest.approx(1)
    assert weights(correctness=1).normalized()["correctness"] == 1
    with pytest.raises(ValidationError, match="at least one selection weight"):
        weights()
    with pytest.raises(ValidationError):
        SelectionWeights(cost=-1)
    assert SelectionWeights(latency=7).speed == 7  # the Phase 12 name still works


@pytest.mark.parametrize("value", [float("inf"), float("nan")])
def test_nonfinite_weights_are_rejected(value):
    with pytest.raises(ValidationError):
        SelectionWeights(cost=value)


def test_required_security_checks_cannot_be_unverified():
    comparison = compare(
        [candidate("A", lint="unverified")], DEFAULT, SelectionConstraints(security_checks_must_pass=True)
    )
    assert comparison.recommended is None
    assert "lint were not verified" in comparison.score("A").violations


# --- Hard constraints -----------------------------------------------------------------------------


def test_constraint_violation_makes_a_candidate_ineligible_whatever_its_score():
    fast_with_deps = candidate("A", deps=["python:redis"], duration=1, cost=0.01)
    slow_plain = candidate("B", duration=100, cost=0.5)
    comparison = compare([fast_with_deps, slow_plain], DEFAULT, SelectionConstraints(no_new_dependencies=True))
    a = comparison.score("A")
    assert not a.eligible and a.violations == ["adds dependencies (python:redis)"]
    # The violation is reported on its own; it is not folded into the score as a penalty.
    assert a.total > 0.9
    assert {f.factor for f in a.factors} == {f.factor for f in comparison.score("B").factors}
    assert comparison.recommended == "B"


@pytest.mark.parametrize(
    ("result", "constraints", "message"),
    [
        (candidate("A", files=12), SelectionConstraints(max_files_changed=10), "changes 12 files (limit 10)"),
        (candidate("A", cost=0.9), SelectionConstraints(max_cost_usd=0.5), "cost $0.9000 (limit $0.5000)"),
        (candidate("A"), SelectionConstraints(max_cost_usd=0.5), "cost unknown"),
        (
            candidate("A", dangerous=1),
            SelectionConstraints(security_checks_must_pass=True),
            "attempted 1 dangerous action(s)",
        ),
        (candidate("A", lint="failed"), SelectionConstraints(security_checks_must_pass=True), "lint failed"),
        (candidate("A", tests="unverified"), RULES, "tests were not verified"),
        (candidate("A", status="max_steps"), RULES, "did not complete (max steps)"),
        (candidate("A", files=0), RULES, "changed nothing"),
    ],
)
def test_constraints(result, constraints, message):
    comparison = compare([result], DEFAULT, constraints)
    assert comparison.recommended is None
    assert any(message in violation for violation in comparison.score("A").violations)


def test_constraints_can_be_relaxed():
    comparison = compare([candidate("A", tests="failed")], DEFAULT, SelectionConstraints(tests_must_pass=False))
    assert comparison.recommended == "A"


def test_failed_and_crashed_candidates_are_scored_zero_correctness_and_ineligible():
    crashed = candidate("A", status="crashed", files=0)
    crashed.evidence = None
    comparison = compare([crashed, candidate("B")], DEFAULT, RULES)
    assert next(f.value for f in comparison.score("A").factors if f.factor == "correctness") == 0
    assert comparison.ranking == ["B"]


# --- Ties ---------------------------------------------------------------------------------------


def test_ties_are_broken_deterministically_and_reported():
    comparison = compare([candidate("A", adds=10), candidate("B", adds=2)], weights(correctness=1), RULES)
    assert comparison.score("A").total == comparison.score("B").total
    assert comparison.recommended == "B"  # tie-break: smaller diff
    assert comparison.tied_with == ["A"]
    assert any("tied with A" in tradeoff for tradeoff in comparison.tradeoffs)
    identical = compare([candidate("B"), candidate("A")], weights(correctness=1), RULES)
    assert identical.recommended == "A"  # last tie-break: candidate id


# --- Explanation ------------------------------------------------------------------------------


def test_transparent_explanation():
    a = candidate("A", cost=0.21, adds=40, deps=["python:redis"])
    b = candidate("B", cost=0.1092, adds=3)
    c = candidate("C", tests="failed", status="verification_failed")
    comparison = compare([a, b, c], DEFAULT, RULES)
    assert comparison.recommended == "B"
    assert comparison.reasons == [
        "all tests passed",
        "48% cheaper than A",
        "smallest diff (+3 -1)",
        "no new dependencies",
        "C is not eligible: did not complete (verification failed)",
    ]


def test_model_assessment_is_separate_and_labelled():
    a, b = candidate("A", adds=5), candidate("B", adds=5)
    reviews = [
        Assessment(candidate_id="A", reviewer="fake:m", scores={"scalability": 5, "maintainability": 3}),
        Assessment(candidate_id="B", reviewer="fake:m", scores={"scalability": 2, "maintainability": 3}),
    ]
    unweighted = compare([a, b], DEFAULT, RULES, reviews)
    assert unweighted.measured == compare([a, b], DEFAULT, RULES).measured  # opinions never touch measurements
    assert unweighted.score("A").total == unweighted.score("B").total

    weighted = compare([a, b], SelectionWeights(scalability=30), RULES, reviews)
    assert weighted.recommended == "A"
    factor = next(f for f in weighted.score("A").factors if f.factor == "scalability")
    assert factor.source == "model" and "model assessment" in factor.detail
    chosen_b = select(weighted, "manual", chooser=lambda _: "B")
    assert any("A has stronger scalability (model assessment: 5.0/5 vs 2.0/5)" in t for t in chosen_b.tradeoffs)


def test_weighted_model_factor_without_review_is_neutral_and_noted():
    comparison = compare([candidate("A"), candidate("B")], SelectionWeights(maintainability=50), RULES)
    assert all(f.source == "neutral" for s in comparison.scores for f in s.factors if f.factor == "maintainability")
    assert any("no model assessment" in note for note in comparison.notes)


def test_reviewer_output_is_parsed_and_validated():
    reply = (
        '{"reviews": [{"candidate": "a", "scores": {"maintainability": 4, "scalability": 9, "vibes": 5}, "rationale": "clean"},'
        ' {"candidate": "Z", "scores": {"maintainability": 1}}]}'
    )
    result = asyncio.run(
        review_candidates(
            FakeModelProvider(responses=[reply]), "t", [candidate("A"), candidate("B")], {"A": "+x", "B": "+y"}
        )
    )
    (assessment,) = result.assessments
    assert assessment.candidate_id == "A" and assessment.scores == {"maintainability": 4}
    assert assessment.reviewer == "fake:fake-model"
    unusable = asyncio.run(
        review_candidates(FakeModelProvider(responses=["I like them all"]), "t", [candidate("A")], {})
    )
    assert unusable.assessments == [] and "could not be used" in unusable.note


# --- Selection modes ------------------------------------------------------------------------------


def two():
    return compare([candidate("A", cost=0.3), candidate("B", cost=0.1)], DEFAULT, RULES)


def test_manual_mode():
    comparison = two()
    chosen = select(comparison, "manual", chooser=lambda c: "a")
    assert (chosen.candidate_id, chosen.decided_by, chosen.recommended) == ("A", "user", "B")
    assert select(comparison, "manual", chooser=lambda c: None).candidate_id is None
    assert select(comparison, "manual").candidate_id is None  # nobody to ask
    with pytest.raises(SelectionError, match="No candidate 'Q'"):
        select(comparison, "manual", chooser=lambda c: "Q")


def test_assisted_mode():
    comparison = two()
    accepted = select(comparison, "assisted", chooser=lambda c: c.recommended)
    assert accepted.candidate_id == "B" and accepted.reasons
    assert select(comparison, "assisted", chooser=lambda c: None).candidate_id is None


def test_autonomous_mode():
    chosen = select(two(), "autonomous")
    assert (chosen.candidate_id, chosen.decided_by) == ("B", "policy")
    assert "67% cheaper than A" in chosen.reasons
    nothing = select(compare([candidate("A", tests="failed")], DEFAULT, RULES), "autonomous")
    assert nothing.candidate_id is None


def test_people_may_override_constraints_and_it_is_recorded():
    comparison = compare([candidate("A", tests="failed"), candidate("B")], DEFAULT, RULES)
    chosen = select(comparison, "manual", chooser=lambda c: "A")
    assert chosen.candidate_id == "A"
    assert chosen.tradeoffs[0] == "chosen although it breaks constraints: tests did not pass"


# --- Applying the selected candidate -------------------------------------------------------------------


@pytest.fixture
def explored(calculator_project, tmp_path):
    repo = init_repo(calculator_project)
    (repo / "NOTES.md").write_text("mine\n")  # unrelated uncommitted work
    scripts = Scripts(
        repo, {"planner": [plans_reply("Fix", "Other fix")], "A": [edit(BUG, FIX), "ok"], "B": ["nothing"]}
    )
    controller = ExplorationController(
        explore_config(repo, tmp_path), permissions=permissive(), provider_factory=scripts
    )
    run = asyncio.run(controller.explore("Fix multiply."))
    return repo, controller, run


def test_autonomous_selection_and_apply(explored):
    repo, controller, run = explored
    notes = (repo / "NOTES.md").read_bytes()
    selection = select(run.comparison, "autonomous")
    controller.record_selection(run, selection)
    assert run.final_evidence is run.candidate("A").evidence

    record, evidence = apply_candidate(run, "A", controller.workspace, controller.store)

    assert record.files == ["calculator.py"]
    assert "return a * b" in (repo / "calculator.py").read_text()
    assert (repo / "NOTES.md").read_bytes() == notes  # unrelated work untouched
    saved = TaskStore(controller.workspace).load_evidence(record.task_id)
    assert saved.verified  # the candidate's proof of work is the final evidence
    assert saved.extra["exploration"]["candidate"] == "A"
    assert [c.path for c in saved.changes.changes] == ["calculator.py"]
    assert controller.store.load(run.run_id).applied.task_id == record.task_id
    # unselected candidates keep their evidence for inspection
    assert controller.store.load(run.run_id).candidate("B").evidence is not None
    with pytest.raises(ApplyError, match="already applied"):
        apply_candidate(run, "A", controller.workspace, controller.store)


def test_apply_refuses_when_the_user_changed_the_file_meanwhile(explored):
    repo, controller, run = explored
    (repo / "calculator.py").write_text((repo / "calculator.py").read_text() + "\n# edited during exploration\n")
    before = tree_hashes(repo)
    preview = preview_apply(run, "A", controller.workspace)
    assert preview.conflicts == ["calculator.py: changed since exploration started"]
    with pytest.raises(ApplyError, match="Nothing was applied"):
        apply_candidate(run, "A", controller.workspace, controller.store)
    assert tree_hashes(repo) == before


def test_apply_is_undoable(explored):
    repo, controller, run = explored
    record, _ = apply_candidate(run, "A", controller.workspace, controller.store)
    store = TaskStore(Workspace(repo))
    apply_undo(plan_undo(store, record.task_id), store)
    assert BUG in (repo / "calculator.py").read_text()


def test_damaged_record_is_never_applied(explored):
    repo, controller, run = explored
    controller.store.save_candidate_file(run.run_id, "A", "calculator.py", b"tampered")
    with pytest.raises(ApplyError, match="missing or damaged"):
        apply_candidate(run, "A", controller.workspace, controller.store)
    assert BUG in (repo / "calculator.py").read_text()


def test_candidate_that_changed_nothing_cannot_be_applied(explored):
    _, controller, run = explored
    assert "changed nothing" in preview_apply(run, "B", controller.workspace).conflicts[0]


def test_undo_rechecks_files_after_preview(explored):
    repo, controller, run = explored
    record, _ = apply_candidate(run, "A", controller.workspace, controller.store)
    store = TaskStore(controller.workspace)
    plan = plan_undo(store, record.task_id)
    later_work = "# later user edit\n"
    (repo / "calculator.py").write_text(later_work)
    assert apply_undo(plan, store) == []
    assert (repo / "calculator.py").read_text() == later_work


def test_undo_refuses_damaged_original(explored):
    repo, controller, run = explored
    record, _ = apply_candidate(run, "A", controller.workspace, controller.store)
    store = TaskStore(controller.workspace)
    plan = plan_undo(store, record.task_id)
    blob = next((store.directory(record.task_id) / "originals").glob("*.bin"))
    blob.write_bytes(b"damaged")
    before = tree_hashes(repo)
    with pytest.raises(ValueError, match="damaged"):
        apply_undo(plan, store)
    assert tree_hashes(repo) == before


def test_apply_refuses_another_workspace(explored, tmp_path):
    _, _, run = explored
    other = tmp_path / "other"
    other.mkdir()
    assert "another workspace" in preview_apply(run, "A", Workspace(other)).conflicts[0]
