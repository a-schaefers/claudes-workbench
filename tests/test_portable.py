"""Machines other than the one workbench grew up on: other servers, ports, configs, platforms, speeds."""
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # run as a script too

from unittest import mock

from workbench import context, images, models, router, store, watch
from tests.test_workbench import Base, TEST_CMD, run_wb, wait_phase


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


def fake_ollama(loaded: list, tags: list, posts: list, fail: dict | None = None,
                down: Path | None = None):
    """An Ollama on a free port: /api/tags lists ``tags``, /api/ps ``loaded`` (dicts with name,
    size, size_vram); POST /api/generate is recorded in ``posts`` and loads or unloads, unless
    ``fail`` (status, body) answers it. While the file ``down`` exists it answers nothing but 503
    (a restart), and comes back with nothing loaded."""
    class H(BaseHTTPRequestHandler):
        def reply(self, code, body):
            self.send_response(code)
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def away(self):
            if down and down.exists():
                loaded.clear()
                self.reply(503, {"error": "restarting"})
                return True
            return False

        def do_GET(self):
            if self.away():
                return
            body = {"/api/version": {"version": "0.40.0"}, "/api/tags": {"models": [{"name": n} for n in tags]},
                    "/api/ps": {"models": loaded}}.get(self.path)
            self.reply(200 if body is not None else 404, body or {})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.away():
                return
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
                          WB_MODELS=str(tmp / "models.json"), WB_COMMANDS=str(tmp / "commands.json"))
        models._ENGINES.clear()
        models._FACTS.clear()
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

    def test_a_shared_port_is_known_by_the_file_in_each_start_command_and_switched(self):
        # names that differ from the file left both models unavailable, the served one too, and a
        # task failed right after wb started the right server (the 0.90 review)
        served = Path(self.tmp.name) / "served"
        llama = self.server({"/v1/models": lambda h: {"data": [{"id": served.read_text().strip()}]}
                             if served.exists() else None})
        start = lambda f: f"echo /models/{f} > {served}"  # noqa: E731
        self.config({"l": {"options": {"baseURL": llama + "/v1"},
                           "models": {"qwen3-coder-30b": {}, "devstral-24b": {}}}},
                    {"l": {"engine": "server", "stop": f"rm -f {served}"},
                     "l/qwen3-coder-30b": {"start": start("Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf")},
                     "l/devstral-24b": {"start": start("Devstral-Small-24B-Q4_K_M.gguf")}})
        qwen, dev = models.resolve("l/qwen3-coder-30b"), models.resolve("l/devstral-24b")
        said = []
        self.assertEqual(models.status(dev)["why"], "stopped (wb starts it)")
        self.assertGreater(models.make_resident(dev, say=said.append), 0)     # a cold start
        self.assertEqual(models.status(dev)["why"], "running")
        self.assertEqual(models.status(qwen), {"available": True, "resident": False, "why":
                         "the port serves l/devstral-24b; wb stops it and starts l/qwen3-coder-30b"})
        models.make_resident(qwen, say=said.append)                            # the switch
        self.assertEqual(said, ["starting l/devstral-24b", "stopping l/devstral-24b",
                                "starting l/qwen3-coder-30b"])
        self.assertTrue(models.status(qwen)["resident"])
        self.assertFalse(models.status(dev)["resident"])
        served.write_text("/models/something-else.gguf")                      # nobody's file
        st = models.status(qwen)
        self.assertFalse(st["available"])
        self.assertIn("can't tell which model it is", st["why"])

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

    def test_an_alias_is_checked_loaded_and_placed_by_the_id_its_server_knows(self):
        # the 2026-11-19 audit (finding 14): an entry keyed `friendly` with "id": "cap" was "the
        # server lists 2 models, not friendly", while OpenCode sends cap and it runs
        two = self.server({"/v1/models": {"data": [{"id": "cap"}, {"id": "other"}]}})
        posts = []
        srv, port = fake_ollama([], ["coder:7b"], posts)
        self.servers.append(srv)
        self.config({"s": {"options": {"baseURL": two + "/v1"},
                           "models": {"friendly": {"id": "cap"}}},
                     "o": {"options": {"baseURL": f"http://127.0.0.1:{port}/v1"},
                           "models": {"small": {"id": "coder:7b"}}}})
        found = models.discover()
        self.assertEqual(sorted(found), ["o/small", "s/friendly"])   # routed by the key
        self.assertEqual(models.status(found["s/friendly"])["why"], "running")
        self.assertEqual(models.status(found["o/small"])["why"], "pulled")
        self.assertEqual(models.resolve("friendly").id, "s/friendly")
        models.make_resident(found["o/small"], say=lambda m: None)
        self.assertEqual([p["model"] for p in posts], ["coder:7b"])  # loaded by its id
        self.assertTrue(models.status(found["o/small"])["resident"])

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
        self.assertFalse(models._fits_beside(found["a/x"], {"b/y"}, {"y"}))   # y was evicted
        # y was already unloaded idle before the load: not an eviction (the 0.90 review)
        self.assertTrue(models._fits_beside(found["a/x"], {"b/y"}, set()))
        found, posts = self.own([{"name": "x", "size": 10, "size_vram": 10},
                                 {"name": "y", "size": 10, "size_vram": 10}], ["x", "y"])
        self.assertTrue(models._fits_beside(found["a/x"], {"b/y"}))

    def test_a_partner_model_that_idled_out_before_the_load_is_no_eviction(self):
        # between rounds, a partner's model unloaded by Ollama's keep-alive read as evicted: the
        # task unloaded its own model and waited for the partner's whole run (the 0.90 review)
        found, posts = self.own([{"name": "x", "size": 10, "size_vram": 10}], ["x", "y"])
        self.assertTrue(models._fits_beside(found["a/x"], {"b/y"}, before={"x"}))
        self.assertFalse(models._fits_beside(found["a/x"], {"b/y"}, before={"x", "y"}))
        partner = subprocess.Popen(["sleep", "60"])  # its runner, holding b/y beside us
        self.addCleanup(partner.kill)
        store.home().mkdir(parents=True, exist_ok=True)
        store.write_json(store.home() / "gpu.json", {str(partner.pid): {"model": "b/y", "task": "t-b"}})
        alone = []
        with mock.patch.object(models.GpuLease, "_take_alone", lambda self, held: alone.append(held)):
            self.assertEqual(models.GpuLease(found["a/x"], "t-a", lambda m: None).load(), 0.0)
        self.assertEqual((alone, posts), ([], []))  # x stays loaded, nobody waits

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

    def test_a_refused_rewrite_says_the_change_and_never_the_providers_keys(self):
        # the 2026-11-16 audit: the refusal printed the whole config, an apiKey included, into
        # Claude's context, cut at 2000 characters before the new entry
        Path(os.environ["WB_OPENCODE_CONFIG"]).write_text(
            '{// mine\n"provider": {"o": {"options": {"baseURL": "http://127.0.0.1:11434/v1"}, "models": {}},'
            ' "or": {"options": {"apiKey": "sk-or-SECRET-123"}}}}')
        with self.assertRaises(RuntimeError) as e:
            models.write_entry("o", "coder", {"limit": {"context": 32768}})
        self.assertIn("has comments", str(e.exception))
        self.assertIn('"coder"', str(e.exception))
        self.assertIn("32768", str(e.exception))
        self.assertNotIn("SECRET", str(e.exception))

    def test_a_symlinked_config_is_written_through_and_keeps_its_mode(self):
        # the 2026-11-16 audit: a dotfiles link became a plain file, and 0600 came back 0644
        real = Path(self.tmp.name) / "dotfiles" / "opencode.json"
        real.parent.mkdir()
        real.write_text(json.dumps({"provider": {"o": {"options": {"baseURL": "http://127.0.0.1:11434/v1"},
                                                       "models": {}}}}))
        real.chmod(0o600)
        link = Path(os.environ["WB_OPENCODE_CONFIG"])
        link.symlink_to(real)
        models.write_entry("o", "coder", {"limit": {"context": 32768}})
        self.assertTrue(link.is_symlink())
        self.assertIn("coder", json.loads(real.read_text())["provider"]["o"]["models"])
        self.assertEqual(real.stat().st_mode & 0o777, 0o600)
        if os.geteuid() != 0:  # a link into a read-only store (Nix) is refused, never replaced
            real.parent.chmod(0o500)
            try:
                with self.assertRaisesRegex(RuntimeError, "isn't writable"):
                    models.write_entry("o", "other", {})
            finally:
                real.parent.chmod(0o700)
            self.assertTrue(link.is_symlink())

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

    def test_a_server_that_wants_an_api_key_gets_the_providers_key_and_refuses_a_wrong_one(self):
        # the 2026-11-16 audit: a 401 read as "not running", and with a start command wb started a
        # second copy and waited 20 minutes for it. The 2026-11-19 audit (finding 19): read as
        # running instead, a wrong key passed too, and the router could pick that model over a
        # working one; it is now unavailable, so nothing is started and the router passes it by.
        os.environ["WB_TEST_KEY"] = "sekret"
        Path(os.environ["WB_COMMANDS"]).write_text(json.dumps({"env": ["WB_TEST_KEY"]}))
        keyed = self.server({"/v1/models": lambda h: {"data": [{"id": "a"}, {"id": "b"}]}
                             if h.get("Authorization") == "Bearer sekret" else 401})
        self.config({"k": {"options": {"baseURL": keyed + "/v1", "apiKey": "{env:WB_TEST_KEY}"},
                           "models": {"a": {}, "c": {}}},
                     "nokey": {"options": {"baseURL": keyed + "/v1"}, "models": {"a": {}}},
                     "wrong": {"options": {"baseURL": keyed + "/v1", "apiKey": "nope"},
                               "models": {"a": {}}}},
                    {"nokey/a": {"start": f"touch {self.tmp.name}/started"}})
        found = models.discover()
        self.assertEqual(models.status(found["k/a"])["why"], "running")
        self.assertIn("not c", models.status(found["k/c"])["why"])     # the list was read with the key
        for mid, why in (("nokey/a", "it wants an API key"), ("wrong/a", "refuses its API key")):
            st = models.status(found[mid])
            self.assertFalse(st["available"] or st["resident"], mid)
            self.assertIn(why, st["why"])
            self.assertIn("options.apiKey", st["why"])
        with self.assertRaisesRegex(RuntimeError, "nokey/a can't run: the server"):
            models.make_resident(found["nokey/a"], say=lambda m: None)
        self.assertFalse((Path(self.tmp.name) / "started").exists())   # no second copy started
        self.assertNotIn("sekret", repr(found["k/a"]))

    def test_a_start_command_that_exits_at_once_paces_its_readiness_probes(self):
        # the 2026-11-19 audit (finding 20): a start that daemonizes (systemctl, docker, cmd &)
        # exits 0 at once, p.wait returned at once on every pass, and the loop probed back to
        # back: 3,095 requests in 0.8 s
        asked = []
        url = self.server({"/v1/models": lambda h: asked.append(1) or 503})
        self.config({"s": {"options": {"baseURL": url + "/v1"}, "models": {"a": {}}}},
                    {"s/a": {"start": "true"}})
        with mock.patch.object(models, "START_TIMEOUT", 3), \
                self.assertRaisesRegex(RuntimeError, "did not come up"):
            models.make_resident(models.resolve("s/a"), say=lambda m: None)
        self.assertLess(len(asked), 8, len(asked))

    def test_an_api_key_the_agent_wont_get_makes_the_model_unavailable_with_the_fix(self):
        # probed with wb's own key it read running, and the agent, its key dropped, had every
        # request refused (the 0.90 review)
        os.environ["LLAMA_API_KEY"] = "sekret"
        keyed = self.server({"/v1/models": lambda h: {"data": [{"id": "a"}]}
                             if h.get("Authorization") == "Bearer sekret" else 401})
        self.config({"k": {"options": {"baseURL": keyed + "/v1", "apiKey": "{env:LLAMA_API_KEY}"},
                           "models": {"a": {}}}})
        st = models.status(models.resolve("k/a"))
        self.assertFalse(st["available"])
        self.assertIn("its apiKey reads {env:LLAMA_API_KEY}, which agents don't get: add "
                      "LLAMA_API_KEY to commands.json env", st["why"])
        Path(os.environ["WB_COMMANDS"]).write_text(json.dumps({"env": ["LLAMA_API_KEY"]}))
        self.assertEqual(models.status(models.resolve("k/a"))["why"], "running")

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

    # only Ollama's API was read: on any other server the router ranked models without their
    # sizes, checked no fit, and every window had to be typed in (2026-11-17)
    def test_lm_studio_says_each_models_size_window_and_tool_training(self):
        from workbench import cli
        lms = self.server({"/api/v1/models": {"models": [
            {"type": "llm", "key": "qwen/coder-30b", "size_bytes": 18_000_000_000,
             "loaded_instances": [{"id": "qwen/coder-30b", "config": {"context_length": 65536}}],
             "capabilities": {"trained_for_tool_use": True}},
            {"type": "llm", "key": "tiny/chat", "size_bytes": 900_000_000, "loaded_instances": [],
             "capabilities": {"trained_for_tool_use": False}}]}})
        self.config({"lms": {"options": {"baseURL": lms + "/v1"}, "models": {}}}, {"lms": {"engine": "server"}})
        self.assertEqual(models.server_facts(lms, "qwen/coder-30b"),
                         {"context": 65536, "weights": 18_000_000_000, "tools": True})
        err = io.StringIO()
        with self.assertRaises(SystemExit), redirect_stderr(err):  # not loaded: its window is unknown
            cli.main(["model", "add", "lms/tiny/chat"])
        self.assertIn("doesn't say what window it serves tiny/chat (LM Studio: not loaded yet)", err.getvalue())
        with redirect_stdout(io.StringIO()):
            cli.main(["model", "add", "lms/tiny/chat", "--context", "32768"])
            cli.main(["model", "add", "lms/qwen/coder-30b"])  # no --context: LM Studio said it
        entry = json.loads(Path(os.environ["WB_OPENCODE_CONFIG"]).read_text())["provider"]["lms"]["models"]
        self.assertEqual(entry["qwen/coder-30b"]["limit"]["context"], 65536)
        self.assertEqual(models.weights(models.resolve("lms/qwen/coder-30b")), 18_000_000_000)
        entry = json.loads(Path(os.environ["WB_OPENCODE_CONFIG"]).read_text())["provider"]["lms"]["models"]
        self.assertFalse(entry["tiny/chat"]["tool_call"])  # LM Studio says it wasn't trained for tools

    def test_llama_server_says_its_size_and_the_window_of_one_slot_and_is_ranked_by_size(self):
        big = self.server({"/v1/models": {"data": [{"id": "/m/big.gguf", "owned_by": "llamacpp",
                                                      "meta": {"size": 20_000_000_000, "n_ctx_train": 131072}}]},
                           "/props?model=%2Fm%2Fbig.gguf&autoload=false":
                               {"default_generation_settings": {"n_ctx": 32768}}})
        small = self.server({"/v1/models": {"data": [{"id": "/m/small.gguf", "owned_by": "llamacpp",
                                                        "meta": {"size": 2_000_000_000}}]}})  # no /props
        self.config({"big": {"options": {"baseURL": big + "/v1"}, "models": {"big": {}}},
                     "small": {"options": {"baseURL": small + "/v1"}, "models": {"small": {}}}},
                    {"big": {"engine": "server"}, "small": {"engine": "server"}})
        self.assertEqual(models.server_facts(big, "big"),  # the slot's window, not the trained one
                         {"context": 32768, "weights": 20_000_000_000, "tools": None})
        self.assertEqual(models.server_facts(small, "small")["context"], None)
        with mock.patch.object(models, "gpu_budget", return_value=64_000_000_000):
            self.assertEqual([s.model.id for s in router.rank("significant")[:2]], ["big/big", "small/small"])
            self.assertEqual(router.rank("trivial")[0].model.id, "small/small")
        with mock.patch.object(models, "gpu_budget", return_value=8_000_000_000):
            self.assertIs(router.rank("significant")[-1].fits, False)  # 20 GB of weights on an 8 GB card

    def test_vllm_says_its_window_and_doctor_flags_an_entry_past_it(self):
        from workbench import cli
        vllm = self.server({"/v1/models": {"data": [{"id": "coder", "owned_by": "vllm", "max_model_len": 32768}]}})
        self.config({"v": {"options": {"baseURL": vllm + "/v1"},
                           "models": {"coder": {"limit": {"context": 131072}}}}}, {"v": {"engine": "server"}})
        self.assertEqual(models.server_facts(vllm, "coder")["context"], 32768)
        out = io.StringIO()
        with self.assertRaises(SystemExit), redirect_stdout(out):
            cli.main(["doctor"])
        self.assertIn("BAD v/coder says a 128K context but the server serves 32K", out.getvalue())

    def test_a_down_ollama_is_named_by_add_and_retire_without_a_traceback(self):
        # the 2026-11-16 audit: --pull died in a urllib traceback, a describe blamed a missing
        # pull, and retire --delete crashed after the entry was already gone
        from workbench import cli
        self.config({"o": {"options": {"baseURL": "http://127.0.0.1:1/v1"},
                           "models": {"old": {"limit": {"context": 32768}}}}}, {"o": {"engine": "ollama"}})
        for argv in (["model", "add", "o/m", "--pull"], ["model", "add", "o/m", "--context", "32768"]):
            err = io.StringIO()
            with self.assertRaises(SystemExit), redirect_stderr(err):
                cli.main(argv)
            self.assertIn("Ollama isn't answering at http://127.0.0.1:1", err.getvalue())
        out = io.StringIO()
        with redirect_stdout(out):
            cli.main(["model", "retire", "o/old", "--delete"])
        self.assertIn("weights were NOT deleted", out.getvalue())
        self.assertIn("ollama rm old", out.getvalue())
        self.assertNotIn("old", json.loads(Path(os.environ["WB_OPENCODE_CONFIG"]).read_text())["provider"]["o"]["models"])

    def test_doctor_fails_when_no_model_can_run_and_asks_a_down_ollama_once(self):
        # the 2026-11-16 audit: doctor said all was well with no model able to run and an entry
        # with no limit.context, and a sleeping Ollama cost it 35 s a model
        from workbench import cli
        self.config({"o": {"options": {"baseURL": "http://127.0.0.1:1/v1"},
                           "models": {"a": {"options": {"max_tokens": 1}}, "b": {}}}}, {"o": {"engine": "ollama"}})
        out = io.StringIO()
        with mock.patch.object(models, "ollama_answers", wraps=models.ollama_answers) as asked, \
                mock.patch.object(models, "ollama_served_context") as served, \
                self.assertRaises(SystemExit) as code, redirect_stdout(out):
            cli.main(["doctor"])
        self.assertEqual(code.exception.code, 1)
        text = out.getvalue()
        self.assertIn("BAD no model can run now (o/a: start Ollama at http://127.0.0.1:1", text)
        self.assertIn("BAD o/b has no limit.context", text)
        self.assertIn("BAD o/b caps its output on Ollama", text)
        self.assertEqual(text.count("Ollama isn't answering at http://127.0.0.1:1"), 1)
        self.assertEqual([c.args for c in asked.call_args_list].count(("http://127.0.0.1:1",)), 1)
        served.assert_not_called()

    def test_the_served_context_asks_no_more_of_an_ollama_that_isnt_answering(self):
        # the 2026-11-16 audit: /api/show waited 30 s a model after /api/ps had no answer, and
        # this machine's OLLAMA_CONTEXT_LENGTH was taken for another host's
        from workbench import hardware
        with mock.patch.object(models, "_get", return_value=None), \
                mock.patch.object(models, "ollama_show") as show, \
                mock.patch.object(hardware, "ollama_env", return_value={"OLLAMA_CONTEXT_LENGTH": '"32768"'}):
            self.assertEqual(models.ollama_served_context("http://127.0.0.1:11434", "m"), 32768)
            self.assertIsNone(models.ollama_served_context("http://192.168.7.7:11434", "m"))
        show.assert_not_called()

    def test_a_model_that_cant_load_gets_its_next_step_and_never_empties_the_gpu(self):
        # the 2026-11-16 audit: wb start said only why, offered --force for an unpulled model,
        # and the forced task unloaded the GPU before its load failed
        ollama = self.server({"/api/version": {"version": "0.40.0"}, "/api/tags": {"models": []}})
        self.config({"o": {"options": {"baseURL": ollama + "/v1"}, "models": {"m": {}}}})
        m = models.resolve("o/m")
        st = models.status(m)
        self.assertEqual(models.remedy(m, st), "wb model add o/m --pull downloads it")
        self.assertFalse(models.may_come_up(st))
        self.assertIn("m is not pulled (wb model add o/m --pull", router.choose("significant")[1])
        with mock.patch.object(models, "free_gpu") as free:
            with self.assertRaisesRegex(RuntimeError, "not pulled: wb model add"):
                models.make_resident(m)
        free.assert_not_called()

    def test_claude_config_dir_moves_the_users_claude_files(self):
        os.environ["CLAUDE_CONFIG_DIR"] = str(Path(self.tmp.name) / "cc")
        skill = Path(self.tmp.name) / "cc" / "skills" / "my-own"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: my-own\n---\n")
        self.assertEqual(context.claude_home(), Path(self.tmp.name) / "cc")
        self.assertIn("my-own", context.denied_skills())


    def test_state_files_are_utf8_under_a_legacy_locale(self):
        # the 2026-11-16 audit: an em dash in a spec failed to write under a non-UTF-8 locale
        code = ("import sys; sys.path.insert(0, sys.argv[1]); from pathlib import Path; "
                "from workbench import store; p = Path(sys.argv[2]); "
                "store.write_json(p, {'t': 'a \\u2014 b'}); "
                "store.append_jsonl(p.with_suffix('.jsonl'), {'t': '\\u2014'}); "
                "print(store.read_json(p)['t'] == 'a \\u2014 b')")
        p = Path(self.tmp.name) / "x.json"
        env = {**os.environ, "LC_ALL": "C", "PYTHONCOERCECLOCALE": "0", "PYTHONUTF8": "0"}
        r = subprocess.run([sys.executable, "-X", "utf8=0", "-c", code,
                            str(Path(__file__).resolve().parent.parent), str(p)],
                           capture_output=True, text=True, env=env)
        self.assertEqual(r.stdout.strip(), "True", r.stderr)
        self.assertEqual(json.loads(p.read_bytes())["t"], "a \u2014 b")

    def test_a_line_that_is_not_utf8_is_skipped_like_any_bad_line(self):
        # the audit: UnicodeDecodeError isn't a JSONDecodeError, so one such line crashed every reader
        p = Path(self.tmp.name) / "rows.jsonl"
        p.write_bytes(b'{"a": 1}\n{"b": "\xe9"}\n{"c": 3}\n')
        rows, end = store.read_jsonl(p)
        self.assertEqual(rows, [{"a": 1}, {"c": 3}])
        self.assertEqual(end, p.stat().st_size)
        p.write_bytes(b'{"b": "\xe9"}')
        self.assertEqual(store.read_json(p, "default"), "default")


