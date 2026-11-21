"""The pane's Stats tab: its settings (stats.json, `wb stats set`), read by the runner (what a
task records for it) and by `wb top` and the mod (the live view). The user's rule (2026-11-20):
a measurement that costs nothing is on by default, and every one can be turned off. The feed:
everything workbench has learned, for the tab's history (`wb stats --json`, #405)."""
import time
from collections import Counter, defaultdict
from datetime import date, timedelta
from statistics import median

from . import store

DEFAULTS = {"recording": True, "live": True, "every": 2}
EVERY = (1, 2, 5)  # seconds between the live view's samples (wb top)
SWITCHES = ("recording", "live")


def path():
    return store.home() / "stats.json"


def settings() -> dict:
    """The settings, each its default unless stats.json says otherwise (a value of the wrong
    type, hand-edited, is ignored rather than trusted)."""
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


def recording() -> bool:
    """Whether a task records what only the Stats tab reads (its row's speeds, watts, guards and
    timings; the round_end event). What the router, tuning and savings read is always kept."""
    return settings()["recording"]


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
        raise ValueError(f"no setting {key!r}: recording, live or every")
    with store.locked(path().with_suffix(".lock")):
        data = store.read_json(path(), {})
        data = data if isinstance(data, dict) else {}
        store.write_json(path(), {**data, **change, "ts": store.now()})
    return settings()


def line() -> str:
    s = settings()
    return (f"stats: recording {'on' if s['recording'] else 'off'} · live view "
            f"{'on' if s['live'] else 'off'} · every {s['every']} s")


# The feed (#405): everything workbench has learned, as one object for the Stats tab (`wb stats
# --json`). Built from what already computes it; a section with no data is left out, never a
# zero, and the savings are the estimate savings.py labels as one.
MILESTONES_LANDED = (1, 10, 25, 50, 100, 250, 500, 1000)
MILESTONES_SAVED = (100_000, 1_000_000, 10_000_000, 100_000_000)  # Claude tokens, estimated
TREND_MIN = 6  # speed samples (tasks) before a model's trend is given


def _day(ts: float | None) -> str | None:
    return time.strftime("%Y-%m-%d", time.localtime(ts)) if ts else None  # a row too old to say


def _midnight(now: float) -> float:
    """The next local midnight after ``now``: the feed holds until then (its streak, its 7 and 30
    days and its periods are counted from today), so the pane reads it again once it passes, files
    unchanged or not (#575)."""
    t = time.localtime(now)
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday + 1, 0, 0, 0, 0, 0, -1))


def _landed(r: dict) -> bool:
    return r.get("outcome") == "accepted"


def _mean(xs: list) -> float | None:
    xs = [x for x in xs if isinstance(x, (int, float))]
    return round(sum(xs) / len(xs), 1) if xs else None


def _streak(rows: list[dict], now: float) -> dict | None:
    """Days in a row with a task landed: the current run (ending today, or yesterday while today
    has none yet) and the best."""
    days = sorted({_day(r["ts"]) for r in rows if _landed(r) and r.get("ts")})
    if not days:
        return None
    best = run = 1
    gap = lambda a, b: (date.fromisoformat(b) - date.fromisoformat(a)).days  # noqa: E731
    for a, b in zip(days, days[1:]):
        run = run + 1 if gap(a, b) == 1 else 1
        best = max(best, run)
    current = 0
    today = date.fromtimestamp(now)  # dates, not now - 86400: a 23- or 25-hour day skipped one (#563)
    if days[-1] in (today.isoformat(), (today - timedelta(days=1)).isoformat()):
        current, i = 1, len(days) - 1
        while i > 0 and gap(days[i - 1], days[i]) == 1:
            current, i = current + 1, i - 1
    return {"current": current, "best": best, "days": len(days)}


def _bests(rows: list[dict]) -> dict:
    out = {}
    first_try = [r for r in rows if _landed(r) and r.get("rounds") == 1 and not r.get("reviews")
                 and r.get("diff")]
    if first_try:
        big = max(first_try, key=lambda r: r["diff"].get("added", 0) + r["diff"].get("removed", 0))
        out["largest_first_try"] = {"task": big.get("task"), "ts": big.get("ts"),
                                    "lines": big["diff"].get("added", 0) + big["diff"].get("removed", 0)}
    timed = [r for r in rows if _landed(r) and isinstance(r.get("minutes"), (int, float))]
    if timed:
        fast = min(timed, key=lambda r: r["minutes"])
        out["fastest"] = {"task": fast.get("task"), "ts": fast.get("ts"), "minutes": fast["minutes"]}
    return out


