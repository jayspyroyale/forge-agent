"""Running shell commands with hard limits.

This module knows nothing about models or tools. The `run_command` tool and
the verification system both use it, so every command Forge runs gets the
same guarantees:

- it runs in a given working directory;
- it always has a timeout, and on timeout the whole process tree is killed
  (a hung grandchild process must not keep Forge waiting);
- output is capped, keeping the start and the end (where errors usually are);
- environment variables that look like secrets are removed before the command
  starts, so `env` or `printenv` cannot leak API keys into model context.
"""

import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel

_SECRET_NAME = re.compile(r"(API_?KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|PRIVATE_?KEY)", re.IGNORECASE)
_KILL_GRACE_SECONDS = 5


class CommandResult(BaseModel):
    command: str
    cwd: str
    exit_code: int | None  # None if the command timed out or could not start
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float
    timed_out: bool = False
    truncated: bool = False
    error: str | None = None  # set when the command could not be started

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and self.error is None


def scrub_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """A copy of `environ` without variables whose names look like secrets."""
    return {name: value for name, value in environ.items() if not _SECRET_NAME.search(name)}


def truncate_output(text: str, limit: int) -> tuple[str, bool]:
    """Keep the first quarter and last three quarters of `limit` characters."""
    if len(text) <= limit:
        return text, False
    head = limit // 4
    tail = limit - head
    removed = len(text) - head - tail
    return f"{text[:head]}\n[... {removed} characters truncated ...]\n{text[-tail:]}", True


def run_command(
    command: str,
    cwd: Path,
    timeout: float,
    output_limit: int = 12_000,
    env: Mapping[str, str] | None = None,
) -> CommandResult:
    environment = dict(env) if env is not None else scrub_environment(os.environ)
    # Python caches compiled modules by source mtime and size. An agent can edit a
    # file twice within one second without changing its size, and a cached .pyc
    # from a test run in between would then hide the second edit. Commands run by
    # Forge therefore never write bytecode caches.
    environment.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            command,
            shell=True,
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **_new_process_group(),
        )
    except OSError as error:
        return CommandResult(
            command=command,
            cwd=str(cwd),
            exit_code=None,
            duration_seconds=time.monotonic() - started,
            error=f"Could not start command: {error}",
        )

    timed_out = False
    try:
        stdout_bytes, stderr_bytes = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_process_tree(process)
        try:
            stdout_bytes, stderr_bytes = process.communicate(timeout=_KILL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            # Something still holds the pipes open; give up on the output.
            stdout_bytes, stderr_bytes = b"", b""
    duration = time.monotonic() - started

    stdout, stdout_cut = truncate_output(_decode(stdout_bytes), output_limit)
    stderr, stderr_cut = truncate_output(_decode(stderr_bytes), output_limit)
    return CommandResult(
        command=command,
        cwd=str(cwd),
        exit_code=None if timed_out else process.returncode,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=round(duration, 3),
        timed_out=timed_out,
        truncated=stdout_cut or stderr_cut,
    )


def _decode(data: bytes | None) -> str:
    return (data or b"").decode("utf-8", errors="replace").replace("\r\n", "\n")


def _new_process_group() -> dict:
    """Start the command in its own process group so the whole tree can be killed."""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _kill_process_tree(process: subprocess.Popen) -> None:
    if sys.platform == "win32":
        # /T kills child processes too; plain process.kill() would only stop cmd.exe.
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.kill()
    except OSError:
        pass
