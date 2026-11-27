#!/usr/bin/env python3
"""MCP server (stdio, newline-delimited JSON-RPC) handing requests to Workbench's driver (the
lead: Claude Code or OpenCode, whichever listens on the web chat) through the chat's API: the same
API and config as ask_workbench.py, for an agent that speaks MCP (#1138, #1366).

    python3 mcp_server.py        # started by the MCP client, which talks to it on stdin/stdout

Tools: ask_workbench(text) sends one request and returns its id at once; get_reply(id) returns
the driver's replies to it, which driver answered, and where it stands (done, still working,
waiting on the user's yes, queued, expired); list_requests() lists this agent's requests.

Environment, as ask_workbench.py: WORKBENCH_URL (the chat's address, e.g. http://localhost:8090)
and WORKBENCH_TOKEN (this agent's token, from `wb chat token add <name>`); CLAUDE_WB_URL and
CLAUDE_WB_TOKEN, their names before 2.2, still work. Local only, by design: the
URL must name this machine (localhost, 127.*, ::1), or every tool refuses
and sends nothing; the chat refuses an agent token from any other machine anyway. It calls the
chat's /api/v1/ paths (docs/chat-api.yaml), so it needs workbench 2.1 or later. Standard library
only.
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

# ask_workbench.py beside it, by its path: under `python3 -I` the script's directory isn't on
# sys.path, and a name lookup there could load another module of that name
_spec = importlib.util.spec_from_file_location(
    "ask_workbench_client", Path(__file__).resolve().parent / "ask_workbench.py")
client = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(client)

PROTOCOL = "2025-06-18"
VERSION = "1"  # the agent chat API's version it speaks (`api` in /api/v1/health)
# the versioned paths (docs/chat-api.yaml), new with this server in workbench 2.1; ask_workbench.py
# keeps /api/ so it works with an older chat, and a chat without /api/v1/ is said so here (#1138)
API = "/api/v1"
# a chat before 2.0 answers it 404; 2.0's agent allow-list, 403 "agent tokens can't use /api/v1/…"
NO_V1 = "this chat has no /api/v1/ (workbench before 2.1): the user updates it with `wb update`. Nothing was sent"
WAIT_DEFAULT, WAIT_MOST = 20, 300  # get_reply's hold: many MCP clients cut a tool call at 60 s
SEND_TRIES = 3  # a send cut by the chat restarting is sent again, under the same key
KEY = re.compile(r"[A-Za-z0-9._:-]{1,64}")  # a cid's room (1 to 64), in plain characters
LISTED = 20  # list_requests: the newest requests
CLIP = 120  # list_requests: characters of each request's text

TOOLS = [
    {"name": "ask_workbench",
     "description": (
         "Hand ONE request to the user's lead, Workbench's driver (a Claude Code or OpenCode "
         "session in workbench mode on this machine), and get its request id at once. For large "
         "or cross-cutting changes, design and review, or work that needs several agents; do "
         "small edits yourself. Name the repository's path, the goal and what done means. Then "
         "call get_reply with the id: the work can take minutes or hours. The driver asks the "
         "user itself before anything that needs their yes (spending, pushing, deleting): you "
         "can't give it."),
     "inputSchema": {"type": "object", "properties": {
         "text": {"type": "string", "description": "the request, 1 to 8000 characters"},
         "key": {"type": "string", "description": (
             "optional, 1 to 64 of A-Z a-z 0-9 . _ : -: the same key on a retry of this request "
             "sends it once (each call without one is a new request)")}},
         "required": ["text"]}},
    {"name": "get_reply",
     "description": (
         "The driver's replies to a request (its id from ask_workbench), which driver answered, "
         "and where it stands: done, still working, waiting on the user's yes (paused on the "
         "human, not stuck), queued (no driver listening yet) or expired. Holds up to `wait` "
         "seconds for news. A done request can get a later report when the driver handed the "
         "work to its own agents: its reply says so, and get_reply then shows it."),
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string", "description": "the request's id"},
         "wait": {"type": "number", "description": (
             f"seconds to hold for a reply or the turn's end, 0 to {WAIT_MOST} (default "
             f"{WAIT_DEFAULT}); more only if your client lets a tool call run that long")}},
         "required": ["id"]}},
    {"name": "list_requests",
     "description": "This agent's recent requests to the driver: id, how far each got, its replies.",
     "inputSchema": {"type": "object", "properties": {}}},
]
# the tool's name before 2.2 (#1366): a client that kept it still reaches it, though only the new
# name is listed
ALIASES = {"ask_claude": "ask_workbench"}


class Refusal(Exception):
    """A tool call that can't go ahead: its text is what the agent is told."""


