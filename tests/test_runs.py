"""Deep workflows (#1278): structured results, report tasks, role cards, flows and the run driver,
on the fake OpenCode (tests/fake_opencode.py)."""
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from workbench import ask_mcp, cards, cli, context, lane, meter, models, runner, runs, schema, store
from workbench.watch import wait_exit
from tests import test_workbench  # its Lane's helpers, never its classes here: they would run twice
from tests.test_workbench import SPEND_FLASH, SPEND_FREE, SPEND_NONE, Base, home_outside_the_lane, run_wb, wait_phase

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

    @unittest.skipUnless(lane.abi(), "needs Landlock")
    @home_outside_the_lane
    def test_the_kernel_refuses_a_report_tasks_writes_to_its_worktree_and_lets_it_read(self):
        # #1278's phase 3: a lane test where Landlock exists, not only writable()'s list. WB_HOME
        # outside a temp root, which the lane writes whole (test_workbench.Lane.fake_home)
        home = test_workbench.Lane.fake_home(self)
        os.environ["WB_HOME"] = str(home / ".local" / "state" / "workbench")
        script = ("import sys, pathlib\nw = pathlib.Path(sys.argv[1])\nprint(w.joinpath('a.txt').read_text().strip())\n"
                  "try:\n    w.joinpath('b.txt').write_text('x')\n    print('wrote')\n"
                  "except PermissionError:\n    print('refused')\n")
        said = {}
        for name, report in (("t-klane", True), ("t-kcode", False)):
            task = test_workbench.Lane.bare_task(self, name, self.repo)
            store.write_json(task.dir / "task.json", {**task.spec, **({"report": True} if report else {})})
            task.worktree.mkdir()
            (task.worktree / "a.txt").write_text("read\n")
            out = subprocess.run(lane.wrap(context.writable(task), [sys.executable, "-c", script, str(task.worktree)],
                                           context.readable(task)), capture_output=True, text=True, timeout=30)
            said[name] = out.stdout.split() or [out.stderr]
        self.assertEqual(said, {"t-klane": ["read", "refused"], "t-kcode": ["read", "wrote"]})
        self.assertFalse((store.Task("t-klane").worktree / "b.txt").exists())

    def test_wb_stats_counts_report_rows_apart_from_trials(self):
        # the review of #1278 (2026-11-25): store.results' trials=True brought the report rows too
        from workbench import results
        for row in ({"task": "t-a", "trial": True}, {"task": "t-b", "report": True}):
            store.append_jsonl(store.home() / "results.jsonl", {"ts": time.time(), **row})
        self.assertTrue(results.table().endswith("left out: 1 trials, 1 report tasks"), results.table())

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
        self.assertEqual(sorted(names), ["audit", "best-of", "build", "fix", "fix-escalate", "lenses", "triage"])
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

    def test_the_coders_report_is_a_place_over_a_code_stage_only(self):
        st = json.loads(json.dumps(AUDIT["stages"]))
        st[1]["prompt"] = "{report}"
        self.assertIn("{report} needs from a code stage", self.errs(stages=st))
        st = [{"id": "code", "kind": "code", "items": ["x"]},
              {"id": "review", "card": "review", "from": "code", "prompt": "{report}\n{diff}"},
              {"id": "claude", "final": True, "from": "review"}]
        self.assertEqual(self.errs(stages=st), "")

    def test_a_compare_stage_reads_every_attempts_diffs_over_a_code_stage_with_attempts_only(self):
        st = [{"id": "code", "kind": "code", "items": ["x"], "attempts": 2},
              {"id": "choose", "card": "choose", "from": "code", "compare": True, "prompt": "{diffs}"},
              {"id": "claude", "final": True, "from": "choose"}]
        self.assertEqual(self.errs(stages=st), "")
        st[1]["prompt"] = "{diff}"
        self.assertIn("a compare stage reads {diffs}, every attempt's, not {diff}", self.errs(stages=st))
        st[1]["prompt"], st[0]["attempts"] = "{diffs}", 1
        self.assertIn("compare is a report stage over a code stage with attempts of 2 or more", self.errs(stages=st))
        st[0]["attempts"] = 2
        st[1].pop("compare")
        self.assertIn("{diffs} is a compare stage's (compare: true)", self.errs(stages=st))

    def test_a_compare_shows_the_green_attempts_alone_when_two_or_more_are_and_its_schema_names_them(self):
        def task(name, ok):
            store.write_json(store.Task(name).dir / "task.json", {"name": name, "base": "HEAD"})
            store.write_json(store.Task(name).dir / "status.json", {"tests": None if ok is None else {"ok": ok}})
            return {"task": name, "round": 1, "item": "x"}
        it = runs.Item("choose", "1", members=[("1#a1", task("c-1", True)), ("1#a2", task("c-2", False)),
                                               ("1#a3", task("c-3", True))])
        rows, said = runs._attempts(it)
        self.assertEqual([r[0] for r in rows], ["a1", "a3"])
        self.assertEqual(said, "Only the attempts whose tests pass are shown; left out: a2 (tests failed).\n\n")
        doc = runs.compare_schema({"card": "choose"}, [r[0] for r in rows])
        self.assertEqual((doc["properties"]["best"]["enum"], doc["properties"]["ranking"]["items"]["enum"]), (["a1", "a3"], ["a1", "a3"]))
        it.members = it.members[:2]  # one green: all shown, said
        rows, said = runs._attempts(it)
        self.assertEqual(([r[0] for r in rows], said), (["a1", "a2"], "Fewer than two attempts pass their tests: all are shown.\n\n"))

    def test_a_batch_member_carries_its_key_as_id_and_its_schema_answers_exactly_the_batch(self):
        self.assertEqual(runs.member("1.2", {"id": "F9", "claim": "c"}), {"id": "1.2", "claim": "c"})
        self.assertEqual(runs.member("3", "issue #12: crash"), {"id": "3", "item": "issue #12: crash"})
        doc = runs.batch_schema({"card": "judge"}, ["1.1", "1.2"])
        arr = doc["properties"]["judgments"]
        self.assertEqual((arr["minItems"], arr["maxItems"]), (2, 2))
        self.assertEqual(arr["items"]["properties"]["id"]["enum"], ["1.1", "1.2"])
        self.assertEqual(schema.valid(doc), [])
        self.assertEqual(runs.batch_schema({"card": "judge", "schema": "triage"}, ["1"])["properties"]["items"]
                         ["items"]["properties"]["id"]["enum"], ["1"])
        self.assertIsNone(runs.batch_schema({"card": "verify"}, ["1"]))  # a verdict answers by no id

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


