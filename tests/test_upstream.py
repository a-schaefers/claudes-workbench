"""Posting to GitHub (workbench/upstream.py; the owner, 2026-11-27: "i walk away from the computer,
so i cannot approve your requests to post ... work stalls"): auto, daily (the default) or never,
and the outbox a post waits in, never a dialog mid-work."""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # run as a script too

from workbench import settings, store, upstream  # noqa: E402
from tests.test_workbench import Base, run_wb  # noqa: E402


def ok(stdout="https://github.com/x/y/issues/9\n"):
    return subprocess.CompletedProcess([], 0, stdout, "")


class PostingToGitHub(Base):
    def setUp(self):
        super().setUp()
        self.body = Path(self.tmp.name) / "body.md"
        self.body.write_text("what went wrong, and how to see it\n")
        self.gh = mock.patch.object(upstream, "gh", return_value=ok())
        self.ran = self.gh.start()
        self.addCleanup(self.gh.stop)

    def post(self, *extra):
        return run_wb("upstream", "post", "issue", "--title", "The push fails", "--body-file", str(self.body),
                      "--repo", "x/y", *extra)

    def test_the_default_is_once_a_day_and_an_unreadable_mode_is_never_read_as_auto(self):
        self.assertEqual(upstream.mode(), "daily")
        upstream.path().write_text("{not json")
        self.assertEqual(upstream.mode(), "daily")
        upstream.path().write_text(json.dumps({"mode": "auto"}))
        self.assertEqual(upstream.mode(), "auto")

    def test_only_the_user_sets_the_mode_never_a_models_call(self):
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1"}):
            with self.assertRaises(SystemExit):
                run_wb("upstream", "mode", "auto")
        self.assertEqual(upstream.mode(), "daily")
        self.assertIn("without asking", run_wb("upstream", "mode", "auto"))  # the user's own terminal
        self.assertEqual(upstream.mode(), "auto")

    def test_in_daily_mode_claudes_post_waits_in_the_outbox_and_nothing_is_sent(self):
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1"}):
            out = self.post()
        self.assertIn("queued, not posted yet", out)
        self.ran.assert_not_called()
        (row,) = upstream.items()
        self.assertEqual((row["kind"], row["repo"], row["title"]), ("issue", "x/y", "The push fails"))
        self.body.write_text("changed after\n")                       # the body was copied in
        self.assertEqual(upstream.body_path(row["id"]).read_text(), "what went wrong, and how to see it\n")
        # Claude can't flush it without the user's yes
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1"}), self.assertRaises(SystemExit):
            run_wb("upstream", "send")
        self.ran.assert_not_called()

    def test_the_daily_yes_sends_exactly_the_posts_it_named_once(self):
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1"}):
            self.post()
            self.post()
        ids = [r["id"] for r in upstream.items()]
        store.write_json(store.home() / upstream.OK, {"ids": ids[:1], "at": store.now()})
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1"}), self.assertRaises(SystemExit):
            run_wb("upstream", "send")                                 # a yes to one is no yes to two
        self.ran.assert_not_called()
        store.write_json(store.home() / upstream.OK, {"ids": ids, "at": store.now()})
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1"}):
            out = run_wb("upstream", "send", *ids)
        self.assertEqual(self.ran.call_count, 2)
        self.assertIn("sent", out)
        self.assertEqual(upstream.items(), [])
        argv = self.ran.call_args_list[0].args
        self.assertEqual(argv[:6], ("issue", "create", "-R", "x/y", "--title", "The push fails"))

    def test_in_auto_mode_claudes_post_goes_at_once_and_in_never_mode_it_stays_a_draft(self):
        upstream.path().write_text(json.dumps({"mode": "auto"}))
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1"}):
            self.assertIn("sent", self.post())
        self.ran.assert_called_once()
        self.assertEqual(upstream.items(), [])
        upstream.path().write_text(json.dumps({"mode": "never"}))
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1"}):
            self.assertIn("kept as a draft, not posted", self.post())
            with self.assertRaises(SystemExit):
                run_wb("upstream", "send")
        self.assertEqual(self.ran.call_count, 1)
        self.assertEqual(len(upstream.items()), 1)

    def test_an_agent_posts_nothing_and_a_failed_post_stays_with_its_error(self):
        with mock.patch.dict(os.environ, {"WB_TASK": "t-1"}), self.assertRaises(SystemExit):
            self.post()
        self.assertEqual(upstream.items(), [])
        self.ran.return_value = subprocess.CompletedProcess([], 1, "", "HTTP 422: Validation Failed")
        out = self.post()                                              # the user's own: sent at once
        self.assertIn("not sent", out)
        (row,) = upstream.items()
        self.assertIn("422", row["error"])

    def test_the_hosts_ask_once_a_day_and_only_while_something_waits(self):
        self.assertEqual(json.loads(run_wb("upstream", "ask", "--json"))["items"], [])
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1"}):
            self.post()
        first = json.loads(run_wb("upstream", "ask", "--json"))["items"]
        self.assertEqual(len(first), 1)
        self.assertIn("The push fails", first[0]["line"])
        self.assertEqual(json.loads(run_wb("upstream", "ask", "--json"))["items"], [])  # asked today
        upstream.path().write_text(json.dumps({"mode": "auto"}))
        store.write_json(store.home() / "upstream-asked.json", {"day": "2000-01-01"})
        self.assertEqual(json.loads(run_wb("upstream", "ask", "--json"))["items"], [])  # only in daily

    def test_settings_shows_the_mode_and_the_outbox_and_its_commands_read_back(self):
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1"}):
            self.post()
        rows = {r["key"]: r for r in settings.rows(fresh=True)}
        self.assertEqual(rows["upstream"]["value"], "daily")
        self.assertEqual([c["value"] for c in rows["upstream"]["choices"]], ["auto", "daily", "never"])
        self.assertIn("The push fails", rows["outbox"]["text"])
        self.assertEqual(settings._argv(rows["upstream"], "never"), ["upstream", "mode", "never"])
        self.assertEqual(settings._argv(rows["outbox"], "send"), ["upstream", "send"])
        self.assertEqual(settings.key_for(["upstream", "mode", "never"]), ("upstream", "never"))
        self.assertEqual(settings.key_for(["upstream", "drop"]), ("outbox", "drop"))
        self.assertIn("upstream", settings.LOGGED)


if __name__ == "__main__":
    unittest.main()
