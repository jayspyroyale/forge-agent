"""MCP integration: client protocol, tool adaptation, permissions, failures, agent use, config, CLI."""

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from forge.agent.runtime import run_task
from forge.cli import app
from forge.config import ConfigError, ForgeConfig, McpServerSettings, McpSettings, load_config
from forge.mcp import McpClient, McpConnectionError, McpManager, McpTimeoutError, adapt_tools
from forge.mcp.client import McpRemoteError
from forge.models.providers.fake import FakeModelProvider
from forge.models.providers.openai_provider import _from_openai_completion, wire_tool_name
from forge.models.types import ModelResponse, ToolCall
from forge.security.permissions import ApprovalChoice, PermissionEngine
from forge.tools.base import ToolContext
from forge.tools.executor import ToolExecutor
from forge.tools.registry import ToolRegistry
from forge.workspace import Workspace

MOCK = Path(__file__).resolve().parent / "mock_mcp_server.py"
runner = CliRunner()


def server(mode="normal", **settings):
    return McpServerSettings(command=sys.executable, args=[str(MOCK), mode], **settings)


@pytest.fixture
def client():
    with McpClient("mock", [sys.executable, str(MOCK)], call_timeout=10) as connected:
        yield connected


def connect(tmp_path, **servers):
    manager = McpManager(McpSettings(servers=servers), tmp_path)
    statuses = manager.connect_all()
    return manager, {status.name: status for status in statuses}


# --- Client -----------------------------------------------------------------------------


def test_connect_and_discover(client):
    assert client.protocol_version == "2025-11-25"
    assert client.server_info["name"] == "mock"
    tools = client.list_tools()  # two pages
    assert [tool["name"] for tool in tools] == ["echo", "add", "fail", "slow", "crash", "env"]


def test_call_tool(client):
    assert client.call_tool("echo", {"message": "hi"}).text == "echo: hi"
    assert client.call_tool("add", {"a": 2, "b": 3}).text == "5"
    result = client.call_tool("fail", {})
    assert result.is_error and result.text == "something went wrong"


def test_server_requests_are_answered_and_the_connection_survives(client):
    client.list_tools()  # the server pinged us after initialize
    assert client.connected
    assert "mock server initialized" in "\n".join(client.stderr_tail)


def test_unknown_tool_is_a_remote_error(client):
    with pytest.raises(McpRemoteError, match="Unknown tool: nope"):
        client.call_tool("nope", {})


def test_tool_timeout(client):
    with pytest.raises(McpTimeoutError, match="did not answer tools/call within 0.5 seconds"):
        client.call_tool("slow", {}, timeout=0.5)


def test_server_crash_fails_cleanly(client):
    with pytest.raises(McpConnectionError, match="disconnected"):
        client.call_tool("crash", {})
    assert not client.connected
    with pytest.raises(McpConnectionError):
        client.call_tool("echo", {"message": "still there?"})


@pytest.mark.parametrize(
    ("mode", "message"),
    [("bad-init", "init exploded"), ("exit", "did not initialize"), ("silent", "did not answer initialize")],
)
def test_failed_startup(mode, message):
    client = McpClient("mock", [sys.executable, str(MOCK), mode], startup_timeout=1)
    with pytest.raises(McpConnectionError, match=message):
        client.connect()
    assert not client.connected


def test_missing_command():
    with pytest.raises(McpConnectionError, match="Could not start MCP server 'ghost'"):
        McpClient("ghost", ["forge-no-such-mcp-server-xyz"]).connect()


# --- Adapter: namespacing, validation, risk ------------------------------------------------------


def test_tools_are_namespaced_and_risky_by_default(tmp_path):
    manager, statuses = connect(tmp_path, mock=server())
    with manager:
        tools = {tool.name: tool for tool in manager.tools()}
    assert statuses["mock"].tools == ["mock.echo", "mock.add", "mock.fail", "mock.slow", "mock.crash", "mock.env"]
    assert all(tool.risk == "execute" for tool in tools.values())  # readOnlyHint on `add` does not lower it
    definition = tools["mock.echo"].definition()
    assert definition.parameters["required"] == ["message"]
    assert definition.description.startswith("[MCP server 'mock']")


def test_configured_risk_and_destructive_hint(tmp_path):
    manager, _ = connect(tmp_path, mock=server(risk="read", tool_risk={"echo": "write"}))
    with manager:
        risks = {tool.name: tool.risk for tool in manager.tools()}
    assert risks["mock.add"] == "read"
    assert risks["mock.echo"] == "write"
    assert risks["mock.crash"] == "execute"  # destructiveHint raises the configured `read`


