"""Builds and runs a task from configuration.

This is the one place that wires the pieces together (provider, workspace,
tools, permissions, verification, evidence). The CLI uses it today; a future
exploration controller can call `run_task` once per candidate to get fully
independent runtimes, each returning its own proof of work.
"""

import asyncio
import contextlib
from collections.abc import Callable, Sequence
from pathlib import Path
from datetime import UTC, datetime

from pydantic import BaseModel

from forge.agent.events import EventHandler, Notice
from forge.agent.loop import Agent
from forge.agent.prompts import build_system_prompt
from forge.agent.state import AgentState, new_task_id
from forge.config import ContextSettings, ForgeConfig
from forge.config.loader import user_config_dir
from forge.concurrency import run_blocking
from forge.context.engine import ContextBudget
from forge.context.items import BackgroundItem, Importance, Provenance, SourceType
from forge.context.retrieval import find_relevant_files, format_relevant_files
from forge.evidence import TaskEvidence, build_evidence
from forge.git.repo import GitRepository
from forge.mcp.manager import McpManager, ServerStatus
from forge.memory.facts import detect_project_facts
from forge.memory.models import MemoryRecord
from forge.memory.retrieval import format_memories, relevant_memories
from forge.memory.store import MemoryStore, MemoryStoreError, project_key
from forge.models.base import ModelProvider
from forge.models.budget import BudgetExhausted
from forge.models.registry import create_provider
from forge.security.permissions import Approver, PermissionEngine
from forge.tasks.snapshot import compute_changes, take_snapshot
from forge.tasks.store import TaskStore
from forge.tools.base import Tool, ToolContext
from forge.tools.builtin import create_default_tools
from forge.tools.builtin.memory import RememberTool
from forge.tools.executor import ToolExecutor
from forge.tools.registry import ToolRegistry
from forge.verification.checks import VerificationCheck
from forge.verification.detect import detect_checks
from forge.verification.runner import Verifier
from forge.workspace import DEFAULT_IGNORED_DIRS, DEFAULT_PROTECTED_PATHS, Workspace


def create_agent(
    config: ForgeConfig,
    *,
    provider: ModelProvider | None = None,
    tools: ToolRegistry | None = None,
    approver: Approver | None = None,
    permissions: PermissionEngine | None = None,
    checks: list[VerificationCheck] | None = None,
    before_write: Callable[[Path], None] | None = None,
    on_event: EventHandler | None = None,
    extra_tools: Sequence[Tool] = (),
    operation_guard: Callable[[], None] | None = None,
    deadline: float | None = None,
) -> Agent:
    """Create an agent. Everything except `config` can be injected (tests, custom setups).

    Pass `permissions` to share one engine (and its session approvals) across
    several tasks; otherwise a new engine is built from `config.permissions`
    and `approver`. `checks` defaults to the checks detected in the workspace.
    """
    workspace = workspace_from_config(config)
    context = ToolContext(workspace=workspace, config=config, before_write=before_write, deadline=deadline)
    engine = permissions or PermissionEngine(config.permissions, approver)
    registry = tools or create_default_tools()
    for tool in extra_tools:
        registry.register(tool)
    executor = ToolExecutor(registry, context, engine)

    if checks is None:
        checks = detect_checks(workspace.root)
    verifier = Verifier(workspace, checks, engine, config.terminal, deadline=deadline, cancelled=context.cancelled)

    system_prompt = build_system_prompt(
        "coding",
        workspace=str(workspace.root),
        tool_names=registry.names(),
        verification_commands=[check.command for check in checks],
    )
    return Agent(
        provider or create_provider(config),
        executor,
        max_steps=config.agent.max_steps,
        system_prompt=system_prompt,
        verifier=verifier if config.agent.verification == "auto" else None,
        verification_attempts=config.agent.verification_attempts,
        on_event=on_event,
        context_budget=context_budget(config.context),
        operation_guard=operation_guard,
    )


