"""OpenCode's `run --format json` events, reduced to what a person or a reviewer needs to see,
and the detector that decides a run is going nowhere.
"""
import hashlib
import json
import os
import re
import shlex
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


# the words that only wrap the program a simple command runs: `env X=1 probe …`, `time probe …`
WRAPPERS = {"env", "command", "exec", "time", "nice", "nohup", "timeout", "stdbuf"}


def _simple_commands(command: str) -> list[str]:
    """A shell line split at its operators (&& || ; | & newline, $( ` ( )), but not inside
    quotes or at a redirection's & (2>&1, >&2, &>): `grep -E "a|b" f 2>&1` is one command.
    Split blind, the pattern's | made "b" a program and 2>&1's & made "1" one (the probe A/B of
    2026-11-21, #857)."""
    return [part for part, _ in _split_ops(command)]


def _split_ops(command: str) -> list[tuple[str, str]]:
    """_simple_commands' parts, each with the operator that ended it ("" for the last): `(`,
    `)`, `$(` and a backquote are where a subshell starts or ends, which shell_names' cd
    tracking needs (#1210's third review)."""
    parts, cur, quote, i = [], [], "", 0
    while i < len(command):
        c = command[i]
        if quote:
            cur.append(c)
            if c == "\\" and quote == '"' and i + 1 < len(command):
                cur.append(command[i + 1])
                i += 1
            elif c == quote:
                quote = ""
        elif c == "\\" and i + 1 < len(command):
            cur += [c, command[i + 1]]
            i += 1
        elif c in "'\"":
            quote = c
            cur.append(c)
        elif c == "&" and ((cur and cur[-1] == ">") or command[i + 1:i + 2] == ">"):
            cur.append(c)                                  # a redirection, not a separator
        elif c in ";|&\n`()" or command.startswith("$(", i):
            op = command[i:i + 2] if command.startswith(("&&", "||", "$("), i) else c
            parts.append(("".join(cur), op))
            cur = []
            i += len(op) - 1
        else:
            cur.append(c)
        i += 1
    parts.append(("".join(cur), ""))
    return parts


# The words of a shell line that may name a file: split at whitespace, quotes and the shell's
# and a script's punctuation, so `cat > lib/util.py`, `--out=dist/x` and a heredoc's
# open('lib/util.py') each give the path. Kept in a shell row's `names` (runner.shell_wrote).
_NAME_SPLIT = re.compile(r"[\s'\"`;&|<>(){}\[\]$=,:*?!\\]+")
NAMES_MAX = 300   # words a row keeps: a heredoc of a whole module is thousands
NAME_LEN = 300


def shell_names(command: str, limit: int = NAMES_MAX) -> list[str]:
    """The words of a shell line that may name a file it wrote, paths first (a word with `/`,
    then one with `.`), each once, NAMES_MAX of them; a word after a `cd` (or `pushd`) in the
    line joined to its directory, so `cd lib && cat > util.py` gives lib/util.py, and an
    absolute one kept whole (runner._shell_named resolves both against the row's `workdir`).
    Build output under an ignored dist/ was taken for the agent's work and refused at accept,
    though `npm run build` names none of it, while `cat > lib/util.py <<EOF` names its file
    (#1210's review, 2026-11-24). A cd ends with the subshell it ran in (`( … )`, `$( … )`, a
    backquote's) and a `popd` goes back to where its `pushd` was: carried past them, `(cd lib
    && make) && cat > top.py` named lib/top.py, not the ignored top.py it wrote, which accept
    then left out as build output (#1210's third review, 2026-11-24). After a move to where no
    word says (`cd "$D"`, `cd -`, a `popd` with no `pushd` in the line) a word is `*/<word>`: the
    file by that path in any directory. Kept as written, `cd "$D" && cat > util.py` named a
    top-level util.py and the lib/util.py it wrote went unnamed, left out as build output: where
    the directory can't be known, a false name costs a look at accept, a false drop the work.
    ``limit`` words at most (NAMES_MAX for a row of events.jsonl)."""
    words: dict[str, None] = {}
    cwd: str | None = ""                                   # where the line is, from its start
    pushed: list[str | None] = []                          # the line's pushd stack
    outer: list[tuple[str | None, list[str | None]]] = []  # where each open subshell began
    in_backquote = False                                   # inside a backquote's subshell
    for part, op in _split_ops(command or ""):
        here = [w for w in _NAME_SPLIT.split(part)
                if w and len(w) <= NAME_LEN and not w.startswith("-") and re.search(r"[A-Za-z0-9]", w)]
        for w in here:
            words.setdefault(_joined(cwd, w))
        if (moved := _cd_target(part)) is not None:
            how, target = moved
            if how == "popd":
                cwd = pushed.pop() if pushed else None            # `popd >/dev/null` too
            else:
                if how == "pushd":
                    pushed.append(cwd)
                cwd = _joined(cwd, target) if target and cwd is not None else None
        if op in ("(", "$(") or (op == "`" and not in_backquote):
            outer.append((cwd, list(pushed)))
            in_backquote = in_backquote or op == "`"
        elif op == ")" or op == "`":
            if op == "`":
                in_backquote = False
            if outer:
                cwd, pushed = outer.pop()
    bare = {w: w[2:] if w.startswith(ANYWHERE) else w for w in words}
    ranked = ([w for w in words if "/" in bare[w]] + [w for w in words if "/" not in bare[w] and "." in bare[w]]
              + [w for w in words if "/" not in bare[w] and "." not in bare[w]])
    return ranked[:limit]


