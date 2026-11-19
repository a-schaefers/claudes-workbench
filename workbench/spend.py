"""What a task cost Claude: its share of the Claude Code session that ran it, from the session's
transcript. The pitch is that Claude's tokens are the scarce resource; this makes it a number.

A turn (one prompt to the next) is charged to the tasks it names: a `[workbench] Task `<name>``
wake, or a `wb … <name>` in one of its Bash calls. Only tasks this session started (task.json
`session`) count, and a turn naming several splits evenly. Subagents' rows count toward the
turn they ran in. Planning before the spec, or talk that names no task, is charged to none.
"""
import re
import shlex
from datetime import datetime
from pathlib import Path

from . import context, store

WAKE = re.compile(r"\[workbench\] Task `([a-z0-9][a-z0-9-]{0,47})`")  # the mod's wake opening
KINDS = ("input", "cache_write_5m", "cache_write_1h", "cache_read", "output")
# Input-equivalent weights: the API's multipliers on the base input price (claude-api skill,
# pricing, cached 2026-10-06): output 5x on every current model, a cache write 1.25x (5 minutes)
# or 2x (1 hour). A cache read is 0.1x on most models and less on some (0.05x, 0.025x); no model
# is named here, so 0.1x, which errs high. The row keeps the raw counts.
WEIGHTS = {"input": 1, "cache_write_5m": 1.25, "cache_write_1h": 2, "cache_read": 0.1, "output": 5}


def transcript_paths(session: str) -> tuple[Path | None, list[Path]]:
    """The session's transcript and its subagents' (none when Claude Code has cleaned it up)."""
    root = context.claude_home() / "projects"
    if not session or "/" in session or not root.is_dir():
        return None, []
    main = next(iter(sorted(root.glob(f"*/{session}.jsonl"))), None)
    if main is None:
        return None, []
    return main, sorted((main.parent / session / "subagents").glob("**/*.jsonl"))


def _ts(row: dict) -> float | None:
    try:
        return datetime.fromisoformat(row["timestamp"]).timestamp()
    except (KeyError, TypeError, ValueError):
        return None


def _prompt(row: dict) -> str | None:
    """A user row's text when it starts a turn; None for a tool result (within one)."""
    if row.get("type") != "user":
        return None
    content = (row.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list) and not any(isinstance(c, dict) and c.get("type") == "tool_result"
                                             for c in content):
        return " ".join(c.get("text", "") for c in content if isinstance(c, dict))
    return None


def _wb_calls(command: str) -> list[list[str]]:
    """Each `wb` a shell command runs, as its words: `cd x && wb diff t | head` gives [[diff, t]]."""
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        words = list(lex)
    except ValueError:  # an unclosed quote
        words = command.split()
    calls, on = [], False
    for w in words:
        if w and set(w) <= set("();<>|&"):
            on = False
        elif on:
            calls[-1].append(w)
        elif Path(w).name == "wb":
            on = True
            calls.append([])
    return calls


CLOSING = {"accept", "discard"}  # past these, the session's work is no longer the task's


EDITS = ("Edit", "Write", "NotebookEdit")  # Claude Code's tools that change a file


def _named(row: dict, names: set[str]) -> dict:
    """What an assistant message does that charging needs: the session's tasks its `wb` calls
    name (`hit`) and close (`shut`), whether it ran `wb` at all, the files it edited, and the
    specs it handed over (`wb start|next --spec`)."""
    out = {"hit": set(), "shut": set(), "wb": False, "edits": set(), "specs": set()}
    for c in (row.get("message") or {}).get("content") or []:
        if not (isinstance(c, dict) and c.get("type") == "tool_use"):
            continue
        given = c.get("input") or {}
        if c.get("name") in EDITS:
            out["edits"].add(str(given.get("file_path") or given.get("notebook_path") or ""))
        if c.get("name") != "Bash":
            continue
        for words in _wb_calls(str(given.get("command", ""))):
            out["wb"] = True
            these = set(words) & names
            out["hit"] |= these
            if words and words[0] in CLOSING:
                out["shut"] |= these
            for i, w in enumerate(words):
                flag, _, val = w.partition("=")
                if flag in HANDED:
                    if val:
                        out["specs"].add(val)
                    elif i + 1 < len(words):
                        out["specs"].add(words[i + 1])
    return out


HANDED = ("--spec", "--message", "--file")  # files Claude writes for wb to hand over


def _theirs(path: str, tasks: set[str], handed: set[str]) -> bool:
    """Whether an edit is the tasks' work: in one's worktree (a takeover), or a file handed to
    `wb` (a spec, feedback, an answer)."""
    return any(path == s or path.endswith("/" + s.removeprefix("./")) for s in handed) or any(
        path.startswith(str(store.task_dir(t) / "worktree") + "/") for t in tasks)


