"""The admin rows (2.0, #1279): every row the pane's Settings tab had, and the wb call each one
makes, in one table the chat server renders the web app's Settings from. A new setting touches only
this file.

A row is a JSON-safe dict: `key` (the pane's name), `group` (GROUPS), `label`, `kind` (KINDS), and
by kind: a `choice` its `value`, `choices` [{value, label, says}] and `says` (the sentence for the
current value), with `other: true` where any model id goes (the role command checks it); a
`number` its `value` (a float, or None for off) and `unit` (`usd` or `per_kwh`); an `info` its
`text`; an `action` its `text` and its `choices`, which are buttons; a `note` one line for the group
(NO_PROVIDER, OLDER_WB or wb's own reason) that stands where the group's rows would be. A row may
carry `help`: the note for Claude while the setting is on but can't work yet (probe not set up),
which apply() also returns when that value was just chosen; the chat server records it, and the
mod turns it into a [workbench] turn where Claude offers to help (#1279's addendum, the owner,
2026-11-24: "the terminal still offers to help").

Reads: `wb route --json` and `wb models --json` as children (the JSON the pane read), the small
files in-process through their own modules; kept ROWS_CACHE_S. Writes: the pane's wb command, as a
child of child_env() (no CLAUDECODE, CLAUDE_PID or CLAUDE_CODE_SESSION_ID, WB_ACTOR=web), one at a
time, then the cache is dropped. The user's click on the web app is the yes, as a command they
type at their terminal is: the child isn't Claude, so `wb route` doesn't gate it, and no approval
file is written. It stamps a role change `by: "web"` (WB_ACTOR; CLAUDECODE wins over it).
"""
from __future__ import annotations

import copy
import json
import math
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from . import cli, context, progress, routes, savings, stats

GROUPS = ("shared", "gpu", "api", "terminal", "experimental")  # display order
KINDS = ("choice", "number", "info", "action", "note")
ACTOR = "WB_ACTOR"  # env var; "web" stamps a role change by="web" (cli._route_apply)
ROWS_CACHE_S = 10  # the pane's ROLES_CACHE_MS
READ_TIMEOUT_S = 15  # wb route --json, wb models --json (the pane's 15 s)
WRITE_TIMEOUT_S = 60
SCRUBBED = ("CLAUDECODE", "CLAUDE_PID", "CLAUDE_CODE_SESSION_ID")
WB = Path(__file__).resolve().parent.parent / "bin" / "wb"  # the clone's launcher; tests swap it
API_MENU = 12  # an API row lists at most this many models, the ones in use first (the pane's)

OLDER_WB = "wb is older than the mod: wb update"
NO_PROVIDER = "No API provider connected: /connect in OpenCode adds one; every role runs on the GPU."
NO_GPU = "No GPU model found (wb models)"

_WRITE = threading.Lock()  # one write at a time: two clicks never interleave their wb calls
_READ = threading.Lock()  # one read of the rows at a time: the others wait for it, then share it
_cache: dict | None = None  # {"at", "rows", "routes", "models"}

# The pane's choices, each (value, label, says): its *_STOPS tables, moved here
AGENTS = [("gpu", "GPU", "Every role runs on its GPU model: nothing spends, and an API task already "
           "started stops before its next round. Each role's own choice is kept for Both."),
          ("api", "API", "Every role runs on its API model; a role with none can't start."),
          ("both", "Both (default)", "Each role runs where its own row says.")]
BUSY = [("wait", "Wait (default)", "A task waits until the GPU is free."),
        ("api", "Use the role's API model", "A task whose role has an API model starts on it while "
         "the GPU is busy; a running task never moves.")]
CODING = [("only", "Only when I say", "You tell Claude when to write code."),
          ("ask", "Ask me first", "Claude can ask to make a specific fix and waits for your yes."),
          ("decide", "Let Claude decide (default)", "Claude uses its judgment to guide the agent or make a "
           "fix itself.")]
WORKFLOWS = [("off", "Off (default)", "Claude runs one task at a time."),
             ("on", "On", "Claude may split a request into tasks that run at once, each on its own role's model.")]
RECORD = [("on", "On (default)", "Each task keeps how it went for the Stats tab: speeds, watts, what stopped "
           "it. Nothing leaves this machine."),
          ("off", "Off", "Tasks keep only what workbench itself learns from.")]
LIVE = [("on", "On (default)", "The Stats tab shows this machine now (wb top), and only while it is on screen."),
        ("off", "Off", "The Stats tab shows the history only.")]
