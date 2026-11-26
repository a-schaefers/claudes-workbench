"""wb patch: a user's own fixes to workbench, kept across updates and offered upstream (#1005).

Claude Code keeps the marketplace install in a clone it owns, and `claude plugin marketplace
update` never pulls it: whenever HEAD differs from the source's head it deletes the clone and
clones it again, one commit deep, so a local commit, an edit, a stash or a branch there is gone
with no backup, even with nothing new upstream (tested on Claude Code 2.1.323, 2026-11-22). So
patches live in a full clone the user owns (`wb patch init`), added to Claude Code as a directory
marketplace, which it never resets: they are commits on branch PATCH_BRANCH tracking upstream,
and `wb update` rebases them onto upstream's news in a scratch worktree, never the live tree
(mid-conflict there, every wb call from the clone died with a SyntaxError).

Every git call here runs without hooks (a hook of the user's ran from wb's own calls once:
an auditor's pre-push hook broke ship.sh, 2026-11-21) and without a terminal, so nothing prompts.
Everything this module needs is imported at the top: `wb patch init` deletes the copy wb runs
from (Claude Code removes the old marketplace clone), and nothing may be imported from it after."""
import ast
import json
import os
import platform
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import NamedTuple

from . import gitops, hardware, ocplugin, spend, store

PATCH_BRANCH = "local"  # the branch a clone install keeps its patches on
BEFORE = "refs/workbench/before-update"  # the tip before wb moved the branch; its reflog, the earlier ones
CARRIED = "Local changes carried over from the marketplace clone"
SLUG = re.compile(r"[a-z0-9][a-z0-9-]*")
# a diff another git applies: the user's prefixes, relative diffs or external tools broke apply
DIFF_PLAIN = ("-c", "diff.noprefix=false", "-c", "diff.mnemonicPrefix=false", "-c", "diff.relative=false",
              "-c", "diff.external=")
# the rebases wb runs: no other branch moved under the user (updateRefs), no fixup reordered
REBASE_PLAIN = ("-c", "rebase.updateRefs=false", "-c", "rebase.autoSquash=false",
                "-c", "rebase.autoStash=false")
NET_SECS = 600  # a full clone of workbench over a slow line
# workbench's contact, for a patch sent without GitHub (wb patch zip, #1291): the owner's own
# address, already in the git log; README.md, CONTRIBUTING.md and ARCHITECTURE.md say the same
UPSTREAM_CONTACT = "adam.schaefers@icloud.com"


class PatchError(RuntimeError):
    """A refusal or a failure wb reports as one line (cli.main dies with it)."""


