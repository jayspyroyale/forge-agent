from pathlib import Path

import pytest
from pydantic import ValidationError

from forge.config import ForgeConfig


def test_defaults():
    config = ForgeConfig()
    assert config.workspace == Path.cwd()
    assert config.max_steps == 20
    assert config.debug is False


def test_max_steps_must_be_positive():
    with pytest.raises(ValidationError):
        ForgeConfig(max_steps=0)


def test_model_setting_defaults():
    config = ForgeConfig()
    assert config.provider == "openai"
    assert config.model is None
    assert config.base_url is None
    assert config.temperature is None
    assert config.timeout == 120.0


def test_temperature_must_be_in_range():
    with pytest.raises(ValidationError):
        ForgeConfig(temperature=3)


def test_from_env_reads_environment_variables(monkeypatch):
    monkeypatch.setenv("FORGE_PROVIDER", "ollama")
    monkeypatch.setenv("FORGE_MODEL", "llama3")
    monkeypatch.setenv("FORGE_TEMPERATURE", "0.5")
    monkeypatch.setenv("FORGE_TIMEOUT", "30")
    monkeypatch.setenv("FORGE_DEBUG", "true")

    config = ForgeConfig.from_env()

    assert config.provider == "ollama"
    assert config.model == "llama3"
    assert config.temperature == 0.5
    assert config.timeout == 30.0
    assert config.debug is True


def test_from_env_overrides_win_and_none_is_ignored(monkeypatch):
    monkeypatch.setenv("FORGE_PROVIDER", "ollama")
    monkeypatch.setenv("FORGE_MODEL", "llama3")

    config = ForgeConfig.from_env(provider="fake", model=None)

    assert config.provider == "fake"
    assert config.model == "llama3"


def test_config_has_no_api_key_field():
    assert not any("key" in field for field in ForgeConfig.model_fields)
