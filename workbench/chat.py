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
"""
from __future__ import annotations

import hmac
import json
import os
import secrets
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import store

PORT = 1337
LISTENING_FOR = 15      # seconds: a listener.json older than this means nobody is listening
MAX_TEXT = 8000
MAX_BODY = 32 * 1024


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

def send(text: str, frm: str) -> dict:
    row = {"id": f"{int(time.time() * 1000)}-{secrets.token_hex(3)}", "ts": store.now(),
           "from": frm, "text": text}
    store.append_jsonl(chat_dir() / "messages.jsonl", row)
    return row


def ack(msg_id: str, by: str) -> None:
    store.append_jsonl(chat_dir() / "receipts.jsonl", {"id": msg_id, "ts": store.now(), "by": by})


def thread(limit: int = 200) -> list[dict]:
    """The latest messages, each of the user's with its receipt (or None: not read yet)."""
    rows = store.read_jsonl(chat_dir() / "messages.jsonl")[0][-limit:]
    receipts = {r["id"]: r for r in store.read_jsonl(chat_dir() / "receipts.jsonl")[0]}
    return [{**r, "received": receipts.get(r["id"])} if r["from"] == "user" else r for r in rows]


def pending() -> list[dict]:
    return [r for r in thread() if r["from"] == "user" and not r["received"]]


def listener() -> dict | None:
    st = store.read_json(chat_dir() / "listener.json", None)
    if not st or store.now() - st.get("ts", 0) > LISTENING_FOR:
        return None
    return st


# ---- the server ----

class Handler(BaseHTTPRequestHandler):
    server_version = "wb-chat"
    hosts: set[str] = set()
    secret = ""

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
        if path in ("/", "/index.html"):
            return self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        if path == "/app.js":
            return self._send(200, SCRIPT.encode(), "text/javascript; charset=utf-8")
        if path == "/api/state":
            if self._authed():
                self._json(200, {"messages": thread(), "listener": listener(), "now": store.now()})
            return
        self._json(404, {"error": "not found"})

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
            text = str(json.loads(self.rfile.read(n) or b"{}").get("text", "")).strip()
        except ValueError:
            return self._json(400, {"error": "bad json"})
        if not text or len(text) > MAX_TEXT:
            return self._json(400, {"error": f"a message is 1 to {MAX_TEXT} characters"})
        self._json(200, {"message": send(text, "user"), "listener": listener()})


def make_servers(port: int, binds: list[str] | None = None) -> tuple[list[ThreadingHTTPServer], dict]:
    """Servers on localhost and the Tailscale address (or the given addresses), not on 0.0.0.0."""
    ts = tailscale()
    if binds is None:
        binds = ["127.0.0.1"] + ([ts["ip"]] if ts.get("ip") else [])
    handler = type("ChatHandler", (Handler,), {
        "hosts": {"localhost", "127.0.0.1", *binds, *ts.get("names", ())},
        "secret": token()})
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


def urls(port: int) -> list[str]:
    ts = tailscale()
    names = sorted(ts.get("names", ()), key=len, reverse=True)
    hosts = names[:1] + ([ts["ip"]] if ts.get("ip") else []) + ["localhost"]
    return [f"http://{h}:{port}/#token={token()}" for h in hosts]


PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="theme-color" content="#1f1e1d">
<title>Claude, away</title>
<style>
:root { --bg:#faf9f5; --fg:#1f1e1d; --dim:#6b6a66; --me:#d97757; --meFg:#fff; --them:#efede6;
        --line:#e3e0d6; --ok:#4f8a5b; --warn:#b5862f; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#1f1e1d; --fg:#ecebe6; --dim:#9a988f; --me:#c4673f; --meFg:#fff; --them:#2c2b29;
          --line:#3a3936; --ok:#7bbf87; --warn:#d9ad5b; }
}
* { box-sizing: border-box; }
html, body { height: 100%; margin: 0; background: var(--bg); color: var(--fg);
  font: 16px/1.45 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif; }
body { display: flex; flex-direction: column; max-width: 720px; margin: 0 auto; }
header { padding: 12px 16px; border-bottom: 1px solid var(--line); display: flex; gap: 10px;
  align-items: baseline; }
header b { font-size: 17px; }
#who { font-size: 13px; color: var(--dim); }
#who.on::before { content: "\\25CF "; color: var(--ok); }
#who.off::before { content: "\\25CF "; color: var(--warn); }
main { flex: 1; overflow-y: auto; padding: 12px 16px; display: flex; flex-direction: column; gap: 10px; }
.msg { max-width: 85%; padding: 8px 12px; border-radius: 14px; white-space: pre-wrap;
  overflow-wrap: anywhere; }
.user { align-self: flex-end; background: var(--me); color: var(--meFg); border-bottom-right-radius: 4px; }
.claude { align-self: flex-start; background: var(--them); border-bottom-left-radius: 4px; }
.meta { font-size: 12px; color: var(--dim); margin-top: -6px; }
.user + .meta { align-self: flex-end; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 14px; }
.empty { color: var(--dim); text-align: center; margin: auto; padding: 24px; }
form { display: flex; gap: 8px; padding: 10px 16px calc(10px + env(safe-area-inset-bottom));
  border-top: 1px solid var(--line); }