def test_malformed_tools_are_skipped_not_fatal(tmp_path):
    manager, statuses = connect(tmp_path, mock=server("malformed"))
    manager.close()
    status = statuses["mock"]
    assert status.connected and len(status.tools) == 6
    reasons = {skipped.name: skipped.reason for skipped in status.skipped}
    assert [skipped.reason for skipped in status.skipped if skipped.name == "<unnamed>"] == [
        "missing or unsupported name (letters, digits, '_' and '-' only)",
        "not an object",
    ]
    assert "missing or unsupported name" in reasons["bad schema"]
    assert reasons["not_object"] == "inputSchema is not a JSON Schema object"
    assert reasons["echo"] == "duplicate name"


def test_tool_allowlist(tmp_path):
    manager, statuses = connect(tmp_path, mock=server(tools=["echo"]))
    manager.close()
    assert statuses["mock"].tools == ["mock.echo"]


def test_unavailable_server_does_not_break_others(tmp_path):
    manager, statuses = connect(tmp_path, good=server(), broken=McpServerSettings(command="forge-no-such-mcp-server-xyz"))
    with manager:
        assert statuses["good"].connected and not statuses["broken"].connected
        assert "Could not start" in statuses["broken"].error
        assert len(manager.tools()) == 6


# --- Permissions and execution through the normal executor --------------------------------------


def execute(tmp_path, tool_risk=None, approver=None, call="mock.echo", arguments=None):
    manager, _ = connect(tmp_path, mock=server(tool_risk=tool_risk or {}))
    calls = []
    with manager:
        client = manager.clients["mock"]
        original = client.call_tool
        client.call_tool = lambda *args, **kwargs: calls.append(args) or original(*args, **kwargs)
        registry = ToolRegistry(manager.tools())
        executor = ToolExecutor(registry, ToolContext(workspace=Workspace(tmp_path)), PermissionEngine(approver=approver))
        result = asyncio.run(executor.execute(ToolCall(id="1", name=call, arguments=arguments or {"message": "hi"})))
    return result, calls


def test_mcp_tools_need_approval_like_any_other_tool(tmp_path):
    result, calls = execute(tmp_path)  # execute risk, no approver
    assert not result.success and result.metadata["denied"]
    assert calls == []  # the server never received the call


def test_approved_and_read_only_calls_run(tmp_path):
    result, calls = execute(tmp_path, approver=lambda request: ApprovalChoice.ALLOW_ONCE)
    assert result.success and result.output == "echo: hi"
    assert result.metadata["permission"] == {"risk": "execute", "decided_by": "user"}
    result, _ = execute(tmp_path, tool_risk={"echo": "read"})
    assert result.success and result.metadata["permission"]["decided_by"] == "policy"


def test_mcp_failures_become_tool_results(tmp_path):
    allow = {"fail": "read", "crash": "read", "echo": "read"}
    result, _ = execute(tmp_path, tool_risk=allow, call="mock.fail", arguments={})
    assert not result.success and result.output == "something went wrong"
    # destructiveHint keeps `crash` at execute risk despite the configured `read`, so it needs approval.
    result, _ = execute(tmp_path, call="mock.crash", arguments={}, approver=lambda request: ApprovalChoice.ALLOW_ONCE)
    assert not result.success and "disconnected" in result.error
    result, calls = execute(tmp_path, tool_risk=allow, arguments={"wrong": 1})
    assert "Missing required argument(s) for 'mock.echo': message" in result.error and calls == []


def test_server_environment_is_scrubbed_and_references_are_filled(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "should-not-leak")
    monkeypatch.setenv("FORGE_TEST_MCP_VALUE", "filled-in")
    settings = server(tool_risk={"env": "read"}, env={"SERVICE_TOKEN": "${FORGE_TEST_MCP_VALUE}"})
    manager, _ = connect(tmp_path, mock=settings)
    with manager:
        client = manager.clients["mock"]
        assert client.call_tool("env", {"name": "OPENAI_API_KEY"}).text == "<unset>"
        assert client.call_tool("env", {"name": "SERVICE_TOKEN"}).text == "filled-in"


def test_missing_env_reference_is_a_clear_error(tmp_path):
    _, statuses = connect(tmp_path, mock=server(env={"X": "${FORGE_TEST_UNSET_VARIABLE}"}))
    assert "FORGE_TEST_UNSET_VARIABLE is not set" in statuses["mock"].error


# --- The agent uses MCP tools like built-in ones -------------------------------------------------


