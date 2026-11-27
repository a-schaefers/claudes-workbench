"""The web chat: a side door to Claude for the user when they are away from the terminal.

`wb chat serve` serves a small page and a JSON API. Messages the user sends are appended to
chat/messages.jsonl; the mod of a Claude Code session in /workbench mode picks them up, records a
receipt (chat/receipts.jsonl) and submits each to Claude as a prompt. Claude answers with
`wb chat reply`, which the page shows. chat/listener.json is the session currently listening
(the mod rewrites it every poll), so the page can say whether anyone will read a message now.

What a message can do is what typing in the terminal can do, so the API wants a secret token
(~/.config/workbench/chat-token, made on first use; `wb chat url` prints a link carrying it in
the fragment, which never leaves the browser) and answers only to the host names it serves,
which stops other web pages and DNS rebinding. It listens on 127.0.0.1 only (#1616): another
device reaches it through a tunnel or proxy of the user's own pointed at localhost, whose name
~/.config/workbench/chat.json's `hosts` adds (`*` for any). The link it prints is always localhost.
"""
from __future__ import annotations

import errno
import hashlib
import hmac
import http.client
import json
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
import tempfile
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from . import savings, store, story, webpush
from .watch import Watcher

PORT = 8090  # the default (#1616, 2026-11-27: 1337 looked unprofessional, and 8080 is llama.cpp's,
             # llama-swap's, LocalAI's and MLX's own); `wb chat on <port>` keeps another (chosen_port)
BIND = "127.0.0.1"      # the one address it listens on (#1616, 2026-11-27)
LOCAL_NAMES = ("localhost", "127.0.0.1", "::1")  # Host names always answered
# Headers a proxy adds to what it forwards: behind one (a tunnel, Caddy, nginx) every peer is
# 127.0.0.1, so these tell another device's request from this machine's (#1616, 2026-11-27)
FORWARDED = ("X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto", "Forwarded", "X-Real-IP", "Via",
             "True-Client-IP", "Cf-Connecting-Ip", "Tailscale-User-Login")
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
NUDGE_AFTER = 0.05      # seconds after each event to its blank line, which hands WebKit its tail
TIMEOUT = 30            # seconds a connection may stall (more than PING_EVERY: streams write by then)
UNACKED_MS = 45_000     # a stream whose writes go unacknowledged this long is dead (Linux)
RELOAD_EVERY = 10       # seconds between looks at the server's own code
PUSH_DELAY = 10         # seconds news waits for an open page to show it before a push goes
PUSH_WINDOW = 900       # only news this recent is pushed, never a backlog
PUSH_RETRY = 30         # seconds before a device whose push failed for now is tried again
API_VERSION = 1         # GET /api/state's and /api/health's `api`: bumped by a breaking change (docs/chat-api.md)
AGENT_SENDS_PER_MIN = 30  # an agent token's POST /api/send per minute, then 429 with retry_after
WAIT_DEFAULT = 300      # GET /api/wait's timeout when none is given, in seconds
WAIT_MAX = 600          # and the most it may ask for
MAX_WAITS = 16          # /api/wait calls held at once (each holds a thread, as a stream does)
WAIT_LOOK = 1           # seconds between a held wait's looks at its client: one gone frees the slot
STATIC = Path(__file__).parent / "chat_static"  # served by name; its src/ (the TypeScript, #1323) never
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
    """The user's choices for the server: {"hosts": [...]}; {} for the defaults. A string is a
    one-item list: iterated as a list, "box.example.org" became the names 'b', 'o', ... (#534). An
    old `bind` is ignored: the server listens on 127.0.0.1 only (#1616, 2026-11-27)."""
    conf = store.read_json(config_path(), {}) or {}
    if not isinstance(conf, dict):
        raise ConfigError(f"{config_path()} isn't a JSON object: {{\"hosts\": [...]}}")
    val = conf.get("hosts")
    if isinstance(val, str):
        conf["hosts"] = [val]
    elif val is not None and not (isinstance(val, list) and all(isinstance(v, str) for v in val)):
        raise ConfigError(f"{config_path()}: hosts is a list of strings, not {json.dumps(val)[:60]}")
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


# ---- agent tokens (#1042) ----
# Other agents (another harness driving Claude through workbench) get tokens of their own: `wb chat
# token add <name>` prints one once and keeps only its sha256 in chat-agents.json, beside the
# user's token file (0600). An agent token reaches what a teammate needs (send, upload, read the
# state and the stream, wait for replies) and nothing else: AGENT_MAY is an allow-list, so an
# endpoint added later is the user's alone until it is named there (#1280: the deny-lists it
# replaced, AGENT_DENIED and AGENT_UNREAD, left every new endpoint open to agents by default).
# Its messages are stored `from: <name>, role: "agent"` and reach Claude as `[agent <name>] …`,
# never with the user's authority. Revoked, it is 401 at once (the file is read per request).
AGENT_NAME = re.compile(r"[a-z0-9-]{1,32}")
AGENT_RESERVED = ("user", "claude", "notice", "agent", "wb")  # `from` values the thread already uses
AGENT_PREFIX = "wba-"  # how an agent token looks, so a leaked one is recognised
# (method, path) an agent token may call; a path ending in "/" covers what starts with it
AGENT_MAY = {("GET", "/api/health"), ("GET", "/api/wait"), ("GET", "/api/state"), ("GET", "/api/stream"),
             ("GET", "/api/upload/"), ("PUT", "/api/upload/"), ("GET", "/api/file/"), ("POST", "/api/send")}
# Only the wording of a refusal, kept from the deny-lists for the paths they named ("agent tokens
# can't <this>"); any other path is "agent tokens can't use <path>". AGENT_MAY alone decides.
AGENT_CANT = {"/api/push": "change notifications", "/api/active": "mark the user away",
              "/api/seen": "mark what the user has seen", "/api/latest": "read the user's notifications",
              "/api/shot": "read screenshots", "/api/audio": "read audio takes",
              "/api/transcribe/": "transcribe the user's voice clips",  # #1203
              "/api/task": "read tasks"}  # a task's diff and report are the user's work (#1201)


def _covers(rule: str, path: str) -> bool:
    return path == rule or rule.endswith("/") and path.startswith(rule)


def agent_may(method: str, path: str) -> bool:
    """Whether an agent token may call ``method`` ``path`` (AGENT_MAY)."""
    return any(m == method and _covers(p, path) for m, p in AGENT_MAY)


def agent_refusal(path: str) -> str:
    """The 403's error for an agent token on a path outside AGENT_MAY."""
    what = next((v for k, v in AGENT_CANT.items() if _covers(k, path)), None)
    return f"agent tokens can't {what or 'use ' + path[:100]}"


def agents_path() -> Path:
    """chat-agents.json, beside the user's token file (so $WB_CHAT_TOKEN moves both)."""
    return token_path().with_name("chat-agents.json")


def agent_tokens() -> dict:
    """{name: {sha256, ts}}: the agents that have a token now; {} for none or an unreadable file."""
    got = store.read_json(agents_path(), {}) or {}
    agents = got.get("agents") if isinstance(got, dict) else None
    return {k: v for k, v in (agents or {}).items() if isinstance(v, dict) and isinstance(v.get("sha256"), str)
            and AGENT_NAME.fullmatch(k)} if isinstance(agents, dict) else {}


# A revoked token's sha256 is kept (the newest REVOKED_KEPT), so its next request is told it was
# revoked (`token_revoked`) rather than that it is wrong (`bad_token`): an agent can then say
# "ask the user for a new token", not "check your setup" (#1138). Only hashes, as for live ones.
REVOKED_KEPT = 200


def _revoked_hashes() -> dict:
    """{sha256: when revoked} of the agent tokens revoked; {} for none or an unreadable file."""
    got = store.read_json(agents_path(), {}) or {}
    r = got.get("revoked") if isinstance(got, dict) else None
    return {k: v for k, v in r.items() if isinstance(k, str) and isinstance(v, (int, float))} \
        if isinstance(r, dict) else {}


def _agents_write(agents: dict, revoked: dict | None = None) -> None:
    revoked = _revoked_hashes() if revoked is None else revoked
    keep = dict(sorted(revoked.items(), key=lambda kv: kv[1])[-REVOKED_KEPT:])
    store.write_json(agents_path(), {"agents": agents, **({"revoked": keep} if keep else {})}, mode=0o600)


def agent_add(name: str) -> str:
    """A new token for agent ``name``, returned once: only its sha256 is kept. ValueError for a
    bad or reserved name, or one that has a token already (revoke it first)."""
    if not (isinstance(name, str) and AGENT_NAME.fullmatch(name)):
        raise ValueError("an agent's name is 1 to 32 of a-z, 0-9 and -")
    if name in AGENT_RESERVED:
        raise ValueError(f"{name} is a name the thread uses already: pick another")
    with store.locked(agents_path().with_name("chat-agents.lock")):
        agents = agent_tokens()
        if name in agents:
            raise ValueError(f"{name} has a token already: wb chat token revoke {name} first")
        secret = AGENT_PREFIX + secrets.token_urlsafe(32)
        agents[name] = {"sha256": hashlib.sha256(secret.encode()).hexdigest(), "ts": store.now()}
        _agents_write(agents)
    return secret


def agent_revoke(name: str) -> bool:
    """Revoke ``name``'s token (False: it had none). Its next request is 401 `token_revoked`."""
    with store.locked(agents_path().with_name("chat-agents.lock")):
        agents = agent_tokens()
        if (rec := agents.pop(name, None)) is None:
            return False
        _agents_write(agents, {**_revoked_hashes(), rec["sha256"]: store.now()})
    return True


def token_revoked(secret: str) -> bool:
    """Whether ``secret`` is an agent token that was revoked (by its sha256, in constant time)."""
    if not secret.startswith(AGENT_PREFIX):
        return False
    digest = hashlib.sha256(secret.encode()).hexdigest()
    found = False
    for h in _revoked_hashes():
        found |= hmac.compare_digest(h, digest)
    return found


def agent_for(secret: str) -> str | None:
    """The agent whose token ``secret`` is, or None (compared in constant time, by its sha256)."""
    if not secret.startswith(AGENT_PREFIX):
        return None
    digest = hashlib.sha256(secret.encode()).hexdigest()
    found = None
    for name, rec in agent_tokens().items():
        if hmac.compare_digest(rec["sha256"], digest):
            found = name
    return found


class RateLimit:
    """At most ``per_min`` events per key in any 60 seconds: hit(key) is 0 when allowed (and
    counted), else the seconds until one is."""

    def __init__(self, per_min: int):
        self.per_min, self.seen, self.lock = per_min, {}, threading.Lock()

    def hit(self, key: str, now: float | None = None) -> int:
        now = time.monotonic() if now is None else now
        with self.lock:
            q = [t for t in self.seen.get(key, []) if now - t < 60]
            if len(q) >= self.per_min:
                self.seen[key] = q
                return max(1, int(60 - (now - q[0]) + 0.999))
            self.seen[key] = q + [now]
            return 0


def _for_user(r: dict) -> bool:
    """Whether a row of Claude's is news for the user: a reply only to agents is pushed to no phone."""
    return "user" in (r.get("to") or ["user"])