def context_budget(settings: ContextSettings) -> ContextBudget:
    return ContextBudget(
        max_tokens=settings.max_tokens,
        keep_recent_outputs=settings.keep_recent_outputs,
        compress_above_tokens=settings.compress_above_tokens,
    )


def memory_path(config: ForgeConfig) -> Path:
    return config.memory.path or user_config_dir() / "memory.db"


def memory_project(config: ForgeConfig) -> str:
    return config.memory.project or project_key(config.workspace_root)


def open_memory(config: ForgeConfig) -> MemoryStore | None:
    """The memory store, or None when memory is disabled. Raises MemoryStoreError if it is unusable."""
    if not config.memory.enabled:
        return None
    return MemoryStore(memory_path(config))


def gather_background(
    config: ForgeConfig, workspace: Workspace, task: str, memories: Sequence[MemoryRecord] = ()
) -> list[BackgroundItem]:
    """Context Forge collects before the agent starts, each item with its provenance."""
    items: list[BackgroundItem] = []
    if memories:
        items.append(
            BackgroundItem(
                content=format_memories(list(memories)),
                provenance=Provenance(
                    source=SourceType.MEMORY,
                    source_id=", ".join(record.id for record in memories),
                    detail="persistent project memory",
                ),
                importance=Importance.HIGH,
                key="background:memory",
            )
        )
    if config.context.retrieval and config.context.retrieval_max_files:
        files = find_relevant_files(workspace, task, limit=config.context.retrieval_max_files)
        if files:
            items.append(
                BackgroundItem(
                    content=format_relevant_files(files),
                    provenance=Provenance(
                        source=SourceType.FILE,
                        source_id=", ".join(item.path for item in files),
                        detail="retrieval: file names, text search, uncommitted changes",
                    ),
                    importance=Importance.NORMAL,
                    key="background:relevant_files",
                )
            )
    return items


class TaskOutcome(BaseModel):
    state: AgentState
    evidence: TaskEvidence


class TaskCancelled(asyncio.CancelledError):
    """Interruption with the persisted proof of work available to the controller."""

    def __init__(self, outcome: TaskOutcome):
        super().__init__("Task cancelled; evidence was saved")
        self.outcome = outcome


