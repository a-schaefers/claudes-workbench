"""Task state on disk: one directory per task, JSON written atomically, JSONL appended under a lock.

Layout of ``$WB_HOME`` (default ``~/.local/state/workbench``)::

    notify.jsonl            events for Claude (review, stuck, failed, question, checkin), all tasks
    results.jsonl           one row per finished task (accepted / discarded), for `wb stats`;
                            a trial run's (`wb start --trial`, `wb trial`) is kept but not evidence
    gpu.lock                held by every running task: shared beside others, exclusive alone
    gpu.json                who holds it (pid → model, task); gpu.admit serializes admissions
                            (both in models.lease_dir(): elsewhere when the machine's accounts share it)
    rules/<name>/           the agent's rules, kept outside its lane (context.rules_dir):
                            opencode.json (the config layer), HANDBOOK.md, CLAUDE.md, bin/git,
                            grants.json
    locks/<name>.<kind>.lock  the task's locks (task_lock): runner, cmd, status, events, steer, grants
    archive/<name>/         a task closed for ARCHIVE_DAYS, moved out of tasks/ (archive); Task
                            still finds it there, all_tasks no longer reads it
    notify.old.jsonl        notices handled and older than ARCHIVE_DAYS (cli.gc)
    tasks/<name>/
        task.json           what the task is (static after start, but for `wb next --test`)
        status.json         what it is doing now (rewritten by the runner and wb)
        events.jsonl        the agent's normalized events, every round
        steer.jsonl, acked, context.md
        runner.log          the runner's own stdout/stderr
        base-tests.txt      the base check: the acceptance command on the base, before round 1
        rounds/<n>/prompt.md, instructions.md, summary.md, final.md, tests.txt
        ask/<id>.json, ask/<id>.answer   the agent's questions and Claude's answers
        worktree/           the git worktree the agent works in (branch delegate/<name>)
    Of a task's directory the agent's lane writes only worktree/ and ask/ (context.writable).

Every file and format: ARCHITECTURE.md, *Files and formats*.
"""
import errno
import fcntl
import functools
import hashlib
import json
import math
import os
import re
import stat
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

# \Z, not $: `$` matches before a final newline, so `wb start 'foo\n'` passed the check (#782)
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}\Z")

# Phases a task moves through. "review", "stuck", "failed" and "asking" need Claude.
ACTIVE = {"queued", "running", "testing", "asking"}
NEEDS_CLAUDE = {"review", "stuck", "failed", "asking"}
CLOSED = {"accepted", "discarded"}
# a run item's notices that still go to Claude (#1278): an ask_reviewer question needs an answer
# only Claude has, and a check-in is the user's own request
RUN_GLOBAL = ("question", "checkin")


def parked(st: dict) -> bool:
    """A task `wb stop --all` (the mod's /workbench-quit) stopped and nothing has picked up since:
    the status line, the session's open-tasks block and `wb status` leave it out, `wb status
    --all` lists it. It stayed "awaiting review" and each new conversation was told to pick it up,
    against the quit (#541). A task waiting on Claude when the quit came (review, stuck, failed:
    `--park-waiting`) is parked as it stands: only the running ones were, and a review stayed in
    the status line and every new conversation's pick-up block (#698). spawn_runner and a takeover
    clear the mark; any other phase isn't parked."""
    return st.get("phase") in PARKS and bool(st.get("parked"))


PARKS = ("review", "stuck", "failed")  # the phases a parked mark counts in (parked)


# The environment variables that name a path. A relative one, or one with a `~` no shell expanded
# (a systemd unit, settings.json's env), meant one place to wb and another to the runner, which
# runs from the package's directory: the task was written under the cwd and looked for under the
# clone (the 2026-11-16 audit). They are made absolute against wb's cwd, once, before anything
# reads them or a child inherits them (absolute_env, from cli.main and the runner). Not resolved:
# a symlinked home stays the path the user named, the one the lane and OpenCode see.
# OPENCODE_CONFIG too: relative, wb start found its models, and the runner, started in the
# package directory, found none and failed the task with "unknown model" (#328, the audit of 1.4);
# and CLAUDE_CONFIG_DIR, which the runner reads for the lane and spend.py for the transcripts.
PATH_VARS = ("WB_HOME", "WB_MODELS", "WB_SKILLS", "WB_IMAGES", "WB_AUDIO", "WB_COMMANDS", "WB_CHAT_TOKEN",
             "WB_CHAT_CONFIG", "WB_OPENCODE_CONFIG", "WB_SAVINGS", "OPENCODE_CONFIG",
             "CLAUDE_CONFIG_DIR", "WB_GPU_DIR",
             # wb patch init's clone (#1005)
             "WB_SOURCE",
             # the roles (#1014), the user's file for agents (#1102)
             "WB_ROUTES", "WB_AGENTS_MD",
             # deep workflows' flows and role cards (#1278): a run's driver starts items elsewhere
             "WB_FLOWS", "WB_ROLES",
             # where OpenCode's global config (and, XDG_DATA_HOME, its sessions) are: relative,
             # wb start found the model and the runner, which runs elsewhere, didn't (#650)
             "OPENCODE_CONFIG_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME")


def absolute_env() -> None:
    for var in PATH_VARS:
        if os.environ.get(var):
            os.environ[var] = os.path.abspath(os.path.expanduser(os.environ[var]))


def home() -> Path:
    v = os.environ.get("WB_HOME")
    return Path(os.path.abspath(os.path.expanduser(v))) if v else Path.home() / ".local/state/workbench"


# Nothing watched the disk: each task holds a checkout, its --copy paths and the setup's output,
# and once it filled, every status write raised, the crash handler's own writes too, and wb
# status, wb wait and the mod's poll ended in a traceback (#342, the audit of 1.4)
FULL = (errno.ENOSPC, errno.EDQUOT)


def disk_full(e: BaseException) -> bool:
    return isinstance(e, OSError) and e.errno in FULL


def free_bytes(path: Path | None = None) -> int | None:
    """Free space where the state directory lives (or ``path``), for the user; None if unknown.
    On macOS the larger of statvfs's and what the volume has for important use (_important_free),
    which counts the purgeable space macOS frees when asked. On WSL2 at most the Windows drive's
    behind the distro's virtual disk (host_drive)."""
    p = Path(path or home())
    while not p.exists() and p != p.parent:
        p = p.parent
    try:
        st = os.statvfs(p)
    except OSError:
        return None
    free = max(st.f_bavail * st.f_frsize, _important_free(p) or 0)
    if (drive := host_drive(p)) is not None:
        try:
            st = os.statvfs(drive)
            free = min(free, st.f_bavail * st.f_frsize)
        except OSError:
            pass
    return free


def host_drive(path: Path) -> str | None:
    """On WSL, for a ``path`` in the distro's own file system, the Windows drive's mount that
    holds its virtual disk: the system drive's (/mnt/c, where WSL puts ext4.vhdx unless it was
    moved), else the first drive's. statvfs there says up to 1 TB free, the virtual disk's
    ceiling, whatever the Windows drive has: on a full C: the checkout grew the disk until
    Windows refused, and WSL saw EIO and a read-only remount, never ENOSPC (#897). None elsewhere,
    for a path on a Windows drive (its own statvfs is that drive's), or with no drive mounted."""
    from . import context, hardware  # both import this module
    if hardware.system() != "wsl":
        return None
    drives = context.windows_drives() or []
    if not drives or context.on_windows(str(path), drives):
        return None
    return next((d for d in drives if os.path.basename(d).lower() == "c"), drives[0])


