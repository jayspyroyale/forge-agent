"""Forge command-line interface.

This module only parses commands and displays output. Any real work is done
by other modules (for example `forge.doctor`), which keeps the CLI thin and
the logic testable on its own.
"""

import asyncio
import json
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.markup import escape
from rich.prompt import Prompt
from rich.table import Table

from forge import __version__
from forge.agent.events import AgentEvent, ToolFinished, ToolStarted, VerificationFinished, VerificationStarted
from forge.agent.runtime import run_task
from forge.agent.state import AgentStatus
from forge.config import ForgeConfig
from forge.doctor import run_all_checks
from forge.evidence import TaskEvidence
from forge.models.errors import ModelError
from forge.models.registry import create_provider, default_registry
from forge.models.types import Message, ToolCall
from forge.security.permissions import ApprovalChoice, Approver, PermissionEngine, PermissionRequest
from forge.tools.base import ToolContext
from forge.tools.builtin import create_default_tools
from forge.tools.executor import ToolExecutor
from forge.tools.registry import ToolNotFoundError
from forge.workspace import Workspace, WorkspaceError

app = typer.Typer(
    help="Forge: a lightweight, model-agnostic runtime for AI coding agents.",
    no_args_is_help=True,
)
models_app = typer.Typer(help="Inspect available model providers.", no_args_is_help=True)
app.add_typer(models_app, name="models")
tools_app = typer.Typer(help="Inspect and run Forge tools directly, without a model.", no_args_is_help=True)
app.add_typer(tools_app, name="tools")

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

    console.print(response.content, markup=False, highlight=False, soft_wrap=True)

    footer = f"{config.provider} · {response.model or model_provider.model}"
    if response.usage is not None:
        footer += f" · {response.usage.total_tokens} tokens"
    console.print(f"[dim]{escape(footer)}[/dim]")


@tools_app.command("list")
def list_tools() -> None:
    """List the built-in tools."""
    table = Table(title="Tools")
    table.add_column("Name", style="bold")
    table.add_column("Description")
    for tool in create_default_tools().list_tools():
        table.add_row(tool.name, tool.description)
    console.print(table)


@tools_app.command("describe")
def describe_tool(name: Annotated[str, typer.Argument(help="Tool name.")]) -> None:
    """Show a tool's description and argument schema."""
    try:
        tool = create_default_tools().get(name)
    except ToolNotFoundError as error:
        error_console.print(f"[red]Error:[/red] {escape(str(error))}")
        raise typer.Exit(code=1)
    definition = tool.definition()
    console.print(f"[bold]{escape(definition.name)}[/bold]")
    console.print(escape(definition.description))
    console.print_json(json.dumps(definition.parameters))


@tools_app.command("run")
def run_tool(
    name: Annotated[str, typer.Argument(help="Tool name.")],
    arguments: Annotated[
        list[str] | None, typer.Argument(help="Arguments as key=value pairs, e.g. path=README.md.")
    ] = None,
    json_arguments: Annotated[
        str | None, typer.Option("--json", help="Arguments as a JSON object.")
    ] = None,
    workspace: Annotated[
        Path | None, typer.Option("--workspace", "-w", help="Workspace root (default: current directory).")
    ] = None,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Approve actions that need approval (never overrides 'deny').")
    ] = False,
) -> None:
    """Run one tool directly and print its result. Permission rules still apply."""
    try:
        parsed = _parse_tool_arguments(arguments or [], json_arguments)
        config = ForgeConfig.from_env()
        context = ToolContext(workspace=Workspace(workspace or Path.cwd()), config=config)
    except (ValueError, ValidationError, WorkspaceError) as error:
        error_console.print(f"[red]Error:[/red] {escape(str(error))}")
        raise typer.Exit(code=2)

    permissions = PermissionEngine(config.permissions, make_cli_approver(auto_approve=yes))
    executor = ToolExecutor(create_default_tools(), context, permissions)
    result = asyncio.run(executor.execute(ToolCall(id="cli", name=name, arguments=parsed)))

    if result.success:
        console.print(result.output, markup=False, highlight=False, soft_wrap=True)
    else:
        if result.output:
            console.print(result.output, markup=False, highlight=False, soft_wrap=True)
        error_console.print(f"[red]Error:[/red] {escape(result.error or 'unknown error')}")
        raise typer.Exit(code=1)


def _parse_tool_arguments(pairs: list[str], json_arguments: str | None) -> dict[str, Any]:
    """Merge --json and key=value arguments. Values that parse as JSON (numbers, true) are converted."""
    parsed: dict[str, Any] = {}
    if json_arguments:
        try:
            loaded = json.loads(json_arguments)
        except json.JSONDecodeError as error:
            raise ValueError(f"--json is not valid JSON: {error}") from None
        if not isinstance(loaded, dict):
            raise ValueError("--json must be a JSON object")
        parsed.update(loaded)
    for pair in pairs:
        key, separator, raw_value = pair.partition("=")
        if not separator or not key:
            raise ValueError(f"Expected key=value, got '{pair}'")
        try:
            parsed[key] = json.loads(raw_value)
        except json.JSONDecodeError:
            parsed[key] = raw_value
    return parsed