def _milestones(rows: list[dict]) -> list[dict]:
    """Each milestone reached, with its day: tasks landed, and Claude tokens saved (estimate)."""
    from . import savings
    out, landed, saved = [], 0, 0
    marks_l, marks_s = list(MILESTONES_LANDED), list(MILESTONES_SAVED)
    for r in sorted(rows, key=lambda r: r.get("ts") or 0):
        if _landed(r):
            landed += 1
            while marks_l and landed >= marks_l[0]:
                out.append({"kind": "landed", "n": marks_l.pop(0), "day": _day(r.get("ts"))})
        if (p := savings.of_row(r)) is not None:
            saved += p["saved"]
            while marks_s and saved >= marks_s[0]:
                out.append({"kind": "saved", "n": marks_s.pop(0), "day": _day(r.get("ts"))})
    return out


def _absorbed(rows: list[dict]) -> dict:
    """Work that never reached Claude: auto-fix rounds (and the tasks whose tests then passed),
    the agents' questions answered, and handovers in tasks that went on to land."""
    fixed = [r for r in rows if (r.get("auto_fixes") or 0) > 0]
    out = {"auto_fixes": sum(r["auto_fixes"] for r in fixed),
           "auto_fixed_tasks": sum(1 for r in fixed if r.get("tests_ok")),
           "questions": sum((r.get("asks") or {}).get("questions", 0) for r in rows),
           "handovers_landed": sum(1 for r in rows if _landed(r)
                                   and any((s.get("handovers") or 0) > 0 for s in r.get("steps") or []))}
    return {k: v for k, v in out.items() if v}


def _models(rows: list[dict]) -> dict:
    from . import router, spend
    by = defaultdict(list)
    for r in rows:
        if r.get("model"):
            by[r["model"]].append(r)
    out = {}
    for model, rs in by.items():
        n, landed = len(rs), sum(map(_landed, rs))
        claude = [spend.weighted(r["claude"]) for r in rs if isinstance(r.get("claude"), dict)]
        m = {"tasks": n, "landed": landed, "accepted_pct": round(100 * landed / n),
             "rounds": _mean([r.get("rounds") for r in rs]),
             "auto_fixes": _mean([r.get("auto_fixes") for r in rs]),
             "reviews": _mean([r.get("reviews") for r in rs]),
             "minutes": _mean([r.get("minutes") for r in rs]),
             "local_tokens": round(sum((r.get("tok_in") or 0) + (r.get("tok_out") or 0) for r in rs) / n),
             **({"claude": round(sum(claude) / len(claude))} if claude else {}),
             **({"speed": sp} if (sp := router.speed(model)) else {})}
        trend = [[r["ts"], r["speed"]["write"]] for r in sorted(rs, key=lambda r: r.get("ts") or 0)
                 if r.get("ts") and isinstance(r.get("speed"), dict) and r["speed"].get("write")]
        if trend:
            m["write_trend"] = trend[-30:]
        if len(trend) >= TREND_MIN:  # the newest third against the oldest third, medians
            k = len(trend) // 3
            old, new = median(v for _, v in trend[:k]), median(v for _, v in trend[-k:])
            if old:
                m["write_change_pct"] = round(100 * (new - old) / old)
        out[model] = {k: v for k, v in m.items() if v is not None}
    return out


def _by(rows: list[dict], key) -> dict:
    by = defaultdict(list)
    for r in rows:
        for k in key(r):
            by[k].append(r)
    return {k: {"tasks": len(rs), "accepted_pct": round(100 * sum(map(_landed, rs)) / len(rs))}
            for k, rs in by.items()}


def _days(rows: list[dict], now: float, n: int = 30) -> list[dict]:
    today = date.fromtimestamp(now)  # local dates: now - i * 86400 skipped or repeated a day at DST (#563)
    want = [(today - timedelta(days=i)).isoformat() for i in range(n - 1, -1, -1)]
    tasks, landed = defaultdict(int), defaultdict(int)
    for r in rows:
        if r.get("ts"):
            d = _day(r["ts"])
            tasks[d] += 1
            landed[d] += _landed(r)
    return [{"day": d, "tasks": tasks[d], "landed": landed[d]} for d in want]


def _bench() -> dict:
    """Each model's newest bench run: the cases passed of those graded."""
    from . import bench
    newest: dict[str, tuple] = {}
    rows = [r for r in bench.rows() if r.get("model") and r.get("arm") != "claude"]
    for r in rows:
        if r["model"] not in newest or r.get("ts", 0) > newest[r["model"]][1]:
            newest[r["model"]] = (r.get("run"), r.get("ts", 0))
    out = {}
    for model, (run, ts) in newest.items():
        rs = [r for r in rows if r["model"] == model and r.get("run") == run]
        out[model] = {"run": run, "day": _day(ts), "passed": sum(r.get("outcome") == "passed" for r in rs),
                      "of": len(rs)}
    return out


