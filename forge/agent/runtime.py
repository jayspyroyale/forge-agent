"""Builds and runs a task from configuration.

This is the one place that wires the pieces together (provider, workspace,
tools, permissions, verification, evidence). The CLI uses it today; a future
exploration controller can call `run_task` once per candidate to get fully
independent runtimes, each returning its own proof of work.
"""

from pydantic import BaseModel

from forge.agent.events import EventHandler
from forge.agent.loop import Agent
from forge.agent.prompts import build_system_prompt
from forge.agent.state import AgentState
from forge.config import ForgeConfig
from forge.evidence import TaskEvidence, build_evidence
from forge.models.base import ModelProvider
from forge.models.registry import create_provider
from forge.security.permissions import Approver, PermissionEngine
from forge.tools.base import ToolContext
from forge.tools.builtin import create_default_tools
from forge.tools.executor import ToolExecutor
from forge.tools.registry import ToolRegistry
from forge.verification.checks import VerificationCheck
from forge.verification.detect import detect_checks
from forge.verification.runner import Verifier
from forge.workspace import Workspace


def create_agent(
    config: ForgeConfig,
    *,
    provider: ModelProvider | None = None,
    tools: ToolRegistry | None = None,
    approver: Approver | None = None,
    permissions: PermissionEngine | None = None,
    checks: list[VerificationCheck] | None = None,
    on_event: EventHandler | None = None,
) -> Agent:
    """Create an agent. Everything except `config` can be injected (tests, custom setups).

    Pass `permissions` to share one engine (and its session approvals) across
    several tasks; otherwise a new engine is built from `config.permissions`
    and `approver`. `checks` defaults to the checks detected in the workspace.
    """
    workspace = Workspace(config.workspace)
    context = ToolContext(workspace=workspace, config=config)
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
        max_steps=config.max_steps,
        system_prompt=system_prompt,
        verifier=verifier if config.verification == "auto" else None,
        verification_attempts=config.verification_attempts,
        on_event=on_event,
    )


class TaskOutcome(BaseModel):
    state: AgentState
    evidence: TaskEvidence


async def run_task(config: ForgeConfig, task: str, **agent_options) -> TaskOutcome:
    """Run one task and collect its proof of work. Options are passed to `create_agent`."""
    agent = create_agent(config, **agent_options)
    state = await agent.run(task)
    configured = agent.verifier.available_kinds if agent.verifier else set()
    evidence = build_evidence(
        state,
        configured_kinds=configured,
        provider=agent.provider.name,
        model=_model_name(agent.provider),
    )
    return TaskOutcome(state=state, evidence=evidence)


def _model_name(provider: ModelProvider) -> str | None:
    try:
        return provider.model
    except Exception:  # some providers have no model configured; evidence just omits it
        return None
