"""MCP server (stdio, newline-delimited JSON-RPC) giving the local agent one tool: ask_reviewer.

OpenCode starts it for each round (each `opencode run`) through the task's config layer, with WB_TASK naming the task. A call
writes the question into the task directory, raises a "question" notification (the Claude Code mod
wakes Claude with it; `wb wait` returns on it), and blocks until Claude's answer file appears or
the wait times out. Questions are budgeted per task so a chatty model can't lean on the reviewer.
A question is served on a thread of its own, so the server answers pings while it waits.
"""
import json
import os
import sys
import threading
import time
import uuid

from . import store
from .watch import Watcher

PROTOCOL = "2025-06-18"
TOOL = {
    "name": "ask_reviewer",
    "description": (
        "Ask the senior reviewer (Claude) ONE blocking question when the spec is genuinely "
        "ambiguous or contradictory and a wrong guess would waste the whole round. Do not ask "
        "for permission, for reassurance, or about things you can find out by reading the code "
        "or running it. The question budget is small. Waits for the answer, which can take "
        "minutes. Also how to run a shell command that was refused (\"Permission denied: "
        "shell\"): put it in `command` and say why in `question`; or a directory outside your "
        "lane (\"Permission denied\" writing it, or \"Permission denied: external_directory\"): "
        "put it in `path`. Claude allows it or suggests another way, and these don't use the "
        "question budget."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "The question, self-contained."},
            "context": {"type": "string",
                        "description": "What you found and the options you are choosing between."},
            "command": {"type": "string",
                        "description": "Only for a refused shell command: the exact command."},
            "path": {"type": "string",
                     "description": "Only for a refused directory: its absolute path."},
        },
        "required": ["question"],
    },
}


PERMISSION_BUDGET = 8  # requests for a refused command or directory, per task: a cap on a stuck loop


def _permission(q: dict) -> bool:
    return bool(q.get("command") or q.get("path"))


def ask(task: store.Task, question: str, context: str = "", command: str = "",
        path: str = "") -> str:
    spec = task.spec
    asked = [store.read_json(p, {}) for p in (task.dir / "ask").glob("*.json")] \
        if (task.dir / "ask").exists() else []
    if command or path:
        if sum(1 for q in asked if _permission(q)) >= PERMISSION_BUDGET:
            return (f"You have asked for {PERMISSION_BUDGET} refused commands or directories "
                    "already. Work inside your lane with the allowed tools, and list what you "
                    "couldn't do in your final summary.")
    elif sum(1 for q in asked if not _permission(q)) >= int(spec.get("ask_budget", 2)):
        return ("The question budget for this task is used up. Make the most reasonable "
                "assumption, carry on, and state the assumption in your final summary.")
    qid = uuid.uuid4().hex[:8]
    qfile, afile = task.dir / "ask" / f"{qid}.json", task.dir / "ask" / f"{qid}.answer"
    store.write_json(qfile, {"id": qid, "ts": store.now(), "round": task.status.get("round"),
                             "question": question, "context": context,
                             **({"command": command} if command else {}),
                             **({"path": path} if path else {})})
    prev = task.status.get("phase")
    task.set_status(phase="asking", asking=qid)
    task.event({"ts": store.now(), "round": task.status.get("round"), "kind": "ask",
                "id": qid, "text": question})
    if command:
        body = (f"PERMISSION to run: `{command}`\nWhy: {question}\nAllow it with `wb allow "
                f"{task.name} \"<pattern>\"` (--always to keep it), or answer with another way.")
    elif path:
        body = (f"PERMISSION to use the directory {path} (outside its lane)\nWhy: {question}\n"
                f"Allow it with `wb allow {task.name} --path {path}` (--always to keep it), or "
                "answer with another way.")
    else:
        body = f"Q: {question}"
    body += f"\nContext: {context}" if context else ""
    task.notify("question", body, id=qid)
    deadline = time.time() + float(spec.get("ask_timeout", 1200))
    watcher = Watcher(afile.parent)  # wakes the moment `wb answer` writes the file
    while time.time() < deadline:
        if afile.exists():
            answer = afile.read_text().strip()
            task.set_status(phase=prev if prev != "asking" else "running", asking=None)
            task.event({"ts": store.now(), "round": task.status.get("round"), "kind": "answer",
                        "id": qid, "text": answer})
            watcher.close()
            return answer
        watcher.wait(max(0.0, min(60.0, deadline - time.time())))
    watcher.close()
    task.set_status(phase=prev if prev != "asking" else "running", asking=None)
    return ("No answer arrived in time. Make the most reasonable assumption, carry on, and state "
            "the assumption in your final summary.")


def handle(msg: dict, task: store.Task):
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:  # a notification (notifications/initialized and the like): no reply
        return None
    if method == "initialize":
        version = (msg.get("params") or {}).get("protocolVersion") or PROTOCOL
        result = {"protocolVersion": version, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "workbench", "version": "0.1.0"}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [TOOL]}
    elif method == "tools/call":
        params = msg.get("params") or {}
        if params.get("name") != TOOL["name"]:
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32602, "message": f"unknown tool {params.get('name')}"}}
        args = params.get("arguments") or {}
        text = ask(task, str(args.get("question", "")).strip(), str(args.get("context", "")),
                   str(args.get("command", "")).strip(), str(args.get("path", "")).strip())
        result = {"content": [{"type": "text", "text": text}], "isError": False}
    else:
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": -32601, "message": f"method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main():
    task = store.Task(os.environ["WB_TASK"])
    out = threading.Lock()

    def serve(line: str) -> None:
        try:
            reply = handle(json.loads(line), task)
        except Exception as exc:  # never let one bad message take the agent's tool away
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": str(exc)}}
        if reply is not None:
            with out:
                sys.stdout.write(json.dumps(reply) + "\n")
                sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        if '"tools/call"' in line:  # a question blocks until Claude answers: pings go on meanwhile
            threading.Thread(target=serve, args=(line,), daemon=True).start()
        else:
            serve(line)


if __name__ == "__main__":
    main()
