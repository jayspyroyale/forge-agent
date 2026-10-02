"""The context engine: decides what the model sees on each call.

The agent's full transcript is kept unchanged in its state (that is the
record). What is *sent* to the model is rendered from context items, under a
token budget, by deterministic rules:

1. Redundant and stale output is replaced by a one-line note:
   - an earlier result for the same thing (same file read, same command) is
     superseded by the later one;
   - an earlier read of a file that was changed afterwards is stale.
2. Old, large tool output is replaced by its structural summary (only the
   most recent tool results are always shown in full).
3. If the conversation is still over budget, the least important, oldest
   items are summarized, then omitted, until it fits.

Never touched: CRITICAL items (the system prompt and the task) and the
latest exchange (the last assistant message and the results of its tool
calls). Tool messages are never removed, only shortened, because every
tool call must keep an answer for the conversation to stay valid.
Rendering the same items always gives the same messages.
"""

from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel

from forge.context.items import ContextItem, Importance, Provenance
from forge.context.tokens import estimate_message_tokens
from forge.models.types import Message

ItemState = Literal["full", "summarized", "superseded", "stale", "omitted", "dropped"]

ASSISTANT_KEEP_CHARS = 300


class ContextBudget(BaseModel):
    max_tokens: int = 64_000
    keep_recent_outputs: int = 6
    compress_above_tokens: int = 1_500


class RenderedItem(BaseModel):
    id: str
    state: ItemState
    tokens: int


class ContextView(BaseModel):
    messages: list[Message]
    items: list[RenderedItem]
    tokens: int
    full_tokens: int  # what sending everything would have cost
    budget: int

    @property
    def over_budget(self) -> bool:
        return self.tokens > self.budget


