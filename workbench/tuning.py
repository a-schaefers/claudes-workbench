"""Settings that tune themselves: each install learns, from its own finished tasks, the value of
each knob that works best for each model at the context window it is served.

A knob has a few candidate values and a default (a reasoned guess, the pick until evidence
says otherwise). At a task's start, choose() takes the value with the best record for that model
and window, each value's mean reward smoothed toward the record of all values (PRIOR tasks'
worth), so one lucky task doesn't win; ties go to the default. One task in EXPLORE tries a
neighbouring value instead, the one tried less, so evidence keeps growing beside the pick and
the pick walks toward a better value one step at a time, never jumping across the range.

A task's reward is how well it went for what it cost: accepted, discounted by the review rounds
the agent caused (Claude's time is the expensive part; a round Claude marked as the spec's gap
isn't the agent's), less COMPACT_PENALTY when OpenCode still had to compact a session, plus or
minus up to SPEED_WEIGHT for taking less or more time than that model's usual task. Discarded
scores 0. Neither a trial run (store.results) nor a discard Claude put down to its own spec
(judged) is evidence, and a value is judged only by the tasks it ran, so a task routed to
explore doesn't blur the pick's record.

A knob can have a ceiling per model (the runner's: fresh_at must come one step's growth, or
CEILING_MARGIN of the window, before OpenCode's compaction, which sits one reply and one step's
growth short of the window: #644), and values past it are never tried. A default past it falls
to the highest value under it; with every value past it, the ceiling itself is the only one (an
output cap that is a large share of the window: #595).

$WB_HOME/tuning.json pins a knob for everyone on this install: {"fresh_at": 0.6}.
"""
from __future__ import annotations

import hashlib
import math
import statistics
from dataclasses import dataclass

from . import store

PRIOR = 2  # tasks' worth of the pooled record each value starts with
EXPLORE = 0.2
COMPACT_PENALTY = 0.25
SPEED_WEIGHT = 0.15
# The least room between the handover point and OpenCode's compaction, as a share of the window
# (runner.fresh_ceiling: one step's growth, if more). With none, one tool result past the
# handover point compacted the session before the runner could hand it over (#595, #644)
CEILING_MARGIN = 0.05


@dataclass(frozen=True)
class Knob:
    values: tuple
    default: float
    help: str


KNOBS = {
    # The share of the window at which a filling session hands over to a fresh one (runner).
    # Earlier: more handovers, each re-reading files. Later: work in a crowded context, closer to
    # OpenCode's compaction. Which costs more depends on the model.
    "fresh_at": Knob((0.4, 0.5, 0.6, 0.7, 0.8), 0.6, "share of the window at which a fresh session takes over"),
    # Whether the agent reviews its own change in one more round once the tests pass, before
    # Claude is woken (runner.SELF_REVIEW). It costs local time; a bug it catches saves Claude a
    # feedback round, a whole extra wake (2026-11-18: a trivial handoff came in under Claude
    # writing it only when the first change Claude saw was right). On by default; a model whose
    # own reviews don't spare Claude's rounds learns it off.
    "self_review": Knob((0, 1), 1, "whether the agent reviews its own change before Claude is woken"),
}


def pinned() -> dict:
    return store.read_json(store.home() / "tuning.json", {}) or {}


def _key(row: dict) -> tuple:
    return row.get("model"), row.get("window")


def judged(row: dict) -> bool:
    """Whether a closed task says anything about the model, a knob or a skill: not a discard
    Claude put down to its own spec (--spec-gap), which would score the agent for Claude's miss;
    nor a task whose agent never ran (`ran` false: its setup or its model's load failed), which
    scored the model 0/3 for a lockfile problem or a sleeping server and locked it out of routing
    (#581). A row from before `ran` was recorded: not accepted, and no token written."""
    if row.get("outcome") == "discarded" and row.get("spec_gaps"):
        return False
    if "ran" in row:
        return bool(row["ran"])
    return row.get("outcome") == "accepted" or row.get("tok_out") != 0


def agent_reviews(row: dict) -> int:
    """The review rounds a task (or a step) needed that weren't the spec's fault (--spec-gap)."""
    return max(0, (row.get("reviews") or 0) - (row.get("spec_rounds") or 0))


def reward(row: dict, usual_minutes: float | None = None) -> float:
    if row.get("outcome") != "accepted":
        return 0.0
    r = 1 / (1 + agent_reviews(row))
    if any(s.get("compacted") for s in row.get("steps") or []):
        r -= COMPACT_PENALTY
    mins = row.get("active_minutes")  # the agent's time, not the wait for Claude's review (#543)
    if usual_minutes and isinstance(mins, (int, float)) and mins > 0:
        r -= SPEED_WEIGHT * max(-1.0, min(1.0, math.log2(mins / usual_minutes)))
    return r


