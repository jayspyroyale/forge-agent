"""The permission engine: decides whether a requested action may run.

    PermissionRequest (tool, arguments, risk) -> policy decision
        ALLOW -> allowed
        DENY  -> refused
        ASK   -> session grant? -> allowed
                 otherwise ask the approver (a person, or a test double)

The engine knows nothing about specific tools, models, or the CLI, so the
same engine can later sit in front of MCP tools, autonomous runs, or
exploration candidates.
"""

from collections.abc import Callable
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

from forge.security.policy import Decision, PermissionPolicy
from forge.security.risk import RiskAssessment, RiskLevel


class ApprovalChoice(StrEnum):
    ALLOW_ONCE = "allow_once"
    ALLOW_SESSION = "allow_session"  # remember this approval for the rest of the session
    DENY = "deny"


class PermissionRequest(BaseModel):
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    risk: RiskAssessment
    # Session approvals are remembered per key: a tool name, or tool + command.
    approval_key: str


class PermissionOutcome(BaseModel):
    allowed: bool
    decision: Decision  # what the policy said for this risk level
    decided_by: Literal["policy", "session", "user", "no_approver"]
    reason: str


class PermissionRecord(BaseModel):
    request: PermissionRequest
    outcome: PermissionOutcome


Approver = Callable[[PermissionRequest], ApprovalChoice]


class PermissionEngine:
    def __init__(self, policy: PermissionPolicy | None = None, approver: Approver | None = None) -> None:
        self.policy = policy or PermissionPolicy()
        self.approver = approver
        self._session_grants: set[str] = set()
        # Every decision, in order. Evidence and reports read from this.
        self.history: list[PermissionRecord] = []

    def authorize(self, request: PermissionRequest) -> PermissionOutcome:
        outcome = self._decide(request)
        self.history.append(PermissionRecord(request=request, outcome=outcome))
        return outcome

    def _decide(self, request: PermissionRequest) -> PermissionOutcome:
        level = request.risk.level
        decision = self.policy.decision_for(level)
        detail = f" ({'; '.join(request.risk.reasons)})" if request.risk.reasons else ""

        if decision == Decision.ALLOW:
            return PermissionOutcome(allowed=True, decision=decision, decided_by="policy", reason=f"{level} actions are allowed by policy")
        if decision == Decision.DENY:
            return PermissionOutcome(
                allowed=False, decision=decision, decided_by="policy", reason=f"{level} actions are denied by policy{detail}"
            )

        # ASK
        if level != RiskLevel.DANGEROUS and request.approval_key in self._session_grants:
            return PermissionOutcome(allowed=True, decision=decision, decided_by="session", reason="approved earlier for this session")
        if self.approver is None:
            return PermissionOutcome(
                allowed=False,
                decision=decision,
                decided_by="no_approver",
                reason=f"{level} actions need approval and no one is available to approve{detail}",
            )

        try:
            choice = self.approver(request)
        except (EOFError, KeyboardInterrupt):
            choice = ApprovalChoice.DENY
        if choice == ApprovalChoice.DENY:
            return PermissionOutcome(allowed=False, decision=decision, decided_by="user", reason="denied by the user")
        if choice == ApprovalChoice.ALLOW_SESSION and level != RiskLevel.DANGEROUS:
            self._session_grants.add(request.approval_key)
        return PermissionOutcome(allowed=True, decision=decision, decided_by="user", reason="approved by the user")
