"""What a task saved Claude, against Claude writing it itself: an estimate, its figures marked `~`
(the words "(estimate)" after a `~` said it twice, #1322).

The estimate comes from eight paired runs on 2026-11-17: Claude made roughly 4.9 +
lines/69 calls, each costing the session's context cost per call, and wrote about 36
output tokens per line changed. Mean error 19%, single tasks -36% to +48%.

Checked on 2026-11-19 against five new runs (`wb bench self` on the bench cases, #120): mean
error 19% again, -32% to +21%, low on four, so it understates the saving. A refit on those five
did better on them (15%) but worse on each one left out (32%), so the constants stand."""

from __future__ import annotations

import json
import os
from pathlib import Path

from . import meter, spend, store
from .meter import money, usd

# The eight-paired-run fit (2026-11-17), see the module docstring.
SELF_CALLS = 4.9
SELF_LINES_PER_CALL = 69
SELF_OUT_PER_LINE = 36


# What the estimate prices, said wherever it is shared (#725)
COUNTS = ("the estimate prices the lines Claude would type: not lockfiles or generated files, and "
          "a file deleted whole as one call")


def per_call(u: dict) -> float | None:
    """One call's context cost, from a task's usage; None without usage or its calls."""
    if not u or not u.get("calls"):
        return None
    return (spend.weighted(u) - 5 * u["output"]) / u["calls"]


def estimate_self(lines: int, per_call: float) -> int:
    """What the change would cost Claude writing it: its calls at this session's per-call cost
    plus its own output, rounded to the token."""
    return round((SELF_CALLS + lines / SELF_LINES_PER_CALL) * per_call
                 + 5 * SELF_OUT_PER_LINE * lines)


def of_row(row: dict) -> dict | None:
    """The saved estimate for one results.jsonl row; None without both sides to compare.

    ``claude`` is the task's share of its session, weighted; ``self_est`` is what
    Claude would spend on the same change; ``saved`` is their difference (positive:
    the agent saved that much); ``wh`` is the task's GPU energy when measured.
    A discarded task has no change to compare; it saved nothing on Claude's side
    but its GPU energy was still spent, so it records a negative saving.
    An API task's row is None: the score answers QUESTION, the GPU's, and an API task's paid
    tokens are on the other side, which it can't see ("saved 60%" for a task that cost $3,
    #1022); api_totals and api_summary say it apart."""
    if row.get("kind") == "api":
        return None
    return _compare(row)


def _compare(row: dict) -> dict | None:
    """of_row's comparison, whatever the row's kind (api_totals sets Claude's two sides of an API
    task side by side, never against its dollars)."""
    if row.get("outcome") == "discarded":
        u = row.get("claude")
        if not isinstance(u, dict):
            return None
        c = spend.weighted(u)
        return {"claude": c, "self_est": 0, "saved": -c, "wh": _wh(row)}
    if "claude" not in row or "diff" not in row:
        return None
    pc = per_call(row["claude"])
    if pc is None:
        return None
    d = row["diff"]
    # the lines of files deleted whole aren't typed: one call removes them (#725: deleting a
    # 2,400-line module once read 'saved ~725K'). A tool's files, lockfiles and the generated,
    # are left out of added and removed already (gitops.footprint).
    gone = d.get("deleted") if isinstance(d.get("deleted"), dict) else {}
    lines = d.get("added", 0) + d.get("removed", 0) - gone.get("removed", 0)
    self_est = estimate_self(max(0, lines), pc) + (round(pc) if gone.get("files") else 0)
    claude = spend.weighted(row["claude"])
    return {"claude": claude, "self_est": self_est, "saved": self_est - claude, "wh": _wh(row)}


def _wh(row: dict) -> float | None:
    """The task's GPU energy; None when not measured (another machine's GPU, which nothing here
    reads: nothing is shown for it, #117, #1322)."""
    return None if row.get("remote_gpu") else row.get("gpu_wh")


def _path() -> Path:
    v = os.environ.get("WB_SAVINGS")
    if v:
        return Path(os.path.expanduser(v))
    return Path.home() / ".config/workbench/savings.json"


