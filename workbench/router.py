"""Which model gets a task: an educated guess, corrected by how real tasks went.

Real delegated work (results.jsonl: accepted or not, and how long it took) is the evidence.
Quality is the acceptance rate with a uniform prior, (accepted + 1) / (tries + 2), so one lucky
task doesn't crown a model. Among the models within QUALITY_MARGIN of the best, the guess
decides: for significant work the largest model (more parameters, better judgment), for trivial
work the smallest (faster), and at equal size the one already on the GPU.

The user's policy can narrow that: a model whose models.json entry has ``"sizes": ["trivial"]``
is only ever routed trivial tasks, whatever it scores elsewhere (`wb model sizes`).

How much one task should hold on a model (its slice) is learned the same way. The guess scales
with the context window the model is served, from what was measured on a 32K one; each closed
step then says whether a slice that size fit (results.step). The biggest that fit sets a floor,
the smallest that overflowed a ceiling, so a big window on a strong machine earns big tasks.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import models, store

QUALITY_MARGIN = 0.15
SIZES = ("trivial", "significant")
# Measured 2026-11-16 on a 32K window: 2-3 files and ~150 new lines landed in one session; eight
# files took an hour of compactions and drift. A bigger window is guessed to hold proportionally
# more, up to GUESS_MAX times that; past it, only steps that fit earn more.
BASE_WINDOW, BASE_FILES, BASE_LINES = 32768, 3, 150
GUESS_MAX = 4


@dataclass
class Score:
    model: models.Model
    state: dict
    weights: int = 0               # bytes of weights, 0 when unknown
    tries: int = 0
    wins: int = 0
    minutes: float | None = None   # mean wall time of a real task of this size

    @property
    def quality(self) -> float:
        return (self.wins + 1) / (self.tries + 2)

    def line(self) -> str:
        if not self.tries:
            return "no real tasks yet"
        t = f", ~{self.minutes:.1f} min" if self.minutes is not None else ""
        return f"{self.wins}/{self.tries} real tasks accepted{t}"


def score(m: models.Model, size: str, state: dict | None = None) -> Score:
    state = state or models.status(m)
    real = [r for r in store.read_jsonl(store.home() / "results.jsonl")[0]
            if r.get("model") == m.id and r.get("size") == size]
    s = Score(model=m, state=state, weights=models.weights(m))
    s.tries = len(real)
    s.wins = sum(r.get("outcome") == "accepted" for r in real)
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
    return lambda s: (sign * s.weights, not s.state.get("resident"))


def rank(size: str) -> list[Score]:
    """Every discovered model's case for a task of this size, best first (the user's policy
    leaves some out: allowed)."""
    scores = [score(m, size) for m in models.discover().values() if allowed(m, size)]
    usable = [s for s in scores if s.state["available"]]
    best = max((s.quality for s in usable), default=0)
    close = sorted((s for s in usable if s.quality >= best - QUALITY_MARGIN), key=_guess(size))
    rest = sorted((s for s in usable if s not in close), key=lambda s: -s.quality)
    blocked = [s for s in scores if not s.state["available"]]
    return close + rest + blocked


def choose(size: str) -> tuple[Score | None, str]:
    """The model for a task of this size and one line on why."""
    ranked = rank(size)
    top = ranked[0] if ranked and ranked[0].state["available"] else None
    if top is None:
        return None, "no local model can run: " + "; ".join(
            f"{s.model.id}: {s.state['why']}" for s in ranked) if ranked else \
            "no local models in opencode.json"
    usable = [s for s in ranked if s.state["available"]]
    sizes = [s.weights for s in usable]
    if len(usable) == 1:
        pick = f"the only model allowed {size} work"
    elif size == "significant" and top.weights == max(sizes):
        pick = "the largest model allowed significant work"
    elif size == "trivial" and top.weights == min(sizes):
        pick = "the smallest model allowed trivial work"
    else:
        pick = f"the best record on {size} work"
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
    steps = [s for r in store.read_jsonl(store.home() / "results.jsonl")[0] if r.get("model") == m.id
             for s in r.get("steps") or [] if s.get("files") and (not window or s.get("window") in (None, window))]
    fit = [s for s in steps if s["landed"] and not (s["overflow"] or s["stuck"] or s["took_over"])]
    over = [s for s in steps if s["overflow"] or s["stuck"]]
    if over:  # below the smallest step that didn't fit
        files = min(files, max(1, min(s["files"] for s in over) - 1))
        lines = min(lines, max(50, min(s["lines"] for s in over) * 3 // 4))
    if fit:  # at least the biggest that did
        files, lines = max(files, max(s["files"] for s in fit)), max(lines, max(s["lines"] for s in fit))
    if not steps:
        return Slice(files, lines, f"a guess from its {basis}; no steps measured yet")
    return Slice(files, lines, f"{basis}; {len(fit)} of {len(steps)} measured steps fit, "
                 f"{len(over)} overflowed or stuck")