class Customized(Base):
    """#1295: typed params, a files param and unless_exists, a flow's slash command, a dry run,
    and the shipped cards and flows a user's copy shadows."""

    def setUp(self):
        super().setUp()
        tmp = Path(self.tmp.name)
        env = mock.patch.dict(os.environ, {"WB_FLOWS": str(tmp / "flows"), "WB_ROLES": str(tmp / "roles")})
        env.start()
        self.addCleanup(env.stop)
        for f in ("src/a.py", "src/b.py", "src/c.txt", "tests/a_test.py"):
            (self.repo / f).parent.mkdir(exist_ok=True)
            (self.repo / f).write_text(f + "\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm",
                        "files"], check=True)
        (self.repo / "src" / "d.py").write_text("uncommitted\n")  # never a files param's
        self.base = runs.gitops.base_commit(self.repo, "HEAD")

    TESTS = {"v": 1, "name": "tests", "params": {
        "src": {"type": "files", "description": "what to test", "default": "src/*.py"},
        "style": {"type": "string", "choices": ["unit", "prop"], "default": "unit"},
        "note": {"type": "string", "optional": True}},
        "stages": [{"id": "write", "kind": "code", "items": "{params.src}", "unless_exists": "tests/{item.stem}_test.py",
                    "prompt": "Write {params.style} tests for {item.file} in tests/{item.stem}_test.py. {params.note}"},
                   {"id": "claude", "final": True, "from": "write"}]}

    def test_a_param_is_its_type_alone_or_an_object_with_choices_a_default_or_optional(self):
        self.assertEqual(runs.validate(self.TESTS), [])
        got = runs.parse_params(self.TESTS, [], self.repo, self.base)
        self.assertEqual((got["style"], "note" in got), ("unit", False))  # the default; optional left absent
        with self.assertRaisesRegex(RuntimeError, "one of unit, prop, not 'big'"):
            runs.parse_params(self.TESTS, ["style=big"], self.repo, self.base)
        bad = {**self.TESTS, "params": {"a": {"type": "int", "choices": ["x"]}, "b": {"type": "list", "default": "x"},
                                        "c": {"type": "string", "colour": 1}, "d": "date"}}
        said = "; ".join(runs.validate(bad))
        for part in ("param a: choices is a list of strings for a string param, of whole numbers for an int",
                     "param b: default is a list", "param c: unknown key 'colour'", "param d: a type of"):
            self.assertIn(part, said)
        with self.assertRaisesRegex(RuntimeError, "needs --param areas=<list>"):  # a bare type still reads
            runs.parse_params(AUDIT, [])
        # a default is its value as written: a json one's text stays text, never parsed
        self.assertEqual(runs.parse_params({"params": {"j": {"type": "json", "default": "as is"}}}, []), {"j": "as is"})

    def test_a_files_param_is_the_files_committed_at_the_base_each_an_item_with_its_parts(self):
        got = runs.parse_params(self.TESTS, ["src=src/*"], self.repo, self.base)["src"]
        self.assertEqual([f["file"] for f in got], ["src/a.py", "src/b.py", "src/c.txt"])  # d.py is uncommitted
        self.assertEqual(got[0], {"file": "src/a.py", "name": "a.py", "stem": "a", "dir": "src"})
        self.assertEqual(runs.file_item("Makefile"), {"file": "Makefile", "name": "Makefile", "stem": "Makefile", "dir": "."})
        with self.assertRaisesRegex(RuntimeError, "matches no file committed"):
            runs.parse_params(self.TESTS, ["src=nothing/*"], self.repo, self.base)
        with mock.patch.object(runs, "FILES_MAX", 2), self.assertRaisesRegex(RuntimeError, "matches 3 files, over 2: narrow it"):
            runs.parse_params(self.TESTS, ["src=src/*"], self.repo, self.base)

    def test_unless_exists_skips_an_item_whose_path_is_at_the_base_as_not_run_and_counts_to_no_cap(self):
        flow = json.loads(json.dumps(self.TESTS))
        flow["stages"][0]["max_items"] = 1
        params = runs.parse_params(flow, [], self.repo, self.base)
        exists = runs.existing(flow, params, self.repo, self.base)
        self.assertEqual(exists, {"write": ["tests/a_test.py"]})
        run = runs.Run("tst")
        store.write_json(run.dir / "run.json", {"flow": flow, "params": params, "exists": exists})
        items = runs.state(run)["write"]
        self.assertEqual([(i.input["file"], i.state, i.why) for i in items],
                         [("src/a.py", "not_run", "exists at base: tests/a_test.py"), ("src/b.py", "pending", "")])
        self.assertEqual(runs.estimate(flow, params, exists)[2], [])
        # a place unless_exists can't fill, or a stage not fed by a files param, is refused by name
        flow["stages"][0]["unless_exists"] = "tests/{item.base}_test.py"
        self.assertIn("{item.base} isn't one of {item.file}, {item.name}, {item.stem}, {item.dir}", "; ".join(runs.validate(flow)))
        flow["stages"][0].update(unless_exists="tests/{item.stem}_test.py", batch=2, flow="barrier")
        self.assertIn("unless_exists checks one file per item: no batch", "; ".join(runs.validate(flow)))
        self.assertIn("unless_exists goes on a stage whose items are a files param",
                      "; ".join(runs.validate({**AUDIT, "stages": [{**AUDIT["stages"][0], "unless_exists": "x"}, *AUDIT["stages"][1:]]})))

    def test_a_dry_run_says_each_stage_its_first_spec_and_the_estimate_and_writes_nothing(self):
        plan = Path(self.tmp.name) / "tests.json"
        plan.write_text(json.dumps(self.TESTS))
        code, out, err = wb("run", "start", "dry", "--plan", str(plan), "--repo", str(self.repo), "--dry-run",
                            "--args", "style=prop note='Keep them short.'")
        self.assertEqual(code, 0, err)
        self.assertIn("  write: code on significant (GPU) · 2 item(s) from {params.src}, 1 skipped: exists at base", out)
        self.assertIn("--- write's first item's spec ---\nWrite prop tests for src/b.py in tests/b_test.py. Keep them short.", out)
        self.assertIn("estimate ", out)
        self.assertFalse(runs.run_dir("dry").exists())
        self.assertEqual(store.all_tasks(), [])
        code, _, err = wb("run", "start", "dry", "--plan", str(plan), "--repo", str(self.repo), "--dry-run", "--param", "style=x")
        self.assertEqual((code, err.strip()), (1, "wb: --param style: one of unit, prop, not 'x'"))

    def test_a_run_started_in_a_subdirectory_reads_the_repo_from_its_top_and_starts_each_item_in_its_package(self):
        # #1494 (2026-11-26): from repo/src every flow command and deepcode was refused "is no git
        # repository", and with --repo <top> a package's own CLAUDE.md, test and skills were lost
        plan = Path(self.tmp.name) / "tests.json"
        plan.write_text(json.dumps(self.TESTS))
        code, out, err = wb("run", "start", "dry", "--plan", str(plan), "--repo", str(self.repo / "src"), "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("2 item(s) from {params.src}", out)  # src/*.py, a path from the top
        pkg = self.repo / "packages" / "p"
        pkg.mkdir(parents=True)
        (pkg / "CLAUDE.md").write_text("# p\nA package.\n")
        (pkg / "x.py").write_text("x\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm",
                        "a package"], check=True)
        os.environ["FAKE_SCENARIO"] = "ok"
        with mock.patch.object(runs, "spawn", return_value=0):  # the item started here, below
            code, _, err = wb("run", "start", "pkg", "--plan", str(plan), "--repo", str(pkg),
                              "--param", "src=packages/p/*.py")
        self.assertEqual(code, 0, err)
        run = runs.Run("pkg")
        self.assertEqual((run.spec["repo"], run.spec["package_dir"]), (str(self.repo.resolve()), str(pkg.resolve())))
        stage, it = runs.stages(run.flow)[0], runs.state(run)["write"][0]
        self.assertEqual(runs._start(run, stage, it, 1), ("running", ""))
        self.assertEqual(store.Task("pkg-write-1").spec.get("subdir"), "packages/p")
        wait_phase("pkg-write-1", {"review", "failed", "stuck"}, timeout=90)

    def test_the_slash_command_line_reads_as_its_fixture_says(self):
        cases = json.loads((ROOT / "tests/fixtures/flow_args.json").read_text())["cases"]
        for c in cases:
            with self.subTest(args=c["args"]):
                flow = {"name": "f", "params": c["params"]}
                if "error" in c:
                    with self.assertRaisesRegex(RuntimeError, re.escape(c["error"])):
                        runs.parse_args(flow, c["args"])
                else:
                    self.assertEqual(runs.parse_args(flow, c["args"]), c["want"])

    def test_a_command_is_one_word_never_wbs_a_hosts_nor_another_flows_and_a_repos_flow_gets_none(self):
        mine = Path(os.environ["WB_FLOWS"])
        mine.mkdir()
        (mine / "tests.json").write_text(json.dumps({**self.TESTS, "command": "tests-for"}))
        rows = {r["name"]: r for r in runs.flow_rows(self.repo)}
        self.assertEqual(rows["tests"]["command"], "tests-for")
        self.assertEqual(rows["tests"]["params"]["style"]["choices"], ["unit", "prop"])
        for cmd, said in (("Tests", "one lowercase word"), ("workbench-x", "is Workbench's own"), ("deepmode", "is Workbench's own"),
                          ("compact", "is a host's own"), ("tests-for", "is flow tests's already")):
            with self.subTest(cmd=cmd):
                self.assertIn(said, "; ".join(runs.validate({**self.TESTS, "name": "other", "command": cmd})))
        repo_flows = self.repo / ".workbench" / "flows"
        repo_flows.mkdir(parents=True)
        (repo_flows / "theirs.json").write_text(json.dumps({**self.TESTS, "name": "theirs", "command": "theirs"}))
        self.assertIsNone({r["name"]: r for r in runs.flow_rows(self.repo)}["theirs"]["command"])
        code, out, _ = wb("run", "flows", "--repo", str(self.repo))
        self.assertIn("tests (yours) /tests-for: ", out)

    def test_a_shipped_card_or_flow_copied_is_yours_says_it_shadows_and_when_the_shipped_one_moved_and_reset_sets_it_aside(self):
        code, out, err = wb("card", "copy", "find")
        self.assertEqual(code, 0, err)
        mine = Path(os.environ["WB_ROLES"]) / "find.md"
        sha = subprocess.run(["git", "hash-object", str(cards.SHIPPED / "find.md")], capture_output=True, text=True).stdout.strip()
        self.assertTrue(mine.read_text().startswith(f'---\nfrom: "{sha}"\n'))
        self.assertEqual(cards.load("find").path, mine)  # it loads, `from` and all
        self.assertIn("find (yours, shadows shipped): ", wb("card", "list")[1])
        self.assertEqual(wb("card", "copy", "find")[0], 1)  # never over yours without --force
        self.assertEqual(wb("flow", "copy", "audit")[0], 0)
        path = Path(os.environ["WB_FLOWS"]) / "audit.json"
        self.assertEqual(runs.load_flow("audit")["from"], cards.blob_sha((runs.SHIPPED / "audit.json").read_bytes()))
        self.assertIn("audit (yours, shadows shipped): ", wb("run", "flows")[1])
        doc = json.loads(path.read_text())
        path.write_text(json.dumps({**doc, "from": "0" * 40}))  # as after wb update changed the shipped one
        self.assertIn("audit (yours, shadows shipped; shipped changed since): ", wb("run", "flows")[1])
        self.assertEqual(wb("card", "copy", "verdict", "--schema")[0], 0)
        self.assertNotIn("from", cards.load_schema("verdict"))  # its record, never a keyword checked or sent
        code, out, _ = wb("flow", "reset", "audit")
        self.assertIn("yours is kept as", out)
        self.assertFalse(path.exists())
        self.assertTrue(path.with_name("audit.json.bak").exists())
        self.assertIn("audit (shipped): ", wb("run", "flows")[1])
        self.assertEqual(wb("flow", "reset", "audit")[0], 1)