_EVERY_SAYS = "How often the live view samples the GPU, the tasks and the models."
EVERY = [("1", "1 s", _EVERY_SAYS), ("2", "2 s (default)", _EVERY_SAYS), ("5", "5 s", _EVERY_SAYS)]
PROGRESS_OFF = ("off", "Off (default)", "No progress checks: each one wakes Claude, which spends its tokens.")
THINK = [("off", "Off (default)", "Faster and cheaper; for tasks started after this."),
         ("on", "On", "Better on hard steps, slower and costs more; for tasks started after this.")]
SIZES = [("all", "Any size (default)", "The router may give it any task."),
         ("trivial", "Trivial only", "The router gives it trivial coding tasks only."),
         ("significant", "Complex only", "The router gives it complex coding tasks only.")]
SPLASH = [("on", "Enabled (default)", "The first /workbench of a session opens with a short animation; any "
           "key skips it."),
          ("off", "Off", "The Tasks page opens at once.")]
PROBE = [("off", "Off (default)", "The agent reads whole files, as without the experiment."),
         ("on", "On (large files only)", f"The agent may extract one function of a file over "
          f"{context.PROBE_FILE_LINES:,} lines with probe, instead of reading it whole (github.com/probelabs/probe).")]
MEDIA = [("image", "Image engine", "image"), ("speech", "Speech engine", "audio"),
         ("sfx", "Sound effects engine", "audio"), ("music", "Music engine", "audio")]
LOWEST_SAID = "thinking at its lowest effort"  # runner.THINKING_LOWEST's opening (the mod's thinkStops: Couplings)

# The note for Claude when Agent code navigation is just turned on and probe isn't set up: the
# pane's PROBE_SETUP_NOTE (the user, 2026-11-20: "let Claude offer to help the user get it fully
# set up"), with wb's own line; the mod's [workbench] turn says who changed what before it
PROBE_HELP = (
    "Agent code navigation (the probe experiment) is on, but it can't work yet. wb says: {said}\n"
    "Offer to set it up, and install nothing without their yes. probe (github.com/probelabs/probe) "
    "is a code-search command-line tool; the agents need its release binary, whose `probe --help` "
    "lists `symbols` (crates.io's probe-code 0.6.0 doesn't, though it reports the same version). "
    "Pick the asset for this OS and CPU from github.com/probelabs/probe/releases, put `probe` in a "
    "directory on the PATH you and wb run with (such as ~/.local/bin), then run `wb experiment "
    'probe` until it says "and working". Tasks started from then use it; running ones keep '
    "what they started with. If they would rather not, `wb experiment probe off`.")


def _probe_help(value, said: str) -> str | None:
    return PROBE_HELP.format(said=said) if value == "on" and "not set up" in said else None


# key -> (value, wb's line) -> the note for Claude, or None: a setting whose chosen value can need
# Claude's help to work names it here
HELP = {"probe": _probe_help}


def child_env() -> dict[str, str]:
    """The environment of every wb the server runs: never Claude's (a server started under Claude
    Code carries CLAUDECODE=1, and wb would then gate the user's own click), marked as the web's."""
    env = {k: v for k, v in os.environ.items() if k not in SCRUBBED}
    env[ACTOR] = "web"
    return env


