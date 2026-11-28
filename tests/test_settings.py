"""The admin table (settings.py, #1279): every row the web app's Settings shows, and the wb call
each one makes, as a child that isn't Claude."""
import io
import json
import os
import textwrap
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from workbench import cli, context, models, routes, settings, store
from tests import test_workbench as tw  # the module: its test classes would run here too

FREE_ID = "someprovider/a-model-no-menu-lists"


def expected(key: str, value) -> list[str]:
    """The pane's wb call for each row, written out apart from settings._argv."""
    head, _, rest = key.partition("-")
    fixed = {"agents": ["route", "all", value], "busy": ["route", "busy", value], "coding": ["coding", value],
             "workflows": ["workflows", value], "deep-side": ["workflows", "side", value],  # #1627
             "progress": ["progress", value], "splash": ["splash", value],
             "probe": ["experiment", "probe", value], "optimize": ["optimize", "start"],
             "cap": ["route", "cap", value], "driver": ["route", "driver", value],
             "upstream": ["upstream", "mode", value], "outbox": ["upstream", value]}  # Posting to GitHub
    if head == "remove":  # a role of the user's own (#1625)
        return ["route", "remove", rest]
    if key in fixed:
        return fixed[key]
    return {"role": ["route", rest, value], "gpu": ["route", rest, "--gpu", value],
            "api": ["route", rest, "--api", value], "think": ["route", rest, "--thinking", value],
            "review": ["route", rest, "--thinking-review", value],  # #1059
            "size": ["model", "sizes", rest, *([] if value == "all" else [value])],
            "media": ["image" if rest == "image" else "audio", "--default", value],
            "ocskill": ["skill", "opencode", rest, value]}[head]  # #1463 P4


class Rows(tw.Base):
    """The rows over a test's own roles and catalog (no tests of its own)."""
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


