"""The pane's Stats tab: its settings (stats.json, `wb stats set`), read by `wb top` and the mod
(the live view: `live`, `every`). What a task records for the tab is always recorded: it costs
nothing, and a `recording` switch to stop it guarded nothing (#1322). The feed: everything
workbench has learned, for the tab's history (`wb stats --json`, #405)."""
import time
from collections import Counter, defaultdict
from statistics import median

from . import store

DEFAULTS = {"live": True, "every": 2}
EVERY = (1, 2, 5)  # seconds between the live view's samples (wb top)
SWITCHES = ("live",)


def path():
    return store.home() / "stats.json"


def settings() -> dict:
    """The settings, each its default unless stats.json says otherwise (a value of the wrong
    type, hand-edited, is ignored rather than trusted; a `recording` key from before #1322 too)."""
    data = store.read_json(path(), {})
    if not isinstance(data, dict):  # the runner reads this every round: never a reason to fail
        data = {}
    out = dict(DEFAULTS)
    for key in SWITCHES:
        if isinstance(data.get(key), bool):
            out[key] = data[key]
    if data.get("every") in EVERY:
        out["every"] = data["every"]
    return out


def set_setting(key: str, value: str) -> dict:
    """`wb stats set KEY VALUE`. Raises ValueError with the allowed values. Under stats.lock: two
    settings picked at once (two sessions' rows, the pane and a shell) each wrote back the snapshot
    it read, and one was lost while both said it took (#564)."""
    if key in SWITCHES:
        if value not in ("on", "off"):
            raise ValueError(f"{key} is on or off")
        change = {key: value == "on"}
    elif key == "every":
        if value not in {str(s) for s in EVERY}:
            raise ValueError(f"every is one of {', '.join(map(str, EVERY))} (seconds)")
        change = {"every": int(value)}
    else:
        raise ValueError(f"no setting {key!r}: live or every")
    with store.locked(path().with_suffix(".lock")):
        data = store.read_json(path(), {})
        data = data if isinstance(data, dict) else {}
        store.write_json(path(), {**data, **change, "ts": store.now()})
    return settings()


def line() -> str:
    s = settings()
    return f"stats: live view {'on' if s['live'] else 'off'} · every {s['every']} s"


# The feed (#405): what workbench has learned, as one object for the Stats tab (`wb stats
# --json`). Built from what already computes it; a section with no data is left out, never a
# zero. The score, the savings estimate, the streak, the bests, the milestones, the work
# absorbed, the questions, spec-gap discards, the energy and its cost and the 30 days' line were
# in it too: precision nobody acted on (#1322); `wb savings` still says the saving.
TREND_MIN = 6  # speed samples (tasks) before a model's trend is given


def _day(ts: float | None) -> str | None:
    return time.strftime("%Y-%m-%d", time.localtime(ts)) if ts else None  # a row too old to say


def _midnight(now: float) -> float:
    """The next local midnight after ``now``: the feed holds until then (its periods are counted
    from today), so a reader reads it again once it passes, files unchanged or not (#575)."""
    t = time.localtime(now)
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday + 1, 0, 0, 0, 0, 0, -1))


def _landed(r: dict) -> bool:
    return r.get("outcome") == "accepted"


def _mean(xs: list) -> float | None:
    xs = [x for x in xs if isinstance(x, (int, float))]
    return round(sum(xs) / len(xs), 1) if xs else None




def _models(rows: list[dict]) -> dict:
    by = defaultdict(list)
    for r in rows:
        if r.get("model"):
            by[r["model"]].append(r)
    return {model: _model_stats(model, rs) for model, rs in by.items()}


