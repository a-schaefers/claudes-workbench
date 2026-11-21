"""What the agent knows before it starts, so it works the way Claude would in the same repo.

OpenCode reads AGENTS.md by itself but not CLAUDE.md, and it lists every skill under
~/.claude/skills and the project's .claude/skills. This module adds the rest to the task's
config layer: the workbench handbook, the user's and the repo's Claude instructions, a map of
the nested CLAUDE.md files to read on the way, and a skill policy (the user's personal skills
and /workbench itself are hidden, leaving the project's own; so is `question`, which nobody
answers headless).
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

from . import audio, chat, gitops, hardware, images, lane, models, skills, store

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
# A command written `NAME=value cmd`: OpenCode matches each simple command's raw text, so `FOO=1
# ls` was refused while `ls` and `env FOO=1 ls` ran. It is how CLAUDE.md files and READMEs spell
# tests (`PYTHONPATH=. pytest`, `CI=1 npm test`), and a small model copying the project's command
# spent a Claude wake asking, or stopped running the tests (#258, the audit of 1.2). `*` matches
# spaces too, so the prefix form lets through a line with an `=` before an allowed command's name:
# no more than `env *` already runs. The ask-first steps get the form as well; the hard limits
# don't (#442).
ASSIGN = "*=*"
# The shell's hard limits: no root, and no road round the read-only git (gitguard.py, a `git` of
# its own first on the agent's PATH): not the real git named by its path, not `gh`, which pushes,
# opens pull requests and runs workflows. OpenCode checks each part of a compound command, so
# `cd x && sudo ...` is refused too. Everything else is the agent's, guided by the handbook.
# The plain wrappers are covered too (`env gh`, `timeout 5 sudo …`). A command built some other
# way (`bash -c`, an interpreter, `FOO=1 sudo`, `env CI=1 gh`) still gets past these patterns;
# behind them, the kernel refuses sudo (no_new_privs, lane.py, on any Linux) and, with Landlock,
# gh finds no login (its config and tokens are out of reach). On macOS only these patterns stand.
_HARD = ("sudo", "doas", "pkexec", "su", "gh")
# A wrapper's own option shapes, before the hard word: `time -p sudo x` matched only the allow
# rule `time *` (the 2026-11-16 audit). Never a `*` before the hard word: `*` matches spaces too,
# so `xargs *`, `env *`, `timeout *` and `*=*` refused `xargs grep -l gh`, `env CI=1 rg gh src`,
# `timeout 300 pytest -k gh` and `pytest --tb=short -k gh`, and as the hard limits come last,
# nothing Claude or the user allowed could lift them (#442, the audit of 1.8). `?` matches one
# character (a space too), so each shape starts with the wrapper's literal word or `-` and spans
# at most three: a count, a duration, a niceness, a buffering mode. A value it can't bound (a
# variable's) is left to the kernel, as above.
_WRAPPERS = ("env", "env -i", "env --", "xargs", "xargs -0", "xargs -r", "xargs -0r", "xargs -r0",
             "xargs --null", "xargs --no-run-if-empty", "xargs -??", "xargs -???", "xargs -? ?",
             "xargs -I {}", "xargs -I{}", "nohup", "nohup --", "nice", "nice -?", "nice -??",
             "nice -???", "nice -n ?", "nice -n ??", "nice -n ???", "time", "time -p", "time --",
             "command", "command -p", "command --", "exec", "timeout ?", "timeout ??",
             "timeout ???", "stdbuf -??", "stdbuf -?? -??", "stdbuf -?? -?? -??", "setsid")


def _denied(commands) -> tuple[str, ...]:
    # `sudo *` matches a bare `sudo` too (OpenCode makes a trailing " *" optional): one rule each,
    # as the rules travel in one environment string (runner.CONFIG_CONTENT_MAX)
    return tuple(f"{c} *" for c in commands) + \
        tuple(f"{w} {c} *" for w in _WRAPPERS for c in commands)


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
    # the toolchains the lane makes room for (.pub-cache, hatch, virtualenvs, .platformio,
    # .conan2) and wb doctor checks, and the common runners and linters: `flutter test` was
    # refused though Claude chose it as the acceptance command (#514, the audit of 1.90).
    # `python3.*`, `pip3.*`: a pinned interpreter (`python3.12 -m pytest`)
    "python3.*", "pip3.*", "pypy3", "pdm", "pipenv", "hatch", "nox", "pixi", "conda", "mamba",
    "micromamba", "pre-commit", "pyright", "flutter", "dart", "bazel", "bazelisk", "just", "task",
    "pio", "platformio", "conan", "xcodebuild", "xcrun", "rubocop", "golangci-lint", "shellcheck",
)
# A project's own tools and builds, by their usual relative paths (`./x` is allowed already).
PROJECT_BINS = (".venv/bin/*", "venv/bin/*", "node_modules/.bin/*", "build/*", "target/debug/*",
                "target/release/*", "bin/*", "out/*", "dist/*", "zig-out/bin/*", "vendor/bin/*",
                "scripts/*", "tools/*")
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
    "pip3.* install *", "python3.* -m pip install *", "pypy3 -m pip install *", "pdm add *",
    "pipenv install *", "pixi add *", "pixi global install *", "conda install *",
    "mamba install *", "micromamba install *", "flutter pub add *", "dart pub add *",
    "dart pub global activate *", "flutter pub global activate *", "pio pkg install *",
    "platformio pkg install *", "conan install *--requires*",
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
    "pip3.* install -r *", "pip3.* install -e .", "python3.* -m pip install -r *",
    "python3.* -m pip install -e .", "pipenv install", "pipenv install --dev", "pipenv sync",
    "pio pkg install", "platformio pkg install",
)
# The pip forms above are allowed only when they install into the worktree's own venv, or no
# environment is active and Landlock bounds pip (python_installs); else they ask first. These,
# which change the active venv, ask first while it is the user's (#490)
ENV_INSTALLS = ("poetry install *", "poetry sync *", "poetry remove *", "uv sync --active *",
                "uv pip sync *", "uv pip uninstall *", "pip uninstall *", "pip3 uninstall *",
                "python -m pip uninstall *", "python3 -m pip uninstall *")


def allowed_patterns(task: store.Task) -> list[str]:
    """What this task's agent may run beyond ALLOWED_COMMANDS: the user's standing allowances
    (commands.json `allow`; "*" turns the list off) and what Claude granted this task."""
    return [*commands_conf().get("allow", []), *(grants(task).get("allow") or [])]


def _simple_commands(line: str) -> list[str]:
    """The simple commands of a shell line as OpenCode checks them, each its raw text: split at
    `&&`, `||`, `;`, `|`, `&` and newlines outside quotes, a subshell's parentheses dropped."""
    parts, cur, quote, i = [], "", None, 0
    while i < len(line):
        ch = line[i]
        if quote:
            quote = None if ch == quote else quote
            cur += ch
        elif ch in "'\"":
            quote, cur = ch, cur + ch
        elif ch == "\\" and i + 1 < len(line):
            cur, i = cur + line[i:i + 2], i + 1
        elif ch in ";|&\n":
            parts.append(cur)
            cur = ""
        else:
            cur += ch
        i += 1
    parts.append(cur)
    return [p.strip().strip("()").strip() for p in parts if p.strip().strip("()").strip()]


