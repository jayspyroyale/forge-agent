"""The project directory Forge is allowed to touch.

All path handling for tools goes through `Workspace`, so the safety rules live
in exactly one place:

- every path is resolved (`..` and symbolic links included) *before* it is
  checked, so `../../secret.txt` or a symlink pointing outside are rejected;
- absolute paths are allowed only if they resolve inside the workspace;
- some paths inside the workspace are protected from writes (`.git`, and the
  task records Forge keeps under `.forge/tasks`).
"""

import os
from collections.abc import Iterator
from pathlib import Path

DEFAULT_PROTECTED_PATHS = (".git", ".forge/tasks")

# Directories that are noise for listing and searching.
DEFAULT_IGNORED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".nox",
    }
)


class WorkspaceError(Exception):
    """A path is outside the workspace or not allowed for the requested use."""


class Workspace:
    def __init__(
        self,
        root: str | Path,
        protected_paths: tuple[str, ...] = DEFAULT_PROTECTED_PATHS,
        ignored_dirs: frozenset[str] = DEFAULT_IGNORED_DIRS,
    ) -> None:
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise WorkspaceError(f"Workspace is not a directory: {self.root}")
        self.protected_paths = tuple(Path(p) for p in protected_paths)
        self.ignored_dirs = ignored_dirs

    def resolve(self, path: str | Path) -> Path:
        """Return the absolute, fully resolved path, or raise if it leaves the workspace."""
        raw = Path(path)
        candidate = raw if raw.is_absolute() else self.root / raw
        # resolve() follows symlinks and collapses "..", so the containment
        # check below sees where the path really points.
        resolved = candidate.resolve()
        if not resolved.is_relative_to(self.root):
            raise WorkspaceError(f"Path '{path}' is outside the workspace")
        return resolved

    def relative(self, path: str | Path) -> str:
        """A workspace-relative path with forward slashes, for display and for the model."""
        resolved = self.resolve(path)
        relative = resolved.relative_to(self.root).as_posix()
        return relative or "."

    def is_protected(self, path: str | Path) -> bool:
        relative = self.resolve(path).relative_to(self.root)
        return any(relative.is_relative_to(protected) for protected in self.protected_paths)

    def ensure_writable(self, path: str | Path) -> Path:
        """Resolve a path that is about to be written, rejecting protected locations."""
        resolved = self.resolve(path)
        if self.is_protected(resolved):
            raise WorkspaceError(f"Path '{path}' is protected and cannot be modified")
        return resolved

    def is_ignored(self, path: str | Path) -> bool:
        relative = self.resolve(path).relative_to(self.root)
        if any(part in self.ignored_dirs for part in relative.parts):
            return True
        return any(relative.is_relative_to(protected) for protected in self.protected_paths)

    def walk_files(self, start: str | Path = ".") -> Iterator[Path]:
        """Yield files under `start` in a stable order, skipping ignored directories."""
        start_path = self.resolve(start)
        if start_path.is_file():
            yield start_path
            return
        for directory, dirnames, filenames in os.walk(start_path):
            current = Path(directory)
            # Pruning dirnames in place stops os.walk from descending into them.
            dirnames[:] = sorted(d for d in dirnames if not self._skip_dir(current / d))
            for filename in sorted(filenames):
                yield current / filename

    def _skip_dir(self, path: Path) -> bool:
        if path.is_symlink():
            # Never follow directory symlinks while walking; they may point outside.
            return True
        return self.is_ignored(path)
