"""Read-only access to a Git repository.

Forge only *reads* Git state. `GitRepository` refuses to run any git
subcommand that is not on a short read-only list, so nothing in this module
can reset, checkout, clean, commit, or push. Restoring files (see
`forge.tasks`) is done by Forge writing file contents itself, never by
rewriting Git state.
"""

import os
import subprocess
from pathlib import Path

from pydantic import BaseModel, Field

READ_ONLY_SUBCOMMANDS = frozenset({"rev-parse", "status", "diff", "show", "ls-files", "log", "cat-file"})
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"  # Git's well-known empty tree object
GIT_TIMEOUT_SECONDS = 30


class GitError(Exception):
    pass


class StatusEntry(BaseModel):
    path: str  # relative to the repository root, forward slashes
    index: str  # staged status letter (" " = unchanged)
    worktree: str  # unstaged status letter ("?" = untracked)
    original_path: str | None = None  # for renames and copies

    @property
    def untracked(self) -> bool:
        return self.index == "?"


class GitStatus(BaseModel):
    branch: str | None
    head: str | None  # None in a repository without commits
    entries: list[StatusEntry] = Field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.entries

    @property
    def modified(self) -> list[str]:
        return [entry.path for entry in self.entries if not entry.untracked]

    @property
    def untracked(self) -> list[str]:
        return [entry.path for entry in self.entries if entry.untracked]


class FileDiffStat(BaseModel):
    path: str
    additions: int | None  # None for binary files
    deletions: int | None


class DiffSummary(BaseModel):
    files: list[FileDiffStat] = Field(default_factory=list)
    additions: int = 0
    deletions: int = 0
    text: str = ""
    truncated: bool = False


class GitRepository:
    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()

    @classmethod
    def discover(cls, path: Path) -> "GitRepository | None":
        """The repository containing `path`, or None if it is not inside one (or git is unavailable)."""
        try:
            top = _run_git(Path(path), ["rev-parse", "--show-toplevel"]).strip()
        except GitError:
            return None
        return cls(Path(top)) if top else None

    def git(self, *args: str) -> str:
        return _run_git(self.root, list(args))

    def head(self) -> str | None:
        try:
            return self.git("rev-parse", "--verify", "--quiet", "HEAD").strip() or None
        except GitError:
            return None

    def branch(self) -> str | None:
        try:
            name = self.git("rev-parse", "--abbrev-ref", "HEAD").strip()
        except GitError:
            return None
        return None if name == "HEAD" else name

    def status(self) -> GitStatus:
        output = self.git("status", "--porcelain=v1", "-z", "--untracked-files=all")
        return GitStatus(branch=self.branch(), head=self.head(), entries=_parse_porcelain(output))

    def diff_summary(self, paths: list[str] | None = None, max_chars: int = 8000) -> DiffSummary:
        """Changes in the working tree (staged and unstaged) compared with HEAD. Untracked files are not included."""
        base = self.head() or EMPTY_TREE
        pathspec = ["--", *paths] if paths else []
        numstat = self.git("diff", base, "--numstat", "-z", *pathspec)
        files = _parse_numstat(numstat)
        text = self.git("diff", base, *pathspec)
        truncated = len(text) > max_chars
        if truncated:
            text = text[:max_chars] + f"\n[... diff truncated, {len(text) - max_chars} more characters ...]"
        return DiffSummary(
            files=files,
            additions=sum(f.additions or 0 for f in files),
            deletions=sum(f.deletions or 0 for f in files),
            text=text,
            truncated=truncated,
        )

    def head_content(self, path: str) -> bytes | None:
        """A file's content at HEAD, or None if it did not exist there."""
        if self.head() is None:
            return None
        try:
            return _run_git(self.root, ["show", f"HEAD:{path}"], binary=True)
        except GitError:
            return None


def _run_git(cwd: Path, args: list[str], binary: bool = False):
    if not args or args[0] not in READ_ONLY_SUBCOMMANDS:
        raise GitError(f"Refusing to run git {args[0] if args else ''}: only read-only commands are allowed")
    environment = dict(os.environ)
    # Don't let status/diff refresh and rewrite the index; keep this truly read-only.
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        completed = subprocess.run(
            ["git", "-c", "core.quotepath=false", *args],
            cwd=cwd,
            capture_output=True,
            timeout=GIT_TIMEOUT_SECONDS,
            env=environment,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise GitError(f"Could not run git: {error}") from error
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", errors="replace").strip()
        raise GitError(message or f"git {args[0]} failed with exit code {completed.returncode}")
    if binary:
        return completed.stdout
    return completed.stdout.decode("utf-8", errors="replace")


def _parse_porcelain(output: str) -> list[StatusEntry]:
    entries = []
    parts = output.split("\0")
    index = 0
    while index < len(parts):
        record = parts[index]
        index += 1
        if len(record) < 4:
            continue
        x, y, path = record[0], record[1], record[3:]
        original = None
        if x in "RC" or y in "RC":
            original = parts[index] if index < len(parts) else None
            index += 1
        entries.append(StatusEntry(path=path, index=x, worktree=y, original_path=original))
    return entries


def _parse_numstat(output: str) -> list[FileDiffStat]:
    files = []
    parts = output.split("\0")
    index = 0
    while index < len(parts):
        record = parts[index]
        index += 1
        if not record.strip():
            continue
        fields = record.split("\t")
        if len(fields) < 3:
            continue
        added, deleted, path = fields[0], fields[1], fields[2]
        if path == "":  # rename: the next two entries are the old and new paths
            path = parts[index + 1] if index + 1 < len(parts) else ""
            index += 2
        files.append(
            FileDiffStat(
                path=path,
                additions=None if added == "-" else int(added),
                deletions=None if deleted == "-" else int(deleted),
            )
        )
    return files
