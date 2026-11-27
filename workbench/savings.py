"""What the tasks spent, stated as facts (#1618, the owner, 2026-11-27: "instead of framing
things as what is saved (cost) can we just frame things factually as what we spend, be it
electricity or API spend, users can figure out if they're losing or saving money themselves"):
Claude's tokens on them (weighted, spend.py), the GPU's energy and its cost at the user's price,
and the API tasks' dollars by provider. Nothing here compares with an estimate of writing the
work by hand: that estimate, its score out of 100 and its badge went with #1618. The module keeps
its name, and the price file `savings.json` (`$WB_SAVINGS`) its own, so a price set stays set."""

from __future__ import annotations

import json
import os
from pathlib import Path

from . import meter, spend, store
from .meter import money, usd


def claude_of(row: dict) -> int:
    """Claude's weighted tokens on one results.jsonl row (its share of its session), 0 without
    a usage recorded."""
    u = row.get("claude")
    return spend.weighted(u) if isinstance(u, dict) and u else 0


def of_row(row: dict) -> dict | None:
    """One GPU task's spend: Claude's tokens and the GPU's energy (None when not measured); None
    for an API task, whose money is api_totals' (its Claude tokens counted there)."""
    if row.get("kind") == "api":
        return None
    return {"claude": claude_of(row), "wh": _wh(row)}


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
    """The GPU tasks measured after ``since``: ``tasks``, Claude's weighted tokens on them
    (``claude``), their GPU energy in watt-hours (``wh``) and what it cost at the set price,
    rounded to a cent (``cost``; None with no price)."""
    parts = [p for r in rows if r.get("ts", 0) >= since and (p := of_row(r)) is not None]
    wh = round(sum(p["wh"] for p in parts if p["wh"] is not None), 1)
    cost = round(wh / 1000 * price(), 2) if parts and price() is not None else None
    return {"tasks": len(parts), "claude": sum(p["claude"] for p in parts), "wh": wh, "cost": cost}


PERIODS = {"week": 7 * 86400, "month": 30 * 86400, "all": None}  # wb spend --week|--month|--all


def energy(wh: float) -> str:
    return f"{wh / 1000:.1f} kWh" if wh >= 1000 else f"{wh:.0f} Wh"


def tokens(n: int) -> str:
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1000:.0f}K"


def summary(t: dict, period: str, api: int = 0) -> str:
    """`wb spend`'s line over ``totals`` (``period`` a PERIODS key, or a label: `wb backlog`'s
    "the batch"): the GPU tasks, Claude's tokens on them, the energy and what it cost. ``api``:
    the period's API tasks: with no GPU task, "" (their line follows alone, #1121)."""
    when = {"week": "the last 7 days", "month": "the last 30 days", "all": "all time"}.get(period, period)
    if not t["tasks"]:
        return "" if api else f"{when}: no task yet"
    gpu = f" · GPU {energy(t['wh'])}" if t["wh"] else ""
    # no currency sign: the price is the user's, in whatever they pay in (a `$` was hard-coded, #609)
    cost = f", cost {t['cost']:.2f}" if gpu and t["cost"] is not None else ""
    return f"{when}: {t['tasks']} GPU task(s) · Claude {tokens(t['claude'])} tokens{gpu}{cost}"


def share(rows: list[dict], period: str, since: float, gpus: list[dict],
          weights: dict[str, int], system: str) -> str:
    """`wb spend --share` (#119): a plain summary to paste into a discussion, where users pool
    their evidence. The machine (its OS, each GPU's name and VRAM), the local models' sizes and no
    names, the tasks and outcomes by size, Claude's tokens and the energy. ``weights`` is the
    models' weights in bytes by id; nothing leaves this machine but what the user pastes."""
    when = {"week": "the last 7 days", "month": "the last 30 days", "all": "all time"}.get(period, period)
    used = [r for r in rows if r.get("ts", 0) >= since and of_row(r) is not None]
    t = totals(used)
    gb = lambda n: f"{n / 1e9:.0f} GB"  # noqa: E731
    cards = ", ".join(f"{g['name']}" + (f" ({gb(g['vram'])} VRAM)" if g.get("vram") else "")
                      for g in gpus) or "none found"
    sizes = sorted(weights.get(m) or 0 for m in {r.get("model") for r in used})
    out = [f"workbench spend, {when} (`wb spend --share`)",
           f"- machine: {system}; GPU: {cards}",
           f"- local models: {len(sizes)}" + (" (" + ", ".join(gb(s) if s else "size unknown"
                                                              for s in sizes) + " of weights)"
                                               if sizes else "")]
    if not t["tasks"]:
        return "\n".join(out + ["- no GPU task yet"])
    out.append(f"- tasks: {t['tasks']}; Claude used {tokens(t['claude'])} input-equivalent tokens on them")
    by: dict = {}
    for r in used:
        b = by.setdefault(r.get("size") or "?", [0, 0, 0])
        b[0] += 1
        b[1] += r.get("outcome") == "accepted"
        b[2] += claude_of(r)
    out.append("- by size: " + ", ".join(f"{size} {n} ({ok} accepted, Claude {tokens(c)})"
                                         for size, (n, ok, c) in sorted(by.items())))
    if t["wh"]:
        out.append(f"- GPU energy: {energy(t['wh'])}")
    return "\n".join(out)


# --- API tasks (#1022): their own line, beside the GPU's ------------------------------------
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
    by_provider: {p: $}, prices (meter.basis of its rows, #1135)}. The results rows are written at a close and dated by it, so `wb spend
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
    plan (`plan`), the trials among them, Claude's weighted tokens on them (`claude`), and the
    dollars by provider. Closed tasks
    only, by their close: the money by day is the ledger's (`spent`, #1094). `prices`, the total's
    and each provider's: meter.basis of the dollars (#1135)."""
    from .results import row_basis, row_cost
    out = {"tasks": 0, "cost": 0.0, "unknown": 0, "plan": 0, "trials": 0,
           "claude": 0, "by_provider": {}}
    for r in rows:
        if (r.get("ts") or 0) < since:
            continue
        if isinstance(fb := r.get("fallback_paid"), dict) and isinstance(fb.get("cost"), (int, float)):
            # a GPU fallback's row, the API rounds before it (#1523): their dollars and provider;
            # its Claude tokens are the GPU line's
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
        out["claude"] += claude_of(r)
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
    figures on every close cost Claude tokens and changed nothing it did (#1322)."""
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
    """`wb spend`'s line for the period's API tasks, after the GPU one; None with none."""
    if not a["tasks"]:
        return None
    return (f"api: {a['tasks']} task(s) · {_money(a)}"
            + (f" · Claude {tokens(a['claude'])} tokens" if a["claude"] else ""))


def api_report(a: dict, period: str, bench: dict | None = None, paid: dict | None = None) -> str:
    """`wb spend --api`: the day ledger's dollars (``paid``, `spent`: open tasks and bench
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
