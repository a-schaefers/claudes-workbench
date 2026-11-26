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
        self.assertEqual(store.read_json(switch), {"on": False, "v": 2})  # 2.0's own off, marked (#1405)
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
        # the port is kept without the pid: once the server had exited, off's link named 1337
        # (the macOS nightly, 2026-11-25; Linux passed only while the server was a zombie)
        self.assertEqual(store.read_json(chat.chat_dir() / "serve.pid"), {"port": port})

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
                mock.patch.object(chat, "_start_unit"), mock.patch.object(chat, "wait_up", return_value=False), \
                mock.patch.object(chat, "start", return_value=4242) as fell:
            got = chat.ensure(other)
        self.assertEqual(got["state"], "failed")
        self.assertTrue(got["why"].startswith(f"{chat.service_name()} didn't answer within"), got)
        self.assertIn("nor did wb's own server", got["why"])
        fell.assert_called_once_with(other)

    def test_ensure_starts_wbs_own_server_once_when_the_unit_never_answers(self):
        # #1343 (bar round 9): a unit that exits 78 (a Python 3.11+ not on systemd's PATH) is
        # stopped for good but still enabled, and ensure only ever started it again, so `wb chat
        # url`, `wb chat on` and the mod's poll left the web app down for good
        port = free_port()
        with mock.patch.object(chat, "PORT", port), mock.patch.object(chat, "_unit_keeps_it", return_value=True), \
                mock.patch.object(chat, "_start_unit") as unit, \
                mock.patch.object(chat, "wait_up", side_effect=[False, True]) as waited, \
                mock.patch.object(chat, "start", return_value=4242) as fell:
            got = chat.ensure(port)
        self.assertEqual(got["state"], "started", got)
        self.assertEqual(got["why"], f"{chat.service_name()} didn't answer within {chat.START_WAIT} s "
                                     f"({chat.service_hint('status')}): wb started its own server")
        self.assertEqual((unit.call_count, fell.call_count), (1, 1))
        self.assertEqual([c.args for c in waited.call_args_list], [(port, None), (port, 4242)])


