"""The tool abstraction.

A tool is an action Forge can perform when a model asks for it. Tools are
provider-independent: a tool describes itself with a neutral `ToolDefinition`
(name, description, JSON Schema), and model adapters translate that into
their own format.

Tools never run themselves in response to a model. The `ToolExecutor` looks
them up, validates arguments, checks permissions, and only then calls
`execute`.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar, Self

from pydantic import BaseModel, ConfigDict

from forge.config import ForgeConfig
from forge.models.types import ToolDefinition
from forge.workspace import Workspace


class ToolError(Exception):
    """An expected tool failure (missing file, bad input, ...). Becomes a failed ToolResult."""


class ToolResult(BaseModel):
    """The normalized outcome of running a tool."""

    success: bool
    output: str = ""
    error: str | None = None
    metadata: dict[str, Any] = {}

    @classmethod
    def ok(cls, output: str = "", **metadata: Any) -> Self:
        return cls(success=True, output=output, metadata=metadata)

    @classmethod
    def fail(cls, error: str, output: str = "", **metadata: Any) -> Self:
        return cls(success=False, error=error, output=output, metadata=metadata)

    def to_model_content(self) -> str:
        """The text sent back to the model as the tool message."""
        if self.success:
            return self.output or "(no output)"
        text = f"Error: {self.error}"
        if self.output:
            text += f"\n{self.output}"
        return text


class ToolArgs(BaseModel):
    """Base class for tool argument models. Unknown arguments are rejected."""

    model_config = ConfigDict(extra="forbid")


class NoArgs(ToolArgs):
    pass


@dataclass
class ToolContext:
    """Everything a tool may use while running. Created once per task."""

    workspace: Workspace
    config: ForgeConfig = field(default_factory=ForgeConfig)


class Tool(ABC):
    """Base class for all tools."""

    name: ClassVar[str]
    description: ClassVar[str]
    Args: ClassVar[type[ToolArgs]] = NoArgs

    @abstractmethod
    def execute(self, args: Any, context: ToolContext) -> ToolResult:
        """Perform the action. `args` is an instance of `self.Args`, already validated."""

    def definition(self) -> ToolDefinition:
        schema = self.Args.model_json_schema()
        schema.pop("title", None)
        return ToolDefinition(name=self.name, description=self.description, parameters=schema)
