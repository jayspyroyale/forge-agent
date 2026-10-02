"""Where exploration records live: `<workspace>/.forge/explorations/<run_id>/`.

    run.json                  plans, every candidate's result and proof of work, comparison, selection
    baseline/                 the user's uncommitted files at the start (what candidates started from)
    <candidate>/changes.patch the candidate's change as a unified diff, for review
    <candidate>/files/        the candidate's final version of every file it added or modified

Records are kept for every candidate, selected or not. The directory
ignores itself in Git and is a protected path, so no tool can change it.
"""

import json
from pathlib import Path

from pydantic import BaseModel

from forge.exploration.isolation import write_file
from forge.exploration.results import ExplorationRun
from forge.fileio import atomic_write_text
from forge.workspace import Workspace
from forge.tasks.paths import record_path
from forge.security.secret_scan import redact_data

EXPLORATIONS_DIR = Path(".forge") / "explorations"


class RunNotFoundError(LookupError):
    pass


class RunSummary(BaseModel):
    run_id: str
    task: str
    status: str
    created_at: str
    candidates: int
    selected: str | None = None


class ExplorationStore:
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self.root = record_path(workspace.root, EXPLORATIONS_DIR)

    def run_dir(self, run_id: str) -> Path:
        if not run_id.isalnum():
            raise RunNotFoundError(f"Invalid run id: {run_id}")
        return record_path(self.workspace.root, EXPLORATIONS_DIR / run_id)

    def create(self, run_id: str) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        ignore = self.root / ".gitignore"
        if not ignore.exists():
            atomic_write_text(ignore, "# Forge exploration records are local and never committed.\n*\n")
        directory = self.run_dir(run_id)
        directory.mkdir(exist_ok=True)
        return directory

    def save(self, run: ExplorationRun) -> None:
        atomic_write_text(self._run_file(run.run_id), json.dumps(redact_data(run.model_dump(mode="json")), indent=2))

    def _run_file(self, run_id: str) -> Path:
        return record_path(self.workspace.root, self.run_dir(run_id).relative_to(self.workspace.root) / "run.json")

    def load(self, run_id: str) -> ExplorationRun:
        path = self._run_file(run_id)
        if not path.is_file():
            raise RunNotFoundError(f"No exploration run '{run_id}' in {self.root}")
        return ExplorationRun.model_validate_json(path.read_text(encoding="utf-8"))

    def latest(self) -> ExplorationRun:
        runs = self.list_runs()
        if not runs:
            raise RunNotFoundError(f"No exploration runs in {self.root}")
        return self.load(runs[0].run_id)

    def candidate_dir(self, run_id: str, candidate_id: str) -> Path:
        if not candidate_id.isalnum():
            raise RunNotFoundError(f"Invalid candidate id: {candidate_id}")
        return record_path(self.workspace.root, self.run_dir(run_id).relative_to(self.workspace.root) / candidate_id)

    def save_candidate_file(self, run_id: str, candidate_id: str, path: str, content: bytes) -> None:
        write_file(self._file_path(run_id, candidate_id, path), content)

    def candidate_file(self, run_id: str, candidate_id: str, path: str) -> bytes | None:
        file = self._file_path(run_id, candidate_id, path)
        return file.read_bytes() if file.is_file() else None

    def save_patch(self, run_id: str, candidate_id: str, patch: str) -> Path:
        path = self._patch_file(run_id, candidate_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, patch)
        return path

    def patch(self, run_id: str, candidate_id: str) -> str:
        path = self._patch_file(run_id, candidate_id)
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    def _patch_file(self, run_id: str, candidate_id: str) -> Path:
        return record_path(
            self.workspace.root,
            self.candidate_dir(run_id, candidate_id).relative_to(self.workspace.root) / "changes.patch",
        )

    def list_runs(self) -> list[RunSummary]:
        if not self.root.is_dir():
            return []
        summaries = []
        for directory in self.root.iterdir():
            path = directory / "run.json"
            if not path.is_file():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            selection = data.get("selection") or {}
            summaries.append(
                RunSummary(
                    run_id=data["run_id"],
                    task=data["task"],
                    status=data["status"],
                    created_at=data["created_at"],
                    candidates=len(data.get("candidates", [])),
                    selected=selection.get("candidate_id"),
                )
            )
        return sorted(summaries, key=lambda summary: summary.created_at, reverse=True)

    def _file_path(self, run_id: str, candidate_id: str, path: str) -> Path:
        base = self.candidate_dir(run_id, candidate_id) / "files"
        target = record_path(self.workspace.root, (base / path).relative_to(self.workspace.root))
        if not target.is_relative_to(base):
            raise RunNotFoundError(f"Invalid file path in candidate record: {path}")
        return record_path(self.workspace.root, target.relative_to(self.workspace.root))
