"""Reverting a task's file changes, safely.

A file is only restored when Forge can prove two things:
1. it knows the file's content from before the task (journal copy, or the
   HEAD version for a file that was clean at task start), and
2. the file has not changed since the task finished (its hash still matches
   the one recorded in the evidence), so no later work is overwritten.

Anything else is skipped with a reason. Files that were already modified
before the task and left alone are never touched. Git state (index,
branches, commits) is never modified; current contents are backed up first.
"""

import hashlib
from typing import Literal

from pydantic import BaseModel, Field

from forge.fileio import atomic_write_bytes
from forge.git.repo import GitRepository
from forge.tasks.snapshot import file_hash
from forge.tasks.store import TaskStore

Action = Literal["restore", "delete", "skip"]


class UndoAction(BaseModel):
    path: str
    action: Action
    reason: str
    source: Literal["journal", "head"] | None = None


class UndoPlan(BaseModel):
    task_id: str
    actions: list[UndoAction] = Field(default_factory=list)

    @property
    def safe(self) -> list[UndoAction]:
        return [action for action in self.actions if action.action != "skip"]

    @property
    def skipped(self) -> list[UndoAction]:
        return [action for action in self.actions if action.action == "skip"]


def plan_undo(store: TaskStore, task_id: str, repo: GitRepository | None = None) -> UndoPlan:
    evidence = store.load_evidence(task_id)
    journal = store.journal(task_id)
    entries = journal.entries()
    plan = UndoPlan(task_id=task_id)
    if evidence.changes is None:
        return plan

    for change in evidence.changes.changes:
        current = file_hash(store.workspace.root / change.path)
        if current != change.after_hash:
            plan.actions.append(UndoAction(path=change.path, action="skip", reason="changed again after the task finished"))
            continue

        entry = entries.get(change.path)
        if entry is not None:
            if entry.existed:
                plan.actions.append(UndoAction(path=change.path, action="restore", reason="restore saved original", source="journal"))
            else:
                plan.actions.append(UndoAction(path=change.path, action="delete", reason="file was created by the task", source="journal"))
            continue

        if change.origin == "task" and change.kind == "added":
            plan.actions.append(UndoAction(path=change.path, action="delete", reason="file was created by the task"))
        elif change.origin == "task" and repo is not None and _head_matches(repo, store, change):
            plan.actions.append(UndoAction(path=change.path, action="restore", reason="restore version from HEAD", source="head"))
        else:
            plan.actions.append(
                UndoAction(path=change.path, action="skip", reason="no saved copy of the original content")
            )
    return plan


def apply_undo(plan: UndoPlan, store: TaskStore, repo: GitRepository | None = None) -> list[UndoAction]:
    """Carry out the safe actions of `plan`. Returns the actions performed."""
    journal = store.journal(plan.task_id)
    backup_root = store.directory(plan.task_id) / "undo-backup"
    performed = []
    for action in plan.safe:
        target = store.workspace.root / action.path
        if target.is_file():
            backup = backup_root / action.path
            backup.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes(backup, target.read_bytes())
        if action.action == "delete":
            target.unlink(missing_ok=True)
        else:
            content = journal.original(action.path) if action.source == "journal" else _head_content(repo, store, action.path)
            if content is None:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes(target, content)
        performed.append(action)
    return performed


def _head_matches(repo: GitRepository, store: TaskStore, change) -> bool:
    content = _head_content(repo, store, change.path)
    return content is not None and hashlib.sha256(content).hexdigest() == change.before_hash


def _head_content(repo: GitRepository | None, store: TaskStore, path: str) -> bytes | None:
    if repo is None:
        return None
    relative = (store.workspace.root / path).resolve().relative_to(repo.root).as_posix()
    return repo.head_content(relative)
