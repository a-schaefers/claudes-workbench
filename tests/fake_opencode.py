#!/usr/bin/env python3
"""Stands in for `opencode run --format json` in tests. FAKE_SCENARIO picks the behaviour:

  ok     write answer.txt = "42", say so
  fix    first round writes "41" (wrong); a continued session (--session) writes "42"
  loop   run the same failing command over and over (the stuck detector should stop it)
  hang   print one event, then go silent (the silence timeout should stop it)
  crash  exit 1 with no events; with FAKE_DOWN (a file) it takes the model server down with it
         (creates the file) the first time, and behaves like ok after
  crashtalk  like crash, after the model's first words ("Let me look at the parser"): a server
         killed out of memory late in a round (#657); with FAKE_CRASH_HANG it then goes silent
         instead of exiting (OpenCode retrying a server that is gone)
  slow   a first run that takes an hour; a continued session behaves like ok
  fill   writes "41", then a step that fills 76% of a 32K window (6K of it reasoning), then
         works on for an hour; a fresh session handed the task behaves like ok
  read   reads, a step that fills 76% before any change, then writes "42" like ok
  compact  writes "41", a step at 52% of a 32K window, then one at 15%: OpenCode compacted it
         between them, which `run --format json` doesn't print (#774); then works on for an
         hour; a fresh session handed the task behaves like ok. FAKE_COMPACT_READ: it reads
         before writing, its first step at 50%
  floor  reads 48% of its window before its first change, then fills to 76%, then writes "42"
  refill every session changes a file, then fills 76% and works on for an hour
  bgwait a command OpenCode moved to the background: `background_wait`, quiet for FAKE_BGWAIT
         seconds (6), then writes "42" like ok
  devserver  writes "42", starts `npm run dev` in the background, ends its turn (reason stop),
         then `background_wait` on that job for an hour (a server never ends: #658)
  toolhang  one tool call, then silent (a command that prints nothing for long)
  retry  no answer from the model's server: a `retry` line every FAKE_RETRY_EVERY seconds (1),
         OpenCode cutting the request at its provider timeout and sending it again (#491)
  say    a few words and no tool call ("I will inspect the files")
  compactfail  reads, a step at 52% of a 32K window, then OpenCode's compaction fails: an
         `error` event `compaction.failed`, "Compaction produced no summary" (FAKE_COMPACT_MSG
         another), and the run ends, exiting 0 or FAKE_COMPACT_EXIT (1 in the bench replay,
         #969). FAKE_COMPACT_EDIT: it writes "41" first. A fresh session handed the task
         behaves like ok, unless FAKE_REFAIL (every session fails again)
A `run` message must follow `--` when it starts with '-', or the fake prints its usage and exits
1, as OpenCode does. FAKE_LATE: seconds before the first event (a model reading its prompt).
FAKE_PROMPTS, if set, is a file each run appends its prompt to; FAKE_RUNS one that records
whether each run continued a session, and its prompt; FAKE_SLEEP pauses each run after its first
event. FAKE_LESSON ends the agent's text with a `Lesson:` line (what workbench.lessons gathers).
FAKE_BG, a file: each run starts `sleep` in a session of its own, left running, and writes its pid
there (what the agent leaves behind). FAKE_STRAY prints lines that are JSON but not events, or
events of the wrong shape, first. FAKE_LINGER: after its last event a run closes its output and
stays alive (a shutdown that hangs). FAKE_EXIT: after its last event a run exits with this code
(a crash after the model's first words, or after an edit).
FAKE_SURROGATE: a run first reads a file whose name and text end in half an emoji (an
unpaired surrogate escaped, which is valid JSON).
FAKE_LATIN1: a run first prints a line that isn't UTF-8 (`caf\xe9`, Latin-1).
FAKE_RUNS rows also say what OpenCode's environment had (`opencode_db`, `xdg_data`, `xdg_state`,
`shell`) and what a command the agent runs through `$SHELL -c` sees (`shell_sees`: its
XDG_DATA_HOME, XDG_STATE_HOME, OPENCODE_DB and SHELL, `-` for one unset).
A run with OPENCODE_DB set records its new session in that home (`<db>.sessions`), as OpenCode
keeps it in its database (#1010). `session export` prints one finished step, its `info.home` the
database it read: FAKE_FILL sets the context it read, FAKE_COMPACTED adds a completed compaction;
in a home that records sessions, one it doesn't hold is "Session not found" (exit 1). `session
list` lists the home's recorded sessions. Either fails without `--standalone`, as the real one
does when another service holds the managed port. `auth export <provider>` prints the rows of
FAKE_AUTH_EXPORT (a JSON list, as OpenCode 2.3.2 prints them) for that provider, or `[]`, and
appends the provider to FAKE_AUTH_LOG when set. `--version` says
`FAKE_VERSION` (2.3.0). A `run` starts the config's workbench MCP server and lists its tools,
as OpenCode does, unless FAKE_NO_MCP.

API models (#1011), as the spike for #1009 saw OpenCode 2.3.2 print them:
  ratelimit  a 429: `retry` events (attempts 2 to FAKE_RETRIES+1, 10 by default, FAKE_RETRY_EVERY
         seconds apart, `retryAfterMs` with FAKE_RETRY_AFTER seconds), then the
         `provider.rate-limit` error and exit 1
  down   the provider unreachable: `provider.transport` retries the same way, then that error, exit 1
  auth   a 401: the `provider.auth` error at once, exit 1;  quota  a 402: `provider.quota`, exit 1
  nokey  no key for the provider: `provider.no-route` "Model unavailable: <id>", exit 1, no step
The upstream error's message (FAKE_ERROR_BODY, else a stock one) comes through verbatim, in
`message` and `response.body`, as in OpenCode (a key-like string in a 401's body reached
stdout). WB_FAKE_CATALOG names a JSON file: a list of `api model.list` entries (`providerID`, `id`, `variants`, `cost`, `limit`…), or
`{"data": [...], "auth": [...]}` to give `auth list` its own rows; unset, the catalog is empty.
WB_FAKE_API sets a model's or a provider's state, `<provider>[/<id>]=<state>,…`, a state being
one of the API scenarios above (or `ok`): a run of that model (a config model's too) plays it,
its own state before its provider's, and a `nokey` provider
drops out of `api model.list`, `models` and `auth list`, as one OpenCode can't authenticate does.
`api model.list` prints `{location, data}`; `models` prints `<provider>/<id>` lines and, like
2.3.2, takes no provider argument; `auth list --format json` prints `[{id, name, connections:
[{type: "credential", id, label, method: "key"}]}]` per provider. FAKE_CALLS, a file: each `api`,
`models` and `auth` call appends its argv (how often wb read the catalog); FAKE_CATALOG_FAIL:
`api model.list` and `models` print an error on stderr and exit 1. `run -m <provider>/<id>#<variant>`
splits the provider at the first `/` and the variant at the first `#` after it; a catalog
provider's unknown model is `nokey`'s error, its unknown variant (the catalog's and the config's
`provider.<p>.models.<m>.variants`, from OPENCODE_CONFIG and OPENCODE_CONFIG_CONTENT) `Variant
unavailable for <id>: <v>`, exit 1. Every `step_finish` carries `part.cost` (USD, from the
catalog's first price, its context tier past `size`; FAKE_COST for a model with none, else 0) and
`part.tokens{input, output, reasoning, cache{read, write}}`; `session export` gives each assistant
message `cost` (FAKE_COST, else 0) and `tokens`, and `info.cost`, `info.tokens` their sums.
"""
import json
import os
import sys
import time
import uuid