def test_allowances(test: str | None) -> list[str]:
    """What `wb start --test` and `wb next --test` grant the task: the acceptance command's own
    simple commands, as written and as `<their command> *`. The protocol tells the agent to run
    it; refused (`flutter test`, `just test`, `scripts/test.sh`), it spent an ask and a Claude
    wake on a command Claude chose, or finished untested (#514, the audit of 1.90). The hard
    limits come later in the rules, so a grant here never lifts them."""
    import shlex
    out = []
    for cmd in _simple_commands(test or ""):
        out.append(cmd)
        try:
            words = shlex.split(cmd)
        except ValueError:
            continue
        words = [w for w in words if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", w)]
        if words and "*" not in words[0]:
            out += [f"{words[0]} *", f"{ASSIGN} {words[0]} *"]
    return list(dict.fromkeys(out))


def commands_path() -> Path:
    return Path(os.environ.get("WB_COMMANDS") or Path.home() / ".config" / "workbench" / "commands.json")


def allow_always(key: str, items: list[str]) -> None:
    """Add ``items`` to commands.json's ``key`` ("allow" or "paths"), for every task (`wb allow
    --always`): under its lock and through a link, the file checked first (commands_conf), since
    rewriting one that doesn't parse would lose it. Unlocked, two sessions' grants close together
    lost one; written with write_json, a dotfiles link became a plain 0644 file (#495)."""
    with store.edit_config(commands_path(), commands_conf) as conf:
        have = conf.get(key) or []
        conf[key] = have + [x for x in items if x not in have]


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
    repos = conf.get("repos")
    if repos is not None and not isinstance(repos, dict):
        raise RuntimeError(f'{path}: "repos" must map a repo\'s path to its settings, e.g. '
                           '{"~/src/app": {"setup": "npm ci"}}')
    for repo, v in (repos or {}).items():
        ok = isinstance(v, dict) and isinstance(v.get("setup", ""), str) and all(
            isinstance(v.get(k, []), list) and all(isinstance(x, str) for x in v.get(k, []))
            for k in ("copy", "siblings"))
        if not ok:
            raise RuntimeError(f'{path}: "repos" → {repo!r}: "setup" is a command, "copy" and '
                               '"siblings" lists of strings')
        if v.get("merge", "merge") not in MERGE_AS:
            raise RuntimeError(f'{path}: "repos" → {repo!r}: "merge" is one of {", ".join(MERGE_AS)}')
    return conf


# Experiments: a change to what the agent may do, not yet measured enough to be supported
# (ARCHITECTURE.md, *Experiments*). Each is off unless the user turns it on: `wb experiment <name> on|off`
# sets it (experiments.json), and `wb start` records its value per task (task.json `experiments`),
# so a flip changes only the tasks started after it. Off, the agent's rules are main's exactly.
EXPERIMENTS = {
    "probe": "the agent's shell may run probe, and its handbook says how: a file's outline and "
             "one function, instead of the whole file",
}
# what an experiment adds to the agent's copy of the handbook, after its own text
EXPERIMENT_NOTES = {
    "probe": "\n## Reading code\nRead a whole file only when you need most of it. `probe symbols "
             "<file>` lists its functions with line numbers; `probe extract <file>#<name>` prints "
             "one; after an edit, re-read only the function you changed.\n",
}


def experiments_path() -> Path:
    return store.home() / "experiments.json"


def experiments() -> dict[str, bool]:
    """Each experiment's setting: off unless experiments.json says "on"."""
    saved = store.read_json(experiments_path(), {}) or {}
    saved = saved if isinstance(saved, dict) else {}
    return {name: saved.get(name) == "on" for name in EXPERIMENTS}


def set_experiment(name: str, on: bool) -> None:
    with store.locked(experiments_path().with_suffix(".lock")):
        saved = store.read_json(experiments_path(), {}) or {}
        saved = saved if isinstance(saved, dict) else {}
        store.write_json(experiments_path(), {**saved, name: "on" if on else "off"})


def probe_bin() -> str | None:
    """probe on the agent's PATH (the runner's, which the agent's shell inherits), when it has
    `symbols`, else None. crates.io's probe-code 0.6.0 hasn't, and says the same version as the
    release that has: `probe symbols main.c` searched for the word, the agent got no results and
    wrote a function named `symbols` to match (2026-11-20)."""
    found = shutil.which("probe", path=agent_path(os.environ.get("PATH") or os.defpath))
    if not found:
        return None
    try:  # any bytes: a `probe` that isn't probelabs' may print anything (#518)
        out = subprocess.run([found, "--help"], capture_output=True, text=True, errors="replace",
                             timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    return found if re.search(r"^\s+symbols\s", out, re.M) else None


# What probe is and where its release binary is: wb experiment, wb doctor and the pane's note to
# Claude name both (the user, 2026-11-20: "indicate somehow ... what the hell probe is")
PROBE_HOME = "github.com/probelabs/probe"
PROBE_RELEASES = "github.com/probelabs/probe/releases"


def task_experiments() -> dict[str, bool]:
    """What `wb start` records: each experiment on only when set on and able to work (probe
    installed), so a task's record says what its agent really had. An experiment that is off
    isn't checked at all: a `probe` on PATH ran on every start with it off, and one printing a
    non-UTF-8 byte failed the start after its branch and worktree were made (#518)."""
    able = {"probe": probe_bin}
    return {name: on and (able[name]() is not None if name in able else True)
            for name, on in experiments().items()}


def experiment(task: store.Task, name: str) -> bool:
    return bool((task.spec.get("experiments") or {}).get(name))


# How wb accept --merge lands: a merge commit, fast-forward only, or one squashed commit (#352)
MERGE_AS = ("merge", "ff", "squash")


def repo_conf(repo: str | Path) -> dict:
    """commands.json's `repos` entry for this repo (matched by real path), or {}: its `setup`
    command, the ignored paths to `copy` into a task's worktree, its `siblings` (#201), how
    `--merge` lands there (`merge`, #352)."""
    real = os.path.realpath(repo)
    for k, v in (commands_conf().get("repos") or {}).items():
        if os.path.realpath(Path(k).expanduser()) == real:
            return v
    return {}


def shell_rules(task: store.Task) -> list[dict]:
    """Deny by default, allow the everyday tools, ask before new dependencies, then what Claude
    and the user allowed; the hard limits last, so no allowance lifts them (later rules win)."""
    rules = [{"action": "shell", "resource": "*", "effect": "deny"}]
    rules += [{"action": "shell", "resource": r, "effect": "allow"}
              for c in ALLOWED_COMMANDS for r in (f"{c} *", f"{ASSIGN} {c} *")]  # `c *` is `c` too
    if experiment(task, "probe"):  # read-only: an outline, one function, a search
        rules += [{"action": "shell", "resource": r, "effect": "allow"}
                  for r in ("probe", "probe *", f"{ASSIGN} probe *")]
    rules += [{"action": "shell", "resource": r, "effect": "allow"}
              for b in ("./*", "../*", *PROJECT_BINS) for r in (b, f"{ASSIGN} {b}")]
    # and what it built anywhere in its lane, by path: a debug binary in the temp directory was
    # refused (2026-11-16) though `bash` could run it; the lane, not this list, bounds writes
    rules += [{"action": "shell", "resource": f"{d}/*", "effect": "allow"}
              for d in (writable(task) or []) if os.path.isdir(d)
              and not d.startswith(("/dev", "/proc"))]
    pip, theirs = python_installs(task)  # into the worktree's venv, not the user's (#490)
    rules += [{"action": "shell", "resource": r, "effect": "deny"}
              for p in (*ASK_FIRST, *(ENV_INSTALLS if theirs else ())) for r in (p, f"{ASSIGN} {p}")]
    rules += [{"action": "shell", "resource": r, "effect": "allow"}
              for p in INSTALL_LISTED if pip or "pip" not in p for r in (p, f"{ASSIGN} {p}")]
    rules += [{"action": "shell", "resource": r, "effect": "allow"} for r in allowed_patterns(task)]
    rules += [{"action": "shell", "resource": c, "effect": "deny"} for c in denied_shell()]
    return rules


# The agent's lane: its worktree, the temp directory, and the caches and toolchain homes that
# builds and package managers fill, so a build or test runs without asking (2026-11-16, the user:
# keep agents in their lane with the least friction). Writes anywhere else are refused by the
# kernel (lane.py), file tools and the shell's working directory are kept to the lane, the
# READABLE places and the installs it runs (read_only, never edited) by OpenCode's
# `external_directory` rule, and the agent asks Claude for the rest (`wb allow --path`). Reading through the shell stays free: `cat`, a compiler, an interpreter.
# A toolchain's caches, not the directories on the user's PATH or in their shell's startup: a file
# the agent plants there runs later outside the lane (the 2026-11-16 audit: ~/.local/lib's user
# site-packages, whose .pth files every python runs; ~/.cargo/bin; GOPATH holding the repo itself).
# Nor uv's tools and Pythons (~/.local/share/uv: every `uv run` and `uv tool` program runs from
# there; its cache is ~/.cache/uv) or rustup's toolchains (what ~/.cargo/bin's proxies exec), the
# 0.90 review. The rest is a trade, documented: a home kept whole (.dotnet, .ghcup, .julia,
# .cache with pre-commit's hooks and poetry's virtualenvs) can hold a program the user's own tools
# run later, and splitting it would refuse the first new entry every build makes there.
TOOL_HOMES = (".cache", ".cargo", ".rustup", "go/pkg", ".npm", ".bun/install/cache", ".yarn",
              ".pnpm-store", ".local/share/pnpm/store", ".gradle", ".m2",
              ".nuget", ".dotnet", ".gem", ".julia", ".cabal", ".stack", ".ghcup", ".platformio",
              ".conan2", ".hex", ".mix", ".sbt", ".ivy2", ".pub-cache", ".konan",
              ".local/share/hatch", ".local/share/virtualenvs",
              # NuGet's http and plugins caches, Cabal 3.10's store without ~/.cabal, and
              # Android's debug keystore, its adbkey hidden (SECRETS): each build failed (#437)
              ".local/share/NuGet", ".local/state/cabal", ".android")
# where the user moved them: a toolchain follows its variable, not the default path; GOPATH and
# BUN_INSTALL name a whole home (GOPATH may hold every project), so only their caches
TOOL_HOME_VARS = ("CARGO_HOME", "RUSTUP_HOME", "GOPATH", "GOMODCACHE", "GOCACHE", "npm_config_cache",
                  "PIP_CACHE_DIR", "UV_CACHE_DIR", "GRADLE_USER_HOME", "BUN_INSTALL", "DENO_DIR",
                  "MIX_HOME", "HEX_HOME", "PUB_CACHE", "KONAN_DATA_DIR", "HATCH_DATA_DIR",
                  "WORKON_HOME", "CARGO_TARGET_DIR", "NPM_CONFIG_CACHE", "NUGET_PACKAGES")
# The SDKs a build reads by variable, not from PATH: with only Android's platform-tools on PATH,
# Gradle couldn't open ~/Android/Sdk/platforms' android.jar, and every build and test failed
# (#437, the audit of 1.8). Their targets are readable, and so are the SDKs' default places.
SDK_VARS = ("ANDROID_HOME", "ANDROID_SDK_ROOT", "JAVA_HOME", "FLUTTER_ROOT", "DOTNET_ROOT")
SDK_DEFAULTS = ("Android/Sdk", "Library/Android/sdk")
VAR_CACHES = {"GOPATH": "pkg", "BUN_INSTALL": "install/cache"}
# inside a toolchain home in the lane, what stays out of it: the programs on the user's PATH and
# what their shell sources, and the toolchains rustup's proxies run (relative to the home, or to
# CARGO_HOME and RUSTUP_HOME for theirs); a toolchain a rust-toolchain.toml names that isn't
# installed yet asks first (`wb allow --path`)
NOT_WRITABLE = (".cargo/bin", ".cargo/env", ".dotnet/tools", ".cabal/bin", ".ghcup/bin",
                ".rustup/toolchains")
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
# A GPU's device nodes, for a task started with --unload-for-tests: its tests use the GPU, and
# without these CUDA said "no CUDA-capable device", ROCm hipErrorNoDevice, Vulkan "no physical
# device", none of them "permission", so the agent spent its auto-fix rounds on what it read as
# its own bug (#331, the audit of 1.4). Render nodes only of /dev/dri (not card*, the display's
# modesetting), ROCm's /dev/kfd, CUDA's nodes, WSL2's /dev/dxg
GPU_NODES = ("/dev/dri/renderD*", "/dev/kfd", "/dev/nvidia[0-9]*", "/dev/nvidiactl",
             "/dev/nvidia-uvm", "/dev/nvidia-uvm-tools", "/dev/dxg")


def gpu_nodes(task: store.Task) -> list[str]:
    """The GPU's device nodes that exist, when the task's tests need the GPU (unload_for_tests)."""
    if not task.spec.get("unload_for_tests"):
        return []
    return sorted({n for pat in GPU_NODES for n in glob.glob(pat)})


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
             chat.token_path(), chat.config_path(), models.overrides_path(), images.config_path(),
             audio.config_path()]
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


# What a task's directory holds beside its worktree: a sibling's link (`../<name>` from the
# worktree) may not take one of these names. A new file in the task directory goes here too.
TASK_FILES = ("acked", "ask", "base-tests.txt", "context.md", "events.jsonl", "rounds",
              "runner.log", "setup.txt", "shots", "status.json", "steer.jsonl", "task.json",
              "tree", "verify", "worktree")


def sibling_parts(s: str) -> tuple[int, str]:
    """A sibling as (levels above the repo, the path below there): `lib` is `../lib`, one up;
    `../../shared/lib` is two up (#261, the audit of 1.2)."""
    parts, ups = s.split("/"), 0
    while parts and parts[0] == "..":
        parts, ups = parts[1:], ups + 1
    return max(ups, 1), "/".join(parts)


def sibling_link(worktree: Path, s: str) -> Path:
    """Where a sibling's link goes, so that the worktree reaches it by the repo's own path."""
    ups, rest = sibling_parts(s)
    return Path(worktree).parents[ups - 1] / rest


def sibling_real(repo, s: str) -> str:
    ups, rest = sibling_parts(s)
    return os.path.realpath(Path(repo).parents[ups - 1] / rest)


def tree(base: Path, repo, sibs, leaf: str) -> Path:
    """Where a checkout of `repo` goes under `base`, so that every sibling's link lands inside
    `base`: `base/leaf` while none is more than one level up, else `base/tree/<the names of the
    repo's own parents>/leaf`. Only `../<name>` resolved: Cargo's `path = "../../shared/lib"`, a
    workspace two levels up, Go's `replace ../../x` broke with nothing to configure (#261)."""
    ups = max((sibling_parts(s)[0] for s in sibs or ()), default=1)
    if ups <= 1:
        return Path(base) / leaf
    return Path(base).joinpath("tree", *[p.name for p in Path(repo).parents[:ups - 1]][::-1], leaf)


def siblings(task: store.Task) -> list[str]:
    """The real paths of the directories around the repo the task reads by a relative path (#201:
    Cargo's `path = "../lib"`, Go's `replace ../lib`, npm's `file:../lib` broke in a worktree
    under the state directory). Read-only: the agent's lane reads them, never writes them."""
    repo = task.spec.get("repo")
    return [sibling_real(repo, n) for n in task.spec.get("siblings") or () if repo]


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
            if os.path.isabs(first):  # a relative CARGO_TARGET_DIR is the build's own, in the repo
                xdg.append(os.path.join(first, VAR_CACHES[v]) if v in VAR_CACHES else first)
    # OpenCode's own directories, made first: the lane writes only what exists, so a first task
    # in a fresh profile failed opening OpenCode's log before any model request (#340, the audit
    # of 1.4). One that can't be made is named in runner.log, the place a failed round points to.
    made = [Path(os.environ.get(v) or home / d) / "opencode"
            for v, d in (("XDG_DATA_HOME", ".local/share"), ("XDG_STATE_HOME", ".local/state"),
                         ("XDG_CACHE_HOME", ".cache"), ("XDG_CONFIG_HOME", ".config"))]
    for d in made:
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            print(f"workbench: can't make OpenCode's directory {d} ({e.strerror}): "
                  f"its first run fails without it", file=sys.stderr)
    places = [tempfile.gettempdir(), *TEMP_ROOTS, *DEV_NODES, *gpu_nodes(task), "/proc",
              *(home / h for h in TOOL_HOMES + OPENCODE_HOMES), *(x for x in xdg if x),
              *flutter_caches(),
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
    rustup = os.environ.get("RUSTUP_HOME")
    for r in {os.path.realpath(r) for r in (home / ".rustup", rustup) if r}:
        if os.path.isdir(r):  # made, so it is split out: else the agent could make it and plant one
            try:
                os.makedirs(os.path.join(r, "toolchains"), exist_ok=True)
            except OSError:
                pass
    # the user's active Python environments too: a venv in a tool home (poetry's, pipenv's,
    # WORKON_HOME) was the agent's to install into (#490)
    out_of = _hidden() | set(python_envs()) | \
        {os.path.realpath(p) for p in (*(home / h for h in NOT_WRITABLE),
                                       *((Path(cargo) / "bin", Path(cargo) / "env") if cargo else ()),
                                       *((Path(rustup) / "toolchains",) if rustup else ()))
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


def flutter_caches() -> list[str]:
    """Flutter's `bin/cache`, for $FLUTTER_ROOT and each `flutter` on PATH: every flutter command
    opens bin/cache/lockfile for writing, so in the lane each one exited (#437, the audit of 1.8).
    Kept whole, like .dotnet: the Dart SDK the user's flutter runs later lives there."""
    roots = [os.environ.get("FLUTTER_ROOT")]
    for d in (os.environ.get("PATH") or "").split(os.pathsep):
        f = os.path.join(d, "flutter")
        if os.path.isabs(d) and os.path.isfile(f):
            roots.append(os.path.dirname(os.path.dirname(os.path.realpath(f))))
    caches = (os.path.join(r, "bin", "cache") for r in roots if r and os.path.isabs(r))
    return list(dict.fromkeys(c for c in caches if os.path.isdir(c)))


def sdk_roots(hidden: set[str]) -> set[str]:
    """The SDKs' homes (SDK_VARS, SDK_DEFAULTS) that exist, real paths: never `/`, the home
    directory or a place holding or inside a secret."""
    home = os.path.realpath(Path.home())
    paths = [os.environ.get(v) for v in SDK_VARS] + [str(Path(home) / d) for d in SDK_DEFAULTS]
    real = {os.path.realpath(p) for p in paths if p and os.path.isabs(p) and os.path.isdir(p)}
    return {r for r in real if r not in ("/", home)
            and not any(h == r or _under(h, r) or _under(r, h) for h in hidden)}


# the toolchains `wb doctor` checks for snaps
TOOLCHAINS = ("go", "node", "npm", "npx", "yarn", "pnpm", "dotnet", "java", "cmake", "flutter",
              "dart", "kotlin", "cargo", "rustc", "python3", "opencode")


def home_gap() -> str:
    """A line when the home directory lies in a temp root, which every lane writes whole (see
    writable): the credentials there are then the agent's to read. Seen in sandboxes that put the
    profile under /tmp (the 2026-11-19 audit), else ""."""
    home = os.path.realpath(Path.home())
    temps = {os.path.realpath(t) for t in (tempfile.gettempdir(), *TEMP_ROOTS)}
    inside = next((t for t in sorted(temps) if _under(home, t)), None)
    return (f"your home directory {home} is inside {inside}, which every agent's lane writes "
            "whole: its credentials there are readable to agents; give this user a home outside "
            "it") if inside else ""


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


# System sockets that hold nothing of the user's, which plain programs connect to: the log
# (logger, syslog) and name and user lookups (resolved, userdb for systemd-homed users, nscd,
# sssd). The files alone, never their directories (sssd's pipes/private stays out), and not the
# system bus, which reaches polkit's actions (the 0.90 review: `logger` and `resolvectl` failed).
SYSTEM_SOCKETS = ("/dev/log", "/run/systemd/journal/socket", "/run/systemd/journal/stdout",
                  "/run/systemd/resolve/io.systemd.Resolve",
                  "/run/systemd/userdb/io.systemd.Multiplexer", "/run/nscd/socket",
                  "/var/lib/sss/pipes/nss")


def unix_paths(task: store.Task) -> list[str]:
    """Where the agent may connect to a UNIX socket made outside its lane (Landlock ABI 9): the
    SYSTEM_SOCKETS there are, and what the user and Claude added (`paths`; `wb allow --path
    /run/postgresql` for a test database), never the session bus or the user's agents, split
    around secrets like the reads (the 0.90 review: a GPG agent socket under a granted ~/.config
    was in reach)."""
    hidden = _hidden()
    return _both([*(s for s in SYSTEM_SOCKETS if os.path.exists(s)),
                  *(q for p in (Path(r).expanduser() for r in lane_paths(task) if r != "*")
                    if os.path.exists(p) for q in _split(str(p), hidden))])


def ask_dir(task: store.Task) -> Path:
    """Where the agent's questions go (ask_mcp.py): the one place in its task directory that
    its lane may write."""
    d = task.dir / "ask"
    if task.dir.is_dir():
        d.mkdir(exist_ok=True)
    return d


def _runtime_base() -> Path:
    return Path("/tmp") if os.path.isdir("/tmp") else Path(tempfile.gettempdir())


def _runtime_path(task: store.Task) -> Path:
    tag = hashlib.sha256(f"{store.home()}\0{task.name}".encode()).hexdigest()[:12]
    return _runtime_base() / f"wb-run-{os.getuid()}-{tag}"


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
        d = Path(tempfile.mkdtemp(prefix="wb-run-"))
    if not (d / RUNTIME_OWNER).exists():
        store.write_json(d / RUNTIME_OWNER, {"home": str(store.home()), "task": task.name})
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


STALE_RUNTIME_DAYS = 7
RUNTIME_OWNER = ".wb-owner"  # {home, task}: whose runtime directory this is


def _owner(d: Path) -> dict | None:
    """A runtime directory's owner mark, or None. The directory is in the agent's lane: a FIFO
    there would hang the read, so only a regular file counts."""
    try:
        if not stat.S_ISREG(os.lstat(d / RUNTIME_OWNER).st_mode):
            return None
        owner = store.read_json(d / RUNTIME_OWNER, None)
    except OSError:
        return None
    return owner if isinstance(owner, dict) else None


def drop_stale_runtime_dirs(tasks) -> int:
    """Remove this profile's runtime directories (``wb-run-*``) untouched for STALE_RUNTIME_DAYS
    whose task (of ``tasks``, this profile's) is closed or gone: a task closed while its runner
    was killed, a fallback mkdtemp left them behind, 13 of them here (the 2026-11-19 audit). Each
    names its owner (RUNTIME_OWNER): any wb-run-* not this profile's open tasks' went, a second
    profile's task nine days in review lost its own, and so did a wb-run-user-notes wb never made
    (#243, the audit of 1.2). One made before the mark goes only as a closed task's of this
    profile, by its name. Returns how many went."""
    tasks = list(tasks)
    live = {t.name for t in tasks if t.status.get("phase") not in store.CLOSED}
    closed = {_runtime_path(t) for t in tasks if t.name not in live}
    home = str(store.home())
    cutoff, n = time.time() - STALE_RUNTIME_DAYS * 86400, 0
    for d in _runtime_base().glob("wb-run-*"):
        try:
            st = os.lstat(d)
        except OSError:
            continue
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mtime > cutoff:
            continue
        owner = _owner(d)
        if owner is None:
            if d not in closed:
                continue
        elif owner.get("home") != home or owner.get("task") in live:
            continue
        shutil.rmtree(d, ignore_errors=True)
        n += 1
    return n


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
           ".config/microsoft-edge", ".config/opera", ".android/adbkey")
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
# WSL2's Windows drives (DrvFs, /mnt/<letter> unless wsl.conf moved them): a Windows program there
# runs through interop as a Windows process, as the user, outside Landlock and no-new-privileges.
# The read lane let the agent start one: python3 running cmd.exe, or the allowed `npm` resolving to
# a Windows-only Node (the 0.90 review). So on WSL they are left out of the agent's PATH
# (agent_path) and of tool_roots: out of the read lane, the exec itself is refused, since execve
# opens the program for reading. A Windows program inside the lane still runs (`wb doctor` says
# so, interop_gap): only turning interop off closes that.
MOUNTS = "/proc/self/mounts"
DRIVE_LETTER = re.compile(r"/mnt/[a-zA-Z](/|$)")
WINDOWS_DEVICE = re.compile(r"[a-zA-Z]:")
BINFMT = "/proc/sys/fs/binfmt_misc"


def windows_drives() -> list[str] | None:
    """On WSL, where the Windows drives are mounted (DrvFs, from the mount table), real paths;
    None elsewhere."""
    if hardware.system() != "wsl":
        return None
    try:
        lines = Path(MOUNTS).read_text().splitlines()
    except OSError:
        lines = []
    out = []
    for line in lines:
        f = line.split()  # a space in a mount point is \040; "C:\134" is C:\ (9p, drvfs, ...)
        if len(f) >= 4 and (f[2] == "drvfs" or "aname=drvfs" in f[3] or WINDOWS_DEVICE.match(f[0])):
            out.append(os.path.realpath(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), f[1])))
    return out


