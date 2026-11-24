"""wb verify: one regression test run on the base and on the candidate, each in a scratch
checkout under the task's own lane and environment, so review can tell a proven fix from a test
that already passed before the change (#96). Neither run touches the user's checkout or the
worktree under review: the candidate is read through a snapshot of it (gitops.snapshot), and the
base gets the candidate's test files copied in, which is what makes a test separable.

A base failure proves the fix only when the test failed on an assertion: a missing module, a
command not found or a timeout says nothing about the bug, so the comparison is inconclusive
(#97: an unrelated setup error can't count as proof).
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import shlex
import shutil
import tempfile
from pathlib import Path, PurePosixPath

from . import context, gitops, runner, store

LIMIT = 300  # seconds for each run; past it, that run is inconclusive

# what a test file's path looks like, in the languages workbench sees: tests/, test_x.py,
# x_test.go, x.test.ts, x.spec.js; and the data a test reads beside it: Go's testdata/, fixtures/,
# Jest's __snapshots__ (*.snap), golden/. Without the data, the base failed for lack of the golden
# file and was called proven (#500, the audit of 1.90). One pattern for verify and bench
# (`looks_like_test`). And the layouts that missed it: Django's tests.py, SwiftPM's Tests/ (the
# directory part in any case), .NET's MyApp.Tests/ and *Tests.cs, JVM/PHP *Test.java/*Spec.kt,
# Cypress's *.cy.ts, Perl's t/*.t. Unmatched, verify and bench add refused, and a Django change to
# polls/tests.py beside a new fixture copied the fixture alone: the base ran its old tests and
# passed, `base-passes` for a valid fix (#731, the 2026-11-21 audit).
TEST_PATH = re.compile(r"(^|/)(?i:tests?|spec|__tests__|testdata|fixtures?|__fixtures__|__snapshots__"
                       r"|golden)/|(^|/)test_[^/]*$|_test\.[A-Za-z]+$|\.(test|spec)\.[A-Za-z]+$"
                       r"|\.snap$|(^|/)tests\.py$|(^|/)[^/]*(Tests?|Spec)\.(cs|fs|vb|swift|kt|java"
                       r"|scala|groovy|php)$|\.Tests?/|\.cy\.[A-Za-z]+$|(^|/)t/[^/]+\.t$")
# a file not found, as the common runtimes say it: what a base missing the candidate's new file
# prints (Go's `open testdata/x.golden: no such file or directory`, Python's FileNotFoundError,
# Node's ENOENT)
MISSING = re.compile(r"no such file or directory|FileNotFoundError|\bENOENT\b"
                     r"|cannot find the (file|path) specified", re.I)
# an assertion failing, as the common runners print one: what went wrong first (pytest's `E
# assert`, unittest's AssertionError, jest's Expected:/expect(, Go's "expected … got"), then a
# failed test's header. Not a bare "expected": Python's `SyntaxError: expected ':'` matched it,
# and a base that never ran an assertion was called proven (#193, the 2026-11-19 audit).
ASSERTION = re.compile(r"AssertionError|^E\s+assert|\bassert(ion)?\b|^\s*Expected\b"
                       r"|\bexpected\b.*\b(but|got|received|to (be|equal|have))\b"
                       r"|Error: expect\(|panic: "
                       # PHPUnit and Pest, XCTest, swift-testing: none satisfied the `\b` after
                       # `assert`, so these stacks were never proven without --expect (#730)
                       r"|Failed asserting|\bXCT\w*Assert\w* failed|\bExpectation failed\b", re.I)
# PHPUnit's `FAILURES!`, Catch2's `file.cpp:5: FAILED:`, TAP and bats' `not ok 1`, swift-testing's
# `✘` (#730)
FAILED_TEST = re.compile(r"^(FAIL[:\s]|--- FAIL|\s*✕|FAILURES!|\S+:\d+: FAILED:|not ok \d+|\s*✘)")
# a run that failed before any test could: the code didn't parse, import, build or collect. A
# failed test's header beside one of these is the setup's failure, not an assertion's (Go prints
# `FAIL pkg [build failed]`)
SETUP = re.compile(r"\b(SyntaxError|IndentationError|TabError|ImportError|ModuleNotFoundError)\b"
                   r"|errors? (during collection|collecting)|\bfixture '[^']*' not found"
                   r"|Cannot find module|\[(build|setup) failed\]|could not compile"
                   r"|error TS\d{4}:|^\S+\.go:\d+:\d+: ", re.M)
# an error saying the base lacks what the test calls, not that it computes it wrong: the candidate
# adds `mod.slugify` or a `sep=` keyword and the test calls it. pytest quoted the failing source
# line (`>       assert mod.slugify(…)`) above `E  AttributeError: …`, which matched ASSERTION, and
# jest's `TypeError: slugify is not a function` came under a ✕ header: both were called proven,
# while the same test written `from mod import slugify` was inconclusive (#729, the 2026-11-21
# audit)
MISSING_API = re.compile(r"has no attribute '|\bis not a (function|constructor)\b|\bNameError: name '"
                         r"|\bReferenceError: \S+ is not defined|unexpected keyword argument"
                         r"|takes (from )?\d+ (to \d+ )?positional arguments? but"
                         r"|missing \d+ required (positional|keyword-only) argument"
                         r"|\bundefined method\b|\buninitialized constant\b"
                         # a script the candidate adds (bats), PHP's function (#730); dash's
                         # `<script>: 3: slugify: not found` too, not only bash's wording
                         # (nightly run 45362198509, 2026-11-24)
                         r"|\bcommand not found\b|\bCall to undefined function\b|(?m::\s+not found$)")
# pytest's own lines: `E   ` says what went wrong (the first one, what went wrong first); `>` quotes
# the source line it went wrong on, which is no evidence of anything (#729)
PYTEST_E = re.compile(r"^E\s{2,}(\S.*)$", re.M)
PYTEST_PROOF = re.compile(r"^(assert\b|AssertionError\b|Failed: )")


class _At(store.Task):
    """The task's lane and environment around a scratch checkout."""
    def __init__(self, name: str, worktree: Path):
        super().__init__(name)
        self._worktree = worktree

    @property
    def worktree(self) -> Path:
        return self._worktree