def _optimized() -> dict:
    data = store.read_json(store.home() / "optimize.json", {}) or {}
    models = data.get("models") if isinstance(data, dict) else None
    return {m: {k: v[k] for k in ("window", "fits", "spill", "ts") if k in v}
            for m, v in (models or {}).items() if isinstance(v, dict) and v.get("window")}


def _helps(rows: list[dict]) -> dict:
    """progress.helped for each model a progress check's answer was tried on, over the feed's
    rows (one pass: a read per model was most of the feed's time on a long history, #565)."""
    from . import progress
    return {m: h for m, h in progress.helped_by_model(rows).items()
            if m and any(n for _, n in h.values())}


SUMMARY = ("periods", "streak", "absorbed", "guards")  # what `wb stats` shows of the feed


def feed(now: float | None = None, only: tuple | None = None) -> dict:
    """`wb stats --json`: the Stats tab's history (#405). results.jsonl is read once and that
    snapshot passed down: each period's savings and each model's helps read it again, 16 reads
    of a 4,800-row history per feed (#565). ``only``: these sections alone (`wb stats`)."""
    from . import savings, tuning
    now = store.now() if now is None else now
    every = store.results()
    rows = [r for r in every if tuning.judged(r)]  # trials and spec-gap discards out
    out: dict = {"ts": now, "until": _midnight(now), "settings": settings(), "tasks": len(rows)}
    periods = {}
    for name, span in savings.PERIODS.items():
        since = now - span if span else 0
        rs = [r for r in rows if (r.get("ts") or 0) >= since]
        t = savings.totals(every, since=since)
        if rs or t["tasks"]:
            landed = sum(map(_landed, rs))
            periods[name] = {"tasks": len(rs), "landed": landed,
                             **({"accepted_pct": round(100 * landed / len(rs))} if rs else {}),
                             # nothing to compare: no savings, rather than a row of zeros
                             **({"savings": t, "score": savings.score(t)} if t["tasks"] else {}),
                             **({"active_minutes": am} if (am := round(sum(r.get("active_minutes") or 0
                                                                           for r in rs), 1)) else {})}
    sections = {
        "periods": lambda: periods,
        "streak": lambda: _streak(rows, now),
        "bests": lambda: _bests(rows),
        "milestones": lambda: _milestones(rows),
        "absorbed": lambda: _absorbed(rows),
        "models": lambda: _models(rows),
        "sizes": lambda: _by(rows, lambda r: [r["size"]] if r.get("size") else []),
        "stacks": lambda: _by(rows, lambda r: r.get("stack") or []),
        "days": lambda: _days(rows, now) if rows else None,
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
    """`wb stats`' lines above its table: the feed's headline, short (Claude reads it too)."""
    lines = []
    for name in ("week", "all"):
        p = f.get("periods", {}).get(name)
        if not p:
            continue
        s, saved = p.get("score"), (p.get("savings") or {}).get("saved", 0)
        said = (f"saved ~{saved / 1000:.0f}K" if saved >= 0 else f"cost ~{-saved / 1000:.0f}K more")
        lines.append(f"{'this week' if name == 'week' else 'all time'}: {p['tasks']} task(s), "
                     f"{p['landed']} landed" + (f" ({p['accepted_pct']}%)" if "accepted_pct" in p else "")
                     + (f" · {said} Claude tokens (estimate) · score {s['score']}/100" if s else ""))
    if st := f.get("streak"):  # a run that ended isn't mentioned: only the best (the plan's rule)
        lines.append(f"streak: {st['current']} day(s) in a row with a task landed · best {st['best']}"
                     if st["current"] else f"best streak: {st['best']} day(s) in a row with a task landed")
    if ab := f.get("absorbed"):
        parts = []
        if ab.get("auto_fixes"):
            parts.append(f"{ab['auto_fixes']} auto-fix round(s), {ab.get('auto_fixed_tasks', 0)} task(s) then green")
        if ab.get("questions"):
            parts.append(f"{ab['questions']} question(s) answered mid-task")
        if ab.get("handovers_landed"):
            parts.append(f"{ab['handovers_landed']} landed after a handover")
        lines.append("without waking Claude: " + " · ".join(parts))
    if g := f.get("guards"):
        lines.append("what stopped agents: " + " · ".join(f"{k} {v}" for k, v in
                                                        sorted(g.items(), key=lambda kv: -kv[1])))
    return lines