# The tab's model lines (#638, the owner, 2026-11-21): every model that ever ran a task, forever,
# each `wb optimize` window its own line, in no order; after 49 tasks the tab listed five, two of
# them variants of a third and one long gone. Shown: the models OpenCode has now, a variant folded
# under its source, the most recently used first; the rest on one line. `models` keeps them all.
OLD_DAYS = 90
# At most this many of them (the owner, 2026-11-21): a machine with a model a week tried for a
# year drew a line each, and the tab's menu sat a long scroll below a new user's first look.
# The rest are counted in `more`; `wb stats --all` and `wb models` still show every one.
STATS_MODEL_LINES = 3


def _model_lines(rows: list[dict], now: float) -> dict | None:
    """{"lines": [{model, variants, windows, last, …_model_stats}], "more": {models, tasks},
    "older": {models, tasks}}: the newest STATS_MODEL_LINES lines, the other recent ones in
    `more`, the retired and long-idle in `older` (each only when it counts any)."""
    from . import models
    try:
        found = models.discover()
    except Exception:  # the feed never fails over a config it can't read
        found = {}
    over = models._overrides()  # a retired variant still folds under its source
    # an id a model had before (models.json `renamed_from`; an Ollama digest it now runs under
    # another id, router.earlier without a server read: from the rows' own weights_id) folds
    # under the id it has now, not into "older" (#818)
    newer = {old: k for k, v in over.items() if isinstance(v, dict)
             for old in _renames(over, k)}
    weights = {r["model"]: r.get("weights_id") for r in rows if r.get("model")}
    digest = {w: k for k, w in weights.items() if k in found and w and w.startswith("ollama:")}
    newer.update({k: digest[w] for k, w in weights.items()
                  if k not in found and k not in newer and w in digest})

    def base(mid: str) -> str:
        mid = newer.get(mid, mid)
        return ((over.get(mid) or {}) if isinstance(over.get(mid), dict) else {}).get("variant_of") or mid
    sources = {base(m) for m in found} | set(found)
    # an API model is no discover() model: its rows say it ran (the catalog isn't read here)
    sources |= {r["model"] for r in rows if r.get("model") and r.get("kind") == "api"}
    groups = defaultdict(list)
    for r in rows:  # by kind too: one id run as a GPU model and then an API one keeps two (#1022)
        if r.get("model"):
            groups[(base(r["model"]), r.get("kind") == "api")].append(r)
    lines, older = [], {"models": 0, "tasks": 0}
    for (b, _), rs in groups.items():
        last = max((r.get("ts") or 0 for r in rs), default=0)
        if b not in sources or (last and now - last > OLD_DAYS * 86400):
            older["models"] += 1
            older["tasks"] += len(rs)
            continue
        newest = max(rs, key=lambda r: r.get("ts") or 0)["model"]
        # a configured variant that ran nothing yet folds here too: its optimized window and
        # bench run are this line's, not "1 more" beside it (2026-11-21)
        ids = {r["model"] for r in rs} | {m for m in found if base(m) == b}
        lines.append({"model": b, "variants": sorted(ids),
                      "windows": sorted({r["window"] for r in rs if isinstance(r.get("window"), int)}),
                      "last": last, **_model_stats(newest, rs)})
    lines.sort(key=lambda x: -x["last"])
    if not lines and not older["models"]:
        return None
    rest = lines[STATS_MODEL_LINES:]
    more = {"models": len(rest), "tasks": sum(x["tasks"] for x in rest)}
    return {"lines": lines[:STATS_MODEL_LINES], **({"more": more} if rest else {}),
            **({"older": older} if older["models"] else {})}


def _renames(over: dict, model_id: str) -> list[str]:
    from . import router
    return router._up(over, model_id, "renamed_from")


