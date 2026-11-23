"""Roles: which model a kind of work goes to (#1014).

Three fixed roles, `significant` (complex coding), `trivial` and `writing` (prose), each with a
selector (`via`: gpu or api), its GPU model (`gpu`: an id, or `auto`, the router's pick), its API
model (`api`: an exact OpenCode id, kept while the role is on the GPU) and `thinking` (off or
on). Kept in ~/.config/workbench/routes.json (`$WB_ROUTES`), with one day cap (`day_usd`). A
missing file or role is the GPU, exactly as before roles; a file that can't be read is an error,
never "all GPU": that would move the user off their API choice without a word.

Claude never changes the API side on its own: under Claude Code (CLAUDECODE=1) `wb route` applies
such a change only when the mod's dialog approved exactly it (`approved`, route-ok.json in
$WB_HOME, one-shot, fresh for ROUTE_OK_SECS). A GPU model pin needs no dialog; the mod echoes
every change as a [workbench] row. A role's `said`/`by`/`ts` record its last gated change (who
sent its code where), `gpu_by`/`gpu_ts` its last change that needed no yes: a pin never rewrites
the record of the user's yes (2026-11-23 review). The change line is the dialog's text: the mod shows what
`wb route --dry-run --json` prints, so both come from `change` here (Couplings)."""
from __future__ import annotations

import copy
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import store

ROLES = ("significant", "trivial", "writing")
# how the pane names them (#1023); files and commands keep the role names
LABELS = {"significant": "Complex coding", "trivial": "Trivial coding", "writing": "English writing"}
VIAS = ("gpu", "api")
THINKING = ("off", "on")
AUTO = "auto"  # the GPU model the router picks: today's behaviour, the default
BY = ("dialog", "pane", "terminal", "claude")  # who made a role's last change (`by`, `gpu_by`)
STAMPS = ("said", "by", "ts", "gpu_by", "gpu_ts")  # a role's record of its changes (stamp)
DAY_USD_SUGGEST = 5  # the dialog's and the first run's suggestion only: there is no default cap
ROUTE_OK = "route-ok.json"  # $WB_HOME/route-ok.json: the mod's dialog approved this one change
ROUTE_OK_SECS = 120  # the dialog's yes is for the call it let through, run at once, as SEND_OK_SECS
NO_CAP = (f"no day cap yet: ask the user for one (suggest ${DAY_USD_SUGGEST}), then add "
          f"--day-usd {DAY_USD_SUGGEST}")


class RoutesError(RuntimeError):
    """routes.json can't be read, or a change can't be made; the message says why."""


def path() -> Path:
    return Path(os.environ.get("WB_ROUTES") or Path.home() / ".config" / "workbench" / "routes.json")


def unreadable(why) -> str:
    return f"{path()} can't be read ({why}): fix it, or wb route <role> … sets it again"


def default_role() -> dict:
    return {"via": "gpu", "gpu": AUTO, "thinking": "off"}


def _cap(v):
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not v > 0 or v != v or v == float("inf"):
        raise RoutesError(f"day_usd is {v!r}, not a positive number of dollars")
    return v


def normalize(doc) -> dict:
    """The document with every role filled in (a missing one is the GPU's default), its values
    checked; RoutesError for one that isn't what wb writes. An unknown role is an error too: a
    hand edit's "trivail" would otherwise be dropped without a word."""
    if not isinstance(doc, dict):
        raise RoutesError("not a JSON object")
    roles = doc.get("roles", {})
    if not isinstance(roles, dict):
        raise RoutesError("roles is not an object")
    if extra := sorted(set(roles) - set(ROLES)):
        raise RoutesError(f"unknown role {extra[0]!r} (the roles: {', '.join(ROLES)})")
    out = {}
    for r in ROLES:
        e = roles.get(r, {})
        if not isinstance(e, dict):
            raise RoutesError(f"{r} is not an object")
        row = default_role()
        for key, allowed in (("via", VIAS), ("thinking", THINKING)):
            if key in e:
                if e[key] not in allowed:
                    raise RoutesError(f"{r}'s {key} is {e[key]!r}, not {' or '.join(allowed)}")
                row[key] = e[key]
        for key in ("gpu", "api"):
            if key in e and e[key] is not None:
                if not isinstance(e[key], str) or not e[key].strip():
                    raise RoutesError(f"{r}'s {key} is {e[key]!r}, not a model id")
                row[key] = e[key]
        if row["via"] == "api" and not row.get("api"):
            raise RoutesError(f"{r} is on API with no api model")
        for key in STAMPS:
            if key in e:
                row[key] = e[key]
        out[r] = row
    return {"v": 1, "day_usd": _cap(doc.get("day_usd")), "roles": out}


