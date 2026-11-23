"""Opt-in progress checks (#95): when the user turns them on (`wb progress on`), a task whose
active work passes its typical length gets a check: the evidence, and a recommendation of a
targeted hint or a smaller step, weighed by what helped on this model before. Active work leaves
out what isn't the agent's: waiting for the GPU (before a round) and for an answer (asking).

A check informs Claude; it never authorizes an edit. Elapsed time doesn't make a task Claude's to
write: the coding choice (`wb coding`) is the user's alone, and nothing here reads or changes it.
Off (the default), nothing here runs and the timing is today's.
"""
from __future__ import annotations

import math
from pathlib import Path

from . import store, tuning

KINDS = ("hint", "smaller")
MIN_TRIES = 2  # below this many tries of each, the evidence decides nothing
# The typical length is the median of TYPICAL_MIN timed tasks of the model and size, at least
# FLOOR_MIN minutes; until there are that many, DEFAULT_MIN for the size. With no history the
# checks never came though `wb progress on` said they would, and one 1.5-minute task made the
# threshold 1.5 minutes: a check every 1.5 minutes, each waking Claude (#724).
TYPICAL_MIN, FLOOR_MIN = 3, 10
DEFAULT_MIN = {"trivial": 20, "significant": 45}


def path() -> Path:
    return store.home() / "progress.json"


def settings() -> dict:
    s = store.read_json(path(), None)
    return s if isinstance(s, dict) else {}


def set_enabled(on: bool, after: float | None = None) -> None:
    """On or off, with the user's fixed threshold: None keeps the one stored, 0 clears it. The
    pane's Progress checks row runs a bare `wb progress on|off`, and each rewrote the file without
    `after`: a `--after 30` went at the next pick, and checks came at the typical length (#578)."""
    kept = settings().get("after") if after is None else after
    store.write_json(path(), {"on": on, **({"after": kept} if kept else {}), "ts": store.now()})


def threshold(spec: dict) -> float | None:
    """Minutes of active work before the first check (`typical`); None while off."""
    return typical(spec)[0] if settings().get("on") else None


def typical(spec: dict) -> tuple[float, str]:
    """(minutes, where from): the user's --after ("user"), else the typical length of a task
    this size on this model (router.expected: "measured" from TYPICAL_MIN timed tasks, at least
    FLOOR_MIN), else the size's DEFAULT_MIN ("default")."""
    s = settings()
    if s.get("after"):
        return float(s["after"]), "user"
    from . import router  # router -> models: only when on
    size = spec.get("size", "significant")
    mins, n = router.expected(spec.get("model", ""), size, spec.get("weights_id"), spec.get("role"))
    if mins is not None and n >= TYPICAL_MIN:
        return max(FLOOR_MIN, mins), "measured"
    return float(DEFAULT_MIN.get(size, DEFAULT_MIN["significant"])), "default"


def _mins(m: float) -> str:
    return f"{m:.0f} min" if m >= 1 else f"{m * 60:.0f} s"


def passed(active_s: float, typical_min: float) -> int:
    """How many check points active work has passed: 1x, 2x, 4x… the typical length (each
    whole multiple once woke Claude every few minutes of a long task, #724)."""
    if not typical_min or typical_min <= 0 or active_s < typical_min * 60:
        return 0
    return int(math.log2(active_s / (typical_min * 60))) + 1


def due(task: store.Task, active_s: float, typical_min: float | None) -> bool:
    """At 1x, 2x, 4x… the typical length, counted in active work, once each."""
    if not typical_min or typical_min <= 0:
        return False
    return passed(active_s, typical_min) > int(task.status.get("progress_checks") or 0)


def helped(model: str) -> dict[str, list[int]]:
    """{kind: [helped, tried]} on this model: a task an intervention was tried on helped when it
    was accepted without Claude taking it over."""
    # judged, as the Stats feed's rows are: a spec-gap discard or a task that never ran isn't
    # the intervention's failure, and the two readers disagreed (#647)
    rows = [r for r in store.results() if tuning.judged(r)]
    return helped_by_model(rows).get(model) or {k: [0, 0] for k in KINDS}


def helped_by_model(rows: list[dict]) -> dict[str, dict[str, list[int]]]:
    """helped for every model with an intervention, in one pass over ``rows`` (the Stats feed's:
    a read of results.jsonl per model was most of its time on a long history, #565)."""
    out: dict[str, dict[str, list[int]]] = {}
    for r in rows:
        kinds = {i.get("kind") for i in r.get("interventions") or [] if isinstance(i, dict)} & set(KINDS)
        if not kinds:
            continue
        ok = tuning.agent_landed(r)
        by = out.setdefault(r.get("model"), {k: [0, 0] for k in KINDS})
        for k in kinds:
            by[k][1] += 1
            by[k][0] += ok
    return out


def recommend(moved: bool, rates: dict[str, list[int]]) -> str:
    """hint or smaller: what helped more on this model once both were tried enough, else from
    the evidence (no file changed since the last check: the step is too big to start on)."""
    (h_ok, h_n), (s_ok, s_n) = rates["hint"], rates["smaller"]
    if h_n >= MIN_TRIES and s_n >= MIN_TRIES and h_ok / h_n != s_ok / s_n:
        return "hint" if h_ok / h_n > s_ok / s_n else "smaller"
    return "hint" if moved else "smaller"


SAY = {"hint": "a targeted hint: wb steer {t} --text \"<what to look at, or the fact it's missing>\"",
       "smaller": "a smaller step: wb steer {t} --now --step --text \"<only the part to finish now>\""}


def text(task: store.Task, active_s: float, typical_min: float, moved: bool, files: int,
         errors: int) -> str:
    """The check's lines, after the check-in digest."""
    st, model = task.status, task.spec.get("model", "")
    rates = helped(model)
    kind = recommend(moved, rates)
    tests = st.get("tests")
    source = typical(task.spec)[1]
    past = (f"the {_mins(typical_min)} the user set" if source == "user" else
            f"the typical {_mins(typical_min)} for a {task.spec.get('size')} task on this model"
            if source == "measured" else
            f"the default {_mins(typical_min)} for a {task.spec.get('size')} task (fewer than "
            f"{TYPICAL_MIN} timed on this model)")
    lines = [f"progress check (on: wb progress): {_mins(active_s / 60)} of active work, past {past} "
             "(waits for the GPU and for answers left out)",
             f"evidence: {files} file(s) changed, " + ("some since the last check" if moved else
                                                        "none since the last check")
             + f"; {errors} failed tool call(s) this round; last tests: "
             + ("none yet" if not tests else "passed" if tests.get("ok") else "failed"),
             "recommended: " + SAY[kind].format(t=task.name)]
    tried = [f"a {'hint' if k == 'hint' else 'smaller step'} helped {ok} of {n}"
             for k, (ok, n) in rates.items() if n]
    if tried:
        lines.append("on this model before: " + ", ".join(tried))
    lines.append("time alone never makes it yours to edit: the coding choice stays as the user "
                 "set it (wb coding)")
    return "\n".join(lines)


def record(task: store.Task, kind: str) -> bool:
    """A steer that answers an open check: kept on the task (status `interventions`), and in its
    results row, so later checks learn whether it helped. False when no check is open."""
    st = task.status
    if not st.get("progress_open"):
        return False
    task.set_status(progress_open=None, interventions=(st.get("interventions") or []) + [
        {"kind": kind, "ts": store.now(), "round": st.get("round"), "step": st.get("steps", 1)}])
    return True