def _review_base(task: store.Task) -> str | None:
    """The commit this step is compared with: the base for step one, else the last approved
    step's, which wb next records (status `step_base`). A step with no changes has no commit of
    its own: from before the record, the newest earlier step that has one, else the base (#652:
    the step after a clean one died on "cannot find the last approved step's commit")."""
    step = task.status.get("steps", 1)
    if step == 1:
        return task.spec["base"]
    if at := task.status.get("step_base"):
        return at
    for earlier in range(step - 1, 0, -1):
        if found := gitops.step_commit(task, earlier, step):
            return found
    return task.spec["base"]


def looks_like_test(path: str) -> bool:
    """Whether a repo path looks like a test or the data a test reads (TEST_PATH)."""
    return bool(TEST_PATH.search(path))


def _changes(task: store.Task, tree: str, base_rev: str) -> list[tuple[str, str]]:
    """(status letter, path) for each file the candidate changed since ``base_rev``.
    NUL-separated, so a name with a tab or newline survives (gitops.name_status, bench's too)."""
    return gitops.name_status(task.worktree, base_rev, tree)


def test_files(task: store.Task, tree: str, base_rev: str) -> list[str]:
    """The candidate's changed files that look like tests, as they are in the candidate: what the
    base run borrows. Not the deleted ones: a renamed test listed its old name too, the overlay
    asked the candidate for it, and git stopped on the pathspec before any test ran (#194, the
    2026-11-19 audit)."""
    return [p for st, p in _changes(task, tree, base_rev)
            if not st.startswith("D") and looks_like_test(p)]


def removed_tests(task: store.Task, tree: str, base_rev: str) -> list[str]:
    """The test files (looks_like_test) the candidate deleted, a renamed test's old name among
    them: the base run goes without them too (#643)."""
    return [p for st, p in _changes(task, tree, base_rev)
            if st.startswith("D") and looks_like_test(p)]


def missing(text: str, added: list[str]) -> list[str]:
    """The files of ``added`` (the candidate's new ones, not copied to the base) that ``text``
    (the base's output) says it couldn't find: a line saying a file is missing that names one, by
    its path or its name. The base then failed for lack of the candidate's file, not on the bug
    (#500, the audit of 1.90)."""
    lines = [x for x in text[-200_000:].splitlines() if MISSING.search(x)]
    return [p for p in added if any(PurePosixPath(p).name in x for x in lines)][:10]


def assertion(text: str) -> str:
    """The line that says what a failed assertion found, else a failed test's header; ''. pytest's
    first `E` line when it printed one, and never a source line it quotes (`>`, #729)."""
    if e := PYTEST_E.search(text):
        return e.group(0).strip()[:300]
    lines = [x.strip() for x in text.splitlines() if not x.startswith(">")]
    for pattern in (ASSERTION, FAILED_TEST):
        for line in lines:
            if pattern.search(line):
                return line[:300]
    return ""