ANYWHERE = "*/"   # a `names` word run where no word says: the path in any directory (no word holds a *)


def _joined(cwd: str | None, word: str) -> str:
    """A word of a simple command run in `cwd` (relative to the line's start; None after a `cd`
    to where no word says: `cd $D`, `cd -`), as a path from the line's start: under None,
    `*/<word>` (its `..`s dropped), which runner._rows_name matches in any directory."""
    if word.startswith("/"):
        return word
    if cwd is None:
        rest = os.path.normpath(word)
        while rest == ".." or rest.startswith("../"):
            rest = rest[3:]
        return ANYWHERE + rest if rest and rest != "." else word
    if not cwd:
        return word
    return os.path.normpath(os.path.join(cwd, word))


def _cd_target(part: str) -> tuple[str, str] | None:
    """A simple command that moves the line: (`cd`, `pushd` or `popd`, the directory it goes
    to, "" where no word says: `cd`, `cd -`, `cd "$D"`, `cd ~/x`, `pushd +1`, a popd's), or None
    if it isn't one."""
    try:
        words = shlex.split(part)
    except ValueError:
        words = part.split()
    while words and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]):
        words.pop(0)
    if not words or words[0] not in ("cd", "pushd", "popd"):
        return None
    args = [w for w in words[1:] if not w.startswith("-")]
    if words[0] == "popd" or not args or re.search(r"[$~`*?]", args[0]) or args[0].startswith("+"):
        return words[0], ""
    return words[0], args[0]


def shell_programs(command: str) -> list[str]:
    """The program each simple command in a shell line runs, in order, its path and the
    variable assignments before it dropped: `cd x && FOO=1 probe symbols a.py | head` is
    ["cd", "probe", "head"]. Which tools an agent really used (the experiments' use, #857):
    its 90-character brief cut the line short and a tool was found only by searching events."""
    out = []
    for part in _simple_commands(command or ""):
        try:
            words = shlex.split(part)
        except ValueError:                                 # an unclosed quote: its words as they are
            words = part.split()
        while words and (re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]) or words[0] in WRAPPERS):
            wrapper = words.pop(0)
            while wrapper in ("timeout", "nice", "stdbuf") and words and words[0].startswith("-"):
                flag = words.pop(0)
                if flag in ("-k", "-s", "-n") and words:
                    words.pop(0)  # its value
            if wrapper == "timeout" and words and re.match(r"^[0-9.]+[smhd]?$", words[0]):
                words.pop(0)  # the duration
        while words and re.match(r"^[0-9]*[<>]", words[0]):  # a redirection first: `2>/dev/null cmd`
            words.pop(0)
        if words and not words[0].startswith(("-", "#")):
            out.append(os.path.basename(words[0].strip("{}")))
    return out


def _obj(v) -> dict:
    return v if isinstance(v, dict) else {}


