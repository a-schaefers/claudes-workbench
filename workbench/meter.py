"""The API meter (#1019): what an API task's model costs, a ledger of it per local day, and the
one day cap over every API provider together (routes.json `day_usd`, the user's).

Prices are the catalog's (`Model.config["cost"]`, per million tokens), priced the way OpenCode
prices itself (the spike for #1009): `input` leaves out cache reads, cache reads and writes at
their own rates, reasoning at the output rate (the reasoning rate when one is listed), and a
`tier: {type: "context", size}` price past its size. OpenCode's own figure is that same
estimate, so wb meters the same way rather than at the cache-miss rate, which over-counted a
cached context about 50 times. Where the split isn't known it never prices under: a usage
that doesn't say its cache reads prices them as input, a missing cache price is the input's.

Two views of the same requests, each kept as high as the other says (never under):
- the keyhole's (`Meter.on_usage`, its thread): each upstream response's `usage`, every request
  the task's model got (titles and compaction too, which no `step_finish` carries). A
  provider's own `usage.cost` (OpenRouter's) wins over the catalog;
- OpenCode's: live, each `step_finish.part.cost` (`Meter.step`); at each round's end the
  sessions' exported `info.cost` (`Meter.end_round`). With the keyhole metering, these only add
  a `reconcile` row when OpenCode says more than the ledger has; without it (no usage in the
  answers, the suite's fake), each step books its own row, OpenCode's cost or the metered one.
An OpenCode cost of 0 for a model whose price isn't 0 is never trusted (never a false 0).

The ledger is `$WB_HOME/spend-api.jsonl` (store.append_jsonl, locked): `step`, `reconcile` and
`checkin` rows, each with its local `day`. A plan (OpenCode Go, an Ollama cloud model) is a
quota, not dollars: its rows have `usd: null` and `plan`, and never count against the cap."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from . import models, routes, store

CHECKIN_SHARE = 0.5      # past this share of the day cap, one check-in a day, across all tasks
PLAN_PROVIDERS = ("opencode-go",)   # a plan's quota, not dollars (and m.cloud: Ollama cloud)
EPS = 1e-9               # less than this between two figures is the same figure
STOP = "the day's API spend cap is reached"
REFUSED = f"workbench: {STOP}"   # the keyhole's answer to OpenCode: a 400, which it doesn't retry
NO_PRICE = "OpenCode's catalog gives no price for it, so the day cap can't be kept"
# routes.json without day_usd: #1014 refuses an API role without one, so only a hand-edited file
# gets here, and an API task then spends nothing rather than without a limit
NO_CAP = "there is no API day cap (routes.json day_usd): ask the user for one (wb route cap <usd>)"


def ledger_path() -> Path:
    return store.home() / "spend-api.jsonl"


def day(ts: float | None = None) -> str:
    """The local calendar day of ``ts``: the cap resets at local midnight."""
    return time.strftime("%Y-%m-%d", time.localtime(time.time() if ts is None else ts))


def _routes_path() -> Path:
    return routes.path()


def _routes() -> dict:
    """routes.json (#1014's routes.read). One that can't be read gives no cap, so an API task
    spends nothing (NO_CAP) rather than without a limit."""
    try:
        return routes.read()
    except routes.RoutesError:
        return {}


_CAP: dict = {}  # routes.json's day_usd by the file's (path, mtime, size): the guard asks per tick


def cap() -> float | None:
    """The day cap in USD (routes.json `day_usd`), or None when the user set none."""
    path = _routes_path()
    try:
        st = os.stat(path)
        at = (str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        at = (str(path), None, None)
    if _CAP.get("at") != at:
        v = _routes().get("day_usd")
        _CAP.update(at=at, cap=float(v) if _num(v) and v >= 0 else None)
    return _CAP["cap"]


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v


def is_plan(m) -> bool:
    """A plan's quota, not dollars: OpenCode Go, an Ollama cloud model."""
    return bool(getattr(m, "cloud", False)) or m.provider in PLAN_PROVIDERS


def is_free(m) -> bool:
    """A free model, which the cap has nothing to keep from: every catalog price 0, or OpenCode's
    free one (Model.free: `opencode/*-free`) with no price listed. A listed price that isn't 0 is
    capped whatever the name says (never under)."""
    p = price(m)
    if p is None:
        return bool(getattr(m, "free", False))
    return all(not v for t in p for k, v in t.items() if k != "size")


def price(m) -> list[dict] | None:
    """The model's prices, per million tokens, from the lowest context tier up: [{size, input,
    output, cache_read, cache_write, reasoning}]. None when the catalog gives none (unknown,
    never 0); every field 0 is a true 0, a free model. Takes the catalog's list
    (`[{input, output, cache{read, write}, tier?}]`) and a config entry's `{input, output,
    cache_read, cache_write}`."""
    cost = (getattr(m, "config", None) or {}).get("cost")
    entries = cost if isinstance(cost, list) else [cost] if isinstance(cost, dict) else []
    out = []
    for e in entries:
        if not isinstance(e, dict) or not (_num(e.get("input")) or _num(e.get("output"))):
            continue
        cache = e.get("cache") if isinstance(e.get("cache"), dict) else {}
        tier = e.get("tier") if isinstance(e.get("tier"), dict) else {}
        inp = float(e.get("input")) if _num(e.get("input")) else 0.0
        outp = float(e.get("output")) if _num(e.get("output")) else 0.0
        read = cache.get("read", e.get("cache_read"))
        write = cache.get("write", e.get("cache_write"))
        out.append({"size": float(tier.get("size") or 0) if _num(tier.get("size")) else 0.0,
                    "input": inp, "output": outp,
                    "cache_read": float(read) if _num(read) else inp,    # unknown: as input
                    "cache_write": float(write) if _num(write) else inp,
                    "reasoning": float(e["reasoning"]) if _num(e.get("reasoning")) else outp})
    return sorted(out, key=lambda p: p["size"]) or None


def _rate(prices: list[dict], tok: dict) -> dict:
    # as OpenCode 2.3.2 picks a context tier: input, cache reads and cache writes together (its
    # bundled source); without the writes, a first turn caching a 150K context priced 5x low
    used = tok["in"] + tok["cache_read"] + tok["cache_write"]
    fit = [p for p in prices if not p["size"] or used > p["size"]]
    return fit[-1] if fit else prices[0]


def metered(prices: list[dict] | None, tok: dict) -> float | None:
    """What these tokens cost at the catalog's prices, as OpenCode reckons it; None unpriced."""
    if not prices:
        return None
    r = _rate(prices, tok)
    return round((tok["in"] * r["input"] + tok["cache_read"] * r["cache_read"]
                  + tok["cache_write"] * r["cache_write"] + tok["out"] * r["output"]
                  + tok["reasoning"] * r["reasoning"]) / 1e6, 10)


def _n(v) -> int:
    return int(v) if _num(v) and v > 0 else 0


def step_tokens(part: dict) -> dict:
    """A `step_finish` part's tokens: `input` without cache reads, output apart from reasoning."""
    tok = part.get("tokens") if isinstance(part.get("tokens"), dict) else {}
    cache = tok.get("cache") if isinstance(tok.get("cache"), dict) else {}
    return {"in": _n(tok.get("input")), "out": _n(tok.get("output")),
            "cache_read": _n(cache.get("read")), "cache_write": _n(cache.get("write")),
            "reasoning": _n(tok.get("reasoning"))}


def usage_tokens(u: dict) -> dict:
    """An upstream `usage` object's tokens, whichever API spoke:
    - Chat Completions: `prompt_tokens` holds the cache reads (`prompt_tokens_details.
      cached_tokens`, DeepSeek's `prompt_cache_hit_tokens`), `completion_tokens` the reasoning
      (`completion_tokens_details.reasoning_tokens`);
    - Responses: `input_tokens` with `input_tokens_details.cached_tokens`, `output_tokens` with
      `output_tokens_details.reasoning_tokens`;
    - Anthropic: `input_tokens` without the cache, `cache_read_input_tokens` and
      `cache_creation_input_tokens` beside it.
    A cache read it doesn't name is priced as input: never under."""
    d = lambda k: u.get(k) if isinstance(u.get(k), dict) else {}  # noqa: E731
    if "prompt_tokens" in u or "completion_tokens" in u:
        pd, cd = d("prompt_tokens_details"), d("completion_tokens_details")
        prompt = _n(u.get("prompt_tokens"))
        read = min(prompt, _n(pd.get("cached_tokens")) or _n(u.get("prompt_cache_hit_tokens")))
        write = min(prompt - read, _n(pd.get("cache_write_tokens")))
        done = _n(u.get("completion_tokens"))
        think = min(done, _n(cd.get("reasoning_tokens")))
        return {"in": prompt - read - write, "cache_read": read, "cache_write": write,
                "out": done - think, "reasoning": think}
    if "input_tokens_details" in u or "output_tokens_details" in u:
        inp, outp = _n(u.get("input_tokens")), _n(u.get("output_tokens"))
        read = min(inp, _n(d("input_tokens_details").get("cached_tokens")))
        think = min(outp, _n(d("output_tokens_details").get("reasoning_tokens")))
        return {"in": inp - read, "cache_read": read, "cache_write": 0, "out": outp - think,
                "reasoning": think}
    return {"in": _n(u.get("input_tokens")), "cache_read": _n(u.get("cache_read_input_tokens")),
            "cache_write": _n(u.get("cache_creation_input_tokens")),
            "out": _n(u.get("output_tokens")), "reasoning": 0}


def _total(tok: dict) -> int:
    return sum(tok.values())


# today(): the ledger read on from where the last read ended (its size and inode tell a new or
# rewritten file), so the runner's 5 s tick never re-reads the whole file
_TODAY: dict = {}
_TODAY_LOCK = threading.Lock()


def today() -> dict:
    """Today's spend over every task: {"usd": float, "unpriced_tok": int, "plan_tok":
    {provider: int}, "checkin": bool}, from today's `step` and `reconcile` rows."""
    path, d = ledger_path(), day()
    with _TODAY_LOCK:
        try:
            st = os.stat(path)
        except OSError:
            return {"usd": 0.0, "unpriced_tok": 0, "plan_tok": {}, "checkin": False}
        c = _TODAY
        if (c.get("path"), c.get("ino"), c.get("day")) != (str(path), st.st_ino, d) \
                or st.st_size < c.get("offset", 0):
            c.clear()
            c.update(path=str(path), ino=st.st_ino, day=d, offset=0, usd=0.0, unpriced=0, plan={},
                     checkin=False)
        if st.st_size > c["offset"]:
            rows, c["offset"] = store.read_jsonl(path, c["offset"])
            for r in rows:
                if not isinstance(r, dict) or r.get("day") != d:
                    continue
                if r.get("kind") == "checkin":
                    c["checkin"] = True
                elif r.get("kind") in ("step", "reconcile"):
                    tok = _total(r["tok"]) if isinstance(r.get("tok"), dict) else 0
                    if _num(r.get("usd")):
                        c["usd"] += r["usd"]
                    elif r.get("plan"):
                        c["plan"][r["plan"]] = c["plan"].get(r["plan"], 0) + tok
                    else:
                        c["unpriced"] += tok
        return {"usd": round(c["usd"], 6), "unpriced_tok": c["unpriced"], "plan_tok": dict(c["plan"]),
                "checkin": c["checkin"]}


def task_total(name: str) -> float:
    """Everything the ledger books for one task, any day."""
    rows, _ = store.read_jsonl(ledger_path())
    return round(sum(r["usd"] for r in rows if isinstance(r, dict) and r.get("task") == name
                     and r.get("kind") in ("step", "reconcile") and _num(r.get("usd"))), 10)


def usd(x: float | None) -> str:
    """$5, $5.03, $0.0041; `unknown` for None."""
    if x is None:
        return "unknown"
    if abs(x) >= 1 and abs(x - round(x)) < 0.005:
        return f"${round(x):.0f}"
    if 0 < abs(x) < 0.01:
        return f"${x:.4f}"
    return f"${x:.2f}"


def _tokens(n: int) -> str:
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}K" if n >= 1e3 else str(n)


