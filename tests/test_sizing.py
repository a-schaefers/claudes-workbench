"""How much one task holds: the window's guess, and the record of each closed step correcting it."""
import json
import os
from pathlib import Path

from workbench import models, router, store
from tests.test_workbench import Base, TEST_CMD, run_wb, wait_phase


class Slice(Base):
    def model(self, window):
        return models.Model(id="ollama/m", provider="ollama", name="m", label="M", base_url="",
                            engine="ollama", config={"limit": {"context": window}} if window else {})

    def steps(self, *steps, model="ollama/m"):
        for s in steps:
            store.append_jsonl(store.home() / "results.jsonl", {"model": model, "steps": [
                {"window": 32768, "landed": True, "overflow": False, "stuck": False,
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

    def test_steps_that_fit_raise_it(self):
        self.steps({"files": 6, "lines": 400}, {"files": 5, "lines": 520})
        s = router.slice(self.model(32768))
        self.assertEqual((s.files, s.lines), (6, 520))       # a strong model earns bigger tasks
        self.assertIn("2 of 2 measured steps fit", s.line())

    def test_steps_that_overflow_lower_it(self):
        self.steps({"files": 2, "lines": 120, "overflow": True})
        s = router.slice(self.model(32768))
        self.assertEqual((s.files, s.lines), (1, 90))
        self.assertIn("1 overflowed or stuck", s.line())

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
        self.assertFalse(first["overflow"] or first["stuck"] or first["took_over"])
        self.assertEqual(first["window"], 32768)
        self.assertFalse(second["landed"])
        self.assertEqual(second["files"], 0)

    def test_a_full_session_marks_the_step_as_overflowing(self):
        os.environ["FAKE_FILL"] = "25000"                    # 76% of the 32K window
        self.start("t-m2", "ok", "--test", TEST_CMD)
        wait_phase("t-m2", {"review"})
        fb = Path(self.tmp.name) / "fb.md"
        fb.write_text("Again.\n")
        run_wb("feedback", "t-m2", "--message", str(fb))
        wait_phase("t-m2", {"review"})
        run_wb("discard", "t-m2", "--reason", "too big")
        (step,) = self.row()["steps"]
        self.assertTrue(step["overflow"])
        self.assertFalse(step["landed"])

    def test_asking_for_a_fresh_session_is_not_an_overflow(self):
        self.start("t-m3", "ok", "--test", TEST_CMD)
        wait_phase("t-m3", {"review"})
        fb = Path(self.tmp.name) / "fb.md"
        fb.write_text("Again.\n")
        run_wb("feedback", "t-m3", "--message", str(fb), "--fresh")
        wait_phase("t-m3", {"review"})
        run_wb("accept", "t-m3", "-m", "feat: answer")
        self.assertFalse(self.row()["steps"][0]["overflow"])
        self.assertIn("1 of 1 measured steps fit",
                      router.slice(models.resolve("ollama/mimo-v2.6:9b-q8_0")).line())
