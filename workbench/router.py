"""Which model gets a task: an educated guess, corrected by how real tasks went.

Real delegated work (results.jsonl: accepted or not, and how long it took) is the evidence.
Quality is the acceptance rate with a uniform prior, (accepted + 1) / (tries + 2), so one lucky
task doesn't crown a model. Work of the same kind counts most: a task in a repo of the same stack
(its main languages, gitops.stack) is judged first by that model's record on that stack, with its
overall record as the prior worth LIKE_PRIOR tasks, so a model strong in one language and weak in
another is routed by the one at hand once there is evidence, and by its whole record until then. Among the models within QUALITY_MARGIN of the best, the guess
decides: for significant work the largest model (more parameters, better judgment), for trivial
work the smallest (faster), and at equal size the one already on the GPU. A model too big for
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
from dataclasses import dataclass
from pathlib import Path

from . import gitops, models, store, tuning

QUALITY_MARGIN = 0.15
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

    @property
    def quality(self) -> float:
        overall = (self.wins + 1) / (self.tries + 2)
        return (self.like_wins + LIKE_PRIOR * overall) / (self.like_tries + LIKE_PRIOR)

    def line(self) -> str:
        if not self.tries:
            return "no real tasks yet"
        t = f", ~{self.minutes:.1f} min" if self.minutes is not None else ""
        like = f", {self.like_wins}/{self.like_tries} on {'/'.join(self.stack)}" if self.like_tries else ""
        return f"{self.wins}/{self.tries} real tasks accepted{like}{t}"


@functools.cache
def _repo_stack(repo: str) -> tuple:
    return tuple(gitops.stack(repo)) if Path(repo).is_dir() else ()


def row_stack(r: dict) -> tuple:
    """A result's stack: recorded since it was, else read from its repo while that exists."""
    if r.get("stack") is not None:
        return tuple(r["stack"])
    return _repo_stack(r["repo"]) if r.get("repo") else ()


def score(m: models.Model, size: str, state: dict | None = None, stack=()) -> Score:
    state = state or models.status(m)
    real = [r for r in store.results() if r.get("model") == m.id and r.get("size") == size
            and tuning.judged(r)]
    s = Score(model=m, state=state, weights=models.weights(m), stack=tuple(stack))
    s.tries = len(real)
    s.wins = sum(r.get("outcome") == "accepted" for r in real)
    if stack:
        like = [r for r in real if set(row_stack(r)) & set(stack)]
        s.like_tries = len(like)
        s.like_wins = sum(r.get("outcome") == "accepted" for r in like)
    times = [r["minutes"] for r in real if r.get("minutes") is not None]
    s.minutes = sum(times) / len(times) if times else None
    return s


def expected_minutes(model_id: str, size: str) -> float | None:
    """How long a task of this size usually takes on this model, from real tasks."""
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
        s.fits = fits(s.model, budget)
    usable = [s for s in scores if s.state["available"]]
    best = max((s.quality for s in usable), default=0)
    close = sorted((s for s in usable if s.quality >= best - QUALITY_MARGIN), key=_guess(size))
    rest = sorted((s for s in usable if s not in close), key=lambda s: -s.quality)
    blocked = [s for s in scores if not s.state["available"]]
    return close + rest + blocked


def choose(size: str, stack=()) -> tuple[Score | None, str]:
    """The model for a task of this size (in a repo of this stack) and one line on why."""
    ranked = rank(size, stack)
    top = ranked[0] if ranked and ranked[0].state["available"] else None
    if top is None:
        return None, "no local model can run: " + "; ".join(
            f"{s.model.id}: {s.state['why']}" for s in ranked) if ranked else \
            "no local models in opencode.json"
    usable = [s for s in ranked if s.state["available"]]
    sizes = [s.weights for s in usable if s.fits is not False or top.fits is False]
    if len(usable) == 1:
        pick = f"the only model allowed {size} work"
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
        return Slice(files, lines, f"a guess from its {basis}; no steps measured yet")
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


def record_speed(model_id: str, messages: list[dict]) -> None:
    """Add a session's measured speeds to speed.json: writing (output and reasoning tokens over
    the time from first token to done) and reading (new input tokens over the time to first token).
    A request cut short (a nudge, a stop) still says how fast it read: on a slow machine the
    first request is often the one interrupted, and the reading was never learned (the
    2026-11-16 audit)."""
    write, read = [], []
    for msg in messages:
        t, tk = msg.get("time") or {}, msg.get("tokens") or {}
        made, first, done = t.get("created"), t.get("firstToken"), t.get("completed")
        if msg.get("type") != "assistant" or not (made and first):
            continue
        out = (tk.get("output") or 0) + (tk.get("reasoning") or 0)
        if done and out >= 100 and done - first > 1000:
            write.append(round(out / ((done - first) / 1000), 1))
        if (tk.get("input") or 0) >= 1000 and first - made > 500:
            read.append(round(tk["input"] / ((first - made) / 1000)))
    if not (write or read):
        return
    path = _speed_path()
    with store.locked(path.with_suffix(".lock")):
        data = store.read_json(path, {}) or {}
        row = data.setdefault(model_id, {})
        row["write"] = (row.get("write", []) + write)[-SPEED_KEEP:]
        row["read"] = (row.get("read", []) + read)[-SPEED_KEEP:]
        store.write_json(path, data)


def speed(model_id: str) -> dict:
    """{"write", "read"}: the model's median tokens per second on this machine, where measured."""
    row = (store.read_json(_speed_path(), {}) or {}).get(model_id) or {}
    return {k: statistics.median(row[k]) for k in ("write", "read") if row.get(k)}


def budgets(m: models.Model) -> dict:
    """{"think", "silence", "round"} in seconds for a task on this model."""
    sp = speed(m.id)
    if not sp.get("write"):
        return {"think": THINK_DEFAULT_S, "silence": SILENCE_DEFAULT_S, "round": ROUND_DEFAULT_S}
    window = (m.config.get("limit") or {}).get("context") or BASE_WINDOW
    think = THINK_TOKENS / sp["write"] + (window / sp["read"] if sp.get("read") else 0)
    think = int(min(max(think, THINK_DEFAULT_S), THINK_MAX))
    scale = max(1.0, think / THINK_DEFAULT_S)
    return {"think": think, "silence": int(SILENCE_DEFAULT_S * scale),
            "round": int(ROUND_DEFAULT_S * scale)}


def speed_line(model_id: str) -> str:
    sp = speed(model_id)
    if not sp.get("write"):
        return ""
    return f"writes ~{sp['write']:.0f} tok/s" + (f", reads ~{sp['read']:.0f} tok/s" if sp.get("read") else "")
