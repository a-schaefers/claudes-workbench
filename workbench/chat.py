"""The web chat: a side door to Claude for the user when they are away from the terminal.

`wb chat serve` serves a small page and a JSON API. Messages the user sends are appended to
chat/messages.jsonl; the mod of a Claude Code session in /workbench mode picks them up, records a
receipt (chat/receipts.jsonl) and submits each to Claude as a prompt. Claude answers with
`wb chat reply`, which the page shows. chat/listener.json is the session currently listening
(the mod rewrites it every poll), so the page can say whether anyone will read a message now.

What a message can do is what typing in the terminal can do, so the API wants a secret token
(~/.config/workbench/chat-token, made on first use; `wb chat url` prints a link carrying it in
the fragment, which never leaves the browser) and answers only to the host names it serves,
which stops other web pages and DNS rebinding. It serves localhost unless the user says otherwise
in ~/.config/workbench/chat.json (`bind`: addresses, `tailscale` for this machine's tailnet
address, `0.0.0.0` for every interface; `hosts`: more names to answer to, `*` for any). The link
it prints is always localhost; reaching it from a phone is the user's own setup.
"""
from __future__ import annotations

import errno
import hmac
import json
import os
import secrets
import select
import shutil
import socket
import subprocess
import sys
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import savings, store, webpush
from .watch import Watcher

PORT = 1337
REPLAN_EVERY = 30       # seconds between looks at a skipped bind address
LISTENING_FOR = 15      # seconds: a listener.json older than this means nobody is listening
MAX_TEXT = 8000
MAX_BODY = 32 * 1024
SHOT_MAX = 16 << 20  # a screenshot served whole up to this
MAX_STREAMS = 16        # open /api/stream connections (each holds a thread)
PING_EVERY = 15         # seconds between keep-alive comments on a stream
TIMEOUT = 30            # seconds a connection may stall (more than PING_EVERY: streams write by then)
UNACKED_MS = 45_000     # a stream whose writes go unacknowledged this long is dead (Linux)
RELOAD_EVERY = 10       # seconds between looks at the server's own code
PUSH_DELAY = 10         # seconds news waits for an open page to show it before a push goes
PUSH_WINDOW = 900       # only news this recent is pushed, never a backlog
PUSH_RETRY = 30         # seconds before a device whose push failed for now is tried again
# macOS's App Store and standalone Tailscale apps keep the CLI in the bundle, off PATH
TAILSCALE_APP = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
STATIC = Path(__file__).parent / "chat_static"
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml",
         ".webmanifest": "application/manifest+json", ".png": "image/png"}
_send_lock = threading.Lock()


def chat_dir() -> Path:
    d = store.home() / "chat"
    d.mkdir(parents=True, exist_ok=True)
    return d


def token_path() -> Path:
    return Path(os.environ.get("WB_CHAT_TOKEN") or Path.home() / ".config" / "workbench" / "chat-token")


def config_path() -> Path:
    return Path(os.environ.get("WB_CHAT_CONFIG") or Path.home() / ".config" / "workbench" / "chat.json")


def settings() -> dict:
    """The user's choices for the server: {"bind": [...], "hosts": [...]}; {} for the defaults."""
    return store.read_json(config_path(), {}) or {}


def token() -> str:
    path = token_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(32) + "\n")
    if path.stat().st_mode & 0o077:
        path.chmod(0o600)  # the token is the whole of the chat's auth
    got = path.read_text().strip()
    if len(got) < 16:  # an empty token would let in any request
        raise RuntimeError(f"{path} holds no usable token: delete it and wb chat makes a new one")
    return got


def tailscale() -> dict:
    """This machine on the tailnet: {"ip", "names"}; {} when Tailscale isn't up; {"missing": True}
    when there is no CLI to ask (said apart from "not running": the 2026-11-16 audit)."""
    cli = shutil.which("tailscale") or (TAILSCALE_APP if sys.platform == "darwin"
                                        and os.path.isfile(TAILSCALE_APP) else None)
    if not cli:
        return {"missing": True}
    try:
        r = subprocess.run([cli, "status", "--json"], capture_output=True, text=True, timeout=5)
        me = json.loads(r.stdout)["Self"]
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        return {}
    ips = [ip for ip in me.get("TailscaleIPs") or [] if "." in ip]
    dns = (me.get("DNSName") or "").rstrip(".")
    names = {n for n in (dns, dns.split(".")[0], me.get("HostName", "")) if n}
    return {"ip": ips[0] if ips else None, "names": names}


# ---- the thread ----

