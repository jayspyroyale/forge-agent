"""Git awareness, task change tracking, and safe undo, using real temporary repositories."""

import asyncio
import subprocess

import pytest
from typer.testing import CliRunner

from forge.agent.runtime import run_task
from forge.cli import app
from forge.config import ForgeConfig
from forge.git import repo as git_repo_module
from forge.git.repo import GitError, GitRepository
from forge.models.providers.fake import FakeModelProvider
from forge.models.types import ModelResponse, ToolCall
from forge.security.permissions import PermissionEngine
from forge.security.policy import PermissionPolicy
from forge.tasks.snapshot import compute_changes, take_snapshot
from forge.tasks.store import TaskStore
from forge.tasks.undo import apply_undo, plan_undo
from forge.workspace import Workspace


def git(cwd, *args):
    subprocess.run(
        ["git", "-c", "user.name=Forge Test", "-c", "user.email=test@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo_dir(tmp_path):
    """A committed repository: app.py, notes.txt, and tests/test_app.py."""
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "core.autocrlf", "false")
    (root / "app.py").write_bytes(b"def double(x):\n    return x + x\n\n\ndef triple(x):\n    return x * 3\n")
    (root / "notes.txt").write_bytes(b"my notes\n")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "initial")
    return root


def tool(call_id, name, **arguments):
    return ModelResponse(tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)])


def run(root, responses, record=True):
    return asyncio.run(
        run_task(
            ForgeConfig(workspace=root),
            "task",
            provider=FakeModelProvider(responses=responses),
            permissions=PermissionEngine(PermissionPolicy.permissive()),
            checks=[],
            record=record,
        )
    )


# --- Repository reading -----------------------------------------------------------


def test_not_a_repository(tmp_path):
    assert GitRepository.discover(tmp_path) is None


def test_discover_from_subdirectory(repo_dir):
    (repo_dir / "src").mkdir()
    repo = GitRepository.discover(repo_dir / "src")
    assert repo.root == repo_dir.resolve()
    assert repo.branch() == "main"
    assert len(repo.head()) == 40


def test_clean_status(repo_dir):
    status = GitRepository(repo_dir).status()
    assert status.clean
    assert status.branch == "main"


def test_modified_and_untracked_status(repo_dir):
    (repo_dir / "app.py").write_bytes(b"changed\n")
    (repo_dir / "new.txt").write_bytes(b"new\n")
    (repo_dir / "notes.txt").write_bytes(b"staged\n")
    git(repo_dir, "add", "notes.txt")

    status = GitRepository(repo_dir).status()

    assert not status.clean
    assert sorted(status.modified) == ["app.py", "notes.txt"]
    assert status.untracked == ["new.txt"]
    staged = next(entry for entry in status.entries if entry.path == "notes.txt")
    assert staged.index == "M"


def test_repository_without_commits(tmp_path):
    git(tmp_path, "init", "-q")
    (tmp_path / "a.txt").write_text("a")
    repo = GitRepository(tmp_path)
    assert repo.head() is None
    assert repo.status().untracked == ["a.txt"]
    assert repo.diff_summary().files == []


def test_diff_summary(repo_dir):
    (repo_dir / "app.py").write_bytes(b"def double(x):\n    return 2 * x\n\n\ndef triple(x):\n    return x * 3\n")
    diff = GitRepository(repo_dir).diff_summary()
    assert [(f.path, f.additions, f.deletions) for f in diff.files] == [("app.py", 1, 1)]
    assert "+    return 2 * x" in diff.text
    assert not diff.truncated


def test_large_diff_is_truncated(repo_dir):
    (repo_dir / "app.py").write_bytes(b"".join(b"line %d\n" % n for n in range(2000)))
    diff = GitRepository(repo_dir).diff_summary(max_chars=1000)
    assert diff.truncated
    assert "diff truncated" in diff.text
    assert diff.additions == 2000


def test_head_content(repo_dir):
    repo = GitRepository(repo_dir)
    assert repo.head_content("notes.txt") == b"my notes\n"
    assert repo.head_content("missing.txt") is None


def test_state_changing_git_commands_are_refused(repo_dir):
    repo = GitRepository(repo_dir)
    for command in (["reset", "--hard"], ["checkout", "."], ["clean", "-fd"], ["commit", "-m", "x"], ["push"]):
        with pytest.raises(GitError, match="only read-only"):
            repo.git(*command)


# --- Git tools for the model ---------------------------------------------------------


