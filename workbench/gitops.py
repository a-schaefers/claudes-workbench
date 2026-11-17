"""Git for a task: a worktree on its own branch at the start, a diff for review, a commit at the end.

The agent never commits; everything it does stays in the worktree's working tree until `wb accept`.
"""
import hashlib
import os
import subprocess
import tempfile
from collections import Counter
from pathlib import Path


class GitError(RuntimeError):
    pass


def git(cwd, *args, check=True, env=None) -> str:
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                       env={**os.environ, **env} if env else None)
    if check and r.returncode:
        raise GitError(f"git {' '.join(args)}: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout


def toplevel(path) -> Path:
    return Path(git(path, "rev-parse", "--show-toplevel").strip())


def branch_name(task: str) -> str:
    return f"delegate/{task}"


def create_worktree(repo: Path, task: str, dest: Path, base: str = "HEAD") -> str:
    """New branch delegate/<task> at ``base`` checked out at ``dest``; returns the base commit."""
    commit = git(repo, "rev-parse", base).strip()
    if git(repo, "branch", "--list", branch_name(task)).strip():
        raise GitError(f"branch {branch_name(task)} already exists in {repo}")
    git(repo, "worktree", "add", "-q", "-b", branch_name(task), str(dest), commit)
    return commit


def changed(worktree: Path) -> str:
    if not Path(worktree).is_dir():  # removed under us (an accept, a discard)
        return ""
    return git(worktree, "status", "--short", "--untracked-files=all").rstrip()


def mark(worktree: Path) -> str:
    """A fingerprint of the working tree's uncommitted state, new files' contents included: it
    changes when, and only when, the agent changes a file. The index is left alone."""
    h = hashlib.sha1(git(worktree, "diff", "HEAD").encode())
    for f in git(worktree, "ls-files", "--others", "--exclude-standard", "-z").split("\0"):
        if f:
            h.update(f.encode() + b"\0")
            try:
                h.update((Path(worktree) / f).read_bytes())
            except OSError:
                pass
    return h.hexdigest()


def with_new_files(worktree: Path, *args) -> str:
    """`git <args>` with new files counted as changes (intent-to-add), in a scratch index: the
    worktree's own index, and anything a concurrent git command holds, are left alone."""
    fd, index = tempfile.mkstemp(prefix="wb-index-")
    os.close(fd)
    os.unlink(index)  # git wants to create it
    env = {"GIT_INDEX_FILE": index}
    try:
        git(worktree, "read-tree", "HEAD", env=env)
        git(worktree, "add", "-A", "-N", env=env)
        return git(worktree, *args, env=env)
    finally:
        for p in (index, index + ".lock"):
            try:
                os.unlink(p)
            except FileNotFoundError:
                pass


def diff(worktree: Path, base: str, stat=False, paths=(), numstat=False) -> str:
    args = (["diff", base] + (["--stat"] if stat else []) + (["--numstat"] if numstat else [])
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
        names = git(repo, "ls-files").splitlines()
    except (GitError, OSError):
        return []
    langs = Counter(LANGUAGES[x] for n in names if (x := Path(n).suffix[1:].lower()) in LANGUAGES)
    total = sum(langs.values())
    return [lang for lang, n in langs.most_common(2) if n >= STACK_SHARE * total]


def commit_all(worktree: Path, message: str) -> str:
    git(worktree, "add", "-A")
    if not git(worktree, "diff", "--cached", "--name-only").strip():
        raise GitError("nothing to commit: the agent's worktree has no changes")
    git(worktree, "commit", "-q", "-m", message)
    return git(worktree, "rev-parse", "--short", "HEAD").strip()


def remove_worktree(repo: Path, worktree: Path, force=False) -> None:
    if worktree.exists():
        git(repo, "worktree", "remove", *(["--force"] if force else []), str(worktree))
    git(repo, "worktree", "prune", check=False)


def delete_branch(repo: Path, task: str) -> None:
    git(repo, "branch", "-D", branch_name(task), check=False)


def merge(repo: Path, task: str, into: str | None = None) -> str:
    """Merge the task's branch into ``into`` (the branch it started from), which the main
    checkout must have out and clean; a conflict is backed out, leaving the repo as it was."""
    if git(repo, "status", "--porcelain", "--untracked-files=no").strip():
        raise GitError(f"{repo} has uncommitted changes; merge {branch_name(task)} by hand")
    have = git(repo, "branch", "--show-current").strip()
    if into and into != "HEAD" and have != into:
        raise GitError(f"{repo} has {have or 'a detached HEAD'} out, not {into}; "
                       f"merge {branch_name(task)} by hand")
    try:
        git(repo, "merge", "--no-ff", "--no-edit", branch_name(task))
    except GitError as e:
        git(repo, "merge", "--abort", check=False)
        raise GitError(f"merging {branch_name(task)} failed and was backed out: {e}") from None
    return git(repo, "rev-parse", "--short", "HEAD").strip()
