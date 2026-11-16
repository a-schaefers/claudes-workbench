"""The local models workbench can hand work to: found in OpenCode's config, not listed here.

Every model of an OpenCode provider whose baseURL is on this machine is a candidate. What each
model is good at is measured (`wb bench`) and learned from real tasks (router.py), never written
down here, so a new model is one opencode.json entry and one `wb bench` away.

``~/.config/workbench/models.json`` (or ``$WB_MODELS``) adds what OpenCode's config can't say,
keyed by OpenCode model id or by provider id:

    {"strata": {"start": "strata launch --preset coder --no-tui --yes",
                "stop": "strata launch --stop --preset coder"},
     "ollama/some-model": {"skip": true}}

The runner keeps one model on the GPU at a time (a consumer card fits one coding model), so it
makes the task's model resident before it starts (make_resident): it unloads Ollama models and stops servers that have
a stop command, then loads or starts the one it needs.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from . import store

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}
OLLAMA_PORT = 11434
START_TIMEOUT = 20 * 60  # Strata's first start can take minutes


@dataclass
class Model:
    id: str            # the OpenCode model id, "provider/model": the one name used everywhere
    provider: str
    name: str          # the model's name at its server
    base_url: str      # the provider's OpenAI-compatible base URL, ending in /v1
    engine: str        # "ollama" (loads models on demand) or "server" (serves one model)
    label: str
    start: str | None = None
    stop: str | None = None
    extra: dict = field(default_factory=dict)
    config: dict = field(default_factory=dict)  # OpenCode's entry: reasoning, limit, variants...

    @property
    def root(self) -> str:
        return self.base_url.rstrip("/").removesuffix("/v1")


def opencode_config_path() -> Path:
    return Path(os.environ.get("WB_OPENCODE_CONFIG")
                or Path.home() / ".config" / "opencode" / "opencode.json")


def _overrides() -> dict:
    path = Path(os.environ.get("WB_MODELS") or Path.home() / ".config" / "workbench" / "models.json")
    return json.loads(path.read_text()) if path.exists() else {}


def discover() -> dict[str, Model]:
    """Every local model OpenCode knows, by OpenCode id, in config order."""
    path = opencode_config_path()
    cfg = json.loads(path.read_text()) if path.exists() else {}
    providers = cfg.get("provider") or cfg.get("providers") or {}
    extra = _overrides()
    found: dict[str, Model] = {}
    for pid, prov in providers.items():
        opts = prov.get("options") or prov.get("settings") or {}
        url = opts.get("baseURL", "")
        parsed = urlparse(url)
        if parsed.hostname not in LOCAL_HOSTS:
            continue
        engine = "ollama" if parsed.port == OLLAMA_PORT else "server"
        for name, spec in (prov.get("models") or {}).items():
            mid = f"{pid}/{name}"
            over = {**extra.get(pid, {}), **extra.get(mid, {})}
            if over.get("skip"):
                continue
            found[mid] = Model(id=mid, provider=pid, name=name, base_url=url, engine=engine,
                               label=over.get("label") or spec.get("name") or mid,
                               start=over.get("start"), stop=over.get("stop"), extra=over,
                               config=spec)
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
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def _fake() -> bool:
    return bool(os.environ.get("WB_FAKE_MODELS"))  # tests drive a fake opencode, no servers


def _ollama_names(m: Model, endpoint: str) -> set[str]:
    body = _get(f"{m.root}/api/{endpoint}") or {}
    return {x.get("name") for x in body.get("models", [])}


def _matches(name: str, names: set[str]) -> bool:
    return name in names or f"{name}:latest" in names


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
    if _get(f"{m.base_url.rstrip('/')}/models", timeout=2) is not None:
        return {"available": True, "resident": True, "why": "running"}
    if m.start:
        return {"available": True, "resident": False, "why": "stopped (wb starts it)"}
    return {"available": False, "resident": False,
            "why": "not running, and no start command in ~/.config/workbench/models.json"}


def fingerprint(m: Model) -> str | None:
    """What identifies the weights behind the id, so a re-pulled or swapped model is re-measured."""
    if _fake():
        return "fake"
    if m.engine == "ollama":
        for x in (_get(f"{m.root}/api/tags") or {}).get("models", []):
            if x.get("name") in (m.name, f"{m.name}:latest"):
                return x.get("digest", "")[:16]
        return None
    body = _get(f"{m.base_url.rstrip('/')}/models", timeout=2) or {}
    ids = sorted(x.get("id", "") for x in body.get("data", []))
    return ",".join(ids) or None


def resident() -> list[Model]:
    """The discovered models holding the GPU now."""
    return [m for m in discover().values() if status(m)["resident"]] if not _fake() else []


def _server_idle(m: Model) -> bool:
    live = (_get(f"{m.root}/metrics", timeout=2) or {}).get("live") or {}
    return live.get("state", "idle") == "idle"  # servers without metrics count as idle


def _ollama_loaded(root: str) -> list[str]:
    return [x.get("name") for x in (_get(f"{root}/api/ps") or {}).get("models", [])]


def make_resident(m: Model, say=print) -> float:
    """Free the GPU for ``m`` and load it. Returns the seconds it took (0 if already there)."""
    if _fake() or status(m)["resident"] and not _others_resident(m):
        return 0.0
    t0 = time.time()
    for other in discover().values():
        if other.id == m.id or other.engine != "server" or not status(other)["resident"]:
            continue
        if not other.stop:
            raise RuntimeError(f"{other.id} holds the GPU and has no stop command; stop it first")
        deadline = time.time() + 600
        while not _server_idle(other) and time.time() < deadline:  # never cut off someone's reply
            time.sleep(5)
        say(f"stopping {other.id}")
        subprocess.run(other.stop, shell=True, capture_output=True, timeout=300)
    roots = {o.root for o in discover().values() if o.engine == "ollama"}
    for root in roots:  # every model Ollama has loaded, ours or another tool's
        for name in _ollama_loaded(root):
            if m.engine == "ollama" and root == m.root and _matches(m.name, {name}):
                continue
            say(f"unloading {name} from ollama")
            try:
                _post(f"{root}/api/generate", {"model": name, "keep_alive": 0}, timeout=120)
            except Exception:
                pass
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


def _others_resident(m: Model) -> bool:
    if any(o.id != m.id and o.engine == "server" and status(o)["resident"]
           for o in discover().values()):
        return True
    roots = {o.root for o in discover().values() if o.engine == "ollama"}
    return any(not (m.engine == "ollama" and r == m.root and _matches(m.name, {n}))
               for r in roots for n in _ollama_loaded(r))


# ---- managing the set: what Claude's weekly model check uses to add and retire models ----

def providers() -> dict[str, dict]:
    """OpenCode's local providers: id → {"root", "engine", "models"} (the config's model entries)."""
    path = opencode_config_path()
    cfg = json.loads(path.read_text()) if path.exists() else {}
    out = {}
    for pid, prov in (cfg.get("provider") or {}).items():
        url = (prov.get("options") or {}).get("baseURL", "")
        parsed = urlparse(url)
        if parsed.hostname in LOCAL_HOSTS:
            out[pid] = {"root": url.rstrip("/").removesuffix("/v1"), "models": prov.get("models") or {},
                        "engine": "ollama" if parsed.port == OLLAMA_PORT else "server"}
    return out


def ollama_installed(root: str) -> list[dict]:
    """What an Ollama has pulled: name, bytes on disk, parameters, quantization."""
    if _fake():
        return []
    return [{"name": x.get("name"), "size": x.get("size"),
             "params": (x.get("details") or {}).get("parameter_size"),
             "quant": (x.get("details") or {}).get("quantization_level")}
            for x in (_get(f"{root}/api/tags") or {}).get("models", [])]


def ollama_show(root: str, name: str) -> dict:
    """Ollama's own facts about a pulled model: its capabilities and trained context length."""
    if _fake():
        return {"capabilities": ["tools", "thinking"], "context_length": None}
    body = _post(f"{root}/api/show", {"model": name}, timeout=30)
    ctx = next((v for k, v in (body.get("model_info") or {}).items()
                if k.endswith(".context_length")), None)
    return {"capabilities": body.get("capabilities") or [], "context_length": ctx}


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
    path = Path(os.environ.get("WB_MODELS") or Path.home() / ".config" / "workbench" / "models.json")
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
    path = opencode_config_path()
    cfg = json.loads(path.read_text())
    prov = (cfg.get("provider") or {}).get(provider)
    if prov is None:
        raise KeyError(f"OpenCode has no provider {provider!r}")
    entries = prov.setdefault("models", {})
    if entry is None:
        entries.pop(name, None)
    else:
        entries[name] = entry
    store.write_json(path, cfg)
