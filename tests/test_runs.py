"""Deep workflows (#1278): structured results, report tasks, role cards, flows and the run driver,
on the fake OpenCode (tests/fake_opencode.py)."""
import io
import json
import os
import subprocess
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from workbench import ask_mcp, cards, cli, context, runner, runs, schema, store
from tests.test_workbench import Base, run_wb, wait_phase

ROOT = Path(__file__).resolve().parent.parent
VERDICT = {"verdict": "confirmed", "confidence": 90, "evidence": "ran it: it lost the row"}


def wb(*argv) -> tuple[int, str, str]:
    """(exit status, stdout, stderr) of wb ARGV, in-process."""
    out, err, code = io.StringIO(), io.StringIO(), 0
    with redirect_stdout(out), redirect_stderr(err):
        try:
            cli.main(list(argv))
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
    return code, out.getvalue(), err.getvalue()


class Schema(unittest.TestCase):
    """schema.py: the subset a result needs, checked outside the lane."""

    SCH = {"type": "object", "required": ["findings"], "properties": {
        "findings": {"type": "array", "maxItems": 2, "items": {
            "type": "object", "required": ["id", "line"], "additionalProperties": False,
            "properties": {"id": {"type": "string"}, "line": {"type": "integer", "minimum": 1},
                           "severity": {"enum": ["high", "low"]}}}}}}

    def test_a_holding_object_has_no_errors(self):
        self.assertEqual(schema.check({"findings": [{"id": "F1", "line": 3, "severity": "low"}]}, self.SCH), [])

    def test_each_error_names_its_path(self):
        errs = schema.check({"findings": [{"line": 0, "x": 1, "severity": "mid"}, {"id": 2, "line": True},
                                          {"id": "c", "line": 1}]}, self.SCH)
        self.assertIn("$.findings: more than 2 items (3)", errs)
        self.assertIn("$.findings[0]: missing required 'id'", errs)
        self.assertIn("$.findings[0].line: below the minimum 1", errs)
        self.assertIn("$.findings[0]: unexpected key 'x'", errs)
        self.assertIn('$.findings[0].severity: "mid" is not one of ["high", "low"]', errs)
        self.assertIn("$.findings[1].id: expected string, got integer", errs)
        self.assertIn("$.findings[1].line: expected integer, got boolean", errs)

    def test_a_missing_required_key_is_said_once(self):
        self.assertEqual(schema.check({}, self.SCH), ["$: missing required 'findings'"])

    def test_a_schema_with_a_keyword_it_cannot_check_is_refused_never_half_checked(self):
        self.assertEqual(schema.valid(self.SCH), [])
        bad = schema.valid({"type": "object", "properties": {"a": {"type": "string", "pattern": "^x"}}})
        self.assertTrue(any("'pattern' isn't checked here" in e for e in bad), bad)
        self.assertTrue(schema.valid({"type": "array"}))  # a result is one object

    def test_the_last_object_is_found_after_prose_and_inside_a_fence(self):
        text = ('I read {the file}. An example: {"a": 1}.\n\n```json\n{"findings": [{"id": "F1", '
                '"line": 3}], "nested": {"x": 1}}\n```\n')
        self.assertEqual(schema.last_object(text), {"findings": [{"id": "F1", "line": 3}], "nested": {"x": 1}})
        self.assertIsNone(schema.last_object("no object here, only [1, 2]"))
        self.assertIsNone(schema.last_object(None))

    def test_every_shipped_schema_is_one_it_can_check(self):
        for p in sorted((ROOT / "agent/schemas").glob("*.json")):
            with self.subTest(schema=p.name):
                self.assertEqual(schema.valid(json.loads(p.read_text())), [])