args = sys.argv[1:]
API_SCENARIOS = ("ratelimit", "down", "auth", "quota", "nokey")


def catalog_file() -> dict:
    """WB_FAKE_CATALOG: a list of entries, or {"data": [...], "auth": [...]}; unset, empty."""
    path = os.environ.get("WB_FAKE_CATALOG")
    if not path:
        return {"data": []}
    with open(path) as fh:
        doc = json.load(fh)
    return {"data": doc} if isinstance(doc, list) else doc


def api_state(provider: str, mid: str = "") -> str:
    """WB_FAKE_API's state for a model, its own before its provider's: `ok` when unset."""
    states = {}
    for item in filter(None, (x.strip() for x in os.environ.get("WB_FAKE_API", "").split(","))):
        name, _, state = item.rpartition("=")
        states[name] = state
    return states.get(f"{provider}/{mid}") or states.get(provider) or "ok"


def reachable() -> list:
    """The catalog as OpenCode lists it: only providers it can authenticate."""
    return [e for e in catalog_file()["data"]
            if api_state(e["providerID"], e["id"]) != "nokey"]


if os.environ.get("FAKE_CALLS") and args[:1] in (["api"], ["models"], ["auth"]):
    with open(os.environ["FAKE_CALLS"], "a") as fh:  # which catalog and key reads wb made (#1013)
        fh.write(json.dumps(args) + "\n")
