"""Tests for command execution. Commands use the current Python interpreter so they work on every OS."""

import sys
import time

from forge.terminal import run_command, scrub_environment, truncate_output
from helpers import run_tool

PYTHON = f'"{sys.executable}"'


def py(code: str) -> str:
    """A shell command that runs `code` (which must not contain double quotes)."""
    return f'{PYTHON} -c "{code}"'


# --- forge.terminal.run_command -------------------------------------------------


def test_successful_command_captures_stdout(tmp_path):
    result = run_command(py("print('hello')"), cwd=tmp_path, timeout=30)
    assert result.exit_code == 0
    assert result.succeeded
    assert result.stdout == "hello\n"
    assert result.stderr == ""
    assert result.timed_out is False
    assert result.duration_seconds >= 0


def test_stderr_is_captured_separately(tmp_path):
    result = run_command(py("import sys; sys.stderr.write('oops')"), cwd=tmp_path, timeout=30)
    assert result.stdout == ""
    assert result.stderr == "oops"


def test_nonzero_exit(tmp_path):
    result = run_command(py("import sys; sys.exit(3)"), cwd=tmp_path, timeout=30)
    assert result.exit_code == 3
    assert not result.succeeded


def test_runs_in_working_directory(tmp_path):
    (tmp_path / "sub").mkdir()
    result = run_command(py("import os; print(os.getcwd())"), cwd=tmp_path / "sub", timeout=30)
    assert result.stdout.strip().lower() == str((tmp_path / "sub").resolve()).lower()


def test_timeout_stops_command(tmp_path):
    started = time.monotonic()
    result = run_command(py("import time; time.sleep(60)"), cwd=tmp_path, timeout=1)
    assert result.timed_out
    assert result.exit_code is None
    assert not result.succeeded
    assert time.monotonic() - started < 20


def test_timeout_kills_grandchild_processes(tmp_path):
    # The command starts a child that sleeps and inherits the output pipes.
    # Without killing the whole tree, reading output would wait for the child.
    code = (
        "import subprocess, sys, time; "
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        "time.sleep(60)"
    )
    started = time.monotonic()
    result = run_command(py(code), cwd=tmp_path, timeout=2)
    assert result.timed_out
    assert time.monotonic() - started < 20


def test_long_output_is_truncated_keeping_head_and_tail(tmp_path):
    code = "print('START' + 'x' * 50000 + 'END')"
    result = run_command(py(code), cwd=tmp_path, timeout=30, output_limit=1000)
    assert result.truncated
    assert result.stdout.startswith("START")
    assert result.stdout.rstrip().endswith("END")
    assert "characters truncated" in result.stdout
    assert len(result.stdout) < 1200


def test_missing_executable(tmp_path):
    result = run_command("definitely_not_a_real_command_xyz", cwd=tmp_path, timeout=30)
    assert result.exit_code not in (0, None)
    assert not result.succeeded
    assert result.stderr


def test_missing_working_directory_is_reported(tmp_path):
    result = run_command("echo hi", cwd=tmp_path / "missing", timeout=30)
    assert result.error is not None
    assert not result.succeeded


def test_secret_environment_variables_are_hidden(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_SERVICE_API_KEY", "super-secret-value")
    monkeypatch.setenv("FORGE_VISIBLE_SETTING", "visible")
    code = "import os; print(os.environ.get('MY_SERVICE_API_KEY', 'missing'), os.environ.get('FORGE_VISIBLE_SETTING'))"
    result = run_command(py(code), cwd=tmp_path, timeout=30)
    assert result.stdout.strip() == "missing visible"


def test_scrub_environment():
    scrubbed = scrub_environment(
        {"PATH": "/bin", "OPENAI_API_KEY": "x", "GITHUB_TOKEN": "y", "DB_PASSWORD": "z", "HOME": "/home/me"}
    )
    assert scrubbed == {"PATH": "/bin", "HOME": "/home/me"}


def test_truncate_output_short_text_is_unchanged():
    assert truncate_output("short", 100) == ("short", False)


# --- run_command tool -----------------------------------------------------------------


def test_tool_success_result(workspace_root):
    result = run_tool(workspace_root, "run_command", command=py("print('tool ok')"))
    assert result.success
    assert "tool ok" in result.output
    assert "[exit code 0" in result.output
    assert result.metadata["exit_code"] == 0
    assert result.metadata["timed_out"] is False
    assert result.metadata["cwd"] == "."


def test_tool_failure_includes_output_for_the_model(workspace_root):
    result = run_tool(workspace_root, "run_command", command=py("import sys; print('details'); sys.exit(2)"))
    assert not result.success
    assert result.error == "Command exited with code 2"
    content = result.to_model_content()
    assert "details" in content
    assert "exit code 2" in content


def test_tool_runs_in_workspace_by_default(workspace_root):
    result = run_tool(workspace_root, "run_command", command=py("import os; print(sorted(os.listdir('.')))"))
    assert "allowed.txt" in result.output


def test_tool_cwd_must_stay_in_workspace(workspace_root):
    result = run_tool(workspace_root, "run_command", command="echo hi", cwd="..")
    assert not result.success
    assert "outside the workspace" in result.error


def test_tool_cwd_must_exist(workspace_root):
    result = run_tool(workspace_root, "run_command", command="echo hi", cwd="missing")
    assert not result.success
    assert "does not exist" in result.error


def test_tool_timeout(workspace_root):
    result = run_tool(workspace_root, "run_command", command=py("import time; time.sleep(60)"), timeout=1)
    assert not result.success
    assert "timed out" in result.error
    assert result.metadata["timed_out"] is True


def test_tool_timeout_is_capped_by_config(workspace_root):
    import asyncio

    from forge.config import ForgeConfig, TerminalSettings
    from forge.models.types import ToolCall
    from forge.security.permissions import PermissionEngine
    from forge.security.policy import PermissionPolicy
    from forge.tools.base import ToolContext
    from forge.tools.builtin import create_default_tools
    from forge.tools.executor import ToolExecutor
    from forge.workspace import Workspace

    config = ForgeConfig(terminal=TerminalSettings(timeout=1, max_timeout=1))
    context = ToolContext(workspace=Workspace(workspace_root), config=config)
    executor = ToolExecutor(create_default_tools(), context, PermissionEngine(PermissionPolicy.permissive()))
    call = ToolCall(id="t", name="run_command", arguments={"command": py("import time; time.sleep(60)"), "timeout": 500})

    started = time.monotonic()
    result = asyncio.run(executor.execute(call))

    assert result.metadata["timed_out"] is True
    assert time.monotonic() - started < 20
