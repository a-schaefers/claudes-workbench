"""Roles: which model a kind of work goes to (#1014).

Three fixed roles, `significant` (complex coding), `trivial` and `writing` (prose), each with a
selector (`via`: gpu or api), its GPU model (`gpu`: an id, or `auto`, the router's pick), its API
model (`api`: an exact OpenCode id, kept while the role is on the GPU), `thinking` (off or
on) and `thinking_review` (on: an API task's self-review round alone thinks, #1059). Kept in
~/.config/workbench/routes.json (`$WB_ROUTES`), with one day cap (`day_usd`). A missing file or
role is the GPU, exactly as before roles; a file that can't be read is an error, never "all GPU":
that would move the user off their API choice without a word.

Claude never changes the API side on its own: under Claude Code (CLAUDECODE=1), an OpenCode
driver (#1309) or in an agent's shell (WB_TASK, #1310; store.under_model), `wb route` applies such
a change only when the host's dialog approved exactly it (`approved`, route-ok.json in
$WB_HOME, one-shot, fresh for ROUTE_OK_SECS). A GPU model pin needs no dialog; the mod echoes
every change as a [workbench] row. A role's `said`/`by`/`ts` record its last gated change (who
sent its code where), `gpu_by`/`gpu_ts` its last change that needed no yes: a pin never rewrites
the record of the user's yes (2026-11-23 review). The change line is the dialog's text: the mod shows what
`wb route --dry-run --json` prints, so both come from `change` here (Couplings).

A role's `fallback` (#1196, the user's own, none by default): an exact API id on another provider,
or `gpu` (the role's GPU side), which a task of the role on its API model moves to when that
provider is down or out of credit: at wb start, and once in the runner. Claude never sets one.

The top-level `driver` (#1316, the OpenCode path): an optional standing default for the driver,
the model with the final say, which the plugin's /workbench applies to its session; `session`
(no key) leaves each session's own /models choice. `wb route driver` sets it (driver_change)."""
from __future__ import annotations

import copy
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import store
from .privacy import FREE_LINE, OPENROUTER  # OPENROUTER: the provider whose roles keep an `order` (#1021)
# An OpenRouter role with no `order`: OpenRouter picks the backend by its own and the user's
# account settings, no fallbacks (keyhole.route_body). Said in the dialog, never refused: wb
# once refused it until Claude asked the user which backends, before a dialog that asks anyway.
OPENROUTER_ANY = "via OpenRouter to its choice of backend"

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
# When the GPU is busy (#1023, the owner's opt-in): routes.json `busy`, missing is "wait" (queue, as
# before); "api" starts a role that has an API model on it instead, at wb start only (a
# running task never moves, nor does anything go from API to the GPU, but by the role's fallback,
# #1196). Agents GPU overrides it.
BUSY = ("wait", "api")
BUSY_LINE = "GPU busy: on the role's API model, as you set"  # wb start's line when it applies
THINKING = ("off", "on")
# A role's `thinking_review` on (#1059, off by default): with its thinking off, an API task runs the
# self-review round #1017 turned off for API, that round alone on the model's thinking variant
# (runner.review_variant). How every view says it; the mod's REVIEW_ONLY the same (Couplings)
REVIEW_ONLY = "thinking: review only"
AUTO = "auto"  # the GPU model the router picks: today's behaviour, the default
# who made a role's last change (`by`, `gpu_by`): `opencode`, an OpenCode driver's (#1309);
# `agent`, an agent's own call on the dialog's yes (#1310)
BY = ("dialog", "pane", "terminal", "claude", "web", "opencode", "agent")
STAMPS = ("said", "by", "ts", "gpu_by", "gpu_ts")  # a role's record of its changes (stamp)
CAP_OFF = "off"  # `wb route cap off`: no day cap (#1023), refused while anything can spend
DAY_USD_SUGGEST = 5  # the cap the first API role takes unless --day-usd gives one (#1322)
ROUTE_OK = "route-ok.json"  # $WB_HOME/route-ok.json: the mod's dialog approved this one change
ROUTE_OK_SECS = 120  # the dialog's yes is for the call it let through, run at once, as SEND_OK_SECS
NO_CAP = (f"no day cap yet: ask the user for one (suggest ${DAY_USD_SUGGEST}), then add "
          f"--day-usd {DAY_USD_SUGGEST}")
# A role's fallback (#1196): `wb route <role> fallback <id>|gpu|none`. `gpu` is the role's GPU side
# (its pin, else the router's pick): it spends nothing and sends nothing off the machine, so it
# needs no yes; an API id does, and a day cap. None is set by default, and Claude never sets one.
FALLBACK_GPU = "gpu"
FALLBACK_OFF = "none"
FALLBACK_LINE = "on the role's fallback, as you set"  # wb start's line and the move's (#1196)
# The driver (the model with the final say) on the OpenCode path (#1316): it is whatever the
# session runs on, picked in OpenCode's own /models, and wb doesn't own the choice. The top-level
# `driver` is an optional standing default the plugin's /workbench applies to the session
# (switchModel): an exact OpenCode id, `#variant` allowed, or `session` (the default: no pin,
# stored as no key). Not a fourth role: ROLES say where tasks go, and nothing routes a task here.
DRIVER_SESSION = "session"


