"""Verification inside the agent loop, and evidence built from what really happened."""

import asyncio
import sys

from forge.agent.runtime import run_task
from forge.agent.state import AgentStatus
from forge.config import ForgeConfig
from forge.models.providers.fake import FakeModelProvider
from forge.models.types import ModelResponse, ToolCall
from forge.security.permissions import ApprovalChoice, PermissionEngine
from forge.security.policy import PermissionPolicy
from forge.verification.checks import VerificationCheck

PYTEST = f'"{sys.executable}" -m pytest -q -p no:cacheprovider'
PYTEST_CHECK = VerificationCheck(name="pytest", kind="test", command=PYTEST, reason="test")

BUG = "def multiply(a, b):\n    return a + b  # BUG: should be a * b"
FIX = "def multiply(a, b):\n    return a * b"
WRONG_FIX = "def multiply(a, b):\n    return a - b"


def tool(call_id, name, **arguments):
    return ModelResponse(tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)], finish_reason="tool_calls")


def edit(call_id, old, new):
    return tool(call_id, "edit_file", path="calculator.py", old_text=old, new_text=new)


def run(project, responses, checks=(PYTEST_CHECK,), permissions=None, **config):
    provider = FakeModelProvider(responses=responses)
    outcome = asyncio.run(
        run_task(
            ForgeConfig(workspace=project, **config),
            "Fix multiply.",
            provider=provider,
            checks=list(checks),
            permissions=permissions or PermissionEngine(PermissionPolicy.permissive()),
        )
    )
    return outcome, provider


def report(evidence):
    return {check.kind: check.outcome for check in evidence.checks}


def test_successful_verification(calculator_project):
    outcome, _ = run(calculator_project, [edit("c1", BUG, FIX), "Fixed."])

    assert outcome.state.status == AgentStatus.COMPLETED
    assert len(outcome.state.verification_rounds) == 1
    evidence = outcome.evidence
    assert report(evidence) == {"test": "verified", "build": "unverified", "typecheck": "unverified", "lint": "unverified"}
    assert evidence.verified
    assert evidence.files_changed == ["calculator.py"]
    assert evidence.verification_rounds[0][0].status == "passed"


def test_failed_verification_retry_then_success(calculator_project):
    outcome, provider = run(
        calculator_project,
        [
            edit("c1", BUG, WRONG_FIX),
            "Fixed it.",  # Forge verifies: fails
            edit("c2", WRONG_FIX, FIX),
            "Fixed it properly.",  # Forge verifies: passes
        ],
    )

    state = outcome.state
    assert state.status == AgentStatus.COMPLETED
    assert [round_.passed for round_ in state.verification_rounds] == [False, True]
    assert state.final_answer == "Fixed it properly."

    # The failure summary went back to the model as a message from Forge.
    feedback = provider.calls[2]["messages"][-1]
    assert feedback.role == "user"
    assert "Forge ran the project's checks" in feedback.content
    assert "pytest" in feedback.content
    assert "failed" in feedback.content
    assert outcome.evidence.verified


def test_verification_gives_up_after_the_attempt_limit(calculator_project):
    outcome, _ = run(
        calculator_project,
        [edit("c1", BUG, WRONG_FIX), "Done.", "Still done."],
        verification_attempts=1,
    )
    assert outcome.state.status == AgentStatus.VERIFICATION_FAILED
    assert report(outcome.evidence)["test"] == "failed"
    assert not outcome.evidence.verified


def test_retries_respect_max_steps(calculator_project):
    responses = [edit("c1", BUG, WRONG_FIX), "Done."] + ["Done."] * 5
    outcome, provider = run(calculator_project, responses, max_steps=2)
    assert outcome.state.status == AgentStatus.VERIFICATION_FAILED
    assert len(provider.calls) == 2


def test_model_cannot_claim_verification_without_running_it(calculator_project):
    # The model edits wrongly and *says* the tests pass. Verification is off,
    # so Forge never ran the tests: the report must not show them as verified.
    outcome, _ = run(
        calculator_project,
        [edit("c1", BUG, WRONG_FIX), "All tests pass! Verified with pytest: 4 passed."],
        verification="off",
    )
    evidence = outcome.evidence
    assert outcome.state.status == AgentStatus.COMPLETED
    assert "All tests pass" in evidence.final_answer
    assert evidence.verification_rounds == []
    assert report(evidence)["test"] == "unverified"
    assert not evidence.verified