class Settings(Rows):
    def test_every_row_has_a_known_group_and_kind_and_the_groups_come_in_order(self):
        rows = settings.rows()
        self.assertEqual([r for r in rows if r["group"] not in settings.GROUPS or r["kind"] not in settings.KINDS], [])
        order = [settings.GROUPS.index(r["group"]) for r in rows]
        self.assertEqual(order, sorted(order))
        keys = [r["key"] for r in rows]
        self.assertEqual(len(keys), len(set(keys)))
        json.dumps(rows)  # JSON-safe
        for want in ("agents", "busy", "role-significant", "coding", "workflows", "cap",
                     "progress", "gpu-trivial", "optimize", "api-connected", "api-writing",
                     "think-trivial", "review-trivial", "splash", "probe"):
            self.assertIn(want, keys)
        self.assertNotIn("reach", keys)  # the web app listens on localhost only (#1616, 2026-11-27)
        self.assertNotIn("api-spend", keys)  # today's spend is the cap row's note now (#1339)
        self.assertNotIn("review-writing", keys)  # only a role with an API model (#1059)
        self.assertNotIn("chat", keys)   # the web app is always on now
        self.assertNotIn("wake", keys)   # a plugin option, set with /plugin
        for gone in ("record", "live", "every", "price"):  # the pane's Settings dropped them (#1322)
            self.assertNotIn(gone, keys)
        self.assertTrue(any(k.startswith("size-") for k in keys))
        by = {r["key"]: r for r in rows}
        self.assertEqual((by["cap"]["kind"], by["cap"]["value"], by["cap"]["unit"]), ("number", 5.0, "usd"))
        self.assertTrue(by["api-trivial"]["other"])
        self.assertEqual(by["api-trivial"]["value"], "deepseek/deepseek-flash")
        self.assertEqual(by["role-trivial"]["says"], "Trivial coding runs on this machine's GPU.")

    def test_the_cap_row_says_todays_api_spend_once_a_role_is_on_api_and_no_row_of_its_own_does(self):
        # #1339's UX review (2026-11-26): the day's spend, which decides whether API tasks stop,
        # was its own "Spend today" row 3.4k px down the page from the cap it is checked against
        says = lambda: {r["key"]: r for r in settings.rows(fresh=True)}["cap"]["says"]
        self.assertEqual(says(), "API tasks stop for the day at $5 of spend.")  # all GPU, nothing spent
        doc = json.loads(self.routes.read_text())
        doc["roles"]["trivial"]["via"] = "api"
        self.routes.write_text(json.dumps(doc))
        self.assertEqual(says(), "API tasks stop for the day at $5 of spend: $0 so far today.")
        store.append_jsonl(store.home() / "spend-api.jsonl",
                           {"day": time.strftime("%Y-%m-%d"), "kind": "step", "provider": "deepseek", "usd": 0.12})
        self.assertEqual(says(), "API tasks stop for the day at $5 of spend: ≈$0.12 so far today.")
        self.assertNotIn("api-spend", [r["key"] for r in settings.rows(fresh=True)])

    def test_a_role_of_the_users_own_has_a_card_by_its_label_a_remove_and_agents_and_roles_adds_one_1625(self):
        doc = json.loads(self.routes.read_text())
        doc["roles"]["docs-fr"] = {"via": "gpu", "custom": True, "label": "French docs", "like": "writing",
                                   "about": "French translations"}
        self.routes.write_text(json.dumps(doc))
        rows = settings.rows(fresh=True)
        mine = [r for r in rows if r["group"] == "docs-fr"]
        self.assertEqual(mine[0]["key"], "role-docs-fr")
        self.assertEqual((mine[0]["card"], mine[0]["label"]), ("French docs", "French docs"))
        used = next(r for r in mine if r["key"] == "usedfor-docs-fr")   # its Used for, a row of its own (#1652)
        self.assertEqual(used["fields"][0]["value"], "French translations")
        self.assertIn("gpu-docs-fr", [r["key"] for r in mine])
        self.assertEqual(settings.label_of("gpu-docs-fr"), "French docs GPU model")
        rm = next(r for r in mine if r["key"] == "remove-docs-fr")
        self.assertTrue(rm["more"])
        self.assertEqual(settings._argv(rm, "remove"), ["route", "remove", "docs-fr"])
        groups = list(dict.fromkeys(r["group"] for r in rows))
        self.assertEqual(groups.index("docs-fr"), groups.index("writing") + 1)   # after the built-ins' cards
        add = next(r for r in rows if r["key"] == "add-role")
        self.assertEqual((add["group"], add["kind"]), ("shared", "form"))
        self.assertEqual([f["name"] for f in add["fields"]], ["name", "label", "about"])   # no Works as (#1652)
        self.assertEqual(settings._argv(add, json.dumps({"name": "tests", "label": "Tests", "about": "the tests"})),
                         ["route", "add", "tests", "--label", "Tests", "--used-for", "the tests"])
        for bad in ({"name": "Bad Name", "label": "x", "about": "x"}, {"name": "tests", "label": "Tests", "about": " "}):
            with self.assertRaises(ValueError):
                settings._argv(add, json.dumps(bad))
        self.assertEqual(settings.key_for(["route", "add", "tests", "--label", "Tests", "--used-for", "the tests"]),
                         ("add-role", "tests"))
        self.assertEqual(settings.key_for(["route", "remove", "docs-fr"]), ("remove-docs-fr", "remove"))
        self.assertEqual(settings.key_for(["route", "docs-fr", "api"]), ("role-docs-fr", "api"))
        got = settings.apply("add-role", json.dumps({"name": "tests", "label": "Tests", "about": "the tests"}))
        self.assertTrue(got["ok"], got)
        self.assertEqual(json.loads(self.routes.read_text())["roles"]["tests"]["label"], "Tests")
        self.assertTrue(any(ln.startswith("  add-role: Add a role: wb route add <name> --label TEXT")
                            for ln in cli.settings_lines(settings.rows(fresh=True))))

    def test_each_role_card_sets_its_used_for_its_skills_and_takes_an_uploaded_skill_1652(self):
        # #1652 (the owner, 2026-11-28): a role's "used for" the orchestrator calls it by, and "select if
        # they receive skills from the orchestrator automatically or use a skill i upload myself"
        by = {r["key"]: r for r in settings.rows(fresh=True)}
        used, mode, up = by["usedfor-trivial"], by["skills-trivial"], by["skillup-trivial"]
        self.assertEqual((used["group"], used["kind"], used["fields"][0]["value"]),
                         ("trivial", "form", routes.USED_FOR["trivial"]))
        self.assertEqual((mode["value"], [c["value"] for c in mode["choices"]]), ("auto", ["auto", "mine"]))
        self.assertTrue(up["more"])                                     # under More while on Auto
        self.assertEqual(up["fields"][0]["kind"], "file")
        self.assertEqual(settings._argv(used, json.dumps({"used_for": "renames"})),
                         ["route", "trivial", "--used-for", "renames"])
        with self.assertRaises(ValueError):
            settings._argv(used, json.dumps({"used_for": ""}))           # required, as at the add
        self.assertTrue(settings.apply("usedfor-trivial", json.dumps({"used_for": "renames"}))["ok"])
        self.assertEqual(routes.used_for("trivial"), "renames")
        self.assertEqual(settings.current("usedfor-trivial"), "renames")
        # the upload: a SKILL.md's text, into the library and onto the role's list, Mine
        text = "---\nname: house-style\ndescription: Use when renaming anything.\n---\nKeep names short.\n"
        got = settings.apply("skillup-trivial", json.dumps({"skill": text}))
        self.assertTrue(got["ok"], got)
        self.assertEqual(routes.skills_of("trivial"), ("mine", ["house-style"]))
        by = {r["key"]: r for r in settings.rows(fresh=True)}
        self.assertEqual((by["skills-trivial"]["value"], by["skills-trivial"]["says"]),
                         ("mine", "This role's skills: house-style."))
        self.assertFalse(by["skillup-trivial"]["more"])
        for bad in ("", "no frontmatter at all", "x" * (settings.SKILL_UPLOAD_MAX + 1)):
            with self.assertRaises(ValueError):
                settings._argv(up, json.dumps({"skill": bad}))
        self.assertEqual(settings.key_for(["route", "trivial", "--skills", "auto"]), ("skills-trivial", "auto"))
        self.assertEqual(settings.key_for(["route", "trivial", "--skills", "a,b"]), ("skills-trivial", "mine"))
        self.assertEqual(settings.key_for(["route", "trivial", "--used-for", "x"]), ("usedfor-trivial", "x"))
        self.assertTrue(settings.apply("skills-trivial", "auto")["ok"])
        self.assertEqual(routes.skills_of("trivial"), ("auto", ["house-style"]))

    def test_deep_workflows_side_is_a_row_of_the_workflows_card_and_its_command_is_logged_1627(self):
        by = {r["key"]: r for r in settings.rows(fresh=True)}
        row = by["deep-side"]
        self.assertEqual((row["group"], row["value"]), ("workflows", "roles"))
        self.assertEqual([c["value"] for c in row["choices"]], list(routes.RUN_SIDES))
        self.assertEqual(settings._argv(row, "gpu"), ["workflows", "side", "gpu"])
        for cmd in ("workflows", "deepmode"):
            self.assertEqual(settings.key_for([cmd, "side", "api"]), ("deep-side", "api"))

    def test_each_role_has_a_card_its_own_sides_model_on_it_the_other_and_thinking_under_more_1553_1626(self):
        # #1553 (2026-11-26): a role's GPU|API, GPU model, API model and thinking were in three
        # cards, the API model 2,000 px below the choice that needed it; #1626 (2026-11-27): its GPU
        # model under More read as a choice only Claude or Optimize could make
        rows = settings.rows()
        self.assertEqual(settings.GROUPS[:3], routes.ROLES)
        for name in routes.ROLES:                                   # every role on the GPU here
            mine = [r for r in rows if r["group"] == name]
            self.assertEqual(mine[0]["key"], f"role-{name}")
            self.assertFalse(mine[0].get("more"))
            self.assertEqual({r["key"].partition("-")[0] for r in mine[1:]} - {"gpu", "api", "think", "review"}, set())
            self.assertEqual([r["key"] for r in mine[1:] if not r.get("more")], [f"gpu-{name}"])
        doc = json.loads(self.routes.read_text())
        doc["roles"]["trivial"]["via"] = "api"
        self.routes.write_text(json.dumps(doc))
        mine = [r for r in settings.rows(fresh=True) if r["group"] == "trivial"]
        self.assertEqual([r["key"] for r in mine if not r.get("more")], ["role-trivial", "api-trivial"])
        self.assertTrue(next(r for r in mine if r["key"] == "gpu-trivial").get("more"))
        self.routes.write_text(json.dumps({**doc, "roles": {**doc["roles"], "trivial": {**doc["roles"]["trivial"], "via": "gpu"}}}))
        rows = settings.rows(fresh=True)
        by = {r["key"]: r for r in rows}
        for key in ("gpu-trivial", "api-trivial", "think-trivial", "review-trivial"):
            self.assertEqual(by[key]["group"], "trivial", key)
        self.assertEqual([r["group"] for r in rows if r["key"].startswith(("size-", "optimize"))],
                         ["gpu"] * len([k for k in by if k.startswith(("size-", "optimize"))]))

    def test_a_role_with_no_api_model_has_no_thinking_row_as_it_has_no_self_review_row(self):
        # the web app's review (2026-11-26): think-<role> was drawn for a role with no API model,
        # a switch that did nothing; review-<role> already came only with the model (#1059)
        keys = [r["key"] for r in settings.rows()]
        self.assertIn("think-trivial", keys)                       # trivial has one
        for name in ("significant", "writing"):
            self.assertNotIn(f"think-{name}", keys)
            self.assertNotIn(f"review-{name}", keys)
            self.assertIn(f"api-{name}", keys)                     # its picker stays

    def test_with_no_api_provider_one_line_stands_for_every_api_control_and_the_rows_stay_for_wb_settings(self):
        # the web app's review (2026-11-26): on the GPU-only path six API controls came before the
        # line saying there was no provider, and "Use the role's API model" quietly set a cap
        self.routes.write_text(json.dumps({"v": 1, "day_usd": 5, "roles": {}}))
        r, _ = settings._read_json(["route", "--json"])
        real = settings._read_json
        with mock.patch.object(settings, "_read_json", lambda argv: ({**r, "providers": {}}, "") if argv == ["route", "--json"] else real(argv)):
            by = {x["key"]: x for x in settings.rows(fresh=True)}
        self.assertEqual((by["gpu-only"]["kind"], by["gpu-only"]["group"], by["gpu-only"]["text"]),
                         ("note", "shared", "Every role runs on your GPU. To add an API model: /connect in OpenCode, or ask Claude."))
        self.assertEqual([k for k in ("agents", "busy", "cap", "driver") if by[k].get("hidden")], ["agents", "busy", "cap", "driver"])
        for name in routes.ROLES:
            self.assertTrue(by[f"role-{name}"]["fixed"], name)
            self.assertEqual(by[f"role-{name}"]["says"], f"{routes.LABELS[name]} runs on this machine's GPU.")
        self.assertEqual(by["api-none"]["text"], settings.NO_PROVIDER)
        # a provider connected: the controls are drawn, but When the GPU is busy waits for a role's API model
        self.routes.write_text(json.dumps({"v": 1, "day_usd": 5, "roles": {}}))
        by = {x["key"]: x for x in settings.rows(fresh=True)}
        self.assertNotIn("gpu-only", by)
        self.assertEqual([k for k in ("agents", "busy", "cap") if by[k].get("hidden")], ["busy"])
        self.assertFalse(any(by[f"role-{n}"].get("fixed") for n in routes.ROLES))
        self.routes.write_text(json.dumps({"v": 1, "day_usd": 5, "roles": {"trivial": {"via": "gpu", "api": "deepseek/deepseek-flash"}}}))
        self.assertFalse({x["key"]: x for x in settings.rows(fresh=True)}["busy"].get("hidden"))

    def test_with_no_gpu_model_a_gpu_role_says_its_tasks_cant_start_and_the_gpu_card_gives_a_next_step(self):
        # the web app's review (2026-11-26): a first `wb start` exited 3 and the page never said why
        m, _ = settings._read_json(["models", "--json"])
        real = settings._read_json
        with mock.patch.object(settings, "_read_json", lambda argv: ({**m, "gpu": []}, "") if argv == ["models", "--json"] else real(argv)):
            by = {x["key"]: x for x in settings.rows(fresh=True)}
        self.assertEqual(by["role-significant"]["says"],
                         "Complex coding runs on the GPU, which has no model yet: its tasks can't start.")
        self.assertEqual((by["gpu-none"]["text"], by["gpu-none"]["ask"]),
                         ("No GPU model yet: pick an API model for a role above (its More), or ask Claude to set up a "
                          "local one.", "Help me set up a local model"))
        self.assertNotIn("gpu-significant", by)
        self.assertEqual({x["key"]: x for x in settings.rows(fresh=True)}["role-significant"]["says"],
                         "Complex coding runs on this machine's GPU.")      # with a model: as before

    def test_a_role_put_on_api_before_it_has_a_model_is_told_to_pick_one_by_its_label_1558(self):
        # #1558 (2026-11-26): the refusal the page toasted was `wb: trivial has no API model yet:
        # wb route trivial <provider/model>`, a gated terminal command naming the role's key
        self.routes.write_text(json.dumps({"v": 1, "day_usd": 5, "roles": {}}))
        by = {x["key"]: x for x in settings.rows(fresh=True)}
        api = next(c for c in by["role-trivial"]["choices"] if c["value"] == "api")
        self.assertEqual((api["label"], api["needs"]), ("API (pick its model)", "api-trivial"))   # the page opens its list
        got = settings.apply("role-trivial", "api")
        self.assertFalse(got["ok"])
        self.assertEqual(got["page"], "Pick Trivial coding's API model first: tap API, or under More.")
        self.assertNotIn("wb route", got["page"])
        self.assertIn("wb route trivial", got["said"])                    # wb's whole line, for the driver
        self.assertNotIn("page", settings.apply("coding", "ask"))

    def test_wbs_line_reaches_the_page_in_its_words_by_the_rows_label_with_no_wb_and_no_command_1563(self):
        # the web app's review (2026-11-26) and #1563: every toast was wb's stdout, `wb:`, role keys
        # and `wb route …` commands under rows labelled otherwise
        got = settings.apply("api-significant", FREE_ID)                  # the real wb: not in the catalog
        self.assertEqual(got["page"], "Complex coding API model wasn't changed: not in OpenCode's catalog.")
        self.assertTrue(got["said"].startswith("wb: not in OpenCode's catalog: wb models"))   # the driver's, whole
        self.assertEqual(settings.page_line("role-trivial", "trivial: API (Agents is GPU, so this applies once Agents is "
                                                            "Both (wb route all both))", ok=True), "Set Agents to Both for this to apply.")
        self.assertEqual(settings.page_line("coding", "Claude coding: Ask me first.", ok=True), "")
        for key, said in (("gpu-trivial", "wb: x isn't a model trivial can use (wb models)"),
                          ("probe", "wb: probe: run `wb experiment probe` again")):
            line = settings.page_line(key, said)
            self.assertTrue(line.startswith(f"{settings.label_of(key)} wasn't changed"), line)
            self.assertNotRegex(line, r"wb |`|\btrivial\b")

    def test_a_folded_cards_line_comes_from_its_rows_briefs(self):
        # the web app's review (2026-11-26): the power groups fold, each with one line of its state
        by = {r["key"]: r for r in settings.rows()}
        # today's spend on the Connected row: its Spend today row went to the cap's note (#1339)
        self.assertTrue(by["api-connected"]["brief"].endswith(" · today $0 of $5"), by["api-connected"]["brief"])
        self.assertRegex(by["optimize"]["brief"], r"^\d+ models?, (not run yet|measured .+|measuring now)$")
        self.assertRegex(by["setup-size"]["brief"], r"^about \d+\.\dK tokens$")
        self.assertTrue(all(r["brief"] == "" for k, r in by.items() if k.startswith("size-")))
        self.assertTrue(by["api-connected"]["brief"])

    def test_what_your_agents_get_is_a_read_only_group_of_what_reaches_them(self):
        # #1295 P1: a global OpenCode skill reached every agent, shown nowhere
        home = Path(self.tmp.name) / "home"
        tw.write_skill(home / ".config" / "opencode" / "skills" / "team", "team", "Use when T.")
        tw.write_skill(home / ".claude" / "skills" / "mine", "mine", "Claude's.")
        os.environ["HOME"] = str(home)
        os.environ.pop("XDG_CONFIG_HOME", None)
        settings.drop()
        rows = [r for r in settings.rows() if r["group"] == "setup"]
        by = {r["key"]: r for r in rows}
        self.assertEqual({r["kind"] for r in rows if not r["key"].startswith("ocskill-")}, {"info"})
        # each global OpenCode skill has its toggle, on by default (#1463 P4)
        self.assertEqual((by["ocskill-team"]["kind"], by["ocskill-team"]["value"], by["ocskill-team"]["label"]),
                         ("choice", "on", "Skill team"))
        self.assertNotIn("ocskill-mine", by)                       # Claude's are denied already
        self.assertEqual(settings._argv(by["ocskill-team"], "off"), ["skill", "opencode", "team", "off"])
        self.assertEqual(settings.key_for(["skill", "opencode", "team", "off"]), ("ocskill-team", "off"))
        self.assertTrue(settings.apply("ocskill-team", "off")["ok"])
        self.assertEqual(settings.current("ocskill-team"), "off")
        self.assertEqual(json.loads(Path(os.environ["WB_COMMANDS"]).read_text())["denied_skills"], ["team"])
        self.assertTrue(settings.apply("ocskill-team", "on")["ok"])
        settings.drop()
        rows = [r for r in settings.rows() if r["group"] == "setup"]
        by = {r["key"]: r for r in rows}
        self.assertTrue(by["setup-instructions"]["text"].startswith("Instructions: the handbook "))
        self.assertIn("team 0.", by["setup-skills"]["text"])
        self.assertIn("and what Claude hands a task from the skill library (0)", by["setup-skills"]["text"])
        self.assertEqual(by["setup-kept"]["text"],
                         "Kept from agents: 1 skill kept out by name (Claude's, ~/.agents and workbench's own).")
        self.assertRegex(by["setup-size"]["text"], r"^About \d+\.\dK tokens, with OpenCode's own")
        self.assertNotIn("setup-plugins", by)  # none: no row
        with self.assertRaises(ValueError):
            settings.apply("setup-skills", "x")
        (home / ".config" / "opencode" / "skills" / "team" / "SKILL.md").unlink()
        settings.drop()
        by = {r["key"]: r for r in settings.rows() if r["group"] == "setup"}
        self.assertEqual(by["setup-skills"]["text"], "Skills: what Claude hands a task from the skill library (0).")

    def test_the_driver_row_reads_claude_code_fixed_without_the_opencode_plugin_and_takes_any_id_with_it(self):
        # #1316: the OpenCode path's standing driver (routes.json `driver`, wb route driver),
        # each session's own /models by default; Claude Code's driver is Claude Code
        from workbench import ocplugin
        by = {r["key"]: r for r in settings.rows()}
        self.assertEqual((by["driver"]["kind"], by["driver"]["text"]), ("info", settings.DRIVER_CLAUDE))
        conf = Path(os.environ["WB_OPENCODE_CONFIG"])
        conf.write_text(json.dumps({**json.loads(conf.read_text()), "plugins": [str(ocplugin.DIR)]}))
        settings.drop()
        row = {r["key"]: r for r in settings.rows()}["driver"]
        self.assertEqual((row["kind"], row["group"], row["value"], row["other"]), ("choice", "shared", "session", True))
        values = [c["value"] for c in row["choices"]]
        self.assertEqual(values[0], "session")
        self.assertIn("deepseek/deepseek-flash", values)              # the catalog's
        self.assertIn("ollama/qwen3.8-27b", values)                   # discover()'s
        api = next(c for c in row["choices"] if c["value"] == "deepseek/deepseek-flash")
        self.assertIn("your code in it, goes to DeepSeek", api["says"])
        log = self.fake_wb()
        self.assertTrue(settings.apply("driver", "deepseek/deepseek-flash#high")["ok"])  # any id, a variant too
        self.assertEqual(json.loads(log.read_text().splitlines()[-1]), ["route", "driver", "deepseek/deepseek-flash#high"])
        with self.assertRaises(ValueError):
            settings.apply("driver", "--help")

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
        for key, value in (("busy", "sometimes"), ("splash", FREE_ID), ("workflows", 3), ("cap", -1), ("cap", "lots"),
                           ("api-trivial", "--day-usd=0"), ("api-trivial", "a b"), ("probe", True)):
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
        self.assertEqual(got["said"], "wb: not in OpenCode's catalog: wb models lists the API models; OpenCode "
                         "lists a provider's models only once it holds its key, and it holds none for someprovider: "
                         "run `opencode auth login someprovider` in a terminal and add it, or check the id")  # #1529
        self.assertNotIn("api", json.loads(self.routes.read_text())["roles"].get("significant", {}))

    def test_a_server_with_no_opencode_on_its_path_keeps_the_good_catalog_and_sets_an_api_model(self):
        # #1342 (bar round 9): the web app's service ran on the manager's PATH, without OpenCode's
        # own install dir, so its read overwrote the terminal's hosted.json with "opencode not
        # found" and every API pick in Settings failed, said OpenCode isn't installed
        empty = Path(self.tmp.name) / "no-opencode"
        empty.mkdir()
        hosted = models.hosted_path()
        good = hosted.read_bytes()
        with mock.patch.dict(os.environ, {"PATH": str(empty)}):  # wb runs by its full path
            os.environ.pop("WB_OPENCODE_BIN")
            self.assertIn("deepseek/deepseek-flash", models.hosted_doc(fresh=True)["models"])
            self.assertEqual(hosted.read_bytes(), good)
            got = settings.apply("api-writing", "deepseek/deepseek-flash")
        self.assertTrue(got["ok"], got)
        self.assertEqual(json.loads(self.routes.read_text())["roles"]["writing"]["api"], "deepseek/deepseek-flash")
        self.assertEqual(hosted.read_bytes(), good)

    def test_with_no_catalog_read_the_api_group_says_why_not_connect(self):
        # #1342: with keys stored, a catalog OpenCode couldn't read said "No API provider
        # connected: /connect in OpenCode adds one", and the user connected again
        self.routes.write_text(json.dumps({"v": 1, "day_usd": 5, "roles": {}}))
        empty = Path(self.tmp.name) / "no-opencode"
        empty.mkdir()
        models.hosted_path().unlink()
        with mock.patch.dict(os.environ, {"PATH": str(empty)}):
            os.environ.pop("WB_OPENCODE_BIN")
            models.hosted_doc(fresh=True)                       # no good one to keep: the error written
            note = next(r for r in settings.rows(fresh=True) if r["key"] == "api-none")
        self.assertEqual(note["text"], settings.CATALOG_FAILED.format(
            why="opencode not found (OpenCode isn't installed)"))

    def test_a_copilot_sign_in_or_an_exported_key_that_runs_counts_as_connected_one_that_cant_does_not(self):
        # #1191 and #1189, carried from the pane's Settings tab when 2.0 moved it here: only a
        # stored key or Ollama's login counted, so a Copilot sign-in or a key from $VAR, which run
        # tasks, read "No API provider connected" and offered no API model
        self.routes.write_text(json.dumps({"v": 1, "day_usd": 5, "roles": {}}))
        r, _ = settings._read_json(["route", "--json"])
        for provs, connected in (
                ({"github-copilot": {"name": "GitHub Copilot", "key": "copilot", "why": "signed in to Copilot in OpenCode"}}, True),
                ({"deepseek": {"name": "DeepSeek", "key": "env", "why": "key from $DEEPSEEK_API_KEY", "runs": True}}, True),
                ({"deepseek": {"name": "DeepSeek", "key": "env", "why": "can't run: $DEEPSEEK_API_KEY isn't set"}}, False)):
            doc = {**r, "providers": provs}
            real = settings._read_json
            with mock.patch.object(settings, "_read_json", lambda argv: (doc, "") if argv == ["route", "--json"] else real(argv)):
                keys = {row["key"] for row in settings.rows(fresh=True)}
            self.assertEqual(("api-none" not in keys, "api-connected" in keys), (connected, connected), provs)

    def test_a_role_on_an_api_model_that_cant_run_says_why_in_its_row(self):
        # #1529 (2026-11-26): a role on Anthropic with no Anthropic key read "Complex coding runs on
        # anthropic/claude-haiku-5-5", and nothing on the page said it couldn't
        self.routes.write_text(json.dumps({"v": 1, "day_usd": 5, "roles": {}}))
        r, _ = settings._read_json(["route", "--json"])
        why = "not in OpenCode's catalog now: OpenCode lists a provider's models only once it holds its key"
        for status, says in (({"available": False, "resident": False, "why": why},
                              f"Complex coding can't run on anthropic/claude-haiku-5-5: {why}."),
                             ({"available": True, "resident": True, "why": "API (Anthropic)"},
                              "Complex coding runs on anthropic/claude-haiku-5-5; your code goes to its provider.")):
            roles = {**r["roles"], "significant": {**r["roles"]["significant"], "via": "api",
                                                   "api": "anthropic/claude-haiku-5-5", "api_status": status}}
            doc = {**r, "roles": roles}
            real = settings._read_json
            with mock.patch.object(settings, "_read_json", lambda argv: (doc, "") if argv == ["route", "--json"] else real(argv)):
                row = next(x for x in settings.rows(fresh=True) if x["key"] == "role-significant")
            self.assertEqual(row["says"], says, status)
            # its fix is a terminal's: the page's Ask link hands it to the driver (the web app's review, 2026-11-26)
            self.assertEqual(bool(row.get("ask")), status["available"] is False, status)

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


