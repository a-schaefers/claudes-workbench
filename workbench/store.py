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
import fcntl
import json
import os
import re
import time
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
PATH_VARS = ("WB_HOME", "WB_MODELS", "WB_SKILLS", "WB_IMAGES", "WB_COMMANDS", "WB_CHAT_TOKEN",
             "WB_CHAT_CONFIG", "WB_OPENCODE_CONFIG")


def absolute_env() -> None:
    for var in PATH_VARS:
        if os.environ.get(var):
            os.environ[var] = os.path.abspath(os.path.expanduser(os.environ[var]))


def home() -> Path:
    v = os.environ.get("WB_HOME")
    return Path(os.path.abspath(os.path.expanduser(v))) if v else Path.home() / ".local/state/workbench"


def tasks_dir() -> Path:
    return home() / "tasks"


def task_dir(name: str) -> Path:
    return tasks_dir() / name


def now() -> float:
    return round(time.time(), 3)


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    # UTF-8 whatever the locale: under a legacy one an em dash in a spec failed to write and a
    # file written under UTF-8 failed to read back (the 2026-11-16 audit)
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)  # readers (woken by inotify on the rename) never see half a file


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
    if path.parent.parent == tasks_dir():
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
    rows = []
    for line in data[:end].splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:  # a torn line; one that isn't UTF-8 too, which crashed every reader (the audit)
            pass
    return rows, offset + end


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
        return self.dir / "worktree"

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


def all_tasks():
    d = tasks_dir()
    if not d.exists():
        return []
    return sorted((Task(p.name) for p in d.iterdir() if (p / "task.json").exists()),
                  key=lambda t: t.spec.get("created_at", 0))
