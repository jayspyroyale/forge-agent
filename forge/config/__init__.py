"""Forge configuration: the schema, profiles, and layered loading.

All configuration lives here so the rest of the code receives a single
`ForgeConfig` object instead of reading settings from many places. Secrets
such as API keys are deliberately NOT part of it: each provider reads its own
key from the environment.
"""

from forge.config.loader import (
    ENV_KEYS,
    ENV_VARS,
    ConfigError,
    LoadedConfig,
    load_config,
    project_config_path,
    redact,
    user_config_path,
)
from forge.config.schema import (
    AgentSettings,
    ExplorationSettings,
    ForgeConfig,
    ModelSettings,
    TerminalSettings,
    UISettings,
    WorkspaceSettings,
)

__all__ = [
    "ENV_KEYS",
    "ENV_VARS",
    "AgentSettings",
    "ConfigError",
    "ExplorationSettings",
    "ForgeConfig",
    "LoadedConfig",
    "ModelSettings",
    "TerminalSettings",
    "UISettings",
    "WorkspaceSettings",
    "load_config",
    "project_config_path",
    "redact",
    "user_config_path",
]