def driver_ref(text) -> tuple[str, str, str] | None:
    """An OpenCode model reference `provider/id#variant` as OpenCode parses it (schema/src/model.ts
    `Model.Ref.parse`: the provider ends at the first `/`, the variant starts at the first `#`
    after it): (provider, id, variant or ""), or None when it isn't one."""
    if not isinstance(text, str) or text != text.strip() or any(c.isspace() for c in text):
        return None
    provider, slash, rest = text.partition("/")
    mid, hash_, variant = rest.partition("#")
    if not provider or not slash or "#" in provider or not mid or (hash_ and (not variant or "#" in variant)):
        return None
    return provider, mid, variant


class RoutesError(RuntimeError):
    """routes.json can't be read, or a change can't be made; the message says why."""


class _NoCap(RoutesError):
    """A change that can spend with no day cap: change() makes it again with DAY_USD_SUGGEST as
    its cap, said in its one line (#1322), unless the change itself took the cap off."""

    def __init__(self):
        super().__init__(NO_CAP)


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
        for key, allowed in (("via", VIAS), ("thinking", THINKING), ("thinking_review", THINKING)):
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
        # request to them alone
        if e.get("order") is not None:
            order = e["order"]
            if not isinstance(order, list) or not all(isinstance(b, str) and b.strip() for b in order):
                raise RoutesError(f"{r}'s order is {order!r}, not a list of backend names")
            row["order"] = list(order)
        # the role's fallback (#1196): an API id, or gpu (the role's GPU side)
        if e.get("fallback") is not None:
            if not isinstance(e["fallback"], str) or not e["fallback"].strip() or e["fallback"] == FALLBACK_OFF:
                raise RoutesError(f"{r}'s fallback is {e['fallback']!r}, not an API model id or gpu")
            row["fallback"] = e["fallback"]
        for key in STAMPS:
            if key in e:
                row[key] = e[key]
        # what its API model does with thinking (with_thinks, #1263): wb's own record, read
        # again on every change, so a malformed one is left out rather than refused
        t = e.get("thinks")
        if isinstance(t, dict) and all(isinstance(t.get(k), str) for k in ("api", "off", "on")):
            row["thinks"] = {k: t[k] for k in ("api", "off", "on")}
        out[r] = row
    use = doc.get("use", "both")
    if use not in USES:
        raise RoutesError(f"use is {use!r}, not {', '.join(USES[:-1])} or {USES[-1]}")
    busy = doc.get("busy", "wait")
    if busy not in BUSY:
        raise RoutesError(f"busy is {busy!r}, not {' or '.join(BUSY)}")
    # the driver pin (#1316): kept only when it pins one, so a file without it reads as before.
    # One that isn't an OpenCode id reads as no pin rather than an unreadable file: refused, a
    # hand edit's typo made every wb start exit 3, Claude Code's too, while the mod's routesValid
    # (which never reads `driver`) went on telling Claude the roles (the review of #1316,
    # 2026-11-25). wb writes only a parsed one (driver_change).
    drv = doc.get("driver")
    pinned = {"driver": drv} if drv != DRIVER_SESSION and driver_ref(drv) is not None else {}
    return {"v": 1, "use": use, "busy": busy, "day_usd": _cap(doc.get("day_usd")), "roles": out, **pinned}


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


def fallback_of(doc: dict, role: str) -> str | None:
    """``role``'s fallback as it applies now (#1196): its `fallback`, an API id or FALLBACK_GPU;
    None when it has none, or it is `gpu` while Agents is API (every role on its API model). It
    applies only to a task on the role's own API model: never a `--model` one, nor one the busy
    opt-in sent there (the GPU, its own side, is what that one waits for)."""
    fb = doc["roles"][role].get("fallback")
    if not fb or (fb == FALLBACK_GPU and doc.get("use") == "api"):
        return None
    return fb


def fallback_name(fb: str, gpu: str = AUTO) -> str:
    """A fallback as the change line and the show say it: `GPU (the router's pick)`, `GPU (<id>)`,
    or the API id."""
    return f"GPU ({_gpu_name(gpu)})" if fb == FALLBACK_GPU else fb


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


def plain_thinks(mid: str, thinking: str) -> str:
    """change()'s ``thinks`` with no catalog to ask: the role's setting as it is."""
    return f"thinking {thinking}"


def thinking_of(m, thinking: str, review: str | None = None) -> str:
    """What API model ``m`` does with the role's ``thinking`` (off or on), as its task will run
    it: runner.thinking_variant's line (task.json `thinking_shown`). The dialog, the start line
    and task.json's route said the role's setting, `thinking off`, for a model that can't turn it
    off (Anthropic's adaptive ones with no `none`), so the user agreed to no reasoning and paid for
    it (#1237, 2026-11-24). ``review``: the role's `thinking_review` (#1059), REVIEW_ONLY's line
    where the self-review alone thinks."""
    from . import runner  # it imports far more than routes needs at its import, as resolve's
    return runner.review_variant(m, thinking, review)[1]


