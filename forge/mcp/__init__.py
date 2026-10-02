"""MCP (Model Context Protocol) integration: external tools in Forge's own tool system."""

from forge.mcp.adapter import McpTool, adapt_tools
from forge.mcp.client import (
    PROTOCOL_VERSION,
    McpClient,
    McpConnectionError,
    McpError,
    McpProtocolError,
    McpTimeoutError,
)
from forge.mcp.manager import McpManager, ServerStatus

__all__ = [
    "PROTOCOL_VERSION",
    "McpClient",
    "McpConnectionError",
    "McpError",
    "McpManager",
    "McpProtocolError",
    "McpTimeoutError",
    "McpTool",
    "ServerStatus",
    "adapt_tools",
]
