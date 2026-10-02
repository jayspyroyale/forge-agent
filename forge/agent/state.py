"""The data an agent carries from one step to the next."""

from pydantic import BaseModel, Field

from forge.models.types import Message


class AgentState(BaseModel):
    messages: list[Message] = Field(default_factory=list)
    step: int = 0
