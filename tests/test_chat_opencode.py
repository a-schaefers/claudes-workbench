"""The web chat on OpenCode's path (#1319, #1340; the host-shape decision, #1296, 2026-11-25): the
OpenCode server whose plugin delivers the messages is the listener, by its pid and heartbeat; wb
answers its claim (`wb chat listener`), away and back, the waiting line in its driver's words, and
the turn's own answer only where its session still owes one (`wb chat reply --if-owed`). The
plugin's side: opencode-plugin/test/chat.test.ts."""
import contextlib
import io
import os
import sys
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # run as a script too

from workbench import chat, models, store  # noqa: E402
from tests.test_workbench import Base, run_wb  # noqa: E402

ME = os.getpid()


def fails(*argv) -> str:
    """run_wb for a call that exits non-zero: what it said on stderr."""
    err = io.StringIO()
    with redirect_stderr(err), contextlib.suppress(SystemExit):
        run_wb(*argv)
    return err.getvalue()


class OpenCodeChat(Base):
    def setUp(self):
        super().setUp()
        os.environ["WB_CHAT_TOKEN"] = str(Path(self.tmp.name) / "chat-token")
        os.environ["WB_CHAT_CONFIG"] = str(Path(self.tmp.name) / "chat.json")  # not the user's
        chat.chat_dir()

    def claim(self, session="ses_a", pid=ME, **kw):
        store.write_json(chat.chat_dir() / "listener.json", {
            "session": session, "tool": "opencode", "pid": pid, "start": models._started(pid),
            "machine": store.machine(), "cwd": "/p", "busy": False, **kw})

    def beat(self, *sessions, **kw):
        store.write_json(store.beat_path(ME), {"tool": "opencode", "pid": ME, "ts": time.time(), **kw,
                                               "sessions": {s: {} for s in sessions}})

    def as_session(self, sid):
        return mock.patch.dict(os.environ, {"OPENCODE_SESSION_ID": sid})

    def user_message(self, text="status?"):
        return chat.send(text, "user")

    def test_an_opencode_server_listens_while_it_runs_and_its_beat_lists_the_session(self):
        self.claim()
        self.assertEqual(chat.listener()["session"], "ses_a")          # no beat yet: its pid
        self.beat("ses_b", store="/db/opencode.db")                    # its whole store, without it
        self.assertIsNone(chat.listener())                             # a session deleted there
        self.beat("ses_a", "ses_b", store="/db/opencode.db")
        self.assertEqual(chat.listener()["session"], "ses_a")
        self.beat("ses_b")                                             # a listing that can't say gone
        self.assertEqual(chat.listener()["session"], "ses_a")
        self.claim(start="1")                                          # our pid, another start: recycled
        self.assertIsNone(chat.listener())

    def test_a_claim_newer_than_the_servers_last_beat_listens_until_a_beat_can_say(self):
        # #1319's live check (2026-11-26): the user's first prompt in a new session claimed the
        # chat up to 30 s before the next beat listed it, and the page said "no session listening"
        self.beat("ses_old", store="/db/opencode.db", ts=time.time() - 20)  # listed before the session was
        self.claim("ses_new")
        self.assertEqual(chat.listener()["session"], "ses_new")
        self.beat("ses_old", store="/db/opencode.db", ts=time.time() + 1)   # a listing after the claim
        self.assertIsNone(chat.listener())                                  # without it: deleted there

    def test_the_server_takes_the_chat_moves_it_between_its_sessions_and_drops_it(self):
        self.assertEqual(run_wb("chat", "listener", "ses_a", "--pid", str(ME), "--busy").strip(), "listening")
        lst = store.read_json(chat.chat_dir() / "listener.json", None)
        self.assertEqual((lst["session"], lst["tool"], lst["pid"], lst["busy"], lst["start"]),
                         ("ses_a", "opencode", ME, True, models._started(ME)))
        self.assertEqual(run_wb("chat", "listener", "ses_b", "--pid", str(ME)).strip(), "listening")
        self.assertEqual(chat.listener()["session"], "ses_b")            # the user typed there
        self.assertEqual(run_wb("chat", "listener", "--drop", "--pid", "12345").strip(), "not this server's")
        self.assertEqual(run_wb("chat", "listener", "--drop", "--pid", str(ME)).strip(), "dropped")
        self.assertIsNone(chat.listener())

    def test_the_servers_claim_names_opencode_and_its_sessions_model_to_the_api(self):
        # the merge with #1366 (2026-11-25): the API names the listening driver by the claim's
        # `driver` or a beat, and the plugin's claim gave neither shape wb #1366 read
        run_wb("chat", "listener", "ses_a", "--pid", str(ME))
        self.assertEqual(chat.listener_driver(chat.listener()), {"tool": "opencode"})
        store.write_json(store.beat_path(ME), {"tool": "opencode", "pid": ME, "ts": time.time(),
                                               "sessions": {"ses_a": {"model": "prov/m1"}}})
        self.assertEqual(chat.listener_view(chat.listener())["driver"], {"tool": "opencode", "model": "prov/m1"})

    def test_another_live_listener_stands_and_a_dead_one_gives_way(self):
        # first live claim wins, either driver's: a Claude Code session's `wb events` here
        store.write_json(chat.chat_dir() / "listener.json",
                         {"session": "claude-key", "cwd": "/c", "pid": ME, "start": models._started(ME)})
        self.assertIn("standing by: claude listens in /c", fails("chat", "listener", "ses_a", "--pid", "777"))
        self.assertEqual(chat.listener()["session"], "claude-key")
        store.write_json(chat.chat_dir() / "listener.json",
                         {"session": "claude-key", "cwd": "/c", "pid": ME, "start": "1"})  # gone
        self.assertEqual(run_wb("chat", "listener", "ses_a", "--pid", str(ME)).strip(), "listening")

    def test_the_listening_sessions_reply_threads_onto_its_message_and_another_sessions_goes_to_the_user(self):
        self.claim()
        m = self.user_message()
        run_wb("chat", "ack", m["id"], "--by", "ses_a")
        with self.as_session("ses_other"):
            run_wb("chat", "reply", "--text", "another session's report")
        r = chat.thread()[-1]
        self.assertEqual((r.get("re"), r["driver"]["tool"], r["opencode_session"]), (None, "opencode", "ses_other"))
        self.assertNotIn("session", r)  # never one the Claude Code mod would hand a message to
        with mock.patch.dict(os.environ, {"CLAUDE_PID": "9100"}):  # a Claude Code session's
            run_wb("chat", "reply", "--text", "a Claude Code report")
        self.assertIsNone(chat.thread()[-1].get("re"))
        with self.as_session("ses_a"):
            run_wb("chat", "reply", "--text", "all green")
        r = chat.thread()[-1]
        self.assertEqual((r["re"], r["to"], r["turn_by"]), ([m["id"]], ["user"], "ses_a"))
        self.assertIn("OpenCode: all green", run_wb("chat", "log"))
        self.assertIn("listening: /p (OpenCode)", run_wb("chat", "log"))

    def test_an_opencode_sessions_report_never_threads_onto_a_claude_code_listeners_message(self):
        store.write_json(chat.chat_dir() / "listener.json",
                         {"session": "s1", "cwd": "/c", "pid": ME, "start": models._started(ME), "parent": 9000})
        m = self.user_message()
        run_wb("chat", "ack", m["id"], "--by", "s1")
        with self.as_session("ses_a"):
            run_wb("chat", "reply", "--text", "OpenCode's report")
        self.assertIsNone(chat.thread()[-1].get("re"))
        with mock.patch.dict(os.environ, {"CLAUDE_PID": "9000"}):  # the listener's own, as before
            run_wb("chat", "reply", "--text", "Claude's answer")
        self.assertEqual(chat.thread()[-1]["re"], [m["id"]])

    def test_the_turns_answer_goes_only_where_the_session_still_owes_one(self):
        # #1340 F on OpenCode: the plugin posts the turn's answer unless a row of the session's
        # answered; one of another session's never settles it
        self.claim()
        m1, m2 = self.user_message("first?"), self.user_message("second?")
        for m in (m1, m2):
            run_wb("chat", "ack", m["id"], "--by", "ses_a")
        with self.as_session("ses_other"):
            run_wb("chat", "reply", "--to", m2["id"], "--text", "answered m2 elsewhere")  # by `re`
            run_wb("chat", "reply", "--text", "another session's plain report")
        with self.as_session("ses_a"):
            self.assertEqual(chat.owed([m1["id"], m2["id"]], "ses_a"), [m1["id"]])
            out = run_wb("chat", "reply", "--turn-answer", "--if-owed", "--text", "the answer",
                         "--to", m1["id"], "--to", m2["id"])
        self.assertEqual(out.strip(), f"sent to the web chat: {m1['id']}")
        r = chat.thread()[-1]
        self.assertEqual((r["re"], r["auto"], r["text"]), ([m1["id"]], True, "the answer"))
        with self.as_session("ses_a"):
            self.assertEqual(chat.owed([m1["id"]], "ses_a"), [])         # its own post, by `re`
            run_wb("chat", "reply", "--text", "a plain reply")
            m3 = self.user_message("third?")
            run_wb("chat", "ack", m3["id"], "--by", "ses_a")
            self.assertEqual(chat.owed([m3["id"]], "ses_a"), [m3["id"]])  # a reply before it isn't one
            run_wb("chat", "reply", "--text", "a plain reply to it")
            self.assertEqual(chat.owed([m3["id"]], "ses_a"), [])
            self.assertEqual(run_wb("chat", "reply", "--turn-answer", "--if-owed", "--text", "x",
                                    "--to", m3["id"]).strip(), "not owed: the session replied")
            # the plugin's own post settles only by `re`: m4's answer never settles m5 (#1352's review)
            m4, m5 = self.user_message("fourth?"), self.user_message("fifth?")
            for m in (m4, m5):
                run_wb("chat", "ack", m["id"], "--by", "ses_a")
            run_wb("chat", "reply", "--turn-answer", "--if-owed", "--text", "four", "--to", m4["id"])
            self.assertEqual(chat.owed([m4["id"], m5["id"]], "ses_a"), [m5["id"]])
        self.assertIn("--if-owed goes with --turn-answer and --to",
                      fails("chat", "reply", "--if-owed", "--text", "x"))

    def test_an_agents_message_is_owed_until_a_row_to_that_agent(self):
        # #1273: a plain reply to the user never settles an agent's debt
        self.claim()
        a = chat.send("please summarize", "hermes", role="agent")
        run_wb("chat", "ack", a["id"], "--by", "ses_a", "--how", "into")
        with self.as_session("ses_a"):
            run_wb("chat", "reply", "--to", "user", "--text", "to the user")
            self.assertEqual(chat.owed([a["id"]], "ses_a"), [a["id"]])
            run_wb("chat", "reply", "--to", a["id"], "--text", "to hermes")
            self.assertEqual(chat.owed([a["id"]], "ses_a"), [])

    def test_away_starts_with_a_delivered_web_message_and_ends_when_the_user_is_back(self):
        m = self.user_message()
        self.assertEqual(run_wb("chat", "away").strip(), "here")
        run_wb("chat", "ack", m["id"], "--by", "ses_a", "--away")
        self.assertEqual(store.read_json(chat.chat_dir() / "away.json", None)["by"], "ses_a")
        self.assertEqual(run_wb("chat", "away").strip(), "away")
        a = chat.send("hi", "hermes", role="agent")
        run_wb("chat", "ack", a["id"], "--by", "ses_a", "--how", "into")  # an agent's: no --away
        self.assertEqual(run_wb("chat", "back").strip(), "back")
        self.assertEqual(run_wb("chat", "away").strip(), "here")
        self.assertEqual(store.read_json(chat.chat_dir() / "away.json", None), {"at": 0})

    def test_away_outlasts_six_hours_while_one_of_the_sessions_tasks_is_open(self):
        store.write_json(chat.chat_dir() / "away.json", {"at": store.now() - chat.AWAY_FOR - 60, "by": "ses_a"})
        with self.as_session("ses_a"):
            self.assertEqual(run_wb("chat", "away").strip(), "here")
            t = store.Task("oc1")
            t.dir.mkdir(parents=True)
            store.write_json(t.dir / "task.json", {"name": "oc1", "session": "ses_a", "created_at": store.now()})
            t.set_status(phase="running", round=1)
            self.assertEqual(run_wb("chat", "away").strip(), "away")
            t.set_status(phase="review", parked=True)                  # parked: waits on no one
            self.assertEqual(run_wb("chat", "away").strip(), "here")
        with self.as_session("ses_other"):
            t.set_status(phase="review", parked=False)
            self.assertEqual(run_wb("chat", "away").strip(), "here")    # another session's task
        chat.set_on(False)
        store.write_json(chat.chat_dir() / "away.json", {"at": store.now(), "by": "ses_a"})
        self.assertEqual(run_wb("chat", "away").strip(), "here")        # the chat off: never away

    def test_the_waiting_line_names_the_driver_whose_dialog_waits(self):
        # #1340 E, labels from the driver: OpenCode's dialog says OpenCode; Claude Code's as before
        chat.active(desk=True)  # a page in use: the user uses the chat
        with self.as_session("ses_a"):
            nid = run_wb("chat", "notice", "--tool", "shell", "--kind", "permission", "--text", "rm -rf build").strip()
        row = next(r for r in chat.thread() if r["id"] == nid)
        self.assertEqual(row["text"], "OpenCode is waiting for you in the terminal." + chat.WAITING_GRANT)
        self.assertEqual(row["driver"], {"tool": "opencode"})  # a reply's shape (#1366)
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1", "OPENCODE_SESSION_ID": "ses_a"}):
            nid = run_wb("chat", "notice", "--tool", "AskUserQuestion", "--kind", "question").strip()
        row = next(r for r in chat.thread() if r["id"] == nid)
        self.assertEqual((row["text"], row.get("driver")), (chat.WAITING, None))

    def test_a_push_names_the_driver_by_its_reply_and_the_listener_it_fetches(self):
        # #1319: the service worker titled every push "Claude", OpenCode's replies too: /api/latest
        # gave it the row alone, and a row with no `driver` needs the listener's to be named
        self.assertIsNone(chat.latest()["listener"])
        run_wb("chat", "listener", "ses_a", "--pid", str(ME))
        with self.as_session("ses_a"):
            run_wb("chat", "reply", "--to", "user", "--text", "Tests pass")
        got = chat.latest()
        self.assertEqual(got["messages"][-1]["driver"], {"tool": "opencode"})   # the title's
        self.assertEqual(got["listener"]["driver"], {"tool": "opencode"})       # the fallback's
        self.assertEqual(got["listener"]["tool"], "opencode")


if __name__ == "__main__":
    unittest.main()