def shots(task: str, n) -> dict | None:
    """What a pair of wb shot's screenshots is of (tasks/<task>/shots/<n>/shots.json, #99): the
    task, step, round, viewport and each side's revision and image; None when there is none."""
    if not (isinstance(task, str) and store.NAME_RE.match(task) and isinstance(n, int) and n > 0):
        return None
    m = store.read_json(store.Task(task).dir / "shots" / str(n) / "shots.json", None)
    if not isinstance(m, dict):
        return None
    return {"task": task, "n": n, "step": m.get("step"), "round": m.get("round"),
            "viewport": m.get("viewport"),
            **{s: {"rev": (m.get(s) or {}).get("rev"), "image": bool((m.get(s) or {}).get("image"))}
               for s in ("base", "candidate")}}


def shot_png(task: str, n: int, side: str) -> bytes | None:
    """One side's screenshot, never through a link: it is wb shot's own copy."""
    if side not in ("base", "candidate") or not shots(task, n):
        return None
    p = store.Task(task).dir / "shots" / str(n) / f"{side}.png"
    try:
        fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as f:
        return f.read(SHOT_MAX + 1)[:SHOT_MAX]


def send(text: str, frm: str, cid: str | None = None, **extra) -> dict:
    """Append a message. A client id (cid) already in the thread returns that message instead,
    so a retried send never lands twice. ``extra``: a pair of screenshots (``shots``), or what a
    reply is about (``about``)."""
    with _send_lock:
        if cid:
            for r in store.read_jsonl(chat_dir() / "messages.jsonl")[0][-500:]:
                if r.get("cid") == cid:
                    return r
        row = {"id": f"{int(time.time() * 1000)}-{secrets.token_hex(3)}", "ts": store.now(),
               "from": frm, "text": text, **({"cid": cid} if cid else {}),
               **{k: v for k, v in extra.items() if v}}
        store.append_jsonl(chat_dir() / "messages.jsonl", row)
        return row


CLAIM_STALE = 120  # seconds: a claim its session never followed with a receipt gives way


def claim(msg_id: str, by: str) -> bool:
    """Whether session `by` may submit message `msg_id` as a prompt: the first session to ask, under
    a lock (chat/claims.json), or the same one again (a retry). Two sessions starting together
    both read an empty listener.json, both claimed the chat, and both submitted the same message
    (the 2026-11-18 audit, #84); each mod's own guard knew only its own sends."""
    path = chat_dir() / "claims.json"
    with store.locked(chat_dir() / "claims.lock"):
        now = store.now()
        claims = {k: v for k, v in (store.read_json(path, {}) or {}).items()
                  if isinstance(v, dict) and now - v.get("ts", 0) < 86400}  # a day's
        cur = claims.get(msg_id)
        if cur and cur.get("by") != by and now - cur.get("ts", 0) < CLAIM_STALE:
            return False
        claims[msg_id] = {"by": by, "ts": now}
        store.write_json(path, claims)
    return True


def ack(msg_id: str, by: str, how: str = "now") -> None:
    """Claude has the message: read at once ("now"), or queued until its current step ends."""
    store.append_jsonl(chat_dir() / "receipts.jsonl",
                       {"id": msg_id, "ts": store.now(), "by": by, "how": how})


def thread(limit: int = 200) -> list[dict]:
    """The latest messages, each of the user's with its receipt (or None: not read yet)."""
    rows = store.read_jsonl(chat_dir() / "messages.jsonl")[0][-limit:]
    receipts = {r["id"]: r for r in store.read_jsonl(chat_dir() / "receipts.jsonl")[0]}
    return [{**r, "received": receipts.get(r["id"])} if r["from"] == "user" else r for r in rows]


def pending() -> list[dict]:
    return [r for r in thread() if r["from"] == "user" and not r["received"]]


def snapshot() -> dict:
    return {"messages": thread(), "asks": asks(), "listener": listener(), "prefs": prefs(),
            "push": {"key": push_key(), "test": (store.read_json(chat_dir() / "push-test.json", None)
                                                 or {}).get("ts")},
            "savings": week_saved(), "build": build(), "now": store.now()}


def week_saved() -> dict:
    """The gear sheet's Savings: `wb savings` for the last 7 days (savings.totals)."""
    t = savings.totals(store.results(), since=store.now() - savings.PERIODS["week"])
    return {**t, "score": savings.score(t)}  # the badge (#101)


# ---- notifications: web push with no payload (webpush.py) ----
# The page reports what it showed while visible (/api/seen); news no page showed within
# PUSH_DELAY gets one push to every subscribed device, whose service worker then fetches the state
# and shows the newest open ask, else Claude's latest message. seen.jsonl: {id, ts, by: page|push,
# endpoint (push: the device that took it)}.

_keys: dict[str, str] = {}


def push_key() -> str:
    """This machine's VAPID public key (the applicationServerKey)."""
    path = str(token_path())
    if path not in _keys:
        _keys[path] = webpush.public_key(webpush.private_key(token_path()))
    return _keys[path]


