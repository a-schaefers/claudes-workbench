"""The admin table (settings.py, #1279): every row the web app's Settings shows, and the wb call
each one makes, as a child that isn't Claude."""
import io
import json
import os
import textwrap
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from workbench import models, routes, settings, store
from tests import test_workbench as tw  # the module: its test classes would run here too

FREE_ID = "someprovider/a-model-no-menu-lists"


def expected(key: str, value) -> list[str]:
    """The pane's wb call for each row, written out apart from settings._argv."""
    head, _, rest = key.partition("-")
    fixed = {"agents": ["route", "all", value], "busy": ["route", "busy", value], "coding": ["coding", value],
             "workflows": ["workflows", value], "record": ["stats", "set", "recording", value],
             "live": ["stats", "set", "live", value], "every": ["stats", "set", "every", value],
             "progress": ["progress", value], "splash": ["splash", value],
             "probe": ["experiment", "probe", value], "optimize": ["optimize", "start"],
             "cap": ["route", "cap", value], "price": ["savings", "--price", value, "--json"]}
    if key in fixed:
        return fixed[key]
    return {"role": ["route", rest, value], "gpu": ["route", rest, "--gpu", value],
            "api": ["route", rest, "--api", value], "think": ["route", rest, "--thinking", value],
            "size": ["model", "sizes", rest, *([] if value == "all" else [value])],
            "media": ["image" if rest == "image" else "audio", "--default", value]}[head]


