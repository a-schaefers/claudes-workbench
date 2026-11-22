"""Sharing the GPU: tasks run side by side when their models fit it together, else one at a time."""
import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # run as a script too

from workbench import cli, hardware, images, models, store, transcript
from tests.test_workbench import Base, IMAGE_PNG, TEST_CMD, run_wb, wait_phase

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
        # its base check comes first: on the nightly's macOS runner it outlasted a fixed 1.5 s
        # sleep, and `last` still said so (#950); the wait is the lease's, once it is in line
        deadline = time.time() + 30
        while not any(h["task"] == "t-small" and h.get("waiting") for h in models.holders().values()):
            self.assertLess(time.time(), deadline, store.Task("t-small").status)
            time.sleep(0.1)
        time.sleep(0.5)                               # and it stays queued behind t-big
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

    def test_a_start_is_let_in_beside_a_task_whose_runner_stepped_aside_for_its_tests(self):
        # #805: t-big's runner let the GPU go for its tests, yet wb start counted it a holder
        # (alive, not in gpu.json) and refused t-small with exit 4 the lease would have let in
        os.environ["WB_FAKE_GPU_GB"] = "24"           # the two can't share
        mark = Path(self.tmp.name) / "base-checked"  # the base check runs quickly, the round's slowly
        self.start("t-big", "ok", "--model", BIG, "--test", f"test -e {mark} && sleep 30; touch {mark}")
        deadline = time.time() + 30
        while store.Task("t-big").status.get("phase") != "testing" and time.time() < deadline:
            time.sleep(0.1)
        big = store.Task("t-big").status
        self.assertEqual(big["leased"], big["pid"])
        self.assertEqual(cli._gpu_holders(), {})
        os.environ["FAKE_SCENARIO"] = "ok"
        out = run_wb("start", "t-small", "--spec", str(self.spec), "--repo", str(self.repo),
                     "--model", SMALL, "--test", TEST_CMD)  # no --queue: refused before
        self.assertNotIn("the GPU is busy", out)
        wait_phase("t-small", {"review"})              # while t-big's tests still run
        self.assertEqual(store.Task("t-big").status["phase"], "testing")
        run_wb("stop", "t-big")

    def runs_in_order(self, *marks):
        """Each mark's first fake run, in the order the runs came (FAKE_RUNS)."""
        runs = [json.loads(line)["prompt"] for line in Path(os.environ["FAKE_RUNS"]).read_text().splitlines()]
        return sorted(marks, key=lambda m: next(i for i, p in enumerate(runs) if m in p))

    def test_queued_tasks_take_the_gpu_in_the_order_they_were_started(self):
        # #108 (2026-11-18): a blocking flock hands the lock to any waiter, so a backlog queued
        # behind a running task ran in no particular order
        os.environ["WB_FAKE_GPU_GB"] = "24"
        self.start("t-big", "hang", "--model", BIG)
        wait_running("t-big")
        names = ["t-3", "t-1", "t-2"]  # names sorting otherwise: the order is the start's
        for name in names:
            self.start(name, "ok", "--model", BIG, "--queue")
        run_wb("stop", "t-big")
        for name in names:
            wait_phase(name, {"review"})
        self.assertEqual(self.runs_in_order(*(f"delegate/{n}" for n in names)),
                         [f"delegate/{n}" for n in names])

    def test_a_task_started_while_wb_optimize_measures_runs_once_it_has_let_go(self):
        # #161 (2026-11-19): wb try right after wb setup's measuring run said it stopped the run
        # "to make room" and was refused anyway: the stop is a SIGTERM, and the run's lease entry
        # was still there when wb start read the holders
        os.environ["WB_FAKE_GPU_GB"] = "24"
        holder = subprocess.Popen([sys.executable, "-c", f"""
import os, signal, sys, time
sys.path.insert(0, {str(Path(__file__).resolve().parent.parent)!r})
from workbench import models, optimize, store
signal.signal(signal.SIGTERM, lambda *a: (time.sleep(1), sys.exit(0)))  # lets go a second late
with models.GpuLease(None, optimize.HOLDER, wait=False):
    store.write_json(optimize.path(), {{"pid": os.getpid()}})
    print("held", flush=True)
    time.sleep(60)
""", "-m", "workbench.optimize"], stdout=subprocess.PIPE, text=True)  # its cmdline names it
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        out = self.start("t-now", "ok", "--model", SMALL)            # no --queue: no exit 4
        self.assertIn("stopped wb optimize", out)
        self.assertIsNotNone(holder.poll())
        wait_phase("t-now", {"review"})

    def test_tasks_on_one_remote_server_take_turns_while_this_gpu_runs_its_own(self):
        # #116: tasks on a remote server all ran at once, and its Ollama swapped their models in
        # and out on every request; it has its own lease now, apart from this machine's GPU
        os.environ["WB_FAKE_GPU_GB"] = "24"
        cfg = json.loads(Path(os.environ["WB_OPENCODE_CONFIG"]).read_text())
        cfg["provider"]["ollama-pc"] = {"options": {"baseURL": "http://gpu-pc.invalid:11434/v1"},
                                        "models": {"coder": {"limit": {"context": 32768}}}}
        Path(os.environ["WB_OPENCODE_CONFIG"]).write_text(json.dumps(cfg))
        Path(os.environ["WB_MODELS"]).write_text(json.dumps({"ollama-pc": {"local": True},
                                                             BIG: {"weights_gb": 20}}))
        remote = models.discover()["ollama-pc/coder"]
        self.assertTrue(remote.remote)
        self.assertEqual(models.pool(remote), "gpu-gpu-pc-invalid-11434")
        self.start("t-r1", "hang", "--model", remote.id)
        wait_running("t-r1")
        self.assertIn("exit 4", self.refused("start", "t-r0", "--spec", str(self.spec), "--repo",
                                             str(self.repo), "--model", remote.id))   # it would wait
        self.start("t-r2", "ok", "--model", remote.id, "--queue")
        self.start("t-here", "ok", "--model", BIG)     # this machine's GPU is free: no wait
        wait_phase("t-here", {"review"})
        deadline = time.time() + 30
        while "waiting for the GPU" not in store.Task("t-r2").status.get("last", ""):
            self.assertLess(time.time(), deadline, store.Task("t-r2").status)
            time.sleep(0.1)
        held = store.read_json(store.home() / "gpu-gpu-pc-invalid-11434.json")
        self.assertEqual(sorted(h["task"] for h in held.values()), ["t-r1", "t-r2"])
        self.assertEqual(models.holders(), {})        # this machine's gpu.json never saw them
        run_wb("stop", "t-r1")
        wait_phase("t-r2", {"review"})
        self.assertTrue(store.Task("t-r2").status.get("remote_gpu"))    # its savings: unmeasured (#117)
        self.assertNotIn("remote_gpu", store.Task("t-here").status)

    def test_an_image_server_on_a_remote_box_takes_its_turn_with_that_boxs_tasks(self):
        # #750 (the 2026-11-21 audit, finding 97): an image url on a `wb remote` box took no lease,
        # so it generated while a task's model sat on that card, and one of them spilled or crashed
        cfg = json.loads(Path(os.environ["WB_OPENCODE_CONFIG"]).read_text())
        cfg["provider"]["ollama-pc"] = {"options": {"baseURL": "http://gpu-pc.invalid:11434/v1"},
                                        "models": {"coder": {"limit": {"context": 32768}}}}
        Path(os.environ["WB_OPENCODE_CONFIG"]).write_text(json.dumps(cfg))
        Path(os.environ["WB_MODELS"]).write_text(json.dumps({"ollama-pc": {"local": True}}))
        tmp = Path(self.tmp.name)
        (tmp / "images.json").write_text(json.dumps(
            {"box": {"url": "http://gpu-pc.invalid:30000/v1/images/generations"},
             "elsewhere": {"url": "http://other-pc.invalid:30000/v1/images/generations"}}))
        os.environ["WB_IMAGES"] = str(tmp / "images.json")
        self.start("t-r1", "hang", "--model", "ollama-pc/coder")
        wait_running("t-r1")
        png = lambda e, v: Path(v["out"]).write_bytes(IMAGE_PNG)
        with mock.patch.object(images, "_server", png), mock.patch.object(images, "ready", lambda e: ""):
            with self.assertRaisesRegex(RuntimeError, "t-r1"):           # its turn: refused, not beside it
                images.generate("a fox", tmp / "o", n=1, name="box", say=lambda m: None)
            self.assertEqual(len(images.generate("a fox", tmp / "o", n=1, name="elsewhere",
                                                 say=lambda m: None)), 1)  # a host no task uses
            run_wb("stop", "t-r1")
            said = []
            self.assertEqual(len(images.generate("a fox", tmp / "o", n=1, name="box", say=said.append)), 1)
        self.assertEqual(models.holders(), {})                           # this machine's GPU untouched
        self.assertEqual(models.holders(pool="gpu-gpu-pc-invalid-11434"), {})   # the turn let go of

    def test_an_engine_in_wsl_waits_for_a_task_on_ollama_for_windows_on_the_same_gpu(self):
        # #750 (the 2026-11-21 audit, finding 97): a task on Ollama for Windows took pool
        # gpu-<winhost> and a command engine in WSL pool gpu, so the engine ran beside the task's
        # model on the one card. `same_gpu` (or WSL2's gateway, models.windows_host) makes it one pool
        cfg = json.loads(Path(os.environ["WB_OPENCODE_CONFIG"]).read_text())
        cfg["provider"]["ollama-win"] = {"options": {"baseURL": "http://winhost.invalid:11434/v1"},
                                         "models": {"coder": {"limit": {"context": 32768}}}}
        Path(os.environ["WB_OPENCODE_CONFIG"]).write_text(json.dumps(cfg))
        Path(os.environ["WB_MODELS"]).write_text(json.dumps({"ollama-win": {"local": True, "same_gpu": True}}))
        win = models.discover()["ollama-win/coder"]
        self.assertTrue(win.remote)
        self.assertEqual(models.pool(win), "gpu")
        tmp = Path(self.tmp.name)
        (tmp / "images.json").write_text(json.dumps(
            {"wsl": {"command": "sd-cli -p {prompt} -o {out}"},
             "win": {"url": "http://winhost.invalid:30000/v1/images/generations"}}))
        os.environ["WB_IMAGES"] = str(tmp / "images.json")
        self.start("t-w1", "hang", "--model", win.id)
        wait_running("t-w1")
        self.assertEqual([h["task"] for h in models.holders().values()], ["t-w1"])   # this machine's
        png = lambda e, v, *rest: Path(v["out"]).write_bytes(IMAGE_PNG)  # noqa: E731
        with mock.patch.object(images, "_server", png), mock.patch.object(images, "_command", png), \
                mock.patch.object(images, "ready", lambda e: ""):
            for name in ("wsl", "win"):
                with self.assertRaisesRegex(RuntimeError, "t-w1"):        # its turn, not beside it
                    images.generate("a fox", tmp / "o", n=1, name=name, say=lambda m: None)
            run_wb("stop", "t-w1")
            self.assertEqual(len(images.generate("a fox", tmp / "o", n=1, name="wsl", say=lambda m: None)), 1)
        self.assertEqual(models.holders(), {})
        with mock.patch.object(models, "windows_host", lambda: "winhost.invalid"):  # found, not said
            Path(os.environ["WB_MODELS"]).write_text(json.dumps({"ollama-win": {"local": True}}))
            self.assertEqual(models.pool(models.discover()["ollama-win/coder"]), "gpu")
            Path(os.environ["WB_MODELS"]).write_text(json.dumps({"ollama-win": {"local": True, "same_gpu": False}}))
            self.assertEqual(models.pool(models.discover()["ollama-win/coder"]), "gpu-winhost-invalid-11434")

    def test_a_task_sent_back_goes_ahead_of_one_started_after_it(self):
        # its review waits on that round: behind a night's backlog, it would wait for all of it
        os.environ["WB_FAKE_GPU_GB"] = "24"
        self.start("t-a", "ok", "--model", BIG)
        wait_phase("t-a", {"review"})
        self.start("t-big", "hang", "--model", BIG)
        wait_running("t-big")
        self.start("t-new", "ok", "--model", BIG, "--queue")
        os.environ["FAKE_SCENARIO"] = "ok"
        run_wb("feedback", "t-a", "--text", "feedback-for-t-a")
        # both in line in gpu.json before the GPU comes free: status `last` read "waiting for the
        # GPU" from the runner's start, before its checkout and its lease, so under CI's load
        # t-big stopped while t-new was the only one in line, and it rightly went first (#950)
        deadline = time.time() + 30
        while {"t-a", "t-new"} - {h["task"] for h in models.holders().values() if h.get("waiting")}:
            self.assertLess(time.time(), deadline, (store.Task("t-a").status, models.holders()))
            time.sleep(0.1)
        run_wb("stop", "t-big")
        wait_phase("t-a", {"review"})
        wait_phase("t-new", {"review"})
        self.assertEqual(self.runs_in_order("delegate/t-new", "feedback-for-t-a"),
                         ["feedback-for-t-a", "delegate/t-new"])

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

    def test_without_proc_a_holders_start_comes_from_ps(self):
        # #236: on macOS (no /proc) a recycled pid of ours was trusted as the holder
        real = Path.read_text
        noproc = lambda self, *a, **kw: (_ for _ in ()).throw(OSError("no /proc")) \
            if str(self).startswith("/proc/") else real(self, *a, **kw)  # noqa: E731
        sleeper = subprocess.Popen(["sleep", "60"])
        self.addCleanup(sleeper.kill)
        with mock.patch.object(Path, "read_text", noproc):
            start = models._started(sleeper.pid)
            self.assertRegex(start or "", r"\d\d:\d\d:\d\d")   # ps's lstart
            self.assertEqual(models._started(sleeper.pid), start)
            self.assertTrue(models._alive(sleeper.pid, start))
            self.assertFalse(models._alive(sleeper.pid, "1"))  # recycled: started at another time

    def test_without_proc_a_start_reads_the_same_in_every_time_zone(self):
        # #848: ps's lstart is local time, so a Mac that changed zones, or a writer and a reader
        # under different TZ, read a live holder, listener or session as a recycled pid
        real = Path.read_text
        noproc = lambda self, *a, **kw: (_ for _ in ()).throw(OSError("no /proc")) \
            if str(self).startswith("/proc/") else real(self, *a, **kw)  # noqa: E731
        sleeper = subprocess.Popen(["sleep", "60"])
        self.addCleanup(sleeper.kill)
        tz = os.environ.get("TZ")
        self.addCleanup(lambda: os.environ.__setitem__("TZ", tz) if tz is not None
                        else os.environ.pop("TZ", None))
        with mock.patch.object(Path, "read_text", noproc):
            os.environ["TZ"] = "EST5"
            start, tag = models._started(sleeper.pid), models.start_tag(sleeper.pid)
            old = models._ps_start(sleeper.pid, utc=False)          # what a wb before #848 wrote
            self.assertNotEqual(old, start)
            os.environ["TZ"] = "JST-9"                              # the laptop flew to Tokyo
            self.assertEqual(models._started(sleeper.pid), start)
            self.assertTrue(models._alive(sleeper.pid, start))
            self.assertTrue(models.same_tag(sleeper.pid, tag))
            self.assertFalse(models._alive(sleeper.pid, "Thu Jan  1 00:00:00 2026"))
            os.environ["TZ"] = "EST5"                               # an old stamp, same zone
            self.assertTrue(models._alive(sleeper.pid, old))
            self.assertTrue(models.same_tag(sleeper.pid, models._tag(old)))
            self.assertFalse(models.same_tag(sleeper.pid, "0badf00d"))

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
        got, said = [], []  # not print: its line reached the suite's output (the 0.90 review)
        t = threading.Thread(target=lambda: got.append(
            models.GpuLease(None, "wb image", said.append).__enter__()))
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
        self.assertEqual(said, ["waiting for the GPU (held by t-big)"])
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
        engine.write_text("import sys\nopen(sys.argv[1], 'wb').write(" + repr(IMAGE_PNG) + ")\n")
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

    def test_wb_audio_refuses_while_a_task_holds_the_gpu_but_a_cpu_engine_runs_beside_it(self):
        # #395: an engine that says "gpu": false (a small speech model) takes no lease, so it
        # makes a voice line while a coding task runs; a GPU engine refuses like wb image
        os.environ["WB_FAKE_GPU_GB"] = "48"
        tmp = Path(self.tmp.name)
        engine = tmp / "engine.py"
        engine.write_text("import sys, wave\nw = wave.open(sys.argv[1], 'wb')\nw.setnchannels(1)\n"
                          "w.setsampwidth(2)\nw.setframerate(8000)\nw.writeframes(b'\\x00\\x20' * 800)\nw.close()\n")
        cmd = f"{sys.executable} {engine} {{out}}"
        (tmp / "audio.json").write_text(json.dumps({"gpu": {"command": cmd},
                                                    "cpu": {"command": cmd, "gpu": False}}))
        os.environ["WB_AUDIO"] = str(tmp / "audio.json")
        self.start("t-big", "hang", "--model", BIG)
        wait_running("t-big")
        said = self.refused("audio", "hello", "--n", "1", "--out", str(tmp / "o"))
        self.assertIn("task t-big", said)
        self.assertIn("wb audio needs it to itself", said)
        self.assertIn("1 clip(s)", run_wb("audio", "hello", "--n", "1", "--engine", "cpu",
                                          "--out", str(tmp / "o")))
        self.assertEqual([h["task"] for h in models.holders().values()], ["t-big"])
        run_wb("stop", "t-big")

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
                mock.patch.object(models, "_fits_beside", lambda m, keep, before=None, kept=(): False), \
                mock.patch.object(models, "_unload", lambda m: said.append(f"unload {m.id}")):
            with models.GpuLease(models.resolve(SMALL), "t-small", said.append) as lease:
                self.assertEqual(lease.load(), 2.0)
                held = store.read_json(store.home() / "gpu.json")
                self.assertEqual([h["task"] for h in held.values()], ["t-small"])  # alone now
        self.assertEqual(calls, [{BIG}, set()])
        self.assertIn(f"sharing the GPU with {BIG}", said[0])
        self.assertIn(f"ollama could not fit {SMALL} beside {BIG}", said[1])
        self.assertEqual(said[2], f"unload {SMALL}")

    def test_a_load_that_loaded_nothing_or_a_model_a_holder_shares_is_never_unloaded(self):
        # another holder's model unloaded idle (its task in a test run past Ollama's keep-alive)
        # read as an eviction: the task unloaded itself, and a shared model too (the 0.90 review)
        os.environ["WB_FAKE_GPU_GB"] = "48"
        sleeper = subprocess.Popen(["sleep", "60"])
        self.addCleanup(sleeper.kill)
        store.home().mkdir(parents=True, exist_ok=True)
        store.write_json(store.home() / "gpu.json", {str(sleeper.pid): {"model": SMALL, "task": "t-a"}})
        parallel = mock.patch.object(models.hardware, "ollama_env", lambda: {"OLLAMA_NUM_PARALLEL": "2"})
        parallel.start()
        self.addCleanup(parallel.stop)
        fits, said = [], []
        with mock.patch.object(models, "make_resident", lambda m, say=print, keep=(), warn=None: 0.0), \
                mock.patch.object(models, "_fits_beside", lambda *a: fits.append(a) or False), \
                mock.patch.object(models, "_unload", lambda m: said.append(f"unload {m.id}")):
            with models.GpuLease(models.resolve(SMALL), "t-b", said.append) as lease:
                self.assertEqual(lease.load(), 0.0)    # already resident: nothing checked
        self.assertEqual((fits, said), ([], [f"sharing the GPU with {SMALL}"]))
        with mock.patch.object(models, "make_resident", lambda m, say=print, keep=(), warn=None: 1.0), \
                mock.patch.object(models, "_fits_beside", lambda *a: False), \
                mock.patch.object(models, "_unload", lambda m: said.append(f"unload {m.id}")), \
                mock.patch.object(models.GpuLease, "_take_alone", lambda self, held: None):
            with models.GpuLease(models.resolve(SMALL), "t-b", said.append) as lease:
                lease.load()
        self.assertIn(f"ollama could not fit {SMALL} beside {SMALL}", said[-1])
        self.assertNotIn(f"unload {SMALL}", said)      # t-a is using it


    def test_a_task_stepping_aside_never_unloads_the_model_the_next_task_was_let_in_with(self):
        # #327 (the audit of 1.4, reproduced): A let go of the lease, B took the GPU and loaded
        # the same model, then A, deciding from the holders it saw before, sent keep_alive 0
        admitted = Path(self.tmp.name) / "b-admitted"
        b = textwrap.dedent(f"""
            import sys, time
            from pathlib import Path
            sys.path.insert(0, {str(Path(__file__).resolve().parent.parent)!r})
            from workbench import models
            with models.GpuLease(models.resolve({SMALL!r}), "t-b", lambda s: None):
                Path({str(admitted)!r}).write_text("in")
                time.sleep(0.5)
            """)
        said = []
        started = []
        with models.GpuLease(models.resolve(SMALL), "t-a", said.append) as lease:
            def let_b_in_if_it_can():            # where A used to pause after letting go
                started.append(subprocess.Popen([sys.executable, "-c", b]))
                self.addCleanup(started[0].kill)
                end = time.time() + 5
                while time.time() < end and not admitted.exists():
                    time.sleep(0.05)
                return set()
            with mock.patch.object(models, "_fake", lambda: False), \
                    mock.patch.object(models, "keep_loaded", let_b_in_if_it_can), \
                    mock.patch.object(models, "_unload",
                                      lambda m: said.append(("unload", admitted.exists()))):
                lease.step_aside(unload=True)
        self.assertEqual(started[0].wait(timeout=20), 0)
        self.assertIn(("unload", False), said)      # sent before t-b was let in, which loads again
        self.assertTrue(admitted.exists())

    def test_tests_that_use_the_gpu_hold_it_from_a_queued_task_until_they_end(self):
        # #428 (the audit of 1.8, reproduced): with --unload-for-tests the lease table was empty
        # while the tests ran, so a rival with --queue was let in and ran a whole model round
        # beside them: on real hardware both allocate VRAM at once, out of memory in one
        os.environ["WB_FAKE_GPU_GB"] = "48"          # the two models fit together: not why it waits
        flag = Path(self.tmp.name) / "tests-end"
        self.start("t-gpu", "ok", "--model", SMALL, "--no-base-check", "--unload-for-tests",
                   "--test", f"while [ ! -e {flag} ]; do sleep 0.1; done; grep -qx 42 answer.txt")
        deadline = time.time() + 30
        while store.Task("t-gpu").status.get("phase") != "testing" and time.time() < deadline:
            time.sleep(0.1)
        held = list(models.holders().values())
        self.assertEqual([(h["task"], h["model"], h.get("tests")) for h in held], [("t-gpu", None, True)])
        argv = ("start", "t-rival", "--spec", str(self.spec), "--repo", str(self.repo),
                "--model", BIG, "--test", TEST_CMD)
        os.environ["FAKE_SCENARIO"] = "ok"
        said = self.refused(*argv)
        self.assertTrue(said.startswith("exit 4: "), said)
        self.assertIn("task t-gpu", said)
        run_wb(*argv, "--queue")
        time.sleep(2)
        st = store.Task("t-rival").status
        self.assertEqual((st["phase"], st["last"]), ("queued", "waiting for the GPU (held by t-gpu)"))
        flag.write_text("")
        self.assertEqual(wait_phase("t-gpu", {"review", "stuck", "failed"})["phase"], "review")
        self.assertEqual(wait_phase("t-rival", {"review", "stuck", "failed"})["phase"], "review")

    def test_tests_that_use_the_gpu_wait_for_a_task_already_sharing_it_to_step_aside(self):
        # #943 (an outside review of 1.90.24): the tests' entry stopped newcomers (#428), but a
        # task admitted beside this one before its tests kept its model loaded and its rounds
        # running while the project's GPU tests allocated VRAM beside it
        os.environ["WB_FAKE_GPU_GB"] = "48"          # 25 + 11 GB: admitted together
        self.start("t-big", "hang", "--model", BIG)
        wait_running("t-big")
        self.start("t-gpu", "ok", "--model", SMALL, "--no-base-check", "--unload-for-tests",
                   "--test", f"touch ran-tests; {TEST_CMD}")
        # the runner says "waiting for the GPU: the tests use it alone" first, then the lease names
        # who holds it: read between the two on a slow CI machine (2026-11-21), so wait for the
        # second, which a test of the order of the two would also need
        deadline = time.time() + 30
        while "(held by" not in store.Task("t-gpu").status.get("last", ""):
            self.assertLess(time.time(), deadline, store.Task("t-gpu").status)
            time.sleep(0.1)
        self.assertEqual(store.Task("t-gpu").status["last"],
                         "waiting for the GPU (held by t-big): the tests use it alone")
        # and says it is waiting: left `running`, wb steer --now promised an interrupt within
        # seconds for a note that came only after the wait and the tests (#943's review)
        self.assertEqual(store.Task("t-gpu").status["phase"], "queued")
        time.sleep(1)
        ran = store.Task("t-gpu").worktree / "ran-tests"
        self.assertFalse(ran.exists())                                # not beside t-big's model
        held = models.holders()
        self.assertEqual(sorted((h["task"], h["model"], h.get("tests")) for h in held.values()),
                         [("t-big", BIG, None), ("t-gpu", None, True)])
        tests = {p: h for p, h in held.items() if h.get("tests")}
        self.assertFalse(models.admits(models.resolve(BIG), tests))  # t-big can't rejoin meanwhile
        run_wb("stop", "t-big")
        self.assertEqual(wait_phase("t-gpu", {"review", "stuck", "failed"})["phase"], "review")
        self.assertTrue(ran.exists())

    def test_two_tasks_whose_loads_misfit_both_get_the_gpu_once_the_holder_ends(self):
        # #366 (the audit of 1.6, reproduced): two leases let in beside a third, each finding at
        # its load that Ollama couldn't keep both, waited for the GPU to themselves. The second
        # kept its shared gpu.lock while it waited, so the first's LOCK_EX never succeeded, and
        # every later task queued behind the two
        os.environ["WB_FAKE_GPU_GB"] = "80"      # 25 + 11 + 11 GB guessed: all three fit
        tmp = Path(self.tmp.name)
        cfg = json.loads(Path(os.environ["WB_OPENCODE_CONFIG"]).read_text())
        cfg["provider"]["ollama"]["models"]["other:9b"] = {"limit": {"context": 32768}}
        Path(os.environ["WB_OPENCODE_CONFIG"]).write_text(json.dumps(cfg))
        Path(os.environ["WB_MODELS"]).write_text(json.dumps(
            {BIG: {"weights_gb": 20}, SMALL: {"weights_gb": 9}, "ollama/other:9b": {"weights_gb": 9}}))
        root = str(Path(__file__).resolve().parent.parent)
        active = textwrap.dedent(f"""
            import sys, time
            from pathlib import Path
            sys.path.insert(0, {root!r})
            from workbench import models
            with models.GpuLease(models.resolve({BIG!r}), "t-act", lambda s: None):
                Path({str(tmp / "act")!r}).write_text("in")
                # held until both loads have misfit beside it and queued: a fixed 3 s ran out
                # first on the nightly's macOS runner, whose interpreters start slowly, and the
                # second load, alone by then, rightly fit (#950)
                deadline = time.time() + 30
                while time.time() < deadline and not all(
                        Path({str(tmp)!r}, n + "-queued").exists() for n in ("t-w1", "t-w2")):
                    time.sleep(0.05)
            """)
        waiter = textwrap.dedent(f"""
            import sys, time
            from pathlib import Path
            sys.path.insert(0, {root!r})
            from workbench import models
            models.make_resident = lambda *a, **k: 1.0     # a load that took a second,
            models._fits_beside = lambda *a: False         # and didn't fit beside the holder
            _take_alone = models.GpuLease._take_alone
            def take_alone(self, held):                    # t-w1 queued before t-w2 loads
                Path({str(tmp)!r}, self.task + "-queued").write_text("")
                _take_alone(self, held)
            models.GpuLease._take_alone = take_alone
            with models.GpuLease(models.resolve(sys.argv[2]), sys.argv[1],
                                 lambda s: print(s, flush=True)) as lease:
                print("in", flush=True)
                Path({str(tmp)!r}, sys.argv[1]).write_text("in")
                while not all(Path({str(tmp)!r}, n).exists() for n in ("t-w1", "t-w2")):
                    time.sleep(0.05)                   # both let in before either loads
                while sys.argv[1] == "t-w2" and not Path({str(tmp)!r}, "t-w1-queued").exists():
                    time.sleep(0.05)
                lease.load()
                print("got", flush=True)
                time.sleep(0.3)
            """)
        procs = [subprocess.Popen([sys.executable, "-c", active])]
        self.addCleanup(lambda: [p.kill() for p in procs])
        deadline = time.time() + 10
        while not (tmp / "act").exists():
            self.assertLess(time.time(), deadline)
            time.sleep(0.05)
        for name, model in (("t-w1", SMALL), ("t-w2", "ollama/other:9b")):  # one model each
            procs.append(subprocess.Popen([sys.executable, "-c", waiter, name, model], stdout=subprocess.PIPE,
                                          stderr=subprocess.STDOUT, text=True))
            self.assertTrue(procs[-1].stdout.readline().startswith("sharing the GPU"))  # let in
            self.assertEqual(procs[-1].stdout.readline().strip(), "in")
        outs = [p.communicate(timeout=30)[0] for p in procs[1:]]
        for out in outs:
            self.assertIn("could not fit", out)
            self.assertIn("got", out)
        self.assertEqual(procs[0].wait(timeout=5), 0)


