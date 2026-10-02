import asyncio

from forge.agent.events import AgentFinished, ModelRequested, ToolFinished, ToolStarted
from forge.agent.loop import Agent
from forge.agent.state import AgentStatus
from forge.models.errors import ModelRequestError
from forge.models.providers.fake import FakeModelProvider
from forge.models.types import ModelResponse, ToolCall, Usage
from forge.tools.base import ToolContext
from forge.tools.builtin import create_default_tools
from forge.tools.executor import ToolExecutor
from forge.workspace import Workspace


def call(name, call_id="call_1", **arguments):
    return ToolCall(id=call_id, name=name, arguments=arguments)


def tool_reply(*calls):
    return ModelResponse(tool_calls=list(calls), finish_reason="tool_calls")


def make_agent(root, responses, max_steps=20, events=None):
    provider = FakeModelProvider(responses=responses)
    executor = ToolExecutor(create_default_tools(), ToolContext(workspace=Workspace(root)))
    agent = Agent(provider, executor, max_steps=max_steps, on_event=events.append if events is not None else None)
    return agent, provider


def test_final_answer_without_tools(workspace_root):
    agent, provider = make_agent(workspace_root, ["Nothing to do."])

    state = asyncio.run(agent.run("Say hi"))

    assert state.status == AgentStatus.COMPLETED
    assert state.final_answer == "Nothing to do."
    assert state.step == 1
    assert state.tool_history == []
    assert [m.role for m in state.messages] == ["user", "assistant"]


def test_model_receives_tool_definitions(workspace_root):
    agent, provider = make_agent(workspace_root, ["done"])
    asyncio.run(agent.run("task"))
    tool_names = [tool.name for tool in provider.calls[0]["tools"]]
    assert "read_file" in tool_names


def test_one_tool_call_then_answer(workspace_root):
    agent, provider = make_agent(
        workspace_root,
        [tool_reply(call("read_file", path="allowed.txt")), "The file says hello."],
    )

    state = asyncio.run(agent.run("Read allowed.txt"))

    assert state.status == AgentStatus.COMPLETED
    assert state.final_answer == "The file says hello."
    assert state.step == 2
    assert len(state.tool_history) == 1
    assert state.tool_history[0].result.success


def test_tool_result_is_inserted_into_next_request(workspace_root):
    agent, provider = make_agent(
        workspace_root,
        [tool_reply(call("read_file", call_id="abc", path="allowed.txt")), "done"],
    )

    asyncio.run(agent.run("Read allowed.txt"))

    second_request = provider.calls[1]["messages"]
    assistant, tool_message = second_request[-2], second_request[-1]
    assert assistant.role == "assistant"
    assert assistant.tool_calls[0].id == "abc"
    assert tool_message.role == "tool"
    assert tool_message.tool_call_id == "abc"
    assert tool_message.content == "hello from inside\n"


def test_multiple_sequential_calls(workspace_root):
    agent, provider = make_agent(
        workspace_root,
        [
            tool_reply(call("list_files", call_id="c1")),
            tool_reply(call("read_file", call_id="c2", path="allowed.txt")),
            "Read it.",
        ],
    )

    state = asyncio.run(agent.run("Explore"))

    assert state.status == AgentStatus.COMPLETED
    assert [execution.call.name for execution in state.tool_history] == ["list_files", "read_file"]
    assert [execution.step for execution in state.tool_history] == [1, 2]
    assert state.step == 3


def test_multiple_calls_in_one_response(workspace_root):
    agent, provider = make_agent(
        workspace_root,
        [
            tool_reply(
                call("file_exists", call_id="c1", path="allowed.txt"),
                call("file_exists", call_id="c2", path="missing.txt"),
            ),
            "Checked both.",
        ],
    )

    state = asyncio.run(agent.run("Check files"))

    tool_messages = [m for m in provider.calls[1]["messages"] if m.role == "tool"]
    assert [m.tool_call_id for m in tool_messages] == ["c1", "c2"]
    assert state.step == 2


