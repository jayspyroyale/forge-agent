"""Local environment checks for `forge doctor`.

Each check is a small function that returns a CheckResult. The checks never
print anything; displaying results is the CLI's job. They also never touch
the network.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

from pydantic import BaseModel

import forge
from forge.config import ConfigError, load_config

MIN_PYTHON = (3, 12)


class CheckResult(BaseModel):
    """The outcome of one doctor check."""

    name: str
    passed: bool
    detail: str = ""


def check_python_version() -> CheckResult:
    version = sys.version_info
    version_text = f"{version.major}.{version.minor}.{version.micro}"
    passed = (version.major, version.minor) >= MIN_PYTHON
    detail = "" if passed else f"Forge requires Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+"
    return CheckResult(name=f"Python {version_text}", passed=passed, detail=detail)


def check_package() -> CheckResult:
    return CheckResult(
        name="Forge package loaded",
        passed=True,
        detail=f"v{forge.__version__}",
    )


def check_workspace(path: Path | None = None) -> CheckResult:
    workspace = path if path is not None else Path.cwd()
    passed = workspace.is_dir() and os.access(workspace, os.R_OK | os.W_OK)
    detail = str(workspace) if passed else f"Cannot read and write {workspace}"
    return CheckResult(name="Workspace accessible", passed=passed, detail=detail)


def check_git() -> CheckResult:
    git_path = shutil.which("git")
    if git_path is None:
        return CheckResult(name="Git installed", passed=False, detail="git not found on PATH")

    try:
        completed = subprocess.run(
            [git_path, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return CheckResult(name="Git installed", passed=False, detail=str(error))

    return CheckResult(name="Git installed", passed=True, detail=completed.stdout.strip())


def check_config() -> CheckResult:
    try:
        config = load_config().config
    except ConfigError as error:
        return CheckResult(name="Configuration valid", passed=False, detail=str(error))
    return CheckResult(
        name="Configuration valid",
        passed=True,
        detail=f"profile={config.profile}, provider={config.model.provider}, model={config.model.name or 'provider default'}",
    )


def run_all_checks() -> list[CheckResult]:
    return [
        check_python_version(),
        check_package(),
        check_workspace(),
        check_git(),
        check_config(),
    ]
