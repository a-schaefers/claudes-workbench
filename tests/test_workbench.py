"""End-to-end tests: real git, real runner processes, a fake opencode. Run: python3 -m unittest"""
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import warnings
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from workbench import runner, ask_mcp, cli, events, store, transcript  # noqa: E402

FAKE = f"{sys.executable} {ROOT / 'tests' / 'fake_opencode.py'}"
TEST_CMD = "grep -qx 42 answer.txt"
OPENCODE_CONFIG = {"provider": {
    "ollama": {"options": {"baseURL": "http://127.0.0.1:11434/v1"},
               "models": {"mimo-v2.6:9b-q8_0": {"name": "MiMo", "limit": {"context": 32768}}, "qwen3.8-27b": {"name": "Qwen 27B"}}},
    "strata": {"options": {"baseURL": "http://127.0.0.1:8080/v1"}, "models": {"strata": {}}},
    "cloud": {"options": {"baseURL": "https://api.example.com/v1"}, "models": {"big": {}}},
}}


def run_wb(*argv) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli.main(list(argv))
    return buf.getvalue()


def wait_phase(name, phases, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = store.Task(name).status
        if st.get("phase") in phases and not st.get("pid"):
            return st
        time.sleep(0.2)
    raise AssertionError(f"{name} never reached {phases}: {store.Task(name).status}")


class Base(unittest.TestCase):
    def setUp(self):
        # `wb start` leaves its runner running on purpose (it outlives the command)
        warnings.simplefilter("ignore", ResourceWarning)
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.env_before = dict(os.environ)
        os.environ.update(WB_HOME=str(tmp / "state"), WB_OPENCODE_BIN=FAKE, WB_FAKE_MODELS="1",
                          WB_OPENCODE_CONFIG=str(tmp / "opencode.json"),
                          WB_MODELS=str(tmp / "wb-models.json"))
        (tmp / "opencode.json").write_text(json.dumps(OPENCODE_CONFIG))
        self.repo = tmp / "repo"
        self.repo.mkdir()
        g = lambda *a: subprocess.run(["git", "-C", str(self.repo), *a], check=True,  # noqa
                                      capture_output=True)
        g("init", "-q", "-b", "main")
        (self.repo / "README.md").write_text("hello\n")
        (self.repo / "CLAUDE.md").write_text("# repo\nA test project.\n")  # wb start needs one
        g("add", ".")
        g("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
        g("config", "user.email", "t@t")
        g("config", "user.name", "t")
        self.spec = tmp / "spec.md"
        self.spec.write_text("Write the answer to answer.txt.\n")

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env_before)
        self.tmp.cleanup()

    def start(self, name, scenario, *extra):
        os.environ["FAKE_SCENARIO"] = scenario
        return run_wb("start", name, "--spec", str(self.spec), "--repo", str(self.repo),
                      "--model", "mimo", *extra)


class Lifecycle(Base):
    def test_ok_round_passes_tests_and_accepts(self):
        self.start("t-ok", "ok", "--test", TEST_CMD)
        st = wait_phase("t-ok", {"review"})
        self.assertTrue(st["tests"]["ok"])
        self.assertTrue(st["session"].startswith("ses_"))
        report = run_wb("report", "t-ok")
        self.assertIn("tests: PASS", report)
        self.assertIn("answer.txt", report)
        self.assertIn("+42", run_wb("diff", "t-ok"))
        # the review notification is delivered once; a later wait says it was
        self.assertIn("[review] t-ok", run_wb("wait", "t-ok", "--timeout", "1"))
        self.assertIn("already delivered", run_wb("wait", "t-ok", "--timeout", "1"))
        out = run_wb("accept", "t-ok", "-m", "feat: answer", "--merge")
        self.assertIn("merged", out)
        self.assertEqual((self.repo / "answer.txt").read_text(), "42\n")
        log = subprocess.run(["git", "-C", str(self.repo), "log", "--format=%B", "-n1",
                              "delegate/t-ok"], capture_output=True, text=True).stdout
        self.assertIn("Workbench-Task: t-ok", log)
        self.assertIn("OpenCode-Session: ses_", log)
        self.assertNotIn("Co-Authored-By", log)   # the user's commit; no AI credit line
        rows, _ = store.read_jsonl(store.home() / "results.jsonl")
        self.assertEqual(rows[-1]["outcome"], "accepted")

    def test_failing_tests_go_back_to_the_agent_without_claude(self):
        self.start("t-fix", "fix", "--test", TEST_CMD)
        st = wait_phase("t-fix", {"review"})
        self.assertEqual(st["round"], 2)       # round 1 failed, round 2 was automatic
        self.assertEqual(st["auto_fixes"], 1)
        self.assertTrue(st["tests"]["ok"])
        prompt2 = store.Task("t-fix").round_dir(2).joinpath("prompt.md").read_text()
        self.assertIn("acceptance tests fail", prompt2)
        rows, _ = store.read_jsonl(store.home() / "notify.jsonl")
        self.assertEqual([r["kind"] for r in rows if r["task"] == "t-fix"], ["review"])

    def test_auto_fix_budget_exhausted_wakes_claude_with_fail(self):
        self.start("t-nofix", "fix", "--test", TEST_CMD, "--auto-fix", "0")
        st = wait_phase("t-nofix", {"review"})
        self.assertFalse(st["tests"]["ok"])
        self.assertIn("tests: FAIL", run_wb("report", "t-nofix"))

    def test_feedback_continues_the_same_session(self):
        self.start("t-fb", "fix", "--test", TEST_CMD, "--auto-fix", "0")
        first = wait_phase("t-fb", {"review"})
        run_wb("feedback", "t-fb", "--text", "1. The answer is 42.")   # or --message <file>
        st = wait_phase("t-fb", {"review"})
        self.assertEqual(st["session"], first["session"])
        self.assertTrue(st["tests"]["ok"])
        self.assertEqual(st["reviews"], 1)

    def test_loop_is_stopped_as_stuck(self):
        self.start("t-loop", "loop")
        st = wait_phase("t-loop", {"stuck"})
        self.assertIn("same call", st["last"])
        self.assertIn("STOPPED", run_wb("report", "t-loop"))

    def test_silence_is_stopped(self):
        self.start("t-hang", "hang", "--silence-timeout", "2")
        st = wait_phase("t-hang", {"stuck"}, timeout=40)
        self.assertIn("no output", st["last"])

    def test_acting_on_a_task_acknowledges_its_notifications(self):
        # A review notice nobody waited for must not resurface after the task is discarded.
        self.start("t-ack", "ok")
        wait_phase("t-ack", {"review"})
        run_wb("discard", "t-ack")
        rows, _ = store.read_jsonl(store.home() / "notify.jsonl")
        notice = [r for r in rows if r["task"] == "t-ack"][-1]
        acked = float((store.Task("t-ack").dir / "acked").read_text())
        self.assertGreaterEqual(acked, notice["ts"])  # what the mod checks before waking Claude

    def test_a_note_does_not_swallow_a_failure_claude_has_not_seen(self):
        # 2026-11-16: a task failed seconds before Claude logged a note; the note acknowledged
        # the failure notice and nothing woke Claude for an hour.
        self.start("t-unseen", "crash")
        wait_phase("t-unseen", {"failed"})
        run_wb("note", "t-unseen", "--text", "progress as of the last check-in")
        rows, _ = store.read_jsonl(store.home() / "notify.jsonl")
        failed = [r for r in rows if r["task"] == "t-unseen" and r["kind"] == "failed"][-1]
        acked = float((store.Task("t-unseen").dir / "acked").read_text())
        self.assertLess(acked, failed["ts"])                     # still news for the mod
        self.assertIn("[failed] t-unseen", run_wb("wait", "t-unseen", "--timeout", "2"))

    def test_wait_returns_when_the_notice_was_taken_elsewhere(self):
        # The mod (or a second waiter) acknowledged the review first: a named wait must not hang.
        self.start("t-taken", "ok")
        wait_phase("t-taken", {"review"})
        self.assertIn("[review] t-taken", run_wb("wait", "t-taken", "--timeout", "1"))
        out = run_wb("wait", "t-taken", "--timeout", "1")
        self.assertIn("already delivered", out)

    def test_thinking_past_the_budget_is_nudged_and_claude_told(self):
        # A model that generates for many minutes with no tool call gets the runner's own --now
        # steer (seen 2026-11-16: 20K-token plans, no edits), and Claude a check-in saying so.
        self.start("t-think", "hang", "--think-timeout", "2", "--silence-timeout", "120")
        deadline = time.time() + 30
        steer = store.Task("t-think").dir / "steer.jsonl"
        while time.time() < deadline and not (steer.exists() and steer.read_text().strip()):
            time.sleep(0.2)
        rows, _ = store.read_jsonl(steer)
        self.assertTrue(rows and rows[0]["now"] and "Stop planning and act now" in rows[0]["text"])
        notes, _ = store.read_jsonl(store.home() / "notify.jsonl")
        self.assertTrue(any(r["task"] == "t-think" and "the runner nudged it" in r["text"] for r in notes))
        self.assertEqual(store.Task("t-think").status.get("nudges"), 1)
        run_wb("stop", "t-think")

    def test_the_thinking_budget_has_a_default(self):
        self.start("t-budget", "ok")
        self.assertEqual(store.Task("t-budget").spec["think_timeout"], cli.THINK_DEFAULT_S)
        wait_phase("t-budget", {"review"})

    def test_crash_is_reported_failed(self):
        self.start("t-crash", "crash")
        wait_phase("t-crash", {"failed"})

    def test_stop_and_discard(self):
        self.start("t-stop", "hang")
        deadline = time.time() + 20
        while store.Task("t-stop").status.get("phase") != "running" and time.time() < deadline:
            time.sleep(0.2)
        run_wb("stop", "t-stop")
        self.assertEqual(store.Task("t-stop").status["phase"], "stuck")
        run_wb("discard", "t-stop", "--reason", "test")
        branches = subprocess.run(["git", "-C", str(self.repo), "branch"], capture_output=True,
                                  text=True).stdout
        self.assertNotIn("delegate/t-stop", branches)
        self.assertFalse(store.Task("t-stop").worktree.exists())


class Context(Base):
    """The agent's context: continued round to round, handed to a fresh session when full."""
    def setUp(self):
        super().setUp()
        self.runs_file = Path(self.tmp.name) / "runs.jsonl"
        os.environ["FAKE_RUNS"] = str(self.runs_file)
        self.feedback = Path(self.tmp.name) / "fb.md"
        self.feedback.write_text("1. Add a trailing comment line.\n")

    def runs(self):
        return [json.loads(x) for x in self.runs_file.read_text().splitlines()]

    def review_then_feedback(self, name, *flags):
        self.start(name, "ok", "--test", TEST_CMD)
        wait_phase(name, {"review"})
        run_wb("feedback", name, "--message", str(self.feedback), *flags)
        return wait_phase(name, {"review"})

    def test_a_roomy_session_is_continued(self):
        self.review_then_feedback("t-c1")
        self.assertEqual([r["session"] for r in self.runs()], [False, True])
        self.assertIn("agent context: 1.3K of 32K (3%)", run_wb("report", "t-c1"))

    def test_a_full_session_hands_the_task_to_a_fresh_one(self):
        os.environ["FAKE_FILL"] = "25000"          # 76% of the 32K window
        st = self.review_then_feedback("t-c2")
        second = self.runs()[1]
        self.assertFalse(second["session"])         # a new session, not the full one
        p = second["prompt"]
        self.assertTrue(p.startswith("You are picking this task up in a fresh session (the last "
                                     "session was 76% full"))
        self.assertIn("Write the answer to answer.txt.", p)       # the task, with the protocol
        self.assertIn("leave the changes", p)
        self.assertIn("+42", p)                                   # the work so far
        self.assertIn("1. Add a trailing comment line.", p.split("## Now")[1])  # then the feedback
        self.assertEqual(len(st["sessions"]), 1)    # the old session is kept, for the transcript
        out = run_wb("watch", "t-c2", "--once", "--no-color")
        self.assertIn("a fresh session for the agent", out)
        self.assertIn("fresh session: the task, the work so far",  # the fake's export is fixed,
                      transcript._user_message(p, False)[0])      # so read the head directly

    def test_keep_context_overrides_and_fresh_forces(self):
        os.environ["FAKE_FILL"] = "25000"
        self.review_then_feedback("t-c3", "--keep-context")
        self.assertTrue(self.runs()[1]["session"])
        os.environ["FAKE_FILL"] = "1200"
        run_wb("feedback", "t-c3", "--message", str(self.feedback), "--fresh")
        wait_phase("t-c3", {"review"})
        self.assertFalse(self.runs()[2]["session"])
        self.assertIn("Claude asked for a fresh session", self.runs()[2]["prompt"])

    def test_after_opencode_compacts_the_next_round_is_fresh(self):
        os.environ["FAKE_COMPACTED"] = "1"
        self.review_then_feedback("t-c4")
        self.assertFalse(self.runs()[1]["session"])
        self.assertIn("OpenCode had to compact", self.runs()[1]["prompt"])
        self.assertIn("OpenCode compacted the agent's context",
                      run_wb("watch", "t-c4", "--once", "--no-color"))

    def test_next_commits_the_step_and_keeps_the_agent_and_its_session(self):
        self.start("t-n", "ok", "--test", TEST_CMD)
        wait_phase("t-n", {"review"})
        step2 = Path(self.tmp.name) / "step2.md"
        step2.write_text("Now also write the question to question.txt.\n")
        out = run_wb("next", "t-n", "--spec", str(step2), "-m", "feat: the answer")
        self.assertIn("step 1 of t-n committed", out)
        st = wait_phase("t-n", {"review"})
        second = self.runs()[1]
        self.assertTrue(second["session"])          # the same agent, warm
        self.assertTrue(second["prompt"].startswith("Step 1 of this task is done and committed"))
        self.assertIn("question.txt", second["prompt"])
        self.assertEqual(st["steps"], 2)
        self.assertEqual(run_wb("diff", "t-n").strip(), "(no changes)")  # step 2's own diff: the
        self.assertIn("+42", run_wb("diff", "t-n", "--all"))  # fake rewrote the same answer
        log = subprocess.run(["git", "-C", str(self.repo), "log", "--format=%s", "delegate/t-n"],
                             capture_output=True, text=True).stdout
        self.assertIn("feat: the answer", log)


class Steering(Base):
    def setUp(self):
        super().setUp()
        self.prompts = Path(self.tmp.name) / "prompts.jsonl"
        os.environ["FAKE_PROMPTS"] = str(self.prompts)

    def sent(self):
        return [json.loads(x) for x in self.prompts.read_text().splitlines()]

    def wait_session(self, name):
        deadline = time.time() + 15
        while not store.Task(name).status.get("session"):
            self.assertLess(time.time(), deadline, "the agent never started")
            time.sleep(0.1)

    def test_now_interrupts_the_agent_and_continues_with_the_note_first(self):
        self.start("t-int", "slow", "--test", TEST_CMD)   # the first run would take an hour
        self.wait_session("t-int")
        self.assertIn("interrupting", run_wb("steer", "t-int", "--now", "--text", "Use 42."))
        st = wait_phase("t-int", {"review"}, timeout=30)
        self.assertEqual(st["round"], 2)
        self.assertTrue(st["tests"]["ok"])
        note = self.sent()[-1]
        self.assertTrue(note.startswith(runner.INTERRUPT_HEAD))
        self.assertIn("Use 42.", note)
        out = run_wb("watch", "t-int", "--once", "--no-color")
        for want in ("■ Claude interrupts the agent", "■ the agent was interrupted"):
            self.assertIn(want, out)
        head, _ = transcript._user_message(note, False)    # how the session shows the note
        self.assertEqual(head, "Claude → agent · INTERRUPT, high priority")

    def test_a_queued_note_waits_for_the_run_to_end_then_goes_before_the_tests(self):
        os.environ["FAKE_SLEEP"] = "2"
        self.start("t-note", "ok", "--test", TEST_CMD)
        self.wait_session("t-note")  # the first event; the run then pauses 2 s
        run_wb("steer", "t-note", "--text", "Mention the answer.")
        st = wait_phase("t-note", {"review"}, timeout=30)
        rows, _ = store.read_jsonl(store.Task("t-note").dir / "events.jsonl")
        self.assertNotIn("interrupted", [r["kind"] for r in rows])
        self.assertEqual(st["round"], 2)
        self.assertEqual([r["kind"] for r in rows].count("tests"), 1)  # once, after the note
        self.assertTrue(self.sent()[-1].startswith(runner.NOTE_HEAD))

    def test_checkins_wake_claude_with_a_digest_and_notes_reach_the_transcript(self):
        self.start("t-chk", "slow", "--checkin", "0.03")    # every ~2 s
        self.wait_session("t-chk")
        deadline = time.time() + 20
        while True:
            rows = [r for r in store.read_jsonl(store.home() / "notify.jsonl")[0]
                    if r["kind"] == "checkin"]
            if rows or time.time() > deadline:
                break
            time.sleep(0.2)
        self.assertTrue(rows, "no check-in arrived")
        self.assertIn("task t-chk · MiMo · round 1 · running", rows[0]["text"])
        self.assertIn("since the last check-in", rows[0]["text"])
        run_wb("note", "t-chk", "--text", "Slow start, reading first.")
        run_wb("stop", "t-chk")
        out = run_wb("watch", "t-chk", "--once", "--no-color")
        self.assertIn("Claude (captain):\n  Slow start, reading first.", out)
        self.assertIn("reasoning", out.splitlines()[0])

    def test_bail_out_stops_every_agent_and_keeps_the_work(self):
        self.start("t-b1", "slow")
        self.start("t-b2", "slow")
        self.wait_session("t-b1")
        self.assertEqual(store.Task("t-b2").status["phase"], "queued")  # behind the GPU lock
        out = run_wb("stop", "--all")
        self.assertIn("t-b1", out)
        for name in ("t-b1", "t-b2"):
            task = store.Task(name)
            self.assertFalse(cli.runner_alive(task))
            self.assertEqual(task.status["phase"], "stuck")
            self.assertTrue(task.worktree.exists())         # stopped, not thrown away
        self.assertIn("nothing was running", run_wb("stop", "--all"))

    def test_get_it_done_takes_over_every_open_task(self):
        self.start("t-g1", "slow")
        self.wait_session("t-g1")
        self.assertIn("took over t-g1", run_wb("takeover", "--all"))
        task = store.Task("t-g1")
        self.assertFalse(cli.runner_alive(task))
        self.assertEqual(task.status["last"], "Claude took over")

    def test_takeover_stops_the_agent_and_hands_over_the_worktree(self):
        self.start("t-take", "slow", "--test", TEST_CMD)
        self.wait_session("t-take")
        out = run_wb("takeover", "t-take")
        self.assertIn("took over", out)
        task = store.Task("t-take")
        self.assertFalse(cli.runner_alive(task))
        (task.worktree / "answer.txt").write_text("42\n")     # Claude finishes it by hand
        self.assertIn("accepted t-take", run_wb("accept", "t-take", "-m", "feat: answer"))
        with self.assertRaises(SystemExit):                  # nothing left to steer
            run_wb("steer", "t-take", "--text", "x")


class Watch(Base):
    def test_watch_shows_the_whole_exchange(self):
        self.start("t-watch", "fix", "--test", TEST_CMD)
        wait_phase("t-watch", {"review"})
        out = run_wb("watch", "t-watch", "--once", "--no-color")
        for want in ("Claude → agent · spec", "│ Write the answer.",  # what Claude sent
                     "✻ Thought for 1.5s", "It wants 42.",            # its reasoning, timed
                     "⏺ Update(answer.txt)",                          # Claude Code's grammar,
                     "⎿  1 line(s) added, 1 removed", "-41", "+42",   # the patch as a diff
                     "⏺ Wrote 42.",                                   # its own words
                     "1.2k tokens in, read in 1.0s · 80 out in 2.0s · 40 tok/s",
                     "■ tests FAIL", "⎿  $ grep -qx 42 answer.txt", "round 2", "■ tests PASS",
                     "waiting for Claude's review"):
            self.assertIn(want, out)
        lines = [json.loads(x) for x in run_wb("watch", "t-watch", "--json").splitlines()]
        clock = next(x["t"] for x in lines if x["s"] == "clock")
        self.assertIn("context 1.3k of 32K (3%)", clock)  # the agent's context, not a sum
        self.assertNotIn("Σ", clock)
        self.assertLess(out.index("■ tests FAIL"), out.index("round 2"))

    def test_json_lines_for_the_pane_end_with_what_is_happening(self):
        self.start("t-live", "hang")
        deadline = time.time() + 10
        while not store.Task("t-live").status.get("session") and time.time() < deadline:
            time.sleep(0.1)
        lines = [json.loads(x) for x in run_wb("watch", "t-live", "--json").splitlines()]
        run_wb("stop", "t-live")
        self.assertEqual([x["s"] for x in (lines[0], lines[-2], lines[-1])],
                         ["meta", "activity", "clock"])
        self.assertRegex(lines[-2]["t"], r"…")
        self.assertRegex(lines[-1]["t"], r"^⏲ task \d")


class AskServer(Base):
    def test_protocol_and_budget(self):
        self.start("t-ask", "ok")
        wait_phase("t-ask", {"review"})
        task = store.Task("t-ask")
        init = ask_mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                               "params": {"protocolVersion": "2025-03-26"}}, task)
        self.assertEqual(init["result"]["protocolVersion"], "2025-03-26")
        self.assertIsNone(ask_mcp.handle({"jsonrpc": "2.0", "method": "notifications/initialized"},
                                         task))
        tools = ask_mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, task)
        self.assertEqual(tools["result"]["tools"][0]["name"], "ask_reviewer")

        def answer_soon():
            for _ in range(100):
                qid = store.Task("t-ask").status.get("asking")
                if qid:
                    run_wb("answer", "t-ask", "--text", "Use 42.")
                    return
                time.sleep(0.05)

        import threading
        threading.Thread(target=answer_soon).start()
        call = {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "ask_reviewer", "arguments": {"question": "41 or 42?"}}}
        self.assertEqual(ask_mcp.handle(call, task)["result"]["content"][0]["text"], "Use 42.")
        rows, _ = store.read_jsonl(store.home() / "notify.jsonl")
        self.assertIn("question", [r["kind"] for r in rows])
        threading.Thread(target=answer_soon).start()
        ask_mcp.handle(call, task)  # second question: budget is 2
        third = ask_mcp.handle(call, task)["result"]["content"][0]["text"]
        self.assertIn("budget", third)