def _git(cwd, *args, check=True, env=None, timeout=None) -> subprocess.CompletedProcess:
    try:
        r = subprocess.run(["git", "-C", str(cwd), *gitops.PLAIN, *gitops.NO_HOOKS, *args],
                           capture_output=True, stdin=subprocess.DEVNULL, start_new_session=True,
                           env={**os.environ, **(env or {})}, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise gitops.GitError(f"git {args[0]}: no answer in {timeout}s") from None
    if check and r.returncode:
        err = (r.stderr or r.stdout).decode("utf-8", "replace").strip()
        raise gitops.GitError(f"git {_verb(args)}: {err}")
    return r


def _verb(args) -> str:
    """The git command an error names: its first word past the `-c name=value` options."""
    i = 0
    while i < len(args) and args[i] == "-c":
        i += 2
    return args[i] if i < len(args) else ""


def _out(cwd, *args, check=True) -> str:
    return _git(cwd, *args, check=check).stdout.decode("utf-8", "replace").strip()


def _net_env(repo) -> dict:
    """A fetch or clone that never prompts: the user's ssh command kept (cli._fetch_origin's rule)."""
    env = {"GIT_TERMINAL_PROMPT": "0", "SSH_ASKPASS_REQUIRE": "never"}
    if not (os.environ.get("GIT_SSH_COMMAND") or os.environ.get("GIT_SSH")
            or repo and _out(repo, "config", "core.sshCommand", check=False)):
        env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes -o ConnectTimeout=15"
    return env


def source_dir(arg=None, root=None) -> Path:
    """Where `wb patch init` puts the clone: --dir, else (given the marketplace clone ``root``)
    patches.json's `source` when it is a clone of root's origin, else WB_SOURCE, else
    ~/.local/share/workbench/source. The recorded clone first, as the mod's gate reads it: wb
    setup --opencode's clone was passed over and a second one made, the patches split between the
    two (#1321's finding 14)."""
    if not arg and root is not None and (noted := _noted_source(root)) is not None:
        return noted
    v = arg or os.environ.get("WB_SOURCE")
    return Path(os.path.abspath(os.path.expanduser(v))) if v else Path.home() / ".local/share/workbench/source"


def _noted_source(root) -> Path | None:
    """patches.json's `source`, when it is another clone (its own top level) of root's origin."""
    noted = store.read_json(noted_path(), {}) or {}
    src = noted.get("source") if isinstance(noted, dict) else None
    if not isinstance(src, str) or not os.path.isabs(src) or not os.path.isdir(src):
        return None
    d = Path(src)
    if d.resolve() == Path(root).resolve() or Path(_out(d, "rev-parse", "--show-toplevel", check=False)
                                                   or "/nonexistent").resolve() != d.resolve():
        return None
    url = _out(root, "config", "--get", "remote.origin.url", check=False)
    theirs = _out(d, "config", "--get", "remote.origin.url", check=False)
    return d if url and theirs and _same_origin(theirs, url) else None


def branch(root) -> str:
    return _out(root, "symbolic-ref", "-q", "--short", "HEAD", check=False)


def _upstream(root, ref="HEAD") -> str | None:
    """The upstream's sha of ``ref``'s branch, None without one."""
    return _out(root, "rev-parse", "-q", "--verify", f"{ref}@{{u}}^{{commit}}", check=False) or None


def remote_of(root) -> str:
    b = branch(root)
    r = b and _out(root, "config", f"branch.{b}.remote", check=False)
    return r if r and r != "." else "origin"


class Changes(NamedTuple):
    """What a clone has that its upstream hasn't: commits (sha, subject), oldest first; the
    porcelain lines of uncommitted changes (untracked files too, never ignored ones); the commit
    they all stand on; its stashes ("stash@{n}: <message>"); and each other local branch's
    commits no remote has ({branch: [(sha, subject)]}). Claude Code's re-clone deletes stashes
    and branches as well: a check of HEAD's commits alone let wb update wipe them (#1005)."""
    commits: list
    dirty: list
    base: str
    stashes: list
    branches: dict

    def __bool__(self):
        return bool(self.commits or self.dirty or self.stashes or self.branches)

    def say(self) -> str:
        parts = [f"{len(self.commits)} local commit(s)"] if self.commits else []
        if self.dirty:
            parts.append(f"{len(self.dirty)} uncommitted change(s)")
        if self.stashes:
            parts.append(f"{len(self.stashes)} stash(es)")
        if self.branches:
            parts.append(f"{sum(map(len, self.branches.values()))} commit(s) on other local branches "
                         f"({', '.join(self.branches)})")
        return ", ".join(parts[:-1]) + " and " + parts[-1] if len(parts) > 1 else "".join(parts)


def _commits(root, *span) -> list:
    rows = _out(root, "log", "--reverse", "--no-merges", "--format=%H%x00%s", *span, "--")
    return [tuple(r.split("\0", 1)) for r in rows.splitlines() if r]


def local_changes(root) -> Changes:
    up = _upstream(root)
    commits = _commits(root, *([f"{up}..HEAD"] if up else ["HEAD", "--not", "--remotes"]))  # no upstream: what no remote has
    if up:  # HEAD's own base: a fetch (wb update --check's) moves the upstream ahead of it
        base = _out(root, "merge-base", "HEAD", up)
    else:
        base = _out(root, "rev-parse", f"{commits[0][0]}^" if commits else "HEAD")
    dirty = _out(root, "status", "--porcelain", "--untracked-files=all").splitlines()
    stashes = [r for r in _out(root, "stash", "list", "--format=%gd: %gs", check=False).splitlines() if r]
    here, others = branch(root), {}
    for b in _out(root, "for-each-ref", "--format=%(refname:short)", "refs/heads/").splitlines():
        if b and b != here:  # HEAD's own are `commits` above
            if rows := _commits(root, f"refs/heads/{b}", "--not", "--remotes", "HEAD"):
                others[b] = rows
    return Changes(commits, [d for d in dirty if d], base, stashes, others)


def uncommitted_diff(root) -> bytes:
    """The worktree against HEAD, new untracked files included, as a binary diff `git apply`
    takes: built in a scratch index, so the user's own index is never touched."""
    with tempfile.TemporaryDirectory() as scratch:
        index = Path(_out(root, "rev-parse", "--git-path", "index"))
        index = index if index.is_absolute() else Path(root) / index
        tmp = os.path.join(scratch, "index")
        if index.exists():
            shutil.copy2(index, tmp)
        env = {"GIT_INDEX_FILE": tmp}
        _git(root, "add", "-A", env=env)
        return _git(root, *DIFF_PLAIN, "diff", "--cached", "--binary", "--no-ext-diff", "--no-textconv",
                    "HEAD", "--", env=env).stdout


def rescue(root, found: Changes) -> Path:
    """Save ``found`` under $WB_HOME/rescued/<UTC time>/: a format-patch file per commit,
    uncommitted.diff, and `base` (the commit they apply to); stash-<n>.diff for each stash
    (its untracked files too), and branch-<name>/ with each other branch's commits."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    parent = store.home() / "rescued"
    parent.mkdir(parents=True, exist_ok=True)
    n = 1
    while True:  # two in one second each get their own
        d = parent / (stamp if n == 1 else f"{stamp}-{n}")
        try:
            d.mkdir()
            break
        except FileExistsError:
            n += 1
    for i, (sha, _) in enumerate(found.commits, 1):
        _git(root, *DIFF_PLAIN, "format-patch", "-q", "--no-signature", "-1", f"--start-number={i}",
             "-o", str(d), sha)
    if found.dirty:
        (d / "uncommitted.diff").write_bytes(uncommitted_diff(root))
    for n in range(len(found.stashes)):
        show = (*DIFF_PLAIN, "stash", "show", "-p", "--binary", "--no-ext-diff", "--no-textconv")
        r = _git(root, *show, "--include-untracked", f"stash@{{{n}}}", check=False)
        if r.returncode:  # a git before 2.32 has no --include-untracked: the tracked part at least
            r = _git(root, *show, f"stash@{{{n}}}")
        (d / f"stash-{n}.diff").write_bytes(r.stdout)
    for b, rows in found.branches.items():
        out = d / ("branch-" + b.replace("/", "-"))
        for i, (sha, _) in enumerate(rows, 1):
            _git(root, *DIFF_PLAIN, "format-patch", "-q", "--no-signature", "-1", f"--start-number={i}",
                 "-o", str(out), sha)
    (d / "base").write_text(found.base + "\n")
    return d


def marketplace_refusal(root, runs_from=None) -> str | None:
    """wb update with local changes in Claude Code's marketplace clone ``root``: they are saved
    first, and the refusal says where. Claude Code's update re-clones, and `wb update` once printed
    "updated marketplace <patched>..<upstream>", exit 0, having wiped the patch (#1005, 2026-11-22).
    ``runs_from``: the developer clone wb runs from, when ``root`` is another copy (#1326)."""
    found = local_changes(root)
    if not found:
        return None
    saved = rescue(root, found)
    fix = ("wb patch init moves workbench to a clone of your own that keeps them as patches and still "
           "updates" if runs_from is None else
           f"git am takes its patches into {runs_from}, and claude plugin marketplace add {runs_from} "
           "makes that clone the marketplace, which Claude Code never re-clones")
    return (f"the marketplace clone {root} has {found.say()}: Claude Code's update re-clones it and "
            f"would wipe them, so nothing was updated. Saved in {saved}; {fix}")


CHECK_LINE = ("local changes in the marketplace clone: wb update would refuse (Claude Code re-clones "
              "it); wb patch init keeps them")


def _subject(root, sha) -> str:
    return _out(root, "log", "-1", "--format=%h %s", sha)


TEST_NAME = re.compile(r"(?:^|/)(?:test_[^/]*\.py|[^/]*\.test\.tsx?)$")


def is_test(path) -> bool:
    """A patch's test file: under tests/ or kernel/test/ (the kernel's props, #1356), or a
    test_*.py, *.test.ts or *.test.tsx anywhere."""
    return path.startswith(("tests/", "kernel/test/")) or bool(TEST_NAME.search(path))


# --- the tests by directory (#1328, 2026-11-25) -------------------------------------------------
# Each part has its own runner, and a patch to one can break another: the OpenCode plugin imports
# the mod's shared core (mod/hooks/core.ts, the kernel's bundle, the goldens), so a change there,
# or to the kernel it is built from, is the plugin's too. `wb patch check` once ran every
# *.test.ts as the mod's suite (an OpenCode plugin test passed "upstream" unrun), and `wb patch
# upstream` named only the mod's commands (#1321, finding 18). PLUGIN_SHARED is what
# opencode-plugin/ imports from mod/ (tests/test_workbench.py checks it against the imports).
PLUGIN_SHARED = ("mod/hooks/core.ts", "mod/hooks/kernel.js", "mod/hooks/kernel.d.ts", "mod/hooks/golden/",
                 "mod/hooks/splash.ts", "mod/hooks/apprentice.ts",  # the splash, one for both hosts (#1333)
                 "mod/hooks/tasks.ts", "mod/hooks/modeline.ts")  # the Tasks view, one for both hosts (#1312)
KERNEL_CHECK = "python3 scripts/kernel.py check"
MOD_TESTS = "claude plugin validate mod && claude plugin test mod"
PLUGIN_TESTS = "BUN_BE_BUN=1 opencode test opencode-plugin/test && BUN_BE_BUN=1 opencode check -p opencode-plugin"
SUITES = (  # (what a change under these paths needs beyond python3 -m unittest, its command)
    (("kernel/",), KERNEL_CHECK),
    (("mod/", "kernel/"), MOD_TESTS),
    (("opencode-plugin/", "package.json", *PLUGIN_SHARED, "kernel/"), PLUGIN_TESTS),
)


def suites(paths) -> list[str]:
    """The test commands a change to ``paths`` needs beyond `python3 -m unittest`, by directory
    (SUITES), in CI's order: the kernel, the mod, the OpenCode plugin."""
    return [cmd for dirs, cmd in SUITES if any(p == d or p.startswith(d) for p in paths for d in dirs)]


def _touched(root, sha) -> list[str]:
    out = _git(root, "diff-tree", "-r", "-z", "--no-commit-id", "--name-only", "--no-renames", "--root", sha)
    return [p for p in out.stdout.decode("utf-8", "surrogateescape").split("\0") if p]


def has_test(root, sha) -> bool:
    return any(is_test(p) for p in _touched(root, sha))


NO_TEST_SEND = ("it has no test: add one before sending (upstream asks for one, and wb patch check "
                "needs it)")


@contextmanager
def scratch(root, at):
    """A detached worktree of ``root`` at ``at``, under $WB_HOME, gone afterwards, also on a failure."""
    holder = store.home() / "patches"
    holder.mkdir(parents=True, exist_ok=True)
    holder = Path(tempfile.mkdtemp(prefix="tree-", dir=holder))
    wt = holder / "tree"
    try:
        _git(root, "worktree", "add", "-q", "--detach", str(wt), at)
        yield wt
    finally:
        _git(root, "worktree", "remove", "--force", "--force", str(wt), check=False)
        shutil.rmtree(holder, ignore_errors=True)
        _git(root, "worktree", "prune", check=False)


def _rebase(root, at, *args, env=None) -> tuple[str | None, str | None]:
    """`git rebase *args` in a scratch worktree at ``at``: (the new tip, None), or (None, the
    patch that stopped it, "<short sha> <subject>"), aborted. The live tree is never touched."""
    with scratch(root, at) as wt:
        r = _git(wt, *REBASE_PLAIN, "rebase", "-q", "--no-autosquash", *args, check=False, env=env)
        if not r.returncode:
            return _out(wt, "rev-parse", "HEAD"), None
        stopped = _out(wt, "rev-parse", "-q", "--verify", "REBASE_HEAD", check=False)
        _git(wt, "rebase", "--abort", check=False)
        if not stopped:  # not a conflict: no identity, a broken repo
            err = (r.stderr or r.stdout).decode("utf-8", "replace").strip()
            raise gitops.GitError(f"git rebase: {err[-300:]}")
        return None, _subject(root, stopped)


def _ident_rows(root, span) -> list:
    return [r for r in _out(root, "log", "--reverse", "--no-merges",
                            "--format=%an%x00%ae%x00%at%x00%s", span, "--").splitlines() if r]


def _sha_rows(root, span) -> list[tuple[str, str]]:
    """(sha, its _ident_rows row) of ``span``'s commits, oldest first."""
    rows = _out(root, "log", "--reverse", "--no-merges", "--format=%H%x00%an%x00%ae%x00%at%x00%s", span, "--")
    return [tuple(r.split("\0", 1)) for r in rows.splitlines() if r]


def _ident(root, sha) -> dict:
    """A commit's ident as patches.json keeps it: what a rebase or a cherry-pick leaves as it was."""
    author, email, date, subject = _out(root, "log", "-1", "--format=%an%x00%ae%x00%at%x00%s", sha).split("\0", 3)
    return {"author": author, "email": email, "date": int(date), "subject": subject}


def _ident_row(ident) -> str | None:
    """``ident`` (patches.json's) as an _ident_rows row; None when it isn't one."""
    try:
        return "\0".join((str(ident["author"]), str(ident["email"]), str(int(ident["date"])), str(ident["subject"])))
    except (KeyError, TypeError, ValueError):
        return None


# git runs it on the todo of the rebase that leaves out a merged pull request's patch: each
# `pick <sha>` of a skipped commit becomes `drop` (the shas come first, the todo's path last)
DROP_EDITOR = """import sys
*skip, todo = sys.argv[1:]
with open(todo, encoding="utf-8", errors="surrogateescape") as f:
    rows = f.read().split("\\n")
def gone(row):
    w = row.split()
    return len(w) > 1 and w[0] in ("pick", "p") and any(s.startswith(w[1]) for s in skip)
with open(todo, "w", encoding="utf-8", errors="surrogateescape") as f:
    f.write("\\n".join("drop " + r.split(None, 1)[1] if gone(r) else r for r in rows))
"""


class Rebased(NamedTuple):
    old: str
    upstream: str
    tip: str
    landed: list  # the subjects of the patches upstream already has, dropped
    kept: int


def rebase_patches(root, skip=frozenset()) -> Rebased:
    """The patches on PATCH_BRANCH (@{u}..HEAD) onto the fetched upstream, in a scratch worktree.
    A patch upstream already has (a clean cherry-pick of one, or one that comes out empty) is
    dropped by the rebase; a conflict raises PatchError naming the patch, nothing changed. The
    commits in ``skip`` (full shas: patches whose own pull request merged, `settle_sent`) are left
    out, so a squashed or reworded merge doesn't conflict with them (#1005, 2026-11-22); they
    aren't counted in `landed`, whose line is upstream's own match."""
    old = _out(root, "rev-parse", "HEAD")
    up = _upstream(root)
    if not up:
        raise PatchError(f"branch {branch(root)} of {root} has no upstream: git -C {root} branch -u origin/main")
    mine = _sha_rows(root, f"{up}..{old}")
    skip = {sha for sha, _ in mine if sha in skip}
    if skip:  # an interactive rebase whose todo drops them; nothing else differs
        editor = shlex.join([sys.executable, "-I", "-c", DROP_EDITOR, *sorted(skip)])
        tip, stopped = _rebase(root, old, "-i", "--empty=drop", up,
                               env={"GIT_SEQUENCE_EDITOR": editor, "GIT_EDITOR": "true"})
    else:
        tip, stopped = _rebase(root, old, up)
    if stopped:
        sha = stopped.split()[0]
        raise PatchError(
            f"the local patch {stopped} no longer applies on upstream's {up[:7]}: nothing changed. "
            f"Either rework it by hand (rebase {PATCH_BRANCH} onto @{{u}} in a scratch worktree, never "
            f"the live tree) or drop it (wb patch drop {sha}), then wb update "
            f"(wb patch check {sha} first: if upstream fixed it another way, drop it)")
    before = [row for sha, row in mine if sha not in skip]
    after = _ident_rows(root, f"{up}..{tip}")
    landed, i = [], 0
    for row in before:  # the rebase keeps each patch's author, date, message and order
        if i < len(after) and after[i] == row:
            i += 1
        else:
            landed.append(row.split("\0")[3])
    return Rebased(old, up, tip, landed, len(after))


def landed_lines(rb: Rebased) -> list[str]:
    return ([f"landed upstream, dropped from {PATCH_BRANCH}: {s}" for s in rb.landed]
            + [f"{rb.kept} local patch(es) on top of upstream ({PATCH_BRANCH})"])


def check_move(root, tip) -> None:
    """Refuse, changing nothing, when moving the live tree to ``tip`` would overwrite a file in
    it: an untracked file on a path ``tip`` adds made `reset --keep` fail only after wb update
    had stopped the tasks, saying "the pull is done" of a branch that never moved (#1005,
    2026-11-22). The dry run is the same two-tree merge `reset --keep` does."""
    r = _git(root, "read-tree", "-m", "-u", "-n", "HEAD", tip, check=False)
    if r.returncode:
        err = " ".join((r.stderr or r.stdout).decode("utf-8", "replace").split())
        raise PatchError(f"moving {branch(root) or 'HEAD'} of {root} to the rebased patches would overwrite "
                         f"files in its tree, so nothing changed: {err[:400]} (move or remove them, then again)")


def move_branch(root, old, tip) -> None:
    """The live branch from ``old`` to ``tip``, then the old tip kept at BEFORE. --keep refuses
    rather than overwrite a change in the tree, and a failure leaves both the branch and BEFORE
    as they were (BEFORE was written first once, and named a tip HEAD never left). The ref
    keeps a reflog of its own, so every earlier tip stays listed, not only the last one."""
    if _out(root, "rev-parse", "HEAD") != old:
        raise PatchError(f"{root} moved from {old[:7]} while wb worked: nothing changed (run it again)")
    try:
        _git(root, "reset", "-q", "--keep", tip)
    except gitops.GitError as e:
        raise PatchError(f"the patches are rebased at {tip[:7]}, but {branch(root) or 'HEAD'} is unchanged: "
                         f"{e}") from None
    keep_before(root, old)


def keep_before(root, old) -> None:
    _git(root, "update-ref", "--create-reflog", "-m", "wb: the patches before", BEFORE, old)


def _need_clone(kind):
    if kind != "clone":
        raise PatchError("the marketplace clone can't keep patches (Claude Code re-clones it on update): "
                         "wb patch init moves workbench to a clone of your own first")


def _on_patch_branch(root):
    b = branch(root)
    if b != PATCH_BRANCH:
        raise PatchError(f"{root} is on {b or 'a detached HEAD'}, not {PATCH_BRANCH}: patches are commits "
                         f"on {PATCH_BRANCH} (git -C {root} switch {PATCH_BRANCH}, or switch -c {PATCH_BRANCH} --track origin/main)")


def _patches(root) -> list:
    rows = _out(root, "log", "--reverse", "--format=%h%x00%s%x00%(trailers:key=Scope,valueonly,separator=%x2C)",
                "@{u}..HEAD", "--")
    return [tuple(r.split("\0")) for r in rows.splitlines() if r]


def track_upstream(root) -> str | None:
    """Make PATCH_BRANCH track origin's default branch when it tracks nothing; the line to print,
    else None. One made with a plain `git switch -c local`, as the texts said, had no upstream:
    wb update died "fatal: no upstream configured", wb doctor skipped its patches, and a reused
    `local` stayed so (#1327, 2026-11-24). Only a branch that exists, with that ref fetched."""
    ref = f"refs/heads/{PATCH_BRANCH}"
    if (not _out(root, "rev-parse", "-q", "--verify", ref, check=False)
            or _out(root, "config", f"branch.{PATCH_BRANCH}.merge", check=False)):
        return None
    head = _out(root, "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD", check=False)
    up = head or "origin/main"
    if not _out(root, "rev-parse", "-q", "--verify", f"refs/remotes/{up}", check=False):
        return None
    _git(root, "branch", "-q", f"--set-upstream-to={up}", PATCH_BRANCH)
    return f"branch {PATCH_BRANCH} tracked no upstream: it tracks {up} now"


def no_upstream(root) -> str | None:
    """None when HEAD's branch tracks one, else what to run, for a refusal or wb doctor (#1327)."""
    b = branch(root)
    if not b or _out(root, "config", f"branch.{b}.merge", check=False):
        return None
    return f"branch {b} tracks no upstream: git -C {root} branch -u origin/main {b}"


def clone_note(root) -> str:
    b = branch(root) or "a detached HEAD"
    if b == PATCH_BRANCH:
        return (f"workbench already runs from a clone you own ({root}), on branch {b}: patches are its "
                "commits (wb patch list)")
    return (f"workbench already runs from a clone you own ({root}), on branch {b}: patches are commits on "
            f"branch {PATCH_BRANCH} (git -C {root} switch -c {PATCH_BRANCH} --track origin/main to start)")


def list_patches(root, kind) -> None:
    if kind != "clone":
        found = local_changes(root)
        print("the marketplace clone can't keep patches: Claude Code re-clones it on update")
        for sha, subject in found.commits:
            print(f"  local commit {sha[:7]}  {subject}")
        for line in found.dirty:
            print(f"  uncommitted {line}")
        for line in found.stashes:
            print(f"  {line}")
        for b, rows in found.branches.items():
            for sha, subject in rows:
                print(f"  on branch {b}: {sha[:7]}  {subject}")
        print("wb patch init moves workbench to a clone of your own" + (" and keeps these as patches" if found else ""))
        return
    b = branch(root) or "a detached HEAD"
    up = _out(root, "rev-parse", "--abbrev-ref", "@{u}", check=False)
    print(f"{root}: branch {b}" + (f", tracking {up}" if up else ", no upstream")
          + ("" if b == PATCH_BRANCH else f" (patches belong on {PATCH_BRANCH}: wb update rebases only that one)"))
    rows = _patches(root) if up else []
    for sha, subject, scope in rows:  # the nudge: wb patch check can't judge one with no test (#1005)
        print(f"{sha}  {subject}  [{scope or 'no Scope trailer'}]" + ("" if has_test(root, sha) else " (no test)"))
    if up and not rows:
        print("no patches: the branch is upstream's")
    dirty = _out(root, "status", "--porcelain", "--untracked-files=all").splitlines()
    if dirty:
        print("uncommitted: " + ", ".join(d.strip() for d in dirty))
    trees = _worktrees(root)
    for pr in _out(root, "for-each-ref", "--format=%(refname:short)", "refs/heads/pr/").splitlines():
        print(f"upstream branch {pr}  {trees.get(pr) or '(no worktree)'}")


def drop(root, kind, commit) -> None:
    _need_clone(kind)
    _on_patch_branch(root)
    if _out(root, "status", "--porcelain", "--untracked-files=no"):
        raise PatchError(f"{root} has uncommitted changes: commit or stash them first")
    sha = _out(root, "rev-parse", "-q", "--verify", f"{commit}^{{commit}}", check=False)
    if not sha or sha not in _out(root, "rev-list", "@{u}..HEAD").split():
        raise PatchError(f"{commit} isn't one of {PATCH_BRANCH}'s patches (wb patch list)")
    old, what = _out(root, "rev-parse", "HEAD"), _subject(root, sha)
    tip, stopped = _rebase(root, old, "--onto", f"{sha}^", sha)
    if stopped:
        raise PatchError(f"without {what}, the later patch {stopped} doesn't apply: nothing changed")
    check_move(root, tip)  # a dropped deletion brings its file back, over an untracked one
    move_branch(root, old, tip)
    print(f"dropped {what}; the tip before is kept at {BEFORE} (git reflog {BEFORE}: the earlier ones). "
          "wb update refreshes the mod")


# --- wb patch check: has upstream fixed it another way? (#1005, 2026-11-22) ---------------------
CHECK_SECS = 900  # a patch's own tests: the added methods alone, or the mod's suite
TEST_DEF = re.compile(r"^\+\s*(?:async\s+)?def\s+(test\w*)\s*\(")


def _added_tests(root, sha, paths) -> dict[str, list[str]]:
    """{test_*.py path: the test methods ``sha``'s diff adds there}, from its `+def test_…` lines."""
    out = {}
    for path in paths:
        if not path.endswith(".py"):
            continue
        diff = _git(root, *DIFF_PLAIN, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "-U0",
                    f"{sha}^", sha, "--", path, env={"GIT_LITERAL_PATHSPECS": "1"}).stdout.decode("utf-8", "replace")
        names = [m.group(1) for line in diff.splitlines() if (m := TEST_DEF.match(line))]
        if names:
            out[path] = list(dict.fromkeys(names))
    return out


def _test_ids(wt, added) -> tuple[list[str], list[str]]:
    """unittest's ids (module.Class.method) of the added methods, their classes read from the file
    as it stands in ``wt``, and the added names placed in no class there (a file that doesn't
    parse places none): those didn't run, so the check can't call the patch's tests passed."""
    ids, lost = [], []
    for path, names in added.items():
        try:
            tree = ast.parse((wt / path).read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError, ValueError):
            lost += names
            continue
        module = path.removesuffix(".py").replace("/", ".")
        found = set()
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                defs = {f.name for f in node.body if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))}
                ids += [f"{module}.{node.name}.{n}" for n in names if n in defs]
                found |= defs
        lost += [n for n in names if n not in found]
    return ids, lost


# the added methods, run as `python -m unittest` runs them (cwd on sys.path: no -I), but told
# apart: 1 a failure or an error, 3 a skip, an expected failure, an unexpected success or no test
# at all, else 0. A skip is not a pass: a test skipped without Landlock or claude was called
# "upstream already passes" (#1005, 2026-11-22)
RUN_TESTS = """import sys, unittest
suite = unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:])
r = unittest.TextTestRunner(stream=sys.stderr, verbosity=0).run(suite)
sys.exit(1 if r.failures or r.errors else
         3 if r.skipped or r.expectedFailures or r.unexpectedSuccesses or not r.testsRun else 0)
"""


def _trial(argv, cwd, env, skipped=None) -> tuple[str, str]:
    """("pass", "fail" or "skipped", the last line it printed): ``argv``'s exit within CHECK_SECS
    (0, else ``skipped`` when it names that exit, else a failure); on the timeout, a failure, its
    whole process group gone. The line is for a pull request's body, which is public: the
    worktree's path and the home directory in it are said as `.` and `~`."""
    with tempfile.TemporaryFile() as out:
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=out,
                                stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code = proc.wait(timeout=CHECK_SECS)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            proc.wait()
            return "fail", f"no end in {CHECK_SECS} s"
        out.seek(max(0, out.seek(0, os.SEEK_END) - 4096))
        tail = out.read().decode("utf-8", "replace")
    last = next((ln.strip() for ln in reversed(tail.splitlines()) if ln.strip()), f"exit {code}")
    last = scrub(last.replace(str(cwd), ".").replace(str(Path.home()), "~"))[:200]
    return ("pass" if code == 0 else "skipped" if skipped is not None and code == skipped else "fail"), last


def _own_runs(wt, tests, added) -> tuple[list[tuple[str, list[str], dict, int | None]], list[str]]:
    """How a patch's own test files run in ``wt``, by directory (#1328): ([(the command as said,
    argv, its environment's additions, the exit that means skipped)], what can't run and why).
    The added test methods of its test_*.py files, alone; a *.test.ts(x) under mod/, the mod's
    suite through claude; under opencode-plugin/, the plugin's through OpenCode's Bun (or bun);
    under kernel/, the kernel's props. Any other *.test.ts has no runner: said, never passed."""
    runs, why = [], []
    ids, lost = _test_ids(wt, added)
    if ids:
        runs.append((f"python3 -m unittest {' '.join(ids)}", [sys.executable, "-c", RUN_TESTS, *ids], {}, 3))
    if lost:
        why.append(f"no test class holds {', '.join(lost)}")
    other = [p for p in tests if not p.endswith(".py")]
    if any(p.startswith("mod/") for p in other):
        if shutil.which("claude") is None:
            why.append("claude isn't on PATH for the mod's tests")
        else:
            runs.append(("claude plugin test mod", ["claude", "plugin", "test", "mod"], {}, None))
    if any(p.startswith("opencode-plugin/") for p in other):
        if shutil.which("opencode"):
            runs.append(("BUN_BE_BUN=1 opencode test opencode-plugin/test",
                         ["opencode", "test", "opencode-plugin/test"], {"BUN_BE_BUN": "1"}, None))
        elif shutil.which("bun"):
            runs.append(("bun test opencode-plugin/test", ["bun", "test", "opencode-plugin/test"], {}, None))
        else:
            why.append("neither opencode nor bun is on PATH for the OpenCode plugin's tests")
    if any(p.startswith("kernel/") for p in other):
        # kernel.py props refuses with neither (exit 1): that read as upstream failing the patch
        if shutil.which("bun") or shutil.which("opencode"):
            runs.append(("python3 scripts/kernel.py props", [sys.executable, "scripts/kernel.py", "props"], {}, None))
        else:
            why.append("neither bun nor opencode is on PATH for the kernel's props")
    stray = [p for p in other if not p.startswith(("mod/", "opencode-plugin/", "kernel/"))]
    if stray:
        why.append(f"no runner for {', '.join(stray)} (only mod/, opencode-plugin/ and kernel/ have one)")
    return runs, why


def check(root, kind, commit) -> None:
    """wb patch check: ``commit``'s own tests, and only them, on plain upstream (the one wb update
    last fetched), in a scratch worktree removed afterwards. A pull request upstream merged after
    rewording or editing it, or a fix of upstream's own, left the local patch to conflict or
    double up (#1005, 2026-11-22): its tests passing there say upstream likely fixed the same
    thing. It only says so: dropping the patch is the user's yes, through wb patch drop."""
    _need_clone(kind)
    sha = _out(root, "rev-parse", "-q", "--verify", f"{commit}^{{commit}}", check=False)
    if not sha:
        raise PatchError(f"{commit}: no such commit")
    up = _upstream(root, PATCH_BRANCH)
    if not up or sha not in _out(root, "rev-list", f"{up}..refs/heads/{PATCH_BRANCH}").split():
        raise PatchError(f"{commit} isn't one of {PATCH_BRANCH}'s patches (wb patch list)")
    subject, short = _out(root, "log", "-1", "--format=%s", sha), _out(root, "rev-parse", "--short", sha)
    no_test = (f"{subject} has no test of its own, so wb patch check can't tell: write one (upstream asks "
               "for one in a pull request too)")
    tests = [p for p in _touched(root, sha) if is_test(p)]
    if not tests:
        print(no_test)
        return
    diff = _git(root, *DIFF_PLAIN, "diff", "--binary", "--no-ext-diff", "--no-textconv", "--no-renames",
                f"{sha}^", sha, "--", *tests, env={"GIT_LITERAL_PATHSPECS": "1"}).stdout
    added = _added_tests(root, sha, tests)
    with scratch(root, up) as wt:
        (wt.parent / "tests.diff").write_bytes(diff)
        if _git(wt, "apply", "--3way", "--whitespace=nowarn", str(wt.parent / "tests.diff"),
                check=False).returncode:
            print(f"{subject}'s tests don't apply to upstream any more: compare by hand")
            return
        env = {**os.environ, "WB_HOME": str(wt.parent / "home")}  # never the user's state
        # why: what didn't run, so a pass can't be told (#1005, 2026-11-22); each by its own
        # directory's runner (#1328)
        runs, why = _own_runs(wt, tests, added)
        verdicts = []
        for _, argv, extra, skipped in runs:
            verdicts.append(_trial(argv, wt, {**env, **extra}, skipped)[0])
            if verdicts[-1] == "skipped":
                why.append("a test was skipped")
    if "fail" in verdicts:
        print(f"upstream fails this patch's tests ({subject}): keep it")
    elif why:
        print(f"{subject}'s tests didn't all run here ({'; '.join(why)}), so wb patch check can't tell: "
              "run them where they can (a skip is not a pass)")
    elif not verdicts:
        print(no_test)
    else:
        print(f"upstream already passes this patch's tests ({subject}): it likely fixed the same thing. "
              f"Drop it with the user's yes: wb patch drop {short}")


def _owner_repo(url) -> str | None:
    m = re.match(r"(?:[a-z+]+://(?:[^@/]+@)?github\.com/|[^@/]+@github\.com:)([^/]+/[^/]+?)(?:\.git)?/?$", url)
    return m.group(1) if m else None


def _tracked(root) -> tuple[str, str]:
    """(remote, branch) of the upstream branch PATCH_BRANCH tracks, else HEAD's, else the
    remote's default: the branch `wb patch upstream` cuts from and a pull request targets."""
    for b in dict.fromkeys((PATCH_BRANCH, branch(root))):
        merge = b and _out(root, "config", f"branch.{b}.merge", check=False)
        remote = b and _out(root, "config", f"branch.{b}.remote", check=False)
        if merge and remote and remote != ".":
            return remote, merge.removeprefix("refs/heads/")
    head = _out(root, "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD", check=False)
    return "origin", head.removeprefix("origin/") or "main"


def noted_path() -> Path:
    return store.home() / "patches.json"


@contextmanager
def _noting():
    """patches.json to change, under its lock, written back atomically when the block ends
    without an error. Each writer changes only its own keys: note_install's `source` and
    `upstream` (the mod's), `prepared` (wb patch upstream), `sent` (wb patch send, wb update)."""
    path = noted_path()
    with store.locked(path.with_name(path.name + ".lock")):
        noted = store.read_json(path, {})
        noted = noted if isinstance(noted, dict) else {}
        yield noted
        store.write_json(path, noted)


def _table(noted, key) -> dict:
    """patches.json's ``key``, a dict made when missing or not one."""
    if not isinstance(noted.get(key), dict):
        noted[key] = {}
    return noted[key]


def note_install(root) -> None:
    """$WB_HOME/patches.json: the clone install and its upstream on GitHub, for the mod (#1005)."""
    url = _out(root, "config", "--get", f"remote.{_tracked(root)[0]}.url", check=False)
    with _noting() as noted:
        noted.update(source=str(root), upstream=_owner_repo(url))


def _forget(slug) -> None:
    """A retired pr/<slug>'s rows in patches.json: what wb patch upstream picked and its pull request."""
    with _noting() as noted:
        for key in ("prepared", "sent"):
            if isinstance(noted.get(key), dict):
                noted[key].pop(slug, None)


# --- gh, GitHub's CLI: the one way wb sends a patch upstream (#1005) ---------------------------
# wb reads gh's state and runs its fork and pr create; it never reads, prints or stores a token
# (no --show-token, no `gh auth token`, never plain `gh auth status`, which prints a masked one),
# and every message from gh or git passes scrub() first: a pushed URL or an error may carry one.
TOKEN = re.compile(r"\b(?:gh[oprsu]_\w+|github_pat_\w+)")  # ghr_: a refresh token
NET_ERR = re.compile(r"dial tcp|proxy|timeout|timed out|unreachable|no such host|connection (?:refused|reset)"
                     r"|network|TLS handshake|could not resolve", re.I)
GH_SECS = 20  # gh auth status asks the API once per account
GH_DOCS = "https://github.com/cli/cli/blob/trunk/docs/install_linux.md"
GH_RELEASES = "https://github.com/cli/cli/releases"
OS_RELEASE = ("/etc/os-release", "/usr/lib/os-release")
# a new fork shows up asynchronously: the first push to it can find no repository there
PUSH_WAITS = (2, 4, 8, 16)


def _shown_url(url) -> str:
    """A remote's URL fit to print: its user:password@ part gone, then scrubbed. A remote off
    GitHub was printed whole, a GitLab token in it too (#1005, 2026-11-22)."""
    return scrub(re.sub(r"(?<=//)[^@/\s]+@", "", url or "")) or "no URL"


def scrub(text) -> str:
    """One line of gh's or git's output, anything token-shaped cut out."""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    return TOKEN.sub("<token>", " ".join((text or "").split()))[:300]


def _os_release() -> dict:
    for p in OS_RELEASE:
        try:
            text = Path(p).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rows = (line.split("=", 1) for line in text.splitlines() if "=" in line)
        return {k.strip(): v.strip().strip("'\"") for k, v in rows}
    return {}


def _user_runs(cmd, how="the user runs", sep=": ") -> str:
    """How the user runs ``cmd``, which asks them something (a password, a login): Claude Code's
    `! ` prefix; from an OpenCode driver, in a terminal of their own, as its `!` had no terminal
    to prompt on (read in OpenCode 2.3.2's source: sudo, pamac and gh's login couldn't ask, and a
    pasted `! …` went to the model; #1321, finding 17), and a terminal works on any version."""
    if store.caller_kind() == "opencode":
        return f"{how}, in a terminal of their own: {cmd}"
    return f"{how}{sep}! {cmd}"


def driver_name() -> str:
    """The model driving wb, as a hint names it (it asks the user first, never handles a token,
    fixes a crash as a patch): OpenCode's from an OpenCode driver, else Claude (#1321, finding 27)."""
    return "OpenCode's model" if store.caller_kind() == "opencode" else "Claude"


def install_hint() -> list[str]:
    """The one line that installs gh on this machine, labelled official or community as GitHub's
    docs label it. Never snap: GitHub's docs say never to install gh as one.
    Installing is the user's yes, and most lines need sudo, which Claude's Bash can't answer."""
    if platform.system() == "Darwin":
        if shutil.which("brew"):
            cmd, label = "brew install gh", "official"
        else:
            cmd, label = f"the macOS .pkg from {GH_RELEASES}/latest", "official"
        docs, notes = "https://github.com/cli/cli#macos", []
    else:
        rel = _os_release()
        ids = [rel.get("ID", "").lower(), *rel.get("ID_LIKE", "").lower().split()]
        docs, notes, label = GH_DOCS, [], "community"
        if ids[0] == "amzn":
            cmd, label = ("(type -p yum-config-manager >/dev/null || sudo yum install yum-utils) && sudo "
                          "yum-config-manager --add-repo https://cli.github.com/packages/rpm/gh-cli.repo && "
                          "sudo yum install gh"), "official"
        elif ids[0] == "manjaro":
            cmd = "pamac install github-cli"
        elif ids[0] == "nixos":
            # nix-shell -p gh leaves gh off PATH once that shell exits (review of #1005, 2026-11-22)
            cmd = "nix-env -iA nixos.gh"
            notes.append("or add gh to environment.systemPackages")
        elif any(i in ("debian", "ubuntu", "raspbian") for i in ids):
            cmd, label = (
                "(type -p wget >/dev/null || (sudo apt update && sudo apt install wget -y)) && sudo mkdir -p "
                "-m 755 /etc/apt/keyrings && out=$(mktemp) && wget -nv -O$out "
                "https://cli.github.com/packages/githubcli-archive-keyring.gpg && cat $out | sudo tee "
                "/etc/apt/keyrings/githubcli-archive-keyring.gpg > /dev/null && sudo chmod go+r "
                "/etc/apt/keyrings/githubcli-archive-keyring.gpg && sudo mkdir -p -m 755 "
                "/etc/apt/sources.list.d && echo \"deb [arch=$(dpkg --print-architecture) "
                "signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages "
                "stable main\" | sudo tee /etc/apt/sources.list.d/github-cli.list > /dev/null && sudo apt "
                "update && sudo apt install gh -y"), "official, GitHub's apt repository"
            notes.append("not the distro's own `apt install gh`: it is older, and its 2.45/2.46 builds are "
                         "broken")
        elif any(i.startswith("opensuse") or i in ("suse", "sles") for i in ids):
            cmd, label = ("sudo zypper addrepo https://cli.github.com/packages/rpm/gh-cli.repo && sudo "
                          "zypper ref && sudo zypper install gh"), "official"
        elif any(i in ("fedora", "rhel", "centos") for i in ids):
            label = "official"
            cmd = ("sudo dnf install dnf5-plugins && sudo dnf config-manager addrepo "
                   "--from-repofile=https://cli.github.com/packages/rpm/gh-cli.repo && sudo dnf install gh"
                   if shutil.which("dnf5") else
                   "sudo dnf install 'dnf-command(config-manager)' && sudo dnf config-manager --add-repo "
                   "https://cli.github.com/packages/rpm/gh-cli.repo && sudo dnf install gh")
        elif "arch" in ids:
            cmd = "sudo pacman -S github-cli"
        elif "alpine" in ids:
            cmd = "apk add github-cli"
        elif shutil.which("nix"):
            cmd = "nix-env -iA nixpkgs.gh"
        elif shutil.which("brew"):
            cmd, label = "brew install gh", "official"
        else:
            cmd, label, docs = f"a precompiled binary from {GH_RELEASES}/latest", "official", GH_RELEASES
        if hardware.system() == "wsl":
            notes.append(f"GitHub's docs don't cover WSL: this is {rel.get('ID') or 'the distro'}'s line, "
                         "run inside WSL")
    # sudo, root and polkit (pamac) lines ask for a password Claude's Bash can't answer: the
    # user's to run; apk and pamac once got "runs:" because only sudo was looked for (#1005)
    run = ("the user runs, as root: " + cmd if cmd.startswith("apk ") else
           _user_runs(cmd) if "sudo " in cmd or cmd.startswith("pamac ") else "runs: " + cmd)
    lines = [f"gh (GitHub's CLI) isn't installed. Installing it is the user's yes: {driver_name()} asks first, then "
             + run,
             f"  ({label}; {docs}" + "".join(f"; {n}" for n in notes) + ")"]
    if platform.system() != "Darwin" and shutil.which("brew") and cmd != "brew install gh":
        lines.append("  or, with no sudo: brew install gh (official)")
    return lines


GH_SSH = re.compile(r"^(?:git@github\.com:|ssh://(?:[^@/]+@)?github\.com)", re.I)
GH_HTTPS = re.compile(r"^https://(?:[^@/]+@)?github\.com/", re.I)


def _guess_protocol(root=None) -> str:
    """ssh when the user has a key or git already reaches GitHub over ssh, else https. An
    insteadOf rule says it by its target, the url.<base> key: a rule rewriting git@github.com:
    to https forces https, and was once read as ssh because its value names git@ (#1005,
    2026-11-22: such a user, no ssh key, was told to log gh in over ssh)."""
    rules = _out(root or Path.home(), "config", "--get-regexp", r"^url\..*\.(push)?insteadof$", check=False)
    targets = []
    for line in rules.splitlines():
        key, _, value = line.partition(" ")
        base = re.sub(r"\.(?:push)?insteadof$", "", key[len("url."):], flags=re.I)
        if GH_SSH.match(value.strip()) or GH_HTTPS.match(value.strip()):  # a rule about GitHub's URLs
            targets.append(base)
    if any(GH_HTTPS.match(t) for t in targets) and not any(GH_SSH.match(t) for t in targets):
        return "https"
    if any(GH_SSH.match(t) for t in targets):
        return "ssh"
    ssh = Path.home() / ".ssh"
    try:
        if any(p.is_file() and p.suffix != ".pub" for p in ssh.glob("id_*")):
            return "ssh"
    except OSError:
        pass
    url = _out(root, "config", "--get", f"remote.{_tracked(root)[0]}.url", check=False) if root else ""
    return "ssh" if GH_SSH.match(url) else "https"


def _gh(*args, timeout=120, cwd=None) -> subprocess.CompletedProcess:
    """gh with no terminal and no prompt: a question it would ask fails the call instead."""
    return subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout, cwd=cwd,
                          stdin=subprocess.DEVNULL, start_new_session=True,
                          env={**os.environ, "GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1"})


