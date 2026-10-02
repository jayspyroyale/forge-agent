"""Forge's built-in tools."""

from forge.tools.builtin.editing import EditFile, WriteFile
from forge.tools.builtin.filesystem import CurrentDirectory, FileExists, ListFiles, ReadFile, SearchText
from forge.tools.registry import ToolRegistry


def create_default_tools() -> ToolRegistry:
    """A fresh registry containing every built-in tool."""
    return ToolRegistry(
        [
            CurrentDirectory(),
            ListFiles(),
            ReadFile(),
            FileExists(),
            SearchText(),
            WriteFile(),
            EditFile(),
        ]
    )