if os.environ.get("FAKE_CATALOG_FAIL") and (args[:2] == ["api", "model.list"] or args[:1] == ["models"]):
    print("Error: Unable to connect. Is the computer able to access the url?", file=sys.stderr)
    sys.exit(1)
if args[:2] == ["api", "model.list"]:  # with or without --standalone, as the real one takes
    print(json.dumps({"location": {"directory": os.getcwd()}, "data": reachable()}))
    sys.exit(0)
if args[:1] == ["models"]:
    extra = [a for a in args[1:] if not a.startswith("-")]
    if extra:  # 2.3.2 takes no provider: `Unexpected positional argument`
        print(f'ERROR\n  Unexpected positional argument: "{extra[0]}"', file=sys.stderr)
        sys.exit(1)
    for e in reachable():
        print(f"{e['providerID']}/{e['id']}")
    sys.exit(0)
if args[:2] == ["auth", "list"]:
    doc = catalog_file()
    rows = doc.get("auth")
    if rows is None:
        rows = [{"id": p, "name": p, "connections": [{"type": "credential", "id": f"cred_{p}",
                                                      "label": "default", "method": "key"}]}
                for p in dict.fromkeys(e["providerID"] for e in doc["data"])]
    rows = [r for r in rows if api_state(r["id"]) != "nokey"]
    print(json.dumps(rows) if "json" in args else "\n".join(r["id"] for r in rows))
    sys.exit(0)
if args == ["--version"]:  # wb doctor; FAKE_VERSION plays an older or odder build
    print(os.environ.get("FAKE_VERSION", "opencode v2.3.0"))
    sys.exit(0)
if args[:2] == ["auth", "export"]:  # models.auth_key: OpenCode 2.x's credential store
    pid = next((a for a in args[2:] if not a.startswith("-")), None)
    if os.environ.get("FAKE_AUTH_LOG"):
        with open(os.environ["FAKE_AUTH_LOG"], "a") as f:
            f.write(f"{pid}\n")
    rows = json.loads(os.environ.get("FAKE_AUTH_EXPORT") or "[]")
    print(json.dumps([r for r in rows if pid is None or r.get("integrationID") == pid]))
    sys.exit(0)


def db_path():
    """The database this OpenCode would use: OPENCODE_DB, else its data home's."""
    return os.environ.get("OPENCODE_DB") or os.path.join(
        os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "opencode", "opencode.db")


def recorded():
    """The sessions this home holds, by id (title), or None when it records none."""
    try:
        with open(db_path() + ".sessions") as fh:
            return {r["id"]: r.get("title", "") for r in (json.loads(x) for x in fh if x.strip())}
    except OSError:
        return None


if args[:1] == ["session"] and "--standalone" not in args:
    # As real OpenCode does when another service holds the managed port (the 2026-11-16 audit):
    # workbench's session commands must not depend on the background service.
    sys.exit("Managed service port 49374 on 127.0.0.1 is already in use by PID 1")
if args[:2] == ["session", "export"]:  # `wb watch`: one finished step, as OpenCode exports it
    sid = args[2]
    held = recorded()
    if held is not None and sid not in held:  # another home's session (#1010)
        sys.exit(f"Session not found: {sid}")
    # FAKE_FILL: the context the step read (the runner's fresh-session check); FAKE_COMPACTED:
    # OpenCode compacted the session first
    compacted = [{"type": "compaction", "id": f"{sid}-c", "status": "completed", "reason": "auto",
                  "summary": "Earlier: read answer.txt.", "time": {"created": 1500}}] \
        if os.environ.get("FAKE_COMPACTED") else []
    exported = {"info": {"id": sid, "home": db_path()}, "messages": [
        {"type": "user", "id": f"{sid}-m1", "time": {"created": 1000}, "text": "Write the answer."},
        *compacted,
        {"type": "assistant", "id": f"{sid}-m2", "finish": "stop",
         "time": {"created": 2000, "firstToken": 3000, "streamed": 5000, "completed": 5000},
         "cost": float(os.environ.get("FAKE_COST") or 0),
         "tokens": {"input": int(os.environ.get("FAKE_FILL", 1200)), "output": 80, "reasoning": 0,
                    "cache": {"read": 0, "write": 0}},
         "content": [
             {"type": "reasoning", "text": "It wants 42.", "time": {"created": 3000, "completed": 4500}},
             {"type": "tool", "name": "edit", "state": {"status": "completed", "input": {
                 "path": "answer.txt", "oldString": "41", "newString": "42"}}},
             {"type": "text", "text": "Wrote 42."}]}]}
    done = [m for m in exported["messages"] if m["type"] == "assistant"]
    exported["info"].update(cost=sum(m["cost"] for m in done), tokens={
        k: sum(m["tokens"][k] for m in done) for k in ("input", "output", "reasoning")} | {
        "cache": {k: sum(m["tokens"]["cache"][k] for m in done) for k in ("read", "write")}})
    print(json.dumps(exported))
    sys.exit(0)
