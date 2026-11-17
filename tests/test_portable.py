"""Machines other than the one workbench grew up on: other servers, ports, configs, platforms, speeds."""
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # run as a script too

from unittest import mock

from workbench import context, images, models, router, store, watch
from tests.test_workbench import Base, wait_phase


def serve(routes: dict) -> tuple[ThreadingHTTPServer, str]:
    """A model server on a free port answering GET path -> JSON body (404 for anything else)."""
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = routes.get(self.path)
            self.send_response(200 if body is not None else 404)
            self.end_headers()
            self.wfile.write(json.dumps(body if body is not None else {"error": "no"}).encode())

        def log_message(self, *a):
            pass
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def fake_ollama(loaded: list, tags: list, posts: list, fail: dict | None = None):
    """An Ollama on a free port: /api/tags lists ``tags``, /api/ps ``loaded`` (dicts with name,
    size, size_vram); POST /api/generate is recorded in ``posts`` and loads or unloads, unless
    ``fail`` (status, body) answers it."""
    class H(BaseHTTPRequestHandler):
        def reply(self, code, body):
            self.send_response(code)
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def do_GET(self):
            body = {"/api/version": {"version": "0.40.0"}, "/api/tags": {"models": [{"name": n} for n in tags]},
                    "/api/ps": {"models": loaded}}.get(self.path)
            self.reply(200 if body is not None else 404, body or {})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            posts.append(body)
            if fail:
                return self.reply(*fail)
            if body.get("keep_alive") == 0:
                loaded[:] = [x for x in loaded if x["name"] != body["model"]]
            else:
                loaded.append({"name": body["model"], "size": 10, "size_vram": 10})
            self.reply(200, {})

        def log_message(self, *a):
            pass
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


