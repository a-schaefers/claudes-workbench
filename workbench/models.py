"""The local models workbench can hand work to: found in OpenCode's config, not listed here.

Every model of an OpenCode provider whose baseURL is on this machine is a candidate (a provider
on another host, such as a GPU box on the LAN, opts in with `"local": true`; an Ollama cloud
model, which runs off this machine, needs the same on its own id). Which one
takes a task is the router's guess from its size, corrected by real tasks (router.py), never
written down here, so a new model is one opencode.json entry away.

``~/.config/workbench/models.json`` (or ``$WB_MODELS``) adds what OpenCode's config can't say,
keyed by OpenCode model id or by provider id:

    {"bigserver": {"start": "bigserver launch --model coder", "stop": "bigserver stop",
                   "weights_gb": 55},
     "ollama/some-model": {"skip": true}}

A provider is driven as Ollama when its server answers Ollama's /api/version, whatever its port;
anything else is a server (`"engine": "ollama"|"server"` on the provider overrides the probe). A
server that lists several models (LM Studio, llama-swap, vLLM) swaps them itself: its models
are one server, stopped or started as one.

The runner makes the task's model resident before it starts (make_resident): it unloads Ollama
models and stops servers that have a stop command, then loads or starts the one it needs. A
GpuLease decides first whether it may instead run beside the tasks already running: when every
model fits the GPU's memory at once (a big card, several GPUs, Apple's unified memory), it loads
its own and leaves theirs; otherwise it waits for the GPU to itself. A model on another host
(`remote`) takes turns on its server instead (its own lease files, `pool`): this machine's GPU
is not the one it runs on, so it frees nothing here, and nothing here unloads or stops it.
"""
from __future__ import annotations

import fcntl
import functools
import ipaddress
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from . import hardware, store
from .watch import Watcher

LOCAL_HOSTS = {"localhost", "0.0.0.0", "host.docker.internal"}
OLLAMA_PORT = 11434  # Ollama's default: the guess while its server is down
START_TIMEOUT = 20 * 60  # a big model server's first start can take minutes
START_GRACE = 10  # after a start command fails: another process may be bringing the server up


@dataclass
class Model:
    id: str            # the OpenCode model id, "provider/model": the one name used everywhere
    provider: str
    # The model's name at its server: the entry's "id" when it has one (an alias: OpenCode sends
    # that), else its key. Taken from the key, an alias OpenCode ran was "the server lists N
    # models, not friendly" (the 2026-11-19 audit). The id keeps the key: it routes and selects.
    name: str
    base_url: str      # the provider's OpenAI-compatible base URL, ending in /v1
    engine: str        # "ollama" (loads models on demand) or "server" (an OpenAI-compatible one)
    label: str
    start: str | None = None
    stop: str | None = None
    extra: dict = field(default_factory=dict)
    config: dict = field(default_factory=dict)  # OpenCode's entry: reasoning, limit, variants...
    remote: bool = False  # on another host (models.json "local": true): its VRAM isn't this GPU's
    # An Ollama cloud model (`:cloud`, `-cloud`): Ollama forwards it to ollama.com, so it is no
    # candidate unless models.json gives its id "local": true (cloud_ok; it is then remote).
    cloud: bool = False
    cloud_ok: bool = False
    problem: str = ""  # a config mistake that would fail every request (url_problem)
    api_key: str = field(default="", repr=False)  # the provider's options.apiKey, resolved: never printed

    @property
    def root(self) -> str:
        return self.base_url.rstrip("/").removesuffix("/v1")

    @property
    def server(self) -> tuple:
        """The server behind the baseURL, one key however its host is spelled (server_key)."""
        return server_key(self.base_url, self.remote)


def server_key(url: str, remote: bool = False) -> tuple:
    """One key per server however its host is spelled: localhost, 127.0.0.1 and this machine's
    own name or address are one server. Compared as strings, two spellings of one Ollama made
    free_gpu unload the models it was told to keep (the 2026-11-16 audit), and wb model retire
    --delete delete the default's weights (#368, the audit of 1.6). No DNS lookup: this runs on
    every admission."""
    u = urlparse(url)
    here = not remote and _this_host(u.hostname)
    return ("this host" if here else (u.hostname or "").lower(),
            u.port or (443 if u.scheme == "https" else 80),
            u.path.rstrip("/").removesuffix("/v1"))


def _config_dir() -> Path:
    """OpenCode's global directory as OpenCode 2.3 takes it: $OPENCODE_CONFIG_DIR, else
    $XDG_CONFIG_HOME/opencode. A user keeping it in a dotfiles directory had "no local models"
    (#370, the audit of 1.6)."""
    if os.environ.get("OPENCODE_CONFIG_DIR"):
        return Path(os.environ["OPENCODE_CONFIG_DIR"]).expanduser().absolute()
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "opencode"


def _system_dir() -> Path:
    """The system config OpenCode layers first: an admin's, or a managed install's (#370)."""
    return Path("/Library/Application Support/opencode" if sys.platform == "darwin" else "/etc/opencode")


def _system_files() -> list[Path]:
    d = _system_dir()
    return [d / "opencode.json", d / "opencode.jsonc"]


def opencode_config_path() -> Path:
    """OpenCode's global config, where OpenCode reads it: $OPENCODE_CONFIG_DIR, else
    $XDG_CONFIG_HOME/opencode (default ~/.config/opencode, on macOS too), opencode.json or
    opencode.jsonc: the one wb writes."""
    if os.environ.get("WB_OPENCODE_CONFIG"):
        return Path(os.environ["WB_OPENCODE_CONFIG"])
    d = _config_dir()
    return next((p for p in (d / "opencode.json", d / "opencode.jsonc") if p.exists()),
                d / "opencode.json")


def _global_files() -> list[Path]:
    d = _config_dir()
    return [d / "opencode.json", d / "opencode.jsonc"]


def _users_config() -> Path | None:
    """The user's own OPENCODE_CONFIG, never a task's layer (the agent's, set in its environment)."""
    raw = os.environ.get("OPENCODE_CONFIG")
    if not raw:
        return None
    path = Path(os.path.expanduser(raw)).absolute()
    return None if path.resolve().is_relative_to(store.home().resolve()) else path


def opencode_config_files() -> list[Path]:
    """Every file OpenCode layers, in its order: the system directory's, then the global
    directory's opencode.json, then opencode.jsonc (both when both exist: checked on OpenCode 2.3,
    the 2026-11-16 audit), then the user's OPENCODE_CONFIG. $WB_OPENCODE_CONFIG alone. Unread, the
    user's OPENCODE_CONFIG hid its providers from wb (the 2026-11-19 audit), and so did the system
    file (#370)."""
    paths = [Path(os.environ["WB_OPENCODE_CONFIG"])] if os.environ.get("WB_OPENCODE_CONFIG") else \
        [*_system_files(), *_global_files(), *filter(None, [_users_config()])]
    return [p for p in paths if p.exists()]


def _strip_jsonc(text: str) -> str:
    """JSON with comments and trailing commas (OpenCode accepts both) as plain JSON."""
    text = re.sub(r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*.*?\*/',
                  lambda m: m.group(0) if m.group(0)[0] == '"' else "", text, flags=re.S)
    return re.sub(r'"(?:\\.|[^"\\])*"|,(\s*[}\]])',
                  lambda m: m.group(1) if m.group(1) is not None else m.group(0), text)


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _layers() -> list[tuple[Path, dict]]:
    """Each config file OpenCode layers, parsed, in its order."""
    out = []
    for path in opencode_config_files():
        try:
            out.append((path, json.loads(_strip_jsonc(path.read_text()))))
        except ValueError as e:  # one bad hand edit crashed every model command with a traceback
            raise RuntimeError(f"{path} doesn't parse ({e}); fix it by hand "
                               "(wb doctor checks it)") from None  # (the 2026-11-16 audit)
    return out


def users_config_content() -> dict:
    """The user's own OPENCODE_CONFIG_CONTENT, parsed: OpenCode loads it after every file, so a
    provider kept there alone was invisible to wb (#347, the audit of 1.4). Unset, unparsed, or
    beside $WB_OPENCODE_CONFIG (which stands alone): nothing."""
    raw = os.environ.get("OPENCODE_CONFIG_CONTENT")
    if not raw or os.environ.get("WB_OPENCODE_CONFIG"):
        return {}
    try:
        cfg = json.loads(_strip_jsonc(raw))
    except ValueError:
        return {}
    return cfg if isinstance(cfg, dict) else {}


def read_config() -> dict:
    """OpenCode's config as OpenCode sees it: its files merged, the later winning, then the
    user's OPENCODE_CONFIG_CONTENT. Read one file only, a provider kept in opencode.jsonc beside
    an opencode.json holding a theme was invisible (the 2026-11-16 audit)."""
    cfg: dict = {}
    for _, layer in _layers():
        cfg = _merge(cfg, layer)
    return _merge(cfg, users_config_content())


def _providers_of(layer: dict) -> dict:
    return layer.get("provider") or layer.get("providers") or {}


def _provider_dirs() -> dict[str, Path]:
    """Each provider's config file's directory, where its relative {file:} paths start."""
    return {pid: path.parent for path, layer in _layers() for pid in _providers_of(layer)}


def _absolute_files(value, base: Path):
    """``value`` with each relative {file:path} made absolute against ``base``."""
    if isinstance(value, dict):
        return {k: _absolute_files(v, base) for k, v in value.items()}
    if isinstance(value, list):
        return [_absolute_files(v, base) for v in value]
    if not isinstance(value, str):
        return value
    def one(mt):
        arg = mt.group(2).strip()
        if mt.group(1) != "file" or arg.startswith("~") or Path(arg).is_absolute():
            return mt.group(0)
        return f"{{file:{base / arg}}}"
    return _TEMPLATE.sub(one, value)


# What a task's layer copies from the files the agent's OpenCode won't load by itself
PROVIDER_KEYS = ("provider", "enabled_providers", "disabled_providers")


def agent_providers() -> dict:
    """The providers wb read from files the agent's OpenCode won't load: $WB_OPENCODE_CONFIG, or
    the user's OPENCODE_CONFIG, which the task's layer takes the place of. OpenCode loads its
    global files itself. Left out, a model wb found ready was "Model unavailable" to the agent
    before any request reached its server (the 2026-11-19 audit). Templates stay templates, a
    relative {file:} made absolute: the layer lives elsewhere."""
    own = {p.resolve() for p in (*_system_files(), *_global_files()) if p.exists()}
    out: dict = {}
    for path, layer in _layers():
        if path.resolve() in own:
            continue
        picked = {k: layer[k] for k in PROVIDER_KEYS if k in layer}
        if "providers" in layer and "provider" not in layer:
            picked["provider"] = layer["providers"]
        out = _merge(out, _absolute_files(picked, path.parent))
    return out


def config_target(change: str) -> Path:
    """The file own_config would write, or RuntimeError saying why it won't (and ``change``, to
    make by hand). Asked first by a change that does something else before writing: wb optimize
    made its Ollama variant, then the config refused it (#266, the audit of 1.2)."""
    system = {p.resolve() for p in _system_files() if p.exists()}  # an admin's: never written (#370)
    files = [f for f in opencode_config_files() if f.resolve() not in system]
    if len(files) > 1:
        raise RuntimeError(f"OpenCode layers {' and '.join(map(str, files))}; wb won't rewrite "
                           f"either: make this change by hand: {change}")
    path = files[0] if files else opencode_config_path()  # the user's OPENCODE_CONFIG, if it's that
    try:
        json.loads(path.read_text() if path.exists() else "{}")
    except ValueError:
        raise RuntimeError(f"{path} has comments, which rewriting it would lose; make this change "
                           f"by hand: {change}") from None
    return store.writable_target(path, change)


CONTENT = "OPENCODE_CONFIG_CONTENT"  # how a refusal names the session's config layer


@contextmanager
def own_config(change: str):
    """(the file, its own parsed contents) of OpenCode's config file wb writes (config_target),
    to change in place: written back when the body ends, under its lock, through a symlink, its
    mode kept (store.edit_config); nothing written when the body raises. Refused for a file with
    comments, which a rewrite would lose, and while two files are layered (config_target). A
    refusal says only ``change`` (what to do by hand): it once printed the whole config, a
    provider's apiKey included, into Claude's context (the 2026-11-16 audit). Only the file's own
    contents: the merged view (read_config) is for lookups. Written back, it copied an admin's
    system file and a session's OPENCODE_CONFIG_CONTENT, an apiKey with it, into the user's file,
    where the admin's later changes no longer took effect (#421, the audit of 1.8)."""
    target = config_target(change)
    def read():
        try:
            own = json.loads(target.read_text()) if target.exists() else {}
        except ValueError:
            raise RuntimeError(f"{target} has comments, which rewriting it would lose; make this "
                               f"change by hand: {change}") from None
        if not isinstance(own, dict):
            raise RuntimeError(f"{target} doesn't hold an object; make this change by hand: {change}")
        return own
    with store.edit_config(target, read, change) as own:
        yield target, own


