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

from . import cards, gitops, meter, models, routes, spend, store

PKG_ROOT = Path(__file__).resolve().parent.parent
SHIPPED = PKG_ROOT / "agent" / "flows"
# <run>-<stage>-<n> fits a task name (store.NAME_RE, 48) and the run a group (cli.GROUP_RE, 32)
RUN_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,23}\Z")
STAGE_RE = re.compile(r"^[a-z][a-z0-9]{0,7}\Z")
FLOW_KEYS = ("v", "name", "description", "params", "budget_usd", "budget_min", "context", "stages")
STAGE_KEYS = ("id", "card", "kind", "role", "items", "from", "split", "keep", "flow", "batch",
              "attempts", "schema", "tools", "skills", "ask", "setup", "test", "refix", "on_failure",
              "max_items", "claude", "final", "prompt", "compare", "escalate")
PARAM_TYPES = ("string", "int", "list", "json", "file", "runs")
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
ARCHIVE = ".archive"   # runs/.archive/<run>: closed runs wb gc moved there (names stay taken)


# --- the keywords (#1278): Workbench's own words, never Claude's `ultracode` --------------------
# The hosts (the Claude Code mod, the OpenCode plugin) detect them in what the user typed; this is
# the reference the kernel's Keywords (#1325, #1356) and tests/fixtures/keywords.json hold to.
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
    if not isinstance(params, dict) or any(t not in PARAM_TYPES for t in params.values()):
        errs.append(f"params: each a type of {', '.join(PARAM_TYPES)}")
        params = {}
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


