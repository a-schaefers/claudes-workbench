"""The detached process that drives one task's rounds until it needs Claude.

    python3 -m workbench.runner <task>

Each round sends rounds/<n>/prompt.md to the agent (`opencode run`, continuing the task's session
after the first, unless that session is full: see fresh_reason), with the skills note
(skills_note) before it, streams its events into events.jsonl and status.json, and stops it if it
loops, errors repeatedly, goes silent or runs too long. After a clean round the task's test
command runs in the worktree, in the agent's lane; a failure goes back to the agent on its own while auto-fix rounds remain, so
Claude is only woken for work that passes, or for a task that has run out of road.
"""
import json
import os
import queue
import re
import select
import shlex
import signal
import subprocess
import sys
import threading
import time
import traceback
from collections import deque
from pathlib import Path

from . import ask_mcp, context, gitguard, gitops, hardware, lane, lessons, models, skills, store, tuning
from .events import StuckDetector, normalize
from .watch import Watcher

PKG_ROOT = Path(__file__).resolve().parent.parent
CURRENT: dict = {}  # the agent process of the round in flight, for the SIGTERM handler
# Keep build droppings out of the worktree, so the review diff is only the agent's work.
CLEAN_ENV = {"PYTHONDONTWRITEBYTECODE": "1"}


class Stopped(BaseException):  # not an Exception: no `except Exception` on the way may swallow a stop
    pass


def _on_term(signum, frame):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)  # one stop is enough: a second (systemd sends
    proc = CURRENT.get("proc")                      # more) must not cut the cleanup short
    if proc and proc.poll() is None:
        _stop(proc)  # the agent (or the tests) runs in its own process group; take it down with us
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
        "share": "disabled",  # a user's `share: "auto"` would publish every agent session
        **({"compaction": {"auto": True, "buffer": buffer}} if buffer else {}),
        "mcp": {"servers": {**users_servers_off(), "workbench": {  # 2.x shape: mcp.servers.<name>
            "type": "local",
            "command": [sys.executable, str(PKG_ROOT / "bin" / "wb-ask-mcp")],
            "environment": {"WB_TASK": task.name, "WB_HOME": str(store.home())},
            "codemode": False,  # a plain tool the model sees, not one behind code mode
            # the tool blocks until Claude answers; OpenCode must not give up on it first
            "timeout": {"startup": 15000, "execution": wait_ms},
        }}},
    })
    return path


def users_servers_off() -> dict:
    """The user's own MCP servers (their global OpenCode config), each replaced by one that
    can't run: OpenCode would start them for the agent and connect to a remote one with the
    user's token (the 2026-11-16 audit). OpenCode 2.3 starts a server whose entry says
    `"enabled": false` all the same, so the command itself goes. Their tools are denied besides
    (context.MCP_RULES)."""
    try:
        servers = (models.read_config().get("mcp") or {}).get("servers") or {}
    except (OSError, ValueError, AttributeError):
        return {}
    return {name: {"type": "local", "command": ["false"], "enabled": False}
            for name in servers if name != "workbench"}


# The rules again, as OPENCODE_CONFIG_CONTENT, which OpenCode loads after every config file: a
# project's opencode.json (the worktree's, or one in a directory above it) loads after the layer
# and its permissions come later, so `{"action": "shell", "resource": "*", "effect": "allow"}`
# there lifted every deny (the 2026-11-16 audit). OPENCODE_DISABLE_PROJECT_CONFIG would shut it
# out too, but it drops AGENTS.md and the project's skills with it, so it is only the fallback for
# rules too long for one environment string (MAX_ARG_STRLEN, 128 KiB).
CONFIG_CONTENT_MAX = 120_000


def rules_env(layer: dict) -> dict:
    content = json.dumps({"permissions": layer["permissions"], "share": "disabled"},
                         separators=(",", ":"))
    if len(content.encode()) <= CONFIG_CONTENT_MAX:
        return {"OPENCODE_CONFIG_CONTENT": content}
    return {"OPENCODE_DISABLE_PROJECT_CONFIG": "1"}


# What OpenCode's own environment adds to the agent's: npx and `npm exec` run only what the
# project installed, instead of fetching a package unasked (npm says yes by itself without a
# terminal; the 2026-11-16 audit). Not the tests' environment: they run as they would for you.
OPENCODE_ENV = {"npm_config_yes": "false"}


# What the agent's environment leaves out of the runner's: credentials (API keys and tokens Claude
# Code or the user's shell carry, the SSH agent that would sign a push) and the runner's own
# PYTHONPATH, which would put `python3 -m workbench.cli` in the agent's reach. A provider that
# needs a key from the environment names it in commands.json `env`, which passes it through.
# The 2026-11-16 audit added the second line's names (SMTP_PASS, MYSQL_PWD, SENTRY_DSN, BW_SESSION
# an unlocked Bitwarden vault, OP_SESSION_<acct> 1Password's) and the value rule (USERINFO: a URL
# with a password, DATABASE_URL=postgres://u:p@h/db, whatever its name).
SECRET_ENV = re.compile(r"TOKEN|SECRET|PASSW|CREDENTIAL|API_?KEY|ACCESS_KEY|PRIVATE_KEY|"
                        r"_(PASS|PASSPHRASE|PWD|DSN|JWT|PAT|KEY)$|^(PASS|PASSPHRASE|KEY)$|"
                        r"(^|_)PASS_|WEBHOOK|^BW_SESSION$|^OP_SESSION_|^KRB5CCNAME$|"
                        r"^(ANTHROPIC|CLAUDE|GH|GITHUB|GITLAB|AWS|AZURE|GOOGLE|OPENAI|HF)_|"
                        r"^(SSH_AUTH_SOCK|SSH_AGENT_PID|GPG_AGENT_INFO|DBUS_SESSION_BUS_ADDRESS|"
                        r"DBUS_SYSTEM_BUS_ADDRESS|PYTHONPATH)$", re.I)
