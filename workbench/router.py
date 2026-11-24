"""Which model gets a task: an educated guess, corrected by how real tasks went.

Real delegated work (results.jsonl: accepted or not, and how long it took) is the evidence.
Quality is the acceptance rate with a uniform prior, (accepted + 1) / (tries + 2), so one lucky
task doesn't crown a model. Work of the same kind counts most: a task in a repo of the same stack
(its main languages, gitops.stack) is judged first by that model's record on that stack, with its
overall record as the prior worth LIKE_PRIOR tasks, so a model strong in one language and weak in
another is routed by the one at hand once there is evidence, and by its whole record until then. Each row weighs half as much every HALF_LIFE_DAYS, so a stale record
recovers, and one task in EXPLORE_ONE_IN goes to a model the record left out on fewer than
EXPLORE_TRIES tasks, so one unlucky task doesn't dethrone one (#648). Among the models within QUALITY_MARGIN of the best, the guess
decides: for significant work the largest model (more parameters, better judgment), for trivial
work the smallest (faster), and at equal size the one already on the GPU. Claude's tokens are
what workbench saves, so once two of those models each have COST_MIN tasks of the size with what
they cost Claude recorded (results' `claude`: the session's calls on the task), one that costs
Claude COST_MARGIN less a landed step (#903) than the guess's pick goes first: on 2026-11-18 the smaller,
faster model the guess sends trivial work to cost Claude more, in its review and fixes, than a
larger one whose first change was right. A model too big for
the GPU (models.need over its budget) spills into RAM and runs several times slower, so in that
band every model that fits comes first.

The user's policy can narrow that: a model whose models.json entry has ``"sizes": ["trivial"]``
is only ever routed trivial tasks, whatever it scores elsewhere (`wb model sizes`).

How much one task should hold on a model (its slice) is learned the same way. The guess scales
with the context window the model is served, from what was measured on a 32K one; each closed
step then says how a slice that size went (results.step): well when it landed with at most one
review round the agent caused, badly when a session compacted or filled up with reading before
any change, the agent got stuck, or it took BAD_REVIEWS rounds. Handing over to a fresh session isn't held against a step: that is how the
runner gets through one. Of the latest steps, the size most good ones reached sets a floor and
the smallest that went badly a ceiling, so a big window on a strong machine earns big tasks.

How long the agent may think, or go quiet, before the runner acts (budgets) is learned too: each
round's session says how fast the model reads and writes on this machine (record_speed).
"""
from __future__ import annotations

import functools
import hashlib
import math
import statistics
import time
import types
from dataclasses import dataclass
from pathlib import Path

from . import gitops, models, spend, store, tuning

QUALITY_MARGIN = 0.15
COST_MIN = 3        # tasks of a size with Claude's spend recorded before it can move the pick
COST_MARGIN = 0.15  # how much less a task must cost Claude to beat the guess
LIKE_PRIOR = 2  # how many tasks of the same stack the overall record weighs as
# The record forgets and explores (#648, the audit of main). One discard on a model's first task
# (Claude's own wrong test) put it 0.17 below a model with no record, out of the band for good,
# and a week misconfigured held a model down forever. So a row weighs half as much each
# HALF_LIFE_DAYS, and one task in EXPLORE_ONE_IN (by its name, as tuning.EXPLORE does) goes to
# the usable model outside the band with the fewest tasks of the size, while it has fewer than
# EXPLORE_TRIES.
HALF_LIFE_DAYS = 60
EXPLORE_ONE_IN = 10
EXPLORE_TRIES = 3
SIZES = ("trivial", "significant")
# Measured 2026-11-16 on a 32K window: 2-3 files and ~150 new lines landed in one session; eight
# files took an hour of compactions and drift. A bigger window is guessed to hold proportionally
# more, up to GUESS_MAX times that; past it, only steps that fit earn more.
BASE_WINDOW, BASE_FILES, BASE_LINES = 32768, 3, 150
GUESS_MAX = 4
BAD_REVIEWS = 2  # review rounds the agent caused at which a step held too much
SLICE_RECENT = 20  # the measured steps the slice is learned from, the latest
FIT_MIN, FIT_SHARE = 2, 0.75  # good steps before they raise it, and the share of them it covers