def on_windows(path: str, drives: list[str] | None) -> bool:
    """Whether `path` is on a Windows drive (`drives`: windows_drives(); None off WSL)."""
    if drives is None:
        return False
    real = os.path.realpath(path)
    return bool(DRIVE_LETTER.match(real)) or any(real == d or _under(real, d) for d in drives)


def agent_path(path: str) -> str:
    """The agent's PATH: on WSL, without the Windows drives' directories (WSL appends the
    Windows PATH: System32, Git for Windows, a Windows Node)."""
    drives = windows_drives()
    if drives is None:
        return path
    return os.pathsep.join(d for d in path.split(os.pathsep) if not (d and on_windows(d, drives)))


# A Python environment the user activated (a venv, a conda env, direnv's `layout python`, `poetry
# shell`) reached the agent with its PATH. When it was the checkout's own (inside the repo, an
# editable install of it, or in a per-project venv home), the agent, the setup, the base check and
# the tests imported the user's checkout instead of the worktree: a test the agent added failed in
# every auto-fix round, or a review said the tests passed on code they never imported; and the
# agent's allowed `pip install -e .` repointed the user's venv at the worktree, which accept or
# discard then deleted (#490, the audit of 1.90). Such an env stays out of the agent's environment
# (python_env); the worktree's own venv, the setup's `uv sync`, comes first on its PATH.
VENV_VARS = {"VIRTUAL_ENV": ("VIRTUAL_ENV", "VIRTUAL_ENV_PROMPT"),
             "CONDA_PREFIX": ("CONDA_PREFIX", "CONDA_DEFAULT_ENV", "CONDA_PROMPT_MODIFIER")}
