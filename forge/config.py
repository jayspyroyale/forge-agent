"""Forge configuration.

All configuration lives here so the rest of the code can receive a single
`ForgeConfig` object instead of reading settings from many places.

Settings can come from environment variables (see `ENV_VARS`). Secrets such as
API keys are deliberately NOT part of this config: each provider reads its own
key from the environment when it is created, so keys never end up in a config
object that might be printed, logged, or saved.
"""

import os
from pathlib import Path
from typing import Any, Self

from pydantic import BaseModel, Field

# Maps each config field to the environment variable that can set it.
ENV_VARS = {
    "provider": "FORGE_PROVIDER",
    "model": "FORGE_MODEL",
    "base_url": "FORGE_BASE_URL",
    "temperature": "FORGE_TEMPERATURE",
    "timeout": "FORGE_TIMEOUT",
    "debug": "FORGE_DEBUG",
}


class TerminalSettings(BaseModel):
    """Limits for commands Forge runs."""

    # Default seconds a command may run; a tool call can ask for less or more, up to max_timeout.
    timeout: float = Field(default=120.0, gt=0)
    max_timeout: float = Field(default=600.0, gt=0)
    # Characters kept from each of stdout and stderr.
    output_limit: int = Field(default=12_000, ge=500)


class ForgeConfig(BaseModel):
    """Settings for a Forge session."""

    # default_factory is called each time a config is created, so the
    # workspace is the directory Forge is run from, not where it was imported.
    workspace: Path = Field(default_factory=Path.cwd)

    # Upper limit on agent loop iterations; must be at least 1.
    max_steps: int = Field(default=20, gt=0)

    debug: bool = False

    # --- Model settings ---
    provider: str = "openai"
    # None means "use the provider's default model".
    model: str | None = None
    # Lets OpenAI-compatible providers point at another server.
    base_url: str | None = None
    # None means "use the provider's default temperature".
    temperature: float | None = Field(default=None, ge=0, le=2)
    # Seconds to wait for a model reply. Generous because local models can be slow.
    timeout: float = Field(default=120.0, gt=0)

    terminal: TerminalSettings = Field(default_factory=TerminalSettings)

    @classmethod
    def from_env(cls, **overrides: Any) -> Self:
        """Build a config from environment variables, then apply overrides.

        Overrides (for example from CLI options) win over environment
        variables. Overrides that are None are ignored, so callers can pass
        optional CLI values straight through.
        """
        values: dict[str, Any] = {}
        for field, env_var in ENV_VARS.items():
            value = os.environ.get(env_var)
            if value:
                values[field] = value

        for field, value in overrides.items():
            if value is not None:
                values[field] = value

        # Pydantic converts strings like "0.5" or "true" to the field's type.
        return cls(**values)
