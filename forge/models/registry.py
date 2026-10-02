"""A simple lookup table of model providers by name."""

from forge.models.base import ModelProvider


class ModelRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, ModelProvider] = {}

    def register(self, provider: ModelProvider) -> None:
        if provider.name in self._providers:
            raise ValueError(f"Model provider '{provider.name}' is already registered")
        self._providers[provider.name] = provider

    def get(self, name: str) -> ModelProvider:
        try:
            return self._providers[name]
        except KeyError:
            raise KeyError(f"No model provider named '{name}'") from None

    def names(self) -> list[str]:
        return sorted(self._providers)