def free_port() -> int:
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        return sk.getsockname()[1]


def fake_remote_ollama(tags: list[dict], shows: dict[str, dict]):
    """An Ollama on another machine (a free port here): /api/tags lists ``tags``, POST /api/show
    answers ``shows[name]``."""
    class H(BaseHTTPRequestHandler):
        def reply(self, body):
            self.send_response(200 if body is not None else 404)
            self.end_headers()
            self.wfile.write(json.dumps(body if body is not None else {"error": "no"}).encode())

        def do_GET(self):
            self.reply({"/api/version": {"version": "0.40.0"}, "/api/tags": {"models": tags},
                        "/api/ps": {"models": []}}.get(self.path))

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.reply(shows.get(body.get("model")) if self.path == "/api/show" else {})

        def log_message(self, *a):
            pass
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


class Remote(unittest.TestCase):
    """wb remote HOST: another machine's Ollama as this one's models (#114). The stub listens on
    loopback, so this machine's own OLLAMA_CONTEXT_LENGTH is kept out of it."""
    setUp, tearDown, server, config = Real.setUp, Real.tearDown, Real.server, Real.config

    def run(self, result=None):
        with mock.patch.object(models.hardware, "ollama_env", return_value={}):
            return super().run(result)

    def wb(self, *argv) -> tuple[int, str]:
        from workbench import cli
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with redirect_stdout(out), redirect_stderr(err):
            try:
                a = cli.parser().parse_args(list(argv))
                a.f(a)
            except SystemExit as e:
                code = e.code or 0
        return code, out.getvalue() + err.getvalue()

    def test_a_macs_own_lan_address_is_this_machine_without_ip(self):
        # the 2026-11-19 audit: macOS has no `ip`, so a provider at the Mac's own LAN address
        # counted as remote and its model was left out
        ifconfig = ("lo0: flags=8049<UP,LOOPBACK> mtu 16384\n\tinet 127.0.0.1 netmask 0xff000000\n"
                    "\tinet6 fe80::1%lo0 prefixlen 64 scopeid 0x1\n"
                    "en0: flags=8863<UP,BROADCAST> mtu 1500\n\tinet 192.168.1.23 netmask 0xffffff00 "
                    "broadcast 192.168.1.255\n\tinet6 fe80::1c2b:3d4e:5f60:7182%en0 prefixlen 64\n")

        def run(argv, **kw):
            if argv[0] == "ip":
                raise FileNotFoundError(argv[0])
            return subprocess.CompletedProcess(argv, 0, ifconfig if argv[0] == "ifconfig" else "", "")
        models._own_addresses.cache_clear()
        self.addCleanup(models._own_addresses.cache_clear)
        with mock.patch.object(models.subprocess, "run", run), \
                mock.patch.object(models.socket, "getaddrinfo", side_effect=OSError):
            self.assertTrue(models._this_host("192.168.1.23"))
            self.assertTrue(models._this_host("fe80::1c2b:3d4e:5f60:7182"))
            self.assertFalse(models._this_host("192.168.1.24"))

    def test_wb_remote_registers_the_tool_models_of_another_machines_ollama_and_remove_undoes_it(self):
        ctx = {"llama.context_length": 131072}
        srv, port = fake_remote_ollama(
            [{"name": "coder:7b"}, {"name": "embed:1b"}, {"name": "big:cloud", "remote_host": "https://ollama.com"}],
            {"coder:7b": {"capabilities": ["completion", "tools"], "model_info": ctx},
             "embed:1b": {"capabilities": ["embedding"], "model_info": ctx},
             "big:cloud": {"capabilities": ["tools"], "model_info": ctx}})
        self.servers.append(srv)
        self.config({}, {"keep/me": {"sizes": ["trivial"]}})
        code, out = self.wb("remote", "127.0.0.1", "--port", str(port), "--context", "32768")
        self.assertEqual(code, 0, out)
        pid = models.remote_pid("127.0.0.1")
        self.assertEqual(pid, "ollama-127-0-0-1")
        self.assertIn(f"registered {pid}/coder:7b: tools", out)
        self.assertNotIn("big:cloud", out)                               # runs on ollama.com, not there
        cfg = json.loads(Path(os.environ["WB_OPENCODE_CONFIG"]).read_text())
        prov = cfg["provider"][pid]
        self.assertEqual(prov["options"]["baseURL"], f"http://127.0.0.1:{port}/v1")
        self.assertEqual(list(prov["models"]), ["coder:7b"])            # no embedding, no cloud model
        entry = prov["models"]["coder:7b"]
        self.assertEqual(entry["limit"]["context"], 32768)
        self.assertIn("max_tokens", entry["options"])                    # an Ollama entry's output cap
        self.assertTrue(json.loads(Path(os.environ["WB_MODELS"]).read_text())[pid]["local"])
        self.assertIn(f"{pid}/coder:7b", models.discover())
        self.assertEqual(self.wb("remote", "127.0.0.1", "--port", str(port), "--context", "32768")[0], 0)
        code, out = self.wb("remote", "127.0.0.1", "--remove")
        self.assertEqual(code, 0, out)
        self.assertIn(f"removed {pid}", out)
        self.assertNotIn(pid, json.loads(Path(os.environ["WB_OPENCODE_CONFIG"]).read_text())["provider"])
        self.assertEqual(json.loads(Path(os.environ["WB_MODELS"]).read_text()),
                         {"keep/me": {"sizes": ["trivial"]}})            # only its own entries go

    def test_wb_remote_never_takes_over_or_deletes_a_provider_it_didnt_add(self):
        # #197 (the 2026-11-19 audit): the provider id comes from the host alone, so a provider of
        # that id the user wrote (or one for another port) was reported as the new endpoint's,
        # and --remove deleted it with its models and their settings
        srv, port = fake_remote_ollama([{"name": "coder:7b"}],
                                       {"coder:7b": {"capabilities": ["tools"], "model_info": {}}})
        self.servers.append(srv)
        pid = models.remote_pid("127.0.0.1")
        mine = {pid: {"name": "Mine", "options": {"baseURL": "http://127.0.0.1:9/v1"},
                      "models": {"x": {"limit": {"context": 8192}}}}}
        self.config(mine, {f"{pid}/x": {"sizes": ["trivial"]}})
        before = (Path(os.environ["WB_OPENCODE_CONFIG"]).read_text(),
                  Path(os.environ["WB_MODELS"]).read_text())
        code, out = self.wb("remote", "127.0.0.1", "--port", str(port), "--context", "32768")
        self.assertEqual(code, 1)
        self.assertIn("leaves it alone", out)
        code, out = self.wb("remote", "127.0.0.1", "--remove")
        self.assertEqual(code, 1)
        self.assertIn("isn't one wb remote added", out)
        self.assertEqual((Path(os.environ["WB_OPENCODE_CONFIG"]).read_text(),
                          Path(os.environ["WB_MODELS"]).read_text()), before)   # both untouched
        # one wb remote added before it marked its own (1.0): its name and host say so
        self.config({pid: {"name": "Ollama on 127.0.0.1", "options": {"baseURL": f"http://127.0.0.1:{port}/v1"},
                           "models": {"coder:7b": {}}}}, {pid: {"local": True}})
        self.assertEqual(self.wb("remote", "127.0.0.1", "--remove")[0], 0)

    def test_wb_remote_says_how_to_open_an_ollama_that_doesnt_answer_and_why_a_model_was_left_out(self):
        code, out = self.wb("remote", "127.0.0.1", "--port", str(free_port()))
        self.assertEqual(code, 1)
        self.assertIn("OLLAMA_HOST=0.0.0.0", out)
        self.assertFalse(Path(os.environ["WB_OPENCODE_CONFIG"]).exists())   # nothing written
        srv, port = fake_remote_ollama([{"name": "coder:7b"}],
                                       {"coder:7b": {"capabilities": ["tools"], "model_info": {}}})
        self.servers.append(srv)
        self.config({})
        code, out = self.wb("remote", "127.0.0.1", "--port", str(port))  # no --context: it can't say
        self.assertEqual(code, 0, out)
        self.assertIn("--  ollama-127-0-0-1/coder:7b: Ollama would serve coder:7b its own default context", out)
        self.assertEqual(self.wb("remote", "nowhere", "--remove")[0], 1)


