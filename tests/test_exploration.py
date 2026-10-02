"""Branching exploration: planning, isolation, independent candidates, failure handling, records, cleanup."""

import asyncio
import json

import pytest
from exploration_helpers import (
    BUG,
    FIX,
    WRONG,
    Scripts,
    edit,
    explore_config,
    git,
    init_repo,
    permissive,
    plans_reply,
    tool,
    tree_hashes,
)

from forge.exploration.controller import ExplorationController
from forge.exploration.dependencies import added_dependencies, declared
from forge.exploration.isolation import ExplorationError
from forge.exploration.planner import ApproachPlanner
from forge.exploration.plans import candidate_ids
from forge.exploration.store import ExplorationStore
from forge.git.repo import GitRepository, StatusEntry
from forge.models.providers.fake import FakeModelProvider
from forge.workspace import Workspace


def explore(project, tmp_path, scripts, approaches=2, events=None, crash=frozenset(), **options):
    factory = Scripts(project, scripts, crash=crash)
    controller = ExplorationController(
        explore_config(project, tmp_path),
        permissions=permissive(),
        provider_factory=factory,
        on_event=events.append if events is not None else None,
    )
    run = asyncio.run(controller.explore("Fix multiply.", approaches, **options))
    return run, factory, controller


@pytest.fixture
def repo(calculator_project):
    return init_repo(calculator_project)


# --- Two and three candidates ----------------------------------------------------------------


def test_two_candidates_produce_independent_results(repo, tmp_path):
    before = tree_hashes(repo)
    head = GitRepository(repo).head()
    run, factory, controller = explore(
        repo,
        tmp_path,
        {"planner": [plans_reply("Direct fix", "Rewrite with operator.mul")], "A": [edit(BUG, FIX), "Fixed."], "B": [edit(BUG, WRONG), "Fixed?"]},
    )

    assert run.status == "completed"
    assert [plan.id for plan in run.plans] == ["A", "B"]
    a, b = run.candidates
    assert (a.status, b.status) == ("completed", "verification_failed")
    assert a.checks["test"] == "verified" and b.checks["test"] == "failed"
    assert [(d.path, d.kind, d.additions, d.deletions) for d in a.files] == [("calculator.py", "modified", 1, 1)]
    assert [d.path for d in b.files] == ["calculator.py"]
    assert a.files[0].final_hash != b.files[0].final_hash  # independent diffs

    # The user's project is untouched: same files, same HEAD, no worktrees left behind.
    assert tree_hashes(repo) == before
    assert GitRepository(repo).head() == head
    assert git(repo, "worktree", "list").count("\n") == 1
    assert not (tmp_path / "worktrees" / run.run_id).exists()

    # Each candidate's agent worked in its own copy with the approach in its task.
    roots = {key: config.workspace_root for key, config in factory.configs.items() if key in "AB"}
    assert roots["A"] != roots["B"] and repo not in roots.values()
    task_text = factory.providers["A"].calls[0]["messages"][-1].content
    assert "Fix multiply." in task_text and "Approach A: Direct fix" in task_text


def test_three_candidates_with_one_crash(repo, tmp_path):
    run, _, _ = explore(
        repo,
        tmp_path,
        {"planner": [plans_reply("One", "Two different", "Three alternative")], "A": [edit(BUG, FIX), "ok"], "C": [edit(BUG, FIX), "ok"]},
        approaches=3,
        crash={"B"},
    )
    statuses = {c.candidate_id: c.status for c in run.candidates}
    assert statuses == {"A": "completed", "B": "crashed", "C": "completed"}
    assert "candidate B blew up" in run.candidate("B").errors[0]
    assert run.status == "completed"


def test_model_failure_is_a_failed_candidate_not_a_failed_run(repo, tmp_path):
    run, _, _ = explore(repo, tmp_path, {"planner": [plans_reply("One", "Two other")], "A": [], "B": [edit(BUG, FIX), "ok"]})
    assert run.candidate("A").status == "failed"
    assert "no scripted responses" in run.candidate("A").errors[0]
    assert run.candidate("B").status == "completed"


# --- Isolation --------------------------------------------------------------------------------


def test_candidates_cannot_see_each_others_work(repo, tmp_path):
    run, factory, _ = explore(
        repo,
        tmp_path,
        {
            "planner": [plans_reply("Writer", "Reader approach")],
            "A": [tool("w", "write_file", path="secret_from_a.txt", content="A was here"), "wrote"],
            "B": [tool("r", "read_file", path="secret_from_a.txt"), "read"],
        },
    )
    assert [d.path for d in run.candidate("A").files] == ["secret_from_a.txt"]
    (read,) = run.candidate("B").evidence.tool_usage
    assert read.success is False
    assert not (repo / "secret_from_a.txt").exists()