class Real(unittest.TestCase):
    """Real HTTP servers, no WB_FAKE_MODELS: discovery and status as they run on a user's machine."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env_before = dict(os.environ)
        tmp = Path(self.tmp.name)
        os.environ.pop("WB_FAKE_MODELS", None)
        os.environ.update(WB_HOME=str(tmp / "state"), WB_OPENCODE_CONFIG=str(tmp / "opencode.json"),
                          WB_MODELS=str(tmp / "models.json"))
        models._ENGINES.clear()
        self.servers = []

    def tearDown(self):
        for s in self.servers:
            s.shutdown()
            s.server_close()
        os.environ.clear()
        os.environ.update(self.env_before)
        self.tmp.cleanup()

    def server(self, routes):
        srv, url = serve(routes)
        self.servers.append(srv)
        return url

    def config(self, providers: dict, extra: dict | None = None):
        Path(os.environ["WB_OPENCODE_CONFIG"]).write_text(json.dumps({"provider": providers}))
        Path(os.environ["WB_MODELS"]).write_text(json.dumps(extra or {}))

    def test_ollama_is_known_by_its_api_on_any_port(self):
        ollama = self.server({"/api/version": {"version": "0.40.0"}, "/api/tags": {"models": []}})
        other = self.server({"/v1/models": {"data": [{"id": "coder"}]}})
        self.config({"o": {"options": {"baseURL": ollama + "/v1"}, "models": {"m": {}}},
                     "s": {"options": {"baseURL": other + "/v1"}, "models": {"coder": {}}}})
        found = models.discover()
        self.assertEqual(found["o/m"].engine, "ollama")
        self.assertEqual(found["s/coder"].engine, "server")
        self.assertEqual(models.status(found["o/m"])["why"], "m is not pulled")

    def test_a_down_server_on_ollamas_port_is_taken_for_ollama_and_the_override_wins(self):
        self.config({"o": {"options": {"baseURL": "http://127.0.0.1:11434/v1"}, "models": {"m": {}}},
                     "x": {"options": {"baseURL": "http://127.0.0.1:1/v1"}, "models": {"m": {}}}},
                    {"x": {"engine": "ollama"}})
        found = models.discover()
        self.assertEqual((found["o/m"].engine, found["x/m"].engine), ("ollama", "ollama"))

    def test_a_multi_model_server_swaps_its_own_models(self):
        lms = self.server({"/v1/models": {"data": [{"id": "a"}, {"id": "b"}]}})
        stuck = self.server({"/v1/models": {"data": [{"id": "z"}]}})   # running, no stop command
        self.config({"lms": {"options": {"baseURL": lms + "/v1"}, "models": {"a": {}, "b": {}, "c": {}}},
                     "one": {"options": {"baseURL": stuck + "/v1"}, "models": {"z": {}}}})
        found = models.discover()
        self.assertTrue(models.status(found["lms/a"])["available"])
        self.assertIn("not c", models.status(found["lms/c"])["why"])
        said = []
        models.free_gpu(keep={"lms/a"}, say=said.append)  # used to raise for lms/b, and for one/z
        self.assertEqual(len(said), 1)
        self.assertIn("one/z is running and wb can't stop it", said[0])

    def test_one_listed_model_answers_to_any_name(self):
        llama = self.server({"/v1/models": {"data": [{"id": "/models/coder-q4.gguf"}]}})
        self.config({"l": {"options": {"baseURL": llama + "/v1"}, "models": {"coder": {}}}})
        self.assertTrue(models.status(models.resolve("coder"))["available"])

    def test_a_port_two_started_models_share_says_which_one_it_serves(self):
        # both read "running", so a task for one ran on the other (the 2026-11-16 audit)
        llama = self.server({"/v1/models": {"data": [{"id": "/models/coder.gguf"}]}})
        self.config({"l": {"options": {"baseURL": llama + "/v1"}, "models": {"coder": {}, "general": {}}}},
                    {"l": {"engine": "server"}, "l/coder": {"start": "serve coder"},
                     "l/general": {"start": "serve general"}})
        self.assertTrue(models.status(models.resolve("l/coder"))["resident"])
        st = models.status(models.resolve("l/general"))
        self.assertFalse(st["available"] or st["resident"])
        self.assertIn("is serving /models/coder.gguf, not general", st["why"])

    def own(self, loaded, tags, fail=None):
        """Two providers reaching one fake Ollama, as localhost and as 127.0.0.1."""
        posts = []
        srv, port = fake_ollama(loaded, tags, posts, fail)
        self.servers.append(srv)
        self.config({"a": {"options": {"baseURL": f"http://localhost:{port}/v1"}, "models": {"x": {}}},
                     "b": {"options": {"baseURL": f"http://127.0.0.1:{port}/v1"}, "models": {"y": {}}}})
        return models.discover(), posts

    def test_one_ollama_under_two_host_spellings_is_one_server(self):
        # free_gpu unloaded the very models it was told to keep (the 2026-11-16 audit)
        found, posts = self.own([{"name": "x", "size": 10, "size_vram": 10},
                                 {"name": "y", "size": 10, "size_vram": 10}], ["x", "y"])
        self.assertEqual(found["a/x"].server, found["b/y"].server)
        models.free_gpu(keep={"a/x", "b/y"}, say=lambda m: None)
        self.assertEqual(posts, [])
        self.assertFalse(models._others_resident(found["a/x"], keep={"b/y"}))
        twin = models.Model(id="b/x", provider="b", name="x", base_url=found["b/y"].base_url,
                            engine="ollama", label="b/x", extra={"weights_gb": 1})
        with mock.patch.object(models.hardware, "ollama_env", lambda: {"OLLAMA_NUM_PARALLEL": "1"}):
            self.assertFalse(models.can_share(twin, [found["a/x"]], int(99e9)))  # the same model

    def test_a_failed_ollama_load_says_why_and_a_missing_model_unloads_nothing(self):
        found, posts = self.own([{"name": "y", "size": 10, "size_vram": 10}], ["x", "y"],
                                fail=(500, {"error": "model requires more system memory (24.1 GiB)"}))
        with self.assertRaisesRegex(RuntimeError, "HTTP 500: model requires more system memory"):
            models.make_resident(found["a/x"], say=lambda m: None)
        found, posts = self.own([{"name": "y", "size": 10, "size_vram": 10}], ["y"])
        with self.assertRaisesRegex(RuntimeError, "x is not pulled"):
            models.make_resident(found["a/x"], say=lambda m: None)
        self.assertEqual(posts, [])  # y, the user's own, stays loaded

    def test_a_model_partly_on_the_cpu_is_a_warning_and_a_share_that_didnt_hold_is_seen(self):
        found, posts = self.own([], ["x", "y"])
        warned = []
        models._placement(found["a/x"], warned.append)
        self.assertEqual(warned, [])
        found, posts = self.own([{"name": "x", "size": 100, "size_vram": 60}], ["x", "y"])
        models._placement(found["a/x"], warned.append)
        self.assertIn("x is 40% on the CPU", warned[0])
        self.assertFalse(models._fits_beside(found["a/x"], {"b/y"}))   # partly on the CPU
        found, posts = self.own([{"name": "x", "size": 10, "size_vram": 10}], ["x", "y"])
        self.assertFalse(models._fits_beside(found["a/x"], {"b/y"}))   # y was evicted
        found, posts = self.own([{"name": "x", "size": 10, "size_vram": 10},
                                 {"name": "y", "size": 10, "size_vram": 10}], ["x", "y"])
        self.assertTrue(models._fits_beside(found["a/x"], {"b/y"}))

    def test_a_start_command_that_fails_at_once_fails_the_load_at_once(self):
        # it was polled for 20 minutes, its exit unseen (the 2026-11-16 audit)
        self.config({"s": {"options": {"baseURL": "http://127.0.0.1:1/v1"}, "models": {"m": {}}}},
                    {"s": {"engine": "server", "start": "echo boom >&2; exit 3"}})
        t0 = time.time()
        with mock.patch.object(models, "START_GRACE", 0), \
                self.assertRaisesRegex(RuntimeError, "start command exited 3 .*run it by hand"):
            models.make_resident(models.resolve("s/m"), say=lambda m: None)
        self.assertLess(time.time() - t0, 10)

    def test_wb_image_waits_only_for_what_free_gpu_can_release(self):
        # a server with no stop command cost every generation the whole 90 s (the 2026-11-16 audit)
        srv = self.server({"/v1/models": {"data": [{"id": "z"}]}})
        self.config({"one": {"options": {"baseURL": srv + "/v1"}, "models": {"z": {}}}})
        self.assertEqual([m.id for m in models.resident()], ["one/z"])
        said, t0 = [], time.time()
        with mock.patch.object(images.hardware, "gpus", lambda windows=True: []):
            images._settle(said.append, timeout=30)
        self.assertLess(time.time() - t0, 5)
        self.assertEqual(said, [])

    def test_this_machine_is_every_loopback_and_its_own_name(self):
        here = socket.gethostname()
        self.config({"a": {"options": {"baseURL": "http://127.0.0.2:1/v1"}, "models": {"m": {}}},
                     "b": {"options": {"baseURL": f"http://{here}:1/v1"}, "models": {"m": {}}},
                     "c": {"options": {"baseURL": "http://[::1]:1/v1"}, "models": {"m": {}}},
                     "lan": {"options": {"baseURL": "http://192.168.7.7:1/v1"}, "models": {"m": {}}},
                     "box": {"options": {"baseURL": "http://192.168.7.8:1/v1"}, "models": {"m": {}}},
                     "cloud": {"options": {"baseURL": "https://api.example.com/v1"}, "models": {"m": {}}}},
                    {"box": {"local": True}})
        found = models.discover()
        self.assertEqual(sorted(found), ["a/m", "b/m", "box/m", "c/m"])
        self.assertTrue(found["box/m"].remote and not found["a/m"].remote)
        self.assertFalse(models.can_share(found["box/m"], [found["a/m"]], int(99e9)))

    def test_opencode_jsonc_under_xdg_config_home(self):
        del os.environ["WB_OPENCODE_CONFIG"]
        os.environ["XDG_CONFIG_HOME"] = str(Path(self.tmp.name) / "xdg")
        d = Path(self.tmp.name) / "xdg" / "opencode"
        d.mkdir(parents=True)
        (d / "opencode.jsonc").write_text(
            '{\n  // my models\n  "provider": {"o": {"options": {"baseURL": "http://127.0.0.1:11434/v1"},'
            ' /* the big one */ "models": {"m": {"name": "a // not a comment"},},},},\n}\n')
        self.assertEqual(models.opencode_config_path(), d / "opencode.jsonc")
        self.assertEqual(models.discover()["o/m"].label, "a // not a comment")
        with self.assertRaisesRegex(RuntimeError, "has comments"):
            models.write_entry("o", "n", {})
        self.assertIn("// my models", (d / "opencode.jsonc").read_text())  # untouched

    def test_claude_config_dir_moves_the_users_claude_files(self):
        os.environ["CLAUDE_CONFIG_DIR"] = str(Path(self.tmp.name) / "cc")
        skill = Path(self.tmp.name) / "cc" / "skills" / "my-own"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: my-own\n---\n")
        self.assertEqual(context.claude_home(), Path(self.tmp.name) / "cc")
        self.assertIn("my-own", context.denied_skills())


class Fallback(unittest.TestCase):
    """watch.Watcher without inotify (macOS): it still says what changed."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = watch._libc
        watch._libc = False

    def tearDown(self):
        watch._libc = self.saved
        self.tmp.cleanup()

    def test_changes_are_reported_without_inotify(self):
        d = Path(self.tmp.name)
        (d / "status.json").write_text("{}")
        with watch.Watcher(d, recursive=True) as w:
            self.assertFalse(w.native)
            self.assertEqual(w.wait(0.3), [])
            threading.Timer(0.2, lambda: (d / "status.json").write_text('{"phase": "review"}')).start()
            self.assertIn(d / "status.json", w.wait(5))
            (d / "tasks").mkdir()
            self.assertIn(d / "tasks", w.wait(5))
            threading.Timer(0.2, lambda: (d / "tasks" / "notify.jsonl").write_text("x\n")).start()
            self.assertIn(d / "tasks" / "notify.jsonl", w.wait(5))  # a new directory is watched too

    def test_it_still_returns_within_a_second(self):
        with watch.Watcher(Path(self.tmp.name)) as w:
            t0 = time.time()
            w.wait(30)
            self.assertLess(time.time() - t0, 1.5)  # callers re-check what no file says