def _seen() -> set:
    """Ids no push is owed for: shown on a page (or pushed before pushes were counted per device)."""
    return {r.get("id") for r in store.read_jsonl(chat_dir() / "seen.jsonl")[0] if not r.get("endpoint")}


def _pushed() -> dict[str, set]:
    """Each id's devices (endpoints) that took its push."""
    out: dict[str, set] = {}
    for r in store.read_jsonl(chat_dir() / "seen.jsonl")[0]:
        if r.get("endpoint"):
            out.setdefault(r.get("id"), set()).add(r["endpoint"])
    return out


def _transient(status: int) -> bool:
    """A push that may go through if tried again: not reached, rate-limited, the service's fault."""
    return status == 0 or status == 429 or status >= 500


def mark_seen(ids, by: str) -> None:
    done = _seen()
    for i in dict.fromkeys(ids):
        if i not in done:
            store.append_jsonl(chat_dir() / "seen.jsonl", {"id": i, "ts": store.now(), "by": by})


def news(now: float | None = None) -> list[str]:
    """Ids of Claude's messages and open asks old enough to push and recent enough to matter,
    not yet shown or pushed."""
    now = now or store.now()
    done = _seen()
    items = [(m["id"], m["ts"]) for m in thread() if m.get("from") == "claude"]
    items += [(a["id"], a["ts"]) for a in asks() if a["answer"] is None]
    return [i for i, ts in items if now - PUSH_WINDOW < ts <= now - PUSH_DELAY and i not in done]


class Pusher:
    """Sends the pushes: woken by the hub, and every PUSH_DELAY/2 so news ripens on time. Each
    device is counted on its own: every due push was marked sent whatever the services said, so a
    phone whose service was down for seconds missed a permission prompt (the 2026-11-18 audit,
    #86). One that failed for now is tried again after PUSH_RETRY, within PUSH_WINDOW."""

    def __init__(self, hub: "Hub", send=None):
        self.hub = hub
        self.send = send or (lambda eps: webpush.push_all(webpush.private_key(token_path()), eps))
        self.retry_at: dict[str, float] = {}  # endpoint: when a failed one may be tried again

    def tick(self, now: float | None = None) -> list[str]:
        """The ids pushed now, to at least one device."""
        now = now or store.now()
        due = news(now)
        subs = [x["endpoint"] for x in webpush.subscriptions()]
        if not due or not subs:
            return []
        pushed = _pushed()
        due = [i for i in due if any(e not in pushed.get(i, ()) for e in subs)]  # owed to a device
        owed = [e for e in subs if self.retry_at.get(e, 0) <= now
                and any(e not in pushed.get(i, ()) for i in due)]
        if not owed:
            return []
        got = self.send(owed) or {}
        took = False
        for e in owed:
            if _transient(got.get(e, 0)):
                self.retry_at[e] = now + PUSH_RETRY
                continue
            self.retry_at.pop(e, None)  # delivered, or refused for good (gone: unsubscribed)
            took = True
            for i in due:
                if e not in pushed.get(i, ()):
                    store.append_jsonl(chat_dir() / "seen.jsonl",
                                       {"id": i, "ts": store.now(), "by": "push", "endpoint": e})
        return due if took else []

    def run(self) -> None:
        seen = -1
        while True:
            with self.hub.cond:
                self.hub.cond.wait_for(lambda: self.hub.version != seen, timeout=PUSH_DELAY / 2)
                seen = self.hub.version
            try:
                self.tick()
            except Exception as e:  # a push service down must not end the pushing
                print(f"web chat: a push failed ({e!r})", file=sys.stderr, flush=True)


def build() -> str:
    """The page's files as served now: a page that loaded other ones reloads itself, so an update
    never leaves a phone running the old page against the new server."""
    files = [(f.name, f.stat().st_mtime_ns, f.stat().st_size) for f in sorted(STATIC.iterdir())]
    return format(zlib.crc32(repr(files).encode()), "x")  # not hash(): it differs per process


# ---- asks: permission prompts, plans, questions ----
# While the user is away on the web chat, the mod puts Claude Code's dialogs here: the terminal's
# would wait for nobody (the user, 2026-11-18). A `permission` (allow or deny the tool), a `plan`
# (ExitPlanMode: approve it, or keep planning with a note), a `question` (AskUserQuestion: the
# chosen options, `answers` keyed by question). asks.jsonl holds an ask row {id, ts, tool, text,
# kind, detail?}, then answer rows {id, ts, answer, by, note?, answers?}; the first answer row
# wins, so the page's answer and the mod's expiry, written by two processes, can't both stand.
ANSWERS = ("allow", "deny", "expired")
KINDS = ("permission", "plan", "question")
ASK_TEXT = 2000
DETAIL_MAX = 40_000   # a plan or four questions with previews
NOTE_MAX = 2000
AWAY = ("ask", "allow")  # prefs.json `away`: ask the user here, or let Claude go ahead