def service_lines(out: str) -> list[str]:
    """wb setup's lines about the web app's service: every line naming the web app but the story's
    `link.where`, where its link is, which setup ends with while the chat is on (#1355)."""
    return [x for x in out.splitlines() if "web app" in x and not x.startswith("--  Open the web app")]


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

    def answers(self, up: bool):
        """Whether the service's chat answers once started: the fake systemctl starts nothing."""
        patch = mock.patch.object(chat, "wait_up", return_value=up)
        patch.start()
        self.addCleanup(patch.stop)

    def test_setup_with_no_service_touches_no_unit_directory(self):
        code, out = self.setup("--no-service")
        self.assertEqual(code, 0, out)
        self.assertFalse((self.home / ".config/systemd").exists())
        self.assertFalse((self.home / "Library").exists())
        self.assertEqual(self.ran(), [])
        self.assertEqual(service_lines(out), [])

    def test_setup_links_the_unit_and_enables_it_once_and_again_safely(self):
        # the server `wb chat on` started stops first, so the unit gets the port
        fake = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "chat", "serve"])
        self.addCleanup(lambda: (fake.kill(), fake.wait()))
        store.write_json(chat.chat_dir() / "serve.pid", {"pid": fake.pid, "port": 1, "start": chat._started(fake.pid)})
        self.answers(True)
        code, out = self.setup()
        self.assertEqual(code, 0, out)
        unit = self.home / ".config/systemd/user/wb-chat.service"
        self.assertTrue(unit.is_symlink())
        self.assertEqual(unit.resolve(), (ROOT / "systemd/wb-chat.service").resolve())
        self.assertEqual(self.ran()[-3:], ["--user daemon-reload", "--user enable wb-chat", "--user restart wb-chat"])
        lines = service_lines(out)
        self.assertEqual(lines, [f"ok  web app kept up by systemd's wb-chat ({unit} → "
                                 f"{ROOT / 'systemd/wb-chat.service'} (linked))"])
        self.assertEqual(fake.wait(timeout=10), -signal.SIGTERM)
        code, out = self.setup()                                            # again: nothing new
        self.assertEqual(code, 0, out)
        self.assertEqual(service_lines(out),
                         [f"ok  web app kept up by systemd's wb-chat ({unit} → {ROOT / 'systemd/wb-chat.service'})"])
        self.assertEqual(self.ran().count("--user enable wb-chat"), 2)
        self.assertEqual(self.ran()[-1], "--user start wb-chat")          # the drop-in unchanged: no restart

    def test_setup_gives_the_unit_its_own_python_path_and_wb_variables(self):
        # #1342 and #1343 (bar round 9): the unit ran with the user manager's PATH, so a pyenv,
        # conda or uv Python 3.11+ was missing (bin/wb exited 78, the unit down for good) and so
        # were OpenCode in ~/.opencode/bin and probe in ~/.local/bin (the web Settings said
        # OpenCode isn't installed); nor did it get the WB_* variables the user exports
        self.answers(True)
        shell = Path(self.tmp.name) / "shell 100%"
        os.environ["PATH"] = os.pathsep.join([str(self.bin), str(shell), "relative/bin", os.environ["PATH"]])
        code, out = self.setup()
        self.assertEqual(code, 0, out)
        dropin = self.home / ".config/systemd/user" / cli.SERVICE_DROPIN
        text = dropin.read_text()
        self.assertTrue(text.startswith(f"# {cli.SERVICE_MARK}"), text)
        env = cli.service_env("/usr/bin")
        self.assertEqual(env["PATH"].split(os.pathsep)[:3], [os.path.dirname(sys.executable), str(self.bin), str(shell)])
        self.assertNotIn("relative/bin", env["PATH"].split(os.pathsep))
        self.assertIn("/usr/bin", env["PATH"].split(os.pathsep))
        self.assertEqual(env["WB_HOME"], str(store.home()))
        self.assertIn(f'Environment="PATH={os.path.dirname(sys.executable)}{os.pathsep}{self.bin}{os.pathsep}'
                      f'{str(shell).replace("%", "%%")}{os.pathsep}', text)       # % is systemd's specifier
        self.assertIn(f'Environment="WB_HOME={store.home()}"', text)
        self.assertIn(f'Environment="WB_ROUTES={os.environ["WB_ROUTES"]}"', text)
        self.assertNotIn("CLAUDE", text.replace("CLAUDE_CONFIG_DIR", ""))   # never Claude Code's session

    def test_setup_stops_a_unit_that_never_answers_and_starts_wbs_own_server(self):
        # #1343 (bar round 9): enable --now returns 0 when ExecStart exits at once (78), and
        # setup said "ok  web app kept up" over a web app it had just taken down for good
        self.answers(False)
        with mock.patch.object(chat, "ensure", return_value={"state": "started", "url": "u"}) as ensured:
            code, out = self.setup()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.ran()[-1], "--user disable --now wb-chat")
        ensured.assert_called_once_with(chat.port())
        self.assertEqual(service_lines(out),
                         [f"--  web app service: systemd's wb-chat didn't answer within {chat.START_WAIT} s, so setup "
                          "stopped it (systemctl --user disable --now wb-chat) and wb started its own server "
                          "instead; journalctl --user -u wb-chat says why, then wb setup again"])

    def test_setup_leaves_a_manager_of_another_home_and_the_users_off_alone(self):
        self.manager.write_text("/elsewhere")                    # a test's HOME, or sudo's
        code, out = self.setup()
        self.assertEqual(code, 0, out)
        self.assertFalse((self.home / ".config/systemd").exists())
        self.assertEqual(self.ran(), ["--user show-environment"])
        self.assertEqual(service_lines(out), [])  # no word of the service
        self.manager.write_text(str(self.home))
        chat.set_on(False)
        code, out = self.setup()
        self.assertIn("--  web app off: no service installed (wb chat on, then wb setup)", out)
        self.assertFalse((self.home / ".config/systemd").exists())


