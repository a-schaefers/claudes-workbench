"""Machines other than the one workbench grew up on: other servers, ports, configs, platforms, speeds."""
import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # run as a script too

from workbench import context, models, router, store, watch
from tests.test_workbench import Base, wait_phase


def serve(routes: dict) -> tuple[ThreadingHTTPServer, str]:
    """A model server on a free port answering GET path -> JSON body (404 for anything else). A
    route may be a function of the request's headers, and an int body is that status code."""
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = routes.get(self.path)
            body = body(self.headers) if callable(body) else body
            if isinstance(body, int):
                self.send_response(body)
                self.end_headers()
                return
            self.send_response(200 if body is not None else 404)
            self.end_headers()
            self.wfile.write(json.dumps(body if body is not None else {"error": "no"}).encode())

        def log_message(self, *a):
            pass
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


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

    def test_opencode_json_and_jsonc_are_layered_as_opencode_does(self):
        # the 2026-11-16 audit: a provider in opencode.jsonc beside an opencode.json holding only a
        # theme was invisible, since only the .json was read
        del os.environ["WB_OPENCODE_CONFIG"]
        os.environ["XDG_CONFIG_HOME"] = str(Path(self.tmp.name) / "xdg")
        d = Path(self.tmp.name) / "xdg" / "opencode"
        d.mkdir(parents=True)
        (d / "opencode.json").write_text(json.dumps({"theme": "dark", "provider": {
            "o": {"options": {"baseURL": "http://127.0.0.1:11434/v1"}, "models": {"a": {}}}}}))
        (d / "opencode.jsonc").write_text('{"provider": {"o": {"models": {"b": {}}}}}')
        self.assertEqual(sorted(models.discover()), ["o/a", "o/b"])
        self.assertEqual(models.opencode_config_files(), [d / "opencode.json", d / "opencode.jsonc"])
        with self.assertRaisesRegex(RuntimeError, "layers"):   # one merged file would lose the layering
            models.write_entry("o", "c", {})

    def test_discovery_follows_opencodes_provider_filters_and_env_urls(self):
        # the 2026-11-16 audit: wb routed to providers OpenCode wouldn't load, and dropped a
        # provider whose baseURL came from {env:NAME}
        os.environ["WB_TEST_BASE"] = "http://127.0.0.1:1"
        os.environ.pop("WB_TEST_UNSET", None)
        Path(os.environ["WB_OPENCODE_CONFIG"]).write_text(json.dumps({
            "disabled_providers": ["off"], "provider": {
                "env": {"options": {"baseURL": "{env:WB_TEST_BASE}/v1"}, "models": {"m": {}}},
                "unset": {"options": {"baseURL": "{env:WB_TEST_UNSET}/v1"}, "models": {"m": {}}},
                "off": {"options": {"baseURL": "http://127.0.0.1:2/v1"}, "models": {"m": {}}}}}))
        found = models.discover()
        self.assertEqual(list(found), ["env/m"])
        self.assertEqual(found["env/m"].base_url, "http://127.0.0.1:1/v1")
        skipped = dict(models.skipped_providers())
        self.assertIn("is WB_TEST_UNSET set", skipped["unset"])
        self.assertIn("disabled_providers", skipped["off"])
        cfg = json.loads(Path(os.environ["WB_OPENCODE_CONFIG"]).read_text())
        cfg["enabled_providers"] = ["off"]
        Path(os.environ["WB_OPENCODE_CONFIG"]).write_text(json.dumps(cfg))
        self.assertEqual(list(models.discover()), [])
        self.assertIn("enabled_providers", dict(models.skipped_providers())["env"])

    def test_a_provider_on_another_host_is_named_with_the_opt_in(self):
        # the 2026-11-16 audit: WSL2's Windows-host Ollama was dropped with "0 local model(s)"
        self.config({"win": {"options": {"baseURL": "http://10.255.255.1:11434/v1"}, "models": {"m": {}}}})
        self.assertEqual(models.discover(), {})
        self.assertIn('add {"win": {"local": true}}', models.why_none())
        with self.assertRaisesRegex(KeyError, "this machine"):
            models.resolve("m")

    def test_an_ollama_baseurl_without_v1_is_refused_with_the_reason(self):
        # the 2026-11-16 audit: the /api probes said ready and every request then got a 404
        ollama = self.server({"/api/version": {"version": "0.40.0"},
                              "/api/tags": {"models": [{"name": "m:latest", "size": 9}]}})
        self.config({"o": {"options": {"baseURL": ollama}, "models": {"m": {}}}})
        st = models.status(models.resolve("o/m"))
        self.assertFalse(st["available"])
        self.assertIn("needs /v1", st["why"])
        self.assertIn("needs /v1", models.providers()["o"]["problem"])

    def test_an_ollama_cloud_model_is_no_candidate_until_opted_in(self):
        # the 2026-11-16 audit: a cloud stub of 384 bytes was "the smallest model allowed trivial
        # work", so trivial tasks' code went to ollama.com
        ollama = self.server({"/api/version": {"version": "0.40.0"}, "/api/ps": {"models": []},
                              "/api/tags": {"models": [
                                  {"name": "coder:latest", "size": 14_000_000_000},
                                  {"name": "glm-5.1:cloud", "size": 384, "remote_host": "https://ollama.com:443"},
                                  {"name": "big:latest", "size": 400, "remote_model": "big"}]}})
        self.config({"o": {"options": {"baseURL": ollama + "/v1"},
                           "models": {"coder": {}, "glm-5.1:cloud": {}, "big": {}}}})
        found = models.discover()
        for mid in ("o/glm-5.1:cloud", "o/big"):
            st = models.status(found[mid])
            self.assertFalse(st["available"])
            self.assertIn("Ollama cloud model", st["why"])
            self.assertEqual(models.weights(found[mid]), 0)
        self.assertEqual(router.choose("trivial")[0].model.id, "o/coder")
        self.assertEqual([x["name"] for x in models.ollama_installed(ollama) if x["cloud"]],
                         ["glm-5.1:cloud", "big:latest"])
        Path(os.environ["WB_MODELS"]).write_text(json.dumps({"o/glm-5.1:cloud": {"local": True}}))
        m = models.resolve("o/glm-5.1:cloud")
        self.assertTrue(m.remote)          # opted in: it holds no GPU lease here
        self.assertTrue(models.status(m)["available"])

    def test_a_server_that_wants_an_api_key_is_running_and_gets_the_providers_key(self):
        # the 2026-11-16 audit: a 401 read as "not running", and with a start command wb started a
        # second copy and waited 20 minutes for it
        os.environ["WB_TEST_KEY"] = "sekret"
        keyed = self.server({"/v1/models": lambda h: {"data": [{"id": "a"}, {"id": "b"}]}
                             if h.get("Authorization") == "Bearer sekret" else 401})
        self.config({"k": {"options": {"baseURL": keyed + "/v1", "apiKey": "{env:WB_TEST_KEY}"},
                           "models": {"a": {}, "c": {}}},
                     "nokey": {"options": {"baseURL": keyed + "/v1"}, "models": {"a": {}}}})
        found = models.discover()
        self.assertEqual(models.status(found["k/a"])["why"], "running")
        self.assertIn("not c", models.status(found["k/c"])["why"])     # the list was read with the key
        st = models.status(found["nokey/a"])
        self.assertTrue(st["available"] and st["resident"])
        self.assertIn("API key", st["why"])
        self.assertNotIn("sekret", repr(found["k/a"]))

    def test_a_proxy_in_the_environment_never_carries_probes_to_this_machine(self):
        # the 2026-11-16 audit: behind a proxy without no_proxy, every local model was "not answering"
        url = self.server({"/api/version": {"version": "0.40.0"}})
        os.environ["http_proxy"] = os.environ["HTTP_PROXY"] = "http://127.0.0.1:9"
        os.environ.pop("no_proxy", None)
        os.environ.pop("NO_PROXY", None)
        self.assertEqual(models._get(url + "/api/version"), {"version": "0.40.0"})
        self.assertEqual(models._get(url.replace("127.0.0.1", "localhost") + "/api/version"),
                         {"version": "0.40.0"})

    def test_a_down_ollama_on_another_port_is_still_ollama_and_add_waits_for_it(self):
        # the 2026-11-16 audit: read as a server, wb model add wrote an entry with no output cap
        from workbench import cli
        self.config({"ollama": {"options": {"baseURL": "http://127.0.0.1:1/v1"}, "models": {}},
                     "box": {"options": {"baseURL": "http://127.0.0.1:2/v1"}, "models": {}}})
        self.assertEqual(models.providers()["ollama"]["engine"], "ollama")
        self.assertEqual(models.providers()["box"]["engine"], "server")
        for mid in ("ollama/m", "box/m"):
            err = io.StringIO()
            with self.assertRaises(SystemExit), redirect_stderr(err):
                cli.main(["model", "add", mid, "--context", "32768"])
            self.assertIn("is not answering", err.getvalue())

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
