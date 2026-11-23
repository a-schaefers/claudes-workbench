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
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import NamedTuple

from . import gitops, hardware, store

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


def note_install(root) -> None:
    """$WB_HOME/patches.json: the clone install and its upstream on GitHub, for the mod (#1005)."""
    url = _out(root, "config", "--get", f"remote.{_tracked(root)[0]}.url", check=False)
    store.write_json(store.home() / "patches.json", {"source": str(root), "upstream": _owner_repo(url)})


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
    who = ("the user runs, as root: " if cmd.startswith("apk ") else
           "the user runs: ! " if "sudo " in cmd or cmd.startswith("pamac ") else "runs: ")
    lines = ["gh (GitHub's CLI) isn't installed. Installing it is the user's yes: Claude asks first, then "
             + who + cmd,
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
    lines.append(f"the user runs: ! gh auth login --hostname github.com --git-protocol {protocol} "
                 "(a browser or one-time code; Claude never handles the token)")
    if protocol == "https":
        lines.append("then: ! gh auth setup-git, so git pushes over https with that login (it edits the "
                     "user's global git config: ask first)")
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
    if picked.returncode:
        print(f"{pr} from {remote}'s {up[:7]} in {wt}: {what} conflicts there. Resolve it in that "
              "worktree, then git cherry-pick --continue, and go on below")
        mod = True
    else:
        print(f"{pr} from {remote}'s {up[:7]} in {wt}, with {what}")
        mod = _git(wt, "diff", "--quiet", up, "HEAD", "--", "mod", check=False).returncode != 0
    if scope == "me":
        print("its Scope trailer is me: a personal patch. Offer it upstream only if it helps everybody")
    note_install(root)
    print("next (none of this has run):")
    print(f"  cd {wt} && python3 -m unittest" + (" && claude plugin validate mod && claude plugin test mod"
                                                 if mod else ""))
    if not repo:
        print(f"{remote} ({_shown_url(url)}) isn't on GitHub, and wb patch send sends only there: send it "
              "the way that project takes changes")
    else:
        # the body outside the worktree: a file left in it is an uncommitted change send refuses
        body, template = body_path(slug), wt / ".github/pull_request_template.md"
        if not body.exists() and template.is_file():
            shutil.copyfile(template, body)
        print(f"  fill in {body} (" + ("a copy of .github/pull_request_template.md" if template.is_file()
                                       else "the pull request's body") + ")")
        print(f"  wb patch send {slug} --title {_quote(what.split(' ', 1)[-1])} --body-file {body}")
        print(gh_line(gh_state(root)))
        print("wb patch send forks, pushes and opens the pull request: the user's yes for it first")
    print(f"once it merges, the next wb update drops the local patch ({what.split()[0]})")


def body_path(slug) -> Path:
    return store.home() / "upstream" / f"{slug}.md"


def _quote(text) -> str:
    """``text`` as one shell word: in double quotes when that needs no escaping, else shlex's."""
    return f'"{text}"' if not re.search(r'["$`\\!]', text) else shlex.quote(text)


def _refuse_send(root, wt, pr, base, body_file, anyway) -> None:
    """Every refusal of wb patch send that needs no network, before any is asked (#1005)."""
    if not (_out(root, "rev-parse", "-q", "--verify", f"refs/heads/{pr}", check=False) and wt.is_dir()
            and branch(wt) == pr):
        raise PatchError(f"no {pr} in {wt}: wb patch upstream <commit> --name {pr.removeprefix('pr/')} "
                         "prepares it")
    if _out(wt, "rev-parse", "-q", "--verify", "CHERRY_PICK_HEAD", check=False):
        raise PatchError(f"a cherry-pick is in progress in {wt}: resolve it, then git cherry-pick --continue")
    if dirty := _out(wt, "status", "--porcelain", "--untracked-files=normal").splitlines():
        raise PatchError(f"{wt} has uncommitted changes ({', '.join(d.strip() for d in dirty[:5])}): "
                         "commit them on the branch, or remove them")
    rows = [r.split("\0") for r in _out(wt, "log", "--format=%h%x00%s%x00%(trailers:key=Scope,valueonly,"
                                        "separator=%x2C)", f"{base}..HEAD", "--").splitlines() if r]
    if not rows:
        raise PatchError(f"{pr} has no commits over {base.removeprefix('refs/remotes/')}: nothing to send")
    body = Path(body_file)
    try:
        text = body.read_bytes()
    except OSError as e:
        raise PatchError(f"--body-file {body}: {e.strerror or e}") from None
    template = wt / ".github/pull_request_template.md"
    if not text.strip():
        raise PatchError(f"--body-file {body} is empty: fill it in from {template}")
    if template.is_file() and text == template.read_bytes():
        raise PatchError(f"--body-file {body} is the template as it came: fill in every section first")
    mine = [f"{sha} {subject}" for sha, subject, scope in rows if scope.strip().lower() == "me"]
    if mine and not anyway:
        raise PatchError(f"{', '.join(mine)}: its Scope trailer is me, a personal preference, not a fix for "
                         "everybody: keep it local, or --anyway sends it")


SEND_OK = "send-ok.json"  # $WB_HOME/send-ok.json: the mod's dialog approved this one send
SEND_OK_SECS = 120  # the dialog's yes is for the call it let through, run at once


def _approved(slug, title, body_file, draft, anyway) -> bool:
    """Whether the mod's dialog approved exactly this send just now. Under Claude Code (its Bash
    sets CLAUDECODE=1) the dialog is the user's yes; with the mod missing, disabled or its hooks
    failing to load, nothing asked them, and Claude's Bash ran the fork, push and pull request
    regardless (#1005, 2026-11-22). The gate writes $WB_HOME/send-ok.json on "Send it": the
    slug, title, body file and its text, the flags and when. One send consumes it, matching or
    not: renamed away first, so two sends can't both read it."""
    path = store.home() / SEND_OK
    claim = path.with_name(f".{SEND_OK}.{os.getpid()}")
    try:
        os.replace(path, claim)
    except OSError:
        return False
    try:
        ok = store.read_json(claim, {})
        text = Path(body_file).read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return False
    finally:
        claim.unlink(missing_ok=True)
    try:
        fresh = 0 <= time.time() - float(ok.get("at", 0)) < SEND_OK_SECS
    except (TypeError, ValueError, AttributeError):
        return False
    return fresh and isinstance(ok, dict) and (ok.get("slug"), ok.get("title"), ok.get("body_file"), ok.get("body"),
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
    _refuse_send(root, wt, pr, base, body_file, anyway)
    url = _out(root, "config", "--get", f"remote.{remote}.url", check=False)
    repo = _owner_repo(url)
    if not repo:
        raise PatchError(f"{remote} ({_shown_url(url)}) isn't on GitHub: wb patch send sends only there")
    st = gh_state(root)
    if st["state"] != "ready":
        raise PatchError("gh can't send it yet, nothing was sent:\n" + gh_line(st))
    login, protocol = st["login"], st["protocol"]
    workflows = _out(wt, "diff", "--name-only", f"{base}...HEAD", "--", ".github/workflows/")
    if workflows and protocol == "https" and "workflow" not in st["scopes"]:
        raise PatchError(f"{pr} changes .github/workflows/, and GitHub refuses that push over https without "
                         "the token's workflow scope, so nothing was sent: the user runs "
                         "! gh auth refresh -h github.com -s workflow")
    if os.environ.get("CLAUDECODE") == "1" and not _approved(slug, title, body_file, draft, anyway):
        again = " ".join(["wb patch send", slug, "--title", _quote(title), "--body-file",
                          shlex.quote(os.path.abspath(body_file)), *(["--draft"] if draft else []),
                          *(["--anyway"] if anyway else [])])
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
            err += ("; if git has no login for github.com over https: the user runs ! gh auth setup-git "
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
    print("once it merges, the next wb update drops the local patch")


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
    lines = []
    for slug, wt in held(root):
        if not _landed_pr(root, slug, up):
            continue
        if wt and wt.is_dir():
            if _out(wt, "status", "--porcelain", check=False) or _out(
                    wt, "rev-parse", "-q", "--verify", "CHERRY_PICK_HEAD", check=False):
                lines.append(f"pr/{slug} landed upstream; {wt} has changes of its own, so it is kept")
                continue
            if _git(root, "worktree", "remove", str(wt), check=False).returncode:
                lines.append(f"pr/{slug} landed upstream; git couldn't remove {wt}, so it is kept")
                continue
        _git(root, "worktree", "prune", check=False)
        _git(root, "branch", "-q", "-D", f"pr/{slug}", check=False)
        body_path(slug).unlink(missing_ok=True)
        lines.append(f"landed upstream, removed pr/{slug}, its worktree and its body file")
    return lines


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
    note_install(dest)
    print(f"workbench runs from {dest} now, branch {PATCH_BRANCH} tracking origin/{track}; "
          f"{carried} patch(es) carried over from the marketplace clone{keep}")
    if found.stashes or found.branches:  # the marketplace clone is gone: rescued/ is their only copy
        what = [f"{len(found.stashes)} stash(es) (stash-<n>.diff)"] if found.stashes else []
        what += [f"branch {b} (branch-{b.replace('/', '-')}/)" for b in found.branches]
        print(f"not carried over, kept only in {saved}: {', '.join(what)}")
    print("patches are commits on it (wb patch list); wb update rebases them onto upstream's news. "
          "/reload-plugins (or a restart) loads the mod from it")