# The repo's own settings for how its files are checked out: a clone has none of its local config,
# so a repo with core.autocrlf (a Windows-born project, a WSL workspace) gave the task CRLF files
# and the base check and wb verify LF ones, and a test reading bytes failed there alone (#426, the
# audit of 1.8). .git/info/attributes too.
CHECKOUT_KEYS = r"^core\.(eol|autocrlf|safecrlf|symlinks|filemode|ignorecase|checkroundtripencoding)$"


def local_filters(repo: Path) -> list[tuple[str, str]]:
    """``repo``'s local `filter.<driver>.*` settings but Git LFS's (share_lfs has those), in
    order. A key with no value (`required` alone) is git's true."""
    out = gitops.git(repo, "config", "-z", "--local", "--includes", "--get-regexp", r"^filter\.",
                     check=False)
    pairs = [(k, v if nl else "true") for k, nl, v in (rec.partition("\n") for rec in out.split("\0"))]
    return [(k, v) for k, v in pairs if k and not k.lower().startswith("filter.lfs.")]


def checkout_settings(repo: Path, clone: Path) -> None:
    """``repo``'s local checkout settings (CHECKOUT_KEYS, its filters, its info/attributes) in
    ``clone``, before anything is checked out there. Its own clean/smudge filters too (git-crypt,
    transcrypt, a hand-written one): without them a filtered file checked out as the stored blob,
    the task's worktree (which shares the repo's config) had it smudged, and the base check
    failed on an unchanged file, which read as evidence for the step (#932). The main checkout
    runs these same commands: nothing new runs."""
    out = gitops.git(repo, "config", "--local", "--includes", "--get-regexp", CHECKOUT_KEYS,
                     check=False)
    for key, _, value in (line.partition(" ") for line in out.splitlines()):
        if key:
            gitops.git(clone, "config", key, value)
    for key, value in local_filters(repo):
        gitops.git(clone, "config", "--add", key, value)
    try:
        attrs = Path(repo) / gitops.git(repo, "rev-parse", "--git-path", "info/attributes").strip()
        if attrs.is_file():
            dest = Path(clone) / gitops.git(clone, "rev-parse", "--git-path", "info/attributes").strip()
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(attrs, dest)
    except (gitops.GitError, OSError):
        pass


FILTER_SAMPLE = 50  # filtered files compared with the task's worktree, at most


def filter_gap(worktree: Path, clone: Path) -> str | None:
    """Why ``clone``'s checkout of its filtered files isn't the task's worktree's, else None:
    the first of them (FILTER_SAMPLE at most) with the same blob in both and other bytes, the
    worktree's unedited. A filter that keeps state in the git dir (git-crypt's keys in
    .git/git-crypt) can't work from the copied config, and a scratch run on the stored bytes is
    no evidence either way (#932). No local filter: one git call."""
    if not Path(worktree).is_dir():
        return None
    filtered = _filtered_files(clone)
    paths = sorted(filtered)[:FILTER_SAMPLE]
    if not paths:
        return None
    mine, theirs = _blobs(clone, paths), _blobs(worktree, paths)
    differ = []
    for p in paths:
        if mine.get(p) and mine.get(p) == theirs.get(p):
            try:
                if (Path(clone) / p).read_bytes() != (Path(worktree) / p).read_bytes():
                    differ.append(p)
            except OSError:
                pass
    if differ:  # an edit in the worktree isn't the filter's doing
        edited = gitops.git_bytes(worktree, "diff", "--name-only", "-z", "--",
                                  *(f":(literal){p}" for p in differ)).split(b"\0")
        differ = [p for p in differ if os.fsencode(p) not in edited]
    if not differ:
        return None
    p = differ[0]
    return (f"{p} checks out differently in the scratch checkout than in the task's worktree "
            f"(filter.{filtered[p]}, the repo's own: state it keeps in the git dir, such as "
            "git-crypt's keys, isn't there), so this run wasn't on the task's bytes")


