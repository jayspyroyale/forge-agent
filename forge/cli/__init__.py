"""Forge command-line interface (Typer + Rich).

This package only parses commands and displays output. All real work happens
in the runtime (`forge.agent.runtime`, `forge.tools`, `forge.security`, ...).
"""

from forge.cli.app import app, main, route

__all__ = ["app", "main", "route"]
