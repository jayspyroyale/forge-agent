"""Builds a ready-to-run agent from configuration.

This is the one place that wires the pieces together (provider, workspace,
tools, executor). The CLI uses it today; a future exploration controller can
call it once per candidate to get fully independent runtimes.
"""

from forge.agent.events import EventHandler
from forge.agent.loop import Agent
from forge.config import ForgeConfig
from forge.models.base import ModelProvider
from forge.models.registry import create_provider
from forge.tools.base import ToolContext
from forge.tools.builtin import create_default_tools
from forge.tools.executor import ToolExecutor
from forge.tools.registry import ToolRegistry
from forge.workspace import Workspace


def create_agent(
    config: ForgeConfig,
    *,
    provider: ModelProvider | None = None,
    tools: ToolRegistry | None = None,
    on_event: EventHandler | None = None,
) -> Agent:
    """Create an agent. `provider` and `tools` can be injected (tests, custom setups)."""
    workspace = Workspace(config.workspace)
    context = ToolContext(workspace=workspace, config=config)
    executor = ToolExecutor(tools or create_default_tools(), context)
    return Agent(
        provider or create_provider(config),
        executor,
        max_steps=config.max_steps,
        on_event=on_event,
    )
