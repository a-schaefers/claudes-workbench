"""results.jsonl: one row per closed task, so the models can be compared on real work."""
from collections import defaultdict
from statistics import median

from . import context, gitops, meter, skills, spend, stats, store

PROVIDER = meter.PROVIDER  # _api_usage's local `meter` shadows the module there


def step(task: store.Task, landed: bool, since: str = "HEAD", gaps=None) -> dict:
    """The step closing now (committed by wb next or accept, or dropped): its size and how it
    went. A session compacted (now, or before a fresh one took over) or filled by reading alone,
    an agent stuck, or review rounds the agent caused (`reviews` less the `spec_rounds` Claude
    marked as the spec's) say it was too much; Claude taking over says nothing of its size; a
    handover to a fresh session is the runner's normal way through a big step, counted, not held
    against it. The router learns each model's slice from these (router.slice). ``since``: where
    the step began (HEAD~1 once its commit has landed: a retried wb next). ``gaps``: the
    --spec-gap the closing command is about to record (cli._spec_gaps), counted for this round:
    wb next and wb accept measure the step first, and a gap given there was held against the
    agent's slice as its own review round (#904)."""
    st = task.status
    first = st.get("step_round") or 1
    try:
        size = gitops.footprint(task.worktree, since)  # the step's work is uncommitted until now
    except (gitops.GitError, OSError):
        size = {}
    # A `fresh` event at a later step's first round replaced the session the step before left
    # (compacted or full when wb next ran: a mid-round handover's is at the round after), and it judges
    # that step (whose own measure saw a compaction), not this one. Kept, a clean step 2 after a compacted step 1 was measured
    # compacted, went badly and shrank the model's slice (2026-11-21, the audit of #958).
    evs = [e for e in store.read_jsonl(task.dir / "events.jsonl")[0] if (e.get("round") or 0) >= first
           and not (first > 1 and e.get("kind") == "fresh" and e.get("round") == first)]
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
    gaps = {g.get("round") for g in st.get("spec_gaps") or [] if (g.get("round") or 0) >= first} \
        | ({st.get("round", 1)} if gaps else set())  # a retry's, recorded already: one round
    return {**size, "window": st.get("window"), "landed": landed, "compacted": compacted,
            "handovers": sum(e.get("kind") == "fresh" and bool(e.get("overflow")) for e in evs),
            "read_full": any(e.get("kind") == "reading_full" for e in evs),  # before any change
            "reviews": reviews, "spec_rounds": min(len(gaps), reviews),
            "stuck": stuck, "took_over": any(e.get("kind") == "takeover" for e in evs)}


# The causes of a stuck or failed notice that are the step's own: a guard stopped the agent, or
# OpenCode exited non-zero. Any other ends a round for reasons outside the step: a shutdown's,
# a logout's or a kill's SIGTERM (`signal`, runner.SIGNAL_STOP), a runner that died (`died`,
# cli.reap: killed, out of memory, a reboot), a model server gone again after the round's one
# revive (`server`, #657), an API provider's rate limit, outage, quota or refused key (`provider`,
# runner.PROVIDER_GUARDS, #1020), the day's API spend cap (`spend`, runner.SPEND, #1019: the user's
# budget, not the model's), workbench updated under the task (`updated`, runner._updated). Counted
# as stuck, a laptop shut down mid-step shrank the model's slice for good (#582, the audit of
# 1.90), and so did an update mid-step until it had a cause (#660): an allowlist, so the next
# stop from outside the step can't slip in. Notices from before `cause` are known by their text.
THE_STEP = ("guard", "agent")
_NOT_THE_STEP_TEXT = ("the runner was stopped by a signal", "the runner died without a report",
                      "workbench was updated under this task")  # runner.UPDATED


