"""What a task saved Claude, against Claude writing it itself: an estimate, always shown as one.

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

from . import spend, store

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
    #1022); api_totals and api_line say it apart."""
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
        return {"claude": c, "self_est": 0, "saved": -c, "wh": row.get("gpu_wh"),
                "remote": bool(row.get("remote_gpu"))}
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
    return {"claude": claude, "self_est": self_est, "saved": self_est - claude,
            "wh": row.get("gpu_wh"), "remote": bool(row.get("remote_gpu"))}


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
    period = [(r, of_row(r)) for r in rows if r.get("ts", 0) >= since]
    parts = [p for _, p in period if p is not None]
    wh = round(sum(p["wh"] for p in parts if p["wh"] is not None), 1)
    cost = round(wh / 1000 * price(), 2) if parts and price() is not None else None
    return {"tasks": len(parts),
            # the coverage: every closed task of the period, compared or not (#101)
            "of": sum(p is not None or r.get("outcome") in ("accepted", "discarded") for r, p in period
                      if r.get("kind") != "api"),  # an API task is no GPU task left uncompared
            "claude": sum(p["claude"] for p in parts),
            "self_est": sum(p["self_est"] for p in parts),
            "saved": sum(p["saved"] for p in parts),
            "wh": wh, "cost": cost, "remote": sum(p["remote"] for p in parts)}


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
    more, y = _wrapped(f"{when} · {t['tasks']} of {t.get('of', t['tasks'])} tasks counted · "
                       "estimated", y + 6, 13, 48, 'fill="#555"')
    lines += more
    h = y + 10
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="360" height="{h}" viewBox="0 0 360 {h}" '
            'font-family="system-ui, -apple-system, Segoe UI, Roboto, sans-serif">'
            f'<rect width="360" height="{h}" rx="20" fill="#fbf8f3"/>' + "".join(lines) + "</svg>\n")


def _gpu(est: dict) -> str:
    """The energy part of a line: a remote model's GPU is another machine's, which nothing here
    reads, so it says so instead of showing nothing (#117)."""
    if est["remote"]:
        return " · GPU energy not measured (another machine's)"
    return f" · GPU {est['wh']:.0f} Wh" if est["wh"] is not None else ""


