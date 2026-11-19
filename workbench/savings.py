"""What a task saved Claude, against Claude writing it itself: an estimate, always shown as one.

The estimate comes from eight paired runs on 2026-11-17: Claude made roughly 4.9 +
lines/69 calls, each costing the session's context cost per call, and wrote about 36
output tokens per line changed. Mean error 19%, single tasks -36% to +48%."""

from __future__ import annotations

import json
import os
from pathlib import Path

from . import spend, store

# The eight-paired-run fit (2026-11-17), see the module docstring.
SELF_CALLS = 4.9
SELF_LINES_PER_CALL = 69
SELF_OUT_PER_LINE = 36


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
    but its GPU energy was still spent, so it records a negative saving."""
    if row.get("outcome") == "discarded":
        u = row.get("claude")
        if not isinstance(u, dict):
            return None
        c = spend.weighted(u)
        return {"claude": c, "self_est": 0, "saved": -c, "wh": row.get("gpu_wh")}
    if "claude" not in row or "diff" not in row:
        return None
    pc = per_call(row["claude"])
    if pc is None:
        return None
    lines = row["diff"].get("added", 0) + row["diff"].get("removed", 0)
    self_est = estimate_self(lines, pc)
    claude = spend.weighted(row["claude"])
    return {"claude": claude, "self_est": self_est, "saved": self_est - claude,
            "wh": row.get("gpu_wh")}


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


def set_price(kwh_price: float) -> None:
    """Write {"kwh_price": kwh_price} to the file price() reads (creating its directory)."""
    store.write_json(_path(), {"kwh_price": kwh_price})


def totals(rows: list[dict], since: float = 0) -> dict:
    """The saved totals over the rows measured after ``since``.

    ``tasks`` is how many of them have both sides; ``claude``/``self_est``/``saved``
    are the token sums (weighted); ``wh`` is their GPU energy in watt-hours; ``cost``
    is what that energy is worth at the set price, rounded to a cent."""
    parts = [of_row(r) for r in rows if r.get("ts", 0) >= since]
    parts = [p for p in parts if p is not None]
    wh = round(sum(p["wh"] for p in parts if p["wh"] is not None), 1)
    cost = round(wh / 1000 * price(), 2) if parts and price() is not None else None
    return {"tasks": len(parts),
            "claude": sum(p["claude"] for p in parts),
            "self_est": sum(p["self_est"] for p in parts),
            "saved": sum(p["saved"] for p in parts),
            "wh": wh, "cost": cost}


def line(row: dict) -> str | None:
    """One row's savings, readable: claude: 56K · writing it yourself: ~59K
    (estimate) · saved ~3K · GPU 41 Wh; None when there is no comparison.
    A discarded row reads: claude: 21K · discarded, nothing saved · GPU 12 Wh."""
    est = of_row(row)
    if est is None:
        return None
    saved, wh = est["saved"], est["wh"]
    k = lambda n: f"{n / 1000:.0f}K"  # noqa: E731
    if row.get("outcome") == "discarded":
        mid = " · discarded, nothing saved"
        if wh is not None:
            mid += f" · GPU {wh:.0f} Wh"
        return f"claude: {k(est['claude'])}{mid}"
    mid = f" · saved ~{k(saved)}" if saved >= 0 else f" · cost ~{k(-saved)} more"
    if wh is not None:
        mid += f" · GPU {wh:.0f} Wh"
    return f"claude: {k(est['claude'])} · writing it yourself: ~{k(est['self_est'])} (estimate){mid}"


PERIODS = {"week": 7 * 86400, "month": 30 * 86400, "all": None}  # wb savings --week|--month|--all


def energy(wh: float) -> str:
    return f"{wh / 1000:.1f} kWh" if wh >= 1000 else f"{wh:.0f} Wh"


def summary(t: dict, period: str) -> str:
    """`wb savings`' one line over ``totals`` (``period`` a PERIODS key, or a label: `wb
    backlog`'s "the batch"): what the tasks cost Claude, the estimate of
    writing them, the saving and its share of that estimate (the badge's score, #101), the
    energy and what it cost."""
    when = {"week": "the last 7 days", "month": "the last 30 days", "all": "all time"}.get(period, period)
    if not t["tasks"]:
        return (f"{when}: no task to compare yet (an accepted task needs its Claude usage and "
                "its diff, recorded since 0.95)")
    k = lambda n: f"{n / 1000:.0f}K"  # noqa: E731
    saved = (f"saved ~{k(t['saved'])}" if t["saved"] >= 0 else f"cost ~{k(-t['saved'])} more")
    pct = f" ({100 * t['saved'] / t['self_est']:.0f}%)" if t["self_est"] > 0 else ""
    gpu = f" · GPU {energy(t['wh'])}" if t["wh"] else ""
    cost = f" (${t['cost']:.2f})" if gpu and t["cost"] is not None else ""
    return (f"{when}: {t['tasks']} task(s) · claude {k(t['claude'])} · writing it yourself "
            f"~{k(t['self_est'])} (estimate) · {saved}{pct}{gpu}{cost}")