def price() -> float | None:
    """The kWh price from $WB_SAVINGS (else ~/.config/workbench/savings.json);
    None when the file is missing, unreadable or not a positive number."""
    try:
        data = json.loads(_path().read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    p = data.get("kwh_price")
    if not isinstance(p, (int, float)) or isinstance(p, bool) or p <= 0:
        return None
    return p


def set_price(kwh_price: float | None) -> None:
    """Set "kwh_price" in the file price() reads (creating its directory), its other keys kept:
    under its lock and through a link, keeping its mode (#495). None removes it: a price once
    set could only be cleared by editing the file (#609)."""
    def read():
        try:
            data = json.loads(_path().read_text())
        except (OSError, ValueError):  # missing or broken: price() reads it as unset
            return {}
        return data if isinstance(data, dict) else {}
    with store.edit_config(_path(), read) as data:
        if kwh_price is None:
            data.pop("kwh_price", None)
        else:
            data["kwh_price"] = kwh_price


def totals(rows: list[dict], since: float = 0) -> dict:
    """The saved totals over the rows measured after ``since``.

    ``tasks`` is how many of them have both sides; ``claude``/``self_est``/``saved``
    are the token sums (weighted); ``wh`` is their GPU energy in watt-hours; ``cost``
    is what that energy is worth at the set price, rounded to a cent."""
    parts = [p for r in rows if r.get("ts", 0) >= since and (p := of_row(r)) is not None]
    wh = round(sum(p["wh"] for p in parts if p["wh"] is not None), 1)
    cost = round(wh / 1000 * price(), 2) if parts and price() is not None else None
    return {"tasks": len(parts),
            "claude": sum(p["claude"] for p in parts),
            "self_est": sum(p["self_est"] for p in parts),
            "saved": sum(p["saved"] for p in parts),
            "wh": wh, "cost": cost}


# #101's badge: one number from 1 to 100, about the percent of Claude's usage saved, from the
# totals (summed before dividing, so a big task weighs as its baseline does). Its colour bands,
# low to high; the colour is never the only signal, the number and the sentence say it too.
BANDS = ((50, "green"), (25, "yellow"), (10, "orange"), (1, "red"))
QUESTION = "How much Claude did my GPU save me?"
NO_SCORE = "No score yet: we need a task we can compare."


def score(t: dict) -> dict | None:
    """{score, color, pct, sentence} for ``totals``; None with nothing to compare. GPU energy,
    its price and the acceptance rate stay out of it."""
    if not t["tasks"] or t["self_est"] <= 0:
        return None
    pct = 100 * (t["self_est"] - t["claude"]) / t["self_est"]
    n, r = min(100, max(1, round(pct))), round(pct)
    return {"score": n, "color": next(c for lo, c in BANDS if n >= lo), "pct": round(pct, 1),
            "sentence": (f"I used about {r}% less Claude than if Claude had done this work alone."
                         if r >= 1 else "I used about the same amount of Claude." if r == 0
                         else f"I used about {-r}% more Claude.")}


def _tokens(n: int) -> str:
    return f"{n / 1e6:.1f} million" if n >= 1e6 else f"{n / 1000:,.0f},000" if n >= 1000 else str(n)


# fill and text per band: the number is large, so 3:1 is the floor; each pair is past 4.5:1
CARD_COLORS = {"green": ("#1a7f37", "#ffffff"), "yellow": ("#f2c94c", "#1f1f1f"),
               "orange": ("#d9480f", "#ffffff"), "red": ("#b42318", "#ffffff")}


def _wrapped(text: str, y: int, size: int, width: int, style: str) -> tuple[list[str], int]:
    """``text`` as SVG lines of at most ``width`` characters (SVG text doesn't wrap), from
    baseline ``y``; the lines and the baseline after them."""
    rows, row = [], ""
    for w in text.split():
        if row and len(row) + len(w) >= width:
            rows.append(row)
            row = ""
        row += (" " if row else "") + w
    rows.append(row)
    out = [f'<text x="24" y="{y + i * round(size * 1.4)}" font-size="{size}" {style}>{r}</text>'
           for i, r in enumerate(rows)]
    return out, y + len(rows) * round(size * 1.4)


def card(t: dict, period: str) -> str:
    """The badge as a card to keep or post (#101): an SVG made here from the totals alone, with
    no name, path, host or account in it, and nothing fetched. 360 wide: legible at phone width."""
    when = {"week": "Last 7 days", "month": "Last 30 days", "all": "All time"}.get(period, period)
    s = score(t)
    ink, grey = 'fill="#1f1f1f"', 'fill="#444"'
    lines, y = _wrapped(QUESTION, 44, 19, 26, f'font-weight="700" {ink}')
    if s:
        fill, on = CARD_COLORS[s["color"]]
        y += 2
        lines += [f'<rect x="24" y="{y}" width="312" height="108" rx="18" fill="{fill}"/>',
                  f'<text x="180" y="{y + 76}" text-anchor="middle" font-size="64" font-weight="800" '
                  f'fill="{on}">{s["score"]}<tspan font-size="26" font-weight="600"> / 100</tspan></text>']
        more, y = _wrapped(s["sentence"], y + 140, 17, 36, ink)
        lines += more
        more, y = _wrapped(f"Claude alone: about {_tokens(t['self_est'])} weighted tokens. With "
                           f"Workbench: {_tokens(t['claude'])}.", y + 6, 14, 44, grey)
        lines += more
    else:
        more, y = _wrapped(NO_SCORE, y + 10, 17, 34, ink)
        lines += more
    more, y = _wrapped(f"{when} · {t['tasks']} task{'' if t['tasks'] == 1 else 's'} · estimated",
                       y + 6, 13, 48, 'fill="#555"')
    lines += more
    h = y + 10
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="360" height="{h}" viewBox="0 0 360 {h}" '
            'font-family="system-ui, -apple-system, Segoe UI, Roboto, sans-serif">'
            f'<rect width="360" height="{h}" rx="20" fill="#fbf8f3"/>' + "".join(lines) + "</svg>\n")


