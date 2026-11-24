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
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import store
from .privacy import OPENROUTER  # the provider whose roles keep an `order` (#1021)

ROLES = ("significant", "trivial", "writing")
# how the pane names them (#1023); files and commands keep the role names
LABELS = {"significant": "Complex coding", "trivial": "Trivial coding", "writing": "English writing"}
VIAS = ("gpu", "api")
# the Agents switch (#1016, #1023): routes.json `use`, over every role's own `via`; missing is
# "both", each role as set. `wb route all gpu|api|both` sets it, never a role's via, so going
# back to Both restores the per-role mix
USES = ("gpu", "api", "both")
USE_NAMES = {"gpu": "GPU", "api": "API", "both": "Both"}  # as the dialog and the pane say it
# Agents on GPU is the user's "nothing spends", for tasks already started too (#1213, 2026-11-24):
# only wb start read the switch, so an API task queued behind its provider's slots took one after
# the switch and spent its rounds and auto-fixes. Now no API task begins a round under it: wb
# start refuses an API --model, wb feedback, next and resume an API task's round, and the runner
# stops one before its next round (a round already running finishes)
AGENTS_GPU = "Agents is GPU, so nothing spends"
AGENTS_STOPS = "an API task already started stops before its next round"  # the change line's clause
AGENTS_BACK = "Ask the user: Agents back to Both or API (wb route all both)"
# When the GPU is busy (#1023, the owner's opt-in): routes.json `busy`, missing is "wait" (queue, or
# exit 4, as before); "api" starts a role that has an API model on it instead, at wb start only (a
# running task never moves, nor does anything go from API to the GPU). Agents GPU overrides it.
BUSY = ("wait", "api")
BUSY_LINE = "GPU busy: on the role's API model, as you set"  # wb start's line when it applies
THINKING = ("off", "on")
AUTO = "auto"  # the GPU model the router picks: today's behaviour, the default
BY = ("dialog", "pane", "terminal", "claude")  # who made a role's last change (`by`, `gpu_by`)
STAMPS = ("said", "by", "ts", "gpu_by", "gpu_ts")  # a role's record of its changes (stamp)
CAP_OFF = "off"  # `wb route cap off`: no day cap (#1023), refused while anything can spend
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
        # an OpenRouter role's backends (wb route --order, #1021): the keyhole routes every
        # request to them alone, and the start's where-note names them
        if e.get("order") is not None:
            order = e["order"]
            if not isinstance(order, list) or not all(isinstance(b, str) and b.strip() for b in order):
                raise RoutesError(f"{r}'s order is {order!r}, not a list of backend names")
            row["order"] = list(order)
        for key in STAMPS:
            if key in e:
                row[key] = e[key]
        out[r] = row
    use = doc.get("use", "both")
    if use not in USES:
        raise RoutesError(f"use is {use!r}, not {', '.join(USES[:-1])} or {USES[-1]}")
    busy = doc.get("busy", "wait")
    if busy not in BUSY:
        raise RoutesError(f"busy is {busy!r}, not {' or '.join(BUSY)}")
    return {"v": 1, "use": use, "busy": busy, "day_usd": _cap(doc.get("day_usd")), "roles": out}


def effective_via(doc: dict, role: str) -> str:
    """Where ``role``'s tasks run: the Agents switch (`use`) unless it is Both, else the role's own
    `via`. Everything that decides GPU or API for a role asks this, never `via` (#1016)."""
    use = doc.get("use", "both")
    return use if use != "both" else doc["roles"][role]["via"]


def agents_gpu() -> bool:
    """Whether the Agents switch is GPU now, so no API task may begin a round (#1213). False for
    a routes.json that can't be read: wb start refuses that file, and a task already started
    goes on rather than stop on a hand edit's typo."""
    try:
        return read()["use"] == "gpu"
    except RoutesError:
        return False


