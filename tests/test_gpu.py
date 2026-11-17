"""Sharing the GPU: tasks run side by side when their models fit it together, else one at a time."""
import io
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # run as a script too

from workbench import cli, images, models, store, transcript
from tests.test_workbench import Base, TEST_CMD, run_wb, wait_phase

BIG, SMALL = "ollama/qwen3.8-27b", "ollama/mimo-v2.6:9b-q8_0"


def wait_running(name, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if store.Task(name).status.get("phase") == "running":
            return
        time.sleep(0.1)
    raise AssertionError(f"{name} never ran: {store.Task(name).status}")


class Sharing(Base):
    def setUp(self):
        super().setUp()
        os.environ["FAKE_RUNS"] = str(Path(self.tmp.name) / "runs.jsonl")
        self.weights(20, 9)

    def weights(self, big, small):
        Path(os.environ["WB_MODELS"]).write_text(json.dumps(
            {BIG: {"weights_gb": big}, SMALL: {"weights_gb": small}}))

    def hold_big_then_start_small(self):
        self.start("t-big", "hang", "--model", BIG)
        wait_running("t-big")
        return self.start("t-small", "ok", "--model", SMALL, "--test", TEST_CMD, "--queue")

    def refused(self, *argv) -> str:
        err = io.StringIO()
        with self.assertRaises(SystemExit) as e, redirect_stderr(err):
            run_wb(*argv)
        self.assertNotEqual(e.exception.code, 0)
        return f"exit {e.exception.code}: {err.getvalue()}"

    def test_models_that_fit_together_run_side_by_side(self):
        os.environ["WB_FAKE_GPU_GB"] = "48"          # 25 + 11 GB with their context, of 43 usable
        self.hold_big_then_start_small()
        wait_phase("t-small", {"review"})             # done while the big one still runs
        self.assertEqual(store.Task("t-big").status["phase"], "running")
        holders = store.read_json(store.home() / "gpu.json")
        self.assertEqual([h["task"] for h in holders.values()], ["t-big"])  # t-small let go
        run_wb("stop", "t-big")

    def test_models_that_dont_fit_take_turns(self):
        os.environ["WB_FAKE_GPU_GB"] = "24"           # 36 GB wanted: too much at once
        out = self.hold_big_then_start_small()
        self.assertIn("note: the GPU is busy with t-big; this task waits as queued", out)
        time.sleep(1.5)
        st = store.Task("t-small").status
        self.assertEqual((st["phase"], st["last"]), ("queued", "waiting for the GPU (held by t-big)"))
        self.assertEqual(transcript.activity(store.Task("t-small"), {}),
                         "Waiting for the GPU (held by t-big)…")  # the pane names the holder
        run_wb("stop", "t-big")
        wait_phase("t-small", {"review"})            # its turn came when the GPU was free

    def test_a_start_that_cant_run_beside_the_gpus_holder_is_refused_before_any_worktree(self):
        # two projects, one GPU: the second session's task queued in silence behind the first
        # (the 2026-11-16 audit); now wb start says who holds the GPU, and --queue waits anyway
        os.environ["WB_FAKE_GPU_GB"] = "24"
        self.start("t-big", "hang", "--model", BIG)
        wait_running("t-big")
        os.environ["FAKE_SCENARIO"] = "ok"
        argv = ("start", "t-small", "--spec", str(self.spec), "--repo", str(self.repo),
                "--model", SMALL, "--test", TEST_CMD)
        said = self.refused(*argv)
        self.assertTrue(said.startswith("exit 4: "), said)
        for part in ("task t-big", str(self.repo), BIG, "--queue", "do this step yourself"):
            self.assertIn(part, said)
        self.assertFalse(store.Task("t-small").dir.exists())
        branches = subprocess.run(["git", "-C", str(self.repo), "branch", "--list", "delegate/t-small"],
                                  capture_output=True, text=True).stdout
        self.assertEqual(branches, "")
        run_wb(*argv, "--queue")
        self.assertIn("note: the GPU is busy with t-big", cli._gpu_note(store.Task("t-small")))
        run_wb("stop", "t-big")
        wait_phase("t-small", {"review"})

    def test_a_task_stopped_while_queued_leaves_no_waiting_entry_behind(self):
        # a stale waiting entry made every later task wait alone (the 2026-11-16 audit)
        os.environ["WB_FAKE_GPU_GB"] = "24"
        self.hold_big_then_start_small()
        deadline = time.time() + 10
        while not any(h.get("waiting") for h in models.holders().values()):
            self.assertLess(time.time(), deadline)
            time.sleep(0.05)
        run_wb("stop", "t-small")
        self.assertEqual([h["task"] for h in models.holders().values()], ["t-big"])
        self.assertEqual(len(store.read_json(store.home() / "gpu.json")), 1)
        run_wb("stop", "t-big")

    def test_a_stale_holder_under_a_recycled_pid_doesnt_stop_tasks_sharing(self):
        os.environ["WB_FAKE_GPU_GB"] = "48"
        sleeper = subprocess.Popen(["sleep", "60"])  # ours, alive, but not the holder it claims
        self.addCleanup(sleeper.kill)
        store.home().mkdir(parents=True, exist_ok=True)
        store.write_json(store.home() / "gpu.json", {
            "1": {"model": None, "task": "x", "waiting": True},               # another user's pid
            str(sleeper.pid): {"model": None, "task": "y", "start": "1", "waiting": True}})
        self.assertEqual(models.holders(), {})
        self.hold_big_then_start_small()
        wait_phase("t-small", {"review"})             # shared, not queued behind "waiting" ghosts
        self.assertEqual(store.Task("t-big").status["phase"], "running")
        run_wb("stop", "t-big")

    def test_a_long_load_never_holds_the_admission_lock(self):
        # a finishing task waited on gpu.admit for another task's load, so its runner looked
        # alive after its review notice and wb feedback refused it (the 2026-11-16 audit)
        os.environ["WB_FAKE_GPU_GB"] = "48"
        gate, loading = threading.Event(), threading.Event()

        def slow(m, say=print, keep=(), warn=None):
            loading.set()
            gate.wait(20)
            return 0.0
        self.addCleanup(gate.set)
        m = models.resolve(SMALL)
        with mock.patch.object(models, "make_resident", slow), models.GpuLease(m, "t-x") as lease:
            t = threading.Thread(target=lease.load)
            t.start()
            self.assertTrue(loading.wait(10))
            got = threading.Event()

            def admit():
                with store.locked(store.home() / "gpu.admit"):
                    got.set()
            threading.Thread(target=admit, daemon=True).start()
            self.assertTrue(got.wait(5), "gpu.admit was held through the load")
            gate.set()
            t.join(10)

    def test_a_machine_that_cant_say_its_vram_never_shares(self):
        self.hold_big_then_start_small()              # no WB_FAKE_GPU_GB: the budget is unknown
        time.sleep(1.5)
        self.assertEqual(store.Task("t-small").status["phase"], "queued")
        run_wb("stop", "t-big")
        wait_phase("t-small", {"review"})

    def test_whatever_takes_the_whole_gpu_waits_for_and_holds_off_the_tasks(self):
        os.environ["WB_FAKE_GPU_GB"] = "48"
        with models.GpuLease(None, "wb image"):  # what wb image --wait does
            self.start("t-small", "ok", "--model", SMALL, "--test", TEST_CMD, "--queue")
            time.sleep(1.5)
            self.assertEqual(store.Task("t-small").status["phase"], "queued")
        wait_phase("t-small", {"review"})
        self.assertEqual(store.read_json(store.home() / "gpu.json"), {})

    def test_a_waiting_image_holds_off_tasks_that_would_share(self):
        os.environ["WB_FAKE_GPU_GB"] = "48"           # small would fit beside big
        self.start("t-big", "hang", "--model", BIG)
        wait_running("t-big")
        got = []
        t = threading.Thread(target=lambda: got.append(models.GpuLease(None, "wb image").__enter__()))
        t.start()
        deadline = time.time() + 10
        while not any(h.get("waiting") for h in store.read_json(store.home() / "gpu.json", {}).values()):
            self.assertLess(time.time(), deadline)
            time.sleep(0.05)
        self.start("t-small", "ok", "--model", SMALL, "--test", TEST_CMD, "--queue")
        time.sleep(1.5)
        self.assertEqual(store.Task("t-small").status["phase"], "queued")  # behind the image
        run_wb("stop", "t-big")
        t.join(30)
        self.assertEqual(len(got), 1)                 # the image's turn came first
        time.sleep(1)
        self.assertEqual(store.Task("t-small").status["phase"], "queued")
        got[0].__exit__(None, None, None)
        wait_phase("t-small", {"review"})

    def test_wb_image_refuses_at_once_while_a_task_holds_the_gpu_unless_it_waits(self):
        # queued behind a task, it blocked in silence past the Bash tool's limit, and Claude
        # couldn't answer that task's question meanwhile (the 2026-11-16 audit)
        os.environ["WB_FAKE_GPU_GB"] = "48"
        tmp = Path(self.tmp.name)
        engine = tmp / "engine.py"
        engine.write_text("import sys\nopen(sys.argv[1], 'wb').write(b'PNG')\n")
        (tmp / "images.json").write_text(json.dumps(
            {"e": {"command": f"{sys.executable} {engine} {{out}}"}}))
        os.environ["WB_IMAGES"] = str(tmp / "images.json")
        self.start("t-big", "hang", "--model", BIG)
        wait_running("t-big")
        t0 = time.time()
        said = self.refused("image", "a fox", "--n", "1", "--out", str(tmp / "o"))
        self.assertLess(time.time() - t0, 5)
        self.assertIn("task t-big", said)
        self.assertIn("--wait", said)
        self.assertFalse((tmp / "o").exists() and any((tmp / "o").glob("*.png")))
        self.assertEqual([h["task"] for h in models.holders().values()], ["t-big"])  # nothing left
        done, said = [], []                           # what wb image --wait does
        t = threading.Thread(target=lambda: done.append(images.generate(
            "a fox", tmp / "o", n=1, say=said.append, wait=True)))
        t.start()
        time.sleep(1.5)
        self.assertEqual((done, said), ([], ["waiting for the GPU (held by t-big)"]))
        run_wb("stop", "t-big")
        t.join(30)
        self.assertEqual(len(done[0]), 1)

    def test_a_share_ollama_couldnt_keep_falls_back_to_the_gpu_alone(self):
        # need() guesses a model's context before it loads; when it's short, Ollama evicts or
        # splits a model and two tasks reload theirs on every request (the 2026-11-16 audit)
        os.environ["WB_FAKE_GPU_GB"] = "48"
        sleeper = subprocess.Popen(["sleep", "60"])  # another task's runner, holding BIG
        self.addCleanup(sleeper.kill)
        store.home().mkdir(parents=True, exist_ok=True)
        store.write_json(store.home() / "gpu.json", {str(sleeper.pid): {"model": BIG, "task": "t-big"}})
        calls, said = [], []
        loads = lambda m, say=print, keep=(), warn=None: calls.append(set(keep)) or 1.0  # noqa: E731
        with mock.patch.object(models, "make_resident", loads), \
                mock.patch.object(models, "_fits_beside", lambda m, keep, before=None: False), \
                mock.patch.object(models, "_unload", lambda m: said.append(f"unload {m.id}")):
            with models.GpuLease(models.resolve(SMALL), "t-small", said.append) as lease:
                self.assertEqual(lease.load(), 2.0)
                held = store.read_json(store.home() / "gpu.json")
                self.assertEqual([h["task"] for h in held.values()], ["t-small"])  # alone now
        self.assertEqual(calls, [{BIG}, set()])
        self.assertIn(f"sharing the GPU with {BIG}", said[0])
        self.assertIn(f"ollama could not fit {SMALL} beside {BIG}", said[1])
        self.assertEqual(said[2], f"unload {SMALL}")


class Budget(unittest.TestCase):
    """The GPU budget: one runtime's GPUs, an iGPU's carve-out left out, Apple's real default."""

    def budget(self, gpus):
        with mock.patch.dict(os.environ, {"WB_FAKE_MODELS": ""}), \
                mock.patch.object(models.hardware, "gpus", lambda windows=True: gpus):
            return models.budget_devices()

    def test_an_igpu_of_another_vendor_isnt_added_to_the_cards_vram(self):
        # an RTX 4060 laptop's Radeon 780M carve-out made 8 GB look like 12 (the 2026-11-16 audit)
        rtx = {"name": "RTX 4060", "driver": "nvidia 560.94", "vram": 8 * 2 ** 30}
        apu = {"name": "Radeon 780M", "driver": "amdgpu", "vram": 4 * 2 ** 30}
        self.assertEqual(self.budget([rtx, apu]), (8 * 2 ** 30, [rtx]))

    def test_several_cards_of_one_driver_add_up_and_a_tiny_carve_out_doesnt(self):
        a = {"name": "RTX 3090", "driver": "nvidia 560.94", "vram": 24 * 2 ** 30}
        tiny = {"name": "Radeon iGPU", "driver": "amdgpu", "vram": 512 * 2 ** 20}
        self.assertEqual(self.budget([a, dict(a), tiny])[0], 48 * 2 ** 30)
        self.assertEqual(self.budget([tiny]), (None, []))      # an APU alone: unknown, no sharing
        self.assertEqual(self.budget([{"name": "Arc", "driver": "xe", "vram": None}]), (None, []))

    def test_apple_silicon_gets_macos_default_share_not_three_quarters(self):
        # 75% of a 16 GB Mac let a model's context spill to the CPU (the 2026-11-16 audit)
        mac = lambda ram, wired=None: {"name": "Apple M3", "driver": "metal", "unified": True,  # noqa
                                       "vram": ram * 2 ** 30, "wired_limit": wired}
        self.assertEqual(self.budget([mac(16)])[0], 16 * 2 ** 30 * 2 // 3)
        self.assertEqual(self.budget([mac(36)])[0], 36 * 2 ** 30 * 2 // 3)
        self.assertEqual(self.budget([mac(64)])[0], 64 * 2 ** 30 * 3 // 4)
        self.assertEqual(self.budget([mac(16, 14 * 2 ** 30)])[0], 14 * 2 ** 30)  # wired_limit_mb wins

    def test_doctor_and_scout_say_what_the_budget_counts(self):
        rtx = {"name": "RTX 4060", "driver": "nvidia 560.94", "vram": 8 * 2 ** 30}
        with mock.patch.object(models, "budget_devices", lambda: (8 * 2 ** 30, [rtx])):
            self.assertIn("GPU budget 9 GB, from RTX 4060", cli.gpu_line())
        with mock.patch.object(models, "budget_devices", lambda: (None, [])), \
                mock.patch.dict(os.environ, {"WB_FAKE_MODELS": ""}), \
                mock.patch.object(cli.hardware, "gpus", lambda windows=True: [
                    {"name": "Arc", "driver": "xe", "vram": None}]):
            self.assertIn("GPU memory unknown here (xe): tasks take turns", cli.gpu_line())


class CanShare(Base):
    def model(self, mid, engine="ollama", gb=10):
        return models.Model(id=mid, provider=mid.split("/")[0], name=mid.split("/")[1],
                            base_url="", engine=engine, label=mid, extra={"weights_gb": gb})

    def test_the_rules(self):
        a, b = self.model("ollama/a"), self.model("ollama/b")
        self.assertTrue(models.can_share(b, [a], int(30e9)))      # 12.5 + 12.5 of 27 usable
        self.assertFalse(models.can_share(b, [a], int(25e9)))     # 25 of 22.5
        self.assertFalse(models.can_share(b, [a], None))          # VRAM unknown
        self.assertFalse(models.can_share(b, [], int(99e9)))      # nothing to share with
        self.assertFalse(models.can_share(self.model("srv/x", "server"), [a], int(99e9)))
        self.assertFalse(models.can_share(b, [self.model("ollama/u", gb=0)], int(99e9)))  # unknown size
        os.environ["OLLAMA_NUM_PARALLEL"] = "1"
        self.assertFalse(models.can_share(a, [a], int(99e9)))     # the same model would take turns

    def test_more_models_than_ollama_keeps_loaded_never_share(self):
        # OLLAMA_MAX_LOADED_MODELS=1 made two tasks' models swap on every request (the 2026-11-16 audit)
        a, b, c = self.model("ollama/a"), self.model("ollama/b"), self.model("ollama/c")
        for most, ok in (("1", False), ("2", True), ("0", True), ("", True)):
            with mock.patch.object(models.hardware, "ollama_env",
                                   lambda: {"OLLAMA_MAX_LOADED_MODELS": most}):
                self.assertEqual(models.can_share(b, [a], int(99e9)), ok, most)
        with mock.patch.object(models.hardware, "ollama_env", lambda: {"OLLAMA_MAX_LOADED_MODELS": "2"}):
            self.assertFalse(models.can_share(c, [a, b], int(99e9)))


if __name__ == "__main__":
    unittest.main()