def model_thinks(m) -> dict:
    """A role's `thinks` record for API model ``m``: what it does with each thinking setting
    (thinking_of), and which model that was said of (`api`)."""
    return {"api": m.id, "off": thinking_of(m, "off"), "on": thinking_of(m, "on")}


def with_thinks(doc: dict, cached: bool = True) -> dict:
    """``doc`` with each role's `thinks` read again from the catalog (model_thinks) for its API
    model. wb writes it with every change (cli._route_apply) so a reader with no catalog, the
    mod's routesSummary, says what the model does (#1263). A model the catalog doesn't list now
    keeps a record said of it (`api` its id) and loses any other: hosted.json holds no models
    for DOWN_SECS after a failed read, and dropping the record then said `thinking off` again,
    and wrote that to routes.json with any change, one to another role too (#1263's review,
    2026-11-24); thinking_said reads a record only while its `api` is the role's."""
    from . import models
    for row in doc["roles"].values():
        m = models.lookup(row["api"], cached=cached) if row.get("api") else None
        if m is not None and m.kind == "api":
            row["thinks"] = model_thinks(m)
        elif m is not None or not row.get("api") or (row.get("thinks") or {}).get("api") != row["api"]:
            row.pop("thinks", None)
    return doc


def thinking_said(row: dict) -> str:
    """What a role's thinking does on its API model: its `thinks` record (with_thinks') for the
    setting, else the setting as it is. Every view of a role's thinking says this one line (the
    mod's routesSummary the same: Couplings): the roles listing, its summary, the pane and wb scout
    said `thinking off` for a model that can't turn it off after #1237 fixed the dialog and the
    start line, so the user kept a model that thinks and paid for it (#1263, 2026-11-24)."""
    t = row.get("thinks")

    def recorded(mid: str, thinking: str) -> str:
        if isinstance(t, dict) and t.get("api") == row.get("api") and isinstance(t.get(thinking), str):
            return t[thinking]
        return plain_thinks(mid, thinking)
    return role_thinks(row, recorded)


def role_thinks(row: dict, thinks: Callable[[str, str], str] = plain_thinks) -> str:
    """What ``row``'s API model does with its thinking as ``thinks`` says it, and with its
    `thinking_review` (#1059): REVIEW_ONLY (with the effort, `thinking: review only (effort
    high)`) when the coding rounds don't think and the thinking variant does, as
    runner.review_variant gives a task; else the coding rounds' line alone."""
    mid = row.get("api") or ""
    text = thinks(mid, row["thinking"])
    if row.get("thinking_review") == "on" and text == plain_thinks(mid, "off"):
        on = thinks(mid, "on")
        if on.startswith(plain_thinks(mid, "on")):
            return REVIEW_ONLY + on.removeprefix(plain_thinks(mid, "on"))
    return text


def _thinking_on(roles: list, doc: dict, thinks: Callable[[str, str], str] = plain_thinks) -> list:
    """`thinking on (trivial)`, for those of ``roles`` (the ones a change newly sends to an API
    model) that will think, as a clause list ([] for none): what ``thinks`` says the model does.
    Thinking can be turned on with no dialog while a role's API side is dormant (on the GPU, or no
    API model yet): the yes that later sends its code there must name it, or API tasks bill
    reasoning tokens nobody agreed to (#1177, 2026-11-24); so must a model that thinks with the
    role's off (#1237). Off, the default, costs nothing and goes unsaid."""
    said: dict[str, list] = {}
    for r in roles:
        row = doc["roles"][r]
        text = role_thinks(row, thinks)
        if not text.startswith("thinking off"):
            said.setdefault(text, []).append(r)
    return [f"{text} ({', '.join(rs)})" for text, rs in said.items()]


def _free_part(roles: list, doc: dict, free: Callable[[str], bool]) -> list:
    """`free: the provider may keep or train on your code (trivial)`, for those of ``roles`` (the
    ones a change newly sends to an API model) on a free model, as a clause list ([] for none):
    training on the code is what the user says yes to there (#1193), and the dialog's text is
    this line."""
    on = [r for r in roles if free(doc["roles"][r].get("api") or "")]
    return [f"{FREE_LINE} ({', '.join(on)})"] if on else []


def _priced(roles: list, doc: dict, free: Callable[[str], bool]) -> bool:
    """Whether any of ``roles`` runs on an API model that isn't free (or on none yet): the day
    cap is taken (DAY_USD_SUGGEST when none) only then, a free model having nothing for it to keep (#1193)."""
    return not roles or any(not free(doc["roles"][r].get("api") or "") for r in roles)


def busy_routed(doc: dict, role: str, m) -> dict:
    """task.json's routing keys for a start the busy opt-in sent to ``m``, the role's API model:
    as routes.resolve's for a role on API, with `busy` (#1023)."""
    from . import models
    row = doc["roles"][role]
    return {"via": "api", "role": role, "provider": m.provider, "provider_name": models.provider_name(m),
            "thinking": row["thinking"],
            "thinking_shown": thinking_of(m, row["thinking"], row.get("thinking_review")), **_review(row), "said": row.get("said") or "", "said_ts": row.get("ts"), "busy": True,
            # an OpenRouter role's backends, as resolve's: the keyhole routes by them (wave 2's merge)
            **({"order": list(row["order"])} if row.get("order") and m.provider == OPENROUTER else {})}