def _not_the_step(notice: dict) -> bool:
    if "cause" in notice:
        return notice["cause"] not in THE_STEP
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
    # a discard keeps the steps wb next committed, unless --drop-steps dropped them (#722)
    landed = sum(1 for s in earlier if s.get("landed")) \
        if outcome == "discarded" and not extra.get("steps_dropped") else 0
    u, per_step = spend.usage_by_step(task)  # what it cost Claude, and each step's share (#558)
    if per_step and len(per_step) == len(steps):
        steps = [{**s, "claude": c} for s, c in zip(steps, per_step)]
    row = {"ts": store.now(), "task": task.name, "outcome": outcome, "model": spec.get("model"),
           "size": spec.get("size"), "repo": spec.get("repo"), "stack": spec.get("stack"),
           **({"role": spec["role"]} if spec.get("role") else {}),  # router.of_role (#1025)
           **({"subdir": spec["subdir"]} if spec.get("subdir") else {}),  # the package (#820)
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
           **({"landed_steps": landed} if landed else {}),
           "window": st.get("window"),
           **({"weights_id": spec["weights_id"]} if spec.get("weights_id") else {}),  # #726
           # an API task's thinking, off or on (the weekly check's evidence, #1018); a GPU task
           # thinks as its OpenCode entry says, so it has none
           **({"thinking": spec["thinking"]} if spec.get("thinking_shown") and spec.get("thinking") else {}),
           "tuned": st.get("tuned") or {}, "explored": st.get("explored") or [],  # tuning.py
           "tuned_max": st.get("tuned_max") or {},
           **({"peak_share": st["peak_share"]} if "peak_share" in st else {}),  # tuning (#721)
           "self_review_due": bool(st.get("self_review_due")),  # tuning.acted (#816)
           "spec_gaps": [g["text"] for g in st.get("spec_gaps") or []],
           **({"backlog": spec["backlog"]} if spec.get("backlog") else {}),  # wb backlog
           **({"claude": u} if u else {}),
           **({"gpu_wh": st["gpu_wh"]} if "gpu_wh" in st else {}),  # the runner's GPU energy
           **({"remote_gpu": True} if st.get("remote_gpu") else {}),  # another machine's: unmeasured
           **({"active_minutes": round(st["active_s"] / 60, 1)} if st.get("active_s") else {}),
           **({"interventions": st["interventions"]} if st.get("interventions") else {}),  # #95
           **({"diff": {"files": sum(s.get("files", 0) for s in steps),  # what savings.py prices
                        "added": sum(s.get("lines", 0) for s in steps),
                        "removed": sum(s.get("removed", 0) for s in steps),
                        # apart, so a later estimate can price them its own way (#725)
                        **_apart(steps, "generated", ("files", "lines", "removed")),
                        **_apart(steps, "deleted", ("files", "removed"))}}
              if outcome == "accepted" else {}),
           **({"trial": True} if spec.get("trial") else {}),
           # on only: a task with every experiment off ran as main does
           **({"experiments": on} if (on := sorted(k for k, v in (spec.get("experiments") or {}).items()
                                                    if v)) else {}),
           # how much the agent used each (#857), and its whole reads of long files
           **({"experiment_use": {k: (st.get("experiment_use") or {}).get(k, 0) for k in on}} if on else {}),
           # with probe on, whether the spec named a long file, the tasks where on does more than
           # cap the output (#857); `experiments` says it was on. Its level and setting went with
           # the levels (the owner, 2026-11-22, #857: off and on, on is large files only)
           **({"probe_long": bool(spec.get("probe_files"))} if context.experiment(task, "probe") else {}),
           "long_reads": st.get("long_reads", 0), **for_stats(task), **extra}
    row = _with_route(task, row)
    store.append_jsonl(store.home() / "results.jsonl", row)
    return row


# --- API tasks (#1022): what the row says of the route, and the money -------------------------

TOKEN_KEYS = ("in", "out", "cache_read", "cache_write", "reasoning")
PLAN_PROVIDERS = meter.PLAN_PROVIDERS  # a quota, not dollars: the meter's one list (#1019)


def api_row(row: dict) -> bool:
    """A results row (or bench row) of a task on an API model; a row without `kind` is a GPU one,
    an Ollama `:cloud` one too: it was routed as a remote GPU model. (models.is_api takes a model
    id; this takes a row.)"""
    return isinstance(row, dict) and row.get("kind") == "api"


def task_kind(spec: dict) -> str:
    """task.json `via` (#1016): "api", or "gpu" for every task without it."""
    return "api" if spec.get("via") == "api" else "gpu"