def patched(state: dict) -> list[str]:
    """The files a `patch` call wrote: added, updated, or moved to (a file it deleted isn't
    written). OpenCode 2.3.2 gives a GPT model (an id with `gpt-`, not `oss` or `gpt-4`) `patch`
    in place of edit and write, its only input `patchText`: a module one wrote under an ignored
    lib/ had no path in its row, so neither the report's NOTE nor accept's refusal saw it, and
    the commit left it out and the worktree took it (#1148, 2026-11-24). Paths as OpenCode gives
    them: `state.metadata.files[].file`, relative to its directory (the task's workdir) or
    absolute; without the metadata, from patchText's headers."""
    files = _obj(state.get("metadata")).get("files")
    if isinstance(files, list):
        out = [str(f["file"]) for f in files
               if isinstance(f, dict) and f.get("file") and f.get("status") != "deleted"]
        return list(dict.fromkeys(out))
    text = _obj(state.get("input")).get("patchText")
    lines = text.splitlines() if isinstance(text, str) else []
    out = []
    for i, line in enumerate(lines):
        line = line.strip()
        if line.startswith("*** Add File: "):
            out.append(line[14:].strip())
        elif line.startswith("*** Update File: "):
            j = i + 1
            while j < len(lines) and lines[j].strip() == "*** End of File":
                j += 1
            nxt = lines[j].strip() if j < len(lines) else ""
            out.append(nxt[13:].strip() if nxt.startswith("*** Move to: ") else line[17:].strip())
    return list(dict.fromkeys(p for p in out if p))


def written(row: dict) -> list[str]:
    """The paths an edit row wrote: its `path`, or a patch's `paths` (#1148)."""
    if row.get("path"):
        return [str(row["path"])]
    paths = row.get("paths")
    return [str(p) for p in paths] if isinstance(paths, list) else []


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
        elif tool in ("patch", "apply_patch") and (paths := patched(state)):
            row["paths"] = paths
            row["brief"] = row["brief"] or _brief(tool, {"path": paths[0]})
        elif tool == "read" and (inp.get("filePath") or inp.get("path")):
            row["path"] = str(inp.get("filePath") or inp.get("path"))
            if not inp.get("offset") and not inp.get("limit"):
                row["whole"] = True  # the whole file: the runner counts a long one (#857)
        elif tool in ("shell", "bash") and isinstance(inp.get("command"), str):
            row["programs"] = list(dict.fromkeys(shell_programs(inp["command"])))[:8]
            # the words that may name a file it wrote: no edit row shows the shell's (#1210)
            row["names"] = shell_names(inp["command"])
            # where it ran: OpenCode 2.3.2's shell tool tells the model to set `workdir`, not to
            # cd, and `cat > util.py` there wrote lib/util.py, named nowhere (#1210's third review)
            if isinstance(inp.get("workdir"), str) and inp["workdir"]:
                row["workdir"] = inp["workdir"]
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
        # cache_read/cache_write apart beside the sum: an API's price differs for the two, and
        # `cost` only when OpenCode gave a number: a missing price is unknown, never 0 (#1022)
        cost = part.get("cost")
        return {"ts": ts, "round": round_no, "kind": "step",
                "in": n(tok.get("input")), "out": n(tok.get("output")),
                "cache": n(cache.get("read")) + n(cache.get("write")),
                "cache_read": n(cache.get("read")), "cache_write": n(cache.get("write")),
                **({"reasoning": n(tok.get("reasoning"))} if n(tok.get("reasoning")) else {}),
                **({"cost": cost} if isinstance(cost, (int, float))
                   and not isinstance(cost, bool) else {})}
    if kind == "error":
        return {"ts": ts, "round": round_no, "kind": "error", "text": _message(raw.get("error"))}
    if kind == "retry":  # a request OpenCode gave up on and sends again: not the model's output
        attempt = raw.get("attempt")
        return {"ts": ts, "round": round_no, "kind": "retry", "text": _message(raw.get("error")),
                **({"attempt": attempt} if isinstance(attempt, int) else {})}
    return None


