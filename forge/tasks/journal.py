"""Saves each file's original content before a task first modifies it.

This is what makes a task reversible without touching Git: undo writes the
saved bytes back (or deletes a file the task created).
"""

import json
import hashlib
import re
from pathlib import Path

from pydantic import BaseModel

from forge.fileio import atomic_write_bytes, atomic_write_text
from forge.tasks.snapshot import file_hash
from forge.workspace import Workspace
from forge.tasks.paths import record_path


class JournalEntry(BaseModel):
    path: str  # workspace-relative
    existed: bool
    blob: str | None = None  # file name under originals/, when the file existed
    sha256: str | None = None


class FileJournal:
    def __init__(self, directory: Path, workspace: Workspace) -> None:
        self.directory = directory
        self.workspace = workspace
        self._index_path = record_path(workspace.root, (directory / "journal.json").relative_to(workspace.root))
        self._entries: dict[str, JournalEntry] = {}
        if self._index_path.is_file():
            raw = json.loads(self._index_path.read_text(encoding="utf-8"))
            self._entries = {item["path"]: JournalEntry(**item) for item in raw}

    def before_write(self, path: Path) -> None:
        """Record `path` the first time it is about to change. Later writes keep the first copy."""
        relative = self.workspace.relative(path)
        if relative in self._entries:
            return
        entry = JournalEntry(path=relative, existed=path.is_file())
        if entry.existed:
            originals = record_path(
                self.workspace.root, (self.directory / "originals").relative_to(self.workspace.root)
            )
            originals.mkdir(parents=True, exist_ok=True)
            entry.blob = f"{len(self._entries):05d}.bin"
            atomic_write_bytes(originals / entry.blob, path.read_bytes())
            entry.sha256 = file_hash(path)
        self._entries[relative] = entry
        self._save()

    def entries(self) -> dict[str, JournalEntry]:
        return dict(self._entries)

    def original(self, relative: str) -> bytes | None:
        """The saved original bytes, or None if the file did not exist (or was never recorded)."""
        entry = self._entries.get(relative)
        if entry is None or not entry.existed or entry.blob is None:
            return None
        if not re.fullmatch(r"[0-9]+\.bin", entry.blob):
            raise ValueError("Invalid original blob path in journal")
        path = record_path(
            self.workspace.root, (self.directory / "originals" / entry.blob).relative_to(self.workspace.root)
        )
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != entry.sha256:
            raise ValueError("Saved original is damaged; refusing to restore it")
        return data

    def pre_images(self) -> dict[str, bytes | None]:
        return {path: self.original(path) for path in self._entries}

    def _save(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        data = [entry.model_dump() for entry in self._entries.values()]
        atomic_write_text(self._index_path, json.dumps(data, indent=2))