@dataclass
class Score:
    model: models.Model
    state: dict
    weights: int = 0               # bytes of weights, 0 when unknown
    tries: int = 0
    wins: float = 0                # accepted, and a discard's landed share (tuning.credit)
    minutes: float | None = None   # median active minutes of a real task of this size
    minutes_n: int = 0             # the tasks it is the median of
    fits: bool | None = None       # its need within the GPU's budget; None when unknown
    stack: tuple = ()              # the kind of work asked about, and the record on it
    like_tries: int = 0
    like_wins: float = 0
    claude: float | None = None    # input-equivalent tokens a landed step cost Claude (spend.py)
    claude_n: int = 0              # the tasks it is measured over
    cheaper: bool = False          # put ahead of the guess for costing Claude less (_cheaper)
    explored: bool = False         # put first to explore it (choose's task_name)
    # the record weighed by recency (HALF_LIFE_DAYS); None: as counted
    w: tuple | None = None         # (tries, wins, like_tries, like_wins)
    guessed: bool = False          # weights guessed from its name: the server says no size (#704)

    @property
    def quality(self) -> float:
        tries, wins, like_tries, like_wins = self.w or (self.tries, self.wins, self.like_tries, self.like_wins)
        overall = (wins + 1) / (tries + 2)
        return (like_wins + LIKE_PRIOR * overall) / (like_tries + LIKE_PRIOR)

    def line(self) -> str:
        if not self.tries:
            return "no real tasks yet"
        t = f", ~{self.minutes:.1f} min of active work a step" if self.minutes is not None else ""
        like = f", {round(self.like_wins, 1):g}/{self.like_tries} on {'/'.join(self.stack)}" if self.like_tries else ""
        c = f", ~{self.claude / 1000:.0f}K Claude tokens a landed step" if self.claude_n >= COST_MIN else ""
        return f"{round(self.wins, 1):g}/{self.tries} real tasks accepted{like}{t}{c}"


@functools.cache
def _repo_stack(repo: str, subdir: str | None = None) -> tuple:
    return tuple(gitops.stack(repo, subdir)) if Path(repo).is_dir() else ()


def row_stack(r: dict) -> tuple:
    """A result's stack: recorded since it was, else read from its repo (its package's, #820)
    while that exists."""
    if r.get("stack") is not None:
        return tuple(r["stack"])
    return _repo_stack(r["repo"], r.get("subdir")) if r.get("repo") else ()


def _up(over: dict, model_id: str, key: str) -> list[str]:
    """The ids models.json's ``key`` leads to from ``model_id``, nearest first (a loop stops)."""
    out, at = [], model_id
    while isinstance(e := over.get(at), dict) and isinstance(nxt := e.get(key), str) \
            and nxt != model_id and nxt not in out:
        out.append(nxt)
        at = nxt
    return out


def _root(over: dict, model_id: str) -> str:
    return (_up(over, model_id, "variant_of") or [model_id])[-1]


# A model's record (score, slice, knobs, speed, the Stats line) was keyed by its OpenCode id, so
# renaming the provider (`ollama` to `local`), moving from `name:latest` to its explicit tag or
# renaming a llama-swap alias started it fresh: behind the models with a record, on the window's
# guess, the default knobs and the default budgets, which stall on a slow machine (#818).
def earlier(m: models.Model, probe: bool = True, weights: str | None = None) -> list[str]:
    """The ids ``m`` had before, nearest first: models.json `renamed_from` (wb model rename),
    followed back; and, with ``probe`` (a server read, cached: models.identity) or ``weights``
    (its identity, already known: task.json `weights_id`), each id no longer discovered whose
    newest results row ran on the Ollama digest ``m`` has now. Only a digest: a `bytes:` size
    matches other weights too often to be called the same model."""
    over = models._overrides()
    out = _up(over, m.id, "renamed_from")
    if (w := weights or (models.identity(m) if probe else None)) and w.startswith("ollama:"):
        newest = {r["model"]: r.get("weights_id") for r in store.results() if r.get("model")}
        if any(v == w for k, v in newest.items() if k != m.id):
            found = models.discover()
            out += [k for k, v in newest.items()
                    if v == w and k != m.id and k not in found and k not in out]
    return out


def lineage(m: models.Model, probe: bool = True) -> set[str]:
    """The ids that share ``m``'s record: a `wb optimize apply` variant (models.json
    `variant_of`), its source, and the source's other variants, followed to the first source: a
    variant of a variant (an apply after the GPU changed) once named only the one it came from,
    and the first source's record split off (#723). One model at two windows is one model's
    record (#584). And the ids it had before (earlier, #818)."""
    over = models._overrides()
    if m.extra.get("variant_of") and not (over.get(m.id) or {}).get("variant_of"):
        over = {**over, m.id: {**(over.get(m.id) or {}), "variant_of": m.extra["variant_of"]}}
    root = _root(over, m.id)
    return {m.id, root, *earlier(m, probe)} | {
        k for k, v in over.items() if isinstance(v, dict) and v.get("variant_of")
        and _root(over, k) == root}


def replaced(ids) -> set[str]:
    """The models a newer `wb optimize apply` variant among ``ids`` stands in for: the source
    (`variant_of`) and every variant it was made from (`replaces`), however far back, so only
    the newest leads (#723: two variants left the router picking the spilling source)."""
    over = models._overrides()
    out = set()
    for i in ids:
        for key in ("variant_of", "replaces"):
            for up in _up(over, i, key):
                out.add(up)
                out |= {u for k in ("variant_of", "replaces") for u in _up(over, up, k)}
    return out


def weights_now(ids) -> dict:
    """{id: models.identity} for the discovered ones of ``ids``."""
    found = models.discover()
    return {i: models.identity(found[i]) for i in ids if i in found}


def same_weights(rows: list[dict], now: dict) -> list[dict]:
    """The rows recorded on the weights each model has now (`now`: weights_now): a row from
    before `weights_id` was recorded, or a model whose weights can't be told, counts (#726)."""
    return [r for r in rows if not (w := r.get("weights_id")) or now.get(r.get("model")) in (None, w)]


