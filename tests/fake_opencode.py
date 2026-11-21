#!/usr/bin/env python3
"""Stands in for `opencode run --format json` in tests. FAKE_SCENARIO picks the behaviour:

  ok     write answer.txt = "42", say so
  fix    first round writes "41" (wrong); a continued session (--session) writes "42"
  loop   run the same failing command over and over (the stuck detector should stop it)
  hang   print one event, then go silent (the silence timeout should stop it)
  crash  exit 1 with no events; with FAKE_DOWN (a file) it takes the model server down with it
         (creates the file) the first time, and behaves like ok after
  slow   a first run that takes an hour; a continued session behaves like ok
  fill   writes "41", then a step that fills 76% of a 32K window, then works on for an hour;
         a fresh session handed the task behaves like ok
  read   reads, a step that fills 76% before any change, then writes "42" like ok
  floor  reads 48% of its window before its first change, then fills to 76%, then writes "42"
  refill every session changes a file, then fills 76% and works on for an hour
  bgwait a command OpenCode moved to the background: `background_wait`, quiet for FAKE_BGWAIT
         seconds (6), then writes "42" like ok
  toolhang  one tool call, then silent (a command that prints nothing for long)
  retry  no answer from the model's server: a `retry` line every FAKE_RETRY_EVERY seconds (1),
         OpenCode cutting the request at its provider timeout and sending it again (#491)
  say    a few words and no tool call ("I will inspect the files")
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
`session export` prints one finished step: FAKE_FILL sets the context it read, FAKE_COMPACTED
adds a completed compaction. `session list` lists nothing. Either fails without `--standalone`,
as the real one does when another service holds the managed port. `--version` says
`FAKE_VERSION` (2.3.0). A `run` starts the config's workbench MCP server and lists its tools,
as OpenCode does, unless FAKE_NO_MCP.
"""
import json
import os
import sys
import time
import uuid

args = sys.argv[1:]
if args == ["--version"]:  # wb doctor; FAKE_VERSION plays an older or odder build
    print(os.environ.get("FAKE_VERSION", "opencode v2.3.0"))
    sys.exit(0)
if args[:1] == ["session"] and "--standalone" not in args:
    # As real OpenCode does when another service holds the managed port (the 2026-11-16 audit):
    # workbench's session commands must not depend on the background service.
    sys.exit("Managed service port 49374 on 127.0.0.1 is already in use by PID 1")
if args[:2] == ["session", "export"]:  # `wb watch`: one finished step, as OpenCode exports it
    sid = args[2]
    # FAKE_FILL: the context the step read (the runner's fresh-session check); FAKE_COMPACTED:
    # OpenCode compacted the session first
    compacted = [{"type": "compaction", "id": f"{sid}-c", "status": "completed", "reason": "auto",
                  "summary": "Earlier: read answer.txt.", "time": {"created": 1500}}] \
        if os.environ.get("FAKE_COMPACTED") else []
    print(json.dumps({"info": {"id": sid}, "messages": [
        {"type": "user", "id": f"{sid}-m1", "time": {"created": 1000}, "text": "Write the answer."},
        *compacted,
        {"type": "assistant", "id": f"{sid}-m2", "finish": "stop",
         "time": {"created": 2000, "firstToken": 3000, "streamed": 5000, "completed": 5000},
         "tokens": {"input": int(os.environ.get("FAKE_FILL", 1200)), "output": 80, "reasoning": 0,
                    "cache": {"read": 0}},
         "content": [
             {"type": "reasoning", "text": "It wants 42.", "time": {"created": 3000, "completed": 4500}},
             {"type": "tool", "name": "edit", "state": {"status": "completed", "input": {
                 "path": "answer.txt", "oldString": "41", "newString": "42"}}},
             {"type": "text", "text": "Wrote 42."}]}]}))
    sys.exit(0)
if args[:2] == ["session", "list"]:  # transcript.find_session before the first event: none yet
    sys.exit(0)
session = args[args.index("--session") + 1] if "--session" in args else f"ses_{uuid.uuid4().hex[:12]}"
scenario = os.environ.get("FAKE_SCENARIO", "ok")
t = lambda: int(time.time() * 1000)  # noqa: E731


def emit(kind, **part):
    print(json.dumps({"type": kind, "timestamp": t(), "sessionID": session,
                      "part": {"sessionID": session, **part}}), flush=True)


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
if os.environ.get("FAKE_RUNS") and args and args[0] == "run":  # which runs continued a session
    with open(os.environ["FAKE_RUNS"], "a") as fh:
        fh.write(json.dumps({"session": "--session" in args, "prompt": args[-1],
                             "cwd": os.getcwd(), "pwd": os.environ.get("PWD")}) + "\n")
if scenario == "slow":
    scenario = "ok" if "--session" in args else "hang"
if scenario == "fill" and "picking this task up in a fresh session" in args[-1]:
    scenario = "ok"
if scenario == "crash" and os.path.exists(os.environ.get("FAKE_DOWN", "") + ".crashed"):
    scenario = "ok"  # the server came back: this run works
if scenario == "crash":
    if os.environ.get("FAKE_DOWN"):  # an out-of-memory kill or an Ollama restart, mid-round
        for f in (os.environ["FAKE_DOWN"], os.environ["FAKE_DOWN"] + ".crashed"):
            open(f, "w").close()
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
elif scenario == "fill":
    with open("answer.txt", "w") as fh:
        fh.write("41\n")
    tool("write", {"filePath": os.path.abspath("answer.txt"), "content": "41"})
    emit("step_finish", reason="tool-calls", tokens={"input": 2000, "output": 100,
                                                      "cache": {"read": 900}})  # began work at 3000
    emit("text", text="Wrote 41 so far; next I fix it to the real answer.")
    emit("step_finish", reason="tool-calls", tokens={"input": 2000, "output": 900,
                                                      "cache": {"read": 22100}})  # 25000: 76%
    time.sleep(3600)
if os.environ.get("FAKE_EXIT") and args and args[0] == "run":
    sys.exit(int(os.environ["FAKE_EXIT"]))
if os.environ.get("FAKE_LINGER") and args and args[0] == "run":
    sys.stdout.flush()
    os.close(1)
    time.sleep(3600)
