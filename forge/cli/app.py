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
from forge.cli.commands.doctor import doctor
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
    version: Annotated[
        bool,
        typer.Option("--version", help="Show the Forge version and exit.", callback=_show_version, is_eager=True),
    ] = False,
) -> None:
    """Forge: a lightweight, model-agnostic runtime for AI coding agents."""


app.command()(run)
app.command(hidden=True)(session)
app.command()(ask)
app.command()(doctor)
app.add_typer(models_app, name="models")
app.add_typer(tools_app, name="tools")
app.add_typer(tasks_app, name="tasks")

ROOT_OPTIONS = frozenset({"--help", "-h", "--version", "--install-completion", "--show-completion"})


def command_names() -> set[str]:
    return set(typer.main.get_command(app).commands)  # type: ignore[attr-defined]


def route(args: list[str]) -> list[str]:
    """Rewrite argv so the shortcuts above work. Pure function: easy to test."""
    if not args:
        return ["session"]
    first = args[0]
    if first in ROOT_OPTIONS or first in command_names():
        return args
    # Anything else is a task (possibly preceded by run options such as -p).
    return ["run", *args]


def main(argv: list[str] | None = None) -> None:
    """Console-script entry point (see [project.scripts] in pyproject.toml)."""
    make_streams_safe()
    args = list(sys.argv[1:] if argv is None else argv)
    app(args=route(args), prog_name="forge")