def fallback_routed(doc: dict, role: str, m, primary: str) -> dict:
    """task.json's routing keys for a task on ``m``, the role's fallback (#1196), in place of its
    API model ``primary``: as routes.resolve's for a role on API (on the GPU `via` and `role`),
    with `fallback` and `fallback_from`."""
    from . import models
    if m.kind != "api":
        return {"via": "gpu", "role": role, "fallback": True, "fallback_from": primary}
    row = doc["roles"][role]
    return {"via": "api", "role": role, "provider": m.provider, "provider_name": models.provider_name(m),
            "thinking": row["thinking"], **_review(row), "said": row.get("said") or "", "said_ts": row.get("ts"),
            "fallback": True, "fallback_from": primary}


def _review(row: dict) -> dict:
    """task.json's `thinking_review` for a task of role ``row`` on an API model (#1059)."""
    return {"thinking_review": "on"} if row.get("thinking_review") == "on" else {}


def fallback_pick(doc: dict, role: str, size: str, stack, name: str | None, primary, tokens: int | None = None,
                  local: str = "") -> tuple | str:
    """The role's fallback for a task whose API model ``primary`` can't take it now (#1196):
    (model, state, why, routed), or the line saying why the fallback can't either ("" when the role
    has none). ``local``: why the task's code must stay on this machine (a local-only spot), which
    refuses an API fallback. An API fallback must be in the catalog, of kind api, on another
    provider than ``primary``'s, able to run now and within the day cap (meter.refusal, with the
    first request's input ``tokens``); `gpu` is the role's pin, else the router's pick, and must
    be able to run. Shared by wb start (cli._fallback_start) and the runner (runner._fallback_move)."""
    from . import meter, models, router  # they import more than routes needs at its import
    fb = fallback_of(doc, role)
    if not fb:
        return ""
    row = doc["roles"][role]
    if fb == FALLBACK_GPU:
        if row["gpu"] != AUTO:
            try:
                m = models.resolve(row["gpu"])
            except KeyError:
                return f"the {role} role's GPU fallback, its GPU model {row['gpu']}, isn't found"
            why = f"the {role} role's GPU model (wb route)"
        else:
            top, why = router.choose(size, stack, name, role=role)
            if top is None:
                return f"the {role} role's GPU fallback has no model: {why}"
            m = top.model
        state = models.status(m)
        if not state["available"]:
            return f"the {role} role's GPU fallback {m.id} can't run: {state['why']}"
        return m, state, f"{why} · {FALLBACK_LINE}", fallback_routed(doc, role, m, primary.id)
    m = models.lookup(fb)
    if m is None or m.kind != "api":
        return f"the {role} role's fallback {fb} isn't in OpenCode's catalog now"
    if m.provider == primary.provider:  # a hand edit's: wb route refuses one
        return f"the {role} role's fallback {fb} is on {primary.provider} too"
    if local:
        return f"the {role} role's fallback {fb} is an API model, and {local}"
    state = models.status(m)
    if not state["available"]:
        return f"the {role} role's fallback {fb} can't run: {state['why']}"
    if why := meter.refusal(m, role, advice=False, tokens=meter.FIRST_TOKENS if tokens is None else tokens):
        return f"the {role} role's fallback {fb} can't take it: {why}"
    routed = fallback_routed(doc, role, m, primary.id)
    return m, state, f"{routed_line(m.id, routed)} · {FALLBACK_LINE}", routed


def _fallback_part(old: dict, row: dict, cap, who: Callable[[str], str],
                   thinks: Callable[[str, str], str] = plain_thinks) -> tuple[list, bool]:
    """A role's fallback clauses (#1196) and whether they take the user's yes: an API fallback set
    or changed sends the role's code to its provider whenever the role's own is down or out of
    credit, so it takes the yes and a day cap (DAY_USD_SUGGEST when none, #1322); `gpu` or none
    sends nothing anywhere new."""
    was, now = old.get("fallback"), row.get("fallback")
    if was == now:
        return [], False
    before = fallback_name(was, old["gpu"]) if was else "none"
    if now is None:
        return [f"fallback {before} → none"], False
    if now == FALLBACK_GPU:
        return [f"fallback {before} → {fallback_name(now, row['gpu'])} when its API model's provider is down "
                "or out of credit"], False
    if cap is None:
        raise _NoCap()
    return [f"fallback {before} → {now}: your code goes to {who(now)} when its API model's provider is down "
            "or out of credit", thinks(now, row["thinking"]), f"cap {money(cap)}/day"], True