async def run_task(config: ForgeConfig, task: str, *, record: bool = True, **agent_options) -> TaskOutcome:
    """Run one task and collect its proof of work. Options are passed to `create_agent`.

    Before the agent starts, Forge snapshots the workspace (Git HEAD plus the
    user's pre-existing changes) and journals every file a tool is about to
    modify. Afterwards it compares the workspace with the snapshot, so the
    evidence separates this task's changes from changes that were already
    there. With `record=True` the snapshot, journal, and evidence are saved
    under `.forge/tasks/<task_id>/`, which makes the task reviewable and undoable.
    """
    workspace = workspace_from_config(config)
    repo = GitRepository.discover(workspace.root)
    snapshot = take_snapshot(workspace, repo)
    task_id = new_task_id()

    store = TaskStore(workspace) if record else None
    journal = None
    if store is not None:
        store.create(task_id)
        store.save_checkpoint(task_id, snapshot)
        journal = store.journal(task_id)

    on_event = agent_options.get("on_event")
    extra_tools = list(agent_options.pop("extra_tools", ()))
    mcp_statuses: list[ServerStatus] = []
    interrupted = False
    # Memory and MCP servers live exactly as long as the agent runs, even if it fails.
    with contextlib.ExitStack() as resources:
        memory, memories = _prepare_memory(config, workspace, task, on_event)
        if memory is not None:
            resources.callback(memory.close)
            if config.memory.model_writes:
                extra_tools.append(RememberTool(memory, memory_project(config)))
        if any(server.enabled for server in config.mcp.servers.values()):
            mcp = McpManager(config.mcp, workspace.root)
            resources.callback(mcp.close)
            mcp_statuses = await run_blocking(mcp.connect_all, on_cancel=mcp.close)
            _report_mcp(mcp_statuses, on_event)
            extra_tools += mcp.tools()

        agent = create_agent(
            config, before_write=journal.before_write if journal else None, extra_tools=extra_tools, **agent_options
        )
        background = gather_background(config, workspace, task, memories)
        try:
            state = await agent.run(task, task_id=task_id, background=background)
        except asyncio.CancelledError:
            interrupted = True
            state = agent.state
            state.status = "cancelled"
            state.error = "Task interrupted; file changes and evidence retained for recovery."
            state.finished_at = datetime.now(UTC)

    changes = compute_changes(snapshot, workspace, repo, journal.pre_images() if journal else None)
    if changes.changes and not interrupted:
        try:
            await agent.verify_final(state)
        except BudgetExhausted as error:
            state.status = "budget_exhausted"
            state.error = str(error)
        except asyncio.CancelledError:
            interrupted = True
            state.status = "cancelled"
            state.error = "Final verification interrupted; evidence retained for recovery."
            state.finished_at = datetime.now(UTC)
    configured = agent.verifier.available_kinds if agent.verifier else set()
    evidence = build_evidence(
        state,
        configured_kinds=configured,
        provider=agent.provider.name,
        model=_model_name(agent.provider),
        changes=changes,
    )
    if memories:
        evidence.extra["memories_used"] = [record.id for record in memories]
    if mcp_statuses:
        evidence.extra["mcp_servers"] = [
            status.model_dump(include={"name", "connected", "error", "tools"}) for status in mcp_statuses
        ]
    if store is not None:
        store.save_evidence(evidence)
        store.save_context(task_id, agent.context.manifest())
    outcome = TaskOutcome(state=state, evidence=evidence)
    if interrupted:
        raise TaskCancelled(outcome)
    return outcome


def _report_mcp(statuses: list[ServerStatus], on_event: EventHandler | None) -> None:
    if on_event is None:
        return
    for status in statuses:
        if not status.connected:
            on_event(Notice(level="warning", message=f"MCP server '{status.name}' is unavailable: {status.error}"))
            continue
        on_event(Notice(message=f"MCP server '{status.name}': {len(status.tools)} tool(s)"))
        for skipped in status.skipped:
            on_event(
                Notice(level="warning", message=f"MCP tool '{status.name}.{skipped.name}' skipped: {skipped.reason}")
            )


def _prepare_memory(
    config: ForgeConfig, workspace: Workspace, task: str, on_event: EventHandler | None
) -> tuple[MemoryStore | None, list[MemoryRecord]]:
    """Open memory, record facts from project files, and pick the memories relevant to `task`.

    Memory is optional: if the database is unusable, the task runs without it and the user is told.
    """
    memory = None
    try:
        memory = open_memory(config)
        if memory is None:
            return None, []
        project = memory_project(config)
        if config.memory.detect_facts:
            for fact in detect_project_facts(workspace.root):
                memory.remember(project, fact)
        records = memory.list_memories(project)
    except MemoryStoreError as error:
        if memory is not None:
            memory.close()
        if on_event is not None:
            on_event(Notice(level="warning", message=f"Project memory is unavailable, continuing without it: {error}"))
        return None, []
    return memory, relevant_memories(records, task, limit=config.memory.max_items)


def _model_name(provider: ModelProvider) -> str | None:
    try:
        return provider.model
    except Exception:  # some providers have no model configured; evidence just omits it
        return None


def workspace_from_config(config: ForgeConfig) -> Workspace:
    """The workspace described by the config: its root plus any extra protected or ignored paths."""
    settings = config.workspace
    return Workspace(
        config.workspace_root,
        protected_paths=(*DEFAULT_PROTECTED_PATHS, *settings.protected_paths),
        ignored_dirs=DEFAULT_IGNORED_DIRS | frozenset(settings.ignored_dirs),
    )
