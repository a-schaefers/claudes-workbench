"""results.jsonl: one row per closed task, so the models can be compared on real work."""
from collections import defaultdict

from . import gitops, skills, store


def step(task: store.Task, landed: bool) -> dict:
    """The step closing now (committed by wb next or accept, or dropped): its size, and whether
    it fit the agent's context. A session that filled or compacted, an agent stuck, or Claude
    taking over says it didn't. The router learns each model's slice from these (router.slice)."""
    st = task.status
    since = st.get("step_round") or 1
    try:
        size = gitops.footprint(task.worktree, "HEAD")  # the step's work is uncommitted until now
    except (gitops.GitError, OSError):
        size = {}
    evs = [e for e in store.read_jsonl(task.dir / "events.jsonl")[0] if (e.get("round") or 0) >= since]
    stuck = any(r.get("task") == task.name and r.get("kind") in ("stuck", "failed")
                and (r.get("round") or 0) >= since
                for r in store.read_jsonl(store.home() / "notify.jsonl")[0])
    from .runner import context_state  # the runner imports what this module does
    try:
        compacted = bool(context_state(task).get("compactions"))
    except Exception:  # no session to export: nothing was compacted
        compacted = False
    return {**size, "window": st.get("window"), "landed": landed,
            "overflow": compacted or any(e.get("kind") == "fresh" and e.get("overflow") for e in evs),
            "stuck": stuck, "took_over": any(e.get("kind") == "takeover" for e in evs)}


def record(task: store.Task, outcome: str, last_step: dict | None = None, **extra) -> dict:
    spec, st = task.spec, task.status
    row = {"ts": store.now(), "task": task.name, "outcome": outcome, "model": spec.get("model"),
           "size": spec.get("size"), "repo": spec.get("repo"), "stack": spec.get("stack"),
           "rounds": st.get("round", 1),
           "auto_fixes": st.get("auto_fixes_total", 0) + st.get("auto_fixes", 0),
           "reviews": st.get("reviews", 0),
           "tests_ok": (st.get("tests") or {}).get("ok"),
           "minutes": round((store.now() - spec.get("created_at", store.now())) / 60, 1),
           "tok_in": st.get("tok_in", 0), "tok_out": st.get("tok_out", 0),
           "skills_offered": st.get("skills_ever") or [], "skills_loaded": skills.loaded(task),
           "skills_rev": st.get("skills_rev") or {},  # the version handed: judges each edit
           "steps": (st.get("step_sizes") or []) + ([last_step] if last_step else []),
           **({"trial": True} if spec.get("trial") else {}), **extra}
    store.append_jsonl(store.home() / "results.jsonl", row)
    return row


def table() -> str:
    rows = store.results()
    trials = len(store.results(trials=True)) - len(rows)
    left_out = f"\n({trials} trial run{'s' if trials > 1 else ''} left out)" if trials else ""
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
