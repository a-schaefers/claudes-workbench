"""results.jsonl: one row per closed task, so the models can be compared on real work."""
from collections import defaultdict

from . import gitops, skills, store


def step(task: store.Task, landed: bool) -> dict:
    """The step closing now (committed by wb next or accept, or dropped): its size and how it
    went. A session compacted (now, or before a fresh one took over) or filled by reading alone,
    an agent stuck, or review rounds the agent caused (`reviews` less the `spec_rounds` Claude
    marked as the spec's) say it was too much; Claude taking over says nothing of its size; a
    handover to a fresh session is the runner's normal way through a big step, counted, not held
    against it. The router learns each model's slice from these (router.slice)."""
    st = task.status
    since = st.get("step_round") or 1
    try:
        size = gitops.footprint(task.worktree, "HEAD")  # the step's work is uncommitted until now
    except (gitops.GitError, OSError):
        size = {}
    evs = [e for e in store.read_jsonl(task.dir / "events.jsonl")[0] if (e.get("round") or 0) >= since]
    stuck = any(r.get("task") == task.name and r.get("kind") in ("stuck", "failed")
                and (r.get("round") or 0) >= since
                for r in store.notices())
    from .runner import context_state  # the runner imports what this module does
    try:
        compacted = bool(context_state(task).get("compactions"))
    except Exception:  # no session to export: nothing was compacted
        compacted = False
    compacted |= any(e.get("kind") == "fresh" and e.get("compacted") for e in evs)  # an earlier session
    reviews = st.get("reviews", 0)  # wb next starts each step's count afresh
    gaps = {g.get("round") for g in st.get("spec_gaps") or [] if (g.get("round") or 0) >= since}
    return {**size, "window": st.get("window"), "landed": landed, "compacted": compacted,
            "handovers": sum(e.get("kind") == "fresh" and bool(e.get("overflow")) for e in evs),
            "read_full": any(e.get("kind") == "reading_full" for e in evs),  # before any change
            "reviews": reviews, "spec_rounds": min(len(gaps), reviews),
            "stuck": stuck, "took_over": any(e.get("kind") == "takeover" for e in evs)}


def record(task: store.Task, outcome: str, last_step: dict | None = None, **extra) -> dict:
    spec, st = task.spec, task.status
    earlier = st.get("step_sizes") or []
    reviews = sum(s.get("reviews") or 0 for s in earlier) + st.get("reviews", 0)  # every step's
    row = {"ts": store.now(), "task": task.name, "outcome": outcome, "model": spec.get("model"),
           "size": spec.get("size"), "repo": spec.get("repo"), "stack": spec.get("stack"),
           "rounds": st.get("round", 1),
           "auto_fixes": st.get("auto_fixes_total", 0) + st.get("auto_fixes", 0),
           "reviews": reviews,
           "spec_rounds": min(len({g.get("round") for g in st.get("spec_gaps") or []}), reviews),
           "tests_ok": (st.get("tests") or {}).get("ok"),
           "minutes": round((store.now() - spec.get("created_at", store.now())) / 60, 1),
           "tok_in": st.get("tok_in", 0), "tok_out": st.get("tok_out", 0),
           "skills_offered": st.get("skills_ever") or [], "skills_loaded": skills.loaded(task),
           "skills_rev": st.get("skills_rev") or {},  # the version handed: judges each edit
           "steps": earlier + ([last_step] if last_step else []),
           "window": st.get("window"),
           "tuned": st.get("tuned") or {}, "explored": st.get("explored") or [],  # tuning.py
           "tuned_max": st.get("tuned_max") or {},
           "spec_gaps": [g["text"] for g in st.get("spec_gaps") or []],
           **({"trial": True} if spec.get("trial") else {}), **extra}
    store.append_jsonl(store.home() / "results.jsonl", row)
    return row


SPEC_GAP_WINDOW = 10


def spec_gap_line() -> str | None:
    """Claude's own record as a spec writer, shown as it hands the next task off: how many of
    the latest tasks needed a round for a gap in the spec (wb feedback|accept --spec-gap)."""
    rows = [r for r in store.results() if "spec_gaps" in r][-SPEC_GAP_WINDOW:]
    if not rows:
        return None
    hit = [r for r in rows if r["spec_gaps"]]
    if not hit:
        return f"your specs: no gaps in the last {len(rows)} task(s)"
    return (f"your specs: {len(hit)} of the last {len(rows)} task(s) needed a round for a spec "
            f"gap; last: {hit[-1]['spec_gaps'][-1][:160]} (the checklist: /workbench, Before wb start)")


def table() -> str:
    from .tuning import judged  # tuning reads results' rows
    every = store.results()
    rows = [r for r in every if judged(r)]
    gaps = len(every) - len(rows)
    trials = len(store.results(trials=True)) - len(every)
    left_out = (f"\n({trials} trial run{'s' if trials > 1 else ''} left out)" if trials else "") + \
        (f"\n({gaps} discard{'s' if gaps > 1 else ''} put down to the spec left out)" if gaps else "")
    if not rows:
        return "no finished tasks yet" + left_out
    by = defaultdict(list)
    for r in rows:
        by[(r["model"], r.get("size") or "?")].append(r)
    out = [f"{'model':34} {'size':12} {'tasks':>5} {'accepted':>8} {'rounds':>6} "
           f"{'autofix':>7} {'reviews':>7} {'min':>6} {'local tok':>10}"]
    for (model, size), rs in sorted(by.items()):
        n = len(rs)
        acc = sum(r["outcome"] == "accepted" for r in rs)
        avg = lambda k: sum(r.get(k) or 0 for r in rs) / n  # noqa: E731
        out.append(f"{model:34} {size:12} {n:>5} {acc / n:>7.0%} {avg('rounds'):>6.1f} "
                   f"{avg('auto_fixes'):>7.1f} {avg('reviews'):>7.1f} {avg('minutes'):>6.1f} "
                   f"{int(avg('tok_in') + avg('tok_out')):>10}")
    return "\n".join(out) + left_out
