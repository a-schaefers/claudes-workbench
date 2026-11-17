"""Sharing the GPU: tasks run side by side when their models fit it together, else one at a time."""
import json
import os
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # run as a script too

from workbench import models, store
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
        self.start("t-small", "ok", "--model", SMALL, "--test", TEST_CMD)

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
        self.hold_big_then_start_small()
        time.sleep(1.5)
        st = store.Task("t-small").status
        self.assertEqual((st["phase"], st["last"]), ("queued", "waiting for the GPU"))
        run_wb("stop", "t-big")
        wait_phase("t-small", {"review"})            # its turn came when the GPU was free

    def test_a_machine_that_cant_say_its_vram_never_shares(self):
        self.hold_big_then_start_small()              # no WB_FAKE_GPU_GB: the budget is unknown
        time.sleep(1.5)
        self.assertEqual(store.Task("t-small").status["phase"], "queued")
        run_wb("stop", "t-big")
        wait_phase("t-small", {"review"})

    def test_whatever_takes_the_whole_gpu_waits_for_and_holds_off_the_tasks(self):
        os.environ["WB_FAKE_GPU_GB"] = "48"
        with models.GpuLease(None, "wb image"):  # what wb image does
            self.start("t-small", "ok", "--model", SMALL, "--test", TEST_CMD)
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
        self.start("t-small", "ok", "--model", SMALL, "--test", TEST_CMD)
        time.sleep(1.5)
        self.assertEqual(store.Task("t-small").status["phase"], "queued")  # behind the image
        run_wb("stop", "t-big")
        t.join(30)
        self.assertEqual(len(got), 1)                 # the image's turn came first
        time.sleep(1)
        self.assertEqual(store.Task("t-small").status["phase"], "queued")
        got[0].__exit__(None, None, None)
        wait_phase("t-small", {"review"})


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


if __name__ == "__main__":
    unittest.main()