class Cards(Base):
    """cards.py: role cards, the user's before the shipped ones."""

    def test_every_shipped_card_loads_and_stays_under_its_size(self):
        names = cards.names()
        for n in ("find", "verify", "judge", "review", "critic", "build", "fix"):
            self.assertIn(n, names)
            c = cards.load(n)
            self.assertLessEqual(len(c.body.encode()), cards.BODY_MAX)
            self.assertEqual(c.report, n not in ("build", "fix"))
            if c.report:
                self.assertEqual(c.ask, 0, n)
                self.assertIsNotNone(cards.schema_path(c.schema), n)
        self.assertEqual(cards.load("judge").tools, "none")

    def test_a_users_card_comes_first_and_a_model_or_permissions_in_it_are_dropped_and_said(self):
        mine = Path(self.tmp.name) / "roles"
        mine.mkdir()
        (mine / "find.md").write_text("---\ndescription: mine\ntools: readonly\nschema: verdict\n"
                                      "model: some/model\npermissions: {edit: allow}\nest:\n  in: 100\n"
                                      "  out: 10\n---\nMy finder.\n")
        with mock.patch.dict(os.environ, {"WB_ROLES": str(mine)}):
            c = cards.load("find")
        self.assertEqual((c.description, c.tools, c.schema, c.est, c.body), ("mine", "read", "verdict",
                                                                            {"in": 100, "out": 10}, "My finder.\n"))
        self.assertEqual(c.dropped, ["model", "permissions"])
        self.assertIn("dropped", cards.said_dropped(c))

    def test_a_card_too_long_or_with_an_unknown_key_is_refused_by_name(self):
        mine = Path(self.tmp.name) / "roles"
        mine.mkdir()
        (mine / "big.md").write_text("---\ntools: read\nschema: verdict\n---\n" + "x" * 3100)
        (mine / "odd.md").write_text("---\ntools: read\nschema: verdict\ncolour: red\n---\nhi\n")
        with mock.patch.dict(os.environ, {"WB_ROLES": str(mine)}):
            with self.assertRaisesRegex(RuntimeError, "over 3,000"):
                cards.load("big")
            with self.assertRaisesRegex(RuntimeError, "unknown key.*colour"):
                cards.load("odd")