def _login_hint(protocol, expired=False) -> list[str]:
    lines = ["gh's github.com login has expired or was revoked (HTTP 401)"] if expired else []
    lines.append(_user_runs(f"gh auth login --hostname github.com --git-protocol {protocol}")
                 + f" (a browser or one-time code; {driver_name()} never handles the token)")
    if protocol == "https":
        lines.append(_user_runs("gh auth setup-git", "then") + ", so git pushes over https with that login "
                     "(it edits the user's global git config: ask first)")
    return lines


def gh_state(root=None) -> dict:
    """gh's state for github.com: 'missing', 'logged-out', 'error' or 'ready', with the login,
    its git protocol, the token's scopes and source (never the token), and `hint`: what the
    user does next, one line each."""
    st = {"state": "missing", "login": None, "protocol": None, "scopes": [], "source": None,
          "hint": "", "error": None}
    if shutil.which("gh") is None:
        return {**st, "hint": "\n".join(install_hint())}

    def failed(state, err, hint):
        return {**st, "state": state, "error": err, "hint": "\n".join(hint)}
    try:
        r = _gh("auth", "status", "--hostname", "github.com", "--json", "hosts", timeout=GH_SECS)
    except subprocess.TimeoutExpired:
        return failed("error", f"gh auth status: no answer in {GH_SECS}s", ["GitHub unreachable, try again"])
    except OSError as e:
        return failed("error", scrub(str(e)), [f"gh didn't run: {scrub(str(e))}"])
    try:
        hosts = json.loads(r.stdout).get("hosts") or {}
    except (ValueError, AttributeError):
        hosts = None
    if hosts is None:  # no JSON: an old gh, or a fatal error before any
        err = scrub(r.stderr or r.stdout) or f"exit {r.returncode}"
        if "not logged in" in err.lower():
            return failed("logged-out", err, _login_hint(_guess_protocol(root)))
        if "unknown flag" in err:
            return failed("error", err, ["this gh is too old for gh auth status --json: the user updates it "
                                         f"the way it was installed (GitHub's ways: {GH_DOCS})"])
        return failed("error", err, ["GitHub unreachable, try again"] if NET_ERR.search(err) else
                      [f"gh auth status failed: {err}"])
    rows = [row for row in (hosts.get("github.com") or []) if isinstance(row, dict)]
    if not rows:
        return failed("logged-out", None, _login_hint(_guess_protocol(root)))
    row = next((row for row in rows if row.get("active")), rows[0])
    if row.get("state") != "success":
        err = scrub(row.get("error") or row.get("state") or "")
        if re.search(r"\b401\b|bad credentials", err, re.I):
            return failed("logged-out", err, _login_hint(_guess_protocol(root), expired=True))
        if row.get("state") == "timeout" or NET_ERR.search(err):
            return failed("error", err, ["GitHub unreachable, try again"])
        return failed("error", err, [f"gh can't use its github.com login ({err})",
                                     *_login_hint(_guess_protocol(root))])
    scopes = row.get("scopes") or []
    scopes = [s.strip() for s in (scopes.split(",") if isinstance(scopes, str) else scopes) if s.strip()]
    source, login = row.get("tokenSource") or None, row.get("login") or None
    hint = [f"an environment token ({source}) overrides the stored login: gh acts as {login} with it "
            f"(unset {source} to use the stored one)"] if source in ("GH_TOKEN", "GITHUB_TOKEN") else []
    return {**st, "state": "ready", "login": login, "protocol": row.get("gitProtocol") or "https",
            "scopes": scopes, "source": source, "hint": "\n".join(hint)}