def busy_api(doc: dict, role: str) -> str | None:
    """The API model ``role`` starts on while the GPU is busy (#1023): its own API model, when
    the user opted in (`busy` api), Agents isn't GPU and the role runs on the GPU; else None."""
    if doc.get("busy") != "api" or doc.get("use") == "gpu" or effective_via(doc, role) != "gpu":
        return None
    return doc["roles"][role].get("api") or None


def busy_api_any(doc: dict) -> bool:
    """Whether the busy opt-in can send any role to API now (busy_api for some role)."""
    return any(busy_api(doc, r) for r in ROLES)


def _busy_newly(doc: dict, new: dict) -> list:
    """The roles the busy opt-in can newly send to an API model (busy_api), one they didn't
    already run on: what a change opens to spending while `busy` stays api."""
    return [r for r in ROLES if (mid := busy_api(new, r)) and _sent(new, r, mid) != _sent(doc, r, busy_api(doc, r))
            and not (effective_via(doc, r) == "api" and _sent(doc, r, doc["roles"][r].get("api")) == _sent(new, r, mid))]


def _sent(doc: dict, role: str, mid: str | None) -> tuple:
    """Where ``role``'s code goes on ``mid``: the model and, for an OpenRouter one, its backends
    (`order`, #1021), which the keyhole routes every request by; new backends are a new place
    (wave 2's merge with #1023: a busy role's order change read as nothing new)."""
    order = doc["roles"][role].get("order") if mid and mid.split("/", 1)[0] == OPENROUTER else None
    return mid, tuple(order or ())


def _thinking_on(roles: list, doc: dict) -> list:
    """`thinking on (trivial)`, for those of ``roles`` (the ones a change newly sends to an API
    model) whose thinking is on, as a clause list ([] for none). Thinking can be turned on with no
    dialog while a role's API side is dormant (on the GPU, or no API model yet): the yes that
    later sends its code there must name it, or API tasks bill reasoning tokens nobody agreed to
    (#1177, 2026-11-24). Off, the default, costs nothing and goes unsaid."""
    on = [r for r in roles if doc["roles"][r]["thinking"] == "on"]
    return [f"thinking on ({', '.join(on)})"] if on else []


def busy_routed(doc: dict, role: str, m) -> dict:
    """task.json's routing keys for a start the busy opt-in sent to ``m``, the role's API model:
    as routes.resolve's for a role on API, with `busy` (#1023)."""
    from . import models
    row = doc["roles"][role]
    return {"via": "api", "role": role, "provider": m.provider, "provider_name": models.provider_name(m),
            "thinking": row["thinking"], "said": row.get("said") or "", "said_ts": row.get("ts"), "busy": True,
            # an OpenRouter role's backends, as resolve's: the keyhole routes by them (wave 2's merge)
            **({"order": list(row["order"])} if row.get("order") and m.provider == OPENROUTER else {})}


def _busy_part(doc: dict, new: dict, cap) -> tuple[str, bool]:
    """The busy row's part of the change line: ('', False) when it is as it was; to api takes the
    user's yes (it can spend), and a day cap first (NO_CAP); back to wait none."""
    was, now = doc.get("busy", "wait"), new.get("busy", "wait")
    if was == now:
        return "", False
    if now == "wait":
        return "When the GPU is busy: the role's API model → wait (queue, as before)", False
    if cap is None:
        raise RoutesError(NO_CAP)
    has = [r for r in ROLES if new["roles"][r].get("api")]
    what = ([f"a role with an API model ({', '.join(has)}) starts on it while the GPU is busy",
             *_thinking_on(has, new)]
            if has else ["no role has an API model yet, so nothing changes until one does"])
    return f"When the GPU is busy: wait → the role's API model ({'; '.join(what)}; cap {money(cap)}/day)", True


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