class Launchers(unittest.TestCase):
    """bin/wb and bin/wb-ask-mcp on an interpreter the package can't run on: macOS's stock 3.9,
    Ubuntu 22.04's 3.10, native Windows (the 2026-11-16 audit)."""
    ROOT = Path(__file__).resolve().parent.parent

    def launch(self, name, setup):
        import subprocess
        code = f"import runpy, sys; {setup}; sys.argv = [{name!r}, '--version']; runpy.run_path({str(self.ROOT / 'bin' / name)!r})"
        return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)

    def test_an_old_python_is_told_the_version_it_needs_before_any_import(self):
        for name in ("wb", "wb-ask-mcp"):
            r = self.launch(name, "sys.version_info = (3, 10, 12)")
            self.assertEqual(r.returncode, 1, r.stderr)
            self.assertIn(f"{name}: needs Python 3.11 or newer", r.stderr)
            self.assertNotIn("Traceback", r.stderr)

    def test_native_windows_is_told_to_use_wsl2(self):
        r = self.launch("wb", "sys.platform = 'win32'")
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("native Windows is not supported", r.stderr)
        self.assertIn("WSL2", r.stderr)

    def test_a_supported_python_runs_the_package(self):
        r = self.launch("wb", "pass")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.startswith("wb "))


class Fit(Base):
    """The router on a known GPU: a model that spills into RAM loses the size guess."""
    def setUp(self):
        super().setUp()
        Path(os.environ["WB_MODELS"]).write_text(json.dumps({
            "ollama/qwen3.8-27b": {"weights_gb": 20}, "ollama/mimo-v2.6:9b-q8_0": {"weights_gb": 9},
            "strata": {"skip": True}}))

    def test_the_largest_model_that_fits_the_gpu(self):
        os.environ["WB_FAKE_GPU_GB"] = "16"         # the big one's 20 GB of weights spill; 9 fit
        top, why = router.choose("significant")
        self.assertEqual(top.model.id, "ollama/mimo-v2.6:9b-q8_0")
        self.assertIn("the largest model allowed significant work that fits the GPU", why)

    def test_a_big_enough_gpu_or_an_unknown_one_keeps_the_largest(self):
        os.environ["WB_FAKE_GPU_GB"] = "48"
        self.assertEqual(router.choose("significant")[0].model.id, "ollama/qwen3.8-27b")
        del os.environ["WB_FAKE_GPU_GB"]
        self.assertEqual(router.choose("significant")[0].model.id, "ollama/qwen3.8-27b")