def gh_line(st) -> str:
    """gh's state as wb prints it: "gh: ready as <login> over <protocol>", or the state and its hint."""
    hint = st["hint"].splitlines()
    if st["state"] == "ready":
        head = f"gh: ready as {st['login']} over {st['protocol']}"
    else:
        head = f"gh: {st['state']}" + (f": {hint.pop(0).strip()}" if hint else "")
    return "\n".join([head, *("  " + h.strip() for h in hint)])


# --- a patch's evidence (#1329, 2026-11-25) -----------------------------------------------------
# A weak driver's patch went upstream with no Scope trailer, a near-empty body and no test run,
# and upstream couldn't tell which driver prepared it (#1321, findings 20-21). So wb patch
# upstream says what's missing, runs the patch's own tests in its worktree and writes what they
# printed into the body, and stamps the pr/ commit with its driver: prints and facts, never a
# refusal (the user decides at the send).
EVIDENCE = "Tests run by wb:"
DRIVER_TRAILER = "Workbench-Driver"
TOOL_NAMES = {"claude": "claude-code", "opencode": "opencode", "agent": "agent", "terminal": "terminal"}


def _claude_model(session) -> str:
    """The model of the newest reply in Claude Code session ``session``'s transcript, "" for none."""
    path = spend.transcript_paths(session)[0]
    if path is None:
        return ""
    try:
        with open(path, "rb") as f:
            f.seek(max(0, f.seek(0, os.SEEK_END) - 262144))
            rows = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return ""
    for line in reversed(rows):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        msg = row.get("message") if isinstance(row, dict) and row.get("type") == "assistant" else None
        model = msg.get("model") if isinstance(msg, dict) else None
        if isinstance(model, str) and model and not model.startswith("<"):  # <synthetic>
            return model
    return ""