def _run(argv: list[str], timeout: float) -> tuple[bool, str, str]:
    """wb ARGV as a child of child_env(): (ok, stdout, stderr); a timeout or a launch failure is
    not ok, its reason in stderr."""
    try:
        p = subprocess.run([sys.executable, "-X", "utf8", str(WB), *argv], env=child_env(),
                           capture_output=True, text=True, errors="replace", timeout=timeout,
                           stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return False, "", f"wb {argv[0]} took longer than {timeout:g} s"
    except OSError as e:
        return False, "", f"wb couldn't run: {e}"
    return p.returncode == 0, p.stdout, p.stderr


def _last(text: str) -> str:
    return next((ln.strip() for ln in reversed(text.splitlines()) if ln.strip()), "")


def _read_json(argv: list[str]) -> tuple[dict | None, str]:
    """A `--json` read: (the document, "") or (None, why): wb's own line, or OLDER_WB for a wb
    without the flag (the pane's rolesRead)."""
    ok, out, err = _run(argv, READ_TIMEOUT_S)
    if ok:
        try:
            doc = json.loads(out)
            if isinstance(doc, dict):
                return doc, ""
        except ValueError:
            pass
        return None, OLDER_WB
    said = _last(err) or _last(out)
    return None, OLDER_WB if not said or "invalid choice" in said or "unrecognized arguments" in said else said


def _choice(key, group, label, stops, value, **extra) -> dict:
    choices = [{"value": v, "label": lab, "says": says} for v, lab, says in stops]
    chosen = next((c for c in choices if c["value"] == value), choices[-1] if choices else None)
    return {"key": key, "group": group, "label": label, "kind": "choice", "value": value,
            "choices": choices, "says": chosen["says"] if chosen else "", "other": False, **extra}


def _info(key, group, label, text) -> dict:
    return {"key": key, "group": group, "label": label, "kind": "info", "text": text}


def _note(key, group, text) -> dict:
    return {"key": key, "group": group, "label": "", "kind": "note", "text": text}


def _number(key, group, label, value, unit, says) -> dict:
    return {"key": key, "group": group, "label": label, "kind": "number", "value": value, "unit": unit,
            "says": says}


def _window(n) -> str:
    return f" ({round(n / 1024)}K)" if isinstance(n, (int, float)) and not isinstance(n, bool) and n > 0 else ""


def _role_stops(label: str, api: str | None, name: str | None) -> list:
    return [("gpu", "GPU", f"{label} runs on this machine's GPU."),
            ("api", f"API ({name or api})" if api else "API (set its model under API first)",
             f"{label} runs on {api}; your code goes to its provider." if api else "Pick its API model under API first.")]


def _gpu_stops(label: str, gpu: list, pinned: str) -> list:
    out = [("auto", "Auto (the router picks)", f"The router picks {label}'s GPU model for each task.")]
    for g in gpu:
        cards = f" · GPU {'+'.join(map(str, g['cards']))}" if g.get("cards") else ""
        out.append((g["id"], f"{g.get('label') or g['id']}{_window(g.get('window'))}{cards}",
                    f"Pins {label} to {g['id']}: the router doesn't pick for it."))
    if pinned != routes.AUTO and not any(g["id"] == pinned for g in gpu):
        out.append((pinned, f"{pinned} (not found: wb models)",
                    "A task of this role exits 3 until it is found again or another is picked."))
    return out


def _api_stops(label: str, m: dict | None, current: str, others: list[str]) -> list:
    """The pane's apiStops: the role's own model, the other roles', those used before, then the
    catalog by provider, API_MENU at most past the ones in use; any other id goes (`other`)."""
    listed = {x["id"]: x for x in (m or {}).get("api") or [] if isinstance(x, dict) and x.get("id")}

    def line(mid: str) -> tuple:
        x = listed.get(mid)
        if not x:
            return mid, mid, f"{label} uses {mid}."
        p = x.get("price") or {}
        price = f" · ${p['input']}/${p['output']} per M" \
            if all(isinstance(p.get(k), (int, float)) for k in ("input", "output")) else ""
        free = f" · {(m or {}).get('free_line') or 'free'}" if x.get("free") else ""
        return (mid, f"{x.get('name') or mid} · {x.get('provider_name') or x.get('provider')}"
                     f"{_window(x.get('window'))}{price}{free}",
                f"{label} uses {mid} on API; your code goes to {x.get('provider_name') or x.get('provider')}.")
    ids: list[str] = []
    for mid in [current, *others, *[u for u in (m or {}).get("used") or [] if u in listed]]:
        if mid and mid not in ids:
            ids.append(mid)
    rest = sorted(listed.values(), key=lambda x: (str(x.get("provider_name")), str(x.get("name"))))
    for x in rest:
        if len(ids) >= API_MENU:
            break
        if x["id"] not in ids:
            ids.append(x["id"])
    return [line(mid) for mid in ids]


def _think_stops(row: dict) -> list:
    """The pane's thinkStops: each choice a model can't keep says what it does instead (#1263)."""
    off = routes.thinking_said({**row, "thinking": "off"})
    on = routes.thinking_said({**row, "thinking": "on"})
    off_why = ("This model can't turn thinking off, but its lowest effort is still cheaper than On; for "
               "tasks started after this." if off.startswith(LOWEST_SAID) else
               "This model can't turn thinking off, so it isn't cheaper; for tasks started after this.")
    return [THINK[0] if off.startswith("thinking off") else ("off", f"Off (default): {off}", off_why),
            ("on", f"On: {on}", "This model doesn't reason, so this changes nothing; for tasks started "
             "after this.") if on.startswith("thinking off") else THINK[1]]


def _spend_line(r: dict) -> str:
    s = r.get("spend_today") or {}
    total = s.get("usd") or 0
    names = r.get("providers") or {}
    by = [f"{(names.get(p) or {}).get('name') or p} {savings.usd(v)}"
          for p, v in (s.get("by_provider") or {}).items() if isinstance(v, (int, float)) and v > 0]
    basis = s.get("prices") or "catalog"
    note = {"catalog": "catalog prices", "yours": "your prices"}.get(basis, "") if total else ""
    inside = "; ".join(x for x in (" · ".join(by), note) if x)
    each = f" ({inside})" if inside else ""
    spent = savings.money(total, basis)
    cap = r.get("day_usd")
    return f"Spend today: {spent}" + (f" of {routes.money(cap)}{each}" if cap is not None else f"{each} · no day cap")


def _connected_line(r: dict) -> str:
    return "Connected: " + " · ".join(
        f"{p.get('name')} (key stored)" if p.get("key") == "stored" else f"{p.get('name')} ({p.get('why') or p.get('key')})"
        for p in (r.get("providers") or {}).values())


def _progress_stops(after) -> list:
    if not after:
        return [PROGRESS_OFF, ("on", "On", "A task past its typical length (20 min trivial, 45 significant, until "
                               "3 of the size are timed on its model) gets a check, again at 2x and 4x, each "
                               "with one recommendation, waking Claude.")]
    return [PROGRESS_OFF, ("on", f"On, after {after:g} min",
                           f"A task past {after:g} min of active work gets a check and one recommendation, waking Claude.")]


def _probe_row() -> dict:
    """Agent code navigation, as wb says it now (`wb experiment probe`'s line): working, with
    probe's version, or on but not set up, which carries the help note."""
    mode = context.experiments().get("probe", "off")
    line = next((ln for ln in cli.experiment_lines() if ln.startswith("probe:")), "")
    recommended = "on is recommended" in line
    stops = []
    for v, lab, says in PROBE:
        named = f"{lab} (recommended)" if v == "on" and recommended else lab
        if v == mode == "on" and "not set up" in line:
            named, says = f"{named}, not set up", "probe isn't installed, so nothing changes yet: Claude can set it up."
        elif v == mode == "on" and "and working" in line:
            found = line.split("and working (", 1)[1].split(")", 1)[0] if "and working (" in line else ""
            named, says = f"{named}, working", says.replace("with probe", f"with probe ({found})" if found else "with probe")
        stops.append((v, named, says))
    row = _choice("probe", "experimental", "Agent code navigation", stops, mode)
    if help := _probe_help(mode, line):
        row["help"] = help
    return row


def _media_rows(m: dict | None) -> list[dict]:
    out = []
    for kind, label, cmd in MEDIA:
        k = ((m or {}).get("media") or {}).get(kind)
        engines = (k or {}).get("engines") or []
        if len(engines) < 2:
            continue
        stops = [(e["name"], f"{e['name']}{'' if e.get('gpu') else ' · off this GPU'}",
                  f"wb {cmd} runs {e['name']} unless --engine names another.") for e in engines]
        out.append(_choice(f"media-{kind}", "gpu", label, stops, k.get("default") or engines[0]["name"]))
    return out


def _optimize_row(m: dict) -> dict:
    o = m.get("optimize") or {}
    if o.get("running"):
        state = "measuring now"
    elif o.get("models"):
        n = o["models"]
        when = f" on {datetime.fromtimestamp(o['done_at']).strftime('%Y-%m-%d')}" \
            if isinstance(o.get("done_at"), (int, float)) else ""
        state = f"measured {n} model{'' if n == 1 else 's'}{when}"
    else:
        state = "not run yet"
    return {"key": "optimize", "group": "gpu", "label": "Optimize", "kind": "action",
            "text": f"Optimize: {state}",
            "choices": [{"value": "start", "label": "Measure now",
                         "says": "Measures this machine for its models (wb optimize start)."}]}


def _build() -> dict:
    r, why = _read_json(["route", "--json"])
    m, mwhy = _read_json(["models", "--json"])
    if m is not None and not isinstance(m.get("gpu"), list):
        m, mwhy = None, OLDER_WB
    r = r if r is not None and isinstance(r.get("roles"), dict) else None
    out: list[dict] = []
    roles = [(name, routes.LABELS[name], (r or {}).get("roles", {}).get(name) or {}) for name in routes.ROLES]
    api_name = {x.get("id"): x.get("name") for x in (m or {}).get("api") or [] if isinstance(x, dict)}

    # shared
    if r is None:
        out.append(_note("roles-why", "shared", why or OLDER_WB))
    else:
        out.append(_choice("agents", "shared", "Agents", AGENTS, r.get("use") or "both"))
        out.append(_choice("busy", "shared", "When the GPU is busy", BUSY, r.get("busy") or "wait"))
        for name, label, row in roles:
            out.append(_choice(f"role-{name}", "shared", label,
                               _role_stops(label, row.get("api"), api_name.get(row.get("api"))), row.get("via") or "gpu"))
    out.append(_choice("coding", "shared", "Claude coding", CODING, cli.coding_mode()))
    out.append(_choice("workflows", "shared", "Dynamic workflows", WORKFLOWS, cli.workflows_mode()))
    if r is not None:
        cap = r.get("day_usd")
        out.append(_number("cap", "shared", "API day cap", float(cap) if cap is not None else None, "usd",
                           "Needed before an API role; $5 suggested." if cap is None else
                           f"API tasks stop for the day at {routes.money(cap)} of spend."))
    st = stats.settings()
    out.append(_choice("record", "shared", "Stats recording", RECORD, "on" if st["recording"] else "off"))
    out.append(_choice("live", "shared", "Live view", LIVE, "on" if st["live"] else "off"))
    out.append(_choice("every", "shared", "Live refresh", EVERY, str(st["every"])))
    prog = progress.settings()
    after = prog.get("after") if isinstance(prog.get("after"), (int, float)) and not isinstance(prog.get("after"), bool) else 0
    out.append(_choice("progress", "shared", "Progress checks", _progress_stops(after),
                       "on" if prog.get("on") is True else "off"))
    price = savings.price()
    out.append(_number("price", "shared", "Electricity price per kWh", price, "per_kwh",
                       "Energy shows in Wh until a price is set." if price is None else
                       "Energy is costed at this price, in what you pay in."))

    # gpu
    if r is None:
        out.append(_note("gpu-why", "gpu", why or OLDER_WB))
    elif m is None:
        out.append(_note("gpu-why", "gpu", mwhy or OLDER_WB))
    elif not m["gpu"]:
        out.append(_note("gpu-none", "gpu", NO_GPU))
    else:
        for name, label, row in roles:
            gpu = row.get("gpu") or routes.AUTO
            out.append(_choice(f"gpu-{name}", "gpu", f"{label} GPU model", _gpu_stops(label, m["gpu"], gpu), gpu))
        for g in m["gpu"]:  # every discovered model: the pane's six-row cap went with the pane
            sizes = g.get("sizes") or []
            out.append(_choice(f"size-{g['id']}", "gpu", f"{g.get('label') or g['id']} tasks", SIZES,
                               sizes[0] if len(sizes) == 1 else "all"))
        out.append(_optimize_row(m))
    out.extend(_media_rows(m))

    # api
    if r is None:
        out.append(_note("api-why", "api", why or OLDER_WB))
    else:
        provs = r.get("providers") or {}
        stored = any(p.get("key") in ("stored", "ollama") for p in provs.values() if isinstance(p, dict))
        if not stored and not any(row.get("api") for _, _, row in roles):
            out.append(_note("api-none", "api", NO_PROVIDER))
        else:
            if provs:
                out.append(_info("api-connected", "api", "Connected", _connected_line(r)))
            for name, label, row in roles:
                others = [x.get("api") for n, _, x in roles if n != name and x.get("api")]
                api = row.get("api") or ""
                stops = _api_stops(label, m, api, others)
                c = _choice(f"api-{name}", "api", f"{label} API model", stops, api or None, other=True)
                if not api:
                    c["says"] = f"{label} has no API model: it runs on the GPU."
                out.append(c)
            for name, label, row in roles:
                out.append(_choice(f"think-{name}", "api", f"{label} thinking",
                                   _think_stops({"api": None, "thinking": "off", **row}), row.get("thinking") or "off"))
            out.append(_info("api-spend", "api", "Spend today", _spend_line(r)))

    out.append(_choice("splash", "terminal", "Opening animation", SPLASH, "on" if cli.splash_on() else "off"))
    out.append(_probe_row())
    order = {g: i for i, g in enumerate(GROUPS)}
    out.sort(key=lambda x: order[x["group"]])  # stable: each group's rows keep their order
    return {"at": time.monotonic(), "rows": out, "routes": r, "models": m}


def _fresh(fresh: bool) -> dict:
    global _cache
    with _READ:
        c = _cache
        if fresh or c is None or time.monotonic() - c["at"] >= ROWS_CACHE_S:
            c = _cache = _build()
        return c


def rows(fresh: bool = False) -> list[dict]:
    """Every admin row, in GROUPS' order; kept ROWS_CACHE_S, `fresh` reads again."""
    return copy.deepcopy(_fresh(fresh)["rows"])


def reads(fresh: bool = False) -> dict:
    """The rows' reads, from the same cache: {"routes": `wb route --json` or None, "models": `wb
    models --json` or None}, for the Stats view's names (#1280's GET /api/stats)."""
    c = _fresh(fresh)
    return copy.deepcopy({"routes": c["routes"], "models": c["models"]})


def drop() -> None:
    """Forget the cached rows: the next rows() reads again."""
    global _cache
    with _READ:
        _cache = None


def _number_value(value) -> float | None:
    """A number row's value: a positive finite number, or None for off (None, "", "off")."""
    if value is None or (isinstance(value, str) and value.strip().lower() in ("", "off")):
        return None
    if isinstance(value, bool):
        raise ValueError("a number, or off")
    try:
        v = float(value.strip().replace(",", ".")) if isinstance(value, str) else float(value)
    except (TypeError, ValueError):
        raise ValueError("a number, or off") from None
    if not (v > 0 and math.isfinite(v)):
        raise ValueError("a positive number, or off")
    return v


def _argv(row: dict, value) -> list[str]:
    """The wb call for ``value`` on ``row`` (the pane's), after the value is checked: ValueError
    when it isn't one the row takes."""
    key, kind = row["key"], row["kind"]
    if kind in ("info", "note"):
        raise ValueError(f"{key} is read-only")
    if kind == "number":
        v = _number_value(value)
        amount = "off" if v is None else f"{v:g}"
        return ["route", "cap", amount] if key == "cap" else ["savings", "--price", amount, "--json"]
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)  # Live refresh's 1, 2 or 5 posted as a number
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} takes one of its choices")
    value = value.strip()
    if value not in {c["value"] for c in row["choices"]}:
        # any id where the row says so; never one wb would read as a flag
        if not row.get("other") or value.startswith("-") or any(ch.isspace() for ch in value):
            raise ValueError(f"{value!r} isn't one of {key}'s choices")
    head, _, rest = key.partition("-")
    if key in ("agents", "busy"):
        return ["route", "all" if key == "agents" else "busy", value]
    if key in ("coding", "workflows", "progress", "splash"):
        return [key, value]
    if key in ("record", "live", "every"):
        return ["stats", "set", "recording" if key == "record" else key, value]
    if key == "probe":
        return ["experiment", "probe", value]
    if key == "optimize":
        return ["optimize", "start"]
    if head == "role":
        return ["route", rest, value]
    if head == "gpu":
        return ["route", rest, "--gpu", value]
    if head == "api":
        return ["route", rest, "--api", value]
    if head == "think":
        return ["route", rest, "--thinking", value]
    if head == "size":
        return ["model", "sizes", rest, *([] if value == "all" else [value])]
    if head == "media":
        return [next(cmd for kind, _, cmd in MEDIA if kind == rest), "--default", value]
    raise KeyError(key)  # a row this table makes but writes nothing for: never shipped