def ask(tool: str, text: str, kind: str = "permission", detail: dict | None = None) -> dict:
    """A new ask. A permission while the user's setting says `allow` is answered at once, by
    `setting`, and stays in the thread so they see what ran."""
    if kind not in KINDS:
        raise ValueError(kind)
    row = {"id": f"ask-{int(time.time() * 1000)}-{secrets.token_hex(4)}", "ts": store.now(),
           "tool": tool[:200], "text": text[:ASK_TEXT], "kind": kind}
    if detail is not None:
        if len(json.dumps(detail)) > DETAIL_MAX:
            raise ValueError("detail too long")
        row["detail"] = detail
    store.append_jsonl(chat_dir() / "asks.jsonl", row)
    if kind == "permission" and prefs()["away"] == "allow":
        return answer(row["id"], "allow", "setting")
    return {**row, "answer": None}


def asks(limit: int = 20) -> list[dict]:
    """The latest asks, each with its answer (None while open), `answered`, `by`, and the answer's
    `note` and `answers` when it has them."""
    seen: dict[str, dict] = {}
    for r in store.read_jsonl(chat_dir() / "asks.jsonl")[0]:
        a = seen.get(r.get("id"))
        if "answer" in r:
            if a and a["answer"] is None and r["answer"] in ANSWERS:
                a.update(answer=r["answer"], answered=r["ts"], by=r.get("by"))
                a.update({k: r[k] for k in ("note", "answers") if k in r})
        elif "tool" in r and r.get("id"):
            seen[r["id"]] = {"kind": "permission", **r, "answer": None}
    return list(seen.values())[-limit:]


def find_ask(ask_id: str) -> dict | None:
    return next((a for a in reversed(asks(10_000)) if a["id"] == ask_id), None)


def answer(ask_id: str, verdict: str, by: str, note: str | None = None,
           answers: dict | None = None) -> dict | None:
    """Answer an open ask (a later answer changes nothing). The ask as it now stands, None when
    there is no such ask. `answers` only for a question, and only its own questions."""
    if verdict not in ANSWERS:
        raise ValueError(verdict)
    cur = find_ask(ask_id)
    if cur and cur["answer"] is None:
        row = {"id": ask_id, "ts": store.now(), "answer": verdict, "by": by}
        if note:
            row["note"] = note[:NOTE_MAX]
        if answers is not None:
            asked = {q.get("question") for q in (cur.get("detail") or {}).get("questions") or []}
            if cur["kind"] != "question" or verdict != "allow" or not answers \
                    or not set(answers) <= asked or not all(isinstance(v, str) and 0 < len(v) <= NOTE_MAX
                                                            for v in answers.values()):
                raise ValueError("answers")
            row["answers"] = answers
        elif cur["kind"] == "question" and verdict == "allow":
            raise ValueError("answers")  # a question is answered with its answers
        store.append_jsonl(chat_dir() / "asks.jsonl", row)
        cur = find_ask(ask_id)
    return cur


def latest_plan() -> str:
    """The plan Claude Code just wrote: the newest of its plan files, from the last day (the
    ExitPlanMode call names none)."""
    d = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser() / "plans"
    try:
        f = max(d.glob("*.md"), key=lambda p: p.stat().st_mtime)
    except (ValueError, OSError):
        return ""
    if time.time() - f.stat().st_mtime > 86400:
        return ""
    return f.read_text(errors="replace")[:DETAIL_MAX - 100]


def prefs() -> dict:
    """The user's choices on the page: {"away": "ask"|"allow"} (chat/prefs.json)."""
    p = store.read_json(chat_dir() / "prefs.json", None) or {}
    return {"away": p.get("away") if p.get("away") in AWAY else "ask"}


def set_prefs(**changes) -> dict:
    with store.locked(chat_dir() / "prefs.lock"):
        p = {**prefs(), **changes}
        if p["away"] not in AWAY:
            raise ValueError("away")
        store.write_json(chat_dir() / "prefs.json", p)
    return p


# The web chat's switch (the user, 2026-11-19): off unless they turn it on, from the pane or `wb chat
# on`, and kept as they left it. Off, the pane shows no link and no session listens or sends it
# their prompts; a server already running (systemd's) is left alone, its messages waiting.
def is_on() -> bool:
    return bool((store.read_json(chat_dir() / "switch.json", None) or {}).get("on"))


def set_on(on: bool) -> None:
    store.write_json(chat_dir() / "switch.json", {"on": bool(on)})
    if not on:  # nobody listens now: the page would say a live session does
        (chat_dir() / "listener.json").unlink(missing_ok=True)


