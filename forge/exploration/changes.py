"""What a candidate changed, measured by Forge from the files themselves.

Before a candidate's agent starts, Forge hashes every file in its workspace
(the manifest). Afterwards it hashes them again. The difference is the
candidate's change set, whether the agent made it with edit tools or with
commands. Caches and dependency folders (`.venv`, `node_modules`,
`__pycache__`, ...) and Forge's own `.forge/` records are not part of it.
"""

import difflib
import hashlib
import os
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from forge.fileio import looks_binary
from forge.tasks.snapshot import line_stats
from forge.workspace import DEFAULT_IGNORED_DIRS

EXCLUDED_DIRS = DEFAULT_IGNORED_DIRS | {".forge", ".git"}
MAX_PATCH_CHARS = 400_000


class FileDelta(BaseModel):
    path: str  # workspace-relative, forward slashes
    kind: Literal["added", "modified", "deleted"]
    additions: int | None = None  # None for binary files
    deletions: int | None = None
    baseline_hash: str | None = None  # None: did not exist at the start
    final_hash: str | None = None  # None: deleted

    @property
    def binary(self) -> bool:
        return self.additions is None


def manifest(root: Path) -> dict[str, str]:
    """Workspace-relative path -> SHA-256 for every file that is part of the project."""
    root = Path(root)
    hashes: dict[str, str] = {}
    for directory, dirnames, filenames in os.walk(root):
        current = Path(directory)
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDED_DIRS and not (current / d).is_symlink())
        for filename in sorted(filenames):
            path = current / filename
            if path.is_symlink() or not path.is_file():
                continue
            hashes[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def compare(before: dict[str, str], root: Path) -> list[FileDelta]:
    """Changed files between the `before` manifest and the workspace now (line counts filled in later)."""
    after = manifest(root)
    deltas = []
    for path in sorted(set(before) | set(after)):
        old, new = before.get(path), after.get(path)
        if old == new:
            continue
        kind = "added" if old is None else "deleted" if new is None else "modified"
        deltas.append(FileDelta(path=path, kind=kind, baseline_hash=old, final_hash=new))
    return deltas


def describe(
    deltas: list[FileDelta], root: Path, baseline: Callable[[str], bytes | None]
) -> tuple[list[FileDelta], str]:
    """Fill in line counts and build a unified diff (for people to read; Forge applies files, not this patch)."""
    described = []
    patch_parts = []
    for delta in deltas:
        old = baseline(delta.path) if delta.kind != "added" else b""
        new = (Path(root) / delta.path).read_bytes() if delta.kind != "deleted" else b""
        if old is None:
            old = b""
        additions, deletions = line_stats(old, new)
        described.append(delta.model_copy(update={"additions": additions, "deletions": deletions}))
        patch_parts.append(_patch(delta, old, new))
    patch = "".join(patch_parts)
    if len(patch) > MAX_PATCH_CHARS:
        patch = patch[:MAX_PATCH_CHARS] + f"\n[... patch truncated, {len(patch) - MAX_PATCH_CHARS} more characters ...]\n"
    return described, patch


def _patch(delta: FileDelta, old: bytes, new: bytes) -> str:
    a = "/dev/null" if delta.kind == "added" else f"a/{delta.path}"
    b = "/dev/null" if delta.kind == "deleted" else f"b/{delta.path}"
    if looks_binary(old) or looks_binary(new):
        return f"--- {a}\n+++ {b}\nBinary files differ\n"
    old_lines = old.decode("utf-8", errors="replace").splitlines(keepends=True)
    new_lines = new.decode("utf-8", errors="replace").splitlines(keepends=True)
    lines = list(difflib.unified_diff(old_lines, new_lines, fromfile=a, tofile=b))
    return "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in lines)
