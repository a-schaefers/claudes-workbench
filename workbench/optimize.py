"""wb optimize: measuring this machine for its local models, when the user asks (through Claude),
never on its own: an automatic run could be cut short and alarm people (the user, 2026-11-18).
Everything else learns passively from real tasks (the router, tuning.py, speed.json); this is the
part that has to load a model to know.

What it measures, for each Ollama model: the largest window that keeps the model and its context
all in VRAM, by Ollama's own placement (/api/ps `size_vram` against `size`) after a load at each
window tried. On 2026-11-18 a 64K window that put a tenth of a 27B model in system RAM wrote at
~17-21 tok/s where its 32K window, all on the GPU, wrote at ~37; the bigger window still spared
Claude review rounds. Where the line falls depends on the card, the model and the server, so it is
measured here, and Claude and the user choose with both facts in hand.

It holds the GPU alone (a GpuLease with no model) and runs detached, so it outlives Claude's
session; a real task that wants the GPU stops it (cli._preempt_bench), and what it measured is
kept. Nothing it does wakes Claude.
"""
from __future__ import annotations

import math
import os
import re
import signal
import statistics
import subprocess
import sys
import time
from pathlib import Path

from . import models, store

HOLDER = "wb optimize"   # its name in gpu.json, which tells real work it may stop it
STEP = 4096              # windows tried are multiples of this
LOAD_GUESS = 45          # seconds a load takes when no task on the model has measured one
MAX_WINDOW = 131072      # never asked past this: a long context's cache could fill system RAM
PKG_ROOT = Path(__file__).resolve().parent.parent


class Stopped(Exception):
    pass


def path() -> Path:
    return store.home() / "optimize.json"


def state() -> dict:
    return store.read_json(path(), {}) or {}


def candidates() -> list[models.Model]:
    return [m for m in models.discover().values()
            if m.engine == "ollama" and not m.remote and models.status(m)["available"]]


def served(m: models.Model) -> int:
    return (m.config.get("limit") or {}).get("context") or models.MIN_WINDOW


def load_secs(m: models.Model) -> float:
    """How long a load of this model takes here: the median its tasks measured, else a guess."""
    secs = [t.status["load_secs"] for t in store.all_tasks()
            if t.spec.get("model") == m.id and t.status.get("load_secs")]
    return statistics.median(secs) if secs else LOAD_GUESS


def loads(m: models.Model) -> int:
    """The loads a fit takes at most: the served window, one end of the range, then halving."""
    span = max(served(m) - models.MIN_WINDOW, min(served(m), MAX_WINDOW - served(m)))
    return 2 + max(0, math.ceil(math.log2(max(1, span / STEP))))


def estimate(ms: list[models.Model]) -> float:
    return sum(loads(m) * (load_secs(m) + 5) for m in ms)


def probe(m: models.Model, window: int) -> dict:
    """Load ``m`` at ``window`` and read where Ollama put it: {window, size, vram}."""
    models._unload(m)
    try:
        models._post(f"{m.root}/api/generate", {"model": m.name, "prompt": "", "keep_alive": "2m",
                                                "options": {"num_ctx": window}}, timeout=900)
        for x in (models._get(f"{m.root}/api/ps") or {}).get("models", []):
            if models._matches(m.name, {x.get("name")}) and x.get("size"):
                return {"window": window, "size": x["size"], "vram": x.get("size_vram") or 0}
        raise RuntimeError(f"Ollama loaded {m.name} but doesn't list it")
    finally:
        models._unload(m)


def fit(m: models.Model, trained: int | None, check=lambda: None) -> dict:
    """The largest window (a multiple of STEP) at which ``m`` is all in VRAM here, searching
    below the served window when it spills, else above it up to twice it (and what it was
    trained on). ``check`` runs between loads: it raises Stopped when real work wants the GPU."""
    tried: list[dict] = []

    def whole(w: int) -> bool:
        check()
        r = probe(m, w)
        tried.append(r)
        return r["vram"] >= r["size"]

    now = served(m)
    top = min(trained or MAX_WINDOW, 2 * now, MAX_WINDOW) // STEP * STEP
    if whole(now):
        good, bad = now, None
        if top > now:
            if whole(top):
                good = top
            else:
                bad = top
    elif now <= models.MIN_WINDOW or not whole(models.MIN_WINDOW):
        good, bad = None, None
    else:
        good, bad = models.MIN_WINDOW, now
    while good and bad and bad - good > STEP:
        mid = (good + bad) // 2 // STEP * STEP
        if mid in (good, bad):
            break
        good, bad = (mid, bad) if whole(mid) else (good, mid)
    at = next((r for r in tried if r["window"] == now), None)
    return {"window": now, "fits": good, "spill": round(100 * (1 - at["vram"] / at["size"]))
            if at and at["size"] else None, "tried": tried, "ts": store.now()}


def _record(model_id: str, row: dict) -> None:
    st = state()
    store.write_json(path(), {**st, "models": {**(st.get("models") or {}), model_id: row}})


