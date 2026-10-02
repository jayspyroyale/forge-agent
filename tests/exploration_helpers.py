"""Shared setup for exploration tests: temporary Git repos and scripted per-candidate models."""

import hashlib
import json
import subprocess
from pathlib import Path

from forge.config import ForgeConfig
from forge.models.providers.fake import FakeModelProvider
from forge.models.types import ModelResponse, ToolCall
from forge.security.permissions import PermissionEngine
from forge.security.policy import PermissionPolicy

BUG = "def multiply(a, b):\n    return a + b  # BUG: should be a * b"
FIX = "def multiply(a, b):\n    return a * b"
WRONG = "def multiply(a, b):\n    return a - b"


def git(cwd, *args):
    return subprocess.run(
        ["git", "-c", "user.name=Forge Test", "-c", "user.email=test@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def init_repo(root: Path) -> Path:
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "core.autocrlf", "false")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "initial")
    return root


def tree_hashes(root: Path) -> dict[str, str]:
    """Every file under root except .git and Forge's own records, with its hash."""
    hashes = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_file() and not relative.startswith((".git/", ".forge/")) and "__pycache__" not in relative:
            hashes[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def tool(call_id, name, **arguments):
    return ModelResponse(tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)], finish_reason="tool_calls")


def edit(old, new, call_id="e1", path="calculator.py"):
    return tool(call_id, "edit_file", path=path, old_text=old, new_text=new)


def plans_reply(*titles, extra=None):
    approaches = [
        {
            "title": title,
            "summary": f"{title}. " * 3,  # distinct per title, so the planner does not treat plans as duplicates
            "files": ["calculator.py"],
            "assumptions": [],
            "risks": [],
            "dependencies": [],
            "complexity": "low",
        }
        for title in titles
    ]
    approaches += extra or []
    return json.dumps({"approaches": approaches})


class Scripts:
    """A provider factory: the planner and each candidate (by workspace directory name) get their own script."""

    def __init__(self, project: Path, scripts: dict[str, list], crash: set[str] = frozenset()) -> None:
        self.project = project.resolve()
        self.scripts = scripts
        self.crash = crash
        self.providers: dict[str, FakeModelProvider] = {}
        self.configs: dict[str, ForgeConfig] = {}

    def __call__(self, config: ForgeConfig) -> FakeModelProvider:
        root = config.workspace_root
        key = "planner" if root == self.project else root.name
        if key in self.crash:
            raise RuntimeError(f"candidate {key} blew up")
        if key == "planner" and "planner" in self.providers:
            key = "planner2"
        self.configs[key] = config
        provider = FakeModelProvider(config, responses=list(self.scripts.get(key, [])))
        self.providers[key] = provider
        return provider


def explore_config(project: Path, tmp_path: Path, **overrides) -> ForgeConfig:
    settings = {
        "workspace": project,
        "verification_attempts": 1,
        "exploration": {"workspace_dir": str(tmp_path / "worktrees")},
        "memory": {"enabled": False},
    }
    settings.update(overrides)
    return ForgeConfig(**settings)


def permissive() -> PermissionEngine:
    return PermissionEngine(PermissionPolicy.permissive())
