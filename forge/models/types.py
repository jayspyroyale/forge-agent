"""Forge's normalized model types.

These are the only shapes the rest of Forge sees. Each provider adapter
converts Forge types into its own request format and converts the provider's
reply back into a `ModelResponse`.
"""

from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

Role = Literal["system", "user", "assistant", "tool"]

FinishReason = Literal["stop", "tool_calls", "length", "content_filter", "other"]


class ToolCall(BaseModel):
    """A request from the model to run a tool."""

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class Message(BaseModel):
    """One message in a conversation.

    - Assistant messages may carry `tool_calls` the model asked for.
    - Tool messages carry a tool's result and the `tool_call_id` they answer.
    """

    role: Role
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None

    @classmethod
    def system(cls, content: str) -> Self:
        return cls(role="system", content=content)

    @classmethod
    def user(cls, content: str) -> Self:
        return cls(role="user", content=content)

    @classmethod
    def assistant(cls, content: str) -> Self:
        return cls(role="assistant", content=content)


class ToolDefinition(BaseModel):
    """Describes a tool to the model: what it is called, what it does, what it accepts.

    `parameters` is a JSON Schema object, the format every major provider uses.
    """

    name: str
    description: str
    parameters: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})


class Usage(BaseModel):
    """Token counts (and cost, when known) for one model call."""

    model_config = ConfigDict(allow_inf_nan=False)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _fill_total(self) -> Self:
        if self.total_tokens is None:
            self.total_tokens = self.input_tokens + self.output_tokens
        return self

    def __add__(self, other: "Usage") -> "Usage":
        """Combine usage from several calls. Cost stays unknown unless both sides know it."""
        cost = None
        if self.cost_usd is not None and other.cost_usd is not None:
            cost = self.cost_usd + other.cost_usd
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            total_tokens=(self.total_tokens or 0) + (other.total_tokens or 0),
            cost_usd=cost,
        )


class ModelResponse(BaseModel):
    """A model's reply, in the same shape no matter which provider produced it."""

    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    finish_reason: FinishReason = "stop"
    usage: Usage | None = None
    model: str | None = None