def advice(model_id: str, row: dict) -> str:
    """One line on what the fit says, or "" when there is nothing to change."""
    w, fits, spill = row.get("window"), row.get("fits"), row.get("spill")
    k = lambda n: f"{n // 1024}K"  # noqa
    if fits is None:
        return f"{model_id}: doesn't fit the GPU whole even at {k(models.MIN_WINDOW)}"
    if spill:
        return (f"{model_id}: its {k(w)} window puts {spill}% of it in system RAM here (slower); "
                f"{k(fits)} fits all on the GPU. A bigger window compacts less, a whole one writes "
                f"faster: wb optimize apply {model_id} makes a {k(fits)} entry beside it")
    if fits > w:
        return (f"{model_id}: {k(fits)} fits all on the GPU here, more than the {k(w)} it serves: "
                f"wb optimize apply {model_id} makes a {k(fits)} entry beside it")
    return ""


def apply(m: models.Model) -> str:
    """Make the measured window an entry of its own beside ``m``: an Ollama model with that
    num_ctx (from ``m``, no download) and OpenCode's entry, copied from ``m``'s (its options,
    such as thinking off, and output cap kept), with the new window."""
    row = (state().get("models") or {}).get(m.id) or {}
    fits = row.get("fits")
    if not fits or fits == row.get("window"):
        raise RuntimeError(f"no measured window to apply for {m.id}: wb optimize first")
    name = f"{re.sub(r'-[0-9]+k$', '', m.name.split(':')[0])}-{fits // 1024}k"
    models._post(f"{m.root}/api/create", {"model": name, "from": m.name, "stream": False,
                                          "parameters": {"num_ctx": fits}}, timeout=300)
    entry = dict(m.config)
    out = (entry.get("limit") or {}).get("output") or fits // 8
    entry["limit"] = {**(entry.get("limit") or {}), "context": fits, "input": fits - out, "output": out}
    if entry.get("name"):
        entry["name"] = f"{entry['name']} ({fits // 1024}K)"
    provider = m.id.split("/", 1)[0]
    models.write_entry(provider, name, entry)
    return (f"registered {provider}/{name}: {m.id}'s settings at a {fits // 1024}K window, all on "
            f"the GPU here. The router weighs both by their records; wb model retire {m.id} when "
            "you'd rather it never runs")


def start(bench: bool = False) -> list[str]:
    """Detach the run (it outlives Claude's session) and say what it will take."""
    st = state()
    if st.get("pid") and models._alive(st["pid"], st.get("start")):
        return [f"wb optimize is already running (pid {st['pid']}): wb optimize report"]
    ms = candidates()
    if not ms:
        raise RuntimeError("no Ollama model here to measure (wb models)")
    if models.holders():
        raise RuntimeError("a task holds the GPU: run wb optimize once it is done (real work first)")
    env = {**os.environ, "PYTHONPATH": str(PKG_ROOT), "WB_HOME": str(store.home())}
    for k in ("WB_TASK", "CLAUDE_CODE_SESSION_ID", "CLAUDE_PID"):
        env.pop(k, None)
    with open(store.home() / "optimize.log", "a") as log:
        proc = subprocess.Popen([sys.executable, "-X", "utf8", "-m", "workbench.optimize",
                                 *(["--bench"] if bench else [])], env=env, stdin=subprocess.DEVNULL,
                                stdout=log, stderr=log, start_new_session=True)
    store.write_json(path(), {**st, "pid": proc.pid, "start": models._started(proc.pid),
                              "began": store.now(), "done": None, "stopped": None})
    mins = estimate(ms) / 60
    return [f"wb optimize: measuring {len(ms)} model(s), about {max(1, round(mins))} min, holding "
            "the GPU alone (a task that wants it stops it; what's measured is kept)"
            + ("; then wb bench run replays the bench's cases on them" if bench else ""),
            "follow: wb optimize report   stop: wb optimize stop"]


def stop() -> int | None:
    st = state()
    pid = st.get("pid")
    if pid and models._alive(pid, st.get("start")):
        os.kill(pid, signal.SIGTERM)
        return pid
    return None


def report() -> list[str]:
    st = state()
    out = []
    if st.get("pid") and models._alive(st["pid"], st.get("start")):
        out.append(f"running since {time.strftime('%H:%M', time.localtime(st['began']))} (pid {st['pid']})")
    elif st.get("stopped"):
        out.append(f"stopped: {st['stopped']}")
    rows = st.get("models") or {}
    if not rows and not out:
        return ["not run yet: wb optimize estimate, then wb optimize"]
    for mid, row in rows.items():
        out.append(advice(mid, row) or f"{mid}: its {row['window'] // 1024}K window fits all on "
                   "the GPU here, and no bigger one does")
    return out


def _on_term(*_):
    raise Stopped("stopped (wb optimize stop, or a task wanted the GPU)")


def main() -> None:
    signal.signal(signal.SIGTERM, _on_term)
    store.absolute_env()
    why = None
    try:
        with models.GpuLease(None, HOLDER, say=print, wait=False):
            models.free_gpu()
            for m in candidates():
                try:
                    trained = models.ollama_show(m.root, m.name).get("context_length")
                except Exception:
                    trained = None
                print(f"{m.id}: measuring", flush=True)
                _record(m.id, fit(m, trained))
    except (Stopped, RuntimeError) as e:
        why = str(e)
    finally:
        st = state()
        store.write_json(path(), {**st, "pid": None, "done": store.now(), "stopped": why})
    if "--bench" in sys.argv and not why:
        from . import bench
        try:
            print("\n".join(bench.start([m.id for m in candidates()], [], [], "optimize", 1)[1]))
        except RuntimeError as e:
            print(f"no bench run: {e}")


if __name__ == "__main__":
    main()
