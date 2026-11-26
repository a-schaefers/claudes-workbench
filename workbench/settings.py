"""The admin rows (2.0, #1279): every row the pane's Settings tab had, and the wb call each one
makes, in one table the chat server renders the web app's Settings from. A new setting touches only
this file.

A row is a JSON-safe dict: `key` (the pane's name), `group` (GROUPS), `label`, `kind` (KINDS), and
by kind: a `choice` its `value`, `choices` [{value, label, says}] and `says` (the sentence for the
current value), with `other: true` where any model id goes (the role command checks it); a
`number` its `value` (a float, or None for off) and `unit` (`usd`); an `info` its
`text`; an `action` its `text` and its `choices`, which are buttons; a `note` one line for the group
(NO_PROVIDER or CATALOG_FAILED, OLDER_WB or wb's own reason), drawn in its place beside the group's other rows. A row may
carry `help`: the note for Claude while the setting is on but can't work yet (probe not set up),
which apply() also returns when that value was just chosen; the chat server records it, and the
mod turns it into a [workbench] turn where Claude offers to help (#1279's addendum, the owner,
2026-11-24: "the terminal still offers to help").

Reads: `wb route --json` and `wb models --json` as children (the JSON the pane read), the small
files in-process through their own modules; kept ROWS_CACHE_S. Writes: the pane's wb command, as a
child of child_env() (no CLAUDECODE, CLAUDE_PID, CLAUDE_CODE_SESSION_ID or OPENCODE_SESSION_ID, WB_ACTOR=web), one at a
time, then the cache is dropped. The user's click on the web app is the yes, as a command they
type at their terminal is: the child isn't Claude, so `wb route` doesn't gate it, and no approval
file is written. It stamps a role change `by: "web"` (WB_ACTOR; CLAUDECODE, or an OpenCode driver's
session id, wins over it).

Claude writes through the same apply() (#1287, `wb settings set`, on the user's clear ask and yes
in the conversation: the owner, 2026-11-24, "We trust Claude"), with by="claude": its child keeps
Claude's environment, so a row that moves a role to API, or any other change `wb route` gates,
still waits for the mod's dialog (route-ok.json), as `wb route` typed by Claude does. Each change
that went through is a row of settings-changes.jsonl (record()), `by` who made it.
"""
from __future__ import annotations

import copy
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from . import chat, cli, context, driver, ocplugin, progress, routes, savings, store

GROUPS = ("shared", "gpu", "api", "setup", "terminal", "experimental")  # display order
KINDS = ("choice", "number", "info", "action", "note")
ACTOR = "WB_ACTOR"  # env var; "web" stamps a role change by="web" (cli._route_apply)
ROWS_CACHE_S = 10  # the pane's ROLES_CACHE_MS
READ_TIMEOUT_S = 15  # wb route --json, wb models --json (the pane's 15 s)
WRITE_TIMEOUT_S = 60
WB = Path(__file__).resolve().parent.parent / "bin" / "wb"  # the clone's launcher; tests swap it
API_MENU = 12  # an API row lists at most this many models, the ones in use first (the pane's)

OLDER_WB = "wb is older than the mod: wb update"
NO_PROVIDER = "No API provider connected: /connect in OpenCode adds one; every role runs on the GPU."
# in NO_PROVIDER's place when OpenCode's catalog couldn't be read: /connect was no fix for a
# server that couldn't find OpenCode, whose keys were stored (#1342)
CATALOG_FAILED = "OpenCode's catalog couldn't be read, so no API provider shows: {why}"
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
PROGRESS_OFF = ("off", "Off (default)", "No progress checks: each one wakes Claude, which spends its tokens.")
THINK = [("off", "Off (default)", "Faster and cheaper; for tasks started after this."),
         ("on", "On", "Better on hard steps, slower and costs more; for tasks started after this.")]
# a role's `thinking_review` (#1059): only a role with an API model gets the row
REVIEW = [("off", "Off (default)", "An API task skips its own review round: Claude reviews its work."),
          ("on", "On", "With thinking off, an API task reviews its own change in one more round before "
           "Claude, that round alone thinking: it costs more; for tasks started after this.")]
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


BY = ("web", "claude")  # apply()'s writers: the web app's click, Claude's `wb settings set`


