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
import os
import tempfile
from pathlib import Path

from . import gitops, skills, store

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
# sudo (no_new_privs, lane.py) and gh finds no login (its config and tokens are out of reach).
_HARD = ("sudo", "doas", "pkexec", "su", "gh", "*/git")
_WRAPPERS = ("env", "env *", "xargs", "xargs *", "nohup", "nice", "nice *", "time", "command",
             "exec", "timeout *", "stdbuf *", "setsid")
DENIED_SHELL = tuple(p for c in _HARD for p in (c, f"{c} *")) + \
    tuple(f"{w} {p}" for w in _WRAPPERS for c in _HARD for p in (c, f"{c} *"))
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
    conf = store.read_json(commands_path(), {}) or {}
    return [*(conf.get("allow") or []), *(grants(task).get("allow") or [])]


def commands_path() -> Path:
    return Path(os.environ.get("WB_COMMANDS") or Path.home() / ".config" / "workbench" / "commands.json")


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
              for d in (writable(task) or []) if os.path.isdir(d) and d not in ("/dev", "/proc")]
    rules += [{"action": "shell", "resource": r, "effect": "allow"} for r in PROJECT_BINS]
    rules += [{"action": "shell", "resource": r, "effect": "deny"} for r in ASK_FIRST]
    rules += [{"action": "shell", "resource": r, "effect": "allow"} for r in INSTALL_LISTED]
    rules += [{"action": "shell", "resource": r, "effect": "allow"} for r in allowed_patterns(task)]
    rules += [{"action": "shell", "resource": c, "effect": "deny"} for c in DENIED_SHELL]
    return rules


# The agent's lane: its worktree, the temp directory, and the caches and toolchain homes that
# builds and package managers fill, so a build or test runs without asking (2026-11-16, the user:
# keep agents in their lane with the least friction). Writes anywhere else are refused by the
# kernel (lane.py), file tools and the shell's working directory are kept to the lane and the
# READABLE places by OpenCode's `external_directory` rule, and the agent asks Claude for the rest
# (`wb allow --path`). Reading through the shell stays free: `cat`, a compiler, an interpreter.
TOOL_HOMES = (".cache", ".cargo", ".rustup", "go", ".npm", ".bun", ".deno", ".yarn",
              ".pnpm-store", ".local/share/pnpm", ".local/share/uv", ".gradle", ".m2", ".nuget",
              ".dotnet", ".gem", ".julia", ".cabal", ".stack", ".ghcup", ".platformio", ".conan2",
              ".hex", ".mix", ".sbt", ".ivy2", ".local/lib")
# where the user moved them: a toolchain follows its variable, not the default path
TOOL_HOME_VARS = ("CARGO_HOME", "RUSTUP_HOME", "GOPATH", "GOMODCACHE", "GOCACHE", "npm_config_cache",
                  "PIP_CACHE_DIR", "UV_CACHE_DIR", "GRADLE_USER_HOME", "BUN_INSTALL", "DENO_DIR",
                  "MIX_HOME", "HEX_HOME")
# what OpenCode and the ask server write themselves, beside the task's directory
OPENCODE_HOMES = (".local/share/opencode", ".local/state/opencode")
# the system, for the file tools: headers, man pages, installed packages' sources
READABLE = ("/usr", "/etc", "/opt", "/lib", "/lib64", "/bin", "/sbin", "/proc", "/sys", "/dev",
            "/nix", "/run/current-system", "/Library", "/System", "/Applications")


def lane_paths(task: store.Task) -> list[str]:
    """What the user (commands.json `paths`) and Claude (`wb allow --path`) added to the lane."""
    conf = store.read_json(commands_path(), {}) or {}
    home, out = os.path.realpath(Path.home()), []
    for p in [*(conf.get("paths") or []), *(grants(task).get("paths") or [])]:
        real = os.path.realpath(Path(p).expanduser()) if p != "*" else p
        if real not in ("/", home):  # never the whole machine or home; "*" turns the lane off
            out.append(p)
    return out


def _both(paths) -> list[str]:
    """Each path and its real path (Landlock follows a symlink, OpenCode's rule may not)."""
    out: list[str] = []
    for p in paths:
        for q in (str(p), os.path.realpath(p)):
            if q not in out:
                out.append(q)
    return out


def writable(task: store.Task) -> list[str] | None:
    """The directories (and files) the agent may write to, None when the user turned the lane off
    (`"paths": ["*"]`). Only those that exist: the kernel's rules are on real files."""
    extra = lane_paths(task)
    if "*" in extra:
        return None
    home = Path.home()
    xdg = [os.environ.get(v) for v in ("XDG_CACHE_HOME", "XDG_RUNTIME_DIR")]
    xdg += [os.path.join(os.environ[v], "opencode") for v in ("XDG_DATA_HOME", "XDG_STATE_HOME")
            if os.environ.get(v)]  # OpenCode's own data, when the user moved those
    xdg += [os.environ[v].split(os.pathsep)[0] for v in TOOL_HOME_VARS if os.environ.get(v)]
    places = [task.dir, tempfile.gettempdir(), "/tmp", "/var/tmp", "/dev", "/proc",
              *(home / h for h in TOOL_HOMES + OPENCODE_HOMES), *(x for x in xdg if x),
              *(Path(p).expanduser() for p in extra)]
    for name in ("notify.jsonl", "notify.jsonl.lock"):   # the ask server raises its questions
        f = store.home() / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.touch()
        places.append(f)
    whole = {"/", os.path.realpath(home)}  # a GOPATH=~ must not hand over the home directory
    return [p for p in _both(places) if os.path.exists(p) and os.path.realpath(p) not in whole]


