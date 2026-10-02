"""Events the agent emits while it works.

The agent never prints. Anything that wants to show progress (the CLI, a log
file, a future exploration dashboard) passes an `on_event` callback and
decides for itself what to display.
"""

from collections.abc import Callable

from pydantic import BaseModel

from forge.models.types import ModelResponse, ToolCall
from forge.tools.base import ToolResult


class AgentEvent(BaseModel):
    pass


class TaskStarted(AgentEvent):
    task: str


class ModelRequested(AgentEvent):
    step: int
    message_count: int


class ModelResponded(AgentEvent):
    step: int
    response: ModelResponse


class ToolStarted(AgentEvent):
    step: int
    call: ToolCall


class ToolFinished(AgentEvent):
    step: int
    call: ToolCall
    result: ToolResult


class AgentFinished(AgentEvent):
    status: str
    steps: int
    final_answer: str | None = None
    error: str | None = None


EventHandler = Callable[[AgentEvent], None]
