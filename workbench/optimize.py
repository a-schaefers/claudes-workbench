"""wb optimize: measuring this machine for its local models, when the user asks (through Claude),
never on its own: an automatic run could be cut short and alarm people (the user, 2026-11-18).
Everything else learns passively from real tasks (the router, tuning.py, speed.json); this is the
part that has to load a model to know.

It runs on its own only where the user expects a short wait and is told: at setup (`wb setup`)
and when a Claude Code session opens (the mod), each time only when it is due (`due`: never
measured, the GPU changed, or a model came since, not one skipped before its retry time),
never while a task holds the GPU; each run
takes a minute or two (the user, 2026-11-18). Anything heavier (`--bench`) waits for the user to
ask, through Claude.

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

import contextlib
import json
import math
import os
import re
import signal
import statistics
import subprocess  # noqa: F401 (cli.detach starts the run; tests patch it here)
import sys
import time
from pathlib import Path

from . import hardware, models, store

HOLDER = "wb optimize"   # its name in gpu.json, which tells real work it may stop it
STEP = 4096              # windows tried are multiples of this
LOAD_GUESS = 20          # seconds a load takes when nothing has measured one (a 27B: 10 s from cache)
MAX_WINDOW = 131072      # never asked past this: a long context's cache could fill system RAM
PKG_ROOT = Path(__file__).resolve().parent.parent


class Stopped(Exception):
    pass


def path() -> Path:
    return store.home() / "optimize.json"


def state() -> dict:
    return store.read_json(path(), {}) or {}


def candidates() -> list[models.Model]:
    """The Ollama models that can be measured: this machine's and those on another machine's
    Ollama (wb remote), where the same /api/ps placement says what fits its card (#115). Not one
    the user keeps loaded (models.json `keep_loaded`, by id, by name, or an alias of the same
    model): each probe unloads and reloads its model, and a background measurement dropped the
    user's autocomplete model, which #215 protected only from eviction (#254, the audit of 1.2)."""
    held = models.keep_loaded()
    return [m for m in models.discover().values()
            if m.engine == "ollama" and m.id not in held and not models._kept_name(m.name, held)
            and models.status(m)["available"]]


def served(m: models.Model) -> int:
    return (m.config.get("limit") or {}).get("context") or models.MIN_WINDOW


def load_secs(m: models.Model) -> float:
    """How long a load of this model takes here: the median of the loads its tasks measured, one
    by one (a task's total held its reloads, #577), else a guess."""
    secs = [v for t in store.all_tasks() if t.spec.get("model") == m.id
            for v in models.single_loads(t.status)]
    return statistics.median(secs) if secs else LOAD_GUESS


def loads(m: models.Model) -> int:
    """The loads a fit takes at most: the served window, one end of the range, then halving."""
    span = max(served(m) - models.MIN_WINDOW, min(served(m), MAX_WINDOW - served(m)))
    return 2 + max(0, math.ceil(math.log2(max(1, span / STEP))))


def estimate(ms: list[models.Model]) -> float:
    took = state().get("secs_per_load")  # what the last run measured here
    return sum(loads(m) * (took or load_secs(m) + 5) for m in ms)


def fingerprint() -> list:
    """What a fit depends on that can change under it: each GPU here and its memory, then each
    remote Ollama that answers, with the models it has pulled and their sizes (a re-pull under the
    same name can be another quantization: another fit)."""
    here = sorted([g.get("name"), g.get("vram")] for g in hardware.gpus(windows=False))
    roots = sorted({m.root for m in models.discover().values() if m.engine == "ollama" and m.remote})
    there = []
    for root in roots:
        tags = models._get(f"{root}/api/tags", timeout=2)
        if isinstance(tags, dict):
            there.append(["remote", root, sorted([x.get("name"), x.get("size")]
                                                 for x in tags.get("models") or [])])
    return here + there


def _split(fp) -> tuple[list, dict]:
    return [e for e in fp if e[:1] != ["remote"]], {e[1]: e[2] for e in fp if e[:1] == ["remote"]}


def changed(old) -> str:
    """What differs from the fingerprint ``old``, or "": this machine's GPUs, and each remote
    server both know. One asleep now, or new since (its models are new models), changes nothing,
    so a gaming PC switched off doesn't make every session's run due."""
    if old is None:
        return ""
    (here, there), (was_here, was_there) = _split(fingerprint()), _split(old)
    if here != was_here:
        return "the GPU changed"
    moved = sorted(r for r in there.keys() & was_there.keys() if there[r] != was_there[r])
    return f"the models at {', '.join(moved)} changed" if moved else ""


def remembered(old) -> list:
    """The fingerprint to store: now's, keeping what ``old`` knew of a server that didn't answer."""
    now = fingerprint()
    _, there = _split(now)
    return now + [e for e in old or [] if e[:1] == ["remote"] and e[1] not in there]


def busy(m: models.Model) -> list[str]:
    """Other models its remote server has loaded: what else is using that card. A fit measured
    beside them would come out small, and they aren't this machine's to unload."""
    if not m.remote:
        return []  # free_gpu has emptied this machine's
    ps = models._get(f"{m.root}/api/ps", timeout=2) or {}
    return [x.get("name") for x in ps.get("models") or [] if not models._matches(m.name, {x.get("name")})]


# A model a run tried and couldn't measure (optimize.json `skipped`: its remote server busy, its
# load refused, its server unreachable) waits this long before it makes a run due again. Without
# a record, it was "new" at every Claude Code session, and each session's run emptied the local
# GPU first, then skipped it again (#583, the audit of 1.90). A changed GPU or remote model list
# still makes a run due at once.
RETRY_SECS = {"busy": 6 * 3600, "unreachable": 24 * 3600, "failed": 7 * 86400}


def _waiting(st: dict, now: float | None = None) -> set[str]:
    """The ids whose last try was skipped and whose retry time hasn't come."""
    now = store.now() if now is None else now
    return {mid for mid, r in (st.get("skipped") or {}).items()
            if isinstance(r, dict) and (r.get("retry_after") or 0) > now}


def _skip(model_id: str, outcome: str, why: str) -> None:
    """Record that ``model_id`` wasn't measured, and when to try it again (RETRY_SECS)."""
    print(f"{model_id}: not measured, {why}", flush=True)
    st = state()
    row = {"outcome": outcome, "why": why, "ts": store.now(),
           "retry_after": store.now() + RETRY_SECS[outcome]}
    store.write_json(path(), {**st, "skipped": {**(st.get("skipped") or {}), model_id: row}})


def due(ms: list[models.Model] | None = None) -> str:
    """Why a run is due, or "": never run, the GPU changed, a model came since, or the last run
    was stopped before it finished."""
    if models._fake():  # the tests' fake models have no server; a detached run there outlived a
        return ""       # test and failed its tearDown (CI, 2026-11-18)
    st = state()
    ms = candidates() if ms is None else ms
    if not ms:
        return ""
    if not st.get("done"):
        return "never measured"
    if why := changed(st.get("hardware")):
        return why
    if st.get("stopped"):
        return "the last run was stopped before it finished"
    waiting = _waiting(st)
    new = [m.id for m in ms if m.id not in (st.get("models") or {}) and m.id not in waiting]
    return f"new model{'s' if len(new) > 1 else ''}: {', '.join(new)}" if new else ""


def probe(m: models.Model, window: int) -> dict:
    """Load ``m`` at ``window`` and read where Ollama put it: {window, size, vram, served}, served
    being the window it actually gave (/api/ps context_length; None when it doesn't say)."""
    models._unload(m)
    try:
        models._post(f"{m.root}/api/generate", {"model": m.name, "prompt": "", "keep_alive": "2m",
                                                "options": {"num_ctx": window}}, timeout=900)
        for x in (models._get(f"{m.root}/api/ps") or {}).get("models", []):
            if models._matches(m.name, {x.get("name")}) and x.get("size"):
                return {"window": window, "size": x["size"], "vram": x.get("size_vram") or 0,
                        "served": int(x["context_length"]) if x.get("context_length") else None}
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
        # a server that serves less than was asked (Ollama clamped to 16K at every load) hasn't
        # shown that ``w`` fits: placement alone recorded a 64K fit it never served (#338, the
        # audit of 1.4)
        return r["vram"] >= r["size"] and not (r.get("served") and r["served"] < w)

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
    short = [r["served"] for r in tried if r.get("served") and r["served"] < r["window"]]
    return {"window": now, "fits": good, "spill": round(100 * (1 - at["vram"] / at["size"]))
            if at and at["size"] else None, "clamped": max(short) if short else None,
            "tried": tried, "ts": store.now()}


def _record(model_id: str, row: dict) -> None:
    st = state()
    skipped = {k: v for k, v in (st.get("skipped") or {}).items() if k != model_id}
    store.write_json(path(), {**st, "models": {**(st.get("models") or {}), model_id: row},
                              "skipped": skipped})


def _made(key: str, source: str | None) -> None:
    """optimize.json `made`: a variant apply made at the server and hasn't yet registered, so a
    retry finishes it rather than refusing it as a model of the user's (#266)."""
    st = state()
    made = {k: v for k, v in (st.get("made") or {}).items() if k != key}
    store.write_json(path(), {**st, "made": {**made, **({key: source} if source else {})}})


def applied(model_id: str, row: dict) -> str:
    """The registered variant `wb optimize apply` made of ``model_id`` at the window ``row``
    measured, or "": the row's `applied`, else the name apply would give it. A run that measured
    the source again (a changed GPU, --all, a stopped run) wrote its row without `applied`, and
    the advice came back, naming a command that answered "exists already" (#584)."""
    have = models.discover()
    if row.get("applied") in have:
        return row["applied"]
    fits = row.get("fits")
    if not fits or fits == row.get("window") or "/" not in model_id:
        return ""
    provider, name = model_id.split("/", 1)
    key = f"{provider}/{entry_name(name, fits)}"
    return key if key != model_id and key in have else ""


def advice(model_id: str, row: dict) -> str:
    """One line on what the fit says, or "" when there is nothing to change (or apply has made
    its variant, still registered: every wb start repeated the advice, and the command it named
    answered "exists already", #584)."""
    if applied(model_id, row):
        return ""
    w, fits, spill = row.get("window"), row.get("fits"), row.get("spill")
    k = lambda n: f"{n // 1024}K"  # noqa
    if fits is None:
        return f"{model_id}: doesn't fit the GPU whole even at {k(models.MIN_WINDOW)}"
    if row.get("clamped") and row["clamped"] < w:  # #338: the window it runs at, not the one asked
        return (f"{model_id}: the server served it {k(row['clamped'])} whatever was asked (OpenCode "
                f"is told {k(w)}); {k(fits)} is what was seen to fit. Fix the server's window "
                f"(OLLAMA_CONTEXT_LENGTH?) or wb model add {model_id} --context {row['clamped']}")
    if spill:
        return (f"{model_id}: its {k(w)} window puts {spill}% of it in system RAM here (slower); "
                f"{k(fits)} fits all on the GPU. A bigger window compacts less, a whole one writes "
                f"faster: wb optimize apply {model_id} makes a {k(fits)} entry beside it")
    if fits > w:
        return (f"{model_id}: {k(fits)} fits all on the GPU here, more than the {k(w)} it serves: "
                f"wb optimize apply {model_id} makes a {k(fits)} entry beside it")
    return ""


def entry_name(source: str, window: int) -> str:
    """The new model's name: the source's whole name, tag and quantization kept, with the window
    as the tag's suffix (an earlier one's replaced). The family alone once named both
    family:7b and family:32b family-16k, and the second overwrote the first in Ollama and in
    OpenCode's config (the 2026-11-19 audit)."""
    base, _, tag = source.partition(":")
    if tag in ("", "latest"):
        base, tag = re.sub(r"-[0-9]+k$", "", base), ""  # an entry this made before 1.2: family-16k
    tag = re.sub(r"(^|-)[0-9]+k$", "", tag)
    return f"{base}:{tag + '-' if tag else ''}{window // 1024}k"


def budget(entry: dict, fits: int) -> tuple[dict, dict]:
    """The entry's limits and options at a ``fits`` window, its caps recomputed together: the
    source's output cap kept while it is at most half the window, else cut to a quarter of it,
    and ``max_tokens`` never past it. A 16K fit of a 128K entry with a 32K output cap and ``max_tokens`` 64K once
    wrote an input limit of -16384 and kept both caps (#332, the audit of 1.4)."""
    lim, opts = dict(entry.get("limit") or {}), dict(entry.get("options") or {})
    out = lim.get("output") or fits // 8
    if out > fits // 2:
        out = fits // 4
    lim.update(context=fits, input=fits - out, output=out)
    if opts.get("max_tokens"):
        opts["max_tokens"] = min(opts["max_tokens"], out)
    # checked before anything is written: a request inside these limits must fit the window
    if not (0 < out <= lim["input"] and lim["input"] + out <= fits
            and 0 < (opts.get("max_tokens") or out) <= out):
        raise RuntimeError(f"no workable budget at a {fits}-token window: limit {lim}, "
                           f"max_tokens {opts.get('max_tokens')}")
    return lim, opts


def apply(m: models.Model) -> str:
    """Make the measured window an entry of its own beside ``m``: an Ollama model with that
    num_ctx (from ``m``, no download) and OpenCode's entry, copied from ``m``'s (its options,
    such as thinking off, and output cap kept while the window leaves room: ``budget``), with
    the new window."""
    row = (state().get("models") or {}).get(m.id) or {}
    fits = row.get("fits")
    if not fits or fits == row.get("window"):
        raise RuntimeError(f"no measured window to apply for {m.id}: wb optimize start first")
    name = entry_name(m.name, fits)
    provider = m.id.split("/", 1)[0]
    entry = dict(m.config)
    entry.pop("id", None)  # an alias's id names m at the server: the new entry is its own model
    entry["limit"], entry["options"] = budget(entry, fits)
    if not entry["options"]:
        del entry["options"]
    if entry.get("name"):
        entry["name"] = f"{entry['name']} ({fits // 1024}K)"
    # the config first: the variant was made, then a config with comments refused its entry, and
    # the retry refused the variant it had made (#266, the audit of 1.2)
    models.config_target(f'under provider {provider}\'s "models", add "{name}": {json.dumps(entry)}')
    key = f"{provider}/{name}"
    made = (state().get("made") or {}).get(key) == m.id  # this apply's, left unregistered
    taken = {x.get("name") for x in (models._get(f"{m.root}/api/tags") or {}).get("models", [])}
    if key in models.discover() or models._matches(name, taken) and not made:
        raise RuntimeError(f"{key} exists already: wb model retire {key} first if it should be replaced")
    if not models._matches(name, taken):
        models._post(f"{m.root}/api/create", {"model": name, "from": m.name, "stream": False,
                                              "parameters": {"num_ctx": fits}}, timeout=300)
        _made(key, m.id)
    models.write_entry(provider, name, entry)
    # the source's models.json policy (its sizes, label, weights...) and a shared record: with a
    # record of its own the variant ranked below the source's one accepted task, so every start
    # picked the spilling source, and a source limited to trivial work got a variant that took
    # significant work (#584). router.lineage reads variant_of; router.rank prefers the variant.
    for k, v in {**(models._overrides().get(m.id) or {}), "variant_of": m.id}.items():
        models.set_override(key, k, v)
    _made(key, None)
    # measured already: it is the window that fit, so its arrival doesn't make a run due
    _record(key, {**row, "window": fits, "spill": 0, "tried": [], "ts": store.now()})
    _record(m.id, {**row, "applied": key})  # advice() stays quiet about the source
    return (f"registered {provider}/{name}: {m.id}'s settings at a {fits // 1024}K window, all on "
            f"the GPU here, with its models.json settings. It shares {m.id}'s record, and the router "
            f"now picks it before {m.id}, which stays for --model; wb model retire {m.id} when "
            "you'd rather it never runs")


def start(bench: bool = False, if_due: bool = False) -> list[str]:
    """Detach the run (it outlives Claude's session) and say what it will take. ``if_due``: only
    when due() says why, and quietly ([]) when it isn't or the GPU is busy (the next session
    tries again)."""
    st = state()
    if running(st):
        return [] if if_due else [f"wb optimize is already running (pid {st['pid']}): wb optimize report"]
    ms = candidates()
    why = due(ms) if if_due else ""
    if if_due and (not why or models.holders()):
        return []
    if not ms:
        raise RuntimeError("no Ollama model here to measure (wb models)")
    if models.holders():
        raise RuntimeError("a task holds the GPU: run wb optimize start once it is done (real work first)")
    from . import context  # its bench replays' agents get the user's PYTHONPATH (#512)
    env = context.detached_env()
    for k in ("WB_TASK", "CLAUDE_CODE_SESSION_ID", "CLAUDE_PID"):
        env.pop(k, None)
    store.home().mkdir(parents=True, exist_ok=True)  # a fresh profile's first wb setup (#198)
    with open(store.home() / "optimize.log", "a") as log:
        argv = ["--bench"] * bench + ["--all"] * (not why)  # asked for: every model again
        from . import cli  # cli imports this module
        # its own scope, to outlive a logout: SIGTERM there recorded "stopped" (#531)
        proc = cli.detach([sys.executable, "-X", "utf8", "-m", "workbench.optimize", *argv],
                          awake="wb optimize", env=env, stdout=log, stderr=log)  # awake: #516
    store.write_json(path(), {**st, "pid": proc.pid, "start": models._started(proc.pid),
                              "began": store.now(), "done": None, "stopped": None})
    mins = max(1, round(estimate(ms) / 60))
    if why:
        return [f"measuring the GPU for {len(ms)} local model(s) ({why}): about {mins} min; a task "
                "you start goes first"]
    return [f"wb optimize: measuring {len(ms)} model(s), about {mins} min, holding "
            "the GPU alone (a task that wants it stops it; what's measured is kept)"
            + ("; then wb bench run replays the bench's cases on them" if bench else ""),
            "follow: wb optimize report   stop: wb optimize stop"]


def running(st: dict) -> bool:
    """Whether state.json's pid is still the measuring process: alive, started when it did (Linux),
    and running workbench.optimize (a recycled pid runs something else: #82)."""
    pid = st.get("pid")
    return bool(pid) and models._alive(pid, st.get("start")) and models.runs(pid, "-m",
                                                                             "workbench.optimize")


def stop() -> int | None:
    st = state()
    pid = st.get("pid")
    if running(st):
        os.kill(pid, signal.SIGTERM)
        return pid
    return None


def report() -> list[str]:
    st = state()
    out = []
    if running(st):
        out.append(f"running since {time.strftime('%H:%M', time.localtime(st['began']))} (pid {st['pid']})")
    elif st.get("stopped"):
        out.append(f"stopped: {st['stopped']}")
    rows = st.get("models") or {}
    if not rows and not out and not st.get("skipped"):
        return ["not run yet: wb optimize estimate, then wb optimize start (Ollama models only)"]
    for mid, row in rows.items():
        if made := applied(mid, row):  # not "fits all on the GPU": it spills, its variant doesn't
            out.append(f"{mid}: {row['fits'] // 1024}K fits all on the GPU here; {made}, applied, "
                       "is that window")
            continue
        out.append(advice(mid, row) or f"{mid}: its {row['window'] // 1024}K window fits all on "
                   "the GPU here, and no bigger one does")
    for mid, row in (st.get("skipped") or {}).items():
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(row.get("retry_after") or 0))
        out.append(f"{mid}: not measured ({row.get('why')}); tried again after {when}, or by "
                   "wb optimize")
    return out