def running(port: int) -> bool:
    """Whether a server answers on the port at an address this machine's plan binds."""
    for addr in plan()["binds"]:
        try:
            with socket.create_connection(("127.0.0.1" if addr in WILDCARD else addr, port), timeout=1):
                return True
        except OSError:
            continue
    return False


def start(port: int) -> int:
    """`wb chat serve` detached, its output in chat/serve.log, its pid in chat/serve.pid: for a
    machine without the systemd unit, so turning the chat on is enough to reach it."""
    root = Path(__file__).resolve().parent.parent
    with open(chat_dir() / "serve.log", "a") as log:
        proc = subprocess.Popen([sys.executable, "-X", "utf8", str(root / "bin" / "wb"), "chat", "serve",
                                 "--port", str(port)], cwd=root, stdin=subprocess.DEVNULL,
                                stdout=log, stderr=log, start_new_session=True)  # outlives wb
    store.write_json(chat_dir() / "serve.pid", {"pid": proc.pid, "port": port})
    return proc.pid


def stop_started() -> bool:
    """Stop the server `start` made, if it still runs; never one wb didn't start (systemd's)."""
    from . import models  # only here: chat is imported by the server, which needs none of it
    pid = (store.read_json(chat_dir() / "serve.pid", None) or {}).get("pid")
    (chat_dir() / "serve.pid").unlink(missing_ok=True)
    if not pid or not _alive(pid) or not models.runs(int(pid), "chat", "serve"):
        return False
    try:
        os.kill(int(pid), 15)
    except OSError:
        return False
    return True


def wait_answer(ask_id: str, timeout: float) -> str | None:
    """Block until the ask is answered (its answer) or the timeout passes (None), woken by the
    file's change; KeyError when there is no such ask."""
    deadline = time.monotonic() + timeout
    w = Watcher(chat_dir())  # before the first look, so an answer between the two isn't missed
    try:
        while True:
            cur = find_ask(ask_id)
            if cur is None:
                raise KeyError(ask_id)
            if cur["answer"]:
                return cur["answer"]
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            w.wait(min(left, 5))
    finally:
        w.close()


def _alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (ProcessLookupError, ValueError, TypeError):
        return False
    except PermissionError:
        return True


def listener() -> dict | None:
    """The session listening now. Its `wb events` process (pid) lives exactly as long as the
    session; a listener.json without a pid falls back to its age."""
    st = store.read_json(chat_dir() / "listener.json", None)
    if not st:
        return None
    if st.get("pid"):
        return st if _alive(st["pid"]) else None
    return st if store.now() - st.get("ts", 0) <= LISTENING_FOR else None


class Hub:
    """One watcher for every open stream: it wakes them the moment a chat file changes or the
    listening session's process ends (a pidfd), so nothing polls."""

    def __init__(self):
        self.cond = threading.Condition()
        self.version = 0
        self.open: list = []  # the open streams' handlers, oldest first
        # the directory it started with: resolved again on each restart, a hub outliving its
        # server (a test's) made the next WB_HOME's chat/ before that test's setUp did (#134)
        self.dir = chat_dir()
        self.closed = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def close(self) -> None:
        """Stop watching: the next look ends the loop (within the watch's 60 s, or at once on a
        change), and nothing is made again."""
        self.closed.set()
        self.bump()

    def bump(self) -> None:
        with self.cond:
            self.version += 1
            self.cond.notify_all()

    def _run(self) -> None:
        while not self.closed.is_set():  # a hub that died would leave every page without news
            try:
                self._watch()
            except Exception as e:
                if self.closed.is_set():
                    break
                print(f"web chat: the file watcher failed ({e!r}); starting it again",
                      file=sys.stderr, flush=True)
                time.sleep(1)
            self.bump()

    def _watch(self) -> None:
        w = Watcher(self.dir)
        pid, pidfd = None, None
        try:
            while not self.closed.is_set():
                st = store.read_json(self.dir / "listener.json", None) or {}
                if st.get("pid") != pid:
                    if pidfd is not None:
                        os.close(pidfd)
                    pid, pidfd = st.get("pid"), None
                    try:
                        pidfd = os.pidfd_open(int(pid)) if pid else None
                    except (OSError, ValueError, AttributeError):
                        pidfd = None
                if not w.native or w.polled:  # no inotify, or chat/ fell back to looking
                    w.wait(1)
                else:
                    ready, _, _ = select.select([w.fd] + ([pidfd] if pidfd is not None else []), [], [], 60)
                    w.read()
                    if pidfd is not None and pidfd in ready:  # the listener is gone
                        os.close(pidfd)
                        pidfd = None
                    time.sleep(0.02)  # gather a burst of writes into one wake
                    w.read()
                self.bump()
        finally:
            w.close()
            if pidfd is not None:
                os.close(pidfd)