PERIODS = {"week": 7 * 86400, "month": 30 * 86400, "all": None}  # wb savings --week|--month|--all


def energy(wh: float) -> str:
    return f"{wh / 1000:.1f} kWh" if wh >= 1000 else f"{wh:.0f} Wh"


def summary(t: dict, period: str, api: int = 0) -> str:
    """`wb savings`' one line over ``totals`` (``period`` a PERIODS key, or a label: `wb
    backlog`'s "the batch"): what the tasks cost Claude, the estimate of writing them, the saving
    and its share of that estimate (the one figure: a score beside it was the percent again,
    #1322), the energy and what it cost. ``api``: the period's API tasks: with none of the GPU's
    to compare, "" (their line follows alone, #1121)."""
    when = {"week": "the last 7 days", "month": "the last 30 days", "all": "all time"}.get(period, period)
    if not t["tasks"]:
        return "" if api else f"{when}: nothing to compare yet"
    k = lambda n: f"{n / 1000:.0f}K"  # noqa: E731
    saved = (f"saved ~{k(t['saved'])}" if t["saved"] >= 0 else f"cost ~{k(-t['saved'])} more")
    pct = f" ({100 * t['saved'] / t['self_est']:.0f}%)" if t["self_est"] > 0 else ""
    gpu = f" · GPU {energy(t['wh'])}" if t["wh"] else ""
    # no currency sign: the price is the user's, in whatever they pay in (a `$` was hard-coded, #609)
    cost = f", cost {t['cost']:.2f}" if gpu and t["cost"] is not None else ""
    return (f"{when}: {t['tasks']} task(s) · claude {k(t['claude'])} · writing it yourself "
            f"~{k(t['self_est'])} · {saved}{pct}{gpu}{cost}")