# where poetry, pipenv, virtualenvwrapper and hatch keep one venv per project (under the home, or
# where their variable moved them): writable tool homes in the lane, so the agent's installs
# would land there even with Landlock
VENV_HOMES = (".cache/pypoetry/virtualenvs", "Library/Caches/pypoetry/virtualenvs",
              ".local/share/virtualenvs", ".virtualenvs", ".local/share/hatch/env",
              "Library/Application Support/hatch/env")
VENV_HOME_VARS = {"WORKON_HOME": "", "POETRY_VIRTUALENVS_PATH": "",
                  "POETRY_CACHE_DIR": "virtualenvs", "HATCH_DATA_DIR": "env"}
# what in a site-packages names an installed project's source: .pth lines, setuptools' editable
# finders, the old develop's egg-links, pip's direct_url.json
EDITABLE_FILES = ("*.pth", "__editable__*", "*.egg-link", "*.dist-info/direct_url.json")


def python_envs(environ=None) -> list[str]:
    """The Python environments active in ``environ`` (the runner's by default), real paths:
    VIRTUAL_ENV, CONDA_PREFIX and each PATH directory that is a venv's or conda env's `bin`."""
    env = os.environ if environ is None else environ
    roots = [env.get("VIRTUAL_ENV"), env.get("CONDA_PREFIX")]
    for d in (env.get("PATH") or "").split(os.pathsep):
        p = Path(d)
        if p.is_absolute() and p.name == "bin" and ((p.parent / "pyvenv.cfg").is_file()
                                                   or (p.parent / "conda-meta").is_dir()):
            roots.append(str(p.parent))
    return list(dict.fromkeys(os.path.realpath(r) for r in roots if r and os.path.isdir(r)))


