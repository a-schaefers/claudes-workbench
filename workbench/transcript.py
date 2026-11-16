"""The conversation between Claude and a task's agent, as a readable live transcript (`wb watch`).

Two sources, merged by time:
- the OpenCode session (`opencode session export`): what Claude sent (the spec, review feedback,
  the automatic test-fix prompts), and everything the agent did: its reasoning, every tool call
  with input and output, its messages.
- the task's own files: test results, questions to Claude and the answers, stops, and the
  task's phase (waiting for review, accepted...).
"""
from __future__ import annotations

import difflib
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass

from . import models, store
from .runner import INTERRUPT_HEAD, NOTE_HEAD, opencode_bin

STYLE = {"claude": "1;38;5;208", "agent": "1;36", "think": "2;3", "tool": "33", "out": "2",
         "ok": "1;32", "bad": "1;31", "sys": "34", "add": "32", "del": "31", "head": "1"}


@dataclass
class Entry:
    ts: float
    key: str
    lines: list[tuple[str, str]]  # (style, text)


class Painter:
    def __init__(self, color: bool):
        self.color = color

    def __call__(self, style: str, text: str) -> str:
        if not self.color or not style:
            return text
        return f"\033[{STYLE[style]}m{text}\033[0m"


def export(session: str) -> dict:
    r = subprocess.run(opencode_bin() + ["session", "export", session], capture_output=True,
                       text=True, timeout=30, cwd="/")
    return json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else {}


def find_session(task: store.Task) -> str | None:
    """The task's OpenCode session: from its status, or by its title before the first event."""
    sid = task.status.get("session")
    if sid or not task.worktree.exists():
        return sid
    r = subprocess.run(opencode_bin() + ["session", "list"], capture_output=True, text=True,
                       cwd=task.worktree, env={**os.environ, "PWD": str(task.worktree)},
                       timeout=30)
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


def _user_message(text: str, verbose: bool) -> tuple[str, list[tuple[str, str]]]:
    if text.startswith("Review feedback from the senior reviewer"):
        head = "Claude → agent · review feedback"
    elif text.startswith("The acceptance tests fail"):  # runner.auto_fix_prompt
        head = "workbench → agent · tests failed, sent back automatically"
    elif text.startswith("You are picking this task up in a fresh session"):  # runner.handoff_prompt
        head = "workbench → agent · fresh session: the task, the work so far, what's next"
    elif text.startswith("Step ") and " of this task is done and committed" in text[:60]:
        head = "Claude → agent · next step"
    elif text.startswith(INTERRUPT_HEAD):
        head = "Claude → agent · INTERRUPT, high priority"
    elif text.startswith(NOTE_HEAD):
        head = "Claude → agent · note"
    else:
        head = "Claude → agent · spec"
    body = text
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
            out.append(Entry(ts, key, [("claude", f"■ Claude {what}")]))
        elif kind == "interrupted":
            out.append(Entry(ts, key, [("bad", "■ the agent was interrupted")]))
        elif kind == "fresh":
            out.append(Entry(ts, key, [("sys", f"■ {r.get('text', '')}")]))
        elif kind == "next_step":
            out.append(Entry(ts, key, [("ok", f"■ {r.get('text', '')}")]))
        elif kind == "takeover":
            out.append(Entry(ts, key, [("claude", "■ Claude took over: finishing it by hand")]))
        elif kind == "stopped":
            out.append(Entry(ts, key, [("bad", f"■ stopped: {r.get('text', '')}")]))
        elif kind == "error":
            out.append(Entry(ts, key, [("bad", f"■ error: {r.get('text', '')}")]))
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
    tokens = ""  # totals are on the clock line
    if phase == "queued":
        return f"Waiting for the GPU… {st.get('last') or ''}".strip()
    if phase == "testing":
        return f"Running the acceptance tests… `{task.spec.get('test_cmd', '')}`"
    if phase == "asking":
        return "Waiting for Claude's answer…"
    if phase != "running":
        return None
    msgs = data.get("messages") or []
    if st.get("last", "").startswith(("loading", "starting", "stopping", "unloading")):
        return f"{st['last'][:1].upper()}{st['last'][1:]}…"
    stamps = [((m.get("time") or {}).get("completed") or (m.get("time") or {}).get("created") or 0)
              for m in msgs]
    since = now - (max(stamps) / 1000 if stamps else (task.spec.get("created_at") or now))
    verb = VERBS[len(msgs) % len(VERBS)]
    live = _server_live(task)
    if live:
        if live.get("state") == "reading" or live.get("phase") == "reading the prompt":
            n = live.get("prompt_tokens")
            return (f"Reading the prompt… {_secs(since * 1000)}"
                    + (f" · {_k(n)} tokens" if n else "") + tokens)
        if live.get("state") != "generating":  # loading, unloaded, restarting...
            return f"Model server {live.get('state')}… {_secs(since * 1000)}{tokens}"
        gen = live.get("generated") or 0
        rate = live.get("tok_s")
        what = "Thinking" if live.get("phase") == "thinking" else "Writing"
        return (f"{what}… {_secs(since * 1000)} · ↓ {_k(gen)} tokens"
                + (f" · {rate:.0f} tok/s" if rate else "") + tokens)
    return f"{verb}… {_secs(since * 1000)}{tokens}"


