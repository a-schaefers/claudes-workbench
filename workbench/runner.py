"""The detached process that drives one task's rounds until it needs Claude.

    python3 -m workbench.runner <task>

Each round sends rounds/<n>/prompt.md to the agent (`opencode run`, continuing the task's session
after the first, unless that session is full: see fresh_reason), streams its events into events.jsonl and status.json, and stops it if it loops,
errors repeatedly, goes silent or runs too long. After a clean round the task's test command runs
in the worktree; a failure goes back to the agent on its own while auto-fix rounds remain, so
Claude is only woken for work that passes, or for a task that has run out of road.
"""
import json
import os
import queue
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

from . import context, gitguard, gitops, lane, lessons, models, skills, store, tuning
from .events import StuckDetector, normalize
from .watch import Watcher

PKG_ROOT = Path(__file__).resolve().parent.parent
CURRENT: dict = {}  # the agent process of the round in flight, for the SIGTERM handler
# Keep build droppings out of the worktree, so the review diff is only the agent's work.
CLEAN_ENV = {"PYTHONDONTWRITEBYTECODE": "1"}


class Stopped(BaseException):  # not an Exception: no `except Exception` on the way may swallow a stop
    pass


def _on_term(signum, frame):
    proc = CURRENT.get("proc")
    if proc and proc.poll() is None:
        _stop(proc)  # the agent runs in its own process group; take it down with us
    raise Stopped()


def opencode_bin() -> list[str]:
    fake = os.environ.get("WB_OPENCODE_BIN")
    return shlex.split(fake) if fake else ["opencode"]


# OpenCode compacts a session once its context passes the model's input limit less
# `compaction.buffer`, and its compaction is the model's own summary, which a small model can get
# wrong: seen 2026-11-16, a summary that recounted CLAUDE.md instead of the task, and the agent
# built what CLAUDE.md said came next. So the runner hands a filling session to a fresh one first
# (the fresh_at knob, checked at every step), and OpenCode's compaction is only the backstop past that:
# its buffer leaves room for one reply at the model's output cap, plus one step's growth.
STEP_GROWTH = 4096


def compaction_buffer(cfg: dict) -> int | None:
    """The buffer that puts OpenCode's compaction where a request still fits the window."""
    lim, opts = cfg.get("limit") or {}, cfg.get("options") or {}
    window = lim.get("context")
    if not window:
        return None
    usable = lim.get("input") or window - (lim.get("output") or 0)
    reply = opts.get("max_tokens") or lim.get("output") or 0
    return max(usable + reply - window, 0) + STEP_GROWTH


def fresh_ceiling(cfg: dict) -> float | None:
    """The latest a fresh session can take over: where OpenCode's compaction sits (tuning.py)."""
    lim, buf = cfg.get("limit") or {}, compaction_buffer(cfg)
    window = lim.get("context")
    if not window or buf is None:
        return None
    usable = lim.get("input") or window - (lim.get("output") or 0)
    return (usable - buf) / window


def write_config_layer(task: store.Task) -> Path:
    """The per-task OpenCode config: the agent's context and the ask_reviewer server."""
    path = context.rules_dir(task) / "opencode.json"
    wait_ms = int(task.spec.get("ask_timeout", 1200) + 60) * 1000
    try:
        buffer = compaction_buffer(models.resolve(task.spec["model"]).config)
    except KeyError:
        buffer = None
    store.write_json(path, {
        "$schema": "https://opencode.ai/config.json",
        **context.layer(task),
        **({"compaction": {"auto": True, "buffer": buffer}} if buffer else {}),
        "mcp": {"servers": {"workbench": {  # OpenCode 2.x shape: mcp.servers.<name>
            "type": "local",
            "command": [sys.executable, str(PKG_ROOT / "bin" / "wb-ask-mcp")],
            "environment": {"WB_TASK": task.name, "WB_HOME": str(store.home())},
            "codemode": False,  # a plain tool the model sees, not one behind code mode
            # the tool blocks until Claude answers; OpenCode must not give up on it first
            "timeout": {"startup": 15000, "execution": wait_ms},
        }}},
    })
    return path


# What the agent's environment leaves out of the runner's: credentials (API keys and tokens Claude
# Code or the user's shell carry, the SSH agent that would sign a push) and the runner's own
# PYTHONPATH, which would put `python3 -m workbench.cli` in the agent's reach. A provider that
# needs a key from the environment names it in commands.json `env`, which passes it through.
SECRET_ENV = re.compile(r"TOKEN|SECRET|PASSW|CREDENTIAL|API_?KEY|ACCESS_KEY|PRIVATE_KEY|"
                        r"^(ANTHROPIC|CLAUDE|GH|GITHUB|GITLAB|AWS|AZURE|GOOGLE|OPENAI|HF)_|"
                        r"^(SSH_AUTH_SOCK|SSH_AGENT_PID|GPG_AGENT_INFO|DBUS_SESSION_BUS_ADDRESS|"
                        r"PYTHONPATH)$", re.I)


