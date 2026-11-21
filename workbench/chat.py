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
import http.client
import json
import mmap
import os
import re
import secrets
import select
import shutil
import socket
import socketserver
import stat
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
AUDIO_MAX = 64 << 20    # a take copied for the page up to this (5 minutes of 48 kHz stereo WAV)
LISTEN_MAX = 8          # takes in one shortlist
LISTEN_KEPT = 20        # shortlists kept (chat/audio/<batch>/): an older one's player says it's gone
# what a browser's <audio> plays, by the clip's suffix
AUDIO_TYPES = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".ogg": "audio/ogg", ".opus": "audio/ogg",
               ".flac": "audio/flac", ".m4a": "audio/mp4"}
BATCH_RE = re.compile(r"^[0-9]{13}-[0-9a-f]{6}$")
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


class ConfigError(RuntimeError):
    """chat.json says something the server can't follow: no retry fixes it (`wb chat serve` exits 78)."""


def settings() -> dict:
    """The user's choices for the server: {"bind": [...], "hosts": [...]}; {} for the defaults. A
    string is a one-item list: iterated as a list, "0.0.0.0" became the binds '0' and '.' (#534)."""
    conf = store.read_json(config_path(), {}) or {}
    if not isinstance(conf, dict):
        raise ConfigError(f"{config_path()} isn't a JSON object: {{\"bind\": [...], \"hosts\": [...]}}")
    for key in ("bind", "hosts"):
        val = conf.get(key)
        if isinstance(val, str):
            conf[key] = [val]
        elif val is not None and not (isinstance(val, list) and all(isinstance(v, str) for v in val)):
            raise ConfigError(f"{config_path()}: {key} is a list of strings, not {json.dumps(val)[:60]}")
    return conf


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
    return {"ip": ips[0] if ips else None, "names": names, "dns": dns.lower()}


_tailnet = {"at": float("-inf"), "name": ""}
_tailnet_lock = threading.Lock()


def tailnet_name() -> str:
    """This machine's MagicDNS name (box.tailnet.ts.net), "" when Tailscale isn't up; asked at
    most every REPLAN_EVERY seconds, so a request under an unknown name costs no subprocess.
    `tailscale serve --bg 1337`, the https the gear advises, passes that name through as the Host,
    and a chat bound to localhost answered every request 403 "unknown host" (#448). Only the full
    name: it is the one serve's certificate has, and no other site's DNS can answer for it."""
    with _tailnet_lock:
        if time.monotonic() - _tailnet["at"] >= REPLAN_EVERY:
            _tailnet.update(at=time.monotonic(), name=tailscale().get("dns") or "")
        return _tailnet["name"]


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
            # a capture that failed shows no image, even when it wrote one (#268; verify.shot_ok)
            **{s: {"rev": (x := m.get(s) or {}).get("rev"), "image": bool(x.get("ok", x.get("image"))),
                   **({"exit": x["exit"]} if x.get("exit") not in (None, 0) else {})}
               for s in ("base", "candidate")}}


def shot_png(task: str, n: int, side: str) -> bytes | None:
    """One side's screenshot, never through a link: it is wb shot's own copy."""
    if side not in ("base", "candidate") or not (s := shots(task, n)) or not s[side]["image"]:
        return None
    p = store.Task(task).dir / "shots" / str(n) / f"{side}.png"
    try:
        fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as f:
        return f.read(SHOT_MAX + 1)[:SHOT_MAX]