def parse_params(flow: dict, given: list[str]) -> dict:
    """--param k=v, each by its declared type; RuntimeError with why. A list is JSON or comma
    separated; a file is read (16 KB); runs is an earlier run's final results."""
    decl = flow.get("params") or {}
    out = {}
    for kv in given or ():
        k, sep, v = kv.partition("=")
        if not sep or k not in decl:
            raise RuntimeError(f"--param {kv!r}: the flow takes {', '.join(decl) or 'no params'} (k=v)")
        t = decl[k]
        try:
            if t == "int":
                out[k] = int(v)
            elif t == "list":
                out[k] = json.loads(v) if v.strip().startswith("[") else [x.strip() for x in v.split(",") if x.strip()]
                if not isinstance(out[k], list):
                    raise ValueError("not a list")
            elif t == "json":
                out[k] = json.loads(v)
            elif t == "file":
                out[k] = Path(v).expanduser().read_text(encoding="utf-8")[:CUTS["item"]]
            elif t == "runs":
                out[k] = [f["result"] for f in report_of(v)["final"]]
            else:
                out[k] = v
        except (ValueError, OSError, RuntimeError, UnicodeDecodeError) as e:
            raise RuntimeError(f"--param {k}: a {t} ({e})") from None
    missing = [k for k in decl if k not in out]
    if missing:
        raise RuntimeError(f"the flow needs --param {', '.join(f'{k}=<{decl[k]}>' for k in missing)}")
    return out


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

    def notify(self, kind: str, text: str, **extra) -> dict:
        """A run notice in the global notify.jsonl, `{ts, run, kind, text}`: the one wake Claude
        gets for a run's checkpoint, pause, stop, failure or end, its text the wake whole (it opens
        `[workbench] Run `<run>``, spend.RUN_WAKE's), so a host relays it as it is. Journaled too."""
        text = f"{spend.RUN_WAKE_OPENING} `{self.name}` {HEADLINE[kind]}: {text}"
        row = store.append_jsonl(store.home() / "notify.jsonl",
                                 {"ts": store.now(), "run": self.name, "kind": kind, "text": text, **extra},
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
    skipped = set(spec.get("skip") or ())
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
                by_item: dict = {}
                for it in src_items:
                    by_item.setdefault(it.key.rsplit("#a", 1)[0], []).append(it)
                for base, its in by_item.items():
                    if all(i.state not in ("pending", "running", "waiting") for i in its):
                        groups[base] = [(i.key, i.output) for i in its if i.state == "done"]
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
        items = []
        idx = -1
        while (idx := idx + 1) < len(inputs):
            key, v, src = inputs[idx]
            it = Item(sid, key, v, src=src, members=groups.get(key))
            row = started.get((sid, key))
            if row:
                it.n, it.task = row.get("n"), row.get("task")
            if (sid, key) in final:
                it.state, it.why = final[(sid, key)]
                if it.n is not None and it.state in ("done", "filtered", "superseded"):
                    it.output = (store.read_json(run.output_file(sid, it.n), {}) or {}).get("result")
            elif sid in skipped:
                it.state, it.why = "not_run", "skipped (wb run amend --skip)"
            elif s.get("compare") and len(it.members or ()) < 2:
                it.state, it.why = "not_run", (f"{len(it.members or ())} of its attempts reached review: "
                                               "nothing to compare (wb run report names the rest)")
            elif idx >= cap and "#e" not in key:
                it.state, it.why = "not_run", f"past the stage's max_items ({cap})"
            elif v is WAITING:
                it.state, it.input = "waiting", None
            elif it.task:
                it.state, it.why, it.output = _live(s, it)
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


def _live(stage: dict, it: Item) -> tuple[str, str, object]:
    """A started item's state from its task, before the driver has taken its end."""
    task = store.Task(it.task)
    if not task.exists:
        return "pending", "", None  # its wb start not through yet, or never: the driver decides
    st = task.status
    phase = st.get("phase")
    if is_code(stage):
        if phase in ("review", "accepted"):
            return "done", "", {"task": it.task, "round": st.get("round"), "item": it.input}
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


def _diffs_of(run: Run, stage: dict, it: Item) -> str:
    """{diffs}: each attempt's change at the item, labelled a1..aN with its task and tests, the
    green ones alone when two or more are (said), each cut to its share of CUTS["diff"]. best-of
    ran N builders and a reviewer per diff, and Claude read every review and most diffs: N times
    the handoff (#1134's study, SWE-agent's Chooser)."""
    rows, said = _attempts(it)
    share = CUTS["diff"] // max(1, len(rows))
    parts = []
    for lab, t, ok in rows:
        try:
            d = gitops.diff(t.worktree, t.spec["base"]) if t.worktree.is_dir() else ""
        except (gitops.GitError, KeyError) as e:
            d = f"(no diff: {e})"
        d = d if len(d) <= share else d[:share] + f"\n[… {len(d) - share:,} characters cut]"
        state = "tests passed" if ok else "tests failed" if ok is False else "no tests"
        parts.append(f"### {lab} (task {t.name}; {state})\n```diff\n{d or '(no changes)'}\n```")
    return said + "\n\n".join(parts)


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
                diff = gitops.diff(t.worktree, t.spec["base"]) if t.exists and t.worktree.is_dir() else ""
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


def _start(run: Run, stage: dict, it: Item, n: int) -> tuple[str, str]:
    """Start item ``it`` as task <run>-<stage>-<n>: ("running"|"not_run"|"null"|"paused", why)."""
    spec = run.spec
    name = run.task_name(stage["id"], n)
    specs = run.dir / "specs"
    specs.mkdir(parents=True, exist_ok=True)
    path = specs / f"{stage['id']}-{n}.md"
    path.write_text(_spec_of(run, stage, it), encoding="utf-8")
    argv = ["start", name, "--spec", str(path), "--repo", spec["repo"], "--base", spec["base"],
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
        run.log(kind="retry", stage=stage["id"], key=it.key)
        return "paused", why
    if r.returncode == 3:
        return "not_run", why
    return "null", why


def _take(run: Run, stage: dict, it: Item) -> None:
    """A report item's end, once its task waits on Claude: its object kept (filtered by `keep`,
    or a refix sent), the task accepted; a failed one discarded, its result null."""
    task = store.Task(it.task)
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
            _checkpoint(run, f"{it.task} ({stage['id']} {it.key}) ended with no result: {why}")


def _src_items(run: Run, stage: dict) -> list:
    return state(run).get(stage.get("from")) or []


def _checkpoint(run: Run, why: str) -> None:
    if run.set_status_if(lambda st: st.get("phase") == "running", phase="checkpoint", why=why):
        run.notify("run_checkpoint", f"{why}. Then: wb run resume {run.name} (or wb run stop {run.name}).")


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
    third review of #1278, 2026-11-25)."""
    from . import cli
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
    if run.status.get("phase") != "running":
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


def tick(run: Run) -> bool:
    """One pass: take the items that ended, start what is ready and has room, raise a checkpoint or
    the end. False once the driver is done."""
    st0 = run.status
    if st0.get("phase") not in OPEN:
        return False
    _ack_items(run)
    flow = run.flow
    from . import cli
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
                run.log(kind="notice", notice="run_checkpoint", item=it.task)
                _checkpoint(run, f"{it.task} ({s['id']} {it.key}) is {store.Task(it.task).status.get('phase')}: "
                                 f"{it.why}; Claude decides it (wb feedback, wb resume or wb discard)")
    if run.status.get("phase") == "running":
        for s in stages(flow):
            if s.get("final") or s.get("claude"):
                continue
            for it in st.get(s["id"]) or ():
                if it.state != "pending" or run.status.get("phase") != "running":
                    continue
                m = stage_model(s, it)
                if it.task and store.Task(it.task).exists:  # started, its runner not up yet
                    continue
                if _room(run, s, st, m):
                    break
                if why := _budget_stop(run, s, m):
                    if run.set_status_if(lambda x: x.get("phase") == "running", phase="paused", why=why):
                        run.notify("run_paused", f"paused at {why}. Done work is kept: wb run report "
                                                 f"{run.name}; wb run amend {run.name} --budget-usd N, then "
                                                 f"wb run resume {run.name}")
                    break
                n = 1 + max([r.get("n") or 0 for r in run.journal() if r.get("kind") == "item"
                             and r.get("stage") == s["id"]] or [0])
                if (k := escalation(it.key)) and not any(r.get("kind") == "escalated" and r.get("stage") == s["id"]
                                                         and r.get("key") == it.key for r in run.journal()):
                    prev = escalation_key(it.key, k - 1) if k > 1 else re.sub(r"#e\d+\Z", "", it.key)
                    run.log(kind="escalated", stage=s["id"], key=it.key, after=prev, role=item_role(s, it))
                got, why = _start(run, s, it, n)
                if got in ("not_run", "null"):
                    run.log(kind=got, stage=s["id"], key=it.key, why=why)
                    it.state = got
                elif got == "paused":
                    if run.spec.get("overnight"):
                        break  # the day cap's: tried again each tick, past midnight
                    if run.set_status_if(lambda x: x.get("phase") == "running", phase="paused", why=why):
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
                run.log(kind="notice", notice="run_checkpoint", stage=s["id"])
                n = len([i for i in st.get(s["from"]) or () if i.state == "done"])
                if run.set_status_if(lambda x: x.get("phase") in ("running", "paused"), phase="checkpoint",
                                     why=f"stage {s['id']} is Claude's"):
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
    """{final: [{key, result, trail}], filtered: [{key, why, result}], null: [{key, why}], not_run:
    [{key, why}], code: [{key, task, phase}], spend}: what the run's items returned, every item
    accounted for (no silent caps). Agents' strings are data."""
    run = Run(name)
    if not run.exists:
        raise RuntimeError(f"no run {name!r} (wb run status lists them)")
    flow = run.flow
    st = state(run)
    by = {(i.stage, i.key): i for items in st.values() if isinstance(items, list) for i in items}
    sts = [s for s in stages(flow)]
    last = sts[-1]
    feeds = last.get("from") or next((s["id"] for s in reversed(sts[:-1])), None)

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
           "final": [], "filtered": [], "superseded": [], "null": [], "not_run": [], "code": [], "pending": []}
    for s in sts:
        items = st.get(s["id"])
        if items is None and not s.get("final"):
            rep["pending"].append({"stage": s["id"], "why": "waits for its inputs"})
            continue
        for it in items or ():
            k = f"{s['id']}:{it.key}"
            if is_code(s) and it.task:
                rep["code"].append({"key": k, "task": it.task, "phase": store.Task(it.task).status.get("phase")})
            if it.state == "done" and s["id"] == feeds and not is_code(s):
                rep["final"].append({"key": k, "result": it.output, "trail": trail(it)})
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
    for what in ("final", "filtered", "superseded", "null", "not_run", "pending"):
        rows = [r for r in rep[what] if stage is None or str(r.get("key", "")).startswith(f"{stage}:")
                or r.get("stage") == stage]
        if not rows:
            continue
        out.append(f"{what} ({len(rows)}):")
        for r in rows:
            if what == "final":
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
            if it.state == "done" and not any(r["key"] == f"{stage}:{it.key}" for r in rep["final"]):
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


def estimate(flow: dict, params: dict) -> tuple[float | None, float, list[str]]:
    """(dollars for the stages whose items are known now, GPU minutes for them, the stages whose
    count is known only as they run), from each card's `est` × meter.price."""
    usd, mins, later = 0.0, 0.0, []
    priced = True
    for s in stages(flow):
        if s.get("final") or s.get("claude"):
            continue
        if "items" not in s:
            later.append(s["id"])
            continue
        n = min(len(_items_of(s, Run("-"), params)), int(s.get("max_items") or MAX_ITEMS)) * int(s.get("attempts") or 1)
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

def snapshot() -> list[dict]:
    """Each run not closed, from its files alone (no state() rebuild, no reap: wb top ticks every
    2 s): `{run, flow, phase, why, alive, stages, spent_usd, budget_usd, budget_min, gpu_min,
    session, cwd}`, by its start."""
    out = []
    for name in all_runs():
        run = Run(name)
        st, sp = run.status, run.spec
        if st.get("phase") == "closed":
            continue
        out.append({"run": name, "flow": sp.get("flow_name") or run.flow.get("name"), "phase": st.get("phase"),
                    "why": st.get("why") or "", "alive": st.get("phase") in OPEN and driver_alive(run),
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
        params = parse_params(flow, a.param)
    except RuntimeError as e:
        _die(str(e))
    repo = Path(a.repo or os.getcwd()).resolve()
    if not (repo / ".git").exists():
        _die(f"{repo} is no git repository (--repo)")
    base = gitops.base_commit(repo, a.base or "HEAD")
    if not base:
        _die(f"no commit {a.base or 'HEAD'} in {repo}")
    said: list[str] = []
    for s in stages(flow):  # an API role with no cap is refused before any file is written
        if s.get("final") or s.get("claude"):
            continue
        for k in range(len(s.get("escalate") or []) + 1):  # its escalate roles' too
            probe = Item(s["id"], "1" + (f"#e{k}" if k else ""), None)
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
    est_usd, est_min, later = estimate(flow, params)
    d = run_dir(a.name)
    d.mkdir(parents=True)
    for sub in ("inbox", "outputs", "specs"):
        (d / sub).mkdir()
    spec = {"name": a.name, "flow": flow, "flow_name": flow["name"], "flow_sha": flow_sha(flow),
            "params": params, "context": ctx[:CUTS["context"]], "repo": str(repo), "base": base,
            "base_ref": a.base, "cwd": os.getcwd(), "budget_usd": usd_, "budget_min": mins,
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
    roles = ", ".join(dict.fromkeys(f"{s['id']} {stage_role(s)}" + (" (API)" if stage_model(s) else " (GPU)")
                                    + (f", then {', '.join(s['escalate'])}" if s.get("escalate") else "")
                                    for s in stages(flow) if not (s.get("final") or s.get("claude"))))
    est = (meter.usd(est_usd) if est_usd is not None else "unpriced") + (f", GPU ~{est_min:g} min" if est_min else "")
    print(f"run {a.name}: flow {flow['name']} · {roles} · estimate {est}"
          + (f" (and {', '.join(later)} as their items come)" if later else "")
          + (f" · budget {meter.usd(usd_)}" if usd_ is not None else "") + f" · driver pid {pid}")
    for line in said:
        print(line)
    # the mod wakes Claude for no run row yet: wb wait in the background is the wake (#1278's review)
    print(f"stop: wb run stop {a.name}. Wait for its notices (a checkpoint, a pause, the end) with "
          f"wb wait {a.name} in the background, again after each: don't poll.")


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


def cmd_resume(a) -> None:
    run = need(a.name)
    reap(run)
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
    stopped = stop(run, "stopped by Claude (wb run stop)")
    if stopped is None:
        _die(f"run {run.name} is {run.status.get('phase')}, not running")
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
    open_code = [n for n in run_tasks(run) if store.Task(n).exists
                 and store.Task(n).status.get("phase") not in store.CLOSED
                 and is_code(stage_by(run.flow, store.Task(n).spec.get("stage") or "") or {})]
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


def cmd_flows(a) -> None:
    repo = Path(a.repo).resolve() if a.repo else Path(os.getcwd())
    rows = flow_names(repo)
    if not rows:
        print("no flows")
    for name, where, desc in rows:
        print(f"{name} ({where}{': read it first, then --plan' if where == 'repo' else ''}): {desc}")


def cmd_ack(a) -> None:
    run = need(a.name)
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
    """The newest run notice not yet taken (for run ``name``, or any in ``scope``), claimed."""
    rows = [r for r in store.notices(runs=True) if "task" not in r and (name is None or r["run"] == name)
            and (scope is None or r["run"] in scope)]
    for r in reversed(rows):
        run = Run(r["run"])
        if run.exists and r["ts"] > run.acked and run.status.get("phase") != "closed" and run.claim_ack(r["ts"]):
            return r
    return None


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "drive":
        drive(sys.argv[2])
    else:
        _die("usage: python3 -m workbench.runs drive <run>")
