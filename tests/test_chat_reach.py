"""Phone reach (#1355): `wb chat reach local|tailnet` and the Settings row `reach`, which write
chat.json's `bind`; the tailnet only while Tailscale is up here, never a LAN option, the token the
key either way, and the https push needs offered as `tailscale serve`, never run. A fake
`tailscale` on PATH; a temp WB_HOME and HOME, no server."""
import io
import json
import os
import sys
import textwrap
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # run as a script too

from workbench import chat, settings, story  # noqa: E402
from tests.test_workbench import Base, run_wb  # noqa: E402

NAME = "box.example-tail.ts.net"
NOWHERE = "192.0.2.1"  # TEST-NET-1 (RFC 5737): on no machine's interfaces
LISTENABLE = chat.listenable


class Reach(Base):
    def setUp(self):
        super().setUp()
        tmp = Path(self.tmp.name)
        self.bin = tmp / "ts-bin"
        self.bin.mkdir()
        # `tailscale status --json` up (FAKE_TS=down: stopped; FAKE_TS_IP: its one address),
        # `serve status --json` serving the chat's port when FAKE_TS_SERVE names it
        (self.bin / "tailscale").write_text(textwrap.dedent(f"""\
            #!{sys.executable}
            import json, os, sys
            if sys.argv[1:] == ["status", "--json"]:
                up = os.environ.get("FAKE_TS") != "down"
                print(json.dumps({{"BackendState": "Running" if up else "Stopped", "Self": {{
                    "DNSName": "{NAME}.", "HostName": "box",
                    "TailscaleIPs": [os.environ["FAKE_TS_IP"]] if "FAKE_TS_IP" in os.environ else ["100.64.0.7", "fd7a::7"]}}}}))
            elif sys.argv[1:] == ["serve", "status", "--json"]:
                port = os.environ.get("FAKE_TS_SERVE")
                print(json.dumps({{"Web": {{"{NAME}:443": {{"Handlers": {{"/": {{"Proxy": f"http://127.0.0.1:{{port}}"}}}}}}}}}}
                                 if port else {{}}))
            else:
                sys.exit(2)
            """))
        (self.bin / "tailscale").chmod(0o755)
        # the fake's address is on no interface here either: taken as one but in #1499's test
        listen = mock.patch.object(chat, "listenable", lambda ip: ip != NOWHERE)
        listen.start()
        self.addCleanup(listen.stop)
        self.conf = chat.config_path()
        self.conf.parent.mkdir(parents=True, exist_ok=True)

    def with_tailscale(self, **env):
        return mock.patch.dict(os.environ, {"PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}", **env})

    def without_tailscale(self):
        empty = Path(self.tmp.name) / "no-ts"
        empty.mkdir(exist_ok=True)
        return mock.patch.dict(os.environ, {"PATH": str(empty)})

    def refused(self, *argv) -> str:
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit) as e:
            run_wb(*argv)
        self.assertEqual(e.exception.code, 1, err.getvalue())
        return err.getvalue()

    def changes(self) -> list[dict]:
        p = chat.changes_path()
        return [json.loads(ln) for ln in p.read_text().splitlines()] if p.exists() else []

    def test_tailnet_binds_the_tailnet_too_keeps_the_users_other_keys_and_says_the_name_to_open(self):
        self.conf.write_text(json.dumps({"hosts": ["laptop"]}))
        with self.without_tailscale():  # nor a real one's serve (#1512)
            self.assertEqual(run_wb("chat", "reach"), "web app reach: local (this machine only)\n")
        with self.with_tailscale():
            out = run_wb("chat", "reach", "tailnet", "--port", "1400")
            self.assertEqual(json.loads(self.conf.read_text()), {"hosts": ["laptop"], "bind": ["127.0.0.1", "tailscale"]})
            self.assertEqual(chat.reach(), "tailnet")
            self.assertIn(f"web app reach: tailnet: http://{NAME}:1400/ with the link's #token=…", out)
            # the server it rebinds answers the tailnet name too
            self.assertEqual(chat.plan()["binds"], ["127.0.0.1", "100.64.0.7"])
            self.assertIn(NAME, chat.plan()["hosts"])
        # push and the home-screen app need https: offered as tailscale serve, run by nobody here
        self.assertTrue(out.endswith("https: not served: notifications and the home-screen app need it; "
                                     "`tailscale serve --bg 1400` serves it on your tailnet only\n"), out)
        [row] = self.changes()
        self.assertEqual((row["key"], row["label"], row["from"], row["to"], row["by"]),
                         ("reach", "Web app reach", "local", "tailnet", "terminal"))
        self.assertEqual(row["help"], settings.REACH_HELP.format(said=out.strip().splitlines()[-1]))
        self.assertIn("run it only on their yes", row["help"])

    def test_https_served_by_tailscale_serve_is_said_and_needs_no_help(self):
        with self.with_tailscale(FAKE_TS_SERVE="1400"):
            out = run_wb("chat", "reach", "tailnet", "--port", "1400")
        self.assertIn(f"https: served at https://{NAME}/", out)
        self.assertNotIn("help", self.changes()[-1])

    def test_tailnet_is_refused_and_nothing_written_while_tailscale_isnt_up_or_here(self):
        with self.with_tailscale(FAKE_TS="down"):
            self.assertIn("Tailscale isn't up here: `tailscale up`, then this again", self.refused("chat", "reach", "tailnet"))
        with self.without_tailscale():
            self.assertIn("no tailscale command here", self.refused("chat", "reach", "tailnet"))
        self.assertFalse(self.conf.exists())
        self.assertEqual(self.changes(), [])
        with self.assertRaises(ValueError):
            chat.set_reach("lan")  # no LAN option: a wildcard bind is the user's own edit

    def test_a_tailnet_address_this_machine_cannot_bind_is_refused_and_never_takes_localhost_down_1499(self):
        # #1499 (2026-11-26): under Tailscale's userspace networking (Docker, LXC) the tailnet
        # address is on no interface; `reach tailnet` wrote the bind anyway, and the server's
        # rebind to it failed, closed its localhost server too and exited, every restart the same
        with mock.patch.object(chat, "listenable", LISTENABLE):
            self.assertTrue(chat.listenable("127.0.0.1"))
            self.assertFalse(chat.listenable(NOWHERE))
            with self.with_tailscale(FAKE_TS_IP=NOWHERE):
                said = self.refused("chat", "reach", "tailnet", "--port", "1400")
                self.assertIn(f"tailnet address {NOWHERE} is on none of its interfaces", said)
                self.assertIn("`tailscale serve --bg 1400`", said)              # the route that works there
                self.assertFalse(self.conf.exists())
                self.assertEqual(self.changes(), [])
                self.conf.write_text(json.dumps({"bind": chat.TAILNET_BIND}))  # by hand, or before the fix
                servers, p = chat.make_servers(0)
                try:
                    self.assertEqual([s.server_address[0] for s in servers], ["127.0.0.1"])  # localhost kept
                    self.assertEqual(p["skipped"], ["tailscale (its address isn't on this machine)"])
                    self.assertEqual(chat.plan()["binds"], p["binds"])          # replan: nothing to restart for
                finally:
                    for s in servers:
                        s.server_close()

    def test_local_with_tailscale_serve_still_in_front_names_the_https_and_the_command_that_stops_it_1512(self):
        # #1512 (2026-11-26): `local` drops only the bind, and wb said "this machine only" while the
        # `tailscale serve` it had advised still served the app to every tailnet device
        self.conf.write_text(json.dumps({"bind": chat.TAILNET_BIND}))
        with self.with_tailscale(FAKE_TS_SERVE="1400"):
            out = run_wb("chat", "reach", "local", "--port", "1400")
            now = run_wb("chat", "reach", "--port", "1400")
        self.assertEqual(out, f"web app reach: local; the server drops the tailnet within {chat.REPLAN_EVERY} s, but "
                              f"`tailscale serve` still serves it to your tailnet at https://{NAME}/: "
                              "`tailscale serve --https=443 off` stops that\n")
        self.assertEqual(now, f"web app reach: local, but tailnet https via tailscale serve (https://{NAME}/)\n")
        self.assertEqual(json.loads(self.conf.read_text()), {})                 # the bind still dropped
        [row] = self.changes()
        self.assertEqual((row["from"], row["to"]), ("tailnet", "local"))
        self.assertEqual(row["help"], settings.REACH_LOCAL_HELP.format(said=out.strip()))
        self.assertIn("run it only on their yes", row["help"])
        with self.with_tailscale():                                              # serve stopped: here alone
            self.assertEqual(run_wb("chat", "reach", "--port", "1400"), "web app reach: local (this machine only)\n")

    def test_local_drops_the_bind_and_a_bind_of_the_users_own_reads_custom(self):
        self.conf.write_text(json.dumps({"bind": ["127.0.0.1", "tailscale"], "hosts": ["laptop"]}))
        with self.without_tailscale():  # nor a real one's serve (#1512)
            out = run_wb("chat", "reach", "local")
        self.assertEqual(out, f"web app reach: local (this machine only); the server drops the tailnet within "
                              f"{chat.REPLAN_EVERY} s\n")
        self.assertEqual(json.loads(self.conf.read_text()), {"hosts": ["laptop"]})
        self.conf.write_text(json.dumps({"bind": "0.0.0.0"}))
        self.assertEqual(chat.reach(), "custom")
        self.assertEqual(run_wb("chat", "reach"), "web app reach: custom (chat.json's own bind)\n")

    def test_the_settings_row_reads_chat_json_and_writes_through_wb_chat_reach(self):
        settings.drop()
        self.addCleanup(settings.drop)
        row = next(r for r in settings.rows(fresh=True) if r["key"] == "reach")
        self.assertEqual((row["group"], row["label"], row["value"]), ("web", "Web app reach", "local"))
        self.assertEqual([c["value"] for c in row["choices"]], ["local", "tailnet"])  # no LAN option
        self.assertIn("`tailscale serve`", row["choices"][1]["says"])
        self.assertEqual(settings._argv(row, "tailnet"), ["chat", "reach", "tailnet"])
        self.conf.write_text(json.dumps({"bind": ["0.0.0.0"]}))
        row = next(r for r in settings.rows(fresh=True) if r["key"] == "reach")
        self.assertEqual((row["value"], row["says"]), ("custom", settings.REACH_CUSTOM[2]))

    def test_chat_on_and_doctor_point_at_the_phones_way_in_only_while_it_opens_here_alone(self):
        from workbench import cli
        with self.with_tailscale():
            self.assertEqual(cli.reach_hint(), story.text("link.reach"))
        with self.without_tailscale():                       # nothing here to reach the tailnet with (#1524)
            self.assertEqual(cli.reach_hint(), "")
        self.conf.write_text(json.dumps({"bind": ["127.0.0.1", "tailscale"]}))
        with self.with_tailscale():
            self.assertEqual(cli.reach_hint(), "")

    def test_on_wsl2_with_tailscale_on_windows_tailnet_names_windows_serve_and_never_says_install_1524(self):
        # #1524 (2026-11-26): with Tailscale for Windows and none in the distro (#920's setup),
        # `reach tailnet` said "install Tailscale", Windows' serve went unseen, and `wb chat on`
        # advised the refused command
        from workbench import cli, hardware
        win = Path(self.tmp.name) / "win-bin"
        win.mkdir()
        (win / "tailscale.exe").write_text((self.bin / "tailscale").read_text())
        (win / "tailscale.exe").chmod(0o755)
        with mock.patch.dict(os.environ, {"PATH": str(win), "FAKE_TS_SERVE": "1400"}), \
                mock.patch.object(hardware, "system", return_value="wsl"):
            said = self.refused("chat", "reach", "tailnet", "--port", "1400")
            self.assertNotIn("install Tailscale", said)
            self.assertIn("Tailscale runs on Windows here", said)
            self.assertIn("`tailscale.exe serve --bg 1400`", said)                # the route that works there
            self.assertFalse(self.conf.exists())
            self.assertEqual(self.changes(), [])
            self.assertIs(chat.https_served(1400), True)                           # Windows' serve, seen
            self.assertEqual(run_wb("chat", "reach", "--port", "1400"),
                             f"web app reach: local, but tailnet https via tailscale serve (https://{NAME}/)\n")
            self.assertEqual(cli.still_served(1400)[1], "tailscale.exe serve --https=443 off")
            self.assertEqual(cli.reach_hint(), "")                                 # never advised where refused

    def test_the_phone_link_is_serves_https_else_the_tailnet_name_and_none_while_local_1552(self):
        # #1552 (2026-11-26): the link a phone's camera opens, the token in it, as the QR code draws it
        with self.with_tailscale():
            self.assertEqual(chat.phone_url(1400), "")                             # reach local: none
        with self.with_tailscale(FAKE_TS_SERVE="1400"):                            # serve in front: its https
            self.assertEqual(chat.phone_url(1400), f"https://{NAME}/#token={chat.token()}")
        self.conf.write_text(json.dumps({"bind": chat.TAILNET_BIND}))
        with self.with_tailscale():
            self.assertEqual(chat.phone_url(1400), f"http://{NAME}:1400/#token={chat.token()}")
        with self.with_tailscale(FAKE_TS="down"):
            self.assertEqual(chat.phone_url(1400), "")
        with self.without_tailscale():
            self.assertEqual(chat.phone_url(1400), "")

    def test_wb_chat_url_draws_the_phone_links_qr_code_at_a_terminal_and_never_for_the_mod_1552(self):
        from contextlib import redirect_stdout
        from workbench import cli, qr

        class Terminal(io.StringIO):
            def isatty(self):
                return True
        here = "http://localhost:1400/#token=t"
        self.conf.write_text(json.dumps({"bind": chat.TAILNET_BIND}))
        with self.with_tailscale(), mock.patch.object(chat, "ensure", return_value={"url": here, "state": "up"}):
            self.assertEqual(run_wb("chat", "url", "--port", "1400"), here + "\n")  # read whole: the link alone
            self.assertEqual(json.loads(run_wb("chat", "url", "--json", "--port", "1400"))["url"], here)
            out = Terminal()
            with redirect_stdout(out):
                cli.main(["chat", "url", "--port", "1400"])
        phone = f"http://{NAME}:1400/#token={chat.token()}"
        self.assertEqual(out.getvalue(), f"{here}\non your phone: {phone}\n{qr.half_blocks(phone)}\n")
        self.conf.write_text("{}")
        out = Terminal()
        with self.with_tailscale(), mock.patch.object(chat, "ensure", return_value={"url": here, "state": "up"}), \
                redirect_stdout(out):
            cli.main(["chat", "url", "--port", "1400"])
        self.assertEqual(out.getvalue(), here + "\n")                              # no phone link: no code

    def test_get_phone_qr_svg_is_the_phone_links_code_for_the_users_token_alone_1552(self):
        import http.client
        servers, _ = chat.make_servers(0, ["127.0.0.1"])
        srv = servers[0]
        port = srv.server_address[1]
        import threading
        threading.Thread(target=srv.serve_forever, daemon=True).start()

        def stop():
            srv.shutdown()
            srv.server_close()
            srv.RequestHandlerClass.hub.close()
        self.addCleanup(stop)

        def get(path, token):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            c.request("GET", path, headers={"Host": f"localhost:{port}", "Authorization": f"Bearer {token}"})
            r = c.getresponse()
            body = r.read()
            c.close()
            return r.status, r.getheader("Content-Type"), r.getheader("Cache-Control"), body
        from workbench import qr
        user = chat.token()
        with self.with_tailscale():
            code, ctype, _, body = get("/api/phone-qr.svg", user)                 # reach local: no link
            self.assertEqual((code, json.loads(body)["code"]), (404, "not_found"))
        with self.with_tailscale(FAKE_TS_SERVE=str(port)):
            for path in ("/api/phone-qr.svg", "/api/v1/phone-qr.svg"):
                code, ctype, cache, body = get(path, user)
                self.assertEqual((code, ctype, cache), (200, "image/svg+xml", "no-store"))  # the token is in it
                self.assertEqual(body.decode(), qr.svg(f"https://{NAME}/#token={user}"))
            self.assertEqual(get("/api/phone-qr.svg", "nope")[0], 401)
            code, _, _, body = get("/api/phone-qr.svg", chat.agent_add("helper"))
            self.assertEqual((code, json.loads(body)["code"]), (403, "agent_forbidden"))  # the user's token: never an agent's


if __name__ == "__main__":
    import unittest
    unittest.main()