def _inbound(r: dict) -> bool:
    """A message to Claude: the user's, or an agent's (#1042)."""
    return r.get("from") == "user" or r.get("role") == "agent"


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
            # a capture that failed shows no image, even when it wrote one (#268; verify.shot_ok);
            # one dropped with its archived task (store.slim_archive) is `pruned` (#821)
            **{s: {"rev": (x := m.get(s) or {}).get("rev"),
                   "image": bool(x.get("ok", x.get("image"))) and not x.get("pruned"),
                   **({"pruned": True} if x.get("pruned") else {}),
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
    reply is about (``about``); an agent's message's ``role: "agent"`` (``frm`` its name), a reply's
    ``re`` and ``to`` (#1042)."""
    with _send_lock:
        if cid and (r := _by_cid(cid)) and r.get("from") != frm:
            # an agent can read the user's cids in the state: one sent again as its own got the
            # user's message back as its send, and a cut-off one re-indexed (#1042 review)
            raise ValueError("that cid is another sender's")
        if cid and r:
            if _inbound(r) and r.get("id") not in open_work()["messages"]:
                # a send cut off between the append and the index (a restart mid-request): the
                # retry found the id and said sent, and the listener never had it (#446)
                _open_update(lambda w: _reopen(w, r))
            return r
        if frm == "claude" and (pid := os.environ.get("CLAUDE_PID", "")).isdigit():
            # whose Claude Code wrote it, on every row of Claude's (a reply, `wb shot --chat`, a
            # listen batch): the mod's owed reply (#767) counts only its own session's rows and
            # those answering the message, as another session's report settled it (#1129); a
            # screenshot another session posted still did, stamped in reply() alone (the #1129
            # review, 2026-11-23)
            extra.setdefault("claude_pid", int(pid))
        if frm == "claude" and (by := writer_driver()):
            extra.setdefault("driver", by)  # which driver wrote it, for the agent it answers (#1366)
        row = {"id": f"{int(time.time() * 1000)}-{secrets.token_hex(3)}", "ts": store.now(),
               "from": frm, "text": text, **({"cid": cid} if cid else {}),
               **{k: v for k, v in extra.items() if v}}
        store.append_jsonl(chat_dir() / "messages.jsonl", row)
        if _inbound(row):
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
    """Claude has the message: read at once ("now", opening its turn or the user's into a running
    one), an agent's read into a turn it may not have opened ("into": delivered into a running
    one, #1175, or queued or submitted behind another prompt, #1212), or queued until its current
    step ends."""
    store.append_jsonl(chat_dir() / "receipts.jsonl",
                       {"id": msg_id, "ts": store.now(), "by": by, "how": how})
    _open_update(lambda w: w["messages"].pop(msg_id, None))


# Work still waiting, kept apart from the files the page reads by their tails: an unread message
# behind 540 long replies fell out of the 4 MiB tails, so `wb chat pending` lost it (#329, the
# audit of 1.4). chat/open.json, {messages: {id: row}}: the user's messages without a receipt,
# kept under open.lock by send and ack, and built from the whole file once where it is missing
# (an older wb's state). An older wb's `asks` and `answered` there are dropped: the chat no longer
# answers Claude Code's dialogs (#1350).
def _open_path() -> Path:
    return chat_dir() / "open.json"


def _open_rebuild() -> dict:
    got = {r.get("id") for r in store.read_jsonl(chat_dir() / "receipts.jsonl")[0]}
    msgs = {r["id"]: r for r in store.read_jsonl(chat_dir() / "messages.jsonl")[0]
            if _inbound(r) and r.get("id") and r["id"] not in got}
    return {"messages": msgs}


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
        if not (isinstance(cur, dict) and isinstance(cur.get("messages"), dict)):
            cur = _open_rebuild()
        for gone in ("asks", "answered"):  # an older wb's (#1350)
            cur.pop(gone, None)
        change(cur)
        store.write_json(_open_path(), cur)
        return cur


def open_work() -> dict:
    """What still waits: {messages: {id: row}}. A message past MESSAGE_EXPIRE leaves it here
    (`_retire`)."""
    cur = store.read_json(_open_path(), None)
    if not (isinstance(cur, dict) and isinstance(cur.get("messages"), dict)):
        cur = _open_update(lambda w: None)
    now = store.now()
    if any(isinstance(r, dict) and _expired(r, now) for r in cur["messages"].values()):
        cur = _open_update(lambda w: _retire(w, store.now()))
    return cur


def _retire(work: dict, now: float) -> None:
    """Under open.lock: take each message past MESSAGE_EXPIRE out of the index, with a receipt
    `{id, ts, by: "wb", how: "expired"}` (thread shows it expired, not read). Only an ack popped
    one, and an expired message is never acked: every new session claimed it again and toasted,
    `wb chat pending` listed it, and gc kept its uploads, for good (#741)."""
    for mid in [k for k, r in work["messages"].items() if isinstance(r, dict) and _expired(r, now)]:
        store.append_jsonl(chat_dir() / "receipts.jsonl",
                           {"id": mid, "ts": now, "by": "wb", "how": "expired"})
        work["messages"].pop(mid, None)


def _expired_receipt(msg_id: str) -> bool:
    """Whether the message was retired as expired (`_retire`): read only when a claim names a
    message the index no longer holds, so a mod that read the index before the retiring still
    gets the refusal, not the message (#741)."""
    try:
        data = (chat_dir() / "receipts.jsonl").read_bytes()
    except FileNotFoundError:
        return False
    key = msg_id.encode()
    return any(r.get("id") == msg_id and r.get("how") == "expired" for line in data.splitlines()
               if key in line for r in store._rows(line) if isinstance(r, dict))


THREAD_BYTES = 4 << 20  # the last 200 messages fit with room to spare (a reply is a few KB)


def thread(limit: int = 200, keep=None) -> list[dict]:
    """The latest messages, each of the user's with its receipt (or None: not read yet), and
    `expired` once unread past MESSAGE_EXPIRE: no session will be given it (#536). A notice whose
    dialog settled in the terminal is left out: the page's waiting line clears (#1350). ``keep``:
    only the rows it passes (an agent's view, #1042)."""
    # bounded reads: the whole history was parsed on every poll (the 2026-11-19 audit: 56 ms at
    # 20,000 rows); a receipt comes after its message, so the newest ones cover the thread
    rows = [r for r in store.read_jsonl_tail(chat_dir() / "messages.jsonl", THREAD_BYTES)
            if keep is None or keep(r)][-limit:]
    receipts = {r["id"]: r for r in store.read_jsonl_tail(chat_dir() / "receipts.jsonl", THREAD_BYTES)}
    now = store.now()
    out = []
    for r in rows:
        if _inbound(r):
            got = receipts.get(r["id"])
            gone = got.get("how") == "expired" if got else _expired(r, now)  # retired (#741)
            r = {**r, "received": None if gone else got, **({"expired": True} if gone else {})}
        elif r["from"] == "notice" and r["id"] in receipts:  # its dialog settled in the terminal
            continue
        out.append(r)
    return out


# Work still waiting is kept apart from what the page shows: `wb chat pending` lost an unread
# message behind 200 newer replies (#265, the audit of 1.2). A notice open past ASK_STALE no
# longer counts as Claude waiting on the user (a session that died with its dialog up never
# settled it). A message of the user's unread past MESSAGE_EXPIRE is `expired`:
# `wb chat claim` refuses it, so no session springs it on Claude, and the page says so and offers
# to send it again. The mod skipped it on its own clock, and the page said "Delivered" for good (#536).
ASK_STALE = 3600
MESSAGE_EXPIRE = 6 * 3600


def _expired(row: dict, now: float) -> bool:
    return now - row.get("ts", 0) > MESSAGE_EXPIRE


def expired(msg_id: str) -> bool:
    """Whether the user's unread message ``msg_id`` is too old to pass to Claude."""
    row = open_work()["messages"].get(msg_id)  # retires what expired (#741)
    return _expired(row, store.now()) if row else _expired_receipt(msg_id)


def pending() -> list[dict]:
    """Every message of the user's without a receipt, however far back (open_work, #329), and
    still deliverable: an expired one is retired (#741)."""
    # by time, ties in the order written (a stable sort over the file's order): two sends in one
    # millisecond came back by their ids' random suffix, and CI's retry test failed by chance
    # (seen 2026-11-21, merging the audit's fixes)
    rows = sorted(open_work()["messages"].values(), key=lambda r: r.get("ts", 0))
    return [{**r, "received": None} for r in rows]


# ---- replies and turns: send, then wait (#1042) ----
# An agent sends a message and waits for Claude's answer to it (GET /api/wait). A reply row says
# what it answers: `re`, the messages' ids (`wb chat reply --to`, else those Claude was given in
# the turn still running: `taken`), and `to`, whose they are ("user", or an agent's name; a row
# without `to` is the user's). The mod records each main-loop turn's end that took a message or
# wrote a reply in turns.jsonl `{ids, ended, by}`, `ids` the messages it took. So a wait is done
# for a message once a turn listing it has ended, and for a reply once a turn has ended after it
# (the turn that wrote it, or a later one; the listener's own, `turn_by`, #1303).

def _turns() -> list[dict]:
    return [r for r in store.read_jsonl_tail(chat_dir() / "turns.jsonl", THREAD_BYTES) if isinstance(r, dict)]


def turn_ended(ids, by: str | None = None) -> dict:
    """The mod: a turn of Claude's main loop ended, having taken the messages ``ids`` (maybe none)."""
    ids = [i for i in dict.fromkeys(ids) if isinstance(i, str) and 0 < len(i) <= 64]
    return store.append_jsonl(chat_dir() / "turns.jsonl",
                              {"ids": ids, "ended": store.now(), **({"by": by} if by else {})})


def taken() -> list[str]:
    """The messages the listening session has (their receipt, newest per id, says `now`, by it)
    that no ended turn lists yet, under MESSAGE_EXPIRE old: what the turn running now answers.
    Only the listener's: a session that died mid-turn never recorded its end, and every reply of
    the next session's for six hours went `to` that agent alone, pushed to no phone (the #1042
    review). Only those that opened their turn (`now`): an agent's message delivered into a wake
    turn (`into`; queued or behind a wake too, #1212) took that turn's plain report to the away
    user, sent to the agent alone and pushed to no phone (#1175, 2026-11-24); the skill tells
    Claude to answer an agent with `--to`."""
    lst = listener()
    if not lst or not lst.get("session"):
        return []
    ended = {i for t in _turns() for i in t.get("ids") or [] if isinstance(i, str)}
    last: dict = {}
    for r in store.read_jsonl_tail(chat_dir() / "receipts.jsonl", THREAD_BYTES):
        if isinstance(r, dict) and isinstance(r.get("id"), str):
            last.pop(r["id"], None)
            last[r["id"]] = r
    now = store.now()
    return [i for i, r in last.items()
            if r.get("how") == "now" and r.get("by") == lst["session"] and i not in ended
            and now - r.get("ts", 0) <= MESSAGE_EXPIRE]


def caller_listens() -> bool:
    """Whether this process runs under the listening session's Claude Code: its CLAUDE_PID is the
    listener's `parent` (the mod's claim, from `wb events`'s hello). Another delegating session,
    told to report through `wb chat reply` while the user is away, threaded its report onto the
    listener's in-flight message: sent to that agent alone, pushed to no phone (#1129,
    2026-11-23). Either unknown (an older mod's claim, a caller outside Claude Code): yes, as
    before; a claim of another machine's is never this process's."""
    lst = listener() or {}
    sid = store.opencode_session()
    if lst and (sid or listener_driver(lst)["tool"] == "opencode"):
        return _listens_across(lst, sid)
    parent, me = lst.get("parent"), os.environ.get("CLAUDE_PID", "")
    if not isinstance(parent, int) or isinstance(parent, bool) or not parent or not me.isdigit():
        return True
    if isinstance(lst.get("machine"), str) and lst["machine"] and lst["machine"] != store.machine():
        return False
    return int(me) == parent


def _listens_across(lst: dict, sid: str) -> bool:
    """caller_listens when an OpenCode session calls or listens: with both drivers open on one
    machine, the one that isn't listening threaded its report onto the listener's in-flight
    message, sent to the agent alone (#1363's matrix, #1366, 2026-11-25), as #1129 was between two
    Claude Code sessions: an OpenCode caller has no CLAUDE_PID to compare, and an OpenCode claim
    no `parent`. An OpenCode session (``sid``) listens when the claim is OpenCode's and its
    `session`, if it names one, is ``sid``; Claude Code (CLAUDE_PID) never does while OpenCode
    listens; a caller of neither, unknown, does, as before. A caller whose session is the claim's
    `session` listens before any driver check: an OpenCode claim without `driver`, which no fresh
    heartbeat lists (none written, one past 120 s, a session newer than its last beat), read as
    Claude Code's, and the listener's own report missed the agent waiting on it (the #1366
    review, 2026-11-25)."""
    if isinstance(lst.get("machine"), str) and lst["machine"] and lst["machine"] != store.machine():
        return False
    if sid and lst.get("session") == sid:
        return True
    if listener_driver(lst)["tool"] != "opencode":
        return False  # an OpenCode session's call while Claude Code listens
    if sid:
        mine = lst.get("session")
        return not (isinstance(mine, str) and mine) or mine == sid
    return not os.environ.get("CLAUDE_PID", "").isdigit()


# A turn's own answer, which the mod posts when a turn that had a web message ended with no reply
# to it (`wb chat reply --turn-answer`, an `auto` row; #1352, from #1340 F). The nudge turn it
# replaced cost Claude a turn, and a driver that doesn't follow the skill (OpenCode's, #1296)
# would have left the phone with nothing. Cut to about a phone's screenful: the whole answer stays
# in the terminal.
TURN_ANSWER_MAX = 4000
TURN_ANSWER_CUT = "… [cut: the whole answer is in the terminal]"


def turn_answer(text: str) -> str:
    """``text``, at most TURN_ANSWER_MAX characters: cut, it ends with TURN_ANSWER_CUT."""
    text = text.strip()
    if len(text) <= TURN_ANSWER_MAX:
        return text
    return text[:TURN_ANSWER_MAX - len(TURN_ANSWER_CUT)].rstrip() + TURN_ANSWER_CUT


def reply(text: str, to: list[str] | None = None, **extra) -> dict:
    """Claude's reply (`wb chat reply`), with `re` (``to``, else `taken`, for the listening
    session's own calls: `caller_listens`; another's goes to the user) and `to` (whose those
    messages are). ValueError for a ``to`` that is no message to Claude in the thread."""
    rows = {r.get("id"): r for r in store.read_jsonl_tail(chat_dir() / "messages.jsonl", THREAD_BYTES)
            if isinstance(r, dict) and _inbound(r)}
    if to:
        # "user": the user's thread, whatever this turn took: a consent question asked during an
        # agent's request went `to` that agent alone, pushed to no phone (#1042 review)
        ids = [i for i in dict.fromkeys(to) if i != "user"]
        if missing := [i for i in ids if i not in rows]:
            raise ValueError(f"no message {missing[0][:64]} to Claude in the thread")
    else:
        ids = [i for i in taken() if i in rows] if caller_listens() else []
        # a turn that took the user's message answers the user alone: an agent's message delivered
        # into the user's running turn read every plain `wb chat reply` to them (the #1042 review)
        if any(rows[i].get("from") == "user" for i in ids):
            ids = [i for i in ids if rows[i].get("from") == "user"]
    # whose turn wrote it (`turn_by`, the listener's key, as its turns.jsonl rows have it): any
    # session's turn end counted, and another session's ending after the listener's "On it" told
    # the agent Claude was done while the listener still worked (#1303, 2026-11-24)
    if (lst := listener()) and isinstance(lst.get("session"), str) and lst["session"] and caller_listens():
        extra.setdefault("turn_by", lst["session"])
    whose = list(dict.fromkeys(["user"] * bool(to and "user" in to) + [
        "user" if rows[i].get("from") == "user" else str(rows[i].get("from")) for i in ids]))
    return send(text, "claude", re=ids, to=whose, **extra)


def waiting_on_user() -> bool:
    """Whether Claude is paused on the user: a dialog the terminal shows that the phone was told
    of (a notice not yet settled), under ASK_STALE old."""
    now = store.now()
    rows = store.read_jsonl_tail(chat_dir() / "messages.jsonl", LATEST_BYTES)
    done = {r.get("id") for r in store.read_jsonl_tail(chat_dir() / "receipts.jsonl", LATEST_BYTES)
            if isinstance(r, dict) and r.get("how") == "answered"}
    return any(isinstance(r, dict) and r.get("from") == "notice" and r.get("id") not in done
               and now - r.get("ts", 0) <= ASK_STALE for r in rows)


def addressed(row: dict, caller: str) -> bool:
    """Whether Claude's ``row`` is for ``caller`` ("user", or an agent's name)."""
    return row.get("from") == "claude" and caller in (row.get("to") or ["user"])


def wait_state(after: str, caller: str) -> dict | None:
    """What GET /api/wait answers for message ``after``: {replies (Claude's rows after it for the
    caller), done (a turn has ended since the newest of those, the listener's that wrote it when
    the row says `turn_by`, else since ``after``: one that took it, for a message to Claude), and
    expired: True for a message to Claude unread past MESSAGE_EXPIRE with no reply}; None when
    ``after`` is in no recent part of the thread."""
    rows = store.read_jsonl_tail(chat_dir() / "messages.jsonl", THREAD_BYTES)
    at = next((n for n, r in enumerate(rows) if isinstance(r, dict) and r.get("id") == after), None)
    if at is None or (caller != "user" and not in_view(rows[at], caller)):  # another's is no message to it
        return None
    replies = [r for r in rows[at + 1:] if isinstance(r, dict) and addressed(r, caller)]
    if not replies and _inbound(rows[at]) and _expired(rows[at], store.now()) and expired(after):
        # never passed on: a wait on it held to its timeout, again and again, and the agent's
        # client waited out its whole deadline for an answer that could not come (#1138)
        return {"replies": [], "done": False, "expired": True}
    # done since the last row the caller now has: its newest reply's turn, else the message's
    me, turns = (replies or rows[at:at + 1])[-1], _turns()
    # a reply's: a turn of the session that wrote it (`turn_by`; an older or another session's
    # row has none: any), never another session's turn ending meanwhile (#1303)
    by = me.get("turn_by")
    done = (any(me["id"] in (t.get("ids") or []) for t in turns) if _inbound(me)
            else any(t.get("ended", 0) >= me.get("ts", 0) and (not by or t.get("by") == by) for t in turns))
    return {"replies": replies, "done": done}


# ---- an agent's view (#1042): its own conversation, nothing of the user's ----
# Round one let an agent token read the whole state and stream: the user's own messages, Claude's
# replies to the user, the settings. An agent reads only its own messages and Claude's rows whose
# `to` names it (`in_view`), the listener, the upload limits and the clock; files only of its own
# uploads (`by`) or attached to rows in its view; never a screenshot, an audio take, a task's sheet
# or a push's summary (outside AGENT_MAY, 403).


def in_view(row: dict, agent: str) -> bool:
    """Whether ``row`` is in ``agent``'s view: its own message, or Claude's to it."""
    return isinstance(row, dict) and ((row.get("role") == "agent" and row.get("from") == agent)
                                      or addressed(row, agent))


def agent_snapshot(agent: str) -> dict:
    """What GET /api/state (and the stream) carries for an agent's token."""
    return {"api": API_VERSION, "messages": thread(keep=lambda r: in_view(r, agent)), "listener": listener_view(listener()),
            "uploads": upload_limits(), "expire": MESSAGE_EXPIRE, "now": store.now()}


def agent_may_read(uid: str, agent: str) -> bool:
    """Whether ``agent`` may read upload ``uid``: one it uploaded, or one attached to a row in its view."""
    m = upload_meta(uid)
    if not m:
        return False
    if m.get("by") == agent:
        return True
    return any(in_view(r, agent) and any(isinstance(f, dict) and f.get("id") == uid for f in r.get("files") or [])
               for r in store.read_jsonl_tail(chat_dir() / "messages.jsonl", THREAD_BYTES))


def _push_key_or_none() -> str | None:
    """push_key, or None when the key can't be had (a full disk, a broken file): a broken push key
    took every snapshot with it, so the whole chat said "Can't reach the computer" (#924)."""
    try:
        return push_key()
    except (OSError, ValueError, RuntimeError) as err:
        if str(err) not in _no_key:  # said once in serve.log, not at every request
            _no_key.add(str(err))
            print(f"wb chat: no push key ({err}): notifications are off", file=sys.stderr, flush=True)
        return None


_no_key: set[str] = set()


def snapshot() -> dict:
    rows, lst = thread(), listener()
    return {"api": API_VERSION, "messages": rows, "listener": listener_view(lst), "driver": driver_state(lst, rows),
            "push": {"key": _push_key_or_none(), "test": (store.read_json(chat_dir() / "push-test.json", None)
                                                 or {}).get("ts")},
            "spend": week_spend(), "uploads": upload_limits(), "build": build(), "expire": MESSAGE_EXPIRE,
            "tasks": open_tasks(), "runs": open_runs(), "commands": flow_commands(), "dictate": dictate_ready(),
            "settings": settings_rev(),
            "now": store.now()}


def open_tasks() -> list[dict]:
    """The open tasks for the page's strip over the thread (#1016): while away, the chat is all
    the user sees, and it named no model (robustness minor 8). From task.json and status.json:
    `{name, phase, model, label, via, provider_name, updated_at}` (`updated_at`, its status's last
    write: the page's mark to fetch an open task's Log again, #1559); a bench replay is no one's task, and a parked one
    (`wb stop --all`) is left out as `wb status` leaves it: the quit's tasks filled the strip for
    good (#1097, 2026-11-23)."""
    out = []
    for t in store.all_tasks():
        sp, phase = t.spec, t.status.get("phase")
        if not sp.get("name") or sp.get("bench") or phase in store.CLOSED or store.parked(t.status):
            continue
        out.append({"name": t.name, "phase": phase or "queued", "model": sp.get("model") or "",
                    "label": model_label(sp), "via": sp.get("via") or "gpu",
                    "provider_name": sp.get("provider_name") or "", "updated_at": t.status.get("updated_at")})
    return out


def model_label(sp: dict) -> str:
    """The web app's one name for a task's model (#1339, 2026-11-26): a GPU model's label, the
    one Stats and wb top give it (task.json `model_label`), an API model's catalog name
    (models.catalog_title, hosted.json as it is); "" when there is none, and the page shows the
    id. The raw ids were most of what pushed the phone's strip to three lines. wb's own lines
    keep `model_label`'s id-first form: Claude reads those."""
    if (sp.get("via") or "gpu") == "api":
        from . import models
        return models.catalog_title(str(sp.get("model") or ""))
    return str(sp.get("model_label") or "")


RUN_FIELDS = ("run", "flow", "phase", "why", "stages", "spent_usd", "budget_usd", "budget_min", "gpu_min")


_COMMANDS: dict = {"at": -1e9, "rows": []}
COMMANDS_S = 30  # the flows' commands, read again after this: each read validates every flow


def flow_commands() -> list[dict]:
    """The flows with a slash command, for the composer's `/word` (#1463 P5): `[{command, flow,
    description, hint}]`, as both hosts register them (runs.flow_rows: the user's and the shipped,
    each valid). The page sends `/<command> <args>` as typed; the driver takes it as that command
    (SKILL.md). Read at most every COMMANDS_S."""
    from . import runs
    now = time.monotonic()
    if now - _COMMANDS["at"] >= COMMANDS_S:
        try:
            rows = runs.flow_rows()
        except (OSError, ValueError, RuntimeError):
            rows = []
        out, seen = [], set()
        for r in rows:
            c = r.get("command")
            if c and c not in seen:
                seen.add(c)
                names = list(r.get("params") or {})
                out.append({"command": c, "flow": r["name"], "description": r.get("description") or "",
                            "hint": f"<{names[0]}>" if len(names) == 1 else " ".join(f"{n}=…" for n in names)})
        _COMMANDS.update(at=now, rows=out)
    return list(_COMMANDS["rows"])


def open_runs() -> list[dict]:
    """The runs not closed, for the page's runs strip (#1278's S12): runs.snapshot's rows, by their
    start, without where each ran (`session`, `cwd`) or whether its driver lives (a `ps` each on
    macOS, on every change the state is built for): `{run, flow, phase, why, stages: {id: {done,
    running, null, filtered, not_run}}, spent_usd, budget_usd, budget_min, gpu_min}`. From the run
    files alone, as wb top's tick reads them."""
    from . import runs
    try:
        return [{k: r.get(k) for k in RUN_FIELDS} for r in runs.snapshot(live=False)]
    except (OSError, ValueError):
        return []


# ---- the task sheet (#1201): what `wb status`, `wb report`, `wb diff` and `wb log` show of an
# open task, read in-process for GET /api/task as the user taps the strip. Away, the strip's one
# line was all the user saw of the work, and "is it on track?" cost a message to Claude. Never
# `wb watch --json`, which can start OpenCode; read only, the lookups shared with cli.py
# (gitops.diff_base, store.Task.report, verify.report_lines, transcript.log_body) so the two
# can't drift. Everything here is agent-written text, which the page draws as text alone: it
# holds the user's token, which changes their Settings.
TASK_PARTS = ("summary", "diff", "log")
TASK_REPORT_CHARS = 64 * 1024   # the newest round's summary.md, cut past this
TASK_DIFF_CHARS = 256 * 1024    # a whole diff past this is `cut` (the page offers its files one by one)
TASK_LOG_ROWS = 50              # the log's rows by default (`n`), at most TASK_LOG_MAX
TASK_LOG_MAX = 200
TASK_LOG_BYTES = 1 << 20        # the events.jsonl tail read for them


def open_task(name) -> store.Task | None:
    """The open task ``name``, as the strip lists it (open_tasks: no bench replay, none parked), else None."""
    if not isinstance(name, str) or not store.NAME_RE.match(name):
        return None
    return store.Task(name) if any(t["name"] == name for t in open_tasks()) else None


def _cut_lines(text: str, most: int) -> tuple[str, int]:
    """``text`` with each line past ``most`` characters cut to it (ending `…`), and how many were."""
    n, out = 0, []
    for line in text.split("\n"):
        if len(line) > most:
            n, line = n + 1, line[:most - 1] + "…"
        out.append(line)
    return "\n".join(out), n


def in_tree(t: store.Task, text: str) -> str:
    """``text`` with the task's worktree written away: `read src/f0.py`, not the 70 characters of
    `~/.local/state/workbench/tasks/<t>/worktree/` before it, which wrapped each Log row to two
    lines on a phone and named a path the phone can't open (#1339, 2026-11-26); the worktree
    itself is `.`. Both spellings, as given and resolved: an agent's tools see either."""
    roots = {str(t.worktree)}
    try:
        roots.add(os.path.realpath(t.worktree))
    except (OSError, ValueError):
        pass
    for root in sorted(roots, key=len, reverse=True):
        text = re.sub(re.escape(root) + r"(?:/|(?![\w.-]))", lambda m: "" if m.group(0).endswith("/") else ".", text)
    return text


LEAD_CAUSE_CHARS = 160  # the cause in the task sheet's "what happened" line, cut past this


def report_lead(t: store.Task, report: str, has_report: bool) -> dict:
    """What the task sheet leads with (#1339, 2026-11-26), read from the report runner.summary_text
    writes: the agent's summary (`agent_summary`), the files it changed (`changed`, the status
    lines' paths, then the ignored files --include commits, runner.ignored_to_commit, as task_diff
    lists them, never the run caches' note; None with no report) and the tests' line (`tests`, `tests: PASS (…)`), and for a
    failed or stuck task one plain line of what happened (`happened`: `The agent stopped before
    writing anything (OpenCode exited).`; with no report, `The task stopped before the agent ran (…).`
    only for a stop before the agent with a clean worktree). The sheet showed only `opencode exited 1 (see
    <path>/runner.log)`, a path the phone can't open, with Reply below the fold. Never
    runner.log's tail: OpenCode's raw stderr, which Claude reads when asked."""
    from . import gitops, runner
    lines = report.split("\n")
    out: dict = {"happened": "", "agent_summary": None, "changed": None, "tests": ""}
    if has_report:
        if "agent's summary:" in lines:
            at = len(lines) - 1 - lines[::-1].index("agent's summary:")
            end = lines.index("files changed:", at) if "files changed:" in lines[at:] else len(lines)
            out["agent_summary"] = "\n".join(lines[at + 1:end]).strip()
            files = []
            for line in lines[end + 1:]:
                if (not line or line.startswith(("NOTE:", "not in the spec:", gitops.RUN_CACHES_NOTE))
                        or re.match(r"\d+ files? changed", line.strip())):
                    break
                if line != "(none)":
                    files.append(line[3:] if re.match(r"^[ MADRCU?!]{2} ", line) else line)
            # the ignored files the commit takes count: git status leaves them out, and the
            # sheet of a failed task whose agent wrote only lib/ said "before writing anything"
            # and "No files changed." beside the Diff tab listing them, then offered "Discard
            # it" (#1591, 2026-11-27; the Diff tab's own fix was #1548)
            out["changed"] = files + [p for p in runner.ignored_to_commit(t) if p not in files]
        out["tests"] = next((x for x in lines if re.match(r"tests: (PASS|FAIL)\b", x)), "")
    phase = t.status.get("phase")
    if phase in ("failed", "stuck"):
        stopped = next((x.removeprefix("STOPPED: ") for x in lines if x.startswith("STOPPED: ")), "")
        cause = stopped or ("OpenCode exited" if any(x.startswith("opencode exited ") for x in lines)
                            else str(t.status.get("last") or ""))
        cause = cause.strip().rstrip(".")
        cause = cause if len(cause) <= LEAD_CAUSE_CHARS else cause[:LEAD_CAUSE_CHARS - 1] + "…"
        why = f" ({cause})" if cause else ""
        if not has_report:
            # No report for this round is not "the agent never ran": a runner reaped mid-round
            # (cli.reap's restart or dead runner) or a round failing after an earlier one's work
            # leaves none either, and the sheet's 'Discard it' then threw away the agent's
            # edits on the word of the phone (#1339's review, 2026-11-26). Only the machine's
            # own stops before the agent (status infra setup, repo, model, which each runner
            # start clears) with a clean worktree say it never ran.
            try:  # an ignored file the commit takes is an edit too (#1591); a run's caches aren't (#1599)
                edits = bool(gitops.changed(t.worktree, runner.run_caches(t)) or runner.ignored_to_commit(t))
            except (OSError, gitops.GitError):
                edits = False
            if edits:
                out["happened"] = f"The task stopped partway{why}: what the agent changed so far is in the worktree."
            elif t.status.get("infra") in ("setup", "repo", "model"):
                out["happened"] = f"The task stopped before the agent ran{why}."
            else:
                out["happened"] = f"The task stopped{why}."
        else:
            did = "partway: what it changed so far is in the worktree" if out["changed"] else "before writing anything"
            out["happened"] = f"The agent {'was stopped' if phase == 'stuck' else 'stopped'} {did}{why}."
    return out


def task_summary(t: store.Task) -> dict:
    """`wb status <t>`'s row and `wb report <t>`'s text: phase, round, step, last, when it began
    and last changed (the page counts age and idle from `now`), its model, the newest round's
    report (cut at TASK_REPORT_CHARS) and the regression check's lines, the worktree's paths
    relative to it (in_tree), and what the sheet leads with (report_lead)."""
    from . import gitops, transcript, verify
    st, sp = t.status, t.spec
    n = st.get("round", 1)
    report = in_tree(t, transcript.plain(t.report(n)))
    lead = report_lead(t, report, (t.round_dir(n) / "summary.md").is_file())
    try:
        checks = [in_tree(t, transcript.plain(x)) for x in verify.report_lines(t)]
    except (OSError, ValueError, KeyError, gitops.GitError):
        checks = []
    return {"phase": st.get("phase") or "queued", "round": n, "step": st.get("steps", 1),
            "last": in_tree(t, transcript.plain(str(st.get("last") or ""))), "created_at": sp.get("created_at"),
            "updated_at": st.get("updated_at"), "model": sp.get("model") or "", "label": model_label(sp),
            "via": sp.get("via") or "gpu",
            "provider_name": sp.get("provider_name") or "", "report": report[:TASK_REPORT_CHARS],
            "report_cut": len(report) > TASK_REPORT_CHARS, "verify": checks, **lead}


def task_diff(t: store.Task, full: bool = False, path: str | None = None) -> dict | None:
    """`wb diff <t> --stat`, against the same base (gitops.diff_base: HEAD after `wb next`), and
    its files (`numstat`); ``full``, the diff instead of the stat (of ``path`` alone, one of the files), each
    line cut at transcript.JSON_LINE_CHARS (`lines_cut`), or null with `cut` past
    TASK_DIFF_CHARS. None for a ``path`` that isn't in it. The ignored files --include commits
    are in it too (runner.ignored_to_commit), as in wb diff: without them the phone showed a
    change importing a lib/util.py it never listed, under an ignored lib/ (#1548, 2026-11-26). The run caches
    accept leaves out (runner.run_caches) aren't, as in wb diff (#1599)."""
    from . import gitops, runner, transcript
    base = gitops.diff_base(t)
    out = {"base": base if base == "HEAD" else base[:12], "stat": "", "files": []}
    if not t.worktree.is_dir():
        return {**out, "note": "no worktree yet" if t.status.get("phase") in store.ACTIVE else "its worktree is gone"}
    # the run caches accept leaves out, out of it too, as wb diff leaves them out: the sheet listed
    # a test run's .pyc as the agent's change (#1599, 2026-11-27)
    force, skip = runner.ignored_to_commit(t), runner.run_caches(t)
    try:
        for line in (gitops.diff(t.worktree, base, numstat=True, force=force, skip=skip) or "").splitlines():
            added, deleted, name = (line.split("\t", 2) + ["", ""])[:3]
            out["files"].append({"path": name, "added": int(added) if added.isdigit() else None,
                                 "deleted": int(deleted) if deleted.isdigit() else None})
        if path is not None and not any(f["path"] == path and " => " not in path for f in out["files"]):
            return None
        paths = [":(literal)" + path] if path is not None else ()
        if not full:  # the stat first; the diff when asked for
            out["stat"] = gitops.diff(t.worktree, base, stat=True, paths=paths, force=force, skip=skip) or ""
        else:
            text = gitops.diff(t.worktree, base, paths=paths, limit=TASK_DIFF_CHARS, force=force, skip=skip)
            if text is None:
                out.update(diff=None, cut=True)
            else:
                text, n = _cut_lines(transcript.plain(text),
                                     transcript.JSON_LINE_CHARS)
                out.update(diff=text, cut=False, lines_cut=n)
    except (gitops.GitError, OSError) as e:
        return {**out, "note": str(e)[:500]}
    return out


def task_log(t: store.Task, n: int = TASK_LOG_ROWS) -> dict:
    """`wb log <t> -v -n <n>`: its last ``n`` rows (from the last TASK_LOG_BYTES of events.jsonl),
    each `{ts, round, kind, text}` with wb log's words (transcript.log_body), the worktree's paths
    relative to it (in_tree), cut at transcript.JSON_LINE_CHARS, `ok` for a tool call; `more` when
    older ones were left out."""
    from . import transcript
    rows = [r for r in store.read_jsonl_tail(t.dir / "events.jsonl", TASK_LOG_BYTES)
            if isinstance(r, dict) and r.get("kind") in transcript.LOG_KINDS]
    out = []
    for r in rows[-n:]:
        text = in_tree(t, transcript.plain(transcript.log_body(r, verbose=True) or ""))
        cut = text if len(text) <= transcript.JSON_LINE_CHARS else text[:transcript.JSON_LINE_CHARS - 1] + "…"
        out.append({"ts": r.get("ts"), "round": r.get("round"), "kind": r.get("kind"), "text": cut,
                    **({"ok": bool(r.get("ok"))} if r.get("kind") == "tool" else {})})
    return {"rows": out, "more": len(rows) > n}


LATEST_BYTES = 1 << 20  # the messages' tail `latest` reads for Claude's last word
# the messages that are news for the user: Claude's, and a notice that a dialog waits in the
# terminal (`notice`, #959)
NEWS = ("claude", "notice")


def latest() -> dict:
    """What a push shows, for the service worker (GET /api/latest): Claude's last message, or a
    notice still open, in the snapshot's shape. It fetched the whole /api/state, up to a
    megabyte, within the time iOS gives a push handler (#755). With the `listener` (its
    `driver`), which names a push whose row says no driver (#1319)."""
    rows = store.read_jsonl_tail(chat_dir() / "messages.jsonl", LATEST_BYTES)
    # a notice whose dialog settled in the terminal is no news (#959)
    answered = {r.get("id") for r in store.read_jsonl_tail(chat_dir() / "receipts.jsonl", LATEST_BYTES)
                if r.get("how") == "answered"}
    last = next((r for r in reversed(rows) if r.get("from") in NEWS and _for_user(r) and r.get("id") not in answered), None)
    return {"now": store.now(), "messages": [last] if last else [], "listener": listener_view(listener()),
            "push": {"test": (store.read_json(chat_dir() / "push-test.json", None) or {}).get("ts")}}


LISTS = ("messages",)  # what a patch sends row by row; the rest, small, it sends whole


def patch(sent: dict, snap: dict) -> dict | None:
    """What a stream sends after its first state (#755): the rows of `messages` new or changed
    since the state ``sent`` records (updated in place) as `{put, drop}` (the ids gone), and
    every other field whole (a kilobyte: the listener's busy, the push key...), or
    None when nothing but `now` changed. The whole state went out on every receipt and every busy
    flip, five times an exchange: 4.6 MB per open device with a long thread and plan mode. The
    small fields go whole so a page that took an older state from a POST's reply can't keep it:
    a busy flipped back would never be sent again."""
    out = {}
    for key in LISTS:
        rows = {r.get("id"): json.dumps(r, sort_keys=True) for r in snap.get(key) or []}
        old = sent.get(key) or {}
        put = [r for r in snap.get(key) or [] if old.get(r.get("id")) != rows[r.get("id")]]
        drop = [i for i in old if i not in rows]
        if put or drop:
            out[key] = {"put": put, "drop": drop}
        sent[key] = rows
    small = {k: v for k, v in snap.items() if k not in LISTS and k != "now"}
    body = json.dumps(small, sort_keys=True)
    if not out and body == sent.get("small"):
        return None
    sent["small"] = body
    return {**small, **out, "now": snap.get("now")}


def week_spend() -> dict:
    """Stats' Spend card: `wb spend` for the last 7 days (#1618: what was spent, never what was
    saved against an estimate). `claude`, Claude's tokens on every task, GPU and API; `wh` and
    `cost` the GPU's; `api`, the API tasks, whose dollars History's This week says (#1339)."""
    since = store.now() - savings.PERIODS["week"]
    t, a = savings.totals(store.results(), since=since), _week_api(since)
    return {**t, "claude": t["claude"] + a["claude"], "tasks": t["tasks"] + a["tasks"], "api": a}


def _week_api(since: float) -> dict:
    """The week's API tasks (#1022): savings.api_totals, each provider's name as OpenCode gives
    it in `names`."""
    a = savings.api_totals(store.results(trials=True), since)
    return {**a, "names": savings.provider_labels(a["by_provider"])}


# ---- notifications: web push with no payload (webpush.py) ----
# The page reports what it showed while visible (/api/seen); news no page showed within
# PUSH_DELAY gets one push to every subscribed device, whose service worker then fetches `/api/latest`
# and shows Claude's latest message (or a notice that it waits in the terminal). seen.jsonl: {id, ts, by: page|push,
# endpoint (push: the device that took it)}.

_keys: dict[str, str] = {}


def push_key() -> str:
    """This machine's VAPID public key (the applicationServerKey)."""
    path = str(token_path())
    if path not in _keys:
        _keys[path] = webpush.public_key(webpush.private_key(token_path()))
    return _keys[path]


_seen_read: dict = {"key": None, "at": 0, "all": set(), "per": {}}  # what's been read of seen.jsonl
_seen_lock = threading.Lock()  # the pusher and the page's requests read it from their own threads


def _seen_update(p: Path, c: dict) -> dict:
    """seen.jsonl read on from where the last call stopped (it is only appended to; another file
    or a shorter one is read afresh), under _seen_lock: every push tick reread the whole file,
    94 ms at 60,000 rows with nobody subscribed (#456)."""
    try:
        st = p.stat()
    except FileNotFoundError:
        c.update(key=None, at=0, all=set(), per={})
        return c
    key = (str(p), st.st_dev, st.st_ino)
    if c["key"] != key or st.st_size < c["at"]:
        c.update(key=key, at=0, all=set(), per={})
    if st.st_size > c["at"]:
        with open(p, "rb") as f:
            f.seek(c["at"])
            data = f.read()
        end = data.rfind(b"\n") + 1  # a row still being written waits for its newline
        for r in store._rows(data[:end]):
            if not isinstance(r, dict):
                continue
            if r.get("endpoint"):
                c["per"].setdefault(r.get("id"), set()).add(r["endpoint"])
            else:
                c["all"].add(r.get("id"))
        c["at"] += end
    return c


# _seen and _pushed hand out the cache itself, not a copy (copying 60,000 ids a tick is what #456
# removed): callers only look things up in it, which is safe while another thread adds.
def _seen() -> set:
    """Ids no push is owed for: shown on a page (or pushed before pushes were counted per device)."""
    with _seen_lock:
        return _seen_update(chat_dir() / "seen.jsonl", _seen_read)["all"]


def _pushed() -> dict[str, set]:
    """Each id's devices (endpoints) that took its push, or whose page showed it (#537)."""
    with _seen_lock:
        return _seen_update(chat_dir() / "seen.jsonl", _seen_read)["per"]


def _transient(status: int) -> bool:
    """A push that may go through if tried again: not reached, rate-limited, the service's fault.
    A certificate this machine can't check (webpush.CERT) is for good: retried every 30 s for 15
    minutes, it was then dropped without a word (#539)."""
    return status == 0 or status == 429 or status >= 500


def _result(got) -> tuple[int, str]:
    """A send's (status, reason) from push_all, or a bare status (a test's sender)."""
    return (got, "") if isinstance(got, int) else (int(got[0]), str(got[1] or ""))


def push_hint(results: dict) -> str:
    """What the page tells the user when a test push failed: `cert` (this machine can't check the
    service's certificate), `clock` (a 403: VAPID's expiry, read against a skewed clock),
    `unreached`, `refused`, or "" when one went through."""
    got = [_result(v)[0] for v in results.values()]
    if not got or any(200 <= c < 300 for c in got):
        return ""
    return ("cert" if webpush.CERT in got else "clock" if 403 in got
            else "unreached" if all(_transient(c) for c in got) else "refused")


def mark_seen(ids, by: str, endpoint: str | None = None) -> None:
    """No push owed for these ids: to any device, or, with ``endpoint``, to that one only (a page
    on screen but not in use speaks for its own device: a desktop tab left open silenced the
    phone, #537)."""
    done, mine = _seen(), _pushed()
    for i in dict.fromkeys(ids):
        if i not in done and not (endpoint and endpoint in mine.get(i, ())):
            store.append_jsonl(chat_dir() / "seen.jsonl", {"id": i, "ts": store.now(), "by": by,
                                                          **({"endpoint": endpoint} if endpoint else {})})


def news(now: float | None = None) -> list[str]:
    """Ids of Claude's messages and open notices old enough to push and recent enough to matter,
    not yet shown or pushed."""
    now = now or store.now()
    done = _seen()
    items = [(m["id"], m["ts"]) for m in thread() if m.get("from") in NEWS and _for_user(m)]
    return [i for i, ts in items if now - PUSH_WINDOW < ts <= now - PUSH_DELAY and i not in done]


class Pusher:
    """Sends the pushes: woken by the hub, and every PUSH_DELAY/2 so news ripens on time. Each
    device is counted on its own: every due push was marked sent whatever the services said, so a
    phone whose service was down for seconds missed a permission prompt's card (the 2026-11-18 audit,
    #86). One that failed for now is tried again after PUSH_RETRY, within PUSH_WINDOW."""

    def __init__(self, hub: "Hub", send=None):
        self.hub = hub
        self.send = send or (lambda eps: webpush.push_all(webpush.private_key(token_path()), eps))
        self.retry_at: dict[str, float] = {}  # endpoint: when a failed one may be tried again
        self.said: set[str] = set()  # failure reasons already in serve.log

    def tick(self, now: float | None = None) -> list[str]:
        """The ids pushed now, to at least one device."""
        now = now or store.now()
        subs = [x["endpoint"] for x in webpush.subscriptions()]
        if not subs:  # before the news: nobody to tell, nothing to read (#456)
            return []
        due = news(now)
        if not due:
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
            status, why = _result(got.get(e, 0))
            if not 200 <= status < 300:
                self._say(e, status, why)
            if _transient(status):
                self.retry_at[e] = now + PUSH_RETRY
                continue
            self.retry_at.pop(e, None)  # delivered, or refused for good (gone: unsubscribed)
            ok = 200 <= status < 300
            took |= ok
            for i in due:  # a refusal isn't tried again, and isn't written down as delivered (#539)
                if e not in pushed.get(i, ()):
                    store.append_jsonl(chat_dir() / "seen.jsonl", {"id": i, "ts": store.now(), "endpoint": e,
                                       **({"by": "push"} if ok else {"by": "refused", "status": status})})
        return due if took else []

    def _say(self, endpoint: str, status: int, why: str) -> None:
        """Each distinct failure once in serve.log: they were dropped, and the log stayed empty (#539)."""
        why = why or f"status {status}"
        if why not in self.said:
            self.said.add(why)
            print(f"web chat: a push to {urlsplit(endpoint).hostname} failed: {why}",
                  file=sys.stderr, flush=True)

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
    never leaves a phone running the old page against the new server. Only the files served:
    src/ and its tsconfigs are the scripts' TypeScript, built into them (#1323), so an edit there
    alone changes no page."""
    files = [(f.name, f.stat().st_mtime_ns, f.stat().st_size) for f in sorted(STATIC.iterdir())
             if f.is_file() and f.suffix in TYPES]
    return format(zlib.crc32(repr(files).encode()), "x")  # not hash(): it differs per process


# The kinds of dialog a notice may name (`wb chat notice --kind`), and the most of its `what` kept
KINDS = ("permission", "plan", "question", "elicitation")
ASK_TEXT = 2000


# ---- which device the user last used (#959) ----
# Away was where the user last typed: a web message made them away, typing in the terminal
# brought them back. The owner typed in the terminal, then picked up the phone with the chat open;
# a permission prompt came up in the terminal and never reached the phone (2026-11-21, #959).
# So using the page counts as using the phone: a page
# on screen and in use, on its Chat view, posts /api/active, and from another device than this
# one (a Host that isn't localhost, #1374) that marks the user away as a web message does
# (chat/away.json's `at`, which the mod reads), until they type in the terminal again or answer a
# dialog there (the mod's `back`). chat/page.json `{at}`: when a page was last in use, so a dialog
# at the terminal is announced only to a user who uses the chat: a page in use within
# PAGE_RECENT, or push on.
PAGE_RECENT = 86400
# The waiting line (#1350, from #1340's decision): no dialog is answered on the phone, only said
# to wait, on either driver. A permission names the way out: the driver run with permissions
# granted (its own mode and rules), so most work goes on from the chat (the owner, #1350). The
# words are the story's (`note.waiting`, `note.grant`, #1355), the driver's name filled in: a
# notice row from OpenCode's session says `driver: {tool: "opencode"}` (#1319, store.caller_kind).
WAITING = story.text("note.waiting", "claude-code")
WAITING_GRANT = " " + story.text("note.grant")
DRIVER_NAMES = {"opencode": "OpenCode"}


def active(desk: bool = False) -> float:
    """A chat page is in use now: recorded in page.json, and, with the chat on, in away.json, as
    a web message would be. Off, no session sends a dialog here, and turning it off cleared away:
    a page used meanwhile doesn't make the user away once it is turned on again. `desk`: the page
    is this machine's own (its Host is localhost, the modeline's link), so page.json alone."""
    now = store.now()
    store.write_json(chat_dir() / "page.json", {"at": now})
    if is_on() and not desk:
        store.write_json(chat_dir() / "away.json", {"at": now, "by": "page"})
    return now


def page_used() -> float:
    """When a chat page was last in use (seconds), 0 for never."""
    try:
        return float((store.read_json(chat_dir() / "page.json", None) or {}).get("at") or 0)
    except (TypeError, ValueError, AttributeError):
        return 0.0


def notice(tool: str, what: str, kind: str = "permission", driver: str = "") -> dict | None:
    """Tell the phone that a dialog waits in the terminal (#959, #1350): a `notice` row in the
    thread, one line with no buttons (only the terminal can answer it), pushed as Claude's
    messages are, until notice_done clears it. Every kind, away or not. None, and nothing
    written, for a user who doesn't use the chat: no page in use within PAGE_RECENT and no device
    with push on. ``driver``: whose dialog (`opencode`, the row's `driver` {tool}, the shape a
    reply's has, #1366; none: Claude Code's, as ever)."""
    if kind not in KINDS:
        raise ValueError(kind)
    if not webpush.subscriptions() and store.now() - page_used() > PAGE_RECENT:
        return None
    line = story.text("note.waiting", story.driver_of(driver)) if driver in DRIVER_NAMES else WAITING
    return send(line + (WAITING_GRANT if kind == "permission" else ""), "notice",
                tool=tool[:200], kind=kind, what=what[:ASK_TEXT],
                **({"driver": {"tool": driver}} if driver in DRIVER_NAMES else {}))


def notice_done(msg_id: str) -> bool:
    """The notice's dialog settled in the terminal (#959): no push is owed for it (one not yet due
    is never sent), and the thread leaves it out (a receipt `{id, ts, by: "terminal", how:
    "answered"}`): the waiting line clears (#1350). Unsettled, every prompt approved at the
    terminal in two seconds still buzzed the phone ten seconds later, saying it waited (the #959
    review). False for an id that is no notice in the thread."""
    if not any(r.get("id") == msg_id and r.get("from") == "notice"
               for r in store.read_jsonl_tail(chat_dir() / "messages.jsonl", LATEST_BYTES)):
        return False
    mark_seen([msg_id], "terminal")
    store.append_jsonl(chat_dir() / "receipts.jsonl",
                       {"id": msg_id, "ts": store.now(), "by": "terminal", "how": "answered"})
    return True


# The web chat's switch: on by default (the owner, 2026-11-24: "just turn on by default", #1281),
# reversing 2026-11-19's off until `wb chat on`. Only `wb chat off` writes {"on": false}, and that
# stays as the user left it. Off, the pane shows no link and no session listens or sends it their
# prompts; a server already running (systemd's) is left alone, its messages waiting.
# 2.0 marks what it writes ("v": 2). A bare {"on": false} is older: 1.91.x and 2.0 before #1281
# wrote it on a failed `wb chat on` too (a port taken), so with no chat/port.json (no server of
# this account ever ran) it is read as never set, and removed once, or the server that then runs
# would write port.json and make it the user's off again (#1405, 2026-11-25). A 1.x `wb chat off`
# before any server ran is turned on with it, as the on-by-default owner wants.
SWITCH_V = 2


def is_on() -> bool:
    path = chat_dir() / "switch.json"
    doc = store.read_json(path, None)
    if not isinstance(doc, dict) or bool(doc.get("on", True)):
        return True
    if doc.get("v") is None and not (chat_dir() / "port.json").exists():
        path.unlink(missing_ok=True)
        return True
    return False


def set_on(on: bool) -> None:
    doc = store.read_json(chat_dir() / "switch.json", None)
    kept = {"port": doc["port"]} if isinstance(doc, dict) and _valid_port(doc.get("port")) else {}
    store.write_json(chat_dir() / "switch.json", {"on": bool(on), "v": SWITCH_V, **kept})
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
    """Who answers on the port at 127.0.0.1, where the chat listens: "workbench" (this account's
    chat: its responses carry MARK keyed by this token), "other" (another program, another
    account's chat, or one holding a token since replaced), or None. A bare connect counted any
    program there as the chat, and `wb chat on` showed a link to it (#200, the 2026-11-19 audit)."""
    conn = http.client.HTTPConnection(BIND, port, timeout=2)
    try:
        conn.request("GET", "/manifest.webmanifest", headers={"Host": "localhost"})
        mark = conn.getresponse().getheader(MARK) or ""
        return "workbench" if hmac.compare_digest(mark, identity(token())) else "other"
    except (OSError, http.client.HTTPException):
        return "other" if _connects(port) else None
    finally:
        conn.close()


def _connects(port: int) -> bool:
    try:
        with socket.create_connection((BIND, port), timeout=1):
            return True
    except OSError:
        return False


def running(port: int) -> bool:
    """Whether the web chat's own server answers on the port."""
    return who_answers(port) == "workbench"


START_WAIT = 10  # seconds wb chat on waits for the server it started to answer


def wait_up(port: int, pid: int | None, limit: float = START_WAIT) -> bool:
    """Whether the server started as ``pid`` answers within ``limit`` seconds: it may exit at once
    (the port taken, an address not here), and a fixed half second said started either way. With
    no pid (systemd or launchd started it), only the wait."""
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if running(port):
            return True
        if pid is None:
            time.sleep(0.1)
            continue
        try:
            if os.waitpid(pid, os.WNOHANG)[0]:
                return False  # it exited: serve.log says why
        except ChildProcessError:  # not this process's child
            if not _alive(pid):
                return False
        time.sleep(0.1)
    return False


# Claude Code's variables, which make a wb command Claude's (the mod's dialogs gate it). The server
# runs commands for the user's clicks, so one started from a Claude session (`wb chat url`, the
# mod's ensure) must never carry them: it would run wb as Claude (#1281). An OpenCode driver's
# session id the same: from its tool or shell, the user's clicks were the driver's, gated (#1309).
CLAUDE_VARS = ("CLAUDECODE", "CLAUDE_PID", "CLAUDE_CODE_SESSION_ID", "OPENCODE_SESSION_ID")


def start(port: int) -> int:
    """`wb chat serve` detached, its output in chat/serve.log, its pid in chat/serve.pid: for a
    machine without the systemd unit, so turning the chat on is enough to reach it. Its
    environment has none of CLAUDE_VARS."""
    from . import cli  # only here: the server never starts one
    root = Path(__file__).resolve().parent.parent
    env = {k: v for k, v in os.environ.items() if k not in CLAUDE_VARS}
    with open(chat_dir() / "serve.log", "a") as log:  # outlives wb, and a logout (#531)
        proc = cli.detach([sys.executable, "-X", "utf8", str(root / "bin" / "wb"), "chat", "serve",
                           "--port", str(port)], cwd=root, stdout=log, stderr=log, env=env)
    store.write_json(chat_dir() / "serve.pid", {"pid": proc.pid, "port": port, "start": _started(proc.pid)})
    return proc.pid


# What keeps the chat up: systemd's wb-chat.service on Linux, launchd's workbench.chat agent on
# macOS (launchd/workbench.chat.plist). Only systemd was known: on a Mac `wb chat off` and the
# port-taken error gave systemctl advice, and the restart ignored the agent (#756).
LAUNCHD_LABEL = "workbench.chat"


def _macos() -> bool:
    return sys.platform == "darwin"


def service_hint(what: str) -> str:
    """The command that does ``what`` ("stop", "status") to the chat's service on this OS."""
    if _macos():
        return {"stop": f"launchctl bootout gui/$(id -u)/{LAUNCHD_LABEL}",
                "status": f"launchctl print gui/$(id -u)/{LAUNCHD_LABEL}"}[what]
    return {"stop": "systemctl --user disable --now wb-chat", "status": "systemctl --user status wb-chat"}[what]


def service_name() -> str:
    return f"launchd's {LAUNCHD_LABEL}" if _macos() else "systemd's wb-chat"


def _unit_keeps_it() -> bool:
    """Whether wb-chat.service is enabled (Linux) or the workbench.chat agent is loaded (macOS),
    so systemd or launchd brings the chat back itself."""
    argv = (["launchctl", "print", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"] if _macos()
            else ["systemctl", "--user", "is-enabled", "--quiet", "wb-chat"])
    if not shutil.which(argv[0]):
        return False
    try:
        return subprocess.run(argv, capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def blocked_why(port: int) -> str:
    """Why the chat can't come up on ``port``: something not this account's chat answers there."""
    return (f"another account's chat (or another program, or a server still on a replaced token) "
            f"answers on port {port}: wb chat on <free port> moves the web app there")


def _start_unit(restart: bool = False) -> None:
    """Have systemd or launchd start the chat it keeps (_unit_keeps_it): a stopped unit stays
    stopped until something starts it, and a second server beside it would take its port.
    ``restart``: stop it first, so it serves a port the user just chose (move)."""
    argv = (["launchctl", "kickstart", *(["-k"] if restart else []), f"gui/{os.getuid()}/{LAUNCHD_LABEL}"]
            if _macos() else ["systemctl", "--user", "restart" if restart else "start", "wb-chat"])
    try:
        subprocess.run(argv, capture_output=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        pass  # wait_up says whether it came up


def _log_tail() -> str:
    try:
        lines = (chat_dir() / "serve.log").read_text(errors="replace").strip().splitlines()
    except OSError:
        lines = []
    return (lines or ["(nothing)"])[-1]


def ensure(port: int) -> dict:
    """The chat up on ``port`` unless the user turned it off (#1281, replacing revive, #449):
    {"url", "state", "why"?}. "off": the switch is off. "up": this account's chat answers.
    "blocked": something else answers there (why: blocked_why). Otherwise it starts the unit that
    keeps it (_unit_keeps_it), else a detached server (start), unless the one serve.pid names is
    still starting, and waits START_WAIT: "started", or "failed" (why: serve.log's last line). The
    unit only for chosen_port, the one it serves; a unit that doesn't answer then is followed by a
    detached server once (#1343): "started" with why naming the unit, or "failed".
    `wb chat url` runs it, which the mod does at session start and every minute: a reboot, a
    crash, WSL without systemd, an install where setup never ran. One at a time (chat/ensure.lock):
    two sessions' at once started two servers, the second failing on the port."""
    from . import models  # as in stop_started
    out = {"url": ""}
    try:
        settings()  # chat.json it can't follow: said here, as the server would exit 78 on it
        out["url"] = url(port)
        if not is_on():
            return {**out, "state": "off"}
        with store.locked(chat_dir() / "ensure.lock"):
            who = who_answers(port)
            if who == "workbench":
                return {**out, "state": "up"}
            if who == "other":
                return {**out, "state": "blocked", "why": blocked_why(port)}
            rec = store.read_json(chat_dir() / "serve.pid", None) or {}
            pid = rec.get("pid") if isinstance(rec, dict) else None
            unit = False
            if not (pid and rec.get("port") == port and _alive(pid, rec.get("start"))
                    and models.runs(int(pid), "chat", "serve")):  # else still starting: wait for it
                if unit := port == chosen_port() and _unit_keeps_it():  # the unit serves the user's alone
                    _start_unit()
                    pid = None
                else:
                    pid = start(port)
            if wait_up(port, int(pid) if pid else None):
                return {**out, "state": "started"}
            if not unit:
                return {**out, "state": "failed", "why": _log_tail()}
            # a unit that never answers (exit 78 on a Python older than 3.11 on its PATH, which
            # stops it for good) left the app down for good: only the unit was ever started
            # again, every minute (#1343). wb's own server once; the unit's next try finds the
            # port taken, and the app is up either way
            unit_why = f"{service_name()} didn't answer within {START_WAIT} s ({service_hint('status')})"
            if wait_up(port, start(port)):
                return {**out, "state": "started", "why": f"{unit_why}: wb started its own server"}
            return {**out, "state": "failed", "why": f"{unit_why}, nor did wb's own server: {_log_tail()}"}
    except (RuntimeError, OSError) as e:  # chat.json it can't follow, a state dir it can't write
        return {**out, "state": "failed", "why": str(e)}


def move(port: int) -> None:
    """`wb chat on <port>`: keep ``port`` as the user's and take the running chat there. The
    server wb started stops (ensure starts one on the new port); the unit that keeps the chat
    restarts, as it serves chosen_port (#1616, 2026-11-27)."""
    set_port(port)
    stop_started()
    if _unit_keeps_it():
        _start_unit(restart=True)


def stop_started() -> bool:
    """Stop the server `start` made, if it still runs; never one wb didn't start (systemd's)."""
    from . import models  # only here: chat is imported by the server, which needs none of it
    pid = (st := store.read_json(chat_dir() / "serve.pid", None) or {}).get("pid")
    # the port stays, the pid goes: with serve.pid gone, port() fell back to PORT once the server
    # had exited, so `wb chat url` and the next `wb chat on` named the default, not the user's port
    # (macOS nightly, 2026-11-25; Linux passed only while the killed server was a zombie)
    if isinstance(st, dict) and isinstance(st.get("port"), int):
        store.write_json(chat_dir() / "serve.pid", {"port": st["port"]})
    else:
        (chat_dir() / "serve.pid").unlink(missing_ok=True)
    if not pid or not _alive(pid, st.get("start")) or not models.runs(int(pid), "chat", "serve"):
        return False
    try:
        os.kill(int(pid), 15)
    except OSError:
        return False
    return True


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
    session; a listener.json without a pid falls back to its age. A claim another machine wrote
    (`machine`, on a shared state directory) listens, as the mod has it: its pid means nothing in
    this process table, and the page said "Waiting for a Claude session" while that machine's
    session got every message (the review of #922). The mod writes the file only on a change, so
    its age can't say either. None while the chat is switched off, whichever host wrote the claim:
    the Claude Code mod's poll wrote its claim back after `wb chat off` (its link cached up), and
    /api/health said Claude listened while /api/wait's messages waited their hour (#1537,
    2026-11-26; #1480 had guarded only OpenCode's `hold`)."""
    st = store.read_json(chat_dir() / "listener.json", None)
    if not st or not is_on():
        return None
    if isinstance(st.get("machine"), str) and st["machine"] and st["machine"] != store.machine():
        return st
    if st.get("tool") == "opencode":
        try:
            at = (chat_dir() / "listener.json").stat().st_mtime
        except OSError:
            at = 0.0
        return st if _server_listens(st, at) else None
    if st.get("pid"):
        return st if _alive(st["pid"], st.get("start")) else None
    return st if store.now() - st.get("ts", 0) <= LISTENING_FOR else None


# ---- OpenCode's listener (#1319, #1340): the plugin's server part, in-process ----
# On OpenCode the listener is no `wb events` child but the OpenCode server whose plugin delivers
# the messages into a session (`session.prompt`), so its identity is that server and its
# heartbeat (the host-shape decision, #1296, 2026-11-25): listener.json `{session, tool:
# "opencode", pid, start, machine, cwd, busy}`, `pid` the server's, `start` as wb reads it. It
# listens while that process runs (pid and start: a recycled pid is gone, #532) and, once the
# server beats (store.beat_of), while the beat lists its session when it lists the whole store: a
# session deleted in that server never listens. A beat whose listing began (`ts`) before the claim
# was written (``at``, listener.json's mtime) can't say: the user's first prompt in a new session
# claims it up to 30 s before a beat lists it, and the page said "no session listening" while the
# session took the message (#1319's live check, 2026-11-26). First live claim wins, either
# driver's, as two Claude Code sessions' do: `hold` refuses while another's claim stands, and the
# Claude Code mod stands by while this one's does (its `claimStands` reads the same pid and start).
def _server_listens(st: dict, at: float = 0.0) -> bool:
    pid = st.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or not _alive(pid, st.get("start")):
        return False
    beat = store.beat_of(pid)
    if not (beat and beat.get("store") and st.get("session") not in beat["sessions"]):
        return True
    try:
        return float(beat.get("ts") or 0) < at
    except (TypeError, ValueError):
        return False


def hold(session: str, pid: int, busy: bool = False, cwd: str | None = None,
         waiting: dict | None = None) -> tuple[bool, dict | None]:
    """The OpenCode server ``pid`` listens for its session ``session`` (`wb chat listener`): (True,
    the claim) when written, or kept as it was; (False, the claim that stands) when another
    listener's live claim does: another server's, or a Claude Code session's. The same server may
    move it to another of its sessions (the user typed there). Written only on a change: every
    write wakes the page's streams. ``waiting``: the dialog up in its terminal, the plugin's
    waitingFact `{kind, tool, since, what?}` (#1355, the page's state line); ValueError for one of
    another shape. (False, None) while the chat is switched off: nothing written (#1480)."""
    if not store.SESSION_RE.fullmatch(session or "") or not isinstance(pid, int) or pid <= 0:
        raise ValueError("a session id and a server pid")
    if waiting is not None and not (isinstance(waiting, dict) and waiting.get("kind") in WAIT_KINDS
                                    and isinstance(waiting.get("tool"), str)
                                    and isinstance(waiting.get("since"), (int, float))
                                    and not isinstance(waiting.get("since"), bool)
                                    and isinstance(waiting.get("what", ""), str)):
        raise ValueError("waiting is {kind, tool, since, what?}")
    with store.locked(chat_dir() / "listener.lock"):
        # Off, nothing delivers: the plugin's on is cached a minute, and its poll took the claim
        # `wb chat off` had emptied again, so /api/health said OpenCode listened and ask_workbench
        # waited its whole hour for nothing (#1480, 2026-11-26). Turned on, its check takes it.
        if not is_on():
            return False, None
        cur = listener()
        if cur and not (cur.get("tool") == "opencode" and cur.get("pid") == pid
                        and cur.get("machine") in (None, store.machine())):
            return False, cur
        body = {"session": session, "tool": "opencode", "driver": "opencode", "cwd": cwd or os.getcwd(), "pid": pid,
                "start": _started(pid), "machine": store.machine(), "busy": bool(busy),
                **({"waiting": {k: waiting[k] for k in ("kind", "tool", "since", "what") if k in waiting}}
                   if waiting else {})}
        if store.read_json(chat_dir() / "listener.json", None) != body:
            store.write_json(chat_dir() / "listener.json", body)
        return True, body


def drop(pid: int) -> bool:
    """The OpenCode server ``pid`` stops listening (its plugin unloads): listener.json emptied
    when the claim is that server's, as the mod's quit does. Whether it was."""
    with store.locked(chat_dir() / "listener.lock"):
        cur = store.read_json(chat_dir() / "listener.json", None) or {}
        if not (isinstance(cur, dict) and cur.get("tool") == "opencode" and cur.get("pid") == pid):
            return False
        store.write_json(chat_dir() / "listener.json", {})
        return True


# Away, as the Claude Code mod reads it (`awayNow`, #110, #959, #1402), for the OpenCode plugin:
# the user last used the chat (chat/away.json's `at`, a web message delivered or a page in use)
# under AWAY_FOR ago, or longer while one of the session's tasks is open (active, or waiting on
# its driver and not parked). With the chat off, never. `back`: the user typed in the terminal,
# or answered a dialog there.
AWAY_FOR = 6 * 3600


def away_at() -> float:
    try:
        return float((store.read_json(chat_dir() / "away.json", None) or {}).get("at") or 0)
    except (TypeError, ValueError, AttributeError):
        return 0.0


def away(session: str = "") -> bool:
    """Whether the user is away from the terminal (above), for the driver session ``session``."""
    at = away_at()
    if not at or not is_on():
        return False
    if store.now() - at < AWAY_FOR:
        return True
    return bool(session) and any(
        t.spec.get("session") == session and (
            t.status.get("phase") in store.ACTIVE
            or (t.status.get("phase") in store.NEEDS_CLAUDE and not store.parked(t.status)))
        for t in store.all_tasks())


def went_away(by: str) -> None:
    """A user's web message reached the driver: away from now (the mod writes the same)."""
    store.write_json(chat_dir() / "away.json", {"at": store.now(), "by": by})


def back() -> None:
    """The user is back at the terminal: away.json `{at: 0}`, as the mod's `back` writes it."""
    if away_at():
        store.write_json(chat_dir() / "away.json", {"at": 0})


def owed(ids, session: str) -> list[str]:
    """Which of the messages ``ids`` the OpenCode session ``session`` still owes a reply (#1340
    F, the mod's `owedAtTurnEnd`): none of Claude's rows since the message was given (its newest
    receipt) answers it, by `re`, or was this session's own (`opencode_session`, not the
    plugin's `auto` post) to the message's sender (`to`, none: the user's). An agent's is
    settled only by a row to it (#1273)."""
    rows = [r for r in store.read_jsonl_tail(chat_dir() / "messages.jsonl", THREAD_BYTES) if isinstance(r, dict)]
    msgs = {r.get("id"): r for r in rows if _inbound(r)}
    given = {r["id"]: r.get("ts", 0) for r in store.read_jsonl_tail(chat_dir() / "receipts.jsonl", THREAD_BYTES)
             if isinstance(r, dict) and isinstance(r.get("id"), str)}
    out = []
    for i in dict.fromkeys(ids):
        m = msgs.get(i)
        if not m:
            continue
        sender = "user" if m.get("from") == "user" else str(m.get("from"))
        since = given.get(i, m.get("ts", 0))
        if not any(r.get("from") == "claude" and r.get("ts", 0) >= since and (
                i in (r.get("re") or [])
                or (not r.get("auto") and r.get("opencode_session") == session
                    and sender in (r.get("to") or ["user"])))
                for r in rows):
            out.append(i)
    return out


# The drivers a listener or a reply names (`driver.tool`, as task.json's `driver` and
# store.caller_kind name them). Another agent hands work to whichever listens, and the API said
# only Claude: the agents' skill told it "Claude" when OpenCode answered (#1366, 2026-11-25).
DRIVER_TOOLS = ("claude", "opencode")


def _driver_row(d: dict) -> dict:
    """``d`` as the API names a driver: {tool, model?} (the model OpenCode's `<provider>/<id>`)."""
    return {"tool": d["tool"], **({"model": d["model"]} if isinstance(d.get("model"), str) and d["model"] else {})}


def listener_driver(lst: dict) -> dict:
    """The driver of listener claim ``lst``, {tool, model?}: the claim's own `driver` (the tool's
    name, or {tool, model?}); else `opencode` when a live OpenCode server's heartbeat lists its
    `session` (with that session's model); else `claude`: before 2.2 only the Claude Code mod
    claimed, and its claim carries `parent`, its Claude Code's pid."""
    d, sid = lst.get("driver"), lst.get("session")
    if lst.get("tool") == "opencode":  # the plugin's own claim (`hold`, #1319): its server's beat names the model
        beat = store.beat_of(lst.get("pid")) or {}
        row = beat.get("sessions", {}).get(sid) if isinstance(beat.get("sessions"), dict) else None
        return _driver_row({"tool": "opencode", **(row if isinstance(row, dict) else {})})
    if isinstance(d, str) and d in DRIVER_TOOLS:
        return {"tool": d}
    if isinstance(d, dict) and d.get("tool") in DRIVER_TOOLS:
        return _driver_row(d)
    if isinstance(lst.get("parent"), int) or not isinstance(sid, str) or not sid:
        return {"tool": "claude"}
    beat = next((hb for hb in store.heartbeats() if store._beating(hb) and sid in hb["sessions"]), None)
    if beat is None:
        return {"tool": "claude"}
    row = beat["sessions"].get(sid)
    return _driver_row({"tool": "opencode", **(row if isinstance(row, dict) else {})})


def listener_view(lst: dict | None) -> dict | None:
    """The listener as the API serves it: the claim, with its `driver` ({tool, model?})."""
    return {**lst, "driver": listener_driver(lst)} if lst else None


# The driver's state (#1355): the one line the page keeps at the top of the chat, from the facts
# the hosts already write: the listener's claim (`busy`; `waiting`, the oldest dialog up in its
# terminal for WAITING_AFTER or more, the host's own filter of an auto-accept's flicker). Never a
# notice row: the mod posts one from every delegating session, and another session's dialog (or a
# dead one's, for ASK_STALE) read as the listener waiting, with its driver's grant (#1481,
# 2026-11-26); both hosts write their own wait in the claim. Waiting beats working beats idle;
# no live listener is `none`, or `off` while the chat is switched off: only `wb chat on` makes a
# driver listen then, and the page told the user to start one and run /workbench (#1526). The words are the story's (`state.*`, `next.*`, `wait.*`), and only
# a permission names the grant (`next.grant`, the driver's own way to run with permissions
# granted): a question or a plan waits however the driver runs.
WAITING_AFTER = 2.0   # seconds: the hosts' WAITING_AFTER_MS, a dialog answered sooner never shown
WAIT_KINDS = ("permission", "question", "plan", "elicitation", "consent")


def _waiting_fact(lst: dict | None) -> dict | None:
    """The dialog the listening driver waits on, {kind, tool, since, what?}: its claim's
    `waiting`; None for none (#1481: never another session's notice row)."""
    w = (lst or {}).get("waiting")
    if isinstance(w, dict) and w.get("kind") in WAIT_KINDS and isinstance(w.get("since"), (int, float)) \
            and not isinstance(w.get("since"), bool):
        tool = w.get("tool") if isinstance(w.get("tool"), str) else ""
        return {"kind": w["kind"], "tool": tool[:200], "since": float(w["since"]),
                **({"what": w["what"][:ASK_TEXT]} if isinstance(w.get("what"), str) and w["what"] else {})}
    return None


def _tilde(path: str) -> str:
    home = os.path.expanduser("~")
    if home not in ("", "/") and (path == home or path.startswith(home + "/")):
        return "~" + path[len(home):]
    return re.sub(r"^/home/[^/]+", "~", path)


_LIVE: dict = {}  # driver_state's default: the live listener, read afresh


def driver_state(lst: dict | None = _LIVE, rows: list[dict] | None = None) -> dict:
    """The driver's state for the page (snapshot's `driver`): {state: off|none|idle|working|waiting,
    driver (story's: claude-code|opencode|any), who, where?, waiting?: {kind, tool, since, what?},
    line, next, grant?: {line, command}}. ``lst``: the live listener (default: listener(); None: none);
    ``rows``: the thread (default: thread()), whose newest driver names a driver with none
    listening."""
    lst = listener() if lst is _LIVE else lst
    rows = thread() if rows is None else rows
    if lst:
        driver = story.driver_of(listener_driver(lst)["tool"])
    else:
        driver = next((story.driver_of(r["driver"]["tool"]) for r in reversed(rows)
                       if isinstance(r, dict) and r.get("from") in NEWS and isinstance(r.get("driver"), dict)
                       and r["driver"].get("tool") in DRIVER_TOOLS), "any")
    who = story.DRIVERS[driver]["who"]
    if not lst:
        state = "none" if is_on() else "off"  # #1526: off, no driver can listen until `wb chat on`
        return {"state": state, "driver": driver, "who": who,
                "line": story.text(f"state.{state}", driver), "next": story.text(f"next.{state}", driver)}
    where = _tilde(str(lst.get("cwd") or "")) or "its directory"
    out = {"driver": driver, "who": who, "where": where}
    wait = _waiting_fact(lst)
    if wait:
        # an MCP server's name (Claude Code's `mcp:<server>`), else what the host knows: OpenCode
        # names a form by no server
        tool = wait["tool"].removeprefix("mcp:")
        tool = tool if tool and tool != "form" else ("an MCP server" if wait["kind"] == "elicitation" else "a tool")
        what = story.text(f"wait.{wait['kind']}", driver, tool=tool)
        out.update(state="waiting", waiting=wait, line=story.text("state.waiting", driver, what=what),
                   next=story.text("next.waiting", driver))
        if wait["kind"] == "permission" and driver in story.ONLY["next.grant"]:
            out["grant"] = {"line": story.text("next.grant", driver), "command": story.DRIVERS[driver]["grant"]}
        return out
    state = "working" if lst.get("busy") else "idle"
    return {**out, "state": state, "line": story.text(f"state.{state}", driver, where=where),
            "next": story.text(f"next.{state}", driver)}


def writer_driver() -> dict | None:
    """The driver writing one of Claude's rows from this process, {tool, model?}, stamped on the
    row as `driver` (#1366): Claude Code's Bash (`claude`), or an OpenCode session (`opencode`,
    with its model when its server's heartbeat lists it); None for anyone else (a terminal, an
    agent's shell). A reply's `from` stays "claude" whichever driver writes it: the API's name."""
    kind = store.caller_kind()
    if kind == "claude":
        return {"tool": "claude"}
    if kind != "opencode":
        return None
    sid = store.opencode_session()
    _, beat = store.driver_server(sid) if sid else ("unknown", None)
    row = (beat or {}).get("sessions", {}).get(sid) if beat else None
    return _driver_row({"tool": "opencode", **(row if isinstance(row, dict) else {})})


IDLE_BUMP_S = 60  # without inotify, nothing changed: the streams wake this often all the same (#841)
# A task's own news (#1559, 2026-11-26): the hub watches tasks/ and each task's directory (never
# below it: its worktree, tree, rounds, its agent's OpenCode home), and wakes the streams only for
# these two files, its status (written on each phase and each tool call's `last`) and its spec;
# events.jsonl, written on every step, would rebuild every open stream's snapshot. Without it the
# strip and the open sheet heard of a task's phase up to IDLE_BUMP_S late
TASK_NEWS = ("status.json", "task.json")
TASK_SKIP = ("*/*",)  # Watcher skips, below tasks/: every directory inside a task's


def _answers(pid) -> bool:
    """Whether a process answers `kill 0` (one of another user's does too); unsure: yes."""
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except (OSError, ValueError, TypeError, OverflowError):
        pass
    return True


class Hub:
    """One watcher for every open stream: it wakes them the moment a chat file changes, a task's
    status or spec does (TASK_NEWS), or the listening session's process ends (a pidfd), so
    nothing polls."""

    def __init__(self):
        self.cond = threading.Condition()
        self.version = 0
        self.open: list = []  # the open streams' handlers, oldest first
        # the directory it started with: resolved again on each restart, a hub outliving its
        # server (a test's) made the next WB_HOME's chat/ before that test's setUp did (#134)
        self.dir = chat_dir()
        # tasks/ made here, beside chat/ and for the same reason, never by the watching thread
        self.tasks = store.home() / "tasks"
        self.tasks.mkdir(parents=True, exist_ok=True)
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

    def _news(self, changed: list) -> bool:
        """Whether a task's changes are news for the page: its status or spec (TASK_NEWS), or a
        task's directory made or gone."""
        return any(p.name in TASK_NEWS or p.parent == self.tasks for p in changed)

    def _watch(self) -> None:
        w = Watcher(self.dir)
        # a closed task's directory is left out, as wb events leaves it: a thousand of them were
        # a thousand watches for nothing (the 2026-11-16 audit)
        closed = tuple(t.name for t in store.all_tasks() if t.status.get("phase") in store.CLOSED)
        tw = Watcher(self.tasks, recursive=True, skip=TASK_SKIP + closed)
        pid, pidfd = None, None
        last, said_gone = time.monotonic(), None
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
                if not w.native or w.polled or tw.polled:  # no inotify, or a directory fell back to looking
                    # a wake only for a change, the listener's process ending (`kill 0`, no `ps`)
                    # or IDLE_BUMP_S, as the native select's timeout: every open stream rebuilt
                    # its whole snapshot each second, a `ps` fork each on macOS, with nothing new
                    # (#841)
                    changed, news, gone = w.wait(1), self._news(tw.wait(0)), False
                    if pid and pid != said_gone and not _answers(pid):
                        gone, said_gone = True, pid
                    if not changed and not news and not gone and time.monotonic() - last < IDLE_BUMP_S:
                        continue
                else:
                    # the idle bump counted from the last wake: a task's steps (no news) wake the
                    # select often, and a timeout restarted at each would never come
                    wait = max(0.0, IDLE_BUMP_S - (time.monotonic() - last))
                    ready, _, _ = select.select([w.fd, tw.fd] + ([pidfd] if pidfd is not None else []), [], [], wait)
                    w.read()
                    tasks = tw.read()
                    gone = pidfd is not None and pidfd in ready
                    if gone:  # the listener is gone
                        os.close(pidfd)
                        pidfd = None
                    time.sleep(0.02)  # gather a burst of writes into one wake
                    w.read()
                    tasks += tw.read()
                    if ready and w.fd not in ready and not gone and not self._news(tasks):
                        continue  # a task's step, its events or its worktree: nothing the page shows
                last = time.monotonic()
                self.bump()
        finally:
            w.close()
            tw.close()
            if pidfd is not None:
                os.close(pidfd)


# ---- attachments: images and files the user sends with a message (#623) ----
# The owner, from the phone (2026-11-20): attach a photo or a file, simple, and never uploaded twice
# by a refresh. Each file gets its id on the page when it is picked, and `PUT /api/upload/<id>`
# stores it under chat/uploads/<id>/ once: the same id again stores nothing and answers with the
# first upload. The body is read to its Content-Length into a part file and becomes the file only
# whole, so an upload cut short leaves nothing. A message names its files by id (`send`'s
# `files`); Claude gets each saved path and Reads it. Standard library only: a raw body, the name
# and type in headers, no multipart parser (`cgi` is gone in 3.13).
MB = 1 << 20
UPLOAD_MB = 25            # chat.json `upload_mb`: one file at most this
UPLOAD_MESSAGE_MB = 50    # chat.json `upload_message_mb`: a message's files together at most this
UPLOAD_FILES = 10         # files one message may carry
# UTF-8 bytes of a saved file's name: under eCryptfs's 143 (ext4 and APFS take 255). Cut by
# characters, 120 CJK ones were 360 bytes, the rename failed ENAMETOOLONG, and the page retried
# that 500 for good (#742)
UPLOAD_NAME = 140
FILE_CACHE = "private, max-age=31536000, immutable"  # GET /api/file/<id> (#754)
UPLOAD_ID = re.compile(r"[A-Za-z0-9-]{8,64}")  # crypto.randomUUID(), or the page's fallback
UPLOAD_TYPE = re.compile(r"[a-z0-9][a-z0-9.+-]*/[a-z0-9][a-z0-9.+-]*")
SHOWN = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/avif"}  # served as images
_upload_lock = threading.Lock()  # one finishing step at a time in this process (the lock file: any)


class UploadError(Exception):
    """An upload or an attachment refused: the HTTP status and the words the page shows on the chip."""

    def __init__(self, code: int, why: str):
        super().__init__(why)
        self.code, self.why = code, why


def _mb(n: float) -> str:
    """A size as the page says it: 300 B, 12 KB, 2.1 MB, 25 MB."""
    return (f"{n:.0f} B" if n < 1024 else f"{n / 1024:.0f} KB" if n < MB
            else f"{n / MB:.1f} MB" if n < 10 * MB else f"{n / MB:.0f} MB")


def upload_limits() -> dict:
    """{file, message, files}: bytes per file and per message (chat.json `upload_mb`,
    `upload_message_mb`, read on each call so a change needs no restart), files per message. Read
    raw, not through settings(): a bad `hosts` there raises ConfigError, and this is on every
    snapshot, so chat.json broken while the server runs would fail each request (#534 keeps serving)."""
    conf = store.read_json(config_path(), {})
    conf = conf if isinstance(conf, dict) else {}

    def mb(key: str, default: float) -> int:
        v = conf.get(key)
        ok = isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0
        return int((v if ok else default) * MB)
    return {"file": mb("upload_mb", UPLOAD_MB), "message": mb("upload_message_mb", UPLOAD_MESSAGE_MB),
            "files": UPLOAD_FILES}


def upload_name(name: str) -> str:
    """The name a file is saved under. One that is a path (a `/` or `\\`), or `.`/`..`, is refused:
    no browser sends one. Otherwise unprintable characters become `_`, leading dots go (the
    directory's own files start with one), and a long name keeps its extension, cut to
    UPLOAD_NAME bytes of UTF-8, never inside a character (#742)."""
    if not name or any(c in name for c in "/\\\0") or name.strip() in (".", ".."):
        raise UploadError(400, "that file name can't be saved")
    clean = "".join(c if c.isprintable() else "_" for c in name).strip().lstrip(". ")
    if len(clean.encode()) > UPLOAD_NAME:
        stem, ext = os.path.splitext(clean)
        ext = _cut(ext, 16)
        clean = _cut(stem, UPLOAD_NAME - len(ext.encode())) + ext
    return clean or "file"


def _cut(text: str, size: int) -> str:
    """``text`` cut to at most ``size`` bytes of UTF-8, whole characters only."""
    return text.encode()[:size].decode("utf-8", "ignore")


# A disk that refuses for good: another try with the same bytes fails the same way, so the page
# is told it is final (`final: true`) and shows why; it re-sent a 25 MB body forever (#742)
FINAL_ERRNOS = {errno.ENOSPC, errno.EDQUOT, errno.EACCES, errno.EPERM, errno.EROFS, errno.ENAMETOOLONG}


def _dir_fd(parent: int | None, name: str, make: bool) -> int:
    """A directory's fd, opened without following a link (O_NOFOLLOW on each step), made 0700 if
    asked; UploadError 409 when something else is in its place."""
    if make:
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
        except FileExistsError:
            pass
    try:
        return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    except FileNotFoundError:
        raise
    except OSError:  # ELOOP (a link), ENOTDIR (a file)
        raise UploadError(409, "something else is where the upload goes") from None


def _upload_dir(uid: str, make: bool = False) -> int:
    chat = os.open(chat_dir(), os.O_RDONLY | os.O_DIRECTORY)  # the state directory: the user's own
    try:
        up = _dir_fd(chat, "uploads", make)
    finally:
        os.close(chat)
    try:
        return _dir_fd(up, uid, make)
    finally:
        os.close(up)


def _read_meta(d: int) -> dict | None:
    try:
        fd = os.open(".meta.json", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=d)
    except OSError:
        return None
    try:
        with os.fdopen(fd, "rb") as f:
            m = json.loads(f.read(64 * 1024))
    except ValueError:
        return None
    return m if isinstance(m, dict) and isinstance(m.get("path"), str) else None


def upload_meta(uid: str) -> dict | None:
    """A stored upload: {id, name, size, type, sha256, ts, path}; None when there is none."""
    if not (isinstance(uid, str) and UPLOAD_ID.fullmatch(uid)):
        return None
    try:
        d = _upload_dir(uid)
    except (OSError, UploadError):
        return None
    try:
        return _read_meta(d)
    finally:
        os.close(d)


def _drain(body, n: int) -> None:
    while n > 0 and (chunk := body.read(min(1 << 16, n))):
        n -= len(chunk)


def save_upload(uid: str, name: str, ctype: str, length: int, body, by: str | None = None) -> tuple[dict, bool]:
    """Store ``length`` bytes from ``body`` as upload ``uid``: (its meta, whether this call stored
    it). An id already stored stores nothing and returns the first upload's meta, whatever this
    one's name or bytes. Refused (UploadError) before a byte is read: a bad id or name, a body over
    the file limit, a link or a file where its directory is. A body cut short is discarded.
    ``by``: the agent uploading (#1042), kept in the meta; an id stored by anyone else is 409 to it."""
    if not (isinstance(uid, str) and UPLOAD_ID.fullmatch(uid)):
        raise UploadError(400, "bad upload id")
    name = upload_name(name)
    limit = upload_limits()["file"]
    if length > limit:
        raise UploadError(413, f"too big: {_mb(length)}, the limit is {_mb(limit)}")
    ctype = (ctype or "").split(";")[0].strip().lower()
    ctype = ctype if len(ctype) <= 100 and UPLOAD_TYPE.fullmatch(ctype) else "application/octet-stream"
    if done := upload_meta(uid):  # a retry, or another tab: nothing to store
        _not_theirs(done, by)
        _drain(body, length)
        return done, False
    d = _upload_dir(uid, make=True)
    part = f".part-{secrets.token_hex(6)}"
    try:
        fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=d)
        try:
            h, got = hashlib.sha256(), 0
            with os.fdopen(fd, "wb") as f:
                while got < length and (chunk := body.read(min(1 << 16, length - got))):
                    f.write(chunk)
                    h.update(chunk)
                    got += len(chunk)
                if got < length:
                    raise UploadError(400, "the upload was cut short: it is tried again")
                f.flush()
                os.fsync(f.fileno())
            with _upload_lock, store.locked(chat_dir() / "uploads.lock"):
                if done := _read_meta(d):  # a twin upload of this id finished first
                    _not_theirs(done, by)
                    return done, False
                try:
                    st = os.lstat(name, dir_fd=d)
                except FileNotFoundError:
                    st = None
                if st and not stat.S_ISREG(st.st_mode):  # a link (or more) in the way
                    raise UploadError(409, "something else is where the file goes")
                # a regular file there is this id's own, from an upload stopped before its meta;
                # rename replaces whatever is at the name and never follows a link
                os.rename(part, name, src_dir_fd=d, dst_dir_fd=d)
                meta = {"id": uid, "name": name, "size": got, "type": ctype, "sha256": h.hexdigest(),
                        "ts": store.now(), "path": str(chat_dir() / "uploads" / uid / name), **({"by": by} if by else {})}
                tmp = f".meta-{secrets.token_hex(6)}"
                mfd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=d)
                with os.fdopen(mfd, "w", encoding="utf-8") as f:
                    f.write(json.dumps(meta, ensure_ascii=False))
                os.rename(tmp, ".meta.json", src_dir_fd=d, dst_dir_fd=d)
                return meta, True
        finally:
            try:
                os.unlink(part, dir_fd=d)
            except FileNotFoundError:
                pass
    finally:
        os.close(d)


ATTACHED = ("id", "name", "size", "type", "path")  # what a message keeps of each of its files


def _not_theirs(meta: dict, by: str | None) -> None:
    """An agent's upload under an id stored by anyone else is refused: answered with the first
    upload, it read the user's file's name and path (#1042)."""
    if by and meta.get("by") != by:
        raise UploadError(409, "that upload id is another sender's")


def attachments(ids) -> list[dict]:
    """A message's `files`: each id's stored upload, in order, one per content (sha256: the same
    photo picked twice is one file). UploadError for an id with no upload or too much in all."""
    lim = upload_limits()
    if not (isinstance(ids, list) and len(ids) <= lim["files"] and all(isinstance(i, str) for i in ids)):
        raise UploadError(400, f"files is a list of up to {lim['files']} upload ids")
    out, sums = [], set()
    for i in dict.fromkeys(ids):
        m = upload_meta(i)
        if not m:
            raise UploadError(400, f"no upload {i[:64]}: it must be uploaded before the message")
        if m.get("sha256") in sums:
            continue
        sums.add(m.get("sha256"))
        out.append({k: m.get(k) for k in ATTACHED})
    if (total := sum(int(f["size"] or 0) for f in out)) > lim["message"]:
        raise UploadError(413, f"too much for one message: {_mb(total)}, the limit is {_mb(lim['message'])}")
    return out


def upload_file(uid: str):
    """(an open binary file, its meta) for serving an upload, never through a link; None if none."""
    m = upload_meta(uid)
    if not m:
        return None
    try:
        d = _upload_dir(uid)
    except (OSError, UploadError):
        return None
    try:
        fd = os.open(m["name"], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=d)
    except OSError:
        return None
    finally:
        os.close(d)
    return os.fdopen(fd, "rb"), m



# ---- dictation (#1203): a voice clip in, its words in the box ----
# The page's mic (shown only while `dictate` is true: audio.json names a listener that can run)
# records a clip, uploads it as any attachment (PUT /api/upload/<id>, once), and asks
# POST /api/transcribe/<id> for its words, which land in the box for the user to edit and send:
# Claude only ever gets text. A voice memo attached as a file reached Claude as audio it can't
# hear. The words are kept beside the clip (TRANSCRIPT), so a retry or a reload never runs the
# listener twice; a GPU listener gives way to a task at once (409), never waits behind it: the
# page keeps the clip and asks again.
TRANSCRIPT = ".transcript.txt"  # a leading dot: no saved upload's name has one (upload_name)
TRANSCRIPT_MAX = 64 * 1024      # read back at most this; a listener's words are far fewer
DICTATE_TYPES = ("audio/", "video/")  # a phone's voice memo may be video/mp4 or video/webm
_dictate_lock = threading.Lock()  # one clip at a time: a retry of the same id then finds its words


def dictate_ready() -> bool:
    """The state's `dictate`: whether the page shows its mic (audio.dictation)."""
    from . import audio  # only here: the server needs none of it otherwise
    try:
        return audio.dictation() is not None
    except Exception:  # noqa: BLE001 - a snapshot never fails for it
        return False


def _transcript(uid: str) -> str | None:
    """The words kept for upload ``uid``, or None when it has none yet."""
    try:
        d = _upload_dir(uid)
    except (OSError, UploadError):
        return None
    try:
        fd = os.open(TRANSCRIPT, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=d)
    except OSError:
        return None
    finally:
        os.close(d)
    with os.fdopen(fd, "rb") as f:
        return f.read(TRANSCRIPT_MAX).decode("utf-8", "replace")


def _keep_transcript(uid: str, text: str) -> None:
    d = _upload_dir(uid)
    try:
        tmp = f".transcript-{secrets.token_hex(6)}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=d)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.rename(tmp, TRANSCRIPT, src_dir_fd=d, dst_dir_fd=d)
    finally:
        os.close(d)


def _say(text: str) -> None:
    print(f"wb chat: dictation: {text}", file=sys.stderr, flush=True)  # serve.log


def transcribe_upload(uid: str) -> str:
    """POST /api/transcribe/<id>: the words the listener hears in upload ``uid`` (audio.hear),
    kept beside it. UploadError 404 (no such upload), 415 (not audio); audio.Busy (the GPU is a
    task's), audio.Down (a url listener nothing answers at), LookupError (no listener),
    RuntimeError and the like (the listener failed) as raised."""
    from . import audio
    m = upload_meta(uid)
    if not m:
        raise UploadError(404, "no such upload")
    ctype = m.get("type") or ""
    if not (ctype.startswith(DICTATE_TYPES) or ctype == "application/octet-stream"):
        raise UploadError(415, f"not a voice clip ({ctype})")
    with _dictate_lock:
        if (said := _transcript(uid)) is not None:
            return said
        got = upload_file(uid)
        if got is None:
            raise UploadError(404, "no such upload")
        f, m = got
        ext = os.path.splitext(m["name"])[1].lower()
        # a copy under a plain name: the user's file name (quotes, a leading dash) never reaches
        # the listener's command line or the form's filename
        with f, tempfile.TemporaryDirectory(prefix=".wb-clip-") as tmp:
            clip = Path(tmp) / ("clip" + (ext if re.fullmatch(r"\.[a-z0-9]{1,8}", ext) else ""))
            with open(clip, "wb") as out:
                shutil.copyfileobj(f, out, 1 << 16)
            text = audio.hear(clip, ctype, say=_say)
        _keep_transcript(uid, text)
        return text

def drop_old_uploads(days: float) -> int:
    """`wb gc`: remove the uploads older than ``days`` whose message has its receipt (an expired
    one's too: open_work retires it, #741), or that no message waiting for one names (a draft
    never sent). The count removed."""
    root = chat_dir() / "uploads"
    if not root.is_dir() or root.is_symlink():
        return 0
    waiting = {f.get("id") for m in open_work()["messages"].values()
               for f in (m.get("files") or []) if isinstance(f, dict)}
    cutoff, n = store.now() - days * 86400, 0
    for d in sorted(root.iterdir()):
        if d.name in waiting or not UPLOAD_ID.fullmatch(d.name):
            continue
        try:
            st = d.lstat()
        except OSError:
            continue
        ts = (_read_meta_path(d) or {}).get("ts") or st.st_mtime
        if not isinstance(ts, (int, float)) or ts > cutoff:
            continue
        if stat.S_ISDIR(st.st_mode):
            shutil.rmtree(d, ignore_errors=True)  # never follows a link inside
        else:
            d.unlink(missing_ok=True)
        n += 1
    return n


def _read_meta_path(d: Path) -> dict | None:
    try:
        fd = os.open(d, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except (OSError, UploadError):
        return None
    try:
        return _read_meta(fd)
    finally:
        os.close(fd)


# ---- retry safety: Idempotency-Key (#1138) ----
# An agent whose POST /api/send was cut (a reset, a proxy's 502, its own timeout) can't tell
# whether the message landed; sent again, Claude was asked twice and did the work twice. A send
# with an `Idempotency-Key` header records {caller and key's sha256: {ts, req, id}} in
# chat/idempotency.json; the same key from the same caller within IDEM_FOR returns the message it
# sent the first time and sends nothing. The same key on another body is 422, never a silent drop
# of a new request. Only a sent message is recorded: a refusal sent nothing, so its retry is
# checked afresh. Kept small: ids, not messages, the newest IDEM_KEPT, none past IDEM_FOR. An
# upload needs none: PUT /api/upload/<id> already stores an id once.
IDEM_FOR = 86400
IDEM_KEPT = 1000
IDEM_KEY = re.compile(r"[\x21-\x7e]{1,255}")  # printable ASCII, no spaces (as the header is sent)


def idem_path() -> Path:
    return chat_dir() / "idempotency.json"


def _idem_read() -> dict:
    got = store.read_json(idem_path(), {}) or {}
    now = store.now()
    return {k: v for k, v in got.items() if isinstance(v, dict) and isinstance(v.get("id"), str)
            and isinstance(v.get("ts"), (int, float)) and now - v["ts"] < IDEM_FOR} if isinstance(got, dict) else {}


def idem_key(caller: str, key: str) -> str:
    return hashlib.sha256(f"{caller}\n{key}".encode()).hexdigest()


def idem_request(body) -> str:
    """The request's fingerprint: its JSON, keys sorted (a client that serialises again still matches)."""
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def idem_lookup(k: str) -> dict | None:
    return _idem_read().get(k)


def idem_record(k: str, req: str, msg_id: str) -> None:
    got = _idem_read()
    got.pop(k, None)
    got[k] = {"ts": store.now(), "req": req, "id": msg_id}
    store.write_json(idem_path(), dict(sorted(got.items(), key=lambda kv: kv[1]["ts"])[-IDEM_KEPT:]))


def message(msg_id: str) -> dict | None:
    """The thread's row ``msg_id``, from anywhere in messages.jsonl (only the lines naming it are
    parsed): a replayed send's message may be older than the thread's tail."""
    try:
        data = (chat_dir() / "messages.jsonl").read_bytes()
    except FileNotFoundError:
        return None
    key = msg_id.encode()
    return next((r for line in data.splitlines() if key in line for r in store._rows(line)
                 if isinstance(r, dict) and r.get("id") == msg_id), None)


# ---- the server ----

# One shape for every error answer (#1138): {"error": the plain line, "code": a stable code}.
# "error" stays the text, as before: the page (chat_static/src/app.ts) and ask_workbench.py show it as is,
# so moving it would have broken both; a client branches on "code". A code by status unless the
# answer names its own (ERRORS_SAID: a revoked token, an agent token from another machine, an
# unknown host, what an agent may not do (outside AGENT_MAY), a malformed Idempotency-Key, an
# upload to transcribe that isn't a voice clip).
ERROR_CODES = {400: "bad_request", 401: "bad_token", 403: "forbidden", 404: "not_found",
               409: "conflict", 410: "expired", 411: "length_required", 413: "too_large",
               414: "too_large", 415: "json_only", 422: "idempotency_key_reused",
               429: "rate_limited", 431: "too_large", 500: "server_error", 501: "not_implemented",
               502: "bad_gateway", 503: "busy", 507: "disk_full"}
ERRORS_SAID = ("token_revoked", "not_local", "unknown_host", "agent_forbidden", "bad_idempotency_key",
               "not_audio")
API_V1 = "/api/v1/"  # every /api/<x> is also /api/v1/<x>, the same handler (#1138)
# Every API route, by method (#1138): docs/chat-api.yaml describes exactly these, under /api/v1/,
# and a test checks it both ways and that each is a branch of its do_<method>; a route ending in
# "/" takes an id after it. A path off this list is 404, once the token and AGENT_MAY have passed
# it (an agent's token on any path outside AGENT_MAY is 403, as it was)
ROUTES = {"GET": ("/api/health", "/api/state", "/api/stream", "/api/wait", "/api/upload/", "/api/file/",
                  "/api/latest", "/api/shot", "/api/audio", "/api/task", "/api/settings", "/api/stats",
                  "/api/top"),
          "PUT": ("/api/upload/",),
          "POST": ("/api/send", "/api/seen", "/api/push", "/api/active", "/api/settings", "/api/transcribe/")}


def routed(method: str, path: str) -> bool:
    """Whether ``method`` ``path`` is one of ROUTES (else 404)."""
    return any(_covers(r, path) for r in ROUTES.get(method, ()))


REVOKED = {"error": "this agent token was revoked: ask the user for a new one (wb chat token add)",
           "code": "token_revoked"}

# ---- the web app's admin (#1280): Settings, Stats, the live view; the user's token alone ----
# A Settings change runs its wb command as a child that isn't Claude (settings.child_env): the
# user's click is the yes, as a command typed at their terminal is. Each change that went
# through is recorded in settings-changes.jsonl, for the mod to tell Claude in the terminal
# (#1283): the terminal stays primary (the owner, 2026-11-24), and a setting that needs Claude's
# help to work (`help`, settings.HELP: probe not set up) still gets Claude's offer.
CHANGES = "settings-changes.jsonl"  # under $WB_HOME
TOP_LINGER_S = 15        # the live view's child outlives its last viewer this long (a tab switched back)
TOP_STREAMS = 4          # GET /api/top streams open at once (each holds a thread)
TOP_RETRY_S = 30         # a child that ended by itself is tried again this long after, doubling (the pane's)
TOP_RETRY_MAX_S = 600    # to this
TOP_LINE_MAX = 4 << 20   # a child's line longer than this is dropped, not kept growing


SETTINGS_REV = "settings-rev.json"  # under chat/: the newest change, settings.record's stamp (#1355)


def changes_path() -> Path:
    return store.home() / CHANGES


def settings_rev() -> dict | None:
    """The newest Settings change, {ts, key, label, to, by} (chat/settings-rev.json), or None: the
    state's `settings`, so an open page reads its rows again and says who changed what (#1355)."""
    st = store.read_json(chat_dir() / SETTINGS_REV, None)
    return st if isinstance(st, dict) and isinstance(st.get("ts"), (int, float)) else None


def admin_set(key, value) -> tuple[int, dict]:
    """POST /api/settings: (status, body). 200 {ok, said, rows, changes, help?, detail?, page?}; 400 for no such row
    or a value it doesn't take (its reason, nothing runs); 409 {ok: false, error, page?, rows} when wb refused. A
    change that went through is {ts, key, label, from, to, said, help?, by: "web"} in CHANGES, which
    the row's wb child logs (cli.main, settings.logged: one writer for every caller, #1355)."""
    from . import settings as admin
    if not isinstance(key, str):
        return 400, {"error": "no such setting"}
    if isinstance(value, bool) or not isinstance(value, (str, int, float, type(None))):
        return 400, {"error": "not one of its choices"}
    try:
        done = admin.apply(key, value)
    except KeyError:
        return 400, {"error": "no such setting"}
    except ValueError as e:  # its reason: a number box was told "not one of its choices" (the web app's review, 2026-11-26)
        return 400, {"error": str(e) or "not one of its choices"}
    rows = admin.rows()
    if not done["ok"]:  # `page`: the refusal in the page's words (settings.page_line, #1558)
        return 409, {"ok": False, "error": done["said"], "rows": rows, **({"page": done["page"]} if done.get("page") else {})}
    helps = {"help": done["help"]} if done.get("help") else {}
    detail = {"detail": done["detail"]} if done.get("detail") else {}  # a run's Summary (#1278's S12)
    page = {"page": done["page"]} if done.get("page") else {}  # a note the page adds to its own words
    return 200, {"ok": True, "said": done["said"], "rows": rows, "changes": admin.changes(), **helps, **detail, **page}


class TopFeed:
    """GET /api/top's one `wb top --json` child per server, shared by every viewer: started with
    the first, killed TOP_LINGER_S after the last leaves; one that ends by itself is said
    (`down`) and tried again after TOP_RETRY_S, doubling to TOP_RETRY_MAX_S, as the pane's was.
    Its viewers wake on the child's lines, through `cond`, never through the Hub. `event` is the
    newest: ("top", the line verbatim) or ("down", why); `seq` counts them."""

    def __init__(self):
        self.cond = threading.Condition()
        self.slots = threading.BoundedSemaphore(TOP_STREAMS)
        self.viewers = 0
        self.left_at = 0.0     # monotonic: when the last viewer left
        self.driving = False   # a driver thread runs (at most one)
        self.closed = False
        self.seq = 0
        self.event: tuple[str, str] | None = None
        self.backoff = 0.0
        self.retry_at = 0.0    # monotonic: no child before then
        self.pid: int | None = None  # the child's, while one runs

    def join(self) -> None:
        with self.cond:
            self.viewers += 1
            if not self.driving:
                self.driving = True
                threading.Thread(target=self._drive, daemon=True, name="wb-top").start()

    def leave(self) -> None:
        with self.cond:
            self.viewers -= 1
            if not self.viewers:
                self.left_at = time.monotonic()
            self.cond.notify_all()

    def close(self) -> None:
        """Ends the driver and its child (a test's server going away)."""
        with self.cond:
            self.closed = True
            self.cond.notify_all()

    def down(self) -> dict:
        """The `down` event's data: why the child ended, and the seconds until it is tried again."""
        with self.cond:
            why = self.event[1] if self.event and self.event[0] == "down" else ""
            return {"why": why, "retry": max(0, round(self.retry_at - time.monotonic()))}

    def _wanted(self) -> bool:
        """Under the lock: whether a child should run; False ends the driver, at once (`driving`
        off), so a viewer who comes next starts the next one."""
        if not self.closed and (self.viewers or time.monotonic() - self.left_at < TOP_LINGER_S):
            return True
        self.driving = False
        return False

    def _drive(self) -> None:
        while True:
            with self.cond:
                if not self._wanted():
                    return
                if (wait := self.retry_at - time.monotonic()) > 0:
                    self.cond.wait(min(wait, 1.0))
                    continue
            if not self._child():
                return

    def _publish(self, kind: str, data: str) -> None:
        with self.cond:
            self.event = (kind, data)
            self.seq += 1
            if kind == "top":
                self.backoff = 0.0
            else:
                self.backoff = min(self.backoff * 2 if self.backoff else TOP_RETRY_S, TOP_RETRY_MAX_S)
                self.retry_at = time.monotonic() + self.backoff
            self.cond.notify_all()

    def _child(self) -> bool:
        """One child, until it ends (True: tried again later) or nobody wants it (False: the
        driver is over, the child killed)."""
        from . import settings as admin
        try:
            proc = subprocess.Popen([sys.executable, "-X", "utf8", str(admin.WB), "top", "--json"],
                                    env=admin.child_env(), stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as e:
            self._publish("down", f"wb couldn't run: {e}")
            return True
        self.pid = proc.pid
        out, err = proc.stdout.fileno(), proc.stderr.fileno()
        open_fds, buf, tail = [out, err], b"", b""
        try:
            while out in open_fds:
                with self.cond:
                    if not self._wanted():
                        return False
                ready, _, _ = select.select(open_fds, [], [], 1.0)
                for fd in ready:
                    chunk = os.read(fd, 1 << 16)
                    if not chunk:
                        open_fds.remove(fd)
                    elif fd == err:
                        tail = (tail + chunk)[-2000:]  # why it ended, should it
                    else:
                        *lines, buf = (buf + chunk).split(b"\n")
                        buf = buf if len(buf) <= TOP_LINE_MAX else b""
                        for line in lines:
                            text = line.decode("utf-8", "replace").strip()
                            try:
                                ok = isinstance(json.loads(text), dict)
                            except ValueError:
                                ok = False
                            if ok:
                                self._publish("top", text)
        finally:
            self.pid = None
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            proc.wait()
            proc.stdout.close()
            proc.stderr.close()
        said = next((ln.strip() for ln in reversed(tail.decode("utf-8", "replace").splitlines()) if ln.strip()), "")
        self._publish("down", said[:300] or f"wb top ended (exit {proc.returncode})")
        return True


class Handler(BaseHTTPRequestHandler):
    server_version = "wb-chat"
    timeout = TIMEOUT  # an idle or half-open connection holds a thread no longer than this
    hosts: set[str] | None = set()  # None: any Host (the user's `hosts: ["*"]`)
    hosts_seen: tuple | None = None  # chat.json's stat when `hosts` was read (config_stamp)
    secret = ""
    hub: "Hub"
    streams: threading.BoundedSemaphore
    waits: threading.BoundedSemaphore
    sends: RateLimit  # an agent token's sends (#1042)
    top: TopFeed  # GET /api/top's one child (#1280)
    agent: str | None = None  # the agent calling, by its token; None for the user's own
    agent_token = ""  # that token, so a held wait or stream ends once it is revoked (_revoked)

    def log_message(self, *args):  # the journal needn't hold every poll
        pass

    def _security_headers(self, cache: str = "no-store") -> None:
        self.send_header(MARK, identity(self.secret))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' blob:; "
                         "media-src blob:; "
                         "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")

    def _send(self, code: int, body: bytes, ctype: str, headers: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        if code >= 400 and isinstance(obj, dict) and "error" in obj:  # every error: {error, code} (#1138)
            obj = {"error": obj["error"], "code": obj.get("code") or ERROR_CODES.get(code, "error"),
                   **{k: v for k, v in obj.items() if k not in ("error", "code")}}
        self._send(code, json.dumps(obj).encode(), "application/json")

    def send_error(self, code, message=None, explain=None):
        """http.server's own refusals (an unsupported method, a malformed request line, headers
        too long) as JSON in the one error shape: they were an HTML page (#1138)."""
        if code < 400:
            return super().send_error(code, message, explain)
        self.close_connection = True
        said = (message or self.responses.get(code, ("error",))[0])[:200]
        body = json.dumps({"error": said, "code": ERROR_CODES.get(code, "error")}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self._security_headers()
        self.end_headers()
        if self.command != "HEAD":  # a HEAD's answer has no body (as http.server's own)
            self.wfile.write(body)

    def _versioned(self) -> None:
        """/api/v1/<x> is /api/<x>: rewritten before anything reads the path, so every check
        (the token's, AGENT_MAY's, ROUTES') sees the one name (#1138)."""
        if self.path.startswith(API_V1):
            self.path = "/api/" + self.path[len(API_V1):]

    def _host(self) -> str:
        """The Host header's name, lowercased, without its port or a v6 address's brackets."""
        host = (self.headers.get("Host") or "").strip().lower()
        if host.startswith("["):  # [v6] or [v6]:port
            host = host[1:host.find("]")] if "]" in host else host
        elif host.count(":") == 1:
            host = host.rsplit(":", 1)[0]
        return host

    def _desk(self) -> bool:
        """Whether the page was opened as this machine's own, by a loopback Host (the modeline's
        localhost link) on a request no proxy forwarded. Never by the peer: a tunnel proxies a
        phone from 127.0.0.1, and passes its own name as the Host (#1374)."""
        host = self._host()
        return (host in ("localhost", "::1") or host.startswith("127.")) and not self._forwarded()

    def _hosts_now(self) -> set[str] | None:
        """chat.json's `hosts` as it stands, read again whenever the file changed: a tunnel's name
        added while the server ran was 403 until a restart nothing named, and from #1616 (no
        product's name looked up) chat.json is every tunnel's way in (2026-11-27). A broken file
        keeps the last good list, as upload_limits keeps serving (#534)."""
        cls, stamp = type(self), config_stamp()
        if stamp != cls.hosts_seen:
            cls.hosts_seen = stamp
            try:
                if stamp is not None:
                    json.loads(config_path().read_text(encoding="utf-8"))  # half-written: keep
                cls.hosts = plan()["hosts"]
            except (OSError, ValueError, ConfigError):
                pass
        return cls.hosts

    def _allowed(self) -> bool:
        host = self._host()
        hosts = self._hosts_now()
        if hosts is not None and host not in hosts:
            self._json(403, {"error": f"unknown host {host[:100]}: add it to chat.json's hosts",
                             "code": "unknown_host"})
            return False
        return True

    def _peer(self) -> str:
        """The address the request came from, lowercased, without a v4-mapped prefix or a zone."""
        try:
            return str(self.client_address[0]).lower().removeprefix("::ffff:").split("%")[0]
        except (AttributeError, IndexError, TypeError):
            return ""

    def _forwarded(self) -> bool:
        """Whether a proxy says it forwarded this request (any of FORWARDED's headers)."""
        return any(self.headers.get(h) is not None for h in FORWARDED)

    def _from_this_machine(self) -> bool:
        """Whether the request came from this machine: a loopback peer, with no proxy's forwarding
        header. The chat listens on 127.0.0.1 only, so behind a tunnel or proxy every peer is 127.0.0.1, another device's too: the headers those
        add tell it apart (#1616, 2026-11-27). An `ssh -L` adds none, so a forward over ssh still
        looks local: that one is the user's own login."""
        peer = self._peer()
        return (peer == "::1" or peer.startswith("127.")) and not self._forwarded()

    def _authed(self) -> str | None:
        """Who calls: "user" for the user's token, "agent <name>" for an agent's (and ``agent``
        set, #1042); else None, answered 401."""
        auth = self.headers.get("Authorization") or ""
        got = auth[len("Bearer "):].strip() if auth.startswith("Bearer ") else ""
        self.agent = None
        if self.secret and got and hmac.compare_digest(got.encode(), self.secret.encode()):
            return "user"
        if got and (name := agent_for(got)):
            if not self._from_this_machine():
                # agents are this machine's own (owner, 2026-11-23): an agent token sent from
                # another device, through a tunnel or proxy to localhost (#1616), is refused; the
                # user's own token is unaffected
                self._json(403, {"error": "agent tokens work only from this machine, at the address"
                                          " `wb chat url` prints", "code": "not_local"})
                return None
            self.agent, self.agent_token = name, got
            return f"agent {name}"
        if got and token_revoked(got):
            self._json(401, REVOKED)
            return None
        self._json(401, {"error": "bad or missing token: open the link from `wb chat url`"})
        return None

    def _revoked(self) -> bool:
        """Whether the agent's token was revoked (or replaced) since this request began: a stream
        open and a wait held went on reading Claude's replies to it after `wb chat token revoke`,
        which no request wakes (the #1042 review). Checked at every wake, before anything is sent."""
        return self.agent is not None and agent_for(self.agent_token) != self.agent

    def _snap(self) -> dict:
        """The state for whoever calls: the whole of it for the user, an agent's own view (#1042)."""
        return agent_snapshot(self.agent) if self.agent else snapshot()

    def _gate(self, method: str, path: str) -> bool:
        """Every /api/ request, before its path's own handler: the token (401), then for an
        agent's the allow-list (AGENT_MAY, else 403). True when the request may go on."""
        if not self._authed():
            return False
        if self.agent and not agent_may(method, path):
            self._json(403, {"error": agent_refusal(path), "code": "agent_forbidden"})
            return False
        return True

    def _wait(self) -> None:
        """GET /api/wait?after=<message id>&timeout=<s>&until=done|reply (#1042): Claude's replies
        to the caller after that message, held until there is one, the turn is done (`until=done`,
        the default), nobody listens, or the timeout. {replies, done, listener}."""
        q = parse_qs(urlsplit(self.path).query)
        after, until = (q.get("after") or [""])[0], (q.get("until") or ["done"])[0]
        try:
            timeout = float((q.get("timeout") or [WAIT_DEFAULT])[0])
        except ValueError:
            timeout = -1.0
        if not 0 < len(after) <= 64 or not 0 <= timeout <= WAIT_MAX or until not in ("done", "reply"):
            return self._json(400, {"error": f"wait is ?after=<message id>&timeout=<0 to {WAIT_MAX}>"
                                             "&until=done|reply"})
        if not self.waits.acquire(blocking=False):
            return self._json(503, {"error": "too many waits open: try again in a moment"})
        hub, caller = self.hub, self.agent or "user"
        deadline = time.monotonic() + timeout
        paused = None  # waiting_on_user at the first look: turning true returns, once
        try:
            while True:
                with hub.cond:
                    seen = hub.version  # before the look: a change after it wakes the wait
                if self._revoked():
                    return self._json(401, REVOKED)
                got = wait_state(after, caller)
                if got is None:
                    return self._json(404, {"error": "no such message"})
                if got.get("expired"):
                    return self._json(410, {"error": "that message expired unread: it was never passed "
                                                     "to the driver, so send it again"})
                lst, left, held = listener(), deadline - time.monotonic(), waiting_on_user()
                paused = held if paused is None else paused
                if got["replies"] or (got["done"] and until == "done") or lst is None or left <= 0 \
                        or (held and not paused):
                    return self._json(200, {**got, "listener": listener_view(lst), "waiting_on_user": held})
                looked = time.monotonic()
                while hub.version == seen and time.monotonic() - looked < PING_EVERY \
                        and (left := deadline - time.monotonic()) > 0:
                    if self._gone():  # a client that hung up holds no slot to its timeout (#1042 review)
                        return
                    with hub.cond:
                        hub.cond.wait_for(lambda: hub.version != seen, timeout=min(left, WAIT_LOOK))
        finally:
            self.waits.release()

    def _task(self) -> None:
        """GET /api/task?name=<t>&part=summary|diff|log (#1201): `diff` takes `full=1` (and
        `path=<one of its files>`), `log` takes `n`. Only an open task, as the strip lists it."""
        q = parse_qs(urlsplit(self.path).query)
        one = lambda k: (q.get(k) or [""])[0]  # noqa: E731
        part, n = one("part") or "summary", one("n")
        if part not in TASK_PARTS or (n and not (n.isdigit() and 0 < int(n) <= TASK_LOG_MAX)):
            return self._json(400, {"error": f"task is ?name=<task>&part=summary|diff|log"
                                             f"&full=1&path=<file>&n=<1 to {TASK_LOG_MAX}>"})
        if (t := open_task(one("name"))) is None:
            return self._json(404, {"error": "no such open task"})
        if part == "summary":
            got = task_summary(t)
        elif part == "diff":
            got = task_diff(t, full=one("full") == "1", path=q["path"][0] if q.get("path") else None)
            if got is None:
                return self._json(404, {"error": "no such file in the diff"})
        else:
            got = task_log(t, int(n) if n else TASK_LOG_ROWS)
        self._json(200, {"task": t.name, "part": part, **got, "now": store.now()})

    def _gone(self) -> bool:
        """Whether the client hung up: its side readable with nothing to read."""
        try:
            ready, _, _ = select.select([self.connection], [], [], 0)
            return bool(ready) and not self.connection.recv(1, socket.MSG_PEEK)
        except (OSError, ValueError):
            return True

    def do_GET(self):
        if not self._allowed():
            return
        self._versioned()
        path = self.path.split("?")[0]
        if path.startswith("/api/") and not self._gate("GET", path):  # the token; agents' allow-list
            return
        if path.startswith("/api/") and not routed("GET", path):  # off ROUTES: none (#1138)
            return self._json(404, {"error": "not found"})
        if path == "/manifest.webmanifest":  # with the key, it opens the installed app keyed
            k = (parse_qs(urlsplit(self.path).query).get("k") or [""])[0]
            m = json.loads((STATIC / "manifest.webmanifest").read_text())
            if self.secret and k and hmac.compare_digest(k.encode(), self.secret.encode()):
                m["start_url"] = f"/?token={k}"
            return self._send(200, json.dumps(m).encode(), TYPES[".webmanifest"])
        if path == "/api/shot":  # one side of wb shot's pair (#99), behind the token like the rest
            q = parse_qs(urlsplit(self.path).query)
            n = (q.get("n") or [""])[0]
            body = shot_png((q.get("task") or [""])[0], int(n) if n.isdigit() else 0,
                            (q.get("side") or [""])[0])
            if body is None:  # 410: dropped with its archived task, the message still names it (#821)
                pair = shots((q.get("task") or [""])[0], int(n) if n.isdigit() else 0)
                side = (pair or {}).get((q.get("side") or [""])[0])
                gone = isinstance(side, dict) and side.get("pruned")
                return self._json(410 if gone else 404, {"error": "expired" if gone else "no such screenshot"})
            return self._send(200, body, "image/png")
        if path == "/api/audio":  # one take of Claude's shortlist (#397), behind the token too
            q = parse_qs(urlsplit(self.path).query)
            n = (q.get("n") or [""])[0]
            got = audio_take((q.get("batch") or [""])[0], int(n) if n.isdigit() else 0)
            return self._json(404, {"error": "no such take"}) if got is None else self._send(200, *got)
        if path == "/api/task":  # the task sheet: one open task's summary, diff or log (#1201)
            return self._task()
        if path.startswith(("/api/upload/", "/api/file/")):  # an attachment (#623)
            return self._get_upload(path)
        if path == "/api/state":
            return self._json(200, self._snap())
        if path == "/api/health":  # an agent's first call: is anyone listening (#1042)
            # and which driver listens (#1366): only while one does, as before 2.2 it was the whole answer
            lst = listener()
            return self._json(200, {"api": API_VERSION, "listening": lst is not None,
                                    **({"driver": listener_driver(lst)} if lst else {})})
        if path == "/api/wait":  # send, then wait for the answer (#1042)
            return self._wait()
        if path == "/api/latest":  # the service worker's, at a push (#755)
            return self._json(200, latest())
        if path == "/api/stream":
            return self._stream()
        if path == "/api/settings":  # the web app's Settings view: settings.py's rows (#1280)
            from . import settings as admin
            # read afresh: the cache knows only this server's writes, so a change made at the
            # terminal within ROWS_CACHE_S was drawn as it had been (#1392, 2026-11-25)
            return self._json(200, {"rows": admin.rows(fresh=True), "changes": admin.changes(), "ts": store.now()})
        if path == "/api/stats":  # the Stats view: wb stats --json in-process, and the names' reads
            from . import settings as admin, stats
            return self._json(200, {"feed": stats.feed(), **admin.reads()})
        if path == "/api/top":  # the Stats view's live part: wb top --json's ticks (TopFeed)
            return self._top()
        name = "index.html" if path in ("/", "/index.html") else path.lstrip("/")
        f = STATIC / name
        if "/" in name or f.suffix not in TYPES or not f.is_file():
            return self._json(404, {"error": "not found"})
        self._send(200, f.read_bytes(), TYPES[f.suffix])

    def _stream(self):
        """Server-sent events: the whole state on connect (`state`), then only what changed
        (`patch`, chat.patch: #755), a ping between. A reconnect starts with the whole state
        again, so it is a resync."""
        hub = self.hub
        if not self.streams.acquire(blocking=False):
            if self.agent:  # an agent's stream never pushes a page out (#1042 review)
                return self._json(503, {"error": "too many open streams"})
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
            # An event is followed, NUDGE_AFTER later, by a blank line (an empty event, nothing
            # for a reader). WebKit's network layer (Playwright's on Linux) hands a streamed body
            # to the page's reader only up to the first 8 KB of what came in one read, the rest
            # once more bytes come: a state past 8 KB on a reconnect waited for the next ping, 15 s,
            # and the push's wake showed nothing (the #1284 review, 2026-11-24). Sent with the
            # event, the byte comes in its read and is held with it, so it goes on its own
            seen, had, sent, nudge = -1, None, time.monotonic(), False
            while True:
                with hub.cond:
                    due = sent + (NUDGE_AFTER if nudge else PING_EVERY)
                    woke = hub.cond.wait_for(lambda: hub.version != seen,
                                             timeout=max(0.0, due - time.monotonic()))
                    seen = hub.version
                # revoked: it ends, and its reconnect is 401. A closed hub (Hub.close) ends it before
                # another snapshot: woken by the close, a test's stream made one in the next test's
                # WB_HOME, its push key cached there (nightly run 45577067512, 2026-11-25)
                if self.evicted or self._revoked() or hub.closed.is_set():
                    return
                if woke:
                    snap = self._snap()
                    if had is None:  # the first: the whole state, recorded for the patches
                        had = {}
                        patch(had, snap)
                        event = f"event: state\ndata: {json.dumps(snap)}\n\n"
                    else:
                        delta = patch(had, snap)
                        event = delta and f"event: patch\ndata: {json.dumps(delta)}\n\n"
                    if event:
                        self.wfile.write(event.encode())
                        self.wfile.flush()
                        sent, nudge = time.monotonic(), True
                        continue
                    if time.monotonic() < due:
                        # Without inotify the hub wakes every second (how a dead listener is
                        # noticed): a ping only after PING_EVERY of silence, not one a second
                        # (the 2026-11-16 audit)
                        continue
                if nudge:  # the event's tail, out of WebKit's buffer; the ping still PING_EVERY on
                    self.wfile.write(b"\n")
                    self.wfile.flush()
                    nudge = False
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

    def _top(self) -> None:
        """GET /api/top: `event: top` with each of wb top --json's lines verbatim (the newest at
        once), `event: down` {why, retry} when its child ended, `: ping` after PING_EVERY of
        silence. A client that hung up is noticed within WAIT_LOOK, so the child's linger starts."""
        feed = self.top
        if not feed.slots.acquire(blocking=False):
            return self._json(503, {"error": "too many live views open"})
        feed.join()
        try:
            if hasattr(socket, "TCP_USER_TIMEOUT"):  # a peer gone silent fails the next write
                try:
                    self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT, UNACKED_MS)
                except OSError:
                    pass
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self._security_headers()
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.wfile.flush()
            seen, sent = -1, time.monotonic()
            while True:
                with feed.cond:
                    feed.cond.wait_for(lambda: feed.seq != seen, timeout=WAIT_LOOK)
                    changed, seen, event = feed.seq != seen, feed.seq, feed.event
                if changed and event:
                    data = event[1] if event[0] == "top" else json.dumps(feed.down())
                    self.wfile.write(f"event: {event[0]}\ndata: {data}\n\n".encode())
                    self.wfile.flush()
                    time.sleep(NUDGE_AFTER)  # the event's tail out of WebKit's buffer, as _stream's
                    self.wfile.write(b"\n")
                    self.wfile.flush()
                    sent = time.monotonic()
                elif self._gone():
                    return
                elif time.monotonic() - sent >= PING_EVERY:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    sent = time.monotonic()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            feed.leave()
            feed.slots.release()

    def _get_upload(self, path: str) -> None:
        """GET /api/upload/<id>: whether it is stored ({upload}, else 404), so a page asks before it
        sends a byte again; GET /api/file/<id>: the file, an image as itself, anything else as a
        download."""
        kind, _, uid = path[len("/api/"):].partition("/")
        if self.agent and not agent_may_read(uid, self.agent):  # not its own: as if none (#1042)
            return self._json(404, {"error": "no such upload" if kind == "upload" else "no such file"})
        if kind == "upload":
            m = upload_meta(uid)
            return self._json(200, {"upload": {k: m.get(k) for k in ATTACHED}}) if m \
                else self._json(404, {"error": "no such upload"})
        got = upload_file(uid)
        if got is None:
            return self._json(404, {"error": "no such file"})
        f, m = got
        with f:
            size = os.fstat(f.fileno()).st_size
            self.send_response(200)
            shown = m.get("type") in SHOWN
            self.send_header("Content-Type", m["type"] if shown else "application/octet-stream")
            self.send_header("Content-Length", str(size))
            if not shown:
                self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(m['name'])}")
            # an upload's bytes never change under its id: the browser may keep them, so the
            # thread's photos aren't all fetched whole again on every load (#754)
            self.send_header("Vary", "Authorization")
            self._security_headers(FILE_CACHE)
            self.end_headers()
            shutil.copyfileobj(f, self.wfile, 1 << 16)

    def do_PUT(self):
        """PUT /api/upload/<id>: the file's bytes as the body, its name (URI-encoded) in
        X-File-Name, its type in Content-Type. 200 {upload, new}; the same id again stores nothing
        and answers with the first. Refusals say why, for the page's chip (#623)."""
        if not self._allowed():
            return
        self._versioned()
        if not self._gate("PUT", self.path.split("?")[0]):
            return
        if not routed("PUT", self.path):
            return self._json(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length") or -1)
        except ValueError:
            n = -1
        if n < 0:
            return self._json(411, {"error": "an upload needs its Content-Length"})
        try:
            meta, new = save_upload(self.path[len("/api/upload/"):], unquote(self.headers.get("X-File-Name") or ""),
                                    self.headers.get("Content-Type") or "", n, self.rfile, by=self.agent)
        except UploadError as e:
            return self._refuse(e.code, e.why)
        except (TimeoutError, ConnectionError):  # the line dropped mid-body: nothing was kept
            self.close_connection = True
            return
        except OSError as e:  # the disk refused
            return self._refuse(507 if e.errno in (errno.ENOSPC, errno.EDQUOT) else 500,
                                f"not saved: {e.strerror or e}", final=e.errno in FINAL_ERRNOS)
        self._json(200, {"upload": {k: meta.get(k) for k in ATTACHED}, "new": new})

    def _transcribe(self, uid: str) -> None:
        """POST /api/transcribe/<id>: 200 {text}; 404 no such upload or no listener, 415 not
        audio, 409 the GPU is a task's (`busy: true`), 503 a url listener nothing answers at
        (`retry: true`), 502 the listener failed. The page keeps a clip through a 409 or a 503
        and asks again (#1203); no GPU listener ever waits for the GPU here."""
        from . import audio
        try:
            return self._json(200, {"text": transcribe_upload(uid)})
        except UploadError as e:  # a 415 here is the upload's type, not the request's (json_only)
            return self._json(e.code, {"error": e.why, **({"code": "not_audio"} if e.code == 415 else {})})
        except audio.Busy as e:
            return self._json(409, {"error": "the GPU is busy with a task", "busy": True, "why": str(e)})
        except audio.Down as e:
            _say(f"the listener is down: {e}")
            return self._json(503, {"error": f"the listener isn't answering: {e}", "retry": True})
        except LookupError as e:
            return self._json(404, {"error": f"dictation is off: {e}"})
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as e:
            _say(f"the listener failed: {e}")
            return self._json(502, {"error": f"the listener failed: {e}"})

    def _refuse(self, code: int, why: str, final: bool = False) -> None:
        try:
            self._json(code, {"error": why, **({"final": True} if final else {})})
        except OSError:  # the page went away meanwhile
            self.close_connection = True
            return
        self._linger()

    def _linger(self) -> None:
        """After answering a body it didn't read: stop sending, then take what is still coming for a
        moment, so the refusal reaches the page rather than a reset that would lose it."""
        self.close_connection = True
        try:
            self.wfile.flush()
            self.connection.shutdown(socket.SHUT_WR)
            self.connection.settimeout(2)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and self.connection.recv(1 << 16):
                pass
        except OSError:
            pass

    def do_POST(self):
        if not self._allowed():
            return
        self._versioned()
        if not self._gate("POST", self.path.split("?")[0]):
            return
        if not routed("POST", self.path):
            return self._json(404, {"error": "not found"})
        key = (self.headers.get("Idempotency-Key") or "").strip() if self.path == "/api/send" else ""
        if key and not IDEM_KEY.fullmatch(key):
            return self._json(400, {"error": "an Idempotency-Key is 1 to 255 printable characters, no spaces",
                                    "code": "bad_idempotency_key"})
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
        if key:  # one key, one send: held while it sends, so a retry racing the first waits for it
            with store.locked(chat_dir() / "idempotency.lock"):
                return self._send_once(body, text, cid, key)
        if self.path == "/api/send":
            return self._send_message(body, text, cid)
        if self.path.startswith("/api/transcribe/"):  # a voice clip's words, for the box (#1203)
            return self._transcribe(self.path[len("/api/transcribe/"):])
        if self.path == "/api/seen":  # what the page showed: no push for it (endpoint: to it alone)
            ids, endpoint = body.get("ids"), body.get("endpoint")
            if not (isinstance(ids, list) and len(ids) <= 200
                    and all(isinstance(x, str) and 0 < len(x) <= 64 for x in ids)) \
                    or not (endpoint is None or webpush.accepted(endpoint)):
                return self._json(400, {"error": "seen is {ids: [up to 200 ids], endpoint?: this device's push URL}"})
            mark_seen(ids, "page", endpoint)
            return self._json(200, {})
        if self.path == "/api/active":  # a page on screen and in use: the user is on this device (#959)
            if body:
                return self._json(400, {"error": "active is {}"})
            # the web app is the desk's admin too (Settings and Stats, from the modeline's link):
            # used there, the user at the terminal was made away, their next plan or question
            # refused into the chat (#1374, 2026-11-25)
            active(desk=self._desk())
            return self._json(200, {})
        if self.path == "/api/push":  # this device's notifications: on, off, or a test
            if body.get("test") is True:
                store.write_json(chat_dir() / "push-test.json", {"ts": store.now()})
                sent = webpush.push_all(webpush.private_key(token_path()))
                got = [_result(v) for v in sent.values()]
                return self._json(200, {"sent": len(sent), "ok": sum(1 for c, _ in got if 200 <= c < 300),
                                        "hint": push_hint(sent),
                                        "reasons": sorted({w for c, w in got if w and not 200 <= c < 300})})
            endpoint, on = body.get("endpoint"), body.get("on")
            if not webpush.accepted(endpoint) or not isinstance(on, bool):
                return self._json(400, {"error": "push is {endpoint: a push service's https URL, on: true|false}"})
            (webpush.subscribe(endpoint, self.headers.get("User-Agent") or "") if on
             else webpush.unsubscribe(endpoint))
            return self._json(200, {"on": on})
        if self.path == "/api/settings":  # one of the web app's Settings, through its wb command
            return self._json(*admin_set(body.get("key"), body.get("value")))
        return None

    def _send_once(self, body: dict, text: str, cid, key: str) -> None:
        """POST /api/send with an Idempotency-Key (#1138): the first answer's message again for a
        key this caller sent within IDEM_FOR (header `Idempotent-Replayed: true`), sending nothing;
        422 for the same key on another body; else sent, and the key recorded."""
        k, req = idem_key(self.agent or "user", key), idem_request(body)
        if (had := idem_lookup(k)) and (msg := message(had["id"])):
            if had.get("req") != req:
                return self._json(422, {"error": "that Idempotency-Key went with another request in the "
                                                 "last 24 hours: use a new key for a new request"})
            data = json.dumps({"message": msg, "listener": listener_view(listener()), "state": self._snap()}).encode()
            return self._send(200, data, "application/json", {"Idempotent-Replayed": "true"})
        sent = self._send_message(body, text, cid)
        if sent:
            idem_record(k, req, sent["id"])

    def _send_message(self, body: dict, text: str, cid) -> dict | None:
        """POST /api/send: the message sent (and answered), else None (answered with the refusal)."""
        if self.agent and (wait := self.sends.hit(self.agent)):  # a replay above costs none of these
            return self._json(429, {"error": f"over {AGENT_SENDS_PER_MIN} sends a minute", "retry_after": wait})
        if self.agent and body.get("about") is not None:  # what it answers is the user's (#1042)
            return self._json(400, {"error": "agent messages can't carry about"})
        files = body.get("files")
        if self.agent and isinstance(files, list):  # only its own uploads: another's is no upload to it (#1042)
            if bad := next((i for i in files if isinstance(i, str) and (upload_meta(i) or {}).get("by") != self.agent), None):
                return self._json(400, {"error": f"no upload {bad[:64]}: it must be uploaded before the message"})
        if files is not None:  # attachments, by the ids they were uploaded with (#623)
            try:
                files = attachments(files)
            except UploadError as e:
                return self._json(e.code, {"error": e.why})
        if (not text and not files) or len(text) > MAX_TEXT:
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
        elif isinstance(about, dict) and set(about) == {"task"}:  # from the task sheet (#1201)
            t = store.Task(about["task"]) if isinstance(about["task"], str) and store.NAME_RE.match(about["task"]) else None
            if not t or not t.exists or t.spec.get("bench"):
                return self._json(400, {"error": "no such task"})
            about = {"task": t.name}
        elif about is not None:  # a reply to a pair of screenshots: its task and step, from the pair
            pair = shots(about.get("task"), about.get("n")) if isinstance(about, dict) else None
            if not pair:
                return self._json(400, {"error": "no such screenshots"})
            about = {k: pair[k] for k in ("task", "n", "step", "round")}
        try:
            msg = send(text, self.agent or "user", cid, about=about, files=files, role="agent" if self.agent else None)
        except ValueError as e:
            return self._json(409, {"error": str(e)})
        self._json(200, {"message": msg, "listener": listener_view(listener()), "state": self._snap()})
        return msg


def config_stamp() -> tuple | None:
    """chat.json's (mtime_ns, size, inode), None when it is missing: a change rereads `hosts`."""
    try:
        st = config_path().stat()
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size, st.st_ino


def plan() -> dict:
    """Where to listen and which Host names to answer: {"binds": [BIND], "hosts" (None: any)}.
    Localhost only (#1616, 2026-11-27): a tunnel or proxy of the user's own points at it, and
    passes its name as the Host, which chat.json's `hosts` adds (`*` for any)."""
    extra = [h.lower() for h in settings().get("hosts") or []]
    return {"binds": [BIND], "hosts": None if "*" in extra else set(LOCAL_NAMES) | set(extra)}


class _Server(ThreadingHTTPServer):
    def server_bind(self):
        # HTTPServer's own also looks the address's host name up (socket.getfqdn), which nothing
        # here reads: on macOS that lookup stalled the start past wb chat on's wait (#236)
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


def make_servers(port: int) -> tuple[list[ThreadingHTTPServer], dict]:
    """The server on 127.0.0.1 (plan's), in a list."""
    stamp = config_stamp()  # before the read: a change between them is read again
    p = plan()
    handler = type("ChatHandler", (Handler,), {
        "hosts": p["hosts"], "hosts_seen": stamp, "secret": token(), "hub": Hub(),
        "streams": threading.BoundedSemaphore(MAX_STREAMS), "waits": threading.BoundedSemaphore(MAX_WAITS),
        "sends": RateLimit(AGENT_SENDS_PER_MIN), "top": TopFeed()})
    servers = []
    for addr in p["binds"]:
        try:
            srv = _Server((addr, port), handler)
        except OSError as e:  # one line, not a traceback (the 2026-11-16 audit)
            for s in servers:
                s.server_close()
            why = (f"the port is taken: another wb chat serve ({service_hint('status')}) or another "
                   "program; wb chat on <port> picks another" if e.errno == errno.EADDRINUSE else e.strerror or str(e))
            raise RuntimeError(f"can't listen on {addr}:{port}: {why}") from None
        srv.daemon_threads = True
        servers.append(srv)
    return servers, p


def serve(port: int, say=print) -> None:
    servers, p = make_servers(port)
    # the port it runs on, for `wb chat url` and the pane's link: they asked without one and got
    # the default, wrong for a server on the free port the CLI advises (#349, the audit of 1.4)
    store.write_json(chat_dir() / "port.json", {"port": servers[0].server_address[1] if servers
                                                else port, "pid": os.getpid(),
                                                "start": _started(os.getpid())})
    for srv in servers[1:]:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    say(f"web chat on {', '.join(f'{s.server_address[0]}:{port}' for s in servers)}")
    if p["hosts"] is None:
        say("answering to any host name (hosts: *): the token is the only lock")
    def restart(why: str):  # the same command again, in this process: systemd and launchd see
        say(why)            # no exit, and a server wb chat on started has no supervisor to rerun it
        os.execv(sys.executable, [sys.executable, *sys.orig_argv[1:]])
    def move(new: Path):  # OpenCode installed a newer generation beside this one's (#1525)
        say(f"a newer workbench is installed at {new}: restarting from it")
        os.chdir(new)
        os.execv(sys.executable, serve_argv(new))
    threading.Thread(target=watch_code, args=(
        lambda: restart("the web chat's code changed: restarting with it"),),
        kwargs={"move": move}, daemon=True).start()
    threading.Thread(target=Pusher(servers[0].RequestHandlerClass.hub).run, daemon=True).start()
    servers[0].serve_forever()


def _stamp(d: Path) -> tuple:
    return tuple(sorted((f.name, f.stat().st_mtime_ns, f.stat().st_size) for f in d.glob("*.py")))


def code_stamp() -> tuple:
    return _stamp(Path(__file__).parent)


def newer_generation(root: Path | None = None) -> Path | None:
    """The newest generation of OpenCode's Git install when ``root`` (this package's, by default)
    is an older one of the same key: `<cache>/<key>/<n>/node_modules/opencode-workbench`, the
    highest `<n>` not `<n>.rejected` that holds this package and its bin/wb. None for the newest,
    and for a root outside OpenCode's cache (a clone, Claude Code's marketplace clone: updated in
    place, which code_stamp sees). A new commit there is a new directory beside the old, and a
    server started from one served its page and Settings until OpenCode removed it, then exited
    restarting into the missing bin/wb (#1525, 2026-11-26)."""
    from . import ocplugin  # only here: the server needs it for this alone
    root = root or Path(__file__).resolve().parent.parent
    mine = root.parent.parent.name
    if root.parent.name != "node_modules" or not mine.isdigit() or not ocplugin.runs_from_cache(root):
        return None
    key = root.parent.parent.parent
    try:
        names = os.listdir(key)
    except OSError:
        return None
    for g in sorted((n for n in names if n.isdigit() and f"{n}.rejected" not in names), key=int, reverse=True):
        d = key / g / "node_modules" / root.name
        if ocplugin.is_copy(d) and (d / "bin" / "wb").is_file():
            return d if int(g) > int(mine) else None
    return None


def serve_argv(root: Path) -> list[str]:
    """This server's command run from ``root``'s bin/wb: another generation's (#1525)."""
    return [sys.executable, "-X", "utf8", str(root / "bin" / "wb"), *sys.argv[1:]]


def watch_code(restart, every: float = RELOAD_EVERY, move=None, root: Path | None = None) -> None:
    """Restart once the package's code has changed and settled (a pull writes files one by one):
    a server left running across an update served the new page from old code for days, and no
    doc's `systemctl --user restart wb-chat` was ever run (2026-11-18). With ``move``, a server
    run from an older generation of OpenCode's install (newer_generation of ``root``) calls
    ``move(<the newest's root>)`` once that one has settled too (#1525)."""
    first = prev = code_stamp()
    seen = None
    while True:
        time.sleep(every)
        if move is not None:
            new = newer_generation(root)
            try:
                mark = (new, _stamp(new / "workbench")) if new else None
            except OSError:  # mid-install, or collected
                mark = None
            if mark is not None and mark == seen:
                return move(new)
            seen = mark
        try:
            now = code_stamp()
        except OSError:  # a file mid-replace
            continue
        if now != first and now == prev:
            return restart()
        prev = now


def _valid_port(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 65535


def chosen_port() -> int:
    """The user's port: the one `wb chat on <port>` kept in chat/switch.json `port`, else PORT. The
    server (`wb chat serve` with no --port, as the service runs it) and the link take it (#1616)."""
    doc = store.read_json(chat_dir() / "switch.json", None)
    v = doc.get("port") if isinstance(doc, dict) else None
    return v if _valid_port(v) else PORT


def set_port(port: int) -> None:
    """Keep ``port`` as the user's (chosen_port), beside the switch."""
    doc = store.read_json(chat_dir() / "switch.json", None)
    doc = doc if isinstance(doc, dict) else {"on": True, "v": SWITCH_V}
    store.write_json(chat_dir() / "switch.json", {**doc, "port": int(port)})


def port() -> int:
    """The port the running server recorded (chat/port.json), else the user's (chosen_port) once
    they chose one, else the one `wb chat on` last started a server on (chat/serve.pid: gone after
    a reboot or a crash, it comes back there, #449), else PORT: so a command or the pane that names
    no port finds a server started on another (#349, the audit of 1.4)."""
    st = store.read_json(chat_dir() / "port.json", None) or {}
    if isinstance(st.get("port"), int) and st.get("pid") and _alive(st["pid"], st.get("start")):
        return st["port"]
    doc = store.read_json(chat_dir() / "switch.json", None)
    if isinstance(doc, dict) and _valid_port(doc.get("port")):
        return doc["port"]
    started = (store.read_json(chat_dir() / "serve.pid", None) or {}).get("port")
    return started if isinstance(started, int) else PORT


def url(port: int) -> str:
    """The one link to open: localhost, the token in the fragment. Reaching it from another
    device is a tunnel or proxy of the user's own to localhost: they change the host in the link."""
    return f"http://localhost:{port}/#token={token()}"
