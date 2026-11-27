"""Git for a task: a worktree on its own branch at the start, a diff for review, a commit at the end.

The agent never commits; everything it does stays in the worktree's working tree until `wb accept`.
"""
import contextlib
import fcntl
import functools
import hashlib
import os
import shlex
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Callable

from . import models, store


class GitError(RuntimeError):
    pass


class NotARepo(GitError):
    """The path is in no git repository (toplevel), in whatever language git said it."""


C_LOCALE = {"LC_ALL": "C", "LANGUAGE": ""}  # git's messages in English, to be matched


# The user's settings that would change what we read: found in the 2026-11-16 audit, a
# `diff.external = difft` made the review diff difftastic's screen, color.ui=always put escape
# codes in the changed-files list, and core.quotePath escaped every non-ASCII file name.
# (color.diff/status/branch=always outrank color.ui, so each is named.) log.showSignature put
# "No signature" ahead of the hash a `git log --format=%h` was read for, and a finished wb accept
# died on it (the audit). submodule.recurse=true made a sparse worktree's `read-tree -mu HEAD`
# reset a submodule the new worktree has no git dir for, so no task could start there (#888, the
# third 2026-11-21 audit): none of our checkouts, resets or merges recurse, fill_submodules fills them.
PLAIN = ("-c", "color.ui=never", "-c", "color.diff=never", "-c", "color.status=never",
         "-c", "color.branch=never", "-c", "core.quotePath=false", "-c", "log.showSignature=false",
         "-c", "submodule.recurse=false")


# No git hook runs from workbench in a task's worktree: with a relative core.hooksPath (husky's
# `.husky/_`, gitignored, so in no diff) the hooks live in the worktree, written by the agent, and
# wb accept ran them as the user outside the lane (the 2026-11-16 audit). The checks belong in
# --test, which runs in the lane. Signing is kept: only the hooks go.
NO_HOOKS = ("-c", "core.hooksPath=/dev/null")
# The variables that tell git which repository, index or work tree to use. Inherited from a git
# hook (git exports GIT_INDEX_FILE, and GIT_DIR in a linked worktree) or a vcsh or bare-dotfiles
# shell (GIT_DIR, GIT_WORK_TREE), they sent wb's calls to the caller's repository or index: the
# runner's `worktree add` failed ("index file open failed: Not a directory"), or the review diff
# and the accept commit were built from the user's own index (#798, the second 2026-11-21
# audit). wb, its runner and the agent never inherit them (drop_repo_env); wb's own deliberate
# GIT_INDEX_FILE (a scratch index) is layered on per call.
REPO_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE", "GIT_PREFIX")


def drop_repo_env(env=None) -> None:
    """REPO_ENV out of ``env`` (the process's own by default), in place: at wb's and the runner's
    start, so no child (the runner, the agent, its tests) inherits the caller's."""
    env = os.environ if env is None else env
    for name in REPO_ENV:
        env.pop(name, None)


def _env(extra=None) -> dict:
    """The environment for one of wb's git calls: the process's, less REPO_ENV, plus ``extra``."""
    return {**{k: v for k, v in os.environ.items() if k not in REPO_ENV}, **(extra or {})}


NO_SMUDGE = {"GIT_LFS_SKIP_SMUDGE": "1"}  # LFS files stay pointers: a trial reads paths only (#469)


def _record(worktree) -> Path | None:
    """Where a task worktree's real git dir is kept: rules/<task>/gitdir.json (context.rules_dir),
    outside the agent's lane. None for any other directory."""
    p = Path(os.path.abspath(worktree))  # not resolved: a worktree swapped for a link still pins
    tasks = Path(os.path.abspath(store.tasks_dir()))
    rel = p.relative_to(tasks).parts if p.is_relative_to(tasks) else ()
    if p.name == "worktree" and (len(rel) == 2 or len(rel) > 3 and rel[1] == "tree"):  # #261
        return tasks.parent / "rules" / rel[0] / "gitdir.json"
    return None


def _pinned(cwd) -> list[str]:
    """The options that hold git to a task worktree's own git dir, recorded at start. The
    worktree's `.git` is a file the agent can rewrite: pointed at a git dir it made under /tmp,
    that dir's config (core.fsmonitor, diff.external, hooks) ran from the runner's mark() outside
    the lane (the 2026-11-16 audit). Pinned, git reads only the real repo's config, and a `.git`
    that no longer says what it said at start stops the call."""
    rec = _record(cwd)
    if rec is None or not Path(cwd).is_dir():
        return []
    want = store.read_json(rec)
    if not want:  # a task started before the record
        return []
    dotgit = Path(cwd) / ".git"
    try:
        same = (stat.S_ISREG(os.lstat(dotgit).st_mode)
                and dotgit.read_bytes() == want["dotgit"].encode())
    except OSError:
        same = False
    if not Path(want["gitdir"]).is_dir():  # its repo moved: `.git` still says so, or repaired (#498)
        task = rec.parent.name
        raise GitError(f"the repo moved or was deleted (its git dir {want['gitdir']} is gone): "
                       f"wb relocate {task} --repo <its new path>")
    if not same:
        raise GitError(f"the worktree's .git was rewritten: git was not run there ({dotgit} "
                       f"must say gitdir: {want['gitdir']})")
    return [f"--git-dir={want['gitdir']}", f"--work-tree={os.path.abspath(cwd)}",
            *NO_HOOKS, "-c", "core.fsmonitor=false", *fs_pins(cwd)]


def fs_pins(worktree, differing: bool = False) -> list[str]:
    """`-c core.filemode=… -c core.ignorecase=…` as the file system the task's worktree is on
    really behaves (_modes_hold, _case_folds; one it can't tell is left out). ``differing``: only
    those the repository's own config says otherwise (unset: git's defaults), for the agent's git
    shim, which then checks the call is in the task's repository: only wb's own calls had them,
    and the agent's `git status` on exFAT listed every file as changed to 100755, and on a
    case-sensitive disk under a core.ignorecase=true repo a case-only rename as a deletion (#796,
    the second 2026-11-21 audit)."""
    d = os.path.dirname(os.path.abspath(worktree))
    rec = _record(worktree) if differing else None
    gitdir = (store.read_json(rec) or {}).get("gitdir") if rec else None
    out = []
    for key, now, default in (("core.filemode", _modes_hold(d), True),
                              ("core.ignorecase", _case_folds(d), False)):
        if now is None:
            continue
        if differing and gitdir:
            said = _run(gitdir, ("config", "--bool", key), check=False).stdout.decode().strip()
            if (said == "true" if said else default) == now:
                continue
        out += ["-c", f"{key}={'true' if now else 'false'}"]
    return out


@functools.lru_cache(maxsize=64)
def _modes_hold(d: str) -> bool | None:
    """Whether the file system at ``d`` keeps the execute bit (None: couldn't tell). A task's
    worktree is on the state directory's, whatever the repo's: with the repo's
    core.filemode=false (a checkout on a drive that distrusts modes, WSL's /mnt/c), a script the
    agent made 0755 ran in the task and was committed 100644, so a fresh checkout couldn't run it
    (#323, the audit of 1.4). Where the modes hold, the task's git reads them: the review diff and
    the commit say the same. Where they don't (exFAT, NTFS, CIFS, drvfs without metadata: every
    file reads 0755 or 0777), it ignores them: with the repo's core.filemode=true every file of
    the tree showed as changed to 100755, and accept committed the flip (#683)."""
    try:
        fd, probe = tempfile.mkstemp(prefix=".wb-mode-", dir=d)
        os.close(fd)
    except OSError:
        return None
    try:
        os.chmod(probe, 0o755)
        on = os.stat(probe).st_mode & 0o111
        os.chmod(probe, 0o644)
        return on == 0o111 and not os.stat(probe).st_mode & 0o111
    except OSError:  # a chmod refused: the modes don't hold there (a mount that forbids it)
        return False
    finally:
        try:
            os.unlink(probe)
        except OSError:
            pass


@functools.lru_cache(maxsize=64)
def _case_folds(d: str) -> bool | None:
    """Whether the file system at ``d`` folds case (None: couldn't tell). A task's worktree is on
    the state directory's, whatever the repo's: with the repo's core.ignorecase=true (a checkout
    Git for Windows made on /mnt/c, a repo from macOS) on a case-sensitive one, git saw a
    case-only rename as a deletion alone and a new OTHER.PY beside other.py as nothing, and wb
    accept committed the deletion and removed the worktree, the new names lost (#492, the audit
    of 1.90; #323 did the same for core.filemode). The task's git says what this one does."""
    try:
        fd, probe = tempfile.mkstemp(prefix=".wb-case-", dir=d)
        os.close(fd)
    except OSError:
        return None
    try:
        twin = os.path.join(d, os.path.basename(probe).upper())
        return os.path.exists(twin) and os.path.samefile(probe, twin)
    except OSError:
        return None
    finally:
        try:
            os.unlink(probe)
        except OSError:
            pass


def _run(cwd, args, env=None, check=True, timeout=None, input=None,
         detach=False, started: Callable[[int], None] | None = None) -> subprocess.CompletedProcess:
    """``detach``: git in a session of its own, with no controlling terminal, for the calls that
    may sign (with NO_TERMINAL): ssh-keygen opens /dev/tty when it can, and on WSL2, where wb has
    one, it prompted there for a passphrase nobody would type, so wb accept waited COMMIT_SECS
    and wb start SIGN_SECS before refusing (#933). Detached, it fails at once. And it outlives a
    SIGKILL of wb, so its output goes to files, not pipes: a signed merge's git whose wb was
    killed made the commit, then died of SIGPIPE printing it, before removing MERGE_HEAD
    (#1231). ``started(pid)``, called once it runs, is how a retry finds that git (#1246); a
    timeout or an interruption ends its whole session (end_session), the signer too."""
    argv = ["git", "-C", str(cwd), *PLAIN, *_pinned(cwd), *args]
    kw = dict(env=_env(env), start_new_session=detach,
              stdin=subprocess.DEVNULL if timeout and input is None else None)
    try:
        if not detach:
            r = subprocess.run(argv, capture_output=True, timeout=timeout, input=input, **kw)
        else:
            assert input is None, "a detached git reads nothing"
            with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
                p = subprocess.Popen(argv, stdout=out, stderr=err, **kw)
                try:
                    if started:
                        started(p.pid)
                    p.wait(timeout=timeout)
                except BaseException:
                    end_session(p.pid, gone=lambda: p.poll() is not None)
                    p.wait()
                    raise
                out.seek(0), err.seek(0)
                r = subprocess.CompletedProcess(argv, p.returncode, out.read(), err.read())
    except subprocess.TimeoutExpired:
        raise GitError(f"git {' '.join(args)}: no answer in {timeout}s") from None
    if check and r.returncode:
        err = (r.stderr or r.stdout).decode("utf-8", "replace").strip()
        raise GitError(f"git {' '.join(args)}: {err}")
    return r


END_SECS = 5  # for a git sent SIGTERM to go before SIGKILL, and for a SIGKILLed one to go


def end_session(pid: int, start: str | None = None, gone: Callable[[], bool] | None = None) -> None:
    """End the detached session git ``pid`` leads (``_run``'s detach: git and its signer), and
    wait for git to go: SIGTERM first, on which git removes its lock files (a SIGKILLed one's
    stay, and refuse the next git that takes them), SIGKILL past END_SECS, then SIGKILL for what
    is left of the group. ``gone`` says when git has gone (our child: polled), else it is
    models._alive's ``pid`` and ``start``; with neither start nor ``gone``, a pid that may be
    another's now is left alone. A retry of a SIGKILLed merge backed it out while that git still
    waited on its signer, which, answered later, landed the merge on the user's branch over the
    unmerged index and files: every file the task changed staged as reverted, and wb said not
    merged (#1246, bar round 5)."""
    if gone is None:
        if not start:
            return
        def gone():
            return not models._alive(pid, start)
    if gone():
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        deadline = time.monotonic() + END_SECS
        while not gone() and time.monotonic() < deadline:
            time.sleep(0.05)
        if gone():
            break
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGKILL)  # the signer, if it outlived git


def git(cwd, *args, check=True, env=None) -> str:
    """git's output as text. Bytes that aren't UTF-8 (a Latin-1 source file in a diff) read as
    U+FFFD: strict decoding crashed the runner on them (the 2026-11-16 audit), and the text goes
    to people and prompts, where a surrogate escape would raise when printed."""
    return _run(cwd, args, env, check).stdout.decode("utf-8", "replace")


def git_bytes(cwd, *args, env=None) -> bytes:
    """git's output exactly, for fingerprints: two different non-UTF-8 bytes must not both
    read as U+FFFD."""
    return _run(cwd, args, env).stdout


def git_hash(cwd, h, *args) -> None:
    """Feed git's output into the hash ``h`` as it comes, a chunk at a time: captured whole, the
    tracked diff of a replaced 12 MiB file held 50 MiB in the runner at every progress check,
    beside the resident model (#678)."""
    with tempfile.TemporaryFile() as err:  # not a pipe: a full one would stall git mid-diff
        p = subprocess.Popen(["git", "-C", str(cwd), *PLAIN, *_pinned(cwd), *args],
                             stdout=subprocess.PIPE, stderr=err, stdin=subprocess.DEVNULL, env=_env())
        with p.stdout:
            while chunk := p.stdout.read(1 << 16):
                h.update(chunk)
        if p.wait():
            err.seek(0)
            raise GitError(f"git {' '.join(args)}: {err.read().decode('utf-8', 'replace').strip()}")


# what a feed_blobs callback returns to skip the rest of the blob it is fed (True stops them all)
SKIP_BLOB = "skip"


def feed_blobs(cwd, oids, h, env=None) -> None:
    """Each blob in ``oids`` fed to ``h(oid, chunk)`` a chunk at a time, through one `git cat-file
    --batch`, until ``h`` returns True: then git is stopped mid-blob. SKIP_BLOB skips the rest of
    that blob: git is stopped and a new one asked for the blobs after it, so a minified bundle's
    megabytes are never read past the first chunks (#857, 2026-11-21). A blob missing is skipped.
    Whether a repo has a long file probe reads (context.probe_off, #857, 2026-11-21) counts lines
    of many blobs and needs only the first lines past the limit of one: a process per blob, read
    to its end, cost a large repo seconds at wb start, inside Claude's Bash limit. ``env``: a
    partial clone's GIT_NO_LAZY_FETCH, so a blob it lacks is `missing`, not fetched (#436)."""
    todo = list(oids)
    while todo:
        todo = _feed_some(cwd, todo, h, env)


def _feed_some(cwd, oids, h, env) -> list:
    """feed_blobs through one git: the blobs left after a SKIP_BLOB mid-blob, else []."""
    with tempfile.TemporaryFile() as err:
        p = subprocess.Popen(["git", "-C", str(cwd), *PLAIN, *_pinned(cwd), "cat-file", "--batch"],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err, env=_env(env))
        try:
            for i, oid in enumerate(oids):
                p.stdin.write(oid.encode() + b"\n")
                p.stdin.flush()
                head = p.stdout.readline().split()
                if head[1:] == [b"missing"]:
                    continue
                if len(head) != 3 or not head[2].isdigit():
                    return []
                left = int(head[2])
                while left:
                    chunk = p.stdout.read(min(left, 1 << 16))
                    if not chunk:
                        return []
                    left -= len(chunk)
                    said = h(oid, chunk)
                    if said == SKIP_BLOB:
                        if left:
                            return oids[i + 1:]
                        break
                    if said:
                        return []
                p.stdout.read(1)  # the newline after each object
        except OSError:  # git gone: what it fed is all there is
            return []
        finally:
            p.kill()
            p.wait()
            for f in (p.stdin, p.stdout):
                try:
                    f.close()
                except OSError:
                    pass
    return []


def toplevel(path) -> Path:
    """The repo's top directory; NotARepo when ``path`` is in none."""
    try:
        return Path(git(path, "rev-parse", "--show-toplevel").strip())
    except GitError as e:
        # git's messages follow the user's language: "not a git repository" matched in English
        # missed a French git's, and wb init refused an empty directory (the 2026-11-19 audit).
        # Asked again in the C locale to classify; the user still reads their own language
        again = _run(path, ["rev-parse", "--show-toplevel"], env=C_LOCALE, check=False)
        if again.returncode and b"not a git repository" in again.stderr:
            raise NotARepo(str(e)) from None
        raise


def common_dir(path) -> Path | None:
    """The repository's own git dir, which all its worktrees share, as a real path; None when
    ``path`` is in none (or gone)."""
    if not Path(path).is_dir():
        return None
    out = git(path, "rev-parse", "--git-common-dir", check=False).strip()
    return Path(os.path.realpath(Path(path) / out)) if out else None


def repo_of(spec: dict) -> Path:
    """Where a task's repo-level git runs (its tip, its branch, wb verify's clones): task.json
    `repo` while it is a checkout of the task's repository; once that checkout is gone (a linked
    worktree removed: one per feature, the bare-repo layout, Claude Code's --worktree sessions)
    the repository's main worktree, from its git dir (task.json `git_common`), or that git dir
    itself when it is bare. A removed `repo` wedged wb accept on every retry, and discard said
    the branch had gone with it (#498, the audit of 1.90). `repo` itself when the git dir is
    gone too: the repo moved (wb relocate)."""
    repo = Path(spec.get("repo") or "/nonexistent")
    common = spec.get("git_common") or _common_recorded(spec.get("name"))
    if not common or not Path(common).is_dir() or common_dir(repo) == Path(common):
        return repo
    for line in git(common, "worktree", "list", "--porcelain", check=False).splitlines():
        if line.startswith("worktree "):  # the first is the main one (a bare repo: its git dir)
            return main if (main := Path(line[9:])).is_dir() else Path(common)
    return Path(common)


def _common_recorded(task: str | None) -> str | None:
    """A task's git dir from gitdir.json, for a task.json from before `git_common`."""
    rec = Path(store.tasks_dir()).parent / "rules" / task / "gitdir.json" if task else None
    want = store.read_json(rec) if rec else None
    try:
        gitdir = Path(want["gitdir"])
        return os.path.realpath(gitdir / (gitdir / "commondir").read_text().strip())
    except (TypeError, KeyError, OSError):
        return None