def test_git_status_and_diff_tools(repo_dir):
    from helpers import run_tool

    (repo_dir / "app.py").write_bytes(b"def double(x):\n    return 2 * x\n")
    status = run_tool(repo_dir, "git_status")
    assert status.success
    assert "modified: app.py" in status.output
    diff = run_tool(repo_dir, "git_diff", path="app.py")
    assert "app.py: +1 -" in diff.output


def test_git_tools_outside_a_repository(tmp_path):
    from helpers import run_tool

    assert "not inside a Git repository" in run_tool(tmp_path, "git_status").error


# --- Task change tracking ---------------------------------------------------------------


def test_task_changes_are_separated_from_pre_existing_changes(repo_dir):
    (repo_dir / "notes.txt").write_bytes(b"my notes\nwork in progress\n")  # the user's own change
    before = (repo_dir / "notes.txt").read_bytes()

    outcome = run(
        repo_dir,
        [
            tool("c1", "edit_file", path="app.py", old_text="return x + x", new_text="return 2 * x"),
            tool("c2", "write_file", path="extra.py", content="VALUE = 1\n"),
            "done",
        ],
    )

    changes = outcome.evidence.changes
    assert changes.git
    assert [(c.path, c.origin, c.kind, c.additions, c.deletions) for c in changes.changes] == [
        ("app.py", "task", "modified", 1, 1),
        ("extra.py", "task", "added", 1, 0),
    ]
    assert changes.pre_existing == ["notes.txt"]
    assert (repo_dir / "notes.txt").read_bytes() == before  # untouched
    assert outcome.evidence.files_changed == ["app.py", "extra.py"]
    assert not changes.head_moved


def test_task_editing_a_file_the_user_already_changed_is_mixed(repo_dir):
    (repo_dir / "app.py").write_bytes(b"def double(x):\n    return x + x  # user edit\n")
    outcome = run(
        repo_dir,
        [tool("c1", "edit_file", path="app.py", old_text="return x + x", new_text="return 2 * x"), "done"],
    )
    (change,) = outcome.evidence.changes.changes
    assert (change.path, change.origin) == ("app.py", "mixed")
    assert (change.additions, change.deletions) == (1, 1)  # measured against the journal copy


def test_changes_made_by_commands_are_detected(repo_dir):
    import sys

    command = f'"{sys.executable}" -c "open(\'made_by_command.txt\', \'w\').write(\'hi\')"'
    outcome = run(repo_dir, [tool("c1", "run_command", command=command), "done"])
    assert [(c.path, c.kind) for c in outcome.evidence.changes.changes] == [("made_by_command.txt", "added")]


def test_deleted_file_is_tracked(repo_dir):
    workspace = Workspace(repo_dir)
    repo = GitRepository(repo_dir)
    start = take_snapshot(workspace, repo)
    (repo_dir / "notes.txt").unlink()
    (change,) = compute_changes(start, workspace, repo).changes
    assert (change.path, change.kind, change.deletions) == ("notes.txt", "deleted", 1)


def test_tracking_outside_git(tmp_path):
    (tmp_path / "a.txt").write_text("one\n")
    outcome = run(
        tmp_path,
        [
            tool("c1", "edit_file", path="a.txt", old_text="one", new_text="two"),
            tool("c2", "write_file", path="b.txt", content="new\n"),
            "done",
        ],
    )
    changes = outcome.evidence.changes
    assert not changes.git
    assert [(c.path, c.kind, c.origin) for c in changes.changes] == [("a.txt", "modified", "task"), ("b.txt", "added", "task")]


def test_task_records_are_saved_and_ignored_by_git(repo_dir):
    outcome = run(repo_dir, [tool("c1", "write_file", path="extra.py", content="x = 1\n"), "done"])
    task_dir = repo_dir / ".forge" / "tasks" / outcome.evidence.task_id
    assert (task_dir / "checkpoint.json").is_file()
    assert (task_dir / "evidence.json").is_file()
    assert (task_dir / "journal.json").is_file()
    # Forge's own records never show up as changes in the user's repository.
    assert GitRepository(repo_dir).status().untracked == ["extra.py"]
    assert TaskStore(Workspace(repo_dir)).load_evidence(outcome.evidence.task_id).task_id == outcome.evidence.task_id


def test_record_false_writes_nothing(repo_dir):
    run(repo_dir, ["done"], record=False)
    assert not (repo_dir / ".forge").exists()