def _filtered_files(clone: Path) -> dict[str, str]:
    """Each of ``clone``'s files under one of its local smudging filters: its driver."""
    drivers = {k.split(".")[1] for k, _ in local_filters(clone)
               if k.lower().endswith((".smudge", ".process"))}
    if not drivers:
        return {}
    files = gitops.git_bytes(clone, "ls-files", "-z")
    said = gitops._run(clone, ("check-attr", "-z", "--stdin", "filter"), input=files,
                       check=False).stdout.split(b"\0")
    return {os.fsdecode(said[i]): os.fsdecode(said[i + 2]) for i in range(0, len(said) - 2, 3)
            if os.fsdecode(said[i + 2]) in drivers}


def _blobs(at: Path, paths: list[str]) -> dict[str, bytes]:
    out = gitops.git_bytes(at, "ls-files", "-s", "-z", "--", *(f":(literal){p}" for p in paths))
    return {os.fsdecode(rec.split(b"\t", 1)[1]): rec.split()[1]
            for rec in out.split(b"\0") if b"\t" in rec}


def smudge_gap(repo: Path, clone: Path) -> str | None:
    """filter_gap for a checkout with no task's worktree beside it (bench's: the task was
    accepted and its worktree removed, and a step's new files aren't in the user's checkout):
    the first filtered file (FILTER_SAMPLE at most) whose bytes in ``clone`` aren't what ``repo``
    itself makes of its blob (`git cat-file --filters`, run there, with the repo's git dir and so
    git-crypt's keys; the attributes of ``clone``'s tree, `--attr-source`). Read-only in
    ``repo``; a blob it lacks, a filter that fails there too, or a git before 2.40 (no
    --attr-source) is skipped (#932's review)."""
    filtered = _filtered_files(clone)
    paths = sorted(filtered)[:FILTER_SAMPLE]
    if not paths:
        return None
    tree = gitops.git(clone, "rev-parse", "HEAD^{tree}").strip()
    for p, blob in _blobs(clone, paths).items():
        made = gitops._run(repo, (f"--attr-source={tree}", "cat-file", "--filters",
                                  f"--path={p}", os.fsdecode(blob)), check=False)
        try:
            if made.returncode == 0 and (Path(clone) / p).read_bytes() != made.stdout:
                return (f"{p} checks out differently in the scratch checkout than in {repo} "
                        f"(filter.{filtered[p]}, the repo's own: state it keeps in the git dir, "
                        "such as git-crypt's keys, isn't there), so the checks wouldn't run on "
                        "the task's bytes")
        except OSError:
            pass
    return None


def _filter_failed(repo: Path, e: gitops.GitError) -> str | None:
    """The checkout's failure, named, when a filter of ``repo``'s made it (a `required` one that
    can't work in the scratch clone: #932), else None."""
    names = {k.split(".")[1] for k, _ in local_filters(repo)}
    hit = next((n for n in sorted(names) if f"filter {n} failed" in str(e)), None)  # git's words
    return (f"checking out the scratch clone failed in filter.{hit}, the repo's own (state it "
            f"keeps in the git dir, such as git-crypt's keys, isn't there): {str(e)[-300:]}") if hit else None


def _say(msg: str) -> None:
    print(msg, flush=True)


@contextlib.contextmanager
def gpu(task: store.Task, say=_say):
    """The GPU to the checks alone, for a task started with --unload-for-tests: queued with the
    tasks in first-come order, then the other models unloaded (models.json's keep_loaded kept) and
    their memory let go (media.gpu), as the round's own tests have it. The base check, wb verify
    and wb shot ran the project's GPU tests beside other tasks' resident models, and an
    out-of-memory failure read as the base's or the change's (#502, the audit of 1.90). This
    machine's GPU whatever the task's model: one on a wb remote box took none, yet the checks run
    here (#943). Otherwise nothing: tests that need no GPU don't wait for it."""
    if not task.spec.get("unload_for_tests"):
        yield
        return
    from . import media  # media imports models and hardware; only these checks need it
    with media.gpu(task.name, say, wait=True):
        yield


