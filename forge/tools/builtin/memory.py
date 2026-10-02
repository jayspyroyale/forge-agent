"""The remember tool: lets the model propose a memory, under Forge's rules.

Off by default (`memory.model_writes`). When enabled it needs approval like
any write, and what the model stores is always low confidence and
unverified: a model's claim never outranks a fact Forge read from a file
or something the user told it.
"""

from typing import Literal

from pydantic import Field

from forge.memory.models import MemoryInput, now
from forge.memory.store import MemorySecretError, MemoryStore
from forge.security.risk import RiskLevel
from forge.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult


class RememberArgs(ToolArgs):
    kind: Literal["instruction", "architecture", "decision", "fact", "workflow", "preference"]
    content: str = Field(min_length=3, max_length=500, description="One short statement worth knowing in future sessions.")
    subject: str | None = Field(
        default=None, max_length=100, description="What question this answers (e.g. 'test_command'); a newer memory replaces it."
    )
    expires_in_days: int | None = Field(default=None, ge=1, le=365, description="For temporary facts such as workarounds.")


class RememberTool(Tool):
    name = "remember"
    description = (
        "Save one short, durable statement about this project for future sessions (a decision, a workflow, "
        "a user preference). Do not save task progress, guesses, or anything secret."
    )
    Args = RememberArgs
    risk = RiskLevel.WRITE
    context_source = "memory"

    def __init__(self, store: MemoryStore, project: str) -> None:
        self.store = store
        self.project = project

    def context_reference(self, arguments):
        subject = arguments.get("subject")
        return subject if isinstance(subject, str) else None

    def execute(self, args: RememberArgs, context: ToolContext) -> ToolResult:
        from datetime import timedelta

        expires_at = now() + timedelta(days=args.expires_in_days) if args.expires_in_days else None
        memory = MemoryInput(
            kind=args.kind,
            content=args.content,
            subject=args.subject,
            source="model",
            confidence="low",
            verification="unverified",
            expires_at=expires_at,
        )
        try:
            result = self.store.remember(self.project, memory)
        except MemorySecretError as error:
            raise ToolError(str(error)) from None
        record = result.record
        note = {
            "created": "Saved.",
            "refreshed": "Already known; refreshed.",
            "superseded": f"Saved; it replaces {len(result.replaced)} older memory.",
            "contested": "Saved as contested: it conflicts with a more trusted memory and will not be used until resolved.",
        }[result.action]
        return ToolResult.ok(f"{note} (memory {record.id}, low confidence)", memory_id=record.id, action=result.action)