class Budget(unittest.TestCase):
    """The GPU budget: one runtime's GPUs, an iGPU's carve-out left out, Apple's real default."""

    def setUp(self):
        # the cards and the fake /proc these tests give are a Linux box's: on the nightly's macOS
        # runner budget_devices kept only unified GPUs (#711) and ps stood in for /proc (#804),
        # so every NVIDIA or AMD case read no budget at all (#950). Apple's case says macOS.
        linux = mock.patch.object(hardware, "system", lambda: "linux")
        linux.start()
        self.addCleanup(linux.stop)

    def budget(self, gpus, env=None, why=False):
        with mock.patch.dict(os.environ, {"WB_FAKE_MODELS": "", "WB_MODELS": "/nonexistent"}), \
                mock.patch.object(models.hardware, "gpus", lambda windows=True: gpus), \
                mock.patch.object(models.hardware, "ollama_env", lambda: env or {}):
            budget, devs, said = models.budget_devices()
            return (budget, devs, said) if why else (budget, devs)

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

    def test_with_one_ollama_per_gpu_each_server_is_budgeted_and_limited_by_its_own_settings(self):
        # #460 (the audit of 1.8, reproduced with a fake /proc): :11434 on GPU 0 and :11435 on
        # GPU 1; the lowest pid's settings stood for both, so the second was budgeted GPU 0 and
        # read the first one's OLLAMA_NUM_PARALLEL
        card = lambda i: {"name": f"RTX 3090 #{i}", "driver": "nvidia 560.94", "vram": 24 * 2 ** 30,  # noqa
                          "uuid": f"GPU-{i}{i}", "discrete": True}
        cards = [card(0), card(1)]
        with tempfile.TemporaryDirectory() as proc:
            def process(pid, environ):
                Path(proc, pid, "ns").mkdir(parents=True)
                Path(proc, pid, "comm").write_text("ollama\n")
                Path(proc, pid, "cmdline").write_bytes(b"ollama\0serve\0")
                Path(proc, pid, "environ").write_bytes(b"\0".join(environ) + b"\0")
                os.symlink("mnt:[1]", Path(proc, pid, "ns", "mnt"))
            Path(proc, "self", "ns").mkdir(parents=True)
            os.symlink("mnt:[1]", Path(proc, "self", "ns", "mnt"))
            process("100", [b"CUDA_VISIBLE_DEVICES=0", b"OLLAMA_NUM_PARALLEL=1"])  # the default port
            process("200", [b"CUDA_VISIBLE_DEVICES=1", b"OLLAMA_HOST=127.0.0.1:11435",
                            b"OLLAMA_NUM_PARALLEL=2", b"CUDA_DEVICE_ORDER=PCI_BUS_ID"])
            first, second = "http://127.0.0.1:11434", "http://127.0.0.1:11435"
            with mock.patch.dict(os.environ, {"WB_FAKE_MODELS": "", "WB_MODELS": "/nonexistent"}), \
                    mock.patch.object(hardware, "PROC", Path(proc)), \
                    mock.patch.object(hardware, "_run", lambda *a, **k: ""), \
                    mock.patch.object(hardware, "gpus", lambda windows=True: cards):
                self.assertEqual(hardware.ollama_env_at(second)["OLLAMA_NUM_PARALLEL"], "2")
                self.assertEqual(hardware.ollama_env_at(first)["OLLAMA_NUM_PARALLEL"], "1")
                self.assertEqual(models.budget_devices(second)[1], [cards[1]])
                self.assertEqual(models.budget_devices(first)[1], [cards[0]])
                m = lambda url: models.Model(id="o/a", provider="o", name="a", base_url=url + "/v1",  # noqa
                                             engine="ollama", label="o/a", extra={"weights_gb": 1})
                self.assertTrue(models.can_share(m(second), [m(second)], int(99e9)))   # two slots
                self.assertFalse(models.can_share(m(first), [m(first)], int(99e9)))    # one

    def test_an_ollama_with_no_ollama_host_is_the_default_port_server_whatever_its_pid(self):
        # the review of #460: a process with no OLLAMA_HOST matched no port, so when the
        # default-port server was the later one, the other's settings (the lowest pid's) stood
        with tempfile.TemporaryDirectory() as proc:
            def process(pid, environ):
                Path(proc, pid, "ns").mkdir(parents=True)
                Path(proc, pid, "comm").write_text("ollama\n")
                Path(proc, pid, "cmdline").write_bytes(b"ollama\0serve\0")
                Path(proc, pid, "environ").write_bytes(b"\0".join(environ) + b"\0")
                os.symlink("mnt:[1]", Path(proc, pid, "ns", "mnt"))
            Path(proc, "self", "ns").mkdir(parents=True)
            os.symlink("mnt:[1]", Path(proc, "self", "ns", "mnt"))
            process("100", [b"CUDA_VISIBLE_DEVICES=1", b"OLLAMA_HOST=127.0.0.1:11435",
                            b"OLLAMA_NUM_PARALLEL=2"])
            process("200", [b"CUDA_VISIBLE_DEVICES=0", b"OLLAMA_NUM_PARALLEL=1"])  # the default port
            with mock.patch.object(hardware, "PROC", Path(proc)), \
                    mock.patch.object(hardware, "_run", lambda *a, **k: ""):
                self.assertEqual(hardware.ollama_env_at("http://127.0.0.1:11434")["OLLAMA_NUM_PARALLEL"], "1")
                self.assertEqual(hardware.ollama_env_at("http://127.0.0.1:11434")["CUDA_VISIBLE_DEVICES"], "0")
                self.assertEqual(hardware.ollama_env_at("http://127.0.0.1:11435")["OLLAMA_NUM_PARALLEL"], "2")

    def test_an_apus_carve_out_never_outranks_or_adds_to_a_discrete_card(self):
        # the 0.90 review: a Radeon 780M set to 16 GiB beside an 8 GiB RTX became the budget, and
        # a 4 GiB one was added to an RX 7600S's 8 (amdgpu reports both as VRAM)
        rtx = {"name": "RTX 4060", "driver": "nvidia 560.94", "vram": 8 * 2 ** 30, "discrete": True}
        apu = lambda gib: {"name": "Radeon 780M", "driver": "amdgpu", "vram": gib * 2 ** 30}  # noqa
        rx = {"name": "RX 7600S", "driver": "amdgpu", "vram": 8 * 2 ** 30, "discrete": True}
        self.assertEqual(self.budget([apu(16), rtx]), (8 * 2 ** 30, [rtx]))
        self.assertEqual(self.budget([apu(4), rx]), (8 * 2 ** 30, [rx]))
        self.assertEqual(self.budget([apu(16)])[0], 16 * 2 ** 30)   # alone, its carve-out counts

    def test_apple_silicon_gets_macos_default_share_not_three_quarters(self):
        # 75% of a 16 GB Mac let a model's context spill to the CPU (the 2026-11-16 audit)
        mac = lambda ram, wired=None: {"name": "Apple M3", "driver": "metal", "unified": True,  # noqa
                                       "vram": ram * 2 ** 30, "wired_limit": wired}
        macos = mock.patch.object(hardware, "system", lambda: "macos")
        macos.start()
        self.addCleanup(macos.stop)
        self.assertEqual(self.budget([mac(16)])[0], 16 * 2 ** 30 * 2 // 3)
        self.assertEqual(self.budget([mac(36)])[0], 36 * 2 ** 30 * 2 // 3)
        self.assertEqual(self.budget([mac(64)])[0], 64 * 2 ** 30 * 3 // 4)
        self.assertEqual(self.budget([mac(16, 14 * 2 ** 30)])[0], 14 * 2 ** 30)  # wired_limit_mb wins

    def test_a_jetson_or_dgx_spark_keeps_back_what_the_system_uses_from_the_ram(self):
        # #524: off macOS a unified NVIDIA GPU's budget was all of MemTotal, so an 8 GB Orin
        # Nano with 2 GB in the OS's hands passed a 6.5 GB model as fitting
        def jetson(ram, free):
            return {"name": "Orin", "driver": "nvidia 540.4", "vram": ram, "ram_free": free,
                    "unified": True, "discrete": False,
                    "memory": "the RAM: nvidia-smi gives the GPU no memory of its own"}

        def budget(g, loaded=()):
            ps = {"models": [{"name": f"m{i}", "size": n} for i, n in enumerate(loaded)]}
            with mock.patch.object(models, "_get", lambda url, timeout=2.5: ps if url.endswith("/api/ps") else None):
                return self.budget([g], why=True)
        n, _, why = budget(jetson(7_950_000_000, 7_000_000_000))    # under the floor: the floor
        self.assertEqual(n, 7_950_000_000 - models.SYSTEM_FLOOR)
        self.assertIn("Orin (the RAM: nvidia-smi gives the GPU no memory of its own, less 2.1 GB "
                      "the system uses)", why)
        n, _, _ = budget(jetson(7_950_000_000, 3_950_000_000))      # 4 GB in use: all of it
        self.assertEqual(n, 3_950_000_000)
        n, _, _ = budget(jetson(7_950_000_000, 3_950_000_000), loaded=[1_000_000_000])
        self.assertEqual(n, 4_950_000_000)                           # a loaded model is the GPU's
        apu = {"name": "Radeon 8060S", "driver": "amdgpu", "vram": 96 * 2 ** 30, "unified": True,
               "memory": "0.5 GB carve-out + 96.0 GB GTT"}
        self.assertEqual(self.budget([apu])[0], 96 * 2 ** 30)       # GTT is the kernel's own limit
        # #712: a 128 GB Strix Halo with its GTT raised to 124 GiB kept nothing back for the OS
        gib = 2 ** 30
        raised = {"name": "Radeon 8060S", "driver": "amdgpu", "vram": gib // 2 + 124 * gib, "unified": True,
                  "carve": gib // 2, "ram": 127 * gib, "ram_free": 117 * gib,
                  "memory": "0.5 GB carve-out + 133.1 GB GTT"}
        n, _, why = budget(raised)                                   # 10 GiB in use: kept back
        self.assertEqual(n, gib // 2 + 117 * gib)
        self.assertIn("less 10.7 GB the system uses", why)
        n, _, _ = budget({**raised, "ram_free": 90 * gib}, loaded=[30 * gib])  # a loaded model is the GPU's
        self.assertEqual(n, gib // 2 + 127 * gib - 7 * gib)
        self.assertEqual(budget({**raised, "vram": gib // 2 + 60 * gib})[0], gib // 2 + 60 * gib)  # GTT lower

    def test_only_the_cards_ollama_may_see_count(self):
        # the 2026-11-19 audit: two 8 GiB cards with Ollama limited to one made a 16 GiB budget
        card = lambda uuid: {"name": "RTX 4060", "driver": "nvidia 560.94", "vram": 8 * 2 ** 30,  # noqa
                             "discrete": True, "uuid": uuid}
        a, b = card("GPU-8932f937-d72c"), card("GPU-1d2e3f40-aa11")
        self.assertEqual(self.budget([a, b])[0], 16 * 2 ** 30)
        budget, devs, why = self.budget([a, b], {"CUDA_VISIBLE_DEVICES": "0"}, why=True)
        self.assertEqual((budget, devs), (8 * 2 ** 30, [a]))
        self.assertEqual(why, "RTX 4060 (VRAM), as CUDA_VISIBLE_DEVICES=0 leaves it")
        self.assertEqual(self.budget([a, b], {"CUDA_VISIBLE_DEVICES": "GPU-1d2e"}), (8 * 2 ** 30, [b]))
        self.assertEqual(self.budget([a, b], {"CUDA_VISIBLE_DEVICES": "1,0"})[0], 16 * 2 ** 30)
        self.assertEqual(self.budget([a, b], {"CUDA_VISIBLE_DEVICES": "-1"}), (None, []))
        self.assertEqual(self.budget([a, b], {"CUDA_VISIBLE_DEVICES": "1,7,0"}), (8 * 2 ** 30, [b]))
        rx = {"name": "RX 7600", "driver": "amdgpu", "vram": 8 * 2 ** 30, "discrete": True}
        self.assertEqual(self.budget([dict(rx), rx], {"ROCR_VISIBLE_DEVICES": "1"}), (8 * 2 ** 30, [rx]))
        self.assertEqual(self.budget([a, b], {"ROCR_VISIBLE_DEVICES": "0"})[0], 16 * 2 ** 30)

    def test_a_cuda_number_counts_fastest_first_unless_ollama_orders_cards_by_pci_bus(self):
        # #381 (the audit of 1.6): nvidia-smi lists cards in PCI order and CUDA numbers them
        # fastest first by default, so with a 1060 in the lower slot beside a 3090,
        # CUDA_VISIBLE_DEVICES=0 was the 1060 to wb and the 3090 to Ollama
        old = {"name": "GTX 1060", "driver": "nvidia 560.94", "vram": 6 * 2 ** 30, "discrete": True,
               "uuid": "GPU-aaaa", "compute_cap": (6, 1)}
        new = {"name": "RTX 3090", "driver": "nvidia 560.94", "vram": 24 * 2 ** 30, "discrete": True,
               "uuid": "GPU-bbbb", "compute_cap": (8, 6)}
        budget, devs, why = self.budget([old, new], {"CUDA_VISIBLE_DEVICES": "0"}, why=True)
        self.assertEqual((budget, devs), (24 * 2 ** 30, [new]))
        self.assertIn("as CUDA_VISIBLE_DEVICES=0 (counted fastest first, CUDA's default order: a guess; "
                      "CUDA_DEVICE_ORDER=PCI_BUS_ID or the card's UUID makes it sure) leaves it", why)
        self.assertEqual(self.budget([old, new], {"CUDA_VISIBLE_DEVICES": "1"}), (6 * 2 ** 30, [old]))
        budget, devs, why = self.budget([old, new], {"CUDA_VISIBLE_DEVICES": "0",
                                                     "CUDA_DEVICE_ORDER": "PCI_BUS_ID"}, why=True)
        self.assertEqual((budget, devs, why), (6 * 2 ** 30, [old], "GTX 1060 (VRAM), as CUDA_VISIBLE_DEVICES=0 leaves it"))
        _, devs, why = self.budget([old, new], {"CUDA_VISIBLE_DEVICES": "GPU-aaaa"}, why=True)
        self.assertEqual((devs, "guess" in why), ([old], False))                # a UUID is sure
        unknown = [{k: v for k, v in g.items() if k != "compute_cap"} for g in (old, new)]
        self.assertEqual(self.budget(unknown, {"CUDA_VISIBLE_DEVICES": "0"})[0], 24 * 2 ** 30)  # memory
        smi = {("nvidia-smi", "--query-gpu=uuid,compute_cap"): "GPU-aaaa, 6.1\nGPU-bbbb, 8.6",
               ("nvidia-smi",): "GPU-aaaa, GTX 1060, 6144, 0, 560.94\nGPU-bbbb, RTX 3090, 24576, 0, 560.94"}
        with mock.patch.object(hardware, "_run", lambda *a: next((v for k, v in smi.items() if a[:len(k)] == k), "")), \
                mock.patch.object(hardware, "system", lambda: "linux"), \
                mock.patch.object(hardware, "DRM", Path("/nonexistent")):
            self.assertEqual([g.get("compute_cap") for g in hardware.gpus()], [(6, 1), (8, 6)])
        with mock.patch.object(hardware, "_run", lambda *a: "LoadState=loaded\nActiveState=active\n"
                               "Environment=CUDA_DEVICE_ORDER=PCI_BUS_ID" if a[0] == "systemctl" else ""), \
                mock.patch.object(hardware, "PROC", Path("/nonexistent")), \
                mock.patch.object(hardware, "system", lambda: "linux"):
            self.assertEqual(hardware.ollama_env()["CUDA_DEVICE_ORDER"], "PCI_BUS_ID")   # Ollama's own

    def test_masks_keep_unset_apart_from_empty_and_count_in_the_order_before_them(self):
        # #255 (the audit of 1.2): an empty CUDA_VISIBLE_DEVICES (no device, to CUDA) counted
        # every card; HIP's index was read in discovery order, not within ROCR's, so it picked
        # the 4 GB card for the 24 GB one; and a Vulkan Ollama's mask wasn't read
        nv = {"name": "RTX 4060", "driver": "nvidia 560.94", "vram": 8 * 2 ** 30, "discrete": True}
        self.assertEqual(self.budget([nv], {"CUDA_VISIBLE_DEVICES": ""}), (None, []))
        self.assertEqual(self.budget([nv], {})[0], 8 * 2 ** 30)
        small = {"name": "RX 6400", "driver": "amdgpu", "vram": 4 * 2 ** 30, "discrete": True}
        big = {"name": "RX 7900", "driver": "amdgpu", "vram": 24 * 2 ** 30, "discrete": True}
        self.assertEqual(self.budget([small, big], {"ROCR_VISIBLE_DEVICES": "1,0",
                                                    "HIP_VISIBLE_DEVICES": "0"}), (24 * 2 ** 30, [big]))
        self.assertEqual(self.budget([small, big], {"GGML_VK_VISIBLE_DEVICES": "1"}), (24 * 2 ** 30, [big]))
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(hardware, "_run", lambda *a: ""), \
                mock.patch.object(hardware, "PROC", Path("/nonexistent")), \
                mock.patch.object(hardware, "system", lambda: "linux"):
            self.assertNotIn("CUDA_VISIBLE_DEVICES", hardware.ollama_env())   # unset stays unset

    def test_models_json_gpu_gb_sets_the_budget_by_hand(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d, "models.json")
            path.write_text(json.dumps({"gpu_gb": 20}))
            with mock.patch.dict(os.environ, {"WB_MODELS": str(path)}), \
                    mock.patch.object(models.hardware, "gpus", lambda windows=True: []):
                self.assertEqual(models.budget_devices(), (20 * 10 ** 9, [], "models.json gpu_gb"))
                self.assertIn("GPU budget 20 GB, from models.json gpu_gb", cli.gpu_line())
                path.write_text(json.dumps({"gpu_gb": "lots"}))
                with self.assertRaisesRegex(RuntimeError, "gpu_gb is 'lots', not a number of GB"):
                    models.gpu_budget()

    def test_every_gb_wb_prints_is_the_decimal_gb_gpu_gb_takes_and_the_hint_names_it(self):
        # #523: scout printed a 16 GiB card as "16.0 GB VRAM" (binary) beside "GPU budget 17 GB"
        # (decimal), and a 12 GiB Arc's owner wrote {"gpu_gb": 12}, 7% short of the card
        from workbench import top
        self.assertEqual(hardware.size(16 * 2 ** 30), "17.2 GB")
        self.assertEqual(top._gb(16 * 2 ** 30), "17.2")
        with mock.patch.object(models, "budget_devices", lambda: (None, [], "")), \
                mock.patch.dict(os.environ, {"WB_FAKE_MODELS": ""}), \
                mock.patch.object(cli.hardware, "gpus", lambda windows=True: [
                    {"name": "Intel Arc B580", "driver": "xe", "vram": None}]):
            self.assertIn('{"gpu_gb": <the card\'s memory in decimal GB: 12.9 for a 12 GiB card>}',
                          cli.gpu_line())

    def test_doctor_and_scout_say_what_the_budget_counts(self):
        with mock.patch.object(models, "budget_devices", lambda: (8 * 2 ** 30, [], "RTX 4060 (VRAM)")):
            self.assertIn("GPU budget 9 GB, from RTX 4060 (VRAM)", cli.gpu_line())
        with mock.patch.object(models, "budget_devices", lambda: (None, [], "")), \
                mock.patch.dict(os.environ, {"WB_FAKE_MODELS": ""}), \
                mock.patch.object(cli.hardware, "gpus", lambda windows=True: [
                    {"name": "Arc", "driver": "xe", "vram": None}]):
            self.assertIn("GPU memory unknown here (xe): tasks take turns", cli.gpu_line())
            # #272 (the audit of 1.2): nothing said how; the line names the file and the key
            self.assertIn(f'set its memory in {models.overrides_path()}: {{"gpu_gb": ', cli.gpu_line())
        # #522: the example fits the card it names: a Radeon seen from WSL is no Intel Arc
        with mock.patch.object(models, "budget_devices", lambda: (None, [], "")), \
                mock.patch.dict(os.environ, {"WB_FAKE_MODELS": ""}), \
                mock.patch.object(cli.hardware, "gpus", lambda windows=True: [
                    {"name": "AMD Radeon RX 7800 XT", "driver": "windows (seen from WSL)", "vram": None}]):
            self.assertIn("if AMD Radeon RX 7800 XT has memory of its own (a Radeon card)", cli.gpu_line())
            self.assertNotIn("Intel Arc", cli.gpu_line())
        # and a card that publishes no size beside one that does: the budget leaves it out
        with mock.patch.object(models, "budget_devices", lambda: (2 * 2 ** 30, [], "Radeon 780M (VRAM)")), \
                mock.patch.dict(os.environ, {"WB_FAKE_MODELS": ""}), \
                mock.patch.object(cli.hardware, "gpus", lambda windows=True: [
                    {"name": "Radeon 780M", "driver": "amdgpu", "vram": 2 * 2 ** 30},
                    {"name": "Arc A770", "driver": "i915", "vram": None}]):
            self.assertIn("Arc A770 publishes no memory size: if models run on it, set its memory in",
                          cli.gpu_line())


class SharedLease(Base):
    """#393: gpu.lock and gpu.json were per account ($WB_HOME), so two accounts' tasks on one
    system Ollama never saw each other's holds and unloaded each other's models mid-round."""

    def setUp(self):
        super().setUp()
        os.environ["FAKE_RUNS"] = str(Path(self.tmp.name) / "runs.jsonl")
        Path(os.environ["WB_MODELS"]).write_text(json.dumps(
            {BIG: {"weights_gb": 20}, SMALL: {"weights_gb": 9}}))

    def other_account(self) -> Path:
        """A second state directory, as another account on this machine has."""
        other = Path(self.tmp.name) / "other-state"
        other.mkdir()
        (other / "tuning.json").write_text('{"self_review": 0}')
        return other

    def test_two_state_directories_on_one_lease_take_turns_on_the_gpu(self):
        os.environ["WB_GPU_DIR"] = str(Path(self.tmp.name) / "shared-gpu")
        os.environ["WB_FAKE_GPU_GB"] = "24"           # 36 GB wanted: too much at once
        self.start("t-big", "hang", "--model", BIG)
        wait_running("t-big")
        mine = os.environ["WB_HOME"]
        os.environ["WB_HOME"] = str(self.other_account())
        out = self.start("t-small", "ok", "--model", SMALL, "--test", TEST_CMD, "--queue")
        self.assertIn("t-big", out)                   # the other account's hold is seen
        time.sleep(1.5)
        self.assertEqual(store.Task("t-small").status["phase"], "queued")
        os.environ["WB_HOME"] = mine
        run_wb("stop", "t-big")
        os.environ["WB_HOME"] = str(Path(self.tmp.name) / "other-state")
        wait_phase("t-small", {"review"})            # its turn came when the other's let go
        self.assertFalse((Path(mine) / "gpu.json").exists())  # nothing in either state directory
        os.environ["WB_HOME"] = mine

    def test_the_lease_is_the_machines_shared_directory_when_this_account_may_write_it(self):
        shared = Path(self.tmp.name) / "var-lib-workbench-gpu"
        del os.environ["WB_GPU_DIR"]
        with mock.patch.object(models, "SHARED_GPU", shared):
            self.assertEqual(models.lease_dir(), store.home())   # no such directory: per account
            self.assertFalse(models.lease_shared())
            shared.mkdir()
            self.assertEqual(models.lease_dir(), shared)
            self.assertTrue(models.lease_shared())
            if os.getuid() != 0:                                  # root may write anything
                shared.chmod(0o555)
                try:
                    self.assertEqual(models.lease_dir(), store.home())
                finally:
                    shared.chmod(0o755)
        os.environ["WB_GPU_DIR"] = str(Path(self.tmp.name) / "elsewhere")
        self.assertEqual(models.lease_dir(), Path(self.tmp.name) / "elsewhere")

    def test_another_accounts_live_holder_is_alive_though_its_pid_cant_be_signalled(self):
        start = models._started(os.getpid())
        me, other = os.getuid(), os.getuid() + 1
        with mock.patch.object(models.os, "kill", side_effect=PermissionError):
            self.assertTrue(models._alive(os.getpid(), start, other))
            self.assertFalse(models._alive(os.getpid(), "1", other))   # a recycled pid: another start
            self.assertFalse(models._alive(os.getpid(), start, me))    # ours can't be refused
            self.assertFalse(models._alive(os.getpid(), start))        # an entry from before: as before
            self.assertFalse(models._alive(os.getpid(), None, other))
            # #806: /proc mounted hidepid hides its start time; EPERM still proves it is there
            with mock.patch.object(models, "_started", lambda pid: None):
                self.assertTrue(models._alive(os.getpid(), start, other))
                self.assertFalse(models._alive(os.getpid(), start, me))

    def test_lease_files_another_account_made_can_still_be_locked(self):
        shared = Path(self.tmp.name) / "shared-gpu"
        shared.mkdir()
        os.environ["WB_GPU_DIR"] = str(shared)
        for name in ("gpu.lock", "gpu.admit", "gpu.load"):
            (shared / name).touch()
            (shared / name).chmod(0o444)              # another account's, under its umask
        with models.GpuLease(None, "an-image", say=lambda *_: None):
            held = models.holders()
            self.assertEqual([h["task"] for h in held.values()], ["an-image"])
            self.assertEqual(next(iter(held.values()))["uid"], os.getuid())
        self.assertEqual(models.holders(), {})

    def test_lease_files_made_under_a_umask_077_are_still_the_groups_to_read(self):
        # #802: account A's umask 077 left gpu.lock, gpu.admit, gpu.load and gpu.json 0600, and
        # account B's holders(), wb start and GpuLease failed with PermissionError
        shared = Path(self.tmp.name) / "shared-gpu"
        shared.mkdir()
        os.environ["WB_GPU_DIR"] = str(shared)
        (shared / "gpu.load").touch()
        (shared / "gpu.load").chmod(0o600)           # left by an earlier run under that umask
        self.assertEqual([p.name for p in models.unshared_lease_files()], ["gpu.load"])
        old = os.umask(0o077)
        try:
            with models.GpuLease(None, "an-image", say=lambda *_: None):
                with models._lease_lock(shared / "gpu.load"):
                    pass
                modes = {p.name: stat.S_IMODE(p.stat().st_mode) for p in shared.glob("gpu*")}
        finally:
            os.umask(old)
        self.assertEqual(set(modes), {"gpu.lock", "gpu.admit", "gpu.load", "gpu.json"})
        for name, mode in modes.items():
            self.assertEqual(mode & 0o044, 0o044, f"{name} is {mode:o}")
        self.assertEqual(models.unshared_lease_files(), [])
        os.environ["WB_GPU_DIR"] = str(store.home())  # not shared: the umask's
        self.assertFalse(models.lease_shared())
        self.assertEqual(models.unshared_lease_files(), [])

    def test_the_lease_and_the_merge_lock_work_where_an_exclusive_flock_needs_a_writable_fd(self):
        # #892: NFSv4 turns an exclusive flock into a whole-file write lock and refuses it with
        # EBADF on an O_RDONLY fd: every task failed at the GPU lease, and wb accept --merge
        # ended committed but unmerged with a bare "Bad file descriptor"
        import errno
        import fcntl
        from workbench import gitops
        real = fcntl.flock

        def nfs4(fd, op):
            f = fd if isinstance(fd, int) else fd.fileno()
            if op & fcntl.LOCK_EX and fcntl.fcntl(f, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY:
                raise OSError(errno.EBADF, "Bad file descriptor")
            return real(fd, op)
        with mock.patch.object(fcntl, "flock", nfs4):
            with models.GpuLease(None, "an-image", say=lambda *_: None):
                self.assertEqual([h["task"] for h in models.holders().values()], ["an-image"])
            with gitops.merge_lock(self.repo):
                pass
        # a lock the file system refuses some other way is the accept's GitError, said plainly
        with mock.patch.object(fcntl, "flock", side_effect=OSError(errno.ENOLCK, "No locks available")), \
                self.assertRaisesRegex(gitops.GitError, "can't lock .* for the merge \\(No locks available\\)"):
            with gitops.merge_lock(self.repo):
                pass

    def test_a_refusal_names_another_accounts_task_as_theirs(self):
        held = {"1": {"task": "fix-it", "model": BIG, "uid": os.getuid() + 1, "user": "sam"}}
        self.assertIn(f"sam's task fix-it ({BIG})", models.busy(held, "wait"))


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
        with mock.patch.object(models.hardware, "ollama_env", lambda: {"OLLAMA_NUM_PARALLEL": "1"}):
            self.assertFalse(models.can_share(a, [a], int(99e9)))     # the same model would take turns

    def test_the_same_model_shares_only_as_many_times_as_ollama_has_slots(self):
        # the 2026-11-19 audit: OLLAMA_NUM_PARALLEL=2 admitted any number, and a third task's
        # requests queued inside Ollama while its silence budget ran
        # (the setting is the running Ollama's, not wb's environment: #379, the audit of 1.6)
        a, b = self.model("ollama/a", gb=1), self.model("ollama/b", gb=1)
        par = lambda n: mock.patch.object(models.hardware, "ollama_env", lambda: {"OLLAMA_NUM_PARALLEL": n})  # noqa
        with par("2"):
            self.assertTrue(models.can_share(a, [a], int(99e9)))
            self.assertFalse(models.can_share(a, [a, a], int(99e9)))
            self.assertTrue(models.can_share(a, [a, b], int(99e9)))   # b's slots are b's own
        with par("3"):
            self.assertTrue(models.can_share(a, [a, a], int(99e9)))

    def test_more_models_than_ollama_keeps_loaded_never_share(self):
        # OLLAMA_MAX_LOADED_MODELS=1 made two tasks' models swap on every request (the 2026-11-16 audit)
        a, b, c = self.model("ollama/a"), self.model("ollama/b"), self.model("ollama/c")
        for most, ok in (("1", False), ("2", True), ("0", True), ("", True)):
            with mock.patch.object(models.hardware, "ollama_env",
                                   lambda: {"OLLAMA_MAX_LOADED_MODELS": most}):
                self.assertEqual(models.can_share(b, [a], int(99e9)), ok, most)
        with mock.patch.object(models.hardware, "ollama_env", lambda: {"OLLAMA_MAX_LOADED_MODELS": "2"}):
            self.assertFalse(models.can_share(c, [a, b], int(99e9)))

    def test_ollamas_default_model_limit_is_three_per_gpu(self):
        # #466 (the audit of 1.8; Ollama's server/sched.go: defaultModelsPerGPU * len(gpus)):
        # counted as 3 in all, a fourth task on a two-card box was refused sharing
        # NVIDIA cards are a Linux box's: on macOS budget_devices keeps only unified GPUs (#711, #950)
        linux = mock.patch.object(hardware, "system", lambda: "linux")
        linux.start()
        self.addCleanup(linux.stop)
        a, b, c, d = (self.model(f"ollama/{x}", gb=1) for x in "abcd")
        card = {"name": "RTX 3090", "driver": "nvidia 560.94", "vram": 24 * 2 ** 30, "discrete": True}
        for cards, ok in (([card], False), ([card, dict(card)], True)):
            with mock.patch.dict(os.environ, {"WB_FAKE_MODELS": "", "WB_MODELS": "/nonexistent"}), \
                    mock.patch.object(models.hardware, "gpus", lambda windows=True: cards), \
                    mock.patch.object(models.hardware, "ollama_env", lambda: {}):
                self.assertEqual(models.max_loaded(), 3 * len(cards))
                self.assertEqual(models.can_share(d, [a, b, c], int(99e9)), ok, len(cards))

    def test_the_model_limit_is_three_per_gpu_when_gpu_gb_sets_the_budget_too(self):
        # #811: gpu_gb (a hand-set headroom) left budget_devices no devices, so a two-card box
        # was allowed 3 models in all, and a fourth task was refused sharing that fit
        # NVIDIA cards are a Linux box's: on macOS budget_devices keeps only unified GPUs (#711, #950)
        linux = mock.patch.object(hardware, "system", lambda: "linux")
        linux.start()
        self.addCleanup(linux.stop)
        a, b, c, d = (self.model(f"ollama/{x}", gb=1) for x in "abcd")
        card = {"name": "RTX 3090", "driver": "nvidia 560.94", "vram": 24 * 2 ** 30, "discrete": True}
        hand = Path(self.tmp.name) / "models.json"
        hand.write_text(json.dumps({"gpu_gb": 40}))
        with mock.patch.dict(os.environ, {"WB_FAKE_MODELS": "", "WB_MODELS": str(hand)}), \
                mock.patch.object(models.hardware, "gpus", lambda windows=True: [card, dict(card)]), \
                mock.patch.object(models.hardware, "ollama_env", lambda: {}):
            self.assertEqual(models.budget_devices(), (40_000_000_000, [], "models.json gpu_gb"))
            self.assertEqual(models.max_loaded(), 6)
            self.assertTrue(models.can_share(d, [a, b, c], models.gpu_budget()))
        with mock.patch.dict(os.environ, {"WB_FAKE_MODELS": "", "WB_MODELS": str(hand)}), \
                mock.patch.object(models.hardware, "gpus", lambda windows=True: []), \
                mock.patch.object(models.hardware, "ollama_env", lambda: {}):
            self.assertEqual(models.max_loaded(), 3)                  # no card it can read: as before



class ServedWindow(unittest.TestCase):
    """#248 (the audit of 1.2): only doctor, scout and wb model add compared limit.context with
    what the server serves. After an Ollama restart without OLLAMA_CONTEXT_LENGTH, every prompt
    was trimmed silently and OpenCode, thinking there was room, never compacted."""

    def model(self, engine="ollama", said=65536):
        return models.Model(id="p/coder", provider="p", name="coder", base_url="http://127.0.0.1:9/v1",
                            engine=engine, label="coder", config={"limit": {"context": said}})

    def setUp(self):
        fake = mock.patch.dict(os.environ, {"WB_FAKE_MODELS": ""})
        fake.start()
        self.addCleanup(fake.stop)

    def ps(self, ctx):
        return mock.patch.object(models, "_get", lambda url, **kw: {
            "models": [{"name": "coder:latest", "context_length": ctx}]} if url.endswith("/api/ps") else None)

    def test_a_short_ollama_window_is_named_with_its_fix(self):
        with self.ps(8192):
            why = models.short_window(self.model())
        self.assertIn("serves p/coder a 8K window, but OpenCode is told 64K", why)
        self.assertIn("OLLAMA_CONTEXT_LENGTH", why)
        with self.ps(65536):
            self.assertIsNone(models.short_window(self.model()))
        with self.ps(None):                                            # an Ollama that doesn't say
            self.assertIsNone(models.short_window(self.model()))

    def test_another_server_is_read_through_its_facts(self):
        with mock.patch.object(models, "server_facts", return_value={"context": 32768}):
            why = models.short_window(self.model("server"))
        self.assertIn("start the server with a bigger window, or wb model add p/coder --context 32768", why)

    def test_the_runner_fails_the_load_before_any_round(self):
        from workbench import runner
        lease = mock.Mock(load=mock.Mock(return_value=3.0))
        with self.ps(8192), self.assertRaises(RuntimeError) as e:
            runner._load(None, lease, self.model())
        self.assertIn("cut the prompt off", str(e.exception))
        with self.ps(65536):
            self.assertEqual(runner._load(None, lease, self.model()), 3.0)


class LmStudioJustInTime(unittest.TestCase):
    """#335 (the audit of 1.4): LM Studio's just-in-time loading lists every downloaded model, so
    status() said running and nothing was loaded; OpenCode's first request loaded it with LM
    Studio's default window (often 4K), which refused the prompt, and round 1 failed."""

    def model(self):
        return models.Model(id="lms/coder", provider="lms", name="coder", base_url="http://127.0.0.1:9/v1",
                            engine="server", label="coder", config={"limit": {"context": 65536}},
                            stop="true")

    def setUp(self):
        fake = mock.patch.dict(os.environ, {"WB_FAKE_MODELS": ""})
        fake.start()
        self.addCleanup(fake.stop)
        models._FACTS.clear()

    def serve(self, loaded):
        rows = {"models": [{"key": "coder", "loaded_instances": loaded}]}
        return [mock.patch.object(models, "status", return_value={"available": True, "resident": True, "why": "running"}),
                mock.patch.object(models, "_ask", lambda url, *a, **kw: rows if url.endswith("/api/v1/models") else None),
                mock.patch.object(models, "free_gpu"), mock.patch.object(models, "_others_resident", return_value=False)]

    def test_a_listed_model_not_yet_loaded_is_loaded_with_opencodes_window(self):
        posts = []
        with contextlib.ExitStack() as st:
            for p in self.serve([]):
                st.enter_context(p)
            st.enter_context(mock.patch.object(models, "_post", lambda url, body, **kw: posts.append((url, body))))
            models.make_resident(self.model(), say=lambda *_: None)
        self.assertEqual(posts, [("http://127.0.0.1:9/api/v1/models/load",
                                  {"model": "coder", "context_length": 65536})])

    def test_a_loaded_one_is_left_alone_and_a_failed_load_names_lms_load(self):
        with contextlib.ExitStack() as st:
            for p in self.serve([{"id": "coder", "config": {"context_length": 65536}}]):
                st.enter_context(p)
            post = st.enter_context(mock.patch.object(models, "_post"))
            self.assertEqual(models.make_resident(self.model(), say=lambda *_: None), 0.0)
        post.assert_not_called()
        with contextlib.ExitStack() as st:
            for p in self.serve([]):
                st.enter_context(p)
            st.enter_context(mock.patch.object(models, "_post", side_effect=RuntimeError("HTTP 500")))
            with self.assertRaises(RuntimeError) as e:
                models.make_resident(self.model(), say=lambda *_: None)
        self.assertIn("lms load coder --context-length 65536", str(e.exception))


class KeepWarm(unittest.TestCase):
    """#864 (the third 2026-11-21 audit): a round waiting on a long background job or on Claude's
    answer let Ollama's 5-minute keep-alive lapse, and the model unloaded mid-round."""

    def model(self, **kw):
        return models.Model(id="ollama/coder", provider="ollama", name="coder",
                            base_url="http://127.0.0.1:9/v1", engine="ollama", label="coder",
                            config={"limit": {"context": 32768}}, **kw)

    def setUp(self):
        fake = mock.patch.dict(os.environ, {"WB_FAKE_MODELS": ""})
        fake.start()
        self.addCleanup(fake.stop)

    def test_a_loaded_ollama_model_gets_its_keep_alive_again_and_one_gone_is_never_loaded(self):
        posts = []
        post = lambda url, body, **kw: posts.append((url, body))  # noqa: E731
        with mock.patch.object(models, "_ollama_loaded", return_value=["coder:latest"]), \
                mock.patch.object(models, "_post", post):
            self.assertTrue(models.keep_warm(self.model()))
        self.assertEqual(posts, [("http://127.0.0.1:9/api/generate",
                                  {"model": "coder", "prompt": "", "keep_alive": "30m"})])
        posts.clear()
        with mock.patch.object(models, "_ollama_loaded", return_value=[]), \
                mock.patch.object(models, "_post", post):
            self.assertFalse(models.keep_warm(self.model()))   # a load here would skip free_gpu
        with mock.patch.object(models, "_ollama_loaded", return_value=["coder"]), \
                mock.patch.object(models, "_post", post), \
                mock.patch.object(models, "same_gpu", return_value=False):
            self.assertFalse(models.keep_warm(self.model(remote=True)))  # another box's GPU
        self.assertEqual(posts, [])
        with mock.patch.object(models, "_ollama_loaded", return_value=["coder"]), \
                mock.patch.object(models, "_post", side_effect=RuntimeError("HTTP 500")):
            self.assertFalse(models.keep_warm(self.model()))


if __name__ == "__main__":
    unittest.main()