def agent_env(task: store.Task) -> dict:
    """The agent's environment: its config layer, and git read-only (gitguard.py)."""
    through = set((store.read_json(context.commands_path(), {}) or {}).get("env") or [])
    keep = {k: v for k, v in os.environ.items() if k in through or not SECRET_ENV.search(k)}
    env = {**keep, "PWD": str(task.worktree),  # opencode finds the project from $PWD
           "OPENCODE_CONFIG": str(write_config_layer(task)),
           "WB_TASK": task.name, "WB_HOME": str(store.home()), **CLEAN_ENV}
    env.update(gitguard.install(context.rules_dir(task) / "bin", Path(task.spec["repo"]), PKG_ROOT,
                                env.get("PATH") or os.defpath) or {})
    return env


def claude_files_changed(files: str) -> list[str]:
    """The instruction files and .claude/ paths among `git status --short` lines."""
    out = []
    for line in files.splitlines():
        path = line[3:].split(" -> ")[-1].strip('"')
        name = Path(path).name
        if name in ("CLAUDE.md", "CLAUDE.local.md", "AGENTS.md") or \
                path.startswith(".claude/") or "/.claude/" in path:
            out.append(path)
    return out


def _reader(stream, q):
    for line in stream:
        q.put(line)
    q.put(None)


def _stop(proc: subprocess.Popen):
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=10)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


# Claude steering a running task (`wb steer`): queued notes wait for the agent's current run to
# end; a --now note interrupts it. Either way the same session continues with the note as its
# next prompt. steer.jsonl holds every note; status.steer_taken counts the delivered ones.
INTERRUPT_HEAD = "Claude, your reviewer, interrupted you."
NOTE_HEAD = "A note from Claude, your reviewer, before you go on."


# The runner's own steer when the agent generates past its thinking budget with nothing to show.
NUDGE = ("You have been thinking for {mins} minutes without a tool call. Stop planning and act "
         "now: make the next edit, one file per tool call, smallest first, and run the tests "
         "as soon as there is something to test. Keep each thought short; decide as you go.")


def pending_steer(task: store.Task) -> list[dict]:
    rows, _ = store.read_jsonl(task.dir / "steer.jsonl")
    return rows[task.status.get("steer_taken", 0):]


def steer_prompt(notes: list[dict], interrupted: bool) -> str:
    body = "\n\n".join(r["text"].strip() for r in notes)
    if interrupted:
        return (f"{INTERRUPT_HEAD} Stop what you were doing and do this first; it overrides "
                f"anything earlier that conflicts with it:\n\n{body}\n\nThen carry on with the "
                "task unless this says otherwise, and finish with a short summary as before.")
    return (f"{NOTE_HEAD} Take it into account now:\n\n{body}\n\nThen carry on with the task, "
            "and finish with a short summary as before.")


def outside_spec(task: store.Task, files: str) -> list[str]:
    """Changed files the current spec never names, when it names any: the agent's drift shows
    here first (seen 2026-11-16: a flow-field module nobody asked for). Matched by path or name."""
    spec = current_task_prompt(task)
    paths = [line[3:].split(" -> ")[-1].strip('"') for line in files.splitlines()]
    if not any(p in spec or Path(p).name in spec for p in paths):
        return []
    return [p for p in paths if p not in spec and Path(p).name not in spec]


def _clock(secs: float) -> str:
    return f"{int(secs // 60)}m{int(secs % 60):02d}s"


def checkin_text(task: store.Task, n: int, out: dict, since: float) -> str:
    """The digest a check-in wakes Claude with: what changed since the last one, and timing."""
    from . import router  # router -> models; imported late to keep the runner's startup lean
    spec, st, now = task.spec, task.status, time.time()
    took = now - spec["created_at"]
    typical = router.expected_minutes(spec["model"], spec.get("size", "significant"))
    eta = ""
    if typical:
        left = typical * 60 - took
        eta = (f" · ~{_clock(left)} left (typically {typical:.0f}m)" if left > 0 else
               f" · {_clock(-left)} past its typical {typical:.0f}m")
    rows = [r for r in store.read_jsonl(task.dir / "events.jsonl")[0] if r.get("ts", 0) > since]
    tools: dict = {}
    for r in rows:
        if r.get("kind") == "tool":
            tools[r["tool"]] = tools.get(r["tool"], 0) + 1
    errs = [r for r in rows if r.get("kind") == "tool" and not r.get("ok")]
    tok_in = sum(r.get("in", 0) for r in rows if r.get("kind") == "step")
    tok_out = sum(r.get("out", 0) for r in rows if r.get("kind") == "step")
    lines = [f"task {task.name} · {spec['model_label']} · round {n} · running {_clock(took)}{eta}",
             f"since the last check-in ({_clock(now - since)}): "
             + (", ".join(f"{k}×{v}" for k, v in sorted(tools.items())) or "no tool calls")
             + (f", {len(errs)} failed" if errs else "")
             + f"; {tok_in} tokens read, {tok_out} written",
             f"now: {st.get('last') or '?'}"]
    if out["texts"]:
        lines.append("its latest words: " + out["texts"][-1].strip().replace("\n", " ")[:400])
    files = gitops.changed(task.worktree)
    lines.append("files changed so far: "
                 + (", ".join(f[3:] for f in files.splitlines()) if files else "none"))
    if extra := outside_spec(task, files):
        lines.append("NOTE: the spec doesn't name " + ", ".join(extra)
                     + ": check it is still on its task")
    return "\n".join(lines)


