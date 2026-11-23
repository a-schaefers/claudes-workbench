"""The keyhole: a per-task loopback proxy that holds an API model's key outside the lane (#1012).

Everything the agent's OpenCode can read, its shell can read too: its environment
(/proc/<pid>/environ), the config layer (rules/<task>/opencode.json, OPENCODE_CONFIG_CONTENT),
and nothing stops TCP. So a provider's key can't be given to OpenCode at all. The runner, which
is outside the lane and dies with it, serves this proxy from a daemon thread instead: OpenCode
gets the keyhole's base URL and a dummy key (the token, new for each runner), and the keyhole
forwards only the task's model's requests to the provider, the real key swapped in.

What a request must pass, in order (each refusal a JSON error body):
1. the path: a POST to the base plus exactly one model path (`chat/completions`, `responses`,
   `messages`, `messages/count_tokens`: what OpenCode posts), with a plain query; `GET …/models`
   is answered here with only the task's model; anything else is 404 (a bad query 400);
2. the token, in `Authorization: Bearer` or `x-api-key` (never the URL: URLs reach OpenCode's
   error text and runner.log), compared in constant time; 401 without it;
3. the body's `model`, which must be the task's model's name at its provider, and no fallback
   list (`models`); 403, an error OpenCode doesn't retry; bodies over 32 MB are 413. The body is
   UTF-8 JSON with no duplicate key, no NaN, and no top-level key that folds to `model` or
   `models` but isn't `model`: a server reading JSON otherwise (Go's encoding/json matches keys
   case-insensitively; some parsers keep the first duplicate) would route a body that passed.
Then the body goes on as it is, the client's auth headers dropped and the real one added, and
the response comes back as it arrives (SSE a whole line at a time), its `data:` lines read on
the way for the served model (status `served`) and the usage (`on_usage`); the key itself never
passes, and an SSE line with an `error` goes through `redact`. An upstream error is read whole
(64 KB at most) and passed through `redact` before OpenCode sees it, since OpenCode prints an
upstream body verbatim (a key-like string in a 401 body reached stdout, #1009).

The path mapping (checked against OpenCode 2.3.2's source, packages/ai/src, and real OpenCode
in RealOpenCodeAPI): a provider package builds its request URL from the configured baseURL
alone, as `new URL(trim(baseURL) + path)`, some first rewriting the base by its path (DeepInfra
appends `/openai` unless the base already ends in it). So the keyhole's base mirrors the
upstream's path on 127.0.0.1 (`layer_options`; DeepInfra's `/openai` already appended, by
`mirror_path`): OpenCode then posts to the very path it would have posted to upstream, and the
keyhole forwards that path, and its query (`?beta=true` for Anthropic), to the upstream's host.
Only packages known to post there with a bearer or `x-api-key` key run (`PACKAGES`): Google's
(`x-goog-api-key`, `/models/<id>:streamGenerateContent`), Azure's (`api-key`, and a base rewritten
by its host), Cohere's native `/chat` and the like can't be served, and `start` says so.

Standard library only: it runs in the runner.
"""
from __future__ import annotations

import base64
import hmac
import http.client
import json
import os
import re
import secrets
import socket
import socketserver
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import store

NEEDS = "needs a key wb can hold outside the lane"
REMEDY = "store an API key with /connect in OpenCode"
# The paths OpenCode 2.3.2 posts a model request to, at the end of the path under the base (the
# spike, #1009): OpenAI chat and responses, Anthropic's messages and its token count.
MODEL_PATHS = ("/chat/completions", "/responses", "/messages", "/messages/count_tokens")
# The provider packages whose model requests the keyhole can carry (OpenCode 2.3.2's
# packages/ai/src/providers: each LLM route posts the base plus one of MODEL_PATHS, the key in
# `x-api-key` for Anthropic's, `Authorization: Bearer` for the rest). Any other package (Google's
# x-goog-api-key, Azure's api-key, Cohere's native /chat, Bedrock, Vertex, gateways with their own
# paths or headers, media-only packages) can't run: the issue wants that said at start, not a
# 401 or 404 on every request (the protocol review, 2026-11-23).
PACKAGES = {"anthropic": "anthropic", "anthropic-compatible": "anthropic",
            **dict.fromkeys(("alibaba", "cerebras", "cloudflare-workers-ai", "deepinfra",
                             "deepseek", "digitalocean", "fireworks", "groq", "meta", "minimax",
                             "mistral", "moonshot", "openai", "openai-compatible",
                             "openai-compatible-responses", "openrouter", "togetherai", "venice",
                             "xai", "zai", "zai-coding-plan"), "bearer")}
