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
import os
import re
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import NamedTuple

from . import gitops, store

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


def source_dir(arg=None) -> Path:
    """Where `wb patch init` puts the clone: --dir, else WB_SOURCE, else ~/.local/share/workbench/source."""
    v = arg or os.environ.get("WB_SOURCE")
    return Path(os.path.abspath(os.path.expanduser(v))) if v else Path.home() / ".local/share/workbench/source"


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


def marketplace_refusal(root) -> str | None:
    """wb update on a marketplace install with local changes: they are saved first, and the
    refusal says where. Claude Code's update re-clones, and `wb update` once printed "updated
    marketplace <patched>..<upstream>", exit 0, having wiped the patch (#1005, 2026-11-22)."""
    found = local_changes(root)
    if not found:
        return None
    saved = rescue(root, found)
    return (f"the marketplace clone {root} has {found.say()}: Claude Code's update re-clones it and "
            f"would wipe them, so nothing was updated. Saved in {saved}; wb patch init moves workbench "
            "to a clone of your own that keeps them as patches and still updates")


CHECK_LINE = ("local changes in the marketplace clone: wb update would refuse (Claude Code re-clones "
              "it); wb patch init keeps them")


def _subject(root, sha) -> str:
    return _out(root, "log", "-1", "--format=%h %s", sha)


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


def _rebase(root, at, *args) -> tuple[str | None, str | None]:
    """`git rebase *args` in a scratch worktree at ``at``: (the new tip, None), or (None, the
    patch that stopped it, "<short sha> <subject>"), aborted. The live tree is never touched."""
    with scratch(root, at) as wt:
        r = _git(wt, *REBASE_PLAIN, "rebase", "-q", "--no-autosquash", *args, check=False)
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


class Rebased(NamedTuple):
    old: str
    upstream: str
    tip: str
    landed: list  # the subjects of the patches upstream already has, dropped
    kept: int


def rebase_patches(root) -> Rebased:
    """The patches on PATCH_BRANCH (@{u}..HEAD) onto the fetched upstream, in a scratch worktree.
    A patch upstream already has (a clean cherry-pick of one, or one that comes out empty) is
    dropped by the rebase; a conflict raises PatchError naming the patch, nothing changed."""
    old = _out(root, "rev-parse", "HEAD")
    up = _upstream(root)
    if not up:
        raise PatchError(f"branch {branch(root)} of {root} has no upstream: git -C {root} branch -u origin/main")
    tip, stopped = _rebase(root, old, up)
    if stopped:
        raise PatchError(
            f"the local patch {stopped} no longer applies on upstream's {up[:7]}: nothing changed. "
            f"Either rework it by hand (rebase {PATCH_BRANCH} onto @{{u}} in a scratch worktree, never "
            f"the live tree) or drop it (wb patch drop {stopped.split()[0]}), then wb update")
    before, after = _ident_rows(root, f"{up}..{old}"), _ident_rows(root, f"{up}..{tip}")
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
                         f"on {PATCH_BRANCH} (git -C {root} switch {PATCH_BRANCH}, or switch -c {PATCH_BRANCH})")


def _patches(root) -> list:
    rows = _out(root, "log", "--reverse", "--format=%h%x00%s%x00%(trailers:key=Scope,valueonly,separator=%x2C)",
                "@{u}..HEAD", "--")
    return [tuple(r.split("\0")) for r in rows.splitlines() if r]


def clone_note(root) -> str:
    b = branch(root) or "a detached HEAD"
    if b == PATCH_BRANCH:
        return (f"workbench already runs from a clone you own ({root}), on branch {b}: patches are its "
                "commits (wb patch list)")
    return (f"workbench already runs from a clone you own ({root}), on branch {b}: patches are commits on "
            f"branch {PATCH_BRANCH} (git -C {root} switch -c {PATCH_BRANCH} to start)")


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
    for sha, subject, scope in rows:
        print(f"{sha}  {subject}  [{scope or 'no Scope trailer'}]")
    if up and not rows:
        print("no patches: the branch is upstream's")
    dirty = _out(root, "status", "--porcelain", "--untracked-files=all").splitlines()
    if dirty:
        print("uncommitted: " + ", ".join(d.strip() for d in dirty))
    trees = {}
    for block in _out(root, "worktree", "list", "--porcelain").split("\n\n"):
        fields = dict(line.split(" ", 1) for line in block.splitlines() if " " in line)
        if fields.get("branch", "").startswith("refs/heads/"):
            trees[fields["branch"].removeprefix("refs/heads/")] = fields.get("worktree")
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