def test_model_claim_is_contradicted_by_actual_checks(calculator_project):
    outcome, _ = run(
        calculator_project,
        [edit("c1", BUG, WRONG_FIX), "All tests pass!"],
        verification_attempts=1,
    )
    assert outcome.state.status == AgentStatus.VERIFICATION_FAILED
    assert report(outcome.evidence)["test"] == "failed"


def test_no_verifier_configured(calculator_project):
    outcome, _ = run(calculator_project, [edit("c1", BUG, FIX), "Fixed."], checks=())
    assert outcome.state.status == AgentStatus.COMPLETED
    assert set(report(outcome.evidence).values()) == {"unverified"}
    assert outcome.evidence.checks[0].detail == "not configured"


def test_no_changes_means_no_verification(calculator_project):
    outcome, _ = run(calculator_project, [tool("c1", "read_file", path="calculator.py"), "It adds."])
    assert outcome.state.verification_rounds == []
    assert report(outcome.evidence)["test"] == "unverified"
    assert outcome.evidence.checks[0].detail == "not run"


def test_denied_verification_is_unverified_not_failed(calculator_project):
    def approver(request):
        return ApprovalChoice.DENY if request.tool_name == "verification" else ApprovalChoice.ALLOW_ONCE

    outcome, _ = run(calculator_project, [edit("c1", BUG, FIX), "Fixed."], permissions=PermissionEngine(approver=approver))

    assert outcome.state.status == AgentStatus.COMPLETED
    test_report = outcome.evidence.checks[0]
    assert test_report.outcome == "unverified"
    assert "not allowed" in test_report.detail


def test_evidence_records_tools_commands_and_usage(calculator_project):
    outcome, _ = run(
        calculator_project,
        [
            tool("c1", "read_file", path="calculator.py"),
            tool("c2", "run_command", command=PYTEST),
            edit("c3", BUG, FIX),
            "Fixed.",
        ],
    )
    evidence = outcome.evidence
    assert [usage.name for usage in evidence.tool_usage] == ["read_file", "run_command", "edit_file"]
    assert evidence.tool_usage[0].risk == "read"
    assert evidence.tool_usage[2].changed_paths == ["calculator.py"]
    assert evidence.commands_run[0].exit_code == 1  # the agent's own test run, before the fix
    assert evidence.steps == 4
    assert evidence.provider == "fake"
    assert evidence.duration_seconds is not None
    assert evidence.task_id == outcome.state.task_id


def test_denied_tools_are_recorded(calculator_project):
    def approver(request):
        return ApprovalChoice.DENY

    outcome, _ = run(calculator_project, [edit("c1", BUG, FIX), "Could not edit."], permissions=PermissionEngine(approver=approver))
    usage = outcome.evidence.tool_usage[0]
    assert usage.denied
    assert not usage.success
    assert outcome.evidence.files_changed == []


def _rewrite_with_command(call_id, new_body):
    script = f"import pathlib; p = pathlib.Path('calculator.py'); p.write_text(p.read_text().replace({BUG!r}, {new_body!r}))"
    return tool(call_id, "run_command", command=f'"{sys.executable}" -c "{script}"')


def test_changes_made_by_a_command_are_verified_before_reporting(calculator_project):
    # The model breaks the code with a command (not an edit tool) and claims success.
    outcome, _ = run(calculator_project, [_rewrite_with_command("c1", WRONG_FIX), "Done, all good."])

    assert "return a - b" in (calculator_project / "calculator.py").read_text()
    assert len(outcome.state.verification_rounds) == 1  # Forge's final check
    assert outcome.state.status == AgentStatus.VERIFICATION_FAILED
    assert report(outcome.evidence)["test"] == "failed"


def test_command_fix_is_verified_and_passes(calculator_project):
    outcome, _ = run(calculator_project, [_rewrite_with_command("c1", FIX), "Fixed with a command."])

    assert outcome.state.status == AgentStatus.COMPLETED
    assert report(outcome.evidence)["test"] == "verified"


def test_no_final_verification_without_changes(calculator_project):
    outcome, _ = run(calculator_project, [tool("c1", "run_command", command=PYTEST), "Tests fail, nothing changed."])

    assert outcome.state.verification_rounds == []
    assert outcome.state.status == AgentStatus.COMPLETED