def _with_route(task: store.Task, row: dict) -> dict:
    """The row's `kind`, `role` and `provider`, and for an API task its money (`api`). An API
    task's GPU never ran it: no `gpu_wh` (the runner's sampler is off for it) and no
    `remote_gpu` (hosted() models are remote, so the runner set it, #117's line for another
    machine's GPU, untrue for a provider's servers)."""
    spec = task.spec
    kind, model = task_kind(spec), str(spec.get("model") or "")
    row = {**row, "kind": kind, "role": spec.get("role"),
           "provider": spec.get("provider") or (model.split("/", 1)[0] if "/" in model else None)}
    if kind == "api":
        row.pop("gpu_wh", None)
        row.pop("remote_gpu", None)
        row["api"] = api_usage(task)
    return row


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def catalog_price(model_id: str) -> float | None:
    """What the catalog says the model costs: None with no price (unknown), 0.0 when every price
    is 0 (a free model: a true 0), else 1.0 (priced; the meter has the rates). From hosted.json
    as it is: never a catalog read at a close or a poll."""
    from . import models
    try:
        m = models.lookup(model_id, cached=True)
    except Exception:  # a config it can't read: the price is unknown, never 0
        return None
    if m is None:
        return None
    if m.free:
        return 0.0
    nums: list = []

    def walk(v):
        if _num(v):
            nums.append(v)
        elif isinstance(v, dict):
            for k, x in v.items():
                if k != "tier":  # a tier's `size` is tokens, not a price
                    walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
    walk((m.config or {}).get("cost"))
    if not nums:
        return None
    return 0.0 if not any(nums) else 1.0


def _plan(model_id: str) -> str | None:
    """The plan a model's usage counts against (OpenCode Go, an Ollama cloud model): its provider,
    else None. An Ollama cloud model by the catalog's `cloud` flag (#1013), never by its name: any
    provider's model may end in `-cloud`, and its dollars would go uncounted (#1022's review)."""
    from . import models
    pid = model_id.partition("/")[0]
    if pid in PLAN_PROVIDERS:
        return pid
    try:
        m = models.lookup(model_id, cached=True)
    except Exception:  # a config it can't read: no plan known, so the dollars are looked for
        return None
    return pid if m is not None and m.cloud else None


def metered(st: dict) -> float | None:
    """The runner's live meter in status (#1019's `api.usd`), or None."""
    v = st.get("api")
    return float(v["usd"]) if isinstance(v, dict) and _num(v.get("usd")) else None


def _step_rows(task: store.Task) -> list[dict]:
    return [e for e in store.read_jsonl(task.dir / "events.jsonl")[0]
            if isinstance(e, dict) and e.get("kind") == "step"]


def step_cost(rows: list) -> float:
    """The step events' summed `cost` (OpenCode's own, live), over whatever rows it is given."""
    return sum(e["cost"] for e in rows if isinstance(e, dict) and e.get("kind") == "step"
               and _num(e.get("cost")))


def spend_so_far(task: store.Task) -> float | None:
    """An API task's dollars so far (live_spend, from its own files: every step, not a tail)."""
    return live_spend(str(task.spec.get("model") or ""), task.status, step_cost(_step_rows(task)))


def live_basis(st: dict) -> str:
    """The basis of a task's dollars so far (meter.basis): the runner's meter's status `api.prices`,
    else the catalog's (#1135)."""
    v = st.get("api")
    b = v.get("prices") if isinstance(v, dict) else None
    return b if b in meter.NOTE else meter.CATALOG


def live_spend(model_id: str, st: dict, total: float) -> float | None:
    """A live API task's dollars so far: the runner's meter, else ``total``, the step events'
    summed `cost` over the whole events.jsonl (a tail's sum was a fraction of a long task's,
    #1022's review; wb top keeps a running sum), else 0 for a free model; None when unknown (never
    a false 0: an unpriced model's steps say 0)."""
    if (v := metered(st)) is not None:
        return v
    if total > 0:
        return round(total, 6)
    return 0.0 if catalog_price(model_id) == 0 else None


def _session_cost(data: dict) -> float | None:
    """One exported session's cost: `info.cost` (it counts the title request, which no message
    carries: the spike for #1009), else the assistant messages' summed; None with neither."""
    info = data.get("info") if isinstance(data.get("info"), dict) else {}
    if _num(info.get("cost")):
        return float(info["cost"])
    costs = [m["cost"] for m in data.get("messages") or []
             if isinstance(m, dict) and m.get("type") == "assistant" and _num(m.get("cost"))]
    return float(sum(costs)) if costs else None


