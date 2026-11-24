"""The conversation between Claude and a task's agent, as a readable live transcript (`wb watch`).

Two sources, merged by time:
- the OpenCode session (`opencode session export`): what Claude sent (the spec, review feedback,
  the automatic test-fix prompts), and everything the agent did: its reasoning, every tool call
  with input and output, its messages.
- the task's own files: test results, questions to Claude and the answers, stops, and the
  task's phase (waiting for review, accepted...).
"""
from __future__ import annotations

import collections
import difflib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass

from . import models, store
from .watch import Watcher
from .runner import INTERRUPT_HEAD, NOTE_HEAD, RERUN, api_task, opencode_bin

STYLE = {"claude": "1;38;5;208", "agent": "1;36", "think": "2;3", "tool": "33", "out": "2",
         "ok": "1;32", "bad": "1;31", "sys": "34", "add": "32", "del": "31", "head": "1"}


@dataclass
class Entry:
    ts: float
    key: str
    lines: list[tuple[str, str]]  # (style, text)


# Control characters other than newline and tab, C1 included (U+009B is CSI). The agent's shell
# output and messages reach the user's terminal through `wb watch`: an OSC 52 in a file it cats
# can set the clipboard, others retitle the window or redraw lines (the 2026-11-16 audit).
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def plain(text: str) -> str:
    """Text from the session or the task's files, safe to print: controls shown as \\xNN."""
    return _CTRL.sub(lambda m: f"\\x{ord(m.group()):02x}", text)


class Painter:
    def __init__(self, color: bool):
        self.color = color

    def __call__(self, style: str, text: str) -> str:
        if not self.color or not style:
            return text
        return f"\033[{STYLE[style]}m{text}\033[0m"


# --standalone, like `opencode run`: a private server on the shared session storage. Through the
# background service an export needs its fixed port (49374), so another user's service there, or
# one started with other XDG dirs, made every export fail and left the runner's context checks,
# its speed learning and `wb watch` silently empty (the 2026-11-16 audit).
_said: set[str] = set()


def _complain(what: str, r: subprocess.CompletedProcess) -> None:
    """A failing session command, said once per process on stderr (runner.log under the runner)."""
    why = next(iter((r.stderr or "").strip().splitlines()), f"exit {r.returncode}")
    if why not in _said:
        _said.add(why)
        print(f"opencode session {what} failed: {why[:200]}", file=sys.stderr, flush=True)


def _same(cmd: list[str]) -> list[str]:
    return cmd


def stores(task: store.Task | None) -> list[tuple[dict | None, object]]:
    """The OpenCode stores a task's sessions may be in, for wb's session commands, as (the
    environment, None for the user's own; what wraps the command): the task's home (#1010) when
    it has a database, then the user's, which holds the sessions of tasks started before
    v1.91.2. Session ids are unique (`ses_` and random), so looking in both is safe. The task's
    home is read in the task's own lane (lane.wrap): the agent writes it, and a link it planted
    there (state/, log/, the database's -wal) had OpenCode, run by wb outside the lane, write
    through it to any file of the user's (the #1010 review, 2026-11-22). The user's store is
    out of every lane, and read outside it."""
    from . import context, lane
    if task is None or not context.opencode_db(task).is_file():
        return [(None, _same)]
    env = {**os.environ, **context.opencode_home_env(task)}
    writable = context.writable(task)
    if writable is None:  # no lane: the agent's commands reach every file of the user's anyway
        return [(env, _same), (None, _same)]
    readable, unix = context.readable(task), context.unix_paths(task)
    return [(env, lambda cmd: lane.wrap(writable, cmd, readable, unix)), (None, _same)]


def export(session: str, strict: bool = False, task: store.Task | None = None) -> dict | None:
    """The session as OpenCode exports it, from the task's own home first (stores); {} when
    that fails, or None with ``strict``: the runner tells a failure from an empty session
    (#262)."""
    failed = None  # the first failure that isn't "not found here", else the last
    for env, wrap in stores(task):
        try:
            r = subprocess.run(wrap(opencode_bin() + ["session", "export", session, "--standalone"]),
                               capture_output=True, text=True, timeout=30, cwd="/", env=env)
            if r.returncode == 0 and r.stdout.strip():
                return store.loads(r.stdout)  # its strings written out again (#438)
            if failed is None or "not found" in (failed.stderr or "").lower():
                failed = r
        except (OSError, subprocess.TimeoutExpired, ValueError):  # a busy or broken store
            pass
    if failed is not None:
        _complain("export", failed)
    return None if strict else {}


