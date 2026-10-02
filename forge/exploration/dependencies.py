"""Which third-party dependencies a candidate added, read from dependency manifests.

Compares the declared dependencies before and after in the files projects
actually use: pyproject.toml, requirements*.txt, package.json, Cargo.toml,
and go.mod. Names are normalized so that `Requests>=2` and `requests` match.
"""

import json
import re
import tomllib
from collections.abc import Callable
from pathlib import PurePosixPath

_REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_GO_REQUIRE = re.compile(r"^\s*(?:require\s+)?([\w.-]+\.[\w./-]+)\s+v[\w.+-]+", re.MULTILINE)


def is_manifest(path: str) -> bool:
    name = PurePosixPath(path).name
    return name in {"pyproject.toml", "package.json", "Cargo.toml", "go.mod"} or (
        name.startswith("requirements") and name.endswith(".txt")
    )


def declared(path: str, content: bytes | None) -> set[str]:
    """Dependencies declared in one manifest file, as 'ecosystem:name'."""
    if not content:
        return set()
    text = content.decode("utf-8", errors="replace")
    name = PurePosixPath(path).name
    try:
        if name == "pyproject.toml":
            return {f"python:{dep}" for dep in _pyproject(tomllib.loads(text))}
        if name.startswith("requirements"):
            return {f"python:{dep}" for dep in _requirements(text)}
        if name == "package.json":
            data = json.loads(text)
            sections = ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies")
            return {f"node:{dep}" for section in sections for dep in (data.get(section) or {})}
        if name == "Cargo.toml":
            data = tomllib.loads(text)
            sections = ("dependencies", "dev-dependencies", "build-dependencies")
            return {f"rust:{dep}" for section in sections for dep in (data.get(section) or {})}
        if name == "go.mod":
            return {f"go:{module}" for module in _GO_REQUIRE.findall(text)}
    except (ValueError, tomllib.TOMLDecodeError, AttributeError, TypeError):
        return set()  # an unparsable manifest is reported by verification, not here
    return set()


def added_dependencies(paths: list[str], before: Callable[[str], bytes | None], after: Callable[[str], bytes | None]) -> list[str]:
    added: set[str] = set()
    for path in paths:
        if is_manifest(path):
            added |= declared(path, after(path)) - declared(path, before(path))
    return sorted(added)


def _pyproject(data: dict) -> set[str]:
    project = data.get("project", {})
    requirements = list(project.get("dependencies", []))
    for group in (project.get("optional-dependencies") or {}).values():
        requirements += list(group)
    for group in (data.get("dependency-groups") or {}).values():
        requirements += [item for item in group if isinstance(item, str)]
    names = {_normalize(match.group(1)) for item in requirements if (match := _REQUIREMENT_NAME.match(str(item)))}
    poetry = data.get("tool", {}).get("poetry", {})
    for section in ("dependencies", "dev-dependencies"):
        names |= {_normalize(dep) for dep in (poetry.get(section) or {}) if dep.lower() != "python"}
    return names


def _requirements(text: str) -> set[str]:
    names = set()
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        match = _REQUIREMENT_NAME.match(line)
        if match:
            names.add(_normalize(match.group(1)))
    return names


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()
