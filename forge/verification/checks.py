"""Data types for verification: what can be checked, and what happened when it was."""

from typing import Literal

from pydantic import BaseModel

CheckKind = Literal["test", "build", "typecheck", "lint"]
CHECK_KINDS: tuple[CheckKind, ...] = ("test", "build", "typecheck", "lint")

CheckStatus = Literal["passed", "failed", "timed_out", "error", "skipped"]
# Statuses that mean "the code is not in a good state" (as opposed to "not checked").
FAILING_STATUSES: frozenset[str] = frozenset({"failed", "timed_out", "error"})


class VerificationCheck(BaseModel):
    """A check Forge found in the project's own configuration."""

    name: str  # e.g. "pytest", "npm test"
    kind: CheckKind
    command: str
    reason: str  # why Forge believes this check applies (which file it came from)


class VerificationResult(BaseModel):
    """The outcome of actually running one check. Created only by Forge's verifier."""

    name: str
    kind: CheckKind
    command: str
    status: CheckStatus
    exit_code: int | None = None
    duration_seconds: float = 0.0
    summary: str = ""  # the end of the output, where failures usually are

    @property
    def passed(self) -> bool:
        return self.status == "passed"

    @property
    def failing(self) -> bool:
        return self.status in FAILING_STATUSES