def _plain(path: Path, most: int) -> bytes:
    """A file's bytes, never through a link (O_NOFOLLOW) and only a regular file, up to ``most``:
    OSError otherwise, ValueError past ``most``."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as f:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "not a regular file")
        data = f.read(most + 1)
    if len(data) > most:
        raise ValueError(f"over {most >> 20} MiB")
    return data


def _batch_dir(batch) -> Path | None:
    if not (isinstance(batch, str) and BATCH_RE.match(batch)):
        return None
    d = chat_dir() / "audio" / batch
    return d if d.is_dir() and not d.is_symlink() else None


def _manifest(batch) -> dict | None:
    """chat/audio/<batch>/audio.json: {batch, ts, clips: [{n, name, path, file, type, line,
    engine?, seed?, voice?}]}; None when there is no such shortlist."""
    d = _batch_dir(batch)
    m = store.read_json(d / "audio.json", None) if d else None
    return m if isinstance(m, dict) and isinstance(m.get("clips"), list) else None


def audio_batch(batch) -> dict | None:
    """What the page shows of a shortlist: {batch, clips: [{n, name, line}]}."""
    m = _manifest(batch)
    return m and {"batch": m["batch"],
                  "clips": [{k: c.get(k) for k in ("n", "name", "line")} for c in m["clips"]]}


def audio_take(batch, n) -> tuple[bytes, str] | None:
    """One take of a shortlist and its media type, never through a link: it is wb's own copy."""
    m = _manifest(batch)
    c = next((c for c in m["clips"] if c.get("n") == n), None) if m and isinstance(n, int) else None
    if not c or c.get("type") not in AUDIO_TYPES.values():
        return None
    try:
        return _plain(chat_dir() / "audio" / m["batch"] / c["file"], AUDIO_MAX), c["type"]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def listen(clips: list, text: str = "") -> dict:
    """Claude's shortlist of `wb audio` takes for the user's ears (#397): Claude can't hear, so a
    voice's character and a song's feel are the user's to pick. Each clip is copied into
    chat/audio/<batch>/ (so the page plays wb's own copy, whatever happens to the original) with
    its check line from the `.json` sidecar beside it, and sent as a message the page shows with
    a player and a Keep button. A link, a missing clip or one the page can't play is refused,
    and nothing is sent. Returns the message."""
    from . import audio
    if not 1 <= len(clips) <= LISTEN_MAX:
        raise ValueError(f"a shortlist is 1 to at most {LISTEN_MAX} takes")
    taken = []
    for c in map(Path, clips):
        c = c if c.is_absolute() else Path.cwd() / c
        kind = AUDIO_TYPES.get(c.suffix.lower())
        if not kind:
            raise ValueError(f"{c}: not a clip the page can play ({', '.join(AUDIO_TYPES)})")
        if c.is_symlink():
            raise ValueError(f"{c} is a link: name the clip itself")
        try:
            data = _plain(c, AUDIO_MAX)
        except FileNotFoundError:
            raise ValueError(f"no such clip {c}") from None
        except (OSError, ValueError) as e:
            raise ValueError(f"{c}: {e.strerror if isinstance(e, OSError) else e}") from None
        try:
            side = json.loads(_plain(c.with_suffix(".json"), 1 << 20))
        except (OSError, ValueError):
            side = {}
        side = side if isinstance(side, dict) else {}
        try:
            line = audio.summary(side, side.get("seconds")) if {"duration", "unreadable"} & set(side) else ""
        except (KeyError, TypeError, ValueError):
            line = ""
        taken.append((c, kind, data, line, {k: side[k] for k in ("engine", "seed", "voice") if side.get(k) not in (None, "")}))
    root = chat_dir() / "audio"
    root.mkdir(exist_ok=True)
    batch = f"{int(time.time() * 1000)}-{secrets.token_hex(3)}"
    d = root / batch
    d.mkdir()
    rows = []
    try:
        for n, (c, kind, data, line, made) in enumerate(taken, 1):
            (d / f"{n}{c.suffix.lower()}").write_bytes(data)
            rows.append({"n": n, "name": c.name, "path": str(c), "file": f"{n}{c.suffix.lower()}",
                         "type": kind, "line": line, **made})
        store.write_json(d / "audio.json", {"batch": batch, "ts": store.now(), "clips": rows})
    except OSError:
        shutil.rmtree(d, ignore_errors=True)
        raise
    for old in sorted(p.name for p in root.iterdir() if BATCH_RE.match(p.name))[:-LISTEN_KEPT]:
        shutil.rmtree(root / old, ignore_errors=True)
    n = len(rows)
    return send(text or f"{n} take(s) to listen to. I can't hear them: tap Keep on the one you want.",
                "claude", audio=audio_batch(batch))


