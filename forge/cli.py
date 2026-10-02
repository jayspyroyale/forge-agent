"""Forge command-line interface.

This module only parses commands and displays output. Any real work is done
by other modules (for example `forge.doctor`), which keeps the CLI thin and
the logic testable on its own.
"""

import asyncio
from typing import Annotated

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from forge import __version__
from forge.config import ForgeConfig
from forge.doctor import run_all_checks
from forge.models.errors import ModelError
from forge.models.registry import create_provider, default_registry
from forge.models.types import Message

app = typer.Typer(
    help="Forge: a lightweight, model-agnostic runtime for AI coding agents.",
    no_args_is_help=True,
)
models_app = typer.Typer(help="Inspect available model providers.", no_args_is_help=True)
app.add_typer(models_app, name="models")

console = Console()
error_console = Console(stderr=True)


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


@models_app.command("list")
def list_models() -> None:
    """List the model providers Forge knows about."""
    table = Table(title="Model providers")
    table.add_column("Name", style="bold")
    table.add_column("Default model")
    table.add_column("API key")
    table.add_column("Description")

    for name in default_registry.names():
        provider_class = default_registry.get(name)
        table.add_row(
            name,
            provider_class.default_model or "-",
            "required" if provider_class.requires_api_key else "not needed",
            provider_class.description,
        )
    console.print(table)


@app.command()
def ask(
    prompt: Annotated[str, typer.Argument(help="The prompt to send to the model.")],
    provider: Annotated[
        str | None, typer.Option("--provider", "-p", help="Provider name (see `forge models list`).")
    ] = None,
    model: Annotated[str | None, typer.Option("--model", "-m", help="Model name.")] = None,
    base_url: Annotated[
        str | None, typer.Option("--base-url", help="Server URL for OpenAI-compatible providers.")
    ] = None,
    debug: Annotated[
        bool | None, typer.Option("--debug", help="Show full error tracebacks.")
    ] = None,
) -> None:
    """Send one prompt to a model and print its reply. No tools, no agent loop."""
    try:
        config = ForgeConfig.from_env(
            provider=provider, model=model, base_url=base_url, debug=debug
        )
    except ValidationError as error:
        error_console.print(f"[red]Invalid configuration:[/red] {escape(str(error))}")
        raise typer.Exit(code=1)

    try:
        model_provider = create_provider(config)
        response = asyncio.run(model_provider.generate([Message.user(prompt)]))
    except ModelError as error:
        if config.debug:
            error_console.print_exception()
        error_console.print(f"[red]Error:[/red] {escape(str(error))}")
        raise typer.Exit(code=1)

    console.print(response.content, markup=False, highlight=False)

    footer = f"{config.provider} · {response.model or model_provider.model}"
    if response.usage is not None:
        footer += f" · {response.usage.total_tokens} tokens"
    console.print(f"[dim]{escape(footer)}[/dim]")