def _plan_name(m) -> str:
    try:
        return models.provider_name(m)
    except Exception:
        return m.provider


# What wb start says and refuses (routes.resolve, #1016, calls these on the role's model)

def refusal(m, role: str | None = None) -> str | None:
    """Why an API model can't start now, or None: no price (and not a plan), or today's spend
    at the cap. ``role`` names the line's subject ("trivial"), else the model. No cap at all is
    #1014's to refuse (a role) and the guard's to stop (NO_CAP)."""
    if getattr(m, "kind", "gpu") != "api" or is_plan(m) or is_free(m):
        return None
    who = f"{role} role" if role else m.id
    if price(m) is None:
        return f"{m.id}: {NO_PRICE}"
    if (c := cap()) is not None and (t := today()["usd"]) >= c:
        return (f"{who}: API spend today is {usd(t)} of the {usd(c)} day cap (it resets at local "
                f"midnight). Ask the user: a higher cap (wb route cap <usd>), the role on the GPU "
                f"(wb route {role or '<role>'} gpu), or tomorrow")
    return None


def tail(m) -> str:
    """The start line's spend part: ` · today $0.42 of $5`, or a plan's ` · plan usage (OpenCode
    Go): 1.2M tokens today`; "" for a GPU model."""
    if getattr(m, "kind", "gpu") != "api":
        return ""
    t = today()
    if is_plan(m):
        return f" · plan usage ({_plan_name(m)}): {_tokens(t['plan_tok'].get(m.provider, 0))} tokens today"
    c = cap()
    return f" · today {usd(t['usd'])}" + (f" of {usd(c)}" if c is not None else ", no day cap")


