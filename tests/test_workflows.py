#!/usr/bin/env python3
"""Holds the merge gate to what ARCHITECTURE.md (*Shipping to main*) says it is.

Plain text checks (no YAML library): every action pinned to a commit, every
workflow with its own permissions, `check` never path-filtered and required by
main's ruleset, the plugin without a version (so installs track main), and
owner-merge.yml approving only the owner's pull requests without ever running
their code.
"""
import json
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
RULESET = ROOT / ".github" / "rulesets" / "main.json"
OWNER_ID = "26800291"  # a-schaefers
GITHUB_ACTIONS_APP = 15368


def text(name: str) -> str:
    return (WORKFLOWS / name).read_text()


class Pins(unittest.TestCase):
    def test_every_action_is_pinned_to_a_commit(self):
        for path in sorted(WORKFLOWS.glob("*.yml")):
            for line in path.read_text().splitlines():
                m = re.search(r"\buses:\s*(\S+)(.*)", line)
                if not m or line.lstrip().startswith("#"):
                    continue
                ref = m.group(1)
                with self.subTest(file=path.name, uses=ref):
                    self.assertRegex(ref, r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$", "pin a commit, not a tag")
                    self.assertRegex(m.group(2), r"#\s*v\d", "say which version the commit is")

    def test_every_workflow_sets_its_permissions(self):
        for path in sorted(WORKFLOWS.glob("*.yml")):
            with self.subTest(file=path.name):
                self.assertRegex(path.read_text(), r"(?m)^permissions:")


class RequiredCheck(unittest.TestCase):
    def test_check_runs_on_every_pull_request(self):
        on = text("check.yml").split("\njobs:")[0]
        self.assertIn("pull_request:", on)
        self.assertNotRegex(on, r"paths(-ignore)?:", "a required check must report on every pull request")
        self.assertRegex(text("check.yml"), r"(?m)^  check:$")

    def test_ruleset_requires_it_from_github_actions(self):
        rules = {r["type"]: r.get("parameters", {}) for r in json.loads(RULESET.read_text())["rules"]}
        checks = rules["required_status_checks"]["required_status_checks"]
        self.assertIn({"context": "check", "integration_id": GITHUB_ACTIONS_APP}, checks)

    def test_check_is_green_only_when_every_shard_and_the_mod_are(self):
        # check.yml's suite in six shards (2026-11-21: 28 to 32 minutes in one job): a matrix job
        # reports as `suite (1)`…, so `check` is the one name the ruleset waits for. Skipped
        # passes a required check, so it runs always() and fails unless both succeeded
        gate = job("check.yml", "check")
        self.assertIn("needs: [suite, mod]", gate)
        self.assertIn("if: always()", gate)
        for want in ("${{ needs.suite.result }}", "${{ needs.mod.result }}", '[ "$SUITE" = success ]',
                     '[ "$MOD" = success ]'):
            self.assertIn(want, gate)
        self.assertNotIn("continue-on-error", text("check.yml"))
        suite = job("check.yml", "suite")
        for want in ("fail-fast: false", "WB_TEST_SHARD: ${{ matrix.shard }}/6", "run: python3 -m unittest\n"):
            self.assertIn(want, suite)
        self.assertIn("claude plugin validate mod && claude plugin test mod", job("check.yml", "mod"))


def job(workflow: str, name: str) -> str:
    """One job's lines, up to the next job."""
    m = re.search(rf"(?ms)^  {name}:\n(.*?)(?=^  \S|\Z)", text(workflow).split("\njobs:\n", 1)[1])
    return m.group(1) if m else ""


class ReleaseGates(unittest.TestCase):
    """The 2026-11-19 audit: the macOS job could fail unseen and ran without OpenCode, nothing ran
    WSL, and no release recorded which of the platforms the README names were run."""

    def test_the_nightly_runs_macos_with_opencode_and_a_failure_shows(self):
        mac = job("nightly.yml", "macos")
        self.assertIn("runs-on: macos-latest", mac)
        self.assertNotIn("continue-on-error", mac)
        self.assertIn("scripts/install-opencode.sh", mac)
        pin = lambda t: re.search(r"OPENCODE_VERSION: '([\d.]+)'", t)[1]  # noqa: E731
        self.assertEqual(pin(mac), pin(text("check.yml")))

    def test_the_nightly_runs_wsl2_debian_as_a_user_who_isnt_root(self):
        wsl = job("nightly.yml", "wsl")
        for want in ("runs-on: windows-", "wsl --set-default-version 2", "--distribution Debian",
                     "bash scripts/ci-wsl.sh"):
            self.assertIn(want, wsl)
        self.assertNotIn("continue-on-error", wsl)
        self.assertEqual(re.search(r"OPENCODE_SHA256: '(\w+)'", wsl)[1],
                         re.search(r"OPENCODE_SHA256: '(\w+)'", text("check.yml"))[1])  # Linux x64's
        script = (ROOT / "scripts" / "ci-wsl.sh").read_text()
        for want in ("sudo -u ci", "install-opencode.sh", "python3 -m unittest"):
            self.assertIn(want, script)

    def test_the_macos_nightly_is_sharded_names_each_test_and_every_suite_job_fails_a_hang(self):
        # #950 (the outside review of 1.90.24): the macOS job ran past its 60 minutes and was
        # cancelled on every run, and unittest reports failures only at the end, so ~40 failures
        # and an 18-minute hang went unseen
        mac = job("nightly.yml", "macos")
        for want in ("shard: [1, 2, 3]", "fail-fast: false", "WB_TEST_SHARD: ${{ matrix.shard }}/3",
                     "WB_TEST_HANG_SECS:", "python3 -X faulthandler -m unittest -v"):
            self.assertIn(want, mac)
        for workflow, name in (("nightly.yml", "check"), ("nightly.yml", "wsl"), ("check.yml", "suite"),
                               ("opencode.yml", "check")):
            with self.subTest(workflow=workflow, job=name):
                self.assertIn("WB_TEST_HANG_SECS:", job(workflow, name))
        self.assertIn("WB_TEST_HANG_SECS", job("nightly.yml", "wsl").split("WSLENV:")[1].split("\n")[0])
        self.assertIn("WB_TEST_HANG_SECS", (ROOT / "scripts" / "ci-wsl.sh").read_text())

    def test_the_release_checklist_names_each_platform_and_the_nightly(self):
        checklist = (ROOT / "docs" / "release-checklist.md").read_text()
        for want in ("gh workflow run nightly.yml", "`macos`", "`wsl`", "Linux", "Apple silicon",
                     "WSL2", "/mnt/c", "skip is not a pass", "docs/releases/<version>.md"):
            self.assertIn(want, checklist)
        self.assertIn("docs/release-checklist.md", (ROOT / "ARCHITECTURE.md").read_text())

    def test_the_release_checklist_runs_what_the_readme_offers_at_the_tag(self):
        # #422 (the audit of 1.8): the 1.8 tag's README offered audio, and `wb audio` landed after it
        checklist = (ROOT / "docs" / "release-checklist.md").read_text()
        self.assertIn("## What the README offers", checklist)
        self.assertIn("runs at the tagged commit", checklist)
        self.assertIn("--help` exits 0", checklist)

    def test_the_release_checklist_records_the_evidence_against_the_candidate(self):
        # #482 (the audit of 1.8): the by-hand runs that would earn 9/10, each on the exact SHA
        checklist = (ROOT / "docs" / "release-checklist.md").read_text()
        self.assertIn("| Run | Result | SHA | OS, filesystem | Versions | Hardware |", checklist)
        for want in ("--no-sign", "blobless", "LFS", "8,192", "Landlock ABI 9", "tailscale serve",
                     "IPv6-only", "real phones", "Python 3.11", "1.7 state directory", "Metal",
                     "mounted Windows volume", "AMD", "NVIDIA", "shared Ollama", "never a gate"):
            self.assertIn(want, checklist)


class Marketplace(unittest.TestCase):
    def test_the_plugin_has_no_version_so_installs_track_main(self):
        # a version would hold every install on its cached copy until the string changed, which
        # took a bump in every pull request (2026-11-18); without one, Claude Code uses the commit
        manifest = json.loads((ROOT / "mod" / ".claude-plugin" / "plugin.json").read_text())
        entries = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())["plugins"]
        self.assertNotIn("version", manifest)
        self.assertFalse([e for e in entries if "version" in e])