class ConfigLayer(Base):
    def test_mcp_layer_uses_opencode_2_shape_and_a_launcher(self):
        # OpenCode 2.x reads mcp.servers.<name>; `python -I` would drop PYTHONPATH, so the
        # server is started through bin/wb-ask-mcp, which finds the package itself.
        from workbench import runner
        self.start("t-cfg", "ok")
        wait_phase("t-cfg", {"review"})
        layer = json.loads(runner.write_config_layer(store.Task("t-cfg")).read_text())
        server = layer["mcp"]["servers"]["workbench"]
        self.assertEqual(server["type"], "local")
        self.assertTrue(server["command"][-1].endswith("bin/wb-ask-mcp"))
        self.assertNotIn("-I", server["command"])
        self.assertIs(server["codemode"], False)
        self.assertGreater(server["timeout"]["execution"], 1200 * 1000)
        self.assertEqual(server["environment"]["WB_TASK"], "t-cfg")

    def test_launcher_answers_initialize(self):
        out = subprocess.run(
            [sys.executable, str(ROOT / "bin" / "wb-ask-mcp")], capture_output=True, text=True,
            input=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                              "params": {"protocolVersion": "2025-06-18"}}) + "\n",
            env={**os.environ, "WB_TASK": "nope", "PYTHONPATH": ""}, timeout=20)
        self.assertEqual(json.loads(out.stdout)["result"]["serverInfo"]["name"], "workbench")


