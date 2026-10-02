"""Forge command-line interface.

This module only parses commands and displays output. Any real work is done
by other modules (for example `forge.doctor`), which keeps the CLI thin and
the logic testable on its own.
"""

from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape

from forge import __version__
from forge.doctor import run_all_checks

app = typer.Typer(
    help="Forge: a lightweight, model-agnostic runtime for AI coding agents.",
    no_args_is_help=True,
)
console = Console()


def _show_version(value: bool) -> None:
    if value:
        console.print(f"forge {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            help="Show the Forge version and exit.",
            callback=_show_version,
            is_eager=True,
        ),
    ] = False,
) -> None:
    """Forge: a lightweight, model-agnostic runtime for AI coding agents."""


@app.command()
def doctor() -> None:
    """Check that the local environment is ready for Forge."""
    console.print("[bold]Forge Doctor[/bold]\n")

    results = run_all_checks()
    for result in results:
        mark = "[green]✓[/green]" if result.passed else "[red]✗[/red]"
        line = f"{mark} {escape(result.name)}"
        if result.detail:
            line += f" [dim]({escape(result.detail)})[/dim]"
        console.print(line)

    console.print()
    if all(result.passed for result in results):
        console.print("[green]Everything looks good.[/green]")
    else:
        console.print("[red]Some checks failed. See the details above.[/red]")
        raise typer.Exit(code=1)
