"""The web app on by default (#1281, the owner, 2026-11-24: "just turn on by default"): the switch
reads on until `wb chat off`, chat.ensure brings the server up (`wb chat url`, the mod), wb setup
installs the service, and the server never runs wb as Claude."""
import contextlib
import io
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # run as a script too

from workbench import chat, cli, store  # noqa: E402
from tests.test_workbench import ROOT, Base, ThreadingHTTPServer, run_wb  # noqa: E402


def free_port() -> int:
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        return sk.getsockname()[1]


class ChatDefault(Base):
    def setUp(self):
        super().setUp()
        os.environ["WB_CHAT_TOKEN"] = str(Path(self.tmp.name) / "chat-token")
        os.environ["WB_CHAT_CONFIG"] = str(Path(self.tmp.name) / "chat.json")  # not the user's
        # the unit is never the owner's: a test's ensure starts no systemd or launchd service
        keeps = mock.patch.object(chat, "_unit_keeps_it", return_value=False)
        keeps.start()
        self.addCleanup(keeps.stop)

    def stop(self, pid: int):
        """The server ensure started, gone before the test's directory is (#1225)."""
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGTERM)
        deadline = time.time() + 10
        with contextlib.suppress(ChildProcessError):
            while os.waitpid(pid, os.WNOHANG) == (0, 0) and time.time() < deadline:
                time.sleep(0.1)
        with contextlib.suppress(ProcessLookupError, ChildProcessError):
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)

    def url_json(self) -> dict:
        buf = io.StringIO()
        with redirect_stdout(buf):
            try:
                cli.main(["chat", "url", "--json"])
                code = 0
            except SystemExit as e:
                code = e.code
        self.assertEqual(code, 0)
        return json.loads(buf.getvalue())

    def test_with_no_switch_file_the_web_app_is_on_and_only_wb_chat_off_turns_it_off(self):
        switch = chat.chat_dir() / "switch.json"
        self.assertFalse(switch.exists())
        self.assertTrue(chat.is_on())
        run_wb("chat", "off", "--port", str(free_port()))
        self.assertEqual(store.read_json(switch), {"on": False})
        self.assertFalse(chat.is_on())                                     # the user's off stays off
        port = free_port()
        self.assertEqual(chat.ensure(port), {"url": chat.url(port), "state": "off"})
        self.assertFalse((chat.chat_dir() / "serve.pid").exists())        # nothing started
        self.assertEqual(cli.chat_line(), (None, "web app off (wb chat on)"))

    def test_doctor_only_looks_and_makes_no_chat_token_where_none_is(self):
        # #1281's review (2026-11-24): on by default, doctor's line went through who_answers to
        # chat.token(), which made the token file, so every doctor test wrote one into its HOME
        self.assertEqual(Path(os.environ["WB_CHAT_TOKEN"]), Path(self.tmp.name) / "chat-token")
        token = chat.token_path()
        self.assertFalse(token.exists())
        port = chat.port()
        with mock.patch.object(chat, "who_answers") as probed:
            self.assertEqual(cli.chat_line(), (False, f"web app down: no server answers on port {port} (wb chat on)"))
        probed.assert_not_called()
        self.assertFalse(token.exists())

    def test_ensure_starts_a_server_that_never_runs_wb_as_claude_then_finds_it_up(self):
        # a server started from a Claude session (the mod's `wb chat url`) would run every web
        # click's wb command as Claude, behind the mod's dialogs
        os.environ.update(CLAUDECODE="1", CLAUDE_PID=str(os.getpid()), CLAUDE_CODE_SESSION_ID="s-1")
        port = free_port()
        got = chat.ensure(port)
        pid = store.read_json(chat.chat_dir() / "serve.pid")["pid"]
        self.addCleanup(self.stop, pid)
        self.assertEqual(got, {"url": chat.url(port), "state": "started"}, got)
        self.assertTrue(chat.running(port))
        environ = Path(f"/proc/{pid}/environ")
        if environ.exists():
            names = {kv.split(b"=", 1)[0].decode() for kv in environ.read_bytes().split(b"\0") if kv}
            self.assertIn("WB_HOME", names)                                  # the rest is kept
            self.assertFalse(names & {"CLAUDECODE", "CLAUDE_PID", "CLAUDE_CODE_SESSION_ID"}, names)
        self.assertEqual(chat.ensure(port), {"url": chat.url(port), "state": "up"})
        self.assertEqual(store.read_json(chat.chat_dir() / "serve.pid")["pid"], pid)  # not a second
        self.assertEqual(self.url_json(), {"url": chat.url(port), "state": "up"})  # the mod's read
        self.assertEqual(cli.chat_line(), (True, f"web app answers on localhost:{port} (started by wb)"))
        self.assertIn("stopped the server it started", run_wb("chat", "off", "--port", str(port)))
        self.assertEqual(self.url_json(), {"url": chat.url(port), "state": "off"})

    def test_a_foreign_listener_blocks_the_web_app_and_doctor_says_why(self):
        class Other(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()

            def log_message(self, *a):
                pass
        other = ThreadingHTTPServer(("127.0.0.1", 0), Other)
        threading.Thread(target=other.serve_forever, daemon=True).start()
        self.addCleanup(other.server_close)
        self.addCleanup(other.shutdown)
        port = other.server_address[1]
        got = chat.ensure(port)
        self.assertEqual((got["state"], got["why"]), ("blocked", chat.blocked_why(port)))
        self.assertIn(f"another account's chat (or another program, or a server still on a replaced "
                      f"token) answers on port {port}: wb chat on --port <free port>", got["why"])
        self.assertFalse((chat.chat_dir() / "serve.pid").exists())        # nothing started
        store.write_json(chat.chat_dir() / "serve.pid", {"pid": 1, "port": port})  # chat.port()'s
        self.assertEqual(cli.chat_line(), (False, f"web app down: {chat.blocked_why(port)} (wb chat on)"))
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit):
            run_wb("chat", "on", "--port", str(port))
        self.assertIn("web chat not up: another account's chat", err.getvalue())
        self.assertTrue(chat.is_on())                                       # put back as it was

    def test_ensure_starts_the_unit_that_keeps_the_chat_and_says_when_it_doesnt_answer(self):
        port, servers = free_port(), []

        def unit():  # systemd starting wb-chat.service: a server on the unit's port
            srv = chat.make_servers(port, ["127.0.0.1"])[0][0]
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            servers.append(srv)
        self.addCleanup(lambda: [(s.shutdown(), s.server_close(), s.RequestHandlerClass.hub.close())
                                 for s in servers])
        with mock.patch.object(chat, "PORT", port), mock.patch.object(chat, "_unit_keeps_it", return_value=True), \
                mock.patch.object(chat, "_start_unit", side_effect=unit) as started:
            self.assertEqual(chat.ensure(port)["state"], "started")
            self.assertEqual(started.call_count, 1)
            self.assertFalse((chat.chat_dir() / "serve.pid").exists())    # no server of wb's beside it
            self.assertEqual(cli.chat_line(), (True, f"web app answers on localhost:{port} ({chat.service_name()})"))
        other = free_port()
        with mock.patch.object(chat, "PORT", other), mock.patch.object(chat, "_unit_keeps_it", return_value=True), \
                mock.patch.object(chat, "_start_unit"), mock.patch.object(chat, "wait_up", return_value=False):
            got = chat.ensure(other)
        self.assertEqual(got["state"], "failed")
        self.assertTrue(got["why"].startswith(f"{chat.service_name()} didn't answer within"), got)


