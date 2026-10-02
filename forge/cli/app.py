"""The root `forge` command and how arguments are routed to it.

    forge                      -> interactive session
    forge "Fix the tests"      -> forge run "Fix the tests"
    forge -p fake "task"       -> forge run -p fake "task"
    forge doctor / run / ...   -> that command
"""

import sys
from typing import Annotated

import typer

from forge import __version__
from forge.cli.commands.benchmark import benchmark_app
from forge.cli.commands.config import config_app
from forge.cli.commands.doctor import doctor
from forge.cli.commands.explore import explore, explorations_app
from forge.cli.commands.mcp import mcp_app
from forge.cli.commands.memory import memory_app
from forge.cli.commands.models import ask, models_app
from forge.cli.commands.run import run
from forge.cli.commands.tasks import tasks_app
from forge.cli.commands.tools import tools_app
from forge.cli.interactive import session
from forge.cli.output import console, make_streams_safe

app = typer.Typer(
    help=(
        "Forge: a lightweight, model-agnostic runtime for AI coding agents.\n\n"
        'Run `forge` for an interactive session, or `forge "your task"` for a single task.'
    ),
    no_args_is_help=True,
)


def _show_version(value: bool) -> None:
    if value:
        console.print(f"forge {__version__}")
        raise typer.Exit()


@app.callback()
def main_callback(
    ctx: typer.Context,
    version: Annotated[
        bool,
        typer.Option("--version", help="Show the Forge version and exit.", callback=_show_version, is_eager=True),
    ] = False,
    profile: Annotated[
        str | None,
        typer.Option("--profile", help="Configuration profile (cheap, balanced, production, maximum-quality, or your own)."),
    ] = None,
) -> None:
    """Forge: a lightweight, model-agnostic runtime for AI coding agents."""
    ctx.obj = {"profile": profile}


app.command()(run)
app.command()(explore)
app.command(hidden=True)(session)
app.command()(ask)
app.command()(doctor)
app.add_typer(models_app, name="models")
app.add_typer(tools_app, name="tools")
app.add_typer(tasks_app, name="tasks")
app.add_typer(config_app, name="config")
app.add_typer(memory_app, name="memory")
app.add_typer(mcp_app, name="mcp")
app.add_typer(explorations_app, name="explorations")
app.add_typer(benchmark_app, name="benchmark")

ROOT_OPTIONS = frozenset({"--help", "-h", "--version", "--install-completion", "--show-completion"})
ROOT_VALUE_OPTIONS = frozenset({"--profile"})  # root options followed by a value


def command_names() -> set[str]:
    return set(typer.main.get_command(app).commands)  # type: ignore[attr-defined]


def route(args: list[str]) -> list[str]:
    """Rewrite argv so the shortcuts above work. Pure function: easy to test."""
    head, rest = _split_root_options(args)
    if not rest:
        return [*head, "session"]
    first = rest[0]
    if first in ROOT_OPTIONS or first in command_names():
        return [*head, *rest]
    # Anything else is a task (possibly preceded by run options such as -p).
    return [*head, "run", *rest]


def _split_root_options(args: list[str]) -> tuple[list[str], list[str]]:
    """Separate leading options that belong to `forge` itself, like `--profile production`."""
    index = 0
    while index < len(args):
        token = args[index]
        if token in ROOT_VALUE_OPTIONS:
            index += 2
        elif any(token.startswith(option + "=") for option in ROOT_VALUE_OPTIONS):
            index += 1
        else:
            break
    return args[:index], args[index:]


def main(argv: list[str] | None = None) -> None:
    """Console-script entry point (see [project.scripts] in pyproject.toml)."""
    make_streams_safe()
    args = list(sys.argv[1:] if argv is None else argv)
    app(args=route(args), prog_name="forge")
