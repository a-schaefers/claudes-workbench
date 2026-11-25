"""The API meter (#1019): what an API task's model costs, a ledger of it per local day, and the
one day cap over every API provider together (routes.json `day_usd`, the user's).

Prices are the catalog's (`Model.config["cost"]`, per million tokens), priced the way OpenCode
prices itself (the spike for #1009): `input` leaves out cache reads, cache reads and writes at
their own rates, reasoning at the output rate (the reasoning rate when one is listed), and a
`tier: {type: "context", size}` price past its size. OpenCode's own figure is that same
estimate (unless the response's usage says its `cost`: then that, #1234), so wb meters the same
way rather than at the cache-miss rate, which over-counted a cached context about 50 times.
Where the split isn't known it never prices under: a usage that doesn't say its cache reads
prices them as input, a missing cache price is the input's.

Two views of the same requests, each kept as high as the other says (never under):
- the keyhole's (`Meter.on_usage`, its thread): each upstream response's `usage`, every request
  the task's model got (titles and compaction too, which no `step_finish` carries). A
  provider's own figure (OpenRouter's `usage.cost`, DeepInfra's `usage.estimated_cost`) wins
  over the catalog; a response cut short (wb stopping OpenCode mid-stream) is the keyhole's
  estimate, marked `cut` (#1276);
- OpenCode's: live, each `step_finish.part.cost` (`Meter.step`); at each round's end the
  sessions' exported `info.cost` (`Meter.end_round`). With the keyhole metering, these only add
  a `reconcile` row when OpenCode says more than the keyhole saw, each at the same prices (a
  provider's `estimated_cost` compared at the catalog's, `at_catalog`: never topped up to it; a
  `usage.cost` as booked, which OpenCode reports as its own figure too, #1234); without
  it (no usage in the answers, the suite's fake), each step books its own row, OpenCode's cost or
  the metered one.
An OpenCode cost of 0 for a model whose price isn't 0 is never trusted (never a false 0).

The ledger is `$WB_HOME/spend-api.jsonl` (store.append_jsonl, locked): `step` and `reconcile`
rows, each with its local `day` (a `checkin` row from before #1322 is ignored); a row at the
user's own prices (wb model price, models.json `cost`) says `prices: "yours"`.

Every figure is an estimate unless the provider says its own or the user set the model's prices:
the catalog's can be about twice a provider's bill (DeepInfra's, #1135, 2026-11-23). `basis`
says which a figure is: "catalog" (shown `≈$0.42`), "yours" or "provider" (the provider's own
`usage.cost` or `usage.estimated_cost`), both plain. A plan (OpenCode Go, GitHub Copilot, an Ollama
cloud model) is a quota, not dollars: its rows have `usd: null` and `plan`, and never count against
the cap."""

from __future__ import annotations

import math
import os
import threading
import time
from pathlib import Path

from . import keyhole, models, routes, store

# a plan's quota, not dollars (and m.cloud: Ollama cloud). GitHub Copilot's subscription counts
# premium requests: its catalog lists the models' API prices all the same (OpenCode 2.3.2's bundled
# catalog: $2/$10 for its Claude and GPT models), which would book dollars the user never pays
# against the day cap (#1191)
PLAN_PROVIDERS = ("opencode-go", "github-copilot")
EPS = 1e-9               # less than this between two figures is the same figure
# status `api`'s figures are written to 6 places (Meter._book), an export's aren't: closer than
# this to one is its rounding. Compared within EPS, a DeepInfra export a few 1e-7 past a rounded-down
# `at_catalog` closed 4 of 10 real tasks of its own rows as `catalog prices` (#1220, 2026-11-24)
STATUS_EPS = 1e-6
STOP = keyhole.SPEND_REFUSED   # the keyhole's answer to OpenCode (a 400, which it doesn't retry) says it
# today under the cap, but not by the next request at its worst, which the keyhole's room refuses:
# "the cap is reached" there had Claude resume into the same stop, a wake for nothing (#1150)
NEAR = "too near the day's API spend cap for its next request"
NO_PRICE = "OpenCode's catalog gives no price for it, so the day cap can't be kept"
# routes.json without day_usd: #1014 refuses an API role without one, so only a hand-edited file
# gets here, and an API task then spends nothing rather than without a limit
NO_CAP = "there is no API day cap (routes.json day_usd): ask the user for one (wb route cap <usd>)"
# routes.json that can't be read (a hand edit, a value routes.normalize refuses): no cap is known,
# so the task stops as for NO_CAP, but saying so: NO_CAP's advice there brought up the dialog
# "every role starts over on the GPU" for a cap that was set (wave 2's review)
UNREADABLE = "routes.json can't be read ({why}), so the day cap can't be kept: wb route shows it"


