"""Persistent project memory in a local SQLite database.

One database per user (default: ~/.forge/memory.db, or $FORGE_HOME/memory.db),
with every memory scoped to a project (the project's root directory). The
standard library's sqlite3 is used, so memory adds no dependency.

Conflicts are resolved when a memory is stored, never silently:

    same subject, same content       -> refreshed (newer timestamp, higher confidence kept)
    same subject, different content  -> if the new memory is at least as trusted,
                                        the old one is marked `superseded`;
                                        otherwise the new one is stored as `contested`
                                        and not used until someone resolves it
    no subject                        -> exact duplicates are refreshed, nothing else changes

Content that looks like a credential is refused.
"""

import contextlib
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path

from forge.memory.models import CONFIDENCE_RANK, MemoryInput, MemoryRecord, RememberResult, now
from forge.security.secret_scan import find_secrets

SCHEMA_VERSION = 1
_COLUMNS = (
    "id, project, kind, subject, content, source, confidence, verification, status, "
    "created_at, updated_at, expires_at, superseded_by, conflicts_with"
)


class MemoryStoreError(Exception):
    """The memory database cannot be used (missing, corrupted, locked, ...)."""


class MemorySecretError(ValueError):
    """Refused to store something that looks like a credential."""


class MemoryNotFoundError(LookupError):
    pass


def project_key(root: Path | str) -> str:
    """How memories are scoped: the project's resolved root directory."""
    return Path(root).resolve().as_posix()


class MemoryStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        try:
            if str(path) != ":memory:":
                self.path.parent.mkdir(parents=True, exist_ok=True)
            # Tools run in worker threads; every use of the connection is serialized by self._lock.
            self._lock = threading.RLock()
            self._db = sqlite3.connect(str(path), autocommit=False, timeout=5.0, check_same_thread=False)
            self._db.row_factory = sqlite3.Row
            self._migrate()
        except sqlite3.DatabaseError as error:
            with contextlib.suppress(Exception):
                self._db.close()
            raise MemoryStoreError(f"Cannot open the memory database {self.path}: {error}") from error

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def __enter__(self) -> "MemoryStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- writing --------------------------------------------------------------------------

    def remember(self, project: str, memory: MemoryInput) -> RememberResult:
        found = find_secrets(memory.content)
        if found:
            raise MemorySecretError(f"Refusing to store a memory that looks like it contains a secret ({', '.join(found)}).")
        content = " ".join(memory.content.split())
        memory = memory.model_copy(update={"content": content})

        with self._transaction():
            same = self._find_same(project, memory)
            if same is not None:
                return self._refresh(same, memory)

            rivals = self._rivals(project, memory)
            record = MemoryRecord(id=uuid.uuid4().hex[:12], project=project, **memory.model_dump())
            stronger = [rival for rival in rivals if _rank(rival) > _rank(record)]
            if stronger:
                record.status = "contested"
                record.conflicts_with = stronger[0].id
                self._insert(record)
                return RememberResult(record=record, action="contested")

            self._insert(record)
            for rival in rivals:
                self._db.execute(
                    "UPDATE memories SET status = 'superseded', superseded_by = ?, updated_at = ? WHERE id = ?",
                    (record.id, _ts(now()), rival.id),
                )
            return RememberResult(
                record=record, action="superseded" if rivals else "created", replaced=[rival.id for rival in rivals]
            )

    def forget(self, memory_id: str, project: str | None = None) -> bool:
        with self._transaction():
            query, params = "DELETE FROM memories WHERE id = ?", [memory_id]
            if project is not None:
                query += " AND project = ?"
                params.append(project)
            return self._db.execute(query, params).rowcount > 0

    def prune_expired(self, project: str | None = None) -> int:
        with self._transaction():
            query, params = "DELETE FROM memories WHERE expires_at IS NOT NULL AND expires_at <= ?", [_ts(now())]
            if project is not None:
                query += " AND project = ?"
                params.append(project)
            return self._db.execute(query, params).rowcount

    # --- reading --------------------------------------------------------------------------

    def get(self, memory_id: str, project: str | None = None) -> MemoryRecord:
        row = self._query_one("SELECT * FROM memories WHERE id = ?", (memory_id,))
        if row is None or (project is not None and row.project != project):
            raise MemoryNotFoundError(f"No memory with id '{memory_id}'")
        return row

    def list_memories(self, project: str, *, include_inactive: bool = False) -> list[MemoryRecord]:
        """Memories of a project, newest first. By default only usable ones (active and not expired)."""
        records = self._query("SELECT * FROM memories WHERE project = ? ORDER BY updated_at DESC, id", (project,))
        if include_inactive:
            return records
        moment = now()
        return [record for record in records if record.usable(moment)]

    def projects(self) -> list[str]:
        with self._lock:
            return [row[0] for row in self._db.execute("SELECT DISTINCT project FROM memories ORDER BY project")]

    # --- internals ------------------------------------------------------------------------

    @contextlib.contextmanager
    def _transaction(self):
        try:
            with self._lock, self._db:
                yield
        except sqlite3.DatabaseError as error:
            raise MemoryStoreError(f"Memory database error ({self.path}): {error}") from error

    def _migrate(self) -> None:
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise MemoryStoreError(f"{self.path} was created by a newer Forge (schema {version})")
        with self._db:
            self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    project TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    subject TEXT,
                    content TEXT NOT NULL,
                    source TEXT NOT NULL,
                    confidence TEXT NOT NULL,
                    verification TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    expires_at TEXT,
                    superseded_by TEXT,
                    conflicts_with TEXT
                )
                """
            )
            self._db.execute("CREATE INDEX IF NOT EXISTS memories_project ON memories (project, status)")
            self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _find_same(self, project: str, memory: MemoryInput) -> MemoryRecord | None:
        for record in self._query(
            "SELECT * FROM memories WHERE project = ? AND kind = ? AND status = 'active'", (project, memory.kind)
        ):
            same_subject = record.subject == memory.subject
            if same_subject and record.content.casefold() == memory.content.casefold():
                return record
        return None

    def _rivals(self, project: str, memory: MemoryInput) -> list[MemoryRecord]:
        """Active memories answering the same question with different content."""
        if memory.subject is None:
            return []
        return self._query(
            "SELECT * FROM memories WHERE project = ? AND kind = ? AND subject = ? AND status = 'active' ORDER BY created_at",
            (project, memory.kind, memory.subject),
        )

    def _refresh(self, record: MemoryRecord, memory: MemoryInput) -> RememberResult:
        stronger = CONFIDENCE_RANK[memory.confidence] > CONFIDENCE_RANK[record.confidence]
        confidence = memory.confidence if stronger else record.confidence
        source = memory.source if stronger else record.source
        verification = "verified" if "verified" in (record.verification, memory.verification) else record.verification
        self._db.execute(
            "UPDATE memories SET confidence = ?, source = ?, verification = ?, updated_at = ?, expires_at = ? WHERE id = ?",
            (confidence, source, verification, _ts(now()), _ts(memory.expires_at), record.id),
        )
        return RememberResult(record=self.get(record.id), action="refreshed")

    def _insert(self, record: MemoryRecord) -> None:
        self._db.execute(
            f"INSERT INTO memories ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.id,
                record.project,
                record.kind,
                record.subject,
                record.content,
                record.source,
                record.confidence,
                record.verification,
                record.status,
                _ts(record.created_at),
                _ts(record.updated_at),
                _ts(record.expires_at),
                record.superseded_by,
                record.conflicts_with,
            ),
        )

    def _query(self, sql: str, params: tuple) -> list[MemoryRecord]:
        try:
            with self._lock:
                rows = self._db.execute(sql, params).fetchall()
        except sqlite3.DatabaseError as error:
            raise MemoryStoreError(f"Memory database error ({self.path}): {error}") from error
        return [_record(row) for row in rows]

    def _query_one(self, sql: str, params: tuple) -> MemoryRecord | None:
        records = self._query(sql, params)
        return records[0] if records else None


def _rank(record: MemoryRecord) -> tuple[int, int]:
    """How much to trust a memory: verified beats unverified, then confidence."""
    return (record.verification == "verified", CONFIDENCE_RANK[record.confidence])


def _ts(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _record(row: sqlite3.Row) -> MemoryRecord:
    data = dict(row)
    for key in ("created_at", "updated_at", "expires_at"):
        if data[key] is not None:
            data[key] = datetime.fromisoformat(data[key])
    return MemoryRecord(**data)
