from pathlib import Path

import pytest
from pydantic import ValidationError

from forge.config import ConfigError, ForgeConfig, load_config, redact
from forge.config.profiles import BUILTIN_PROFILES

# --- Schema and defaults --------------------------------------------------------------


def test_defaults():
    config = ForgeConfig()
    assert config.workspace_root == Path.cwd().resolve()
    assert config.agent.max_steps == 20
    assert config.ui.debug is False
    assert config.profile == "balanced"


def test_model_setting_defaults():
    model = ForgeConfig().model
    assert model.provider == "openai"
    assert model.name is None
    assert model.base_url is None
    assert model.temperature is None
    assert model.timeout == 120.0


def test_max_steps_must_be_positive():
    with pytest.raises(ValidationError):
        ForgeConfig(agent={"max_steps": 0})


def test_temperature_must_be_in_range():
    with pytest.raises(ValidationError):
        ForgeConfig(model={"temperature": 3})


def test_unknown_settings_are_rejected():
    with pytest.raises(ValidationError):
        ForgeConfig(agent={"max_stepz": 5})


def test_config_has_no_secret_fields():
    from forge.config.loader import SECRET_KEY, flatten

    assert not [key for key in flatten(ForgeConfig().model_dump()) if SECRET_KEY.search(key.split(".")[-1])]


def test_exploration_settings_are_reserved_but_validated():
    config = ForgeConfig(exploration={"approaches": 3, "selection_mode": "recommend"})
    assert config.exploration.approaches == 3
    with pytest.raises(ValidationError):
        ForgeConfig(exploration={"approaches": 0})


# --- Backwards compatibility --------------------------------------------------------------


def test_legacy_flat_keywords_still_work(tmp_path):
    config = ForgeConfig(
        provider="ollama",
        model="llama3",
        base_url="http://box:11434/v1",
        temperature=0.5,
        timeout=30,
        max_steps=7,
        verification="off",
        verification_attempts=2,
        debug=True,
        workspace=tmp_path,
    )
    assert (config.model.provider, config.model.name, config.model.base_url) == ("ollama", "llama3", "http://box:11434/v1")
    assert (config.model.temperature, config.model.timeout) == (0.5, 30.0)
    assert (config.agent.max_steps, config.agent.verification, config.agent.verification_attempts) == (7, "off", 2)
    assert config.ui.debug is True
    assert config.workspace_root == tmp_path.resolve()


def test_legacy_and_nested_keywords_combine():
    config = ForgeConfig(provider="fake", model={"name": "x"})
    assert (config.model.provider, config.model.name) == ("fake", "x")


def test_from_env_reads_environment_variables(monkeypatch):
    monkeypatch.setenv("FORGE_PROVIDER", "ollama")
    monkeypatch.setenv("FORGE_MODEL", "llama3")
    monkeypatch.setenv("FORGE_TEMPERATURE", "0.5")
    monkeypatch.setenv("FORGE_TIMEOUT", "30")
    monkeypatch.setenv("FORGE_DEBUG", "true")

    config = ForgeConfig.from_env()

    assert config.model.provider == "ollama"
    assert config.model.name == "llama3"
    assert config.model.temperature == 0.5
    assert config.model.timeout == 30.0
    assert config.ui.debug is True


def test_from_env_overrides_win_and_none_is_ignored(monkeypatch):
    monkeypatch.setenv("FORGE_PROVIDER", "ollama")
    monkeypatch.setenv("FORGE_MODEL", "llama3")

    config = ForgeConfig.from_env(provider="fake", model=None)

    assert config.model.provider == "fake"
    assert config.model.name == "llama3"


# --- Layered loading ------------------------------------------------------------------------


@pytest.fixture
def layers(tmp_path, monkeypatch):
    """Paths for a user config (via FORGE_HOME) and a project config."""
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "project"
    (project / ".forge").mkdir(parents=True)
    monkeypatch.setenv("FORGE_HOME", str(home))
    return home / "config.toml", project / ".forge" / "config.toml", project


def test_missing_config_files_are_fine(layers):
    _, _, project = layers
    loaded = load_config(workspace=project)
    assert loaded.config.agent.max_steps == 20
    assert [f.exists for f in loaded.files] == [False, False]
    assert loaded.sources["agent.max_steps"] == "default"


def test_user_config(layers):
    user, _, project = layers
    user.write_text('[model]\nprovider = "ollama"\nname = "llama3"\n')
    loaded = load_config(workspace=project)
    assert loaded.config.model.provider == "ollama"
    assert loaded.sources["model.name"] == f"user:{user}"


def test_project_config_overrides_user(layers):
    user, project_file, project = layers
    user.write_text("[agent]\nmax_steps = 11\nverification_attempts = 4\n")
    project_file.write_text("[agent]\nmax_steps = 12\n")
    loaded = load_config(workspace=project)
    assert loaded.config.agent.max_steps == 12
    assert loaded.config.agent.verification_attempts == 4
    assert loaded.sources["agent.max_steps"].startswith("project:")
    assert loaded.sources["agent.verification_attempts"].startswith("user:")


def test_full_precedence_chain(layers, monkeypatch):
    user, project_file, project = layers
    user.write_text('[model]\nname = "from-user"\ntimeout = 10\n[agent]\nmax_steps = 11\n')
    project_file.write_text('[model]\nname = "from-project"\n[agent]\nmax_steps = 12\n')
    monkeypatch.setenv("FORGE_MODEL", "from-env")

    loaded = load_config(workspace=project, cli={"agent.max_steps": 99})

    config, sources = loaded.config, loaded.sources
    assert config.agent.max_steps == 99 and sources["agent.max_steps"] == "cli"
    assert config.model.name == "from-env" and sources["model.name"] == "env:FORGE_MODEL"
    assert config.model.timeout == 10 and sources["model.timeout"].startswith("user:")
    assert sources["model.provider"] == "default"


