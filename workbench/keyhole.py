"""The keyhole: a per-task loopback proxy that holds an API model's key outside the lane (#1012).

Everything the agent's OpenCode can read, its shell can read too: its environment
(/proc/<pid>/environ), the config layer (rules/<task>/opencode.json, OPENCODE_CONFIG_CONTENT),
and nothing stops TCP. So a provider's key can't be given to OpenCode at all. The runner, which
is outside the lane and dies with it, serves this proxy from a daemon thread instead: OpenCode
gets the keyhole's base URL and a dummy key (the token, new for each runner), and the keyhole
forwards only the task's model's requests to the provider, the real key swapped in.

What a request must pass, in order (each refusal a JSON error body):
1. the path: a POST to the base plus exactly one model path of the protocol's (`chat/completions`,
   `responses`, `messages`, `messages/count_tokens`; Gemini's `models/<id>:streamGenerateContent`;
   Bedrock Converse's `model/<id>/converse-stream`: what OpenCode posts), with a plain query
   (Gemini's only `alt=sse`: a `key=` there is a second way in, refused); `GET …/models` is
   answered here with only the task's model; anything else is 404 (a bad query 400);
2. the token, in `Authorization: Bearer`, `x-api-key`, `api-key` or `x-goog-api-key` (never the
   URL: URLs reach OpenCode's error text and runner.log), compared in constant time; 401 without it;
3. the model, which must be the task's model's name at its provider: the body's `model` (and no
   fallback list, `models`), or for Gemini and Converse, whose path names it, the path's (and a
   Converse body's `modelId`, the body naming no `model`); 403, an error OpenCode doesn't retry;
   bodies over 32 MB are 413. The body is
   UTF-8 JSON with no duplicate key, no NaN, and no top-level key that folds to `model` or
   `models` but isn't `model`: a server reading JSON otherwise (Go's encoding/json matches keys
   case-insensitively; some parsers keep the first duplicate) would route a body that passed.
Then the body goes on as it is (an OpenRouter body with its `provider` routing set to the role's
`order` and `allow_fallbacks: false`, whatever OpenCode sent: #1021), the client's auth headers
dropped and the real one added, and
the response comes back as it arrives (SSE a whole line at a time), its `data:` lines (Bedrock's
event-stream frames) read on the way for the served model (status `served`) and the usage
(`on_usage`); the key itself never
passes, and an SSE line with an `error` goes through `redact`. An upstream error is read whole
(64 KB at most) and passed through `redact` before OpenCode sees it, since OpenCode prints an
upstream body verbatim (a key-like string in a 401 body reached stdout, #1009).

The path mapping (checked against OpenCode 2.3.2's source, packages/ai/src, and real OpenCode
in RealOpenCodeAPI): a provider package builds its request URL from the configured baseURL
alone, as `new URL(trim(baseURL) + path)`, some first rewriting the base by its path (DeepInfra
appends `/openai` unless the base already ends in it) or host (Azure appends `/v1` and
`?api-version=v1` to an `*.openai.azure.com` base, and to any other host nothing). So the
keyhole's base mirrors the upstream's path on 127.0.0.1 (`layer_options`; what the package
appends already appended, by `mirror_path`, and Azure's query added on the way, `upstream_query`):
OpenCode then posts to the very path it would have posted to upstream, and the keyhole forwards
that path, and its query (`?beta=true` for Anthropic), to the upstream's host. Only packages
known to post there with a static key run (`PACKAGES`, #1192): a bearer or `x-api-key` key,
Google's Gemini (`x-goog-api-key`), Azure OpenAI's `api-key`, and Bedrock's API key (bearer, over
SigV4, for Converse and Mantle). Cohere's native `/chat`, Google's Interactions route, Vertex and
SigV4 signing can't be served, and `start` says so.
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
import select
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

from . import privacy, store

NEEDS = "needs a key wb can hold outside the lane"
REMEDY = "store an API key with /connect in OpenCode"
# The paths OpenCode 2.3.2 posts a model request to, at the end of the path under the base (the
# spike, #1009): OpenAI chat and responses, Anthropic's messages and its token count.
MODEL_PATHS = ("/chat/completions", "/responses", "/messages", "/messages/count_tokens")
# The path-model protocols' (#1192): Gemini's (protocols/gemini.ts: the model in the path, SSE
# pinned by `?alt=sse`; OpenCode 2.3.2 posts no `:countTokens` nor `:generateContent`, so neither
# is let through) and Bedrock Converse's (bedrock-converse.ts: `encodeURIComponent(modelId)`).
# `tunedModels/` matches too, to be refused as another model (403), not a 404.
GEMINI_PATH = re.compile(r"/(models|tunedModels)/(.+):streamGenerateContent")
CONVERSE_PATH = re.compile(r"/model/([^/]+)/converse-stream")
# The provider packages whose model requests the keyhole can carry, by how the key goes
# (OpenCode 2.3.2's packages/ai/src/providers): "bearer" (`Authorization: Bearer`, each LLM route
# posting the base plus one of MODEL_PATHS), "anthropic" (`x-api-key`), "google" (Gemini:
# `x-goog-api-key`, GEMINI_PATH), "api-key" (Azure OpenAI: `api-key`, MODEL_PATHS), "converse"
# (Bedrock Converse: a Bedrock API key as `Authorization: Bearer`, which amazon-bedrock.ts sends
# ahead of SigV4; CONVERSE_PATH). Any other package (Cohere's native /chat, Vertex, gateways with
# their own paths or headers, media-only packages) can't run: the issue wants that said at start,
# not a 401 or 404 on every request (the protocol review, 2026-11-23).
PACKAGES = {"anthropic": "anthropic", "anthropic-compatible": "anthropic", "google": "google",
            "azure": "api-key", "amazon-bedrock": "converse",
            **dict.fromkeys(("alibaba", "cerebras", "cloudflare-workers-ai", "deepinfra",
                             "deepseek", "digitalocean", "fireworks", "groq", "meta", "minimax",
                             "mistral", "moonshot", "openai", "openai-compatible",
                             "openai-compatible-responses", "openrouter", "togetherai", "venice",
                             "xai", "zai", "zai-coding-plan"), "bearer")}
# A sub-package posting other than its package does, by its path under PACKAGE_PREFIX: Google's
# Interactions route (`/interactions`, its own events: not served), Bedrock Mantle's (OpenAI
# Responses or Chat, or Anthropic Messages, each with the Bedrock API key as a bearer:
# amazon-bedrock-mantle.ts; `amazon-bedrock-mantle` is its old name)
SUBPACKAGES = {"google/interactions": None, "amazon-bedrock-mantle": "bearer",
               **dict.fromkeys(("amazon-bedrock/mantle", "amazon-bedrock/mantle/responses",
                                "amazon-bedrock/mantle/chat", "amazon-bedrock/mantle/messages"),
                               "bearer")}
PROTOCOLS = ("bearer", "anthropic", "google", "api-key", "converse")
PACKAGE_PREFIX = "@opencode/ai/providers/"
AI_SDK_PREFIX = "@ai-sdk/"  # a config provider's V1 npm (#1137): read as PACKAGE_PREFIX
BODY_CAP = 32 << 20     # a 131K-token prompt is a few MB of JSON
ERROR_CAP = 64 << 10    # an upstream error body, read whole to redact it
SNIFF_CAP = 4 << 20     # a non-streamed JSON answer kept to read its model and usage
RATE_LIMIT_SECS = 60    # a 429 without Retry-After: how long the model is rate limited
# The hold (#1020): a 429, a 529 or an outage (a 5xx, a refused or reset connection, DNS, TLS) is
# held here and sent upstream again, waiting Retry-After or backing off BACKOFF; one OpenCode
# request for at most RATE_HOLD_S, under the 600 s header guard (#1017's), so OpenCode never cuts
# it; past it the last answer goes back, OpenCode prints `retry` and sends again. Status
# `ratelimit_until` (at least HOLD_FLOOR_S ahead) and `ratelimit` tell the runner, whose clocks
# pause meanwhile: OpenCode prints nothing while a request waits.
RATE_HOLD_S = 300
QUEUED_AFTER_S = 30     # a 200 with only SSE keep-alives this long: the provider's queue
HOLD_FLOOR_S = 60
BACKOFF = (2, 60)       # without Retry-After: 2, 4, 8 … 60 s
# Cloudflare's 520-524 (the origin's unknown error, down, refused, timed out) in front of a
# provider are as transient as a 502-504: held too (the engineer's call on #1020, 2026-11-23)
HOLD_CODES = {429: "rate_limited", 529: "rate_limited",   # 529: Anthropic's "overloaded"
              **dict.fromkeys((500, 502, 503, 504, 520, 521, 522, 523, 524), "outage")}
# A 429 or 403 that is a spent balance or plan limit, not a rate: no wait helps (OpenAI's
# insufficient_quota, OpenRouter's limit_source, a plan's 5-hour or weekly limit)
QUOTA = re.compile(r"(?i)insufficient[_ ](?:quota|balance|credits?|funds)|exceeded your current "
                   r"quota|out of credits?|credits? (?:are |is )?(?:exhausted|used up)|limit_source"
                   r"|\b(?:5|five)[- ]hour\b|weekly (?:usage )?limit|plan(?:'s)? (?:usage )?limit")
# A spent balance said in a 400 (Anthropic's invalid_request_error "Your credit balance is too low
# to access the Anthropic API. Please go to Plans & Billing to upgrade or purchase credits."): the
# task failed as the agent's, no no_credit, no remedy (#1079, 2026-11-23). Only this wording on a
# 400, never a bare "billing": an ordinary bad request may mention billing.
CREDIT = re.compile(r"(?i)credit balance is too low|purchase credits|plans (?:&|and) billing")
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
       "trailers", "transfer-encoding", "upgrade", "proxy-connection"}
CLIENT_AUTH = {"authorization", "x-api-key", "api-key", "x-goog-api-key"}
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
SPEND_REFUSED = "the day's API spend cap is reached"  # meter.STOP too: a refusal OpenCode doesn't retry


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


def _package_path(package: str) -> str:
    """``@opencode/ai/providers/meta/messages`` → ``meta/messages``, ``@ai-sdk/openai-compatible``
    → ``openai-compatible``; a bare name stays as it is."""
    name = package or ""
    # a config provider's V1 npm, the form OpenCode's docs give a custom provider: read as its
    # scope, `@ai-sdk`, every such provider was refused as one the keyhole can't speak (#1137,
    # 2026-11-23). OpenCode 2.3.2 runs `@ai-sdk/<name>` as its own `@opencode/ai/providers/<name>`
    # where it has one (model.list prints that); an unknown `@ai-sdk/foo` still reads as
    # None through PACKAGES, and its `aisdk:@ai-sdk/foo` (an npm package OpenCode loads) too.
    for prefix in (PACKAGE_PREFIX, AI_SDK_PREFIX):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def _package_name(package: str) -> str:
    """``@opencode/ai/providers/meta/messages`` → ``meta``, ``@ai-sdk/openai-compatible`` →
    ``openai-compatible``; any other scoped package reads as its scope (``@acme``), which
    PACKAGES never holds."""
    return _package_path(package).split("/")[0]


def protocol_of(package: str) -> str | None:
    """How the provider takes its key and where the model is named (PROTOCOLS), from PACKAGES or
    SUBPACKAGES; None for a package the keyhole can't carry."""
    path = _package_path(package)
    if path in SUBPACKAGES:
        return SUBPACKAGES[path]
    return PACKAGES.get(_package_name(package))


