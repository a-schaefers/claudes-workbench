"""Deep workflows (#1278): `wb run`, a flow's items as wb tasks, driven by a detached driver.

Claude's own dynamic workflows fan work out to Claude subagents; a run fans it out to the user's
OpenCode agents on the user's roles (GPU or API), each item one wb task with its lane, keyhole,
meter and day cap. Claude writes or picks the flow, starts it once (`wb run start`) and is woken
only at checkpoints and the end: the driver (`python3 -m workbench.runs drive <run>`, detached
like bench's driver, cli.detach) starts each item with `wb start` as a subprocess, so refusals and
exit statuses are the user's own, takes each item's notice itself (store.Task.notify routes an
open run's items' notices to the run), checks results, filters, refixes and keeps a journal, and
raises one run notice when Claude is needed. Claude has the final say (the owner, 2026-11-24): a
flow ends at its `final` stage, Claude's; what a `keep` filtered stays in the report; code waits
at review for Claude.

A flow is JSON (FLOW_KEYS; agent/flows/*.json, $WB_FLOWS, or --plan FILE): stages, each a role
card's report or code tasks over items (a list, or the outputs of an earlier stage: `from`,
`split`), as a pipeline (an item moves on as soon as its input is ready) or behind a barrier,
batched, attempted N times, filtered (`keep`), refixed (`refix`), checkpointed (`claude`).

Files, in $WB_HOME/runs/<run>/: run.json (who started it, the flow whole, its params and
budgets), status.json (the phase and each stage's counts), journal.jsonl (each item started,
its output, its null, its not-run, each refix and notice: resume replays it by stage and item),
inbox/ (Claude's feeds for a `claude` stage), outputs/ (each item's object), specs/ (each item's
spec), schemas/ (each batch's schema), notify.jsonl (the items' notices, the driver's alone), result.json (the end's report),
acked (the run's notices taken), driver.log.
"""
import hashlib
import json
import math
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import ask_mcp, cards, gitops, meter, models, routes, spend, store

PKG_ROOT = Path(__file__).resolve().parent.parent
SHIPPED = PKG_ROOT / "agent" / "flows"
# <run>-<stage>-<n> fits a task name (store.NAME_RE, 48) and the run a group (cli.GROUP_RE, 32)
RUN_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,23}\Z")
STAGE_RE = re.compile(r"^[a-z][a-z0-9]{0,7}\Z")
FLOW_KEYS = ("v", "name", "description", "params", "budget_usd", "budget_min", "context", "stages",
             "command", "from")
STAGE_KEYS = ("id", "card", "kind", "role", "items", "from", "split", "keep", "flow", "batch",
              "attempts", "schema", "tools", "skills", "ask", "setup", "test", "refix", "on_failure",
              "max_items", "claude", "final", "prompt", "compare", "escalate", "unless_exists")
PARAM_TYPES = ("string", "int", "list", "json", "file", "files", "runs")
# a param is its type alone ("list"), or an object of these (#1295, Goose's recipe parameters):
# `choices` for a string or int, `default` used when --param leaves it out, `optional` absent then
PARAM_KEYS = ("type", "description", "default", "choices", "optional")
FILE_KEYS = ("file", "name", "stem", "dir")  # a `files` param's item, {item.<key>}
FILES_MAX = 1000       # a `files` param's matches, at most: past it, narrow the pathspec
DRY_SPEC_CUT = 1500    # characters of a stage's first spec `wb run start --dry-run` shows
# a flow's `command` (#1295): one lowercase word; never wb's own nor either host's built-ins
COMMAND_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}\Z")
RESERVED_COMMANDS = frozenset((
    "deepmode",
    # Claude Code's
    "add-dir", "agents", "bashes", "bug", "clear", "compact", "config", "context", "cost", "doctor",
    "exit", "export", "help", "hooks", "ide", "init", "install-github-app", "login", "logout", "mcp",
    "memory", "model", "output-style", "permissions", "plugin", "pr-comments", "privacy-settings",
    "quit", "release-notes", "reload-plugins", "resume", "review", "rewind", "security-review",
    "status", "statusline", "terminal-setup", "todos", "upgrade", "usage", "vim", "feedback", "keybindings",
    "rename", "copy", "skills", "theme", "files", "plan", "loop", "schedule", "fork", "add",
    # OpenCode's TUI
    "connect", "details", "editor", "models", "new", "redo", "sessions", "share", "themes", "thinking",
    "undo", "unshare"))
RUN_REFUSED = ("No one answers in a run (wb run): Claude reads your result at its end, and nothing outside "
               "your task's rules can be granted. Do the task without it, and say so in your result.")
ESCALATE_MAX = 3       # a code stage's `escalate` roles, at most
MAX_ITEMS = 200        # a stage's items, at most (`max_items`): the rest not_run, named (no silent caps)
ATTEMPTS_MAX = 5
REFIX_MAX = 2
IN_FLIGHT = 6          # a run's items started and not yet done: each is a worktree, and its setup
TEMPLATE = re.compile(r"\{\{|\}\}|\{([a-z]+)(?:\.([A-Za-z0-9_-]+))?\}")
PLACES = ("item", "params", "context", "diff", "report", "diffs", "run", "stage", "base")
CODE_PLACES = ("diff", "report")  # over a code stage only: its change, its coder's round report
# a compare stage's (`compare`): every attempt's change at an item, labelled a1..aN, the whole cut at
# CUTS["diff"] shared among them
CUTS = {"item": 16_000, "context": 4_000, "diff": 40_000, "report": 6_000}  # characters a spec carries
DEFAULT_PROMPT = "{item}"
WAITING = object()     # a refix's next review, held until the coder's next round is in
OPEN = ("running", "checkpoint", "paused")  # the phases a driver runs in: its items' notices are its
PHASES = (*OPEN, "stopped", "review", "closed", "failed")
TICK_S = 30.0          # the driver's longest sleep: woken by its notices and the tasks' files first
KINDS = ("run_checkpoint", "run_done", "run_paused", "run_stopped", "run_failed")
HEADLINE = {"run_checkpoint": "needs Claude", "run_done": "is done", "run_paused": "is paused",
            "run_stopped": "stopped", "run_failed": "failed"}
# The phase each notice is raised in, on the row as `phase`: the hosts deliver one only while the
# run is still in it (a pause since resumed, a checkpoint since fed, is acknowledged unsaid), as a
# task's notice is judged by the kernel's noticePlan, which the hosts call with it (#1278's S9)
NOTICE_PHASE = {"run_checkpoint": "checkpoint", "run_done": "review", "run_paused": "paused",
                "run_stopped": "stopped", "run_failed": "failed"}
WEB_STOP = "stopped from the web app"  # wb run stop from the web app's Settings (settings.ACTOR web)
ARCHIVE = ".archive"   # runs/.archive/<run>: closed runs wb gc moved there (names stay taken)


# --- the keywords (#1278): Workbench's own words, never Claude's `ultracode` --------------------
# The hosts detect them in what the user wrote, both through one module, mod/hooks/keywords.ts (the
# Claude Code mod's, which the OpenCode plugin imports): this is its reference, and
# tests/fixtures/keywords.json holds both to it. Reading a prompt is parsing text, which stays out
# of the kernel (kernel/BOUNDARY.md, #1325).
FENCE = re.compile(r"```.*?(?:```|\Z)|`[^`\n]*`", re.S)
DEEPCODE = re.compile(r"(?<![\w-])deepcode(?![\w-])", re.I)
DEEPMODE_LINE = re.compile(r"^\s*deepmode\s+(on|off)\s*[.!]?\s*$", re.I | re.M)
DEEPCODE_RULE = ("The user wrote `deepcode`, Workbench's own word (not Claude's `ultracode`): this "
                 "request is a deep workflow on their agents. Follow the /workbench skill's *Deep "
                 "workflows*: pick a flow, say it in at most three lines, run `wb run start` once, "
                 "and wait for its notices. Each item runs on its role's model; the GPU lease, the "
                 "provider slots and the day cap decide what runs.")


def keywords(text: str) -> dict:
    """{"deepcode": the word as a whole word, "deepmode": True/False for a line of its own
    `deepmode on|off`, else None}, outside backticks and fences."""
    bare = FENCE.sub(" ", text or "")
    toggle = DEEPMODE_LINE.findall(bare)
    return {"deepcode": bool(DEEPCODE.search(bare)), "deepmode": (toggle[-1].lower() == "on") if toggle else None}


# --- flows -------------------------------------------------------------------------------------

def flows_dir() -> Path:
    v = os.environ.get("WB_FLOWS")
    return Path(v).expanduser() if v else Path.home() / ".config" / "workbench" / "flows"


def flow_places(repo: Path | None = None) -> list[tuple[str, Path]]:
    """(where, directory) a flow is looked for: the user's, then the shipped ones; a repo's own
    `.workbench/flows/` last, which `wb run flows` shows as "read it first" and --flow refuses: a
    repo's file is the repo's text, and Claude reads it before it passes it as --plan."""
    out = [("yours", flows_dir()), ("shipped", SHIPPED)]
    if repo is not None:
        out.append(("repo", Path(repo) / ".workbench" / "flows"))
    return out


def flow_names(repo: Path | None = None) -> list[tuple[str, str, str]]:
    """(name, where, description) of every flow, the user's shadowing the shipped."""
    seen, out = set(), []
    for where, d in flow_places(repo):
        for p in sorted(d.glob("*.json")) if d.is_dir() else ():
            if p.stem in seen:
                continue
            seen.add(p.stem)
            try:
                desc = str(json.loads(p.read_text()).get("description") or "")
            except (OSError, ValueError, AttributeError):
                desc = "(unreadable)"
            out.append((p.stem, where, desc))
    return out


def flow_path(name: str) -> Path | None:
    """Flow ``name``'s file, the user's before the shipped one (never a repo's)."""
    if not RUN_RE.match(name):
        return None
    return next((d / f"{name}.json" for _, d in flow_places() if (d / f"{name}.json").is_file()), None)


def load_flow(name: str | None = None, plan: str | None = None) -> dict:
    """The flow --flow names or --plan's file holds, checked (validate); RuntimeError with why."""
    if plan:
        path = Path(plan).expanduser()
    else:
        path = flow_path(name or "")
        if path is None:
            raise RuntimeError(f"no flow {name!r} (wb run flows lists them; a repo's .workbench/flows/ goes "
                               "in with --plan once read)")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise RuntimeError(f"{path}: {e.strerror}") from None
    except (ValueError, UnicodeDecodeError) as e:
        raise RuntimeError(f"{path}: not JSON ({e})") from None
    if errs := validate(doc):
        raise RuntimeError(f"{path}: " + "; ".join(errs[:6]))
    return doc


