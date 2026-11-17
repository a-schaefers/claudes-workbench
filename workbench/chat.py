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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import store
from .watch import Watcher

PORT = 1337
REPLAN_EVERY = 30       # seconds between looks at a skipped bind address
LISTENING_FOR = 15      # seconds: a listener.json older than this means nobody is listening
MAX_TEXT = 8000
MAX_BODY = 32 * 1024
MAX_STREAMS = 16        # open /api/stream connections (each holds a thread)
PING_EVERY = 15         # seconds between keep-alive comments on a stream
TIMEOUT = 30            # seconds a connection may stall (more than PING_EVERY: streams write by then)
# macOS's App Store and standalone Tailscale apps keep the CLI in the bundle, off PATH
TAILSCALE_APP = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
STATIC = Path(__file__).parent / "chat_static"
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml",
         ".webmanifest": "application/manifest+json"}
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

def send(text: str, frm: str, cid: str | None = None) -> dict:
    """Append a message. A client id (cid) already in the thread returns that message instead,
    so a retried send never lands twice."""
    with _send_lock:
        if cid:
            for r in store.read_jsonl(chat_dir() / "messages.jsonl")[0][-500:]:
                if r.get("cid") == cid:
                    return r
        row = {"id": f"{int(time.time() * 1000)}-{secrets.token_hex(3)}", "ts": store.now(),
               "from": frm, "text": text, **({"cid": cid} if cid else {})}
        store.append_jsonl(chat_dir() / "messages.jsonl", row)
        return row


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
    return {"messages": thread(), "listener": listener(), "now": store.now()}


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
        threading.Thread(target=self._run, daemon=True).start()

    def bump(self) -> None:
        with self.cond:
            self.version += 1
            self.cond.notify_all()

    def _run(self) -> None:
        w = Watcher(chat_dir())
        pid, pidfd = None, None
        while True:
            st = store.read_json(chat_dir() / "listener.json", None) or {}
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
                         "default-src 'self'; style-src 'self' 'unsafe-inline'; "
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
        if not self.streams.acquire(blocking=False):
            return self._json(503, {"error": "too many open streams"})
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self._security_headers()
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            hub, seen, last_body, sent = self.hub, -1, None, time.monotonic()
            while True:
                with hub.cond:
                    woke = hub.cond.wait_for(lambda: hub.version != seen,
                                             timeout=max(0.0, PING_EVERY - (time.monotonic() - sent)))
                    seen = hub.version
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
            self.streams.release()

    def do_POST(self):
        if not self._allowed():
            return
        if self.path != "/api/send":
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
        if not text or len(text) > MAX_TEXT:
            return self._json(400, {"error": f"a message is 1 to {MAX_TEXT} characters"})
        if cid is not None and not (isinstance(cid, str) and 0 < len(cid) <= 64):
            return self._json(400, {"error": "bad cid"})
        msg = send(text, "user", cid)
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
    servers[0].serve_forever()


def url(port: int) -> str:
    """The one link to open: localhost, the token in the fragment. Reaching it from another
    device (Tailscale, a tunnel) is the user's own setup: they change the host in the link."""
    return f"http://localhost:{port}/#token={token()}"