def test_cli_override_beats_everything(layers, monkeypatch):
    user, project_file, project = layers
    project_file.write_text('[model]\nprovider = "ollama"\n')
    monkeypatch.setenv("FORGE_PROVIDER", "openai")
    assert load_config(workspace=project, cli={"model.provider": "fake"}).config.model.provider == "fake"


def test_permissions_from_project_config(layers):
    _, project_file, project = layers
    project_file.write_text('[permissions]\nwrite = "allow"\nexecute = "deny"\n')
    policy = load_config(workspace=project).config.permissions
    assert (policy.write, policy.execute, policy.dangerous) == ("allow", "deny", "deny")


def test_workspace_extra_paths(layers):
    _, project_file, project = layers
    project_file.write_text('[workspace]\nprotected_paths = ["secrets"]\nignored_dirs = ["dist"]\n')
    from forge.agent.runtime import workspace_from_config

    workspace = workspace_from_config(load_config(workspace=project).config)
    assert workspace.is_protected("secrets/key.txt")
    assert workspace.is_ignored("dist/bundle.js")
    assert workspace.is_protected(".git/HEAD")


def test_invalid_toml(layers):
    _, project_file, project = layers
    project_file.write_text("[agent\nmax_steps = 5\n")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config(workspace=project)


def test_unknown_key_reports_its_file(layers):
    _, project_file, project = layers
    project_file.write_text("[agent]\nmax_stepz = 5\n")
    with pytest.raises(ConfigError, match=r"agent\.max_stepz: unknown setting \(from project:"):
        load_config(workspace=project)


def test_invalid_value_reports_its_source(layers, monkeypatch):
    _, _, project = layers
    monkeypatch.setenv("FORGE_TEMPERATURE", "hot")
    with pytest.raises(ConfigError, match=r"model\.temperature: .*\(from env:FORGE_TEMPERATURE\)"):
        load_config(workspace=project)


def test_secrets_in_project_config_are_rejected(layers):
    _, project_file, project = layers
    project_file.write_text('[model]\nprovider = "openai"\napi_key = "sk-not-real"\n')
    with pytest.raises(ConfigError, match="secrets must not be stored in project config"):
        load_config(workspace=project)


def test_secrets_in_user_config_are_rejected(layers):
    user, _, project = layers
    user.write_text('[model]\ntoken = "abc"\n')
    with pytest.raises(ConfigError, match="Use environment variables"):
        load_config(workspace=project)


def test_include_files_false_ignores_files(layers):
    _, project_file, project = layers
    project_file.write_text("[agent]\nmax_steps = 12\n")
    assert load_config(workspace=project, include_files=False).config.agent.max_steps == 20


# --- Profiles -------------------------------------------------------------------------------


def test_builtin_profiles_exist():
    assert {"cheap", "balanced", "production", "maximum-quality"} <= set(BUILTIN_PROFILES)


def test_profile_changes_settings(layers):
    _, _, project = layers
    loaded = load_config(workspace=project, cli={"profile": "cheap"})
    assert loaded.config.profile == "cheap"
    assert loaded.config.agent.max_steps == 10
    assert loaded.config.exploration.budget_usd == 0.10
    assert loaded.sources["agent.max_steps"] == "profile:cheap"


def test_maximum_quality_profile_sets_future_exploration_settings(layers):
    _, _, project = layers
    config = load_config(workspace=project, cli={"profile": "maximum-quality"}).config
    assert config.exploration.approaches == 3
    assert config.exploration.selection_mode == "assisted"  # "recommend" is still accepted as an alias
    assert config.agent.verification_attempts == 5


def test_explicit_settings_beat_the_profile(layers):
    _, project_file, project = layers
    project_file.write_text('profile = "cheap"\n[agent]\nmax_steps = 15\n')
    loaded = load_config(workspace=project)
    assert loaded.profile == "cheap"
    assert loaded.config.agent.max_steps == 15
    assert loaded.config.agent.verification_attempts == 1  # still from the profile
    assert loaded.sources["profile"].startswith("project:")


def test_custom_profile_in_config_file(layers):
    user, _, project = layers
    user.write_text('[profiles.tiny.agent]\nmax_steps = 3\n')
    config = load_config(workspace=project, cli={"profile": "tiny"}).config
    assert config.agent.max_steps == 3


def test_profile_from_environment(layers, monkeypatch):
    _, _, project = layers
    monkeypatch.setenv("FORGE_PROFILE", "production")
    loaded = load_config(workspace=project)
    assert loaded.profile == "production"
    assert loaded.sources["profile"] == "env:FORGE_PROFILE"


def test_unknown_profile(layers):
    _, _, project = layers
    with pytest.raises(ConfigError, match="Unknown profile 'turbo'"):
        load_config(workspace=project, cli={"profile": "turbo"})


# --- Redaction --------------------------------------------------------------------------------


def test_redaction():
    assert redact("model.api_key", "sk-123") == "***"
    assert redact("x.token", "abc") == "***"
    assert redact("model.base_url", "https://user:pass@example.com/v1") == "https://***@example.com/v1"
    assert redact("model.base_url", "http://localhost:11434/v1") == "http://localhost:11434/v1"
    assert redact("agent.max_steps", 5) == 5