class Settings(tw.Base):
    catalog = tw.ApiHarness.catalog

    def setUp(self):
        super().setUp()
        # an API provider with a key stored, so the API group has its rows (ROUTES_CATALOG's)
        self.catalog(tw.ROUTES_CATALOG, tw.ROUTES_AUTH)
        models.hosted(fresh=True)  # hosted.json: the providers the roles' JSON names
        self.routes = Path(os.environ["WB_ROUTES"])
        self.routes.write_text(json.dumps({"v": 1, "day_usd": 5, "roles": {
            "trivial": {"via": "gpu", "api": "deepseek/deepseek-flash"}}}))
        settings.drop()
        self.addCleanup(settings.drop)

    def fake_wb(self) -> Path:
        """A wb that records each write's argv (one JSON line) and passes the two reads to the real
        one, so the rows stay real while nothing changes."""
        log = Path(self.tmp.name) / "wb-calls.jsonl"
        fake = Path(self.tmp.name) / "fake-wb"
        fake.write_text(textwrap.dedent(f"""\
            import json, os, sys
            argv = sys.argv[1:]
            if argv in (["route", "--json"], ["models", "--json"]):
                os.execv(sys.executable, [sys.executable, "-X", "utf8", {str(tw.ROOT / "bin" / "wb")!r}, *argv])
            with open({str(log)!r}, "a") as f:
                f.write(json.dumps(argv) + "\\n")
            print("recorded")
            """))
        patch = mock.patch.object(settings, "WB", fake)
        patch.start()
        self.addCleanup(patch.stop)
        return log

    def test_every_row_has_a_known_group_and_kind_and_the_groups_come_in_order(self):
        rows = settings.rows()
        self.assertEqual([r for r in rows if r["group"] not in settings.GROUPS or r["kind"] not in settings.KINDS], [])
        order = [settings.GROUPS.index(r["group"]) for r in rows]
        self.assertEqual(order, sorted(order))
        keys = [r["key"] for r in rows]
        self.assertEqual(len(keys), len(set(keys)))
        json.dumps(rows)  # JSON-safe
        for want in ("agents", "busy", "role-significant", "coding", "workflows", "cap", "record", "live", "every",
                     "progress", "price", "gpu-trivial", "optimize", "api-connected", "api-writing",
                     "think-trivial", "api-spend", "splash", "probe"):
            self.assertIn(want, keys)
        self.assertNotIn("chat", keys)   # the web app is always on now
        self.assertNotIn("wake", keys)   # a plugin option, set with /plugin
        self.assertTrue(any(k.startswith("size-") for k in keys))
        by = {r["key"]: r for r in rows}
        self.assertEqual((by["cap"]["kind"], by["cap"]["value"], by["cap"]["unit"]), ("number", 5.0, "usd"))
        self.assertEqual((by["price"]["value"], by["price"]["unit"]), (None, "per_kwh"))
        self.assertTrue(by["api-trivial"]["other"])
        self.assertEqual(by["api-trivial"]["value"], "deepseek/deepseek-flash")
        self.assertEqual(by["role-trivial"]["says"], "Trivial coding runs on this machine's GPU.")

    def test_each_choice_applied_runs_the_panes_wb_call(self):
        log = self.fake_wb()
        rows = settings.rows()
        with mock.patch.object(settings, "drop", lambda: None):  # the fake changes nothing: keep the read
            for row in rows:
                tries = [c["value"] for c in row.get("choices", [])] if row["kind"] in ("choice", "action") else \
                    [(5, "5"), ("off", "off"), (0.3, "0.3")] if row["kind"] == "number" else []
                for t in tries:
                    value, said = t if isinstance(t, tuple) else (t, t)
                    with self.subTest(key=row["key"], value=value):
                        got = settings.apply(row["key"], value)
                        self.assertTrue(got["ok"], got)
                        self.assertEqual(json.loads(log.read_text().splitlines()[-1]), expected(row["key"], said))
        self.assertGreater(len(log.read_text().splitlines()), 40)

    def test_a_role_changed_from_the_web_is_stamped_by_web(self):
        got = settings.apply("role-trivial", "api")
        self.assertTrue(got["ok"], got)
        self.assertTrue(got["said"].startswith("changed: trivial: GPU → API deepseek/deepseek-flash"), got)
        row = json.loads(self.routes.read_text())["roles"]["trivial"]
        self.assertEqual((row["via"], row["by"]), ("api", "web"))
        self.assertIn("web", routes.BY)
        self.assertEqual(next(r for r in settings.rows() if r["key"] == "role-trivial")["value"], "api")

    def test_a_change_applied_under_claude_code_still_writes_ungated(self):
        # the server may have been started from Claude's Bash: child_env scrubs what wb gates on,
        # so the user's click needs no route-ok.json, as at their own terminal
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1", "CLAUDE_PID": str(os.getpid()),
                                          "CLAUDE_CODE_SESSION_ID": "s-1"}):
            env = settings.child_env()
            got = settings.apply("role-trivial", "api")
        self.assertEqual([k for k in ("CLAUDECODE", "CLAUDE_PID", "CLAUDE_CODE_SESSION_ID") if k in env], [])
        self.assertEqual(env[settings.ACTOR], "web")
        self.assertTrue(got["ok"], got)
        self.assertFalse((store.home() / "route-ok.json").exists())
        self.assertEqual(json.loads(self.routes.read_text())["roles"]["trivial"]["by"], "web")

    def test_claude_setting_wb_actor_is_still_claude(self):
        # CLAUDECODE wins: WB_ACTOR=web under Claude Code is no yes
        from workbench import cli
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1", "WB_ACTOR": "web"}), \
                redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as e:
            cli.main(["route", "trivial", "api"])
        self.assertEqual(e.exception.code, 3)
        self.assertEqual(json.loads(self.routes.read_text())["roles"]["trivial"]["via"], "gpu")

    def test_an_unknown_key_raises_key_error_and_a_value_off_the_choices_value_error(self):
        log = self.fake_wb()
        with self.assertRaises(KeyError):
            settings.apply("no-such-setting", "on")
        for key, value in (("busy", "sometimes"), ("splash", FREE_ID), ("every", 3), ("cap", -1), ("cap", "lots"),
                           ("api-spend", "x"), ("api-trivial", "--day-usd=0"), ("api-trivial", "a b"), ("probe", True)):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                settings.apply(key, value)
        self.assertFalse(log.exists())  # nothing ran for any of them

    def test_an_api_row_takes_a_free_id_and_leaves_it_to_wb_route(self):
        log = self.fake_wb()
        row = next(r for r in settings.rows() if r["key"] == "api-significant")
        self.assertNotIn(FREE_ID, [c["value"] for c in row["choices"]])
        self.assertTrue(settings.apply("api-significant", FREE_ID)["ok"])
        self.assertEqual(json.loads(log.read_text().splitlines()[-1]), ["route", "significant", "--api", FREE_ID])

    def test_wbs_refusal_is_not_ok_and_says_wbs_line(self):
        got = settings.apply("api-significant", FREE_ID)  # the real wb: not in the catalog
        self.assertFalse(got["ok"])
        self.assertEqual(got["said"], "wb: not in OpenCode's catalog: wb models lists the API models")
        self.assertNotIn("api", json.loads(self.routes.read_text())["roles"].get("significant", {}))

    def test_turning_probe_on_without_probe_returns_the_note_for_claude(self):
        # #1279's addendum (the owner, 2026-11-24): the terminal still offers to help
        empty = Path(self.tmp.name) / "no-probe"
        empty.mkdir()
        with mock.patch.dict(os.environ, {"PATH": str(empty)}):  # wb runs by its full path
            got = settings.apply("probe", "on")
            row = next(r for r in settings.rows() if r["key"] == "probe")
        self.assertTrue(got["ok"], got)
        self.assertIn("not set up", got["said"])
        self.assertTrue(got["help"].startswith("Agent code navigation (the probe experiment) is on, but it can't "
                                               "work yet. wb says: probe: on, but not set up"), got["help"])
        self.assertIn("install nothing without their yes", got["help"])
        self.assertEqual((row["value"], row["help"]), ("on", got["help"].replace(
            "; tasks already started keep theirs", "")))
        self.assertNotIn("help", settings.apply("probe", "off"))