def _sets(layer: dict, keys: tuple) -> bool:
    """Whether ``layer`` sets the key path ``keys`` (a provider's under either shape's key)."""
    for head in ("provider", "providers") if keys[0] == "provider" else keys[:1]:
        node = layer.get(head)
        for k in keys[1:]:
            node = node.get(k) if isinstance(node, dict) else None
        if node is not None:
            return True
    return False


def set_elsewhere(target: Path, *keys, later: bool = False) -> list[str]:
    """The layers OpenCode loads besides ``target`` (own_config's file) that set the key path
    ``keys``, by name (a file, or OPENCODE_CONFIG_CONTENT): every one, or with ``later`` only
    those loaded after it, whose value wins over the target's. What wb can't write: a model
    removed from the user's file stayed in effect from the system file, and a default written
    there lost to the session's (#421)."""
    layers = [(str(p), p.resolve(), layer) for p, layer in _layers()]
    if content := users_config_content():
        layers.append((CONTENT, None, content))
    if later:
        at = next((i for i, (_, p, _) in enumerate(layers) if p == target), None)
        layers = layers[at + 1:] if at is not None else [x for x in layers if x[1] is None]
    return [name for name, p, layer in layers if p != target and _sets(layer, keys)]


def _not_ours(what: str, where: list[str], change: str) -> RuntimeError:
    return RuntimeError(f"{what} is set in {' and '.join(where)}, which wb doesn't write: make "
                        f"this change there by hand: {change}")


def set_default(model_id: str):
    """Make ``model_id`` OpenCode's default model ("model" in the user's file); the default it
    replaces. Refused while a layer loaded later (OPENCODE_CONFIG_CONTENT) sets one, which wins."""
    change = f'set "model": "{model_id}"'
    old = read_config().get("model")
    with own_config(change) as (target, own):
        if where := set_elsewhere(target, "model", later=True):
            raise _not_ours('"model"', where, change)
        own["model"] = model_id
    return old


def overrides_path() -> Path:
    return Path(os.environ.get("WB_MODELS") or Path.home() / ".config" / "workbench" / "models.json")


def _overrides() -> dict:
    path = overrides_path()
    try:
        data = json.loads(path.read_text()) if path.exists() else {}
    except ValueError as e:  # one bad edit must not break every command
        raise RuntimeError(f"{path} doesn't parse ({e}); fix it by hand") from None
    return data if isinstance(data, dict) else {}


@functools.lru_cache(maxsize=1)
def _own_addresses() -> frozenset:
    """This machine's own IP addresses (LAN, Tailscale, ...): a provider at one of them is local."""
    found = set()
    try:
        found |= {a[4][0] for a in socket.getaddrinfo(socket.gethostname(), None)}
    except OSError:
        pass
    # Every interface's: `ip` on Linux, else `ifconfig` (macOS has no `ip`, so a provider at the
    # Mac's own LAN address counted as remote and its model was left out: the 2026-11-19 audit)
    for argv in (["ip", "-o", "addr"], ["ifconfig", "-a"]):
        try:
            out = subprocess.run(argv, capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.TimeoutExpired):
            continue
        # 10.0.0.5/24 (ip), fe80::1%en0 (ifconfig's scope), addr:10.0.0.5 (old net-tools)
        found |= {w.split("/")[0].split("%")[0].removeprefix("addr:")
                  for line in out.splitlines() for i, w in enumerate(line.split())
                  if i and line.split()[i - 1] in ("inet", "inet6")}
        break
    return frozenset(found)


def _this_host(host: str | None) -> bool:
    """Whether a baseURL's host is this machine: any loopback address, or this host's own name."""
    if not host:
        return False
    if host.lower() in LOCAL_HOSTS:
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_loopback or str(ip) in _own_addresses()
    except ValueError:
        me = socket.gethostname().lower()
        return host.lower() in (me, me.split(".")[0], f"{me.split('.')[0]}.local")


_ENGINES: dict[str, str] = {}  # root -> engine, once its server has answered
# Roots whose probe got no answer, and when: a provider whose server is down (a wb remote box
# asleep) cost 1.5 s on every discover(), which runs many times per command, so wb models took
# 12 s instead of 0.1 s (#337, the audit of 1.4). Asked again after DOWN_SECS, so a runner or the
# chat server, which live for hours, learn the engine once the server is back.
_DOWN: dict[str, float] = {}
DOWN_SECS = 60

# A server on this machine is asked directly, never through a proxy: urllib honours http_proxy and
# exempts loopback only when no_proxy says so, so behind a corporate proxy every local model read
# as "not answering" (the 2026-11-16 audit). A provider on another host keeps the environment's.
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# Each provider's resolved apiKey by its server's root, so every probe sends what the agent will:
# an Ollama behind a key served the model, but the native /api probes sent none and it read as
# unavailable (#256, the audit of 1.2). discover() fills it.
_KEYS: dict[str, str] = {}


def _open(target, timeout: float):
    url = target.full_url if isinstance(target, urllib.request.Request) else target
    key = next((k for root, k in _KEYS.items() if url == root or url.startswith(root + "/")), "")
    if key and not isinstance(target, urllib.request.Request):
        target = urllib.request.Request(url)
    if key and not target.has_header("Authorization"):
        target.add_header("Authorization", f"Bearer {key}")
    opener = _DIRECT.open if _this_host(urlparse(url).hostname) else urllib.request.urlopen
    return opener(target, timeout=timeout)


def _engine(url: str, over: dict, pid: str = "") -> str:
    """"ollama" when the server answers Ollama's own /api/version, else "server". While it is
    down, Ollama's default port or a provider id naming Ollama decides (an Ollama moved to another
    port read as a server needing a start command: the 2026-11-16 audit); the answer is kept once
    the server has given one."""
    if over.get("engine") in ("ollama", "server"):
        return over["engine"]
    root = url.rstrip("/").removesuffix("/v1")
    if root in _ENGINES:
        return _ENGINES[root]
    guess = "ollama" if urlparse(url).port == OLLAMA_PORT or "ollama" in pid.lower() else "server"
    if _fake() or time.monotonic() - _DOWN.get(root, -DOWN_SECS) < DOWN_SECS:
        return guess
    try:
        with _open(f"{root}/api/version", timeout=1.5) as r:
            body = json.loads(r.read() or b"{}")
        _ENGINES[root] = "ollama" if isinstance(body, dict) and "version" in body else "server"
    except urllib.error.HTTPError:  # it answered, without Ollama's API
        _ENGINES[root] = "server"
    except Exception:  # down, or not speaking JSON: the port is the best guess
        _DOWN[root] = time.monotonic()
        return guess
    return _ENGINES[root]


def engine_known(url: str, over: dict) -> bool:
    """Whether the engine was said (models.json) or probed, not guessed while the server is down."""
    return _fake() or over.get("engine") in ("ollama", "server") or \
        url.rstrip("/").removesuffix("/v1") in _ENGINES


_TEMPLATE = re.compile(r"\{(env|file):([^}]*)\}")


def _agent_sees(name: str) -> bool:
    """Whether the agent's OpenCode gets this variable (runner.agent_env drops credentials)."""
    from . import context, runner  # runner imports this module
    through = set((store.read_json(context.commands_path(), {}) or {}).get("env") or [])
    return name in through or not runner.SECRET_ENV.search(name)


def _expand(value: str, agent: bool = True, base: Path | None = None) -> str:
    """OpenCode's `{env:NAME}` and `{file:path}` in a config string, resolved as OpenCode does:
    an unset name or an unreadable file is empty; a relative path is the directory of the config
    file it is in (``base``). With ``agent``, a name the agent's environment drops is empty too:
    its OpenCode won't see it. Taken raw, such a baseURL had no host and its provider vanished
    (the 2026-11-16 audit)."""
    def one(mt):
        kind, arg = mt.group(1), mt.group(2).strip()
        if kind == "env":
            return os.environ.get(arg, "") if not agent or _agent_sees(arg) else ""
        path = Path(os.path.expanduser(arg))
        try:
            return (path if path.is_absolute() else (base or opencode_config_path().parent) / path
                    ).read_text().strip()
        except OSError:
            return ""
    return _TEMPLATE.sub(one, value or "")


def _scan(cfg: dict, extra: dict, dirs: dict | None = None):
    """(provider id, its config, its baseURL resolved, why it is skipped or "", remote) for each
    provider with a baseURL, as OpenCode loads them: disabled_providers and enabled_providers
    followed (the router used to pick a provider OpenCode wouldn't load: the 2026-11-16 audit).
    A provider with no baseURL is one of OpenCode's own cloud providers: not listed."""
    enabled, disabled = cfg.get("enabled_providers"), set(cfg.get("disabled_providers") or [])
    dirs = _provider_dirs() if dirs is None else dirs
    for pid, prov in _providers_of(cfg).items():
        opts = prov.get("options") or prov.get("settings") or {}
        raw = opts.get("baseURL") or ""
        if not raw:
            continue
        url = _expand(raw, base=dirs.get(pid))
        host = urlparse(url).hostname
        here = _this_host(host)
        if pid in disabled:
            why = "disabled in OpenCode's config (disabled_providers)"
        elif enabled and pid not in enabled:
            why = "not in OpenCode's enabled_providers"
        elif not host:
            names = [n.strip() for k, n in _TEMPLATE.findall(raw) if k == "env"]
            why = f"its baseURL {raw} resolves to {url or 'nothing'}, which has no host" + (
                f": is {', '.join(names)} set (and passed to agents: commands.json env)?" if names else "")
        elif not (here or (extra.get(pid) or {}).get("local")):
            why = (f"{host} isn't this machine; if it's your own model server (WSL2's Windows host, "
                   f"a box on the LAN), add {{\"{pid}\": {{\"local\": true}}}} to {overrides_path()}")
        else:
            why = ""
        yield pid, prov, url, why, not here


def _local_providers(cfg: dict, extra: dict, dirs: dict | None = None):
    """(provider id, its config, its baseURL, engine, remote) for each provider on this machine,
    or opted in with models.json "local": true."""
    for pid, prov, url, why, remote in _scan(cfg, extra, dirs):
        if not why:
            # the key before the probe: an Ollama that wants one on /api/version answered the
            # keyless probe with an error, was cached as a server, and its Ollama management
            # (load, unload, pull, the window check) was skipped (#337, the audit of 1.4)
            raw = str((prov.get("options") or prov.get("settings") or {}).get("apiKey") or "")
            key = _expand(raw, base=(dirs or {}).get(pid)) if raw else auth_key(pid)  # #336
            if key:
                _KEYS[url.rstrip("/").removesuffix("/v1")] = key
            yield pid, prov, url, _engine(url, extra.get(pid) or {}, pid), remote


def skipped_providers() -> list[tuple[str, str]]:
    """(provider id, why) for each provider with a baseURL that discovery leaves out."""
    return [(pid, why) for pid, _, _, why, _ in _scan(read_config(), _overrides()) if why]


def why_none() -> str:
    """Why discovery found no model: the providers it passed over, or how to make one."""
    skipped = skipped_providers()
    if skipped:
        return "; ".join(f"provider {pid}: {why}" for pid, why in skipped)
    return (f"OpenCode has no local provider in {opencode_config_path()}: wb model add "
            "<provider>/<model> --url <baseURL> adds one for any OpenAI-compatible server on this "
            "machine (llama.cpp, LM Studio, vLLM, ...); with Ollama running, wb model add "
            "ollama/<model> adds one by itself")


def url_problem(url: str, engine: str, npm: str | None = None) -> str:
    """Why a provider's baseURL would fail every request, or "". Ollama's OpenAI API lives under
    /v1: without it OpenCode posts to /chat/completions and gets 404 after the model is loaded,
    while the /api probes said ready (the 2026-11-16 audit). A native Ollama SDK provider differs."""
    if engine == "ollama" and (npm or "@ai-sdk/openai-compatible") == "@ai-sdk/openai-compatible" \
            and not urlparse(url).path.rstrip("/").endswith("/v1"):
        return f"its baseURL {url} needs /v1 (Ollama's OpenAI API is at {url.rstrip('/')}/v1)"
    return ""


def _cloud_name(name: str) -> bool:
    return name.endswith((":cloud", "-cloud"))


def _cloud_row(row: dict) -> bool:
    """An /api/tags row Ollama forwards to its cloud: a stub of a few hundred bytes."""
    return bool(row.get("remote_host") or row.get("remote_model")) or _cloud_name(row.get("name") or "")


def cloud_allowed(model_id: str) -> bool:
    return (_overrides().get(model_id) or {}).get("local") is True


def auth_key(pid: str) -> str:
    """The key `opencode auth login` keeps for a provider, in OpenCode's auth.json: OpenCode's
    docs set a custom provider's key there, with no options.apiKey, and the probes sent none, so
    a model OpenCode would run read "it wants an API key" (#336, the audit of 1.4). The lane keeps
    auth.json readable, so the agent's OpenCode sends the same key. Never printed."""
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
    try:
        row = json.loads((Path(base) / "opencode" / "auth.json").read_text()).get(pid)
    except (OSError, ValueError, AttributeError):
        return ""
    return str(row.get("key") or "") if isinstance(row, dict) and row.get("type") == "api" else ""


