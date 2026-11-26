"""The web app's admin endpoints (#1280): GET and POST /api/settings, GET /api/stats and the live
view's GET /api/top, the user's token alone; agent tokens on an allow-list (AGENT_MAY), so an
endpoint added later is never open to them by default. A real `wb chat serve` on a temp WB_HOME."""
import contextlib
import http.client
import io
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # run as a script too

from workbench import chat, cli, settings, store  # noqa: E402
from tests.test_workbench import ROOT, Base, run_wb  # noqa: E402


def free_port() -> int:
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        return sk.getsockname()[1]


def children(pid: int, has: str) -> list[int]:
    """``pid``'s children whose command line has ``has`` (ps: Linux and macOS alike)."""
    out = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,args="], capture_output=True, text=True).stdout
    return [int(p) for p, pp, args in (ln.split(None, 2) for ln in out.splitlines() if len(ln.split(None, 2)) == 3)
            if int(pp) == pid and has in args]


class ChatAdmin(Base):
    def setUp(self):
        super().setUp()
        tmp = Path(self.tmp.name)
        # the server's wb children take the config files' locks here, never in the user's runtime dir
        (tmp / "run").mkdir(mode=0o700)
        # no probe on its PATH, whatever this machine has: Agent code navigation turned on needs help
        path = os.pathsep.join(d for d in os.environ.get("PATH", "").split(os.pathsep)
                               if d and not (Path(d) / "probe").exists())
        self.env = {**os.environ, "XDG_RUNTIME_DIR": str(tmp / "run"), "PATH": path}
        self.port = free_port()
        self.server = subprocess.Popen([sys.executable, str(ROOT / "bin" / "wb"), "chat", "serve", "--port",
                                        str(self.port), "--bind", "127.0.0.1"], env=self.env,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.addCleanup(self.stop)
        deadline = time.time() + 20
        while not chat.running(self.port):
            self.assertIsNone(self.server.poll(), self.server.stderr.read().decode() if self.server.poll() else "")
            self.assertLess(time.time(), deadline, "the server never answered")
            time.sleep(0.1)
        self.user = chat.token()

    def stop(self):
        """The server and its children (the live view's), gone before the test's directory is."""
        kids = children(self.server.pid, "")
        self.server.terminate()
        try:
            self.server.wait(10)
        except subprocess.TimeoutExpired:
            self.server.kill()
            self.server.wait()
        self.server.stderr.close()
        for k in kids:
            with contextlib.suppress(ProcessLookupError):
                os.kill(k, signal.SIGKILL)

    def call(self, path, body=None, token=None, method=None, ctype="application/json"):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        headers = {"Host": f"localhost:{self.port}"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if body is not None:
            headers["Content-Type"] = ctype
        c.request(method or ("POST" if body is not None else "GET"), path,
                  (body if isinstance(body, str) else json.dumps(body)) if body is not None else None, headers)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, (json.loads(data) if r.getheader("Content-Type") == "application/json" else data)

    @staticmethod
    def row(rows, key):
        return next(r for r in rows if r["key"] == key)

    def test_the_owners_get_and_post_of_settings_round_trip_a_coding_change_and_record_it(self):
        code, got = self.call("/api/settings", token=self.user)
        self.assertEqual(code, 200)
        self.assertIsInstance(got["ts"], float)
        self.assertEqual(self.row(got["rows"], "coding")["value"], "decide")
        code, r = self.call("/api/settings", {"key": "coding", "value": "ask"}, token=self.user)
        self.assertEqual(code, 200, r)
        self.assertTrue(r["ok"])
        self.assertIsInstance(r["said"], str)
        self.assertEqual(self.row(r["rows"], "coding")["value"], "ask")
        self.assertNotIn("help", r)
        self.assertEqual(self.row(self.call("/api/settings", token=self.user)[1]["rows"], "coding")["value"], "ask")
        self.assertEqual(cli.coding_mode(), "ask")                         # wb itself says so
        rec = store.read_jsonl(chat.changes_path())[0]
        self.assertEqual([{k: x[k] for k in ("key", "label", "from", "to", "by")} for x in rec],
                         [{"key": "coding", "label": "Claude coding", "from": "decide", "to": "ask", "by": "web"}])
        self.assertEqual(rec[0]["said"], r["said"])
        # nothing runs for a row that isn't, or a value it doesn't take; neither is recorded
        self.assertEqual(self.call("/api/settings", {"key": "nope", "value": "x"}, token=self.user),
                         (400, {"error": "no such setting", "code": "bad_request"}))
        self.assertEqual(self.call("/api/settings", {"key": "coding", "value": "sometimes"}, token=self.user),
                         (400, {"error": "not one of its choices", "code": "bad_request"}))
        self.assertEqual(self.call("/api/settings", {"key": "coding", "value": True}, token=self.user)[0], 400)
        self.assertEqual(len(store.read_jsonl(chat.changes_path())[0]), 1)

    def test_a_change_made_at_the_terminal_shows_on_the_next_read_not_the_cached_rows_1392(self):
        # #1392: Settings beside the terminal drew the rows cached before `wb coding ask` ran there,
        # and a click to put the shown value back sent nothing
        self.assertEqual(self.row(self.call("/api/settings", token=self.user)[1]["rows"], "coding")["value"], "decide")
        run_wb("coding", "ask")
        self.assertEqual(self.row(self.call("/api/settings", token=self.user)[1]["rows"], "coding")["value"], "ask")

    def test_agent_code_navigation_turned_on_without_probe_carries_claudes_help_into_the_record(self):
        # #1280's addendum (the owner, 2026-11-24: "the terminal still offers to help"): the mod
        # turns the row into a [workbench] turn where Claude offers to set probe up (#1283)
        code, r = self.call("/api/settings", {"key": "probe", "value": "on"}, token=self.user)
        self.assertEqual(code, 200, r)
        self.assertIn("not set up", r["said"])
        self.assertIn("install nothing without their yes", r["help"])
        rec = store.read_jsonl(chat.changes_path())[0][-1]
        self.assertEqual((rec["key"], rec["from"], rec["to"], rec["by"], rec["help"]), ("probe", "off", "on", "web", r["help"]))

    def test_a_post_without_the_json_content_type_gets_415(self):
        code, r = self.call("/api/settings", "key=coding&value=ask", token=self.user,
                            ctype="application/x-www-form-urlencoded")
        self.assertEqual((code, r), (415, {"error": "json only", "code": "json_only"}))
        self.assertEqual(cli.coding_mode(), "decide")

    def test_the_stats_view_gets_wb_stats_feed_and_the_names_reads(self):
        code, r = self.call("/api/stats", token=self.user)
        self.assertEqual(code, 200)
        self.assertEqual(sorted(r), ["feed", "models", "routes"])
        self.assertEqual(sorted(r["feed"]), sorted(json.loads(run_wb("stats", "--json"))))
        self.assertIsInstance(r["models"], dict)
        self.assertEqual(self.call("/api/stats")[0], 401)

    def test_an_agent_token_gets_403_on_the_admin_endpoints_and_every_path_outside_its_allow_list(self):
        with contextlib.redirect_stderr(io.StringIO()):
            agent = run_wb("chat", "token", "add", "my-agent").strip()
        for path in ("/api/settings", "/api/stats", "/api/top"):
            self.assertEqual(self.call(path, token=agent), (403, {"error": f"agent tokens can't use {path}", "code": "agent_forbidden"}))
        self.assertEqual(self.call("/api/settings", {"key": "coding", "value": "only"}, token=agent),
                         (403, {"error": "agent tokens can't use /api/settings", "code": "agent_forbidden"}))
        self.assertEqual(cli.coding_mode(), "decide")
        # every path the handler answers, by every method it takes: outside AGENT_MAY is 403
        paths = set(re.findall(r'"(/api/[a-z]+/?)', (ROOT / "workbench" / "chat.py").read_text()))
        self.assertTrue({"/api/settings", "/api/stats", "/api/top", "/api/push", "/api/upload/"} <= paths)
        self.assertFalse({"/api/answer", "/api/ask/", "/api/prefs"} & paths)  # no dialog answered from the chat (#1350)
        refused = 0
        for path in sorted(paths):
            at = path + "x1234567" if path.endswith("/") else path
            for method in ("GET", "POST", "PUT"):
                if chat.agent_may(method, at):
                    continue
                code, r = self.call(at, {} if method != "GET" else None, token=agent, method=method)
                self.assertEqual(code, 403, f"{method} {at}")
                self.assertTrue(r["error"].startswith("agent tokens can't "), r)
                refused += 1
        self.assertGreater(refused, 30)
        # the old wording where there was one
        self.assertEqual(self.call("/api/push", {"test": True}, token=agent)[1],
                         {"error": "agent tokens can't change notifications", "code": "agent_forbidden"})
        self.assertEqual(self.call("/api/latest", token=agent)[1], {"error": "agent tokens can't read the user's notifications",
                                                                         "code": "agent_forbidden"})
        self.assertEqual(self.call("/api/health", token=agent)[0], 200)   # and its own still work
        self.assertEqual(self.call("/api/send", {"text": "hi"}, token=agent)[0], 200)

    def test_the_live_view_sends_a_top_event_then_stops_its_child_after_the_viewer_hangs_up(self):
        c = socket.create_connection(("127.0.0.1", self.port), timeout=30)
        c.sendall(f"GET /api/top HTTP/1.1\r\nHost: localhost:{self.port}\r\n"
                  f"Authorization: Bearer {self.user}\r\n\r\n".encode())
        got = b""
        while not re.search(rb"event: top\ndata: [^\n]*\n\n", got):
            chunk = c.recv(1 << 16)
            self.assertTrue(chunk, got)
            got += chunk
        self.assertIn(b"Content-Type: text/event-stream", got)
        tick = json.loads(re.search(rb"event: top\ndata: ([^\n]*)\n", got).group(1))
        self.assertIn("gpus", tick)                                     # one wb top --json object
        kids = children(self.server.pid, "top --json")
        self.assertEqual(len(kids), 1)
        c.close()
        gone = time.monotonic()
        time.sleep(2)
        self.assertEqual(children(self.server.pid, "top --json"), kids)  # it lingers for the next viewer
        while children(self.server.pid, "top --json"):
            self.assertLess(time.monotonic() - gone, chat.TOP_LINGER_S + 5, "the child outlived its linger")
            time.sleep(0.5)


class AdminSet(Base):
    """chat.admin_set, the POST's body, with settings.apply stood in for."""

    def test_a_refusal_is_409_with_wbs_line_and_the_rows_and_is_never_recorded(self):
        rows = [{"key": "coding", "label": "Claude coding", "kind": "choice", "value": "decide"}]
        with mock.patch.object(settings, "rows", return_value=rows), \
                mock.patch.object(settings, "apply", return_value={"ok": False, "said": "wb: no"}):
            self.assertEqual(chat.admin_set("coding", "ask"), (409, {"ok": False, "error": "wb: no", "rows": rows}))
        self.assertFalse(chat.changes_path().exists())

    def test_a_change_needing_claudes_help_carries_it_to_the_page_and_leaves_the_record_to_wbs_child(self):
        # #1355: one writer logs every Settings write, cli.main around the row's own wb (the
        # server's child here, stood in for): the POST logging it too would log it twice
        rows = [{"key": "probe", "label": "Agent code navigation", "kind": "choice", "value": "off"}]
        done = {"ok": True, "said": "probe: on, but not set up", "help": "Offer to set it up."}
        with mock.patch.object(settings, "rows", return_value=rows), \
                mock.patch.object(settings, "apply", return_value=done):
            code, r = chat.admin_set("probe", "on")
        self.assertEqual((code, r["help"], r["said"]), (200, "Offer to set it up.", "probe: on, but not set up"))


class RunsOnThePage(Base):
    """The runs strip's state and a run row's answer (#1278's S12)."""

    def test_the_state_carries_each_run_not_closed_for_the_strip_without_where_it_ran(self):
        from workbench import runs
        for name, phase in (("night", "running"), ("gone", "closed")):
            d = runs.run_dir(name)
            d.mkdir(parents=True)
            store.write_json(d / "run.json", {"name": name, "flow_name": "audit", "session": "S1", "cwd": "/x",
                                              "budget_min": 30, "created": 1.0})
            runs.Run(name).set_status(phase=phase, stages={"find": {"done": 1, "running": 2}}, spent_usd=0.25,
                                      budget_usd=2.0, gpu_min=4.5)
        self.assertEqual(chat.snapshot()["runs"], [{
            "run": "night", "flow": "audit", "phase": "running", "why": "",
            "stages": {"find": {"done": 1, "running": 2}}, "spent_usd": 0.25, "budget_usd": 2.0, "budget_min": 30,
            "gpu_min": 4.5}])
        self.assertNotIn("runs", chat.agent_snapshot("bot"))  # an agent's token sees no runs

    def test_a_runs_summary_reaches_the_page_as_its_detail_and_is_never_recorded(self):
        rows = [{"key": "run-night", "label": "Run night", "kind": "action", "choices": []}]
        done = {"ok": True, "said": "run night · flow audit · running · $0", "detail": "run night · flow audit\nfinal (1):"}
        with mock.patch.object(settings, "rows", return_value=rows), \
                mock.patch.object(settings, "apply", return_value=done):
            code, r = chat.admin_set("run-night", "summary")
        self.assertEqual((code, r["said"], r["detail"]), (200, done["said"], done["detail"]))
        self.assertFalse(chat.changes_path().exists())
