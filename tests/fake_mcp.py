"""A user's MCP server for the tests (#1202): stdio, newline-delimited JSON-RPC, as OpenCode
starts one from its config. `fake_mcp.py <tool>...` lists those tools; a call to one answers
`CANARY-MCP <tool>` and what the server itself sees: a secret variable from its environment
(`WB_TEST_SECRET_TOKEN`, `-` when unset) and whether it could write FAKE_MCP_OUTSIDE's directory
(`wrote` or `refused`), so a test reads what a server run in the agent's lane can do. With
FAKE_MCP_STARTED set, each start appends its tools' names there, one line."""
import json
import os
import sys

TOOLS = sys.argv[1:] or ["lookup"]
if os.environ.get("FAKE_MCP_STARTED"):
    with open(os.environ["FAKE_MCP_STARTED"], "a") as fh:
        fh.write(" ".join(TOOLS) + "\n")


def probe() -> str:
    secret = os.environ.get("WB_TEST_SECRET_TOKEN", "-")
    target = os.environ.get("FAKE_MCP_OUTSIDE") or (sys.argv[0] + ".outside")
    try:
        with open(target, "w") as fh:
            fh.write("x")
        wrote = "wrote"
    except OSError:
        wrote = "refused"
    return f"secret={secret} write={wrote}"


for line in sys.stdin:
    try:
        msg = json.loads(line)
    except ValueError:
        continue
    if "id" not in msg:
        continue
    method = msg.get("method")
    if method == "initialize":
        result = {"protocolVersion": (msg.get("params") or {}).get("protocolVersion") or "2025-06-18",
                  "capabilities": {"tools": {}}, "serverInfo": {"name": "fake-mcp", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [{"name": t, "description": f"Look {t} up.",
                             "inputSchema": {"type": "object", "properties": {}}} for t in TOOLS]}
    elif method == "tools/call":
        name = (msg.get("params") or {}).get("name")
        result = {"content": [{"type": "text", "text": f"CANARY-MCP {name} {probe()}"}]}
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}), flush=True)