def _role_part(r: str, old: dict, row: dict, cap, who: Callable[[str], str],
               new: dict) -> tuple[list, bool]:
    """``r``'s clauses of the change line and whether they take the user's yes. ``new``: the
    document after the change (its `use` and `busy`), so what the role's code does is judged by
    where it actually goes: with Agents on API, or the busy opt-in on, a role whose own `via` is
    gpu still runs on its API model, and thinking turned on there billed reasoning tokens with no
    dialog (#1149, 2026-11-24)."""
    if new.get("use") == "api" and old["via"] != row["via"]:
        # Agents on API holds over only the role's own side (#1016): its API model, thinking and
        # backends are what its next start runs on, so they are said as if the role kept its side,
        # and the side as held (_held's `the move of ...`). Said as one held move, a new model
        # read as waiting for Both while the next start spent on it (#1236, 2026-11-24)
        clauses, _ = _role_part(r, old, {**row, "via": old["via"]}, cap, who, new)
        return [*clauses, f"{_side(old)} → {_side(row)}"], True
    clauses, gated = [], False
    on_api = row["via"] == "api"
    after = {**new, "roles": {**new["roles"], r: row}}
    sends = bool(row.get("api")) and (effective_via(after, r) == "api" or bool(busy_api(after, r)))
    moved = old["via"] != row["via"] or (on_api and old.get("api") != row.get("api"))
    if moved:
        gated = True
        clauses.append(f"{_side(old)} → {_side(row)}")
        if on_api:
            clauses += [f"your code goes to {who(row['api'])}", f"thinking {row['thinking']}"]
            if row.get("order"):  # an OpenRouter role's backends (#1021): part of what is agreed to
                clauses.append(f"backends {', '.join(row['order'])}")
            if cap is not None:
                clauses.append(f"cap {money(cap)}/day")
        elif row.get("api"):
            clauses.append(f"API kept: {row['api']}")
        if not on_api and new.get("use", "both") == "both":
            # the role's own move keeps its started tasks where they are, as a thinking change
            # does; only Agents on GPU stops them (#1213): said, so the move isn't read as a stop
            clauses.append("its tasks already started keep their model")
    elif old.get("api") != row.get("api"):  # set while on the GPU: used by wb route <role> api
        gated = True
        # under Agents on API the role already runs there: the new model is the next start's
        now = effective_via(after, r) == "api"
        clauses.append(f"API model {old.get('api') or 'none'} → {row['api']} ({who(row['api'])}), "
                       + ("used from the next start" if now else "used once it goes to API"))
        if now and old["thinking"] == row["thinking"] == "on":  # else the thinking clause says it
            clauses.append("thinking on")  # turned on with no dialog before it had a model (#1177)
    if old["gpu"] != row["gpu"] and not (moved and not on_api):
        clauses.append(f"GPU model {_gpu_name(old['gpu'])} → {_gpu_name(row['gpu'])}")
    if old["thinking"] != row["thinking"] and not (moved and on_api):
        clauses.append(f"thinking {old['thinking']} → {row['thinking']}")
        gated |= on_api or sends
    if row.get("order") and old.get("order") != row["order"] and not (moved and on_api):
        clauses.append(f"backends {', '.join(old.get('order') or ['none'])} → {', '.join(row['order'])}")
        gated = True  # where the code goes, within OpenRouter (#1021)
    return clauses, gated


def _goes_to(roles: list, doc: dict, who: Callable[[str], str]) -> str:
    """`your code goes to DeepSeek (trivial), Anthropic (significant, writing)`: by provider."""
    by: dict[str, list] = {}
    for r in roles:
        by.setdefault(who(doc["roles"][r]["api"]), []).append(r)
    return "your code goes to " + ", ".join(f"{p} ({', '.join(rs)})" for p, rs in by.items())


