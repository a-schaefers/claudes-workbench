"""wb bench: real accepted tasks replayed against any model, graded by the tests they were accepted
with, so models (or a change to workbench itself) are compared on the same work.

A case (`wb bench add`) is one step of an accepted task: its base as a git bundle of that commit's
history alone (the agent can't read the answer out of git), its spec and acceptance command, the
grants it had, and its hidden checks: the reference's test files, copied over the replay's work
before the acceptance command runs again, plus an optional --check command. A run (`wb bench
run`) is a detached driver that replays each case on each model, one at a time, as a trial task
that wakes no one and records no results row, and appends one row per replay to
bench/runs.jsonl. Real work comes first: a task that needs the GPU stops the bench's
(cli._preempt_bench), and the driver waits until no real task runs before each replay.

The synthetic suite this replaces (removed 2026-11-16) graded toy tasks every model passed in one
round; these are the tasks this machine's models were actually given.
"""
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

from . import ask_mcp, context, gitops, media, models, results, router, runner, spend, store, verify
from .runner import PKG_ROOT
from .watch import Watcher

PREEMPTED = "preempted: a real task needed the GPU"
REFUSED = ("No reviewer is available in this run, so nothing outside the everyday rules can be "
           "granted. Do the task without it, and say so in your summary.")
# The protocol's fold (cli.PROTOCOL, `transcript`'s marker) and its acceptance line; a step's
# opening (cli.cmd_next). Prompt openings are an API: these read the prompts wb wrote.
FOLD = "\n---\nHow to work"
# across lines and to the sentence's end: `.+` stopped at a newline, and a command of several
# lines (`set -e` and a script) read as none (#941, the v1.90.24 review). Only for a task from
# before status `step_tests`, which records each step's command as data
TEST_LINE = re.compile(r"^- The acceptance command is `(.+?)`\. Run it before you finish", re.M | re.S)
STEP_HEAD = re.compile(r"\AStep (\d+) of this task is done and committed \([^)]*\)\. The next step:\n\n")
BASE_CHECK_SECS = 120  # the hidden checks on the base, at add time; past this, not checked


def home() -> Path:
    return store.home() / "bench"


def case_dir(name: str) -> Path:
    return home() / "cases" / name


def runs_file() -> Path:
    return home() / "runs.jsonl"


def cases() -> dict[str, dict]:
    d = home() / "cases"
    found = {}
    for p in sorted(d.iterdir()) if d.is_dir() else ():
        if (c := store.read_json(p / "case.json")) and c.get("name") == p.name:
            found[p.name] = c
    return found


def remove(names: list[str]) -> list[str]:
    """`wb bench rm`: the cases gone, with their bundles; their rows in runs.jsonl stay, the
    history. Nothing ever removed one, and a case keeps its base and hidden checks for good
    (#740, the 2026-11-21 audit). Refused while a run goes: its plan names them."""
    have = cases()
    if unknown := [n for n in names if n not in have]:
        raise RuntimeError(f"no case {', '.join(unknown)} (wb bench list)")
    if driver():
        raise RuntimeError("a bench run is going: wb bench stop first")
    for n in names:
        shutil.rmtree(case_dir(n))
    return names


RUNS_KEPT = 20  # the runs whose replays keep their logs, diffs and grades; older runs keep plan.json


def prune_runs(keep: int = RUNS_KEPT) -> int:
    """Each replay's directory (wb.log, diff.patch, grade.txt: up to 640 KB of test output) of the
    runs older than the newest ``keep``, never the one going; plan.json and runs.jsonl's rows
    stay, which the report and the Stats tab read. They were kept for good (#740). How many went."""
    runs = home() / "runs"
    going = driver().get("run")
    old = sorted((d for d in runs.iterdir() if d.is_dir()), key=lambda d: d.stat().st_mtime,
                 reverse=True)[keep:] if runs.is_dir() else []
    gone = 0
    for d in old:
        if d.name == going:
            continue
        for replay in d.iterdir():
            if replay.is_dir() and not replay.is_symlink():
                shutil.rmtree(replay, ignore_errors=True)
                gone += 1
    return gone


# --- making a case ------------------------------------------------------------------------------

def _step_prompt(task: store.Task, step: int) -> str | None:
    """The prompt that handed the agent step `step`: round 1's, or the one `wb next` wrote; a
    round's own (`store.round_prompt`), not the handoff a fresh session got in its place (#503)."""
    rounds = sorted(int(p.name) for p in (task.dir / "rounds").iterdir()
                    if p.name.isdigit()) if (task.dir / "rounds").is_dir() else []
    for n in rounds:
        try:
            text = store.round_prompt(task, n).read_text(encoding="utf-8")
        except OSError:
            continue
        head = STEP_HEAD.match(text)
        if (step == 1 and n == 1 and not head) or (head and int(head.group(1)) == step - 1):
            return text
    return None


def _spec_of(prompt: str) -> str:
    text = STEP_HEAD.sub("", prompt, count=1)
    return text.split(FOLD, 1)[0].rstrip() + "\n"


def _safe(path: str) -> bool:
    p = Path(path)
    return bool(path) and not p.is_absolute() and ".." not in p.parts