USERINFO = re.compile(r"\w+://[^/@\s:]*:[^/@\s]+@")
# Configuration, not credentials, under the prefixes above: a test that reads Hugging Face models
# offline from HF_HOME, or a region, behaves in the lane as it does for the user (the audit).
ENV_KEEP = frozenset({"HF_HOME", "HF_HUB_CACHE", "HF_HUB_OFFLINE", "HF_DATASETS_OFFLINE",
                      "HF_ENDPOINT", "HF_HUB_DISABLE_TELEMETRY", "TRANSFORMERS_OFFLINE",
                      "TOKENIZERS_PARALLELISM", "AWS_REGION", "AWS_DEFAULT_REGION",
                      "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_REGION"})


def agent_env(task: store.Task) -> dict:
    """The agent's environment: its config layer, and git read-only (gitguard.py)."""
    through = set(context.commands_conf().get("env", []))
    keep = {k: v for k, v in os.environ.items() if k in through or k in ENV_KEEP
            or not (SECRET_ENV.search(k) or USERINFO.search(v))}
    layer = write_config_layer(task)
    env = {**keep, "PWD": str(task.worktree),  # opencode finds the project from $PWD
           "OPENCODE_CONFIG": str(layer), **rules_env(json.loads(layer.read_text())),
           "WB_TASK": task.name, "WB_HOME": str(store.home()), **CLEAN_ENV}
    env["PATH"] = env.get("PATH") or os.defpath
    if context.writable(task) is not None:  # its own: the user's holds their bus and agents
        env["XDG_RUNTIME_DIR"] = str(context.runtime_dir(task))
        # On WSL, not the Windows PATH, whose programs run outside the lane (context.agent_path),
        # nor the interop socket's name (the 0.90 review; /init finds it without: interop_gap)
        env.pop("WSL_INTEROP", None)
        env["PATH"] = context.agent_path(env["PATH"])
    env.update(gitguard.install(context.rules_dir(task) / "bin", Path(task.spec["repo"]), PKG_ROOT,
                                env["PATH"]) or {})
    return env


def claude_files_changed(files: str) -> list[str]:
    """The instruction files, .claude/ paths and OpenCode project config among `git status
    --short` lines (a project opencode.json loads into the agent's next round)."""
    out = []
    for line in files.splitlines():
        path = line[3:].split(" -> ")[-1].strip('"')
        name = Path(path).name
        if name in ("CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", "opencode.json",
                    "opencode.jsonc") or path.startswith((".claude/", ".opencode/")) or \
                "/.claude/" in path or "/.opencode/" in path:
            out.append(path)
    return out


def _reader(stream, q):
    for line in stream:
        q.put(line)
    q.put(None)


EXIT_GRACE = 30  # seconds OpenCode has to exit once its output closes; past that, it's stopped