def _run(task: store.Task, checkout, cmd, limit: int, keep=None,
         setup: bool = False) -> tuple[int | str, str]:
    """``cmd`` in a scratch clone that ``checkout`` sets up, under the task's lane; ``keep(work)``
    takes what the run left before the clone goes. ``cmd`` may be a function of the clone's path,
    for a command that names it. Prepared from the task's worktree, unless ``setup`` (--setup)
    asks for the setup to run there again."""
    store.home().mkdir(parents=True, exist_ok=True)  # not in /tmp, where the git shim allows writes
    with tempfile.TemporaryDirectory(prefix=store.scratch(".verify-"), dir=store.home()) as scratch:
        # nested as the worktree is, so that a sibling two levels up stays in the scratch (#261)
        repo = gitops.repo_of(task.spec)  # a removed linked worktree: the main one (#498)
        work = context.tree(Path(scratch), task.spec["repo"], task.spec.get("siblings"), "repo")
        work.parent.mkdir(parents=True, exist_ok=True)
        gitops.scratch_clone(repo, work)  # a shallow repo's too (#884)
        checkout_settings(repo, work)  # its line endings, as the task's (#426)
        gitops.share_lfs(repo, work)  # its LFS files as data (#382)
        if sparse := gitops.sparse_of(repo):  # as the task's worktree is (#263)
            gitops.apply_sparse(work, sparse)
        try:
            checkout(work)
        except gitops.GitError as e:
            if why := _filter_failed(repo, e):
                return "filtered", f"[{why}]"
            raise
        if why := filter_gap(task.worktree, work):  # not the task's bytes: no evidence (#932)
            return "filtered", f"[{why}]"
        # its submodules as the task's worktree has them (#350)
        gitops.fill_submodules(repo, gitops.git(work, "rev-parse", "HEAD").strip(), work)
        at = _At(task.name, work)
        # its setup, copies and siblings, as the worktree (#201): copied from it (#249)
        code, said = runner.prepare(at, None if setup else task.worktree)
        if code != 0:
            why = runner.setup_timed_out(task.spec) if code == "timeout" else code
            return "setup failed", f"[setting up the scratch checkout failed: {why}]\n{said}"
        out = runner.run_in_lane(at, cmd(work) if callable(cmd) else cmd, limit)
        if keep:
            keep(work)
        return out


def on_base(task: store.Task, cmd: str, limit: int, say=_say,
            turn=contextlib.nullcontext) -> tuple[int | str, str]:
    """``cmd`` on the task's base commit alone, in a scratch checkout under its lane (the runner's
    base check): nothing it writes reaches the task's worktree. With the GPU alone when the
    task's tests need it (``gpu``), and then ``turn()`` (the runner's prep slot) around the run
    itself: taken first, the slot was held through the whole wait for the GPU, behind a task's
    round of up to 40 minutes, and every queued task's setup waited too (#679)."""
    with gpu(task, say), turn():
        return _run(task, lambda work: gitops.git(work, "checkout", "-q", "--detach", task.spec["base"]),
                    cmd, limit)


def _checkouts(task: store.Task, tree: str, files: list[str], base_rev: str,
               removed: list[str] = ()):
    """The base, with the candidate's ``files`` copied in and its ``removed`` tests gone, and the
    candidate as the worktree is."""
    head = gitops.git(task.worktree, "rev-parse", "HEAD").strip()

    def base(work: Path) -> None:
        gitops.git(work, "checkout", "-q", "--detach", base_rev)
        if files:  # the candidate's, alone; literal, so a `*` in a name is no glob
            gitops.git(work, "checkout", "-q", tree, "--", *(f":(literal){f}" for f in files))
        # the tests the candidate deleted or renamed away go from the base too: kept, broad
        # discovery ran a failing test_old.py on the base and only the passing test_new.py on the
        # candidate, and the old test's assertion was called proof with no code changed (#643,
        # Codex's audit of 1.90.6)
        for i in range(0, len(removed), 200):
            gitops.git(work, "rm", "-q", "--ignore-unmatch", "--",
                       *(f":(literal){f}" for f in removed[i:i + 200]))

    def candidate(work: Path) -> None:
        gitops.git(work, "checkout", "-q", "--detach", head)
        gitops.git(work, "read-tree", "-u", "--reset", tree)  # the worktree as it is, new files too
        if gitops.sparse_of(work):  # read-tree writes nothing outside the sparse patterns: the
            # agent's new package beside the cone was missing, and the candidate failed (#493)
            made = [f for f in gitops.git_bytes(work, "diff", "--name-only", "--no-renames", "-z",
                                                "--diff-filter=AMT", head, tree).split(b"\0") if f]
            for i in range(0, len(made), 200):  # skip-worktree, as read-tree marked them
                gitops.git(work, "checkout", "-q", "--ignore-skip-worktree-bits", tree, "--",
                           *(":(literal)" + os.fsdecode(f) for f in made[i:i + 200]))
    return base, candidate


