"""A small MCP client for servers that speak the stdio transport.

    Forge (client)                                 MCP server (subprocess)
        initialize  ------------------------------>
                    <------------------------------  result: protocolVersion, capabilities
        notifications/initialized  --------------->
        tools/list  ------------------------------>  (paginated with nextCursor)
        tools/call  ------------------------------>  result: content[], isError

Messages are JSON-RPC 2.0 objects, one per line, on the server's stdin and
stdout (protocol revision 2025-11-25). A reader thread matches responses to
requests by id. Every request has a timeout, and a crashed or disconnected
server fails pending and future requests with a clear error instead of
hanging Forge. The server is started with Forge's scrubbed environment
(secret-looking variables removed) plus only the variables configured for it.

Forge implements this itself instead of depending on an SDK: the client
needs about two hundred lines, and keeping it here keeps every byte that
crosses the trust boundary visible.
"""

import contextlib
import itertools
import json
import os
import shutil
import subprocess
import sys
import threading
from collections import deque
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from forge import __version__
from forge.security.secret_scan import redact_secrets
from forge.terminal import _new_process_group, _kill_process_tree

PROTOCOL_VERSION = "2025-11-25"
MAX_PAGES = 20
STDERR_LINES_KEPT = 50
MAX_MESSAGE_BYTES = 2_000_000


class McpError(Exception):
    pass


class McpConnectionError(McpError):
    """The server could not be started, did not initialize, or is no longer running."""


class McpTimeoutError(McpError):
    pass


class McpProtocolError(McpError):
    """The server sent something that does not follow the protocol."""


