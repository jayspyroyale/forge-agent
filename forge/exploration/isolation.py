"""Isolated workspaces for exploration candidates.

Every candidate works in its own directory outside the user's project, so
built-in filesystem tools are scoped to that candidate. This is workspace
separation, not an operating-system sandbox for shell commands:

    Git project      a linked worktree per candidate, checked out at HEAD
                     (`git worktree add --detach`); the user's working tree,
                     index, branches and HEAD are not touched
    other projects   a copy of the project per candidate

The *baseline* is what every candidate starts from: by default, the
project exactly as the user has it, including uncommitted changes (they are
copied into each worktree; nothing is committed). Files Git ignores are not
copied (they often hold secrets such as `.env`).

Before anything is created, `preflight` refuses states where "the user's
project as it is" is ambiguous: a merge or rebase in progress, unresolved
conflicts, or a repository without commits.
"""

import shutil
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from forge.fileio import atomic_write_bytes
from forge.git.repo import GitError, GitRepository
from forge.git.worktrees import WorktreeManager
from forge.workspace import DEFAULT_IGNORED_DIRS, Workspace

COPY_EXCLUDED = DEFAULT_IGNORED_DIRS | {".forge"}


class ExplorationError(Exception):
    """Exploration cannot start or continue safely. The message explains why and what to do."""


class Baseline(BaseModel):
    kind: Literal["git", "copy"]
    workspace_root: str
    repo_root: str | None = None
    subdir: str = ""  # the workspace inside the repository ("" = the repository root)
    head: str | None = None
    branch: str | None = None
    include_uncommitted: bool = True
    uncommitted: list[str] = Field(default_factory=list)  # repo-relative paths copied into every candidate
    left_out: list[str] = Field(default_factory=list)  # uncommitted paths candidates do not see (--from-head)


class CandidateWorkspace(BaseModel):
    candidate_id: str
    container: str  # the directory Forge created (worktree or copy)
    root: str  # the candidate's workspace root inside it
    kind: Literal["git", "copy"]


def preflight(workspace: Workspace, include_uncommitted: bool = True) -> Baseline:
    """Check that the project can be explored safely and describe the starting point."""
    repo = GitRepository.discover(workspace.root)
    if repo is None:
        return Baseline(kind="copy", workspace_root=str(workspace.root), include_uncommitted=True)

    try:
        operation = repo.operation_in_progress()
        status = repo.status()
    except GitError as error:
        raise ExplorationError(f"Could not read the Git state of {repo.root}: {error}") from error
    if operation:
        raise ExplorationError(
            f"{operation.capitalize()} is in progress in {repo.root}. Finish or abort it first; "
            "Forge will not guess which version of your files to explore from."
        )
    conflicts = [entry.path for entry in status.entries if entry.unmerged]
    if conflicts:
        raise ExplorationError(
            f"There are unresolved merge conflicts ({', '.join(conflicts[:5])}). Resolve them first."
        )
    if status.head is None:
        raise ExplorationError("The repository has no commits yet. Make an initial commit, then explore.")

    dirty = sorted({path for entry in status.entries for path in (entry.path, entry.original_path) if path})
    dirty = [path for path in dirty if not _ignored(path)]
    subdir = workspace.root.relative_to(repo.root).as_posix() if workspace.root != repo.root else ""
    return Baseline(
        kind="git",
        workspace_root=str(workspace.root),
        repo_root=str(repo.root),
        subdir="" if subdir == "." else subdir,
        head=status.head,
        branch=status.branch,
        include_uncommitted=include_uncommitted,
        uncommitted=dirty if include_uncommitted else [],
        left_out=[] if include_uncommitted else dirty,
    )


