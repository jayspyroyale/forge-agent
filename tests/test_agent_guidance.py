"""Behavior contracts of the agent loop that don't depend on what a model says."""

import asyncio

from forge.agent.guidance import FailureTracker
from forge.agent.loop import Agent
from forge.agent.prompts import EMPTY_RESPONSE_NOTE, LAST_STEP_NOTE
from forge.agent.state import AgentStatus
from forge.models.providers.fake import FakeModelProvider
from forge.models.types import ModelResponse, ToolCall
from forge.tools.base import ToolContext, ToolResult
from forge.tools.builtin import create_default_tools
from forge.tools.executor import ToolExecutor
from forge.workspace import Workspace


def read_missing(call_id):
    return ModelResponse(tool_calls=[ToolCall(id=call_id, name="read_file", arguments={"path": "missing.py"})])


def run_agent(root, responses, max_steps=20):
    provider = FakeModelProvider(responses=responses)
    executor = ToolExecutor(create_default_tools(), ToolContext(workspace=Workspace(root)))
    state = asyncio.run(Agent(provider, executor, max_steps=max_steps).run("task"))
    return state, provider


def tool_messages(provider, call_index):
    return [m for m in provider.calls[call_index]["messages"] if m.role == "tool"]


def test_first_failure_has_no_extra_note(workspace_root):
    _, provider = run_agent(workspace_root, [read_missing("c1"), "done"])
    assert tool_messages(provider, 1)[-1].content == "Error: File not found: missing.py"


def test_repeated_identical_failure_gets_a_note(workspace_root):
    _, provider = run_agent(workspace_root, [read_missing("c1"), read_missing("c2"), "done"])
    second = tool_messages(provider, 2)[-1].content
    assert second.startswith("Error: File not found: missing.py")
    assert "failed 2 times" in second
    assert "Do not repeat it" in second


def test_different_failing_calls_are_not_called_repeats():
    tracker = FailureTracker()
    failure = ToolResult.fail("nope")
    assert tracker.note_for(ToolCall(id="1", name="read_file", arguments={"path": "a"}), failure) is None
    assert tracker.note_for(ToolCall(id="2", name="read_file", arguments={"path": "b"}), failure) is None


def test_consecutive_failures_trigger_a_step_back_note():
    tracker = FailureTracker(consecutive_threshold=3)
    failure = ToolResult.fail("nope")
    notes = [tracker.note_for(ToolCall(id=str(n), name="t", arguments={"n": n}), failure) for n in range(3)]
    assert notes[:2] == [None, None]
    assert "last 3 tool calls failed" in notes[2]


def test_success_resets_the_consecutive_counter():
    tracker = FailureTracker(consecutive_threshold=2)
    tracker.note_for(ToolCall(id="1", name="t", arguments={"n": 1}), ToolResult.fail("x"))
    tracker.note_for(ToolCall(id="2", name="t", arguments={"n": 2}), ToolResult.ok("fine"))
    assert tracker.note_for(ToolCall(id="3", name="t", arguments={"n": 3}), ToolResult.fail("x")) is None


def test_empty_reply_gets_one_nudge(workspace_root):
    state, provider = run_agent(workspace_root, ["", "Real answer."])
    assert state.status == AgentStatus.COMPLETED
    assert state.final_answer == "Real answer."
    assert provider.calls[1]["messages"][-1].content == EMPTY_RESPONSE_NOTE


def test_second_empty_reply_ends_the_task(workspace_root):
    state, provider = run_agent(workspace_root, ["", ""])
    assert state.status == AgentStatus.COMPLETED
    assert state.final_answer == ""
    assert len(provider.calls) == 2


def test_last_step_warning_is_sent_before_the_final_allowed_call(workspace_root):
    endless = [read_missing(f"c{n}") for n in range(5)]
    state, provider = run_agent(workspace_root, endless, max_steps=3)
    assert state.status == AgentStatus.MAX_STEPS
    assert LAST_STEP_NOTE not in [m.content for m in provider.calls[1]["messages"]]
    assert provider.calls[2]["messages"][-1].content == LAST_STEP_NOTE


def test_no_last_step_warning_with_a_single_step(workspace_root):
    _, provider = run_agent(workspace_root, ["done"], max_steps=1)
    assert LAST_STEP_NOTE not in [m.content for m in provider.calls[0]["messages"]]