def send(text: str, frm: str, cid: str | None = None, **extra) -> dict:
    """Append a message. A client id (cid) already in the thread returns that message instead,
    so a retried send never lands twice. ``extra``: a pair of screenshots (``shots``), or what a
    reply is about (``about``)."""
    with _send_lock:
        if cid and (r := _by_cid(cid)):
            if r.get("from") == "user" and r.get("id") not in open_work()["messages"]:
                # a send cut off between the append and the index (a restart mid-request): the
                # retry found the id and said sent, and the listener never had it (#446)
                _open_update(lambda w: _reopen(w, r))
            return r
        row = {"id": f"{int(time.time() * 1000)}-{secrets.token_hex(3)}", "ts": store.now(),
               "from": frm, "text": text, **({"cid": cid} if cid else {}),
               **{k: v for k, v in extra.items() if v}}
        store.append_jsonl(chat_dir() / "messages.jsonl", row)
        if frm == "user":
            _open_update(lambda w: w["messages"].__setitem__(row["id"], row))
        return row


CIDS_KEPT = 5000  # client ids remembered: an outbox item older than that many messages is no retry
_cids: dict = {"key": None, "at": 0, "rows": {}}  # what _by_cid has read of messages.jsonl


def _by_cid(cid: str) -> dict | None:
    """The message a client sent with id ``cid``, or None. Read on from where the last call
    stopped (the file is only appended to; another file or a shorter one is read afresh), and the
    newest CIDS_KEPT kept: the last 500 rows were checked, and an outbox item from a tab offline
    longer than that landed twice (#199, the 2026-11-19 audit)."""
    p = chat_dir() / "messages.jsonl"
    try:
        st = p.stat()
    except FileNotFoundError:
        return None
    key = (str(p), st.st_dev, st.st_ino)
    if _cids["key"] != key or st.st_size < _cids["at"]:
        _cids.update(key=key, at=0, rows={})
    with open(p, "rb") as f:
        f.seek(_cids["at"])
        data = f.read()
    end = data.rfind(b"\n") + 1  # a row still being written waits for its newline
    rows = _cids["rows"]
    for line in data[:end].splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict) and isinstance(r.get("cid"), str):
            rows.pop(r["cid"], None)
            rows[r["cid"]] = r
    while len(rows) > CIDS_KEPT:
        del rows[next(iter(rows))]
    _cids["at"] += end
    return rows.get(cid)


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
    _open_update(lambda w: w["messages"].pop(msg_id, None))


# Work still waiting, kept apart from the files the page reads by their tails: an unread message
# behind 540 long replies, and an open question behind 120 plan cards, fell out of the 4 MiB
# tails, so `wb chat pending` lost the one and the other could no longer be answered (#329, the
# audit of 1.4). chat/open.json, {messages: {id: row}, asks: {id: row}}: the user's messages
# without a receipt and the asks without an answer, kept under open.lock by send and ack, ask and
# answer, and built from the whole files once where it is missing (an older wb's state).
def _open_path() -> Path:
    return chat_dir() / "open.json"


def _open_rebuild() -> dict:
    got = {r.get("id") for r in store.read_jsonl(chat_dir() / "receipts.jsonl")[0]}
    msgs = {r["id"]: r for r in store.read_jsonl(chat_dir() / "messages.jsonl")[0]
            if r.get("from") == "user" and r.get("id") and r["id"] not in got}
    open_asks: dict = {}
    answered: dict = {}
    for r in store.read_jsonl(chat_dir() / "asks.jsonl")[0]:
        if "answer" in r:
            if r.get("answer") in ANSWERS and (a := open_asks.pop(r.get("id"), None)):
                _settle(answered, a, r)
        elif "tool" in r and r.get("id"):
            open_asks[r["id"]] = {"kind": "permission", **r}
    return {"messages": msgs, "asks": open_asks, "answered": answered}


def _received(msg_id: str) -> bool:
    """Whether the message has a receipt: read whole, but only when a retried send finds its
    message missing from the index (rare), so the rows are parsed only where the id appears."""
    try:
        data = (chat_dir() / "receipts.jsonl").read_bytes()
    except FileNotFoundError:
        return False
    key = msg_id.encode()
    return any(r.get("id") == msg_id for line in data.splitlines() if key in line
               for r in store._rows(line) if isinstance(r, dict))


def _reopen(work: dict, row: dict) -> None:
    """Under open.lock: index a message the index lacks, unless Claude already has it (an ack
    writes its receipt before it takes the lock, so a receipt not seen here is popped after)."""
    if row["id"] not in work["messages"] and not _received(row["id"]):
        work["messages"][row["id"]] = row