def _busy_part(doc: dict, new: dict, cap, free: Callable[[str], bool] = lambda mid: False,
               thinks: Callable[[str, str], str] = plain_thinks) -> tuple[str, bool]:
    """The busy row's part of the change line: ('', False) when it is as it was; to api takes the
    user's yes (it can spend), and a day cap (DAY_USD_SUGGEST when none) unless every API model is free; back
    to wait none."""
    was, now = doc.get("busy", "wait"), new.get("busy", "wait")
    if was == now:
        return "", False
    if now == "wait":
        return "When the GPU is busy: the role's API model → wait (queue, as before)", False
    has = [r for r in ROLES if new["roles"][r].get("api")]
    if cap is None and _priced(has, new, free):
        raise _NoCap()
    what = ([f"a role with an API model ({', '.join(has)}) starts on it while the GPU is busy",
             *_thinking_on(has, new, thinks), *_free_part(has, new, free)]
            if has else ["no role has an API model yet, so nothing changes until one does"])
    capped = f"; cap {money(cap)}/day" if cap is not None else ""
    return f"When the GPU is busy: wait → the role's API model ({'; '.join(what)}{capped})", True


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
               new: dict, free: Callable[[str], bool] = lambda mid: False,
               thinks: Callable[[str, str], str] = plain_thinks) -> tuple[list, bool]:
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
        clauses, _ = _role_part(r, old, {**row, "via": old["via"]}, cap, who, new, free, thinks)
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
            clauses += [f"your code goes to {who(row['api'])}",
                        *([FREE_LINE] if free(row["api"]) else []),  # what the yes is to (#1193)
                        role_thinks(row, thinks)]
            if row.get("order"):  # an OpenRouter role's backends (#1021): part of what is agreed to
                clauses.append(f"backends {', '.join(row['order'])}")
            elif str(row["api"]).split("/", 1)[0] == OPENROUTER:  # none named: OpenRouter's pick (#1322)
                clauses.append(OPENROUTER_ANY)
            if (fb := row.get("fallback")) and fb == old.get("fallback"):  # a kept one (#1196)
                clauses.append(f"fallback {fallback_name(fb, row['gpu'])}"
                               + (f" ({who(fb)})" if fb != FALLBACK_GPU else ""))
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
        # else the thinking clause says it: turned on with no dialog before it had a model
        # (#1177), or a model that thinks with the role's off (#1237)
        said = role_thinks(row, thinks)
        if now and old["thinking"] == row["thinking"] and not said.startswith("thinking off"):
            clauses.append(said)
        if now and free(row["api"]):  # once it goes to API, that change's line says it (#1193)
            clauses.append(FREE_LINE)
    if old["gpu"] != row["gpu"] and not (moved and not on_api):
        clauses.append(f"GPU model {_gpu_name(old['gpu'])} → {_gpu_name(row['gpu'])}")
    if old["thinking"] != row["thinking"] and not (moved and on_api):
        # what the model does with it (#1237): `thinking off → on (effort high)`, or `thinking on
        # → off: thinking as the model's default (…)` for one that can't turn it off
        new_said = plain_thinks("", row["thinking"])
        text = thinks(row["api"], row["thinking"]) if row.get("api") else new_said
        clauses.append(f"thinking {old['thinking']} → " + (text.removeprefix("thinking ") if text.startswith(new_said)
                                                          else f"{row['thinking']}: {text}"))
        gated |= on_api or sends
    was, now_review = old.get("thinking_review", "off"), row.get("thinking_review", "off")
    if was != now_review and not (moved and on_api):
        # #1059: on, the self-review is one more paid round on API, which the yes is to
        said = role_thinks(row, thinks)
        clauses.append(f"self-review thinking {was} → {now_review}" + (
            f" ({said}: the agent reviews its own change in one more round before Claude)"
            if said.startswith(REVIEW_ONLY) else ""))
        gated |= on_api or sends
    if row.get("order") and old.get("order") != row["order"] and not (moved and on_api):
        clauses.append(f"backends {', '.join(old.get('order') or ['none'])} → {', '.join(row['order'])}")
        gated = True  # where the code goes, within OpenRouter (#1021)
    more, g = _fallback_part(old, row, cap, who, thinks)
    return clauses + more, gated or g


def _goes_to(roles: list, doc: dict, who: Callable[[str], str]) -> str:
    """`your code goes to DeepSeek (trivial), Anthropic (significant, writing)`: by provider."""
    by: dict[str, list] = {}
    for r in roles:
        by.setdefault(who(doc["roles"][r]["api"]), []).append(r)
    return "your code goes to " + ", ".join(f"{p} ({', '.join(rs)})" for p, rs in by.items())


def _agents_part(doc: dict, new: dict, cap, who: Callable[[str], str],
                 free: Callable[[str], bool] = lambda mid: False,
                 thinks: Callable[[str, str], str] = plain_thinks) -> tuple[str, bool, list]:
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
        what += [_goes_to(to_api, new, who), *_free_part(to_api, new, free), *_thinking_on(to_api, new, thinks)]
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
           order: list[str] | None = None, busy: str | None = None, fallback: str | None = None,
           free: Callable[[str], bool] = lambda mid: False,
           thinks: Callable[[str, str], str] = plain_thinks, thinking_review: str | None = None) -> Change:
    """What a `wb route` does to ``doc``: ``role`` one of ROLES, "all" (``use``, the Agents
    switch, ``thinking`` and ``thinking_review`` only) or None (the cap alone). ``who``: an API
    id's provider, as the user knows it (the catalog's name); ``thinks``: what an API id does with a thinking setting,
    as its task would run it (thinking_of, #1237). Never writes. RoutesError for a change that can't be
    made: `api` with no API model kept. The first API role with no cap takes DAY_USD_SUGGEST as
    the change's cap, in its line (`cap $5/day`), so the dialog's one yes sets both: it was a
    refusal (NO_CAP) whose answer was always --day-usd 5 (#1322); with the cap taken off in
    this change it is still refused. ``order``: an OpenRouter role's backends (#1021), dropped
    when the role's API model is another provider's. ``free``: whether an API id is a free model
    (meter.is_free): its role's line says FREE_LINE, and no cap is taken while every model newly
    on API is free (#1193). ``fallback`` (#1196): an API id, FALLBACK_GPU, or FALLBACK_OFF to
    clear it; an API one that is the role's own API model, on the same provider, or OpenRouter's
    is refused. ``thinking_review`` (#1059): off or on, the role's self-review thinking."""
    args = dict(role=role, via=via, gpu=gpu, api=api, thinking=thinking, who=who, use=use,
                order=order, busy=busy, fallback=fallback, free=free, thinks=thinks,
                thinking_review=thinking_review)
    try:
        return _change(doc, day_usd=day_usd, **args)
    except _NoCap:
        if day_usd is not None:
            raise
    return _change(doc, day_usd=DAY_USD_SUGGEST, **args)


