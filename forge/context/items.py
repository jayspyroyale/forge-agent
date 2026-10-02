"""What a piece of context is, and where it came from.

Every message Forge sends to a model is backed by a `ContextItem`. The item
keeps the message plus the facts Forge needs to manage it:

    provenance   where the content came from (the user, a file, a command, a memory, ...)
    importance   how hard Forge tries to keep it when the context budget is tight
    tokens       an estimate of its size
    key          what it is "about" (e.g. file:src/app.py), so newer content can supersede older

Items are provider-independent: they wrap Forge's normalized `Message`.
"""

from enum import IntEnum, StrEnum

from pydantic import BaseModel, Field

from forge.context.tokens import estimate_message_tokens
from forge.models.types import Message


class SourceType(StrEnum):
    USER = "user"  # what the user asked for
    MODEL = "model"  # what the model said
    FILE = "file"  # file contents or file changes
    TOOL_RESULT = "tool_result"  # output of a tool that is not about one file or command
    TERMINAL = "terminal"  # output of a command
    GIT = "git"  # repository state
    MEMORY = "memory"  # persistent project memory
    MCP = "mcp"  # a tool provided by an MCP server
    VERIFICATION = "verification"  # Forge's own verification results
    SYSTEM = "system"  # Forge's instructions and notes


class Importance(IntEnum):
    """Higher is kept longer. CRITICAL items are never compressed or omitted."""

    LOW = 1  # nudges and hints that matter only briefly
    NORMAL = 2  # ordinary tool output and model text
    HIGH = 3  # errors, verification failures, memory, user instructions
    CRITICAL = 4  # the system prompt and the task itself


class Provenance(BaseModel):
    """Where a piece of context came from. Answers: "how does Forge know this?"."""

    source: SourceType
    source_id: str | None = None  # a path, a command, a tool name, a memory id, ...
    step: int = 0  # agent step that produced it (0 = before the first model call)
    tool_name: str | None = None
    tool_call_id: str | None = None
    detail: str | None = None

    def describe(self) -> str:
        where = f" {self.source_id}" if self.source_id else ""
        return f"{self.source}{where} (step {self.step})"


class ContextItem(BaseModel):
    id: str
    order: int  # position in the conversation; the only ordering Forge uses (deterministic)
    message: Message
    provenance: Provenance
    importance: Importance = Importance.NORMAL
    tokens: int = 0
    # What this item is about. A later item with the same key supersedes it.
    key: str | None = None
    # Paths this item changed; makes earlier reads of those files stale.
    changes: list[str] = Field(default_factory=list)
    # A structural summary to use instead of the full content when space is short.
    summary: str | None = None

    def model_post_init(self, _context) -> None:
        if not self.tokens:
            self.tokens = estimate_message_tokens(self.message)


class BackgroundItem(BaseModel):
    """Context Forge adds before the agent starts (relevant files, project memory, ...)."""

    content: str
    provenance: Provenance
    importance: Importance = Importance.HIGH
    key: str | None = None