class ScheduledFlows(Base):
    """#1295: `wb schedule add --flow` fires `wb run start` itself, no Claude on the unit's PATH."""

    NIGHT = {"v": 1, "name": "night", "budget_usd": 2, "params": {"areas": {"type": "list", "default": []}},
             "stages": [{"id": "find", "card": "find", "items": "{params.areas}"},
                        {"id": "claude", "final": True, "from": "find"}]}

    def setUp(self):
        super().setUp()
        from workbench import schedule
        tmp = Path(self.tmp.name)
        bin_ = tmp / "sched-bin"
        bin_.mkdir()
        (bin_ / "systemctl").write_text('#!/bin/sh\nexit 0\n')  # never the user's manager
        (bin_ / "systemctl").chmod(0o755)
        (tmp / "flows").mkdir()
        (tmp / "flows" / "night.json").write_text(json.dumps(self.NIGHT))
        env = mock.patch.dict(os.environ, {"WB_FLOWS": str(tmp / "flows"), "WB_ROLES": str(tmp / "roles"),
                                           "XDG_CONFIG_HOME": str(tmp / "xdg"),
                                           "PATH": f"{bin_}{os.pathsep}{os.environ['PATH']}"})
        env.start()
        self.addCleanup(env.stop)
        linux = mock.patch.object(schedule, "_macos", return_value=False)
        linux.start()
        self.addCleanup(linux.stop)

    def tearDown(self):
        for n in runs.all_runs():
            runs._stop_driver(runs.Run(n))
        super().tearDown()

    def test_a_flow_fires_its_run_with_no_claude_and_a_second_that_day_gets_a_name_of_its_own(self):
        from workbench import schedule
        out = run_wb("schedule", "add", "nightly", "--flow", "night", "--repo", str(self.repo), "--at", "Sun 02:00")
        self.assertIn("each fire: wb run start nightly-YYMMDD --flow night --overnight --budget-usd 2", out)
        s = schedule.read("nightly")
        self.assertEqual((s["flow"], s["budget_usd"], "file" in s, "max_turns" in s), ("night", 2, False, False))
        self.assertIn("a run of flow night", (Path(os.environ["XDG_CONFIG_HOME"]) / "systemd/user/wb-schedule-nightly.service").read_text())
        with mock.patch.object(schedule.shutil, "which", side_effect=lambda p, path=None: None if p == "claude" else "/x"):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(schedule.fire("nightly"), 0)
            # its doctor line asks for no Claude
            self.assertEqual([ok for ok, _ in schedule.doctor_lines()], [True])
        day = time.strftime("%y%m%d")
        row = schedule.runs("nightly")[-1]
        self.assertEqual((row["outcome"], row["run"], row["flow"]), ("started", f"nightly-{day}", "night"))
        spec = runs.Run(f"nightly-{day}").spec
        self.assertEqual((spec["overnight"], spec["budget_usd"], "session" in spec, spec["cwd"]),
                         (True, 2.0, False, str(self.repo.resolve())))
        self.wait_ended(f"nightly-{day}")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(schedule.fire("nightly"), 0)
        self.assertEqual(schedule.runs("nightly")[-1]["run"], f"nightly-{day}-2")
        self.assertIn(f"started run nightly-{day}-2 · flow night", schedule.summary(schedule.runs("nightly")[-1]))

    def wait_ended(self, name):
        deadline = time.time() + 60
        while time.time() < deadline and runs.Run(name).status.get("phase") in runs.OPEN:
            time.sleep(0.3)

    def test_a_flow_schedule_is_refused_a_long_name_a_param_it_lacks_and_no_budget(self):
        from workbench import schedule
        for argv, said in ((["long-name-for-a-flow", "--flow", "night"], "at most 14 characters"),
                           (["n1", "--flow", "night", "--param", "colour=red"], "the flow takes areas"),
                           (["n2", "--flow", "audit", "--param", "areas=a", "--param", "focus=f"], "flow audit has no budget_usd: --budget-usd"),
                           (["n3", "--flow", "night", "--max-turns", "5"], "--max-turns goes with --file")):
            with self.subTest(argv=argv):
                code, _, err = wb("schedule", "add", *argv, "--repo", str(self.repo), "--at", "Sun 02:00")
                self.assertEqual(code, 1)
                self.assertIn(said, err)
        self.assertEqual(schedule.names(), [])


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
        # each review reads the round it reviews' report: the coder's answer to a refix (#1134's study)
        for review, rnd in (("bld-review-1", 1), ("bld-review-2", 2)):
            said = store.Task(review).round_dir(1).joinpath("prompt.md").read_text()
            self.assertIn(f"The coder's report:\ntask bld-code-1 · ", said)
            self.assertIn(f" · round {rnd} · ", said.split("The coder's report:")[1].split("The change:")[0])
        self.assertIn("1 change(s) at review", self.run_notices("bld")[-1]["text"])
        # the driver took each item's notice (_ack_items): the code at review is no news for the
        # mod's idle net, which reads `acked` against `noticed` (the review of #1278, 2026-11-25)
        for n in runs.run_tasks(runs.Run("bld")):
            t = store.Task(n)
            self.assertGreaterEqual(cli._acked(t), float((t.dir / "noticed").read_text()), n)
        self.assertEqual([r for r in store.notices() if r["task"].startswith("bld-")], [])
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

    def wait_item(self, name, check, timeout=60):
        deadline = time.time() + timeout
        while time.time() < deadline:
            t = store.Task(name)
            if t.exists and check(t.status):
                return t
            time.sleep(0.2)
        self.fail(f"{name}: {store.Task(name).status if store.Task(name).exists else 'never started'}")

    def test_a_reboot_under_a_run_resumes_its_item_in_flight_and_the_run_ends_with_its_result(self):
        # the review of #1278 (2026-11-25): resume took up only the items a stop stopped, and a
        # reboot's item in flight, failed RESTARTED, was taken as null by the next tick
        os.environ["FAKE_RESULT_BY"] = json.dumps({"Audit area": {"findings": [], "covered": ["README.md"]}})
        os.environ["FAKE_SLEEP"] = "600"  # the item's agent still at work when the machine goes
        run_wb("run", "start", "boot", "--plan", self.plan({**AUDIT, "name": "boot"}), "--param", "areas=a",
               "--repo", str(self.repo))
        item = self.wait_item("boot-find-1", lambda st: st.get("phase") == "running" and st.get("agent_pid"))
        run = runs.Run("boot")
        runs._stop_driver(run)  # then as a reboot leaves them: the runner and the driver gone
        runner_pid, agent = item.status["pid"], item.status["agent_pid"]
        for kill in (lambda: os.kill(runner_pid, 9), lambda: os.killpg(agent, 9)):
            try:
                kill()
            except ProcessLookupError:
                pass
        wait_exit(runner_pid, 10)
        st = item.status
        st.update(boot="old-boot", updated_at=time.time() - 60)
        store.write_json(item.dir / "status.json", st)
        st = run.set_status(phase="running", pid=2 ** 22 + 7)
        st["updated_at"] = time.time() - 60
        store.write_json(run.dir / "status.json", st)
        with mock.patch.object(store, "boot_id", return_value="this-boot"):
            self.assertIn("run boot · flow boot · stopped", run_wb("status"))
        self.assertEqual(item.status["last"], cli.RESTARTED)
        (told,) = [r for r in store.notices() if r["task"] == "boot-find-1"]
        self.assertIn("wb run resume boot continues its run, them first", told["text"])
        self.assertEqual(self.run_notices("boot"), [])  # one notice: the items', which says the run
        os.environ["FAKE_SLEEP"] = "0"
        self.assertIn("resumed boot", run_wb("run", "resume", "boot"))
        self.assertEqual(self.wait_run("boot", ("review", "failed", "checkpoint"))["phase"], "review")
        rep = runs.report_of("boot")
        self.assertEqual(rep["null"], [], rep)
        (found,) = runs.state(run)["find"]
        self.assertEqual((found.state, found.output), ("done", {"findings": [], "covered": ["README.md"]}))
        self.assertEqual([(r["task"], r["by"], r["ok"]) for r in run.journal() if r.get("kind") == "resumed"],
                         [("boot-find-1", "resume", True)])

    def test_wb_run_stop_stops_its_items_and_resume_takes_them_up_where_they_were(self):
        os.environ["FAKE_RESULT_BY"] = json.dumps({"Audit area": {"findings": [], "covered": ["README.md"]}})
        os.environ["FAKE_SLEEP"] = "600"
        run_wb("run", "start", "stp", "--plan", self.plan({**AUDIT, "name": "stp"}), "--param", "areas=a",
               "--repo", str(self.repo))
        self.wait_item("stp-find-1", lambda st: st.get("phase") == "running" and st.get("agent_pid"))
        out = run_wb("run", "stop", "stp")
        self.assertIn("stopped stp and 1 item(s): stp-find-1. Done work is kept", out)
        run = runs.Run("stp")
        self.assertEqual(run.status["phase"], "stopped")
        self.assertFalse(runs.driver_alive(run))
        self.assertEqual(store.Task("stp-find-1").status["last"], runs.STOPPED_BY_CLAUDE)
        os.environ["FAKE_SLEEP"] = "0"
        run_wb("run", "resume", "stp")
        self.assertEqual(self.wait_run("stp", ("review", "failed", "checkpoint"))["phase"], "review")
        self.assertEqual(runs.report_of("stp")["null"], [])

    def test_a_driver_waiting_on_an_item_sleeps_rewriting_no_status(self):
        # the review of #1278 (2026-11-25): every pass wrote status.json in the run directory the
        # driver watches, so each wait returned at once: 20 rewrites a second while an item ran
        os.environ["FAKE_RESULT_BY"] = json.dumps({"Audit area": {"findings": [], "covered": ["README.md"]}})
        os.environ["FAKE_SLEEP"] = "600"
        run_wb("run", "start", "calm", "--plan", self.plan({**AUDIT, "name": "calm"}), "--param", "areas=a",
               "--repo", str(self.repo))
        self.wait_item("calm-find-1", lambda st: st.get("phase") == "running" and st.get("agent_pid"))
        time.sleep(1)                                                    # the start's own writes
        path, seen = runs.run_dir("calm") / "status.json", set()
        deadline = time.time() + 3
        while time.time() < deadline:
            seen.add(store.read_json(path, {}).get("updated_at"))
            time.sleep(0.02)
        self.assertLessEqual(len(seen), 2, f"{len(seen)} status writes in 3 s")
        self.assertTrue(runs.driver_alive(runs.Run("calm")))
        run_wb("run", "stop", "calm")

    def test_attempts_run_an_item_n_times_and_a_batch_hands_a_barrier_its_inputs_as_one_item(self):
        os.environ["FAKE_RESULT_BY"] = json.dumps({
            "Audit area alpha": {"findings": [finding("A1", "claim-one")], "covered": ["README.md"]}})
        flow = {"v": 1, "name": "ab", "params": {}, "stages": [
            {"id": "find", "card": "find", "items": ["alpha"], "attempts": 2, "prompt": "Audit area {item} now."},
            {"id": "judge", "card": "judge", "from": "find", "flow": "barrier", "batch": 2,
             "prompt": "Judge these:\n{item}"},
            {"id": "claude", "final": True, "from": "judge"}]}
        run_wb("run", "start", "ab", "--plan", self.plan(flow), "--repo", str(self.repo))
        self.assertEqual(self.wait_run("ab", ("review", "failed", "checkpoint", "paused"))["phase"], "review")
        st = runs.state(runs.Run("ab"))
        self.assertEqual([(i.key, i.state) for i in st["find"]], [("1#a1", "done"), ("1#a2", "done")])
        (judged,) = st["judge"]
        self.assertEqual(judged.key, "b1")
        self.assertEqual([o["findings"][0]["id"] for o in judged.input], ["A1", "A1"])
        self.assertEqual(sorted(runs.run_tasks(runs.Run("ab"))), ["ab-find-1", "ab-find-2", "ab-judge-1"])
        self.assertIn("A1", store.Task("ab-judge-1").round_dir(1).joinpath("prompt.md").read_text())
        self.assertEqual(len(runs.report_of("ab")["final"]), 1)

    def test_a_batchs_judge_answers_by_its_members_ids_its_trail_lists_them_and_an_unjudged_one_is_named(self):
        # lenses and triage handed Claude judgments with no trail and without the findings they
        # judged, ids the judge had to invent, and a skipped finding vanished (#1134's study, #1278)
        j = lambda fid: {"id": fid, "meets_bar": True, "class": "false result", "why": "w"}
        os.environ["FAKE_RESULT_BY"] = json.dumps({
            "Audit area alpha": {"findings": [finding("A1", "claim-one"), finding("A2", "claim-two"),
                                              finding("A3", "claim-three")], "covered": ["README.md"]},
            "claim-one": {"judgments": [j("1.1"), j("1.1")]},  # a duplicate in 1.2's place
            "claim-three": {"judgments": [j("1.3")]}})
        flow = {"v": 1, "name": "bj", "params": {}, "stages": [
            {"id": "find", "card": "find", "items": ["alpha"], "prompt": "Audit area {item} now."},
            {"id": "judge", "card": "judge", "from": "find", "split": "findings", "flow": "barrier",
             "batch": 2, "prompt": "Judge these:\n{item}"},
            {"id": "claude", "final": True, "from": "judge"}]}
        run_wb("run", "start", "bj", "--plan", self.plan(flow), "--repo", str(self.repo))
        self.assertEqual(self.wait_run("bj", ("review", "failed", "checkpoint", "paused"))["phase"], "review")
        prompt = store.Task("bj-judge-1").round_dir(1).joinpath("prompt.md").read_text()
        self.assertIn('"id": "1.1"', prompt)
        self.assertIn('"id": "1.2"', prompt)
        self.assertNotIn('"A1"', prompt)  # the member's key, never the finder's own
        self.assertEqual(store.Task("bj-judge-1").spec["schema"], str(runs.run_dir("bj") / "schemas" / "judge-1.json"))
        sch = json.loads((runs.run_dir("bj") / "schemas" / "judge-1.json").read_text())
        self.assertEqual(sch["properties"]["judgments"]["items"]["properties"]["id"]["enum"], ["1.1", "1.2"])
        rep = runs.report_of("bj")
        b1 = next(f for f in rep["final"] if f["key"] == "judge:b1")
        self.assertEqual([(t["stage"], t["key"], t["result"]["claim"]) for t in b1["trail"]],
                         [("find", "1.1", "claim-one"), ("find", "1.2", "claim-two")])
        self.assertEqual(rep["not_run"], [{"key": "judge:1.2", "why": "unjudged: judge:b1 returned nothing for it"}])
        self.assertIn("judge:1.2: unjudged", run_wb("run", "report", "bj"))

    def test_best_of_has_a_chooser_read_every_attempts_diff_and_claude_gets_its_pick_with_the_attempts_kept(self):
        # best-of ran 3 builders and a reviewer per diff, and Claude read every review and most
        # diffs: N times the handoff (#1134's study); the chooser sees them side by side
        os.environ["FAKE_RESULT_BY"] = json.dumps({"Pick the best attempt": {
            "best": "a2", "ranking": ["a2", "a1", "a3"], "why": "a2 is the smallest"}})
        run_wb("run", "start", "bo", "--flow", "best-of", "--param", "request=Write the answer to answer.txt.",
               "--repo", str(self.repo))
        self.assertEqual(self.wait_run("bo", ("review", "failed", "checkpoint", "paused"))["phase"], "review")
        (pick,) = [n for n in runs.run_tasks(runs.Run("bo")) if n.startswith("bo-choose-")]
        prompt = store.Task(pick).round_dir(1).joinpath("prompt.md").read_text()
        for a, n in (("a1", 1), ("a2", 2), ("a3", 3)):
            self.assertIn(f"### {a} (task bo-code-{n}; no tests)\n```diff\n", prompt)
        self.assertIn("+42", prompt)
        sch = json.loads((runs.run_dir("bo") / "schemas" / f"choose-1.json").read_text())
        self.assertEqual(sch["properties"]["best"]["enum"], ["a1", "a2", "a3"])
        rep = runs.report_of("bo")
        (final,) = rep["final"]
        self.assertEqual((final["key"], final["result"]["best"]), ("choose:1", "a2"))
        self.assertEqual([(t["key"], t["result"]["task"]) for t in final["trail"]],
                         [("1#a1", "bo-code-1"), ("1#a2", "bo-code-2"), ("1#a3", "bo-code-3")])
        # nothing discarded on the chooser's word: every attempt waits at review for Claude
        self.assertEqual([(c["task"], c["phase"]) for c in rep["code"]],
                         [("bo-code-1", "review"), ("bo-code-2", "review"), ("bo-code-3", "review")])
        for n in (1, 2, 3):
            run_wb("discard", f"bo-code-{n}")

    def test_an_item_stuck_on_its_role_is_tried_again_from_the_base_on_the_next_escalate_role_before_claude_is_asked(self):
        # #1134's study: escalating attempts across roles; Claude checkpointed only when the last fails
        flow = {"v": 1, "name": "esc", "params": {}, "stages": [
            {"id": "code", "kind": "code", "role": "trivial", "escalate": ["significant"], "items": ["the answer"],
             "prompt": "Write the answer to answer.txt."},
            {"id": "claude", "final": True, "from": "code"}]}
        os.environ["FAKE_SCENARIO_BY_TASK"] = json.dumps({"esc-code-1": "crash"})
        out = run_wb("run", "start", "esc", "--plan", self.plan(flow), "--repo", str(self.repo))
        self.assertIn("code trivial (GPU), then significant", out)
        self.assertEqual(self.wait_run("esc", ("review", "failed", "checkpoint", "paused"))["phase"], "review")
        run = runs.Run("esc")
        self.assertEqual(store.Task("esc-code-1").status["phase"] in ("failed", "stuck", "discarded"), True)
        self.assertEqual((store.Task("esc-code-2").spec.get("role"), store.Task("esc-code-2").status["phase"]),
                         ("significant", "review"))
        self.assertEqual([(r["key"], r["after"], r["role"]) for r in run.journal() if r.get("kind") == "escalated"],
                         [("1#e1", "1", "significant")])
        self.assertFalse([r for r in run.journal() if r.get("notice") == "run_checkpoint"])  # Claude not asked
        rep = runs.report_of("esc")
        self.assertEqual([(c["key"], c["task"]) for c in rep["code"] if c.get("phase") == "review"], [("code:1#e1", "esc-code-2")])
        run_wb("discard", "esc-code-2")

    def test_asks_left_past_refix_max_escalate_with_the_attempt_kept_and_the_last_roles_failure_asks_claude(self):
        asks = {"verdict": "changes", "must_fix": ["handle the empty file"], "nits": [], "summary": "close"}
        os.environ["FAKE_RESULT_BY"] = json.dumps({"Review this fix": asks})
        os.environ["FAKE_SCENARIO_BY_TASK"] = json.dumps({"fxe-code-2": "crash"})  # the significant role's
        run_wb("run", "start", "fxe", "--flow", "fix-escalate", "--param", "findings=answer.txt is missing",
               "--repo", str(self.repo))
        st = self.wait_run("fxe", ("review", "failed", "checkpoint", "paused"))
        run = runs.Run("fxe")
        self.assertEqual(st["phase"], "checkpoint", st)
        self.assertIn("fxe-code-2 (code 1#e1) is ", st["why"])
        (esc,) = [r for r in run.journal() if r.get("kind") == "escalate"]
        self.assertEqual((esc["key"], esc["task"], esc["why"]), ("1", "fxe-code-1", "1 ask(s) left after 2 refix(es)"))
        self.assertEqual([(r["key"], r["role"]) for r in run.journal() if r.get("kind") == "escalated"],
                         [("1#e1", "significant")])
        self.assertEqual((store.Task("fxe-code-1").spec.get("role"), store.Task("fxe-code-1").status["phase"]),
                         ("trivial", "review"))                         # kept for Claude, never discarded
        rep = runs.report_of("fxe")
        self.assertIn("escalated: 1 ask(s) of fxe-code-1 left after 2 refix(es)", [x["why"] for x in rep["superseded"]])
        run_wb("run", "stop", "fxe")
        for n in ("fxe-code-1", "fxe-code-2"):
            if store.Task(n).status.get("phase") not in store.CLOSED:
                run_wb("discard", n)

    def test_an_items_role_follows_its_escalation_and_escalate_takes_distinct_roles(self):
        st = [{"id": "code", "kind": "code", "items": ["x"], "escalate": ["significant", "writing"]},
              {"id": "review", "card": "review", "from": "code", "refix": {"path": "must_fix", "max": 1},
               "prompt": "{diff}"},
              {"id": "claude", "final": True, "from": "review"}]
        self.assertEqual(runs.validate({"v": 1, "name": "x", "stages": st}), [])
        it = runs.Item("code", "1#e2", None)
        self.assertEqual((runs.item_role(st[0], it), runs.item_role(st[0], runs.Item("code", "1#e1", None)),
                          runs.item_role(st[0], runs.Item("code", "1", None))), ("writing", "significant", "significant"))
        self.assertEqual((runs.escalation("1#a2#e2"), runs.escalation_key("1#a2#e1", 2), runs.escalation("1")),
                         (2, "1#a2#e2", 0))
        st[0]["escalate"] = ["significant", "significant"]
        self.assertIn("escalate is a list of 1 to 3 distinct roles", self.errs(stages=st) if hasattr(self, "errs") else
                      "; ".join(runs.validate({"v": 1, "name": "x", "stages": st})))

    def test_a_skipped_stage_and_items_past_max_items_are_not_run_and_named(self):
        flow = {"v": 1, "name": "cap", "params": {"areas": "list"}, "stages": [
            {"id": "find", "card": "find", "items": "{params.areas}", "max_items": 2},
            {"id": "judge", "card": "judge", "items": ["x"]},
            {"id": "claude", "final": True, "from": "find"}]}
        with mock.patch.object(runs, "spawn", return_value=0):  # no driver: the state alone
            run_wb("run", "start", "cap", "--plan", self.plan(flow), "--param", "areas=a,b,c",
                   "--repo", str(self.repo))
        run = runs.Run("cap")
        self.assertEqual([(i.key, i.state, i.why) for i in runs.state(run)["find"]],
                         [("1", "pending", ""), ("2", "pending", ""),
                          ("3", "not_run", "past the stage's max_items (2)")])
        self.assertIn("amended cap: skip judge", run_wb("run", "amend", "cap", "--skip", "judge"))
        self.assertEqual([(i.state, i.why) for i in runs.state(run)["judge"]],
                         [("not_run", "skipped (wb run amend --skip)")])
        code, _, err = wb("run", "amend", "cap", "--skip", "claude")
        self.assertEqual(code, 1)
        self.assertIn("no stage claude to skip (its stages: find, judge)", err)
        self.assertEqual(runs.report_of("cap")["not_run"],
                         [{"key": "find:3", "why": "past the stage's max_items (2)"},
                          {"key": "judge:1", "why": "skipped (wb run amend --skip)"}])

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