def relocate(repo: Path, worktree: Path) -> None:
    """Point a task's worktree at ``repo``, the place its repository moved to, and record that
    (gitdir.json): its git dir is the one ``repo`` keeps for it (the one whose link back is this
    worktree); then `git worktree repair`, and its `.git` written as repair would have when
    the repair couldn't (a git too old to infer it). A `.git` naming another git dir that exists
    (one the agent made) is refused first, as _pinned refuses it: a newer git's repair would
    quietly take it back."""
    common, dotgit = common_dir(repo), Path(worktree) / ".git"
    # With worktree.useRelativePaths (git 2.48+) both links are relative: the admin dir's, read
    # against wb's cwd, matched nothing, and every relocate was refused (#682). Read against the
    # admin dir it still can't match once the repo moved (the admin dir moved, the worktree
    # didn't), so the admin dir is also known by its name in gitdir.json, unless its link names
    # another worktree that exists.
    rec = _record(worktree)
    want = store.read_json(rec) if rec else None
    name = Path(want["gitdir"]).name if isinstance(want, dict) and want.get("gitdir") else None
    admin = by_name = None
    for d in (common / "worktrees").iterdir() if common and (common / "worktrees").is_dir() else ():
        with contextlib.suppress(OSError):
            link = os.path.realpath(d / (d / "gitdir").read_text().strip())
            if link == os.path.realpath(dotgit):
                admin = d
            elif d.name == name and not os.path.isfile(link):
                by_name = d
    if (admin := admin or by_name) is None:
        raise GitError(f"{worktree} isn't one of {repo}'s worktrees")

    def said() -> Path | None:
        try:
            if stat.S_ISREG(os.lstat(dotgit).st_mode):  # a relative one is the worktree's (#682)
                return Path(worktree) / dotgit.read_text().removeprefix("gitdir:").strip()
        except OSError:
            pass
        return None
    if (was := said()) is not None and was.is_dir() and os.path.realpath(was) != os.path.realpath(admin):
        raise GitError(f"{dotgit} names a git dir of its own ({was}): it isn't one of {repo}'s "
                       "worktrees")
    git(repo, *NO_HOOKS, "worktree", "repair", str(worktree), check=False)
    if (now := said()) is None or os.path.realpath(now) != os.path.realpath(admin):
        if dotgit.is_symlink() or dotgit.is_dir():
            raise GitError(f"{dotgit} isn't a file: it isn't one of {repo}'s worktrees")
        dotgit.write_text(f"gitdir: {admin}\n")
    if rec:
        store.write_json(rec, {"gitdir": str(admin), "dotgit": dotgit.read_text()})


def branch_name(task: str) -> str:
    return f"delegate/{task}"


def branch_clash(repo: Path, task: str) -> str:
    """Why branch delegate/<task> can't be made in ``repo``, or "". Besides that branch itself, a
    branch named `delegate`, or one under `delegate/<task>/`, holds the ref's place: `branch
    --list` missed them, and the runner's `worktree add` failed for every task in that repo,
    after it had spawned (#792, the second 2026-11-21 audit)."""
    name = branch_name(task)
    if git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{name}", check=False).strip():
        return f"branch {name} already exists in {repo}: pick a new name"
    if git(repo, "rev-parse", "--verify", "-q", "refs/heads/delegate", check=False).strip():
        return (f"the branch delegate in {repo} blocks workbench's delegate/ branches: rename it "
                "(git branch -m delegate <new name>), then start again")
    if under := git(repo, "for-each-ref", "--count=1", "--format=%(refname:short)",
                    f"refs/heads/{name}/", check=False).strip():
        return (f"the branch {under} in {repo} blocks branch {name}: pick a new name, or rename "
                "that branch")
    return ""


def case_clash(repo: Path, commit: str, where: Path, name: str = "") -> str:
    """Why ``commit`` can't be checked out faithfully under ``where``, or "": its tree holds paths
    that differ only by case (docs/API.md and docs/api.md, Makefile and makefile) and the file
    system there folds case (macOS's default APFS, exFAT, a casefold mount). Only one file is
    written for each pair, so the review showed the other as changed by the agent before it
    did anything, and an edit to one was committed to the other: the branch accepted wasn't the
    diff reviewed (#797, the second 2026-11-21 audit). ``name``: the base as the user gave it."""
    if not _case_folds(os.path.abspath(where)):
        return ""
    groups: dict[str, list[str]] = {}
    for p in _run(repo, ("ls-tree", "-r", "-z", "--name-only", commit)).stdout.split(b"\0"):
        if p:
            groups.setdefault(_folded(p), []).append(os.fsdecode(p))
    twins = [g for g in groups.values() if len(g) > 1]
    if not twins:
        return ""
    shown = "; ".join(" and ".join(g) for g in twins[:3]) + \
        (f"; {len(twins) - 3} more" if len(twins) > 3 else "")
    return (f"{name or commit[:12]} tracks paths that differ only by case ({shown}), and the state "
            f"directory's file system folds case: checked out there, one of each would overwrite "
            "the other, and accept would commit an edit to the wrong one. Put WB_HOME on a "
            "case-sensitive volume")


def files_at(repo: Path, commit: str, pathspecs: list[str]) -> list[str]:
    """The files committed at ``commit`` that ``pathspecs`` match, git's own pathspec rules
    (`src/**/*.py`, `docs/`, `:!vendor`), sorted: a flow's `files` param (#1295). `ls-files` reads
    an index, so one built from the commit alone, in a scratch file: never the user's index, and
    their uncommitted files never count."""
    with tempfile.TemporaryDirectory(prefix="wb-files-") as d:
        env = {"GIT_INDEX_FILE": os.path.join(d, "index")}
        _run(repo, ("read-tree", commit), env=env)
        out = _run(repo, ("ls-files", "-z", "--", *pathspecs), env=env).stdout
    return sorted(os.fsdecode(p) for p in out.split(b"\0") if p)


def paths_at(repo: Path, commit: str) -> set[str]:
    """Every path at ``commit``, its files and the directories that hold them: what a stage's
    `unless_exists` checks, in one call rather than one per item."""
    out: set[str] = set()
    for p in _run(repo, ("ls-tree", "-r", "-z", "--name-only", commit)).stdout.split(b"\0"):
        if p:
            path = os.fsdecode(p)
            out.add(path)
            while "/" in path:
                path = path.rsplit("/", 1)[0]
                out.add(path)
    return out


def base_commit(repo: Path, base: str) -> str | None:
    """The commit ``base`` names (an annotated tag peeled), or None when it names none: a typo
    once read as a missing CLAUDE.md (the 2026-11-16 audit)."""
    return git(repo, "rev-parse", "--verify", "-q", f"{base}^{{commit}}", check=False).strip() or None


def create_worktree(repo: Path, task: str, dest: Path, base: str = "HEAD") -> str:
    """New branch delegate/<task> at ``base`` checked out at ``dest``; returns the base commit.
    No checkout hook runs (the agent's worktree needs none; git-lfs's own fails without git-lfs
    on PATH), and a failed add leaves no branch or worktree behind: one once left both, and the
    task's name could never be used again (the audit)."""
    commit = base_commit(repo, base)
    if not commit:
        raise GitError(f"no commit {base} in {repo}")
    if why := branch_clash(repo, task) or case_clash(repo, commit, Path(dest).parent, base):
        raise GitError(why)
    # a sparse checkout's patterns: newer git copies them into a worktree it adds (2.56 does),
    # an older one checked out the whole tree; set before the checkout either way (#263)
    sparse = sparse_of(repo)
    try:
        git(repo, *NO_HOOKS, "worktree", "add", "-q", *(["--no-checkout"] if sparse else []),
            "-b", branch_name(task), str(dest), commit)
        if sparse:
            apply_sparse(Path(dest), sparse)
            git(dest, *NO_HOOKS, "read-tree", "-mu", "HEAD")
    except GitError:
        git(repo, *NO_HOOKS, "worktree", "remove", "--force", str(dest), check=False)
        drop_registration(repo, dest)
        delete_branch(repo, task, at=commit)  # this call made it: branch_clash found none
        raise
    if rec := _record(dest):  # before any agent has run there: see _pinned
        rec.parent.mkdir(parents=True, exist_ok=True)
        store.write_json(rec, {"gitdir": git(dest, "rev-parse", "--absolute-git-dir").strip(),
                               "dotgit": (Path(dest) / ".git").read_text()})
    return commit


def sparse_of(repo: Path) -> tuple[str, bool] | None:
    """The checkout's sparse patterns and whether they are cone mode, None when it isn't sparse.
    A worktree added plainly held the whole tree, a monorepo's every package, and the base check's
    clone of it too (#263, the audit of 1.2)."""
    if git(repo, "config", "--bool", "core.sparseCheckout", check=False).strip() != "true":
        return None
    try:
        path = Path(repo) / git(repo, "rev-parse", "--git-path", "info/sparse-checkout").strip()
        patterns = path.read_text()
    except (GitError, OSError):
        return None
    cone = git(repo, "config", "--bool", "core.sparseCheckoutCone", check=False).strip() == "true"
    return (patterns, cone) if patterns.strip() else None


def apply_sparse(dest: Path, sparse: tuple[str, bool]) -> None:
    """``sparse_of``'s patterns for the checkout at ``dest``, before anything is checked out: a
    linked worktree's own (config --worktree where the repo keeps per-worktree config, else the
    shared config's setting holds), or a clone's."""
    patterns, cone = sparse
    linked = git(dest, "config", "--bool", "extensions.worktreeConfig", check=False).strip() == "true"
    scope = ["--worktree"] if linked else []
    git(dest, "config", *scope, "core.sparseCheckout", "true")
    git(dest, "config", *scope, "core.sparseCheckoutCone", "true" if cone else "false")
    info = Path(dest) / git(dest, "rev-parse", "--git-path", "info/sparse-checkout").strip()
    info.parent.mkdir(parents=True, exist_ok=True)
    info.write_text(patterns)


# A Git LFS pointer file: its first line, and the spec's bounds (the smallest has no extensions)
LFS_POINTER = b"version https://git-lfs.github.com/spec/v1\n"
LFS_POINTER_MIN, LFS_POINTER_MAX = 126, 1024


def lfs_filter(repo: Path) -> list[tuple[str, str]]:
    """The repo's `filter.lfs.*` settings (any scope): none when its checkouts don't run Git LFS."""
    out = git(repo, "config", "--get-regexp", r"^filter\.lfs\.", check=False)
    return [(k, v) for k, _, v in (line.partition(" ") for line in out.splitlines()) if k]


# The oldest git workbench supports: Ubuntu 22.04's 2.34, where the fallbacks for what came later
# (merge-tree --write-tree, 2.38; GIT_NO_LAZY_FETCH, 2.44) are checked. wb doctor flags an older
# one (#527).
GIT_FLOOR = (2, 34)


@functools.lru_cache(maxsize=1)
def version() -> tuple[int, ...] | None:
    """The git on PATH as numbers ((2, 44, 0)), None when it can't say."""
    try:
        out = subprocess.run(["git", "--version"], capture_output=True, text=True).stdout
    except OSError:
        return None
    word = next((w for w in out.split() if w[:1].isdigit()), "")
    nums = [int(n) for n in word.split(".")[:3] if n.isdigit()]
    return tuple(nums) or None


def partial_clone(repo: Path) -> bool:
    """A clone made with --filter: the blobs it lacks are fetched one by one when asked for. Its
    remote is a promisor (an older git also set extensions.partialClone, a newer one may not)."""
    if git(repo, "config", "extensions.partialClone", check=False).strip():
        return True
    out = git(repo, "config", "--get-regexp", r"^remote\..*\.promisor$", check=False)
    return any(line.rpartition(" ")[2].lower() in ("true", "yes", "on", "1")
               for line in out.splitlines())


def sparse_keeps(repo: Path) -> Callable[[str], bool] | None:
    """Whether a path is in the checkout's sparse cone (or its patterns), None when it isn't
    sparse. Cone mode: every file at the top and in a cone directory's parents, everything under
    a cone directory. Otherwise (patterns, which only git matches): what the checkout's own index
    keeps, its entries without the skip-worktree bit."""
    sparse = sparse_of(repo)
    if not sparse:
        return None
    if sparse[1]:
        cones = [d.strip("/") for d in git(repo, "sparse-checkout", "list", check=False).splitlines()
                 if d.strip("/")]
        def keeps(path: str) -> bool:
            d = os.path.dirname(path)
            return not d or any(d == c or d.startswith(c + "/") or c.startswith(d + "/")
                                for c in cones)
        return keeps
    kept = {row[2:] for row in git(repo, "ls-files", "-t", "-z", check=False).split("\0")
            if row[:2] != "S "}
    return kept.__contains__


def checkout_size(repo: Path, commit: str) -> tuple[int, int]:
    """The bytes a checkout of ``commit`` writes, and how many of its files' sizes aren't known
    here: each blob's size, and where the repo runs Git LFS, for a pointer the size of the file it
    stands for. Counted as pointers, a repo of LFS-tracked data read as a few kilobytes, and wb
    start's disk check passed it (#382, the audit of 1.6). Only what the sparse cone checks out
    counts, and in a partial clone only the blobs it holds, asked with lazy fetching off (git
    2.44+; an older one's are all unknown): the whole tree of a 30 GB monorepo was counted for a
    1 GB cone, and a blobless clone fetched every blob, one at a time, to size it (#436). The
    submodules fill_submodules fills count too, whole and nested: a gitlink is no blob, so a
    6 GB engine/ submodule counted nothing, the check passed on 8 GB free, and the base check's
    scratch fill hit ENOSPC (#886, the third 2026-11-21 audit)."""
    size, unknown = _tree_size(repo, commit, sparse=True)
    sub_size, sub_unknown = _submodules_size(repo, commit)
    return size + sub_size, unknown + sub_unknown


def _submodules_size(repo: Path, commit: str) -> tuple[int, int]:
    """checkout_size of each submodule of ``commit`` that _fill can fill (the user's checkout of
    it holds the commit recorded), at that commit, its whole tree (checkout-index writes it all)
    and its own submodules."""
    size = unknown = 0
    for path in submodules(repo, commit):
        row = git(repo, "ls-tree", commit, "--", path, check=False).split()
        sha = row[2] if len(row) >= 3 and row[1] == "commit" else ""
        src = Path(repo) / path
        if not sha or not (src / ".git").exists() or \
                _run(src, ("cat-file", "-e", f"{sha}^{{commit}}"), check=False).returncode:
            continue
        for s, u in (_tree_size(src, sha, sparse=False), _submodules_size(src, sha)):
            size, unknown = size + s, unknown + u
    return size, unknown


def _tree_size(repo: Path, commit: str, sparse: bool) -> tuple[int, int]:
    keeps = sparse_keeps(repo) if sparse else None
    lazy = partial_clone(repo)
    no_fetch = {"GIT_NO_LAZY_FETCH": "1"}
    size, unknown, pointers, blobs = 0, 0, [], []
    for row in git(repo, "ls-tree", "-r", *([] if lazy else ["-l"]), "-z", commit,
                   check=False).split("\0"):
        head, _, path = row.partition("\t")
        f = head.split()
        if len(f) < 3 or f[1] != "blob" or (keeps and not keeps(path)):
            continue
        if lazy:
            blobs.append(f[2])
        elif len(f) == 4 and f[3].isdigit():
            size += int(f[3])
            if LFS_POINTER_MIN <= int(f[3]) <= LFS_POINTER_MAX:
                pointers.append(f[2])
    if blobs and (version() or (0,)) < (2, 44):
        return size, len(blobs)
    if blobs:
        out = _run(repo, ("cat-file", "--batch-check"), env=no_fetch, check=False,
                   input="".join(f"{sha}\n" for sha in blobs).encode()).stdout
        for line in out.decode("ascii", "replace").splitlines():
            f = line.split()
            if len(f) == 3 and f[2].isdigit():
                size += int(f[2])
                if LFS_POINTER_MIN <= int(f[2]) <= LFS_POINTER_MAX:
                    pointers.append(f[0])
            else:  # "<sha> missing": not fetched yet
                unknown += 1
    if not pointers or not lfs_filter(repo):
        return size, unknown
    out = _run(repo, ("cat-file", "--batch"), env=no_fetch, check=False,
               input="".join(f"{sha}\n" for sha in pointers).encode()).stdout
    at = 0
    while (nl := out.find(b"\n", at)) >= 0:
        head = out[at:nl].split()
        if len(head) < 3 or not head[2].isdigit():  # "<sha> missing"
            at = nl + 1
            continue
        n = int(head[2])
        body, at = out[nl + 1:nl + 1 + n], nl + 2 + n
        if body.startswith(LFS_POINTER):
            for line in body.splitlines():
                if line.startswith(b"size ") and line[5:].isdigit():
                    size += int(line[5:]) - n
    return size, unknown


def share_lfs(repo: Path, clone: Path) -> None:
    """A scratch clone of ``repo`` (the base check's, wb verify's) smudges Git LFS files as the
    repo does, from the repo's own LFS objects. A clone has none of the repo's local config: after
    `git lfs install --local` it checked out pointer files where the tests expected the data, and
    with a global install each clone fetched its own copy of every object (#382, the audit of
    1.6). git-lfs only reads that directory here: the objects are there already."""
    pairs = lfs_filter(repo)
    if not pairs:
        return
    for key, value in pairs:
        git(clone, "config", key, value)
    common = Path(repo) / git(repo, "rev-parse", "--git-common-dir").strip()
    storage = git(repo, "config", "lfs.storage", check=False).strip()
    git(clone, "config", "lfs.storage", str((common / (storage or "lfs")).resolve()))


