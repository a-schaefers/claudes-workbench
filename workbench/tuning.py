"""Settings that tune themselves: each install learns, from its own finished tasks, the value of
each knob that works best for each model at the context window it is served.

A knob has a few candidate values and a default (a reasoned guess, the pick until evidence
says otherwise). At a task's start, choose() takes the value with the best record for that model
and window, each value's mean reward smoothed toward the record of all values (PRIOR tasks'
worth), among the default and the values tried TRY_MIN times, so one lucky task doesn't win; ties
go to the default. A knob that acts only on a filling session (fresh_at) learns only from the
tasks whose sessions reached its smallest value, and one that acts only after passing tests
(self_review) only from the tasks with a round whose tests passed. One task in EXPLORE tries a
neighbouring value instead, the one tried less, so evidence keeps growing beside the pick and
the pick walks toward a better value one step at a time, never jumping across the range.

A task's reward is how well it went for what it cost: accepted (and not taken over: Claude's
work isn't the agent's), discounted by the review rounds the agent caused a step (Claude's time
is the expensive part; a round Claude marked as the spec's gap isn't the agent's), less
COMPACT_PENALTY when OpenCode still had to compact a session, plus or minus up to SPEED_WEIGHT
for taking less or more time a step than that model's usual step of the same size. Discarded
scores 0. Neither a trial run (store.results) nor a discard Claude put down to its own spec
(judged) is evidence, and a value is judged only by the tasks it ran, so a task routed to
explore doesn't blur the pick's record.

A knob can have a ceiling per model (the runner's: fresh_at must come one step's growth, or
CEILING_MARGIN of the window, before OpenCode's compaction, which sits one reply and one step's
growth short of the window: #644), and values past it are never tried. A default past it falls
to the highest value under it; with every value past it, the ceiling itself is the only one (an
output cap that is a large share of the window: #595).

$WB_HOME/tuning.json pins a knob for everyone on this install: {"fresh_at": 0.6}. A name there
that is no knob (probe_aggressive, from before probe was off and on: the owner, 2026-11-22, #857)
is ignored, as is a results row's value for one: only KNOBS are chosen, scored or listed.
"""
from __future__ import annotations

import hashlib
import math
import statistics
from dataclasses import dataclass

from . import store

PRIOR = 2  # tasks' worth of the pooled record each value starts with
TRY_MIN = 3  # tasks a value other than the default needs before it can be the pick (#721)
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
    fills: bool = False  # acts only on a session that fills: a task whose sessions never reached
    # the smallest value (results' `peak_share`) is no evidence of it (#721)
    passed: bool = False  # acts only after a round whose tests passed: a task with no --test, or
    # whose tests never passed, ran alike under every value (results' `self_review_due`, #816)


