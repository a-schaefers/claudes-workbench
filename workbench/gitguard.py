"""Git is read-only for the agent: Claude owns commits, branches, merges and pushes.

The runner puts a `git` of its own first on the agent's PATH. It lets every command that only
reads through, and refuses the rest in the task's repository and in any repository outside the
temp directory, so the agent can't commit, reset, stash, check out or clean away its own work, or
touch another project. Scratch repositories under the temp directory, which test suites make,
stay writable. The shell rules in context.py stop the agent naming the real git by its path.
It guards against mistakes, not against a model set on getting round it.

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


def _temp_roots(temps=()) -> list[str]:
    """The temp directories: fixed when the shim was written (`temps`), so an agent that sets
    TMPDIR can't make a real project count as scratch."""
    roots = {"/tmp", "/var/tmp", *temps} if temps else {tempfile.gettempdir(), "/tmp", "/var/tmp"}
    return [os.path.realpath(r) for r in roots]


def common_dir(real: str, opts: list[str]) -> str | None:
    """The repository a git command with these global options acts on (its common git dir)."""
    r = subprocess.run([real, *opts, "rev-parse", "--git-common-dir"], capture_output=True,
                       text=True)
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


def destination(sub: str, rest: list[str], opts: list[str]) -> list[str]:
    """For `init` and `clone`, global options aimed at the nearest existing directory of the new
    repository, which is what decides whether it lands inside a protected one."""
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
    path = os.path.realpath(os.path.join(_where(opts), dest))
    while not os.path.isdir(path):
        path = os.path.dirname(path)
    return [a for i, a in enumerate(opts) if a != "-C" and (i == 0 or opts[i - 1] != "-C")] + \
        ["-C", path]


def protected(common: str | None, task_git: str, temps=()) -> bool:
    if common is None:
        return False
    if common == os.path.realpath(task_git):
        return True
    return not any(common == t or common.startswith(t + os.sep) for t in _temp_roots(temps))


REFUSAL = """\
workbench: git is read-only for you here, so `git {sub}` was not run.
Leave your changes uncommitted: Claude reviews the diff and commits it, and handles branches,
merges, pushes and deploys. Read freely: status, diff, log, show, blame, grep. To undo your own
edit to a file, edit it back or use `git show HEAD:<path>`. If the task needs a git write, say so
with ask_reviewer or in your summary.
"""


def main(real: str, task_git: str, args: list[str], temps=()) -> int:
    opts, sub, rest = split(args)
    if reads(sub, rest):
        # no index refresh in the task's repo, which Claude may be diffing at the same moment
        os.execve(real, [real, *args], {**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
    target = destination(sub, rest, opts) if sub in ("init", "clone") else opts
    if not protected(common_dir(real, target), task_git, temps):
        os.execv(real, [real, *args])
    sys.stderr.write(REFUSAL.format(sub=sub))
    return 1


def install(bin_dir: Path, task_repo: Path, pkg_root: Path, path: str) -> dict | None:
    """Write the shim into bin_dir; returns the env the agent runs with, or None without git."""
    import shutil
    real = shutil.which("git", path=path)
    if not real:
        return None
    common = subprocess.run([real, "-C", str(task_repo), "rev-parse", "--git-common-dir"],
                            capture_output=True, text=True, check=True).stdout.strip()
    repo_dir = os.path.realpath(os.path.join(task_repo, common))
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "git"
    # a /bin/sh wrapper, not a `#!<python>` line: the kernel splits a shebang at its first space
    # and (before 5.1) cuts it at 127 bytes, so a Python under "/home/John Smith/" broke every
    # git call the agent made (the 2026-11-16 audit). -I: no user site, no PYTHONPATH.
    code = ("import sys\n"
            f"sys.path.insert(0, {str(pkg_root)!r})\n"
            "from workbench.gitguard import main\n"
            f"sys.exit(main({real!r}, {repo_dir!r}, sys.argv[1:], "
            f"{sorted({tempfile.gettempdir(), '/tmp', '/var/tmp'})!r}))\n")
    shim.write_text("#!/bin/sh\n"
                    "# workbench: git is read-only for the agent (see workbench/gitguard.py)\n"
                    f"exec {shlex.quote(sys.executable)} -I -c {shlex.quote(code)} \"$@\"\n")
    shim.chmod(0o755)
    return {"PATH": f"{bin_dir}{os.pathsep}{path}"}