def finished_export(task: store.Task, sid: str) -> dict:
    """An earlier session of the task's (status `sessions`, before a handover), exported once and
    kept beside the task (`.export-<sid>.json`, a dotfile `wb events` ignores): it never changes,
    and the pane's `wb watch --json`, run on every status write of the followed task, exported
    every one of them again each time, a core kept busy beside the model (#691)."""
    if not re.fullmatch(r"[\w-]{1,128}", sid or ""):
        return export(sid, task=task) if sid else {}
    path = task.dir / f".export-{sid}.json"
    kept = store.read_json(path, None)
    if isinstance(kept, dict) and kept:
        return kept
    got = export(sid, task=task)
    if got:
        try:
            store.write_json(path, got)
        except OSError:  # a full disk: exported again next time
            pass
    return got


def find_session(task: store.Task) -> str | None:
    """The task's OpenCode session: from its status, or by its title before the first event, in
    the task's own home, else the user's store (stores)."""
    sid = task.status.get("session")
    if sid or not task.worktree.exists():
        return sid
    for env, wrap in stores(task):
        try:
            r = subprocess.run(wrap(opencode_bin() + ["session", "list", "--standalone"]),
                               capture_output=True, text=True, cwd=task.workdir,
                               env={**(os.environ if env is None else env), "PWD": str(task.workdir)},
                               timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if r.returncode:
            _complain("list", r)
        for line in r.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) > 1 and parts[1] == f"wb: {task.name}":
                return parts[0]
    return None


def _k(n: float) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(int(n))


def _secs(ms: float) -> str:
    s = ms / 1000
    return f"{s:.1f}s" if s < 60 else f"{int(s // 60)}m{int(s % 60):02d}s"


def step_line(m: dict) -> str | None:
    """How one model call went: prompt reading, writing speed, tokens. From the session's timings."""
    t, tok = m.get("time") or {}, m.get("tokens") or {}
    created, first, end = t.get("created"), t.get("firstToken"), t.get("streamed") or t.get("completed")
    if not (created and first and end):
        return None
    read_in = tok.get("input", 0) + (tok.get("cache") or {}).get("read", 0)
    out = tok.get("output", 0) + tok.get("reasoning", 0)
    gen_ms = max(1, end - first)
    if not (read_in or out):
        return None  # a call that failed before any token
    cached = (tok.get("cache") or {}).get("read", 0)
    reuse = f" ({_k(cached)} cached)" if cached else ""
    rate = f" · {out / (gen_ms / 1000):.0f} tok/s" if out and gen_ms > 300 else ""
    return (f"  ⏱ {_k(read_in)} tokens in{reuse}, read in {_secs(first - created)} · "
            f"{_k(out)} out in {_secs(gen_ms)}{rate}")


def _clip(text: str, n: int, tail: bool = False) -> list[str]:
    lines = text.rstrip("\n").splitlines() or [""]
    if len(lines) <= n:
        return lines
    cut = len(lines) - n
    return ([f"… {cut} more line(s)"] + lines[-n:]) if tail else (lines[:n] + [f"… {cut} more line(s)"])


def _block(style: str, text: str, n: int, indent: str = "    ", tail: bool = False):
    return [(style, indent + line) for line in _clip(text, n, tail)]


SKILLS_HEADS = ("Claude picked these skills", "Claude handed you more skills", "Claude took back:")


def _user_message(text: str, verbose: bool) -> tuple[str, list[tuple[str, str]]]:
    full = text
    if text.startswith(SKILLS_HEADS) and "\n\n" in text:  # runner.skills_note goes first
        text = text.split("\n\n", 1)[1]
    if text.startswith("Review feedback from the senior reviewer"):
        head = "Claude → agent · review feedback"
    elif text.startswith("Before Claude reviews this"):  # runner.SELF_REVIEW
        head = "workbench → agent · its own review first, before Claude's"
    elif text.startswith("The acceptance tests fail"):  # runner.auto_fix_prompt
        head = "workbench → agent · tests failed, sent back automatically"
    elif text.startswith("You are picking this task up in a fresh session"):  # runner.handoff_prompt
        head = "workbench → agent · fresh session: the task, the work so far, what's next"
    elif text.startswith("Step ") and " of this task is done and committed" in text[:60]:
        head = "Claude → agent · next step"
    elif text.startswith(INTERRUPT_HEAD) and "without a tool call. Stop planning" in text:
        head = "workbench → agent · nudge: thinking too long without acting"  # runner.NUDGE
    elif text.startswith(INTERRUPT_HEAD):
        head = "Claude → agent · INTERRUPT, high priority"
    elif text.startswith(NOTE_HEAD):
        head = "Claude → agent · note"
    elif text.startswith(RERUN[:40]):  # runner._run_again (#1235)
        head = "workbench → agent · the round again, after its provider or server stopped it"
    else:
        head = "Claude → agent · spec"
    body = text = full
    if not verbose and "\n---\nHow to work" in text:
        body = text.split("\n---\nHow to work")[0] + "\n(+ the workbench protocol; -v shows it)"
    return head, [("claude", head)] + _block("", body, 10_000 if verbose else 40, "  │ ")