def share(rows: list[dict], period: str, since: float, gpus: list[dict],
          weights: dict[str, int], system: str) -> str:
    """`wb savings --share` (#119): a plain summary to paste into a discussion, where users pool
    their evidence. The machine (its OS, each GPU's name and VRAM), the local models' sizes and no
    names, the tasks, the ratios by size, and the energy. ``weights`` is the models' weights in
    bytes by id; nothing leaves this machine but what the user pastes."""
    when = {"week": "the last 7 days", "month": "the last 30 days", "all": "all time"}.get(period, period)
    used = [r for r in rows if r.get("ts", 0) >= since and of_row(r) is not None]
    t = totals(used)
    gb = lambda n: f"{n / 1e9:.0f} GB"  # noqa: E731
    cards = ", ".join(f"{g['name']}" + (f" ({gb(g['vram'])} VRAM)" if g.get("vram") else "")
                      for g in gpus) or "none found"
    sizes = sorted(weights.get(m) or 0 for m in {r.get("model") for r in used})
    out = [f"workbench savings, {when} (`wb savings --share`)",
           f"- machine: {system}; GPU: {cards}",
           f"- local models: {len(sizes)}" + (" (" + ", ".join(gb(s) if s else "size unknown"
                                                              for s in sizes) + " of weights)"
                                               if sizes else "")]
    if not t["tasks"]:
        return "\n".join(out + ["- no task to compare yet"])
    k = lambda n: f"{n / 1000:.0f}K"  # noqa: E731
    out.append(f"- tasks: {t['tasks']}; Claude used {k(t['claude'])} input-equivalent tokens, "
               f"writing them itself ~{k(t['self_est'])}"
               + (f"; handoff/self {t['claude'] / t['self_est']:.2f}" if t["self_est"] else ""))
    out.append(f"- {COUNTS}")
    s = score(t)  # the badge's number and its words, as the card says them (#101)
    out.append(f"- score: {s['score']}/100. {s['sentence']}" if s else f"- {NO_SCORE}")
    by: dict = {}
    for r in used:
        p = of_row(r)
        b = by.setdefault(r.get("size") or "?", [0, 0, 0])
        b[0], b[1], b[2] = b[0] + 1, b[1] + p["claude"], b[2] + p["self_est"]
    out.append("- by size: " + ", ".join(
        f"{size} {c / s:.2f} ({n})" if s else f"{size} discarded ({n})"
        for size, (n, c, s) in sorted(by.items())))
    if t["wh"]:
        out.append(f"- GPU energy: {energy(t['wh'])}")
    return "\n".join(out)


# --- API tasks (#1022): their own line, beside the score, never in it ------------------------
# Dollars are OpenCode's unit (Money.USD), so they carry a `$`; the electricity's cost doesn't
# (the price is the user's own, #609, #845). Never netted against Claude's tokens.

def provider_labels(pids) -> dict[str, str]:
    """Providers as OpenCode names them (hosted.json, as it is, read once: no catalog read), else
    their ids."""
    from . import models
    pids = list(pids)
    doc = store.read_json(models.hosted_path(), {}) if pids else {}
    provs = doc.get("providers") if isinstance(doc, dict) else None
    provs = provs if isinstance(provs, dict) else {}
    out = {}
    for pid in pids:
        name = provs[pid].get("name") if isinstance(provs.get(pid), dict) else None
        out[pid] = name if isinstance(name, str) and name else pid
    return out


def provider_label(pid: str) -> str:
    return provider_labels([pid])[pid]


def _day(ts: float) -> str:
    import time
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def spent(since: float = 0) -> dict:
    """The day ledger's dollars since ``since`` (meter.py's spend-api.jsonl, `step` and
    `reconcile` rows, as the day cap and the pane read it): open tasks and bench replays in, each
    on the day it was spent. {usd, today (meter.today's, the pane's), by_day: {YYYY-MM-DD: $},
    by_provider: {p: $}, prices (meter.basis of its rows, #1135)}. The results rows are written at a close and dated by it, so `wb savings
    --api` said $3 today and $3 for the week where the ledger said $2 and $5 (#1094, 2026-11-23)."""
    out, bases = {"usd": 0.0, "by_day": {}, "by_provider": {}}, set()
    for r in store.read_jsonl(meter.ledger_path())[0]:
        if not (isinstance(r, dict) and r.get("kind") in ("step", "reconcile") and meter._num(r.get("usd"))
                and meter._num(r.get("ts")) and r["ts"] >= since):
            continue
        d, p = str(r.get("day") or _day(r["ts"])), str(r.get("provider") or "?")
        out["usd"] += r["usd"]
        out["by_day"][d] = out["by_day"].get(d, 0.0) + r["usd"]
        out["by_provider"][p] = out["by_provider"].get(p, 0.0) + r["usd"]
        if r["usd"]:
            bases.add(meter.row_basis(r))
    return {"usd": round(out["usd"], 6), "today": meter.today()["usd"], "prices": meter.basis(bases),
            "by_day": {d: round(v, 6) for d, v in sorted(out["by_day"].items())},
            "by_provider": {p: round(v, 6) for p, v in out["by_provider"].items()}}