def _owner_repo(url) -> str | None:
    m = re.match(r"(?:[a-z+]+://(?:[^@/]+@)?github\.com/|[^@/]+@github\.com:)([^/]+/[^/]+?)(?:\.git)?/?$", url)
    return m.group(1) if m else None


def upstream(root, kind, commit, slug) -> None:
    """A branch pr/<slug> from fresh upstream with only ``commit``, in its own worktree, and the
    commands that would send it: wb never pushes or opens anything itself."""
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
    repo = _owner_repo(url) or "<owner/repo>"  # gh takes owner/repo, never a URL (gh repo fork <url> was printed)
    scope = _out(root, "log", "-1", "--format=%(trailers:key=Scope,valueonly,separator=%x2C)", sha).lower()
    if picked.returncode:
        print(f"{pr} from {remote}'s {up[:7]} in {wt}: {what} conflicts there. Resolve it in that "
              "worktree, then git cherry-pick --continue, and go on below")
        mod = True
    else:
        print(f"{pr} from {remote}'s {up[:7]} in {wt}, with {what}")
        mod = _git(wt, "diff", "--quiet", up, "HEAD", "--", "mod", check=False).returncode != 0
    if scope == "me":
        print("its Scope trailer is me: a personal patch. Offer it upstream only if it helps everybody")
    if repo.startswith("<"):
        print(f"{remote} ({url or 'no URL'}) isn't on GitHub: put its owner/repo below, or send it the way "
              "that project takes changes")
    print("next, in that worktree (none of this has run):")
    print(f"  cd {wt} && python3 -m unittest" + (" && claude plugin validate mod && claude plugin test mod"
                                                 if mod else ""))
    print(f"  gh repo fork {repo} --remote --remote-name fork")
    print(f"  git push -u fork {pr}")
    print(f"  gh pr create --repo {repo} --head <your login>:{pr} --body-file <the body: "
          f"{wt}/.github/pull_request_template.md, filled in>")
    print(f"once it merges, the next wb update drops the local patch ({what.split()[0]})")


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
    for step in (["claude", "plugin", "marketplace", "add", str(dest)], [str(dest / "bin/wb"), "setup", "--force"],
                 ["claude", "plugin", "update", "workbench@workbench"]):
        err = _run(step, dest)
        if err is not None:
            done = (f"; {dest} is ready and nothing else changed: wb patch init --dir {dest} again goes on "
                    "from there" if step[0] == "claude" and step[3] == "add" else
                    f"; Claude Code's marketplace is {dest} already, so finish by hand: "
                    f"{dest}/bin/wb setup --force && claude plugin update workbench@workbench")
            raise PatchError(f"{' '.join(Path(step[0]).name if i == 0 else s for i, s in enumerate(step[:4]))} "
                             f"failed: {err}{done}{keep}")
    print(f"workbench runs from {dest} now, branch {PATCH_BRANCH} tracking origin/{track}; "
          f"{carried} patch(es) carried over from the marketplace clone{keep}")
    if found.stashes or found.branches:  # the marketplace clone is gone: rescued/ is their only copy
        what = [f"{len(found.stashes)} stash(es) (stash-<n>.diff)"] if found.stashes else []
        what += [f"branch {b} (branch-{b.replace('/', '-')}/)" for b in found.branches]
        print(f"not carried over, kept only in {saved}: {', '.join(what)}")
    print("patches are commits on it (wb patch list); wb update rebases them onto upstream's news. "
          "/reload-plugins (or a restart) loads the mod from it")