def _open_update(change) -> dict:
    with store.locked(chat_dir() / "open.lock"):
        cur = store.read_json(_open_path(), None)
        if not (isinstance(cur, dict) and isinstance(cur.get("messages"), dict)
                and isinstance(cur.get("asks"), dict)):
            cur = _open_rebuild()
        cur.setdefault("answered", {})  # an older wb's index has none
        change(cur)
        store.write_json(_open_path(), cur)
        return cur


def open_work() -> dict:
    """What still waits: {messages, asks}, each {id: row}."""
    cur = store.read_json(_open_path(), None)
    if isinstance(cur, dict) and isinstance(cur.get("messages"), dict) and isinstance(cur.get("asks"), dict):
        return cur
    return _open_update(lambda w: None)


THREAD_BYTES = 4 << 20  # the last 200 messages fit with room to spare (a reply is a few KB)


def thread(limit: int = 200) -> list[dict]:
    """The latest messages, each of the user's with its receipt (or None: not read yet)."""
    # bounded reads: the whole history was parsed on every poll (the 2026-11-19 audit: 56 ms at
    # 20,000 rows); a receipt comes after its message, so the newest ones cover the thread
    rows = store.read_jsonl_tail(chat_dir() / "messages.jsonl", THREAD_BYTES)[-limit:]
    receipts = {r["id"]: r for r in store.read_jsonl_tail(chat_dir() / "receipts.jsonl", THREAD_BYTES)}
    return [{**r, "received": receipts.get(r["id"])} if r["from"] == "user" else r for r in rows]


# Work still waiting is kept apart from what the page shows: an open question vanished from the
# page's list behind 20 newer answered ones, and `wb chat pending` lost an unread message behind
# 200 newer replies (#265, the audit of 1.2). Past these ages one is marked `stale`: the mod waits
# 30 minutes on an ask, and a message a day old is no longer news.
ASK_STALE = 3600
PENDING_STALE = 86400


def pending() -> list[dict]:
    """Every message of the user's without a receipt, however far back (open_work, #329)."""
    now = store.now()
    rows = sorted(open_work()["messages"].values(), key=lambda r: (r.get("ts", 0), r.get("id", "")))
    return [{**r, "received": None, **({"stale": True} if now - r.get("ts", 0) > PENDING_STALE else {})}
            for r in rows]


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
DETAIL_MAX = 40_000   # bytes of JSON as stored (UTF-8): a plan or four questions with previews
CUT_NOTE = " (cut: the rest is in the terminal)"
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
        row["detail"] = fit_detail(detail)
    store.append_jsonl(chat_dir() / "asks.jsonl", row)
    _open_update(lambda w: w["asks"].__setitem__(row["id"], row))
    if kind == "permission" and prefs()["away"] == "allow":
        return answer(row["id"], "allow", "setting")
    return {**row, "answer": None}


def _stored(d) -> int:
    return len(json.dumps(d, ensure_ascii=False).encode())


def fit_detail(detail: dict) -> dict:
    """``detail`` within DETAIL_MAX as stored: a plan, else the questions' previews, else their
    options' descriptions, cut (each to one length, the longest first) and marked CUT_NOTE.
    Measured as json.dumps' ASCII escapes, a 7,700-character Japanese plan was six bytes a
    character and refused, and the dialog sat in the terminal with the phone told nothing (#535).
    ValueError when even the questions themselves don't fit."""
    if _stored(detail) <= DETAIL_MAX:
        return detail
    d = json.loads(json.dumps(detail))
    opts = [o for q in d.get("questions") or [] if isinstance(q, dict)
            for o in q.get("options") or [] if isinstance(o, dict)]
    for spots in ([(d, "plan")], [(o, "preview") for o in opts], [(o, "description") for o in opts]):
        spots = [(h, k, h[k]) for h, k in spots if isinstance(h.get(k), str) and h[k]]
        if not spots:
            continue
        def cap(n):
            for h, k, text in spots:
                h[k] = text if len(text) <= n else text[:n].rstrip() + CUT_NOTE
        lo, hi = 0, max(len(t) for _, _, t in spots)
        while lo < hi:  # the longest length every text may keep
            mid = (lo + hi + 1) // 2
            cap(mid)
            lo, hi = (mid, hi) if _stored(d) <= DETAIL_MAX else (lo, mid - 1)
        cap(lo)
        if _stored(d) <= DETAIL_MAX:
            return d
    raise ValueError("detail too long")