def served_model(asked: str, got) -> str | None:
    """The model a response said served it (status `served.model`, the keyhole's), when it isn't
    the one asked for; None when it is or none was said. Providers echo the id with or without a
    provider prefix (OpenRouter's `deepseek/x` for `openrouter/deepseek/x`), so only the last part
    is compared, any case: a dated snapshot for an alias is a different model, and recorded. Only
    `served.by` reached the row, so a provider's swap was gone from results once the task closed
    (#1122, 2026-11-23)."""
    if not isinstance(got, str) or not got:
        return None
    last = lambda s: s.rsplit("/", 1)[-1].lower()  # noqa: E731
    return None if last(got) == last(asked) else got


def api_usage(task: store.Task) -> dict | None:
    """An API task's money at its close, for its row's `api`: {cost, tokens, source}.
    Summed over every session it had (status `sessions` and `session`: handovers, revives and wb
    next each start one), and `backend`, OpenRouter's served provider (status `served.by`, the
    keyhole's), when known, and `served_model`, the model the response named when it isn't the
    one asked for (`served_model`). `source`: "opencode" (OpenCode's own cost, from every session's
    export), "mixed" (the sessions that exported, the rest filled from the meter), "metered"
    (the runner's meter when every row it booked is the provider's own figure, #1165, or when it
    counted more than every export: the day cap's figure, #1019; else,
    with no full export, the runner's meter or the step events' cost: a floor, both miss
    OpenCode's title and compaction requests), "plan" (a quota: no dollars; the meter's status
    `api.plan` too), "none" (nothing sent or billed: $0), "unknown" (no price anywhere),
    "unrecorded" (a priced model, no figure came back). `cost` is
    None unless known, never 0 for a missing price: a 0 from OpenCode for a model the catalog
    prices isn't trusted. None for a GPU task. The order: every session's export, then some
    sessions' plus the meter's remainder, then the meter, then the step events' sum (both floors,
    `metered`), then a free model's 0, then nothing sent's 0. A known cost says its `prices`
    (meter.basis: the meter's; OpenCode's export is never the provider's own figure, #1135)."""
    out = _api_usage(task)
    if out is not None and out.get("cost") is not None and out["source"] not in ("plan", "none"):
        b = live_basis(task.status)
        out["prices"] = meter.CATALOG if b == meter.PROVIDER and out["source"] in ("opencode", "mixed") else b
    return out


def row_basis(row: dict) -> str:
    """A results or bench row's money basis (`api.prices`, `paid.prices`): a row from before
    #1135 is the catalog's."""
    for key in ("api", "paid"):
        v = row.get(key)
        if isinstance(v, dict) and v.get("prices") in meter.NOTE:
            return v["prices"]
    return meter.CATALOG