def verdict(base: int | str, base_out: str, cand: int | str, expect: str = "",
            added: list[str] = ()) -> str:
    """proven, base-passes, candidate-fails or inconclusive. Proven only on positive evidence:
    ``expect`` (the failure Claude names, `wb verify --expect`) in the base's output, else an
    assertion line, else a failed test's header with no setup error beside it, nor a file of
    ``added`` (the candidate's new files the base didn't get) said missing."""
    if isinstance(base, str) or isinstance(cand, str):  # a timeout, an output limit, the setup
        return "inconclusive"
    if cand != 0:
        return "candidate-fails"
    if base == 0:
        return "base-passes"
    if expect:
        return "proven" if expect in base_out else "inconclusive"
    # The setup's failure first: a SyntaxError's traceback prints the bad source line, and an
    # `assert 2 = 2` there matched ASSERTION, so a base that never ran a test was called proven
    # (#326, the audit of 1.4)
    if SETUP.search(base_out) or missing(base_out, list(added)):  # #500
        return "inconclusive"
    # an API the candidate adds, missing on the base; and pytest's first error not an assertion's
    # (an AttributeError, a TypeError): the test never got to check the bug (#729)
    if MISSING_API.search(base_out):
        return "inconclusive"
    if (e := PYTEST_E.search(base_out)) and not PYTEST_PROOF.match(e.group(1)):
        return "inconclusive"
    return "proven" if assertion(base_out) else "inconclusive"


def run(task: store.Task, cmd: str, files: list[str] | None = None, limit: int = LIMIT,
        expect: str = "", setup: bool = False) -> dict:
    """Run ``cmd`` on the base (with the candidate's test files) and on the candidate; the
    result, also kept in tasks/<task>/verify/<n>/ with both outputs."""
    # the tree accept would commit: a big text file it lands is in it (#642); what accept refuses
    # without --include is left out, and lands with it, so then nothing here is proof (below)
    tree, left = gitops.snapshot_left(task.worktree, landing=True)
    base_rev = _review_base(task)
    if not base_rev:
        raise RuntimeError("cannot find the last approved step's commit for the comparison")
    files = test_files(task, tree, base_rev) if files is None else files
    if not files:
        raise RuntimeError("no test file among the candidate's changes: name the regression "
                           "test's files with --files (or review it as usual)")
    # the candidate's new files the base doesn't get: a test that reads one fails there for that
    uncopied = [p for st, p in _changes(task, tree, base_rev) if st.startswith("A") and p not in files]
    removed = removed_tests(task, tree, base_rev)
    base, candidate = _checkouts(task, tree, files, base_rev, removed)
    with gpu(task):  # both runs under one hold: nothing loads in between
        b_code, b_out = _run(task, base, cmd, limit, setup=setup)
        c_code, c_out = _run(task, candidate, cmd, limit, setup=setup)
    d = task.dir / "verify"
    n = 1 + max((int(p.name) for p in d.glob("*") if p.name.isdigit()), default=0)
    out = d / str(n)
    out.mkdir(parents=True)
    (out / "base.txt").write_text(b_out, encoding="utf-8")
    (out / "candidate.txt").write_text(c_out, encoding="utf-8")
    said = verdict(b_code, b_out, c_code, expect, uncopied)
    if left and said == "proven":  # a candidate missing files accept can land proves nothing (#642)
        said = "inconclusive"
    result = {"cmd": cmd, "files": files, "tree": tree, "ts": store.now(),
              "base": {"rev": base_rev, "exit": b_code, "assertion": assertion(b_out)},
              "candidate": {"rev": tree, "exit": c_code, "assertion": assertion(c_out)},
              "verdict": said, "dir": str(out),
              **({"expect": expect} if expect else {}),
              **({"uncopied": uncopied[:50]} if uncopied else {}),
              **({"removed": removed[:50]} if removed else {}),
              **({"left_out": left} if left else {})}
    for code, text in ((b_code, b_out), (c_code, c_out)):  # a checkout unlike the task's (#932)
        if code == "filtered":
            result["filtered"] = text.strip().strip("[]")
            break
    if b_code != 0 and (gone := missing(b_out, uncopied)):
        result["missing"] = gone
    # --files naming code (a Rust file with its inline tests): the base got the candidate's code
    # too, so its passing says nothing about the test (#731)
    if code := [f for f in files if not looks_like_test(f)]:
        result["code_copied"] = code[:50]
    store.write_json(out / "result.json", result)
    return result