def _agents_part(doc: dict, new: dict, cap, who: Callable[[str], str]) -> tuple[str, bool, list]:
    """The Agents switch's part of the change line (#1016): ('', False, []) when `use` is as it
    was; else the clause, whether it takes the user's yes (to API, or to Both when that puts a
    role back on its API model; never to GPU) and the roles whose side it moves."""
    was, use = doc.get("use", "both"), new["use"]
    if was == use:
        return "", False, []
    moved = [r for r in ROLES if effective_via(doc, r) != effective_via(new, r)]
    to_api = [r for r in moved if effective_via(new, r) == "api" and new["roles"][r].get("api")]
    if use == "gpu":
        what = [f"every role on the GPU; {AGENTS_STOPS}; each role's own choice is kept for Both"]
    elif use == "api":
        what = ["every role on its API model"]
        if none := [r for r in ROLES if not new["roles"][r].get("api")]:
            what.append(f"{', '.join(none)} {'has' if len(none) == 1 else 'have'} no API model and "
                        "can't start")
    else:
        what = ["each role as set: " + ", ".join(f"{r} {_side(new['roles'][r])}" for r in ROLES)]
    if to_api:
        what += [_goes_to(to_api, new, who), *_thinking_on(to_api, new)]
        if cap is not None:
            what.append(f"cap {money(cap)}/day")
    # to Both only when it newly puts a role on API (from GPU): from API it sends nothing new
    gated = use == "api" or bool(to_api)
    return f"Agents: {USE_NAMES[was]} → {USE_NAMES[use]} ({'; '.join(what)})", gated, moved


def _held(doc: dict, new: dict, roles: list, chose: bool) -> str:
    """What the Agents switch holds over of ``roles``' own change, '' for none; ``chose``: the
    change set a side or an API model. Agents on GPU holds all of it (a role's API side is
    dormant there); Agents on API only the roles' moves between GPU and API, as their next start
    runs on their API model already (#1236, 2026-11-24)."""
    use = new.get("use", "both")
    if use == "gpu" and roles and chose:
        return "Agents is GPU, so this applies once Agents is Both (wb route all both)"
    moved = [r for r in roles if doc["roles"][r]["via"] != new["roles"][r]["via"]] if use == "api" else []
    if not moved:
        return ""
    to = ", ".join(f"{r} to {'API' if new['roles'][r]['via'] == 'api' else 'the GPU'}" for r in moved)
    return (f"Agents is API, so the move{'s' if len(moved) > 1 else ''} of {to} "
            f"appl{'y' if len(moved) > 1 else 'ies'} once Agents is Both (wb route all both)")


