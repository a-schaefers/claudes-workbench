"""Git for a task: a worktree on its own branch at the start, a diff for review, a commit at the end.

The agent never commits; everything it does stays in the worktree's working tree until `wb accept`.
"""
import hashlib
import os
import shutil
import stat
import subprocess
import tempfile
from collections import Counter
from pathlib import Path

from . import store


class GitError(RuntimeError):
    pass


# The user's settings that would change what we read: found in the 2026-11-16 audit, a
# `diff.external = difft` made the review diff difftastic's screen, color.ui=always put escape
# codes in the changed-files list, and core.quotePath escaped every non-ASCII file name.
# (color.diff/status/branch=always outrank color.ui, so each is named.) log.showSignature put
# "No signature" ahead of the hash a `git log --format=%h` was read for, and a finished wb accept
# died on it (the audit).
PLAIN = ("-c", "color.ui=never", "-c", "color.diff=never", "-c", "color.status=never",
         "-c", "color.branch=never", "-c", "core.quotePath=false", "-c", "log.showSignature=false")


# No git hook runs from workbench in a task's worktree: with a relative core.hooksPath (husky's
# `.husky/_`, gitignored, so in no diff) the hooks live in the worktree, written by the agent, and
# wb accept ran them as the user outside the lane (the 2026-11-16 audit). The checks belong in
# --test, which runs in the lane. Signing is kept: only the hooks go.
NO_HOOKS = ("-c", "core.hooksPath=/dev/null")


def _record(worktree) -> Path | None:
    """Where a task worktree's real git dir is kept: rules/<task>/gitdir.json (context.rules_dir),
    outside the agent's lane. None for any other directory."""
    p = Path(os.path.abspath(worktree))  # not resolved: a worktree swapped for a link still pins
    tasks = Path(os.path.abspath(store.tasks_dir()))
    if p.name == "worktree" and p.parent.parent == tasks:
        return tasks.parent / "rules" / p.parent.name / "gitdir.json"
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
    if not same:
        raise GitError(f"the worktree's .git was rewritten: git was not run there ({dotgit} "
                       f"must say gitdir: {want['gitdir']})")
    return [f"--git-dir={want['gitdir']}", f"--work-tree={os.path.abspath(cwd)}",
            *NO_HOOKS, "-c", "core.fsmonitor=false"]


def _run(cwd, args, env=None, check=True) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", "-C", str(cwd), *PLAIN, *_pinned(cwd), *args], capture_output=True,
                       env={**os.environ, **env} if env else None)
    if check and r.returncode:
        err = (r.stderr or r.stdout).decode("utf-8", "replace").strip()
        raise GitError(f"git {' '.join(args)}: {err}")
    return r


def git(cwd, *args, check=True, env=None) -> str:
    """git's output as text. Bytes that aren't UTF-8 (a Latin-1 source file in a diff) read as
    U+FFFD: strict decoding crashed the runner on them (the 2026-11-16 audit), and the text goes
    to people and prompts, where a surrogate escape would raise when printed."""
    return _run(cwd, args, env, check).stdout.decode("utf-8", "replace")


def git_bytes(cwd, *args) -> bytes:
    """git's output exactly, for fingerprints: two different non-UTF-8 bytes must not both
    read as U+FFFD."""
    return _run(cwd, args).stdout


def toplevel(path) -> Path:
    return Path(git(path, "rev-parse", "--show-toplevel").strip())


def branch_name(task: str) -> str:
    return f"delegate/{task}"


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
    if git(repo, "branch", "--list", branch_name(task)).strip():
        raise GitError(f"branch {branch_name(task)} already exists in {repo}")
    try:
        git(repo, *NO_HOOKS, "worktree", "add", "-q", "-b", branch_name(task), str(dest), commit)
    except GitError:
        git(repo, *NO_HOOKS, "worktree", "remove", "--force", str(dest), check=False)
        git(repo, "worktree", "prune", check=False)
        delete_branch(repo, task)  # this call made it: the --list above found none
        raise
    if rec := _record(dest):  # before any agent has run there: see _pinned
        rec.parent.mkdir(parents=True, exist_ok=True)
        store.write_json(rec, {"gitdir": git(dest, "rev-parse", "--absolute-git-dir").strip(),
                               "dotgit": (Path(dest) / ".git").read_text()})
    return commit


