"""The admin rows (2.0, #1279): every row the pane's Settings tab had, and the wb call each one
makes, in one table the chat server renders the web app's Settings from. A new setting touches only
this file.

A row is a JSON-safe dict: `key` (the pane's name), `group` (GROUPS), `label`, `kind` (KINDS), and
by kind: a `choice` its `value`, `choices` [{value, label, says, group?}] (`group`: an API model's provider,
or IN_USE, drawn as an <optgroup>) and `says` (the sentence for the
current value), with `other: true` where any model id goes (the role command checks it); a
`number` its `value` (a float, or None for off) and `unit` (`usd`); an `info` its
`text`; an `action` its `text` and its `choices`, which are buttons; a `note` one line for the group
(NO_PROVIDER or CATALOG_FAILED, OLDER_WB or wb's own reason), drawn in its place beside the group's other rows.
`more: true`, a row the page draws under its card's More (a role's GPU and API model and thinking);
`hidden: true`, a row the page doesn't draw while it can do nothing (`wb settings` still lists it); `fixed:
true`, a choice the page draws as its `says` alone (a role's GPU|API with no API provider); a choice's
`needs`, the row whose value it needs first (a role's API, its `api-<role>`: the page opens that row's
list under it instead of sending); `ask`, a row
that carries a problem or a request, which the page gives an "Ask <driver>" link filling the chat's box
(true: "Help me with Settings › <its card>: <its line>"; a string: that request); a note, or a row with
`help`, gets the link without it. `brief`, what a folded card's one-line summary says of the row ("" for
nothing; without it, a choice's `<label>: <its choice>`). A row may carry `help`: the note for Claude while the setting is on but can't work yet (probe not set up),
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

from . import chat, cli, context, driver, ocplugin, progress, routes, runs, savings, store

# display order. A card per role first (#1553, 2026-11-26): its
# GPU or API, then its GPU model, API model and thinking under More (`more`), where one role's
# settings were spread over three cards, its API model 2,000 px below its GPU|API choice
GROUPS = (*routes.ROLES, "shared", "gpu", "api", "setup", "workflows", "terminal", "experimental")
KINDS = ("choice", "number", "info", "action", "note", "form")  # form: Add a role's fields, #1625
ACTOR = "WB_ACTOR"  # env var; "web" stamps a role change by="web" (cli._route_apply)
ROWS_CACHE_S = 10  # the pane's ROLES_CACHE_MS
READ_TIMEOUT_S = 15  # wb route --json, wb models --json (the pane's 15 s)
WRITE_TIMEOUT_S = 60
WB = Path(__file__).resolve().parent.parent / "bin" / "wb"  # the clone's launcher; tests swap it
IN_USE = "In use"  # an API row's first group: the role's own model, the other roles', those used (#1617)

OLDER_WB = "wb is older than the mod: wb update"
NO_PROVIDER = "No API provider connected: /connect in OpenCode adds one; every role runs on the GPU."
# in NO_PROVIDER's place when OpenCode's catalog couldn't be read: /connect was no fix for a
# server that couldn't find OpenCode, whose keys were stored (#1342)
CATALOG_FAILED = "OpenCode's catalog couldn't be read, so no API provider shows: {why}"
# With no model discover() finds: a next step, where "No GPU model found (wb models)" left a first
# `wb start` to exit 3 with the page never saying why (the web app's review, 2026-11-26). {who}: the
# driver to ask, as GPU_ONLY's; the note's Ask link asks for the local one (NO_GPU_ASK)
NO_GPU = "No GPU model yet: pick an API model for a role above (its More), or ask {who} to set up a local one."
NO_GPU_ASK = "Help me set up a local model"
# With no API provider, the one line that stands for every API control (the roles' API choice, Agents,
# When the GPU is busy, the day cap), which the page then hides (`hidden`, `fixed`): six API controls
# came before the line saying there was no provider, on the GPU-only path (the web app's review,
# 2026-11-26). The rows stay, for `wb settings`. {who}: the driver to ask
GPU_ONLY = "Every role runs on your GPU. To add an API model: /connect in OpenCode, or ask {who}."

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
# The workflows group (#1278's S12): deepmode, a row per run not closed (runs.snapshot) with its
# Stop and its Summary (the morning's look at an overnight run), and the flows there are. A run row
# is no setting: neither is recorded as a change (record), and the web's Stop is said to the run's
# session as its own notice (runs.cmd_stop under WB_ACTOR=web)
RUN_STOP = ("stop", "Stop", "Stops the run (wb run stop): done work is kept, and the session that started it is told.")
RUN_SUMMARY = ("summary", "Summary", "What the run has returned so far (wb run report), shown below.")
RUN_DETAIL_CHARS = 16_000  # the Summary's text past this is cut: wb run report <run> has it all
# key prefixes whose action changes no setting (record): a run's Stop or Summary, and Optimize's
# Measure now, a button logged "Optimize: off → start", from a value that never was (the web app's
# review, 2026-11-26)
NOT_RECORDED = ("run-",)
FLOWS_SAY = "Ask for one by name, or put deepcode in a message with your request."
# which side a deep workflow's items run on (#1627): `wb workflows side <value>`, routes.run_side
DEEP_SIDES = [("roles", "As each role says (default)", "Each item runs on its role's own side, GPU or API."),
              ("gpu", "GPU only", "Every item runs on the GPU, on its role's GPU model or the router's pick: "
               "nothing is paid."),
              ("api", "API only", "Every item runs on its role's API model, within the day cap; a role "
               "without one can't take part.")]
PROGRESS_OFF = ("off", "Off (default)", "No progress checks: each one wakes Claude, which spends its tokens.")
THINK = [("off", "Off (default)", "Faster and cheaper; for tasks started after this."),
         ("on", "On", "Better on hard steps, slower and costs more; for tasks started after this.")]
# a role's `thinking_review` (#1059): only a role with an API model gets the row
REVIEW = [("off", "Off (default)", "An API task skips its own review round: Claude reviews its work."),
          ("on", "On", "With thinking off, an API task reviews its own change in one more round before "
           "Claude, that round alone thinking: it costs more; for tasks started after this.")]
SIZES = [("all", "Any size (default)", "For roles on Auto, the router may give it any task."),
         ("trivial", "Trivial only", "For roles on Auto, the router gives it trivial coding tasks only."),
         ("significant", "Complex only", "For roles on Auto, the router gives it complex coding tasks only.")]
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


def help_now(key: str) -> str | None:
    """The note HELP gives ``key`` as it stands now (its value and wb's line for it now), else None:
    an offer older than OFFER_MAX_AGE_S is made only while this still says one (#1557's review,
    2026-11-26: a help row days old was offered after probe was set up outside the offer, an untrue offer that started a paid turn). `wb settings help` prints it for
    the mod. A key added to HELP needs its own case here, or its old offers are never made."""
    try:
        if key == "probe":
            return _probe_row().get("help")
    except (chat.ConfigError, OSError, ValueError):
        return None
    return None


BY = ("web", "claude")  # apply()'s writers: the web app's click, Claude's `wb settings set`

# Each row's label, as the web app shows it and settings-changes.jsonl records it: the fixed ones
# here, a role's from routes.LABELS (label_of), a model's tasks row from its id
LABELS = {"agents": "Agents", "busy": "When the GPU is busy", "driver": "Driver", "coding": "Claude coding",
          "add-role": "Add a role",
          "workflows": "Dynamic workflows", "deep-side": "Deep workflows run on", "cap": "API day cap", "progress": "Progress checks",
          "splash": "Opening animation", "probe": "Agent code navigation",
          "optimize": "Optimize"}
ROLE_LABELS = {"role": "{}", "gpu": "{} GPU model", "api": "{} API model", "think": "{} thinking",
               "review": "{} self-review thinking"}


def label_of(key: str) -> str:
    """Row ``key``'s label (a model's tasks row by its id: the web app's names it by its label)."""
    head, _, rest = key.partition("-")
    if key in LABELS:
        return LABELS[key]
    if head in ROLE_LABELS and rest in routes.LABELS:
        return ROLE_LABELS[head].format(routes.LABELS[rest])
    if head in ROLE_LABELS and rest in routes.all_roles():  # the user's own, by its label (#1625)
        return ROLE_LABELS[head].format(routes.label(rest))
    if head == "remove" and rest in routes.all_roles():
        return f"Remove {routes.label(rest)}"
    if head == "size":
        return f"{rest} tasks"
    if head == "media":
        return next((lab for kind, lab, _ in MEDIA if kind == rest), key)
    return key


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
    # a stop is (value, label, says) or (value, label, says, group): an API model's provider, which
    # the page draws as the select's <optgroup> (#1617)
    choices = [{"value": v, "label": lab, "says": says, **({"group": g[0]} if g else {})} for v, lab, says, *g in stops]
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


def _role_stops(label: str, api: str | None, name: str | None, no_gpu: bool = False) -> list:
    """A role's GPU|API; ``no_gpu``: discover() finds no model, so a task on the GPU can't start."""
    return [("gpu", "GPU", f"{label} runs on the GPU, which has no model yet: its tasks can't start."
             if no_gpu else f"{label} runs on this machine's GPU."),
            ("api", f"API ({name or api})" if api else "API (pick its model)",
             f"{label} runs on {api}; your code goes to its provider." if api else "Pick its API model first.")]


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
    """Every API model of `wb models --json`'s `api`, each (value, label, says, group): the role's own
    model, the other roles' and those used before first (IN_USE), then one group per provider, by
    its name, its models by name; any other id goes (`other`). A cap of 12 cut the rest (#1617,
    2026-11-27): with 111 models Anthropic's 18 filled the list, and DeepInfra's others and every
    xAI model never showed."""
    listed = {x["id"]: x for x in (m or {}).get("api") or [] if isinstance(x, dict) and x.get("id")}

    def who(x: dict) -> str:
        return str(x.get("provider_name") or x.get("provider") or x["id"].split("/", 1)[0])

    def line(mid: str, group: str) -> tuple:
        x = listed.get(mid)
        if not x:
            return mid, mid, f"{label} uses {mid}.", group
        p = x.get("price") or {}
        price = f" · ${p['input']}/${p['output']} per M" \
            if all(isinstance(p.get(k), (int, float)) for k in ("input", "output")) else ""
        free = f" · {(m or {}).get('free_line') or 'free'}" if x.get("free") else ""
        return (mid, f"{x.get('name') or mid} · {who(x)}{_window(x.get('window'))}{price}{free}",
                f"{label} uses {mid} on API; your code goes to {who(x)}.", group)
    ids: list[str] = []
    for mid in [current, *others, *[u for u in (m or {}).get("used") or [] if u in listed]]:
        if mid and mid not in ids:
            ids.append(mid)
    rest = sorted((x for x in listed.values() if x["id"] not in ids),
                  key=lambda x: (who(x).casefold(), str(x.get("name") or x["id"]).casefold(), x["id"]))
    return [line(mid, IN_USE) for mid in ids] + [line(x["id"], who(x)) for x in rest]


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


def _cap_row(r: dict, quiet: dict) -> dict:
    """The API day cap: a number of dollars, or off (`wb route cap off`) at any time, even with
    roles on API (#1624, the owner, 2026-11-27: "what if i was rich and didnt care?"). In the API
    card, beside the providers: in Agents and roles it went unfound."""
    cap, spent = r.get("day_usd"), _spent_today(r)
    so_far = f": {spent} so far today." if spent else "."
    says = (f"API tasks stop for the day at {routes.money(cap)} of spend{so_far}" if cap is not None else
            f"Off: API tasks spend with no daily limit{so_far}" if r.get("no_cap") else
            f"None yet: the first API role takes ${routes.DAY_USD_SUGGEST}, which you can change or turn off.")
    return {**_number("cap", "api", LABELS["cap"], float(cap) if cap is not None else None, "usd", says), **quiet}


def _spend_brief(r: dict) -> str:
    """Today's API spend for the API card's summary: `today ≈$0.07 of $5`."""
    s = r.get("spend_today") or {}
    cap = r.get("day_usd")
    return f"today {savings.money(s.get('usd') or 0, s.get('prices') or 'catalog')}" + \
        (f" of {routes.money(cap)}" if cap is not None else "")


def _spent_today(r: dict) -> str | None:
    """Today's API spend, the ledger's (`wb route --json`'s `spend_today`), as the cap row's note
    says it (`≈$0.12`), or None while it says nothing: no role on API and nothing spent. Its own
    "Spend today" row sat 3.4k px down the page from the cap it is checked against (#1339's UX
    review, 2026-11-26); the Stats view's Now says it too, and the API card's folded line
    (`_spend_brief`, on its Connected row)."""
    s = r.get("spend_today") or {}
    total = s.get("usd") if isinstance(s.get("usd"), (int, float)) else 0
    on_api = isinstance(r.get("roles"), dict) and any(
        (r.get("use") if r.get("use") in ("gpu", "api") else (row or {}).get("via")) == "api"
        for row in r["roles"].values() if isinstance(row, dict))  # routes.effective_via
    if not total and not on_api:
        return None
    # the `≈` alone says the basis: a "(catalog prices)" note after it said it twice (#1322)
    return savings.money(total, s.get("prices") or "catalog")


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
    if not _opencode_path():  # the page shows the line only under OpenCode, where it is a choice
        return {**_info("driver", "shared", LABELS["driver"], DRIVER_CLAUDE), "hidden": True}
    pin = r.get("driver") or routes.DRIVER_SESSION
    stops = [DRIVER_SESSION]
    for g in (m or {}).get("gpu") or []:
        stops.append((g["id"], f"{g.get('label') or g['id']}{_window(g.get('window'))} · GPU",
                      f"/workbench in OpenCode switches its session to {g['id']}, on this machine's GPU.", "GPU"))
    api = {x["id"]: x for x in (m or {}).get("api") or [] if isinstance(x, dict) and x.get("id")}
    # its own pin and the roles' API models lead its In use group, as on the roles' rows: with
    # every catalog model listed a pin sank into its provider's group, and wb settings lost its
    # `*` line (#1617's review, 2026-11-27)
    own = pin if pin != routes.DRIVER_SESSION and pin not in {s[0] for s in stops} else ""
    roles_api = [x.get("api") for x in (r.get("roles") or {}).values()  # the user's own too (#1625)
                 if isinstance(x, dict) and x.get("api")]
    for mid, label, _, group in _api_stops("The driver", m, own, roles_api):
        x = api.get(mid) or {}
        who = x.get("provider_name") or x.get("provider") or mid.split("/", 1)[0]
        stops.append((mid, label, f"/workbench in OpenCode switches its session to {mid}; that "
                                  f"conversation, your code in it, goes to {who}.", group))
    if pin not in {s[0] for s in stops}:
        stops.append((pin, pin, f"/workbench in OpenCode switches its session to {pin}."))
    return _choice("driver", "shared", LABELS["driver"], stops, pin, other=True)


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
    row = _choice("probe", "experimental", LABELS["probe"], stops, mode)
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
    n = len(m.get("gpu") or [])
    return {"key": "optimize", "group": "gpu", "label": LABELS["optimize"], "kind": "action",
            "text": f"Optimize: {state}", "brief": f"{n} model{'' if n == 1 else 's'}, {state}",
            "choices": [{"value": "start", "label": "Measure now",
                         "says": "Measures this machine for its models (wb optimize start)."}]}


ADD_ROLE_LIKE = [("significant", "Complex coding", "Prompts, slices and the router's record as complex coding's."),
                 ("trivial", "Trivial coding", "Small slices, as trivial coding's."),
                 ("writing", "English writing", "The writing role's prompt and handbook: prose, not code.")]


def _add_role_row() -> dict:
    """Agents and roles' Add a role (#1625): a form of a name, a label, the built-in it works as and
    what it is for, posted as one JSON string, `wb route add`; the role then has a card of its own."""
    return {"key": "add-role", "group": "shared", "label": LABELS["add-role"], "kind": "form",
            "text": "A role of your own: tasks for it go to the model you give it, GPU or API. It starts on "
                    "the GPU, and the orchestrator agent picks it by name or by what it is for.",
            "fields": [{"name": "name", "label": "Name", "kind": "text", "pattern": routes.CUSTOM_RE.pattern,
                        "hint": "lowercase, as tests or docs-fr"},
                       {"name": "label", "label": "Label", "kind": "text", "max": routes.LABEL_MAX},
                       {"name": "like", "label": "Works as", "kind": "select",
                        "choices": [{"value": v, "label": lab, "says": says} for v, lab, says in ADD_ROLE_LIKE]},
                       {"name": "about", "label": "What it's for", "kind": "text", "max": routes.ABOUT_MAX,
                        "optional": True}],
            "choices": [{"value": "add", "label": "Add", "says": "Adds the role (wb route add)."}]}


def _remove_row(name: str, label: str) -> dict:
    """A role of the user's own's Remove, under its card's More (#1625): `wb route remove`."""
    return {"key": f"remove-{name}", "group": name, "label": f"Remove {label}", "kind": "action", "more": True,
            "text": f"Remove {label}: a role of your own.",
            "choices": [{"value": "remove", "label": "Remove role",
                         "says": "Removes the role; tasks already started keep their model (wb route remove)."}]}


def run_text(r: dict) -> str:
    """A run's line as its row says it (runs.snapshot's row): flow, phase, each stage's counts,
    spend against the budget, and why it isn't running."""
    parts = [f"flow {r.get('flow') or '?'}", str(r.get("phase") or "?") + ("" if r.get("alive") or
                                                                           r.get("phase") not in runs.OPEN else " (no driver)")]
    for sid, c in (r.get("stages") or {}).items():
        bits = [f"{v} {k.replace('_', ' ')}" for k, v in (c or {}).items() if v]
        if bits:
            parts.append(f"{sid}: " + ", ".join(bits))
    b = r.get("budget_usd")
    parts.append(savings.usd(r.get("spent_usd") or 0) + (f" of {savings.usd(b)}" if isinstance(b, (int, float)) else ""))
    if r.get("gpu_min"):
        parts.append(f"GPU {r['gpu_min']:g} min" + (f" of {r['budget_min']:g}" if r.get("budget_min") else ""))
    why = r.get("why") if r.get("phase") != "running" else ""
    return " · ".join(parts) + (f". {why}" if why else "")


def _run_rows() -> list[dict]:
    """A row per run not closed, and the flows there are (runs.flow_rows: shipped and the user's)."""
    out = []
    try:
        snap = runs.snapshot()
    except (OSError, ValueError):
        snap = []
    for r in snap:
        stops = ([RUN_STOP] if r.get("phase") in runs.OPEN else []) + [RUN_SUMMARY]
        out.append({"key": f"run-{r['run']}", "group": "workflows", "label": f"Run {r['run']}", "kind": "action",
                    "text": run_text(r), "brief": f"run {r['run']} {r.get('phase') or '?'}", "choices": [{"value": v, "label": lab, "says": says} for v, lab, says in stops]})
    try:
        names = [f["name"] for f in runs.flow_rows() if f.get("where") != "repo"]
    except (OSError, ValueError):
        names = []
    if names:
        out.append({**_info("flows", "workflows", "Flows", f"Flows: {', '.join(names)}. {FLOWS_SAY}"), "ask": True,
                    "brief": f"{len(names)} flow{'' if len(names) == 1 else 's'}"})
    return out


def _build() -> dict:
    r, why = _read_json(["route", "--json"])
    m, mwhy = _read_json(["models", "--json"])
    if m is not None and not isinstance(m.get("gpu"), list):
        m, mwhy = None, OLDER_WB
    r = r if r is not None and isinstance(r.get("roles"), dict) else None
    out: list[dict] = []
    # the built-ins, then the user's own in routes.json's order (#1625), each by its label
    got = (r or {}).get("roles") or {}
    roles = [(name, routes.LABELS[name], got.get(name) or {}) for name in routes.ROLES] + \
        [(name, str(row.get("label") or name), row) for name, row in got.items()
         if name not in routes.ROLES and isinstance(row, dict) and row.get("custom")]
    api_name = {x.get("id"): x.get("name") for x in (m or {}).get("api") or [] if isinstance(x, dict)}
    provs = (r or {}).get("providers") or {}
    # a key that runs tasks: a stored one, Ollama cloud's login, Copilot's sign-in, which the
    # keyhole holds as a key (#1191), or an exported one it can read (`runs`, #1189)
    stored = any(p.get("key") in ("stored", "ollama", "copilot") or p.get("runs") is True
                 for p in provs.values() if isinstance(p, dict))
    any_api = any(row.get("api") for _, _, row in roles)
    # no API provider (the api group's note alone): GPU_ONLY stands for every API control, which
    # wait until they can do something; not when the catalog couldn't be read (CATALOG_FAILED says why)
    gpu_only = r is not None and not stored and not any_api and not r.get("catalog_error")
    quiet = {"hidden": True} if gpu_only else {}
    no_gpu = m is not None and not m.get("gpu")  # discover() finds none: a GPU role's tasks can't start
    who = "OpenCode" if _opencode_path() else "Claude"

    # the roles, then the rows for every role (shared)
    if r is None:
        out.append(_note("roles-why", "shared", why or OLDER_WB))
    else:
        if gpu_only:
            out.append(_note("gpu-only", "shared", GPU_ONLY.format(who=who)))
        out.append({**_choice("agents", "shared", LABELS["agents"], AGENTS, r.get("use") or "both"), **quiet})
        # only once a role has an API model: before it, "Use the role's API model" did nothing but
        # set a cap (the web app's review, 2026-11-26)
        out.append({**_choice("busy", "shared", LABELS["busy"], BUSY, r.get("busy") or "wait"),
                    **({} if any_api else {"hidden": True})})
        for name, label, row in roles:
            c = _choice(f"role-{name}", name, label_of(f"role-{name}"),
                        _role_stops(label, row.get("api"), api_name.get(row.get("api")), no_gpu), row.get("via") or "gpu")
            # a role on an API model that can't run says why: with no key for its provider the
            # rows said it runs there, and nothing said it couldn't (#1529, 2026-11-26)
            st = row.get("api_status")
            if row.get("via") == "api" and isinstance(st, dict) and st.get("available") is False:
                c["says"] = f"{label} can't run on {row.get('api')}: {st.get('why') or 'wb route says why'}."
                c["ask"] = True  # the fix is a terminal's: the page's Ask link hands it to the driver
            if gpu_only:
                c["fixed"] = True  # its says alone: GPU_ONLY stands for its API choice
            elif not row.get("api"):
                # API with no model: the page opens api-<role>'s list under the row, a pick sends it
                # and then the role's API, where the tap was refused and the list was 2,000 px away,
                # under another card (the web app's review, 2026-11-26; #1558)
                c["choices"][1]["needs"] = f"api-{name}"
            if row.get("custom"):  # the user's own: its card is titled by its label, under the built-ins'
                c["card"] = label
                if row.get("about"):
                    c["text"] = row["about"]
            out.append(c)
        out.append(_add_role_row())
    if r is not None:
        out.append(_driver_row(r, m))
    out.append(_choice("coding", "shared", LABELS["coding"], CODING, cli.coding_mode()))
    out.append(_choice("workflows", "workflows", LABELS["workflows"], WORKFLOWS, cli.workflows_mode()))
    out.append(_choice("deep-side", "workflows", LABELS["deep-side"], DEEP_SIDES, routes.run_side()))
    out.extend(_run_rows())
    # Stats recording, Live view, Live refresh and the electricity price had rows here, as the
    # pane's Settings tab did until #1322 dropped them (recording always on; wb stats set and wb
    # savings --price keep the rest): a `recording` row read a key stats.settings() no longer has
    prog = progress.settings()
    after = progress.user_after(prog) or 0
    out.append(_choice("progress", "shared", LABELS["progress"], _progress_stops(after),
                       "on" if prog.get("on") is True else "off"))

    # gpu
    if r is None:
        out.append(_note("gpu-why", "gpu", why or OLDER_WB))
    elif m is None:
        out.append(_note("gpu-why", "gpu", mwhy or OLDER_WB))
    elif not m["gpu"]:
        out.append({**_note("gpu-none", "gpu", NO_GPU.format(who=who)), "ask": NO_GPU_ASK, "brief": "no model yet"})
    else:
        for name, label, row in roles:
            gpu = row.get("gpu") or routes.AUTO
            # on the card while the role runs on the GPU, as its API model is while on API: under
            # More it read as a choice only Claude or Optimize could make (#1626, the owner, 2026-11-27)
            out.append(_choice(f"gpu-{name}", name, label_of(f"gpu-{name}"), _gpu_stops(label, m["gpu"], gpu), gpu,
                               more=row.get("via") == "api"))
        for g in m["gpu"]:  # every discovered model: the pane's six-row cap went with the pane
            sizes = g.get("sizes") or []
            out.append(_choice(f"size-{g['id']}", "gpu", f"{g.get('label') or g['id']} tasks", SIZES,
                               sizes[0] if len(sizes) == 1 else "all", brief=""))
        out.append(_optimize_row(m))
    out.extend(_media_rows(m))

    # api
    if r is None:
        out.append(_note("api-why", "api", why or OLDER_WB))
    else:
        cap_row = _cap_row(r, quiet)  # beside the providers, where spend is looked for (#1624)
        if not stored and not any_api:
            err = r.get("catalog_error")
            out.append({**_note("api-none", "api", CATALOG_FAILED.format(why=err) if err else NO_PROVIDER),
                        "brief": "catalog not read" if err else "no provider connected"})
            out.append(cap_row)
        else:
            if provs:
                c = {**_info("api-connected", "api", "Connected", _connected_line(r)),
                     # and today's spend: its Spend today row went to the cap row's note (#1339), and
                     # this card's folded line still says it
                     "brief": " · ".join(x for x in (", ".join(str(p.get("name")) for p in provs.values()
                                                               if isinstance(p, dict) and p.get("name")),
                                                     _spend_brief(r)) if x)}
                if any(isinstance(p, dict) and p.get("key") not in ("stored", "ollama", "copilot") and p.get("runs") is not True
                       for p in provs.values()):
                    c["ask"] = True  # a provider with no key it can run on
                out.append(c)
            out.append(cap_row)
            for name, label, row in roles:
                others = [x.get("api") for n, _, x in roles if n != name and x.get("api")]
                api = row.get("api") or ""
                stops = _api_stops(label, m, api, others)
                c = _choice(f"api-{name}", name, label_of(f"api-{name}"), stops, api or None, other=True,
                            more=row.get("via") != "api")  # on the card while the role runs on API (#1626)
                if not api:
                    c["says"] = f"{label} has no API model: it runs on the GPU."
                out.append(c)
            # thinking is an API model's alone: a role with none got a switch that did nothing (the
            # web app's review, 2026-11-26), so its row comes with the model, as review-<role>'s does
            for name, label, row in roles:
                if row.get("api"):
                    out.append(_choice(f"think-{name}", name, label_of(f"think-{name}"),
                                       _think_stops({"api": None, "thinking": "off", **row}), row.get("thinking") or "off",
                                       more=True))
            for name, label, row in roles:
                if row.get("api"):
                    out.append(_choice(f"review-{name}", name, label_of(f"review-{name}"), REVIEW,
                                       row.get("thinking_review") or "off", more=True))
            out += [_remove_row(name, label) for name, label, row in roles if row.get("custom")]

    out += setup_rows()
    out.append(_choice("splash", "terminal", LABELS["splash"], SPLASH, "on" if cli.splash_on() else "off"))
    out.append(_probe_row())
    order = {g: i for i, g in enumerate(GROUPS)}
    # a role of the user's own (#1625): its card after the built-ins', before Agents and roles
    out.sort(key=lambda x: order.get(x["group"], len(routes.ROLES) - 0.5))  # stable: each group's rows keep their order
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
    out.append({**_info("setup-size", "setup", "Size", f"About {said['tokens'] / 1000:.1f}K tokens, with "
                        "OpenCode's own prompt and tools, before a task's project files and spec."),
                "brief": f"about {said['tokens'] / 1000:.1f}K tokens"})
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
    if kind == "form":  # Add a role (#1625): its fields as one JSON string
        try:
            got = json.loads(value) if isinstance(value, str) else None
        except ValueError:
            got = None
        if not isinstance(got, dict) or not all(isinstance(got.get(k), str) and got[k].strip()
                                                for k in ("name", "label", "like")):
            raise ValueError("a role needs a name, a label and what it works as")
        if why := routes.custom_why(got["name"].strip(), got["label"], got["like"], got.get("about") or None):
            raise ValueError(why)
        about = str(got.get("about") or "").strip()
        return ["route", "add", got["name"].strip(), "--label", got["label"].strip(), "--like", got["like"],
                *(["--about", about] if about else [])]
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
    if key == "deep-side":
        return ["workflows", "side", value]
    if key == "probe":
        return ["experiment", "probe", value]
    if key == "optimize":
        return ["optimize", "start"]
    if key == "driver":
        return ["route", "driver", value]
    if head == "run":
        return ["run", "stop" if value == "stop" else "report", rest]
    if head == "remove":
        return ["route", "remove", rest]
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
    """Set ``key`` to ``value`` through its wb command: {"ok", "said", "help"?, "detail"?, "page"?}
    (`page`: a refusal in the page's words, or a note to a change that went through: page_line)
    (`detail`: a run's Summary, its report, cut at RUN_DETAIL_CHARS). KeyError for no
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
    if line := page_line(key, result["said"], ok):
        result["page"] = line
    if ok and argv[:2] == ["run", "report"]:  # a run's Summary: its report, drawn under the row
        text = out.strip()
        result["said"] = text.splitlines()[0] if text else "nothing in yet"
        result["detail"] = text if len(text) <= RUN_DETAIL_CHARS else \
            text[:RUN_DETAIL_CHARS] + f"\n… (cut: wb run report {argv[2]} has the rest)"
    if ok and key in HELP and (note := HELP[key](argv[-1], result["said"])):
        result["help"] = note
    return result


# what wb's line says after a role or a value it names, cut at the first command it suggests: a
# `…` span, or a `wb …` (the page's toast has no command: the driver reads wb's whole line)
_COMMAND = re.compile(r"[\s:;,(]+(?:`|wb [a-z]|run\s+`)")
_ROLE_WORD = re.compile(r"\b(" + "|".join(routes.ROLES) + r")\b")


def page_line(key: str, said: str, ok: bool = False) -> str:
    """wb's line as the web app says it: in the page's words, by the row's label and the roles'
    (routes.LABELS), with no `wb:` and no command; wb's whole line stays `said`, which the driver
    reads (settings-changes.jsonl, Recent changes, the mods' relay). A refusal always has one; a
    change that went through only a note the page adds to its own words ("" for none). A role put
    on API before it had a model was told `wb route trivial <provider/model>`, a gated command
    naming the key the row calls Trivial coding (#1558), and every refusal was wb's stdout, `wb:`
    and its commands too (the web app's review, 2026-11-26)."""
    head, _, rest = key.partition("-")
    if ok:  # a role moved while Agents isn't Both: kept, applied once it is
        return "Set Agents to Both for this to apply." if "once Agents is Both" in said else ""
    if head == "role" and rest in routes.LABELS and "has no API model" in said:
        return f"Pick {routes.LABELS[rest]}'s API model first: tap API, or under More."
    text = said.strip().removeprefix("wb: ").strip()
    text = _COMMAND.split(text, 1)[0].strip(" :;,.")
    text = _ROLE_WORD.sub(lambda r: routes.LABELS[r.group(1)], text)
    return f"{label_of(key)} wasn't changed: {text}." if text else f"{label_of(key)} wasn't changed."


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
    if key.startswith(NOT_RECORDED):  # a run's Stop or Summary: no setting changed (a run's stop is a notice)
        return None
    # an action (Optimize's Measure now) is pressed, never changed from a value: logged with no
    # `from`, drawn "Optimize: started", never "off → start" (the web app's review, 2026-11-26)
    was = None if before.get("kind") == "action" else before.get("value")
    entry = {"ts": store.now(), "key": key, "label": before.get("label") or key, "from": was,
             "to": value, "said": done["said"], **({"help": done["help"]} if done.get("help") else {}), "by": by}
    store.append_jsonl(chat.changes_path(), entry)
    store.write_json(chat.chat_dir() / REV, {k: entry[k] for k in ("ts", "key", "label", "to", "by")})
    return entry


# Every write logged in one place (#1355): cli.main, around the command it runs. A Settings row is
# a wb command line (_argv); key_for reads that line back into the row and its value, so whoever
# types the row's own command (the web app's child, Claude's `wb settings set` child, the OpenCode
# driver's shell, the user at a terminal) logs one row of settings-changes.jsonl, `by` actor():
# before #1355's wave 3 only the web app's POST and `wb settings set` logged, so `wb coding ask`
# typed at a terminal changed the row with no stamp and no open page heard of it. Only the rows'
# own command lines count: a combined one (`wb route significant api --api x`) logs nothing.
ROUTE_FLAGS = {"--gpu": "gpu", "--api": "api", "--thinking": "think", "--thinking-review": "review"}
LOGGED = ("route", "coding", "workflows", "deepmode", "progress", "splash", "experiment", "optimize", "model",
          "image", "audio", "chat")  # the commands key_for reads: cli.main imports this module only for them


def key_for(argv: list[str]) -> tuple[str, str] | None:
    """The row a wb command line writes and the value, _argv's inverse: (key, value), or None for
    a line that is no row's own (a read, a dry run, a flag a row's command doesn't take)."""
    a = [str(x) for x in argv]
    for i, x in enumerate(a):  # `--api=x` as `--api x`
        if x.startswith("--") and "=" in x:
            a[i:i + 1] = x.split("=", 1)
            break
    if a[:2] == ["route", "add"] and len(a) > 2 and "--dry-run" not in a and "--json" not in a:
        return "add-role", a[2]  # Add a role's form (#1625): its flags are its fields
    if not a or any(x.startswith("-") and x not in (*ROUTE_FLAGS, "--default", "--port") for x in a):
        return None  # a dry run, --json, a flag no row's command takes
    cmd, rest = a[0], a[1:]
    if cmd == "route" and len(rest) == 2 and rest[0] == "remove":
        return f"remove-{rest[1]}", "remove"
    if cmd == "route":
        if len(rest) == 2 and not rest[1].startswith("-"):
            if rest[0] in ("all", "busy"):
                return ("agents" if rest[0] == "all" else "busy"), rest[1]
            if rest[0] in ("cap", "driver"):
                return rest[0], rest[1]
            if rest[0] in routes.all_roles() and rest[1] in ("gpu", "api"):
                return f"role-{rest[0]}", rest[1]
        if len(rest) == 3 and rest[0] in routes.all_roles() and rest[1] in ROUTE_FLAGS and not rest[2].startswith("-"):
            return f"{ROUTE_FLAGS[rest[1]]}-{rest[0]}", rest[2]
        return None
    if cmd in ("workflows", "deepmode") and len(rest) == 2 and rest[0] == "side":
        return "deep-side", rest[1]
    if cmd in ("coding", "workflows", "progress", "splash") and len(rest) == 1 and not rest[0].startswith("-"):
        return cmd, rest[0]
    if cmd == "experiment" and len(rest) == 2 and rest[0] == "probe":
        return "probe", rest[1]
    if a == ["optimize", "start"]:
        return "optimize", "start"
    if cmd == "model" and len(rest) >= 2 and rest[0] == "sizes" and not any(x.startswith("-") for x in rest):
        sizes = sorted(set(rest[2:]))
        return f"size-{rest[1]}", sizes[0] if len(sizes) == 1 else "all"
    if cmd in ("image", "audio") and len(rest) == 2 and rest[0] == "--default":
        kind = "image" if cmd == "image" else _audio_kind(rest[1])
        return (f"media-{kind}", rest[1]) if kind else None
    return None


def _audio_kind(engine: str) -> str | None:
    from . import audio
    try:
        e = audio.engines().get(engine)
    except (RuntimeError, OSError):
        return None
    kind = audio.kind_of(e) if e else None
    return kind if any(k == kind for k, _, _ in MEDIA) else None


def current(key: str):
    """Row ``key``'s value now, read in this process from the files the row's command writes (never
    the rows' `wb route --json` and `wb models --json` children: cli.main asks before and after
    each write); None for none, an action, or a file that can't be read."""
    head, _, rest = key.partition("-")
    try:
        if key == "add-role":  # the user's own roles as they stand (#1625): an add moves it
            return ", ".join(r for r in routes.all_roles() if r not in routes.ROLES) or "none"
        if head == "remove":
            return "kept" if rest in routes.all_roles() else "removed"
        if key in ("agents", "busy", "cap", "driver") or (head in ROLE_LABELS and rest in routes.all_roles()):
            doc = routes.read()
            if key == "agents":
                return doc.get("use") or "both"
            if key == "busy":
                return doc.get("busy") or "wait"
            if key == "cap":
                return float(doc["day_usd"]) if doc.get("day_usd") is not None else None
            if key == "driver":
                return doc.get("driver") or routes.DRIVER_SESSION
            row = doc["roles"].get(rest) or {}
            return {"role": row.get("via") or "gpu", "gpu": row.get("gpu") or routes.AUTO, "api": row.get("api") or None,
                    "think": row.get("thinking") or "off", "review": row.get("thinking_review") or "off"}[head]
        if key == "coding":
            return cli.coding_mode()
        if key == "workflows":
            return cli.workflows_mode()
        if key == "deep-side":
            return routes.run_side()
        if key == "progress":
            return "on" if progress.settings().get("on") is True else "off"
        if key == "splash":
            return "on" if cli.splash_on() else "off"
        if key == "probe":
            return context.experiments().get("probe", "off")
        if head == "size":
            from . import models
            sizes = (models._overrides().get(rest) or {}).get("sizes") or []
            return sizes[0] if len(sizes) == 1 else "all"
        if head == "media":
            from . import audio, images
            kinds = {"image": images.choices()} if rest == "image" else audio.choices()
            return (kinds.get(rest) or {}).get("default")
    except (routes.RoutesError, chat.ConfigError, RuntimeError, OSError, ValueError, KeyError, TypeError):
        return None
    return None


def actor() -> str:
    """Who a write is by: the caller (store.caller_kind: `agent`, `claude`, `opencode`), else `web`
    for the web app's child (WB_ACTOR, which child_env sets; Claude's own WB_ACTOR=web stays
    Claude's), else `terminal`."""
    kind = store.caller_kind()
    return "web" if kind == "terminal" and os.environ.get(ACTOR) == "web" else kind


def logged(key: str, value: str, before, said: str, by: str | None = None) -> dict | None:
    """Log a write that went through (cli.main's): record() with the row's value ``before`` and
    after it, wb's last line ``said`` and the note HELP gives for it; None for a no-op."""
    kind = "action" if key == "optimize" else "number" if key == "cap" else "choice"
    after = current(key)
    to = value if kind == "action" or after is None else after
    said = said or "done"
    done = {"said": said, **({"help": h} if key in HELP and (h := HELP[key](value, said)) else {})}
    return record({"key": key, "label": label_of(key), "kind": kind, "value": before}, key, to, done,
                  by or actor())


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
OFFER_MAX_AGE_S = 3600  # the mod's OLDEST_WAKE_MS: a row without help older is no news
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
    than settings-offered.json's mark: a note under OFFER_MAX_AGE_S old, an offer however old
    while it is its setting's newest row (#1557, 2026-11-26: the page promises "your driver will
    offer to set it up", and a change made with no driver up lapsed after the hour; a later change
    of the same setting, anyone's, retires it), one older than the hour only while help_now still
    gives a note (else it is retired too). ``spend`` (--if-due) moves the mark past them under its
    lock, so one host's servers say each once."""
    now = store.now() if now is None else now
    mark_path = store.home() / OFFERED
    with store.locked(store.home() / "locks" / "settings-offered.lock"):
        mark = float((store.read_json(mark_path, {}) or {}).get("ts") or 0)
        got: dict = {"offers": [], "notes": []}
        newest = mark
        rows = [r for r in store.read_jsonl_tail(chat.changes_path(), OFFER_TAIL)
                if isinstance(r, dict) and isinstance(r.get("ts"), (int, float))]
        latest: dict[str, float] = {}  # each setting's newest row, by anyone
        for r in rows:
            k = _one(r.get("key"), 80)
            latest[k] = max(latest.get(k, 0.0), float(r["ts"]))
        for r in rows:
            ts = r["ts"]
            if r.get("by") != "web" or ts <= mark:
                continue
            key, label = _one(r.get("key"), 80), _one(r.get("label"), 80) or _one(r.get("key"), 80)
            help = r.get("help").strip() if isinstance(r.get("help"), str) else ""
            if (ts < latest[key]) if help else (ts < now - OFFER_MAX_AGE_S):
                continue
            newest = max(newest, float(ts))
            if help and ts < now - OFFER_MAX_AGE_S and not help_now(key):
                continue  # what it asked for is done, or moot: retired, said never (#1557's review)
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