# ---- the server ----

class Handler(BaseHTTPRequestHandler):
    server_version = "wb-chat"
    timeout = TIMEOUT  # an idle or half-open connection holds a thread no longer than this
    hosts: set[str] | None = set()  # None: any Host (the user's `hosts: ["*"]`)
    secret = ""
    hub: "Hub"
    streams: threading.BoundedSemaphore

    def log_message(self, *args):  # the journal needn't hold every poll
        pass

    def _security_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' blob:; "
                         "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _allowed(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        if host.startswith("["):  # [v6] or [v6]:port
            host = host[1:host.find("]")] if "]" in host else host
        elif host.count(":") == 1:
            host = host.rsplit(":", 1)[0]
        if self.hosts is not None and host not in self.hosts:
            self._json(403, {"error": "unknown host"})
            return False
        return True

    def _authed(self) -> bool:
        auth = self.headers.get("Authorization") or ""
        got = auth[len("Bearer "):].strip() if auth.startswith("Bearer ") else ""
        if not self.secret or not got or not hmac.compare_digest(got.encode(), self.secret.encode()):
            self._json(401, {"error": "bad or missing token: open the link from `wb chat url`"})
            return False
        return True

    def do_GET(self):
        if not self._allowed():
            return
        path = self.path.split("?")[0]
        if path == "/manifest.webmanifest":  # with the key, it opens the installed app keyed
            k = (parse_qs(urlsplit(self.path).query).get("k") or [""])[0]
            m = json.loads((STATIC / "manifest.webmanifest").read_text())
            if self.secret and k and hmac.compare_digest(k.encode(), self.secret.encode()):
                m["start_url"] = f"/?token={k}"
            return self._send(200, json.dumps(m).encode(), TYPES[".webmanifest"])
        if path == "/api/shot":  # one side of wb shot's pair (#99), behind the token like the rest
            if self._authed():
                q = parse_qs(urlsplit(self.path).query)
                n = (q.get("n") or [""])[0]
                body = shot_png((q.get("task") or [""])[0], int(n) if n.isdigit() else 0,
                                (q.get("side") or [""])[0])
                if body is None:
                    self._json(404, {"error": "no such screenshot"})
                else:
                    self._send(200, body, "image/png")
            return
        if path == "/api/state":
            if self._authed():
                self._json(200, snapshot())
            return
        if path == "/api/stream":
            if self._authed():
                self._stream()
            return
        name = "index.html" if path in ("/", "/index.html") else path.lstrip("/")
        f = STATIC / name
        if "/" in name or f.suffix not in TYPES or not f.is_file():
            return self._json(404, {"error": "not found"})
        self._send(200, f.read_bytes(), TYPES[f.suffix])

    def _stream(self):
        """Server-sent events: the whole state on connect and on every change, a ping between.
        The client takes each state as the truth, so a reconnect is a resync."""
        hub = self.hub
        if not self.streams.acquire(blocking=False):
            # Full: the oldest stream gives way, since a stream the phone dropped without a word
            # (asleep, a new network) lingers while a live page can't get in; once the cap filled
            # with those, the page said "Reconnecting" until a hard refresh (2026-11-18)
            with hub.cond:
                if hub.open:
                    hub.open[0].evicted = True
                hub.version += 1
                hub.cond.notify_all()
            if not self.streams.acquire(timeout=5):
                return self._json(503, {"error": "too many open streams"})
        self.evicted = False
        with hub.cond:
            hub.open.append(self)
        if hasattr(socket, "TCP_USER_TIMEOUT"):  # a peer gone silent fails the next write
            try:
                self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT, UNACKED_MS)
            except OSError:
                pass
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self._security_headers()
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            seen, last_body, sent = -1, None, time.monotonic()
            while True:
                with hub.cond:
                    woke = hub.cond.wait_for(lambda: hub.version != seen,
                                             timeout=max(0.0, PING_EVERY - (time.monotonic() - sent)))
                    seen = hub.version
                if self.evicted:
                    return
                if woke:
                    snap = snapshot()
                    body = json.dumps({k: v for k, v in snap.items() if k != "now"})
                    if body != last_body:
                        last_body = body
                        self.wfile.write(f"event: state\ndata: {json.dumps(snap)}\n\n".encode())
                        self.wfile.flush()
                        sent = time.monotonic()
                        continue
                    if time.monotonic() - sent < PING_EVERY:
                        # Without inotify the hub wakes every second (how a dead listener is
                        # noticed): a ping only after PING_EVERY of silence, not one a second
                        # (the 2026-11-16 audit)
                        continue
                self.wfile.write(b": ping\n\n")  # keeps the line (and the page's watchdog) alive
                self.wfile.flush()
                sent = time.monotonic()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with hub.cond:
                hub.open.remove(self)
            self.streams.release()

    def do_POST(self):
        if not self._allowed():
            return
        if self.path not in ("/api/send", "/api/answer", "/api/prefs", "/api/seen", "/api/push"):
            return self._json(404, {"error": "not found"})
        if not self._authed():
            return
        if (self.headers.get("Content-Type") or "").split(";")[0] != "application/json":
            return self._json(415, {"error": "json only"})
        try:  # -1 would read until the client hangs up (the 2026-11-16 audit)
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if n > MAX_BODY:
            return self._json(413, {"error": "too long"})
        if n < 0:
            return self._json(400, {"error": "bad length"})
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
            text, cid = str(body.get("text", "")).strip(), body.get("cid")
        except (ValueError, AttributeError):
            return self._json(400, {"error": "bad json"})
        if self.path == "/api/answer":  # an ask: allow or deny, a note, a question's answers
            ask_id, verdict = body.get("id"), body.get("answer")
            note, answers = body.get("note"), body.get("answers")
            if not (isinstance(ask_id, str) and 0 < len(ask_id) <= 64) or verdict not in ("allow", "deny") \
                    or not (note is None or isinstance(note, str) and len(note) <= NOTE_MAX) \
                    or not (answers is None or isinstance(answers, dict) and len(answers) <= 4):
                return self._json(400, {"error": "an answer is {id, answer: allow|deny, note?, answers?}"})
            try:
                cur = answer(ask_id, verdict, "web", note=note or None, answers=answers)
            except ValueError:
                return self._json(400, {"error": "those answers don't fit the question"})
            if cur is None:
                return self._json(404, {"error": "no such prompt"})
            return self._json(200, {"ask": cur, "state": snapshot()})
        if self.path == "/api/seen":  # what the page showed while visible: no push for it
            ids = body.get("ids")
            if not (isinstance(ids, list) and len(ids) <= 200
                    and all(isinstance(x, str) and 0 < len(x) <= 64 for x in ids)):
                return self._json(400, {"error": "seen is {ids: [up to 200 ids]}"})
            mark_seen(ids, "page")
            return self._json(200, {})
        if self.path == "/api/push":  # this device's notifications: on, off, or a test
            if body.get("test") is True:
                store.write_json(chat_dir() / "push-test.json", {"ts": store.now()})
                sent = webpush.push_all(webpush.private_key(token_path()))
                return self._json(200, {"sent": len(sent), "ok": sum(1 for s in sent.values() if 200 <= s < 300)})
            endpoint, on = body.get("endpoint"), body.get("on")
            if not webpush.accepted(endpoint) or not isinstance(on, bool):
                return self._json(400, {"error": "push is {endpoint: a push service's https URL, on: true|false}"})
            (webpush.subscribe(endpoint, self.headers.get("User-Agent") or "") if on
             else webpush.unsubscribe(endpoint))
            return self._json(200, {"on": on})
        if self.path == "/api/prefs":  # the page's settings
            if set(body) - {"away"} or body.get("away") not in AWAY:
                return self._json(400, {"error": "prefs are {away: ask|allow}"})
            set_prefs(**body)
            return self._json(200, {"state": snapshot()})
        if not text or len(text) > MAX_TEXT:
            return self._json(400, {"error": f"a message is 1 to {MAX_TEXT} characters"})
        if cid is not None and not (isinstance(cid, str) and 0 < len(cid) <= 64):
            return self._json(400, {"error": "bad cid"})
        about = body.get("about")
        if about is not None:  # a reply to a pair of screenshots: its task and step, from the pair
            pair = shots(about.get("task"), about.get("n")) if isinstance(about, dict) else None
            if not pair:
                return self._json(400, {"error": "no such screenshots"})
            about = {k: pair[k] for k in ("task", "n", "step", "round")}
        msg = send(text, "user", cid, about=about)
        self._json(200, {"message": msg, "listener": listener(), "state": snapshot()})