KNOBS = {
    # The share of the window at which a filling session hands over to a fresh one (runner).
    # Earlier: more handovers, each re-reading files. Later: work in a crowded context, closer to
    # OpenCode's compaction. Which costs more depends on the model.
    "fresh_at": Knob((0.4, 0.5, 0.6, 0.7, 0.8), 0.6, "share of the window at which a fresh session takes over",
                     fills=True),
    # Whether the agent reviews its own change in one more round once the tests pass, before
    # Claude is woken (runner.SELF_REVIEW). It costs local time; a bug it catches saves Claude a
    # feedback round, a whole extra wake (2026-11-18: a trivial handoff came in under Claude
    # writing it only when the first change Claude saw was right). On by default; a model whose
    # own reviews don't spare Claude's rounds learns it off. On an API model it costs money
    # instead, a round Claude never sees: fixed off there unless Claude says --self-review
    # (runner.fixed_knobs, #1017), so neither the record nor the explore turns it on.
    "self_review": Knob((0, 1), 1, "whether the agent reviews its own change before Claude is woken",
                        passed=True),
    # (probe_aggressive, which chose probe's level, went with the levels: the owner, 2026-11-22,
    # #857: off and on, on is large files only)
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


def took_over(row: dict) -> bool:
    """Whether Claude took a step of the task over (wb takeover): what it accepted is Claude's."""
    return any(isinstance(s, dict) and s.get("took_over") for s in row.get("steps") or [])


def agent_landed(row: dict) -> bool:
    """Whether the agent's own work was accepted: accepted, and no step taken over. A task Claude
    finished after wb takeover counted as the agent's win in the router, the tuning and the skill
    records, so the model that most often needed taking over looked best, and its knob values
    scored highest (#647, the audit of main)."""
    return row.get("outcome") == "accepted" and not took_over(row)


def credit(row: dict) -> float:
    """How much of the task was the agent's approved work that was kept: 1 when agent_landed; an
    accepted chain with steps taken over, the share of its steps the agent did (#647); a discard
    whose earlier steps `wb next` committed and the branch kept (results' `landed_steps`), their
    share of its steps; else 0. Counted as a whole rejected task, a chain of four approved steps
    whose fifth stuck scored the model a failed try and every knob and skill it ran 0 (#722)."""
    steps = [s for s in row.get("steps") or [] if isinstance(s, dict)]
    if row.get("outcome") == "accepted":
        if not took_over(row):
            return 1.0
        return sum(not s.get("took_over") for s in steps) / max(1, len(steps))
    landed = row.get("landed_steps")
    if row.get("outcome") == "discarded" and isinstance(landed, int) and landed > 0:
        return min(1.0, landed / max(1, len(row.get("steps") or [])))
    return 0.0


def agent_reviews(row: dict) -> int:
    """The review rounds a task (or a step) needed that weren't the spec's fault (--spec-gap)."""
    return max(0, (row.get("reviews") or 0) - (row.get("spec_rounds") or 0))


def _steps(row: dict) -> int:
    return max(1, len(row.get("steps") or []))


def _step_minutes(row: dict) -> float | None:
    """The agent's active minutes per step (#543: not the wait for Claude's review)."""
    mins = row.get("active_minutes")
    if isinstance(mins, (int, float)) and not isinstance(mins, bool) and mins > 0:
        return mins / _steps(row)
    return None


def reward(row: dict, usual_minutes: float | None = None) -> float:
    """Per step, so a chain is set against single tasks fairly: a 5-step chain with one review a
    step scored 1/6 (#721). ``usual_minutes``: the model's usual minutes a step for the size."""
    share = credit(row)  # the agent's kept share: no takeover's (#647), a discard's landed steps (#722)
    if not share:
        return 0.0
    r = share / (1 + agent_reviews(row) / _steps(row))
    if any(s.get("compacted") for s in row.get("steps") or []):
        r -= COMPACT_PENALTY
    mins = _step_minutes(row)
    if usual_minutes and mins:
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


def acted(name: str, row: dict, ceiling=None) -> bool:
    """Whether the knob could have acted on the task: a `fills` knob, only when one of its
    sessions reached the smallest value (a row from before `peak_share` was recorded counts).
    Most tasks are small and never fill 40% of the window: one fast trivial task at 0.5 flipped
    the pick, one discard flipped it back (#721). A `passed` knob, only when a round's tests
    passed (`self_review_due`; a row from before it, when its last tests passed): a task without
    --test scored 0 or 1 though it ran alike under both, and could turn self-review off (#816)."""
    k = KNOBS[name]
    if k.passed:
        due = row.get("self_review_due")
        if not (due if isinstance(due, bool) else row.get("tests_ok") is True):
            return False
    peak = row.get("peak_share")
    return not k.fills or not isinstance(peak, (int, float)) \
        or peak >= min(allowed(name, ceiling)) - 1e-9


def _rows(model: str, window) -> list[dict]:
    """The rows for this model and window recorded on the weights it has now (router.same_weights:
    knobs learned on another model under the same id don't carry over, #726), under the ids it
    had before too (router.earlier: a renamed provider or alias started fresh, #818)."""
    from . import models, router  # router imports this module
    m = models.lookup(model)  # an API task's model too (#1013)
    ids = {model, *(router.earlier(m) if m else [])}
    return router.same_weights([r for r in store.results() if r.get("model") in ids
                                and r.get("window") == window], router.weights_now({model}))


def record(name: str, model: str, window, ceiling=None) -> dict:
    """Each allowed value's evidence for this model and window: {value: [rewards]}, each task's
    speed set against the usual minutes a step of its own size took (pooled over sizes, a
    trivial task beat a significant one's median every time, #721)."""
    rows = [r for r in _rows(model, window) if judged(r)
            and (r.get("tuned") or {}).get(name) is not None and acted(name, r, ceiling)]
    usual = {}
    for size in {r.get("size") for r in rows}:
        mins = [m for r in rows if r.get("size") == size and (m := _step_minutes(r))]
        usual[size] = statistics.median(mins) if mins else None
    out: dict = {v: [] for v in allowed(name, ceiling)}
    for r in rows:
        if r["tuned"][name] in out:
            out[r["tuned"][name]].append(reward(r, usual.get(r.get("size"))))
    return out


def scores(name: str, model: str, window, ceiling=None) -> dict:
    """Each allowed value's smoothed mean reward."""
    rec = record(name, model, window, ceiling)
    pooled = [x for xs in rec.values() for x in xs]
    prior = statistics.fmean(pooled) if pooled else 0.5
    return {v: (sum(xs) + PRIOR * prior) / (len(xs) + PRIOR) for v, xs in rec.items()}


def best(name: str, model: str, window, ceiling=None) -> float:
    """The best-scoring value among the default and those tried TRY_MIN times: an untried value
    sits at the pooled mean, so any one task above it won the pick (#721)."""
    d, sc = default(name, ceiling), scores(name, model, window, ceiling)
    rec = record(name, model, window, ceiling)
    ok = [v for v in sc if v == d or len(rec[v]) >= TRY_MIN]
    return max(ok, key=lambda v: (round(sc[v], 9), v == d, -abs(v - d)))


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
