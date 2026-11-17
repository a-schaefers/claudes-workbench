"""The local models workbench can hand work to: found in OpenCode's config, not listed here.

Every model of an OpenCode provider whose baseURL is on this machine is a candidate (a provider
on another host, such as a GPU box on the LAN, opts in with `"local": true`). Which one
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
(`remote`) is left out of all of it: this machine's GPU is not the one it runs on, so it takes no
lease (tasks on one remote server run at once, and that server schedules them), frees nothing
here, and nothing here unloads or stops it.
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
START_GRACE = 10  # after a start command fails: another process may be bringing the server up


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

    @property
    def root(self) -> str:
        return self.base_url.rstrip("/").removesuffix("/v1")

    @property
    def server(self) -> tuple:
        """The server behind the baseURL, one key however its host is spelled: localhost,
        127.0.0.1 and this machine's own name or address are one server. Compared as strings,
        two spellings of one Ollama made free_gpu unload the models it was told to keep (the
        2026-11-16 audit). No DNS lookup: this runs on every admission."""
        u = urlparse(self.base_url)
        here = not self.remote and _this_host(u.hostname)
        return ("this host" if here else (u.hostname or "").lower(),
                u.port or (443 if u.scheme == "https" else 80),
                u.path.rstrip("/").removesuffix("/v1"))


def opencode_config_path() -> Path:
    """OpenCode's global config, where OpenCode reads it: $XDG_CONFIG_HOME/opencode (default
    ~/.config/opencode, on macOS too), opencode.json or opencode.jsonc."""
    if os.environ.get("WB_OPENCODE_CONFIG"):
        return Path(os.environ["WB_OPENCODE_CONFIG"])
    d = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "opencode"
    return next((p for p in (d / "opencode.json", d / "opencode.jsonc") if p.exists()),
                d / "opencode.json")


def _strip_jsonc(text: str) -> str:
    """JSON with comments and trailing commas (OpenCode accepts both) as plain JSON."""
    text = re.sub(r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*.*?\*/',
                  lambda m: m.group(0) if m.group(0)[0] == '"' else "", text, flags=re.S)
    return re.sub(r'"(?:\\.|[^"\\])*"|,(\s*[}\]])',
                  lambda m: m.group(1) if m.group(1) is not None else m.group(0), text)


def read_config() -> dict:
    path = opencode_config_path()
    return json.loads(_strip_jsonc(path.read_text())) if path.exists() else {}


def write_config(cfg: dict) -> None:
    """Write OpenCode's config back; refused for a file with comments, which a rewrite would lose."""
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


def _engine(url: str, over: dict) -> str:
    """"ollama" when the server answers Ollama's own /api/version, else "server". While it is
    down, Ollama's default port decides; the answer is kept once the server has given one."""
    if over.get("engine") in ("ollama", "server"):
        return over["engine"]
    root = url.rstrip("/").removesuffix("/v1")
    if root in _ENGINES:
        return _ENGINES[root]
    guess = "ollama" if urlparse(url).port == OLLAMA_PORT else "server"
    if _fake():
        return guess
    try:
        with urllib.request.urlopen(f"{root}/api/version", timeout=1.5) as r:
            body = json.loads(r.read() or b"{}")
        _ENGINES[root] = "ollama" if isinstance(body, dict) and "version" in body else "server"
    except urllib.error.HTTPError:  # it answered, without Ollama's API
        _ENGINES[root] = "server"
    except Exception:  # down, or not speaking JSON: the port is the best guess
        return guess
    return _ENGINES[root]


def _local_providers(cfg: dict, extra: dict):
    """(provider id, its config, its baseURL, engine, remote) for each provider on this machine,
    or opted in with models.json "local": true."""
    for pid, prov in (cfg.get("provider") or cfg.get("providers") or {}).items():
        opts = prov.get("options") or prov.get("settings") or {}
        url = opts.get("baseURL", "")
        here = _this_host(urlparse(url).hostname)
        if not url or not (here or (extra.get(pid) or {}).get("local")):
            continue
        yield pid, prov, url, _engine(url, extra.get(pid) or {}), not here