class Ruleset(unittest.TestCase):
    def test_main_takes_pull_requests_only_with_an_approval(self):
        ruleset = json.loads(RULESET.read_text())
        rules = {r["type"]: r.get("parameters", {}) for r in ruleset["rules"]}
        self.assertEqual(ruleset["enforcement"], "active")
        self.assertEqual(ruleset["conditions"]["ref_name"]["include"], ["~DEFAULT_BRANCH"])
        self.assertEqual(ruleset["bypass_actors"], [], "no bypass: the owner and every agent alike")
        self.assertIn("deletion", rules)
        self.assertIn("non_fast_forward", rules)
        self.assertEqual(rules["pull_request"]["required_approving_review_count"], 1)
        self.assertTrue(rules["pull_request"]["dismiss_stale_reviews_on_push"])


class OwnerMerge(unittest.TestCase):
    def setUp(self):
        self.text = text("owner-merge.yml")
        self.code = "\n".join(l for l in self.text.splitlines() if not l.lstrip().startswith("#"))

    def test_runs_mains_copy_and_never_the_pull_requests_code(self):
        self.assertIn("pull_request_target:", self.code)
        self.assertNotRegex(self.code, r"\buses:", "no checkout, no action: nothing from the branch runs")
        self.assertNotRegex(self.code, r"\bgit\b|\bnpm\b|\bnode\b|python", "no code from the branch")

    def test_reads_nothing_from_the_event_but_numbers_hashes_and_ids(self):
        used = set(re.findall(r"\$\{\{\s*([^}]+?)\s*\}\}", self.code))
        allowed = {
            "github.event.pull_request.number",
            "github.event.pull_request.head.sha",
            "github.repository",
            "github.token",
        }
        self.assertLessEqual(used, allowed, "event text (a title, a branch name) must never reach a shell")

    def test_approves_only_the_owners_own_pull_request(self):
        cond = re.search(r"if: >-\n((?:\s{6}.+\n)+)", self.code).group(1)
        self.assertIn(f"github.event.pull_request.user.id == {OWNER_ID}", cond)
        self.assertIn(f"github.event.sender.id == {OWNER_ID}", cond)
        self.assertIn("github.event.pull_request.head.repo.full_name == github.repository", cond)
        self.assertIn("!github.event.pull_request.draft", cond)
        self.assertEqual(cond.count("&&"), 3, "every condition must hold")

    def test_approves_and_merges_only_the_head_it_was_woken_for(self):
        self.assertIn('commit_id="$HEAD"', self.code)
        for merge in re.findall(r"gh pr merge [^\n]*--merge[^\n]*", self.code):
            self.assertIn('--match-head-commit "$HEAD"', merge)