def asks(limit: int = 20) -> list[dict]:
    """The latest asks, and every open one older than those, each with its answer (None while
    open; `stale` once open past ASK_STALE), `answered`, `by`, and the answer's `note` and
    `answers` when it has them."""
    seen: dict[str, dict] = {}
    for r in store.read_jsonl_tail(chat_dir() / "asks.jsonl", THREAD_BYTES):  # an answer follows its ask
        a = seen.get(r.get("id"))
        if "answer" in r:
            if a and a["answer"] is None and r["answer"] in ANSWERS:
                a.update(answer=r["answer"], answered=r["ts"], by=r.get("by"))
                a.update({k: r[k] for k in ("note", "answers") if k in r})
        elif "tool" in r and r.get("id"):
            seen[r["id"]] = {"kind": "permission", **r, "answer": None}
    # an open ask older than the tail is still open (#329)
    older = sorted(({**a, "answer": None} for i, a in open_work()["asks"].items() if i not in seen),
                   key=lambda a: (a.get("ts", 0), a.get("id", "")))
    rows, now = list(seen.values()), store.now()
    out = older + [a for a in rows[:max(0, len(rows) - limit)] if a["answer"] is None] + rows[-limit:]
    return [{**a, "stale": True} if a["answer"] is None and now - a["ts"] > ASK_STALE else a
            for a in out]


def find_ask(ask_id: str) -> dict | None:
    """The ask as it stands, answered or not. One answered after its row left the asks' tail is
    in open.json's `answered`: answering took it out of the open index, and its waiter then got
    "no such ask" for an answer just given (#447)."""
    return (next((a for a in reversed(asks(10_000)) if a["id"] == ask_id), None)
            or (open_work().get("answered") or {}).get(ask_id))


ANSWER_KEPT = 86400  # seconds an answer stays in open.json for its waiter (the mod waits 30 minutes)


def _settle(answered: dict, ask_row: dict, ans: dict) -> None:
    """Keep the ask with its answer (the first one stands), and drop those kept past ANSWER_KEPT."""
    answered.setdefault(ask_row["id"], {"kind": "permission", **ask_row, "answer": ans["answer"],
                                        "answered": ans["ts"], "by": ans.get("by"),
                                        **{k: ans[k] for k in ("note", "answers") if k in ans}})
    cut = store.now() - ANSWER_KEPT
    for k in [k for k, a in answered.items() if a.get("answered", 0) < cut]:
        del answered[k]


def _answered(ask_row: dict, ans: dict) -> None:
    def change(w):
        w["asks"].pop(ask_row["id"], None)
        _settle(w["answered"], ask_row, ans)
    _open_update(change)


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
        _answered({k: v for k, v in cur.items() if k not in ("answer", "stale")}, row)
        cur = find_ask(ask_id) or {**cur, "answer": verdict, "answered": row["ts"], "by": by}
    return cur


def session_plan(session: str | None, plan_file: str | None = None) -> str:
    """The plan this session wrote: the file ExitPlanMode's input names, else the last plan file
    its transcript names, else "". The newest file in the plans directory was any project's: with
    two planning, one session's card showed the other's plan, and the user sent revisions against
    it (#253, the audit of 1.2)."""
    from . import context, spend  # context imports this module
    d = (context.claude_home().expanduser() / "plans").resolve()
    f = None
    if plan_file:
        f = Path(plan_file).expanduser()
    elif session and (transcript := spend.transcript_paths(session)[0]):
        pat = re.compile(re.escape(str(d).encode()) + rb"/[A-Za-z0-9._-]+\.md")
        try:
            with open(transcript, "rb") as fh, mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                f = next((Path(m.group().decode()) for m in reversed(list(pat.finditer(mm)))), None)
        except (OSError, ValueError):  # gone, or empty (mmap refuses an empty file)
            f = None
    try:
        if f is None or f.resolve().parent != d or f.suffix != ".md" or not f.resolve().is_file():
            return ""
        return f.read_text(errors="replace")[:DETAIL_MAX + 1]  # ask cuts it, marked (#535)
    except OSError:
        return ""


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


MARK = "X-Workbench-Chat"  # on every response: how wb chat on knows its own server


def identity(secret: str) -> str:
    """MARK's value: an HMAC of a fixed string keyed by the server's token, so it names whose chat
    this is and gives away nothing of the token. A bare "1" said only "a wb chat": on a shared
    machine another account's `wb chat on` took the first's server for its own and showed a link
    its token got 401 on (#554)."""
    return hmac.new(secret.encode(), b"workbench web chat", "sha256").hexdigest()[:32]