SAY = {"proven": "proven: it fails on the base and passes on the candidate",
       "base-passes": "not proven: it already passes on the base, so it doesn't show the bug",
       "candidate-fails": "the candidate fails it",
        # not a setup error alone: a runner whose output the check doesn't read lands here too (#730)
        "inconclusive": "inconclusive: the base run didn't fail on an assertion the check "
                        "recognises (an error first: syntax, import, build, collection, or an API "
                        "or file the candidate adds; output it doesn't read, which --expect TEXT "
                        "can name; or not the --expect text), a run timed out, the setup command "
                        "failed, or the candidate left out files accept --include would land"}


def lines(r: dict) -> list[str]:
    """The report: the verdict, each run's exit and assertion, where the outputs are."""
    out = [f"regression check `{r['cmd']}` ({', '.join(r['files'])}): {SAY[r['verdict']]}"]
    if base_rev := r["base"].get("rev"):
        out.append(f"  comparing base commit {base_rev} to candidate tree {r['tree']}")
    for side in ("base", "candidate"):
        x = r[side]
        out.append(f"  {side}: exit {x['exit']}" + (f": {x['assertion']}" if x["assertion"] else ""))
    if r.get("filtered"):
        out.append(f"  {r['filtered']}")
    if r.get("missing"):
        out.append(f"  the base lacked what the candidate adds: {', '.join(r['missing'])} "
                   "(--files copies what the test reads onto the base)")
    if r.get("code_copied"):
        out.append(f"  the base got the candidate's code too, not only tests: "
                   f"{', '.join(r['code_copied'])} (so a pass there may be the change's)")
    if r.get("left_out"):
        out.append(f"  not in the candidate (accept refuses them without --include, and lands "
                   f"them with it): {', '.join(r['left_out'])}")
    out.append(f"  outputs: {r['dir']}/{{base,candidate}}.txt")
    return out


# where a capture command writes, in its checkout ($WB_SHOT_OUT): a fresh name each run, absent
# before it, since a PNG already there (a tracked .wb-shot.png) passed a capture that wrote
# nothing, `true` reporting both images captured (#344, the audit of 1.4)
SHOT = ".wb-shot-{}.png"


def shot(task: store.Task, cmd: str, files: list[str] | None = None, viewport: str = "1280x800",
         limit: int = LIMIT, setup: bool = False) -> dict:
    """Run one capture command on the base and on the candidate (#98), each in a scratch
    checkout under the task's lane: the same command, fixture and viewport, so the two images
    show the same interaction. The command is the project's own (a headless browser script, a
    test that saves a screenshot): it writes a PNG to $WB_SHOT_OUT and reads the size from
    $WB_SHOT_VIEWPORT. Kept in tasks/<task>/shots/<n>/ with what each image is of."""
    if not re.fullmatch(r"\d{2,5}x\d{2,5}", viewport):
        raise RuntimeError(f"--viewport {viewport}: give it as WIDTHxHEIGHT, e.g. 390x844")
    # what accept would land, as verify (#642); what it refuses without --include (a new binary
    # of 1 MiB or more, a directory past NEW_FILES_MAX) is said: a new photo missing from the
    # candidate showed as a broken image, with nothing said (#836)
    tree, left = gitops.snapshot_left(task.worktree, landing=True)
    # after wb next, the last approved step, as wb verify compares: the original base showed the
    # earlier steps' change as this one's (#501, the audit of 1.90)
    base_rev = _review_base(task)
    if not base_rev:
        raise RuntimeError("cannot find the last approved step's commit for the comparison")
    base, candidate = _checkouts(task, tree, files or [], base_rev,
                                 removed_tests(task, tree, base_rev))
    d = task.dir / "shots"
    n = 1 + max((int(p.name) for p in d.glob("*") if p.name.isdigit()), default=0)
    out = d / str(n)
    out.mkdir(parents=True)
    st = task.status
    meta = {"task": task.name, "step": st.get("steps", 1), "round": st.get("round", 1),
            "cmd": cmd, "viewport": viewport, "files": files or [], "ts": store.now(),
            "base": {"rev": base_rev}, "candidate": {"rev": tree}}
    if left:
        meta["left_out"] = left
    with gpu(task):
        _capture(task, cmd, viewport, limit, setup, out, meta, (("base", base), ("candidate", candidate)))
    store.write_json(out / "shots.json", meta)
    return {**meta, "dir": str(out)}


