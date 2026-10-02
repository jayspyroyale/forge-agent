"""The data an agent carries through a task."""

from enum import StrEnum

from pydantic import BaseModel, Field

from forge.models.types import Message, ToolCall, Usage
from forge.tools.base import ToolResult


class AgentStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"  # the model gave a final answer
    MAX_STEPS = "max_steps"  # stopped by the step limit
    FAILED = "failed"  # stopped by an error (for example the model provider failed)


class ToolExecution(BaseModel):
    """One tool call the agent made, and what came back."""

    step: int
    call: ToolCall
    result: ToolResult


class AgentState(BaseModel):
    task: str
    messages: list[Message] = Field(default_factory=list)
    step: int = 0
    status: AgentStatus = AgentStatus.RUNNING
    tool_history: list[ToolExecution] = Field(default_factory=list)
    final_answer: str | None = None
    error: str | None = None
    # Total across all model calls; None until a provider reports usage.
    usage: Usage | None = None
