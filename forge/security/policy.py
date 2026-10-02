"""Which decision applies to each risk level."""

from enum import StrEnum
from typing import Self

from pydantic import BaseModel

from forge.security.risk import RiskLevel


class Decision(StrEnum):
    ALLOW = "allow"
    ASK = "ask"  # ask the user before proceeding
    DENY = "deny"


class PermissionPolicy(BaseModel):
    """Default: reading is free, changing or running things needs approval, dangerous is refused."""

    read: Decision = Decision.ALLOW
    write: Decision = Decision.ASK
    execute: Decision = Decision.ASK
    dangerous: Decision = Decision.DENY

    def decision_for(self, level: RiskLevel) -> Decision:
        return getattr(self, level.value)

    @classmethod
    def permissive(cls) -> Self:
        """Allow everything. For tests and fully trusted, sandboxed environments only."""
        return cls(read=Decision.ALLOW, write=Decision.ALLOW, execute=Decision.ALLOW, dangerous=Decision.ALLOW)