@app.command()
def run(
    task: Annotated[str, typer.Argument(help="What you want the agent to do.")],
    provider: Annotated[
        str | None, typer.Option("--provider", "-p", help="Provider name (see `forge models list`).")
    ] = None,
    model: Annotated[str | None, typer.Option("--model", "-m", help="Model name.")] = None,
    max_steps: Annotated[
        int | None, typer.Option("--max-steps", help="Maximum number of model calls.")
    ] = None,
    debug: Annotated[
        bool | None, typer.Option("--debug", help="Show full error tracebacks.")
    ] = None,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Approve actions that need approval (never overrides 'deny').")
    ] = False,
) -> None:
    """Run the agent on a task in the current directory."""
    try:
        config = ForgeConfig.from_env(provider=provider, model=model, max_steps=max_steps, debug=debug)
    except ValidationError as error:
        error_console.print(f"[red]Invalid configuration:[/red] {escape(str(error))}")
        raise typer.Exit(code=1)

    try:
        outcome = asyncio.run(
            run_task(config, task, approver=make_cli_approver(auto_approve=yes), on_event=_print_event)
        )
    except (ModelError, WorkspaceError) as error:
        if config.debug:
            error_console.print_exception()
        error_console.print(f"[red]Error:[/red] {escape(str(error))}")
        raise typer.Exit(code=1)

    state, evidence = outcome.state, outcome.evidence
    if state.final_answer:
        console.print()
        console.print(state.final_answer, markup=False, highlight=False, soft_wrap=True)
    _print_evidence(evidence)
    console.print()
    if state.status == AgentStatus.COMPLETED:
        steps = "1 step" if state.step == 1 else f"{state.step} steps"
        console.print(f"[green]Completed[/green] [dim]in {steps}[/dim]")
    else:
        error_console.print(f"[red]Stopped ({state.status}):[/red] {escape(state.error or '')}")
        raise typer.Exit(code=1)


def _print_event(event: AgentEvent) -> None:
    if isinstance(event, ToolStarted):
        arguments = escape(_short_json(event.call.arguments))
        console.print(f"[cyan]→ {escape(event.call.name)}[/cyan] [dim]{arguments}[/dim]")
    elif isinstance(event, ToolFinished) and not event.result.success:
        console.print(f"  [red]✗ {escape(event.result.error or 'failed')}[/red]")
    elif isinstance(event, VerificationStarted):
        console.print(f"[magenta]⧗ verifying: {escape(event.name)}[/magenta]")
    elif isinstance(event, VerificationFinished):
        color = "green" if event.result.passed else "red"
        console.print(f"  [{color}]{escape(event.result.status)}[/{color}]")


_OUTCOME_STYLE = {"verified": ("green", "✓"), "failed": ("red", "✗"), "unverified": ("yellow", "-")}


def _print_evidence(evidence: TaskEvidence) -> None:
    console.print()
    if evidence.files_changed:
        console.print("[bold]Changed:[/bold] " + escape(", ".join(evidence.files_changed)))
    console.print("[bold]Verification:[/bold]")
    for check in evidence.checks:
        color, mark = _OUTCOME_STYLE[check.outcome]
        console.print(f"  [{color}]{mark} {check.kind}[/{color}] [dim]{escape(check.detail)}[/dim]")


_APPROVAL_CHOICES = {"y": ApprovalChoice.ALLOW_ONCE, "a": ApprovalChoice.ALLOW_SESSION, "n": ApprovalChoice.DENY}


def make_cli_approver(auto_approve: bool = False) -> Approver:
    """An approver that shows the request and asks on the terminal."""

    def approve(request: PermissionRequest) -> ApprovalChoice:
        risk = request.risk
        console.print()
        console.print(f"[bold yellow]Permission needed[/bold yellow]: [bold]{escape(request.tool_name)}[/bold] "
                      f"[yellow]({risk.level})[/yellow]")
        for reason in risk.reasons:
            console.print(f"  [yellow]! {escape(reason)}[/yellow]")
        console.print(f"  [dim]{escape(_short_json(request.arguments, limit=500))}[/dim]")
        if auto_approve:
            console.print("  [green]approved (--yes)[/green]")
            return ApprovalChoice.ALLOW_ONCE
        answer = Prompt.ask(
            "  Allow? [bold]y[/bold]es once, [bold]a[/bold]lways this session, [bold]n[/bold]o",
            choices=list(_APPROVAL_CHOICES),
            default="n",
            console=console,
        )
        return _APPROVAL_CHOICES[answer]

    return approve


def _short_json(value: Any, limit: int = 100) -> str:
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[: limit - 3] + "..."