# Where credentials and the user's own records live, under the home directory: the agent's
# processes can't read files there (Landlock), so a push, a gh call or a cloud login can't borrow
# the user's keys, nor can an agent read Claude's own files or the user's mail. Found 2026-11-16:
# `python3` running gh inside the lane was logged in, and the SSH keys that push were readable.
SECRETS = (".ssh", ".gnupg", ".password-store", ".netrc", ".git-credentials", ".authinfo",
           ".authinfo.gpg", ".bitwarden.json", ".secrets.gpg", ".config/gh", ".config/hub",
           ".local/share/keyrings", ".aws", ".azure", ".config/gcloud", ".kube", ".docker",
           ".terraform.d", ".pypirc", ".gem/credentials", ".cargo/credentials",
           ".cargo/credentials.toml", ".config/rclone", ".codex", ".pki", ".mozilla",
           ".thunderbird", ".config/chromium", ".config/google-chrome", ".config/BraveSoftware",
           ".camoufox", ".cloakbrowser", ".irssi", ".bash_history", ".zsh_history",
           ".python_history", ".claude", ".claude.json")
# Of the home directory's own entries, the dotfiles (tools' configs, caches, toolchains) stay
# readable, and these; the user's documents and other projects don't, beyond the task's repo,
# workbench itself and what Claude allowed (`wb allow --path`).
HOME_READABLE = ("bin", "go")


def readable(task: store.Task) -> list[str] | None:
    """What the agent's processes may read: the system, the home directory's dotfiles but
    SECRETS, the task's repo and its lane. None with the lane off. Landlock only allows, so this
    lists the entries beside each excluded one; lane.py adds the lane (writable) itself."""
    lane = writable(task)
    if lane is None:
        return None
    home = os.path.realpath(Path.home())
    hidden = {os.path.realpath(p) for p in (*(Path.home() / h for h in SECRETS), claude_home())}
    keep = {os.path.realpath(p) for p in (task.spec.get("repo"), PKG_ROOT, *lane,  # PKG_ROOT:
                                          *lane_paths(task)) if p}  # the git shim, the ask server
    under = lambda a, b: a.startswith(b.rstrip(os.sep) + os.sep)  # noqa: E731
    out: list[str] = []

    def visit(p: str, default: bool) -> None:
        real = os.path.realpath(p)
        if real in hidden or any(under(real, h) for h in hidden):
            return                                         # a secret, or a link into one
        if real in keep:
            out.append(p)
            return
        holds = [h for h in hidden | keep if under(h, real)]
        if default and not any(h in hidden for h in holds):
            out.append(p)
        elif holds:                                        # go in: something to skip or keep
            try:
                names = sorted(os.listdir(p))
            except OSError:
                return
            for name in names:
                visit(os.path.join(p, name),
                      name.startswith(".") or name in HOME_READABLE if real == home else default)
    visit("/", True)
    return out


def external_rules(task: store.Task) -> list[dict]:
    """OpenCode's `external_directory` rule: its file tools and the shell's working directory
    stay in the lane and READABLE, and the skills handed to the task."""
    lane = writable(task)
    if lane is None:
        return []
    have = skills.available()
    places = [*lane, *READABLE, *(have[n]["path"] for n in task.status.get("skills") or []
                                  if n in have)]
    rules = [{"action": "external_directory", "resource": "*", "effect": "deny"}]
    rules += [{"action": "external_directory", "resource": f"{p.rstrip('/')}/*", "effect": "allow"}
              for p in _both(places) if os.path.isdir(p)]
    return rules


# Claude's files: the instructions every agent reads, and the project's skills and settings.
# The agent proposes a change to them in its summary; Claude makes it.
CLAUDE_FILES = ("*CLAUDE.md", "*CLAUDE.local.md", "*AGENTS.md", ".claude/*", "*/.claude/*")


def rules_dir(task: store.Task) -> Path:
    """Where the agent's rules live (its OpenCode config, its instructions, the git shim): out
    of its lane, so it can't rewrite them for its next round. The task directory is in the lane
    (the ask server writes the task's status there)."""
    d = store.home() / "rules" / task.name
    d.mkdir(parents=True, exist_ok=True)
    return d


def grants(task: store.Task) -> dict:
    """What Claude allowed this task beyond the everyday rules: {"allow": [patterns], "paths":
    [dirs]}. Kept with the rules, not in the status the agent's lane can write: an agent that
    edited its own grants would widen its next round."""
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
    rules = [{"action": t, "resource": "*", "effect": "deny"} for t in DENIED_TOOLS]
    rules += shell_rules(task)
    rules += external_rules(task)
    rules += [{"action": "edit", "resource": f, "effect": "deny"} for f in CLAUDE_FILES]
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