def _change(doc: dict, role: str | None, via: str | None, gpu: str | None, api: str | None,
            thinking: str | None, day_usd, who: Callable[[str], str], use: str | None,
            order: list[str] | None, busy: str | None, fallback: str | None,
            free: Callable[[str], bool], thinks: Callable[[str, str], str],
            thinking_review: str | None = None) -> Change:
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
        if thinking_review == "on":
            row["thinking_review"] = "on"
        elif thinking_review is not None:
            row.pop("thinking_review", None)
        if order is not None:
            row["order"] = list(order)
        if fallback == FALLBACK_OFF:
            row.pop("fallback", None)
        elif fallback is not None:
            row["fallback"] = fallback
        if (fb := row.get("fallback")) and fb != FALLBACK_GPU and (fallback is not None or api is not None):
            _fallback_check(r, fb, row.get("api"))
        if row.get("order") and str(row.get("api") or "").split("/", 1)[0] != OPENROUTER:
            row.pop("order")
        if via == "api" and not row.get("api"):
            raise RoutesError(f"{r} has no API model yet: wb route {r} <provider/model>")
        if via is not None:
            row["via"] = via
        clauses, g = _role_part(r, old, row, cap, who, new, free, thinks)
        if not clauses:
            continue
        parts.append(f"{r}: " + " · ".join(clauses))
        gated |= g
        changed.append(r)
        new["roles"][r] = row  # its stamps kept: stamp() renews the side this change touched
    if changed and role != "all" and (held := _held(doc, new, changed, via is not None or api is not None)):
        parts.append(held)  # the role's own choice, which the Agents switch holds over for now
    agents, g, moved = _agents_part(doc, new, cap, who, free, thinks)
    if agents:
        parts.insert(0, agents)
        gated |= g
        changed += [r for r in moved if r not in changed]
    # a role newly sent to an API model, by its own change or the switch
    newly = [r for r in ROLES if effective_via(new, r) == "api" and new["roles"][r].get("api")
             and (effective_via(doc, r) != "api" or doc["roles"][r].get("api") != new["roles"][r].get("api"))]
    to_api = bool(newly)
    # every one on a free model: no cap to take, as the guard keeps none for it (#1193); a
    # priced model set later takes one then
    if to_api and cap is None and _priced(newly, new, free):
        raise _NoCap()
    busy_line, g = _busy_part(doc, new, cap, free, thinks)
    capped = to_api or g  # a line that says "cap $N/day" already
    if busy_line:
        parts.append(busy_line)
        gated |= g
    elif busy_now := _busy_newly(doc, new):
        # with the opt-in on, a GPU role given an API model, or Agents back from GPU, can now send
        # code to API while the GPU is busy: the user's yes and a cap, as any role newly on API
        # (2026-11-23 review: `wb route all both` re-enabled it with no dialog, and a role's API
        # model set after `cap off` spent with no cap)
        if cap is None and _priced(busy_now, new, free):
            raise _NoCap()
        said = [_goes_to(busy_now, new, who), *_free_part(busy_now, new, free),
                *_thinking_on(busy_now, new, thinks), *([f"cap {money(cap)}/day"] if cap is not None else [])]
        parts.append(f"when the GPU is busy, {', '.join(busy_now)} {'starts' if len(busy_now) == 1 else 'start'} "
                     f"on {'its' if len(busy_now) == 1 else 'their'} API model ({'; '.join(said)})")
        gated = capped = True
    if cap is None and doc["day_usd"] is not None:  # `wb route cap off` (#1023): never under an API role
        if spends := [r for r in ROLES if effective_via(new, r) == "api"] + (["busy"] if busy_api_any(new) else []):
            raise RoutesError(f"the day cap stays while {', '.join(spends)} can spend on API: put "
                              f"{'it' if len(spends) == 1 else 'them'} on the GPU (or wb route busy wait) first")
        parts.append(f"day cap {money(doc['day_usd'])} → none")
        gated = True
    elif cap != doc["day_usd"]:
        gated = True
        # else the role's, its fallback's or busy's "cap $N/day" said it
        if doc["day_usd"] is not None or not (capped or f"cap {money(cap)}/day" in "; ".join(parts)):
            parts.append(f"day cap {money(doc['day_usd']) if doc['day_usd'] is not None else 'none'}"
                         f" → {money(cap)}/day")
    if not changed and not agents and not busy_line and cap == doc["day_usd"]:
        return Change(new, "", False, [])
    return Change(new, "; ".join(parts), gated, changed)


