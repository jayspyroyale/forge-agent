"""Layered configuration loading.

Lowest to highest priority:

    defaults -> profile -> user config -> project config -> environment -> CLI

    user config:     ~/.forge/config.toml        (or $FORGE_HOME/config.toml)
    project config:  <workspace>/.forge/config.toml

Every value remembers which layer set it, so `forge config show` can explain
where each setting came from. TOML is read with the standard library's
`tomllib`, so this adds no dependency.
"""

import os
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from forge.config.profiles import BUILTIN_PROFILES, DEFAULT_PROFILE
from forge.config.schema import LEGACY_KEYS, ForgeConfig
from forge.security.secret_scan import find_secrets

# Environment variable -> dotted config key.
ENV_KEYS: dict[str, str] = {
    "FORGE_PROVIDER": "model.provider",
    "FORGE_MODEL": "model.name",
    "FORGE_BASE_URL": "model.base_url",
    "FORGE_TEMPERATURE": "model.temperature",
    "FORGE_TIMEOUT": "model.timeout",
    "FORGE_MAX_STEPS": "agent.max_steps",
    "FORGE_VERIFICATION": "agent.verification",
    "FORGE_DEBUG": "ui.debug",
    "FORGE_VERBOSE": "ui.verbose",
    "FORGE_PROFILE": "profile",
}
# Dotted config key -> environment variable (the older shape, kept for callers that use it).
ENV_VARS: dict[str, str] = {key: env for env, key in ENV_KEYS.items()}

# "token" only as a whole name or suffix (auth_token, GITHUB_TOKEN), so settings like max_tokens are not secrets.
SECRET_KEY = re.compile(r"(api_?key|(^|_)token$|secret|password|passwd|credential)", re.IGNORECASE)
ENV_REFERENCE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}")
_URL_USERINFO = re.compile(r"(?<=://)[^/@\s]+@")


class ConfigError(Exception):
    """A config file or value is invalid. The message says where."""


class ConfigFile(BaseModel):
    layer: Literal["user", "project"]
    path: Path
    exists: bool


class LoadedConfig(BaseModel):
    config: ForgeConfig
    sources: dict[str, str]  # dotted key -> "default", "profile:x", "user:<path>", "project:<path>", "env:NAME", "cli"
    files: list[ConfigFile]
    profile: str