def who_answers(port: int) -> str | None:
    """Who answers on the port at an address this machine's plan binds: "workbench" (this
    account's chat: its responses carry MARK keyed by this token), "other" (another program,
    another account's chat, or one holding a token since replaced), or None. A bare connect
    counted any program there as the chat, and `wb chat on` showed a link to it (#200, the
    2026-11-19 audit)."""
    found, mine = None, identity(token())
    for addr in plan()["binds"]:
        conn = http.client.HTTPConnection(_probe(addr), port, timeout=2)
        try:
            conn.request("GET", "/manifest.webmanifest", headers={"Host": "localhost"})
            if hmac.compare_digest(conn.getresponse().getheader(MARK) or "", mine):
                return "workbench"
            found = "other"
        except (OSError, http.client.HTTPException):
            found = found or (_connects(addr, port) and "other") or None
        finally:
            conn.close()
    return found


def _probe(addr: str) -> str:
    """Where to reach a bound address from here: a wildcard by its family's loopback. `::` was
    probed at 127.0.0.1, which an IPv6-only socket (bindv6only=1) never answers, so a healthy
    chat looked down and `wb chat on` started a second that failed on the port (#450)."""
    return {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(addr, addr)


def _connects(addr: str, port: int) -> bool:
    try:
        with socket.create_connection((_probe(addr), port), timeout=1):
            return True
    except OSError:
        return False


def running(port: int) -> bool:
    """Whether the web chat's own server answers on the port."""
    return who_answers(port) == "workbench"


START_WAIT = 10  # seconds wb chat on waits for the server it started to answer


def wait_up(port: int, pid: int, limit: float = START_WAIT) -> bool:
    """Whether the server started as ``pid`` answers within ``limit`` seconds: it may exit at once
    (the port taken, an address not here), and a fixed half second said started either way."""
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if running(port):
            return True
        try:
            if os.waitpid(pid, os.WNOHANG)[0]:
                return False  # it exited: serve.log says why
        except ChildProcessError:  # not this process's child
            if not _alive(pid):
                return False
        time.sleep(0.1)
    return False


def start(port: int) -> int:
    """`wb chat serve` detached, its output in chat/serve.log, its pid in chat/serve.pid: for a
    machine without the systemd unit, so turning the chat on is enough to reach it."""
    from . import cli  # only here: the server never starts one
    root = Path(__file__).resolve().parent.parent
    with open(chat_dir() / "serve.log", "a") as log:  # outlives wb, and a logout (#531)
        proc = cli.detach([sys.executable, "-X", "utf8", str(root / "bin" / "wb"), "chat", "serve",
                           "--port", str(port)], cwd=root, stdout=log, stderr=log)
    store.write_json(chat_dir() / "serve.pid", {"pid": proc.pid, "port": port, "start": _started(proc.pid)})
    return proc.pid


def _unit_keeps_it() -> bool:
    """Whether wb-chat.service is enabled, so systemd brings the chat back itself."""
    if not shutil.which("systemctl"):
        return False
    try:
        return subprocess.run(["systemctl", "--user", "is-enabled", "--quiet", "wb-chat"],
                              capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def revive() -> int | None:
    """Start again the server `wb chat on` started (chat/serve.pid), when the switch is still on and
    it is gone and nothing answers on its port; the port it started on, else None. Nothing brought
    it back after a reboot or a crash, while the switch said on and the pane showed the link, so
    the phone said "Can't reach the computer" for good (#449). `wb chat url` calls it, which the
    mod runs at a session's start and every ten minutes while the chat is on. Never for a chat wb
    didn't start, nor where wb-chat.service is enabled (systemd's to restart)."""
    from . import models  # as in stop_started
    rec = store.read_json(chat_dir() / "serve.pid", None) or {}
    pid, port = rec.get("pid"), rec.get("port")
    if not is_on() or not isinstance(port, int) or not pid:
        return None
    if _alive(pid) and models.runs(int(pid), "chat", "serve"):  # up, or still starting
        return None
    if who_answers(port) is not None or _unit_keeps_it():
        return None
    start(port)
    return port


def stop_started() -> bool:
    """Stop the server `start` made, if it still runs; never one wb didn't start (systemd's)."""
    from . import models  # only here: chat is imported by the server, which needs none of it
    pid = (st := store.read_json(chat_dir() / "serve.pid", None) or {}).get("pid")
    (chat_dir() / "serve.pid").unlink(missing_ok=True)
    if not pid or not _alive(pid, st.get("start")) or not models.runs(int(pid), "chat", "serve"):
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


def _started(pid: int) -> str | None:
    from . import models
    return models._started(pid)


def _alive(pid, start=None) -> bool:
    """models._alive: a pid of another user's (EPERM) is gone, and one that started at another
    time than recorded was recycled. This said both were alive: a root daemon under listener.json's
    old pid read as a session listening (#532)."""
    from . import models  # only here: chat is imported by the server, which needs little of it
    try:
        return models._alive(int(pid), start if isinstance(start, str) else None)
    except (ValueError, TypeError):
        return False


def listener() -> dict | None:
    """The session listening now. Its `wb events` process (pid) lives exactly as long as the
    session; a listener.json without a pid falls back to its age."""
    st = store.read_json(chat_dir() / "listener.json", None)
    if not st:
        return None
    if st.get("pid"):
        return st if _alive(st["pid"], st.get("start")) else None
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
        self.send_header(MARK, identity(self.secret))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' blob:; "
                         "media-src blob:; "
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
        if (self.hosts is not None and host not in self.hosts and not (host and host.split("%")[0] == self._local())
                and not (host and host == tailnet_name())):
            self._json(403, {"error": f"unknown host {host[:100]}: add it to chat.json's hosts"})
            return False
        return True

    def _local(self) -> str:
        """The address this connection came in on: on a wildcard bind, the LAN or tailnet IP a
        phone typed. The names were worked out once at startup from the host name, which resolves
        to 127.0.1.1 on Debian, Ubuntu and WSL, and missed an address gained later (a late Wi-Fi,
        a VPN), so those IPs got 403 (#533). An address can't be rebound: DNS rebinding needs a
        name, and the socket's own address is one this server bound."""
        try:
            addr = self.connection.getsockname()[0]
        except (OSError, AttributeError, IndexError):
            return ""
        return addr.lower().removeprefix("::ffff:").split("%")[0]

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
        if path == "/api/audio":  # one take of Claude's shortlist (#397), behind the token too
            if self._authed():
                q = parse_qs(urlsplit(self.path).query)
                n = (q.get("n") or [""])[0]
                got = audio_take((q.get("batch") or [""])[0], int(n) if n.isdigit() else 0)
                if got is None:
                    self._json(404, {"error": "no such take"})
                else:
                    self._send(200, *got)
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
        if isinstance(about, dict) and "audio" in about:  # the take the user keeps, from its batch
            m, n = _manifest(about.get("audio")), about.get("n")
            take = next((c for c in m["clips"] if c.get("n") == n), None) if m and type(n) is int else None
            if not take:
                return self._json(400, {"error": "no such take"})
            about = {"audio": m["batch"], "n": n, "name": take.get("name"), "path": take.get("path")}
        elif about is not None:  # a reply to a pair of screenshots: its task and step, from the pair
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
    # A wildcard covers the specific addresses of its family, and on Linux (bindv6only=0) `::`
    # takes IPv4 too: each further socket on the port failed EADDRINUSE, so ["localhost",
    # "0.0.0.0"] or ["0.0.0.0", "::"] never started, blamed on "another wb chat serve" (#534).
    # Their names stay answered; `::` beside any IPv4 bind is made IPv6-only (IPV6_V6ONLY).
    if "0.0.0.0" in out:
        out = [b for b in out if b == "0.0.0.0" or ":" in b]
    if "::" in out:
        out = [b for b in out if b == "::" or ":" not in b]
    return {"binds": out or ["127.0.0.1"], "hosts": None if "*" in extra else allowed | set(extra),
            "names": names, "skipped": skipped,
            "v6only": "::" in out and any(":" not in b for b in out)}


class _Server(ThreadingHTTPServer):
    v6only = False  # set for `::` beside an IPv4 bind, so the two can share the port (#534)

    def server_bind(self):
        if self.v6only:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        # HTTPServer's own also looks the address's host name up (socket.getfqdn), which nothing
        # here reads: on macOS that lookup stalled the start past wb chat on's wait (#236)
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class _Server6(_Server):
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
            kind = (type("_Server6Only", (_Server6,), {"v6only": True}) if addr == "::" and p["v6only"]
                    else _Server6 if ":" in addr else _Server)
            srv = kind((addr, port), handler)
        except OSError as e:  # one line, not a traceback (the 2026-11-16 audit)
            for s in servers:
                s.server_close()
            why = (("the port is taken: by another address in the bind list (chat.json's bind, or "
                    "--bind) or another program" if servers else
                    "the port is taken: another wb chat serve (systemctl --user status wb-chat) or "
                    "another program") + "; --port picks another" if e.errno == errno.EADDRINUSE else
                   "that address isn't on this machine (chat.json's bind, or --bind)"
                   if e.errno == errno.EADDRNOTAVAIL else e.strerror or str(e))
            raise RuntimeError(f"can't listen on {addr}:{port}: {why}") from None
        srv.daemon_threads = True
        servers.append(srv)
    return servers, p


def serve(port: int, binds: list[str] | None, hosts: list[str] | None = None, say=print) -> None:
    servers, p = make_servers(port, binds, hosts)
    # the port it runs on, for `wb chat url` and the pane's link: they asked without one and got
    # the default, wrong for a server on the free port the CLI advises (#349, the audit of 1.4)
    store.write_json(chat_dir() / "port.json", {"port": servers[0].server_address[1] if servers
                                                else port, "pid": os.getpid(),
                                                "start": _started(os.getpid())})
    for srv in servers[1:]:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    say(f"web chat on {', '.join(f'{s.server_address[0]}:{port}' for s in servers)}"
        + (f" (http://{p['names'][0]}:{port} on the tailnet)" if p["names"] else "")
        + (f"; skipped {', '.join(p['skipped'])}" if p["skipped"] else ""))
    if any(b in WILDCARD for b in p["binds"]):
        say("every interface: anyone who can reach this machine and has the link can use it")
    if p["hosts"] is None:
        say("answering to any host name (hosts: *): the token is the only lock")
    def restart(why: str):  # the same command again, in this process: systemd and launchd see
        say(why)            # no exit, and a server wb chat on started has no supervisor to rerun it
        os.execv(sys.executable, [sys.executable, *sys.orig_argv[1:]])
    if p["skipped"] or "tailscale" in (binds or settings().get("bind") or []):
        # an address that comes and goes (tailscaled after login, a VPN): once the plan's set
        # changes, rebind. Only a longer list was noticed, so a tailscale-only chat on loopback
        # stayed there when the tailnet came up; and it exited 75, which ended a chat with no
        # service manager (#200, the 2026-11-19 audit)
        def replan():
            while True:
                time.sleep(REPLAN_EVERY)
                try:
                    now = set(plan(binds, hosts)["binds"])
                except ConfigError:  # chat.json broken while it runs: keep serving what it bound
                    continue
                if now != set(p["binds"]):
                    restart("the addresses to listen on changed: restarting to bind them")
        threading.Thread(target=replan, daemon=True).start()
    threading.Thread(target=watch_code, args=(
        lambda: restart("the web chat's code changed: restarting with it"),), daemon=True).start()
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


def port() -> int:
    """The port the running server recorded (chat/port.json), else the one `wb chat on` last
    started a server on (chat/serve.pid: gone after a reboot or a crash, it comes back there,
    #449), else PORT: so a command or the pane that names no port finds a server started on
    another (#349, the audit of 1.4)."""
    st = store.read_json(chat_dir() / "port.json", None) or {}
    if isinstance(st.get("port"), int) and st.get("pid") and _alive(st["pid"], st.get("start")):
        return st["port"]
    started = (store.read_json(chat_dir() / "serve.pid", None) or {}).get("port")
    return started if isinstance(started, int) else PORT


def url(port: int) -> str:
    """The one link to open: localhost, the token in the fragment. Reaching it from another
    device (Tailscale, a tunnel) is the user's own setup: they change the host in the link. A bind
    list without loopback or a wildcard gets its first address instead: localhost reached nothing
    there (#534)."""
    host = "localhost"
    binds = settings().get("bind") or []
    if binds and not any(b in ("localhost", "127.0.0.1", "::1", *WILDCARD) for b in binds):
        first = plan()["binds"][0]
        host = f"[{first}]" if ":" in first else first
    return f"http://{host}:{port}/#token={token()}"
