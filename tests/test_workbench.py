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

from workbench import ask_mcp, cli, events, store  # noqa: E402

FAKE = f"{sys.executable} {ROOT / 'tests' / 'fake_opencode.py'}"
TEST_CMD = "grep -qx 42 answer.txt"


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
        os.environ.update(WB_HOME=str(tmp / "state"), WB_OPENCODE_BIN=FAKE, WB_FAKE_MODELS="1")
        self.repo = tmp / "repo"
        self.repo.mkdir()
        g = lambda *a: subprocess.run(["git", "-C", str(self.repo), *a], check=True,  # noqa
                                      capture_output=True)
        g("init", "-q", "-b", "main")
        (self.repo / "README.md").write_text("hello\n")
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
        msg = Path(self.tmp.name) / "fb.md"
        msg.write_text("1. The answer is 42.\n")
        run_wb("feedback", "t-fb", "--message", str(msg))
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

    def test_wait_returns_when_the_notice_was_taken_elsewhere(self):
        # The mod (or a second waiter) acknowledged the review first: a named wait must not hang.
        self.start("t-taken", "ok")
        wait_phase("t-taken", {"review"})
        self.assertIn("[review] t-taken", run_wb("wait", "t-taken", "--timeout", "1"))
        out = run_wb("wait", "t-taken", "--timeout", "1")
        self.assertIn("already delivered", out)

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
        (tmp / "home" / ".claude" / "CLAUDE.md").write_text("user rules\n")
        (self.repo / "CLAUDE.md").write_text("repo rules\n")
        (self.repo / "AGENTS.md").write_text("same as agents\n")
        (self.repo / "web").mkdir()
        (self.repo / "web" / "CLAUDE.md").write_text("web rules\n")
        (self.repo / ".claude" / "rules").mkdir(parents=True)
        (self.repo / ".claude" / "rules" / "style.md").write_text("style rules\n")
        g = lambda *a: subprocess.run(["git", "-C", str(self.repo), *a], check=True)  # noqa
        g("add", ".")
        g("commit", "-qm", "instructions")
        (self.repo / "CLAUDE.local.md").write_text("same as agents\n")  # uncommitted, = AGENTS.md
        self.start("t-ctx", "ok")
        wait_phase("t-ctx", {"review"})
        task = store.Task("t-ctx")
        layer = json.loads(runner.write_config_layer(task).read_text())
        loaded = layer["instructions"]
        self.assertEqual(loaded[0], str(context.HANDBOOK))
        self.assertIn(str(tmp / "home" / ".claude" / "CLAUDE.md"), loaded)
        self.assertIn(str(task.worktree / "CLAUDE.md"), loaded)        # committed: worktree copy
        self.assertIn(str(task.worktree / ".claude" / "rules" / "style.md"), loaded)
        self.assertNotIn(str(self.repo / "CLAUDE.local.md"), loaded)   # same text as AGENTS.md
        self.assertIn("`web/CLAUDE.md`", Path(loaded[-1]).read_text())  # nested: mapped, not loaded
        denied = {(r["action"], r["resource"]) for r in layer["permissions"]
                  if r["effect"] == "deny"}
        self.assertLessEqual({("question", "*"), ("execute", "*"), ("opencode_*", "*"), ("skill", "docx"), ("skill", "delegate")}, denied)

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