def _fallback_check(r: str, fb: str, api: str | None) -> None:
    """An API fallback wb route refuses (#1196): the role's own API model, one on its provider (a
    provider down or out of credit stops both), or OpenRouter's (its backends are the role's own
    model's, chosen with --order, which already fail over among themselves)."""
    p = fb.split("/", 1)[0]
    if p == OPENROUTER:
        raise RoutesError(f"{fb}: an OpenRouter model can't be a fallback; an OpenRouter role's own backends "
                          "(--order) already fail over among themselves")
    if fb == api:
        raise RoutesError(f"{fb} is the {r} role's own API model: a fallback is another provider's model, or gpu")
    if api and p == api.split("/", 1)[0]:
        raise RoutesError(f"the {r} role's fallback {fb} is on {p}, as its API model {api} is: a provider down "
                          "or out of credit stops both, so the fallback is another provider's model, or gpu")


def change_pairs(doc: dict, pairs: list, day_usd=None,
                 who: Callable[[str], str] = lambda mid: mid.split("/", 1)[0],
                 free: Callable[[str], bool] = lambda mid: False,
                 thinks: Callable[[str, str], str] = plain_thinks) -> Change:
    """Several roles in one change, one line and one yes (#1024: the first run's question):
    ``pairs`` [(role, via, api)] each as change() takes them, ``day_usd`` the cap for all. All or
    nothing: RoutesError for any pair change() refuses. The line: each role's move, then where
    the code goes once per provider, thinking and the cap once: `trivial: GPU → API a ·
    significant: GPU → API b · your code goes to DeepSeek · thinking off · cap $5/day`."""
    new, roles, gated = doc, [], False
    for i, (r, via, api) in enumerate(pairs):
        ch = change(new, r, via=via, api=api, day_usd=day_usd if i == 0 else None, who=who, free=free, thinks=thinks)
        new, gated = ch.doc, gated or ch.gated
        roles += [x for x in ch.roles if x not in roles]
    cap = new["day_usd"]
    if not roles and cap == doc["day_usd"]:
        return Change(new, "", False, [])
    parts, sent = [], []
    for r in roles:
        old, row = doc["roles"][r], new["roles"][r]
        clauses, _ = _role_part(r, old, row, cap, who, new, free, thinks)
        if row["via"] == "api" and (old["via"] != "api" or old.get("api") != row.get("api")):
            sent.append(r)  # said once for all below, not per role
            once = {f"your code goes to {who(row['api'])}", role_thinks(row, thinks), FREE_LINE}
            # no cap with only free models (#1193): nothing to leave out, and money(None) raised
            clauses = [c for c in clauses if c not in once and (cap is None or c != f"cap {money(cap)}/day")]
        parts.append(f"{r}: " + " · ".join(clauses))
    if sent:
        by: dict[str, list] = {}
        for r in sent:
            by.setdefault(who(new["roles"][r]["api"]), []).append(r)
        parts.append(f"your code goes to {next(iter(by))}" if len(by) == 1 else _goes_to(sent, new, who))
        parts += _free_part(sent, new, free)
        said = {r: role_thinks(new["roles"][r], thinks) for r in sent}
        parts.append(next(iter(said.values())) if len(set(said.values())) == 1 else
                     "thinking " + ", ".join(f"{t.removeprefix('thinking ')} ({r})" for r, t in said.items()))
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


DRIVER_APPLIED = "the plugin's /workbench switches its OpenCode session to it"  # #1316


def driver_change(doc: dict, pin: str, gpu: Callable[[str], bool], who: Callable[[str], str]) -> Change:
    """`wb route driver <id>|session` (#1316): routes.json's top-level `driver`, the standing
    default the OpenCode plugin's /workbench applies to its session. A GPU model's pin (``gpu``:
    one discover() finds) sends nothing off the machine and needs no yes; any other id is taken
    for an API model, failing closed: the driver's whole conversation, the code in it, would go
    to its provider, so a model's call needs the host's dialog, as a role's API change does.
    `session` takes the pin off (no yes either). Nothing is stamped: no task goes by it."""
    new = copy.deepcopy(doc)
    old = doc.get("driver") or DRIVER_SESSION
    if pin == DRIVER_SESSION:
        new.pop("driver", None)
    elif driver_ref(pin) is None:
        raise RoutesError(f"{pin!r} isn't an OpenCode model id (provider/model, #variant allowed) "
                          f"or {DRIVER_SESSION}")
    else:
        new["driver"] = pin
    if pin == old:
        return Change(new, "", False)
    if pin == DRIVER_SESSION:
        return Change(new, "driver: each OpenCode session's own (/models), no pin", False)
    bare = pin.split("#", 1)[0]
    if gpu(bare):
        return Change(new, f"driver: {pin} (GPU): {DRIVER_APPLIED}", False)
    return Change(new, f"driver: {pin} (API, {who(bare)}): {DRIVER_APPLIED}, and that session's "
                       "conversation, the code in it, goes to its provider", True)