class McpRemoteError(McpError):
    """The server answered a request with a JSON-RPC error."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"{redact_secrets(message)} (code {code})")
        self.code = code


class McpToolInfo(BaseModel):
    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    annotations: dict[str, Any] = Field(default_factory=dict)


class McpCallResult(BaseModel):
    text: str
    is_error: bool = False


class _Pending:
    def __init__(self) -> None:
        self.done = threading.Event()
        self.response: dict[str, Any] | None = None
        self.error: McpError | None = None


class McpClient:
    def __init__(
        self,
        name: str,
        command: list[str],
        *,
        env: dict[str, str] | None = None,
        cwd: Path | None = None,
        startup_timeout: float = 15.0,
        call_timeout: float = 30.0,
    ) -> None:
        self.name = name
        self.command = command
        self.env = env if env is not None else dict(os.environ)
        self.cwd = cwd
        self.startup_timeout = startup_timeout
        self.call_timeout = call_timeout
        self.server_info: dict[str, Any] = {}
        self.protocol_version: str | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._ids = itertools.count(1)
        self._pending: dict[int, _Pending] = {}
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._closed = threading.Event()
        self._disconnect_reason: str | None = None
        self.stderr_tail: deque[str] = deque(maxlen=STDERR_LINES_KEPT)

    # --- lifecycle ------------------------------------------------------------------------

    def connect(self) -> None:
        executable = shutil.which(self.command[0]) or self.command[0]
        try:
            self._process = subprocess.Popen(
                [executable, *self.command[1:]],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self.cwd,
                env=self.env,
                **_new_process_group(),
            )
        except OSError as error:
            raise McpConnectionError(
                f"Could not start MCP server '{self.name}' ({self.command[0]}): {error}"
            ) from error
        threading.Thread(target=self._read_stdout, name=f"mcp-{self.name}-out", daemon=True).start()
        threading.Thread(target=self._read_stderr, name=f"mcp-{self.name}-err", daemon=True).start()

        try:
            result = self.request(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "forge", "version": __version__},
                },
                timeout=self.startup_timeout,
            )
        except McpError as error:
            self.close()
            raise McpConnectionError(f"MCP server '{self.name}' did not initialize: {error}") from error
        if not isinstance(result, dict) or "protocolVersion" not in result:
            self.close()
            raise McpConnectionError(f"MCP server '{self.name}' sent an invalid initialize result")
        self.protocol_version = str(result["protocolVersion"])
        self.server_info = result.get("serverInfo") or {}
        self.notify("notifications/initialized")

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        process = self._process
        if process is not None:
            with contextlib.suppress(OSError, ValueError):
                process.stdin.close()  # type: ignore[union-attr]
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                _kill_process_tree(process)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=2)
        self._fail_all(f"MCP server '{self.name}' was disconnected")

    @property
    def connected(self) -> bool:
        return (
            self._process is not None
            and self._process.poll() is None
            and not self._closed.is_set()
            and self._disconnect_reason is None
        )

    def __enter__(self) -> "McpClient":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- MCP methods ----------------------------------------------------------------------

    def list_tools(self) -> list[dict[str, Any]]:
        """Raw tool descriptions from the server (validated by the adapter, not here)."""
        tools: list[dict[str, Any]] = []
        cursor = None
        for _ in range(MAX_PAGES):
            result = self.request("tools/list", {"cursor": cursor} if cursor else {})
            if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
                raise McpProtocolError(f"MCP server '{self.name}' sent an invalid tools/list result")
            tools += result["tools"]
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    def call_tool(self, name: str, arguments: dict[str, Any], timeout: float | None = None) -> McpCallResult:
        result = self.request("tools/call", {"name": name, "arguments": arguments}, timeout=timeout)
        if not isinstance(result, dict):
            raise McpProtocolError(f"MCP server '{self.name}' sent an invalid tools/call result")
        return McpCallResult(text=_content_text(result), is_error=bool(result.get("isError")))

    # --- JSON-RPC -------------------------------------------------------------------------

    def request(self, method: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> Any:
        if self._disconnect_reason is not None or self._closed.is_set():
            raise McpConnectionError(self._disconnect_reason or f"MCP server '{self.name}' is not connected")
        request_id = next(self._ids)
        pending = _Pending()
        with self._lock:
            self._pending[request_id] = pending
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})

        limit = timeout if timeout is not None else self.call_timeout
        if not pending.done.wait(limit):
            with self._lock:
                self._pending.pop(request_id, None)
            with contextlib.suppress(McpError):
                self.notify("notifications/cancelled", {"requestId": request_id, "reason": "timeout"})
            raise McpTimeoutError(f"MCP server '{self.name}' did not answer {method} within {limit:g} seconds")
        if pending.error is not None:
            raise pending.error
        response = pending.response or {}
        if "error" in response:
            error = response["error"] if isinstance(response["error"], dict) else {}
            code = error.get("code", -32603)
            if not isinstance(code, int):
                raise McpProtocolError("MCP error code must be an integer")
            raise McpRemoteError(code, str(error.get("message", "unknown error")))
        return response.get("result")

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._send(message)

    def _send(self, message: dict[str, Any]) -> None:
        line = json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
        process = self._process
        if process is None or process.stdin is None:
            raise McpConnectionError(f"MCP server '{self.name}' is not running")
        try:
            with self._write_lock:
                process.stdin.write(line.encode("utf-8"))
                process.stdin.flush()
        except (OSError, ValueError) as error:
            self._disconnect(f"MCP server '{self.name}' stopped accepting input ({error})")
            raise McpConnectionError(self._disconnect_reason or str(error)) from error

    def _read_stdout(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        while raw := self._process.stdout.readline(MAX_MESSAGE_BYTES + 1):
            if len(raw) > MAX_MESSAGE_BYTES:
                self._disconnect("MCP message exceeds the size limit")
                return
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                self.stderr_tail.append(redact_secrets(f"[non-JSON on stdout] {line[:200]}"))
                continue
            if isinstance(message, dict):
                self._dispatch(message)
        code = self._process.poll()
        detail = f" (exit code {code})" if code is not None else ""
        self._disconnect(f"MCP server '{self.name}' disconnected{detail}")

    def _read_stderr(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        while raw := self._process.stderr.readline(8192):
            self.stderr_tail.append(redact_secrets(raw.decode("utf-8", errors="replace").rstrip()))

    def _dispatch(self, message: dict[str, Any]) -> None:
        if "method" in message:
            if "id" in message:  # a request from the server
                self._answer_server_request(message)
            return  # notifications from the server are not used yet
        request_id = message.get("id")
        with self._lock:
            pending = self._pending.pop(request_id, None) if isinstance(request_id, int) else None
        if pending is not None:
            pending.response = message
            pending.done.set()

    def _answer_server_request(self, message: dict[str, Any]) -> None:
        if message["method"] == "ping":
            reply: dict[str, Any] = {"jsonrpc": "2.0", "id": message["id"], "result": {}}
        else:
            # Forge offers no client capabilities (sampling, roots, elicitation).
            reply = {
                "jsonrpc": "2.0",
                "id": message["id"],
                "error": {"code": -32601, "message": f"Method not supported by Forge: {message['method']}"},
            }
        with contextlib.suppress(McpError):
            self._send(reply)

    def _disconnect(self, reason: str) -> None:
        if self._disconnect_reason is None:
            self._disconnect_reason = reason
        self._fail_all(reason)

    def _fail_all(self, reason: str) -> None:
        with self._lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for item in pending:
            item.error = McpConnectionError(reason)
            item.done.set()


def _content_text(result: dict[str, Any]) -> str:
    """The text of a tools/call result. Non-text content is described, not embedded."""
    parts = []
    for item in result.get("content") or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "text":
            parts.append(str(item.get("text", "")))
        elif kind == "resource" and isinstance(item.get("resource"), dict) and "text" in item["resource"]:
            parts.append(str(item["resource"]["text"]))
        else:
            parts.append(f"[{kind or 'unknown'} content omitted]")
    if not parts and result.get("structuredContent") is not None:
        parts.append(json.dumps(result["structuredContent"], ensure_ascii=False))
    return "\n".join(parts)