def model_line(task: store.Task, data: dict) -> str:
    """The model and how it is set to think: OpenCode's variant and the model's own settings."""
    sp = task.spec
    m = models.discover().get(sp.get("model", ""))
    cfg = m.config if m else {}
    variant = ((data.get("info") or {}).get("model") or {}).get("variant") \
        or (sp.get("opencode_model", "").partition("#")[2]) or "default"
    vopts = ((cfg.get("variants") or {}).get(variant) or {})
    opts = {**(cfg.get("options") or {}), **(vopts.get("options") or vopts)}
    effort = opts.get("reasoningEffort") or opts.get("reasoning_effort") or (
        f"{_k(opts['thinking']['budgetTokens'])} thinking tokens"
        if isinstance(opts.get("thinking"), dict) and opts["thinking"].get("budgetTokens") else None)
    if cfg.get("reasoning") is False:
        think = "no reasoning"
    else:
        think = f"reasoning {effort}" if effort else "reasoning on, default effort"
    ctx = (cfg.get("limit") or {}).get("context")
    return " · ".join(x for x in (sp.get("model_label") or sp.get("model"), think,
                                  f"variant {variant}", f"{ctx // 1024}K context" if ctx else "") if x)


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
        typical = router.expected_minutes(sp.get("model", ""), sp.get("size", "significant"))
        if typical:
            left = typical * 60 - took
            parts.append(f"~{_secs(left * 1000)} left (typically {typical:.0f}m)" if left > 0
                         else f"{_secs(-left * 1000)} past its typical {typical:.0f}m")
        else:
            parts.append("no ETA yet (wb bench)")
    used, window = context_fill(data or {}), st.get("window")
    if used:
        parts.append(f"context {_k(used)}" + (f" of {window // 1024}K ({100 * used // window}%)"
                                               if window else ""))
    if st.get("tok_out"):
        parts.append(f"{_k(st['tok_out'])} tokens written")
    return "⏲ " + " · ".join(parts)


PHASE_LINE = {
    "queued": ("sys", "■ waiting for the GPU"),
    "review": ("ok", "■ done: waiting for Claude's review"),
    "stuck": ("bad", "■ stuck: waiting for Claude"),
    "failed": ("bad", "■ failed: waiting for Claude"),
    "asking": ("claude", "■ waiting for Claude's answer"),
    "accepted": ("ok", "■ Claude accepted it"),
    "discarded": ("bad", "■ Claude discarded it"),
}


def header(task: store.Task, paint: Painter, data: dict | None = None) -> str:
    sp = task.spec
    lines = [paint("head", f"task {task.name}") + f" · {model_line(task, data or {})}",
             paint("out", f"  repo {sp.get('repo')} · branch {sp.get('branch')}"
                   + (f" · tests `{sp['test_cmd']}`" if sp.get("test_cmd") else ""))]
    if sp.get("route"):
        lines.append(paint("out", f"  routed: {sp['route']}"))
    return "\n".join(lines)


def follow(task: store.Task, verbose: bool = False, once: bool = False, color: bool | None = None,
           out=None, poll: float = 1.5, as_json: bool = False) -> None:
    """Print the transcript, then (unless once) keep printing what's new until the task closes.
    as_json writes one {"s": style, "t": text} per line, for the Claude Code pane to style."""
    out = out or sys.stdout
    paint = Painter(out.isatty() if color is None else color)
    if as_json:
        def emit(style, text):
            print(json.dumps({"s": style, "t": text}, ensure_ascii=False), file=out, flush=True)
    else:
        clear = "\r\033[K" if out.isatty() else ""  # over the activity row, redrawn after
        def emit(style, text):
            print(clear + paint(style, text), file=out, flush=True)
    sid = find_session(task)
    first = export(sid) if sid else {}
    if as_json:
        emit("meta", ("benchmark task, graded by hidden tests · " if task.spec.get("bench") else "")
             + model_line(task, first))
    else:
        print(header(task, paint, first), file=out, flush=True)
    seen: set[str] = set()
    last_phase = None
    worktree = str(task.worktree)
    while True:
        sid = find_session(task)
        data = export(sid) if sid else {}
        entries = task_entries(task) + session_entries(data, worktree, verbose)
        for old in task.status.get("sessions") or []:  # earlier sessions, before a fresh start
            entries += session_entries(export(old), worktree, verbose)
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
            now_line = activity(task, data)
            if now_line:
                emit("activity", now_line)
            emit("clock", clock_line(task, data=data))
        elif out.isatty() and phase not in store.CLOSED:
            now_line = activity(task, data)
            spin = "✻✳✶✢·"[int(time.time() * 2) % 5]
            row = (paint("claude", f"{spin} {now_line}") + "  " if now_line else "") \
                + paint("out", clock_line(task, data=data) or "")
            print("\r\033[K" + row, end="", file=out, flush=True)
        if phase != last_phase and phase in PHASE_LINE and (last_phase is not None or once
                                                            or phase not in ("running",)):
            emit(*PHASE_LINE[phase])
        last_phase = phase
        if once or phase in store.CLOSED:
            return
        time.sleep(poll)
