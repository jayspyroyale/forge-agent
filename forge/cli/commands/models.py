"""`forge models list` and `forge ask`: talk to a model directly, without tools."""

import asyncio
from typing import Annotated

import typer
from pydantic import ValidationError
from rich.markup import escape
from rich.table import Table

from forge.cli.output import console, fail, print_plain
from forge.config import ForgeConfig
from forge.models.errors import ModelError
from forge.models.registry import create_provider, default_registry
from forge.models.types import Message

models_app = typer.Typer(help="Inspect available model providers.", no_args_is_help=True)


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


def ask(
    prompt: Annotated[str, typer.Argument(help="The prompt to send to the model.")],
    provider: Annotated[
        str | None, typer.Option("--provider", "-p", help="Provider name (see `forge models list`).")
    ] = None,
    model: Annotated[str | None, typer.Option("--model", "-m", help="Model name.")] = None,
    base_url: Annotated[
        str | None, typer.Option("--base-url", help="Server URL for OpenAI-compatible providers.")
    ] = None,
    debug: Annotated[bool | None, typer.Option("--debug", help="Show full error tracebacks.")] = None,
) -> None:
    """Send one prompt to a model and print its reply. No tools, no agent loop."""
    try:
        config = ForgeConfig.from_env(provider=provider, model=model, base_url=base_url, debug=debug)
    except ValidationError as error:
        raise fail(str(error), prefix="Invalid configuration")

    try:
        model_provider = create_provider(config)
        response = asyncio.run(model_provider.generate([Message.user(prompt)]))
    except ModelError as error:
        raise fail(str(error), debug=config.debug)

    print_plain(response.content)
    footer = f"{config.provider} · {response.model or model_provider.model}"
    if response.usage is not None:
        footer += f" · {response.usage.total_tokens} tokens"
    console.print(f"[dim]{escape(footer)}[/dim]")
