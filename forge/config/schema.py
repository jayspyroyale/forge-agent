"""The shape of Forge's configuration.

Every section rejects unknown keys, so a typo in a config file is reported
instead of silently ignored. No section has a field for secrets: API keys are
read from environment variables by the provider that needs them.
"""

import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from forge.security.policy import PermissionPolicy


class Section(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ModelSettings(Section):
    provider: str = "openai"
    name: str | None = None  # None: the provider's default model
    base_url: str | None = None  # for OpenAI-compatible servers
    temperature: float | None = Field(default=None, ge=0, le=2)  # None: the provider's default
    timeout: float = Field(default=120.0, gt=0)  # seconds to wait for a reply
    max_response_tokens: int = Field(default=4096, ge=1)
    input_cost_per_million: float | None = Field(default=None, ge=0)
    output_cost_per_million: float | None = Field(default=None, ge=0)


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


class ContextSettings(Section):
    """How much conversation Forge sends to the model, and how it makes room (see forge.context)."""

    # Estimated tokens per model call. Keep it below the model's context window, with headroom for its reply.
    max_tokens: int = Field(default=64_000, ge=2_000)
    # The most recent tool results are always sent in full.
    keep_recent_outputs: int = Field(default=6, ge=0)
    # Older tool output larger than this is replaced by a structural summary.
    compress_above_tokens: int = Field(default=1_500, ge=50)
    # Before the agent starts, point it at files that look relevant (name and text search, no embeddings).
    retrieval: bool = True
    retrieval_max_files: int = Field(default=8, ge=0, le=50)


class MemorySettings(Section):
    """Persistent project memory (see forge.memory)."""

    enabled: bool = True
    path: Path | None = None  # None: <FORGE_HOME or ~/.forge>/memory.db
    project: str | None = None  # memory scope; None: the workspace root
    max_items: int = Field(default=8, ge=0, le=50)  # memories shown to the agent per task
    detect_facts: bool = True  # record facts read from project files (package manager, languages)
    model_writes: bool = False  # give the agent a `remember` tool (needs approval; stored as low confidence)


RiskName = Literal["read", "write", "execute", "dangerous"]


class McpServerSettings(Section):
    """One MCP server Forge starts and talks to over stdio.

    Secrets never go here: `env` values can reference your environment as
    "${NAME}", and Forge fills them in when it starts the server.
    """

    command: str = Field(min_length=1)
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None  # relative to the workspace; None: the workspace root
    enabled: bool = True
    startup_timeout: float = Field(default=15.0, gt=0)
    timeout: float = Field(default=30.0, gt=0)  # seconds per tool call
    # How risky this server's tools are for the permission engine. External tools are
    # treated as running programs unless you say otherwise; servers cannot lower this.
    risk: RiskName = "execute"
    tool_risk: dict[str, RiskName] = Field(default_factory=dict)  # per tool, overrides `risk`
    tools: list[str] | None = None  # only expose these tools (None: all)


class McpSettings(Section):
    servers: dict[str, McpServerSettings] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_names(self) -> "McpSettings":
        for name in self.servers:
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,31}", name):
                raise ValueError(
                    f"MCP server name '{name}' must start with a letter and use only letters, digits, '_' or '-' (max 32)"
                )
        return self


class UISettings(Section):
    verbose: bool = False
    debug: bool = False


class SelectionWeights(Section):
    """How much each factor counts when Forge ranks candidates. Any scale; Forge normalizes them to sum to 1.

    Measured factors: correctness, safety, cost, speed, simplicity, minimal_diff.
    Model-assessed factors (only used when > 0, which asks a model to review the diffs):
    maintainability, scalability.
    """

    correctness: float = Field(default=50, ge=0)
    safety: float = Field(default=20, ge=0)
    cost: float = Field(default=10, ge=0)
    speed: float = Field(default=5, ge=0)
    simplicity: float = Field(default=10, ge=0)
    minimal_diff: float = Field(default=5, ge=0)
    maintainability: float = Field(default=0, ge=0)
    scalability: float = Field(default=0, ge=0)

    @model_validator(mode="before")
    @classmethod
    def _accept_latency(cls, data: Any) -> Any:
        if isinstance(data, dict) and "latency" in data:  # the Phase 12 name for speed
            data = dict(data)
            data.setdefault("speed", data.pop("latency"))
        return data

    @model_validator(mode="after")
    def _not_all_zero(self) -> "SelectionWeights":
        if sum(self.model_dump().values()) <= 0:
            raise ValueError("at least one selection weight must be greater than 0")
        return self

    def normalized(self) -> dict[str, float]:
        weights = self.model_dump()
        total = sum(weights.values())
        return {factor: value / total for factor, value in weights.items()}


class SelectionConstraints(Section):
    """Hard rules. A candidate that breaks one is ineligible, whatever its score."""

    require_completed: bool = True  # the agent finished (no crash, step limit, or budget stop)
    tests_must_pass: bool = True  # if the project has tests, Forge's own run of them passed
    security_checks_must_pass: bool = False  # no dangerous actions attempted, and lint/typecheck did not fail
    no_new_dependencies: bool = False
    max_files_changed: int | None = Field(default=None, ge=0)
    max_cost_usd: float | None = Field(default=None, ge=0)


SELECTION_MODE_ALIASES = {"user": "manual", "recommend": "assisted", "auto": "autonomous"}


class ExplorationSettings(Section):
    """`forge explore`: several candidate implementations, verified and compared (see forge.exploration)."""

    approaches: int = Field(default=2, ge=1, le=10)  # candidates per exploration (10 is a hard limit)
    # Where candidate workspaces (Git worktrees or copies) are created. None: <FORGE_HOME>/worktrees.
    workspace_dir: Path | None = None
    budget_usd: float | None = Field(default=None, ge=0)
    max_api_cost: float | None = Field(default=None, ge=0)
    max_tokens: int | None = Field(default=None, ge=0)
    max_elapsed_time: float | None = Field(default=None, gt=0)
    adaptive: bool = False
    initial_approaches: int = Field(default=2, ge=1, le=10)
    dominance_margin: float = Field(default=0.12, ge=0, le=1)
    plateau_rounds: int = Field(default=2, ge=1)
    experimental_generation: bool = False
    strategy: Literal["different_approaches", "same_approach"] = "different_approaches"
    model_assignment: Literal["round_robin", "automatic", "explicit"] = "round_robin"
    model_pool: list[ModelSettings] = Field(default_factory=list)
    candidate_models: dict[str, ModelSettings] = Field(default_factory=dict)
    # manual: you choose; assisted: Forge recommends, you confirm; autonomous: Forge chooses by policy.
    selection_mode: Literal["manual", "assisted", "autonomous"] = "assisted"
    weights: SelectionWeights = Field(default_factory=SelectionWeights)
    constraints: SelectionConstraints = Field(default_factory=SelectionConstraints)
    # Ask a model to review the diffs (maintainability, scalability, ...). "auto": when those factors are weighted.
    review: Literal["auto", "always", "never"] = "auto"

    @model_validator(mode="before")
    @classmethod
    def _accept_old_mode_names(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get("selection_mode") in SELECTION_MODE_ALIASES:
            data = dict(data)
            data["selection_mode"] = SELECTION_MODE_ALIASES[data["selection_mode"]]
        return data


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
    context: ContextSettings = Field(default_factory=ContextSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    mcp: McpSettings = Field(default_factory=McpSettings)
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