CATALOG, YOURS, PROVIDER = "catalog", "yours", "provider"  # a figure's basis (#1135)
BASES = (CATALOG, YOURS, PROVIDER)


def row_basis(r: dict) -> str:
    """A ledger row's basis: the provider's own figure, the user's prices, else the catalog's
    (every row from before #1135)."""
    return PROVIDER if r.get("source") == "provider" else YOURS if r.get("prices") == YOURS else CATALOG


def basis(bases) -> str:
    """Several figures' basis summed: any catalog one makes the sum an estimate; none at all is
    the catalog's (a $0 so far reads as the default)."""
    got = set(bases)
    return CATALOG if CATALOG in got or not got else YOURS if YOURS in got else PROVIDER


def model_basis(m) -> str:
    """What a model's next figures will be: the user's prices (wb model price), else the catalog's."""
    return YOURS if models.own_price(m) else CATALOG


def money(x: float | None, b: str = CATALOG) -> str:
    """usd(), with `≈` in front of a catalog estimate (#1135); $0 is none. The figure's basis is
    said by the `≈` alone: a "(catalog prices)" note after it said it twice (#1322)."""
    return ("≈" if b == CATALOG and x else "") + usd(x)


def ledger_path() -> Path:
    return store.home() / "spend-api.jsonl"


def day(ts: float | None = None) -> str:
    """The local calendar day of ``ts``: the cap resets at local midnight."""
    return time.strftime("%Y-%m-%d", time.localtime(time.time() if ts is None else ts))


def _routes_path() -> Path:
    return routes.path()


_CAP: dict = {}  # routes.json's day_usd by the file's (path, mtime, size): the guard asks per tick