def submodules(repo: Path, commit: str) -> list[str]:
    """The submodule paths ``commit`` lists. In a task's worktree each is filled read-only by
    fill_submodules, never checked out: a `.git` there would be a file the agent could point at a
    git dir of its own, whose config git then obeys outside the lane (see _pinned; the 2026-11-16
    audit). wb start names only the ones it couldn't fill. Read NUL-terminated, each record
    `key\nvalue`: split at the first space, a submodule named `my lib` (git's default for that
    path) read as `lib.path my lib`, never filled, and wb start named a path that isn't there
    (#791, the second 2026-11-21 audit)."""
    out = git(repo, "config", "-z", "--blob", f"{commit}:.gitmodules", "--get-regexp",
              r"^submodule\..*\.path$", check=False)
    return [rec.split("\n", 1)[1] for rec in out.split("\0") if "\n" in rec]


def submodule_sources(repo: Path, commit: str) -> list[Path]:
    """The user's checkouts fill_submodules fills a worktree of ``commit`` from, nested ones too
    (each at the commit the one around it records), as _fill finds them: what a task's worktree
    holds beyond the repo's own files. privacy.local_only reads their marks: a submodule marked
    local-only went to the API model with every task of the repo around it (#1267, the bar loop,
    2026-11-24)."""
    out = []
    for path in submodules(repo, commit):
        row = git(repo, "ls-tree", commit, "--", path, check=False).split()
        src = Path(repo) / path
        if len(row) >= 3 and row[1] == "commit" and (src / ".git").exists():
            out += [src, *submodule_sources(src, row[2])]
    return out


def superproject(path) -> Path | None:
    """The working tree of the repo whose submodule ``path``'s repo is, or None."""
    out = git(path, "rev-parse", "--show-superproject-working-tree", check=False).strip()
    return Path(out) if out else None


SUBMODULES = "submodules.json"  # rules/<task>/: the submodules filled in its worktree (#419)


def fill_submodules(repo: Path, commit: str, checkout: Path, dry: bool = False) -> list[str]:
    """Each submodule of ``commit`` filled in ``checkout`` (a task's worktree or a scratch one)
    with its files at the commit ``commit`` records, from the user's own checkout of it: written
    by git itself (a scratch index, `checkout-index`), with no `.git` for the agent to repoint
    (see submodules) and read-only, since the gitlink keeps an edit there out of the diff and the
    commit. The ones it couldn't fill come back (the checkout lacks the submodule or that commit).
    Empty, a C/C++ or vendored project's build failed on the base and in every round (#350, the
    audit of 1.4). A directory already holding files is left as it is. A submodule's own
    submodules are filled the same way, from the user's checkout of each inside it, and the ones
    it couldn't fill come back by their whole path: only the top `.gitmodules` was read, and a
    nested one stayed an empty directory (#383, the audit of 1.6). In a task's worktree each one
    filled is recorded (SUBMODULES, beside gitdir.json, with its index: submodule_edits).
    ``dry``: only the ones it couldn't fill, nothing written or recorded (wb start says them
    before the runner's checkout, #529)."""
    if dry:
        return _fill(repo, commit, checkout, None, [], "", dry=True)
    rec, filled = _record(checkout), []
    left = _fill(repo, commit, checkout, rec.parent if rec else None, filled, "")
    if rec is not None and filled:
        store.write_json(rec.with_name(SUBMODULES), filled)
    return left


def _fill(repo: Path, commit: str, checkout: Path, keep: Path | None, filled: list, at: str,
          dry: bool = False) -> list[str]:
    left = []
    for path in submodules(repo, commit):
        row = git(repo, "ls-tree", commit, "--", path, check=False).split()
        sha = row[2] if len(row) >= 3 and row[1] == "commit" else ""
        src, dest = Path(repo) / path, Path(checkout) / path
        if not dry and dest.is_dir() and any(dest.iterdir()):
            continue
        if not sha or not (src / ".git").exists() or \
                _run(src, ("cat-file", "-e", f"{sha}^{{commit}}"), check=False).returncode:
            left.append(path)
            continue
        if dry:
            left += [f"{path}/{p}" for p in _fill(src, sha, dest, None, filled, "", dry=True)]
            continue
        store.home().mkdir(parents=True, exist_ok=True)  # the scratch index: never in the lane
        with tempfile.TemporaryDirectory(prefix=store.scratch(".submodule-"), dir=store.home()) as scratch:
            env = {"GIT_INDEX_FILE": os.path.join(scratch, "index")}
            try:
                dest.mkdir(parents=True, exist_ok=True)
                _run(src, ("read-tree", sha), env=env)
                _run(src, (f"--work-tree={dest}", "checkout-index", "-a", "-f"), env=env)
            except (GitError, OSError):
                left.append(path)
                continue
            for root, _, files in os.walk(dest):
                for name in files:
                    f = os.path.join(root, name)
                    if not os.path.islink(f):
                        os.chmod(f, os.stat(f).st_mode & ~0o222)
            if keep is not None:  # its index, the files' stat info fresh, for submodule_edits
                _run(src, (f"--work-tree={dest}", "update-index", "-q", "--refresh"), env=env,
                     check=False)
                name = f"submodule-{len(filled)}.index"
                with contextlib.suppress(OSError):  # copy2: its time is git's racy-file guard
                    shutil.copy2(env["GIT_INDEX_FILE"], keep / name)
                filled.append({"path": at + path, "src": str(src), "sha": sha, "index": name})
        left += [f"{path}/{p}" for p in _fill(src, sha, dest, keep, filled, f"{at}{path}/")]
    return left


def submodule_edits(worktree: Path) -> list[str]:
    """For each submodule fill_submodules filled in a task's worktree whose tracked files no
    longer match its commit (changed or deleted): why it can't land. The gitlink keeps them out
    of the diff and the commit, and read-only files stop no `sed -i`, which replaces a file
    through its directory: an edit the tests passed on vanished at wb accept (#419, the audit of
    1.8). Checked with the user's checkout of it as git dir and a copy of the index made at fill
    time, so the agent's files are only read; a submodule that can't be checked is said too.
    New untracked files aren't edits: submodule_check."""
    return submodule_check(worktree)[0]


def submodule_check(worktree: Path) -> tuple[list[str], list[str]]:
    """(submodule_edits, a note per filled submodule with new files not ignored there). New files
    are left behind as any submodule checkout leaves them: counted as edits, the build output a
    test command wrote in a vendored submodule (`make -C vendor/lua`) refused every wb accept and
    came back with the next test run (#889, the third 2026-11-21 audit)."""
    rec = _record(worktree)
    subs = store.read_json(rec.with_name(SUBMODULES), []) if rec else []
    out, notes = [], []
    for sub in subs if isinstance(subs, list) else []:
        dest = Path(worktree) / sub["path"]
        if not dest.is_dir() or dest.is_symlink():
            out.append(_edit_line(sub, ["the directory itself, removed or replaced"]))
            continue
        store.home().mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=store.scratch(".submodule-"), dir=store.home()) as scratch:
            env = {"GIT_INDEX_FILE": os.path.join(scratch, "index")}
            opts = (*NO_HOOKS, "-c", "core.fsmonitor=false", f"--work-tree={dest}")
            try:
                try:
                    # with its own time: git trusts an entry's stat (whole seconds, inode, size)
                    # unless the file is as new as the index. A fresh copy made every entry look
                    # older, so a same-size rewrite in the second the fill wrote it, on an inode
                    # the filesystem reused, read as unchanged, and the edit vanished at accept
                    # (#419 again; CI on ext4, batch 7 of 2026-11-21)
                    shutil.copy2(rec.parent / sub["index"], env["GIT_INDEX_FILE"])
                except OSError:
                    _run(sub["src"], ("read-tree", sub["sha"]), env=env)
                _run(sub["src"], (*opts, "update-index", "-q", "--refresh"), env=env, check=False)
                changed = _run(sub["src"], (*opts, "diff-files", "--name-only", "-z",
                                            "--ignore-submodules=all"), env=env).stdout
                new = _run(sub["src"], (*opts, "ls-files", "--others", "--exclude-standard", "-z"),
                           env=env).stdout
            except (GitError, OSError):
                # skipped, an edit there went unreported and vanished at accept: the #419
                # failure, back for a task whose repo moved (#682)
                out.append(f"edits in submodule {sub['path']} can't be checked: its checkout "
                           f"{sub['src']} is gone or unreadable (moved? wb relocate {rec.parent.name} "
                           "--repo <its new path>)")
                continue
        if files := [shown(f) for f in changed.split(b"\0") if f]:
            out.append(_edit_line(sub, files))
        if files := [shown(f) for f in new.split(b"\0") if f]:
            notes.append(f"new files in submodule {sub['path']} won't be committed: "
                         f"{', '.join(brief(files))}. The submodule doesn't track them, so the "
                         "gitlink leaves them out: build output can stay; a new source file "
                         "belongs in the submodule's own repository")
    return out, notes


def _edit_line(sub: dict, files: list[str]) -> str:
    return (f"edits in submodule {sub['path']} can't land: {', '.join(brief(files))}. Its files "
            f"are a copy of its commit {sub['sha'][:8]}, which the gitlink keeps out of the diff "
            "and the commit: undo them (or make the change in the submodule's own repository)")


def nested_repos(worktree: Path) -> list[str]:
    """New directories with a git repository of their own (a scaffolder's, a test fixture's): git
    lists only those as `dir/` among untracked files. Diffed, one failed or showed one line;
    committed, it became a gitlink to a commit nowhere else (the 2026-11-16 audit)."""
    return [f.rstrip("/") for f in git(worktree, "ls-files", "--others", "--exclude-standard",
                                       "-z").split("\0") if f.endswith("/")]


def nested_line(repos: list[str]) -> str:
    """The round report's line on nested_repos, so Claude hears of them before wb accept refuses."""
    return (f"NOTE: {', '.join(repos)}: a git repository of its own, left out of the diff and "
            "never committed: have its .git removed, or gitignore it")


def changed(worktree: Path, skip=()) -> str:
    """git status's lines, but for the paths in ``skip`` (the run caches commit_all leaves out)."""
    if not Path(worktree).is_dir():  # removed under us (an accept, a discard)
        return ""
    # porcelain: the `XY path` lines whatever the user's status.branch or colours say (a
    # status.branch=true put "## delegate/<task>" in every round's changed files: the audit)
    return git(worktree, "status", "--porcelain", "--untracked-files=all",
               *(["--", ".", *(":(exclude,literal)" + p for p in skip)] if skip else [])).rstrip()


# A changed-file list goes into every report and check-in Claude reads: whole, a build/ the
# project doesn't ignore added a line per file, thousands a wake (#251, the audit of 1.2)
CHANGED_SHOWN = 40  # lines listed as they are; the rest are counted by top-level directory
BUILD_FILES = 50    # new files in one new top-level directory: likely build output
NEW_FILES_MAX = 300  # wb accept and wb next commit no more new files than this without --include
BINARY_MAX = 1 << 20  # nor a new binary file this big

# A run's caches, which wb accept and wb next leave out of the commit unless someone wrote them:
# Claude runs the tests in the worktree (SKILL.md), bytecode on, and in a repo that ignores no
# __pycache__ the commit took a test run's .pyc that neither the review diff nor the accept line
# showed (#1580, 2026-11-26). runner.PREP_CACHES's and cli.CACHE_DIRS's caches, not an editor's
# .idea or .vscode, which a project may commit; a name matches any part of the path.
RUN_CACHES = frozenset({"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".hypothesis",
                        ".tox", ".nox", ".cache", ".vite", ".turbo", ".parcel-cache", ".nyc_output",
                        ".eslintcache"})
RUN_CACHE_SUFFIXES = (".pyc", ".pyo")