PACKAGE_PREFIX = "@opencode/ai/providers/"
BODY_CAP = 32 << 20     # a 131K-token prompt is a few MB of JSON
ERROR_CAP = 64 << 10    # an upstream error body, read whole to redact it
SNIFF_CAP = 4 << 20     # a non-streamed JSON answer kept to read its model and usage
RATE_LIMIT_SECS = 60    # a 429 without Retry-After: how long the model is rate limited
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
       "trailers", "transfer-encoding", "upgrade", "proxy-connection"}
CLIENT_AUTH = {"authorization", "x-api-key", "api-key"}
# A query the keyhole forwards: printable ASCII only. A control character made http.client raise
# InvalidURL (reported as the model unreachable) and a non-ASCII one UnicodeEncodeError (a
# traceback, the connection dropped): the protocol review, 2026-11-23.
QUERY = re.compile(r"[\x21-\x7e]*")
# What a key may hold: printable ASCII, no space. A stored key with a newline made http.client
# raise ValueError quoting the whole header, key and all, into runner.log (the review, 2026-11-23).
KEY_CHARS = re.compile(r"[\x21-\x7e]+")
# A chunked body's size line: hex, at most 8 digits (int("-1", 16) read to EOF past BODY_CAP).
CHUNK_SIZE = re.compile(rb"[0-9A-Fa-f]{1,8}")
LINE_CAP = 64 << 10     # a chunk-size or trailer line
LOOPBACK = {"127.0.0.1", "localhost", "::1"}


class KeyholeError(Exception):
    """Why an API task can't start: the text names no key."""


