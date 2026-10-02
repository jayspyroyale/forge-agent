"""Where Forge keeps its records of each task: `<workspace>/.forge/tasks/<task_id>/`.

    checkpoint.json   workspace snapshot taken before the task started
    journal.json      which files the task modified, with originals/ copies
    evidence.json     the task's proof of work (written when it finishes)
    undo-backup/      contents replaced by `forge tasks undo`

The tasks directory ignores itself in Git (`.forge/tasks/.gitignore`), and
`.forge/tasks` is a protected path, so tools cannot modify these records.
"""

import json
from pathlib import Path

from pydantic import BaseModel

from forge.evidence import TaskEvidence
from forge.fileio import atomic_write_text
from forge.tasks.journal import FileJournal
from forge.tasks.snapshot import WorkspaceSnapshot
from forge.workspace import Workspace

TASKS_DIR = Path(".forge") / "tasks"


class TaskNotFoundError(LookupError):
    pass


class TaskSummary(BaseModel):
    task_id: str
    task: str
    status: str
    started_at: str
    files_changed: int


class TaskStore:
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self.root = workspace.root / TASKS_DIR

    def directory(self, task_id: str) -> Path:
        if not task_id.isalnum():
            raise TaskNotFoundError(f"Invalid task id: {task_id}")
        return self.root / task_id

    def create(self, task_id: str) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        ignore = self.root / ".gitignore"
        if not ignore.exists():
            atomic_write_text(ignore, "# Forge task records are local and never committed.\n*\n")
        directory = self.directory(task_id)
        directory.mkdir(exist_ok=True)
        return directory

    def journal(self, task_id: str) -> FileJournal:
        return FileJournal(self.directory(task_id), self.workspace)

    def save_checkpoint(self, task_id: str, snapshot: WorkspaceSnapshot) -> None:
        atomic_write_text(self.directory(task_id) / "checkpoint.json", snapshot.model_dump_json(indent=2))

    def save_evidence(self, evidence: TaskEvidence) -> None:
        atomic_write_text(self.directory(evidence.task_id) / "evidence.json", evidence.model_dump_json(indent=2))

    def load_evidence(self, task_id: str) -> TaskEvidence:
        path = self.directory(task_id) / "evidence.json"
        if not path.is_file():
            raise TaskNotFoundError(f"No recorded task '{task_id}' in {self.root}")
        return TaskEvidence.model_validate_json(path.read_text(encoding="utf-8"))

    def list_tasks(self) -> list[TaskSummary]:
        if not self.root.is_dir():
            return []
        summaries = []
        for directory in self.root.iterdir():
            evidence_path = directory / "evidence.json"
            if not evidence_path.is_file():
                continue
            try:
                data = json.loads(evidence_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            changes = (data.get("changes") or {}).get("changes", [])
            summaries.append(
                TaskSummary(
                    task_id=data["task_id"],
                    task=data["task"],
                    status=data["status"],
                    started_at=data["started_at"],
                    files_changed=len(changes),
                )
            )
        return sorted(summaries, key=lambda summary: summary.started_at, reverse=True)
