#!/usr/bin/env python3
"""Stands in for `opencode run --format json` in tests. FAKE_SCENARIO picks the behaviour:

  ok     write answer.txt = "42", say so
  fix    first round writes "41" (wrong); a continued session (--session) writes "42"
  loop   run the same failing command over and over (the stuck detector should stop it)
  hang   print one event, then go silent (the silence timeout should stop it)
  crash  exit 1 with no events
  slow   a first run that takes an hour; a continued session behaves like ok
  fill   writes "41", then a step that fills 76% of a 32K window, then works on for an hour;
         a fresh session handed the task behaves like ok
  read   reads, a step that fills 76% before any change, then writes "42" like ok
  floor  reads 48% of its window before its first change, then fills to 76%, then writes "42"
  refill every session changes a file, then fills 76% and works on for an hour
  bgwait a command OpenCode moved to the background: `background_wait`, quiet for FAKE_BGWAIT
         seconds (6), then writes "42" like ok
  toolhang  one tool call, then silent (a command that prints nothing for long)
A `run` message must follow `--` when it starts with '-', or the fake prints its usage and exits
1, as OpenCode does. FAKE_LATE: seconds before the first event (a model reading its prompt).
FAKE_PROMPTS, if set, is a file each run appends its prompt to; FAKE_RUNS one that records
whether each run continued a session, and its prompt; FAKE_SLEEP pauses each run after its first
event. FAKE_LESSON ends the agent's text with a `Lesson:` line (what workbench.lessons gathers).
FAKE_BG, a file: each run starts `sleep` in a session of its own, left running, and writes its pid
there (what the agent leaves behind). FAKE_STRAY prints lines that are JSON but not events, or
events of the wrong shape, first.
`session export` prints one finished step: FAKE_FILL sets the context it read, FAKE_COMPACTED
adds a completed compaction. `session list` lists nothing. Either fails without `--standalone`,
as the real one does when another service holds the managed port. `--version` says
`FAKE_VERSION` (2.3.0).
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
        fh.write(json.dumps({"session": "--session" in args, "prompt": args[-1]}) + "\n")
if scenario == "slow":
    scenario = "ok" if "--session" in args else "hang"
if scenario == "fill" and "picking this task up in a fresh session" in args[-1]:
    scenario = "ok"
if scenario == "crash":
    sys.exit(1)
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
