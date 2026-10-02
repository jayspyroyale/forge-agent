import json
import os

import pytest
from typer.testing import CliRunner

from forge.cli import app, route
from forge.models.providers.fake import FakeModelProvider
from forge.models.types import ModelResponse, ToolCall

runner = CliRunner()


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "project"
    (root / ".forge").mkdir(parents=True)
    (root / "allowed.txt").write_text("hello\n")
    monkeypatch.chdir(root)
    return root


def show(*extra):
    result = runner.invoke(app, [*extra, "config", "show", "--json"])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_config_show_lists_values_and_sources(project):
    result = runner.invoke(app, ["config", "show"])
    assert result.exit_code == 0
    assert "agent.max_steps" in result.output
    assert "default" in result.output
    data = show()
    assert data["agent.max_steps"] == {"value": 20, "source": "default"}


def test_config_show_reflects_project_config(project):
    (project / ".forge" / "config.toml").write_text('[model]\nprovider = "fake"\n')
    assert show()["model.provider"] == {"value": "fake", "source": "project config"}


def test_config_show_reflects_environment(project, monkeypatch):
    monkeypatch.setenv("FORGE_MAX_STEPS", "7")
    assert show()["agent.max_steps"] == {"value": 7, "source": "env:FORGE_MAX_STEPS"}


def test_global_profile_option(project):
    data = show("--profile", "cheap")
    assert data["profile"]["value"] == "cheap"
    assert data["agent.max_steps"] == {"value": 10, "source": "profile:cheap"}


def test_config_show_never_prints_credentials(project):
    (project / ".forge" / "config.toml").write_text('[model]\nbase_url = "https://alice:hunter2@llm.example.com/v1"\n')
    result = runner.invoke(app, ["config", "show"])
    assert "hunter2" not in result.output
    assert result.exit_code == 1
    assert "secrets must not be stored" in result.output


def test_config_paths(project):
    result = runner.invoke(app, ["config", "paths"])
    assert result.exit_code == 0
    assert "user" in result.output and "project" in result.output
    assert os.environ["FORGE_HOME"] in result.output.replace("\n", "")
    assert "Precedence" in result.output


def test_invalid_project_config_is_a_clear_error(project):
    (project / ".forge" / "config.toml").write_text("[agent]\nmax_stepz = 5\n")
    result = runner.invoke(app, ["run", "task"])
    assert result.exit_code == 1
    assert "agent.max_stepz: unknown setting" in result.output
    assert "Traceback" not in result.output


def test_secret_in_project_config_is_refused(project):
    (project / ".forge" / "config.toml").write_text('[model]\napi_key = "sk-not-real"\n')
    result = runner.invoke(app, ["config", "show"])
    assert result.exit_code == 1
    assert "secrets must not be stored" in result.output
    assert "sk-not-real" not in result.output


def test_unknown_profile_error(project):
    result = runner.invoke(app, ["--profile", "turbo", "config", "show"])
    assert result.exit_code == 1
    assert "Unknown profile 'turbo'" in result.output


def test_run_uses_project_config_and_cli_overrides(project, monkeypatch):
    (project / ".forge" / "config.toml").write_text('[model]\nprovider = "fake"\n[agent]\nmax_steps = 50\n')
    seen = {}

    def provider(config):
        seen["config"] = config
        return FakeModelProvider(
            config, responses=[ModelResponse(tool_calls=[ToolCall(id=f"c{n}", name="list_files")]) for n in range(5)]
        )

    monkeypatch.setattr("forge.agent.runtime.create_provider", provider)

    result = runner.invoke(app, ["run", "--max-steps", "2", "loop"])

    assert seen["config"].model.provider == "fake"
    assert seen["config"].agent.max_steps == 2
    assert "reached the step limit" in result.output


def test_no_verify_flag(project, monkeypatch):
    seen = {}

    def provider(config):
        seen["config"] = config
        return FakeModelProvider(config, responses=["done"])

    monkeypatch.setattr("forge.agent.runtime.create_provider", provider)
    runner.invoke(app, ["run", "--no-verify", "task"])
    assert seen["config"].agent.verification == "off"


def test_profile_reaches_the_runtime(project, monkeypatch):
    seen = {}

    def provider(config):
        seen["config"] = config
        return FakeModelProvider(config, responses=["done"])

    monkeypatch.setattr("forge.agent.runtime.create_provider", provider)
    result = runner.invoke(app, route(["--profile", "maximum-quality", "a task"]))
    assert result.exit_code == 0, result.output
    assert seen["config"].agent.max_steps == 50


@pytest.mark.parametrize(
    ("argv", "routed"),
    [
        (["--profile", "cheap"], ["--profile", "cheap", "session"]),
        (["--profile", "cheap", "task"], ["--profile", "cheap", "run", "task"]),
        (["--profile=cheap", "doctor"], ["--profile=cheap", "doctor"]),
        (["--profile", "cheap", "config", "show"], ["--profile", "cheap", "config", "show"]),
    ],
)
def test_routing_with_global_profile(argv, routed):
    assert route(argv) == routed
