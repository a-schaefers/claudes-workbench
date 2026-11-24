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
import hashlib
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
from urllib.parse import parse_qs, quote, unquote, urlsplit

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
# Tailscale for Windows' CLI under WSL's automount root, for a chat whose PATH lacks Windows' (#920)
TAILSCALE_WINDOWS = "c/Program Files/Tailscale/tailscale.exe"
API_VERSION = 1         # GET /api/state's and /api/health's `api`: bumped by a breaking change (docs/chat-api.md)
AGENT_SENDS_PER_MIN = 30  # an agent token's POST /api/send per minute, then 429 with retry_after
WAIT_DEFAULT = 300      # GET /api/wait's timeout when none is given, in seconds
WAIT_MAX = 600          # and the most it may ask for
MAX_WAITS = 16          # /api/wait calls held at once (each holds a thread, as a stream does)
WAIT_LOOK = 1           # seconds between a held wait's looks at its client: one gone frees the slot
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


# ---- agent tokens (#1042) ----
# Other agents (another harness driving Claude through workbench) get tokens of their own: `wb chat
# token add <name>` prints one once and keeps only its sha256 in chat-agents.json, beside the
# user's token file (0600). An agent token reaches what a teammate needs (send, upload, read the
# state and the stream, wait for replies) and never what only the user may do: Claude Code's
# dialogs, the page's settings, push, and marking the user away (AGENT_DENIED, 403). Its messages
# are stored `from: <name>, role: "agent"` and reach Claude as `[agent <name>] …`, never with the
# user's authority. Revoked, it is 401 at once (the file is read per request).
AGENT_NAME = re.compile(r"[a-z0-9-]{1,32}")
AGENT_RESERVED = ("user", "claude", "notice", "agent", "wb")  # `from` values the thread already uses
AGENT_PREFIX = "wba-"  # how an agent token looks, so a leaked one is recognised
AGENT_DENIED = {"/api/answer": "answer Claude Code's dialogs",
                "/api/prefs": "change the user's settings",
                "/api/push": "change notifications",
                "/api/active": "mark the user away",
                "/api/seen": "mark what the user has seen"}


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


