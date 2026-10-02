"""`forge config show | paths`: see the effective configuration and where each value came from."""

import json
from typing import Annotated

import typer
from rich.markup import escape
from rich.table import Table

from forge.cli.output import console
from forge.cli.settings import load_cli_config
from forge.config.loader import flatten, redact
from forge.config.profiles import BUILTIN_PROFILES

config_app = typer.Typer(help="Show Forge's effective configuration.", no_args_is_help=True)


@config_app.command("show")
def show_config(
    ctx: typer.Context,
    profile: Annotated[str | None, typer.Option("--profile", help="Show the config for this profile.")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print as JSON.")] = False,
) -> None:
    """Show every setting, its value, and which layer set it. Secrets are never shown."""
    loaded = load_cli_config(ctx, profile=profile)
    values = flatten(loaded.config.model_dump(mode="json"))
    rows = [(key, redact(key, value), _short_source(loaded.sources.get(key, "default"))) for key, value in values.items()]

    if as_json:
        console.print_json(json.dumps({key: {"value": value, "source": source} for key, value, source in rows}))
        return
    table = Table(title=f"Forge configuration (profile: {loaded.profile})")
    table.add_column("Setting", style="bold")
    table.add_column("Value")
    table.add_column("Source", style="dim")
    for key, value, source in rows:
        table.add_row(key, escape(_display(value)), escape(source))
    console.print(table)


@config_app.command("paths")
def show_paths(ctx: typer.Context) -> None:
    """Show where Forge looks for configuration files."""
    loaded = load_cli_config(ctx)
    for config_file in loaded.files:
        state = "[green]found[/green]" if config_file.exists else "[dim]not present[/dim]"
        console.print(f"{config_file.layer:8} {escape(str(config_file.path))}  {state}")
    console.print(f"\n[dim]Built-in profiles: {', '.join(BUILTIN_PROFILES)} (active: {loaded.profile})[/dim]")
    console.print("[dim]Precedence: defaults < profile < user < project < environment (FORGE_*) < command line[/dim]")


def _display(value) -> str:
    if value is None:
        return "-"
    if isinstance(value, (list, dict)):
        return json.dumps(value)
    return str(value)


def _short_source(source: str) -> str:
    layer, _, path = source.partition(":")
    if layer in ("user", "project") and path:
        return f"{layer} config"
    return source