def test_agent_calls_an_mcp_tool(tmp_path):
    config = ForgeConfig(
        workspace=tmp_path,
        verification="off",
        mcp={"servers": {"mock": server(tool_risk={"add": "read"}).model_dump()}},
    )
    provider = FakeModelProvider(
        responses=[ModelResponse(tool_calls=[ToolCall(id="m1", name="mock.add", arguments={"a": 40, "b": 2})]), "It is 42."]
    )
    events = []
    outcome = asyncio.run(run_task(config, "Add 40 and 2", provider=provider, on_event=events.append))

    assert "mock.add" in [tool.name for tool in provider.calls[0]["tools"]]
    (execution,) = outcome.state.tool_history
    assert execution.result.success and execution.result.output == "42"
    assert outcome.evidence.extra["mcp_servers"][0]["connected"] is True
    assert any("MCP server 'mock': 6 tool(s)" in getattr(event, "message", "") for event in events)
    tool_message = provider.calls[1]["messages"][-1]
    assert tool_message.content == "42"


def test_task_runs_without_an_unavailable_server(tmp_path):
    config = ForgeConfig(workspace=tmp_path, mcp={"servers": {"gone": {"command": "forge-no-such-mcp-server-xyz"}}})
    events = []
    outcome = asyncio.run(run_task(config, "hello", provider=FakeModelProvider(responses=["hi"]), on_event=events.append))
    assert outcome.state.status == "completed"
    assert any("MCP server 'gone' is unavailable" in getattr(event, "message", "") for event in events)


# --- Configuration --------------------------------------------------------------------------------


def test_secret_literals_are_refused_but_references_allowed(tmp_path):
    project = tmp_path / "p"
    (project / ".forge").mkdir(parents=True)
    path = project / ".forge" / "config.toml"
    path.write_text('[mcp.servers.github]\ncommand = "npx"\nenv = { GITHUB_TOKEN = "${GITHUB_TOKEN}" }\n')
    assert load_config(workspace=project).config.mcp.servers["github"].env == {"GITHUB_TOKEN": "${GITHUB_TOKEN}"}

    path.write_text('[mcp.servers.github]\ncommand = "npx"\nenv = { GITHUB_TOKEN = "ghp_abcdefghijklmnopqrstuvwxyz0123" }\n')
    with pytest.raises(ConfigError, match="secrets must not be stored"):
        load_config(workspace=project)


def test_server_names_are_validated():
    with pytest.raises(ValueError, match="must start with a letter"):
        McpSettings(servers={"bad.name": {"command": "x"}})


# --- Tool names on the wire -------------------------------------------------------------------------


def test_wire_names_for_openai_compatible_apis():
    assert wire_tool_name("read_file") == "read_file"
    assert wire_tool_name("github.create_issue") == "github__create_issue"
    long = wire_tool_name("server." + "x" * 100)
    assert len(long) == 64 and long != wire_tool_name("server." + "x" * 101)


def test_tool_call_names_are_mapped_back():
    raw_call = SimpleNamespace(id="c1", type="function", function=SimpleNamespace(name="github__create_issue", arguments="{}"))
    message = SimpleNamespace(content="", tool_calls=[raw_call])
    completion = SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="tool_calls")], usage=None, model="m")
    response = _from_openai_completion(completion, {"github__create_issue": "github.create_issue"})
    assert response.tool_calls[0].name == "github.create_issue"


# --- CLI ------------------------------------------------------------------------------------------


@pytest.fixture
def mcp_project(tmp_path, monkeypatch):
    (tmp_path / ".forge").mkdir()
    mock = json.dumps(str(MOCK))
    python = json.dumps(sys.executable)
    (tmp_path / ".forge" / "config.toml").write_text(
        f"[mcp.servers.mock]\ncommand = {python}\nargs = [{mock}]\nenv = {{ API_TOKEN = \"${{FORGE_TEST_TOKEN}}\" }}\n\n"
        f"[mcp.servers.broken]\ncommand = {python}\nargs = [{mock}, \"bad-init\"]\n"
    )
    monkeypatch.setenv("FORGE_TEST_TOKEN", "value-never-printed")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_cli_mcp_list(mcp_project):
    result = runner.invoke(app, ["mcp", "list"])
    assert result.exit_code == 0
    assert "mock" in result.output and "broken" in result.output and "API_TOKEN" in result.output
    assert "value-never-printed" not in result.output


def test_cli_mcp_tools(mcp_project):
    result = runner.invoke(app, ["mcp", "tools", "mock"])
    assert result.exit_code == 0, result.output
    assert "mock.echo" in result.output and "(execute)" in result.output


def test_cli_mcp_test(mcp_project):
    ok = runner.invoke(app, ["mcp", "test", "mock"])
    assert ok.exit_code == 0 and "connected" in ok.output and "2025-11-25" in ok.output
    broken = runner.invoke(app, ["mcp", "test", "broken"])
    assert broken.exit_code == 1 and "init exploded" in broken.output
    unknown = runner.invoke(app, ["mcp", "test", "nope"])
    assert unknown.exit_code == 1 and "No MCP server named 'nope'" in unknown.output
