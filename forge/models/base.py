"""The interface every model provider will implement.

Forge talks to all AI providers (OpenAI, Anthropic, Gemini, local models, ...)
through this one interface, so the agent never depends on a specific vendor.
No providers are implemented in Phase 1.
"""

from abc import ABC, abstractmethod
from typing import Literal

from pydantic import BaseModel


class Message(BaseModel):
    """One message in a conversation with a model."""

    role: Literal["system", "user", "assistant"]
    content: str


class ModelProvider(ABC):
    """Base class for model provider adapters."""

    name: str

    @abstractmethod
    def complete(self, messages: list[Message]) -> Message:
        """Send the conversation to the model and return its reply."""
