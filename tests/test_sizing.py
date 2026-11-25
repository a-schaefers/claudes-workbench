"""How much one task holds: the window's guess, and the record of each closed step correcting it."""
import json
import os
import shutil
import signal
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # run as a script too

from unittest import mock

from workbench import models, router, runner, store, tuning
from tests import test_workbench as tw
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

    def test_one_outlier_step_that_went_well_no_longer_sets_the_slice_for_good(self):
        # #720: the biggest good step ever won over every cap: one dependency bump (+2,400 lines)
        # made a 128K model's slice ~2,400 lines, and six later steps of 900 lines that all
        # compacted left it there
        self.steps({"files": 1, "lines": 2400, "window": 131072},
                   *({"files": 9, "lines": 900, "compacted": True, "window": 131072},) * 6)
        s = router.slice(self.model(131072))
        self.assertEqual((s.files, s.lines), (8, 600))       # one good step raises nothing
        self.assertIn("1 of 7 measured steps went well, 6 badly", s.line())
        # 40 small steps that went well, 30 a little bigger that went badly, then one rename and
        # lockfile at 28 files: it printed ~28 files, ~3000 lines
        (store.home() / "results.jsonl").unlink()
        big = {"window": 131072}
        mixed = [{"files": 3, "lines": 150, **big}, {"files": 5, "lines": 250, "compacted": True, **big}] * 30
        self.steps(*({"files": 3, "lines": 150, **big},) * 10, *mixed, {"files": 28, "lines": 3000, **big})
        s = router.slice(self.model(131072))
        self.assertEqual((s.files, s.lines), (4, 187))       # below the 5 / 250 that went badly
        self.assertIn("of the last 20 measured steps", s.line())
        # good steps past a bad one raise it when the steps that big mostly went well
        (store.home() / "results.jsonl").unlink()
        self.steps({"files": 6, "lines": 400, "stuck": True}, *({"files": 8, "lines": 500},) * 4)
        s = router.slice(self.model(32768))
        self.assertEqual((s.files, s.lines), (8, 500))

    def test_lockfiles_and_generated_files_are_counted_apart_from_a_steps_size(self):
        # #720: footprint counted every numstat line, so a lockfile's 2,400 lines were the step's
        import subprocess
        from workbench import gitops
        g = lambda *a: subprocess.run(["git", "-C", str(self.repo), *a], check=True, capture_output=True)  # noqa
        (self.repo / "old.py").write_text("x = 1\n" * 40)
        g("add", "."), g("commit", "-qm", "base")
        g("mv", "old.py", "new.py")                                  # a rename: its new name
        (self.repo / "app.py").write_text("print(1)\nprint(2)\n")
        (self.repo / "web").mkdir()
        (self.repo / "web" / "package-lock.json").write_text("{}\n" * 2400)
        (self.repo / "api_client.py").write_text("pass\n" * 300)
        (self.repo / ".gitattributes").write_text("api_client.py linguist-generated\nsnap.txt -diff\n")
        (self.repo / "snap.txt").write_text("frame\n" * 50)
        got = gitops.footprint(self.repo, "HEAD")
        self.assertEqual(got, {"files": 3, "lines": 4, "removed": 0,  # app.py, new.py, .gitattributes
                               "generated": {"files": 3, "lines": 2700, "removed": 0}})  # -diff: no lines


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
        self.assertEqual(self.row()["landed_steps"], 1)        # kept on the branch (#722)
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
        self.assertGreaterEqual(self.row()["peak_share"], 0.7)  # fresh_at could act on it (#721)
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

    def test_a_compacted_step_leaves_the_clean_step_after_it_measured_clean(self):
        # 2026-11-21, the audit of #958: the fresh session wb next's step 2 began in replaced step
        # 1's compacted one, at step 2's first round, and step 2 was measured compacted for it
        os.environ["FAKE_COMPACTED"] = "1"
        self.start("t-m5", "ok", "--test", TEST_CMD)
        wait_phase("t-m5", {"review"})
        nxt = Path(self.tmp.name) / "next.md"
        nxt.write_text("Write two more files.\n")
        self.assertIn("to a fresh session", run_wb("next", "t-m5", "-m", "step one", "--spec", str(nxt)))
        wait_phase("t-m5", {"review"})
        del os.environ["FAKE_COMPACTED"]                     # step 2's own session never compacted
        task = store.Task("t-m5")
        self.assertTrue(any(e.get("kind") == "fresh" and e.get("compacted")
                            and e.get("round") == task.status["step_round"]
                            for e in store.read_jsonl(task.dir / "events.jsonl")[0]))
        for f in ("a.txt", "b.txt"):
            (task.worktree / f).write_text("line\n" * 40)
        run_wb("accept", "t-m5", "-m", "both")
        first, second = self.row()["steps"]
        self.assertTrue(first["compacted"])                  # counted once, on its own step
        self.assertEqual((second["files"], second["compacted"], second["handovers"]), (2, False, 0))
        self.assertIn("1 of 2 measured steps went well, 1 badly",
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

    def test_workbench_updated_under_a_step_isnt_held_against_its_size(self):
        # #660: runner._updated's stuck notice had no cause, so an update mid-step recorded the
        # step stuck and capped the model's slice below it, with no expiry
        from unittest import mock
        from workbench import results, runner
        self.start("t-upd", "ok", "--test", TEST_CMD, "--no-self-review")
        wait_phase("t-upd", {"review"})
        task = store.Task("t-upd")
        with mock.patch.dict(runner.CURRENT, code="the code before the update"):
            self.assertTrue(runner._updated(task))
        note = [r for r in store.notices() if r["task"] == "t-upd"][-1]
        self.assertEqual((note["kind"], note["cause"]), ("stuck", "updated"))
        self.assertFalse(results.step(task, True)["stuck"])
        old = {"kind": "stuck", "round": 1, "text": f"task t: {runner.UPDATED}: wb resume t …"}
        self.assertTrue(results._not_the_step(old))                # one written before the cause
        for cause, skip in (("guard", False), ("agent", False), ("a stop added later", True)):
            self.assertIs(results._not_the_step({"kind": "stuck", "cause": cause}), skip)


class ApiSizing(Base):
    """#1017: an API model's window and output capped once (models.api_limits), so the layer's
    pins, OpenCode's compaction, the slice, status `window`, the steps and the tuning keys agree;
    its budgets with no local prefill, from the account's speed-api.json; a 600 s header guard;
    fewer rounds Claude never sees (auto-fix 1, self-review off). With the fake OpenCode's catalog
    (#1011), the spike's DeepInfra model: a 1M window and 384K output."""
    catalog = tw.ApiHarness.catalog
    # the real-OpenCode helpers, borrowed (an ApiHarness subclass would run its tests again)
    real_env, oc_cwd, real_model, real_run, capture, real_list = (
        tw.ApiHarness.real_env, tw.ApiHarness.oc_cwd, tw.ApiHarness.real_model,
        tw.ApiHarness.real_run, tw.ApiHarness.capture, tw.ApiHarness.real_list)
    layer = staticmethod(tw.ApiHarness.layer)

    def setUp(self):
        super().setUp()
        self.catalog(tw.API_CATALOG)
        self.m = models.hosted()[tw.FLASH]

    def as_api(self, name):
        """A task wb start ran on the GPU, its task.json then naming the API model, as #1016's
        wb start --role will write it, and its capped entry recorded as drive records it."""
        task = store.Task(name)
        store.write_json(task.dir / "task.json", {**task.spec, "model": tw.FLASH,
                                                  "opencode_model": tw.FLASH, "via": "api"})
        task = store.Task(name)
        runner.pin_api(task, models.resolve(tw.FLASH))
        return store.Task(name)

    def fail_catalog(self):
        """hosted.json as a catalog rebuild that failed leaves it (CATALOG_TIMEOUT, OpenCode
        busy): _build_hosted keeps no models."""
        doc = store.read_json(models.hosted_path(), {})
        store.write_json(models.hosted_path(), {**doc, "error": "timed out", "providers": {}, "models": {}})

    def test_an_api_models_window_and_output_are_capped_once_for_every_reader(self):
        cfg = self.m.config
        # 256K, not 128K: at 128K every DeepSeek run compacted at 72% of it, 4-13 times a run;
        # at 256K, 0-1 (#1134's E19, 12 runs over 5 cells, 2026-11-24)
        self.assertEqual(cfg["limit"], {"context": 262144, "output": 32768})
        self.assertEqual(cfg["options"]["max_tokens"], 32768)
        self.assertEqual(self.m.extra["catalog_limit"], {"context": 1048576, "output": 384000})
        self.assertEqual(runner.compaction_buffer(cfg), 36864)      # 262144 + 32768 - 262144 + 4096
        self.assertAlmostEqual(runner.compaction_point(cfg), (262144 - 36864) / 262144)   # about 0.86
        self.assertEqual(runner.handover_warning(cfg), "")          # 384K out: "no handover fits"
        self.assertTrue(0.8 < runner.fresh_ceiling(cfg) < 0.85)
        self.assertIn("256K window", router.slice(self.m).line())   # not 1024K
        self.assertEqual(models.lookup(tw.FLASH, cached=True).config["limit"]["context"], 262144)
        # a smaller catalog keeps its own; an input limit past the capped window is capped too,
        # or usable_input would read the wrong side
        self.assertEqual(models.api_limits({"limit": {"context": 65536, "input": 900000, "output": 8000}}),
                         {"limit": {"context": 65536, "input": 65536, "output": 8000},
                          "options": {"max_tokens": 8000}})
        self.assertEqual(models.api_limits({"limit": {"context": 200000}})["limit"]["output"], 32768)
        self.assertEqual(models.api_limits({"limit": {}}), {"limit": {}})  # no window: can't run

    def test_a_small_catalog_window_gets_an_output_cap_that_leaves_room_for_a_handover(self):
        # #1073: output was capped at API_OUTPUT whatever the window, so a 32K/32K catalog entry
        # put OpenCode's compaction below zero (buffer 36864 for a 32768 window), fresh_at fell
        # back to a point never reached, and the note told the user to lower a catalog's limits
        lo = min(tuning.KNOBS["fresh_at"].values)
        for context, output in ((16384, 16384), (32768, 32768), (32768, None), (40960, 40960),
                                (65536, 65536), (131072, 131072)):
            with self.subTest(context=context, output=output):
                cfg = models.api_limits({"limit": {"context": context, **({"output": output} if output else {})}})
                self.assertGreater(runner.compaction_point(cfg), 0)
                self.assertGreaterEqual(runner.fresh_ceiling(cfg), lo)
                self.assertEqual(runner.handover_warning(cfg), "")
                self.assertEqual(cfg["limit"]["output"], cfg["options"]["max_tokens"])
        self.assertEqual(models.api_limits({"limit": {"context": 131072, "output": 131072}})["limit"]["output"],
                         models.API_OUTPUT)                         # a large window: the cap as before

    def test_a_small_catalog_input_limit_gets_a_note_that_names_it_not_the_output(self):
        # #1073: with the output bounded by api_limits, only a small catalog limit.input leaves no
        # room for a handover, and wb start still said "lower limit.output and options.max_tokens",
        # which changes nothing and isn't the user's to do for a catalog model
        self.catalog([{**tw.API_CATALOG[0], "limit": {"context": 1048576, "input": 16384, "output": 384000}}])
        m = models.hosted(fresh=True)[tw.FLASH]
        routed = {"via": "api", "role": "trivial", "provider": m.provider, "provider_name": m.provider,
                  "thinking": "off", "said": ""}
        with mock.patch.object(tw.cli, "pick_model", lambda *a, **k: (m, models.status(m), "the role", routed)), \
                mock.patch.object(tw.cli, "spawn_runner", lambda t: 0):
            out = run_wb("start", "t-input", "--spec", str(self.spec), "--repo", str(self.repo))
        self.assertIn(f"note: {tw.FLASH}: its catalog limits (limit.input 16384, limit.output 32768 of a 256K "
                      "window) put OpenCode's compaction at 5% of the window", out)
        self.assertIn("no handover fits, so long steps compact; a catalog's limits aren't wb's or yours to "
                      "change: a model with a larger input limit has room", out)
        self.assertNotIn("lower limit.output", out)
        gpu = {"limit": {"context": 32768, "output": 32768}}                   # a GPU entry: as before
        self.assertTrue(runner.handover_warning(gpu).endswith("; lower limit.output and options.max_tokens"))

    def test_an_api_tasks_layer_pins_the_capped_window_and_waits_600_s_for_headers(self):
        self.start("t-gpu", "ok")
        wait_phase("t-gpu", {"review"})
        gpu = json.loads(runner.write_config_layer(store.Task("t-gpu")).read_text())
        self.assertIs(gpu["provider"]["ollama"]["options"]["headerTimeout"], False)  # as today (#491)
        task = self.as_api("t-gpu")
        layer = json.loads(runner.write_config_layer(task).read_text())
        prov = layer["provider"]["deepinfra"]
        self.assertEqual(prov["models"], {"deepseek-ai/DeepSeek-V4.1-Flash-1017": {
            "limit": {"context": 262144, "output": 32768}, "options": {"max_tokens": 32768}}})
        self.assertEqual(prov["options"]["headerTimeout"], 600_000)
        self.assertEqual(layer["compaction"]["buffer"], 36864)
        content = json.loads(runner.rules_env(layer)["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(content["compaction"]["keep"], {"tokens": 26214})  # restated (#1185)
        self.assertEqual(content["provider"]["deepinfra"]["options"]["headerTimeout"], 600_000)
        self.assertEqual(content["provider"]["deepinfra"]["models"], prov["models"])
        self.assertEqual(runner.provider_timeouts(task)["provider"]["deepinfra"]["options"],
                         {"headerTimeout": 600_000, "chunkTimeout": 960_000})

    def test_a_step_an_api_round_records_is_counted_by_the_next_slice_and_tuning(self):
        window = (self.m.config.get("limit") or {}).get("context")   # what drive writes to status
        self.assertEqual(window, 262144)
        row = {"model": tw.FLASH, "kind": "api", "outcome": "accepted", "ran": True, "window": window,  # #1022's kind
               "steps": [{"files": 2, "lines": 90, "window": window, "landed": True,
                          "compacted": False, "stuck": False, "took_over": False}]}
        store.append_jsonl(store.home() / "results.jsonl", row)
        self.assertIn("256K window; 1 of 1 measured steps went well", router.slice(self.m).line())
        self.assertEqual(tuning._key(row), (tw.FLASH, 262144))

    def test_api_budgets_have_no_prefill_term_and_room_for_one_silent_reply(self):
        self.assertEqual(router.budgets(self.m), {"think": 420, "silence": 900, "round": 2400})
        def msgs(write, ttft, n=5, out=1000):   # n requests at `write` tok/s after `ttft` s
            t0 = 1_700_000_000_000                  # ms; a 0 reads as unset
            return [{"type": "assistant", "time": {"created": t0, "firstToken": t0 + ttft * 1000,
                                                   "completed": t0 + ttft * 1000 + out / write * 1000},
                     "tokens": {"input": 50000, "output": out}} for _ in range(n)]
        router.record_speed(tw.FLASH, msgs(50, 4))
        self.assertFalse((store.machine_dir() / "speed.json").exists())  # not this machine's
        row = json.loads((store.home() / "speed-api.json").read_text())[tw.FLASH]
        self.assertEqual((row["write"], row["ttft"]), ([50.0] * 5, [4.0] * 5))
        self.assertNotIn("read", row)                    # its time to first token is the network's
        self.assertEqual(router.api_speed(tw.FLASH), {"write": 50.0, "ttft": 4.0})
        self.assertEqual(router.speed(tw.FLASH), {"write": 50.0})
        # 13500 / 50 + 4 = 274 s, clamped up to 420; with a window / read term, ~700 s
        self.assertEqual(router.budgets(self.m), {"think": 420, "silence": 900, "round": 2400})
        router.record_read_bound(tw.FLASH, 12)
        self.assertFalse((store.machine_dir() / "speed.json").exists())
        self.assertEqual(router.thinking_stall(tw.FLASH, self.m.config), "")
        # slow: a 32K reply written in silence (reasoning prints nothing until its step ends) must
        # outlast the silence limit: 32768 / 20 + 4 + 60 s, past the scaled 900
        (store.home() / "speed-api.json").unlink()
        router.record_speed(tw.FLASH, msgs(20, 4))
        got = router.budgets(self.m)
        self.assertEqual(got["think"], 679)                          # 13500 / 20 + 4
        self.assertEqual(got["silence"], 1703)                       # ceil(1638.4 + 4) + 60
        self.assertGreaterEqual(got["round"], got["silence"])
        self.assertEqual(got["silence"], router.budgets(models.Model(
            id=tw.FLASH, provider="deepinfra", name="x", base_url="", engine="api", label="x",
            kind="api"))["silence"])                                 # no config: API_OUTPUT

    def test_an_api_task_auto_fixes_once_and_a_gpu_task_twice_unless_claude_says(self):
        out = self.start("t-gpu", "ok", "--test", TEST_CMD)
        self.assertIn("auto-fix 2", out)
        self.assertEqual(store.Task("t-gpu").spec["auto_fix"], 2)
        wait_phase("t-gpu", {"review"})
        state = {"available": True, "resident": True, "why": "API (DeepInfra)"}
        for name, extra, want in (("t-api", (), 1), ("t-api3", ("--auto-fix", "3"), 3)):
            routed = {"via": "api", "role": "significant", "provider": "deepinfra", "said": "asked for"}
            with mock.patch.object(tw.cli, "pick_model", lambda *a, **k: (self.m, state, "asked for", routed)), \
                    mock.patch.object(tw.cli, "spawn_runner", lambda t: None), \
                    mock.patch.object(tw.cli, "_gpu_wait", lambda *a: ({}, False)):
                out = run_wb("start", name, "--spec", str(self.spec), "--repo", str(self.repo),
                             "--test", TEST_CMD, *extra)
            self.assertEqual(store.Task(name).spec["auto_fix"], want)
            self.assertIn(f"auto-fix {want}", out)                   # Claude sees what it costs

    def test_an_api_tasks_self_review_is_off_whatever_the_record_unless_claude_asks(self):
        (store.home() / "tuning.json").unlink()                      # Base pins it off
        gpu = models.resolve("mimo")
        def tuned(name, m, **spec):
            task = store.Task(name)
            task.dir.mkdir(parents=True)
            store.write_json(task.dir / "task.json", {"name": name, "model": m.id, **spec})
            tuning.tune(task, m.id, m.config["limit"]["context"], {}, runner.fixed_knobs(task, m))
            return store.Task(name).status["tuned"]["self_review"]
        names = [f"t-sr{i}" for i in range(30)]               # some explore (tuning.EXPLORE)
        self.assertEqual({tuned(n, self.m) for n in names}, {0})
        self.assertIn(1, {tuned(f"g-{n}", gpu) for n in names})     # on by default on the GPU
        self.assertEqual(tuned("t-own", self.m, self_review=True), 1)  # --self-review: Claude's word
        self.assertEqual(tuned("t-own-gpu", gpu, self_review=False), 0)

    def test_an_api_round_that_never_got_its_first_token_records_no_read_bound(self):
        # a real task with its layer and round files, so only the API check keeps a bound out
        # (with task=None the check's removal crashed instead: the review of #1017)
        self.start("t-gpu", "ok")
        wait_phase("t-gpu", {"review"})
        task = self.as_api("t-gpu")
        runner.write_config_layer(task)
        with mock.patch.object(runner, "_down", lambda m: False), \
                mock.patch.object(runner, "_stopped", lambda m: False):
            self.assertIsNone(runner.read_bound(task, 1, {"waited": 600, "guard": "silence"}, self.m))
        self.assertFalse((store.machine_dir() / "speed.json").exists())

    def test_a_users_own_max_tokens_under_the_cap_is_kept_not_raised(self):
        # an off-machine provider or Ollama cloud entry from the user's own config: 8192 is their
        # word, and raising it to 32768 spent up to 4x the output they chose (the review of #1017)
        got = models.api_limits({"limit": {"context": 65536, "output": 32768}, "options": {"max_tokens": 8192}})
        self.assertEqual(got["limit"]["output"], 8192)
        self.assertEqual(got["options"]["max_tokens"], 8192)
        got = models.api_limits({"limit": {"context": 65536}, "options": {"max_tokens": 8192, "x": 1}})
        self.assertEqual((got["limit"]["output"], got["options"]), (8192, {"max_tokens": 8192, "x": 1}))
        got = models.api_limits({"limit": {"context": 131072}, "options": {"max_tokens": 100000}})
        self.assertEqual((got["limit"]["output"], got["options"]["max_tokens"]), (32768, 32768))
        self.assertEqual(runner.compaction_buffer(got), 32768 + runner.STEP_GROWTH)

    def test_a_catalog_read_failing_mid_task_keeps_an_api_tasks_pins_buffer_and_header_guard(self):
        # the caps were worked out again each round from hosted.json: a rebuild that failed kept no
        # models, and the next round compacted at 1M, asked 256K out, waited on headers with no
        # limit and wrote its speeds as this machine's (the review of #1017)
        self.start("t-gpu", "ok")
        wait_phase("t-gpu", {"review"})
        task = self.as_api("t-gpu")
        self.assertEqual(task.status["api_entry"], {"limit": {"context": 262144, "output": 32768},
                                                    "options": {"max_tokens": 32768}})
        before = json.loads(runner.write_config_layer(task).read_text())
        self.fail_catalog()
        self.assertFalse(models.is_api(tw.FLASH))
        with self.assertRaises(KeyError):   # what a later runner's drive meets: _load_failed
            models.resolve(tw.FLASH)
        layer = json.loads(runner.write_config_layer(task).read_text())
        self.assertEqual(layer["provider"]["deepinfra"], before["provider"]["deepinfra"])
        self.assertEqual(layer["provider"]["deepinfra"]["options"]["headerTimeout"], 600_000)
        self.assertEqual(layer["compaction"], {"background": False, "auto": True, "buffer": 36864,
                                               "keep": {"tokens": 26214}})  # a tenth of 256K (#1185)
        content = json.loads(runner.rules_env(layer)["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(content["provider"]["deepinfra"]["models"],
                         {"deepseek-ai/DeepSeek-V4.1-Flash-1017": task.status["api_entry"]})
        t0 = 1_700_000_000_000
        msgs = [{"type": "assistant", "time": {"created": t0, "firstToken": t0 + 4000, "completed": t0 + 24000},
                 "tokens": {"input": 50000, "output": 1000}}] * 3
        task.set_status(session="ses_1017")
        with mock.patch("workbench.transcript.export", lambda sid, task=None: {"messages": msgs}):
            runner.measure_speed(store.Task("t-gpu"), 0)
        self.assertEqual(router.api_speed(tw.FLASH)["write"], 50.0)
        self.assertNotIn(tw.FLASH, store.read_json(store.machine_dir() / "speed.json", {}) or {})
        # back on the GPU: drive's record cleared, the guard lifted as before (#491)
        runner.pin_api(task, models.resolve("mimo"))
        self.assertIsNone(runner.api_entry(store.Task("t-gpu")))
        self.assertIs(runner.provider_timeouts(store.Task("t-gpu"))["provider"]["deepinfra"]["options"]
                      ["headerTimeout"], False)

    def test_drive_records_an_api_tasks_caps_and_a_later_runner_refuses_a_catalog_that_lost_it(self):
        with mock.patch.object(tw.cli, "spawn_runner"):
            self.start("t-gpu", "ok", "--no-base-check")
        task = store.Task("t-gpu")
        store.write_json(task.dir / "task.json", {**task.spec, "model": tw.FLASH,
                                                  "opencode_model": tw.FLASH, "via": "api"})
        with mock.patch.object(runner, "checkout", return_value=False):  # stop right after resolve
            runner.drive(store.Task("t-gpu"))
        self.assertEqual(store.Task("t-gpu").status["api_entry"]["limit"], {"context": 262144, "output": 32768})
        self.fail_catalog()
        with mock.patch.object(runner, "checkout", side_effect=AssertionError("ran on")):
            runner.drive(store.Task("t-gpu"))
        st = store.Task("t-gpu").status
        self.assertEqual((st["phase"], st.get("session")), ("failed", None))  # no round unpinned
        # a catalog that can't be read is never said as the model gone (#1016's left_catalog)
        self.assertIn("can't be checked: OpenCode's catalog can't be read now", st["last"])

    def test_an_api_models_silence_stops_at_the_gpus_longest(self):
        # cap / write had no ceiling: at 2 tok/s, 16448 s, a hung request holding the round and a
        # provider slot 4.6 hours (the review of #1017)
        top = int(router.SILENCE_DEFAULT_S * router.THINK_MAX / router.THINK_DEFAULT_S)
        self.assertEqual(router.SILENCE_MAX, top)
        for write, want in ((20, (679, 1703, 3880)), (5, (1800, top, 10285)), (2, (1800, top, 10285))):
            store.write_json(store.home() / "speed-api.json", {tw.FLASH: {"write": [write] * 5, "ttft": [4] * 5}})
            got = router.budgets(self.m)
            self.assertEqual((got["think"], got["silence"], got["round"]), want, write)

    def test_an_api_window_is_capped_at_256k_its_catalogs_own_or_a_price_tier_never_under_128k(self):
        # #1134's E19 (DeepInfra's DeepSeek V4.1 Flash, 12 runs over 5 cells, 2026-11-24): at 128K
        # every run compacted 4-13 times, at 72% of the window; at 256K 0-1 times, at about the
        # same cost and with no new stop. A ceiling only: a smaller catalog window is its own, and
        # past a price tier every request costs the tier's rate, so it stops there, but never
        # under the 128K every API task had before
        from workbench import meter
        flat = [{"input": 0.2, "output": 0.6}]
        tiered = lambda size: flat + [{"tier": {"type": "context", "size": size}, "input": 1, "output": 3}]  # noqa: E731
        for context, cost, want in ((1048576, flat, 262144), (200000, flat, 200000),
                                    (1048576, tiered(200000), 200000), (131072, flat, 131072),
                                    (64000, flat, 64000), (1048576, tiered(100000), 131072),
                                    (1048576, None, 262144)):
            with self.subTest(context=context, cost=cost):
                m = models._api_model("deepinfra/m", {"name": "m", "limit": {"context": context, "output": 384000},
                                                      **({"cost": cost} if cost else {})}, {}, {})
                self.assertEqual(m.config["limit"]["context"], want)
                self.assertEqual(m.config["options"]["max_tokens"], min(32768, m.config["limit"]["output"]))
        haiku = models.hosted()["anthropic/claude-haiku-5-5"]
        self.assertEqual(meter.first_tier(haiku.config), 100000)
        self.assertEqual(haiku.config["limit"]["context"], 131072)    # its 5x tier: kept where it was

    def test_a_driven_api_task_runs_in_the_256k_window_in_its_layer_restatement_and_catalog(self):
        # #1134's E19, its plan's risk 1: the layer, OPENCODE_CONFIG_CONTENT (loaded after it, so
        # a 128K there would undo the layer's) and the task's catalog.json all say one window
        from workbench import meter
        store.write_json(Path(os.environ["WB_ROUTES"]), {"v": 1, "day_usd": 100, "roles": {}})
        os.environ["FAKE_AUTH_EXPORT"] = json.dumps([
            {"id": "cred_deepinfra", "integrationID": "deepinfra", "label": "main", "active": True,
             "value": {"type": "key", "key": tw._ApiTask.CANARY}}])
        self.addCleanup(meter._METERS.clear)
        with mock.patch.object(tw.cli, "spawn_runner"):
            self.start("t-wide", "ok", "--no-base-check")
        task = store.Task("t-wide")
        store.write_json(task.dir / "task.json", {**task.spec, "model": tw.FLASH,
                                                  "opencode_model": tw.FLASH, "via": "api"})
        seen = {}

        def rounds(t, m, say):    # what drive hands the rounds, while the keyhole serves
            seen["m"] = m
            seen["layer"] = json.loads(runner.write_config_layer(t).read_text())
            seen["env"] = runner.agent_env(t)
            path = runner.catalog_path(t)
            seen["catalog"] = json.loads(path.read_text()) if path.is_file() else None
        with mock.patch.object(runner, "_drive", rounds):
            runner.drive(store.Task("t-wide"))
        self.assertIn("m", seen, store.Task("t-wide").status.get("last"))
        entry = {"limit": {"context": 262144, "output": 32768}, "options": {"max_tokens": 32768}}
        self.assertEqual(store.Task("t-wide").status["api_entry"], entry)
        cfg = seen["m"].config      # what _drive sets as status `window`, the tuning key, fresh_at's ceiling
        self.assertEqual(runner.fresh_ceiling(cfg), round((262144 - 36864) / 262144 - 0.05, 6))
        name = tw.FLASH.split("/", 1)[1]
        pinned = lambda doc: (lambda e: {"limit": e["limit"], "options": {  # noqa: E731
            "max_tokens": e["options"]["max_tokens"]}})(doc["provider"]["deepinfra"]["models"][name])
        self.assertEqual(pinned(seen["layer"]), entry)               # beside the keyhole's base and token
        self.assertEqual(seen["layer"]["compaction"]["buffer"], 36864)
        content = json.loads(seen["env"]["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(pinned(content), entry)                     # loaded last: the window holds
        self.assertEqual(content["compaction"]["buffer"], 36864)
        self.assertEqual(seen["catalog"]["deepinfra"]["models"][name]["limit"]["context"], 262144)

    def test_a_deepseek_task_whose_off_variant_is_v2_restates_the_256k_window(self):
        # a body variant (DeepSeek's `thinking`) moves the provider to V2 (runner._as_v2): the
        # window must reach the restatement there too, or OpenCode reads 128K's again (#1134)
        self.catalog(tw.THINK_CATALOG)
        models.hosted(fresh=True)
        self.models = models
        task = tw.ApiThinking.start_api(self, "t-v2", "deepseek/deepseek-plain")
        self.assertEqual(task.spec["opencode_model"], "deepseek/deepseek-plain#wb-off")
        runner.pin_api(task, models.resolve("deepseek/deepseek-plain"))
        task = store.Task("t-v2")
        layer = json.loads(runner.write_config_layer(task).read_text())
        self.assertEqual(layer["providers"]["deepseek"]["models"]["deepseek-plain"]["limit"]["context"], 262144)
        content = json.loads(runner.agent_env(task)["OPENCODE_CONFIG_CONTENT"])
        self.assertNotIn("deepseek", content.get("provider") or {})
        mine = content["providers"]["deepseek"]["models"]["deepseek-plain"]
        self.assertEqual((mine["limit"]["context"], mine["settings"]), (262144, {"max_tokens": 32768}))

    @unittest.skipUnless(shutil.which("opencode"), "needs the real opencode")
    def test_real_opencode_compacts_an_api_model_at_the_capped_window_and_asks_32k_out(self):
        # the edge case the issue names: the layer's pins must merge into a catalog model under
        # V1 `provider`, or OpenCode compacts at 1M again and asks for 256K out (the spike). And
        # the window is 256K, so OpenCode compacts only past 86% of it, not at 72% of 128K
        # (#1134's E19: 4-13 compactions a run at 128K, 0-1 at 256K, 2026-11-24)
        entry = self.real_model("deepinfra")
        mid = f"deepinfra/{entry['id']}"
        api = models._api_model(mid, {**entry, "name": entry["id"]}, {}, {})
        window = api.config["limit"]["context"]
        self.assertEqual(window, min(entry["limit"]["context"], models.api_window(api.config)))
        with mock.patch.object(models, "resolve", lambda _: api):
            pins = runner.model_pins(mid)
        buffer = runner.compaction_buffer(api.config)
        at = runner.usable_input(api.config["limit"]) - buffer          # OpenCode's compaction point
        self.assertAlmostEqual(at / window, (window - 36864) / window)
        seen = {}
        for pinned, tokens in ((True, at + 5_000), (False, at + 5_000), ("under", at - 20_000)):
            usage = json.dumps({"prompt_tokens": tokens, "completion_tokens": 1, "total_tokens": tokens + 1})
            port, out, _ = self.capture(f"cap-{pinned}", CAPTURE_USAGE=usage)
            layer = self.layer(port, "deepinfra", compaction={"auto": True, "buffer": buffer})
            if pinned:
                layer["provider"]["deepinfra"].update(pins["provider"]["deepinfra"])
            code, rows = self.real_run(mid, layer)
            self.assertEqual(code, 0, rows)
            sid = next(r["sessionID"] for r in rows if r.get("sessionID"))
            p = subprocess.run(["opencode", "run", "--standalone", "-m", mid, "--format", "json",
                                "--session", sid, "--", "go on"], cwd=self.oc_cwd(),
                               env=self.real_env(layer), capture_output=True, text=True, timeout=90)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            lines = out.read_text().splitlines()
            bodies = [json.loads(x) for x in lines]
            seen[pinned] = ({b.get("max_tokens") for b in bodies if b.get("tools")},
                            any("<template>" in x and "## Objective" in x for x in lines))
        # pinned: 32K out at most (OpenCode asks less once the window has less room left), and a
        # session just past (window - 36864) compacts, and 20K under it (twice 128K's point) not
        self.assertEqual((max(seen[True][0]), seen[True][1]), (32768, True))
        self.assertFalse(seen["under"][1])
        # unpinned, the catalog's: min(384000, 256000) out, and no compaction under 1M
        self.assertEqual(seen[False], ({min(entry["limit"]["output"], 256000)}, False))


if __name__ == "__main__":
    unittest.main()
