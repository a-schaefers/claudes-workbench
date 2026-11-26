"""MCP server (stdio, newline-delimited JSON-RPC) giving the local agent ask_reviewer, and a report
task its submit_result too (#1278).

OpenCode starts it for each round (each `opencode run`) through the task's config layer, with
WB_TASK naming the task, inside the agent's lane. A call writes the question to ask/<id>.json,
the one place in the task's directory the lane may write, and blocks until Claude's answer file
appears or the wait times out. The runner, outside the lane, relays it (relay): it checks the
file, counts it against the task's budgets, raises the "question" notice (the Claude Code mod
wakes Claude with it; `wb wait` returns on it) and keeps the phase `asking` while any question is
open. The server once set the status and wrote notify.jsonl itself, which put both in the lane,
where the agent could forge any task's notice and its own spec (the 2026-11-16 audit): nothing
the agent writes is trusted outside it now. A question is served on a thread of its own, so the
server answers pings while it waits.
"""
import json
import os
import re
import secrets
import shlex
import stat
import sys
import threading
import time
import uuid

from . import __version__, schema, store
from .watch import Watcher

PROTOCOL = "2025-06-18"
LISTED = ".listed"  # in ask/: written when OpenCode lists the tool (a newer OpenCode might not)
TOOL = {
    "name": "ask_reviewer",
    "description": (
        "Ask the senior reviewer (Claude) ONE blocking question when the spec is genuinely "
        "ambiguous or contradictory and a wrong guess would waste the whole round. Do not ask "
        "for permission, for reassurance, or about things you can find out by reading the code "
        "or running it. The question budget is small. Waits for the answer, which can take "
        "minutes. Also how to run a shell command that was refused (\"Permission denied: "
        "shell\"): put it in `command` and say why in `question`; or a directory outside your "
        "lane (\"Permission denied\" writing it, or \"Permission denied: external_directory\"): "
        "put it in `path`; or a TCP port whose connect fails with \"Permission denied\": put it "
        "in `port`. Claude allows it or suggests another way, and these don't use the "
        "question budget."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "The question, self-contained."},
            "context": {"type": "string",
                        "description": "What you found and the options you are choosing between."},
            "command": {"type": "string",
                        "description": "Only for a refused shell command: the exact command."},
            "path": {"type": "string",
                     "description": "Only for a refused directory: its absolute path."},
            "port": {"type": "integer",
                     "description": "Only for a refused TCP connect: the port number."},
        },
        "required": ["question"],
    },
}


# A report task's second tool (#1278): its result, checked against the task's schema, which is the
# tool's inputSchema too, so OpenCode validates the arguments first and hands its errors back to
# the model (its tool runtime, 2.3.2). The server checks again (schema.check) and writes the object
# to ask/result.json, the lane's one place; the runner, outside it, reads it and checks it a
# third time (runner.result_of): nothing the agent writes is trusted outside the lane.
SUBMIT = "submit_result"
SUBMIT_DESCRIPTION = ("Send your task's result: the whole JSON object your task's schema asks for, "
                      "once, when you are done. Errors come back to you; fix them and call again "
                      "with the whole object.")
RESULT = "result.json"  # in ask/
SUBMITTED = ("Result received. You are done: end your turn now with a one-line summary, and "
             "don't call this again.")


def schema_file(task: store.Task):
    """A report task's schema, as wb start wrote it in its rules (cli.SCHEMA_FILE)."""
    return store.home() / "rules" / task.name / "schema.json"


