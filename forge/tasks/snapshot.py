"""What the workspace looked like when a task started, and what the task changed.

At task start Forge records:
- in a Git repository: HEAD, the branch, and a content hash of every file
  that was already modified or untracked (the user's pre-existing changes);
- outside Git: a content hash of every (non-ignored) file.

At the end it compares the workspace against that snapshot and classifies
every changed file:

    task          clean at start, changed now           -> Forge's change
    pre_existing  already changed at start, untouched   -> the user's change, left alone
    mixed         already changed at start, changed again by the task
"""

import difflib
import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from forge.fileio import looks_binary
from forge.git.repo import GitRepository
from forge.workspace import Workspace

MAX_SNAPSHOT_FILES = 20_000

Origin = Literal["task", "pre_existing", "mixed"]
ChangeKind = Literal["added", "modified", "deleted"]


def file_hash(path: Path) -> str | None:
    """SHA-256 of a file's bytes, or None if it does not exist."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
        return None


class WorkspaceSnapshot(BaseModel):
    taken_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    git: bool
    head: str | None = None
    branch: str | None = None
    # Workspace-relative path -> content hash (None = the file did not exist).
    # In Git mode: only files that were already dirty or untracked.
    # Outside Git: every file in the workspace.
    files: dict[str, str | None] = Field(default_factory=dict)


class FileChange(BaseModel):
    path: str
    origin: Origin
    kind: ChangeKind
    additions: int | None = None  # None when unknown (binary, or no earlier version available)
    deletions: int | None = None
    before_hash: str | None = None  # at task start (None = did not exist)
    after_hash: str | None = None  # at task end (None = deleted)


class ChangeReport(BaseModel):
    git: bool
    head_at_start: str | None = None
    head_at_end: str | None = None
    branch: str | None = None
    changes: list[FileChange] = Field(default_factory=list)  # task and mixed
    pre_existing: list[str] = Field(default_factory=list)  # dirty at start, untouched by the task

    @property
    def head_moved(self) -> bool:
        return self.git and self.head_at_start != self.head_at_end

    @property
    def changed_paths(self) -> list[str]:
        return [change.path for change in self.changes]

    @property
    def additions(self) -> int:
        return sum(change.additions or 0 for change in self.changes)

    @property
    def deletions(self) -> int:
        return sum(change.deletions or 0 for change in self.changes)


def take_snapshot(workspace: Workspace, repo: GitRepository | None) -> WorkspaceSnapshot:
    if repo is None:
        files = {}
        for path in workspace.walk_files():
            if len(files) >= MAX_SNAPSHOT_FILES:
                break
            files[workspace.relative(path)] = file_hash(path)
        return WorkspaceSnapshot(git=False, files=files)

    status = repo.status()
    files = {}
    for repo_path in _dirty_paths(status):
        relative = _to_workspace(repo, workspace, repo_path)
        if relative is not None:
            files[relative] = file_hash(workspace.root / relative)
    return WorkspaceSnapshot(git=True, head=status.head, branch=status.branch, files=files)


def compute_changes(
    start: WorkspaceSnapshot,
    workspace: Workspace,
    repo: GitRepository | None,
    pre_images: dict[str, bytes | None] | None = None,
) -> ChangeReport:
    """Compare the workspace now with `start`. `pre_images` are file contents saved before the task wrote them."""
    pre_images = pre_images or {}
    end = take_snapshot(workspace, repo)
    candidates = sorted(set(start.files) | set(end.files))

    changes: list[FileChange] = []
    pre_existing: list[str] = []
    for path in candidates:
        now = file_hash(workspace.root / path)
        if path in start.files:
            before = start.files[path]
            if now == before:
                if start.git:
                    pre_existing.append(path)
                continue
            origin: Origin = "mixed" if start.git else "task"
        else:
            # Clean at start: outside Git that means the file is new; in Git it matched HEAD.
            before = _head_hash(repo, workspace, path) if start.git else None
            if now == before:
                continue
            origin = "task"
        changes.append(_describe(path, origin, before, now, workspace, repo, pre_images))

    return ChangeReport(
        git=start.git,
        head_at_start=start.head,
        head_at_end=end.head,
        branch=end.branch,
        changes=changes,
        pre_existing=pre_existing,
    )


def _describe(path, origin, before, now, workspace, repo, pre_images) -> FileChange:
    if before is None:
        kind: ChangeKind = "added"
    elif now is None:
        kind = "deleted"
    else:
        kind = "modified"

    old = _pre_image(path, origin, before, workspace, repo, pre_images)
    current_path = workspace.root / path
    new = current_path.read_bytes() if now is not None else b""
    additions = deletions = None
    if old is not None:
        additions, deletions = line_stats(old, new)
    return FileChange(
        path=path, origin=origin, kind=kind, additions=additions, deletions=deletions, before_hash=before, after_hash=now
    )


def _pre_image(path, origin, before, workspace, repo, pre_images) -> bytes | None:
    """The file's content at task start, if Forge can know it."""
    if before is None:
        return b""
    if path in pre_images and pre_images[path] is not None:
        return pre_images[path]
    if origin == "task" and repo is not None:
        return repo.head_content(_to_repo(repo, workspace, path))
    return None


def line_stats(old: bytes, new: bytes) -> tuple[int | None, int | None]:
    if looks_binary(old) or looks_binary(new):
        return None, None
    old_lines = old.decode("utf-8", errors="replace").splitlines()
    new_lines = new.decode("utf-8", errors="replace").splitlines()
    additions = deletions = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False).get_opcodes():
        if tag in ("replace", "delete"):
            deletions += i2 - i1
        if tag in ("replace", "insert"):
            additions += j2 - j1
    return additions, deletions


def _head_hash(repo, workspace, path) -> str | None:
    if repo is None:
        return None
    content = repo.head_content(_to_repo(repo, workspace, path))
    return hashlib.sha256(content).hexdigest() if content is not None else None


def _dirty_paths(status) -> list[str]:
    paths = []
    for entry in status.entries:
        paths.append(entry.path)
        if entry.original_path:
            paths.append(entry.original_path)
    return paths


def _to_workspace(repo: GitRepository, workspace: Workspace, repo_path: str) -> str | None:
    absolute = (repo.root / repo_path).resolve()
    if not absolute.is_relative_to(workspace.root):
        return None  # changes elsewhere in the repository are not this task's business
    if workspace.is_ignored(absolute):
        return None
    return absolute.relative_to(workspace.root).as_posix()


def _to_repo(repo: GitRepository, workspace: Workspace, path: str) -> str:
    return (workspace.root / path).resolve().relative_to(repo.root).as_posix()
