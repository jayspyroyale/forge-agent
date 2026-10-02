"""The interactive session started by running `forge` with no arguments.

Each line is a separate task with its own proof of work. One permission
engine is shared by the whole session, so "always allow this session"
answers carry over from task to task.
"""

from typing import Annotated

import typer
from pydantic import ValidationError
from rich.markup import escape

from forge import __version__
from forge.cli.approval import make_cli_approver
from forge.cli.commands.run import execute_task, verbosity_from
from forge.cli.output import Verbosity, console, fail
from forge.config import ForgeConfig
from forge.models.registry import default_registry
from forge.models.errors import ProviderNotFoundError
from forge.security.permissions import PermissionEngine

HELP = """\
Type a task and press Enter. Forge asks before writing files or running commands.
  /help      show this help
  /verbose   toggle detailed output
  /exit      leave (also /quit or Ctrl+D)"""


def session(
    provider: Annotated[str | None, typer.Option("--provider", "-p", help="Provider name.")] = None,
    model: Annotated[str | None, typer.Option("--model", "-m", help="Model name.")] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show tool output and more detail.")] = False,
    debug: Annotated[bool | None, typer.Option("--debug", help="Show everything, including tracebacks.")] = None,
) -> None:
    """Start an interactive session (what `forge` with no arguments does)."""
    try:
        config = ForgeConfig.from_env(provider=provider, model=model, debug=debug)
    except ValidationError as error:
        raise fail(str(error), prefix="Invalid configuration")

    verbosity: Verbosity = verbosity_from(verbose, config.debug)
    permissions = PermissionEngine(config.permissions, make_cli_approver())
    _print_header(config)

    while True:
        try:
            line = console.input("\n[bold cyan]>[/bold cyan] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            break
        if not line:
            continue
        if line in ("/exit", "/quit"):
            break
        if line == "/help":
            console.print(HELP)
            continue
        if line == "/verbose":
            verbosity = "normal" if verbosity != "normal" else "verbose"
            console.print(f"[dim]output: {verbosity}[/dim]")
            continue
        if line.startswith("/"):
            console.print(f"[yellow]Unknown command {escape(line)}. Type /help.[/yellow]")
            continue
        execute_task(config, line, permissions=permissions, verbosity=verbosity)

    console.print("[dim]Bye.[/dim]")


def _print_header(config: ForgeConfig) -> None:
    try:
        default_model = default_registry.get(config.provider).default_model
    except ProviderNotFoundError:
        default_model = None
    model = config.model or default_model or "(no model set)"
    policy = config.permissions
    console.print(f"[bold]Forge[/bold] [dim]{__version__}[/dim]")
    console.print(f"[dim]Model:[/dim]       {escape(config.provider)} · {escape(model)}")
    console.print(f"[dim]Workspace:[/dim]   {escape(str(config.workspace))}")
    console.print(
        f"[dim]Permissions:[/dim] read={policy.read} write={policy.write} "
        f"execute={policy.execute} dangerous={policy.dangerous}"
    )
    console.print("[dim]Type a task, /help for commands, /exit to quit.[/dim]")
