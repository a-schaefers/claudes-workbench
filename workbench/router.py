"""Which model gets a task: an educated guess, corrected by how real tasks went.

Real delegated work (results.jsonl: accepted or not, and how long it took) is the evidence.
Quality is the acceptance rate with a uniform prior, (accepted + 1) / (tries + 2), so one lucky
task doesn't crown a model. Work of the same kind counts most: a task in a repo of the same stack
(its main languages, gitops.stack) is judged first by that model's record on that stack, with its
overall record as the prior worth LIKE_PRIOR tasks, so a model strong in one language and weak in
another is routed by the one at hand once there is evidence, and by its whole record until then. Among the models within QUALITY_MARGIN of the best, the guess
decides: for significant work the largest model (more parameters, better judgment), for trivial
work the smallest (faster), and at equal size the one already on the GPU. Claude's tokens are
what workbench saves, so once two of those models each have COST_MIN tasks of the size with what
they cost Claude recorded (results' `claude`: the session's calls on the task), one that costs
Claude COST_MARGIN less a task than the guess's pick goes first: on 2026-11-18 the smaller,
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
runner gets through one. The biggest that went well sets a floor, the smallest that went badly a
ceiling, so a big window on a strong machine earns big tasks.

How long the agent may think, or go quiet, before the runner acts (budgets) is learned too: each
round's session says how fast the model reads and writes on this machine (record_speed).
"""
from __future__ import annotations

import functools
import statistics
import types
from dataclasses import dataclass
from pathlib import Path

from . import gitops, models, spend, store, tuning

QUALITY_MARGIN = 0.15
COST_MIN = 3        # tasks of a size with Claude's spend recorded before it can move the pick
COST_MARGIN = 0.15  # how much less a task must cost Claude to beat the guess
LIKE_PRIOR = 2  # how many tasks of the same stack the overall record weighs as
SIZES = ("trivial", "significant")
# Measured 2026-11-16 on a 32K window: 2-3 files and ~150 new lines landed in one session; eight
# files took an hour of compactions and drift. A bigger window is guessed to hold proportionally
# more, up to GUESS_MAX times that; past it, only steps that fit earn more.
BASE_WINDOW, BASE_FILES, BASE_LINES = 32768, 3, 150
GUESS_MAX = 4
BAD_REVIEWS = 2  # review rounds the agent caused at which a step held too much


@dataclass
class Score:
    model: models.Model
    state: dict
    weights: int = 0               # bytes of weights, 0 when unknown
    tries: int = 0
    wins: int = 0
    minutes: float | None = None   # mean wall time of a real task of this size
    fits: bool | None = None       # its need within the GPU's budget; None when unknown
    stack: tuple = ()              # the kind of work asked about, and the record on it
    like_tries: int = 0
    like_wins: int = 0
    claude: float | None = None    # mean input-equivalent tokens a task cost Claude (spend.py)
    claude_n: int = 0              # the tasks it is the mean of
    cheaper: bool = False          # put ahead of the guess for costing Claude less (_cheaper)

    @property
    def quality(self) -> float:
        overall = (self.wins + 1) / (self.tries + 2)
        return (self.like_wins + LIKE_PRIOR * overall) / (self.like_tries + LIKE_PRIOR)

    def line(self) -> str:
        if not self.tries:
            return "no real tasks yet"
        t = f", ~{self.minutes:.1f} min of active work" if self.minutes is not None else ""
        like = f", {self.like_wins}/{self.like_tries} on {'/'.join(self.stack)}" if self.like_tries else ""
        c = f", ~{self.claude / 1000:.0f}K Claude tokens a task" if self.claude_n >= COST_MIN else ""
        return f"{self.wins}/{self.tries} real tasks accepted{like}{t}{c}"


@functools.cache
def _repo_stack(repo: str) -> tuple:
    return tuple(gitops.stack(repo)) if Path(repo).is_dir() else ()


def row_stack(r: dict) -> tuple:
    """A result's stack: recorded since it was, else read from its repo while that exists."""
    if r.get("stack") is not None:
        return tuple(r["stack"])
    return _repo_stack(r["repo"]) if r.get("repo") else ()


def lineage(m: models.Model) -> set[str]:
    """The ids that share ``m``'s record: a `wb optimize apply` variant (models.json
    `variant_of`), its source, and the source's other variants. One model at two windows is one
    model's record (#584)."""
    over = models._overrides()
    root = m.extra.get("variant_of") or m.id
    return {m.id, root} | {k for k, v in over.items() if isinstance(v, dict) and v.get("variant_of") == root}


def score(m: models.Model, size: str, state: dict | None = None, stack=()) -> Score:
    state = state or models.status(m)
    ids = lineage(m)
    real = [r for r in store.results() if r.get("model") in ids and r.get("size") == size
            and tuning.judged(r)]
    # a model that can't run isn't weighed: its server was just found down, and asking it again
    # cost seconds per model on a sleeping LAN box (the 2026-11-16 audit)
    s = Score(model=m, state=state, stack=tuple(stack),
              weights=models.weights(m) if state["available"] or m.extra.get("weights_gb") else 0)
    s.tries = len(real)
    s.wins = sum(r.get("outcome") == "accepted" for r in real)
    if stack:
        like = [r for r in real if set(row_stack(r)) & set(stack)]
        s.like_tries = len(like)
        s.like_wins = sum(r.get("outcome") == "accepted" for r in like)
    # the agent's active work, median: wall minutes held the queue, Claude's review and a night
    # before an accept (480 min for a 12-minute task), and a mean let one such row set every
    # ETA and progress check at many times the real length (#543). Rows without it are skipped.
    times = [r["active_minutes"] for r in real if isinstance(r.get("active_minutes"), (int, float))
             and not isinstance(r["active_minutes"], bool)]
    s.minutes = statistics.median(times) if times else None
    spent = [spend.weighted(r["claude"]) for r in real if isinstance(r.get("claude"), dict)]
    s.claude, s.claude_n = (statistics.fmean(spent) if spent else None), len(spent)
    return s


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