def _important_free(path: Path) -> int | None:
    """The volume's available capacity for important usage (CoreFoundation's
    kCFURLVolumeAvailableCapacityForImportantUsageKey, what Finder shows as Available), macOS only;
    None elsewhere or when it can't say. On APFS statvfs leaves out purgeable space (local Time
    Machine snapshots, iCloud Drive's optimized files, caches), which macOS frees under pressure: a
    Mac with 1.5 GB by statvfs and 30 GB in Finder refused every wb start, and a task admitted
    failed at its checkout (#852)."""
    if sys.platform != "darwin":
        return None
    try:
        import ctypes
        cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        key = ctypes.c_void_p.in_dll(cf, "kCFURLVolumeAvailableCapacityForImportantUsageKey")
        make = cf.CFURLCreateFromFileSystemRepresentation
        make.restype, make.argtypes = ctypes.c_void_p, [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_bool]
        copy = cf.CFURLCopyResourcePropertyForKey
        copy.restype = ctypes.c_bool
        copy.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
        number = cf.CFNumberGetValue
        number.restype, number.argtypes = ctypes.c_bool, [ctypes.c_void_p, ctypes.c_long, ctypes.c_void_p]
        cf.CFRelease.restype, cf.CFRelease.argtypes = None, [ctypes.c_void_p]
        raw = os.fsencode(path)
        url = make(None, raw, len(raw), True) if key.value else None
        if not url:
            return None
        try:
            val = ctypes.c_void_p()
            if not copy(url, key, ctypes.byref(val), None) or not val.value:
                return None
            try:
                out = ctypes.c_int64()
                ok = number(val, 4, ctypes.byref(out))  # kCFNumberSInt64Type
                return out.value if ok and out.value >= 0 else None
            finally:
                cf.CFRelease(val)
        finally:
            cf.CFRelease(url)
    except (ImportError, OSError, AttributeError, ValueError, TypeError):
        return None


def noexec(path: Path | None = None) -> bool:
    """Whether the state directory (or ``path``) is on a noexec mount (Linux's statvfs flag): the
    agent's git shim, the worktree's own ./gradlew, node_modules/.bin and .venv/bin and the scratch
    checkouts' built test binaries all live there, and each exec failed with EACCES that read as a
    lane or project failure, while wb doctor called the directory fine (#851)."""
    flag = getattr(os, "ST_NOEXEC", None)
    if flag is None:  # macOS's statvfs carries no such flag
        return False
    p = Path(path or home())
    while not p.exists() and p != p.parent:
        p = p.parent
    try:
        return bool(os.statvfs(p).f_flag & flag)
    except OSError:
        return False


def dir_size(path: Path, budget: float | None = None) -> tuple[int, bool]:
    """Bytes under ``path`` (links not followed, a hard-linked file once) and whether the count
    is whole: with a ``budget`` in seconds it stops there, a lower bound. A file is its own size:
    a 64 KB file given to --copy counted as nothing, the count called whole (#436)."""
    try:
        st = os.lstat(path)
    except OSError:
        return 0, True
    if not stat.S_ISDIR(st.st_mode):
        return st.st_blocks * 512, True
    total, seen, end = 0, set(), (time.monotonic() + budget) if budget else None
    stack = [str(path)]
    while stack:
        if end and time.monotonic() > end:
            return total, False
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    try:
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
                    elif (st.st_dev, st.st_ino) not in seen:
                        seen.add((st.st_dev, st.st_ino))
                        total += st.st_blocks * 512
        except OSError:
            continue
    return total, True


def gb(n: int) -> str:
    return f"{n / 1e9:.1f} GB"


def tasks_dir() -> Path:
    return home() / "tasks"


def archive_dir() -> Path:
    return home() / "archive"


def task_dir(name: str) -> Path:
    """tasks/<name>, or archive/<name> for a task archived since it closed (archive)."""
    d = tasks_dir() / name
    if not d.exists() and (old := archive_dir() / name).exists():
        return old
    return d


def now() -> float:
    return round(time.time(), 3)


def scrub(data):
    """``data`` with every lone surrogate in its strings (and keys) made U+FFFD. JSON may escape
    half an emoji (``"\\ud83d"``) and json.loads keeps it, but no UTF-8 file can hold it: one in
    a model's tool arguments made the runner's next status write raise UnicodeEncodeError and
    failed the task (#438). Paired halves stay the character they are."""
    if isinstance(data, str):
        try:
            data.encode("utf-8")  # the common case, at C speed
            return data
        except UnicodeEncodeError:
            return data.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
    if isinstance(data, dict):
        return {scrub(k): scrub(v) for k, v in data.items()}
    if isinstance(data, list):
        return [scrub(v) for v in data]
    return data


def loads(text):
    """json.loads for text from outside workbench (OpenCode's output, the agent's files), its
    strings scrubbed so they can be written back out."""
    return scrub(json.loads(text))


def note_claude_session() -> None:
    """Record the Claude Code session a `wb` call runs in under its process
    (claude-sessions/<CLAUDE_PID>: "<ts> <session id>" lines), once per id. /clear starts a new
    session id and transcript in the same process, and spend.usage read only the one `wb start`
    recorded: the review turns after a /clear went uncounted (#451). Each process's lines follow
    a "start <its start>" line (claude_word): a new process with an old one's pid (a container,
    a WSL restart) was handed the old one's sessions, and with them its tasks (#787)."""
    sid, pid = os.environ.get("CLAUDE_CODE_SESSION_ID", ""), os.environ.get("CLAUDE_PID", "")
    if not sid or not pid.isdigit() or not re.fullmatch(r"[\w-]{1,128}", sid):
        return
    from . import models  # here: models imports store
    start = claude_word(models._started(int(pid)))
    path = home() / "claude-sessions" / pid
    try:
        sections = _session_sections(path.read_text().split())
    except FileNotFoundError:
        sections = []
    last = sections[-1] if sections else (None, [])
    if (start is None or last[0] == start) and sid in (s for _, s in last[1]):
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write((f"start {start}\n" if start is not None and last[0] != start else "")
                + f"{now()} {sid}\n")


def claude_word(start) -> str | None:
    """A process start (models._started: /proc's clock ticks, or ps's `lstart` with its spaces)
    as one word, for claude-sessions' "start" lines."""
    return "_".join(str(start).split()) or None if start is not None else None


def _session_sections(words: list[str]) -> list[tuple[str | None, list[tuple[float, str]]]]:
    """claude-sessions' pairs, grouped under the "start" line before them (None: written before
    #787, with none)."""
    out: list = [(None, [])]
    for a, b in zip(words[::2], words[1::2]):
        if a == "start":
            out.append((b, []))
            continue
        try:
            out[-1][1].append((float(a), b))
        except ValueError:
            continue
    return out if out[0][1] else out[1:]


def note_task_session(name) -> None:
    """Record the Claude Code session a `wb` call naming a task runs in, in the task's own
    `claude-sessions` (`<ts> <session id>` lines, once per id): a task adopted by another
    process after a restart (its start process gone, cli._scope) was reviewed and accepted
    there, and spend read only the start process's sessions, so the reviews went uncounted
    (#719)."""
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if not isinstance(name, str) or not NAME_RE.match(name) or not re.fullmatch(r"[\w-]{1,128}", sid):
        return
    task = Task(name)
    if not task.exists:
        return
    path = task.dir / "claude-sessions"
    if sid in task_sessions(task):
        return
    with path.open("a") as f:
        f.write(f"{now()} {sid}\n")


def task_sessions(task) -> list[str]:
    """The session ids note_task_session recorded for the task, first seen first."""
    try:
        words = (task.dir / "claude-sessions").read_text().split()
    except OSError:
        return []
    out = []
    for sid in words[1::2]:
        if sid not in out:
            out.append(sid)
    return out


def claude_sessions(pid, since: float = 0, start=None) -> list[str]:
    """The session ids Claude Code process `pid` ran `wb` in from `since` on, first seen first.
    With ``start`` (its models._started, or task.json `claude_start`), only that process's: the
    file is keyed by pid alone, and a later process given the same pid had the earlier one's
    sessions, so its plain `wb status` listed and acted on that process's tasks as its own
    (#787). Lines from before the start lines count only in a file that has none."""
    try:
        sections = _session_sections((home() / "claude-sessions" / str(int(pid))).read_text().split())
    except (OSError, TypeError, ValueError):
        return []
    born = None
    if start is not None:
        word, born = claude_word(start), _start_epoch(start)
        # lines from before the start lines: this process's when written after it started, else
        # (its start unknown as a time) only in a file that has nothing newer
        sections = [s for s in sections if s[0] == word
                    or s[0] is None and (born is not None or len(sections) == 1)]
    out = []
    for word_, rows in sections:
        for ts, sid in rows:
            if ts >= since and sid not in out and (word_ is not None or born is None or ts >= born - 1):
                out.append(sid)
    return out