class Key:
    """A provider's static API key, held in the runner's memory only. Its repr and str name the
    provider, never the key."""
    __slots__ = ("provider", "_value")

    def __init__(self, provider: str, value: str):
        self.provider = provider
        self._value = value

    @property
    def value(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return f"Key({self.provider}, api key)"

    __str__ = __repr__

    def __reduce__(self):  # never pickled, copied into a log or a child's argv
        raise TypeError("a Key isn't copied")


# What redact replaces besides the key itself: the key formats from the robustness review
# (OpenAI, Anthropic, OpenRouter, xAI, Groq, DeepSeek; an echo like "sk-ab…wxyz" included, its
# ellipsis raw or JSON-escaped),
# a bearer credential, and any run long enough to be a key.
KEYLIKE = (r"(?<![A-Za-z0-9])(?:sk-ant|sk-or|sk|xai|gsk|dsk)-(?:[A-Za-z0-9_\-*.…]|\\u2026){4,}",
           r"(?i:bearer)\s+\S+",
           r"[A-Za-z0-9_\-]{32,}")
REDACTED = "[key redacted]"


def redact(text: str, key: str | Key = "") -> str:
    """``text`` with the key, its first 8 and last 4 characters, anything key-shaped (KEYLIKE)
    and any bearer credential replaced with [key redacted]. One pass, so a replacement is never
    matched again."""
    key = key.value if isinstance(key, Key) else (key or "")
    parts = [re.escape(key)] if key else []
    if len(key) >= 12:  # shorter, its fragments would be most of it, and match everywhere
        parts += [re.escape(key[:8]), re.escape(key[-4:])]
    return re.sub("|".join(parts + list(KEYLIKE)), REDACTED, text)


def _package_name(package: str) -> str:
    """``@opencode/ai/providers/meta/messages`` → ``meta``; a bare name stays as it is."""
    name = package or ""
    if name.startswith(PACKAGE_PREFIX):
        name = name[len(PACKAGE_PREFIX):]
    return name.split("/")[0]


def protocol_of(package: str) -> str | None:
    """How the provider takes its key: "anthropic" (`x-api-key`) or "bearer" (`Authorization:
    Bearer`), from PACKAGES; None for a package the keyhole can't carry."""
    return PACKAGES.get(_package_name(package))


def mirror_path(package: str, base_url: str) -> str:
    """The upstream base the keyhole mirrors: the catalog's, with what the package appends to it
    already appended, so OpenCode posts the keyhole's base plus a model path exactly (DeepInfra
    adds `/openai` unless the base ends in it: providers/deepinfra.ts `configure`)."""
    root = base_url.rstrip("/")
    if _package_name(package) == "deepinfra" and not root.endswith("/openai"):
        return f"{root}/openai"
    return base_url


def _signs(provider: str, package: str = "") -> bool:
    """A provider that signs each request (SigV4, Vertex), which a header swap can't do."""
    return any(s in f"{provider} {package}" for s in ("amazon-bedrock", "google-vertex", "bedrock"))


def _configured_key(provider: str) -> str | None:
    """Where the user's OpenCode config gives ``provider`` an apiKey the agent could read: a file,
    or OPENCODE_CONFIG_CONTENT (the agent's environment). "env:<VAR>" for a `{env:VAR}` key."""
    from . import models
    try:
        layers = [(str(p), layer) for p, layer in models._layers()]
    except RuntimeError as e:  # a config that doesn't parse: OpenCode won't start either
        raise KeyholeError(str(e)) from None
    if raw := os.environ.get("OPENCODE_CONFIG_CONTENT"):
        try:
            layers.append((models.CONTENT, json.loads(models._strip_jsonc(raw))))
        except ValueError:
            pass
    found = None
    for name, layer in layers:
        prov = models._providers_of(layer if isinstance(layer, dict) else {}).get(provider)
        opts = (prov or {}).get("options") if isinstance(prov, dict) else None
        opts = opts if isinstance(opts, dict) else {}
        vals = [opts.get("apiKey")]
        # a key written as a header (the leak review, 2026-11-23): the agent can read it, and
        # OpenCode would send it in place of the token
        heads = opts.get("headers") if isinstance(opts.get("headers"), dict) else {}
        vals += [v for k, v in heads.items() if isinstance(v, str) and (
            _AUTH_HEADER.search(str(k)) or re.search("|".join(KEYLIKE[:2]), v))]
        for val in vals:
            if isinstance(val, str) and val.strip():
                env = re.search(r"\{env:([^}]+)\}", val)
                found = f"env:{env.group(1)}" if env else name
    return found


# A header the user's config may carry a key in: Authorization, x-api-key, api-key, x-goog-api-key,
# cf-aig-authorization, a token header.
_AUTH_HEADER = re.compile(r"(?i)auth|key|token")


def _opencode(*args: str) -> subprocess.CompletedProcess:
    from . import runner
    try:
        return subprocess.run([*runner.opencode_bin(), *args], capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise KeyholeError(f"can't run opencode {args[0]} {args[1]}: {type(e).__name__}") from None


def _first_line(text: str) -> str:
    return redact(next((ln.strip() for ln in (text or "").splitlines() if ln.strip()), ""))[:200]


def load_key(provider: str, package: str = "") -> Key:
    """``provider``'s static API key from the user's own OpenCode store (`opencode auth export`),
    run with the runner's environment, outside the lane. Only stdout is read, into memory; a
    failure's stderr is cut to its first line and redacted, and copied nowhere else. Raises
    KeyholeError ("needs a key wb can hold outside the lane: <why>") for OAuth, request signing,
    a key only in the environment, or one written where the agent can read it."""
    if _signs(provider, package):
        raise KeyholeError(f"{NEEDS}: {provider} signs each request (SigV4 or Vertex)")
    if where := _configured_key(provider):
        if where.startswith("env:"):
            raise KeyholeError(f"{NEEDS}: {provider}'s key is only in the environment "
                               f"(${where[4:]}), which agents don't get")
        raise KeyholeError(f"{NEEDS}: its key is written in {where}, which agents can read: "
                           "store it with /connect instead")
    r = _opencode("auth", "export", provider)
    if r.returncode != 0:
        raise KeyholeError(f"can't read {provider}'s key: opencode auth export exited "
                           f"{r.returncode}: {_first_line(r.stderr) or 'no message'}")
    try:
        rows = json.loads(r.stdout or "[]")
    except ValueError:
        raise KeyholeError(f"can't read {provider}'s key: opencode auth export printed no JSON") from None
    rows = [x for x in (rows if isinstance(rows, list) else [rows]) if isinstance(x, dict)
            and x.get("integrationID", provider) == provider and isinstance(x.get("value"), dict)]
    rows.sort(key=lambda x: not x.get("active"))  # the active credential first
    for row in rows:
        v = row["value"]
        if v.get("type") == "key" and isinstance(v.get("key"), str) and v["key"].strip():
            value = v["key"].strip()  # a paste's newline: OpenCode trims on /connect, not a hand edit
            if not KEY_CHARS.fullmatch(value):
                raise KeyholeError(f"can't use {provider}'s stored key: it holds a character an "
                                   "HTTP header can't carry; store it again with /connect")
            return Key(provider, value)
    kinds = {row["value"].get("type") for row in rows}
    if "oauth" in kinds:
        raise KeyholeError(f"{NEEDS}: {provider} is signed in with OAuth, whose tokens rotate")
    if "external" in kinds:
        raise KeyholeError(f"{NEEDS}: {provider}'s credential comes from an external source")
    if var := _env_only(provider):
        raise KeyholeError(f"{NEEDS}: {provider}'s key is only in the environment (${var}), "
                           "which agents don't get")
    raise KeyholeError(f"{NEEDS}: OpenCode has no key stored for {provider}")


def _env_only(provider: str) -> str:
    """The variable `opencode auth list` names as ``provider``'s key, if any."""
    r = _opencode("auth", "list", "--format", "json")
    try:
        rows = json.loads(r.stdout) if r.returncode == 0 else []
    except ValueError:
        return ""
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and provider in (row.get("id"), row.get("name")):
            for c in row.get("connections") or []:
                if isinstance(c, dict) and c.get("type") == "env" and c.get("name"):
                    return str(c["name"])
    return ""


def layer_options(kh: "Keyhole") -> dict:
    """provider.<p>.options for the config layer (and rules_env's restatement): the keyhole's
    base and the token as the dummy key, never the key. The base mirrors the upstream's path, so
    each package posts where it would upstream (the module's docstring):
    - bearer, DeepInfra: upstream https://api.deepinfra.com/v1/openai, base
      http://127.0.0.1:<port>/v1/openai, posted to …/v1/openai/chat/completions, the token in
      `Authorization: Bearer`;
    - anthropic: upstream https://api.anthropic.com/v1, base http://127.0.0.1:<port>/v1, posted to
      …/v1/messages?beta=true, the token in `x-api-key`."""
    return {"baseURL": kh.base, "apiKey": kh.token}


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    log = staticmethod(lambda line: None)

    def server_bind(self):
        # not HTTPServer's, which looks the host's name up between bind and listen (#950)
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def handle_error(self, request, client_address):
        # socketserver's prints the traceback to stderr (runner.log), and a ValueError from
        # http.client quotes the header it refused, the key in it (the review, 2026-11-23): one
        # line, the exception's type only
        self.log(f"keyhole: a request failed ({sys.exc_info()[0].__name__})")


class Keyhole:
    """The proxy for one task's model: ``model`` (a models.Model: id, provider, name), ``key``
    (a Key), ``upstream`` (the catalog's API base: never anything from a request), ``protocol``
    ("bearer" or "anthropic"). It serves at once, from a daemon thread, on 127.0.0.1 (never
    localhost, which may be ::1 first). ``on_usage(usage)`` gets each response's usage;
    ``on_health(state, why, until)`` each outcome (ok, no_credit, rate_limited, unreachable);
    ``log`` the start and refusal lines (runner.log: the runner's stdout)."""

    def __init__(self, task, model, key: Key, upstream: str, protocol: str = "bearer", *,
                 timeout: float | None = None, on_usage=None, on_health=None, log=None):
        if not isinstance(key, Key):
            raise TypeError("key must be a Key")
        if protocol not in ("bearer", "anthropic"):
            raise KeyholeError(f"{NEEDS}: {model.provider} takes its key in a way the keyhole "
                               "doesn't speak")
        up = urllib.parse.urlsplit(upstream)
        if up.scheme not in ("https", "http") or not up.hostname:
            raise KeyholeError(f"{model.id}'s API base isn't an http(s) URL")
        self.task, self.model, self._key, self.protocol = task, model, key, protocol
        self._up = up
        self.upstream_path = up.path.rstrip("/")
        self.timeout = timeout if timeout is not None else _timeout(task)
        self.on_usage = on_usage or (lambda usage: None)
        self.on_health = on_health or (lambda state, why, until: None)
        self.log = log or (lambda line: print(line, flush=True))
        self.token = secrets.token_urlsafe(32)
        self._refused: set[str] = set()
        self._lock = threading.Lock()
        st = task.status if task is not None else {}
        self.served = st.get("served") if isinstance(st.get("served"), dict) else None
        self._server = _Server(("127.0.0.1", 0), _handler(self))
        self._server.log = self.log
        self.port = self._server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}{self.upstream_path}"
        self._thread = threading.Thread(target=self._server.serve_forever, args=(0.1,), daemon=True,
                                        name=f"keyhole-{self.port}")
        self._thread.start()

    def __repr__(self) -> str:
        return f"Keyhole({self.model.id} on 127.0.0.1:{self.port})"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # what the handler asks
    def token_ok(self, headers) -> bool:
        auth = headers.get("Authorization") or ""
        given = auth[7:].strip() if auth[:7].lower() == "bearer " else headers.get("x-api-key") or ""
        return hmac.compare_digest(given.encode(), self.token.encode())

    def refused(self, name: str) -> None:
        with self._lock:
            if name in self._refused:
                return
            self._refused.add(name)
        self.log(f"keyhole: refused {_printable(name)} (this task runs {self.model.id})")

    def auth_header(self) -> tuple[str, str]:
        if self.protocol == "anthropic":
            return "x-api-key", self._key.value
        return "Authorization", f"Bearer {self._key.value}"

    def redact(self, text: str) -> str:
        return redact(text, self._key)

    def saw(self, model, by) -> None:
        """A response named the model that served it (and OpenRouter its provider): status
        `served` when it first appears or changes, and a `served` event."""
        if not isinstance(model, str) or not model:
            return
        now = {"model": model, "by": by if isinstance(by, str) and by else None}
        with self._lock:
            if now == self.served:
                return
            self.served = now
        if self.task is None:
            return
        self.task.set_status(served=now)
        via = f" (via {_printable(now['by'])})" if now["by"] else ""
        self.task.event({"ts": store.now(), "round": self.task.status.get("round", 1),
                         "kind": "served", **now,
                         "text": f"the provider served {_printable(model)}{via}"})

    def health(self, state: str, why: str = "", until: float | None = None) -> None:
        try:
            self.on_health(state, why, until)
        except Exception:  # a hook's failure never fails a model request
            pass

    def usage(self, usage) -> None:
        try:
            self.on_usage(usage)
        except Exception:
            pass

    def connection(self) -> http.client.HTTPConnection:
        """A fresh upstream connection (one per request: parallel requests never share one),
        through wb's own https_proxy (CONNECT) unless NO_PROXY covers the host or it is this
        machine."""
        up = self._up
        host, port = up.hostname, up.port or (443 if up.scheme == "https" else 80)
        if up.scheme == "http":  # a server of the user's own (or a test's): no proxy, no TLS
            return http.client.HTTPConnection(host, port, timeout=self.timeout)
        proxy = None
        if host not in LOOPBACK and not urllib.request.proxy_bypass_environment(host):
            proxy = urllib.request.getproxies_environment().get("https")
        ctx = ssl.create_default_context()
        if not proxy:
            return http.client.HTTPSConnection(host, port, timeout=self.timeout, context=ctx)
        p = urllib.parse.urlsplit(proxy if "://" in proxy else f"http://{proxy}")
        conn = http.client.HTTPSConnection(p.hostname, p.port or 80, timeout=self.timeout,
                                           context=ctx)
        tunnel = {}
        if p.username is not None:
            cred = f"{urllib.parse.unquote(p.username)}:{urllib.parse.unquote(p.password or '')}"
            tunnel["Proxy-Authorization"] = "Basic " + base64.b64encode(cred.encode()).decode()
        conn.set_tunnel(host, port, headers=tunnel)
        return conn

    @property
    def upstream_host(self) -> str:
        return self._up.netloc.rpartition("@")[2]


def _timeout(task) -> float:
    """The upstream socket's timeout: the task's silence limit + 60 s (a reasoning model is silent
    as long as it thinks)."""
    if task is None:
        return 960.0
    from . import runner
    try:
        return runner.limit(task, "silence") + 60
    except Exception:
        return 960.0


def _printable(text: str) -> str:
    return redact(re.sub(r"[^\x20-\x7e]", "?", str(text)))[:120]


def _retry_after(value: str | None) -> float:
    """Retry-After (seconds or an HTTP date) as an epoch time; RATE_LIMIT_SECS without one."""
    now = time.time()
    if value:
        try:
            return now + max(0.0, float(value))
        except ValueError:
            try:
                return parsedate_to_datetime(value).timestamp()
            except (TypeError, ValueError, IndexError):
                pass
    return now + RATE_LIMIT_SECS


def _handler(kh: Keyhole):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "workbench-keyhole"
        sys_version = ""

        def log_message(self, *args):  # nothing about a request reaches runner.log
            pass

        def handle(self):
            try:
                super().handle()
            except (BrokenPipeError, ConnectionResetError, socket.timeout):
                pass

        def send_json(self, code: int, message: str, close: bool = False, etype=None):
            body = json.dumps({"error": {"type": etype or "invalid_request_error",
                                         "message": message}}).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if close:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            self.wfile.write(body)

        def route(self, method: str) -> str | None:
            """The model path under the keyhole's base (exactly the base plus one of MODEL_PATHS,
            or `/models` for a GET), or None. No segment between: the base mirrors the
            upstream's, so OpenCode never posts one, and `/v1/threads/<id>/messages` went on with
            the key (the reviews, 2026-11-23)."""
            path = urllib.parse.urlsplit(self.path).path
            base = kh.upstream_path
            rest = path[len(base):] if path.startswith(base + "/") else ""
            return rest if rest in (("/models",) if method == "GET" else MODEL_PATHS) else None

        def query_ok(self) -> bool:
            if QUERY.fullmatch(urllib.parse.urlsplit(self.path).query):
                return True
            self.send_json(400, "keyhole: a query that isn't printable ASCII", close=True)
            return False

        def refuse_path(self):
            path = urllib.parse.urlsplit(self.path).path
            self.send_json(404, f"keyhole: {_printable(path)} isn't a model request", close=True)

        def do_GET(self):
            if self.route("GET") is None:
                return self.refuse_path()
            if not self.query_ok():
                return
            if not kh.token_ok(self.headers):
                return self.send_json(401, "keyhole: not this task's token", close=True)
            name = kh.model.name
            if kh.protocol == "anthropic":
                listing = {"data": [{"type": "model", "id": name, "display_name": name}],
                           "has_more": False, "first_id": name, "last_id": name}
            else:
                listing = {"object": "list", "data": [{"id": name, "object": "model",
                                                       "owned_by": kh.model.provider}]}
            body = json.dumps(listing).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.route("POST") is None:
                return self.refuse_path()
            if not self.query_ok():
                return
            if not kh.token_ok(self.headers):
                return self.send_json(401, "keyhole: not this task's token", close=True)
            raw = self.read_body()
            if raw is None:
                return
            try:
                body = _strict_json(raw)
            except _Ambiguous as e:
                kh.refused(str(e))
                return self.send_json(403, f"keyhole: this task may use only {kh.model.id}")
            except ValueError:
                return self.send_json(400, "keyhole: the body isn't JSON")
            if not isinstance(body, dict):
                return self.send_json(400, "keyhole: the body isn't a JSON object")
            asked = body.get("model")
            if "models" in body or asked != kh.model.name:  # a fallback list routes elsewhere
                kh.refused("a fallback list" if "models" in body else str(asked))
                return self.send_json(403, f"keyhole: this task may use only {kh.model.id}")
            self.forward(raw)

        def do_PUT(self):
            self.refuse_path()

        do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = do_PUT

        def read_body(self) -> bytes | None:
            if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
                out = bytearray()
                while True:
                    line = self.rfile.readline(LINE_CAP).split(b";")[0].strip()
                    if not CHUNK_SIZE.fullmatch(line):  # "-1" read to EOF, "zz" a traceback
                        self.send_json(400, "keyhole: a bad chunk size", close=True)
                        return None
                    size = int(line, 16)
                    if size == 0:
                        for _ in range(100):  # the trailer, bounded
                            if self.rfile.readline(LINE_CAP) in (b"\r\n", b"\n", b""):
                                break
                        return bytes(out)
                    if len(out) + size > BODY_CAP:
                        self.send_json(413, "keyhole: the request is over 32 MB", close=True)
                        return None
                    piece = self.rfile.read(size)
                    if len(piece) < size:
                        self.send_json(400, "keyhole: the body ended early", close=True)
                        return None
                    out += piece
                    self.rfile.readline(LINE_CAP)
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if n < 0:
                self.send_json(400, "keyhole: a bad Content-Length", close=True)
                return None
            if n > BODY_CAP:
                self.send_json(413, "keyhole: the request is over 32 MB", close=True)
                return None
            return self.rfile.read(n)

        def forward(self, raw: bytes):
            heads = {}
            named = {h.strip().lower() for h in (self.headers.get("Connection") or "").split(",")}
            for k, v in self.headers.items():
                lk = k.lower()
                if lk in HOP or lk in named or lk in CLIENT_AUTH or lk in (
                        "host", "content-length", "accept-encoding"):
                    continue
                heads[k] = v
            name, value = kh.auth_header()
            heads[name] = value
            heads["Host"] = kh.upstream_host
            heads["Accept-Encoding"] = "identity"
            heads["Content-Length"] = str(len(raw))
            query = urllib.parse.urlsplit(self.path).query
            target = urllib.parse.urlsplit(self.path).path + (f"?{query}" if query else "")
            conn = kh.connection()
            try:
                try:
                    conn.request("POST", target, body=raw, headers=heads)
                    resp = conn.getresponse()
                except Exception as e:
                    if isinstance(e, (OSError, http.client.HTTPException)) and not isinstance(
                            e, http.client.InvalidURL):
                        return self.unreachable(e)
                    # nothing was sent, and not the upstream's fault (InvalidURL, a ValueError or
                    # UnicodeError from http.client): never `unreachable`, and only the type, since
                    # putheader's ValueError quotes the header, the key in it (the reviews,
                    # 2026-11-23)
                    return self.send_json(502, f"keyhole: can't send this request on "
                                               f"({type(e).__name__})", etype="api_error")
                if 200 <= resp.status < 300:
                    return self.stream(resp)
                return self.relay_error(resp)
            finally:
                conn.close()

        def unreachable(self, e: Exception):
            host = kh._up.hostname
            if isinstance(e, ssl.SSLCertVerificationError):
                why = (f"its certificate didn't verify ({getattr(e, 'verify_message', '') or e})"
                       + ("; Python has no CA certificates: on macOS run its Install "
                          "Certificates command" if sys.platform == "darwin" else ""))
            elif isinstance(e, socket.gaierror):
                why = f"no address for it ({e.strerror or e})"
            else:
                why = str(e) or type(e).__name__
            why = kh.redact(why)
            kh.health("unreachable", why)
            self.send_json(502, f"keyhole: can't reach {host}: {why}", etype="api_error")

        def relay_error(self, resp):
            data = resp.read(ERROR_CAP + 1)
            text = data[:ERROR_CAP].decode("utf-8", "replace")
            if len(data) > ERROR_CAP:
                # cut: a key straddling the cut would leave its first characters, which no
                # pattern knows (7 of a 32-character key passed, the review, 2026-11-23), so drop
                # as much as a key (twice: JSON may escape it) from the end
                text = text[:-2 * max(len(kh._key.value), 64)]
            text = kh.redact(text)
            out = text.encode()
            if resp.status == 402 or "insufficient_quota" in text:
                kh.health("no_credit", _first_line(text))
            elif resp.status == 429:
                kh.health("rate_limited", _first_line(text), _retry_after(resp.getheader("Retry-After")))
            elif resp.status >= 500:
                kh.health("unreachable", f"{resp.status} {_first_line(text)}".strip())
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                lk = k.lower()
                if lk in HOP or lk in ("content-length", "content-encoding", "connection"):
                    continue
                self.send_header(k, kh.redact(v))
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def stream(self, resp):
            kh.health("ok")
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                lk = k.lower()
                if lk in HOP or lk in ("content-length", "connection"):
                    continue
                self.send_header(k, v)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            sse = "event-stream" in (resp.getheader("Content-Type") or "")
            key = kh._key.value.encode()
            hold = bytearray()  # SSE: a partial line; else the last len(key) - 1 bytes
            whole = bytearray()

            def send(data: bytes):
                if data:
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
                    self.wfile.flush()

            try:
                while True:
                    piece = resp.read1(65536)
                    if not piece:
                        break
                    hold += piece
                    if sse:  # whole lines on: an error event's line goes through redact
                        *done, rest = bytes(hold).split(b"\n")
                        if len(rest) > SNIFF_CAP:  # no line end in sight: pass it on, key out
                            done, rest = done + [rest], b""
                        hold = bytearray(rest)
                        if done:
                            send(b"\n".join(self.data_line(ln) for ln in done) + b"\n")
                    else:  # the key never passes, even split across two pieces
                        if len(whole) < SNIFF_CAP:
                            whole += piece
                        out = bytes(hold).replace(key, REDACTED.encode())
                        cut = max(0, len(out) - (len(key) - 1))
                        send(out[:cut])
                        hold = bytearray(out[cut:])
                if sse and hold:
                    send(self.data_line(bytes(hold)))
                elif hold:
                    send(bytes(hold).replace(key, REDACTED.encode()))
                hold = bytearray()
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except (OSError, http.client.HTTPException):
                self.close_connection = True  # the client or the upstream went away mid-answer
                return
            if not sse and whole:
                try:
                    self.sniff(json.loads(whole))
                except ValueError:
                    pass

        def data_line(self, ln: bytes) -> bytes:
            """One SSE line read for the model and usage, and as it goes on: with the key out,
            and through redact when it carries an `error` (a mid-stream error arrives inside a
            200: Anthropic's `event: error`, OpenRouter's `data: {"error": …}`)."""
            safe = ln.replace(kh._key.value.encode(), REDACTED.encode())
            s = ln.strip()
            if not s.startswith(b"data:"):
                return safe
            payload = s[5:].strip()
            if not payload or payload == b"[DONE]":
                return safe
            try:
                obj = json.loads(payload)
            except ValueError:
                return safe
            self.sniff(obj)
            if isinstance(obj, dict) and "error" in obj:
                return kh.redact(ln.decode("utf-8", "surrogateescape")).encode(
                    "utf-8", "surrogateescape")
            return safe

        def sniff(self, obj):
            if not isinstance(obj, dict):
                return
            msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
            # the Responses API (OpenAI's package, xAI, OpenRouter): under `response`, in
            # response.created and response.completed
            res = obj.get("response") if isinstance(obj.get("response"), dict) else {}
            served = (obj.get("model") or res.get("model")
                      or (msg.get("model") if obj.get("type") == "message_start" else None))
            if served:
                kh.saw(served, obj.get("provider"))
            usage = obj.get("usage") or msg.get("usage") or res.get("usage")
            if isinstance(usage, dict) and usage:
                kh.usage(usage)

    return Handler


class _Ambiguous(ValueError):
    """A body another JSON parser could read as naming another model."""


def _no_constant(name):
    raise ValueError(f"{name} isn't JSON")


def _pairs(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise _Ambiguous("a duplicate key")
        out[k] = v
    return out


def _strict_json(raw: bytes):
    """The body as every JSON parser reads it alike, else _Ambiguous (refused: 403) or ValueError
    (400). Go's encoding/json matches a key case-insensitively (with Unicode folding: `ſ` is `s`)
    and some parsers keep the first duplicate, so `{"model":"ok","MODEL":"other"}`,
    `{"model":"other","model":"ok"}` and `{"model":"ok","Models":[…]}` passed here and went on
    to route elsewhere (the leak review, 2026-11-23)."""
    body = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_no_constant)
    if isinstance(body, dict):
        import unicodedata
        for k in body:
            if k != "model" and unicodedata.normalize("NFKC", k).casefold() in ("model", "models"):
                raise _Ambiguous("a fallback list" if k.casefold().endswith("s") else
                                 f"a key spelled {k!r}"[:60])
    return body


def start(task, m, *, log=None, on_usage=None, on_health=None) -> Keyhole:
    """The keyhole for task ``task``'s API model ``m`` (models.Model: id, provider, name,
    base_url, and the catalog's provider package as ``package``): its key loaded (load_key), its
    protocol from the package, its upstream the catalog's base. Logs one line, no key and no
    token. Raises KeyholeError when there is no key wb can hold, or no API base."""
    package = getattr(m, "package", "") or ""
    if _signs(m.provider, package):
        raise KeyholeError(f"{NEEDS}: {m.provider} signs each request (SigV4 or Vertex)")
    protocol = protocol_of(package)
    if protocol is None:
        raise KeyholeError(f"{NEEDS}: {m.provider} takes its key in a way the keyhole doesn't "
                           f"speak ({_printable(package) or 'no provider package'})")
    if not m.base_url:
        raise KeyholeError(f"{m.id} has no API base in OpenCode's catalog")
    key = load_key(m.provider, package)
    kh = Keyhole(task, m, key, mirror_path(package, m.base_url), protocol, log=log,
                 on_usage=on_usage, on_health=on_health)
    kh.log(f"keyhole: {m.provider} on 127.0.0.1:{kh.port} for {m.id}")
    return kh