def cap() -> float | None:
    """The day cap in USD (routes.json `day_usd`), or None when the user set none or the file
    can't be read (`no_cap`'s line says which): an API task then spends nothing rather than
    without a limit."""
    path = _routes_path()
    try:
        st = os.stat(path)
        at = (str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        at = (str(path), None, None)
    if _CAP.get("at") != at:
        try:
            v, err = routes.read().get("day_usd"), None
        except routes.RoutesError as e:
            v, err = None, str(e)
        _CAP.update(at=at, cap=float(v) if _num(v) and v >= 0 else None, error=err)
    return _CAP["cap"]


def no_cap() -> str:
    """Why there is no cap: routes.json can't be read (UNREADABLE, with why), else NO_CAP."""
    cap()
    return UNREADABLE.format(why=_CAP["error"]) if _CAP.get("error") else NO_CAP


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v


def is_plan(m) -> bool:
    """A plan's quota, not dollars: OpenCode Go, GitHub Copilot (#1191), an Ollama cloud model."""
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
    return _prices((getattr(m, "config", None) or {}).get("cost"))


def first_tier(cfg: dict) -> int | None:
    """The context size past which a model entry's price first changes (price's lowest `size`
    above 0), or None for a flat price or none: models.api_window lifts no window past it, as
    every request over it is priced at the tier's rate (Haiku's 100K, 5x)."""
    sizes = [p["size"] for p in _prices(cfg.get("cost")) or () if p["size"] > 0]
    return int(min(sizes)) if sizes else None


def _prices(cost) -> list[dict] | None:
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
      `cache_creation_input_tokens` beside it, `output_tokens` with
      `output_tokens_details.thinking_tokens`. Told by its cache fields before the Responses
      API's details: its message_delta now carries `output_tokens_details` too, which read it as
      the Responses API and booked every cache read and write as 0 (#1081, 2026-11-23);
    - Gemini's `usageMetadata` (#1192): `promptTokenCount` holds the cache reads
      (`cachedContentTokenCount`), `toolUsePromptTokenCount` is input beside it, and
      `candidatesTokenCount` leaves out `thoughtsTokenCount` (OpenCode's gemini.ts mapUsage);
    - Bedrock Converse's (#1192): `inputTokens` without the cache, `cacheReadInputTokens` and
      `cacheWriteInputTokens` beside it, `outputTokens` (bedrock-converse.ts mapUsage).
    A cache read it doesn't name is priced as input: never under."""
    d = lambda k: u.get(k) if isinstance(u.get(k), dict) else {}  # noqa: E731
    if "promptTokenCount" in u or "candidatesTokenCount" in u:
        prompt = _n(u.get("promptTokenCount")) + _n(u.get("toolUsePromptTokenCount"))
        read = min(prompt, _n(u.get("cachedContentTokenCount")))
        return {"in": prompt - read, "cache_read": read, "cache_write": 0,
                "out": _n(u.get("candidatesTokenCount")), "reasoning": _n(u.get("thoughtsTokenCount"))}
    if "inputTokens" in u or "outputTokens" in u:
        return {"in": _n(u.get("inputTokens")), "cache_read": _n(u.get("cacheReadInputTokens")),
                "cache_write": _n(u.get("cacheWriteInputTokens")), "out": _n(u.get("outputTokens")),
                "reasoning": 0}
    if "prompt_tokens" in u or "completion_tokens" in u:
        pd, cd = d("prompt_tokens_details"), d("completion_tokens_details")
        prompt = _n(u.get("prompt_tokens"))
        read = min(prompt, _n(pd.get("cached_tokens")) or _n(u.get("prompt_cache_hit_tokens")))
        write = min(prompt - read, _n(pd.get("cache_write_tokens")))
        done = _n(u.get("completion_tokens"))
        think = min(done, _n(cd.get("reasoning_tokens")))
        return {"in": prompt - read - write, "cache_read": read, "cache_write": write,
                "out": done - think, "reasoning": think}
    anthropic = "cache_read_input_tokens" in u or "cache_creation_input_tokens" in u
    if not anthropic and ("input_tokens_details" in u or "output_tokens_details" in u):
        inp, outp = _n(u.get("input_tokens")), _n(u.get("output_tokens"))
        read = min(inp, _n(d("input_tokens_details").get("cached_tokens")))
        think = min(outp, _n(d("output_tokens_details").get("reasoning_tokens")))
        return {"in": inp - read, "cache_read": read, "cache_write": 0, "out": outp - think,
                "reasoning": think}
    outp = _n(u.get("output_tokens"))
    think = min(outp, _n(d("output_tokens_details").get("thinking_tokens")))
    return {"in": _n(u.get("input_tokens")), "cache_read": _n(u.get("cache_read_input_tokens")),
            "cache_write": _n(u.get("cache_creation_input_tokens")),
            "out": outp - think, "reasoning": think}


def max_output(body) -> int | float:
    """A request's output cap, wherever its API puts it: top-level `max_tokens` and the like,
    Gemini's `generationConfig.maxOutputTokens`, Converse's `inferenceConfig.maxTokens` (#1192);
    models.API_OUTPUT (the layer's cap) when it names none."""
    if not isinstance(body, dict):
        return models.API_OUTPUT
    nested = [(body.get(o), k) for o, k in (("generationConfig", "maxOutputTokens"),
                                            ("inferenceConfig", "maxTokens"))]
    for v in [body.get(k) for k in ("max_tokens", "max_completion_tokens", "max_output_tokens")] + [
            o.get(k) for o, k in nested if isinstance(o, dict)]:
        if _num(v):
            return v
    return models.API_OUTPUT


def _total(tok: dict) -> int:
    return sum(tok.values())


# today(): the ledger read on from where the last read ended (its size and inode tell a new or
# rewritten file), so the runner's 5 s tick never re-reads the whole file
_TODAY: dict = {}
_TODAY_LOCK = threading.Lock()


def today() -> dict:
    """Today's spend over every task: {"usd": float, "unpriced_tok": int, "plan_tok":
    {provider: int}, "basis": its rows' (basis), "bases"}, from today's `step` and `reconcile`
    rows."""
    path, d = ledger_path(), day()
    with _TODAY_LOCK:
        try:
            st = os.stat(path)
        except OSError:
            return {"usd": 0.0, "unpriced_tok": 0, "plan_tok": {}, "basis": CATALOG, "bases": []}
        c = _TODAY
        if (c.get("path"), c.get("ino"), c.get("day")) != (str(path), st.st_ino, d) \
                or st.st_size < c.get("offset", 0):
            c.clear()
            c.update(path=str(path), ino=st.st_ino, day=d, offset=0, usd=0.0, unpriced=0, plan={},
                     bases=set())
        if st.st_size > c["offset"]:
            rows, c["offset"] = store.read_jsonl(path, c["offset"])
            for r in rows:
                if not isinstance(r, dict) or r.get("day") != d:
                    continue
                if r.get("kind") in ("step", "reconcile"):
                    tok = _total(r["tok"]) if isinstance(r.get("tok"), dict) else 0
                    if _num(r.get("usd")):
                        c["usd"] += r["usd"]
                        c["bases"].add(row_basis(r))
                    elif r.get("plan"):
                        c["plan"][r["plan"]] = c["plan"].get(r["plan"], 0) + tok
                    else:
                        c["unpriced"] += tok
        return {"usd": round(c["usd"], 6), "unpriced_tok": c["unpriced"], "plan_tok": dict(c["plan"]),
                "basis": basis(c["bases"]), "bases": sorted(c["bases"])}


def _task_rows(name: str) -> list[dict]:
    rows, _ = store.read_jsonl(ledger_path())
    return [r for r in rows if isinstance(r, dict) and r.get("task") == name
            and r.get("kind") in ("step", "reconcile") and _num(r.get("usd"))]


def _at_catalog(usd_: float, source, mtr, own=None) -> float:
    """A booked figure as OpenCode's own figure prices it, to compare the two: a provider's own
    (`source: provider`) at its metered one, the catalog's (or the user's, which the task's
    catalog.json gives OpenCode too); any other as booked (#1165, 2026-11-24). A provider's
    `usage.cost` (``own`` "cost") as booked too: OpenCode 2.3.2 reports a usage's `cost` as its
    own step and export figure; `estimated_cost` (DeepInfra's) it never reads. At the
    catalog's, every OpenRouter response billed past its listed price booked the excess a second
    time as a `reconcile` row (#1234, 2026-11-24). A row from before `own` is read as before."""
    return float(mtr) if source == PROVIDER and own != "cost" and _num(mtr) else usd_


def task_total(name: str) -> float:
    """Everything the ledger books for one task, any day."""
    return round(sum(r["usd"] for r in _task_rows(name)), 10)


def usd(x: float | None) -> str:
    """Every dollar figure wb prints: $5.00, $0.42; `<$0.01` under a cent, `$0` for none, and
    `unknown` for None. One rule here, the pane's and the chat's `usd` too: meter's "$5" and
    savings' "$5.00" said one cap two ways (#1322)."""
    if x is None:
        return "unknown"
    if x == 0:
        return "$0"
    if 0 < abs(x) < 0.01:
        return "<$0.01"
    return f"${x:.2f}"


def _tokens(n: int) -> str:
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}K" if n >= 1e3 else str(n)


def _plan_name(m) -> str:
    try:
        return models.provider_name(m)
    except Exception:
        return m.provider


# What wb start says and refuses (routes.resolve, #1016, calls these on the role's model)

# A fresh session's first request at the least, in tokens as the room counts a body (its bytes
# / 4): OpenCode's system prompt and tools, the handbook, the protocol, the user's instructions.
# A real task's first step read 16,161 input tokens, about 65 KB of body (#1173, 2026-11-24).
FIRST_TOKENS = 16_384

# An image in a request (#1199's review): OpenCode sends each `run -f` image, and the read tool's,
# as base64, a data: URI (OpenAI's image_url, an OpenAI-compatible server's) or a `data` field
# beside its image media type (Anthropic's source, Google's inlineData). At its bytes / 4 one
# 10 MiB image read as 3.5M tokens, $10.49 at $3/M input: past a $10 cap every request carrying
# it was refused, and wb start, counting no image, had let the task start (#1118's failure
# again). A provider counts an image by its pixels after its own downscale, a few thousand
# tokens (Anthropic's about 1,600, OpenAI's tiles about 1,100, a Qwen-VL's up to 16,384): the
# room and runner.first_tokens count each at that worst, its base64 left out.
IMAGE_TOKENS = 16_384
MEDIA_KEYS = ("media_type", "mimeType", "mime_type", "mediaType")


def images_in(body) -> tuple[int, int]:
    """A request body's images: how many, and the characters of their base64 (each a data: URI,
    or a `data` string beside an image/ media type), which the room leaves out of its bytes / 4."""
    n = chars = 0
    todo = [body]
    while todo:
        x = todo.pop()
        if isinstance(x, dict):
            data = x.get("data")
            if isinstance(data, str) and any(isinstance(x.get(k), str) and x[k].startswith("image/")
                                             for k in MEDIA_KEYS):
                n, chars = n + 1, chars + len(data)
            else:
                todo.extend(x.values())
        elif isinstance(x, list):
            todo.extend(x)
        elif isinstance(x, str) and x.startswith("data:image/") and ";base64," in x[:128]:
            n, chars = n + 1, chars + len(x)
    return n, chars


def body_tokens(body, size: int) -> float:
    """A request's input as the room reckons it: its bytes / 4, each image at IMAGE_TOKENS in
    place of its base64 (images_in)."""
    n, chars = images_in(body) if isinstance(body, (dict, list)) else (0, 0)
    return max(size - chars, 0) / 4 + n * IMAGE_TOKENS


def request_floor(m, tokens: int = FIRST_TOKENS) -> float | None:
    """The next request at its worst, as the keyhole's room reckons it: its input ``tokens``
    (the room's body / 4: runner.first_tokens, else a fresh session's FIRST_TOKENS) and the
    model's pinned output (options.max_tokens, else limit.output, else models.API_OUTPUT), which
    OpenCode asks for in full while the window has room, at the dearest tier's prices. None
    unpriced. #1150 left the input out, to keep this a floor: then a day within the input's cost
    of the cap passed wb start, feedback, next, resume and drive's look ahead, the round's first
    request was refused at once, and the cap the refusal named ("a cap of at least") was refused
    again at that request, a second wake and a second dialog (#1173, 2026-11-24)."""
    if not (p := price(m)):
        return None
    cfg = getattr(m, "config", None) or {}
    opts = cfg.get("options") if isinstance(cfg.get("options"), dict) else {}
    lim = cfg.get("limit") if isinstance(cfg.get("limit"), dict) else {}
    out = _n(opts.get("max_tokens")) or _n(lim.get("output")) or models.API_OUTPUT
    top = p[-1]
    return (_n(tokens) * top["input"] + out * max(top["output"], top["reasoning"])) / 1e6


def _enough(t: float, worst: float) -> float:
    """The least cap that lets today's spend ``t`` make one request of ``worst``: up to the cent."""
    return math.ceil(round((t + worst) * 100, 6)) / 100


def _ask(role: str | None, more: str, task=None, asked: bool = False) -> str:
    """refusal's "Ask the user: …", ``more`` the cap that would do. For a task already running
    (``task``: wb feedback, next or resume) the role on the GPU is for the next tasks only: the
    task keeps its API model, so "the role on the GPU" there had the user switch it and the retry
    refused again with the same line (#1171, 2026-11-24, #1095's mistake in stop_text again). The
    way off API is routes.off_api's: `wb route <role> gpu` under Agents API changed nothing (#1218).
    A `--model` start (``asked``) runs on the model it names whatever the roles say, so its way off
    API is a GPU model, as the local-only refusal's: `wb route <role> gpu` there changed nothing
    for it, and the retry was refused with the same line (#1233, 2026-11-24)."""
    if asked and task is None:
        return f". Ask the user: {more} (wb route cap <usd>), a GPU model, or tomorrow"
    off = routes.off_api(role)
    if task is None:
        return f". Ask the user: {more} (wb route cap <usd>)" + (f", {off[0]} ({off[2]})," if off else "") + " or tomorrow"
    n = task.name
    return (f". Ask the user: {more} (wb route cap <usd>) or tomorrow, then try again; or "
            + (f"{off[0]} ({off[2]}) for the next tasks, with wb takeover {n} or wb discard {n} for this one"
               if off else f"wb takeover {n} or wb discard {n}") + " (it keeps its API model)")


def refusal(m, role: str | None = None, advice: bool = True, tokens: int = FIRST_TOKENS,
            task=None, asked: bool = False) -> str | None:
    """Why an API model can't start now, or None: no price (and not a plan), or today's spend
    at the cap, or so near it that one request at its worst (request_floor, its input
    ``tokens``: runner.first_tokens, #1173) would pass it: the room refused that request at
    once, after the checkout, setup and base check (#1150).
    ``role`` names the line's subject ("trivial"), else the model. ``advice``: the cap line's
    "Ask the user: …"; the busy opt-in's note goes without, its role being on the GPU already
    and its start only waiting for it (wave 2's merge); ``task``, a task already running, whose
    advice keeps the GPU for the next tasks (`_ask`, #1171); ``asked``, a `--model` start, whose
    advice is a GPU model (#1233). No cap at all (NO_CAP, or
    UNREADABLE) is refused too: a `--model` start without one passed the dialog and wb start,
    then the guard stopped it in round 1, after the checkout, setup and base check (#1089,
    2026-11-23)."""
    if getattr(m, "kind", "gpu") != "api" or is_plan(m) or is_free(m):
        return None
    who = f"{role} role" if role else m.id
    if price(m) is None:
        return f"{m.id}: {NO_PRICE}"
    if cap() is None:
        return f"{who}: {no_cap()}"
    if (c := cap()) is not None and (t := today()["usd"]) >= c:
        return (f"{who}: API spend today is {money(t, today()['basis'])} of the {usd(c)} day cap (it resets at local "
                f"midnight)" + (_ask(role, "a higher cap", task, asked) if advice else ""))
    if (w := request_floor(m, tokens)) and t + w > c:
        return (f"{who}: API spend today is {money(t, today()['basis'])} of the {usd(c)} day cap, and one "
                f"request on it can cost up to {usd(w)} (it resets at local midnight)"
                + (_ask(role, f"a cap of at least {usd(_enough(t, w))}", task, asked) if advice else ""))
    return None


def tail(m) -> str:
    """The start line's spend part: ` · today ≈$0.42 of $5.00` (plain `$0.42` when today's rows
    are all the provider's own or at the user's prices, #1135, #1165), or a plan's ` · plan usage
    (OpenCode Go): 1.2M tokens today`; "" for a GPU model."""
    if getattr(m, "kind", "gpu") != "api":
        return ""
    t = today()
    if is_plan(m):
        return f" · plan usage ({_plan_name(m)}): {_tokens(t['plan_tok'].get(m.provider, 0))} tokens today"
    c = cap()
    return f" · today {money(t['usd'], t['basis'])}" + (f" of {usd(c)}" if c is not None else ", no day cap")


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


def stop_reason(task, ahead: bool = False, tokens: int = FIRST_TOKENS) -> str | None:
    """Why the task's next request must not go out, or None (a GPU task, a plan): the keyhole
    refused one for want of room (status `api_stop`, guard `spend`), today's spend is at the
    cap, or there is no cap. drive's `_go_on` asks before every round, with ``ahead``: today
    within one request at its worst (request_floor, its input ``tokens``: runner.first_tokens,
    #1173) of the cap stops it too, as the round's first request would be refused (#1150).
    run_round's tick asks within one, without: a request the room let out is never cut for the
    next one's sake. A free model's is None too: it spends
    nothing for the cap to keep."""
    mt = for_task(task)
    if mt is None or mt.plan or mt.free:
        return None
    stop = task.status.get("api_stop")
    if isinstance(stop, dict) and stop.get("guard") == "spend" and stop.get("text"):
        return str(stop["text"])
    if not mt.prices:  # however it started (refusal is wb start's): no cap can be kept for it
        return f"{mt.m.id}: {NO_PRICE}"
    if (c := cap()) is None:
        return no_cap()
    if (t := today()["usd"]) >= c:
        return f"{STOP}: {money(t, today()['basis'])} of {usd(c)} today"
    if ahead and (w := request_floor(mt.m, tokens)) and t + w > c:
        return f"{NEAR}: {money(t, today()['basis'])} of {usd(c)} today, and it could cost up to {usd(w)}"
    return None


def stop_text(task, worst: float | None = None, tokens: int = FIRST_TOKENS) -> str:
    """The `stuck` notice's lead for a spend stop; ``worst``: the room's refused request at its
    worst, which the round's stop carries (`_spend_lead`: `api_stop` is consumed by then), else
    request_floor's with the next request's input ``tokens`` (`_go_on`'s, #1173)."""
    c, t, b = cap(), today()["usd"], today()["basis"]
    if c is None:
        return (f"{task.name} stopped: {no_cap()}. Its work so far is in the worktree; then wb resume "
                f"{task.name}.")
    # "the role on the GPU" went with "then wb resume", but resume keeps the task's API model
    # (fixed per task): the resumed round stopped at the cap again at once (#1095, 2026-11-23).
    # A route change is for the next tasks; this one is taken over or discarded.
    # The way off API is routes.off_api's, from routes.json now: `wb route <role> gpu` under Agents
    # API, or for a task the busy opt-in sent, changed nothing (#1218, 2026-11-24).
    n, off = task.name, routes.off_api(task.spec.get("role"))
    tail = (f"; or {off[0]} ({off[2]}) for the next tasks, with wb takeover {n} or wb discard {n} for this one"
            if off else f"; or wb takeover {n} or wb discard {n}") + " (resume keeps its API model)."
    if w := _near(task, c, t, worst, tokens):  # under the cap: "the cap is reached" had Claude resume into it (#1150)
        return (f"{n} stopped: {NEAR} (up to {usd(w)}; {money(t, b)} of {usd(c)} today). Its work "
                f"so far is in the worktree. Ask the user: a cap of at least {usd(_enough(t, w))} (wb route "
                f"cap <usd>) or tomorrow, then wb resume {n}" + tail)
    return (f"{n} stopped: {STOP} ({money(t, b)} of {usd(c)} today). Its work so far is in the "
            f"worktree. Ask the user: a higher cap (wb route cap <usd>) or tomorrow, then wb resume "
            f"{n}" + tail)


def _near(task, c: float, t: float, worst: float | None = None,
          tokens: int = FIRST_TOKENS) -> float | None:
    """The next request's worst when today ``t`` is under the cap ``c`` but not by that much: the
    room's own figure (``worst``, else status `api_stop` `worst`), else request_floor's for
    ``tokens`` of input; None otherwise."""
    if t >= c or (mt := for_task(task)) is None or not mt.prices:
        return None
    # The round's stop consumes `api_stop` (`_ProviderWait.stop`) before the notice is written:
    # read from status alone, the room's worst was never seen, request_floor (no input) fell
    # short of the cap, and the notice said "the cap is reached" with a cap too low (#1150,
    # 2026-11-24). The round carries it instead.
    stop = task.status.get("api_stop")
    if not _num(worst):
        worst = stop.get("worst") if isinstance(stop, dict) and _num(stop.get("worst")) else None
    w = worst if worst is not None else request_floor(mt.m, tokens)
    return w if w and t + w > c else None


class Meter:
    """One API task's meter in its runner: the ledger rows, status `api`, and the keyhole's hooks
    (`hooks()`: on_usage and room)."""

    def __init__(self, task, m):
        self.task, self.m = task, m
        self.plan = is_plan(m)
        self.prices = price(m)
        self.free = not self.plan and is_free(m)  # booked, never capped
        self.yours = models.own_price(m)  # wb model price's: its rows say so (#1135)
        self.lock = threading.Lock()
        rows = _task_rows(task.name)  # booked for the task, any day, this runner's too
        self.total = round(sum(r["usd"] for r in rows), 10)
        # the ledger as OpenCode's figure prices it: a provider's own row at its catalog figure
        # (`metered`), unless it was usage.cost, which OpenCode reports too (#1165, #1234)
        self.at_catalog = round(sum(_at_catalog(r["usd"], r.get("source"), r.get("metered"),
                                                r.get("own")) for r in rows), 10)
        self.bases = {row_basis(r) for r in rows}
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
              mtr: float | None = None, own: str | None = None, cut: bool = False) -> None:
        ts = time.time()
        row = {"ts": round(ts, 3), "day": day(ts), "kind": kind, "task": self.task.name,
               "round": self.n or self.task.status.get("round"), "provider": self.m.provider,
               "model": self.m.id, "usd": None if self.plan or usd_ is None else round(usd_, 10)}
        if kind == "step":
            row["metered"] = None if mtr is None else round(mtr, 10)
        if self.plan:
            row["plan"] = self.m.provider
        row["source"] = source
        if own:
            row["own"] = own   # the provider's field: `cost` OpenCode reports too (#1234)
        if cut:
            row["cut"] = True  # a response cut short: the keyhole's estimate (#1276)
        if self.yours and not self.plan:
            row["prices"] = YOURS
        if tok is not None:
            row["tok"] = tok
        store.append_jsonl(ledger_path(), row)
        with self.lock:
            if _num(row["usd"]):
                self.total += row["usd"]
                self.at_catalog += _at_catalog(row["usd"], source, mtr, own)
                self.bases.add(row_basis(row))
            if _num(mtr):
                self.metered_total += mtr
            for k, v in (tok or {}).items():
                self.tok[k] = self.tok.get(k, 0) + v
            api = {"usd": None if self.plan else round(self.total, 6),
                   "metered": round(self.metered_total, 6), "source": source, "tok": dict(self.tok),
                   # the total as OpenCode's estimate prices it, which results.api_usage compares
                   # its export with: against `usd`, a provider's own rows came back at the
                   # catalog's in `paid` (#1165's review, 2026-11-24)
                   **({} if self.plan else {"at_catalog": round(self.at_catalog, 6)}),
                   "prices": basis(self.bases or {model_basis(self.m)}),
                   **({"plan": self.m.provider} if self.plan else {})}
            # written under the lock: the keyhole's thread and the runner's book at once, and a
            # total written after a newer one left status `api` under the ledger (the review)
            self.task.set_status(api=api)
    def on_usage(self, usage: dict) -> None:
        """The keyhole's hook (its thread): one upstream response's usage, booked as a step."""
        if not isinstance(usage, dict):
            return
        tok = usage_tokens(usage)
        mtr = metered(self.prices, tok)
        # the provider's own figure: OpenRouter's usage.cost, DeepInfra's usage.estimated_cost (in
        # its last, `choices: []` chunk, at its own rate: dropped, every DeepInfra figure and the
        # day cap ran at the catalog's, about 2x the bill, #1165, 2026-11-24)
        field = "cost" if _num(usage.get("cost")) else "estimated_cost"
        own = usage.get(field)
        cut = usage.get("cut") is True  # keyhole._cut_usage's estimate: a running cost is partial
        if not cut and _num(own) and own >= 0 and not (own == 0 and (mtr or 0) > 0):
            value, source = float(own), PROVIDER
        else:
            value, source, field = mtr, "keyhole", None
        with self.lock:
            self.keyhole = True
            # what the keyhole saw at the prices OpenCode's step figure uses (_at_catalog)
            self.round_kh += _at_catalog(value or 0, source, mtr, field)
        self._book("step", value, source, tok, mtr, field, cut)

    def step(self, part: dict) -> None:
        """A `step_finish` (the runner's thread): its own row, or OpenCode's figure reconciling
        what the keyhole saw. Compared at the same prices (round_kh: a provider's own figure at
        its catalog one, but a usage.cost as booked, which OpenCode's figure is too, #1234):
        against what was booked, OpenCode's catalog estimate topped every DeepInfra row back up
        to the catalog's (#1165, 2026-11-24)."""
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
        More than the ledger has, a provider's own rows at their catalog figure (at_catalog, as in
        step: #1165; a usage.cost row as booked, #1234): one `reconcile` row with the difference.
        Never less (the keyhole's view is what went out), never from a 0 for a priced model, never
        from a session that didn't export."""
        if self.plan or not sessions:
            return
        costs = [(e.get("info") or {}).get("cost") if isinstance(e, dict) else None
                 for e in sessions.values()]
        if not all(_num(c) and c >= 0 for c in costs):
            return
        said = sum(costs)
        with self.lock:
            extra = said - self.at_catalog
        if said == 0 or extra <= EPS:
            return
        self._book("reconcile", extra, "opencode")

    def room(self, body: dict, size: int) -> float | None:
        """The keyhole's check before each request: the cap less today less this request at its
        worst (its body's length / 4 at the input price, an image at IMAGE_TOKENS: body_tokens;
        its max output at the output price).
        Negative: refused, and status `api_stop` (guard `spend`) stops the round on the next
        tick. None: nothing to keep (a plan, a free model). No cap at all is no room, and so is an
        unpriced model: wb start refuses one (refusal), and whichever path started it anyway, it
        ran with no cap at all once #1016's path skipped that check (wave 2's review)."""
        c = cap()
        if self.plan or self.free:
            return None
        if not self.prices:
            if not self.task.status.get("api_stop"):
                self.task.set_status(api_stop={"guard": "spend", "text": f"{self.m.id}: {NO_PRICE}"})
            return -1.0
        if c is None:
            if not self.task.status.get("api_stop"):
                self.task.set_status(api_stop={"guard": "spend", "text": no_cap()})
            return -1.0
        top = self.prices[-1]  # the dearest tier: the worst case
        out = max_output(body)
        worst = (body_tokens(body, size) * top["input"] + out * max(top["output"], top["reasoning"])) / 1e6
        t = today()["usd"]
        left = c - t - worst
        if left < 0 and not self.task.status.get("api_stop"):
            # NEAR while today is under the cap (#1150); `worst` for stop_text's "at least" cap
            self.task.set_status(api_stop={"guard": "spend", "worst": round(worst, 10), "text": (
                f"{STOP if t >= c else NEAR}: {money(t, today()['basis'])} of {usd(c)} today, and its next "
                f"request could cost up to {usd(worst)}")})
        return left
