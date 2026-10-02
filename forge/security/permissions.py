"""Decides whether an action requested by the agent is allowed.

Phase 1 placeholder: every request is denied. Starting from "deny" means that
if a later phase forgets to wire in a real policy, Forge fails safe instead of
silently allowing actions.
"""

from enum import StrEnum

from pydantic import BaseModel


class Decision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"  # ask the user before proceeding


class PermissionRequest(BaseModel):
    """An action the agent wants to perform."""

    tool_name: str
    description: str = ""


def check_permission(request: PermissionRequest) -> Decision:
    return Decision.DENY
