"""Tests for the OpenAI adapter. No network and no API key: the SDK client is replaced by a stub."""

import asyncio
from types import SimpleNamespace

import httpx2
import openai
import pytest
from openai.types.chat import ChatCompletion

from forge.config import ForgeConfig
from forge.models.errors import (
    AuthenticationError,
    InvalidModelResponseError,
    ModelRequestError,
    ProviderConfigError,
)
from forge.models.providers.openai_provider import OllamaProvider, OpenAIProvider
from forge.models.types import Message, ToolCall, ToolDefinition


class StubClient:
    """Looks like AsyncOpenAI for the one method Forge uses: chat.completions.create."""

    def __init__(self, reply=None, error=None):
        self.reply = reply
        self.error = error
        self.requests = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.reply


def make_completion(message, finish_reason="stop", usage=None):
    """Build a real SDK ChatCompletion object from plain data."""
    return ChatCompletion.model_validate(
        {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 0,
            "model": "gpt-test",
            "choices": [
                {"index": 0, "message": {"role": "assistant", **message}, "finish_reason": finish_reason}
            ],
            "usage": usage,
        }
    )


def make_provider(reply=None, error=None, **config):
    client = StubClient(reply=reply, error=error)
    provider = OpenAIProvider(ForgeConfig(provider="openai", **config), client=client)
    return provider, client


FAKE_REQUEST = httpx2.Request("POST", "https://api.example.test/v1/chat/completions")


def status_error(error_class, status_code, body):
    response = httpx2.Response(status_code, request=FAKE_REQUEST)
    return error_class("request failed", response=response, body=body)


# --- Successful replies -------------------------------------------------------


def test_text_reply_is_normalized():
    completion = make_completion(
        {"content": "FORGE_OK"},
        usage={"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
    )
    provider, _ = make_provider(reply=completion)

    response = asyncio.run(provider.generate([Message.user("Say FORGE_OK")]))

    assert response.content == "FORGE_OK"
    assert response.finish_reason == "stop"
    assert response.model == "gpt-test"
    assert response.tool_calls == []
    assert (response.usage.input_tokens, response.usage.output_tokens) == (12, 3)
    assert response.usage.total_tokens == 15


def test_tool_call_reply_is_normalized():
    completion = make_completion(
        {
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "search", "arguments": '{"query": "forge"}'},
                }
            ],
        },
        finish_reason="tool_calls",
    )
    provider, _ = make_provider(reply=completion)

    response = asyncio.run(provider.generate([Message.user("Find forge")]))

    assert response.content == ""
    assert response.finish_reason == "tool_calls"
    assert response.tool_calls == [ToolCall(id="call_1", name="search", arguments={"query": "forge"})]
    assert response.usage is None


def test_unknown_finish_reason_becomes_other():
    provider, _ = make_provider(reply=make_completion({"content": "x"}, finish_reason="function_call"))
    response = asyncio.run(provider.generate([Message.user("hi")]))
    assert response.finish_reason == "other"


# --- What Forge sends ---------------------------------------------------------


def test_request_uses_default_model_and_converts_messages():
    provider, client = make_provider(reply=make_completion({"content": "ok"}))

    asyncio.run(provider.generate([Message.system("Be brief"), Message.user("Hi")]))

    request = client.requests[0]
    assert request["model"] == OpenAIProvider.default_model
    assert request["messages"] == [
        {"role": "system", "content": "Be brief"},
        {"role": "user", "content": "Hi"},
    ]
    assert "tools" not in request
    assert "temperature" not in request


def test_request_includes_tools_temperature_and_options():
    provider, client = make_provider(
        reply=make_completion({"content": "ok"}), model="gpt-custom", temperature=0.2
    )
    tool = ToolDefinition(
        name="search",
        description="Search the project",
        parameters={"type": "object", "properties": {"query": {"type": "string"}}},
    )

    asyncio.run(provider.generate([Message.user("Hi")], tools=[tool], max_completion_tokens=50))

    request = client.requests[0]
    assert request["model"] == "gpt-custom"
    assert request["temperature"] == 0.2
    assert request["max_completion_tokens"] == 50
    assert request["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "search",
                "description": "Search the project",
                "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
            },
        }
    ]


