"""Risk levels for actions Forge can take on the user's behalf."""

from enum import StrEnum

from pydantic import BaseModel, Field


class RiskLevel(StrEnum):
    READ = "read"  # looks at files; changes nothing
    WRITE = "write"  # changes files inside the workspace
    EXECUTE = "execute"  # runs programs
    DANGEROUS = "dangerous"  # could destroy data, escape the workspace, or affect the system

    @property
    def rank(self) -> int:
        return list(RiskLevel).index(self)


class RiskAssessment(BaseModel):
    level: RiskLevel
    reasons: list[str] = Field(default_factory=list)
