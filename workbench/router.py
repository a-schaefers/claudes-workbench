"""Which model gets a task: decided from what each model has shown, not from a table.

Evidence per model and task size comes from two places: `wb bench` (graded coding tasks with
hidden tests, plus speed probes) and real delegated work (results.jsonl: accepted or not, and how
long it took). Quality is the success rate with a uniform prior, (wins + 1) / (tries + 2), so one
lucky run doesn't crown a model. Among the models within QUALITY_MARGIN of the best, the fastest
expected finish wins, counting the time to get the model onto the GPU when it isn't there.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import models, store

QUALITY_MARGIN = 0.15
SIZES = ("trivial", "significant")
# Until a model's cold load is measured: what a 10-20 GB load usually costs on this kind of box.
DEFAULT_LOAD_MIN = {"ollama": 0.5, "server": 5.0}


@dataclass
class Score:
    model: models.Model
    state: dict
    tries: int = 0
    wins: int = 0
    minutes: float | None = None   # mean wall time of a task of this size
    load_min: float = 0.0          # to get it onto the GPU, if it isn't there
    probe: dict | None = None

    @property
    def quality(self) -> float:
        return (self.wins + 1) / (self.tries + 2)

    @property
    def cost(self) -> float:
        return (self.minutes if self.minutes is not None else 10.0) + self.load_min

    def line(self) -> str:
        if not self.tries:
            return "no evidence yet (wb bench)"
        q = f"{self.wins}/{self.tries} passed"
        t = f", ~{self.minutes:.1f} min" if self.minutes is not None else ""
        load = f" + {self.load_min:.1f} min to load" if self.load_min >= 0.05 else ""
        return q + t + load


def _bench_rows(m: models.Model, fp: str | None) -> list[dict]:
    rows = [r for r in store.read_jsonl(store.home() / "bench.jsonl")[0] if r.get("model") == m.id]
    if not rows:
        return []
    # Measurements of other weights behind the same id don't count. A stopped server can't say
    # what it serves: then the latest weights it was measured with stand in.
    fp = fp or rows[-1].get("fingerprint")
    return [r for r in rows if r.get("fingerprint") == fp]


def score(m: models.Model, size: str, state: dict | None = None) -> Score:
    state = state or models.status(m)
    bench = _bench_rows(m, models.fingerprint(m))
    real = [r for r in store.read_jsonl(store.home() / "results.jsonl")[0]
            if r.get("model") == m.id and r.get("size") == size and not r.get("bench")]
    tasks = [r for r in bench if r.get("kind") == "task" and r.get("size") == size]
    s = Score(model=m, state=state)
    s.tries = len(tasks) + len(real)
    s.wins = sum(bool(r.get("passed")) for r in tasks) + \
        sum(r.get("outcome") == "accepted" for r in real)
    times = [r["minutes"] for r in tasks + real if r.get("minutes") is not None]
    s.minutes = sum(times) / len(times) if times else None
    probes = [r for r in bench if r.get("kind") == "probe"]
    s.probe = probes[-1] if probes else None
    if not state.get("resident"):
        loads = [r["load_s"] / 60 for r in probes if r.get("load_s")]
        s.load_min = loads[-1] if loads else DEFAULT_LOAD_MIN[m.engine]
    return s


def expected_minutes(model_id: str, size: str) -> float | None:
    """How long a task of this size usually takes on this model, from the same evidence."""
    m = models.discover().get(model_id)
    if not m:
        return None
    return score(m, size, state={"available": True, "resident": True}).minutes


def rank(size: str) -> list[Score]:
    """Every discovered model's case for a task of this size, best first."""
    scores = [score(m, size) for m in models.discover().values()]
    usable = [s for s in scores if s.state["available"]]
    proven = [s for s in usable if s.tries]
    if proven:
        best = max(s.quality for s in proven)
        close = [s for s in proven if s.quality >= best - QUALITY_MARGIN]
        close.sort(key=lambda s: s.cost)
        rest = sorted((s for s in proven if s not in close), key=lambda s: -s.quality)
    else:
        # Nothing measured yet: prefer what is already loaded, then anything that can run.
        close, rest = sorted(usable, key=lambda s: not s.state["resident"]), []
    unproven = [s for s in usable if not s.tries and s not in close]
    blocked = [s for s in scores if not s.state["available"]]
    return close + rest + unproven + blocked


def choose(size: str) -> tuple[Score | None, str]:
    """The model for a task of this size and one line on why."""
    ranked = rank(size)
    top = ranked[0] if ranked and ranked[0].state["available"] else None
    if top is None:
        return None, "no local model can run: " + "; ".join(
            f"{s.model.id}: {s.state['why']}" for s in ranked) if ranked else \
            "no local models in opencode.json"
    if not top.tries:
        return top, f"{top.model.id}: no {size} benchmarks yet, so the loaded or first " \
                    f"runnable model (`wb bench` measures them)"
    others = [s for s in ranked[1:4] if s.state["available"] and s.tries]
    vs = "; ".join(f"{s.model.id} {s.line()}" for s in others)
    return top, f"{top.model.id}: {size} {top.line()}" + (f" (vs {vs})" if vs else "")