class AgentContext(Base):
    def test_claude_instructions_reach_the_agent(self):
        from workbench import context, runner
        tmp = Path(self.tmp.name)
        os.environ["HOME"] = str(tmp / "home")
        synced = tmp / "home" / ".claude" / "skills" / "synced" / "bucket" / "docx"
        synced.mkdir(parents=True)
        (synced / "SKILL.md").write_text("---\nname: docx\n---\n")
        personal = tmp / "home" / ".claude" / "skills" / "publishing-arm"   # the user's own work
        personal.mkdir(parents=True)
        (personal / "SKILL.md").write_text("---\nname: publishing-arm\n---\n")
        (tmp / "home" / ".claude" / "CLAUDE.md").write_text("user rules\n")
        (self.repo / "CLAUDE.md").write_text("repo rules\n")
        (self.repo / "AGENTS.md").write_text("same as agents\n")
        (self.repo / "web").mkdir()
        (self.repo / "web" / "CLAUDE.md").write_text("web rules\n")
        (self.repo / ".claude" / "rules").mkdir(parents=True)
        (self.repo / ".claude" / "rules" / "style.md").write_text("style rules\n")
        (self.repo / ".claude" / "skills" / "add-level").mkdir(parents=True)   # the project's own
        (self.repo / ".claude" / "skills" / "add-level" / "SKILL.md").write_text("---\nname: add-level\n---\n")
        g = lambda *a: subprocess.run(["git", "-C", str(self.repo), *a], check=True)  # noqa
        g("add", ".")
        g("commit", "-qm", "instructions")
        (self.repo / "CLAUDE.local.md").write_text("same as agents\n")  # uncommitted, = AGENTS.md
        self.start("t-ctx", "ok")
        wait_phase("t-ctx", {"review"})
        task = store.Task("t-ctx")
        layer = json.loads(runner.write_config_layer(task).read_text())
        loaded = layer["instructions"]
        self.assertEqual(loaded[0], str(task.dir / "HANDBOOK.md"))   # a copy: no path into workbench
        self.assertEqual(Path(loaded[0]).read_text(), context.HANDBOOK.read_text())
        self.assertIn(str(tmp / "home" / ".claude" / "CLAUDE.md"), loaded)
        self.assertIn(str(task.worktree / "CLAUDE.md"), loaded)        # committed: worktree copy
        self.assertIn(str(task.worktree / ".claude" / "rules" / "style.md"), loaded)
        self.assertNotIn(str(self.repo / "CLAUDE.local.md"), loaded)   # same text as AGENTS.md
        self.assertIn("`web/CLAUDE.md`", Path(loaded[-1]).read_text())  # nested: mapped, not loaded
        denied = {(r["action"], r["resource"]) for r in layer["permissions"]
                  if r["effect"] == "deny"}
        self.assertLessEqual({("question", "*"), ("execute", "*"), ("opencode_*", "*"), ("skill", "docx"), ("skill", "delegate"),
                             ("skill", "publishing-arm")}, denied)
        self.assertNotIn(("skill", "add-level"), denied)

    def test_uncommitted_local_instructions_come_from_the_repo(self):
        from workbench import context
        (self.repo / "CLAUDE.local.md").write_text("my local rules\n")
        self.start("t-local", "ok")
        wait_phase("t-local", {"review"})
        files, nested = context.instruction_files(store.Task("t-local"))
        self.assertIn(self.repo.resolve() / "CLAUDE.local.md", [p.resolve() for p in files])
        self.assertEqual(nested, [])