def this_machine(url: str) -> bool:
    """Whether ``url`` names this machine, over http or https: localhost, 127.0.0.0/8 or ::1 (an
    IPv4-mapped one too), where the chat listens (#1616, 2026-11-27: localhost only). Agents are
    the user's own, on this machine (#1138's design intent): another address would send the token
    off it."""
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
    url, token = client.env(client.URL_VARS), client.env(client.TOKEN_VARS)
    if not url or not token:
        raise Refusal("set WORKBENCH_URL (the chat's address, e.g. http://localhost:8090) and "
                      "WORKBENCH_TOKEN (the user makes it: `wb chat token add <name>`) in this MCP "
                      "server's environment")
    if not this_machine(url):
        raise Refusal(f"WORKBENCH_URL must be this machine's chat (localhost, 127.*, ::1), "
                      f"not {url[:200]}: agents work only from this machine. "
                      "Nothing was sent")


def call(method: str, path: str, body=None, headers=None, timeout: float = 30, answered=None):
    """client.call on ``API`` + ``path``, after the config check; Refusal for a refused token."""
    config()
    try:
        return client.call(method, API + path, body, headers, timeout, answered)
    except client.Refused as e:
        if str(e).startswith(f"agent tokens can't use {API}/"):  # a 2.0 chat: /api/v1/ is off its allow-list
            raise Refusal(NO_V1) from None
        raise Refusal(f"the token was refused: {e}") from None


def _error(got: dict, code) -> str:
    return str(got.get("error") or f"HTTP {code}")


def ask(text: str, key: str = "") -> dict:
    """Send one request, at once; its id. Checks first that a session listens (nothing is sent
    otherwise, as ask_workbench.py). ``key`` (else a new one) is both the send's `cid` and its
    Idempotency-Key: a send cut off is sent again under it, and lands once."""
    if not text.strip():
        raise Refusal("text is the request: 1 to 8000 characters")
    if key and not KEY.fullmatch(key):
        raise Refusal("key is 1 to 64 of A-Z a-z 0-9 . _ : -")
    key = key or str(uuid.uuid4())
    code, health = call("GET", "/health")
    if code == 404:  # a chat before 2.0
        raise Refusal(NO_V1)
    if code != 200:
        raise Refusal(f"the chat answered {_error(health, code)}")
    if not health.get("listening"):
        raise Refusal("no driver is listening (no Claude Code or OpenCode session in workbench "
                      "mode with the web chat on): nothing was sent. Do it yourself, or try later")
    why = ""
    for n in range(SEND_TRIES):
        answered: dict = {}
        try:
            code, got = call("POST", "/send", {"text": text, "cid": key},
                             {"Idempotency-Key": key}, answered=answered)
        except OSError as e:  # reset or refused: the chat restarting (it re-execs on an update)
            code, got = None, {"error": str(e)}
        if code == 200:
            # the chat's answer to a key it has: the first send's message, nothing sent again,
            # whether that send was a try of this loop's cut on its way back or a call before
            msg = got.get("message") or {}
            lst = got.get("listener")
            return {"id": msg.get("id"), "key": key, "listening": lst is not None,
                    "driver": client.who((lst or {}).get("driver") or health.get("driver")),
                    "replayed": answered.get("idempotent-replayed") == "true"}
        if got.get("code") == "idempotency_key_reused":
            raise Refusal(f"key {key} went with another request in the last 24 hours: nothing was "
                          "sent. Ask without a key (or a new one) for a new request")
        if code == 429:
            raise Refusal(f"rate-limited: try again in {got.get('retry_after') or 5} s with key "
                          f"{key}, so it is sent once")
        why = _error(got, code)
        if code is not None and code not in client.TRANSIENT:
            raise Refusal(f"not sent: {why}")
        if n + 1 < SEND_TRIES:
            time.sleep(1 + n)
    raise Refusal(f"the chat didn't answer ({why}): try again with key {key}, so it is sent once")