class RunsApi(Base):
    """A run on an API role (#1278), on the fake's catalog (WB_FAKE_CATALOG, tests.test_workbench's
    SPEND_CATALOG): the day cap, the run's dollar budget, exit 3's two meanings and the provider
    slots. The driver's pass (runs.tick) runs here; each item's wb start is the real command."""

    def setUp(self):
        super().setUp()
        from tests.test_workbench import SPEND_CATALOG
        path = Path(self.tmp.name) / "catalog.json"
        path.write_text(json.dumps(SPEND_CATALOG))
        os.environ["WB_FAKE_CATALOG"] = str(path)
        os.environ["FAKE_AUTH_EXPORT"] = json.dumps([
            {"id": "cred_meterco", "integrationID": "meterco", "label": "main", "active": True,
             "value": {"type": "key", "key": "sk-run-test"}}])
        self.roles(SPEND_FLASH, 5)
        self.assertEqual(models.lookup(SPEND_FLASH).kind, "api")  # hosted.json, which stage_model reads

    def roles(self, model, cap):
        """routes.json: the significant role (every AUDIT stage's card's) on ``model``, the cap."""
        store.write_json(Path(os.environ["WB_ROUTES"]), {"v": 1, **({"day_usd": cap} if cap else {}),
                                                         "roles": {"significant": {"via": "api", "api": model}}})
        meter._CAP.clear()

    def spent(self, usd, task="other"):
        store.append_jsonl(meter.ledger_path(), {
            "ts": time.time(), "day": meter.day(), "kind": "step", "task": task, "round": 1,
            "provider": "meterco", "model": SPEND_FLASH, "usd": usd, "metered": usd, "source": "opencode",
            "tok": {"in": 1, "out": 0, "cache_read": 0, "cache_write": 0, "reasoning": 0}})

    def start(self, name, *extra, flow=AUDIT):
        p = Path(self.tmp.name) / f"{name}.json"
        p.write_text(json.dumps({**flow, "name": name}))
        with mock.patch.object(runs, "spawn", return_value=0):  # the passes run here
            code, out, err = wb("run", "start", name, "--plan", str(p), "--param", "areas=a", "--repo",
                                str(self.repo), *extra)
        self.assertEqual(code, 0, err)
        return runs.Run(name), out

    def notices(self, name):
        return [r for r in store.notices(runs=True) if r.get("run") == name and "task" not in r]

    def test_an_api_stage_with_no_day_cap_is_refused_before_any_file_is_written(self):
        self.roles(SPEND_FLASH, None)
        code, _, err = wb("run", "start", "nocap", "--flow", "audit", "--param", "areas=a", "--param", "focus=x",
                          "--repo", str(self.repo))
        self.assertEqual(code, 1)
        self.assertIn(f"stage find runs on {SPEND_FLASH} (role significant), and {meter.NO_CAP}", err)
        self.assertFalse(runs.run_dir("nocap").exists())

    def test_the_runs_dollar_budget_counts_its_tasks_and_the_next_requests_worst_then_pauses(self):
        m = models.lookup(SPEND_FLASH)
        floor = meter.request_floor(m)
        self.assertGreater(floor, 0)
        run, out = self.start("bud", "--budget-usd", "1")
        self.assertIn("budget $1.00", out)
        run.log(kind="item", stage="find", key="0", n=9, task="bud-find-9")  # an earlier item's spend
        self.spent(0.5, "bud-find-9")
        self.spent(3.0)                                                  # another task's: not the run's
        stage = runs.stages(run.flow)[0]
        run.amend(budget_usd=0.5 + floor * 2)
        self.assertIsNone(runs._budget_stop(run, stage, m))
        run.amend(budget_usd=round(0.5 + floor / 2, 6))
        why = runs._budget_stop(run, stage, m)
        self.assertTrue(why.startswith("the run's budget: $0.50 spent of $"), why)
        self.assertIn(f"the next request can cost up to {meter.usd(floor)}", why)
        self.assertIsNone(runs._budget_stop(run, stage, models.lookup(SPEND_FREE)))  # a free model: $0
        runs.tick(run)
        self.assertEqual(run.status["phase"], "paused")
        self.assertEqual([r.get("task") for r in run.journal() if r.get("kind") == "item"], ["bud-find-9"])
        (told,) = self.notices("bud")
        self.assertTrue(told["text"].startswith("[workbench] Run `bud` is paused: paused at the run's budget"))

    def test_the_day_cap_pauses_the_run_and_an_overnight_run_tries_again_with_no_wake(self):
        self.spent(5.03)                                                 # today at the $5 cap
        run, out = self.start("capr", "--budget-usd", "1")
        self.assertIn("today's cap leaves $0", out)
        runs.tick(run)
        st = run.status
        self.assertEqual(st["phase"], "paused")
        self.assertIn(meter.RESETS, st["why"])
        (told,) = self.notices("capr")
        self.assertIn("is paused: paused: significant role: API spend today is", told["text"])
        self.assertEqual([r["kind"] for r in run.journal() if r.get("stage") == "find"], ["item", "retry"])
        self.assertFalse(store.Task("capr-find-1").exists)
        night, _ = self.start("night", "--budget-usd", "1", "--overnight")
        for _ in range(2):
            runs.tick(night)
        self.assertEqual(night.status["phase"], "running")
        self.assertEqual(self.notices("night"), [])
        self.assertEqual([r["kind"] for r in night.journal() if r.get("stage") == "find"],
                         ["item", "retry", "item", "retry"])
        (it,) = runs.state(night)["find"]
        self.assertEqual(it.state, "pending")                            # tried again past midnight

    def test_any_other_exit_3_is_not_run_and_an_overnight_run_never_tries_it_again(self):
        # the review of #1278 (2026-11-25): "day cap" matched NO_PRICE, NO_CAP and UNREADABLE too,
        # and an --overnight run retried a refusal that never clears every tick, silently
        self.roles(SPEND_NONE, 5)
        self.assertEqual(models.lookup(SPEND_NONE).kind, "api")
        run, out = self.start("nopr", "--budget-usd", "1", "--overnight")
        self.assertIn("estimate unpriced", out)
        runs.tick(run)
        (it,) = runs.state(run)["find"]
        self.assertEqual(it.state, "not_run")
        self.assertIn(meter.NO_PRICE, it.why)
        self.assertNotIn("retry", [r["kind"] for r in run.journal()])
        self.assertEqual(run.status["phase"], "review")                 # nothing else to run: Claude's
        self.assertEqual([r["kind"] for r in self.notices("nopr")], ["run_done"])
        self.assertEqual(runs.report_of("nopr")["not_run"][0]["key"], "find:1")

    def test_the_providers_slots_and_a_users_task_waiting_for_one_come_before_a_runs_next_item(self):
        run, _ = self.start("slot", "--budget-usd", "1")
        m = models.lookup(SPEND_FLASH)
        stage = runs.stages(run.flow)[0]
        st = runs.state(run)
        user = store.Task("users-own")
        user.dir.mkdir(parents=True)
        store.write_json(user.dir / "task.json", {"name": "users-own"})
        mate = store.Task("slot-find-7")
        mate.dir.mkdir(parents=True)
        store.write_json(mate.dir / "task.json", {"name": "slot-find-7", "run": "slot"})
        with mock.patch.object(cli, "_api_slots_free", return_value=0):
            self.assertEqual(runs._room(run, stage, st, m), "meterco's slots are taken")
        with mock.patch.object(cli, "_api_slots_free", return_value=2), \
                mock.patch.object(cli, "_api_pending", return_value=["users-own"]):
            self.assertEqual(runs._room(run, stage, st, m), "a task of the user's waits for a slot first")
            with mock.patch.object(runs, "_start") as started:
                runs.tick(run)
            started.assert_not_called()                                  # the user's goes first
        with mock.patch.object(cli, "_api_slots_free", return_value=2), \
                mock.patch.object(cli, "_api_pending", return_value=["slot-find-7"]):
            self.assertIsNone(runs._room(run, stage, st, m))             # another of the run's: no wait
        flying = {"find": [runs.Item("find", str(i), state="running") for i in range(runs.IN_FLIGHT)]}
        self.assertEqual(runs._room(run, stage, flying, m), f"{runs.IN_FLIGHT} items in flight")


    def wait_item(self, name, check, timeout=60):
        deadline = time.time() + timeout
        while time.time() < deadline:
            t = store.Task(name)
            if t.exists and check(t.status):
                return t
            time.sleep(0.2)
        self.fail(f"{name}: {store.Task(name).status if store.Task(name).exists else 'never started'}")

    def until_done(self, run, timeout=60):
        """The passes, as the driver runs them, until the run leaves `running`."""
        deadline = time.time() + timeout
        while runs.tick(run) and run.status.get("phase") == "running" and time.time() < deadline:
            time.sleep(0.3)
        return run.status

    def test_an_items_ask_in_a_run_is_refused_at_once_never_left_holding_the_gpu_for_its_timeout(self):
        # the real GPU audit of #1278 (2026-11-26): a finder asked for a path outside its lane,
        # nobody is told of an item's ask in a run, and it held the GPU its 20-minute ask_timeout
        run, _ = self.start("ask")
        os.environ["FAKE_SLEEP"] = "600"
        runs.tick(run)
        task = self.wait_item("ask-find-1", lambda st: st.get("phase") == "running" and st.get("agent_pid"))
        qid = "abcd1234"
        store.write_json(task.dir / "ask" / f"{qid}.json", {"id": qid, "question": "May I read it?", "path": "/etc"})
        task.set_status(phase="asking", asking=qid)
        runs.tick(run)
        self.assertEqual((task.dir / "ask" / f"{qid}.answer").read_text().strip(), runs.RUN_REFUSED)
        os.environ["FAKE_SLEEP"] = "0"
        run_wb("stop", "ask-find-1")

    def stopped_mid_round(self, run, name, **status):
        """Item ``name`` started and at work, then stopped as ``status`` says, its runner gone."""
        os.environ["FAKE_SLEEP"] = "600"
        runs.tick(run)
        self.wait_item(name, lambda st: st.get("phase") == "running" and st.get("agent_pid"))
        self.assertIn(f"stopped {name}", run_wb("stop", name))
        os.environ["FAKE_SLEEP"] = "0"
        task = store.Task(name)
        task.set_status(**status)
        return task

    def test_an_item_its_provider_stopped_is_resumed_where_it_stood_and_its_result_kept(self):
        # the review of #1278 (2026-11-25): only cli._interrupted stops were held; a provider's
        # stop (here the fake's 401, WB_FAKE_API) was taken as null, its task discarded
        os.environ.update(FAKE_SCENARIO="ok", WB_FAKE_API="meterco=auth", FAKE_RESULT_BY=json.dumps(
            {"Audit area": {"findings": [], "covered": ["README.md"]}}))
        run, _ = self.start("prov", "--budget-usd", "1")
        runs.tick(run)
        task = self.wait_item("prov-find-1", lambda st: st.get("phase") in ("stuck", "failed", "review"))
        self.assertEqual(cli.stopped_outside(task.status), "provider", task.status)
        os.environ.pop("WB_FAKE_API")                                    # the key works again
        runs.tick(run)
        self.assertEqual([(r["task"], r["by"], r["ok"]) for r in run.journal() if r.get("kind") == "resumed"],
                         [("prov-find-1", "driver", True)])
        self.wait_item("prov-find-1", lambda st: st.get("phase") in ("review", "stuck", "failed"))
        self.assertEqual(self.until_done(run)["phase"], "review", run.journal()[-4:])
        rep = runs.report_of("prov")
        self.assertEqual((rep["null"], [f["key"] for f in rep["final"]]), ([], []), rep)  # no findings: none to verify
        (found,) = runs.state(run)["find"]
        self.assertEqual((found.state, found.output), ("done", {"findings": [], "covered": ["README.md"]}))

    def test_an_item_the_day_cap_stopped_mid_round_waits_past_midnight_overnight_never_discarded(self):
        # the review of #1278 (2026-11-25): the cap's stop in a round (infra spend) was null, so
        # --overnight's promise to go on past midnight failed for whatever was in flight
        os.environ.update(FAKE_SCENARIO="ok", FAKE_RESULT_BY=json.dumps(
            {"Audit area": {"findings": [], "covered": ["README.md"]}}))
        run, _ = self.start("ovn", "--budget-usd", "1", "--overnight")
        why = f"significant role: API spend today is $5.03 of the $5.00 day cap ({meter.RESETS})"
        task = self.stopped_mid_round(run, "ovn-find-1", phase="stuck", last=why, infra=runner.SPEND,
                                      guard=runner.SPEND)
        self.spent(5.03)                                                 # today at the cap
        for _ in range(2):
            runs.tick(run)
        self.assertEqual(run.status["phase"], "running")
        self.assertEqual(self.notices("ovn"), [])                        # no wake: tried each pass
        self.assertEqual([r["kind"] for r in run.journal() if r.get("task") == task.name], ["item", "held"])
        self.assertEqual((task.status["phase"], runs.state(run)["find"][0].state), ("stuck", "running"))
        self.roles(SPEND_FLASH, 50)                                      # past midnight: room again
        runs.tick(run)
        self.assertEqual([r["by"] for r in run.journal() if r.get("kind") == "resumed"], ["driver"])
        self.wait_item(task.name, lambda st: st.get("phase") in ("review", "stuck", "failed"))
        self.assertEqual(self.until_done(run)["phase"], "review", run.journal()[-4:])
        self.assertEqual(runs.report_of("ovn")["null"], [])

    def test_without_overnight_the_cap_pauses_the_run_and_wb_run_resume_takes_the_item_up(self):
        os.environ.update(FAKE_SCENARIO="ok", FAKE_RESULT_BY=json.dumps(
            {"Audit area": {"findings": [], "covered": ["README.md"]}}))
        run, _ = self.start("cpr", "--budget-usd", "1")
        why = f"significant role: API spend today is $5.03 of the $5.00 day cap ({meter.RESETS})"
        task = self.stopped_mid_round(run, "cpr-find-1", phase="stuck", last=why, infra=runner.SPEND)
        self.spent(5.03)
        runs.tick(run)
        self.assertEqual(run.status["phase"], "paused")
        (told,) = self.notices("cpr")
        self.assertIn("cpr-find-1 waits where it stopped: wb run resume cpr once that allows", told["text"])
        self.assertEqual(task.status["phase"], "stuck")                  # kept, never discarded
        self.roles(SPEND_FLASH, 50)
        with mock.patch.object(runs, "spawn", return_value=0):
            self.assertEqual(wb("run", "resume", "cpr")[0], 0)
        self.assertEqual(runs._resume_items(run), ["cpr-find-1"])        # as the new driver starts
        self.wait_item(task.name, lambda st: st.get("phase") in ("review", "stuck", "failed"))
        self.assertEqual(self.until_done(run)["phase"], "review", run.journal()[-4:])

    def test_a_second_stop_from_outside_after_the_drivers_resume_is_claudes_checkpoint_not_a_null(self):
        run, _ = self.start("twice", "--budget-usd", "1")
        task = self.stopped_mid_round(run, "twice-find-1", phase="stuck", last="rate_limited: 429",
                                      infra="provider", guard="rate_limited")
        run.log(kind="resumed", task=task.name, by="driver", ok=True, why="")  # the first stop's
        runs.tick(run)
        st = run.status
        self.assertEqual(st["phase"], "checkpoint")
        self.assertIn("twice-find-1 was stopped from outside again (provider", st["why"])
        self.assertEqual(task.status["phase"], "stuck")                  # its work kept
        self.assertNotIn("null", [r["kind"] for r in run.journal()])

    def test_an_item_whose_model_or_runner_failed_is_its_null_never_resumed_nor_a_checkpoint(self):
        # the third review of #1278 (2026-11-25): every cli.stopped_outside was held, so a crash or
        # a failed load was resumed once, then each item woke Claude; #1278 has a failed item null
        for n, (infra, last) in enumerate((("crash", "KeyError: 'round'"), ("model", "model: no load"))):
            run, _ = self.start(f"ends{n}", "--budget-usd", "1")
            task = self.stopped_mid_round(run, f"ends{n}-find-1", phase="failed", last=last, infra=infra)
            runs.tick(run)
            self.assertEqual([r["kind"] for r in run.journal() if r.get("task") == task.name], ["item", "null"])
            self.assertEqual(task.status["phase"], "discarded")
            self.assertNotEqual(run.status["phase"], "checkpoint")
            self.assertFalse(runs._resumable(run, task.name, task.status))

    def test_wb_run_resume_takes_up_wb_run_stops_items_never_claudes_own_wb_stop(self):
        # the review of #1278 (2026-11-25): both leave `last` "stopped by the reviewer"
        run, _ = self.start("own", "--budget-usd", "1")
        task = self.stopped_mid_round(run, "own-find-1")
        self.assertEqual(task.status["last"], runs.STOPPED_BY_CLAUDE)
        self.assertFalse(runs._resumable(run, task.name, task.status))
        run.log(kind="stopped", task=task.name)                           # as runs.stop journals it
        self.assertTrue(runs._resumable(run, task.name, task.status))
        run.log(kind="resumed", task=task.name, by="resume", ok=True, why="")
        self.assertFalse(runs._resumable(run, task.name, task.status))   # Claude's next stop is Claude's

    def test_a_start_that_timed_out_after_writing_its_task_is_the_items_never_a_null(self):
        run, _ = self.start("slow", "--budget-usd", "1")
        stage, it = runs.stages(run.flow)[0], runs.state(run)["find"][0]
        late = subprocess.CompletedProcess([], 124, "", "wb start timed out")
        with mock.patch.object(runs, "_wb", return_value=late):
            self.assertEqual(runs._start(run, stage, it, 1), ("null", "wb start timed out"))
            t = store.Task("slow-find-1")
            t.dir.mkdir(parents=True)
            store.write_json(t.dir / "task.json", {"name": t.name, "run": "slow"})
            self.assertEqual(runs._start(run, stage, it, 1), ("running", ""))

    def test_a_context_file_that_cant_be_read_is_said_never_a_traceback(self):
        p = Path(self.tmp.name) / "ctx.json"
        p.write_text(json.dumps({**AUDIT, "name": "ctx"}))
        code, _, err = wb("run", "start", "ctx", "--plan", str(p), "--param", "areas=a", "--repo",
                          str(self.repo), "--context-file", str(Path(self.tmp.name) / "missing.md"))
        self.assertEqual(code, 1)
        self.assertIn("wb: --context-file", err)
        self.assertNotIn("Traceback", err)

    def test_a_context_past_its_cut_is_said_at_start_and_in_each_spec_it_goes_in(self):
        # #1519 (2026-11-26): run.json kept it cut, so the spec's cut note never showed and
        # nothing said the agents missed all past 4,000 characters
        ctx = Path(self.tmp.name) / "ctx.md"
        ctx.write_text("x" * 5_990 + "TAILMARKER")
        flow = {**AUDIT, "stages": [{**AUDIT["stages"][0], "prompt": "Audit area {item} now.\n{context}"},
                                    *AUDIT["stages"][1:]]}
        run, out = self.start("long", "--context-file", str(ctx), flow=flow)
        self.assertIn("context: 6,000 characters, the first 4,000 go in each spec's {context}", out)
        self.assertEqual(len(run.spec["context"]), 6_000)
        spec = runs._spec_of(run, runs.stages(run.flow)[0], runs.state(run)["find"][0])
        self.assertIn("\n[… 2,000 characters cut]", spec)
        self.assertNotIn("TAILMARKER", spec)


