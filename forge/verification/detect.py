"""Find verification checks from a project's own configuration files.

Forge never guesses: a check is only returned when a file in the project
shows that the tool is set up (a tests directory, a [tool.ruff] table, a
"test" script in package.json, a Cargo.toml, ...).
"""

import json
import sys
import tomllib
from pathlib import Path
from typing import Any

from forge.verification.checks import VerificationCheck

# npm's placeholder "test" script; it always fails and verifies nothing.
_NPM_PLACEHOLDER_TEST = 'echo "Error: no test specified" && exit 1'


def detect_checks(root: Path, environment_root: Path | None = None) -> list[VerificationCheck]:
    """Checks for the project at `root`.

    `environment_root` is where to look for the project's virtual environment
    when it is not inside `root` (exploration candidates are copies of the
    project without its `.venv`, so they use the original project's).
    """
    checks: list[VerificationCheck] = []
    checks += _python_checks(root, environment_root or root)
    checks += _node_checks(root)
    checks += _rust_checks(root)
    checks += _go_checks(root)
    return checks


# --- Python --------------------------------------------------------------------


def _python_checks(root: Path, environment_root: Path) -> list[VerificationCheck]:
    pyproject = _read_toml(root / "pyproject.toml")
    tool = pyproject.get("tool", {}) if pyproject else {}
    python = _python_command(root) if (root / ".venv").is_dir() else _python_command(environment_root)
    checks = []

    pytest_reason = _pytest_reason(root, tool)
    if pytest_reason:
        checks.append(
            VerificationCheck(name="pytest", kind="test", command=f"{python} -m pytest -q", reason=pytest_reason)
        )
    if "mypy" in tool or (root / "mypy.ini").is_file():
        checks.append(
            VerificationCheck(name="mypy", kind="typecheck", command=f"{python} -m mypy .", reason="mypy is configured")
        )
    if "ruff" in tool or (root / "ruff.toml").is_file() or (root / ".ruff.toml").is_file():
        checks.append(
            VerificationCheck(name="ruff", kind="lint", command=f"{python} -m ruff check .", reason="ruff is configured")
        )
    return checks


def _pytest_reason(root: Path, tool: dict[str, Any]) -> str | None:
    if "pytest" in tool:
        return "pyproject.toml has [tool.pytest]"
    for name in ("pytest.ini", "conftest.py"):
        if (root / name).is_file():
            return f"{name} found"
    for directory in (root / "tests", root / "test", root):
        if directory.is_dir() and any(directory.glob("test_*.py")):
            relative = directory.relative_to(root).as_posix()
            return f"test files found in {relative if relative != '.' else 'the project root'}"
    return None


def _python_command(root: Path) -> str:
    """The project's own virtual environment if it has one, otherwise the Python running Forge."""
    for candidate in (root / ".venv" / "Scripts" / "python.exe", root / ".venv" / "bin" / "python"):
        if candidate.is_file():
            return f'"{candidate}"'
    return f'"{sys.executable}"'


# --- Node ----------------------------------------------------------------------


def _node_checks(root: Path) -> list[VerificationCheck]:
    package = root / "package.json"
    if not package.is_file():
        return []
    try:
        scripts = json.loads(package.read_text(encoding="utf-8")).get("scripts", {})
    except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
        return []
    if not isinstance(scripts, dict):
        return []

    manager = _node_package_manager(root)
    checks = []
    test_script = scripts.get("test")
    if test_script and test_script.strip() != _NPM_PLACEHOLDER_TEST:
        checks.append(VerificationCheck(name=f"{manager} test", kind="test", command=f"{manager} test", reason="package.json has a test script"))
    for script, kind in (("typecheck", "typecheck"), ("type-check", "typecheck"), ("lint", "lint"), ("build", "build")):
        if script in scripts and not any(check.kind == kind for check in checks):
            checks.append(
                VerificationCheck(
                    name=f"{manager} run {script}",
                    kind=kind,
                    command=f"{manager} run {script}",
                    reason=f"package.json has a {script} script",
                )
            )
    return checks


def _node_package_manager(root: Path) -> str:
    if (root / "pnpm-lock.yaml").is_file():
        return "pnpm"
    if (root / "yarn.lock").is_file():
        return "yarn"
    if (root / "bun.lockb").is_file() or (root / "bun.lock").is_file():
        return "bun"
    return "npm"


# --- Rust and Go ---------------------------------------------------------------


def _rust_checks(root: Path) -> list[VerificationCheck]:
    if not (root / "Cargo.toml").is_file():
        return []
    return [
        VerificationCheck(name="cargo test", kind="test", command="cargo test", reason="Cargo.toml found"),
        VerificationCheck(name="cargo build", kind="build", command="cargo build", reason="Cargo.toml found"),
    ]


def _go_checks(root: Path) -> list[VerificationCheck]:
    if not (root / "go.mod").is_file():
        return []
    return [
        VerificationCheck(name="go test", kind="test", command="go test ./...", reason="go.mod found"),
        VerificationCheck(name="go vet", kind="lint", command="go vet ./...", reason="go.mod found"),
    ]


def _read_toml(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        return None