def _azure_openai(package: str, base_url: str) -> bool:
    """An Azure OpenAI base OpenCode rewrites (azure.ts `endpoint`): the azure package's, its host
    `*.openai.azure.com`. Any other host OpenCode posts to as it is, and so does the keyhole."""
    host = (urllib.parse.urlsplit(base_url).hostname or "").lower()
    return _package_name(package) == "azure" and host.endswith(".openai.azure.com")


def mirror_path(package: str, base_url: str) -> str:
    """The upstream base the keyhole mirrors: the catalog's, with what the package appends to it
    already appended, so OpenCode posts the keyhole's base plus a model path exactly (DeepInfra
    adds `/openai` unless the base ends in it: providers/deepinfra.ts `configure`; Azure `/v1` to
    an `*.openai.azure.com` base, which on 127.0.0.1 it wouldn't: azure.ts `endpoint`, #1192)."""
    root = base_url.rstrip("/")
    if _package_name(package) == "deepinfra" and not root.endswith("/openai"):
        return f"{root}/openai"
    if _azure_openai(package, base_url):
        return f"{root}/v1"
    return base_url


def upstream_query(package: str, base_url: str) -> dict:
    """What the keyhole adds to each request's query on the way: Azure's `api-version=v1`, which
    OpenCode adds only for an `*.openai.azure.com` base (azure.ts `endpoint`), never the
    keyhole's (#1192)."""
    return {"api-version": "v1"} if _azure_openai(package, base_url) else {}


