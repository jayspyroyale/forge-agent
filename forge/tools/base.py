"""The interface every tool will implement.

A tool is an action the agent can ask Forge to perform, such as reading a
file or running a command. No real tools are implemented in Phase 1.
"""

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel


class ToolResult(BaseModel):
    """What a tool returns to the agent."""

    success: bool
    output: str = ""


class Tool(ABC):
    """Base class for tools."""

    name: str
    description: str

    @abstractmethod
    def run(self, **kwargs: Any) -> ToolResult:
        """Perform the tool's action."""