def read() -> dict:
    """routes.json, normalized: a missing file is every role on the GPU (the router picks).
    RoutesError when it can't be read or parsed."""
    try:
        text = path().read_text(encoding="utf-8")
    except FileNotFoundError:
        return normalize({})
    except UnicodeDecodeError:
        raise RoutesError("not UTF-8") from None
    except OSError as e:
        raise RoutesError(e.strerror or str(e)) from None
    try:
        doc = json.loads(text)
    except ValueError as e:
        raise RoutesError(f"not JSON: {e}") from None
    return normalize(doc)


def load() -> dict:
    """read() under the name the 2.0 branches were pinned to (stats, spend, thinking, the start
    line): RoutesError too, so no reader shows a broken file as every role on the GPU."""
    return read()


@contextmanager
def editing(lenient: bool = False):
    """A locked read-modify-write of routes.json (store.edit_config: the config files' lock, a
    link kept, nothing written when the body raises). Yields [doc, why]: the body changes doc in
    place; ``lenient``: a file that can't be read starts from the defaults, its why given."""
    held = ["", ""]

    def reader():
        try:
            held[0] = read()
        except RoutesError as e:
            if not lenient:
                raise
            held[0], held[1] = normalize({}), str(e)
        return held[0]
    with store.edit_config(path(), reader):
        yield held


def write(doc: dict) -> None:
    """``doc`` (normalized first) as routes.json, under the lock."""
    new = normalize(doc)
    with editing(lenient=True) as held:
        held[0].clear()
        held[0].update(new)


def money(v) -> str:
    return f"${int(v)}" if float(v).is_integer() else f"${v:.2f}"


def _side(row: dict) -> str:
    if row["via"] == "api":
        return f"API {row['api']}"
    return "GPU" if row["gpu"] == AUTO else f"GPU {row['gpu']}"


def _gpu_name(gpu: str) -> str:
    return "the router's pick" if gpu == AUTO else gpu


@dataclass
class Change:
    doc: dict            # the new document; nothing is written
    line: str            # the change, as the dialog shows it; "" for none
    gated: bool          # it touches the API side or the cap: the user's yes in the dialog
    roles: list = field(default_factory=list)  # the roles it changes


def _role_part(r: str, old: dict, row: dict, cap, who: Callable[[str], str]) -> tuple[list, bool]:
    clauses, gated = [], False
    on_api = row["via"] == "api"
    moved = old["via"] != row["via"] or (on_api and old.get("api") != row.get("api"))
    if moved:
        gated = True
        clauses.append(f"{_side(old)} → {_side(row)}")
        if on_api:
            clauses += [f"your code goes to {who(row['api'])}", f"thinking {row['thinking']}"]
            if cap is not None:
                clauses.append(f"cap {money(cap)}/day")
        elif row.get("api"):
            clauses.append(f"API kept: {row['api']}")
    elif old.get("api") != row.get("api"):  # set while on the GPU: used by wb route <role> api
        gated = True
        clauses.append(f"API model {old.get('api') or 'none'} → {row['api']} ({who(row['api'])}), "
                       "used once it goes to API")
    if old["gpu"] != row["gpu"] and not (moved and not on_api):
        clauses.append(f"GPU model {_gpu_name(old['gpu'])} → {_gpu_name(row['gpu'])}")
    if old["thinking"] != row["thinking"] and not (moved and on_api):
        clauses.append(f"thinking {old['thinking']} → {row['thinking']}")
        gated |= on_api
    return clauses, gated