if args[:2] == ["session", "list"]:  # transcript.find_session before the first event
    for sid, title in (recorded() or {}).items():
        print(f"{sid}\t{title}\t11/22/2026, 11:31:45 PM")
    sys.exit(0)
session = args[args.index("--session") + 1] if "--session" in args else f"ses_{uuid.uuid4().hex[:12]}"
scenario = os.environ.get("FAKE_SCENARIO", "ok")
t = lambda: int(time.time() * 1000)  # noqa: E731


PRICE = None  # the run's model's catalog prices, once -m is read


def cost(tok: dict) -> float:
    """What OpenCode charges a step, from the catalog: `input` leaves out cache reads, reasoning
    is billed as output, and a `tier: {type: "context", size}` price applies past its size."""
    if not PRICE:
        return float(os.environ.get("FAKE_COST") or 0)
    used = tok["input"] + tok["cache"]["read"]
    rate = [p for p in PRICE if not p.get("tier") or used > p["tier"].get("size", 0)][-1]
    cache = rate.get("cache") or {}
    return round((tok["input"] * rate.get("input", 0) + (tok["output"] + tok["reasoning"])
                  * rate.get("output", 0) + tok["cache"]["read"] * cache.get("read", 0)
                  + tok["cache"]["write"] * cache.get("write", 0)) / 1e6, 10)


def emit(kind, **part):
    if kind == "step_finish":  # every field OpenCode 2.3 prints, and the step's price
        tok = {"input": 0, "output": 0, "reasoning": 0, **part.get("tokens", {})}
        tok["cache"] = {"read": 0, "write": 0, **tok.get("cache", {})}
        part.update(tokens=tok, cost=cost(tok))
    print(json.dumps({"type": kind, "timestamp": t(), "sessionID": session,
                      "part": {"sessionID": session, **part}}), flush=True)


def fail(kind: str, message: str, status: int | None = None):
    """An `error` line as OpenCode prints it, the upstream body verbatim; exit 1."""
    err = {"type": kind, "message": message}
    if status:
        err.update(status=status, response={"body": json.dumps({"error": {"message": message}})})
    print(json.dumps({"type": "error", "timestamp": t(), "sessionID": session, "error": err}),
          flush=True)
    sys.exit(1)


def layer_variants(provider: str, mid: str) -> set:
    """The variants the config adds to a model: V1 `provider.<p>.models.<m>.variants.<name>`,
    V2 `providers.<p>.models.<m>.variants: [{id}]`, from the layer file and the content."""
    found = set()
    docs = []
    if os.environ.get("OPENCODE_CONFIG"):
        try:
            with open(os.environ["OPENCODE_CONFIG"]) as fh:
                docs.append(json.load(fh))
        except (OSError, ValueError):
            pass
    try:
        docs.append(json.loads(os.environ.get("OPENCODE_CONFIG_CONTENT") or "{}"))
    except ValueError:
        pass
    for doc in docs:
        for key in ("provider", "providers"):
            v = (((doc.get(key) or {}).get(provider) or {}).get("models") or {}).get(mid) or {}
            v = v.get("variants") or {}
            found |= set(v) if isinstance(v, dict) else {x.get("id") for x in v}
    return found


def tool(name, inp, status="completed", error=None):
    state = {"status": status, "input": inp}
    if error:
        state["error"] = error
    emit("tool_use", tool=name, state=state)


def finish():
    emit("step_finish", reason="stop", tokens={"input": 120, "output": 30, "cache": {"read": 0}})


