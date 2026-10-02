"""The shape of Forge's configuration.

Every section rejects unknown keys, so a typo in a config file is reported
instead of silently ignored. No section has a field for secrets: API keys are
read from environment variables by the provider that needs them.
"""

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from forge.security.policy import PermissionPolicy


class Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModelSettings(Section):
    provider: str = "openai"
    name: str | None = None  # None: the provider's default model
    base_url: str | None = None  # for OpenAI-compatible servers
    temperature: float | None = Field(default=None, ge=0, le=2)  # None: the provider's default
    timeout: float = Field(default=120.0, gt=0)  # seconds to wait for a reply


class AgentSettings(Section):
    max_steps: int = Field(default=20, gt=0)
    # "auto": after the agent changes files, Forge runs the project's checks before accepting "done".
    verification: Literal["auto", "off"] = "auto"
    # How many times Forge verifies (and sends failures back) before giving up.
    verification_attempts: int = Field(default=3, ge=1)


class WorkspaceSettings(Section):
    root: Path | None = None  # None: the directory Forge is run from
    protected_paths: list[str] = Field(default_factory=list)  # added to .git and .forge/tasks
    ignored_dirs: list[str] = Field(default_factory=list)  # added to .venv, node_modules, ...


class TerminalSettings(Section):
    """Limits for commands Forge runs."""

    # Default seconds a command may run; a tool call can ask for less or more, up to max_timeout.
    timeout: float = Field(default=120.0, gt=0)
    max_timeout: float = Field(default=600.0, gt=0)
    # Characters kept from each of stdout and stderr.
    output_limit: int = Field(default=12_000, ge=500)


class UISettings(Section):
    verbose: bool = False
    debug: bool = False


class SelectionWeights(Section):
    correctness: float = Field(default=1.0, ge=0)
    safety: float = Field(default=1.0, ge=0)
    cost: float = Field(default=0.0, ge=0)
    latency: float = Field(default=0.0, ge=0)


class ExplorationSettings(Section):
    """Reserved for branching solution exploration (not implemented yet).

    Validated now so profiles and config files can already carry these values
    and future phases don't need a config migration.
    """

    approaches: int = Field(default=1, ge=1, le=10)
    budget_usd: float | None = Field(default=None, ge=0)
    weights: SelectionWeights = Field(default_factory=SelectionWeights)
    selection_mode: Literal["user", "recommend", "auto"] = "user"


# Phase 1-11 used flat keyword arguments. They still work when constructing a
# config: ForgeConfig(provider="fake", model="x", max_steps=5).
LEGACY_KEYS: dict[str, tuple[str, str]] = {
    "provider": ("model", "provider"),
    "base_url": ("model", "base_url"),
    "temperature": ("model", "temperature"),
    "timeout": ("model", "timeout"),
    "max_steps": ("agent", "max_steps"),
    "verification": ("agent", "verification"),
    "verification_attempts": ("agent", "verification_attempts"),
    "debug": ("ui", "debug"),
    "verbose": ("ui", "verbose"),
}


class ForgeConfig(Section):
    profile: str = "balanced"
    model: ModelSettings = Field(default_factory=ModelSettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    permissions: PermissionPolicy = Field(default_factory=PermissionPolicy)
    workspace: WorkspaceSettings = Field(default_factory=WorkspaceSettings)
    terminal: TerminalSettings = Field(default_factory=TerminalSettings)
    ui: UISettings = Field(default_factory=UISettings)
    exploration: ExplorationSettings = Field(default_factory=ExplorationSettings)

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_keys(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = dict(data)
        # model="name" (a string) meant the model name; model={...} is the section.
        if isinstance(data.get("model"), str):
            data["model"] = {"name": data["model"]}
        if isinstance(data.get("workspace"), (str, Path)):
            data["workspace"] = {"root": data["workspace"]}
        for key, (section, field) in LEGACY_KEYS.items():
            if key in data:
                value = data.pop(key)
                section_data = data.get(section)
                if isinstance(section_data, BaseModel):
                    section_data = section_data.model_dump()
                section_data = dict(section_data or {})
                section_data[field] = value
                data[section] = section_data
        return data

    @property
    def workspace_root(self) -> Path:
        return (self.workspace.root or Path.cwd()).resolve()

    @classmethod
    def from_env(cls, **overrides: Any) -> "ForgeConfig":
        """Defaults + FORGE_* environment variables + `overrides` (no config files).

        Kept for compatibility; the CLI uses `forge.config.load_config`, which
        also reads user and project config files.
        """
        from forge.config.loader import load_config, overrides_to_dotted

        return load_config(cli=overrides_to_dotted(overrides), include_files=False).config