RESULT = "  ⎿  "  # under a call, as Claude Code draws a tool's result
UNDER = "     "


def _result(style: str, text: str, n: int, tail: bool = False):
    lines = _clip(text, n, tail)
    return [(style, (RESULT if i == 0 else UNDER) + line) for i, line in enumerate(lines)]


def _tool_lines(name: str, state: dict, rel, verbose: bool, done: bool):
    inp = state.get("input") or {}
    out = "\n".join(c.get("text", "") for c in state.get("content") or [] if c.get("type") == "text")
    status = state.get("status")
    n_out = 400 if verbose else 8
    lines = []
    if name == "shell":
        lines.append(("tool", f"⏺ Bash({inp.get('command', '')})"))
        if done and out.strip():
            lines += _result("out", out, n_out, tail=True)
    elif name == "edit":
        lines.append(("tool", f"⏺ Update({rel(inp.get('path', ''))})"))
        if done or verbose:
            diff = difflib.unified_diff(str(inp.get("oldString", "")).splitlines(),
                                        str(inp.get("newString", "")).splitlines(), lineterm="", n=1)
            body = [d for d in diff if not d.startswith(("---", "+++", "@@"))]
            added = sum(d.startswith("+") for d in body)
            removed = sum(d.startswith("-") for d in body)
            lines.append(("out", f"{RESULT}{added} line(s) added, {removed} removed"))
            for d in body[: (400 if verbose else 14)]:
                lines.append(("add" if d.startswith("+") else "del" if d.startswith("-") else "out",
                              UNDER + d))
            if len(body) > 14 and not verbose:
                lines.append(("out", f"{UNDER}… {len(body) - 14} more diff line(s)"))
    elif name == "write":
        content = str(inp.get("content", ""))
        lines.append(("tool", f"⏺ Write({rel(inp.get('path', ''))})"))
        lines.append(("out", f"{RESULT}{len(content.splitlines())} line(s)"))
        lines += [("add", UNDER + x) for x in _clip(content, 400 if verbose else 8)]
    elif name == "read":
        where = f", from line {inp['offset']}" if inp.get("offset") else ""
        lines.append(("tool", f"⏺ Read({rel(inp.get('path', ''))}{where})"))
    elif name in ("grep", "glob"):
        what = f'"{inp.get("pattern", "")}"' + (f", in {rel(inp['path'])}" if inp.get("path") else "")
        lines.append(("tool", f"⏺ {'Search' if name == 'grep' else 'Find'}({what})"))
        if done:
            found = [x for x in out.splitlines() if x.strip()]
            lines.append(("out", f"{RESULT}{len(found)} found" + (": " + rel(found[0])[:100] if found else "")))
    elif name == "skill":
        lines.append(("tool", f"⏺ Skill({inp.get('id') or inp.get('name') or ''})"))
    else:
        brief = json.dumps(inp, ensure_ascii=False)
        lines.append(("tool", f"⏺ {name}({brief[:160]})"))
        if done and out.strip():
            lines += _result("out", out, n_out, tail=True)
    if status == "error":
        err = state.get("error") or out or "error"
        lines += _result("bad", str(err if isinstance(err, str) else json.dumps(err)), 6)
    return lines


