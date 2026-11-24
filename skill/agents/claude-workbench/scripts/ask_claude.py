#!/usr/bin/env python3
"""Hand a request to Claude through workbench's web chat API, and print Claude's replies.

    ask_claude.py "<request>" [--file F]... [--timeout S]
    ask_claude.py --after <message or reply id> [--timeout S]     # wait for a later report

Environment: CLAUDE_WB_URL (the chat's address, e.g. http://localhost:1337) and CLAUDE_WB_TOKEN
(this agent's token, from `wb chat token add <name>`). Standard library only.

Prints each of Claude's replies to stdout as it comes, and on stderr the ids to follow up with.
Exit status: 0 when Claude's turn is done, 1 on an error or the timeout (Claude still at work: wait
again with --after), 2 when Claude isn't listening, 3 when the token is refused. A wait the server
cuts (it restarts on an update) is asked again until the timeout.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import quote, urlencode

WAIT_EACH = 300  # seconds one GET /api/wait may hold (the server's default)
# A wait cut by the server going away for a moment (it re-execs itself on a code change, `wb
# update`, a rebind or a restart of its unit; a proxy in front answers 502 to 504 meanwhile) is
# asked again after a pause growing from RETRY_FIRST to RETRY_MOST seconds, until the deadline:
# it ended the client at once with a bare error and no `--after` id (#1107, 2026-11-23)
TRANSIENT = (502, 503, 504)
RETRY_FIRST, RETRY_MOST = 1.0, 15.0
# straight to the chat, never through an http_proxy from the environment: the token would pass it
_open = urllib.request.build_opener(urllib.request.ProxyHandler({})).open


class Refused(Exception):
    """The token was refused (401, 403)."""


def call(method: str, path: str, body=None, headers=None, timeout: float = 30):
    """(status, JSON) for one request to the chat; Refused for 401 and 403."""
    base = os.environ.get("CLAUDE_WB_URL", "").rstrip("/")
    token = os.environ.get("CLAUDE_WB_TOKEN", "")
    if not base or not token:
        raise SystemExit(_say("set CLAUDE_WB_URL and CLAUDE_WB_TOKEN", 3))
    h = {"Authorization": f"Bearer {token}", **(headers or {})}
    if isinstance(body, dict):
        body, h["Content-Type"] = json.dumps(body).encode(), "application/json"
    req = urllib.request.Request(base + path, data=body, method=method, headers=h)
    try:
        with _open(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            got = json.loads(e.read() or b"{}")
        except ValueError:
            got = {}
        if e.code in (401, 403):
            raise Refused(got.get("error") or f"HTTP {e.code}") from None
        return e.code, got


def _say(text: str, code: int) -> int:
    print(f"ask_claude: {text}", file=sys.stderr)
    return code


def upload(path: Path) -> str:
    """Upload one file; its upload id."""
    uid = str(uuid.uuid4())
    kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    code, got = call("PUT", f"/api/upload/{uid}", path.read_bytes(),
                     {"X-File-Name": quote(path.name), "Content-Type": kind}, timeout=120)
    if code != 200:
        raise RuntimeError(f"{path}: {got.get('error') or f'HTTP {code}'}")
    return uid


def send(text: str, files: list[str]) -> dict:
    """POST /api/send, waiting out a rate limit (the same cid: never sent twice)."""
    body = {"text": text, "cid": str(uuid.uuid4()), **({"files": files} if files else {})}
    for _ in range(5):
        code, got = call("POST", "/api/send", body)
        if code == 429:
            time.sleep(min(60, int(got.get("retry_after") or 5)))
            continue
        if code != 200:
            raise RuntimeError(got.get("error") or f"HTTP {code}")
        return got
    raise RuntimeError("still rate-limited after five tries")


def follow(after: str, until: str, deadline: float) -> int:
    """Print Claude's replies after ``after`` until the turn is done (``until`` "reply": first a
    new reply, for a later report)."""
    last, quiet_since, paused, pause, cut = after, None, False, RETRY_FIRST, None
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            why = f"the chat didn't answer ({cut})" if cut else "Claude is still at work"
            return _say(f"{why}: ask_claude.py --after {last} waits on", 1)
        each = int(max(0, min(WAIT_EACH, left)))
        q = urlencode({"after": last, "timeout": each, "until": until})
        try:
            code, got = call("GET", f"/api/wait?{q}", timeout=each + 30)
        except OSError as e:  # a reset or refused connection: the server restarting
            code, got = None, {"error": str(e)}
        if code is None or code in TRANSIENT:
            cut = got.get("error") or f"HTTP {code}"
            time.sleep(max(0, min(pause, deadline - time.monotonic())))
            pause = min(pause * 2, RETRY_MOST)
            continue
        pause, cut = RETRY_FIRST, None
        if code != 200:
            return _say(got.get("error") or f"HTTP {code}", 1)
        for r in got.get("replies") or []:
            print(r.get("text", ""), flush=True)
            print(flush=True)
            last, until = r["id"], "done"
            print(f"[reply {last}]", file=sys.stderr, flush=True)
        if got.get("waiting_on_user") and not paused:  # a dialog or a question for the user
            paused = True
            print("[waiting on the user: Claude is paused on their answer]", file=sys.stderr, flush=True)
        if got.get("done") and until == "done":
            print(f"[done: follow up with ask_claude.py --after {last}]", file=sys.stderr)
            return 0
        if got.get("listener") is None:  # the session went away: give it a minute to come back
            quiet_since = quiet_since or time.monotonic()
            if time.monotonic() - quiet_since > 60:
                return _say(f"Claude stopped listening; ask_claude.py --after {last} waits on later", 2)
            time.sleep(5)
        else:
            quiet_since = None


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Hand a request to Claude (workbench's web chat API).")
    p.add_argument("request", nargs="?", help="what to ask: one request, with the repo and the goal")
    p.add_argument("--file", action="append", default=[], help="a file to attach (again for more)")
    p.add_argument("--timeout", type=float, default=3600, help="seconds to wait in all (default 3600)")
    p.add_argument("--after", metavar="ID", help="send nothing: wait for Claude's next report after "
                   "this message or reply")
    a = p.parse_args(argv)
    if bool(a.request) == bool(a.after):
        p.error("a request, or --after <id>")
    deadline = time.monotonic() + a.timeout
    try:
        _, health = call("GET", "/api/health")
        if not health.get("listening"):
            return _say("Claude isn't listening (no Claude Code session in workbench mode with the "
                        "web chat on): do it yourself, or try later", 2)
        if a.after:
            return follow(a.after, "reply", deadline)
        sent = send(a.request, [upload(Path(f)) for f in a.file])
        mid = sent["message"]["id"]
        print(f"[message {mid}]", file=sys.stderr, flush=True)
        if sent.get("listener") is None:
            return _say(f"Claude isn't listening: message {mid} waits in the queue", 2)
        return follow(mid, "done", deadline)
    except Refused as e:
        return _say(f"the token was refused: {e}", 3)
    except (OSError, RuntimeError, ValueError) as e:
        return _say(str(e), 1)


if __name__ == "__main__":
    sys.exit(main())