def _model_stats(model: str, rs: list[dict]) -> dict:
    """One model's record over ``rs``; its speeds (router.speed) are ``model``'s."""
    from . import router
    n, landed = len(rs), sum(map(_landed, rs))
    m = {"tasks": n, "landed": landed, "accepted_pct": round(100 * landed / n),
         "rounds": _mean([r.get("rounds") for r in rs]),
         "auto_fixes": _mean([r.get("auto_fixes") for r in rs]),
         "reviews": _mean([r.get("reviews") for r in rs]),
         "minutes": _mean([r.get("minutes") for r in rs]),
         "local_tokens": round(sum((r.get("tok_in") or 0) + (r.get("tok_out") or 0) for r in rs) / n),
         **({"speed": sp} if (sp := router.speed(model)) else {}),
         **_kind_stats(rs)}
    trend = [[r["ts"], r["speed"]["write"]] for r in sorted(rs, key=lambda r: r.get("ts") or 0)
             if r.get("ts") and isinstance(r.get("speed"), dict) and r["speed"].get("write")]
    if trend:
        m["write_trend"] = trend[-30:]
    if len(trend) >= TREND_MIN:  # the newest third against the oldest third, medians
        k = len(trend) // 3
        old, new = median(v for _, v in trend[:k]), median(v for _, v in trend[-k:])
        if old:
            m["write_change_pct"] = round(100 * (new - old) / old)
    return {k: v for k, v in m.items() if v is not None}


def _kind_stats(rs: list[dict]) -> dict:
    """A model's `kind` and, for an API model, `paid`: the mean dollars a task over its rows with
    a known cost (none known: left out, never 0) (#1022); a task that sent nothing is none
    (results.task_cost, #1106)."""
    from .results import api_row, task_cost
    if not any(map(api_row, rs)):
        return {"kind": "gpu"}
    known = [c for r in rs if api_row(r) and (c := task_cost(r)) is not None]
    return {"kind": "api", **({"paid": round(sum(known) / len(known), 6)} if known else {})}


def _api_period(a: dict) -> dict | None:
    """A period's `api` part of the feed, from savings.api_totals; None with no API task."""
    if not a["tasks"]:
        return None
    # plan: no false $0; prices: the dollars' basis, the pane's ≈ (#1135)
    return {k: a[k] for k in ("tasks", "cost", "unknown", "plan", "by_provider", "prices") if k in a}


def _by(rows: list[dict], key) -> dict:
    by = defaultdict(list)
    for r in rows:
        for k in key(r):
            by[k].append(r)
    return {k: {"tasks": len(rs), "accepted_pct": round(100 * sum(map(_landed, rs)) / len(rs))}
            for k, rs in by.items()}


def _bench() -> dict:
    """Each model's newest finished bench run, the cases passed of those graded, and a newer run
    still going or cut short as `partial` beside it ({run, day, passed, of, planned, running}),
    never in its place: the newest run's first graded case read "1/1" as the model's result,
    for good if the run was stopped (#570). A run is finished for a model once each of its
    planned replays (plan.json's pairs on it, times repeat) has settled: graded, or didn't start
    (an error, which drive doesn't retry). A run with no plan is taken as finished."""
    from . import bench
    every = [r for r in store.read_jsonl(bench.runs_file())[0]
             if isinstance(r, dict) and r.get("model") and r.get("arm") != "claude"]
    graded = defaultdict(list)
    settled = defaultdict(set)
    for r in every:
        key = (r["model"], r.get("run"))
        if r.get("outcome") in ("passed", "failed"):
            graded[key].append(r)
        if r.get("outcome") in ("passed", "failed", "error"):
            settled[key].add((r.get("case"), r.get("rep", 1)))
    plans: dict = {}
    running = (bench.driver() or {}).get("run")

    def planned(model, run) -> int | None:
        if run not in plans:
            plans[run] = bench.plan(run) if isinstance(run, str) and run else {}
        p = plans[run]
        if not p.get("pairs"):
            return None
        return sum(1 for _, m in p["pairs"] if m == model) * int(p.get("repeat") or 1)

    out: dict = {}
    for (model, run), rs in sorted(graded.items(), key=lambda kv: max(r.get("ts", 0) for r in kv[1])):
        ts = max(r.get("ts", 0) for r in rs)
        line = {"run": run, "day": _day(ts), "passed": sum(r["outcome"] == "passed" for r in rs), "of": len(rs)}
        n = planned(model, run)
        m = out.setdefault(model, {})
        if n is None or len(settled[(model, run)]) >= n:
            m.pop("partial", None)  # a newer run finished: the older partial is history
            m.update(line)
        else:
            m["partial"] = {**line, "planned": n, "running": run == running}
    return out


