"""The terminal experience: routing, the interactive session, live output, reports, and errors."""

import pytest
from typer.testing import CliRunner

from forge.cli import app, route
from forge.models.errors import ModelRequestError
from forge.models.providers.fake import FakeModelProvider
from forge.models.types import ModelResponse, ToolCall

runner = CliRunner()


def script(monkeypatch, responses):
    monkeypatch.setattr(
        "forge.agent.runtime.create_provider",
        lambda config: FakeModelProvider(config, responses=list(responses)),
    )


def tool(call_id, name, **arguments):
    return ModelResponse(tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)])


# --- Routing -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "routed"),
    [
        ([], ["session"]),
        (["Fix the failing tests"], ["run", "Fix the failing tests"]),
        (["-p", "fake", "task"], ["run", "-p", "fake", "task"]),
        (["--yes", "task"], ["run", "--yes", "task"]),
        (["run", "task"], ["run", "task"]),
        (["doctor"], ["doctor"]),
        (["tools", "list"], ["tools", "list"]),
        (["tasks", "list"], ["tasks", "list"]),
        (["--version"], ["--version"]),
        (["--help"], ["--help"]),
    ],
)
def test_route(argv, routed):
    assert route(argv) == routed


def test_bare_task_runs_the_agent(workspace_root, monkeypatch):
    monkeypatch.chdir(workspace_root)
    result = runner.invoke(app, route(["-p", "fake", "say hello"]))
    assert result.exit_code == 0
    assert "Task: say hello" in result.output
    assert "[fake] say hello" in result.output


def test_existing_commands_are_unchanged():
    for args in (["--version"], ["doctor"], ["models", "list"], ["tools", "list"]):
        assert runner.invoke(app, args).exit_code == 0, args


# --- Live output and the completion report ---------------------------------------------


def test_run_shows_tool_calls_and_report(workspace_root, monkeypatch):
    monkeypatch.chdir(workspace_root)
    script(monkeypatch, [tool("c1", "read_file", path="allowed.txt"), "It says hello."])

    result = runner.invoke(app, ["run", "Read allowed.txt"])

    assert result.exit_code == 0
    output = result.output
    assert "→ read_file path=allowed.txt" in output
    assert "It says hello." in output
    assert "✓ Completed in 2 steps" in output
    assert "Changed: nothing" in output
    assert "Verification:" in output
    assert "forge tasks show" in output
    # Normal mode keeps quiet about successful reads.
    assert "hello from inside" not in output


def test_verbose_shows_tool_output(workspace_root, monkeypatch):
    monkeypatch.chdir(workspace_root)
    script(monkeypatch, [tool("c1", "read_file", path="allowed.txt"), "done"])
    result = runner.invoke(app, ["run", "--verbose", "Read allowed.txt"])
    assert "hello from inside" in result.output
    assert "asking the model" in result.output


def test_edits_and_denials_are_shown(workspace_root, monkeypatch):
    monkeypatch.chdir(workspace_root)
    script(
        monkeypatch,
        [
            tool("c1", "write_file", path="new.txt", content="hi\n"),
            tool("c2", "run_command", command="rm -rf ."),
            "Wrote new.txt; the cleanup command was refused.",
        ],
    )

    result = runner.invoke(app, ["run", "--yes", "task"])

    assert "✎ Created new.txt" in result.output
    assert "⊘ Permission denied: dangerous actions are denied by policy" in result.output
    assert "new.txt added +1 -0" in result.output
    assert (workspace_root / "allowed.txt").exists()


def test_permission_prompt_in_run(workspace_root, monkeypatch):
    monkeypatch.chdir(workspace_root)
    script(monkeypatch, [tool("c1", "write_file", path="new.txt", content="hi\n"), "I could not write it."])

    result = runner.invoke(app, ["run", "task"], input="n\n")

    assert "Permission needed: write_file (write)" in result.output
    assert "denied by the user" in result.output
    assert not (workspace_root / "new.txt").exists()


def test_max_steps_report(workspace_root, monkeypatch):
    monkeypatch.chdir(workspace_root)
    script(monkeypatch, [tool(f"c{n}", "list_files") for n in range(5)])
    result = runner.invoke(app, ["run", "--max-steps", "2", "loop"])
    assert result.exit_code == 1
    assert "reached the step limit" in result.output


# --- Errors ------------------------------------------------------------------------------


class Broken(FakeModelProvider):
    async def generate(self, messages, tools=None, **options):
        raise ModelRequestError("connection refused")


def test_model_error_is_one_clean_line(workspace_root, monkeypatch):
    monkeypatch.chdir(workspace_root)
    monkeypatch.setattr("forge.agent.runtime.create_provider", lambda config: Broken(config))
    result = runner.invoke(app, ["run", "task"])
    assert result.exit_code == 1
    assert "connection refused" in result.output
    assert "Traceback" not in result.output


def test_missing_key_error_normal_vs_debug(workspace_root, monkeypatch):
    monkeypatch.chdir(workspace_root)
    normal = runner.invoke(app, ["run", "task"])
    assert "OPENAI_API_KEY" in normal.output
    assert "Traceback" not in normal.output

    debug = runner.invoke(app, ["run", "--debug", "task"])
    assert "Traceback" in debug.output


# --- Interactive session --------------------------------------------------------------------


def test_interactive_session_runs_tasks_until_exit(workspace_root, monkeypatch):
    monkeypatch.chdir(workspace_root)
    monkeypatch.setenv("FORGE_PROVIDER", "fake")

    result = runner.invoke(app, ["session"], input="first task\n\nsecond task\n/exit\n")

    assert result.exit_code == 0
    output = result.output
    assert "Model:" in output and "fake" in output
    assert "Workspace:" in output
    assert "[fake] first task" in output
    assert "[fake] second task" in output
    assert "Bye." in output


def test_interactive_help_and_unknown_commands(workspace_root, monkeypatch):
    monkeypatch.chdir(workspace_root)
    monkeypatch.setenv("FORGE_PROVIDER", "fake")
    result = runner.invoke(app, ["session"], input="/help\n/nope\n/verbose\n/quit\n")
    assert "/exit" in result.output
    assert "Unknown command /nope" in result.output
    assert "output: verbose" in result.output


def test_interactive_session_ends_on_eof(workspace_root, monkeypatch):
    monkeypatch.chdir(workspace_root)
    monkeypatch.setenv("FORGE_PROVIDER", "fake")
    assert runner.invoke(app, ["session"], input="").exit_code == 0


def test_interactive_session_survives_task_errors(workspace_root, monkeypatch):
    monkeypatch.chdir(workspace_root)
    result = runner.invoke(app, ["session"], input="task one\n/exit\n")  # default provider: no API key
    assert "OPENAI_API_KEY" in result.output
    assert "Bye." in result.output


def test_session_approvals_carry_over_between_tasks(workspace_root, monkeypatch):
    monkeypatch.chdir(workspace_root)
    # One script per task: each new provider takes the next one.
    scripts = [
        [tool("c1", "write_file", path="a.txt", content="a\n"), "wrote a"],
        [tool("c2", "write_file", path="b.txt", content="b\n"), "wrote b"],
    ]
    monkeypatch.setattr(
        "forge.agent.runtime.create_provider",
        lambda config: FakeModelProvider(config, responses=scripts.pop(0)),
    )

    # "a" = allow write_file for the rest of the session; the second task must not ask again.
    result = runner.invoke(app, ["session"], input="first\na\nsecond\n/exit\n")

    assert result.output.count("Permission needed") == 1
    assert (workspace_root / "a.txt").exists()
    assert (workspace_root / "b.txt").exists()
