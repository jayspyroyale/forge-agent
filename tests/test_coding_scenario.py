"""End-to-end: the runtime has everything needed to inspect, edit, and verify a real (tiny) project.

The model is scripted, so this tests Forge's plumbing, not model quality.
"""

import asyncio
import subprocess
import sys

from forge.agent.runtime import create_agent
from forge.agent.state import AgentStatus
from forge.config import ForgeConfig
from forge.models.providers.fake import FakeModelProvider
from forge.models.types import ModelResponse, ToolCall
from forge.security.permissions import ApprovalChoice

PYTEST = f'"{sys.executable}" -m pytest -q -p no:cacheprovider'


def tool(call_id, name, **arguments):
    return ModelResponse(tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)], finish_reason="tool_calls")


def run_pytest(project):
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], cwd=project, capture_output=True, text=True
    )


def test_example_project_starts_broken(calculator_project):
    result = run_pytest(calculator_project)
    assert result.returncode != 0
    assert "2 failed, 2 passed" in result.stdout


def test_scripted_agent_inspects_edits_and_verifies(calculator_project):
    approvals = []

    def approver(request):
        approvals.append((request.tool_name, request.risk.level.value))
        return ApprovalChoice.ALLOW_ONCE

    provider = FakeModelProvider(
        responses=[
            tool("c1", "run_command", command=PYTEST),
            tool("c2", "search_text", pattern="def multiply"),
            tool("c3", "read_file", path="calculator.py"),
            tool(
                "c4",
                "edit_file",
                path="calculator.py",
                old_text="def multiply(a, b):\n    return a + b  # BUG: should be a * b",
                new_text="def multiply(a, b):\n    return a * b",
            ),
            tool("c5", "run_command", command=PYTEST),
            "Fixed multiply to use *; all 4 tests pass.",
        ]
    )
    agent = create_agent(ForgeConfig(workspace=calculator_project), provider=provider, approver=approver)

    state = asyncio.run(agent.run("The multiply tests fail. Fix the bug and verify."))

    assert state.status == AgentStatus.COMPLETED
    history = state.tool_history
    assert [h.call.name for h in history] == ["run_command", "search_text", "read_file", "edit_file", "run_command"]

    # Inspect: the failing run and the search gave the model what it needed.
    assert not history[0].result.success
    assert "2 failed" in history[0].result.output
    assert "calculator.py:12: def multiply(a, b):" in history[1].result.output

    # Edit: exactly the buggy line changed.
    assert history[3].result.success
    source = (calculator_project / "calculator.py").read_text()
    assert "return a * b" in source
    assert "def add(a, b):\n    return a + b" in source

    # Verify: the second test run passed, and an independent run agrees.
    assert history[4].result.success
    assert "4 passed" in history[4].result.output
    assert run_pytest(calculator_project).returncode == 0

    # Writes and commands went through the approver; reads did not.
    assert approvals == [("run_command", "execute"), ("edit_file", "write"), ("run_command", "execute")]


def test_tool_results_reach_the_model_in_order(calculator_project):
    provider = FakeModelProvider(
        responses=[tool("c1", "read_file", path="calculator.py"), "It adds instead of multiplying."]
    )
    agent = create_agent(ForgeConfig(workspace=calculator_project), provider=provider)

    asyncio.run(agent.run("What is wrong with multiply?"))

    sent = provider.calls[1]["messages"]
    assert [m.role for m in sent] == ["system", "user", "assistant", "tool"]
    assert "return a + b  # BUG" in sent[-1].content
