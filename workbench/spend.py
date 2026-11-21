"""What a task cost Claude: its share of the Claude Code session that ran it, from the session's
transcript. The pitch is that Claude's tokens are the scarce resource; this makes it a number.

A turn (one prompt to the next) is charged to the tasks it names: a `[workbench] Task `<name>``
wake, or a `wb … <name>` in one of its Bash calls. Only tasks this session started (task.json
`session`) count, and a turn naming several splits evenly. A subagent's transcript is charged
the same way, in the turn it ran in, and its calls that name no task as the session's call before
them. Planning before the spec, or talk that names no task, is charged to none.
"""
import bisect
import itertools
import re
import shlex
from datetime import datetime
from pathlib import Path

from . import context, store

# The mod's wake opening, at a prompt's start only, after the line Claude Code puts before a
# plugin's message: matched anywhere, a summary quoting one charged later calls to it (#557)
WAKE = re.compile(r"\s*(?:The [\w .-]{1,60} plugin sent a message:\s*)?"
                  r"\[workbench\] Task `([a-z0-9][a-z0-9-]{0,47})`")
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
    """A user row's text when it starts a turn; None for a tool result (within one), and for
    the rows Claude Code adds inside a turn (isMeta: an image a Read showed, a skill's body) or
    after a compaction (isCompactSummary): taken for new turns, they once ended the task's
    charge mid-review (#557)."""
    if row.get("type") != "user" or row.get("isMeta") or row.get("isCompactSummary"):
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
    specs it handed over (`wb start|next --spec`), and the tasks a `wb next` names (`nexts`)."""
    out = {"hit": set(), "shut": set(), "wb": False, "edits": set(), "specs": set(), "nexts": set()}
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
            if words and words[0] == "next":
                out["nexts"] |= these
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
        path.startswith(str(store.Task(t).worktree) + "/") for t in tasks)


def _usage(row: dict) -> dict:
    u = (row.get("message") or {}).get("usage") or {}
    split = u.get("cache_creation") or {}
    one_h = int(split.get("ephemeral_1h_input_tokens") or 0)
    return {"input": int(u.get("input_tokens") or 0),
            "cache_write_5m": max(0, int(u.get("cache_creation_input_tokens") or 0) - one_h),
            "cache_write_1h": one_h,
            "cache_read": int(u.get("cache_read_input_tokens") or 0),
            "output": int(u.get("output_tokens") or 0)}


def _events(rows: list[dict], names: set[str]) -> list:
    """A transcript's turns and calls in order: a set (a prompt, and the task its wake names) or
    a call's dict (what it does, `_named`, its time and usage), one per API call."""
    events, by_id = [], {}  # an assistant message is a row per content block, each with its usage
    for row in rows:
        if (text := _prompt(row)) is not None:
            events.append({m.group(1)} & names if (m := WAKE.match(text)) else set())
        elif row.get("type") == "assistant":
            does = _named(row, names)
            mid = (row.get("message") or {}).get("id") or ("row", len(events))  # not deduplicated
            if mid in by_id:
                for k in ("hit", "shut", "edits", "specs", "nexts"):
                    by_id[mid][k] |= does[k]
                by_id[mid]["wb"] |= does["wb"]
                continue
            by_id[mid] = {"id": mid, "at": _ts(row), "usage": _usage(row), **does}
            events.append(by_id[mid])
    return events


def _charge(events: list) -> list[dict]:
    """One thread's calls (a session's, or one subagent's), each with the tasks it is charged to
    (`calls` says how)."""
    out, turn, current, waiting = [], 0, set(), []
    for ev in events:
        if isinstance(ev, set):  # a prompt: a new turn, the wake's task, or none yet
            turn, current, waiting = turn + 1, set(ev), []
            continue
        c = {"id": ev["id"], "at": ev["at"], "turn": turn, "names": set(ev["hit"] or current),
             "usage": ev["usage"], "ahead": set(ev["nexts"])}
        out.append(c)
        if ev["hit"]:
            # the calls just before it are the reading and the spec for it, back to the last
            # one that ran `wb` or edited something else: a long turn of other work that ends
            # in `wb start` put all of that work on the task (82K output, 2026-11-17)
            for w, wev in reversed(waiting):
                if wev["wb"] or not all(_theirs(p, ev["hit"], ev["specs"]) for p in wev["edits"]):
                    break
                w["names"], w["ahead"] = set(ev["hit"]), set(ev["nexts"])
            waiting, current = [], ev["hit"] - ev["shut"]
        elif current and all(_theirs(p, current, set()) for p in ev["edits"]):
            pass  # still the task's: reading its diff, its worktree, writing its feedback
        else:  # another file edited: other work, until a `wb` names a task again
            c["names"], current = set(), set()
            waiting.append((c, ev))
    return out