# The OpenCode driver (#1309, 2.2): the model with the final say, in the user's own OpenCode. Its
# identity is OPENCODE_SESSION_ID, which the workbench plugin's tools set on their `wb` child and
# OpenCode itself sets in every shell, the agents' own shells included: so WB_TASK outranks it
# (an agent is never a driver). No flag and no variable of the plugin's says "driver": a missing
# plugin can't open a gate, it can only fail to ask (#1296, the host-shape decision, 2026-11-25).
DRIVERS = "drivers"           # $WB_HOME/drivers/: the drivers' heartbeats
# One beat per OpenCode server, never one for them all: the background service, every
# `opencode --standalone` TUI and wb's own --standalone helpers (models._opencode_json,
# keyhole._opencode, the runner's export) each load the user's global plugin, and with one shared
# file any of them, beating its own pid and none of the service's sessions, made a live session
# read as gone: another session in its directory took its tasks, its notices and `wb adopt` (the
# #1309 review, 2026-11-25; #693/#696's kind). So `opencode-<machine_tag>-<pid>.json`, each naming
# the store it lists (`store`, the server's database: every session in it), and a task is gone
# only by a fresh beat of the store it recorded (task.json `driver.store`), never another's.
BEAT_RE = re.compile(r"opencode-([0-9a-f]{8})-([1-9][0-9]{0,9})\.json")
HEARTBEAT_FRESH = 120         # seconds: an older beat says nothing (the plugin beats every 30)
SESSION_RE = re.compile(r"[\w-]{1,128}")  # a session id a file may hold, as the Claude ids


def opencode_session() -> str:
    """The OpenCode session this `wb` runs for: OPENCODE_SESSION_ID, unless WB_TASK says this is
    an agent's call (OpenCode re-sets the id in the agent's own shells, #1310) or Claude Code's
    Bash runs it (CLAUDECODE, ranked first, exactly as before 2.2). "" for none or a malformed id."""
    if os.environ.get("CLAUDECODE") == "1" or os.environ.get("WB_TASK"):
        return ""
    sid = os.environ.get("OPENCODE_SESSION_ID", "")
    return sid if SESSION_RE.fullmatch(sid) else ""


def under_model() -> bool:
    """Whether a model's shell runs this `wb`, so a gated change (an API role, an API `--model`,
    `wb patch send`) needs the user's yes from the host's dialog: an agent's (WB_TASK, #1310),
    Claude Code's Bash (CLAUDECODE=1), or an OpenCode driver's (OPENCODE_SESSION_ID). Before #1309
    only CLAUDECODE counted, so an OpenCode driver's `wb route … --api` was taken for the user at
    a terminal: no dialog, `by: terminal`. Any id at all counts here, a malformed one too: the
    gate fails closed."""
    return caller_kind() != "terminal"


# Who runs a wb call, WB_TASK first (#1310, 2026-11-25): OpenCode 2.4.1's shell tool sets
# OPENCODE=1, AGENT=1 and OPENCODE_SESSION_ID on every command it runs, an agent's own included
# (core/src/tool/plugin/shell.ts), whatever runner.agent_env dropped (runner.DRIVER_ENV); and a
# Claude Code session's CLAUDECODE=1 reaches every agent it started. So no driver's mark proves a
# driver: a call with WB_TASK (which only agent_env sets) is an agent's, and an agent's is a
# model's at the gates. #1309's first rule read an agent's call as the terminal's (WB_TASK hid the
# session id and nothing else counted): on a task wb started from a terminal, `wb route … --api`
# from an unlaned agent's shell went through as the user's own (the #1310 review, 2026-11-25).
def caller_kind() -> str:
    """Who runs this `wb`, for the gates (under_model), their wording and a role's `by`/`gpu_by`:
    `agent` (WB_TASK set, whatever else is: ranked first), `claude` (CLAUDECODE=1), `opencode`
    (an OpenCode driver: OPENCODE_SESSION_ID set), or `terminal`."""
    if os.environ.get("WB_TASK"):
        return "agent"
    if os.environ.get("CLAUDECODE") == "1":
        return "claude"
    return "opencode" if os.environ.get("OPENCODE_SESSION_ID") else "terminal"


def beat_path(pid: int) -> Path:
    """The heartbeat of this machine's OpenCode server ``pid``, as the plugin's server part writes it."""
    return home() / DRIVERS / f"opencode-{machine_tag()}-{pid}.json"


def heartbeats() -> list[dict]:
    """This machine's OpenCode servers' heartbeats, `$WB_HOME/drivers/opencode-<machine_tag>-<pid>.json`,
    each as the plugin's server part writes its own: {tool: "opencode", pid, machine?, store?, ts,
    sessions: {<id>: {model?, variant?, dir?}}}, `pid` the file name's; with `store` (the server's
    database), `sessions` every session in it when the listing began, at `ts` (ARCHITECTURE.md,
    *The heartbeat*). A file of another machine's (its tag, or its `machine`) is left out: its
    pids aren't this one's to judge (#922), and so is one not of that shape. A file of this
    machine's whose server has exited is removed: nothing else does, and each of wb's own
    --standalone helpers, which load the global plugin, left one behind (the #1309 re-review)."""
    from . import models  # here: models imports store
    try:
        paths = sorted((home() / DRIVERS).iterdir())
    except OSError:
        return []
    out, tag, me = [], machine_tag(), machine()
    for p in paths:
        m = BEAT_RE.fullmatch(p.name)
        if not m or m.group(1) != tag:
            continue
        hb = read_json(p)
        mach = hb.get("machine") if isinstance(hb, dict) else None
        if isinstance(mach, str) and mach and mach != me:
            continue
        if not models._alive(int(m.group(2))):
            p.unlink(missing_ok=True)
            continue
        if not isinstance(hb, dict) or hb.get("tool") != "opencode" or not isinstance(hb.get("sessions"), dict):
            continue
        ts = hb.get("ts")
        if not isinstance(ts, (int, float)) or isinstance(ts, bool) or not math.isfinite(ts) \
                or hb.get("pid") != int(m.group(2)) or isinstance(hb.get("pid"), bool):
            continue
        out.append(hb)
    return out


def _beating(hb: dict) -> bool:
    """Whether a heartbeat's server still beats: fresh, and its pid running."""
    from . import models  # here: models imports store
    return time.time() - hb["ts"] <= HEARTBEAT_FRESH and models._alive(hb["pid"])


def beat_of(pid) -> dict | None:
    """The fresh beat of this machine's running OpenCode server ``pid``, or None: what the web
    chat's OpenCode listener (`chat.listener`) and anything else asking after one server read."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    return next((hb for hb in heartbeats() if hb["pid"] == pid and _beating(hb)), None)


def _store_of(hb_or_driver: dict) -> str:
    """The OpenCode store a beat lists or a task recorded (`store`: the server's database, an
    absolute path), as a real path so a link and its target are one store; "" for none."""
    st = hb_or_driver.get("store")
    return os.path.realpath(st) if isinstance(st, str) and os.path.isabs(st) else ""


def driver_server(session, driver=None) -> tuple[str, dict | None]:
    """Whether an OpenCode driver session lives, and the beat that says so: (`live`, a beat) when
    a fresh beat of a running server lists it, the server the task recorded (``driver``, task.json
    `driver`: `pid`) first, else the newest; else (`gone`, None) when a fresh beat of a running
    server of the store the task recorded (`store`) leaves the session out, its listing begun after
    the task last saw the session there (`seen`): deleted; else (`unknown`, None): no store
    recorded (no plugin beat at `wb start`), no fresh beat of that store (a laptop asleep, a
    stopped, restarting or busy service, a stalled timer), or the task another machine's (#922).
    Only positive evidence makes a task gone, as `cli._gone` never says gone while Claude Code's
    process lives: never another store's beat, a stale beat, or a server's exit, since a session
    outlives its server (the #1309 reviews, 2026-11-25: each read as gone, and another session
    nearby took its tasks, their notices and `wb adopt`; the third, a service restart whose new
    beat didn't list the session yet). Only `gone` hands a task to another session: unknown is
    not gone, as a Claude task with no pid isn't."""
    d = driver if isinstance(driver, dict) else {}
    on = d.get("machine")
    if not isinstance(session, str) or not session or isinstance(on, str) and on and on != machine():
        return "unknown", None
    beats = [hb for hb in heartbeats() if _beating(hb)]
    live = [hb for hb in beats if session in hb["sessions"]]
    if live:
        return "live", max(live, key=lambda hb: (hb["pid"] == d.get("pid"), hb["ts"]))
    st, seen = _store_of(d), d.get("seen")
    if st and isinstance(seen, (int, float)) and not isinstance(seen, bool) \
            and any(_store_of(hb) == st and hb["ts"] > seen for hb in beats):
        return "gone", None
    return "unknown", None


def driver_state(session, driver=None) -> str:
    """`live`, `gone` or `unknown`: driver_server's first value."""
    return driver_server(session, driver)[0]


def _lineage() -> list[int]:
    """This process and its ancestors, nearest first: /proc, else ps (macOS)."""
    out, pid = [], os.getpid()
    while pid > 1 and pid not in out and len(out) < 64:
        out.append(pid)
        try:
            pid = int(Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()[1])
        except (OSError, IndexError, ValueError):
            import subprocess
            try:
                pid = int(subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True,
                                         text=True, timeout=5).stdout.strip() or 0)
            except (OSError, ValueError, subprocess.SubprocessError):
                break
    return out