class ContextEngine:
    def __init__(self, budget: ContextBudget | None = None) -> None:
        self.budget = budget or ContextBudget()
        self.items: list[ContextItem] = []
        self.last_view: ContextView | None = None

    def add(
        self,
        message: Message,
        provenance: Provenance,
        *,
        importance: Importance = Importance.NORMAL,
        key: str | None = None,
        changes: Iterable[str] = (),
        summary: str | None = None,
    ) -> ContextItem:
        item = ContextItem(
            id=f"ctx{len(self.items) + 1:04d}",
            order=len(self.items),
            message=message,
            provenance=provenance,
            importance=importance,
            key=key,
            changes=list(changes),
            summary=summary,
        )
        self.items.append(item)
        return item

    # --- rendering --------------------------------------------------------------------

    def render(self) -> ContextView:
        states: dict[str, ItemState] = {item.id: "full" for item in self.items}
        protected = self._protected_ids()

        self._mark_redundant(states, protected)
        self._summarize_old_tool_output(states, protected)

        sizes = {item.id: self._tokens(item, states[item.id]) for item in self.items}
        total = sum(sizes.values())
        if total > self.budget.max_tokens:
            # Least important first, then oldest first.
            candidates = sorted(
                (item for item in self.items if item.id not in protected),
                key=lambda item: (item.importance, item.order),
            )
            for target_state in ("summarized", "omitted"):
                for item in candidates:
                    if total <= self.budget.max_tokens:
                        break
                    new_state = self._shrink(item, states[item.id], target_state)
                    if new_state is None:
                        continue
                    new_size = self._tokens(item, new_state)
                    if new_size < sizes[item.id]:
                        total -= sizes[item.id] - new_size
                        states[item.id], sizes[item.id] = new_state, new_size

        messages = [self._message(item, states[item.id]) for item in self.items if states[item.id] != "dropped"]
        view = ContextView(
            messages=messages,
            items=[RenderedItem(id=item.id, state=states[item.id], tokens=sizes[item.id]) for item in self.items],
            tokens=total,
            full_tokens=sum(item.tokens for item in self.items),
            budget=self.budget.max_tokens,
        )
        self.last_view = view
        return view

    def messages(self) -> list[Message]:
        return self.render().messages

    def _protected_ids(self) -> set[str]:
        """CRITICAL items and the latest exchange: the last assistant message and everything after it."""
        protected = {item.id for item in self.items if item.importance == Importance.CRITICAL}
        last_assistant = max((item.order for item in self.items if item.message.role == "assistant"), default=None)
        if last_assistant is not None:
            protected |= {item.id for item in self.items if item.order >= last_assistant}
        elif self.items:
            protected.add(self.items[-1].id)
        return protected

    def _mark_redundant(self, states: dict[str, ItemState], protected: set[str]) -> None:
        latest_for_key: dict[str, int] = {}
        changed_at: dict[str, int] = {}
        for item in self.items:
            if item.key:
                latest_for_key[item.key] = item.order
            for path in item.changes:
                changed_at[path] = item.order
        for item in self.items:
            if item.id in protected or item.importance == Importance.CRITICAL or not item.key:
                continue
            if latest_for_key[item.key] > item.order:
                states[item.id] = "superseded" if item.message.role == "tool" else "dropped"
            elif item.key.startswith("file:") and changed_at.get(item.key[5:], -1) > item.order:
                states[item.id] = "stale"

    def _summarize_old_tool_output(self, states: dict[str, ItemState], protected: set[str]) -> None:
        tool_items = [item for item in self.items if item.message.role == "tool"]
        keep = self.budget.keep_recent_outputs
        old = tool_items[: max(len(tool_items) - keep, 0)]
        for item in old:
            if (
                item.id not in protected
                and states[item.id] == "full"
                and item.summary
                and item.tokens > self.budget.compress_above_tokens
            ):
                states[item.id] = "summarized"

    @staticmethod
    def _shrink(item: ContextItem, state: ItemState, target: str) -> ItemState | None:
        if state != "full" and not (state == "summarized" and target == "omitted"):
            return None
        if target == "summarized":
            if item.summary and item.message.role == "tool":
                return "summarized"
            return None
        if item.message.role == "tool" or item.message.role == "assistant":
            return "omitted"
        return "dropped"  # extra user/system notes can disappear entirely

    def _tokens(self, item: ContextItem, state: ItemState) -> int:
        if state == "full":
            return item.tokens
        if state == "dropped":
            return 0
        return estimate_message_tokens(self._message(item, state))

    @staticmethod
    def _message(item: ContextItem, state: ItemState) -> Message:
        message = item.message
        if state == "full":
            return message
        provenance = item.provenance
        if state == "summarized":
            content = item.summary or message.content
        elif state == "superseded":
            content = (
                f"[Forge: output omitted; a later {provenance.tool_name or 'call'} for "
                f"{provenance.source_id or 'the same target'} returned newer output.]"
            )
        elif state == "stale":
            content = (
                f"[Forge: earlier content of {provenance.source_id} omitted because the file was changed later. "
                "Read it again if you need it.]"
            )
        elif message.role == "assistant":
            text = message.content
            content = text if len(text) <= ASSISTANT_KEEP_CHARS else text[:ASSISTANT_KEEP_CHARS] + " [...]"
        else:
            content = (
                f"[Forge: output omitted to fit the context budget (source: {provenance.describe()}). "
                "Run the tool again if you need it.]"
            )
        return message.model_copy(update={"content": content})

    # --- provenance -------------------------------------------------------------------

    def trace(self, text: str) -> list[Provenance]:
        """Where did this information come from? The provenance of every item that contains `text`."""
        needle = text.lower()
        return [item.provenance for item in self.items if needle in item.message.content.lower()]

    def manifest(self) -> list[dict]:
        """A record of every item (without its content): source, importance, size, and how it was last sent."""
        states = {rendered.id: rendered for rendered in self.last_view.items} if self.last_view else {}
        records = []
        for item in self.items:
            rendered = states.get(item.id)
            records.append(
                {
                    "id": item.id,
                    "order": item.order,
                    "role": item.message.role,
                    "provenance": item.provenance.model_dump(mode="json", exclude_none=True),
                    "importance": item.importance.name.lower(),
                    "tokens": item.tokens,
                    "key": item.key,
                    "last_state": rendered.state if rendered else None,
                    "last_tokens": rendered.tokens if rendered else None,
                }
            )
        return records