WILDCARD = ("0.0.0.0", "::")


def this_machine() -> set[str]:
    """The names and addresses other devices may reach this machine by (for a wildcard bind)."""
    name = socket.gethostname()
    found = {name, name.split(".")[0], socket.getfqdn()}
    try:
        found |= {a[4][0] for a in socket.getaddrinfo(name, None)}
    except OSError:
        pass
    return {n.lower() for n in found if n}


def plan(binds: list[str] | None = None, hosts: list[str] | None = None) -> dict:
    """Where to listen and which Host names to answer: {"binds", "hosts" (None: any), "names"
    (to show), "skipped"}. Default localhost; the user's chat.json or --bind/--host say more."""
    conf = settings()
    binds = list(binds or conf.get("bind") or ["127.0.0.1"])
    extra = [h.lower() for h in [*(conf.get("hosts") or []), *(hosts or [])]]
    allowed = {"localhost", "127.0.0.1", "::1"}
    out, names, skipped = [], [], []
    for b in binds:
        if b == "localhost":
            b = "127.0.0.1"
        if b == "tailscale":  # this machine's tailnet address, when there is one
            ts = tailscale()
            if not ts.get("ip"):
                skipped.append("tailscale (CLI not found: put `tailscale` on PATH)" if ts.get("missing")
                               else "tailscale (not running)")
                continue
            b = ts["ip"]
            allowed |= {n.lower() for n in ts["names"]}
            names += sorted(ts["names"], key=len, reverse=True)[:1]
        if b in WILDCARD:
            allowed |= this_machine()
        else:
            allowed.add(b)
        if b not in out:
            out.append(b)
    return {"binds": out or ["127.0.0.1"], "hosts": None if "*" in extra else allowed | set(extra),
            "names": names, "skipped": skipped}


