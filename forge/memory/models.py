"""What a memory is.

A memory is a small statement Forge keeps about one project across sessions
("uses pnpm", "always run the slow tests with -m slow"). It is never a dump
of the conversation. Every memory says where it came from and how much to
trust it:

    source        file:package.json, user, model, ...
    confidence    low | medium | high
    verification  verified (derived from a file Forge read), unverified, contradicted
    status        active | superseded (replaced by newer evidence) | contested (conflicts
                  with a more trusted memory; kept for review, not used)
"""

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

MemoryKind = Literal["instruction", "architecture", "decision", "fact", "workflow", "preference"]
MEMORY_KINDS: tuple[MemoryKind, ...] = ("instruction", "architecture", "decision", "fact", "workflow", "preference")
Confidence = Literal["low", "medium", "high"]
CONFIDENCE_RANK: dict[str, int] = {"low": 1, "medium": 2, "high": 3}
Verification = Literal["unverified", "verified", "contradicted"]
Status = Literal["active", "superseded", "contested"]


def now() -> datetime:
    return datetime.now(UTC)


class MemoryInput(BaseModel):
    """A memory Forge has been asked to keep (before it is stored)."""

    kind: MemoryKind
    content: str = Field(min_length=1, max_length=2_000)
    # What question this memory answers ("package_manager:node"). Memories with the
    # same subject compete: a newer, at-least-as-trusted one replaces the older one.
    subject: str | None = Field(default=None, max_length=200)
    source: str = "user"  # "file:package.json", "user", "model", "conversation", ...
    confidence: Confidence = "medium"
    verification: Verification = "unverified"
    expires_at: datetime | None = None


class MemoryRecord(MemoryInput):
    id: str
    project: str
    status: Status = "active"
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)
    superseded_by: str | None = None
    conflicts_with: str | None = None

    def expired(self, at: datetime | None = None) -> bool:
        return self.expires_at is not None and self.expires_at <= (at or now())

    def usable(self, at: datetime | None = None) -> bool:
        """Active and not expired: may be shown to the agent."""
        return self.status == "active" and not self.expired(at)


RememberAction = Literal["created", "refreshed", "superseded", "contested"]


class RememberResult(BaseModel):
    record: MemoryRecord
    action: RememberAction
    replaced: list[str] = Field(default_factory=list)  # ids this memory superseded