def submodules(repo: Path, commit: str) -> list[str]:
    """The submodule paths ``commit`` lists. They stay empty in a task's worktree: each one's
    `.git` would be a file the agent could point at a git dir of its own, whose config git then
    obeys outside the lane (see _pinned), so wb start says so instead (the 2026-11-16 audit)."""
    out = git(repo, "config", "--blob", f"{commit}:.gitmodules", "--get-regexp",
              r"^submodule\..*\.path$", check=False)
    return [line.split(" ", 1)[1] for line in out.splitlines() if " " in line]


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


def changed(worktree: Path) -> str:
    if not Path(worktree).is_dir():  # removed under us (an accept, a discard)
        return ""
    # porcelain: the `XY path` lines whatever the user's status.branch or colours say (a
    # status.branch=true put "## delegate/<task>" in every round's changed files: the audit)
    return git(worktree, "status", "--porcelain", "--untracked-files=all").rstrip()


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


def mark(worktree: Path) -> str:
    """A fingerprint of the working tree's uncommitted state, new files' contents included: it
    changes when, and only when, the agent changes a file (a new file past MARK_READ_MAX counts
    by its size and time, a symlink by its target). The index is left alone. Raw bytes: a decoded
    diff would hide an edit from one non-UTF-8 byte to another."""
    h = hashlib.sha1(git_bytes(worktree, "diff", "--no-ext-diff", "--no-color", "HEAD"))
    for f in git_bytes(worktree, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0"):
        if f:
            h.update(f + b"\0" + _untracked(Path(worktree) / os.fsdecode(f)))
    return h.hexdigest()


def with_new_files(worktree: Path, *args) -> str:
    """`git <args>` with new files counted as changes (intent-to-add), in a scratch index: the
    worktree's own index, and anything a concurrent git command holds, are left alone. The
    scratch index starts as a copy of the worktree's, so sparse-checkout's skip-worktree entries
    aren't read as deletions (read-tree listed a monorepo's every file outside the cone as
    deleted: the 2026-11-16 audit). Nested repositories are left out (nested_repos)."""
    # in a private directory of the state dir, not the temp dir: /tmp is in the agent's lane, and
    # an agent process still running could rewrite the index between `add -N` and the diff, so
    # files dropped out of the diff Claude reviews (the 2026-11-16 audit)
    from . import store
    store.home().mkdir(parents=True, exist_ok=True)
    scratch = tempfile.mkdtemp(prefix="wb-index-", dir=store.home())
    index = os.path.join(scratch, "index")
    env = {"GIT_INDEX_FILE": index}
    try:
        try:
            real = git(worktree, "rev-parse", "--git-path", "index").strip()
            shutil.copyfile(Path(worktree) / real, index)  # a relative path is the worktree's
        except (GitError, OSError):
            git(worktree, "read-tree", "HEAD", env=env)
        skip = [f":(exclude){p}" for p in nested_repos(worktree)]
        git(worktree, "add", "-A", "-N", *(["--", ".", *skip] if skip else []), env=env)
        return git(worktree, *args, env=env)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def diff(worktree: Path, base: str, stat=False, paths=(), numstat=False) -> str:
    args = (["diff", "--no-ext-diff", "--no-color", base] + (["--stat"] if stat else []) + (["--numstat"] if numstat else [])
            + (["--", *paths] if paths else []))
    return with_new_files(worktree, *args).rstrip()


def footprint(worktree: Path, base: str) -> dict:
    """What a change spans since ``base``: files touched and lines added, new files included."""
    rows = [r.split("\t") for r in diff(worktree, base, numstat=True).splitlines() if r]
    return {"files": len(rows), "lines": sum(int(r[0]) for r in rows if r[0].isdigit())}


# Source files by extension, for the kind of work a repo holds (stack). Markup, data and docs
# say nothing about it and are left out.
LANGUAGES = {
    "py": "python", "js": "javascript", "mjs": "javascript", "cjs": "javascript", "jsx": "javascript",
    "ts": "typescript", "tsx": "typescript", "c": "c", "h": "c", "cc": "c++", "cpp": "c++",
    "cxx": "c++", "hpp": "c++", "hh": "c++", "rs": "rust", "go": "go", "java": "java", "kt": "kotlin",
    "swift": "swift", "m": "objective-c", "cs": "c#", "rb": "ruby", "php": "php", "lua": "lua",
    "zig": "zig", "dart": "dart", "scala": "scala", "ex": "elixir", "exs": "elixir", "hs": "haskell",
    "ml": "ocaml", "sh": "shell", "bash": "shell", "gd": "gdscript", "glsl": "glsl", "sql": "sql",
}
STACK_SHARE = 0.2  # a language holding at least this share of the source files is part of it


def stack(repo) -> list[str]:
    """The repo's main languages, most files first (at most two), from its tracked files; empty
    when it has no source or isn't a repository."""
    try:
        names = git(repo, "ls-files", "-z").split("\0")
    except (GitError, OSError):
        return []
    langs = Counter(LANGUAGES[x] for n in names if (x := Path(n).suffix[1:].lower()) in LANGUAGES)
    total = sum(langs.values())
    return [lang for lang, n in langs.most_common(2) if n >= STACK_SHARE * total]


def commit_all(worktree: Path, message: str) -> str:
    """Commit everything in the worktree, without git hooks (NO_HOOKS; also for a task started
    before its git dir was recorded) but signed if the user signs. Refuses a nested repository: committed, it was a gitlink to
    a commit that exists nowhere else."""
    if nested := nested_repos(worktree):
        raise GitError(f"{', '.join(nested)}: a git repository of its own, so not committed: "
                       "remove its .git or gitignore it, then try again")
    git(worktree, *NO_HOOKS, "add", "-A")
    if not git(worktree, "diff", "--cached", "--name-only").strip():
        raise GitError("nothing to commit: the agent's worktree has no changes")
    git(worktree, *NO_HOOKS, "commit", "-q", "-m", message)
    return git(worktree, "rev-parse", "--short", "HEAD").strip()


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
    Always --force: after accept's commit only ignored files are left, which git deletes anyway,
    and without it git runs `status` in the worktree. When git fails (a read-only directory, an
    embedded repo, a repo moved or deleted), a task's own worktree is deleted by hand: accept and
    discard once wedged there for good (the 2026-11-16 audit)."""
    worktree, warn = Path(worktree), ""
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
        git(repo, "worktree", "prune", check=False)
    return warn


def delete_branch(repo: Path, task: str) -> None:
    git(repo, "branch", "-D", branch_name(task), check=False)


def current_branch(repo: Path) -> str:
    """The checked-out branch, "" for a detached HEAD (any git: `branch --show-current` is 2.22+)."""
    return git(repo, "symbolic-ref", "--short", "-q", "HEAD", check=False).strip()


def start_branch(repo: Path, base: str) -> str | None:
    """The branch a task starts from: the one checked out for HEAD, else ``base`` when it names a
    local branch; None for a detached HEAD or a commit."""
    if base == "HEAD":
        return current_branch(repo) or None
    return base if _run(repo, ("rev-parse", "--verify", "-q", f"refs/heads/{base}"),
                        check=False).returncode == 0 else None


def merge(repo: Path, task: str, into: str | None = None, base: str | None = None) -> str:
    """Merge the task's branch into ``into`` (the branch it started from: task.json's
    base_branch), which the main checkout must have out and clean; a conflict is backed out,
    leaving the repo as it was. With no branch recorded, the checkout's branch must contain
    ``base``, the task's base commit. Never onto a detached HEAD, where the merge is lost at the
    next checkout, nor onto another branch than the task's (both happened: the 2026-11-16 audit)."""
    if git(repo, "status", "--porcelain", "--untracked-files=no").strip():
        raise GitError(f"{repo} has uncommitted changes; merge {branch_name(task)} by hand")
    have = current_branch(repo)
    if not have:
        raise GitError(f"{repo} has a detached HEAD out; merge {branch_name(task)} by hand")
    if into and into != "HEAD" and have != into:
        raise GitError(f"{repo} has {have} out, not {into}; merge {branch_name(task)} by hand")
    if (not into or into == "HEAD") and base and _run(
            repo, ("merge-base", "--is-ancestor", base, "HEAD"), check=False).returncode:
        raise GitError(f"{repo} has {have} out, which doesn't hold the task's base; "
                       f"merge {branch_name(task)} by hand")
    try:
        git(repo, "merge", "--no-ff", "--no-edit", branch_name(task))
    except GitError as e:
        git(repo, "merge", "--abort", check=False)
        raise GitError(f"merging {branch_name(task)} failed and was backed out: {e}") from None
    return git(repo, "rev-parse", "--short", "HEAD").strip()