def _steer_bell(task: store.Task, proc, q: queue.Queue) -> None:
    """Ring the round's loop the moment Claude writes a steer note, so `wb steer --now`
    interrupts at once instead of at the loop's next timeout."""
    with Watcher(task.dir) as w:
        while proc.poll() is None:
            if any(p.name == "steer.jsonl" for p in w.wait(5)):
                q.put("")


# Linux takes at most 128 KiB in one argv string (MAX_ARG_STRLEN); a longer prompt is sent as a
# pointer to its file in the round's directory, which the agent can read.
PROMPT_ARG_MAX = 100_000


def run_round(task: store.Task, n: int, keep: bool = False) -> dict:
    """One `opencode run`. keep: Claude asked for this session to carry on (wb feedback
    --keep-context), so the round never hands over mid-way."""
    spec, st = task.spec, task.status
    prompt = task.round_dir(n).joinpath("prompt.md").read_text()
    cmd = opencode_bin() + ["run", "--standalone", "-m", spec["opencode_model"], "--auto",
                            "--format", "json", "--title", f"wb: {task.name}"]
    marks = {gitops.mark(task.worktree)}  # the tree as the round and the session began
    if st.get("session"):
        cmd += ["--session", st["session"]]
        marks.add(st.get("session_mark"))
    else:
        task.set_status(session_mark=next(iter(marks)), session_floor=None)
    floor = task.status.get("session_floor")  # the context the session had read when it began work
    note = skills_note(task, n)
    if len((note + prompt).encode()) > PROMPT_ARG_MAX:
        prompt = (f"Your instructions for this round are long, so they are in the file "
                  f"{task.round_dir(n) / 'prompt.md'}. Read all of it first (in parts if it is "
                  f"too big for one read), then do what it says.")
    cmd.append(note + prompt)
    env = agent_env(task)
    cmd = lane.wrap(context.writable(task), cmd,   # writes stay in the agent's lane (lane.py),
                    context.readable(task))       # and credentials out of its reach
    log = open(task.dir / "runner.log", "a")
    proc = subprocess.Popen(cmd, cwd=task.worktree, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=log, text=True,
                            start_new_session=True)  # its own group, so a stop takes MCP too
    CURRENT["proc"] = proc
    task.set_status(agent_pid=proc.pid)  # its own session: wb stop ends it if the runner can't
    q: queue.Queue = queue.Queue()
    threading.Thread(target=_reader, args=(proc.stdout, q), daemon=True).start()
    threading.Thread(target=_steer_bell, args=(task, proc, q), daemon=True).start()

    detector = StuckDetector(**spec.get("stuck", {}))
    wall, silence = float(spec.get("round_timeout", 2400)), float(spec.get("silence_timeout", 900))
    think = float(spec.get("think_timeout") or 0)
    nudged = False
    renudge = task.status.get("nudged_round") == n - 1  # the last round ended in our nudge
    edit_mark = gitops.mark(task.worktree)  # shell edits (sed -i, cat >) count as edits too
    started = last_seen = prev = time.time()
    paused = 0.0  # time spent waiting on Claude's answer: not the round's own
    out = {"exit": None, "stuck": None, "texts": [], "tools": {}, "errors": [],
           "tok_in": 0, "tok_out": 0, "interrupted": False, "full": None}
    window = st.get("window")
    fresh_at = tuning.value(task, "fresh_at")
    while True:
        try:  # woken by the agent's output or a steer note; the timeout runs the clocks below
            line = q.get(timeout=5)
        except queue.Empty:
            line = ""
        if line is None:
            break
        now = time.time()
        if line:
            last_seen = now
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if raw.get("sessionID") and not task.status.get("session"):
                task.set_status(session=raw["sessionID"])
            row = normalize(raw, n)
            if row is None:
                continue
            sig = row.pop("sig", None)
            if row["kind"] == "tool":
                out["tools"][row["tool"]] = out["tools"].get(row["tool"], 0) + 1
                if not row["ok"]:
                    out["errors"].append(f"{row['tool']}: {row.get('error', '')}")
                task.set_status(last=f"{row['tool']} {row['brief']}".strip(),
                                tools=task.status.get("tools", 0) + 1)
            elif row["kind"] == "text":
                out["texts"].append(row["text"])
                row["text"] = row["text"][:2000]
            elif row["kind"] == "step":
                out["tok_in"] += row["in"]
                out["tok_out"] += row["out"]
                task.set_status(tok_in=task.status.get("tok_in", 0) + row["in"],
                                tok_out=task.status.get("tok_out", 0) + row["out"])
                used = row["in"] + row.get("cache", 0) + row["out"]
                # Full: a fresh session takes over (drive), never OpenCode's own summary. Only
                # once this session has changed a file: one that filled up reading would hand
                # the same reading to the next, which fills up too (seen 2026-11-16: twenty
                # handovers in twelve minutes, no edit made). It carries on instead.
                # And only when a successor, re-reading what this one read before its first
                # change (its floor), would have room left to work: seen the same day, a step
                # whose reading came to 16K of a 19K handover point handed over three times in
                # four minutes, each fresh session getting 3K of work. It carries on instead.
                changed = None
                if floor is None and window:
                    changed = marks != {gitops.mark(task.worktree)}
                    if changed:
                        floor = used
                        task.set_status(session_floor=floor)
                if window and not keep and used >= fresh_at * window and not out["full"]:
                    if changed is None:
                        changed = marks != {gitops.mark(task.worktree)}  # since either began
                    if not changed:
                        if not out.get("reading_full"):
                            out["reading_full"] = used
                            _reading_full(task, n, used, window)
                    elif fresh_at * window - floor >= HANDOVER_ROOM * window:
                        out["full"] = used
                    elif not out.get("reading_full"):
                        out["reading_full"] = used
                        _reading_full(task, n, used, window, floor)
            elif row["kind"] == "error":
                out["errors"].append(f"opencode: {row['text']}")
            task.event(row)
            why = detector.feed({**row, "sig": sig}) if sig else None
            if why and detector.since_edit >= detector.no_edit:
                if (m := gitops.mark(task.worktree)) != edit_mark:  # it edited through the shell
                    edit_mark, detector.since_edit, why = m, 0, None
            if why:
                out["stuck"] = why
        asking = task.status.get("phase") == "asking"  # the agent is waiting on Claude: not silent
        if asking:  # the wait is neither the round's time nor silence
            paused += now - prev
            last_seen = now
        prev = now
        if not out["stuck"] and now - started - paused > wall:
            out["stuck"] = f"the round ran past its {int(wall // 60)}-minute limit"
        if not out["stuck"] and not asking and now - last_seen > silence:
            out["stuck"] = f"no output for {int(silence // 60)} minutes"
        # Generating with nothing to show past the budget: interrupt with a nudge (once a round)
        # and tell Claude. The nudge starts a new round, so one that again shows nothing at all
        # is stopped: else a model that never answers would be nudged forever.
        if think and not nudged and not out["stuck"] and not asking and now - last_seen > think:
            if renudge and not out["tools"] and not out["texts"]:
                out["stuck"] = (f"no tool call or word for {int((now - last_seen) // 60)} minutes, "
                                "even after a nudge")
        if think and not nudged and not out["stuck"] and not asking and now - last_seen > think:
            nudged = True
            mins = int((now - last_seen) // 60)
            text = NUDGE.format(mins=mins)
            task.set_status(nudged_round=n)
            store.append_jsonl(task.dir / "steer.jsonl", {"ts": store.now(), "text": text, "now": True,
                                                         "by": "workbench"})
            task.event({"ts": store.now(), "round": n, "kind": "steer", "now": True, "by": "workbench",
                        "text": text})
            nudges = task.status.get("nudges", 0) + 1
            task.set_status(nudges=nudges)
            task.notify("checkin", checkin_text(task, n, out, max(started, float(
                task.status.get("checkin_at") or 0))) + f"\nthe runner nudged it: {mins} min of "
                f"thinking with no tool call (nudge {nudges} this task; budget {int(think)}s)", round=n)
        if out["stuck"]:
            task.event({"ts": store.now(), "round": n, "kind": "stopped", "text": out["stuck"]})
            _stop(proc)
            break
        if out["full"]:
            _stop(proc)
            break
        every = float(spec.get("checkin") or 0) * 60
        since = max(started, float(task.status.get("checkin_at") or 0))
        if every and now - since >= every and not asking:
            task.set_status(checkin_at=now)
            task.notify("checkin", checkin_text(task, n, out, since), round=n)
        notes = pending_steer(task)
        if any(r.get("now") for r in notes):
            ours = all(r.get("by") == "workbench" for r in notes)
            task.event({"ts": store.now(), "round": n, "kind": "interrupted",
                        **({"by": "workbench"} if ours else {}),
                        "text": "the runner interrupted the agent with a nudge" if ours
                        else "Claude interrupted the agent"})
            out["interrupted"] = True
            _stop(proc)
            break
    out["exit"] = proc.wait()
    log.close()
    if task.status.get("asking"):  # it ended mid-question: nothing will read that answer now
        task.set_status(asking=None)
    task.set_status(agent_pid=None)
    out["secs"] = round(time.time() - started, 1)
    return out


def run_tests(task: store.Task, n: int):
    cmd = task.spec.get("test_cmd")
    if not cmd:
        return None
    task.set_status(phase="testing", last=f"tests: {cmd}")
    limit = int(task.spec.get("test_timeout") or 900)
    # in the agent's lane and with its environment: the tests run the agent's code, and must
    # pass or fail as they did when the agent ran them
    argv = lane.wrap(context.writable(task), ["/bin/sh", "-c", cmd], context.readable(task))
    proc = subprocess.Popen(argv, cwd=task.worktree, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, env=agent_env(task),
                            start_new_session=True)  # its own group: a timeout ends all of it
    try:
        output, _ = proc.communicate(timeout=limit)
        code = proc.returncode
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        output, _ = proc.communicate()
        code, output = "timeout", (output or b"") + f"\n[timed out after {limit}s]".encode()
    output = output.decode(errors="replace")
    tail = "\n".join(output.strip().splitlines()[-40:])
    task.round_dir(n).joinpath("tests.txt").write_text(f"$ {cmd}\nexit: {code}\n\n{output}")
    result = {"ok": code == 0, "exit": code, "tail": tail, "round": n}
    task.event({"ts": store.now(), "round": n, "kind": "tests", "ok": result["ok"],
                "text": f"exit {code}"})
    return result


def summary_text(task: store.Task, n: int, rnd: dict, tests) -> str:
    """The compact report Claude reads; kept short on purpose, the diff is fetched separately."""
    spec, st = task.spec, task.status
    lines = [f"task {task.name} · {spec['model_label']} · round {n} · {rnd['secs'] / 60:.1f} min"
             f" · local tokens {rnd['tok_in']} in / {rnd['tok_out']} out"]
    c = context_state(task)
    if c.get("used") and c.get("window"):
        lines.append(f"agent context: {c['used'] / 1000:.1f}K of {c['window'] // 1024}K "
                     f"({100 * c['used'] // c['window']}%)"
                     + (f", compacted {c['compactions']}x" if c.get("compactions") else "")
                     + ("; the next round starts a fresh session" if fresh_reason(task) else ""))
    if rnd["stuck"]:
        lines.append(f"STOPPED: {rnd['stuck']}")
    elif rnd["exit"] not in (0, None):
        lines.append(f"opencode exited {rnd['exit']} (see {task.dir / 'runner.log'})")
    if tests is not None:
        lines.append(f"tests: {'PASS' if tests['ok'] else 'FAIL'} (`{spec['test_cmd']}`, "
                     f"exit {tests['exit']})")
        if not tests["ok"]:
            lines.append("```\n" + "\n".join(tests["tail"].splitlines()[-25:]) + "\n```")
    if st.get("auto_fixes"):
        lines.append(f"auto-fix rounds used: {st['auto_fixes']}/{spec.get('auto_fix', 0)}")
    tools = ", ".join(f"{k}×{v}" for k, v in sorted(rnd["tools"].items())) or "none"
    lines.append(f"tools: {tools}")
    for err in rnd["errors"][-4:]:
        lines.append(f"tool error: {err[:200]}")
    final = (rnd["texts"][-1] if rnd["texts"] else "(the agent wrote no final message)").strip()
    lines += ["", "agent's summary:", final[:2500], ""]
    files = gitops.changed(task.worktree)
    lines += ["files changed:", files or "(none)"]
    if extra := outside_spec(task, files):
        lines.append("NOTE: the spec doesn't name " + ", ".join(extra)
                     + ": needed for the task, or drift?")
    if mine := claude_files_changed(files):
        lines.append(f"NOTE: the agent changed your files ({', '.join(mine)}): instructions and "
                     ".claude/ are yours to write; keep, rewrite or revert them")
    stat = gitops.diff(task.worktree, spec["base"], stat=True).splitlines()
    if stat:
        lines.append(stat[-1].strip())
    return "\n".join(lines)


# A round continues the task's OpenCode session, so the agent keeps what it has read. Once that
# session has filled a share of the model's window (the fresh_at knob, tuning.py: learned per
# model and window from how tasks went), or OpenCode has compacted it
# (a small model's own summary of a coding session drops details), the next round starts a fresh
# session that is handed exactly what it needs: the task, the work so far and the new instructions.
ASKED_FRESH = "Claude asked for a fresh session"
COMPACTED = "OpenCode had to compact the last session"
# The handoff carries the diff while it is under this share of the window (at ~4 characters a
# token: 12K characters, ~3K tokens, on a 32K one); a bigger diff is listed, for the agent to read.
HANDOFF_DIFF_SHARE = 0.09
HANDOFF_DIFF_MAX = 60_000  # characters, whatever the window: the prompt is one argv string (see run_round)


def context_state(task: store.Task) -> dict:
    """How full the task's current session is: {"used", "window", "compactions"}."""
    sid = task.status.get("session")
    if not sid:
        return {}
    from .transcript import export  # transcript imports this module
    msgs = export(sid).get("messages", [])
    last = next((m for m in reversed(msgs) if m.get("type") == "assistant" and m.get("tokens")), None)
    used = None
    if last:  # what the next request carries: everything the last one read, plus its reply
        tk, cache = last["tokens"], last["tokens"].get("cache") or {}
        used = tk.get("input", 0) + cache.get("read", 0) + cache.get("write", 0) + tk.get("output", 0)
    return {"used": used, "window": task.status.get("window"),
            "compactions": sum(m.get("type") == "compaction" and m.get("status") == "completed"
                               for m in msgs)}


def fresh_reason(task: store.Task) -> str | None:
    """Why round n should start a fresh session, or None to continue the current one."""
    st = task.status
    if st.get("context") == "fresh":  # even with no session yet: the prompt still needs the task
        return ASKED_FRESH
    if not st.get("session"):  # the step's first round, or one whose session never started
        first = (st.get("step_round") or 1) == st.get("round", 1)
        return None if first else "the last session ended before it began"
    if st.get("context") == "keep":
        return None
    if st.get("context") == "full":
        return f"the last session was {st.get('full_pct')}% full"
    c = context_state(task)
    if c.get("compactions"):
        return COMPACTED
    if c.get("used") and c.get("window") and c["used"] >= tuning.value(task, "fresh_at") * c["window"]:
        return (f"the last session was {100 * c['used'] // c['window']}% full "
                f"({c['used'] / 1000:.1f}K of {c['window'] // 1024}K tokens)")
    return None


def handoff_prompt(task: store.Task, why: str, instructions: str) -> str:
    wt, spec = task.worktree, task.spec
    steps = gitops.git(wt, "log", "--oneline", f"{spec['base']}..HEAD").strip()
    diff = gitops.diff(wt, "HEAD")
    stat = gitops.diff(wt, "HEAD", stat=True)
    room = min(int(HANDOFF_DIFF_SHARE * (task.status.get("window") or 32768) * 4), HANDOFF_DIFF_MAX)
    work = (f"```diff\n{diff}\n```" if len(diff) <= room else
            f"The diff is too long to include; read the changed files:\n```\n{stat}\n```")
    return (f"You are picking this task up in a fresh session ({why}). Nothing from the earlier "
            f"session carries over except what is in the working tree, so start from what is "
            f"below and re-read the files you need.\n\n## The task\n\n"
            f"{current_task_prompt(task)}\n\n## The work so far\n\n"
            + (f"Earlier steps, committed:\n```\n{steps}\n```\n\n" if steps else "")
            + (f"Uncommitted changes (`git diff HEAD`):\n{work}" if diff else
               "No uncommitted changes yet.")
            + f"\n\n## Now\n\n{instructions}")


def skills_note(task: store.Task, n: int) -> str:
    """What the agent is told about the skills Claude handed it: the whole set at the start of a
    session, then only what changed (handed or taken back) since it was last told."""
    st = task.status
    now, told = st.get("skills") or [], st.get("skills_told")
    have = skills.available()
    lines = []
    if told is None:
        new, gone = now, []
        head = "Claude picked these skills for this task. Load each with the skill tool " \
               "before the part of the work it covers, and follow it:"
    else:
        new, gone = [x for x in now if x not in told], [x for x in told if x not in now]
        head = "Claude handed you more skills; load each before the part of the work it covers:"
    if new:
        lines += [head] + [f"- `{x}`: {have[x]['description']}" for x in new if x in have]
    if gone:
        lines.append("Claude took back: " + ", ".join(f"`{x}`" for x in gone)
                     + ". They no longer apply to this work; don't follow them.")
    task.set_status(skills_told=list(now))
    if not lines:
        return ""
    task.event({"ts": store.now(), "round": n, "kind": "skills", "text": "\n".join(lines)})
    return "\n".join(lines) + "\n\n"


def current_task_prompt(task: store.Task) -> str:
    """The spec the agent is working to now, with the protocol: round 1's, or the latest `wb next`."""
    d = task.round_dir(task.status.get("step_round") or 1)
    f = d / "instructions.md"  # the step began in a fresh session: prompt.md is its handoff
    return (f if f.is_file() else d / "prompt.md").read_text().strip()


def _start_fresh(task: store.Task, n: int, why: str) -> None:
    path = task.round_dir(n).joinpath("prompt.md")
    raw = path.read_text().strip()
    path.with_name("instructions.md").write_text(raw)  # round_asks reads it back
    now = "Start on the task above." if n == (task.status.get("step_round") or 1) else raw
    path.write_text(handoff_prompt(task, why, now))
    st = task.status
    task.set_status(session=None, sessions=(st.get("sessions") or []) + [st["session"]] * bool(st.get("session")),
                    context=None, skills_told=None)  # a new session hears the skills again
    task.event({"ts": store.now(), "round": n, "kind": "fresh", "overflow": why != ASKED_FRESH,
                "compacted": why == COMPACTED,
                "text": f"a fresh session for the agent: {why}"})


def auto_fix_prompt(tests: dict, cmd: str) -> str:
    return (f"The acceptance tests fail. Command: `{cmd}` (exit {tests['exit']}). Output:\n\n"
            f"```\n{tests['tail']}\n```\n\nFix the code so the tests pass. Do not weaken, skip or "
            "delete tests to make them pass. Do not commit. Finish with a short summary of what "
            "you changed and the test result.")


def measure_speed(task: store.Task, since: float) -> None:
    """Record how fast the model read and wrote in this round's requests (router.budgets)."""
    from . import router
    from .transcript import export
    sid = task.status.get("session")
    if not sid:
        return
    try:
        msgs = [m for m in export(sid).get("messages", [])
                if ((m.get("time") or {}).get("created") or 0) >= since * 1000]
        router.record_speed(task.spec["model"], msgs)
    except Exception:  # a measurement, never a reason to stop the task
        pass


def _load_failed(task: store.Task, e: Exception) -> None:
    task.set_status(phase="failed", last=f"model: {e}")
    task.notify("failed", f"task {task.name}: could not load {task.spec['model']}: {e}")


def drive(task: store.Task) -> None:
    task.set_status(phase="queued", last="waiting for the GPU", pid=os.getpid())
    say = lambda msg: task.set_status(last=msg)  # noqa: E731
    try:
        m = models.resolve(task.spec["model"])
    except KeyError as e:
        return _load_failed(task, e)
    # beside the running tasks' models when all fit the GPU, else alone once they finish
    with models.GpuLease(m, task.name, say) as lease:
        try:
            secs = lease.load()
            window = (m.config.get("limit") or {}).get("context")
            task.set_status(load_secs=round(secs, 1), window=window)
            tuning.tune(task, task.spec["model"], window, {"fresh_at": fresh_ceiling(m.config)})
        except (KeyError, RuntimeError, OSError) as e:
            return _load_failed(task, e)
        while True:
            n = task.status["round"]
            why = fresh_reason(task) if n > 1 else None
            keep = task.status.get("context") == "keep"
            if why:
                _start_fresh(task, n, why)
            elif keep:
                task.set_status(context=None)  # a one-round instruction, held by run_round
            task.set_status(phase="running", round=n, round_started_at=store.now(),
                            last="agent started")  # not "starting…": the pane shows its progress
            task.event({"ts": store.now(), "round": n, "kind": "round", "text": "started"})
            began = time.time()
            rnd = run_round(task, n, keep=keep)
            measure_speed(task, began)
            if rnd["full"]:  # before notes: the handover carries them (_full_round)
                _full_round(task, n, rnd)
                continue
            if rnd["interrupted"] or (pending_steer(task) and not rnd["stuck"]):
                _steer_round(task, n, rnd["interrupted"])
                continue
            if rnd["stuck"] or (rnd["exit"] not in (0, None) and not rnd["texts"]):
                text = summary_text(task, n, rnd, None)
                task.round_dir(n).joinpath("summary.md").write_text(text)
                phase = "stuck" if rnd["stuck"] else "failed"
                task.set_status(phase=phase, last=rnd["stuck"] or f"opencode exited {rnd['exit']}")
                task.notify(phase, text, round=n)
                return
            tests = run_tests(task, n)
            if pending_steer(task):  # a note that came in while the tests ran
                _steer_round(task, n, False)
                continue
            text = summary_text(task, n, rnd, tests)
            task.round_dir(n).joinpath("summary.md").write_text(text)
            used = task.status.get("auto_fixes", 0)
            if tests and not tests["ok"] and used < int(task.spec.get("auto_fix", 0)):
                task.round_dir(n + 1).mkdir(parents=True, exist_ok=True)
                task.round_dir(n + 1).joinpath("prompt.md").write_text(
                    auto_fix_prompt(tests, task.spec["test_cmd"]))
                task.set_status(round=n + 1, auto_fixes=used + 1, tests=tests)
                task.event({"ts": store.now(), "round": n, "kind": "autofix",
                            "text": "tests failed; sent the output back to the agent"})
                continue
            task.set_status(phase="review", tests=tests,
                            last="tests passed, waiting for review" if tests and tests["ok"]
                            else "waiting for review")
            task.notify("review", text, round=n)
            return


# What the fresh session is told after the runner took over from a full one.
CARRY_ON = ("Your last session filled up, so you are continuing in a fresh one. Carry on with "
            "the task above from where the work stands: re-read the files you are changing, then "
            "finish them and run the tests.{asked} Your last words before the switch:\n\n{last}")
ASKED = "\n\nWhat you were asked to do, and it isn't done until it is:\n\n{}\n\n"
# What the fresh session is told when Claude resumes a task it stopped (wb resume).
RESUME = ("Claude stopped your last session and is resuming the task in a fresh one. Carry on "
          "with the task above from where the work stands: re-read the files you are changing, "
          "then finish them and run the tests.{asked}")


def round_asks(task: store.Task, n: int) -> list[str]:
    """The instructions rounds n, n-1, ... gave since the step's task, back to the last review
    feedback: what a fresh session taking over mid-round must still carry out. A handover's own
    carry-on adds nothing (it is skipped). Seen 2026-11-16: two handovers in a feedback round
    each said only "carry on with the task", and the fresh sessions, finding the tests green,
    reported done with the feedback never made. A resume (wb resume) is skipped the same way."""
    first = task.status.get("step_round") or 1
    out: list[str] = []
    for r in range(n, first, -1):
        d = task.round_dir(r)
        f = d / "instructions.md" if (d / "instructions.md").is_file() else d / "prompt.md"
        raw = f.read_text().strip() if f.is_file() else ""
        if not raw or raw.startswith((CARRY_ON[:60], RESUME[:60])):
            continue
        out.insert(0, raw)
        if raw.startswith(lessons.FEEDBACK_HEAD):
            break
    return out


# A handover needs a successor with room to work: at least this share of the window between
# the reading it repeats (the floor) and the handover point. Less, and the session carries on.
HANDOVER_ROOM = 0.25


def _reading_full(task: store.Task, n: int, used: int, window: int,
                  floor: int | None = None) -> None:
    pct = 100 * used // window
    if floor is None:
        what = "before it changed a file: the reading this step needs fills the handover point"
    else:
        what = (f"and had read {100 * floor // window}% before its first change: a fresh session "
                f"re-reading that would have little room to work")
    text = (f"the agent's session reached {pct}% of its window {what}, so it carries on in this "
            f"session (OpenCode compacts it if it must). Name fewer files to read, or put what "
            f"it needs in the spec.")
    task.event({"ts": store.now(), "round": n, "kind": "reading_full", "text": text})
    task.notify("checkin", f"task {task.name}: {text}", round=n)


def _full_round(task: store.Task, n: int, rnd: dict) -> None:
    window = task.status["window"]
    pct = 100 * rnd["full"] // window
    task.event({"ts": store.now(), "round": n, "kind": "full",
                "text": f"the agent's session reached {pct}% of its window "
                        f"({rnd['full'] / 1000:.1f}K of {window // 1024}K): a fresh one takes over"})
    last = "\n\n".join(t.strip() for t in rnd["texts"][-2:] if t.strip())[-1200:]
    task.round_dir(n + 1).mkdir(parents=True, exist_ok=True)
    asks = round_asks(task, n)
    notes = pending_steer(task)  # Claude's notes that came in as it filled: the successor's too
    task.round_dir(n + 1).joinpath("prompt.md").write_text(CARRY_ON.format(
        asked=ASKED.format("\n\n".join(asks)) if asks else "",
        last="> " + last.replace("\n", "\n> ") if last else "(none)")
        + (f"\n\n{steer_prompt(notes, False)}" if notes else ""))
    task.set_status(round=n + 1, context="full", full_pct=pct,
                    handovers=task.status.get("handovers", 0) + 1,
                    steer_taken=task.status.get("steer_taken", 0) + len(notes))


def _steer_round(task: store.Task, n: int, interrupted: bool) -> None:
    notes = pending_steer(task)
    task.round_dir(n + 1).mkdir(parents=True, exist_ok=True)
    task.round_dir(n + 1).joinpath("prompt.md").write_text(steer_prompt(notes, interrupted))
    task.set_status(round=n + 1, steer_taken=task.status.get("steer_taken", 0) + len(notes))


def main():
    task = store.Task(sys.argv[1])
    signal.signal(signal.SIGTERM, _on_term)
    try:
        drive(task)
    except Stopped:
        task.event({"ts": store.now(), "round": task.status.get("round"), "kind": "stopped",
                    "by": "reviewer", "text": "stopped by the reviewer"})
    except Exception:
        err = traceback.format_exc()
        (task.dir / "runner.log").open("a").write(err)
        task.set_status(phase="failed", last=err.strip().splitlines()[-1][:200])
        task.notify("failed", f"task {task.name}: the runner crashed\n{err[-1500:]}")
    finally:
        task.set_status(pid=None)


if __name__ == "__main__":
    main()
