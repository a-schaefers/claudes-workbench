"""Machines other than the one workbench grew up on: other servers, ports, configs, platforms, speeds."""
import ctypes
import ctypes.util
import io
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import contextlib
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # run as a script too

from unittest import mock

from workbench import context, images, media, models, results, router, store, watch
from tests.test_workbench import Base, FAKE, TEST_CMD, ThreadingHTTPServer, run_wb, wait_phase  # listens at once (#950)


REAL_SLEPT = models.slept  # Real stands a machine that never sleeps in for it (#716)


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


def fake_lmstudio(loaded: dict, posts: list):
    """An LM Studio with just-in-time loading on, models a and b: /v1/models lists both,
    /api/v1/models their ``loaded`` instances (instance id -> context length); POST
    /api/v1/models/load and /unload are recorded in ``posts`` and load or unload."""
    class H(BaseHTTPRequestHandler):
        def reply(self, body):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def do_GET(self):
            if self.path == "/v1/models":
                return self.reply({"data": [{"id": "a"}, {"id": "b"}]})
            if self.path == "/api/v1/models":
                return self.reply({"models": [
                    {"key": k, "loaded_instances": [{"id": i, "config": {"context_length": c}}
                                                    for i, c in loaded.items() if i == k]}
                    for k in ("a", "b")]})
            self.send_response(404)
            self.end_headers()

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            posts.append((self.path, body))
            if self.path == "/api/v1/models/load":
                loaded[body["model"]] = body.get("context_length", 4096)
            elif self.path == "/api/v1/models/unload":
                loaded.pop(body["instance_id"], None)
            self.reply({})

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
                          WB_MODELS=str(tmp / "models.json"), WB_COMMANDS=str(tmp / "commands.json"),
                          # OpenCode's credential store (models.auth_key): the fake's, never the
                          # user's own, and no OpenCode server started for each provider (#1010)
                          WB_OPENCODE_BIN=FAKE)
        models._ENGINES.clear()
        models._AUTH.clear()
        models._EMULATORS.clear()
        models._DOWN.clear()
        models._KEYS.clear()
        models._STORED.clear()
        models._FACTS.clear()
        models._STOP_STAYS.clear()
        self.servers = []
        no_gpu = mock.patch.object(models.hardware, "gpus", lambda windows=True: [])  # this machine's
        no_gpu.start()  # would decide what fits; a test that wants a budget sets models.json gpu_gb
        self.addCleanup(no_gpu.stop)
        awake = mock.patch.object(models, "slept", lambda: 0.0)  # this machine's suspends would
        awake.start()  # excuse the past stamps the tests set (#716); a test that wants one sets it
        self.addCleanup(awake.stop)
        # the tests' stamps run minutes ahead while they scale BOX_QUIET to seconds: read with the
        # real keep-alive, every idle model would look mid-request (#899); the test of that sets it
        keep = mock.patch.object(models, "BOX_KEEP_ALIVE", 0)
        keep.start()
        self.addCleanup(keep.stop)

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

    def test_a_server_that_is_down_is_probed_once_a_minute_not_on_every_discover(self):
        # a wb remote box asleep cost 1.5 s on every discover(): wb models took 12 s (#337)
        dead = socket.socket()
        dead.bind(("127.0.0.1", 0))
        port = dead.getsockname()[1]
        dead.close()  # nothing listens there now
        self.config({"o": {"options": {"baseURL": f"http://127.0.0.1:{port}/v1"}, "models": {"m": {}}}})
        probes = []
        real = models._open
        def counting(target, timeout):
            url = target.full_url if hasattr(target, "full_url") else target
            if url.endswith("/api/version"):
                probes.append(url)
            return real(target, timeout)
        with mock.patch.object(models, "_open", counting):
            for _ in range(3):
                found = models.discover()
        self.assertEqual(len(probes), 1)
        self.assertEqual(found["o/m"].engine, "server")
        self.assertFalse(models.engine_known(f"http://127.0.0.1:{port}/v1", {}))
        later = time.monotonic() + models.DOWN_SECS              # a runner hours on: asked again
        with mock.patch.object(models, "_open", counting), \
                mock.patch.object(models.time, "monotonic", lambda: later):
            models.discover()
        self.assertEqual(len(probes), 2)

    def test_a_sleeping_remote_ollama_is_asked_once_per_pass_by_wb_models_and_routing(self):
        # #705: each model's status asked the box again into a 2.5 s timeout: wb models took 31 s
        # and each wb start 10 s for a remote of 4 models, though the task ran on a local one
        from workbench import cli
        dead = socket.socket()
        dead.bind(("127.0.0.1", 0))
        port = dead.getsockname()[1]
        dead.close()  # nothing listens there now
        here = self.server({"/v1/models": {"data": [{"id": "local"}]}})
        self.config({"box": {"options": {"baseURL": f"http://127.0.0.1:{port}/v1"},
                             "models": {n: {} for n in ("a", "b", "c", "d")}},
                     "s": {"options": {"baseURL": here + "/v1"}, "models": {"local": {}}}},
                    {"box": {"engine": "ollama"}, "s": {"engine": "server"}})
        asked = []
        real = models._open

        def counting(target, timeout):
            url = target.full_url if hasattr(target, "full_url") else target
            if f":{port}/" in url:
                asked.append(url)
            return real(target, timeout)
        with mock.patch.object(models, "_open", counting):
            self.assertEqual(router.choose("significant")[0].model.id, "s/local")
            self.assertEqual(len(asked), 1)
            asked.clear()
            with redirect_stdout(io.StringIO()):
                cli.main(["models"])
        self.assertEqual(len(asked), 1)

    def test_an_ollama_behind_a_key_is_probed_with_it_and_known_as_ollama(self):
        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                ok = self.headers.get("Authorization") == "Bearer sekrit"
                body = {"/api/version": {"version": "0.40.0"}, "/api/tags": {"models": []}}.get(self.path)
                data = json.dumps(body or {}).encode()
                self.send_response(200 if ok and body is not None else 401 if not ok else 404)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            def log_message(self, *a):
                pass
        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.servers.append(srv)
        url = f"http://127.0.0.1:{srv.server_address[1]}"
        self.config({"k": {"options": {"baseURL": url + "/v1", "apiKey": "sekrit"}, "models": {"m": {}}}})
        self.assertEqual(models.discover()["k/m"].engine, "ollama")

    def test_ollama_is_known_by_its_api_on_any_port(self):
        ollama = self.server({"/api/version": {"version": "0.40.0"}, "/api/tags": {"models": []}})
        other = self.server({"/v1/models": {"data": [{"id": "coder"}]}})
        self.config({"o": {"options": {"baseURL": ollama + "/v1"}, "models": {"m": {}}},
                     "s": {"options": {"baseURL": other + "/v1"}, "models": {"coder": {}}}})
        found = models.discover()
        self.assertEqual(found["o/m"].engine, "ollama")
        self.assertEqual(found["s/coder"].engine, "server")
        self.assertEqual(models.status(found["o/m"])["why"], "m is not pulled")

    def test_a_native_ollama_sdk_provider_with_an_api_base_is_ollama(self):
        # #585 (the audit of 1.90): only /v1 was stripped, so a baseURL ending in /api probed
        # /api/api/version, read as a server "not running, and no start command"
        posts = []
        srv, port = fake_ollama([], ["coder"], posts)
        self.servers.append(srv)
        self.config({"nat": {"npm": "ollama-ai-provider-v2",
                             "options": {"baseURL": f"http://127.0.0.1:{port}/api"}, "models": {"coder": {}}},
                     "oai": {"options": {"baseURL": f"http://localhost:{port}/v1"}, "models": {"coder": {}}}})
        found = models.discover()
        nat = found["nat/coder"]
        self.assertEqual((nat.engine, nat.root), ("ollama", f"http://127.0.0.1:{port}"))
        self.assertEqual(models.status(nat), {"available": True, "resident": False, "why": "pulled"})
        self.assertEqual(nat.server, found["oai/coder"].server)       # one Ollama, not two
        self.assertEqual(nat.openai_base, f"http://127.0.0.1:{port}/v1")
        self.assertEqual(models.providers()["nat"]["root"], f"http://127.0.0.1:{port}")
        self.config({"nat": {"npm": "ollama-ai-provider-v2", "options": {
            "baseURL": f"http://127.0.0.1:{port}/api"}, "models": {"coder": {}}}}, {"nat": {"engine": "ollama"}})
        self.assertTrue(models.status(models.discover()["nat/coder"])["available"])

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

    def test_llama_server_answers_to_any_name_and_a_strict_server_says_the_name_it_serves(self):
        # #256 (the audit of 1.2): only llama-server answers to any name; vLLM, SGLang and TGI
        # 404 a wrong one, so the round died at its first request as "opencode exited 1"
        llama = self.server({"/v1/models": {"data": [{"id": "/models/coder-q4.gguf", "owned_by": "llamacpp"}]}})
        meta = self.server({"/v1/models": {"data": [{"id": "/models/coder-q4.gguf", "meta": {"n_ctx_train": 1}}]}})
        vllm = self.server({"/v1/models": {"data": [{"id": "Org/Coder-7B-Instruct", "owned_by": "vllm"}]}})
        empty = self.server({"/v1/models": {"object": "list", "data": []}})
        self.config({"l": {"options": {"baseURL": llama + "/v1"}, "models": {"coder": {}}},
                     "m": {"options": {"baseURL": meta + "/v1"}, "models": {"coder": {}}},
                     "v": {"options": {"baseURL": vllm + "/v1"}, "models": {"coder-7b": {},
                                                                           "Org/Coder-7B-Instruct": {}}},
                     "e": {"options": {"baseURL": empty + "/v1"}, "models": {"x": {}}}})
        self.assertTrue(models.status(models.resolve("l/coder"))["available"])
        self.assertTrue(models.status(models.resolve("m/coder"))["available"])
        st = models.status(models.resolve("v/coder-7b"))
        self.assertEqual((st["available"], st["why"]), (False, "the server serves Org/Coder-7B-Instruct, "
                                                                "not coder-7b: set the entry's id to Org/Coder-7B-Instruct"))
        self.assertTrue(models.status(models.resolve("v/Org/Coder-7B-Instruct"))["available"])
        st = models.status(models.resolve("e/x"))                              # it read as resident
        self.assertEqual((st["available"], st["resident"]), (False, False))
        self.assertIn("lists no model", st["why"])

    def test_a_strict_server_takes_its_exact_id_not_one_ending_in_the_same_name(self):
        # #339 (the audit of 1.4): a vLLM serving studio/coder read as serving personal/coder,
        # a basename match meant for llama-server's file paths
        vllm = self.server({"/v1/models": {"data": [{"id": "studio/coder", "owned_by": "vllm"}]}})
        self.config({"v": {"options": {"baseURL": vllm + "/v1"},
                           "models": {"personal/coder": {}, "studio/coder": {}}}})
        st = models.status(models.resolve("v/personal/coder"))
        self.assertEqual((st["available"], st["why"]), (False, "the server serves studio/coder, "
                                                                "not personal/coder: set the entry's id to studio/coder"))
        self.assertTrue(models.status(models.resolve("v/studio/coder"))["available"])

    def test_a_keyed_ollama_is_probed_with_the_providers_key(self):
        # #256 (the audit of 1.2): it served the model to the key, but the native /api probes
        # sent none, so it read as unavailable
        key = lambda body: lambda h: body if h.get("Authorization") == "Bearer s3cret" else 401  # noqa
        ollama = self.server({"/api/version": key({"version": "0.40.0"}),
                              "/api/tags": key({"models": [{"name": "m:latest"}]}),
                              "/api/ps": key({"models": [{"name": "m:latest"}]})})
        self.config({"o": {"options": {"baseURL": ollama + "/v1", "apiKey": "s3cret"}, "models": {"m": {}}}},
                    {"o": {"engine": "ollama"}})
        self.assertEqual(models.status(models.resolve("o/m")),
                         {"available": True, "resident": True, "why": "loaded"})

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

    def per_gpu(self, loaded_a, loaded_b, cuda=("0", "1")):
        """Two fake Ollamas, one per card: a/* on GPU 0 (its OLLAMA_HOST on a's port), b/* on
        GPU 1, each `ollama serve` in a fake /proc with its CUDA_VISIBLE_DEVICES (None: unset)."""
        posts_a, posts_b = [], []
        srv_a, port_a = fake_ollama(loaded_a, [x["name"] for x in loaded_a] + ["x"], posts_a)
        srv_b, port_b = fake_ollama(loaded_b, [x["name"] for x in loaded_b], posts_b)
        self.servers += [srv_a, srv_b]
        self.config({"a": {"options": {"baseURL": f"http://127.0.0.1:{port_a}/v1"},
                           "models": {"x": {}, "chat": {}}},
                     "b": {"options": {"baseURL": f"http://127.0.0.1:{port_b}/v1"}, "models": {"y": {}}}},
                    {"a/x": {"vram_gb": 4}})
        proc = Path(self.tmp.name, "proc")
        for pid, port, dev in (("100", port_a, cuda[0]), ("200", port_b, cuda[1])):
            d = proc / pid
            (d / "ns").mkdir(parents=True)
            (d / "comm").write_text("ollama\n")
            (d / "cmdline").write_bytes(b"ollama\0serve\0")
            env = [f"OLLAMA_HOST=127.0.0.1:{port}"] + ([f"CUDA_VISIBLE_DEVICES={dev}"] if dev else [])
            (d / "environ").write_bytes("\0".join(env).encode() + b"\0")
            os.symlink("mnt:[1]", d / "ns" / "mnt")
        (proc / "self" / "ns").mkdir(parents=True)
        os.symlink("mnt:[1]", proc / "self" / "ns" / "mnt")
        cards = [{"name": "RTX 3090", "driver": "nvidia 560.94", "vram": 24 * 2 ** 30,
                  "discrete": True, "uuid": f"GPU-{i}{i}"} for i in (0, 1)]
        # a Linux box's: on macOS ps stands in for /proc (#804) and only unified GPUs count
        # (#711), so the nightly's macOS runner read neither server's cards (#950)
        for patch in (mock.patch.object(models.hardware, "PROC", proc),
                      mock.patch.object(models.hardware, "system", lambda: "linux"),
                      mock.patch.object(models.hardware, "_run", lambda *a, **k: ""),
                      mock.patch.object(models.hardware, "gpus", lambda windows=True: cards)):
            patch.start()
            self.addCleanup(patch.stop)
        return models.discover(), posts_a, posts_b

    def test_with_one_ollama_per_gpu_freeing_one_card_leaves_the_other_cards_models_loaded(self):
        # #460 (the audit of 1.8): free_gpu unloaded every Ollama's models, so a task on GPU 0's
        # server emptied GPU 1's too, where they took none of its memory
        gb = 10 ** 9
        on_a = [{"name": "chat", "size": 18 * gb, "size_vram": 18 * gb}]
        found, posts_a, posts_b = self.per_gpu(on_a, [{"name": "y", "size": 18 * gb, "size_vram": 18 * gb}])
        x, y = found["a/x"], found["b/y"]
        self.assertTrue(models.apart(x.root, y.root))
        self.assertFalse(models.apart(x.root, x.root))
        self.assertFalse(models._others_resident(found["a/chat"]))   # y is on the other card
        self.assertTrue(models._room_beside(x))     # 18 + 4 of GPU 0's 23.2: y's 18 aren't on it
        models.free_gpu(keep={"a/x"}, say=lambda m: None, of=x)
        self.assertEqual([p["model"] for p in posts_a], ["chat"])
        self.assertEqual(posts_b, [])
        # can_share counts what sits on its own card: 15 + 15 on two cards of 24, not 30 on one
        big = lambda m: models.Model(id=m.id, provider=m.provider, name=m.name,  # noqa: E731
                                     base_url=m.base_url, engine="ollama", label=m.id,
                                     extra={"vram_gb": 15})
        self.assertTrue(models.can_share(big(x), [big(y)], models.gpu_budget(x.root)))
        self.assertFalse(models.can_share(big(x), [big(found["a/chat"])], models.gpu_budget(x.root)))
        on_a.append({"name": "chat", "size": 18 * gb, "size_vram": 18 * gb})
        posts_a.clear()
        models.make_resident(big(x), say=lambda m: None)   # no room beside chat: GPU 0 is freed
        self.assertEqual([p["model"] for p in posts_a], ["chat", "x"])
        self.assertEqual(posts_b, [])
        models.free_gpu(say=lambda m: None)           # the whole GPU (wb image): every card
        self.assertEqual([p["model"] for p in posts_b], ["y"])

    def test_with_the_ollamas_cards_unknown_freeing_the_gpu_empties_every_server_as_before(self):
        # #460: devices not known for sure are taken to be shared, as every server was before
        gb = 10 ** 9
        found, posts_a, posts_b = self.per_gpu([{"name": "chat", "size": 18 * gb, "size_vram": 18 * gb}],
                                               [{"name": "y", "size": 18 * gb, "size_vram": 18 * gb}],
                                               cuda=("0", None))  # b's server sees both cards
        x, y = found["a/x"], found["b/y"]
        self.assertFalse(models.apart(x.root, y.root))
        self.assertFalse(models._room_beside(x))
        models.free_gpu(keep={"a/x"}, say=lambda m: None, of=x)
        self.assertEqual([p["model"] for p in posts_a + posts_b], ["chat", "y"])
        big = lambda m: models.Model(id=m.id, provider=m.provider, name=m.name,  # noqa: E731
                                     base_url=m.base_url, engine="ollama", label=m.id,
                                     extra={"vram_gb": 15})
        self.assertFalse(models.can_share(big(x), [big(y)], models.gpu_budget(x.root)))
        shutil.rmtree(Path(self.tmp.name, "proc", "200"))  # one process readable: not sure
        self.assertIsNone(models.server_devices(x.root))
        self.assertFalse(models.apart(x.root, y.root))      # unknown is never apart

    def test_an_engine_pinned_to_one_card_waits_for_and_frees_that_card_alone(self):
        # #752 (the 2026-11-21 audit, finding 99): with one Ollama per GPU, `wb image` refused
        # while a task ran on either card, then unloaded both cards' models, whichever card its
        # engine used (`env CUDA_VISIBLE_DEVICES=1 sd-cli …`)
        gb = 10 ** 9
        found, posts_a, posts_b = self.per_gpu([{"name": "chat", "size": 18 * gb, "size_vram": 18 * gb}],
                                               [{"name": "y", "size": 18 * gb, "size_vram": 18 * gb}])
        one, zero = frozenset({"GPU-11"}), frozenset({"GPU-00"})
        self.assertEqual(media.cards({"command": "sd-cli", "beside": "b/y"}), one)
        self.assertEqual(media.cards({"command": "sd-cli", "devices": "1"}), one)
        self.assertEqual(media.cards({"command": "sd-cli", "devices": 1}), one)
        self.assertEqual(media.cards({"command": "sd-cli", "devices": "GPU-11"}), one)
        for unknown in ({"devices": "7"}, {"beside": "nobody/x"}, {}):    # every card, as before
            self.assertIsNone(media.cards({"command": "sd-cli", **unknown}))
        self.assertIn("devices must", media.problem({"command": "sd-cli", "devices": True}))
        self.assertIn("beside must be a string", media.problem({"command": "sd-cli", "beside": 1}))
        task_a = {"1": {"model": "a/x", "task": "t-a"}}
        self.assertTrue(models.admits(None, task_a, one))            # beside a task on GPU 0
        self.assertFalse(models.admits(None, task_a, zero))          # on its card: it waits
        self.assertFalse(models.admits(None, task_a))                # not pinned: the whole GPU
        image = {"2": {"model": None, "task": "wb image", "cards": ["GPU-11"]}}
        self.assertTrue(models.admits(found["a/x"], image))          # a task on GPU 0 joins it
        self.assertFalse(models.admits(found["b/y"], image))         # one on its card waits
        self.assertFalse(models.admits(found["a/x"], {"3": {"model": None, "task": "wb image"}}))
        os.environ["WB_GPU_DIR"] = str(Path(self.tmp.name, "lease"))
        holder = subprocess.Popen(["sleep", "60"])
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        store.write_json(models.lease_dir() / "gpu.json", {str(holder.pid): {
            "model": "a/x", "task": "t-a", "since": 0, "start": models._started(holder.pid),
            "uid": os.getuid()}})
        said = []
        with models.GpuLease(None, "wb image", said.append, wait=False, cards=one):
            self.assertEqual(models.holders()[str(os.getpid())]["cards"], ["GPU-11"])
        self.assertEqual(said, ["sharing the GPU with a/x"])
        with self.assertRaisesRegex(RuntimeError, "in use by t-a: wb image needs its cards"):
            with models.GpuLease(None, "wb image", said.append, wait=False, cards=zero):
                pass
        with self.assertRaisesRegex(RuntimeError, "in use by t-a"):   # not pinned: as before
            with media.turns("wb image", said.append, False, {"command": "sd-cli"}):
                pass
        with media.turns("wb image", said.append, False, {"command": "sd-cli", "beside": "b/y"}):
            self.assertEqual(posts_a, [])                            # GPU 1's Ollama freed alone
            self.assertEqual([p["model"] for p in posts_b], ["y"])

    def test_ollama_for_windows_seen_from_wsl_is_freed_and_leased_as_this_machines_gpu(self):
        # #750 (the 2026-11-21 audit, finding 97): on WSL2 with NAT, a task on Ollama for Windows
        # (the gateway, `local: true`, so remote) took pool gpu-<winhost> and a command engine in
        # WSL pool gpu; neither waited for the other, free_gpu skipped the remote models, and both
        # ran on the one card. 127.0.0.1 stands for the Windows host here, localhost for WSL.
        for k in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
            os.environ.pop(k, None)
        gb = 10 ** 9
        posts_wsl, posts_win, on_wsl, on_win = [], [], [], []
        srv_a, port_a = fake_ollama(on_wsl, ["x"], posts_wsl)
        srv_b, port_b = fake_ollama(on_win, ["coder"], posts_win)
        self.servers += [srv_a, srv_b]
        providers = {"wsl": {"options": {"baseURL": f"http://localhost:{port_a}/v1"}, "models": {"x": {}}},
                     "win": {"options": {"baseURL": f"http://127.0.0.1:{port_b}/v1"}, "models": {"coder": {}}}}
        here = mock.patch.object(models, "_this_host", lambda h: h == "localhost")
        here.start()
        self.addCleanup(here.stop)
        engine = {"url": "http://127.0.0.1:30000/v1/images/generations"}
        for say, gateway in ((None, "127.0.0.1"), (True, None), (False, "127.0.0.1")):
            self.config(providers, {"win": {"local": True, **({} if say is None else {"same_gpu": say})}})
            posts_wsl.clear(), posts_win.clear()
            on_wsl[:] = [{"name": "x", "size": 8 * gb, "size_vram": 8 * gb}]
            on_win[:] = [{"name": "coder", "size": 14 * gb, "size_vram": 14 * gb}]
            with mock.patch.object(models, "windows_host", lambda: gateway):
                win = models.discover()["win/coder"]
                self.assertTrue(win.remote)
                shared = say is not False
                self.assertEqual(models.same_gpu(win), shared)
                self.assertEqual(models.pool(win), "gpu" if shared else f"gpu-127-0-0-1-{port_b}")
                self.assertEqual(models.releasable(win), shared)
                self.assertEqual(media.on_gpu(engine), shared)             # its turn is this GPU's
                self.assertEqual(media.boxes(engine), [] if shared else [win])
                models.free_gpu(say=lambda m: None)
                self.assertEqual([p["model"] for p in posts_wsl], ["x"])
                self.assertEqual([p["model"] for p in posts_win], ["coder"] if shared else [])
        self.assertFalse(models.same_gpu(models.discover()["wsl/x"]))      # a local model: never remote

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

    def test_a_shared_task_counts_the_kept_models_already_loaded_and_sees_one_evicted(self):
        # #803: keep_loaded's embedding (1 GB) and autocomplete (5 GB) models loaded, x (10 GB)
        # running: y (10 GB) was let in to share, and loading it made Ollama evict a kept model
        gb = 10 ** 9
        loaded = [{"name": "emb", "size": gb, "size_vram": gb},
                  {"name": "auto", "size": 5 * gb, "size_vram": 5 * gb}]
        self.own(loaded, ["x", "y", "emb", "auto"])
        kept = lambda *ids: Path(os.environ["WB_MODELS"]).write_text(json.dumps(  # noqa: E731
            {"a/x": {"vram_gb": 10}, "b/y": {"vram_gb": 10}, "keep_loaded": list(ids)}))
        env = lambda n: mock.patch.object(models.hardware, "ollama_env",  # noqa: E731
                                          lambda: {"OLLAMA_MAX_LOADED_MODELS": n})
        kept()
        x, y = models.discover()["a/x"], models.discover()["b/y"]
        with env("9"):
            self.assertTrue(models.can_share(y, [x], int(24e9)))     # 20 of 21.6: nothing kept
            kept("emb", "auto")
            self.assertFalse(models.can_share(y, [x], int(24e9)))    # 26 of 21.6
            self.assertTrue(models.can_share(y, [x], int(30e9)))     # 26 of 27
            kept("emb", "auto", "a/x")                                # x is the task's: counted once
            self.assertTrue(models.can_share(y, [x], int(30e9)))
        with env("3"):
            kept("emb", "auto")
            self.assertFalse(models.can_share(y, [x], int(99e9)))    # x, emb, auto and y: 4 of 3
            kept("emb")
            self.assertTrue(models.can_share(y, [x], int(99e9)))
        loaded[:] = [{"name": n, "size": 10, "size_vram": 10} for n in ("x", "y", "auto")]
        self.assertFalse(models._fits_beside(y, {"a/x"}, {"x", "emb", "auto"}, {"emb", "auto"}))  # emb went
        self.assertTrue(models._fits_beside(y, {"a/x"}, {"x", "auto"}, {"auto"}))
        # and the lease sees it: the load evicts emb, so y waits for the GPU alone
        os.environ["WB_GPU_DIR"] = str(Path(self.tmp.name) / "lease")
        runner_x = subprocess.Popen(["sleep", "60"])  # x's task's runner, holding a/x
        self.addCleanup(runner_x.kill)
        store.write_json(models.lease_dir() / "gpu.json", {str(runner_x.pid): {"model": "a/x", "task": "t-x"}})
        loaded[:] = [{"name": n, "size": 10, "size_vram": 10} for n in ("x", "emb")]
        def evicting(m, say=print, keep=(), warn=None):
            loaded[:] = [r for r in loaded if r["name"] != "emb"] + [{"name": m.name, "size": 10, "size_vram": 10}]
            return 1.0
        Path(os.environ["WB_MODELS"]).write_text(json.dumps(
            {"a/x": {"vram_gb": 10}, "b/y": {"vram_gb": 10}, "keep_loaded": ["emb"], "gpu_gb": 99}))
        said, alone = [], []
        with env("9"), mock.patch.object(models, "make_resident", evicting), \
                mock.patch.object(models.GpuLease, "_take_alone", lambda self, held: alone.append(1)):
            with models.GpuLease(models.discover()["b/y"], "t-y", said.append) as lease:
                self.assertEqual(alone, [])                          # let in to share
                lease.load()
        self.assertEqual(alone, [1])
        self.assertIn("ollama could not fit b/y beside a/x, emb", said[-1])

    def test_a_model_that_fits_beside_the_loaded_ones_unloads_nothing_and_kept_ones_stay(self):
        # the 2026-11-19 audit: every round unloaded every other Ollama model, the user's Open
        # WebUI or autocomplete model and an embedding model the project's tests use, fits or not
        gb = 10 ** 9
        loaded = [{"name": "emb", "size": gb, "size_vram": gb}, {"name": "chat", "size": 2 * gb, "size_vram": 2 * gb}]
        found, posts = self.own(loaded, ["x", "chat", "emb"])
        over = lambda **kw: Path(os.environ["WB_MODELS"]).write_text(  # noqa: E731
            json.dumps({"a/x": {"vram_gb": 4}, **kw}))  # read at discovery: rediscovered below
        x = lambda: models.discover()["a/x"]  # noqa: E731
        over(gpu_gb=20)                       # 3 + 4 of 18 usable: room for x beside both
        with mock.patch.object(models.hardware, "ollama_env", lambda: {"OLLAMA_MAX_LOADED_MODELS": "2"}):
            self.assertFalse(models._room_beside(x()))   # but Ollama keeps two: it would swap
        models.make_resident(x(), say=lambda m: None)
        self.assertEqual(posts, [{"model": "x", "prompt": "", "keep_alive": "30m"}])
        models._unload(x())
        posts.clear()
        over(gpu_gb=6, keep_loaded=["emb"])   # 3 + 4 of 5.4: chat goes, emb is kept
        models.make_resident(x(), say=lambda m: None)
        self.assertEqual([(p["model"], p.get("keep_alive")) for p in posts], [("chat", 0), ("x", "30m")])
        posts.clear()
        over(gpu_gb=1, keep_loaded=["b/emb"])  # no room, but only a kept model beside x (by id:
        # the provider must be one the config has, or it's a namespaced Ollama name, #461)
        self.assertEqual(models.make_resident(x(), say=lambda m: None), 0.0)
        models.free_gpu(keep={"a/x"}, say=lambda m: None)
        self.assertEqual(posts, [])
        next(r for r in loaded if r["name"] == "x")["size_vram"] = 5  # x partly on the CPU
        over(gpu_gb=20)
        models.make_resident(x(), say=lambda m: None)
        self.assertEqual([(p["model"], p.get("keep_alive")) for p in posts],
                         [("emb", 0), ("x", 0), ("x", "30m")])  # room is made, x placed again (#703)
        posts.clear()
        over()                                # no budget known: the GPU is freed, as before
        loaded.append({"name": "chat", "size": gb, "size_vram": gb})
        models._unload(x())
        posts.clear()
        models.make_resident(x(), say=lambda m: None)
        self.assertEqual([p["model"] for p in posts], ["chat", "x"])

    def test_a_model_loaded_partly_on_the_cpu_is_placed_again_once_it_fits_and_only_once(self):
        # #703: loaded while something else held the VRAM, x stayed 30% on the CPU for the whole
        # task: resident with nothing else beside it read as placed, and a generate reuses the
        # loaded runner as it is. Now it is unloaded and loaded again, once
        gb = 10 ** 9
        loaded = [{"name": "x", "size": 10 * gb, "size_vram": 7 * gb}]
        found, posts = self.own(loaded, ["x"])
        over = lambda **kw: Path(os.environ["WB_MODELS"]).write_text(json.dumps(kw))  # noqa: E731
        x = lambda: models.discover()["a/x"]  # noqa: E731
        models._RESPREAD.clear()
        self.addCleanup(models._RESPREAD.clear)
        over(gpu_gb=8)                         # 10 GB never fits 8: left as it is, no reload
        self.assertEqual(models.make_resident(x(), say=lambda m: None), 0.0)
        self.assertEqual(posts, [])
        over(gpu_gb=12)
        models.make_resident(x(), say=lambda m: None, warn=lambda m: None)
        self.assertEqual([(p["model"], p.get("keep_alive")) for p in posts], [("x", 0), ("x", "30m")])
        posts.clear()
        loaded[:] = [{"name": "x", "size": 10 * gb, "size_vram": 7 * gb}]   # split again all the same
        self.assertEqual(models.make_resident(x(), say=lambda m: None), 0.0)  # not every round
        self.assertEqual(posts, [])

    def test_free_gpu_waits_for_a_stopped_servers_vram_before_ollama_loads(self):
        # #703: pkill llama-server returns before its memory is freed, and Ollama loading at once
        # put part of the model on the CPU
        flag = Path(self.tmp.name) / "stopped"
        srv = self.server({"/v1/models": lambda h: 503 if flag.exists() else {"data": [{"id": "m"}]}})
        self.config({"s": {"options": {"baseURL": srv + "/v1"}, "models": {"m": {}}}},
                    {"s": {"stop": f"touch '{flag}'"}})
        used = iter([8 * 2 ** 30, 5 * 2 ** 30, 2 * 2 ** 30, 2 * 2 ** 30])
        reads = []
        with mock.patch.object(models.hardware, "gpus", lambda windows=True: reads.append(1) or [
                {"name": "card", "vram": 16 * 2 ** 30, "vram_used": next(used)}]), \
                mock.patch.object(models, "time", mock.Mock(wraps=time, sleep=lambda s: None)):
            models.free_gpu(say=lambda m: None)
        self.assertEqual(len(reads), 4)        # until the used VRAM stopped falling

    def test_free_gpu_waits_for_a_stopped_server_to_go_before_reading_its_vram_as_released(self):
        # #898: a stop that returns at once (pkill vllm) left the server holding its VRAM; two
        # equal reads a second apart read as "stopped falling", and Ollama loaded into the card
        # still taken, part of its model on the CPU for round 1
        flag, answered = Path(self.tmp.name) / "stopped", []

        def catalog(h):  # the server lets go three looks after the stop returned
            if flag.exists():
                answered.append(1)
                if len(answered) > 3:
                    return 503
            return {"data": [{"id": "m"}]}
        srv = self.server({"/v1/models": catalog})
        self.config({"s": {"options": {"baseURL": srv + "/v1"}, "models": {"m": {}}}},
                    {"s": {"stop": f"touch '{flag}'"}})
        seen = []
        with mock.patch.object(models.hardware, "gpus", lambda windows=True: seen.append(len(answered)) or [
                {"name": "card", "vram": 16 * 2 ** 30,
                 "vram_used": (2 if len(answered) > 3 else 8) * 2 ** 30}]), \
                mock.patch.object(models, "time", mock.Mock(wraps=time, sleep=lambda s: None)):
            models.free_gpu(say=lambda m: None)
        self.assertGreater(len(answered), 3)               # waited until it stopped answering
        self.assertEqual(seen[0], 0)                       # the cards read before the stop: held
        self.assertTrue(seen[-1] > 3)                      # and read as released only after

    def test_a_stop_that_unloads_and_leaves_its_server_answering_is_not_waited_on_for_30_s(self):
        # reviewing #898 (2026-11-21): free_gpu waited, up to STOP_SETTLE, for a stopped server to
        # stop answering; a stop that unloads (an unload endpoint, `lms unload`) leaves it up and
        # listing its model, so every free_gpu paid 30 s, and again each round it was stopped.
        # Now its fall ends the wait, and once it has answered all of STOP_SETTLE with nothing to
        # give back, it isn't waited on again in this process (the second review, 2026-11-21)
        flag = Path(self.tmp.name) / "unloaded"
        srv = self.server({"/v1/models": {"data": [{"id": "m"}]}})       # it answers throughout
        self.config({"s": {"options": {"baseURL": srv + "/v1"}, "models": {"m": {}}}},
                    {"s": {"stop": f"touch '{flag}'"}})
        took = []
        with mock.patch.object(models.hardware, "gpus", lambda windows=True: [
                {"name": "card", "vram": 16 * 2 ** 30, "vram_used": (2 if flag.exists() else 8) * 2 ** 30}]), \
                mock.patch.object(models, "STOP_SETTLE", 3):
            for _ in range(3):  # the second and third: stopped again with nothing left to give back
                t0 = time.monotonic()
                models.free_gpu(say=lambda m: None)
                took.append(time.monotonic() - t0)
        self.assertTrue(flag.exists())
        self.assertLess(took[0], 2.5, took)            # its VRAM fell: no wait for it to go
        self.assertGreaterEqual(took[1], 2.5, took)    # nothing fell: it could be #898's server
        self.assertLess(took[2], 2.5, took)            # seen staying up: not waited on again
        self.assertEqual(len(models._STOP_STAYS), 1)    # that server, keyed by its address

    def test_free_gpu_waits_for_every_stopped_server_not_the_first_ones_release(self):
        # the second review of #898 (2026-11-21): the cards read before the first stop, the first
        # server's release ended the wait while the second still held its 10 GB, and two equal
        # reads then read as released: Ollama loaded beside it and split its model again. A
        # server still answering as it let go was recorded as one that stays up, and its later
        # stops were waited on only through the VRAM
        flags = {k: Path(self.tmp.name) / f"stopped-{k}" for k in ("s1", "s2")}
        looks: list = []

        def catalog(k, after):  # s1 goes at once; s2 answers `after` looks past its stop
            def page(h):
                if flags[k].exists():
                    looks.append(k)
                    if looks.count(k) > after:
                        return 503
                return {"data": [{"id": "m"}]}
            return page
        gone = lambda k, after: flags[k].exists() and looks.count(k) > after  # noqa: E731
        s1, s2 = (self.server({"/v1/models": catalog(k, n)}) for k, n in (("s1", 0), ("s2", 3)))
        self.config({"s1": {"options": {"baseURL": s1 + "/v1"}, "models": {"m": {}}},
                     "s2": {"options": {"baseURL": s2 + "/v1"}, "models": {"m": {}}}},
                    {k: {"stop": f"touch '{f}'"} for k, f in flags.items()})
        seen = []

        def gpus(windows=True):
            used = (0 if gone("s1", 0) else 8) + (0 if gone("s2", 3) else 10)
            seen.append(used)
            return [{"name": "card", "vram": 24 * 2 ** 30, "vram_used": used * 2 ** 30}]
        with mock.patch.object(models.hardware, "gpus", gpus), \
                mock.patch.object(models, "time", mock.Mock(wraps=time, sleep=lambda s: None)):
            models.free_gpu(say=lambda m: None)
        self.assertTrue(all(f.exists() for f in flags.values()))
        self.assertEqual(seen[-1], 0, seen)                # released only once both had let go
        self.assertEqual(models._STOP_STAYS, set())        # both went: neither stays up
        # one pkill server, its VRAM already falling while it answers a few looks more
        for f in flags.values():
            f.unlink()
        looks.clear()
        self.config({"s2": {"options": {"baseURL": s2 + "/v1"}, "models": {"m": {}}}},
                    {"s2": {"stop": f"touch '{flags['s2']}'"}})

        def falling(windows=True):
            return [{"name": "card", "vram": 24 * 2 ** 30,
                     "vram_used": (2 if flags["s2"].exists() else 10) * 2 ** 30}]
        with mock.patch.object(models.hardware, "gpus", falling), \
                mock.patch.object(models, "time", mock.Mock(wraps=time, sleep=lambda s: None)):
            models.free_gpu(say=lambda m: None)
        self.assertEqual(models._STOP_STAYS, set())        # it went in the end: its stop ends it

    def test_a_llama_server_or_vllm_answering_a_request_is_not_idle(self):
        # #465 (the audit of 1.8): only a JSON /metrics `live.state` was read; llama-server and
        # vLLM serve Prometheus text, read as idle, so free_gpu stopped one mid-reply
        pages = {}
        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                body = pages.get(self.path)
                self.send_response(200 if body is not None else 404)
                self.end_headers()
                self.wfile.write((body if isinstance(body, str) else json.dumps(body or {})).encode())
            def log_message(self, *a):
                pass
        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.servers.append(srv)
        m = models.Model(id="s/m", provider="s", name="m", engine="server", label="s/m",
                         base_url=f"http://127.0.0.1:{srv.server_address[1]}/v1")
        prom = lambda name, n: (f"# HELP {name} running\n# TYPE {name} gauge\n"  # noqa: E731
                                f'{name}{{model_name="m"}} {n}\n')
        cases = [({"/slots": [{"id": 0, "is_processing": False}, {"id": 1, "is_processing": True}]}, False),
                 ({"/slots": [{"id": 0, "is_processing": False}]}, True),
                 ({"/metrics": prom("llamacpp:requests_processing", "1")}, False),
                 ({"/metrics": prom("vllm:num_requests_running", "2.0")}, False),
                 ({"/metrics": prom("vllm:num_requests_running", "0.0")}, True),
                 ({"/metrics": {"live": {"state": "generating"}}}, False),
                 ({}, True)]                                   # says nothing: idle, as before
        # #715: SGLang's and TGI's own /metrics text, as they print it, answering one request
        sglang = ('# HELP sglang:num_running_reqs The number of running requests.\n'
                  '# TYPE sglang:num_running_reqs gauge\n'
                  'sglang:num_running_reqs{{model_name="Qwen/Qwen2.5-Coder-7B-Instruct"}} {run}\n'
                  '# HELP sglang:num_queue_reqs The number of requests in the waiting queue.\n'
                  '# TYPE sglang:num_queue_reqs gauge\n'
                  'sglang:num_queue_reqs{{model_name="Qwen/Qwen2.5-Coder-7B-Instruct"}} {queue}\n'
                  '# TYPE sglang:gen_throughput gauge\n'
                  'sglang:gen_throughput{{model_name="Qwen/Qwen2.5-Coder-7B-Instruct"}} 41.2\n')
        tgi = ('# TYPE tgi_batch_current_size gauge\ntgi_batch_current_size {batch}\n'
               '# TYPE tgi_queue_size gauge\ntgi_queue_size {queue}\n'
               '# TYPE tgi_request_count counter\ntgi_request_count 17\n')
        cases += [({"/metrics": sglang.format(run="1.0", queue="0.0")}, False),
                  ({"/metrics": sglang.format(run="0.0", queue="2.0")}, False),
                  ({"/metrics": sglang.format(run="0.0", queue="0.0")}, True),
                  ({"/metrics": tgi.format(batch="1", queue="0")}, False),
                  ({"/metrics": tgi.format(batch="0", queue="3")}, False),
                  ({"/metrics": tgi.format(batch="0", queue="0")}, True)]
        for page, idle in cases:
            pages.clear()
            pages.update(page)
            self.assertEqual(models._server_idle(m), idle, page)

    def test_keep_loaded_keeps_namespaced_ollama_names_and_only_them(self):
        # #461 (the audit of 1.8, reproduced): cut at the first `/`, a kept hf.co/... name never
        # matched itself, mannix/llama3:8b kept llama3:8b, and acme/coder missed acme/coder:latest
        hf = "hf.co/nomic-ai/nomic-embed-text-v1.5-GGUF:Q4_K_M"
        loaded = [{"name": n, "size": 10, "size_vram": 10}
                  for n in (hf, "llama3:8b", "mannix/llama3:8b", "acme/coder:latest", "big:1b")]
        found, posts = self.own(loaded, ["x"])
        cfg = json.loads(Path(os.environ["WB_OPENCODE_CONFIG"]).read_text())
        cfg["provider"]["a"]["models"]["emb"] = {"id": "big:1b"}       # an alias, by its id
        Path(os.environ["WB_OPENCODE_CONFIG"]).write_text(json.dumps(cfg))
        Path(os.environ["WB_MODELS"]).write_text(json.dumps(
            {"keep_loaded": [hf, "mannix/llama3:8b", "acme/coder", "a/emb"]}))
        self.assertTrue(models._kept_name(hf, models.keep_loaded()))
        self.assertFalse(models._kept_name("llama3:8b", models.keep_loaded()))
        with mock.patch.object(models, "INFLIGHT_WATCH", 0):
            models.free_gpu(say=lambda m: None)
        self.assertEqual([p["model"] for p in posts], ["llama3:8b"])   # only the one not kept

    def test_a_model_answering_someone_else_is_left_until_it_goes_idle_then_unloaded(self):
        # #378 (the audit of 1.6): on a system Ollama two accounts share, each one's runner
        # unloaded the other's model before every round, mid-use, and both reloaded on every request
        at = lambda s: time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() + s)) + ".123456789Z"  # noqa
        theirs = {"name": "theirs", "size": 10, "size_vram": 10, "expires_at": at(300)}
        idle = {"name": "idle", "size": 10, "size_vram": 10, "expires_at": at(200)}
        found, posts = self.own([theirs, idle], ["x"])
        self.assertEqual(models.unheld_loaded(), ["theirs", "idle"])   # what wb doctor names
        Path(os.environ["WB_MODELS"]).write_text(json.dumps({"keep_loaded": ["idle"]}))
        self.assertEqual(models.unheld_loaded(), ["theirs"])
        Path(os.environ["WB_MODELS"]).write_text("{}")
        done = threading.Event()

        def requests_finishing():  # another user's agent: a request ends every half second
            for i in range(5):
                time.sleep(0.5)
                theirs["expires_at"] = at(300 + i)
            done.set()
        threading.Thread(target=requests_finishing, daemon=True).start()
        warned = []
        with mock.patch.object(models, "INFLIGHT_WATCH", 1.5), mock.patch.object(models, "INFLIGHT_WAIT", 60):
            models.free_gpu(keep={"a/x"}, say=lambda m: None, warn=warned.append)
            self.assertTrue(done.is_set())                    # it waited out their requests
            self.assertEqual(len(warned), 1)
            self.assertIn("theirs on ollama is answering requests that aren't this account's tasks", warned[0])
            self.assertEqual(sorted(p["model"] for p in posts), ["idle", "theirs"])  # then unloaded
            warned.clear()
            _, posts = self.own([idle], [])                    # idle: watched, unloaded, no word
            models.free_gpu(keep={"a/x"}, say=lambda m: None, warn=warned.append)
            self.assertEqual((warned, [p["model"] for p in posts]), ([], ["idle"]))
        stuck = {"name": "long", "size": 10, "size_vram": 10, "expires_at": at(-60)}  # a request
        found, posts = self.own([stuck], [])     # running past the keep-alive: its clock stopped
        with mock.patch.object(models, "INFLIGHT_WATCH", 0.5), mock.patch.object(models, "INFLIGHT_WAIT", 1):
            models.free_gpu(say=lambda m: None, warn=warned.append)
        self.assertIn("long on ollama was still in use after", warned[-1])
        self.assertEqual([p["model"] for p in posts], ["long"])   # the GPU is the task's after all
        self.assertFalse(models._in_use({"r": []}))            # nothing to watch: no wait at all

    def test_an_idle_models_stamp_left_past_by_a_suspend_is_not_taken_for_a_request(self):
        # #716: Ollama's unload timer stops in a suspend while its wall-clock expires_at doesn't,
        # so after the lid opened an idle model read as answering someone and the next task
        # waited for nothing behind a warning
        at = lambda s: time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() + s)) + ".1Z"  # noqa
        idle = {"name": "idle", "size": 10, "size_vram": 10, "expires_at": at(-600)}
        found, posts = self.own([idle], [])
        warned = []
        with mock.patch.object(models, "slept", lambda: 3600.0), \
                mock.patch.object(models, "INFLIGHT_WATCH", 0.3), mock.patch.object(models, "INFLIGHT_WAIT", 5):
            t0 = time.monotonic()
            models.free_gpu(say=lambda m: None, warn=warned.append)
        self.assertLess(time.monotonic() - t0, 3)
        self.assertEqual((warned, [p["model"] for p in posts]), ([], ["idle"]))
        self.assertTrue(models._past(at(-600)))                # never asleep: a request past it
        with mock.patch.object(models, "slept", lambda: 60.0):
            self.assertTrue(models._past(at(-600)))            # later than any sleep explains
        clocks = {getattr(time, "CLOCK_BOOTTIME", -1): 5000.0, time.CLOCK_MONOTONIC: 1400.0}
        with mock.patch.object(models.hardware, "system", lambda: "linux"), \
                mock.patch.object(models.time, "clock_gettime", lambda c: clocks.get(c, 0.0)):
            if hasattr(time, "CLOCK_BOOTTIME"):
                self.assertEqual(REAL_SLEPT(), 3600.0)            # BOOTTIME counts the suspends

    def test_waiting_for_someone_elses_request_asks_ollama_at_most_once_a_second(self):
        # #459 (the audit of 1.8, reproduced): a stamp already past is busy at once, so each
        # watch returned without a sleep and the wait asked /api/ps back to back, 1,137 times
        at = lambda s: time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() + s)) + ".1Z"  # noqa
        long = {"name": "long", "size": 10, "size_vram": 10, "expires_at": at(-60)}
        found, _ = self.own([long], [])
        root = found["a/x"].root
        asked = []
        real = models._get
        def counting(url, timeout=2.5):
            if url.endswith("/api/ps"):
                asked.append(time.monotonic())
            return real(url, timeout)
        def per_second():
            return len(asked) / max(asked[-1] - asked[0], 1)
        with mock.patch.object(models, "_get", counting), \
                mock.patch.object(models, "INFLIGHT_WATCH", 0.25), \
                mock.patch.object(models, "INFLIGHT_WAIT", 3):
            models._wait_unused({root: ["long"]}, warn=lambda m: None)        # busy throughout
            self.assertLessEqual(per_second(), 2)
            asked.clear()
            threading.Timer(1.2, lambda: long.update(expires_at=at(300))).start()  # goes idle
            began = time.monotonic()
            models._wait_unused({root: ["long"]}, warn=lambda m: None)
            self.assertLess(time.monotonic() - began, 2.9)     # it saw the quiet and stopped
            self.assertLessEqual(len(asked), 8)
            asked.clear()
            long["expires_at"] = at(-60)
            srv = self.servers.pop()
            threading.Timer(0.5, lambda: (srv.shutdown(), srv.server_close())).start()  # goes away
            models._wait_unused({root: ["long"]}, warn=lambda m: None)
            self.assertLessEqual(per_second(), 2)

    def test_a_server_the_user_keeps_loaded_is_never_stopped(self):
        srv = self.server({"/v1/models": {"data": [{"id": "m"}]}})
        stopped = Path(self.tmp.name) / "stopped"
        self.config({"s": {"options": {"baseURL": srv + "/v1"}, "models": {"m": {}}}},
                    {"s": {"stop": f"touch {stopped}"}, "keep_loaded": ["s/m"]})
        models.free_gpu(say=lambda m: None)
        self.assertFalse(stopped.exists())
        self.config({"s": {"options": {"baseURL": srv + "/v1"}, "models": {"m": {}}}},
                    {"s": {"stop": f"touch {stopped}"}})
        # this stop leaves the server answering: free_gpu waited out all of STOP_SETTLE's 30 s
        # for it to go quiet (2026-11-21); what it waits for is another test's (#898)
        with mock.patch.object(models, "STOP_SETTLE", 1):
            models.free_gpu(say=lambda m: None)
        self.assertTrue(stopped.exists())

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
        with mock.patch.object(models.hardware, "gpus", lambda windows=True: []):
            media.settle(said.append, timeout=30)
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

    def test_a_key_kept_by_opencode_auth_login_is_sent_when_the_config_has_no_api_key(self):
        # #336, the audit of 1.4: OpenCode's docs keep a custom provider's key in auth.json, with
        # no options.apiKey; the probes sent none and a model OpenCode would run read "it wants
        # an API key"
        keyed = self.server({"/v1/models": lambda h: {"data": [{"id": "a"}]}
                             if h.get("Authorization") == "Bearer authkey" else 401})
        data = Path(self.tmp.name) / "xdgdata"
        (data / "opencode").mkdir(parents=True)
        (data / "opencode" / "auth.json").write_text(json.dumps(
            {"ak": {"type": "api", "key": "authkey"}, "wrong": {"type": "api", "key": "authkey"}}))
        self.config({"ak": {"options": {"baseURL": keyed + "/v1"}, "models": {"a": {}}},
                     "wrong": {"options": {"baseURL": keyed + "/v1", "apiKey": "nope"},
                               "models": {"a": {}}}})
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(data)}):
            found = models.discover()
            self.assertEqual(models.status(found["ak/a"])["why"], "running")
            self.assertEqual(models.providers()["ak"]["key"], "authkey")
            # a key in the config is still the one probed
            self.assertIn("refuses its API key", models.status(found["wrong/a"])["why"])
        self.assertNotIn("authkey", repr(found["ak/a"]))

    def test_a_local_server_that_wants_no_key_never_starts_opencode_to_ask_for_one(self):
        # the #1010 review (2026-11-22): discovery asked OpenCode's credential store for every
        # keyless local provider, an OpenCode server started against the user's store each time,
        # about 0.9 s of a busy core in every process: the pane's wb watch --json on each refresh
        # paid it for a stock Ollama, the cost #691 took off that path. Asked only when refused.
        log = Path(self.tmp.name) / "auth.log"
        os.environ.update(FAKE_AUTH_LOG=str(log), FAKE_AUTH_EXPORT=json.dumps([
            {"id": "cred_1", "integrationID": "ak", "label": "ak", "active": True,
             "value": {"type": "key", "key": "storedkey"}}]))
        free = self.server({"/v1/models": lambda h: {"data": [{"id": "a"}]}})
        keyed = self.server({"/v1/models": lambda h: {"data": [{"id": "a"}]}
                             if h.get("Authorization") == "Bearer storedkey" else 401})
        self.config({"free": {"options": {"baseURL": free + "/v1"}, "models": {"a": {}}}})
        found = models.discover()
        self.assertEqual(models.status(found["free/a"])["why"], "running")
        self.assertFalse(log.exists(), "OpenCode was asked for a key no server wanted")
        self.config({"free": {"options": {"baseURL": free + "/v1"}, "models": {"a": {}}},
                     "ak": {"options": {"baseURL": keyed + "/v1"}, "models": {"a": {}}}})
        found = models.discover()
        self.assertFalse(log.exists(), "discovery itself asked")
        self.assertEqual(models.status(found["ak/a"])["why"], "running")   # #336 still holds
        self.assertEqual(models.status(found["ak/a"])["why"], "running")
        self.assertEqual(log.read_text().split(), ["ak"])                 # once, the refused one
        self.assertNotIn("storedkey", repr(found["ak/a"]))

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
    def test_lm_studio_without_just_in_time_loading_has_a_model_it_hasnt_loaded(self):
        # #462 (the audit of 1.8, reproduced): JIT off lists only loaded models on /v1/models, so
        # with a loaded, b read "the server serves a, not b: set the entry's id to a"
        rows = lambda *loaded: {"models": [  # noqa: E731
            {"key": k, "loaded_instances": [{"id": k}] if k in loaded else []} for k in ("a", "b", "c")]}
        state = {"v1": {"data": [{"id": "a"}]}, "lms": rows("a")}
        lms = self.server({"/v1/models": lambda h: state["v1"], "/api/v1/models": lambda h: state["lms"]})
        plain = self.server({"/v1/models": {"data": [{"id": "a"}]}})     # not LM Studio
        self.config({"lms": {"options": {"baseURL": lms + "/v1"}, "models": {"a": {}, "b": {}, "z": {}}},
                     "srv": {"options": {"baseURL": plain + "/v1"}, "models": {"b": {}}}},
                    {"lms": {"engine": "server"}, "srv": {"engine": "server"}})
        found = models.discover()
        self.assertEqual(models.status(found["lms/b"]),
                         {"available": True, "resident": False, "why": "in LM Studio, not loaded (wb loads it)"})
        self.assertTrue(models.status(found["lms/a"])["resident"])
        self.assertIn("set the entry's id to a", models.status(found["lms/z"])["why"])  # LM Studio lacks z
        self.assertIn("set the entry's id to a", models.status(found["srv/b"])["why"])
        state.update(v1={"data": [{"id": "a"}, {"id": "c"}]}, lms=rows("a", "c"))  # two loaded
        self.assertTrue(models.status(found["lms/b"])["available"])
        state.update(v1={"data": []}, lms=rows())                               # none loaded
        self.assertTrue(models.status(found["lms/b"])["available"])

    def test_lm_studio_unloads_the_model_a_switch_leaves_and_reloads_one_with_a_short_window(self):
        # #645 (the audit of main): LM Studio evicts only its just-in-time loads, never what
        # /api/v1/models/load loaded, and free_gpu skipped the kept model's server: each switch
        # between two of its models stacked one more in VRAM, and an instance loaded with the
        # UI's 4K window was kept, so short_window failed the task
        loaded, posts = {"a": 32768}, []
        srv, url = fake_lmstudio(loaded, posts)
        self.servers.append(srv)
        win = {"limit": {"context": 32768}}
        self.config({"lms": {"options": {"baseURL": url + "/v1"}, "models": {"a": win, "b": win}}},
                    {"lms": {"engine": "server"}})
        found = models.discover()
        models.make_resident(found["lms/b"], say=lambda *_: None)
        self.assertEqual(loaded, {"b": 32768})                    # a let go, b with OpenCode's window
        self.assertEqual(posts, [("/api/v1/models/unload", {"instance_id": "a"}),
                                 ("/api/v1/models/load", {"model": "b", "context_length": 32768})])
        self.assertEqual(models.make_resident(found["lms/b"], say=lambda *_: None), 0.0)
        loaded.update(a=4096)                                     # someone loads a beside it
        models.make_resident(found["lms/b"], say=lambda *_: None)
        self.assertEqual(loaded, {"b": 32768})
        loaded.clear()
        loaded["a"] = 4096                                         # a, loaded in the UI at 4K
        posts.clear()
        models.make_resident(found["lms/a"], say=lambda *_: None)
        self.assertEqual(loaded, {"a": 32768})
        self.assertEqual(posts, [("/api/v1/models/unload", {"instance_id": "a"}),
                                 ("/api/v1/models/load", {"model": "a", "context_length": 32768})])
        Path(os.environ["WB_MODELS"]).write_text(json.dumps({"lms": {"engine": "server"},
                                                             "keep_loaded": ["lms/a"]}))
        models.make_resident(found["lms/b"], say=lambda *_: None)
        self.assertEqual(loaded, {"a": 32768, "b": 32768})        # keep_loaded is kept

    def test_an_idle_lm_studio_neither_empties_the_gpu_of_ollamas_models_nor_stops_a_respread(self):
        # #808: just in time, LM Studio's /v1/models lists every downloaded model, so with nothing
        # loaded it read as a resident server: every round unloaded the user's Ollama models that
        # fit beside the task's, and a model split onto the CPU was never placed again (#703)
        gb = 10 ** 9
        ollama_loaded, lms_loaded, posts = [{"name": "chat", "size": 2 * gb, "size_vram": 2 * gb}], {}, []
        srv, port = fake_ollama(ollama_loaded, ["x", "chat"], posts)
        lms, url = fake_lmstudio(lms_loaded, [])
        self.servers += [srv, lms]
        win = {"limit": {"context": 32768}}
        self.config({"o": {"options": {"baseURL": f"http://127.0.0.1:{port}/v1"}, "models": {"x": {}, "chat": {}}},
                     "lms": {"options": {"baseURL": url + "/v1"}, "models": {"a": win, "b": win}}},
                    {"lms": {"engine": "server"}, "o/x": {"vram_gb": 4}, "gpu_gb": 20})
        found = models.discover()
        self.assertTrue(models.status(found["lms/a"])["resident"])  # its list, not its memory
        x = found["o/x"]
        self.assertTrue(models._room_beside(x))                    # 2 + 4 of 18, LM Studio idle
        lms_loaded["a"] = 32768
        self.assertFalse(models._room_beside(x))                   # an instance whose size is unsaid
        ollama_loaded.append({"name": "x", "size": 10 * gb, "size_vram": 5 * gb})  # half on the CPU
        self.assertIsNotNone(models._respread(x))                  # free_gpu unloads a
        Path(os.environ["WB_MODELS"]).write_text(json.dumps(
            {"lms": {"engine": "server"}, "o/x": {"vram_gb": 4}, "gpu_gb": 20, "keep_loaded": ["lms/a"]}))
        self.assertIsNone(models._respread(models.discover()["o/x"]))  # kept: its memory stays taken
        lms_loaded.clear()
        self.assertIsNotNone(models._respread(models.discover()["o/x"]))

    def test_scout_finds_jan_and_text_generation_webui_and_a_server_that_wants_a_key(self):
        # #810: Jan (1337) and text-generation-webui (5000) were never probed, and a 401 read as
        # no answer: "none found", and first run offered servers to install
        from workbench import cli
        self.assertIn((1337, "Jan"), models.SERVER_PORTS)
        self.assertIn((5000, "text-generation-webui"), models.SERVER_PORTS)

        def refusing(code, said):
            class H(BaseHTTPRequestHandler):
                def do_GET(self):
                    self.send_response(code)
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": said}).encode())

                def log_message(self, *a):
                    pass
            srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            self.servers.append(srv)
            return srv.server_address[1]
        jan = self.server({"/v1/models": {"data": [{"id": "qwen-coder"}]}})
        keyed = refusing(401, "invalid api key")
        chat = refusing(401, "bad or missing token: open the link from `wb chat url`")  # wb's own
        airplay = refusing(403, "forbidden")                                              # macOS's 5000
        ports = ((int(jan.rsplit(":", 1)[1]), "Jan"), (keyed, "LM Studio"), (chat, "Jan"),
                 (airplay, "text-generation-webui"))
        with mock.patch.object(models, "SERVER_PORTS", ports), \
                mock.patch.object(models, "windows_host", return_value=None):
            _, found = models.servers_here()
            self.assertEqual(found, [{"root": jan, "usually": "Jan", "models": ["qwen-coder"]},
                                     {"root": f"http://127.0.0.1:{keyed}", "usually": "LM Studio",
                                      "models": [], "key": True}])
            lines = cli.server_lines({})
        self.assertIn(f"{jan} answers (usually Jan): qwen-coder", lines[-2])
        self.assertIn(f"127.0.0.1:{keyed} answers (usually LM Studio): it wants an API key", lines[-1])

    def test_a_wb_remote_box_waits_for_another_clients_model_and_names_one_its_load_evicted(self):
        # #812: two machines' tasks on one wb remote box each took their own lease, so each load
        # evicted the other client's model, mid-request, and the cause was never named
        loaded, posts = [{"name": "theirs", "size": 10, "size_vram": 10,
                          "expires_at": "2020-01-01T00:00:00Z"}], []           # past: answering
        srv, port = fake_ollama(loaded, ["x", "theirs", "emb"], posts)
        self.servers.append(srv)
        self.config({"box": {"options": {"baseURL": f"http://127.0.0.1:{port}/v1"}, "models": {"x": {}}}},
                    {"box": {"local": True}})
        warned = []
        with mock.patch.object(models, "_this_host", lambda h: False), \
                mock.patch.object(models, "BOX_WAIT", 0), mock.patch.object(models, "BOX_QUIET", 1):
            x = models.discover()["box/x"]
            self.assertTrue(x.remote)
            self.assertEqual(models.box_others(x), ["theirs"])
            models.make_resident(x, say=lambda *_: None, warn=warned.append)
            self.assertIn("theirs on 127.0.0.1 is answering another client's requests", warned[0])
            self.assertIn("don't share a GPU lease", warned[0])
            self.assertIn("still in use after 0 min: loading x anyway", warned[1])
            self.assertEqual([p["model"] for p in posts], ["x"])              # theirs left loaded
            self.assertEqual(len(warned), 2)                                  # and not evicted
            models._unload(x)
            loaded[:] = [{"name": "theirs", "size": 10, "size_vram": 10}, {"name": "emb", "size": 1}]
            Path(os.environ["WB_MODELS"]).write_text(json.dumps({"box": {"local": True}, "keep_loaded": ["emb"]}))
            self.assertEqual(models.box_others(models.discover()["box/x"]), ["theirs"])  # emb: the user's
            real_post, warned[:] = models._post, []

            def evicting(url, body, timeout=600):
                loaded[:] = [r for r in loaded if r["name"] != "theirs"]
                return real_post(url, body, timeout)
            with mock.patch.object(models, "_post", evicting):
                models.make_resident(models.discover()["box/x"], say=lambda *_: None, warn=warned.append)
            self.assertEqual(len(warned), 1)
            self.assertIn("loading x on 127.0.0.1 unloaded theirs, which another client of the box had loaded",
                          warned[0])
            local = models.Model(id="o/x", provider="o", name="x", base_url=x.base_url, engine="ollama", label="o/x")
            self.assertEqual(models.box_others(local), [])                    # this machine's: free_gpu's

    def test_a_wb_remote_box_client_loads_over_another_clients_model_only_once_it_has_gone_quiet(self):
        # #812: the clients of one box share no lease, and each load watched the other client's
        # model for a moment only: one between two requests of its round (a tool running) was
        # loaded over, and the two swapped on every request after. Now a load waits until the
        # other's model has gone BOX_QUIET without a request: the box is taken a round at a time
        at = lambda s: time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() + s)) + ".1Z"  # noqa
        theirs = {"name": "theirs", "size": 10, "size_vram": 10, "expires_at": at(300)}
        loaded, posts = [theirs], []
        srv, port = fake_ollama(loaded, ["x", "theirs"], posts)
        self.servers.append(srv)
        self.config({"box": {"options": {"baseURL": f"http://127.0.0.1:{port}/v1"}, "models": {"x": {}}}},
                    {"box": {"local": True}})
        said, warned, requests = [], [], []

        def request():  # the other client's next request in its round finishes
            requests.append(time.monotonic())
            theirs["expires_at"] = at(300 + len(requests))
        for delay in (0.6, 1.2):
            threading.Timer(delay, request).start()
        with mock.patch.object(models, "_this_host", lambda h: False), \
                mock.patch.object(models, "INFLIGHT_WATCH", 0.3), \
                mock.patch.object(models, "BOX_QUIET", 1.5), mock.patch.object(models, "BOX_POLL", 0.1):
            x = models.discover()["box/x"]
            models.make_resident(x, say=said.append, warn=warned.append)
            loaded_at = time.monotonic()
        self.assertEqual(len(requests), 2)
        self.assertGreaterEqual(loaded_at - requests[-1], 1.5)       # quiet that long first
        self.assertEqual([p["model"] for p in posts], ["x"])
        self.assertIn("theirs on 127.0.0.1 is another client's: loading x once it has gone 1.5 s "
                      "without a request", said[0])
        self.assertEqual(len(warned), 1)
        self.assertIn("theirs on 127.0.0.1 is answering another client's requests", warned[0])
        # an idle one, its round over: loaded over once BOX_QUIET passes, with nothing to warn of
        loaded[:], posts[:], warned[:] = [theirs], [], []
        with mock.patch.object(models, "_this_host", lambda h: False), \
                mock.patch.object(models, "BOX_QUIET", 0.5), mock.patch.object(models, "BOX_POLL", 0.1):
            began = time.monotonic()
            models.make_resident(models.discover()["box/x"], say=said.append, warn=warned.append)
            self.assertGreaterEqual(time.monotonic() - began, 0.5)
        self.assertEqual(warned, [])
        self.assertEqual([p["model"] for p in posts], ["x"])

    def test_a_box_model_whose_stamp_stands_still_past_box_quiet_is_waited_for_until_it_moves_or_goes(self):
        # #899: a stamp stands still while a request runs, so another client's request running
        # past BOX_QUIET read as quiet and was loaded over; and a laptop's suspend time since boot
        # excused every box stamp from being past
        at = lambda s: time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() + s)) + ".1Z"  # noqa
        theirs = {"name": "theirs", "size": 10, "size_vram": 10, "expires_at": at(250)}  # done 50 s ago
        loaded, posts = [theirs], []
        srv, port = fake_ollama(loaded, ["x", "theirs"], posts)
        self.servers.append(srv)
        self.config({"box": {"options": {"baseURL": f"http://127.0.0.1:{port}/v1"}, "models": {"x": {}}}},
                    {"box": {"local": True}})
        gone = []

        def done():  # its keep-alive ends (or its long request did, and its round): Ollama unloads it
            loaded.clear()
            gone.append(time.monotonic())
        threading.Timer(2.5, done).start()
        with mock.patch.object(models, "_this_host", lambda h: False), \
                mock.patch.object(models, "BOX_KEEP_ALIVE", 300), mock.patch.object(models, "BOX_QUIET", 1), \
                mock.patch.object(models, "BOX_POLL", 0.1):
            models.make_resident(models.discover()["box/x"], say=lambda *_: None, warn=lambda *_: None)
            loaded_at = time.monotonic()
        self.assertTrue(gone and loaded_at >= gone[0])           # not after BOX_QUIET of no movement
        self.assertEqual([p["model"] for p in posts], ["x"])
        # a box's stamp past by more than BOX_SKEW is a request still running, however long this
        # machine has slept since it booted
        with mock.patch.object(models, "slept", lambda: 36000.0):
            self.assertTrue(models._past(at(-60), remote=True))
            self.assertFalse(models._past(at(-60)))                  # this machine's own Ollama: excused
            self.assertFalse(models._past(at(-5), remote=True))      # within the clocks' skew

    def test_a_model_loaded_on_a_box_keeps_its_stamp_readable_through_its_first_long_request(self):
        # reviewing #899 (2026-11-21): make_resident loaded a box's model for 30 min, so its stamp
        # stood 25 min past what box_quiet reads as its last finished request while the round's
        # first request (the full prefill) ran; another client read it as quiet and loaded over it
        at = lambda s: time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() + s)) + ".1Z"  # noqa
        loaded, posts = [], []
        srv, port = fake_ollama(loaded, ["x", "y"], posts)
        self.servers.append(srv)
        self.config({"box": {"options": {"baseURL": f"http://127.0.0.1:{port}/v1"},
                             "models": {"x": {}, "y": {}}}}, {"box": {"local": True}})
        with mock.patch.object(models, "_this_host", lambda h: False), \
                mock.patch.object(models, "BOX_KEEP_ALIVE", 300):
            models.make_resident(models.discover()["box/x"], say=lambda *_: None, warn=lambda *_: None)
        keep = posts[-1]["keep_alive"]
        secs = int(keep[:-1]) * {"s": 1, "m": 60}[keep[-1]]
        # 200 s after that load, the first request still running: the stamp hasn't moved
        loaded[:] = [{"name": "x", "size": 10, "size_vram": 10, "expires_at": at(secs - 200)}]
        gone = []

        def done():  # the request and its round end, and the keep-alive after: Ollama unloads it
            loaded.clear()
            gone.append(time.monotonic())
        threading.Timer(2.5, done).start()
        with mock.patch.object(models, "_this_host", lambda h: False), \
                mock.patch.object(models, "BOX_KEEP_ALIVE", 300), mock.patch.object(models, "BOX_QUIET", 1), \
                mock.patch.object(models, "BOX_POLL", 0.1):
            models.box_quiet(models.discover()["box/y"], ["x"], say=lambda *_: None, warn=lambda *_: None)
            loaded_at = time.monotonic()
        self.assertTrue(gone and loaded_at >= gone[0])           # waited for it, not loaded over it

    def test_a_model_this_machine_left_on_its_box_is_loaded_over_without_waiting(self):
        # reviewing #812 (2026-11-21): every model on the box that the task wasn't using read as
        # another client's, so one user with one box waited BOX_QUIET at every model switch for
        # the model their own last task had left loaded
        at = lambda s: time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() + s)) + ".1Z"  # noqa
        loaded, posts = [], []
        srv, port = fake_ollama(loaded, ["x", "y"], posts)
        self.servers.append(srv)
        self.config({"box": {"options": {"baseURL": f"http://127.0.0.1:{port}/v1"},
                             "models": {"x": {}, "y": {}}}}, {"box": {"local": True}})
        with mock.patch.object(models, "_this_host", lambda h: False), \
                mock.patch.object(models, "INFLIGHT_WATCH", 0.3), \
                mock.patch.object(models, "BOX_QUIET", 30), mock.patch.object(models, "BOX_POLL", 0.1):
            found = models.discover()
            models.make_resident(found["box/x"], say=lambda *_: None, warn=lambda *_: None)
            self.assertIn("x", models.box_mine(found["box/x"].root))
            loaded[:] = [{"name": "x", "size": 10, "size_vram": 10, "expires_at": at(300)}]
            self.assertEqual(models.box_others(found["box/y"]), [])     # this machine's own
            began = time.monotonic()
            models.make_resident(found["box/y"], say=lambda *_: None, warn=lambda *_: None)
            self.assertLess(time.monotonic() - began, 5)                # no BOX_QUIET wait
            loaded[:] = [{"name": "theirs", "size": 10, "size_vram": 10, "expires_at": at(300)}]
            self.assertEqual(models.box_others(found["box/y"]), ["theirs"])   # still another's

    def test_a_model_this_machine_once_loaded_on_its_box_is_waited_for_while_another_client_uses_it(self):
        # #893: boxes.json marked a model this machine loaded on Monday as its own for a week, so
        # on Wednesday, with another client mid-round on it, the load evicted it without waiting
        # or a word. A request on it now is another client's: this machine runs one task there
        at = lambda s: time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() + s)) + ".1Z"  # noqa
        x = {"name": "x", "size": 10, "size_vram": 10, "expires_at": at(300)}
        loaded, posts = [], []
        srv, port = fake_ollama(loaded, ["x", "y"], posts)
        self.servers.append(srv)
        self.config({"box": {"options": {"baseURL": f"http://127.0.0.1:{port}/v1"},
                             "models": {"x": {}, "y": {}}}}, {"box": {"local": True}})
        warned, requests = [], []

        def request():  # the other client's request on x finishes
            requests.append(time.monotonic())
            x["expires_at"] = at(300 + len(requests))
        with mock.patch.object(models, "_this_host", lambda h: False), \
                mock.patch.object(models, "INFLIGHT_WATCH", 1), \
                mock.patch.object(models, "BOX_QUIET", 1.5), mock.patch.object(models, "BOX_POLL", 0.1):
            found = models.discover()
            models.make_resident(found["box/x"], say=lambda *_: None, warn=lambda *_: None)
            root = found["box/x"].root
            self.assertIn("x", models.box_mine(root))
            loaded[:] = [x]
            self.assertEqual(models.box_others(found["box/y"]), [])                 # as before
            self.assertEqual(models.box_others(found["box/y"], own=True), ["x"])
            threading.Timer(0.4, request).start()
            posts[:] = []
            models.make_resident(found["box/y"], say=lambda *_: None, warn=warned.append)
            loaded_at = time.monotonic()
        self.assertEqual(len(requests), 1)
        self.assertGreaterEqual(loaded_at - requests[-1], 1.5)       # waited BOX_QUIET for it
        self.assertIn("x on 127.0.0.1 is answering another client's requests", warned[0])
        self.assertNotIn("x", models.box_mine(root))                   # no longer taken as ours
        self.assertEqual([p["model"] for p in posts], ["y"])

    def test_a_media_job_on_a_wb_remote_box_unloads_another_clients_model_only_once_it_has_gone_quiet(self):
        # #944 (an outside review of 1.90.24): wb image and wb audio on a wb remote box took this
        # machine's lease for it only and unloaded every model there at once, another client's
        # in the middle of its round, which paid a reload it was never told of
        at = lambda s: time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() + s)) + ".1Z"  # noqa
        theirs = {"name": "theirs", "size": 10, "size_vram": 10, "expires_at": at(300)}
        loaded, posts = [theirs], []
        srv, port = fake_ollama(loaded, ["x", "theirs"], posts)
        self.servers.append(srv)
        self.config({"box": {"options": {"baseURL": f"http://127.0.0.1:{port}/v1"}, "models": {"x": {}}}},
                    {"box": {"local": True}})
        engine = {"url": "http://127.0.0.1:30000/v1/images/generations"}
        said, requests = [], []

        def request():  # the other client's next request in its round finishes
            requests.append(time.monotonic())
            theirs["expires_at"] = at(300 + len(requests))
        for delay in (0.6, 1.2):
            threading.Timer(delay, request).start()
        with mock.patch.object(models, "_this_host", lambda h: False), \
                mock.patch.object(models, "windows_host", lambda: None), \
                mock.patch.object(models, "INFLIGHT_WATCH", 0.3), \
                mock.patch.object(models, "BOX_QUIET", 1.5), mock.patch.object(models, "BOX_POLL", 0.1):
            self.assertEqual([m.id for m in media.boxes(engine)], ["box/x"])
            with media.turns("wb image", said.append, True, engine):
                entered = time.monotonic()
            self.assertEqual(len(requests), 2)
            self.assertGreaterEqual(entered - requests[-1], 1.5)          # quiet that long first
            self.assertEqual(posts, [{"model": "theirs", "keep_alive": 0}])
            self.assertIn("theirs on 127.0.0.1 is another client's: unloading it for wb image once "
                          "it has gone 1.5 s without a request", said)
            self.assertTrue(any("is answering another client's requests" in x for x in said))
            # without --wait: refused at once, naming it, and nothing unloaded
            loaded[:], posts[:] = [{**theirs, "expires_at": at(300)}], []
            with self.assertRaisesRegex(RuntimeError, "theirs on 127.0.0.1 is another client's of that "
                                                      "wb remote box: wb image would unload it"):
                with media.turns("wb image", said.append, False, engine):
                    pass
            self.assertEqual(posts, [])
            self.assertEqual(models.holders(pool=models.pool(models.discover()["box/x"])), {})
            # this machine's own load there, idle: unloaded as before, no wait to refuse
            models._note_mine(models.discover()["box/x"])
            loaded[:] = [{"name": "x", "size": 10, "size_vram": 10, "expires_at": at(300)}]
            with media.turns("wb image", said.append, False, engine):
                pass
            self.assertEqual(posts, [{"model": "x", "keep_alive": 0}])

    def test_a_media_batch_takes_the_remote_boxes_turns_in_one_order_whatever_its_engines_order(self):
        # #945 (an outside review of 1.90.24): the boxes' leases were taken in engine order, so
        # two wb audio jobs with generator and listener on two boxes the other way round (two
        # audio.json files, one shared lease directory) each held one box waiting for the other
        box = lambda h: models.Model(id=f"{h}/x", provider=h, name="x", engine="server",  # noqa: E731
                                     base_url=f"http://{h}.invalid:8000/v1", label=h, remote=True)
        a, b = box("box-a"), box("box-b")
        taken = []

        @contextlib.contextmanager
        def lease(m, who, say, wait=True, on=None, cards=None):
            taken.append(models.pool(on))
            yield
        gen, ear = {"url": "http://box-b.invalid:9000/v1/audio", "on": b}, \
            {"url": "http://box-a.invalid:9000/v1/audio", "on": a}
        with mock.patch.object(media, "boxes", lambda e: [e["on"]]), \
                mock.patch.object(models, "same_gpu", lambda m: False), \
                mock.patch.object(models, "GpuLease", lease):
            for engines in ((gen, ear), (ear, gen)):
                with media.turns("wb audio", lambda *_: None, True, *engines):
                    pass
        self.assertEqual(taken, ["gpu-box-a-invalid-8000", "gpu-box-b-invalid-8000"] * 2)

    def test_a_remote_task_hears_when_another_client_unloaded_its_model_between_rounds(self):
        # #812: the other client's load evicted this task's model, and nothing told this task why
        # its next round began with a reload
        loaded, posts = [], []
        srv, port = fake_ollama(loaded, ["x", "theirs"], posts)
        self.servers.append(srv)
        self.config({"box": {"options": {"baseURL": f"http://127.0.0.1:{port}/v1"}, "models": {"x": {}}}},
                    {"box": {"local": True}})
        with mock.patch.object(models, "_this_host", lambda h: False):
            lease = models.GpuLease(models.discover()["box/x"], "", say=lambda *_: None)
            warned = []
            lease.warn = warned.append
            lease.load()
            self.assertEqual(warned, [])                                 # its first load
            loaded[:] = [{"name": "theirs", "size": 10, "size_vram": 10}]  # theirs evicted it
            with mock.patch.object(models, "BOX_QUIET", 0):
                lease.load()
        self.assertIn("x was unloaded from 127.0.0.1 since the last round, which has theirs loaded "
                      "for another client", warned[0])
        self.assertEqual([p["model"] for p in posts], ["x", "x"])

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

    def kobold(self, extra: bool = True) -> str:
        """A server answering as KoboldCpp does (checked against its source for #580): Ollama's
        /api/version, /api/tags and /api/ps with one hard-coded 0.39 GB row of family koboldcpp,
        /api/show with only completion, and, ``extra``, its own /api/extra/version."""
        row = {"name": "koboldcpp/coder", "model": "koboldcpp/coder", "size": 394998579,
               "size_vram": 394998579, "details": {"family": "koboldcpp"}}
        return self.server({"/api/version": {"version": "0.7.0"}, "/api/tags": {"models": [row]},
                            "/api/ps": {"models": [row]},
                            **({"/api/extra/version": {"result": "KoboldCpp", "version": "1.80"}}
                               if extra else {}),
                            "/v1/models": {"data": [{"id": "koboldcpp/coder", "owned_by": "koboldcpp"}]}})

    def test_koboldcpps_ollama_emulation_is_driven_as_a_server_and_add_keeps_its_tool_calls(self):
        # #580, the audit of 1.90: any /api/version with a version read as Ollama, so wb model add
        # wrote tool_call: false from its /api/show, and the router, unloads and optimize drove
        # it as Ollama with a fake 0.39 GB size
        from workbench import cli
        for extra in (True, False):        # its own endpoint, or only its tags row's family
            models._ENGINES.clear()
            models._EMULATORS.clear()
            url = self.kobold(extra)
            self.config({"kc": {"options": {"baseURL": url + "/v1"}, "models": {}}})
            self.assertEqual(models.providers()["kc"]["engine"], "server")
            self.assertFalse(models.ollama_answers(url))
            self.assertIn("is KoboldCpp's emulation of it: wb drives it as an OpenAI-compatible "
                          "server", models.providers()["kc"]["engine_note"])
            out = io.StringIO()
            with redirect_stdout(out):
                cli.main(["model", "add", "kc/koboldcpp/coder", "--context", "32768"])
            self.assertIn("KoboldCpp's emulation", out.getvalue())
            self.assertNotIn("NO tool calls", out.getvalue())
            entry = json.loads(Path(os.environ["WB_OPENCODE_CONFIG"]).read_text())["provider"]["kc"]["models"]
            self.assertNotEqual(entry["koboldcpp/coder"].get("tool_call"), False)
            self.assertNotIn("max_tokens", entry["koboldcpp/coder"].get("options") or {})
            m = models.resolve("kc/koboldcpp/coder")
            self.assertEqual((m.engine, models.status(m)["why"]), ("server", "running"))
        # the models.json override still wins, and then nothing is said
        self.config({"kc": {"options": {"baseURL": url + "/v1"}, "models": {}}}, {"kc": {"engine": "ollama"}})
        self.assertEqual((models.providers()["kc"]["engine"], models.providers()["kc"]["engine_note"]),
                         ("ollama", ""))

    def test_a_real_ollama_is_not_taken_for_an_emulator_and_a_server_on_its_port_is_named(self):
        posts = []
        srv, port = fake_ollama([], ["m"], posts)
        self.servers.append(srv)
        other = self.server({"/v1/models": {"data": [{"id": "m"}]}})
        self.config({"o": {"options": {"baseURL": f"http://127.0.0.1:{port}/v1"}, "models": {}},
                     "ollama-box": {"options": {"baseURL": other + "/v1"}, "models": {}}})
        provs = models.providers()
        self.assertEqual((provs["o"]["engine"], provs["o"]["engine_note"]), ("ollama", ""))
        self.assertEqual(provs["ollama-box"]["engine"], "server")
        self.assertIn("ollama-box is named for Ollama, but", provs["ollama-box"]["engine_note"])
        self.assertIn('set "engine" for ollama-box', provs["ollama-box"]["engine_note"])

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

    def test_each_model_is_fitted_against_its_own_servers_cards_not_the_first_ollamas(self):
        # #801: one Ollama per card (8 GB and 24 GB); one budget for all, the first process's,
        # held the 24 GB card's 20 GB model back and passed a model too big for the 8 GB one
        big = self.server({"/v1/models": {"data": [{"id": "big", "owned_by": "llamacpp",
                                                      "meta": {"size": 20_000_000_000}}]}})
        small = self.server({"/v1/models": {"data": [{"id": "small", "owned_by": "llamacpp",
                                                        "meta": {"size": 12_000_000_000}}]}})
        self.config({"big": {"options": {"baseURL": big + "/v1"}, "models": {"big": {}}},
                     "small": {"options": {"baseURL": small + "/v1"}, "models": {"small": {}}}},
                    {"big": {"engine": "server"}, "small": {"engine": "server"}})
        cards = {big: 24_000_000_000, small: 8_000_000_000}
        asked = []
        with mock.patch.object(models, "gpu_budget",
                               side_effect=lambda root=None: asked.append(root) or cards.get(root, 8_000_000_000)):
            fit = {s.model.id: s.fits for s in router.rank("significant")}
        self.assertEqual(fit, {"big/big": True, "small/small": False})
        self.assertEqual(sorted(asked), sorted(cards))  # once per server in the pass

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

    def test_models_a_server_gives_no_size_are_ranked_by_their_names_or_start_file_and_say_so(self):
        # #704: vLLM's /v1/models has no size, so every weight was 0: small-3b listed first took
        # significant work as "the largest model", and trivial work as "the smallest"
        from workbench import cli
        vllm = self.server({"/v1/models": {"data": [{"id": "small-3b", "owned_by": "vllm"},
                                                    {"id": "big-70b", "owned_by": "vllm"},
                                                    {"id": "coder", "owned_by": "vllm"}]}})
        entry = {"limit": {"context": 32768}}
        self.config({"v": {"options": {"baseURL": vllm + "/v1"},
                           "models": {"small-3b": entry, "big-70b": entry}}}, {"v": {"engine": "server"}})
        top, why = router.choose("significant")
        self.assertEqual(top.model.id, "v/big-70b")
        self.assertIn("the largest model allowed significant work (its size guessed from its name)", why)
        self.assertEqual(router.choose("trivial")[0].model.id, "v/small-3b")
        self.assertEqual(models.guessed_weights(models.Model(
            id="o/m", provider="o", name="mixtral-8x7b-instruct", base_url="", engine="server", label="o/m")), 56 * 10 ** 9)
        self.assertEqual(models.guessed_weights(models.Model(
            id="o/m", provider="o", name="qwen3-30b-a3b", base_url="", engine="server", label="o/m")), 30 * 10 ** 9)
        # no size anywhere: said, not called the largest
        self.config({"v": {"options": {"baseURL": vllm + "/v1"}, "models": {"coder": entry, "small-3b": entry}}},
                    {"v": {"engine": "server"}})
        top, why = router.choose("significant")
        self.assertIn("v/coder: size unknown, models.json weights_gb sets it", why)
        self.assertEqual(top.model.id, "v/small-3b")
        self.config({"v": {"options": {"baseURL": vllm + "/v1"}, "models": {"coder": entry}},
                     "w": {"options": {"baseURL": vllm + "/v1"}, "models": {"coder": entry}}},
                    {"v": {"engine": "server"}, "w": {"engine": "server"}})
        self.assertIn("sizes unknown: config order; models.json weights_gb sets a model's size",
                      router.choose("significant")[1])
        out = io.StringIO()
        with redirect_stdout(out):
            cli.main(["models"])
        self.assertIn("size: unknown (the server says none; models.json weights_gb sets it)", out.getvalue())
        out = io.StringIO()
        with contextlib.suppress(SystemExit), redirect_stdout(out):
            cli.main(["doctor"])
        self.assertIn("--  v/coder: its size is unknown (the server says none)", out.getvalue())
        # a llama-server start command's -m file is the size, measured
        gguf = Path(self.tmp.name, "coder.gguf")
        gguf.write_bytes(b"\0" * 4096)
        self.config({"v": {"options": {"baseURL": vllm + "/v1"}, "models": {"coder": entry}}},
                    {"v": {"engine": "server"}, "v/coder": {"start": f"llama-server -m {gguf} --port 1"}})
        self.assertEqual(models.weights(models.discover()["v/coder"]), 4096)

    def test_doctor_scout_and_add_ask_ollama_by_an_aliased_entrys_id(self):
        # #588 (the audit of 1.90): `ollama/coder` with id qwen3-coder:30b; doctor asked Ollama
        # about `coder`, fell back to OLLAMA_CONTEXT_LENGTH and said BAD, its advice (wb model add
        # ollama/coder --context …) died on a 404, and scout listed qwen3-coder:30b as unregistered
        from workbench import cli
        tag = "qwen3-coder:30b"
        class H(BaseHTTPRequestHandler):
            def reply(self, code, body):
                self.send_response(code)
                self.end_headers()
                self.wfile.write(json.dumps(body).encode())
            def do_GET(self):
                body = {"/api/version": {"version": "0.40.0"},
                        "/api/tags": {"models": [{"name": tag, "size": 18_000_000_000}]},
                        "/api/ps": {"models": []}}.get(self.path)
                self.reply(200 if body is not None else 404, body or {})
            def do_POST(self):
                asked = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["model"]
                if self.path != "/api/show" or asked != tag:
                    return self.reply(404, {"error": f"model '{asked}' not found"})
                self.reply(200, {"capabilities": ["tools"], "parameters": "num_ctx 65536",
                                 "model_info": {"qwen3.context_length": 262144}})
            def log_message(self, *a):
                pass
        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.servers.append(srv)
        entry = {"id": tag, "limit": {"context": 65536, "output": 8192}, "options": {"max_tokens": 16384}}
        self.config({"ollama": {"options": {"baseURL": f"http://127.0.0.1:{srv.server_address[1]}/v1"},
                                "models": {"coder": entry}}})
        out = io.StringIO()
        with mock.patch.object(models.hardware, "ollama_env", return_value={"OLLAMA_CONTEXT_LENGTH": "32768"}), \
                mock.patch.object(models.hardware, "ollama_context", return_value=(32768, "32768")), \
                contextlib.suppress(SystemExit), redirect_stdout(out):
            cli.main(["doctor"])
        self.assertNotIn("Ollama serves 32K", out.getvalue())
        self.assertNotIn("served context is unknown", out.getvalue())
        out = io.StringIO()
        with redirect_stdout(out):
            cli.main(["model", "add", "ollama/coder"])          # no 404: it asks for the id
        self.assertIn("registered ollama/coder", out.getvalue())
        self.assertIn("64K context", out.getvalue())
        saved = json.loads(Path(os.environ["WB_OPENCODE_CONFIG"]).read_text())["provider"]["ollama"]["models"]
        self.assertEqual(saved["coder"]["id"], tag)              # the alias stays an alias
        out = io.StringIO()
        tmp = Path(self.tmp.name)
        with mock.patch.dict(os.environ, {"WB_SKILLS": str(tmp / "skills"), "WB_IMAGES": str(tmp / "i.json"),
                                          "WB_AUDIO": str(tmp / "a.json")}), \
                mock.patch.object(cli.hardware, "report", return_value=[]), \
                mock.patch.object(cli, "ollama_line", return_value=""), \
                mock.patch.object(cli.models, "servers_here", return_value=([], [])), redirect_stdout(out):
            cli.main(["scout"])
        self.assertNotIn("installed in ollama but not registered", out.getvalue())
        self.assertIn("ollama/coder: 18.0 GB on disk · 64K context · Ollama serves 64K", out.getvalue())
        ipv6 = (["llama-server"], [{"root": "http://[::1]:8080", "usually": "llama-server", "models": ["m"]}])
        with mock.patch.object(cli.models, "servers_here", return_value=ipv6):
            lines = cli.server_lines({"ls": {"root": "http://127.0.0.1:8080", "url": "http://[::1]:8080/v1"}})
            self.assertNotIn("no provider", lines[1])           # ::1 is this machine too
            lines = cli.server_lines({"ls": {"root": "http://localhost:8080", "url": "http://localhost:8080/v1"}})
            self.assertNotIn("no provider", lines[1])

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


    def test_threads_writing_one_state_file_never_share_a_temp_file(self):
        # the 2026-11-19 audit: named by pid alone, two of the chat server's threads writing
        # push-test.json shared one temp file, and one's rename found it gone (a 500)
        p = Path(self.tmp.name) / "push-test.json"
        errors = []

        def write(n):
            try:
                for i in range(200):
                    store.write_json(p, {"thread": n, "i": i, "pad": "x" * 2000})
            except OSError as e:
                errors.append(e)
        threads = [threading.Thread(target=write, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(store.read_json(p)["i"], 199)
        self.assertEqual([f.name for f in p.parent.iterdir()], ["push-test.json"])  # no temp left
        mode = p.stat().st_mode & 0o777
        p.unlink()
        with mock.patch("json.dumps", side_effect=ValueError("no")), self.assertRaises(ValueError):
            store.write_json(p, {})
        self.assertEqual(list(p.parent.iterdir()), [])    # a failed write leaves nothing behind
        store.write_json(p, {})
        self.assertEqual(p.stat().st_mode & 0o777, mode)  # the umask's mode, not mkstemp's 0600

    def test_inotify_works_where_libc_isnt_libc_so_6(self):
        # the 2026-11-19 audit: on musl (Alpine) find_library("c") can find nothing and there is
        # no libc.so.6, so every wait fell back to polling
        if not sys.platform.startswith("linux"):
            self.skipTest("inotify is Linux's")
        from workbench import watch
        real = ctypes.CDLL

        def musl(name, *a, **kw):
            if name and "libc.so" in str(name):
                raise OSError(f"{name}: cannot open shared object file")
            return real(name, *a, **kw)
        with mock.patch.object(watch, "_libc", None), mock.patch("ctypes.CDLL", musl), \
                mock.patch("ctypes.util.find_library", lambda name: None):
            self.assertTrue(watch._lib())

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


def fake_remote_ollama(tags: list[dict], shows: dict[str, dict], host: str = "127.0.0.1"):
    """An Ollama on another machine (a free port here, on ``host``: "::1" listens on IPv6):
    /api/tags lists ``tags``, POST /api/show answers ``shows[name]``."""
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
    server = type("V6", (ThreadingHTTPServer,), {"address_family": socket.AF_INET6}) if ":" in host \
        else ThreadingHTTPServer
    srv = server((host, 0), H)
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

    def test_this_machine_named_by_its_lan_or_tailnet_name_is_this_host(self):
        # #807: a baseURL at box.lan (dotfiles shared with a laptop) was another host: its own
        # GPU pool, no sharing or fit check, and the local lease never saw its tasks
        me = socket.gethostname().split(".")[0].lower()
        names = {f"{me}.lan": "192.168.1.23", f"{me}.tail1234.ts.net": "100.64.0.7",
                 "gpubox.lan": "192.168.1.40", "loop.example": "127.0.1.1"}

        def resolve(name, *a, **kw):
            if name not in names:
                raise socket.gaierror(name)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (names[name], 0))]
        for f in (models._own_addresses, models._named_here):
            f.cache_clear()
            self.addCleanup(f.cache_clear)
        with mock.patch.object(models.socket, "getaddrinfo", resolve), \
                mock.patch.object(models.socket, "getfqdn", lambda *a: "desk.corp.example"), \
                mock.patch.object(models, "_own_addresses", lambda: frozenset({"192.168.1.23", "100.64.0.7"})):
            for host in (f"{me}.lan", f"{me}.tail1234.ts.net", "loop.example", "desk.corp.example", "desk"):
                self.assertTrue(models._this_host(host), host)
            self.assertFalse(models._this_host("gpubox.lan"))     # a box of its own
            self.assertFalse(models._this_host("nowhere.lan"))    # unresolved: not this machine
            url = f"http://{me}.lan:11434/v1"
            self.assertEqual(models.server_key(url), models.server_key("http://127.0.0.1:11434/v1"))
            ((pid, _, _, why, remote),) = models._scan({"provider": {"o": {"options": {"baseURL": url}}}}, {})
            self.assertEqual((why, remote), ("", False))
            m = models.Model(id="o/m", provider="o", name="m", base_url=url, engine="ollama", label="o/m")
            self.assertEqual(models.pool(m), "gpu")

    def test_a_host_lookup_that_failed_is_asked_again_and_a_new_address_is_seen(self):
        # #900: a lookup that timed out or failed (a tailnet reconnecting after a wake, slow DNS)
        # was cached as "another host" for the life of the process, and so were the machine's
        # addresses: a runner or the chat server took this machine's server for another's for
        # hours, beside processes that knew better
        me = socket.gethostname().split(".")[0].lower()
        name, looked = f"{me}.tail1234.ts.net", []
        up = []                                            # the tailnet: down, then up

        def resolve(host, *a, **kw):
            looked.append(host)
            if not up:
                raise socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("100.64.0.7", 0))]
        ip_out = ["1: lo    inet 127.0.0.1/8 scope host lo\n"]

        def run(argv, **kw):
            return subprocess.CompletedProcess(argv, 0, ip_out[0] if argv[0] == "ip" else "", "")
        for f in (models._own_addresses, models._named_here):
            f.cache_clear()
            self.addCleanup(f.cache_clear)
        with mock.patch.object(models.socket, "getaddrinfo", resolve), \
                mock.patch.object(models.socket, "getfqdn", lambda *a: "desk.corp.example"), \
                mock.patch.object(models.subprocess, "run", run):
            self.assertFalse(models._this_host(name))
            self.assertFalse(models._this_host(name))
            self.assertEqual(looked.count(name), 1)         # within DOWN_SECS: not asked again
            up.append(1)
            ip_out[0] += "3: tailscale0    inet 100.64.0.7/32 scope global tailscale0\n"
            with mock.patch.object(models, "DOWN_SECS", 0), mock.patch.object(models, "ADDR_SECS", 0):
                self.assertTrue(models._this_host(name))    # asked again, and the tailnet IP is ours
                self.assertTrue(models._this_host("100.64.0.7"))
            n = len(looked)
            self.assertTrue(models._this_host(name))        # a definite answer: kept
            self.assertEqual(len(looked), n)

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

    def test_wb_remote_brackets_a_bare_ipv6_address_and_either_spelling_is_one_provider(self):
        # #707: `wb remote ::1` wrote http://::1:PORT/v1, which the probe reached and discovery
        # couldn't parse: no models, and retrying as [::1] hit a provider conflict
        ctx = {"llama.context_length": 131072}
        try:
            srv, port = fake_remote_ollama([{"name": "coder:7b"}],
                                           {"coder:7b": {"capabilities": ["tools"], "model_info": ctx}}, host="::1")
        except OSError:
            self.skipTest("no IPv6 loopback here")
        self.servers.append(srv)
        self.config({}, {})
        code, out = self.wb("remote", "::1", "--port", str(port), "--context", "32768")
        self.assertEqual(code, 0, out)
        pid = models.remote_pid("::1")
        prov = json.loads(Path(os.environ["WB_OPENCODE_CONFIG"]).read_text())["provider"][pid]
        self.assertEqual(prov["options"]["baseURL"], f"http://[::1]:{port}/v1")
        self.assertIn(f"{pid}/coder:7b", models.discover())
        code, out = self.wb("remote", "[::1]", "--port", str(port), "--context", "32768")
        self.assertEqual(code, 0, out)                                   # the same provider, reused
        self.assertEqual(self.wb("remote", "[::1]", "--remove")[0], 0)
        with self.assertRaises(RuntimeError):
            models.remote_root("bad host", 11434)

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

    def test_wb_remote_remove_keeps_a_provider_the_user_repointed_after_wb_remote_made_it(self):
        # #242 (the audit of 1.2): the `remote` mark outlived the provider wb remote made; the
        # user repointed it at another server with a model of their own, and --remove deleted
        # the replacement, its model and their settings
        srv, port = fake_remote_ollama([{"name": "coder:7b"}],
                                       {"coder:7b": {"capabilities": ["tools"], "model_info": {}}})
        self.servers.append(srv)
        pid = models.remote_pid("127.0.0.1")
        self.config({}, {})
        self.assertEqual(self.wb("remote", "127.0.0.1", "--port", str(port), "--context", "32768")[0], 0)
        cfg_path, models_path = Path(os.environ["WB_OPENCODE_CONFIG"]), Path(os.environ["WB_MODELS"])
        cfg = json.loads(cfg_path.read_text())
        cfg["provider"][pid] = {"name": "Ollama on 127.0.0.1", "options": {"baseURL": "http://127.0.0.1:9/v1"},
                                "models": {"mine": {"limit": {"context": 8192}}}}
        cfg_path.write_text(json.dumps(cfg, indent=2))
        over = json.loads(models_path.read_text())
        over[f"{pid}/mine"] = {"sizes": ["small"]}
        models_path.write_text(json.dumps(over, indent=2))
        before = (cfg_path.read_bytes(), models_path.read_bytes())
        code, out = self.wb("remote", "127.0.0.1", "--remove")
        self.assertEqual(code, 1, out)
        self.assertIn("isn't one wb remote added", out)
        self.assertEqual((cfg_path.read_bytes(), models_path.read_bytes()), before)  # byte for byte

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
    tearDown, config = Real.tearDown, Real.config  # not Real's tests again

    def setUp(self):
        Real.setUp(self)
        from workbench import optimize
        self.no_gpu_here = optimize.no_gpu_here
        card = mock.patch.object(optimize, "no_gpu_here", lambda m, seen=None: False)  # the card's
        card.start()  # there, though hardware.gpus lists none (Real's); #809's test says otherwise
        self.addCleanup(card.stop)

    def ollama(self, window, entry=None, loaded=None, extra=None, served=None):
        loaded, posts = loaded if loaded is not None else [], []
        test, tags = self, [{"name": "m"}]

        class H(BaseHTTPRequestHandler):
            def reply(self, body):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(body).encode())

            def do_GET(self):
                self.reply({"/api/version": {"version": "0.40.0"}, "/api/tags": {"models": tags},
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
                        loaded.append({"name": "m", "size": size, "size_vram": min(size, test.CARD),
                                       **({"context_length": served} if served else {})})
                if self.path == "/api/create":
                    tags.append({"name": body["model"]})
                self.reply({"status": "success"} if self.path == "/api/create" else {})

            def log_message(self, *a):
                pass
        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.servers.append(srv)
        self.tags = tags  # what /api/tags lists: a test may repoint a tag (#709)
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

    def test_a_remote_model_another_client_has_loaded_is_skipped_as_busy_and_never_unloaded(self):
        # #944 (an outside review of 1.90.24): busy() left out the model being measured, and the
        # box's lease is this machine's alone, so a run measuring the model another client of the
        # wb remote box was running unloaded and reloaded it at odd windows, several times
        from workbench import optimize
        loaded = [{"name": "m", "size": 5, "size_vram": 5}]                 # another client's
        posts = self.ollama(32768, loaded=loaded, extra={"ollama": {"local": True}})
        with mock.patch.object(models, "_this_host", lambda h: False), \
                mock.patch.object(models, "INFLIGHT_WATCH", 0.3), \
                mock.patch.object(optimize.hardware, "gpus", lambda windows=True: [{"name": "Card", "vram": 1}]), \
                mock.patch.object(sys, "argv", ["optimize", "--all"]):
            m, = optimize.candidates()
            self.assertEqual(optimize.busy(m), ["m"])
            out = io.StringIO()
            with redirect_stdout(out):
                optimize.main()
            self.assertIn(f"{m.id}: not measured, {m.root} has m loaded", out.getvalue())
            self.assertEqual([p for p in posts if p[0] == "/api/generate"], [])   # nothing unloaded
            models._note_mine(m)                       # this machine's own load, idle: measured
            self.assertEqual(optimize.busy(m), [])

    def test_a_machine_with_no_gpu_measures_nothing_and_a_model_on_the_cpu_gets_no_advice(self):
        # #809: on a CPU-only box wb optimize ran by itself, loaded each model into RAM twice, and
        # wb start and wb doctor said each "doesn't fit the GPU whole even at 16K"
        from workbench import optimize
        self.ollama(32768)
        real = mock.patch.object(optimize, "no_gpu_here", self.no_gpu_here)  # as it is
        real.start()
        self.addCleanup(real.stop)
        with mock.patch.object(models.hardware, "gpus", lambda windows=True: []):  # Real's: none
            self.assertTrue(optimize.no_gpu_here(models.discover()["ollama/m"]))
            self.assertEqual(optimize.candidates(), [])
            self.assertEqual(optimize.due(), "")                       # nothing starts by itself
            Path(os.environ["WB_MODELS"]).write_text(json.dumps({"gpu_gb": 12}))  # said by hand
            self.assertEqual([m.id for m in optimize.candidates()], ["ollama/m"])
        with mock.patch.object(models.hardware, "system", lambda: "macos"), \
                mock.patch.object(models.hardware, "gpus", lambda windows=True: [
                    {"name": "AMD Radeon Pro 5500M", "driver": "metal", "vram": 4 * 2 ** 30}]):
            Path(os.environ["WB_MODELS"]).write_text("{}")
            self.assertTrue(optimize.no_gpu_here(models.discover()["ollama/m"]))  # an Intel Mac's
        self.CARD = 0                                                  # Ollama puts it all in RAM
        row = optimize.fit(models.discover()["ollama/m"], 262144)
        self.assertEqual((row["fits"], row.get("cpu")), (None, True))
        self.assertEqual(optimize.advice("ollama/m", row), "")
        self.assertEqual(optimize.advice("ollama/m", {k: v for k, v in row.items() if k != "cpu"}), "")  # an old row
        self.CARD = 5_000_000_000                                      # partly on a GPU: advised
        row = optimize.fit(models.discover()["ollama/m"], 262144)
        self.assertNotIn("cpu", row)
        self.assertIn("doesn't fit the GPU whole even at 16K", optimize.advice("ollama/m", row))

    def test_a_model_the_user_keeps_loaded_is_never_measured_or_unloaded(self):
        # #254 (the audit of 1.2): each probe unloads and reloads its model, so a background
        # measurement dropped the user's autocomplete model; keep_loaded guarded only eviction
        from workbench import optimize
        for keep in (["ollama/m"], ["m"], ["m:latest"]):                    # by id, name or tag
            posts = self.ollama(32768, extra={"keep_loaded": keep})
            with mock.patch.object(optimize.hardware, "gpus", lambda windows=True: [{"name": "Card", "vram": 1}]), \
                    mock.patch.object(sys, "argv", ["optimize", "--all"]):
                self.assertEqual(optimize.candidates(), [], keep)
                with redirect_stdout(io.StringIO()):
                    optimize.main()
            self.assertEqual([b for path, b in posts if path == "/api/generate"], [], keep)  # no unload

    def test_every_hint_to_measure_names_wb_optimize_start_which_is_what_measures(self):
        # #600 (the audit of v1.90): bare `wb optimize` only reports, and its never-run line named
        # the bare command again, so the first run's step 3 measured nothing
        from workbench import optimize
        self.ollama(32768)
        self.assertEqual(optimize.report(), ["not run yet: wb optimize estimate, then wb optimize "
                                             "start (Ollama models only)"])
        with self.assertRaisesRegex(RuntimeError, "wb optimize start first"):
            optimize.apply(models.discover()["ollama/m"])
        with mock.patch.object(models, "holders", return_value={"1": {"task": "t"}}), \
                self.assertRaisesRegex(RuntimeError, "run wb optimize start once"):
            optimize.start()
        skill = (Path(__file__).parent.parent / "skill/workbench/SKILL.md").read_text()
        self.assertIn("`wb optimize start`", skill.split("**First run**")[1].split("4. `wb try`")[0])

    def test_the_first_run_on_a_fresh_profile_starts(self):
        # #198 (the 2026-11-19 audit): optimize.log was opened before the state directory existed,
        # and wb setup swallowed the error, so the first measurement never ran
        from workbench import cli, optimize
        self.ollama(32768)
        os.environ["WB_HOME"] = str(Path(os.environ["WB_HOME"]) / "fresh")
        # the detached run faked, not every process: with subprocess.Popen patched, the `mount`
        # store.network_fs runs on macOS (no /proc/self/mounts) got a mock and raised (#950)
        with mock.patch.object(cli, "detach") as detach, \
                mock.patch.object(models, "_started", return_value="1"):  # not ps on the fake
            detach.return_value.pid = os.getpid()
            self.assertIn("wb optimize: measuring 1 model(s)", optimize.start()[0])
        self.assertIn("workbench.optimize", detach.call_args[0][0])
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
        self.assertIn("registered ollama/m:64k", out)
        self.assertIn(("/api/create", {"model": "m:64k", "from": "m", "stream": False,
                                       "parameters": {"num_ctx": 65536}}), posts)
        entry = models.read_config()["provider"]["ollama"]["models"]["m:64k"]
        self.assertEqual(entry["options"], {"reasoningEffort": "none"})       # thinking stays off
        self.assertEqual(entry["limit"], {"context": 65536, "input": 65536 - 4096, "output": 4096})

    def test_an_applied_variant_shares_its_sources_record_and_policy_and_the_advice_stops(self):
        # #584, the audit of 1.90: the variant started with no record and ranked below the
        # spilling source's one accepted task, it dropped the source's models.json sizes, and
        # every wb start repeated the advice whose command then said "exists already"
        from workbench import optimize
        self.ollama(65536, extra={"ollama/m": {"sizes": ["trivial"], "label": "M"}})
        store.append_jsonl(store.home() / "results.jsonl", {"task": "t1", "model": "ollama/m",
                                                            "size": "trivial", "outcome": "accepted",
                                                            "ran": True})
        m = models.discover()["ollama/m"]
        row = {"window": 65536, "fits": 49152, "spill": 12, "tried": []}
        optimize._record(m.id, row)
        self.assertIn("wb optimize apply ollama/m", optimize.advice(m.id, row))
        said = optimize.apply(m)
        self.assertIn("It shares ollama/m's record, and the router now picks it before ollama/m", said)
        v = models.discover()["ollama/m:48k"]
        self.assertEqual((v.extra["sizes"], v.extra["label"], v.extra["variant_of"]),
                         (["trivial"], "M", "ollama/m"))
        self.assertFalse(router.allowed(v, "significant"))          # the source's policy holds
        self.assertEqual(optimize.advice(m.id, optimize.state()["models"][m.id]), "")
        self.assertEqual([s.model.id for s in router.rank("trivial")][:2], ["ollama/m:48k", "ollama/m"])
        sv, sm = router.score(v, "trivial"), router.score(m, "trivial")
        self.assertEqual((sv.tries, sv.wins, sm.tries), (1, 1, 1))   # one record, both ways
        self.assertEqual(router.lineage(m), {"ollama/m", "ollama/m:48k"})

    def test_a_models_weights_are_known_by_ollamas_digest_and_asked_once_a_while(self):
        # #726: what tells a model swapped under the same id from the one its record is about
        self.ollama(32768)
        m = models.discover()["ollama/m"]
        models._IDS.clear()
        asked = []
        tags = {"models": [{"name": "m", "digest": "0123456789abcdef0123456789"}]}
        with mock.patch.object(models, "_get", lambda url, timeout=2.5: asked.append(url) or tags):
            self.assertEqual(models.identity(m), "ollama:0123456789abcdef")
            self.assertEqual(models.identity(m), "ollama:0123456789abcdef")
            self.assertEqual(len(asked), 1)                                 # within FACTS_TTL
            models._IDS.clear()
            tags["models"][0]["digest"] = "fedcba9876543210fedcba"           # re-pulled
            self.assertEqual(models.identity(m), "ollama:fedcba9876543210")
            models._IDS.clear()
            tags["models"] = []                                             # not pulled: unknown
            self.assertIsNone(models.identity(m))
        models._IDS.clear()

    def test_a_variant_of_a_variant_keeps_the_first_sources_record_and_leads_alone(self):
        # #723 (reproduced): apply on a variant wrote variant_of the variant, lineage followed one
        # level, so the source's tasks left the record, and with both the source and the first
        # variant `replaced` the router went back to the spilling source; retiring a variant
        # dropped its rows from the record
        import io
        from contextlib import redirect_stdout
        from workbench import cli, optimize
        self.ollama(65536)
        for i in range(3):
            store.append_jsonl(store.home() / "results.jsonl", {"task": f"t{i}", "model": "ollama/m",
                                                                "size": "trivial", "outcome": "accepted", "ran": True})
        m = models.discover()["ollama/m"]
        optimize._record(m.id, {"window": 65536, "fits": 49152, "spill": 12, "tried": []})
        optimize.apply(m)
        v1 = models.discover()["ollama/m:48k"]
        store.append_jsonl(store.home() / "results.jsonl", {"task": "t3", "model": v1.id,
                                                            "size": "trivial", "outcome": "accepted", "ran": True})
        optimize._record(v1.id, {"window": 49152, "fits": 32768, "spill": 9, "tried": []})  # a new card
        optimize.apply(v1)
        v2 = models.discover()["ollama/m:32k"]
        self.assertEqual((v2.extra["variant_of"], v2.extra["replaces"]), ("ollama/m", "ollama/m:48k"))
        self.assertEqual(router.lineage(v2), {"ollama/m", "ollama/m:48k", "ollama/m:32k"})
        self.assertEqual(router.score(v2, "trivial").tries, 4)
        self.assertEqual([s.model.id for s in router.rank("trivial")][0], "ollama/m:32k")
        self.assertEqual(router.replaced([v2.id]), {"ollama/m", "ollama/m:48k"})
        # an older apply's chain (variant_of the variant itself) is followed to the source too
        models.set_override(v2.id, "variant_of", v1.id)
        models.set_override(v2.id, "replaces", None)
        self.assertEqual(router.lineage(models.discover()[v2.id]), {"ollama/m", "ollama/m:48k", "ollama/m:32k"})
        self.assertEqual(router.replaced([v2.id]), {"ollama/m", "ollama/m:48k"})
        with redirect_stdout(io.StringIO()):
            cli.main(["model", "retire", v1.id])
        self.assertNotIn(v1.id, models.discover())
        self.assertEqual(router.score(models.discover()["ollama/m"], "trivial").tries, 4)  # its row stays

    def test_a_source_measured_again_after_its_apply_gets_no_advice_to_apply_it_again(self):
        # the review of #584: a run that measured the source again (a changed GPU, --all, a
        # stopped run) wrote its row without `applied`, the advice came back, and the command it
        # named answered "exists already"; the report said the spilling window fit
        from workbench import optimize
        self.ollama(65536)
        m = models.discover()["ollama/m"]
        row = {"window": 65536, "fits": 49152, "spill": 12, "tried": []}
        optimize._record(m.id, row)
        optimize.apply(m)
        optimize._record(m.id, {**row, "ts": store.now()})          # as fit() writes it, again
        self.assertEqual(optimize.advice(m.id, optimize.state()["models"][m.id]), "")
        said = "\n".join(optimize.report())
        self.assertIn("ollama/m: 48K fits all on the GPU here; ollama/m:48k, applied, is that window", said)
        self.assertNotIn("ollama/m: its 64K window fits all on the GPU", said)
        # a different window measured (a new card): that one is advised
        self.assertIn("wb optimize apply ollama/m makes a 32K entry",
                      optimize.advice(m.id, {**row, "fits": 32768}))

    def test_a_smaller_window_cuts_the_output_cap_and_max_tokens_so_input_stays_positive(self):
        # #332 (the audit of 1.4): a 16K fit of a 128K entry wrote an input limit of -16384 and
        # kept its 32K output cap and its 64K max_tokens
        self.ollama(131072, entry={"limit": {"context": 131072, "output": 32768},
                                   "options": {"max_tokens": 65536}})
        m = models.discover()["ollama/m"]
        from workbench import optimize
        optimize._record(m.id, {"window": 131072, "fits": 16384, "spill": 40, "tried": []})
        self.assertIn("registered ollama/m:16k", optimize.apply(m))
        entry = models.read_config()["provider"]["ollama"]["models"]["m:16k"]
        self.assertEqual(entry["limit"], {"context": 16384, "input": 16384 - 4096, "output": 4096})
        self.assertEqual(entry["options"], {"max_tokens": 4096})
        from workbench import runner
        self.assertGreater(runner.compaction_buffer(entry), 0)
        self.assertLess(runner.compaction_buffer(entry), 16384)
        self.assertEqual(optimize.budget({"limit": {"context": 8192, "output": 1024}}, 8192)[0]["input"], 7168)
        kept = optimize.budget({"limit": {"output": 20480}, "options": {"max_tokens": 20480}}, 65536)
        self.assertEqual((kept[0]["output"], kept[1]["max_tokens"]), (20480, 20480))  # room left
        # half the window leaves under a step's growth past the earliest handover point (#644)
        cut = optimize.budget({"limit": {"output": 32768}, "options": {"max_tokens": 32768}}, 65536)
        self.assertEqual((cut[0]["output"], cut[1]["max_tokens"]), (16384, 16384))

    def test_two_tags_of_one_model_get_names_of_their_own_and_a_taken_name_is_refused(self):
        # the 2026-11-19 audit: family:7b and family:32b both became family-16k, the second
        # overwriting the first in Ollama and in OpenCode's config
        from workbench import optimize
        self.assertEqual([optimize.entry_name(n, 16384) for n in
                          ("family:7b", "family:32b", "family:32b-q4_K_M", "family:32b-24k", "family-24k", "m")],
                         ["family:7b-16k", "family:32b-16k", "family:32b-q4_K_M-16k", "family:32b-16k",
                          "family:16k", "m:16k"])
        self.ollama(32768)
        m = models.discover()["ollama/m"]
        optimize._record(m.id, optimize.fit(m, 262144))
        optimize.apply(m)
        with self.assertRaisesRegex(RuntimeError, "ollama/m:64k exists already"):
            optimize.apply(m)

    def test_apply_checks_the_config_first_and_a_retry_registers_the_variant_it_made(self):
        # #266 (the audit of 1.2): the variant was made, then a config with comments refused its
        # entry, and once the config was plain JSON the retry refused the variant it had made
        from workbench import optimize
        posts = self.ollama(32768)
        m = models.discover()["ollama/m"]
        optimize._record(m.id, optimize.fit(m, 262144))
        cfg = Path(os.environ["WB_OPENCODE_CONFIG"])
        plain = cfg.read_text()
        cfg.write_text("// the user's notes\n" + plain)
        with self.assertRaisesRegex(RuntimeError, "has comments"):
            optimize.apply(m)
        self.assertNotIn("/api/create", [p for p, _ in posts])           # nothing made
        cfg.write_text(plain)
        # a variant made and left unregistered (the process died between the two): finished
        with mock.patch.object(models, "write_entry", side_effect=RuntimeError("killed")):
            with self.assertRaisesRegex(RuntimeError, "killed"):
                optimize.apply(m)
        self.assertEqual([p for p, _ in posts].count("/api/create"), 1)
        self.assertIn("registered ollama/m:64k", optimize.apply(m))
        self.assertEqual([p for p, _ in posts].count("/api/create"), 1)  # not made twice
        self.assertIn("m:64k", models.read_config()["provider"]["ollama"]["models"])
        self.assertNotIn("ollama/m:64k", optimize.state().get("made") or {})
        # a model of that name the user made: never taken over
        models.write_entry("ollama", "m:64k", None)
        with self.assertRaisesRegex(RuntimeError, "exists already"):
            optimize.apply(m)

    def test_a_window_the_server_never_served_is_not_recorded_as_a_fit(self):
        # #338 (the audit of 1.4): Ollama served 16K at every load, all in VRAM, and the fit
        # copied the asked-for 64K into its result, checking only placement
        self.ollama(65536, served=16384)
        m = models.discover()["ollama/m"]
        from workbench import optimize
        row = optimize.fit(m, 262144)
        self.assertEqual((row["window"], row["fits"], row["clamped"]), (65536, 16384, 16384))
        self.assertIn("served it 16K whatever was asked", optimize.advice(m.id, row))

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
        from workbench import cli, optimize
        gpu = [{"name": "Card A", "vram": 16e9}]
        with mock.patch.object(optimize.hardware, "gpus", lambda windows=True: gpu):
            self.assertEqual(optimize.due(), "never measured")
            m = models.discover()["ollama/m"]
            optimize._record(m.id, optimize.fit(m, 262144))
            store.write_json(optimize.path(), {**optimize.state(), "done": 1, "stopped": None,
                                               "hardware": optimize.fingerprint()})
            self.assertEqual(optimize.due(), "")
            with mock.patch.object(cli, "detach") as detach, \
                mock.patch.object(models, "_started", return_value="1"):  # not ps on the fake
                self.assertEqual(optimize.start(if_due=True), [])            # nothing to do: quiet
                detach.assert_not_called()
            optimize.apply(m)                                    # its own entry: measured already
            self.assertEqual(optimize.due(), "")
            gpu[0] = {"name": "Card B", "vram": 24e9}
            self.assertEqual(optimize.due(), "the GPU changed")
            with mock.patch.object(models, "holders", lambda *a: {"1": {"task": "t"}}):
                self.assertEqual(optimize.start(if_due=True), [])            # busy: next session
            with mock.patch.object(cli, "detach") as detach, \
                mock.patch.object(models, "_started", return_value="1"):  # not ps on the fake
                detach.return_value.pid = os.getpid()
                out = optimize.start(if_due=True)
            # the model and the variant apply made (the fake lists what /api/create made: #266)
            self.assertIn("measuring the GPU for 2 local model(s) (the GPU changed)", out[0])
            self.assertIn("a task you start goes first", out[0])
            self.assertNotIn("--all", detach.call_args[0][0])

    def test_a_fit_is_due_again_when_its_models_window_or_weights_change_and_only_that_one(self):
        # #709: the fit was keyed to the GPUs alone: an entry raised from 32K to 64K, or a tag
        # repointed at other weights, kept its old fit and no run was due
        from workbench import optimize
        self.ollama(32768)
        self.tags[0]["digest"] = "sha256:aaa"
        gpu = [{"name": "Card A", "vram": 16e9}]
        cfg_path = Path(os.environ["WB_OPENCODE_CONFIG"])

        def window(n):
            cfg = json.loads(cfg_path.read_text())
            cfg["provider"]["ollama"]["models"]["m"]["limit"]["context"] = n
            cfg_path.write_text(json.dumps(cfg))
        with mock.patch.object(optimize.hardware, "gpus", lambda windows=True: gpu):
            m = models.discover()["ollama/m"]
            optimize._record(m.id, {**optimize.fit(m, 262144), "digest": optimize.digest(m)})
            store.write_json(optimize.path(), {**optimize.state(), "done": 1, "stopped": None,
                                               "hardware": optimize.fingerprint()})
            self.assertEqual(optimize.due(), "")
            window(65536)
            self.assertEqual(optimize.due(), "ollama/m's window changed")
            window(32768)
            self.assertEqual(optimize.due(), "")
            self.tags[0]["digest"] = "sha256:bbb"           # ollama pull m: other weights, one name
            self.assertEqual(optimize.due(), "ollama/m's weights changed")
            del optimize.state()["models"][m.id]["digest"]  # a fit from before: its window alone
            rows = optimize.state()["models"]
            self.assertEqual(optimize.stale(m, {k: v for k, v in rows[m.id].items() if k != "digest"}), "")

    def test_a_model_it_could_not_measure_is_remembered_and_not_due_again_each_session(self):
        # #583, the audit of 1.90: a model skipped (its remote server busy) left no row and was
        # "new" at every session, whose run emptied this machine's GPU first; one whose load
        # failed (HTTP 500) stopped the run, so the next measured everything again
        from workbench import optimize
        gpu = lambda windows=True: [{"name": "Card", "vram": 1}]  # noqa: E731
        loaded = [{"name": "theirs", "size": 5, "size_vram": 5}]           # the other machine's own
        self.ollama(32768, loaded=loaded, extra={"ollama": {"local": True}})
        with mock.patch.object(models, "_this_host", lambda h: False), \
                mock.patch.object(optimize.hardware, "gpus", gpu), \
                mock.patch.object(models, "free_gpu") as free, \
                mock.patch.object(sys, "argv", ["optimize"]):
            m, = optimize.candidates()
            self.assertTrue(m.remote)
            with redirect_stdout(io.StringIO()) as out:
                optimize.main()
            self.assertIn(f"{m.id}: not measured, {m.root} has theirs loaded", out.getvalue())
            free.assert_not_called()                  # only a remote model was due: nothing here
            st = optimize.state()
            self.assertEqual(st["skipped"][m.id]["outcome"], "busy")
            self.assertGreater(st["skipped"][m.id]["retry_after"], store.now() + 3600)
            self.assertIsNone(st["stopped"])
            self.assertEqual(optimize.due(), "")                     # not due at the next session
            self.assertIn("not measured", "\n".join(optimize.report()))
            store.write_json(optimize.path(), {**st, "skipped": {m.id: {**st["skipped"][m.id],
                                                                        "retry_after": 1}}})
            self.assertEqual(optimize.due(), f"new model: {m.id}")   # its time came: tried again
            loaded.clear()
            with redirect_stdout(io.StringIO()):
                optimize.main()
            self.assertIn(m.id, optimize.state()["models"])
            free.assert_not_called()               # measured remotely: this machine's GPU untouched

    def test_one_models_failed_load_is_recorded_and_the_run_carries_on(self):
        import urllib.error
        from workbench import optimize
        self.ollama(32768)
        gpu = lambda windows=True: [{"name": "Card", "vram": 1}]  # noqa: E731
        for err, outcome in ((RuntimeError("http://x answered HTTP 500: out of memory"), "failed"),
                             (urllib.error.URLError(TimeoutError("timed out")), "unreachable")):
            with self.subTest(outcome), mock.patch.object(optimize.hardware, "gpus", gpu), \
                    mock.patch.object(models, "free_gpu") as free, \
                    mock.patch.object(optimize, "fit", side_effect=err), \
                    mock.patch.object(sys, "argv", ["optimize"]):
                optimize.path().unlink(missing_ok=True)
                with redirect_stdout(io.StringIO()) as out:
                    optimize.main()                          # no traceback: the run finishes
                self.assertIn("ollama/m: not measured", out.getvalue())
                free.assert_called_once()                    # a local model: freed for its probe
                st = optimize.state()
                self.assertEqual((st["skipped"]["ollama/m"]["outcome"], st["stopped"]), (outcome, None))
                self.assertTrue(st["done"])
                self.assertEqual(optimize.due(), "")
        with mock.patch.object(optimize.hardware, "gpus", gpu), mock.patch.object(models, "free_gpu"), \
                mock.patch.object(sys, "argv", ["optimize", "--all"]), redirect_stdout(io.StringIO()):
            optimize.main()                                  # asked for: measured, the record cleared
        self.assertIn("ollama/m", optimize.state()["models"])
        self.assertEqual(optimize.state()["skipped"], {})

    def test_real_work_stops_it_and_what_it_measured_stays(self):
        self.ollama(32768)
        m = models.discover()["ollama/m"]
        from workbench import cli, optimize
        held = {"123": {"task": optimize.HOLDER, "model": None, "start": None}}
        waited = []  # pid 123 is a real process here (a kernel thread): waiting on it took all of
        # OPTIMIZE_EXIT's 30 s (2026-11-21). That it is waited for is #161's
        with mock.patch.object(cli, "_gpu_holders", lambda me=None, of=None: held), \
                mock.patch.object(optimize, "stop", lambda: 123) as stop, \
                mock.patch.object(cli, "wait_exit", lambda pid, secs: waited.append((pid, secs))):
            self.assertEqual(cli._preempt_bench(m, "t"),
                             "note: stopped wb optimize (what it measured is kept) to make room")
        self.assertEqual(waited, [(123, cli.OPTIMIZE_EXIT)])


def _strict(name):
    """Path.<name> as Python 3.11-3.13 have it: a PermissionError from stat (an unreadable
    parent) is raised, not read as False as 3.14 does, so the test fails on any Python."""
    real = getattr(Path, name)

    def probe(self, *a, **kw):
        try:
            os.stat(self)
        except PermissionError:
            raise
        except OSError:
            pass
        return real(self, *a, **kw)
    return probe


@unittest.skipIf(os.geteuid() == 0, "root reads a 000 directory")
class UnreadableParents(Base):
    def test_a_probe_under_a_directory_this_account_cant_read_reads_as_absent_on_every_python(self):
        # #850: before 3.14, Path.is_dir/exists/is_file raise PermissionError under an unreadable
        # parent: an admin's 0750 /var/lib/workbench or /etc/opencode failed every wb command,
        # and another account's ~/bin on PATH failed every lane
        tmp = Path(self.tmp.name)
        locked = tmp / "locked"
        (locked / "x" / "bin").mkdir(parents=True)
        locked.chmod(0)
        self.addCleanup(lambda: locked.exists() and locked.chmod(0o755))  # Base's tearDown may go first
        os.environ.pop("WB_GPU_DIR", None)
        os.environ.pop("WB_OPENCODE_CONFIG", None)
        os.environ["XDG_CONFIG_HOME"] = str(tmp / "xdg")
        with mock.patch.object(Path, "exists", _strict("exists")), \
                mock.patch.object(Path, "is_dir", _strict("is_dir")), \
                mock.patch.object(Path, "is_file", _strict("is_file")), \
                mock.patch.object(models, "SHARED_GPU", locked / "gpu"), \
                mock.patch.object(models, "_system_dir", return_value=locked / "opencode"):
            self.assertEqual(models.lease_dir(), store.home())
            self.assertEqual(models.opencode_config_files(), [])
            self.assertEqual(models.agent_providers(), {})
            self.assertEqual(context.python_envs({"PATH": f"{locked / 'x' / 'bin'}:/usr/bin"}), [])


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


class StopAnExitedGroup(unittest.TestCase):
    """runner._stop on a group with nothing left in it, as macOS answers it."""

    def test_a_group_whose_members_all_exited_is_stopped_without_an_error(self):
        # the macOS nightly (2026-11-22): every failed-compaction handover crashed the runner with
        # PermissionError from os.killpg, macOS's EPERM for a group whose members had all exited
        # (Linux says ESRCH, ProcessLookupError), and the task failed instead of handing over
        from workbench import runner
        # killpg is faked, so the group's state doesn't matter (os.waitid, to wait for the exit
        # without reaping it, doesn't exist on macOS: this test errored there, 2026-11-22)
        proc = subprocess.Popen(["true"], start_new_session=True)
        signals = []

        def killpg(pgid, sig):
            signals.append(sig)
            raise PermissionError(1, "Operation not permitted")
        with mock.patch.object(runner.os, "killpg", killpg):
            runner._stop(proc)
        self.assertEqual(signals[0], signal.SIGTERM)
        self.assertEqual(proc.wait(timeout=5), 0)


class Launchers(unittest.TestCase):
    """bin/wb and bin/wb-ask-mcp on an interpreter the package can't run on: macOS's stock 3.9,
    Ubuntu 22.04's 3.10, native Windows (the 2026-11-16 audit)."""
    ROOT = Path(__file__).resolve().parent.parent

    def launch(self, name, setup, path=""):
        import subprocess
        code = f"import runpy, sys; {setup}; sys.argv = [{name!r}, '--version']; runpy.run_path({str(self.ROOT / 'bin' / name)!r})"
        return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30,
                              env={**os.environ, "PATH": path})

    def test_an_old_python_is_told_the_version_it_needs_before_any_import(self):
        for name in ("wb", "wb-ask-mcp"):
            r = self.launch(name, "sys.version_info = (3, 10, 12)")
            self.assertEqual(r.returncode, 78, r.stderr)  # EX_CONFIG: the chat unit stops on it
            self.assertIn(f"{name}: needs Python 3.11 or newer", r.stderr)
            self.assertNotIn("Traceback", r.stderr)

    def test_an_old_python3_hands_over_to_the_newest_python3_n_on_path_once(self):
        # #259 (the audit of 1.2): Ubuntu 22.04's python3 is 3.10, RHEL 9's 3.9, and wb refused
        # though the user had installed python3.12 beside it
        with tempfile.TemporaryDirectory() as d:
            old, new = Path(d, "a"), Path(d, "b")
            for where, minor in ((old, 11), (new, 12), (new, 9)):
                where.mkdir(exist_ok=True)
                exe = where / f"python3.{minor}"
                exe.write_text(f"#!/bin/sh\necho python3.{minor} \"$@\"\n")
                exe.chmod(0o755)
            for name in ("wb", "wb-ask-mcp"):
                r = self.launch(name, "sys.version_info = (3, 10, 12)", f"{old}:{new}")
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(r.stdout.split(), ["python3.12", "-X", "wb-python",
                                                    str(self.ROOT / "bin" / name), "--version"])
                r = self.launch(name, "sys.version_info = (3, 10, 12); sys._xoptions = {'wb-python': True}",
                                f"{old}:{new}")
                self.assertEqual(r.returncode, 78, r.stderr)                # handed over once only

    def test_a_python3_n_that_cannot_run_is_passed_over_for_one_that_can(self):
        # #526: on Ubuntu 22.04 with pyenv, ~/.pyenv/shims/python3.12 (a version pyenv has only
        # for another project) exited 127, and the apt python3.11 behind it was never tried
        with tempfile.TemporaryDirectory() as d:
            shims, apt, broken = Path(d, "shims"), Path(d, "apt"), Path(d, "partial")
            fakes = {shims / "python3.12": 'echo "pyenv: python3.12: command not found" >&2; exit 127\n',
                     broken / "python3.13": '[ "$1" = -c ] && exit 1\necho python3.13 "$@"\n',  # no ctypes
                     apt / "python3.11": 'echo python3.11 "$@"\n'}
            for exe, body in fakes.items():
                exe.parent.mkdir(exist_ok=True)
                exe.write_text("#!/bin/sh\n" + body)
                exe.chmod(0o755)
            for name in ("wb", "wb-ask-mcp"):
                r = self.launch(name, "sys.version_info = (3, 10, 12)", f"{shims}:{broken}:{apt}")
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(r.stdout.split(), ["python3.11", "-X", "wb-python",
                                                    str(self.ROOT / "bin" / name), "--version"])
                r = self.launch(name, "sys.version_info = (3, 10, 12)", f"{shims}:{broken}")
                self.assertEqual(r.returncode, 78, r.stderr)              # none runs: say which
                self.assertIn(f"tried {broken / 'python3.13'}, {shims / 'python3.12'}", r.stderr)

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
        self.assertEqual(prevent, [["78"]])

    def test_no_note_says_the_chat_exits_75_since_it_re_execs_in_place(self):
        # #480 (the audit of v1.8): the unit and ARCHITECTURE.md still said chat.py exits 75 to be
        # restarted, after #200 made it re-exec in place
        import re
        chat = (self.ROOT / "workbench" / "chat.py").read_text()
        self.assertIn("os.execv(sys.executable, [sys.executable, *sys.orig_argv[1:]])", chat)
        self.assertNotRegex(chat, r"(sys\.)?exit\(75\)")
        for f in ("systemd/wb-chat.service", "ARCHITECTURE.md"):
            text = (self.ROOT / f).read_text()
            self.assertIsNone(re.search(r"exits 75|never 75|the replan exit", text), f)

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

    def test_a_model_that_never_loaded_says_resume_and_its_discard_is_not_held_against_it(self):
        # #581, the audit of 1.90: a task whose model never loaded, discarded, counted as the
        # model's rejected work
        from workbench import tuning
        self.tags.clear()                                             # not pulled: fails at once
        self.start("t-noload", "ok", "--test", TEST_CMD, "--model", "o/m", "--force")
        wait_phase("t-noload", {"failed"}, timeout=30)
        note = [r for r in store.notices() if r["task"] == "t-noload"][-1]
        self.assertIn("the agent never ran: once the model can, wb resume t-noload starts it",
                      note["text"])
        run_wb("discard", "t-noload")
        row = [r for r in store.results() if r["task"] == "t-noload"][-1]
        self.assertEqual((row["ran"], row["failed_before"]), (False, "load"))
        self.assertFalse(tuning.judged(row))
        self.assertEqual(router.score(models.resolve("o/m"), row["size"]).tries, 0)


class ServerRestarted(Base):
    """A model server wb starts itself (models.json `start`) that dies under a round: #579, the
    audit of 1.90. Only Ollama's and servers wb can't start were revived; an out-of-memory kill of
    a llama-server failed the task as "opencode exited 1"."""
    def setUp(self):
        super().setUp()
        os.environ.pop("WB_FAKE_MODELS")
        self.down = Path(self.tmp.name) / "down"
        self.starts = Path(self.tmp.name) / "starts"
        srv, url = serve({"/v1/models": lambda h: 503 if self.down.exists() else {"data": [{"id": "m"}]}})
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        Path(os.environ["WB_OPENCODE_CONFIG"]).write_text(json.dumps({"provider": {"s": {
            "options": {"baseURL": f"{url}/v1"}, "models": {"m": {"limit": {"context": 32768}}}}}}))
        Path(os.environ["WB_MODELS"]).write_text(json.dumps({"s": {"engine": "server"}, "s/m": {
            "start": f"echo >> {self.starts}; rm -f {self.down}"}}))

    def test_a_server_wb_starts_that_dies_under_a_round_is_started_again_and_the_round_rerun(self):
        os.environ["FAKE_DOWN"] = str(self.down)                      # the round takes it down
        self.start("t-oom", "crash", "--test", TEST_CMD, "--model", "s/m")
        st = wait_phase("t-oom", {"review", "stuck", "failed"}, timeout=90)
        self.assertEqual(st["phase"], "review", st.get("last"))
        self.assertTrue(st["tests"]["ok"])                              # the round, run again
        self.assertEqual(st["round"], 1)
        self.assertEqual(self.starts.read_text().count("\n"), 1)        # started once, by wb
        rows, _ = store.read_jsonl(store.Task("t-oom").dir / "events.jsonl")
        self.assertIn("opencode exited 1 with the model's server down: wb starts it again and the "
                      "round runs again", [r["text"] for r in rows if r.get("kind") == "load"])

    def test_a_server_that_dies_after_the_models_first_words_is_revived_too(self):
        # #657: the revive needed a round with no text, but an out-of-memory kill comes late in a
        # round, after "Let me look at the parser": the task failed and counted against the model
        os.environ["FAKE_DOWN"] = str(self.down)
        self.start("t-talk", "crashtalk", "--test", TEST_CMD, "--model", "s/m")
        st = wait_phase("t-talk", {"review", "stuck", "failed"}, timeout=90)
        self.assertEqual(st["phase"], "review", st.get("last"))
        self.assertTrue(st["tests"]["ok"])                              # the round, run again
        self.assertEqual(self.starts.read_text().count("\n"), 1)

    def test_a_round_left_silent_by_a_server_that_died_is_revived_not_reported_stuck(self):
        # #657: OpenCode retrying a server that is gone, until the silence guard stops the round
        os.environ.update(FAKE_DOWN=str(self.down), FAKE_CRASH_HANG="1")
        self.start("t-hush", "crashtalk", "--test", TEST_CMD, "--model", "s/m",
                   "--silence-timeout", "3")
        st = wait_phase("t-hush", {"review", "stuck", "failed"}, timeout=90)
        self.assertEqual(st["phase"], "review", st.get("last"))
        rows, _ = store.read_jsonl(store.Task("t-hush").dir / "events.jsonl")
        self.assertIn("the round went silent with the model's server down: wb starts it again and "
                      "the round runs again", [r["text"] for r in rows if r.get("kind") == "load"])

    def test_a_second_crash_in_the_same_round_still_fails_the_task(self):
        os.environ["FAKE_DOWN"] = str(self.down)
        # the start brings the server back but the crash marker goes too: it crashes again
        Path(os.environ["WB_MODELS"]).write_text(json.dumps({"s": {"engine": "server"}, "s/m": {
            "start": f"echo >> {self.starts}; rm -f {self.down} {self.down}.crashed"}}))
        self.start("t-oom2", "crash", "--test", TEST_CMD, "--model", "s/m")
        st = wait_phase("t-oom2", {"review", "stuck", "failed"}, timeout=90)
        self.assertEqual((st["phase"], st["last"]), ("failed", "opencode exited 1"))
        self.assertEqual(self.starts.read_text().count("\n"), 1)        # revived once a round
        # the machine's failure, not the step's: the model's slice isn't shrunk for it (#657)
        self.assertEqual(st.get("infra"), "model")
        note = [r for r in store.notices() if r["task"] == "t-oom2"][-1]
        self.assertEqual(note["cause"], "server")
        self.assertFalse(results.step(store.Task("t-oom2"), False)["stuck"])


class WindowsServer(Base):
    """#663: on WSL2's default NAT networking a server on Windows never answers on 127.0.0.1, so
    first run said no server answers here, and Claude proposed a second one inside WSL."""

    def test_the_windows_host_is_read_from_the_default_route_else_the_resolver(self):
        tmp = Path(self.tmp.name)
        route, resolv = tmp / "route", tmp / "resolv.conf"
        route.write_text("Iface\tDestination\tGateway \tFlags\n"
                         "eth0\t0010A8C0\t00000000\t0001\neth0\t00000000\t0110A8C0\t0003\n")
        resolv.write_text("# generated by WSL\nnameserver 172.20.0.1\n")
        with mock.patch.object(models, "ROUTE", route), mock.patch.object(models, "RESOLV", resolv):
            with mock.patch.object(models.hardware, "system", return_value="linux"):
                self.assertIsNone(models.windows_host())
            with mock.patch.object(models.hardware, "system", return_value="wsl"):
                self.assertEqual(models.windows_host(), "192.168.16.1")
                route.write_text("Iface\tDestination\tGateway\n")
                self.assertEqual(models.windows_host(), "172.20.0.1")

    def test_a_server_at_the_windows_host_is_found_and_said_to_need_local(self):
        from workbench import cli
        os.environ.pop("WB_FAKE_MODELS")
        srv, url = serve({"/v1/models": {"data": [{"id": "qwen-coder"}]}})
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        port = srv.server_address[1]
        try:  # the Windows host, played by another loopback address (Linux has them all)
            win = ThreadingHTTPServer(("127.0.0.2", 0), srv.RequestHandlerClass)
        except OSError:
            self.skipTest("no 127.0.0.2 here")
        threading.Thread(target=win.serve_forever, daemon=True).start()
        self.addCleanup(win.server_close)
        self.addCleanup(win.shutdown)
        srv.shutdown()                                  # nothing answers on 127.0.0.1
        wport = win.server_address[1]
        with mock.patch.object(models, "SERVER_PORTS", ((wport, "LM Studio"),)), \
                mock.patch.object(models, "windows_host", return_value="127.0.0.2"):
            on_path, found = models.servers_here()
            self.assertEqual(found, [{"root": f"http://127.0.0.2:{wport}", "usually": "LM Studio",
                                      "models": ["qwen-coder"], "windows": "127.0.0.2"}])
            line = cli.server_lines({})[-1]
            self.assertIn(f"127.0.0.2:{wport} answers on Windows (usually LM Studio): qwen-coder", line)
            self.assertIn("it needs local: true (wb remote 127.0.0.2", line)
            Path(os.environ["WB_OPENCODE_CONFIG"]).write_text("{}")
            with mock.patch.object(models, "ollama_answers", return_value=False):
                first = "\n".join(cli.first_run())
            self.assertIn(f"--  a model server answers on Windows at http://127.0.0.2:{wport}", first)
            self.assertNotIn("ok  a model server answers", first)
        with mock.patch.object(models, "SERVER_PORTS", ((port, "LM Studio"),)), \
                mock.patch.object(models, "windows_host", return_value="127.0.0.3"), \
                mock.patch.object(models, "ollama_answers", return_value=False):
            self.assertIn("networkingMode=mirrored", cli.server_lines({})[-1])  # nothing at all
            self.assertIn("networkingMode=mirrored", "\n".join(cli.first_run()))


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

    def test_machines_sharing_a_network_state_dir_each_budget_from_their_own_speeds(self):
        # #901: speed.json was one file for every machine on a network home, so a fast desktop's
        # samples outvoted a slow laptop's in the median and the laptop's rounds got the defaults
        m = models.resolve("mimo")
        router.record_speed(m.id, self.session(15, 150))         # measured before #901: shared
        with mock.patch.object(store, "network_fs", return_value="nfs"):
            with mock.patch.object(store, "machine", return_value="desktop"):
                self.assertEqual(router.speed(m.id), {"write": 15.0, "read": 150})  # until its own
                router.record_speed(m.id, self.session(60, 2800, n=20))
                self.assertEqual(router.budgets(m)["think"], 420)
            with mock.patch.object(store, "machine", return_value="laptop"):
                router.record_speed(m.id, self.session(15, 150))
                self.assertEqual(router.speed(m.id), {"write": 15.0, "read": 150})
                self.assertEqual(router.budgets(m)["think"], int(13500 / 15 + 32768 / 150))
        self.assertTrue((store.home() / "machines" / "laptop" / "speed.json").exists())

    def test_this_machines_row_for_a_variants_source_wins_over_the_shared_files_row_for_the_variant(self):
        # reviewing the merge of #901/#905/#906 (2026-11-21): the docs said the shared file's row
        # was taken before the chain of earlier ids; speed() runs the whole chain in this
        # machine's file first, its own measurements, and the docs now say so
        var, src = "ollama/mimo-v2.6:9b-q8_0-fit", models.resolve("mimo").id
        Path(os.environ["WB_MODELS"]).write_text(json.dumps({var: {"variant_of": src}}))
        store.write_json(store.home() / "speed.json", {var: {"write": [60.0], "read": [2800]}})
        with mock.patch.object(store, "network_fs", return_value="nfs"), \
                mock.patch.object(store, "machine", return_value="laptop"):
            self.assertEqual(router.speed(var), {"write": 60.0, "read": 2800})   # nothing here yet
            router.record_speed(src, self.session(15, 150))
            self.assertEqual(router.speed(var), {"write": 15.0, "read": 150})

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

    def test_a_task_measured_slow_after_it_started_gets_the_longer_limits_in_its_next_round(self):
        # #814: the limits were fixed at wb start, so a first task whose round measured the model
        # slow kept 900 s of silence in every later round; a limit a flag gave stays the user's
        self.start("t-later", "ok", "--round-timeout", "3000")
        spec = store.Task("t-later").spec
        self.assertEqual(spec["timeouts_given"], ["round"])
        self.assertEqual(spec["silence_timeout"], 900)
        wait_phase("t-later", {"review"})
        self.assertIsNone(store.Task("t-later").status.get("limits"))   # not measured: as started
        m = models.resolve("mimo")
        router.record_speed(m.id, self.session(15, 150, n=40))
        b = router.budgets(m)
        run_wb("feedback", "t-later", "--text", "1. Again.")
        st = wait_phase("t-later", {"review"})
        self.assertEqual(st["limits"], {"think": b["think"], "silence": b["silence"]})
        layer = json.loads((context.rules_dir(store.Task("t-later")) / "opencode.json").read_text())
        self.assertEqual(layer["provider"]["ollama"]["options"]["chunkTimeout"], (b["silence"] + 60) * 1000)
        evs, _ = store.read_jsonl(store.Task("t-later").dir / "events.jsonl")
        self.assertTrue(any(e["kind"] == "limits" and e["round"] == 2 for e in evs))

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
