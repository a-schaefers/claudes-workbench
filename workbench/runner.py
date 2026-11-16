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
import signal
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

from . import context, gitguard, gitops, models, skills, store
from .events import StuckDetector, normalize
from .watch import Watcher

PKG_ROOT = Path(__file__).resolve().parent.parent
CURRENT: dict = {}  # the agent process of the round in flight, for the SIGTERM handler
# Keep build droppings out of the worktree, so the review diff is only the agent's work.
CLEAN_ENV = {"PYTHONDONTWRITEBYTECODE": "1"}


class Stopped(Exception):
    pass


def _on_term(signum, frame):
    proc = CURRENT.get("proc")
    if proc and proc.poll() is None:
        _stop(proc)  # the agent runs in its own process group; take it down with us
    raise Stopped()


def opencode_bin() -> list[str]:
    fake = os.environ.get("WB_OPENCODE_BIN")
    return fake.split() if fake else ["opencode"]


def write_config_layer(task: store.Task) -> Path:
    """The per-task OpenCode config: the agent's context and the ask_reviewer server."""
    path = task.dir / "opencode.json"
    wait_ms = int(task.spec.get("ask_timeout", 1200) + 60) * 1000
    store.write_json(path, {
        "$schema": "https://opencode.ai/config.json",
        **context.layer(task),
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


def agent_env(task: store.Task) -> dict:
    """The agent's environment: its config layer, and git read-only (gitguard.py)."""
    env = {**os.environ, "PWD": str(task.worktree),  # opencode finds the project from $PWD
           "OPENCODE_CONFIG": str(write_config_layer(task)),
           "WB_TASK": task.name, "WB_HOME": str(store.home()), **CLEAN_ENV}
    env.update(gitguard.install(task.dir / "bin", Path(task.spec["repo"]), PKG_ROOT,
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
    return "\n".join(lines)


def _steer_bell(task: store.Task, proc, q: queue.Queue) -> None:
    """Ring the round's loop the moment Claude writes a steer note, so `wb steer --now`
    interrupts at once instead of at the loop's next timeout."""
    with Watcher(task.dir) as w:
        while proc.poll() is None:
            if any(p.name == "steer.jsonl" for p in w.wait(5)):
                q.put("")


def run_round(task: store.Task, n: int) -> dict:
    spec, st = task.spec, task.status
    prompt = task.round_dir(n).joinpath("prompt.md").read_text()
    cmd = opencode_bin() + ["run", "--standalone", "-m", spec["opencode_model"], "--auto",
                            "--format", "json", "--title", f"wb: {task.name}"]
    if st.get("session"):
        cmd += ["--session", st["session"]]
    note = skills_note(task, n)
    cmd.append(note + prompt)
    env = agent_env(task)
    log = open(task.dir / "runner.log", "a")
    proc = subprocess.Popen(cmd, cwd=task.worktree, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=log, text=True,
                            start_new_session=True)  # its own group, so a stop takes MCP too
    CURRENT["proc"] = proc
    q: queue.Queue = queue.Queue()
    threading.Thread(target=_reader, args=(proc.stdout, q), daemon=True).start()
    threading.Thread(target=_steer_bell, args=(task, proc, q), daemon=True).start()

    detector = StuckDetector(**spec.get("stuck", {}))
    wall, silence = float(spec.get("round_timeout", 2400)), float(spec.get("silence_timeout", 900))
    think = float(spec.get("think_timeout") or 0)
    nudged = False
    started = last_seen = time.time()
    out = {"exit": None, "stuck": None, "texts": [], "tools": {}, "errors": [],
           "tok_in": 0, "tok_out": 0, "interrupted": False}
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
            elif row["kind"] == "error":
                out["errors"].append(f"opencode: {row['text']}")
            task.event(row)
            why = detector.feed({**row, "sig": sig}) if sig else None
            if why:
                out["stuck"] = why
        asking = task.status.get("phase") == "asking"  # the agent is waiting on Claude: not silent
        if not out["stuck"] and now - started > wall:
            out["stuck"] = f"the round ran past its {int(wall // 60)}-minute limit"
        if not out["stuck"] and not asking and now - last_seen > silence:
            out["stuck"] = f"no output for {int(silence // 60)} minutes"
        # Generating with nothing to show past the budget: interrupt with a nudge (once a round;
        # the silence limit above still stops a model that never answers) and tell Claude.
        if think and not nudged and not out["stuck"] and not asking and now - last_seen > think:
            nudged = True
            mins = int((now - last_seen) // 60)
            text = NUDGE.format(mins=mins)
            store.append_jsonl(task.dir / "steer.jsonl", {"ts": store.now(), "text": text, "now": True})
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
        every = float(spec.get("checkin") or 0) * 60
        since = max(started, float(task.status.get("checkin_at") or 0))
        if every and now - since >= every and not asking:
            task.set_status(checkin_at=now)
            task.notify("checkin", checkin_text(task, n, out, since), round=n)
        if any(r.get("now") for r in pending_steer(task)):
            task.event({"ts": store.now(), "round": n, "kind": "interrupted",
                        "text": "Claude interrupted the agent"})
            out["interrupted"] = True
            _stop(proc)
            break
    out["exit"] = proc.wait()
    log.close()
    out["secs"] = round(time.time() - started, 1)
    return out


def run_tests(task: store.Task, n: int):
    cmd = task.spec.get("test_cmd")
    if not cmd:
        return None
    task.set_status(phase="testing", last=f"tests: {cmd}")
    try:
        r = subprocess.run(cmd, shell=True, cwd=task.worktree, capture_output=True, text=True,
                           env={**os.environ, **CLEAN_ENV},
                           timeout=int(task.spec.get("test_timeout", 900)))
        code, output = r.returncode, (r.stdout + r.stderr)
    except subprocess.TimeoutExpired as e:
        code, output = "timeout", f"{e.stdout or ''}{e.stderr or ''}\n[timed out]"
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
    if mine := claude_files_changed(files):
        lines.append(f"NOTE: the agent changed your files ({', '.join(mine)}): instructions and "
                     ".claude/ are yours to write; keep, rewrite or revert them")
    stat = gitops.diff(task.worktree, spec["base"], stat=True).splitlines()
    if stat:
        lines.append(stat[-1].strip())
    return "\n".join(lines)


# A round continues the task's OpenCode session, so the agent keeps what it has read. Once that
# session has filled this share of the model's window, or OpenCode has compacted it (a small
# model's own summary of a coding session drops details), the next round starts a fresh session
# that is handed exactly what it needs: the task, the work so far and the new instructions.
FRESH_AT = 0.6
ASKED_FRESH = "Claude asked for a fresh session"
# The handoff carries the diff while it is under this share of the window (at ~4 characters a
# token: 12K characters, ~3K tokens, on a 32K one); a bigger diff is listed, for the agent to read.
HANDOFF_DIFF_SHARE = 0.09


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
    if not st.get("session") or st.get("context") == "keep":
        return None
    if st.get("context") == "fresh":
        return ASKED_FRESH
    c = context_state(task)
    if c.get("compactions"):
        return "OpenCode had to compact the last session"
    if c.get("used") and c.get("window") and c["used"] >= FRESH_AT * c["window"]:
        return (f"the last session was {100 * c['used'] // c['window']}% full "
                f"({c['used'] / 1000:.1f}K of {c['window'] // 1024}K tokens)")
    return None


def handoff_prompt(task: store.Task, why: str, instructions: str) -> str:
    wt, spec = task.worktree, task.spec
    steps = gitops.git(wt, "log", "--oneline", f"{spec['base']}..HEAD").strip()
    gitops.git(wt, "add", "-A", "-N")
    try:
        diff = gitops.git(wt, "diff", "HEAD").rstrip()
        stat = gitops.git(wt, "diff", "HEAD", "--stat").rstrip()
    finally:
        gitops.git(wt, "reset", "-q")
    room = int(HANDOFF_DIFF_SHARE * (task.status.get("window") or 32768) * 4)
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
    n = task.status.get("step_round") or 1
    return task.round_dir(n).joinpath("prompt.md").read_text().strip()


def _start_fresh(task: store.Task, n: int, why: str) -> None:
    path = task.round_dir(n).joinpath("prompt.md")
    now = ("Start on the task above." if n == (task.status.get("step_round") or 1)
           else path.read_text().strip())
    path.write_text(handoff_prompt(task, why, now))
    st = task.status
    task.set_status(session=None, sessions=(st.get("sessions") or []) + [st["session"]],
                    context=None, skills_told=None)  # a new session hears the skills again
    task.event({"ts": store.now(), "round": n, "kind": "fresh", "overflow": why != ASKED_FRESH,
                "text": f"a fresh session for the agent: {why}"})


def auto_fix_prompt(tests: dict, cmd: str) -> str:
    return (f"The acceptance tests fail. Command: `{cmd}` (exit {tests['exit']}). Output:\n\n"
            f"```\n{tests['tail']}\n```\n\nFix the code so the tests pass. Do not weaken, skip or "
            "delete tests to make them pass. Do not commit. Finish with a short summary of what "
            "you changed and the test result.")


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
            task.set_status(load_secs=round(secs, 1),
                            window=(m.config.get("limit") or {}).get("context"))
        except (KeyError, RuntimeError, OSError) as e:
            return _load_failed(task, e)
        while True:
            n = task.status["round"]
            why = fresh_reason(task) if n > 1 else None
            if why:
                _start_fresh(task, n, why)
            elif task.status.get("context") == "keep":
                task.set_status(context=None)  # a one-round instruction
            task.set_status(phase="running", round=n, round_started_at=store.now(),
                            last="agent started")  # not "starting…": the pane shows its progress
            task.event({"ts": store.now(), "round": n, "kind": "round", "text": "started"})
            rnd = run_round(task, n)
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
                    "text": "stopped by the reviewer"})
    except Exception:
        err = traceback.format_exc()
        (task.dir / "runner.log").open("a").write(err)
        task.set_status(phase="failed", last=err.strip().splitlines()[-1][:200])
        task.notify("failed", f"task {task.name}: the runner crashed\n{err[-1500:]}")
    finally:
        task.set_status(pid=None)


if __name__ == "__main__":
    main()
