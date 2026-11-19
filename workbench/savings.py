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
    the agent saved that much); ``wh`` is the task's GPU energy when measured."""
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
    (estimate) · saved ~3K · GPU 41 Wh; None when there is no comparison."""
    est = of_row(row)
    if est is None:
        return None
    saved, wh = est["saved"], est["wh"]
    k = lambda n: f"{n / 1000:.0f}K"  # noqa: E731
    mid = f" · saved ~{k(saved)}" if saved >= 0 else f" · cost ~{k(-saved)} more"
    if wh is not None:
        mid += f" · GPU {wh:.0f} Wh"
    return f"claude: {k(est['claude'])} · writing it yourself: ~{k(est['self_est'])} (estimate){mid}"