def driver_line() -> str:
    """`<tool> <model>` for the Workbench-Driver trailer: who runs this wb (store.caller_kind:
    claude-code, opencode, agent or terminal) and, when wb can read it, the model: Claude Code's
    from its session's transcript, an OpenCode driver's from its server's heartbeat, an agent's
    from its task."""
    kind = store.caller_kind()
    model = ""
    try:
        if kind == "claude":
            model = _claude_model(os.environ.get("CLAUDE_CODE_SESSION_ID", ""))
        elif kind == "opencode" and (sid := store.opencode_session()):
            model = str(store.driver_record(sid).get("model") or "")
        elif kind == "agent" and store.SESSION_RE.fullmatch(name := os.environ.get("WB_TASK", "")):
            model = str(store.Task(name).spec.get("model") or "")
    except (OSError, ValueError, TypeError, AttributeError):
        model = ""
    model = re.sub(r"\s+", "", model)[:120]
    return f"{TOOL_NAMES.get(kind, kind)} {model}".strip()


def stamp_driver(wt) -> str:
    """Amend ``wt``'s HEAD (the pr/ commit) with `Workbench-Driver: <driver_line>`, replacing one
    there: only the message changes, so its author, date, subject and `git patch-id --stable`
    stay, and _landed_pr and wb update's drop still match it. Returns what it says of it: the
    trailer's value, or why it isn't there (a fact for upstream, so a failure stops nothing)."""
    value = driver_line()
    try:
        msg = _git(wt, "log", "-1", "--format=%B", "HEAD").stdout
        new = subprocess.run(["git", *gitops.PLAIN, "interpret-trailers", "--if-exists", "replace",
                              "--trailer", f"{DRIVER_TRAILER}: {value}"], input=msg, capture_output=True,
                             check=True, cwd=wt).stdout
        with tempfile.NamedTemporaryFile(dir=wt.parent, prefix=".msg-", delete=False) as f:
            f.write(new)
        try:
            _git(wt, "-c", "commit.cleanup=verbatim", "commit", "-q", "--amend", "--no-verify",
                 "--allow-empty", "-F", f.name)
        finally:
            Path(f.name).unlink(missing_ok=True)
    except (gitops.GitError, subprocess.CalledProcessError, OSError) as e:
        return f"not added ({str(e).strip()[:200] or type(e).__name__})"
    return f"{value} (a trailer on {branch(wt)}'s commit, not the local one)"


def evidence(root, sha, wt) -> list[str]:
    """The `Tests run by wb:` lines of pr/<slug>: ``sha``'s own tests (as wb patch check picks
    them, each by its directory's runner) run in its worktree ``wt``, each `<command>` → the
    last line it printed; or `not run`, and why."""
    tests = [p for p in _touched(root, sha) if is_test(p)]
    if not tests:
        return [f"{EVIDENCE} not run (the patch has no test of its own)"]
    runs, why = _own_runs(wt, tests, _added_tests(root, sha, tests))
    lines = []
    with tempfile.TemporaryDirectory(prefix="wb-patch-home-") as home:
        env = {**os.environ, "WB_HOME": home, "PYTHONDONTWRITEBYTECODE": "1"}  # never the user's state
        for shown, argv, extra, skipped in runs:
            verdict, last = _trial(argv, wt, {**env, **extra}, skipped)
            lines.append(f"{EVIDENCE} `{shown}` → {last}" + ("" if verdict == "pass" else f" ({verdict})"))
    if runs:  # what they left (not ignored) is no change of the patch's, and send refuses a tree with any
        _git(wt, "clean", "-fdq", check=False)
    lines += [f"{EVIDENCE} not run: {w}" for w in why]
    return lines or [f"{EVIDENCE} not run (no test method found in {', '.join(tests)})"]


def with_evidence(text: str, lines) -> str:
    """The body ``text`` with ``lines`` in place of any `Tests run by wb:` lines it had: under its
    `## Tests run` heading (the template's), else at the end."""
    kept = [ln for ln in text.splitlines() if not ln.startswith(EVIDENCE)]
    block = ["", *lines]
    at = next((i for i, ln in enumerate(kept) if ln.strip().lower() == "## tests run"), None)
    if at is None:
        while kept and not kept[-1].strip():
            kept.pop()
        return "\n".join([*kept, *block]) + "\n"
    rest = kept[at + 1:]
    while rest and not rest[0].strip():
        rest.pop(0)
    return "\n".join([*kept[:at + 1], *block, "", *rest]) + "\n"


def _plain(text: str) -> str:
    """``text`` with its comments, the evidence lines and all whitespace gone: what was written."""
    text = re.sub(r"(?s)<!--.*?-->", "", text)
    return "".join(ln for ln in text.splitlines() if not ln.startswith(EVIDENCE)).replace(" ", "").replace("\t", "")


def _bare(text: str) -> list[str]:
    """``text``'s words but the evidence lines'."""
    return "\n".join(ln for ln in text.splitlines() if not ln.startswith(EVIDENCE)).split()


def _sections(text: str) -> dict[str, str]:
    """{a `## ` heading: what's under it, _plain}, in order."""
    out, head, rows = {}, None, []
    for ln in text.splitlines():
        if ln.startswith("## "):
            if head is not None:
                out[head] = _plain("\n".join(rows))
            head, rows = ln[3:].strip(), []
        elif head is not None:
            rows.append(ln)
    if head is not None:
        out[head] = _plain("\n".join(rows))
    return out


def unfilled(body: str, template: str) -> list[str]:
    """The template's sections that ``body`` lacks, leaves empty or leaves as the template has them."""
    mine = _sections(body)
    return [h for h, t in _sections(template).items() if not mine.get(h) or mine[h] == t]


def upstream(root, kind, commit, slug) -> None:
    """A branch pr/<slug> from fresh upstream with only ``commit``, in its own worktree, gh's
    state, and the one command that sends it (wb patch send, the user's yes): nothing is pushed
    or opened here."""
    _need_clone(kind)
    if not SLUG.fullmatch(slug):
        raise PatchError(f"--name {slug!r}: lowercase letters, digits and hyphens")
    sha = _out(root, "rev-parse", "-q", "--verify", f"{commit}^{{commit}}", check=False)
    if not sha:
        raise PatchError(f"{commit}: no such commit")
    pr, wt = f"pr/{slug}", store.home() / "upstream" / slug
    if _out(root, "rev-parse", "-q", "--verify", f"refs/heads/{pr}", check=False) or wt.exists():
        raise PatchError(f"{pr} is already prepared ({wt}): finish or remove it first "
                         f"(git -C {root} worktree remove {wt} && git -C {root} branch -D {pr})")
    remote = remote_of(root)
    try:
        _git(root, "fetch", "-q", "--", remote, env=_net_env(root), timeout=NET_SECS)
    except gitops.GitError as e:
        raise PatchError(f"couldn't fetch {remote}: {e}") from None
    up = _upstream(root, PATCH_BRANCH) or _upstream(root) or _out(
        root, "rev-parse", "-q", "--verify", f"refs/remotes/{remote}/HEAD", check=False)
    if not up:
        raise PatchError(f"no upstream to branch from: git -C {root} branch -u {remote}/main {PATCH_BRANCH}")
    wt.parent.mkdir(parents=True, exist_ok=True)
    _git(root, "worktree", "add", "-q", "-b", pr, str(wt), up)
    picked = _git(wt, "cherry-pick", sha, check=False)
    what = _subject(root, sha)
    url = _out(root, "config", "--get", f"remote.{remote}.url", check=False)
    repo = _owner_repo(url)  # gh takes owner/repo, never a URL (gh repo fork <url> was printed)
    scope = _out(root, "log", "-1", "--format=%(trailers:key=Scope,valueonly,separator=%x2C)", sha).lower()
    tag = " (Scope: me)" if scope == "me" else ""  # a fact; the dialog says it at the send (#1322)
    # what a rerun can't redo, recorded before the tests run: they can take minutes, and a run
    # killed there (Claude's Bash stops at 2) left pr/<slug>, which a rerun refuses, with no
    # prepared row for wb update and no body file (the #1329 review, 2026-11-25)
    note_install(root)
    with _noting() as noted:  # the local commit wb update later drops when its pull request merges
        _table(noted, "prepared")[slug] = _ident(root, sha)
    # the body outside the worktree: a file left in it is an uncommitted change send refuses
    body, template = body_path(slug), wt / ".github/pull_request_template.md"
    had = body.exists()
    if repo and not had and template.is_file():
        try:
            shutil.copyfile(template, body)
        except OSError as e:
            raise PatchError(f"{body}: {e.strerror or e}") from None
    if picked.returncode:
        print(f"{pr} from {remote}'s {up[:7]} in {wt}: {what}{tag} conflicts there. Resolve it in that "
              "worktree, then git cherry-pick --continue, and go on below")
        proof = [f"{EVIDENCE} not run (the patch conflicted on upstream: run its tests once it's resolved)"]
    else:
        # who prepared it, for upstream (#1329): a trailer on the pr/ commit only
        print(f"{pr} from {remote}'s {up[:7]} in {wt}, with {what}{tag}")
        print(f"{DRIVER_TRAILER}: {stamp_driver(wt)}")
        proof = evidence(root, sha, wt)
    if not scope:
        print("no Scope trailer: decide first whether it is for everybody (Scope: everybody) or a preference "
              "that stays a local patch (Scope: me), CONTRIBUTING.md section 1")
    if not has_test(root, sha):
        print(NO_TEST_SEND)
    print("\n".join(proof))
    # each directory's own suite (#1328): the kernel's check when the patch touches kernel/ (#1356:
    # the suite holds the committed bundle offline, but only kernel.py check rebuilds it and runs
    # its props), the mod's, and the OpenCode plugin's (it imports the mod's shared core)
    print("next (wb ran only the patch's own tests, above):" if proof[0].startswith(f"{EVIDENCE} `")
          else "next (none of this has run):")
    print(f"  cd {wt} && " + " && ".join(["python3 -m unittest", *suites(_touched(root, sha))]))
    if not repo:
        print(f"{remote} ({_shown_url(url)}) isn't on GitHub, and wb patch send sends only there: send it "
              "the way that project takes changes")
    else:
        try:  # the evidence in the body, under its Tests run (#1329): the driver fills in the rest
            text = body.read_text(encoding="utf-8", errors="replace") if body.exists() else ""
            body.write_text(with_evidence(text, proof), encoding="utf-8")
            empty = unfilled(text, template.read_text(encoding="utf-8", errors="replace")) \
                if template.is_file() else []
        except OSError as e:
            raise PatchError(f"{body}: {e.strerror or e}") from None
        print(f"  fill in {body} (" + ("a copy of .github/pull_request_template.md" if template.is_file()
                                       else "the pull request's body")
              + (f"; {'still ' if had else ''}unfilled: {', '.join(empty)}" if empty else "") + ")")
        print(f"  wb patch send {slug} --title {_quote(what.split(' ', 1)[-1])} --body-file {body}")
        print(gh_line(gh_state(root)))  # the user's yes is the mod's dialog (#1322: no line for it)
    print(f"once it merges, the next wb update drops the local patch ({what.split()[0]})")


