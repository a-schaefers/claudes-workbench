"""How much one task holds: the window's guess, and the record of each closed step correcting it."""
import json
import os
import signal
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # run as a script too

from workbench import models, router, store
from tests.test_workbench import Base, TEST_CMD, backdate, run_wb, running, wait_phase


class Slice(Base):
    def model(self, window):
        return models.Model(id="ollama/m", provider="ollama", name="m", label="M", base_url="",
                            engine="ollama", config={"limit": {"context": window}} if window else {})

    def steps(self, *steps, model="ollama/m"):
        for s in steps:
            store.append_jsonl(store.home() / "results.jsonl", {"model": model, "steps": [
                {"window": 32768, "landed": True, "compacted": False, "stuck": False,
                 "took_over": False, **s}]})

    def test_the_guess_grows_with_the_window(self):
        self.assertEqual((router.slice(self.model(32768)).files, router.slice(self.model(32768)).lines),
                         (3, 150))                            # what was measured on 32K
        big = router.slice(self.model(131072))
        self.assertEqual((big.files, big.lines), (12, 600))   # a 128K window: four times as much
        self.assertIn("a guess from its 128K window", big.line())
        huge = router.slice(self.model(1 << 20))
        self.assertEqual(huge.files, 12)                      # past that, only fitting steps earn more
        self.assertEqual(router.slice(self.model(None)).files, 3)
        # it read "a guess from its window unknown" (the 0.90 review)
        self.assertIn("a guess, its window unknown; no steps measured yet",
                      router.slice(self.model(None)).line())

    def test_steps_that_fit_raise_it(self):
        self.steps({"files": 6, "lines": 400}, {"files": 5, "lines": 520})
        s = router.slice(self.model(32768))
        self.assertEqual((s.files, s.lines), (6, 520))       # a strong model earns bigger tasks
        self.assertIn("2 of 2 measured steps went well", s.line())

    def test_steps_that_went_badly_lower_it(self):
        for bad in ({"compacted": True}, {"read_full": True}, {"stuck": True}, {"reviews": 2}):
            with self.subTest(bad):
                (store.home() / "results.jsonl").unlink(missing_ok=True)
                self.steps({"files": 2, "lines": 120, **bad})
                s = router.slice(self.model(32768))
                self.assertEqual((s.files, s.lines), (1, 90))
                self.assertIn("1 badly", s.line())

    def test_a_handover_or_a_round_the_spec_cost_isnt_held_against_a_step(self):
        self.steps({"files": 4, "lines": 300, "handovers": 3},
                   {"files": 3, "lines": 330, "reviews": 2, "spec_rounds": 1},
                   {"files": 2, "lines": 140, "overflow": True, "compacted": None})  # recorded
        s = router.slice(self.model(32768))                    # when a handover set overflow
        self.assertEqual((s.files, s.lines), (4, 330))
        self.assertIn("3 of 3 measured steps went well, 0 badly", s.line())

    def test_other_models_and_windows_dont_count(self):
        self.steps({"files": 9, "lines": 900}, model="ollama/other")
        self.steps({"files": 9, "lines": 900, "window": 16384})  # before its window was raised
        self.assertEqual(router.slice(self.model(32768)).files, 3)