class Optimize(unittest.TestCase):
    """wb optimize against an Ollama whose placement depends on the window: 10 GB of weights and
    32 KB a token of context, on a card that holds 12 GB (so up to 64K fits whole)."""
    WEIGHTS, PER_TOKEN, CARD = 10_000_000_000, 32_000, 12_100_000_000
    setUp, tearDown, config = Real.setUp, Real.tearDown, Real.config  # not Real's tests again

    def ollama(self, window, entry=None, loaded=None, extra=None):
        loaded, posts = loaded if loaded is not None else [], []
        test = self

        class H(BaseHTTPRequestHandler):
            def reply(self, body):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(body).encode())

            def do_GET(self):
                self.reply({"/api/version": {"version": "0.40.0"}, "/api/tags": {"models": [{"name": "m"}]},
                            "/api/ps": {"models": loaded}}.get(self.path) or {})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                posts.append((self.path, body))
                if self.path == "/api/show":
                    return self.reply({"model_info": {"x.context_length": 262144}, "capabilities": ["tools"]})
                if self.path == "/api/generate":
                    loaded.clear()
                    if body.get("keep_alive") != 0:
                        size = test.WEIGHTS + test.PER_TOKEN * body["options"]["num_ctx"]
                        loaded.append({"name": "m", "size": size, "size_vram": min(size, test.CARD)})
                self.reply({"status": "success"} if self.path == "/api/create" else {})

            def log_message(self, *a):
                pass
        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.servers.append(srv)
        self.config({"ollama": {"options": {"baseURL": f"http://127.0.0.1:{srv.server_address[1]}/v1"},
                                "models": {"m": entry or {"limit": {"context": window, "output": 4096},
                                                          "options": {"reasoningEffort": "none"}}}}}, extra)
        return posts

    def test_a_remote_ollama_is_measured_and_its_models_are_in_the_fingerprint_while_it_answers(self):
        # #115: wb remote's models fit another card, read by the same /api/ps placement
        from workbench import optimize
        loaded = [{"name": "theirs", "size": 5, "size_vram": 5}]           # the other machine's own
        self.ollama(32768, loaded=loaded, extra={"ollama": {"local": True}})
        srv = self.servers[-1]
        with mock.patch.object(models, "_this_host", lambda h: False), \
                mock.patch.object(optimize.hardware, "gpus", lambda windows=True: [{"name": "Card", "vram": 1}]):
            m, = optimize.candidates()
            self.assertTrue(m.remote)
            self.assertEqual(optimize.busy(m), ["theirs"])                  # not ours to unload
            loaded.clear()
            self.assertEqual(optimize.busy(m), [])
            fp = optimize.fingerprint()
            self.assertEqual(fp[-1], ["remote", m.root, [["m", None]]])
            self.assertEqual(optimize.changed(fp), "")
            self.assertEqual(optimize.changed(fp[:-1] + [["remote", m.root, [["m", 123]]]]),
                             f"the models at {m.root} changed")             # re-pulled: another fit
            self.assertEqual(optimize.changed(fp[:-1]), "")                 # a server new since
            srv.shutdown()
            srv.server_close()
            self.servers.remove(srv)
            self.assertEqual(optimize.changed(fp), "")                      # asleep: not due again
            self.assertEqual(optimize.remembered(fp), fp)                   # and still known

    def test_a_remote_model_a_task_is_using_is_never_unloaded_to_measure_it(self):
        # #198 (the 2026-11-19 audit): the run held this machine's lease only, and busy() leaves
        # out the model being measured, so it unloaded the model a task on that server was using
        from workbench import optimize
        posts = self.ollama(32768, extra={"ollama": {"local": True}})
        with mock.patch.object(models, "_this_host", lambda h: False), \
                mock.patch.object(optimize.hardware, "gpus", lambda windows=True: [{"name": "Card", "vram": 1}]), \
                mock.patch.object(sys, "argv", ["optimize", "--all"]):
            m, = optimize.candidates()
            task = subprocess.Popen(["sleep", "60"])                          # the task's runner
            self.addCleanup(task.kill)
            store.write_json(store.home() / f"{models.pool(m)}.json",
                             {str(task.pid): {"task": "t", "model": m.id, "start": models._started(task.pid)}})
            out = io.StringIO()
            with redirect_stdout(out):
                optimize.main()
            self.assertIn(f"{m.id}: not measured, a task is using {m.root}", out.getvalue())
            self.assertEqual([p for p in posts if p[0] == "/api/generate"], [])   # nothing unloaded
            self.assertIn(str(task.pid), models.holders(pool=models.pool(m)))      # its turn kept
            task.kill()
            task.wait()
            with redirect_stdout(io.StringIO()):
                optimize.main()                                                # free: measured
            self.assertTrue([p for p in posts if p[0] == "/api/generate"])
            self.assertIn(m.id, optimize.state()["models"])
            self.assertEqual(models.holders(pool=models.pool(m)), {})          # and let go

    def test_the_first_run_on_a_fresh_profile_starts(self):
        # #198 (the 2026-11-19 audit): optimize.log was opened before the state directory existed,
        # and wb setup swallowed the error, so the first measurement never ran
        from workbench import optimize
        self.ollama(32768)
        os.environ["WB_HOME"] = str(Path(os.environ["WB_HOME"]) / "fresh")
        with mock.patch.object(optimize.subprocess, "Popen") as popen:
            popen.return_value.pid = os.getpid()
            self.assertIn("wb optimize: measuring 1 model(s)", optimize.start()[0])
        self.assertTrue((store.home() / "optimize.log").exists())

    def test_a_window_that_spills_is_measured_down_to_the_largest_that_fits_whole(self):
        # a 64K window put a tenth of a 27B model in RAM and halved its writing speed (2026-11-18)
        self.ollama(98304)
        m = models.discover()["ollama/m"]
        from workbench import optimize
        row = optimize.fit(m, 262144)
        self.assertEqual((row["window"], row["fits"]), (98304, 65536))
        self.assertGreater(row["spill"], 0)
        self.assertLessEqual(len(row["tried"]), optimize.loads(m))
        self.assertIn("puts", optimize.advice(m.id, row))
        self.assertIn("64K fits all on the GPU", optimize.advice(m.id, row))

    def test_a_window_with_room_to_spare_is_measured_up_and_applied_with_its_settings(self):
        posts = self.ollama(32768)
        m = models.discover()["ollama/m"]
        from workbench import optimize
        optimize._record(m.id, optimize.fit(m, 262144))
        row = optimize.state()["models"][m.id]
        self.assertEqual((row["window"], row["fits"], row["spill"]), (32768, 65536, 0))
        self.assertIn("more than the 32K it serves", optimize.advice(m.id, row))
        out = optimize.apply(m)
        self.assertIn("registered ollama/m-64k", out)
        self.assertIn(("/api/create", {"model": "m-64k", "from": "m", "stream": False,
                                       "parameters": {"num_ctx": 65536}}), posts)
        entry = models.read_config()["provider"]["ollama"]["models"]["m-64k"]
        self.assertEqual(entry["options"], {"reasoningEffort": "none"})       # thinking stays off
        self.assertEqual(entry["limit"], {"context": 65536, "input": 65536 - 4096, "output": 4096})

    def test_nothing_to_change_says_nothing_and_a_task_on_the_gpu_keeps_it(self):
        self.ollama(65536)
        m = models.discover()["ollama/m"]
        from workbench import optimize
        row = optimize.fit(m, 65536)                   # trained on no more: nothing above it
        self.assertEqual((row["fits"], optimize.advice(m.id, row)), (65536, ""))
        with mock.patch.object(models, "holders", lambda *a: {"1": {"task": "t", "model": m.id}}):
            with self.assertRaisesRegex(RuntimeError, "real work first"):
                optimize.start()

    def test_it_is_due_when_never_run_the_gpu_changes_or_a_model_comes(self):
        # a run takes a minute or two, so it runs at setup and session start when due (the user,
        # 2026-11-18), quietly otherwise
        self.ollama(32768)
        from workbench import optimize
        gpu = [{"name": "Card A", "vram": 16e9}]
        with mock.patch.object(optimize.hardware, "gpus", lambda windows=True: gpu):
            self.assertEqual(optimize.due(), "never measured")
            m = models.discover()["ollama/m"]
            optimize._record(m.id, optimize.fit(m, 262144))
            store.write_json(optimize.path(), {**optimize.state(), "done": 1, "stopped": None,
                                               "hardware": optimize.fingerprint()})
            self.assertEqual(optimize.due(), "")
            with mock.patch.object(optimize.subprocess, "Popen") as popen:
                self.assertEqual(optimize.start(if_due=True), [])            # nothing to do: quiet
                popen.assert_not_called()
            optimize.apply(m)                                    # its own entry: measured already
            self.assertEqual(optimize.due(), "")
            gpu[0] = {"name": "Card B", "vram": 24e9}
            self.assertEqual(optimize.due(), "the GPU changed")
            with mock.patch.object(models, "holders", lambda *a: {"1": {"task": "t"}}):
                self.assertEqual(optimize.start(if_due=True), [])            # busy: next session
            with mock.patch.object(optimize.subprocess, "Popen") as popen:
                popen.return_value.pid = os.getpid()
                out = optimize.start(if_due=True)
            self.assertIn("measuring the GPU for 1 local model(s) (the GPU changed)", out[0])
            self.assertIn("a task you start goes first", out[0])
            self.assertNotIn("--all", popen.call_args[0][0])

    def test_real_work_stops_it_and_what_it_measured_stays(self):
        self.ollama(32768)
        m = models.discover()["ollama/m"]
        from workbench import cli, optimize
        held = {"123": {"task": optimize.HOLDER, "model": None, "start": None}}
        with mock.patch.object(cli, "_gpu_holders", lambda me=None, of=None: held), \
                mock.patch.object(optimize, "stop", lambda: 123) as stop:
            self.assertEqual(cli._preempt_bench(m, "t"),
                             "note: stopped wb optimize (what it measured is kept) to make room")


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
            self.assertEqual(r.returncode, 78, r.stderr)  # EX_CONFIG: the chat unit stops on it
            self.assertIn(f"{name}: needs Python 3.11 or newer", r.stderr)
            self.assertNotIn("Traceback", r.stderr)

    def test_native_windows_is_told_to_use_wsl2(self):
        r = self.launch("wb", "sys.platform = 'win32'")
        self.assertEqual(r.returncode, 78, r.stderr)
        self.assertIn("native Windows is not supported", r.stderr)
        self.assertIn("WSL2", r.stderr)

    def test_the_chat_unit_stops_on_a_broken_install_and_keeps_retrying_anything_else(self):
        # the 0.90 review: a start limit gave up for good on an address that came up late
        unit = (self.ROOT / "systemd" / "wb-chat.service").read_text()
        self.assertIn("StartLimitIntervalSec=0", unit)
        self.assertNotIn("StartLimitBurst", unit)
        self.assertIn("Restart=always", unit)
        prevent = [l.split("=", 1)[1].split() for l in unit.splitlines()
                   if l.startswith("RestartPreventExitStatus=")]
        self.assertEqual(prevent, [["78"]])  # never 75: chat.py exits 75 to be restarted

    def test_a_legacy_locale_still_reads_and_writes_utf8(self):
        # the 2026-11-19 audit: under a non-UTF-8 locale wb's reads and writes without an
        # encoding used that one, so a note with an accent was a traceback
        import subprocess
        with tempfile.TemporaryDirectory() as home:
            Path(home, "notes.md").write_text("- café: naïve ✓\n", encoding="utf-8")
            env = {**os.environ, "WB_HOME": home, "LC_ALL": "C", "PYTHONCOERCECLOCALE": "0",
                   "PYTHONUTF8": "0"}
            r = subprocess.run([sys.executable, str(self.ROOT / "bin" / "wb"), "notes"], env=env,
                               capture_output=True, timeout=30)
            self.assertEqual(r.returncode, 0, r.stderr.decode(errors="replace"))
            self.assertIn("café: naïve ✓", r.stdout.decode())

    def test_a_supported_python_runs_the_package(self):
        r = self.launch("wb", "pass")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.startswith("wb "))