def weights_changed(m: models.Model) -> bool:
    """Whether every recorded task of the model ran on other weights than it has now: its record
    starts fresh (`wb models` says so)."""
    now = weights_now({m.id}).get(m.id)
    seen = {r.get("weights_id") for r in store.results() if r.get("model") == m.id} - {None}
    return bool(now and seen and now not in seen)


def record_role(role: str | None) -> str | None:
    """The role whose record ranks and times a task, given task.json's or wb start's `role`:
    writing's own (#1025), else None, its size's, as before roles. #1016 writes a role for every
    task, and a coding one read as its role timed a `--size` other than its role's on the wrong
    record (wave 2's merge)."""
    return role if role == "writing" else None


def of_role(row: dict, role: str) -> bool:
    """Whether a results row is a record of ``role``'s work: its `role`, else its `size` (a row
    from before the roles). Writing slices like significant work (decision 1 of #1008), but a
    model's prose results moved its coding rank and the other way round: each counts apart (#1025)."""
    return tuning.work(row) == role


def score(m: models.Model, size: str, state: dict | None = None, stack=(), role: str | None = None) -> Score:
    """``role``: the record it ranks on (of_role), the size's when None."""
    state = state or models.status(m)
    ids = lineage(m, probe=state["available"])  # a server down isn't asked its weights
    real = [r for r in store.results() if r.get("model") in ids and of_role(r, role or size)
            and tuning.judged(r) and same_kind(r, m)]
    # the weights each model has now: a server just found down isn't asked again (the audit)
    real = same_weights(real, weights_now(ids) if state["available"] else {})
    # a model that can't run isn't weighed: its server was just found down, and asking it again
    # cost seconds per model on a sleeping LAN box (the 2026-11-16 audit)
    s = Score(model=m, state=state, stack=tuple(stack),
              weights=models.weights(m) if state["available"] or m.extra.get("weights_gb") else 0)
    if not s.weights and state["available"] and (guess := models.guessed_weights(m)):
        s.weights, s.guessed = guess, True  # ordered by its name's size, not the config's (#704)
    s.tries = len(real)
    s.wins = sum(tuning.credit(r) for r in real)  # the agent's kept share (#647, #722)
    like = [r for r in real if set(row_stack(r)) & set(stack)] if stack else []
    if stack:
        s.like_tries = len(like)
        s.like_wins = sum(tuning.credit(r) for r in like)
    now = time.time()
    s.w = (sum(_weight(r, now) for r in real), sum(_weight(r, now) * tuning.credit(r) for r in real),
           sum(_weight(r, now) for r in like), sum(_weight(r, now) * tuning.credit(r) for r in like))
    s.minutes, s.minutes_n = _typical(real)
    s.claude, s.claude_n = _per_step(real)
    return s


def _per_step(real: list[dict]) -> tuple[float | None, int]:
    """What Claude spent on the rows with a `claude`, over the steps of theirs that landed as the
    agent's (credit x steps), and how many rows that is. The mean a task set a model whose record
    held `wb next` chains (a row is the whole chain's cost) as dear beside one that ran single
    tasks, and counted a discard, which records only what Claude spent before giving up, as a
    cheap task (#903, the third audit of 2026-11-21). None while no step landed."""
    rows = [r for r in real if isinstance(r.get("claude"), dict)]
    landed = sum(tuning.credit(r) * tuning._steps(r) for r in rows)
    if not landed:
        return None, 0
    return sum(spend.weighted(r["claude"]) for r in rows) / landed, len(rows)


def _typical(real: list[dict]) -> tuple[float | None, int]:
    """The median of the rows' active minutes a step, and how many rows it is of."""
    # the agent's active work, median: wall minutes held the queue, Claude's review and a night
    # before an accept (480 min for a 12-minute task), and a mean let one such row set every
    # ETA and progress check at many times the real length (#543). Rows without it are skipped.
    # A step's: a `wb next` chain's row holds every step's minutes, and set beside single tasks
    # it put a step past "its typical" minutes into its chain (#815), as tuning's reward does
    times = [t for r in real if (t := tuning._step_minutes(r)) is not None]
    return (statistics.median(times) if times else None), len(times)


def _weight(r: dict, now: float) -> float:
    """A row's weight in the record: half for each HALF_LIFE_DAYS of age; 1 without a time."""
    ts = r.get("ts")
    if not isinstance(ts, (int, float)) or isinstance(ts, bool):
        return 1.0
    return 0.5 ** (max(now - ts, 0) / 86400 / HALF_LIFE_DAYS)


def explores(task_name: str | None) -> bool:
    """Whether this task is one the router sends to explore (one in EXPLORE_ONE_IN, by name)."""
    if not task_name:
        return False
    return int(hashlib.sha1(f"route\0{task_name}".encode()).hexdigest(), 16) % EXPLORE_ONE_IN == 0


