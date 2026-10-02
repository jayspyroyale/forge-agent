"""Forge configuration.

All configuration lives here so the rest of the code can receive a single
`ForgeConfig` object instead of reading settings from many places.
Loading config from files or environment variables comes in a later phase.
"""

from pathlib import Path

from pydantic import BaseModel, Field


class ForgeConfig(BaseModel):
    """Settings for a Forge session."""

    # default_factory is called each time a config is created, so the
    # workspace is the directory Forge is run from, not where it was imported.
    workspace: Path = Field(default_factory=Path.cwd)

    # Upper limit on agent loop iterations; must be at least 1.
    max_steps: int = Field(default=20, gt=0)

    debug: bool = False