class ServerAway(Base):
    """A model server that is only briefly away: the 2026-11-19 audit (finding 17). make_resident
    raised at once, so a task wb start --force queued "to wait for it" failed on its first check,
    and an Ollama restart between rounds or under a round failed the task for good."""
    def setUp(self):
        super().setUp()
        os.environ.pop("WB_FAKE_MODELS")
        self.down = Path(self.tmp.name) / "down"
        self.posts, self.tags = [], ["m"]
        srv, port = fake_ollama([], self.tags, self.posts, down=self.down)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        Path(os.environ["WB_OPENCODE_CONFIG"]).write_text(json.dumps({"provider": {"o": {
            "options": {"baseURL": f"http://127.0.0.1:{port}/v1"},
            "models": {"m": {"limit": {"context": 32768}, "options": {"max_tokens": 4096}}}}}}))
        # down, it can't say it is Ollama: models.json does
        Path(os.environ["WB_MODELS"]).write_text(json.dumps({"o": {"engine": "ollama"}}))

    def back_up(self, after: float, once_down: bool = False):
        """Bring the server back ``after`` seconds from now, or with ``once_down`` each time it
        goes down, ``after`` seconds later."""
        def run():
            if not once_down:
                time.sleep(after)
                return self.down.unlink(missing_ok=True)
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                if self.down.exists():
                    time.sleep(after)
                    self.down.unlink(missing_ok=True)
                time.sleep(0.1)
        threading.Thread(target=run, daemon=True).start()

    def waits(self, name):
        rows, _ = store.read_jsonl(store.Task(name).dir / "events.jsonl")
        return [r["text"] for r in rows if r.get("kind") == "load"
                and not r["text"].startswith("model reloaded")]   # a wait, or a round run again

    def test_a_task_forced_onto_a_server_that_is_down_waits_for_it_then_runs(self):
        self.down.touch()
        with self.assertRaises(SystemExit):      # wb start says --force queues it, to wait for it
            self.start("t-down", "ok", "--test", TEST_CMD)
        self.start("t-down", "ok", "--test", TEST_CMD, "--model", "o/m", "--force")
        deadline = time.monotonic() + 30
        while "waiting for" not in (store.Task("t-down").status.get("last") or "") \
                and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertNotIn("t-down", str(models.holders()))     # it holds no GPU while it waits
        self.back_up(1)
        st = wait_phase("t-down", {"review", "stuck", "failed"}, timeout=90)
        self.assertEqual(st["phase"], "review", st.get("last"))
        self.assertTrue(st["tests"]["ok"])
        self.assertIn("m", [p["model"] for p in self.posts])               # loaded once it was up
        self.assertTrue(self.waits("t-down"))

    def test_a_round_lost_with_its_server_runs_again_once_the_server_is_back(self):
        os.environ["FAKE_DOWN"] = str(self.down)                      # the round takes it down
        self.back_up(3, once_down=True)
        self.start("t-crash", "crash", "--test", TEST_CMD, "--model", "o/m")
        st = wait_phase("t-crash", {"review", "stuck", "failed"}, timeout=90)
        self.assertEqual(st["phase"], "review", st.get("last"))
        self.assertTrue(st["tests"]["ok"])                              # the round, run again
        self.assertTrue(Path(f"{self.down}.crashed").exists())
        self.assertTrue(self.waits("t-crash"))
        self.assertEqual(st["round"], 1)

    def test_a_server_gone_between_rounds_is_waited_for_and_the_model_loaded_again(self):
        # the tests take it down after round 1 (an Ollama restart while they ran)
        self.back_up(3, once_down=True)
        self.start("t-fix", "fix", "--model", "o/m", "--auto-fix", "1", "--no-self-review",
                   "--test", f"grep -qx 42 answer.txt || {{ touch {self.down}; exit 1; }}")
        st = wait_phase("t-fix", {"review", "stuck", "failed"}, timeout=90)
        self.assertEqual(st["phase"], "review", st.get("last"))
        self.assertTrue(st["tests"]["ok"])
        self.assertEqual(st["round"], 2)
        self.assertTrue(self.waits("t-fix"))
        self.assertEqual([p["model"] for p in self.posts if p.get("keep_alive") != 0], ["m", "m"])

    def test_a_crash_with_the_server_up_and_a_model_not_pulled_still_fail_at_once(self):
        self.start("t-crash", "crash", "--test", TEST_CMD, "--model", "o/m")
        st = wait_phase("t-crash", {"review", "stuck", "failed"}, timeout=30)
        self.assertEqual((st["phase"], st["last"]), ("failed", "opencode exited 1"))
        self.assertFalse(self.waits("t-crash"))
        self.tags.clear()                                            # a config error: no wait
        began = time.monotonic()
        self.start("t-gone", "ok", "--test", TEST_CMD, "--model", "o/m", "--force")
        st = wait_phase("t-gone", {"review", "stuck", "failed"}, timeout=30)
        self.assertEqual(st["phase"], "failed")
        self.assertIn("m is not pulled", st["last"])
        self.assertLess(time.monotonic() - began, 20)
        self.assertFalse(self.waits("t-gone"))


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

    def test_a_thinking_model_that_would_outlast_its_budget_on_this_machine_is_flagged(self):
        # a 64K entry left thinking with a 16K cap, spilled to 21 tok/s, sat silent 14 minutes
        # at a time (2026-11-18); the same settings on a fast card are fine
        m = models.resolve("mimo")
        entry = {"reasoning": True, "limit": {"context": 32768}, "options": {"max_tokens": 16384}}
        self.assertEqual(router.thinking_stall(m.id, entry), "")          # unmeasured: no claim
        router.record_speed(m.id, self.session(60, 2800))                  # fast: 16384/60 < 420 s
        self.assertEqual(router.thinking_stall(m.id, entry), "")
        router.record_speed(m.id, self.session(10, 2800))
        router.record_speed(m.id, self.session(10, 2800))                  # now the median is slow
        why = router.thinking_stall(m.id, entry)
        self.assertIn("16384-token output cap", why)
        self.assertIn('reasoningEffort to "none"', why)
        self.assertEqual(router.thinking_stall(m.id, {**entry, "options": {
            "max_tokens": 16384, "reasoningEffort": "none"}}), "")      # thinking off: fine
        self.assertEqual(router.thinking_stall(m.id, {**entry, "reasoning": False}), "")
        server = {"reasoning": True, "limit": {"context": 32768, "output": 16384}}  # not Ollama
        self.assertIn("16384-token output cap", router.thinking_stall(m.id, server))


if __name__ == "__main__":
    unittest.main()