def line(row: dict) -> str | None:
    """One row's savings, readable: claude: 56K · writing it yourself: ~59K
    (estimate) · saved ~3K · GPU 41 Wh; None when there is no comparison.
    A discarded row reads: claude: 21K · discarded, nothing saved · GPU 12 Wh."""
    if row.get("kind") == "api":
        return api_line(row)
    est = of_row(row)
    if est is None:
        return None
    saved, wh = est["saved"], est["wh"]
    k = lambda n: f"{n / 1000:.0f}K"  # noqa: E731
    if row.get("outcome") == "discarded":
        mid = " · discarded, nothing saved"
        mid += _gpu(est)
        return f"claude: {k(est['claude'])}{mid}"
    mid = f" · saved ~{k(saved)}" if saved >= 0 else f" · cost ~{k(-saved)} more"
    mid += _gpu(est)
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
    # no currency sign: the price is the user's, in whatever they pay in (a `$` was hard-coded, #609)
    cost = f", cost {t['cost']:.2f}" if gpu and t["cost"] is not None else ""
    away = (f" · {t['remote']} on another machine's GPU, not measured" if t.get("remote") else "")
    s = score(t)
    return (f"{when}: {t['tasks']} task(s) · claude {k(t['claude'])} · writing it yourself "
            f"~{k(t['self_est'])} (estimate) · {saved}{pct}{gpu}{cost}{away}"
            + (f" · score {s['score']}/100" if s else ""))


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
    out = [f"workbench savings, {when} (an estimate: `wb savings --share`)",
           f"- machine: {system}; GPU: {cards}",
           f"- local models: {len(sizes)}" + (" (" + ", ".join(gb(s) if s else "size unknown"
                                                              for s in sizes) + " of weights)"
                                               if sizes else "")]
    if not t["tasks"]:
        return "\n".join(out + ["- no task to compare yet"])
    k = lambda n: f"{n / 1000:.0f}K"  # noqa: E731
    out.append(f"- tasks: {t['tasks']}; Claude used {k(t['claude'])} input-equivalent tokens, "
               f"writing them itself ~{k(t['self_est'])} (estimate)"
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
    out.append(f"- GPU energy: {energy(t['wh'])}" if t["wh"] else "- GPU energy: not measured")
    if t["remote"]:
        out[-1] += f" ({t['remote']} task(s) on another machine's GPU, not measured)"
    return "\n".join(out)


# --- API tasks (#1022): their own line, beside the score, never in it ------------------------
# Dollars are OpenCode's unit (Money.USD), so they carry a `$`; the electricity's cost doesn't
# (the price is the user's own, #609, #845). Never netted against Claude's tokens.

def usd(x: float) -> str:
    """$0.42; a cent's fraction to two figures, written out ($0.0041, $0.00004: `:.2g` gave
    $4e-05, #1022's review); a free model's $0. The pane's and the chat's `usd` say the same."""
    from decimal import Decimal
    if x == 0:
        return "$0"
    if abs(x) >= 0.01:
        return f"${x:.2f}"
    r = float(f"{x:.2g}")  # 0.009999 rounds to a cent: $0.01, not $0.010
    return f"${r:.2f}" if abs(r) >= 0.01 else "$" + format(Decimal(repr(r)), "f")


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


def api_totals(rows: list[dict], since: float = 0) -> dict:
    """The API rows measured after ``since``, trials and discards included (money spent is money
    spent): tasks, the known dollars (`cost`), how many had no price (`unknown`) or count against
    a plan (`plan`), the trials among them, Claude's side (`claude`, `self_est`: weighted tokens,
    side by side, never subtracted from the dollars), and the dollars by provider and local day."""
    from .results import row_cost
    out = {"tasks": 0, "cost": 0.0, "unknown": 0, "plan": 0, "trials": 0, "claude": 0,
           "self_est": 0, "by_provider": {}, "by_day": {}}
    for r in rows:
        if r.get("kind") != "api" or (r.get("ts") or 0) < since:
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
            day = _day(r.get("ts") or 0)
            out["by_day"][day] = out["by_day"].get(day, 0.0) + cost
        if (c := _compare(r)) is not None:
            out["claude"] += c["claude"]
            out["self_est"] += c["self_est"]
    out["cost"] = round(out["cost"], 6)
    for p in out["by_provider"].values():
        p["cost"] = round(p["cost"], 6)
    out["by_day"] = {d: round(v, 6) for d, v in sorted(out["by_day"].items())}
    return out


def _paid(row: dict) -> str:
    from .results import row_cost
    api = row.get("api") if isinstance(row.get("api"), dict) else {}
    pid = str(row.get("provider") or "?")
    if api.get("source") == "plan":
        return f"plan usage ({api.get('plan') or pid})"
    cost = row_cost(row)
    if cost is None:
        return "paid: unknown (no price for this model)"
    return f"paid {provider_label(pid)} {usd(cost)} ({row.get('model')})"


def api_line(row: dict) -> str:
    """An API task's line at its close (line() gives it for one): Claude's side as line() says it,
    then what the provider was paid, apart: claude: 56K · writing it yourself: ~59K (estimate) ·
    paid DeepSeek $0.12 (deepseek/deepseek-flash)."""
    est = _compare(row)
    k = lambda n: f"{n / 1000:.0f}K"  # noqa: E731
    if est is None:
        return _paid(row)
    if row.get("outcome") == "discarded":
        return f"claude: {k(est['claude'])} · discarded · {_paid(row)}"
    return (f"claude: {k(est['claude'])} · writing it yourself: ~{k(est['self_est'])} (estimate) · "
            f"{_paid(row)}")


def _priced(v: dict) -> bool:
    """Whether a provider's tasks include one with a known price."""
    return v["tasks"] > v["unknown"] + v.get("plan", 0)


def _by_provider(a: dict) -> str:
    return ", ".join(f"{p} {usd(v['cost'])}" for p, v in
                     sorted(a["by_provider"].items(), key=lambda kv: -kv[1]["cost"]) if _priced(v))


def _money(a: dict) -> str:
    """The period's dollars, the tasks with no price and on a plan said apart: never `$0.00`
    for a task whose price is unknown."""
    known = a["tasks"] - a["unknown"] - a["plan"]
    parts = [f"{usd(a['cost'])}" + (f" ({by})" if (by := _by_provider(a)) else "")] if known else []
    if a["unknown"]:
        parts.append(f"{a['unknown']} with no price")
    if a["plan"]:
        parts.append(f"{a['plan']} on plan usage")
    return " + ".join(parts)


def _counts(a: dict) -> str:
    """`--api`'s first line: the dollars and the tasks with no price or on a plan, without the
    providers (its `by provider:` line says them, #1022's review)."""
    known = a["tasks"] - a["unknown"] - a["plan"]
    return ", ".join(([usd(a["cost"])] if known else [])
                     + ([f"{a['unknown']} with no price"] if a["unknown"] else [])
                     + ([f"{a['plan']} on plan usage"] if a["plan"] else []))


def api_summary(a: dict) -> str | None:
    """`wb savings`' line for the period's API tasks, after the GPU one; None with none."""
    if not a["tasks"]:
        return None
    k = lambda n: f"{n / 1000:.0f}K"  # noqa: E731
    return (f"api: {a['tasks']} task(s) · {_money(a)}"
            + (f" · claude {k(a['claude'])} · writing it yourself ~{k(a['self_est'])} (estimate)"
               if a["claude"] else ""))


def api_report(a: dict, period: str, bench: dict | None = None) -> str:
    """`wb savings --api`: the period's API money, by provider and by day, and the bench's replays
    (their rows are bench/runs.jsonl's, not results')."""
    when = {"week": "the last 7 days", "month": "the last 30 days", "all": "all time"}.get(period, period)
    if not a["tasks"] and not (bench or {}).get("tasks"):
        return f"api, {when}: no API task"
    trial = f" (incl. {a['trials']} trial{'s' if a['trials'] > 1 else ''})" if a["trials"] else ""
    out = [f"api, {when}: {a['tasks']} task(s)" + (f", {_counts(a)}" if a["tasks"] else "") + trial]
    provs = sorted(a["by_provider"].items(), key=lambda kv: (-kv[1]["cost"], kv[0]))
    if provs:
        out.append("  by provider: " + " · ".join(
            f"{p} " + (usd(v["cost"]) if _priced(v) else "plan usage" if v.get("plan") else "no price")
            + f" ({v['tasks']})"
            for p, v in provs))
    if a["by_day"]:
        out.append("  by day: " + " · ".join(f"{d[5:]} {usd(v)}" for d, v in a["by_day"].items()))
    if bench and bench.get("tasks"):
        out.append(f"  bench replays: {usd(bench['cost'])} ({bench['tasks']})"
                   + (f" + {bench['unknown']} with no price" if bench.get("unknown") else ""))
    return "\n".join(out)
