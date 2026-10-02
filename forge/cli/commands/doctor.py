"""`forge doctor`: local environment checks."""

import typer
from rich.markup import escape

from forge.cli.output import console
from forge.doctor import run_all_checks


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
