"""Loading configuration for CLI commands: files, environment, then command-line options."""

from typing import Any

import typer

from forge.cli.output import fail
from forge.config import ConfigError, LoadedConfig, load_config


def global_profile(ctx: typer.Context | None) -> str | None:
    """The value of `forge --profile NAME ...`, if given."""
    if ctx is None:
        return None
    root = ctx.find_root()
    return (root.obj or {}).get("profile") if root is not None else None


def load_cli_config(ctx: typer.Context | None, **cli: Any) -> LoadedConfig:
    """Load layered config. `cli` uses dotted keys (`agent.max_steps=5`); None values are ignored."""
    cli = {key: value for key, value in cli.items() if value is not None}
    if "profile" not in cli and global_profile(ctx):
        cli["profile"] = global_profile(ctx)
    try:
        return load_config(cli=cli)
    except ConfigError as error:
        raise fail(str(error))