def menu_model(provider: str, name: str, mid: str, price=None) -> dict:
    """One of `wb models --json`'s `api` entries, as _api_stops reads it."""
    return {"id": f"{provider}/{mid}", "name": name, "provider": provider, "window": 163840,
            "provider_name": {"anthropic": "Anthropic", "deepinfra": "DeepInfra", "xai": "xAI"}[provider],
            **({"price": {"input": price[0], "output": price[1]}} if price else {})}


# #1617 (2026-11-27): the owner's 111 models showed as 12, DeepSeek then 11 of Anthropic's, as a cap
# of 12 filled the list by provider name: DeepInfra's others and every xAI model never showed
MENU = {"api": [*[menu_model("xai", f"Grok {n}", f"grok-{n}") for n in (4, 3, 2)],
                *[menu_model("deepinfra", f"Model {c}", f"m-{c}") for c in "fedcba"],
                menu_model("deepinfra", "DS", "ds", (0.1, 0.2)),
                *[menu_model("anthropic", f"Claude {c}", f"c-{c}") for c in "zyxwvu"]],
        "used": ["xai/grok-3", "gone/not-in-the-catalog"]}


class ApiMenu(unittest.TestCase):
    def test_an_api_row_lists_every_catalog_model_the_ones_in_use_first_then_a_group_per_provider_by_name_1617(self):
        stops = settings._api_stops("Complex coding", MENU, "deepinfra/ds", ["anthropic/c-x"])
        self.assertEqual(len(stops), len(MENU["api"]))  # every one: 16, past the old 12
        self.assertEqual(len({s[0] for s in stops}), len(stops))  # each once: the ones in use not again in their group
        groups = [g for i, (_, _, _, g) in enumerate(stops) if i == 0 or stops[i - 1][3] != g]
        self.assertEqual(groups, [settings.IN_USE, "Anthropic", "DeepInfra", "xAI"])  # each group once, in order
        self.assertEqual([s[0] for s in stops[:3]], ["deepinfra/ds", "anthropic/c-x", "xai/grok-3"])
        self.assertEqual([s[0] for s in stops if s[3] == "xAI"], ["xai/grok-2", "xai/grok-4"])  # by name
        self.assertEqual([s[0] for s in stops if s[3] == "DeepInfra"], [f"deepinfra/m-{c}" for c in "abcdef"])
        self.assertEqual(stops[0][1:3], ("DS · DeepInfra (160K) · $0.1/$0.2 per M",  # the option's text as before
                                        "Complex coding uses deepinfra/ds on API; your code goes to DeepInfra."))
        row = settings._choice("api-significant", "significant", "API model", stops, "deepinfra/ds", other=True)
        self.assertEqual(row["choices"][-1], {"value": "xai/grok-4", "label": "Grok 4 · xAI (160K)",
                                              "says": "Complex coding uses xai/grok-4 on API; your code goes to xAI.",
                                              "group": "xAI"})
        self.assertNotIn("group", settings._choice("x", "shared", "X", [("a", "A", "says a")], "a")["choices"][0])

    def test_wb_settings_lists_an_api_rows_models_in_use_and_counts_the_rest_1617(self):
        # every model in the page's list is 4 rows x 111 lines in wb settings, Claude's to read
        stops = settings._api_stops("Complex coding", MENU, "deepinfra/ds", [])
        row = settings._choice("api-significant", "significant", "API model", stops, "deepinfra/ds", other=True)
        lines = cli.settings_lines([row])
        self.assertEqual(lines[1:5], ["  api-significant: API model: deepinfra/ds",
                                      "    * deepinfra/ds: DS · DeepInfra (160K) · $0.1/$0.2 per M",
                                      "      xai/grok-3: Grok 3 · xAI (160K)",
                                      "      or any of the 14 other API models' exact provider/model id (wb models lists them)"])
        # a value outside In use keeps its `*` line, and leaves the count (#1617's review, 2026-11-27)
        lines = cli.settings_lines([{**row, "value": "xai/grok-4"}])
        self.assertIn("    * xai/grok-4: Grok 4 · xAI (160K)", lines)
        self.assertIn("      or any of the 13 other API models' exact provider/model id (wb models lists them)", lines)

    def test_the_driver_rows_api_pin_and_the_roles_api_models_lead_its_list_and_wb_settings_marks_the_pin_1617(self):
        # #1617's review (2026-11-27): the driver's list passed no model of its own, so a pin sank into
        # its provider's group and wb settings, which lists only In use, printed no `*` line for it
        r = {"driver": "anthropic/c-v", "roles": {"significant": {"api": "deepinfra/ds"}, "trivial": {}}}
        with mock.patch.object(settings, "_opencode_path", lambda: True):
            row = settings._driver_row(r, {**MENU, "gpu": [{"id": "gpu/a", "label": "A"}]})
        lead = [(c["value"], c.get("group")) for c in row["choices"][:5]]
        self.assertEqual(lead, [("session", None), ("gpu/a", "GPU"), ("anthropic/c-v", settings.IN_USE),
                                ("deepinfra/ds", settings.IN_USE), ("xai/grok-3", settings.IN_USE)])
        self.assertIn("    * anthropic/c-v: Claude v · Anthropic (160K)", cli.settings_lines([row]))