def unproven(scores: list[Score]) -> list[Score]:
    """The usable models outside the band with fewer than EXPLORE_TRIES tasks of the size, fewest
    first: those the record left out before it could say much. One that would spill out of the
    GPU only when the pick would too."""
    band = [s for s in scores if s.state["available"]][:1]
    if not band:
        return []
    best = max(s.quality for s in scores if s.state["available"])
    head = band[0]
    out = [s for s in scores if s.state["available"] and s.quality < best - QUALITY_MARGIN
           and s.tries < EXPLORE_TRIES and (s.fits is not False or head.fits is False)]
    return sorted(out, key=lambda s: (s.tries, -s.quality))


def _cheaper(close: list[Score]) -> list[Score]:
    """The band with the model measured to cost Claude least first, when it beats the guess's
    pick by COST_MARGIN and both have COST_MIN tasks of evidence; else as the guess ordered it.
    A model that would spill out of the GPU never jumps one that fits."""
    if not close or close[0].claude_n < COST_MIN:
        return close
    head = close[0]
    rivals = [s for s in close[1:] if s.claude_n >= COST_MIN
              and (s.fits is not False or head.fits is False)]
    best = min(rivals, key=lambda s: s.claude, default=None)
    if best is None or best.claude > (1 - COST_MARGIN) * head.claude:
        return close
    best.cheaper = True
    return [best] + [s for s in close if s is not best]


def expected_minutes(model_id: str, size: str, weights: str | None = None,
                     role: str | None = None) -> float | None:
    """How long a step of this size usually takes on this model, from real tasks: the median of
    their active minutes a step (the agent's own working time), so compare it with a step's active
    time. ``weights``: the task's own (task.json `weights_id`): rows on other weights left out."""
    return expected(model_id, size, weights, role)[0]


def expected(model_id: str, size: str, weights: str | None = None,
             role: str | None = None) -> tuple[float | None, int]:
    """expected_minutes, and how many timed tasks it is the median of. From the files alone: a
    whole score() asked the server for the weights and the identity it never used, and every
    `wb watch --json` the pane ran each 5 s spent ~5 s on a sleeping LAN box's timeouts (#819)."""
    m = models.lookup(model_id, cached=True)  # an API task's too; the pane polls: no rebuild
    if not m:
        return None, 0
    ids = lineage(m, probe=False)
    real = [r for r in store.results() if r.get("model") in ids and of_role(r, role or size)
            and tuning.judged(r) and same_kind(r, m)]
    return _typical(same_weights(real, {m.id: weights} if weights else {}))


def same_kind(row: dict, m: models.Model) -> bool:
    """Whether a row was run as the model's kind is (a row without `kind` is a GPU one): an
    Ollama `:cloud` id is a GPU model's in old rows and an API model's now (#1013), and one
    record mixing the two ranked an API model by GPU tasks (#1022). score's rows feed _per_step
    and _cheaper, so they rank within a kind too; slice and tuning._rows keep to it as well."""
    return (row.get("kind") or "gpu") == m.kind


def allowed(m: models.Model, size: str) -> bool:
    return size in (m.extra.get("sizes") or SIZES)


def _guess(size: str):
    """Sort key within the top band: the largest model for significant work, the smallest for
    trivial, then the one already loaded."""
    sign = -1 if size == "significant" else 1
    return lambda s: (s.fits is False, not s.weights, sign * s.weights,  # unknown size: last
                      not s.state.get("resident"))


def fits(m: models.Model, budget: int | None) -> bool | None:
    """Whether the model fits the GPU whole; None when the machine or the model can't say.
    Ollama's placement says yes once it is loaded all in VRAM; before that its weights (or
    models.json's `vram_gb`) must fit, the context left out, since that depends on how the
    server stores it: only a model that can't fit is held back."""
    if m.remote:
        return None
    if models.all_on_gpu(m):
        return True
    n = int(float(m.extra["vram_gb"]) * 1e9) if m.extra.get("vram_gb") else models.weights(m)
    return n <= models.SHARE_AT * budget if n and budget else None


def rank(size: str, stack=(), down: set | None = None, catalog: dict | None = None,
         role: str | None = None) -> list[Score]:
    """Every discovered model's case for a task of this size (in a repo of this stack), best
    first (the user's policy leaves some out: allowed). ``down`` and ``catalog``: what this pass
    has learnt of the servers (models.status), the caller's to share across calls: each model's
    status asked its server afresh, so a wb remote box asleep cost 2.5 s a model (#705)."""
    down = set() if down is None else down
    catalog = {} if catalog is None else catalog
    scores = [score(m, size, state=models.status(m, down, catalog), stack=stack, role=role)
              for m in models.discover().values() if allowed(m, size)]
    # each model against its own server's cards: with one Ollama per GPU, one budget for all (the
    # first process's) held an 18 GB model on the 24 GB card's server back as not fitting the 8 GB
    # one, and passed a model too big for the 8 GB card (#801); read once per server this pass
    by_root: dict = {}
    for s in scores:
        if s.state["available"] and s.model.root not in by_root:
            by_root[s.model.root] = None if s.model.remote else models.gpu_budget(s.model.root)
        s.fits = fits(s.model, by_root[s.model.root]) if s.state["available"] else None
    usable = [s for s in scores if s.state["available"]]
    best = max((s.quality for s in usable), default=0)
    # a source whose optimize variant can run goes after it: the variant is the window measured
    # to fit, with the same record (#584)
    stood_in = replaced(s.model.id for s in usable)
    guess = _guess(size)
    close = _cheaper(sorted((s for s in usable if s.quality >= best - QUALITY_MARGIN),
                            key=lambda s: (s.model.id in stood_in, *guess(s))))
    rest = sorted((s for s in usable if s not in close), key=lambda s: -s.quality)
    blocked = [s for s in scores if not s.state["available"]]
    return close + rest + blocked


