import asyncio

import pytest

from forge.models.base import ModelProvider
from forge.models.errors import ModelRequestError
from forge.models.providers.fake import FakeModelProvider
from forge.models.types import Message, ModelResponse, ToolCall, ToolDefinition


def test_fake_provider_is_a_model_provider():
    assert isinstance(FakeModelProvider(), ModelProvider)


def test_echoes_last_user_message_when_not_scripted():
    provider = FakeModelProvider()
    messages = [Message.system("Be brief"), Message.user("Hello")]

    response = asyncio.run(provider.generate(messages))

    assert isinstance(response, ModelResponse)
    assert response.content == "[fake] Hello"
    assert response.model == "fake-model"
    assert response.usage is not None


def test_returns_scripted_responses_in_order():
    provider = FakeModelProvider(responses=["Hello", "Goodbye"])

    first = asyncio.run(provider.generate([Message.user("1")]))
    second = asyncio.run(provider.generate([Message.user("2")]))

    assert first.content == "Hello"
    assert second.content == "Goodbye"


def test_can_script_a_tool_call():
    tool_reply = ModelResponse(
        tool_calls=[ToolCall(id="call_1", name="search", arguments={"query": "forge"})],
        finish_reason="tool_calls",
    )
    provider = FakeModelProvider(responses=[tool_reply])

    response = asyncio.run(provider.generate([Message.user("Find forge")]))

    assert response.finish_reason == "tool_calls"
    assert response.tool_calls[0].name == "search"


def test_raises_when_script_runs_out():
    provider = FakeModelProvider(responses=["only one"])
    asyncio.run(provider.generate([Message.user("1")]))

    with pytest.raises(ModelRequestError):
        asyncio.run(provider.generate([Message.user("2")]))


def test_records_what_was_sent():
    provider = FakeModelProvider(responses=["ok"])
    tool = ToolDefinition(name="search", description="Search the project")

    asyncio.run(provider.generate([Message.user("Hi")], tools=[tool], temperature=0.1))

    call = provider.calls[0]
    assert call["messages"][0].content == "Hi"
    assert call["tools"] == [tool]
    assert call["options"] == {"temperature": 0.1}
