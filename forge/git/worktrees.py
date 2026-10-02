"""Git worktrees for exploration candidates: the only place Forge changes Git state.

`GitRepository` is read-only by design. Exploration needs one kind of write:
creating and removing *linked worktrees*, separate checkouts that share the
repository's objects but have their own working directory, index, and
(detached) HEAD. Creating one does not touch the user's working tree,
index, branches, or HEAD; Git only records it under `.git/worktrees/`.

This module allows exactly `git worktree add --detach`, `remove`, `prune`,
and `list`, and only for paths inside the directory Forge was given for
candidate workspaces. Forge never commits in these worktrees.
"""

import os
import shutil
import subprocess
from pathlib import Path

from forge.git.repo import GitError, GitRepository

WORKTREE_TIMEOUT_SECONDS = 300


class WorktreeManager:
    def __init__(self, repo: GitRepository, base_dir: Path) -> None:
        self.repo = repo
        self.base_dir = Path(base_dir).resolve()

    def add(self, path: Path, commit: str) -> Path:
        """Check out `commit` (detached) into the new directory `path`."""
        target = self._inside_base(path)
        if target.exists():
            raise GitError(f"Worktree path already exists: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        self._run("add", "--detach", str(target), commit)
        return target

    def remove(self, path: Path) -> None:
        """Remove a worktree Forge created. Falls back to deleting the directory and pruning."""
        target = self._inside_base(path)
        try:
            self._run("remove", "--force", str(target))
        except GitError:
            shutil.rmtree(target, ignore_errors=True)
        self.prune()

    def prune(self) -> None:
        try:
            self._run("prune")
        except GitError:
            pass

    def list_paths(self) -> list[Path]:
        output = self._run("list", "--porcelain")
        return [Path(line[len("worktree ") :]) for line in output.splitlines() if line.startswith("worktree ")]

    def _inside_base(self, path: Path) -> Path:
        target = Path(path).resolve()
        if not target.is_relative_to(self.base_dir) or target == self.base_dir:
            raise GitError(f"Refusing to manage a worktree outside Forge's candidate directory: {target}")
        return target

    def _run(self, subcommand: str, *args: str) -> str:
        if subcommand not in {"add", "remove", "prune", "list"}:
            raise GitError(f"Refusing to run git worktree {subcommand}")
        environment = dict(os.environ)
        environment["GIT_TERMINAL_PROMPT"] = "0"
        try:
            completed = subprocess.run(
                ["git", "-c", "core.quotepath=false", "worktree", subcommand, *args],
                cwd=self.repo.root,
                capture_output=True,
                timeout=WORKTREE_TIMEOUT_SECONDS,
                env=environment,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise GitError(f"Could not run git worktree {subcommand}: {error}") from error
        if completed.returncode != 0:
            message = completed.stderr.decode("utf-8", errors="replace").strip()
            raise GitError(message or f"git worktree {subcommand} failed with exit code {completed.returncode}")
        return completed.stdout.decode("utf-8", errors="replace")