# The runner's meter, one per task in this process

_METERS: dict = {}  # str(task.dir) -> Meter, or None for a task that isn't an API one


def for_task(task, m=None):
    """The task's Meter in this process (made once), or None for a GPU task. ``m``: its model
    when the caller has it (drive), else looked up (models.lookup: a GPU id asks no catalog)."""
    key = str(task.dir)
    if key in _METERS:
        return _METERS[key]
    if m is None and task.spec.get("via", "gpu") != "gpu":  # no `via`: a GPU task, as ever (#1016)
        try:
            m = models.lookup(task.spec.get("model") or "", cached=True)  # hosted.json as it is
        except Exception:
            m = None
    _METERS[key] = Meter(task, m) if m is not None and getattr(m, "kind", "gpu") == "api" else None
    return _METERS[key]


def stop_reason(task) -> str | None:
    """Why the task's next request must not go out, or None (a GPU task, a plan): the keyhole
    refused one for want of room (status `api_stop`, guard `spend`), today's spend is at the
    cap, or there is no cap. drive's `_go_on` asks before every round, run_round's tick within
    one. A free model's is None too: it spends nothing for the cap to keep."""
    mt = for_task(task)
    if mt is None or mt.plan or mt.free:
        return None
    stop = task.status.get("api_stop")
    if isinstance(stop, dict) and stop.get("guard") == "spend" and stop.get("text"):
        return str(stop["text"])
    if (c := cap()) is None:
        return NO_CAP
    if (t := today()["usd"]) >= c:
        return f"{STOP}: {usd(t)} of {usd(c)} today"
    return None