def _stop(proc: subprocess.Popen):
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=10)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _kill_group(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def sweep(task: store.Task) -> int:
    """Kill every process of ours the agent started for this task and left running: a server
    backgrounded with `&`, a `setsid`'d child, the agent itself after its runner died. They are
    found by the environment the runner gave them (WB_TASK and WB_HOME, agent_env), since
    OpenCode's shell starts each command in a group of its own. Linux only (/proc); elsewhere
    the process group kills are all there is. The 2026-11-16 audit: a leftover writer kept
    editing the worktree after Claude read the diff, and a crashed runner left its agent working."""
    proc = Path("/proc")
    if not proc.joinpath("self").exists():
        return 0
    want = {f"WB_TASK={task.name}".encode(), f"WB_HOME={store.home()}".encode()}
    me, uid, n = os.getpid(), os.getuid(), 0
    for d in proc.iterdir():
        if not d.name.isdigit() or int(d.name) == me:
            continue
        try:
            if d.stat().st_uid != uid or not want <= set(d.joinpath("environ").read_bytes().split(b"\0")):
                continue
            os.kill(int(d.name), signal.SIGKILL)
            n += 1
        except OSError:
            continue
    return n


def _end_agent(task: store.Task) -> None:
    """Nothing a round started outlives it, however the round ended (the 2026-11-16 audit: a
    crash in the runner left the agent editing the worktree after the GPU lease was released):
    the agent if it still runs, what it left in its group, and what it started outside it."""
    proc = CURRENT.pop("proc", None)
    if proc is not None:
        if proc.poll() is None:
            _stop(proc)
        _kill_group(proc.pid)
    sweep(task)
    try:
        if task.status.get("agent_pid"):
            task.set_status(agent_pid=None)
        ask_mcp.close_all(task)  # its questions died with it: a stop unwound past run_round's close
        st = task.status
        if st.get("asking") or st.get("phase") == "asking":
            task.set_status(asking=None, **({"phase": "running"} if st.get("phase") == "asking" else {}))
    except OSError:  # a full disk must not hide the error that brought us here
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
    here first (seen 2026-11-16: a flow-field module nobody asked for). Matched by path or name;
    a brace form names each (`src/replay_io.{h,c}`: flagged as unnamed on 2026-11-18)."""
    spec = current_task_prompt(task)
    spec += "\n" + " ".join(a + part + b for a, parts, b in
                             re.findall(r"([\w./-]*)\{([\w.-]+(?:,[\w.-]+)+)\}([\w./-]*)", spec)
                             for part in parts.split(","))
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
    interrupts at once instead of at the loop's next timeout, and when a question or its answer
    lands in ask/, so the runner relays it at once (ask_mcp.relay)."""
    ask = context.ask_dir(task)
    with Watcher(task.dir, ask) as w:
        while proc.poll() is None:
            if any(p.name == "steer.jsonl" or p.parent == ask and not p.name.startswith(".")
                   for p in w.wait(5)):
                q.put("")


# Linux takes at most 128 KiB in one argv string (MAX_ARG_STRLEN); a longer prompt is sent as a
# pointer to its file in the round's directory, which the agent can read.
PROMPT_ARG_MAX = 100_000


# The round's guards run on the monotonic clock, which a suspend doesn't advance and NTP never
# steps: on the wall clock a laptop shut for 20 minutes woke to a round stopped for silence (the
# 2026-11-16 audit). A loop pass far longer than its 5 s wait means the process was frozen (a
# host's sleep that the monotonic clock still counted, as WSL2's may): that time isn't the
# round's either. Check-ins stay on the wall clock: they compare with timestamps in files.
FROZEN_SECS = 60


class _Clock:
    """The round's time and its silence, less the waits that aren't the agent's: Claude's answer
    to a question, and a frozen machine."""

    def __init__(self, t: float):
        self.t0 = self.tick = self.seen = t
        self.paused = 0.0

    def step(self, t: float, asking: bool) -> None:
        dt, self.tick = t - self.tick, t
        if asking:  # waiting on Claude: neither the round's time nor silence
            self.paused += dt
            self.seen = t
        elif dt > FROZEN_SECS:
            self.paused += dt
            self.seen += dt

    def ran(self, t: float) -> float:
        return t - self.t0 - self.paused

    def quiet(self, t: float) -> float:
        return t - self.seen


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
    # After `--`: OpenCode reads a message that starts with '-' (a spec opening with a bullet or
    # front matter) as a flag, prints its usage and exits (the 2026-11-16 audit).
    cmd += ["--", note + prompt]
    env = {**agent_env(task), **OPENCODE_ENV}
    cmd = lane.wrap(context.writable(task), cmd,   # writes stay in the agent's lane (lane.py),
                    context.readable(task),       # and credentials out of its reach
                    context.unix_paths(task))
    log = open(task.dir / "runner.log", "a")
    proc = subprocess.Popen(cmd, cwd=task.worktree, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=log, text=True,
                            start_new_session=True)  # its own group, so a stop takes MCP too
    CURRENT["proc"] = proc
    task.set_status(agent_pid=proc.pid)  # its own session: wb stop ends it if the runner can't
    q: queue.Queue = queue.Queue()
    threading.Thread(target=_reader, args=(proc.stdout, q), daemon=True).start()
    threading.Thread(target=_steer_bell, args=(task, proc, q), daemon=True).start()
    power = None
    if not CURRENT.get("remote") and not models._fake():  # the suite's fakes: the GPU isn't theirs
        power = _Power()
        power.start()

    detector = StuckDetector(**spec.get("stuck", {}), worktree=task.worktree)
    wall, silence = float(spec.get("round_timeout", 2400)), float(spec.get("silence_timeout", 900))
    think = float(spec.get("think_timeout") or 0)
    # A shell command OpenCode moved to the background (past 2 minutes it does, and the turn
    # ends waiting on it) prints nothing until it is done: a long test suite counted as thinking,
    # was nudged and killed every 7 minutes, and rerun (the 2026-11-16 audit). While it waits
    # (`background_wait`), no nudge, and silence runs to the test timeout; the round's limit holds.
    wait_hush = max(silence, float(spec.get("test_timeout") or 900))
    nudged = False
    renudge = task.status.get("nudged_round") == n - 1  # the last round ended in our nudge
    edit_mark = gitops.mark(task.worktree)  # shell edits (sed -i, cat >) count as edits too
    started = time.time()  # the wall clock, for check-ins: they compare with timestamps in files
    clock = _Clock(time.monotonic())
    # The think budget starts at the round's first output: until then the model is reading its
    # prompt, which on the CPU or a model spilled into RAM takes many minutes, and a round nudged
    # for it was then stopped "even after a nudge" (the 2026-11-16 audit). Silence still applies.
    heard = False
    waiting_bg = False
    out = {"exit": None, "stuck": None, "texts": [], "tools": {}, "errors": [],
           "tok_in": 0, "tok_out": 0, "interrupted": False, "full": None}
    window = st.get("window")
    fresh_at = tuning.value(task, "fresh_at")
    opened: dict = {}  # the questions open this round (ask_mcp.relay)
    while True:
        try:  # woken by the agent's output or a steer note; the timeout runs the clocks below
            line = q.get(timeout=5)
        except queue.Empty:
            line = ""
        if line is None:
            break
        now, mono = time.time(), time.monotonic()
        ask_mcp.relay(task, opened)  # the agent's questions: checked, counted, raised
        asking = task.status.get("phase") == "asking"  # the agent is waiting on Claude: not silent
        clock.step(mono, asking)
        if line:
            clock.seen, heard = mono, True
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(raw, dict):  # a stray number or list on stdout isn't an event
                continue
            if raw.get("type") == "background_wait":
                waiting_bg = True
            if raw.get("sessionID") and not task.status.get("session"):
                task.set_status(session=raw["sessionID"])
            row = normalize(raw, n)
            if row is None:
                continue
            waiting_bg = False  # the session moved on
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
        quiet = clock.quiet(mono)
        hush = wait_hush if waiting_bg else silence
        if not out["stuck"] and clock.ran(mono) > wall:
            out["stuck"] = f"the round ran past its {int(wall // 60)}-minute limit"
        if not out["stuck"] and not asking and quiet > hush:
            out["stuck"] = (f"no output for {int(hush // 60)} minutes" if heard else
                            f"no first output in {int(hush // 60)} minutes: the model may be "
                            "reading its prompt slowly (on the CPU, or partly in RAM?)")
            if waiting_bg:
                out["stuck"] += ", waiting on a background job"
        # Generating with nothing to show past the budget: interrupt with a nudge (once a round)
        # and tell Claude. The nudge starts a new round, and that round stalling again is
        # stopped, whatever it did first: else a model that never answers, or a command that
        # prints nothing for longer than the budget, would be nudged and rerun forever.
        if (think and heard and not waiting_bg and not nudged and not out["stuck"] and not asking
                and quiet > think):
            mins = int(quiet // 60)
            if renudge:
                out["stuck"] = (f"no tool call or word for {mins} minutes, even after a nudge"
                                if not out["tools"] and not out["texts"] else
                                f"{mins} minutes again with no tool call, even after a nudge")
            else:
                nudged = True
                text = NUDGE.format(mins=mins)
                task.set_status(nudged_round=n)
                store.append_jsonl(task.dir / "steer.jsonl", {"ts": store.now(), "text": text,
                                                             "now": True, "by": "workbench"})
                task.event({"ts": store.now(), "round": n, "kind": "steer", "now": True,
                            "by": "workbench", "text": text})
                nudges = task.status.get("nudges", 0) + 1
                task.set_status(nudges=nudges)
                task.notify("checkin", checkin_text(task, n, out, max(started, float(
                    task.status.get("checkin_at") or 0))) + f"\nthe runner nudged it: {mins} min of "
                    f"thinking with no tool call (nudge {nudges} this task; budget {int(think)}s)",
                    round=n)
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
    # Its output closed: it exits now. One hung in its shutdown held the task and the GPU lease
    # for good (the 2026-11-18 audit, #83), so the silence limit still holds, at most EXIT_GRACE.
    grace = min(EXIT_GRACE, silence)
    try:
        out["exit"] = proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        log.write(f"workbench: opencode closed its output but was still running {grace:.0f}s "
                  "later: stopped\n")
        log.flush()
        _stop(proc)
        out["exit"] = proc.wait()
    log.close()
    ask_mcp.close(task, opened)  # its server is gone: closed, so nothing takes them for open
    if task.status.get("asking") or task.status.get("phase") == "asking":  # it ended
        task.set_status(asking=None, phase="running")  # mid-question: nothing reads that answer now
    if power:
        power.stop()
        power.join(timeout=1)
        task.set_status(gpu_wh=round((task.status.get("gpu_wh") or 0) + power.wh, 2))
    out["secs"] = round(time.monotonic() - clock.t0, 1)
    return out


# A test that floods its output (a print in a retry loop) must not fill the runner's memory, the
# disk or Claude's context (the 2026-11-16 audit): past TEST_OUTPUT_CAP bytes it is stopped, and
# only its first KEEP_HEAD and last KEEP_TAIL bytes are kept, for tests.txt and the tail.
TEST_OUTPUT_CAP = 20 << 20
KEEP_HEAD, KEEP_TAIL = 64 << 10, 256 << 10


class _Capture(threading.Thread):
    """Reads a test's output as it comes, keeping its head and tail and counting the rest. It
    never waits on the end of the output: a child that escaped the process group may hold the
    pipe open long after the test is done (a server started with setsid)."""

    def __init__(self, stream):
        super().__init__(daemon=True)
        self.fd, self.head, self.tail = stream.fileno(), bytearray(), deque()
        self.total, self.kept, self.done = 0, 0, threading.Event()

    def run(self):
        while not self.done.is_set():
            if not select.select([self.fd], [], [], 0.2)[0]:
                continue
            chunk = os.read(self.fd, 65536)
            if not chunk:
                return
            self.total += len(chunk)
            if len(self.head) < KEEP_HEAD:
                take = KEEP_HEAD - len(self.head)
                self.head += chunk[:take]
                chunk = chunk[take:]
            if chunk:
                self.tail.append(chunk)
                self.kept += len(chunk)
                while self.kept - len(self.tail[0]) >= KEEP_TAIL:
                    self.kept -= len(self.tail.popleft())

    def text(self) -> str:
        tail = b"".join(self.tail)[-KEEP_TAIL:]
        cut = self.total - len(self.head) - len(tail)
        return (bytes(self.head) + (f"\n[... {cut} bytes of output cut ...]\n".encode() if cut > 0 else b"")
                + tail).decode(errors="replace")


POWER_EVERY = 10  # seconds between GPU power samples: fine enough for a round, cheap to read


class _Power(threading.Thread):
    """Samples the GPU's power draw every POWER_EVERY seconds while OpenCode runs a round and
    integrates it into watt-hours (the tests after it aren't counted). A None reading adds nothing
    for that interval. It is the whole GPU's draw: tasks sharing the GPU each count all of it."""

    def __init__(self):
        super().__init__(daemon=True)
        self.done = threading.Event()
        self.wh = 0.0

    def run(self):
        last = time.time()  # the first sample is taken at once; it adds nothing yet
        watts = hardware.gpu_power_w()
        while not self.done.is_set():
            self.done.wait(POWER_EVERY)
            now = time.time()
            if watts is not None:
                self.wh += watts * (now - last) / 3600
            last = now
            watts = hardware.gpu_power_w()

    def stop(self):
        self.done.set()


def run_in_lane(task: store.Task, cmd: str, limit: int) -> tuple[int | str, str]:
    """Run `cmd` in the task's worktree, in the agent's lane and with its environment; the
    exit code ("timeout" past `limit` seconds, "output limit" past TEST_OUTPUT_CAP bytes) and the
    decoded output (its head and tail when long)."""
    # in the agent's lane and with its environment: the tests run the agent's code, and must
    # pass or fail as they did when the agent ran them
    argv = lane.wrap(context.writable(task), ["/bin/sh", "-c", cmd], context.readable(task),
                     context.unix_paths(task))
    proc = subprocess.Popen(argv, cwd=task.worktree, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, env=agent_env(task),
                            start_new_session=True)  # its own group: a timeout ends all of it
    prev = CURRENT.get("proc")
    CURRENT["proc"] = proc  # a stop (SIGTERM, _on_term) takes the tests down too
    out = _Capture(proc.stdout)
    out.start()
    code, why, end = None, "", time.monotonic() + limit
    try:
        while code is None:
            try:
                code = proc.wait(timeout=0.25)
            except subprocess.TimeoutExpired:
                if time.monotonic() > end:
                    code, why = "timeout", f"[timed out after {limit}s]"
                elif out.total > TEST_OUTPUT_CAP:
                    code, why = "output limit", f"[stopped: its output passed {TEST_OUTPUT_CAP >> 20} MB]"
    finally:
        _kill_group(proc.pid)  # a timeout's remains, or what it left running in its group
        proc.wait()
        sweep(task)            # and what left the group
        if prev is None:
            CURRENT.pop("proc", None)
        else:
            CURRENT["proc"] = prev
        out.join(2)            # what is still in the pipe
        out.done.set()
        out.join(1)
        proc.stdout.close()
    output = out.text()
    return code, output + (f"\n{why}" if why else "")


def clip_tail(output: str, lines: int, width: int = 300, total: int = 4000) -> str:
    """The end of a test's output for status, the report and the auto-fix prompt: the last
    `lines` lines, each cut to `width` characters, the whole to `total`. One multi-megabyte line
    (a big assertion diff, minified JS) once went whole into status.json and the notice (the
    2026-11-16 audit); the full output stays in tests.txt."""
    out = [ln if len(ln) <= width else ln[:width] + f" … ({len(ln) - width} more chars)"
           for ln in output.strip().splitlines()[-lines:]]
    s = "\n".join(out)
    return s if len(s) <= total else "… (earlier output cut)\n" + s[-total:]


def run_tests(task: store.Task, n: int):
    cmd = task.spec.get("test_cmd")
    if not cmd:
        return None
    task.set_status(phase="testing", last=f"tests: {cmd}")
    limit = int(task.spec.get("test_timeout") or 900)
    code, output = run_in_lane(task, cmd, limit)
    task.round_dir(n).joinpath("tests.txt").write_text(f"$ {cmd}\nexit: {code}\n\n{output}")
    result = {"ok": code == 0, "exit": code, "tail": clip_tail(output, 40), "round": n}
    task.event({"ts": store.now(), "round": n, "kind": "tests", "ok": result["ok"],
                "text": f"exit {code}"})
    return result


# The base check runs in the runner before round 1 (it once ran inside `wb start`, which a slow
# suite pushed past Claude's two-minute Bash limit: the 2026-11-16 audit). Past this, not checked.
BASE_CHECK_SECS = 120


def base_check(task: store.Task) -> dict | None:
    """The acceptance command run once on the base, before the agent starts: it should fail, and
    only for what the step changes, which Claude checks from the tail (the runner can't tell).
    Passing already, it can't tell when the step is done."""
    cmd = task.spec.get("test_cmd")
    if not cmd:
        return None
    limit = min(BASE_CHECK_SECS, int(task.spec.get("test_timeout") or 900))
    code, output = run_in_lane(task, cmd, limit)
    task.dir.joinpath("base-tests.txt").write_text(f"$ {cmd}\nexit: {code}\n\n{output}")
    return {"ok": code == 0, "exit": code, "tail": clip_tail(output, 8, total=1500), "limit": limit}


def base_check_line(r: dict, task: store.Task) -> str:
    """What the acceptance command did on the base, for Claude to judge (the first report)."""
    if r["exit"] == "timeout":
        limit = r.get("limit", BASE_CHECK_SECS)
        if limit < BASE_CHECK_SECS:  # --test-timeout set it: every round's tests stop there too
            return (f"warning: the acceptance command ran past --test-timeout ({limit}s) on the "
                    "base, so every round's tests will time out too: raise --test-timeout or "
                    "narrow the command (wb discard, then start again)")
        return f"note: the acceptance command ran past {limit}s on the base; not checked"
    if r["ok"]:
        # the usual case when the agent writes the step's tests: a warning that said to discard
        # and start again cost a whole restart in one task of two (the 2026-11-17 measurement)
        return ("note: the acceptance command already passes on the base, so only the tests this "
                "step adds can fail it: check the diff adds tests that fail without the change")
    tail = "\n".join("    " + line for line in r["tail"].splitlines())
    # an instruction, not a claim: the runner can't know why it fails (the 0.90 review: "as it
    # should, but only for what the step changes" read as checked, for a broken build too)
    return (f"on the base the acceptance command fails (exit {r['exit']}): check the tail shows it "
            f"failing only for what the step changes ({task.dir / 'base-tests.txt'}):\n{tail}")


# characters (~8K tokens): a diff this size comes with the review notice. Claude reads all of it
# anyway, and fetching it is a call over its whole session: at 12,000 both significant tasks of
# 2026-11-18 (13K and 15K characters) cost that extra call.
REVIEW_DIFF_MAX = 32000


# The instruction files an agent may propose a new version of (`<name>.proposed`, beside it): it
# can't edit them (context.CLAUDE_FILES), and Claude making each change itself was a call over its
# whole context on most tasks (2026-11-18). `wb accept --take-claude-md` takes them.
PROPOSABLE = ("CLAUDE.md", "AGENTS.md")


def proposals(worktree: Path) -> list[tuple[Path, Path]]:
    """(the instruction file, the agent's proposed new version) for each proposal in the
    worktree; a link is never one (it could point anywhere)."""
    out = []
    for name in PROPOSABLE:
        for prop in sorted(worktree.rglob(name + ".proposed")):
            if not prop.is_symlink() and prop.is_file() and ".git" not in prop.parts:
                out.append((prop.with_name(name), prop))
    return out


def proposal_diff(task: store.Task) -> str:
    import difflib
    parts = []
    for real, prop in proposals(task.worktree):
        old = real.read_text(errors="replace").splitlines(keepends=True) if real.is_file() and \
            not real.is_symlink() else []
        rel = real.relative_to(task.worktree)
        d = "".join(difflib.unified_diff(old, prop.read_text(errors="replace").splitlines(
            keepends=True), f"a/{rel}", f"b/{rel}"))
        parts.append(f"\n\nthe agent proposes this change to {rel} (wb accept --take-claude-md "
                     f"takes it; without it, accept drops it):\n{d or '(no change)'}")
    return "".join(parts)


def review_diff(task: store.Task) -> str:
    """The diff under review, for the review notice, when it is small; else its stat. Claude reads
    it anyway, and each call to fetch it re-reads the whole session: a wake that carries it saves
    the review one to three calls (the 2026-11-17 measurement). After Claude's feedback on the
    same step, only what changed since the last review notice: the wake after a feedback round
    carried the whole 14K-character diff again, which Claude already had (2026-11-18). Records
    the snapshot it showed (status `reviewed`)."""
    steps = task.status.get("steps", 1)
    base = "HEAD" if steps > 1 else task.spec["base"]
    last = task.status.get("reviewed") or {}
    since = last.get("tree") if last.get("step") == steps else None
    # Only the proposals accept settles (proposals()) are left out of the code's diff: every
    # `*.proposed` was, so an ordinary schema.proposed read as "no changes" and was committed
    # all the same (#79, the 2026-11-18 audit).
    props = [str(p.relative_to(task.worktree)) for _, p in proposals(task.worktree)]
    code = (".", *(f":(exclude,literal){p}" for p in props))
    try:
        task.set_status(reviewed={"step": steps, "tree": gitops.snapshot(task.worktree)})
        if since:
            full = gitops.diff(task.worktree, since, paths=code)
            same = not props or not gitops.diff(task.worktree, since,
                                                 paths=tuple(f":(literal){p}" for p in props))
            prop = ("\n\nthe agent's proposed instruction files: unchanged since your last review"
                    if same and proposals(task.worktree) else proposal_diff(task))
            if not full:
                return "\n\nthe diff since your last review: (no changes)" + prop
            if len(full) <= REVIEW_DIFF_MAX:
                return (f"\n\nthe diff since your last review (wb diff {task.name} for all of "
                        f"it):\n{full}" + prop)
            base = since
        full = gitops.diff(task.worktree, base, paths=code)
        prop = proposal_diff(task)
        if not full:
            return "\n\nthe diff: (no changes)" + prop
        if len(full) <= REVIEW_DIFF_MAX:
            return (f"\n\nthe diff (all of it; wb diff {task.name} again only after a change):"
                    f"\n{full}" + prop)
        return (f"\n\nthe diff is {len(full)} characters, too big to include: "
                f"its stat:\n{gitops.diff(task.worktree, base, stat=True)}" + prop)
    except (gitops.GitError, OSError):
        return ""


def _base_check(task: store.Task) -> None:
    """Before round 1, once: the base check, its result in status `base_check` and the first
    report, and a check-in at once when it says the command can't do its job."""
    task.set_status(last="checking the acceptance command on the base")
    r = base_check(task)
    task.set_status(base_check=r, last="waiting for the GPU")
    if r["exit"] == "timeout" and r["limit"] < BASE_CHECK_SECS:  # every round's tests will too
        task.notify("checkin", f"task {task.name}: {base_check_line(r, task)}", round=1)


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
    full = [e for e in store.read_jsonl(task.dir / "events.jsonl")[0]
            if e.get("kind") == "reading_full" and e.get("round") == n]
    if full:
        lines.append("reading: " + full[-1]["text"])
    if rnd["stuck"]:
        lines.append(f"STOPPED: {rnd['stuck']}")
    elif rnd["exit"] not in (0, None):
        lines.append(f"opencode exited {rnd['exit']} (see {task.dir / 'runner.log'})")
    if tests is not None:
        lines.append(f"tests: {'PASS' if tests['ok'] else 'FAIL'} (`{spec['test_cmd']}`, "
                     f"exit {tests['exit']})")
        if not tests["ok"]:
            lines.append("```\n" + "\n".join(tests["tail"].splitlines()[-25:]) + "\n```")
    if st.get("base_check") and not st.get("reviews") and st.get("steps", 1) == 1:
        lines.append("base check: " + base_check_line(st["base_check"], task))
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
    if nested := gitops.nested_repos(task.worktree):
        lines.append(gitops.nested_line(nested))
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
    # Not this round's prompt alone: what the step's rounds asked, back to the review feedback.
    # An auto-fix or a steer round after a feedback round, starting fresh, once carried only the
    # failing tests, and the fresh agent fixed them and reported done with the feedback never
    # made (the 2026-11-16 audit). A carry-on or a resume carries the asks already.
    if n == (task.status.get("step_round") or 1):
        now = "Start on the task above."
    elif raw.startswith((CARRY_ON[:60], RESUME[:60])):
        now = raw
    else:
        asks = round_asks(task, n)  # ends with this round's own: instructions.md is written
        now = raw if len(asks) < 2 else ASKED.format("\n\n".join(asks[:-1])).strip() + "\n\nNow:\n\n" + raw
    path.write_text(handoff_prompt(task, why, now))
    st = task.status
    task.set_status(session=None, sessions=(st.get("sessions") or []) + [st["session"]] * bool(st.get("session")),
                    context=None, skills_told=None)  # a new session hears the skills again
    task.event({"ts": store.now(), "round": n, "kind": "fresh", "overflow": why != ASKED_FRESH,
                "compacted": why == COMPACTED,
                "text": f"a fresh session for the agent: {why}"})


# The checks Claude's reviews kept making on 2026-11-17: a rule with no test, a test's expected
# value that passed by luck, an unchecked error path, small things (a float function on an int, a
# wrong comment). An error naming the wrong one of two inputs got past it twice (2026-11-18): the
# prompt now asks for that case by name. A rejection test that passed only because its file was
# also short, and an "exact bytes" test that never compared the whole file, cost a review round
# on 2026-11-18: point 1 asks for each. Its rerun's review round was a `static` in a file whose
# CLAUDE.md forbids globals and a proposed CLAUDE.md line describing the wrong flag: point 4 asks
# for the project's rules and for docs that say what the code does. The third run's agent was
# asked point 1 and called its count test fine; it wasn't (a short file, rejected for that): so
# the rejections are now checked by doing it, removing each check and watching a test fail. The
# next task's agent "restored" a check by retyping it and rebuilt the loop wrong, then spent
# its round debugging its own restore (2026-11-18): it copies the file back instead. One
# automatic round, before Claude is woken, costs local time only.
SELF_REVIEW = (
    "Before Claude reviews this, review your own change as a strict senior reviewer would: `git "
    "diff`, and `git status` for new files. Check each point, and fix what fails:\n"
    "1. Every rule in the task (each must, only, at most, rejects, limit and error message) has a "
    "test that fails if that rule alone breaks. A rule without one: write the test. Check each "
    "rejection and limit by doing it, one at a time: copy the file aside (`cp f.c /tmp/f.c`), "
    "comment out its check, run the acceptance command, see a test fail, then copy the file back "
    "(never retype it), and run the command again to see it pass. No test failed: the "
    "test's input breaks another rule too; make it valid in every other way. Each test the task "
    "names checks what it says (exact output: the whole of it, length too, against a literal "
    "written out by hand).\n"
    "2. Every expected value in the tests: work it out by hand from the rule, not from what your "
    "code prints. A test that passes only because of the values it happens to try is a bug.\n"
    "3. Every error path: each write, read, allocation and parse that can fail is checked and "
    "handled as the task says. Each error message is tested once for every input that can cause "
    "it (with two inputs, the first valid and the second bad too), checking the exact text, which "
    "names the input that failed.\n"
    "4. The project's rules (CLAUDE.md, AGENTS.md) hold for every line you changed. Nothing the "
    "task didn't ask for; no dead code, debug output, unused names or wrong comments; each type "
    "used as declared; every doc line you wrote (a proposed CLAUDE.md too) says what the code "
    "does.\n"
    "Then run the acceptance command (`{cmd}`). Do not commit. Finish with a short summary: what "
    "you found and fixed, and for each rule the test that covers it (and, for each rejection, "
    "that removing its check failed it).")


# An agent whose change compared the wrong way round spent 40 minutes sure the compiler was
# broken, compiling test programs in /tmp, until Claude pointed at its loop (2026-11-18). A test
# it wrote this task can be the wrong half too, so it checks that one by hand first.
def auto_fix_prompt(tests: dict, cmd: str) -> str:
    return (f"The acceptance tests fail. Command: `{cmd}` (exit {tests['exit']}). Output:\n\n"
            f"```\n{tests['tail']}\n```\n\nFix the code so the tests pass. When a failing check is "
            "one you wrote for this task, first work its expected value out by hand from the task: "
            "if the test was wrong, correct it (that isn't weakening it). The compiler, libraries "
            "and system work; the bug is in this change. Do not weaken, skip or delete tests to "
            "make them pass. Do not commit. Finish with a short summary of what you changed and "
            "the test result.")


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
    CURRENT["remote"] = m.remote  # the GPU is not ours to measure when the model is remote
    st = task.status
    if (task.spec.get("test_cmd") and not task.spec.get("no_base_check")
            and "base_check" not in st and st.get("round", 1) == 1):
        _base_check(task)  # on the untouched base, before the agent and the GPU
    # beside the running tasks' models when all fit the GPU, else alone once they finish
    with models.GpuLease(m, task.name, say) as lease:
        try:
            secs = lease.load()
            window = (m.config.get("limit") or {}).get("context")
            task.set_status(load_secs=round(secs, 1), window=window)
            own = task.spec.get("self_review")  # --self-review / --no-self-review, else learned
            tuning.tune(task, task.spec["model"], window, {"fresh_at": fresh_ceiling(m.config)},
                        {} if own is None else {"self_review": int(own)})
        except (KeyError, RuntimeError, OSError) as e:
            return _load_failed(task, e)
        handed = 0  # mid-round handovers in this runner: one review round (HANDOVER_MAX)
        first = True
        while True:
            if not first:
                # Again before every round: each request resets Ollama's keep-alive to its own
                # default (5 minutes), so after a longer test run the model was unloaded and the
                # auto-fix round's think budget paid for loading it (the 2026-11-16 audit).
                # Resident already, this is one look at the server.
                try:
                    if (again := lease.load()) > 0:
                        task.set_status(load_secs=round(task.status.get("load_secs", 0) + again, 1))
                        task.event({"ts": store.now(), "round": task.status["round"], "kind": "load",
                                    "text": f"model reloaded ({again:.0f}s)"})
                except (KeyError, RuntimeError, OSError) as e:
                    return _load_failed(task, e)
            first = False
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
            try:
                rnd = run_round(task, n, keep=keep)
            finally:  # before the lease is released: the GPU's next task must not meet our agent
                _end_agent(task)
            measure_speed(task, began)
            if rnd["full"]:  # before notes: the handover carries them (_full_round)
                handed += 1
                if handed <= HANDOVER_MAX:
                    _full_round(task, n, rnd, handed)
                    continue
                rnd["stuck"] = (f"handed over to a fresh session {HANDOVER_MAX} times in this "
                                "round without finishing: the step is too big for this model's "
                                "window; split it (wb next) or take it over")
            if rnd["interrupted"] or (pending_steer(task) and not rnd["stuck"]):
                _steer_round(task, n, rnd["interrupted"])
                continue
            if rnd["stuck"] or (rnd["exit"] not in (0, None) and not rnd["texts"]):
                text = summary_text(task, n, rnd, None) + _undelivered(task)
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
            if pending_steer(task):  # a note that came in while the report was written
                _steer_round(task, n, False)
                continue
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
            mark = [task.status.get("steps", 1), task.status.get("reviews", 0)]
            if tuning.value(task, "self_review") and tests and tests["ok"] \
                    and task.status.get("self_reviewed") != mark:
                task.round_dir(n + 1).mkdir(parents=True, exist_ok=True)
                task.round_dir(n + 1).joinpath("prompt.md").write_text(
                    SELF_REVIEW.format(cmd=task.spec["test_cmd"]))
                task.set_status(round=n + 1, self_reviewed=mark, tests=tests)
                task.event({"ts": store.now(), "round": n, "kind": "self_review",
                            "text": "tests pass; the agent reviews its own change first"})
                continue
            task.set_status(phase="review", tests=tests,
                            last="tests passed: Claude reviews next" if tests and tests["ok"]
                            else "Claude reviews next")
            task.notify("review", text + review_diff(task), round=n)
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
    # Not a wake: the advice is for the next spec, which Claude reads in the round's report.
    # As a check-in it woke Claude mid-round with nothing to do, every round of a step, and then
    # once even on a trivial task (2026-11-17)
    task.event({"ts": store.now(), "round": n, "kind": "reading_full", "text": text})


# Mid-round handovers in one review round before it is stopped as stuck. A session that changes
# something early and then fills up reading handed over again and again, for hours, with Claude
# told nothing (the 2026-11-16 audit). A big step needs a few; past this it is too big for the
# window. The one before the last is a check-in, so Claude can steer or stop it; each before it
# woke Claude to read "on course" and wait again (2026-11-17), and the last is a stuck report.
HANDOVER_MAX = 5


def _full_round(task: store.Task, n: int, rnd: dict, handed: int = 1) -> None:
    window = task.status["window"]
    pct = 100 * rnd["full"] // window
    text = (f"the agent's session reached {pct}% of its window "
            f"({rnd['full'] / 1000:.1f}K of {window // 1024}K): a fresh one takes over")
    task.event({"ts": store.now(), "round": n, "kind": "full", "text": text})
    if handed == HANDOVER_MAX - 1:
        files = gitops.changed(task.worktree)
        task.notify("checkin", f"task {task.name}: {text} (handover {handed} this round; "
                    f"stopped as stuck past {HANDOVER_MAX}); files changed so far: "
                    + (", ".join(f[3:] for f in files.splitlines()) if files else "none"), round=n)
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


def _undelivered(task: store.Task) -> str:
    """Claude's notes a stopped round never delivered, for its report (the runner's own nudges
    aside): wb feedback and wb next drop them, wb resume carries them (cli._stale_notes)."""
    notes = [r for r in pending_steer(task) if r.get("by") != "workbench"]
    return ("\nNOTE: not delivered, the round stopped first: "
            + " | ".join(r["text"].strip().replace("\n", " ")[:200] for r in notes)) if notes else ""


def _steer_round(task: store.Task, n: int, interrupted: bool) -> None:
    notes = pending_steer(task)
    task.round_dir(n + 1).mkdir(parents=True, exist_ok=True)
    task.round_dir(n + 1).joinpath("prompt.md").write_text(steer_prompt(notes, interrupted))
    task.set_status(round=n + 1, steer_taken=task.status.get("steer_taken", 0) + len(notes))


# A SIGTERM that didn't come from wb (a shutdown, a logout, a kill): wb marks its own stops
# (status `stopping`, cli._stop_runner); any other leaves the task stuck, with a notice, so it
# doesn't sit in a live phase that nothing will ever end (the 2026-11-16 audit).
SIGNAL_STOP = "the runner was stopped by a signal (a shutdown, a logout or a kill), not by Claude"


def lock_file(name: str, kind: str):
    """Task `name`'s lock of `kind`, in $WB_HOME/locks/, which no lane reads (store.task_lock)."""
    return store.task_lock(name, kind)


def _one_runner(task: store.Task):
    """Hold the task's runner lock (lock_file) for the runner's life, or None when another
    runner holds it: two wb commands acting on one task at once once spawned two runners on one
    worktree and session (the 2026-11-16 audit). The holder's pid is in the file."""
    import fcntl
    path = lock_file(task.name, "runner")
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.seek(0)
        held = fh.read().strip()
        fh.close()
        if held.isdigit():  # spawn_runner wrote this one's pid: put the live runner's back
            task.set_status_if(lambda st: st.get("pid") == os.getpid(), pid=int(held))
        return None
    fh.truncate(0)
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


def main():
    store.absolute_env()
    task = store.Task(sys.argv[1])
    lock = _one_runner(task)  # noqa: F841 (held until the process exits)
    if lock is None:
        print(f"another runner drives {task.name}; this one exits", flush=True)
        return
    signal.signal(signal.SIGTERM, _on_term)
    try:
        drive(task)
    except Stopped:
        st = task.status
        if st.get("stopping") or st.get("phase") not in store.ACTIVE:
            task.event({"ts": store.now(), "round": st.get("round"), "kind": "stopped",
                        "by": "reviewer", "text": "stopped by the reviewer"})
        else:
            task.event({"ts": store.now(), "round": st.get("round"), "kind": "stopped",
                        "by": "signal", "text": SIGNAL_STOP})
            task.set_status(phase="stuck", last=SIGNAL_STOP)
            task.notify("stuck", f"task {task.name}: {SIGNAL_STOP}. wb resume {task.name} "
                        f"continues it in a fresh session, wb discard {task.name} drops it",
                        round=st.get("round"))
    except Exception:
        err = traceback.format_exc()
        (task.dir / "runner.log").open("a").write(err)
        task.set_status(phase="failed", last=err.strip().splitlines()[-1][:200])
        task.notify("failed", f"task {task.name}: the runner crashed\n{err[-1500:]}")
    finally:
        try:
            _end_agent(task)  # a backstop: drive's own cleanup may not have been reached
        finally:
            task.set_status(pid=None, stopping=None)


if __name__ == "__main__":
    main()