if __name__ == "__main__":
    unittest.main()


class SuiteHooks(unittest.TestCase):
    """tests/__init__.py's CI hooks (#950): a hang named and failed, and the suite in shards."""

    def test_a_test_hung_past_wb_test_hang_secs_fails_named_with_its_stack_and_the_suite_goes_on(self):
        script = ("import sys, time, unittest\n"
                  "sys.path.insert(0, sys.argv[1])\n"
                  "import tests  # the hooks\n"
                  "class Hangs(unittest.TestCase):\n"
                  "    def test_hangs(self):\n"
                  "        time.sleep(60)\n"
                  "    def test_then_this(self):\n"
                  "        pass\n"
                  "unittest.main(argv=['x', '-v'])\n")
        began = time.monotonic()
        r = subprocess.run([sys.executable, "-c", script, str(ROOT)], capture_output=True, text=True,
                           env={**os.environ, "WB_TEST_HANG_SECS": "1"}, timeout=50)
        self.assertLess(time.monotonic() - began, 30)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("TimeoutError: __main__.Hangs.test_hangs hung past WB_TEST_HANG_SECS (1 s)", r.stderr)
        self.assertIn("in test_hangs", r.stderr)                       # its stack, dumped
        self.assertIn("test_then_this (__main__.Hangs.test_then_this) ... ok", r.stderr)
        self.assertIn("Ran 2 tests", r.stderr)

    def test_the_shards_deal_the_discovered_or_named_suite_test_by_test_with_none_lost(self):
        def ids(shard=None, named=False):
            env = {k: v for k, v in os.environ.items() if k != "WB_TEST_SHARD"}
            if shard:
                env["WB_TEST_SHARD"] = shard
            with mock.patch.dict(os.environ, env, clear=True):
                suite = unittest.TestLoader().loadTestsFromName("tests") if named else \
                    unittest.TestLoader().discover(str(ROOT), top_level_dir=str(ROOT))
            out, todo = [], [suite]
            while todo:
                t = todo.pop()
                if isinstance(t, unittest.TestSuite):
                    todo.extend(t)
                else:
                    out.append(t.id())
            return out
        whole = ids()
        for n in (3, 6):  # the macOS nightly's, check.yml's
            with self.subTest(shards=n):
                parts = [ids(f"{k}/{n}") for k in range(1, n + 1)]
                self.assertEqual(sorted(sum(parts, [])), sorted(whole))     # each test once
                self.assertLessEqual(max(map(len, parts)) - min(map(len, parts)), 1)
                # every class sliced across the shards: dealt whole, Lifecycle's 147 slow tests
                # weighed on one shard (2026-11-21)
                for cls in {i.rsplit(".", 1)[0] for i in whole}:
                    each = [sum(i.rsplit(".", 1)[0] == cls for i in p) for p in parts]
                    self.assertLessEqual(max(each) - min(each), 1, cls)
                # the package named, `python3 -m unittest tests`: the same shards, dealt once (the
                # review of #950: its load_tests ran twice, nested, and each shard ran about a
                # third of its own)
                for k, part in zip(range(1, n + 1), parts):
                    self.assertEqual(sorted(ids(f"{k}/{n}", named=True)), sorted(part))
        self.assertEqual(sorted(ids(named=True)), sorted(whole))