def stop_text(task) -> str:
    """The `stuck` notice's lead for a spend stop."""
    c, t = cap(), today()["usd"]
    if c is None:
        return (f"{task.name} stopped: {NO_CAP}. Its work so far is in the worktree; then wb resume "
                f"{task.name}.")
    return (f"{task.name} stopped: {STOP} ({usd(t)} of {usd(c)} today). Its work so far is in the "
            f"worktree. Ask the user: a higher cap (wb route cap <usd>), the role on the GPU, or "
            f"tomorrow; then wb resume {task.name}.")


class Meter:
    """One API task's meter in its runner: the ledger rows, status `api`, the check-in, and the
    keyhole's hooks (`hooks()`: on_usage and room)."""

    def __init__(self, task, m):
        self.task, self.m = task, m
        self.plan = is_plan(m)
        self.prices = price(m)
        self.free = not self.plan and is_free(m)  # booked, never capped
        self.lock = threading.Lock()
        self.total = task_total(task.name)  # booked for the task, any day, this runner's too
        self.metered_total = 0.0
        self.tok = {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0, "reasoning": 0}
        self.keyhole = False   # the keyhole metered a response: steps then only reconcile
        self.n = 0
        self.round_oc = self.round_kh = self.round_fix = 0.0

    def hooks(self) -> dict:
        """What keyhole.start takes from the meter: `on_usage` and `room`."""
        return {"on_usage": self.on_usage, "room": self.room}

    def begin(self, n: int) -> None:
        """A round starts: its live figures from 0, and an old round's keyhole stop cleared."""
        with self.lock:
            self.n, self.round_oc, self.round_kh, self.round_fix = n, 0.0, 0.0, 0.0
        if self.task.status.get("api_stop"):
            self.task.set_status(api_stop=None)

    def _trusted(self, oc, tok: dict) -> bool:
        """OpenCode's figure, unless it is missing, negative, or 0 for tokens that cost money."""
        return _num(oc) and oc >= 0 and not (oc == 0 and (metered(self.prices, tok) or 0) > 0)

    def _book(self, kind: str, usd_: float | None, source: str, tok: dict | None = None,
              mtr: float | None = None) -> None:
        ts = time.time()
        row = {"ts": round(ts, 3), "day": day(ts), "kind": kind, "task": self.task.name,
               "round": self.n or self.task.status.get("round"), "provider": self.m.provider,
               "model": self.m.id, "usd": None if self.plan or usd_ is None else round(usd_, 10)}
        if kind == "step":
            row["metered"] = None if mtr is None else round(mtr, 10)
        if self.plan:
            row["plan"] = self.m.provider
        row["source"] = source
        if tok is not None:
            row["tok"] = tok
        store.append_jsonl(ledger_path(), row)
        with self.lock:
            if _num(row["usd"]):
                self.total += row["usd"]
            if _num(mtr):
                self.metered_total += mtr
            for k, v in (tok or {}).items():
                self.tok[k] = self.tok.get(k, 0) + v
            api = {"usd": None if self.plan else round(self.total, 6),
                   "metered": round(self.metered_total, 6), "source": source, "tok": dict(self.tok),
                   **({"plan": self.m.provider} if self.plan else {})}
            # written under the lock: the keyhole's thread and the runner's book at once, and a
            # total written after a newer one left status `api` under the ledger (the review)
            self.task.set_status(api=api)
        if _num(row["usd"]) and row["usd"] > 0:
            self._checkin()

    def _checkin(self) -> None:
        """Once a day across every task: today past CHECKIN_SHARE of the cap wakes Claude once."""
        c = cap()
        if not c or today()["usd"] < CHECKIN_SHARE * c or today()["checkin"]:
            return
        path = ledger_path()
        with store.locked(path.with_name(path.name + ".checkin.lock")):
            t = today()
            if t["checkin"]:
                return
            ts = time.time()
            store.append_jsonl(path, {"ts": round(ts, 3), "day": day(ts), "kind": "checkin",
                                      "task": self.task.name, "usd_today": round(t["usd"], 6), "cap": c})
        self.task.notify("checkin", f"API spend today has passed half the day cap: {usd(t['usd'])} of "
                                    f"{usd(c)} ({self.task.name} {usd(self.total)} so far). Nothing to do "
                                    "unless the user wants to change it.", round=self.n or None)

    def on_usage(self, usage: dict) -> None:
        """The keyhole's hook (its thread): one upstream response's usage, booked as a step."""
        if not isinstance(usage, dict):
            return
        tok = usage_tokens(usage)
        mtr = metered(self.prices, tok)
        own = usage.get("cost")  # the provider's own figure (OpenRouter's usage.cost)
        if _num(own) and own >= 0 and not (own == 0 and (mtr or 0) > 0):
            value, source = float(own), "provider"
        else:
            value, source = mtr, "keyhole"
        with self.lock:
            self.keyhole = True
            self.round_kh += value or 0
        self._book("step", value, source, tok, mtr)

    def step(self, part: dict) -> None:
        """A `step_finish` (the runner's thread): its own row, or OpenCode's figure reconciling
        what the keyhole booked."""
        if not isinstance(part, dict):
            return
        tok = step_tokens(part)
        oc = part.get("cost")
        mtr = metered(self.prices, tok)
        trusted = self._trusted(oc, tok)
        if self.keyhole:
            if self.plan or not trusted:
                return
            with self.lock:
                self.round_oc += oc
                extra = self.round_oc - self.round_kh - self.round_fix
                if extra <= EPS:
                    return
                self.round_fix += extra
            return self._book("reconcile", extra, "opencode")
        if self.plan or trusted:
            return self._book("step", None if self.plan else float(oc), "opencode", tok, mtr)
        self._book("step", mtr, "metered (opencode said 0)" if _num(oc) and oc == 0 and mtr
                   else "metered", tok, mtr)

    def end_round(self, sessions: dict) -> None:
        """At a round's end: OpenCode's figure for the whole task, the `info.cost` of each of its
        sessions (``sessions``: id -> export), which counts the title requests no step carries.
        More than the ledger has: one `reconcile` row with the difference. Never less (the
        keyhole's view is what went out), never from a 0 for a priced model, never from a
        session that didn't export."""
        if self.plan or not sessions:
            return
        costs = [(e.get("info") or {}).get("cost") if isinstance(e, dict) else None
                 for e in sessions.values()]
        if not all(_num(c) and c >= 0 for c in costs):
            return
        said = sum(costs)
        with self.lock:
            extra = said - self.total
        if said == 0 or extra <= EPS:
            return
        self._book("reconcile", extra, "opencode")

    def room(self, body: dict, size: int) -> float | None:
        """The keyhole's check before each request: the cap less today less this request at its
        worst (its body's length / 4 at the input price, its max output at the output price).
        Negative: refused, and status `api_stop` (guard `spend`) stops the round on the next
        tick. None: nothing to keep (a plan, a free model, an unpriced one, which can't start); no
        cap at all is no room."""
        c = cap()
        if self.plan or self.free or not self.prices:
            return None
        if c is None:
            if not self.task.status.get("api_stop"):
                self.task.set_status(api_stop={"guard": "spend", "text": NO_CAP})
            return -1.0
        top = self.prices[-1]  # the dearest tier: the worst case
        out = next((body.get(k) for k in ("max_tokens", "max_completion_tokens", "max_output_tokens")
                    if _num(body.get(k))), models.API_OUTPUT) if isinstance(body, dict) else models.API_OUTPUT
        worst = (size / 4 * top["input"] + out * max(top["output"], top["reasoning"])) / 1e6
        t = today()["usd"]
        left = c - t - worst
        if left < 0 and not self.task.status.get("api_stop"):
            self.task.set_status(api_stop={"guard": "spend", "text": (
                f"{STOP}: {usd(t)} of {usd(c)} today, and its next request could cost up to "
                f"{usd(worst)}")})
        return left
