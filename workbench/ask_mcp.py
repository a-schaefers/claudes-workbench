"""MCP server (stdio, newline-delimited JSON-RPC) giving the local agent one tool: ask_reviewer.

OpenCode starts it per task through the task's config layer, with WB_TASK naming the task. A call
writes the question into the task directory, raises a "question" notification (the Claude Code mod
wakes Claude with it; `wb wait` returns on it), and blocks until Claude's answer file appears or
the wait times out. Questions are budgeted per task so a chatty model can't lean on the reviewer.
"""
import json
import os
import sys
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
        "shell\"): put it in `command` and say why in `question`; Claude allows it or suggests "
        "another way, and these don't use the question budget."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "The question, self-contained."},
            "context": {"type": "string",
                        "description": "What you found and the options you are choosing between."},
            "command": {"type": "string",
                        "description": "Only for a refused shell command: the exact command."},
        },
        "required": ["question"],
    },
}


PERMISSION_BUDGET = 8  # requests to run a refused command, per task: a cap on a stuck loop


def ask(task: store.Task, question: str, context: str = "", command: str = "") -> str:
    spec = task.spec
    asked = [store.read_json(p, {}) for p in (task.dir / "ask").glob("*.json")] \
        if (task.dir / "ask").exists() else []
    if command:
        if sum(1 for q in asked if q.get("command")) >= PERMISSION_BUDGET:
            return (f"You have asked to run {PERMISSION_BUDGET} refused commands already. Use the "
                    "allowed tools, and list what you couldn't run in your final summary.")
    elif sum(1 for q in asked if not q.get("command")) >= int(spec.get("ask_budget", 2)):
        return ("The question budget for this task is used up. Make the most reasonable "
                "assumption, carry on, and state the assumption in your final summary.")
    qid = uuid.uuid4().hex[:8]
    qfile, afile = task.dir / "ask" / f"{qid}.json", task.dir / "ask" / f"{qid}.answer"
    store.write_json(qfile, {"id": qid, "ts": store.now(), "round": task.status.get("round"),
                             "question": question, "context": context,
                             **({"command": command} if command else {})})
    prev = task.status.get("phase")
    task.set_status(phase="asking", asking=qid)
    task.event({"ts": store.now(), "round": task.status.get("round"), "kind": "ask",
                "id": qid, "text": question})
    body = ((f"PERMISSION to run: `{command}`\nWhy: {question}\nAllow it with `wb allow "
             f"{task.name} \"<pattern>\"` (--always to keep it), or answer with another way.")
            if command else f"Q: {question}") + (f"\nContext: {context}" if context else "")
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
                   str(args.get("command", "")).strip())
        result = {"content": [{"type": "text", "text": text}], "isError": False}
    else:
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": -32601, "message": f"method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main():
    task = store.Task(os.environ["WB_TASK"])
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            reply = handle(json.loads(line), task)
        except Exception as exc:  # never let one bad message take the agent's tool away
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": str(exc)}}
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