textarea { flex: 1; resize: none; font: inherit; padding: 9px 12px; border-radius: 12px;
  border: 1px solid var(--line); background: var(--bg); color: var(--fg); min-height: 44px; max-height: 40vh; }
button { font: inherit; padding: 0 16px; border: 0; border-radius: 12px; background: var(--me);
  color: var(--meFg); min-height: 44px; }
button:disabled { opacity: .5; }
#err { color: var(--warn); font-size: 13px; padding: 0 16px; }
</style>
</head><body>
<header><b>Claude</b><span id="who">connecting…</span></header>
<main id="log"><div class="empty">Messages you send here reach Claude in your terminal while
/delegate is running there. Claude answers here.</div></main>
<div id="err"></div>
<form id="f"><textarea id="t" rows="1" placeholder="Message Claude" enterkeyhint="send"></textarea>
<button id="b">Send</button></form>
<script src="/app.js"></script>
</body></html>
"""

SCRIPT = r"""
const KEY = 'wb-chat-token'
let tok = ''
try {
  const m = location.hash.match(/token=([\w-]+)/)
  if (m) { localStorage.setItem(KEY, m[1]); history.replaceState(null, '', location.pathname) }
  tok = localStorage.getItem(KEY) || ''
} catch (e) { const m = location.hash.match(/token=([\w-]+)/); tok = m ? m[1] : '' }
const $ = id => document.getElementById(id)
const log = $('log'), who = $('who'), err = $('err'), box = $('t'), btn = $('b')
let last = '', timer = 0

const clock = ts => new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
function body(text) {           // text, with `code` spans; never HTML from the thread
  const el = document.createElement('div')
  text.split(/(`[^`\n]+`)/).forEach(part => {
    if (part.startsWith('`') && part.endsWith('`') && part.length > 2) {
      const c = document.createElement('code'); c.textContent = part.slice(1, -1); el.append(c)
    } else el.append(document.createTextNode(part))
  })
  return el
}
function draw(state) {
  const key = JSON.stringify([state.messages, !!state.listener])
  if (state.listener) {
    who.className = 'on'
    who.textContent = 'listening in ' + state.listener.cwd.replace(/^\/home\/[^/]+/, '~')
  } else {
    who.className = 'off'
    who.textContent = 'no Claude session is listening: messages wait for one'
  }
  if (key === last) return
  last = key
  const atEnd = log.scrollHeight - log.scrollTop - log.clientHeight < 80
  log.replaceChildren()
  if (!state.messages.length) {
    const e = document.createElement('div'); e.className = 'empty'
    e.textContent = 'Messages you send here reach Claude in your terminal while /delegate is ' +
      'running there. Claude answers here.'
    log.append(e)
  }
  for (const m of state.messages) {
    const b = body(m.text); b.className = 'msg ' + m.from; log.append(b)
    const meta = document.createElement('div'); meta.className = 'meta'
    meta.textContent = m.from === 'user'
      ? clock(m.ts) + (m.received ? ' · received by Claude ' + clock(m.received.ts) : ' · waiting for Claude')
      : 'Claude · ' + clock(m.ts)
    if (m.from === 'user') meta.style.alignSelf = 'flex-end'
    log.append(meta)
  }
  if (atEnd) log.scrollTop = log.scrollHeight
}
async function api(path, opts = {}) {
  const r = await fetch(path, { ...opts, headers: { ...(opts.headers || {}), Authorization: 'Bearer ' + tok } })
  const j = await r.json().catch(() => ({}))
  if (!r.ok) throw new Error(j.error || r.status)
  return j
}
async function refresh() {
  clearTimeout(timer)
  try { draw(await api('/api/state')); err.textContent = '' }
  catch (e) { err.textContent = String(e.message || e) }
  timer = setTimeout(refresh, document.hidden ? 20000 : 3000)
}
document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh() })
$('f').addEventListener('submit', async ev => {
  ev.preventDefault()
  const text = box.value.trim()
  if (!text) return
  btn.disabled = true
  try {
    await api('/api/send', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                             body: JSON.stringify({ text }) })
    box.value = ''; box.style.height = ''
    last = ''; await refresh(); log.scrollTop = log.scrollHeight
  } catch (e) { err.textContent = String(e.message || e) }
  btn.disabled = false
})
box.addEventListener('input', () => { box.style.height = ''; box.style.height = box.scrollHeight + 'px' })
box.addEventListener('keydown', ev => {
  if (ev.key === 'Enter' && !ev.shiftKey && !('ontouchstart' in window)) { ev.preventDefault(); $('f').requestSubmit() }
})
if (!tok) err.textContent = 'No token: open the link `wb chat url` prints on the computer.'
refresh()
"""