def api_choice(role: str) -> str:
    """The refusal's other choice when a provider has a key wb can hold (#1024): the user may put
    the role on an API model, on their yes; '' with none (decision 2: no fallback, Claude asks),
    and '' while the user's Agents switch holds every role on the GPU: a role's own API model
    would only apply once Agents is Both, so the line led round a dialog that changed nothing
    that runs (#1024's review)."""
    from . import routes  # it imports this module lazily too
    try:
        if routes.read().get("use") == "gpu":
            return ""
    except routes.RoutesError:  # wb start refuses that before the router (routes.resolve)
        pass
    try:
        keys = models.connected() or {}
    except (RuntimeError, OSError):
        keys = {}
    if not any(k["key"] in models.HELD_KEYS for k in keys.values()):
        return ""
    return (f"; or put {role} on an API model you connected (the user's yes): wb route {role} <id>, "
            "wb models --api <text> lists them")


def choose(size: str, stack=(), task_name: str | None = None, down: set | None = None,
           catalog: dict | None = None, role: str | None = None) -> tuple[Score | None, str]:
    """The model for a task of this size (in a repo of this stack) and one line on why. With
    ``task_name``, one task in EXPLORE_ONE_IN explores a model outside the band (unproven);
    ``down``, ``catalog``: rank's; ``role``: the task's role (wb route's), which picks the record
    it ranks on (record_role: writing's own, else the size's). None and the refusal when no GPU
    model can take it, which names the API choice for ``role`` (the size's name when not given)
    when a key is stored (#1024)."""
    record = record_role(role)
    ranked = rank(size, stack, down, catalog, record)
    work = record or size  # whose record the line speaks of
    if explores(task_name) and (new := unproven(ranked)):
        new[0].explored = True
        ranked = [new[0]] + [s for s in ranked if s is not new[0]]
    top = ranked[0] if ranked and ranked[0].state["available"] else None
    if top is None:
        if ranked:
            return None, "no GPU model can run: " + "; ".join(
                f"{s.model.id}: {s.state['why']}" + (f" ({fix})" if (fix := models.remedy(s.model, s.state)) else "")
                for s in ranked) + api_choice(role or size)
        if found := models.discover():  # all held back by the size policy, not missing (the 2026-11-16 audit)
            return None, (f"no GPU model takes {size} tasks ({', '.join(found)}: limited by wb model "
                          f"sizes; wb model sizes <id> trivial significant allows one, or pass --model)"
                          + api_choice(role or size))
        return None, "no GPU models: " + models.why_none() + api_choice(role or size)
    usable = [s for s in ranked if s.state["available"]]
    sizes = [s.weights for s in usable if s.fits is not False or top.fits is False]
    unknown = [s.model.id for s in usable if not s.weights]
    if len(usable) == 1:
        pick = f"the only model allowed {size} work"
    elif top.explored:
        pick = (f"exploring: its record on {work} work is {top.tries} task(s), too few to leave it "
                f"out (one task in {EXPLORE_ONE_IN})")
    elif top.cheaper:
        pick = f"costs Claude least on {work} work"
    elif size in ("significant", "trivial") and not top.weights:
        # every weight 0 made it "the largest" and "the smallest" at once, by config order (#704)
        pick = "sizes unknown: config order; models.json weights_gb sets a model's size"
    elif size == "significant" and top.weights == max(sizes):
        pick = "the largest model allowed significant work" + (
            " that fits the GPU" if top.fits and any(s.fits is False for s in usable) else "")
    elif size == "trivial" and top.weights == min(sizes):
        pick = "the smallest model allowed trivial work"
    else:
        kind = f"{work} {'/'.join(stack)}" if top.like_tries else work
        pick = f"the best record on {kind} work"
    if top.guessed and pick.startswith(("the largest", "the smallest")):
        pick += " (its size guessed from its name)"
    if unknown and top.weights and size in ("significant", "trivial"):
        pick += f" ({', '.join(unknown[:3])}: size unknown, models.json weights_gb sets it)"
    others = "; ".join(f"{s.model.id} {s.line()}" for s in usable[1:4])
    return top, f"{top.model.id}: {pick}; {top.line()}" + (f" (vs {others})" if others else "")


@dataclass
class Slice:
    files: int
    lines: int
    why: str

    def line(self) -> str:
        return f"~{self.files} files, ~{self.lines} new lines per task ({self.why})"