def allowed(name: str, ceiling=None) -> list:
    """The knob's values at or under ``ceiling``; none is, the ceiling itself (or the smallest
    value, when that isn't positive: nothing fits, and runner.handover_warning says)."""
    vals = sorted(KNOBS[name].values)
    under = [v for v in vals if ceiling is None or v <= ceiling + 1e-9]
    if under:
        return under
    return [round(ceiling, 3)] if ceiling > 0 else vals[:1]


def default(name: str, ceiling=None):
    d, ok = KNOBS[name].default, allowed(name, ceiling)
    return d if d in ok else max(ok)


def record(name: str, model: str, window, ceiling=None) -> dict:
    """Each allowed value's evidence for this model and window: {value: [rewards]}."""
    rows = [r for r in store.results() if _key(r) == (model, window) and judged(r)
            and (r.get("tuned") or {}).get(name) is not None]
    mins = [r["active_minutes"] for r in rows if isinstance(r.get("active_minutes"), (int, float))
            and r["active_minutes"] > 0]
    usual = statistics.median(mins) if mins else None
    out: dict = {v: [] for v in allowed(name, ceiling)}
    for r in rows:
        if r["tuned"][name] in out:
            out[r["tuned"][name]].append(reward(r, usual))
    return out


def scores(name: str, model: str, window, ceiling=None) -> dict:
    """Each allowed value's smoothed mean reward."""
    rec = record(name, model, window, ceiling)
    pooled = [x for xs in rec.values() for x in xs]
    prior = statistics.fmean(pooled) if pooled else 0.5
    return {v: (sum(xs) + PRIOR * prior) / (len(xs) + PRIOR) for v, xs in rec.items()}


def best(name: str, model: str, window, ceiling=None) -> float:
    d, sc = default(name, ceiling), scores(name, model, window, ceiling)
    return max(sc, key=lambda v: (round(sc[v], 9), v == d, -abs(v - d)))


def choose(name: str, model: str, window, task_name: str, ceiling=None) -> tuple[float, bool]:
    """(value, explored) for a new task: a pin, else the best, else (one task in EXPLORE, by the
    task's name, so a restarted runner picks the same) the less-tried neighbour of the best."""
    pin = pinned().get(name)
    if pin is not None:
        return pin, False
    pick = best(name, model, window, ceiling)
    if int(hashlib.sha1(f"{name}\0{task_name}".encode()).hexdigest(), 16) % 1000 >= EXPLORE * 1000:
        return pick, False
    vals = allowed(name, ceiling)
    i = vals.index(pick)
    near = [vals[j] for j in (i - 1, i + 1) if 0 <= j < len(vals)]
    if not near:
        return pick, False
    rec = record(name, model, window, ceiling)
    return min(near, key=lambda v: (len(rec[v]), v)), True


def value(task: store.Task, name: str):
    """The knob's value for this task: what was chosen at its start, else the default."""
    return (task.status.get("tuned") or {}).get(name, KNOBS[name].default)


def tune(task: store.Task, model: str, window, ceilings: dict | None = None,
         fixed: dict | None = None) -> None:
    """Fix every knob for this task once (a restarted runner keeps them). `fixed`: what Claude
    set for this task itself (a flag), which wins over the record and a pin."""
    if task.status.get("tuned") is not None:
        return
    ceilings, fixed = ceilings or {}, fixed or {}
    tuned, explored = {}, []
    for name in KNOBS:
        v, e = (fixed[name], False) if name in fixed else \
            choose(name, model, window, task.name, ceilings.get(name))
        tuned[name] = v
        if e:
            explored.append(name)
    task.set_status(tuned=tuned, explored=explored, tuned_max=ceilings)


def _ceiling(name: str, model: str, window):
    """The ceiling the latest task on this model and window ran under."""
    rows = [r for r in store.results() if _key(r) == (model, window) and r.get("tuned_max")]
    return rows[-1]["tuned_max"].get(name) if rows else None


def table() -> str:
    keys = sorted({_key(r) for r in store.results() if r.get("tuned")}, key=str)
    pin = pinned()
    out = []
    for name, knob in KNOBS.items():
        out.append(f"{name}: {knob.help} (default {knob.default}"
                   + (f"; pinned to {pin[name]} in tuning.json)" if name in pin else ")"))
        if not keys:
            out.append("  no evidence yet: every model starts at the default")
        for model, window in keys:
            top = _ceiling(name, model, window)
            rec, sc = record(name, model, window, top), scores(name, model, window, top)
            pick = best(name, model, window, top)
            cells = "  ".join(f"{v}: {len(rec[v])}×{sc[v]:.2f}{'←' if v == pick else ''}"
                              for v in sorted(rec))
            out.append(f"  {model} @ {window}: {cells}" + (f"  (at most {top})" if top else ""))
    return "\n".join(out) + "\n(value: tasks × smoothed reward; ← the pick)"