def _usage(row: dict) -> dict:
    u = (row.get("message") or {}).get("usage") or {}
    split = u.get("cache_creation") or {}
    one_h = int(split.get("ephemeral_1h_input_tokens") or 0)
    return {"input": int(u.get("input_tokens") or 0),
            "cache_write_5m": max(0, int(u.get("cache_creation_input_tokens") or 0) - one_h),
            "cache_write_1h": one_h,
            "cache_read": int(u.get("cache_read_input_tokens") or 0),
            "output": int(u.get("output_tokens") or 0)}


def calls(main: Path, subagents: list[Path], names: set[str]) -> list[dict]:
    """Every API call of the session, {at, turn, names, usage}, `names` being the tasks it is
    charged to. A call that runs `wb … <task>` is that task's, and so are the calls after it in
    the turn, until another `wb` names other tasks or `wb accept|discard` closes it; the calls
    just before a turn's first such `wb` are its tasks' too (the reading and the spec before
    `wb start`), back to the last call that ran `wb` or edited a file other than its `--spec`;
    a wake's turn is the wake's task's. An edit to a file outside the tasks' worktrees, and not
    one handed to `wb` (`HANDED`), ends the charge: that is other work. A subagent's call is
    charged as the session's call before it.
    Charging whole turns was wrong: one long turn that also ran a task put 5.5M cached tokens
    of other work on it (rel090-check, 2026-11-17)."""
    events, by_id = [], {}  # an assistant message is a row per content block, each with its usage
    for row in store.read_jsonl(main)[0]:
        if (text := _prompt(row)) is not None:
            events.append(set(WAKE.findall(text)) & names)
        elif row.get("type") == "assistant":
            does = _named(row, names)
            mid = (row.get("message") or {}).get("id") or f"row {len(events)}"
            if mid in by_id:
                for k in ("hit", "shut", "edits", "specs"):
                    by_id[mid][k] |= does[k]
                by_id[mid]["wb"] |= does["wb"]
                continue
            by_id[mid] = {"at": _ts(row), "usage": _usage(row), **does}
            events.append(by_id[mid])
    out, turn, current, waiting = [], 0, set(), []
    for ev in events:
        if isinstance(ev, set):  # a prompt: a new turn, the wake's task, or none yet
            turn, current, waiting = turn + 1, set(ev), []
            continue
        c = {"at": ev["at"], "turn": turn, "names": set(ev["hit"] or current), "usage": ev["usage"]}
        out.append(c)
        if ev["hit"]:
            # the calls just before it are the reading and the spec for it, back to the last
            # one that ran `wb` or edited something else: a long turn of other work that ends
            # in `wb start` put all of that work on the task (82K output, 2026-11-17)
            for w, wev in reversed(waiting):
                if wev["wb"] or not all(_theirs(p, ev["hit"], ev["specs"]) for p in wev["edits"]):
                    break
                w["names"] = set(ev["hit"])
            waiting, current = [], ev["hit"] - ev["shut"]
        elif current and all(_theirs(p, current, set()) for p in ev["edits"]):
            pass  # still the task's: reading its diff, its worktree, writing its feedback
        else:  # another file edited: other work, until a `wb` names a task again
            c["names"], current = set(), set()
            waiting.append((c, ev))
    seen = set(by_id)
    starts = [c["at"] for c in out]
    for path in subagents:
        for row in store.read_jsonl(path)[0]:
            if row.get("type") != "assistant" or (at := _ts(row)) is None:
                continue
            mid = (row.get("message") or {}).get("id")
            if mid in seen:
                continue
            seen.add(mid)
            i = max((j for j, s in enumerate(starts) if s is not None and s <= at), default=None)
            if i is not None and out[i]["names"]:
                out.append({"at": at, "turn": out[i]["turn"], "names": set(out[i]["names"]),
                            "usage": _usage(row)})
    return out


def usage(task: store.Task) -> dict | None:
    """The task's share of its session: {turns, calls, input, cache_write_5m,
    cache_write_1h, cache_read, output}; None without a session or its transcript.
    ``calls`` is the task's share of each charged call, each counting 1/len(names)."""
    sid = task.spec.get("session")
    main, subs = transcript_paths(sid) if sid else (None, [])
    if main is None:
        return None
    names = {t.name for t in store.all_tasks() if t.spec.get("session") == sid} | {task.name}
    total, turns, call_count = dict.fromkeys(KINDS, 0.0), set(), 0.0
    try:
        charged = calls(main, subs, names)
    except OSError:  # unreadable: a measurement never stops an accept
        return None
    for c in charged:
        if task.name in c["names"]:
            turns.add(c["turn"])
            call_count += 1 / len(c["names"])
            for k in KINDS:
                total[k] += c["usage"][k] / len(c["names"])
    return {"turns": len(turns), "calls": round(call_count, 1),
            **{k: round(v) for k, v in total.items()}}


def weighted(u: dict) -> int:
    """One number for a usage: input-equivalent tokens (WEIGHTS)."""
    return round(sum(WEIGHTS[k] * (u.get(k) or 0) for k in KINDS))


def line(u: dict | None) -> str | None:
    if not u:
        return None
    return (f"claude: ~{weighted(u) / 1000:.0f}K input-equivalent tokens over {u['turns']} "
            f"turn(s) ({u['output']} output)")