def _signs(provider: str, package: str = "") -> bool:
    """A provider that signs each request (Vertex), which a header swap can't do. Bedrock isn't
    one by name (#1192): OpenCode 2.3.2 sends a Bedrock API key as a bearer ahead of SigV4
    (amazon-bedrock.ts, amazon-bedrock-mantle.ts), so only Bedrock without a stored key signs
    (load_key, _bedrock)."""
    return "google-vertex" in f"{provider} {package}"


def _bedrock(provider: str, package: str = "") -> bool:
    return any(s in f"{provider} {package}" for s in ("amazon-bedrock", "bedrock"))


def _lone_env(prov) -> tuple[str, str]:
    """(where, VAR) of a provider-level `options.apiKey` that is only `{env:VAR}`, else ("", "").
    The layer's overlay replaces it with the token and it names no secret, and OpenCode 2.3.2
    sends a stored /connect key over it (checked, 2026-11-23): with a key stored too it refused,
    telling the user to /connect again (#1075). Only V1 `options`, as checked: a V2 `settings`
    one still refuses."""
    for where in ("options",):
        opts = prov.get(where) if isinstance(prov, dict) else None
        raw = opts.get("apiKey") if isinstance(opts, dict) else None
        if isinstance(raw, str) and (lone := re.fullmatch(r"\s*\{env:([^}]+)\}\s*", raw)):
            return where, lone.group(1).strip()
    return "", ""


def _configured_key(provider: str) -> tuple[str | None, str]:
    """(where the user's OpenCode config gives ``provider`` an apiKey the agent could read: a
    file, or OPENCODE_CONFIG_CONTENT (the agent's environment), "env:<VAR>" for a `{env:VAR}` one
    elsewhere (a header could replace the token); VAR of a lone provider-level `{env:VAR}` apiKey,
    which a stored key may stand beside, _lone_env)."""
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
    found, env_var = None, ""
    for name, layer in layers:
        layer = layer if isinstance(layer, dict) else {}
        # both forms, V1 `provider` and V2 `providers`, and the whole entry: a model's own
        # options, headers, V2 settings and variants carry a key as well as the provider's, and
        # OpenCode 2.3.2 sends a model's over its provider's (v1/config/migrate.ts migrateModel;
        # the review of #1015, 2026-11-23)
        for form in ("provider", "providers"):
            prov = layer[form].get(provider) if isinstance(layer.get(form), dict) else None
            where, var = _lone_env(prov) if form == "provider" else ("", "")
            if where:
                env_var = var
                prov = {**prov, where: {k: v for k, v in prov[where].items() if k != "apiKey"}}
            for val in _keys_in(prov):
                env = re.search(r"\{env:([^}]+)\}", val)
                found = f"env:{env.group(1)}" if env else name
    return found, env_var


def _keys_in(entry) -> list[str]:
    """Every key a provider's config entry holds, at any depth: an `apiKey`, and a key written
    as a header (the leak review, 2026-11-23: the agent can read it, and OpenCode would send it
    in place of the token)."""
    out = []
    if isinstance(entry, list):
        for v in entry:
            out += _keys_in(v)
    if not isinstance(entry, dict):
        return out
    for k, v in entry.items():
        if k == "apiKey" and isinstance(v, str) and v.strip():
            out.append(v)
        elif k == "headers" and isinstance(v, dict):
            out += [h for hk, h in v.items() if isinstance(h, str) and h.strip() and (
                _AUTH_HEADER.search(str(hk)) or re.search("|".join(KEYLIKE[:2]), h))]
        else:
            out += _keys_in(v)
    return out


# A header the user's config may carry a key in: Authorization, x-api-key, api-key, x-goog-api-key,
# cf-aig-authorization, a token header.
_AUTH_HEADER = re.compile(r"(?i)auth|key|token")


