"""Which model gets a task: an educated guess, corrected by how real tasks went.

Real delegated work (results.jsonl: accepted or not, and how long it took) is the evidence.
Quality is the acceptance rate with a uniform prior, (accepted + 1) / (tries + 2), so one lucky
task doesn't crown a model. Among the models within QUALITY_MARGIN of the best, the guess
decides: for significant work the largest model (more parameters, better judgment), for trivial
work the smallest (faster), and at equal size the one already on the GPU.

The user's policy can narrow that: a model whose models.json entry has ``"sizes": ["trivial"]``
is only ever routed trivial tasks, whatever it scores elsewhere (`wb model sizes`).
"""
from __future__ import annotations

from dataclasses import dataclass

from . import models, store

QUALITY_MARGIN = 0.15
SIZES = ("trivial", "significant")


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