def session_entries(data: dict, worktree: str, verbose: bool) -> list[Entry]:
    def rel(path: str) -> str:  # paths in the worktree, as the agent's own tree sees them
        if worktree and path == worktree:
            return "."
        return path[len(worktree) + 1:] if worktree and path.startswith(worktree + "/") else path

    out: list[Entry] = []
    for m in data.get("messages", []):
        mid, mts = m.get("id", ""), (m.get("time") or {}).get("created", 0) / 1000
        if m.get("type") == "user":
            _, lines = _user_message(m.get("text", ""), verbose)
            out.append(Entry(mts, mid, lines))
            continue
        if m.get("type") == "compaction" and m.get("status") == "completed":
            out.append(Entry(mts, mid, [("sys", "⟲ OpenCode compacted the agent's context "
                                                "(its own summary replaced the earlier messages)")]
                             + (_block("think", m.get("summary", ""), 400, "    ") if verbose else [])))
            continue
        if m.get("type") != "assistant":
            continue
        finished = m.get("finish") is not None
        parts = m.get("content") or []
        for i, part in enumerate(parts):
            kind, key = part.get("type"), f"{mid}:{i}"
            pts = mts + i * 1e-3  # parts in their order: their own timestamps are uneven
            if kind == "reasoning" and ((part.get("time") or {}).get("completed") or finished):
                text = part.get("text", "").strip()
                if text:
                    pt = part.get("time") or {}
                    took = (f"thought for {_secs(pt['completed'] - pt['created'])}"
                            if pt.get("created") and pt.get("completed") else "thought")
                    shown = _clip("\n".join(x for x in text.splitlines() if x.strip()),
                                  400 if verbose else 6)
                    out.append(Entry(pts, key, [("think", f"✻ {took[:1].upper()}{took[1:]}")]
                                     + [("think", "  " + line) for line in shown]))
            elif kind == "text" and finished:
                text = part.get("text", "").strip()
                if text:
                    body = text.splitlines()
                    out.append(Entry(pts, key, [("agent", f"⏺ {body[0]}")]
                                     + [("agent", f"  {line}") for line in body[1:]]))
            elif kind == "tool":
                name = part.get("name", "")
                if name.endswith("ask_reviewer"):
                    continue  # the question and answer come from the task's own events
                state = part.get("state") or {}
                done = state.get("status") in ("completed", "error")
                if not done:
                    out.append(Entry(pts, f"{key}:start", _tool_lines(name, state, rel, verbose, False)))
                else:
                    # a call seen running was printed already: now only its result
                    out.append(Entry(pts, f"{key}:end", _tool_lines(name, state, rel, verbose, True)))
        if finished:
            end = mts + len(parts) * 1e-3
            if m.get("finish") == "error":
                err = m.get("error") or {}
                msg = err.get("message") if isinstance(err, dict) else err
                out.append(Entry(end, f"{mid}:error",
                                 [("bad", f"■ the model call failed{': ' + str(msg)[:300] if msg else ''}")]))
            line = step_line(m)
            if line:
                out.append(Entry(end + 1e-4, f"{mid}:step", [("out", line)]))
    return out


# The event kinds task_entries gives a line, each but the tool rows (from the session's export)
# from its own row. wb log prints these too, one line each, from this one set: it once printed a
# fixed nine, so the runner's own (a model partly on the CPU, a handover, a steer) never showed
# there, and a test checks task_entries has a branch for each (#861).
EVENT_KINDS = frozenset({
    "round", "tests", "autofix", "self_review", "ask", "answer", "captain", "steer", "skills",
    "allow", "interrupted", "fresh", "full", "load", "left_bg", "lingered", "settled", "reading_full",
    "compacted", "limits", "export", "resumed", "coding", "spec_gap", "next_step", "takeover",
    "stopped", "error", "retry", "gpu", "served", "rate_limited", "backend"})