def child_env(by: str = "web") -> dict[str, str]:
    """The environment of every wb the server runs: never Claude's (a server started under Claude
    Code carries CLAUDECODE=1, and wb would then gate the user's own click), marked as the web's.
    by="claude" (`wb settings set`): the caller's own, unmarked, so wb gates it as it gates Claude."""
    if by == "claude":
        return {k: v for k, v in os.environ.items() if k != ACTOR}
    env = {k: v for k, v in os.environ.items() if k not in chat.CLAUDE_VARS}  # what chat.start scrubs
    env[ACTOR] = "web"
    return env


def _run(argv: list[str], timeout: float, by: str = "web") -> tuple[bool, str, str]:
    """wb ARGV as a child of child_env(by): (ok, stdout, stderr); a timeout or a launch failure is
    not ok, its reason in stderr."""
    try:
        p = subprocess.run([sys.executable, "-X", "utf8", str(WB), *argv], env=child_env(by),
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
    row = {**row, "thinking_review": "off"}  # the coding rounds' thinking alone: its own row says the review's
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
    # the `≈` alone says the basis: a "(catalog prices)" note after it said it twice (#1322)
    each = f" ({' · '.join(by)})" if by else ""
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


# The driver row (#1316): the standing default routes.json's `driver` holds, which the OpenCode
# plugin's /workbench applies to its session; each session's own /models is the default. Claude
# Code's driver is Claude Code: the row reads so where the OpenCode plugin isn't installed.
DRIVER_SESSION = (routes.DRIVER_SESSION, "Each session's own (default)",
                  "Each OpenCode session's driver is the model you pick in its /models; Workbench pins "
                  f"none. Under Claude Code the driver is {driver.CLAUDE_CODE}.")
DRIVER_CLAUDE = (f"{driver.CLAUDE_CODE}: Claude has the final say. Workbench inside OpenCode lets you "
                 "pick the driver from its /models.")


def _opencode_path() -> bool:
    """Whether the OpenCode plugin is installed (ocplugin.installs): the OpenCode path, whose
    driver the user picks. A config that can't be read counts as not: the row then only reads."""
    try:
        return any(f["kind"] != "off" for f in ocplugin.installs())
    except (RuntimeError, OSError):
        return False


def _driver_row(r: dict, m: dict | None) -> dict:
    """The Driver row: an `other` choice (any OpenCode id, `#variant` allowed) of each session's
    own, the GPU models, then API models as the API rows list them; else, without the OpenCode
    plugin, an info row reading Claude Code (fixed)."""
    if not _opencode_path():
        return _info("driver", "shared", "Driver", DRIVER_CLAUDE)
    pin = r.get("driver") or routes.DRIVER_SESSION
    stops = [DRIVER_SESSION]
    for g in (m or {}).get("gpu") or []:
        stops.append((g["id"], f"{g.get('label') or g['id']}{_window(g.get('window'))} · GPU",
                      f"/workbench in OpenCode switches its session to {g['id']}, on this machine's GPU."))
    api = {x["id"]: x for x in (m or {}).get("api") or [] if isinstance(x, dict) and x.get("id")}
    for mid, label, _ in _api_stops("The driver", m, "", []):
        x = api.get(mid) or {}
        who = x.get("provider_name") or x.get("provider") or mid.split("/", 1)[0]
        stops.append((mid, label, f"/workbench in OpenCode switches its session to {mid}; that "
                                  f"conversation, your code in it, goes to {who}."))
    if pin not in {v for v, _, _ in stops}:
        stops.append((pin, pin, f"/workbench in OpenCode switches its session to {pin}."))
    return _choice("driver", "shared", "Driver", stops, pin, other=True)


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
    if r is not None:
        out.append(_driver_row(r, m))
    out.append(_choice("coding", "shared", "Claude coding", CODING, cli.coding_mode()))
    out.append(_choice("workflows", "shared", "Dynamic workflows", WORKFLOWS, cli.workflows_mode()))
    if r is not None:
        cap = r.get("day_usd")
        out.append(_number("cap", "shared", "API day cap", float(cap) if cap is not None else None, "usd",
                           "Needed before an API role; $5 suggested." if cap is None else
                           f"API tasks stop for the day at {routes.money(cap)} of spend."))
    # Stats recording, Live view, Live refresh and the electricity price had rows here, as the
    # pane's Settings tab did until #1322 dropped them (recording always on; wb stats set and wb
    # savings --price keep the rest): a `recording` row read a key stats.settings() no longer has
    prog = progress.settings()
    after = progress.user_after(prog) or 0
    out.append(_choice("progress", "shared", "Progress checks", _progress_stops(after),
                       "on" if prog.get("on") is True else "off"))

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
        # a key that runs tasks: a stored one, Ollama cloud's login, Copilot's sign-in, which the
        # keyhole holds as a key (#1191), or an exported one it can read (`runs`, #1189)
        stored = any(p.get("key") in ("stored", "ollama", "copilot") or p.get("runs") is True
                     for p in provs.values() if isinstance(p, dict))
        if not stored and not any(row.get("api") for _, _, row in roles):
            err = r.get("catalog_error")
            out.append(_note("api-none", "api", CATALOG_FAILED.format(why=err) if err else NO_PROVIDER))
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
            for name, label, row in roles:
                if row.get("api"):
                    out.append(_choice(f"review-{name}", "api", f"{label} self-review thinking", REVIEW,
                                       row.get("thinking_review") or "off"))
            out.append(_info("api-spend", "api", "Spend today", _spend_line(r)))

    out += setup_rows()
    out.append(_choice("splash", "terminal", "Opening animation", SPLASH, "on" if cli.splash_on() else "off"))
    out.append(_probe_row())
    order = {g: i for i, g in enumerate(GROUPS)}
    out.sort(key=lambda x: order[x["group"]])  # stable: each group's rows keep their order
    return {"at": time.monotonic(), "rows": out, "routes": r, "models": m}


def setup_rows() -> list[dict]:
    """The "What your agents get" group (#1295, P1): read-only info rows from context.agent_setup,
    what reaches every agent before a task's own files and what is kept from them, so the user
    (and Claude) can see a global OpenCode skill, instruction or plugin tool that reaches them."""
    try:
        said = context.setup_said(context.agent_setup())
    except (OSError, RuntimeError) as e:
        return [_note("setup-why", "setup", f"What your agents get can't be read: {e}")]
    lines = [("setup-instructions", "Instructions", said["instructions"],
              "and each project's CLAUDE.md, AGENTS.md and rules"),
             ("setup-skills", "Skills", said["skills"],
              f"and what Claude hands a task from {said['library'] or 'the skill library'}"),
             ("setup-plugins", "Plugins", said["plugins"], "allowed: yours"),
             ("setup-runs", "Also running", said["runs"], ""),
             ("setup-kept", "Kept from agents", said["kept"], "")]
    out = [_info(key, "setup", label, f"{label}: {', '.join(got)}" + (f"; {tail}." if tail else ".")
                 if got else f"{label}: {tail.removeprefix('and ')}.")
           for key, label, got, tail in lines if got or key in ("setup-instructions", "setup-skills")]
    out.append(_info("setup-size", "setup", "Size", f"About {said['tokens'] / 1000:.1f}K tokens, with "
                     "OpenCode's own prompt and tools, before a task's project files and spec."))
    return out


def _fresh(fresh: bool) -> dict:
    global _cache
    with _READ:
        c = _cache
        if fresh or c is None or time.monotonic() - c["at"] >= ROWS_CACHE_S:
            c = _cache = _build()
        return c


def rows(fresh: bool = False) -> list[dict]:
    """Every admin row, in GROUPS' order; kept ROWS_CACHE_S, `fresh` reads again. A row changed
    within CHANGED_FOR carries `changed` {ts, by}: its newest change (#1355)."""
    out = copy.deepcopy(_fresh(fresh)["rows"])
    last = changed()
    for r in out:
        if r.get("key") in last:
            r["changed"] = last[r["key"]]
    return out


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
        return ["route", "cap", amount]
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)  # a choice posted as a number
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
    if key == "probe":
        return ["experiment", "probe", value]
    if key == "optimize":
        return ["optimize", "start"]
    if key == "driver":
        return ["route", "driver", value]
    if head == "role":
        return ["route", rest, value]
    if head == "gpu":
        return ["route", rest, "--gpu", value]
    if head == "api":
        return ["route", rest, "--api", value]
    if head == "think":
        return ["route", rest, "--thinking", value]
    if head == "review":
        return ["route", rest, "--thinking-review", value]
    if head == "size":
        return ["model", "sizes", rest, *([] if value == "all" else [value])]
    if head == "media":
        return [next(cmd for kind, _, cmd in MEDIA if kind == rest), "--default", value]
    raise KeyError(key)  # a row this table makes but writes nothing for: never shipped