class Isolation:
    """Creates and removes candidate workspaces for one exploration run."""

    def __init__(self, baseline: Baseline, work_dir: Path, records_dir: Path) -> None:
        self.baseline = baseline
        self.work_dir = Path(work_dir).resolve()  # candidate directories go here (outside the project)
        self.records_dir = Path(records_dir)  # baseline copies of uncommitted files are saved here
        self._repo = GitRepository(Path(baseline.repo_root)) if baseline.repo_root else None
        self._worktrees = WorktreeManager(self._repo, self.work_dir) if self._repo else None
        self._pristine = self.work_dir / "_baseline"
        source = Path(baseline.repo_root or baseline.workspace_root).resolve()
        if self.work_dir.is_relative_to(source) or source.is_relative_to(self.work_dir):
            raise ExplorationError("Candidate directory must be outside and disjoint from the project")
        self._active: dict[str, CandidateWorkspace] = {}

    def prepare(self) -> None:
        """Save what candidates start from, so it can be compared and applied later."""
        self.work_dir.mkdir(parents=True, exist_ok=True)
        source = Path(self.baseline.repo_root or self.baseline.workspace_root)
        if self.baseline.kind == "git":
            for path in self.baseline.uncommitted:
                file = source / path
                if not file.resolve().is_relative_to(source) or any(
                    p.is_symlink() for p in (file, *file.parents) if p != source
                ):
                    raise ExplorationError(f"Cannot safely snapshot changed symlink: {path}")
                if file.is_file():
                    write_file(self.records_dir / "baseline" / path, file.read_bytes())
        else:
            _copy_tree(Path(self.baseline.workspace_root), self._pristine)

    def create(self, candidate_id: str) -> CandidateWorkspace:
        if not re.fullmatch(r"[A-Z]{1,3}", candidate_id):
            raise ExplorationError("Invalid candidate ID")
        container = self.work_dir / candidate_id
        if container.exists() or container.is_symlink():
            raise ExplorationError("Candidate directory already exists")
        root = container / self.baseline.subdir if self.baseline.kind == "git" and self.baseline.subdir else container
        workspace = CandidateWorkspace(
            candidate_id=candidate_id, container=str(container), root=str(root), kind=self.baseline.kind
        )
        self._active[candidate_id] = workspace
        if self.baseline.kind == "git":
            assert self._worktrees is not None and self.baseline.head is not None
            try:
                self._worktrees.add(container, self.baseline.head)
            except GitError as error:
                raise ExplorationError(f"Could not create a worktree for candidate {candidate_id}: {error}") from error
            self._overlay_uncommitted(container)
            root = container / self.baseline.subdir if self.baseline.subdir else container
        else:
            _copy_tree(self._pristine, container)
            root = container
        workspace = CandidateWorkspace(
            candidate_id=candidate_id, container=str(container), root=str(root), kind=self.baseline.kind
        )
        self._active[candidate_id] = workspace
        return workspace

    def remove(self, workspace: CandidateWorkspace) -> None:
        container = Path(workspace.container)
        target = container.resolve()
        if target == self.work_dir or not target.is_relative_to(self.work_dir):
            raise ExplorationError("Refusing to remove a candidate outside the run directory")
        self._active.pop(workspace.candidate_id, None)
        if workspace.kind == "git" and self._worktrees is not None:
            try:
                self._worktrees.remove(container)
                return
            except GitError:
                pass
        shutil.rmtree(container, ignore_errors=True)

    def cleanup(self) -> None:
        """Remove run-level scratch data (not candidate directories that were kept)."""
        for workspace in list(self._active.values()):
            self.remove(workspace)
        shutil.rmtree(self._pristine, ignore_errors=True)
        if self._worktrees is not None:
            self._worktrees.prune()
        try:
            self.work_dir.rmdir()  # only if empty (no kept candidates)
        except OSError:
            pass

    def retain(self, workspace: CandidateWorkspace) -> None:
        self._active.pop(workspace.candidate_id, None)

    def baseline_content(self, path: str) -> bytes | None:
        """A workspace-relative file's content at the start of exploration (None: it did not exist)."""
        if self.baseline.kind == "copy":
            file = self._pristine / path
            return file.read_bytes() if file.is_file() else None
        repo_path = f"{self.baseline.subdir}/{path}" if self.baseline.subdir else path
        if repo_path in self.baseline.uncommitted:
            saved = self.records_dir / "baseline" / repo_path
            return saved.read_bytes() if saved.is_file() else None
        assert self._repo is not None
        return self._repo.head_content(repo_path) if self.baseline.head else None

    def _overlay_uncommitted(self, container: Path) -> None:
        for path in self.baseline.uncommitted:
            saved = self.records_dir / "baseline" / path
            target = container / path
            if saved.is_file():
                write_file(target, saved.read_bytes())
            elif target.is_file():
                target.unlink()  # deleted in the user's working tree


def write_file(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(path, data)


def _ignored(path: str) -> bool:
    return any(part in COPY_EXCLUDED for part in Path(path).parts)


def _copy_tree(source: Path, target: Path) -> None:
    shutil.copytree(
        source,
        target,
        symlinks=True,  # copy links as links; never follow them out of the project
        ignore=shutil.ignore_patterns(*COPY_EXCLUDED),
    )