def task_entries(task: store.Task) -> list[Entry]:
    out = []
    rows, _ = store.read_jsonl(task.dir / "events.jsonl")
    for i, r in enumerate(rows):
        kind, ts, key = r.get("kind"), r.get("ts", 0), f"ev:{i}"
        if kind == "round":
            out.append(Entry(ts, key, [("head", f"──────── round {r.get('round')} ────────")]))
        elif kind == "tests":
            ok = r.get("ok")
            lines = [("ok" if ok else "bad", f"■ tests {'PASS' if ok else 'FAIL'}"
                                            f" ({task.spec.get('test_cmd', '')})")]
            if not ok:
                f = task.round_dir(r.get("round", 1)) / "tests.txt"
                if f.exists():
                    lines += _result("out", f.read_text(), 12, tail=True)
            out.append(Entry(ts, key, lines))
        elif kind == "autofix":
            out.append(Entry(ts, key, [("sys", "■ workbench sends the failures back to the agent")]))
        elif kind == "self_review":
            out.append(Entry(ts, key, [("sys", "■ tests pass: the agent does its own review first, "
                                               "before Claude's")]))
        elif kind == "ask":
            out.append(Entry(ts, key, [("claude", "agent → Claude · question")]
                             + _block("", r.get("text", ""), 40, "  ")))
        elif kind == "answer":
            out.append(Entry(ts, key, [("claude", "Claude → agent · answer")]
                             + _block("", r.get("text", ""), 40, "  ")))
        elif kind == "captain":
            out.append(Entry(ts, key, [("claude", "Claude (captain):")]
                             + _block("claude", r.get("text", ""), 40, "  ")))
        elif kind == "steer":
            what = "interrupts the agent" if r.get("now") else "queues a note for the agent's next step"
            who = "the runner" if r.get("by") == "workbench" else "Claude"  # its think nudge
            out.append(Entry(ts, key, [("claude", f"■ {who} {what}")]))
        elif kind == "skills":
            out.append(Entry(ts, key, [("claude", "Claude → agent · skills")]
                             + _block("", r.get("text", ""), 12, "  ")))
        elif kind == "allow":
            out.append(Entry(ts, key, [("claude", f"■ Claude {r.get('text', '')}")]))
        elif kind == "interrupted":
            out.append(Entry(ts, key, [("bad", "■ the agent was interrupted")]))
        # reading_full: why a session grew into compaction, beside fresh and full; it, resumed,
        # spec_gap and coding were dropped as kinds with no branch (#846), and then compacted,
        # limits and export, written just before (#865)
        elif kind in ("fresh", "full", "load", "left_bg", "reading_full", "compacted", "limits", "export",
                      "lingered",  # the agent's turn over, OpenCode still open: stopped (#1160)
                      "settled",  # a self-review at a guard, the tree its tests passed on: stopped (#1168, #1250, #1261)
                      "served",  # served: the keyhole, an API model's provider changed what ran
                      "rate_limited"):  # an API provider's wait, the clocks paused (#1020)
            out.append(Entry(ts, key, [("sys", f"■ {r.get('text', '')}")]))
        elif kind == "backend":  # OpenRouter served a backend outside the role's order (#1021)
            out.append(Entry(ts, key, [("bad", f"■ {r.get('text', '')}")]))
        elif kind in ("resumed", "coding"):  # the text says who: Claude, or the user's allowance
            out.append(Entry(ts, key, [("claude", f"■ {r.get('text', '')}")]))
        elif kind == "spec_gap":
            out.append(Entry(ts, key, [("claude", f"■ Claude: the spec missed: {r.get('text', '')}")]))
        elif kind == "next_step":
            out.append(Entry(ts, key, [("ok", f"■ {r.get('text', '')}")]))
        elif kind == "takeover":
            out.append(Entry(ts, key, [("claude", "■ Claude took over: finishing it by hand")]))
        elif kind == "stopped":
            out.append(Entry(ts, key, [("bad", f"■ stopped: {r.get('text', '')}")]))
        elif kind == "error":
            out.append(Entry(ts, key, [("bad", f"■ error: {r.get('text', '')}")]))
        elif kind == "retry":  # OpenCode sends a request again (#491)
            out.append(Entry(ts, key, [("bad", f"■ OpenCode retries a request: {r.get('text', '')}")]))
        elif kind == "gpu":  # a server wb can't stop, a model partly on the CPU: why rounds crawl
            out.append(Entry(ts, key, [("bad", f"■ GPU: {r.get('text', '')}")]))
    return out


VERBS = ("Thinking", "Pondering", "Tinkering", "Mulling", "Puzzling", "Noodling", "Scheming",
         "Untangling", "Brewing", "Sketching", "Weighing", "Tracing")


def _server_live(task: store.Task) -> dict | None:
    """Live progress from the model's server, when it reports it (Strata's /metrics does)."""
    try:
        m = models.discover().get(task.spec.get("model", ""))
        data = models._get(f"{m.root}/metrics", timeout=1.0) if m else None
    except Exception:
        return None
    live = data.get("live") if isinstance(data, dict) else None
    return live if isinstance(live, dict) and live.get("state") not in (None, "idle") else None