class RunRows(Rows):
    """The workflows group (#1278's S12): a row per run not closed, with its Stop and its Summary."""

    def make_run(self, name, phase, **status):
        from workbench import runs
        d = runs.run_dir(name)
        d.mkdir(parents=True)
        store.write_json(d / "run.json", {"name": name, "flow_name": "audit", "flow": runs.load_flow("audit"),
                                          "params": {"areas": ["a.py"]}, "created": time.time()})
        runs.Run(name).set_status(phase=phase, why=status.pop("why", ""), **status)

    def test_each_run_not_closed_has_its_row_with_stop_while_open_and_its_summary(self):
        self.make_run("night", "running", stages={"find": {"done": 1, "running": 2, "null": 0}}, spent_usd=0.25, budget_usd=2.0)
        self.make_run("held", "paused", why="paused at the day cap")
        self.make_run("ended", "review")
        self.make_run("gone", "closed")
        by = {r["key"]: r for r in settings.rows()}
        self.assertNotIn("run-gone", by)
        night = by["run-night"]
        self.assertEqual((night["group"], night["kind"], night["label"]), ("workflows", "action", "Run night"))
        self.assertEqual(night["text"], "flow audit · running (no driver) · find: 1 done, 2 running · $0.25 of $2.00")
        self.assertEqual([c["value"] for c in night["choices"]], ["stop", "summary"])
        self.assertTrue(by["run-held"]["text"].endswith(". paused at the day cap"), by["run-held"]["text"])
        self.assertEqual([c["value"] for c in by["run-ended"]["choices"]], ["summary"])  # at review: nothing to stop
        self.assertEqual(by["workflows"]["group"], "workflows")                          # deepmode beside them
        self.assertIn("audit", by["flows"]["text"])
        log = self.fake_wb()
        with mock.patch.object(settings, "drop", lambda: None):
            self.assertTrue(settings.apply("run-night", "stop")["ok"])
            self.assertTrue(settings.apply("run-night", "summary")["ok"])
            with self.assertRaises(ValueError):
                settings.apply("run-ended", "stop")
        self.assertEqual([json.loads(x) for x in log.read_text().splitlines()],
                         [["run", "stop", "night"], ["run", "report", "night"]])

    def test_customize_lists_the_users_own_cards_and_flows_with_a_diff_and_a_reset_for_a_shipped_ones_copy_1463(self):
        # #1463 P6: what the user changed, read on the phone; Reset keeps theirs as a .bak
        from workbench import cards, chat, runs
        tmp = Path(self.tmp.name)
        with mock.patch.dict(os.environ, {"WB_ROLES": str(tmp / "roles"), "WB_FLOWS": str(tmp / "flows")}):
            tw.run_wb("card", "copy", "find")
            mine = tmp / "roles" / "find.md"
            mine.write_text(mine.read_text() + "- Also read the tests.\n")
            (tmp / "flows").mkdir()
            (tmp / "flows" / "night.json").write_text(json.dumps({**runs.load_flow("audit"), "name": "night"}))
            by = {r["key"]: r for r in settings.rows(fresh=True)}
            find = by["mine-card-find"]
            self.assertEqual((find["group"], find["kind"], find["label"]), ("workflows", "action", "Card find"))
            self.assertTrue(find["text"].startswith("Yours, shadows shipped: "), find["text"])
            self.assertEqual([c["value"] for c in find["choices"]], ["diff", "reset"])
            self.assertTrue(find["ask"].startswith(f"Help me change my card find ({mine})"))
            night = by["mine-flow-night"]
            self.assertEqual((night["kind"], night["text"].split(":")[0]), ("info", "Yours, your own"))
            self.assertEqual(settings._argv(find, "diff"), ["card", "diff", "find"])
            self.assertEqual(settings.key_for(["flow", "reset", "night"]), ("mine-flow-night", "reset"))
            self.assertEqual(settings.label_of("mine-flow-night"), "Flow night")
            got = settings.apply("mine-card-find", "diff")
            self.assertTrue(got["ok"], got)
            self.assertEqual(got["said"], "find: 1 line(s) changed from the shipped one")
            self.assertIn("+- Also read the tests.", got["detail"])
            self.assertNotIn("from: ", got["detail"])
            self.assertIsNone(settings.record(find, "mine-card-find", "diff", got, "web"))
            self.assertTrue(settings.apply("mine-card-find", "reset")["ok"])
            self.assertFalse(mine.exists())
            self.assertTrue((tmp / "roles" / "find.md.bak").is_file())
            self.assertEqual(cards.load("find").path, cards.SHIPPED / "find.md")
            self.assertNotIn("mine-card-find", [r["key"] for r in settings.rows(fresh=True)])
            self.assertFalse(chat.changes_path().exists())
            with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
                tw.run_wb("flow", "diff", "night")                    # nothing shipped by its name

    def test_the_web_apps_stop_and_summary_run_wb_and_neither_is_recorded_as_a_change(self):
        from workbench import chat, runs
        self.make_run("night", "running")
        before = next(r for r in settings.rows() if r["key"] == "run-night")
        got = settings.apply("run-night", "summary")
        self.assertTrue(got["ok"], got)
        self.assertTrue(got["detail"].startswith("run night · flow audit · running"), got)
        self.assertEqual(got["said"], got["detail"].splitlines()[0])
        got = settings.apply("run-night", "stop")
        self.assertTrue(got["ok"], got)
        self.assertNotIn("detail", got)
        self.assertEqual(runs.Run("night").status["why"], runs.WEB_STOP)       # wb ran as the web's
        self.assertEqual([r["kind"] for r in store.notices(runs=True)], ["run_stopped"])
        self.assertIsNone(settings.record(before, "run-night", "stop", got, "web"))
        self.assertFalse(chat.changes_path().exists())
        self.assertEqual([c["value"] for c in next(r for r in settings.rows() if r["key"] == "run-night")["choices"]],
                         ["summary"])


