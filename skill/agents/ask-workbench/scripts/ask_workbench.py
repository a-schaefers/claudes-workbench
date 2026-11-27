#!/usr/bin/env python3
"""Hand a request to Workbench's driver (the lead: Claude Code or OpenCode, whichever listens
on the web chat) through the chat's API, and print the driver's replies.

    ask_workbench.py "<request>" [--file F]... [--timeout S] [--key K]
    ask_workbench.py --after <message or reply id> [--timeout S]     # wait for a later report

Environment: WORKBENCH_URL (the chat's address, e.g. http://localhost:1337) and WORKBENCH_TOKEN
(this agent's token, from `wb chat token add <name>`); CLAUDE_WB_URL and CLAUDE_WB_TOKEN, their
names before 2.2, still work. Standard library only.

Prints each of the driver's replies to stdout as it comes, and on stderr the ids to follow up
with and which driver answered ("[answered by OpenCode (<its model>)]", when the chat says).
Exit status: 0 when the driver's turn is done, 1 on an error or the timeout (still at work: wait
again with --after), 2 when no driver is listening, the message expired unread, or the chat stays
unreachable, 3 when the token is refused. A send or a wait the server cuts (it restarts on an
update) is asked again for up to two minutes; a send carries an Idempotency-Key, so it never
lands twice. A send still unreachable then exits 2 naming its key: it may have landed, and the
same request again with --key K is answered with that message if it did, never sent twice.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import quote, urlencode

WAIT_EACH = 300  # seconds one GET /api/wait may hold (the server's default)
# A wait cut by the server going away for a moment (it re-execs itself on a code change, `wb
# update` or a restart of its unit; a proxy in front answers 502 to 504 meanwhile) is
# asked again after a pause growing from RETRY_FIRST to RETRY_MOST seconds: it ended the client at
# once with a bare error and no `--after` id (#1107, 2026-11-23). A chat cut longer than any
# restart takes (OUTAGE_MOST seconds in a row: `wb chat off`, the machine asleep, a tunnel down)
# ends it with exit 2 and the `--after` id, never a wait to the deadline (an hour by default:
# the #1107 review, 2026-11-23)
TRANSIENT = (502, 503, 504)
# The variables, the new names first: the old ones (CLAUDE_WB_*) named one driver where either
# answers (#1366, 2026-11-25), and an install made before 2.2 still sets them
URL_VARS, TOKEN_VARS = ("WORKBENCH_URL", "CLAUDE_WB_URL"), ("WORKBENCH_TOKEN", "CLAUDE_WB_TOKEN")
# the chat's driver names (a reply's `driver.tool`, the listener's `driver`), as a person reads them
DRIVERS = {"claude": "Claude Code", "opencode": "OpenCode"}
RETRY_FIRST, RETRY_MOST = 1.0, 15.0
OUTAGE_MOST = 120.0
class _Stay(urllib.request.HTTPRedirectHandler):
    """No redirect is followed: urllib sends the Authorization header on to wherever a 3xx points,
    so a redirect could carry the token off this machine (the chat never sends one: a 3xx comes
    back as its status; #1138, the MCP server's this-machine-only check is moot otherwise)."""
    def redirect_request(self, *args, **kwargs):
        return None


# straight to the chat, never through an http_proxy from the environment: the token would pass it
_open = urllib.request.build_opener(urllib.request.ProxyHandler({}), _Stay).open


class Refused(Exception):
    """The token was refused (401, 403)."""


class Unreachable(RuntimeError):
    """A send cut for OUTAGE_MOST in a row; ``key`` is its Idempotency-Key (and cid)."""
    def __init__(self, text: str, key: str):
        super().__init__(text)
        self.key = key


# what both the server's Idempotency-Key (1 to 255) and a cid (at most 64) take, as --key is both
KEY = re.compile(r"[!-~]{1,64}")


def env(names: tuple[str, ...]) -> str:
    """The first of ``names`` set in the environment, else ""."""
    return next((v for n in names if (v := os.environ.get(n, ""))), "")


def who(driver) -> str:
    """A driver as the chat reports it (`{tool, model?}`) in words: "Claude Code", "OpenCode
    (<provider>/<model>)"; "" when the chat doesn't say (a chat before 2.2)."""
    if not isinstance(driver, dict) or driver.get("tool") not in DRIVERS:
        return ""
    model = driver.get("model")
    return DRIVERS[driver["tool"]] + (f" ({model})" if isinstance(model, str) and model else "")


def call(method: str, path: str, body=None, headers=None, timeout: float = 30, answered=None):
    """(status, JSON) for one request to the chat; Refused for 401 and 403. ``answered``, a dict,
    gets the response's headers (the MCP server reads Idempotent-Replayed from them)."""
    base = env(URL_VARS).rstrip("/")
    token = env(TOKEN_VARS)
    if not base or not token:
        raise SystemExit(_say("set WORKBENCH_URL and WORKBENCH_TOKEN", 3))
    h = {"Authorization": f"Bearer {token}", **(headers or {})}
    if isinstance(body, dict):
        body, h["Content-Type"] = json.dumps(body).encode(), "application/json"
    req = urllib.request.Request(base + path, data=body, method=method, headers=h)
    try:
        with _open(req, timeout=timeout) as r:
            if answered is not None:
                answered.update({k.lower(): v for k, v in r.headers.items()})
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
    print(f"ask_workbench: {text}", file=sys.stderr)
    return code


def upload(path: Path, key: str) -> str:
    """Upload one file; its upload id. The id comes from the send's key and the file's bytes, so a
    rerun with --key names the same files and its body matches the first send's: a random id made
    the body differ, and the retry was refused 422 (#1138). The server stores an id once."""
    data = path.read_bytes()
    uid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{key}\n{hashlib.sha256(data).hexdigest()}"))
    kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    code, got = call("PUT", f"/api/upload/{uid}", data,
                     {"X-File-Name": quote(path.name), "Content-Type": kind}, timeout=120)
    if code != 200:
        raise RuntimeError(f"{path}: {got.get('error') or f'HTTP {code}'}")
    return uid


def send(text: str, files: list[str], key: str) -> dict:
    """POST /api/send, waiting out a rate limit, and sent again when the chat cut it (a reset, 502
    to 504) for up to OUTAGE_MOST: one Idempotency-Key (and cid) ``key`` for every try, so a send
    that landed before the cut is answered with its message, never sent twice (#1138). Still cut
    then: Unreachable, with the key, for main to name (exit 2)."""
    body = {"text": text, "cid": key, **({"files": files} if files else {})}
    limited, pause, cut_since = 0, RETRY_FIRST, None
    while True:
        try:
            code, got = call("POST", "/api/send", body, {"Idempotency-Key": key})
        except OSError as e:  # the connection reset or refused: the server restarting
            code, got = None, {"error": str(e)}
        if code is None or code in TRANSIENT:
            cut_since = cut_since or time.monotonic()
            if time.monotonic() - cut_since > OUTAGE_MOST:
                raise Unreachable(f"the chat is unreachable ({got.get('error') or f'HTTP {code}'})", key)
            time.sleep(pause)
            pause = min(pause * 2, RETRY_MOST)
            continue
        if code == 429:
            limited += 1
            if limited >= 5:
                raise RuntimeError("still rate-limited after five tries")
            time.sleep(min(60, int(got.get("retry_after") or 5)))
            continue
        if code != 200:
            raise RuntimeError(got.get("error") or f"HTTP {code}")
        return got


def follow(after: str, until: str, deadline: float) -> int:
    """Print the driver's replies after ``after`` until the turn is done (``until`` "reply": first
    a new reply, for a later report), and which driver answered, once and again when it changes."""
    last, quiet_since, paused, pause, cut, cut_since = after, None, False, RETRY_FIRST, None, None
    said = ""
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            why = f"the chat didn't answer ({cut})" if cut else "the driver is still at work"
            return _say(f"{why}: ask_workbench.py --after {last} waits on", 1)
        each = int(max(0, min(WAIT_EACH, left)))
        q = urlencode({"after": last, "timeout": each, "until": until})
        try:
            code, got = call("GET", f"/api/wait?{q}", timeout=each + 30)
        except OSError as e:  # a reset or refused connection: the server restarting
            code, got = None, {"error": str(e)}
        if code is None or code in TRANSIENT:
            cut = got.get("error") or f"HTTP {code}"
            cut_since = cut_since or time.monotonic()
            if time.monotonic() - cut_since > OUTAGE_MOST:
                return _say(f"the chat is unreachable ({cut}): ask_workbench.py --after {last} waits on later", 2)
            time.sleep(max(0, min(pause, deadline - time.monotonic())))
            pause = min(pause * 2, RETRY_MOST)
            continue
        pause, cut, cut_since = RETRY_FIRST, None, None
        if got.get("code") == "expired":  # never passed on: nobody listened for hours (#1138)
            return _say(f"{got.get('error')}: message {last}", 2)
        if code != 200:
            return _say(got.get("error") or f"HTTP {code}", 1)
        for r in got.get("replies") or []:
            # the reply's own writer (#1366), else the session listening now (an older reply)
            if (by := who(r.get("driver")) or who((got.get("listener") or {}).get("driver"))) and by != said:
                said = by
                print(f"[answered by {by}]", file=sys.stderr, flush=True)
            print(r.get("text", ""), flush=True)
            print(flush=True)
            last, until = r["id"], "done"
            print(f"[reply {last}]", file=sys.stderr, flush=True)
        if got.get("waiting_on_user") and not paused:  # a dialog or a question for the user
            paused = True
            print("[waiting on the user: the driver is paused on their answer]", file=sys.stderr, flush=True)
        if got.get("done") and until == "done":
            print(f"[done: follow up with ask_workbench.py --after {last}]", file=sys.stderr)
            return 0
        if got.get("listener") is None:  # the session went away: give it a minute to come back
            quiet_since = quiet_since or time.monotonic()
            if time.monotonic() - quiet_since > 60:
                return _say(f"the driver stopped listening; ask_workbench.py --after {last} waits on later", 2)
            time.sleep(5)
        else:
            quiet_since = None


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Hand a request to Workbench's driver (its web chat API).")
    p.add_argument("request", nargs="?", help="what to ask: one request, with the repo and the goal")
    p.add_argument("--file", action="append", default=[], help="a file to attach (again for more)")
    p.add_argument("--timeout", type=float, default=3600, help="seconds to wait in all (default 3600)")
    p.add_argument("--after", metavar="ID", help="send nothing: wait for the driver's next report after "
                   "this message or reply")
    p.add_argument("--key", help="the Idempotency-Key an unreachable send named: the same request "
                   "again with it lands at most once")
    a = p.parse_args(argv)
    if bool(a.request) == bool(a.after):
        p.error("a request, or --after <id>")
    if a.key is not None and not (a.request and KEY.fullmatch(a.key)):
        p.error("--key goes with a request: 1 to 64 printable characters, no spaces")
    deadline = time.monotonic() + a.timeout
    try:
        _, health = call("GET", "/api/health")
        if not health.get("listening"):
            return _say("no driver is listening (no Claude Code or OpenCode session in workbench "
                        "mode with the web chat on): do it yourself, or try later", 2)
        if a.after:
            return follow(a.after, "reply", deadline)
        key = a.key or str(uuid.uuid4())
        sent = send(a.request, [upload(Path(f), key) for f in a.file], key)
        mid = sent["message"]["id"]
        print(f"[message {mid}]", file=sys.stderr, flush=True)
        if sent.get("listener") is None:
            return _say(f"no driver is listening: message {mid} waits in the queue", 2)
        return follow(mid, "done", deadline)
    except Refused as e:
        return _say(f"the token was refused: {e}", 3)
    except Unreachable as e:
        # the send may have landed before the cut, and with no message id the agent had nothing
        # to follow; exit 1 said "an error" where the docs promised 2 (the #1138 review)
        return _say(f"{e}: the message may have landed (Idempotency-Key and cid {e.key}); the same "
                    f"request again with --key {e.key} is answered with it if so, never sent twice", 2)
    except (OSError, RuntimeError, ValueError) as e:
        return _say(str(e), 1)


if __name__ == "__main__":
    sys.exit(main())