def _said(argv: list[str], ok: bool, out: str, err: str) -> str:
    said = _last(out) if ok else (_last(err) or _last(out))
    return said or ("done" if ok else "wb refused, and said nothing")


def _row(key: str) -> dict:
    row = next((x for x in _fresh(False)["rows"] if x["key"] == key), None)
    if row is None:
        raise KeyError(key)
    return row


def apply(key: str, value, by: str = "web") -> dict:
    """Set ``key`` to ``value`` through its wb command: {"ok", "said", "help"?}. KeyError for no
    such row, ValueError for a value it doesn't take (nothing runs for either); `ok` false is wb's
    refusal, `said` its line. `help`, when the value chosen needs Claude's help to work (HELP).
    `by` (BY): "web", the user's click, never gated; "claude", run as its caller (child_env)."""
    if by not in BY:
        raise ValueError(f"by is one of {', '.join(BY)}")
    argv = _argv(_row(key), value)
    with _WRITE:
        try:
            ok, out, err = _run(argv, WRITE_TIMEOUT_S, by)
        finally:
            drop()
    result = {"ok": ok, "said": _said(argv, ok, out, err)}
    if ok and key in HELP and (note := HELP[key](argv[-1], result["said"])):
        result["help"] = note
    return result


def dry_run(key: str, value) -> tuple[int, str, str]:
    """What apply(key, value, by="claude") would ask the mod's dialog: for a row whose wb call is
    a `wb route`, that call's `--dry-run --json` (exit code, stdout, stderr), else no gate
    (`{"change": "", "gate": false}`). The mod shows its `change` and, on the user's Allow,
    writes its `approval` to route-ok.json, which the real call then takes (#1287). KeyError and
    ValueError as apply's."""
    argv = _argv(_row(key), value)
    if argv[0] != "route":
        return 0, json.dumps({"change": "", "gate": False}), ""
    ok, out, err = _run(["route", "--dry-run", "--json", *argv[1:]], WRITE_TIMEOUT_S, "claude")
    return (0 if ok else 3), out, err