def discover() -> dict[str, Model]:
    """Every local model OpenCode knows, by OpenCode id, in config order."""
    extra, dirs = _overrides(), _provider_dirs()
    found: dict[str, Model] = {}
    for pid, prov, url, engine, remote in _local_providers(read_config(), extra, dirs):
        opts = prov.get("options") or prov.get("settings") or {}
        problem = url_problem(url, engine, prov.get("npm"))
        # the key the agent's OpenCode will send: probed with wb's own, a key the agent never got
        # read as running and every request of the task was refused (the 0.90 review)
        raw_key = str(opts.get("apiKey") or "")
        key = _expand(raw_key, base=dirs.get(pid)) or (auth_key(pid) if not raw_key else "")
        hidden = [n.strip() for k, n in _TEMPLATE.findall(raw_key)
                  if k == "env" and n.strip() in os.environ and not _agent_sees(n.strip())]
        if hidden and not problem:
            from . import context
            problem = (f"its apiKey reads {{env:{hidden[0]}}}, which agents don't get: add "
                       f"{hidden[0]} to commands.json env ({context.commands_path()})")
        for name, spec in (prov.get("models") or {}).items():
            mid = f"{pid}/{name}"
            over = {**extra.get(pid, {}), **extra.get(mid, {})}
            if over.get("skip"):
                continue
            # an Ollama cloud model was routed trivial work as "the smallest" (the 2026-11-16 audit)
            cloud = engine == "ollama" and _cloud_name(name)
            cloud_ok = cloud and (extra.get(mid) or {}).get("local") is True
            found[mid] = Model(id=mid, provider=pid, name=str(spec.get("id") or name),
                               base_url=url, engine=engine,
                               label=over.get("label") or spec.get("name") or mid,
                               start=over.get("start"), stop=over.get("stop"), extra=over,
                               config=spec, remote=remote or cloud_ok, cloud=cloud,
                               cloud_ok=cloud_ok, problem=problem, api_key=key)
    return found


def resolve(wanted: str) -> Model:
    """An OpenCode id, a model's name at its server, or a fragment naming exactly one model."""
    models = discover()
    if wanted in models:
        return models[wanted]
    exact = [m for m in models.values() if wanted in (m.name, m.id.split("/", 1)[1], m.provider)]
    hits = exact or [m for m in models.values() if wanted.lower() in m.id.lower()]
    if len(hits) == 1:
        return hits[0]
    known = ", ".join(models) or f"none ({why_none()})"
    if hits:
        raise KeyError(f"{wanted!r} matches {', '.join(m.id for m in hits)}; be more specific")
    raise KeyError(f"unknown model {wanted!r}; known: {known}")