class _Server6(ThreadingHTTPServer):
    address_family = socket.AF_INET6


def make_servers(port: int, binds: list[str] | None = None,
                 hosts: list[str] | None = None) -> tuple[list[ThreadingHTTPServer], dict]:
    """One server per address the plan names (localhost unless the user chose more)."""
    p = plan(binds, hosts)
    handler = type("ChatHandler", (Handler,), {
        "hosts": p["hosts"], "secret": token(), "hub": Hub(),
        "streams": threading.BoundedSemaphore(MAX_STREAMS)})
    servers = []
    for addr in p["binds"]:
        try:
            srv = (_Server6 if ":" in addr else ThreadingHTTPServer)((addr, port), handler)
        except OSError as e:  # one line, not a traceback (the 2026-11-16 audit)
            for s in servers:
                s.server_close()
            why = ("the port is taken: another wb chat serve (systemctl --user status wb-chat) or "
                   "another program; --port picks another" if e.errno == errno.EADDRINUSE else
                   "that address isn't on this machine (chat.json's bind, or --bind)"
                   if e.errno == errno.EADDRNOTAVAIL else e.strerror or str(e))
            raise RuntimeError(f"can't listen on {addr}:{port}: {why}") from None
        srv.daemon_threads = True
        servers.append(srv)
    return servers, p


def serve(port: int, binds: list[str] | None, hosts: list[str] | None = None, say=print) -> None:
    servers, p = make_servers(port, binds, hosts)
    for srv in servers[1:]:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    say(f"web chat on {', '.join(f'{s.server_address[0]}:{port}' for s in servers)}"
        + (f" (http://{p['names'][0]}:{port} on the tailnet)" if p["names"] else "")
        + (f"; skipped {', '.join(p['skipped'])}" if p["skipped"] else ""))
    if any(b in WILDCARD for b in p["binds"]):
        say("every interface: anyone who can reach this machine and has the link can use it")
    if p["hosts"] is None:
        say("answering to any host name (hosts: *): the token is the only lock")
    if p["skipped"]:  # an address not up yet (tailscaled after login): serve what is, and once
        def replan():  # it comes up, end so the service manager starts the server with it
            while True:
                time.sleep(REPLAN_EVERY)
                if len(plan(binds, hosts)["binds"]) > len(p["binds"]):
                    say("an address this server skipped is up now: restarting to bind it")
                    os._exit(75)
        threading.Thread(target=replan, daemon=True).start()

    def restart():  # the same command again, in this process: systemd and launchd see no exit
        say("the web chat's code changed: restarting with it")
        os.execv(sys.executable, [sys.executable, *sys.orig_argv[1:]])
    threading.Thread(target=watch_code, args=(restart,), daemon=True).start()
    threading.Thread(target=Pusher(servers[0].RequestHandlerClass.hub).run, daemon=True).start()
    servers[0].serve_forever()


def code_stamp() -> tuple:
    return tuple(sorted((f.name, f.stat().st_mtime_ns, f.stat().st_size)
                        for f in Path(__file__).parent.glob("*.py")))


def watch_code(restart, every: float = RELOAD_EVERY) -> None:
    """Restart once the package's code has changed and settled (a pull writes files one by one):
    a server left running across an update served the new page from old code for days, and no
    doc's `systemctl --user restart wb-chat` was ever run (2026-11-18)."""
    first = prev = code_stamp()
    while True:
        time.sleep(every)
        try:
            now = code_stamp()
        except OSError:  # a file mid-replace
            continue
        if now != first and now == prev:
            return restart()
        prev = now


def url(port: int) -> str:
    """The one link to open: localhost, the token in the fragment. Reaching it from another
    device (Tailscale, a tunnel) is the user's own setup: they change the host in the link."""
    return f"http://localhost:{port}/#token={token()}"