def slice(m: models.Model) -> Slice:
    """How much one task should hold on this model: the window's guess, corrected by real steps."""
    window = (m.config.get("limit") or {}).get("context")
    scale = min(window / BASE_WINDOW, GUESS_MAX) if window else 1
    files, lines = max(1, round(BASE_FILES * scale)), max(50, round(BASE_LINES * scale / 10) * 10)
    basis = f"{window // 1024}K window" if window else "window unknown"
    ids = {m.id, *earlier(m)}  # under the ids it had before too (#818)
    rows = same_weights([r for r in store.results() if r.get("model") in ids and tuning.judged(r)
                         and same_kind(r, m)],  # its own kind's steps only (#1022's review)
                        weights_now({m.id}))  # another model's steps under the same id (#726)
    steps = [s for r in rows for s in r.get("steps") or []
             if s.get("files") and (not window or s.get("window") in (None, window))]
    every = len(steps)
    steps = steps[-SLICE_RECENT:]  # the latest say how it does now
    went = [_went(s) for s in steps]
    fit = [s for s, w in zip(steps, went) if w == "well"]
    over = [s for s, w in zip(steps, went) if w == "badly"]
    # Below the smallest recent step that went badly; raised to the size most recent good steps
    # reached (FIT_SHARE of them, once there are FIT_MIN), unless the steps of the bad one's size
    # or more mostly went badly. The biggest good step ever once won over every cap: one lockfile
    # bump, or a rename across 30 files, set the slice for good, and 30 failures at a sixth of its
    # size couldn't lower it (#720).
    got = {"files": files, "lines": lines}
    for key, least, cap in (("files", 1, lambda n: n - 1), ("lines", 50, lambda n: n * 3 // 4)):
        bad = min((s[key] for s in over), default=None)
        if bad is not None:
            got[key] = min(got[key], max(least, cap(bad)))
        if len(fit) >= FIT_MIN:
            sizes = sorted(s[key] for s in fit)
            reach = sizes[math.ceil(FIT_SHARE * len(sizes)) - 1]
            there = [w for s, w in zip(steps, went) if bad is not None and s[key] >= bad]
            if bad is None or reach < bad or there.count("badly") <= there.count("well"):
                got[key] = max(got[key], reach)
    files, lines = got["files"], got["lines"]
    if not steps:
        return Slice(files, lines, f"a guess from its {basis}; no steps measured yet" if window
                     else "a guess, its window unknown; no steps measured yet")  # not "from its window unknown" (the 0.90 review)
    last = f"the last {len(steps)}" if every > len(steps) else f"{len(steps)}"
    return Slice(files, lines, f"{basis}; {len(fit)} of {last} measured steps went well, "
                 f"{len(over)} badly")


def _went(s: dict) -> str | None:
    """"well", "badly", or None (dropped or taken over, nothing to say about its size). A step
    recorded before handovers were normal has `overflow` set by them alone: it isn't read."""
    if s.get("compacted") or s.get("read_full") or s.get("stuck") \
            or tuning.agent_reviews(s) >= BAD_REVIEWS:
        return "badly"
    return "well" if s["landed"] and not s.get("took_over") else None


# ---- budgets: how long a round may think or go quiet, from the model's measured speed ----
# THINK_DEFAULT_S (420 s) was set on a 27B that writes ~36 tokens/s and reads ~750/s (measured
# 2026-11-16): about 13.5K tokens of thinking plus reading a full window. A model measured slower
# on this machine gets the same work in its own time, and the silence and round limits scale with
# it, so a slow machine isn't cut off. None goes below its default.
THINK_DEFAULT_S, SILENCE_DEFAULT_S, ROUND_DEFAULT_S = 420, 900, 2400
THINK_TOKENS = 13500
THINK_MAX = 1800
SPEED_KEEP = 60  # samples kept per model and measure


def _speed_path():
    # this machine's (store.machine_dir): shared on a network home, a fast desktop's samples
    # outvoted a laptop's in the median, and the laptop's rounds ran on the desktop's limits (#901)
    return store.machine_dir() / "speed.json"


def record_speed(model_id: str, messages: list[dict], api: bool | None = None) -> dict:
    """Add a session's measured speeds to speed.json: writing (output and reasoning tokens over
    the time from first token to done) and reading (new input tokens over the time to first token).
    A request cut short (a nudge, a stop) still says how fast it read: on a slow machine the
    first request is often the one interrupted, and the reading was never learned (the
    2026-11-16 audit). ``api``: whether it is an API model, as the task recorded it (the runner's
    api_entry), else the cached catalog's word (models.is_api)."""
    got = speed_samples(messages)
    write, read = got["write"], got["read"]
    if models.is_api(model_id) if api is None else api:  # the account's, not this machine's (#1017)
        _record_api_speed(model_id, write, got["ttft"])
        return got
    if not (write or read):
        return got
    path = _speed_path()
    with store.locked(path.with_suffix(".lock")):
        data = store.read_json(path, {}) or {}
        row = data.setdefault(model_id, {})
        row["write"] = (row.get("write", []) + write)[-SPEED_KEEP:]
        row["read"] = (row.get("read", []) + read)[-SPEED_KEEP:]
        store.write_json(path, data)
    return got


def record_read_bound(model_id: str, tok_s: int) -> None:
    """Add a read speed that a request which never got its first token was at most (the
    runner's read_bound, #491) to speed.json's reading samples. Never an API model's: its time
    to first token is the network and the provider's queue, not how fast it reads (#1017)."""
    if models.is_api(model_id):
        return
    path = _speed_path()
    with store.locked(path.with_suffix(".lock")):
        data = store.read_json(path, {}) or {}
        row = data.setdefault(model_id, {})
        row["read"] = (row.get("read", []) + [tok_s])[-SPEED_KEEP:]
        store.write_json(path, data)


def speed_samples(messages: list[dict]) -> dict:
    """{"write", "read", "ttft"}: each request's write and read speed (tok/s; the thresholds keep
    tiny requests from skewing them) and its time to first token (seconds), from a session
    export's messages."""
    write, read, ttft = [], [], []
    for msg in messages:
        t, tk = msg.get("time") or {}, msg.get("tokens") or {}
        made, first, done = t.get("created"), t.get("firstToken"), t.get("completed")
        if msg.get("type") != "assistant" or not (made and first):
            continue
        ttft.append(round((first - made) / 1000, 2))
        out = (tk.get("output") or 0) + (tk.get("reasoning") or 0)
        if done and out >= 100 and done - first > 1000:
            write.append(round(out / ((done - first) / 1000), 1))
        if (tk.get("input") or 0) >= 1000 and first - made > 500:
            read.append(round(tk["input"] / ((first - made) / 1000)))
    return {"write": write, "read": read, "ttft": ttft}


def speed(model_id: str, weights: str | None = None) -> dict:
    """{"write", "read"}: the model's median tokens per second on this machine, where measured;
    unmeasured under this id, under the nearest id it had before (earlier, #818), else the
    `wb optimize apply` variant it replaced or the source it is a variant of (#906).

    No server is asked: `wb top` and the Stats tab's feed call this for every running task and
    rack row on their tick, and earlier()'s identity read cost 2.5 s an unreachable Ollama root,
    ~4 s a model on another server, frozen with a few on a sleeping LAN box (#905, as #819 was
    for expected). The digest link only from ``weights``, the identity the caller already has
    (task.json `weights_id`: wb start, the runner's budgets)."""
    def row_in(path: Path) -> dict:
        data = store.read_json(path, {}) or {}
        row = data.get(model_id) or {}
        if not row and data:
            over = models._overrides()
            ids = _up(over, model_id, "renamed_from")
            if weights and (m := models.discover().get(model_id)):
                ids = earlier(m, probe=False, weights=weights)
            # a variant's first round got the default budgets, though it shares its source's record:
            # stopped as stuck on a slow machine, the step went into the shared slice as gone badly
            # (#906). The variant it replaced first (made for this GPU too), then the source, whose
            # speed is the variant's at worst: it spilled, so the budgets err long
            ids += [i for key in ("replaces", "variant_of") for i in _up(over, model_id, key) if i not in ids]
            row = next((data[i] for i in ids if data.get(i)), {})
        return row
    if write := api_speed(model_id).get("write"):  # an API model's: the account's (#1017)
        return {"write": write}
    row = row_in(_speed_path())
    if not row and (shared := store.home() / "speed.json") != _speed_path():
        row = row_in(shared)  # every machine's, from before #901: until this one has measured it
    return {k: statistics.median(row[k]) for k in ("write", "read") if row.get(k)}


def budgets(m: models.Model, weights: str | None = None) -> dict:
    """{"think", "silence", "round"} in seconds for a task on this model; ``weights``: its
    identity, when known (speed). An API model's: budgets_api."""
    if getattr(m, "kind", "gpu") == "api":
        return budgets_api(m)
    sp = speed(m.id, weights)
    if not sp:
        return {"think": THINK_DEFAULT_S, "silence": SILENCE_DEFAULT_S, "round": ROUND_DEFAULT_S}
    # a reading speed alone stretches them too: a model whose first request never finished had
    # only that (the runner's read_bound), and got the defaults again (#491)
    window = (m.config.get("limit") or {}).get("context") or BASE_WINDOW
    think = (THINK_TOKENS / sp["write"] if sp.get("write") else 0) + \
        (window / sp["read"] if sp.get("read") else 0)
    think = int(min(max(think, THINK_DEFAULT_S), THINK_MAX))
    scale = max(1.0, think / THINK_DEFAULT_S)
    return {"think": think, "silence": int(SILENCE_DEFAULT_S * scale),
            "round": int(ROUND_DEFAULT_S * scale)}


def thinking_stall(model_id: str, entry: dict) -> str:
    """Why this model may sit silent until its think budget runs out on this machine, or "".

    A reasoning model left thinking can spend its whole output cap (options.max_tokens) before
    it acts; at this machine's measured write speed that can outlast the think budget. Seen
    2026-11-18: a 64K entry, thinking, capped at 16K and partly in system RAM at 21 tok/s, went
    silent for 14 minutes at a time until nudged and stopped. The same settings are harmless on a
    card that writes fast, so it is measured here, not ruled out everywhere. Not an API model's:
    its output is pinned (models.API_OUTPUT) and its silence budget holds one reply at the cap
    (budgets_api)."""
    if models.is_api(model_id):
        return ""
    opts = entry.get("options") or {}
    # Ollama honours options.max_tokens; other servers get OpenCode's limit.output
    cap, sp = opts.get("max_tokens") or (entry.get("limit") or {}).get("output"), speed(model_id)
    if not (entry.get("reasoning") and cap and sp.get("write")) or \
            str(opts.get("reasoningEffort", "")).lower() in ("none", "minimal"):
        return ""
    secs = cap / sp["write"]
    m = types.SimpleNamespace(id=model_id, config=entry)
    think = budgets(m)["think"]
    if secs <= think:
        return ""
    return (f"{model_id} thinks before it acts and may spend its {cap}-token output cap doing it: "
            f"at its ~{sp['write']:.0f} tok/s here that is {secs / 60:.0f} min, past its "
            f"{think / 60:.0f}-min think budget, so it can sit silent until nudged and stopped. "
            f"Set its options.reasoningEffort to \"none\" (or a lower output cap) in OpenCode's config")


# An API model's budgets (#1017). It reads nothing on this machine: prompt caching and the
# provider's own hardware make window / read a fiction, and a "read" speed taken from the network
# time to first token gave a 1M window 200-500 s of prefill no request pays. So think is the time
# to write THINK_TOKENS plus the time to first token (its 90th percentile: the queue's bad days).
# A reasoning step streams nothing until it ends (the spike, #1009: `run --format json` printed
# `step_start` and then nothing until the text), so the silence limit holds one whole reply at the
# output cap, written in silence, after the first token's wait, with SILENT_SLACK to spare. The
# samples are the account's, kept in $WB_HOME/speed-api.json: an API's speed isn't this machine's.
# That floor stops at SILENCE_MAX, the GPU's own longest silence (900 s scaled by THINK_MAX / 420,
# 3857 s): at 2 tok/s it came to 16448 s, and a request hung for good held the round, and one of
# the provider's API_PARALLEL slots, 4.6 hours before the guard fired (the review of #1017). A
# model that slow loses a reply at the cap to the guard; a hang costs more.
API_TTFT_SHARE = 0.9
SILENT_SLACK = 60
SILENCE_MAX = int(SILENCE_DEFAULT_S * THINK_MAX / THINK_DEFAULT_S)


def _api_speed_path() -> Path:
    return store.home() / "speed-api.json"


def _record_api_speed(model_id: str, write: list, ttft: list) -> None:
    if not (write or ttft):
        return
    path = _api_speed_path()
    with store.locked(path.with_suffix(".lock")):
        data = store.read_json(path, {}) or {}
        row = data.setdefault(model_id, {})
        row["write"] = (row.get("write", []) + write)[-SPEED_KEEP:]
        row["ttft"] = (row.get("ttft", []) + ttft)[-SPEED_KEEP:]
        store.write_json(path, data)


def api_speed(model_id: str) -> dict:
    """{"write": median tok/s, "ttft": 90th percentile seconds} of an API model, where measured
    (speed-api.json); {} for one never measured or a GPU model."""
    data = store.read_json(_api_speed_path(), {}) if _api_speed_path().exists() else {}
    row = (data or {}).get(model_id) if isinstance(data, dict) else None
    if not isinstance(row, dict):
        return {}
    out = {}
    if write := [x for x in row.get("write") or [] if isinstance(x, (int, float)) and x > 0]:
        out["write"] = statistics.median(write)
    if ttft := sorted(x for x in row.get("ttft") or [] if isinstance(x, (int, float)) and x >= 0):
        out["ttft"] = ttft[math.ceil(API_TTFT_SHARE * len(ttft)) - 1]
    return out


def budgets_api(m) -> dict:
    """{"think", "silence", "round"} for an API model: think = THINK_TOKENS / write + the time to
    first token's 90th percentile, clamped to [THINK_DEFAULT_S, THINK_MAX], with no window / read
    term; silence and round scale by think / THINK_DEFAULT_S, silence at least one reply at the
    output cap written in silence (see above) up to SILENCE_MAX, round at least the silence. No
    write speed: the defaults."""
    sp = api_speed(m.id)
    if not sp.get("write"):
        return {"think": THINK_DEFAULT_S, "silence": SILENCE_DEFAULT_S, "round": ROUND_DEFAULT_S}
    ttft = sp.get("ttft") or 0
    think = int(min(max(THINK_TOKENS / sp["write"] + ttft, THINK_DEFAULT_S), THINK_MAX))
    scale = max(1.0, think / THINK_DEFAULT_S)
    cfg = getattr(m, "config", None) or {}
    cap = (cfg.get("options") or {}).get("max_tokens") or (cfg.get("limit") or {}).get("output") \
        or models.API_OUTPUT
    silence = max(int(SILENCE_DEFAULT_S * scale),
                  min(math.ceil(cap / sp["write"] + ttft) + SILENT_SLACK, SILENCE_MAX))
    return {"think": think, "silence": silence, "round": max(int(ROUND_DEFAULT_S * scale), silence)}


def speed_line(model_id: str) -> str:
    sp = speed(model_id)
    if not sp.get("write"):
        return ""
    return f"writes ~{sp['write']:.0f} tok/s" + (f", reads ~{sp['read']:.0f} tok/s" if sp.get("read") else "")