def _in_repo(path: str, repo: str) -> bool:
    """``path`` in the repo, unless the repo holds the home directory (a dotfiles repo at ~)."""
    home = os.path.realpath(Path.home())
    return not (home == repo or _under(home, repo)) and (path == repo or _under(path, repo))


def checkout_env(root: str, repo: str) -> str:
    """Why the environment at ``root`` is the user's checkout's (``repo``, a real path): ''
    when it isn't."""
    if _in_repo(root, repo):
        return "inside the repo"
    homes = [Path.home() / h for h in VENV_HOMES]
    homes += [Path(os.environ[v]).expanduser() / sub for v, sub in VENV_HOME_VARS.items()
              if os.environ.get(v)]
    if any(_under(root, os.path.realpath(h)) for h in homes):
        return "a per-project venv"
    names = {repo, os.path.abspath(repo)}
    names |= {urllib.parse.quote(n) for n in names}
    found = re.compile("|".join(map(re.escape, sorted(names))) + r"(?=$|[/\\'\"\s,}])", re.M)
    sites = [*glob.glob(os.path.join(glob.escape(root), "lib", "*", "site-packages")),
             os.path.join(root, "lib", "site-packages")]
    for site in sites:
        for pattern in EDITABLE_FILES:
            for f in glob.glob(os.path.join(glob.escape(site), pattern)):
                try:
                    with open(f, encoding="utf-8", errors="replace") as fh:
                        text = fh.read(1 << 16)
                except OSError:
                    continue
                lines = (ln.strip() for ln in text.splitlines()) if f.endswith(".pth") else ()
                if found.search(text) or any(  # a .pth line through a link
                        ln and not ln.startswith(("#", "import")) and os.path.isabs(ln)
                        and _in_repo(os.path.realpath(ln), repo) for ln in lines):
                    return "an editable install of the repo"
    return ""


