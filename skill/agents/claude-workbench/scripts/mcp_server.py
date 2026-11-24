#!/usr/bin/env python3
"""MCP server (stdio, newline-delimited JSON-RPC) handing requests to Claude through workbench's
web chat API: the same API and config as ask_claude.py, for an agent that speaks MCP (#1138).

    python3 mcp_server.py        # started by the MCP client, which talks to it on stdin/stdout

Tools: ask_claude(text) sends one request and returns its id at once; get_reply(id) returns
Claude's replies to it and where it stands (done, still working, waiting on the user's yes,
queued, expired); list_requests() lists this agent's requests.

Environment, as ask_claude.py: CLAUDE_WB_URL (the chat's address, e.g. http://localhost:1337) and
CLAUDE_WB_TOKEN (this agent's token, from `wb chat token add <name>`). Local only, by design: the
URL must name this machine (localhost, 127.*, ::1), or every tool refuses and sends nothing; the
chat refuses an agent token from any other address anyway. Standard library only.
"""
from __future__ import annotations

import importlib.util
import ipaddress
import json
import os
import re
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlencode, urlsplit

# ask_claude.py beside it, by its path: under `python3 -I` the script's directory isn't on
# sys.path, and a name lookup there could load another module of that name
_spec = importlib.util.spec_from_file_location(
    "claude_workbench_ask", Path(__file__).resolve().parent / "ask_claude.py")
ask_claude = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ask_claude)

PROTOCOL = "2025-06-18"
VERSION = "1"  # the agent chat API's version it speaks (`api` in /api/health)
WAIT_DEFAULT, WAIT_MOST = 20, 300  # get_reply's hold: many MCP clients cut a tool call at 60 s
SEND_TRIES = 3  # a send cut by the chat restarting is sent again, under the same key
KEY = re.compile(r"[A-Za-z0-9._:-]{1,64}")  # a cid's room (1 to 64), in plain characters
LISTED = 20  # list_requests: the newest requests
CLIP = 120  # list_requests: characters of each request's text

TOOLS = [
    {"name": "ask_claude",
     "description": (
         "Hand ONE request to Claude, the user's lead (a Claude Code session in workbench mode "
         "on this machine), and get its request id at once. For large or cross-cutting changes, "
         "design and review, or work that needs several agents; do small edits yourself. Name "
         "the repository's path, the goal and what done means. Then call get_reply with the id: "
         "the work can take minutes or hours. Claude asks the user itself before anything that "
         "needs their yes (spending, pushing, deleting): you can't give it."),
     "inputSchema": {"type": "object", "properties": {
         "text": {"type": "string", "description": "the request, 1 to 8000 characters"},
         "key": {"type": "string", "description": (
             "optional, 1 to 64 of A-Z a-z 0-9 . _ : -: the same key on a retry of this request "
             "sends it once (each call without one is a new request)")}},
         "required": ["text"]}},
    {"name": "get_reply",
     "description": (
         "Claude's replies to a request (its id from ask_claude) and where it stands: done, "
         "still working, waiting on the user's yes (paused on the human, not stuck), queued (no "
         "Claude session listening yet) or expired. Holds up to `wait` seconds for news. A done "
         "request can get a later report when Claude handed the work to its own agents: its "
         "reply says so, and get_reply then shows it."),
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string", "description": "the request's id"},
         "wait": {"type": "number", "description": (
             f"seconds to hold for a reply or the turn's end, 0 to {WAIT_MOST} (default "
             f"{WAIT_DEFAULT}); more only if your client lets a tool call run that long")}},
         "required": ["id"]}},
    {"name": "list_requests",
     "description": "This agent's recent requests to Claude: id, how far each got, its replies.",
     "inputSchema": {"type": "object", "properties": {}}},
]


class Refusal(Exception):
    """A tool call that can't go ahead: its text is what the agent is told."""


def loopback(url: str) -> bool:
    """Whether ``url`` names this machine: localhost, 127.0.0.0/8 or ::1 (an IPv4-mapped one too),
    over http or https. Agents are the user's own, on this machine (#1138's design intent): a
    tailnet or LAN address would send the token off it."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").rstrip(".").lower()
        _ = parts.port  # a bad port raises ValueError
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not host:
        return False
    if host == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(host.split("%")[0])
    except ValueError:
        return False  # any other name: its address could be anywhere
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_loopback


def config() -> None:
    """Refusal unless both variables are set and the URL is this machine's."""
    url, token = os.environ.get("CLAUDE_WB_URL", ""), os.environ.get("CLAUDE_WB_TOKEN", "")
    if not url or not token:
        raise Refusal("set CLAUDE_WB_URL (the chat's address, e.g. http://localhost:1337) and "
                      "CLAUDE_WB_TOKEN (the user makes it: `wb chat token add <name>`) in this MCP "
                      "server's environment")
    if not loopback(url):
        raise Refusal(f"CLAUDE_WB_URL must be this machine's chat (localhost, 127.*, ::1), not "
                      f"{url[:200]}: agents work only from this machine. Nothing was sent")


