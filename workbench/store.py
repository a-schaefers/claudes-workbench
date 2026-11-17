"""Task state on disk: one directory per task, JSON written atomically, JSONL appended under a lock.

Layout of ``$WB_HOME`` (default ``~/.local/state/workbench``)::

    notify.jsonl            events that need Claude (review, stuck, failed, question), all tasks
    results.jsonl           one row per finished task (accepted / discarded), for `wb stats`;
                            a trial run's (`wb start --trial`, `wb trial`) is kept but not evidence
    gpu.lock                held by every running task: shared beside others, exclusive alone
    gpu.json                who holds it (pid → model, task); gpu.admit serializes admissions
    tasks/<name>/
        task.json           what the task is (static after start)
        status.json         what it is doing now (rewritten by the runner and the ask server)
        events.jsonl        the agent's normalized events, every round
        runner.log          the runner's own stdout/stderr
        opencode.json       the per-task OpenCode config layer (the ask_reviewer MCP server)
        rounds/<n>/prompt.md, summary.md, tests.txt
        ask/<id>.json, ask/<id>.answer
        worktree/           the git worktree the agent works in (branch delegate/<name>)
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


def home() -> Path:
    return Path(os.environ.get("WB_HOME") or Path.home() / ".local/state/workbench")


def tasks_dir() -> Path:
    return home() / "tasks"


def task_dir(name: str) -> Path:
    return tasks_dir() / name


def now() -> float:
    return round(time.time(), 3)


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, path)  # readers (woken by inotify on the rename) never see half a file


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
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


def append_jsonl(path: Path, row: dict) -> None:
    with locked(path.with_name(path.name + ".lock")):
        with open(path, "a") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def results(trials: bool = False) -> list[dict]:
    """results.jsonl's rows: the evidence the router and the skill records learn from, which a
    trial run (a check of workbench itself on a scratch repo) is not, unless asked for."""
    rows = read_jsonl(home() / "results.jsonl")[0]
    return rows if trials else [r for r in rows if not r.get("trial")]


def mark_trials(names: set[str]) -> list[str]:
    """Mark closed tasks' rows as trial runs; the names that had an unmarked row."""
    path = home() / "results.jsonl"
    with locked(path.with_name(path.name + ".lock")):
        rows = read_jsonl(path)[0]
        hit = [r["task"] for r in rows if r.get("task") in names and not r.get("trial")]
        if hit:
            tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            tmp.write_text("".join(json.dumps({**r, "trial": True} if r.get("task") in names else r,
                                              ensure_ascii=False) + "\n" for r in rows))
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
        except json.JSONDecodeError:
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
        # The runner and the ask server both write status; the lock keeps their edits whole.
        with locked(self.dir / "status.lock"):
            st = self.status
            st.update(fields, updated_at=now())
            write_json(self.dir / "status.json", st)
            return st

    def event(self, row: dict) -> None:
        append_jsonl(self.dir / "events.jsonl", row)

    def notify(self, kind: str, text: str, **extra) -> None:
        append_jsonl(home() / "notify.jsonl",
                     {"ts": now(), "task": self.name, "kind": kind, "text": text, **extra})


def all_tasks():
    d = tasks_dir()
    if not d.exists():
        return []
    return sorted((Task(p.name) for p in d.iterdir() if (p / "task.json").exists()),
                  key=lambda t: t.spec.get("created_at", 0))