def compaction_failed(raw) -> bool:
    """Whether an `error` event is OpenCode's compaction failing: `compaction.failed` in OpenCode
    2.3 ("Compaction produced no summary", "... did not match the required template", "...
    reached the output token limit"), or any error whose message opens "Compaction " (a provider
    blocking the summary, a build that types it otherwise). The session is left over its window
    and can't go on: OpenCode exited 1 on it and the task failed, or exited 0 and auto-fix sent
    the failing tests back to it (2026-11-21, #969)."""
    if not isinstance(raw, dict) or raw.get("type") != "error":
        return False
    err = raw.get("error")
    if isinstance(err, dict) and err.get("type") == "compaction.failed":
        return True
    return _message(err).startswith("Compaction ")


def _message(err) -> str:
    """An event's error, {type, message} in OpenCode 2.3, as its message."""
    err = err or {}
    return str(err.get("message") if isinstance(err, dict) and err.get("message") else err)[:400]


CYCLE = (2, 3)  # the block lengths the repeat guard also counts: A B A B …, A B C A B C … (#1575)


class StuckDetector:
    """Says why a run should be stopped, or None.

    A small model that has lost the thread repeats itself: the same failing command, a string of
    errors, or many calls that read and run things but never change a file. Wall-clock and silence
    limits are the runner's (it owns the clock); this looks only at the stream of tool calls.
    """

    def __init__(self, repeat=4, error_streak=6, no_edit=40, worktree=None, workdir=None):
        self.repeat, self.error_streak, self.no_edit = repeat, error_streak, no_edit
        self.worktree = os.path.realpath(worktree) if worktree else None
        # a relative path is OpenCode's directory's, the package in a monorepo (#1148)
        self.workdir = os.path.realpath(workdir) if workdir else self.worktree
        self.recent = deque(maxlen=repeat)
        # The last calls (signature, what it was), for a block of CYCLE calls repeated `repeat`
        # times; and each path's last write, for a write of the text it already had (#1575)
        self.calls = deque(maxlen=max(CYCLE) * repeat)
        self.wrote = {}
        self.errors = 0
        self.since_edit = 0
        self.guard = None

    def feed(self, row: dict):
        if row.get("kind") != "tool":
            return None
        self.recent.append(row["sig"])
        self.calls.append((row["sig"], f"{row['tool']} {row['brief']}".strip()))
        self.errors = 0 if row["ok"] else self.errors + 1
        changed = row["tool"] in EDIT_TOOLS and row["ok"] and any(
            self._ours(p) and self.wrote.get(self._key(p)) != row["sig"] for p in written(row) or [None])
        if row["tool"] in EDIT_TOOLS and row["ok"]:
            self.wrote.update((self._key(p), row["sig"]) for p in written(row))
        self.since_edit = 0 if changed else self.since_edit + 1
        # .guard names the one that fired, for the Stats tab's "what stops agents" (#404)
        if len(self.recent) == self.repeat and len(set(self.recent)) == 1:
            self.guard = "repeat"
            return f"the same call {self.repeat} times in a row: {row['tool']} {row['brief']}"
        # A loop of two calls, the same text written to answer.txt then the same failing test,
        # passed every guard: each write reset the explore count and no one call came 4 times in
        # a row, so 800 calls a round ran to its 40-minute wall, three rounds (#1575, 2026-11-26)
        for k in CYCLE:
            last = [sig for sig, _ in list(self.calls)[-k * self.repeat:]]
            if len(last) == k * self.repeat and last == last[-k:] * self.repeat and len(set(last)) > 1:
                self.guard = "repeat"
                what = "; ".join(label for _, label in list(self.calls)[-k:])
                return f"the same {k} calls {self.repeat} times in a row: {what}"
        if self.errors >= self.error_streak:
            self.guard = "errors"
            return f"{self.errors} failing tool calls in a row (last: {row.get('error', '')[:120]})"
        if self.since_edit >= self.no_edit:
            self.guard = "no_edit"
            return f"{self.since_edit} tool calls without changing a file in the task"
        return None

    def _key(self, path) -> str:
        return os.path.realpath(os.path.join(self.workdir or "", path)) if path else ""

    def _ours(self, path) -> bool:
        """Whether an edit was to the task's files. Scratch files elsewhere aren't progress: an
        agent sure the compiler was broken wrote 20 minutes of test programs in /tmp, each write
        resetting the count, while its own change still failed its test (2026-11-18)."""
        if not (self.worktree and path):
            return True
        full = os.path.realpath(os.path.join(self.workdir, path))
        return full == self.worktree or full.startswith(self.worktree + os.sep)