def activity(task: store.Task, data: dict, now: float | None = None) -> str | None:
    """What is happening this moment, as one line: the spinner row under the transcript."""
    now = now or time.time()
    st = task.status
    phase = st.get("phase")
    last = st.get("last") or ""
    if phase == "queued":  # the runner says what it waits on, or what it loads
        # the base check said "Waiting for the GPU" (the 0.90 review), and so did a setup
        # running for half an hour, and a reload of a model partly on the CPU (#869)
        if last.startswith(("loading", "starting", "stopping", "unloading", "sharing", "waiting",
                            "checking", "setting", "reloading")):
            return f"{last[:1].upper()}{last[1:]}…"
        # "resumed by the reviewer" and the like, before the runner's first write: an API task
        # holds no GPU and waits for none (#1112, 2026-11-23)
        return "Starting…" if api_task(task) else "Waiting for the GPU…"
    if phase == "testing":
        return f"Running the acceptance tests… `{task.spec.get('test_cmd', '')}`"
    if phase == "asking":
        return "Waiting for Claude's answer…"
    if phase != "running":
        return None
    msgs = data.get("messages") or []
    stamps = [((m.get("time") or {}).get("completed") or (m.get("time") or {}).get("created") or 0)
              for m in msgs]
    since = now - (max(stamps) / 1000 if stamps else (task.spec.get("created_at") or now))
    verb = VERBS[len(msgs) % len(VERBS)]
    live = _server_live(task)
    if live:
        if live.get("state") == "reading" or live.get("phase") == "reading the prompt":
            n = live.get("prompt_tokens")
            return (f"Reading the prompt… {_secs(since * 1000)}"
                    + (f" · {_k(n)} tokens" if n else ""))
        if live.get("state") != "generating":  # loading, unloaded, restarting...
            return f"Model server {live.get('state')}… {_secs(since * 1000)}"
        gen = live.get("generated") or 0
        rate = live.get("tok_s")
        what = "Thinking" if live.get("phase") == "thinking" else "Writing"
        return (f"{what}… {_secs(since * 1000)} · ↓ {_k(gen)} tokens"
                + (f" · {rate:.0f} tok/s" if rate else ""))
    return f"{verb}… {_secs(since * 1000)}"


def model_line(task: store.Task, data: dict) -> str:
    """The model and how it is set to think: OpenCode's variant and the model's own settings."""
    sp = task.spec
    m = models.lookup(sp.get("model", ""), cached=True)  # an API task's too; the pane polls: no rebuild
    cfg = m.config if m else {}
    variant = ((data.get("info") or {}).get("model") or {}).get("variant") \
        or (sp.get("opencode_model", "").partition("#")[2]) or "default"
    variants = cfg.get("variants") or {}
    if isinstance(variants, list):  # the catalog's: [{id, settings | body}]
        variants = {v.get("id"): v.get("settings") or v.get("body") or {}
                    for v in variants if isinstance(v, dict)}
    vopts = (variants.get(variant) or {}) if isinstance(variants, dict) else {}
    opts = {**(cfg.get("options") or {}), **(vopts.get("options") or vopts)}
    effort = opts.get("reasoningEffort") or opts.get("reasoning_effort") or (
        f"{_k(opts['thinking']['budgetTokens'])} thinking tokens"
        if isinstance(opts.get("thinking"), dict) and opts["thinking"].get("budgetTokens") else None)
    if cfg.get("reasoning") is False:
        think = "no reasoning"
    else:
        think = f"reasoning {effort}" if effort else "reasoning on, default effort"
    ctx = (cfg.get("limit") or {}).get("context")
    if sp.get("thinking_shown"):  # an API task's, fixed at wb start; never the config's wb-off (#1018)
        think, variant = sp["thinking_shown"], ""
    return " · ".join(x for x in (sp.get("model_label") or sp.get("model"), think,
                                  variant and f"variant {variant}", f"{ctx // 1024}K context" if ctx else "") if x)


def context_fill(data: dict) -> int | None:
    """The tokens the agent's next request carries: the last step's input, cache and output."""
    last = next((m for m in reversed(data.get("messages") or [])
                 if m.get("type") == "assistant" and m.get("tokens")), None)
    if not last:
        return None
    tk, cache = last["tokens"], last["tokens"].get("cache") or {}
    return tk.get("input", 0) + cache.get("read", 0) + cache.get("write", 0) + tk.get("output", 0)


