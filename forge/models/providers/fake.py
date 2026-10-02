"""A fake provider that never touches the network.

Used by tests (with scripted responses) and available from the CLI as
`--provider fake` (echoes your prompt) to check Forge's plumbing offline.
"""

from typing import Any

from forge.config import ForgeConfig
from forge.models.base import ModelProvider
from forge.models.errors import ModelRequestError
from forge.models.types import Message, ModelResponse, ToolDefinition, Usage


class FakeModelProvider(ModelProvider):
    name = "fake"
    description = "Offline test provider (echoes the prompt or replays scripted replies)"
    default_model = "fake-model"

    def __init__(
        self,
        config: ForgeConfig | None = None,
        responses: list[ModelResponse | str] | None = None,
    ) -> None:
        super().__init__(config or ForgeConfig())
        # Scripted replies, returned in order. Plain strings become text replies.
        self._responses = [
            ModelResponse(content=item) if isinstance(item, str) else item
            for item in (responses or [])
        ]
        self._scripted = responses is not None
        # Every call is recorded so tests can check exactly what was sent.
        self.calls: list[dict[str, Any]] = []

    async def generate(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        **options: Any,
    ) -> ModelResponse:
        # Copy the list: callers keep appending to theirs after this call returns.
        self.calls.append({"messages": list(messages), "tools": tools, "options": options})

        if self._scripted:
            if not self._responses:
                raise ModelRequestError("FakeModelProvider has no scripted responses left")
            return self._responses.pop(0)

        last_user_message = next(
            (message.content for message in reversed(messages) if message.role == "user"),
            "",
        )
        content = f"[fake] {last_user_message}"
        return ModelResponse(
            content=content,
            model=self.model,
            usage=Usage(input_tokens=len(last_user_message.split()), output_tokens=len(content.split())),
        )
