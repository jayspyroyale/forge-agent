"""A tiny MCP server for tests (stdio transport, standard library only).

    python mock_mcp_server.py [mode]

modes:
    normal      tools: echo, add, fail, slow, crash, env (+ a server-initiated ping)
    malformed   also lists tools with broken descriptions
    bad-init    answers initialize with an error
    silent      never answers anything
    exit        exits immediately
"""

import json
import os
import sys
import time

MODE = sys.argv[1] if len(sys.argv) > 1 else "normal"

TOOLS = [
    {
        "name": "echo",
        "description": "Echo a message back.",
        "inputSchema": {"type": "object", "properties": {"message": {"type": "string"}}, "required": ["message"]},
    },
    {
        "name": "add",
        "description": "Add two numbers.",
        "inputSchema": {"type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}}, "required": ["a", "b"]},
        "annotations": {"readOnlyHint": True},
    },
    {"name": "fail", "description": "Always reports an error.", "inputSchema": {"type": "object"}},
    {"name": "slow", "description": "Takes a long time.", "inputSchema": {"type": "object"}},
    {"name": "crash", "description": "Kills the server.", "inputSchema": {"type": "object"}, "annotations": {"destructiveHint": True}},
    {"name": "env", "description": "Shows one environment variable.", "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}}},
]
MALFORMED = [
    {"description": "no name"},
    {"name": "bad schema", "inputSchema": {"type": "object"}},
    {"name": "not_object", "inputSchema": {"type": "string"}},
    {"name": "echo", "inputSchema": {"type": "object"}},
    "just a string",
]


def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def reply(request_id, result):
    send({"jsonrpc": "2.0", "id": request_id, "result": result})


def text(value, is_error=False):
    return {"content": [{"type": "text", "text": value}], "isError": is_error}


def handle(message):
    method, request_id, params = message.get("method"), message.get("id"), message.get("params") or {}
    if request_id is None:
        return  # a notification (initialized, cancelled) or a reply to our ping
    if method == "initialize":
        if MODE == "bad-init":
            send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32603, "message": "init exploded"}})
            return
        reply(request_id, {"protocolVersion": params.get("protocolVersion"), "capabilities": {"tools": {}}, "serverInfo": {"name": "mock", "version": "0.1"}})
        send({"jsonrpc": "2.0", "id": "server-ping-1", "method": "ping"})
        print("mock server initialized", file=sys.stderr, flush=True)
    elif method == "tools/list":
        tools = TOOLS + (MALFORMED if MODE == "malformed" else [])
        if not params.get("cursor"):
            reply(request_id, {"tools": tools[:2], "nextCursor": "page2"})
        else:
            reply(request_id, {"tools": tools[2:]})
    elif method == "tools/call":
        name, arguments = params.get("name"), params.get("arguments") or {}
        if name == "echo":
            reply(request_id, text(f"echo: {arguments.get('message')}"))
        elif name == "add":
            reply(request_id, text(str(arguments["a"] + arguments["b"])))
        elif name == "fail":
            reply(request_id, text("something went wrong", is_error=True))
        elif name == "slow":
            time.sleep(10)
            reply(request_id, text("finally"))
        elif name == "crash":
            os._exit(3)
        elif name == "env":
            reply(request_id, text(os.environ.get(arguments.get("name", ""), "<unset>")))
        else:
            send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": f"Unknown tool: {name}"}})
    else:
        send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"Unknown method: {method}"}})


def main():
    if MODE == "exit":
        sys.exit(1)
    for line in sys.stdin:
        line = line.strip()
        if not line or MODE == "silent":
            continue
        handle(json.loads(line))


if __name__ == "__main__":
    main()