class ClaudeSets(Rows):
    """wb settings (#1287): Claude explains the rows and, on the user's clear ask and yes in the
    conversation, changes one through settings.apply, as the web app does; the owner, 2026-11-24:
    "We trust Claude", so only what wb route already gates waits for the mod's dialog."""

    def claude(self):
        return mock.patch.dict(os.environ, {"CLAUDECODE": "1", "CLAUDE_PID": str(os.getpid()),
                                            "CLAUDE_CODE_SESSION_ID": "s-1"})

    def changes(self) -> list[dict]:
        from workbench import chat
        p = chat.changes_path()
        return [json.loads(ln) for ln in p.read_text().splitlines()] if p.exists() else []

    def refused(self, *argv) -> tuple[int, str]:
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit) as e:
            tw.run_wb(*argv)
        return e.exception.code, err.getvalue()

    def test_wb_settings_lists_every_row_with_its_choices_the_current_one_marked(self):
        out = tw.run_wb("settings")
        rows = settings.rows()
        lines = out.splitlines()
        for r in rows:
            with self.subTest(key=r["key"]):
                if r["kind"] == "note":
                    self.assertIn(f"  {r['text']}", lines)
                    continue
                self.assertTrue(any(ln.startswith(f"  {r['key']}") for ln in lines), r["key"])
                # an API row's models past those in use are counted, not listed (#1617)
                rest = [c for c in r.get("choices") or [] if r.get("other") and c["value"] != r["value"]
                        and c.get("group") not in (None, settings.IN_USE, "GPU")]
                # a form's one button is its fields' submit, not a value to list (#1625)
                for c in (r.get("choices") or []) if r["kind"] != "form" else []:
                    mark = "*" if r["kind"] == "choice" and c["value"] == r["value"] else " "
                    says = "" if r.get("other") else f". {c['says']}"
                    (self.assertNotIn if c in rest else self.assertIn)(f"    {mark} {c['value']}: {c['label']}{says}", lines)
                if rest:
                    self.assertIn(f"      or any of the {len(rest)} other API models' exact provider/model id "
                                  "(wb models lists them)", lines)
        self.assertIn("  coding: Claude coding: decide", lines)
        self.assertIn("  cap: API day cap: $5. API tasks stop for the day at $5 of spend. Takes a number, or off.", lines)
        self.assertEqual(lines[-1], "wb settings set <key> <value> changes one, on the user's clear ask and yes")
        self.assertEqual(json.loads(tw.run_wb("settings", "--json")), rows)

    def test_the_day_cap_row_sits_in_the_api_card_and_says_when_it_is_off_1624(self):
        # #1624 (the owner, 2026-11-27): "I dont see anything about spend caps in the settings":
        # it was in Agents and roles; and off, with a role on API, is the user's to choose
        cap = next(r for r in settings.rows() if r["key"] == "cap")
        self.assertEqual((cap["group"], cap["kind"], cap["value"]), ("api", "number", 5.0))
        self.assertTrue(cap["says"].startswith("API tasks stop for the day at $5 of spend"), cap["says"])
        doc = json.loads(self.routes.read_text())
        self.routes.write_text(json.dumps({**doc, "day_usd": None, "no_cap": True}))
        cap = next(r for r in settings.rows(fresh=True) if r["key"] == "cap")
        self.assertEqual(cap["value"], None)
        self.assertTrue(cap["says"].startswith("Off: API tasks spend with no daily limit"), cap["says"])
        self.routes.write_text(json.dumps({**doc, "day_usd": None}))
        self.assertIn("None yet: the first API role takes $5",
                      next(r for r in settings.rows(fresh=True) if r["key"] == "cap")["says"])

    def test_wb_settings_set_applies_the_change_and_records_it_by_claude(self):
        from workbench import cli
        with self.claude():
            out = tw.run_wb("settings", "set", "coding", "ask")
        self.assertEqual(cli.coding_mode(), "ask")
        self.assertEqual(out, "Claude coding: Ask me first. " + cli.CODING["ask"][1] + "\n")
        [row] = self.changes()
        self.assertEqual({k: row[k] for k in ("key", "label", "from", "to", "said", "by")},
                         {"key": "coding", "label": "Claude coding", "from": "decide", "to": "ask",
                          "said": out.strip(), "by": "claude"})
        self.assertNotIn("help", row)

    def test_a_role_moved_to_api_by_claude_still_waits_for_the_mods_dialog(self):
        self.routes.write_text(json.dumps({"v": 1, "day_usd": 5, "roles": {
            "significant": {"via": "gpu", "api": "deepseek/deepseek-flash"}}}))
        with self.claude():
            code, err = self.refused("settings", "set", "role-significant", "api")
            self.assertEqual(code, 3)
            self.assertIn("only through the workbench mod's dialog", err)
            self.assertEqual(json.loads(self.routes.read_text())["roles"]["significant"]["via"], "gpu")
            self.assertEqual(self.changes(), [])
            # the mod's dialog: the dry run's line, and on Allow its approval in route-ok.json
            q = json.loads(tw.run_wb("settings", "set", "--dry-run", "--json", "role-significant", "api"))
            self.assertTrue(q["gate"], q)
            self.assertTrue(q["change"].startswith("significant: GPU → API deepseek/deepseek-flash"), q)
            self.assertEqual(q["approval"], {"argv": ["route", "significant", "api"], "change": q["change"]})
            (store.home() / routes.ROUTE_OK).write_text(json.dumps({**q["approval"], "at": time.time()}))
            out = tw.run_wb("settings", "set", "role-significant", "api")
        self.assertTrue(out.startswith("changed: significant: GPU → API deepseek/deepseek-flash"), out)
        row = json.loads(self.routes.read_text())["roles"]["significant"]
        self.assertEqual((row["via"], row["by"]), ("api", "dialog"))
        self.assertFalse((store.home() / routes.ROUTE_OK).exists())  # one-shot
        self.assertEqual([(c["key"], c["from"], c["to"], c["by"]) for c in self.changes()],
                         [("role-significant", "gpu", "api", "claude")])

    def test_a_change_wb_route_doesnt_gate_needs_no_dialog(self):
        with self.claude():
            for argv in (("coding", "ask"), ("splash", "off")):
                self.assertEqual(json.loads(tw.run_wb("settings", "set", "--dry-run", *argv)),
                                 {"change": "", "gate": False})
            q = json.loads(tw.run_wb("settings", "set", "--dry-run", "--json", "gpu-trivial", "auto"))
        self.assertFalse(q["gate"], q)
        self.assertEqual(self.changes(), [])  # a dry run writes nothing

    def test_probe_turned_on_without_probe_prints_the_help_note(self):
        empty = Path(self.tmp.name) / "no-probe"
        empty.mkdir()
        with self.claude(), mock.patch.dict(os.environ, {"PATH": str(empty)}):
            out = tw.run_wb("settings", "set", "probe", "on")
        said, _, note = out.partition("\n")
        self.assertTrue(said.startswith("probe: on, but not set up"), out)
        self.assertTrue(note.startswith("Agent code navigation (the probe experiment) is on, but it can't work yet."), out)
        self.assertIn("install nothing without their yes", note)
        [row] = self.changes()
        self.assertEqual((row["key"], row["to"], row["by"], row["help"]), ("probe", "on", "claude", note.strip()))

    def test_an_unknown_key_or_a_value_off_its_choices_exits_2_and_runs_nothing(self):
        log = self.fake_wb()
        with self.claude():
            code, err = self.refused("settings", "set", "no-such", "on")
            self.assertEqual((code, err), (2, "wb: no setting 'no-such': wb settings lists them\n"))
            code, err = self.refused("settings", "set", "busy", "sometimes")
            self.assertEqual(code, 2)
            self.assertIn("isn't one of busy's choices", err)
            self.assertEqual(self.refused("settings", "set", "busy")[0], 2)
        self.assertFalse(log.exists())
        self.assertEqual(self.changes(), [])

    def test_an_agents_shell_cant_run_wb_settings(self):
        # #1287: the agent's shell is deny by default and wb isn't on its list
        from workbench import context
        task = store.Task("t-settings")
        task.dir.mkdir(parents=True)
        (task.dir / "task.json").write_text(json.dumps({"name": "t-settings", "repo": str(self.repo)}))
        rules = [*context.shell_rules(task), *({"action": "shell", "resource": r, "effect": "deny"}
                                                for r in context.denied_shell())]
        for cmd in ("wb settings set coding ask", "wb settings", "wb settings set role-trivial api"):
            self.assertEqual(tw.Permission.verdict(rules, cmd), "deny", cmd)