def change(doc: dict, role: str | None, via: str | None = None, gpu: str | None = None,
           api: str | None = None, thinking: str | None = None, day_usd=None,
           who: Callable[[str], str] = lambda mid: mid.split("/", 1)[0], use: str | None = None,
           order: list[str] | None = None, busy: str | None = None) -> Change:
    """What a `wb route` does to ``doc``: ``role`` one of ROLES, "all" (``use``, the Agents
    switch, and ``thinking`` only) or None (the cap alone). ``who``: an API id's provider, as the
    user knows it (the catalog's name). Never writes. RoutesError for a change that can't be
    made: `api` with no API model kept, and the first API role with no cap (NO_CAP). ``order``:
    an OpenRouter role's backends (#1021), dropped when the role's API model is another
    provider's."""
    new = copy.deepcopy(doc)
    new.setdefault("use", "both")
    if day_usd is not None:
        new["day_usd"] = None if day_usd == CAP_OFF else _cap(day_usd)
    if use is not None:
        if use not in USES:
            raise RoutesError(f"Agents is gpu, api or both, not {use!r}")
        new["use"] = use
    if busy is not None:
        if busy not in BUSY:
            raise RoutesError(f"busy is wait or api, not {busy!r}")
        new["busy"] = busy
    cap = new["day_usd"]
    parts, gated, changed = [], False, []
    for r in (ROLES if role == "all" else (role,) if role else ()):
        old = doc["roles"][r]
        row = dict(old)
        if api is not None:
            row["api"] = api
        if gpu is not None:
            row["gpu"] = gpu
        if thinking is not None:
            row["thinking"] = thinking
        if order is not None:
            row["order"] = list(order)
        if row.get("order") and str(row.get("api") or "").split("/", 1)[0] != OPENROUTER:
            row.pop("order")
        if via == "api" and not row.get("api"):
            raise RoutesError(f"{r} has no API model yet: wb route {r} <provider/model>")
        if via is not None:
            row["via"] = via
        clauses, g = _role_part(r, old, row, cap, who, new)
        if not clauses:
            continue
        parts.append(f"{r}: " + " · ".join(clauses))
        gated |= g
        changed.append(r)
        new["roles"][r] = row  # its stamps kept: stamp() renews the side this change touched
    if changed and role != "all" and (held := _held(doc, new, changed, via is not None or api is not None)):
        parts.append(held)  # the role's own choice, which the Agents switch holds over for now
    agents, g, moved = _agents_part(doc, new, cap, who)
    if agents:
        parts.insert(0, agents)
        gated |= g
        changed += [r for r in moved if r not in changed]
    # a role newly sent to an API model, by its own change or the switch
    to_api = any(effective_via(new, r) == "api" and new["roles"][r].get("api")
                 and (effective_via(doc, r) != "api" or doc["roles"][r].get("api") != new["roles"][r].get("api"))
                 for r in ROLES)
    if to_api and cap is None:
        raise RoutesError(NO_CAP)
    busy_line, g = _busy_part(doc, new, cap)
    if busy_line:
        parts.append(busy_line)
        gated |= g
    elif busy_now := _busy_newly(doc, new):
        # with the opt-in on, a GPU role given an API model, or Agents back from GPU, can now send
        # code to API while the GPU is busy: the user's yes and a cap, as any role newly on API
        # (2026-11-23 review: `wb route all both` re-enabled it with no dialog, and a role's API
        # model set after `cap off` spent with no cap)
        if cap is None:
            raise RoutesError(NO_CAP)
        parts.append(f"when the GPU is busy, {', '.join(busy_now)} {'starts' if len(busy_now) == 1 else 'start'} "
                     f"on {'its' if len(busy_now) == 1 else 'their'} API model "
                     f"({'; '.join([_goes_to(busy_now, new, who), *_thinking_on(busy_now, new)])}; "
                     f"cap {money(cap)}/day)")
        gated = True
    if cap is None and doc["day_usd"] is not None:  # `wb route cap off` (#1023): never under an API role
        if spends := [r for r in ROLES if effective_via(new, r) == "api"] + (["busy"] if busy_api_any(new) else []):
            raise RoutesError(f"the day cap stays while {', '.join(spends)} can spend on API: put "
                              f"{'it' if len(spends) == 1 else 'them'} on the GPU (or wb route busy wait) first")
        parts.append(f"day cap {money(doc['day_usd'])} → none")
        gated = True
    elif cap != doc["day_usd"]:
        gated = True
        if doc["day_usd"] is not None or not to_api:  # else the role's "cap $N/day" said it
            parts.append(f"day cap {money(doc['day_usd']) if doc['day_usd'] is not None else 'none'}"
                         f" → {money(cap)}/day")
    if not changed and not agents and not busy_line and cap == doc["day_usd"]:
        return Change(new, "", False, [])
    return Change(new, "; ".join(parts), gated, changed)