def driver_record(session: str) -> dict:
    """task.json `driver` for a task an OpenCode session starts or adopts: the tool, this machine,
    and the session's server: the OpenCode server this `wb` runs under (its tool's child, or its
    shell's), whose store holds the session whether or not its last listing caught it, `seen`
    now; else the running server whose beat lists the session, fresh first, a stale one too (a
    busy service: the #1309 re-review, its task otherwise unknown for good), `seen` that beat's
    `ts`. From that beat: `store`, `pid` (and `start` as wb reads it, models._started, never the
    plugin's: a stamp of another format would read as a recycled pid), the session's model and
    variant. No beat (no plugin): none of those, and driver_server says `unknown`. The session
    itself is task.json `session`, which every reader of a task's owner already reads, the Claude
    Code mod's `mine` included: a session it never had, with no `claude_pid` to die, so it never
    takes an OpenCode task."""
    from . import models  # here: models imports store
    beats = heartbeats()
    mine = {hb["pid"]: hb for hb in beats}
    hb = next((mine[p] for p in _lineage() if p in mine), None)
    seen = time.time()
    if hb is None:
        listed = [b for b in beats if session in b["sessions"]]
        hb = max(listed, key=lambda b: (_beating(b), b["ts"])) if listed else None
        seen = hb["ts"] if hb else seen
    if not hb:
        return {"tool": "opencode", "machine": machine()}
    row = hb["sessions"].get(session)
    row = row if isinstance(row, dict) else {}
    return {"tool": "opencode", "machine": machine(), "pid": hb["pid"],
            **({"start": st} if (st := models._started(hb["pid"])) else {}),
            **({"store": st, "seen": seen} if (st := _store_of(hb)) else {}),
            **{k: row[k] for k in ("model", "variant") if isinstance(row.get(k), str) and row[k]}}


def driver_fill(task) -> None:
    """Fill in an OpenCode task's server later (task.json `driver`: `pid`, `start`, `store`,
    `seen`) when it recorded no store, from the fresh beat that lists its session now: a task
    started or adopted with no beat to read (the plugin not yet loaded, a stalled service) read
    `unknown` for good, so no session could ever take it, its own deleted (the #1309 re-review).
    Under the task's command lock, never waited for: a command that holds it fills it next time."""
    from . import models  # here: models imports store
    spec = task.spec
    d = spec.get("driver")
    if driver_of(spec) != "opencode" or not isinstance(d, dict) or _store_of(d):
        return
    state, hb = driver_server(spec.get("session"), d)
    if state != "live" or not (st := _store_of(hb)):
        return
    try:
        with locked(task_lock(task.name, "cmd"), blocking=False):
            spec = dict(task.spec)
            if spec.get("driver") != d:
                return
            row = hb["sessions"].get(spec.get("session"))
            row = {k: row[k] for k in ("model", "variant")
                   if isinstance(row, dict) and isinstance(row.get(k), str) and row[k]}
            new = {k: v for k, v in d.items() if k != "start"}  # a start of another pid's misleads
            spec["driver"] = {**row, **new, "pid": hb["pid"], "store": st, "seen": hb["ts"],
                              **({"start": s} if (s := models._started(hb["pid"])) else {})}
            write_json(task.dir / "task.json", spec)
    except BlockingIOError:
        pass


# A local driver on the GPU (#1317): the runner's free_gpu unloads every model but the task's,
# those beside it and keep_loaded's, so a driver on this GPU was evicted at each round, and its
# next turn reloaded it and evicted the agent's. A session whose last model request (the beat's
# `used`, the plugin's `model.request` hook) is this recent counts as driving: Ollama keeps a
# model 5 minutes by default and wb's own loads 30, so an idle afternoon session doesn't pin VRAM.
DRIVER_HOLD = 1800  # seconds


def driver_models(session: str | None = None) -> set[str]:
    """The models this machine's live OpenCode sessions drive with (#1317): each session row of
    a fresh beat of a running server whose `used` (its last model request) is within DRIVER_HOLD,
    by its OpenCode id (`provider/model`, as discover() names a GPU one; an API one never
    matches a GPU model). models.keep_loaded adds them, so no coding round unloads them, and the
    GPU lease counts the memory of those resident (models.driver_holders). ``session``: that
    session's alone (wb start's note on a task that waits for its own driver's model)."""
    now, out = time.time(), set()
    for hb in heartbeats():
        if not _beating(hb):
            continue
        for sid, row in hb["sessions"].items():
            used = row.get("used") if isinstance(row, dict) else None
            if isinstance(used, (int, float)) and not isinstance(used, bool) and math.isfinite(used) \
                    and now - used <= DRIVER_HOLD and isinstance(row.get("model"), str) and row["model"] \
                    and session in (None, sid):
                out.add(row["model"])
    return out


def driver_of(spec: dict) -> str | None:
    """Which driver a task belongs to (task.json): `opencode` (its `driver.tool`), `claude` (a
    `session` and no `driver`: every task before 2.2's), or None (no session: by directory)."""
    d = spec.get("driver")
    if isinstance(d, dict) and d.get("tool") == "opencode":
        return "opencode"
    return "claude" if spec.get("session") else None


def _start_epoch(start) -> float | None:
    """A process start (models._started) as a Unix time: /proc's clock ticks after boot (btime in
    /proc/stat), or ps's `lstart`; None when it can't be read."""
    text = str(start).strip()
    if text.isdigit():
        try:
            btime = next(int(l.split()[1]) for l in Path("/proc/stat").read_text().splitlines()
                         if l.startswith("btime "))
            return btime + int(text) / os.sysconf("SC_CLK_TCK")
        except (OSError, StopIteration, ValueError, IndexError):
            return None
    try:
        return time.mktime(time.strptime(" ".join(text.split()), "%a %b %d %H:%M:%S %Y"))
    except (ValueError, OverflowError):
        return None


_BOOT: list = []


def mac_tool(name: str) -> str:
    """A macOS system tool by its absolute path (/usr/sbin's ioreg and sysctl), by name elsewhere.
    Every process on one Mac must read the same machine and boot: the launchd agent's PATH had no
    /usr/sbin, so `wb chat serve` took the host name for the machine while the mod's `wb events`
    found ioreg and sent the hardware UUID, and the server read each session's listener.json as
    another machine's claim, a closed session as listening (#1305, 2026-11-24)."""
    return f"/usr/sbin/{name}" if sys.platform == "darwin" else name