def user_config_dir(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    return Path(env["FORGE_HOME"]) if env.get("FORGE_HOME") else Path.home() / ".forge"


def user_config_path(env: Mapping[str, str] | None = None) -> Path:
    return user_config_dir(env) / "config.toml"


def project_config_path(workspace: Path) -> Path:
    return workspace / ".forge" / "config.toml"


def load_config(
    *,
    workspace: Path | None = None,
    cli: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
    include_files: bool = True,
) -> LoadedConfig:
    """Build the effective configuration. `cli` uses dotted keys, e.g. {"agent.max_steps": 5}."""
    env = os.environ if env is None else env
    cli_values = {key: value for key, value in (cli or {}).items() if value is not None}
    root = Path(cli_values.get("workspace.root") or workspace or Path.cwd()).resolve()

    files = [
        ConfigFile(layer="user", path=user_config_path(env), exists=False),
        ConfigFile(layer="project", path=project_config_path(root), exists=False),
    ]
    file_data: dict[str, dict[str, Any]] = {}
    if include_files:
        for config_file in files:
            config_file.exists = config_file.path.is_file()
            if config_file.exists:
                file_data[config_file.layer] = _read_toml(config_file.path, config_file.layer)

    env_values = {key: env[name] for name, key in ENV_KEYS.items() if env.get(name)}

    user, project = file_data.get("user", {}), file_data.get("project", {})
    profiles = {**BUILTIN_PROFILES, **user.get("profiles", {}), **project.get("profiles", {})}
    user_path, project_path = files[0].path, files[1].path
    profile, profile_source = DEFAULT_PROFILE, "default"
    for candidate, source in (
        (user.get("profile"), f"user:{user_path}"),
        (project.get("profile"), f"project:{project_path}"),
        (env_values.get("profile"), "env:FORGE_PROFILE"),
        (cli_values.get("profile"), "cli"),
    ):
        if candidate:
            profile, profile_source = candidate, source
    if profile not in profiles:
        raise ConfigError(f"Unknown profile '{profile}'. Available profiles: {', '.join(sorted(profiles))}")

    merged: dict[str, Any] = {}
    sources: dict[str, str] = {}
    _apply(merged, sources, ForgeConfig().model_dump(mode="json"), "default")
    _apply(merged, sources, profiles[profile], f"profile:{profile}")
    for layer, data, path in (("user", user, user_path), ("project", project, project_path)):
        _apply(merged, sources, _without(data, "profiles"), f"{layer}:{path}")
    _apply(
        merged, sources, _unflatten(env_values), None, env_sources={key: f"env:{ENV_VARS[key]}" for key in env_values}
    )
    _apply(merged, sources, _unflatten(cli_values), "cli")
    merged["profile"] = profile
    sources["profile"] = profile_source
    if "workspace.root" not in cli_values and not _get(merged, "workspace.root"):
        merged.setdefault("workspace", {})["root"] = str(root)

    try:
        config = ForgeConfig.model_validate(merged)
    except ValidationError as error:
        raise ConfigError(_explain(error, sources)) from None
    return LoadedConfig(config=config, sources=sources, files=files, profile=profile)


def overrides_to_dotted(overrides: Mapping[str, Any]) -> dict[str, Any]:
    """Translate legacy flat override names (provider=, model=, max_steps=, ...) into dotted keys."""
    dotted: dict[str, Any] = {}
    for key, value in overrides.items():
        if value is None:
            continue
        if key == "model" and not isinstance(value, Mapping):
            dotted["model.name"] = value
        elif key == "workspace":
            dotted["workspace.root"] = str(value)
        elif key in LEGACY_KEYS:
            section, field = LEGACY_KEYS[key]
            dotted[f"{section}.{field}"] = value
        else:
            dotted[key] = value
    return dotted


def flatten(data: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for key, value in data.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, Mapping) and value:
            flat.update(flatten(value, dotted + "."))
        else:
            flat[dotted] = value
    return flat


def is_env_reference(value: Any) -> bool:
    """True for values like "${GITHUB_TOKEN}": a pointer to a secret, not the secret itself."""
    return isinstance(value, str) and bool(ENV_REFERENCE.fullmatch(value.strip()))


def redact(key: str, value: Any) -> Any:
    """Hide anything that looks secret: secret-named keys, and credentials inside URLs."""
    if value is None:
        return None
    if SECRET_KEY.search(key.split(".")[-1]) and not is_env_reference(value):
        return "***"
    if isinstance(value, str):
        return _URL_USERINFO.sub("***@", value)
    return value


# --- helpers ---------------------------------------------------------------------


def _read_toml(path: Path, layer: str) -> dict[str, Any]:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path}: invalid TOML: {error}") from None
    except (OSError, UnicodeDecodeError) as error:
        raise ConfigError(f"{path}: cannot be read: {error}") from None
    secrets = [
        key
        for key, value in flatten(data).items()
        if (SECRET_KEY.search(key.split(".")[-1]) and not is_env_reference(value))
        # Legacy user URLs remain supported; committed project URLs must not hold credentials.
        or (
            isinstance(value, str) and set(find_secrets(value)) - ({"credentials in URL"} if layer == "user" else set())
        )
    ]
    if secrets:
        where = "project config (it may be committed to Git)" if layer == "project" else "config files"
        raise ConfigError(
            f"{path}: secrets must not be stored in {where}: {', '.join(secrets)}. "
            "Use environment variables such as OPENAI_API_KEY instead."
        )
    return data


def _apply(
    target: dict[str, Any],
    sources: dict[str, str],
    layer: Mapping[str, Any],
    source: str | None,
    env_sources: dict[str, str] | None = None,
) -> None:
    for key, value in flatten(layer).items():
        _set(target, key, value)
        sources[key] = env_sources[key] if env_sources else source  # type: ignore[assignment]


def _set(target: dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    node = target
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value


def _get(data: Mapping[str, Any], dotted: str) -> Any:
    node: Any = data
    for part in dotted.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node


def _unflatten(values: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in values.items():
        _set(result, key, value)
    return result


def _without(data: Mapping[str, Any], key: str) -> dict[str, Any]:
    return {k: v for k, v in data.items() if k != key}


def _explain(error: ValidationError, sources: dict[str, str]) -> str:
    problems = []
    for item in error.errors():
        dotted = ".".join(str(part) for part in item["loc"])
        source = sources.get(dotted)
        where = f" (from {source})" if source else ""
        message = "unknown setting" if item["type"] == "extra_forbidden" else item["msg"]
        problems.append(f"{dotted}: {message}{where}")
    return "Invalid configuration: " + "; ".join(problems)
