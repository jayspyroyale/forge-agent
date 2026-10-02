"""A collection of tools, looked up by name.

Registries are ordinary objects, not globals: each task (and later, each
exploration candidate) can have its own set of tools.
"""

from forge.models.types import ToolDefinition
from forge.tools.base import Tool


class ToolNotFoundError(LookupError):
    pass


class ToolRegistry:
    def __init__(self, tools: list[Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools or []:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool '{tool.name}' is already registered")
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        if name not in self._tools:
            raise ToolNotFoundError(f"No tool named '{name}'")
        del self._tools[name]

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError:
            raise ToolNotFoundError(f"No tool named '{name}'") from None

    def list_tools(self) -> list[Tool]:
        return [self._tools[name] for name in self.names()]

    def names(self) -> list[str]:
        return sorted(self._tools)

    def definitions(self) -> list[ToolDefinition]:
        """Neutral tool descriptions for model providers to translate."""
        return [tool.definition() for tool in self.list_tools()]

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)