def checkout_envs(task: store.Task, environ=None) -> dict[str, str]:
    """The active Python environments that are the user's checkout's, each with why."""
    repo = os.path.realpath(task.spec["repo"])
    return {r: why for r in python_envs(environ) if (why := checkout_env(r, repo))}


def own_venv(task: store.Task) -> Path | None:
    """The worktree's own virtualenv (`.venv` or `venv`, in the package or at the top: the
    setup's `uv sync` or `python3 -m venv .venv`), else None."""
    for d in dict.fromkeys((task.workdir, task.worktree)):
        for name in (".venv", "venv"):
            if (d / name / "pyvenv.cfg").is_file() and not (d / name).is_symlink():
                return d / name
    return None


def python_env(task: store.Task, env: dict) -> list[str]:
    """``env``, the agent's, without the user's checkout's Python environments (checkout_envs):
    their variables and their `bin` directories off PATH; the PATH directories inside the repo
    as the worktree's (`node_modules/.bin`, a direnv PATH_add); the worktree's own venv first,
    as VIRTUAL_ENV. In place; the PATH entries now inside the worktree, which the agent writes
    and the runner's own lookups (the git shim's) must not use."""
    repo = os.path.realpath(task.spec["repo"])
    dropped = list(checkout_envs(task, env))
    gone = lambda p: any(p == r or _under(p, r) for r in dropped)  # noqa: E731
    for var, names in VENV_VARS.items():
        if env.get(var) and gone(os.path.realpath(env[var])):
            for n in names:
                env.pop(n, None)
    if env.get("PYTHONHOME") and (gone(home := os.path.realpath(env["PYTHONHOME"]))
                                  or _in_repo(home, repo)):
        env.pop("PYTHONHOME")
    path, inside = [], []
    for d in (env.get("PATH") or "").split(os.pathsep):
        real = os.path.realpath(d) if os.path.isabs(d) else ""
        if real and gone(real):
            continue
        if real and _in_repo(real, repo):
            d = os.path.normpath(os.path.join(task.worktree, os.path.relpath(real, repo)))
            inside.append(d)
        path.append(d)
    if own := own_venv(task):
        inside.append(str(own / "bin"))
        path.insert(0, str(own / "bin"))
        env["VIRTUAL_ENV"] = str(own)
        env.pop("VIRTUAL_ENV_PROMPT", None)
    env["PATH"] = os.pathsep.join(path)
    return inside