def _opencode(*args: str) -> subprocess.CompletedProcess:
    """An OpenCode command run as models._opencode_json runs one: `--standalone`, in the state
    directory with the project config shut out. Without `--standalone` the first API task started
    OpenCode's background service with the runner's environment (PYTHONPATH, CLAUDECODE=1), left
    running for the user's own OpenCode to attach to, and where another home's service held its
    port the export exited 1 and the task failed at start (#1070, 2026-11-23)."""
    from . import runner
    from .media import run_group  # its group killed whole on a timeout, as _opencode_json's
    where = store.home()
    where.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PWD": str(where), "OPENCODE_DISABLE_PROJECT_CONFIG": "1"}
    try:
        return run_group([*runner.opencode_bin(), *args, "--standalone"], 60, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, cwd=where, env=env,
                         stdin=subprocess.DEVNULL)
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
        raise KeyholeError(f"{NEEDS}: {provider} signs each request (Vertex)")
    where, env_var = _configured_key(provider)
    if where:
        if where.startswith("env:"):
            raise KeyholeError(f"{NEEDS}: {provider}'s key is only in the environment "
                               f"(${where[4:]}), which agents don't get")
        # /connect alone changed nothing while the written key stayed (#1075, 2026-11-23)
        raise KeyholeError(f"{NEEDS}: its key is written in {where}, which agents can read: "
                           "remove it there, and store the key with /connect")
    r = _opencode("auth", "export", provider)
    if r.returncode != 0 and env_var:  # no stored key beside it (OpenCode's "not found"): say why
        raise KeyholeError(f"{NEEDS}: {provider}'s key is only in the environment (${env_var}), "
                           "which agents don't get")
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
    # An OAuth login the user switched to (`opencode auth switch`) over an older key: the key
    # was billed, the login OpenCode itself would use ignored (#1110, 2026-11-23)
    # (a login alone is refused below, as it always was). Any active credential that isn't a
    # key, not only OAuth or external: a type OpenCode adds later (1.x had `wellknown`) would
    # otherwise bill the inactive key the same way (the review of #1110, 2026-11-23)
    active = rows[0]["value"].get("type") if rows and rows[0].get("active") and any(
        r["value"].get("type") == "key" for r in rows) else None
    if active and active != "key":
        what = ("comes from an external source" if active == "external" else
                "is an OAuth login, whose tokens rotate" if active == "oauth" else
                f"is a {active} credential, not an API key")
        raise KeyholeError(f"{NEEDS}: {provider}'s active credential {what}: `opencode auth "
                           "switch` to its API key, or store one with /connect")
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
    if var := env_var or _env_only(provider):
        raise KeyholeError(f"{NEEDS}: {provider}'s key is only in the environment (${var}), "
                           "which agents don't get")
    if _bedrock(provider, package):  # an AWS login or profile: SigV4, which a header swap can't do
        raise KeyholeError(f"{NEEDS}: {provider} signs each request (SigV4) without a Bedrock "
                           "API key stored in OpenCode")
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
    (one of PROTOCOLS), ``query`` (what each forwarded query gets set: upstream_query). It serves
    at once, from a daemon thread, on 127.0.0.1 (never localhost, which may be ::1 first).
    ``on_usage(usage)`` gets each response's usage, once, before its end marker (`[DONE]`,
    `message_stop`, `response.completed`) or else its last byte
    goes to OpenCode (the meter, #1019, #1114); ``on_health(state, why, until)``
    each outcome (ok, no_credit, rate_limited, unreachable); ``room(body, size)`` is asked before
    each request is sent on (the meter's spend room: negative is refused); ``log`` the start and
    refusal lines (runner.log: the runner's stdout)."""

    def __init__(self, task, model, key: Key, upstream: str, protocol: str = "bearer", *,
                 timeout: float | None = None, on_usage=None, on_health=None, log=None,
                 hold: float | None = None, room=None, order=None, query=None):
        if not isinstance(key, Key):
            raise TypeError("key must be a Key")
        if protocol not in PROTOCOLS:
            raise KeyholeError(f"{NEEDS}: {model.provider} takes its key in a way the keyhole "
                               "doesn't speak")
        up = urllib.parse.urlsplit(upstream)
        if up.scheme not in ("https", "http") or not up.hostname:
            raise KeyholeError(f"{model.id}'s API base isn't an http(s) URL")
        self.task, self.model, self._key, self.protocol = task, model, key, protocol
        self.query = {str(k): str(v) for k, v in (query or {}).items()}
        # OpenRouter's backends, as the role chose them (task.json `order`, #1021): every request
        # is routed to them alone, and a response from another is an event and a report line
        self.order = [str(b) for b in order or () if str(b).strip()]
        self._up = up
        self.upstream_path = up.path.rstrip("/")
        self.timeout = timeout if timeout is not None else _timeout(task)
        self.on_usage = on_usage or (lambda usage: None)
        self.on_health = on_health or (lambda state, why, until: None)
        self.room = room
        self.stopped: str | None = None  # close(reason): every model request refused from now on
        self.log = log or (lambda line: print(line, flush=True))
        self.token = secrets.token_urlsafe(32)
        self._hold = hold
        self.closed = False
        self._marked = False  # status ratelimit written and not yet cleared
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

    @property
    def hold(self) -> float:
        """How long one request is held through a rate limit or outage: RATE_HOLD_S, read each
        request, unless given."""
        return RATE_HOLD_S if self._hold is None else self._hold

    def close(self, reason: str | None = None) -> None:
        """Shut the keyhole down; with ``reason``, keep serving but refuse every model request
        at once (the spend stop, #1019): a 400 OpenCode doesn't retry, so a round stopped for
        spend never waits out a retry loop. Either way a held request stops holding (#1020)."""
        if reason is not None:
            self.stopped = reason
            return
        self.closed = True  # a held request stops holding
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # what the handler asks
    def token_ok(self, headers) -> bool:
        """The token in the header the package sends it in: `Authorization: Bearer`, else
        `x-api-key`, `api-key` (Azure's) or `x-goog-api-key` (Gemini's, #1192). Every one of them
        is dropped on the way (CLIENT_AUTH), so which one carried it changes nothing upstream."""
        auth = headers.get("Authorization") or ""
        given = auth[7:].strip() if auth[:7].lower() == "bearer " else next(
            (v for h in ("x-api-key", "api-key", "x-goog-api-key") if (v := headers.get(h))), "")
        return hmac.compare_digest(given.encode(), self.token.encode())

    def refused(self, name: str) -> None:
        with self._lock:
            if name in self._refused:
                return
            self._refused.add(name)
        self.log(f"keyhole: refused {_printable(name)} (this task runs {self.model.id})")

    def auth_header(self) -> tuple[str, str]:
        header = {"anthropic": "x-api-key", "google": "x-goog-api-key", "api-key": "api-key"}
        if name := header.get(self.protocol):
            return name, self._key.value
        return "Authorization", f"Bearer {self._key.value}"  # bearer, and Bedrock's API key

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
        if self.model.provider == privacy.OPENROUTER and privacy.outside_order(now["by"], self.order):
            self.task.event({"ts": store.now(), "round": self.task.status.get("round", 1),
                             "kind": "backend", "by": now["by"],
                             "text": f"OpenRouter served {_printable(now['by'])}, not "
                                     f"{', '.join(_printable(b) for b in self.order)}"})

    def route_body(self, body: dict, raw: bytes) -> bytes:
        """The bytes forwarded: an OpenRouter request's `provider` routing set here, not trusted
        to OpenCode (2.3.2 builds it from the model's `settings.provider`, which its resolver
        replaces with the catalog's `canonical` provider id when one is set, model-resolver.ts):
        the role's `order` and `allow_fallbacks: false` over whatever the body had; the rest of
        its `provider` object (data_collection, zdr, only, ignore: each only narrows) kept.
        Any other provider's body goes on byte for byte."""
        if self.model.provider != privacy.OPENROUTER:
            return raw
        given = body.get("provider") if isinstance(body.get("provider"), dict) else {}
        routing = privacy.routing(self.order)
        if not self.order:  # none chosen (a gated --model start): no fallback all the same
            routing.pop("order")
        body["provider"] = {**given, **routing}
        return json.dumps(body).encode()

    def health(self, state: str, why: str = "", until: float | None = None) -> None:
        try:
            self.on_health(state, why, until)
        except Exception:  # a hook's failure never fails a model request
            pass

    def cooldown(self) -> float:
        """The provider's rate limit as recorded in api-health.json (by any task's keyhole: three
        API tasks of a provider share one account's limit): its `until`, or 0."""
        try:
            from . import models
            row = models.api_health(self.model.provider)
        except Exception:
            return 0.0
        if row and row.get("state") == "rate_limited" and isinstance(row.get("until"), (int, float)):
            return float(row["until"])
        return 0.0

    def held(self, kind: str, code, wait: float | None, why: str = "") -> None:
        """Status for a held answer or a queued stretch (#1020): `ratelimit_until` (now + the wait,
        at least HOLD_FLOOR_S) and `ratelimit` {kind, provider, code, since, retry_after}, `since`
        kept while the run goes on; `last` says it. Never the upstream body."""
        if self.task is None:
            return
        now, p = time.time(), self.model.provider
        with self._lock:
            prev = self.task.status.get("ratelimit")
            prev = prev if isinstance(prev, dict) else None
            same = prev is not None and (prev.get("kind") == "queued") == (kind == "queued")
            since = prev["since"] if same and isinstance(prev.get("since"), (int, float)) else now
            row = {"kind": kind, "provider": p, "code": code, "since": since,
                   "retry_after": None if wait is None else round(wait), **({"why": why} if why else {})}
            if kind == "rate_limited":
                last = f"rate-limited by {p} (HTTP {code}): waiting, retry in {wait:.0f} s"
            elif kind == "outage":
                last = f"{p} isn't answering ({f'HTTP {code}' if code else why}): waiting, retry in {wait:.0f} s"
            else:
                last = f"{p} holds the request in its queue: waiting for it to start"
            self.task.set_status(ratelimit_until=now + max(wait or 0, HOLD_FLOOR_S), ratelimit=row,
                                 last=last)
            self._marked = True

    def answered(self) -> None:
        """The first data after a hold or a queue: both status keys cleared."""
        if self.task is None or not self._marked:
            return
        with self._lock:
            self._marked = False
            self.task.set_status(ratelimit_until=None, ratelimit=None)

    def quota(self, code: int) -> str:
        """A spent balance or plan: `api_stop` (the runner stops the round, shared with #1019's
        spend stop) and the text OpenCode is answered with."""
        text = f"{self.model.provider} says the account is out of credit (HTTP {code})"
        if self.task is not None:
            self.task.set_status(api_stop={"guard": "quota", "text": text})
        return text

    def unreachable(self, why: str) -> str:
        """This machine's setup can't reach the provider (a certificate that doesn't verify, a
        proxy refusing the tunnel): `api_stop` with guard `setup`, so the round stops at once and
        isn't revived, and the text OpenCode is answered with. As a 502 it was held, read as the
        provider's outage for 10 minutes, then run again (#1080, 2026-11-23)."""
        text = f"this machine can't reach {self.model.provider} ({self._up.hostname}): {why}"
        if self.task is not None:
            self.task.set_status(api_stop={"guard": "setup", "text": text})
        return text

    def usage(self, usage) -> None:
        try:
            self.on_usage(usage)
        except Exception:
            pass

    def refusal(self, body: dict, size: int) -> str | None:
        """Why this request isn't sent on, or None: the keyhole was closed with a reason, or
        the meter's room for it is negative (the day cap, #1019)."""
        if self.stopped:
            return self.stopped
        if self.room is None:
            return None
        try:
            left = self.room(body, size)
        except Exception:  # the meter's failure never fails a request
            return None
        return SPEND_REFUSED if left is not None and left < 0 else None

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
        # webpush's context: the default one, plus macOS's own bundle when Python's store is empty
        # (python.org's Python before Install Certificates.command). The bare default failed every
        # request there while OpenCode, with Bun's CAs, worked (#1080, 2026-11-23).
        from .webpush import tls
        ctx = tls()
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


def _after_secs(value: str | None) -> float | None:
    """Retry-After as seconds from now (seconds or an HTTP date), or None without one."""
    if not value:
        return None
    return max(0.0, _retry_after(value) - time.time())


# Google's rate signals in a 429 body (google.rpc QuotaFailure and RetryInfo): a quota id per
# minute (`GenerateRequestsPerMinutePerProjectPerModel-FreeTier`), and the wait it asks
_PER_MINUTE = re.compile(r"PerMinute")
_RETRY_DELAY = re.compile(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"')


def _google_rate(text: str) -> tuple[bool, float | None]:
    """(a per-minute quota, RetryInfo's retryDelay in seconds or None) of an upstream error's
    body. Only a per-minute quota is a rate: a daily one waits past any hold, as a spent
    balance does."""
    delay = _RETRY_DELAY.search(text or "")
    return bool(_PER_MINUTE.search(text or "")), float(delay.group(1)) if delay else None


def _unreachable_why(kh: Keyhole, e: Exception) -> str:
    if isinstance(e, ssl.SSLCertVerificationError):
        return (f"its certificate didn't verify ({getattr(e, 'verify_message', '') or e})"
                + ("; Python has no CA certificates: on macOS run its Install "
                   "Certificates command" if sys.platform == "darwin" else ""))
    if isinstance(e, socket.gaierror):
        return f"no address for it ({e.strerror or e})"
    return str(e) or type(e).__name__


def _transient(e: Exception) -> bool:
    """A failure that may pass on its own (refused, reset, DNS, TLS, a timeout): held. Not a
    certificate that doesn't verify, nor a proxy refusing the tunnel: those are the setup's, and
    stop the round at once (_setup_failure, #1080)."""
    if isinstance(e, ssl.SSLCertVerificationError):
        return False
    return isinstance(e, (ConnectionError, socket.gaierror, socket.timeout, TimeoutError,
                          ssl.SSLError, http.client.IncompleteRead))


def _setup_failure(e: Exception) -> bool:
    """A transport failure that is this machine's setup, never passing on its own: a certificate
    that doesn't verify, or the proxy refusing the tunnel (http.client's "Tunnel connection
    failed: 403 …", or 407 for its login). Only a 4xx: a proxy's 502, 503 or 504 is the
    provider's host down or slow behind it, an outage that keeps its old path (the review of
    #1080, 2026-11-23)."""
    return isinstance(e, ssl.SSLCertVerificationError) or (
        isinstance(e, OSError) and str(e).startswith("Tunnel connection failed: 4"))


def _short_why(e: Exception) -> str:
    """A transport failure in a few words, for status `ratelimit` and the stop's text."""
    if isinstance(e, ConnectionRefusedError):
        return "connection refused"
    if isinstance(e, ConnectionResetError):
        return "connection reset"
    if isinstance(e, socket.gaierror):
        return "no address"
    if isinstance(e, ssl.SSLError):
        return "TLS error"
    if isinstance(e, (socket.timeout, TimeoutError)):
        return "timed out"
    return "no connection"


def _handler(kh: Keyhole):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "workbench-keyhole"
        sys_version = ""
        retry_delay = None  # the last upstream error's RetryInfo wait (read_error, Google's)

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
            """The model path under the keyhole's base (exactly the base plus one of the
            protocol's model paths: MODEL_PATHS, GEMINI_PATH, CONVERSE_PATH; or `/models` for a
            GET, none for Converse's), or None. No segment between: the base mirrors the
            upstream's, so OpenCode never posts one, and `/v1/threads/<id>/messages` went on with
            the key (the reviews, 2026-11-23). A path-model one's model is do_POST's to check."""
            path = urllib.parse.urlsplit(self.path).path
            base = kh.upstream_path
            rest = path[len(base):] if path.startswith(base + "/") else ""
            if method == "GET":
                return rest if rest == "/models" and kh.protocol != "converse" else None
            if kh.protocol == "google":
                return rest if GEMINI_PATH.fullmatch(rest) else None
            if kh.protocol == "converse":
                return rest if CONVERSE_PATH.fullmatch(rest) else None
            return rest if rest in MODEL_PATHS else None

        def query_ok(self) -> bool:
            query = urllib.parse.urlsplit(self.path).query
            if not QUERY.fullmatch(query):
                self.send_json(400, "keyhole: a query that isn't printable ASCII", close=True)
                return False
            if kh.protocol != "google" or self.command == "GET" and not query:
                return True
            # Gemini takes a key in the query too (`?key=`): a second way in, beside the
            # header the keyhole swaps; OpenCode 2.3.2 sends `alt=sse` alone (#1192)
            names = [k.lower() for k, _ in urllib.parse.parse_qsl(query, keep_blank_values=True)]
            if "key" in names:
                self.send_json(400, "keyhole: a key in the query is refused", close=True)
                return False
            if query != "alt=sse" and self.command == "POST":
                self.send_json(400, "keyhole: Gemini's model path takes only ?alt=sse", close=True)
                return False
            return True

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
            elif kh.protocol == "google":  # Google's listing shape (models.list)
                listing = {"models": [{"name": f"models/{name}", "displayName": name,
                                       "supportedGenerationMethods": ["streamGenerateContent"]}]}
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
            if (other := self.other_model(body)) is not None:
                kh.refused(other)
                return self.send_json(403, f"keyhole: this task may use only {kh.model.id}")
            if why := kh.refusal(body, len(raw)):  # a 400: OpenCode doesn't retry it (#1019)
                return self.send_json(400, f"workbench: {_printable(why)}")
            self.forward(kh.route_body(body, raw), body, len(raw))

        def other_model(self, body: dict) -> str | None:
            """What the request names in place of the task's model (logged), or None. The body's
            `model`, and no fallback list; for Gemini and Converse the path's, as the model is
            there (#1192): Gemini's only under `models/` (a `tunedModels/` one is another model),
            Converse's as OpenCode writes it (encodeURIComponent: one written otherwise, or a
            body's `modelId` other than it, may name another), and a body naming any `model`."""
            name = kh.model.name
            if "models" in body:  # a fallback list routes elsewhere
                return "a fallback list"
            rest = self.route("POST") or ""
            if kh.protocol == "google":
                kind, asked = GEMINI_PATH.fullmatch(rest).groups()
                if kind != "models" or asked != name:
                    return f"{kind}/{asked}"
            elif kh.protocol == "converse":
                asked = CONVERSE_PATH.fullmatch(rest).group(1)
                if asked != urllib.parse.quote(name, safe="-_.!~*'()"):
                    return urllib.parse.unquote(asked)
                if body.get("modelId", name) != name:
                    return str(body.get("modelId"))
            else:
                return None if body.get("model") == name else str(body.get("model"))
            return str(body["model"]) if "model" in body else None

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

        def forward(self, raw: bytes, body: dict | None = None, size: int = 0):
            """Send ``raw`` on, holding through rate limits and outages (#1020). ``body``, ``size``:
            the request as asked, whose spend room (kh.refusal, #1019) is asked again before each
            pass after the first: a hold once sent a request on after another task, or the wait,
            had used the day's room up (wave 2's review)."""
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
            if kh.query:  # Azure's api-version (upstream_query), over any OpenCode sent
                pairs = [(k, v) for k, v in urllib.parse.parse_qsl(query, keep_blank_values=True)
                         if k not in kh.query]
                query = urllib.parse.urlencode(pairs + list(kh.query.items()))
            target = urllib.parse.urlsplit(self.path).path + (f"?{query}" if query else "")
            deadline = time.monotonic() + kh.hold
            backoff = BACKOFF[0]
            first = True
            while True:
                if not first and body is not None and (why := kh.refusal(body, size)):
                    return self.send_json(400, f"workbench: {_printable(why)}")  # as do_POST's
                first = False
                # another task's 429 on this provider (api-health.json): wait it out first, within
                # this request's hold
                cool = kh.cooldown() - time.time()
                if cool > 0 and (left := deadline - time.monotonic()) > 0:
                    kh.held("rate_limited", 429, cool)
                    if not self.nap(min(cool, left)):
                        return self.drop()
                conn = kh.connection()
                try:
                    try:
                        conn.request("POST", target, body=raw, headers=heads)
                        resp = conn.getresponse()
                    except Exception as e:
                        if isinstance(e, (OSError, http.client.HTTPException)) and not isinstance(
                                e, http.client.InvalidURL):
                            why = kh.redact(_unreachable_why(kh, e))
                            kh.health("unreachable", why)
                            if _setup_failure(e):  # the round stops: no wait or retry helps (#1080)
                                return self.send_json(400, f"keyhole: {kh.unreachable(why)}: "
                                                           "the round stops")
                            how = (self.hold_on("outage", None, backoff, deadline, _short_why(e))
                                   if _transient(e) else "pass")
                            if how == "again":
                                backoff = min(backoff * 2, BACKOFF[1])
                                continue
                            if how == "gone":
                                return self.drop()
                            return self.send_json(502, f"keyhole: can't reach {kh._up.hostname}: {why}",
                                                  etype="api_error")
                        # nothing was sent, and not the upstream's fault (InvalidURL, a ValueError or
                        # UnicodeError from http.client): never `unreachable`, and only the type, since
                        # putheader's ValueError quotes the header, the key in it (the reviews,
                        # 2026-11-23)
                        return self.send_json(502, f"keyhole: can't send this request on "
                                                   f"({type(e).__name__})", etype="api_error")
                    if 200 <= resp.status < 300:
                        return self.stream(resp)
                    # without Retry-After, the provider's cooldown is the backoff this keyhole
                    # waits itself, not RATE_LIMIT_SECS: its own next pass reads it back (cooldown)
                    status, sent, text, kind = self.read_error(
                        resp, backoff if backoff <= deadline - time.monotonic() else None)
                    if kind == "quota":  # no wait helps: the round stops, and OpenCode doesn't retry a 400
                        return self.send_json(400, f"keyhole: {kh.quota(status)}: the round stops")
                    if kind in ("rate_limited", "outage"):
                        after = _after_secs(resp.getheader("Retry-After"))
                        if after is None:
                            after = self.retry_delay
                        how = self.hold_on(kind, status, backoff if after is None else after, deadline)
                        if how == "again":
                            if after is None:
                                backoff = min(backoff * 2, BACKOFF[1])
                            continue
                        if how == "gone":
                            return self.drop()
                    return self.send_error(status, sent, text)
                finally:
                    conn.close()

        def hold_on(self, kind: str, code, wait: float, deadline: float, why: str = "") -> str:
            """A held answer: status written; "again" once ``wait`` has passed, "pass" when it
            would run past the request's hold (the answer goes back to OpenCode), "gone" when
            OpenCode hung up meanwhile (stopped, or steered)."""
            kh.held(kind, code, wait, why)
            if wait > deadline - time.monotonic():
                return "pass"
            return "again" if self.nap(wait) else "gone"

        def nap(self, secs: float) -> bool:
            """Sleep ``secs`` in short steps; False as soon as OpenCode hangs up or the keyhole
            closes: a held request is dropped with its client, never sent on for no one. A spend
            stop (close with a reason, #1019) ends a hold too: what it waited for may not go out."""
            end = time.monotonic() + secs
            while (left := end - time.monotonic()) > 0:
                if kh.closed or kh.stopped or self.gone():
                    return False
                time.sleep(min(0.25, left))
            return not (kh.closed or kh.stopped or self.gone())

        def gone(self) -> bool:
            try:
                ready, _, _ = select.select([self.connection], [], [], 0)
                return bool(ready) and not self.connection.recv(1, socket.MSG_PEEK)
            except (OSError, ValueError):
                return True

        def drop(self):
            self.close_connection = True

        def read_error(self, resp, wait: float | None = None):
            """(status, headers, redacted body, kind) of an upstream error: kind `quota`,
            `rate_limited`, `outage` or None (HOLD_CODES, QUOTA, CREDIT), and the provider's health: a
            rate limit until Retry-After, else ``wait`` (the hold's backoff) when given, else
            RATE_LIMIT_SECS; any 5xx unreachable, held or not."""
            data = resp.read(ERROR_CAP + 1)
            text = data[:ERROR_CAP].decode("utf-8", "replace")
            # read before redact, which takes Google's quota ids (52 letters) for a key
            per_minute, self.retry_delay = _google_rate(text)
            if len(data) > ERROR_CAP:
                # cut: a key straddling the cut would leave its first characters, which no
                # pattern knows (7 of a 32-character key passed, the review, 2026-11-23), so drop
                # as much as a key (twice: JSON may escape it) from the end
                text = text[:-2 * max(len(kh._key.value), 64)]
            text = kh.redact(text)
            status = resp.status
            kind = ("quota" if status == 402 or (status in (429, 403) and QUOTA.search(text))
                    or (status in (400, 403, 429) and CREDIT.search(text)) else HOLD_CODES.get(status))
            if kind == "quota" and status == 429 and per_minute:
                # Gemini's per-minute limit says "You exceeded your current quota" as its daily
                # one does, which read as a spent balance and stopped the round (#1192): a rate
                kind = "rate_limited"
            if kind == "quota" or "insufficient_quota" in text:
                kh.health("no_credit", _first_line(text))
            elif kind == "rate_limited":
                after = resp.getheader("Retry-After")
                if not after and self.retry_delay is not None:  # Google's RetryInfo, as Retry-After
                    after = f"{self.retry_delay:g}"
                # a 429 without Retry-After read back as 60 s held every retry 60 s, never
                # BACKOFF's 2, 4, 8 (the review of #1020, 2026-11-23)
                kh.health("rate_limited", _first_line(text),
                          time.time() + wait if wait is not None and not after else _retry_after(after))
            elif kind == "outage" or status >= 500:  # a 501, a 505 or a 525: not held, still told
                kh.health("unreachable", f"{status} {_first_line(text)}".strip())
            sent = [(k, kh.redact(v)) for k, v in resp.getheaders()
                    if k.lower() not in HOP and k.lower() not in ("content-length", "content-encoding",
                                                                  "connection")]
            return status, sent, text, kind

        def send_error(self, status: int, sent, text: str):
            out = text.encode()
            self.send_response(status)
            for k, v in sent:
                self.send_header(k, v)
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
            ctype = (resp.getheader("Content-Type") or "").lower()
            sse = "event-stream" in ctype and "amazon.eventstream" not in ctype
            frames = bytearray() if "amazon.eventstream" in ctype else None  # Bedrock's (#1192)
            self.used = {}  # this response's usage, merged: kh.usage once, before its end (#1114)
            key = kh._key.value.encode()
            hold = bytearray()  # SSE: a partial line; else the last len(key) - 1 bytes
            whole = bytearray()
            started, marked = False, time.monotonic()  # the first data; the queue's last mark

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
                        if not started and any(ln.strip() and not ln.startswith(b":") for ln in done):
                            started = True  # an event, not a keep-alive comment: it has begun
                            kh.answered()
                        elif not started and time.monotonic() - marked >= QUEUED_AFTER_S:
                            marked = time.monotonic()  # keep-alives only: the provider's queue
                            kh.held("queued", resp.status, None)
                    else:  # the key never passes, even split across two pieces
                        if not started:
                            started = True
                            kh.answered()
                        if frames is not None:
                            rest = self.frames(bytes(frames + piece))
                            frames = None if rest is None else bytearray(rest)
                        elif len(whole) < SNIFF_CAP and "eventstream" not in ctype:
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
                if not sse and whole:
                    try:
                        self.sniff(json.loads(whole))
                    except ValueError:
                        pass
                # usage that came after the end's marker, or with none (a non-SSE body): booked
                # before the last byte (#1019); with a marker it went at the marker (data_line)
                self.flush_usage()
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except (OSError, http.client.HTTPException):
                self.close_connection = True  # the client or the upstream went away mid-answer
                self.flush_usage()  # what the provider counted so far is still spent
                return

        def frames(self, buf: bytes) -> bytes | None:
            """AWS event-stream frames read for their payload's usage (Converse's `metadata`
            event: `{"usage": {inputTokens, …}}`): each `[total:4][headers:4][crc:4][headers]
            [payload][crc:4]`, big-endian; what isn't a whole frame yet is returned. A frame that
            can't be one (its length past SNIFF_CAP, or under the prelude's) is None: the stream
            still goes on, unread, and OpenCode's figure books it (Meter.step)."""
            while len(buf) >= 12:
                total, heads = int.from_bytes(buf[:4], "big"), int.from_bytes(buf[4:8], "big")
                if total > SNIFF_CAP or total < 16 + heads:
                    return None
                if len(buf) < total:
                    break
                try:
                    self.sniff(json.loads(buf[12 + heads:total - 4]))
                except ValueError:
                    pass
                buf = buf[total:]
            return buf

        def flush_usage(self):
            used, self.used = getattr(self, "used", None), {}
            if used:
                kh.usage(used)

        def data_line(self, ln: bytes) -> bytes:
            """One SSE line read for the model and usage, and as it goes on: with the key out,
            and through redact when it carries an `error` (a mid-stream error arrives inside a
            200: Anthropic's `event: error`, OpenRouter's `data: {"error": …}`)."""
            safe = ln.replace(kh._key.value.encode(), REDACTED.encode())
            s = ln.strip()
            if not s.startswith(b"data:"):
                return safe
            payload = s[5:].strip()
            if payload == b"[DONE]":
                # booked before the end's marker goes on: OpenCode prints the step's step_finish
                # when it reads [DONE], not at the body's last byte, and a provider sending its
                # last byte late had the response booked twice, OpenCode's row and then ours
                # (#1114, 2026-11-23; checked on 2.3.2: Anthropic's waits for the last byte)
                self.flush_usage()
            if not payload or payload == b"[DONE]":
                return safe
            try:
                obj = json.loads(payload)
            except ValueError:
                return safe
            self.sniff(obj)
            if isinstance(obj, dict) and obj.get("type") in ("message_stop", "response.completed"):
                self.flush_usage()  # the end of Anthropic's and the Responses API's stream (#1114)
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
            # Gemini's (#1192): `modelVersion` and `usageMetadata` in each chunk, the counts a
            # running total; no `[DONE]` and no end event, so it is booked at the stream's end,
            # which is where OpenCode ends the step too (gemini.ts: no terminal event, onHalt)
            served = (obj.get("model") or res.get("model") or obj.get("modelVersion")
                      or (msg.get("model") if obj.get("type") == "message_start" else None))
            if served:
                kh.saw(served, obj.get("provider"))
            usage = obj.get("usage") or msg.get("usage") or res.get("usage") or obj.get("usageMetadata")
            if isinstance(usage, dict) and usage:
                # one response, one usage: Anthropic's message_start and message_delta each carry
                # part, and some servers send a running total in every chunk (counts only grow)
                self.used = _merged(getattr(self, "used", None) or {}, usage)

    return Handler


def _merged(a: dict, b: dict) -> dict:
    """Two usage objects of one response as one: numbers the larger, objects merged alike."""
    out = dict(a)
    for k, v in b.items():
        old = out.get(k)
        if isinstance(v, dict) and isinstance(old, dict):
            out[k] = _merged(old, v)
        elif isinstance(v, (int, float)) and not isinstance(v, bool) and \
                isinstance(old, (int, float)) and not isinstance(old, bool):
            out[k] = max(old, v)
        elif v is not None or k not in out:
            out[k] = v
    return out


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


def start(task, m, *, log=None, on_usage=None, on_health=None, room=None) -> Keyhole:
    """The keyhole for task ``task``'s API model ``m`` (models.Model: id, provider, name,
    base_url, and the catalog's provider package as ``package``): its key loaded (load_key), its
    protocol from the package, its upstream the catalog's base. Logs one line, no key and no
    token. Raises KeyholeError when there is no key wb can hold, or no API base."""
    package = getattr(m, "package", "") or ""
    if _signs(m.provider, package):
        raise KeyholeError(f"{NEEDS}: {m.provider} signs each request (Vertex)")
    protocol = protocol_of(package)
    if protocol is None:
        raise KeyholeError(f"{NEEDS}: {m.provider} takes its key in a way the keyhole doesn't "
                           f"speak ({_printable(package) or 'no provider package'})")
    if not m.base_url:
        raise KeyholeError(f"{m.id} has no API base in OpenCode's catalog")
    key = load_key(m.provider, package)
    kh = Keyhole(task, m, key, mirror_path(package, m.base_url), protocol, log=log,
                 on_usage=on_usage, on_health=on_health, room=room,
                 query=upstream_query(package, m.base_url),
                 order=task.spec.get("order") if task is not None else None)  # #1021
    kh.log(f"keyhole: {m.provider} on 127.0.0.1:{kh.port} for {m.id}")
    return kh