def _optimized() -> dict:
    data = store.read_json(store.machine_dir() / "optimize.json", {}) or {}  # optimize.path
    models = data.get("models") if isinstance(data, dict) else None
    return {m: {k: v[k] for k in ("window", "fits", "spill", "ts") if k in v}
            for m, v in (models or {}).items() if isinstance(v, dict) and v.get("window")}


def _helps(rows: list[dict]) -> dict:
    """progress.helped for each model a progress check's answer was tried on, over the feed's
    rows (one pass: a read per model was most of the feed's time on a long history, #565)."""
    from . import progress
    return {m: h for m, h in progress.helped_by_model(rows).items()
            if m and any(n for _, n in h.values())}


# what `wb stats` shows of the feed: its periods. The rest (guards and the like) is the tab's:
# headline lines above the table cost Claude tokens on every `wb stats` (#1322)
SUMMARY = ("periods",)


def feed(now: float | None = None, only: tuple | None = None) -> dict:
    """`wb stats --json`: the Stats tab's history (#405). results.jsonl is read once and that
    snapshot passed down: each model's helps read it again, 16 reads of a 4,800-row history per
    feed (#565). ``only``: these sections alone (`wb stats`)."""
    from . import savings, tuning
    now = store.now() if now is None else now
    every = store.results()
    rows = [r for r in every if tuning.judged(r)]  # trials and spec-gap discards out
    out: dict = {"ts": now, "until": _midnight(now), "settings": settings(), "tasks": len(rows)}
    paid_rows = store.results(trials=True)  # the money counts trials: money spent is spent (#1022)
    periods = {}
    for name, span in savings.PERIODS.items():
        since = now - span if span else 0
        rs = [r for r in rows if (r.get("ts") or 0) >= since]
        api = _api_period(savings.api_totals(paid_rows, since))
        if rs or api:
            landed = sum(map(_landed, rs))
            periods[name] = {"tasks": len(rs), "landed": landed,
                             **({"accepted_pct": round(100 * landed / len(rs))} if rs else {}),
                             **({"active_minutes": am} if (am := round(sum(r.get("active_minutes") or 0
                                                                           for r in rs), 1)) else {}),
                             **({"api": api} if api else {})}
    sections = {
        "periods": lambda: periods,
        "models": lambda: _models(rows),
        "model_lines": lambda: _model_lines(rows, now),
        "sizes": lambda: _by(rows, lambda r: [r["size"]] if r.get("size") else []),
        "stacks": lambda: _by(rows, lambda r: r.get("stack") or []),
        "guards": lambda: dict(sum((Counter(r.get("guards") or {}) for r in rows), Counter())),
        "helps": lambda: _helps(rows),
        "bench": _bench,
        "optimized": _optimized,
    }
    for k, make in sections.items():
        if only is None or k in only:
            if v := make():
                out[k] = v
    return out


def summary(f: dict) -> list[str]:
    """`wb stats`' lines above its table: each period's tasks and landings, short (Claude reads it
    too). The saving is `wb savings`', and the week's API dollars too (#1322)."""
    lines = []
    for name in ("week", "all"):
        p = f.get("periods", {}).get(name)
        if not p:
            continue
        lines.append(f"{'this week' if name == 'week' else 'all time'}: {p['tasks']} task(s), "
                     f"{p['landed']} landed" + (f" ({p['accepted_pct']}%)" if "accepted_pct" in p else ""))
    return lines