def _on_term(*_):
    raise Stopped("stopped (wb optimize stop, or a task wanted the GPU)")


def main() -> None:
    signal.signal(signal.SIGTERM, _on_term)
    store.absolute_env()
    why, began, n = None, time.monotonic(), 0
    try:
        st = state()
        every = "--all" in sys.argv or st.get("stopped") or st.get("hardware") is None or \
            changed(st["hardware"])
        waiting = set() if every else _waiting(st)
        with models.GpuLease(None, HOLDER, say=print, wait=False):
            freed = False
            for m in candidates():
                if not every and (m.id in (st.get("models") or {}) or m.id in waiting):
                    continue  # due for a new model only: the others' fits still hold
                with contextlib.ExitStack() as turn:
                    if m.remote:  # its server's own lease too: a task there may be using this very
                        try:      # model, and probe unloads it (#198, the 2026-11-19 audit)
                            turn.enter_context(models.GpuLease(None, HOLDER, say=print, wait=False,
                                                               on=m))
                        except RuntimeError:
                            _skip(m.id, "busy", f"a task is using {m.root}")
                            continue
                    if others := busy(m):
                        _skip(m.id, "busy", f"{m.root} has {', '.join(others)} loaded")
                        continue
                    if not m.remote and not freed:
                        # this machine's GPU emptied only for a local probe: a run due for a
                        # remote model alone unloaded the user's local models for nothing (#583)
                        models.free_gpu()
                        freed = True
                    try:
                        trained = models.ollama_show(m.root, m.name).get("context_length")
                    except Exception:
                        trained = None
                    print(f"{m.id}: measuring", flush=True)
                    # one model's failure is that model's: an HTTP 500 at its load (too big for
                    # the card) or a dropped connection stopped the run, and the next run
                    # measured everything again (#583); a stop (Stopped) still ends the run
                    try:
                        row = fit(m, trained)
                    except RuntimeError as e:
                        _skip(m.id, "failed", f"its load failed: {e}")
                        continue
                    except OSError as e:
                        _skip(m.id, "unreachable", f"its server didn't answer: {getattr(e, 'reason', e)}")
                        continue
                    n += len(row["tried"])
                    _record(m.id, row)
    except (Stopped, RuntimeError) as e:
        why = str(e)
    finally:
        st = state()
        took = (time.monotonic() - began) / n if n else st.get("secs_per_load")
        store.write_json(path(), {**st, "pid": None, "done": store.now(), "stopped": why,
                                  "hardware": remembered(st.get("hardware")), "secs_per_load": took})
    if "--bench" in sys.argv and not why:
        from . import bench
        try:
            print("\n".join(bench.start([m.id for m in candidates()], [], [], "optimize", 1)[1]))
        except RuntimeError as e:
            print(f"no bench run: {e}")


if __name__ == "__main__":
    main()
