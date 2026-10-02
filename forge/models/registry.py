"""Looks up model providers by name and creates them from config.

The rest of Forge never instantiates a provider class directly. It calls
`create_provider(config)`, and the registry picks the class named by
`config.model.provider`.
"""

from forge.config import ForgeConfig
from forge.models.base import ModelProvider
from forge.models.errors import ProviderNotFoundError
from forge.models.providers.fake import FakeModelProvider
from forge.models.providers.openai_provider import OllamaProvider, OpenAIProvider


class ProviderRegistry:
    """Maps provider names to provider classes."""

    def __init__(self) -> None:
        self._providers: dict[str, type[ModelProvider]] = {}

    def register(self, provider_class: type[ModelProvider]) -> None:
        name = provider_class.name
        if name in self._providers:
            raise ValueError(f"Model provider '{name}' is already registered")
        self._providers[name] = provider_class

    def get(self, name: str) -> type[ModelProvider]:
        try:
            return self._providers[name]
        except KeyError:
            available = ", ".join(self.names()) or "none"
            raise ProviderNotFoundError(
                f"Unknown model provider '{name}'. Available providers: {available}."
            ) from None

    def names(self) -> list[str]:
        return sorted(self._providers)

    def create(self, config: ForgeConfig) -> ModelProvider:
        provider_class = self.get(config.model.provider)
        return provider_class(config)


default_registry = ProviderRegistry()
default_registry.register(FakeModelProvider)
default_registry.register(OpenAIProvider)
default_registry.register(OllamaProvider)


def create_provider(
    config: ForgeConfig, registry: ProviderRegistry = default_registry
) -> ModelProvider:
    """Create the provider named in `config.model.provider`."""
    return registry.create(config)
