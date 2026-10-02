"""The interface every model provider implements.

Forge talks to all AI providers (OpenAI, Anthropic, Gemini, local models, ...)
through this one interface, so the agent never depends on a specific vendor.
"""

from abc import ABC, abstractmethod
from typing import Any

from forge.config import ForgeConfig
from forge.models.errors import ProviderConfigError
from forge.models.types import Message, ModelResponse, ToolDefinition


class ModelProvider(ABC):
    """Base class for model provider adapters.

    Subclasses set the class attributes below and implement `generate`.
    """

    name: str
    description: str
    requires_api_key: bool = False
    default_model: str | None = None

    def __init__(self, config: ForgeConfig) -> None:
        self.config = config

    @property
    def model(self) -> str:
        """The model to use: the configured one, or this provider's default."""
        model = self.config.model.name or self.default_model
        if model is None:
            raise ProviderConfigError(
                f"The '{self.name}' provider needs a model name. "
                "Pass --model or set FORGE_MODEL."
            )
        return model

    @abstractmethod
    async def generate(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        **options: Any,
    ) -> ModelResponse:
        """Send the conversation to the model and return its normalized reply."""