class Budgets(Base):
    """The runner's limits from the model's measured speed."""
    def session(self, write_tps, read_tps, n=5):
        msgs = []
        for i in range(n):
            t0 = 1_000_000 + i * 100_000
            msgs.append({"type": "assistant",
                         "tokens": {"input": 4000, "output": 1000, "reasoning": 1000, "cache": {"read": 9000}},
                         "time": {"created": t0, "firstToken": t0 + 4000 / read_tps * 1000,
                                  "completed": t0 + 4000 / read_tps * 1000 + 2000 / write_tps * 1000}})
        msgs.append({"type": "user"})
        return msgs

    def test_unmeasured_and_this_machines_models_keep_the_defaults(self):
        m = models.resolve("mimo")
        self.assertEqual(router.budgets(m), {"think": 420, "silence": 900, "round": 2400})
        router.record_speed(m.id, self.session(36.3, 748))     # the 27B the defaults were set on
        self.assertEqual(router.budgets(m)["think"], 420)
        router.record_speed(m.id, self.session(60, 2800))      # faster: never below the default
        self.assertEqual(router.budgets(m), {"think": 420, "silence": 900, "round": 2400})

    def test_a_slow_machine_gets_time(self):
        m = models.resolve("mimo")
        router.record_speed(m.id, self.session(15, 150))
        self.assertEqual(router.speed(m.id), {"write": 15.0, "read": 150})
        b = router.budgets(m)
        self.assertEqual(b["think"], int(13500 / 15 + 32768 / 150))   # 1118 s
        self.assertEqual(b["silence"], int(900 * b["think"] / 420))
        self.assertEqual(b["round"], int(2400 * b["think"] / 420))
        self.start("t-slow", "ok")
        spec = store.Task("t-slow").spec
        self.assertEqual((spec["think_timeout"], spec["silence_timeout"]), (b["think"], b["silence"]))
        wait_phase("t-slow", {"review"})


if __name__ == "__main__":
    unittest.main()