def expected_minutes(model_id: str, size: str) -> float | None:
    """How long a task of this size usually takes on this model, from real tasks: the median of
    their active minutes (the agent's own working time), so compare it with a task's active time."""
    m = models.discover().get(model_id)
    if not m:
        return None
    return score(m, size, state={"available": True, "resident": True}).minutes


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


def rank(size: str, stack=()) -> list[Score]:
    """Every discovered model's case for a task of this size (in a repo of this stack), best
    first (the user's policy leaves some out: allowed)."""
    scores = [score(m, size, stack=stack) for m in models.discover().values() if allowed(m, size)]
    budget = models.gpu_budget() if scores else None
    for s in scores:
        s.fits = fits(s.model, budget) if s.state["available"] else None
    usable = [s for s in scores if s.state["available"]]
    best = max((s.quality for s in usable), default=0)
    # a source whose optimize variant can run goes after it: the variant is the window measured
    # to fit, with the same record (#584)
    replaced = {s.model.extra.get("variant_of") for s in usable} - {None}
    guess = _guess(size)
    close = _cheaper(sorted((s for s in usable if s.quality >= best - QUALITY_MARGIN),
                            key=lambda s: (s.model.id in replaced, *guess(s))))
    rest = sorted((s for s in usable if s not in close), key=lambda s: -s.quality)
    blocked = [s for s in scores if not s.state["available"]]
    return close + rest + blocked


def choose(size: str, stack=()) -> tuple[Score | None, str]:
    """The model for a task of this size (in a repo of this stack) and one line on why."""
    ranked = rank(size, stack)
    top = ranked[0] if ranked and ranked[0].state["available"] else None
    if top is None:
        if ranked:
            return None, "no local model can run: " + "; ".join(
                f"{s.model.id}: {s.state['why']}" + (f" ({fix})" if (fix := models.remedy(s.model, s.state)) else "")
                for s in ranked)
        if found := models.discover():  # all held back by the size policy, not missing (the 2026-11-16 audit)
            return None, (f"no local model takes {size} tasks ({', '.join(found)}: limited by wb model "
                          f"sizes; wb model sizes <id> trivial significant allows one, or pass --model)")
        return None, "no local models: " + models.why_none()
    usable = [s for s in ranked if s.state["available"]]
    sizes = [s.weights for s in usable if s.fits is not False or top.fits is False]
    if len(usable) == 1:
        pick = f"the only model allowed {size} work"
    elif top.cheaper:
        pick = f"costs Claude least on {size} work"
    elif size == "significant" and top.weights == max(sizes):
        pick = "the largest model allowed significant work" + (
            " that fits the GPU" if top.fits and any(s.fits is False for s in usable) else "")
    elif size == "trivial" and top.weights == min(sizes):
        pick = "the smallest model allowed trivial work"
    else:
        kind = f"{size} {'/'.join(stack)}" if top.like_tries else size
        pick = f"the best record on {kind} work"
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
    steps = [s for r in store.results() if r.get("model") == m.id and tuning.judged(r)
             for s in r.get("steps") or [] if s.get("files") and (not window or s.get("window") in (None, window))]
    went = [_went(s) for s in steps]
    fit = [s for s, w in zip(steps, went) if w == "well"]
    over = [s for s, w in zip(steps, went) if w == "badly"]
    if over:  # below the smallest step that went badly
        files = min(files, max(1, min(s["files"] for s in over) - 1))
        lines = min(lines, max(50, min(s["lines"] for s in over) * 3 // 4))
    if fit:  # at least the biggest that went well
        files, lines = max(files, max(s["files"] for s in fit)), max(lines, max(s["lines"] for s in fit))
    if not steps:
        return Slice(files, lines, f"a guess from its {basis}; no steps measured yet" if window
                     else "a guess, its window unknown; no steps measured yet")  # not "from its window unknown" (the 0.90 review)
    return Slice(files, lines, f"{basis}; {len(fit)} of {len(steps)} measured steps went well, "
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
    return store.home() / "speed.json"


def record_speed(model_id: str, messages: list[dict]) -> dict:
    """Add a session's measured speeds to speed.json: writing (output and reasoning tokens over
    the time from first token to done) and reading (new input tokens over the time to first token).
    A request cut short (a nudge, a stop) still says how fast it read: on a slow machine the
    first request is often the one interrupted, and the reading was never learned (the
    2026-11-16 audit)."""
    got = speed_samples(messages)
    write, read = got["write"], got["read"]
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
    runner's read_bound, #491) to speed.json's reading samples."""
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


def speed(model_id: str) -> dict:
    """{"write", "read"}: the model's median tokens per second on this machine, where measured."""
    row = (store.read_json(_speed_path(), {}) or {}).get(model_id) or {}
    return {k: statistics.median(row[k]) for k in ("write", "read") if row.get(k)}


def budgets(m: models.Model) -> dict:
    """{"think", "silence", "round"} in seconds for a task on this model."""
    sp = speed(m.id)
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
    card that writes fast, so it is measured here, not ruled out everywhere."""
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


def speed_line(model_id: str) -> str:
    sp = speed(model_id)
    if not sp.get("write"):
        return ""
    return f"writes ~{sp['write']:.0f} tok/s" + (f", reads ~{sp['read']:.0f} tok/s" if sp.get("read") else "")