def summary(doc: dict) -> str:
    """The roles in one line, from the document alone (the mod's routesSummary says the same:
    Couplings): what the context block and the [workbench] echo tell Claude."""
    out = []
    for r in ROLES:
        row = doc["roles"][r]
        # an OpenRouter role's backends (`order`, #1021) are where its code goes: left out, a
        # change of them alone reached the conversation as nothing (#1126, 2026-11-23)
        backends = f", backends {', '.join(row['order'])}" if row.get("api") and row.get("order") else ""
        # the role's fallback (#1196): where its code may go too, so a change of it alone is a row
        fb = f", fallback {'GPU' if row['fallback'] == FALLBACK_GPU else row['fallback']}" if row.get("fallback") else ""
        if row["via"] == "api":
            out.append(f"{r} API {row['api']}{backends}, {thinking_said(row)}{fb}")
            continue
        text = f"{r} GPU " + ("(the router picks)" if row["gpu"] == AUTO else row["gpu"])
        if row.get("api"):
            text += f", API kept: {row['api']}{backends}"
        if row["thinking"] == "on":
            text += ", thinking on"
        elif row.get("thinking_review") == "on":  # #1059: kept for API, as thinking is
            text += f", {REVIEW_ONLY}"
        out.append(text + fb)
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
    said = fix and (fix in why or any(w in why for w in ("/connect", "opencode auth login", "top it up")))  # its own remedy
    return (f"{role} role: {mid} can't run: {why}" + (f" ({fix})" if fix and not said else "")
            + f". Ask the user: fix that, or {other}")


def said_part(routed: dict) -> str:
    """Why an API task runs where it does, as the start line and `routed` say it: `trivial role`
    (only the user sets a role, so no stamp says who or when: the roles' listing has it, #1322),
    or `asked for, approved in the dialog` for a gated --model."""
    said = routed.get("said") or ""
    if said.startswith("asked for"):
        return said
    return f"{routed.get('role')} role" + ("'s fallback" if routed.get("fallback") else "")


def _thinking_said(routed: dict) -> str:
    """What the task does with thinking: `thinking_shown` (thinking_of's, #1237), else, for a
    task.json from before it, the role's setting."""
    return routed.get("thinking_shown") or f"thinking {routed.get('thinking') or 'off'}"


def routed_line(model_id: str, routed: dict) -> str:
    """An API task's routing in one line (task.json's keys): `API <id> (<provider>) · <why> ·
    thinking off`. The start line, task.json `route` and wb watch's `routed` say it."""
    return (f"API {model_id} ({routed.get('provider_name') or routed.get('provider')}) · "
            f"{said_part(routed)} · {_thinking_said(routed)}")


def model_line(model_id: str, routed: dict) -> str:
    """wb start's model line for an API task: `model: <id> · API (<provider>) · trivial role ·
    thinking off`, which wb start ends with meter.tail (today's spend); a GPU task's
    line is as before roles."""
    return (f"model: {model_id} · API ({routed.get('provider_name') or routed.get('provider')}) · "
            f"{said_part(routed)} · {_thinking_said(routed)}")


def resolve(role: str, size: str, wanted: str | None, stack=(), name: str | None = None,
            gate: Callable | None = None, tokens: int | None = None) -> tuple:
    """wb start's model: (model, state, why, routed), `routed` the task.json keys (`via`, `role`;
    for API `provider`, `provider_name`, `thinking`, `said`, `said_ts`). Refused, with the one
    line, for what can't run as asked. In order: routes.json must read (never as all GPU); a
    `--model` is taken as asked (an API one through ``gate``; the user's yes is wb start's, last,
    #1104); the role's side
    (effective_via): the GPU, its pin or the router's pick (router.choose as before roles), or
    the role's API model by its exact id. No fallback in either direction: a GPU role with no
    GPU model never tries its stored API model, and an API role that can't run never the GPU (the
    user's own `fallback` is wb start's to apply, after this: cli._fallback_start, #1196).
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
        if why := meter.refusal(m, tokens=first, asked=True):  # before the dialog: never ask for a start that is refused
            raise Refused(why)
        approved = store.under_model()  # the gate above asked (cli.start_gate), an agent's call too (#1310)
        return m, models.status(m), "asked for", {
            "via": "api", "role": role, "provider": m.provider, "provider_name": models.provider_name(m),
            "thinking": "off", "thinking_shown": thinking_of(m, "off"),
            "said": "asked for" + (", approved in the dialog" if approved else "")}
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
              "thinking": row["thinking"],
              "thinking_shown": thinking_of(m, row["thinking"], row.get("thinking_review")), **_review(row), "said": row.get("said") or "", "said_ts": row.get("ts")}
    # an OpenRouter role's backends (#1021): task.json's `order`, which the keyhole routes every
    # request by; the user agreed to them in the dialog, and once they never reached the task
    if row.get("order") and m.provider == OPENROUTER:
        routed["order"] = list(row["order"])
    return m, models.status(m), routed_line(m.id, routed), routed
