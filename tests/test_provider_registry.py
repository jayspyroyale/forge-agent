import asyncio

import pytest

from forge.config import ForgeConfig
from forge.models.errors import ProviderNotFoundError
from forge.models.providers.fake import FakeModelProvider
from forge.models.providers.openai_provider import OllamaProvider, OpenAIProvider
from forge.models.registry import ProviderRegistry, create_provider, default_registry
from forge.models.types import Message


def test_default_registry_lists_builtin_providers():
    assert default_registry.names() == ["fake", "ollama", "openai"]


def test_get_returns_provider_class():
    assert default_registry.get("fake") is FakeModelProvider
    assert default_registry.get("openai") is OpenAIProvider
    assert default_registry.get("ollama") is OllamaProvider


def test_unknown_provider_raises_clear_error():
    with pytest.raises(ProviderNotFoundError, match="Available providers: fake, ollama, openai"):
        default_registry.get("does-not-exist")


def test_create_provider_from_config():
    provider = create_provider(ForgeConfig(provider="fake", model="custom-fake"))

    assert isinstance(provider, FakeModelProvider)
    assert provider.model == "custom-fake"
    response = asyncio.run(provider.generate([Message.user("Hi")]))
    assert response.content == "[fake] Hi"


def test_create_provider_with_unknown_name():
    with pytest.raises(ProviderNotFoundError):
        create_provider(ForgeConfig(provider="nope"))


def test_registering_the_same_name_twice_fails():
    registry = ProviderRegistry()
    registry.register(FakeModelProvider)
    with pytest.raises(ValueError):
        registry.register(FakeModelProvider)


def test_empty_registry_says_none_available():
    with pytest.raises(ProviderNotFoundError, match="Available providers: none"):
        ProviderRegistry().get("fake")
