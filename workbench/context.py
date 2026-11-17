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
                "opencode_*", "*worktree*")
# The shell's hard limits: no root, and no road round the read-only git (gitguard.py, a `git` of
# its own first on the agent's PATH): not the real git named by its path, not `gh`, which pushes,
# opens pull requests and runs workflows. OpenCode checks each part of a compound command, so
# `cd x && sudo ...` is refused too. Everything else is the agent's, guided by the handbook.
DENIED_SHELL = ("sudo", "sudo *", "doas", "doas *", "pkexec *", "su", "su *",
                "*/git", "*/git *", "gh", "gh *")
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
    "file", "stat", "du", "df", "tree", "realpath", "dirname", "basename", "readlink", "touch",
    "mkdir", "rmdir", "cp", "mv", "rm", "ln", "chmod", "install", "mktemp", "tar", "gzip",
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
    "bash", "sh", "zsh", "dash", "xvfb-run", "Xvfb", "sqlite3", "git", "opencode",
)
# Inside the allowed tools, the steps that change what the project depends on: a new package is
# Claude's decision (the handbook: no new dependencies), so these ask first even though the tool
# is allowed. Installing what the project already lists (`npm install`, `npm ci`) needs no ask.
ASK_FIRST = (
    "pip install *", "pip3 install *", "python -m pip install *", "python3 -m pip install *",
    "uv pip install *", "uv add *", "poetry add *", "npm install *", "npm i *", "npm add *",
    "pnpm add *", "pnpm install *", "yarn add *", "bun add *", "bun install *", "cargo add *",
    "cargo install *", "go install *", "go get *", "gem install *", "bundle add *",
    "composer require *",
)


def allowed_patterns(task: store.Task) -> list[str]:
    """What this task's agent may run beyond ALLOWED_COMMANDS: the user's standing allowances
    (commands.json `allow`; "*" turns the list off) and what Claude granted this task."""
    conf = store.read_json(commands_path(), {}) or {}
    return [*(conf.get("allow") or []), *(task.status.get("allow") or [])]


def commands_path() -> Path:
    return Path(os.environ.get("WB_COMMANDS") or Path.home() / ".config" / "workbench" / "commands.json")


def shell_rules(task: store.Task) -> list[dict]:
    """Deny by default, allow the everyday tools, ask before new dependencies, then what Claude
    and the user allowed; the hard limits last, so no allowance lifts them (later rules win)."""
    rules = [{"action": "shell", "resource": "*", "effect": "deny"}]
    rules += [{"action": "shell", "resource": r, "effect": "allow"}
              for c in ALLOWED_COMMANDS for r in (c, f"{c} *")]
    rules += [{"action": "shell", "resource": r, "effect": "allow"} for r in ("./*", "../*")]
    rules += [{"action": "shell", "resource": r, "effect": "deny"} for r in ASK_FIRST]
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
              ".dotnet", ".gem", ".julia", ".cabal", ".stack", ".ghcup", ".platformio", ".conan2")
# what OpenCode and the ask server write themselves, beside the task's directory
OPENCODE_HOMES = (".local/share/opencode", ".local/state/opencode")
# the system, for the file tools: headers, man pages, installed packages' sources
READABLE = ("/usr", "/etc", "/opt", "/lib", "/lib64", "/bin", "/sbin", "/proc", "/sys", "/dev",
            "/nix", "/run/current-system", "/Library", "/System", "/Applications")


def lane_paths(task: store.Task) -> list[str]:
    """What the user (commands.json `paths`) and Claude (`wb allow --path`) added to the lane."""
    conf = store.read_json(commands_path(), {}) or {}
    return [*(conf.get("paths") or []), *(task.status.get("paths") or [])]


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
    places = [task.dir, tempfile.gettempdir(), "/tmp", "/var/tmp", "/dev", "/proc",
              *(home / h for h in TOOL_HOMES + OPENCODE_HOMES), *(x for x in xdg if x),
              *(Path(p).expanduser() for p in extra)]
    for name in ("notify.jsonl", "notify.jsonl.lock"):   # the ask server raises its questions
        f = store.home() / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.touch()
        places.append(f)
    return [p for p in _both(places) if os.path.exists(p)]


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
    handbook = task.dir / "HANDBOOK.md"
    handbook.write_text(HANDBOOK.read_text())
    load: list[Path] = [handbook]
    user = claude_home() / "CLAUDE.md"
    if user.is_file():
        load.append(user)
    for rel in ROOT_FILES:
        # the worktree's copy when the file is committed; the repo's when it is local or uncommitted
        for base in (wt, repo):
            if (base / rel).is_file():
                load.append(base / rel)
                break
    load += [wt / p for p in _tracked(wt, ".claude/rules/*.md")]
    # OpenCode already loads AGENTS.md: skip a CLAUDE.md that is the same file or the same text
    agents = {_digest(p) for p in (wt / "AGENTS.md", repo / "AGENTS.md") if p.is_file()}
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


def denied_skills(extra: list[str] | None = None) -> list[str]:
    """The user's personal skills (~/.claude/skills, the synced Claude-app ones included) are
    theirs, about their other work: an agent sees only the project's own .claude/skills. On
    2026-11-16 an agent building a game, its context full, summarised a personal publishing skill
    into a made-up "newer task" and went off reading the user's private library."""
    names = {"delegate", *(extra or [])}
    for home in {claude_home() / "skills", Path.home() / ".claude" / "skills"}:  # OpenCode reads ~/.claude
        names |= {s.parent.name for s in home.glob("*/SKILL.md")}
        names |= {s.parent.name for s in (home / "synced").glob("*/*/SKILL.md")}
    return sorted(names)


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
              for n in denied_skills(task.spec.get("deny_skills"))]
    # the skills Claude handed this task, one directory each: OpenCode lists exactly those, and
    # the rest of the library stays out of the prompt (a retired one is simply left out)
    have = skills.available()
    handed = [str(have[n]["path"]) for n in task.status.get("skills") or [] if n in have]
    return {"instructions": [str(p) for p in files + ([note] if note else [])],
            "permissions": rules, "skills": handed}