def _capture(task: store.Task, cmd: str, viewport: str, limit: int, setup: bool, out: Path,
             meta: dict, sides) -> None:
    """Each side's capture, its image and output in ``out``, what it gave in ``meta``."""
    for side, checkout in sides:
        dest = out / f"{side}.png"
        name = SHOT.format(secrets.token_hex(6))

        def keep(work: Path, dest=dest, name=name) -> None:
            p = work / name
            if p.is_file() and not p.is_symlink() and p.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n":
                shutil.copyfile(p, dest)
        # absolute: the command runs in the task's package (task.json `subdir`), where a relative
        # path landed while the image was looked for at the checkout's top (#433, the audit of 1.8).
        # Exported, then the command itself, in the agent's shell as --test and wb verify run it
        # (runner.test_shell): a nested `sh -c` was dash on Debian and Ubuntu, and `source
        # .venv/bin/activate && …` failed on both sides (#517). Every part of a compound command
        # sees the variables.
        def line(work: Path, name=name) -> str:
            return (f"export WB_SHOT_OUT={shlex.quote(str(work / name))} "
                    f"WB_SHOT_VIEWPORT={shlex.quote(viewport)}\n{cmd}")
        code, text = _run(task, checkout, line, limit, keep, setup)
        (out / f"{side}.txt").write_text(text, encoding="utf-8")
        # a capture that exits nonzero failed, PNG or not: one that wrote its image and exited 7
        # on both sides printed two normal-looking paths (#268, the audit of 1.2)
        ok = code == 0 and dest.exists()
        meta[side].update(exit=code, ok=ok, image=str(dest) if dest.exists() else None)
        if not ok:  # said, never hidden: the ordinary code review still stands
            meta[side]["error"] = (f"exit {code}" if code != 0 else "no PNG at $WB_SHOT_OUT") + \
                f": {out / (side + '.txt')}"


def shot_ok(side: dict) -> bool:
    """Whether one side's capture worked: exit 0 and a PNG (a shots.json before #268: a PNG)."""
    return bool(side.get("ok", side.get("image")))


def shot_lines(m: dict) -> list[str]:
    out = [f"screenshots of {m['task']}, step {m['step']} round {m['round']} at {m['viewport']} "
           f"(`{m['cmd']}`):"]
    for side in ("base", "candidate"):
        x = m[side]
        out.append(f"  {side} {x['rev'][:10]}: " + (
            f"{x['image']} (exit 0)" if shot_ok(x) else
            f"failed, {x['error']}" + (f" (the PNG it wrote anyway: {x['image']})" if x.get("image") else "")))
    if not (shot_ok(m["base"]) and shot_ok(m["candidate"])):
        out.append("  a capture failed: the code review stands on its own")
    if m.get("left_out"):
        out.append(f"  {LEFT_OUT.format(', '.join(m['left_out']))}")
    return out


# a shot's candidate without what accept lands only with --include (#836): in the report and the
# chat's message, where the user judges the picture
LEFT_OUT = ("not in the candidate (accept lands them only with --include): {}; "
            "its picture may not show them")


def note(task: store.Task) -> str:
    """For wb accept and the report: the newest check when it isn't proof of the change being
    accepted (not proven, or run on an earlier state of the worktree); '' when it is, or none ran."""
    r = last(task)
    if not r:
        return ""
    if r["stale"]:
        return (f"note: the last regression check (`{r['cmd']}`, {r['verdict']}) ran before the "
                "worktree last changed: it proves nothing about this state")
    if r["verdict"] != "proven":
        return f"note: the last regression check (`{r['cmd']}`) is {SAY[r['verdict']]}"
    return ""


def report_lines(task: store.Task) -> list[str]:
    """The newest regression check as `wb report` shows it at review (#97), [] for none: the web
    chat's task sheet shows the same lines (#1201)."""
    if not (r := last(task)):
        return []
    return lines(r) + (["  (stale: the worktree has changed since; run wb verify again)"] if r["stale"] else [])


def last(task: store.Task) -> dict | None:
    """The task's newest regression check, with ``stale`` when the worktree has changed since."""
    runs = sorted((p for p in (task.dir / "verify").glob("*") if p.name.isdigit()),
                  key=lambda p: int(p.name))
    if not runs:
        return None
    try:
        r = json.loads((runs[-1] / "result.json").read_text())
    except (OSError, ValueError):
        return None
    # Old checks always compared against task.json's original base (#252).
    stale = task.worktree.is_dir() and (
        r["base"].get("rev", task.spec["base"]) != _review_base(task)
        or gitops.snapshot(task.worktree, landing=True) != r.get("tree"))
    return {**r, "stale": stale}
