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
FAKE_PROMPTS, if set, is a file each run appends its prompt to; FAKE_SLEEP pauses each run after its first event.
FAKE_LESSON, if set, ends the agent's text with a `Lesson:` line (what workbench.lessons gathers).
"""
import json
import os
import sys
import time
import uuid

args = sys.argv[1:]
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