if args and args[0] == "run" and args[-1].startswith("-") and args[-2:-1] != ["--"]:
    print("opencode run [flags] [<message...>]", file=sys.stderr)  # OpenCode 2.3 took it for a flag
    sys.exit(1)
if os.environ.get("FAKE_PROMPTS") and args and args[0] == "run":
    with open(os.environ["FAKE_PROMPTS"], "a") as fh:
        fh.write(json.dumps(args[-1]) + "\n")
if os.environ.get("OPENCODE_DB") and args and args[0] == "run" and "--session" not in args:
    title = args[args.index("--title") + 1] if "--title" in args else ""
    os.makedirs(os.path.dirname(os.environ["OPENCODE_DB"]), exist_ok=True)
    open(os.environ["OPENCODE_DB"], "a").close()  # OpenCode makes its database on first use
    with open(os.environ["OPENCODE_DB"] + ".sessions", "a") as fh:
        fh.write(json.dumps({"id": session, "title": title}) + "\n")
if os.environ.get("FAKE_RUNS") and args and args[0] == "run":  # which runs continued a session
    import subprocess
    try:  # a command the agent runs, as OpenCode runs it: `$SHELL -c`
        sees = subprocess.run([os.environ.get("SHELL") or "sh", "-c",
                               'printf "%s|%s|%s|%s" "${XDG_DATA_HOME:--}" "${XDG_STATE_HOME:--}" '
                               '"${OPENCODE_DB:--}" "${SHELL:--}"'],
                              capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        sees = None
    with open(os.environ["FAKE_RUNS"], "a") as fh:
        fh.write(json.dumps({"session": "--session" in args, "prompt": args[-1],
                             "cwd": os.getcwd(), "pwd": os.environ.get("PWD"),
                             "opencode_db": os.environ.get("OPENCODE_DB"),
                             "xdg_data": os.environ.get("XDG_DATA_HOME"),
                             "xdg_state": os.environ.get("XDG_STATE_HOME"),
                             "shell": os.environ.get("SHELL"),
                             "fetch_off": os.environ.get("OPENCODE_DISABLE_MODELS_FETCH"),
                             "shell_sees": sees}) + "\n")
if args and args[0] == "run" and "-m" in args:  # the model: <provider>/<id>[#<variant>]
    ref = args[args.index("-m") + 1]
    provider, _, rest = ref.partition("/")
    mid, _, variant = rest.partition("#")
    listed = [e for e in catalog_file()["data"] if e["providerID"] == provider]
    entry = next((e for e in listed if e["id"] == mid), None)
    state = api_state(provider, mid)  # any model's, in the catalog or not
    if state in API_SCENARIOS:
        scenario = state
    if listed:  # a catalog provider's model (a config model passes as before)
        if entry is None:
            scenario = "nokey"
        elif variant and variant not in {v["id"] for v in entry.get("variants", [])} \
                | layer_variants(provider, mid):
            fail("provider.no-route", f"Variant unavailable for {provider}/{mid}: {variant}")
        else:
            PRICE = entry.get("cost") or None
    if scenario == "nokey":
        fail("provider.no-route", f"Model unavailable: {provider}/{mid}")
if scenario == "slow":
    scenario = "ok" if "--session" in args else "hang"
if (scenario in ("fill", "compact") or scenario == "compactfail" and not os.environ.get("FAKE_REFAIL")) \
        and "picking this task up in a fresh session" in args[-1]:
    scenario = "ok"
if scenario in ("crash", "crashtalk") and os.path.exists(os.environ.get("FAKE_DOWN", "") + ".crashed"):
    scenario = "ok"  # the server came back: this run works
if scenario == "crashtalk":
    emit("step_start")
    emit("text", text="Let me look at the parser.")
    scenario = "crash"
if scenario == "crash":
    if os.environ.get("FAKE_DOWN"):  # an out-of-memory kill or an Ollama restart, mid-round
        for f in (os.environ["FAKE_DOWN"], os.environ["FAKE_DOWN"] + ".crashed"):
            open(f, "w").close()
    if os.environ.get("FAKE_CRASH_HANG"):
        time.sleep(3600)
    sys.exit(1)
if args and args[0] == "run" and not os.environ.get("FAKE_NO_MCP"):
    # as OpenCode does when a session starts: each local MCP server in the config is started and
    # its tools listed (the workbench one here; the user's are commands that do nothing)
    import subprocess
    try:
        with open(os.environ["OPENCODE_CONFIG"]) as fh:
            server = json.load(fh)["mcp"]["servers"]["workbench"]
    except (KeyError, OSError, ValueError, TypeError):
        server = None
    if server and server.get("type") == "local":
        hello = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                 {"jsonrpc": "2.0", "method": "notifications/initialized"},
                 {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
        subprocess.run(server["command"], input="".join(json.dumps(m) + "\n" for m in hello),
                       env={**os.environ, **(server.get("environment") or {})},
                       capture_output=True, text=True, timeout=30)
if os.environ.get("FAKE_STRAY"):
    for stray in ("42", "[1, 2]", "null", '{"type": "step_finish", "part": {"tokens": 5}}',
                  '{"type": "tool_use", "part": "x"}', '{"type": "text", "part": {"text": 7}}',
                  '{"type": "error", "error": "boom"}'):
        print(stray, flush=True)
if os.environ.get("FAKE_BG") and args and args[0] == "run":
    import subprocess
    bg = subprocess.Popen(["sleep", "300"], start_new_session=True, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL)
    with open(os.environ["FAKE_BG"], "w") as fh:
        fh.write(str(bg.pid))
if os.environ.get("FAKE_LATIN1") and args and args[0] == "run":  # a plugin printing Latin-1
    sys.stdout.flush()
    sys.stdout.buffer.write(b"caf\xe9\n")
    sys.stdout.buffer.flush()
if os.environ.get("FAKE_SURROGATE") and args and args[0] == "run":  # escaped, as JSON allows
    print('{"type": "tool_use", "timestamp": %d, "sessionID": "%s", "part": {"tool": "read", '
          '"state": {"status": "completed", "input": {"filePath": "caf\\ud83d.txt"}}}}'
          % (t(), session), flush=True)
    print('{"type": "text", "timestamp": %d, "sessionID": "%s", "part": {"text": "half \\ud83d"}}'
          % (t(), session), flush=True)
time.sleep(float(os.environ.get("FAKE_LATE", 0)))  # before any output: reading the prompt
emit("step_start")
time.sleep(float(os.environ.get("FAKE_SLEEP", 0)))  # after the first event: the session is known
if scenario in ("ok", "fix"):
    value = "41" if scenario == "fix" and "--session" not in args else "42"
    with open("answer.txt", "w") as fh:
        fh.write(value + "\n")
    tool("write", {"filePath": os.path.abspath("answer.txt"), "content": value})
    emit("text", text=f"Wrote answer.txt = {value}."
         + (f"\nLesson: {os.environ['FAKE_LESSON']}" if os.environ.get("FAKE_LESSON") else ""))
    finish()
elif scenario == "loop":
    for _ in range(10):
        tool("bash", {"command": "make test"}, status="error", error="make: *** No rule")
        time.sleep(0.05)
    finish()
elif scenario == "hang":
    time.sleep(3600)
elif scenario == "chatter":  # a newer OpenCode's progress event, unknown here, ten times a second
    for _ in range(600):
        print(json.dumps({"type": "progress", "timestamp": t(), "sessionID": session}), flush=True)
        time.sleep(0.1)
elif scenario == "retry":  # as OpenCode 2.3 prints it, after the step_start above
    for attempt in range(2, 200):
        print(json.dumps({"type": "retry", "timestamp": t(), "sessionID": session,
                          "messageID": "msg_fake", "attempt": attempt, "nextRetryAt": t() + 1000,
                          "error": {"type": "provider.transport",
                                    "message": "Timed out waiting for response headers"}}), flush=True)
        time.sleep(float(os.environ.get("FAKE_RETRY_EVERY", 1)))
elif scenario in ("ratelimit", "down"):  # OpenCode 2.3.2's ten retries, then the error
    if scenario == "ratelimit":
        message = os.environ.get("FAKE_ERROR_BODY") or "Rate limit reached for requests"
        err = {"type": "provider.rate-limit", "message": message, "status": 429,
               "response": {"body": json.dumps({"error": {"message": message}})}}
    else:
        err = {"type": "provider.transport",
               "message": "ECONNREFUSED: Unable to connect. Is the computer able to access the url?"}
    after = os.environ.get("FAKE_RETRY_AFTER")
    every = float(os.environ.get("FAKE_RETRY_EVERY", 1))
    for attempt in range(2, int(os.environ.get("FAKE_RETRIES", 10)) + 2):
        print(json.dumps({"type": "retry", "timestamp": t(), "sessionID": session,
                          "messageID": "msg_fake", "attempt": attempt,
                          "nextRetryAt": t() + int(every * 1000),
                          "error": {**err, **({"retryAfterMs": int(float(after) * 1000)}
                                              if after and scenario == "ratelimit" else {})}}),
              flush=True)
        time.sleep(every)
    fail(err["type"], err["message"], err.get("status"))
elif scenario in ("auth", "quota"):  # no retry: the error at once
    fail(f"provider.{scenario}", os.environ.get("FAKE_ERROR_BODY") or (
        "Invalid API key" if scenario == "auth" else "Insufficient balance"),
        401 if scenario == "auth" else 402)
elif scenario == "say":
    emit("text", text="I will inspect the files.")
    finish()
elif scenario == "toolhang":
    tool("bash", {"command": "make check"})
    time.sleep(3600)
elif scenario == "bgwait":
    tool("bash", {"command": "make test"})
    emit("text", text="The tests run in the background; waiting for them.")
    finish()
    print(json.dumps({"type": "background_wait", "timestamp": t(), "sessionID": session,
                      "jobs": [{"id": "job1", "kind": "shell", "title": "make test"}]}), flush=True)
    time.sleep(float(os.environ.get("FAKE_BGWAIT", 6)))
    with open("answer.txt", "w") as fh:
        fh.write("42\n")
    tool("write", {"filePath": os.path.abspath("answer.txt"), "content": "42"})
    emit("text", text="Wrote answer.txt = 42.")
    finish()
elif scenario == "devserver":
    with open("answer.txt", "w") as fh:
        fh.write("42\n")
    tool("write", {"filePath": os.path.abspath("answer.txt"), "content": "42"})
    tool("shell", {"command": "npm run dev", "background": True})
    emit("text", text="Wrote answer.txt = 42; the dev server is up.")
    finish()
    print(json.dumps({"type": "background_wait", "timestamp": t(), "sessionID": session,
                      "jobs": [{"id": "job1", "kind": "shell", "title": "npm run dev"}]}), flush=True)
    time.sleep(3600)
elif scenario == "refill":  # every session changes something early, then fills up
    with open("notes.txt", "a") as fh:
        fh.write(f"{session}\n")
    tool("write", {"filePath": os.path.abspath("notes.txt"), "content": session})
    emit("step_finish", reason="tool-calls", tokens={"input": 2000, "output": 100,
                                                      "cache": {"read": 900}})
    emit("step_finish", reason="tool-calls", tokens={"input": 2000, "output": 900,
                                                      "cache": {"read": 22100}})  # 25000: 76%
    time.sleep(3600)
elif scenario == "read":
    tool("read", {"filePath": os.path.abspath("answer.txt")})
    emit("step_finish", reason="tool-calls", tokens={"input": 2000, "output": 900,
                                                      "cache": {"read": 22100}})  # 25000: 76%
    time.sleep(1)
    with open("answer.txt", "w") as fh:
        fh.write("42\n")
    tool("write", {"filePath": os.path.abspath("answer.txt"), "content": "42"})
    emit("text", text="Wrote answer.txt = 42.")
    finish()
elif scenario == "probing":  # #857: probe in a compound line, a long file read whole and in part
    with open("long.py", "w") as fh:
        fh.write("x = 1\n" * 400)
    tool("shell", {"command": "cd . && timeout 30 probe symbols long.py | head -40"})
    tool("read", {"filePath": os.path.abspath("long.py")})                      # whole: counted
    tool("read", {"filePath": os.path.abspath("long.py"), "offset": 10, "limit": 20})  # a part
    tool("shell", {"command": "grep -n probe long.py"})                         # not a probe call
    with open("answer.txt", "w") as fh:
        fh.write("42\n")
    tool("write", {"filePath": os.path.abspath("answer.txt"), "content": "42"})
    emit("text", text="Wrote answer.txt = 42.")
    finish()
elif scenario == "floor":  # read half its window before its first change, then filled up
    tool("read", {"filePath": os.path.abspath("answer.txt")})
    emit("step_finish", reason="tool-calls", tokens={"input": 2000, "output": 100,
                                                      "cache": {"read": 13900}})  # 16000: 48%
    time.sleep(1)  # the runner sees that step before the change
    with open("answer.txt", "w") as fh:
        fh.write("41\n")
    tool("write", {"filePath": os.path.abspath("answer.txt"), "content": "41"})
    emit("step_finish", reason="tool-calls", tokens={"input": 2000, "output": 100,
                                                      "cache": {"read": 14900}})  # began at 17000
    time.sleep(1)
    emit("step_finish", reason="tool-calls", tokens={"input": 2000, "output": 900,
                                                      "cache": {"read": 22100}})  # 25000: 76%
    time.sleep(1)
    with open("answer.txt", "w") as fh:
        fh.write("42\n")
    tool("write", {"filePath": os.path.abspath("answer.txt"), "content": "42"})
    emit("text", text="Wrote answer.txt = 42.")
    finish()
elif scenario == "compact":  # #774: a tool result past the margin compacted it between steps
    if os.environ.get("FAKE_COMPACT_READ"):  # read half its window before its first change
        tool("read", {"filePath": os.path.abspath("answer.txt")})
        emit("step_finish", reason="tool-calls", tokens={"input": 2000, "output": 100,
                                                          "cache": {"read": 14300}})  # 16400: 50%
        time.sleep(1)  # the runner sees that step before the change
    with open("answer.txt", "w") as fh:
        fh.write("41\n")
    tool("write", {"filePath": os.path.abspath("answer.txt"), "content": "41"})
    emit("step_finish", reason="tool-calls", tokens={"input": 2000, "output": 100,
                                                      "cache": {"read": 900 if not os.environ.get(
                                                          "FAKE_COMPACT_READ") else 14400}})
    emit("text", text="Wrote 41 so far; reading the test log next.")
    emit("step_finish", reason="tool-calls", tokens={"input": 2000, "output": 100,
                                                      "cache": {"read": 14900}})  # 17000: 52%
    tool("read", {"filePath": os.path.abspath("answer.txt")})
    emit("step_finish", reason="tool-calls", tokens={"input": 4000, "output": 900,
                                                      "cache": {"read": 0}})  # 4900: 15%
    if os.environ.get("FAKE_COMPACT_READ"):  # a session that carries on: it finishes the work
        with open("answer.txt", "w") as fh:
            fh.write("42\n")
        tool("write", {"filePath": os.path.abspath("answer.txt"), "content": "42"})
        emit("text", text="Wrote answer.txt = 42.")
        finish()
    else:
        time.sleep(3600)
elif scenario == "compactfail":  # #969: as OpenCode 2.3 prints it, then the run ends
    if os.environ.get("FAKE_COMPACT_EDIT"):
        with open("answer.txt", "w") as fh:
            fh.write("41\n")
        tool("write", {"filePath": os.path.abspath("answer.txt"), "content": "41"})
    tool("read", {"filePath": os.path.abspath("answer.txt")})
    emit("step_finish", reason="tool-calls", tokens={"input": 2000, "output": 100,
                                                      "cache": {"read": 14900}})  # 17000: 52%
    emit("text", text="Reading the whole test file next.")
    print(json.dumps({"type": "error", "timestamp": t(), "sessionID": session,
                      "error": {"type": "compaction.failed", "message": os.environ.get(
                          "FAKE_COMPACT_MSG", "Compaction produced no summary")}}), flush=True)
    sys.exit(int(os.environ.get("FAKE_COMPACT_EXIT", 0)))  # this run's, not its successor's
elif scenario == "fill":
    with open("answer.txt", "w") as fh:
        fh.write("41\n")
    tool("write", {"filePath": os.path.abspath("answer.txt"), "content": "41"})
    emit("step_finish", reason="tool-calls", tokens={"input": 2000, "output": 100,
                                                      "cache": {"read": 900}})  # began work at 3000
    emit("text", text="Wrote 41 so far; next I fix it to the real answer.")
    # 25000: 76%, but 58% without its reasoning, which OpenCode's compaction counts (#644)
    emit("step_finish", reason="tool-calls", tokens={"input": 2000, "output": 900, "reasoning": 6000,
                                                      "cache": {"read": 16100}})
    time.sleep(3600)
if os.environ.get("FAKE_EXIT") and args and args[0] == "run":
    sys.exit(int(os.environ["FAKE_EXIT"]))
if os.environ.get("FAKE_LINGER") and args and args[0] == "run":
    sys.stdout.flush()
    os.close(1)
    time.sleep(3600)