def call(method: str, path: str, body=None, headers=None, timeout: float = 30):
    """ask_claude.call, after the config check; Refusal for a refused token."""
    config()
    try:
        return ask_claude.call(method, path, body, headers, timeout)
    except ask_claude.Refused as e:
        raise Refusal(f"the token was refused: {e}") from None


def _error(got: dict, code) -> str:
    return str(got.get("error") or f"HTTP {code}")


def ask(text: str, key: str = "") -> dict:
    """Send one request, at once; its id. Checks first that a session listens (nothing is sent
    otherwise, as ask_claude.py). ``key`` (else a new one) is both the send's `cid` and its
    Idempotency-Key: a send cut off is sent again under it, and lands once."""
    if not text.strip():
        raise Refusal("text is the request: 1 to 8000 characters")
    if key and not KEY.fullmatch(key):
        raise Refusal("key is 1 to 64 of A-Z a-z 0-9 . _ : -")
    key = key or str(uuid.uuid4())
    code, health = call("GET", "/api/health")
    if code != 200:
        raise Refusal(f"the chat answered {_error(health, code)}")
    if not health.get("listening"):
        raise Refusal("Claude isn't listening (no Claude Code session in workbench mode with the "
                      "web chat on): nothing was sent. Do it yourself, or try later")
    why = ""
    for n in range(SEND_TRIES):
        try:
            code, got = call("POST", "/api/send", {"text": text, "cid": key},
                             {"Idempotency-Key": key})
        except OSError as e:  # reset or refused: the chat restarting (it re-execs on an update)
            code, got = None, {"error": str(e)}
        if code == 200:
            msg = got.get("message") or {}
            return {"id": msg.get("id"), "key": key, "listening": got.get("listener") is not None}
        if code == 429:
            raise Refusal(f"rate-limited: try again in {got.get('retry_after') or 5} s with key "
                          f"{key}, so it is sent once")
        why = _error(got, code)
        if code is not None and code not in ask_claude.TRANSIENT:
            raise Refusal(f"not sent: {why}")
        if n + 1 < SEND_TRIES:
            time.sleep(1 + n)
    raise Refusal(f"the chat didn't answer ({why}): try again with key {key}, so it is sent once")


def _wait(after: str, hold: float) -> dict:
    q = urlencode({"after": after, "timeout": int(hold), "until": "done"})
    try:
        code, got = call("GET", f"/api/wait?{q}", timeout=hold + 30)
    except OSError as e:
        raise Refusal(f"the chat didn't answer ({e}): call get_reply again in a moment") from None
    if code == 404:
        raise Refusal(f"no request {after[:64]} in your conversation (an id from ask_claude, "
                      "within the thread's recent part)")
    if code != 200:
        raise Refusal(f"the chat answered {_error(got, code)}: call get_reply again in a moment")
    return got


def _expired(rid: str) -> bool:
    code, got = call("GET", "/api/state")
    return code == 200 and any(m.get("id") == rid and m.get("expired")
                               for m in got.get("messages") or [])


def reply(rid: str, wait: float = WAIT_DEFAULT) -> dict:
    """Where request ``rid`` stands: {id, status, replies: [{id, text}]}. The status is "done",
    "waiting on the user", "queued", "expired" or "still working"."""
    if not rid or len(rid) > 64:
        raise Refusal("id is a request's id, from ask_claude")
    rows: list = []

    def where(got: dict) -> str:
        # the wait answers for the agent's whole conversation: its replies can be another
        # request's, and `done` is for the newest of them. A reply is for the requests its `re`
        # names; one to a later request alone means the turn that took this one has ended
        mine = [r for r in rows if rid in (r.get("re") or [])]
        newest_mine = not rows or rid in (rows[-1].get("re") or [])
        if (got.get("done") if newest_mine else mine):
            return "done"
        if got.get("waiting_on_user"):
            return "waiting on the user"
        return "queued" if got.get("listener") is None else "still working"

    got = _wait(rid, 0)
    rows += got.get("replies") or []
    # held to the turn's end, not to the first reply: "On it" comes first, and a get_reply that
    # returned on it would leave the agent polling again at once
    deadline = time.monotonic() + wait
    while (status := where(got)) == "still working" and (left := deadline - time.monotonic()) >= 1:
        got = _wait(rows[-1]["id"] if rows else rid, left)
        rows += got.get("replies") or []
    if status == "queued" and _expired(rid):
        status = "expired"
    mine = [r for r in rows if rid in (r.get("re") or [])]
    return {"id": rid, "status": status,
            "replies": [{"id": r.get("id"), "text": r.get("text", "")} for r in mine]}


