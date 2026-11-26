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
import shutil
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
# scripts/suite.sh's wait -n -p; macOS's bash is 3.2 (the nightly runs this file there)
_bash_51 = bool(shutil.which("bash")) and subprocess.run(
    ["bash", "-c", "((BASH_VERSINFO[0] * 100 + BASH_VERSINFO[1] >= 501))"]).returncode == 0
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

    def test_check_is_green_only_when_every_shard_the_mod_the_kernel_and_the_opencode_plugin_are(self):
        # check.yml's suite in six shards (2026-11-21: 28 to 32 minutes in one job): a matrix job
        # reports as `suite (1)`…, so `check` is the one name the ruleset waits for. Skipped
        # passes a required check, so it runs always() and fails unless all four succeeded (the
        # OpenCode plugin's job since #1328)
        gate = job("check.yml", "check")
        self.assertIn("needs: [suite, mod, kernel, opencode-plugin]", gate)
        self.assertIn("if: always()", gate)
        for want in ("${{ needs.suite.result }}", "${{ needs.mod.result }}", "${{ needs.kernel.result }}",
                     "${{ needs.opencode-plugin.result }}", '[ "$SUITE" = success ]', '[ "$MOD" = success ]',
                     '[ "$KERNEL" = success ]', '[ "$PLUGIN" = success ]'):
            self.assertIn(want, gate)
        self.assertNotIn("continue-on-error", text("check.yml"))
        suite = job("check.yml", "suite")
        for want in ("fail-fast: false", "WB_TEST_SHARD: ${{ matrix.shard }}/6",
                     'run: exec scripts/suite.sh "$WB_TEST_SHARD" 4\n'):
            self.assertIn(want, suite)
        self.assertIn("claude plugin validate mod && claude plugin test mod", job("check.yml", "mod"))

    def test_the_kernel_job_rebuilds_against_the_commit(self):
        # #1356: the committed mod/hooks/kernel.js must be what kernel/src builds with the pinned
        # toolchain (its props under OpenCode's Bun, installed first)
        kernel = job("check.yml", "kernel")
        steps = ["scripts/install-opencode.sh", "actions/cache@", "python3 scripts/kernel.py fetch",
                 "python3 scripts/kernel.py check"]
        at = [kernel.find(s) for s in steps]
        self.assertNotIn(-1, at, steps)
        self.assertEqual(at, sorted(at), "in this order: Bun, the cache, fetch, check")
        self.assertIn("hashFiles('kernel/toolchain.json', 'kernel/spago.lock')", kernel)
        self.assertNotIn("paths:", kernel)

    def test_the_opencode_plugin_job_runs_its_tests_and_types_under_the_pinned_opencodes_bun(self):
        # #1328: the plugin's tests ran only inside the kernel's job, as an afterthought; its own
        # job, as the mod has, runs the commands wb patch upstream names for a plugin patch, and
        # the goldens must hold under OpenCode's Bun as under Claude Code (#1356)
        from workbench import patches
        plugin = job("check.yml", "opencode-plugin")
        steps = ['scripts/install-opencode.sh "$OPENCODE_VERSION" "$OPENCODE_SHA256"',
                 *patches.PLUGIN_TESTS.split(" && ")]
        at = [plugin.find(s) for s in steps]
        self.assertNotIn(-1, at, steps)
        self.assertEqual(at, sorted(at), "in this order: OpenCode, the tests, the types")
        self.assertNotIn("paths:", plugin)
        self.assertNotIn("continue-on-error", plugin)
        self.assertNotIn("opencode test", job("check.yml", "kernel"))

    def test_the_macos_nightly_checks_the_kernel_on_one_shard_and_opencode_yml_runs_the_goldens(self):
        # #1356: the bundle's bytes must not depend on the platform, and BUN_BE_BUN, undocumented,
        # must still hold on the newest OpenCode
        mac = job("nightly.yml", "macos")
        steps = ["actions/cache@", "python3 scripts/kernel.py fetch", "python3 scripts/kernel.py check"]
        at = [mac.find(s) for s in steps]
        self.assertNotIn(-1, at, steps)
        self.assertEqual(at, sorted(at))
        self.assertGreater(at[0], mac.find("python3 -X faulthandler -m unittest -v"), "after the suite")
        for s in steps:
            line_start = mac.rfind("\n      - ", 0, mac.find(s))
            self.assertIn("if: matrix.shard == 1", mac[line_start:mac.find(s)], f"{s}: shard 1 only")
        self.assertIn("hashFiles('kernel/toolchain.json', 'kernel/spago.lock')", mac)
        oc = job("opencode.yml", "check")
        self.assertIn("BUN_BE_BUN=1 opencode test opencode-plugin/test", oc)
        self.assertLess(oc.find("scripts/install-opencode.sh newest"), oc.find("BUN_BE_BUN=1 opencode test"))


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
        for want in ("sudo -u ci", "install-opencode.sh", 'exec scripts/suite.sh "$WB_TEST_SHARD" 4'):
            self.assertIn(want, script)

    def test_the_macos_nightly_is_sharded_names_each_test_and_every_suite_job_fails_a_hang(self):
        # #950 (the outside review of 1.90.24): the macOS job ran past its 60 minutes and was
        # cancelled on every run, and unittest reports failures only at the end, so ~40 failures
        # and an 18-minute hang went unseen
        mac = job("nightly.yml", "macos")
        for want in ("shard: [1, 2, 3, 4, 5]", "fail-fast: false", "WB_TEST_SHARD: ${{ matrix.shard }}/5",
                     "WB_TEST_HANG_SECS:", "python3 -X faulthandler -m unittest -v"):
            self.assertIn(want, mac)
        for workflow, name in (("nightly.yml", "check"), ("nightly.yml", "wsl"), ("check.yml", "suite"),
                               ("opencode.yml", "check")):
            with self.subTest(workflow=workflow, job=name):
                self.assertIn("WB_TEST_HANG_SECS:", job(workflow, name))
        self.assertIn("WB_TEST_HANG_SECS", job("nightly.yml", "wsl").split("WSLENV:")[1].split("\n")[0])
        self.assertIn("WB_TEST_HANG_SECS", (ROOT / "scripts" / "ci-wsl.sh").read_text())

    def test_the_nightlys_linux_and_wsl_suites_run_in_shards_of_four_processes(self):
        # both ran the suite in one process and were cancelled at their 60 minutes in the last two
        # runs before 2.1 (run 45707953076, 2026-11-25)
        for name in ("check", "wsl"):
            with self.subTest(job=name):
                got = job("nightly.yml", name)
                for want in ("shard: [1, 2, 3]", "fail-fast: false", "WB_TEST_SHARD: ${{ matrix.shard }}/3"):
                    self.assertIn(want, got)
        self.assertIn('run: exec scripts/suite.sh "$WB_TEST_SHARD" 4\n', job("nightly.yml", "check"))
        self.assertNotIn("python3 -m unittest", job("nightly.yml", "check"))
        # the mod's test once, not once a shard
        self.assertEqual(job("nightly.yml", "check").count("if: matrix.shard == 1\n"), 3)
        self.assertIn("claude plugin validate mod && claude plugin test mod", job("nightly.yml", "check"))
        self.assertIn("WB_TEST_SHARD", job("nightly.yml", "wsl").split("WSLENV:")[1].split("\n")[0])
        script = (ROOT / "scripts" / "ci-wsl.sh").read_text()
        self.assertIn("WB_TEST_SHARD", script.split("--preserve-env=")[1].split(" ")[0])
        self.assertNotIn("python3 -m unittest", script)

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
                  "    def test_hangs_where_an_oserror_is_caught(self):\n"
                  "        try:\n"
                  "            time.sleep(60)\n"
                  "        except OSError:\n"
                  "            pass\n"
                  "    def test_then_this(self):\n"
                  "        pass\n"
                  "unittest.main(argv=['x', '-v'])\n")
        began = time.monotonic()
        r = subprocess.run([sys.executable, "-c", script, str(ROOT)], capture_output=True, text=True,
                           env={**os.environ, "WB_TEST_HANG_SECS": "1"}, timeout=50)
        self.assertLess(time.monotonic() - began, 30)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("tests.Hung: __main__.Hangs.test_hangs hung past WB_TEST_HANG_SECS (1 s)", r.stderr)
        # a hang inside an `except OSError` fails too: as a TimeoutError it was caught there, in
        # socket.getfqdn on the macOS nightly, and the test passed after 600 s (2026-11-22)
        self.assertIn("tests.Hung: __main__.Hangs.test_hangs_where_an_oserror_is_caught hung past", r.stderr)
        self.assertIn("in test_hangs", r.stderr)                       # its stack, dumped
        self.assertIn("test_then_this (__main__.Hangs.test_then_this) ... ok", r.stderr)
        self.assertIn("Ran 3 tests", r.stderr)

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
        for n in (5, 6):  # the macOS nightly's, check.yml's
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

    @unittest.skipUnless(_bash_51, "scripts/suite.sh needs bash 5.1 (wait -n -p): check.yml's Linux has it")
    def test_suite_sh_runs_each_test_of_its_shard_once_across_its_processes_and_fails_with_any(self):
        # check.yml's shards in four processes each (2026-11-25: one process left its runner's
        # CPUs half idle, and the slowest shard was every pull request's 10-minute wait)
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "tests").mkdir()
        shutil.copy(ROOT / "tests" / "__init__.py", tmp / "tests" / "__init__.py")
        (tmp / "ran").mkdir()
        body = "".join(f"    def test_{i:02}(self):\n        ran(self)\n" for i in range(24))
        (tmp / "tests" / "test_many.py").write_text(
            "import os, unittest\n"
            "def ran(test):\n"
            "    name = test.id().rsplit('.', 1)[1]\n"
            "    os.close(os.open(os.path.join('ran', name), os.O_CREAT | os.O_EXCL))  # twice: raises\n"
            "    if os.path.exists(os.path.join('fail', name)):\n"
            "        test.fail('told to fail: ' + name)\n"
            "class Many(unittest.TestCase):\n" + body)
        script = str(ROOT / "scripts" / "suite.sh")
        env = {k: v for k, v in os.environ.items() if k != "WB_TEST_SHARD"}
        r = subprocess.run(["bash", script, "2/3", "4"], cwd=tmp, env=env, capture_output=True,
                           text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(sorted(p.name for p in (tmp / "ran").iterdir()),
                         [f"test_{i:02}" for i in range(24) if i % 3 == 1])  # shard 2/3's, once each
        for sub in ("2/12", "5/12", "8/12", "11/12"):
            self.assertIn(f"=== WB_TEST_SHARD={sub}: exit 0", r.stdout)
        # one process failing fails the shard, its log printed under its own shard
        shutil.rmtree(tmp / "ran")
        (tmp / "ran").mkdir()
        (tmp / "fail").mkdir()
        (tmp / "fail" / "test_04").touch()  # index 4: 4 mod 12 = 4, so 5/12's
        r = subprocess.run(["bash", script, "2/3", "4"], cwd=tmp, env=env, capture_output=True,
                           text=True, timeout=120)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        failed = r.stdout.split("=== WB_TEST_SHARD=5/12: exit 1\n", 1)[1].split("=== ", 1)[0]
        self.assertIn("told to fail: test_04", failed)
        self.assertEqual(len(list((tmp / "ran").iterdir())), 8)  # the others still ran
        self.assertEqual(subprocess.run(["bash", script, "4/3", "4"], cwd=tmp, env=env,
                                        capture_output=True).returncode, 64)

    @unittest.skipUnless(_bash_51, "scripts/suite.sh needs bash 5.1 (wait -n -p): check.yml's Linux has it")
    def test_suite_sh_cancelled_prints_every_log_so_far_and_stops_its_processes(self):
        # a cancel's signal reaches suite.sh only through check.yml's exec: the step's shell never
        # passes SIGINT or SIGTERM on, and suite.sh was SIGKILLed with no log printed (its review,
        # 2026-11-25)
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "tests").mkdir()
        shutil.copy(ROOT / "tests" / "__init__.py", tmp / "tests" / "__init__.py")
        (tmp / "tests" / "test_slow.py").write_text(
            "import os, sys, time, unittest\n"
            "class Slow(unittest.TestCase):\n" + "".join(
                f"    def test_{i}(self):\n"
                f"        print('started {i}', os.getpid(), file=sys.stderr, flush=True)\n"
                "        open(os.path.join('pids', str(os.getpid())), 'w').close()\n"
                "        time.sleep(120)\n" for i in range(2)))
        (tmp / "pids").mkdir()
        env = {k: v for k, v in os.environ.items() if k != "WB_TEST_SHARD"}
        # SIGINT back to its default first: run by suite.sh itself in CI, this test is a background
        # job's child, and a background job ignores SIGINT, which a shell started with it ignored
        # can't trap (#1370's CI, 2026-11-25: the cancel never reached suite.sh, timed out at 30 s).
        # The runner's step shell is no background job, so its suite.sh gets SIGINT as here.
        step = subprocess.Popen(["bash", "-c", f"exec {ROOT / 'scripts' / 'suite.sh'} 1/1 2"], cwd=tmp,
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))
        self.addCleanup(lambda: step.poll() is None and step.kill())
        deadline = time.monotonic() + 60
        while len(list((tmp / "pids").iterdir())) < 2 and time.monotonic() < deadline:
            time.sleep(0.1)
        pids = [int(p.name) for p in (tmp / "pids").iterdir()]
        self.assertEqual(len(pids), 2)
        step.send_signal(signal.SIGINT)  # the runner's cancel: the step's process alone
        out, _ = step.communicate(timeout=30)
        self.assertEqual(step.returncode, 1, out)
        for sub, i in (("1/2", 0), ("2/2", 1)):
            self.assertIn(f"=== WB_TEST_SHARD={sub}: cancelled\nstarted {i}", out)
        deadline = time.monotonic() + 10
        while any(_alive(p) for p in pids) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertFalse([p for p in pids if _alive(p)], "suite.sh left its processes running")


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # a zombie is dead, waiting on its reaper
        return Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1][:1] != "Z"
    except OSError:
        return not Path("/proc/self").exists()