class OneWriter(Rows):
    """Every Settings write logged in one place (#1355): cli.main, around a row's own wb command
    line, whoever runs it (the web app's child, Claude's `wb settings set` child, an OpenCode
    driver's shell, the user's terminal), once, `by` settings.actor()."""

    def changes(self) -> list[dict]:
        from workbench import chat
        p = chat.changes_path()
        return [json.loads(ln) for ln in p.read_text().splitlines()] if p.exists() else []

    def test_every_rows_own_command_line_reads_back_as_its_key_and_value(self):
        rows = [r for r in settings.rows() if r["kind"] not in ("info", "note")]
        self.assertGreater(len(rows), 15)
        for r in rows:
            if r["kind"] == "form":  # its value is its fields as JSON: ..._agents_and_roles_adds_one_1625
                continue
            values = ["6"] if r["kind"] == "number" else [c["value"] for c in r["choices"]]
            for v in values:
                with self.subTest(key=r["key"], value=v):
                    self.assertEqual(settings.key_for(settings._argv(r, v)), (r["key"], v))
        # the rows a test's machine may lack: a model's tasks, an engine, the OpenCode driver
        self.assertEqual(settings.key_for(["model", "sizes", "local/m", "trivial"]), ("size-local/m", "trivial"))
        self.assertEqual(settings.key_for(["model", "sizes", "local/m"]), ("size-local/m", "all"))
        self.assertEqual(settings.key_for(["image", "--default", "comfy"]), ("media-image", "comfy"))
        with mock.patch.object(settings, "_audio_kind", return_value="music"):
            self.assertEqual(settings.key_for(["audio", "--default", "ace"]), ("media-music", "ace"))
        self.assertEqual(settings.key_for(["route", "driver", "ollama/x#high"]), ("driver", "ollama/x#high"))
        self.assertEqual(settings.key_for(["route", "trivial", "--api=deepseek/x"]), ("api-trivial", "deepseek/x"))

    def test_optimizes_button_is_logged_as_pressed_and_never_as_off_to_start(self):
        # the web app's review (2026-11-26): Recent changes said "Optimize: off → start", a value that never was
        got = settings.logged("optimize", "start", "off", "optimize: measuring")
        self.assertEqual((got["key"], got["from"], got["to"]), ("optimize", None, "start"))
        self.assertEqual([c["key"] for c in self.changes()], ["optimize"])
        tw.run_wb("coding", "ask")                                    # a setting's own change too
        self.assertEqual([c["key"] for c in self.changes()], ["optimize", "coding"])

    def test_a_read_a_dry_run_or_a_combined_line_is_no_rows_write(self):
        for argv in (["route"], ["route", "--json"], ["route", "--dry-run", "cap", "5"], ["route", "trivial"],
                     ["route", "significant", "api", "--api", "x"], ["route", "trivial", "fallback", "gpu"],
                     ["coding"], ["settings", "set", "coding", "ask"], ["optimize", "start", "--if-due"],
                     ["chat", "log"], ["chat", "url"], ["experiment"], ["model", "sizes", "m", "--x"],
                     ["experiment", "probe", "--json"], ["image", "--default", "--list"]):
            with self.subTest(argv=argv):
                self.assertIsNone(settings.key_for(argv))

    def test_a_change_at_the_terminal_logs_once_by_the_terminal_with_wbs_line_and_a_no_op_logs_nothing(self):
        from workbench import chat, cli
        out = tw.run_wb("coding", "ask")
        self.assertEqual(cli.coding_mode(), "ask")
        [row] = self.changes()
        self.assertEqual({k: row[k] for k in ("key", "label", "from", "to", "said", "by")},
                         {"key": "coding", "label": "Claude coding", "from": "decide", "to": "ask",
                          "said": out.strip(), "by": "terminal"})
        self.assertEqual(chat.settings_rev()["key"], "coding")  # an open page redraws
        tw.run_wb("coding", "ask")
        self.assertEqual(len(self.changes()), 1)
        tw.run_wb("route", "cap", "7")
        self.assertEqual([(c["key"], c["from"], c["to"]) for c in self.changes()][-1], ("cap", 5.0, 7.0))
        tw.run_wb("route", "--dry-run", "cap", "9")
        tw.run_wb("route", "--json")
        self.assertEqual(len(self.changes()), 2)

    def test_the_writer_is_who_runs_it_and_the_web_apps_mark_never_hides_a_driver(self):
        for env, by in (({"WB_ACTOR": "web"}, "web"), ({"CLAUDECODE": "1", "WB_ACTOR": "web"}, "claude"),
                        ({"OPENCODE_SESSION_ID": "ses_abc123"}, "opencode")):
            with self.subTest(by=by), mock.patch.dict(os.environ, env):
                tw.run_wb("workflows", "on" if by != "claude" else "off")
                self.assertEqual(self.changes()[-1]["by"], by)
        self.assertEqual([c["to"] for c in self.changes()], ["on", "off", "on"])

    def test_a_refused_write_logs_nothing(self):
        self.routes.write_text(json.dumps({"v": 1, "day_usd": 5, "roles": {
            "significant": {"via": "gpu", "api": "deepseek/deepseek-flash"}}}))
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1"}), redirect_stderr(err), self.assertRaises(SystemExit) as e:
            tw.run_wb("route", "significant", "api")  # gated: the mod's dialog approved none
        self.assertEqual(e.exception.code, 3, err.getvalue())
        with redirect_stderr(err), self.assertRaises(SystemExit):
            tw.run_wb("coding", "sometimes")
        self.assertEqual(self.changes(), [])