ZIP_HINT = ("no GitHub account, or rather not? wb patch zip makes a .zip of the patch to email upstream "
            "yourself (it sends nothing)")
ZIP_README = """A patch to workbench, made by wb patch zip on {when} (UTC).

{count} commit(s), oldest first, a .patch file each:
{rows}

They apply to upstream's commit {base}.
To apply them, in a clone of workbench:
  git switch -c patch-{name} {base}
  git am <this folder>/*.patch

To send it upstream: email this .zip to {contact}, workbench's contact,
or send it any way you like. wb sent nothing.

The .patch files name each commit's author as git had them, in their From: lines
({authors}): edit them before sending if you'd rather not.
"""


def zip_out() -> Path:
    """Where wb patch zip writes: ~/Downloads when it exists, else $WB_HOME/patches/out/."""
    downloads = Path.home() / "Downloads"
    return downloads if downloads.is_dir() else store.home() / "patches" / "out"


def zip_patches(root, kind, commits=(), name=None) -> Path:
    """wb patch zip (#1291): ``commits`` (else every commit on PATCH_BRANCH its upstream lacks,
    what wb patch upstream would send) as one format-patch file each in a .zip, with README.txt
    (the subjects, the upstream commit they apply to, git am, UPSTREAM_CONTACT), for a user with
    no GitHub account to email upstream themselves. The owner, 2026-11-24: "a lot of people dont
    have a github account and dont want to bother". Local only: no fetch, no gh, nothing sent,
    so the mod's consent gate doesn't ask about it."""
    _need_clone(kind)
    if name is not None and not SLUG.fullmatch(name):
        raise PatchError(f"--name {name!r}: lowercase letters, digits and hyphens")
    ref = f"refs/heads/{PATCH_BRANCH}"
    if not _out(root, "rev-parse", "-q", "--verify", ref, check=False):
        raise PatchError(f"{root} has no branch {PATCH_BRANCH}: patches are commits on it "
                         f"(git -C {root} switch -c {PATCH_BRANCH} --track origin/main)")
    if line := track_upstream(root):
        print(line)
    up = _upstream(root, PATCH_BRANCH)
    if not up:
        raise PatchError(f"{PATCH_BRANCH} tracks no upstream: git -C {root} branch -u origin/main {PATCH_BRANCH}")
    mine = _commits(root, f"{up}..{ref}")  # no fetch: the upstream wb update last fetched
    if not mine:
        raise PatchError(f"no patches on {PATCH_BRANCH}: it is upstream's (wb patch list)")
    if commits:
        order = {sha: i for i, (sha, _) in enumerate(mine)}
        picked = set()
        for c in commits:
            sha = _out(root, "rev-parse", "-q", "--verify", f"{c}^{{commit}}", check=False)
            if not sha:
                raise PatchError(f"{c}: no such commit")
            if sha not in order:
                raise PatchError(f"{c} isn't one of {PATCH_BRANCH}'s patches (wb patch list)")
            picked.add(sha)
        mine = [row for row in mine if row[0] in picked]  # in local's order, as git am applies them
    base = _out(root, "merge-base", ref, up)
    authors = list(dict.fromkeys(_out(root, "log", "-1", "--format=%an <%ae>", sha) for sha, _ in mine))
    stamp = time.gmtime()
    stem = f"workbench-patch-{name or mine[-1][0][:7]}-{time.strftime('%Y%m%d', stamp)}"
    out = zip_out()
    out.mkdir(parents=True, exist_ok=True)
    readme = ZIP_README.format(
        when=time.strftime("%Y-%m-%d %H:%M", stamp), count=len(mine), base=base, name=name or mine[-1][0][:7],
        rows="\n".join(f"  {sha[:7]} {subject}" for sha, subject in mine), contact=UPSTREAM_CONTACT,
        authors=", ".join(authors))
    with tempfile.TemporaryDirectory() as made:
        for i, (sha, _) in enumerate(mine, 1):
            _git(root, *DIFF_PLAIN, "format-patch", "-q", "--no-signature", "-1", f"--start-number={i}",
                 "-o", made, sha)
        fd, part = tempfile.mkstemp(prefix=".wb-patch-", suffix=".part", dir=out)
        try:
            with os.fdopen(fd, "wb") as f, zipfile.ZipFile(f, "w", zipfile.ZIP_DEFLATED) as z:
                z.writestr(f"{stem}/README.txt", readme)
                for patch in sorted(Path(made).iterdir()):
                    z.write(patch, f"{stem}/{patch.name}")
            n, dest = 1, out / f"{stem}.zip"
            while dest.exists():  # a second zip the same day keeps the first
                n += 1
                dest = out / f"{stem}-{n}.zip"
            os.replace(part, dest)
        finally:
            Path(part).unlink(missing_ok=True)
    print(f"wrote {dest}: {len(mine)} patch(es) on upstream's {base[:7]}")
    print(f"email it to {UPSTREAM_CONTACT}, or send it any way you like; nothing was sent")
    print(f"the patch files name the commit author ({', '.join(authors)}); edit them before sending if "
          "you'd rather not")
    return dest


def body_path(slug) -> Path:
    return store.home() / "upstream" / f"{slug}.md"


def _quote(text) -> str:
    """``text`` as one shell word: in double quotes when that needs no escaping, else shlex's."""
    return f'"{text}"' if not re.search(r'["$`\\!]', text) else shlex.quote(text)


def _refuse_send(root, wt, pr, base, body_file) -> None:
    """Every refusal of wb patch send that needs no network, before any is asked (#1005). A
    `Scope: me` commit is none: the mod's dialog says it, one yes (#1322: --anyway was a second)."""
    if not (_out(root, "rev-parse", "-q", "--verify", f"refs/heads/{pr}", check=False) and wt.is_dir()
            and branch(wt) == pr):
        raise PatchError(f"no {pr} in {wt}: wb patch upstream <commit> --name {pr.removeprefix('pr/')} "
                         "prepares it")
    if _out(wt, "rev-parse", "-q", "--verify", "CHERRY_PICK_HEAD", check=False):
        raise PatchError(f"a cherry-pick is in progress in {wt}: resolve it, then git cherry-pick --continue")
    if dirty := _out(wt, "status", "--porcelain", "--untracked-files=normal").splitlines():
        raise PatchError(f"{wt} has uncommitted changes ({', '.join(d.strip() for d in dirty[:5])}): "
                         "commit them on the branch, or remove them")
    if not _out(wt, "log", "--format=%h", f"{base}..HEAD", "--"):
        raise PatchError(f"{pr} has no commits over {base.removeprefix('refs/remotes/')}: nothing to send")
    body = Path(body_file)
    try:
        text = body.read_bytes()
    except OSError as e:
        raise PatchError(f"--body-file {body}: {e.strerror or e}") from None
    template = wt / ".github/pull_request_template.md"
    if not text.strip():
        raise PatchError(f"--body-file {body} is empty: fill it in from {template}")
    # the evidence wb patch upstream wrote in is no filling-in (#1329)
    if template.is_file() and _bare(text.decode("utf-8", "replace")) == _bare(
            template.read_text(encoding="utf-8", errors="replace")):
        raise PatchError(f"--body-file {body} is the template as it came: fill in every section first")


SEND_OK = "send-ok.json"  # $WB_HOME/send-ok.json: the mod's dialog approved this one send
SEND_OK_SECS = 120  # the dialog's yes is for the call it let through, run at once


def _approved(slug, title, body_file, draft, anyway) -> bool:
    """Whether the mod's dialog approved exactly this send just now. Under Claude Code (its Bash
    sets CLAUDECODE=1) the dialog is the user's yes; with the mod missing, disabled or its hooks
    failing to load, nothing asked them, and Claude's Bash ran the fork, push and pull request
    regardless (#1005, 2026-11-22). The gate writes $WB_HOME/send-ok.json on "Send it": the
    slug, title, body file and its text, the flags and when. One send consumes it, matching or
    not: renamed away first, so two sends can't both read it."""
    ok = store.take_approval(SEND_OK, SEND_OK_SECS)
    if ok is None:
        return False
    try:
        text = Path(body_file).read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return False
    return (ok.get("slug"), ok.get("title"), ok.get("body_file"), ok.get("body"),
            bool(ok.get("draft")), bool(ok.get("anyway"))) == (
        slug, title, os.path.abspath(body_file), text, draft, anyway)


def _json(text):
    try:
        return json.loads(text or "null")
    except ValueError:
        return None


def _fork_of(repo, login, said) -> str:
    """The user's fork of ``repo``, owner/name. gh repo fork names it only when it was there
    already ("<owner/name> already exists"; nothing for a new one without a terminal), and a
    fork the user renamed, or one GitHub named <name>-1 beside an unrelated repository of
    theirs, isn't <login>/<name>: wb retried for 30 s, or pushed into that other repository
    (#1005, 2026-11-22). So it asks GitHub which it is; PatchError when it can't tell."""
    if m := re.search(r"([\w.-]+/[\w.-]+) already exists", said):
        return m.group(1)
    guess = f"{login}/{repo.split('/', 1)[1]}"
    r = _gh("api", f"repos/{guess}", timeout=GH_SECS)
    info = _json(r.stdout) if not r.returncode else None
    if isinstance(info, dict):
        if str((info.get("parent") or {}).get("full_name", "")).lower() == repo.lower():
            return str(info.get("full_name") or guess)
    elif not re.search(r"\b404\b|not found", r.stderr or r.stdout or "", re.I):
        raise PatchError(f"gh api repos/{guess}: {scrub(r.stderr or r.stdout) or f'exit {r.returncode}'}")
    r = _gh("api", f"repos/{repo}/forks?sort=newest&per_page=100", timeout=GH_SECS)
    rows = _json(r.stdout) if not r.returncode else None
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and str((row.get("owner") or {}).get("login", "")).lower() == login.lower():
            return str(row.get("full_name"))
    if isinstance(info, dict):  # never a push into a repository that isn't the fork
        raise PatchError(f"{guess} isn't a fork of {repo}, and GitHub lists no fork of {login}'s")
    return guess  # not listed yet: a new fork appears asynchronously, and the push waits for it