def _wait(after: str, hold: float, until: str = "done") -> dict:
    # tenths, not whole seconds: a wait back empty ends the hold, and one cut to int(1.99) ended
    # a 2-second get_reply at 1
    q = urlencode({"after": after, "timeout": round(hold, 1), "until": until})
    try:
        code, got = call("GET", f"/wait?{q}", timeout=hold + 30)
    except OSError as e:
        raise Refusal(f"the chat didn't answer ({e}): call get_reply again in a moment") from None
    if got.get("code") == "expired":  # unread past 6 hours, no reply: the chat says so at once
        return {"replies": [], "done": False, "expired": True}
    if code == 404:
        raise Refusal(f"no request {after[:64]} in your conversation (an id from ask_workbench, "
                      "within the thread's recent part)")
    if code != 200:
        raise Refusal(f"the chat answered {_error(got, code)}: call get_reply again in a moment")
    return got


def _messages() -> dict:
    """This agent's view of the thread from /api/state, by id ({} when the chat won't say)."""
    code, got = call("GET", "/state")
    return {m["id"]: m for m in (got.get("messages") or []) if isinstance(m, dict) and m.get("id")} \
        if code == 200 else {}


def reply(rid: str, wait: float = WAIT_DEFAULT) -> dict:
    """Where request ``rid`` stands: {id, status, driver, replies: [{id, text, driver}]}. The status
    is "done", "waiting on the user", "queued", "expired" or "still working"; `driver` names who
    answered (the newest reply's writer, else the driver listening now; "" when the chat doesn't
    say, as one before 2.2)."""
    if not rid or len(rid) > 64:
        raise Refusal("id is a request's id, from ask_workbench")
    rows: list = []
    sent: dict = {}  # the agent's requests' send times, read when a reply names another request

    def later(ids: set) -> bool:
        """Whether any of ``ids`` is a request of this agent's sent after ``rid``."""
        if ids - sent.keys():
            sent.update({i: m.get("ts", 0) for i, m in _messages().items() if m.get("role") == "agent"})
        # a request past the state's newest 200 is older than every request it lists
        at = sent.get(rid, float("-inf"))
        return any(sent[i] > at for i in ids if i in sent)

    def where(got: dict) -> str:
        if got.get("expired"):
            return "expired"
        # the wait answers for the agent's whole conversation: its replies can be other requests',
        # and its `done` is for the newest of them (else for this request: a turn that took it
        # has ended). A reply is for the requests its `re` names
        n = max((k for k, r in enumerate(rows) if rid in (r.get("re") or [])), default=-1)
        if n == len(rows) - 1:  # no replies, or the newest is this request's
            done = got.get("done")
        else:
            # a turn ended after a later reply ended after this request's newest one too; and a
            # reply to a request sent after this one, not naming it, came from a later turn (one
            # runs at a time), so the turn that took this one has ended, whether or not it replied
            # here (it can answer the user alone). One to an earlier request says nothing of it
            done = (n >= 0 and got.get("done")) or later(
                {i for r in rows[n + 1:] for i in r.get("re") or [] if i != rid})
        if done:
            return "done"
        if got.get("waiting_on_user"):
            return "waiting on the user"
        return "queued" if got.get("listener") is None else "still working"

    def expired(status: str) -> bool:
        # the chat retires a request unread past 6 hours whether or not a session listens: one
        # with a listener up said "still working" for good (the #1138 review)
        return status in ("still working", "queued") \
            and not any(rid in (r.get("re") or []) for r in rows) \
            and bool(_messages().get(rid, {}).get("expired"))

    got = _wait(rid, 0)
    listening = (got.get("listener") or {}).get("driver")
    rows += got.get("replies") or []
    status = where(got)
    if expired(status):  # never taken: no hold for a turn that won't come
        status = "expired"
    # held to the turn's end, not to the first reply: "On it" comes first, and a get_reply that
    # returned on it would leave the agent polling again at once
    deadline = time.monotonic() + wait
    while status == "still working" and (left := deadline - time.monotonic()) >= 1:
        # the wait's `done` is for the newest row it is after. With no reply of this request's
        # among them, that is another request's reply, whose turn may have ended already: the
        # wait answered at once, over and over (3,105 waits in 2 s, the #1138 review), so then it
        # holds for a new reply instead. With one, a turn ended after the newest ends this too
        ours = not rows or any(rid in (r.get("re") or []) for r in rows)
        got = _wait(rows[-1]["id"] if rows else rid, left, "done" if ours else "reply")
        new = got.get("replies") or []
        rows += new
        listening = (got.get("listener") or {}).get("driver") or listening
        status = where(got)
        if not new:  # the chat holds a wait until there is news: back with none, it was the last
            break
    if expired(status):
        status = "expired"
    mine = [r for r in rows if rid in (r.get("re") or [])]
    by = next((w for r in reversed(mine) if (w := client.who(r.get("driver")))), "") \
        or (client.who(listening) if mine else "")
    return {"id": rid, "status": status, "driver": by,
            "replies": [{"id": r.get("id"), "text": r.get("text", ""), "driver": client.who(r.get("driver"))}
                        for r in mine]}