def clock_line(task: store.Task, now: float | None = None, data: dict | None = None) -> str | None:
    """Time, every way that helps at a glance: the task, this round, loading, and an ETA."""
    from . import router
    now = now or time.time()
    sp, st = task.spec, task.status
    took = now - (sp.get("created_at") or now)
    parts = [f"task {_secs(took * 1000)}"]
    if st.get("round_started_at") and st.get("phase") in ("running", "testing", "asking"):
        parts.append(f"round {st.get('round', 1)} {_secs((now - st['round_started_at']) * 1000)}")
    elif st.get("round", 1) > 1:
        parts.append(f"{st['round']} rounds")
    if st.get("load_secs", 0) >= 1:
        parts.append(f"model load {_secs(st['load_secs'] * 1000)}")
    if st.get("phase") in ("queued", "running", "testing", "asking"):
        typical = router.expected_minutes(sp.get("model", ""), sp.get("size", "significant"),
                                          sp.get("weights_id"), router.record_role(sp.get("role")))
        if typical:  # against active work, as typical is (#543)
            from .runner import active_so_far
            left = typical * 60 - active_so_far(task, now)
            parts.append(f"~{_secs(left * 1000)} left (typically {typical:.0f}m)" if left > 0
                         else f"{_secs(-left * 1000)} past its typical {typical:.0f}m")
        else:
            parts.append("no ETA yet: no finished task of this size on this model")
    used, window = context_fill(data or {}), st.get("window")
    if used:
        parts.append(f"context {_k(used)}" + (f" of {window // 1024}K ({100 * used // window}%)"
                                               if window else ""))
    if st.get("tok_out"):
        parts.append(f"{_k(st['tok_out'])} tokens written")
    return "⏲ " + " · ".join(parts)


PHASE_LINE = {
    "queued": ("sys", "■ waiting for the GPU"),   # an API task's: API_QUEUED_LINE
    "review": ("ok", "■ done: Claude reviews next"),
    "stuck": ("bad", "■ stuck: waiting for Claude"),
    "failed": ("bad", "■ failed: waiting for Claude"),
    "asking": ("claude", "■ waiting for Claude's answer"),
    "accepted": ("ok", "■ Claude accepted it"),
    "discarded": ("bad", "■ Claude discarded it"),
}

# an API task's queued line: "waiting for the GPU" under "Waiting for an API slot" (#1112, 2026-11-23)
API_QUEUED_LINE = ("sys", "■ waiting to start")


def header(task: store.Task, paint: Painter, data: dict | None = None) -> str:
    sp = task.spec
    lines = [paint("head", f"task {task.name}") + f" · {plain(model_line(task, data or {}))}",
             paint("out", plain(f"  repo {sp.get('repo')} · branch {sp.get('branch')}"
                                + (f" · tests `{sp['test_cmd']}`" if sp.get("test_cmd") else "")))]
    if routed := routed_text(task):
        lines.append(paint("out", plain(f"  routed: {routed}")))
    return "\n".join(lines)


def routed_text(task: store.Task) -> str:
    """How the task's model was chosen (#1016): an API task's `API <id> (<provider>) · trivial
    role, set by you 11-20 · thinking off` from task.json's keys, a GPU task's the router's why
    (task.json `route`); '' for neither. wb watch's `routed:` line, and --json's `routed` row."""
    from . import routes
    sp = task.spec
    if sp.get("via") == "api":
        return routes.routed_line(sp.get("model", ""), sp)
    return sp.get("route") or ""


JSON_LINE_CHARS = 2000  # the pane cuts each line to this anyway (the mod's LINE_CHARS)


LIVE_PHASES = ("queued", "running", "testing", "asking")
EXPORT_EVERY = 30   # seconds: a live task's session is exported at least this often, changed or not
REAP_EVERY = 60     # seconds between `reap` calls while the task looks live