def boot_id() -> str | None:
    """This boot of the machine: Linux's (and WSL2's VM's) random boot_id, macOS's boot time; None
    where neither is known. The runner records it (status `boot`): a runner recorded under another
    boot was ended by a restart, which kills it without a word (#544)."""
    if not _BOOT:
        boot = None
        try:
            boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip() or None
        except OSError:
            import subprocess
            try:  # "{ sec = 1732000000, usec = 123456 } Wed Nov 20 …"
                out = subprocess.run([mac_tool("sysctl"), "-n", "kern.boottime"], capture_output=True,
                                     text=True, timeout=5).stdout
                boot = (m := re.search(r"sec = (\d+)", out)) and m.group(1)
            except (OSError, subprocess.SubprocessError):
                pass
        _BOOT.append(boot)
    return _BOOT[0]


def folds_case(path) -> bool:
    """Whether ``path`` is on a file system that ignores case: some component of its real path,
    with its letters' case swapped, is the same file in the same parent (APFS and HFS+ as macOS
    formats them, WSL2's drvfs on /mnt/c; not ext4). Here, not in cli, since
    context.repo_entries needs it too (#1157, 2026-11-24).

    Each component is tried in its own parent, never the whole path swapped: on WSL2 that read
    /MNT/C/uSERS/…, /MNT isn't on the ext4 root, so a repo on /mnt/c read as case-sensitive and
    a local_only key in another case than the cwd's sent it to the API model (#1217,
    2026-11-24). Any component, not only the deepest: drvfs makes a directory WSL creates
    case-sensitive (`case=dir`) under Windows' own folding ones, such as C:\\Users."""
    p = os.path.realpath(path)
    while (head := os.path.dirname(p)) != p:
        name = os.path.basename(p)
        if name.swapcase() != name:
            try:
                if os.path.samefile(p, os.path.join(head, name.swapcase())):
                    return True
            except OSError:
                pass
        p = head
    return False


_MACHINE: list = []


def machine() -> str:
    """This machine, steadily: /etc/machine-id (Linux, WSL), the hardware UUID (macOS), else the
    host name. A state directory on a network home is shared by several machines, and each one's
    wb read the other's live runners as dead (its pid not in this machine's process table): failed
    them as restarted, and signalled pids of its own (#849). The runner records it (status
    `machine`), and a runner of another machine is never reaped or stopped here."""
    if not _MACHINE:
        mid = None
        for f in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
            try:
                mid = Path(f).read_text().strip() or None
            except OSError:
                continue
            if mid:
                break
        if not mid and sys.platform == "darwin":
            import subprocess
            try:
                out = subprocess.run([mac_tool("ioreg"), "-rd1", "-c", "IOPlatformExpertDevice"],
                                     capture_output=True, text=True, timeout=5).stdout
                mid = (m := re.search(r'"IOPlatformUUID" = "([^"]+)"', out)) and m.group(1)
            except (OSError, subprocess.SubprocessError):
                pass
        _MACHINE.append(mid or host())
    return _MACHINE[0]


def host() -> str:
    """This machine's name, for the user (status `host`)."""
    import socket
    return socket.gethostname()


def elsewhere(st: dict) -> str | None:
    """The machine a task's runner runs on, when it is another one (status `machine`, named by
    `host`): its pid means nothing here (#849). None for this machine's, or one recorded before."""
    m = st.get("machine")
    return (st.get("host") or m) if m and m != machine() else None


# Network file systems by their mount type (/proc/self/mounts, macOS's mount): a state directory
# on one may be shared by several machines (#849)
NETWORK_FS = ("nfs", "nfs4", "cifs", "smb3", "smbfs", "afs", "afpfs", "webdav", "ceph", "glusterfs",
              "lustre", "gpfs", "beegfs", "fuse.sshfs", "fuse.glusterfs", "fuse.cephfs")


@functools.cache
def network_fs(path: str) -> str | None:
    """The network file system ``path`` (a real path) is on, by its mount type; None for a local
    one, or where the mounts can't be read."""
    mounts: list[tuple[str, str]] = []
    try:
        for row in Path("/proc/self/mounts").read_text().splitlines():
            f = row.split()
            if len(f) > 2:
                mounts.append((f[1].replace("\\040", " "), f[2]))
    except OSError:
        if sys.platform == "darwin":  # "server:/export on /Users (nfs, nodev, …)"
            import subprocess
            try:
                out = subprocess.run(["mount"], capture_output=True, text=True, timeout=5).stdout
            except (OSError, subprocess.SubprocessError):
                out = ""
            for row in out.splitlines():
                if (m := re.search(r" on (/.*) \(([^,)]+)", row)):
                    mounts.append((m.group(1), m.group(2)))
    # the longest mount point holding it, and of several at one point the last listed, which is
    # stacked on the others: an automount (x-systemd.automount, an autofs direct map) lists its
    # autofs trigger first and the NFS mount after it at the same path, and max() kept the
    # trigger, so a network home read as local (#896)
    best = None
    for m in mounts:
        if (path == m[0] or path.startswith(m[0].rstrip("/") + "/")) and \
                (best is None or len(m[0]) >= len(best[0])):
            best = m
    return best[1] if best and best[1] in NETWORK_FS else None


def machine_dir() -> Path:
    """Where this machine's own state lives: $WB_HOME, or on a network file system its
    `machines/<machine()>/`, since several machines may share the state directory there. The GPU
    lease's files were the first (#849); optimize.json, boxes.json and wsl-gpus.json followed,
    shared, one machine's measurements re-measured away by the next and its loads on a wb remote
    box read as the other's own (#894). Made when needed."""
    h = home()
    if not network_fs(os.path.realpath(h)):
        return h
    own = h / "machines" / machine()
    own.mkdir(parents=True, exist_ok=True)
    return own


def write_json(path: Path, data, mode: int | None = None) -> None:
    """``data`` as ``path``, atomically; ``mode``: the file's, else the umask's (a shared GPU
    lease's gpu.json is the group's to read: #802)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # a name of its own per write, created exclusively: by pid alone, two of the chat server's
    # threads writing one file shared it, and one's rename found it gone (the 2026-11-19 audit).
    # Not mkstemp, whose 0600 would replace the file's mode.
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:12]}.tmp")
    try:
        # UTF-8 whatever the locale: under a legacy one an em dash in a spec failed to write and a
        # file written under UTF-8 failed to read back (the 2026-11-16 audit)
        with open(tmp, "x", encoding="utf-8", errors="backslashreplace") as fh:  # as append_jsonl
            fh.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
            _synced(fh)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)  # readers (woken by inotify on the rename) never see half a file
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def replace_bytes(path: Path, data: bytes) -> None:
    """``path``'s whole contents replaced by ``data`` as write_json does: a name of its own,
    synced, then renamed into place. results.jsonl (wb trial), notify.jsonl (the daily gc), a
    task's `acked` and notes.md were renamed unsynced under a pid-only name, so a crash soon after
    could leave the router's whole history or every pending notice empty (#734, the 2026-11-21
    audit)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:12]}.tmp")
    try:
        with open(tmp, "xb") as fh:
            fh.write(data)
            _synced(fh)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _synced(fh) -> None:
    """The file's data on the disk before a rename puts it in place: renamed unsynced, a power
    loss on XFS (RHEL's default) or ext4 without auto_da_alloc can leave task.json or
    status.json empty, read as {} (#475, the audit of 1.8)."""
    fh.flush()
    os.fsync(fh.fileno())


class Damaged(KeyError, RuntimeError):
    """A key missing from a task's task.json: a KeyError to the code that reads it, a message to
    wb's caller (main prints a RuntimeError's)."""

    def __str__(self) -> str:
        return str(self.args[0])


class Record(dict):
    """task.json's contents: a key it lacks says the file is damaged, not a bare KeyError
    (#475)."""

    def __init__(self, data=None, task: str = ""):
        super().__init__(data if isinstance(data, dict) else {})
        self.task = task

    def __missing__(self, key):
        raise Damaged(f"task {self.task}'s task.json is damaged (it has no {key!r}: a write a "
                      f"crash or power loss cut short?); its work is in "
                      f"{task_dir(self.task) / 'worktree'}")


