import re

from typer.testing import CliRunner

from forge import __version__
from forge.cli import app

runner = CliRunner()


def test_help():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "doctor" in result.output
    assert "--version" in re.sub(r"\x1b\[[0-9;]*m", "", result.output)


def test_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_doctor_succeeds():
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "Forge Doctor" in result.output
    assert "Everything looks good." in result.output


def test_models_list_shows_builtin_providers():
    result = runner.invoke(app, ["models", "list"])
    assert result.exit_code == 0
    for name in ["fake", "openai", "ollama"]:
        assert name in result.output


def test_ask_with_fake_provider():
    result = runner.invoke(app, ["ask", "--provider", "fake", "Reply with exactly FORGE_OK"])
    assert result.exit_code == 0
    assert "[fake] Reply with exactly FORGE_OK" in result.output
    assert "fake · fake-model" in result.output


def test_ask_uses_provider_from_environment(monkeypatch):
    monkeypatch.setenv("FORGE_PROVIDER", "fake")
    result = runner.invoke(app, ["ask", "hello"])
    assert result.exit_code == 0
    assert "[fake] hello" in result.output


def test_ask_without_api_key_shows_clear_error():
    # conftest.py removes OPENAI_API_KEY, so the default "openai" provider has no key.
    result = runner.invoke(app, ["ask", "hello"])
    assert result.exit_code == 1
    assert "OPENAI_API_KEY" in result.output
    assert "Traceback" not in result.output


def test_ask_with_debug_shows_traceback():
    result = runner.invoke(app, ["ask", "--debug", "hello"])
    assert result.exit_code == 1
    assert "Traceback" in result.output


def test_ask_with_unknown_provider():
    result = runner.invoke(app, ["ask", "--provider", "nope", "hello"])
    assert result.exit_code == 1
    assert "Unknown model provider 'nope'" in result.output


def test_ask_with_invalid_environment_value(monkeypatch):
    monkeypatch.setenv("FORGE_TEMPERATURE", "hot")
    result = runner.invoke(app, ["ask", "--provider", "fake", "hello"])
    assert result.exit_code == 1
    assert "Invalid configuration" in result.output


# --- forge run ----------------------------------------------------------------


def _script_provider(monkeypatch, responses):
    """Make `forge run` use a scripted fake provider instead of the configured one."""
    from forge.models.providers.fake import FakeModelProvider

    monkeypatch.setattr(
        "forge.agent.runtime.create_provider",
        lambda config: FakeModelProvider(config, responses=list(responses)),
    )


def test_run_with_fake_provider_echo(workspace_root, monkeypatch):
    monkeypatch.chdir(workspace_root)
    result = runner.invoke(app, ["run", "-p", "fake", "hello agent"])
    assert result.exit_code == 0
    assert "[fake] hello agent" in result.output
    assert "Completed" in result.output


def test_run_with_scripted_tool_call(workspace_root, monkeypatch):
    from forge.models.types import ModelResponse, ToolCall

    monkeypatch.chdir(workspace_root)
    _script_provider(
        monkeypatch,
        [
            ModelResponse(tool_calls=[ToolCall(id="c1", name="read_file", arguments={"path": "allowed.txt"})]),
            "allowed.txt says hello.",
        ],
    )

    result = runner.invoke(app, ["run", "Read allowed.txt"])

    assert result.exit_code == 0
    assert "read_file" in result.output
    assert "allowed.txt says hello." in result.output
    assert "Completed in 2 steps" in result.output


def test_run_reports_max_steps(workspace_root, monkeypatch):
    from forge.models.types import ModelResponse, ToolCall

    monkeypatch.chdir(workspace_root)
    _script_provider(
        monkeypatch,
        [ModelResponse(tool_calls=[ToolCall(id=f"c{n}", name="list_files")]) for n in range(5)],
    )

    result = runner.invoke(app, ["run", "--max-steps", "2", "loop"])

    assert result.exit_code == 1
    assert "max_steps" in result.output


def test_run_without_api_key_shows_clear_error(workspace_root, monkeypatch):
    monkeypatch.chdir(workspace_root)
    result = runner.invoke(app, ["run", "task"])
    assert result.exit_code == 1
    assert "OPENAI_API_KEY" in result.output


def test_run_shows_verification_evidence(calculator_project, monkeypatch):
    from forge.models.types import ModelResponse, ToolCall

    monkeypatch.chdir(calculator_project)
    fix = ToolCall(
        id="c1",
        name="edit_file",
        arguments={
            "path": "calculator.py",
            "old_text": "return a + b  # BUG: should be a * b",
            "new_text": "return a * b",
        },
    )
    _script_provider(monkeypatch, [ModelResponse(tool_calls=[fix]), "Fixed multiply."])

    result = runner.invoke(app, ["run", "--yes", "Fix multiply"])

    assert result.exit_code == 0, result.output
    assert "verifying pytest" in result.output
    assert "Changed:" in result.output
    assert "calculator.py +1 -1" in result.output
    assert "✓ test" in result.output
    assert "pytest passed" in result.output
    assert "- lint" in result.output
