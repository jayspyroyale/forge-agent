"""`forge mcp list | tools | test`: inspect configured MCP servers and their tools."""

import time
from typing import Annotated

import typer
from rich.markup import escape
from rich.table import Table

from forge.cli.output import console, fail
from forge.cli.settings import load_cli_config
from forge.mcp.manager import McpManager, ServerStatus, describe_command

mcp_app = typer.Typer(help="Inspect MCP servers configured in [mcp.servers.<name>].", no_args_is_help=True)


@mcp_app.command("list")
def list_servers(ctx: typer.Context) -> None:
    """Show configured MCP servers (does not start them)."""
    config = load_cli_config(ctx).config
    servers = config.mcp.servers
    if not servers:
        console.print("[dim]No MCP servers configured. Add one under [mcp.servers.<name>] in .forge/config.toml.[/dim]")
        return
    table = Table(title="MCP servers")
    for column in ("Name", "Command", "Enabled", "Risk", "Env"):
        table.add_column(column)
    for name, server in servers.items():
        env = ", ".join(sorted(server.env)) or "-"  # names only, never values
        table.add_row(name, escape(describe_command(server)), "yes" if server.enabled else "no", server.risk, escape(env))
    console.print(table)


@mcp_app.command("tools")
def list_tools(
    ctx: typer.Context,
    server: Annotated[str | None, typer.Argument(help="Only this server.")] = None,
) -> None:
    """Start the servers, list the tools Forge would register, then stop them."""
    config = load_cli_config(ctx).config
    _require(config.mcp.servers, server)
    with McpManager(config.mcp, config.workspace_root) as manager:
        statuses = manager.connect_all(only=server)
        tools = {tool.name: tool for tool in manager.tools()}
    for status in statuses:
        if not status.connected:
            console.print(f"[red]✗ {escape(status.name)}[/red]: {escape(status.error or 'unavailable')}")
            continue
        console.print(f"[bold]{escape(status.name)}[/bold] [dim]({len(status.tools)} tools)[/dim]")
        for name in status.tools:
            tool = tools[name]
            console.print(f"  [cyan]{escape(name)}[/cyan] [yellow]({tool.risk})[/yellow] [dim]{escape(_short(tool.description))}[/dim]")
        for skipped in status.skipped:
            console.print(f"  [yellow]skipped {escape(skipped.name)}: {escape(skipped.reason)}[/yellow]")


@mcp_app.command("test")
def test_server(ctx: typer.Context, server: Annotated[str, typer.Argument(help="Server name.")]) -> None:
    """Connect to one server, check the handshake and tool list, and report problems."""
    config = load_cli_config(ctx).config
    _require(config.mcp.servers, server)
    started = time.monotonic()
    with McpManager(config.mcp, config.workspace_root) as manager:
        (status,) = manager.connect_all(only=server)
    elapsed = time.monotonic() - started
    _report(status, elapsed)
    if not status.connected:
        raise typer.Exit(code=1)


def _report(status: ServerStatus, elapsed: float) -> None:
    if not status.connected:
        console.print(f"[red]✗ {escape(status.name)}: {escape(status.error or 'unavailable')}[/red]")
        for line in status.stderr:
            console.print(f"  [dim]{escape(line)}[/dim]")
        return
    console.print(f"[green]✓ {escape(status.name)}[/green] connected in {elapsed:.1f}s")
    console.print(f"  server:   {escape(status.server_name or '(unnamed)')}")
    console.print(f"  protocol: {escape(status.protocol_version or '?')}")
    console.print(f"  tools:    {len(status.tools)}" + (f", {len(status.skipped)} skipped" if status.skipped else ""))
    for skipped in status.skipped:
        console.print(f"  [yellow]skipped {escape(skipped.name)}: {escape(skipped.reason)}[/yellow]")


def _require(servers: dict, name: str | None) -> None:
    if name is not None and name not in servers:
        available = ", ".join(sorted(servers)) or "none"
        raise fail(f"No MCP server named '{name}'. Configured: {available}")


def _short(text: str, limit: int = 80) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