def run_caches(worktree: Path, written=()) -> list[str]:
    """The new files (untracked, not ignored) that are a run's caches, not anyone's work: under or
    named one of RUN_CACHES, or bytecode, and none of ``written`` (commit_all's: the agent's or
    Claude's own files are committed whatever their name)."""
    mine, out = set(written), []
    for f in git_bytes(worktree, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0"):
        p = os.fsdecode(f)
        if (p and not p.endswith("/") and p not in mine
                and (p.endswith(RUN_CACHE_SUFFIXES) or not RUN_CACHES.isdisjoint(p.split("/")))):
            out.append(p)
    return sorted(out)


def run_caches_line(paths: list[str]) -> str:
    """The note for the run caches a commit left out (run_caches), each cache directory once with
    its count: an accept line that named only the commit hid them (#1580)."""
    def where(p: str) -> str:
        parts = p.split("/")
        i = next((i for i, c in enumerate(parts) if c in RUN_CACHES), len(parts) - 1)
        return "/".join(parts[:i + 1]) + ("/" if i < len(parts) - 1 else "")
    groups = Counter(where(p) for p in paths)
    named = [f"{d} ({n:,} file{'s' if n != 1 else ''})" if d.endswith("/") else d
             for d, n in sorted(groups.items())]
    return ("note: the commit leaves out a run's caches, which nobody wrote: "
            + ", ".join(brief(named)))


def shown(path) -> str:
    """A path git gave as bytes, for a line a person reads: a byte that isn't UTF-8 as `\\xe9`.
    os.fsdecode kept it as a lone surrogate, which no UTF-8 write takes: a Latin-1 fixture's name
    in the left-out list crashed the runner's review notice, every wb resume again, and wb
    verify's result (#784)."""
    raw = path if isinstance(path, bytes) else os.fsencode(path)
    return raw.decode("utf-8", "backslashreplace")


def _top(path: str) -> str:
    path = path.strip('"')
    return path.split("/", 1)[0] + "/" if "/" in path else "(top level)"


def brief(lines: list[str], *, status: bool = False) -> list[str]:
    """Show the first paths and count the rest by directory, with a bound on both lists."""
    rest = Counter(_top(line[3:] if status else line) for line in lines[CHANGED_SHOWN:])
    out = lines[:CHANGED_SHOWN]
    for directory, count in rest.most_common(CHANGED_SHOWN):
        out.append(f"{directory} ({count:,} more file{'s' if count != 1 else ''})")
    if len(rest) > CHANGED_SHOWN:
        out.append(f"… {len(rest) - CHANGED_SHOWN:,} other directories")
    return out


def brief_paths(files: str) -> str:
    """Status paths as one bounded line for a check-in."""
    return ", ".join(brief([line[3:] for line in files.splitlines()])) or "none"


def build_dirs(worktree: Path, files: str) -> list[tuple[str, int]]:
    """New top-level directories (none at HEAD) holding BUILD_FILES or more untracked files:
    likely build output the project doesn't ignore. [(dir/, files)]."""
    counts = Counter(_top(x[3:]) for x in files.splitlines() if x.startswith("??") and "/" in x[3:])
    if not counts or counts.most_common(1)[0][1] < BUILD_FILES:
        return []
    tracked = set(git_bytes(worktree, "ls-tree", "--name-only", "-z", "HEAD").split(b"\0"))
    return [(d, n) for d, n in counts.most_common()
            if n >= BUILD_FILES and os.fsencode(d[:-1]) not in tracked]


SNAPSHOT_MAX = 1 << 25  # a review snapshot stores at most this many bytes of new files


def _new_files(worktree: Path) -> list[tuple[bytes, int]]:
    """The new files (untracked, not ignored) and their sizes, from `ls-files --others` and
    lstat alone: nothing is read or hashed. A symlink or anything but a regular file counts 0;
    nested repositories (nested_repos) are left out."""
    top, out = os.fsencode(os.path.abspath(worktree)), []
    for f in git_bytes(worktree, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0"):
        if f and not f.endswith(b"/"):
            try:
                st = os.lstat(os.path.join(top, f))
            except OSError:  # gone mid-scan
                continue
            out.append((f, st.st_size if stat.S_ISREG(st.st_mode) else 0))
    return out


def _binary(worktree: Path, path: bytes) -> bool | None:
    """Whether git would store ``path`` as a binary file, None when a filter (Git LFS) decides
    what is stored: the `diff` attribute, else a NUL in its first 8000 bytes, as git's own test."""
    out = _run(worktree, ["check-attr", "-z", "filter", "diff", "--", os.fsdecode(path)],
               check=False).stdout.split(b"\0")
    attrs = {out[i + 1]: out[i + 2] for i in range(0, len(out) - 2, 3)}
    if attrs.get(b"filter", b"unspecified") not in (b"unspecified", b"unset"):
        return None
    if attrs.get(b"diff") in (b"set", b"unset"):
        return attrs[b"diff"] == b"unset"
    try:
        fd = os.open(os.path.join(os.fsencode(os.path.abspath(worktree)), path),
                     os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as f:
            return b"\0" in f.read(8000)
    except OSError:
        return False


def _bulk(worktree: Path, skip=()) -> str | None:
    """Why the worktree's new files shouldn't be committed without --include, or None: more than
    NEW_FILES_MAX of them, a new binary of BINARY_MAX or more. Counted from the file system
    before anything is hashed: checked on a snapshot, every file the check then refused was
    already in the user's object store (#431, the audit of 1.8). Paths are bytes: a name that
    isn't UTF-8 is checked too. Not ``skip``: the run caches the commit leaves out (#1580)."""
    skip = {os.fsencode(p) for p in skip}
    added = [(f, n) for f, n in _new_files(worktree) if f not in skip]
    top = os.fsencode(os.path.abspath(worktree))
    for f in git_bytes(worktree, "diff", "--cached", "--name-only", "--no-renames",
                       "--diff-filter=A", "-z", "HEAD").split(b"\0"):  # staged, not at HEAD
        with contextlib.suppress(OSError):
            if f and stat.S_ISREG((st := os.lstat(os.path.join(top, f))).st_mode):
                added.append((f, st.st_size))
    why = []
    if len(added) > NEW_FILES_MAX:
        tops = Counter(_top(os.fsdecode(p)) for p, _ in added).most_common(3)
        why.append(f"{len(added):,} new files ({', '.join(f'{d} {n:,}' for d, n in tops)})")
    for path, size in added:
        if size >= BINARY_MAX and _binary(worktree, path):
            why.append(f"a {size / (1 << 20):.0f} MB binary file, {path.decode('utf-8', 'replace')}")
            if len(why) == 4:
                break
    return "; ".join(why) or None


MARK_READ_MAX = 1 << 20  # a new file past this counts by its size and time, not its bytes


def _untracked(path: Path) -> bytes:
    """A new file's part of the fingerprint. Never follows a link or opens anything but a
    regular file: a symlink to /dev/zero took the runner's memory, one to a FIFO blocked its
    loop (and with it every guard), and a core dump was re-read at every step (the audit)."""
    try:
        st = os.lstat(path)
        if stat.S_ISLNK(st.st_mode):
            return b"L" + os.fsencode(os.readlink(path))
        if not stat.S_ISREG(st.st_mode):
            return b"T%o" % st.st_mode
        if st.st_size > MARK_READ_MAX:
            return b"S%d:%d" % (st.st_size, st.st_mtime_ns)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as f:
            return b"F" + f.read(MARK_READ_MAX + 1)
    except OSError:  # gone mid-scan
        return b""


def case_renames(worktree: Path, env=None) -> list[tuple[bytes, bytes]]:
    """The index's paths the agent renamed by case only, as (old, new), on a file system that
    folds case (empty elsewhere). There the task's git runs with core.ignorecase=true, which is
    right, but it then matches Helpers.py in the index to helpers.py on disk: `add -A` staged no
    rename, the review diff didn't show it and wb accept committed Helpers.py beside a main.py
    importing helpers (#656). The agent can't `git mv` (the shim refuses it), so each component
    is read back from its directory: an exact name missing, one name equal but for case (and
    Unicode normalization, as APFS compares) present, is the agent's new name."""
    if not _case_folds(os.path.dirname(os.path.abspath(worktree))):
        return []
    tracked = [p for p in _run(worktree, ("ls-files", "-z"), env).stdout.split(b"\0") if p]
    have, listed, out = set(tracked), {}, []
    top = os.fsencode(os.path.abspath(worktree))
    for path in tracked:
        here, real = top, []
        for part in path.split(b"/"):
            if here not in listed:
                try:
                    listed[here] = set(os.listdir(here))
                except OSError:  # gone, or a file now: not a rename
                    listed[here] = set()
            if part not in listed[here]:
                twin = [n for n in listed[here] if _folded(n) == _folded(part)]
                if len(twin) != 1:
                    break
                part = twin[0]
            real.append(part)
            here = os.path.join(here, part)
        else:
            if (new := b"/".join(real)) != path and new not in have:
                out.append((path, new))
    return out


def record_case_renames(worktree: Path, env=None) -> None:
    """Stage case_renames in the index ``env`` names (a scratch one, or the worktree's own for
    the commit), before `add -A`, so the review shows R Helpers.py -> helpers.py (#656)."""
    if pairs := case_renames(worktree, env):
        spec = lambda p: ":(literal)" + os.fsdecode(p)  # noqa: E731
        _run(worktree, ["rm", "--cached", "-q", "--", *(spec(o) for o, _ in pairs)], env)
        _run(worktree, ["add", "--", *(spec(n) for _, n in pairs)], env)


def mark(worktree: Path) -> str:
    """A fingerprint of the working tree's uncommitted state, new files' contents included: it
    changes when, and only when, the agent changes a file (a new file past MARK_READ_MAX counts
    by its size and time, a symlink by its target). The index is left alone. Raw bytes: a decoded
    diff would hide an edit from one non-UTF-8 byte to another."""
    h = hashlib.sha1()
    git_hash(worktree, h, "diff", "--no-ext-diff", "--no-color", "HEAD")
    for f in git_bytes(worktree, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0"):
        if f:
            h.update(f + b"\0" + _untracked(Path(worktree) / os.fsdecode(f)))
    for old, new in case_renames(worktree):  # invisible to both above where case folds (#656)
        h.update(b"R" + old + b"\0" + new + b"\0")
    return h.hexdigest()


def with_new_files(worktree: Path, *args, whole: bool = False, limit: int | None = None,
                   new: list[bytes] | None = None, force=()) -> str | None:
    """`git <args>` with new files counted as changes (intent-to-add), in a scratch index: the
    worktree's own index, and anything a concurrent git command holds, are left alone. The
    scratch index starts as a copy of the worktree's, so sparse-checkout's skip-worktree entries
    aren't read as deletions (read-tree listed a monorepo's every file outside the cone as
    deleted: the 2026-11-16 audit). Nested repositories are left out (nested_repos). ``whole``
    adds new files' contents too, not just their intent (for write-tree: snapshot). ``limit``:
    at most that many characters of output, else None (_bounded). ``new``: only these new files
    are added, beside the tracked files' changes (snapshot_left). ``force``: files git ignores
    added too (`add -f`), the ones wb accept commits with --include (runner.ignored_to_commit):
    left out, an agent's feedback-round edit of its lib/util.py read "the diff since your last
    review: (no changes)" and --include committed code Claude never saw (#1259, 2026-11-24)."""
    # in a private directory of the state dir, not the temp dir: /tmp is in the agent's lane, and
    # an agent process still running could rewrite the index between `add -N` and the diff, so
    # files dropped out of the diff Claude reviews (the 2026-11-16 audit)
    from . import store
    store.home().mkdir(parents=True, exist_ok=True)
    scratch = tempfile.mkdtemp(prefix=store.scratch("wb-index-"), dir=store.home())
    index = os.path.join(scratch, "index")
    env = {"GIT_INDEX_FILE": index}
    try:
        try:
            real = git(worktree, "rev-parse", "--git-path", "index").strip()
            shutil.copyfile(Path(worktree) / real, index)  # a relative path is the worktree's
        except (GitError, OSError):
            git(worktree, "read-tree", "HEAD", env=env)
        record_case_renames(worktree, env)
        if new is not None:
            _run(worktree, ["add", "-u"], env)
            if new:
                add_all(worktree, *([] if whole else ["-N"]), "--",
                        *(":(literal)" + os.fsdecode(p) for p in new), env=env)
        else:
            skip = [f":(exclude){p}" for p in nested_repos(worktree)]
            add_all(worktree, *([] if whole else ["-N"]), *(["--", ".", *skip] if skip else []),
                    env=env)
        if force := [p for p in force if os.path.lexists(os.path.join(worktree, p))]:
            add_all(worktree, "-f", *([] if whole else ["-N"]), "--",
                    *(":(literal)" + p for p in force), env=env)
        return _bounded(worktree, args, env, limit) if limit else git(worktree, *args, env=env)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def add_all(worktree: Path, *args, env=None, pre=()) -> None:
    """`git add -A <args>`, with `--sparse` when a sparse checkout refused it: a file the agent
    made outside the patterns (a new package beside the cone) made git 2.34+ exit 1, which crashed
    the round report and stopped wb diff, verify, next and accept on the task (#493, the audit of
    1.90). Retried only after that refusal: a git older than --sparse neither has it nor refuses."""
    r = _run(worktree, [*pre, "add", "-A", *args], env, check=False)
    if r.returncode and git(worktree, "config", "--bool", "core.sparseCheckout",
                            check=False).strip() == "true":
        if not _run(worktree, [*pre, "add", "-A", "--sparse", *args], env, check=False).returncode:
            return
    if r.returncode:
        err = (r.stderr or r.stdout).decode("utf-8", "replace").strip()
        raise GitError(f"git add -A {' '.join(args)}: {err}")


def _bounded(cwd, args, env, limit: int) -> str | None:
    """git's output as text when it is at most ``limit`` characters, else None, read as it
    streams and git stopped past the limit: a 19 MB generated file's patch was captured and
    decoded whole (a 55 MiB peak) only for the review notice to show its stat (#348, the audit
    of 1.4). A character is at most 4 bytes, so past 4 * limit bytes it can't fit."""
    with tempfile.TemporaryFile() as err:
        p = subprocess.Popen(["git", "-C", str(cwd), *PLAIN, *_pinned(cwd), *args],
                             stdout=subprocess.PIPE, stderr=err, env=_env(env))
        out = p.stdout.read(4 * limit + 1)
        over = len(out) > 4 * limit
        if over:
            p.kill()
        p.stdout.close()
        p.wait()
        if over:
            return None
        if p.returncode:
            err.seek(0)
            raise GitError(f"git {' '.join(args)}: {err.read().decode('utf-8', 'replace').strip()}")
    text = out.decode("utf-8", "replace")
    return text if len(text) <= limit else None


def snapshot(worktree: Path, landing: bool = False) -> str:
    """The working tree as it is now, new files included, as a tree object: a base to diff
    against later. The worktree's own index is left alone. Bulky new files are left out
    (snapshot_left)."""
    return snapshot_left(worktree, landing)[0]


def has_tree(cwd: Path, rev: str) -> bool:
    """Whether ``rev`` still names a tree here. A review snapshot (snapshot_left) is reachable from
    nothing but its REVIEW_REF (#1198), so without one, a review from before then or a ref deleted
    by hand, git gc prunes it once gc.pruneExpire passes (#795)."""
    return not _run(cwd, ("cat-file", "-e", f"{rev}^{{tree}}"), check=False).returncode


def snapshot_left(worktree: Path, landing: bool = False, force=()) -> tuple[str, list[str]]:
    """snapshot's tree, and what it left out ("build/ (400 new files)", "data.bin (3 MB)"; []
    when nothing): a new file of BINARY_MAX or more, the top-level directories with the most new
    files while there are more than NEW_FILES_MAX, then the biggest new files while the rest
    pass SNAPSHOT_MAX bytes. Chosen by count and size alone, before anything is hashed: `add -A`
    wrote every new file into the user's object store, each round it changed, kept until git's
    prune window passed (a refused accept of 400 x 100 KB build/ files took .git/objects from
    28K to 41M: #431, the audit of 1.8). ``landing``: the tree wb accept would commit (wb
    verify's candidate): only what accept refuses without --include is left out, a new binary
    of BINARY_MAX or more and the directories past NEW_FILES_MAX, and a big text file, which
    accept commits, is kept: verify ran without a 1.1 MB text file its test read, said proven,
    and the test failed on main once accept had landed it (#642, Codex's audit of 1.90.6).
    ``force``: ignored files it holds too (with_new_files, #1259)."""
    return review_snapshot(worktree, landing, force)[:2]


KEEP_MAX = 100  # the paths a review tree records as left out (keep) before they go by directory


def review_snapshot(worktree: Path, landing: bool = False, force=()) -> tuple[str, list[str], list[str]]:
    """snapshot_left's tree and lines, and the paths it left out as wb rewind matches them
    (``keep``: `dir/` for a directory, else a file, as `shown` writes it; past KEEP_MAX by
    top-level directory): a rewind to the tree leaves them as they are, since the tree has no
    copy of them to put back (#1198)."""
    files = _new_files(worktree)
    if landing:
        big = [(p, n) for p, n in files if n >= BINARY_MAX and _binary(worktree, p)]
    else:
        big = [(p, n) for p, n in files if n >= BINARY_MAX]
    skip = set(big)
    rest = [r for r in files if r not in skip]
    dirs, count, keep = [], Counter(_top(shown(p)) for p, _ in rest), []
    for d, n in count.most_common():
        if len(rest) <= NEW_FILES_MAX:
            break
        dirs.append(f"{d} ({n:,} new file{'s' if n != 1 else ''})")
        # "(top level)" is no directory: its files go by name
        keep += [d] if d.endswith("/") else [shown(p) for p, _ in rest if _top(shown(p)) == d]
        rest = [(p, m) for p, m in rest if _top(shown(p)) != d]
    rest.sort(key=lambda r: r[1])
    while not landing and rest and sum(n for _, n in rest) > SNAPSHOT_MAX:
        big.append(rest.pop())
    if len(rest) == len(files):
        return with_new_files(worktree, "write-tree", whole=True, force=force).strip(), [], []
    keep += [shown(p) for p, _ in big]
    if len(keep) > KEEP_MAX:  # bounded in status.json; keeping more only deletes less
        keep = sorted({_top(k) if "/" in k else k for k in keep})
    tree = with_new_files(worktree, "write-tree", whole=True, new=[p for p, _ in rest],
                          force=force).strip()
    return tree, dirs + brief([f"{shown(p)} ({n / (1 << 20):.0f} MB)" if n >= 1 << 20
                               else f"{shown(p)} ({n >> 10:,} KB)" for p, n in sorted(big)]), sorted(keep)


# Each review tree of a task's step, pinned (#1198): a review snapshot is reachable from nothing,
# so git gc pruned it (#795), and wb rewind had no tree to go back to. One ref per review round,
# in the user's repo as ACCEPTED_REF is, deleted at wb next, accept and discard (drop_reviews).
REVIEW_REF = "refs/workbench/review/{}/{}"


def keep_review(cwd: Path, task: str, rnd: int, tree: str) -> bool:
    """Pin round ``rnd``'s review ``tree`` at REVIEW_REF; whether it took. Never fails a review."""
    return not _run(cwd, ("update-ref", "-m", "workbench: review", REVIEW_REF.format(task, rnd),
                          tree), check=False).returncode


# A run's review item's base (#1503, 2026-11-26): round ``rnd``'s review tree committed on the
# coder's base, so the reviewer's worktree holds the change it reviews. REVIEW_REF is a bare tree
# no worktree can check out, so every review started at the run's base, searched for the change
# and retyped it from the diff to run a test, on the base. Under the coder's REVIEW_REF, so
# drop_reviews deletes it with them; committed as wb, never signed: it lands on no branch.
REVIEWED_REF = "refs/workbench/review/{}/{}-commit"
REVIEWED_IDENT = {"GIT_AUTHOR_NAME": "workbench", "GIT_AUTHOR_EMAIL": "workbench@localhost",
            "GIT_COMMITTER_NAME": "workbench", "GIT_COMMITTER_EMAIL": "workbench@localhost"}


def review_commit(repo: Path, task: str, rnd: int, tree: str, parent: str) -> str | None:
    """Round ``rnd``'s review ``tree`` of ``task`` as a commit on ``parent``, pinned at
    REVIEWED_REF; None when git can't make or pin it."""
    r = _run(repo, ("commit-tree", "--no-gpg-sign", "-p", parent, "-m",
                    f"workbench: {task} round {rnd}, as reviewed", tree), env=REVIEWED_IDENT, check=False)
    sha = r.stdout.decode().strip() if not r.returncode else ""
    if not sha or _run(repo, ("update-ref", "-m", "workbench: reviewed", REVIEWED_REF.format(task, rnd), sha),
                       check=False).returncode:
        return None
    return sha


def drop_reviews(repo: Path, task: str) -> int:
    """Delete every REVIEW_REF of ``task``, in one transaction; how many there were."""
    refs = git(repo, "for-each-ref", "--format=%(refname)", REVIEW_REF.format(task, ""),
               check=False).split()
    if refs:
        _run(repo, ("update-ref", "--stdin"), check=False,
             input="".join(f"delete {r}\n" for r in refs).encode())
    return len(refs)


def _kept(path, keep) -> bool:
    s = shown(path)
    return any(s == k or (k.endswith("/") and s.startswith(k)) for k in keep)


def _tree_diff(wt: Path, a: str, b: str) -> list[tuple[bytes, bytes]]:
    """(status, path) for each file that differs from tree ``a`` to ``b``, submodules left out."""
    raw = git_bytes(wt, "diff-tree", "-r", "-z", "--raw", "--no-renames", a, b).split(b"\0")
    out = []
    for meta, path in zip(raw[0::2], raw[1::2]):
        fields = meta.split()
        if len(fields) < 5 or b"160000" in (fields[0].lstrip(b":"), fields[1]):
            continue  # a submodule's commit: its files are in neither tree
        out.append((fields[4], path))
    return out


def _unlink_in(top: bytes, paths: list[bytes]) -> list[bytes]:
    """Delete each of ``paths`` under the worktree ``top``, and the directories it empties; the
    ones deleted."""
    done = []
    for path in paths:
        f = os.path.join(top, path)
        parent = os.path.realpath(os.path.dirname(f))
        # never through a link out of the worktree: a parent swapped for one since the snapshot
        if parent != top and not parent.startswith(top + b"/") or not os.path.lexists(f) \
                or os.path.isdir(f) and not os.path.islink(f):
            continue
        os.unlink(f)
        done.append(path)
        d = os.path.dirname(f)
        while d != top:
            try:
                os.rmdir(d)  # only an empty one: git tracks no directory of its own
            except OSError:
                break
            d = os.path.dirname(d)
    return done


def restore_tree(worktree: Path, tree: str, keep=(), now: str | None = None,
                 now_keep=(), force=()) -> dict:
    """Put the worktree's files back to ``tree``, a review snapshot (wb rewind, #1198). Only what
    differs from the worktree now (its own snapshot) is touched: a file the tree has and the
    worktree lacks or holds otherwise is written from a scratch index (`read-tree` and
    `checkout-index -f`, as fill_submodules does), and one the worktree has and the tree lacks is
    deleted, unless ``keep`` (review_snapshot's paths left out of the tree) names it, since the
    tree holds no copy of it. A file the worktree's own snapshot left out (``now_keep``, its
    review_snapshot's third value) is never written over either: no tree holds that copy, and a
    2 MB file the agent grew from round 2's 10 KB got round 2's copy, gone for good, while the
    rewind said it was left as it was (#1198's review). Ignored files, nested repositories and
    submodules are in neither tree, and stay, but for ``force``: the ignored files the agent
    wrote, which the runner's review trees hold (#1259) and both snapshots here take too. The
    tree's .gitignore, written back, can stop
    ignoring a file a later round made and ignored (a secret.env wb accept would then have
    committed, in no round's review): the worktree is snapshotted again, and a file new in it
    that neither tree has is deleted. Nothing is committed and the worktree's own index is left
    alone: a `read-tree HEAD` there would drop a sparse checkout's skip-worktree bits, and every
    file outside the cone would read as deleted. ``now``: the worktree's snapshot, when the
    caller has just taken it (``now_keep`` then too). {"written", "deleted", "kept", "left",
    "unignored", "differs"}, the paths as `shown` writes them: "kept" the tree's ``keep`` the
    worktree has, "left" what ``now_keep`` kept from being written, "unignored" the files deleted
    after the second snapshot, "differs" what is still not as the tree has it (beside "kept"
    and "left"): a file a link or a directory kept, or one too big for the second snapshot
    that the tree lacks (one it has is as it has it: written, or alike in both snapshots)."""
    wt = Path(worktree)
    if now is None:
        now, _, now_keep = review_snapshot(wt, force=force)
    out = {k: [] for k in ("written", "deleted", "kept", "left", "unignored", "differs")}
    if now == tree:
        return out
    top = os.fsencode(os.path.realpath(wt))
    write, gone, kept, left = [], [], [], []
    first = _tree_diff(wt, tree, now)
    for status, path in first:
        if status == b"A":
            (kept if _kept(path, keep) else gone).append(path)
        elif status == b"D" and _kept(path, now_keep) and os.path.lexists(os.path.join(top, path)):
            left.append(path)  # the worktree's copy, which its snapshot left out: in no tree
        else:
            write.append(path)
    done = _unlink_in(top, gone)
    if write:
        store.home().mkdir(parents=True, exist_ok=True)  # the scratch index: never in the lane
        with tempfile.TemporaryDirectory(prefix=store.scratch("wb-index-"), dir=store.home()) as scratch:
            env = {"GIT_INDEX_FILE": os.path.join(scratch, "index")}
            _run(wt, ("read-tree", tree), env=env)
            _run(wt, ("checkout-index", "-f", "-z", "--stdin"), env=env,
                 input=b"".join(p + b"\0" for p in write))
    after, _, after_keep = review_snapshot(wt, force=force)
    unignored, differs = [], []
    if after != tree:
        seen, expected, new = {p for _, p in first}, set(kept) | set(left), []
        for status, path in _tree_diff(wt, tree, after):
            if _kept(path, after_keep):
                # too big for this snapshot, so missing from it: a.txt, as round 2 had it, was
                # named as differing once a later round's logs/ unignored pushed the total past
                # SNAPSHOT_MAX (#1198's review); `gained` below checks the tree has it
                continue
            if path in seen:
                if path not in expected:
                    differs.append(path)  # a link out, a directory: _unlink_in passed it by
            elif _kept(path, keep) or _kept(path, now_keep):
                continue  # left out of a snapshot, not ignored: named as left as it is
            elif status == b"A":
                new.append(path)  # in neither tree: ignored until the tree's .gitignore
            else:
                differs.append(path)
        unignored = _unlink_in(top, new)
        differs += [p for p in new if p not in unignored]
    # too big for the second snapshot, and left out of neither the tree nor the first snapshot
    gained = [k for k in after_keep if not _kept(k, keep) and not _kept(k, now_keep)]
    if gained:  # a file the tree holds, which this snapshot left out of a bigger total, is fine
        held = set(git_bytes(wt, "ls-tree", "-r", "-z", "--name-only", tree, "--",
                             *gained).split(b"\0"))
        differs += [os.fsencode(k) for k in gained if os.fsencode(k) not in held]
    out.update(written=[shown(p) for p in write], deleted=[shown(p) for p in done],
               kept=[shown(p) for p in kept], left=[shown(p) for p in left],
               unignored=[shown(p) for p in unignored],
               differs=list(dict.fromkeys(shown(p) for p in differs)))
    return out


def left_line(left: list[str]) -> str:
    """The review notice's line on what snapshot_left left out."""
    return (f"\n\nleft out of the review snapshot, too big to store each round: {', '.join(left)}"
            "; the next review's diff shows them again" if left else "")


def diff_base(task: store.Task, all_steps: bool = False) -> str:
    """What `wb diff` compares the worktree with: the task's base, or after `wb next` HEAD (the
    current step's work alone; ``all_steps``: the whole task). The web chat's task sheet reads
    it too, so the phone and `wb diff` show the same diff (#1201)."""
    return "HEAD" if task.status.get("steps", 1) > 1 and not all_steps else task.spec["base"]


def diff_tip(task: store.Task) -> str:
    """The task's work for a diff without its worktree: the accepted commit, else the branch
    (`--merge` deletes the branch: #477, the audit of 1.8)."""
    return (task.status.get("tip") or "")[:12] or task.spec["branch"]


def diff(worktree: Path, base: str, stat=False, paths=(), numstat=False, stat_count=None,
         limit: int | None = None, force=()) -> str | None:
    """The diff since ``base``, new files included. ``limit``: None when it is longer than that
    many characters, never held whole in memory (#348, the audit of 1.4). ``force``: ignored
    files it shows too (with_new_files, #1259)."""
    args = (["diff", "--no-ext-diff", "--no-color", base] + (["--stat"] if stat else []) + (["--numstat"] if numstat else [])
            + ([f"--stat-count={stat_count}"] if stat_count else []) + (["--", *paths] if paths else []))
    out = with_new_files(worktree, *args, limit=limit, force=force)
    return None if out is None else out.rstrip()


# Files a tool writes, not a person: a package manager's lockfiles, by name. With the files
# .gitattributes marks `linguist-generated` or `-diff`, they are counted apart from a change's
# size: one dependency bump's 2,400-line package-lock.json set a model's slice for good (#720).
LOCKFILES = frozenset({
    "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml", "bun.lock",
    "bun.lockb", "deno.lock", "Cargo.lock", "poetry.lock", "Pipfile.lock", "uv.lock", "pdm.lock",
    "Gemfile.lock", "composer.lock", "go.sum", "go.work.sum", "flake.lock", "mix.lock",
    "pubspec.lock", "Podfile.lock", "packages.lock.json", "gradle.lockfile", "Package.resolved",
    "conda-lock.yml", "pixi.lock", "MODULE.bazel.lock"})


def generated(worktree: Path, paths: list[str]) -> set[str]:
    """Which of ``paths`` a tool writes: a known lockfile, or one the repo's .gitattributes marks
    `linguist-generated` or `-diff`."""
    out = {p for p in paths if Path(p).name in LOCKFILES}
    rest = [p for p in paths if p not in out]
    if rest:
        try:
            r = _run(worktree, ["check-attr", "-z", "--stdin", "linguist-generated", "diff"],
                     input="".join(p + "\0" for p in rest).encode())
            words = r.stdout.decode("utf-8", "replace").split("\0")
        except (GitError, OSError):
            words = []
        for path, attr, value in zip(words[::3], words[1::3], words[2::3]):
            if (attr == "linguist-generated" and value in ("set", "true")) or \
                    (attr == "diff" and value == "unset"):
                out.add(path)
    return out


def footprint(worktree: Path, base: str) -> dict:
    """What a change spans since ``base``: files touched and lines added or removed, new files
    included, a renamed file under its new name. Generated files (`generated`) are left out of
    those and counted in `generated` ({files, lines, removed}, only when there are any); the
    files deleted whole are counted again in `deleted` ({files, removed}, when any): one call
    deletes them, which savings.py prices apart (#725)."""
    out = with_new_files(worktree, "diff", "--no-ext-diff", "--no-color", "--numstat", "-z", base)
    words, rows = (out or "").split("\0"), []
    i = 0
    while i < len(words):
        cols = words[i].split("\t")
        i += 1
        if len(cols) < 3:
            continue
        added, removed, path = cols[0], cols[1], cols[2]
        if not path:  # a rename: its old name, then its new one
            path, i = (words[i + 1] if i + 1 < len(words) else ""), i + 2
        rows.append((added, removed, path))
    made = generated(worktree, [p for _, _, p in rows if p])

    def total(rs):
        return {"files": len(rs), "lines": sum(int(a) for a, _, _ in rs if a.isdigit()),
                "removed": sum(int(r) for _, r, _ in rs if r.isdigit())}
    size = total([r for r in rows if r[2] not in made])
    gen = [r for r in rows if r[2] in made]
    gone = set(with_new_files(worktree, "diff", "--no-renames", "--name-only", "--diff-filter=D", "-z",
                              base).split("\0")) - {""} - made
    dead = total([r for r in rows if r[2] in gone])
    return {**size, **({"generated": total(gen)} if gen else {}),
            **({"deleted": {"files": dead["files"], "removed": dead["removed"]}} if dead["files"] else {})}


# Source files by extension, for the kind of work a repo holds (stack). Markup, data and docs
# say nothing about it and are left out. A `.h` is C, C++ or Objective-C, so it isn't here: it
# goes to whichever of those the repo's sources are mostly (HEADERS). Counted as C, a C++ repo's
# headers made it "c/c++" and C and C++ records mixed in the router (#908, the third audit of
# 2026-11-21), which also missed the extensions from mts to cu below.
LANGUAGES = {
    "py": "python", "js": "javascript", "mjs": "javascript", "cjs": "javascript", "jsx": "javascript",
    "ts": "typescript", "tsx": "typescript", "mts": "typescript", "cts": "typescript",
    "vue": "javascript", "svelte": "javascript", "astro": "javascript",
    "c": "c", "cc": "c++", "cpp": "c++", "cxx": "c++", "hpp": "c++", "hh": "c++", "hxx": "c++",
    "ipp": "c++", "inl": "c++", "cu": "cuda", "cuh": "cuda", "rs": "rust", "go": "go", "java": "java",
    "kt": "kotlin", "kts": "kotlin", "swift": "swift", "m": "objective-c", "mm": "objective-c",
    "cs": "c#", "fs": "f#", "fsx": "f#", "vb": "visual basic", "rb": "ruby", "php": "php", "lua": "lua",
    "zig": "zig", "dart": "dart", "scala": "scala", "ex": "elixir", "exs": "elixir", "hs": "haskell",
    "ml": "ocaml", "sh": "shell", "bash": "shell", "gd": "gdscript", "glsl": "glsl", "sql": "sql",
}
HEADERS = ("c", "c++", "objective-c")  # what a .h may be, the first when the repo has none of them
STACK_SHARE = 0.2  # a language holding at least this share of the source files is part of it


def stack(repo, subdir: str | None = None) -> list[str]:
    """The repo's main languages, most files first (at most two), from its tracked files; empty
    when it has no source or isn't a repository. ``subdir``: a monorepo package's alone (a Go
    service in a TypeScript monorepo was routed and recorded as TypeScript work, #820)."""
    try:
        names = git(repo, "ls-files", "-z", *(["--", subdir] if subdir else [])).split("\0")
    except (GitError, OSError):
        return []
    exts = Counter(Path(n).suffix[1:].lower() for n in names)
    langs = Counter()
    for x, n in exts.items():
        if x in LANGUAGES:
            langs[LANGUAGES[x]] += n
    if exts["h"]:  # max takes the first of HEADERS on a tie: c when the repo has none
        langs[max(HEADERS, key=lambda lang: langs[lang])] += exts["h"]
    total = sum(langs.values())
    return [lang for lang, n in langs.most_common(2) if n >= STACK_SHARE * total]


# an accepted task's tip, kept for wb bench add until #799 (STEPS_BUNDLE now): one from before
# then is moved into the bundle and deleted by wb gc (cli._released_refs)
ACCEPTED_REF = "refs/workbench/accepted/{}"


def keep_accepted(repo: Path, task: str, tip: str) -> None:
    """A ref of workbench's own at the accepted ``tip``, so its commits, every step's, outlive the
    delegate branch: a hash in status.json keeps no object alive, and after a squash accept (no
    ancestor of main), its branch deleted and git's reflogs and gc done, wb bench add could no
    longer find the step (#649, Codex's audit of 1.90.6). Only when bundle_steps couldn't make
    the bundle (#799). Never fails the accept."""
    git(repo, "update-ref", "-m", "workbench: accepted", ACCEPTED_REF.format(task), tip, check=False)


def drop_accepted(repo: Path, task: str, tip: str) -> bool:
    """Delete ``task``'s ACCEPTED_REF while it still points at ``tip`` (a compare-and-delete), once
    nothing needs it: a branch that holds the tip keeps every step alive, or each step has its
    bench case. Kept on every accept for good, each task's steps showed as a side line in `git log
    --all`, `clone --mirror` copied them, and gc could never prune them (#799, the second
    2026-11-21 audit). Whether it went."""
    return not _run(repo, ("update-ref", "-d", ACCEPTED_REF.format(task), tip),
                    check=False).returncode


def scratch_clone(repo: Path, dest: Path, bare: bool = False, tags: bool = True) -> None:
    """A `--shared --no-checkout` (or ``bare``) clone of ``repo`` at ``dest``, every scratch
    checkout's (the base check, wb verify and shot, bench's), borrowing the repo's objects
    through alternates. A shallow repo's clone ignores --shared, writes no alternates and
    pack-copies what its refs reach: the review snapshot's tree and a squashed task's earlier
    steps, which no ref reaches, weren't there (wb verify and shot died "failed to unpack tree
    object", bench add on step 2), and a big shallow checkout was copied whole for each base
    check (#884, the third 2026-11-21 audit). So there it is made by hand: init, alternates, the
    repo's `shallow` file, its branches (and tags) and HEAD. In the repo's own object format:
    a plain init takes git's default (or init.defaultObjectFormat), and a SHA-256 shallow repo's,
    or a SHA-1 one's under a SHA-256 default, refused every ref ("invalid <new-oid>", #884's
    review)."""
    dest = Path(dest)
    if git(repo, "rev-parse", "--is-shallow-repository", check=False).strip() != "true":
        git(dest.parent, "clone", "-q", "--shared", "--bare" if bare else "--no-checkout",
            *([] if tags else ["--no-tags"]), str(repo), str(dest))
        return
    common = Path(repo) / git(repo, "rev-parse", "--git-common-dir").strip()
    fmt = git(repo, "rev-parse", "--show-object-format").strip()
    git(dest.parent, "init", "-q", f"--object-format={fmt}", *(["--bare"] if bare else []),
        str(dest))
    gd = dest if bare else dest / ".git"
    info = gd / "objects" / "info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "alternates").write_text(f"{(common / 'objects').resolve()}\n")
    shutil.copyfile(common / "shallow", gd / "shallow")
    at = (f"--git-dir={gd}",)  # named: safe.bareRepository=explicit (#887)
    refs = git(repo, "for-each-ref", "--format=%(objectname) %(refname)", "refs/heads",
               *(["refs/tags"] if tags else []))
    if lines := [f"create {r} {sha}\n" for sha, _, r in
                 (row.partition(" ") for row in refs.splitlines()) if r]:
        _run(dest.parent, (*at, "update-ref", "--stdin"), input="".join(lines).encode())
    if head := git(repo, "symbolic-ref", "-q", "HEAD", check=False).strip():
        git(dest.parent, *at, "symbolic-ref", "HEAD", head)
    else:
        git(dest.parent, *at, "update-ref", "--no-deref", "HEAD",
            git(repo, "rev-parse", "HEAD").strip())


STEPS_BUNDLE = "steps.bundle"  # tasks/<task>/: an accepted task's steps, which wb bench add reads


def bundle_steps(repo: Path, base: str, tip: str, dest: Path) -> bool:
    """Keep an accepted task's steps, ``base``..``tip``, as a git bundle at ``dest`` in its own
    state, for wb bench add: a hash in status.json keeps no object alive (#649), and the ref of
    workbench's own that did, kept in the user's repo after a squash accept, showed each task's
    steps in `git log --all`, went out with every `clone --mirror` and kept whatever the agent
    once committed from git's gc (#799, the second 2026-11-21 audit). Made in a ``--shared``
    scratch clone, whose ref is the only one: the user's repo gets none, even for a moment.
    Whether it is there (made now or before); never fails the accept."""
    if dest.is_file():
        return True
    with tempfile.TemporaryDirectory(prefix="wb-steps-") as tmp:
        clone, part = Path(tmp) / "c", dest.with_name(f".{dest.name}.{os.getpid()}.tmp")
        try:
            scratch_clone(repo, clone, bare=True, tags=False)  # a shallow repo's too (#884)
            # named, not found from the cwd: safe.bareRepository=explicit refuses a bare repo git
            # discovers, the bundle was never made, and every accept kept the ref #799 removed
            # (#887, the third 2026-11-21 audit)
            bare = f"--git-dir={clone}"
            git(tmp, bare, "update-ref", "refs/heads/steps", tip)
            git(tmp, bare, "bundle", "create", "-q", str(part.resolve()), f"{base}..steps")
            os.replace(part, dest)
        except (GitError, OSError):
            part.unlink(missing_ok=True)
            return False
    return True


def restore_steps(task: store.Task) -> None:
    """Put an accepted task's steps back in its repo from its STEPS_BUNDLE when git's gc has
    pruned them (a squash accept, a branch deleted by hand: #799): ``bundle unbundle`` stores
    the objects alone, with no ref and no FETCH_HEAD, so `git log --all` never shows them and
    git's gc may prune them again once wb bench add has made its case."""
    tip, f = task.status.get("tip"), task.dir / STEPS_BUNDLE
    repo = repo_of(task.spec)
    if tip and f.is_file() and _run(repo, ("cat-file", "-e", f"{tip}^{{commit}}"),
                                    check=False).returncode:
        git(repo, "bundle", "unbundle", str(f.resolve()), check=False)


def name_status(repo: Path, base: str, rev: str, *opts: str) -> list[tuple[str, str]]:
    """(status letter, path) for each file ``rev`` changed since ``base``, renames as a delete and
    an add. NUL-separated: core.quotePath=false still C-quotes a name with a tab, newline, double
    quote or backslash ("tests/a\\tb.py"), and bench add, splitting --name-only on newlines, missed
    such a test or asked git for the quoted name (#942, the v1.90.24 review). wb verify's too."""
    raw = git(repo, "diff", "--name-status", "--no-renames", "-z", *opts, base, rev).split("\0")
    return [(st, p) for st, p in zip(raw[0::2], raw[1::2]) if p]


def step_base(task: store.Task) -> str:
    """Where the task's step under review began: the base for step one, else the last approved
    step's commit, which wb next records (status `step_base`). A step with no changes has no
    commit of its own: from before the record, the newest earlier step that has one, else the
    base (#652: the step after a clean one died on "cannot find the last approved step's
    commit"). wb verify and wb shot compare the step with it, and wb next, accept and discard
    measure it from it (results.step): measured from HEAD, a step whose commit an interrupted
    wb next (or accept, #879) had made read as 0 files and 0 lines, and the savings card priced
    the task at nothing (#1302, 2026-11-24)."""
    step = task.status.get("steps", 1)
    if step == 1:
        return task.spec["base"]
    if at := task.status.get("step_base"):
        return at
    for earlier in range(step - 1, 0, -1):
        if found := step_commit(task, earlier, step):
            return found
    return task.spec["base"]


def step_commit(task: store.Task, step: int, steps: int) -> str | None:
    """The commit that landed step `step`: `wb next` marks its steps "(step N)", `wb accept` the last."""
    spec = task.spec
    want = f"Workbench-Task: {task.name}" + (f" (step {step})" if step < steps else "")
    # the branch is deleted once merged (cli.gc); the tip is kept in STEPS_BUNDLE (bench add
    # restores it: restore_steps), or by ACCEPTED_REF for a task accepted before #799
    tip = task.status.get("tip") or spec["branch"]
    log = git(repo_of(spec), "log", "--format=%H%x00%B%x01", f"{spec['base']}..{tip}", check=False)
    for entry in log.split("\x01"):
        sha, _, body = entry.strip().partition("\x00")
        if sha and want in body.splitlines():
            return sha
    return None


SIGN_SECS = 10    # the dry signature at wb start: an unlocked key answers at once
COMMIT_SECS = 90  # a signed commit at accept: a pinentry nobody answers, under Claude's 2 minutes
UNLOCK_HOW = "ssh-add for an SSH key, or sign once in a terminal for GPG (echo x | gpg -s >/dev/null)"
UNLOCK = f"commits here are signed and the key needs unlocking first: {UNLOCK_HOW}; or --no-sign"
NO_TERMINAL = {"GPG_TTY": "", "DISPLAY": "", "WAYLAND_DISPLAY": "", "SSH_ASKPASS_REQUIRE": "never"}


def _unlock(repo: Path) -> str:
    signer = presence_signer(repo)
    return (f"commits here are signed by {signer}, which asks for a touch or an approval: none "
            "came; or --no-sign") if signer else UNLOCK


def signs(repo: Path) -> bool:
    return git(repo, "config", "--bool", "commit.gpgsign", check=False).strip() == "true"


# SSH agents that ask on each use (macOS's Secretive, 1Password's), by their socket's path
ASKING_AGENTS = {"secretive": "Secretive", "1password": "1Password"}


def presence_signer(repo: Path) -> str | None:
    """What signs ``repo``'s commits when it asks the user each time (a touch, an approval), else
    None: an SSH security key (`sk-`), a `gpg.ssh.program` of its own (1Password's op-ssh-sign), an
    SSH agent that asks per use, an OpenPGP key on a card. A dry signature made the key blink on
    every wb start, and on a timeout advised ssh-add, which can't make it sign (#528)."""
    fmt = git(repo, "config", "gpg.format", check=False).strip().lower() or "openpgp"
    key = git(repo, "config", "user.signingkey", check=False).strip()
    if fmt == "ssh":
        text = key.removeprefix("key::")
        # a path: the key file, or its .pub. None at all (gpg.ssh.defaultKeyCommand picks the
        # key): Path("") has no name, and its .pub raised a ValueError past main, a traceback at
        # every wb start (#662)
        if text and not text.startswith(("sk-", "ssh-", "ecdsa-")):
            path = Path(os.path.expanduser(text))
            for f in (path, path.with_name(path.name + ".pub")):
                with contextlib.suppress(OSError, ValueError):
                    head = f.read_bytes()[:4096]
                    if head.startswith(b"sk-"):
                        text = "sk-"
                        break
        if text.startswith("sk-"):
            return "a security key (an sk- SSH key)"
        if prog := git(repo, "config", "gpg.ssh.program", check=False).strip():
            return f"{os.path.basename(prog)} (gpg.ssh.program)"
        sock = os.environ.get("SSH_AUTH_SOCK", "").lower()
        return next((name for word, name in ASKING_AGENTS.items() if word in sock), None)
    if fmt != "openpgp":
        return None
    gpg = git(repo, "config", "gpg.program", check=False).strip() or "gpg"
    try:  # field 15 of a secret key's row: a token's serial number when the key is on a card
        out = subprocess.run([gpg, "--batch", "--with-colons", "--list-secret-keys",
                              *([key] if key else [])], capture_output=True, text=True,
                             timeout=SIGN_SECS, stdin=subprocess.DEVNULL).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    for row in out.splitlines():
        f = row.split(":")
        if len(f) > 14 and f[0] in ("sec", "ssb") and "s" in f[11] and f[14] not in ("", "#", "+"):
            return "an OpenPGP card"
    return None


def touch_line(signer: str) -> str:
    return (f"commits here are signed by {signer}, which asks for a touch or an approval: wb accept "
            f"waits {COMMIT_SECS} s for it; --no-sign commits unattended")


def unlock_how(repo: Path) -> str:
    """Why a signed commit failed and what makes it work: unlock a passphrase key, or be there
    for a key that asks each time, which ssh-add can't make sign (#528)."""
    signer = presence_signer(repo)
    return (f"{signer} asks for a touch or an approval nobody gave" if signer
            else f"the key needs unlocking first ({UNLOCK_HOW})")


def sign_problem(repo: Path, rev: str) -> str | None:
    """UNLOCK when ``repo``'s commits are signed and signing can't happen unattended (a key with a
    passphrase no agent holds, a pinentry that needs a terminal), else None. A dry commit-tree -S
    of ``rev``'s tree, with no terminal or display: wb accept failed at the very end of the task
    with "incorrect passphrase supplied" (#374, the audit of 1.6). It leaves a loose object. A key
    that asks each time gets no dry run, which only made it ask on every start (#528): it is
    said instead (touch_line)."""
    if not signs(repo):
        return None
    if signer := presence_signer(repo):
        return touch_line(signer)
    r = None
    with contextlib.suppress(GitError):
        r = _run(repo, ["commit-tree", "-S", "-m", "workbench: can it sign?", f"{rev}^{{tree}}"],
                 NO_TERMINAL, check=False, timeout=SIGN_SECS, detach=True)
    return None if r is not None and r.returncode == 0 else UNLOCK


def ignored_files(worktree: Path, paths) -> list[str]:
    """Of ``paths`` (relative to the worktree), the files there that git ignores and doesn't track."""
    if not (paths := sorted(set(paths))):
        return []
    out = git(worktree, "ls-files", "--others", "--ignored", "--exclude-standard", "-z", "--",
              *(":(literal)" + p for p in paths))
    return sorted({p for p in out.split("\0") if p})


CLOCK_SLACK = 0.05  # a file's times come from the kernel's coarse clock, a tick behind time.time()


def _kind(path: str) -> str:
    """A file's suffix, or its whole name when it has none: what hand_written matches on."""
    name = path.rsplit("/", 1)[-1]
    return os.path.splitext(name)[1].lower() or name


def _marks_cache(d: str) -> bool:
    """Whether directory ``d`` says it is a cache: a CACHEDIR.TAG (cargo's target/, pytest's), or
    a .gitignore whose one rule is `*` (pytest's, mypy's, ruff's, a venv's since Python 3.13)."""
    if os.path.lexists(os.path.join(d, "CACHEDIR.TAG")):
        return True
    try:
        fd = os.open(os.path.join(d, ".gitignore"), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as f:
            rules = [x.strip() for x in f.read(4096).splitlines()]
    except OSError:
        return False
    return [x for x in rules if x and not x.startswith(b"#")] == [b"*"]


def _text(full: str) -> bool:
    """No NUL in the first 8000 bytes, git's own test for text."""
    try:
        fd = os.open(full, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as f:
            return b"\0" not in f.read(8000)
    except OSError:
        return False


def _ignored_changed(worktree: Path, since: float):
    """hand_written's scan: (top, outside, kept, key) for the files git ignores and doesn't track
    that changed since ``since``: ``outside``, those under no cache; ``kept``, those of them text
    and of a kind the commit would hold; ``key(p)``, the outermost ignored directory holding p,
    else p itself. None when nothing changed."""
    top = os.path.abspath(worktree)
    changed = []
    for raw in git_bytes(worktree, "ls-files", "--others", "--ignored", "--exclude-standard",
                         "-z").split(b"\0"):
        if not raw:
            continue
        p = os.fsdecode(raw)
        try:
            st = os.lstat(os.path.join(top, p))
        except OSError:  # gone mid-scan
            continue
        if stat.S_ISREG(st.st_mode) and max(st.st_mtime, st.st_ctime) >= since - CLOCK_SLACK:
            changed.append(p)
    if not changed:
        return None
    held = git_bytes(worktree, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    kinds = {_kind(os.fsdecode(p)) for p in held.split(b"\0") if p}
    caches: dict[str, bool] = {}

    def in_cache(p: str) -> bool:
        parts = p.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            d = "/".join(parts[:i])
            if d not in caches:
                caches[d] = _marks_cache(os.path.join(top, d))
            if caches[d]:
                return True
        return False

    outside = [p for p in changed if not in_cache(p)]
    kept = [p for p in outside if _kind(p) in kinds and _text(os.path.join(top, p))]
    dirs = ignored_dirs(worktree)

    def key(p: str) -> str:
        parts = p.split("/")[:-1]
        return next((d for i in range(1, len(parts) + 1) if (d := "/".join(parts[:i])) in dirs), p)
    return top, outside, kept, key


def ignored_dirs(worktree: Path) -> set[str]:
    """The outermost directories git ignores in the worktree (`ls-files --directory`'s), relative."""
    return {os.fsdecode(d).rstrip("/") for d in git_bytes(
        worktree, "ls-files", "--others", "--ignored", "--exclude-standard", "--directory",
        "-z").split(b"\0") if d.endswith(b"/")}


def under(p: str, keys) -> bool:
    """Whether path ``p`` is one of ``keys`` or under one (ran_wrote's directories and files)."""
    return any(p == k or p.startswith(k + "/") for k in keys)


def ran_wrote(worktree: Path, since: float) -> list[str]:
    """Where the runner's own setup or test run, begun at ``since``, wrote what hand_written
    would take for Claude's (text, of a kind the commit holds, outside a cache): each such file's
    outermost ignored directory, else the file itself. A rerun of that command by Claude writes
    there again, and is no one's work: `mvn test`'s target/surefire-reports, tsc's dist/*.d.ts or
    jest's coverage/ refused wb accept and wb next as files written by hand, the runner's later
    runs rewrote them, and every accept after was refused again (#1211, 2026-11-24)."""
    if not (scan := _ignored_changed(worktree, since)):
        return []
    _, _, kept, key = scan
    return sorted({key(p) for p in kept})


def hand_written(worktree: Path, since: float, left: list[str] | None = None,
                 ran=()) -> list[str]:
    """The files git ignores and doesn't track that changed in the worktree since ``since`` (a
    time.time()) and look written by hand, not by a build or a test run: text, named like a file
    the commit would hold (its suffix, or its whole name when it has none), under no directory
    that marks itself a cache (_marks_cache), not where the runner's own runs wrote (``ran``,
    ran_wrote's: Claude's rerun of the acceptance command, #1211), and fewer than BUILD_FILES
    such files in their ignored directory (an install), else, ignored one by one, their top-level
    one. A module Claude
    wrote under Python.gitignore's lib/ after a takeover was left out of wb accept's commit and
    deleted with the worktree, with no warning: only the agent's edit tools were read (#1179,
    2026-11-24). A test run's .pyc, .pytest_cache/ and logs, or an npm install, must not refuse
    the commit (Claude runs the tests there). ``left`` gets the rest that are text outside a
    cache, fewer than BUILD_FILES such in their group, any suffix: the commit leaves them out,
    said, never silently dropped (left_out_line, #1179's review). The kind filter alone is
    waived, for the .csv beside Claude's module: unfiltered, a test run's bytecode was named at
    every accept, and an npm install's 20,000 paths went into status.json (#1179's review)."""
    if not (scan := _ignored_changed(worktree, since)):
        return []
    top, outside, kept, key = scan
    kept = [p for p in kept if not under(p, ran)]
    # Only the files the other filters keep count toward BUILD_FILES: counted before them, the
    # .pyc of Claude's first test run (agents run without bytecode) in a 60-module app/ cut
    # app/lib/helper.py, and accept --merge dropped it without a word (#1179, 2026-11-24).
    # Counted by ignored directory: by top-level one, setuptools' in-tree services/api/build/lib
    # copies of 60 modules cut services/api/lib/helper.py the same way (#1179's review)

    def group(p: str) -> str:
        return k if (k := key(p)) != p else _top(p)
    groups = Counter(group(p) for p in kept if "/" in p)
    out = sorted(p for p in kept if "/" not in p or groups[group(p)] < BUILD_FILES)
    if left is not None:
        keep = set(out)
        text = [p for p in outside if p not in keep and _text(os.path.join(top, p))]
        # with `groups` (kept's), every text file of the group: under BUILD_FILES, all its kept
        # ones are in out, so none is counted twice
        rest = Counter(group(p) for p in text if "/" in p)
        left.extend(sorted(p for p in text if "/" not in p
                           or groups[group(p)] + rest[group(p)] < BUILD_FILES))
    return out


def left_out_line(paths: list[str], worktree: Path, kept: Path | None = None, gone: bool = False) -> str:
    """The note for the ignored files changed in the worktree that the commit leaves out, as
    hand_written took them for a build's or a test run's: a .csv beside Claude's module under
    lib/ was dropped without a word, --include or not (#1179's review, 2026-11-24); the agent's
    shell's since its first round too, so not "since the agent stopped" (#1210). ``kept``: the
    copy wb accept keeps of them before the worktree goes; ``gone``: too many to copy; else where
    `git add -f` stages one."""
    head = (f"note: git ignores {len(paths)} more file{'s' if len(paths) != 1 else ''} changed "
            f"in the worktree, which the commit leaves out as build or test output "
            f"({', '.join(brief(paths))})")
    if kept is not None:
        return f"{head}: a copy is in {kept}"
    if gone:
        return f"{head}: too many to keep a copy, they go with the worktree"
    return f"{head}: git -C {shlex.quote(str(worktree))} add -f <file> has the next commit keep one"


def ignored_line(paths: list[str], who: str = "the agent wrote") -> str:
    """What the review and a refused commit say of the files written in the worktree that git
    ignores: the agent's in the review, the agent's or Claude's in a refused commit (#1179). The
    review diff and wb diff show them (force=, #1259), so it no longer says the diff leaves them out."""
    return (f"git ignores files {who} ({', '.join(brief(paths))}): the diff shows them, "
            "but a commit leaves them out and they'd go with the worktree: read them, then "
            "--include commits them, or have them moved or deleted")


def commit_all(worktree: Path, message: str, include: bool = False, sign: bool = True,
               written=(), left=(), dropped: list[str] | None = None) -> str:
    """Commit everything in the worktree, without git hooks (NO_HOOKS; also for a task started
    before its git dir was recorded) but signed if the user signs. Refuses a nested repository: committed, it was a gitlink to
    a commit that exists nowhere else. Refuses, unless ``include``, more than NEW_FILES_MAX new
    files or a large new binary: `add -A` committed a build/ the project didn't ignore (#251).
    Refuses, unless ``include``, which adds them, any of ``written`` (the files the agent wrote,
    and Claude's, #1179) that git ignores: `add -A` left a module under Python.gitignore's lib/
    out of the commit, and it was deleted with the worktree while main imported it (audit round 2
    of 1.91). A refusal names ``left`` too (left_out_line: what the commit leaves out, #1179's review).
    Leaves out, --include or not, the run caches that are none of ``written`` (run_caches: a
    test run's .pyc was committed unshown, #1580), and adds them to ``dropped``, for the note."""
    ignored = ignored_files(worktree, written)
    if ignored and not include:
        rest = sorted(set(left) - set(ignored))
        raise GitError("not committed: " + ignored_line(ignored, "written in the worktree")
                       + (f"\n{left_out_line(rest, worktree)}" if rest else ""))
    if nested := nested_repos(worktree):
        raise GitError(f"{', '.join(nested)}: a git repository of its own, so not committed: "
                       "remove its .git or gitignore it, then try again")
    if edits := submodule_edits(worktree):  # they would vanish with the worktree (#419)
        raise GitError("not committed: " + "; ".join(edits))
    caches = run_caches(worktree, written)
    if not include and (why := _bulk(worktree, caches)):
        raise GitError(f"not committed: {why}. Build output? Gitignore or remove it in "
                       f"{worktree}, or --include to commit it anyway")
    record_case_renames(worktree)
    add_all(worktree, *(["--", ".", *(":(exclude,literal)" + p for p in caches)] if caches else []),
            pre=NO_HOOKS)
    if dropped is not None:
        dropped.extend(caches)
    if ignored:
        git(worktree, *NO_HOOKS, "add", "-f", "--", *(":(literal)" + p for p in ignored))
    if not git(worktree, "diff", "--cached", "--name-only").strip():
        raise GitError("nothing to commit: the agent's worktree has no changes")
    # --no-sign, at start or now: the task's commits go unsigned (#374)
    commit(worktree, *NO_HOOKS, "commit", "-q", "-m", message, sign=sign)
    return git(worktree, "rev-parse", "--short", "HEAD").strip()


def commit(repo: Path, *args, sign: bool = True, env=None) -> None:
    """`git <args>`, a commit in the user's repo: unsigned when not ``sign``, else signed as their
    git says, with no terminal or display, detached, within COMMIT_SECS, and a failure said with
    how to unlock (#374, #933), not git's raw error. wb init's commits went through plain git: a
    key with a passphrase failed wb try at once with "incorrect passphrase supplied", and with a
    terminal (WSL2) it waited on a prompt until Claude's Bash limit killed it (#1226)."""
    if not sign:
        git(repo, "-c", "commit.gpgsign=false", *args, env=env)
    elif signs(repo):
        try:
            r = _run(repo, args, {**NO_TERMINAL, **(env or {})}, check=False, timeout=COMMIT_SECS,
                     detach=True)
        except GitError as e:  # no answer: a pinentry waiting on an unattended desktop
            raise GitError(f"not committed: {_unlock(repo)} ({e})") from None
        if r.returncode:
            said = (r.stderr or r.stdout).decode("utf-8", "replace").strip().splitlines()
            raise GitError(f"not committed: {_unlock(repo)} "
                           f"(git: {said[0] if said else r.returncode})")
    else:
        git(repo, *args, env=env)


def put_on_branch(repo: Path, branch: str, commit: str, worktree: Path) -> None:
    """Make ``branch`` the accepted ``commit`` before its worktree goes: already there, moved
    forward when it is an ancestor (a worktree on a detached HEAD or another branch), made when
    gone. Refuses a branch holding commits ``commit`` lacks, naming them and the way out: accept
    committed on a detached HEAD, said the branch had it and removed the only checkout that did
    (#417, the audit of 1.8)."""
    ref, sha = f"refs/heads/{branch}", git(repo, "rev-parse", "--verify", f"{commit}^{{commit}}").strip()
    have = git(repo, "rev-parse", "--verify", "-q", ref, check=False).strip()
    if have == sha:
        return
    if have and not contains(repo, sha, have):
        lost = git(repo, "log", "--format=%h %s", f"{sha}..{have}").splitlines()
        raise GitError(
            f"{branch} has {len(lost)} commit(s) the accepted {commit} lacks ("
            f"{'; '.join(lost[:5])}{'; …' if len(lost) > 5 else ''}), so the worktree {worktree} "
            f"is kept: bring them together there (git -C {shlex.quote(str(worktree))} rebase "
            f"{branch}), or drop them (git -C {shlex.quote(str(repo))} branch -f {branch} "
            f"{commit}), then accept again")
    git(repo, "update-ref", "-m", "workbench: accept", ref, sha, have)  # unmoved since it was read


def _rmtree(path: Path) -> None:
    for d, dirs, _ in os.walk(path):  # a read-only directory keeps its entries from deletion
        for x in (d, *(os.path.join(d, n) for n in dirs)):
            if not os.path.islink(x):
                try:
                    os.chmod(x, os.stat(x).st_mode | stat.S_IRWXU)
                except OSError:
                    pass
    shutil.rmtree(path, ignore_errors=True)
    if os.path.islink(path):
        os.unlink(path)


def remove_worktree(repo: Path, worktree: Path) -> str:
    """Remove a task's worktree and its registration; a warning when git couldn't, else "".
    Always --force: after accept's commit only ignored files and run caches (run_caches) are
    left, which git deletes anyway,
    and without it git runs `status` in the worktree. When git fails (a read-only directory, an
    embedded repo, a repo moved or deleted), a task's own worktree is deleted by hand: accept and
    discard once wedged there for good (the 2026-11-16 audit)."""
    worktree, warn = Path(worktree), ""
    if Path(repo).is_dir():
        _unlock_cut_short(repo, worktree)
    if worktree.exists() or worktree.is_symlink():
        try:
            if not Path(repo).is_dir():
                raise GitError(f"{repo} is gone")
            git(repo, *NO_HOOKS, "-c", "core.fsmonitor=false", "worktree", "remove", "--force",
                str(worktree))
        except GitError as e:
            if _record(worktree) is None:  # only ever a task's own worktree by hand
                return f"the worktree wasn't removed: {e}"
            _rmtree(worktree)
            warn = f"git couldn't remove the worktree ({e}); it was deleted"
    if Path(repo).is_dir():
        drop_registration(repo, worktree)
    return warn


def drop_registration(repo: Path, worktree: Path) -> None:
    """Forget ``worktree``'s registration in ``repo`` once the directory is gone (its admin dir,
    `<common git dir>/worktrees/<id>`, whose `gitdir` names it), unless it is locked. Only that
    one: a repository-wide `git worktree prune` after each task's removal dropped an unrelated
    worktree on a drive unmounted for the moment, whose `git status` then exited 128 and whose
    staged index was gone (#641, Codex's audit of 1.90.6)."""
    worktree = Path(worktree)
    if worktree.exists() or worktree.is_symlink():
        return  # still there: git's to remove
    common = common_dir(repo)
    admin = common / "worktrees" if common else None
    if not admin or not admin.is_dir():
        return
    at = {os.path.abspath(worktree), os.path.realpath(worktree)}
    for d in admin.iterdir():
        try:
            named = (d / "gitdir").read_text().strip()
        except OSError:
            continue
        # against the admin dir, as git reads it: with worktree.useRelativePaths (git 2.48+) the
        # link is relative, read against wb's cwd it matched nothing, and the registration and
        # its delegate branch stayed (#793, the second 2026-11-21 audit); an absolute one stays
        top = os.path.dirname(d / named)
        if {os.path.abspath(top), os.path.realpath(top)} & at and not (d / "locked").exists():
            shutil.rmtree(d, ignore_errors=True)


def _unlock_cut_short(repo: Path, worktree: Path) -> None:
    """`git worktree add` locks its worktree "initializing" until the checkout is done: killed in
    between (a runner's checkout stopped by wb discard, a reboot), the lock stayed, remove --force
    and prune both skipped it, and the branch checked out there couldn't be deleted (#376, #529).
    Only that lock goes, and only for a task's own worktree: one the user locked stays locked."""
    if _record(worktree) is None:
        return
    at = {os.path.abspath(worktree), os.path.realpath(worktree)}
    entry = None
    for line in git(repo, "worktree", "list", "--porcelain", check=False).splitlines():
        if line.startswith("worktree "):
            entry = line[9:]
        elif entry and line == "locked initializing" and \
                {os.path.abspath(entry), os.path.realpath(entry)} & at:
            git(repo, "worktree", "unlock", entry, check=False)
            return


def checked_out_in(repo: Path, branch: str) -> str | None:
    """The worktree that has ``branch`` checked out (the main one or a linked one), or None.
    `git branch -d/-D` refuse such a branch, but `update-ref -d` skips that check: wb gc deleted a
    landed delegate branch the user had out in a second worktree, whose HEAD then named no ref,
    `git status` showed every file staged and the next commit there had no parent (#930). A
    registration whose directory is gone (`prunable`) holds nothing: no checkout breaks."""
    held = entry = None
    for line in git(repo, "worktree", "list", "--porcelain", check=False).splitlines() + [""]:
        if line.startswith("worktree "):
            entry, held = line[9:], None
        elif line == f"branch refs/heads/{branch}":
            held = entry
        elif line.startswith("prunable"):
            held = entry = None
        elif not line and held:
            return held
    return None


def delete_branch(repo: Path, task: str, at: str | None = None) -> bool:
    """Delete the task's branch; with ``at``, only while it still points at that commit (one
    update-ref, compare and delete), so a branch someone else made or moved stays (#416), and
    never while a worktree has it checked out (#930). True when it went."""
    if at:
        branch = branch_name(task)
        return checked_out_in(repo, branch) is None and _run(
            repo, ("update-ref", "-d", f"refs/heads/{branch}", at), check=False).returncode == 0
    return _run(repo, ("branch", "-D", branch_name(task)), check=False).returncode == 0


def drop_merged_branch(repo: Path, branch: str) -> bool:
    """`git branch -d`: only a branch merged into the checkout's HEAD (or its upstream) goes.
    Merged delegate branches piled up, 8 in one repo (the 2026-11-19 audit)."""
    return _run(repo, ("branch", "-d", branch), check=False).returncode == 0


def drop_landed_branch(repo: Path, branch: str, tip: str, into: str | None) -> bool:
    """Delete ``branch`` once it has landed: merged into the checkout's HEAD (drop_merged_branch),
    else held by ``into`` (the task's base branch, not checked out), deleted only while it still
    points at ``tip`` (update-ref's compare-and-delete), and never while a worktree has it checked
    out: update-ref doesn't look, and that checkout's HEAD was left naming no ref (#930). wb gc
    (#686)."""
    if drop_merged_branch(repo, branch):  # git's own check refuses a checked-out branch
        return True
    return bool(into) and contains(repo, f"refs/heads/{into}", tip) and \
        checked_out_in(repo, branch) is None and _run(
            repo, ("update-ref", "-d", f"refs/heads/{branch}", tip), check=False).returncode == 0


def current_branch(repo: Path) -> str:
    """The checked-out branch, "" for a detached HEAD (any git: `branch --show-current` is 2.22+)."""
    return git(repo, "symbolic-ref", "--short", "-q", "HEAD", check=False).strip()


def jj_colocated(repo: Path) -> bool:
    """A Jujutsu repo colocated with git's (`.jj/` beside `.git`): jj keeps git's HEAD detached
    once it has moved the working copy, so no task there records a branch and `wb accept --merge`
    refused "a detached HEAD", with a git merge as the way out (#385, the audit of 1.6)."""
    return (Path(repo) / ".jj").is_dir() and (Path(repo) / ".git").exists()


def merge_by_hand(repo: Path, branch: str, how: str = "merge", sign: bool = True,
                  into: str | None = None) -> str:
    """The git command that lands ``branch`` as wb accept --merge would: in the repo's mode (the
    --merge-as, else commands.json's `merge`) and unsigned after --no-sign. The line said merge
    --no-ff whatever the repo kept to (#471, the audit of 1.8). With ``into`` (the branch it
    belongs on) not checked out, or HEAD detached, it switches there first: the line printed
    after "has feature-x out, not main" merged into feature-x, or onto a detached HEAD the next
    checkout dropped (#1154, 2026-11-24)."""
    g, nosign = f"git -C {shlex.quote(str(repo))}", "" if sign else " --no-gpg-sign"
    switch = f"{g} switch {shlex.quote(into)} && " if into and current_branch(repo) != into else ""
    if how == "ff":
        return f"{switch}{g} merge --ff-only {branch}"
    if how == "squash":
        return f"{switch}{g} merge --squash --ff {branch} && {g} commit{nosign} -C {branch}"
    return f"{switch}{g} merge --no-ff{nosign} {branch}"


def jj_merge(repo: Path, branch: str) -> str:
    """The jj command that merges ``branch`` onto the working copy's parent, as a new change."""
    return f"jj -R {shlex.quote(str(repo))} new @- {branch}"


def detached(repo: Path, branch: str, had: bool = False) -> str:
    """Why a merge onto the checkout's detached HEAD is refused, and how to merge instead."""
    if jj_colocated(repo):
        return (f"{repo} is a colocated Jujutsu repo, whose git HEAD jj keeps detached; merge "
                f"{branch} with jj: {jj_merge(repo, branch)}")
    return f"{repo} {'had' if had else 'has'} a detached HEAD out; merge {branch} by hand"


def start_branch(repo: Path, base: str) -> str | None:
    """The branch a task starts from: the one checked out for HEAD, else ``base`` when it names a
    local branch; None for a detached HEAD or a commit."""
    if base == "HEAD":
        return current_branch(repo) or None
    return base if _run(repo, ("rev-parse", "--verify", "-q", f"refs/heads/{base}"),
                        check=False).returncode == 0 else None


MERGE_LOCK = "workbench-merge.lock"  # in the destination checkout's git dir (merge_lock)
MERGE_WAIT = COMMIT_SECS + 15  # waiting on another merge into it: one signed commit's time


@contextlib.contextmanager
def merge_lock(repo: Path, say=None):
    """One wb merge at a time into a checkout: a flock on MERGE_LOCK in its own git dir (a linked
    worktree's, so two worktrees of one repo don't wait on each other), which every profile and
    account merging there shares. Two accepts into one checkout once interleaved: B read HEAD,
    A merged, B conflicted and its rollback reset to the HEAD it had read, rewinding A's merge
    while A's record said merged and its branch was gone (#639, Codex's audit of 1.90.6). The
    file is opened read-only (flock needs no write: another account made it). An agent can read
    the git dir and so could hold the lock: past MERGE_WAIT the merge is refused, never made.
    Read-write where it may be (store.lock_fd): NFSv4 refuses an exclusive flock on a read-only
    fd with EBADF, and the accept ended in a bare "Bad file descriptor" (#892)."""
    gd = git(repo, "rev-parse", "--absolute-git-dir").strip()
    try:
        fd = store.lock_fd(os.path.join(gd, MERGE_LOCK), 0o664)
    except OSError as e:
        raise GitError(f"can't lock {gd} for the merge ({e.strerror})") from None
    try:
        deadline, said = time.monotonic() + MERGE_WAIT, False
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise GitError(f"another merge into {repo} held it for {MERGE_WAIT}s") from None
                if say and not said:
                    say(f"waiting for another merge into {repo}")
                    said = True
                time.sleep(0.2)
            except OSError as e:  # a file system that can't lock it: say so, as a GitError (#892)
                raise GitError(f"can't lock {gd} for the merge ({e.strerror})") from None
        yield
    finally:
        os.close(fd)  # releases the lock


def merge(repo: Path, task: str, into: str | None = None, base: str | None = None,
          *, tip: str | None = None, how: str = "merge", sign: bool = True,
          started: Callable[[int], None] | None = None, message: str | None = None) -> str:
    """Merge the task's branch into ``into`` (the branch it started from: task.json's
    base_branch), which the main checkout must have out, clean and not mid-merge; a conflict, an
    ignored file in the way, or a merge git left unfinished is backed out, leaving the repo as it
    was, and success means the branch is in HEAD. With no branch recorded, the checkout's branch must contain
    ``base``, the task's base commit. Never onto a detached HEAD, where the merge is lost at the
    next checkout, nor onto another branch than the task's (both happened: the 2026-11-16 audit).
    ``how``: "merge" (a merge commit, the default), "ff" (fast-forward only, refused when the
    branch has moved) or "squash" (one new commit with the tip's message): a repo kept to linear
    history refused every --no-ff merge (#352, the audit of 1.4). The same guards hold for each.
    The commit it makes is signed as commit_all's is: with no terminal and COMMIT_SECS when the
    user signs, unsigned when ``sign`` is False (--no-sign); a timeout, a failure or an
    interruption (a SIGTERM, raised by the caller) backs the checkout out to where it was: a
    pinentry nobody answered hung accept until it was killed, leaving MERGE_HEAD and the staged
    merge in the user's checkout, and --no-sign's merge still signed (#420, the audit of 1.8).
    ``started(pid)``: the pid of each git that may sign, once it runs, for clear_killed_merge.
    ``message``: the squash's, in place of the tip's: wb accept's when it made no commit, the tip
    a step's, whose subject and `(step N)` trailer stood for the whole task on main (#1393)."""
    dirty, submodules = _uncommitted(repo)
    if dirty:
        raise GitError(f"{repo} has uncommitted changes ({_listed(dirty)}); merge "
                       f"{branch_name(task)} by hand")
    have = current_branch(repo)
    if not have:
        raise GitError(detached(repo, branch_name(task)))
    if into and into != "HEAD" and have != into:
        raise GitError(f"{repo} has {have} out, not {into}; merge {branch_name(task)} by hand")
    if (tip or not into or into == "HEAD") and base and _run(
            repo, ("merge-base", "--is-ancestor", base, "HEAD"), check=False).returncode:
        # a branch reset or rebased since the task started: the merge would bring back what was
        # withdrawn from it, so the commits are named and the choice is Claude's (#418)
        back = git(repo, "log", "--format=%h %s", f"HEAD..{base}").splitlines()
        b = branch_name(task)
        raise GitError(
            f"{repo} has {have} out, which doesn't hold the task's base {base[:12]} (reset or "
            f"rebased since?): merging would bring back {len(back)} commit(s) it lacks ("
            f"{'; '.join(back[:5])}{'; …' if len(back) > 5 else ''}). Rebase the task's commits "
            f"onto it (git -C {shlex.quote(str(repo))} rebase --onto {have} {base[:12]} {b}, "
            f"then git switch {have} && git merge {b}), or merge {b} by hand to bring them back")
    if _merging(repo):  # an --abort below would throw away the user's own merge
        raise GitError(f"{repo} is in the middle of a merge; finish it, then merge "
                       f"{branch_name(task)} by hand")
    source = tip or branch_name(task)  # the reviewed commit survives a moved delegate ref (#246)
    if in_the_way := _overwritten(repo, source):
        raise GitError(f"the merge would overwrite {', '.join(in_the_way)} in {repo}, untracked "
                       f"there (yours, maybe ignored): move it aside, then merge "
                       f"{branch_name(task)} by hand")
    written = set(_written(repo, source).split(b"\0")) if submodules else set()
    if touched := [f"submodule {p.decode('utf-8', 'replace')} {why}"
                   for p, why in submodules.items() if p in written]:
        raise GitError(f"the merge would change what {', '.join(touched)} in {repo} points at: "
                       f"bring it back (git submodule update), then merge {branch_name(task)} "
                       "by hand")
    before = git(repo, "rev-parse", "HEAD").strip()
    signed = sign and signs(repo)
    unsigned = () if sign else ("-c", "commit.gpgsign=false")
    # a fast-forward makes no commit, and an unsigned merge of a large checkout takes its time
    secs = COMMIT_SECS if signed and how != "ff" else None

    def committing(args):  # the step that makes a commit, so may sign
        r = _run(repo, [*unsigned, *args], NO_TERMINAL, check=False, timeout=secs, detach=signed,
                 started=started if signed else None)
        if r.returncode:
            said = (r.stderr or r.stdout).decode("utf-8", "replace").strip()
            raise GitError(f"git {' '.join(args)}: {said}")

    def failed(what, e):
        why = str(e)
        if signed and any(w in why for w in ("no answer in", "failed to sign", "failed to write commit")):
            why = (f"the commit is signed and {unlock_how(repo)}; then "
                   f"merge it by hand ({why.splitlines()[0]})")
        return GitError(f"{what} {branch_name(task)} failed and was backed out: {why}")

    # --commit and --no-squash outrank a branch.<name>.mergeOptions of --no-commit or --squash,
    # which left the merge pending while accept said merged (#190, the 2026-11-19 audit); a
    # fast-forward needs --no-squash too: --ff-only under mergeOptions=--squash staged a squash
    # and left HEAD (#430, the audit of 1.8). The squash says --ff: merge.ff=only refused it once
    # the branch had moved, merge.ff=false every time ("cannot be used together", #429)
    # No mode runs the repo's hooks (NO_HOOKS, --no-verify), as no commit of workbench's does:
    # a commit-msg hook refused the merge commit's message, which nobody could edit, while the
    # squash ran none and landed; and once staged the merge holds the agent's files, a relative
    # hooksPath's hooks among them (#499, Claude's audit of 1.90). The merge commit names the
    # branch as git's own would (fmt-merge-msg, the repo's merge.log and suppressDest kept), not
    # the tip's SHA it merges (#471, the audit of 1.8)
    try:
        if how == "squash":  # no MERGE_HEAD to abort: backed out with reset --merge
            git(repo, *NO_HOOKS, "merge", "--squash", "--ff", "--no-commit", source)
            committing([*NO_HOOKS, "commit", "--no-verify", "-q",
                        *(("--cleanup=verbatim", "-m", message) if message else ("-C", source))])
        elif how == "ff":
            committing([*NO_HOOKS, "merge", "--ff-only", "--no-squash", source])
        else:
            committing([*NO_HOOKS, "merge", "--no-ff", "--commit", "--no-squash", "--no-verify",
                        "--no-log", "-m", merge_message(repo, task, source), source])
    except BaseException as e:  # a conflict, a timeout, a SIGTERM: the checkout as it was (#420)
        _back_out(repo, before, source, how)
        if isinstance(e, GitError):
            raise failed("squashing" if how == "squash" else "merging", e) from None
        raise
    if how == "squash":
        if git(repo, "rev-parse", "HEAD").strip() == before:  # a no-op squash committed nothing
            raise GitError(f"squashing {branch_name(task)} left nothing to commit: merge it by hand")
        return git(repo, "rev-parse", "--short", "HEAD").strip()
    if _merging(repo) or _run(repo, ("merge-base", "--is-ancestor", source, "HEAD"),
                              check=False).returncode:
        # HEAD or the index changed with no MERGE_HEAD (a squash staged) is backed out too (#430)
        _back_out(repo, before, source, how)
        raise GitError(f"git didn't complete the merge of {branch_name(task)} (a merge setting "
                       "of yours?); it was backed out: merge it by hand")
    return git(repo, "rev-parse", "--short", "HEAD").strip()


def _uncommitted(repo: Path) -> tuple[list[str], dict[bytes, str]]:
    """(the checkout's uncommitted changes to tracked files, {submodule: how it differs} for
    each submodule changed only in its own checkout). A submodule ahead of its gitlink (a pull
    without submodule update) or edited inside stays as it is through a merge that doesn't
    change its gitlink, and was refused as "uncommitted changes", naming nothing (#505, Claude's
    audit of 1.90). --ignore-submodules=none: a setting of the user's that hides one mustn't hide
    it from the merge's check."""
    out = _run(repo, ("status", "--porcelain=v2", "-z", "--untracked-files=no",
                      "--ignore-submodules=none")).stdout.split(b"\0")
    dirty, subs, rows = [], {}, iter(out)
    for row in rows:
        if not row:
            continue
        kind, fields = row[:1], row.split(b" ")
        n = {b"1": 8, b"2": 9, b"u": 10}.get(kind)
        if n is None:
            continue
        path = b" ".join(fields[n:])
        if kind == b"2":
            next(rows, None)  # the rename's original path
        xy, sub = fields[1].decode(), fields[2].decode()
        if kind == b"1" and sub.startswith("S") and xy[0] == ".":
            subs[path] = ("is at another commit" if sub[1] == "C" else "has local edits"
                          if sub[2] == "M" else "has untracked files")
        else:
            dirty.append(path.decode("utf-8", "replace"))
    return dirty, subs


def _listed(paths: list[str], most: int = 5) -> str:
    return ", ".join(sorted(paths)[:most]) + (f" and {len(paths) - most} more"
                                               if len(paths) > most else "")


def _written(repo: Path, branch: str) -> bytes:
    """The paths merging ``branch`` would leave different from HEAD (NUL-separated)."""
    written = _merge_tree(repo, branch)
    return _trial_merge(repo, branch) if written is None else written


def merge_message(repo: Path, task: str, tip: str) -> str:
    """The merge commit's message git itself writes for ``delegate/<task>`` (`fmt-merge-msg`:
    "Merge branch 'delegate/<task>'", "into <branch>" unless merge.suppressDest drops it, the
    shortlog under merge.log, so the merge itself takes --no-log), though what is merged is
    the reviewed ``tip`` (#471)."""
    b, sha = branch_name(task), git(repo, "rev-parse", "--verify", f"{tip}^{{commit}}").strip()
    r = _run(repo, ["fmt-merge-msg"], check=False, input=f"{sha}\t\tbranch '{b}' of .\n".encode())
    said = r.stdout.decode("utf-8", "replace").strip() if r.returncode == 0 else ""
    return said or f"Merge branch '{b}'"


def squashed(repo: Path, into: str, before: str | None, tip: str,
             message: str | None = None) -> str | None:
    """The squash of ``tip`` an earlier accept made on ``into``, which stood at ``before`` then:
    a commit of the branch's first-parent line whose parent is ``before`` and whose message is
    the tip's (commit -C), or ``message`` when merge was given one (#1393). A squash never holds
    the tip, so a retry interrupted between it and its record squashed again and recorded it
    refused (#432, the audit of 1.8)."""
    if not before:
        return None
    want = message.strip() if message else git(repo, "log", "-1", "--format=%B", tip, check=False).strip()
    log = git(repo, "log", "--first-parent", "--format=%H %P%x00%B%x01",
              f"{before}..refs/heads/{into}", check=False)
    for entry in log.split("\x01"):
        head, _, body = entry.lstrip("\n").partition("\x00")
        sha, *parents = head.split() or [""]
        if sha and parents[:1] == [before] and want and body.strip() == want:
            return git(repo, "rev-parse", "--short", sha).strip()
    return None


def _back_out(repo: Path, before: str, source: str | None = None, how: str = "merge") -> None:
    """Undo wb's own merge of ``source`` in the user's checkout, which was clean at ``before``: a
    conflict, a signature that timed out, a kill mid-merge. `reset --merge` drops MERGE_HEAD and
    the staged merge (a squash leaves no MERGE_HEAD), and a commit made just before an
    interruption. Only what this merge did: a MERGE_HEAD of another merge is left, and a HEAD
    that moved past ``before`` by anything but this merge's own commit is reset to, not from.
    The rollback reset to the HEAD it had read, rewinding another accept's merge that landed in
    between (#639, Codex's audit of 1.90.6)."""
    tip = git(repo, "rev-parse", "-q", "--verify", f"{source}^{{commit}}",
              check=False).strip() if source else ""
    if _merging(repo):
        theirs = git(repo, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False).strip()
        if tip and theirs != tip:
            return  # someone else's merge under way: not ours to abort
        git(repo, "merge", "--abort", check=False)
    head = git(repo, "rev-parse", "HEAD", check=False).strip()
    to = before if head == before or not tip or _made_by(repo, head, before, tip, how) else head
    if head != to or git(repo, "status", "--porcelain", "--untracked-files=no", check=False).strip():
        git(repo, "reset", "-q", "--merge", to, check=False)


def clear_killed_merge(repo: Path, before: str, tip: str, how: str = "merge",
                       merge_git: list | None = None) -> None:
    """What an earlier accept's merge of ``tip`` onto ``before`` left in the user's checkout when
    it was SIGKILLed, which no handler sees, so merge()'s backout never ran: MERGE_HEAD at the
    tip, or a staged squash whose SQUASH_MSG names it. A merge whose commit was made (HEAD holds
    the tip: git committed, then died before removing MERGE_HEAD) is quit, the rest backed out
    for the retry to merge again. Killed while the signer waited, the retry called wb's own
    staged merge "uncommitted changes" and closed the task unmerged; with the commit made, it
    said merged with the checkout still mid-merge, and the user's next commit became a merge
    (#1231, bar round 4). Anything else, a merge of another tip, is left alone. First the killed
    try's own git, ``merge_git`` ([pid, start], merge()'s ``started``), detached so it outlived
    wb, is ended if it still waits on its signer: backed out under it, the merge it landed later
    left the index and files unmerged (#1246). Only then is what it left read."""
    if merge_git:
        end_session(*merge_git)
    if how == "merge" and _merging(repo):
        if git(repo, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False).strip() != tip:
            return
        if contains(repo, "HEAD", tip):
            git(repo, "merge", "--quit", check=False)
        else:
            _back_out(repo, before, tip, how)
    elif how == "squash" and git(repo, "rev-parse", "HEAD", check=False).strip() == before:
        msg = Path(repo, git(repo, "rev-parse", "--git-path", "SQUASH_MSG", check=False).strip())
        with contextlib.suppress(OSError):
            if f"\ncommit {tip}\n" in msg.read_text(errors="replace"):
                _back_out(repo, before, tip, how)


def _made_by(repo: Path, head: str, before: str, tip: str, how: str) -> bool:
    """Whether ``head`` is the commit merging ``tip`` onto ``before`` made: the tip itself
    (a fast-forward), else a child of ``before`` whose second parent is the tip (a merge) or
    whose message is the tip's (a squash, commit -C)."""
    if how == "ff":
        return head == tip
    parents = git(repo, "rev-list", "--parents", "-n", "1", head, check=False).split()[1:]
    if parents[:1] != [before]:
        return False
    if how == "squash":
        msg = lambda c: git(repo, "log", "-1", "--format=%B", c, check=False).strip()  # noqa
        return len(parents) == 1 and msg(head) == msg(tip)
    return parents[1:2] == [tip]


def _overwritten(repo: Path, branch: str) -> list[str]:
    """The files the merge would write that are already in the checkout untracked: git's merge
    silently replaces an ignored one, so a task that un-ignored and committed settings.ini
    overwrote the user's own copy (#189, the 2026-11-19 audit). Its --no-overwrite-ignore doesn't
    stop it (git 2.56's ort merge ignores it), so the paths are checked first, and a parent that
    is a file counts too. The paths come from the merge itself, done in memory (merge-tree): the
    branch's added paths missed a modify/delete conflict, whose abort deleted the personal copy, a
    directory rename that moved an added file onto one, and a non-UTF-8 name read with U+FFFD
    (#241, the audit of 1.2). Raw bytes throughout. Under core.ignorecase (macOS, Windows) a
    path matching a tracked one but for case is that tracked file when the file system says so
    (the same inode): a case-only rename (Readme.md to README.md) was refused as "would
    overwrite README.md" on APFS (#470, the audit of 1.8), and the setting alone was trusted: a
    copied repo carried core.ignorecase=true onto a case-sensitive file system, where an ignored
    personal `config` beside the tracked `CONFIG` was replaced by the merge (#640, Codex's audit
    of 1.90.6)."""
    written = _merge_tree(repo, branch)
    if written is None:  # git before 2.38
        written = _trial_merge(repo, branch)
    tracked = set(_run(repo, ("ls-tree", "-r", "--name-only", "-z", "HEAD")).stdout.split(b"\0"))
    ignorecase = git(repo, "config", "--bool", "core.ignorecase", check=False).strip() == "true"
    folded: dict[str, list[bytes]] = {}
    for t in tracked if ignorecase else ():
        folded.setdefault(_folded(t), []).append(t)
    top = os.fsencode(repo)
    out = []
    for p in (p for p in written.split(b"\0") if p and p not in tracked):
        parts = p.split(b"/")
        for i in range(1, len(parts) + 1):
            here = os.path.join(top, *parts[:i])
            if (b"/".join(parts[:i]) in tracked or not os.path.lexists(here)
                    or folded and _same_file(here, top, folded.get(_folded(b"/".join(parts[:i]))))):
                # a tracked file where the merge puts a directory is the merge's to replace: a
                # clean file-to-directory refactor (pkg to pkg/init.py) was refused (#343, the
                # audit of 1.4)
                continue
            if i < len(parts) and os.path.islink(here):
                # an untracked link the merge writes under: git won't write through it and
                # replaced the user's ignored link to a dataset with a plain directory (#367, the
                # audit of 1.6). os.path.isdir follows links, so it read as a directory
                out.append(b"/".join(parts[:i]).decode("utf-8", "replace"))
                break
            if i < len(parts) and os.path.isdir(here):
                continue
            if i == len(parts) and os.path.isdir(here) and not os.path.islink(here):
                # a tracked directory the merge turns into a file is in the way only through
                # what it holds untracked (#343)
                inside = _untracked_under(here, top, tracked)
                out.extend(inside)
                if not inside:
                    continue
                break
            out.append(p.decode("utf-8", "replace"))
            break
    return out


def _same_file(here: bytes, top: bytes, twins: list[bytes] | None) -> bool:
    """Whether ``here`` is one of the tracked ``twins`` (paths equal to it but for case) on
    disk: the same file system object, so a case-insensitive file system's name for it (#640)."""
    try:
        st = os.lstat(here)
    except OSError:
        return False
    for t in twins or ():
        try:
            if os.path.samestat(st, os.lstat(os.path.join(top, t))):
                return True
        except OSError:
            continue
    return False


def _folded(path: bytes) -> str:
    """A path as a case-insensitive file system compares it (APFS folds case and Unicode
    normalization alike): for core.ignorecase (#470)."""
    return unicodedata.normalize("NFC", os.fsdecode(path)).casefold()


def _untracked_under(path: bytes, top: bytes, tracked: set[bytes]) -> list[str]:
    """What a directory holds that HEAD doesn't track, ignored or not (empty directories
    aside): the files a merge replacing the directory with a file would delete (#343)."""
    found = []
    for root, dirs, files in os.walk(path):
        for name in files + [d for d in dirs if os.path.islink(os.path.join(root, d))]:
            rel = os.path.relpath(os.path.join(root, name), top)
            if rel not in tracked:
                found.append(rel.decode("utf-8", "replace"))
    return found


def _merge_tree(repo: Path, branch: str) -> bytes | None:
    """The paths the merge would leave different from HEAD, from the merge done in memory
    (`merge-tree --write-tree`, git 2.38+); None on an older git."""
    r = _run(repo, ("merge-tree", "--write-tree", "-z", "HEAD", branch), check=False)
    if r.returncode not in (0, 1):  # 1: it conflicts, and the tree holds what the merge would leave
        return None
    tree = r.stdout.split(b"\0", 1)[0].strip()
    return _run(repo, ("diff", "--name-only", "--no-renames", "-z", "HEAD", tree)).stdout


def _trial_merge(repo: Path, branch: str) -> bytes:
    """The paths the merge would write, from the same merge done in a scratch worktree of HEAD:
    git before 2.38 has no merge-tree --write-tree, and the paths the branch adds, its fallback,
    missed a modify/delete conflict whose abort deleted the developer's ignored copy on Git
    2.34.1, Ubuntu 22.04's (#320, the audit of 1.4). A trial that can't run refuses the merge.
    The worktree is made without a checkout and its index read from HEAD, so the merge writes
    only the files it changes, and LFS's smudge is skipped: a full checkout of HEAD took
    gigabytes and minutes on a big repo, ignoring its sparse patterns, and failed when LFS
    objects weren't local (#469, the audit of 1.8). A file missing from the worktree is no local
    change to git's merge, and the paths are read from the index (diff --cached)."""
    store.home().mkdir(parents=True, exist_ok=True)
    scratch = tempfile.mkdtemp(prefix=store.scratch("wb-merge-"), dir=store.home())
    wt = os.path.join(scratch, "w")
    try:
        _run(repo, (*NO_HOOKS, "worktree", "add", "-q", "--no-checkout", "--detach", wt, "HEAD"))
        _run(wt, ("read-tree", "HEAD"))
        _run(wt, (*NO_HOOKS, "-c", "rerere.enabled=false", "merge", "--no-commit", "--no-ff",
                  "--no-edit", branch), NO_SMUDGE, check=False)  # a conflict says what it touched
        changed = _run(wt, ("diff", "--cached", "--name-only", "--no-renames", "-z", "HEAD")).stdout
        unmerged = [row.split(b"\t", 1)[1] for row in
                    _run(wt, ("ls-files", "-u", "-z")).stdout.split(b"\0") if b"\t" in row]
        return b"\0".join([*changed.split(b"\0"), *unmerged])
    except GitError as e:
        raise GitError(f"couldn't check what merging {branch} would write ({e}); merge it by "
                       "hand") from None
    finally:
        _run(repo, ("worktree", "remove", "--force", wt), check=False)
        shutil.rmtree(scratch, ignore_errors=True)
        drop_registration(repo, Path(wt))  # its own only (#641)


def contains(repo: Path, ref: str, commit: str) -> bool:
    """Whether ``ref`` holds ``commit`` (its ancestry): a merge already made, whatever the branch
    is called now."""
    return _run(repo, ("merge-base", "--is-ancestor", commit, ref), check=False).returncode == 0


def _merging(repo: Path) -> bool:
    """Whether a merge is under way in ``repo`` (MERGE_HEAD is there)."""
    return _run(repo, ("rev-parse", "-q", "--verify", "MERGE_HEAD"), check=False).returncode == 0


# What git leaves in the git dir while an operation waits for the user's commit, and its name
UNDER_WAY = (("MERGE_HEAD", "a merge"), ("CHERRY_PICK_HEAD", "a cherry-pick"),
             ("REVERT_HEAD", "a revert"), ("rebase-merge", "a rebase"), ("rebase-apply", "a rebase"),
             ("sequencer", "a cherry-pick or revert"))


def under_way(repo: Path) -> str | None:
    """The git operation waiting in ``repo`` for the user to finish it (UNDER_WAY), or None. A
    commit made then is theirs to shape: during a merge, wb init's link commit took MERGE_HEAD as
    its second parent with only HEAD's tree, so the merge's changes were lost and MERGE_HEAD
    consumed (the review of #931)."""
    paths = git(repo, "rev-parse", *(a for p, _ in UNDER_WAY for a in ("--git-path", p))).splitlines()
    return next((what for (_, what), p in zip(UNDER_WAY, paths) if (Path(repo) / p).exists()), None)


def ignored(repo: Path, path: str) -> bool:
    """Whether `path` is ignored in the checkout (.gitignore, info/exclude, the global excludes)."""
    return _run(repo, ("check-ignore", "-q", "--", path), check=False).returncode == 0
