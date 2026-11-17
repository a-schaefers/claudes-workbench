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
(`remote`) is left out of all of it: this machine's GPU is not the one it runs on, so it never
waits for the lease, frees nothing here, and nothing here unloads or stops it.
"""
from __future__ import annotations

import fcntl
import functools
import ipaddress
import json
import os
import re
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from . import hardware, store

LOCAL_HOSTS = {"localhost", "0.0.0.0", "host.docker.internal"}
OLLAMA_PORT = 11434  # Ollama's default: the guess while its server is down
START_TIMEOUT = 20 * 60  # a big model server's first start can take minutes


@dataclass
class Model:
    id: str            # the OpenCode model id, "provider/model": the one name used everywhere
    provider: str
    name: str          # the model's name at its server
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


def _config_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "opencode"


def opencode_config_path() -> Path:
    """OpenCode's global config, where OpenCode reads it: $XDG_CONFIG_HOME/opencode (default
    ~/.config/opencode, on macOS too), opencode.json or opencode.jsonc: the one wb writes."""
    if os.environ.get("WB_OPENCODE_CONFIG"):
        return Path(os.environ["WB_OPENCODE_CONFIG"])
    d = _config_dir()
    return next((p for p in (d / "opencode.json", d / "opencode.jsonc") if p.exists()),
                d / "opencode.json")


def opencode_config_files() -> list[Path]:
    """Every global file OpenCode layers, in its order: opencode.json, then opencode.jsonc (both
    when both exist: checked on OpenCode 2.3, the 2026-11-16 audit). $WB_OPENCODE_CONFIG alone."""
    d = _config_dir()
    paths = [Path(os.environ["WB_OPENCODE_CONFIG"])] if os.environ.get("WB_OPENCODE_CONFIG") else \
        [d / "opencode.json", d / "opencode.jsonc"]
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


def read_config() -> dict:
    """OpenCode's global config as OpenCode sees it: its files merged, the later winning. Read
    one file only, a provider kept in opencode.jsonc beside an opencode.json holding a theme was
    invisible (the 2026-11-16 audit)."""
    cfg: dict = {}
    for path in opencode_config_files():
        cfg = _merge(cfg, json.loads(_strip_jsonc(path.read_text())))
    return cfg


def write_config(cfg: dict) -> None:
    """Write OpenCode's config back; refused for a file with comments, which a rewrite would lose,
    and while two files are layered, which one merged file would rewrite."""
    files = opencode_config_files()
    if len(files) > 1:
        raise RuntimeError(f"OpenCode layers {' and '.join(map(str, files))}; wb won't rewrite "
                           f"either: make this change by hand: {json.dumps(cfg)[:2000]}")
    path = opencode_config_path()
    try:
        json.loads(path.read_text() if path.exists() else "{}")
    except ValueError:
        raise RuntimeError(f"{path} has comments, which rewriting it would lose; make this change "
                           f"by hand: {json.dumps(cfg)[:2000]}") from None
    store.write_json(path, cfg)


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
    try:  # every interface's, where `ip` is there (Linux)
        out = subprocess.run(["ip", "-o", "addr"], capture_output=True, text=True, timeout=5).stdout
        found |= {w.split("/")[0] for line in out.splitlines() for i, w in enumerate(line.split())
                  if i and line.split()[i - 1] in ("inet", "inet6")}
    except (OSError, subprocess.TimeoutExpired):
        pass
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

# A server on this machine is asked directly, never through a proxy: urllib honours http_proxy and
# exempts loopback only when no_proxy says so, so behind a corporate proxy every local model read
# as "not answering" (the 2026-11-16 audit). A provider on another host keeps the environment's.
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _open(target, timeout: float):
    url = target.full_url if isinstance(target, urllib.request.Request) else target
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
    if _fake():
        return guess
    try:
        with _open(f"{root}/api/version", timeout=1.5) as r:
            body = json.loads(r.read() or b"{}")
        _ENGINES[root] = "ollama" if isinstance(body, dict) and "version" in body else "server"
    except urllib.error.HTTPError:  # it answered, without Ollama's API
        _ENGINES[root] = "server"
    except Exception:  # down, or not speaking JSON: the port is the best guess
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


def _expand(value: str, agent: bool = True) -> str:
    """OpenCode's `{env:NAME}` and `{file:path}` in a config string, resolved as OpenCode does:
    an unset name or an unreadable file is empty; a relative path is the config's directory's.
    With ``agent``, a name the agent's environment drops is empty too: its OpenCode won't see it.
    Taken raw, such a baseURL had no host and its provider vanished (the 2026-11-16 audit)."""
    def one(mt):
        kind, arg = mt.group(1), mt.group(2).strip()
        if kind == "env":
            return os.environ.get(arg, "") if not agent or _agent_sees(arg) else ""
        path = Path(os.path.expanduser(arg))
        try:
            return (path if path.is_absolute() else opencode_config_path().parent / path).read_text().strip()
        except OSError:
            return ""
    return _TEMPLATE.sub(one, value or "")


def _scan(cfg: dict, extra: dict):
    """(provider id, its config, its baseURL resolved, why it is skipped or "", remote) for each
    provider with a baseURL, as OpenCode loads them: disabled_providers and enabled_providers
    followed (the router used to pick a provider OpenCode wouldn't load: the 2026-11-16 audit).
    A provider with no baseURL is one of OpenCode's own cloud providers: not listed."""
    enabled, disabled = cfg.get("enabled_providers"), set(cfg.get("disabled_providers") or [])
    for pid, prov in (cfg.get("provider") or cfg.get("providers") or {}).items():
        opts = prov.get("options") or prov.get("settings") or {}
        raw = opts.get("baseURL") or ""
        if not raw:
            continue
        url = _expand(raw)
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


def _local_providers(cfg: dict, extra: dict):
    """(provider id, its config, its baseURL, engine, remote) for each provider on this machine,
    or opted in with models.json "local": true."""
    for pid, prov, url, why, remote in _scan(cfg, extra):
        if not why:
            yield pid, prov, url, _engine(url, extra.get(pid) or {}, pid), remote


def skipped_providers() -> list[tuple[str, str]]:
    """(provider id, why) for each provider with a baseURL that discovery leaves out."""
    return [(pid, why) for pid, _, _, why, _ in _scan(read_config(), _overrides()) if why]


def why_none() -> str:
    """Why discovery found no model: the providers it passed over, or how to make one."""
    skipped = skipped_providers()
    if skipped:
        return "; ".join(f"provider {pid}: {why}" for pid, why in skipped)
    return (f"OpenCode has no local provider in {opencode_config_path()}: with Ollama running, "
            "wb model add ollama/<model> adds one; or add an OpenAI-compatible provider whose "
            "baseURL is on this machine")


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


def discover() -> dict[str, Model]:
    """Every local model OpenCode knows, by OpenCode id, in config order."""
    extra = _overrides()
    found: dict[str, Model] = {}
    for pid, prov, url, engine, remote in _local_providers(read_config(), extra):
        opts = prov.get("options") or prov.get("settings") or {}
        problem = url_problem(url, engine, prov.get("npm"))
        key = _expand(str(opts.get("apiKey") or ""), agent=False)
        for name, spec in (prov.get("models") or {}).items():
            mid = f"{pid}/{name}"
            over = {**extra.get(pid, {}), **extra.get(mid, {})}
            if over.get("skip"):
                continue
            # an Ollama cloud model was routed trivial work as "the smallest" (the 2026-11-16 audit)
            cloud = engine == "ollama" and _cloud_name(name)
            cloud_ok = cloud and (extra.get(mid) or {}).get("local") is True
            found[mid] = Model(id=mid, provider=pid, name=name, base_url=url, engine=engine,
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
    exact = [m for m in models.values() if wanted in (m.name, m.provider)]
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
    with _open(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def _fake() -> bool:
    return bool(os.environ.get("WB_FAKE_MODELS"))  # tests drive a fake opencode, no servers


def _ollama_names(m: Model, endpoint: str) -> set[str]:
    body = _get(f"{m.root}/api/{endpoint}") or {}
    return {x.get("name") for x in body.get("models", [])}


def _matches(name: str, names: set[str]) -> bool:
    return name in names or f"{name}:latest" in names


def _no(why: str) -> dict:
    return {"available": False, "resident": False, "why": why}


def _tag_row(name: str, tags: dict) -> dict | None:
    return next((x for x in tags.get("models") or [] if _matches(name, {x.get("name")})), None)


CLOUD_WHY = ("an Ollama cloud model: it runs on ollama.com, off this machine (models.json "
             "\"local\": true on {id} allows it)")


def status(m: Model) -> dict:
    """``{available, resident, why}``: can the runner get this model onto the GPU, is it there.
    What the config alone rules out comes first, so the fake models meet it too."""
    if m.config.get("tool_call") is False:  # the router used to hand it tasks (the 2026-11-16 audit)
        return _no("OpenCode's config says it can't call tools (tool_call: false): agent work needs them")
    if m.problem:
        return _no(m.problem)
    if m.cloud and not m.cloud_ok:
        return _no(CLOUD_WHY.format(id=m.id))
    if _fake():
        return {"available": True, "resident": True, "why": "fake"}
    if m.engine == "ollama":
        tags = _get(f"{m.root}/api/tags")
        if tags is None:
            return _no("ollama is not answering")
        row = _tag_row(m.name, tags)
        if row is None:
            return _no(f"{m.name} is not pulled")
        if _cloud_row(row) and not cloud_allowed(m.id):  # a cloud model by another name
            return _no(CLOUD_WHY.format(id=m.id))
        resident = _matches(m.name, _ollama_names(m, "ps"))
        return {"available": True, "resident": resident, "why": "loaded" if resident else "pulled"}
    auth = {"Authorization": f"Bearer {m.api_key}"} if m.api_key else {}
    try:
        with _open(urllib.request.Request(f"{m.base_url.rstrip('/')}/models", headers=auth), timeout=2) as r:
            body = json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        e.close()
        # a server behind a key answered: it runs, and starting a second copy would only wait
        # 20 minutes for it (the 2026-11-16 audit)
        if e.code in (401, 403):
            return {"available": True, "resident": True,
                    "why": "running (it wants an API key: the provider's options.apiKey)"}
        body = None
    except Exception:
        body = None
    if isinstance(body, dict):
        # one listed model: a one-model server answers to any name; several: it routes by name
        served = {x.get("id") for x in (body.get("data") or []) if isinstance(x, dict)}
        if len(served) > 1 and m.name not in served:
            return _no(f"the server lists {len(served)} models, not {m.name}")
        return {"available": True, "resident": True, "why": "running"}
    if m.start:
        return {"available": True, "resident": False, "why": "stopped (wb starts it)"}
    return _no(f"not running, and no start command in {overrides_path()}")


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
    return 0


def resident() -> list[Model]:
    """The discovered models holding the GPU now."""
    return [m for m in discover().values() if status(m)["resident"]] if not _fake() else []


def _server_idle(m: Model) -> bool:
    live = (_get(f"{m.root}/metrics", timeout=2) or {}).get("live") or {}
    return live.get("state", "idle") == "idle"  # servers without metrics count as idle


def _ollama_loaded(root: str) -> list[str]:
    return [x.get("name") for x in (_get(f"{root}/api/ps") or {}).get("models", [])]


def make_resident(m: Model, say=print, keep=()) -> float:
    """Free the GPU for ``m`` and load it, leaving the models in ``keep`` (ids) loaded. Returns
    the seconds it took (0 if already there)."""
    if _fake() or status(m)["resident"] and (m.remote or not _others_resident(m, keep)):
        return 0.0
    t0 = time.time()
    if not m.remote:
        free_gpu(keep={m.id, *keep}, say=say)
    if m.engine == "ollama":
        say(f"loading {m.name} into ollama")
        _post(f"{m.root}/api/generate", {"model": m.name, "prompt": "", "keep_alive": "30m"},
              timeout=START_TIMEOUT)
    elif not status(m)["resident"]:
        if not m.start:
            raise RuntimeError(f"{m.id} is not running and has no start command")
        say(f"starting {m.id}")
        subprocess.Popen(m.start, shell=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        deadline = time.time() + START_TIMEOUT
        while not status(m)["resident"]:
            if time.time() > deadline:
                raise RuntimeError(f"{m.id} did not come up within {START_TIMEOUT // 60} min")
            time.sleep(5)
    return time.time() - t0


def free_gpu(keep=(), say=print) -> None:
    """Stop every model server and unload every Ollama model holding the GPU, except the models
    in ``keep`` (ids): the task's own, and those of tasks running beside it."""
    if _fake():
        return
    local = [o for o in discover().values() if not o.remote]  # another host's GPU isn't ours to free
    kept = [o for o in local if o.id in keep]
    done = {o.root for o in kept if o.engine == "server"}  # a server swaps its own models
    for other in local:
        if other.engine != "server" or other.root in done or not status(other)["resident"]:
            continue
        done.add(other.root)
        if not other.stop:  # it may be idle with nothing loaded (LM Studio's server often is)
            say(f"{other.id} is running and wb can't stop it (no stop command in "
                f"{overrides_path().name}); it may hold GPU memory")
            continue
        deadline = time.time() + 600
        while not _server_idle(other) and time.time() < deadline:  # never cut off someone's reply
            time.sleep(5)
        say(f"stopping {other.id}")
        try:
            r = subprocess.run(other.stop, shell=True, capture_output=True, text=True, timeout=300)
        except subprocess.TimeoutExpired:
            say(f"{other.id}'s stop command ran past 5 min; it may still hold GPU memory")
            continue
        if r.returncode:
            say(f"{other.id}'s stop command failed (exit {r.returncode}): "
                f"{(r.stderr or r.stdout).strip()[-200:]}")
    roots = {o.root for o in local if o.engine == "ollama"}
    for root in roots:  # every model Ollama has loaded, ours or another tool's
        for name in _ollama_loaded(root):
            if any(o.engine == "ollama" and root == o.root and _matches(o.name, {name}) for o in kept):
                continue
            say(f"unloading {name} from ollama")
            try:
                _post(f"{root}/api/generate", {"model": name, "keep_alive": 0}, timeout=120)
            except Exception:
                pass


def _others_resident(m: Model, keep=()) -> bool:
    local = [o for o in discover().values() if not o.remote]
    ours = [m, *(o for o in local if o.id in keep)]
    if any(o.engine == "server" and o.stop and o.root not in {x.root for x in ours}
           and status(o)["resident"] for o in local):
        return True
    roots = {o.root for o in local if o.engine == "ollama"}
    return any(not any(o.engine == "ollama" and r == o.root and _matches(o.name, {n}) for o in ours)
               for r in roots for n in _ollama_loaded(r))


# ---- sharing the GPU: several tasks' models at once, when they fit ----
# Every runner holds gpu.lock for its whole run: shared while its model runs beside others',
# exclusive when it needs the GPU to itself (its model doesn't fit beside the running ones, or
# can't share). Whatever else needs the whole GPU takes gpu.lock exclusively too, which waits for
# the running tasks and keeps new ones out. gpu.json lists the holders; gpu.admit makes each
# admission, and each load, one decision at a time.
SHARE_AT = 0.9    # of the GPU's memory: the rest is the runtime's and the desktop's
KV_GUESS = 1.25   # a model not loaded yet: its weights plus about a quarter for its context


def gpu_budget() -> int | None:
    """Bytes of GPU memory models can use, or None when the machine can't say (then no sharing).
    Apple Silicon: what macOS lets the GPU wire (by default about three quarters of the RAM)."""
    if _fake():
        gb = os.environ.get("WB_FAKE_GPU_GB")  # tests: a GPU of this size
        return int(float(gb) * 1e9) if gb else None
    total = 0
    for g in hardware.gpus():
        if g.get("unified"):
            total += g.get("wired_limit") or int((g.get("vram") or 0) * 0.75)
        else:
            total += g.get("vram") or 0
    return total or None


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
    re-read their prompts on every request. A model on another host runs alone: its GPU isn't
    the one this machine measures."""
    if not budget or not running or m.engine != "ollama" or m.remote or \
            any(o.engine != "ollama" or o.remote for o in running):
        return False
    if any(o.id == m.id for o in running):
        par = hardware.ollama_env().get("OLLAMA_NUM_PARALLEL") or ""
        return par.isdigit() and int(par) >= 2
    sizes = [need(o) for o in {o.id: o for o in running}.values()] + [need(m)]
    return all(sizes) and sum(sizes) <= SHARE_AT * budget


class GpuLease:
    """A task's hold on the GPU, beside other tasks' models when all fit, else alone. With no
    model (an image generation) it is always alone, and queues in gpu.json like a task, so
    tasks that come after it wait instead of joining the ones it waits for (flock would grant
    them a shared lock while its exclusive one waits). A remote model holds nothing."""

    def __init__(self, m: Model | None, task: str, say=print):
        self.m, self.task, self.say = m, task, say
        self.home = store.home()
        self.fh = None

    def _holders(self) -> dict:
        """gpu.json's live entries: pid → {model, task}."""
        held = store.read_json(self.home / "gpu.json", {}) or {}
        return {p: h for p, h in held.items() if _alive(int(p))}

    def _set(self, held: dict) -> None:
        store.write_json(self.home / "gpu.json", held)

    def __enter__(self):
        if self.m and self.m.remote:
            return self
        self.home.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.home / "gpu.lock", "a+")
        model = self.m.id if self.m else None
        try:
            with store.locked(self.home / "gpu.admit"):
                held = self._holders()
                running = [o for o in (discover().get(h["model"]) for h in held.values()) if o]
                waiting = any(h.get("waiting") for h in held.values())  # first come, first served
                if self.m and not waiting and len(running) == len(held) and (
                        not held or can_share(self.m, running, gpu_budget())):
                    try:
                        fcntl.flock(self.fh, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    except BlockingIOError:
                        pass  # something has the GPU to itself: wait below
                    else:
                        self._set({**held, str(os.getpid()): {"model": model, "task": self.task}})
                        if held:
                            self.say("sharing the GPU with " + ", ".join(sorted({o.id for o in running})))
                        return self
            with store.locked(self.home / "gpu.admit"):
                self._set({**self._holders(), str(os.getpid()): {"model": model, "task": self.task,
                                                                 "waiting": True}})
            fcntl.flock(self.fh, fcntl.LOCK_EX)  # until the running tasks finish; none join us
            with store.locked(self.home / "gpu.admit"):
                self._set({**self._holders(), str(os.getpid()): {"model": model, "task": self.task}})
            return self
        except BaseException:
            self.fh.close()
            raise

    def load(self) -> float:
        """Make the model resident, leaving the other holders' models loaded."""
        if self.m.remote:
            return make_resident(self.m, say=self.say)
        with store.locked(self.home / "gpu.admit"):
            keep = {h["model"] for p, h in self._holders().items()
                    if p != str(os.getpid()) and not h.get("waiting")}
            return make_resident(self.m, say=self.say, keep=keep)

    def __exit__(self, *exc):
        if not self.fh:
            return
        try:
            with store.locked(self.home / "gpu.admit"):
                held = self._holders()
                held.pop(str(os.getpid()), None)
                self._set(held)
        finally:
            self.fh.close()  # releases gpu.lock


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


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
    "problem", "models"} (the config's model entries)."""
    extra = _overrides()
    return {pid: {"root": url.rstrip("/").removesuffix("/v1"), "url": url,
                  "models": prov.get("models") or {}, "engine": engine,
                  "engine_known": engine_known(url, extra.get(pid) or {}),
                  "start": (extra.get(pid) or {}).get("start"),
                  "problem": url_problem(url, engine, prov.get("npm"))}
            for pid, prov, url, engine, _ in _local_providers(read_config(), extra)}


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


def add_ollama_provider(pid: str) -> str | None:
    """Give OpenCode a provider ``pid`` for this machine's Ollama when its config has none by that
    id and Ollama answers; its root, or None. A first run had no provider and no command made one
    (the 2026-11-16 audit). A provider the user wrote is never touched."""
    cfg = read_config()
    if pid in (cfg.get("provider") or {}) or pid in (cfg.get("providers") or {}):
        return None
    root = ollama_root_here()
    if not _fake() and not ollama_answers(root):
        return None
    if "providers" in cfg and "provider" not in cfg:  # OpenCode 2.x's own shape
        cfg["providers"][pid] = {"npm": "@ai-sdk/openai-compatible", "name": "Ollama",
                                 "settings": {"baseURL": f"{root}/v1"}}
    else:
        cfg.setdefault("provider", {})[pid] = {"npm": "@ai-sdk/openai-compatible", "name": "Ollama",
                                               "options": {"baseURL": f"{root}/v1"}}
    write_config(cfg)
    return root


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


def ollama_show(root: str, name: str) -> dict:
    """Ollama's own facts about a pulled model: its capabilities, trained context length, and
    the num_ctx its Modelfile sets, if any."""
    if _fake():
        return {"capabilities": ["tools", "thinking"], "context_length": None, "num_ctx": None}
    body = _post(f"{root}/api/show", {"model": name}, timeout=30)
    ctx = next((v for k, v in (body.get("model_info") or {}).items()
                if k.endswith(".context_length")), None)
    num = next((int(p.split()[1]) for p in (body.get("parameters") or "").splitlines()
                if p.split()[:1] == ["num_ctx"]), None)
    return {"capabilities": body.get("capabilities") or [], "context_length": ctx, "num_ctx": num}


def ollama_served_context(root: str, name: str) -> int | None:
    """The window Ollama gives this model over OpenCode's API, which can't ask for one: what it
    runs with if loaded, else its Modelfile's num_ctx, else OLLAMA_CONTEXT_LENGTH. None when only
    Ollama's own default would say (it varies by version and VRAM)."""
    if _fake():
        return None
    for x in (_get(f"{root}/api/ps") or {}).get("models", []):
        if _matches(name, {x.get("name")}) and x.get("context_length"):
            return int(x["context_length"])
    try:
        num = ollama_show(root, name).get("num_ctx")
    except Exception:
        num = None
    env = hardware.ollama_env().get("OLLAMA_CONTEXT_LENGTH")
    return num or (int(env) if env and env.isdigit() else None)


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
                    say(f"{status}: {pct}% of {total / 1024 ** 3:.1f} GB")
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
    path = overrides_path()
    data = _overrides()
    entry = data.setdefault(model_id, {})
    if value is None:
        entry.pop(key, None)
    else:
        entry[key] = value
    if not entry:
        data.pop(model_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    store.write_json(path, data)


def write_entry(provider: str, name: str, entry: dict | None) -> None:
    """Add, replace (entry) or remove (None) one model in OpenCode's config."""
    cfg = read_config()
    prov = (cfg.get("provider") or cfg.get("providers") or {}).get(provider)
    if prov is None:
        raise RuntimeError(f"OpenCode has no provider {provider!r}")
    entries = prov.setdefault("models", {})
    if entry is None:
        entries.pop(name, None)
    else:  # keys added by hand (variants, options) stay
        old = entries.get(name) or {}
        entries[name] = {**old, **entry, "options": {**(old.get("options") or {}), **(entry.get("options") or {})}}
        if not entries[name]["options"]:
            del entries[name]["options"]
    write_config(cfg)
