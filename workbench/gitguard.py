"""Git is read-only for the agent: Claude owns commits, branches, merges and pushes.

The runner puts a `git` of its own first on the agent's PATH. It lets every command that only
reads through, and refuses the rest in the task's repository and in any repository outside the
temp directory and the worktree, so the agent can't commit, reset, stash, check out or clean away
its own work, or touch another project. Scratch repositories under the temp directory or nested
in the worktree, which test suites make, stay writable. The shell rules in context.py stop the
agent naming the real git by its path. It guards against mistakes, not against a model set on getting round it.

Only the standard library here: the shim imports this module on every git call.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

# subcommands that never write to the repository
READ = {
    "annotate", "blame", "cat-file", "check-attr", "check-ignore", "check-ref-format", "cherry",
    "count-objects", "describe", "diff", "diff-files", "diff-index", "diff-tree", "for-each-ref",
    "grep", "help", "log", "ls-files", "ls-remote", "ls-tree", "merge-base", "name-rev",
    "range-diff", "rev-list", "rev-parse", "shortlog", "show", "show-branch", "show-ref", "status",
    "var", "verify-commit", "verify-tag", "version", "whatchanged",
    # (#443, the audit of 1.8) pure readers too: an archive goes to stdout or a file, trailers to
    # stdout (or, --in-place, a worktree file the agent may edit anyway)
    "archive", "check-mailmap", "get-tar-commit-id", "interpret-trailers",
    "verify-pack", "show-index",
}
# global options that take the next argument as their value
VALUED = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--attr-source"}
BRANCH_WRITES = {"-d", "-D", "--delete", "-m", "-M", "--move", "-c", "-C", "--copy", "-f",
                 "--force", "-u", "--set-upstream-to", "--unset-upstream", "--edit-description",
                 "-t", "--track", "--no-track"}
BRANCH_LISTS = {"-l", "--list", "--show-current", "--contains", "--no-contains", "--merged",
                "--no-merged", "--points-at"}
# `tag` lists with any of these, a pattern or a commit after them: version scripts in Makefiles,
# setup.py and build.rs run `git tag --points-at HEAD`, refused until #443 (the audit of 1.8)
TAG_LISTS = {"-l", "--list", "--points-at", "--contains", "--no-contains", "--merged",
             "--no-merged"}
TAG_WRITES = {"-a", "-s", "-u", "-d", "-f", "-m", "-F", "--annotate", "--sign", "--delete",
              "--force", "--message", "--file"}
APPLY_REPORTS = {"--check", "--stat", "--numstat", "--summary"}
CONFIG_READS = {"--get", "--get-all", "--get-regexp", "--get-urlmatch", "--list", "-l", "get",
                "list"}
# init's and clone's options that take the next argument as their value
DEST_VALUED = {"-b", "--branch", "-o", "--origin", "--depth", "-c", "--config", "--reference",
               "--reference-if-able", "--separate-git-dir", "-u", "--upload-pack", "--template",
               "--shallow-since", "--shallow-exclude", "-j", "--jobs", "--filter", "--bundle-uri",
               "--server-option", "--initial-branch", "--object-format", "--ref-format"}
# config's options that take the next argument as their value: counted as a key or value, `git
# config -f .gitmodules submodule.x.url` read as a write and was refused (#794)
CONFIG_VALUED = {"-f", "--file", "--blob", "--type", "--default", "--comment", "--value"}
# config's scopes that name a file of their own, not the repository's config
CONFIG_FILES = {"--global", "--system", "-f", "--file", "--blob"}
CONFIG_WRITES = {"--add", "--unset", "--unset-all", "--replace-all", "--rename-section",
                 "--remove-section", "-e", "--edit", "set", "unset", "rename-section",
                 "remove-section", "edit"}


def split(args: list[str]) -> tuple[list[str], str | None, list[str]]:
    """(global options, subcommand or None, the subcommand's arguments)."""
    i = 0
    while i < len(args):
        a = args[i]
        if a in VALUED:
            i += 2
        elif a.startswith("-"):
            i += 1
        else:
            return args[:i], a, args[i + 1:]
    return args, None, []


def reads(sub: str | None, rest: list[str]) -> bool:
    """Whether `git <sub> <rest>` only reads."""
    if sub is None or sub in READ:
        return True
    flags = {a.split("=", 1)[0] for a in rest if a.startswith("-")}
    plain = [a for a in rest if not a.startswith("-")]
    first = rest[0] if rest else None
    if sub == "branch":
        return not flags & BRANCH_WRITES and (not plain or bool(flags & BRANCH_LISTS))
    if sub == "tag":
        return not flags & TAG_WRITES and (not plain or bool(flags & TAG_LISTS))
    if sub == "config":
        plain = _config_plain(rest)
        words = flags | set(plain[:1])
        return not words & CONFIG_WRITES and (bool(words & CONFIG_READS) or len(plain) == 1)
    if sub == "stash":
        return first in ("list", "show")
    if sub == "worktree":
        return first == "list"
    if sub == "remote":
        words = [a for a in rest if a not in ("-v", "--verbose")]
        return (words[0] if words else None) in (None, "get-url", "show")
    if sub == "reflog":
        return first is None or first in ("show", "exists") or first.startswith("-")
    if sub in ("notes", "submodule"):
        return first in (None, "list", "show", "status")
    if sub == "apply":  # only reports on the patch, applying nothing
        return bool(flags & APPLY_REPORTS) and "--apply" not in flags
    if sub == "hash-object":  # prints the id; -w writes the object
        return "-w" not in flags
    if sub == "fsck":  # --lost-found writes into the git dir
        return "--lost-found" not in flags
    if sub == "symbolic-ref":
        return len(plain) == 1 and not flags & {"-d", "--delete", "-m"}
    return False


def _config_plain(rest: list[str]) -> list[str]:
    """config's arguments that aren't options or their values: its key, value, subcommand."""
    plain, skip = [], False
    for a in rest:
        if skip:
            skip = False
        elif a.startswith("-"):
            skip = a in CONFIG_VALUED
        else:
            plain.append(a)
    return plain


def config_files(rest: list[str], opts: list[str]) -> list[str] | None:
    """The files a `git config` write with --global, --system or -f writes (the ones it may),
    or None when it writes the repository's own config. Decided by the cwd's repository, a
    fixture's `git config --global user.name t` with HOME pointed at its temp directory, run
    from the worktree, was refused, and the base check and every round failed on it (#794, the
    second 2026-11-21 audit); the lane keeps the real ~/.gitconfig and /etc out of reach."""
    env, where = os.environ, _where(opts)
    for i, a in enumerate(rest):
        name, eq, value = a.partition("=")
        if name not in CONFIG_FILES:
            continue
        if name == "--blob":
            return []  # git writes no blob
        if name == "--global":
            if env.get("GIT_CONFIG_GLOBAL"):
                return [os.path.join(where, env["GIT_CONFIG_GLOBAL"])]
            home = env.get("HOME") or os.path.expanduser("~")
            xdg = env.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
            return [os.path.join(home, ".gitconfig"), os.path.join(xdg, "git", "config")]
        if name == "--system":
            return [env.get("GIT_CONFIG_SYSTEM") or "/etc/gitconfig"]
        value = value if eq else (rest[i + 1] if i + 1 < len(rest) else "")
        return [os.path.join(where, value)]
    return None


def _temp_roots(temps=()) -> list[str]:
    """The temp directories: fixed when the shim was written (`temps`), so an agent that sets
    TMPDIR can't make a real project count as scratch."""
    roots = {"/tmp", "/var/tmp", *temps} if temps else {tempfile.gettempdir(), "/tmp", "/var/tmp"}
    return [os.path.realpath(r) for r in roots]


def common_dir(real: str, opts: list[str]) -> str | None:
    """The repository a git command with these global options acts on (its common git dir)."""
    r = subprocess.run([real, *opts, "rev-parse", "--git-common-dir"], capture_output=True,
                       text=True, encoding="utf-8", errors="surrogateescape")  # a path, kept (#664)
    if r.returncode:
        return None  # not in a repository: `git init` or `git clone` somewhere new
    return os.path.realpath(os.path.join(_where(opts), r.stdout.strip()))


def _where(opts: list[str]) -> str:
    """The directory git acts from: the cwd, moved by each -C."""
    base = os.getcwd()
    for i, a in enumerate(opts):
        if a == "-C" and i + 1 < len(opts):
            base = os.path.join(base, opts[i + 1])
    return base


def new_repo(sub: str, rest: list[str], opts: list[str]) -> str:
    """Where `init` or `clone` puts the new repository (resolved, perhaps not there yet)."""
    plain, skip = [], False
    for a in rest:  # the arguments that aren't options or their values
        if skip:
            skip = False
        elif a.startswith("-"):
            skip = a in DEST_VALUED
        else:
            plain.append(a)
    if sub == "init":
        dest = plain[0] if plain else "."
    else:  # clone <url> [<dir>]
        dest = plain[1] if len(plain) > 1 else \
            os.path.basename(plain[0].rstrip("/")).removesuffix(".git") if plain else "."
    return os.path.realpath(os.path.join(_where(opts), dest))


def destination(sub: str, rest: list[str], opts: list[str]) -> list[str]:
    """For `init` and `clone`, global options aimed at the nearest existing directory of the new
    repository, which is what decides whether it lands inside a protected one."""
    path = new_repo(sub, rest, opts)
    while not os.path.isdir(path):
        path = os.path.dirname(path)
    return [a for i, a in enumerate(opts) if a != "-C" and (i == 0 or opts[i - 1] != "-C")] + \
        ["-C", path]


def _inside(path: str, roots) -> bool:
    return any(path == r or path.startswith(r + os.sep) for r in roots)


def nested(sub: str, rest: list[str], opts: list[str], lanes=()) -> bool:
    """Whether `init` or `clone` makes a repository of its own below the worktree (`lanes`), as a
    test suite does in `target/tmp` or `tests/tmp`: never the worktree itself, and never with its
    git dir sent elsewhere."""
    if any(a.split("=", 1)[0] == "--separate-git-dir" for a in rest):
        return False
    path = new_repo(sub, rest, opts)
    return any(path.startswith(os.path.realpath(r) + os.sep) for r in lanes)


def protected(common: str | None, task_git: str, temps=(), lanes=()) -> bool:
    """Whether a write to the repository with this common git dir is refused: the task's own
    always; else any outside the temp roots and the worktree (`lanes`), where a test suite's
    repositories are scratch (#444, the audit of 1.8: `git init target/tmp/x`, Rust's
    CARGO_TARGET_TMPDIR, and fixtures in tests/tmp were refused)."""
    if common is None:
        return False
    if common == os.path.realpath(task_git):
        return True
    return not (_inside(common, _temp_roots(temps))
                or any(common.startswith(os.path.realpath(r) + os.sep) for r in lanes))


REFUSAL = """\
workbench: git is read-only for you here, so `git {sub}` was not run.
Leave your changes uncommitted: Claude reviews the diff and commits it, and handles branches,
merges, pushes and deploys. Read freely: status, diff, log, show, blame, grep. To undo your own
edit to a file, edit it back or use `git show HEAD:<path>`. If the task needs a git write, say so
with ask_reviewer or in your summary.
"""


def main(real: str, task_git: str, args: list[str], temps=(), lanes=()) -> int:
    """``lanes``: the worktree and the lane's tool homes, where a repository other than the
    task's is writable (a test suite's own, a package manager's)."""
    opts, sub, rest = split(args)
    if reads(sub, rest):
        # no index refresh in the task's repo, which Claude may be diffing at the same moment
        os.execve(real, [real, *args], {**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
    if sub == "config" and (files := config_files(rest, opts)) is not None:
        # a file of its own: refused only where it is the task's repository's (#794)
        # or a `.git` link, whose rewrite would point the worktree at a git dir of its own
        if not any(_inside(os.path.realpath(f), [os.path.realpath(task_git)])
                   or os.path.basename(os.path.realpath(f)) == ".git" for f in files):
            os.execv(real, [real, *args])
        sys.stderr.write(REFUSAL.format(sub=sub))
        return 1
    making = sub in ("init", "clone")
    target = destination(sub, rest, opts) if making else opts
    if (making and nested(sub, rest, opts, lanes)) or \
            not protected(common_dir(real, target), task_git, temps, lanes):
        os.execv(real, [real, *args])
    sys.stderr.write(REFUSAL.format(sub=sub))
    return 1


def install(bin_dir: Path, task_repo: Path, pkg_root: Path, path: str,
            worktree: Path | None = None, scratch=()) -> dict | None:
    """Write the shim into bin_dir; returns the env the agent runs with, or None without git.
    ``worktree``: the task's, where repositories other than the task's are scratch; ``scratch``:
    the rest of the lane's writable places (the tool homes), where they are scratch too: a
    package manager's own git repositories there (pre-commit's `git remote add` in
    ~/.cache/pre-commit, uv's `git fetch` in ~/.cache/uv, Go's direct fetches in go/pkg) were
    refused, and `--setup 'uv sync'` failed before round 1 (#665). The agent writes those
    places directly anyway, and the lane leaves the task's repo out of them."""
    import shutil
    real = shutil.which("git", path=path)
    if not real:
        return None
    common = subprocess.run([real, "-C", str(task_repo), "rev-parse", "--git-common-dir"],
                            capture_output=True, text=True, encoding="utf-8",
                            errors="surrogateescape", check=True).stdout.strip()  # #664
    repo_dir = os.path.realpath(os.path.join(task_repo, common))
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "git"
    lanes = list(dict.fromkeys(os.path.realpath(r) for r in ([worktree] if worktree else [])
                               + list(scratch)))
    # a /bin/sh wrapper, not a `#!<python>` line: the kernel splits a shebang at its first space
    # and (before 5.1) cuts it at 127 bytes, so a Python under "/home/John Smith/" broke every
    # git call the agent made (the 2026-11-16 audit). -I: no user site, no PYTHONPATH.
    code = ("import sys\n"
            f"sys.path.insert(0, {str(pkg_root)!r})\n"
            "from workbench.gitguard import main\n"
            f"sys.exit(main({real!r}, {repo_dir!r}, sys.argv[1:], "
            f"{sorted({tempfile.gettempdir(), '/tmp', '/var/tmp'})!r}, "
            f"{lanes!r}))\n")
    shim.write_text("#!/bin/sh\n"
                    "# workbench: git is read-only for the agent (see workbench/gitguard.py)\n"
                    f"exec {shlex.quote(sys.executable)} -I -c {shlex.quote(code)} \"$@\"\n")
    shim.chmod(0o755)
    return {"PATH": f"{bin_dir}{os.pathsep}{path}"}
