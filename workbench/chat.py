"""The web chat: a side door to Claude for the user when they are away from the terminal.

`wb chat serve` serves a small page and a JSON API. Messages the user sends are appended to
chat/messages.jsonl; the mod of a Claude Code session in /delegate mode picks them up, records a
receipt (chat/receipts.jsonl) and submits each to Claude as a prompt. Claude answers with
`wb chat reply`, which the page shows. chat/listener.json is the session currently listening
(the mod rewrites it every poll), so the page can say whether anyone will read a message now.

What a message can do is what typing in the terminal can do, so the API wants a secret token
(~/.config/workbench/chat-token, made on first use; `wb chat url` prints a link carrying it in
the fragment, which never leaves the browser) and answers only to the host names it serves
(localhost and this machine's Tailscale names), which stops other web pages and DNS rebinding.
The link it prints is always localhost; reaching it from a phone is the user's own setup.
"""
from __future__ import annotations

import hmac
import json
import os
import secrets
import select
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import store
from .watch import Watcher

PORT = 1337
LISTENING_FOR = 15      # seconds: a listener.json older than this means nobody is listening
MAX_TEXT = 8000
MAX_BODY = 32 * 1024
MAX_STREAMS = 16        # open /api/stream connections (each holds a thread)
PING_EVERY = 15         # seconds between keep-alive comments on a stream
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


def token() -> str:
    path = token_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(32) + "\n")
    return path.read_text().strip()


def tailscale() -> dict:
    """This machine on the tailnet: {"ip", "names"}, or {} without Tailscale."""
    try:
        r = subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=5)
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
            if not w.native:
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
    hosts: set[str] = set()
    secret = ""
    hub: "Hub"
    streams: threading.BoundedSemaphore

    def log_message(self, *args):  # the journal needn't hold every poll
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                         "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _allowed(self) -> bool:
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        if host not in self.hosts:
            self._json(403, {"error": "unknown host"})
            return False
        return True

    def _authed(self) -> bool:
        got = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(got.encode(), self.secret.encode()):
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
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            hub, seen, last_body = self.hub, -1, None
            while True:
                with hub.cond:
                    woke = hub.cond.wait_for(lambda: hub.version != seen, timeout=PING_EVERY)
                    seen = hub.version
                if woke:
                    snap = snapshot()
                    body = json.dumps({k: v for k, v in snap.items() if k != "now"})
                    if body != last_body:
                        last_body = body
                        self.wfile.write(f"event: state\ndata: {json.dumps(snap)}\n\n".encode())
                        self.wfile.flush()
                        continue
                self.wfile.write(b": ping\n\n")  # keeps the line (and the page's watchdog) alive
                self.wfile.flush()
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
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            return self._json(413, {"error": "too long"})
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


def make_servers(port: int, binds: list[str] | None = None) -> tuple[list[ThreadingHTTPServer], dict]:
    """Servers on localhost and the Tailscale address (or the given addresses), not on 0.0.0.0."""
    ts = tailscale()
    if binds is None:
        binds = ["127.0.0.1"] + ([ts["ip"]] if ts.get("ip") else [])
    handler = type("ChatHandler", (Handler,), {
        "hosts": {"localhost", "127.0.0.1", *binds, *ts.get("names", ())},
        "secret": token(), "hub": Hub(),
        "streams": threading.BoundedSemaphore(MAX_STREAMS)})
    servers = []
    for addr in binds:
        srv = ThreadingHTTPServer((addr, port), handler)
        srv.daemon_threads = True
        servers.append(srv)
    return servers, ts


def serve(port: int, binds: list[str] | None, say=print) -> None:
    servers, ts = make_servers(port, binds)
    for srv in servers[1:]:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    names = sorted(ts.get("names", ()), key=len, reverse=True)
    say(f"web chat on {', '.join(f'{s.server_address[0]}:{port}' for s in servers)}"
        + (f" (http://{names[0]}:{port} on the tailnet)" if names else ""))
    servers[0].serve_forever()


def url(port: int) -> str:
    """The one link to open: localhost, the token in the fragment. Reaching it from another
    device (Tailscale, a tunnel) is the user's own setup: they change the host in the link."""
    return f"http://localhost:{port}/#token={token()}"