def writable_target(path: Path, change: str = "") -> Path:
    """The file a write to the user's config file ``path`` lands in: a symlink's target (dotfiles:
    stow, chezmoi, yadm, home-manager), or RuntimeError when that isn't writable (a link into a
    read-only store, such as Nix's), saying ``change``, what to make by hand, if given."""
    target = path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if not os.access(target.parent, os.W_OK) or target.exists() and not os.access(target, os.W_OK):
        raise RuntimeError(f"{path}" + (f" links to {target}, which" if target != path else "")
                           + " isn't writable (a read-only store, such as Nix's?); "
                           + (f"make this change in its source by hand: {change}" if change
                              else "edit it in its source (your dotfiles) by hand"))
    return target


def write_through(path: Path, data, change: str = "") -> None:
    """write_json for a file of the user's own (OpenCode's config, models.json, commands.json,
    savings.json): written to a symlink's target, so the link stays (writable_target), and with
    the file's mode kept, 0600 for a new one (start/stop commands and apiKeys can carry tokens),
    the temporary file never readable by others. write_json replaced a dotfiles link with a plain
    0644 file, so the change never reached the dotfiles and the next `chezmoi apply` undid it
    (#495; OpenCode's config since the 2026-11-16 audit)."""
    target = writable_target(path, change)
    mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else 0o600
    tmp = target.with_name(f".{target.name}.{os.getpid()}.{uuid.uuid4().hex[:12]}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
            _synced(fh)
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def config_lock_dir() -> Path:
    """Where the config files' locks live: one place for every state directory of this account,
    since the files are shared. Under $WB_HOME/locks two profiles (a second WB_HOME, a test one
    beside the real one) editing one commands.json took different locks, and the later write
    dropped the other's grant (#939, the outside review of 1.90.24; #495 again). $XDG_RUNTIME_DIR
    first (no lane reads /run/user, context.ONLY_KEPT: an agent can't flock one there), else a
    0700 directory of this uid's in the temp directory (in the lane: an agent could hold a lock
    there until its round's sweep, the price of a machine without a runtime directory); one that
    isn't this account's own leaves $WB_HOME/locks. Not beside the file: there it would be a
    stray file in the user's dotfiles."""
    uid = os.getuid()
    for base, name in ((os.environ.get("XDG_RUNTIME_DIR"), "workbench"),
                       (tempfile.gettempdir(), f"workbench-{uid}")):
        if not base or not os.path.isabs(base):
            continue
        d = Path(base) / name
        try:
            d.mkdir(mode=0o700, exist_ok=True)
            st = os.lstat(d)
        except OSError:
            continue
        if stat.S_ISDIR(st.st_mode) and st.st_uid == uid and not st.st_mode & 0o077:
            return d
    return home() / "locks"


def config_lock(path: Path) -> Path:
    """The lock over a read-modify-write of the user's config file ``path`` (edit_config), keyed
    by the file a write lands in, in config_lock_dir."""
    key = hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:16]
    return config_lock_dir() / f"config-{key}.lock"


def _seen(path: Path):
    """The file a write to ``path`` lands in, as (inode, mtime, size), or None when absent."""
    try:
        st = path.resolve().stat()
    except OSError:
        return None
    return st.st_ino, st.st_mtime_ns, st.st_size


@contextmanager
def edit_config(path: Path, read, change: str = ""):
    """A read-modify-write of the user's config file ``path``: under its lock (config_lock),
    ``read()`` (which checks it and raises on a file it can't keep) is yielded to change in place,
    then written through (write_through); nothing is written when the body raises. Unlocked, two
    sessions' `wb allow --always` close together lost one of the grants (#495). A file changed
    under it all the same (by OpenCode, by hand, or a wb before config_lock_dir) is left as it
    is, with a RuntimeError to run the command again: written, it would lose that change (#939)."""
    with locked(config_lock(path)):
        seen = _seen(path)
        data = read()
        yield data
        if _seen(path) != seen:
            raise RuntimeError(f"{path} changed while wb was editing it; nothing was written: "
                               "run the command again")
        write_through(path, data, change)


def take_approval(name: str, secs: float) -> dict | None:
    """$WB_HOME/<name>, the mod's dialog's one-shot yes (send-ok.json #1005, route-ok.json
    #1014), consumed: renamed away first, then read, so one call takes it whether it matches or
    not and two calls can't both read it. None when missing, unreadable, or not written within
    ``secs`` (its `at`); the caller compares the rest."""
    path = home() / name
    claim = path.with_name(f".{name}.{os.getpid()}")
    try:
        os.replace(path, claim)
    except OSError:
        return None
    try:
        ok = read_json(claim, {})
    except OSError:
        return None
    finally:
        claim.unlink(missing_ok=True)
    try:
        fresh = isinstance(ok, dict) and 0 <= time.time() - float(ok.get("at", 0)) < secs
    except (TypeError, ValueError):
        return None
    return ok if fresh else None


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):  # ValueError: bad JSON, or bytes that aren't UTF-8
        return default


def lock_fd(path: Path | str, mode: int = 0o664) -> int:
    """An fd to flock `path` through, made if missing: read-write when this account may write it,
    else read-only (another account's file in a shared lease or git dir: flock needs no write
    there). Read-only alone failed on NFSv4, whose client turns an exclusive flock into a
    whole-file write lock and refuses it with EBADF on an fd without write access: every task
    failed at the GPU lease, and wb accept --merge ended committed but unmerged (#892)."""
    try:
        return os.open(path, os.O_RDWR | os.O_CREAT, mode)
    except OSError as e:
        if e.errno not in (errno.EACCES, errno.EPERM, errno.EROFS):
            raise
        return os.open(path, os.O_RDONLY | os.O_CREAT, mode)


@contextmanager
def locked(path: Path, shared: bool = False, blocking: bool = True):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as fh:
        flags = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        if not blocking:
            flags |= fcntl.LOCK_NB
        fcntl.flock(fh, flags)  # raises BlockingIOError when non-blocking and held
        try:
            yield fh
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def task_lock(name: str, kind: str) -> Path:
    """Task `name`'s lock of `kind` ("runner": one runner a task; "cmd": one wb command at a
    time; "status", "events", "steer", "grants": one writer of that file), in $WB_HOME/locks/,
    which no lane reads. In the task's directory or its rules (both readable), an agent could
    hold one with flock (an O_RDONLY fd is enough) from a process sweep misses and wedge every
    later runner or command: runner.lock first, then status.lock, which froze the runner at its
    next status write and `wb stop` with it (the 0.90 review)."""
    return home() / "locks" / f"{name}.{kind}.lock"


def _lock_of(path: Path) -> Path:
    # a task's own JSONL (events, steer) takes its lock outside the lane; the shared files
    # (notify, results, lessons, chat) aren't in any lane and keep theirs beside them
    if path.parent.parent in (tasks_dir(), archive_dir()):
        return task_lock(path.parent.name, path.stem)
    return path.with_name(path.name + ".lock")


def append_jsonl(path: Path, row: dict, after: float | None = None) -> dict:
    """Append ``row``, and return it as written. ``after`` (a time): its `ts` is made later than
    it and than every row's already there, under the same lock (the notices: Task.notify)."""
    with locked(_lock_of(path)):
        if after is not None:
            row = {**row, "ts": max(row["ts"], round(max(after, newest_ts(path)) + 0.001, 3))}
        # a lone surrogate (a path's byte that isn't UTF-8) as its JSON escape, which reads back
        # the same: strict, it raised past every caller's guard and failed the task (#784)
        line = json.dumps(row, ensure_ascii=False).encode("utf-8", "backslashreplace") + b"\n"
        with open(path, "ab+") as fh:
            # A write cut short (a full disk, a crash) leaves a line with no end, and the next row
            # appended to it was lost with it (the 2026-11-16 audit): end the fragment first, so
            # only it is skipped. In append mode the write lands at the end wherever we read.
            fh.seek(0, os.SEEK_END)
            if fh.tell():
                fh.seek(-1, os.SEEK_END)
                if fh.read(1) != b"\n":
                    line = b"\n" + line
            fh.write(line)
    return row


_results_seen: dict = {}  # path -> (inode, size, mtime_ns), rows: results() parses a file once


