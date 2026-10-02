"""The whole architecture in one task, driven through the real CLI entry path.

    CLI -> config (project .forge/config.toml) -> agent -> model provider (scripted)
        -> tool requests -> permission engine -> workspace / terminal -> edit
        -> Forge's own verification -> Git change tracking -> completion report

A user's repository with a buggy calculator, a pre-existing uncommitted
change, and a secret file just outside the workspace. The scripted model
tries to escape the workspace and to run a destructive command before doing
the real fix.
"""

import subprocess
import sys

from typer.testing import CliRunner

import forge.tools.builtin.terminal as terminal_tool
from forge.cli import app, route
from forge.git.repo import GitRepository
from forge.models.providers.fake import FakeModelProvider
from forge.models.types import ModelResponse, ToolCall
from forge.tasks.store import TaskStore
from forge.workspace import Workspace

PYTEST = f'"{sys.executable}" -m pytest -q -p no:cacheprovider'
BUG = "def multiply(a, b):\n    return a + b  # BUG: should be a * b"
FIX = "def multiply(a, b):\n    return a * b"


def git(cwd, *args):
    subprocess.run(
        ["git", "-c", "user.name=Forge Test", "-c", "user.email=test@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def tool(call_id, name, **arguments):
    return ModelResponse(tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)], finish_reason="tool_calls")


def test_full_task_through_every_layer(calculator_project, monkeypatch):
    repo_root = calculator_project
    outside = repo_root.parent / "outside.txt"
    outside.write_text("TOP SECRET\n")

    # The user's repository: committed project config and code, then an uncommitted change of their own.
    (repo_root / ".forge").mkdir()
    (repo_root / ".forge" / "config.toml").write_text(
        '[model]\nprovider = "fake"\n\n[agent]\nmax_steps = 12\n\n'
        '[permissions]\nwrite = "ask"\nexecute = "ask"\ndangerous = "deny"\n'
    )
    (repo_root / "NOTES.md").write_text("# Notes\n")
    git(repo_root, "init", "-q", "-b", "main")
    git(repo_root, "config", "core.autocrlf", "false")
    git(repo_root, "add", ".")
    git(repo_root, "commit", "-q", "-m", "initial")
    head_before = GitRepository(repo_root).head()
    (repo_root / "NOTES.md").write_text("# Notes\n- remember to refactor subtract\n")  # user's own edit
    notes_before = (repo_root / "NOTES.md").read_bytes()

    # A scripted model, and a spy on every command that actually reaches the shell.
    script = [
        tool("c1", "read_file", path="../outside.txt"),
        tool("c2", "run_command", command="rm -rf ."),
        tool("c3", "search_text", pattern="def multiply"),
        tool("c4", "read_file", path="calculator.py"),
        tool("c5", "edit_file", path="calculator.py", old_text=BUG, new_text=FIX),
        tool("c6", "run_command", command=PYTEST),
        "Fixed multiply to use *. The test suite passes.",
    ]
    configs = []

    def scripted_provider(config):
        configs.append(config)
        return FakeModelProvider(config, responses=list(script))

    monkeypatch.setattr("forge.agent.runtime.create_provider", scripted_provider)
    executed = []
    real_run_command = terminal_tool.run_command

    def spy(command, **kwargs):
        executed.append(command)
        return real_run_command(command, **kwargs)

    monkeypatch.setattr(terminal_tool, "run_command", spy)
    monkeypatch.chdir(repo_root)

    # Approve: the edit, the agent's own test run, and Forge's verification.
    result = CliRunner().invoke(app, route(["Fix the failing multiply tests"]), input="y\ny\ny\n")
    output = result.output

    # --- CLI and config -----------------------------------------------------------------
    assert result.exit_code == 0, output
    assert configs[0].model.provider == "fake"  # from the project config
    assert configs[0].agent.max_steps == 12

    # --- No workspace escape --------------------------------------------------------------
    assert "outside the workspace" in output
    assert "TOP SECRET" not in output
    assert outside.read_text() == "TOP SECRET\n"

    # --- No unapproved risky execution ----------------------------------------------------
    assert "dangerous actions are denied by policy" in output
    assert "rm -rf ." not in executed
    assert executed == [PYTEST]  # the only command the agent ran; verification uses its own runner
    assert output.count("Permission needed") == 3  # edit, agent's pytest, Forge verification
    assert (repo_root / "calculator.py").exists() and (repo_root / "tests").is_dir()

    # --- The fix, and no infinite loop -----------------------------------------------------------
    assert "return a * b" in (repo_root / "calculator.py").read_text()
    assert "✓ Completed in 7 steps" in output

    # --- Real verification evidence -------------------------------------------------------
    store = TaskStore(Workspace(repo_root))
    (summary,) = store.list_tasks()
    evidence = store.load_evidence(summary.task_id)
    assert evidence.verified
    assert [r.status for r in evidence.verification_rounds[-1]] == ["passed"]
    assert {c.kind: c.outcome for c in evidence.checks}["test"] == "verified"
    assert "✓ test" in output
    denied = [usage.name for usage in evidence.tool_usage if usage.denied]
    assert denied == ["run_command"]
    assert [usage.success for usage in evidence.tool_usage] == [False, False, True, True, True, True]

    # --- Correct Git change tracking -----------------------------------------------------------
    changes = evidence.changes
    assert changes.git
    assert [(c.path, c.origin, c.additions, c.deletions) for c in changes.changes] == [("calculator.py", "task", 1, 1)]
    assert changes.pre_existing == ["NOTES.md"]
    assert (repo_root / "NOTES.md").read_bytes() == notes_before  # the user's work is untouched
    repo = GitRepository(repo_root)
    assert repo.head() == head_before  # Forge never commits, resets, or checks out
    assert sorted(repo.status().modified) == ["NOTES.md", "calculator.py"]
    assert not [path for path in repo.status().untracked if path.startswith(".forge")]

    # --- Readable report -------------------------------------------------------------------------
    assert "Fixed multiply to use *." in output
    assert "calculator.py +1 -1" in output
    assert "1 file you had already changed: left untouched" in output
    assert f"forge tasks show {summary.task_id}" in output

    # --- And the task can be reviewed and undone afterwards ------------------------------------
    undo = CliRunner().invoke(app, ["tasks", "undo", summary.task_id, "--yes"])
    assert "Reverted 1 file(s)" in undo.output
    assert BUG in (repo_root / "calculator.py").read_text()
    assert (repo_root / "NOTES.md").read_bytes() == notes_before