def test_candidates_cannot_escape_their_workspace(repo, tmp_path):
    run, _, _ = explore(
        repo,
        tmp_path,
        {"planner": [plans_reply("Escape", "Stay home")], "A": [tool("w", "write_file", path="../escaped.txt", content="x"), "done"], "B": ["nothing"]},
    )
    assert run.candidate("A").files == []
    assert not list(tmp_path.rglob("escaped.txt"))


def test_dirty_repository_baseline_is_shared_but_never_modified(repo, tmp_path):
    (repo / "NOTES.md").write_text("my uncommitted notes\n")  # untracked
    (repo / "calculator.py").write_text((repo / "calculator.py").read_text() + "\n# my local tweak\n")
    before = tree_hashes(repo)
    run, factory, _ = explore(
        repo,
        tmp_path,
        {"planner": [plans_reply("Reads notes", "Fixes code")], "A": [tool("r", "read_file", path="NOTES.md"), "seen"], "B": [edit(BUG, FIX), "ok"]},
    )

    assert run.baseline.uncommitted == ["NOTES.md", "calculator.py"]
    assert "my uncommitted notes" in factory.providers["A"].calls[1]["messages"][-1].content
    b = run.candidate("B")
    assert [d.path for d in b.files] == ["calculator.py"]  # only the candidate's own change
    patch = ExplorationStore(Workspace(repo)).patch(run.run_id, "B")
    assert "-    return a + b  # BUG: should be a * b" in patch and "my local tweak" not in patch.split("@@")[0]
    assert tree_hashes(repo) == before


def test_from_head_leaves_uncommitted_changes_out(repo, tmp_path):
    (repo / "NOTES.md").write_text("draft\n")
    run, factory, _ = explore(
        repo,
        tmp_path,
        {"planner": [plans_reply("Look", "Other")], "A": [tool("r", "file_exists", path="NOTES.md"), "done"], "B": ["done"]},
        include_uncommitted=False,
    )
    assert run.baseline.left_out == ["NOTES.md"] and run.baseline.uncommitted == []
    assert "does not exist" in factory.providers["A"].calls[1]["messages"][-1].content


def test_non_git_project_uses_copies(calculator_project, tmp_path):
    before = tree_hashes(calculator_project)
    run, _, _ = explore(calculator_project, tmp_path, {"planner": [plans_reply("Fix", "Other fix")], "A": [edit(BUG, FIX), "ok"], "B": ["no change"]})
    assert run.baseline.kind == "copy"
    assert run.candidate("A").checks["test"] == "verified"
    assert run.candidate("B").files == []
    assert tree_hashes(calculator_project) == before


def test_keep_workspaces(repo, tmp_path):
    run, _, _ = explore(repo, tmp_path, {"planner": [plans_reply("Fix", "Other fix")], "A": [edit(BUG, FIX), "ok"], "B": ["no"]}, keep_workspaces=True)
    a = run.candidate("A")
    assert a.kept and "return a * b" in (tmp_path / "worktrees" / run.run_id / "A" / "calculator.py").read_text()


# --- Unsafe repository states ---------------------------------------------------------------------


def test_merge_in_progress_is_refused(repo, tmp_path):
    (repo / ".git" / "MERGE_HEAD").write_text(GitRepository(repo).head() + "\n")
    with pytest.raises(ExplorationError, match="A merge is in progress"):
        explore(repo, tmp_path, {})
    assert not (tmp_path / "worktrees").exists()


def test_repository_without_commits_is_refused(calculator_project, tmp_path):
    git(calculator_project, "init", "-q")
    with pytest.raises(ExplorationError, match="no commits yet"):
        explore(calculator_project, tmp_path, {})


def test_unmerged_entries_are_detected():
    assert StatusEntry(path="a", index="U", worktree="U").unmerged
    assert StatusEntry(path="a", index="A", worktree="A").unmerged
    assert not StatusEntry(path="a", index="M", worktree=" ").unmerged


def test_approach_count_is_limited(repo, tmp_path):
    with pytest.raises(ExplorationError, match="between 1 and 10"):
        explore(repo, tmp_path, {}, approaches=11)


# --- Records and events -----------------------------------------------------------------------


def test_run_is_recorded_for_later_inspection(repo, tmp_path):
    events = []
    run, _, _ = explore(repo, tmp_path, {"planner": [plans_reply("Fix", "Other fix")], "A": [edit(BUG, FIX), "ok"], "B": ["no"]}, events=events)
    store = ExplorationStore(Workspace(repo))
    loaded = store.load(run.run_id)
    assert loaded.candidate("A").evidence.verified
    assert store.candidate_file(run.run_id, "A", "calculator.py").decode().count("return a * b") == 1
    assert [summary.run_id for summary in store.list_runs()] == [run.run_id]
    assert [type(event).__name__ for event in events] == [
        "ExplorationStarted",
        "PlansReady",
        "CandidateStarted",
        "CandidateFinished",
        "CandidateStarted",
        "CandidateFinished",
        "ExplorationFinished",
    ]
    assert not [path for path in GitRepository(repo).status().untracked if path.startswith(".forge/")]


