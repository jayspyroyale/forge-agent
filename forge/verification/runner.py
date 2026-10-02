"""Runs verification checks through the same permission engine and terminal limits as tools."""

from collections.abc import Sequence

from forge.config import TerminalSettings
from forge.security.commands import classify_command
from forge.security.permissions import PermissionEngine, PermissionRequest
from forge.terminal import CommandResult, run_command
from forge.verification.checks import CheckKind, VerificationCheck, VerificationResult
from forge.workspace import Workspace

SUMMARY_LINES = 30
SUMMARY_CHARS = 3000


class Verifier:
    def __init__(
        self,
        workspace: Workspace,
        checks: Sequence[VerificationCheck],
        permissions: PermissionEngine,
        terminal: TerminalSettings | None = None,
    ) -> None:
        self.workspace = workspace
        self.checks = list(checks)
        self.permissions = permissions
        self.terminal = terminal or TerminalSettings()

    @property
    def available_kinds(self) -> set[CheckKind]:
        return {check.kind for check in self.checks}

    def run_check(self, check: VerificationCheck) -> VerificationResult:
        request = PermissionRequest(
            tool_name="verification",
            arguments={"command": check.command},
            risk=classify_command(check.command, self.workspace.root),
            approval_key=f"verification:{check.command}",
        )
        outcome = self.permissions.authorize(request)
        if not outcome.allowed:
            return VerificationResult(
                name=check.name,
                kind=check.kind,
                command=check.command,
                status="skipped",
                summary=f"Not run: {outcome.reason}",
            )

        result = run_command(
            check.command,
            cwd=self.workspace.root,
            timeout=self.terminal.max_timeout,
            output_limit=self.terminal.output_limit,
        )
        return VerificationResult(
            name=check.name,
            kind=check.kind,
            command=check.command,
            status=_status(result),
            exit_code=result.exit_code,
            duration_seconds=result.duration_seconds,
            summary=_summary(result),
        )


def _status(result: CommandResult) -> str:
    if result.error:
        return "error"
    if result.timed_out:
        return "timed_out"
    return "passed" if result.exit_code == 0 else "failed"


def _summary(result: CommandResult) -> str:
    if result.error:
        return result.error
    text = "\n".join(part.rstrip("\n") for part in (result.stdout, result.stderr) if part.strip())
    lines = text.splitlines()[-SUMMARY_LINES:]
    summary = "\n".join(lines)
    return summary[-SUMMARY_CHARS:]