def _same(a, b) -> bool:
    """Whether a row's value ``a`` and the value set ``b`` are one (`5` and `5.0`, None and "off")."""
    na, nb = (None if v is None or (isinstance(v, str) and v.strip().lower() in ("", "off")) else v for v in (a, b))
    if na is None or nb is None:
        return na is nb
    try:
        return float(na) == float(nb)
    except (TypeError, ValueError):
        return str(na) == str(nb)


def record(before: dict, key: str, value, done: dict, by: str) -> dict | None:
    """Append a change that went through to settings-changes.jsonl (chat.CHANGES): {ts, key,
    label, from, to, said, help?, by}, ``before`` the row as it was, and stamp chat/settings-rev.json
    with it, which wakes every open page's stream (the page draws who changed what, #1355). The mod
    tells Claude of the web's (#1283); Claude's own (by "claude") it made itself. None, nothing
    written, for a choice or a number set to the value it had: no change to tell anyone of."""
    if before.get("kind") in ("choice", "number") and _same(before.get("value"), value):
        return None
    entry = {"ts": store.now(), "key": key, "label": before.get("label") or key, "from": before.get("value"),
             "to": value, "said": done["said"], **({"help": done["help"]} if done.get("help") else {}), "by": by}
    store.append_jsonl(chat.changes_path(), entry)
    store.write_json(chat.chat_dir() / REV, {k: entry[k] for k in ("ts", "key", "label", "to", "by")})
    return entry


# Who changed a setting, as the page shows it (#1355, "A Settings change made while Claude is
# mid-turn, or by Claude itself, shows who changed what and when, and needs no reload"): each
# row's newest change within CHANGED_FOR (`changed`), the newest CHANGES_SHOWN (`changes`), and
# chat/settings-rev.json, the newest change's {ts, key, label, to, by}, which the chat's state
# carries as `settings` so an open page reads its rows again. Last writer wins: no lock.
REV = "settings-rev.json"  # under chat/ (chat.SETTINGS_REV, which the state reads)
CHANGED_FOR = 7 * 86400
CHANGES_SHOWN = 10
CHANGES_TAIL = 64 << 10


def changes(n: int = CHANGES_SHOWN) -> list[dict]:
    """The newest ``n`` changes, newest first: {ts, key, label, from, to, by}."""
    out = []
    for r in reversed(store.read_jsonl_tail(chat.changes_path(), CHANGES_TAIL)):
        if not isinstance(r, dict) or not isinstance(r.get("ts"), (int, float)) or not isinstance(r.get("key"), str):
            continue
        out.append({k: r.get(k) for k in ("ts", "key", "label", "from", "to", "by")})
        if len(out) >= n:
            break
    return out