class SetupService(Base):
    """wb setup installs the web app's service (#1281); --no-service skips it."""
    def setUp(self):
        super().setUp()
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.bin = Path(self.tmp.name) / "fakebin"
        self.bin.mkdir()
        self.calls = Path(self.tmp.name) / "systemctl.calls"
        manager = Path(self.tmp.name) / "manager-home"
        manager.write_text(str(self.home))
        (self.bin / "systemctl").write_text(
            "#!/bin/sh\n"
            f'echo "$*" >> "{self.calls}"\n'
            f'[ "$2" = show-environment ] && {{ echo "HOME=$(cat "{manager}")"; echo PATH=/usr/bin; exit 0; }}\n'
            '[ "$2" = is-enabled ] && exit 1\n'
            "exit 0\n")
        (self.bin / "systemctl").chmod(0o755)
        self.manager = manager
        os.environ.update(HOME=str(self.home), PATH=os.pathsep.join([str(self.bin), os.environ["PATH"]]))
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        os.environ.pop("XDG_CONFIG_HOME", None)
        for patch in (mock.patch.object(chat, "_macos", return_value=False),
                      mock.patch.object(cli.optimize, "start", return_value=[])):  # not this test's
            patch.start()
            self.addCleanup(patch.stop)

    def setup(self, *a):
        buf = io.StringIO()
        with redirect_stdout(buf), self.assertRaises(SystemExit) as e:
            cli.main(["setup", *a])
        return e.exception.code, buf.getvalue()

    def ran(self) -> list[str]:
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def test_setup_with_no_service_touches_no_unit_directory(self):
        code, out = self.setup("--no-service")
        self.assertEqual(code, 0, out)
        self.assertFalse((self.home / ".config/systemd").exists())
        self.assertFalse((self.home / "Library").exists())
        self.assertEqual(self.ran(), [])
        self.assertNotIn("web app", out)

    def test_setup_links_the_unit_and_enables_it_once_and_again_safely(self):
        # the server `wb chat on` started stops first, so the unit gets the port
        fake = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "chat", "serve"])
        self.addCleanup(lambda: (fake.kill(), fake.wait()))
        store.write_json(chat.chat_dir() / "serve.pid", {"pid": fake.pid, "port": 1, "start": chat._started(fake.pid)})
        code, out = self.setup()
        self.assertEqual(code, 0, out)
        unit = self.home / ".config/systemd/user/wb-chat.service"
        self.assertTrue(unit.is_symlink())
        self.assertEqual(unit.resolve(), (ROOT / "systemd/wb-chat.service").resolve())
        self.assertEqual(self.ran()[-2:], ["--user daemon-reload", "--user enable --now wb-chat"])
        lines = [x for x in out.splitlines() if "web app" in x]
        self.assertEqual(lines, [f"ok  web app kept up by systemd's wb-chat ({unit} → "
                                 f"{ROOT / 'systemd/wb-chat.service'} (linked))"])
        self.assertEqual(fake.wait(timeout=10), -signal.SIGTERM)
        code, out = self.setup()                                            # again: nothing new
        self.assertEqual(code, 0, out)
        self.assertEqual([x for x in out.splitlines() if "web app" in x],
                         [f"ok  web app kept up by systemd's wb-chat ({unit} → {ROOT / 'systemd/wb-chat.service'})"])
        self.assertEqual(self.ran().count("--user enable --now wb-chat"), 2)

    def test_setup_leaves_a_manager_of_another_home_and_the_users_off_alone(self):
        self.manager.write_text("/elsewhere")                    # a test's HOME, or sudo's
        code, out = self.setup()
        self.assertEqual(code, 0, out)
        self.assertFalse((self.home / ".config/systemd").exists())
        self.assertEqual(self.ran(), ["--user show-environment"])
        self.assertNotIn("web app", out)
        self.manager.write_text(str(self.home))
        chat.set_on(False)
        code, out = self.setup()
        self.assertIn("--  web app off: no service installed (wb chat on, then wb setup)", out)
        self.assertFalse((self.home / ".config/systemd").exists())


if __name__ == "__main__":
    unittest.main()