@unittest.skipUnless(shutil.which("opencode"), "needs the real opencode")
class RealOpenCode(Base):
    """Guards the OpenCode behaviour context.py relies on, with a model server that records."""

    def test_what_the_model_receives(self):
        from workbench import runner
        (self.repo / "CLAUDE.md").write_text("CANARY-CLAUDE-MD: follow me\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "CLAUDE.md"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "rules"], check=True)
        self.start("t-real", "ok")
        wait_phase("t-real", {"review"})
        task = store.Task("t-real")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        out = Path(self.tmp.name) / "requests.jsonl"
        server = subprocess.Popen([sys.executable, str(ROOT / "tests" / "capture_llm.py"),
                                   str(port), str(out)])
        self.addCleanup(server.kill)
        layer_path = runner.write_config_layer(task)
        layer = json.loads(layer_path.read_text())
        layer["provider"] = {"cap": {
            "npm": "@ai-sdk/openai-compatible",
            "options": {"baseURL": f"http://127.0.0.1:{port}/v1", "apiKey": "x"},
            "models": {"cap": {"name": "capture", "tool_call": True,
                               "limit": {"context": 32768, "output": 1024}}}}}
        layer_path.write_text(json.dumps(layer))
        time.sleep(0.5)
        env = {**os.environ, "PWD": str(task.worktree), "OPENCODE_CONFIG": str(layer_path)}
        subprocess.run(["opencode", "run", "--standalone", "-m", "cap/cap", "--format", "json",
                        "say ok"], cwd=task.worktree, env=env, capture_output=True, timeout=120)
        reqs = [json.loads(line) for line in out.read_text().splitlines()]
        agent = max(reqs, key=lambda r: len(r.get("tools", [])))  # not the title request
        text = json.dumps(agent["messages"])
        tools = {t["function"]["name"] for t in agent["tools"]}
        self.assertIn("CANARY-CLAUDE-MD", text)          # CLAUDE.md reaches the model
        self.assertIn("Workbench handbook", text)
        self.assertIn("workbench_ask_reviewer", tools)
        self.assertTrue({"edit", "read", "shell"} <= tools)
        self.assertFalse({"question", "execute", "subagent"} & tools)
        self.assertNotIn("<id>delegate</id>", text)


    def test_sudo_is_refused_even_inside_a_compound_command(self):
        from workbench import runner
        self.start("t-sudo", "ok")
        wait_phase("t-sudo", {"review"})
        task = store.Task("t-sudo")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        out = Path(self.tmp.name) / "requests.jsonl"
        call = {"name": "shell", "arguments": {"command": "cd . && sudo -n true",
                                               "workdir": str(task.worktree)}}
        server = subprocess.Popen([sys.executable, str(ROOT / "tests" / "capture_llm.py"),
                                   str(port), str(out), json.dumps(call)])
        self.addCleanup(server.kill)
        layer_path = runner.write_config_layer(task)
        layer = json.loads(layer_path.read_text())
        layer["provider"] = {"cap": {
            "npm": "@ai-sdk/openai-compatible",
            "options": {"baseURL": f"http://127.0.0.1:{port}/v1", "apiKey": "x"},
            "models": {"cap": {"name": "capture", "tool_call": True,
                               "limit": {"context": 32768, "output": 1024}}}}}
        layer_path.write_text(json.dumps(layer))
        time.sleep(0.5)
        env = {**os.environ, "PWD": str(task.worktree), "OPENCODE_CONFIG": str(layer_path)}
        subprocess.run(["opencode", "run", "--standalone", "-m", "cap/cap", "--auto", "--format",
                        "json", "go"], cwd=task.worktree, env=env, capture_output=True, timeout=120)
        reqs = [json.loads(line) for line in out.read_text().splitlines()]
        answers = [m for r in reqs for m in r.get("messages", []) if m.get("role") == "tool"]
        self.assertTrue(answers, "the fake model's call never came back")
        self.assertIn("permission.rejected", json.dumps(answers[-1]["content"]))


class Discovery(Base):
    def test_local_providers_only_with_overrides(self):
        from workbench import models
        Path(os.environ["WB_MODELS"]).write_text(json.dumps({
            "strata": {"start": "strata launch", "stop": "strata stop"},
            "ollama/qwen3.8-27b": {"skip": True}}))
        found = models.discover()
        self.assertEqual(list(found), ["ollama/mimo-v2.6:9b-q8_0", "strata/strata"])
        self.assertEqual(found["ollama/mimo-v2.6:9b-q8_0"].engine, "ollama")
        self.assertEqual(found["strata/strata"].engine, "server")
        self.assertEqual(found["strata/strata"].start, "strata launch")
        self.assertEqual(models.resolve("mimo").id, "ollama/mimo-v2.6:9b-q8_0")
        self.assertEqual(models.resolve("strata").id, "strata/strata")

    def test_ambiguous_and_unknown_names_are_refused(self):
        from workbench import models
        with self.assertRaisesRegex(KeyError, "matches"):
            models.resolve("ollama/")
        with self.assertRaisesRegex(KeyError, "unknown model"):
            models.resolve("gpt")


class Routing(Base):
    def setUp(self):
        super().setUp()
        Path(os.environ["WB_MODELS"]).write_text(json.dumps({
            "ollama/qwen3.8-27b": {"weights_gb": 13}, "ollama/mimo-v2.6:9b-q8_0": {"weights_gb": 9.7},
            "strata": {"weights_gb": 55}}))

    def result(self, model, size, outcome, minutes=2.0, n=1):
        for _ in range(n):
            store.append_jsonl(store.home() / "results.jsonl", {
                "model": model, "size": size, "outcome": outcome, "minutes": minutes})

    def test_with_no_results_significant_gets_the_largest_and_trivial_the_smallest(self):
        from workbench import router
        top, why = router.choose("significant")
        self.assertEqual(top.model.id, "strata/strata")
        self.assertIn("the largest model allowed significant work; no real tasks yet", why)
        top, why = router.choose("trivial")
        self.assertEqual(top.model.id, "ollama/mimo-v2.6:9b-q8_0")
        self.assertIn("the smallest model allowed trivial work", why)

    def test_the_size_policy_narrows_the_guess(self):
        from workbench import router
        run_wb("model", "sizes", "strata", "trivial")
        self.assertEqual(router.choose("significant")[0].model.id, "ollama/qwen3.8-27b")

    def test_a_poor_record_outweighs_size(self):
        from workbench import router
        self.result("strata/strata", "significant", "discarded", n=4)
        self.result("ollama/qwen3.8-27b", "significant", "accepted", n=3)
        top, why = router.choose("significant")
        self.assertEqual(top.model.id, "ollama/qwen3.8-27b")
        self.assertIn("3/3 real tasks accepted, ~2.0 min", why)
        self.assertEqual(router.rank("significant")[-1].model.id, "strata/strata")

    def test_within_the_margin_size_still_decides(self):
        from workbench import router
        self.result("ollama/qwen3.8-27b", "significant", "accepted", n=3)
        self.result("strata/strata", "significant", "accepted", n=3)   # 4/6 vs 4/5: within 0.15
        self.result("strata/strata", "significant", "discarded", n=1)
        self.assertEqual(router.choose("significant")[0].model.id, "strata/strata")

    def test_set_default_writes_the_pick_to_opencode(self):
        out = run_wb("models", "--set-default")
        self.assertIn("→ strata/strata", out)
        cfg = json.loads(Path(os.environ["WB_OPENCODE_CONFIG"]).read_text())
        self.assertEqual(cfg["model"], "strata/strata")
        self.assertIn("provider", cfg)                # the rest of the config is kept


class Projects(Base):
    """wb init, and wb start's demand for a CLAUDE.md."""
    def git(self, d, *a):
        return subprocess.run(["git", "-C", str(d), *a], capture_output=True, text=True).stdout.strip()

    def test_init_a_new_project(self):
        d = Path(self.tmp.name) / "game"
        os.environ.update(GIT_CONFIG_GLOBAL=str(Path(self.tmp.name) / "gitconfig"))
        (Path(self.tmp.name) / "gitconfig").write_text("[user]\n\temail = t@t\n\tname = t\n")
        out = run_wb("init", str(d))
        self.assertIn("git repo on master", out)
        self.assertIn("next: write CLAUDE.md", out)
        self.assertEqual(self.git(d, "branch", "--show-current"), "master")
        self.assertEqual(self.git(d, "log", "--format=%s"), "Initial commit")
        self.assertEqual(self.git(d, "remote"), "")             # no remote until the user names one
        (d / "CLAUDE.md").write_text("# game\n")
        self.git(d, "add", "CLAUDE.md"); self.git(d, "commit", "-qm", "docs: CLAUDE.md")
        out = run_wb("init", str(d))
        self.assertIn("AGENTS.md → CLAUDE.md", out)
        self.assertIn("CLAUDE.md is current", out)
        self.assertEqual(os.readlink(d / "AGENTS.md"), "CLAUDE.md")
        self.assertEqual(self.git(d, "status", "--porcelain"), "")  # the link is committed
        self.assertIn("already set up", run_wb("init", str(d)))
        with self.assertRaises(SystemExit):
            run_wb("init", str(d / "sub"))                     # inside a repo: not a new project

    def test_an_agents_md_becomes_claude_md(self):
        (self.repo / "AGENTS.md").write_text("# rules\n")
        (self.repo / "CLAUDE.md").unlink()
        subprocess.run(["git", "-C", str(self.repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "agents"], check=True)
        run_wb("init", str(self.repo))
        self.assertEqual((self.repo / "CLAUDE.md").read_text(), "# rules\n")
        self.assertTrue((self.repo / "AGENTS.md").is_symlink())

    def test_start_wants_a_claude_md(self):
        (self.repo / "CLAUDE.md").unlink()
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qam", "no docs"], check=True)
        os.environ["FAKE_SCENARIO"] = "ok"
        with self.assertRaises(SystemExit):
            run_wb("start", "t-blind", "--spec", str(self.spec), "--repo", str(self.repo))
        self.assertFalse(store.Task("t-blind").exists)
        run_wb("start", "t-ok", "--spec", str(self.spec), "--repo", str(self.repo), "--no-claude-md")
        self.assertTrue(store.Task("t-ok").exists)

    def test_accept_says_whether_claude_md_kept_up(self):
        self.start("t-ok", "ok", "--test", TEST_CMD)
        wait_phase("t-ok", {"review"})
        out = run_wb("accept", "t-ok", "-m", "feat: answer")
        self.assertIn("CLAUDE.md last changed 1 commit(s) before", out)


class ModelSet(Base):
    """What the weekly model check uses: add, retire, the size policy, scout's schedule."""
    def config(self):
        return json.loads(Path(os.environ["WB_OPENCODE_CONFIG"]).read_text())

    def test_add_and_retire(self):
        out = run_wb("model", "add", "ollama/new-coder:14b", "--pull", "--label", "New Coder")
        self.assertIn("registered ollama/new-coder:14b", out)
        entry = self.config()["provider"]["ollama"]["models"]["new-coder:14b"]
        self.assertEqual(entry["limit"]["context"], 32768)        # what the provider serves
        self.assertTrue(entry["tool_call"])
        # Ollama ignores max_completion_tokens; max_tokens is the cap it honours
        self.assertEqual(entry["options"]["max_tokens"], 2 * entry["limit"]["output"])
        self.assertIn("ollama/new-coder:14b", run_wb("models"))
        out = run_wb("model", "retire", "ollama/new-coder:14b", "--delete")
        self.assertIn("weights were deleted", out)
        self.assertNotIn("new-coder:14b", self.config()["provider"]["ollama"]["models"])
        with self.assertRaises(SystemExit):
            run_wb("model", "add", "cloud/big")                    # not a local provider

    def test_retire_refuses_the_default(self):
        cfg = self.config(); cfg["model"] = "ollama/qwen3.8-27b"
        Path(os.environ["WB_OPENCODE_CONFIG"]).write_text(json.dumps(cfg))
        with self.assertRaises(SystemExit):
            run_wb("model", "retire", "ollama/qwen3.8-27b")

    def test_size_policy(self):
        run_wb("model", "sizes", "mimo", "trivial")
        from workbench import router
        self.assertNotIn("ollama/mimo-v2.6:9b-q8_0", [s.model.id for s in router.rank("significant")])
        self.assertIn("ollama/mimo-v2.6:9b-q8_0", [s.model.id for s in router.rank("trivial")])
        self.assertIn("not for significant tasks", run_wb("models"))
        run_wb("model", "sizes", "mimo")
        self.assertIn("ollama/mimo-v2.6:9b-q8_0", [s.model.id for s in router.rank("significant")])

    def test_scout_schedule(self):
        with self.assertRaises(SystemExit) as due:
            run_wb("scout", "--due")
        self.assertEqual(due.exception.code, 0)
        out = run_wb("scout")
        self.assertIn("last model check: never", out)
        self.assertIn("ollama/qwen3.8-27b", out)
        run_wb("scout", "--done", "--notes", "kept all")
        with self.assertRaises(SystemExit) as due:
            run_wb("scout", "--due")
        self.assertEqual(due.exception.code, 1)
        self.assertIn("days ago: kept all", run_wb("scout"))

    def test_the_watch_list_comes_back_every_check_until_settled(self):
        self.assertNotIn("watch list", run_wb("scout"))
        run_wb("scout", "--watch", "big model: retry when it fits in VRAM")
        run_wb("scout", "--watch", "runtime: retry after the watchdog fix")
        out = run_wb("scout")
        self.assertIn("1. (", out)
        self.assertIn("big model: retry when it fits in VRAM", out)
        run_wb("scout", "--unwatch", "1")
        out = run_wb("scout")
        self.assertNotIn("big model", out)
        self.assertIn("1. (", out)
        self.assertIn("runtime: retry", out)


class WebChat(Base):
    """wb chat: the page's API (token, host check), receipts, Claude's replies."""
    def setUp(self):
        super().setUp()
        os.environ["WB_CHAT_TOKEN"] = str(Path(self.tmp.name) / "chat-token")
        from workbench import chat
        self.chat = chat
        servers, _ = chat.make_servers(0, ["127.0.0.1"])
        self.srv = servers[0]
        self.port = self.srv.server_address[1]
        import threading
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def call(self, path, body=None, token=None, host="localhost"):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Host": f"{host}:{self.port}"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if body is not None:
            headers["Content-Type"] = "application/json"
        c.request("POST" if body is not None else "GET", path,
                  json.dumps(body) if body is not None else None, headers)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, (json.loads(data) if r.getheader("Content-Type") == "application/json" else data)

    def test_the_page_needs_the_token_and_a_known_host(self):
        tok = self.chat.token()
        self.assertEqual(oct(Path(os.environ["WB_CHAT_TOKEN"]).stat().st_mode & 0o777), "0o600")
        self.assertEqual(self.call("/")[0], 200)                     # the page itself is no secret
        self.assertEqual(self.call("/api/state")[0], 401)
        self.assertEqual(self.call("/api/state", token="wrong")[0], 401)
        self.assertEqual(self.call("/api/send", {"text": "rm -rf"})[0], 401)
        self.assertEqual(self.call("/api/state", token=tok, host="evil.example")[0], 403)  # rebinding
        self.assertEqual(self.call("/api/state", token=tok)[0], 200)
        self.assertEqual(self.call("/api/send", {"text": ""}, token=tok)[0], 400)
        self.assertEqual(self.chat.url(1337), f"http://localhost:1337/#token={tok}")

    def test_static_files_and_nothing_else(self):
        for path, kind in (("/", "text/html"), ("/app.js", "text/javascript"), ("/app.css", "text/css"),
                           ("/icon.svg", "image/svg+xml"), ("/manifest.webmanifest", "application/manifest+json")):
            import http.client
            c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            c.request("GET", path, headers={"Host": "localhost"})
            r = c.getresponse(); r.read(); c.close()
            self.assertEqual((r.status, r.getheader("Content-Type").split(";")[0]), (200, kind), path)
        for path in ("/../chat.py", "/chat.py", "/nope.js", "/%2e%2e/store.py"):
            self.assertEqual(self.call(path)[0], 404, path)

    def test_a_retried_send_lands_once(self):
        tok = self.chat.token()
        a = self.call("/api/send", {"text": "hi", "cid": "c-1"}, token=tok)[1]
        b = self.call("/api/send", {"text": "hi", "cid": "c-1"}, token=tok)[1]
        self.assertEqual(a["message"]["id"], b["message"]["id"])
        self.assertEqual(len(b["state"]["messages"]), 1)
        self.assertEqual(self.call("/api/send", {"text": "x", "cid": 7}, token=tok)[0], 400)

    def test_the_stream_sends_the_whole_state_then_each_change(self):
        import http.client
        tok = self.chat.token()
        self.call("/api/send", {"text": "first"}, token=tok)
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("GET", "/api/stream", headers={"Host": "localhost", "Authorization": f"Bearer {tok}"})
        r = c.getresponse()
        self.assertEqual(r.getheader("Content-Type"), "text/event-stream")

        def event():
            lines = []
            while True:
                line = r.fp.readline().decode().rstrip("\n")
                if not line:
                    if lines:
                        return lines
                    continue
                lines.append(line)
        first = event()
        self.assertEqual(first[0], "event: state")
        self.assertEqual([m["text"] for m in json.loads(first[1][6:])["messages"]], ["first"])
        run_wb("chat", "reply", "--text", "On it.")
        second = event()
        self.assertEqual([m["text"] for m in json.loads(second[1][6:])["messages"]], ["first", "On it."])
        c.close()
        self.assertEqual(self.call("/api/stream")[0], 401)

    def test_a_message_its_receipt_and_the_reply(self):
        tok = self.chat.token()
        code, r = self.call("/api/send", {"text": "how is the game going?"}, token=tok)
        self.assertEqual(code, 200)
        self.assertIsNone(r["listener"])                            # nobody listening yet
        mid = r["message"]["id"]
        pending = [json.loads(x) for x in run_wb("chat", "pending").splitlines()]
        self.assertEqual([p["id"] for p in pending], [mid])
        run_wb("chat", "ack", mid, "--by", "s1")
        self.assertEqual(run_wb("chat", "pending"), "")
        store.write_json(self.chat.chat_dir() / "listener.json",
                         {"session": "s1", "cwd": "/home/u/game", "ts": store.now()})
        run_wb("chat", "reply", "--text", "Step 2 is in review.")
        _, st = self.call("/api/state", token=tok)
        self.assertEqual(st["listener"]["cwd"], "/home/u/game")
        mine, reply = st["messages"]
        self.assertEqual(mine["received"]["by"], "s1")
        self.assertEqual((reply["from"], reply["text"]), ("claude", "Step 2 is in review."))
        self.assertIn("Claude: Step 2 is in review.", run_wb("chat", "log"))


class Detector(unittest.TestCase):
    def test_edits_reset_the_no_edit_counter(self):
        d = events.StuckDetector(no_edit=3)
        mk = lambda tool, i: {"kind": "tool", "tool": tool, "ok": True, "brief": "",  # noqa
                              "sig": f"{tool}:{i}"}
        self.assertIsNone(d.feed(mk("read", 1)))
        self.assertIsNone(d.feed(mk("read", 2)))
        self.assertIsNone(d.feed(mk("edit", 3)))
        self.assertIsNone(d.feed(mk("read", 4)))
        self.assertIsNone(d.feed(mk("read", 5)))
        self.assertIn("without changing", d.feed(mk("read", 6)))


if __name__ == "__main__":
    unittest.main()
