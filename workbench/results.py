"""results.jsonl: one row per closed task, so the models can be compared on real work."""
from collections import defaultdict

from . import store


def record(task: store.Task, outcome: str, **extra) -> dict:
    spec, st = task.spec, task.status
    row = {"ts": store.now(), "task": task.name, "outcome": outcome, "model": spec.get("model"),
           "size": spec.get("size"), "repo": spec.get("repo"), "rounds": st.get("round", 1),
           "auto_fixes": st.get("auto_fixes_total", 0) + st.get("auto_fixes", 0),
           "reviews": st.get("reviews", 0),
           "tests_ok": (st.get("tests") or {}).get("ok"),
           "minutes": round((store.now() - spec.get("created_at", store.now())) / 60, 1),
           "tok_in": st.get("tok_in", 0), "tok_out": st.get("tok_out", 0), **extra}
    store.append_jsonl(store.home() / "results.jsonl", row)
    return row


def table() -> str:
    rows, _ = store.read_jsonl(store.home() / "results.jsonl")
    if not rows:
        return "no finished tasks yet"
    by = defaultdict(list)
    for r in rows:
        by[(r["model"], r.get("size") or "?")].append(r)
    out = [f"{'model':10} {'size':12} {'tasks':>5} {'accepted':>8} {'rounds':>6} "
           f"{'autofix':>7} {'reviews':>7} {'min':>6} {'local tok':>10}"]
    for (model, size), rs in sorted(by.items()):
        n = len(rs)
        acc = sum(r["outcome"] == "accepted" for r in rs)
        avg = lambda k: sum(r.get(k) or 0 for r in rs) / n  # noqa: E731
        out.append(f"{model:10} {size:12} {n:>5} {acc / n:>7.0%} {avg('rounds'):>6.1f} "
                   f"{avg('auto_fixes'):>7.1f} {avg('reviews'):>7.1f} {avg('minutes'):>6.1f} "
                   f"{int(avg('tok_in') + avg('tok_out')):>10}")
    return "\n".join(out)