def results(trials: bool = False, reports: bool | None = None) -> list[dict]:
    """results.jsonl's rows: the evidence the router and the skill records learn from, which a
    trial run (a check of workbench itself on a scratch repo) is not, unless asked for, nor a
    report task's row (`report`, #1278: a finder's or a judge's, no coding step), unless
    ``reports`` (by default as ``trials``: the money readers ask for every row, and a report
    task's dollars were spent like any other's).
    The file is parsed again only when it changed (its inode, size or mtime): the router, the
    slice and `wb tune` asked for it per model, size and knob, and at 20,000 rows `wb models`
    read it 9 times (2.9 s) and `wb tune` 25 (7.8 s) (#727). Callers don't change the rows."""
    path = home() / "results.jsonl"
    try:
        st = path.stat()
        stamp = (st.st_ino, st.st_size, st.st_mtime_ns)
    except FileNotFoundError:
        stamp = None
    seen = _results_seen.get(path)
    if seen is not None and seen[0] == stamp:
        rows = seen[1]
    else:
        rows = read_jsonl(path)[0] if stamp else []
        _results_seen.clear()  # one file at a time: a test's WB_HOME, then the next one's
        _results_seen[path] = (stamp, rows)
    reports = trials if reports is None else reports
    return [r for r in rows if (trials or not r.get("trial")) and (reports or not r.get("report"))]


def notices(runs: bool = False) -> list[dict]:
    """notify.jsonl's rows that are whole: a dict with a task's name, a kind and a time. One row
    without a `task` made every command that acknowledges raise (the 2026-11-16 audit, when the
    agent's lane could still write the file), and a name like ../../x put the ack elsewhere.
    ``runs``: a run's notices too (#1278: `run` in a task's place), which every caller that indexes
    `task` never asks for."""
    return [r for r in read_jsonl(home() / "notify.jsonl")[0]
            if isinstance(r, dict) and isinstance(r.get("kind"), str) and isinstance(r.get("ts"), (int, float))
            and isinstance(r.get("text", ""), str)
            and (isinstance(r.get("task"), str) and NAME_RE.match(r["task"])
                 or runs and "task" not in r and isinstance(r.get("run"), str) and NAME_RE.match(r["run"]))]


def newest_ts(path: Path) -> float:
    """The latest `ts` among a JSONL file's rows (its tail first: notices carry reports), 0 for
    none."""
    rows = read_jsonl_tail(path, 256 * 1024) or read_jsonl(path)[0]
    return max((r["ts"] for r in rows if isinstance(r, dict)
                and isinstance(r.get("ts"), (int, float))), default=0.0)


def mark_trials(names: set[str]) -> list[str]:
    """Mark closed tasks' rows as trial runs; the names that had an unmarked row."""
    path = home() / "results.jsonl"
    with locked(path.with_name(path.name + ".lock")):
        rows = read_jsonl(path)[0]
        hit = [r["task"] for r in rows if r.get("task") in names and not r.get("trial")]
        if hit:
            replace_bytes(path, "".join(json.dumps({**r, "trial": True} if r.get("task") in names
                                                   else r, ensure_ascii=False) + "\n"
                                        for r in rows).encode("utf-8"))  # #734
    return sorted(set(hit))


def read_jsonl(path: Path, offset: int = 0):
    """Rows from byte ``offset`` on, and the offset after the last whole line."""
    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            data = fh.read()
    except FileNotFoundError:
        return [], offset
    end = data.rfind(b"\n") + 1  # a line still being written waits for the next read
    return _rows(data[:end]), offset + end


def read_jsonl_tail(path: Path, max_bytes: int) -> list:
    """The whole rows in the file's last ``max_bytes``: a reader that wants the latest rows
    parsed the whole history every time (the 2026-11-19 audit: the chat's thread took 56 ms at
    20,000 messages)."""
    try:
        with open(path, "rb") as fh:
            start = max(0, fh.seek(0, os.SEEK_END) - max_bytes)
            fh.seek(max(0, start - 1))
            data = fh.read()
    except FileNotFoundError:
        return []
    if start:  # from the first line that starts inside the window
        data = data[data.find(b"\n") + 1:] if b"\n" in data else b""
    return _rows(data[:data.rfind(b"\n") + 1])


def _rows(data: bytes) -> list:
    rows = []
    for line in data.splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:  # a torn line; one that isn't UTF-8 too, which crashed every reader (the audit)
            pass
    return rows


def round_prompt(task, n: int) -> Path:
    """Round n's own prompt: `instructions.md` once a fresh session's handoff took prompt.md's
    place (runner._start_fresh), else prompt.md. Read by current_task_prompt, round_asks, the
    lessons and wb bench add: bench read prompt.md alone and refused every step sent to a fresh
    session, "step 2's prompt is gone" (#503, the audit of 1.90). Of anything with `round_dir`,
    as the runner's readers are called."""
    d = task.round_dir(n)
    return d / "instructions.md" if (d / "instructions.md").is_file() else d / "prompt.md"

class Task:
    """A task directory. ``spec`` is task.json, ``status`` is status.json (re-read on access)."""

    def __init__(self, name: str):
        self.name = name
        self.dir = task_dir(name)

    @property
    def exists(self) -> bool:
        return (self.dir / "task.json").exists()

    @property
    def spec(self) -> dict:
        return Record(read_json(self.dir / "task.json", {}), self.name)

    @property
    def status(self) -> dict:
        return read_json(self.dir / "status.json", {})

    @property
    def worktree(self) -> Path:
        # deeper, under tree/, for a sibling more than one level up (task.json `worktree`, #261)
        return self.dir / self.spec.get("worktree", "worktree")

    @property
    def workdir(self) -> Path:
        """Where the agent, the setup and the tests run: the package wb start was given in a
        monorepo (task.json `subdir`, #375), else the worktree's top."""
        return self.worktree / self.spec.get("subdir", "")

    def round_dir(self, n: int) -> Path:
        return self.dir / "rounds" / str(n)

    def report(self, n: int) -> str:
        """Round ``n``'s report (summary.md), else what stands instead, as `wb report` and the
        web chat's task sheet (#1201) both say it."""
        p = self.round_dir(n) / "summary.md"
        try:
            return p.read_text(errors="replace")
        except FileNotFoundError:
            return f"round {n} has no report yet ({self.status.get('phase')}: {self.status.get('last')})"


    def set_status(self, **fields) -> dict:
        # The runner and wb both write status; the lock keeps their edits whole.
        with locked(task_lock(self.name, "status")):
            st = self.status
            st.update(fields, updated_at=now())
            write_json(self.dir / "status.json", st)
            return st

    def set_status_if(self, check, **fields) -> dict | None:
        """set_status only while check(status) holds, under the same lock: a compare-and-set,
        so two processes acting on the same news (two waiters reaping one dead runner) act
        once. The new status, or None when the check failed."""
        with locked(task_lock(self.name, "status")):
            st = self.status
            if not check(st):
                return None
            st.update(fields, updated_at=now())
            write_json(self.dir / "status.json", st)
            return st

    def event(self, row: dict) -> None:
        append_jsonl(self.dir / "events.jsonl", row)

    def notify(self, kind: str, text: str, **extra) -> dict | None:
        """The notice, as notify.jsonl has it (its `ts` maybe moved later), or None for a bench
        replay's, which wakes no one."""
        if self.spec.get("bench"):  # a replay of wb bench's: no one is to be woken
            return None
        # stamped after every notice before it and after the task's acknowledgement, whatever
        # the clock did since: corrected backwards (a dual boot with a local-time RTC), a notice
        # raised after a mark came out at or before it, and wb wait, the mod's wake and its idle
        # net all skipped it as handled (#473, the audit of 1.8)
        try:
            acked = float((self.dir / "acked").read_text() or 0)
        except (OSError, ValueError):
            acked = 0.0
        path = home() / "notify.jsonl"
        if kind not in RUN_GLOBAL and self.spec.get("run"):
            from . import runs  # only here: runs imports store
            if runs.item_open(self.spec):  # #1278: an open run's item wakes its live driver, never Claude
                path = runs.run_dir(self.spec["run"]) / "notify.jsonl"
        row = append_jsonl(path, {"ts": now(), "task": self.name, "kind": kind, "text": text, **extra},
                           after=acked if math.isfinite(acked) else 0.0)
        # each named task's newest notice, in its own directory: the mod's idle net read it from
        # notify.jsonl's last 1 MiB only, and after a reload woke Claude again for a review whose
        # notice it had acknowledged, pushed out of that tail by later ones (#844). A run item's
        # too, whose notice its driver acknowledges (runs._ack_items): with no mark the idle net
        # read `acked` against the status written after the notice, and woke Claude a minute into
        # each code item's review mid-run (the review of #1278, 2026-11-25)
        for name in dict.fromkeys([self.name, *(extra.get("tasks") or [])]):
            if not isinstance(name, str) or "/" in name or name.startswith("."):
                continue  # a batch names tasks wb named: nothing outside tasks/
            d = self.dir if name == self.name else home() / "tasks" / name
            try:
                was = float((d / "noticed").read_text() or 0)
            except (OSError, ValueError):
                was = 0.0
            if d.is_dir() and not was >= row["ts"]:
                replace_bytes(d / "noticed", str(row["ts"]).encode())
        return row