def changed(now: float | None = None) -> dict[str, dict]:
    """Each key's newest change within CHANGED_FOR: {key: {ts, by}}."""
    now = store.now() if now is None else now
    out: dict[str, dict] = {}
    for r in store.read_jsonl_tail(chat.changes_path(), CHANGES_TAIL):
        if isinstance(r, dict) and isinstance(r.get("key"), str) and isinstance(r.get("ts"), (int, float)) \
                and now - r["ts"] <= CHANGED_FOR:
            out[r["key"]] = {"ts": r["ts"], "by": str(r.get("by") or "")}
    return out


def rev() -> dict | None:
    """chat/settings-rev.json: the newest change's {ts, key, label, to, by}, or None."""
    return chat.settings_rev()


# The settings offer (#1334, the OpenCode plugin's idle check; kernel/BOUNDARY.md: it stays in
# wb): each change the user made in the web app's Settings reaches the driver once. One with
# `help` (the value just chosen is on but can't work yet) is a turn that asks the driver to offer
# to set it up; any other a row the conversation reads, starting no turn, but the rows whose own
# notes say every change (SAID_ELSEWHERE). The Claude Code mod's settingsTurns keeps the same rule
# and words in register.tsx with its mark in its own store, so each host offers a row once.
OFFERED = "settings-offered.json"  # under $WB_HOME: {ts}, the newest row `wb settings offers --if-due` gave
OFFER_TAIL = 64 << 10  # the mod's SETTINGS_TAIL
OFFER_MAX_AGE_S = 3600  # the mod's OLDEST_WAKE_MS: a row older is no news
SAID_ELSEWHERE = re.compile(r"(?:agents|busy|cap|coding|workflows|(?:role|gpu|api|think)-[a-z]+)")


def _one(v, n: int) -> str:
    return " ".join(str("" if v is None else v).split())[:n]


def _value_word(v) -> str:
    return "off" if v is None or v == "" else _one(v, 120)


def change_said(r: dict) -> str:
    """A web change as the driver is told it (the mod's settingSaid)."""
    said = _one(r.get("said"), 300)
    return (f"The user changed {_one(r.get('label'), 80) or _one(r.get('key'), 80)} from "
            f"{_value_word(r.get('from'))} to {_value_word(r.get('to'))} on the web app."
            + (f" wb said: {said}" if said else ""))


def offers(spend: bool, now: float | None = None) -> dict:
    """The web changes due a word (`wb settings offers`): {"offers": [{ts, key, label, text,
    toast}], "notes": [{ts, key, text}]}, from settings-changes.jsonl's rows `by: "web"` newer
    than settings-offered.json's mark and under OFFER_MAX_AGE_S old. ``spend`` (--if-due) moves
    the mark past them under its lock, so one host's servers say each once."""
    now = store.now() if now is None else now
    mark_path = store.home() / OFFERED
    with store.locked(store.home() / "locks" / "settings-offered.lock"):
        mark = float((store.read_json(mark_path, {}) or {}).get("ts") or 0)
        got: dict = {"offers": [], "notes": []}
        newest = mark
        for r in store.read_jsonl_tail(chat.changes_path(), OFFER_TAIL):
            if not isinstance(r, dict) or r.get("by") != "web":
                continue
            ts = r.get("ts")
            if not isinstance(ts, (int, float)) or ts <= mark or ts < now - OFFER_MAX_AGE_S:
                continue
            newest = max(newest, float(ts))
            key, label = _one(r.get("key"), 80), _one(r.get("label"), 80) or _one(r.get("key"), 80)
            help = r.get("help").strip() if isinstance(r.get("help"), str) else ""
            if help:
                got["offers"].append({
                    "ts": ts, "key": key, "label": label,
                    "text": f"[workbench] {change_said(r)}\n{help[:4000]}\n"
                            "Offer to help with it, and install or change nothing without their yes.",
                    "toast": f"{label} changed on the web app: the driver will offer to set it up"})
            elif not SAID_ELSEWHERE.fullmatch(key):
                got["notes"].append({"ts": ts, "key": key, "text": change_said(r)})
        if spend and newest > mark:
            store.write_json(mark_path, {"ts": newest})
    return got
