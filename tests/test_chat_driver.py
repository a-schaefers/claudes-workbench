"""The driver's state on the web app (#1355): chat.driver_state, the line the page keeps at the top of
the chat (off, none, idle, working, waiting, in the story's words), from the listener's claim and the
open waiting rows; and who changed a setting (settings.record, changes, changed, the state's
`settings`). A temp WB_HOME, no server."""
import contextlib
import io
import json
import os
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # run as a script too

from workbench import chat, settings, store, story  # noqa: E402
from tests.test_workbench import Base, run_wb  # noqa: E402

CLAUDE = {"session": "s1", "cwd": os.path.expanduser("~/project"), "pid": 1, "parent": 2, "busy": False}
OPENCODE = {"session": "ses_1", "tool": "opencode", "driver": "opencode", "cwd": "/srv/app", "pid": 1, "busy": False}


class DriverState(Base):
    def setUp(self):
        super().setUp()
        store.write_json(chat.chat_dir() / "page.json", {"at": store.now()})  # a page in use: notices are written

    def test_no_live_listener_is_none_named_by_the_last_driver_that_wrote_else_any(self):
        got = chat.driver_state(None, [])
        self.assertEqual((got["state"], got["driver"], got["who"]), ("none", "any", "Claude or OpenCode"))  # never "your driver" (#1339)
        self.assertEqual(got["line"], "No session is listening: messages wait.")
        self.assertEqual(got["next"], "Start Claude Code or OpenCode in a terminal and run /workbench: it answers here.")
        rows = [{"id": "1", "from": "claude", "text": "hi", "ts": 1, "driver": {"tool": "opencode"}}]
        got = chat.driver_state(None, rows)
        self.assertEqual((got["state"], got["who"]), ("none", "OpenCode"))
        self.assertEqual(got["next"], "Start OpenCode in a terminal and type in a session: it answers here.")
        self.assertNotIn("waiting", got)

    def test_the_state_line_says_the_chat_is_off_and_how_to_turn_it_on(self):
        # #1526 (2026-11-26): with the chat off (a service kept the server up), the page told the
        # user to start their driver and run /workbench, which can't make it listen: only wb chat on
        chat.set_on(False)
        got = chat.driver_state(None, [])
        self.assertEqual(got["state"], "off")
        self.assertEqual(got["line"], "The web chat is off: no session listens here.")
        self.assertEqual(got["next"], "Turn it on in a terminal: `wb chat on`.")
        self.assertNotIn("/workbench", got["next"])
        self.assertEqual(chat.snapshot()["driver"]["state"], "off")
        chat.set_on(True)
        self.assertEqual(chat.driver_state(None, [])["state"], "none")

    def test_a_listener_is_idle_or_working_where_it_runs_named_by_its_driver(self):
        got = chat.driver_state(CLAUDE, [])
        self.assertEqual((got["state"], got["driver"], got["where"]), ("idle", "claude-code", "~/project"))
        self.assertEqual((got["line"], got["next"]), ("Claude is listening in ~/project.", "Send a message: Claude answers here."))
        got = chat.driver_state({**OPENCODE, "busy": True}, [])
        self.assertEqual((got["state"], got["who"]), ("working", "OpenCode"))
        self.assertEqual(got["line"], "OpenCode is working in /srv/app.")
        self.assertNotIn("grant", got)

    def test_waiting_beats_working_names_the_claims_prompt_and_its_settling_clears_it(self):
        # the host writes the oldest dialog up in its terminal as the claim's `waiting` (#1355)
        perm = {"kind": "permission", "tool": "Bash", "since": 1.0, "what": "rm -rf runs/*"}
        got = chat.driver_state({**CLAUDE, "busy": True, "waiting": perm}, [])
        self.assertEqual(got["state"], "waiting")
        self.assertEqual(got["line"], "Claude is waiting for you in the terminal: a permission for Bash.")
        self.assertEqual(got["next"], "Answer it in the terminal; a message sent now waits behind it.")
        self.assertEqual((got["waiting"]["kind"], got["waiting"]["what"]), ("permission", "rm -rf runs/*"))
        self.assertEqual(got["grant"], {"line": "To work from the chat, run Claude Code with permissions granted: "
                                                "`claude --permission-mode auto`.", "command": "claude --permission-mode auto"})
        plan = {"kind": "plan", "tool": "ExitPlanMode", "since": 2.0}
        got = chat.driver_state({**CLAUDE, "busy": True, "waiting": plan}, [])
        self.assertEqual(got["line"], "Claude is waiting for you in the terminal: a plan to approve.")
        self.assertNotIn("grant", got)  # a plan waits however the driver runs
        self.assertEqual(chat.driver_state({**CLAUDE, "busy": True}, [])["state"], "working")

    def test_another_sessions_open_notice_never_reads_as_the_listener_waiting(self):
        # #1481 (2026-11-26): with OpenCode listening, a Claude Code session's dialog (the mod posts
        # from every delegating session) read as OpenCode waiting, with OpenCode's grant, and each
        # unread message as behind a prompt; a dead session's open notice did so for an hour. The
        # listener's own wait is its claim's: both hosts write it
        me = os.getpid()
        self.assertTrue(chat.hold("ses_abc123", me, cwd="/srv/proj")[0])
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "other"}):
            run_wb("chat", "notice", "--tool=Bash", "--kind=permission", "--text=rm -rf build")
        chat.notice("Bash", "make")                                     # unstamped, as an older mod's
        got = chat.driver_state()
        self.assertEqual((got["state"], got["line"]), ("idle", "OpenCode is listening in /srv/proj."))
        self.assertNotIn("grant", got)
        self.assertNotIn("waiting", got)
        # its own dialog, as its claim says, still waits
        chat.hold("ses_abc123", me, cwd="/srv/proj", waiting={"kind": "permission", "tool": "bash", "since": 1.0})
        self.assertEqual(chat.driver_state()["grant"]["command"], "opencode --auto")

    def test_the_claims_own_wait_wins_and_only_a_permission_names_the_grant(self):
        for kind, what in (("question", "a question"), ("elicitation", "a form from github"),
                           ("consent", "a yes for github"), ("permission", "a permission for github")):
            with self.subTest(kind=kind):
                lst = {**OPENCODE, "busy": True, "waiting": {"kind": kind, "tool": "mcp:github", "since": 5.0}}
                got = chat.driver_state(lst, [])
                self.assertEqual(got["line"], f"OpenCode is waiting for you in the terminal: {what}.")
                self.assertEqual(got["waiting"], {"kind": kind, "tool": "mcp:github", "since": 5.0})
                self.assertEqual("grant" in got, kind == "permission")
        got = chat.driver_state({**OPENCODE, "waiting": {"kind": "permission", "tool": "bash", "since": 5.0}}, [])
        self.assertEqual(got["grant"]["command"], "opencode --auto")
        # an odd fact is no wait
        for odd in ({"kind": "nap", "tool": "x", "since": 1.0}, {"kind": "plan", "since": True}, "permission"):
            with self.subTest(odd=odd):
                self.assertEqual(chat.driver_state({**OPENCODE, "waiting": odd}, [])["state"], "idle")

    def test_any_driver_is_never_told_a_grant(self):
        with self.assertRaises(ValueError):
            story.text("next.grant", "any")
        lst = {**CLAUDE, "waiting": {"kind": "permission", "tool": "Bash", "since": 1.0}}
        with mock.patch.object(chat, "listener_driver", return_value={"tool": "cursor"}):
            got = chat.driver_state(lst, [])
        self.assertEqual((got["driver"], got["state"]), ("any", "waiting"))
        self.assertNotIn("grant", got)

    def test_the_opencode_plugins_claim_carries_its_wait_and_drops_it_once_settled(self):
        me = os.getpid()
        fact = {"kind": "permission", "tool": "bash", "since": 1800000000.0, "what": "rm -rf build"}
        ok, claim = chat.hold("ses_a", me, True, cwd="/srv/app", waiting=fact)
        self.assertTrue(ok)
        self.assertEqual(chat.listener()["waiting"], fact)
        got = chat.driver_state(chat.listener(), [])
        self.assertEqual(got["line"], "OpenCode is waiting for you in the terminal: a permission for bash.")
        chat.hold("ses_a", me, True, cwd="/srv/app")                  # settled: the next hold says none
        self.assertNotIn("waiting", chat.listener())
        self.assertEqual(chat.driver_state(chat.listener(), [])["state"], "working")
        for odd in ({"kind": "nap", "tool": "x", "since": 1.0}, {"kind": "plan", "tool": "x", "since": True},
                    {"kind": "plan", "since": 1.0}, ["plan"]):
            with self.subTest(odd=odd), self.assertRaises(ValueError):
                chat.hold("ses_a", me, waiting=odd)
        # wb chat listener --waiting, the plugin's call
        self.assertEqual(run_wb("chat", "listener", "ses_a", "--pid", str(me), "--waiting", json.dumps(fact)).strip(), "listening")
        self.assertEqual(chat.listener()["waiting"], fact)
        with self.assertRaises(SystemExit) as e, contextlib.redirect_stderr(io.StringIO()):
            run_wb("chat", "listener", "ses_a", "--pid", str(me), "--waiting", "{")
        self.assertEqual(e.exception.code, 1)
        # OpenCode's form names no server: the line says an MCP server's
        lst = {**OPENCODE, "waiting": {"kind": "elicitation", "tool": "form", "since": 1.0}}
        self.assertEqual(chat.driver_state(lst, [])["line"],
                         "OpenCode is waiting for you in the terminal: a form from an MCP server.")

    def test_wb_chat_log_ends_with_the_pages_state_line_word_for_word(self):
        # #1355 S2b: `wb chat log` said "listening: <cwd>" or "no session is listening", in words
        # of its own; it ends with the line the page keeps at the top, a waiting dialog's too
        self.assertTrue(run_wb("chat", "log").endswith("No session is listening: messages wait.\n"))
        with mock.patch.object(chat, "listener", return_value={**CLAUDE, "busy": True}):
            self.assertTrue(run_wb("chat", "log").endswith(f"Claude is working in {chat._tilde(CLAUDE['cwd'])}.\n"))
        wait = {"kind": "permission", "tool": "Bash", "since": 1.0, "what": "make deploy"}
        with mock.patch.object(chat, "listener", return_value={**CLAUDE, "busy": True, "waiting": wait}):
            self.assertTrue(run_wb("chat", "log").endswith(
                "Claude is waiting for you in the terminal: a permission for Bash.\n"))

    def test_the_snapshot_carries_the_driver_state_and_the_newest_settings_change(self):
        snap = chat.snapshot()
        self.assertEqual(snap["driver"]["state"], "none")
        self.assertIsNone(snap["settings"])
        settings.record({"key": "coding", "label": "Claude coding", "kind": "choice", "value": "decide"},
                        "coding", "ask", {"said": "ok"}, "claude")
        got = chat.snapshot()["settings"]
        self.assertEqual({k: got[k] for k in ("key", "label", "to", "by")},
                         {"key": "coding", "label": "Claude coding", "to": "ask", "by": "claude"})


