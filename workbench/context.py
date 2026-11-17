"""What the agent knows before it starts, so it works the way Claude would in the same repo.

OpenCode reads AGENTS.md by itself but not CLAUDE.md, and it lists every skill under
~/.claude/skills and the project's .claude/skills. This module adds the rest to the task's
config layer: the workbench handbook, the user's and the repo's Claude instructions, a map of
the nested CLAUDE.md files to read on the way, and a skill policy (the user's personal skills
and /delegate itself are hidden, leaving the project's own; so is `question`, which nobody
answers headless).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

from . import chat, gitops, images, models, skills, store

PKG_ROOT = Path(__file__).resolve().parent.parent
HANDBOOK = PKG_ROOT / "agent" / "HANDBOOK.md"
ROOT_FILES = ("CLAUDE.md", ".claude/CLAUDE.md", "CLAUDE.local.md")
# Tools an unattended run in a worktree must not use. Nobody answers `question`. Code mode
# (`execute`) and the opencode_* tools can move the session or remove worktrees. A `subagent`
# would load a second model onto the GPU behind the task's back. Denied tools also leave the prompt, which cuts
# the fixed prompt from ~9k to ~3.5k tokens: less to read on every request. OpenCode 2.3 still
# lists opencode_worktree_remove; the rule then refuses the call.
DENIED_TOOLS = ("question", "experimental_image_generate", "execute", "subagent", "websearch",
                "webfetch", "opencode_*", "*worktree*")  # the network, like curl: Claude's to allow
# The shell's hard limits: no root, and no road round the read-only git (gitguard.py, a `git` of
# its own first on the agent's PATH): not the real git named by its path, not `gh`, which pushes,
# opens pull requests and runs workflows. OpenCode checks each part of a compound command, so
# `cd x && sudo ...` is refused too. Everything else is the agent's, guided by the handbook.
# The plain wrappers are covered too (`env gh`, `timeout 5 sudo …`). A command built some other
# way (`bash -c`, an interpreter) still gets past these patterns; behind them, the kernel refuses
# sudo (no_new_privs, lane.py, on any Linux) and, with Landlock, gh finds no login (its config and
# tokens are out of reach). On macOS only these patterns stand.
_HARD = ("sudo", "doas", "pkexec", "su", "gh")
# a wrapper's option forms too: `time -p sudo x` matched only the allow rule `time *` (the
# 2026-11-16 audit); not `time *` itself, which would refuse `time pytest -k gh`
_WRAPPERS = ("env", "env *", "xargs", "xargs *", "nohup", "nohup --", "nice", "nice *", "time",
             "time -p", "time --", "command", "command -p", "command --", "exec", "timeout *",
             "stdbuf *", "setsid")


def _denied(commands) -> tuple[str, ...]:
    return tuple(p for c in commands for p in (c, f"{c} *")) + \
        tuple(f"{w} {p}" for w in _WRAPPERS for c in commands for p in (c, f"{c} *"))


DENIED_SHELL = _denied(_HARD)
# The real git by its path: the binaries themselves, not `*/git`, which (`*` matching anything)
# also refused `go test ./internal/git` and `ls pkg/git`, whatever Claude allowed (the 2026-11-16
# audit). A relative path to it still gets by, like `bash -c`: the shim and the repository's git
# directory outside the lane are the real guard.
GIT_PATHS = ("/usr/bin/git", "/bin/git", "/usr/local/bin/git", "/opt/homebrew/bin/git")


def denied_shell() -> tuple[str, ...]:
    """DENIED_SHELL and the real git binaries on PATH by their paths (the shim's left out)."""
    rules = str(store.home() / "rules")
    found = list(GIT_PATHS)
    for d in (os.environ.get("PATH") or "").split(os.pathsep):
        g = os.path.join(d, "git")
        if os.path.isabs(d) and not _under(os.path.realpath(d), os.path.realpath(rules)) \
                and os.path.isfile(g):
            found += [g, os.path.realpath(g)]
    return DENIED_SHELL + _denied(dict.fromkeys(found))
# The commands an agent runs without asking: the everyday tools of software work, generously
# (2026-11-16, the user: guide the agents, don't hobble them). OpenCode checks each command inside
# a compound line (pipes, &&, loops, subshells, $(…)) against these. Anything else is refused
# ("Permission denied: shell") and the agent asks Claude through ask_reviewer with `command`;
# `wb allow` grants it, for the task or always. This is a guardrail on the agent's own choices,
# not a sandbox: make and every interpreter can run anything. The hard limits stay DENIED_SHELL.
ALLOWED_COMMANDS = (
    # the shell itself
    "cd", "pwd", "echo", "printf", "test", "[", "true", "false", "export", "set", "unset", "env",
    "type", "command", "which", "time", "timeout", "sleep", "wait", "exit", "source", ".", "read",
    "nproc", "uname", "date", "whoami", "id", "hostname", "nice", "nohup", "ps", "kill", "jobs",
    # files and text
    "ls", "cat", "head", "tail", "wc", "sort", "uniq", "cut", "tr", "sed", "awk", "gawk", "grep",
    "egrep", "fgrep", "rg", "ag", "find", "fd", "xargs", "tee", "diff", "cmp", "comm", "patch",
    "file", "identify", "stat", "du", "df", "tree", "realpath", "dirname", "basename", "readlink",
    "touch", "mkdir", "rmdir", "cp", "mv", "rm", "ln", "chmod", "install", "mktemp", "tar", "gzip",
    "gunzip", "zcat", "zip", "unzip", "xz", "bzip2", "zstd", "jq", "yq", "column", "nl", "od",
    "xxd", "hexdump", "strings", "md5sum", "sha1sum", "sha256sum", "base64", "seq", "paste",
    "fold", "fmt", "expand", "rev", "tac", "split", "csplit", "join", "iconv", "less", "more",
    # building, testing, running code
    "make", "gmake", "cmake", "ctest", "ninja", "meson", "autoreconf", "configure", "pkg-config",
    "cc", "gcc", "g++", "c++", "clang", "clang++", "ld", "ar", "nm", "objdump", "strip", "gdb",
    "lldb", "valgrind", "clang-format", "clang-tidy", "cppcheck",
    "python", "python3", "pip", "pip3", "pytest", "ruff", "mypy", "black", "isort", "flake8",
    "pylint", "uv", "uvx", "poetry", "tox", "coverage",
    "node", "npm", "npx", "pnpm", "yarn", "bun", "bunx", "deno", "tsc", "tsx", "eslint",
    "prettier", "vitest", "jest", "playwright",
    "go", "gofmt", "cargo", "rustc", "rustfmt", "rustup", "java", "javac", "jar", "mvn", "gradle",
    "dotnet", "ruby", "bundle", "rake", "rspec", "php", "composer", "lua", "luajit", "zig",
    "swift", "swiftc", "kotlin", "kotlinc", "scala", "sbt", "ghc", "cabal", "stack", "elixir",
    "mix", "erl", "perl", "R", "Rscript", "julia",
    "bash", "sh", "zsh", "dash", "xvfb-run", "Xvfb", "sqlite3", "git",
)
# A project's own tools and builds, by their usual relative paths (`./x` is allowed already).
PROJECT_BINS = (".venv/bin/*", "venv/bin/*", "node_modules/.bin/*", "build/*", "target/debug/*",
                "target/release/*", "bin/*", "out/*", "dist/*", "zig-out/bin/*")
# Inside the allowed tools, the steps that change what the project depends on: a new package is
# Claude's decision (the handbook: no new dependencies), so these ask first even though the tool
# is allowed. Installing what the project already lists (`npm install`, `npm ci`) needs no ask.
ASK_FIRST = (
    "pip install *", "pip3 install *", "python -m pip install *", "python3 -m pip install *",
    "uv pip install *", "uv add *", "poetry add *", "npm install *", "npm i *", "npm add *",
    "pnpm add *", "pnpm install *", "pnpm i *", "yarn add *", "yarn global add *", "bun add *",
    "bun install *", "bun i *", "cargo add *", "cargo install *", "go install *", "go get *",
    "gem install *", "bundle add *", "composer require *", "deno add *", "deno install *",
    "dotnet add * package *",
    # fetch a package and run it
    "npx -y *", "npx --yes *", "npm exec *", "pnpm dlx *", "yarn dlx *", "bunx *", "uvx *",
    # their other spellings (the 2026-11-16 audit); a plain `npx <pkg>` runs only what the
    # project installed, as the runner sets npm_config_yes=false for OpenCode
    "npm x *", "npx -p *", "npx --package *", "npx --package=*", "bun x *", "uv tool run *",
    "uv tool install *", "uv run --with *", "deno run *npm:*", "deno run *jsr:*",
)
# OpenCode's matcher makes a trailing " *" optional, so `npm install *` also matches a bare
# `npm install`. Installing what the project already lists is the agent's: allowed again after.
INSTALL_LISTED = (
    "npm install", "npm i", "npm ci", "pnpm install", "pnpm i", "bun install", "bun i",
    "yarn", "yarn install", "pip install -r *", "pip3 install -r *", "pip install -e .",
    "pip3 install -e .", "python -m pip install -r *", "python3 -m pip install -r *",
    "python -m pip install -e .", "python3 -m pip install -e .", "uv pip install -r *",
    "uv pip install -e .",
)


def allowed_patterns(task: store.Task) -> list[str]:
    """What this task's agent may run beyond ALLOWED_COMMANDS: the user's standing allowances
    (commands.json `allow`; "*" turns the list off) and what Claude granted this task."""
    return [*commands_conf().get("allow", []), *(grants(task).get("allow") or [])]


def commands_path() -> Path:
    return Path(os.environ.get("WB_COMMANDS") or Path.home() / ".config" / "workbench" / "commands.json")


def commands_conf() -> dict:
    """commands.json, checked: a file that doesn't parse, or an `allow`, `paths` or `env` that
    isn't a list of strings, is refused, not read as {} (the 2026-11-16 audit: a trailing comma
    dropped every standing allowance silently, and `"allow": "make *"` was read letter by letter,
    its `*` allowing every command)."""
    path = commands_path()
    try:
        conf = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except ValueError as e:
        raise RuntimeError(f"{path} doesn't parse ({e}); fix it by hand") from None
    if not isinstance(conf, dict):
        raise RuntimeError(f"{path} must hold an object, e.g. {{\"allow\": [\"make *\"]}}")
    for key in ("allow", "paths", "env"):
        v = conf.get(key)
        if v is not None and not (isinstance(v, list) and all(isinstance(x, str) for x in v)):
            raise RuntimeError(f'{path}: "{key}" must be a list of strings, e.g. ["make *"]')
    return conf


def shell_rules(task: store.Task) -> list[dict]:
    """Deny by default, allow the everyday tools, ask before new dependencies, then what Claude
    and the user allowed; the hard limits last, so no allowance lifts them (later rules win)."""
    rules = [{"action": "shell", "resource": "*", "effect": "deny"}]
    rules += [{"action": "shell", "resource": r, "effect": "allow"}
              for c in ALLOWED_COMMANDS for r in (c, f"{c} *")]
    rules += [{"action": "shell", "resource": r, "effect": "allow"} for r in ("./*", "../*")]
    # and what it built anywhere in its lane, by path: a debug binary in the temp directory was
    # refused (2026-11-16) though `bash` could run it; the lane, not this list, bounds writes
    rules += [{"action": "shell", "resource": f"{d}/*", "effect": "allow"}
              for d in (writable(task) or []) if os.path.isdir(d)
              and not d.startswith(("/dev", "/proc"))]
    rules += [{"action": "shell", "resource": r, "effect": "allow"} for r in PROJECT_BINS]
    rules += [{"action": "shell", "resource": r, "effect": "deny"} for r in ASK_FIRST]
    rules += [{"action": "shell", "resource": r, "effect": "allow"} for r in INSTALL_LISTED]
    rules += [{"action": "shell", "resource": r, "effect": "allow"} for r in allowed_patterns(task)]
    rules += [{"action": "shell", "resource": c, "effect": "deny"} for c in denied_shell()]
    return rules


# The agent's lane: its worktree, the temp directory, and the caches and toolchain homes that
# builds and package managers fill, so a build or test runs without asking (2026-11-16, the user:
# keep agents in their lane with the least friction). Writes anywhere else are refused by the
# kernel (lane.py), file tools and the shell's working directory are kept to the lane and the
# READABLE places by OpenCode's `external_directory` rule, and the agent asks Claude for the rest
# (`wb allow --path`). Reading through the shell stays free: `cat`, a compiler, an interpreter.
# A toolchain's caches, not the directories on the user's PATH or in their shell's startup: a file
# the agent plants there runs later outside the lane (the 2026-11-16 audit: ~/.local/lib's user
# site-packages, whose .pth files every python runs; ~/.cargo/bin; GOPATH holding the repo itself).
TOOL_HOMES = (".cache", ".cargo", ".rustup", "go/pkg", ".npm", ".bun/install/cache", ".yarn",
              ".pnpm-store", ".local/share/pnpm/store", ".local/share/uv", ".gradle", ".m2",
              ".nuget", ".dotnet", ".gem", ".julia", ".cabal", ".stack", ".ghcup", ".platformio",
              ".conan2", ".hex", ".mix", ".sbt", ".ivy2", ".pub-cache", ".konan",
              ".local/share/hatch", ".local/share/virtualenvs")
# where the user moved them: a toolchain follows its variable, not the default path; GOPATH and
# BUN_INSTALL name a whole home (GOPATH may hold every project), so only their caches
TOOL_HOME_VARS = ("CARGO_HOME", "RUSTUP_HOME", "GOPATH", "GOMODCACHE", "GOCACHE", "npm_config_cache",
                  "PIP_CACHE_DIR", "UV_CACHE_DIR", "GRADLE_USER_HOME", "BUN_INSTALL", "DENO_DIR",
                  "MIX_HOME", "HEX_HOME", "PUB_CACHE", "KONAN_DATA_DIR", "HATCH_DATA_DIR",
                  "WORKON_HOME")
VAR_CACHES = {"GOPATH": "pkg", "BUN_INSTALL": "install/cache"}
# inside a toolchain home in the lane, what stays out of it: the programs on the user's PATH and
# what their shell sources (relative to the home, or to CARGO_HOME for cargo's)
NOT_WRITABLE = (".cargo/bin", ".cargo/env", ".dotnet/tools", ".cabal/bin", ".ghcup/bin")
# and what cargo makes at the top of its split home, made first so the split grants it: a split
# home refuses a new entry (the 0.90 review: a fresh rustup's ~/.cargo, only bin/ and env, failed
# every build with a dependency, and a git dependency failed wherever git/ didn't exist yet)
CARGO_MADE = ("registry/", "git/", ".package-cache", ".package-cache-mutate", ".global-cache")
# what OpenCode and the ask server write themselves, beside the task's directory
OPENCODE_HOMES = (".local/share/opencode", ".local/state/opencode")
# The device nodes builds use, not /dev: the user's terminals, input devices (/dev/uinput) and
# security keys were open to the agent (the 2026-11-16 audit). /dev/shm stays, a trade-off:
# multiprocessing's semaphores, Chromium and many test runners need it, and Landlock can't tell
# one program's segments from another's. /dev/pts stays out (every terminal of the user's); a
# task that needs it, or a GPU, asks (`wb allow --path`). /dev/ptmx alone opens no pty (Landlock
# checks the /dev/pts/N it hands out: "out of pty devices"); it is here so that a /dev/pts grant
# is all a pty-driven test needs (the 0.90 review).
DEV_NODES = ("/dev/null", "/dev/zero", "/dev/full", "/dev/random", "/dev/urandom", "/dev/tty",
             "/dev/ptmx", "/dev/shm")
# the system, for the file tools: headers, man pages, installed packages' sources
READABLE = ("/usr", "/etc", "/opt", "/lib", "/lib64", "/bin", "/sbin", "/proc", "/sys",
            "/nix", "/run/current-system", "/Library", "/System", "/Applications")
TEMP_ROOTS = ("/tmp", "/var/tmp")


def lane_paths(task: store.Task) -> list[str]:
    """What the user (commands.json `paths`) and Claude (`wb allow --path`) added to the lane:
    never the whole machine or home, nor a place in or under what the lane hides (lane_refusal);
    "*" turns the lane off."""
    out = []
    for p in [*commands_conf().get("paths", []), *(grants(task).get("paths") or [])]:
        if p == "*" or not lane_refusal(p):
            out.append(p)
    return out


def lane_refusal(raw: str) -> str | None:
    """Why `raw` can't join an agent's lane, or None: the whole machine or home, or a place in a
    secret or workbench's own (its state: rules, notices, other tasks; its config: the chat
    token). A place that holds one is granted around it (writable), so it isn't refused."""
    real = os.path.realpath(Path(raw).expanduser())
    if real in ("/", os.path.realpath(Path.home())):
        return f"{raw}: not the whole of / or your home; name the directory the task needs"
    for h in secret_paths() | private_paths() | {os.path.realpath(skills.library())}:
        if real == h or _under(real, h):
            return (f"{raw}: in {h}, which holds credentials or workbench's own state and stays "
                    "out of every lane; take what the task needs from it yourself")
    return None


def private_paths() -> set[str]:
    """Workbench's own files, real paths, which no agent reads or writes: the state directory
    (the rules, notify.jsonl, the chat log, other tasks) but the task's own files (readable), and
    the config (the chat token, commands.json). The 2026-11-16 audit read the chat token from a
    lane, which would let an agent post to Claude as the user."""
    paths = [store.home(), Path.home() / ".config" / "workbench", commands_path(),
             chat.token_path(), chat.config_path(), models.overrides_path(), images.config_path()]
    whole = {"/", os.path.realpath(Path.home())}
    return {r for r in map(os.path.realpath, paths) if r not in whole}


def _both(paths) -> list[str]:
    """Each path and its real path (Landlock follows a symlink, OpenCode's rule may not)."""
    out: list[str] = []
    for p in paths:
        for q in (str(p), os.path.realpath(p)):
            if q not in out:
                out.append(q)
    return out


def _under(a: str, b: str) -> bool:
    return a.startswith(b.rstrip(os.sep) + os.sep)


_COMMON: dict[str, str] = {}


def repo_dirs(task: store.Task) -> set[str]:
    """The task's repo and its git directory, real paths: no lane entry may hold either, or the
    agent could write a hook or core.fsmonitor that wb's own git runs outside the lane (the
    2026-11-16 audit: GOPATH=~/src, or ~/go with the repo in ~/go/src)."""
    repo = task.spec.get("repo")
    if not repo:
        return set()
    real = os.path.realpath(repo)
    if real not in _COMMON:
        try:
            _COMMON[real] = os.path.realpath(os.path.join(real, gitops.git(
                Path(real), "rev-parse", "--git-common-dir").strip()))
        except (gitops.GitError, OSError):
            return {real, os.path.join(real, ".git")}
    return {real, _COMMON[real]}


def _hidden() -> set[str]:
    """The secrets and workbench's own files that exist: what a lane entry is split around."""
    return {h for h in secret_paths() | private_paths() if os.path.lexists(h)}


def _split(place: str, out_of: set[str]) -> list[str]:
    """`place`, or when something in `out_of` lies beneath it, its other entries one by one
    (Landlock only allows: the way to leave one file out of a directory is to grant the rest)."""
    real = os.path.realpath(place)
    if real in out_of or any(_under(real, h) for h in out_of):
        return []
    if not any(_under(h, real) for h in out_of):
        return [place]
    try:
        names = sorted(os.listdir(real))
    except OSError:
        return []
    return [q for n in names for q in _split(os.path.join(real, n), out_of)]


def writable(task: store.Task) -> list[str] | None:
    """The directories (and files) the agent may write to, None when the user turned the lane off
    (`"paths": ["*"]`). Only those that exist: the kernel's rules are on real files."""
    extra = lane_paths(task)
    if "*" in extra:
        return None
    home = Path.home()
    # XDG_RUNTIME_DIR isn't here: it holds the user's bus, SSH and GPG agent sockets and their
    # runtime systemd units (the 2026-11-16 audit); the agent gets its own (runtime_dir)
    xdg = [os.environ.get("XDG_CACHE_HOME")]
    xdg += [os.path.join(os.environ[v], "opencode") for v in ("XDG_DATA_HOME", "XDG_STATE_HOME")
            if os.environ.get(v)]  # OpenCode's own data, when the user moved those
    for v in TOOL_HOME_VARS:
        if os.environ.get(v):
            first = os.environ[v].split(os.pathsep)[0]
            xdg.append(os.path.join(first, VAR_CACHES[v]) if v in VAR_CACHES else first)
    places = [tempfile.gettempdir(), *TEMP_ROOTS, *DEV_NODES, "/proc",
              *(home / h for h in TOOL_HOMES + OPENCODE_HOMES), *(x for x in xdg if x),
              *(Path(p).expanduser() for p in extra)]
    whole = {"/", os.path.realpath(home)}  # a GOPATH=~ must not hand over the home directory
    temps = {os.path.realpath(t) for t in (tempfile.gettempdir(), *TEMP_ROOTS)}
    guard = repo_dirs(task)  # a repo kept in /tmp is the user's choice: the temp dirs stay
    cargo = os.environ.get("CARGO_HOME")
    for c in {os.path.realpath(c) for c in (home / ".cargo", cargo) if c}:
        if os.path.isdir(c) and any(os.path.lexists(os.path.join(c, n)) for n in ("bin", "env")):
            for n in CARGO_MADE:
                try:
                    if n.endswith("/"):
                        os.makedirs(os.path.join(c, n), exist_ok=True)
                    elif not os.path.lexists(os.path.join(c, n)):
                        Path(c, n).touch()
                except OSError:  # a read-only home: the task starts all the same
                    pass
    out_of = _hidden() | \
        {os.path.realpath(p) for p in (*(home / h for h in NOT_WRITABLE),
                                       *((Path(cargo) / "bin", Path(cargo) / "env") if cargo else ()))
         if os.path.lexists(p)}
    # Of the task's directory only its worktree and ask/, where the ask server writes its
    # questions: the rest (task.json, status.json, steer.jsonl, the rounds, runner.log) steers
    # the runner and wb, outside the lane, and notify.jsonl wakes Claude for every task (the
    # 2026-11-16 audit: an agent could rewrite its own spec and guards, forge any task's
    # notice, and plant a link the runner then wrote through)
    kept = [str(task.worktree), str(ask_dir(task))]
    for p in places:
        real = os.path.realpath(p)
        if not os.path.exists(p) or real in whole:
            continue
        if real in temps:  # a secret or repo kept in /tmp stays in reach: splitting /tmp would
            kept.append(str(p))  # refuse every new file there (Java's, Xvfb's)
        else:  # split around the repo like a secret: a grant of ~/work beside the repo ~/work/api
            kept += _split(str(p), out_of | guard)  # was dropped whole (the 0.90 review)
    return [p for p in _both(kept) if os.path.exists(p) and os.path.realpath(p) not in whole]


# the toolchains `wb doctor` checks for snaps
TOOLCHAINS = ("go", "node", "npm", "npx", "yarn", "pnpm", "dotnet", "java", "cmake", "flutter",
              "dart", "kotlin", "cargo", "rustc", "python3", "opencode")


def lane_on() -> bool:
    """Whether the lane is on for every task (commands.json `"paths": ["*"]` turns it off)."""
    return "*" not in commands_conf().get("paths", [])


def snaps(names) -> list[str]:
    """The commands among `names` that resolve to a snap: snap-confine needs the privilege that
    the lane's no-new-privileges takes away, so a snap can't run in the lane."""
    out = []
    for n in names:
        p = shutil.which(n)
        if p and (p.startswith("/snap/") or os.path.realpath(p) == "/usr/bin/snap"):
            out.append(n)
    return out


def unix_paths(task: store.Task) -> list[str]:
    """Where the agent may connect to a UNIX socket made outside its lane (Landlock ABI 9): only
    what the user and Claude added (`paths`; `wb allow --path /run/postgresql` for a test
    database), never the session bus or the user's agents, split around secrets like the reads
    (the 0.90 review: a GPG agent socket under a granted ~/.config was in reach)."""
    hidden = _hidden()
    return _both(q for p in (Path(r).expanduser() for r in lane_paths(task) if r != "*")
                 if os.path.exists(p) for q in _split(str(p), hidden))


def ask_dir(task: store.Task) -> Path:
    """Where the agent's questions go (ask_mcp.py): the one place in its task directory that
    its lane may write."""
    d = task.dir / "ask"
    if task.dir.is_dir():
        d.mkdir(exist_ok=True)
    return d


def _runtime_path(task: store.Task) -> Path:
    tag = hashlib.sha256(f"{store.home()}\0{task.name}".encode()).hexdigest()[:12]
    base = Path("/tmp") if os.path.isdir("/tmp") else Path(tempfile.gettempdir())
    return base / f"wb-run-{os.getuid()}-{tag}"


def runtime_dir(task: store.Task) -> Path:
    """The agent's own XDG_RUNTIME_DIR, private (0700) and short (a socket path has ~107 bytes),
    in /tmp, which is in the lane: tools that want one (pnpm's locks, bun) get it, and nothing
    finds the user's session bus or agents there."""
    d = _runtime_path(task)
    try:
        d.mkdir(mode=0o700)
    except FileExistsError:
        pass
    st = os.lstat(d)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():  # someone else's: not this one
        return Path(tempfile.mkdtemp(prefix="wb-run-"))
    return d


def drop_runtime_dir(task: store.Task) -> None:
    """Remove the agent's runtime directory once the task is closed (accept, discard)."""
    d = _runtime_path(task)
    try:
        st = os.lstat(d)
    except OSError:
        return
    if stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid():
        shutil.rmtree(d, ignore_errors=True)


# Where credentials and the user's own records live, under the home directory: the agent's
# processes can't read files there (Landlock), so a push, a gh call or a cloud login can't borrow
# the user's keys, nor can an agent read Claude's own files or the user's mail. Found 2026-11-16:
# `python3` running gh inside the lane was logged in, and the SSH keys that push were readable.
# The audit that day added the rest below `.claude.json` (OpenCode's service password among
# them: its background service runs outside the lane). Left readable on purpose: `.npmrc` and
# `.yarnrc.yml` (the package managers read them to install) and OpenCode's own `auth.json`
# (OpenCode runs in the lane and needs it).
SECRETS = (".ssh", ".gnupg", ".password-store", ".netrc", ".git-credentials", ".authinfo",
           ".authinfo.gpg", ".bitwarden.json", ".secrets.gpg", ".config/gh", ".config/hub",
           ".local/share/keyrings", ".aws", ".azure", ".config/gcloud", ".kube", ".docker",
           ".terraform.d", ".pypirc", ".gem/credentials", ".cargo/credentials",
           ".cargo/credentials.toml", ".config/rclone", ".codex", ".pki", ".mozilla",
           ".thunderbird", ".config/chromium", ".config/google-chrome", ".config/BraveSoftware",
           ".camoufox", ".cloakbrowser", ".irssi", ".bash_history", ".zsh_history",
           ".python_history", ".claude", ".claude.json",
           ".config/opencode/service.json", ".local/state/opencode/service.json",
           ".config/git/credentials", ".gcm", ".pgpass", ".my.cnf", ".vault-token", ".s3cfg",
           ".boto", ".config/glab-cli", ".config/github-copilot", ".config/op",
           ".config/Bitwarden CLI", ".config/doctl", ".config/sops", ".config/age", ".fly",
           ".kaggle", ".oci", ".gemini", ".config/containers/auth.json",
           ".local/share/fish/fish_history", ".var/app", ".config/vivaldi",
           ".config/microsoft-edge", ".config/opera")
# where the user moved them (a file, a directory, or a path list)
SECRET_VARS = ("GNUPGHOME", "PASSWORD_STORE_DIR", "GH_CONFIG_DIR", "DOCKER_CONFIG", "KUBECONFIG",
               "AWS_SHARED_CREDENTIALS_FILE", "AWS_CONFIG_FILE", "CLOUDSDK_CONFIG",
               "AZURE_CONFIG_DIR", "HISTFILE")
# Of the home directory's own entries, the dotfiles (tools' configs, caches, toolchains) stay
# readable, and these (R's and Perl's user libraries); the user's documents and other projects
# don't, beyond the task's repo, workbench itself, the programs on PATH and what Claude allowed.
HOME_READABLE = ("bin", "go", "R", "perl5")
# Outside the home directory, these hold the user's data, not the system: like the home's own
# entries, only what is kept (the repo, the lane, a grant) is readable beneath them. /mnt holds
# the Windows drives on WSL2, /home the other users' homes (the 2026-11-16 audit).
ONLY_KEPT = ("/dev", "/mnt", "/media", "/run/media", "/run/user", "/home")


def secret_paths() -> set[str]:
    """SECRETS and Claude's directory, real paths, where the environment says they are too."""
    home = Path.home()
    paths = [*(home / h for h in SECRETS), claude_home()]
    for var, base in (("XDG_CONFIG_HOME", ".config/"), ("XDG_DATA_HOME", ".local/share/"),
                      ("XDG_STATE_HOME", ".local/state/")):
        if os.environ.get(var):
            paths += [Path(os.environ[var]) / h[len(base):] for h in SECRETS if h.startswith(base)]
    paths += [Path(p).expanduser() for v in SECRET_VARS
              for p in (os.environ.get(v) or "").split(os.pathsep) if p]
    if os.environ.get("CARGO_HOME"):
        paths += [Path(os.environ["CARGO_HOME"]) / n for n in ("credentials", "credentials.toml")]
    try:  # .claude.json's backups and temporary copies
        paths += [home / n for n in os.listdir(home) if n.startswith(".claude.json")]
    except OSError:
        pass
    whole = {"/", os.path.realpath(home)}  # a variable set to ~ must not hide the whole home
    return {r for r in map(os.path.realpath, paths) if r not in whole}


def tool_roots(hidden: set[str]) -> set[str]:
    """The installs the agent's programs run from: each PATH directory's prefix (its parent when
    it is a `bin`), the targets of the links in the home's PATH directories, and the Python
    running workbench (the git shim, the ask server). Running a program reads it, so one under a
    non-dot home directory (~/miniconda3, ~/flutter, ~/apps) failed in the lane (the 2026-11-16
    audit). The same under an ONLY_KEPT root, short of a user's home or a whole drive: Linuxbrew's
    bin links into ../Cellar, which the lane hid (the 0.90 review). Never `/`, the home directory
    or anything holding a secret: a prefix is kept whole."""
    home = os.path.realpath(Path.home())
    prefix = lambda d: os.path.dirname(d) if os.path.basename(d) == "bin" else d  # noqa: E731
    roots = {os.path.realpath(r) for r in ONLY_KEPT}

    def opens(p: str) -> bool:  # under an ONLY_KEPT root, deeper than a home or a drive
        for root in roots:
            if _under(p, root):
                parts = p[len(root):].strip(os.sep).split(os.sep)
                deep = 3 if root.endswith("media") else 2       # /media/<user>/<drive>
                if len(parts) > 1 and parts[1] == "Users":      # /mnt/c/Users/<you>
                    deep = 4
                return len(parts) >= deep and not (p == home or _under(home, p))
        return False

    found = set()
    for d in (os.environ.get("PATH") or "").split(os.pathsep):
        if not os.path.isabs(d) or not os.path.isdir(d):
            continue
        real = os.path.realpath(d)
        if not _under(real, home) and not opens(prefix(real)):
            found.add(real)  # the directory alone: /mnt/c/Users/<you>/bin isn't a prefix
            continue
        found.add(prefix(real))
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for n in names:
            link = os.path.join(d, n)
            if os.path.islink(link):
                target = os.path.realpath(link)
                where = prefix(os.path.dirname(target))
                if _under(target, home) or opens(where):
                    found.add(where)
    found |= {prefix(os.path.dirname(os.path.realpath(sys.executable)))}
    found |= {os.path.realpath(p) for p in (sys.prefix, sys.base_prefix, sys.exec_prefix,
                                            os.environ.get("CONDA_PREFIX")) if p}
    return {r for r in found if r not in ("/", home)
            and not any(h == r or _under(h, r) for h in hidden)}


def readable(task: store.Task) -> list[str] | None:
    """What the agent's processes may read: the system, the home directory's dotfiles but
    SECRETS, the task's repo and its git directory, its lane and the programs it runs. None with the lane off. Landlock
    only allows, so this lists the entries beside each excluded one; lane.py adds the lane
    (writable) itself."""
    lane = writable(task)
    if lane is None:
        return None
    home = os.path.realpath(Path.home())
    hidden = secret_paths() | private_paths()
    keep = {os.path.realpath(p) for p in (task.spec.get("repo"), PKG_ROOT, *lane,  # PKG_ROOT:
                                          *lane_paths(task)) if p}  # the git shim, the ask server
    keep |= tool_roots(hidden)
    # the git directory too, outside the repo when it is a linked worktree: git failed there
    # ("gitfile does not point to a valid repository", the 0.90 review); writable() keeps it out
    keep |= repo_dirs(task)
    # Inside a hidden directory, only these come out: workbench itself (the marketplace install
    # puts it in ~/.claude/plugins, the 2026-11-16 audit), and of workbench's state the task's own
    # directory (its prompts, the ask server's spec) and its rules (its OpenCode config, its
    # instructions, the git shim), read-only; the rest stays hidden
    own = (PKG_ROOT, task.dir, rules_dir(task))
    inner = {r for r in map(os.path.realpath, own) if any(_under(r, h) or r == h for h in hidden)}
    only_kept = {os.path.realpath(p) for p in ONLY_KEPT}
    under = _under
    out: list[str] = []

    def visit(p: str, default: bool) -> None:
        real = os.path.realpath(p)
        if real in hidden or any(under(real, h) for h in hidden):
            if real in inner:
                out.append(p)
            elif any(under(i, real) for i in inner):
                try:
                    names = sorted(os.listdir(p))
                except OSError:
                    names = []
                for name in names:
                    visit(os.path.join(p, name), False)
            return                                         # a secret, or a link into one
        inside = real in keep
        holds = [h for h in hidden | keep | only_kept if under(h, real)]
        hides = any(h in hidden or h in only_kept for h in holds)
        if inside and not hides:
            out.append(p)
            return
        if default and not hides:
            out.append(p)
        elif holds:                    # go in: something to skip or keep (a repo at ~/.config
            try:                       # keeps the rest of itself, not its secrets)
                names = sorted(os.listdir(p))
            except OSError:
                return
            for name in names:
                child = os.path.join(p, name)
                if inside:
                    visit(child, True)
                elif os.path.realpath(child) in only_kept:
                    visit(child, False)
                else:
                    visit(child, name.startswith(".") or name in HOME_READABLE
                          if real == home else default)
    visit("/", True)
    return out


def external_rules(task: store.Task) -> list[dict]:
    """OpenCode's `external_directory` rule: its file tools and the shell's working directory
    stay in the lane and READABLE, and the skills handed to the task."""
    lane = writable(task)
    if lane is None:  # (explicit: layer's rule for other MCP servers' tools would match it)
        return [{"action": "external_directory", "resource": "*", "effect": "allow"}]
    have = skills.available()
    # and the rounds' prompts, read-only, where a prompt too long for one argument waits
    places = [*lane, *READABLE, task.dir / "rounds",
              *(have[n]["path"] for n in task.status.get("skills") or [] if n in have)]
    rules = [{"action": "external_directory", "resource": "*", "effect": "deny"}]
    rules += [{"action": "external_directory", "resource": f"{p.rstrip('/')}/*", "effect": "allow"}
              for p in _both(places) if os.path.isdir(p)]
    return rules


# Claude's files: the instructions every agent reads, and the project's skills and settings.
# The agent proposes a change to them in its summary; Claude makes it.
CLAUDE_FILES = ("*CLAUDE.md", "*CLAUDE.local.md", "*AGENTS.md", ".claude/*", "*/.claude/*")
# and OpenCode's project config, which loads after the task's layer: an `opencode.json` the agent
# wrote lifted its own denies from the next round on (the 2026-11-16 audit). The runner also puts
# the rules last (OPENCODE_CONFIG_CONTENT), so these only keep the file tools off them.
OPENCODE_FILES = ("*opencode.json", "*opencode.jsonc", ".opencode/*", "*/.opencode/*")
# Every MCP server's tools but workbench's: the user's own servers in their global OpenCode config
# (a GitHub or database server, with their token) reached every agent (the 2026-11-16 audit), and
# a project's could too. OpenCode names an MCP tool <server>_<tool>; no built-in tool has an
# underscore, but two permissions do, so they get their rules back after this one.
MCP_RULES = ({"action": "*_*", "resource": "*", "effect": "deny"},
             {"action": "workbench_*", "resource": "*", "effect": "allow"},
             {"action": "doom_loop", "resource": "*", "effect": "allow"})  # what --auto gave it


def rules_dir(task: store.Task) -> Path:
    """Where the agent's rules live (its OpenCode config, its instructions, the git shim): out
    of its lane, so it can't rewrite them for its next round."""
    d = store.home() / "rules" / task.name
    d.mkdir(parents=True, exist_ok=True)
    return d


def grants(task: store.Task) -> dict:
    """What Claude allowed this task beyond the everyday rules: {"allow": [patterns], "paths":
    [dirs]}. Kept with the rules, out of the agent's reach: an agent that edited its own grants
    would widen its next round."""
    return store.read_json(rules_dir(task) / "grants.json", {}) or {}


def grant(task: store.Task, key: str, new: list[str]) -> list[str]:
    """Add to one of the task's grants; returns the ones it didn't have."""
    with store.locked(rules_dir(task) / "grants.lock"):
        have = grants(task)
        added = [p for p in new if p not in (have.get(key) or [])]
        if added:
            store.write_json(rules_dir(task) / "grants.json",
                             {**have, key: (have.get(key) or []) + added})
    return added


def claude_home() -> Path:
    """The user's Claude Code directory: $CLAUDE_CONFIG_DIR when they moved it, else ~/.claude."""
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tracked(worktree: Path, *patterns: str) -> list[str]:
    out = gitops.git(worktree, "ls-files", "-z", "--", *patterns)
    return sorted(p for p in out.split("\0") if p)


def instruction_files(task: store.Task) -> tuple[list[Path], list[str]]:
    """(files OpenCode should load, nested CLAUDE.md paths the agent reads when it works there)."""
    wt, repo = task.worktree, Path(task.spec["repo"])
    # the agent's own copy: OpenCode shows each instruction file's path, and a path into the
    # workbench repo once led a freshly started agent to go looking there for plans
    handbook = rules_dir(task) / "HANDBOOK.md"
    handbook.write_text(HANDBOOK.read_text())
    load: list[Path] = [handbook]
    user = claude_home() / "CLAUDE.md"
    if user.is_file():  # a copy too: the agent can't read Claude's directory (SECRETS)
        mine = rules_dir(task) / "CLAUDE.md"
        mine.write_text(user.read_text())
        load.append(mine)
    for rel in ROOT_FILES:
        # the worktree's copy when the file is committed; the repo's when it is local or uncommitted
        for base in (wt, repo):
            if (base / rel).is_file():
                load.append(base / rel)
                break
    load += [wt / p for p in _tracked(wt, ".claude/rules/*.md")]
    # OpenCode already loads the worktree's AGENTS.md: skip a CLAUDE.md with the same text (one
    # only in the user's checkout isn't loaded, so it can't stand in for CLAUDE.md)
    agents = {_digest(wt / "AGENTS.md")} if (wt / "AGENTS.md").is_file() else set()
    seen, files = set(agents), []
    for p in load:
        d = _digest(p)
        if d not in seen:
            seen.add(d)
            files.append(p)
    nested = [p for p in _tracked(wt, "*CLAUDE.md", "*AGENTS.md")
              if "/" in p and Path(p).name in ("CLAUDE.md", "AGENTS.md")
              and not p.startswith(".claude/")]
    return files, nested


def denied_skills(extra: list[str] | None = None, project: Path | None = None) -> list[str]:
    """The user's personal skills (~/.claude/skills, the synced Claude-app ones included) are
    theirs, about their other work: an agent sees only the project's own .claude/skills. On
    2026-11-16 an agent, its context full, summarised a personal publishing skill
    into a made-up "newer task" and went off reading the user's private library."""
    names = {"delegate", *(extra or [])}
    for home in {claude_home() / "skills", Path.home() / ".claude" / "skills"}:  # OpenCode reads ~/.claude
        names |= {s.parent.name for s in home.glob("*/SKILL.md")}
        names |= {s.parent.name for s in (home / "synced").glob("*/*/SKILL.md")}
    if project:  # a project's own skill keeps its name (the personal ones are unreadable anyway)
        names -= {s.parent.name for s in (project / ".claude" / "skills").glob("*/SKILL.md")}
    return sorted(names | {"delegate", *(extra or [])})


def write_map(task: store.Task, nested: list[str]) -> Path | None:
    """A short note pointing at the directory-level instructions, read only where they apply."""
    if not nested:
        return None
    path = task.dir / "context.md"
    lines = ["# Directory instructions in this repository", "",
             "Before you change files under one of these directories, read its file and follow it:"]
    lines += [f"- `{p}`" for p in nested]
    path.write_text("\n".join(lines) + "\n")
    return path


def layer(task: store.Task) -> dict:
    """The config-layer keys that give the agent its context and its skill policy."""
    files, nested = instruction_files(task)
    note = write_map(task, nested)
    rules = [dict(r) for r in MCP_RULES]   # external_directory gets its own below
    rules += [{"action": t, "resource": "*", "effect": "deny"} for t in DENIED_TOOLS]
    rules += shell_rules(task)
    rules += external_rules(task)
    rules += [{"action": "edit", "resource": f, "effect": "deny"}
              for f in CLAUDE_FILES + OPENCODE_FILES]
    rules += [{"action": "skill", "resource": n, "effect": "deny"}
              for n in denied_skills(task.spec.get("deny_skills"), task.worktree)]
    # the skill library is Claude's to write, with or without the kernel's lane
    rules += [{"action": "edit", "resource": f"{p.rstrip('/')}/*", "effect": "deny"}
              for p in _both([skills.library()])]
    # the skills Claude handed this task, one directory each: OpenCode lists exactly those, and
    # the rest of the library stays out of the prompt (a retired one is simply left out)
    have = skills.available()
    handed = [str(have[n]["path"]) for n in task.status.get("skills") or [] if n in have]
    return {"instructions": [str(p) for p in files + ([note] if note else [])],
            "permissions": rules, "skills": handed}