def _api_usage(task: store.Task) -> dict | None:
    spec, st = task.spec, task.status
    if task_kind(spec) != "api":
        return None
    from . import transcript
    model = str(spec.get("model") or "")
    steps = _step_rows(task)
    tokens = {k: 0 for k in TOKEN_KEYS}
    for e in steps:
        for k in TOKEN_KEYS:
            if _num(e.get(k)):
                tokens[k] += e[k]
        if "cache_read" not in e and _num(e.get("cache")):  # a row from before the split
            tokens["cache_read"] += e["cache"]
    out = {"cost": None, "tokens": tokens, "source": "unknown"}
    served = st.get("served") if isinstance(st.get("served"), dict) else {}
    if isinstance(served.get("by"), str) and served["by"]:  # OpenRouter's backend, the last seen
        out["backend"] = served["by"]
    if (got := served_model(model, served.get("model"))) is not None:
        out["served_model"] = got
    # the meter's own `api.plan` too: an Ollama cloud model hosted.json no longer lists at close
    # read as no price where the meter had counted it as plan usage (#1106, 2026-11-23)
    booked = st.get("api") if isinstance(st.get("api"), dict) else {}
    if (plan := _plan(model) or (booked.get("plan") if isinstance(booked.get("plan"), str)
                                 and booked["plan"] else None)) is not None:
        return {**out, "source": "plan", "plan": plan}
    price = catalog_price(model)
    # every row the meter booked is the provider's own figure (DeepInfra's estimated_cost,
    # OpenRouter's cost): that, over OpenCode's export at catalog prices, which ran about 2x
    # DeepInfra's bill in every closed task's `paid` (#1165, 2026-11-24)
    if live_basis(st) == PROVIDER and (live := metered(st)) is not None:
        return {**out, "cost": round(live, 6), "source": "metered"}
    cur = st.get("session")
    sids = list(dict.fromkeys([*(s for s in st.get("sessions") or [] if s), *([cur] if cur else [])]))
    costs = []
    for sid in sids:
        try:
            data = transcript.export(sid, task=task) if sid == cur else transcript.finished_export(task, sid)
        except Exception:  # an export that can't be read: this session's cost is unknown
            data = {}
        costs.append(_session_cost(data) if isinstance(data, dict) else None)
    said = [c for c in costs if c is not None]
    if said and len(said) == len(costs) and (sum(said) > 0 or price == 0):
        # never below the runner's meter, the ledger the day cap counted: it adds the provider's
        # own usage.cost and answers a steer or stop cut short, which OpenCode doesn't bill, and the
        # row once said $0.30 where the ledger and the round report said $0.50 (wave 2's review)
        if (live := metered(st)) is not None and live > sum(said):
            return {**out, "cost": round(live, 6), "source": "metered"}
        return {**out, "cost": round(sum(said), 6), "source": "opencode"}
    # the meter: the runner's, else the step events' cost (OpenCode's per step, but no step
    # carries its title or compaction requests, so a floor like the meter and labelled as one,
    # #1022's review). Both are the whole task's, every session's.
    meter = metered(st)
    if meter is None and (live := [e["cost"] for e in steps if _num(e.get("cost"))]) and sum(live) > 0:
        meter = float(sum(live))
    if said and len(said) < len(costs) and meter is not None:
        # some sessions exported, some didn't (the engineer's call, #1022): the exported ones'
        # own cost, the rest filled from the meter, the part of it those don't account for.
        # Dropping the whole sum for one failed export threw away the sessions that said theirs
        total = sum(said) + max(0.0, meter - sum(said))
        if total > 0:
            return {**out, "cost": round(total, 6), "source": "mixed"}
    if meter is not None:
        return {**out, "cost": round(meter, 6), "source": "metered"}
    if price == 0:
        return {**out, "cost": 0.0, "source": "opencode"}
    # nothing came back to bill: no session cost above 0, no meter, no token and no response that
    # named its model (status `served`). A bad --setup, a discard while queued, a key or quota
    # refused: these read "paid: unknown (no price for this model)" for a priced model, and the
    # week's spend "1 with no price" (#1106, 2026-11-23). A cut-off answer that named its model
    # stays unknown: it may have been billed
    if not ran(task) or (sum(said) == 0 and not any(tokens.values()) and not served.get("model")):
        return {**out, "cost": 0.0, "source": "none"}
    # a figure missing for a model the catalog prices is not "no price" (#1106)
    return out if price is None else {**out, "source": "unrecorded"}


def row_cost(row: dict) -> float | None:
    """A results or bench row's known dollars (`api.cost`, a bench row's `paid.cost`), or None."""
    for key in ("api", "paid"):
        v = row.get(key)
        if isinstance(v, dict) and _num(v.get("cost")):
            return float(v["cost"])
    return None


def _source(row: dict, source: str) -> bool:
    return any(isinstance(row.get(k), dict) and row[k].get("source") == source
               for k in ("api", "paid"))  # a bench row's is `paid`


def task_cost(row: dict) -> float | None:
    """A row's dollars for a mean a task (`paid`, the weekly check's ~$/task): row_cost, but None
    for a task that sent nothing (source "none"). Its $0 is spent money in the totals, never a
    price a task: a bad --setup or a first run with no key pulled a model's mean down (#1106,
    2026-11-23)."""
    return None if _source(row, "none") else row_cost(row)


