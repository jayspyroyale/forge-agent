"""`forge memory list | inspect | add | forget | prune`: see and manage what Forge remembers."""

import json
from datetime import timedelta
from typing import Annotated

import typer
from pydantic import ValidationError
from rich.markup import escape
from rich.table import Table

from forge.agent.runtime import memory_path, memory_project
from forge.cli.output import console, fail
from forge.cli.settings import load_cli_config
from forge.memory import MEMORY_KINDS, MemoryInput, MemoryNotFoundError, MemorySecretError, MemoryStore, MemoryStoreError
from forge.memory.models import MemoryRecord, now
from forge.security.secret_scan import redact_secrets
from forge.tools.executor import summarize_validation_error

memory_app = typer.Typer(help="See and manage Forge's persistent project memory.", no_args_is_help=True)

_STATUS_STYLE = {"active": "green", "superseded": "dim", "contested": "yellow"}


def _open(ctx: typer.Context) -> tuple[MemoryStore, str]:
    config = load_cli_config(ctx).config
    try:
        return MemoryStore(memory_path(config)), memory_project(config)
    except MemoryStoreError as error:
        raise fail(str(error))


@memory_app.command("list")
def list_memories(
    ctx: typer.Context,
    show_all: Annotated[bool, typer.Option("--all", "-a", help="Include superseded, contested, and expired memories.")] = False,
    kind: Annotated[str | None, typer.Option("--kind", help=f"Only this kind ({', '.join(MEMORY_KINDS)}).")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print as JSON.")] = False,
) -> None:
    """List what Forge remembers about this project."""
    store, project = _open(ctx)
    with store:
        records = store.list_memories(project, include_inactive=show_all)
    if kind:
        records = [record for record in records if record.kind == kind]
    if as_json:
        console.print_json(json.dumps([_public(record) for record in records]))
        return
    if not records:
        console.print(f"[dim]No memories for {escape(project)}.[/dim]")
        return
    table = Table(title=f"Memory for {project}")
    for column in ("ID", "Kind", "Confidence", "Source", "Status", "Content"):
        table.add_column(column, no_wrap=column != "Content")
    for record in records:
        status = "expired" if record.expired() else record.status
        style = _STATUS_STYLE.get(status, "dim")
        table.add_row(
            record.id,
            record.kind,
            record.confidence,
            escape(record.source),
            f"[{style}]{status}[/{style}]",
            escape(_shorten(redact_secrets(record.content))),
        )
    console.print(table)


@memory_app.command("inspect")
def inspect_memory(ctx: typer.Context, memory_id: Annotated[str, typer.Argument(help="Memory id.")]) -> None:
    """Show one memory with its provenance, confidence, and history."""
    store, project = _open(ctx)
    with store:
        try:
            record = store.get(memory_id, project)
        except MemoryNotFoundError as error:
            raise fail(str(error))
    for key, value in _public(record).items():
        if value is not None:
            console.print(f"[bold]{key:<14}[/bold] {escape(str(value))}")


@memory_app.command("add")
def add_memory(
    ctx: typer.Context,
    kind: Annotated[str, typer.Argument(help=f"One of: {', '.join(MEMORY_KINDS)}.")],
    content: Annotated[str, typer.Argument(help="The statement to remember.")],
    subject: Annotated[str | None, typer.Option("--subject", help="What it answers; a newer memory with the same subject replaces it.")] = None,
    expires_in_days: Annotated[int | None, typer.Option("--expires-in-days", help="Forget it after this many days.")] = None,
    confidence: Annotated[str, typer.Option("--confidence", help="low, medium, or high.")] = "high",
) -> None:
    """Tell Forge something to remember about this project."""
    try:
        memory = MemoryInput(
            kind=kind,  # type: ignore[arg-type]
            content=content,
            subject=subject,
            source="user",
            confidence=confidence,  # type: ignore[arg-type]
            expires_at=now() + timedelta(days=expires_in_days) if expires_in_days else None,
        )
    except ValidationError as error:
        raise fail(f"Invalid memory: {summarize_validation_error(error)}")
    store, project = _open(ctx)
    with store:
        try:
            result = store.remember(project, memory)
        except MemorySecretError as error:
            raise fail(str(error))
    console.print(f"[green]{result.action.capitalize()}[/green] memory {result.record.id}")
    if result.replaced:
        console.print(f"[dim]It replaces: {', '.join(result.replaced)}[/dim]")
    if result.action == "contested":
        console.print(f"[yellow]It conflicts with the more trusted memory {result.record.conflicts_with} and will not be used.[/yellow]")


@memory_app.command("forget")
def forget_memory(
    ctx: typer.Context,
    memory_id: Annotated[str, typer.Argument(help="Memory id (see `forge memory list`).")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't ask for confirmation.")] = False,
) -> None:
    """Delete one memory permanently."""
    store, project = _open(ctx)
    with store:
        try:
            record = store.get(memory_id, project)
        except MemoryNotFoundError as error:
            raise fail(str(error))
        console.print(f"{record.kind}: {escape(_shorten(redact_secrets(record.content)))}")
        if not yes and not typer.confirm("Forget this memory?", default=False):
            raise typer.Exit(code=1)
        store.forget(memory_id, project)
    console.print(f"[green]Forgot[/green] {memory_id}")


@memory_app.command("prune")
def prune_memories(ctx: typer.Context) -> None:
    """Delete this project's expired memories."""
    store, project = _open(ctx)
    with store:
        removed = store.prune_expired(project)
    console.print(f"Removed {removed} expired memor{'y' if removed == 1 else 'ies'}.")


def _public(record: MemoryRecord) -> dict:
    data = record.model_dump(mode="json")
    data["content"] = redact_secrets(record.content)
    data["expired"] = record.expired()
    return data


def _shorten(text: str, limit: int = 100) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"