class SettingsOffers(tw.Base):
    """wb settings offers (#1334): the web app's changes due a word to the driver, which the
    OpenCode plugin asks for when a session goes idle; the mod's settingsTurns keeps the same rule."""

    def change(self, key: str, ts: float, help: str = "", by: str = "web", **row):
        from workbench import chat
        store.append_jsonl(chat.changes_path(), {"ts": ts, "key": key, "label": row.pop("label", key.title()),
                                                 "from": "off", "to": "on", "said": f"{key}: on",
                                                 **({"help": help} if help else {}), "by": by, **row})

    def offers(self, *flags: str) -> dict:
        return json.loads(tw.run_wb("settings", "offers", "--json", *flags))

    def test_each_web_change_is_offered_once_with_its_help_and_the_rest_are_notes(self):
        now = store.now()
        self.change("probe", now - 5, help="It can't work yet: offer to set it up.", label="Agent code navigation")
        self.change("splash", now - 4)
        self.change("coding", now - 3)          # its own rows say every change: not said twice
        self.change("role-trivial", now - 2)
        self.change("progress", now - 1, by="claude")  # Claude's own: it made it
        self.change("busy", now - 7200)         # over an hour old: no news
        got = self.offers("--if-due")
        [offer] = got["offers"]
        self.assertEqual(offer["key"], "probe")
        self.assertEqual(offer["text"], "[workbench] The user changed Agent code navigation from off to on on the web "
                         "app. wb said: probe: on\nIt can't work yet: offer to set it up.\nOffer to help with it, "
                         "and install or change nothing without their yes.")
        self.assertEqual(offer["toast"], "Agent code navigation changed on the web app: the driver will offer to set it up")
        self.assertEqual([n["key"] for n in got["notes"]], ["splash"])
        self.assertEqual(got["notes"][0]["text"], "The user changed Splash from off to on on the web app. wb said: splash: on")
        # said: never again, to this server or another
        self.assertEqual(self.offers("--if-due"), {"offers": [], "notes": []})
        self.change("splash", store.now())
        self.assertEqual([n["key"] for n in self.offers()["notes"]], ["splash"])
        # without --if-due nothing is spent
        self.assertEqual([n["key"] for n in self.offers("--if-due")["notes"]], ["splash"])

    def test_an_offer_made_with_no_driver_up_waits_past_the_hour_until_its_setting_changes_again(self):
        # #1557 (2026-11-26): the page promises "your driver will offer to set it up", and an
        # offer over OFFER_MAX_AGE_S old was dropped, so a driver started later never offered
        now = store.now()
        context.set_experiment("probe", "on")            # and still not set up: no probe on PATH
        self.change("probe", now - 3700, help="Offer to set probe up.", label="Agent code navigation")
        self.change("coding", now - 9000, help="Offer to help with it.")
        self.change("coding", now - 8000, by="terminal")  # changed again since: that offer is retired
        self.change("splash", now - 3700)                # a note that old is still no news
        empty = Path(self.tmp.name) / "no-probe"
        empty.mkdir()
        with mock.patch.dict(os.environ, {"PATH": str(empty)}):
            got = self.offers("--if-due")
            self.assertEqual([o["key"] for o in got["offers"]], ["probe"])
            self.assertEqual(got["notes"], [])
            self.assertEqual(self.offers("--if-due"), {"offers": [], "notes": []})  # once

    def test_an_old_offer_whose_help_no_longer_applies_is_never_made(self):
        # #1557's review (2026-11-26): probe set up outside the offer (from the other host, or by
        # hand), and days later a driver's first start still offered "probe: on, but not set up",
        # an untrue offer that starts a paid turn
        now = store.now()
        context.set_experiment("probe", "on")
        with mock.patch.dict(os.environ):
            tw.fake_probe(Path(self.tmp.name))           # working: `probe --help` lists `symbols`
            self.assertIsNone(settings.help_now("probe"))
            self.change("probe", now - 3 * 86400, help="Offer to set probe up.", label="Agent code navigation",
                        said="probe: on, but not set up")
            self.assertEqual(self.offers("--if-due"), {"offers": [], "notes": []})
            # inside the block: leaving it takes the fake probe off PATH (#1557's CI, 2026-11-26)
            self.assertEqual(json.loads(tw.run_wb("settings", "help", "probe", "--json")), {"key": "probe", "help": None})
        # retired: with probe gone again it is still never said (a new change would be)
        self.assertEqual(self.offers("--if-due"), {"offers": [], "notes": []})

    def test_with_nothing_changed_there_is_nothing_to_offer(self):
        self.assertEqual(self.offers("--if-due"), {"offers": [], "notes": []})
        self.assertEqual(tw.run_wb("settings", "offers"), "")