class SetupLaunchd(Base):
    """wb setup on macOS writes the launchd agent with setup's environment (#1342, #1343)."""
    def setUp(self):
        super().setUp()
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        fakebin = Path(self.tmp.name) / "fakebin"
        fakebin.mkdir()
        self.calls = Path(self.tmp.name) / "launchctl.calls"
        self.loaded = Path(self.tmp.name) / "loaded"
        (fakebin / "launchctl").write_text(
            "#!/bin/sh\n"
            f'echo "$1" >> "{self.calls}"\n'
            f'[ "$1" = print ] && {{ [ -e "{self.loaded}" ]; exit; }}\n'
            f'[ "$1" = bootstrap ] && touch "{self.loaded}"\n'
            f'[ "$1" = bootout ] && rm -f "{self.loaded}"\n'
            "exit 0\n")
        (fakebin / "launchctl").chmod(0o755)
        os.environ.update(HOME=str(self.home), PATH=os.pathsep.join([str(fakebin), os.environ["PATH"]]))
        import pwd
        me = pwd.getpwuid(os.getuid())
        for patch in (mock.patch.object(chat, "_macos", return_value=True),
                      mock.patch("pwd.getpwuid", return_value=mock.Mock(pw_dir=str(self.home), pw_name=me.pw_name)),
                      mock.patch.object(chat, "wait_up", return_value=True)):
            patch.start()
            self.addCleanup(patch.stop)
        self.plist = self.home / "Library/LaunchAgents/workbench.chat.plist"

    def ran(self) -> list[str]:
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def test_setup_writes_the_agent_with_its_python_first_and_rewrites_only_its_own(self):
        import plistlib
        self.assertEqual(cli.install_service(), f"ok  web app kept up by launchd's workbench.chat ({self.plist} (written))")
        agent = plistlib.loads(self.plist.read_bytes())
        env = agent["EnvironmentVariables"]
        self.assertEqual(env["PATH"].split(os.pathsep)[0], os.path.dirname(sys.executable))
        self.assertIn("/opt/homebrew/bin", env["PATH"].split(os.pathsep))     # the shipped PATH still on it
        self.assertEqual(env["WB_HOME"], str(store.home()))
        self.assertIn(cli.SERVICE_MARK.encode(), self.plist.read_bytes())
        self.assertEqual(self.ran(), ["print", "bootstrap"])
        self.assertEqual(cli.install_service(), f"ok  web app kept up by launchd's workbench.chat ({self.plist})")
        self.assertEqual(self.ran()[2:], ["print"])                          # unchanged and loaded: left as it is
        os.environ["WB_SKILLS"] = str(Path(self.tmp.name) / "skills-moved")  # setup's environment changed
        cli.install_service()
        self.assertEqual(self.ran()[3:], ["print", "bootout", "bootstrap"])  # launchd reads it only when bootstrapped
        self.assertEqual(plistlib.loads(self.plist.read_bytes())["EnvironmentVariables"]["WB_SKILLS"],
                         os.environ["WB_SKILLS"])
        mine = self.plist.read_bytes().replace(cli.SERVICE_MARK.encode(), b"Mine")  # the user's own copy
        self.plist.write_bytes(mine)
        os.environ["WB_SKILLS"] = str(Path(self.tmp.name) / "other")
        cli.install_service()
        self.assertEqual(self.plist.read_bytes(), mine)

    def test_a_hand_installed_1_91_plist_is_rewritten_and_one_the_user_edited_is_kept(self):
        # #1406 (2026-11-25): 1.91's plist said to cp it into LaunchAgents by hand; with no mark and
        # not today's shipped bytes, setup kept it as the user's, without the PATH and WB_* variables
        import hashlib, plistlib, textwrap
        old = textwrap.dedent("""\
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
        <!--
          The web chat on macOS (wb chat serve), kept up the way systemd/wb-chat.service keeps it on Linux:
          started at login, started again whenever it exits (#449). Install it as a copy (launchd reads
          LaunchAgents at login):
            cp launchd/workbench.chat.plist ~/Library/LaunchAgents/
            launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/workbench.chat.plist
          and remove it with `launchctl bootout gui/$(id -u)/workbench.chat`. A shell runs ~/.local/bin/wb
          (wb setup's link), since launchd expands neither ~ nor $HOME. PATH puts Homebrew's python3 first:
          /usr/bin/python3 is Apple's 3.9, which bin/wb refuses (exit 78). launchd can't exempt that exit
          from KeepAlive, so a broken install retries every ThrottleInterval seconds: fix it, then
          `launchctl kickstart -k gui/$(id -u)/workbench.chat`. Set WB_HOME or WB_CHAT_* here too if you
          export them.
        -->
        <plist version="1.0">
        <dict>
          <key>Label</key>
          <string>workbench.chat</string>
          <key>ProgramArguments</key>
          <array>
            <string>/bin/sh</string>
            <string>-c</string>
            <string>exec "$HOME/.local/bin/wb" chat serve</string>
          </array>
          <key>EnvironmentVariables</key>
          <dict>
            <key>PATH</key>
            <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
          </dict>
          <key>RunAtLoad</key>
          <true/>
          <key>KeepAlive</key>
          <true/>
          <key>ThrottleInterval</key>
          <integer>10</integer>
        </dict>
        </plist>
        """).encode()  # v1.91.2:launchd/workbench.chat.plist (CI's clone has no tags)
        self.assertIn(hashlib.sha256(old).hexdigest(), cli.SHIPPED_PLISTS)
        self.plist.parent.mkdir(parents=True)
        self.plist.write_bytes(old)
        self.assertEqual(cli.install_service(), f"ok  web app kept up by launchd's workbench.chat ({self.plist} (written))")
        self.assertIn(cli.SERVICE_MARK.encode(), self.plist.read_bytes())
        self.assertEqual(plistlib.loads(self.plist.read_bytes())["EnvironmentVariables"]["WB_HOME"], str(store.home()))
        mine = old.replace(b"<integer>10</integer>", b"<integer>30</integer>")  # edited: theirs
        self.plist.write_bytes(mine)
        self.assertEqual(cli.install_service(), f"ok  web app kept up by launchd's workbench.chat ({self.plist}, yours)")
        self.assertEqual(self.plist.read_bytes(), mine)

    def test_an_agent_that_never_answers_is_booted_out_and_wbs_own_server_started(self):
        # #1343: KeepAlive retried a bin/wb that exits 78 every 10 s, and setup said ok
        with mock.patch.object(chat, "wait_up", return_value=False), \
                mock.patch.object(chat, "ensure", return_value={"state": "started", "url": "u"}):
            said = cli.install_service()
        self.assertEqual(self.ran()[-1], "bootout")
        self.assertTrue(said.startswith("--  web app service: launchd's workbench.chat didn't answer within"), said)
        self.assertIn(f"launchctl bootout gui/{os.getuid()}/workbench.chat", said)


if __name__ == "__main__":
    unittest.main()