NEXT = {
    "done": "The driver's turn on it is done. If its reply says it handed the work on, a later "
            "report comes: call get_reply again later.",
    "waiting on the user": "The driver is paused on the user's yes (a permission, a plan or a "
                           "question): only the user can answer it. Not stuck: call get_reply "
                           "again later.",
    "queued": "No driver is listening: it is read once one listens, or never past 6 hours. Call "
              "get_reply again later, or do the work yourself.",
    "expired": "It waited unread past 6 hours and will never be passed on: ask again if it "
               "still matters.",
    "still working": "The driver is still at work on it: call get_reply again.",
}


def requests() -> dict:
    """This agent's recent requests, from its view of the thread: {listening, driver, requests}."""
    code, got = call("GET", "/state")
    if code != 200:
        raise Refusal(f"the chat answered {_error(got, code)}")
    rows = got.get("messages") or []
    out = []
    for m in [m for m in rows if m.get("role") == "agent"][-LISTED:]:
        # a driver's reply is `from: "claude"` whichever driver wrote it (the API's name, kept)
        n = sum(1 for r in rows if r.get("from") == "claude" and m.get("id") in (r.get("re") or []))
        how = ("expired" if m.get("expired") else f"{n} replies" if n
               else "received" if m.get("received") else "queued")
        text = " ".join(str(m.get("text", "")).split())
        out.append({"id": m.get("id"), "ts": m.get("ts"), "status": how, "replies": n,
                    "text": text[:CLIP] + ("…" if len(text) > CLIP else "")})
    lst = got.get("listener")
    return {"listening": lst is not None, "driver": client.who((lst or {}).get("driver")), "requests": out}


def run_tool(name: str, args: dict) -> tuple[str, dict]:
    """(text, structured result) of one tool call; Refusal for one that can't go ahead."""
    if name == "ask_workbench":
        got = ask(str(args.get("text", "")), str(args.get("key") or ""))
        where = f"{got['driver'] or 'the driver'} is listening" if got["listening"] else "it waits in the queue"
        if got["replayed"]:
            where = f"once: key {got['key']} had sent it already, and it was not sent again"
        return (f"request {got['id']}: sent ({where}). Call get_reply with id {got['id']} for "
                "the driver's answer: it can take minutes.", got)
    if name == "get_reply":
        try:
            wait = float(args.get("wait", WAIT_DEFAULT))
        except (TypeError, ValueError):
            wait = WAIT_DEFAULT
        got = reply(str(args.get("id", "")).strip(), max(0.0, min(wait, WAIT_MOST)))
        parts = [f"request {got['id']}: {got['status']}" + (f", answered by {got['driver']}" if got["driver"] else "")]
        parts += [f"[reply {r['id']}]\n{r['text']}" for r in got["replies"]]
        parts.append(NEXT[got["status"]])
        return "\n\n".join(parts), got
    if name == "list_requests":
        got = requests()
        lines = [f"{got['driver'] or 'The driver'} is listening." if got["listening"] else "No driver is listening."]
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
                  "serverInfo": {"name": "ask-workbench", "version": VERSION}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        params = msg.get("params") or {}
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            args = {}
        name = ALIASES.get(params.get("name"), params.get("name"))
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
        print(f"ask-workbench: {e}", file=sys.stderr, flush=True)

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
