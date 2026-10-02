"""The run_command tool: the model's only way to run a program.

The tool resolves the working directory through the workspace and applies the
configured timeout and output limits; the actual process handling lives in
`forge.terminal`. Authorization happens before this tool runs, in the
executor (see Phase 7), not here.
"""

import sys

from pydantic import Field

from forge.security.commands import classify_command
from forge.security.risk import RiskAssessment, RiskLevel
from forge.terminal import CommandResult, run_command
from forge.tools.base import Tool, ToolArgs, ToolContext, ToolError, ToolResult

SHELL_NAME = "cmd.exe" if sys.platform == "win32" else "/bin/sh"


class RunCommandArgs(ToolArgs):
    command: str = Field(min_length=1, description="The command line to run.")
    cwd: str = Field(default=".", description="Working directory, relative to the workspace root.")
    timeout: float | None = Field(
        default=None, gt=0, description="Seconds before the command is stopped. Defaults to the configured timeout."
    )


class RunCommand(Tool):
    name = "run_command"
    description = (
        f"Run a shell command ({SHELL_NAME}) inside the workspace and return its exit code, stdout, and stderr. "
        "Use it for tests, builds, and other project commands. Commands are stopped when they time out, "
        "and long output is truncated."
    )
    Args = RunCommandArgs
    risk = RiskLevel.EXECUTE
    context_source = "terminal"

    def context_reference(self, arguments):
        command = arguments.get("command")
        return command if isinstance(command, str) else None

    def assess_risk(self, args: RunCommandArgs, context: ToolContext) -> RiskAssessment:
        return classify_command(args.command, context.workspace.root)

    def approval_key(self, args: RunCommandArgs) -> str:
        # A session approval covers this exact command line, not every command.
        return f"{self.name}:{args.command}"

    def execute(self, args: RunCommandArgs, context: ToolContext) -> ToolResult:
        cwd = context.workspace.resolve(args.cwd)
        if not cwd.is_dir():
            raise ToolError(f"Working directory does not exist: {args.cwd}")

        settings = context.config.terminal
        timeout = min(args.timeout or settings.timeout, settings.max_timeout)
        result = run_command(args.command, cwd=cwd, timeout=timeout, output_limit=settings.output_limit)

        metadata = {
            "command": result.command,
            "cwd": context.workspace.relative(cwd),
            "exit_code": result.exit_code,
            "duration_seconds": result.duration_seconds,
            "timed_out": result.timed_out,
            "truncated": result.truncated,
        }
        output = format_command_result(result)
        if result.succeeded:
            return ToolResult.ok(output, **metadata)
        return ToolResult.fail(_failure_summary(result, timeout), output=output, **metadata)


def format_command_result(result: CommandResult) -> str:
    lines = [f"$ {result.command}"]
    if result.error:
        lines.append(result.error)
        return "\n".join(lines)
    status = "timed out" if result.timed_out else f"exit code {result.exit_code}"
    lines.append(f"[{status}, {result.duration_seconds:.1f}s{', output truncated' if result.truncated else ''}]")
    if result.stdout:
        lines += ["--- stdout ---", result.stdout.rstrip("\n")]
    if result.stderr:
        lines += ["--- stderr ---", result.stderr.rstrip("\n")]
    if not result.stdout and not result.stderr:
        lines.append("(no output)")
    return "\n".join(lines)


def _failure_summary(result: CommandResult, timeout: float) -> str:
    if result.error:
        return result.error
    if result.timed_out:
        return f"Command timed out after {timeout:g} seconds and was stopped"
    return f"Command exited with code {result.exit_code}"