def send(root, kind, slug, title, body_file, draft=False, anyway=False) -> None:
    """The turnkey send of a branch wb patch upstream prepared: the refusals first, then gh's
    state, then a fork (unless the user owns upstream), the push and the pull request. It is
    the command the mod's consent gate asks the user about (under Claude Code it refuses what
    the gate didn't approve: `_approved`), and outside Claude Code the user typing it is the
    consent: nothing else in wb forks, pushes or opens anything (#1005)."""
    _need_clone(kind)
    if not SLUG.fullmatch(slug):
        raise PatchError(f"{slug!r}: a slug is lowercase letters, digits and hyphens")
    if not title.strip():
        raise PatchError("--title is empty")
    pr, wt = f"pr/{slug}", store.home() / "upstream" / slug
    remote, target = _tracked(root)
    base = f"refs/remotes/{remote}/{target}"
    _refuse_send(root, wt, pr, base, body_file)
    template = wt / ".github/pull_request_template.md"
    if template.is_file() and (empty := unfilled(Path(body_file).read_text(encoding="utf-8", errors="replace"),
                                                 template.read_text(encoding="utf-8", errors="replace"))):
        print(f"note: the body leaves {', '.join(empty)} unfilled")  # a print, never a refusal (#1329)
    url = _out(root, "config", "--get", f"remote.{remote}.url", check=False)
    repo = _owner_repo(url)
    if not repo:
        raise PatchError(f"{remote} ({_shown_url(url)}) isn't on GitHub: wb patch send sends only there")
    st = gh_state(root)
    if st["state"] != "ready":
        # no gh, or no login: the zip is the way without GitHub (#1291)
        raise PatchError("gh can't send it yet, nothing was sent:\n" + gh_line(st)
                         + (f"\n{ZIP_HINT}" if st["state"] in ("missing", "logged-out") else ""))
    login, protocol = st["login"], st["protocol"]
    workflows = _out(wt, "diff", "--name-only", f"{base}...HEAD", "--", ".github/workflows/")
    if workflows and protocol == "https" and "workflow" not in st["scopes"]:
        raise PatchError(f"{pr} changes .github/workflows/, and GitHub refuses that push over https without "
                         "the token's workflow scope, so nothing was sent: "
                         + _user_runs("gh auth refresh -h github.com -s workflow", sep=" "))
    # an OpenCode driver's shell too (#1309), and an agent's (#1310): store.under_model. Each
    # sent with no one asked
    if store.under_model() and not _approved(slug, title, body_file, draft, anyway):
        again = " ".join(["wb patch send", slug, "--title", _quote(title), "--body-file",
                          shlex.quote(os.path.abspath(body_file)), *(["--draft"] if draft else []),
                          *(["--anyway"] if anyway else [])])
        if store.caller_kind() == "agent":  # its reviewer or the user decides (#1310)
            raise PatchError("in an agent's shell wb patch send goes only on the user's yes in the driver's "
                             "dialog, and the user approved none for this send, so nothing was sent. Ask "
                             "the reviewer (ask_reviewer); or the user runs it in a terminal of their own: "
                             f"{again}")
        if store.caller_kind() == "opencode":
            raise PatchError("in OpenCode wb patch send goes only through the workbench plugin's dialog, "
                             "and the user approved none for this send, so nothing was sent (the plugin's "
                             "workbench_patch_send tool asks them; is the plugin installed and loaded?). Or the "
                             f"user runs it in a terminal of their own, outside OpenCode: {again}")
        raise PatchError("in Claude Code wb patch send goes only through the workbench mod's dialog, and "
                         "the user approved none for this send, so nothing was sent (is the mod installed "
                         "and loaded? wb doctor says). Or the user runs it in a terminal of their own, "
                         f"outside Claude Code: {again}")
    note_install(root)
    owner = repo.split("/", 1)[0]
    done = []

    def failed(step, err):
        said = f"; done already: {', '.join(done)}" if done else "; nothing was sent"
        return PatchError(f"{step} failed: {scrub(err) or 'no message'}{said}" +
                          (f" (wb patch send {slug} again goes on from there)" if done else ""))
    if login and login.lower() == owner.lower():  # the user's own repository: no fork
        dest, where = remote, repo
    else:
        try:
            r = _gh("repo", "fork", repo, "--clone=false", "--remote=false", cwd=wt)  # --remote renames origin
        except (OSError, subprocess.TimeoutExpired) as e:
            raise failed("gh repo fork", str(e)) from None
        if r.returncode:
            raise failed("gh repo fork", r.stderr or r.stdout)
        done.append(f"forked {repo}")
        try:
            where = _fork_of(repo, login, f"{r.stderr}\n{r.stdout}")
        except (OSError, subprocess.TimeoutExpired, PatchError) as e:
            raise failed("finding the fork", str(e)) from None
        done[-1] = f"forked {repo} to {where}"
        dest = f"git@github.com:{where}.git" if protocol == "ssh" else f"https://github.com/{where}.git"
    for wait in (*PUSH_WAITS, None):
        try:
            r = _git(root, "push", "-q", "--", dest, f"{pr}:refs/heads/{pr}", check=False,
                     env=_net_env(root), timeout=NET_SECS)
        except gitops.GitError as e:  # no answer in NET_SECS: still a failed step, said as one
            raise failed(f"git push to {where}", str(e)) from None
        err = scrub(r.stderr or r.stdout)
        # only a fork not there yet is waited for: a refused login or a rejected push fails now
        if not r.returncode or wait is None or dest == remote or not re.search(
                r"not found|does not exist|does not appear to be a git repository", err, re.I):
            break
        time.sleep(wait)
    if r.returncode:
        if protocol == "https":
            err += ("; if git has no login for github.com over https: " + _user_runs("gh auth setup-git", sep=" ") + " "
                    "(it edits the user's global git config: ask first)")
        raise failed(f"git push to {where}", err)
    done.append(f"pushed {pr} to {where}")
    print(f"pushed {pr} to {where}" + (f" (forked from {repo})" if where != repo else ""))
    args = ["pr", "create", "-R", repo, "--base", target, "--head", f"{where.split('/')[0]}:{pr}",
            "--title", title, "--body-file", os.path.abspath(body_file)] + (["--draft"] if draft else [])
    try:
        r = _gh(*args, cwd=wt)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise failed("gh pr create", str(e)) from None
    links = re.findall(r"https://github\.com/\S+/pull/\d+", f"{r.stdout}\n{r.stderr}")
    if r.returncode and links and "already exists" in f"{r.stdout}{r.stderr}":
        # opened by an earlier send whose answer was lost (a timeout): a rerun was promised to
        # go on from there, and failed on it forever (#1005, 2026-11-22)
        print(f"opened {links[-1]} (already open)")
    elif r.returncode:
        raise failed("gh pr create", r.stderr or r.stdout)
    else:
        print(f"opened {links[-1] if links else scrub(r.stdout) or 'the pull request'}"
              + (" (a draft)" if draft else ""))
    if links:
        note_sent(root, slug, repo, links[-1])
    print("once it merges, the next wb update drops the local patch")


def note_sent(root, slug, repo, url) -> None:
    """patches.json's `sent` row for pr/<slug>'s pull request: its repository, number and URL,
    and the ident of the local commit it carries, which wb update drops once it merged (a
    squashed, reworded or edited merge left the patch to conflict or double up: #1005,
    2026-11-22). The ident is wb patch upstream's `prepared` row; a branch prepared before it
    kept one, the first commit pr/<slug> got after its cut."""
    number = int(url.rsplit("/", 1)[-1])
    with _noting() as noted:
        ident = (noted.get("prepared") or {}).get(slug) if isinstance(noted.get("prepared"), dict) else None
        if _ident_row(ident) is None:
            log = _out(root, "log", "-g", "--format=%H", f"refs/heads/pr/{slug}", "--", check=False).splitlines()
            first = _out(root, "rev-list", "--reverse", f"{log[-1]}..refs/heads/pr/{slug}", check=False).split() if log else []
            ident = _ident(root, first[0]) if first else None
        if ident is not None:
            _table(noted, "sent")[slug] = {"repo": repo, "pr": number, "url": url,
                                           **{k: ident[k] for k in ("author", "email", "date", "subject")}}


# --- what became of a sent pull request (#1005, 2026-11-22) -------------------------------------
class Settled(NamedTuple):
    skip: set      # the local commits whose pull request merged: rebase_patches leaves them out
    lines: list    # said at once: GitHub not asked
    dropped: list  # said once the branch moved: "your pull request #<n> merged: dropped …"
    closed: list   # (slug, number, subject, ident row): closed unmerged, said by `closed_lines`


def _pr_state(repo, number, up, root) -> tuple[str | None, str]:
    """gh pr view's state of ``repo``'s pull request ``number``: MERGED, CLOSED, OPEN, PENDING (merged,
    but its merge commit isn't in the fetched upstream ``up`` yet: dropping the patch then left the
    user without the fix until the next fetch), or None with why GitHub wasn't asked, scrubbed."""
    if shutil.which("gh") is None:
        return None, "gh isn't installed"
    try:
        r = _gh("pr", "view", str(number), "-R", repo, "--json", "state,mergeCommit", timeout=GH_SECS)
    except subprocess.TimeoutExpired:
        return None, f"no answer in {GH_SECS}s"
    except OSError as e:
        return None, scrub(str(e))[:120]
    info = _json(r.stdout) if not r.returncode else None
    state = info.get("state") if isinstance(info, dict) else None
    if state not in ("MERGED", "CLOSED", "OPEN"):
        return None, (scrub(r.stderr or r.stdout) or f"exit {r.returncode}")[:120]
    merge = info.get("mergeCommit")
    oid = merge.get("oid") if isinstance(merge, dict) else None
    if state == "MERGED" and isinstance(oid, str) and oid and not (up and _git(
            root, "merge-base", "--is-ancestor", oid, up, check=False).returncode == 0):
        return "PENDING", ""
    return state, ""


def settle_sent(root) -> Settled:
    """wb update, on PATCH_BRANCH, before the rebase (never --check: the mod runs that on a timer,
    and it never asks GitHub): each pull request wb patch send opened, asked of gh once,
    GH_SECS at most. Merged: its local commit (by ident) goes in ``skip``, and the row is marked
    merged so a later run needn't ask again; `retire_sent` removes it once the commit is gone.
    Closed: said once, after the move (`closed_lines`). Open: nothing. Not asked: one line, the
    patch kept."""
    sent = store.read_json(noted_path(), {})
    sent = sent.get("sent") if isinstance(sent, dict) else None
    if not isinstance(sent, dict) or not sent:
        return Settled(set(), [], [], [])
    up = _upstream(root)
    mine = _sha_rows(root, f"{up}..HEAD") if up else []
    skip, lines, dropped, closed, marks = set(), [], [], [], {}
    for slug, row in sent.items():
        ident = _ident_row(row) if isinstance(row, dict) else None
        if ident is None or not isinstance(row.get("pr"), int) or not isinstance(row.get("repo"), str):
            continue
        n, subject = row["pr"], row["subject"]
        sha = next((s for s, r in mine if r == ident), None)
        if not row.get("merged"):
            state, why = _pr_state(row["repo"], n, up, root)
            if state is None:
                lines.append(f"couldn't ask GitHub about pull request #{n} ({why}): kept the local patch")
                continue
            if state in ("OPEN", "PENDING"):
                if row.get("closed_said"):  # reopened: a later close is said again
                    marks[slug] = {"closed_said": False}
                continue
            if state == "CLOSED":
                if not sha:  # no local patch left to keep: nothing to say, nothing to watch
                    marks[slug] = None
                elif not row.get("closed_said"):
                    closed.append((slug, n, subject, ident))
                continue
            marks[slug] = {"merged": True}
        if sha:
            skip.add(sha)
            dropped.append(f"your pull request #{n} merged: dropped the local patch {subject}")
    if marks:
        with _noting() as noted:
            table = _table(noted, "sent")
            for slug, change in marks.items():
                if change is None:
                    table.pop(slug, None)
                elif isinstance(table.get(slug), dict):
                    table[slug].update(change)
    return Settled(skip, lines, dropped, closed)


def closed_lines(root, settled) -> list[str]:
    """wb update's end: each pull request closed unmerged, said once (`closed_said`), naming its
    patch's sha as it is now: the one settle_sent saw was gone after the rebase, and wb patch
    check refused it (#1005, 2026-11-22)."""
    up = _upstream(root)
    mine = _sha_rows(root, f"{up}..HEAD") if up and settled.closed else []
    lines, said = [], []
    for slug, n, subject, ident in settled.closed:
        sha = next((s for s, r in mine if r == ident), None)
        if sha:
            lines.append(f"your pull request #{n} was closed without merging: kept the local patch {subject} "
                         f"(wb patch check {_out(root, 'rev-parse', '--short', sha)} tells whether upstream "
                         "fixed it another way)")
            said.append(slug)
    if said:
        with _noting() as noted:
            table = _table(noted, "sent")
            for slug in said:
                if isinstance(table.get(slug), dict):
                    table[slug]["closed_said"] = True
    return lines


def retire_sent(root) -> list[str]:
    """wb update's end: each merged pull request (`settle_sent` marked it) whose local commit is
    gone from PATCH_BRANCH forgotten, its pr/<slug> retired as retire_landed does. Only after the
    branch moved: a run that died before (another patch's conflict) left the row to drop the
    commit next time, where forgetting it first would have left it to conflict for good."""
    sent = store.read_json(noted_path(), {})
    sent = sent.get("sent") if isinstance(sent, dict) else None
    if not isinstance(sent, dict):
        return []
    up = _upstream(root, PATCH_BRANCH)
    mine = {r for _, r in _sha_rows(root, f"{up}..refs/heads/{PATCH_BRANCH}")} if up else set()
    held_here, lines = dict(held(root)), []
    for slug, row in list(sent.items()):
        if not (isinstance(row, dict) and row.get("merged")) or _ident_row(row) in mine:
            continue
        if slug in held_here:
            lines.append(_retire(root, slug, held_here[slug], f"pull request #{row.get('pr')} merged"))
        else:
            body_path(slug).unlink(missing_ok=True)
            _forget(slug)
    return lines