NEXT = {
    "done": "Claude's turn on it is done. If its reply says it handed the work on, a later report "
            "comes: call get_reply again later.",
    "waiting on the user": "Claude is paused on the user's yes (a permission, a plan or a "
                           "question): only the user can answer it. Not stuck: call get_reply "
                           "again later.",
    "queued": "No Claude session is listening: it is read once one listens, or never past 6 "
              "hours. Call get_reply again later, or do the work yourself.",
    "expired": "It waited unread past 6 hours and will never be passed on: ask again if it "
               "still matters.",
    "still working": "Claude is still at work on it: call get_reply again.",
}


def requests() -> dict:
    """This agent's recent requests, from its view of the thread: {listening, requests}."""
    code, got = call("GET", "/api/state")
    if code != 200:
        raise Refusal(f"the chat answered {_error(got, code)}")
    rows = got.get("messages") or []
    out = []
    for m in [m for m in rows if m.get("role") == "agent"][-LISTED:]:
        n = sum(1 for r in rows if r.get("from") == "claude" and m.get("id") in (r.get("re") or []))
        how = ("expired" if m.get("expired") else f"{n} replies" if n
               else "received" if m.get("received") else "queued")
        text = " ".join(str(m.get("text", "")).split())
        out.append({"id": m.get("id"), "ts": m.get("ts"), "status": how, "replies": n,
                    "text": text[:CLIP] + ("…" if len(text) > CLIP else "")})
    return {"listening": got.get("listener") is not None, "requests": out}


def run_tool(name: str, args: dict) -> tuple[str, dict]:
    """(text, structured result) of one tool call; Refusal for one that can't go ahead."""
    if name == "ask_claude":
        got = ask(str(args.get("text", "")), str(args.get("key") or ""))
        where = "Claude is listening" if got["listening"] else "it waits in the queue"
        return (f"request {got['id']}: sent ({where}). Call get_reply with id {got['id']} for "
                "Claude's answer: it can take minutes.", got)
    if name == "get_reply":
        try:
            wait = float(args.get("wait", WAIT_DEFAULT))
        except (TypeError, ValueError):
            wait = WAIT_DEFAULT
        got = reply(str(args.get("id", "")).strip(), max(0.0, min(wait, WAIT_MOST)))
        parts = [f"request {got['id']}: {got['status']}"]
        parts += [f"[reply {r['id']}]\n{r['text']}" for r in got["replies"]]
        parts.append(NEXT[got["status"]])
        return "\n\n".join(parts), got
    if name == "list_requests":
        got = requests()
        lines = [f"Claude is {'listening' if got['listening'] else 'not listening'}."]
        lines += [f"{r['id']}  {r['status']}  {r['text']}" for r in got["requests"]] or ["No requests yet."]
        return "\n".join(lines), got
    raise KeyError(name)


def handle(msg: dict):
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:  # a notification (notifications/initialized and the like): no reply
        return None
    if method == "initialize":
        version = (msg.get("params") or {}).get("protocolVersion") or PROTOCOL
        result = {"protocolVersion": version, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "claude-workbench", "version": VERSION}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        params = msg.get("params") or {}
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            args = {}
        name = params.get("name")
        if name not in {t["name"] for t in TOOLS}:
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32602, "message": f"unknown tool {name}"}}
        try:
            text, data = run_tool(name, args)
        except Refusal as e:
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "isError": True, "content": [{"type": "text", "text": f"{name}: {e}"}]}}
        except (OSError, ValueError, KeyError, TypeError) as e:  # the agent hears it and goes on
            why = " ".join(str(e).split())[:200] or type(e).__name__
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "isError": True, "content": [{"type": "text", "text": f"{name} failed: {why}"}]}}
        result = {"content": [{"type": "text", "text": text}], "structuredContent": data,
                  "isError": False}
    else:
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": -32601, "message": f"method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main() -> int:
    out = threading.Lock()
    try:
        config()
    except Refusal as e:  # said in the client's log; each tool call says it again
        print(f"claude-workbench: {e}", file=sys.stderr, flush=True)

    def serve(line: str) -> None:
        mid = None  # null only for a line that isn't a request at all, as JSON-RPC says
        try:
            msg = json.loads(line)
            mid = msg.get("id") if isinstance(msg, dict) else None
            reply_ = handle(msg) if isinstance(msg, dict) else {
                "jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "not a request"}}
        except Exception as exc:  # never let one bad message take the tools away
            # with the request's own id: a client drops a reply with id null (ask_mcp.py, #525)
            reply_ = {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(exc)}}
        if reply_ is not None:
            with out:
                sys.stdout.write(json.dumps(reply_) + "\n")
                sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        if '"tools/call"' in line:  # get_reply holds up to WAIT_MOST: pings go on meanwhile
            threading.Thread(target=serve, args=(line,), daemon=True).start()
        else:
            serve(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
