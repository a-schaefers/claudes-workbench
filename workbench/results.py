"""results.jsonl: one row per closed task, so the models can be compared on real work."""
from collections import defaultdict
from statistics import median

from . import gitops, skills, spend, stats, store


def step(task: store.Task, landed: bool, since: str = "HEAD") -> dict:
    """The step closing now (committed by wb next or accept, or dropped): its size and how it
    went. A session compacted (now, or before a fresh one took over) or filled by reading alone,
    an agent stuck, or review rounds the agent caused (`reviews` less the `spec_rounds` Claude
    marked as the spec's) say it was too much; Claude taking over says nothing of its size; a
    handover to a fresh session is the runner's normal way through a big step, counted, not held
    against it. The router learns each model's slice from these (router.slice). ``since``: where
    the step began (HEAD~1 once its commit has landed: a retried wb next)."""
    st = task.status
    first = st.get("step_round") or 1
    try:
        size = gitops.footprint(task.worktree, since)  # the step's work is uncommitted until now
    except (gitops.GitError, OSError):
        size = {}
    evs = [e for e in store.read_jsonl(task.dir / "events.jsonl")[0] if (e.get("round") or 0) >= first]
    stuck = any(r.get("task") == task.name and r.get("kind") in ("stuck", "failed")
                and (r.get("round") or 0) >= first and not _not_the_step(r)
                for r in store.notices())
    from .runner import context_state  # the runner imports what this module does
    try:
        compacted = bool(context_state(task).get("compactions"))
    except Exception:  # no session to export: nothing was compacted
        compacted = False
    compacted |= any(e.get("kind") == "fresh" and e.get("compacted") for e in evs)  # an earlier session
    reviews = st.get("reviews", 0)  # wb next starts each step's count afresh
    gaps = {g.get("round") for g in st.get("spec_gaps") or [] if (g.get("round") or 0) >= first}
    return {**size, "window": st.get("window"), "landed": landed, "compacted": compacted,
            "handovers": sum(e.get("kind") == "fresh" and bool(e.get("overflow")) for e in evs),
            "read_full": any(e.get("kind") == "reading_full" for e in evs),  # before any change
            "reviews": reviews, "spec_rounds": min(len(gaps), reviews),
            "stuck": stuck, "took_over": any(e.get("kind") == "takeover" for e in evs)}


# Notices that end a round for reasons outside the step: a shutdown's, a logout's or a kill's
# SIGTERM (runner.SIGNAL_STOP), and a runner that died (cli.reap: killed, out of memory, a reboot).
# Counted as stuck, a laptop shut down mid-step shrank the model's slice for good (#582, the audit
# of 1.90). Notices from before `cause` was written are known by their text.
NOT_THE_STEP = ("signal", "died")
_NOT_THE_STEP_TEXT = ("the runner was stopped by a signal", "the runner died without a report")


def _not_the_step(notice: dict) -> bool:
    if "cause" in notice:
        return notice["cause"] in NOT_THE_STEP
    return any(t in str(notice.get("text") or "") for t in _NOT_THE_STEP_TEXT)


def ran(task: store.Task) -> bool:
    """Whether the task's agent ever got a request to its model: a session began (OpenCode's first
    event names it) or a token was written. A task that failed on its setup or its model's load
    never did, and discarded, it counted against the model as rejected work (#581)."""
    st = task.status
    return bool(st.get("session") or st.get("sessions") or st.get("tok_out"))


def record(task: store.Task, outcome: str, last_step: dict | None = None, **extra) -> dict:
    spec, st = task.spec, task.status
    if spec.get("bench"):  # a replay: its row is bench/runs.jsonl's
        return {}
    earlier = st.get("step_sizes") or []
    reviews = sum(s.get("reviews") or 0 for s in earlier) + st.get("reviews", 0)  # every step's
    steps = earlier + ([last_step] if last_step else [])
    u, per_step = spend.usage_by_step(task)  # what it cost Claude, and each step's share (#558)
    if per_step and len(per_step) == len(steps):
        steps = [{**s, "claude": c} for s, c in zip(steps, per_step)]
    row = {"ts": store.now(), "task": task.name, "outcome": outcome, "model": spec.get("model"),
           "size": spec.get("size"), "repo": spec.get("repo"), "stack": spec.get("stack"),
           "rounds": st.get("round", 1),
           "auto_fixes": st.get("auto_fixes_total", 0) + st.get("auto_fixes", 0),
           "reviews": reviews,
           "spec_rounds": min(len({g.get("round") for g in st.get("spec_gaps") or []}), reviews),
           "tests_ok": (st.get("tests") or {}).get("ok"),
           "minutes": round((store.now() - spec.get("created_at", store.now())) / 60, 1),
           "tok_in": st.get("tok_in", 0), "tok_out": st.get("tok_out", 0),
           "ran": (went := ran(task)),
           **({"failed_before": st["failed_before"]} if not went and st.get("failed_before") else {}),
           "skills_offered": st.get("skills_ever") or [], "skills_loaded": skills.loaded(task),
           "skills_rev": st.get("skills_rev") or {},  # the version handed: judges each edit
           "steps": steps,
           "window": st.get("window"),
           "tuned": st.get("tuned") or {}, "explored": st.get("explored") or [],  # tuning.py
           "tuned_max": st.get("tuned_max") or {},
           **({"peak_share": st["peak_share"]} if "peak_share" in st else {}),  # tuning (#721)
           "spec_gaps": [g["text"] for g in st.get("spec_gaps") or []],
           **({"backlog": spec["backlog"]} if spec.get("backlog") else {}),  # wb backlog
           **({"claude": u} if u else {}),
           **({"gpu_wh": st["gpu_wh"]} if "gpu_wh" in st else {}),  # the runner's GPU energy
           **({"remote_gpu": True} if st.get("remote_gpu") else {}),  # another machine's: unmeasured
           **({"active_minutes": round(st["active_s"] / 60, 1)} if st.get("active_s") else {}),
           **({"interventions": st["interventions"]} if st.get("interventions") else {}),  # #95
           **({"diff": {"files": sum(s.get("files", 0) for s in steps),  # what savings.py prices
                        "added": sum(s.get("lines", 0) for s in steps),
                        "removed": sum(s.get("removed", 0) for s in steps)}}
              if outcome == "accepted" else {}),
           **({"trial": True} if spec.get("trial") else {}),
           # on only: a task with every experiment off ran as main does
           **({"experiments": on} if (on := sorted(k for k, v in (spec.get("experiments") or {}).items()
                                                    if v)) else {}), **for_stats(task), **extra}
    store.append_jsonl(store.home() / "results.jsonl", row)
    return row


