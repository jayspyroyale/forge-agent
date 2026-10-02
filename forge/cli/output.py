"""Shared terminal output helpers for the CLI."""

import json
import sys
from typing import Any, Literal

import typer
from rich.console import Console
from rich.markup import escape

Verbosity = Literal["normal", "verbose", "debug"]

console = Console()
error_console = Console(stderr=True)


def make_streams_safe() -> None:
    """Never crash on output: terminals that cannot show a character (✓, ─) get a '?' instead."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="replace")
            except (ValueError, OSError):
                pass


def print_plain(text: str) -> None:
    """Print text exactly as given: no markup, no highlighting, no re-wrapping."""
    console.print(text, markup=False, highlight=False, soft_wrap=True)


def fail(message: str, *, debug: bool = False, code: int = 1, prefix: str = "Error") -> typer.Exit:
    """Print a one-line error (plus a traceback in debug mode) and return the Exit to raise."""
    if debug:
        error_console.print_exception()
    error_console.print(f"[red]{escape(prefix)}:[/red] {escape(message)}")
    return typer.Exit(code=code)


def short_value(value: Any, limit: int = 60) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = text.replace("\r", "").replace("\n", "⏎")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def short_arguments(arguments: dict[str, Any], limit: int = 110) -> str:
    """Tool arguments as a compact `key=value` line for display."""
    text = " ".join(f"{key}={short_value(value)}" for key, value in arguments.items())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def short_json(value: Any, limit: int = 100) -> str:
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"