class Driver(Base):
    """How the driver runs wb for its items (runs._wb)."""

    def test_an_items_wb_runs_workbenchs_own_package_never_a_workbench_in_the_users_repo(self):
        # the review of #1278 (2026-11-25): `python -m workbench.cli` with the run's cwd put the
        # cwd ahead of PYTHONPATH, so a repo's own top-level workbench/cli.py ran for every
        # item's start, accept and discard, as the user, outside the lane
        repo = Path(self.tmp.name) / "shadow"
        (repo / "workbench").mkdir(parents=True)
        (repo / "workbench" / "__init__.py").write_text("")
        ran = Path(self.tmp.name) / "the-repos-cli-ran"
        (repo / "workbench" / "cli.py").write_text(f"open({str(ran)!r}, 'w').write('x')\n")
        d = runs.run_dir("shad")
        d.mkdir(parents=True)
        store.write_json(d / "run.json", {"name": "shad", "cwd": str(repo), "repo": str(repo)})
        with mock.patch.dict(os.environ):
            os.environ.pop("PYTHONPATH", None)                           # as from bin/wb, which sets none
            r = runs._wb(["--version"], runs.Run("shad"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.startswith("wb "), r.stdout)
        self.assertFalse(ran.exists())


class Wiring(Base):
    """The run's pieces in the rest of wb (#1278)."""

    def open_run(self, name="wr", alive=True, flow=AUDIT):
        """A run, open; ``alive``: with a driver, a process whose command line is one's (sleeping,
        reaped by a thread of this test's as it ends)."""
        d = runs.run_dir(name)
        d.mkdir(parents=True)
        store.write_json(d / "run.json", {"name": name, "flow": flow, "cwd": "/x", "session": "S1"})
        run = runs.Run(name)
        run.set_status(phase="running")
        if alive:
            proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)",
                                     "-m", "workbench.runs", "drive", name])
            threading.Thread(target=proc.wait, daemon=True).start()
            self.addCleanup(lambda: proc.poll() is None and proc.kill())
            run.set_status(pid=proc.pid, start=models._started(proc.pid), machine=store.machine())
            self.assertTrue(runs.driver_alive(run))
        return run

    def item(self, name, run="wr", status=None, **spec):
        t = store.Task(name)
        t.dir.mkdir(parents=True)
        store.write_json(t.dir / "task.json", {"name": name, **({"run": run, "stage": "find", "item": "1"}
                                                                if run else {}), **spec})
        if status is not None:
            store.write_json(t.dir / "status.json", status)
        return t

    def test_an_open_runs_item_wakes_its_driver_but_its_question_still_wakes_claude(self):
        run = self.open_run()
        t = self.item("wr-find-1")
        t.notify("review", "the report")
        t.notify("question", "which file?", id="q1")
        self.assertEqual([r["kind"] for r in store.notices() if r["task"] == t.name], ["question"])
        (routed,) = store.read_jsonl(runs.run_dir("wr") / "notify.jsonl")[0]
        self.assertEqual(routed["kind"], "review")
        runs.Run("wr").set_status(phase="review")
        t.notify("review", "again")  # the run no longer open: Claude's again
        self.assertEqual([r["kind"] for r in store.notices() if r["task"] == t.name], ["question", "review"])
        self.assertTrue(run.exists)

    def test_a_routed_notice_leaves_its_noticed_mark_and_the_driver_acknowledges_it(self):
        # the review of #1278 (2026-11-25): with no mark the mod's idle net read `acked` against
        # the status written after the notice, and woke Claude a minute into a code item's review
        run = self.open_run()
        t = self.item("wr-code-1")
        t.notify("review", "the change")
        (routed,) = store.read_jsonl(run.dir / "notify.jsonl")[0]
        self.assertEqual(float((t.dir / "noticed").read_text()), routed["ts"])
        self.assertLess(cli._acked(t), routed["ts"])
        runs._ack_items(run)
        self.assertGreaterEqual(cli._acked(t), float((t.dir / "noticed").read_text()))
        self.assertEqual(store.notices(), [])

    def test_a_dead_drivers_items_wake_claude_until_a_driver_lives(self):
        # the review of #1278: a driver killed left the run open, and every item notice, a code
        # item's stuck or failed too, went to a file nobody read until a wb run status reaped it
        run = self.open_run(alive=False)
        run.set_status(pid=2 ** 22 + 9)
        t = self.item("wr-code-1")
        t.notify("stuck", "the agent stopped")
        self.assertEqual([r["kind"] for r in store.notices() if r["task"] == t.name], ["stuck"])
        self.assertFalse((run.dir / "notify.jsonl").exists())

    def reboot(self, alive):
        """A restart under a run's item and a task of the user's, both of session S1 in /x: the
        runners recorded under another boot; the run's driver dead (or, ``alive``, still up)."""
        run = self.open_run("aa", alive=alive)
        if not alive:
            st = run.set_status(pid=2 ** 22 + 7)
            st["updated_at"] = time.time() - 60
            store.write_json(run.dir / "status.json", st)
        old = {"phase": "running", "boot": "old-boot", "pid": 2 ** 22 + 3, "updated_at": time.time() - 60}
        item = self.item("aa-find-1", run="aa", status=old, session="S1", cwd="/x", repo="/x")
        mine = self.item("mine", run=None, status=old, session="S1", cwd="/x", repo="/x")
        with mock.patch.object(store, "boot_id", return_value="this-boot"):
            for t in (item, mine):  # as wb status reaps them, the run's first
                self.assertTrue(cli.reap(t))
        return run, mine

    def test_a_reboot_tells_claude_of_the_users_tasks_apart_from_a_runs_items(self):
        # the review of #1278 (2026-11-25): one batch held both (a run's driver inherits the
        # session and the directory); led by a run item, its notice went to the run's file and
        # `mine` was acknowledged with it, Claude never told
        run, mine = self.reboot(alive=False)
        rows = {r["task"]: r for r in store.notices()}
        self.assertEqual(sorted(rows), ["aa-find-1", "mine"])
        self.assertEqual(rows["mine"]["tasks"], ["mine"])
        self.assertIn("wb resume mine", rows["mine"]["text"])
        self.assertLess(cli._acked(mine), rows["mine"]["ts"])  # news still: the mod wakes Claude for it
        self.assertEqual(rows["aa-find-1"]["tasks"], ["aa-find-1"])
        self.assertIn("wb run resume aa continues its run, them first", rows["aa-find-1"]["text"])
        # the run is stopped, said by its items' notice alone
        self.assertEqual(run.status["phase"], "stopped")
        self.assertEqual([r for r in store.notices(runs=True) if "task" not in r], [])
        self.assertEqual(store.Task("aa-find-1").status["last"], cli.RESTARTED)

    def test_a_reboot_under_a_live_driver_gives_it_the_items_and_claude_the_users_tasks(self):
        run, mine = self.reboot(alive=True)
        self.assertEqual([r["task"] for r in store.notices()], ["mine"])
        self.assertEqual([r["task"] for r in store.read_jsonl(run.dir / "notify.jsonl")[0]], ["aa-find-1"])
        self.assertLess(cli._acked(mine), store.notices()[0]["ts"])
        self.assertEqual(run.status["phase"], "running")

    def test_update_stop_stops_a_runs_driver_and_tells_claude_of_its_items_apart(self):
        # the same root as a reboot's (the review of #1278): one batch per owner held a run's items
        # with the user's tasks, and a live driver would have taken the stopped items as null
        run = self.open_run("up")
        item = self.item("up-find-1", run="up", status={"phase": "running"}, session="S1", cwd="/x", repo="/x")
        mine = self.item("mine", run=None, status={"phase": "running"}, session="S1", cwd="/x", repo="/x")
        self.assertIn("stopped for the update: up-find-1", cli._stop_for_update([item, mine]))
        self.assertEqual((run.status["phase"], run.status["why"]), ("stopped", cli.UPDATE_STOP))
        self.assertFalse(runs.driver_alive(run))
        rows = {r["task"]: r for r in store.notices()}
        self.assertEqual(sorted(rows), ["mine", "up-find-1"])
        self.assertIn("wb resume mine continues it in a fresh session", rows["mine"]["text"])
        self.assertIn("wb run resume up continues its run, it first", rows["up-find-1"]["text"])
        self.assertLess(cli._acked(mine), rows["mine"]["ts"])

    def test_run_notices_are_kept_apart_from_task_notices_and_rotated_once_taken(self):
        run = self.open_run()
        row = run.notify("run_checkpoint", "stage merge is yours")
        self.assertEqual(store.notices(), [])
        self.assertEqual([r["run"] for r in store.notices(runs=True)], ["wr"])
        self.assertEqual(cli._rotate_notices(0), 0)  # not taken: kept
        run.claim_ack(row["ts"])
        self.assertEqual(cli._rotate_notices(-1), 1)

    def test_a_runs_wake_is_charged_to_its_tasks_by_its_journal_never_by_their_prefix(self):
        # the review of #1278 (2026-11-25): by the `<run>-` prefix, a user's own `wr-x` was
        # charged for run wr's wakes
        from workbench import spend
        run = self.open_run(alive=False)
        for n in ("wr-find-1", "wr-find-2"):
            run.log(kind="item", stage="find", key=n[-1], n=int(n[-1]), task=n)
        said = "The workbench plugin sent a message: [workbench] Run `wr` needs Claude: stage merge is yours"
        self.assertEqual(spend._woken(said, {"wr-find-1", "wr-find-2", "wr-x", "other"}), {"wr-find-1", "wr-find-2"})
        self.assertEqual(spend._woken("[workbench] Task `other` is ready for review", {"wr-find-1", "other"}),
                         {"other"})
        self.assertEqual(spend._woken("see [workbench] Run `wr` above", {"wr-find-1"}), set())
        run.set_status(phase="closed", updated_at=0)
        st = run.status
        st["updated_at"] = 0
        store.write_json(run.dir / "status.json", st)
        self.assertEqual(runs.archive(30), 1)  # an old transcript's wake, its run archived since
        self.assertEqual(spend._woken("[workbench] Run `wr` is done: 2 result(s)", {"wr-find-1", "wr-x"}), {"wr-find-1"})

    def test_a_run_notice_says_the_phase_it_is_true_in_and_wb_wait_skips_one_no_longer_true(self):
        # #1278's S9: the hosts deliver a run's notice while the run is in the phase it was raised
        # in, as a task's; wb wait <run> told Claude "paused" of a run since resumed
        run = self.open_run(alive=False)
        run.set_status(phase="paused")
        paused = run.notify("run_paused", "the day cap")
        self.assertEqual((paused["phase"], paused["run"], "task" in paused), ("paused", "wr", False))
        run.set_status(phase="running")                          # wb run resume
        self.assertIsNone(runs.due("wr"))
        self.assertLess(run.acked, paused["ts"])                 # nor taken: the host acknowledges it
        run.set_status(phase="checkpoint")
        cp = run.notify("run_checkpoint", "stage merge is yours")
        self.assertEqual(runs.due("wr")["ts"], cp["ts"])
        self.assertEqual(run.acked, cp["ts"])
        self.assertEqual(set(runs.NOTICE_PHASE), set(runs.KINDS))
        self.assertLessEqual(set(runs.NOTICE_PHASE.values()), set(runs.PHASES))

    def test_wb_ack_run_if_puts_the_mark_back_only_while_it_is_still_the_hosts(self):
        # a host's wake the engine dropped (#786's rollback, for a run's notice: #1278's S9)
        run = self.open_run(alive=False)
        self.assertEqual(wb("ack", "wr", "5", "--run")[0], 0)
        self.assertEqual(wb("ack", "wr", "5", "--run")[0], 3)            # taken already
        self.assertEqual(wb("ack", "wr", "0", "--run", "--if", "4")[0], 3)  # not this host's mark
        self.assertEqual(run.acked, 5.0)
        code, out, _ = wb("ack", "wr", "0", "--run", "--if", "5")
        self.assertEqual((code, out.strip(), run.acked), (0, "released run wr", 0.0))

    def test_the_web_apps_stop_is_said_to_the_runs_session_and_claudes_own_is_not(self):
        # #1278's S12: the web app's Stop (settings.py's run row, WB_ACTOR=web) is the user's: the
        # session waiting on the run hears it as a notice; Claude's own wb run stop is no news
        run = self.open_run(alive=False)
        with mock.patch.dict(os.environ, {"WB_ACTOR": "web"}):
            for k in ("CLAUDECODE", "WB_TASK", "OPENCODE_SESSION_ID"):
                os.environ.pop(k, None)
            code, out, _ = wb("run", "stop", "wr")
        self.assertEqual(code, 0, out)
        self.assertEqual((run.status["phase"], run.status["why"]), ("stopped", runs.WEB_STOP))
        (row,) = store.notices(runs=True)
        self.assertEqual((row["run"], row["kind"], row["phase"]), ("wr", "run_stopped", "stopped"))
        self.assertTrue(row["text"].startswith("[workbench] Run `wr` stopped: stopped from the web app; done work is kept"))
        other = self.open_run("own", alive=False)
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1", "WB_ACTOR": "web"}):
            self.assertEqual(wb("run", "stop", "own")[0], 0)
        self.assertEqual(other.status["why"], "stopped by Claude (wb run stop)")
        self.assertEqual(len(store.notices(runs=True)), 1)

    def test_wb_top_json_carries_each_run_not_closed_with_its_drivers_life(self):
        # plan §5 (#1278, S8): the seam the pane's and the web's run strip read
        live = self.open_run("live")
        live.set_status(stages={"find": {"done": 1, "running": 2}}, spent_usd=0.25, budget_usd=2.0)
        self.open_run("gone", alive=False).set_status(phase="closed")
        (row,) = runs.snapshot()
        self.assertEqual({k: row[k] for k in ("run", "phase", "alive", "stages", "spent_usd", "budget_usd",
                                               "session", "cwd")},
                         {"run": "live", "phase": "running", "alive": True,
                          "stages": {"find": {"done": 1, "running": 2}}, "spent_usd": 0.25, "budget_usd": 2.0,
                          "session": "S1", "cwd": "/x"})
        from workbench import top
        drm = Path(self.tmp.name, "empty")
        drm.mkdir()
        self.assertEqual([r["run"] for r in top.Top(1, drm=drm).tick()["runs"]], ["live"])

    def test_wb_run_feed_never_reopens_a_closed_run(self):
        # the review of #1278: feed skipped resume's check, and on a closed run set the phase
        # back to running and spawned a driver
        flow = {"v": 1, "name": "chk", "params": {"areas": "list"}, "stages": [
            AUDIT["stages"][0], {"id": "merge", "claude": True, "from": "find"},
            {"id": "end", "final": True, "from": "merge"}]}
        run = self.open_run("chk", alive=False, flow=flow)
        run.set_status(phase="closed")
        feed = Path(self.tmp.name) / "feed.json"
        feed.write_text("[]")
        with mock.patch.object(runs, "spawn") as spawned:
            code, _, err = wb("run", "feed", "chk", "merge", "--file", str(feed))
        self.assertEqual((code, err.strip()), (1, "wb: run chk is closed"))
        self.assertEqual(run.status["phase"], "closed")
        self.assertFalse((run.dir / "inbox" / "merge-feed").exists())
        spawned.assert_not_called()

    def test_flow_import_makes_a_commands_arguments_each_item_and_keeps_its_braces(self):
        # plan §3 (#1278): `wb flow import X.md` maps $ARGUMENTS to {item}
        os.environ["WB_FLOWS"] = str(Path(self.tmp.name) / "flows")
        cmd = Path(self.tmp.name) / "Sec-Review.md"
        cmd.write_text("---\ndescription: A security pass over one area\nallowed-tools: Bash\n---\n"
                       "Review $ARGUMENTS for injection, and say each {finding} with its line.\n")
        out = run_wb("flow", "import", str(cmd))
        self.assertIn("imported Sec-Review.md as flow sec-review (", out)
        self.assertIn("Run it: wb run start <run> --flow sec-review --param items=<a,b,…>", out)
        flow = runs.load_flow("sec-review")
        self.assertEqual(flow["description"], "A security pass over one area")
        self.assertEqual(flow["params"], {"items": "list"})
        stage = runs.stages(flow)[0]
        self.assertEqual((stage["kind"], stage["items"]), ("code", "{params.items}"))
        self.assertEqual(runs.render(stage["prompt"], {"item": "auth/"}),
                         "Review auth/ for injection, and say each {finding} with its line.")
        self.assertIn(("sec-review", "yours", "A security pass over one area"), runs.flow_names())
        code, _, err = wb("flow", "import", str(cmd))
        self.assertEqual(code, 1)
        self.assertIn("exists (--force replaces it)", err)
        cmd.write_text("Fix $1 in $2\n")
        code, _, err = wb("flow", "import", str(cmd), "--name", "pos", "--card", "find")
        self.assertIn("$1: an imported command takes only $ARGUMENTS", err)
        cmd.write_text("Audit the parser.\n")  # no $ARGUMENTS: the item comes after the command's text
        run_wb("flow", "import", str(cmd), "--name", "aud2", "--card", "find")
        stage = runs.stages(runs.load_flow("aud2"))[0]
        self.assertEqual(stage["card"], "find")
        self.assertEqual(runs.render(stage["prompt"], {"item": "lexer.py"}), "Audit the parser.\n\nlexer.py")

    def test_wb_stop_all_stops_a_runs_driver_first(self):
        run = self.open_run()
        self.assertIn("wb run wr", run_wb("stop", "--all"))
        self.assertEqual(run.status["phase"], "stopped")
        self.assertFalse(runs.driver_alive(run))

    def test_an_opencode_sessions_wb_stop_all_takes_its_own_run_and_leaves_another_sessions(self):
        # 2.2's merge: cli._caller gained the driver kind and _runs_in_scope crashed unpacking it;
        # a run an OpenCode session started records that session (#1309), as a task does
        mine, other = self.open_run("mine"), self.open_run("other")
        spec = mine.spec | {"session": "ses_a", "session_kind": "opencode", "cwd": os.getcwd()}
        store.write_json(runs.run_dir("mine") / "run.json", spec)
        env = {"OPENCODE_SESSION_ID": "ses_a", "CLAUDECODE": "", "CLAUDE_CODE_SESSION_ID": "", "WB_TASK": ""}
        with mock.patch.dict(os.environ, env):
            run_wb("stop", "--all")
        self.assertEqual(runs.Run("mine").status["phase"], "stopped")
        self.assertEqual(runs.Run("other").status["phase"], "running")

    def test_deepmode_is_workflows_new_name_and_prints_the_deepcode_rule(self):
        self.assertEqual(run_wb("deepmode"), "Dynamic workflows (deepmode): Off (default). Claude runs one task at "
                                             "a time; deepcode asks for a deep workflow once.\n")
        self.assertIn("Dynamic workflows (deepmode): On.", run_wb("deepmode", "on"))
        self.assertIn("On.", run_wb("workflows"))
        self.assertEqual(run_wb("deepmode", "--rule"), runs.DEEPCODE_RULE + "\n")

    def test_gc_archives_a_closed_run_and_keeps_its_name_taken(self):
        run = self.open_run("old", alive=False)
        run.set_status(phase="closed")
        st = run.status
        st["updated_at"] = time.time() - 40 * 86400
        store.write_json(run.dir / "status.json", st)
        self.assertEqual(runs.archive(30), 1)
        self.assertFalse(run.exists)
        self.assertIn("run names are never reused", runs.taken("old"))