def paid_cell(rows: list[dict]) -> str:
    """The tables' `paid`: the mean dollars a task over the rows with a known cost (task_cost:
    a task that sent nothing is none); `-` for GPU rows, `plan` when every API row is plan usage,
    `?` when none is known, `~$0.12` when some are unknown (plan rows aside: a quota is no
    unknown price, and they read `?`, #1106; nothing-sent rows too)."""
    api = [r for r in rows if api_row(r)]
    if not api:
        return "-"
    priced = [r for r in api if not _source(r, "plan")]
    if not priced:
        return "plan"
    priced = [r for r in priced if not _source(r, "none")]
    known = [(c, r) for r in priced if (c := task_cost(r)) is not None]
    if not known:
        return "?"
    from .savings import usd
    est = meter.basis(row_basis(r) for _, r in known) == meter.CATALOG  # #1135: ≈, an estimate
    return ("~" if len(known) < len(priced) else "≈" if est else "") + usd(sum(c for c, _ in known) / len(known))


def _apart(steps: list[dict], key: str, fields: tuple) -> dict:
    """{key: the steps' `key` counts summed} when any step has them, else nothing."""
    have = [s[key] for s in steps if isinstance(s.get(key), dict)]
    return {key: {f: sum(h.get(f, 0) for h in have) for f in fields}} if have else {}


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
    shown = [r for r in every if only is None or r.get("model") in only]
    rows = [r for r in shown if judged(r)]
    # each kind left out counted apart: every row not shown was called a spec-gap discard, so
    # 40 tasks on a retired model read "(41 discards put down to the spec left out)" (#817)
    gaps = sum(1 for r in shown if r.get("outcome") == "discarded" and r.get("spec_gaps"))
    never = len(shown) - len(rows) - gaps
    trials = len(store.results(trials=True)) - len(every)
    left_out = (f"\n({trials} trial run{'s' if trials > 1 else ''} left out)" if trials else "") + \
        (f"\n({gaps} discard{'s' if gaps > 1 else ''} put down to the spec left out)" if gaps else "") + \
        (f"\n({never} task{'s' if never > 1 else ''} whose agent never ran left out)" if never else "")
    if not rows:
        return "no finished tasks yet" + left_out
    by = defaultdict(list)
    for r in rows:  # by kind and role (#1022), a row from before roles by its size: one id run as
        # a GPU model and then an API one (an Ollama `:cloud` id, #1013) keeps two records
        by[(r["model"], "api" if api_row(r) else "gpu", r.get("role") or r.get("size") or "?")].append(r)
    claude = any("claude" in r for r in rows)
    # `kind`, `paid` and `agent tok` (#1022) only with an API row shown: a GPU-only table reads as
    # before (`size`, `local tok`), and no id is cut, so two long ones never read alike (wave 2's
    # review: two qwen3-coder quantizations both read `ollama/qwen3-coder:30b-a3b-ins`)
    api = any(kind == "api" for _, kind, _ in by)
    width = max([30 if api else 34] + [len(model) for model, _, _ in by])
    work = "role" if api or any(w not in ("trivial", "significant", "?") for _, _, w in by) else "size"
    ww = 11 if work == "role" else 12
    out = [f"{'model':{width}} " + (f"{'kind':4} " if api else "") + f"{work:{ww}} {'tasks':>5} "
           f"{'accepted':>8} {'rounds':>6} {'autofix':>7} {'reviews':>7} {'min':>6} "
           + (f"{'agent tok':>9} {'paid':>7}" if api else f"{'local tok':>10}")
           + (f" {'claude':>9}" if claude else "")]
    for (model, kind, role), rs in sorted(by.items()):
        n = len(rs)
        acc = sum(r["outcome"] == "accepted" for r in rs)
        avg = lambda k: sum(r.get(k) or 0 for r in rs) / n  # noqa: E731
        tok = int(avg('tok_in') + avg('tok_out'))
        out.append(f"{model:{width}} " + (f"{kind:4} " if api else "") + f"{role[:ww]:{ww}} {n:>5} "
                   f"{acc / n:>7.0%} {avg('rounds'):>6.1f} "
                   f"{avg('auto_fixes'):>7.1f} {avg('reviews'):>7.1f} {avg('minutes'):>6.1f} "
                   + (f"{tok:>9} {paid_cell(rs):>7}" if api else f"{tok:>10}")
                   + (f" {_claude_avg(rs):>9}" if claude else ""))
    if api and any(api_row(r) and row_cost(r) and row_basis(r) == meter.CATALOG for r in rows):
        left_out = "\npaid: ≈ is at catalog prices, an estimate" + left_out  # #1135
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