def change(doc: dict, role: str | None, via: str | None = None, gpu: str | None = None,
           api: str | None = None, thinking: str | None = None, day_usd=None,
           who: Callable[[str], str] = lambda mid: mid.split("/", 1)[0]) -> Change:
    """What a `wb route` does to ``doc``: ``role`` one of ROLES, "all" (``via`` and ``thinking``
    only) or None (the cap alone). ``who``: an API id's provider, as the user knows it (the
    catalog's name). Never writes. RoutesError for a change that can't be made: `api` with no
    API model kept, and the first API role with no cap (NO_CAP)."""
    new = copy.deepcopy(doc)
    if day_usd is not None:
        new["day_usd"] = _cap(day_usd)
    cap = new["day_usd"]
    parts, gated, changed, to_api = [], False, [], False
    for r in (ROLES if role == "all" else (role,) if role else ()):
        old = doc["roles"][r]
        row = dict(old)
        if api is not None:
            row["api"] = api
        if gpu is not None:
            row["gpu"] = gpu
        if thinking is not None:
            row["thinking"] = thinking
        if via == "api" and not row.get("api"):
            if role != "all":
                raise RoutesError(f"{r} has no API model yet: wb route {r} <provider/model>")
            parts.append(f"{r}: stays GPU (no API model set)")
            row["via"] = old["via"]
        elif via is not None:
            row["via"] = via
        clauses, g = _role_part(r, old, row, cap, who)
        if not clauses:
            continue
        to_api |= row["via"] == "api" and (old["via"] != "api" or old.get("api") != row.get("api"))
        parts.append(f"{r}: " + " · ".join(clauses))
        gated |= g
        changed.append(r)
        new["roles"][r] = row  # its stamps kept: stamp() renews the side this change touched
    if to_api and cap is None:
        raise RoutesError(NO_CAP)
    if cap != doc["day_usd"]:
        gated = True
        if doc["day_usd"] is not None or not to_api:  # else the role's "cap $N/day" said it
            parts.append(f"day cap {money(doc['day_usd']) if doc['day_usd'] is not None else 'none'}"
                         f" → {money(cap)}/day")
    if not changed and cap == doc["day_usd"]:  # a "stays GPU" alone changes nothing
        return Change(new, "", False, [])
    return Change(new, "; ".join(parts), gated, changed)


def stamp(ch: Change, by: str, now: float | None = None) -> dict:
    """ch.doc with each changed role's record: a gated change's `said` (the line), `by` and `ts`;
    one that needed no yes (a GPU pin, thinking on the GPU) only `gpu_by` and `gpu_ts`. Claude's
    pin on a role on API rewrote `by` to claude and `said` to the pin's line, so the user's own
    yes to send that role's code read as Claude's (2026-11-23 review)."""
    ts = store.now() if now is None else now
    for r in ch.roles:
        ch.doc["roles"][r].update(dict(said=ch.line, by=by, ts=ts) if ch.gated else dict(gpu_by=by, gpu_ts=ts))
    return ch.doc


def summary(doc: dict) -> str:
    """The roles in one line, from the document alone (the mod's routesSummary says the same:
    Couplings): what the context block and the [workbench] echo tell Claude."""
    out = []
    for r in ROLES:
        row = doc["roles"][r]
        if row["via"] == "api":
            out.append(f"{r} API {row['api']}, thinking {row['thinking']}")
            continue
        text = f"{r} GPU " + ("(the router picks)" if row["gpu"] == AUTO else row["gpu"])
        if row.get("api"):
            text += f", API kept: {row['api']}"
        if row["thinking"] == "on":
            text += ", thinking on"
        out.append(text)
    return "; ".join(out)


def any_api(doc: dict) -> bool:
    return any(doc["roles"][r].get("api") for r in ROLES)


def approved(expect: dict) -> dict | None:
    """The mod's approval of exactly this change, consumed (store.take_approval: one call takes
    it, matching or not), or None. ``expect``'s keys must all match: `argv` (the canonical
    command) or `start` ({task, model}), and `change`, the line computed now, so a file that
    moved between the dialog and the call is caught."""
    ok = store.take_approval(ROUTE_OK, ROUTE_OK_SECS)
    if ok is None or any(ok.get(k) != v for k, v in expect.items()):
        return None
    return ok