def for_stats(task: store.Task) -> dict:
    """What only the Stats tab reads (#404), with stats recording on: when the task began, its
    load and setup time, the runner's nudges, the questions it asked, the guards that stopped
    its rounds, its own median speeds and time to first token, and the GPU's mean and peak watts
    while it ran. A value never measured is left out, not written as zero."""
    if not stats.recording():
        return {}
    spec, st = task.spec, task.status
    speeds = {k: round(median(v), 2) for k, v in (st.get("speeds") or {}).items() if v}
    keep = {"started_at": spec.get("created_at"), "load_secs": st.get("load_secs"),
            "setup_secs": st.get("setup_secs"), "nudges": st.get("nudges"),
            "asks": _asks(st.get("asks") or {}, st.get("answered") or []) or None, "guards": st.get("guards"),
            "speed": speeds or None,
            "gpu_w": {"mean": round(st["gpu_w_sum"] / st["gpu_w_n"], 1),
                      "peak": round(st.get("gpu_w_peak") or 0, 1)} if st.get("gpu_w_n") else None}
    return {k: v for k, v in keep.items() if v is not None}


def _asks(asks: dict, answered: list | None = None) -> dict:
    """The ask_reviewer calls by kind (status `asks`: "q" a question, "p" a command or path
    request, "x" one refused), and the questions `wb answer` answered (status `answered`; one
    timed out or outliving its round got NO_ANSWER, which isn't one, #562); none left out."""
    names = {"q": "questions", "p": "permissions", "x": "refused"}
    out = {}
    for kind in asks.values():
        if kind in names:
            out[names[kind]] = out.get(names[kind], 0) + 1
    if n := sum(1 for q in set(answered or []) if asks.get(q) == "q"):
        out["answered"] = n
    return out


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


def table(only: set | None = None) -> str:
    """`wb stats`' table; ``only``: these models' rows alone (the ones the Stats tab shows)."""
    from .tuning import judged  # tuning reads results' rows
    every = store.results()
    rows = [r for r in every if judged(r) and (only is None or r.get("model") in only)]
    gaps = len(every) - len(rows)
    trials = len(store.results(trials=True)) - len(every)
    left_out = (f"\n({trials} trial run{'s' if trials > 1 else ''} left out)" if trials else "") + \
        (f"\n({gaps} discard{'s' if gaps > 1 else ''} put down to the spec left out)" if gaps else "")
    if not rows:
        return "no finished tasks yet" + left_out
    by = defaultdict(list)
    for r in rows:
        by[(r["model"], r.get("size") or "?")].append(r)
    claude = any("claude" in r for r in rows)
    out = [f"{'model':34} {'size':12} {'tasks':>5} {'accepted':>8} {'rounds':>6} "
           f"{'autofix':>7} {'reviews':>7} {'min':>6} {'local tok':>10}"
           + (f" {'claude':>9}" if claude else "")]
    for (model, size), rs in sorted(by.items()):
        n = len(rs)
        acc = sum(r["outcome"] == "accepted" for r in rs)
        avg = lambda k: sum(r.get(k) or 0 for r in rs) / n  # noqa: E731
        out.append(f"{model:34} {size:12} {n:>5} {acc / n:>7.0%} {avg('rounds'):>6.1f} "
                   f"{avg('auto_fixes'):>7.1f} {avg('reviews'):>7.1f} {avg('minutes'):>6.1f} "
                   f"{int(avg('tok_in') + avg('tok_out')):>10}"
                   + (f" {_claude_avg(rs):>9}" if claude else ""))
    if claude:
        unknown = sum("claude" not in r for r in rows)
        left_out = ("\nclaude: what a task cost Claude, its calls in the session that ran it, in "
                    "input-equivalent tokens; averaged over the tasks that recorded it"
                    + (f" ({unknown} had no session or transcript)" if unknown else "")
                    + left_out)
    return "\n".join(out) + left_out


def _claude_avg(rows: list[dict]) -> str:
    known = [spend.weighted(r["claude"]) for r in rows if isinstance(r.get("claude"), dict)]
    return f"{sum(known) / len(known) / 1000:.0f}K" if known else "-"