def tailscale(windows: bool = False) -> dict:
    """This machine on the tailnet: {"ip", "names"}; {} when Tailscale isn't up (no Self, or a
    BackendState other than Running); {"missing": True} when there is no CLI to ask (said apart
    from "not running": the 2026-11-16 audit). The ip is the IPv4 one, else the first IPv6: a node
    with IPv4 turned off was reported "not running" and the chat bound localhost alone (#937).
    ``windows``: under WSL with no Linux CLI, Windows' tailscale.exe (Tailscale's own setup for
    WSL2), for the name `tailscale serve` passes in through Windows' localhost forwarding: `which`
    never tries .exe, so the chat answered it 403 "unknown host" (#920). From PATH, else its
    install under the automount root: wb-chat.service runs with the user manager's PATH, which
    has none of Windows', as has appendWindowsPath=false (the review of #920). Not for the
    address: in WSL's NAT networking, Windows' tailnet IP isn't on WSL's interfaces."""
    cli = shutil.which("tailscale") or (TAILSCALE_APP if sys.platform == "darwin"
                                        and os.path.isfile(TAILSCALE_APP) else None)
    if not cli and windows:
        from . import hardware
        cli = (hardware.windows_program("tailscale.exe", TAILSCALE_WINDOWS)
               if hardware.system() == "wsl" else None)
    if not cli:
        return {"missing": True}
    try:
        r = subprocess.run([cli, "status", "--json"], capture_output=True, text=True, timeout=5)
        status = json.loads(r.stdout)
        me = status["Self"]
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
        return {}
    if status.get("BackendState", "Running") != "Running":  # stopped or logged out: not up
        return {}
    ips = sorted(me.get("TailscaleIPs") or [], key=lambda ip: ":" in ip)  # IPv4 first
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
            _tailnet.update(at=time.monotonic(), name=tailscale(windows=True).get("dns") or "")
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
            if _inbound(r) and r.get("id") and r["id"] not in got}
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
    """What still waits: {messages, asks}, each {id: row}. A message past MESSAGE_EXPIRE leaves
    it here (`_retire`)."""
    cur = store.read_json(_open_path(), None)
    if not (isinstance(cur, dict) and isinstance(cur.get("messages"), dict) and isinstance(cur.get("asks"), dict)):
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
    `expired` once unread past MESSAGE_EXPIRE: no session will be given it (#536). ``keep``: only
    the rows it passes (an agent's view, #1042)."""
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
        elif r["from"] == "notice" and r["id"] in receipts:  # its dialog answered in the terminal (#959)
            r = {**r, "answered": receipts[r["id"]].get("ts")}
        out.append(r)
    return out


# Work still waiting is kept apart from what the page shows: an open question vanished from the
# page's list behind 20 newer answered ones, and `wb chat pending` lost an unread message behind
# 200 newer replies (#265, the audit of 1.2). An ask open past ASK_STALE is marked `stale` (the
# mod waits 30 minutes on one). A message of the user's unread past MESSAGE_EXPIRE is `expired`:
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
# (the turn that wrote it, or a later one).

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
    review)."""
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
    parent, me = lst.get("parent"), os.environ.get("CLAUDE_PID", "")
    if not isinstance(parent, int) or isinstance(parent, bool) or not parent or not me.isdigit():
        return True
    if isinstance(lst.get("machine"), str) and lst["machine"] and lst["machine"] != store.machine():
        return False
    return int(me) == parent


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
    whose = list(dict.fromkeys(["user"] * bool(to and "user" in to) + [
        "user" if rows[i].get("from") == "user" else str(rows[i].get("from")) for i in ids]))
    return send(text, "claude", re=ids, to=whose, **extra)


def waiting_on_user() -> bool:
    """Whether Claude is paused on the user: a dialog open in the chat (an ask) or one the
    terminal shows that the phone was told of (a notice not yet answered), under ASK_STALE old."""
    now = store.now()
    if any(a.get("answer") is None and not a.get("stale") for a in asks()):
        return True
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
    caller), done (a turn has ended since the newest of those, else since ``after``: one that took
    it, for a message to Claude), and expired: True for a message to Claude unread past
    MESSAGE_EXPIRE with no reply}; None when ``after`` is in no recent part of the thread."""
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
    done = (any(me["id"] in (t.get("ids") or []) for t in turns) if _inbound(me)
            else any(t.get("ended", 0) >= me.get("ts", 0) for t in turns))
    return {"replies": replies, "done": done}


# ---- an agent's view (#1042): its own conversation, nothing of the user's ----
# Round one let an agent token read the whole state and stream: the user's own messages, Claude's
# replies to the user, Claude Code's dialogs, the settings. An agent reads only its own messages
# and Claude's rows whose `to` names it (`in_view`), the listener, the upload limits and the
# clock; files only of its own uploads (`by`) or attached to rows in its view; never an ask, a
# screenshot, an audio take or a push's summary (AGENT_UNREAD, 403).
AGENT_UNREAD = {"/api/latest": "read the user's notifications",
                "/api/ask/": "read Claude Code's dialogs",
                "/api/shot": "read screenshots",
                "/api/audio": "read audio takes"}


def in_view(row: dict, agent: str) -> bool:
    """Whether ``row`` is in ``agent``'s view: its own message, or Claude's to it."""
    return isinstance(row, dict) and ((row.get("role") == "agent" and row.get("from") == agent)
                                      or addressed(row, agent))


def agent_snapshot(agent: str) -> dict:
    """What GET /api/state (and the stream) carries for an agent's token."""
    return {"api": API_VERSION, "messages": thread(keep=lambda r: in_view(r, agent)), "listener": listener(),
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


# The page's state is sent over and over, so a settled ask travels without its bulk: an answered
# (or stale) plan as its first PLAN_PREVIEW characters with `cut`, the rest fetched from GET
# /api/ask/<id> when the user unfolds it; a settled question as its questions' text, all the page
# shows of it. Thirty answered plans were most of a 922 KB state (#755).
PLAN_PREVIEW = 1200


def page_ask(a: dict) -> dict:
    """``a`` as the page's state carries it: an open one whole, a settled one lean."""
    d = a.get("detail")
    if not isinstance(d, dict) or (a.get("answer") is None and not a.get("stale")):
        return a
    lean = dict(d)
    if isinstance(d.get("plan"), str) and len(d["plan"]) > PLAN_PREVIEW:
        lean.update(plan=d["plan"][:PLAN_PREVIEW], cut=True)
    if isinstance(d.get("questions"), list):
        lean["questions"] = [{"question": q.get("question")} for q in d["questions"] if isinstance(q, dict)]
    return {**a, "detail": lean}


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
    return {"api": API_VERSION, "messages": thread(), "asks": [page_ask(a) for a in asks()], "listener": listener(), "prefs": prefs(),
            "push": {"key": _push_key_or_none(), "test": (store.read_json(chat_dir() / "push-test.json", None)
                                                 or {}).get("ts")},
            "savings": week_saved(), "uploads": upload_limits(), "build": build(), "expire": MESSAGE_EXPIRE,
            "tasks": open_tasks(), "now": store.now()}


def open_tasks() -> list[dict]:
    """The open tasks for the page's strip over the thread (#1016): while away, the chat is all
    the user sees, and it named no model (robustness minor 8). From task.json and status.json:
    `{name, phase, model, via, provider_name}`; a bench replay is no one's task, and a parked one
    (`wb stop --all`) is left out as `wb status` leaves it: the quit's tasks filled the strip for
    good (#1097, 2026-11-23)."""
    out = []
    for t in store.all_tasks():
        sp, phase = t.spec, t.status.get("phase")
        if not sp.get("name") or sp.get("bench") or phase in store.CLOSED or store.parked(t.status):
            continue
        out.append({"name": t.name, "phase": phase or "queued", "model": sp.get("model") or "",
                    "via": sp.get("via") or "gpu", "provider_name": sp.get("provider_name") or ""})
    return out


LATEST_BYTES = 1 << 20  # the messages' tail `latest` reads for Claude's last word
# the messages that are news for the user: Claude's, and a notice that a dialog waits in the
# terminal (`notice`, #959)
NEWS = ("claude", "notice")


def latest() -> dict:
    """What a push shows, for the service worker (GET /api/latest): the newest open ask (a plan's
    text left out: the notification says only that one is ready) and Claude's last message, in
    the snapshot's shape. It fetched the whole /api/state, the thread and 20 asks with their
    plans, up to a megabyte, within the time iOS gives a push handler (#755)."""
    open_ = [a for a in asks() if a.get("answer") is None and not a.get("stale")]
    ask = max(open_, key=lambda a: a.get("ts", 0), default=None)
    if ask and ask.get("kind") == "plan":
        ask = {k: v for k, v in ask.items() if k != "detail"}
    rows = store.read_jsonl_tail(chat_dir() / "messages.jsonl", LATEST_BYTES)
    # a notice whose dialog was answered in the terminal is no news (#959)
    answered = {r.get("id") for r in store.read_jsonl_tail(chat_dir() / "receipts.jsonl", LATEST_BYTES)
                if r.get("how") == "answered"}
    last = next((r for r in reversed(rows) if r.get("from") in NEWS and _for_user(r) and r.get("id") not in answered), None)
    return {"now": store.now(), "asks": [ask] if ask else [], "messages": [last] if last else [],
            "push": {"test": (store.read_json(chat_dir() / "push-test.json", None) or {}).get("ts")}}


LISTS = ("messages", "asks")  # what a patch sends row by row; the rest, small, it sends whole


def patch(sent: dict, snap: dict) -> dict | None:
    """What a stream sends after its first state (#755): the rows of `messages` and `asks` new or
    changed since the state ``sent`` records (updated in place) as `{put, drop}` (the ids gone),
    and every other field whole (a kilobyte: the listener's busy, the prefs, the push key...), or
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


def week_saved() -> dict:
    """The gear sheet's Savings: `wb savings` for the last 7 days (savings.totals)."""
    since = store.now() - savings.PERIODS["week"]
    t = savings.totals(store.results(), since=since)
    return {**t, "score": savings.score(t), "api": _week_api(since)}  # the badge (#101)


def _week_api(since: float) -> dict:
    """The week's API tasks for the gear (#1022): savings.api_totals, each provider's name as
    OpenCode gives it in `names`; never part of the score."""
    a = savings.api_totals(store.results(trials=True), since)
    return {**a, "names": savings.provider_labels(a["by_provider"])}


# ---- notifications: web push with no payload (webpush.py) ----
# The page reports what it showed while visible (/api/seen); news no page showed within
# PUSH_DELAY gets one push to every subscribed device, whose service worker then fetches `/api/latest`
# and shows the newest open ask, else Claude's latest message. seen.jsonl: {id, ts, by: page|push,
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
    """Ids of Claude's messages and open asks old enough to push and recent enough to matter,
    not yet shown or pushed."""
    now = now or store.now()
    done = _seen()
    items = [(m["id"], m["ts"]) for m in thread() if m.get("from") in NEWS and _for_user(m)]
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


def ask(tool: str, text: str, kind: str = "permission", detail: dict | None = None,
        pid: int | None = None, for_: list[str] | None = None, here: bool = False) -> dict:
    """A new ask. A permission while the user's setting says `allow` is answered at once, by
    `setting`, and stays in the thread so they see what ran. ``pid``: the asking session's `wb
    events` process, which lives exactly as long as the session; once it is gone the ask expires.
    ``for_``: the agents whose request Claude is working on (#1042: the page says "for <name>");
    ``here``: the user is at the terminal, so their away setting doesn't answer it."""
    if kind not in KINDS:
        raise ValueError(kind)
    names = list(dict.fromkeys(for_ or []))
    if not all(isinstance(n, str) and AGENT_NAME.fullmatch(n) for n in names):
        raise ValueError("--for is an agent's name")
    row = {"id": f"ask-{int(time.time() * 1000)}-{secrets.token_hex(4)}", "ts": store.now(),
           "tool": tool[:200], "text": text[:ASK_TEXT], "kind": kind}
    if detail is not None:
        row["detail"] = fit_detail(detail)
    if pid:
        row["pid"] = int(pid)
    if names:
        row["for"] = names
    store.append_jsonl(chat_dir() / "asks.jsonl", row)
    _open_update(lambda w: w["asks"].__setitem__(row["id"], row))
    if kind == "permission" and prefs()["away"] == "allow" and not here:
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
    for a in out:
        if a["answer"] is None and a.get("pid") and not _alive(a["pid"]):
            _orphaned(a)
    return [{**a, "stale": True} if a["answer"] is None and now - a["ts"] > ASK_STALE else a
            for a in out]


def _orphaned(a: dict) -> None:
    """Expire an open ask whose session is gone (its pid), by `interrupted`, in place. A Bash card
    left by a crashed or closed session stayed open for good: answerable for an hour though nobody
    waited, then "it waits in the terminal", and every later push showed it (#538)."""
    row = {"id": a["id"], "ts": store.now(), "answer": "expired", "by": "interrupted"}
    store.append_jsonl(chat_dir() / "asks.jsonl", row)
    _answered({k: v for k, v in a.items() if k not in ("answer", "stale")}, row)
    a.update(answer="expired", answered=row["ts"], by="interrupted")


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


# ---- which device the user last used (#959) ----
# Away was where the user last typed: a web message made them away, typing in the terminal
# brought them back. The owner typed in the terminal, then picked up the phone with the chat open
# and "Let Claude go ahead" set; a permission prompt came up in the terminal, never reached the
# phone and wasn't allowed (2026-11-21, #959). So using the page counts as using the phone: a page
# on screen and in use posts /api/active, and that marks the user away as a web message does
# (chat/away.json's `at`, which the mod reads), until they type in the terminal again or answer a
# dialog there (the mod's `back`). chat/page.json `{at}`: when a page was last in use, so a dialog
# at the terminal is announced only to a user who uses the chat: a page in use within
# PAGE_RECENT, or push on.
PAGE_RECENT = 86400
NOTICE_WHAT = {"permission": "a permission", "plan": "a plan", "question": "a question"}


def active() -> float:
    """A chat page is in use now: recorded in page.json, and, with the chat on, in away.json, as
    a web message would be. Off, no session sends a dialog here, and turning it off cleared away:
    a page used meanwhile doesn't make the user away once it is turned on again."""
    now = store.now()
    store.write_json(chat_dir() / "page.json", {"at": now})
    if is_on():
        store.write_json(chat_dir() / "away.json", {"at": now, "by": "page"})
    return now


def page_used() -> float:
    """When a chat page was last in use (seconds), 0 for never."""
    try:
        return float((store.read_json(chat_dir() / "page.json", None) or {}).get("at") or 0)
    except (TypeError, ValueError, AttributeError):
        return 0.0


def notice(tool: str, what: str, kind: str = "permission") -> dict | None:
    """Tell the phone that a dialog waits in the terminal while the user isn't away (#959): a
    `notice` row in the thread, with no buttons (only the terminal can answer it), pushed as
    Claude's messages are. None, and nothing written, for a user who doesn't use the chat: no
    page in use within PAGE_RECENT and no device with push on."""
    if kind not in KINDS:
        raise ValueError(kind)
    if not webpush.subscriptions() and store.now() - page_used() > PAGE_RECENT:
        return None
    return send(f"Claude is waiting on {NOTICE_WHAT[kind]} in the terminal", "notice",
                tool=tool[:200], what=what[:ASK_TEXT])


def notice_done(msg_id: str) -> bool:
    """The notice's dialog was answered in the terminal (#959): no push is owed for it (one not yet
    due is never sent), and the thread shows it answered (a receipt `{id, ts, by: "terminal", how:
    "answered"}`). Unsettled, every prompt approved at the terminal in two seconds still buzzed
    the phone ten seconds later, saying it waited (the #959 review). False for an id that is no
    notice in the thread."""
    if not any(r.get("id") == msg_id and r.get("from") == "notice"
               for r in store.read_jsonl_tail(chat_dir() / "messages.jsonl", LATEST_BYTES)):
        return False
    mark_seen([msg_id], "terminal")
    store.append_jsonl(chat_dir() / "receipts.jsonl",
                       {"id": msg_id, "ts": store.now(), "by": "terminal", "how": "answered"})
    return True


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


# What keeps the chat up: systemd's wb-chat.service on Linux, launchd's workbench.chat agent on
# macOS (launchd/workbench.chat.plist). Only systemd was known: on a Mac `wb chat off` and the
# port-taken error gave systemctl advice, and revive ignored the agent (#756).
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
    session; a listener.json without a pid falls back to its age. A claim another machine wrote
    (`machine`, on a shared state directory) listens, as the mod has it: its pid means nothing in
    this process table, and the page said "Waiting for a Claude session" while that machine's
    session got every message (the review of #922). The mod writes the file only on a change, so
    its age can't say either."""
    st = store.read_json(chat_dir() / "listener.json", None)
    if not st:
        return None
    if isinstance(st.get("machine"), str) and st["machine"] and st["machine"] != store.machine():
        return st
    if st.get("pid"):
        return st if _alive(st["pid"], st.get("start")) else None
    return st if store.now() - st.get("ts", 0) <= LISTENING_FOR else None


IDLE_BUMP_S = 60  # without inotify, nothing changed: the streams wake this often all the same (#841)


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
                if not w.native or w.polled:  # no inotify, or chat/ fell back to looking
                    # a wake only for a change, the listener's process ending (`kill 0`, no `ps`)
                    # or IDLE_BUMP_S, as the native select's timeout: every open stream rebuilt
                    # its whole snapshot each second, a `ps` fork each on macOS, with nothing new
                    # (#841)
                    changed, gone = w.wait(1), False
                    if pid and pid != said_gone and not _answers(pid):
                        gone, said_gone = True, pid
                    if not changed and not gone and time.monotonic() - last < IDLE_BUMP_S:
                        continue
                else:
                    ready, _, _ = select.select([w.fd] + ([pidfd] if pidfd is not None else []), [], [], 60)
                    w.read()
                    if pidfd is not None and pidfd in ready:  # the listener is gone
                        os.close(pidfd)
                        pidfd = None
                    time.sleep(0.02)  # gather a burst of writes into one wake
                    w.read()
                last = time.monotonic()
                self.bump()
        finally:
            w.close()
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
    raw, not through settings(): a bad `bind` there raises ConfigError, and this is on every
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
# "error" stays the text, as before: the page (chat_static/app.js) and ask_claude.py show it as is,
# so moving it would have broken both; a client branches on "code". A code by status unless the
# answer names its own (ERRORS_SAID: a revoked token, an agent token from another machine, an
# unknown host, what an agent may not do, a malformed Idempotency-Key).
ERROR_CODES = {400: "bad_request", 401: "bad_token", 403: "forbidden", 404: "not_found",
               409: "conflict", 410: "expired", 411: "length_required", 413: "too_large",
               414: "too_large", 415: "json_only", 422: "idempotency_key_reused",
               429: "rate_limited", 431: "too_large", 500: "server_error", 501: "not_implemented",
               503: "busy", 507: "disk_full"}
ERRORS_SAID = ("token_revoked", "not_local", "unknown_host", "agent_forbidden", "bad_idempotency_key")
API_V1 = "/api/v1/"  # every /api/<x> is also /api/v1/<x>, the same handler (#1138)
# Every API route, by method (#1138): docs/chat-api.yaml describes exactly these, under /api/v1/,
# and a test checks it both ways and that each is a branch of its do_<method>; a route ending in
# "/" takes an id after it. A path off this list is 404 before anything else reads it
ROUTES = {"GET": ("/api/health", "/api/state", "/api/stream", "/api/wait", "/api/upload/", "/api/file/",
                  "/api/ask/", "/api/latest", "/api/shot", "/api/audio"),
          "PUT": ("/api/upload/",),
          "POST": ("/api/send", "/api/answer", "/api/prefs", "/api/seen", "/api/push", "/api/active")}
REVOKED = {"error": "this agent token was revoked: ask the user for a new one (wb chat token add)",
           "code": "token_revoked"}

class Handler(BaseHTTPRequestHandler):
    server_version = "wb-chat"
    timeout = TIMEOUT  # an idle or half-open connection holds a thread no longer than this
    hosts: set[str] | None = set()  # None: any Host (the user's `hosts: ["*"]`)
    secret = ""
    hub: "Hub"
    streams: threading.BoundedSemaphore
    waits: threading.BoundedSemaphore
    sends: RateLimit  # an agent token's sends (#1042)
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
        (the agents' denials and unreadables among them) sees the one name (#1138)."""
        if self.path.startswith(API_V1):
            self.path = "/api/" + self.path[len(API_V1):]

    def _allowed(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        if host.startswith("["):  # [v6] or [v6]:port
            host = host[1:host.find("]")] if "]" in host else host
        elif host.count(":") == 1:
            host = host.rsplit(":", 1)[0]
        if (self.hosts is not None and host not in self.hosts and not (host and host.split("%")[0] == self._local())
                and not (host and host == tailnet_name())):
            self._json(403, {"error": f"unknown host {host[:100]}: add it to chat.json's hosts",
                             "code": "unknown_host"})
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

    def _from_loopback(self) -> bool:
        """Whether the request came from this machine: the peer is a loopback address."""
        try:
            peer = str(self.client_address[0]).lower().removeprefix("::ffff:").split("%")[0]
        except (AttributeError, IndexError, TypeError):
            return False
        return peer == "::1" or peer.startswith("127.")

    def _authed(self) -> str | None:
        """Who calls: "user" for the user's token, "agent <name>" for an agent's (and ``agent``
        set, #1042); else None, answered 401."""
        auth = self.headers.get("Authorization") or ""
        got = auth[len("Bearer "):].strip() if auth.startswith("Bearer ") else ""
        self.agent = None
        if self.secret and got and hmac.compare_digest(got.encode(), self.secret.encode()):
            return "user"
        if got and (name := agent_for(got)):
            if not self._from_loopback():
                # agents are this machine's own (owner, 2026-11-23): an agent token sent from
                # another machine (a tailnet or LAN peer of a chat bound for the user's phone) is
                # refused, whatever the bind; the user's own token is unaffected
                self._json(403, {"error": "agent tokens work only from this machine (localhost)",
                                 "code": "not_local"})
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

    def _agent_unread(self, path: str) -> bool:
        """For a path an agent may not read (AGENT_UNREAD): answered here (401, or 403 for an
        agent) unless the user's token calls. True when answered."""
        if not (key := next((k for k in AGENT_UNREAD if path == k or k.endswith("/") and path.startswith(k)), None)):
            return False
        if not self._authed():
            return True
        if self.agent:
            self._json(403, {"error": f"agent tokens can't {AGENT_UNREAD[key]}", "code": "agent_forbidden"})
            return True
        return False

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
                                                     "to Claude, so send it again"})
                lst, left, held = listener(), deadline - time.monotonic(), waiting_on_user()
                paused = held if paused is None else paused
                if got["replies"] or (got["done"] and until == "done") or lst is None or left <= 0 \
                        or (held and not paused):
                    return self._json(200, {**got, "listener": lst, "waiting_on_user": held})
                looked = time.monotonic()
                while hub.version == seen and time.monotonic() - looked < PING_EVERY \
                        and (left := deadline - time.monotonic()) > 0:
                    if self._gone():  # a client that hung up holds no slot to its timeout (#1042 review)
                        return
                    with hub.cond:
                        hub.cond.wait_for(lambda: hub.version != seen, timeout=min(left, WAIT_LOOK))
        finally:
            self.waits.release()

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
        if path.startswith("/api/") and not (path in ROUTES["GET"] or path.startswith(
                tuple(r for r in ROUTES["GET"] if r.endswith("/")))):  # off ROUTES: none (#1138)
            return self._json(404, {"error": "not found"})
        if self._agent_unread(path):  # the user's alone (#1042)
            return
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
                if body is None:  # 410: dropped with its archived task, the message still names it (#821)
                    pair = shots((q.get("task") or [""])[0], int(n) if n.isdigit() else 0)
                    side = (pair or {}).get((q.get("side") or [""])[0])
                    gone = isinstance(side, dict) and side.get("pruned")
                    self._json(410 if gone else 404, {"error": "expired" if gone else "no such screenshot"})
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
        if path.startswith(("/api/upload/", "/api/file/")):  # an attachment (#623)
            if self._authed():
                self._get_upload(path)
            return
        if path == "/api/state":
            if self._authed():
                self._json(200, self._snap())
            return
        if path == "/api/health":  # an agent's first call: is anyone listening (#1042)
            if self._authed():
                self._json(200, {"api": API_VERSION, "listening": listener() is not None})
            return
        if path == "/api/wait":  # send, then wait for the answer (#1042)
            if self._authed():
                self._wait()
            return
        if path.startswith("/api/ask/"):  # a settled plan's whole text, when it is unfolded (#755)
            if self._authed():
                ask_id = path[len("/api/ask/"):]
                got = find_ask(ask_id) if 0 < len(ask_id) <= 64 else None
                if got is None:
                    self._json(404, {"error": "no such prompt"})
                else:
                    self._json(200, {"ask": got})
            return
        if path == "/api/latest":  # the service worker's, at a push (#755)
            if self._authed():
                self._json(200, latest())
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
            seen, had, sent = -1, None, time.monotonic()
            while True:
                with hub.cond:
                    woke = hub.cond.wait_for(lambda: hub.version != seen,
                                             timeout=max(0.0, PING_EVERY - (time.monotonic() - sent)))
                    seen = hub.version
                if self.evicted or self._revoked():  # revoked: it ends, and its reconnect is 401
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
        if not self.path.startswith(ROUTES["PUT"]):
            return self._json(404, {"error": "not found"})
        if not self._authed():
            return
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
        if self.path not in ROUTES["POST"]:
            return self._json(404, {"error": "not found"})
        if not self._authed():
            return
        if self.agent and self.path in AGENT_DENIED:  # the user's alone (#1042)
            return self._json(403, {"error": f"agent tokens can't {AGENT_DENIED[self.path]}",
                                    "code": "agent_forbidden"})
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
            active()
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
        if self.path == "/api/prefs":  # the page's settings
            if set(body) - {"away"} or body.get("away") not in AWAY:
                return self._json(400, {"error": "prefs are {away: ask|allow}"})
            set_prefs(**body)
            return self._json(200, {"state": snapshot()})
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
            data = json.dumps({"message": msg, "listener": listener(), "state": self._snap()}).encode()
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
        elif about is not None:  # a reply to a pair of screenshots: its task and step, from the pair
            pair = shots(about.get("task"), about.get("n")) if isinstance(about, dict) else None
            if not pair:
                return self._json(400, {"error": "no such screenshots"})
            about = {k: pair[k] for k in ("task", "n", "step", "round")}
        try:
            msg = send(text, self.agent or "user", cid, about=about, files=files, role="agent" if self.agent else None)
        except ValueError as e:
            return self._json(409, {"error": str(e)})
        self._json(200, {"message": msg, "listener": listener(), "state": self._snap()})
        return msg


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
                               else "tailscale (no Tailscale address)" if ts else "tailscale (not running)")
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
        "streams": threading.BoundedSemaphore(MAX_STREAMS), "waits": threading.BoundedSemaphore(MAX_WAITS),
        "sends": RateLimit(AGENT_SENDS_PER_MIN)})
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
                    f"the port is taken: another wb chat serve ({service_hint('status')}) or "
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