class Measured(Base):
    def setUp(self):
        super().setUp()
        os.environ["FAKE_RUNS"] = str(Path(self.tmp.name) / "runs.jsonl")

    def row(self):
        return store.read_jsonl(store.home() / "results.jsonl")[0][-1]

    def test_each_step_is_measured_when_it_closes(self):
        out = self.start("t-m1", "ok", "--test", TEST_CMD)
        self.assertIn("slice: ~", out)                        # Claude sees it as the task starts
        wait_phase("t-m1", {"review"})
        nxt = Path(self.tmp.name) / "next.md"
        nxt.write_text("Write it again.\n")
        run_wb("next", "t-m1", "-m", "step one", "--spec", str(nxt))
        wait_phase("t-m1", {"review"})
        run_wb("discard", "t-m1", "--reason", "enough")      # the fake has nothing more to write
        first, second = self.row()["steps"]
        self.assertEqual((first["files"], first["lines"]), (1, 1))  # answer.txt, one line
        self.assertTrue(first["landed"])
        self.assertFalse(first["compacted"] or first["stuck"] or first["took_over"])
        self.assertEqual((first["handovers"], first["reviews"]), (0, 0))
        self.assertEqual(first["window"], 32768)
        self.assertFalse(second["landed"])
        self.assertEqual(second["files"], 0)

    def test_a_full_session_hands_over_and_the_step_still_went_well(self):
        os.environ["FAKE_FILL"] = "25000"                    # 76% of the 32K window
        self.start("t-m2", "ok", "--test", TEST_CMD)
        wait_phase("t-m2", {"review"})
        fb = Path(self.tmp.name) / "fb.md"
        fb.write_text("Again.\n")
        run_wb("feedback", "t-m2", "--message", str(fb))
        wait_phase("t-m2", {"review"})
        run_wb("accept", "t-m2", "-m", "feat: answer")
        (step,) = self.row()["steps"]
        self.assertEqual((step["handovers"], step["reviews"], step["compacted"]), (1, 1, False))
        self.assertIn("1 of 1 measured steps went well",
                      router.slice(models.resolve("ollama/mimo-v2.6:9b-q8_0")).line())

    def test_a_compacted_session_and_a_multi_step_tasks_reviews(self):
        os.environ["FAKE_COMPACTED"] = "1"
        self.start("t-m4", "ok", "--test", TEST_CMD)
        wait_phase("t-m4", {"review"})
        fb = Path(self.tmp.name) / "fb.md"
        fb.write_text("Again.\n")
        run_wb("feedback", "t-m4", "--message", str(fb), "--spec-gap", "the spec never said again")
        wait_phase("t-m4", {"review"})
        run_wb("next", "t-m4", "-m", "step one", "--spec", str(fb))
        wait_phase("t-m4", {"review"})
        run_wb("feedback", "t-m4", "--message", str(fb))
        wait_phase("t-m4", {"review"})
        run_wb("accept", "t-m4", "-m", "both")               # the fake has nothing more to write
        row = self.row()
        self.assertEqual([(s["reviews"], s["spec_rounds"]) for s in row["steps"]], [(1, 1), (1, 0)])
        self.assertEqual((row["reviews"], row["spec_rounds"]), (2, 1))  # every step's, not the last's
        self.assertTrue(all(s["compacted"] for s in row["steps"]))
        self.assertIn("0 of 1 measured steps went well, 1 badly",  # the empty second step has no size
                      router.slice(models.resolve("ollama/mimo-v2.6:9b-q8_0")).line())

    def test_asking_for_a_fresh_session_is_not_an_overflow(self):
        self.start("t-m3", "ok", "--test", TEST_CMD)
        wait_phase("t-m3", {"review"})
        fb = Path(self.tmp.name) / "fb.md"
        fb.write_text("Again.\n")
        run_wb("feedback", "t-m3", "--message", str(fb), "--fresh")
        wait_phase("t-m3", {"review"})
        run_wb("accept", "t-m3", "-m", "feat: answer")
        self.assertEqual(self.row()["steps"][0]["handovers"], 0)
        self.assertIn("1 of 1 measured steps went well",
                      router.slice(models.resolve("ollama/mimo-v2.6:9b-q8_0")).line())


    def test_a_shutdown_or_a_dead_runner_mid_step_isnt_held_against_its_size(self):
        # #582, the audit of 1.90: a SIGTERM from a shutdown, or a runner killed out of memory and
        # reaped, recorded the step stuck, and the slice shrank though the step then landed
        mimo = models.resolve("ollama/mimo-v2.6:9b-q8_0")
        for name, how in (("t-down", signal.SIGTERM), ("t-oom", signal.SIGKILL)):
            with self.subTest(how=how):
                (store.home() / "results.jsonl").unlink(missing_ok=True)
                self.start(name, "hang", "--test", TEST_CMD, "--no-self-review")
                st = running(name)
                os.kill(st["pid"], how)
                if how == signal.SIGKILL:
                    backdate(name)
                    self.assertIn(f"[failed] {name}", run_wb("wait", name, "--timeout", "5"))
                else:
                    wait_phase(name, {"stuck"})
                cause = [r for r in store.notices() if r["task"] == name][-1]["cause"]
                self.assertEqual(cause, "signal" if how == signal.SIGTERM else "died")
                os.environ["FAKE_SCENARIO"] = "ok"
                run_wb("resume", name)
                wait_phase(name, {"review"})
                run_wb("accept", name, "-m", "feat: answer")
                (step,) = self.row()["steps"]
                self.assertEqual((step["landed"], step["stuck"]), (True, False))
                self.assertIn("1 of 1 measured steps went well, 0 badly", router.slice(mimo).line())

    def test_a_guard_still_counts_the_step_stuck_and_old_notices_are_read_by_their_text(self):
        from workbench import results
        self.start("t-guard", "loop", "--test", TEST_CMD)            # one call repeated: a guard
        wait_phase("t-guard", {"stuck"})
        self.assertEqual([r for r in store.notices() if r["task"] == "t-guard"][-1]["cause"], "guard")
        run_wb("discard", "t-guard")
        self.assertTrue(self.row()["steps"][0]["stuck"])
        for text, skip in (("task t: the runner was stopped by a signal (a shutdown…)", True),
                           ("task t: the runner died without a report (killed…)", True),
                           ("task t: stuck: one call repeated 4 times", False)):
            self.assertIs(results._not_the_step({"kind": "stuck", "text": text}), skip)


if __name__ == "__main__":
    unittest.main()
