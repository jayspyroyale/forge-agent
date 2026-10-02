"""The agent loop: ask the model what to do, run tools, repeat.

Not implemented in Phase 1. The class exists so the shape of the agent layer
is visible and later phases have a clear place to build on.
"""

from forge.config import ForgeConfig


class AgentLoop:
    def __init__(self, config: ForgeConfig) -> None:
        self.config = config

    def run(self, task: str) -> None:
        raise NotImplementedError("The agent loop is planned for Phase 2.")