def test_no_state_changing_git_commands_during_a_task(repo_dir, monkeypatch):
    seen = []
    real_run = subprocess.run

    def spy(args, *rest, **kwargs):
        if args and args[0] == "git":
            seen.append(args)
        return real_run(args, *rest, **kwargs)

    monkeypatch.setattr(git_repo_module.subprocess, "run", spy)
    (repo_dir / "notes.txt").write_bytes(b"dirty\n")

    run(repo_dir, [tool("c1", "edit_file", path="app.py", old_text="x + x", new_text="2 * x"), "done"])

    subcommands = {args[3] for args in seen}  # ["git", "-c", "core.quotepath=false", <subcommand>, ...]
    assert subcommands <= {"rev-parse", "status", "diff", "show"}
    assert seen


# --- Undo ------------------------------------------------------------------------------


def test_undo_restores_task_changes_and_keeps_user_changes(repo_dir):
    (repo_dir / "notes.txt").write_bytes(b"my notes\nwork in progress\n")
    original_app = (repo_dir / "app.py").read_bytes()
    outcome = run(
        repo_dir,
        [
            tool("c1", "edit_file", path="app.py", old_text="return x + x", new_text="return 2 * x"),
            tool("c2", "write_file", path="extra.py", content="VALUE = 1\n"),
            "done",
        ],
    )
    store = TaskStore(Workspace(repo_dir))

    plan = plan_undo(store, outcome.evidence.task_id, GitRepository(repo_dir))
    assert [(a.path, a.action) for a in plan.actions] == [("app.py", "restore"), ("extra.py", "delete")]

    apply_undo(plan, store, GitRepository(repo_dir))

    assert (repo_dir / "app.py").read_bytes() == original_app
    assert not (repo_dir / "extra.py").exists()
    assert (repo_dir / "notes.txt").read_bytes() == b"my notes\nwork in progress\n"
    backup = repo_dir / ".forge" / "tasks" / outcome.evidence.task_id / "undo-backup" / "app.py"
    assert b"2 * x" in backup.read_bytes()


def test_undo_restores_the_users_version_of_a_mixed_file(repo_dir):
    user_version = b"def double(x):\n    return x + x  # user edit\n"
    (repo_dir / "app.py").write_bytes(user_version)
    outcome = run(repo_dir, [tool("c1", "edit_file", path="app.py", old_text="return x + x", new_text="return 2 * x"), "done"])
    store = TaskStore(Workspace(repo_dir))

    apply_undo(plan_undo(store, outcome.evidence.task_id), store)

    assert (repo_dir / "app.py").read_bytes() == user_version


def test_undo_skips_files_changed_after_the_task(repo_dir):
    outcome = run(repo_dir, [tool("c1", "edit_file", path="app.py", old_text="x + x", new_text="2 * x"), "done"])
    (repo_dir / "app.py").write_bytes(b"later work by the user\n")

    plan = plan_undo(TaskStore(Workspace(repo_dir)), outcome.evidence.task_id)

    assert [(a.action, a.reason) for a in plan.actions] == [("skip", "changed again after the task finished")]
    assert plan.safe == []


def test_undo_uses_head_for_command_changes_to_clean_files(repo_dir):
    import sys

    command = f'"{sys.executable}" -c "open(\'notes.txt\', \'w\').write(\'overwritten\')"'
    outcome = run(repo_dir, [tool("c1", "run_command", command=command), "done"])
    repo = GitRepository(repo_dir)
    store = TaskStore(Workspace(repo_dir))

    plan = plan_undo(store, outcome.evidence.task_id, repo)
    assert [(a.path, a.action, a.source) for a in plan.actions] == [("notes.txt", "restore", "head")]
    apply_undo(plan, store, repo)
    assert (repo_dir / "notes.txt").read_bytes() == b"my notes\n"


def test_cli_tasks_list_show_and_undo(repo_dir, monkeypatch):
    outcome = run(repo_dir, [tool("c1", "write_file", path="extra.py", content="x = 1\n"), "done"])
    task_id = outcome.evidence.task_id
    monkeypatch.chdir(repo_dir)
    runner = CliRunner()

    listed = runner.invoke(app, ["tasks", "list"])
    assert task_id in listed.output

    shown = runner.invoke(app, ["tasks", "show", task_id])
    assert shown.exit_code == 0
    assert "extra.py" in shown.output

    dry = runner.invoke(app, ["tasks", "undo", task_id])
    assert "Dry run" in dry.output
    assert (repo_dir / "extra.py").exists()

    applied = runner.invoke(app, ["tasks", "undo", task_id, "--yes"])
    assert "Reverted 1 file(s)" in applied.output
    assert not (repo_dir / "extra.py").exists()


def test_cli_unknown_task(repo_dir, monkeypatch):
    monkeypatch.chdir(repo_dir)
    result = CliRunner().invoke(app, ["tasks", "show", "deadbeef"])
    assert result.exit_code == 1
    assert "No recorded task" in result.output