def follow(task: store.Task, verbose: bool = False, once: bool = False, color: bool | None = None,
           out=None, poll: float = 1.5, as_json: bool = False, last: int = 0, reap=None) -> None:
    """Print the transcript, then (unless once) keep printing what's new until the task closes.
    as_json writes one {"s": style, "t": text} per line, for the Claude Code pane to style, each
    cut to JSON_LINE_CHARS, and with `last` only the newest `last` transcript lines (the meta,
    activity, clock and phase lines always): the engine keeps only the first 4 MiB of the mod's
    read, so a long task's newest lines were the ones lost (the 2026-11-16 audit).

    The session is exported again only when the task's directory or its status' `session` or
    `updated_at` changed (or EXPORT_EVERY passed while live); the clock is redrawn from the last
    export, and once the task isn't live the terminal's tick slows to 30 s: a watch left open
    over a review ran `opencode session export` every 1.5 s, half a CPU second each (#555).
    ``reap`` (cli.reap) is called every REAP_EVERY seconds while the task looks live."""
    out = out or sys.stdout
    paint = Painter(out.isatty() if color is None else color)
    held = collections.deque(maxlen=last) if as_json and last else None
    if as_json:
        def emit(style, text, keep=False):
            if len(text) > JSON_LINE_CHARS:
                text = text[:JSON_LINE_CHARS - 1] + "…"
            row = json.dumps({"s": style, "t": text}, ensure_ascii=False)
            if held is not None and not keep:
                held.append(row)
            else:
                print(row, file=out, flush=True)
    else:
        clear = "\r\033[K" if out.isatty() else ""  # over the activity row, redrawn after
        def emit(style, text):  # cleaned before the colours: those are ours
            print(clear + paint(style, plain(text)), file=out, flush=True)
    sid = first_sid = find_session(task)
    first = export(sid, task=task) if sid else {}
    if as_json:
        emit("meta", model_line(task, first), keep=True)
        if routed := routed_text(task):  # the pane shows it under the task line (#1016)
            emit("routed", f"routed: {routed}", keep=True)
    else:
        print(header(task, paint, first), file=out, flush=True)
    seen: set[str] = set()
    done: dict[str, list[Entry]] = {}  # earlier sessions are finished: export each once
    watcher = Watcher(task.dir)
    last_phase = None
    worktree = str(task.worktree)
    dirty, stamp, data, exported, reaped = True, None, {}, 0.0, time.monotonic()
    while True:
        st = task.status
        if reap and st.get("phase") in LIVE_PHASES and time.monotonic() - reaped >= REAP_EVERY:
            reaped = time.monotonic()
            reap(task)
            st = task.status
        now_stamp = (st.get("session"), st.get("updated_at"))
        live = st.get("phase") in LIVE_PHASES
        if dirty or now_stamp != stamp or live and time.monotonic() - exported >= EXPORT_EVERY:
            stamp, exported = now_stamp, time.monotonic()
            sid = find_session(task)
            # the first pass reuses the export just made: every `wb watch --json` (the pane's, every
            # few seconds) ran two OpenCode exports where one does (the 2026-11-16 audit)
            data = first if first is not None and sid == first_sid else \
                export(sid, task=task) if sid else {}
            first = None
            for old in st.get("sessions") or []:  # earlier sessions, before a fresh start
                if old not in done:
                    got = finished_export(task, old)
                    if got:  # else a busy store: try again next time
                        done[old] = session_entries(got, worktree, verbose)
        entries = task_entries(task) + session_entries(data, worktree, verbose)
        for old in st.get("sessions") or []:
            entries += done.get(old, [])
        for e in sorted((e for e in entries if e.key not in seen), key=lambda e: e.ts):
            seen.add(e.key)
            if e.key.endswith(":end") and e.key[:-4] + ":start" in seen:
                lines = e.lines[1:]          # the call line was printed when it started
            else:
                lines = e.lines
            for style, text in lines:
                emit(style, text)
        phase = task.status.get("phase")
        if as_json:
            while held:
                print(held.popleft(), file=out, flush=True)
            now_line = activity(task, data)
            if now_line:
                emit("activity", now_line, keep=True)
            emit("clock", clock_line(task, data=data), keep=True)
        elif out.isatty() and phase not in store.CLOSED:
            now_line = activity(task, data)
            spin = "✻✳✶✢·"[int(time.time() * 2) % 5]
            row = (paint("claude", f"{spin} {plain(now_line)}") + "  " if now_line else "") \
                + paint("out", clock_line(task, data=data) or "")
            print("\r\033[K" + row, end="", file=out, flush=True)
        if phase != last_phase and phase in PHASE_LINE and (last_phase is not None or once
                                                            or phase not in ("running",)):
            line = API_QUEUED_LINE if phase == "queued" and api_task(task) else PHASE_LINE[phase]
            emit(*line, **({"keep": True} if as_json else {}))
        last_phase = phase
        if once or phase in store.CLOSED:
            return
        # the task's files change as it works: wake on that; on a terminal, also tick the clock,
        # every `poll` while the task is live, else every 30 s (#555)
        dirty = bool(watcher.wait(poll if out.isatty() and phase in LIVE_PHASES else 30))
