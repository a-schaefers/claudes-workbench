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
# apply's options that write the index or the object store (--build-fake-ancestor writes an index
# file); without them `git apply` writes only working-tree files, which the agent may edit
# anyway: plain `git apply p` and `-R` were refused, and a setup or test script applying patches
# failed every round (#890, the third 2026-11-21 audit). Matched by prefix too: git takes `--ind`
APPLY_INDEX = {"--index", "--cached", "--3way", "--intent-to-add", "--build-fake-ancestor"}
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
    if sub == "apply":  # reports on the patch, or applies it to working-tree files only
        return not any(o.startswith(f) for f in flags if f.startswith("--") and len(f) > 2
                       for o in APPLY_INDEX) and not _apply_shorts(rest) & {"3", "N"}
    if sub == "hash-object":  # prints the id; -w writes the object
        return "-w" not in flags
    if sub == "fsck":  # --lost-found writes into the git dir
        return "--lost-found" not in flags
    if sub == "symbolic-ref":
        return len(plain) == 1 and not flags & {"-d", "--delete", "-m"}
    return False


def _apply_shorts(rest: list[str]) -> set[str]:
    """apply's single-letter options, clusters split (`-3R`); `-p` and `-C` take the rest of
    their word as a value (`-p3` isn't `-3`)."""
    letters = set()
    for a in rest:
        if a == "--":
            break
        if a.startswith("-") and not a.startswith("--"):
            for c in a[1:]:
                letters.add(c)
                if c in "pC":
                    break
    return letters


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
    second 2026-11-21 audit). The user's own (user_configs) main refuses: only the lane kept
    them out of reach, and without one (macOS) the agent rewrote ~/.gitconfig (#1120)."""
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


def user_configs(env=None) -> list[str]:
    """The user's own global and system git config files, real paths, as the environment at
    install names them (HOME's .gitconfig, XDG_CONFIG_HOME's git/config, GIT_CONFIG_GLOBAL,
    GIT_CONFIG_SYSTEM, else /etc/gitconfig): a `git config --global` the shim lets through with
    HOME at a fixture's temp directory writes elsewhere (#794), one to these never (#1120)."""
    env = os.environ if env is None else env
    home = env.get("HOME") or os.path.expanduser("~")
    xdg = env.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    files = [os.path.join(home, ".gitconfig"), os.path.join(xdg, "git", "config"),
             env.get("GIT_CONFIG_GLOBAL"), env.get("GIT_CONFIG_SYSTEM") or "/etc/gitconfig"]
    return sorted({os.path.realpath(f) for f in files if f and os.path.isabs(f)})


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


# `git submodule update`'s options that take the next argument as their value
SUBMODULE_VALUED = {"--reference", "--depth", "-j", "--jobs", "--filter", "--name", "-b",
                    "--branch"}
FILLED = ("workbench: submodules are filled already ({paths}), so `git submodule {op}` was not "
          "run and has nothing to do.\n")
UNFILLED = ("workbench: `git submodule {op}` was not run: workbench fills submodules itself, from "
            "the reviewer's checkout, and {paths} couldn't be filled (that checkout lacks it). Say "
            "so with ask_reviewer or in your summary.\n")


def filled_submodules(real: str, task_git: str, opts: list[str], rest: list[str],
                      lanes=()) -> tuple[int, str] | None:
    """`git submodule update|init|sync` in the task's worktree or a scratch checkout of it
    (``lanes[0]``), whose submodules workbench filled itself (gitops.fill_submodules: files, no
    `.git`): (0, a note) when each one asked for is filled, else (1, why). None for any other
    command or repository. A build that inits its own submodules (CMake's GIT_SUBMODULE
    snippet, a Makefile prerequisite) was refused as a write in the worktree, and in the base
    check's clone git failed on the filled directories ("already exists and is not an empty
    directory"): the base and every round went red with the change not at fault (#885, the
    third 2026-11-21 audit)."""
    words = [a for a in rest if a not in ("-q", "--quiet")]
    if not words or words[0] not in ("update", "init", "sync"):
        return None
    op, paths, skip, dashed = words[0], [], False, False
    for a in words[1:]:
        if skip:
            skip = False
        elif dashed or not a.startswith("-"):
            paths.append(a)
        elif a == "--":
            dashed = True
        else:
            skip = a in SUBMODULE_VALUED
    top = subprocess.run([real, *opts, "rev-parse", "--show-toplevel"], capture_output=True,
                         text=True, encoding="utf-8", errors="surrogateescape")
    if top.returncode or not top.stdout.strip():
        return None
    top = os.path.realpath(top.stdout.strip())
    if not (common_dir(real, opts) == os.path.realpath(task_git)
            or (lanes and top == os.path.realpath(lanes[0]))):
        return None
    listed = subprocess.run([real, "-C", top, "config", "-z", "-f", ".gitmodules",
                             "--get-regexp", r"^submodule\..*\.path$"], capture_output=True)
    subs = [os.fsdecode(r.split(b"\n", 1)[1]) for r in listed.stdout.split(b"\0") if b"\n" in r]
    where = _where(opts)
    asked = {os.path.relpath(os.path.realpath(os.path.join(where, p)), top).rstrip("/")
             for p in paths}
    subs = [p for p in subs if not asked or p.rstrip("/") in asked]
    if not subs:
        return None

    def filled(path: str) -> bool:
        d = os.path.join(top, path)
        try:
            return os.path.isdir(d) and not os.path.lexists(os.path.join(d, ".git")) \
                and any(os.scandir(d))
        except OSError:
            return False
    if any(os.path.lexists(os.path.join(top, p, ".git")) for p in subs):
        return None  # a real submodule checkout: git's own business
    empty = [p for p in subs if not filled(p)]
    if empty:
        return 1, UNFILLED.format(op=op, paths=", ".join(empty))
    return 0, FILLED.format(op=op, paths=", ".join(subs))


REFUSAL = """\
workbench: git is read-only for you here, so `git {sub}` was not run.
Leave your changes uncommitted: Claude reviews the diff and commits it, and handles branches,
merges, pushes and deploys. Read freely: status, diff, log, show, blame, grep. To undo your own
edit to a file, edit it back or use `git show HEAD:<path>`. If the task needs a git write, say so
with ask_reviewer or in your summary.
"""


def main(real: str, task_git: str, args: list[str], temps=(), lanes=(), pins=(), owns=()) -> int:
    """``lanes``: the worktree and the lane's tool homes, where a repository other than the
    task's is writable (a test suite's own, a package manager's). ``pins``: the `-c` options
    that make git see the worktree's file system as wb's own calls do (gitops.fs_pins), given
    to a read in the task's repository only: forced on every git, a test suite's scratch repos
    on other file systems would get them too (#796). ``owns``: the user's own git config files
    (user_configs, at install), which no `git config` write reaches (#1120)."""
    opts, sub, rest = split(args)
    if reads(sub, rest):
        pre = list(pins) if pins and common_dir(real, opts) == os.path.realpath(task_git) else []
        # no index refresh in the task's repo, which Claude may be diffing at the same moment
        os.execve(real, [real, *pre, *args], {**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
    if sub == "submodule" and (said := filled_submodules(real, task_git, opts, rest, lanes)):
        code, line = said
        sys.stderr.write(line)
        return code
    if sub == "config" and (files := config_files(rest, opts)) is not None:
        # a file of its own: refused only where it is the task's repository's (#794)
        # or a `.git` link, whose rewrite would point the worktree at a git dir of its own, or
        # the user's own ~/.gitconfig or /etc/gitconfig: without a lane (macOS) `git config
        # --global user.name "Test Bot"` rewrote the user's identity (#1120, 2026-11-23)
        if not any(_inside(os.path.realpath(f), [os.path.realpath(task_git)])
                   or os.path.basename(os.path.realpath(f)) == ".git"
                   or os.path.realpath(f) in owns for f in files):
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
            worktree: Path | None = None, scratch=(), pins=()) -> dict | None:
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
            f"{lanes!r}, {list(pins)!r}, {user_configs()!r}))\n")
    shim.write_text("#!/bin/sh\n"
                    "# workbench: git is read-only for the agent (see workbench/gitguard.py)\n"
                    f"exec {shlex.quote(sys.executable)} -I -c {shlex.quote(code)} \"$@\"\n")
    shim.chmod(0o755)
    return {"PATH": f"{bin_dir}{os.pathsep}{path}"}
