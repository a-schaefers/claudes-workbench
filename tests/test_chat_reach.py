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


class Reach(Base):
    def setUp(self):
        super().setUp()
        tmp = Path(self.tmp.name)
        self.bin = tmp / "ts-bin"
        self.bin.mkdir()
        # `tailscale status --json` up (FAKE_TS=down: stopped), `serve status --json` serving the
        # chat's port when FAKE_TS_SERVE names it
        (self.bin / "tailscale").write_text(textwrap.dedent(f"""\
            #!{sys.executable}
            import json, os, sys
            if sys.argv[1:] == ["status", "--json"]:
                up = os.environ.get("FAKE_TS") != "down"
                print(json.dumps({{"BackendState": "Running" if up else "Stopped", "Self": {{
                    "DNSName": "{NAME}.", "HostName": "box", "TailscaleIPs": ["100.64.0.7", "fd7a::7"]}}}}))
            elif sys.argv[1:] == ["serve", "status", "--json"]:
                port = os.environ.get("FAKE_TS_SERVE")
                print(json.dumps({{"Web": {{"{NAME}:443": {{"Handlers": {{"/": {{"Proxy": f"http://127.0.0.1:{{port}}"}}}}}}}}}}
                                 if port else {{}}))
            else:
                sys.exit(2)
            """))
        (self.bin / "tailscale").chmod(0o755)
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

    def test_local_drops_the_bind_and_a_bind_of_the_users_own_reads_custom(self):
        self.conf.write_text(json.dumps({"bind": ["127.0.0.1", "tailscale"], "hosts": ["laptop"]}))
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
        self.assertEqual(cli.reach_hint(), story.text("link.reach"))
        self.conf.write_text(json.dumps({"bind": ["127.0.0.1", "tailscale"]}))
        self.assertEqual(cli.reach_hint(), "")


if __name__ == "__main__":
    import unittest
    unittest.main()