def discover() -> dict[str, Model]:
    """Every local model OpenCode knows, by OpenCode id, in config order."""
    extra = _overrides()
    found: dict[str, Model] = {}
    for pid, prov, url, engine, remote in _local_providers(read_config(), extra):
        for name, spec in (prov.get("models") or {}).items():
            mid = f"{pid}/{name}"
            over = {**extra.get(pid, {}), **extra.get(mid, {})}
            if over.get("skip"):
                continue
            found[mid] = Model(id=mid, provider=pid, name=name, base_url=url, engine=engine,
                               label=over.get("label") or spec.get("name") or mid,
                               start=over.get("start"), stop=over.get("stop"), extra=over,
                               config=spec, remote=remote)
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
    known = ", ".join(models) or "none (no local provider in opencode.json)"
    if hits:
        raise KeyError(f"{wanted!r} matches {', '.join(m.id for m in hits)}; be more specific")
    raise KeyError(f"unknown model {wanted!r}; known: {known}")


def _get(url: str, timeout: float = 2.5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except Exception:
        return None


def _post(url: str, body: dict, timeout: float = 600):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
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


def _serves(served: str, name: str) -> bool:
    """Whether a one-model server's id is this model: llama-server lists the file's path."""
    base = lambda s: re.sub(r"\.gguf$", "", s.rsplit("/", 1)[-1], flags=re.I).lower()  # noqa: E731
    return served == name or base(served) == base(name)


def status(m: Model) -> dict:
    """``{available, resident, why}``: can the runner get this model onto the GPU, is it there."""
    if _fake():
        return {"available": True, "resident": True, "why": "fake"}
    if m.engine == "ollama":
        if _get(f"{m.root}/api/tags") is None:
            return {"available": False, "resident": False, "why": "ollama is not answering"}
        if not _matches(m.name, _ollama_names(m, "tags")):
            return {"available": False, "resident": False, "why": f"{m.name} is not pulled"}
        resident = _matches(m.name, _ollama_names(m, "ps"))
        return {"available": True, "resident": resident, "why": "loaded" if resident else "pulled"}
    body = _get(f"{m.base_url.rstrip('/')}/models", timeout=2)
    if body is not None:
        # one listed model: a one-model server answers to any name; several: it routes by name
        served = {x.get("id") for x in (body.get("data") or []) if isinstance(x, dict)}
        if len(served) > 1 and m.name not in served:
            return {"available": False, "resident": False,
                    "why": f"the server lists {len(served)} models, not {m.name}"}
        if len(served) == 1 and m.start and _contended(m) and not _serves(str(next(iter(served))), m.name):
            # the task would run on the other model, recorded under this one (the 2026-11-16 audit)
            return {"available": False, "resident": False,
                    "why": f"{m.root} is serving {next(iter(served))}, not {m.name}, and other models "
                           "with their own start commands share that port, so wb can't tell or switch "
                           "them: give each its own port (or --alias it as its name), or use one "
                           "server that swaps models (llama-swap)"}
        return {"available": True, "resident": True, "why": "running"}
    if m.start:
        return {"available": True, "resident": False, "why": "stopped (wb starts it)"}
    return {"available": False, "resident": False,
            "why": f"not running, and no start command in {overrides_path()}"}


def weights(m: Model) -> int:
    """Bytes of weights behind the id, the router's measure of a model's size; 0 when unknown.
    models.json's `weights_gb` says it for a server that can't (or a test)."""
    if m.extra.get("weights_gb"):
        return int(float(m.extra["weights_gb"]) * 1e9)
    if m.engine == "ollama" and not _fake():
        for x in (_get(f"{m.root}/api/tags") or {}).get("models", []):
            if x.get("name") in (m.name, f"{m.name}:latest"):
                return int(x.get("size") or 0)
    return 0


def resident() -> list[Model]:
    """The discovered models holding the GPU now."""
    return [m for m in discover().values() if status(m)["resident"]] if not _fake() else []


def releasable(m: Model) -> bool:
    """Whether free_gpu can let go of the model: a local Ollama one, or a local server with a
    stop command (a server without one, or another host's model, stays up)."""
    return not m.remote and (m.engine == "ollama" or bool(m.stop))


def _server_idle(m: Model) -> bool:
    live = (_get(f"{m.root}/metrics", timeout=2) or {}).get("live") or {}
    return live.get("state", "idle") == "idle"  # servers without metrics count as idle


def _ollama_loaded(root: str) -> list[str]:
    return [x.get("name") for x in (_get(f"{root}/api/ps") or {}).get("models", [])]


def make_resident(m: Model, say=print, keep=(), warn=None) -> float:
    """Free the GPU for ``m`` and load it, leaving the models in ``keep`` (ids) loaded. Returns
    the seconds it took (0 if already there). ``warn`` gets what Claude should hear about later
    (a server wb can't stop, a model partly on the CPU); it defaults to ``say``."""
    warn = warn or say
    if _fake():
        return 0.0
    st = status(m)
    if st["resident"] and (m.remote or not _others_resident(m, keep)):
        return 0.0
    if m.engine == "ollama" and not st["available"]:  # before free_gpu unloads anything for it
        raise RuntimeError(f"{m.id}: {st['why']}")
    t0 = time.time()
    if not m.remote:
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
        began, failed_at = time.time(), None
        deadline = began + START_TIMEOUT
        while not (now := status(m))["resident"]:
            if not now["available"] and time.time() >= began + START_GRACE:
                raise RuntimeError(f"{m.id}: {now['why']}")  # it answers as something else
            # a start command that fails at once used to be waited on for 20 min (the 2026-11-16
            # audit); exit 0 is a start that daemonizes (systemctl start, docker start, cmd &)
            if failed_at is None and p.poll():
                failed_at = time.time()
            if failed_at and time.time() >= failed_at + START_GRACE:
                raise RuntimeError(f"{m.id}'s start command exited {p.returncode} and nothing "
                                   f"answers; run it by hand to see why: {m.start}")
            if time.time() > deadline:
                raise RuntimeError(f"{m.id} did not come up within {START_TIMEOUT // 60} min")
            try:
                p.wait(timeout=1 if failed_at else 5)  # returns at once when the command exits
            except subprocess.TimeoutExpired:
                pass
    return time.time() - t0


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


def free_gpu(keep=(), say=print, warn=None) -> None:
    """Stop every model server and unload every Ollama model holding the GPU, except the models
    in ``keep`` (ids): the task's own, and those of tasks running beside it."""
    warn = warn or say
    if _fake():
        return
    local = [o for o in discover().values() if not o.remote]  # another host's GPU isn't ours to free
    kept = [o for o in local if o.id in keep]
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
    for key, root in _roots(o for o in local if o.engine == "ollama").items():
        for name in _ollama_loaded(root):  # every model Ollama has loaded, ours or another tool's
            if any(o.server == key and _matches(o.name, {name}) for o in kept_ollama):
                continue
            say(f"unloading {name} from ollama")
            try:
                _post(f"{root}/api/generate", {"model": name, "keep_alive": 0}, timeout=120)
            except Exception:
                pass


def _others_resident(m: Model, keep=()) -> bool:
    local = [o for o in discover().values() if not o.remote]
    ours = [m, *(o for o in local if o.id in keep)]
    if any(o.engine == "server" and o.stop and o.server not in {x.server for x in ours}
           and status(o)["resident"] for o in local):
        return True
    return any(not any(o.engine == "ollama" and o.server == key and _matches(o.name, {n}) for o in ours)
               for key, root in _roots(o for o in local if o.engine == "ollama").items()
               for n in _ollama_loaded(root))


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
OLLAMA_MAX_LOADED = 3  # Ollama's own default (3 per GPU) when OLLAMA_MAX_LOADED_MODELS is unset


def unified_default(ram: int) -> int:
    """What macOS lets the GPU wire of ``ram`` bytes of unified memory, by default."""
    return ram * 2 // 3 if ram <= UNIFIED_SMALL else ram * 3 // 4


def budget_devices() -> tuple[int | None, list[dict]]:
    """Bytes of GPU memory models can use (None when the machine can't say: then no sharing),
    and the devices that make it up. One runtime serves a model, so it's the largest group of
    one driver's GPUs (several NVIDIA cards, which Ollama splits a model across, add up; an
    iGPU of another vendor beside them doesn't), leaving out devices under 1 GiB (an integrated
    GPU's carve-out). Summing every GPU counted a laptop's Radeon iGPU into its RTX card's 8 GB
    (the 2026-11-16 audit). Apple Silicon: iogpu.wired_limit_mb, else macOS's default share."""
    if _fake():
        gb = os.environ.get("WB_FAKE_GPU_GB")  # tests: a GPU of this size
        return (int(float(gb) * 1e9), []) if gb else (None, [])
    groups: dict[str, list] = {}
    for g in hardware.gpus(windows=False):  # WSL's Windows names carry no VRAM: skip PowerShell
        if g.get("unified"):
            usable = g.get("wired_limit") or unified_default(g.get("vram") or 0)
        else:
            usable = g.get("vram") or 0
            if usable < IGPU_UNDER:
                continue
        if usable:
            groups.setdefault((g.get("driver") or "?").split()[0], []).append((usable, g))
    if not groups:
        return None, []
    best = max(groups.values(), key=lambda devs: sum(n for n, _ in devs))
    return sum(n for n, _ in best), [g for _, g in best]


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
    if any(_key(o) == _key(m) for o in running):
        par = env.get("OLLAMA_NUM_PARALLEL") or ""
        return par.isdigit() and int(par) >= 2
    distinct = {_key(o): o for o in running}
    most = env.get("OLLAMA_MAX_LOADED_MODELS") or ""  # 0 or unset: Ollama's default
    if len(distinct) + 1 > (int(most) if most.isdigit() and int(most) else OLLAMA_MAX_LOADED):
        return False
    sizes = [need(o) for o in distinct.values()] + [need(m)]
    return all(sizes) and sum(sizes) <= SHARE_AT * budget


def admits(m: Model | None, held: dict) -> bool:
    """Whether ``m`` may take the GPU now beside the live holders ``held`` (gpu.json's entries):
    nobody waits ahead of it (first come, first served), every holder's model is known, and they
    all fit together (can_share). Nothing (an image, ``m`` None) joins anyone."""
    if not held:
        return m is not None
    if m is None or any(h.get("waiting") for h in held.values()):
        return False
    found = discover()
    running = [o for o in (found.get(h.get("model") or "") for h in held.values()) if o]
    return len(running) == len(held) and can_share(m, running, gpu_budget())


def holders(home: Path | None = None) -> dict:
    """gpu.json's live entries: pid → {model, task, start, waiting?}."""
    held = store.read_json((home or store.home()) / "gpu.json", {}) or {}
    return {p: h for p, h in held.items() if p.isdigit() and _alive(int(p), h.get("start"))}


def _fits_beside(m: Model, keep: set) -> bool:
    """After a shared load: Ollama kept every other holder's model and has all of ``m`` in VRAM.
    need() guesses a model's context before it loads; when the guess is short, Ollama evicts a
    model or splits one onto the CPU, and two tasks then reload their models on every request
    (the 2026-11-16 audit). Unknown (Ollama didn't answer) counts as fitting."""
    if _fake() or m.engine != "ollama":
        return True
    body = _get(f"{m.root}/api/ps")
    if body is None:
        return True
    ps = body.get("models") or []
    names = {x.get("name") for x in ps}
    found = discover()
    others = [o for o in (found.get(k) for k in keep) if o and o.server == m.server]
    whole = any(_matches(m.name, {x.get("name")}) and x.get("size") and x.get("size_vram") == x["size"]
                for x in ps)
    return whole and all(_matches(o.name, names) for o in others)


class GpuLease:
    """A task's hold on the GPU, beside other tasks' models when all fit, else alone. With no
    model (an image generation) it is always alone, and queues in gpu.json like a task, so
    tasks that come after it wait instead of joining the ones it waits for (flock would grant
    them a shared lock while its exclusive one waits); with ``wait`` False it refuses at once
    when anything holds the GPU. A remote model holds nothing."""

    def __init__(self, m: Model | None, task: str, say=print, wait: bool = True):
        self.m, self.task, self.say, self.wait = m, task, say, wait
        self.home = store.home()
        self.fh = None
        self.me = str(os.getpid())

    def _holders(self) -> dict:
        return holders(self.home)

    def _set(self, held: dict) -> None:
        store.write_json(self.home / "gpu.json", held)

    def _entry(self, waiting: bool = False) -> dict:
        # the start time tells this runner from a later process given its pid (after a reboot)
        return {"model": self.m.id if self.m else None, "task": self.task,
                "start": _started(os.getpid()), **({"waiting": True} if waiting else {})}

    def warn(self, msg: str) -> None:
        """What Claude should see later, not only in status.last (overwritten within moments):
        also a `gpu` event in the task's transcript."""
        self.say(msg)
        task = store.Task(self.task)
        if task.exists:
            task.event({"ts": store.now(), "round": task.status.get("round", 1), "kind": "gpu",
                        "text": msg})

    def __enter__(self):
        if self.m and self.m.remote:
            return self
        self.home.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.home / "gpu.lock", "a+")
        try:
            with store.locked(self.home / "gpu.admit"):
                held = self._holders()
                if held and self.m is None and not self.wait:
                    raise RuntimeError(busy(held, "wb image needs it to itself. Run it once they "
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
                        return self
                self._set({**held, self.me: self._entry(waiting=True)})
            self._take_alone(held)
            return self
        except BaseException:
            self._leave()  # a stop while queued used to leave a waiting entry behind (the
            raise          # 2026-11-16 audit), which made every later task wait alone

    def _take_alone(self, held: dict) -> None:
        """Wait for the GPU to itself (marked waiting in gpu.json, so nobody joins the holders
        meanwhile), saying who holds it: the runner's status.last, the pane's line."""
        others = sorted({h["task"] for p, h in held.items() if p != self.me})
        if others:  # a queued task used to wait in silence, its holder unnamed (the 2026-11-16 audit)
            self.say(f"waiting for the GPU (held by {', '.join(others)})")
        fcntl.flock(self.fh, fcntl.LOCK_EX)  # until the running tasks finish; none join us
        with store.locked(self.home / "gpu.admit"):
            # nobody else can hold gpu.lock now: any other non-waiting entry is stale
            self._set({**{p: h for p, h in self._holders().items() if h.get("waiting")},
                       self.me: self._entry()})

    def load(self) -> float:
        """Make the model resident, leaving the other holders' models loaded. Loads take turns
        under gpu.load, never under gpu.admit: a task finishing beside a long load used to wait
        for it to leave the lease, so its runner looked alive after its review notice (the
        2026-11-16 audit). Lock order: gpu.load, then gpu.admit."""
        if self.m.remote:
            return make_resident(self.m, say=self.say, warn=self.warn)
        with store.locked(self.home / "gpu.load"):
            with store.locked(self.home / "gpu.admit"):
                held = self._holders()
                keep = {h["model"] for p, h in held.items()
                        if p != self.me and not h.get("waiting") and h.get("model")}
            secs = make_resident(self.m, say=self.say, keep=keep, warn=self.warn)
            if not keep or _fits_beside(self.m, keep):
                return secs
            self.warn(f"ollama could not fit {self.m.id} beside {', '.join(sorted(keep))}; "
                      "waiting for the GPU to itself")
            _unload(self.m)  # or the holders' next requests swap it in and out
            with store.locked(self.home / "gpu.admit"):
                held = self._holders()
                self._set({**held, self.me: self._entry(waiting=True)})
        self._take_alone(held)  # flock converts: drops the shared lock, then waits for the others
        with store.locked(self.home / "gpu.load"):
            return secs + make_resident(self.m, say=self.say, warn=self.warn)

    def _leave(self) -> None:
        if not self.fh:
            return
        try:
            with store.locked(self.home / "gpu.admit"):
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


def _started(pid: int) -> str | None:
    """When the process started (/proc/<pid>/stat's 22nd field); None without /proc."""
    try:
        return Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()[19]
    except (OSError, IndexError):
        return None


def _alive(pid: int, start: str | None = None) -> bool:
    """Whether the holder is still running: its pid is this user's (a recycled pid of another
    user's is not), and on Linux it started when the holder did (a recycled pid of ours didn't).
    Without /proc, or for an entry without `start`, the pid alone decides."""
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
    """OpenCode's local providers: id → {"root", "engine", "models"} (the config's model entries)."""
    return {pid: {"root": url.rstrip("/").removesuffix("/v1"), "models": prov.get("models") or {},
                  "engine": engine}
            for pid, prov, url, engine, _ in _local_providers(read_config(), _overrides())}


def ollama_installed(root: str) -> list[dict]:
    """What an Ollama has pulled: name, bytes on disk, parameters, quantization."""
    if _fake():
        return []
    return [{"name": x.get("name"), "size": x.get("size"),
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
    with urllib.request.urlopen(req, timeout=24 * 3600) as r:
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
    urllib.request.urlopen(req, timeout=60).close()


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
