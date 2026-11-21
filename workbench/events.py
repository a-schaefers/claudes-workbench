"""OpenCode's `run --format json` events, reduced to what a person or a reviewer needs to see,
and the detector that decides a run is going nowhere.
"""
import hashlib
import json
import os
import time
from collections import deque

EDIT_TOOLS = {"edit", "write", "patch", "multiedit", "apply_patch"}


def _brief(tool: str, inp: dict) -> str:
    if not isinstance(inp, dict):
        return ""
    if tool == "skill":
        return str(inp.get("id") or inp.get("name") or "")
    for key in ("command", "filePath", "path", "pattern", "url", "query", "description"):
        if inp.get(key):
            text = str(inp[key]).replace("\n", " ")
            return text if len(text) <= 90 else text[:87] + "..."
    return ""


def _obj(v) -> dict:
    return v if isinstance(v, dict) else {}


def normalize(raw: dict, round_no: int):
    """One OpenCode event as a compact row, or None for events nobody needs to see. A line of
    the wrong shape (a plugin's console.log on OpenCode's stdout) is skipped, not a crash of the
    runner (the 2026-11-16 audit)."""
    if not isinstance(raw, dict):
        return None
    kind = raw.get("type")
    part = _obj(raw.get("part"))
    ts = raw.get("timestamp")
    # when OpenCode leaves the timestamp out, the time it was read: 0.0 sorted the row first in
    # wb watch and the check-in digests (#481)
    ts = round(ts / 1000, 3) if isinstance(ts, (int, float)) and not isinstance(ts, bool) else round(time.time(), 3)
    if kind == "tool_use":
        state = _obj(part.get("state"))
        tool = str(part.get("tool") or "?")
        row = {"ts": ts, "round": round_no, "kind": "tool", "tool": tool,
               "brief": _brief(tool, state.get("input")), "ok": state.get("status") != "error"}
        if state.get("status") == "error":
            row["error"] = str(state.get("error"))[:240]
        inp = state.get("input") if isinstance(state.get("input"), dict) else {}
        if tool in EDIT_TOOLS and (inp.get("filePath") or inp.get("path")):
            row["path"] = str(inp.get("filePath") or inp.get("path"))  # whole: brief is cut at 90
        # The signature is what "the same call again" means to the stuck detector: the whole
        # input. Cut to its first 400 characters, `content` sorting before `filePath`, four new
        # files opening with one licence header were "the same call 4 times" (#497).
        whole = json.dumps(state.get("input"), sort_keys=True, default=str)
        row["sig"] = f"{tool}:{hashlib.sha1(whole.encode(errors='replace')).hexdigest()}"
        return row
    if kind == "text":
        return {"ts": ts, "round": round_no, "kind": "text", "text": str(part.get("text") or "")}
    if kind == "step_finish":
        tok = _obj(part.get("tokens"))
        cache = _obj(tok.get("cache"))
        n = lambda v: v if isinstance(v, (int, float)) else 0  # noqa: E731
        # reasoning apart from output, as OpenCode counts it: its compaction check adds it in,
        # so the runner's handover check must too (#644)
        return {"ts": ts, "round": round_no, "kind": "step",
                "in": n(tok.get("input")), "out": n(tok.get("output")),
                "cache": n(cache.get("read")) + n(cache.get("write")),
                **({"reasoning": n(tok.get("reasoning"))} if n(tok.get("reasoning")) else {})}
    if kind == "error":
        return {"ts": ts, "round": round_no, "kind": "error", "text": _message(raw.get("error"))}
    if kind == "retry":  # a request OpenCode gave up on and sends again: not the model's output
        attempt = raw.get("attempt")
        return {"ts": ts, "round": round_no, "kind": "retry", "text": _message(raw.get("error")),
                **({"attempt": attempt} if isinstance(attempt, int) else {})}
    return None


def _message(err) -> str:
    """An event's error, {type, message} in OpenCode 2.3, as its message."""
    err = err or {}
    return str(err.get("message") if isinstance(err, dict) and err.get("message") else err)[:400]


class StuckDetector:
    """Says why a run should be stopped, or None.

    A small model that has lost the thread repeats itself: the same failing command, a string of
    errors, or many calls that read and run things but never change a file. Wall-clock and silence
    limits are the runner's (it owns the clock); this looks only at the stream of tool calls.
    """

    def __init__(self, repeat=4, error_streak=6, no_edit=40, worktree=None):
        self.repeat, self.error_streak, self.no_edit = repeat, error_streak, no_edit
        self.worktree = os.path.realpath(worktree) if worktree else None
        self.recent = deque(maxlen=repeat)
        self.errors = 0
        self.since_edit = 0
        self.guard = None

    def feed(self, row: dict):
        if row.get("kind") != "tool":
            return None
        self.recent.append(row["sig"])
        self.errors = 0 if row["ok"] else self.errors + 1
        changed = row["tool"] in EDIT_TOOLS and row["ok"] and self._ours(row.get("path"))
        self.since_edit = 0 if changed else self.since_edit + 1
        # .guard names the one that fired, for the Stats tab's "what stops agents" (#404)
        if len(self.recent) == self.repeat and len(set(self.recent)) == 1:
            self.guard = "repeat"
            return f"the same call {self.repeat} times in a row: {row['tool']} {row['brief']}"
        if self.errors >= self.error_streak:
            self.guard = "errors"
            return f"{self.errors} failing tool calls in a row (last: {row.get('error', '')[:120]})"
        if self.since_edit >= self.no_edit:
            self.guard = "no_edit"
            return f"{self.since_edit} tool calls without changing a file in the task"
        return None

    def _ours(self, path) -> bool:
        """Whether an edit was to the task's files. Scratch files elsewhere aren't progress: an
        agent sure the compiler was broken wrote 20 minutes of test programs in /tmp, each write
        resetting the count, while its own change still failed its test (2026-11-18)."""
        if not (self.worktree and path):
            return True
        full = os.path.realpath(os.path.join(self.worktree, path))
        return full == self.worktree or full.startswith(self.worktree + os.sep)