def test_tool_round_trip_messages_are_converted():
    provider, client = make_provider(reply=make_completion({"content": "done"}))
    messages = [
        Message.user("Find forge"),
        Message(
            role="assistant",
            tool_calls=[ToolCall(id="call_1", name="search", arguments={"query": "forge"})],
        ),
        Message(role="tool", content="3 results", tool_call_id="call_1"),
    ]

    asyncio.run(provider.generate(messages))

    sent = client.requests[0]["messages"]
    assert sent[1]["tool_calls"][0]["function"] == {
        "name": "search",
        "arguments": '{"query": "forge"}',
    }
    assert sent[2] == {"role": "tool", "content": "3 results", "tool_call_id": "call_1"}


# --- Invalid replies ----------------------------------------------------------


def test_invalid_tool_arguments_raise_invalid_response():
    completion = make_completion(
        {
            "content": None,
            "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "search", "arguments": "{not json"}}
            ],
        },
        finish_reason="tool_calls",
    )
    provider, _ = make_provider(reply=completion)

    with pytest.raises(InvalidModelResponseError):
        asyncio.run(provider.generate([Message.user("hi")]))


def test_no_choices_raises_invalid_response():
    completion = ChatCompletion.model_validate(
        {"id": "x", "object": "chat.completion", "created": 0, "model": "gpt-test", "choices": []}
    )
    provider, _ = make_provider(reply=completion)

    with pytest.raises(InvalidModelResponseError):
        asyncio.run(provider.generate([Message.user("hi")]))


# --- SDK errors become Forge errors -------------------------------------------


def test_rejected_key_becomes_authentication_error():
    error = status_error(openai.AuthenticationError, 401, {"error": {"message": "Incorrect API key"}})
    provider, _ = make_provider(error=error)

    with pytest.raises(AuthenticationError, match="rejected the API key"):
        asyncio.run(provider.generate([Message.user("hi")]))


def test_rate_limit_becomes_request_error_with_short_message():
    error = status_error(openai.RateLimitError, 429, {"error": {"message": "Slow down"}})
    provider, _ = make_provider(error=error)

    with pytest.raises(ModelRequestError, match="HTTP 429: Slow down"):
        asyncio.run(provider.generate([Message.user("hi")]))


def test_timeout_becomes_request_error():
    provider, _ = make_provider(error=openai.APITimeoutError(request=FAKE_REQUEST), timeout=5)

    with pytest.raises(ModelRequestError, match="within 5 seconds"):
        asyncio.run(provider.generate([Message.user("hi")]))


def test_connection_failure_becomes_request_error():
    provider, _ = make_provider(error=openai.APIConnectionError(request=FAKE_REQUEST))

    with pytest.raises(ModelRequestError, match="Could not connect"):
        asyncio.run(provider.generate([Message.user("hi")]))


def test_forge_error_keeps_the_sdk_error_as_cause():
    sdk_error = openai.APIConnectionError(request=FAKE_REQUEST)
    provider, _ = make_provider(error=sdk_error)

    with pytest.raises(ModelRequestError) as caught:
        asyncio.run(provider.generate([Message.user("hi")]))
    assert caught.value.__cause__ is sdk_error


# --- API keys and provider settings -------------------------------------------


def test_missing_api_key_raises_authentication_error():
    with pytest.raises(AuthenticationError, match="OPENAI_API_KEY"):
        OpenAIProvider(ForgeConfig(provider="openai"))


def test_api_key_is_read_from_environment(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    provider = OpenAIProvider(ForgeConfig(provider="openai"))
    assert provider._client.api_key == "sk-test-not-real"


def test_ollama_needs_no_key_and_uses_local_url():
    provider = OllamaProvider(ForgeConfig(provider="ollama", model="llama3"))
    assert str(provider._client.base_url).startswith("http://localhost:11434/v1")
    assert provider.model == "llama3"


def test_base_url_from_config_overrides_default():
    provider = OllamaProvider(
        ForgeConfig(provider="ollama", model="llama3", base_url="http://gpu-box:11434/v1")
    )
    assert str(provider._client.base_url).startswith("http://gpu-box:11434/v1")


def test_ollama_without_model_raises_config_error():
    provider = OllamaProvider(ForgeConfig(provider="ollama"))
    with pytest.raises(ProviderConfigError, match="--model"):
        asyncio.run(provider.generate([Message.user("hi")]))