def flow_sha(flow: dict) -> str:
    return hashlib.sha256(json.dumps(flow, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


def stages(flow: dict) -> list[dict]:
    return [s for s in flow.get("stages") or () if isinstance(s, dict)]


def stage_by(flow: dict, sid: str) -> dict | None:
    return next((s for s in stages(flow) if s.get("id") == sid), None)


def is_code(stage: dict) -> bool:
    return stage.get("kind") == "code" or (bool(stage.get("card")) and _card_tools(stage) == "code")


def settled(flow: dict) -> dict:
    """``flow`` with each code card's stage marked `kind: code`, as run.json keeps it: is_code read
    the card on every pass, so a card copied and edited past loading (a typo'd key, a body over
    cards.BODY_MAX) mid-run made a code item at review read as a report, and the driver's _take
    or wb run close discarded it, branch and all (#1470, 2026-11-26)."""
    flow = json.loads(json.dumps(flow))
    for s in stages(flow):
        if s.get("card") and s.get("kind") is None and is_code(s):
            s["kind"] = "code"
    return flow


def _card_tools(stage: dict) -> str | None:
    try:
        return cards.load(stage["card"]).tools
    except (RuntimeError, KeyError, TypeError):
        return None


def placeholders(text: str) -> list[tuple[str, str | None]]:
    return [(m.group(1), m.group(2)) for m in TEMPLATE.finditer(text or "") if m.group(1)]


def validate(flow) -> list[str]:
    """What makes ``flow`` unusable, [] when it can run. Refused: an unknown key or placeholder, a
    last stage that isn't `final`, a stage after a code stage but a report over its {diff}."""
    if not isinstance(flow, dict):
        return ["a flow is a JSON object"]
    errs = [f"unknown key {k!r} (a flow takes {', '.join(FLOW_KEYS)})" for k in flow if k not in FLOW_KEYS]
    if flow.get("v") != 1:
        errs.append('"v": 1 (this flow format\'s version) is missing')
    if not (isinstance(flow.get("name"), str) and RUN_RE.match(flow["name"])):
        errs.append("name: lowercase letters, digits and hyphens, at most 24")
    params = flow.get("params") or {}
    if not isinstance(params, dict):
        errs.append(f"params: an object, each param a type of {', '.join(PARAM_TYPES)} or {{type, …}}")
        params = {}
    for k, v in params.items():
        errs += [f"param {k}: {e}" for e in param_errors(v)]
    if "command" in flow:
        errs += command_errors(flow)
    if "from" in flow and not (isinstance(flow["from"], str) and re.fullmatch(r"[0-9a-f]{40}", flow["from"])):
        errs.append("from: the shipped flow's blob sha a copy came from (wb flow copy writes it)")
    for k in ("budget_usd", "budget_min"):
        if k in flow and flow[k] is not None and not (isinstance(flow[k], (int, float)) and flow[k] >= 0):
            errs.append(f"{k}: a number")
    if "context" in flow and not isinstance(flow["context"], str):
        errs.append("context: text")
    st = stages(flow)
    if not st or len(st) != len(flow.get("stages") or ()):
        return errs + ["stages: a list of stage objects"]
    ids = [s.get("id") for s in st]
    for i, s in enumerate(st):
        sid = s.get("id")
        where = f"stage {sid or i + 1}"
        errs += [f"{where}: unknown key {k!r}" for k in s if k not in STAGE_KEYS]
        if not (isinstance(sid, str) and STAGE_RE.match(sid)):
            errs.append(f"{where}: id is a lowercase letter then letters or digits, at most 8")
        if ids.count(sid) > 1:
            errs.append(f"{where}: its id is used twice")
        before = ids[:i]
        if s.get("final"):
            if i != len(st) - 1:
                errs.append(f"{where}: final is the last stage")
            if any(k in s for k in ("card", "kind", "items", "split", "keep", "claude")):
                errs.append(f"{where}: the final stage is Claude's: no card, items or keep")
            if "from" in s and s["from"] not in before:
                errs.append(f"{where}: from names an earlier stage")
            continue
        if s.get("claude"):
            if "from" not in s or s["from"] not in before:
                errs.append(f"{where}: a claude checkpoint takes the outputs of an earlier stage (from)")
            if any(k in s for k in ("card", "kind", "items")):
                errs.append(f"{where}: a claude checkpoint has no card or items: Claude feeds it (wb run feed)")
            continue
        if ("items" in s) == ("from" in s):
            errs.append(f"{where}: items or from, one of them")
        if "from" in s and s["from"] not in before:
            errs.append(f"{where}: from names an earlier stage")
        if "split" in s and ("from" not in s or not isinstance(s["split"], str)):
            errs.append(f"{where}: split (a key of the earlier output) goes with from")
        if not s.get("card") and s.get("kind") != "code":
            errs.append(f"{where}: a card, or kind code")
        if s.get("card"):
            try:
                c = cards.load(s["card"])
            except RuntimeError as e:
                errs.append(f"{where}: {e}")
                c = None
        else:
            c = None
        if s.get("kind") not in (None, "code"):
            errs.append(f"{where}: kind is code or absent")
        if c is not None and s.get("kind") == "code" and c.report:
            errs.append(f"{where}: kind code with a report card ({c.name})")
        code = is_code(s)
        if s.get("role") is not None and s["role"] not in routes.ROLES:
            errs.append(f"{where}: role is one of {', '.join(routes.ROLES)}")
        if s.get("flow", "pipeline") not in ("pipeline", "barrier"):
            errs.append(f"{where}: flow is pipeline or barrier")
        for k, top in (("batch", 50), ("attempts", ATTEMPTS_MAX), ("max_items", MAX_ITEMS)):
            v = s.get(k)
            if v is not None and not (isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= top):
                errs.append(f"{where}: {k} is a whole number from 1 to {top}")
        if (s.get("batch") or 1) > 1 and s.get("flow") != "barrier":
            errs.append(f"{where}: a batch waits for all its inputs: flow barrier")
        if (s.get("batch") or 1) > 1 and code:
            errs.append(f"{where}: a code stage isn't batched")
        keep = s.get("keep")
        if keep is not None and not (isinstance(keep, dict) and isinstance(keep.get("path"), str)
                                     and isinstance(keep.get("in"), list) and set(keep) <= {"path", "in"}):
            errs.append(f"{where}: keep is {{path, in: [values]}}")
        if code and (keep or s.get("schema") or s.get("tools")):
            errs.append(f"{where}: a code stage has no schema, tools or keep")
        if not code and (s.get("test") or s.get("setup")):
            errs.append(f"{where}: a report stage has no test or setup")
        if not code:
            name = s.get("schema") or (c.schema if c else None)
            if not name:
                errs.append(f"{where}: a report stage needs a schema (its card's, or schema)")
            else:
                try:
                    cards.load_schema(name)
                except RuntimeError as e:
                    errs.append(f"{where}: {e}")
        if s.get("tools") not in (None, "read", "none"):
            errs.append(f"{where}: tools is read or none")
        if s.get("on_failure") not in (None, "checkpoint"):
            errs.append(f"{where}: on_failure is checkpoint or absent (null)")
        src = stage_by(flow, s["from"]) if isinstance(s.get("from"), str) else None
        over_code = src is not None and is_code(src) and not src.get("claude")
        compare = bool(s.get("compare"))
        if "compare" in s and s["compare"] is not True:
            errs.append(f"{where}: compare is true or absent")
        if compare and (code or not over_code or int(src.get("attempts") or 1) < 2):
            errs.append(f"{where}: compare is a report stage over a code stage with attempts of 2 or more")
        if compare and any(k in s for k in ("split", "batch", "attempts", "refix")):
            errs.append(f"{where}: a compare stage takes each item's attempts whole: no split, batch, attempts or refix")
        esc = s.get("escalate")
        if esc is not None:
            if not (isinstance(esc, list) and 1 <= len(esc) <= ESCALATE_MAX and all(r in routes.ROLES for r in esc)
                    and len(set(esc)) == len(esc)):
                errs.append(f"{where}: escalate is a list of 1 to {ESCALATE_MAX} distinct roles ({', '.join(routes.ROLES)})")
            elif not code or s.get("claude"):
                errs.append(f"{where}: escalate goes on a code stage")
        refix = s.get("refix")
        if refix is not None:
            if not (isinstance(refix, dict) and isinstance(refix.get("path"), str) and set(refix) <= {"path", "max"}
                    and isinstance(refix.get("max", 1), int) and 1 <= refix.get("max", 1) <= REFIX_MAX):
                errs.append(f"{where}: refix is {{path, max: 1 to {REFIX_MAX}}}")
            elif not over_code or code:
                errs.append(f"{where}: refix goes on a report stage over a code stage's diff")
        prompt = s.get("prompt", DEFAULT_PROMPT)
        if not isinstance(prompt, str):
            errs.append(f"{where}: prompt is text")
            prompt = ""
        for place, sub in placeholders(prompt):
            if place not in PLACES:
                errs.append(f"{where}: unknown placeholder {{{place}}} (known: {', '.join(PLACES)})")
            elif place == "params" and (not sub or sub not in params):
                errs.append(f"{where}: {{params.{sub}}} isn't a declared param")
            elif place in CODE_PLACES and not over_code:
                errs.append(f"{where}: {{{place}}} needs from a code stage")
            elif place == "diffs" and not compare:
                errs.append(f"{where}: {{diffs}} is a compare stage's (compare: true)")
            elif place in CODE_PLACES and compare:
                errs.append(f"{where}: a compare stage reads {{diffs}}, every attempt's, not {{{place}}}")
            elif place != "item" and place != "params" and sub:
                errs.append(f"{where}: {{{place}.{sub}}}: only item and params take a key")
        items = s.get("items")
        if "unless_exists" in s:
            errs += [f"{where}: {e}" for e in unless_errors(s, params)]
        if isinstance(items, str):
            ph = placeholders(items)
            if len(ph) != 1 or ph[0][0] != "params" or ph[0][1] not in params or items.strip() != f"{{params.{ph[0][1]}}}":
                errs.append(f"{where}: items is a list, or \"{{params.<a list param>}}\"")
        elif items is not None and not isinstance(items, list):
            errs.append(f"{where}: items is a list, or \"{{params.<a list param>}}\"")
        # after a code stage, only a report over a code stage's {diff}, or the final: code waits
        # at review for Claude, and nothing may build on code Claude hasn't accepted
        if any(is_code(t) and not t.get("claude") for t in st[:i]) and (code or not over_code
                                                                      or ("{diffs}" if compare else "{diff}") not in prompt):
            errs.append(f"{where}: after a code stage comes only a report over its {{diff}} (a compare "
                        "stage's {{diffs}}), or final")
    if st and not st[-1].get("final"):
        errs.append("the last stage is final: Claude's, who has the final say")
    return errs


def param_decl(v) -> dict:
    """A param's declaration as an object: a bare type ("list") is {"type": "list"}."""
    return {"type": v} if isinstance(v, str) else dict(v) if isinstance(v, dict) else {}


def param_type(v) -> str:
    return str(param_decl(v).get("type") or "")


def param_errors(v) -> list[str]:
    """What is wrong with one param's declaration, [] when nothing."""
    d = param_decl(v)
    t = d.get("type")
    if t not in PARAM_TYPES:
        return [f"a type of {', '.join(PARAM_TYPES)}, alone or as {{type, {', '.join(PARAM_KEYS[1:])}}}"]
    errs = [f"unknown key {k!r} (a param takes {', '.join(PARAM_KEYS)})" for k in d if k not in PARAM_KEYS]
    if "description" in d and not isinstance(d["description"], str):
        errs.append("description is text")
    if "optional" in d and not isinstance(d["optional"], bool):
        errs.append("optional is true or false")
    ch = d.get("choices")
    if ch is not None:
        kind = int if t == "int" else str
        if t not in ("string", "int") or not (isinstance(ch, list) and ch and all(
                isinstance(c, kind) and not isinstance(c, bool) for c in ch)):
            errs.append("choices is a list of strings for a string param, of whole numbers for an int")
            ch = None
    if "default" in d:
        dv = d["default"]
        ok = {"int": isinstance(dv, int) and not isinstance(dv, bool), "list": isinstance(dv, list), "json": True,
              "files": isinstance(dv, str) or isinstance(dv, list) and all(isinstance(x, str) for x in dv)
              }.get(t, isinstance(dv, str))
        if not ok:
            errs.append(f"default is a {t}" + (" (a pathspec, or a list of them)" if t == "files" else ""))
        elif ch and dv not in ch:
            errs.append(f"default {dv!r} isn't one of its choices")
    return errs


def command_errors(flow: dict) -> list[str]:
    """A flow's `command` (#1295): one lowercase word, none of wb's (`workbench*`, `deepmode`) or
    the hosts' own, and no other flow's (the user's or a shipped one of another name)."""
    c = flow.get("command")
    if not (isinstance(c, str) and COMMAND_RE.match(c)):
        return ["command: one lowercase word (letters, digits, hyphens; at most 32), without its /"]
    if c.startswith("workbench") or c in RESERVED_COMMANDS:
        return [f"command /{c} is {'Workbench' if c.startswith('workbench') or c == 'deepmode' else 'a host'}'s own: "
                "name it otherwise"]
    other = next((n for n, cmd in _commands_on_disk() if cmd == c and n != flow.get("name")), None)
    return [f"command /{c} is flow {other}'s already"] if other else []


def _commands_on_disk() -> list[tuple[str, str]]:
    """(flow name, command) of the user's and the shipped flows that set one, the user's shadowing
    the shipped (read, never validated: validate calls this)."""
    out = []
    for name, _, _ in flow_names():
        try:
            c = json.loads(flow_path(name).read_text(encoding="utf-8")).get("command")
        except (OSError, ValueError, AttributeError, UnicodeDecodeError):
            continue
        if isinstance(c, str):
            out.append((name, c))
    return out


def files_param(stage: dict, params: dict) -> str | None:
    """The `files` param a stage's items come from (`items: "{params.<it>}"`), else None."""
    items = stage.get("items")
    if not isinstance(items, str):
        return None
    ph = placeholders(items)
    k = ph[0][1] if len(ph) == 1 and ph[0][0] == "params" else None
    return k if k in params and param_type(params[k]) == "files" else None


def unless_errors(stage: dict, params: dict) -> list[str]:
    """A stage's `unless_exists` (#1295): a path template on a stage fed by a `files` param, its
    places the item's own keys and the flow's params."""
    tpl = stage.get("unless_exists")
    if not (isinstance(tpl, str) and tpl.strip()):
        return ["unless_exists is a path template (`tests/{item.stem}_test.py`)"]
    if files_param(stage, params) is None:
        return ["unless_exists goes on a stage whose items are a files param (\"{params.<files>}\")"]
    if (stage.get("batch") or 1) > 1:
        return ["unless_exists checks one file per item: no batch"]
    errs = []
    for place, sub in placeholders(tpl):
        if place == "item" and sub in FILE_KEYS:
            continue
        if place == "params" and sub in params:
            continue
        errs.append(f"unless_exists: {{{place}{'.' + sub if sub else ''}}} isn't one of "
                    f"{', '.join('{item.' + k + '}' for k in FILE_KEYS)} or a declared {{params.<k>}}")
    return errs


def unless_path(stage: dict, item, params: dict) -> str:
    """The path a stage's `unless_exists` names for ``item``, normalized (`./x` is `x`)."""
    import posixpath
    return posixpath.normpath(render(stage["unless_exists"], {"item": item, "params": params}))


def existing(flow: dict, params: dict, repo: Path, base: str) -> dict:
    """{stage id: [paths]}: for each stage with `unless_exists`, the paths its items name that
    exist at ``base``, found once at the start (run.json `exists`); its items there are
    not_run, `exists at base: <path>` (state)."""
    out = {}
    for s in stages(flow):
        if s.get("unless_exists") and (k := files_param(s, flow.get("params") or {})):
            at = gitops.paths_at(repo, base)
            hits = sorted({p for it in params.get(k) or () if (p := unless_path(s, it, params)) in at})
            if hits:
                out[s["id"]] = hits
    return out


def file_item(path: str) -> dict:
    """A `files` param's item: the file's path from the repo's root, its name, stem and directory
    (`.` at the root)."""
    name = path.rsplit("/", 1)[-1]
    stem = name.rsplit(".", 1)[0] if "." in name.lstrip(".") else name
    return {"file": path, "name": name, "stem": stem, "dir": path.rsplit("/", 1)[0] if "/" in path else "."}


def _one_param(t: str, v, repo: Path | None, base: str | None):
    """One param's value from its text (--param's, or a default's) by its type."""
    if t == "int":
        return v if isinstance(v, int) else int(v)
    if t == "list":
        if isinstance(v, list):
            return v
        out = json.loads(v) if v.strip().startswith("[") else [x.strip() for x in v.split(",") if x.strip()]
        if not isinstance(out, list):
            raise ValueError("not a list")
        return out
    if t == "json":
        return json.loads(v) if isinstance(v, str) else v
    if t == "file":
        return Path(v).expanduser().read_text(encoding="utf-8")[:CUTS["item"]]
    if t == "files":
        specs = v if isinstance(v, list) else (json.loads(v) if v.strip().startswith("[")
                                               else [x.strip() for x in v.split(",") if x.strip()])
        if not specs or not all(isinstance(x, str) and x for x in specs):
            raise ValueError("a pathspec, or several comma separated")
        if repo is None or not base:
            raise ValueError("no repository to match it in")
        try:
            found = gitops.files_at(repo, base, specs)
        except gitops.GitError as e:
            raise ValueError(str(e).splitlines()[-1][:300]) from None
        if not found:
            raise ValueError(f"{', '.join(specs)} matches no file committed at {base[:12]}")
        if len(found) > FILES_MAX:
            raise ValueError(f"{', '.join(specs)} matches {len(found):,} files, over {FILES_MAX:,}: narrow it")
        return [file_item(f) for f in found]
    if t == "runs":
        return [f["result"] for f in report_of(v)["final"]]
    return v if isinstance(v, str) else str(v)


def parse_params(flow: dict, given: list[str], repo: Path | None = None, base: str | None = None) -> dict:
    """--param k=v, each by its declared type; RuntimeError with why. A list is JSON or comma
    separated; a file is read (16 KB); files is a pathspec matched at ``base`` in ``repo``
    (committed files only, each an item {file, name, stem, dir}); runs is an earlier run's final
    results. One left out takes its `default`, or stays absent when `optional`."""
    decl = {k: param_decl(v) for k, v in (flow.get("params") or {}).items()}
    out = {}
    for kv in given or ():
        k, sep, v = kv.partition("=")
        if not sep or k not in decl:
            raise RuntimeError(f"--param {kv!r}: the flow takes {', '.join(decl) or 'no params'} (k=v)")
        out[k] = v
    typed = set()  # a default is its value already (validate checked it), but a path or a run to read
    for k, d in decl.items():
        if k not in out and "default" in d:
            out[k] = d["default"]
            if d["type"] in ("string", "int", "list", "json"):
                typed.add(k)
    for k in list(out):
        t = decl[k]["type"]
        try:
            out[k] = out[k] if k in typed else _one_param(t, out[k], repo, base)
        except (ValueError, TypeError, OSError, RuntimeError, UnicodeDecodeError) as e:
            raise RuntimeError(f"--param {k}: a {t} ({e})") from None
        ch = decl[k].get("choices")
        if ch and out[k] not in ch:
            raise RuntimeError(f"--param {k}: one of {', '.join(map(str, ch))}, not {out[k]!r}")
    missing = [k for k, d in decl.items() if k not in out and not d.get("optional")]
    if missing:
        need = ", ".join(f"{k}=<{decl[k]['type']}>" for k in missing)
        raise RuntimeError(f"the flow needs --param {need}")
    return out


def parse_args(flow: dict, raw: str) -> list[str]:
    """A slash command's arguments (#1295) as --param words, for both hosts (they pass the line
    as typed: `wb run start --args`): a flow with one param takes the whole line as it; otherwise
    `k=v` words, shell quoted (`area="a b"`). RuntimeError with why."""
    import shlex
    decl = list((flow.get("params") or {}).keys())
    raw = (raw or "").strip()
    if not raw:
        return []
    if not decl:
        raise RuntimeError(f"flow {flow.get('name')} takes no arguments")
    if len(decl) == 1:  # the whole line, a leading `<k>=` of its own dropped
        return [f"{decl[0]}={raw.removeprefix(decl[0] + '=')}"]
    try:
        words = shlex.split(raw)
    except ValueError as e:
        raise RuntimeError(f"the arguments: {e}") from None
    bad = [w for w in words if "=" not in w or w.split("=", 1)[0] not in decl]
    if bad:
        raise RuntimeError(f"{bad[0]!r}: the arguments are k=v words, k one of {', '.join(decl)}")
    return words


def _text(v, cut: int) -> str:
    """A value as a spec carries it: text as it is; an object or a list as JSON in a fence (an
    agent's output is data, never instructions); cut at ``cut`` characters, said."""
    s = v if isinstance(v, str) else "```json\n" + json.dumps(v, indent=1, ensure_ascii=False) + "\n```"
    return s if len(s) <= cut else s[:cut] + f"\n[… {len(s) - cut:,} characters cut]"


def render(text: str, ctx: dict) -> str:
    """A stage's prompt with its placeholders filled; `{{` and `}}` are braces."""
    def one(m):
        tok = m.group(0)
        if tok in ("{{", "}}"):
            return tok[0]
        place, sub = m.group(1), m.group(2)
        if place == "item":
            v = ctx.get("item")
            if sub:
                v = v.get(sub, "") if isinstance(v, dict) else ""
            return _text(v if v is not None else "", CUTS["item"])
        if place == "params":
            return _text((ctx.get("params") or {}).get(sub, ""), CUTS["item"])
        if place in CUTS:
            return _text(ctx.get(place) or "", CUTS[place])
        return str(ctx.get(place) or "")  # {diffs} cut each attempt's share already
    return TEMPLATE.sub(one, text)


def get_path(obj, path: str):
    """``obj``'s value at a dotted ``path`` (`verdict`, `review.must_fix`), None when absent."""
    for part in path.split("."):
        obj = obj.get(part) if isinstance(obj, dict) else None
    return obj


def stage_schema(stage: dict) -> dict | None:
    """A report stage's schema, its own or its card's; None when it can't be read now."""
    name = stage.get("schema")
    if not name and stage.get("card"):
        try:
            name = cards.load(stage["card"]).schema
        except RuntimeError:
            return None
    try:
        return cards.load_schema(name) if name else None
    except RuntimeError:
        return None


def answers_key(doc: dict | None) -> str | None:
    """The key of a schema's answer list, one entry per input by `id` (judgments' `judgments`,
    triage's `items`): its first top-level array of objects that require an `id`. None: no list
    answers by id, and a batch's coverage isn't checked."""
    props = (doc or {}).get("properties")
    for k, sub in (props.items() if isinstance(props, dict) else ()):
        items = sub.get("items") if isinstance(sub, dict) and sub.get("type") == "array" else None
        if isinstance(items, dict) and "id" in (items.get("required") or ()):
            return k
    return None


def batch_schema(stage: dict, ids: list[str]) -> dict | None:
    """The stage's schema for one batch: its answer list exactly ``ids`` long, each `id` one of
    them, so an invented, missing or extra id gets the runner's one repair round (SCHEMA_FIX). None
    when the schema answers by no id."""
    doc = stage_schema(stage)
    key = answers_key(doc)
    if key is None:
        return None
    doc = json.loads(json.dumps(doc))
    arr = doc["properties"][key]
    arr["minItems"] = arr["maxItems"] = len(ids)
    arr["items"].setdefault("properties", {})["id"] = {"type": "string", "enum": list(ids)}
    return doc


def compare_schema(stage: dict, labels: list[str]) -> dict | None:
    """A compare item's schema: its `best` one of the attempts' labels, each of `ranking` too, so an
    attempt it was never shown is asked again once. None when the schema names neither."""
    doc = stage_schema(stage)
    props = (doc or {}).get("properties")
    if not isinstance(props, dict) or not any(isinstance(props.get(k), dict) for k in ("best", "ranking")):
        return None
    doc = json.loads(json.dumps(doc))
    if isinstance(doc["properties"].get("best"), dict):
        doc["properties"]["best"]["enum"] = list(labels)
    rank = doc["properties"].get("ranking")
    if isinstance(rank, dict) and isinstance(rank.get("items"), dict):
        rank["items"]["enum"] = list(labels)
        rank["maxItems"] = len(labels)
    return doc


def unanswered(stage: dict, it: "Item") -> list[str]:
    """A batch item's member ids its result answered nothing for (a duplicate took a place, or the
    schema answers by no `id`-enum of the batch's): each goes under not_run as unjudged, never
    dropped (#1278's no silent caps; the lenses and triage flows lost them, #1134's study)."""
    if not it.members or it.state != "done" or not isinstance(it.output, dict):
        return []
    key = answers_key(stage_schema(stage))
    if key is None:
        return []
    got = {a.get("id") for a in it.output.get(key) or () if isinstance(a, dict)}
    return [k for k, _ in it.members if k not in got]


# --- a run -------------------------------------------------------------------------------------

def runs_dir() -> Path:
    return store.home() / "runs"


def run_dir(name: str) -> Path:
    return runs_dir() / name


def taken(name: str) -> str | None:
    """Why ``name`` can't be a run: a run (open, or archived by wb gc) or a task has it. A run and
    a task never share a name, in either direction: a run's items are its tasks' prefix."""
    if (run_dir(name) / "run.json").exists() or (runs_dir() / ARCHIVE / name).exists():
        return f"run {name!r} exists: run names are never reused"
    if store.Task(name).exists or (store.archive_dir() / name).exists():
        return f"a task is named {name!r}: a run takes a name of its own"
    return None


def all_runs() -> list[str]:
    d = runs_dir()
    return sorted(p.name for p in d.iterdir() if (p / "run.json").is_file()) if d.is_dir() else []


@dataclass
class Item:
    stage: str
    key: str
    input: object = None
    n: int | None = None
    task: str | None = None
    state: str = "pending"  # pending, running, done, filtered, superseded, null, not_run
    why: str = ""
    output: object = None
    src: str | None = None  # the key of the item it came from (the trail)
    members: list | None = None  # a batch's [(key, value)], each value carrying its key as `id`
    reviews: dict | None = None  # a compare item's: each attempt's review, by its key (#1504)


def member(key: str, v) -> dict:
    """A batch member as its batch item carries it: its key as `id`, which the judge answers by
    (the finder's card asks for no id, so a judge given none invented them), an object's own keys
    after it, anything else under `item`."""
    if isinstance(v, dict):
        return {"id": key, **{k: x for k, x in v.items() if k != "id"}}
    return {"id": key, "item": v}


class Run:
    def __init__(self, name: str):
        self.name = name
        self.dir = run_dir(name)

    @property
    def exists(self) -> bool:
        return (self.dir / "run.json").is_file()

    @property
    def spec(self) -> dict:
        return store.read_json(self.dir / "run.json", {})

    @property
    def flow(self) -> dict:
        return self.spec.get("flow") or {}

    @property
    def status(self) -> dict:
        return store.read_json(self.dir / "status.json", {})

    def set_status(self, **fields) -> dict:
        with store.locked(store.task_lock(self.name, "runstatus")):
            st = self.status
            st.update(fields, updated_at=store.now())
            store.write_json(self.dir / "status.json", st)
            return st

    def set_status_if(self, check, **fields) -> dict | None:
        with store.locked(store.task_lock(self.name, "runstatus")):
            st = self.status
            if not check(st):
                return None
            st.update(fields, updated_at=store.now())
            store.write_json(self.dir / "status.json", st)
            return st

    def amend(self, **fields) -> dict:
        """run.json's budgets and switches, changed under the run's status lock."""
        with store.locked(store.task_lock(self.name, "runstatus")):
            spec = self.spec
            spec.update(fields)
            store.write_json(self.dir / "run.json", spec)
            return spec

    def journal(self) -> list[dict]:
        return [r for r in store.read_jsonl(self.dir / "journal.jsonl")[0] if isinstance(r, dict)]

    def log(self, **row) -> None:
        store.append_jsonl(self.dir / "journal.jsonl", {"ts": store.now(), **row})

    def task_name(self, stage: str, n: int) -> str:
        return f"{self.name}-{stage}-{n}"

    def output_file(self, stage: str, n: int) -> Path:
        return self.dir / "outputs" / f"{stage}-{n}.json"

    @property
    def acked(self) -> float:
        try:
            v = float((self.dir / "acked").read_text() or 0)
        except (OSError, ValueError):
            return 0.0
        return v if math.isfinite(v) else 0.0

    def claim_ack(self, ts: float) -> bool:
        """The run's notice mark up to ``ts`` unless there already (wb ack --run, wb wait)."""
        with store.locked(store.task_lock(self.name, "runack")):
            if self.acked >= ts:
                return False
            store.replace_bytes(self.dir / "acked", str(ts).encode())
            return True

    def release_ack(self, back: float, mine: float) -> bool:
        """The mark back to ``back`` only while it is still ``mine`` (wb ack --run <run> <back> --if
        <mine>): a host's wake the engine dropped, as cli.release_ack is a task's (#786)."""
        with store.locked(store.task_lock(self.name, "runack")):
            if self.acked != mine:
                return False
            store.replace_bytes(self.dir / "acked", str(back).encode())
            return True

    def notify(self, kind: str, text: str, **extra) -> dict:
        """A run notice in the global notify.jsonl, `{ts, run, kind, phase, text}`: the one wake
        Claude gets for a run's checkpoint, pause, stop, failure or end, its text the wake whole (it
        opens `[workbench] Run `<run>``, spend.RUN_WAKE's), so a host relays it as it is; `phase`
        (NOTICE_PHASE) the run's while it is still true. Journaled too."""
        text = f"{spend.RUN_WAKE_OPENING} `{self.name}` {HEADLINE[kind]}: {text}"
        row = store.append_jsonl(store.home() / "notify.jsonl",
                                 {"ts": store.now(), "run": self.name, "kind": kind, "phase": NOTICE_PHASE[kind],
                                  "text": text, **extra},
                                 after=self.acked)
        self.log(kind="notice", notice=kind, at=row["ts"])
        return row

    def open(self) -> bool:
        return self.status.get("phase") in OPEN


def item_open(spec: dict) -> bool:
    """Whether a task's notices go to its run's driver (store.Task.notify): its run is open and
    its driver alive to take them. A driver killed (a reboot, a kill) left the run open until a
    wb run status reaped it, which no host runs: every item notice went to a file nobody read, a
    code item's stuck or failed too (the review of #1278, 2026-11-25). Claude has them then."""
    run = spec.get("run")
    if not (isinstance(run, str) and RUN_RE.match(run)):
        return False
    r = Run(run)
    return r.open() and driver_alive(r)


# --- the state: every item, from the flow, the journal and the tasks ---------------------------

def _items_of(stage: dict, run: Run, params: dict) -> list:
    items = stage.get("items")
    if isinstance(items, str):
        items = (params or {}).get(placeholders(items)[0][1]) or []
    return list(items or [])


def state(run: Run) -> dict:
    """{stage id: [Item, …]} in the flow's order, each item's state from the journal and its
    task; a barrier stage whose inputs aren't all in yet has None."""
    spec = run.spec
    flow, params = spec.get("flow") or {}, spec.get("params") or {}
    rows = run.journal()
    started = {}   # (stage, key) -> journal row
    final = {}     # (stage, key) -> (state, why)
    refixes: dict = {}  # (stage, src key) -> [refix rows]
    fed = {}
    escalated = set()  # (code stage, key): a review's asks left past refix's max sent it on (_take)
    refused = set()  # tasks whose last resume was the driver's, refused (_held's null)
    for r in rows:
        k = (r.get("stage"), r.get("key"))
        if r.get("kind") == "item":
            started[k] = r
            final.pop(k, None)
        elif r.get("kind") == "retry":
            started.pop(k, None)
            final.pop(k, None)
        elif r.get("kind") in ("output", "null", "not_run"):
            final[k] = (r.get("state") or r["kind"], r.get("why") or "")
        elif r.get("kind") == "refix":
            refixes.setdefault((r.get("stage"), r.get("src")), []).append(r)
        elif r.get("kind") == "fed":
            fed[r.get("stage")] = r
        elif r.get("kind") == "escalate":  # the trigger; "escalated" is the next role's start
            escalated.add((r.get("stage"), r.get("key")))
        elif r.get("kind") == "resumed":  # _live's: the driver's own resume refused, its last
            (refused.add if r.get("by") == "driver" and not r.get("ok") else refused.discard)(r.get("task"))
    skipped = set(spec.get("skip") or ())
    driven = run.status.get("phase") in OPEN  # _live's: what follows may still start
    out: dict = {}
    complete: dict = {}
    for s in stages(flow):
        sid = s["id"]
        if s.get("final"):
            out[sid] = []
            complete[sid] = all(complete.values())
            continue
        if s.get("claude"):
            src = s["from"]
            if not complete.get(src):
                out[sid], complete[sid] = None, False
                continue
            feed = fed.get(sid)
            if feed is None:
                out[sid], complete[sid] = None, False
                continue
            got = store.read_json(run.dir / "inbox" / f"{sid}-feed", [])
            out[sid] = [Item(sid, str(i + 1), v, state="done", output=v) for i, v in enumerate(got)]
            complete[sid] = True
            continue
        inputs: list = []
        groups: dict = {}  # a batch's or a compare item's members, by its key
        reviewed: dict = {}  # a compare item's attempts' reviews, by its key
        if "items" in s:
            inputs = [(str(i + 1), v, None) for i, v in enumerate(_items_of(s, run, params))]
            ready = True
        else:
            src = s["from"]
            src_items = out.get(src)
            barrier = s.get("flow") == "barrier"
            ready = bool(complete.get(src)) or not barrier
            if not ready or src_items is None:
                out[sid], complete[sid] = None, False
                continue
            code_src = is_code(stage_by(flow, src) or {})
            if s.get("compare") and code_src:  # each item's attempts whole, once all of them ended
                # and their reviews (an earlier report stage over the same code stage) too, which
                # its {diffs} carries: best-of paid for a review per attempt that reached neither
                # the chooser nor Claude (#1504, 2026-11-26)
                revs = [out.get(r["id"]) for r in stages(flow) if r["id"] in out and r.get("from") == src
                        and not (is_code(r) or r.get("compare") or r.get("final") or r.get("claude"))]
                of: dict = {}
                for r in revs:
                    for i in r or ():
                        of.setdefault(i.src, []).append(i)
                by_item: dict = {}
                for it in src_items:
                    by_item.setdefault(it.key.rsplit("#a", 1)[0], []).append(it)
                for base, its in by_item.items():
                    if all(i.state not in ("pending", "running", "waiting") for i in its) and None not in revs \
                            and all(x.state not in ("pending", "running", "waiting") for i in its for x in of.get(i.key, ())):
                        groups[base] = [(i.key, i.output) for i in its if i.state == "done"]
                        reviewed[base] = {i.key: x.output for i in its for x in of.get(i.key, ()) if x.state == "done"}
                        inputs.append((base, [v for _, v in groups[base]], None))
                src_items = []
            for it in src_items:
                if s.get("refix") and code_src:
                    fixes = refixes.get((sid, it.key)) or []
                    c = len(fixes)
                    if it.state != "done" and not c:
                        continue
                    for j in range(c):  # the earlier reviews, superseded
                        inputs.append((it.key + (f"~r{j}" if j else ""), None, it.key))
                    if it.state == "null":
                        continue  # the coder ended without a change: nothing more to review
                    # back to the coder: its next review waits for the round after the refix's
                    held = it.state != "done" or (c and (store.Task(it.task).status.get("round") or 0)
                                                  <= fixes[-1].get("round", 0))
                    inputs.append((it.key + (f"~r{c}" if c else ""), WAITING if held else it.output, it.key))
                    continue
                if it.state != "done":
                    continue
                if s.get("split"):
                    parts = get_path(it.output, s["split"])
                    for j, v in enumerate(parts if isinstance(parts, list) else []):
                        inputs.append((f"{it.key}.{j + 1}", v, it.key))
                else:
                    inputs.append((it.key, it.output, it.key))
        attempts = int(s.get("attempts") or 1)
        if attempts > 1:
            inputs = [(f"{k}#a{a}", v, src) for k, v, src in inputs for a in range(1, attempts + 1)]
        batch = int(s.get("batch") or 1)
        if batch > 1:
            for i in range(0, len(inputs), batch):
                groups[f"b{i // batch + 1}"] = [(k, member(k, v)) for k, v, _ in inputs[i:i + batch]]
            inputs = [(key, [v for _, v in ms], None) for key, ms in groups.items()]
        cap = int(s.get("max_items") or MAX_ITEMS)
        esc = (s.get("escalate") or []) if is_code(s) else []
        there = set((spec.get("exists") or {}).get(sid) or ()) if s.get("unless_exists") else set()
        items = []
        # the cap counts distinct items: a refix's reviews of one change (`1`, `1~r1`, …) are one,
        # as an escalation's `#eN` is, and unless_exists's skips are none. Counted by index, a large
        # fix run's refixes pushed a review already running past the cap, its result dropped (#1484,
        # 2026-11-26)
        idx, counted = -1, set()
        while (idx := idx + 1) < len(inputs):
            key, v, src = inputs[idx]
            one = re.sub(r"~r\d+", "", key)
            it = Item(sid, key, v, src=src, members=groups.get(key), reviews=reviewed.get(key))
            row = started.get((sid, key))
            if row:
                it.n, it.task = row.get("n"), row.get("task")
            hit = unless_path(s, v, params) if there and not row else None
            if (sid, key) in final:
                it.state, it.why = final[(sid, key)]
                if it.n is not None and it.state in ("done", "filtered", "superseded"):
                    it.output = (store.read_json(run.output_file(sid, it.n), {}) or {}).get("result")
            elif hit in there:
                it.state, it.why = "not_run", f"exists at base: {hit}"
            elif sid in skipped and not row:
                it.state, it.why = "not_run", "skipped (wb run amend --skip)"
            elif sid in skipped and it.task and _stopped_at_skip(it.task):
                it.state, it.why = "not_run", SKIP_STOPPED
            elif s.get("compare") and len(it.members or ()) < 2:
                it.state, it.why = "not_run", (f"{len(it.members or ())} of its attempts reached review: "
                                               "nothing to compare (wb run report names the rest)")
            elif not row and "#e" not in key and one not in counted and len(counted) >= cap:
                it.state, it.why = "not_run", f"past the stage's max_items ({cap})"  # never one started
            elif v is WAITING:
                it.state, it.input = "waiting", None
            elif it.task:
                it.state, it.why, it.output = _live(s, it, driven, refused=it.task in refused)
            if "#e" not in key and hit not in there:
                counted.add(one)
            items.append(it)
            # stuck, failed, or its review's asks left past refix's max: the next role tries it
            # from the base (#1134's study: escalating attempts across roles)
            if esc and escalation(key) < len(esc) and (it.state == "null" or (sid, key) in escalated):
                inputs.append((escalation_key(key, escalation(key) + 1), v, src))
        out[sid] = items
        complete[sid] = ready and ("items" in s or complete.get(s.get("from"), False)) \
            and all(i.state not in ("pending", "running", "waiting") for i in items)
    out["_complete"] = complete
    return out


TAKEN_OVER = "taken over by Claude (wb takeover): Claude's to finish, not the run's"
SKIP_STOPPED = "stopped mid-run (wb run amend --skip)"


def _stopped_at_skip(name: str) -> bool:
    """Whether a started item of a skipped stage is stopped (wb stop's, or from outside): not_run,
    never _take's null (discarded, an on_failure checkpoint) nor _held's resume. A started item
    read as `skipped` whatever its task did: it ran on, paid, its result dropped, the run done
    under it and its review notice Claude's (#1577, 2026-11-26)."""
    t = store.Task(name)
    st = t.status if t.exists else {}
    return st.get("phase") in ("stuck", "failed") and (st.get("last") == STOPPED_BY_CLAUDE or _outside(st))


def _stop_skipped(run: Run) -> list[str]:
    """Stop the items of the run's skipped stages still at work (wb stop, journalled `stopped`
    with `skip`, as stop's), their names. wb run amend --skip's, and each tick's, for an item a
    pass that read the stages before the amend started anyway (#1577). One at a time (the lock):
    the amend's line counts what it stopped, never the driver's at once."""
    skip = set(run.spec.get("skip") or ())
    if not skip:
        return []
    out = []
    with store.locked(store.task_lock(run.name, "skip")):
        for r in run.journal():
            if r.get("kind") != "item" or r.get("stage") not in skip or not r.get("task"):
                continue
            t = store.Task(r["task"])
            if t.exists and t.status.get("phase") in store.ACTIVE and _wb(["stop", t.name], run).returncode == 0:
                run.log(kind="stopped", task=t.name, skip=r["stage"])
                out.append(t.name)
    return out


def _live(stage: dict, it: Item, driven: bool = False, refused: bool = False) -> tuple[str, str, object]:
    """A started item's state from its task, before the driver has taken its end. ``driven``:
    the run is open, its driver starting what follows: there a code item Claude took over is no
    longer the run's (not_run, never an escalation's null), as its review phase read as done and
    the next stage reviewed, approved or sent a refix to the unfinished diff Claude was editing
    (#1471, the #1373 audit, 2026-11-26), and a code item stopped from outside (_outside) runs on,
    as _held holds it, unless the driver's last resume of it was refused otherwise (``refused``:
    its null, Claude's checkpoint). Read as null, an item the day cap stopped mid-round under
    --overnight ended its stage and the run in the pass that held it for midnight, and in
    fix-escalate started the next role's attempt beside it (#1581, 2026-11-26). Once the run has
    ended (stopped, at review, closed) its report keeps the item as it was."""
    task = store.Task(it.task)
    if not task.exists:
        return "pending", "", None  # its wb start not through yet, or never: the driver decides
    st = task.status
    phase = st.get("phase")
    if is_code(stage) and driven and st.get("taken_over"):
        return "not_run", TAKEN_OVER, None
    if is_code(stage):
        if phase in ("review", "accepted"):
            return "done", "", {"task": it.task, "round": st.get("round"), "item": it.input}
        if phase in ("stuck", "failed") and driven and not refused and _outside(st):
            return "running", "", None
        if phase in ("stuck", "failed", "discarded"):
            return "null", st.get("last") or phase, None
        return "running", "", None
    if phase in store.ACTIVE or phase is None:
        return "running", "", None
    return "running", "", None  # review/failed/stuck: the driver takes it (_take) this tick


# --- the driver ----------------------------------------------------------------------------------

def _wb(argv: list[str], run: Run, timeout: float = 115) -> subprocess.CompletedProcess:
    """`wb ARGV` as the user's own command: a subprocess, from where the run was started (the
    cwd says whose session and repo it is), running this workbench's own package.
    `-P` (3.11's safe_path): with `-m` Python put the cwd ahead of PYTHONPATH, so a repo holding
    a top-level `workbench/` package ran its own `cli.py` for every item's start, accept and
    discard, as the user, outside any lane; a run from a workbench clone on another branch ran
    that branch's (the review of #1278, 2026-11-25). PYTHONPATH is the package root
    (context.detached_env): from `bin/wb`, which only inserts it in sys.path, the child found
    no workbench at all."""
    from . import context
    cwd = run.spec.get("cwd") or run.spec.get("repo")
    env = context.detached_env()
    env.pop("WB_TASK", None)
    try:
        return subprocess.run([sys.executable, "-X", "utf8", "-P", "-m", "workbench.cli", *argv],
                              cwd=cwd if cwd and os.path.isdir(cwd) else str(PKG_ROOT), env=env,
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        return subprocess.CompletedProcess(e.cmd, 124, e.stdout or "", f"wb {argv[0]} timed out")


def _last_line(r: subprocess.CompletedProcess) -> str:
    lines = [ln for ln in (r.stderr or "").splitlines() if ln.strip()] or \
        [ln for ln in (r.stdout or "").splitlines() if ln.strip()]
    return (lines[-1] if lines else f"exit {r.returncode}").removeprefix("wb: ")[:300]


def escalation(key: str) -> int:
    """An item's escalation: `1#e2` is the stage's second escalate role's try, `1` none."""
    m = re.search(r"#e(\d+)\Z", key)
    return int(m.group(1)) if m else 0


def escalation_key(key: str, k: int) -> str:
    return re.sub(r"#e\d+\Z", "", key) + f"#e{k}"


def item_role(stage: dict, it: "Item | None" = None) -> str:
    """The role an item runs on: its stage's, or for an escalation its escalate role."""
    k = escalation(it.key) if it is not None else 0
    esc = stage.get("escalate") or []
    return esc[k - 1] if 0 < k <= len(esc) and esc[k - 1] in routes.ROLES else stage_role(stage)


def stage_model(stage: dict, it: "Item | None" = None):
    """The API model a stage's role (an item's, item_role) runs on now, or None for the GPU (or a
    roles file that can't be read: wb start says why for each item)."""
    role = item_role(stage, it)
    try:
        doc = routes.read()
    except routes.RoutesError:
        return None
    if routes.effective_via(doc, role) != "api" or not doc["roles"][role].get("api"):
        return None
    m = models.lookup(doc["roles"][role]["api"], cached=True)
    return m if m is not None and m.kind == "api" else None


def role_cant_run(role: str) -> str:
    """wb start's own line for a role on API whose model can't be found (routes.resolve's: no
    API model, or one the catalog doesn't list, a missing key named), or "". Read fresh, not
    cached: stage_model's None read as the GPU, so `wb run start` said `significant (GPU) ·
    estimate $0` for a role on a provider with no key, and every item was then refused; with no
    hosted.json yet, a working API role read as the GPU too (#1532, 2026-11-26)."""
    try:
        doc = routes.read()
    except routes.RoutesError:
        return ""  # wb start says why for each item
    if routes.effective_via(doc, role) != "api":
        return ""
    if not (mid := doc["roles"][role].get("api")):
        return routes.no_api_model(role)
    m = models.lookup(mid)
    return "" if m is not None and m.kind == "api" else routes.cant_run(role, mid, models.unlisted_why(mid), "", doc)


def stage_role(stage: dict) -> str:
    role = stage.get("role")
    if not role and stage.get("card"):
        try:
            role = cards.load(stage["card"]).role
        except RuntimeError:
            role = None
    return role if role in routes.ROLES else "significant"


def run_tasks(run: Run) -> list[str]:
    return [r["task"] for r in run.journal() if r.get("kind") == "item" and r.get("task")]


def _attempt_of(task: store.Task):
    """``task``'s place as an attempt of a compare item (key `<item>#a<n>`): (its run, the run's item
    rows, its own row, its key), or None for a task that is no attempt."""
    name = task.spec.get("run")
    if not (isinstance(name, str) and RUN_RE.match(name) and Run(name).exists):
        return None
    run = Run(name)
    rows = [r for r in run.journal() if r.get("kind") == "item" and isinstance(r.get("task"), str)]
    me = next((r for r in rows if r["task"] == task.name), None)
    key = str((me or {}).get("key") or "")
    return (run, rows, me, key) if "#a" in key else None


def _siblings(rows: list, me: dict, key: str) -> list[store.Task]:
    """The other attempts at ``me``'s item in its stage, as tasks that still exist."""
    item = key.rsplit("#a", 1)[0]
    return [t for r in rows if r["task"] != me["task"] and r.get("stage") == me.get("stage")
            and "#a" in (k := str(r.get("key") or "")) and k.rsplit("#a", 1)[0] == item
            and (t := store.Task(r["task"])).exists]


def _chosen(run: Run, rows: list, stage: str, item: str) -> str | None:
    """The label (a1..aN) a compare stage over code stage ``stage`` named best for ``item``: its
    result's `best`, else its `ranking`'s first; None before a chooser answered."""
    for s in stages(run.flow):
        if not (s.get("compare") and s.get("from") == stage):
            continue
        for r in reversed(rows):
            if r.get("stage") == s.get("id") and r.get("key") == item and isinstance(r.get("n"), int):
                res = (store.read_json(run.output_file(s["id"], r["n"]), {}) or {}).get("result")
                res = res if isinstance(res, dict) else {}
                best = res.get("best") or next(iter(res.get("ranking") or ()), None)
                if isinstance(best, str):
                    return best
    return None


def not_chosen(task: store.Task) -> bool:
    """Whether ``task`` is an attempt of a compare item (key `<item>#a<n>`) that lost: another
    attempt at the same item was accepted, or the run's chooser named another one, or (before any
    chooser answered) another one waits at review. best-of's `wb run close` wants every attempt
    accepted or discarded, and each losing one was recorded as the model's rejected work, so every
    run scored the role's model 1 of N and the router steered away from it (#1473, the #1373
    audit, 2026-11-26). Read from the accepted sibling only, a loser discarded before the pick was
    accepted was still scored so (#1568, 2026-11-26): wb accept marks those (discarded_siblings)."""
    if (at := _attempt_of(task)) is None:
        return False
    run, rows, me, key = at
    phases = {t.status.get("phase") for t in _siblings(rows, me, key)}
    if "accepted" in phases:
        return True
    best = _chosen(run, rows, str(me.get("stage")), key.rsplit("#a", 1)[0])
    return best != attempt_label(key) if best else "review" in phases


def discarded_siblings(task: store.Task) -> set[str]:
    """The attempts at ``task``'s compare item already discarded: wb accept of ``task`` marks their
    rows not_chosen, whatever order they were closed in (#1568, 2026-11-26)."""
    if (at := _attempt_of(task)) is None:
        return set()
    _, rows, me, key = at
    return {t.name for t in _siblings(rows, me, key) if t.status.get("phase") == "discarded"}


def tasks_of(name: str) -> set[str]:
    """Run ``name``'s tasks by its journal, open or archived by wb gc (an old transcript's wake
    is charged after gc moved its run): none for a name that is no run."""
    if not RUN_RE.match(name or ""):
        return set()
    d = run_dir(name) if (run_dir(name) / "journal.jsonl").is_file() else runs_dir() / ARCHIVE / name
    return {r["task"] for r in store.read_jsonl(d / "journal.jsonl")[0]
            if isinstance(r, dict) and r.get("kind") == "item" and isinstance(r.get("task"), str)}


def spent(run: Run) -> dict:
    """{usd: the ledger's figure for the run's tasks, gpu_min: their GPU tasks' active minutes}."""
    names = run_tasks(run)
    gpu = sum((store.Task(n).status.get("active_s") or 0) for n in names
              if store.Task(n).exists and store.Task(n).spec.get("via") != "api")
    return {"usd": meter.run_total(names), "gpu_min": round(gpu / 60, 1)}


def _counts(items) -> dict:
    out = {k: 0 for k in ("done", "running", "null", "filtered", "not_run")}
    for it in items or ():
        k = "done" if it.state in ("done", "superseded") else "running" if it.state in ("pending", "running", "waiting") \
            else it.state
        out[k] = out.get(k, 0) + 1
    return out


def attempt_label(key: str) -> str:
    """An attempt's label in a compare stage: `1#a2` is a2."""
    return "a" + key.rsplit("#a", 1)[1] if "#a" in key else key


def _task_diff(t: store.Task) -> str:
    """A code item's change since its base as wb accept --include commits it: the gitignored files
    the coder wrote too (runner.ignored_to_commit), as the review notice and wb diff show them. The
    reviewer's {diff} and the chooser's {diffs} left a lib/util.py out, judged on part of the change
    (#1547, 2026-11-26)."""
    from . import runner
    return gitops.diff(t.worktree, t.spec["base"], force=runner.ignored_to_commit(t))


def _diffs_of(run: Run, stage: dict, it: Item) -> str:
    """{diffs}: each attempt's change at the item, labelled a1..aN with its task and tests, the
    green ones alone when two or more are (said), each cut to its share of CUTS["diff"]. best-of
    ran N builders and a reviewer per diff, and Claude read every review and most diffs: N times
    the handoff (#1134's study, SWE-agent's Chooser)."""
    rows, said = _attempts(it)
    share = CUTS["diff"] // max(1, len(rows))
    reviews = {attempt_label(k): v for k, v in (it.reviews or {}).items()}
    parts = []
    for lab, t, ok in rows:
        try:
            d = _task_diff(t) if t.worktree.is_dir() else ""
        except (gitops.GitError, KeyError) as e:
            d = f"(no diff: {e})"
        d = d if len(d) <= share else d[:share] + f"\n[… {len(d) - share:,} characters cut]"
        state = "tests passed" if ok else "tests failed" if ok is False else "no tests"
        parts.append(f"### {lab} (task {t.name}; {state})\n```diff\n{d or '(no changes)'}\n```"
                     + (f"\nIts review: {_review_of(reviews[lab])}" if lab in reviews else ""))
    return said + "\n\n".join(parts)


REVIEW_CUT = 2_000  # characters of an attempt's review a chooser's {diffs} carries


def _review_of(v) -> str:
    """An attempt's review as {diffs} carries it: its verdict, must_fix and summary (the review
    schema's), else the whole result, as JSON (data), cut at REVIEW_CUT."""
    shown = {k: v[k] for k in ("verdict", "must_fix", "summary") if k in v} if isinstance(v, dict) else {}
    out = json.dumps(shown or v, ensure_ascii=False)
    return out if len(out) <= REVIEW_CUT else out[:REVIEW_CUT] + f" [… {len(out) - REVIEW_CUT:,} characters cut]"


def _attempts(it: Item) -> tuple[list, str]:
    """A compare item's attempts shown, [(label, task, tests ok | None)], and what is said of the
    rest: the green ones alone when two or more are."""
    rows = []
    for key, out in it.members or ():
        t = store.Task(out.get("task", "")) if isinstance(out, dict) else None
        if t is None or not t.exists:
            continue
        tests = t.status.get("tests")
        rows.append((attempt_label(key), t, None if not isinstance(tests, dict) else bool(tests.get("ok"))))
    green = [r for r in rows if r[2]]
    said = ""
    if len(green) >= 2 and len(green) < len(rows):
        said = ("Only the attempts whose tests pass are shown; left out: "
                + ", ".join(f"{lab} (tests {'failed' if ok is False else 'not run'})" for lab, _, ok in rows if not ok) + ".\n\n")
        rows = green
    elif rows and len(green) < 2 and any(ok is not None for _, _, ok in rows):
        said = "Fewer than two attempts pass their tests: all are shown.\n\n"
    return rows, said


def _spec_of(run: Run, stage: dict, it: Item) -> str:
    spec = run.spec
    prompt = stage.get("prompt") or DEFAULT_PROMPT
    if stage.get("compare"):
        first = (it.members or [(None, {})])[0][1]
        return render(prompt, {
            "item": first.get("item") if isinstance(first, dict) else None, "params": spec.get("params") or {},
            "context": spec.get("context") or "", "diffs": _diffs_of(run, stage, it), "run": run.name,
            "stage": stage["id"], "base": spec.get("base", "")})
    diff = report = ""
    over_code = isinstance(it.input, dict) and bool(it.input.get("task")) and any(
        f"{{{p}}}" in prompt for p in CODE_PLACES)
    if over_code:
        t = store.Task(it.input["task"])
        if "{diff}" in prompt:
            try:
                diff = _task_diff(t) if t.exists and t.worktree.is_dir() else ""
            except (gitops.GitError, KeyError) as e:
                diff = f"(no diff: {e})"
            diff = f"```diff\n{diff}\n```" if diff else "(no changes)"
        if "{report}" in prompt:
            report = _code_report(t, it.input.get("round"))
    item = it.input.get("item") if over_code else it.input
    return render(prompt, {
        "item": item, "params": spec.get("params") or {}, "context": spec.get("context") or "",
        "diff": diff, "report": report, "run": run.name, "stage": stage["id"], "base": spec.get("base", "")})


def _code_report(t: store.Task, n) -> str:
    """{report}: the reviewed round's report (summary.md: the coder's summary, its test line), so
    a review after a refix reads the coder's answer to each ask. The refix asked "done, or declined
    with why" while the next review saw only the item and the diff: a reasoned decline was asked
    again until `max` ran out, paid rounds burned (#1134's study, on #1278, 2026-11-25)."""
    if not t.exists:
        return "(no report: the task is gone)"
    n = n if isinstance(n, int) and n > 0 else t.status.get("round") or 1
    return t.report(n).strip() or f"(round {n}'s report is empty)"


def _base_of(run: Run, stage: dict, it: Item) -> str:
    """An item's base: the run's, but for a report over one code item the reviewed round's tree
    committed on it (gitops.review_commit), so its worktree holds the change it reads. At the
    run's base, every review searched for the change and retyped it from the diff to run a test,
    on the base (#1503, 2026-11-26). A compare's attempts share no tree: it stays at the base."""
    base = run.spec["base"]
    src = stage_by(run.flow, stage.get("from") or "") or {}
    if is_code(stage) or stage.get("compare") or not is_code(src) or not isinstance(it.input, dict):
        return base
    t = store.Task(str(it.input.get("task") or ""))
    if not it.input.get("task") or not t.exists:
        return base
    st = t.status
    rnd = it.input.get("round") or st.get("round") or 1
    tree = ((st.get("review_trees") or {}).get(str(rnd)) or {}).get("tree")
    if not tree:
        return base
    try:
        return gitops.review_commit(Path(run.spec["repo"]), t.name, rnd, tree, t.spec.get("base") or base) or base
    except gitops.GitError:
        return base


def _start(run: Run, stage: dict, it: Item, n: int, m=None) -> tuple[str, str]:
    """Start item ``it`` as task <run>-<stage>-<n>: ("running"|"not_run"|"null"|"paused", why).
    ``m``: the item's model now (stage_model), named in a day cap's retry row for _cap_held."""
    spec = run.spec
    name = run.task_name(stage["id"], n)
    specs = run.dir / "specs"
    specs.mkdir(parents=True, exist_ok=True)
    path = specs / f"{stage['id']}-{n}.md"
    path.write_text(_spec_of(run, stage, it), encoding="utf-8")
    # the directory the run was started from (#1494): wb start's package walk goes up from it
    argv = ["start", name, "--spec", str(path), "--repo", spec.get("package_dir") or spec["repo"],
            "--base", _base_of(run, stage, it),
            "--role", item_role(stage, it), "--queue", "--group", run.name, "--run", run.name,
            "--stage", stage["id"], "--item", it.key, "--checkin", "0"]
    if stage.get("card"):
        argv += ["--card", stage["card"]]
    for sk in stage.get("skills") or ():
        argv += ["--skill", sk]
    if spec.get("trial"):
        argv.append("--trial")
    if spec.get("no_claude_md"):
        argv.append("--no-claude-md")
    if is_code(stage):
        if stage.get("test"):
            argv += ["--test", stage["test"]]
        if stage.get("setup") is not None:
            argv += ["--setup", stage["setup"]]
        if stage.get("ask") is not None:
            argv += ["--ask-budget", str(stage["ask"])]
    else:
        argv += ["--report", "--ask-budget", str(stage.get("ask") or 0), "--no-self-review",
                 "--no-base-check", "--setup", ""]
        doc = compare_schema(stage, [lab for lab, _, _ in _attempts(it)[0]]) if stage.get("compare") \
            else batch_schema(stage, [k for k, _ in it.members]) if it.members else None
        if doc is not None:
            path = run.dir / "schemas" / f"{stage['id']}-{n}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            store.write_json(path, doc)
            argv += ["--schema", str(path)]
        elif stage.get("schema"):
            argv += ["--schema", stage["schema"]]
        if stage.get("tools"):
            argv += ["--tools", stage["tools"]]
    run.log(kind="item", stage=stage["id"], key=it.key, n=n, task=name)  # before: a crash adopts it
    r = _wb(argv, run)
    if r.returncode == 0:
        return "running", ""
    why = _last_line(r)
    if r.returncode == 124 and store.Task(name).exists:
        # timed out after writing the task: it may run, its notices this driver's, so it is the
        # item's (cli.reap fails one whose runner never came, a stop from outside: _held); a
        # null here left a live task nobody took (the review of #1278, 2026-11-25)
        return "running", ""
    # the day cap's own refusal, the one that clears at midnight (meter.RESETS, anywhere in what
    # wb start said): no cap, no price or an unreadable routes.json say "day cap" too, and never clear
    if r.returncode == 3 and meter.RESETS in (r.stderr or "") + (r.stdout or ""):
        # its day, cap and model, so the next pass doesn't run wb start again while they hold (_cap_held)
        run.log(kind="retry", stage=stage["id"], key=it.key, day=meter.day(), cap=meter.cap(),
                model=getattr(m, "id", None), why=why)
        return "paused", why
    if r.returncode == 3:
        return "not_run", why
    return "null", why


def _take(run: Run, stage: dict, it: Item) -> None:
    """A report item's end, once its task waits on Claude: its object kept (filtered by `keep`,
    or a refix sent), the task accepted; a failed one discarded, its result null."""
    task = store.Task(it.task)
    if not task.spec.get("report"):  # a code task is Claude's, never discarded as a report (#1470)
        return
    st = task.status
    phase = st.get("phase")
    from . import runner
    if phase in ("review", "accepted") and (task.dir / runner.RESULT).is_file():
        obj = store.read_json(task.dir / runner.RESULT, None)
        store.write_json(run.output_file(stage["id"], it.n), {"key": it.key, "input": it.input, "result": obj,
                                                              "task": it.task})
        keep = stage.get("keep")
        state, why = "done", ""
        if keep and get_path(obj, keep["path"]) not in keep["in"]:
            state, why = "filtered", f"{keep['path']} is {json.dumps(get_path(obj, keep['path']))}"
        refix = stage.get("refix")
        if state == "done" and refix and it.src is not None:
            asks = get_path(obj, refix["path"])
            src_item = next((i for i in _src_items(run, stage) if i.key == it.src), None)
            fixes = [r for r in run.journal() if r.get("kind") == "refix" and r.get("stage") == stage["id"]
                     and r.get("src") == it.src]
            src_stage = stage_by(run.flow, stage.get("from")) or {}
            if isinstance(asks, list) and asks and src_item and len(fixes) >= int(refix.get("max", 1)) \
                    and escalation(it.src) < len(src_stage.get("escalate") or []):
                # past refix's max with asks left: the next role tries it from the base, this
                # attempt kept for Claude (its task stays in review)
                run.log(kind="escalate", stage=src_stage["id"], key=it.src, task=src_item.task,
                        why=f"{len(asks)} ask(s) left after {len(fixes)} refix(es)")
                state, why = "superseded", (f"escalated: {len(asks)} ask(s) of {src_item.task} left after "
                                            f"{len(fixes)} refix(es)")
            elif isinstance(asks, list) and asks and src_item and len(fixes) < int(refix.get("max", 1)):
                code = store.Task(src_item.task)
                text = ("A reviewer read your change and asks for these before Claude reviews it:\n"
                        + "\n".join(f"{i + 1}. {a}" for i, a in enumerate(asks))
                        + "\nApply each, and answer each in your summary: done, or declined with why.")
                was = code.status.get("round") or 0  # the round reviewed: the next review is after it
                # a refix is a new paid round the driver starts: held as a start is, the review
                # retaken each pass until wb run resume (and amend --budget-usd) let it go; sent
                # past the run's budget, and while the run said it was paused there (#1586, 2026-11-26)
                if not _going(run.status):
                    return
                if over := _budget_stop(run, src_stage, stage_model(src_stage, src_item)):
                    _pause_at_budget(run, over)
                    return
                r = _wb(["feedback", src_item.task, "--text", text], run)
                if r.returncode == 0:
                    run.log(kind="refix", stage=stage["id"], src=it.src, task=src_item.task,
                            count=len(fixes) + 1, round=was)
                    state, why = "superseded", f"refix {len(fixes) + 1} sent to {src_item.task}"
                else:
                    why = f"refix not sent: {_last_line(r)}"
        run.log(kind="output", stage=stage["id"], key=it.key, state=state, why=why, task=it.task)
        if phase == "review":
            _wb(["accept", it.task], run)
        return
    if phase in ("failed", "stuck", "discarded", "review", "accepted"):
        why = st.get("last") or phase
        run.log(kind="null", stage=stage["id"], key=it.key, why=why, task=it.task)
        if phase not in store.CLOSED:
            _wb(["discard", it.task, "--reason", f"run {run.name}: no valid result"], run)
        if stage.get("on_failure") == "checkpoint":
            _checkpoint(run, f"{it.task} ({stage['id']} {it.key}) ended with no result: {why}", item=True)


def _src_items(run: Run, stage: dict) -> list:
    return state(run).get(stage.get("from")) or []


def _going(st: dict) -> bool:
    """Whether the run starts, and resumes, its items: running, or at a checkpoint an item's null
    raised (`cause: item`), which holds only that item. One crashed code item stopped every other
    from starting until wb run resume: a scheduled fix or build flow sat idle all night, and each
    later night's fire was skipped (#1510, 2026-11-26)."""
    return st.get("phase") == "running" or (st.get("phase") == "checkpoint" and st.get("cause") == "item")


def _checkpoint(run: Run, why: str, item: bool = False) -> bool:
    """The checkpoint raised and said, or False when the run wasn't there to raise it: an item's
    (``item``) only from running, another over an item's too."""
    if run.set_status_if(lambda st: st.get("phase") == "running" if item else _going(st), phase="checkpoint",
                         why=why, cause="item" if item else None):
        run.notify("run_checkpoint", f"{why}. Then: wb run resume {run.name} (or wb run stop {run.name}).")
        return True
    return False


def _ack_items(run: Run) -> None:
    """Take each item's notice (the run's notify.jsonl) for the run: no session is woken for it,
    and the mod's idle net finds it acknowledged."""
    from . import cli
    for r in store.read_jsonl(run.dir / "notify.jsonl")[0]:
        if isinstance(r, dict) and isinstance(r.get("task"), str) and store.NAME_RE.match(r["task"]) \
                and isinstance(r.get("ts"), (int, float)):
            t = store.Task(r["task"])
            if t.exists and cli._acked(t) < r["ts"]:
                cli.claim_ack(t, r["ts"])


STOPPED_BY_CLAUDE = "stopped by the reviewer"  # cli.cmd_stop's `last`: wb run stop's items


def _outside(st: dict) -> bool:
    """Whether a task's stop came from outside its step and holds it rather than ending it: an
    interruption (cli._interrupted: a restart, a killed runner, a shutdown's signal, an update),
    or a stop whose cause passes or is the user's to lift (held_infra: the day cap's in a round, a
    provider's rate limit, outage or refusal, a repo made local-only, `wb stop --all`). Only the
    first were held: an item the cap or its provider stopped mid-round was taken as null, its paid
    work discarded, never run again, past midnight under --overnight nor on wb run resume (the
    review of #1278, 2026-11-25). The rest of cli.stopped_outside's (a model that failed, a setup,
    a crash, a full disk, the repo gone, OpenCode unusable) end the item: its stage's on_failure,
    null by default, as #1278 has a failed item, and as wb resume --all leaves those for Claude's
    look (cli.INTERRUPTED, #544); held, each was resumed once and then woke Claude per item (the
    third review of #1278, 2026-11-25). Claude's own `wb stop` never is, whatever `infra` it left:
    one made while the task waited behind a told cap or Agents stop kept the wait's `spend`, and
    the driver resumed the item Claude gave up on, past midnight or once the cap was raised, its
    paid tokens unasked (#1588's waits, the bar loop, 2026-11-27); wb run stop's items are
    _resumable's (_run_stopped)."""
    from . import cli
    if st.get("last") == STOPPED_BY_CLAUDE:
        return False
    return st.get("phase") in ("stuck", "failed") and (
        cli._interrupted(st.get("last")) or cli.stopped_outside(st) in held_infra())


def held_infra() -> frozenset:
    """Status `infra` values _outside holds an item for."""
    from . import cli, runner
    return frozenset({runner.SPEND, "provider", runner.LOCAL_ONLY, cli.BAILED, runner.SIGNAL_INFRA, cli.UPDATE,
                      "restart", "runner died"})


def _run_stopped(run: Run, name: str) -> bool:
    """Whether wb run stop stopped task ``name`` (journal `stopped`), not resumed since. Claude's
    own `wb stop <item>` leaves the same `last`, and wb run resume took that one up too (the
    review of #1278, 2026-11-25)."""
    kinds = [r.get("kind") for r in run.journal() if r.get("task") == name and r.get("kind") in ("stopped", "resumed")]
    return bool(kinds) and kinds[-1] == "stopped"


def _resumable(run: Run, name: str, st: dict) -> bool:
    """Whether wb run resume takes item ``name`` up where it stopped: a stop from outside its step
    (_outside), or wb run stop's."""
    stage = next((r.get("stage") for r in run.journal() if r.get("kind") == "item" and r.get("task") == name), None)
    if stage in (run.spec.get("skip") or ()):
        return False  # the user skipped its stage: it stays stopped (#1577)
    return _outside(st) or (st.get("last") == STOPPED_BY_CLAUDE and _run_stopped(run, name))


def _resume_items(run: Run) -> list[str]:
    """The run's items a stop or an interruption left, `wb resume`d where they were, with a live
    runner's reaped first; their names. Only the restarts' and kills' were missing: a reboot's
    items in flight, failed RESTARTED, were taken as null by the next tick and never ran again,
    the very case resume is for (the review of #1278, 2026-11-25). Run by the driver as it starts
    (alive, so their notices are its), or by _resume under a live one."""
    from . import cli
    out = []
    for n in dict.fromkeys(run_tasks(run)):
        t = store.Task(n)
        if not t.exists:
            continue
        if t.status.get("phase") in cli.LIVE:
            cli.reap(t, quiet=True)  # one notice at most: this resume is the news
        st = t.status
        if _resumable(run, n, st) and not cli.runner_alive(t):
            r = _wb(["resume", n], run)
            run.log(kind="resumed", task=n, by="resume", ok=r.returncode == 0,
                    why="" if r.returncode == 0 else _last_line(r))
            out.append(n)
    return out


def _held(run: Run, it: Item) -> bool:
    """Whether item ``it``, stopped from outside its step (_outside), goes on rather than being
    taken as null: `wb resume`d here while the run runs; held for wb run resume while it doesn't.
    A resume refused with exit 3 (the day cap, which clears at midnight; a repo made local-only,
    Agents on the GPU: the user's to lift) holds it too, the run paused, or under --overnight
    tried again each pass while the cap's refusal stands. A second stop from outside after the
    driver's resume is a checkpoint, Claude's call, never a loop of resumes (a spend stop aside:
    one a night, each past midnight). False for any other item, and for one whose resume was
    refused otherwise (its null, said)."""
    from . import cli, runner
    st = store.Task(it.task).status
    if not _outside(st):
        return False
    if not _going(run.status):
        return True
    mine = [r for r in run.journal() if r.get("task") == it.task]
    why_out = cli.stopped_outside(st) or st.get("last") or "stopped"
    if why_out != runner.SPEND and any(r.get("kind") == "resumed" and r.get("by") == "driver" and r.get("ok")
                                for r in mine):
        _checkpoint(run, f"{it.task} was stopped from outside again ({why_out}: {st.get('last') or ''}); "
                         f"its work is kept: wb run resume {run.name} takes it up again, or wb discard {it.task}")
        return True
    r = _wb(["resume", it.task], run)
    if r.returncode == 0:
        run.log(kind="resumed", task=it.task, by="driver", ok=True, why="")
        return True
    why = _last_line(r)
    if r.returncode != 3:
        run.log(kind="resumed", task=it.task, by="driver", ok=False, why=why)
        return False
    if not (mine and mine[-1].get("kind") == "held"):
        run.log(kind="held", task=it.task, why=why)
    if meter.RESETS in (r.stderr or "") + (r.stdout or "") and run.spec.get("overnight"):
        return True  # the day cap's: resumed on the first pass past midnight
    if run.set_status_if(lambda x: x.get("phase") == "running", phase="paused", why=why):
        run.notify("run_paused", f"paused: {why}. Done work is kept, and {it.task} waits where it stopped: "
                                 f"wb run resume {run.name} once that allows")
    return True


def _cap_held(run: Run, stage: dict, it: Item, m) -> str | None:
    """The day cap's refusal of ``it``'s last start while it still holds (that day, that cap,
    and ``m``, the item's model now: stage_model, the one refused, or one the cap still
    refuses), else None. Each pass ran a refused wb start, journalled item and retry and wrote a
    spec, and the journal's write woke the driver at once: about 7 starts a second until
    midnight at the cap, under keep-awake (#1543, 2026-11-26). A cap the user raised tries
    again, and so does a role the pause's own advice moved to the GPU or a free model: held on
    the journal row alone, `wb run resume` paused again at once with the old line until
    midnight (#1543's review, 2026-11-26)."""
    if m is None:
        return None  # the GPU's: no day cap
    rows = [r for r in run.journal() if r.get("stage") == stage["id"] and r.get("key") == it.key
            and r.get("kind") in ("item", "retry")]
    last = rows[-1] if rows else {}
    if last.get("kind") != "retry" or last.get("day") != meter.day() or last.get("cap") != meter.cap():
        return None
    if last.get("model") == m.id:
        # the model wb start refused, the same day and cap: spend only grows, so it still
        # refuses. Asked again with meter.refusal's FIRST_TOKENS, a day within the item's spec
        # of the cap (about spec bytes / 4 at the input price, $0.015 on a $3/M model) passed
        # here and wb start, which counts the spec (runner.first_tokens), refused it each pass:
        # the spin again (#1543's second review, 2026-11-26)
        return last.get("why") or f"the day cap ({meter.RESETS})"
    from . import runner
    # another model: asked as wb start asks it, round 1's first request with the spec in it
    why = meter.refusal(m, item_role(stage, it), advice=False,
                        tokens=runner.first_tokens(prompt=_spec_of(run, stage, it)))
    return why if why and meter.RESETS in why else None  # any other refusal is wb start's to say


def _room(run: Run, stage: dict, st: dict, m) -> str | None:
    """Why stage's next item can't start now (None: it can): the run's items in flight, the
    provider's slots, a user's task waiting first, the GPU's one waiting item per stage."""
    from . import cli
    flying = [i for items in st.values() if isinstance(items, list) for i in items
              if isinstance(i, Item) and i.state == "running"]
    if len(flying) >= IN_FLIGHT:
        return f"{IN_FLIGHT} items in flight"
    if m is not None:
        if cli._api_slots_free(m) <= 0:
            return f"{models.api_provider(m)}'s slots are taken"
        if any(not store.Task(n).spec.get("run") for n in cli._api_pending(m)):
            return "a task of the user's waits for a slot first"
        return None
    mine = [i for i in st.get(stage["id"]) or () if i.state == "running" and i.task]
    for i in mine:
        s = store.Task(i.task).status
        if s.get("phase") == "queued" and s.get("leased") != s.get("pid"):
            return "one item of the stage already waits for the GPU"
    return None


def _budget_stop(run: Run, stage: dict, m) -> str | None:
    """Why the next start would pass the run's budget, or None."""
    spec = run.spec
    used = spent(run)
    if m is not None and not (meter.is_plan(m) or meter.is_free(m)):
        b = spec.get("budget_usd")
        floor = meter.request_floor(m) or 0.0
        if isinstance(b, (int, float)) and used["usd"] + floor > b:
            return (f"the run's budget: {meter.usd(used['usd'])} spent of {meter.usd(b)}, and the next "
                    f"request can cost up to {meter.usd(floor)}")
    if m is None and isinstance(bm := spec.get("budget_min"), (int, float)) and used["gpu_min"] >= bm:
        return f"the run's GPU budget: {used['gpu_min']:g} of {bm:g} minutes used"
    return None


def _pause_at_budget(run: Run, why: str) -> None:
    """The run paused at ``why``, _budget_stop's, and said once: a start's, or a refix's (_take)."""
    if run.set_status_if(lambda x: x.get("phase") == "running", phase="paused", why=why):
        run.notify("run_paused", f"paused at {why}. Done work is kept: wb run report "
                                 f"{run.name}; wb run amend {run.name} --budget-usd N, then "
                                 f"wb run resume {run.name}")


def tick(run: Run) -> bool:
    """One pass: take the items that ended, start what is ready and has room, raise a checkpoint or
    the end. False once the driver is done."""
    st0 = run.status
    if st0.get("phase") not in OPEN:
        return False
    _ack_items(run)
    flow = run.flow
    from . import cli
    _stop_skipped(run)  # one a pass started after the user's skip, at its last read of the stages
    st = state(run)
    held = set()  # items interrupted from outside: resumed, or waiting for wb run resume
    for s in stages(flow):  # the ends first: a report's object, a failed one's null
        for it in st.get(s["id"]) or ():
            # a code item's null is its task's (_live); a report's is the journal's, taken already
            if not it.task or it.state not in (("running", "null") if is_code(s) else ("running",)):
                continue
            t = store.Task(it.task)
            if not t.exists:
                continue
            if it.state == "running" and t.status.get("phase") in cli.LIVE:
                cli.reap(t, quiet=True)
            if it.state == "running" and t.status.get("phase") == "asking" and (qid := t.status.get("asking")) \
                    and ask_mcp.is_open(t, qid):
                # nobody is told of an item's ask in a run (its notices are the driver's): a finder
                # asking for a path outside its lane held the GPU its 20-minute ask_timeout while
                # the other finder queued (the real GPU audit of #1278, 2026-11-26); as the bench does
                ask_mcp.answer_once(t, qid, RUN_REFUSED)
            if t.status.get("phase") not in cli.LIVE and _held(run, it):
                held.add(it.task)
            elif not is_code(s) and it.state == "running" and t.status.get("phase") not in cli.LIVE:
                _take(run, s, it)
    st = state(run)
    for s in stages(flow):
        if s.get("final") or s.get("claude"):
            continue
        for it in st.get(s["id"]) or ():
            if it.task in held:
                continue
            if is_code(s) and it.state == "null" and s.get("on_failure", "checkpoint") == "checkpoint" \
                    and escalation(it.key) >= len(s.get("escalate") or []) \
                    and not any(r.get("kind") == "notice" and r.get("item") == it.task for r in run.journal()):
                # journalled only once raised: one that came at a checkpoint or a pause was marked
                # said and never raised after the resume (#1510)
                if _checkpoint(run, f"{it.task} ({s['id']} {it.key}) is {store.Task(it.task).status.get('phase')}: "
                                    f"{it.why}; Claude decides it (wb feedback, wb resume or wb discard)", item=True):
                    run.log(kind="notice", notice="run_checkpoint", item=it.task)
    if _going(run.status):
        for s in stages(flow):
            if s.get("final") or s.get("claude"):
                continue
            for it in st.get(s["id"]) or ():
                if it.state != "pending" or not _going(run.status):
                    continue
                if s["id"] in (run.spec.get("skip") or ()):
                    break  # skipped since this pass read the stages (#1577): never started
                m = stage_model(s, it)
                if it.task and store.Task(it.task).exists:  # started, its runner not up yet
                    continue
                if _room(run, s, st, m):
                    break
                if why := _budget_stop(run, s, m):
                    _pause_at_budget(run, why)
                    break
                n = 1 + max([r.get("n") or 0 for r in run.journal() if r.get("kind") == "item"
                             and r.get("stage") == s["id"]] or [0])
                if (k := escalation(it.key)) and not any(r.get("kind") == "escalated" and r.get("stage") == s["id"]
                                                         and r.get("key") == it.key for r in run.journal()):
                    prev = escalation_key(it.key, k - 1) if k > 1 else re.sub(r"#e\d+\Z", "", it.key)
                    run.log(kind="escalated", stage=s["id"], key=it.key, after=prev, role=item_role(s, it))
                got, why = ("paused", capped) if (capped := _cap_held(run, s, it, m)) else _start(run, s, it, n, m)
                if got in ("not_run", "null"):
                    run.log(kind=got, stage=s["id"], key=it.key, why=why)
                    it.state = got
                elif got == "paused":
                    if run.spec.get("overnight"):
                        break  # the day cap's: tried again on the first pass past midnight (_cap_held)
                    # at an item's checkpoint too (_going, its cause kept): held only there, the run
                    # started paid items past midnight with nobody's wb run resume (#1543)
                    if run.set_status_if(_going, phase="paused", why=why):
                        run.notify("run_paused", f"paused: {why}. Done work is kept: wb run resume {run.name} "
                                                 "once the cap allows")
                    break
                else:
                    it.state, it.task = "running", run.task_name(s["id"], n)
                st = state(run)
    # a claude checkpoint whose inputs are all in: Claude feeds it (wb run feed)
    complete = st.get("_complete") or {}
    for s in stages(flow):
        if s.get("claude") and complete.get(s.get("from")) and st.get(s["id"]) is None:
            if not any(r.get("kind") == "notice" and r.get("stage") == s["id"] for r in run.journal()):
                n = len([i for i in st.get(s["from"]) or () if i.state == "done"])
                # never over an item's checkpoint: the resume acks that notice, and this one, journalled,
                # was never raised again, so the run sat running with nothing due (#1510, 2026-11-26).
                # It waits for the resume instead, journalled only once raised, so raised right after.
                if run.set_status_if(lambda x: x.get("phase") in ("running", "paused"),
                                     phase="checkpoint", why=f"stage {s['id']} is Claude's", cause=None):
                    run.log(kind="notice", notice="run_checkpoint", stage=s["id"])
                    run.notify("run_checkpoint", f"stage {s['id']} is yours: {n} result(s) of {s['from']} are in "
                               f"(wb run report {run.name} --stage {s['from']}). Merge them, then wb run feed "
                               f"{run.name} {s['id']} --file <list.json>")
    _write_status(run, st)
    if complete.get(stages(flow)[-1]["id"]) and run.status.get("phase") == "running":
        return _finish(run)
    return True


def _write_status(run: Run, st: dict) -> None:
    """The pass's counts and spend in status.json, written only when one changed: the driver
    watches its run directory, and a write every pass woke it at once, a loop of 20 rewrites a
    second for as long as an item ran, each pass reading the whole ledger, and every `wb wait
    <run>` with it (the review of #1278, 2026-11-25)."""
    flow = run.flow
    used = spent(run)
    fields = {"stages": {s["id"]: _counts(st.get(s["id"])) for s in stages(flow) if not s.get("final")},
              "spent_usd": used["usd"], "gpu_min": used["gpu_min"], "budget_usd": run.spec.get("budget_usd"),
              "budget_min": run.spec.get("budget_min")}
    run.set_status_if(lambda cur: any(cur.get(k) != v for k, v in fields.items()), **fields)


def _finish(run: Run) -> bool:
    rep = report_of(run.name)
    store.write_json(run.dir / "result.json", rep)
    if run.set_status_if(lambda x: x.get("phase") == "running", phase="review", why="its final stage is Claude's",
                         pid=None):
        open_code = [c for c in rep["code"] if c["phase"] not in store.CLOSED]
        run.notify("run_done", f"{len(rep['final'])} result(s), {len(rep['filtered'])} filtered, "
                   f"{len(rep['null'])} null, {len(rep['not_run'])} not run"
                   + (f", {len(open_code)} change(s) at review" if open_code else "")
                   + f" · {meter.usd(rep['spend']['usd'])}"
                   + f". Read it: wb run report {run.name}; decide each item, then wb run close {run.name}")
    return False


def drive(name: str) -> None:
    """The detached driver: one per run (its `drive` lock), until the run leaves OPEN."""
    run = Run(name)
    from .watch import Watcher
    try:
        with store.locked(store.task_lock(name, "drive"), blocking=False):
            # its own pid, not spawn's: cli.detach's keep-awake wrapper (systemd-inhibit) is that one
            run.set_status(pid=os.getpid(), start=models._started(os.getpid()), machine=store.machine())
            signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
            store.tasks_dir().mkdir(parents=True, exist_ok=True)  # watched: an item's status moves there
            if run.status.get("phase") == "running":
                _resume_items(run)  # alive now: their notices are this driver's, before a tick takes one
            with Watcher(run.dir, store.tasks_dir()) as w:
                while tick(run):
                    w.wait(TICK_S)
    except BlockingIOError:
        print(f"run {name}: another driver holds it", file=sys.stderr)
        return
    except Exception as e:  # a bug: the run fails, said, never a silent stop
        import traceback
        traceback.print_exc()
        if run.set_status_if(lambda x: x.get("phase") in OPEN, phase="failed", why=f"the driver crashed: {e}"[:300],
                             pid=None):
            run.notify("run_failed", f"the driver crashed ({e}); its log: {run.dir / 'driver.log'}. Done work "
                                     f"is kept: wb run resume {name} starts a new driver")
    finally:
        run.set_status_if(lambda x: x.get("pid") == os.getpid(), pid=None)


def driver_alive(run: Run) -> bool:
    st = run.status
    pid = st.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return False
    if store.elsewhere(st):
        return True
    return models._alive(pid, st.get("start")) and models.runs(pid, "-m", "workbench.runs", "drive", run.name)


def spawn(run: Run) -> int:
    from . import cli, context
    env = context.detached_env()
    env.pop("WB_TASK", None)
    with open(run.dir / "driver.log", "a") as log:
        proc = cli.detach([sys.executable, "-X", "utf8", "-m", "workbench.runs", "drive", run.name],
                          awake=f"wb run {run.name}", cwd=PKG_ROOT, env=env, stdout=log, stderr=log)
    run.set_status(spawned=proc.pid)  # the driver writes its own pid (reap's grace covers the gap)
    return proc.pid


def reap(run: Run, quiet: bool = False) -> bool:
    """A run whose driver died while it was open: stopped, said once (run_stopped). True when.
    ``quiet``: no run notice, a restart's batch notice for its items saying it (cli._restart_notice)."""
    if run.status.get("phase") not in OPEN or driver_alive(run):
        return False
    if (time.time() - (run.status.get("updated_at") or 0)) < 5:
        return False  # just started: its driver may not have written its pid yet
    if run.set_status_if(lambda x: x.get("phase") in OPEN and not driver_alive(run), phase="stopped",
                         why="its driver died (a reboot, a kill)", pid=None):
        if quiet:
            run.log(kind="reaped")
        else:
            run.notify("run_stopped", f"its driver died; done work is kept. wb run resume {run.name} goes on "
                                      f"from its journal, wb run report {run.name} says what is in")
        return True
    return False


# --- the report ----------------------------------------------------------------------------------

def report_of(name: str) -> dict:
    """{final: [{key, result, trail}], results: [{key, result, trail}], filtered: [{key, why,
    result}], null: [{key, why}], not_run: [{key, why}], code: [{key, task, phase}], spend}: what
    the run's items returned, every item accounted for (no silent caps). `results`: the done items
    of a stage no later one reads (best-of's reviews), which no report showed though each was
    paid for (#1504, 2026-11-26). Agents' strings are data."""
    run = Run(name)
    if not run.exists:
        raise RuntimeError(f"no run {name!r} (wb run status lists them)")
    flow = run.flow
    st = state(run)
    by = {(i.stage, i.key): i for items in st.values() if isinstance(items, list) for i in items}
    sts = [s for s in stages(flow)]
    last = sts[-1]
    feeds = last.get("from") or next((s["id"] for s in reversed(sts[:-1])), None)
    read = {s.get("from") for s in sts}

    def trail(it):
        """What ``it`` came from, nearest first: each item up the `from` chain; a batch's
        members, each as its judge was given it (with its `id`), where a batch's trail stopped
        at None: lenses' and triage's judgments reached Claude without what they judged (#1134)."""
        out, cur, seen = [], it, set()
        while cur is not None and (cur.stage, cur.key) not in seen:
            seen.add((cur.stage, cur.key))
            src_stage = (stage_by(flow, cur.stage) or {}).get("from")
            if cur.members is not None:
                out += [{"stage": src_stage or cur.stage, "key": k, "result": v} for k, v in cur.members]
                break
            if cur.src is None:
                break
            cur = by.get((src_stage, cur.src))
            if cur is not None:
                out.append({"stage": cur.stage, "key": cur.key, "result": cur.output})
        return out
    rep = {"run": name, "flow": flow.get("name"), "phase": run.status.get("phase"),
           "final": [], "results": [], "filtered": [], "superseded": [], "null": [], "not_run": [], "code": [], "pending": []}
    for s in sts:
        items = st.get(s["id"])
        if items is None and not s.get("final"):
            rep["pending"].append({"stage": s["id"], "why": "waits for its inputs"})
            continue
        for it in items or ():
            k = f"{s['id']}:{it.key}"
            # only a task there is: _start logs the item's task before wb start runs, so a code
            # item refused at its start (a role with no key) read "1 change(s) at review", and
            # the report named a task that never was (#1531, 2026-11-26), as cmd_close filters
            if is_code(s) and it.task and store.Task(it.task).exists:
                rep["code"].append({"key": k, "task": it.task, "phase": store.Task(it.task).status.get("phase")})
            if it.state == "done" and s["id"] == feeds and not is_code(s):
                rep["final"].append({"key": k, "result": it.output, "trail": trail(it)})
            elif it.state == "done" and s["id"] not in read and not (is_code(s) or s.get("claude")):
                rep["results"].append({"key": k, "result": it.output, "trail": trail(it)})
            elif it.state == "filtered":
                rep["filtered"].append({"key": k, "why": it.why, "result": it.output})
            elif it.state == "superseded":
                rep["superseded"].append({"key": k, "why": it.why, "result": it.output})
            elif it.state == "null":
                rep["null"].append({"key": k, "why": it.why,
                                    **({"members": [mk for mk, _ in it.members]} if it.members else {})})
            elif it.state == "not_run":
                rep["not_run"].append({"key": k, "why": it.why})
            elif it.state in ("pending", "running", "waiting"):
                rep["pending"].append({"key": k, "task": it.task})
            for mk in unanswered(s, it):
                rep["not_run"].append({"key": f"{s['id']}:{mk}", "why": f"unjudged: {k} returned nothing for it"})
    used = spent(run)
    rep["spend"] = {"usd": used["usd"], "budget_usd": run.spec.get("budget_usd"), "gpu_min": used["gpu_min"],
                    "budget_min": run.spec.get("budget_min")}
    return rep


def report_lines(rep: dict, stage: str | None = None) -> list[str]:
    """The report as Claude reads it: short lines, each result as JSON (data)."""
    out = [f"run {rep['run']} · flow {rep['flow']} · {rep['phase']} · {meter.usd(rep['spend']['usd'])}"
           + (f" of {meter.usd(rep['spend']['budget_usd'])}" if rep["spend"].get("budget_usd") is not None else "")
           + (f" · GPU {rep['spend']['gpu_min']:g} min" if rep["spend"].get("gpu_min") else "")]
    def js(v):
        return json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    for what in ("final", "results", "filtered", "superseded", "null", "not_run", "pending"):
        rows = [r for r in rep[what] if stage is None or str(r.get("key", "")).startswith(f"{stage}:")
                or r.get("stage") == stage]
        if not rows:
            continue
        out.append(f"{what} ({len(rows)}):")
        for r in rows:
            if what in ("final", "results"):
                out.append(f"  {r['key']}: {js(r['result'])}" + (f"  ← {js([t['result'] for t in r['trail']])}"
                                                                if r["trail"] else ""))
            elif what in ("filtered", "superseded"):
                out.append(f"  {r['key']}: {r['why']}: {js(r['result'])}")
            elif what == "pending":
                out.append(f"  {r.get('key') or r.get('stage')}: {r.get('task') or r.get('why')}")
            else:
                out.append(f"  {r['key']}: {r['why']}" + (f" (its items: {', '.join(r['members'])})"
                                                         if r.get("members") else ""))
    if stage is not None:  # a checkpoint's look at one stage: its every done item
        st = state(Run(rep["run"]))
        for it in st.get(stage) or ():
            if it.state == "done" and not any(r["key"] == f"{stage}:{it.key}" for r in rep["final"] + rep["results"]):
                out.append(f"  {stage}:{it.key}: {js(it.output)}")
    if rep["code"]:
        out.append("code (Claude's to review, accept or discard): "
                   + ", ".join(f"{c['task']} {c['phase']}" for c in rep["code"]))
    return out


def status_line(name: str) -> str:
    run = Run(name)
    st = run.status
    parts = [f"run {name}", f"flow {run.flow.get('name')}", st.get("phase") or "?"]
    for sid, c in (st.get("stages") or {}).items():
        bits = [f"{v} {k}" for k, v in c.items() if v]
        if bits:
            parts.append(f"{sid} " + ", ".join(bits))
    b = st.get("budget_usd")
    parts.append(meter.usd(st.get("spent_usd") or 0) + (f" of {meter.usd(b)}" if isinstance(b, (int, float)) else ""))
    line = " · ".join(parts)
    return line + (f"\n  why: {st['why']}" if st.get("why") and st.get("phase") != "running" else "")


def estimate(flow: dict, params: dict, exists: dict | None = None) -> tuple[float | None, float, list[str]]:
    """(dollars for the stages whose items are known now, GPU minutes for them, the stages whose
    count is known only as they run), from each card's `est` × meter.price; the items
    `unless_exists` skips (``exists``, existing's) cost nothing."""
    usd, mins, later = 0.0, 0.0, []
    priced = True
    for s in stages(flow):
        if s.get("final") or s.get("claude"):
            continue
        if "items" not in s:
            later.append(s["id"])
            continue
        n = len(_items_of(s, Run("-"), params)) - len((exists or {}).get(s["id"]) or ())
        n = min(n, int(s.get("max_items") or MAX_ITEMS)) * int(s.get("attempts") or 1)
        if (s.get("batch") or 1) > 1:
            n = math.ceil(n / s["batch"])
        try:
            est = cards.load(s["card"]).est if s.get("card") else {}
        except RuntimeError:
            est = {}
        m = stage_model(s)
        if m is None:
            mins += n * float(est.get("min") or 0)
            continue
        p = meter.price(m)
        if not p:
            priced = priced and (meter.is_plan(m) or meter.is_free(m))
            continue
        usd += n * (float(est.get("in") or 0) * p[0]["input"] + float(est.get("out") or 0) * p[0]["output"]) / 1e6
    return (round(usd, 4) if priced else None), mins, later


# --- wb flow import: a command file's prompt as a flow (#1278) ------------------------------------

ARGUMENTS = "$ARGUMENTS"
POSITIONAL = re.compile(r"\$[1-9]\b")
IMPORT_STAGE = "run"


def import_command(text: str, name: str, card: str | None = None, source: str = "") -> dict:
    """A slash command's Markdown (Claude Code's `.claude/commands/`, OpenCode's `command/`: an
    optional frontmatter, then the prompt) as a flow of one stage over a list param `items`, each
    item one task whose spec is the prompt with `$ARGUMENTS` as `{item}` (the plan's mapping, §3);
    the prompt's own braces kept as text (`{{`, `}}`). A code stage, or ``card``'s. RuntimeError
    with why: a positional `$1` (no item fills it), no prompt, or a flow validate refuses."""
    front, body = cards.parse(text)
    body = body.strip()
    if not body:
        raise RuntimeError("the file has no prompt after its frontmatter")
    if POSITIONAL.search(body):
        raise RuntimeError(f"{POSITIONAL.search(body).group(0)}: an imported command takes only {ARGUMENTS} "
                           "(each item fills it); write the flow yourself (reference/deep.md)")
    if ARGUMENTS not in body:  # the item still reaches the agent, after the command's own text
        body += f"\n\n{ARGUMENTS}"
    prompt = body.replace("{", "{{").replace("}", "}}").replace(ARGUMENTS, "{item}")
    stage = {"id": IMPORT_STAGE, **({"card": card} if card else {"kind": "code"}),
             "items": "{params.items}", "prompt": prompt}
    desc = front.get("description") if isinstance(front.get("description"), str) else ""
    flow = {"v": 1, "name": name, "description": desc or f"imported from {source or 'a command file'}",
            "params": {"items": "list"}, "stages": [stage, {"id": "claude", "final": True, "from": IMPORT_STAGE}]}
    if errs := validate(flow):
        raise RuntimeError("; ".join(errs[:6]))
    return flow


def cmd_flow_import(a) -> None:
    src = Path(a.file).expanduser()
    try:
        text = src.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        _die(f"{src}: {getattr(e, 'strerror', None) or e}")
    name = a.name or re.sub(r"[^a-z0-9-]+", "-", src.stem.lower()).strip("-")[:24]
    if not RUN_RE.match(name or ""):
        _die(f"{name!r} can't name a flow: --name, lowercase letters, digits and hyphens, at most 24")
    out = Path(a.out).expanduser() if a.out else flows_dir() / f"{name}.json"
    if out.exists() and not a.force:
        _die(f"{out} exists (--force replaces it)")
    try:
        flow = import_command(text, name, a.card, src.name)
    except RuntimeError as e:
        _die(f"{src}: {e}")
    out.parent.mkdir(parents=True, exist_ok=True)
    store.write_json(out, flow)
    how = f"--flow {name}" if out.resolve() == (flows_dir() / f"{name}.json").resolve() else f"--plan {out}"
    print(f"imported {src.name} as flow {name} ({out}): one {a.card or 'code'} task per item, "
          f"{ARGUMENTS} as {{item}}. Run it: wb run start <run> {how} --param items=<a,b,…>")


# --- what wb top --json carries of the runs (#1278): the pane's and the web's seam -----------------

def snapshot(live: bool = True) -> list[dict]:
    """Each run not closed, from its files alone (no state() rebuild, no reap: wb top ticks every
    2 s): `{run, flow, phase, why, alive, stages, spent_usd, budget_usd, budget_min, gpu_min,
    session, cwd}`, by its start. ``live`` False: `alive` None, no driver asked after (a `ps`
    each on macOS: the web's state is built on every change, chat.open_runs)."""
    out = []
    for name in all_runs():
        run = Run(name)
        st = run.status
        if st.get("phase") == "closed":  # before run.json, which carries the whole flow: the web's
            continue                     # state reads this on every change too (chat.open_runs)
        sp = run.spec
        out.append({"run": name, "flow": sp.get("flow_name") or run.flow.get("name"), "phase": st.get("phase"),
                    "why": st.get("why") or "", "alive": st.get("phase") in OPEN and driver_alive(run) if live else None,
                    "stages": st.get("stages") or {}, "spent_usd": st.get("spent_usd") or 0.0,
                    "budget_usd": st.get("budget_usd"), "budget_min": sp.get("budget_min"),
                    "gpu_min": st.get("gpu_min") or 0.0, "session": sp.get("session"), "cwd": sp.get("cwd"),
                    "created": sp.get("created")})
    return sorted(out, key=lambda r: r.get("created") or 0)


# --- wb run ----------------------------------------------------------------------------------------

def _die(msg: str, code: int = 1):
    print(f"wb: {msg}", file=sys.stderr)
    sys.exit(code)


def need(name: str) -> Run:
    run = Run(name)
    if not (RUN_RE.match(name or "") and run.exists):
        _die(f"no run {name!r} (wb run status lists them)")
    return run


def _budget(flow: dict, a, said: list) -> tuple[float | None, float | None]:
    """(dollars, GPU minutes) the run may spend: --budget-usd, else the flow's, else what the day
    cap leaves today; never past the cap's rest (the cap still checks every start)."""
    c = meter.cap()
    left = None if c is None else max(0.0, round(c - meter.today()["usd"], 4))
    usd_ = a.budget_usd if a.budget_usd is not None else flow.get("budget_usd")
    if usd_ is None:
        usd_ = left
        if left is not None:
            said.append(f"budget: today's cap left, {meter.usd(left)} (--budget-usd sets less)")
    elif left is not None and usd_ > left and not a.overnight:
        said.append(f"budget: {meter.usd(usd_)} asked, today's cap leaves {meter.usd(left)}: the cap "
                    "pauses the run first (--overnight goes on past midnight)")
    mins = a.budget_min if a.budget_min is not None else flow.get("budget_min")
    return usd_, mins


def cmd_start(a) -> None:
    from . import cli, context
    if not RUN_RE.match(a.name or ""):
        _die("a run's name: lowercase letters, digits and hyphens, at most 24 (its items are <run>-<stage>-<n>)")
    if why := taken(a.name):
        _die(why)
    if bool(a.flow) == bool(a.plan):
        _die("--flow <name> or --plan <file>, one of them (wb run flows lists the flows)")
    if a.overnight and a.budget_usd is None:
        _die("--overnight goes with --budget-usd: a run past midnight has a budget of its own")
    try:
        flow = load_flow(a.flow, a.plan)
    except RuntimeError as e:
        _die(str(e))
    # a subdirectory is the repo it is in, as wb start's --repo: from repo/packages/web (a
    # monorepo's package, or Claude's Bash cwd moved) every flow command and deepcode was refused
    # "is no git repository" (#1494, 2026-11-26). The run reads the repo from its top (files params
    # and base); each item starts from the directory given, so wb start finds its package there
    start_dir = Path(a.repo or os.getcwd()).resolve()
    try:
        repo = gitops.toplevel(start_dir).resolve()
    except gitops.GitError:
        _die(f"{start_dir} is no git repository (--repo)")
    base = gitops.base_commit(repo, a.base or "HEAD")
    if not base:
        _die(f"no commit {a.base or 'HEAD'} in {repo}")
    try:
        given = [*(parse_args(flow, a.args) if getattr(a, "args", None) else []), *(a.param or [])]
        params = parse_params(flow, given, repo, base)
        exists = existing(flow, params, repo, base)
    except (RuntimeError, gitops.GitError) as e:
        _die(str(e))
    said: list[str] = []
    for s in stages(flow):  # an API role with no cap, or that can't run, is refused before any file is written
        if s.get("final") or s.get("claude"):
            continue
        for k in range(len(s.get("escalate") or []) + 1):  # its escalate roles' too
            probe = Item(s["id"], "1" + (f"#e{k}" if k else ""), None)
            if why := role_cant_run(item_role(s, probe)):  # wb start's exit 3, before a driver (#1532)
                _die(f"stage {s['id']}: {why}", 3)
            m = stage_model(s, probe)
            if m is not None and not (meter.is_plan(m) or meter.is_free(m)) and meter.cap() is None:
                _die(f"stage {s['id']} runs on {m.id} (role {item_role(s, probe)}), and {meter.no_cap()}")
    usd_, mins = _budget(flow, a, said)
    ctx = a.context or ""
    if a.context_file:
        try:  # said, never a traceback (the review of #1278, 2026-11-25)
            ctx = Path(a.context_file).expanduser().read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            _die(f"--context-file {a.context_file}: {getattr(e, 'strerror', None) or e}")
    ctx = ctx or flow.get("context") or ""
    if len(ctx) > CUTS["context"]:
        # run.json keeps it whole and each spec's {context} is cut there, said (runs._text): cut
        # here first, the spec's cut note never showed and past 4,000 characters went unsaid
        # (#1519, 2026-11-26)
        said.append(f"context: {len(ctx):,} characters, the first {CUTS['context']:,} go in each spec's {{context}}")
    est_usd, est_min, later = estimate(flow, params, exists)
    roles = ", ".join(dict.fromkeys(f"{s['id']} {stage_role(s)}" + (" (API)" if stage_model(s) else " (GPU)")
                                    + (f", then {', '.join(s['escalate'])}" if s.get("escalate") else "")
                                    for s in stages(flow) if not (s.get("final") or s.get("claude"))))
    est = (meter.usd(est_usd) if est_usd is not None else "unpriced") + (f", GPU ~{est_min:g} min" if est_min else "")
    # tracked files only, as wb start's (#1322): every item starts from the base without them,
    # and the driver drops each wb start's own note, so a whole run of paid items worked on the
    # tree without the edits it was about, unsaid (#1570, 2026-11-26)
    dirty = [] if not gitops.git(repo, "status", "--porcelain", "--untracked-files=no").strip() else \
        [f"note: the repo has uncommitted changes; every item starts from {a.base or 'HEAD'} without them"]
    if getattr(a, "dry_run", False):
        for line in dry_run(a.name, flow, params, exists, ctx, base, est, later, usd_) + dirty:
            print(line)
        for line in said:
            print(line)
        return
    d = run_dir(a.name)
    d.mkdir(parents=True)
    for sub in ("inbox", "outputs", "specs"):
        (d / sub).mkdir()
    sha = flow_sha(flow)
    flow = settled(flow)
    spec = {"name": a.name, "flow": flow, "flow_name": flow["name"], "flow_sha": sha,
            "params": params, **({"exists": exists} if exists else {}),
            "context": ctx, "repo": str(repo),
            **({"package_dir": str(start_dir)} if start_dir != repo else {}), "base": base,
            # the branch it started from: its items get --base <sha>, which records no branch,
            # and wb accept --merge merged one into whatever branch was checked out by then
            # (#1541, 2026-11-26); null for a detached HEAD, tag or commit, as a task's
            "base_ref": a.base, "base_branch": gitops.start_branch(repo, a.base or "HEAD"), "cwd": os.getcwd(), "budget_usd": usd_, "budget_min": mins,
            "overnight": bool(a.overnight), "trial": bool(a.trial), "no_claude_md": bool(a.no_claude_md),
            "created": store.now(), "driver": {"python": sys.executable, "machine": store.machine()},
            **({"session": sid} if (sid := os.environ.get("CLAUDE_CODE_SESSION_ID")) else {}),
            # or an OpenCode driver session's (#1309, as wb start's task.json): `wb run stop` and
            # `wb stop --all` there take its runs, the Claude Code mod's never by directory
            **({"session": oc, "session_kind": "opencode"}
               if not sid and (oc := store.opencode_session()) else {}),
            **({"claude_pid": int(pid), "claude_start": models._started(int(pid))}
               if (pid := os.environ.get("CLAUDE_PID", "")).isdigit() else {})}
    store.write_json(d / "run.json", spec)
    Run(a.name).set_status(phase="running", why="", stages={}, spent_usd=0.0, budget_usd=usd_)
    run = Run(a.name)
    run.log(kind="start", flow=flow["name"], flow_sha=spec["flow_sha"])
    pid = spawn(run)
    print(f"run {a.name}: flow {flow['name']} · {roles} · estimate {est}"
          + (f" (and {', '.join(later)} as their items come)" if later else "")
          + (f" · budget {meter.usd(usd_)}" if usd_ is not None else "") + f" · driver pid {pid}")
    for line in dirty + said:
        print(line)
    # its notices wake the session that started it, through the mod or the OpenCode plugin, as a
    # task's do (#1278's S9); with neither, wb wait in the background is the wake (#1278's review)
    print(f"stop: wb run stop {a.name}. Its notices (a checkpoint, a pause, the end) wake this session as a "
          f"task's do: end your turn; without the mod or the OpenCode plugin, wb wait {a.name} in the "
          f"background, again after each. Don't poll.")


def dry_run(name: str, flow: dict, params: dict, exists: dict, ctx: str, base: str, est: str,
            later: list[str], usd_) -> list[str]:
    """`wb run start --dry-run` (#1295): what the run would do, writing and starting nothing: a
    line per stage (its role and side, where its items come from), the first item's spec of each
    stage whose items are known before the run (cut at DRY_SPEC_CUT), and the estimate."""
    out = [f"dry run {name}: flow {flow['name']} at {base[:12]} (nothing written, nothing started)"]
    for s in stages(flow):
        sid = s["id"]
        if s.get("final"):
            out.append(f"  {sid}: final, yours" + (f" (over {s['from']})" if s.get("from") else ""))
            continue
        if s.get("claude"):
            out.append(f"  {sid}: a checkpoint of yours over {s['from']} (wb run feed)")
            continue
        side = "API" if stage_model(s) else "GPU"
        what = s.get("card") or "code"
        if "items" in s:
            n = len(_items_of(s, Run("-"), params))
            skip = len((exists or {}).get(sid) or ())
            src = f"{n} item(s)" + (f" from {s['items']}" if isinstance(s["items"], str) else "") + \
                (f", {skip} skipped: exists at base" if skip else "")
            cap = int(s.get("max_items") or MAX_ITEMS)
            if n - skip > cap:
                src += f", {n - skip - cap} past max_items {cap}"
        else:
            src = f"from {s['from']}" + (f" split by {s['split']}" if s.get("split") else "") + \
                (", once all are in" if s.get("flow") == "barrier" else ", as each comes")
        att = int(s.get("attempts") or 1)
        out.append(f"  {sid}: {what} on {stage_role(s)} ({side}) · {src}" + (f" · {att} attempts" if att > 1 else "")
                   + (f" · batches of {s['batch']}" if (s.get("batch") or 1) > 1 else ""))
    for s in stages(flow):
        if "items" not in s or s.get("final") or s.get("claude"):
            continue
        there = set((exists or {}).get(s["id"]) or ())
        first = next((v for v in _items_of(s, Run("-"), params)
                      if not (there and unless_path(s, v, params) in there)), None)
        if first is None:
            continue
        b = int(s.get("batch") or 1)
        if b > 1:  # the first batch, its members as the judge gets them
            first = [member(str(i + 1), v) for i, v in enumerate(_items_of(s, Run("-"), params)[:b])]
        text = render(s.get("prompt") or DEFAULT_PROMPT, {
            "item": first, "params": params,
            "context": ctx, "run": name, "stage": s["id"], "base": base})
        cut = text if len(text) <= DRY_SPEC_CUT else text[:DRY_SPEC_CUT] + f"\n[… {len(text) - DRY_SPEC_CUT:,} characters more]"
        out += [f"--- {s['id']}'s first item's spec ---", cut]
    out.append(f"estimate {est}" + (f" (and {', '.join(later)} as their items come)" if later else "")
               + (f" · budget {meter.usd(usd_)}" if usd_ is not None else ""))
    return out


def _runs_in_scope(a) -> list[Run]:
    from . import cli
    where, sessions, _ = cli._caller(a)
    out = []
    for n in all_runs():
        run = Run(n)
        sp = run.spec
        if sessions and sp.get("session") and sp["session"] not in sessions:
            continue
        if sessions and not sp.get("session") and where and sp.get("cwd") != where:
            continue
        out.append(run)
    return out


def cmd_status(a) -> None:
    runs_ = [need(a.name)] if a.name else [r for r in _runs_in_scope(a) if a.all or r.status.get("phase") != "closed"]
    for r in runs_:
        reap(r)
    if a.json:
        print(json.dumps([{"run": r.name, "flow": r.flow.get("name"), **r.status} for r in runs_]))
        return
    if not runs_:
        print("no open runs" + ("" if a.all else " (--all lists closed ones too)"))
    for r in runs_:
        print(status_line(r.name))


def cmd_report(a) -> None:
    run = need(a.name)
    reap(run)
    rep = report_of(run.name)
    if a.stage and stage_by(run.flow, a.stage) is None:
        _die(f"run {run.name} has no stage {a.stage!r}")
    if a.json:
        print(json.dumps(rep, ensure_ascii=False))
        return
    for line in report_lines(rep, a.stage):
        print(line)


def _resume(run: Run) -> str:
    """The run running again, its driver started unless one lives: '' or why not. The items a
    stop or an interruption left go on where they were (_resume_items): by the new driver as it
    starts, so their notices are its, or here under a live one."""
    if why := _ended(run):
        return why
    run.set_status(phase="running", why="")
    _ack_run(run)
    if driver_alive(run):
        _resume_items(run)
        return ""
    spawn(run)
    return ""


def _ended(run: Run) -> str:
    """Why run can't go on (closed, or at its end, `review`), else ''. wb run feed skipped it, and
    on a closed run set the phase back to running and spawned a driver (the review of #1278)."""
    phase = run.status.get("phase")
    return f"run {run.name} is {phase}" if phase in ("closed", "review") else ""


def _ack_run(run: Run) -> None:
    rows = [r for r in store.notices(runs=True) if r.get("run") == run.name and "task" not in r]
    if rows:
        run.claim_ack(max(r["ts"] for r in rows))


def awaiting_feed(run: Run) -> dict | None:
    """The `claude` stage whose inputs are all in, whose checkpoint tick has raised (its notice
    row journalled) and that Claude hasn't fed yet, or None. Unraised, it sits under an item's
    checkpoint, and only the resume lets tick raise it (#1510): #1546's refusal took that resume
    too, exit 1, and the merge was never raised (#1546's review, 2026-11-26)."""
    st = state(run)
    complete = st.get("_complete") or {}
    raised = {r.get("stage") for r in run.journal() if r.get("kind") == "notice" and r.get("stage")}
    return next((s for s in stages(run.flow) if s.get("claude") and complete.get(s.get("from"))
                 and st.get(s["id"]) is None and s["id"] in raised), None)


def cmd_resume(a) -> None:
    run = need(a.name)
    reap(run)
    # at a claude stage only its feed goes on: a resume set the run running, acked the checkpoint
    # tick raises once, and left it with nothing due, every later scheduled fire skipped (#1546)
    if not _ended(run) and (s := awaiting_feed(run)):
        _die(f"run {run.name} waits for Claude's merge at stage {s['id']}: wb run report {run.name} --stage "
             f"{s['from']}, then wb run feed {run.name} {s['id']} --file <list.json> (a resume can't go on)")
    phase = run.status.get("phase")
    if phase == "running" and driver_alive(run):
        print(f"run {run.name} is running")
        return
    if why := _resume(run):
        _die(why)
    print(f"resumed {run.name}: done work kept, the rest goes on")


def _stop_driver(run: Run) -> None:
    # a driver just spawned writes its pid within a moment: wait for it, or a stop missed it
    deadline = time.time() + 5
    while not driver_alive(run) and isinstance(sp := run.status.get("spawned"), int) and sp > 0 \
            and models._alive(sp) and time.time() < deadline:
        time.sleep(0.1)
    if driver_alive(run) and not store.elsewhere(run.status):
        try:
            os.kill(run.status["pid"], signal.SIGTERM)
        except OSError:
            pass
        from .watch import wait_exit
        wait_exit(run.status["pid"], 10)


def cmd_stop(a) -> None:
    run = need(a.name)
    # the web app's Stop (settings.py's run row): the user's, so the run's session hears of it as a
    # notice (Claude's own stop is news to no one); a run-* row is no settings change (#1278's S12)
    web = os.environ.get("WB_ACTOR") == "web" and store.caller_kind() == "terminal"
    stopped = stop(run, WEB_STOP if web else "stopped by Claude (wb run stop)")
    if stopped is None:
        _die(f"run {run.name} is {run.status.get('phase')}, not running")
    if web:
        run.notify("run_stopped", f"{WEB_STOP}; done work is kept"
                   + (f", and {len(stopped)} item(s) stopped" if stopped else "")
                   + f". wb run report {run.name} says what is in; wb run resume {run.name} goes on, on the user's word")
    print(f"stopped {run.name}" + (f" and {len(stopped)} item(s): {', '.join(stopped)}" if stopped else "")
          + f". Done work is kept: wb run report {run.name}; wb run resume {run.name} goes on")


def stop(run: Run, why: str, items: bool = True) -> list[str] | None:
    """Stop the driver, then (``items``) the items still running: the run kept, stopped. None when
    it wasn't open. wb stop --all stops the items itself, with every other task."""
    if run.status.get("phase") not in OPEN:
        return None
    run.set_status(phase="stopped", why=why)
    _stop_driver(run)
    out = []
    for n in run_tasks(run) if items else ():
        t = store.Task(n)
        if t.exists and t.status.get("phase") in store.ACTIVE:
            r = _wb(["stop", n], run)
            if r.returncode == 0:
                run.log(kind="stopped", task=n)  # wb run resume's to take up (_resumable)
                out.append(n)
    run.set_status(pid=None)
    return out


def cmd_amend(a) -> None:
    run = need(a.name)
    if run.status.get("phase") == "closed":
        _die(f"run {run.name} is closed")
    fields, said = {}, []
    if a.budget_usd is not None:
        fields["budget_usd"] = a.budget_usd
        said.append(f"budget {meter.usd(a.budget_usd)}")
    if a.budget_min is not None:
        fields["budget_min"] = a.budget_min
        said.append(f"GPU budget {a.budget_min:g} min")
    if a.overnight is not None:
        if a.overnight and (fields.get("budget_usd", run.spec.get("budget_usd")) is None):
            _die("--overnight goes with --budget-usd")
        fields["overnight"] = a.overnight
        said.append("overnight" if a.overnight else "not overnight")
    if a.skip:
        bad = [s for s in a.skip if stage_by(run.flow, s) is None or stage_by(run.flow, s).get("final")]
        if bad:
            _die(f"no stage {', '.join(bad)} to skip (its stages: "
                 + ", ".join(s["id"] for s in stages(run.flow) if not s.get("final")) + ")")
        fields["skip"] = sorted(set(run.spec.get("skip") or ()) | set(a.skip))
        said.append(f"skip {', '.join(a.skip)}")
    if not fields:
        _die("nothing to amend: --budget-usd, --budget-min, --overnight or --skip")
    run.amend(**fields)
    run.set_status(budget_usd=run.spec.get("budget_usd"), budget_min=run.spec.get("budget_min"))
    run.log(kind="amend", **fields)
    if a.skip:
        # the skipped stage's items at work stop: they ran on, paid, their results dropped and
        # their notices Claude's once the run ended under them (#1577, 2026-11-26)
        gone = _stop_skipped(run)
        said[-1] += (f"; stopped {len(gone)} running item(s): {', '.join(gone)}" if gone
                     else "; none of its items running")
    print(f"amended {run.name}: {'; '.join(said)}"
          + (f". It is {run.status.get('phase')}: wb run resume {run.name} goes on"
             if run.status.get("phase") in ("paused", "checkpoint", "stopped") else ""))


def cmd_feed(a) -> None:
    """Claude's merge at a `claude` checkpoint: the list of items the next stage takes."""
    run = need(a.name)
    s = stage_by(run.flow, a.stage)
    if not s or not s.get("claude"):
        _die(f"stage {a.stage!r} of run {run.name} isn't a claude checkpoint")
    if why := _ended(run):  # before the inbox is written: nothing reopens a closed run
        _die(why)
    try:
        got = json.loads(Path(a.file).read_text(encoding="utf-8") if a.file != "-" else sys.stdin.read())
    except (OSError, ValueError) as e:
        _die(f"--file: a JSON list ({e})")
    if not isinstance(got, list):
        _die("--file: a JSON list, one entry per item of the next stage")
    store.write_json(run.dir / "inbox" / f"{a.stage}-feed", got)
    run.log(kind="fed", stage=a.stage, n=len(got))
    if why := _resume(run):
        _die(why)
    print(f"fed {run.name} {a.stage}: {len(got)} item(s); the run goes on")


def cmd_close(a) -> None:
    run = need(a.name)
    # code is any item not started as a report, from its own task (#1470): never the card as it
    # reads now, which an edit may have made unloadable
    open_code = [n for n in run_tasks(run) if store.Task(n).exists
                 and store.Task(n).status.get("phase") not in store.CLOSED
                 and not store.Task(n).spec.get("report")]
    if open_code:
        _die(f"run {run.name} has code waiting on you: {', '.join(open_code)} (wb accept or wb discard each)")
    if run.status.get("phase") in OPEN:
        stop(run, "closed")
    for n in run_tasks(run):  # a report item never taken (stopped first): its worktree goes
        t = store.Task(n)
        if t.exists and t.status.get("phase") not in store.CLOSED:
            _wb(["discard", n, "--reason", f"run {run.name} closed"], run)
    if not (run.dir / "result.json").exists():
        store.write_json(run.dir / "result.json", report_of(run.name))
    run.set_status(phase="closed", why="closed by Claude", pid=None)
    _ack_run(run)
    print(f"closed {run.name}: its report stays (wb run report {run.name})")


def flow_rows(repo: Path | None = None) -> list[dict]:
    """Every flow as `wb run flows --json` gives it: {name, where, description, mark (a user's
    shadowing a shipped one, cards.shadow_mark), command (the user's and shipped only, valid:
    what the hosts register as /<command>), params ({name: {type, …}})}."""
    out = []
    for name, where, desc in flow_names(repo):
        d = dict(flow_places(repo))[where]
        path = d / f"{name}.json"
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            doc = {}
        doc = doc if isinstance(doc, dict) else {}
        cmd = doc.get("command") if where != "repo" else None
        if cmd is not None and (command_errors(doc) or validate(doc)):
            cmd = None  # a flow wb run start would refuse has no command
        out.append({"name": name, "where": where, "description": desc,
                    "mark": cards.shadow_mark(path, SHIPPED / f"{name}.json", cards.recorded_from(path))
                    if where == "yours" else "",
                    "command": cmd, "params": {k: param_decl(v) for k, v in (doc.get("params") or {}).items()}
                    if isinstance(doc.get("params"), dict) else {}})
    return out


def cmd_flows(a) -> None:
    repo = Path(a.repo).resolve() if a.repo else Path(os.getcwd())
    rows = flow_rows(repo)
    if getattr(a, "json", False):
        print(json.dumps(rows))
        return
    if not rows:
        print("no flows")
    for r in rows:
        marks = [r["mark"]] if r["mark"] else []
        if r["where"] == "repo":
            marks.append("read it first, then --plan")
        print(f"{r['name']} ({r['where']}{', ' + ', '.join(marks) if marks else ''})"
              + (f" /{r['command']}" if r["command"] else "") + f": {r['description']}")


def cmd_flow_copy(a) -> None:
    """wb flow copy|reset <name> (#1295): a shipped flow to customize as yours, or yours set aside."""
    if not RUN_RE.match(a.name or ""):
        _die(f"{a.name!r}: a flow's name is lowercase letters, digits and hyphens, at most 24")
    try:
        if a.flow_cmd == "copy":
            print(cards.copy_shipped(SHIPPED / f"{a.name}.json", flows_dir() / f"{a.name}.json", a.force))
        else:
            print(cards.reset_shipped(SHIPPED / f"{a.name}.json", flows_dir() / f"{a.name}.json"))
    except (RuntimeError, OSError, ValueError) as e:
        _die(str(e))


def cmd_ack(a) -> None:
    run = need(a.name)
    if getattr(a, "if_ts", None) is not None:  # a host's rollback, only over its own mark
        if not run.release_ack(a.ts, a.if_ts):
            _die(f"run {a.name}: acknowledged since", 3)
        print(f"released run {a.name}")
        return
    if not run.claim_ack(a.ts):
        _die(f"run {a.name}: already acknowledged", 3)
    print(f"acknowledged run {a.name}")


def archive(days: float) -> int:
    """wb gc (#1278): runs closed over ``days`` ago moved to runs/.archive/, their names kept
    taken. How many."""
    n = 0
    for name in all_runs():
        run = Run(name)
        st = run.status
        if st.get("phase") == "closed" and time.time() - (st.get("updated_at") or 0) > days * 86400:
            dest = runs_dir() / ARCHIVE / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            if not dest.exists():
                os.replace(run.dir, dest)
                n += 1
    return n


COMMANDS = {"start": cmd_start, "status": cmd_status, "report": cmd_report, "resume": cmd_resume,
            "stop": cmd_stop, "amend": cmd_amend, "feed": cmd_feed, "close": cmd_close, "flows": cmd_flows}


def wait_line(row: dict) -> str:
    return f"[{row['kind']}] run {row['run']}\n{row.get('text', '')}"


def due(name: str | None, scope: set | None = None) -> dict | None:
    """The newest run notice not yet taken (for run ``name``, or any in ``scope``), claimed; one
    no longer true (its `phase`, NOTICE_PHASE, isn't the run's now: a pause since resumed) is
    skipped, as the hosts skip it."""
    rows = [r for r in store.notices(runs=True) if "task" not in r and (name is None or r["run"] == name)
            and (scope is None or r["run"] in scope)]
    for r in reversed(rows):
        run = Run(r["run"])
        phase = run.status.get("phase")
        if run.exists and r["ts"] > run.acked and phase != "closed" and r.get("phase") in (None, phase) \
                and run.claim_ack(r["ts"]):
            return r
    return None


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "drive":
        drive(sys.argv[2])
    else:
        _die("usage: python3 -m workbench.runs drive <run>")