def bundle(repo: Path, commit: str, dest: Path) -> None:
    """A bundle of `commit`'s tree alone, as branch main's one commit (its message, no parent):
    made in a scratch clone, so the user's repo gets no ref, and nothing after it (the answer) is
    in it. The whole history it once packed was the repo's every reachable object, about 800 MB a
    case on a big repo, for good (#740, the 2026-11-21 audit); and a shallow clone, whose cut-off
    history couldn't be bundled (#478), has the tree."""
    with tempfile.TemporaryDirectory(prefix="wb-bench-") as tmp:
        clone = Path(tmp) / "c"
        gitops.scratch_clone(repo, clone)  # a shallow repo's too (#884)
        when = gitops.git(clone, "log", "-1", "--format=%aI", commit).strip()
        who = {"GIT_AUTHOR_NAME": "workbench bench", "GIT_AUTHOR_EMAIL": "bench@workbench.invalid",
               "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}
        who |= {"GIT_COMMITTER_NAME": who["GIT_AUTHOR_NAME"], "GIT_COMMITTER_EMAIL": who["GIT_AUTHOR_EMAIL"]}
        message = gitops.git(clone, "log", "-1", "--format=%B", commit).strip() or "the base"
        root = gitops.git(clone, "commit-tree", "--no-gpg-sign", "-m", message, f"{commit}^{{tree}}",
                          env=who).strip()
        gitops.git(clone, "update-ref", "refs/heads/main", root)
        gitops.git(clone, "bundle", "create", str(dest.resolve()), "main")


def _checkout_at(d: Path, case: dict) -> Path:
    """Where a case's checkout goes in `d`: nested as a task's worktree is when a sibling is two
    or more levels up, so that its link lands in `d` (#261, the audit of 1.2)."""
    work = context.tree(d, _checkout(case), case.get("siblings"), "repo")
    work.parent.mkdir(parents=True, exist_ok=True)
    return work


def _clone_base(bundle_path: Path, dest: Path, repo: Path) -> None:
    """A bundle's main checked out at `dest`, its Git LFS files as data from `repo`'s own LFS
    objects (gitops.share_lfs), which no bundle carries: a plain clone of one checked out pointer
    files where the tests wanted the data (#509, the audit of 1.90). RuntimeError naming LFS
    when they can't be had."""
    gitops.git(dest.parent, "clone", "-q", "--no-checkout", "-b", "main", str(bundle_path), str(dest))
    if repo.is_dir():  # its line endings, as the task's worktree had them (#736)
        verify.checkout_settings(repo, dest)
    lfs = bool(gitops.git(dest, "grep", "-l", "-e", "filter=lfs", "main", "--",
                          ":(glob)**/.gitattributes", check=False).strip())
    if lfs and not repo.is_dir():
        raise RuntimeError(f"the case's base has Git LFS files, which come from {repo}, and it is "
                           "gone: it can't be checked out as the task had it")
    if lfs:
        gitops.share_lfs(repo, dest)
    try:
        gitops.git(dest, "reset", "-q", "--hard")
    except gitops.GitError as e:
        _filtered(repo, dest, e)
        if not lfs:
            raise
        raise RuntimeError(f"the case's Git LFS files can't be checked out from {repo}'s LFS "
                           f"objects (git lfs fetch there?): {str(e)[-300:]}") from None
    _filtered(repo, dest)


def _filtered(repo: Path, work: Path, e: gitops.GitError | None = None) -> None:
    """RuntimeError naming the repo's own filter when `work`'s checkout isn't the bytes the task
    had: the checkout's failure in a `required` one (verify._filter_failed), else a filtered file
    whose bytes aren't what the repo itself makes of them (verify.smudge_gap). Bench's clones took
    the repo's filters (#932) but not verify's handling of them: git-crypt's keys are in the git
    dir, so bench add died on a raw "smudge filter git-crypt failed" naming no setting, each
    replay of such a case was an error row saying the same, and a filter that only falls back
    to the stored bytes graded replays on other bytes without a word (#932's review). Nothing
    when the repo is gone: its filters weren't copied then."""
    if not repo.is_dir():
        return
    if e is not None:
        if why := verify._filter_failed(repo, e):
            raise RuntimeError(why) from None
        return
    if why := verify.smudge_gap(repo, work):
        raise RuntimeError(why)


def _filled(repo: Path, commit: str, work: Path, where: str) -> None:
    """`commit`'s submodules filled in a scratch checkout from the user's checkout of each, as
    verify's are (gitops.fill_submodules): empty, a C/C++ or vendored build failed there and add
    blamed the case (#509). RuntimeError naming the ones it couldn't fill."""
    if left := gitops.fill_submodules(repo, commit, work):
        raise RuntimeError(f"the submodules {', '.join(left[:5])} can't be filled in {where} from "
                           f"{repo}, whose checkout lacks them or their commits (git submodule "
                           "update --init --recursive there): the checks would fail for that")


def _submodule_repos(src: Path, commit: str, dest: Path) -> list[str]:
    """Each submodule of `commit` made a repository of its own in the checkout `dest`, at the
    commit `commit` records: its objects borrowed from the user's checkout of it (alternates),
    no ref but a detached HEAD, so no later commit of it is in sight. wb start and the scratch
    checkouts then fill a replay's worktree from it as they fill a task's from the user's
    checkout (gitops.fill_submodules): the bundle carries no submodule, and a replay's stayed
    empty (#509, the audit of 1.90). The ones it couldn't make come back by their whole path."""
    left = []
    for path in gitops.submodules(src, commit):
        row = gitops.git(src, "ls-tree", commit, "--", path, check=False).split()
        sha = row[2] if len(row) >= 3 and row[1] == "commit" else ""
        s, d = src / path, dest / path
        if not sha or not (s / ".git").exists() or gitops.git(
                s, "cat-file", "-t", sha, check=False).strip() != "commit":
            left.append(path)
            continue
        objects = (s / gitops.git(s, "rev-parse", "--git-path", "objects").strip()).resolve()
        d.mkdir(parents=True, exist_ok=True)
        gitops.git(d, "init", "-q")
        (d / ".git" / "objects" / "info" / "alternates").write_text(f"{objects}\n")
        gitops.git(d, "update-ref", "--no-deref", "HEAD", sha)
        gitops.git(d, "reset", "-q", "--hard")
        left += [f"{path}/{p}" for p in _submodule_repos(s, sha, d)]
    return left


def _repo(case: dict) -> Path:
    """The case's repo: case.json `repo`, or once that linked worktree is removed, its
    repository's main one (gitops.repo_of, from `git_common`): read from the removed path, a
    replay lost its line endings, and one with Git LFS files or submodules failed as "gone"
    (#909, the third audit of 2026-11-21)."""
    return gitops.repo_of({"repo": case.get("repo"), "git_common": case.get("git_common")})


def _checkout(case: dict) -> str:
    """The checkout the task ran in, which its siblings are relative to (`../lib`), even once
    that linked worktree is removed: Path.parents is lexical, so they resolve from the path
    alone. Not _repo's main worktree, which sits elsewhere (Claude Code's
    `.claude/worktrees/<name>`, `../proj-wt/feat`): resolved from there, a replay's links named
    the wrong place, its build failed and the failure counted against the model (#909's
    review). case.json `checkout`, or `repo` in a case from before it."""
    return case.get("checkout") or case["repo"]


def checkout(case: dict, dest: Path) -> None:
    """The case's base, checked out at `dest`, with an identity of its own (nothing commits), and
    its Git LFS files and submodules as the task had them, from the case's repo (#509).
    RuntimeError naming what it couldn't reproduce."""
    repo = _repo(case)
    _clone_base(case_dir(case["name"]) / "base.bundle", dest, repo)
    gitops.git(dest, "config", "user.name", "workbench bench")
    gitops.git(dest, "config", "user.email", "bench@workbench.invalid")
    if gitops.submodules(dest, "HEAD"):
        left = _submodule_repos(repo, case["base"], dest) if repo.is_dir() else \
            gitops.submodules(dest, "HEAD")
        if left:
            raise RuntimeError(f"the submodules {', '.join(left[:5])} can't be filled from "
                               f"{repo}: it is gone, or its checkout lacks them or their commits "
                               "(git submodule update --init --recursive there)")


def overlay(case: dict, worktree: Path, src: Path | None = None) -> None:
    """The hidden checks over the replay's work. The driver runs outside the lane and the agent
    could have planted links in its worktree, so nothing is written through one: a link on the
    way (a file or a directory) is replaced. Each keeps its mode in `hidden/`: an executable
    `./test_check.sh` written at 0644 exited 126 on the right answer (#346, the audit of 1.4).
    `src` is the case's directory (`add` overlays from its temporary one)."""
    src = src or case_dir(case["name"])
    for rel in case["hidden"]:
        at = worktree
        for part in Path(rel).parts:
            at = at / part
            if at.is_symlink() or (at.exists() and at != worktree / rel and not at.is_dir()):
                at.unlink()
            elif at == worktree / rel and at.is_dir():
                shutil.rmtree(at)
        (worktree / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src / "hidden" / rel, worktree / rel)
        # the mode too: copyfile over a file the agent wrote keeps that file's (#346)
        os.chmod(worktree / rel, (src / "hidden" / rel).stat().st_mode & 0o777)


class _At(store.Task):
    """A task whose worktree is somewhere else: the original's lane and environment around a
    scratch checkout of its reference (add's check)."""
    def __init__(self, name: str, worktree: Path):
        super().__init__(name)
        self._worktree = worktree

    @property
    def worktree(self) -> Path:
        return self._worktree


def _prepared(at: store.Task, where: str) -> None:
    """A scratch checkout set up as the task's worktree was (runner.prepare); RuntimeError
    naming what it couldn't reproduce."""
    code, said = runner.prepare(at)
    if code != 0:
        what = f"`{at.spec.get('setup')}` exited {code}" if isinstance(code, int) else str(code)
        raise RuntimeError(f"setting up {where} as the task's worktree was failed ({what}): the "
                           f"case can't reproduce its dependencies. {runner.clip_tail(said, 4, total=400)}")


def _inputs(case: dict, repo: Path) -> None:
    """The original checkout's copied paths and siblings, beside a replay's checkout, where its
    wb start --copy and --sibling find them (#250): the copies from the repo as it is now
    (_repo), the siblings from where the task ran (_checkout)."""
    src = _repo(case)
    for p in case.get("copy") or ():
        if os.path.lexists(src / p):
            runner._copy(src / p, repo / p)
    for name in case.get("siblings") or ():  # at any level the case reaches (#261)
        if not os.path.lexists(at := context.sibling_link(repo, name)):
            at.parent.mkdir(parents=True, exist_ok=True)
            at.symlink_to(context.sibling_real(_checkout(case), name))


def add(name: str, step: int | None = None, hidden: list[str] | None = None,
        check: str | None = None, case_name: str | None = None) -> list[str]:
    """Make a case of one step of an accepted task; the report's lines. RuntimeError refuses."""
    task = store.Task(name)
    if not task.exists:
        raise RuntimeError(f"no task named {name!r}")
    spec, st = task.spec, task.status
    if st.get("phase") != "accepted":
        raise RuntimeError(f"{name} is {st.get('phase')}: a case is made from an accepted task")
    if spec.get("trial") or spec.get("bench"):
        raise RuntimeError(f"{name} is a trial run: a check of workbench, not work to replay")
    # the checkout it started from, else its repository's main worktree once that linked one is
    # removed, as accept and discard find it: refused as gone, though restore_steps and
    # step_commit below would have found the commits (#909, the third audit of 2026-11-21)
    repo = gitops.repo_of(spec)
    if not repo.is_dir():
        raise RuntimeError(f"{spec.get('repo')} moved or was deleted: wb relocate {name} --repo "
                           "<its new path>, then try again")
    steps = st.get("steps", 1)
    step = step or steps
    if not 1 <= step <= steps:
        raise RuntimeError(f"{name} had {steps} step(s)")
    case_name = case_name or (name if steps == 1 else f"{name}-s{step}")
    if not store.NAME_RE.match(case_name):
        raise RuntimeError("a case name is lowercase letters, digits and hyphens, at most 48")
    if case_dir(case_name).exists():
        raise RuntimeError(f"case {case_name} exists (wb bench list)")
    gitops.restore_steps(task)  # from the task's own bundle, once git's gc pruned them (#799)
    ref = gitops.step_commit(task, step, steps)
    if not ref:
        raise RuntimeError(f"no commit of step {step} on {spec.get('branch')} in {repo}: the branch "
                           "was deleted or rewritten")
    base = gitops.git(repo, "rev-parse", f"{ref}^").strip()
    prompt = _step_prompt(task, step)
    if prompt is None:
        raise RuntimeError(f"step {step}'s prompt is gone from {task.dir / 'rounds'}")
    if str(step) in (recorded := st.get("step_tests") or {}):  # as data since #941
        test_cmd = recorded[str(step)]
    else:
        test_cmd = (m := TEST_LINE.search(prompt)) and m.group(1)
    if not test_cmd:
        raise RuntimeError(f"step {step} had no acceptance command: nothing to grade a replay by")
    # NUL-separated, as verify's: a name with a tab or a quote came out C-quoted (#942)
    changed = [p for _, p in gitops.name_status(repo, base, ref, "--diff-filter=AM")]
    # the tests and the data they read (Go's testdata/, fixtures/, golden files, Jest snapshots),
    # verify's one pattern: a golden file left out of hidden/ failed the base for lack of it and
    # passed a replay only when the agent wrote it (#500, the audit of 1.90)
    picked = hidden if hidden is not None else [p for p in changed if verify.looks_like_test(p)]
    for p in picked:
        if not _safe(p):
            raise RuntimeError(f"{p}: a hidden check is a path inside the repo")
        if gitops.git(repo, "cat-file", "-t", f"{ref}:{p}", check=False).strip() != "blob":
            raise RuntimeError(f"{p} isn't a file at step {step}'s commit")
    if not picked and not check:
        raise RuntimeError(f"step {step} changed no test files ({len(changed)} files): name the "
                           "hidden checks with --hidden <path>…, or --check <command>")
    d = case_dir(case_name)
    tmp = d.with_name(f".{case_name}.{os.getpid()}.tmp")  # the pid: wb gc sweeps a killed one's
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    try:
        for p in picked:
            (tmp / "hidden" / p).parent.mkdir(parents=True, exist_ok=True)
            (tmp / "hidden" / p).write_bytes(gitops.git_bytes(repo, "show", f"{ref}:{p}"))
            # with its Git mode, which overlay copies: 100755 is executable (#346, the audit of 1.4)
            mode = gitops.git(repo, "ls-tree", ref, "--", p).split(" ", 1)[0]
            (tmp / "hidden" / p).chmod(0o755 if mode == "100755" else 0o644)
        bundle(repo, base, tmp / "base.bundle")
        (tmp / "spec.md").write_text(_spec_of(prompt), encoding="utf-8")
        (tmp / "reference.patch").write_bytes(gitops.git_bytes(repo, "diff", base, ref))
        row = next((r for r in reversed(store.results(trials=True)) if r.get("task") == name), {})
        measured = row.get("steps") or []
        step_row = measured[step - 1] if len(measured) >= step else {}
        grants = context.grants(task)
        case = {"name": case_name, "task": name, "step": step, "repo": str(repo),
                "checkout": str(spec.get("repo") or repo),  # its siblings' (_checkout)
                **({"git_common": str(c)} if (c := gitops.common_dir(repo)) else {}),  # _repo
                "base": base,
                "reference": ref, "size": spec.get("size"), "stack": spec.get("stack"),
                "test_cmd": test_cmd, "test_timeout": spec.get("test_timeout") or 900,
                "check_cmd": check, "hidden": picked, "allow": grants.get("allow") or [],
                "paths": grants.get("paths") or [], "skills": st.get("skills_ever") or [],
                # the worktree's setup (#201), so the reference, the base and each replay are the
                # project as the task had it: a task whose --setup made an ignored stamp had its
                # own commit rejected, the scratch clone without it (#250, the audit of 1.2)
                "setup": spec.get("setup") or "", "copy": spec.get("copy") or [],
                "siblings": spec.get("siblings") or [],
                # its setup's limit and its tests' GPU: a replay without them stopped a 90-minute
                # build at 1800 s, failed as an error and was retried by every run, never graded
                # (#735, the 2026-11-21 audit)
                "setup_timeout": spec.get("setup_timeout"),
                "unload_for_tests": bool(spec.get("unload_for_tests")),
                # the package the task ran in (#375), where each replay runs, sets up and is
                # graded: from the root, an answer written there passed a package's check (#425)
                "subdir": spec.get("subdir") or "",
                "original": {"model": spec.get("model"), "rounds": row.get("rounds"),
                             "reviews": step_row.get("reviews", row.get("reviews")),
                             "minutes": row.get("minutes"), "steps": steps},
                "created_at": store.now()}
        # the hidden checks must fail on the base, or a replay that does nothing passes. Run as
        # the reference check and a replay's grade are, in the agent's lane: in a plain shell
        # with the caller's environment the same test saw other files, other variables and no
        # cleanup (#81, the 2026-11-18 audit)
        home().mkdir(parents=True, exist_ok=True)  # not in /tmp, where the git shim allows writes
        with tempfile.TemporaryDirectory(prefix=store.scratch(".base-"), dir=home()) as scratch:
            work = _checkout_at(Path(scratch), case)
            _clone_base(tmp / "base.bundle", work, repo)  # its LFS files and submodules (#509)
            _filled(repo, base, work, "the base")
            overlay(case, work, tmp)  # as a replay gets them (#346)
            at = _At(name, work)
            _prepared(at, "the base")
            codes = [runner.run_in_lane(at, c, BASE_CHECK_SECS)[0]
                     for c in (case["test_cmd"], check) if c]
        if all(c == 0 for c in codes):
            raise RuntimeError("the hidden checks already pass on the base: a replay that changed "
                               "nothing would pass. Name others (--hidden, --check)")
        case["base_check"] = "not checked (timed out)" if "timeout" in codes else "fails"
        # and pass on the step's own commit, run as a replay's grade is (the lane, read-only
        # git): workbench's suite commits, so clean-fixtures failed every replay (2026-11-17)
        home().mkdir(parents=True, exist_ok=True)  # not in /tmp, where the git shim allows writes
        with tempfile.TemporaryDirectory(prefix=store.scratch(".check-"), dir=home()) as scratch:
            work = _checkout_at(Path(scratch), case)
            gitops.scratch_clone(repo, work)  # a shallow repo's too (#884)
            # the repo's core.autocrlf and info/attributes: a clone has none of its local config,
            # so the reference check read LF where the task read CRLF and refused the case, and
            # each replay was graded on other bytes than the task saw (#736, the 2026-11-21 audit)
            verify.checkout_settings(repo, work)
            gitops.share_lfs(repo, work)  # its LFS files as data, as verify's (#382, #509)
            if sparse := gitops.sparse_of(Path(repo)):  # as the task's worktree was (#263)
                gitops.apply_sparse(work, sparse)
            try:
                gitops.git(work, "checkout", "-q", "--detach", ref)
            except gitops.GitError as e:
                _filtered(repo, work, e)  # a required filter that can't work here, named (#932)
                raise
            _filtered(repo, work)  # or one that fell back to the stored bytes
            _filled(repo, ref, work, f"step {step}'s own commit")
            # the hidden checks overlaid as on a replay: graded straight from the commit, a check
            # the overlay broke (its execute bit lost) still let the case in (#346, the audit of 1.4)
            overlay(case, work, tmp)
            at = _At(name, work)
            _prepared(at, f"step {step}'s own commit")
            for c in (case["test_cmd"], check):
                code = c and runner.run_in_lane(at, c, case["test_timeout"])[0]
                if c and code != 0:
                    raise RuntimeError(
                        f"`{c}` fails on step {step}'s own commit (exit {code}) when run as a "
                        "replay is graded, in the agent's lane: no replay could pass it")
        store.write_json(tmp / "case.json", case)
        os.replace(tmp, d)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return [f"case {case_name}: step {step} of {name}, base {base[:8]}, {spec.get('size')}",
            f"acceptance: `{case['test_cmd']}`" + (f", then `{check}`" if check else ""),
            "hidden checks: " + (", ".join(picked) if picked else "none (the --check command)"),
            f"on the base: {case['base_check']}",
            "a case is worth keeping when the spec pinned what the hidden tests call; "
            f"wb bench run --case {case_name} --model <id>"]


# --- running ------------------------------------------------------------------------------------

def wb_commit() -> str:
    return gitops.git(PKG_ROOT, "rev-parse", "--short", "HEAD", check=False).strip()


def driver() -> dict:
    """The driver running now ({pid, run, start, machine, host}), or {}. `start` is when it
    started (#511): a recycled pid of ours that runs the bench again isn't it. Another machine's
    (on a state directory several share) lives while its lock file beats (runner.BEAT_SECS): on
    this one its pid was dead, so gc here discarded its live replays and removed their clones, and
    wb bench start started a second driver beside it (#876, Claude's audit of 1.90.24)."""
    d = store.read_json(home() / "driver.json", {}) or {}
    if d.get("machine") not in (None, store.machine()):
        from . import cli  # cli imports this module
        try:
            beat = (home() / "driver.lock").stat().st_mtime
        except OSError:
            return {}
        return d if time.time() - beat < cli.BEAT_DEAD else {}
    pid = d.get("pid")
    if not isinstance(pid, int) or pid <= 0 or not models._alive(pid, d.get("start")):
        return {}
    return d if models.runs(pid, "-m", "workbench.bench") else {}  # a recycled pid runs something else


def sweep() -> list[str]:
    """What a driver killed without warning (SIGKILL, the OOM killer, a hard reset) left: its
    replay, not closed, and its clone. Only `replay`'s finally closed them, so each such run
    left a task open for good and a full clone with its copied dependencies under
    bench/runs/, and a runner that outlived its driver replayed beside the next run's (#511,
    the audit of 1.90). Each replay not closed whose run's driver is gone is discarded (a live
    runner stopped first) and each such run's clones removed; `wb gc` and `start` call it. The
    replays' names."""
    from . import cli  # cli imports this module
    live = driver().get("run")
    out = []
    for t in store.all_tasks():
        b = t.spec.get("bench")
        if not isinstance(b, dict) or b.get("run") == live:
            continue
        if store.elsewhere(t.status) and cli.runner_alive(t):
            continue  # another machine's replay, its runner still beating there (#876)
        if t.status.get("phase") not in store.CLOSED:
            with open(os.devnull, "w") as quiet, redirect_stdout(quiet):
                try:
                    cli.cmd_discard(SimpleNamespace(name=t.name, reason="bench: its driver died",
                                                    spec_gap=None, keep_branch=False))
                except SystemExit:
                    pass
            if t.status.get("phase") not in store.CLOSED:
                continue  # the discard refused: its clone is still in use (#876)
            out.append(t.name)
        d = home() / "runs" / str(b.get("run")) / t.name
        for left in (d / "repo", d / "tree"):  # tree/ for a sibling further up (_checkout_at)
            if left.exists():
                shutil.rmtree(left, ignore_errors=True)
                out += [t.name] if t.name not in out else []
    return out


def stop_driver() -> int | None:
    d = driver()
    if not d:
        return None
    if d.get("machine") not in (None, store.machine()):  # its pid here is another process (#876)
        raise RuntimeError(f"bench run {d.get('run')} runs on {d.get('host') or d['machine']}, "
                           "which shares this state directory: stop it there")
    try:
        os.kill(d["pid"], signal.SIGTERM)  # it stops its replay's runner and closes the task
    except ProcessLookupError:
        return None
    from .watch import wait_exit
    wait_exit(d["pid"], 30)
    return d["pid"]


def plan(run: str) -> dict:
    return store.read_json(home() / "runs" / run / "plan.json", {}) or {}


def start(model_ids: list[str], case_names: list[str], skills: list[str], label: str,
          repeat: int) -> tuple[str | None, list[str]]:
    """Write a run's plan and spawn its driver; the run id (None: nothing left to replay) and
    the report's lines."""
    if d := driver():
        raise RuntimeError(f"bench run {d.get('run')} is running (wb bench stop)")
    sweep()  # a killed driver's replay, its runner perhaps still going (#511)
    from . import cli  # cli imports this module
    if alive := [t.name for t in store.all_tasks() if t.spec.get("bench") and cli.runner_alive(t)]:
        raise RuntimeError(f"replay {alive[0]}'s runner still runs: wb discard {alive[0]}")
    have = cases()
    if not have:
        raise RuntimeError("no cases yet: wb bench add <accepted task>")
    unknown = [c for c in case_names if c not in have]
    if unknown:
        raise RuntimeError(f"no case {', '.join(unknown)} (wb bench list)")
    chosen = {n: have[n] for n in case_names} if case_names else have
    found = models.discover()
    if model_ids:
        try:
            ms = [models.resolve(x) for x in model_ids]
        except KeyError as e:  # a typo or a retired id: its one line, not a traceback (#737)
            raise RuntimeError(e.args[0]) from None
        if api := [m.id for m in ms if getattr(m, "kind", "gpu") == "api"]:  # replays take the GPU lease (#1013)
            raise RuntimeError(f"{api[0]} is an API model: wb bench replays on GPU models only")
    else:
        ms = [m for m in found.values() if models.status(m)["available"]]
    if not ms:
        raise RuntimeError("no model that can run (wb models)")
    pairs = [(c, m.id) for c in chosen for m in ms
             if model_ids or router.allowed(m, chosen[c].get("size") or "significant")]
    done = done_keys()
    if pairs and all((c, m, label, rep) in done for c, m in pairs for rep in range(1, repeat + 1)):
        # a driver that skips them all said "N replay(s)" and did nothing (#508)
        return None, [f"all {len(pairs) * repeat} replay(s) already done"
                      + (f" under label {label}" if label else " (no --label)")
                      + ": --label <name> runs them again"]
    prune_runs()  # #740
    run = stamp = time.strftime("%m%d-%H%M%S")
    for n in range(2, 100):  # two runs in one second: the second's mkdir failed (#511's test)
        if not (home() / "runs" / run).exists():
            break
        run = f"{stamp}-{n}"
    d = home() / "runs" / run
    d.mkdir(parents=True)
    store.write_json(d / "plan.json", {"run": run, "label": label or "", "pairs": pairs,
                                       "skills": skills, "repeat": repeat, "created_at": store.now()})
    env = context.detached_env()  # its replays' agents get the user's PYTHONPATH (#512)
    env.pop("WB_TASK", None)
    env.pop("CLAUDE_CODE_SESSION_ID", None)  # its tasks are no session's
    env.pop("CLAUDE_PID", None)
    from . import cli  # cli imports this module
    with open(d / "driver.log", "a") as log:  # its own scope, to outlive a logout (#531)
        proc = cli.detach([sys.executable, "-X", "utf8", "-m", "workbench.bench", run], cwd=d,
                          awake=f"wb bench run {run}", env=env, stdout=log, stderr=log)  # #516
    store.write_json(home() / "driver.json", {"pid": proc.pid, "run": run,
                                              "start": models._started(proc.pid),
                                              "machine": store.machine(), "host": store.host()})
    return run, [f"bench run {run}: {len(pairs)} replay(s) × {repeat}"
                 + (f", label {label}" if label else "") + ", one at a time; real tasks go first",
                 f"follow: wb bench report --run {run}   stop: wb bench stop"]


def _done(row: dict) -> bool:
    return row.get("outcome") in ("passed", "failed")


def done_keys() -> set[tuple]:
    """(case, model, label, rep) of every replay graded: an error, the machine's, isn't (#508)."""
    return {(r.get("case"), r.get("model"), r.get("label", ""), r.get("rep", 1))
            for r in store.read_jsonl(runs_file())[0] if _done(r)}


def _idle() -> bool:
    """No real task runs that the GPU's replays would wait for: an API task takes no GPU, so an
    overnight run doesn't wait for one (#1015)."""
    return not any(t.status.get("phase") in store.ACTIVE and not t.spec.get("bench")
                   and not api_task(t) for t in store.all_tasks())


def api_task(t: store.Task) -> bool:
    """Whether a task runs an API model: task.json `via` "api" (how wb start records a role's
    route, #1016), else its model as discovery has it (hosted.json as it is, never read again)."""
    if t.spec.get("via"):
        return t.spec["via"] == "api"
    mid = t.spec.get("model") or ""
    if models.gpu_side(mid):
        return False
    m = models.lookup(mid, cached=True)
    return bool(m and m.kind == "api")


CURRENT: dict = {}


def _on_term(signum, frame):
    raise SystemExit(1)


def drive(run: str) -> None:
    p = plan(run)
    label, reps = p.get("label", ""), int(p.get("repeat") or 1)
    for rep in range(1, reps + 1):
        for case_name, model in p.get("pairs") or []:
            if (case_name, model, label, rep) in done_keys():
                continue
            case = cases().get(case_name)
            if not case:
                continue
            while True:
                with Watcher(store.tasks_dir()) as watcher:
                    while not _idle():  # a real task runs, or waits for the GPU: after it
                        watcher.wait(60.0)
                row = replay(case, model, rep, run, label, p.get("skills") or [])
                store.append_jsonl(runs_file(), row)
                if row["outcome"] != "preempted":
                    break


def subdir(case: dict) -> str | None:
    """The package a case's task ran in ('' for the repo's top), None when that can't be known:
    a case made before it was kept (#425, the audit of 1.8) reads it from its task, while the
    task lasts."""
    if "subdir" in case:
        return case["subdir"] or ""
    task = store.Task(case.get("task") or "")
    return (task.spec.get("subdir") or "") if case.get("task") and task.exists else None


def _kept(case: dict, key: str):
    """A task setting the case keeps; one made before it was kept (#735) reads it from its task,
    while the task lasts, as ``subdir`` does."""
    if key in case:
        return case[key]
    task = store.Task(case.get("task") or "")
    return task.spec.get(key) if case.get("task") and task.exists else None


def _start_task(case: dict, model: str, name: str, repo: Path, skills: list[str], meta: dict) -> None:
    from . import cli  # cli imports this module
    # from the package, so wb start finds it as the task's start did (cli._package, #425)
    argv = ["start", name, "--spec", str(case_dir(case["name"]) / "spec.md"),
            "--repo", str(repo / (subdir(case) or "")),
            "--size", case.get("size") or "significant", "--model", model, "--test", case["test_cmd"],
            "--test-timeout", str(case.get("test_timeout") or 900), "--ask-budget", "0",
            "--checkin", "0", "--queue", "--trial", "--no-claude-md"]
    for s in skills:
        argv += ["--skill", s]
    for pat in case.get("allow") or []:
        argv += ["--allow", pat]
    if case.get("setup"):
        argv += ["--setup", case["setup"]]
        if isinstance(secs := _kept(case, "setup_timeout"), int) and secs > 0:  # #735
            argv += ["--setup-timeout", str(secs)]
    if _kept(case, "unload_for_tests"):
        argv += ["--unload-for-tests"]
    for p in case.get("copy") or []:
        argv += ["--copy", p]
    for s in case.get("siblings") or []:
        argv += ["--sibling", s]
    for path in case.get("paths") or []:
        argv += ["--allow-path", path]
    a = cli.parser().parse_args(argv)
    a.bench = meta
    a.f(a)


def replay(case: dict, model: str, rep: int, run: str, label: str, skills: list[str]) -> dict:
    """One case on one model: start, wait, grade, close. Its runs.jsonl row."""
    from . import cli
    stamp = f"{int(time.time() * 1000) % 10 ** 9:09d}"
    name = f"bench-{case['name'][:30].rstrip('-')}-{stamp}"
    row = {"ts": store.now(), "run": run, "label": label, "rep": rep, "case": case["name"],
           "model": model, "task": name, "wb_commit": wb_commit(), "skills": skills}
    if (sub := subdir(case)) is None:  # graded from the root, a package's case can pass wrongly
        return {**row, "outcome": "error", "why": f"case {case['name']} predates the package path "
                f"cases keep, and its task {case.get('task')} is gone: where it ran is unknown "
                "(#425); make it again from a task"}
    d = home() / "runs" / run / name
    d.mkdir(parents=True)
    began = time.time()
    log = open(d / "wb.log", "a")
    work = _checkout_at(d, case)
    try:
        try:
            checkout(case, work)
        except RuntimeError as e:  # its LFS, submodules (#509), filters (#932): the next run retries
            return {**row, "outcome": "error", "why": str(e)[:300]}
        _inputs(case, work)
        with redirect_stdout(log):
            try:
                _start_task(case, model, name, work, skills,
                            {"case": case["name"], "run": run, "label": label})
            except SystemExit:
                pass
        task = store.Task(name)
        if not task.exists:
            return {**row, "outcome": "error", "why": "wb start refused"}
        CURRENT["task"] = task
        if (task.spec.get("subdir") or "") != sub:  # its package's CLAUDE.md gone from the base
            return {**row, "outcome": "error", "why": f"wb start didn't take {sub or 'the top'} as "
                    "the package the case's task ran in"}
        st, asks_refused = _wait(task)
        phase = st.get("phase")
        row.update(phase=phase, rounds=st.get("round", 1),
                   auto_fixes=st.get("auto_fixes_total", 0) + st.get("auto_fixes", 0),
                   tok_in=st.get("tok_in", 0), tok_out=st.get("tok_out", 0),
                   minutes=round((time.time() - began) / 60, 1), asks_refused=asks_refused,
                   visible_ok=(st.get("tests") or {}).get("ok"),
                   handovers=sum(e.get("kind") == "fresh" for e in
                                 store.read_jsonl(task.dir / "events.jsonl")[0]),
                   # the arm it ran in and what its agent used of it (#857): an A/B reads both
                   experiments=sorted(k for k, v in (task.spec.get("experiments") or {}).items() if v),
                   experiment_use=st.get("experiment_use") or {}, long_reads=st.get("long_reads", 0),
                   # "on" with probe on: once its level, moderate or aggressive (the owner,
                   # 2026-11-22, #857: off and on, on is large files only)
                   **({"probe_level": "on"} if context.experiment(task, "probe") else {}),
                   **paid(task))
        if st.get("last") == PREEMPTED:
            return {**row, "outcome": "preempted"}
        # the setup, the server, the runner, the disk; or an API provider's or the spend cap's
        # stop, `stuck` (#1020, #1019)
        if st.get("infra") and (phase == "failed" or st["infra"] in ("provider", "spend")):
            # the machine's doing, not the model's: an error, run again next time (#508)
            return {**row, "outcome": "error", "why": f"{st['infra']}: {st.get('last') or ''}"[:200]}
        if phase != "review":
            return {**row, "outcome": "failed", "why": (st.get("last") or phase)[:200]}
        (d / "diff.patch").write_text(gitops.diff(task.worktree, task.spec["base"]))
        overlay(case, task.worktree)
        out, ok = [], True
        for cmd in (case["test_cmd"], case.get("check_cmd")):
            if cmd:
                code, text = runner.run_in_lane(task, cmd, int(case.get("test_timeout") or 900))
                out.append(f"$ {cmd}\nexit: {code}\n\n{text}")
                ok = ok and code == 0
        (d / "grade.txt").write_text("\n\n".join(out))
        return {**row, "outcome": "passed" if ok else "failed",
                **({} if ok else {"why": "the hidden checks failed (grade.txt)"})}
    finally:
        log.close()
        task = CURRENT.pop("task", None)
        if task is not None and task.status.get("phase") not in store.CLOSED:
            with open(os.devnull, "w") as quiet, redirect_stdout(quiet):
                try:
                    cli.cmd_discard(SimpleNamespace(name=task.name, reason="bench", spec_gap=None,
                                                    keep_branch=False))
                except SystemExit:
                    pass
        shutil.rmtree(d / "tree" if work.parent != d else work, ignore_errors=True)


def paid(task: store.Task) -> dict:
    """A replay on an API model: its runs.jsonl row's `kind` and `paid` {cost, source}
    (results.api_usage; a replay writes no results row, so its money is said here, #1022)."""
    if (u := results.api_usage(task)) is None:
        return {}
    return {"kind": "api", "paid": {"cost": u["cost"], "source": u["source"]}}


def paid_since(since: float = 0) -> dict:
    """The API replays' money since ``since``, for `wb savings --api`: {tasks, cost, unknown}."""
    rs = [r for r in store.read_jsonl(runs_file())[0] if isinstance(r, dict) and results.api_row(r)
          and (r.get("ts") or 0) >= since]
    known = [c for r in rs if (c := results.row_cost(r)) is not None]
    return {"tasks": len(rs), "cost": round(sum(known), 6), "unknown": len(rs) - len(known)}


def _wait(task: store.Task) -> tuple[dict, int]:
    """Until the replay needs Claude: refuse what it asks for (no one is there to grant it)."""
    from . import cli
    refused = 0
    with Watcher(task.dir) as watcher:
        while True:
            st = task.status
            phase = st.get("phase")
            if phase in cli.LIVE:
                cli.reap(task)
            if phase == "asking" and (qid := st.get("asking")) and ask_mcp.is_open(task, qid):
                refused += ask_mcp.answer_once(task, qid, REFUSED)  # not if closed meanwhile (#938)
            elif phase in ("review", "stuck", "failed") and not cli.runner_alive(task):
                return st, refused
            watcher.wait(30.0)


# --- Claude writing it (#118): the other side of the handoff's cost ------------------------------

SELF_TURNS = 40  # claude -p's turn cap for one case
SELF_PROMPT = ("{spec}\n\nThe acceptance command is `{test}`: run it before you finish, and finish "
               "only when it passes. Change only what the task needs.")


def _self_result(out) -> tuple[dict, int | None]:
    """claude -p's JSON result, and its API calls when they can be counted. Claude Code 2.1.311
    prints every event as a list ending in the result (the 0.99 runs for #120 died on it); its
    calls are the assistant messages' distinct ids, as spend counts a session's: num_turns
    counts more (13 for 7 calls), one per tool call made in parallel."""
    if not isinstance(out, list):
        return out, None
    result = next((e for e in reversed(out) if isinstance(e, dict) and e.get("type") == "result"), {})
    ids = {(e.get("message") or {}).get("id") for e in out
           if isinstance(e, dict) and e.get("type") == "assistant"} - {None}
    return result, len(ids) or None


def _self_usage(result: dict, calls: int | None = None) -> dict:
    """claude -p's JSON usage in spend's shape, so spend.weighted prices it as it prices a handoff."""
    u = result.get("usage") or {}
    made = u.get("cache_creation") or {}
    one_h = made.get("ephemeral_1h_input_tokens") or 0
    return {"turns": result.get("num_turns") or 0, "calls": calls or result.get("num_turns") or 0,
            "input": u.get("input_tokens") or 0,
            "cache_write_5m": made.get("ephemeral_5m_input_tokens",
                                       (u.get("cache_creation_input_tokens") or 0) - one_h),
            "cache_write_1h": one_h, "cache_read": u.get("cache_read_input_tokens") or 0,
            "output": u.get("output_tokens") or 0}


def handoff_cost(case: dict) -> tuple[int | None, int]:
    """What handing the case's step of its original task off cost Claude, and how many steps
    that figure covers: 1 when it is the step's own (its results row's `steps`, or the row's
    `claude` for a task of one step), the task's step count when only the whole task's is
    known (a row written before #558), which can't be set against Claude writing one step."""
    row = next((r for r in reversed(store.results(trials=True)) if r.get("task") == case.get("task")), {})
    steps = row.get("steps") or []
    n = max(len(steps), (case.get("original") or {}).get("steps") or 1)
    step = case.get("step") or 1
    if n > 1:
        own = steps[step - 1].get("claude") if len(steps) >= step else None
        if isinstance(own, dict):
            return spend.weighted(own), 1
        return (spend.weighted(row["claude"]) if row.get("claude") else None), n
    return (spend.weighted(row["claude"]) if row.get("claude") else None), 1


def self_run(case_name: str, turns: int = SELF_TURNS) -> dict:
    """One case written by Claude (`claude -p`) in a scratch checkout of its base, graded by its
    hidden checks in the original task's lane, as a replay is; its runs.jsonl row (arm: claude)."""
    case = cases().get(case_name)
    if not case:
        raise RuntimeError(f"no case named {case_name!r} (wb bench list)")
    original = store.Task(case["task"])
    if not original.exists:
        raise RuntimeError(f"{case['task']}, the task {case_name} was made from, is gone: its lane "
                           "is what grades a run, so this case can't be graded as replays are")
    claude = shutil.which("claude")
    if not claude:
        raise RuntimeError("claude (Claude Code) isn't on PATH")
    stamp = f"{int(time.time() * 1000) % 10 ** 9:09d}"
    run, name = f"self-{stamp}", f"self-{case_name[:30]}-{stamp}"
    d = home() / "runs" / run / name
    d.mkdir(parents=True)
    row = {"ts": store.now(), "run": run, "label": "claude", "rep": 1, "case": case_name,
           "model": "claude", "arm": "claude", "task": name, "wb_commit": wb_commit()}
    began = time.time()
    work = _checkout_at(d, case)
    try:
        checkout(case, work)
        code, said = runner.prepare(_At(original.name, work))  # as the replays are (#250)
        if code != 0:
            return {**row, "outcome": "error", "why": f"the setup failed ({code}): {said.strip()[-200:]}"}
        prompt = SELF_PROMPT.format(spec=(case_dir(case_name) / "spec.md").read_text(),
                                    test=case["test_cmd"])
        # edits and the acceptance command only: nothing else is allowed without someone to ask.
        # Claude Code checks each simple command of a compound line on its own, so each gets a rule
        allow = [f"Bash({c.strip()}:*)" for c in re.split(r"&&|\|\||;", case["test_cmd"]) if c.strip()]
        argv = [claude, "-p", "--output-format", "json", "--max-turns", str(turns),
                "--permission-mode", "acceptEdits", "--allowedTools", *allow, "--", prompt]
        try:
            # its own group, killed with wb (#589): a Bash-limit kill left claude -p running
            p = media.run_group(argv, int(case.get("test_timeout") or 900) * 4, cwd=work / (subdir(case) or ""),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        except subprocess.TimeoutExpired:
            return {**row, "outcome": "error", "why": "claude -p timed out"}
        (d / "claude.json").write_text(p.stdout)
        try:
            result = json.loads(p.stdout)
        except ValueError:
            return {**row, "outcome": "error", "why": f"claude -p exited {p.returncode} without a "
                    f"JSON result: {(p.stderr or p.stdout).strip()[:200]}"}
        result, calls = _self_result(result)
        usage = _self_usage(result, calls)
        row.update(claude=usage, minutes=round((time.time() - began) / 60, 1),
                   turns=usage["turns"], claude_model=next(iter(result.get("modelUsage") or {}), None))
        (d / "diff.patch").write_text(gitops.diff(work, "HEAD"))
        overlay(case, work)
        out, ok = [], True
        for cmd in (case["test_cmd"], case.get("check_cmd")):
            if cmd:
                code, text = runner.run_in_lane(_At(original.name, work), cmd,
                                                int(case.get("test_timeout") or 900))
                out.append(f"$ {cmd}\nexit: {code}\n\n{text}")
                ok = ok and code == 0
        (d / "grade.txt").write_text("\n\n".join(out))
        return {**row, "outcome": "passed" if ok else "failed",
                **({} if ok else {"why": "the hidden checks failed (grade.txt)"})}
    except SystemExit:  # ended (Claude's Bash limit): a row says so, not nothing (#589)
        store.append_jsonl(runs_file(), {**row, "outcome": "error",
                                         "why": "stopped before it finished (a signal)"})
        raise
    finally:
        shutil.rmtree(d / "tree" if work.parent != d else work, ignore_errors=True)


def self_lines(rs: list[dict]) -> list[str]:
    """Claude writing each case against handing it off: weighted tokens, and the ratio."""
    if not rs:
        return []
    out = ["", "Claude writing it (wb bench self), against the handoff's cost to Claude:"]
    have = cases()
    for r in rs:
        mine = spend.weighted(r["claude"]) if r.get("claude") else None
        hand, covers = handoff_cost(have.get(r["case"]) or {"task": None})
        # a whole task's cost over one step's writing: 240K over 90K read 2.67 when step 3's
        # own share was about 80K (#558, Claude's audit of 1.90)
        ratio = f", handoff/self {hand / mine:.2f}" if hand and mine and covers == 1 else ""
        out.append(f"{r['case']}: {'PASS' if r['outcome'] == 'passed' else 'fail'}, self "
                   + (f"~{mine / 1000:.0f}K" if mine else "?") + ", handoff "
                   + (f"~{hand / 1000:.0f}K" + (f" (all {covers} steps; not comparable)"
                                                 if covers > 1 else "")
                      if hand else "not measured") + ratio)
    return out


# --- reading the results ------------------------------------------------------------------------

def rows(run: str | None = None, label: str | None = None) -> list[dict]:
    return [r for r in store.read_jsonl(runs_file())[0]
            if (run is None or r.get("run") == run) and (label is None or r.get("label", "") == label)
            and _done(r)]


def report(run: str | None = None, label: str | None = None) -> str:
    rs = rows(run, label)
    mine = [r for r in rs if r.get("arm") == "claude"]
    rs = [r for r in rs if r.get("arm") != "claude"]
    every = store.read_jsonl(runs_file())[0]
    other = [r for r in every if (run is None or r.get("run") == run) and not _done(r)]
    head = []
    if d := driver():
        p = plan(d["run"])
        n = len(p.get("pairs") or []) * int(p.get("repeat") or 1)
        head.append(f"running: {d['run']}, {len([r for r in every if r.get('run') == d['run'] and _done(r)])}"
                    f" of {n} replayed")
    # why a replay never started, and where its log is: a run whose every replay failed to start
    # said only "no replays yet" (the 2026-11-18 audit, #89)
    errors = [r for r in other if r.get("outcome") == "error"]
    # one that started and broke for the machine's reasons (a `phase`: #508) points at its runner
    unstarted = [f"{r.get('case')} on {r.get('model')}: "
                 + (f"broke off: {r.get('why') or '?'} ({store.task_dir(str(r.get('task'))) / 'runner.log'})"
                    if r.get("phase") else f"didn't start: {r.get('why') or '?'} "
                    f"({home() / 'runs' / str(r.get('run')) / str(r.get('task')) / 'wb.log'})")
                 for r in errors[-10:]]
    if not rs:
        return "\n".join(head + ([f"no replay finished; {len(errors)} didn't start"
                                  + (" or broke off" if any(r.get("phase") for r in errors) else "")
                                  + ":"] + unstarted
                                 if errors else
                                 ["no replays yet" + (" (wb bench add, then wb bench run)"
                                                      if not cases() else "")]) + self_lines(mine))
    by: dict = {}
    for r in rs:
        by.setdefault((r.get("label") or "-", r["model"]), []).append(r)
    # `agent tok` and `paid` (#1022) only with an API replay shown: a GPU-only report reads as
    # before, `local tok` (wave 2's review)
    api = any(results.api_row(r) for r in rs)
    out = head + [f"{'label':12} {'model':34} {'cases':>5} {'passed':>6} {'rounds':>6} "
                  f"{'autofix':>7} {'min':>6} {'agent tok' if api else 'local tok':>10}"
                  + (f" {'paid':>7}" if api else "")]
    for (lab, model), g in sorted(by.items()):
        n = len(g)
        avg = lambda k: sum(r.get(k) or 0 for r in g) / n  # noqa: E731
        out.append(f"{lab[:12]:12} {model[:34]:34} {n:>5} {sum(r['outcome'] == 'passed' for r in g) / n:>6.0%} "
                   f"{avg('rounds'):>6.1f} {avg('auto_fixes'):>7.1f} {avg('minutes'):>6.1f} "
                   f"{int(avg('tok_in') + avg('tok_out')):>10}" + (f" {results.paid_cell(g):>7}" if api else ""))
    out += use_lines(by)
    have = cases()
    out.append("")
    for c in sorted({r["case"] for r in rs}):
        o = (have.get(c) or {}).get("original") or {}
        marks = ", ".join(f"{r['model'].split('/')[-1]} {'PASS' if r['outcome'] == 'passed' else 'fail'}"
                          f" {r.get('rounds', '?')}r {r.get('minutes', '?')}m"
                          for r in rs if r["case"] == c)
        out.append(f"{c}: {marks}  (accepted from {str(o.get('model') or '?').split('/')[-1]}, "
                   f"{o.get('reviews', '?')} review round(s))")
    if len(other) > len(errors):
        out.append(f"({len(other) - len(errors)} replay(s) preempted, left out)")
    out += unstarted + self_lines(mine)
    return "\n".join(out)


def use_lines(by: dict) -> list[str]:
    """Per label and model whose replays had an experiment on: how many of them used it, how
    often, and their whole reads of long files. An A/B whose agent never ran the tool measures
    nothing, and the first probe run learnt it only by searching the events afterwards (#857)."""
    out = []
    for (lab, model), g in sorted(by.items()):
        for name in sorted({x for r in g for x in r.get("experiments") or ()}):
            on = [r for r in g if name in (r.get("experiments") or ())]
            calls = [(r.get("experiment_use") or {}).get(name, 0) for r in on]
            out.append(f"{lab[:12]} {model.split('/')[-1]}: {name} used in {sum(c > 0 for c in calls)} of "
                       f"{len(on)} replay(s), {sum(calls)} call(s); whole reads of long files: "
                       f"{sum(r.get('long_reads') or 0 for r in on)}")
    return out


def model_line(model_id: str) -> str | None:
    """For wb models and wb scout: how a model did on the cases (shown only; the router doesn't read it)."""
    g = [r for r in rows() if r.get("model") == model_id]
    if not g:
        return None
    latest = {}
    for r in g:
        latest[(r["case"], r.get("label") or "")] = r
    passed = sum(r["outcome"] == "passed" for r in latest.values())
    return f"{passed} of {len(latest)} case(s) passed"


def list_lines() -> list[str]:
    have = cases()
    if not have:
        return ["no cases yet: wb bench add <accepted task>"]
    out = []
    for n, c in have.items():
        done = [r for r in rows() if r.get("case") == n]
        out.append(f"{n:30} {c.get('size') or '?':11} {len(c.get('hidden') or [])} hidden"
                   + (" + check" if c.get("check_cmd") else "") + f", from {c['task']}"
                   + (f" step {c['step']}" if (c.get("original") or {}).get("steps", 1) > 1 else "")
                   + f", {len(done)} replay(s)")
    return out


def main() -> None:
    run = sys.argv[1]
    signal.signal(signal.SIGTERM, _on_term)
    store.absolute_env()
    beat = home() / "driver.lock"  # .lock: no wb events wakes for the touch
    beat.touch()
    runner._beat(beat)  # another machine sharing the state directory sees it alive (#876)
    try:
        drive(run)
    finally:
        mine = store.read_json(home() / "driver.json", {}) or {}
        if mine.get("pid") == os.getpid():
            (home() / "driver.json").unlink(missing_ok=True)


if __name__ == "__main__":
    main()