def change_pairs(doc: dict, pairs: list, day_usd=None,
                 who: Callable[[str], str] = lambda mid: mid.split("/", 1)[0]) -> Change:
    """Several roles in one change, one line and one yes (#1024: the first run's question):
    ``pairs`` [(role, via, api)] each as change() takes them, ``day_usd`` the cap for all. All or
    nothing: RoutesError for any pair change() refuses. The line: each role's move, then where
    the code goes once per provider, thinking and the cap once: `trivial: GPU → API a ·
    significant: GPU → API b · your code goes to DeepSeek · thinking off · cap $5/day`."""
    new, roles, gated = doc, [], False
    for i, (r, via, api) in enumerate(pairs):
        ch = change(new, r, via=via, api=api, day_usd=day_usd if i == 0 else None, who=who)
        new, gated = ch.doc, gated or ch.gated
        roles += [x for x in ch.roles if x not in roles]
    cap = new["day_usd"]
    if not roles and cap == doc["day_usd"]:
        return Change(new, "", False, [])
    parts, sent = [], []
    for r in roles:
        old, row = doc["roles"][r], new["roles"][r]
        clauses, _ = _role_part(r, old, row, cap, who, new)
        if row["via"] == "api" and (old["via"] != "api" or old.get("api") != row.get("api")):
            sent.append(r)  # said once for all below, not per role
            once = {f"your code goes to {who(row['api'])}", f"thinking {row['thinking']}"}
            clauses = [c for c in clauses if c not in once and c != f"cap {money(cap)}/day"]
        parts.append(f"{r}: " + " · ".join(clauses))
    if sent:
        by: dict[str, list] = {}
        for r in sent:
            by.setdefault(who(new["roles"][r]["api"]), []).append(r)
        parts.append(f"your code goes to {next(iter(by))}" if len(by) == 1 else _goes_to(sent, new, who))
        think = {new["roles"][r]["thinking"] for r in sent}
        parts.append(f"thinking {think.pop()}" if len(think) == 1 else
                     "thinking " + ", ".join(f"{new['roles'][r]['thinking']} ({r})" for r in sent))
        if cap is not None:
            parts.append(f"cap {money(cap)}/day")
    if cap != doc["day_usd"] and (doc["day_usd"] is not None or not sent):
        parts.append(f"day cap {money(doc['day_usd']) if doc['day_usd'] is not None else 'none'} → {money(cap)}/day")
    if held := _held(doc, new, roles, True):
        parts.append(held)
    return Change(new, " · ".join(parts), gated, roles)


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
        # an OpenRouter role's backends (`order`, #1021) are where its code goes: left out, a
        # change of them alone reached the conversation as nothing (#1126, 2026-11-23)
        backends = f", backends {', '.join(row['order'])}" if row.get("api") and row.get("order") else ""
        if row["via"] == "api":
            out.append(f"{r} API {row['api']}{backends}, thinking {row['thinking']}")
            continue
        text = f"{r} GPU " + ("(the router picks)" if row["gpu"] == AUTO else row["gpu"])
        if row.get("api"):
            text += f", API kept: {row['api']}{backends}"
        if row["thinking"] == "on":
            text += ", thinking on"
        out.append(text)
    use = doc.get("use", "both")
    head = f"Agents {USE_NAMES[use]}, over each role's own: " if use != "both" else ""
    tail = "; when the GPU is busy, a role with an API model runs on it" if doc.get("busy") == "api" else ""
    return head + "; ".join(out) + tail


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


# wb start --role (#1016): a task's role resolved to its model, GPU or API, never the other side
class Refused(RoutesError):
    """wb start can't run the task as asked: the one line, and its exit code (3; 1 for a
    `--model` that names no model, as before roles)."""

    def __init__(self, msg: str, code: int = 3):
        super().__init__(msg)
        self.code = code


def role_and_size(role: str | None, size: str | None) -> tuple[str, str]:
    """wb start's role and size, either given alone: no --role, the size's name (trivial, else
    significant); no --size, trivial for the trivial role, else significant (writing slices like
    significant). Both given: the role picks the model, the size the slice and the GPU router's
    size."""
    if role is None:
        role = "trivial" if size == "trivial" else "significant"
    if size is None:
        size = "trivial" if role == "trivial" else "significant"
    return role, size


def no_api_model(role: str) -> str:
    return (f"{role} role has no API model, and Agents is set to API: ask the user to pick one (wb route "
            f"{role} --api <id>) or set Agents to Both or GPU (wb route all both|gpu)")