@unittest.skipUnless(shutil.which("opencode"), "needs the real opencode")
class RealOpenCodeReports(Base):
    """A report task's round on the real OpenCode (CLAUDE.md: how OpenCode is configured changed,
    so one real run): the tools the layer's report rules leave the model, and submit_result's
    round trip from the model's call through OpenCode and ask_mcp to the runner's result.json.
    The model is tests/capture_llm.py, which records each request and makes the call it is given.
    The setUp is test_workbench.RealOpenCodeRun's."""

    def setUp(self):
        super().setUp()
        tmp = Path(self.tmp.name)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        conf = tmp / "xdg" / "config" / "opencode"
        conf.mkdir(parents=True)
        (conf / "opencode.json").write_text(json.dumps({"provider": {"cap": {
            "npm": "@ai-sdk/openai-compatible",
            "options": {"baseURL": f"http://127.0.0.1:{self.port}/v1", "apiKey": "x"},
            "models": {"cap": {"name": "capture", "tool_call": True,
                               "limit": {"context": 32768, "output": 1024}}}}}}))
        os.environ.update({f"XDG_{d.upper()}_HOME": str(tmp / "xdg" / d) for d in ("config", "data", "state")})
        os.environ["WB_OPENCODE_CONFIG"] = str(conf / "opencode.json")
        os.environ.pop("WB_OPENCODE_BIN")

    def report(self, name, result, *extra):
        """Task ``name``'s one round, the model calling submit_result with ``result``: its state and
        the tool lists of the requests that offered tools."""
        out = Path(self.tmp.name) / f"{name}-requests.jsonl"
        call = {"name": f"workbench_{ask_mcp.SUBMIT}", "arguments": result}
        server = subprocess.Popen([sys.executable, str(ROOT / "tests" / "capture_llm.py"), str(self.port),
                                   str(out), json.dumps([call])])
        self.addCleanup(server.kill)
        time.sleep(0.5)
        run_wb("start", name, "--spec", str(self.spec), "--repo", str(self.repo), "--model", "cap",
               "--report", *extra)
        st = wait_phase(name, {"review", "stuck", "failed"}, timeout=180)
        server.kill()
        server.wait()
        asked = [json.loads(x) for x in out.read_text().splitlines()]
        return st, [{t["function"]["name"]: t["function"] for t in r["tools"]} for r in asked if r.get("tools")]

    def test_a_read_report_gets_no_edit_tool_and_its_submit_result_reaches_result_json(self):
        st, offered = self.report("t-rread", VERDICT, "--schema", "verdict")
        task = store.Task("t-rread")
        self.assertEqual(st["phase"], "review", (task.dir / "runner.log").read_text()[-3000:])
        self.assertEqual(json.loads((task.dir / runner.RESULT).read_text()), VERDICT)
        self.assertTrue(offered)
        sch = json.loads((ROOT / "agent/schemas/verdict.json").read_text())
        for tools in offered:
            self.assertIn("read", tools)
            self.assertFalse({"edit", "write", "patch", "apply_patch"} & set(tools), sorted(tools))
            params = tools[f"workbench_{ask_mcp.SUBMIT}"]["parameters"]
            self.assertEqual({k: params.get(k) for k in ("type", "required", "properties")},
                             {k: sch[k] for k in ("type", "required", "properties")})

    def test_a_report_with_tools_none_gets_only_skill_and_workbenchs_own_tools(self):
        judged = {"judgments": [{"id": "F1", "meets_bar": False, "class": "none", "why": "no loss"}]}
        st, offered = self.report("t-rnone", judged, "--schema", "judgments", "--tools", "none")
        task = store.Task("t-rnone")
        self.assertEqual(st["phase"], "review", (task.dir / "runner.log").read_text()[-3000:])
        self.assertEqual(json.loads((task.dir / runner.RESULT).read_text()), judged)
        self.assertTrue(offered)
        for tools in offered:
            self.assertIn(f"workbench_{ask_mcp.SUBMIT}", tools)
            self.assertEqual({t for t in tools if not t.startswith("workbench_")} - {"skill"}, set(), sorted(tools))


if __name__ == "__main__":  # at the end: partway, a run as a script skipped every class after it
    unittest.main()