def _get(url: str, timeout: float = 2.5):
    try:
        with _open(url, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except Exception:
        return None


def _post(url: str, body: dict, timeout: float = 600):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with _open(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:  # say what the server said: "HTTP Error 500" alone
        raw = e.read().decode(errors="replace")  # hid an out-of-memory load (the 2026-11-16 audit)
        try:
            msg = (json.loads(raw) or {}).get("error") or raw
        except (ValueError, AttributeError):
            msg = raw
        raise RuntimeError(f"{url.split('/api/')[0]} answered HTTP {e.code}: "
                           f"{str(msg).strip()[:300] or e.reason}") from None


def _fake() -> bool:
    return bool(os.environ.get("WB_FAKE_MODELS"))  # tests drive a fake opencode, no servers


def _ollama_names(m: Model, endpoint: str) -> set[str]:
    body = _get(f"{m.root}/api/{endpoint}") or {}
    return {x.get("name") for x in body.get("models", [])}


def _matches(name: str, names: set[str]) -> bool:
    return name in names or f"{name}:latest" in names


def _key(m: Model) -> tuple:
    """One model on one server, whatever the provider or host spelling that reaches it."""
    return m.server, m.name.removesuffix(":latest")


def _roots(ms) -> dict:
    """One root per server among ``ms``: each server is asked (and swept) once."""
    out = {}
    for o in ms:
        out.setdefault(o.server, o.root)
    return out


def _contended(m: Model) -> bool:
    """Several models on one server port, each with its own start command (one llama-server per
    model): a server listing one model can't say which of them it is."""
    starts = {o.start for o in discover().values()
              if o.engine == "server" and o.server == m.server and o.start}
    return len(starts) > 1


def _base(s: str) -> str:
    return re.sub(r"\.gguf$", "", s.rsplit("/", 1)[-1], flags=re.I).lower()


def _serves(served: str, name: str) -> bool:
    """Whether a one-model server's id is this model: llama-server lists the file's path."""
    return served == name or _base(served) == _base(name)


def _owners(m: Model, served: str) -> list[Model]:
    """The models on m's contended port that a one-model server's id may be: by name, or by the
    served file in its start command (`-m <path>`). Names that differ from the file left every
    model on the port unavailable, the one being served too (the 0.90 review)."""
    def started(o: Model) -> bool:
        try:
            words = shlex.split(o.start or "")
        except ValueError:
            words = (o.start or "").split()
        return any(w.rsplit("=", 1)[-1] == served or _base(w.rsplit("=", 1)[-1]) == _base(served)
                   for w in words)
    return [o for o in discover().values() if o.engine == "server" and o.server == m.server
            and (_serves(served, o.name) or started(o))]


def _no(why: str) -> dict:
    return {"available": False, "resident": False, "why": why}


def _tag_row(name: str, tags: dict) -> dict | None:
    return next((x for x in tags.get("models") or [] if _matches(name, {x.get("name")})), None)


CLOUD_WHY = ("an Ollama cloud model: it runs on ollama.com, off this machine (models.json "
             "\"local\": true on {id} allows it)")


# The smallest window a task fits in: OpenCode's system prompt and tools, PROTOCOL and the
# handbook take ~5K tokens before the spec, and below this OpenCode's compaction comes before the
# earliest handover point, or before the first request (at 4K its threshold is negative): the
# 2026-11-16 audit. A model served less can't run; an entry with no limit.context isn't checked.
MIN_WINDOW = 16384


def window_too_small(cfg: dict) -> int | None:
    """The entry's window when it is under MIN_WINDOW, else None."""
    ctx = (cfg.get("limit") or {}).get("context")
    return ctx if isinstance(ctx, int) and 0 < ctx < MIN_WINDOW else None


def small_window_why(ctx: int) -> str:
    return (f"a {ctx // 1024}K window; tasks need {MIN_WINDOW // 1024}K+ (raise "
            "OLLAMA_CONTEXT_LENGTH or the server's context, then wb model add … --context N)")


def _catalog(m: Model, catalog: dict | None):
    """A server's GET {base_url}/models: the body (a dict, else None) or the HTTPError's code.
    ``catalog``: one probe's answers by (base_url, api_key), shared by every entry on that server,
    so its aliases ask it once: wb top sent 8 requests for 8 aliases each probe, one after
    another, 2 s each on a server that stalled (#561)."""
    key = (m.base_url.rstrip('/'), m.api_key or "")
    if catalog is not None and key in catalog:
        return catalog[key]
    auth = {"Authorization": f"Bearer {m.api_key}"} if m.api_key else {}
    try:
        with _open(urllib.request.Request(f"{key[0]}/models", headers=auth), timeout=2) as r:
            got = json.loads(r.read() or b"{}")
        got = got if isinstance(got, dict) else None  # an int would read as an HTTP code
    except urllib.error.HTTPError as e:
        e.close()
        got = e.code
    except Exception:
        got = None
    if catalog is not None:
        catalog[key] = got
    return got


def status(m: Model, down=frozenset(), catalog: dict | None = None) -> dict:
    """``{available, resident, why}``: can the runner get this model onto the GPU, is it there.
    What the config alone rules out comes first, so the fake models meet it too. ``down``: Ollama
    roots the caller already found not answering, not asked again. ``catalog``: a dict the caller
    keeps for one pass, so each server other than Ollama is asked for its /models once (_catalog)."""
    if small := window_too_small(m.config):
        return _no(small_window_why(small))
    if m.config.get("tool_call") is False:  # the router used to hand it tasks (the 2026-11-16 audit)
        return _no("OpenCode's config says it can't call tools (tool_call: false): agent work needs them")
    if m.problem:
        return _no(m.problem)
    if m.cloud and not m.cloud_ok:
        return _no(CLOUD_WHY.format(id=m.id))
    if _fake():
        return {"available": True, "resident": True, "why": "fake"}
    if m.engine == "ollama":
        tags = None if m.root in down else _get(f"{m.root}/api/tags")
        if tags is None:
            return _no("ollama is not answering")
        row = _tag_row(m.name, tags)
        if row is None:
            return _no(f"{m.name} is not pulled")
        if _cloud_row(row) and not cloud_allowed(m.id):  # a cloud model by another name
            return _no(CLOUD_WHY.format(id=m.id))
        resident = _matches(m.name, _ollama_names(m, "ps"))
        return {"available": True, "resident": resident, "why": "loaded" if resident else "pulled"}
    body = _catalog(m, catalog)
    if isinstance(body, int):  # an HTTP error's code
        code, body = body, None
        # a server behind a key answered, with the key the agent will send: refused, every
        # request of the task would be. Not "running": a wrong key passed, and the router could
        # pick that model over a working one (the 2026-11-19 audit). Not "not running" either,
        # which started a second copy and waited 20 minutes for it (the 2026-11-16 audit).
        if code in (401, 403):
            # where else a key may be kept: opencode auth login's auth.json (#336, the audit of 1.4)
            return _no(f"the server refuses its API key (HTTP {code}): set the provider's "
                       f"options.apiKey, or `opencode auth login` for {m.provider}, to the key it "
                       "wants" if m.api_key else
                       f"the server answers, but it wants an API key (HTTP {code}): set the "
                       f"provider's options.apiKey, or `opencode auth login` for {m.provider}")
    if isinstance(body, dict):
        # one listed model: llama-server answers to any name; several: it routes by name
        rows = [x for x in (body.get("data") or []) if isinstance(x, dict)]
        served = {x.get("id") for x in rows}
        if isinstance(body.get("data"), list) and not served:  # it read as running (#256)
            return _no("the server answers but lists no model: load it there")
        if len(served) > 1 and m.name not in served:
            return _no(f"the server lists {len(served)} models, not {m.name}")
        sid = str(next(iter(served))) if len(served) == 1 else ""
        # vLLM, SGLang and TGI refuse any other name with a 404: the round died at its first
        # request as "opencode exited 1", and a vLLM name is a long HF path, easy to miss (#256,
        # the audit of 1.2). llama-server says it is one (owned_by llamacpp, a `meta` field).
        llamacpp = any(x.get("owned_by") == "llamacpp" or "meta" in x for x in rows)
        # the file's basename is llama-server's spelling only: a strict server takes its exact
        # id, so studio/coder read as serving personal/coder (#339, the audit of 1.4)
        same = sid == m.name or (llamacpp and _serves(sid, m.name))
        if sid and not same and not llamacpp and not (m.start and _contended(m)):
            return _no(f"the server serves {sid}, not {m.name}: set the entry's id to {sid}")
        if sid and m.start and _contended(m) and not same:
            # the task would run on the other model, recorded under this one (the 2026-11-16 audit)
            owners = _owners(m, sid)
            if [o.id for o in owners] == [m.id]:
                return {"available": True, "resident": True, "why": "running"}
            if len(owners) == 1 and owners[0].stop:  # free_gpu stops it (the 0.90 review)
                return {"available": True, "resident": False,
                        "why": f"the port serves {owners[0].id}; wb stops it and starts {m.id}"}
            why = (f"it is {owners[0].id}, which has no stop command, so wb can't switch them"
                   if len(owners) == 1 else "so wb can't tell which model it is")
            return _no(f"{m.root} is serving {sid}, not {m.name}, and other models with their own "
                       f"start commands share that port; {why}: give each model a stop command and "
                       "its file's name (or --alias it as its name), or its own port, or use one "
                       "server that swaps models (llama-swap)")
        return {"available": True, "resident": True, "why": "running"}
    if m.start:
        return {"available": True, "resident": False, "why": "stopped (wb starts it)"}
    return _no(f"not running, and no start command in {overrides_path()}")


def remedy(m: Model, state: dict) -> str:
    """The next step for a model status() says can't run; "" when the reason already says it.
    wb start used to say only why, or offer --force for a model that could only fail to load
    (the 2026-11-16 audit)."""
    why = state.get("why", "")
    if why == "ollama is not answering":
        return f"start Ollama at {m.root} (ollama serve, or its service or app)"
    if why.endswith(" is not pulled"):
        return f"wb model add {m.id} --pull downloads it"
    if why.startswith(("the server lists", "the server answers but lists no model")):
        return f"load {m.name} in that server, or fix the model's name in OpenCode's config"
    if why.startswith("not running, and no start command"):
        return f"start its server, or give {m.id} a \"start\" command there"
    return ""


def may_come_up(state: dict) -> bool:
    """Whether a model that can't run now may by the time a queued task gets the GPU: its server
    is only down. Not pulled, or ruled out by the config, it would only fail to load."""
    return state.get("why", "") == "ollama is not answering" or \
        state.get("why", "").startswith("not running, and no start command")


def weights(m: Model) -> int:
    """Bytes of weights behind the id, the router's measure of a model's size; 0 when unknown.
    models.json's `weights_gb` says it for a server that can't (or a test). A cloud model's
    figure is a stub's, so it says nothing."""
    if m.extra.get("weights_gb"):
        return int(float(m.extra["weights_gb"]) * 1e9)
    if m.engine == "ollama" and not m.cloud and not _fake():
        row = _tag_row(m.name, _get(f"{m.root}/api/tags") or {})
        if row and not _cloud_row(row):
            return int(row.get("size") or 0)
    if m.engine != "ollama" and not _fake():
        return server_facts(m.root, m.name, m.api_key)["weights"] or 0
    return 0


# What a server other than Ollama says about a model: only Ollama's API was read, so on any other
# server the router ranked models without their sizes, checked no fit, and every window had to
# be typed in (2026-11-17). Each server answers its own call; the fields as each project's docs
# and source give them (checked 2026-11-17):
#   LM Studio 0.4+  GET /api/v1/models: models[].key, size_bytes,
#                   loaded_instances[].config.context_length, capabilities.trained_for_tool_use
#   llama-server    GET /v1/models: data[].meta.size (bytes; null while loading);
#                   GET /props?model=<id>&autoload=false: default_generation_settings.n_ctx, the
#                   window of one slot (-c split across an explicit --parallel unless -kvu); autoload=false so a
#                   router-mode server isn't made to load a model to answer
#   vLLM, SGLang    GET /v1/models: data[].max_model_len
FACTS_TTL = 30          # seconds: weights() runs on every routing; a server's answers don't move faster
_FACTS: dict = {}


def _ask(url: str, key: str = "", timeout: float = 2):
    """A server's JSON answer to a GET, or None (down, 404, not JSON)."""
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"} if key else {})
    try:
        with _open(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        e.close()
        return None
    except Exception:
        return None


def server_facts(root: str, name: str, key: str = "") -> dict:
    """{"context", "weights", "tools"} for ``name`` on the server at ``root``: the window it
    serves, its weights' bytes, whether it was trained for tool calls; each None when the server
    doesn't say. Cached for FACTS_TTL seconds, misses too (a down server costs one probe)."""
    hit = _FACTS.get((root, name))
    if hit and time.monotonic() - hit[0] < FACTS_TTL:
        return hit[1]
    facts = {"context": None, "weights": None, "tools": None}
    lms = _ask(f"{root}/api/v1/models", key)
    rows = lms.get("models") if isinstance(lms, dict) else None
    if isinstance(rows, list):  # LM Studio
        for x in rows:
            if not isinstance(x, dict):
                continue
            loaded = [i for i in x.get("loaded_instances") or [] if isinstance(i, dict)]
            if not (_serves(str(x.get("key") or ""), name) or any(i.get("id") == name for i in loaded)):
                continue
            facts["weights"] = _pos(x.get("size_bytes"))
            facts["tools"] = (x.get("capabilities") or {}).get("trained_for_tool_use")
            facts["context"] = next((_pos((i.get("config") or {}).get("context_length"))
                                     for i in loaded), None)  # a JIT load's window is unknown
            break
    else:
        body = _ask(f"{root}/v1/models", key)
        data = body.get("data") if isinstance(body, dict) else None
        cards = [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []
        card = next((x for x in cards if _serves(str(x.get("id") or ""), name)),
                    cards[0] if len(cards) == 1 else None)  # one listed: a one-model server
        if card:
            facts["context"] = _pos(card.get("max_model_len"))  # vLLM, SGLang
            meta = card.get("meta") if isinstance(card.get("meta"), dict) else {}
            facts["weights"] = _pos(meta.get("size"))
            if card.get("owned_by") == "llamacpp" or meta:
                q = urllib.parse.quote(str(card.get("id") or name), safe="")
                props = _ask(f"{root}/props?model={q}&autoload=false", key)
                gen = (props or {}).get("default_generation_settings") if isinstance(props, dict) else None
                facts["context"] = _pos((gen or {}).get("n_ctx")) or facts["context"]
    _FACTS[(root, name)] = (time.monotonic(), facts)
    return facts


def _pos(v) -> int | None:
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else None


def resident() -> list[Model]:
    """The discovered models holding the GPU now."""
    return [m for m in discover().values() if status(m)["resident"]] if not _fake() else []


def releasable(m: Model) -> bool:
    """Whether free_gpu can let go of the model: a local Ollama one, or a local server with a
    stop command (a server without one, or another host's model, stays up)."""
    return not m.remote and (m.engine == "ollama" or bool(m.stop))


def freed(m: Model, keep=()) -> bool:
    """Whether free_gpu(keep) lets go of the model: releasable, and not one it keeps (``keep``,
    keep_loaded, or a model on a kept one's server). media.settle waits only for these: a kept
    model is still resident when the unloads are done, so every `wb image` waited its whole 90 s
    and then said the memory was still being released (#467)."""
    if not releasable(m):
        return False
    held = {*keep, *keep_loaded()}
    if m.id in held or (m.engine == "ollama" and _kept_name(m.name, held)):
        return False
    return not (m.engine == "server" and any(
        o.engine == "server" and o.server == m.server and o.id in held and not o.remote
        for o in discover().values()))


def _server_idle(m: Model) -> bool:
    live = (_get(f"{m.root}/metrics", timeout=2) or {}).get("live") or {}
    return live.get("state", "idle") == "idle"  # servers without metrics count as idle


def _ollama_loaded(root: str) -> list[str]:
    return [x.get("name") for x in (_get(f"{root}/api/ps") or {}).get("models", [])]


def single_loads(st: dict) -> list[float]:
    """A task's loads one by one (status `loads`: each load that happened, #577), the samples of a
    model's typical load (wb top, wb optimize); a task from before `loads` gives its `load_secs`."""
    num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0  # noqa: E731
    if isinstance(st.get("loads"), list):
        return [v for v in st["loads"] if num(v)]
    return [st["load_secs"]] if num(st.get("load_secs")) else []


def make_resident(m: Model, say=print, keep=(), warn=None) -> float:
    """Free the GPU for ``m`` and load it, leaving the models in ``keep`` (ids) loaded. Returns
    the seconds it took (0 if already there). ``warn`` gets what Claude should hear about later
    (a server wb can't stop, a model partly on the CPU); it defaults to ``say``."""
    warn = warn or say
    if _fake():
        return 0.0
    st = status(m)
    if not st["available"]:  # a --force task for a model that couldn't load emptied the GPU
        # first and failed after (the 2026-11-16 audit): fail before freeing anything
        raise RuntimeError(f"{m.id} can't run: {st['why']}"
                           + (f": {fix}" if (fix := remedy(m, st)) else ""))
    if m.engine == "server" and (key := lmstudio_unloaded(m)):
        # LM Studio's just-in-time loading lists every downloaded model as served, so nothing
        # was loaded, and OpenCode's first request loaded it with LM Studio's default window
        # (often 4K): the prompt was refused and round 1 failed as "opencode exited 1" (#335,
        # the audit of 1.4). Load it here with the window OpenCode is told.
        t0 = time.time()
        if not m.remote and not _room_beside(m):
            free_gpu(keep={m.id, *keep}, say=say, warn=warn)
        _lmstudio_load(m, key, say)
        return time.time() - t0
    if st["resident"] and (m.remote or not _others_resident(m, keep) or _room_beside(m)):
        return 0.0
    t0 = time.time()
    if not m.remote and not _room_beside(m):
        free_gpu(keep={m.id, *keep}, say=say, warn=warn)
    if m.engine == "ollama":
        say(f"loading {m.name} into ollama")
        _post(f"{m.root}/api/generate", {"model": m.name, "prompt": "", "keep_alive": "30m"},
              timeout=START_TIMEOUT)
        _placement(m, warn)
    elif not status(m)["resident"]:
        if not m.start:
            raise RuntimeError(f"{m.id} is not running and has no start command")
        say(f"starting {m.id}")
        p = subprocess.Popen(m.start, shell=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        began, failed_at = time.monotonic(), None
        deadline = began + START_TIMEOUT
        while not (now := status(m))["resident"]:
            if not now["available"] and time.monotonic() >= began + START_GRACE:
                raise RuntimeError(f"{m.id}: {now['why']}")  # it answers as something else
            # a start command that fails at once used to be waited on for 20 min (the 2026-11-16
            # audit); exit 0 is a start that daemonizes (systemctl start, docker start, cmd &)
            if failed_at is None and p.poll():
                failed_at = time.monotonic()
            if failed_at and time.monotonic() >= failed_at + START_GRACE:
                raise RuntimeError(f"{m.id}'s start command exited {p.returncode} and nothing "
                                   f"answers; run it by hand to see why: {m.start}")
            if time.monotonic() > deadline:
                raise RuntimeError(f"{m.id} did not come up within {START_TIMEOUT // 60} min")
            # the next probe on a clock of its own: p.wait returns at once once the command has
            # exited, and a start that daemonizes left the loop probing back to back (3,095
            # requests in 0.8 s: the 2026-11-19 audit)
            due = min(time.monotonic() + (1 if failed_at else 5), deadline + 0.01)
            if p.poll() is None:
                try:
                    p.wait(timeout=max(due - time.monotonic(), 0))
                except subprocess.TimeoutExpired:
                    pass
            time.sleep(max(due - time.monotonic(), 0))
    return time.time() - t0


def lmstudio_unloaded(m: Model) -> str | None:
    """The LM Studio key of ``m`` when LM Studio lists it but has no instance of it loaded (it
    would load on the first request, with its default window: #335); else None."""
    if _fake():
        return None
    lms = _ask(f"{m.root}/api/v1/models", m.api_key)
    rows = lms.get("models") if isinstance(lms, dict) else None
    for x in rows if isinstance(rows, list) else []:
        if not isinstance(x, dict):
            continue
        loaded = [i for i in x.get("loaded_instances") or [] if isinstance(i, dict)]
        if any(i.get("id") == m.name for i in loaded):
            return None
        if _serves(str(x.get("key") or ""), m.name):
            return None if loaded else str(x["key"])
    return None


def _lmstudio_load(m: Model, key: str, say) -> None:
    """Load ``key`` into LM Studio with OpenCode's window (limit.context); a refusal names the
    command that does it by hand."""
    ctx = (m.config.get("limit") or {}).get("context")
    body = {"model": key, **({"context_length": ctx} if isinstance(ctx, int) and ctx > 0 else {})}
    say(f"loading {key} into LM Studio" + (f" with a {ctx // 1024}K window" if "context_length" in body else ""))
    try:
        _post(f"{m.root}/api/v1/models/load", body, timeout=START_TIMEOUT)
    except Exception as e:
        raise RuntimeError(f"{m.id} isn't loaded in LM Studio and loading it failed ({e}): run "
                           f"lms load {key}" + (f" --context-length {ctx}" if "context_length" in body else ""))
    _FACTS.pop((m.root, m.name), None)  # the window it now serves, for short_window


def _placement(m: Model, warn) -> None:
    """Say when Ollama put part of the model in RAM: it runs several times slower there, and
    nothing else tells Claude why its rounds crawl (the 2026-11-16 audit)."""
    for x in (_get(f"{m.root}/api/ps") or {}).get("models", []):
        if _matches(m.name, {x.get("name")}) and x.get("size"):
            cpu = round(100 * (1 - (x.get("size_vram") or 0) / x["size"]))
            if cpu > 0:
                warn(f"{m.name} is {cpu}% on the CPU: expect it to be slow (something else may "
                     "hold GPU memory, or the model and its context don't fit the GPU)")
            return


def _unload(m: Model) -> None:
    try:
        _post(f"{m.root}/api/generate", {"model": m.name, "keep_alive": 0}, timeout=120)
    except Exception:
        pass


def keep_loaded() -> set[str]:
    """models.json's top-level `keep_loaded`: model ids or Ollama names that wb never unloads or
    stops (an embedding model a project's tests need, the user's autocomplete or Open WebUI
    model): every round once unloaded every one of them (the 2026-11-19 audit)."""
    v = _overrides().get("keep_loaded") or []
    return {str(x) for x in v} if isinstance(v, list) else set()


def _kept_name(name: str, held: set[str]) -> bool:
    return any(_matches(name, {k.split("/", 1)[-1]}) or _matches(k.split("/", 1)[-1], {name})
               for k in held)


def free_gpu(keep=(), say=print, warn=None) -> None:
    """Stop every model server and unload every Ollama model holding the GPU, except the models
    in ``keep`` (ids): the task's own, and those of tasks running beside it; and those the user
    keeps loaded (keep_loaded)."""
    warn = warn or say
    if _fake():
        return
    held = keep_loaded()
    local = [o for o in discover().values() if not o.remote]  # another host's GPU isn't ours to free
    kept = [o for o in local if o.id in keep or o.id in held]
    # a server swaps its own models; one serving another model on the kept one's port is stopped
    done = {o.server for o in kept if o.engine == "server"
            and (not _contended(o) or status(o)["resident"])}
    for other in local:
        if other.engine != "server" or other.server in done or not status(other)["resident"]:
            continue
        done.add(other.server)
        if not other.stop:  # it may be idle with nothing loaded (LM Studio's server often is)
            warn(f"{other.id} is running and wb can't stop it (no stop command in "
                 f"{overrides_path().name}); it may hold GPU memory")
            continue
        deadline = time.time() + 600
        while not _server_idle(other) and time.time() < deadline:  # never cut off someone's reply
            time.sleep(5)
        say(f"stopping {other.id}")
        try:
            r = subprocess.run(other.stop, shell=True, capture_output=True, text=True, timeout=300)
        except subprocess.TimeoutExpired:
            warn(f"{other.id}'s stop command ran past 5 min; it may still hold GPU memory")
            continue
        if r.returncode:
            warn(f"{other.id}'s stop command failed (exit {r.returncode}): "
                 f"{(r.stderr or r.stdout).strip()[-200:]}")
    kept_ollama = [o for o in kept if o.engine == "ollama"]
    doomed = {root: [name for name in _ollama_loaded(root)  # every model Ollama has loaded,
                     if not any(o.server == key and _matches(o.name, {name}) for o in kept_ollama)
                     and not _kept_name(name, held)]  # ours or another tool's
              for key, root in _roots(o for o in local if o.engine == "ollama").items()}
    _wait_unused(doomed, warn)
    for root, names in doomed.items():
        for name in names:
            say(f"unloading {name} from ollama")
            try:
                _post(f"{root}/api/generate", {"model": name, "keep_alive": 0}, timeout=120)
            except Exception:
                pass


# A model free_gpu would unload may be answering someone this account's lease doesn't know:
# another account's tasks on a shared system Ollama (gpu.lock is per account), Open WebUI, an
# editor. Ollama moves a model's /api/ps expires_at each time a request finishes (now plus its
# keep-alive) and stops its clock while one runs, so a stamp that moves, or one already past,
# is a model in use. Each account's runner once unloaded the other's model before every round,
# and both paid a reload on every request (#378, the audit of 1.6).
INFLIGHT_WATCH = 10   # seconds free_gpu watches /api/ps for a request on what it would unload
INFLIGHT_WAIT = 600   # at most this long waiting for one in use, as for a server's reply


def _stamps(doomed: dict) -> dict:
    """(root, name) → /api/ps expires_at, for the models in ``doomed`` (root → names)."""
    return {(root, x["name"]): x["expires_at"] for root, names in doomed.items() if names
            for x in (_get(f"{root}/api/ps") or {}).get("models", [])
            if x.get("name") in names and x.get("expires_at")}


def _past(stamp: str) -> bool:
    """Whether an /api/ps expires_at has gone by (a request running past the keep-alive)."""
    try:  # Ollama writes nanoseconds; fromisoformat before 3.12 reads at most six digits
        when = datetime.fromisoformat(re.sub(r"(\.\d{6})\d+", r"\1", stamp.replace("Z", "+00:00")))
    except (ValueError, TypeError):
        return False
    return when.tzinfo is not None and when.timestamp() < time.time() - 2


def _in_use(doomed: dict, watch: float | None = None) -> set:
    """The (root, name) pairs of ``doomed`` with a request on them, watched for ``watch`` seconds
    (INFLIGHT_WATCH): expires_at moved, or is past. A server whose /api/ps gives no expires_at
    has nothing to watch: none."""
    first = _stamps(doomed)
    busy = {k for k, v in first.items() if _past(v)}
    deadline = time.monotonic() + (INFLIGHT_WATCH if watch is None else watch)
    while first and len(busy) < len(first) and time.monotonic() < deadline:
        time.sleep(min(1.0, max(deadline - time.monotonic(), 0)))
        busy |= {k for k, v in _stamps(doomed).items() if k in first and v != first[k]}
    return busy


def _wait_unused(doomed: dict, warn) -> None:
    """Wait, up to INFLIGHT_WAIT, while a model free_gpu would unload is answering requests that
    aren't this account's tasks' (they are kept): unloading it between someone's requests made
    them reload it on the next (#378). It is unloaded after, used or not: the GPU is the task's."""
    busy = _in_use(doomed)
    if not busy:
        return
    names = ", ".join(sorted({n for _, n in busy}))
    warn(f"{names} on ollama is answering requests that aren't this account's tasks (another "
         f"user's wb, Open WebUI, an editor): waiting up to {INFLIGHT_WAIT // 60} min for it to go "
         "idle before unloading it (models.json keep_loaded leaves a model loaded)")
    deadline = time.monotonic() + INFLIGHT_WAIT
    while busy and time.monotonic() < deadline:
        busy = _in_use({root: [n for r, n in busy if r == root] for root in doomed},
                       min(INFLIGHT_WATCH, max(deadline - time.monotonic(), 0)))
    if busy:
        names = ", ".join(sorted({n for _, n in busy}))
        warn(f"{names} on ollama was still in use after {INFLIGHT_WAIT // 60} min: unloading it")


def unheld_loaded(skip=()) -> list[str]:
    """The models this machine's Ollama servers have loaded that no running task of this account
    holds (gpu.json) and keep_loaded doesn't list: another user's, another tool's, or idle ones
    of wb's. wb doctor says how to keep them on a shared Ollama (#378). ``skip``: roots that
    don't answer."""
    if _fake():
        return []
    held, found = keep_loaded(), discover()
    local = [o for o in found.values() if not o.remote and o.engine == "ollama"]
    tasks = [o for o in (found.get(h.get("model") or "") for h in holders().values()) if o]
    return [name for key, root in _roots(local).items() if root not in skip
            for name in _ollama_loaded(root)
            if not any(o.server == key and _matches(o.name, {name}) for o in tasks)
            and not _kept_name(name, held)]


def _others_resident(m: Model, keep=()) -> bool:
    """Whether anything free_gpu would let go of holds the GPU beside ``m``."""
    held = keep_loaded()
    local = [o for o in discover().values() if not o.remote]
    ours = [m, *(o for o in local if o.id in keep or o.id in held)]
    if any(o.engine == "server" and o.stop and o.server not in {x.server for x in ours}
           and status(o)["resident"] for o in local):
        return True
    return any(not any(o.engine == "ollama" and o.server == key and _matches(o.name, {n}) for o in ours)
               and not _kept_name(n, held)
               for key, root in _roots(o for o in local if o.engine == "ollama").items()
               for n in _ollama_loaded(root))


def _room_beside(m: Model) -> bool:
    """Whether ``m`` fits on the GPU beside every model resident there now, so nothing need be
    unloaded: every round once emptied the GPU of the user's other models even when all of them
    fit (the 2026-11-19 audit). Only for Ollama, under a known budget (gpu_budget), with no other
    server resident (whose memory nothing here measures), ``m`` all in VRAM if it is loaded, and
    Ollama's own limit on loaded models (OLLAMA_MAX_LOADED_MODELS) not reached."""
    budget = gpu_budget()
    if m.engine != "ollama" or not budget:
        return False
    local = [o for o in discover().values() if not o.remote]
    if any(o.engine == "server" and status(o)["resident"] for o in local):
        return False
    used, here, mine = 0, [], False
    for key, root in _roots(o for o in local if o.engine == "ollama").items():
        for x in (_get(f"{root}/api/ps") or {}).get("models", []):
            if key == m.server and _matches(m.name, {x.get("name")}):
                if not x.get("size") or x.get("size_vram") != x["size"]:
                    return False  # partly on the CPU: room has to be made
                mine = True
                continue
            used += x.get("size") or 0
            if key == m.server:
                here.append(x)
    most = hardware.ollama_env().get("OLLAMA_MAX_LOADED_MODELS") or ""
    if not mine and len(here) + 1 > (int(most) if most.isdigit() and int(most) else OLLAMA_MAX_LOADED):
        return False
    n = need(m)
    return bool(n) and used + n <= SHARE_AT * budget


# ---- sharing the GPU: several tasks' models at once, when they fit ----
# Every runner holds gpu.lock for its whole run: shared while its model runs beside others',
# exclusive when it needs the GPU to itself (its model doesn't fit beside the running ones, or
# can't share). Whatever else needs the whole GPU takes gpu.lock exclusively too, which waits for
# the running tasks and keeps new ones out. gpu.json lists the holders; gpu.admit makes each
# admission one decision at a time, and gpu.load each load.
SHARE_AT = 0.9    # of the GPU's memory: the rest is the runtime's and the desktop's
KV_GUESS = 1.25   # a model not loaded yet: its weights plus about a quarter for its context
IGPU_UNDER = 2 ** 30  # Ollama's own cutoff: a device with less is an integrated GPU's carve-out
# Metal's default recommendedMaxWorkingSetSize: about two thirds of the RAM on Macs up to here,
# three quarters above (iogpu.wired_limit_mb overrides it). 32-36 GB reports vary: two thirds is
# the safe side, since too low only stops sharing and too high spills a model to the CPU.
UNIFIED_SMALL = 36 * 2 ** 30
# A GPU that shares the RAM with nothing set aside for the OS (a Jetson, a DGX Spark: nvidia-smi
# gives it no memory) keeps back at least this, else what the system used when measured (#524)
SYSTEM_FLOOR = 2 * 2 ** 30
OLLAMA_MAX_LOADED = 3  # Ollama's own default (3 per GPU) when OLLAMA_MAX_LOADED_MODELS is unset
QUEUE_POLL = 1.0  # a queued task looks again this often; gpu.json changing wakes it sooner


def unified_default(ram: int) -> int:
    """What macOS lets the GPU wire of ``ram`` bytes of unified memory, by default."""
    return ram * 2 // 3 if ram <= UNIFIED_SMALL else ram * 3 // 4


def system_reserve(g: dict) -> int:
    """Bytes of a RAM-sharing NVIDIA GPU's memory (hardware.gpus: a Jetson's, a DGX Spark's) the
    system keeps: what was in use when hardware read it, less the models this machine's Ollama
    has loaded (theirs is the GPU's), and never under SYSTEM_FLOOR. All of MemTotal passed a
    6.5 GB model as fitting an 8 GB Orin Nano whose desktop held 2 GB (#524). Another server's
    loaded model counts as the system's: too little room only stops sharing."""
    if g.get("ram_free") is None:
        return SYSTEM_FLOOR
    loaded = sum(int(x.get("size") or 0) for x in
                 (_get(f"{ollama_root_here()}/api/ps", timeout=1.0) or {}).get("models") or [])
    return max(SYSTEM_FLOOR, (g.get("vram") or 0) - g["ram_free"] - loaded)


def budget_devices() -> tuple[int | None, list[dict], str]:
    """Bytes of GPU memory models can use (None when the machine can't say: then no sharing),
    the devices that make it up, and where the number comes from (wb doctor and wb scout say it).
    models.json's `gpu_gb` sets it by hand, for hardware this can't read. One runtime serves a
    model, so it's the largest group of one driver's GPUs (several NVIDIA cards, which Ollama
    splits a model across, add up; an iGPU of another vendor beside them doesn't), leaving out
    devices under 1 GiB (an integrated GPU's carve-out). Summing every GPU counted a laptop's Radeon iGPU into its RTX card's 8 GB
    (the 2026-11-16 audit). Once a device is known discrete (hardware.gpus `discrete`), only
    those count: an AMD APU's carve-out never outranks or adds to a card (the 0.90 review: a
    16 GiB Radeon 780M carve-out beside an 8 GiB RTX became the budget, a 4 GiB one was added to
    an RX 7600S's 8). With none known, as before. Only the devices Ollama's environment lets it
    see count (hardware.visible). Apple Silicon: iogpu.wired_limit_mb, else macOS's default
    share; an APU: its carve-out and GTT, as hardware.gpus found; an NVIDIA GPU that shares the
    RAM (a DGX Spark, a Jetson): the RAM less what the system uses (system_reserve, #524)."""
    hand = _overrides().get("gpu_gb")
    if hand is not None:
        try:
            return int(float(hand) * 1e9), [], "models.json gpu_gb"
        except (TypeError, ValueError):
            raise RuntimeError(f"{overrides_path()}: gpu_gb is {hand!r}, not a number of GB; "
                               "fix it by hand") from None
    if _fake():
        gb = os.environ.get("WB_FAKE_GPU_GB")  # tests: a GPU of this size
        return (int(float(gb) * 1e9), [], "WB_FAKE_GPU_GB") if gb else (None, [], "")
    devs = hardware.gpus(windows=False)  # WSL's Windows names carry no VRAM: skip PowerShell
    notes = []
    if any((g.get("driver") or "").split(" ")[0] in ("nvidia", "amdgpu") for g in devs):
        devs, notes = hardware.visible(devs, hardware.ollama_env())
    if any(g.get("discrete") and g.get("vram") for g in devs):
        devs = [g for g in devs if g.get("discrete")]
    groups: dict[str, list] = {}
    for g in devs:
        if g.get("unified") and not g.get("memory"):  # Apple Silicon
            usable = g.get("wired_limit") or unified_default(g.get("vram") or 0)
            how = "iogpu.wired_limit_mb" if g.get("wired_limit") else "macOS's default share of the RAM"
        elif g.get("unified") and (g.get("driver") or "").startswith("nvidia"):
            keep = system_reserve(g)
            usable = max(0, (g.get("vram") or 0) - keep)
            how = f"{g['memory']}, less {hardware.size(keep)} the system uses"
        elif g.get("unified"):
            usable, how = g.get("vram") or 0, g["memory"]
        else:
            usable, how = g.get("vram") or 0, "VRAM"
            if usable < IGPU_UNDER:
                continue
        if usable:
            # an adapter named from Windows (WSL) is a group of its own: the registry can't tell
            # a laptop's Radeon iGPU from its Radeon card, so the largest counts alone (#522)
            key = (g.get("driver") or "?").split()[0]
            groups.setdefault(f"{key}:{g['name']}" if key == "windows" else key, []).append((usable, g, how))
    if not groups:
        return None, [], ""
    best = max(groups.values(), key=lambda devs: sum(n for n, _, _ in devs))
    why = "; ".join(f"{g['name']} ({how})" for _, g, how in best)
    return (sum(n for n, _, _ in best), [g for _, g, _ in best],
            why + "".join(f", as {n} leaves it" for n in notes))


def gpu_budget() -> int | None:
    """Bytes of GPU memory models can use, or None when the machine can't say (budget_devices)."""
    return budget_devices()[0]


def need(m: Model) -> int:
    """Bytes of GPU memory ``m`` takes: Ollama's own figure once loaded (weights and context),
    else models.json's `vram_gb`, else a guess from its weights; 0 when unknown."""
    if m.extra.get("vram_gb"):
        return int(float(m.extra["vram_gb"]) * 1e9)
    if m.engine == "ollama" and not _fake():
        for x in (_get(f"{m.root}/api/ps") or {}).get("models", []):
            if _matches(m.name, {x.get("name")}) and x.get("size"):
                return int(x["size"])
    return int(weights(m) * KV_GUESS)


def all_on_gpu(m: Model) -> bool:
    """Whether Ollama has the model loaded with every byte of it in VRAM (its own placement)."""
    if m.engine != "ollama" or _fake():
        return False
    return any(_matches(m.name, {x.get("name")}) and x.get("size") and x.get("size_vram") == x["size"]
               for x in (_get(f"{m.root}/api/ps") or {}).get("models", []))


def can_share(m: Model, running: list[Model], budget: int | None) -> bool:
    """Whether ``m`` can run beside the running tasks' models, every one on the GPU at once.
    Only Ollama shares (a one-model server wants the GPU to itself). The same model twice needs
    Ollama to serve requests in parallel (OLLAMA_NUM_PARALLEL), or the two would take turns and
    re-read their prompts on every request; different models must not outnumber what Ollama
    keeps loaded (OLLAMA_MAX_LOADED_MODELS), or it swaps them on every request. A model on
    another host never joins this machine's lease: its GPU isn't the one this machine measures,
    so it takes no lease at all (GpuLease lets it straight through)."""
    if not budget or not running or m.engine != "ollama" or m.remote or \
            any(o.engine != "ollama" or o.remote for o in running):
        return False
    env = hardware.ollama_env()
    if same := sum(_key(o) == _key(m) for o in running):
        # one slot each: a value of 2 once admitted any number, and a third task's requests
        # queued inside Ollama while its silence budget ran (the 2026-11-19 audit)
        par = env.get("OLLAMA_NUM_PARALLEL") or ""
        return par.isdigit() and same + 1 <= int(par)
    distinct = {_key(o): o for o in running}
    most = env.get("OLLAMA_MAX_LOADED_MODELS") or ""  # 0 or unset: Ollama's default
    if len(distinct) + 1 > (int(most) if most.isdigit() and int(most) else OLLAMA_MAX_LOADED):
        return False
    sizes = [need(o) for o in distinct.values()] + [need(m)]
    return all(sizes) and sum(sizes) <= SHARE_AT * budget


def admits(m: Model | None, held: dict) -> bool:
    """Whether ``m`` may take the GPU now beside the live holders ``held`` (gpu.json's entries):
    nobody waits ahead of it (first come, first served), every holder's model is known, and they
    all fit together (can_share). Nothing (an image, ``m`` None) joins anyone, and on a remote
    server, whose memory nothing here measures, nobody shares: tasks take turns (#116)."""
    if not held:
        return m is not None
    if m is None or m.remote or any(h.get("waiting") for h in held.values()):
        return False
    found = discover()
    running = [o for o in (found.get(h.get("model") or "") for h in held.values()) if o]
    return len(running) == len(held) and can_share(m, running, gpu_budget())


def holders(home: Path | None = None, pool: str = "gpu") -> dict:
    """gpu.json's live entries (a remote server's: ``pool`` gpu-<host>): pid → {model, task,
    start, waiting?, tests? (tests that use the GPU hold it: GpuLease.step_aside)}."""
    held = store.read_json((home or store.home()) / f"{pool}.json", {}) or {}
    return {p: h for p, h in held.items() if p.isdigit() and _alive(int(p), h.get("start"))}


def _fits_beside(m: Model, keep: set, before=None) -> bool:
    """After a shared load: Ollama kept every other holder's model and has all of ``m`` in VRAM.
    need() guesses a model's context before it loads; when the guess is short, Ollama evicts a
    model or splits one onto the CPU, and two tasks then reload their models on every request
    (the 2026-11-16 audit). Unknown (Ollama didn't answer) counts as fitting. ``before``: the
    names /api/ps listed before the load. A kept model missing from it was unloaded idle (its task
    in a long test run or waiting on a question past the keep-alive), not evicted: counted as one,
    the other task unloaded itself and waited for that task to finish (the 0.90 review)."""
    if _fake() or m.engine != "ollama":
        return True
    body = _get(f"{m.root}/api/ps")
    if body is None:
        return True
    ps = body.get("models") or []
    names = {x.get("name") for x in ps}
    found = discover()
    others = [o for o in (found.get(k) for k in keep) if o and o.server == m.server
              and (before is None or _matches(o.name, before))]
    whole = any(_matches(m.name, {x.get("name")}) and x.get("size") and x.get("size_vram") == x["size"]
                for x in ps)
    return whole and all(_matches(o.name, names) for o in others)


class GpuLease:
    """A task's hold on the GPU, beside other tasks' models when all fit, else alone. With no
    model (an image generation) it is always alone, and queues in gpu.json like a task, so
    tasks that come after it wait instead of joining the ones it waits for (flock would grant
    them a shared lock while its exclusive one waits); with ``wait`` False it refuses at once
    when anything holds the GPU. A remote model takes turns on its server, under that server's own
    files (gpu-<host>.*, ``pool``), in the same first-come order, one task at a time: nothing here
    knows its memory (#116). Before, tasks on one remote server all ran at once, and its Ollama
    swapped their models in and out on every request."""

    def __init__(self, m: Model | None, task: str, say=print, wait: bool = True,
                 on: Model | None = None):
        self.m, self.task, self.say, self.wait = m, task, say, wait
        self.home = store.home()
        self.pool = pool(m or on)  # ``on``: whose server a lease with no model takes (wb optimize)
        self.fh = None
        self.aside = False  # stepped out of gpu.json for the tests or a server's wait (#257)
        self.me = str(os.getpid())
        # its place in the queue: when the task started, so one sent back for another round
        # (feedback, next, resume) goes ahead of tasks started after it; an image, from now
        t = store.Task(task) if task else None
        self.since = (t.spec.get("created_at") if t and t.exists else None) or store.now()
        # when this wait began, for how long it has waited: `since` is the task's age, so a task
        # sent back three hours after it started read "waiting 3h00m" ten seconds in (#573)
        self.waiting_since: float | None = None

    def _holders(self) -> dict:
        return holders(self.home, self.pool)

    def _set(self, held: dict) -> None:
        store.write_json(self.home / f"{self.pool}.json", held)

    def _entry(self, waiting: bool = False) -> dict:
        # the start time tells this runner from a later process given its pid (after a reboot)
        if not waiting:
            self.waiting_since = None
        elif self.waiting_since is None:
            self.waiting_since = store.now()
        return {"model": self.m.id if self.m else None, "task": self.task, "since": self.since,
                "start": _started(os.getpid()),
                **({"waiting": True, "waiting_since": self.waiting_since} if waiting else {})}

    def warn(self, msg: str) -> None:
        """What Claude should see later, not only in status.last (overwritten within moments):
        also a `gpu` event in the task's transcript."""
        self.say(msg)
        task = store.Task(self.task)
        if task.exists:
            task.event({"ts": store.now(), "round": task.status.get("round", 1), "kind": "gpu",
                        "text": msg})

    def __enter__(self):
        self.home.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.home / f"{self.pool}.lock", "a+")
        try:
            self._admit()
            return self
        except BaseException:
            self._leave()  # a stop while queued used to leave a waiting entry behind (the
            raise          # 2026-11-16 audit), which made every later task wait alone

    def _admit(self) -> None:
        with store.locked(self.home / f"{self.pool}.admit"):
            held = {p: h for p, h in self._holders().items() if p != self.me}
            if held and self.m is None and not self.wait:
                raise RuntimeError(busy(held, f"{self.task} needs it to itself. Run it once they "
                                        "are in review (answer one that asks first), or with "
                                        "--wait, in the background, to queue behind them"))
            if admits(self.m, held):
                try:
                    fcntl.flock(self.fh, fcntl.LOCK_SH | fcntl.LOCK_NB)
                except BlockingIOError:
                    pass  # something has the GPU to itself: wait below
                else:
                    self._set({**held, self.me: self._entry()})
                    if held:
                        self.say("sharing the GPU with " + ", ".join(
                            sorted({h["model"] for h in held.values() if h.get("model")})))
                    return
            self._set({**held, self.me: self._entry(waiting=True)})
        self._take_alone(held)

    def step_aside(self, unload: bool = False) -> None:
        """Let the GPU go while the task needs none of it: its tests (up to --test-timeout, every
        round) and a wait for its server (up to 20 minutes) ran under the lease, so queued tasks
        waited through them, and a project's own GPU tests ran beside the resident model, their
        out-of-memory failures sent back to the agent as its bug (#257, the audit of 1.2).
        ``unload`` (tests that use the GPU, --unload-for-tests): the model too, unless another
        holder runs it, and the lease stays held for the tests, an entry with no model (`tests`)
        that nobody joins, so wb start refuses and a queued task waits: let go, a queued task was
        admitted and ran a model round beside them, both allocating VRAM at once (#428, the audit
        of 1.8). On a remote model's server the tests use none of its GPU: let go there.
        `rejoin` before the next round."""
        if self.aside or not self.fh:
            return
        # The unload is decided and sent while this task still holds the lease, under the lock
        # every admission takes: decided after letting go, from the holders seen before, it
        # unloaded the model the next task had just been admitted with and loaded (#327, the
        # audit of 1.4). Nobody is admitted meanwhile, and a task let in after it loads again.
        with store.locked(self.home / f"{self.pool}.admit"):
            held = self._holders()
            held.pop(self.me, None)
            if unload:
                self._unload_unshared(held)
            if unload and not (self.m and self.m.remote):
                self._set({**held, self.me: {**self._entry(), "model": None, "tests": True}})
            else:
                self._set(held)
                fcntl.flock(self.fh, fcntl.LOCK_UN)
            self.aside = True

    def _unload_unshared(self, held: dict) -> None:
        held_ids = keep_loaded()  # the user's own (#215), and only Ollama unloads on request
        if self.m and self.m.engine == "ollama" and not self.m.remote and not _fake() \
                and self.m.id not in held_ids and not _kept_name(self.m.name, held_ids):
            found = discover()
            if _key(self.m) not in {_key(o) for o in (found.get(h.get("model")) for h in held.values()) if o}:
                _unload(self.m)

    def rejoin(self) -> None:
        """Back on the GPU after `step_aside`: beside the holders when the model fits, else
        alone, in first-come order by the task's start (``since``), so it keeps its place ahead
        of tasks started after it."""
        if self.aside:
            self.aside = False
            self._admit()

    def _take_alone(self, held: dict) -> None:
        """Wait for the GPU to itself (marked waiting in gpu.json, so nobody joins the holders
        meanwhile), saying who holds it: the runner's status.last, the pane's line."""
        others = sorted({h["task"] for p, h in held.items() if p != self.me})
        if others:  # a queued task used to wait in silence, its holder unnamed (the 2026-11-16 audit)
            self.say(f"waiting for the GPU (held by {', '.join(others)})")
        # First come, first served: a blocking flock hands the lock to any of its waiters, so three
        # queued tasks started in no particular order (#108). Each waits its turn in gpu.json
        # (oldest `since` first, then task name) and only the first tries the lock.
        # Holding nothing meanwhile: a lease let in sharing whose load then misfit kept its shared
        # gpu.lock while second in line, so the first's LOCK_EX never succeeded, and every later
        # task queued behind the two for good (#366, the audit of 1.6)
        if self.fh:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
        with Watcher(self.home) as w:
            while True:
                if self._first():
                    try:
                        fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        pass  # the holders are still running (or still letting go)
                w.wait(QUEUE_POLL)
        with store.locked(self.home / f"{self.pool}.admit"):
            # nobody else can hold gpu.lock now: any other non-waiting entry is stale
            self._set({**{p: h for p, h in self._holders().items() if h.get("waiting")},
                       self.me: self._entry()})

    def _first(self) -> bool:
        """Whether this lease is first among the waiting entries (or the only one)."""
        waiting = {p: h for p, h in self._holders().items() if h.get("waiting")}
        order = sorted(waiting, key=lambda p: (waiting[p].get("since") or 0,
                                               waiting[p].get("task") or ""))
        return not order or order[0] == self.me or self.me not in waiting

    def load(self) -> float:
        """Make the model resident, leaving the other holders' models loaded. Loads take turns
        under gpu.load, never under gpu.admit: a task finishing beside a long load used to wait
        for it to leave the lease, so its runner looked alive after its review notice (the
        2026-11-16 audit). Lock order: gpu.load, then gpu.admit."""
        if self.m.remote:
            return make_resident(self.m, say=self.say, warn=self.warn)
        with store.locked(self.home / f"{self.pool}.load"):
            with store.locked(self.home / f"{self.pool}.admit"):
                held = self._holders()
                keep = {h["model"] for p, h in held.items()
                        if p != self.me and not h.get("waiting") and h.get("model")}
            before = set(_ollama_loaded(self.m.root)) if keep and self.m.engine == "ollama" \
                and not _fake() else None
            secs = make_resident(self.m, say=self.say, keep=keep, warn=self.warn)
            # a load that loaded nothing evicted nothing (the 0.90 review)
            if not keep or not secs or _fits_beside(self.m, keep, before):
                return secs
            self.warn(f"ollama could not fit {self.m.id} beside {', '.join(sorted(keep))}; "
                      "waiting for the GPU to itself")
            found = discover()
            if _key(self.m) not in {_key(o) for o in (found.get(k) for k in keep) if o}:
                _unload(self.m)  # or the holders' next requests swap it in and out; a model a
                # holder runs too is theirs as well, and stops being asked for once we wait
            with store.locked(self.home / f"{self.pool}.admit"):
                held = self._holders()
                self._set({**held, self.me: self._entry(waiting=True)})
        self._take_alone(held)  # it drops the shared lock, then waits its turn (#366)
        with store.locked(self.home / f"{self.pool}.load"):
            return secs + make_resident(self.m, say=self.say, warn=self.warn)

    def _leave(self) -> None:
        if not self.fh:
            return
        try:
            with store.locked(self.home / f"{self.pool}.admit"):
                held = self._holders()
                held.pop(self.me, None)
                self._set(held)
        finally:
            self.fh.close()  # releases gpu.lock
            self.fh = None

    def __exit__(self, *exc):
        self._leave()


def busy(held: dict, why: str) -> str:
    """Who holds the GPU, for a refusal: each holder's task, its repo and model, and its phase."""
    def one(h):
        task = store.Task(h.get("task") or "")
        if not h.get("task") or not task.exists:
            return h.get("task") or "something"
        spec, phase = task.spec, task.status.get("phase")
        return (f"task {task.name} (repo {spec.get('repo')}, {h.get('model') or spec.get('model')}"
                + (f", {phase}" if phase else "") + (", waiting for it" if h.get("waiting") else "") + ")")
    return f"the GPU is in use by {', '.join(one(h) for h in held.values())}: {why}"


def pool(m: Model | None) -> str:
    """The lease files a model takes turns under: gpu.* for this machine's GPU, gpu-<host>.* for
    a remote server's (its host and port in file-name characters)."""
    if not (m and m.remote):
        return "gpu"
    u = urlparse(m.root)
    return "gpu-" + re.sub(r"[^a-z0-9]+", "-", f"{u.hostname or 'host'}-{u.port or ''}".lower()).strip("-")


def _started(pid: int) -> str | None:
    """When the process started: /proc/<pid>/stat's 22nd field, else ps's `lstart` (macOS, where a
    stale gpu.json holder under a recycled pid of ours was trusted, and kept tasks from sharing:
    #236); None when neither says."""
    try:
        return Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()[19]
    except (OSError, IndexError):
        pass
    try:
        r = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True,
                           timeout=5, env={**os.environ, "LC_ALL": "C"})
    except (OSError, subprocess.TimeoutExpired):
        return None
    return (" ".join(r.stdout.split()) or None) if r.returncode == 0 else None


PROC = Path("/proc")


def runs(pid: int, *words: str) -> bool:
    """Whether process ``pid`` runs a command line holding these words in a row (`-m`,
    `workbench.bench`): a pid recycled after an unclean shutdown runs something else, and a stop
    would signal it. /proc's cmdline, else `ps` (macOS: there the bench and optimize stops trusted
    any live pid, #82, the 2026-11-18 audit); with neither, the pid is trusted."""
    try:
        raw = (PROC / str(pid) / "cmdline").read_bytes()
        toks = [a.decode(errors="replace") for a in raw.split(b"\0")]
    except OSError:
        if (PROC / "self").exists():
            return False  # /proc works: the pid is gone
        # -ww: BSD ps cuts the column at 79 characters, and a framework Python's path alone is longer
        try:
            toks = subprocess.run(["ps", "-ww", "-o", "command=", "-p", str(pid)],
                                  capture_output=True, text=True, timeout=5).stdout.split()
        except (OSError, subprocess.SubprocessError):
            return True
    n = len(words)
    return any(toks[i:i + n] == list(words) for i in range(len(toks) - n + 1))


def start_tag(pid: int) -> str:
    """`_started` short enough for a file name (its crc32, in hex), '' when unknown: a scratch
    directory's name carries its maker's, so wb gc knows a recycled pid isn't the maker (#532)."""
    s = _started(pid)
    return format(zlib.crc32(s.encode()), "08x") if s else ""


def _alive(pid: int, start: str | None = None) -> bool:
    """Whether the holder is still running: its pid is this user's (a recycled pid of another
    user's is not), and it started when the holder did (a recycled pid of ours didn't: /proc, else
    ps). For an entry without `start`, or where neither says, the pid alone decides."""
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    except (OverflowError, ValueError):
        return False
    now = _started(pid) if start else None
    return now is None or now == start


# ---- managing the set: what Claude's weekly model check uses to add and retire models ----

# Ollama's OpenAI-compatible API ignores `max_completion_tokens`, the only cap OpenCode sends, and
# honours `max_tokens` (checked on 0.40: asked for 60, it gave 1,492). Without this, a model that
# falls into a degenerate generation runs until its context is full: 20K-token turns with no
# edits, seen 2026-11-16. So every Ollama entry carries max_tokens in its options, which OpenCode
# passes through: room for the reasoning plus the answer's own output limit.
def ollama_cap(entry: dict) -> int:
    out = (entry.get("limit") or {}).get("output") or 4096
    return 2 * out


def providers() -> dict[str, dict]:
    """OpenCode's local providers: id → {"root", "url", "engine", "engine_known", "start",
    "problem", "key" (its apiKey, resolved, else auth.json's: never printed), "models"} (the config's model entries)."""
    extra, dirs = _overrides(), _provider_dirs()
    def key(pid, prov):
        raw = str((prov.get("options") or prov.get("settings") or {}).get("apiKey") or "")
        return _expand(raw, base=dirs.get(pid)) if raw else auth_key(pid)   # #336, the audit of 1.4
    return {pid: {"root": url.rstrip("/").removesuffix("/v1"), "url": url,
                  "models": prov.get("models") or {}, "engine": engine,
                  "key": key(pid, prov),
                  "engine_known": engine_known(url, extra.get(pid) or {}),
                  "start": (extra.get(pid) or {}).get("start"),
                  "problem": url_problem(url, engine, prov.get("npm"))}
            for pid, prov, url, engine, _ in _local_providers(read_config(), extra, dirs)}


def ollama_root_here() -> str:
    """Where this machine's Ollama listens: its OLLAMA_HOST (a bind address read as loopback),
    else its default port."""
    raw = (hardware.ollama_env().get("OLLAMA_HOST") or "").strip()
    if not raw:
        return f"http://127.0.0.1:{OLLAMA_PORT}"
    u = urlparse(raw if "://" in raw else f"http://{raw}")
    host = u.hostname or "127.0.0.1"
    host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    return f"{u.scheme}://{f'[{host}]' if ':' in host else host}:{u.port or OLLAMA_PORT}"


def ollama_answers(root: str) -> bool:
    if _fake():
        return False
    body = _get(f"{root}/api/version", timeout=1.5)
    return isinstance(body, dict) and "version" in body


def _add_provider(cfg: dict, pid: str, name: str, base: str) -> None:
    key, url = ("providers", "settings") if "providers" in cfg and "provider" not in cfg \
        else ("provider", "options")  # the first: OpenCode 2.x's own shape
    entry = {"npm": "@ai-sdk/openai-compatible", "name": name, url: {"baseURL": base}}
    with own_config(f'under "{key}" add "{pid}": {json.dumps(entry)}') as (_, own):
        own.setdefault(key, {})[pid] = entry  # the user's file only: never the merge (#421)


def _has_provider(cfg: dict, pid: str) -> bool:
    return _provider(cfg, pid) is not None


def _provider(cfg: dict, pid: str) -> dict | None:
    """Provider ``pid``'s entry in OpenCode's config, under either shape's key."""
    return (cfg.get("provider") or {}).get(pid) or (cfg.get("providers") or {}).get(pid)


def _provider_url(prov: dict) -> str:
    """A provider entry's raw baseURL (options, or the 1.x shape's settings), or ""."""
    return str((prov.get("options") or prov.get("settings") or {}).get("baseURL") or "")


def add_ollama_provider(pid: str) -> str | None:
    """Give OpenCode a provider ``pid`` for this machine's Ollama when its config has none by that
    id and Ollama answers; its root, or None. A first run had no provider and no command made one
    (the 2026-11-16 audit). A provider the user wrote is never touched."""
    cfg = read_config()
    if _has_provider(cfg, pid):
        return None
    root = ollama_root_here()
    if not _fake() and not ollama_answers(root):
        return None
    _add_provider(cfg, pid, "Ollama", f"{root}/v1")
    return root


def add_server_provider(pid: str, base: str) -> bool:
    """Give OpenCode a provider ``pid`` for the OpenAI-compatible server at ``base`` (its
    baseURL, usually ending in /v1), whatever runs it: llama.cpp, LM Studio, vLLM, Ollama. Only
    Ollama had a command for it, so every other server meant editing opencode.json by hand. False
    when the config already has ``pid`` for that same endpoint (a provider the user wrote is never
    touched); RuntimeError when it has ``pid`` for another one."""
    u = urlparse(base)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise RuntimeError(f"--url {base}: give the server's baseURL, e.g. http://127.0.0.1:8080/v1")
    if not _this_host(u.hostname) and not (_overrides().get(pid) or {}).get("local"):
        raise RuntimeError(f"{base} isn't on this machine: workbench hands work only to local models. "
                           f"If it is your own server (WSL2's Windows host, a LAN box), give {pid} "
                           f"\"local\": true in {overrides_path()} first")
    cfg = read_config()
    prov = _provider(cfg, pid)
    if prov is not None:
        have = _provider_url(prov)
        if base.rstrip("/") in (have.rstrip("/"), _expand(have, base=_provider_dirs().get(pid)).rstrip("/")):
            return False
        # the model was registered under the old endpoint and wb said it was added: two servers
        # serving one model name then did plausible work on the wrong machine (#334, the audit of 1.4)
        raise RuntimeError(f"OpenCode's config has a provider {pid} already, for "
                           f"{have or 'no baseURL'}, not {base}: wb leaves it alone. Give this server "
                           f"a provider id of its own (wb model add <provider>/<model> --url {base}), "
                           f"or change {pid}'s baseURL in {opencode_config_path()} by hand")
    _add_provider(cfg, pid, pid, base.rstrip("/"))
    return True


def remote_pid(host: str) -> str:
    """The provider id wb remote gives another machine's Ollama: ollama-<host>, in id characters."""
    return "ollama-" + (re.sub(r"[^a-z0-9]+", "-", host.lower()).strip("-") or "host")


def add_remote(host: str, port: int = OLLAMA_PORT) -> tuple[str, str, list[str]]:
    """Give OpenCode a provider for the Ollama on another machine (a gaming PC on the LAN or
    tailnet), marked local in models.json: (its provider id, its root, the tool-capable models it
    has pulled, cloud models left out). Raises RuntimeError when it doesn't answer (#114)."""
    root = f"http://{host}:{port}"
    if not ollama_answers(root):
        raise RuntimeError(f"no Ollama answers at {root}: on {host}, have Ollama listen on the "
                           "network (OLLAMA_HOST=0.0.0.0, then restart it), and check the port")
    pid = remote_pid(host)
    cfg = read_config()
    prov = _provider(cfg, pid)
    if prov is None:
        _add_provider(cfg, pid, f"Ollama on {host}", f"{root}/v1")
        set_override(pid, "remote", root)  # wb made it, so --remove may delete it
    elif _provider_url(prov).rstrip("/") != f"{root}/v1":
        # a provider of that id for another endpoint (a port of its own, or the user's): kept,
        # and its models were reported as the new endpoint's (#197, the 2026-11-19 audit)
        raise RuntimeError(f"OpenCode's config has a provider {pid} already, for "
                           f"{_provider_url(prov) or 'no baseURL'}, not {root}/v1: wb remote leaves it "
                           f"alone. Rename or remove it, or add that server's models with wb model "
                           f"add <provider>/<model> --url {root}/v1")
    set_override(pid, "local", True)  # the user's own machine: its models count as local
    names = []
    for row in (_get(f"{root}/api/tags") or {}).get("models", []):
        name = row.get("name")
        if not name or _cloud_row(row):
            continue
        try:
            caps = ollama_show(root, name, timeout=10).get("capabilities") or []
        except Exception:  # a model it can't describe is one wb model add would refuse anyway
            continue
        if "tools" in caps:
            names.append(name)
    return pid, root, names


def remove_remote(host: str) -> tuple[str, list[str]]:
    """Undo add_remote: its models out of OpenCode's config with their models.json entries, the
    provider and its local mark gone. (provider id, the models removed)."""
    pid = remote_pid(host)
    cfg = read_config()
    key = "provider" if pid in (cfg.get("provider") or {}) else "providers"
    prov = (cfg.get(key) or {}).get(pid)
    if prov is None:
        raise RuntimeError(f"no provider {pid} in {opencode_config_path()}: nothing to remove")
    # only the provider wb remote made: models.json's `remote` mark, while the provider still
    # points where the mark says, or (made before the mark) its name and host as add_remote writes
    # them. A hand-written provider of that id was deleted with its models and their settings
    # (#197, the 2026-11-19 audit), and so was one the user repointed at another server after
    # wb remote made it, its mark left behind (#242, the audit of 1.2)
    mark = (_overrides().get(pid) or {}).get("remote")
    if not (f"{mark}/v1" == _provider_url(prov).rstrip("/") if mark else (
            prov.get("name") == f"Ollama on {host}"
            and (urlparse(_provider_url(prov)).hostname or "") == host.lower())):
        raise RuntimeError(f"{pid} in {opencode_config_path()} isn't one wb remote added (it "
                           f"points at {_provider_url(prov) or 'no baseURL'}): remove it by hand")
    if str(cfg.get("model") or "").startswith(pid + "/"):
        raise RuntimeError(f"{cfg['model']} is OpenCode's default model; wb models --set-default "
                           "picks another first")
    names = sorted(prov.get("models") or {})
    change = f'under "{key}" remove "{pid}"'
    with own_config(change) as (target, own):
        if where := set_elsewhere(target, "provider", pid):  # it would stay in effect (#421)
            raise _not_ours(f"provider {pid}", where, change)
        for k in ("provider", "providers"):
            (own.get(k) or {}).pop(pid, None)
    for mid in [f"{pid}/{n}" for n in names] + [pid]:
        for k in list(_overrides().get(mid) or {}):
            set_override(mid, k, None)
    return pid, names


# Where the common local model servers listen by default and what their command is called, for
# wb scout: Claude works with whatever the user runs, so it needs to see what that is (only
# Ollama was looked for, and the skill steered every first run to it).
SERVER_PORTS = ((11434, "Ollama"), (1234, "LM Studio"), (8080, "llama.cpp, llama-swap, LocalAI or MLX"),
                (8000, "vLLM"), (5001, "KoboldCpp"), (30000, "SGLang"))
SERVER_COMMANDS = ("ollama", "lms", "llama-server", "llama-swap", "vllm", "sglang", "koboldcpp",
                   "local-ai", "mlx_lm.server", "jan")


def servers_here() -> tuple[list[str], list[dict]]:
    """The model servers' commands on PATH, and the servers answering on their default ports here:
    {"root", "usually", "models"} (the ids an OpenAI-compatible /v1/models lists)."""
    on_path = [c for c in SERVER_COMMANDS if shutil.which(c)]
    if _fake():
        return on_path, []
    answering = []
    for port, usually in SERVER_PORTS:
        root = f"http://127.0.0.1:{port}"
        body = _get(f"{root}/v1/models", timeout=1)
        if isinstance(body, dict) and isinstance(body.get("data"), list):
            answering.append({"root": root, "usually": usually,
                              "models": [str(x.get("id")) for x in body["data"] if isinstance(x, dict)]})
    return on_path, answering


def ollama_cloud(root: str, name: str) -> bool:
    """Whether Ollama forwards this model to its cloud: by name, or by its /api/tags row."""
    if _cloud_name(name) or _fake():
        return _cloud_name(name)
    return any(_cloud_row(x) for x in (_get(f"{root}/api/tags") or {}).get("models", [])
               if _matches(name, {x.get("name")}))


def ollama_installed(root: str) -> list[dict]:
    """What an Ollama has pulled: name, bytes on disk, parameters, quantization."""
    if _fake():
        return []
    return [{"name": x.get("name"), "size": x.get("size"), "cloud": _cloud_row(x),
             "params": (x.get("details") or {}).get("parameter_size"),
             "quant": (x.get("details") or {}).get("quantization_level")}
            for x in (_get(f"{root}/api/tags") or {}).get("models", [])]


def ollama_show(root: str, name: str, timeout: float = 30) -> dict:
    """Ollama's own facts about a pulled model: its capabilities, trained context length, and
    the num_ctx its Modelfile sets, if any."""
    if _fake():
        return {"capabilities": ["tools", "thinking"], "context_length": None, "num_ctx": None}
    body = _post(f"{root}/api/show", {"model": name}, timeout=timeout)
    ctx = next((v for k, v in (body.get("model_info") or {}).items()
                if k.endswith(".context_length")), None)
    num = next((int(p.split()[1]) for p in (body.get("parameters") or "").splitlines()
                if p.split()[:1] == ["num_ctx"]), None)
    return {"capabilities": body.get("capabilities") or [], "context_length": ctx, "num_ctx": num}


def served_window(m: Model) -> int | None:
    """The window the server gives ``m`` now that it is loaded: Ollama's /api/ps
    context_length, else what server_facts reads (llama-server's n_ctx, vLLM's max_model_len,
    LM Studio's loaded instance). None when the server doesn't say."""
    if _fake():
        return None
    if m.engine == "ollama":
        for x in (_get(f"{m.root}/api/ps") or {}).get("models", []):
            if _matches(m.name, {x.get("name")}) and x.get("context_length"):
                return int(x["context_length"])
        return None
    _FACTS.pop((m.root, m.name), None)  # fresh: this is what the round will meet
    return server_facts(m.root, m.name, m.api_key)["context"]


def short_window(m: Model) -> str | None:
    """Why ``m`` can't run as OpenCode is told: the server serves less than its limit.context,
    so it would cut prompts off silently while OpenCode, thinking there is room, never compacts
    (#248, the audit of 1.2). None when it serves enough, or doesn't say."""
    said = (m.config.get("limit") or {}).get("context")
    served = served_window(m)
    if not said or not served or served >= said:
        return None
    fix = ("raise OLLAMA_CONTEXT_LENGTH (Ollama restarted without it?)" if m.engine == "ollama"
           else "start the server with a bigger window")
    return (f"the server serves {m.id} a {served // 1024}K window, but OpenCode is told "
            f"{said // 1024}K (limit.context): it would cut the prompt off silently. Fix: {fix}, "
            + (f"or wb model add {m.id} --context {served}" if served >= MIN_WINDOW
               else f"tasks need {MIN_WINDOW // 1024}K+"))


def ollama_served_context(root: str, name: str) -> int | None:
    """The window Ollama gives this model over OpenCode's API, which can't ask for one: what it
    runs with if loaded, else its Modelfile's num_ctx, else OLLAMA_CONTEXT_LENGTH. None when only
    Ollama's own default would say (it varies by version and VRAM)."""
    if _fake():
        return None
    ps, num = _get(f"{root}/api/ps"), None
    for x in (ps or {}).get("models", []):
        if _matches(name, {x.get("name")}) and x.get("context_length"):
            return int(x["context_length"])
    if ps is not None:  # one that didn't answer /api/ps won't answer /api/show: a sleeping LAN
        try:            # box cost 30 s a model here, and doctor ran past 2 minutes (the 2026-11-16 audit)
            num = ollama_show(root, name, timeout=10).get("num_ctx")  # metadata, not a load
        except Exception:
            num = None
    # OLLAMA_CONTEXT_LENGTH is this machine's service's: it says nothing of another host's
    return num or (hardware.ollama_context()[0] if _this_host(urlparse(root).hostname) else None)


def ollama_pull(root: str, name: str, say=print) -> None:
    """Download a model, reporting progress about every 5% (long: run it in the background)."""
    if _fake():
        say(f"pulled {name} (fake)")
        return
    req = urllib.request.Request(f"{root}/api/pull", data=json.dumps({"model": name}).encode(),
                                 headers={"Content-Type": "application/json"})
    shown, last = -1, ""
    with _open(req, timeout=24 * 3600) as r:
        for raw in r:
            ev = json.loads(raw or b"{}")
            if ev.get("error"):
                raise RuntimeError(ev["error"])
            status, total, done = ev.get("status", ""), ev.get("total"), ev.get("completed")
            if status != last:
                shown = -1  # a new layer counts from 0 again
            if total and done is not None:
                pct = int(100 * done / total)
                if pct >= shown + 5 or pct == 100:
                    shown = pct
                    say(f"{status}: {pct}% of {total / 1e9:.1f} GB")  # decimal, as Ollama says it
            elif status != last:
                say(status)
            last = status


def ollama_delete(root: str, name: str) -> None:
    if _fake():
        return
    req = urllib.request.Request(f"{root}/api/delete", data=json.dumps({"model": name}).encode(),
                                 headers={"Content-Type": "application/json"}, method="DELETE")
    _open(req, timeout=60).close()


def set_override(model_id: str, key: str, value) -> None:
    """Set (or with None, drop) one key of a model's models.json entry."""
    with store.edit_config(overrides_path(), _overrides) as data:  # through a link, locked (#495)
        entry = data.setdefault(model_id, {})
        if value is None:
            entry.pop(key, None)
        else:
            entry[key] = value
        if not entry:
            data.pop(model_id)


def write_entry(provider: str, name: str, entry: dict | None) -> None:
    """Add, replace (entry) or remove (None) one model in OpenCode's config: in the user's file
    (own_config), the provider looked up in the merged view. A provider that file doesn't define
    gets the model as an overlay there (an admin's, from the system file), unless only the
    session's OPENCODE_CONFIG_CONTENT defines it, which wb can't write; a model another layer sets
    is not removed, nor changed when that layer loads later and wins (#421)."""
    cfg = read_config()
    if _provider(cfg, provider) is None:
        raise RuntimeError(f"OpenCode has no provider {provider!r}")
    if entry is None:
        change = f'delete "{name}" from provider {provider}\'s "models"'
    else:
        change = (f'under provider {provider}\'s "models", merge into "{name}" (keeping keys you '
                  f"added): {json.dumps(entry)}")  # wb's own keys only: never the rest of the config
    with own_config(change) as (target, own):
        if where := set_elsewhere(target, "provider", provider, "models", name, later=entry is not None):
            raise _not_ours(f"{provider}/{name}", where, change)
        mine = _provider(own, provider)
        if mine is None:
            if entry is None:
                return  # in no layer at all: nothing to remove
            if set_elsewhere(target, "provider", provider) == [CONTENT]:
                raise _not_ours(f"provider {provider}", [CONTENT], change)
            key = next((k for k in ("provider", "providers") if k in own),
                       "provider" if provider in (cfg.get("provider") or {}) else "providers")
            mine = own.setdefault(key, {}).setdefault(provider, {})
        entries = mine.setdefault("models", {})
        if entry is None:
            entries.pop(name, None)
        else:  # keys added by hand (variants, options) stay
            old = entries.get(name) or {}
            entries[name] = {**old, **entry, "options": {**(old.get("options") or {}), **(entry.get("options") or {})}}
            if not entries[name]["options"]:
                del entries[name]["options"]