class ReportTasks(Base):
    """`wb start --report` (#1278): read-only, no diff, one object, checked outside the lane."""

    def report(self, name, *extra, env=None):
        os.environ.update(FAKE_SCENARIO="ok", **(env or {}))
        return run_wb("start", name, "--spec", str(self.spec), "--repo", str(self.repo), "--model", "mimo",
                      "--report", *extra)

    def test_a_valid_object_reaches_the_review_notice_and_result_json_whole(self):
        # #1278's study: the report's 2,500-character cut lost the object in 13 of 31 rounds
        out = self.report("t-rep", "--schema", "verdict",
                          env={"FAKE_RESULT": json.dumps(VERDICT), "FAKE_REPORT_PAD": "4000"})
        self.assertIn("report: one object, checked against verdict; tools read", out)
        st = wait_phase("t-rep", ("review", "failed", "stuck"))
        self.assertEqual(st["phase"], "review", store.Task("t-rep").report(1))
        task = store.Task("t-rep")
        self.assertEqual(json.loads((task.dir / runner.RESULT).read_text()), VERDICT)
        row = [r for r in store.notices() if r["task"] == "t-rep"][-1]
        self.assertEqual(row["kind"], "review")
        self.assertIn('"verdict": "confirmed"', row["text"])
        self.assertIn("from final.md", row["text"])
        self.assertNotIn("diff --git", row["text"])
        self.assertEqual(st.get("auto_fixes"), 0)
        self.assertIsNone(st.get("self_reviewed"))

    def test_the_prompt_ends_with_the_report_protocol_and_the_schema_sits_in_the_rules(self):
        self.report("t-proto", "--schema", "verdict")
        wait_phase("t-proto", ("review", "failed", "stuck"))
        task = store.Task("t-proto")
        prompt = task.round_dir(1).joinpath("prompt.md").read_text()
        self.assertIn("\n---\nHow to work (workbench report protocol):", prompt)
        self.assertIn("`Lesson: <the rule, and why>`", prompt)
        self.assertIn(str(context.rules_dir(task) / "schema.json"), prompt)
        self.assertEqual(json.loads((context.rules_dir(task) / "schema.json").read_text()),
                         json.loads((ROOT / "agent/schemas/verdict.json").read_text()))
        self.assertEqual(context.handbook_text(task), context.REPORT_HANDBOOK)

    def test_an_invalid_object_is_asked_for_once_then_the_task_fails_with_no_result(self):
        self.report("t-bad", "--schema", "verdict", env={"FAKE_RESULT": '{"verdict": "maybe"}'})
        st = wait_phase("t-bad", ("review", "failed", "stuck"))
        task = store.Task("t-bad")
        self.assertEqual(st["phase"], "failed")
        self.assertEqual(st["round"], 2)
        fix = task.round_dir(2).joinpath("prompt.md").read_text()
        self.assertTrue(fix.startswith("rejected: $: missing required 'confidence'"), fix)
        self.assertIn("$.verdict: \"maybe\" is not one of", fix)
        self.assertIn("Resend the whole object", fix)
        self.assertFalse((task.dir / runner.RESULT).exists())
        self.assertFalse(task.round_dir(3).exists())
        self.assertIn("no valid result (verdict)", [r for r in store.notices() if r["task"] == "t-bad"][-1]["text"])

    def test_a_repaired_object_is_taken_and_a_valid_one_never_gets_a_repair_round(self):
        self.report("t-fix", "--schema", "verdict",
                    env={"FAKE_RESULT": '{"verdict": "confirmed"}', "FAKE_RESULT_FIX": json.dumps(VERDICT)})
        self.assertEqual(wait_phase("t-fix", ("review", "failed", "stuck"))["phase"], "review")
        self.assertEqual(json.loads((store.Task("t-fix").dir / runner.RESULT).read_text()), VERDICT)
        self.report("t-ok", "--schema", "verdict", env={"FAKE_RESULT": json.dumps(VERDICT),
                                                        "FAKE_RESULT_FIX": ""})
        self.assertEqual(wait_phase("t-ok", ("review", "failed", "stuck"))["round"], 1)

    def test_submit_result_is_checked_by_the_server_and_the_runner_reads_it(self):
        log = Path(self.tmp.name) / "submit.log"
        self.report("t-sub", "--schema", "verdict", env={"FAKE_RESULT": json.dumps(VERDICT), "FAKE_SUBMIT": "1",
                                                         "FAKE_SUBMIT_LOG": str(log)})
        st = wait_phase("t-sub", ("review", "failed", "stuck"))
        self.assertEqual(st["phase"], "review", (store.Task("t-sub").report(1), log.read_text() if log.exists() else "no log",
                                                 (store.Task("t-sub").dir / "runner.log").read_text()[-3000:]))
        self.assertIn("from submit_result", store.Task("t-sub").report(1))
        self.assertEqual(json.loads(log.read_text())["error"], False)

    def test_the_server_lists_submit_result_with_the_schema_and_refuses_a_bad_call(self):
        self.report("t-srv", "--schema", "verdict")
        wait_phase("t-srv", ("review", "failed", "stuck"))
        task = store.Task("t-srv")
        listed = ask_mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, task)
        tools = {t["name"]: t for t in listed["result"]["tools"]}
        self.assertEqual(tools["submit_result"]["inputSchema"]["required"], ["verdict", "confidence", "evidence"])
        bad = ask_mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                              "params": {"name": "submit_result", "arguments": {"verdict": "x"}}}, task)
        self.assertTrue(bad["result"]["isError"])
        self.assertIn("missing required 'confidence'", bad["result"]["content"][0]["text"])
        self.assertFalse((ask_mcp.ask_dir(task) / ask_mcp.RESULT).exists())

    def test_a_code_task_lists_no_submit_result(self):
        os.environ["FAKE_SCENARIO"] = "ok"
        self.start("t-code", "ok")
        wait_phase("t-code", ("review", "failed", "stuck"))
        listed = ask_mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, store.Task("t-code"))
        self.assertEqual([t["name"] for t in listed["result"]["tools"]], ["ask_reviewer"])

    def test_a_report_that_changes_its_worktree_is_void(self):
        # without a lane only this check holds read-only: here WB_HOME is in a temp root the lane writes whole
        self.report("t-wr", "--schema", "verdict", env={"FAKE_REPORT_WRITE": "1", "FAKE_RESULT": json.dumps(VERDICT)})
        st = wait_phase("t-wr", ("review", "failed", "stuck"))
        self.assertEqual(st["phase"], "failed")
        self.assertEqual(st["last"], runner.REPORT_CHANGED)
        self.assertFalse((store.Task("t-wr").dir / runner.RESULT).exists())

    def test_the_layer_denies_every_edit_last_and_tools_none_denies_every_tool_but_workbenchs(self):
        self.report("t-ro", "--schema", "verdict")
        self.report("t-none", "--schema", "judgments", "--tools", "none")
        for n in ("t-ro", "t-none"):
            wait_phase(n, ("review", "failed", "stuck"))
        ro = context.layer(store.Task("t-ro"))["permissions"]
        self.assertEqual(ro[-1], {"action": "edit", "resource": "*", "effect": "deny"})
        none = context.layer(store.Task("t-none"))["permissions"]
        self.assertEqual(none[-3:], [{"action": "*", "resource": "*", "effect": "deny"},
                                     {"action": "workbench_*", "resource": "*", "effect": "allow"},
                                     {"action": "doom_loop", "resource": "*", "effect": "allow"}])

    def test_the_lane_leaves_a_report_tasks_worktree_out_of_its_writes(self):
        self.report("t-lane", "--schema", "verdict")
        wait_phase("t-lane", ("review", "failed", "stuck"))
        task = store.Task("t-lane")
        lane = context.writable(task) or []
        self.assertNotIn(str(task.worktree), lane)
        self.assertIn(str(ask_mcp.ask_dir(task)), lane)
        self.assertIn(f"{task.worktree}/*", [r["resource"] for r in context.external_rules(task)
                                             if r["effect"] == "allow"])

    def test_accepting_a_report_commits_nothing_and_its_row_is_kept_from_the_learners(self):
        self.report("t-acc", "--schema", "verdict", env={"FAKE_RESULT": json.dumps(VERDICT)})
        wait_phase("t-acc", ("review",))
        head = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"], capture_output=True,
                              text=True).stdout
        out = run_wb("accept", "t-acc")  # no -m: nothing to commit
        self.assertIn("accepted t-acc: its report is kept", out)
        self.assertIn("branch delegate/t-acc deleted", out)
        self.assertEqual(subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"], capture_output=True,
                                        text=True).stdout, head)
        self.assertEqual(store.Task("t-acc").status["phase"], "accepted")
        row = store.results(trials=True)[-1]
        self.assertEqual((row["task"], row["outcome"], row["report"]), ("t-acc", "reported", True))
        self.assertEqual([r for r in store.results() if r["task"] == "t-acc"], [])

    def test_a_code_accept_still_needs_its_subject(self):
        self.start("t-msg", "ok")
        wait_phase("t-msg", ("review",))
        code, _, err = wb("accept", "t-msg")
        self.assertEqual(code, 1)
        self.assertIn('-m "<commit subject>"', err)

    def test_report_flags_are_refused_where_they_mean_nothing(self):
        for argv, said in ((["--schema", "verdict"], "--schema goes with --report"),
                           (["--tools", "none"], "--tools goes with --report"),
                           (["--report"], "names its result's schema"),
                           (["--report", "--schema", "verdict", "--test", "true"], "drop --test"),
                           (["--report", "--card", "build"], "is a code card"),
                           (["--card", "verify"], "is a report card"),
                           (["--report", "--schema", "nosuch"], "no schema 'nosuch'"),
                           (["--run", "r1"], "go together")):
            with self.subTest(argv=argv):
                os.environ["FAKE_SCENARIO"] = "ok"
                code, _, err = wb("start", "t-x", "--spec", str(self.spec), "--repo", str(self.repo),
                                  "--model", "mimo", *argv)
                self.assertEqual(code, 1, err)
                self.assertIn(said, err)
                self.assertFalse(store.Task("t-x").exists)

    def test_a_card_gives_the_report_its_schema_its_handbook_and_no_questions(self):
        self.report("t-card", "--card", "verify", env={"FAKE_RESULT": json.dumps(VERDICT)})
        wait_phase("t-card", ("review", "failed", "stuck"))
        task = store.Task("t-card")
        self.assertEqual((task.spec["schema"], task.spec["ask_budget"], task.spec["card"]["name"]),
                         ("verdict", 0, "verify"))
        handbook = (context.rules_dir(task) / "HANDBOOK.md").read_text()
        self.assertTrue(handbook.startswith("You are a skeptic."))
        self.assertNotIn(context.HANDBOOK.read_text()[:200], handbook)

    def test_a_code_card_appends_its_role_to_the_handbook_and_its_steps_reach_wbs_agent(self):
        mine = Path(self.tmp.name) / "roles"
        mine.mkdir()
        (mine / "tidy.md").write_text("---\ntools: code\nsteps: 30\n---\nTidy one file.\n")
        os.environ["WB_ROLES"] = str(mine)
        self.start("t-tidy", "ok", "--card", "tidy")
        wait_phase("t-tidy", ("review", "failed", "stuck"))
        task = store.Task("t-tidy")
        handbook = (context.rules_dir(task) / "HANDBOOK.md").read_text()
        self.assertTrue(handbook.startswith(context.HANDBOOK.read_text().rstrip("\n")))
        self.assertIn("\n## Your role\nTidy one file.\n", handbook)
        content = json.loads(runner.agent_env(task)["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(content["agents"][context.AGENT]["steps"], 30)


if __name__ == "__main__":
    unittest.main()


def finding(fid: str, claim: str) -> dict:
    return {"id": fid, "file": "README.md", "line": 1, "claim": claim, "evidence": "README.md:1",
            "fix": "say so", "severity": "medium", "score": 7}


def verdict(v: str) -> dict:
    return {"verdict": v, "confidence": 80, "evidence": "read README.md:1"}


AUDIT = {"v": 1, "name": "aud", "params": {"areas": "list"}, "stages": [
    {"id": "find", "card": "find", "items": "{params.areas}", "prompt": "Audit area {item} now."},
    {"id": "verify", "card": "verify", "from": "find", "split": "findings",
     "keep": {"path": "verdict", "in": ["confirmed"]}, "prompt": "Check this finding:\n{item}"},
    {"id": "claude", "final": True, "from": "verify"}]}


class Flows(Base):
    """A flow's checks (runs.validate): what is refused before any file is written."""

    def errs(self, **change):
        flow = json.loads(json.dumps(AUDIT))
        for k, v in change.items():
            flow[k] = v
        return "; ".join(runs.validate(flow))

    def test_every_shipped_flow_holds_and_ends_with_claude(self):
        names = [n for n, where, _ in runs.flow_names() if where == "shipped"]
        self.assertEqual(sorted(names), ["audit", "best-of", "build", "fix", "lenses", "triage"])
        for n in names:
            flow = runs.load_flow(n)
            self.assertTrue(runs.stages(flow)[-1]["final"], n)

    def test_the_last_stage_is_claudes(self):
        self.assertIn("the last stage is final", self.errs(stages=AUDIT["stages"][:2]))

    def test_an_unknown_key_or_placeholder_is_refused_by_name(self):
        st = json.loads(json.dumps(AUDIT["stages"]))
        st[0]["colour"] = "red"
        st[1]["prompt"] = "Check {finding}"
        said = self.errs(stages=st, extra=1)
        self.assertIn("unknown key 'extra'", said)
        self.assertIn("stage find: unknown key 'colour'", said)
        self.assertIn("unknown placeholder {finding}", said)
        st[1]["prompt"] = "Check {params.nope}"
        self.assertIn("{params.nope} isn't a declared param", self.errs(stages=st))

    def test_after_a_code_stage_comes_only_a_report_over_its_diff(self):
        st = [{"id": "code", "kind": "code", "items": ["x"]},
              {"id": "find", "card": "find", "from": "code", "prompt": "{item}"},
              {"id": "claude", "final": True}]
        self.assertIn("after a code stage comes only a report over its {diff}", self.errs(stages=st))
        st[1]["prompt"] = "{diff}"
        self.assertEqual(self.errs(stages=st), "")
        st.insert(2, {"id": "more", "kind": "code", "from": "find"})
        self.assertIn("stage more: after a code stage", self.errs(stages=st))

    def test_a_diff_needs_a_code_stage_and_a_refix_a_report_over_one(self):
        st = json.loads(json.dumps(AUDIT["stages"]))
        st[1]["prompt"] = "{diff}"
        st[1]["refix"] = {"path": "must_fix", "max": 3}
        said = self.errs(stages=st)
        self.assertIn("{diff} needs from a code stage", said)
        self.assertIn("refix is {path, max: 1 to 2}", said)

    def test_a_template_fills_each_place_and_says_what_it_cut(self):
        out = runs.render("{item.id}: {item} {{literal}} {params.p} {run}/{stage}",
                          {"item": {"id": "F1", "x": "y" * 20_000}, "params": {"p": "P"}, "run": "r", "stage": "s"})
        self.assertTrue(out.startswith('F1: ```json\n{\n "id": "F1"'))
        self.assertIn("characters cut]", out)
        self.assertTrue(out.endswith(" {literal} P r/s"))

    def test_params_are_read_by_their_declared_type(self):
        self.assertEqual(runs.parse_params(AUDIT, ["areas=a, b"]), {"areas": ["a", "b"]})
        self.assertEqual(runs.parse_params(AUDIT, ['areas=["a"]']), {"areas": ["a"]})
        with self.assertRaisesRegex(RuntimeError, "needs --param areas=<list>"):
            runs.parse_params(AUDIT, [])
        with self.assertRaisesRegex(RuntimeError, "the flow takes areas"):
            runs.parse_params(AUDIT, ["x=1"])


class Keywords(unittest.TestCase):
    """The keywords' reference (#1278): the hosts hold to tests/fixtures/keywords.json."""

    def test_each_fixture_case_reads_as_it_says(self):
        cases = json.loads((ROOT / "tests/fixtures/keywords.json").read_text())["cases"]
        for c in cases:
            with self.subTest(text=c["text"]):
                self.assertEqual(runs.keywords(c["text"]), {"deepcode": c["deepcode"], "deepmode": c["deepmode"]})

    def test_the_rule_names_workbenchs_word_and_the_skills_section(self):
        self.assertIn("`deepcode`", runs.DEEPCODE_RULE)
        self.assertIn("*Deep\nworkflows*".replace("\n", " "), runs.DEEPCODE_RULE.replace("\n", " "))
        self.assertIn("wb run start", runs.DEEPCODE_RULE)


class Runs(Base):
    """`wb run` (#1278): the driver starts each item with wb start, takes its notice, checks its
    result and wakes Claude once, at a checkpoint or the end."""

    def setUp(self):
        super().setUp()
        os.environ["FAKE_SCENARIO"] = "ok"

    def tearDown(self):
        # the drivers first, while WB_HOME is still the test's (Base.tearDown puts the user's back)
        for n in runs.all_runs():
            runs._stop_driver(runs.Run(n))
        super().tearDown()

    def plan(self, flow: dict) -> str:
        p = Path(self.tmp.name) / f"{flow['name']}.json"
        p.write_text(json.dumps(flow))
        return str(p)

    def wait_run(self, name, phases, timeout=120):
        deadline = time.time() + timeout
        while time.time() < deadline:
            st = runs.Run(name).status
            if st.get("phase") in phases:
                return st
            time.sleep(0.3)
        log = (runs.run_dir(name) / "driver.log")
        self.fail(f"run {name} still {runs.Run(name).status}; journal {runs.Run(name).journal()[-6:]}; "
                  f"log {log.read_text()[-2000:] if log.exists() else ''}")

    def run_notices(self, name):
        return [r for r in store.notices(runs=True) if r.get("run") == name and "task" not in r]

    def test_an_audit_runs_to_the_end_keeps_the_confirmed_and_wakes_claude_once(self):
        os.environ["FAKE_RESULT_BY"] = json.dumps({
            "Audit area alpha": {"findings": [finding("A1", "claim-one"), finding("A2", "claim-two")],
                                 "covered": ["README.md"]},
            "Audit area beta": {"findings": [finding("B1", "claim-three")], "covered": ["README.md"]},
            "claim-one": verdict("confirmed"), "claim-two": verdict("refuted"),
            "claim-three": verdict("confirmed")})
        out = run_wb("run", "start", "aud", "--plan", self.plan(AUDIT), "--param", "areas=alpha,beta",
                     "--repo", str(self.repo))
        self.assertIn("run aud: flow aud · find significant (GPU), verify significant (GPU)", out)
        self.assertIn("stop: wb run stop aud", out)
        self.wait_run("aud", ("review", "failed", "paused", "checkpoint", "stopped"))
        rep = runs.report_of("aud")
        self.assertEqual(rep["phase"], "review", rep)
        self.assertEqual(sorted(f["result"]["verdict"] for f in rep["final"]), ["confirmed", "confirmed"])
        self.assertEqual([f["result"]["verdict"] for f in rep["filtered"]], ["refuted"])
        self.assertEqual(rep["filtered"][0]["why"], 'verdict is "refuted"')
        trails = [t["trail"][0]["result"]["findings"][0]["id"] for t in rep["final"]]
        self.assertIn("B1", trails)
        # every item's notice went to the run's driver; Claude got one, the end
        tasks = runs.run_tasks(runs.Run("aud"))
        self.assertEqual(len(tasks), 5)
        self.assertEqual([r for r in store.notices() if r["task"] in tasks], [])
        for n in tasks:
            self.assertEqual(store.Task(n).status["phase"], "accepted", n)
            self.assertEqual(store.Task(n).spec["group"], "aud")
        woke = self.run_notices("aud")
        self.assertEqual([r["kind"] for r in woke], ["run_done"])
        self.assertTrue(woke[0]["text"].startswith("[workbench] Run `aud` is done: 2 result(s), 1 filtered"),
                        woke[0]["text"])
        self.assertEqual(json.loads((runs.run_dir("aud") / "result.json").read_text())["final"], rep["final"])
        said = run_wb("run", "report", "aud")
        self.assertIn("final (2):", said)
        self.assertIn('filtered (1):\n  verify:1.2: verdict is "refuted"', said)
        self.assertEqual([r for r in store.results() if r["task"] in tasks], [])  # kept from the learners
        # wb wait takes the run's notice once; the mod's `wb ack --run` sees it taken
        self.assertIn("[run_done] run aud\n[workbench] Run `aud` is done", run_wb("wait", "aud"))
        code, _, err = wb("ack", "aud", str(woke[0]["ts"]), "--run")
        self.assertEqual((code, err.strip()), (3, "wb: run aud: already acknowledged"))
        self.assertIn("closed aud", run_wb("run", "close", "aud"))
        self.assertEqual(runs.Run("aud").status["phase"], "closed")

    def test_a_review_sends_its_must_fix_back_to_the_coder_then_the_change_waits_for_claude(self):
        seq = Path(self.tmp.name) / "seq.json"
        seq.write_text(json.dumps([
            {"verdict": "changes", "must_fix": ["name the answer file in the README"], "nits": [], "summary": "close"},
            {"verdict": "approve", "must_fix": [], "nits": [], "summary": "good"}]))
        os.environ["FAKE_RESULT_SEQ"] = str(seq)
        run_wb("run", "start", "bld", "--flow", "build", "--param", "request=Write the answer to answer.txt.",
               "--repo", str(self.repo))
        self.wait_run("bld", ("review", "failed", "paused", "checkpoint", "stopped"))
        rep = runs.report_of("bld")
        self.assertEqual(rep["phase"], "review", rep)
        self.assertEqual([s["why"] for s in rep["superseded"]], ["refix 1 sent to bld-code-1"])
        self.assertEqual([f["result"]["verdict"] for f in rep["final"]], ["approve"])
        self.assertEqual(rep["code"], [{"key": "code:1", "task": "bld-code-1", "phase": "review"}])
        code = store.Task("bld-code-1")
        self.assertEqual(code.status["round"], 2)
        self.assertIn("name the answer file in the README", code.round_dir(2).joinpath("prompt.md").read_text())
        self.assertIn("1 change(s) at review", self.run_notices("bld")[-1]["text"])
        rc, _, err = wb("run", "close", "bld")
        self.assertEqual(rc, 1)
        self.assertIn("has code waiting on you: bld-code-1", err)
        run_wb("discard", "bld-code-1")
        self.assertIn("closed bld", run_wb("run", "close", "bld"))

    def test_a_gpu_budget_pauses_before_a_start_and_amend_then_resume_goes_on(self):
        os.environ["FAKE_RESULT_BY"] = json.dumps({"Audit area": {"findings": [], "covered": ["README.md"]}})
        run_wb("run", "start", "bud", "--plan", self.plan({**AUDIT, "name": "bud"}), "--param", "areas=a",
               "--repo", str(self.repo), "--budget-min", "0")
        st = self.wait_run("bud", ("paused", "review", "failed"))
        self.assertEqual(st["phase"], "paused")
        self.assertIn("the run's GPU budget: 0 of 0 minutes used", st["why"])
        self.assertEqual(runs.run_tasks(runs.Run("bud")), [])
        self.assertIn("Run `bud` is paused", self.run_notices("bud")[-1]["text"])
        self.assertIn("amended bud: GPU budget 100 min. It is paused: wb run resume bud",
                      run_wb("run", "amend", "bud", "--budget-min", "100"))
        run_wb("run", "resume", "bud")
        self.assertEqual(self.wait_run("bud", ("review", "failed"))["phase"], "review")
        self.assertEqual([r["kind"] for r in self.run_notices("bud")], ["run_paused", "run_done"])

    def test_a_claude_checkpoint_waits_for_its_feed(self):
        flow = {"v": 1, "name": "chk", "params": {"areas": "list"}, "stages": [
            AUDIT["stages"][0],
            {"id": "merge", "claude": True, "from": "find"},
            {"id": "verify", "card": "verify", "from": "merge", "prompt": "Check this finding:\n{item}"},
            {"id": "end", "final": True, "from": "verify"}]}
        os.environ["FAKE_RESULT_BY"] = json.dumps({
            "Audit area": {"findings": [finding("A1", "claim-one")], "covered": ["README.md"]},
            "claim-one": verdict("confirmed"), "merged-claim": verdict("unsure")})
        run_wb("run", "start", "chk", "--plan", self.plan(flow), "--param", "areas=a,b", "--repo", str(self.repo))
        st = self.wait_run("chk", ("checkpoint", "review", "failed"))
        self.assertEqual(st["phase"], "checkpoint")
        self.assertIn("stage merge is yours: 2 result(s) of find are in", self.run_notices("chk")[-1]["text"])
        self.assertIn("find:1:", run_wb("run", "report", "chk", "--stage", "find"))
        feed = Path(self.tmp.name) / "feed.json"
        feed.write_text(json.dumps([finding("M1", "merged-claim")]))
        self.assertIn("fed chk merge: 1 item(s)", run_wb("run", "feed", "chk", "merge", "--file", str(feed)))
        self.assertEqual(self.wait_run("chk", ("review", "failed"))["phase"], "review")
        self.assertEqual([f["result"]["verdict"] for f in runs.report_of("chk")["final"]], ["unsure"])

    def test_a_dead_driver_stops_its_run_once_and_resume_goes_on_from_the_journal(self):
        os.environ["FAKE_RESULT_BY"] = json.dumps({"Audit area": {"findings": [], "covered": ["README.md"]}})
        run_wb("run", "start", "dead", "--plan", self.plan({**AUDIT, "name": "dead"}), "--param", "areas=a",
               "--repo", str(self.repo), "--budget-min", "0")
        self.wait_run("dead", ("paused",))
        run = runs.Run("dead")
        runs._stop_driver(run)  # then as a reboot leaves it: running, its driver's pid gone
        run.set_status(phase="running", pid=2 ** 22 + 7)
        st = run.status
        st["updated_at"] = time.time() - 60
        store.write_json(run.dir / "status.json", st)
        self.assertIn("run dead · flow dead · stopped", run_wb("status"))
        self.assertIn("its driver died", run.status["why"])
        run_wb("status")
        self.assertEqual([r["kind"] for r in self.run_notices("dead")], ["run_paused", "run_stopped"])
        run.amend(budget_min=None)
        self.assertIn("resumed dead", run_wb("run", "resume", "dead"))
        self.assertEqual(self.wait_run("dead", ("review", "failed"))["phase"], "review")

    def test_a_name_is_a_run_or_a_task_never_both(self):
        os.environ["FAKE_RESULT_BY"] = json.dumps({"Audit area": {"findings": [], "covered": ["README.md"]}})
        run_wb("run", "start", "nm", "--plan", self.plan({**AUDIT, "name": "nm"}), "--param", "areas=a",
               "--repo", str(self.repo), "--budget-min", "0")
        code, _, err = wb("start", "nm", "--spec", str(self.spec), "--repo", str(self.repo), "--model", "mimo")
        self.assertEqual(code, 1)
        self.assertIn("a run is named 'nm'", err)
        self.start("t-plain", "ok")
        code, _, err = wb("run", "start", "t-plain", "--flow", "audit", "--param", "areas=a", "--param", "focus=x")
        self.assertIn("a task is named 't-plain'", err)
        code, _, err = wb("run", "start", "nm", "--flow", "audit")
        self.assertIn("run 'nm' exists: run names are never reused", err)
        code, _, err = wb("run", "start", "nm2", "--flow", "audit", "--overnight", "--param", "areas=a",
                          "--param", "focus=x", "--repo", str(self.repo))
        self.assertIn("--overnight goes with --budget-usd", err)
        self.assertFalse(runs.run_dir("nm2").exists())


class Wiring(Base):
    """The run's pieces in the rest of wb (#1278)."""

    def open_run(self, name="wr"):
        d = runs.run_dir(name)
        d.mkdir(parents=True)
        store.write_json(d / "run.json", {"name": name, "flow": AUDIT})
        runs.Run(name).set_status(phase="running")
        return runs.Run(name)

    def test_an_open_runs_item_wakes_its_driver_but_its_question_still_wakes_claude(self):
        self.open_run()
        t = store.Task("wr-find-1")
        t.dir.mkdir(parents=True)
        store.write_json(t.dir / "task.json", {"name": t.name, "run": "wr", "stage": "find", "item": "1"})
        t.notify("review", "the report")
        t.notify("question", "which file?", id="q1")
        self.assertEqual([r["kind"] for r in store.notices() if r["task"] == t.name], ["question"])
        self.assertEqual([r["kind"] for r in store.read_jsonl(runs.run_dir("wr") / "notify.jsonl")[0]], ["review"])
        self.assertFalse((t.dir / "noticed").read_text() == "")
        runs.Run("wr").set_status(phase="review")
        t.notify("review", "again")  # the run no longer open: Claude's again
        self.assertEqual([r["kind"] for r in store.notices() if r["task"] == t.name], ["question", "review"])

    def test_run_notices_are_kept_apart_from_task_notices_and_rotated_once_taken(self):
        run = self.open_run()
        row = run.notify("run_checkpoint", "stage merge is yours")
        self.assertEqual(store.notices(), [])
        self.assertEqual([r["run"] for r in store.notices(runs=True)], ["wr"])
        self.assertEqual(cli._rotate_notices(0), 0)  # not taken: kept
        run.claim_ack(row["ts"])
        self.assertEqual(cli._rotate_notices(-1), 1)

    def test_a_runs_wake_is_charged_to_its_tasks(self):
        from workbench import spend
        m = spend.WAKE.match("[workbench] Run `wr` needs Claude: stage merge is yours")
        self.assertEqual(spend._woken(m, {"wr-find-1", "wr-find-2", "other"}), {"wr-find-1", "wr-find-2"})
        m = spend.WAKE.match("[workbench] Task `other` is ready for review")
        self.assertEqual(spend._woken(m, {"wr-find-1", "other"}), {"other"})

    def test_wb_stop_all_stops_a_runs_driver_first(self):
        run = self.open_run()
        self.assertIn("wb run wr", run_wb("stop", "--all"))
        self.assertEqual(run.status["phase"], "stopped")

    def test_deepmode_is_workflows_new_name_and_prints_the_deepcode_rule(self):
        self.assertEqual(run_wb("deepmode"), "Dynamic workflows (deepmode): Off (default). Claude runs one task at "
                                             "a time; deepcode asks for a deep workflow once.\n")
        self.assertIn("Dynamic workflows (deepmode): On.", run_wb("deepmode", "on"))
        self.assertIn("On.", run_wb("workflows"))
        self.assertEqual(run_wb("deepmode", "--rule"), runs.DEEPCODE_RULE + "\n")

    def test_gc_archives_a_closed_run_and_keeps_its_name_taken(self):
        run = self.open_run("old")
        run.set_status(phase="closed")
        st = run.status
        st["updated_at"] = time.time() - 40 * 86400
        store.write_json(run.dir / "status.json", st)
        self.assertEqual(runs.archive(30), 1)
        self.assertFalse(run.exists)
        self.assertIn("run names are never reused", runs.taken("old"))