def test_failed_tool_is_fed_back_and_agent_continues(workspace_root):
    agent, provider = make_agent(
        workspace_root,
        [
            tool_reply(call("read_file", call_id="c1", path="missing.py")),
            tool_reply(call("list_files", call_id="c2")),
            "missing.py does not exist; allowed.txt does.",
        ],
    )

    state = asyncio.run(agent.run("Read missing.py"))

    assert state.status == AgentStatus.COMPLETED
    assert not state.tool_history[0].result.success
    tool_message = provider.calls[1]["messages"][-1]
    assert tool_message.content == "Error: File not found: missing.py"


def test_unknown_tool_is_fed_back(workspace_root):
    agent, provider = make_agent(workspace_root, [tool_reply(call("delete_everything")), "Sorry."])

    state = asyncio.run(agent.run("task"))

    assert state.status == AgentStatus.COMPLETED
    assert "Unknown tool 'delete_everything'" in provider.calls[1]["messages"][-1].content


def test_workspace_escape_is_fed_back(workspace_root):
    agent, provider = make_agent(workspace_root, [tool_reply(call("read_file", path="../outside.txt")), "Blocked."])

    asyncio.run(agent.run("task"))

    content = provider.calls[1]["messages"][-1].content
    assert "outside the workspace" in content
    assert "secret" not in content


def test_max_steps_stops_safely(workspace_root):
    endless = [tool_reply(call("list_files", call_id=f"c{n}")) for n in range(10)]
    agent, provider = make_agent(workspace_root, endless, max_steps=3)

    state = asyncio.run(agent.run("Loop forever"))

    assert state.status == AgentStatus.MAX_STEPS
    assert state.step == 3
    assert len(provider.calls) == 3
    assert "Stopped after 3 steps" in state.error
    assert state.final_answer is None


def test_model_provider_error_stops_with_failed_status(workspace_root):
    agent, _ = make_agent(workspace_root, [tool_reply(call("list_files"))])  # second call has no script left

    state = asyncio.run(agent.run("task"))

    assert state.status == AgentStatus.FAILED
    assert "no scripted responses left" in state.error
    assert len(state.tool_history) == 1


class BrokenProvider(FakeModelProvider):
    async def generate(self, messages, tools=None, **options):
        raise ModelRequestError("connection refused")


def test_provider_failure_on_first_call(workspace_root):
    executor = ToolExecutor(create_default_tools(), ToolContext(workspace=Workspace(workspace_root)))
    state = asyncio.run(Agent(BrokenProvider(), executor).run("task"))
    assert state.status == AgentStatus.FAILED
    assert state.error == "connection refused"
    assert state.step == 1


def test_usage_is_accumulated(workspace_root):
    agent, _ = make_agent(
        workspace_root,
        [
            ModelResponse(tool_calls=[call("list_files")], usage=Usage(input_tokens=10, output_tokens=2)),
            ModelResponse(content="done", usage=Usage(input_tokens=20, output_tokens=3)),
        ],
    )
    state = asyncio.run(agent.run("task"))
    assert state.usage.input_tokens == 30
    assert state.usage.total_tokens == 35


def test_system_prompt_is_first_message(workspace_root):
    provider = FakeModelProvider(responses=["ok"])
    executor = ToolExecutor(create_default_tools(), ToolContext(workspace=Workspace(workspace_root)))
    asyncio.run(Agent(provider, executor, system_prompt="Be careful.").run("task"))
    first = provider.calls[0]["messages"][0]
    assert (first.role, first.content) == ("system", "Be careful.")


def test_events_are_emitted_in_order(workspace_root):
    events = []
    agent, _ = make_agent(workspace_root, [tool_reply(call("list_files")), "done"], events=events)

    asyncio.run(agent.run("task"))

    kinds = [type(event).__name__ for event in events]
    assert kinds == [
        "TaskStarted",
        "ModelRequested",
        "ModelResponded",
        "ToolStarted",
        "ToolFinished",
        "ModelRequested",
        "ModelResponded",
        "AgentFinished",
    ]
    assert isinstance(events[1], ModelRequested)
    assert isinstance(events[3], ToolStarted) and isinstance(events[4], ToolFinished)
    assert isinstance(events[-1], AgentFinished) and events[-1].status == "completed"