def python_installs(task: store.Task) -> tuple[bool, bool]:
    """Whether the agent may install what the project lists with pip (`pip install -e .`, `-r`)
    without asking: into the worktree's own venv, or with none active (the user's checkout's are
    dropped, python_env) while Landlock bounds what pip writes; else pip would write the user's
    own environment (a conda env, a tools venv), or their Python without the lane (#490). And
    whether the active venv the agent's shell has is the user's (ENV_INSTALLS ask first)."""
    if own_venv(task):
        return True, False
    kept = [r for r in python_envs() if r not in checkout_envs(task)]
    return not kept and writable(task) is not None and lane.abi() > 0, bool(kept)


def venv_line(task: store.Task, setup: bool) -> str:
    """For wb start: the active environments the agent leaves out, and how to give it its own."""
    envs = checkout_envs(task)
    if not envs:
        return ""
    said = "; ".join(f"{r} ({why})" for r, why in envs.items())
    line = (f"the active Python environment {said} is your checkout's: the agent, its setup and "
            "its tests leave it out, so they import the worktree's code")
    if not setup:
        line += ("; --setup 'uv sync' --test 'uv run pytest' (or the project's own install) gives "
                 "the worktree its own .venv, first on their PATH")
    return line


def interop_gap() -> str | None:
    """What WSL's interop leaves open, for `wb doctor`, or None (not WSL, or interop off)."""
    if hardware.system() != "wsl":
        return None
    for name in ("WSLInterop", "WSLInterop-late"):
        try:
            if Path(BINFMT, name).read_text().split("\n")[0].strip() == "enabled":
                break
        except OSError:
            continue
    else:
        return None
    return ("WSL interop is on: a Windows program runs as you outside the agent's lane. The "
            "Windows drives are out of its PATH and reads, but a Windows program in its lane or "
            "repo still runs; [interop] enabled=false in /etc/wsl.conf (then wsl --shutdown) "
            "closes it")


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
    or anything holding a secret: a prefix is kept whole; never a Windows drive on WSL."""
    home = os.path.realpath(Path.home())
    prefix = lambda d: os.path.dirname(d) if os.path.basename(d) == "bin" else d  # noqa: E731
    roots = {os.path.realpath(r) for r in ONLY_KEPT}
    drives = windows_drives()

    def opens(p: str) -> bool:  # under an ONLY_KEPT root, deeper than a home or a drive
        for root in roots:
            if _under(p, root):
                parts = p[len(root):].strip(os.sep).split(os.sep)
                deep = 3 if root.endswith("media") else 2       # /media/<user>/<drive>
                if len(parts) > 1 and parts[1] == "Users":      # /mnt/c/Users/<you>
                    deep = 4
                return len(parts) >= deep and not (p == home or _under(home, p))
        return False

    found, seen = set(), set()
    for d in (os.environ.get("PATH") or "").split(os.pathsep):
        if not os.path.isabs(d) or not os.path.isdir(d):
            continue
        real = os.path.realpath(d)
        # each directory once (a PATH often names one twice, or by a link), and never a Windows
        # drive's on WSL (above): their programs run outside the lane
        if real in seen or on_windows(real, drives):
            continue
        seen.add(real)
        if not _under(real, home) and not opens(prefix(real)):
            found.add(real)  # the directory alone: /media/<user>/<drive>/bin isn't a prefix
            continue
        found.add(prefix(real))
        # links only in the home's PATH directories and a `bin` (Linuxbrew's): each entry is an
        # lstat, and a big directory elsewhere has thousands for no link (the 0.90 review)
        if not (_under(real, home) or os.path.basename(real) == "bin"):
            continue
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for n in names:
            link = os.path.join(d, n)
            if os.path.islink(link):
                target = os.path.realpath(link)
                where = prefix(os.path.dirname(target))
                if on_windows(target, drives):
                    continue
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
                                          *lane_paths(task), *siblings(task))  # the git shim,
            if p}                                                              # the ask server
    keep |= tool_roots(hidden) | sdk_roots(hidden)
    # OpenCode's global config where OPENCODE_CONFIG_DIR keeps it, outside the dotfiles: the
    # agent's OpenCode loads it itself (#370, the audit of 1.6)
    if os.environ.get("OPENCODE_CONFIG_DIR"):
        keep.add(os.path.realpath(Path(os.environ["OPENCODE_CONFIG_DIR"]).expanduser()))
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


def read_only(task: store.Task, lane: list[str]) -> list[str]:
    """The installs the lane lets the agent read but not write, for its file tools too: the
    programs it runs (tool_roots: a conda, pyenv or mise env's site-packages) and the toolchains
    split out of the write lane (NOT_WRITABLE, $RUSTUP_HOME/toolchains: rustup's std sources).
    `read` on pandas' frame.py got "Permission denied: external_directory", counting toward the
    six-errors guard, and the only remedy, `wb allow --path`, made the install writable (#521,
    the audit of 1.90). Also the SDKs (sdk_roots). Never one inside a lane entry, whose own
    rules say what is done there."""
    hidden = secret_paths() | private_paths()
    home = Path.home()
    split = [home / h for h in NOT_WRITABLE]
    split += [Path(v) / d for v, d in ((os.environ.get("RUSTUP_HOME"), "toolchains"),
                                       (os.environ.get("CARGO_HOME"), "bin")) if v]
    places = tool_roots(hidden) | sdk_roots(hidden) | {
        os.path.realpath(p) for p in split
        if os.path.isdir(p) and not any(_under(os.path.realpath(p), h) for h in hidden)}
    real_lane = {os.path.realpath(p) for p in lane}
    # one holding a lane entry stays (a Flutter SDK, its bin/cache in the lane: read_only_rules
    # lets the entry be edited again); one in a lane entry is the lane's
    return sorted(p for p in places if p not in real_lane and not any(
        p == q or _under(p, q) for q in real_lane))


def read_only_rules(task: store.Task) -> list[dict]:
    """The file tools may read `read_only` places but never edit there: the kernel refuses the
    write too, with Landlock; without it (macOS) these rules are what keep the file tools out."""
    lane = writable(task)
    if lane is None:
        return []
    places = read_only(task, lane)
    inner = [q for q in lane if any(_under(os.path.realpath(q), p) for p in places)]
    return [{"action": "edit", "resource": f"{p.rstrip('/')}/*", "effect": "deny"}
            for p in _both(places)] + \
        [{"action": "edit", "resource": f"{q.rstrip('/')}/*", "effect": "allow"} for q in _both(inner)]


def external_rules(task: store.Task) -> list[dict]:
    """OpenCode's `external_directory` rule: its file tools and the shell's working directory
    stay in the lane, READABLE and the read-only installs, and the skills handed to the task."""
    lane = writable(task)
    if lane is None:  # (explicit: layer's rule for other MCP servers' tools would match it)
        return [{"action": "external_directory", "resource": "*", "effect": "allow"}]
    have = skills.available()
    # and the rounds' prompts, read-only, where a prompt too long for one argument waits
    places = [*lane, *READABLE, *read_only(task, lane), task.dir / "rounds", *siblings(task),
              *(sibling_link(task.worktree, n) for n in task.spec.get("siblings") or ()),
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
    # read without making the directory: a closed task's rules are gone (cli.gc), its grants
    # kept in its status (cli._keep_grants: wb bench add replays a case with them, #250)
    return store.read_json(store.home() / "rules" / task.name / "grants.json", {}) or \
        task.status.get("grants_kept") or {}


def grant(task: store.Task, key: str, new: list[str]) -> list[str]:
    """Add to one of the task's grants; returns the ones it didn't have."""
    with store.locked(store.task_lock(task.name, "grants")):  # not in rules/, which the lane reads
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
    handbook.write_text(HANDBOOK.read_text() + "".join(
        note for name, note in EXPERIMENT_NOTES.items() if experiment(task, name)))
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
    if sub := task.spec.get("subdir"):  # the package's own, as Claude Code loads it there (#375)
        for rel in ROOT_FILES:
            for base in (wt / sub, repo / sub):
                if (base / rel).is_file():
                    load.append(base / rel)
                    break
    load += [wt / p for p in _tracked(wt, ".claude/rules/*.md")]
    # OpenCode already loads the worktree's AGENTS.md: skip a CLAUDE.md with the same text (one
    # only in the user's checkout isn't loaded, so it can't stand in for CLAUDE.md)
    # (and, started in a package, the package's too: checked on OpenCode 2.3, #375)
    agents = {_digest(a) for a in {wt / "AGENTS.md", wt / (sub or "") / "AGENTS.md"} if a.is_file()}
    seen, files = set(agents), []
    for p in load:
        d = _digest(p)
        if d not in seen:
            seen.add(d)
            files.append(p)
    nested = [p for p in _tracked(wt, "*CLAUDE.md", "*AGENTS.md")
              if "/" in p and Path(p).name in ("CLAUDE.md", "AGENTS.md")
              and not p.startswith(".claude/") and Path(p).parent.as_posix() != sub]
    return files, nested


# What OpenCode 2.3 itself puts before the instructions: its system prompt and the tools left
# after DENIED_TOOLS, measured with tests/capture_llm.py (the note at DENIED_TOOLS). Text is
# estimated at CHARS_PER_TOKEN; a fixed prompt past FIXED_SHARE of the window is said at wb start.
OPENCODE_BASE_TOKENS = 3500
CHARS_PER_TOKEN = 4
FIXED_SHARE = 0.4


def fixed_prompt(task: store.Task) -> tuple[int, list[tuple[str, int]]]:
    """About how many tokens the agent reads before its first step (OpenCode's own prompt, the
    instruction files, AGENTS.md, round 1's prompt), and each part's, largest first. A 25 KB
    CLAUDE.md on a 32K window started the agent near its handover point, and it looped between
    reading and handovers (the 2026-11-19 audit)."""
    files, _ = instruction_files(task)
    wt, rules = task.worktree, rules_dir(task)
    named = {rules / "HANDBOOK.md": "the handbook", rules / "CLAUDE.md": "~/.claude/CLAUDE.md"}
    paths = [*files, *([wt / "AGENTS.md"] if (wt / "AGENTS.md").is_file() else []),
             task.round_dir(1) / "prompt.md"]
    parts = [("OpenCode's own prompt and tools", OPENCODE_BASE_TOKENS)]
    for p in paths:
        try:
            n = p.stat().st_size // CHARS_PER_TOKEN
        except OSError:
            continue
        label = named.get(p) or ("the spec" if p.name == "prompt.md" else
                                 str(p.relative_to(wt)) if p.is_relative_to(wt) else p.name)
        parts.append((label, n))
    parts.sort(key=lambda x: -x[1])
    return sum(n for _, n in parts), parts


def denied_skills(extra: list[str] | None = None, project: Path | None = None) -> list[str]:
    """The user's personal skills (~/.claude/skills, the synced Claude-app ones included) are
    theirs, about their other work: an agent sees only the project's own .claude/skills. On
    2026-11-16 an agent, its context full, summarised a personal publishing skill
    into a made-up "newer task" and went off reading the user's private library."""
    names = {"workbench", *(extra or [])}
    # OpenCode reads ~/.claude/skills and ~/.agents/skills, the cross-tool place other agents read
    # too: the second reached every agent, prefill on every round (#371, the audit of 1.6)
    for home in {claude_home() / "skills", Path.home() / ".claude" / "skills",
                 Path.home() / ".agents" / "skills"}:
        names |= {s.parent.name for s in home.glob("*/SKILL.md")}
        names |= {s.parent.name for s in (home / "synced").glob("*/*/SKILL.md")}
    if project:  # a project's own skill keeps its name (the personal ones are unreadable anyway)
        for own in (".claude", ".agents"):
            names -= {s.parent.name for s in (project / own / "skills").glob("*/SKILL.md")}
    return sorted(names | {"workbench", *(extra or [])})


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
    rules += read_only_rules(task)
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