def calls(main: Path, subagents: list[Path], names: set[str]) -> list[dict]:
    """Every API call of the session, {id, at, turn, names, usage, ahead}, `names` being the tasks
    it is charged to and `ahead` those of them it hands a next step (a `wb next` call and the spec
    charged with it). A call that runs `wb … <task>` is that task's, and so are the calls after it in
    the turn, until another `wb` names other tasks or `wb accept|discard` closes it; the calls
    just before a turn's first such `wb` are its tasks' too (the reading and the spec before
    `wb start`), back to the last call that ran `wb` or edited a file other than its `--spec`;
    a wake's turn is the wake's task's. An edit to a file outside the tasks' worktrees, and not
    one handed to `wb` (`HANDED`), ends the charge: that is other work.
    Charging whole turns was wrong: one long turn that also ran a task put 5.5M cached tokens
    of other work on it (rel090-check, 2026-11-17).
    Each subagent's transcript is a thread of its own, charged the same way (its prompt starts
    its turn), in the turn of the session's call before it; a subagent call that names no task
    is charged as that session call. Charged only as the session's call before it, a task a
    subagent started, reviewed and accepted, under a turn whose one call was the Agent tool's,
    cost Claude nothing (#718)."""
    out = _charge(_events(store.read_jsonl(main)[0], names))
    seen = {c["id"] for c in out}
    # the session's call before a time: the latest in the transcript of those made at or before
    # it (a prefix max over them sorted by time, since times are almost but not quite in order),
    # by bisection: a scan of every session call per subagent row took 6.6 s at 8,000 calls and
    # 30,000 subagent rows, on every accept and discard (#728)
    order = sorted((c["at"], j) for j, c in enumerate(out) if c["at"] is not None)
    times, latest = [t for t, _ in order], list(itertools.accumulate((j for _, j in order), max))
    for path in subagents:
        rows = [r for r in store.read_jsonl(path)[0]
                if r.get("type") != "assistant" or (_ts(r) is not None
                                                    and (r.get("message") or {}).get("id") not in seen)]
        for c in _charge(_events(rows, names)):
            seen.add(c["id"])
            k = bisect.bisect_right(times, c["at"])
            parent = out[latest[k - 1]] if k else None
            if not c["names"]:
                if not (parent and parent["names"]):
                    continue
                c["names"], c["ahead"] = set(parent["names"]), set(parent["ahead"])
            c["turn"] = parent["turn"] if parent else f"{path.name} {c['turn']}"
            out.append(c)
    return out


def sessions(task: store.Task) -> list[str]:
    """The Claude Code sessions the task's Claude worked in: the one `wb start` recorded, then
    each one its process had since (a /clear starts another: store.note_claude_session, #451),
    then each one a `wb` call naming the task ran in (another process that adopted it after a
    restart: store.note_task_session, #719)."""
    sid = task.spec.get("session")
    if not sid:
        return []
    later = store.claude_sessions(task.spec.get("claude_pid"), task.spec.get("created_at") or 0,
                                  task.spec.get("claude_start"))  # its process's, not the pid's (#787)
    return list(dict.fromkeys([sid, *later, *store.task_sessions(task)]))


def usage(task: store.Task) -> dict | None:
    """The task's share of its sessions: {turns, calls, input, cache_write_5m,
    cache_write_1h, cache_read, output}; None without a session or any of its transcripts.
    ``calls`` is the task's share of each charged call, each counting 1/len(names)."""
    return usage_by_step(task)[0]


def _step_bounds(task: store.Task) -> list[float]:
    """When each `wb next` handed the task its next step (its `next_step` events), in order."""
    return sorted(float(e["ts"]) for e in store.read_jsonl(task.dir / "events.jsonl")[0]
                  if e.get("kind") == "next_step" and isinstance(e.get("ts"), (int, float)))


def usage_by_step(task: store.Task) -> tuple[dict | None, list[dict] | None]:
    """``usage`` for the whole task, and the same for each of its steps (#558) when it had more
    than one and every call charged to it has a timestamp: a call is the step under way when it
    was made (the `next_step` events mark where each began), except that a `wb next` call and
    the calls charged with it as its spec (`ahead`) are the step it hands over. So a step's cost
    is its spec, its reviews and its feedback; step 1's also has the planning before `wb start`,
    the last step's the accept. Every session the task's Claude worked in counts (`sessions`: a
    /clear starts another, #451), a turn per session."""
    sids = sessions(task)
    found = [(s, *transcript_paths(s)) for s in sids]
    found = [(s, main, subs) for s, main, subs in found if main is not None]
    if not found:
        return None, None
    names = {t.name for t in store.all_tasks() if t.spec.get("session") in sids} | {task.name}
    try:
        charged, seen = [], set()
        for s, main, subs in found:
            for c in calls(main, subs, names):
                # a resumed or forked session's transcript can copy the history it came from:
                # a call is counted once, by its message id (#719)
                if isinstance(c["id"], str) and c["id"] in seen:
                    continue
                seen.add(c["id"])
                if task.name in c["names"]:
                    charged.append({**c, "turn": (s, c["turn"])})
    except OSError:  # unreadable: a measurement never stops an accept
        return None, None
    bounds = _step_bounds(task)
    per = None
    if bounds and all(c["at"] is not None for c in charged):
        per = [[] for _ in range(len(bounds) + 1)]
        for c in charged:
            i = sum(b <= c["at"] for b in bounds) + (task.name in c.get("ahead", ()))
            per[min(i, len(bounds))].append(c)  # a `wb next` that failed hands nothing over
    if not charged:  # found, but no call names it: unmeasured, not a measured 0 (#718)
        return None, None
    return _sum(charged), None if per is None else [_sum(cs) for cs in per]


def _sum(charged: list[dict]) -> dict:
    total, turns, call_count = dict.fromkeys(KINDS, 0.0), set(), 0.0
    for c in charged:
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