class SettingsProvenance(Base):
    ROW = {"key": "coding", "label": "Claude coding", "kind": "choice", "value": "decide"}

    def lines(self) -> list[dict]:
        p = chat.changes_path()
        return [json.loads(ln) for ln in p.read_text().splitlines()] if p.exists() else []

    def test_a_change_to_the_value_a_row_had_logs_nothing(self):
        self.assertIsNone(settings.record(self.ROW, "coding", "decide", {"said": "ok"}, "web"))
        cap = {"key": "cap", "label": "API day cap", "kind": "number", "value": 5.0}
        self.assertIsNone(settings.record(cap, "cap", "5", {"said": "ok"}, "web"))
        self.assertIsNone(settings.record({**cap, "value": None}, "cap", "off", {"said": "ok"}, "web"))
        self.assertEqual(self.lines(), [])
        self.assertFalse((chat.chat_dir() / chat.SETTINGS_REV).exists())
        # an action always is one: its buttons do something each time
        act = {"key": "optimize", "label": "Optimize", "kind": "action"}
        self.assertIsNotNone(settings.record(act, "optimize", "run", {"said": "ok"}, "web"))
        self.assertIsNotNone(settings.record(cap, "cap", "6", {"said": "ok"}, "web"))
        self.assertEqual([r["key"] for r in self.lines()], ["optimize", "cap"])

    def test_changes_come_newest_first_and_each_row_its_newest_change_within_a_week(self):
        now = store.now()
        for ts, key, by in ((now - 8 * 86400, "splash", "web"), (now - 60, "coding", "web"), (now - 5, "coding", "opencode")):
            store.append_jsonl(chat.changes_path(), {"ts": ts, "key": key, "label": key, "from": "a", "to": "b",
                                                     "said": "x", "by": by})
        self.assertEqual([(c["key"], c["by"]) for c in settings.changes()],
                         [("coding", "opencode"), ("coding", "web"), ("splash", "web")])
        self.assertEqual(set(settings.changes()[0]), {"ts", "key", "label", "from", "to", "by"})  # never `said`
        self.assertEqual(settings.changed(), {"coding": {"ts": now - 5, "by": "opencode"}})
        rows = [{"key": "coding", "kind": "choice", "value": "b"}, {"key": "splash", "kind": "choice", "value": "b"}]
        with mock.patch.object(settings, "_fresh", return_value={"rows": rows}):
            got = settings.rows()
        self.assertEqual(got[0]["changed"], {"ts": now - 5, "by": "opencode"})
        self.assertNotIn("changed", got[1])

    def test_a_change_stamps_the_state_so_an_open_page_reads_its_rows_again(self):
        entry = settings.record(self.ROW, "coding", "ask", {"said": "ok"}, "claude")
        self.assertEqual(settings.rev(), {"ts": entry["ts"], "key": "coding", "label": "Claude coding", "to": "ask", "by": "claude"})
        self.assertEqual(chat.settings_rev(), settings.rev())


if __name__ == "__main__":
    import unittest
    unittest.main()