def off_api(role: str | None, doc: dict | None = None) -> tuple[str, str, str] | None:
    """How the user keeps ``role``'s next starts off API, from routes.json as it is now (``doc``,
    else read): (noun, verb, command), as `the role on the GPU`, `put the role on the GPU`,
    `wb route <role> gpu`. With Agents on API that is the Agents switch (Both only when the role's
    own side is the GPU and no busy opt-in sends it back); a role on the GPU that the busy opt-in
    sends to API, `wb route busy wait`; None when its next starts are off API already. Every
    refusal and notice that offers the GPU asks this: `wb route <role> gpu` under Agents API, or for
    a busy opt-in's task, changed nothing, and the retry was refused with the same line (#1218,
    2026-11-24). No role, or routes.json unreadable: the role's own command, as before."""
    r = role or "<role>"
    plain = ("the role on the GPU", f"put {r} on the GPU", f"wb route {r} gpu")
    if role is None:
        return plain
    if doc is None:
        try:
            doc = read()
        except RoutesError:
            return plain
    if role not in doc["roles"]:
        return plain
    if effective_via(doc, role) == "api":
        if doc.get("use") != "api":
            return plain
        if doc["roles"][role]["via"] == "gpu" and doc.get("busy") != "api":
            return "Agents on Both or GPU", "set Agents to Both or GPU", "wb route all both|gpu"
        return "Agents on GPU", "set Agents to GPU", "wb route all gpu"
    if busy_api(doc, role):
        return "waiting for a busy GPU", "have starts wait for a busy GPU", "wb route busy wait"
    return None


def cant_run(role: str, mid: str, why: str, fix: str, doc: dict) -> str:
    """The one line for a role whose API model can't run: never another model (#1016)."""
    off = off_api(role, doc) or off_api(None)
    other = (f"{off[1]} ({off[2]})" if doc.get("use") == "api"
             else f"put the role on the GPU (wb route {role} gpu)")
    said = fix and (fix in why or any(w in why for w in ("/connect", "top it up")))  # its own remedy
    return (f"{role} role: {mid} can't run: {why}" + (f" ({fix})" if fix and not said else "")
            + f". Ask the user: fix that, or {other}")


def said_part(routed: dict) -> str:
    """Why an API task runs where it does, as the start line and `routed` say it: `trivial role,
    set by you 11-20` (only the user sets a role: the dialog, the pane or a terminal), or `asked
    for, approved in the dialog` for a gated --model."""
    said = routed.get("said") or ""
    if said.startswith("asked for"):
        return said
    out = f"{routed.get('role')} role"
    if isinstance(ts := routed.get("said_ts"), (int, float)):
        out += f", set by you {time.strftime('%m-%d', time.localtime(ts))}"
    return out


def routed_line(model_id: str, routed: dict) -> str:
    """An API task's routing in one line (task.json's keys): `API <id> (<provider>) · <why> ·
    thinking off`. The start line, task.json `route` and wb watch's `routed` say it."""
    return (f"API {model_id} ({routed.get('provider_name') or routed.get('provider')}) · "
            f"{said_part(routed)} · thinking {routed.get('thinking') or 'off'}")


def model_line(model_id: str, routed: dict) -> str:
    """wb start's model line for an API task: `model: <id> · API (<provider>) · trivial role, set by
    you 11-20 · thinking off`, which wb start ends with meter.tail (today's spend); a GPU task's
    line is as before roles."""
    return (f"model: {model_id} · API ({routed.get('provider_name') or routed.get('provider')}) · "
            f"{said_part(routed)} · thinking {routed.get('thinking') or 'off'}")


