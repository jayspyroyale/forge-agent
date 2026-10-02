import asyncio

from forge.agent.loop import Agent
from forge.models.providers.fake import FakeModelProvider
from forge.models.types import ModelResponse, ToolCall
from forge.security.permissions import ApprovalChoice, PermissionEngine, PermissionRequest
from forge.security.policy import Decision, PermissionPolicy
from forge.security.risk import RiskAssessment, RiskLevel
from forge.tools.base import Tool, ToolContext, ToolResult
from forge.tools.builtin import create_default_tools
from forge.tools.executor import ToolExecutor
from forge.tools.registry import ToolRegistry
from forge.workspace import Workspace


class RecordingApprover:
    """A scripted stand-in for a person answering approval prompts."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests: list[PermissionRequest] = []

    def __call__(self, request):
        self.requests.append(request)
        return self.answers.pop(0)


def request(level, key="tool", reasons=()):
    return PermissionRequest(
        tool_name="tool", risk=RiskAssessment(level=level, reasons=list(reasons)), approval_key=key
    )


# --- Engine ---------------------------------------------------------------------


def test_default_policy():
    policy = PermissionPolicy()
    assert policy.decision_for(RiskLevel.READ) == Decision.ALLOW
    assert policy.decision_for(RiskLevel.WRITE) == Decision.ASK
    assert policy.decision_for(RiskLevel.EXECUTE) == Decision.ASK
    assert policy.decision_for(RiskLevel.DANGEROUS) == Decision.DENY


def test_read_is_allowed_without_asking():
    approver = RecordingApprover()
    outcome = PermissionEngine(approver=approver).authorize(request(RiskLevel.READ))
    assert outcome.allowed
    assert outcome.decided_by == "policy"
    assert approver.requests == []


def test_write_requires_approval():
    approver = RecordingApprover(ApprovalChoice.ALLOW_ONCE)
    outcome = PermissionEngine(approver=approver).authorize(request(RiskLevel.WRITE))
    assert outcome.allowed
    assert outcome.decided_by == "user"
    assert len(approver.requests) == 1


def test_user_denial():
    outcome = PermissionEngine(approver=RecordingApprover(ApprovalChoice.DENY)).authorize(request(RiskLevel.WRITE))
    assert not outcome.allowed
    assert outcome.reason == "denied by the user"


def test_allow_once_asks_again_next_time():
    approver = RecordingApprover(ApprovalChoice.ALLOW_ONCE, ApprovalChoice.DENY)
    engine = PermissionEngine(approver=approver)
    assert engine.authorize(request(RiskLevel.WRITE)).allowed
    assert not engine.authorize(request(RiskLevel.WRITE)).allowed
    assert len(approver.requests) == 2


def test_session_approval_is_remembered_per_key():
    approver = RecordingApprover(ApprovalChoice.ALLOW_SESSION, ApprovalChoice.DENY)
    engine = PermissionEngine(approver=approver)

    assert engine.authorize(request(RiskLevel.WRITE, key="write_file")).allowed
    second = engine.authorize(request(RiskLevel.WRITE, key="write_file"))
    assert second.allowed
    assert second.decided_by == "session"
    # A different key still asks.
    assert not engine.authorize(request(RiskLevel.EXECUTE, key="run_command:rm x")).allowed
    assert len(approver.requests) == 2


def test_dangerous_is_denied_even_with_an_approver():
    approver = RecordingApprover(ApprovalChoice.ALLOW_ONCE)
    outcome = PermissionEngine(approver=approver).authorize(request(RiskLevel.DANGEROUS, reasons=["rm -r"]))
    assert not outcome.allowed
    assert outcome.decided_by == "policy"
    assert "rm -r" in outcome.reason
    assert approver.requests == []


def test_dangerous_can_never_be_approved_for_the_session():
    policy = PermissionPolicy(dangerous=Decision.ASK)
    approver = RecordingApprover(ApprovalChoice.ALLOW_SESSION, ApprovalChoice.DENY)
    engine = PermissionEngine(policy, approver)
    assert engine.authorize(request(RiskLevel.DANGEROUS, key="k")).allowed
    assert not engine.authorize(request(RiskLevel.DANGEROUS, key="k")).allowed


def test_ask_without_approver_is_denied():
    outcome = PermissionEngine().authorize(request(RiskLevel.EXECUTE))
    assert not outcome.allowed
    assert outcome.decided_by == "no_approver"


def test_approver_interrupted_counts_as_denial():
    def interrupted(_):
        raise EOFError

    assert not PermissionEngine(approver=interrupted).authorize(request(RiskLevel.WRITE)).allowed


def test_custom_policy():
    engine = PermissionEngine(PermissionPolicy(write=Decision.ALLOW, read=Decision.DENY))
    assert engine.authorize(request(RiskLevel.WRITE)).allowed
    assert not engine.authorize(request(RiskLevel.READ)).allowed


def test_history_records_every_decision():
    engine = PermissionEngine()
    engine.authorize(request(RiskLevel.READ))
    engine.authorize(request(RiskLevel.DANGEROUS))
    assert [record.outcome.allowed for record in engine.history] == [True, False]


# --- Executor: authorization happens before execution ------------------------------


class SpyTool(Tool):
    name = "spy"
    description = "Records whether it ran."
    risk = RiskLevel.WRITE

    def __init__(self):
        self.ran = False

    def execute(self, args, context):
        self.ran = True
        return ToolResult.ok("ran")


def run_spy(tmp_path, approver):
    tool = SpyTool()
    executor = ToolExecutor(
        ToolRegistry([tool]), ToolContext(workspace=Workspace(tmp_path)), PermissionEngine(approver=approver)
    )
    result = asyncio.run(executor.execute(ToolCall(id="c", name="spy")))
    return tool, result


def test_tool_does_not_run_when_denied(tmp_path):
    tool, result = run_spy(tmp_path, RecordingApprover(ApprovalChoice.DENY))
    assert not tool.ran
    assert not result.success
    assert result.metadata["denied"] is True
    assert "Permission denied" in result.error


def test_tool_is_not_executed_before_authorization(tmp_path):
    tool = SpyTool()
    seen_before_decision = []

    def approver(_request):
        seen_before_decision.append(tool.ran)
        return ApprovalChoice.ALLOW_ONCE

    executor = ToolExecutor(
        ToolRegistry([tool]), ToolContext(workspace=Workspace(tmp_path)), PermissionEngine(approver=approver)
    )
    result = asyncio.run(executor.execute(ToolCall(id="c", name="spy")))

    assert seen_before_decision == [False]
    assert tool.ran
    assert result.success
    assert result.metadata["permission"] == {"risk": "write", "decided_by": "user"}


def test_dangerous_command_never_reaches_the_shell(workspace_root, monkeypatch):
    calls = []
    monkeypatch.setattr("forge.tools.builtin.terminal.run_command", lambda *a, **k: calls.append(a))
    approver = RecordingApprover(ApprovalChoice.ALLOW_ONCE)
    executor = ToolExecutor(
        create_default_tools(), ToolContext(workspace=Workspace(workspace_root)), PermissionEngine(approver=approver)
    )

    result = asyncio.run(executor.execute(ToolCall(id="c", name="run_command", arguments={"command": "rm -rf ."})))

    assert calls == []
    assert approver.requests == []
    assert not result.success
    assert "dangerous actions are denied" in result.error


def test_read_tools_need_no_approval(workspace_root):
    executor = ToolExecutor(create_default_tools(), ToolContext(workspace=Workspace(workspace_root)))
    result = asyncio.run(executor.execute(ToolCall(id="c", name="read_file", arguments={"path": "allowed.txt"})))
    assert result.success


def test_write_without_approver_is_denied_and_file_untouched(workspace_root):
    executor = ToolExecutor(create_default_tools(), ToolContext(workspace=Workspace(workspace_root)))
    result = asyncio.run(
        executor.execute(ToolCall(id="c", name="write_file", arguments={"path": "new.txt", "content": "x"}))
    )
    assert not result.success
    assert not (workspace_root / "new.txt").exists()


def test_command_session_approval_covers_only_that_command(workspace_root, monkeypatch):
    approver = RecordingApprover(ApprovalChoice.ALLOW_SESSION, ApprovalChoice.DENY)
    engine = PermissionEngine(approver=approver)
    executor = ToolExecutor(create_default_tools(), ToolContext(workspace=Workspace(workspace_root)), engine)

    def run(command):
        return asyncio.run(executor.execute(ToolCall(id="c", name="run_command", arguments={"command": command})))

    assert run("echo one").success
    assert run("echo one").success  # remembered
    assert not run("echo two").success  # different command: asked, denied
    assert len(approver.requests) == 2


# --- Denials flow back to the agent -------------------------------------------------


def test_denied_result_reaches_the_model_and_agent_continues(workspace_root):
    provider = FakeModelProvider(
        responses=[
            ModelResponse(
                tool_calls=[ToolCall(id="w1", name="write_file", arguments={"path": "new.txt", "content": "x"})]
            ),
            "I was not allowed to write the file.",
        ]
    )
    engine = PermissionEngine(approver=RecordingApprover(ApprovalChoice.DENY))
    executor = ToolExecutor(create_default_tools(), ToolContext(workspace=Workspace(workspace_root)), engine)

    state = asyncio.run(Agent(provider, executor).run("Create new.txt"))

    assert state.status == "completed"
    tool_message = provider.calls[1]["messages"][-1]
    assert tool_message.role == "tool"
    assert "Permission denied: denied by the user" in tool_message.content
    assert not (workspace_root / "new.txt").exists()
