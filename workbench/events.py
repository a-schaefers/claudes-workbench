"""OpenCode's `run --format json` events, reduced to what a person or a reviewer needs to see,
and the detector that decides a run is going nowhere.
"""
import json
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


def normalize(raw: dict, round_no: int):
    """One OpenCode event as a compact row, or None for events nobody needs to see."""
    kind = raw.get("type")
    part = raw.get("part") or {}
    ts = round((raw.get("timestamp") or 0) / 1000, 3)
    if kind == "tool_use":
        state = part.get("state") or {}
        tool = part.get("tool") or "?"
        row = {"ts": ts, "round": round_no, "kind": "tool", "tool": tool,
               "brief": _brief(tool, state.get("input")), "ok": state.get("status") != "error"}
        if state.get("status") == "error":
            row["error"] = str(state.get("error"))[:240]
        # The signature is what "the same call again" means to the stuck detector.
        row["sig"] = f"{tool}:{json.dumps(state.get('input'), sort_keys=True)[:400]}"
        return row
    if kind == "text":
        return {"ts": ts, "round": round_no, "kind": "text", "text": part.get("text", "")}
    if kind == "step_finish":
        tok = part.get("tokens") or {}
        return {"ts": ts, "round": round_no, "kind": "step",
                "in": tok.get("input", 0), "out": tok.get("output", 0),
                "cache": (tok.get("cache") or {}).get("read", 0)}
    if kind == "error":
        err = raw.get("error") or {}
        return {"ts": ts, "round": round_no, "kind": "error",
                "text": str(err.get("message") or err)[:400]}
    return None


class StuckDetector:
    """Says why a run should be stopped, or None.

    A small model that has lost the thread repeats itself: the same failing command, a string of
    errors, or many calls that read and run things but never change a file. Wall-clock and silence
    limits are the runner's (it owns the clock); this looks only at the stream of tool calls.
    """

    def __init__(self, repeat=4, error_streak=6, no_edit=40):
        self.repeat, self.error_streak, self.no_edit = repeat, error_streak, no_edit
        self.recent = deque(maxlen=repeat)
        self.errors = 0
        self.since_edit = 0

    def feed(self, row: dict):
        if row.get("kind") != "tool":
            return None
        self.recent.append(row["sig"])
        self.errors = 0 if row["ok"] else self.errors + 1
        self.since_edit = 0 if (row["tool"] in EDIT_TOOLS and row["ok"]) else self.since_edit + 1
        if len(self.recent) == self.repeat and len(set(self.recent)) == 1:
            return f"the same call {self.repeat} times in a row: {row['tool']} {row['brief']}"
        if self.errors >= self.error_streak:
            return f"{self.errors} failing tool calls in a row (last: {row.get('error', '')[:120]})"
        if self.since_edit >= self.no_edit:
            return f"{self.since_edit} tool calls without changing a file"
        return None