def api_totals(rows: list[dict], since: float = 0) -> dict:
    """The API rows measured after ``since``, trials and discards included (money spent is money
    spent): tasks, the known dollars (`cost`), how many have no known cost (`unknown`: no price,
    or a priced model whose figure never came back, #1106, said alike, #1322) or count against a
    plan (`plan`), the trials among them, Claude's side (`claude`, `self_est`: weighted tokens,
    side by side, never subtracted from the dollars), and the dollars by provider. Closed tasks
    only, by their close: the money by day is the ledger's (`spent`, #1094). `prices`, the total's
    and each provider's: meter.basis of the dollars (#1135)."""
    from .results import row_basis, row_cost
    out = {"tasks": 0, "cost": 0.0, "unknown": 0, "plan": 0, "trials": 0,
           "claude": 0, "self_est": 0, "by_provider": {}}
    for r in rows:
        if (r.get("ts") or 0) < since:
            continue
        if isinstance(fb := r.get("fallback_paid"), dict) and isinstance(fb.get("cost"), (int, float)):
            # a GPU fallback's row, the API rounds before it (#1523): their dollars and provider;
            # its Claude side is the GPU score's
            p = out["by_provider"].setdefault(str(fb.get("provider") or "?"), {"tasks": 0, "cost": 0.0, "unknown": 0})
            out["tasks"] += 1
            p["tasks"] += 1
            out["trials"] += bool(r.get("trial"))
            out["cost"] += fb["cost"]
            p["cost"] += fb["cost"]
            if fb["cost"]:
                p.setdefault("bases", set()).add(fb.get("prices") if fb.get("prices") in meter.BASES
                                                 else meter.CATALOG)
            continue
        if r.get("kind") != "api":
            continue
        api = r.get("api") if isinstance(r.get("api"), dict) else {}
        p = out["by_provider"].setdefault(str(r.get("provider") or "?"),
                                          {"tasks": 0, "cost": 0.0, "unknown": 0})
        out["tasks"] += 1
        p["tasks"] += 1
        out["trials"] += bool(r.get("trial"))
        cost = row_cost(r)
        if api.get("source") == "plan":
            out["plan"] += 1
            p["plan"] = p.get("plan", 0) + 1
        elif cost is None:
            out["unknown"] += 1
            p["unknown"] += 1
        else:
            out["cost"] += cost
            p["cost"] += cost
            if cost:
                p.setdefault("bases", set()).add(row_basis(r))
        if (c := _compare(r)) is not None:
            out["claude"] += c["claude"]
            out["self_est"] += c["self_est"]
    out["cost"] = round(out["cost"], 6)
    every = set()
    for p in out["by_provider"].values():
        p["cost"] = round(p["cost"], 6)
        every |= (bases := p.pop("bases", set()))
        p["prices"] = meter.basis(bases)
    out["prices"] = meter.basis(every)
    return out


def paid(row: dict) -> str:
    """An API task's money at its close, for wb accept's and discard's line: `paid ≈$0.12`,
    `paid: unknown` (no price, or no figure came back: said alike, #1322), `plan usage (…)`;
    `paid ≈$0.42 (before the GPU fallback)` for a task its API rounds paid for before it moved
    (`fallback_paid`, #1523); "" for a GPU task. Its Claude side isn't said there: a line of
    estimates on every close cost Claude tokens and changed nothing it did (#1322)."""
    from .results import row_basis, row_cost
    if row.get("kind") != "api":
        fb = row.get("fallback_paid")
        if isinstance(fb, dict) and isinstance(fb.get("cost"), (int, float)):
            return f"paid {money(fb['cost'], fb.get('prices') or meter.CATALOG)} (before the GPU fallback)"
        return ""
    api = row.get("api") if isinstance(row.get("api"), dict) else {}
    if api.get("source") == "plan":
        return f"plan usage ({api.get('plan') or row.get('provider') or '?'})"
    cost = row_cost(row)
    return "paid: unknown" if cost is None else f"paid {money(cost, row_basis(row))}"