# The scratch directories workbench makes in its home, each named <prefix><maker's pid>-…: wb gc
# removes one SCRATCH_AGE old whose maker is gone. Killed commands left full clones there, with
# their copied dependencies, that _disk_room then counted against every new task (#376, the
# audit of 1.6).
SCRATCH_PREFIXES = (".verify-", ".base-", ".check-", ".submodule-", "wb-index-", "wb-merge-",
                    ".wb-image-", ".wb-audio-")  # the last two in wb image's and audio's output (#589)
SCRATCH_AGE = 3600


def scratch(prefix: str) -> str:
    """``prefix`` (one of SCRATCH_PREFIXES) with this process's pid, its start (`.<tag>`,
    models.start_tag: a pid recycled since isn't the maker, #532) and this machine
    (`.<machine_tag>`: on a state directory several machines share, gc on one removed another's
    live clone, its pid not in this process table, #876), for tempfile."""
    pid = os.getpid()
    return f"{prefix}{pid}.{_start_tag(pid)}.{machine_tag()}-"


def machine_tag() -> str:
    """This machine (store.machine) in 8 hex digits, for a name."""
    import zlib
    return f"{zlib.crc32(machine().encode()):08x}"


@functools.cache  # by pid: a forked child is another process
def _start_tag(pid: int) -> str:
    from . import models  # only here: models imports store
    return models.start_tag(pid)


def owner(t: "Task") -> tuple:
    """Whose task it is, as one restart or shutdown's batch is judged (#544): the Claude Code
    session `wb start` ran in and the directory it ran in (else the repo)."""
    return t.spec.get("session"), t.spec.get("cwd") or t.spec.get("repo")


def write_ack(task: "Task", ts: float) -> None:
    """Task's `acked` mark, replaced whole: the mod and wb wait read it on every poll, and a read
    between a truncate and the write (or a write that failed after it) saw 0, every old notice
    news again (the 2026-11-16 audit)."""
    replace_bytes(task.dir / "acked", str(ts).encode())  # synced: read back empty, all news (#734)


def ack_batch(batch: list, row: dict | None, ts: float) -> None:
    """The rest of a batch notice's tasks (batch[1:]) acknowledged with it: up to the stamp
    notify.jsonl has (`row`), which append_jsonl may have moved past `ts`, and never lower than
    each one's own mark. Acked at the `ts` asked for, each was left below its `noticed`, and the
    mod's idle net woke Claude once a task, the wake the batch was for (#874, Claude's audit of
    1.90.24)."""
    upto = row["ts"] if row else ts
    for t in batch[1:]:
        try:
            acked = float((t.dir / "acked").read_text() or 0)
        except (OSError, ValueError):
            acked = 0.0
        write_ack(t, max(acked, upto) if math.isfinite(acked) else upto)


def all_tasks(archived: bool = False):
    """Every task in tasks/: open, and closed less than ARCHIVE_DAYS ago (archive); with
    ``archived``, the archived ones too, for a question about history: a batch's older tasks
    dropped out of wb backlog after 30 days, and its totals with them (#270, the audit of 1.2)."""
    ds = [d for d in (tasks_dir(), *([archive_dir()] if archived else ())) if d.exists()]
    return sorted((Task(p.name) for d in ds for p in d.iterdir() if (p / "task.json").exists()),
                  key=lambda t: t.spec.get("created_at", 0))


ARCHIVE_DAYS = 30


SLIM_DAYS = 90  # an archived task keeps its bulky outputs this long after it closed
# what a closed task's directory holds that nothing reads once it is old: the runner's log
# (appended every round), each round's and check's test output (up to 320 KB each). Its
# task.json, status.json, prompts, summaries, events and asks stay: names stay taken, wb bench
# add reads an accepted task's prompts, and the lessons and transcripts its events (#740).
# And the earlier sessions' exports (`.export-<sid>.json`, megabytes each: a cache of OpenCode's
# own store, which transcript.finished_export fills again on demand) and wb shot's PNGs, whose
# shots.json side says `pruned` so the web chat shows them expired, not broken (#821).
SLIM_GLOBS = ("runner.log", "base-tests.txt", "setup.txt", "rounds/*/tests.txt",
              "verify/*/base.txt", "verify/*/candidate.txt", "shots/*/*.txt", ".export-*.json")
SHOT_SIDES = ("base", "candidate")


def slim_archive(days: float = SLIM_DAYS) -> int:
    """Drop SLIM_GLOBS from the archived tasks closed over ``days`` ago: archive/ kept every
    one of them for good (#740, the 2026-11-21 audit). The bytes freed."""
    cutoff, freed = time.time() - days * 86400, 0
    for d in sorted(archive_dir().iterdir()) if archive_dir().is_dir() else ():
        st = read_json(d / "status.json", {})
        if not isinstance(st, dict) or st.get("phase") not in CLOSED or (st.get("updated_at") or 0) > cutoff:
            continue
        for pattern in SLIM_GLOBS:
            for f in d.glob(pattern):
                try:
                    if f.is_file() and not f.is_symlink():
                        freed += f.stat().st_size
                        f.unlink()
                except OSError:
                    continue
        for shot in d.glob("shots/*/"):
            freed += _prune_shots(shot)
    return freed


def _prune_shots(shot: Path) -> int:
    """Drop one wb shot pair's PNGs, its shots.json side marked `pruned` first (chat.shots: the
    image expired); the bytes freed."""
    meta, freed = read_json(shot / "shots.json", None), 0
    if not isinstance(meta, dict):
        return 0
    pngs = [(side, f) for side in SHOT_SIDES
            if (f := shot / f"{side}.png").is_file() and not f.is_symlink()]
    if not pngs:
        return 0
    for side, _ in pngs:
        meta[side] = {**(meta.get(side) if isinstance(meta.get(side), dict) else {}), "pruned": True}
    write_json(shot / "shots.json", meta)  # before the files: never an image said there that isn't
    for _, f in pngs:
        try:
            freed += f.stat().st_size
            f.unlink()
        except OSError:
            continue
    return freed


def archive(days: float = ARCHIVE_DAYS) -> list[str]:
    """Move the tasks closed and untouched for ``days`` to archive/, with their locks gone: every
    wb wait wake, wb events minute and wb start read each task's two files, which after 101 tasks
    in three days was most of what they did (the 2026-11-19 audit). Task(name) still finds them
    (task_dir), so wb report, bench add and the results keep working; names stay taken."""
    cutoff, moved = time.time() - days * 86400, []
    for t in all_tasks():
        st = t.status
        if st.get("phase") not in CLOSED or st.get("unrecorded") or (st.get("updated_at") or 0) > cutoff:
            continue
        archive_dir().mkdir(parents=True, exist_ok=True)
        try:
            os.rename(t.dir, archive_dir() / t.name)
        except OSError:
            continue
        for lock in (home() / "locks").glob(f"{t.name}.*.lock"):
            lock.unlink(missing_ok=True)
        moved.append(t.name)
    return moved
