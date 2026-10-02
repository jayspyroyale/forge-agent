"""Small, rule-based nudges that keep the agent loop from going in circles.

These do not decide anything for the model. They add short notes to the
conversation when Forge observes a pattern (the same call failing again,
several failures in a row, an empty reply), so the model gets explicit
feedback instead of silently repeating itself.
"""

import json
from collections import Counter

from forge.agent.prompts import CONSECUTIVE_FAILURES_NOTE, REPEATED_FAILURE_NOTE
from forge.models.types import ToolCall
from forge.tools.base import ToolResult

CONSECUTIVE_FAILURE_THRESHOLD = 4


class FailureTracker:
    def __init__(self, consecutive_threshold: int = CONSECUTIVE_FAILURE_THRESHOLD) -> None:
        self.consecutive_threshold = consecutive_threshold
        self._failures_by_call: Counter[str] = Counter()
        self.consecutive_failures = 0

    def note_for(self, call: ToolCall, result: ToolResult) -> str | None:
        """Record a tool result. Returns a note to append to it, if one is warranted."""
        if result.success:
            self.consecutive_failures = 0
            return None

        self.consecutive_failures += 1
        key = _call_key(call)
        self._failures_by_call[key] += 1
        repeats = self._failures_by_call[key]

        if repeats >= 2:
            return REPEATED_FAILURE_NOTE.format(count=repeats)
        if self.consecutive_failures % self.consecutive_threshold == 0:
            return CONSECUTIVE_FAILURES_NOTE.format(count=self.consecutive_failures)
        return None


def _call_key(call: ToolCall) -> str:
    return call.name + ":" + json.dumps(call.arguments, sort_keys=True, default=str)
