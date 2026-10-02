import pytest
from pydantic import ValidationError

from forge.models.types import Message, ModelResponse, ToolCall, ToolDefinition, Usage


def test_message_creation():
    message = Message(role="user", content="Hello")
    assert message.role == "user"
    assert message.content == "Hello"
    assert message.tool_calls == []
    assert message.tool_call_id is None


def test_message_shortcuts():
    assert Message.system("Be brief").role == "system"
    assert Message.user("Hi").role == "user"
    assert Message.assistant("Hello").content == "Hello"


def test_message_rejects_unknown_role():
    with pytest.raises(ValidationError):
        Message(role="robot", content="beep")


def test_tool_message_answers_a_tool_call():
    message = Message(role="tool", content="42 lines", tool_call_id="call_1")
    assert message.tool_call_id == "call_1"


def test_tool_call_creation():
    call = ToolCall(id="call_1", name="read_file", arguments={"path": "README.md"})
    assert call.name == "read_file"
    assert call.arguments == {"path": "README.md"}


def test_tool_call_arguments_default_to_empty():
    assert ToolCall(id="call_1", name="list_files").arguments == {}


def test_tool_definition_defaults_to_empty_object_schema():
    tool = ToolDefinition(name="noop", description="Does nothing")
    assert tool.parameters == {"type": "object", "properties": {}}


def test_model_response_defaults():
    response = ModelResponse()
    assert response.content == ""
    assert response.tool_calls == []
    assert response.finish_reason == "stop"
    assert response.usage is None


def test_model_response_with_tool_calls_and_usage():
    response = ModelResponse(
        tool_calls=[ToolCall(id="call_1", name="search", arguments={"query": "forge"})],
        finish_reason="tool_calls",
        usage=Usage(input_tokens=10, output_tokens=5, cost_usd=0.0001),
    )
    assert response.tool_calls[0].arguments["query"] == "forge"
    assert response.usage.cost_usd == 0.0001


def test_model_response_rejects_unknown_finish_reason():
    with pytest.raises(ValidationError):
        ModelResponse(finish_reason="exploded")


def test_usage_total_is_computed_when_missing():
    assert Usage(input_tokens=10, output_tokens=5).total_tokens == 15


def test_usage_keeps_reported_total():
    assert Usage(input_tokens=10, output_tokens=5, total_tokens=20).total_tokens == 20
