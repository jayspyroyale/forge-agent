"""Starting the configured MCP servers for a task, and stopping them afterwards.

A server that fails to start, times out, or sends malformed tool
descriptions is reported in its `ServerStatus` and left out; the task goes
on with the other tools. Nothing here raises for a single server's failure.
"""

import os
import shlex
from pathlib import Path

from pydantic import BaseModel, Field

from forge.config import McpServerSettings, McpSettings
from forge.config.loader import ENV_REFERENCE
from forge.mcp.adapter import McpTool, SkippedTool, adapt_tools
from forge.mcp.client import McpClient, McpError
from forge.terminal import scrub_environment


class ServerStatus(BaseModel):
    name: str
    connected: bool
    error: str | None = None
    protocol_version: str | None = None
    server_name: str | None = None
    tools: list[str] = Field(default_factory=list)
    skipped: list[SkippedTool] = Field(default_factory=list)
    stderr: list[str] = Field(default_factory=list)  # last lines the server logged (shown on failure)


class McpManager:
    def __init__(self, settings: McpSettings, workspace_root: Path, environ: dict[str, str] | None = None) -> None:
        self.settings = settings
        self.workspace_root = workspace_root
        self.environ = dict(os.environ) if environ is None else environ
        self.clients: dict[str, McpClient] = {}
        self._tools: list[McpTool] = []
        self.statuses: list[ServerStatus] = []

    def connect_all(self, only: str | None = None) -> list[ServerStatus]:
        for name, server in self.settings.servers.items():
            if only is not None and name != only:
                continue
            if not server.enabled and only is None:
                continue
            self.statuses.append(self._connect(name, server))
        return self.statuses

    def tools(self) -> list[McpTool]:
        return list(self._tools)

    def close(self) -> None:
        for client in self.clients.values():
            client.close()
        self.clients.clear()

    def __enter__(self) -> "McpManager":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _connect(self, name: str, server: McpServerSettings) -> ServerStatus:
        client: McpClient | None = None
        try:
            env = self._environment(server)
            cwd = (self.workspace_root / server.cwd).resolve() if server.cwd else self.workspace_root
            if not cwd.is_relative_to(self.workspace_root):
                raise McpError(f"cwd '{server.cwd}' is outside the workspace")
            client = McpClient(
                name,
                [server.command, *server.args],
                env=env,
                cwd=cwd,
                startup_timeout=server.startup_timeout,
                call_timeout=server.timeout,
            )
            client.connect()
        except McpError as error:
            stderr = list(client.stderr_tail)[-10:] if client is not None else []
            return ServerStatus(name=name, connected=False, error=str(error), stderr=stderr)

        self.clients[name] = client
        try:
            tools, skipped = adapt_tools(client, name, server, client.list_tools())
        except McpError as error:
            client.close()
            del self.clients[name]
            return ServerStatus(name=name, connected=False, error=f"could not list tools: {error}")
        self._tools += tools
        return ServerStatus(
            name=name,
            connected=True,
            protocol_version=client.protocol_version,
            server_name=str(client.server_info.get("name") or "") or None,
            tools=[tool.name for tool in tools],
            skipped=skipped,
        )

    def _environment(self, server: McpServerSettings) -> dict[str, str]:
        """Forge's scrubbed environment plus the server's own variables, with ${NAME} filled in."""
        env = scrub_environment(self.environ)
        for key, value in server.env.items():
            env[key] = ENV_REFERENCE.sub(lambda match: self._lookup(match.group(0)[2:-1]), value)
        return env

    def _lookup(self, name: str) -> str:
        if name not in self.environ:
            raise McpError(f"environment variable {name} is not set (referenced in the server's env)")
        return self.environ[name]


def describe_command(server: McpServerSettings) -> str:
    return shlex.join([server.command, *server.args])