def test_planning_failure_fails_cleanly(repo, tmp_path):
    with pytest.raises(ExplorationError, match="did not propose any usable approach"):
        explore(repo, tmp_path, {"planner": ["I would rather not.", "Still no."]})
    (summary,) = ExplorationStore(Workspace(repo)).list_runs()
    assert summary.status == "failed"


# --- Planner ------------------------------------------------------------------------------------


def plan(provider_replies, count=3, existing=None):
    planner = ApproachPlanner(FakeModelProvider(responses=provider_replies))
    return asyncio.run(planner.plan("Add caching", count, existing=existing)), planner


def test_planner_parses_fenced_json_and_numbers_plans():
    result, _ = plan(["Here you go:\n```json\n" + plans_reply("In-memory LRU", "Redis cache", "SQLite-backed cache") + "\n```"])
    assert [(p.id, p.title) for p in result.plans] == [("A", "In-memory LRU"), ("B", "Redis cache"), ("C", "SQLite-backed cache")]


def test_planner_drops_duplicates_and_asks_again():
    first = plans_reply("In-memory LRU cache", "In memory LRU cache!", extra=[{"title": "broken"}])
    result, planner = plan([first, plans_reply("Redis cache", "File cache")])
    assert [p.title for p in result.plans] == ["In-memory LRU cache", "Redis cache", "File cache"]
    assert any("too similar" in note for note in result.notes)
    assert any("malformed" in note for note in result.notes)
    retry = planner.provider.calls[1]["messages"][-1].content
    assert "In-memory LRU cache" in retry and "Propose 2 more" in retry


def test_planner_accepts_fewer_distinct_plans():
    result, _ = plan([plans_reply("Only idea"), "not json"], count=3)
    assert [p.title for p in result.plans] == ["Only idea"]
    assert "1 distinct approach(es) instead of the 3 requested" in result.notes


def test_candidate_ids():
    assert candidate_ids(3) == ["A", "B", "C"]
    assert candidate_ids(2, start=25) == ["Z", "AA"]


# --- Dependencies -------------------------------------------------------------------------------


def test_new_dependencies_are_detected_from_manifests():
    before = {"pyproject.toml": b'[project]\ndependencies = ["requests>=2"]\n', "package.json": b'{"dependencies": {"left-pad": "1"}}'}
    after = {
        "pyproject.toml": b'[project]\ndependencies = ["Requests>=2.1", "redis[hiredis]>=5"]\n',
        "package.json": json.dumps({"dependencies": {"left-pad": "1"}, "devDependencies": {"vitest": "2"}}).encode(),
    }
    added = added_dependencies(list(after), before.get, after.get)
    assert added == ["node:vitest", "python:redis"]
    assert declared("requirements-dev.txt", b"# tools\npytest==8\n-r base.txt\nruff\n") == {"python:pytest", "python:ruff"}


# --- CLI ------------------------------------------------------------------------------------------


def test_explore_cli(repo, tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from forge.cli import app, route

    scripts = Scripts(repo, {"planner": [plans_reply("Direct fix", "Other fix")], "A": [edit(BUG, FIX), "Fixed."], "B": ["Gave up."]})
    monkeypatch.setattr("forge.exploration.controller.create_provider", scripts)
    monkeypatch.chdir(repo)
    (repo / ".forge").mkdir(exist_ok=True)
    (repo / ".forge" / "config.toml").write_text(
        f'[exploration]\nworkspace_dir = "{(tmp_path / "wt").as_posix()}"\n[agent]\nverification_attempts = 1\n'
    )
    runner = CliRunner()

    # Assisted mode: accept the recommendation, then confirm applying it.
    result = runner.invoke(app, route(["explore", "-n", "2", "--yes", "Fix multiply"]), input="y\ny\n")

    assert result.exit_code == 0, result.output
    output = result.output
    assert "Candidate plans" in output and "Direct fix" in output
    assert "Candidate A" in output and "Candidate B" in output
    assert "A: completed" in output and "tests PASS" in output
    assert "MEASURED" in output and "Recommended: Candidate A" in output
    assert "B ineligible: changed nothing" in output
    assert "Applied candidate A" in output
    assert "return a * b" in (repo / "calculator.py").read_text()
    run = ExplorationStore(Workspace(repo)).latest()
    run_id = run.run_id
    assert run.selection.candidate_id == "A" and run.applied.candidate_id == "A"

    undo = runner.invoke(app, ["tasks", "undo", run.applied.task_id, "--yes"])
    assert "Reverted 1 file(s)" in undo.output
    assert BUG in (repo / "calculator.py").read_text()

    listed = runner.invoke(app, ["explorations", "list"])
    assert run_id in listed.output
    shown = runner.invoke(app, ["explorations", "show", run_id, "-c", "A", "--patch"])
    assert "+    return a * b" in shown.output
    missing = runner.invoke(app, ["explorations", "show", run_id, "-c", "Z"])
    assert missing.exit_code == 1 and "No candidate 'Z'" in missing.output
