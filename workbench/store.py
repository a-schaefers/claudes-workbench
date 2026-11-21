"""Task state on disk: one directory per task, JSON written atomically, JSONL appended under a lock.

Layout of ``$WB_HOME`` (default ``~/.local/state/workbench``)::

    notify.jsonl            events for Claude (review, stuck, failed, question, checkin), all tasks
    results.jsonl           one row per finished task (accepted / discarded), for `wb stats`;
                            a trial run's (`wb start --trial`, `wb trial`) is kept but not evidence
    gpu.lock                held by every running task: shared beside others, exclusive alone
    gpu.json                who holds it (pid → model, task); gpu.admit serializes admissions
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
        rounds/<n>/prompt.md, instructions.md, summary.md, tests.txt
        ask/<id>.json, ask/<id>.answer   the agent's questions and Claude's answers
        worktree/           the git worktree the agent works in (branch delegate/<name>)
    Of a task's directory the agent's lane writes only worktree/ and ask/ (context.writable).

Every file and format: ARCHITECTURE.md, *Files and formats*.
"""
import errno
import fcntl
import hashlib
import json
import os
import re
import stat
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")

# Phases a task moves through. "review", "stuck", "failed" and "asking" need Claude.
ACTIVE = {"queued", "running", "testing", "asking"}
NEEDS_CLAUDE = {"review", "stuck", "failed", "asking"}
CLOSED = {"accepted", "discarded"}


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
             "CLAUDE_CONFIG_DIR")


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
    """Free space where the state directory lives (or ``path``), for the user; None if unknown."""
    p = Path(path or home())
    while not p.exists() and p != p.parent:
        p = p.parent
    try:
        st = os.statvfs(p)
    except OSError:
        return None
    return st.f_bavail * st.f_frsize


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


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # a name of its own per write, created exclusively: by pid alone, two of the chat server's
    # threads writing one file shared it, and one's rename found it gone (the 2026-11-19 audit).
    # Not mkstemp, whose 0600 would replace the file's mode.
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:12]}.tmp")
    try:
        # UTF-8 whatever the locale: under a legacy one an em dash in a spec failed to write and a
        # file written under UTF-8 failed to read back (the 2026-11-16 audit)
        with open(tmp, "x", encoding="utf-8") as fh:
            fh.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        os.replace(tmp, path)  # readers (woken by inotify on the rename) never see half a file
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


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
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def config_lock(path: Path) -> Path:
    """The lock over a read-modify-write of the user's config file ``path`` (edit_config), in
    $WB_HOME/locks/, keyed by the file a write lands in: beside it, it would be a stray file in
    the user's dotfiles."""
    key = hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:16]
    return home() / "locks" / f"config-{key}.lock"


@contextmanager
def edit_config(path: Path, read, change: str = ""):
    """A read-modify-write of the user's config file ``path``: under its lock (config_lock),
    ``read()`` (which checks it and raises on a file it can't keep) is yielded to change in place,
    then written through (write_through); nothing is written when the body raises. Unlocked, two
    sessions' `wb allow --always` close together lost one of the grants (#495)."""
    with locked(config_lock(path)):
        data = read()
        yield data
        write_through(path, data, change)


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):  # ValueError: bad JSON, or bytes that aren't UTF-8
        return default


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


def append_jsonl(path: Path, row: dict) -> None:
    line = json.dumps(row, ensure_ascii=False).encode() + b"\n"
    with locked(_lock_of(path)):
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


def results(trials: bool = False) -> list[dict]:
    """results.jsonl's rows: the evidence the router and the skill records learn from, which a
    trial run (a check of workbench itself on a scratch repo) is not, unless asked for."""
    rows = read_jsonl(home() / "results.jsonl")[0]
    return rows if trials else [r for r in rows if not r.get("trial")]


def notices() -> list[dict]:
    """notify.jsonl's rows that are whole: a dict with a task's name, a kind and a time. One row
    without a `task` made every command that acknowledges raise (the 2026-11-16 audit, when the
    agent's lane could still write the file), and a name like ../../x put the ack elsewhere."""
    return [r for r in read_jsonl(home() / "notify.jsonl")[0]
            if isinstance(r, dict) and isinstance(r.get("task"), str) and NAME_RE.match(r["task"])
            and isinstance(r.get("kind"), str) and isinstance(r.get("ts"), (int, float))
            and isinstance(r.get("text", ""), str)]


def mark_trials(names: set[str]) -> list[str]:
    """Mark closed tasks' rows as trial runs; the names that had an unmarked row."""
    path = home() / "results.jsonl"
    with locked(path.with_name(path.name + ".lock")):
        rows = read_jsonl(path)[0]
        hit = [r["task"] for r in rows if r.get("task") in names and not r.get("trial")]
        if hit:
            tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            tmp.write_text("".join(json.dumps({**r, "trial": True} if r.get("task") in names else r,
                                              ensure_ascii=False) + "\n" for r in rows),
                           encoding="utf-8")
            os.replace(tmp, path)
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
        return read_json(self.dir / "task.json", {})

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

    def notify(self, kind: str, text: str, **extra) -> None:
        if self.spec.get("bench"):  # a replay of wb bench's: no one is to be woken
            return
        append_jsonl(home() / "notify.jsonl",
                     {"ts": now(), "task": self.name, "kind": kind, "text": text, **extra})


# The scratch directories workbench makes in its home, each named <prefix><maker's pid>-…: wb gc
# removes one SCRATCH_AGE old whose maker is gone. Killed commands left full clones there, with
# their copied dependencies, that _disk_room then counted against every new task (#376, the
# audit of 1.6).
SCRATCH_PREFIXES = (".verify-", ".base-", ".check-", ".submodule-", "wb-index-", "wb-merge-")
SCRATCH_AGE = 3600


def scratch(prefix: str) -> str:
    """``prefix`` (one of SCRATCH_PREFIXES) with this process's pid, for tempfile."""
    return f"{prefix}{os.getpid()}-"


def all_tasks(archived: bool = False):
    """Every task in tasks/: open, and closed less than ARCHIVE_DAYS ago (archive); with
    ``archived``, the archived ones too, for a question about history: a batch's older tasks
    dropped out of wb backlog after 30 days, and its totals with them (#270, the audit of 1.2)."""
    ds = [d for d in (tasks_dir(), *([archive_dir()] if archived else ())) if d.exists()]
    return sorted((Task(p.name) for d in ds for p in d.iterdir() if (p / "task.json").exists()),
                  key=lambda t: t.spec.get("created_at", 0))


ARCHIVE_DAYS = 30


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