def _said(argv: list[str], ok: bool, out: str, err: str) -> str:
    said = _last(out) if ok else (_last(err) or _last(out))
    if ok and argv[0] == "savings":  # --json: the price, not the week's whole document
        try:
            p = json.loads(out).get("price")
        except (ValueError, AttributeError):
            p = None
        said = f"electricity price: {p:g} per kWh" if isinstance(p, (int, float)) else "electricity price: not set"
    return said or ("done" if ok else "wb refused, and said nothing")


def apply(key: str, value) -> dict:
    """Set ``key`` to ``value`` through its wb command: {"ok", "said", "help"?}. KeyError for no
    such row, ValueError for a value it doesn't take (nothing runs for either); `ok` false is wb's
    refusal, `said` its line. `help`, when the value chosen needs Claude's help to work (HELP)."""
    row = next((x for x in _fresh(False)["rows"] if x["key"] == key), None)
    if row is None:
        raise KeyError(key)
    argv = _argv(row, value)
    with _WRITE:
        try:
            ok, out, err = _run(argv, WRITE_TIMEOUT_S)
        finally:
            drop()
    result = {"ok": ok, "said": _said(argv, ok, out, err)}
    if ok and key in HELP and (note := HELP[key](argv[-1], result["said"])):
        result["help"] = note
    return result