def doctor_lines(root, kind) -> list[str]:
    """wb doctor's install line, its local patches, and gh's state (#1005). Local git only: gh,
    which asks GitHub, only when there are patches or a branch prepared for upstream."""
    b = branch(root) or "a detached HEAD"
    if kind == "marketplace":
        lines = [f"ok  install: marketplace at {root}"]
        found = local_changes(root)
        if found:
            lines.append(f"--  local patches: the marketplace clone has {found.say()}, which Claude Code's "
                         "update re-clones away: wb patch init keeps them")
        patched = bool(found)
    else:
        noted = store.read_json(store.home() / "patches.json", {}) or {}
        own = b == PATCH_BRANCH or (isinstance(noted, dict) and bool(noted.get("source"))
                                    and Path(str(noted["source"])).resolve() == Path(root).resolve())
        lines = [f"ok  install: your own clone at {root}, branch {b} (wb patch init)" if own
                 else f"ok  install: developer clone at {root}, branch {b}"]
        rows = _patches(root) if b == PATCH_BRANCH and _upstream(root) else []
        if b == PATCH_BRANCH and (fix := no_upstream(root)):  # its patches unseen, wb update failing (#1327)
            lines.append(f"--  local patches: {fix} (wb update sets it)")
        if rows:
            lines.append(f"--  local patches: {len(rows)} ("
                         + ", ".join(scope or "no Scope" for _, _, scope in rows) + ")")
        patched = bool(rows)
    # a branch that landed upstream sends nothing more: doctor asked GitHub on every run for
    # one, up to 20 s offline, until wb update cleared them (#1005, 2026-11-22)
    up = f"refs/remotes/{'/'.join(_tracked(root))}" if kind == "clone" else None
    if patched or any(not _landed_pr(root, slug, up) for slug, _ in held(root)):
        st = gh_state(root)
        lines.append(("ok  " if st["state"] == "ready" else "--  ") + gh_line(st).replace("\n", "\n    "))
    return lines


def held(root) -> list[tuple[str, Path | None]]:
    """wb patch upstream's branches: (slug, its worktree or None), only a pr/<slug> whose worktree
    is $WB_HOME/upstream/<slug> or whose body file is there; a developer's own pr/ branches
    aren't wb's to touch."""
    trees, home, out = _worktrees(root), (store.home() / "upstream").resolve(), []
    for pr in _out(root, "for-each-ref", "--format=%(refname:short)", "refs/heads/pr/").splitlines():
        slug, wt = pr.removeprefix("pr/"), trees.get(pr)
        if (wt and Path(wt).resolve() == home / slug) or body_path(slug).exists():
            out.append((slug, Path(wt) if wt else None))
    return out


def _worktrees(root) -> dict:
    """{branch: worktree path} of root's worktrees."""
    trees = {}
    for block in _out(root, "worktree", "list", "--porcelain").split("\n\n"):
        fields = dict(line.split(" ", 1) for line in block.splitlines() if " " in line)
        if fields.get("branch", "").startswith("refs/heads/"):
            trees[fields["branch"].removeprefix("refs/heads/")] = fields.get("worktree")
    return trees


def _landed_pr(root, slug, up) -> bool:
    """Whether every commit pr/<slug> added since wb patch upstream cut it is upstream's (``up``):
    merged as it was, rebased (same author, date and subject, as `rebase_patches` tells), or
    squashed into the same diff. The cut is the branch's first reflog entry; with no reflog,
    or nothing committed yet (a cherry-pick still in conflict), it hasn't landed."""
    pr = f"refs/heads/pr/{slug}"
    if not up or not _out(root, "rev-parse", "-q", "--verify", up, check=False):
        return False
    log = _out(root, "log", "-g", "--format=%H", pr, "--", check=False).splitlines()
    if not log:
        return False
    cut = log[-1]
    mine = _ident_rows(root, f"{cut}..{pr}")
    if not mine:
        return False
    if _git(root, "merge-base", "--is-ancestor", pr, up, check=False).returncode == 0:
        return True
    cherry = _out(root, "cherry", up, pr, cut, check=False).splitlines()
    if cherry and all(r.startswith("-") for r in cherry):
        return True
    theirs = set(_ident_rows(root, f"{cut}..{up}"))
    return all(row in theirs for row in mine)


def retire_landed(root) -> list[str]:
    """wb update: each pr/<slug> that landed upstream removed, its worktree and body file too;
    they stayed for good, wb patch list showing them and doctor asking gh for them, and the slug
    refused (#1005, 2026-11-22). A worktree with changes of its own is kept, and said."""
    up = f"refs/remotes/{'/'.join(_tracked(root))}"
    return [_retire(root, slug, wt, "landed upstream") for slug, wt in held(root) if _landed_pr(root, slug, up)]


def _retire(root, slug, wt, why) -> str:
    """pr/<slug>'s worktree, branch and body file removed, and its patches.json rows; a worktree
    with changes of its own (or one git won't remove) is kept, and said."""
    if wt and wt.is_dir():
        if _out(wt, "status", "--porcelain", check=False) or _out(
                wt, "rev-parse", "-q", "--verify", "CHERRY_PICK_HEAD", check=False):
            _forget(slug)
            return f"pr/{slug} {why}; {wt} has changes of its own, so it is kept"
        if _git(root, "worktree", "remove", str(wt), check=False).returncode:
            _forget(slug)
            return f"pr/{slug} {why}; git couldn't remove {wt}, so it is kept"
    _git(root, "worktree", "prune", check=False)
    _git(root, "branch", "-q", "-D", f"pr/{slug}", check=False)
    body_path(slug).unlink(missing_ok=True)
    _forget(slug)
    return f"{why}, removed pr/{slug}, its worktree and its body file"


def _same_origin(a: str, b: str) -> bool:
    norm = lambda u: u.strip().rstrip("/").removesuffix(".git").lower()  # noqa: E731
    return norm(a) == norm(b)


def _run(step, cwd) -> str | None:
    """A step after the switch: its error, or None. ``cwd`` is the new clone: the old one is gone."""
    try:
        r = subprocess.run(step, capture_output=True, text=True, timeout=300, cwd=cwd,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        return str(e)
    return (r.stderr or r.stdout).strip()[-300:] or f"exit {r.returncode}" if r.returncode else None


def init(root: Path, dest: Path) -> None:
    """Move a marketplace install to a full clone the user owns, on PATCH_BRANCH, the marketplace
    clone's own changes carried over as its first patches; then switch Claude Code's marketplace
    to it, repoint wb and the skill, and refresh the mod. Every refusal comes before any change.
    Claude Code deletes ``root`` at the switch: after it only the new clone's programs run."""
    dest = Path(os.path.abspath(dest))
    if dest.resolve().is_relative_to(root.resolve().parent):
        raise PatchError(f"{dest} is inside Claude Code's marketplaces directory, which it re-clones: "
                         "choose another --dir")
    url = _out(root, "config", "--get", "remote.origin.url", check=False)
    if not url:
        raise PatchError(f"the marketplace clone {root} has no origin to clone")
    found = local_changes(root)
    if (found.commits or found.dirty) and not all(_out(root, "var", v, check=False) for v in ("GIT_AUTHOR_IDENT", "GIT_COMMITTER_IDENT")):
        raise PatchError("git has no user.name/user.email: the carried-over patches are committed as you "
                         "(git config --global user.name/user.email)")
    b = branch(root)
    track = (_out(root, "config", f"branch.{b}.merge", check=False) if b else "").removeprefix("refs/heads/") or b
    reuse = dest.exists() and (not dest.is_dir() or any(dest.iterdir()))
    if reuse:
        top = _out(dest, "rev-parse", "--show-toplevel", check=False) if dest.is_dir() else ""
        theirs = _out(dest, "config", "--get", "remote.origin.url", check=False) if top else ""
        if not top or Path(top).resolve() != dest.resolve() or not _same_origin(theirs, url):
            raise PatchError(f"{dest} isn't empty and isn't a clone of {url}: choose another --dir (or WB_SOURCE)")
    saved = rescue(root, found) if found else None
    keep = f" (saved in {saved})" if saved else ""
    made, carried, was = False, 0, None
    try:
        if reuse:
            _git(dest, "fetch", "-q", "origin", env=_net_env(dest), timeout=NET_SECS)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            made = True
            _git(dest.parent, "clone", "-q", "--", url, str(dest), env=_net_env(None), timeout=NET_SECS)
        track = track or _out(dest, "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD",
                              check=False).removeprefix("origin/") or "main"
        if _out(dest, "rev-parse", "-q", "--verify", f"refs/heads/{PATCH_BRANCH}", check=False):
            if branch(dest) != PATCH_BRANCH:
                _git(dest, "switch", "-q", PATCH_BRANCH)  # git refuses it over changes of the user's
            print(f"{dest} already has branch {PATCH_BRANCH}: kept as it is, nothing carried over{keep}")
            if line := track_upstream(dest):
                print(line)
        else:
            if reuse and _out(dest, "status", "--porcelain", "--untracked-files=all"):
                raise PatchError(f"{dest} has uncommitted changes: commit or stash them, then wb patch init")
            have = _out(dest, "rev-parse", "-q", "--verify", f"{found.base}^{{commit}}", check=False)
            if reuse:  # where DIR was, to go back to if the carry-over fails
                was = (branch(dest), _out(dest, "rev-parse", "HEAD"))
            _git(dest, "switch", "-q", "--no-track", "-c", PATCH_BRANCH, have or f"origin/{track}")
            _git(dest, "branch", "-q", f"--set-upstream-to=origin/{track}", PATCH_BRANCH)
            files = sorted(str(p) for p in saved.glob("*.patch")) if saved else []
            if files and _git(dest, "am", "-q", "--3way", *files, check=False).returncode:
                _git(dest, "am", "--abort", check=False)
                raise PatchError(f"the marketplace clone's commits don't apply in {dest}")
            carried = len(files)
            diff = saved / "uncommitted.diff" if saved else None
            if diff and diff.exists() and diff.stat().st_size:
                if _git(dest, "apply", "--index", str(diff), check=False).returncode:
                    raise PatchError(f"the marketplace clone's uncommitted changes don't apply in {dest}")
                _git(dest, "commit", "-q", "-m", CARRIED)
                carried += 1
    except (PatchError, gitops.GitError) as e:
        if made:
            shutil.rmtree(dest, ignore_errors=True)
        elif was:  # a reused DIR: the branch this run made goes, or the rerun found `local`
            # there, carried nothing, and switched Claude Code over with the changes only in
            # rescued/ (#1005, 2026-11-22). DIR was clean before: what the reset drops is ours
            _git(dest, "reset", "-q", "--hard", check=False)
            _git(dest, "checkout", "-q", *([was[0]] if was[0] else ["--detach", was[1]]), check=False)
            if branch(dest) != PATCH_BRANCH:
                _git(dest, "branch", "-q", "-D", PATCH_BRANCH, check=False)
        raise PatchError(f"{e}; nothing else changed: the marketplace install is as it was{keep}") from None
    # OpenCode's plugin, when it loads from (or runs the wb of) the clone Claude Code deletes at
    # the switch, moves to the new one too: init re-linked Claude Code's side alone (#1321, N2)
    opencode = ocplugin.loaded_from(root)
    for step in (["claude", "plugin", "marketplace", "add", str(dest)], [str(dest / "bin/wb"), "setup", "--force"],
                 ["claude", "plugin", "update", "workbench@workbench"],
                 *([[str(dest / "bin/wb"), "setup", "--opencode", "--no-service"]] if opencode else [])):
        err = _run(step, dest)
        if err is not None:
            done = (f"; {dest} is ready and nothing else changed: wb patch init --dir {dest} again goes on "
                    "from there" if step[0] == "claude" and step[3] == "add" else
                    f"; Claude Code runs from {dest} now, so finish OpenCode's side by hand: {dest}/bin/wb "
                    "setup --opencode" if "--opencode" in step else
                    f"; Claude Code's marketplace is {dest} already, so finish by hand: "
                    f"{dest}/bin/wb setup --force && claude plugin update workbench@workbench")
            raise PatchError(f"{' '.join(Path(step[0]).name if i == 0 else s for i, s in enumerate(step[:4]))} "
                             f"failed: {err}{done}{keep}")
    note_install(dest)
    print(f"workbench runs from {dest} now, branch {PATCH_BRANCH} tracking origin/{track}; "
          f"{carried} patch(es) carried over from the marketplace clone{keep}")
    if found.stashes or found.branches:  # the marketplace clone is gone: rescued/ is their only copy
        what = [f"{len(found.stashes)} stash(es) (stash-<n>.diff)"] if found.stashes else []
        what += [f"branch {b} (branch-{b.replace('/', '-')}/)" for b in found.branches]
        print(f"not carried over, kept only in {saved}: {', '.join(what)}")
    print("patches are commits on it (wb patch list); wb update rebases them onto upstream's news. "
          "/reload-plugins (or a restart) loads the mod from it"
          + ("; OpenCode loads its plugin from it too" if opencode else ""))
