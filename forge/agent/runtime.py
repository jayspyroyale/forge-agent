"""Builds and runs a task from configuration.

This is the one place that wires the pieces together (provider, workspace,
tools, permissions, verification, evidence). The CLI uses it today; a future
exploration controller can call `run_task` once per candidate to get fully
independent runtimes, each returning its own proof of work.
"""

from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel

from forge.agent.events import EventHandler
from forge.agent.loop import Agent
from forge.agent.prompts import build_system_prompt
from forge.agent.state import AgentState, new_task_id
from forge.config import ForgeConfig
from forge.evidence import TaskEvidence, build_evidence
from forge.git.repo import GitRepository
from forge.models.base import ModelProvider
from forge.models.registry import create_provider
from forge.security.permissions import Approver, PermissionEngine
from forge.tasks.snapshot import compute_changes, take_snapshot
from forge.tasks.store import TaskStore
from forge.tools.base import ToolContext
from forge.tools.builtin import create_default_tools
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
) -> Agent:
    """Create an agent. Everything except `config` can be injected (tests, custom setups).

    Pass `permissions` to share one engine (and its session approvals) across
    several tasks; otherwise a new engine is built from `config.permissions`
    and `approver`. `checks` defaults to the checks detected in the workspace.
    """
    workspace = workspace_from_config(config)
    context = ToolContext(workspace=workspace, config=config, before_write=before_write)
    engine = permissions or PermissionEngine(config.permissions, approver)
    registry = tools or create_default_tools()
    executor = ToolExecutor(registry, context, engine)

    if checks is None:
        checks = detect_checks(workspace.root)
    verifier = Verifier(workspace, checks, engine, config.terminal)

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
    )


class TaskOutcome(BaseModel):
    state: AgentState
    evidence: TaskEvidence


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

    agent = create_agent(config, before_write=journal.before_write if journal else None, **agent_options)
    state = await agent.run(task, task_id=task_id)

    changes = compute_changes(snapshot, workspace, repo, journal.pre_images() if journal else None)
    configured = agent.verifier.available_kinds if agent.verifier else set()
    evidence = build_evidence(
        state,
        configured_kinds=configured,
        provider=agent.provider.name,
        model=_model_name(agent.provider),
        changes=changes,
    )
    if store is not None:
        store.save_evidence(evidence)
    return TaskOutcome(state=state, evidence=evidence)


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