def resolve(role: str, size: str, wanted: str | None, stack=(), name: str | None = None,
            gate: Callable | None = None, tokens: int | None = None) -> tuple:
    """wb start's model: (model, state, why, routed), `routed` the task.json keys (`via`, `role`;
    for API `provider`, `provider_name`, `thinking`, `said`, `said_ts`). Refused, with the one
    line, for what can't run as asked. In order: routes.json must read (never as all GPU); a
    `--model` is taken as asked (an API one through ``gate``; the user's yes is wb start's, last,
    #1104); the role's side
    (effective_via): the GPU, its pin or the router's pick (router.choose as before roles), or
    the role's API model by its exact id. No fallback in either direction: a GPU role with no
    GPU model never tries its stored API model, and an API role that can't run never the GPU.
    Either API path meets meter.refusal (no price, or today at the cap, #1019, or within one
    request at its worst of it, #1150, its input ``tokens``: round 1's, runner.first_tokens,
    #1173; None, a fresh session's least) first: the `--model` one before the dialog, the role's
    (agreed to in `wb route`'s) before it starts."""
    from . import meter, models, router  # they import more than routes needs at its import
    first = meter.FIRST_TOKENS if tokens is None else tokens
    try:
        doc = read()
    except RoutesError as e:
        raise Refused(unreadable(e)) from None
    if wanted:
        try:
            m = models.resolve(wanted)
        except KeyError as e:
            raise Refused(str(e).strip('"'), 1) from None
        if m.kind != "api":
            return m, models.status(m), "asked for", {"via": "gpu", "role": role}
        if doc["use"] == "gpu":  # before the dialog: the runner would stop it at once (#1213)
            raise Refused(f"{AGENTS_GPU}: {m.id} is an API model. {AGENTS_BACK}, or a GPU model")
        if gate is not None:  # API_RUNS: the dialog's yes is wb start's own, last (#1104)
            gate(m)
        if why := meter.refusal(m, tokens=first):  # before the dialog: never ask for a start that is refused
            raise Refused(why)
        approved = os.environ.get("CLAUDECODE") == "1"
        return m, models.status(m), "asked for", {
            "via": "api", "role": role, "provider": m.provider, "provider_name": models.provider_name(m),
            "thinking": "off", "said": "asked for" + (", approved in the dialog" if approved else "")}
    row = doc["roles"][role]
    if effective_via(doc, role) == "gpu":
        if row["gpu"] != AUTO:  # the user's pin: that model, never the router's other pick
            # by its exact id, as wb route stored it and its listing finds it: models.resolve's
            # fragment match ran a retired qwen3:8b pin's role on qwen3:8b-q8_0, said as the pin,
            # while wb route said not found (#1214, 2026-11-24)
            found = models.discover()
            m = found.get(row["gpu"])
            if m is None:
                known = ", ".join(found) or f"none ({models.why_none()})"
                raise Refused(f"{role} role: its GPU model {row['gpu']} isn't found (not in OpenCode's config "
                              f"now; known: {known}). Ask the user: fix that, or let the router pick "
                              f"(wb route {role} --gpu auto)")
            return m, models.status(m), f"the {role} role's GPU model (wb route)", {"via": "gpu", "role": role}
        # the writing role ranks on its own record (#1025), a coding role on its size's: choose
        # reads that from the role, which also names it in the refusal's API choice (#1024)
        top, why = router.choose(size, stack, name, role=role)
        if top is None:
            raise Refused(why)
        return top.model, top.state, why, {"via": "gpu", "role": role}
    mid = row.get("api")
    if not mid:  # only with Agents on API: a role on API of its own has one (normalize)
        raise Refused(no_api_model(role))
    m = models.lookup(mid)
    if m is None or m.kind != "api":
        failed = models.hosted_error()
        raise Refused(cant_run(role, mid, "not in OpenCode's catalog now" + (
            f" (its read failed: {failed})" if failed else ""), "", doc))
    # a role on an unpriced model ran with no cap at all once #1016's path skipped this (wave 2's review)
    if why := meter.refusal(m, role, tokens=first):
        raise Refused(why)
    routed = {"via": "api", "role": role, "provider": m.provider, "provider_name": models.provider_name(m),
              "thinking": row["thinking"], "said": row.get("said") or "", "said_ts": row.get("ts")}
    # an OpenRouter role's backends (#1021): task.json's `order`, which the keyhole routes every
    # request by; the user agreed to them in the dialog, and once they never reached the task
    if row.get("order") and m.provider == OPENROUTER:
        routed["order"] = list(row["order"])
    return m, models.status(m), routed_line(m.id, routed), routed