def _priced(v: dict) -> bool:
    """Whether a provider's tasks include one with a known price."""
    return v["tasks"] > v["unknown"] + v.get("plan", 0)


def unknown_parts(a: dict) -> list[str]:
    """The tasks with no known cost, in one wording whatever the cause (no price, or no figure
    recorded: #1106's two texts, one since #1322)."""
    return [f"{a['unknown']} with no known cost"] if a.get("unknown") else []


def unpriced(v: dict) -> str:
    """A provider with no known dollars: `plan usage` (a quota, not dollars), else `unknown`."""
    return "plan usage" if v.get("plan") else "unknown"


def money_of(v: dict) -> str:
    """A total's dollars with its `≈` (money, #1135): a row from before it is the catalog's."""
    return money(v["cost"], v.get("prices", meter.CATALOG))


def _by_provider(a: dict) -> str:
    return ", ".join(f"{p} {money_of(v)}" for p, v in
                     sorted(a["by_provider"].items(), key=lambda kv: -kv[1]["cost"]) if _priced(v))


def _money(a: dict) -> str:
    """The period's dollars, the tasks with no known cost and on a plan said apart: never
    `$0.00` for a task whose price is unknown."""
    known = a["tasks"] - a["unknown"] - a["plan"]
    parts = [money_of(a) + (f" ({by})" if (by := _by_provider(a)) else "")] if known else []
    parts += unknown_parts(a)
    if a["plan"]:
        parts.append(f"{a['plan']} on plan usage")
    return " + ".join(parts)


def api_summary(a: dict) -> str | None:
    """`wb savings`' line for the period's API tasks, after the GPU one; None with none."""
    if not a["tasks"]:
        return None
    k = lambda n: f"{n / 1000:.0f}K"  # noqa: E731
    return (f"api: {a['tasks']} task(s) · {_money(a)}"
            + (f" · claude {k(a['claude'])} · writing it yourself ~{k(a['self_est'])}"
               if a["claude"] else ""))


def api_report(a: dict, period: str, bench: dict | None = None, paid: dict | None = None) -> str:
    """`wb savings --api`: the day ledger's dollars (``paid``, `spent`: open tasks and bench
    replays in, as the day cap and the pane count them, #1094) with today's, by provider and by
    day; the period's closed tasks counted (their dollars are --json's: a second total for the
    same period that disagreed with the first, #1322); the bench's replays, already in the total."""
    when = {"week": "the last 7 days", "month": "the last 30 days", "all": "all time"}.get(period, period)
    paid = paid or {"usd": 0.0, "today": 0.0, "by_day": {}, "by_provider": {}}
    if not a["tasks"] and not (bench or {}).get("tasks") and not paid["by_day"]:
        return f"api, {when}: no API task"
    b = paid.get("prices", meter.CATALOG)
    trial = f" (incl. {a['trials']} trial{'s' if a['trials'] > 1 else ''})" if a["trials"] else ""
    closed = ", ".join([f"{a['tasks']} task(s) closed{trial}", *unknown_parts(a),
                        *([f"{a['plan']} on plan usage"] if a["plan"] else [])]) if a["tasks"] else ""
    # the ledger's dollars when it booked any: a plan-only week never reads `$0` (#1022's review)
    spent_ = f"{money(paid['usd'], b)} ({money(paid['today'], b)} today)" if paid["by_day"] else ""
    out = [f"api, {when}: " + " · ".join(x for x in (spent_, closed) if x)]
    if provs := [(p, v) for p, v in sorted((paid.get("by_provider") or {}).items(), key=lambda kv: -kv[1]) if v]:
        out.append("  by provider: " + " · ".join(f"{p} {money(v, b)}" for p, v in provs))
    if paid["by_day"]:
        out.append("  by day: " + " · ".join(f"{d[5:]} {money(v, b)}" for d, v in paid["by_day"].items()))
    if bench and bench.get("tasks"):
        out.append(f"  bench replays: {money(bench['cost'], bench.get('prices', meter.CATALOG))} "
                   f"({bench['tasks']})"
                   + (f" + {bench['unknown']} with no known cost" if bench.get("unknown") else ""))
    return "\n".join(out)
