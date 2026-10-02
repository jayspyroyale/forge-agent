import asyncio

import pytest
from pydantic import Field

from forge.models.types import ToolCall, ToolDefinition
from forge.security.permissions import PermissionEngine
from forge.security.policy import PermissionPolicy
from forge.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult
from forge.tools.builtin import create_default_tools
from forge.tools.executor import ToolExecutor
from forge.tools.registry import ToolNotFoundError, ToolRegistry
from forge.workspace import Workspace


class EchoArgs(ToolArgs):
    text: str = Field(description="Text to echo.")
    times: int = Field(default=1, ge=1)


class EchoTool(Tool):
    name = "echo"
    description = "Echo text back."
    Args = EchoArgs

    def execute(self, args, context):
        return ToolResult.ok(args.text * args.times, length=len(args.text))


class FailingTool(Tool):
    name = "fails"
    description = "Always fails with an expected error."

    def execute(self, args, context):
        raise ToolError("this tool always fails")


class BuggyTool(Tool):
    name = "buggy"
    description = "Crashes with an unexpected exception."

    def execute(self, args, context):
        raise RuntimeError("boom")


def make_executor(tmp_path, *tools):
    permissions = PermissionEngine(PermissionPolicy.permissive())
    return ToolExecutor(ToolRegistry(list(tools)), ToolContext(workspace=Workspace(tmp_path)), permissions)


def run(executor, name, **arguments):
    return asyncio.run(executor.execute(ToolCall(id="t1", name=name, arguments=arguments)))


# --- Registry -----------------------------------------------------------------


def test_register_and_get():
    registry = ToolRegistry()
    tool = EchoTool()
    registry.register(tool)
    assert registry.get("echo") is tool
    assert "echo" in registry
    assert len(registry) == 1


def test_duplicate_registration_is_rejected():
    registry = ToolRegistry([EchoTool()])
    with pytest.raises(ValueError, match="already registered"):
        registry.register(EchoTool())


def test_unknown_tool_lookup():
    with pytest.raises(ToolNotFoundError):
        ToolRegistry().get("missing")


def test_unregister():
    registry = ToolRegistry([EchoTool()])
    registry.unregister("echo")
    assert "echo" not in registry
    with pytest.raises(ToolNotFoundError):
        registry.unregister("echo")


def test_list_is_sorted_by_name():
    registry = ToolRegistry([FailingTool(), EchoTool()])
    assert registry.names() == ["echo", "fails"]
    assert [tool.name for tool in registry.list_tools()] == ["echo", "fails"]


def test_schema_export_is_neutral_json_schema():
    definition = EchoTool().definition()
    assert isinstance(definition, ToolDefinition)
    assert definition.name == "echo"
    schema = definition.parameters
    assert schema["type"] == "object"
    assert schema["required"] == ["text"]
    assert schema["properties"]["text"]["description"] == "Text to echo."
    assert schema["additionalProperties"] is False


def test_tool_without_arguments_has_empty_object_schema():
    schema = FailingTool().definition().parameters
    assert schema["type"] == "object"
    assert schema["properties"] == {}


def test_registry_exports_all_definitions():
    definitions = ToolRegistry([EchoTool(), FailingTool()]).definitions()
    assert [definition.name for definition in definitions] == ["echo", "fails"]


def test_default_tools_are_registered():
    assert create_default_tools().names() == [
        "current_directory",
        "edit_file",
        "file_exists",
        "list_files",
        "read_file",
        "run_command",
        "search_text",
        "write_file",
    ]


# --- Executor -----------------------------------------------------------------


def test_successful_result(tmp_path):
    result = run(make_executor(tmp_path, EchoTool()), "echo", text="hi", times=2)
    assert result.success
    assert result.output == "hihi"
    assert result.metadata["length"] == 2
    assert result.metadata["permission"] == {"risk": "execute", "decided_by": "policy"}
    assert result.to_model_content() == "hihi"


def test_failed_result(tmp_path):
    result = run(make_executor(tmp_path, FailingTool()), "fails")
    assert not result.success
    assert result.error == "this tool always fails"
    assert result.to_model_content() == "Error: this tool always fails"


def test_unexpected_exception_becomes_failed_result(tmp_path):
    result = run(make_executor(tmp_path, BuggyTool()), "buggy")
    assert not result.success
    assert "RuntimeError: boom" in result.error


def test_unknown_tool_becomes_failed_result(tmp_path):
    result = run(make_executor(tmp_path, EchoTool()), "nope")
    assert not result.success
    assert "Unknown tool 'nope'" in result.error
    assert "echo" in result.error


def test_missing_argument(tmp_path):
    result = run(make_executor(tmp_path, EchoTool()), "echo")
    assert not result.success
    assert "Invalid arguments for 'echo'" in result.error
    assert "text" in result.error


def test_wrong_argument_type(tmp_path):
    result = run(make_executor(tmp_path, EchoTool()), "echo", text="hi", times="many")
    assert not result.success
    assert "times" in result.error


def test_unexpected_argument_is_rejected(tmp_path):
    result = run(make_executor(tmp_path, EchoTool()), "echo", text="hi", colour="red")
    assert not result.success
    assert "colour" in result.error