def submit_tool(task: store.Task) -> dict | None:
    """submit_result for a report task whose schema reads, else None (a code task has only
    ask_reviewer)."""
    if not task.spec.get("report"):
        return None
    try:
        doc = json.loads(schema_file(task).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if schema.valid(doc):
        return None
    return {"name": SUBMIT, "description": SUBMIT_DESCRIPTION, "inputSchema": doc}


def submit(task: store.Task, args) -> tuple[bool, str]:
    """A submit_result call: (taken, the text the agent gets back)."""
    tool = submit_tool(task)
    if tool is None:
        return False, "submit_result: this task has no result schema"
    if not isinstance(args, dict):
        return False, "rejected: the arguments are one JSON object, the result itself"
    errs = schema.check(args, tool["inputSchema"])
    if errs:
        return False, ("rejected: " + "; ".join(errs) + ". Call submit_result again with the whole "
                       "object.")
    store.write_json(ask_dir(task) / RESULT, {"ts": time.time(), "result": args})
    return True, SUBMITTED


PERMISSION_BUDGET = 8  # requests for a refused command, directory or port, per task: a cap on a stuck loop
QID = re.compile(r"^[0-9a-f]{8,32}$")
LINE_MAX = 4096       # characters of a `command` or `path`: one line, never clipped (Claude copies it)
QUESTION_CHARS = 2000  # of the question in Claude's notice; the ask file keeps the whole of it
CONTEXT_CHARS = 4000   # of the context: a model pasting a whole log cost Claude 50K tokens a wake
FILE_MAX = 256 * 1024  # bytes of an ask file the runner reads; a longer one is refused
NEW_MAX = 20           # new ask files the runner looks at in one tick
ASKS_MAX = 200         # ask files it counts in all (status `asks`); past that it looks at none
BUDGET_USED = ("The question budget for this task is used up. Make the most reasonable "
               "assumption, carry on, and state the assumption in your final summary.")
PERMISSIONS_USED = (f"You have asked for {PERMISSION_BUDGET} refused commands, directories or ports "
                    "already. Work inside your lane with the allowed tools, and list what you "
                    "couldn't do in your final summary.")
NO_QUESTION = "Ask with a `question`, and ask again."
NO_ANSWER = ("No answer arrived in time. Make the most reasonable assumption, carry on, and state "
             "the assumption in your final summary.")


# A question's wait runs on the monotonic clock, less any step longer than FROZEN_SECS (runner's
# too: a frozen process, a host's sleep that the monotonic clock still counted, as WSL2's and
# macOS's may). On the wall clock a laptop suspended past the ask timeout woke to its question
# expired while every round clock had paused (#453, the 2026-11-20 audit).
FROZEN_SECS = 60
STEP_SECS = 30  # the server's longest wait between looks: well inside FROZEN_SECS


class Waited:
    """Seconds waited so far, a suspend left out."""

    def __init__(self):
        self.tick, self.secs = time.monotonic(), 0.0

    def step(self) -> float:
        t = time.monotonic()
        dt, self.tick = t - self.tick, t
        if 0 <= dt <= FROZEN_SECS:
            self.secs += dt
        return self.secs


# The runner's wait on each relayed question, by task directory and id: `opened` holds its mark
_WAITS: dict[tuple[str, str], Waited] = {}


def _permission(q: dict) -> bool:
    return bool(q.get("command") or q.get("path") or q.get("port"))


def ask_dir(task: store.Task):
    return task.dir / "ask"


def bad_request(command: str, path: str, port: str = "") -> str | None:
    """Why a `command`, `path` or `port` can't go to Claude as it is, or None. One line, so it
    can't write lines of its own into Claude's notice; an absolute path, which `wb allow --path`
    would otherwise resolve against Claude's working directory (the 2026-11-16 audit); a port
    as digits, which the notice prints (#1197)."""
    for name, v in (("command", command), ("path", path)):
        if v and (len(v) > LINE_MAX or any(c in v for c in "\n\r\0")):
            return f"Give `{name}` as one line of at most {LINE_MAX} characters, and ask again."
    if path and not os.path.isabs(path):
        return "Give `path` as one absolute directory path, and ask again."
    if port and not (port.isascii() and port.isdigit() and 0 < int(port) < 65536):
        return "Give `port` as one TCP port number (1-65535), and ask again."
    return None


def ask(task: store.Task, question: str, context: str = "", command: str = "",
        path: str = "", port: str = "") -> str:
    if why := bad_request(command, path, port):
        return why
    if not (question or "").strip():  # as the runner judges it (_judge): never an "x" file (#831)
        return NO_QUESTION
    spec = task.spec
    # The runner counts them too, outside the lane (relay): this is a courtesy, counted as it
    # counts, from status `asks` (readable in the lane), and a file not judged yet by what it
    # holds. Counting every file charged the ones it refused ("x": an empty question,
    # close_all's strays), so a question the runner would relay got BUDGET_USED (#831).
    asks = task.status.get("asks") or {}
    kinds = [k for k in asks.values() if k in ("q", "p")]
    kinds += ["p" if _permission(q) else "q" for n in _names(task) if n[:-5] not in asks
              and (q := read_question(task, n[:-5]))]
    if command or path or port:
        if kinds.count("p") >= PERMISSION_BUDGET:
            return PERMISSIONS_USED
    elif kinds.count("q") >= int(spec.get("ask_budget", 2)):
        return BUDGET_USED
    qid = uuid.uuid4().hex[:8]
    qfile, afile = ask_dir(task) / f"{qid}.json", ask_dir(task) / f"{qid}.answer"
    store.write_json(qfile, {"id": qid, "ts": store.now(), "round": task.status.get("round"),
                             "question": question, "context": context,
                             **({"command": command} if command else {}),
                             **({"path": path} if path else {}),
                             **({"port": int(port)} if port else {})})
    limit, waited = float(spec.get("ask_timeout", 1200)), Waited()
    watcher = Watcher(afile.parent)  # wakes the moment `wb answer` writes the file
    try:
        while waited.step() < limit:
            answer = _read(afile)  # written whole (write_answer): there means done
            if answer is not None:
                return answer.strip()
            watcher.wait(max(0.0, min(STEP_SECS, limit - waited.secs)))
    finally:
        watcher.close()
    return NO_ANSWER


# --- the runner's side, outside the lane ---------------------------------------------------------

def _names(task: store.Task) -> list[str]:
    try:
        return sorted(n for n in os.listdir(ask_dir(task))
                      if n.endswith(".json") and QID.match(n[:-5]))
    except OSError:
        return []


def _read(path, limit: int = FILE_MAX) -> str | None:
    """A regular file's text, never through a link or a FIFO the agent planted in ask/, and
    None past `limit` bytes."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        data = b""
        while len(data) <= limit and (chunk := os.read(fd, limit + 1 - len(data))):
            data += chunk
    finally:
        os.close(fd)
    return None if len(data) > limit else data.decode(errors="replace")


def read_question(task: store.Task, qid: str) -> dict | None:
    """ask/<qid>.json as the agent wrote it, its fields as strings, or None."""
    try:
        q = store.loads(_read(ask_dir(task) / f"{qid}.json") or "")  # the runner writes it on (#525)
    except ValueError:
        return None
    if not isinstance(q, dict):
        return None
    return {**q, **{k: str(q.get(k) or "") for k in ("question", "context", "command", "path",
                                                     "port")}}


def too_long(text: str) -> str:
    """Why an answer can't be written, or "": past FILE_MAX the server reads none of it, so the
    agent waited out its ask timeout for NO_ANSWER while wb said "answered" (#605)."""
    size = len((text.strip() + "\n").encode())
    if size <= FILE_MAX:
        return ""
    return (f"the answer is {size // 1024} KiB, over the {FILE_MAX // 1024} KiB the agent can be "
            "sent: put the material in a file in the task's worktree and answer with its path")


def write_answer(task: store.Task, qid: str, text: str) -> None:
    """ask/<qid>.answer, whole at once (the ask server watches for it: never half a file) and
    never through a link: ask/ is in the agent's lane, and a link planted there once made wb
    write outside it (the 2026-11-16 audit). A new temporary name, then a rename over the
    answer's, which replaces a link instead of following it."""
    if not QID.match(qid):
        raise RuntimeError(f"no question {qid}")
    if why := too_long(text):
        raise RuntimeError(why)
    d = os.open(ask_dir(task), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        tmp = f".{qid}.{secrets.token_hex(6)}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o644, dir_fd=d)
        with os.fdopen(fd, "w") as fh:
            fh.write(text.strip() + "\n")
        os.replace(tmp, f"{qid}.answer", src_dir_fd=d, dst_dir_fd=d)
    finally:
        os.close(d)


def answer_once(task: store.Task, qid: str, text: str) -> bool:
    """Answer a question still open, as one step: False (nothing written) when another answer or
    the runner's NO_ANSWER got there first. is_open and write_answer apart let two `wb answer`s
    (or `wb answer` and `wb allow`) both say "answered", the second replacing the first after the
    server had returned it, and an answer racing close() overwrite NO_ANSWER and be counted
    answered though the agent got none (#938, the outside review of 1.90.24). The lock is in
    $WB_HOME/locks (store.task_lock), which no lane reads: an agent can't hold it."""
    with store.locked(store.task_lock(task.name, "ask")):
        if not is_open(task, qid):
            return False
        write_answer(task, qid, text)
        return True


def is_open(task: store.Task, qid: str) -> bool:
    """Asked and not answered, as the mod's asking() judges it: ask/<qid>.json and no answer
    (read as the server reads it, so a link planted at the answer's name isn't one)."""
    d = ask_dir(task)
    return bool(QID.match(qid)) and (d / f"{qid}.json").exists() and _read(d / f"{qid}.answer") is None


def close(task: store.Task, opened: dict) -> None:
    """Answer the questions the runner stops relaying (timed out, or their round ended) with
    NO_ANSWER: nobody reads it, but a question left without an answer looked open for good, so
    the mod woke Claude for it and `wb answer` reported an answer nobody would read (the 0.90
    review). The server's own words, should one still be waiting a moment; lessons skip it."""
    for qid in list(opened):
        _WAITS.pop((str(task.dir), qid), None)
        try:
            answer_once(task, qid, NO_ANSWER)
        except OSError:
            pass
        del opened[qid]


def close_all(task: store.Task) -> None:
    """Close every question the task has relayed (status `asks`) that is still open: called once
    its round's agent is gone, however the round ended, since no server outlives its round. A
    stop or a signal unwound past run_round's close, and `wb answer` said "answered" to nobody;
    after a resume `wb allow` could answer the dead request (the 0.90 review). And every ask file
    the runner never relayed (written in the tick before the round ended, or after a runner died
    while its agent lived on, off Linux): answered and counted as refused ("x"), or the next
    round's first relay raised it as live, woke Claude for it and held the phase `asking`,
    pausing every guard, for a question nobody waited on (#680)."""
    asks = task.status.get("asks") or {}
    stray = [n[:-5] for n in _names(task) if n[:-5] not in asks]
    close(task, dict.fromkeys([*asks, *stray]))
    if stray:
        task.set_status(asks={**(task.status.get("asks") or {}), **dict.fromkeys(stray, "x")})


def used(task: store.Task) -> int:
    """The plain questions the task has asked, as the runner counted them."""
    return sum(1 for k in (task.status.get("asks") or {}).values() if k == "q")


def _quote(text: str, limit: int, qfile) -> str:
    """The agent's words in Claude's notice: clipped, and every line after the first marked as
    the agent's, so none can pass for workbench's own."""
    text = text.strip()
    if len(text) > limit:
        text = text[:limit] + f"… ({len(text) - limit} more characters: {qfile})"
    return text.replace("\n", "\n> ")


def notice(task: store.Task, qid: str, q: dict) -> str:
    """The `question` notice's text; its openings are an API (`Q: `, `PERMISSION to run: `,
    `PERMISSION to use the directory `, `PERMISSION to connect to TCP port `)."""
    qfile = ask_dir(task) / f"{qid}.json"
    question = _quote(q["question"], QUESTION_CHARS, qfile)
    if q["command"]:
        body = (f"PERMISSION to run: `{q['command']}`\nWhy: {question}\nAllow it with `wb allow "
                f"{task.name} \"<pattern>\"` (--always to keep it), or answer with another way.")
    elif q["path"]:
        from . import context  # the runner's side: the ask server never builds a notice
        path = shlex.quote(q["path"])
        body = f"PERMISSION to use the directory {path} (outside its lane)\nWhy: {question}\n"
        # no grant reaches it: don't suggest one (#912; a file an API lane hides, #1093)
        if why := context.kept_out(q["path"]) or context.api_hidden_refusal(task, q["path"]):
            body += f"No grant reaches it: {why}, or answer with another way."
        else:
            body += (f"Allow it with `wb allow {task.name} --path {path}` (--always to keep it), "
                     "or answer with another way.")
    elif q["port"]:
        from . import context
        body = f"PERMISSION to connect to TCP port {q['port']} (outside its lane)\nWhy: {question}\n"
        if int(q["port"]) in context.DAEMON_PORTS:
            body += (f"It is {context.DAEMON_PORTS[int(q['port'])]}'s port. Allow it with `wb allow "
                     f"{task.name} --port {q['port']}` (--always to keep it), or answer with "
                     "another way.")
        else:  # the lane refuses only DAEMON_PORTS: nothing a grant lifts
            body += ("The lane doesn't refuse that port, so no grant lifts it: answer with "
                     "another way.")
    else:
        body = f"Q: {question}"
    if q["context"].strip():
        body += f"\nContext: {_quote(q['context'], CONTEXT_CHARS, qfile)}"
    return body


def _judge(task: store.Task, q: dict | None, asks: dict) -> tuple[str, str | None]:
    """A new ask file's kind ("q" a question, "p" a permission request, "x" refused) and, when
    refused, what the agent is told instead."""
    if q is None or not q["question"].strip():
        return "x", NO_QUESTION
    if why := bad_request(q["command"], q["path"], q["port"]):
        return "x", why
    if _permission(q):
        if sum(1 for k in asks.values() if k == "p") >= PERMISSION_BUDGET:
            return "x", PERMISSIONS_USED
        return "p", None
    if sum(1 for k in asks.values() if k == "q") >= int(task.spec.get("ask_budget", 2)):
        return "x", BUDGET_USED
    return "q", None


def relay(task: store.Task, opened: dict) -> None:
    """The runner's side of the agent's questions, once a tick, outside the lane. Each new
    ask/<id>.json is checked and counted (status `asks`, which the agent can't write: deleting
    its ask files gives it no new budget), and raises the `question` notice; a refused one is
    answered with why. The phase is `asking` while any question is open, `status.asking` the
    oldest, so two at once keep the task waiting until both are answered (the audit: the first
    answer set it running, and the silence guard stopped the round). `opened` is the round's
    open questions, id -> when the runner saw it: a question outlives its round never, and one
    the server gave up on is closed (close). The phase is set before the notice is raised: a
    reader that sees the notice sees the task asking (the mod drops a question notice otherwise)."""
    st = task.status
    asks = dict(st.get("asks") or {})
    new = [n[:-5] for n in _names(task) if n[:-5] not in asks]
    raised = []
    for qid in new[:NEW_MAX] if len(asks) < ASKS_MAX else []:
        q = read_question(task, qid)
        kind, why = _judge(task, q, asks)
        asks[qid] = kind
        if why:
            try:
                answer_once(task, qid, why)  # unless a `wb answer` got there first
            except OSError:
                pass
            continue
        # when it was seen, and its notice's mark: never one another open question's has, or
        # acknowledging that one acknowledged both (two asked in one tick, the 0.90 review)
        opened[qid] = max(store.now(), round(max(opened.values(), default=0) + 0.001, 3))
        task.event({"ts": store.now(), "round": st.get("round"), "kind": "ask", "id": qid,
                    "text": q["question"][:QUESTION_CHARS]})
        raised.append((qid, opened[qid], notice(task, qid, q)))
    timeout = float(task.spec.get("ask_timeout", 1200))
    for qid in list(opened):
        answer = _read(ask_dir(task) / f"{qid}.answer")
        waited = _WAITS.setdefault((str(task.dir), qid), Waited()).step()
        if answer is not None:
            task.event({"ts": store.now(), "round": st.get("round"), "kind": "answer",
                        "id": qid, "text": answer.strip()[:QUESTION_CHARS]})
            del opened[qid]
            _WAITS.pop((str(task.dir), qid), None)
        elif waited > timeout:  # the server has given up on it (both leave a suspend out)
            close(task, {qid: opened.pop(qid)})
    if asks != (st.get("asks") or {}):
        task.set_status(asks=asks)
    oldest = min(opened, key=lambda k: (opened[k], k)) if opened else None
    if oldest and (st.get("phase") != "asking" or st.get("asking") != oldest):
        task.set_status(phase="asking", asking=oldest)
    elif not oldest and st.get("phase") == "asking":
        task.set_status(phase="running", asking=None)
    for qid, ts, text in raised:
        task.notify("question", text, id=qid, ts=ts)


def handle(msg: dict, task: store.Task):
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:  # a notification (notifications/initialized and the like): no reply
        return None
    if method == "initialize":
        version = (msg.get("params") or {}).get("protocolVersion") or PROTOCOL
        result = {"protocolVersion": version, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "workbench", "version": __version__}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [TOOL, *filter(None, [submit_tool(task)])]}
        try:  # OpenCode listed the tool: the runner's round report checks for it (LISTED)
            (ask_dir(task) / LISTED).touch()
        except OSError:
            pass
    elif method == "tools/call":
        params = msg.get("params") or {}
        if params.get("name") == SUBMIT:  # a report task's result (#1278)
            try:
                ok, text = submit(task, params.get("arguments"))
            except OSError as exc:  # a full disk: the agent can still end with the object
                ok, text = False, (f"submit_result failed ({exc.strerror or exc}): end your last "
                                   "message with the object instead")
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "isError": not ok, "content": [{"type": "text", "text": text}]}}
        if params.get("name") != TOOL["name"]:
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32602, "message": f"unknown tool {params.get('name')}"}}
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            args = {}
        try:
            text = ask(task, str(args.get("question", "")).strip(), str(args.get("context", "")),
                       str(args.get("command", "")).strip(), str(args.get("path", "")).strip(),
                       str(args.get("port") or "").strip())
        except Exception as exc:  # a full disk, an unwritable ask/: the agent hears it and goes on
            why = " ".join(str(exc).split())[:200] or type(exc).__name__
            return {"jsonrpc": "2.0", "id": mid, "result": {"isError": True, "content": [
                {"type": "text", "text": f"ask_reviewer failed: {why}; carry on without an "
                                         "answer and say so in your summary."}]}}
        result = {"content": [{"type": "text", "text": text}], "isError": False}
    else:
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": -32601, "message": f"method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main():
    task = store.Task(os.environ["WB_TASK"])
    out = threading.Lock()

    def serve(line: str) -> None:
        mid = None  # null only for a line that isn't a request at all, as JSON-RPC says
        try:
            # scrubbed: half an emoji (a lone surrogate) in the question failed its write (#525)
            msg = store.loads(line)
            mid = msg.get("id") if isinstance(msg, dict) else None
            reply = handle(msg, task)
        except Exception as exc:  # never let one bad message take the agent's tool away
            # with the request's own id: OpenCode's MCP client dropped a reply with id null, and
            # the call waited until the silence guard stopped the round (#525)
            reply = {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(exc)}}
        if reply is not None:
            with out:
                sys.stdout.write(json.dumps(reply) + "\n")
                sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        if '"tools/call"' in line:  # a question blocks until Claude answers: pings go on meanwhile
            threading.Thread(target=serve, args=(line,), daemon=True).start()
        else:
            serve(line)


if __name__ == "__main__":
    main()
