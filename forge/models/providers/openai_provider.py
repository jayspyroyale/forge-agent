"""Adapter for OpenAI and OpenAI-compatible servers (Ollama, DeepSeek, vLLM, ...).

This is the only module in Forge that imports the `openai` SDK. It converts
Forge types to Chat Completions requests, and Chat Completions replies (and
SDK exceptions) back to Forge types.

The file is named `openai_provider.py`, not `openai.py`, so that it is never
confused with the `openai` package it imports.
"""

import hashlib
import json
import os
import re
from typing import Any

import openai
from openai import AsyncOpenAI

from forge.config import ForgeConfig
from forge.models.base import ModelProvider
from forge.models.errors import (
    AuthenticationError,
    InvalidModelResponseError,
    ModelRequestError,
)
from forge.models.types import (
    FinishReason,
    Message,
    ModelResponse,
    ToolCall,
    ToolDefinition,
    Usage,
)

# OpenAI finish reasons -> Forge finish reasons. Anything else becomes "other".
_FINISH_REASONS: dict[str, FinishReason] = {
    "stop": "stop",
    "tool_calls": "tool_calls",
    "length": "length",
    "content_filter": "content_filter",
}


class OpenAIProvider(ModelProvider):
    name = "openai"
    description = "OpenAI Chat Completions API (key from OPENAI_API_KEY)"
    requires_api_key = True
    default_model = "gpt-5.4-mini"
    api_key_env = "OPENAI_API_KEY"
    default_base_url: str | None = None  # None = the SDK's default (api.openai.com)

    def __init__(self, config: ForgeConfig, client: AsyncOpenAI | None = None) -> None:
        super().__init__(config)
        # Tests pass their own `client`, so no API key or network is needed.
        self._client = client or AsyncOpenAI(
            api_key=self._read_api_key(),
            base_url=config.model.base_url or self.default_base_url,
            timeout=config.model.timeout,
            max_retries=1,
        )

    def _read_api_key(self) -> str:
        api_key = os.environ.get(self.api_key_env)
        if not api_key:
            raise AuthenticationError(
                f"No API key found for '{self.name}'. "
                f"Set the {self.api_key_env} environment variable."
            )
        return api_key

    async def generate(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        **options: Any,
    ) -> ModelResponse:
        request: dict[str, Any] = {
            "model": self.model,
            "messages": [_to_openai_message(message) for message in messages],
        }
        names = {wire_tool_name(tool.name): tool.name for tool in tools or []}
        if tools:
            request["tools"] = [_to_openai_tool(tool) for tool in tools]
        if self.config.model.temperature is not None:
            request["temperature"] = self.config.model.temperature
        request.update(options)

        try:
            completion = await self._client.chat.completions.create(**request)
        except openai.AuthenticationError as error:
            raise AuthenticationError(
                f"'{self.name}' rejected the API key in {self.api_key_env}."
            ) from error
        except openai.APITimeoutError as error:
            raise ModelRequestError(
                f"'{self.name}' did not reply within {self.config.model.timeout:g} seconds."
            ) from error
        except openai.APIConnectionError as error:
            raise ModelRequestError(f"Could not connect to '{self.name}'.") from error
        except openai.APIStatusError as error:
            raise ModelRequestError(
                f"'{self.name}' returned HTTP {error.status_code}: {_short_message(error)}"
            ) from error

        return _from_openai_completion(completion, names)


class OllamaProvider(OpenAIProvider):
    """Ollama's OpenAI-compatible endpoint. Runs locally, so no API key is needed."""

    name = "ollama"
    description = "Local models via Ollama's OpenAI-compatible API"
    requires_api_key = False
    default_model = None  # depends on which models you have pulled
    api_key_env = "OLLAMA_API_KEY"
    default_base_url = "http://localhost:11434/v1"

    def _read_api_key(self) -> str:
        # Ollama ignores the key, but the SDK requires a non-empty value.
        return os.environ.get(self.api_key_env) or "ollama"


# --- Forge -> OpenAI ---------------------------------------------------------

_WIRE_NAME = re.compile(r"[^A-Za-z0-9_-]")
MAX_TOOL_NAME = 64


def wire_tool_name(name: str) -> str:
    """A Forge tool name in the character set every OpenAI-compatible API accepts.

    Forge namespaces MCP tools with a dot (`github.create_issue`); function
    names here may only use letters, digits, `_` and `-`. Dots become `__`;
    anything else becomes `_`; very long names are shortened with a hash so
    they stay unique. Replies are mapped back to Forge names.
    """
    wire = _WIRE_NAME.sub("_", name.replace(".", "__"))
    if len(wire) > MAX_TOOL_NAME:
        digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]
        wire = f"{wire[: MAX_TOOL_NAME - 9]}_{digest}"
    return wire


def _to_openai_message(message: Message) -> dict[str, Any]:
    result: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.tool_calls:
        result["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": wire_tool_name(call.name), "arguments": json.dumps(call.arguments)},
            }
            for call in message.tool_calls
        ]
    if message.tool_call_id is not None:
        result["tool_call_id"] = message.tool_call_id
    return result


def _to_openai_tool(tool: ToolDefinition) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": wire_tool_name(tool.name),
            "description": tool.description,
            "parameters": tool.parameters,
        },
    }


# --- OpenAI -> Forge ---------------------------------------------------------


def _from_openai_completion(completion: Any, names: dict[str, str] | None = None) -> ModelResponse:
    if not completion.choices:
        raise InvalidModelResponseError("The model returned no choices.")
    choice = completion.choices[0]

    tool_calls = []
    for raw_call in choice.message.tool_calls or []:
        if raw_call.type != "function":
            raise InvalidModelResponseError(f"Unsupported tool call type: {raw_call.type}")
        tool_calls.append(
            ToolCall(
                id=raw_call.id,
                name=(names or {}).get(raw_call.function.name, raw_call.function.name),
                arguments=_parse_arguments(raw_call.function.arguments),
            )
        )

    usage = None
    if completion.usage is not None:
        usage = Usage(
            input_tokens=completion.usage.prompt_tokens,
            output_tokens=completion.usage.completion_tokens,
            total_tokens=completion.usage.total_tokens,
        )

    return ModelResponse(
        content=choice.message.content or "",
        tool_calls=tool_calls,
        finish_reason=_FINISH_REASONS.get(choice.finish_reason, "other"),
        usage=usage,
        model=completion.model,
    )


def _parse_arguments(raw_arguments: str) -> dict[str, Any]:
    if not raw_arguments:
        return {}
    try:
        arguments = json.loads(raw_arguments)
    except json.JSONDecodeError as error:
        raise InvalidModelResponseError(
            f"The model sent tool arguments that are not valid JSON: {raw_arguments!r}"
        ) from error
    if not isinstance(arguments, dict):
        raise InvalidModelResponseError("The model sent tool arguments that are not a JSON object.")
    return arguments


def _short_message(error: openai.APIStatusError) -> str:
    """The provider's own error message, without the full response body."""
    body = error.body
    if isinstance(body, dict):
        # OpenAI nests the message under "error"; some compatible servers don't.
        nested = body.get("error")
        if isinstance(nested, dict) and nested.get("message"):
            return str(nested["message"])
        if isinstance(nested, str):
            return nested
        if body.get("message"):
            return str(body["message"])
    return error.message
