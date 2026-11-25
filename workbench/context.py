"""What the agent knows before it starts, so it works the way Claude would in the same repo.

The agent's OpenCode runs with the project's own config shut out (OPENCODE_DISABLE_PROJECT_CONFIG,
runner.rules_env), which also stops it reading the project's AGENTS.md and skills; it still lists
the user's global ones (~/.claude/skills and the like). This module gives the task's config layer
everything else: the project's AGENTS.md, skills and opencode.json `instructions` handed back,
the workbench handbook, the user's and the repo's Claude instructions, a map of the nested
CLAUDE.md files to read on the way, and a skill policy (the user's personal skills and /workbench
itself are hidden, leaving the project's own; so is `question`, which nobody answers headless).
"""
from __future__ import annotations

import fnmatch
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

from . import audio, chat, gitops, hardware, images, lane, models, ocplugin, privacy, routes, skills, store

PKG_ROOT = Path(__file__).resolve().parent.parent
HANDBOOK = PKG_ROOT / "agent" / "HANDBOOK.md"
WRITING = PKG_ROOT / "agent" / "WRITING.md"  # the writing role's, in HANDBOOK.md's place (#1025)
ROOT_FILES = ("CLAUDE.md", ".claude/CLAUDE.md", "CLAUDE.local.md")
# Tools an unattended run in a worktree must not use. Nobody answers `question`. Code mode
# (`execute`) and the opencode_* tools can move the session or remove worktrees. A `subagent`
# would load a second model onto the GPU behind the task's back. Denied tools also leave the prompt, which cuts
# the fixed prompt from ~9k to ~3.5k tokens: less to read on every request. OpenCode 2.3 still
# lists opencode_worktree_remove; the rule then refuses the call.
DENIED_TOOLS = ("question", "experimental_image_generate", "execute", "subagent", "websearch",
                "webfetch", "opencode_*", "*worktree*")  # the network, like curl: Claude's to allow
# wb's own OpenCode agent, which every `opencode run` names (`--agent`, runner.run_round; a
# resumed `--session` is switched to it too). Without it OpenCode 2.3.2 took the session's agent
# from the config's `default_agent`, and the user's global opencode.json is one of those
# documents: an agent of theirs whose rules allowed webfetch and subagent gave every wb agent
# both, the layer's denies notwithstanding, as an agent's own rules come after the top-level
# ones and the last match wins (#1290, 2026-11-24, measured with tests/capture_llm.py). A user's
# own `wb-code` (their JSON, or agents/wb-code.md) loads before the layer and kept its system,
# steps and rules, so the layer removes any (`disabled`, AGENT_OFF) and rules_env's content,
# loaded after the layer, makes it afresh (AGENT_ENTRY): a new agent starts from OpenCode's
# defaults and takes every top-level rule, the layer's included, as the default `build` agent did.
AGENT = "wb-code"
AGENT_OFF = {AGENT: {"disabled": True}}
AGENT_ENTRY = {AGENT: {"mode": "primary", "description": "Workbench's agent: the task's rules alone"}}
PLUGIN_OFF = ocplugin.PLUGIN_OFF  # "-workbench": the task layer's removal of the OpenCode plugin (#1311)
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
# behind them, the kernel refuses sudo (no_new_privs, lane.py, on any Linux; Seatbelt by its path
# on macOS) and, with Landlock or Seatbelt, gh finds no login (its config and tokens are out of
# reach). Without either only these patterns stand.
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
    # not a second for the bare word
    return tuple(f"{c} *" for c in commands) + \
        tuple(f"{w} {c} *" for w in _WRAPPERS for c in commands)


# A recursive rm of the home or the root, whatever was allowed: `rm` is on ALLOWED_COMMANDS, the
# agent's HOME is the real one and its lane writes the caches under it, so `rm -rf main ~` would
# have deleted them; an API agent sent just that (task mid1-xl-scout, 2026-11-23, #1146), refused
# only because the same line ran `wb`. The space before the word keeps `rm -rf build~`, and the
# word ending there (or a space after it) keeps `rm -rf ~/build` and `rm -rf /tmp/x`. Not `~/*`,
# `$HOME/*` or `/*`: OpenCode's `*` can't be matched literally, so such a pattern would refuse
# every rm under home. Nor `bash -c`, `find ~ -delete`, `xargs rm`, `\rm`, `/bin/rm`, an operand
# before the options (`rm ~ -rf`) or another quoting: the lane stands behind those.
_HOME_OR_ROOT = ("~", "~/", "$HOME", '"$HOME"', "${HOME}", "$HOME/", "/")
DENIED_SHELL = _denied(_HARD) + _denied(tuple(f"rm * {w}" for w in _HOME_OR_ROOT))
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
    "xxd", "hexdump", "strings", "md5*", "sha*sum", "base64", "seq", "paste",
    "fold", "fmt", "expand", "rev", "tac", "split", "csplit", "join", "iconv", "less", "more",
    # building, testing, running code
    "make", "gmake", "cmake", "ctest", "ninja", "meson", "autoreconf", "configure", "pkg-config",
    # `gcc*`, `g++*`, `clang*`: clang++, clang-format, clang-tidy and the versioned ones
    # (`gcc-13`), one entry each (a family's names cost ~135 bytes each in the layer)
    "cc", "gcc*", "g++*", "c++", "clang*", "ld", "ar", "nm", "objdump", "strip", "gdb",
    "lldb", "valgrind", "cppcheck",
    "python", "python3", "pip", "pip3", "pytest", "ruff", "mypy", "black", "isort", "flake8",
    "pylint", "uv", "uvx", "poetry", "tox", "coverage",
    "node", "npm", "npx", "pnpm", "yarn", "bun", "bunx", "deno", "tsc", "tsx", "eslint",
    "prettier", "vitest", "jest", "playwright",
    "go", "gofmt", "cargo", "rustc", "rustfmt", "rustup", "java", "javac", "jar", "mvn", "gradle",
    "dotnet", "ruby", "bundle", "rake", "rspec", "php", "composer", "zig",
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
    # whole ecosystems and plain builtins #514 missed: `dune build`, `lein test`, `pod install`,
    # `pushd sub && make && popd` and `printenv PATH` each cost a question and a wake (#672).
    # Not nix: its builds go through the daemon's socket, outside the lane.
    "dune", "opam exec", "lein", "clojure", "clj", "rebar3", "nimble", "shards", "gleam", "elm",
    "mill", "scala-cli", "pod", "ant", "crystal", "nim", "erlc", "escript",
    "pushd", "popd", "printenv", ":", "ulimit", "local", "declare", "shift", "trap", "pgrep",
    # and the ordinary tools #514 and #672 still missed (#830, the second 2026-11-21 audit):
    # macOS's own (`shasum`, `md5`: `sha*sum`, `md5*` above), the wrappers _WRAPPERS already
    # bounds, process and coreutils tools, native builds' (versioned compilers like python3.*),
    # more ecosystems'
    "sysctl", "sw_vers", "stdbuf", "setsid", "pkill", "killall", "expr", "bc",
    "truncate", "dd", "shuf", "cksum", "flock",
    "ldd", "readelf", "addr2line", "size", "gcov", "ccache", "sccache", "gfortran", "nvcc",
    "mpirun",
    "scons", "protoc", "ocaml", "iex", "ghci", "runghc", "irb", "gem", "busted",
    # `lua*`: lua, luajit, lua5.*, luac, luac5.*, luacheck, luarocks (`luarocks install *` asks)
    "lua*", "cython", "maturin", "virtualenv", "sphinx-build", "mkdocs", "corepack",
    # the Lisps, Schemes and Forth, and the checkers the skill's reference/checks.md puts in a
    # task's --test: an agent trying `guile t.scm` or `sbcl --script` mid-round was refused, each
    # first use a question and a wake (the owner, 2026-11-22: "does our whitelisting too strict").
    # `nix-instantiate --parse` only: nix's evaluation can fetch and build through the daemon
    "sbcl", "ecl", "ccl", "clisp", "racket", "raco", "guile", "guild", "csc", "csi",
    "chicken-cs?", "scheme", "chez*", "petite", "gosh", "chibi-scheme",
    "mit-scheme", "gforth", "selene", "basedpyright", "staticcheck",
    "yamllint", "actionlint", "hlint", "tflint", "statix", "deadnix", "clj-kondo",
    "nix-instantiate --parse",
    # the infrastructure a repo checks offline (the owner, 2026-11-22: "dont forget about common
    # infra tools like docker podman terraform"). Terraform and OpenTofu by subcommand, no longer
    # whole: format, validate, fetch what the project lists (init, no backend; not `get`, whose
    # `-update` moves the modules to newer versions, a dependency choice), no state. `plan`,
    # `show`, `output`, `refresh` read remote state with the user's credentials, `test` "creates
    # real infrastructure" (its help, 1.16.1), `apply`, `import`, `state` change it. Of docker,
    # the compose file rendered (no daemon: the lane has no socket to one, rootless podman needs
    # the setuid newuidmap); of kubectl, kustomize (every other subcommand is the cluster, with
    # its credentials); of helm, what renders a chart offline; the scanners over local files;
    # Ansible's syntax check, which reaches no host (a dynamic inventory it loads can still call
    # a cloud); and a test's own database or cache, started
    # in the temp directory (the skill's reference/checks.md says how). Postgres's and Redis's
    # clients aren't here: what the worktree's .env names can be production. A `*` inside an
    # entry takes the flags projects put first (`init -input=false -backend=false`, `-chdir=infra`,
    # `compose -f ci.yml config`, `ansible-playbook site.yml --syntax-check`), and can be steered:
    # `terraform -chdir=. plan -out validate` matches `-chdir=* validate` (the outward steps below
    # still ask). `docker?compose` is `docker compose` and `docker-compose`, one entry
    "terraform fmt", "terraform validate", "terraform version", "terraform init *-backend=false",
    "terraform -chdir=* fmt", "terraform -chdir=* validate",
    "terraform -chdir=* init *-backend=false",
    "tofu fmt", "tofu validate", "tofu init *-backend=false", "tofu -chdir=* fmt",
    "tofu -chdir=* validate",
    "terraform-docs", "checkov", "hadolint", "docker?compose config",
    "docker?compose -f *ml config",
    "helm lint", "helm template", "helm dependency build", "helm dep build",
    "kustomize", "kubectl kustomize", "kubeconform", "conftest test", "opa test", "trivy config",
    "trivy fs", "ansible-lint", "ansible-playbook *--syntax-check", "cfn-lint",
    "initdb", "pg_ctl", "pg_isready", "redis-server",
    # and the database clients, to read the test's own database: python can already dial any
    # database the lane reaches (the owner, 2026-11-22: "allow the clients, keep migrations for
    # review"). Not the migration tools: alembic, flyway or prisma migrate act on whatever the
    # worktree's .env names, which can be production
    "psql", "mysql", "mariadb", "redis-cli", "valkey-cli", "mongosh",
    # and the wider set the research for #994 proposed (2026-11-22), each rechecked: each first
    # use was a question and a wake. Off it or narrowed, and why (ARCHITECTURE.md lists them):
    # what sends usage data by default runs only in its opt-out spelling (cdk, sam, redocly,
    # semgrep, azurite) or not at all (turbo, moon, skaffold, vcpkg, oasdiff); what sends the
    # project's runs or files to a service the repo names only with that off (nx, rush; `buf
    # generate`, whose remote plugins run on the BSR, not at all); globs over names anyone
    # publishes under (`wasm-*` takes wasm-to-oci, which pushes; `qemu-*` the setuid
    # bridge helper; `protoc-gen-*`, `mdbook-*`, which protoc and mdbook run themselves) named.
    # more IaC, offline
    "packer fmt", "packer validate", "packer version",
    "CDK_DISABLE_CLI_TELEMETRY=true cdk synth", "CDK_DISABLE_CLI_TELEMETRY=true cdk synthesize",
    "CDK_DISABLE_CLI_TELEMETRY=true cdk ls", "CDK_DISABLE_CLI_TELEMETRY=true cdk list",
    "SAM_CLI_TELEMETRY=0 sam validate", "SAM_CLI_TELEMETRY=0 sam build",
    "cfn-guard", "bicep build", "bicep lint", "bicep format",
    "ansible-galaxy collection build", "ansible-galaxy collection init", "ansible-galaxy role init",
    "molecule syntax", "molecule list",
    "opa build", "opa parse", "opa inspect", "regal lint", "kyverno test", "gator verify",
    "pluto detect-files", "polaris audit *--audit-path*", "kics scan", "grype",
    "cue vet", "cue eval", "cue export", "cue fmt", "cue def",
    "jsonnet", "jsonnetfmt", "jsonnet-lint", "tk eval", "tk export", "tk fmt", "tk lint", "ytt",
    "dhall", "dhall-to-json", "dhall-to-yaml", "nickel", "pkl eval", "pkl test",
    "docker buildx bake *--print*", "podman-compose config", "act -l", "act --list",
    "systemd-analyze verify", "systemd-analyze calendar", "systemd-analyze security",
    "nginx -t", "nginx -T", "nginx -v", "nginx -V", "caddy validate", "caddy adapt", "caddy fmt",
    "haproxy -c", "nomad fmt", "consul validate",
    # local services for tests (started in the temp directory, as above); Firebase's only on a
    # demo- project, whose code "interacts with emulators only" (its docs): a real project's
    # functions reach the live services the emulators don't cover
    "postgres", "pg_config", "mysqld", "mariadbd", "mariadb-install-db", "mysql_install_db",
    "redis-benchmark", "valkey-server", "memcached", "mongod", "nats-server", "etcd",
    "minio server", "duckdb", "azurite *--disableTelemetry*", "moto_server",
    "firebase emulators:start *--project?demo-*", "firebase emulators:exec *--project?demo-*",
    # build systems and monorepos
    "buck2", "pants", "plz", "nx *--no-cloud*", "NX_NO_CLOUD=true nx",
    "RUSH_BUILD_CACHE_WRITE_ALLOWED=0 rush", "rush install", "rush update", "rush list", "rushx",
    "gn", "autoninja", "xmake", "premake5", "waf", "qmake", "qmake6", "samu", "muon", "colcon",
    "cpack", "bear", "compiledb", "include-what-you-use", "autoconf", "automake", "aclocal",
    "autoheader", "libtoolize", "libtool", "m4", "bison", "flex", "yacc", "re2c", "gperf", "swig",
    "llvm-*", "lld", "ld.lld", "mold", "objcopy", "ranlib", "c++filt",
    # languages
    "dmd", "ldc2", "gdc", "rdmd", "dub", "dscanner", "dfmt",
    "fpm", "flang", "flang-new", "lfortran", "fprettify", "gnatmake", "gprbuild", "gprclean",
    "gnatprove", "alr", "fpc", "lazbuild", "cobc", "v", "odin", "valac", "hare", "haxe", "haxelib",
    "purs", "spago", "purs-tidy", "rescript", "fantomas", "paket", "roc", "idris2", "agda",
    "coqc", "coq_makefile", "coqdep", "rocq", "lake", "lean", "swipl", "gprolog", "tclsh",
    "prove", "perltidy", "perlcritic", "carton", "raku", "zef", "octave", "octave-cli",
    "groovy", "groovyc", "ktlint", "ktfmt", "detekt", "shadow-cljs", "bb", "cljfmt", "zprint",
    # contracts, wasm, schemas (`forge create` and `forge script` deploy: not on it)
    "forge build", "forge test", "forge fmt", "forge coverage", "forge snapshot", "forge inspect",
    "forge clean", "forge doc", "forge tree", "anvil", "solc", "solhint", "slither", "vyper",
    "wasm-pack", "wasm-opt", "wasm-tools", "wasm-bindgen", "wasm-ld", "wasm-objdump",
    "wasm-validate", "wasm-interp", "wasm-strip", "wasm-merge", "wasm-dis", "wasm-as",
    "wat2wasm", "wasm2wat", "wasmtime", "emcc", "em++", "emcmake", "emmake",
    "buf build", "buf lint", "buf format", "buf breaking", "buf export", "buf convert",
    "protolint", "graphql-codegen", "spectral",
    "REDOCLY_TELEMETRY=off redocly lint", "REDOCLY_TELEMETRY=off redocly bundle",
    "REDOCLY_TELEMETRY=off redocly build-docs", "REDOCLY_TELEMETRY=off redocly split",
    "REDOCLY_TELEMETRY=off redocly join", "REDOCLY_TELEMETRY=off redocly stats",
    "oapi-codegen", "openapi-generator-cli", "vacuum",
    # docs
    "mdbook", "hugo", "jekyll", "asciidoctor", "pandoc", "latexmk", "pdflatex",
    "xelatex", "lualatex", "bibtex", "biber", "makeindex", "kpsewhich", "chktex", "tectonic",
    "typst", "quarto", "zola", "doxygen", "plantuml", "dot", "mmdc", "d2", "typedoc",
    "sphinx-autobuild", "sphinx-apidoc", "pdoc", "lychee", "rsvg-convert", "magick",
    # shaders, Godot, embedded: compile and emulate, no flasher (the flashing steps are OUTWARD)
    "glslangValidator", "glslc", "spirv-*", "dxc", "slangc", "naga", "godot", "godot4", "gdlint",
    "gdformat", "arm-none-eabi-*", "avr-gcc", "avr-g++", "avr-objcopy", "avr-size",
    "riscv64-unknown-elf-*", "riscv32-unknown-elf-*", "aarch64-linux-gnu-*",
    "x86_64-w64-mingw32-*", "qemu-system-*", "qemu-img", "qemu-aarch64*", "qemu-arm*",
    "qemu-riscv*", "qemu-x86_64*", "qemu-i386*", "renode",
    "idf.py build", "idf.py set-target", "idf.py size", "idf.py fullclean", "idf.py reconfigure",
    "west build", "twister", "arduino-cli compile",
    # formatters, linters, test runners
    "biome", "oxlint", "dprint", "stylua", "shfmt", "taplo", "editorconfig-checker", "typos",
    "codespell", "vale", "sqlfluff", "cspell", "markdownlint-cli2", "markdownlint", "stylelint",
    "buildifier", "buildozer", "cmake-format", "gersemi", "yamlfmt", "swiftlint", "swiftformat",
    "scalafmt", "google-java-format", "checkstyle", "ormolu", "fourmolu", "ocamlformat",
    "erlfmt", "nixfmt", "alejandra", "php-cs-fixer", "phpcs", "phpstan", "psalm", "phpunit",
    "pint", "gitleaks", "detect-secrets", "osv-scanner", "bandit", "pip-audit",
    "semgrep *--metrics?off*", "SEMGREP_SEND_METRICS=off semgrep",
    "bats", "robot", "behave", "cucumber-js", "hyperfine", "mocha", "ava", "c8", "nyc",
    "ginkgo", "gotestsum", "kcov", "lcov", "genhtml", "gcovr",
)
# The tools on that list only by their subcommands: `--test 'terraform validate'` granted
# `terraform *` (test_allowances), so the agent could run `terraform apply` and `helm push`
# under it. Theirs is the command as written, with more arguments, and no more. The tool of an
# entry spelled with its opt-out first (`SAM_CLI_TELEMETRY=0 sam build`) is the word after it:
# else `--test 'REDOCLY_TELEMETRY=off redocly lint'` granted `redocly *`, telemetry and `redocly
# push` with it (#994, 2026-11-22)
def _entry_tool(entry: str) -> str:
    words = [w for w in entry.split(" ") if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=\S*", w)]
    return re.split(r"[?*]", words[0])[0] if words else ""


LIMITED = (frozenset(_entry_tool(e) for e in ALLOWED_COMMANDS if " " in e)
           - set(ALLOWED_COMMANDS))
# and the tools that reach a cloud, a cluster, a registry, a host or a container daemon, on the
# list by a subcommand or not at all: `--test 'terragrunt hcl fmt --check'` granted
# `terragrunt *`, and with it `terragrunt apply`; `pulumi preview` `pulumi up`, `cdk synth` `cdk
# destroy` (the review, 2026-11-22). Read by test_allowances only: nothing in the layer
OUTWARD_TOOLS = frozenset((
    "terragrunt", "pulumi", "cdk", "cdktf", "sam", "serverless", "sls", "packer", "aws", "gcloud",
    "gsutil", "az", "doctl", "eksctl", "flyctl", "fly", "heroku", "vercel", "netlify", "wrangler",
    "firebase", "kubectl", "helm", "kind", "minikube", "k3d", "skaffold", "argocd", "flux", "oc",
    "docker", "docker-compose", "podman", "buildah", "ansible", "ansible-playbook", "vagrant",
    "nomad", "vault", "consul",
    # and the monorepo runners whose remote cache uploads with the user's login, off the list
    # (#994, 2026-11-22): `--test 'turbo run test'` mustn't grant `turbo *`
    "turbo", "lerna"))
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
    "opam install *", "nimble install *", "gleam add *", "elm install *", "clojure -Ttools *",
    "clj -Ttools *",  # (#672)
    "luarocks install *",  # (#830; `gem install *` above, now that `gem` itself is allowed)
    # fetch a package and run it
    "npx -y *", "npx --yes *", "npm exec *", "pnpm dlx *", "yarn dlx *", "bunx *", "uvx *",
    # their other spellings (the 2026-11-16 audit); a plain `npx <pkg>` runs only what the
    # project installed, as the runner sets npm_config_yes=false for OpenCode
    "npm x *", "npx -p *", "npx --package *", "npx --package=*", "bun x *", "uv tool run *",
    "uv tool install *", "uv run --with *", "deno run *npm:*", "deno run *jsr:*",
    # the package steps of the tools #994 added (2026-11-22); `fpm install`, `v install`, `zef
    # install` and `alr install` put programs or modules in the user's own prefix, like `cargo
    # install`. Kept for the dropped tools too, for a grant of the whole tool
    "nx add *", "nx migrate *", "rush add *", "rush remove *", "rush upgrade-interactive *",
    "moon toolchain *", "moon setup *", "xmake require *", "vcpkg install *", "vcpkg add *",
    "vcpkg x-update-baseline *", "dub add *", "dub fetch *", "dub add-local *", "dub add-path *",
    "dub add-override *", "dub remove *", "alr with *", "alr get *", "alr toolchain *",
    "alr install *", "alr index *", "haxelib install *", "haxelib git *", "haxelib hg *",
    "haxelib update *",
    "haxelib upgrade *", "haxelib setup *", "spago install *", "spago uninstall *",
    "spago upgrade *", "paket add *", "zef install *", "zef upgrade *", "zef uninstall *",
    "fpm install *", "v install *", "hugo mod get *", "quarto add *", "quarto install *",
    "quarto use *", "quarto update *", "quarto remove *",
    # `dub run <pkg>` fetches a registry package by name and runs it, as `uvx` does; `build` and
    # `test` fetch one too. The project's own (bare, a flag first, `:sub`) are INSTALL_LISTED's
    # (#994's review, 2026-11-22)
    "dub run *", "dub build *", "dub test *",
)
# Inside the allowed tools, the steps that reach out: publishing to a registry, deploying,
# changing infrastructure or a cluster (the owner, 2026-11-22, asking for the infra tools). They
# stand at ASK_FIRST's place in the rules (shell_rules): after the acceptance command's tools, so
# `--test 'cargo test'` granting `cargo *` doesn't let `cargo publish` through, and before the
# user's and Claude's allowances, so `wb allow` lifts one. Each starts with `*`, so a launcher's
# or wrapper's spelling is caught too (`pnpm publish`, `npx cdk deploy`, `./mvnw deploy`, `timeout
# 600 terraform apply`, `env helm install`), and needs no `*=*` copy. Most verbs are matched as
# a word, a space before it and a space or the end after it (the trailing ` *` is optional): the
# looser `*mvn* *deploy*` also refused `mvn -pl deploy-tools test`, a `*` reaching into the next
# word as #442's did before the hard words. Gradle's are prefixes, as its publish tasks are
# (`publishToMavenCentral`, `:lib:publishAllPublicationsToSonatypeRepository`, `publishPlugins`),
# and so are Maven's goals (`deploy:deploy-file`, `release:perform`): a project or task named
# publish… (`./gradlew :publishing:test`, `-x publish`) or a module `-pl deploy` asks once, as
# does what else still matches (`npm publish --dry-run`, `./gradlew publishToMavenLocal`,
# `wrangler deploy --dry-run`, `rg 'npm publish x'`). What none can see: the same step from a
# script, a Makefile target (`make deploy` names nothing a pattern can judge), `bash -c` or an
# interpreter, the verbs not listed here (`bun publish`, `sbt publish`), flags before a verb
# other than npm's and cargo's (`yarn --cwd x publish`), and anything behind a wrapper or an
# `=` that isn't one of these steps: `env`, `timeout`, `nice`, `nohup`, `time`, `command` and
# `xargs` are allowed whole, and `*=* <cmd> *` takes any line with an `=` before an allowed
# command's name, so `env terraform plan`, `timeout 60 kubectl patch …`, `env docker run` and
# `kubectl --context=prod exec x -- sh` run, every subcommand limit and the default deny lifted
# alike. Review is the guard there; these only stop the plain and the wrapped spelling of each.
OUTWARD = (
    # npm's registry writes; `pnpm publish` and yarn 2's `yarn npm publish` contain them.
    # ~/.npmrc stays readable for installs, so its token would work. Not `npm login` or
    # `adduser`: they wait on a browser or a terminal, and with Landlock can't write ~/.npmrc
    "*npm publish *", "*npm unpublish *", "*npm deprecate *", "*npm dist-tag *", "*npm owner *",
    "*npm access *", "*npm token *", "*yarn publish *",
    # and with flags before the verb: a workspace's or a monorepo's (`pnpm -r publish`, `npm -w
    # packages/a publish`, `pnpm --filter @x/a publish`), a toolchain's (`cargo +nightly publish`)
    "*npm -* publish *", "*cargo +* publish *",
    # the other registries whose tools are allowed (`python -m twine upload` through `python *`)
    "*cargo publish *", "*gem push *", "*poetry publish *", "*uv publish *", "*twine upload *",
    "*dotnet nuget push *", "*pub publish *", "*hex.publish *",
    # Maven's deploy phase and release plugin, Gradle's publish tasks (a subproject's too): their
    # settings.xml and gradle.properties sit in the writable ~/.m2 and ~/.gradle
    "*mvn* deploy *", "*mvn* deploy:*", "*mvn* release:*", "*gradle* publish*",
    "*gradle* *:publish*",
    # deploys through a project's own CLI (npx cdk, npx wrangler)
    "*cdk deploy *", "*wrangler* deploy *", "*firebase deploy *", "*prisma migrate deploy *",
    # infrastructure and cluster changes, for a grant (`wb allow 'terraform *'`) or a wrapper
    # (`timeout *`, `env *`) the narrowed entries above don't bound
    "*terraform* apply *", "*terraform* destroy *", "*terraform* import *", "*terraform* state *",
    "*tofu* apply *", "*tofu* destroy *", "*kubectl* apply *", "*kubectl* create *",
    # `test` creates real infrastructure, and `terraform -chdir=infra test` matched `*=* test *`
    # (`grep -rn terraform test` and `make -C terraform test` ask once). `init -upgrade` moves
    # the providers and modules to newer versions, a dependency choice, as `get -update` would
    "*terraform* test *", "*tofu* test *",
    "*terraform* init *-upgrade*", "*tofu* init *-upgrade*",
    "*kubectl* delete *", "*helm* install *", "*helm* upgrade *", "*helm* uninstall *",
    # an image to a registry; a chart rendered against the live cluster
    "*docker push *", "*helm * --validate*",
    # the outward steps of the tools #994 added (2026-11-22): a publish, a login, a remote
    # cache's link, a deploy, a self-update (it replaces the user's own program), a board
    # flashed or its eFuses burned (`idf.py build flash`, `-t flash`, `--device-testing`,
    # `compile -u`), results uploaded (polaris's to Fairwinds Insights). Kept for the dropped
    # tools too (turbo, moon, vcpkg): a grant of the whole tool still stops at them
    "*turbo login *", "*turbo link *", "*rush publish *", "*moon upgrade *", "*pants publish *",
    "*pants experimental-deploy *", "*plz update *", "*buck2* install *", "*xmake update *",
    "*xmake service *", "*xmake repo *", "*vcpkg integrate *", "*fpm publish *",
    "*alr publish *", "*haxelib submit *", "*haxelib register *", "*spago publish *",
    "*spago auth *", "*spago registry *", "*paket push *", "*lake upload *", "*lake cache put *",
    "*lake exe cache put*",
    "*wasm-pack publish *", "*wasm-pack login *", "*hugo deploy *", "*hugo server trust *",
    "*quarto publish *", "*semgrep* ci *", "*semgrep* login *", "*semgrep* publish *",
    "*nx login *", "*nx import *", "*nx configure-ai-agents *", "*typst update *",
    "*cucumber* --publish*", "*polaris* --upload-insights*",
    "*packer build *", "*bicep publish *", "*buf push *", "*redocly push *", "*forge create *",
    "*forge* --broadcast*",
    "*idf.py* *flash *", "*idf.py* *efuse-*", "*idf.py* *erase*", "*west build* -t flash *",
    "*west build* --target?flash *", "*twister* --device-testing*", "*twister* --west-flash*",
    "*arduino-cli* --upload*", "*arduino-cli* -u*", "*west build* -tflash*",
    # and what the recheck's review found the research missed (#994, 2026-11-22): under
    # `NX_NO_CLOUD=true nx` and `nx *--no-cloud*`, `nx release publish` ran npm publish with the
    # readable ~/.npmrc's token, and `nx connect`, `view-logs` ("upload the most recent run
    # details"), `record` and the other nx-cloud aliases ran (nx's command reference); results
    # uploaded by a flag (psalm's to Shepherd, kcov's to coveralls.io); self-updates
    "*nx release *", "*nx connect *", "*nx connect-to-nx-cloud*", "*nx view-logs *",
    "*nx record *", "*nx start-ci-run *", "*nx start-agent *", "*nx start-nx-agents *",
    "*nx stop-all-agents *", "*nx fix-ci *", "*nx apply-locally *", "*nx download-cloud-client *",
    "*psalm* --shepherd*", "*kcov* --coveralls-id*",
    "v up *", "* v up *", "*dprint upgrade *", "*php-cs-fixer* self-update*",
    "*php-cs-fixer* selfupdate*",
    # a shared build cache written, or the build run on remote workers, from the agent's
    # unreviewed worktree (#999, 2026-11-22). The agent's environment turns Pants's and Nx's off
    # whatever the repo's config says (runner.REMOTE_CACHES_OFF); these are the spellings that
    # turn them back on: Pants's flags, which beat its environment (its options docs), and either
    # variable named at all (`unset`, `=false`). Bazel and Buck2 have no such switch, so only
    # their flags that name a cache, an executor or a build-event server, or upload every
    # action, are here; what a repo's .bazelrc or .buckconfig turns on is the reviewer's to see
    "*pants* --remote-cache-write *", "*pants* --remote-cache-write=*",
    "*pants* --remote-execution *", "*pants* --remote-execution=*",
    "*PANTS_REMOTE_CACHE_WRITE*", "*PANTS_REMOTE_EXECUTION*", "*NX_SKIP_REMOTE_CACHE*",
    "*bazel* --remote_cache=*", "*bazel* --remote_cache *", "*bazel* --remote_executor=*",
    "*bazel* --remote_executor *", "*bazel* --bes_backend=*", "*bazel* --bes_backend *",
    "*buck2* --upload-all-actions*", "*buck2* --write-to-cache-anyway*",
    "*buck2* --remote-only*", "*buck2* --prefer-remote*", "*buck2* *buck2_re_client.*",
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
    "pio pkg install", "platformio pkg install", "nimble install -d", "nimble install --depsOnly",
    # and what the project lists, for the tools #994 added (2026-11-22): xmake.lua's
    # add_requires, a build.hxml's libraries, spago.yaml's, the META6.json's, go.mod's modules
    "xmake require", "haxelib install *.hxml", "spago install", "zef install --deps-only .",
    "hugo mod get", *(f"dub {c}{a}" for c in ("run", "build", "test") for a in ("", " -*", " :*")),
)
# and with the flags projects spell those installs with, one flag each, and pip's extras of the
# project itself: `pnpm install --frozen-lockfile` and `pip install -e ".[dev]"` were refused,
# so an agent copying the project's own command spent a question and a Claude wake (#671).
# `[*]` ends in `]`, which no package name does (a second requirement with extras after it
# would match: a guardrail, not a sandbox).
INSTALL_FLAGS = ("--frozen-lockfile", "--immutable", "--prefer-offline", "--offline",
                 "--legacy-peer-deps", "--no-audit", "--ignore-scripts", "--prod", "--production",
                 "--no-fund")
# and the common CI lines with two, as written, by the managers that spell them:
# `pnpm install --frozen-lockfile --prefer-offline` and `npm i --no-audit --no-fund` asked first
# (#916, the third 2026-11-21 audit). A short list, not every pair: with each rule's `*=*` copy
# those were ~80 KB of rules (then past runner.CONFIG_CONTENT_MAX, which bounded the rules until
# #994). No flag with `=*`: the `*` would let a package name through.
INSTALL_FLAG_PAIRS = {
    ("npm install", "npm i"): ("--no-audit --no-fund", "--no-fund --no-audit",
                               "--prefer-offline --no-audit", "--no-audit --prefer-offline"),
    ("pnpm install", "bun install"): ("--frozen-lockfile --prefer-offline",
                                      "--prefer-offline --frozen-lockfile",
                                      "--frozen-lockfile --ignore-scripts",
                                      "--frozen-lockfile --production")}
INSTALL_LISTED += tuple(f"{c} {f}" for c in ("npm install", "npm i", "pnpm install", "pnpm i",
                                             "bun install", "bun i") for f in INSTALL_FLAGS)
INSTALL_LISTED += tuple(f"{c} {f}" for cs, fs in INSTALL_FLAG_PAIRS.items() for c in cs for f in fs)
INSTALL_LISTED += tuple(f"{c} -e {e}" for c in (
    "pip install", "pip3 install", "python -m pip install", "python3 -m pip install",
    "uv pip install", "pip3.* install", "python3.* -m pip install")
    for e in (".[*]", '".[*]"', "'.[*]'"))
# The pip forms above are allowed only when they install into the worktree's own venv, or no
# environment is active and Landlock bounds pip (python_installs); else they ask first. These,
# which change the active venv, ask first while it is the user's (#490)
ENV_INSTALLS = ("poetry install *", "poetry sync *", "poetry remove *", "uv sync --active *",
                "uv pip sync *", "uv pip uninstall *", "pip uninstall *", "pip3 uninstall *",
                "python -m pip uninstall *", "python3 -m pip uninstall *")
# A tool the project installed runs through its package manager or by its path, not by its name:
# `npx turbo run build`, `pnpm exec cdk synth`, `yarn nx test`, `uv run semgrep`,
# `node_modules/.bin/redocly lint`. `npx *`, `pnpm *`, `yarn *`, `bun *`, `uv *` and
# `node_modules/.bin/*` are allowed whole, so each of those ran without the opt-out its plain
# spelling needs, and turbo and lerna, which aren't allowed at all, ran (#999, 2026-11-22). The
# runner forms of the tools the rules name, as each ecosystem installs them, now get the verdict
# of the plain form: denied after the acceptance command's tools (shell_rules), then allowed again
# as each ALLOWED_COMMANDS entry of the tool is spelled under each runner, its opt-out first
# (`CDK_DISABLE_CLI_TELEMETRY=true npx cdk synth`); their package steps ask first and their
# installs of what the project lists are allowed again. npm's names (registry.npmjs.org's `bin`
# of each package, 2026-11-22): Redocly's CLI is also `openapi`, Rush ships `rush-pnpm`, moon
# `moonx`, Azurite a server per service, Vercel `vc`, Netlify `ntl`, Wrangler `wrangler2` and
# `cf-wrangler`; spago is allowed whole, here for its package steps. Python's: the tools pip
# installs into a project's venv (semgrep has no CLI on npm, nor sam).
RUNNER_TOOLS = {
    "node": ("turbo", "lerna", "moon", "moonx", "cdk", "cdktf", "nx", "rush", "rush-pnpm",
             "redocly", "openapi", "firebase", "azurite", "azurite-blob", "azurite-queue",
             "azurite-table", "serverless", "sls", "vercel", "vc", "netlify", "ntl", "wrangler",
             "wrangler2", "cf-wrangler", "spago"),
    "python": ("sam", "semgrep", "ansible", "ansible-playbook", "ansible-galaxy", "molecule",
               "aws", "az", "gsutil", "docker-compose")}
# and the packages npx also takes by name, each of one tool above (`npx aws-cdk deploy` missed
# `*cdk deploy *`'s word, `npx firebase-tools deploy` `*firebase deploy *`)
NODE_PACKAGES = ("aws-cdk", "cdktf-cli", "@microsoft/rush", "@redocly/cli", "firebase-tools",
                 "@moonrepo/cli", "netlify-cli")
# How the runners spell a tool, as deny patterns, `{t}` the tool: npx and its flags (`npx --no
# turbo`) and a version npx takes when it matches the installed one (`npx nx@19 release`);
# pnpm's and yarn's shorthands and filters (`pnpm --filter web exec cdk`, `yarn workspace a nx`);
# bun's; any runner's `run` and `exec` (`npm run cdk` runs a package.json script named for the
# tool, `pnpm exec` and `yarn exec` run any command, `bun run`, `uv run`, `poetry run`, `pixi
# run`, `conda run`); and a bin directory's (`node_modules/.bin/`, `.venv/bin/`, absolute too).
# Each starts with `*`, as OUTWARD's do, so `CI=1` or `env` before it is caught too. A word
# after a flag is taken for the tool, so `pnpm --filter cdk build`, a package named cdk, asks
# once. A flag's quoted command (`npx -c 'turbo run'`) passes, as `bash -c` does.
RUNNER_FORMS = {
    "node": ("*npx {t} *", "*npx -* {t} *", "*pnpm {t} *", "*pnpm -* {t} *", "*yarn {t} *",
             "*yarn -* {t} *", "*yarn workspace * {t} *", "*bun {t} *", "*bun -* {t} *",
             "*run {t} *", "*run -* {t} *", "*exec {t} *", "*exec -* {t} *", "*bin/{t} *"),
    "python": ("*run {t} *", "*run -* {t} *", "*exec {t} *", "*exec -* {t} *", "*bin/{t} *")}
NPX_VERSIONED = ("*npx {t}@*", "*npx -* {t}@*")
# and the anchored spellings an entry is allowed again under: a runner's word, or the path to
# the project's bin directory (an absolute one asks)
RUNNER_PREFIXES = {
    "node": ("npx", "pnpm", "pnpm exec", "yarn", "yarn run", "yarn exec", "bun", "bun run",
             "node_modules/.bin/", "./node_modules/.bin/"),
    "python": ("uv run", "poetry run", "pipenv run", "pdm run", "hatch run", "pixi run",
               ".venv/bin/", "./.venv/bin/", "venv/bin/", "./venv/bin/")}


def _under_runner(entry: str, prefix: str) -> str:
    """ALLOWED entry ``entry`` spelled under ``prefix``: its variables first, then the runner."""
    words = entry.split(" ")
    n = next((i for i, w in enumerate(words)
              if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=\S*", w)), len(words))
    rest = " ".join(words[n:])
    return " ".join([*words[:n], prefix + rest if prefix.endswith("/") else f"{prefix} {rest}"])


def _runner_rules() -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """(deny, allow, ask, listed): the runner forms of the RUNNER_TOOLS not allowed whole, the
    runner spellings of their ALLOWED_COMMANDS entries, of their ASK_FIRST entries and of their
    INSTALL_LISTED ones. Each pattern once: the allows also behind `*=*` (shell_rules)."""
    whole = set(ALLOWED_COMMANDS)
    deny, allow, ask, listed = [], [], [], []
    for eco, tools in RUNNER_TOOLS.items():
        held = [t for t in tools if t not in whole]
        names = held + (list(NODE_PACKAGES) if eco == "node" else [])
        deny += [f.format(t=t) for t in names for f in RUNNER_FORMS[eco]]
        deny += [f.format(t=t) for t in names for f in NPX_VERSIONED if eco == "node"]
        allow += [_under_runner(e, p) for e in ALLOWED_COMMANDS if " " in e
                  and _entry_tool(e) in held for p in RUNNER_PREFIXES[eco]]
        ask += [f.format(t=e[:-2] if e.endswith(" *") else e) for e in ASK_FIRST
                if _entry_tool(e) in tools for f in RUNNER_FORMS[eco]]
        listed += [_under_runner(e, p) for e in INSTALL_LISTED if _entry_tool(e) in tools
                   for p in RUNNER_PREFIXES[eco]]
    return tuple(dict.fromkeys(deny)), tuple(dict.fromkeys(allow)), \
        tuple(dict.fromkeys(ask)), tuple(dict.fromkeys(listed))


RUNNER_DENY, RUNNER_ALLOW, RUNNER_ASK, RUNNER_LISTED = _runner_rules()
RUNNER_HELD = frozenset(t for ts in RUNNER_TOOLS.values() for t in ts if t not in ALLOWED_COMMANDS)


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


def test_allowances(test: str | None) -> tuple[list[str], list[str]]:
    """What `wb start --test` and `wb next --test` grant the task, as (allow, test_tools): the
    acceptance command's own simple commands as written, and with more arguments (`npm test *`)
    and their tools (`npm *`). The protocol tells the agent to run it; refused (`flutter test`,
    `just test`, `scripts/test.sh`), it spent an ask and a Claude wake on a command Claude chose,
    or finished untested (#514, the audit of 1.90). The tools go in a tier of their own, before
    ASK_FIRST (shell_rules): granted with the commands, after it, `--test 'npm test'` allowed
    `npm install left-pad`, `--test 'uv run pytest'` `uv add requests` (#646, the audit of main).
    So does each command with more arguments: after the asks, `--test 'npm install && npm test'`
    allowed `npm install left-pad`, `pip install -e . && pytest` `pip install -e . requests`
    (#915, the third 2026-11-21 audit); only the command as written comes after them.
    A tool on ALLOWED_COMMANDS only by its subcommands (LIMITED) isn't granted whole: `--test
    'terraform validate'` granted `terraform *`, and with it `terraform apply` and `plan`
    (2026-11-22). Nor one that reaches out (OUTWARD_TOOLS): `--test 'terragrunt hcl fmt
    --check'` granted `terragrunt *` and `terragrunt apply`, `pulumi preview` `pulumi up` (the
    review, 2026-11-22). Nor one of RUNNER_HELD, the tools a runner's spelling of is held
    (`moon`, `vercel`, #999). The hard limits come later in the rules, so no grant here lifts them."""
    import shlex
    exact, tools = [], []
    for cmd in _simple_commands(test or ""):
        exact.append(cmd)
        tools.append(f"{cmd} *")
        try:
            words = shlex.split(cmd)
        except ValueError:
            continue
        words = [w for w in words if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", w)]
        # a tool by its path is that tool: `--test 'node_modules/.bin/turbo run test'` granted
        # `node_modules/.bin/turbo *`, `/usr/bin/terraform validate` `/usr/bin/terraform *` (#999)
        if words and "*" not in words[0] and os.path.basename(words[0]) not in \
                LIMITED | OUTWARD_TOOLS | RUNNER_HELD:
            tools += [f"{words[0]} *", f"{ASSIGN} {words[0]} *"]
    return list(dict.fromkeys(exact)), list(dict.fromkeys(tools))


def commands_path() -> Path:
    return Path(os.environ.get("WB_COMMANDS") or Path.home() / ".config" / "workbench" / "commands.json")


def allow_always(key: str, items: list[str]) -> None:
    """Add ``items`` to commands.json's ``key`` ("allow", "paths" or "ports"), for every task (`wb allow
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
    v = conf.get("ports")  # wb allow --port --always (#1197)
    if v is not None and not (isinstance(v, list) and all(
            isinstance(x, int) and not isinstance(x, bool) and 0 < x < 65536 for x in v)):
        raise RuntimeError(f'{path}: "ports" must be a list of TCP port numbers, e.g. [9229]')
    if why := mcp_list_refusal(conf.get("mcp")):
        raise RuntimeError(f"{path}: {why}")
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
        if "setup_timeout" in v and not (isinstance(v["setup_timeout"], int)
                                         and not isinstance(v["setup_timeout"], bool)
                                         and v["setup_timeout"] > 0):
            raise RuntimeError(f'{path}: "repos" → {repo!r}: "setup_timeout" is a whole number '
                               'of seconds, e.g. 3600')
        if v.get("merge", "merge") not in MERGE_AS:
            raise RuntimeError(f'{path}: "repos" → {repo!r}: "merge" is one of {", ".join(MERGE_AS)}')
        # read as a privacy switch: "yes" or 1 is no answer either way (#1021)
        if "local_only" in v and not isinstance(v["local_only"], bool):
            raise RuntimeError(f'{path}: "repos" → {repo!r}: "local_only" is true or false '
                               '(true: API models never get this repo)')
        if why := mcp_list_refusal(v.get("mcp")):
            raise RuntimeError(f'{path}: "repos" → {repo!r}: {why}')
    return conf


# the file length from which the probe experiment's note and protocol line apply: its gain
# showed only on a 2,700-line file, its cost on 1,500-line ones (#857)
PROBE_FILE_LINES = 1500

# Experiments: a change to what the agent may do, not yet measured enough to be supported
# (ARCHITECTURE.md, *Experiments*). Each is off unless the user turns it on: `wb experiment <name> on|off`
# sets it (experiments.json), and `wb start` records its value per task (task.json `experiments`),
# so a flip changes only the tasks started after it. Off, the agent's rules are main's exactly.
EXPERIMENTS = {
    # what on does and all it adds, since `wb experiment` said neither (the review, 2026-11-21).
    # probe is off and on, on is large files only (the owner, 2026-11-22, #857): its levels
    # (adaptable, moderate, aggressive) went, on is what moderate did
    "probe": "on means large files only: from a file over "
             f"{PROBE_FILE_LINES} lines the agent is told to extract the function it needs with "
             "probe instead of reading the file whole, and its handbook says how (a file's outline, "
             "one function, a search); on also caps one shell call's output at a tenth of the "
             "window and outlines the spec's long files in round 1, and is off for a task whose "
             "repo has no such file",
}
# probe's levels before it was off and on (the owner, 2026-11-22, #857: off and on, on is large
# files only): one saved in experiments.json reads as on, and `wb experiment probe <level>` still
# sets on (a running A/B calls `wb experiment probe adaptable`); neither help nor listing shows them
OLD_LEVELS = {"probe": ("adaptable", "moderate", "aggressive")}
# Whether the pane marks probe's on "recommended": Claude sets it once a measurement says on, with
# the read cap and the outline, is at least as good as off (#857's step 6). Measured 2026-11-22 on
# a 27B at 40K, it wasn't (ARCHITECTURE.md, *Experiments*, the third A/B)
PROBE_RECOMMENDED = False
# With probe on, round 1's prompt ends with an outline of each file over PROBE_FILE_LINES the spec
# names (runner.add_outline), at most this many tokens in all, so the agent starts knowing where
# things are instead of exploring for it, as Aider's repo map and Agentless's skeleton do (the
# owner's design, 2026-11-21, #857). Each file's part opens with OUTLINE_HEAD, a prompt text
# (ARCHITECTURE.md, *The runner, inside*), and ends, when cut, with OUTLINE_MORE.
PROBE_OUTLINE_TOKENS = 1000
OUTLINE_HEAD = "Outline of {file} (from probe symbols):"
OUTLINE_MORE = "… {n} more: `probe symbols {file}`"


# what an experiment adds to the agent's copy of the handbook, after its own text. probe aborts
# (SIGABRT, a core dump, a desktop crash dialog) when a pipe closes early: Rust's println! panics
# on EPIPE. Agents piped it into `head` (the A/B of 2026-11-21), so the note steers them to its
# own limits and to `grep`, which reads to the end (#857). OpenCode's `read` returns at most 2,000
# lines or 50 KB, which the layer's `tool_output` cap doesn't bound (runner.output_cap, measured
# on 2.3.2, 2026-11-21): the note says how to read on, since the agent paged through a long file
EXPERIMENT_NOTES = {
    "probe": "\n## Reading code: probe\nWhen you need one function from a file over "
             "{probe_lines} lines, read just that function, not the whole file. You know its name: "
             "`probe extract <file>#<name>`. You don't: `probe symbols <file>` lists each function "
             "and class with its lines (add `--allow-tests` in a test file). To find where a name is "
             "defined or used, `probe search \"name1 OR name2\" <dir> --dry-run -o plain` prints "
             "only files and line ranges; then extract or read those lines. One search already "
             "matches other spellings (case, snake_case, camelCase), so never repeat one or re-spell "
             "it. \"No results\" doesn't mean the name is absent: check with `grep -n`. After two "
             "searches that miss, use `grep -n`. Stop exploring once you know the function you will "
             "change and what calls it. Never pipe probe into `head` (it crashes when its output is "
             "cut off): filter with `grep`. When probe says \"File too large\" (over 1 MB), use "
             "`grep -n` for the names and read those lines. A read of a long file returns only its "
             "first 2,000 lines (or 50 KB): read further with `offset` and `limit`, or `probe "
             "extract` the function.\n",
}
# what an experiment adds to every round prompt's protocol, beside the acceptance line: the
# handbook alone went unread (probe: 0 calls in 14 replays, 2026-11-21). A condition, not an
# order: "before you read a file over 300 lines, run probe symbols" made the agent outline and
# search mid-sized files it didn't need to, filling a 40K window (the second A/B, 2026-11-21:
# 2-3x the calls, a handover in every probe-on replay of big-compaction, #857)
EXPERIMENT_PROTOCOL = {
    "probe": "- When you need one function from a file over {probe_lines} lines, `probe extract "
             "<file>#<name>` it instead of reading the whole file.\n",
}
EXPERIMENT_NOTES = {k: v.replace("{probe_lines}", str(PROBE_FILE_LINES)) for k, v in EXPERIMENT_NOTES.items()}
EXPERIMENT_PROTOCOL = {k: v.replace("{probe_lines}", str(PROBE_FILE_LINES))
                       for k, v in EXPERIMENT_PROTOCOL.items()}
# The most a probe search prints, in tokens, unless the user's PROBE_MAX_TOKENS says otherwise
# (runner.agent_env): it has no limit of its own, and one plain query printed 42K tokens (#857)
PROBE_MAX_TOKENS = 2000

# A read of a whole file longer than this is what probe is for: the round prompt's trigger, and
# what the runner counts (status `long_reads`) beside the experiments' use (#857)
LONG_FILE_LINES = 300


def experiment_use(task: store.Task, row: dict) -> list[str]:
    """The experiments this tool call used: probe when a shell line runs `probe` and the task has
    probe on. Counted live (status `experiment_use`), so an A/B knows within a replay whether the
    agent takes the tool up: the first measurement learnt only at its end that probe had never
    run, in 14 replays (#857)."""
    return ["probe"] if experiment(task, "probe") and "probe" in (row.get("programs") or ()) else []


def long_read(task: store.Task, row: dict) -> int:
    """The length in lines of the file this call read whole, when over LONG_FILE_LINES; else 0."""
    if row.get("tool") != "read" or not row.get("whole") or not row.get("path"):
        return 0
    path = Path(row["path"])
    path = path if path.is_absolute() else task.worktree / path
    try:
        with open(path, "rb") as f:
            n = sum(1 for _ in f)
    except OSError:
        return 0
    return n if n > LONG_FILE_LINES else 0


def experiment_protocol(task: store.Task | None = None, experiments: dict | None = None) -> str:
    """The protocol lines of the experiments this task's agent has (EXPERIMENT_PROTOCOL): the
    task's, or ``experiments`` as task.json will record them (wb start writes round 1's prompt
    before task.json, #873)."""
    have = experiments if experiments is not None else (task.spec.get("experiments") or {}) if task else {}
    return "".join(line for name, line in EXPERIMENT_PROTOCOL.items() if have.get(name))


def experiments_path() -> Path:
    return store.home() / "experiments.json"


def setting(name: str, value) -> str:
    """An experiment's setting as experiments.json or `wb experiment` gives it: "on" or "off".
    "on", True or one of its OLD_LEVELS (probe's, before it was off and on, 2026-11-22) is on;
    anything else is off."""
    return "on" if value is True or value == "on" or value in OLD_LEVELS.get(name, ()) else "off"


def experiments() -> dict[str, str]:
    """Each experiment's setting: off unless experiments.json says otherwise."""
    saved = store.read_json(experiments_path(), {}) or {}
    saved = saved if isinstance(saved, dict) else {}
    return {name: setting(name, saved.get(name)) for name in EXPERIMENTS}


def set_experiment(name: str, value) -> None:
    """Set ``name`` on or off (True and False, or an old level, mean the same: setting)."""
    with store.locked(experiments_path().with_suffix(".lock")):
        saved = store.read_json(experiments_path(), {}) or {}
        saved = saved if isinstance(saved, dict) else {}
        store.write_json(experiments_path(), {**saved, name: setting(name, value)})


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
# The subcommands the agent may run: they read files and print. `probe *` let it run the rest too
# (2026-11-21, testing the experiment): `lsp` starts a daemon and language servers, `--lsp` on a
# read starts them too, `config set|reset` writes probe's config, `benchmark` takes the CPU.
PROBE_READS = ("symbols", "extract", "search", "grep", "query")
PROBE_RELEASES = "github.com/probelabs/probe/releases"


def task_experiments() -> dict:
    """What `wb start` records: each experiment's setting only when set and able to work (probe
    installed), else False, so a task's record says what its agent really had (probe is then off
    where the repo has no long file, probe_off). An experiment that is off isn't checked at all: a `probe` on
    PATH ran on every start with it off, and one printing a non-UTF-8 byte failed the start after
    its branch and worktree were made (#518)."""
    able = {"probe": probe_bin}
    return {name: s if s != "off" and (able[name]() is not None if name in able else True) else False
            for name, s in experiments().items()}


# Probe is off where it can't help (the owner, 2026-11-21, #857: "No sense in clogging up the
# subagents with irrelevant information"): a task's repo needs a file in a language probe reads
# over PROBE_FILE_LINES (probe_off), else the task runs exactly as with probe off.
#
# The languages: `probe search --help`'s `--language` possible values, as probe-code 0.6.0 lists
# them (2026-11-21), each to the file extensions `probe symbols` parses for it, tried on a
# one-function file of each (one it has no parser for comes back as `"kind": "text"` lines, so
# .mjs, .cjs, .mts, .cts, .pyi, .c++ and .cu are left out). The list itself is read from the
# installed probe (probe_languages): a newer probe's new language needs only its row here, and a
# value with no row counts for nothing. Mapped to nothing on purpose: yaml, data rather than code
# (a long YAML, JSON or CSV file is no reason to turn probe on; JSON and CSV aren't in probe's
# list), and swift, which 0.6.0 lists but has no parser for ("Failed to get parser for
# extension: swift").
PROBE_LANGUAGES = {
    "rust": (".rs",), "rs": (".rs",),
    "javascript": (".js",), "js": (".js",), "jsx": (".jsx",),
    "typescript": (".ts",), "ts": (".ts",), "tsx": (".tsx",),
    "python": (".py",), "py": (".py",),
    "go": (".go",),
    "c": (".c", ".h"), "h": (".h",),
    "cpp": (".cpp", ".cc", ".cxx", ".hpp", ".hxx", ".hh"), "cc": (".cc",), "cxx": (".cxx",),
    "hpp": (".hpp", ".hh"), "hxx": (".hxx",),
    "java": (".java",),
    "ruby": (".rb",), "rb": (".rb",),
    "php": (".php",),
    "swift": (),
    "solidity": (".sol",), "sol": (".sol",),
    "crystal": (".cr",), "cr": (".cr",),
    "haskell": (".hs", ".lhs"), "hs": (".hs",), "lhs": (".lhs",),
    "csharp": (".cs",), "cs": (".cs",),
    "yaml": (), "yml": (),
    "bash": (".sh", ".bash"), "sh": (".sh",),
    "qml": (".qml",),
}
# wb status's words for a task where probe was set but is off (task.json `experiments_off`)
PROBE_OFF_SHORT = f"no file over {PROBE_FILE_LINES} lines in a language probe reads"
PROBE_OFF_HELP = "probe's languages couldn't be read from `probe search --help`"
PROBE_OFF_GAVE_UP = (f"no file over {PROBE_FILE_LINES} lines in a language probe reads among "
                     "the largest it counted")
PROBE_OFF_WRITING = "a writing task"  # prose: no code to navigate (#1025)
PROBE_OFF_PARTIAL = "a partial clone, whose files git before 2.44 can't count without fetching each"
# the most of a repo's blobs probe_off counts, largest first, before it gives up (PROBE_OFF_GAVE_UP)
PROBE_SCAN_BYTES = 64 << 20
# A blob sparser than this many bytes per PROBE_FILE_LINES lines is generated (minified, a
# bundle), not code anyone reads by function: probe_off skips the rest of it once its first
# PROBE_SPARSE_AFTER bytes are that sparse. Counted whole, a 70 MiB one-line big.min.js used up
# PROBE_SCAN_BYTES and the 2,000-line a.py beside it was never reached: off (review, #857, 2026-11-21)
PROBE_SPARSE_BYTES = 2 << 20
PROBE_SPARSE_AFTER = 1 << 16


def probe_languages_path() -> Path:
    return store.home() / "probe-languages.json"


def probe_language_names(help_text: str) -> list[str] | None:
    """--language's possible values in `probe search --help`, or None when it lists none: from
    its option line to the next option's, in clap's short form (`[possible values: rust, …]`) or
    its long one (`Possible values:` and a `- rust: …` line each). Searched past its block, a
    --language in the long form read --format's values (terminal, markdown, …) as the languages
    (review, #857, 2026-11-21)."""
    lines = help_text.splitlines()
    at = next((i for i, line in enumerate(lines) if re.match(r"\s*(?:-\w,\s*)?--language\b", line)), None)
    if at is None:
        return None
    block = [lines[at]]
    for line in lines[at + 1:]:
        if re.match(r"\s*-{1,2}[A-Za-z]", line):  # the next option ("- rust: …" has a space)
            break
        block.append(line)
    text = "\n".join(block)
    if m := re.search(r"\[possible values:([^\]]*)\]", text):
        names = [n.strip() for n in m.group(1).split(",")]
    elif "Possible values:" in text:
        names = re.findall(r"^\s*- ([\w+#.-]+)", text.partition("Possible values:")[2], re.M)
    else:
        names = []
    return [n for n in names if n] or None


def probe_languages(exe: str) -> frozenset[str] | None:
    """The file extensions the probe ``exe`` reads (PROBE_LANGUAGES of the languages its help
    lists), or None when its help can't be read or parsed: then probe counts as off, never on.
    Read once per probe version (`probe --version`) and kept in probe-languages.json."""
    def run(*args) -> str | None:
        try:  # any bytes: a `probe` that isn't probelabs' may print anything (#518)
            r = subprocess.run([exe, *args], capture_output=True, text=True, errors="replace",
                               timeout=10, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return r.stdout if r.returncode == 0 else None
    version = (run("--version") or "").strip()
    key = {"probe": os.path.realpath(exe), "version": version}
    saved = store.read_json(probe_languages_path(), {}) if version else {}
    saved = saved if isinstance(saved, dict) else {}
    names = saved.get("languages") if all(saved.get(k) == v for k, v in key.items()) else None
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        names = probe_language_names(run("search", "--help") or "")
        # values none of which PROBE_LANGUAGES knows are another option's, or a probe renamed:
        # not its languages, and not cached (review, #857, 2026-11-21)
        if names is None or not any(n in PROBE_LANGUAGES for n in names):
            return None
        if version:  # a probe that names no version is asked each time
            try:
                store.write_json(probe_languages_path(), {**key, "languages": names, "at": store.now()})
            except OSError:
                pass
    return frozenset(e for n in names for e in PROBE_LANGUAGES.get(n, ()))


def probe_off(spec: str, repo: Path, sub: str | None, rev: str | None) -> tuple[str | None, dict[str, int]]:
    """(why probe is off for this task, or None; the long files its spec names in probe's
    languages, from long_files). On only when the repo at commit ``rev`` has a file in a language
    probe reads (probe_languages) over PROBE_FILE_LINES: one the spec names anywhere in the repo,
    or any in the task's package ``sub`` (the whole repo without one), where the agent works.
    Counted from git's objects, largest first, lines a chunk at a time and only until one is past
    the limit, never a file read whole (_Lines), a sparse blob (PROBE_SPARSE_BYTES) no further
    than its first chunks, at most PROBE_SCAN_BYTES in all. A submodule's files don't count: its
    gitlink is no blob here, and its checkout in the worktree is read-only
    (gitops.fill_submodules), another project's code the task can't change. Nor symbolic links,
    nor a file outside a sparse checkout's cone, which the task's worktree doesn't have either
    (gitops.apply_sparse). A partial clone's blobs are counted only where it holds them: asked
    with lazy fetching off (git 2.44+), as gitops.checkout_size asks (#436); an older git's would
    each be fetched, so only the named files count there (PROBE_OFF_PARTIAL). (the owner,
    2026-11-21, #857; the review's sparse, partial and minified cases, the same day)"""
    exe = probe_bin()
    kinds = probe_languages(exe) if exe else None
    if kinds is None:
        return PROBE_OFF_HELP, {}
    keeps = gitops.sparse_keeps(repo) if rev else None
    named = {rel: n for rel, n in long_files(spec, repo, sub, rev, kinds).items()
             if not keeps or keeps(rel)}
    if named:
        return None, named
    if not rev or not kinds:
        return PROBE_OFF_SHORT, {}
    lazy = gitops.partial_clone(repo)
    if lazy and (gitops.version() or (0,)) < (2, 44):
        return PROBE_OFF_PARTIAL, {}
    no_fetch = {"GIT_NO_LAZY_FETCH": "1"} if lazy else None  # a blob it lacks: size BAD, missing
    try:
        tree = gitops.git_bytes(repo, "ls-tree", "-r", "-l", "-z", "--full-tree", rev,
                                *(["--", sub] if sub else []), env=no_fetch)
    except gitops.GitError:
        return PROBE_OFF_SHORT, {}
    found = []
    for entry in tree.split(b"\0"):
        meta, _, path = entry.partition(b"\t")
        meta = meta.split()
        if len(meta) != 4 or meta[1] != b"blob" or meta[0] == b"120000" or not meta[3].isdigit():
            continue
        name = os.fsdecode(path)
        # a file of more than N lines has more than N bytes: the rest can't be long
        if int(meta[3]) > PROBE_FILE_LINES and os.path.splitext(name)[1] in kinds and \
                (not keeps or keeps(name)):
            found.append((int(meta[3]), meta[2].decode()))
    found.sort(reverse=True)
    now: dict[str, _Lines] = {}  # the blob being fed: one at a time, each once
    read = {"bytes": 0, "blob": 0, "long": False, "spent": False}

    def feed(oid: str, chunk: bytes):
        if oid not in now:
            now.clear()
            now[oid] = _Lines()
            read["blob"] = 0
        lines = now[oid]
        lines.update(chunk)
        read["bytes"] += len(chunk)
        read["blob"] += len(chunk)
        if lines.count() > PROBE_FILE_LINES:
            read["long"] = True
            return True
        if read["bytes"] >= PROBE_SCAN_BYTES:
            read["spent"] = True
            return True
        if read["blob"] >= PROBE_SPARSE_AFTER and \
                read["blob"] > (lines.count() + 1) * PROBE_SPARSE_BYTES // PROBE_FILE_LINES:
            return gitops.SKIP_BLOB  # generated: the next blob
        return False

    seen: set[str] = set()  # two files alike are one blob, counted once
    oids = [oid for _, oid in found if not (oid in seen or seen.add(oid))]
    if oids:
        gitops.feed_blobs(repo, oids, feed, env=no_fetch)
    if read["long"]:
        return None, {}
    return (PROBE_OFF_GAVE_UP if read["spent"] else PROBE_OFF_SHORT), {}


def experiment(task: store.Task, name: str) -> bool:
    """Whether this task's agent has experiment ``name``: task.json records "on" (or False). A
    task from before probe was off and on recorded its level ("moderate", "aggressive") or True:
    on too, with on's protocol line (the owner, 2026-11-22, #857: off and on, on is large files
    only)."""
    return bool((task.spec.get("experiments") or {}).get(name))


def brace_names(text: str) -> list[str]:
    """Each path a brace form names, `src/replay_io.{h,c}`: src/replay_io.h, src/replay_io.c
    (flagged as unnamed by outside_spec on 2026-11-18)."""
    return [a + part + b for a, parts, b in
            re.findall(r"([\w./-]*)\{([\w.-]+(?:,[\w.-]+)+)\}([\w./-]*)", text)
            for part in parts.split(",")]


# a path in a spec: a name with an extension, maybe under directories (`workbench/cli.py`,
# `cli.py`, `./src/a.c:120`, `a.py#run`)
SPEC_PATH = re.compile(r"(?<![\w/.-])((?:\.{1,2}/)?(?:[\w.-]+/)*[\w-][\w.-]*\.[A-Za-z]\w*)(?![\w/-])")
SPEC_PATHS_MAX = 40  # the paths a spec names that are looked at, in order


def spec_paths(text: str) -> list[str]:
    """The relative file paths a spec's text names, in order, once each (brace forms too)."""
    seen: list[str] = []
    for p in SPEC_PATH.findall(text + "\n" + " ".join(brace_names(text))):
        p = p[2:] if p.startswith("./") else p
        if p not in seen and ".." not in Path(p).parts:
            seen.append(p)
    return seen[:SPEC_PATHS_MAX]


def long_files(text: str, root: Path, sub: str | None = None, rev: str | None = None,
               kinds: frozenset[str] | None = None) -> dict[str, int]:
    """{path from ``root``: its lines} for each file the spec names with more than
    PROBE_FILE_LINES lines, in the order named: read in the checkout ``root`` (the worktree), or
    at commit ``rev`` of the repo ``root`` (wb start, before the worktree exists). A name is
    tried in the package ``sub`` first, then at the top; one outside ``root`` is none. With
    ``kinds``, only names with one of those extensions (probe's languages: probe_languages)."""
    out: dict[str, int] = {}
    top = os.path.realpath(root)
    for p in spec_paths(text):
        if kinds is not None and os.path.splitext(p)[1] not in kinds:
            continue  # the outline only for files probe reads (#857, 2026-11-21)
        for rel in ([f"{sub}/{p}"] if sub else []) + [p]:
            # counted a chunk at a time: read whole, a spec naming a large data file loaded all
            # of it, at wb start inside Claude's Bash limit (review, 2026-11-21)
            lines = _Lines()
            if rev:
                try:
                    gitops.git_hash(root, lines, "cat-file", "blob", f"{rev}:{rel}")
                except gitops.GitError:
                    continue
            else:
                real = os.path.realpath(Path(root) / rel)
                if not real.startswith(top + os.sep) or not os.path.isfile(real):
                    continue
                try:
                    with open(real, "rb") as f:
                        while chunk := f.read(1 << 16):
                            lines.update(chunk)
                except OSError:
                    continue
            n = lines.count()
            if n > PROBE_FILE_LINES:
                out.setdefault(rel, n)
            break
    return out


class _Lines:
    """A file's lines, fed a chunk at a time (as gitops.git_hash feeds a hash): its newlines,
    and one more for a last line without one."""

    def __init__(self):
        self.n, self.last = 0, b"\n"

    def update(self, chunk: bytes) -> None:
        if chunk:
            self.n += chunk.count(b"\n")
            self.last = chunk[-1:]

    def count(self) -> int:
        return self.n + (self.last != b"\n")


# How wb accept --merge lands: a merge commit, fast-forward only, or one squashed commit (#352)
MERGE_AS = ("merge", "ff", "squash")


def repo_conf(repo: str | Path, sub: str | None = None) -> dict:
    """commands.json's `repos` entry for the task's spot, the repo or its package ``sub``
    (task.json `subdir`), or {}: its `setup` command and its limit (`setup_timeout`, #520), the
    ignored paths to `copy` into a task's worktree, its `siblings` (#201), how `--merge` lands
    there (`merge`, #352). The key nearest the spot wins: the spot itself, else the nearest
    directory above it inside the repo, matched by real path, else the same by git dir, as the
    path inside its worktree: a linked worktree of a configured repo is that repo (#498). Any
    key once matched by git dir, so in a monorepo the first package's entry (its setup, its
    copies, its merge) applied to every task of the repo, the root's and the other packages' (#778)."""
    return next((v for _, v in repo_entries(repo, sub)), {})


def repo_conf_dir(repo: str | Path, sub: str | None = None) -> str:
    """The directory of repo_conf's key inside the repo, as ``sub`` names it: '' for the top (or
    no key). The spot itself or a directory above it, so a prefix of ``sub``, matched by real path
    or by git dir as repo_entries does. A package key's `copy` paths are read from it: from the
    root they copied the root's node_modules into every task of the package (#1346, 2026-11-24)."""
    entries = repo_entries(repo, sub)
    if not entries or not sub:
        return ""
    fold = store.folds_case(repo)
    real = lambda p: os.path.realpath(p).lower() if fold else os.path.realpath(p)  # noqa: E731
    kp, top = Path(entries[0][0]).expanduser(), real(repo)
    kr, spot = real(kp), real(Path(repo) / sub)
    # by path only when the spot is at or under the key, repo_entries' `near`: a key on a linked
    # worktree under the top (.claude/worktrees/x) matches the main checkout by git dir, and its
    # depth from the top, three parts, read the root key's node_modules as the package's (#1346's
    # review, 2026-11-24)
    if (kr == top or kr.startswith(top.rstrip(os.sep) + os.sep)) \
            and (spot == kr or spot.startswith(kr.rstrip(os.sep) + os.sep)):
        rel = os.path.relpath(kr, top)
    else:  # by git dir: the key's path inside its own worktree
        try:
            rel = os.path.relpath(kr, real(gitops.toplevel(kp)))
        except gitops.GitError:
            return ""
    depth = 0 if rel == "." else len(Path(rel).parts)
    return "/".join(Path(sub.strip("/")).parts[:depth])


def repo_entries(repo: str | Path, sub: str | None = None,
                 whole: bool = False) -> list[tuple[str, dict]]:
    """Every commands.json `repos` entry for the task's spot, nearest first, as (key, entry):
    repo_conf's rule, whose first is the one that applies (the spot, else the nearest directory
    above it, by real path, else by git dir with the path inside the worktree). ``whole``: every
    entry for what a task's checkout holds, at or under the repo's top by path or by git dir (a
    repo of its own nested under the top left out), the spot's first: privacy.local_only reads
    them all, since a directory marked alone once went to the API model with every task started
    outside it (#1255, 2026-11-24)."""
    repos = commands_conf().get("repos") or {}
    if not repos:
        return []
    # real paths, in lower case where the file system ignores case (macOS), as cli._scope's: a
    # key typed ~/Code/ClientX for git's ~/code/clientx matched neither by path nor by git dir,
    # and a local-only repo's task went to the API model (#1157, 2026-11-24)
    fold = store.folds_case(repo)
    real = lambda p: os.path.realpath(p).lower() if fold else os.path.realpath(p)  # noqa: E731
    top = real(repo)
    spot = real(Path(repo) / sub) if sub else top
    common = gitops.common_dir(repo)

    def within(p: str, d: str) -> bool:
        return d == "." or p == d or p.startswith(d.rstrip(os.sep) + os.sep)

    def ours(kp: Path) -> bool:  # a key under the top that isn't a repo of its own nested there
        kc = gitops.common_dir(kp)
        return kc is None or common is None or real(kc) == real(common)

    # (far from the spot, nearest first, key, entry): by path, then by git dir
    by_path = sorted((not near, -len(kr), k, v) for k, v in repos.items()
                     if within(kr := real(Path(k).expanduser()), top)
                     and ((near := within(spot, kr)) or whole and ours(Path(k).expanduser())))
    if not whole and by_path:
        return [(k, v) for *_, k, v in by_path]
    found, rel_spot = [], os.path.relpath(spot, top)
    for k, v in repos.items() if common else ():
        kp = Path(k).expanduser()
        # under the top, only whole mode skips a key (by_path holds it then): a key on a linked
        # worktree nested there (Claude Code's .claude/worktrees/<name>) is the main checkout's by
        # git dir, and skipping it always lost its setup (#1255's review, 2026-11-24)
        if whole and within(real(kp), top) or (kc := gitops.common_dir(kp)) is None \
                or real(kc) != real(common):
            continue
        try:
            rel = os.path.relpath(real(kp), real(gitops.toplevel(kp)))
        except gitops.GitError:
            continue
        if (near := within(rel_spot, rel)) or whole:
            found.append((not near, 0 if rel == "." else -(len(rel) + 1), k, v))
    found.sort(key=lambda x: x[:2])
    if not whole:
        return [(k, v) for *_, k, v in found]
    # the spot's own first (by path, then by git dir), then the rest the same
    return [(k, v) for far in (False, True) for rows in (by_path, found)
            for f, _, k, v in rows if f is far]


def shell_rules(task: store.Task) -> list[dict]:
    """Deny by default, allow the everyday tools, ask before new dependencies, then what Claude
    and the user allowed; the hard limits last, so no allowance lifts them (later rules win)."""
    rules = [{"action": "shell", "resource": "*", "effect": "deny"}]
    rules += [{"action": "shell", "resource": r, "effect": "allow"}
              for c in ALLOWED_COMMANDS for r in (f"{c} *", f"{ASSIGN} {c} *")]  # `c *` is `c` too
    if experiment(task, "probe"):  # read-only: an outline, one function, a search
        rules += [{"action": "shell", "resource": r, "effect": "allow"}
                  for c in PROBE_READS for r in (f"probe {c} *", f"{ASSIGN} probe {c} *")]
        rules += [{"action": "shell", "resource": r, "effect": "deny"}
                  for r in ("probe *--lsp*", f"{ASSIGN} probe *--lsp*")]
    rules += [{"action": "shell", "resource": r, "effect": "allow"}
              for b in ("./*", "../*", *PROJECT_BINS) for r in (b, f"{ASSIGN} {b}")]
    # and what it built anywhere in its lane, by path: a debug binary in the temp directory was
    # refused (2026-11-16) though `bash` could run it; the lane, not this list, bounds writes
    rules += [{"action": "shell", "resource": f"{d}/*", "effect": "allow"}
              for d in (writable(task) or []) if os.path.isdir(d)
              and not d.startswith(("/dev", "/proc"))]
    # the acceptance command's tools (test_allowances): before the asks, which still win (#646)
    test_tools = grants(task).get("test_tools") or []
    rules += [{"action": "shell", "resource": r, "effect": "allow"} for r in test_tools]
    # the runner forms of the tools the rules name (`npx turbo`, `uv run semgrep`) get their
    # plain form's verdict (#999, 2026-11-22): after those tools, so `--test 'npx vitest run'`
    # granting `npx *` doesn't lift them, then each tool's entries under each runner, and the
    # acceptance command's own lines with more arguments (`npx turbo run test *` under `--test
    # 'npx turbo run test'`, as plain `turbo run test *`), never a tool granted whole
    rules += [{"action": "shell", "resource": p, "effect": "deny"} for p in RUNNER_DENY]
    rules += [{"action": "shell", "resource": r, "effect": "allow"}
              for p in RUNNER_ALLOW for r in (f"{p} *", f"{ASSIGN} {p} *")]
    rules += [{"action": "shell", "resource": r, "effect": "allow"} for r in test_tools
              if len(r.removeprefix(f"{ASSIGN} ").split(" ")) > 2]
    # the outward steps: after those tools, which can't lift them, before what Claude allowed
    rules += [{"action": "shell", "resource": p, "effect": "deny"} for p in OUTWARD]
    pip, theirs = python_installs(task)  # into the worktree's venv, not the user's (#490)
    rules += [{"action": "shell", "resource": r, "effect": "deny"}
              for p in (*ASK_FIRST, *(ENV_INSTALLS if theirs else ())) for r in (p, f"{ASSIGN} {p}")]
    rules += [{"action": "shell", "resource": p, "effect": "deny"} for p in RUNNER_ASK]
    # pipenv isn't pip: it installs into the active venv, else makes its own in the worktree
    # (PIPENV_VENV_IN_PROJECT), lane or no lane. Taken for a pip form by the "pip" in its name,
    # `pipenv install` asked first wherever Landlock isn't, every macOS task (#950)
    listed = [p for p in INSTALL_LISTED
              if (not theirs if p.startswith("pipenv") else pip or "pip" not in p)]
    rules += [{"action": "shell", "resource": r, "effect": "allow"}
              for p in (*listed, *RUNNER_LISTED) for r in (p, f"{ASSIGN} {p}")]
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
              ".local/share/NuGet", ".local/state/cabal", ".android",
              # the homes of ecosystems #672 allowed: `nimble install -d` writes ~/.nimble/pkgs2,
              # `elm make` in a fresh worktree takes ~/.elm's package lock, Clojure's git deps
              # fill ~/.gitlibs, and each was refused (#911, the third 2026-11-21 audit)
              ".nimble", ".elm", ".gitlibs",
              # the caches of tools #994 allowed (2026-11-22): Godot's user:// data (app_userdata,
              # what `godot --headless --script` writes; its editor settings, which can hold a
              # keystore's password, are ~/.config/godot, not here), LuaLaTeX's font cache in
              # TeX Live's TEXMFVAR (`.texlive` on Arch, `.texlive2025` from TeX Live's
              # installer; not texmf-config, read by every later run), dub's and xmake's packages
              # and caches (not their settings), zef's store, cpanm's work directory (carton's),
              # Foundry's RPC and Etherscan cache (not its keystores), tflint's plugins, Bicep's
              # module cache
              ".local/share/godot", ".texlive*/texmf-var", ".dub/packages", ".dub/cache",
              ".xmake/packages", ".xmake/cache", ".zef/store", ".cpanm", ".foundry/cache",
              ".tflint.d", ".bicep",
              # macOS's ~/.cache (#1194): Go's build cache, pip's, Yarn's, node-gyp's and
              # Playwright's browsers are there, and Seatbelt would refuse every build's write;
              # whole, Apple's Python bytecode in it denied by path (NOT_WRITABLE, write_denied)
              "Library/Caches")
# where the user moved them: a toolchain follows its variable, not the default path; GOPATH and
# BUN_INSTALL name a whole home (GOPATH may hold every project), so only their caches. The second
# line's were refused where the user moved them, HF_HOME though ENV_KEEP passes it on (#911)
TOOL_HOME_VARS = ("CARGO_HOME", "RUSTUP_HOME", "GOPATH", "GOMODCACHE", "GOCACHE", "npm_config_cache",
                  "PIP_CACHE_DIR", "UV_CACHE_DIR", "GRADLE_USER_HOME", "BUN_INSTALL", "DENO_DIR",
                  "MIX_HOME", "HEX_HOME", "PUB_CACHE", "KONAN_DATA_DIR", "HATCH_DATA_DIR",
                  "WORKON_HOME", "CARGO_TARGET_DIR", "NPM_CONFIG_CACHE", "NUGET_PACKAGES",
                  "CCACHE_DIR", "SCCACHE_DIR", "YARN_CACHE_FOLDER", "PRE_COMMIT_HOME", "STACK_ROOT",
                  "CABAL_DIR", "JULIA_DEPOT_PATH", "CONAN_HOME", "PLATFORMIO_CORE_DIR", "HF_HOME",
                  "TORCH_HOME", "ELM_HOME", "NIMBLE_DIR")
# the variables whose every entry is a home, not only the first: Julia stacks its depots (#911)
ALL_ENTRIES = ("JULIA_DEPOT_PATH",)
# TOOL_HOMES' entries under these follow the variable, as secret_paths does (#911)
XDG_BASES = (("XDG_DATA_HOME", ".local/share/"), ("XDG_STATE_HOME", ".local/state/"))
# The SDKs a build reads by variable, not from PATH: with only Android's platform-tools on PATH,
# Gradle couldn't open ~/Android/Sdk/platforms' android.jar, and every build and test failed
# (#437, the audit of 1.8). Their targets are readable, and so are the SDKs' default places.
SDK_VARS = ("ANDROID_HOME", "ANDROID_SDK_ROOT", "JAVA_HOME", "FLUTTER_ROOT", "DOTNET_ROOT")
SDK_DEFAULTS = ("Android/Sdk", "Library/Android/sdk")
VAR_CACHES = {"GOPATH": "pkg", "BUN_INSTALL": "install/cache"}
# inside a toolchain home in the lane, what stays out of it: the programs on the user's PATH and
# what their shell sources, and the toolchains rustup's proxies run (relative to the home, or to
# CARGO_HOME and RUSTUP_HOME for theirs); no grant reaches them (kept_out, #912), so `cargo
# install` or a toolchain a rust-toolchain.toml names that isn't installed yet is Claude's to do
NOT_WRITABLE = (".cargo/bin", ".cargo/env", ".dotnet/tools", ".cabal/bin", ".ghcup/bin",
                ".rustup/toolchains", ".nimble/bin",
                # #1194's fourth review (2026-11-24): cargo's own config, which it runs from at
                # the user's next build (build.rustc-wrapper, build.rustc, a target's linker or
                # runner), outside the lane. Seatbelt keeps ~/.cargo whole, so the agent could
                # make it; Landlock's split refused a new one but granted one already there.
                # Cargo never writes it (`cargo login` writes credentials.toml)
                ".cargo/config.toml", ".cargo/config",
                # #1194's review (2026-11-24): Apple's /usr/bin/python3 (the Command Line
                # Tools') keeps its bytecode here (sys.pycache_prefix, its stdlib on a read-only
                # volume) and loads a .pyc whose recorded mtime and size match the source's, so
                # one planted in the writable Library/Caches ran as the user, outside the lane,
                # at the next node-gyp, Xcode build script or shell's python3. Seatbelt denies
                # its writes inside the whole Library/Caches (write_denied), made or not
                "Library/Caches/com.apple.python",
                # #1311's review (2026-11-25): OpenCode's package cache, where `opencode plugin
                # add` installs the OpenCode plugin from Git (packages/util/src/npm.ts: global.cache
                # /npm/<key>/<generation>/node_modules), and every npm plugin and provider package
                # beside it. The user's OpenCode, outside the lane, imports what is there at its
                # next start with no integrity check (npm.resolve; npm.add trusts an active
                # generation), and the plugin holds the consent gates and runs wb as the user.
                # Made first (MADE_FIRST), so a generation can't be planted before the install
                ".cache/opencode/npm")
# and the same places in the homes TOOL_HOME_VARS moved: nimble and cabal install programs into
# their bin/ (#911); OpenCode's package cache where XDG_CACHE_HOME moves it (#1311's review)
VAR_NOT_WRITABLE = {"CARGO_HOME": ("bin", "env", "config.toml", "config"),
                    "RUSTUP_HOME": ("toolchains",), "NIMBLE_DIR": ("bin",), "CABAL_DIR": ("bin",),
                    "XDG_CACHE_HOME": ("opencode/npm",)}
# a home holding one is made with it first, so the split keeps it out before the tool makes it
# (else the agent could make it and plant a program there)
MADE_FIRST = {".rustup": "toolchains", ".nimble": "bin", "RUSTUP_HOME": "toolchains",
              "NIMBLE_DIR": "bin", ".cache": "opencode/npm", "XDG_CACHE_HOME": "opencode/npm"}
# and what the agent's own OpenCode makes in its cache directory beside npm/ (ripgrep, downloaded
# into bin/ when none is on PATH: core/src/ripgrep/binary.ts), made first: the cache directory is
# split around npm/ and refuses a new entry (#1311's review)
OPENCODE_CACHE_MADE = ("bin/",)
# and what cargo makes at the top of its split home, made first so the split grants it: a split
# home refuses a new entry (the 0.90 review: a fresh rustup's ~/.cargo, only bin/ and env, failed
# every build with a dependency, and a git dependency failed wherever git/ didn't exist yet)
CARGO_MADE = ("registry/", "git/", ".package-cache", ".package-cache-mutate", ".global-cache")
# and nimble's, for the same reason: its home is split around bin/ (MADE_FIRST), so a fresh or
# half-made ~/.nimble refused pkgs2/ on the first `nimble install -d`, buildtemp/ on every
# package build, pkgcache/, nimbledata2.json, and packages_temp.json, which every `nimble
# refresh` writes before copying it over packages_official.json (#911's review). The data file
# gets nimble's own empty contents (nimbledatafile.nim's newNimbleDataNode): it parses any
# nimbledata2.json it finds, so an empty one would fail every command. A package list can't be
# made ahead (an empty one reads as a broken list, not a missing one), so a home without one
# needs a first `nimble refresh` from Claude.
NIMBLE_MADE = ("pkgs2/", "pkgcache/", "buildtemp/", "packages_temp.json", "nimbledata2.json")
MADE_TEXT = {"nimbledata2.json": '{"version": 1, "reverseDeps": {}}\n'}
# What the common tools make in a cache home split around an active venv (poetry's, from
# `poetry shell` or direnv), made first so the split grants them: Landlock can only allow, so a
# split home refuses every new entry, and uv's, Go's and pre-commit's first run failed (#825)
CACHE_MADE = ("uv", "pip", "go-build", "pre-commit")
# and what Hugging Face's libraries make at their home's top (huggingface_hub's and datasets'
# constants: HF_HUB_CACHE, HF_XET_CACHE, HF_ASSETS_CACHE, HF_DATASETS_CACHE, HF_MODULES_CACHE)
HF_MADE = ("hub/", "xet/", "assets/", "datasets/", "modules/")
# and what a tool makes at the top of its home when a secret inside splits it (SECRETS): with
# Maven's master password in ~/.m2, a first build couldn't make repository/ there, nor the
# wrapper (./mvnw, its base ~/.m2) its wrapper/ (#992, 2026-11-22)
SPLIT_MADE = {".m2": ("repository/", "wrapper/"),
              # and in an API task's lane, where Gradle's gradle.properties, Hex's hex.config and
              # NuGet's NuGet.Config are hidden too when they hold a credential (API_HIDDEN,
              # #1021): what Gradle makes at its user home's top, Hex's package cache, NuGet's
              # global packages folder (dotnet writes NuGet.Config on first use, so a first
              # restore couldn't make packages/)
              ".gradle": ("caches/", "daemon/", "native/", "wrapper/", "jdks/", "notifications/",
                          ".tmp/", "build-scan-data/"),
              ".hex": ("packages/",), ".nuget": ("packages/",),
              # and ~/.cache, split around Hugging Face's token (#1092) or anything hidden there
              # (HISTFILE=~/.cache/zsh/history, on the GPU too): its common tools' caches, and in
              # Hugging Face's home, split only around its own token, its caches. Keyed apart:
              # under ".cache" a GPU task made ~/.cache/huggingface/* on machines that never used
              # it (#1092's review, 2026-11-23). Another tool's first new entry is refused, the
              # report naming its grant
              ".cache": tuple(f"{n}/" for n in CACHE_MADE), ".cache/huggingface": HF_MADE}
# and the same homes where the user moved them, (variable, below it, SPLIT_MADE's key):
# API_HIDDEN_VARS hides the file inside those too
SPLIT_MADE_VARS = (("GRADLE_USER_HOME", "", ".gradle"), ("HEX_HOME", "", ".hex"),
                   ("XDG_CACHE_HOME", "", ".cache"), ("HF_HOME", "", ".cache/huggingface"),
                   ("XDG_CACHE_HOME", "huggingface", ".cache/huggingface"))
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
    if real in secret_sockets():
        return None
    for h in secret_paths() | private_paths() | {os.path.realpath(skills.library())}:
        if real == h or _under(real, h):
            return (f"{raw}: in {h}, which holds credentials or workbench's own state and stays "
                    "out of every lane; take what the task needs from it yourself")
    return None


def private_paths() -> set[str]:
    """Workbench's own files, real paths, which no agent reads or writes: the state directory
    (the rules, notify.jsonl, the chat log, other tasks) but the task's own files (readable), and
    the config (the chat token, commands.json, routes.json). The 2026-11-16 audit read the chat
    token from a lane, which would let an agent post to Claude as the user."""
    paths = [store.home(), Path.home() / ".config" / "workbench", commands_path(),
             chat.token_path(), chat.config_path(), models.overrides_path(), images.config_path(),
             audio.config_path(), models.lease_dir(),  # a shared GPU lease too: flock on it wedges every account's (#393)
             routes.path()]  # the roles, wherever WB_ROUTES puts them (#1014)
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
    2026-11-16 audit: GOPATH=~/src, or ~/go with the repo in ~/go/src). A removed linked
    worktree's: the repository's main worktree and its git dir (gitops.repo_of), not the removed
    path and a `.git` under it (#776)."""
    checkout, git = _repo_parts(task)
    return checkout | git


def _repo_parts(task: store.Task) -> tuple[set[str], set[str]]:
    """repo_dirs as (the checkout, the git directory), real paths."""
    repo = task.spec.get("repo")
    if not repo:
        return set(), set()
    real = gone = os.path.realpath(repo)
    if not os.path.isdir(real):
        real = os.path.realpath(gitops.repo_of(task.spec))
    if real not in _COMMON:
        try:
            _COMMON[real] = os.path.realpath(os.path.join(real, gitops.git(
                Path(real), "rev-parse", "--git-common-dir").strip()))
        except (gitops.GitError, OSError):
            return {gone, real}, {os.path.join(real, ".git")}
    return {gone, real}, {_COMMON[real]}


def read_repo_dirs(task: store.Task) -> set[str]:
    """What the lane reads of the task's repo (readable): the checkout and its git directory, or
    for an API task the git directory and the files its config includes (_config_includes),
    which the worktree's git needs. Every file an API agent reads goes to the provider, and a
    gitignored .env in the checkout was one `cat` away, though wb start refuses to copy it unless
    it is named (#1091, 2026-11-23); the instruction files only the checkout holds go as copies
    (instruction_files)."""
    checkout, git = _repo_parts(task)
    return git | _config_includes(checkout, git) if task.spec.get("via") == "api" else checkout | git


def _config_includes(checkout: set[str], git: set[str]) -> set[str]:
    """The files the repo's own git config includes outside its git directory, real paths: with
    `include.path = ../.gitconfig` (a tracked .gitconfig of aliases, filters or merge drivers)
    every git call in an API lane, which reads no checkout, died "bad config line" (#1091's
    review, 2026-11-23). git's own `--show-origin` names them, relative to the top."""
    found: set[str] = set()
    for top in sorted(checkout):
        if not os.path.isdir(top):
            continue
        try:
            out = os.fsdecode(gitops.git_bytes(Path(top), "config", "--local", "--includes",
                                               "--list", "--show-origin", "-z"))
        except (gitops.GitError, OSError):
            continue
        for origin in out.split("\0")[::2]:
            if origin.startswith("file:"):
                f = os.path.realpath(os.path.join(top, origin[len("file:"):]))
                if not any(f == g or _under(f, g) for g in git):
                    found.add(f)
        break
    return found


# What a task's directory holds beside its worktree: a sibling's link (`../<name>` from the
# worktree) may not take one of these names. A new file in the task directory goes here too.
TASK_FILES = ("acked", "ask", "base-tests.txt", "claude-sessions", "context.md", "events.jsonl", "images", "noticed", "prepared.json", "rounds",
              "left-out", "runner.log", "setup.txt", "shots", "status.json", "steer.jsonl", "steps.bundle", "task.json",
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


def tool_homes() -> list[Path]:
    """TOOL_HOMES' places: under the home, or under XDG_DATA_HOME and XDG_STATE_HOME where those
    are set (pnpm's store, hatch's and cabal's were refused there, #911). An entry with a `*` is
    each place it matches (TeX Live's `.texlive2025`, #994)."""
    home, out = Path.home(), []
    for t in TOOL_HOMES:
        var = next((v, b) for v, b in (*XDG_BASES, (None, "")) if t.startswith(b))
        base, rel = (Path(os.environ[var[0]]), t[len(var[1]):]) \
            if var[0] and os.environ.get(var[0]) else (home, t)
        out += [Path(g) for g in sorted(glob.glob(f"{glob.escape(str(base))}/{rel}"))] \
            if "*" in rel else [base / rel]
    return out


def tool_var_homes() -> list[str]:
    """The toolchain homes TOOL_HOME_VARS name, each that is set (its first entry, every one for
    ALL_ENTRIES, its cache for VAR_CACHES' whole homes)."""
    out = []
    for v in TOOL_HOME_VARS:
        if os.environ.get(v):
            entries = os.environ[v].split(os.pathsep)
            for e in entries if v in ALL_ENTRIES else entries[:1]:
                if os.path.isabs(e):  # a relative CARGO_TARGET_DIR is the build's own, in the repo
                    out.append(os.path.join(e, VAR_CACHES[v]) if v in VAR_CACHES else e)
    return out


def tool_home_of(real: str) -> str | None:
    """The toolchain home (TOOL_HOMES under the home, a TOOL_HOME_VARS target) that the real path
    ``real`` lies in, or the one beneath it when it holds exactly one (`~/go` for `go/pkg`), as a
    real path; None when it is in none. A home not made yet isn't in the lane (writable keeps
    what exists), and the walk up from a refused `~/.npm/_cacache` reached the home directory,
    which no grant may name, so a first `npm ci` or `go test` named no grant at all (#824)."""
    homes = dict.fromkeys(os.path.realpath(h) for h in (
        *tool_homes(), *tool_var_homes()))
    homes.pop(os.path.realpath(Path.home()), None)
    within = [h for h in homes if real == h or _under(real, h)]
    if within:
        return max(within, key=len)  # the nearest: GOMODCACHE inside ~/.cache, say
    below = [h for h in homes if _under(h, real)]
    return below[0] if len(below) == 1 else None


def not_writable_paths() -> list[Path]:
    """NOT_WRITABLE's places under the home and VAR_NOT_WRITABLE's in the homes those variables
    name, made or not."""
    return [*(Path.home() / h for h in NOT_WRITABLE),
            *(Path(os.environ[v]) / n for v, names in VAR_NOT_WRITABLE.items()
              if os.environ.get(v) for n in names)]


def not_writable(made_only: bool = True) -> set[str]:
    """NOT_WRITABLE's places, CARGO_HOME's, RUSTUP_HOME's and the like's own among them, as real
    paths: those that exist, or with ``made_only`` false every one (lane_grant: a grant made
    before cargo or rustup runs must not hold their bin/ either)."""
    return {os.path.realpath(p) for p in not_writable_paths()
            if not made_only or os.path.lexists(p)}


def split_points() -> set[str]:
    """What a grant is split around, real paths (lane_grant): the secrets and workbench's own
    files, the user's active Python environments (#825: the test report named `~/.cache`, which
    a grant split around the same venv again) and NOT_WRITABLE's places, made or not."""
    return _hidden() | set(python_envs()) | not_writable(made_only=False)


def kept_out(raw: str) -> str | None:
    """Why the lane drops a grant of ``raw`` whole, or None: it is (in) a place writable() always
    splits out, an active Python environment or a NOT_WRITABLE place, made or not. `wb allow
    --path ~/.cargo/bin` was saved and the agent told to run it again, and its retry was refused
    the same way (#912, the third 2026-11-21 audit)."""
    real = os.path.realpath(Path(raw).expanduser())
    for h in sorted(set(python_envs()) | not_writable(made_only=False)):
        if real == h or _under(real, h):
            return (f"{raw}: in {h}, which the lane always keeps out (what is there runs later "
                    "outside the lane, as you); install the toolchain or package there yourself")
    return None


def gem_home_of(real: str) -> str | None:
    """The RubyGems home the real path ``real`` lies in (GEM_HOME, or the nearest directory up
    with both `gems/` and `specifications/`: an rbenv, rvm, asdf or mise Ruby's), else None.
    `bundle install` writes its cache/, gems/, specifications/ and extensions/, so a grant of
    the one directory Bundler named was refused again at the next (#832)."""
    if (g := os.environ.get("GEM_HOME")) and os.path.isabs(g) and \
            (real == (g := os.path.realpath(g)) or _under(real, g)):
        return g
    home = os.path.realpath(Path.home())
    d = real
    while d not in ("/", home) and d != os.path.dirname(d):
        if os.path.isdir(os.path.join(d, "gems")) and os.path.isdir(os.path.join(d, "specifications")):
            return d
        d = os.path.dirname(d)
    return None


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
    xdg += tool_var_homes()
    # OpenCode's cache and config directories, made first: the lane writes only what exists, so
    # a first task in a fresh profile failed opening OpenCode's log before any model request
    # (#340, the audit of 1.4). One that can't be made is named in runner.log, the place a
    # failed round points to. Not its data and state: the agent's OpenCode keeps those in the
    # task's own home (opencode_home), and the user's are hidden (SECRETS, #1010)
    made = [Path(os.environ.get(v) or home / d) / "opencode"
            for v, d in (("XDG_CACHE_HOME", ".cache"), ("XDG_CONFIG_HOME", ".config"))]
    for d in made:
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            print(f"workbench: can't make OpenCode's directory {d} ({e.strerror}): "
                  f"its first run fails without it", file=sys.stderr)
    places = [tempfile.gettempdir(), *TEMP_ROOTS, *darwin_cache(), *DEV_NODES, *gpu_nodes(task),
              "/proc", *tool_homes(), *(x for x in xdg if x),
              *flutter_caches(),
              *(Path(p).expanduser() for p in extra)]
    whole = {"/", os.path.realpath(home)}  # a GOPATH=~ must not hand over the home directory
    temps = {os.path.realpath(t) for t in (tempfile.gettempdir(), *TEMP_ROOTS)}
    guard = repo_dirs(task)  # a repo kept in /tmp is the user's choice: the temp dirs stay
    cargo = os.environ.get("CARGO_HOME")
    cargos = {os.path.realpath(c) for c in (home / ".cargo", cargo) if c}
    for c in cargos:
        # every cargo home is split, its own entries made first: #1194's fifth review
        # (2026-11-24): split only when bin/, env or a config was there, a distro's cargo
        # (Arch's rust, Debian's, Fedora's), which makes only registry/, left ~/.cargo whole,
        # and the agent made a config.toml whose build.rustc-wrapper ran at the next build
        if os.path.isdir(c):
            _make_first(c, CARGO_MADE)
    nimble = os.environ.get("NIMBLE_DIR")
    for c in {os.path.realpath(c) for c in (home / ".nimble", nimble) if c and os.path.isabs(c)}:
        if os.path.isdir(c):  # split around bin/ whenever it exists (MADE_FIRST, below)
            _make_first(c, NIMBLE_MADE)
    hidden = _hidden() | {h for h in api_hidden(task) if os.path.lexists(h)}  # #1021
    for k, n in MADE_FIRST.items():  # ~/.rustup's toolchains/, ~/.nimble's bin/ (#911)
        r = os.environ.get(k) if k.isupper() else str(home / k)
        if r and os.path.isabs(r) and os.path.isdir(r):  # made, so it is split out: else the
            try:                                         # agent could make it and plant one
                os.makedirs(os.path.join(r, n), exist_ok=True)
            except OSError:
                pass
    if made[0].is_dir():  # OpenCode's cache, split around npm/ (MADE_FIRST): its own first
        _make_first(str(made[0]), OPENCODE_CACHE_MADE)
    # with Landlock a NOT_WRITABLE place splits its home like a secret (~/.cache around
    # OpenCode's npm/, #1311's review): what the common tools make at its top is made first too
    split_by = hidden | (set() if lane.kind() == "seatbelt" else not_writable())
    for d, names in SPLIT_MADE.items():  # only a home something splits: else it is granted whole
        c = os.path.realpath(home / d)
        if os.path.isdir(c) and any(_under(h, c) for h in split_by):
            _make_first(c, names)
    for v, sub, d in SPLIT_MADE_VARS:  # GRADLE_USER_HOME's, HEX_HOME's (#1021's review)
        c = os.environ.get(v) or ""
        c = os.path.realpath(os.path.join(c, sub)) if os.path.isabs(c) else ""
        if c and os.path.isdir(c) and any(_under(h, c) for h in split_by):
            _make_first(c, SPLIT_MADE[d])
    # the user's active Python environments too: a venv in a tool home (poetry's, pipenv's,
    # WORKON_HOME) was the agent's to install into (#490)
    envs = set(python_envs())
    # Seatbelt denies NOT_WRITABLE's places by path inside the lane (write_denied): no split.
    # Landlock splits around those made, and a cargo home's made or not, so its split refuses
    # a new bin/, env or config there (#1194's fifth review)
    out_of = hidden | envs | (set() if lane.kind() == "seatbelt" else not_writable() | {
        os.path.join(c, n) for c in cargos for n in VAR_NOT_WRITABLE["CARGO_HOME"]})
    cache = os.path.realpath(os.environ.get("XDG_CACHE_HOME") or home / ".cache")
    if any(_under(e, cache) for e in envs):  # (#825)
        for n in CACHE_MADE:
            try:
                os.makedirs(os.path.join(cache, n), exist_ok=True)
            except OSError:  # a read-only home: the task starts all the same
                pass
    # Of the task's directory only its worktree, ask/, where the ask server writes its
    # questions, and opencode/, its OpenCode's data and state (#1010): the rest (task.json,
    # status.json, steer.jsonl, the rounds, runner.log) steers the runner and wb, outside the
    # lane, and notify.jsonl wakes Claude for every task (the 2026-11-16 audit: an agent could
    # rewrite its own spec and guards, forge any task's notice, and plant a link the runner
    # then wrote through)
    kept = [str(task.worktree), str(ask_dir(task)), str(opencode_home(task))]
    for p in places:
        real = os.path.realpath(p)
        if not os.path.exists(p) or real in whole:
            continue
        if real in temps:  # a secret or repo kept in /tmp stays in reach: splitting /tmp would
            kept.append(str(p))  # refuse every new file there (Java's, Xvfb's)
        else:  # split around the repo like a secret: a grant of ~/work beside the repo ~/work/api
            kept += _split(str(p), out_of | guard)  # was dropped whole (the 0.90 review)
    return [p for p in _both(kept) if os.path.exists(p) and os.path.realpath(p) not in whole]


def write_denied(places: list[str]) -> list[str]:
    """With Seatbelt, NOT_WRITABLE's places (made or not, real paths and their own names, a link
    unresolved) inside the lane's
    writable ``places``: its profile denies their writes after the lane's allows, the last match
    winning, and a path rule holds before the tool makes the place, so nothing is made first.
    Landlock can only allow, so there writable() splits around them instead, and this is [].
    #1194's second review (2026-11-24): split around com.apple.python, Library/Caches refused
    every tool's first cache folder (golangci-lint's, pipenv's, black's, Homebrew's) but the
    few made for it, and woke Claude for a grant it never needed."""
    if lane.kind() != "seatbelt":
        return []
    real = {os.path.realpath(p) for p in places}
    # and each by its own name, its directory resolved but not the place: a ~/.cargo/config.toml
    # linked into ~/dotfiles or /nix/store (stow, home-manager) was denied only at its target,
    # outside the lane and dropped, so the agent could unlink the link and write its own config
    # (#1194's fifth review, 2026-11-24)
    named = {p for q in not_writable_paths() for p in (
        os.path.realpath(q), os.path.join(os.path.realpath(q.parent), q.name))}
    return sorted(p for p in named if any(p == w or _under(p, w) for w in real))


def darwin_cache() -> list[str]:
    """macOS's per-user cache directory (DARWIN_USER_CACHE_DIR, `C` beside the temp directory's
    `T` in /var/folders), where clang's module cache and xcrun's go: Seatbelt would refuse every
    Swift or Objective-C build's write there (#1194). Nothing elsewhere."""
    temp = Path(tempfile.gettempdir())
    cache = temp.parent / "C"
    if sys.platform != "darwin" or temp.name != "T" or not cache.is_dir():
        return []
    return [str(cache)]


def _make_first(home: str, names) -> None:
    """Make each of names in a split tool home (a trailing / is a directory, else a file with
    MADE_TEXT's contents), keeping what is there: a split home refuses a new entry."""
    for n in names:
        try:
            if n.endswith("/"):
                os.makedirs(os.path.join(home, n), exist_ok=True)
            elif not os.path.lexists(os.path.join(home, n)):
                with open(os.path.join(home, n), "x") as f:  # never over the tool's own
                    f.write(MADE_TEXT.get(n, ""))
        except OSError:  # a read-only home, or the tool made it first: the task starts all the same
            pass


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


# The search paths the user's shell exports for a workspace outside the repo: a ROS 2 overlay
# (~/ros2_ws/install, PYTHONPATH and AMENT_PREFIX_PATH from its setup.bash), a src tree, Spack.
# python_path hands the agent PYTHONPATH, but the read lane kept nothing of it under a non-dot
# home directory, so `import my_msgs` raised PermissionError and a library on LD_LIBRARY_PATH
# "cannot open shared object file" (#667, the 2026-11-21 audit). Read-only, like sdk_roots.
SEARCH_PATH_VARS = ("PYTHONPATH", "LD_LIBRARY_PATH", "AMENT_PREFIX_PATH", "CMAKE_PREFIX_PATH",
                    "PKG_CONFIG_PATH")


def search_roots(hidden: set[str]) -> set[str]:
    """The absolute directories on SEARCH_PATH_VARS (and the user's PYTHONPATH a detached runner
    keeps, USER_PYTHONPATH) that exist, real paths: never `/`, the home directory or a place
    holding or inside a secret."""
    home = os.path.realpath(Path.home())
    paths = [d for v in (*SEARCH_PATH_VARS, USER_PYTHONPATH) for d in (os.environ.get(v) or "").split(os.pathsep)]
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
# And the other NSS lookups: avahi's (nsswitch's mdns4_minimal: a `.local` model server resolved
# for the runner's probes and failed every request in the lane), nslcd's and winbind's (LDAP and
# AD users: getpwuid() failed, so `whoami` and ssh did) (#913, the third 2026-11-21 audit)
SYSTEM_SOCKETS = ("/dev/log", "/run/systemd/journal/socket", "/run/systemd/journal/stdout",
                  "/run/systemd/resolve/io.systemd.Resolve",
                  "/run/systemd/userdb/io.systemd.Multiplexer", "/run/nscd/socket",
                  "/var/lib/sss/pipes/nss", "/run/avahi-daemon/socket", "/run/nslcd/socket",
                  "/run/samba/winbindd/pipe", "/var/run/samba/winbindd/pipe")


def secret_sockets() -> set[str]:
    """SECRET_SOCKETS under the home that exist as sockets, real paths."""
    found = set()
    for n in SECRET_SOCKETS:
        p = os.path.realpath(Path.home() / n)
        try:
            if stat.S_ISSOCK(os.stat(p).st_mode):
                found.add(p)
        except OSError:
            pass
    return found


def unix_paths(task: store.Task) -> list[str]:
    """Where the agent may connect to a UNIX socket made outside its lane (Landlock ABI 9): the
    SYSTEM_SOCKETS there are, and what the user and Claude added (`paths`; `wb allow --path
    /run/postgresql` for a test database), never the session bus or the user's agents, split
    around secrets like the reads (the 0.90 review: a GPG agent socket under a granted ~/.config
    was in reach)."""
    hidden = _hidden()
    granted = [Path(r).expanduser() for r in lane_paths(task) if r != "*"]
    inside = secret_sockets()  # a socket in a secret: connected to, nothing else (no read, write)
    return _both([*(s for s in SYSTEM_SOCKETS if os.path.exists(s)),
                  *(str(p) for p in granted if os.path.realpath(p) in inside),
                  *(q for p in granted if os.path.exists(p) for q in _split(str(p), hidden))])


def ask_dir(task: store.Task) -> Path:
    """Where the agent's questions go (ask_mcp.py): the one place in its task directory that
    its lane may write."""
    d = task.dir / "ask"
    if task.dir.is_dir():
        d.mkdir(exist_ok=True)
    return d


# The agent's OpenCode keeps its data and state (the session database, snapshot/, tool-output/,
# log/, the prompt history) in the task's own home, not the user's (#1010, 2026-11-22): OpenCode
# V2 keeps every /connect credential, for every provider, in ~/.local/share/opencode/opencode.db,
# with every project's sessions and snapshots, and the lane wrote that directory. OpenCode has no
# switch of its own for these (checked on 2.3.2: OPENCODE_DB moves only the database, and the
# data directory's auth.json is still imported into it), so XDG_DATA_HOME and XDG_STATE_HOME
# move them, in OpenCode's environment alone: the agent's commands get the user's back from the
# shell shim (runner.write_shell_shim), so uv, pipx, poetry and pnpm find their data.
OPENCODE_HOME_PARTS = ("share", "state")
# what nothing reads once the task is closed: OpenCode's snapshots of the worktree, the tool
# output it saved and each shell command's (shell/<project>/sh_*.out, on 2.3: no session export
# names them, the #1010 review); the database stays, for wb watch, the lessons and finished_export
OPENCODE_SCRATCH = ("snapshot", "tool-output", "shell")


def opencode_home(task: store.Task) -> Path:
    """The task's own OpenCode home, `tasks/<task>/opencode/` (share/ and state/ inside), in the
    agent's lane. Made by make_opencode_home."""
    return task.dir / "opencode"


def make_opencode_home(task: store.Task) -> Path:
    """The task's OpenCode home, made private (0700) with share/ and state/: before each round.
    A link the agent left in place of one of them is replaced by a directory, so OpenCode never
    writes, nor wb reads, through it."""
    home = opencode_home(task)
    for d in (home, *(home / p for p in OPENCODE_HOME_PARTS)):
        try:
            if os.path.islink(d) or (os.path.lexists(d) and not os.path.isdir(d)):
                os.unlink(d)
            d.mkdir(mode=0o700, exist_ok=True)
            os.chmod(d, 0o700)
        except OSError as e:
            print(f"workbench: can't make the task's OpenCode home {d} ({e.strerror})",
                  file=sys.stderr)
    return home


def opencode_home_env(task: store.Task) -> dict:
    """The environment that puts OpenCode in the task's own home, for OpenCode alone (the
    runner's round and wb's session commands), never the agent's commands or the tests.
    OPENCODE_DB always: a user's own, inherited, would point back at the user's database.
    OPENCODE_DISABLE_MODELS_FETCH: each OpenCode server start fetches the models.dev catalog
    and caches it in its database, 13.7 MB, so every task's home held one and every task fetched
    it again (measured 2026-11-23: 14 MB with it, 320 KB without); the catalog bundled in the
    binary serves instead (the #1010 review)."""
    home = opencode_home(task)
    return {"XDG_DATA_HOME": str(home / "share"), "XDG_STATE_HOME": str(home / "state"),
            "OPENCODE_DB": str(home / "share" / "opencode" / "opencode.db"),
            "OPENCODE_DISABLE_MODELS_FETCH": "1"}


def opencode_db(task: store.Task) -> Path:
    """The task's own OpenCode database (it may not exist yet)."""
    return Path(opencode_home_env(task)["OPENCODE_DB"])


def drop_opencode_scratch(task: store.Task) -> None:
    """Remove OpenCode's snapshots and saved tool and shell output from the task's home once the
    task is closed (accept, discard); never through a link the agent left in the lane, at any
    step from the home down (rmtree itself never follows one)."""
    home = opencode_home(task)
    for name in OPENCODE_SCRATCH:
        d = home / "share" / "opencode" / name
        steps = [home, *(p for p in d.parents if home in p.parents), d]
        if any(os.path.islink(p) for p in steps) or not os.path.isdir(d):
            continue
        shutil.rmtree(d, ignore_errors=True)


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
# `.yarnrc.yml` (the package managers read them to install).
# #1010 (2026-11-22) hid OpenCode's data and state directories whole: V2 keeps every /connect
# credential there (opencode.db, its -wal and -shm, a release channel's db, the legacy
# auth.json), with every project's sessions, snapshot/ (OpenCode's git snapshots of other
# worktrees), tool-output/, worktree/, repos/ and the user's own TUI prompts
# (prompt-history.jsonl), all of which the agent could read and write. The agent's OpenCode
# keeps its own in the task's home (opencode_home).
# #992 (2026-11-22) added the deploy and registry logins after `.android/adbkey`, each checked
# against its tool's docs or source where they said. The same trade as `.npmrc` keeps these
# readable, since an install reads them: Maven's `.m2/settings.xml` and Gradle's
# `.gradle/gradle.properties` (mirrors, repository logins, the JVM options a build needs; only
# Maven's master password, `settings-security.xml`, is hidden: a build reads it only to decrypt
# a server password settings.xml holds encrypted, so in the lane such a login fails, and a first
# download from that repository with it; what ~/.m2/repository already holds still resolves),
# NuGet's `NuGet.Config` (restore's sources), poetry's `auth.toml` and composer's `auth.json`
# (both read on every command, and an unreadable one ends it), pub's `pub-tokens.json` (an
# unreadable one ends `dart pub get`; pub's own login, `pub-credentials.json`, is publishing
# only and hidden) and Hex's `hex.config` (its organisations' keys for `mix deps.get`, in the
# writable ~/.hex). Helm's `registry` is hidden although helm reads it at every start and stops
# when it can't: the agent's HELM_REGISTRY_CONFIG names a file of its own (runner.agent_env).
# So is k6's `.config/k6` (`k6 cloud login`'s token), which `k6 run` reads at every start and
# stops when it can't (its readDiskConfig): the agent's K6_CONFIG names its own. Vercel's CLI
# stops on every command, `vercel build` too, when its config or login can't be read: in the
# lane it doesn't run for a user logged in, a deploy CLI, which is Claude's to run.
# #999 (2026-11-22), each read with its file there but unreadable: the Firebase CLI stops, at
# exit if not before (configstore 5, which it uses, rethrows EACCES, and its cli.js deletes
# `lastError` from the store on every exit: run against an unreadable file, configstore's own
# module exited 1). Its store's path follows XDG_CONFIG_HOME alone, which would move OpenCode's
# and every tool's config too, so firebase doesn't run in the lane as it is, logged in or not:
# that exit handler writes the file on every run, so anyone who ran the CLI has one, and where
# it isn't the write fails (~/.config/configstore isn't the lane's; configstore rethrows that
# EACCES too: the review, the directory read-only). `XDG_CONFIG_HOME=$(mktemp -d) firebase
# emulators:exec --project demo-x ...` runs, a store of its own. semgrep stops too, so the agent's SEMGREP_SETTINGS_FILE names its own
# (runner.agent_env). Rush reads its credentials only for a cloud build cache with no
# RUSH_BUILD_CACHE_CREDENTIAL: the S3 and HTTP plugins catch the read's error, warn and miss
# (the build runs locally); the Azure one's first read isn't caught (AzureStorageBuildCacheProvider,
# not run here), so such a repo builds with `RUSH_BUILD_CACHE_ENABLED=0` before it in the lane.
# A hidden file inside a writable tool home splits it (writable): SPLIT_MADE lists what the tool
# makes at that home's top, made first.
SECRETS = (".ssh", ".gnupg", ".password-store", ".netrc", ".git-credentials", ".authinfo",
           ".authinfo.gpg", ".bitwarden.json", ".secrets.gpg", ".config/gh", ".config/hub",
           ".local/share/keyrings", ".aws", ".azure", ".config/gcloud", ".kube", ".docker",
           ".terraform.d", ".pypirc", ".gem/credentials", ".cargo/credentials",
           ".cargo/credentials.toml", ".config/rclone", ".codex", ".pki", ".mozilla",
           ".thunderbird", ".config/chromium", ".config/google-chrome", ".config/BraveSoftware",
           ".camoufox", ".irssi", ".bash_history", ".zsh_history",
           ".python_history", ".claude", ".claude.json",
           ".config/opencode/service.json", ".local/share/opencode", ".local/state/opencode",
           ".config/git/credentials", ".gcm", ".pgpass", ".my.cnf", ".vault-token", ".s3cfg",
           ".boto", ".config/glab-cli", ".config/github-copilot", ".config/op",
           ".config/Bitwarden CLI", ".config/doctl", ".config/sops", ".config/age", ".fly",
           ".kaggle", ".oci", ".gemini", ".config/containers/auth.json",
           ".local/share/fish/fish_history", ".var/app", ".config/vivaldi",
           ".config/microsoft-edge", ".config/opera", ".android/adbkey",
           # #992 (2026-11-22): deploy CLIs' logins, registry and publish tokens, cloud and
           # database logins, signing keys: none an install needs (a test that does brings its own)
           ".config/.wrangler", ".local/share/com.vercel.cli", ".supabase/access-token",
           ".netlify", ".config/netlify", ".pulumi/credentials.json", ".serverlessrc",
           ".config/k6", ".terraformrc", ".config/helm/registry",
           ".config/dart/pub-credentials.json", ".ansible/galaxy_token", ".m2/settings-security.xml",
           ".foundry/keystores", ".mc", ".erlang.cookie", ".config/erlang/.erlang.cookie",
           ".mylogin.cnf", ".dbt/profiles.yml", ".config/openstack", ".config/hcloud", ".chef",
           ".config/infracost", ".minikube",
           # #999 (2026-11-22): the Firebase CLI's login (its refresh token, in configstore's
           # file of its own; the rest of ~/.config/configstore is other tools'), semgrep's
           # (`semgrep login`'s token; under XDG_CONFIG_HOME's .semgrep when that is set) and
           # Rush's cloud build cache credentials (`rush update-cloud-credentials`)
           ".config/configstore/firebase-tools.json", ".semgrep/settings.yml",
           ".config/.semgrep/settings.yml", ".rush-user/credentials.json",
           # #1092 (2026-11-23): Ollama's account key, which signs its ollama.com requests:
           # the server uses it, outside the lane, and the agent's `ollama` talks to the server
           ".ollama/id_ed25519",
           # #1194 (2026-11-24): macOS's own, readable now that Library is (HOME_READABLE): the
           # keychains, cookies, mail and messages, and the browsers' and Claude's profiles
           "Library/Keychains", "Library/Cookies", "Library/Mail", "Library/Messages",
           "Library/Safari", "Library/Application Support/Google/Chrome",
           "Library/Application Support/BraveSoftware", "Library/Application Support/Firefox",
           "Library/Application Support/Microsoft Edge", "Library/Application Support/Vivaldi",
           "Library/Application Support/com.operasoftware.Opera", "Library/Application Support/Claude",
           # and the macOS homes of the tools above, where each keeps its login there (#1194's
           # review, 2026-11-24): Go's os.UserConfigDir (sops' age keys, doctl, k6, which keeps
           # its token under loadimpact/k6 on Linux too), env-paths' and the CLIs' own (Vercel,
           # wrangler, Netlify, helm, Bitwarden's, pub's), Thunderbird's and Chromium's profiles,
           # and OpenStack's clouds.yaml and secure.yaml (openstacksdk's loader reads platformdirs'
           # user_config_dir before ~/.config/openstack)
           "Library/Application Support/sops", "Library/Application Support/com.vercel.cli",
           "Library/Preferences/.wrangler", "Library/Application Support/Bitwarden CLI",
           "Library/Preferences/helm/registry", "Library/Preferences/netlify",
           "Library/Application Support/dart/pub-credentials.json",
           "Library/Application Support/loadimpact/k6", ".config/loadimpact/k6",
           "Library/Application Support/doctl", "Library/Thunderbird",
           "Library/Application Support/Chromium", "Library/Application Support/openstack")
# The sockets inside a secret that a grant may still reach, to connect only (unix_paths): Docker
# Desktop's, under ~/.docker beside the registry logins in config.json, so `wb allow --path` was
# refused and only turning the lane off let a Testcontainers suite run (#441, the audit of 1.8)
SECRET_SOCKETS = (".docker/desktop/docker.sock", ".docker/run/docker.sock")
# where the user moved them (a file, a directory, or a path list)
SECRET_VARS = ("GNUPGHOME", "PASSWORD_STORE_DIR", "GH_CONFIG_DIR", "DOCKER_CONFIG", "KUBECONFIG",
               "AWS_SHARED_CREDENTIALS_FILE", "AWS_CONFIG_FILE", "CLOUDSDK_CONFIG",
               "AZURE_CONFIG_DIR", "HISTFILE",
               # the files Terraform's CLI config, podman's and buildah's logins, helm's registry
               # logins, Ansible Galaxy's token and the MinIO client's keys were moved to (#992)
               "TF_CLI_CONFIG_FILE", "REGISTRY_AUTH_FILE", "HELM_REGISTRY_CONFIG",
               "ANSIBLE_GALAXY_TOKEN_PATH", "MC_CONFIG_DIR",
               # and semgrep's settings, its login inside (#999)
               "SEMGREP_SETTINGS_FILE")
# Hidden in an API task's lane only (#1021): the registry and token files SECRETS' comment keeps
# readable because installs read them. On the GPU that trade keeps a token on this machine; on
# an API task every file the agent reads and every command's output goes to the provider, so
# `cat ~/.npmrc`, or a failing install echoing its config, would send the registry token there.
# Most of these tools stop on every run when one is there but unreadable, not only on a private
# install (API_HIDDEN_STOPS), so one is hidden only when it holds a credential
# (privacy.holds_credential: #1021's decision, 2026-11-23), and wb start names which (api_hidden_note).
# Each path from its tool's docs (2026-11-23): npm's user config, Yarn Berry's, Maven's
# settings.xml, Gradle's user-home properties, NuGet's user config in both places tools use
# (the .NET CLI's ~/.nuget/NuGet, Mono's ~/.config/NuGet) and their additional config/ folders,
# poetry's config dir (XDG on Linux, Library/Application Support on macOS), composer's home
# (XDG, else ~/.composer, as on macOS), pub's tokens (cli_util's applicationConfigHome: XDG,
# Library/Application Support on macOS), Hex's home and its MIX_XDG config, pip's user config
# (pip's configuration.py: the XDG one, macOS's Library/Application Support and the legacy
# ~/.pip), Bundler's global config
# (Settings#global_config_file) and bun's global bunfig (bunfig's get_home_config_path).
# And Hugging Face's login (#1092, 2026-11-23: huggingface_hub's constants.py, HF_HOME's `token`
# and `stored_tokens`): SECRET_ENV drops HF_TOKEN, but the library reads the same token from the
# file by itself, and `cat` sent it to the provider; on the GPU it stays readable for a test that
# downloads a gated model (a test's login() or logout() can rewrite it, as any file in ~/.cache).
API_HIDDEN = (".npmrc", ".yarnrc.yml", ".m2/settings.xml", ".gradle/gradle.properties",
              ".nuget/NuGet/NuGet.Config", ".config/NuGet/NuGet.Config", ".nuget/config",
              ".config/NuGet/config", ".config/pypoetry/auth.toml",
              "Library/Application Support/pypoetry/auth.toml", ".config/composer/auth.json",
              ".composer/auth.json", ".config/dart/pub-tokens.json",
              "Library/Application Support/dart/pub-tokens.json", ".hex/hex.config",
              ".config/hex/hex.config", ".config/pip/pip.conf", ".pip/pip.conf",
              "Library/Application Support/pip/pip.conf", ".bundle/config",
              ".bunfig.toml", ".cache/huggingface/token", ".cache/huggingface/stored_tokens")
# the ones that are a token and nothing else, by name or by the variable that moves it, which
# holds_credential's key and value matcher can't see: one there holds one unless empty (#1092)
API_HIDDEN_BARE = ("token", "HF_TOKEN_PATH")
# where the user moved them: (variable, the file inside, or "" for a file named by the variable,
# and its tool, or None for API_HIDDEN_TOOL's by the file's name); bun reads
# $XDG_CONFIG_HOME/.bunfig.toml in place of ~/.bunfig.toml
API_HIDDEN_VARS = (("NPM_CONFIG_USERCONFIG", "", "npm"), ("npm_config_userconfig", "", "npm"),
                   ("GRADLE_USER_HOME", "gradle.properties", None), ("POETRY_CONFIG_DIR", "auth.toml", None),
                   ("COMPOSER_HOME", "auth.json", None), ("HEX_HOME", "hex.config", None),
                   ("PIP_CONFIG_FILE", "", "pip"), ("BUNDLE_CONFIG", "", "Bundler"),
                   ("BUNDLE_USER_CONFIG", "", "Bundler"), ("BUNDLE_USER_HOME", "config", "Bundler"),
                   ("XDG_CONFIG_HOME", ".bunfig.toml", None),
                   # Hugging Face's home: HF_HOME, else XDG_CACHE_HOME's huggingface (#1092)
                   ("HF_HOME", "token", "Hugging Face"), ("HF_HOME", "stored_tokens", "Hugging Face"),
                   ("HF_TOKEN_PATH", "", "Hugging Face"), ("HF_STORED_TOKENS_PATH", "", "Hugging Face"),
                   ("XDG_CACHE_HOME", "huggingface/token", "Hugging Face"),
                   ("XDG_CACHE_HOME", "huggingface/stored_tokens", "Hugging Face"))
# which tool reads each, by its name, or its folder's and its name (~/.bundle/config)
API_HIDDEN_TOOL = {".npmrc": "npm", ".yarnrc.yml": "Yarn", "settings.xml": "Maven",
                   "gradle.properties": "Gradle", "NuGet.Config": "dotnet", "config": "dotnet",
                   "auth.toml": "poetry",
                   "auth.json": "Composer", "pub-tokens.json": "dart pub get", "hex.config": "Hex",
                   "pip.conf": "pip", ".bundle/config": "Bundler", ".bunfig.toml": "bun",
                   "huggingface/token": "Hugging Face", "huggingface/stored_tokens": "Hugging Face"}
# and the tools that stop on every run while theirs is there but unreadable, so in an API lane
# they don't run at all (#1021's review, 2026-11-23, each from its source): Yarn Berry
# (Configuration.findFolderRcFile rethrows all but ENOENT), Maven (DefaultSettingsBuilder:
# "Non-readable settings", FATAL), Gradle (DefaultGradlePropertiesLoader: isFile, then
# GUtil.loadProperties throws), NuGet (SettingsFile: "Failed to read NuGet.Config due to
# unauthorized access"; every dotnet restore, build and test), poetry and composer (SECRETS'
# comment: read on every command), pub (an unreadable pub-tokens.json ends `dart pub get`),
# Bundler (Settings#load_config reads it through filesystem_access: EACCES is a PermissionError,
# every bundle command). npm (an unreadable ~/.npmrc still ran `npm test`), Hex (Hex.Config.read
# takes any read error as no config), pip (configparser's read skips a file it can't open) and
# bun (unreadable_config skips an auto-loaded bunfig, "like npm") fail only what needs the
# token: a private registry's install.
API_HIDDEN_STOPS = frozenset({"Yarn", "Maven", "Gradle", "dotnet", "poetry", "Composer", "dart pub get",
                              "Bundler"})
# and what fails for a tool that installs nothing (#1092): huggingface_hub takes a token file it
# can't read as no token, so only a gated or private model's download fails
API_HIDDEN_FAILS = {"Hugging Face": "Hugging Face's gated and private models don't download"}
# What says a checkout builds with each tool: a manifest at its top or in the task's package
# (fnmatch patterns; poetry's pyproject.toml only with a [tool.poetry] table, _uses). The note
# named every hidden file on every API start, whatever the repo built with, and the skill had
# Claude tell the user the repo wouldn't build on API (#1322)
API_HIDDEN_USES = {"npm": ("package.json",), "Yarn": ("package.json",), "bun": ("package.json",),
                   "Maven": ("pom.xml",),
                   "Gradle": ("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts"),
                   "dotnet": ("*.csproj", "*.fsproj", "*.vbproj", "*.sln", "nuget.config", "NuGet.Config"),
                   "poetry": ("pyproject.toml",), "Composer": ("composer.json",), "Bundler": ("Gemfile",),
                   "dart pub get": ("pubspec.yaml",), "Hex": ("mix.exs",),
                   "pip": ("pyproject.toml", "requirements*.txt", "setup.py", "setup.cfg"),
                   "Hugging Face": ("pyproject.toml", "requirements*.txt", "setup.py", "setup.cfg")}
# Of the home directory's own entries, the dotfiles (tools' configs, caches, toolchains) stay
# readable, and these (R's and Perl's user libraries); the user's documents and other projects
# don't, beyond the task's repo, workbench itself, the programs on PATH and what Claude allowed.
# macOS's Library too (#1194): its Application Support and Preferences are what the dotfiles are
# on Linux, and `pip install --user` puts packages in Library/Python; what of it holds a login is
# in SECRETS.
HOME_READABLE = ("bin", "go", "R", "perl5", "Library")
# Outside the home directory, these hold the user's data, not the system: like the home's own
# entries, only what is kept (the repo, the lane, a grant) is readable beneath them. /mnt holds
# the Windows drives on WSL2, /home the other users' homes (the 2026-11-16 audit), and /Users
# theirs on macOS (#1194), /Volumes its external disks and network shares, as /media and /mnt
# (#1194's review: its `Macintosh HD` is a link to /, which Seatbelt matches resolved)
ONLY_KEPT = ("/dev", "/mnt", "/media", "/run/media", "/run/user", "/home", "/Users", "/Volumes")
# The links in /dev to the calling process's own descriptors: they name no file. Resolved in wb's
# process, /dev/stderr was /dev/null under `wb verify 2>/dev/null` and so was listed beside it, and
# in the lane's child, where fd 2 is a pipe, Landlock refused the rule (EBADFD) and with it the
# lane: both runs exited 126 and verify said "the candidate fails it" (#1308, 2026-11-24).
FD_LINKS = ("/dev/stdin", "/dev/stdout", "/dev/stderr", "/dev/fd")
# WSL2's Windows drives (DrvFs, /mnt/<letter> unless wsl.conf moved them): a Windows program there
# runs through interop as a Windows process, as the user, outside Landlock and no-new-privileges.
# The read lane let the agent start one: python3 running cmd.exe, or the allowed `npm` resolving to
# a Windows-only Node (the 0.90 review). So on WSL they are left out of the agent's PATH
# (agent_path), of tool_roots and, wherever they are mounted, of the reads (readable keeps only
# what is kept beneath them, as for ONLY_KEPT, #914): out of the read lane, the exec itself is
# refused, since execve opens the program for reading. A Windows program inside the lane still runs (`wb doctor` says
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
        # os.path, not Path.is_file: before 3.14 that raises under a parent this account can't
        # read (another account's ~/bin, a dead NFS mount's ESTALE), and every lane failed (#850)
        if p.is_absolute() and p.name == "bin" and (os.path.isfile(p.parent / "pyvenv.cfg")
                                                   or os.path.isdir(p.parent / "conda-meta")):
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
    python_path(task, env, gone)
    return inside


# The user's PYTHONPATH (ROS's setup.bash, a src layout exported from a shell profile, Blender,
# Spack) never reached the agent, the setup or the tests: spawn_runner replaced it with the
# package root, the agent's environment dropped that, and naming PYTHONPATH in commands.json `env`
# passed workbench's own path, which lacks the project's modules and put `python3 -m workbench.cli`
# in the agent's reach (#512, the audit of 1.90). The detached processes (the runner, the bench
# driver, wb optimize) keep the caller's in USER_PYTHONPATH; agent_env gives it back (python_path).
USER_PYTHONPATH = "WB_USER_PYTHONPATH"


def detached_env() -> dict:
    """The environment for a detached workbench process: `PYTHONPATH` the package root, and the
    caller's own kept in USER_PYTHONPATH (already kept, when wb runs inside one: a bench replay)."""
    env = {**os.environ, "PYTHONPATH": str(PKG_ROOT), "WB_HOME": str(store.home())}
    env[USER_PYTHONPATH] = os.environ.get(USER_PYTHONPATH, os.environ.get("PYTHONPATH", ""))
    return env


def python_path(task: store.Task, env: dict, gone=lambda p: False) -> None:
    """``env``'s PYTHONPATH, in place: the user's (USER_PYTHONPATH, never the runner's own), less
    workbench's package and the dropped environments' directories (``gone``), with each directory
    inside the repo as the worktree's, as on PATH; unset when nothing is left."""
    repo = os.path.realpath(task.spec["repo"])
    own = os.path.realpath(PKG_ROOT)
    out = []
    for d in env.pop(USER_PYTHONPATH, "").split(os.pathsep):
        real = os.path.realpath(d) if os.path.isabs(d) else ""
        if real and (real == own or _under(real, own) or gone(real)):
            continue
        if real and _in_repo(real, repo):
            d = os.path.normpath(os.path.join(task.worktree, os.path.relpath(real, repo)))
        if d:
            out.append(d)
    env.pop("PYTHONPATH", None)
    if out:
        env["PYTHONPATH"] = os.pathsep.join(out)


def python_installs(task: store.Task) -> tuple[bool, bool]:
    """Whether the agent may install what the project lists with pip (`pip install -e .`, `-r`)
    without asking: into the worktree's own venv, or with none active (the user's checkout's are
    dropped, python_env) while Landlock bounds what pip writes; else pip would write the user's
    own environment (a conda env, a tools venv), or their Python without the lane (#490). And
    whether the active venv the agent's shell has is the user's (ENV_INSTALLS ask first)."""
    if own_venv(task):
        return True, False
    kept = [r for r in python_envs() if r not in checkout_envs(task)]
    return not kept and writable(task) is not None and lane.held(), bool(kept)


# mise refuses a config it hasn't been told to trust outside a terminal, and trust is kept per
# path (~/.local/state/mise/trusted-configs): the worktree, the base check's and wb verify's
# checkouts are paths the user never trusted, so every mise shim the setup, the agent and the
# tests ran failed ('Config files in …/mise.toml are not trusted'), and `mise trust` writes outside
# the lane (#674, checked with mise 2026.11). The configs mise asks trust for (.tool-versions
# isn't one):
MISE_CONFIGS = ("mise.toml", ".mise.toml", "mise.local.toml", ".mise.local.toml",
                ".config/mise.toml", ".config/mise/config.toml", ".mise/config.toml",
                "mise/config.toml")


def mise_trusted(task: store.Task, path: str | None = None) -> bool | None:
    """Whether the user's checkout's mise config is trusted (`mise trust --show`, run outside the
    lane); None when it has none, ``path`` has no `mise`, or mise can't say."""
    repo = os.path.realpath(task.spec["repo"])
    at = os.path.join(repo, task.spec["subdir"]) if task.spec.get("subdir") else repo
    if not any(os.path.isfile(os.path.join(d, n)) for d in {repo, at} for n in MISE_CONFIGS):
        return None
    mise = shutil.which("mise", path=path)
    if not mise:
        return None
    try:
        out = subprocess.run([mise, "trust", "--show"], cwd=at, capture_output=True, text=True,
                             timeout=10, stdin=subprocess.DEVNULL).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    said = []
    for line in out.splitlines():  # `~/src/app: trusted`, one line per directory
        where, _, verdict = line.rpartition(": ")
        real = os.path.realpath(os.path.expanduser(where.strip())) if where else ""
        if verdict.strip() in ("trusted", "untrusted") and (real == repo or _under(real, repo)):
            said.append(verdict.strip() == "trusted")
    return all(said) if said else None


def venv_line(task: store.Task) -> str:
    """For wb start with no --setup: the active environments the agent leaves out, and how to
    give it its own."""
    envs = checkout_envs(task)
    if not envs:
        return ""
    return (f"the agent leaves out your active venv ({', '.join(envs)}): --setup 'uv sync' (or "
            "the project's install) gives the worktree its own")


def interop_on() -> bool:
    """Whether WSL runs Windows programs (binfmt_misc's WSLInterop enabled): False off WSL."""
    if hardware.system() != "wsl":
        return False
    for name in ("WSLInterop", "WSLInterop-late"):
        try:
            if Path(BINFMT, name).read_text().split("\n")[0].strip() == "enabled":
                return True
        except OSError:
            continue
    return False


def interop_gap() -> str | None:
    """What WSL's interop leaves open, for `wb doctor`, or None (not WSL, or interop off)."""
    if not interop_on():
        return None
    return ("WSL interop is on: a Windows program runs as you outside the agent's lane. The "
            "Windows drives are out of its PATH and reads, but a Windows program in its lane or "
            "repo still runs; [interop] enabled=false in /etc/wsl.conf (then wsl --shutdown) "
            "closes it, and also stops wb reading Windows' GPUs: set their memory in models.json "
            "gpu_gb then")  # a GPU only Windows sees had no budget at all after it (#702)


# Local daemons that take commands over TCP, each a way out of the lane: a Docker or Podman API
# runs containers as the user (root on the host), adb drives their devices, a GDB server their
# boards, Chrome's DevTools port is a browser (maybe the user's, signed in), Node's inspector runs
# code outside the lane. #993 (2026-11-22) only warned in `wb doctor`, believing Landlock's TCP
# rule could only allow ports; it can refuse these by allowing every other one (#1197,
# 2026-11-24), so the lane refuses connecting to them from ABI 4 (deny_ports), listening or not:
# a daemon started after the lane is drawn is refused too.
DAEMON_PORTS = {2375: "a Docker or Podman API", 2376: "a Docker or Podman API (TLS)",
                5037: "adb's server", 3333: "OpenOCD's GDB server", 2331: "J-Link's GDB server",
                9222: "Chrome's DevTools", 9229: "Node's inspector"}
NET_ABI = 4  # Landlock's TCP rule (Linux 6.7)


def granted_ports(task: store.Task | None = None) -> set[int]:
    """The TCP ports the user (commands.json `ports`) or Claude for this task (grants `ports`,
    `wb allow --port`) let the agent connect to."""
    ports = [*commands_conf().get("ports", []),
             *((grants(task).get("ports") or []) if task else [])]
    return {p for p in ports if isinstance(p, int) and not isinstance(p, bool)}


def url_port(url: str) -> int | None:
    """The TCP port a URL's requests go to (its scheme's when unsaid), or None."""
    try:
        u = urllib.parse.urlsplit(url or "")
        return u.port or {"http": 80, "https": 443}.get(u.scheme.lower())
    except ValueError:
        return None


def deny_ports(task: store.Task | None = None, keep=()) -> list[int]:
    """The TCP ports the task's lane refuses connecting to (lane.wrap's `deny_ports`):
    DAEMON_PORTS but the granted ones and ``keep``, the ports the round itself needs (its model
    server's, its keyhole's: runner.lane_ports), so no setup is ever asked for."""
    return sorted(set(DAEMON_PORTS) - granted_ports(task) - {p for p in keep if p})


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
    if db := os.environ.get("OPENCODE_DB"):  # the user's own OpenCode database, moved (#1010)
        paths += [Path(db + x).expanduser() for x in ("", "-wal", "-shm")]
    if os.environ.get("CARGO_HOME"):
        paths += [Path(os.environ["CARGO_HOME"]) / n for n in ("credentials", "credentials.toml")]
    try:  # .claude.json's backups and temporary copies
        paths += [home / n for n in os.listdir(home) if n.startswith(".claude.json")]
    except OSError:
        pass
    whole = {"/", os.path.realpath(home)}  # a variable set to ~ must not hide the whole home
    return {r for r in map(os.path.realpath, paths) if r not in whole}


def api_hidden_paths() -> set[str]:
    """API_HIDDEN, real paths, where the environment says they are too (XDG_CONFIG_HOME as
    secret_paths honours it, API_HIDDEN_VARS): each that holds a credential
    (privacy.holds_credential), so a config with none keeps its tool working."""
    return set(_api_hidden_tools())


def _api_hidden_tools() -> dict[str, str]:
    """api_hidden_paths, each with the tool that reads it (API_HIDDEN_TOOL)."""
    home = Path.home()
    paths = [(home / h, None, None) for h in API_HIDDEN]
    if os.environ.get("XDG_CONFIG_HOME"):
        paths += [(Path(os.environ["XDG_CONFIG_HOME"]) / h[len(".config/"):], None, None)
                  for h in API_HIDDEN if h.startswith(".config/")]
    for var, inside, tool in API_HIDDEN_VARS:
        if (v := os.environ.get(var)) and os.path.isabs(os.path.expanduser(v)):
            paths.append((Path(v).expanduser() / inside, tool, var))
    whole = {"/", os.path.realpath(home)}
    return {r: tool or API_HIDDEN_TOOL.get(f"{p.parent.name}/{p.name}") or API_HIDDEN_TOOL.get(p.name, "npm")
            for p, tool, var in paths
            if (r := os.path.realpath(p)) not in whole and (privacy.holds_credential(r) or (
                (p.name in API_HIDDEN_BARE or var in API_HIDDEN_BARE) and _filled(r)))}


def _filled(real: str) -> bool:
    """Whether the file ``real`` is there and not empty: a token file alone (API_HIDDEN_BARE)."""
    try:
        return os.path.isfile(real) and os.path.getsize(real) > 0
    except OSError:
        return True


def api_hidden(task: store.Task) -> set[str]:
    """What an API task's lane leaves out besides SECRETS (API_HIDDEN); nothing for a GPU task."""
    return api_hidden_paths() if task.spec.get("via") == "api" else set()


# Why no grant reaches a file api_hidden keeps out: readable() and writable() hide it over any
# grant, so a test refused on ~/.m2/settings.xml named `wb allow --path` for it, wb allow saved
# it, and the paid feedback round was refused the same way (#1093, 2026-11-23)
API_HIDDEN_WHY = ("an API task's lane hides it (it holds a credential, which would reach the "
                  "provider), so no grant reaches it: name the credential by an environment "
                  "reference in it instead, or run the task on the GPU")


def api_hidden_refusal(task: store.Task, raw: str) -> str | None:
    """Why ``raw`` can't join ``task``'s lane when it is (in) a file its API lane hides
    (api_hidden, API_HIDDEN_WHY), else None."""
    real = os.path.realpath(Path(raw).expanduser())
    if any(real == h or _under(real, h) for h in api_hidden(task)):
        return f"{raw}: {API_HIDDEN_WHY}"
    return None


def _uses(task: store.Task) -> set[str]:
    """The tools of API_HIDDEN_USES whose manifest the task's checkout has, at its top or in its
    package (task.json `subdir`)."""
    repo = os.path.realpath(task.spec["repo"])
    at = os.path.join(repo, task.spec["subdir"]) if task.spec.get("subdir") else repo
    names = set()
    for d in {repo, at}:
        try:
            names.update(os.listdir(d))
        except OSError:
            pass
    used = {tool for tool, pats in API_HIDDEN_USES.items()
            if any(fnmatch.fnmatch(n, pat) for n in names for pat in pats)}
    if "poetry" in used and not any(_reads(os.path.join(d, "pyproject.toml"), "[tool.poetry")
                                    for d in {repo, at}):
        used.discard("poetry")
    return used


def _reads(path: str, text: str) -> bool:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return text in f.read(1 << 20)
    except OSError:
        return False


def api_hidden_note(task: store.Task) -> str:
    """wb start's note for an API task when a file API_HIDDEN names holds a credential and the
    checkout builds with its tool (_uses), else '': the files, then the tools that don't run in
    its lane (API_HIDDEN_STOPS), those whose private-registry installs fail and what else fails
    (API_HIDDEN_FAILS: Hugging Face's gated models, #1092). '' where ``task`` has no lane to
    hide them: neither Landlock nor Seatbelt, or the lane off."""
    # Without a lane nothing is hidden: the agent reads the file and Maven runs. On macOS every
    # API start said otherwise, so Claude, as the skill says, told the user the repo won't build
    # on API and that the token couldn't reach the provider (#1156, 2026-11-24)
    if not lane.held() or writable(task) is None:
        return ""
    home = os.path.realpath(Path.home())
    uses = _uses(task)
    tools = {p: tool for p, tool in _api_hidden_tools().items() if tool in uses}
    there = sorted(tools)  # each there and holding a credential (a file, or NuGet's config/)
    if not there:
        return ""
    names = ", ".join("~" + p[len(home):] if p.startswith(home + os.sep) else p for p in there[:4])
    more = f" and {len(there) - 4} more" if len(there) > 4 else ""
    used = list(dict.fromkeys(tools[p] for p in there))
    stop = [t for t in used if t in API_HIDDEN_STOPS]
    fail = [t for t in used if t not in API_HIDDEN_STOPS and t not in API_HIDDEN_FAILS]

    def both(xs):
        return xs[0] if len(xs) == 1 else f"{', '.join(xs[:-1])} and {xs[-1]}"
    said = ([f"{both(stop)} stop{'s' if len(stop) == 1 else ''} on every run"] if stop else []) + \
        ([f"{both(fail)} installs from a private registry fail"] if fail else []) + \
        [API_HIDDEN_FAILS[t] for t in used if t in API_HIDDEN_FAILS]
    held = "it holds a credential" if len(there) == 1 else "each holds a credential"
    return (f"an API task can't read {names}{more} ({held}, which would reach the provider): "
            f"in its lane {', and '.join(said)}")


# A git URL with a login in it: any with a password, or an http(s) one whose user alone looks
# like a token (https://<token>@host): a known prefix, or 20 or more letters, digits and _ with
# a digit among them. Not an account name: Azure DevOps' https://<org>@dev.azure.com and
# Bitbucket's https://<user>@bitbucket.org, their default clone URLs, got the note on every API
# start (#1072's review, 2026-11-23); nor ssh://git@host
GIT_URL_LOGIN = re.compile(r"\bhttps?://(?:(?:gh[pousr]_|github_pat_|glpat-|x-access-token|oauth2)"
                           r"[^\s/@:]*|(?=[^\s/@:]*\d)\w{20,})@|\b[a-z][\w+.-]*://[^\s/@:]*:[^\s/@]+@",
                           re.I | re.A)
# the git config keys that hold a URL: remotes' and insteadOf rules' (the URL is in the key)
GIT_URL_KEYS = r"^(remote\..*\.(push)?url|url\..*\.(push)?insteadof)$"


def git_url_note(repo) -> str:
    """wb start's note for an API task when the repo's git config or the user's holds a URL with
    a login (GIT_URL_LOGIN), else '': `git remote -v` or `git config -l` in its lane would send
    it to the provider, and git needs those files to run, so no lane hides them (#1072,
    2026-11-23: a token in origin's URL, or in a ~/.gitconfig insteadOf rule, came out of
    `git remote -v`). Names the files, never the URL."""
    try:
        out = gitops.git(Path(repo), "config", "--show-origin", "--get-regexp", GIT_URL_KEYS,
                         check=False)
        top = gitops.git(Path(repo), "rev-parse", "--show-toplevel").strip() if out else ""
    except (gitops.GitError, OSError):
        return ""
    home = os.path.realpath(Path.home())
    files = []
    for line in out.splitlines():
        where, _, entry = line.partition("\t")
        if where.startswith("file:") and GIT_URL_LOGIN.search(entry):  # .git/config: the top's
            f = os.path.realpath(os.path.join(top, where[len("file:"):]))
            name = "~" + f[len(home):] if f.startswith(home + os.sep) else f
            if name not in files:
                files.append(name)
    if not files:
        return ""
    return (f"an API task can read the login in a git URL in {', '.join(files)} (`git remote -v`, "
            "which would send it to the provider): keep the token in a credential helper instead "
            "(`git config --global credential.helper store` puts it in ~/.git-credentials, which no "
            "lane reads)")


# where conda, mamba and micromamba say their install is, with or without an env activated
CONDA_VARS = ("CONDA_EXE", "CONDA_PYTHON_EXE", "MAMBA_EXE", "MAMBA_ROOT_PREFIX")


def tool_roots(hidden: set[str]) -> set[str]:
    """The installs the agent's programs run from: each PATH directory's prefix (its parent when
    it is a `bin`), the targets of the links in the home's PATH directories, and the Python
    running workbench (the git shim, the ask server). Running a program reads it, so one under a
    non-dot home directory (~/miniconda3, ~/flutter, ~/apps) failed in the lane (the 2026-11-16
    audit). The same under an ONLY_KEPT root, short of a user's home or a whole drive: Linuxbrew's
    bin links into ../Cellar, which the lane hid (the 0.90 review). Never `/`, the home directory
    or anything holding a secret: a prefix is kept whole; never a Windows drive on WSL. And
    conda's and mamba's installs by their variables (CONDA_VARS), a `condabin` taken like a
    `bin`: with the base env not activated only condabin is on PATH, and `conda run` couldn't
    read the interpreter or the envs beside it (#827)."""
    home = os.path.realpath(Path.home())
    prefix = lambda d: os.path.dirname(d) if os.path.basename(d) in ("bin", "condabin") else d  # noqa: E731
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
    for v in CONDA_VARS:
        if os.path.isabs(p := os.environ.get(v) or ""):  # a program's install, or a root itself
            real = os.path.realpath(p)
            root = prefix(os.path.dirname(real)) if v.endswith("_EXE") else real
            if os.path.isdir(root) and not on_windows(root, drives):
                found.add(root)
    return {r for r in found if r not in ("/", home)
            and not any(h == r or _under(h, r) for h in hidden)}


def readable(task: store.Task) -> list[str] | None:
    """What the agent's processes may read: the system, the home directory's dotfiles but
    SECRETS, the task's repo (an API task's: its git directory and config includes alone,
    read_repo_dirs) and its git
    directory, its lane and the programs it runs. None with the lane off. Landlock
    only allows, so this lists the entries beside each excluded one; lane.py adds the lane
    (writable) itself."""
    walked = _read_walk(task)
    return None if walked is None else walked[0]


def read_rules(task: store.Task) -> list[tuple[str, str]] | None:
    """The same read lane as Seatbelt's rules (#1194): ("deny" | "allow", real path), each over
    those before it (the last match wins, as in a Seatbelt profile; ancestors first), everything
    readable until a rule says otherwise. So a hidden directory is denied
    and what is kept inside it allowed after, and a directory where little is kept (/dev, the
    state directory) is one rule, not one per entry. None with the lane off."""
    walked = _read_walk(task)
    return None if walked is None else walked[1]


def _read_walk(task: store.Task) -> tuple[list[str], list[tuple[str, str]]] | None:
    """readable and read_rules, from one walk of the file system."""
    lane = writable(task)
    if lane is None:
        return None
    home = os.path.realpath(Path.home())
    hidden = secret_paths() | private_paths() | api_hidden(task)  # API_HIDDEN: an API task's (#1021)
    keep = {os.path.realpath(p) for p in (PKG_ROOT, *lane,                     # PKG_ROOT:
                                          *lane_paths(task), *siblings(task))  # the git shim,
            if p}                                                              # the ask server
    keep |= tool_roots(hidden) | sdk_roots(hidden) | search_roots(hidden)
    keep |= mcp_roots(task)  # the user's MCP servers this task runs (#1202); their secrets stay hidden
    # OpenCode's global config where OPENCODE_CONFIG_DIR keeps it, outside the dotfiles: the
    # agent's OpenCode loads it itself (#370, the audit of 1.6)
    if os.environ.get("OPENCODE_CONFIG_DIR"):
        keep.add(os.path.realpath(Path(os.environ["OPENCODE_CONFIG_DIR"]).expanduser()))
    # and the config home where XDG_CONFIG_HOME moves it, a non-dot directory under the home
    # (~/conf): its OpenCode config, git's and every tool's were unreadable, and a provider in
    # the global opencode.json failed every task as an unknown model (#1103, 2026-11-23); its
    # secrets stay hidden (secret_paths and _api_hidden_tools follow XDG_CONFIG_HOME)
    if os.path.isabs(xdg := os.environ.get("XDG_CONFIG_HOME") or "") and os.path.realpath(xdg) != home:
        keep.add(os.path.realpath(xdg))
    # the git directory too, outside the repo when it is a linked worktree: git failed there
    # ("gitfile does not point to a valid repository", the 0.90 review); writable() keeps it out
    keep |= read_repo_dirs(task)  # the checkout too, but on an API task (#1091)
    # Inside a hidden directory, only these come out: workbench itself (the marketplace install
    # puts it in ~/.claude/plugins, the 2026-11-16 audit), and of workbench's state the task's own
    # directory (its prompts, the ask server's spec) and its rules (its OpenCode config, its
    # instructions, the git shim), read-only; the rest stays hidden
    # The task's repo and git directory too when they are workbench's own: a bench replay's repo is
    # a clone in bench/runs/<run>/<task>/repo, so in every replay the agent's `git status` and
    # `git diff` failed, "gitfile does not point to a valid repository", and the agent spent its
    # round's calls on a git it took for broken (found in the probe pilot, 2026-11-21, #858)
    own = (PKG_ROOT, task.dir, rules_dir(task), *read_repo_dirs(task))
    inner = {r for r in map(os.path.realpath, own) if r not in hidden
             and any(_under(r, h) for h in hidden)}
    only_kept = {os.path.realpath(p) for p in ONLY_KEPT}
    # and WSL's drives wherever they are mounted: at /c (wsl.conf's `[automount] root = /`) a
    # drive was read whole, so the agent could exec cmd.exe by its path, outside the lane (#914)
    only_kept |= set(windows_drives() or [])
    under = _under
    out: list[str] = []
    rules: list[tuple[str, str]] = []  # read_rules': what changes the state each place inherits

    def say(real: str, p: str, now: bool, was: bool) -> bool:
        """Note a change of state at `real`; the state its entries inherit."""
        if real != p:  # reached through a link: a grant by it adds up (Landlock grants the
            if now:    # target), a refusal by it is nothing (the place is hidden or not on its
                rules.append(("allow", real))  # own way there)
            return now or was
        if now != was:
            rules.append(("allow" if now else "deny", real))
        return now

    def visit(p: str, default: bool, was: bool = True) -> None:
        if p in FD_LINKS:  # the caller's fds, not files: the child's are its own (#1308)
            return
        real = os.path.realpath(p)
        if real in hidden or any(under(real, h) for h in hidden):
            if real in inner:
                out.append(p)
                say(real, p, True, was)
                return
            now = say(real, real, False, was)  # a secret, or a link into one: by any way there
            if any(under(i, real) for i in inner):
                try:
                    names = sorted(os.listdir(p))
                except OSError:
                    names = []
                for name in names:
                    visit(os.path.join(p, name), False, now)
            return
        inside = real in keep
        holds = [h for h in hidden | keep | only_kept if under(h, real)]
        hides = any(h in hidden or h in only_kept for h in holds)
        if inside and not hides:
            out.append(p)
            say(real, p, True, was)
            return
        if default and not hides:
            out.append(p)
            say(real, p, True, was)
        elif holds:                    # go in: something to skip or keep (a repo at ~/.config
            # the place itself as its entries mostly are: denied where only what is kept is read
            now = say(real, p, inside or default, was)
            try:                       # keeps the rest of itself, not its secrets)
                names = sorted(os.listdir(p))
            except OSError:
                return
            for name in names:
                child = os.path.join(p, name)
                if inside:
                    visit(child, True, now)
                elif os.path.realpath(child) in only_kept:
                    visit(child, False, now)
                else:
                    visit(child, name.startswith(".") or name in HOME_READABLE
                          if real == home else default, now)
        else:
            say(real, p, False, was)
    visit("/", True)
    # ancestors first, so each rule is over the wider ones (the last match wins), and an allow
    # over a deny of the same place: Landlock's rules add up, so a place granted by a link
    # elsewhere (~/bin to ~/tools/bin) is read whatever its own way there says
    rules = sorted(dict.fromkeys(rules), key=lambda r: (len(Path(r[1]).parts), r[0] == "allow"))
    return out, rules


def socket_places(task: store.Task) -> list[str]:
    """Where the agent may connect to a UNIX socket: unix_paths, and with Seatbelt, which can't
    tell a socket the agent made from the user's (Landlock can), the task's own places too: its
    worktree, ask/, its OpenCode home and its runtime directory; never a temp directory, where
    tmux's and the SSH agent's are (#1194)."""
    places = unix_paths(task)
    if lane.kind() == "seatbelt":
        places += [str(task.worktree), str(ask_dir(task)), str(opencode_home(task)),
                   str(_runtime_path(task))]
    return places


def lane_wrap(task: store.Task, ports: list[int] | None = None):
    """What runs a command in ``task``'s lane (lane.wrap with its lists, drawn once): Landlock's
    read lane (readable) and its refused TCP ports (``ports``, else deny_ports: #1197), or
    Seatbelt's read rules (read_rules, #1194), whichever this machine has; the command as it is
    with the lane off."""
    places = writable(task)
    if places is None:
        return lambda cmd: lane.wrap(None, cmd)
    if lane.kind() == "seatbelt":
        reads, sockets, denied = read_rules(task), socket_places(task), write_denied(places)
        return lambda cmd: lane.wrap(places, cmd, unix=sockets, reads=reads, deny_write=denied)
    readable_, sockets = readable(task), unix_paths(task)
    denied_ports = deny_ports(task) if ports is None else ports
    return lambda cmd: lane.wrap(places, cmd, readable_, sockets, deny_ports=denied_ports)


def read_only(task: store.Task, lane: list[str]) -> list[str]:
    """The installs the lane lets the agent read but not write, for its file tools too: the
    programs it runs (tool_roots: a conda, pyenv or mise env's site-packages) and the toolchains
    split out of the write lane (NOT_WRITABLE, $RUSTUP_HOME/toolchains: rustup's std sources).
    `read` on pandas' frame.py got "Permission denied: external_directory", counting toward the
    six-errors guard, and the only remedy, `wb allow --path`, made the install writable (#521,
    the audit of 1.90). Also the SDKs (sdk_roots). Never one inside a lane entry, whose own
    rules say what is done there, but Seatbelt's write_denied places (#1194)."""
    hidden = secret_paths() | private_paths()
    split = not_writable_paths()
    places = tool_roots(hidden) | sdk_roots(hidden) | {
        os.path.realpath(p) for p in split
        if os.path.isdir(p) and not any(_under(os.path.realpath(p), h) for h in hidden)}
    real_lane = {os.path.realpath(p) for p in lane}
    # one holding a lane entry stays (a Flutter SDK, its bin/cache in the lane: read_only_rules
    # lets the entry be edited again); one in a lane entry is the lane's, but one Seatbelt
    # denies there (write_denied: com.apple.python in the whole Library/Caches)
    return sorted({p for p in places if p not in real_lane and not any(
        p == q or _under(p, q) for q in real_lane)} | set(write_denied(lane)))


def read_only_rules(task: store.Task) -> list[dict]:
    """The file tools may read `read_only` places but never edit there: the kernel refuses the
    write too, with Landlock or Seatbelt; without either these rules are what keep the file tools
    out."""
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
    # and the rounds' prompts, read-only, where a prompt too long for one argument waits, and the
    # images Claude attached (--image, #1199), which the prompt says to read again there
    places = [*lane, *READABLE, *read_only(task, lane), task.dir / "rounds", task.dir / "images",
              *siblings(task),
              *(sibling_link(task.worktree, n) for n in task.spec.get("siblings") or ()),
              *(have[n]["path"] for n in task.status.get("skills") or [] if n in have)]
    rules = [{"action": "external_directory", "resource": "*", "effect": "deny"}]
    rules += [{"action": "external_directory", "resource": f"{p.rstrip('/')}/*", "effect": "allow"}
              for p in _both(places) if os.path.isdir(p)]
    return rules


# Claude's files: the instructions every agent reads, and the project's skills and settings.
# The agent proposes a change to them in its summary; Claude makes it.
CLAUDE_FILES = ("*CLAUDE.md", "*CLAUDE.local.md", "*AGENTS.md", ".claude/*", "*/.claude/*")
# and OpenCode's project config, which loaded after the task's layer: an `opencode.json` the agent
# wrote lifted its own denies from the next round on (the 2026-11-16 audit). OpenCode no longer
# loads it at all (OPENCODE_DISABLE_PROJECT_CONFIG, runner.rules_env, #994), but wb still reads its
# `instructions` (project_config_instructions), so these keep the file tools off them.
OPENCODE_FILES = ("*opencode.json", "*opencode.jsonc", ".opencode/*", "*/.opencode/*")
# Every MCP server's tools but workbench's: the user's own servers in their global OpenCode config
# (a GitHub or database server, with their token) reached every agent (the 2026-11-16 audit), and
# a project's could too. OpenCode names an MCP tool <server>_<tool>; no built-in tool has an
# underscore, but two permissions do, so they get their rules back after this one.
MCP_RULES = ({"action": "*_*", "resource": "*", "effect": "deny"},
             {"action": "workbench_*", "resource": "*", "effect": "allow"},
             {"action": "doom_loop", "resource": "*", "effect": "allow"})  # what --auto gave it

# The user's own MCP servers an agent may have (#1202, 2026-11-24): each one the user names in
# commands.json `mcp` (every task) or `repos.<path>.mcp` (that repo's tasks), from their OpenCode
# config, if it holds no credential: no `environment`, `headers` or `oauth`, no password in a URL
# or a key in its query, no token flag in its command and no {env:}/{file:} for OpenCode to fill
# in (the 2026-11-16 audit's GitHub and database servers reached every agent with their tokens).
# Only the user names one: `wb allow --always` never writes `mcp`. A name is letters, digits and
# hyphens, as OpenCode names its tools <server>_<tool>: an underscore would let `a_*` allow a
# server `a_b`'s tools, and a name whose tools a denied rule matches (`opencode`, DENIED_TOOLS)
# would be listed and never callable.
MCP_NAME = re.compile(r"^[A-Za-z0-9-]+$")
MCP_TOKEN_FLAG = re.compile(r"^--?[\w-]*(token|secret|passw|api[_-]?key|access[_-]?key|auth)",
                            re.I)
MCP_URL_PASSWORD = re.compile(r"\w+://[^/@\s:]*:[^/@\s]+@")
MCP_QUERY_KEY = re.compile(r"key|token|secret|passw|auth|sig|cred", re.I)


def mcp_list_refusal(v) -> str:
    """Why commands.json's `mcp` value ``v`` (top level or a repo's) is refused, or ''."""
    if v is None:
        return ""
    if not (isinstance(v, list) and all(isinstance(x, str) for x in v)):
        return '"mcp" must be a list of your OpenCode MCP servers\' names, e.g. ["docs"]'
    for name in v:
        if not MCP_NAME.match(name):
            return f'"mcp": {name!r}: a server\'s name is letters, digits and hyphens'
        if name == "workbench" or any(fnmatch.fnmatchcase(f"{name}_tool", p) for p in DENIED_TOOLS):
            return f'"mcp": {name!r} is a name workbench keeps for itself or denies; rename the server'
    return ""


def users_mcp_servers() -> dict:
    """The user's OpenCode MCP servers by name, or {}: the 2.x shape, mcp.servers.<name>, over
    the 1.x one, mcp.<name>, which OpenCode 2.3.2 still starts beside the layer's mcp.servers.
    Read in the 2.x shape alone, a 1.x entry was never stubbed (runner.users_servers_off) and
    started in every agent's OpenCode, its `environment` and `headers` with it; a 2.x stub of
    its name stops it (both measured, 2026-11-24, #1202)."""
    try:
        mcp = models.read_config().get("mcp") or {}
    except (OSError, ValueError, AttributeError):
        return {}
    if not isinstance(mcp, dict):
        return {}
    v2 = mcp.get("servers") if isinstance(mcp.get("servers"), dict) else {}
    return {**{k: v for k, v in mcp.items() if k != "servers" and isinstance(v, dict)}, **v2}


def mcp_refusal(name: str, entry) -> str:
    """Why the user's MCP server ``name`` (its OpenCode entry) can't go to an agent, or '': the
    key that holds a credential, never its value."""
    head = f"MCP server {name!r} (commands.json \"mcp\")"
    if not isinstance(entry, dict):
        return f"{head} isn't in your OpenCode config ({models.opencode_config_path()}): add it there"
    fix = f"; take it out of commands.json \"mcp\", or give {name!r} no credential"
    kind = entry.get("type")
    if kind == "local":
        cmd = entry.get("command")
        if not (isinstance(cmd, list) and cmd and all(isinstance(x, str) for x in cmd)):
            return f"{head}: its `command` isn't a list of strings"
        if entry.get("environment"):
            return f"{head} sets `environment`, which can hold a token{fix}"
        if flag := next((w for w in cmd if MCP_TOKEN_FLAG.match(w)), None):
            return f"{head}: its `command` passes {flag.split('=', 1)[0]}, a credential{fix}"
    elif kind == "remote":
        if entry.get("headers"):
            return f"{head} sets `headers`, which can hold a token{fix}"
        if entry.get("oauth") not in (None, False):
            return f"{head} sets `oauth`{fix}"
        url = entry.get("url")
        if not isinstance(url, str):
            return f"{head}: its `url` isn't a string"
        if any(MCP_QUERY_KEY.search(k) for k, _ in urllib.parse.parse_qsl(
                urllib.parse.urlsplit(url).query, keep_blank_values=True)):
            return f"{head}: its `url` carries a key in its query{fix}"
    else:
        return f"{head}: its `type` is neither local nor remote"
    text = json.dumps(entry)
    if MCP_URL_PASSWORD.search(text):
        return f"{head}: a URL in it carries a password{fix}"
    if "{env:" in text or "{file:" in text:
        return f"{head} uses {{env:}} or {{file:}}, which OpenCode fills in from your environment or files{fix}"
    return ""


def mcp_servers(names) -> tuple[dict, list[str]]:
    """The user's servers ``names`` for the agent's layer, {name: entry}, and the refusal lines
    (mcp_refusal) of those that can't go. An entry goes as the user wrote it, `codemode` too:
    OpenCode 2.3.2 sends every tool as a direct one when `execute` is denied, whatever its
    `codemode`, and DENIED_TOOLS always denies it (its tool builder, read 2026-11-24; the
    real-OpenCode test's server says `codemode: true`)."""
    have = users_mcp_servers() if names else {}
    ok, bad = {}, []
    for name in names or ():
        if why := mcp_refusal(name, have.get(name)):
            bad.append(why)
        else:
            ok[name] = have[name]
    return ok, bad


def mcp_names(repo: str | Path, sub: str | None = None) -> list[str]:
    """The servers commands.json gives the tasks of ``repo`` (its package ``sub``): the top
    level's, then the repo entry's (repo_conf), each once."""
    conf = commands_conf()
    return list(dict.fromkeys([*(conf.get("mcp") or []), *(repo_conf(repo, sub).get("mcp") or [])]))


def task_mcp(task: store.Task) -> dict:
    """The user's servers this task's agent gets: task.json `mcp` (fixed at wb start), less any
    that now holds a credential (checked again every round: the user's config can change)."""
    return mcp_servers(task.spec.get("mcp") or [])[0]


# What marks a server's own project (mcp_project): its node_modules, its venv and its sources
# sit beside one of these
MCP_PROJECT_MARKS = ("package.json", "pyproject.toml", ".git")


def mcp_roots(task: store.Task) -> set[str]:
    """The files a task's local MCP servers run from, for the lane to read: for each absolute
    path in a server's command, its directory (its parent when it is a `bin`) both as written
    and resolved, each widened to its project (mcp_project); a directory named as itself. One
    under a non-dot home directory (~/src/schema-mcp) couldn't start in the lane otherwise. The
    resolved path alone missed the two usual layouts of a server built by hand (#1202's review,
    2026-11-24): `srv/.venv/bin/python -m schema_mcp` resolved to the base interpreter, so
    Python started without its venv, and `node srv/dist/index.js` read dist/ without
    srv/node_modules; each server died at its start in the lane. Never `/` nor the home
    directory itself, and on an API task never one that is, lies in or holds the checkout."""
    home = os.path.realpath(Path.home())
    # on an API task never the checkout, a directory in it or one holding it, which its lane
    # hides (#1091): only the widening was kept from it, and a server named in the repo
    # (`node <repo>/tools/mcp/server.js`) or the repo itself as a server's argument made its
    # gitignored .env one `cat` away from the provider again (#1202's review, 2026-11-24)
    checkout = _repo_parts(task)[0] if task.spec.get("via") == "api" else set()
    out = set()
    for entry in task_mcp(task).values():
        for word in entry.get("command") or () if entry.get("type") == "local" else ():
            w = os.path.expanduser(word)
            if not os.path.isabs(w) or not os.path.exists(w):
                continue
            for p in dict.fromkeys((os.path.abspath(w), os.path.realpath(w))):
                if os.path.isdir(p):
                    d = p
                else:
                    d = os.path.dirname(p)
                    d = os.path.dirname(d) if os.path.basename(d) == "bin" else d
                    top = mcp_project(d, home)
                    if top and not _touches(os.path.realpath(top), checkout):
                        d = top
                d = os.path.realpath(d)
                if d not in ("/", home) and not _under(home, d) and not _touches(d, checkout):
                    out.add(d)
    return out


def _touches(d: str, dirs) -> bool:
    """Whether ``d`` is one of ``dirs``, lies in one or holds one."""
    return any(c == d or _under(d, c) or _under(c, d) for c in dirs)


def mcp_checkout_notes(names, repos) -> list[str]:
    """A line for each of the user's servers ``names`` whose command names a path that is, lies
    in or holds one of ``repos``: an API task's lane hides the checkout (#1091), so mcp_roots
    leaves it out and the server dies at its start there. OpenCode 2.3.2 starts a local server
    in the project's directory (its `resolveCwd`: the config's `cwd` from there, else the
    directory itself), the task's worktree, so a path relative to the repo names the
    worktree's copy (#1202's review, 2026-11-24). Its callers ask it only where a lane hides the
    checkout (Landlock or Seatbelt, the lane on): with none the server reads it."""
    home = os.path.realpath(Path.home())
    tops = {os.path.realpath(os.path.expanduser(str(r))) for r in repos}
    tops |= {os.path.abspath(os.path.expanduser(str(r))) for r in repos}
    out = []
    for name, entry in mcp_servers(list(names))[0].items():
        for word in entry.get("command") or () if entry.get("type") == "local" else ():
            w = os.path.expanduser(word)
            if not os.path.isabs(w):
                continue
            # as written and resolved, as mcp_roots reads it: a venv's python in the repo resolves
            # out of it, but its venv is left out
            hit = next(((p, t) for p in dict.fromkeys((os.path.abspath(w), os.path.realpath(w)))
                        if p not in ("/", home) and not _under(home, p)
                        for t in sorted(tops) if _touches(p, {t})), None)
            if not hit:
                continue
            p, top = hit
            holds = _under(top, p)
            out.append(f"note: MCP server {name!r} names {word}, "
                       + (f"which holds the repo {top}" if holds else f"in the repo {top}")
                       + ", which an API task's lane hides (#1091), so an API agent won't reach it: "
                       + ("name what it needs in the repo relative to it" if holds else
                          f"name it relative to the repo: `{os.path.relpath(p, top)}`")
                       + " (OpenCode starts a local server in the task's worktree)")
            break
    return out


def mcp_project(d: str, home: str) -> str | None:
    """The nearest of ``d`` and its parents holding a MCP_PROJECT_MARKS file, below ``home``
    when under it; never `/`, the home directory nor one holding it."""
    while d not in ("/", home) and not _under(home, d):
        if any(os.path.lexists(os.path.join(d, m)) for m in MCP_PROJECT_MARKS):
            return d
        up = os.path.dirname(d)
        if up == d:
            return None
        d = up
    return None


def rules_dir(task: store.Task) -> Path:
    """Where the agent's rules live (its OpenCode config, its instructions, the git shim): out
    of its lane, so it can't rewrite them for its next round."""
    d = store.home() / "rules" / task.name
    d.mkdir(parents=True, exist_ok=True)
    return d


def grants(task: store.Task) -> dict:
    """What Claude allowed this task beyond the everyday rules: {"allow": [patterns], "paths":
    [dirs], "ports": [TCP ports], "test_tools": [patterns, ASK_FIRST still asked]}. Kept with
    the rules, out of the agent's reach: an agent that edited its own grants would widen its
    next round."""
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


# The user's own instructions for agents, in every project, beside workbench's other config. The
# user's ~/.claude/CLAUDE.md, its imports and ~/.claude/rules/** went to every agent (#673, #513)
# until 2026-11-23, when the owner's global CLAUDE.md, written to Claude and naming a private
# tool's folder, went to two API providers with every calibration task: those files are Claude's,
# not the agents' (#1061). Relative to the home directory, read at each call.
USER_AGENTS_MD = Path(".config") / "workbench" / "AGENTS.md"


def user_agents_md() -> Path:
    """The user's file for agents: $WB_AGENTS_MD, else ~/.config/workbench/AGENTS.md
    (USER_AGENTS_MD). The variable is the tests' own file: the user's went into their prompts,
    and in an agent's lane, which hides it, every test that started a task failed (#1102)."""
    return Path(os.environ.get("WB_AGENTS_MD") or Path.home() / USER_AGENTS_MD)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tracked(worktree: Path, *patterns: str) -> list[str]:
    out = gitops.git(worktree, "ls-files", "-z", "--", *patterns)
    return sorted(p for p in out.split("\0") if p)


# A CLAUDE.md split with `@` imports (`See @README.md`, `@docs/testing.md`, a user's
# `@~/.claude/rules/python.md`), as Claude Code's memory docs suggest, reached the agent as those
# literal lines: OpenCode 2.3 reads each instruction file as a raw string, and an import into
# ~/.claude was unreadable in the lane besides (#513, the audit of 1.90). So the imports are listed
# as instruction files of their own, after the file that imports them, as Claude Code loads them:
# relative to that file's real directory, `~` expanded, IMPORT_HOPS deep, code spans, fences and
# HTML comments skipped, missing or non-text targets ignored. One outside the worktree and the repo
# is copied into rules/<task>/imports/, which the lane reads. Claude Code asks before a project's
# file imports from outside the project; wb can't, so a project's imports reach only the repo and
# the Markdown in Claude's directory (no dot-named entry there: .credentials.json), and the user's
# own AGENTS.md (USER_AGENTS_MD, #1061) reaches anywhere but a secret or workbench's own files,
# the Markdown beside it in workbench's config directory among what it may (`@python.md`).
IMPORT = re.compile(r"(?:^|\s)@((?:[^\s\\]|\\ )+)")
IMPORT_HOPS = 5
FENCE = re.compile(r" {0,3}(`{3,}|~{3,})")


def imports_in(text: str) -> list[str]:
    """The `@` import paths in a Markdown file's ``text``, as Claude Code reads them."""
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    found, fence = [], ""
    for line in text.splitlines():
        if m := FENCE.match(line):
            mark = m.group(1)
            if not fence:
                fence = mark
            elif mark[0] == fence[0] and len(mark) >= len(fence) and not line.strip(" `~"):
                fence = ""
            continue
        if fence:
            continue
        line = re.sub(r"(`+).+?\1", " ", line)
        for m in IMPORT.finditer(line):
            p = m.group(1).split("#", 1)[0].replace("\\ ", " ")
            if p.startswith(("./", "~/")) or (p.startswith("/") and p != "/") or (
                    re.match(r"[a-zA-Z0-9._-]", p) and not re.match(r"[#%^&*()]+", p)):
                found.append(p)
    return found


def _text_file(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            head = f.read(8192)
    except OSError:
        return False
    if b"\0" in head:
        return False
    try:
        head.decode("utf-8")
    except UnicodeDecodeError as e:  # a character cut at the 8 KiB mark is still text
        return e.start >= len(head) - 3
    return True


def import_files(task: store.Task, roots: list[tuple[Path, Path, bool]]) -> list[Path]:
    """Each of ``roots`` (the path OpenCode loads, the file it was read from, whether it is the
    user's own), followed by what it imports, depth first: the paths OpenCode should load."""
    wt, repo = task.worktree, os.path.realpath(task.spec["repo"])
    claude = os.path.realpath(claude_home())
    config = os.path.realpath(user_agents_md().parent)
    hidden = secret_paths() | private_paths()
    within = lambda p, d: p == d or _under(p, d)  # noqa: E731
    copies = rules_dir(task) / "imports"
    seen: set[str] = set()
    made: set[Path] = set()
    out: list[Path] = []

    def where(real: str, users: bool) -> Path | None:
        """Where the agent reads the import at ``real``: the worktree's copy, the repo's, or a copy
        in its rules; None when it may not have it."""
        for base in (os.path.realpath(wt), repo):
            if within(real, base):
                rel = os.path.relpath(real, base)
                return wt / rel if (wt / rel).is_file() else Path(real)
        if within(real, claude):
            mine = real.endswith(".md") and not any(
                part.startswith(".") for part in Path(os.path.relpath(real, claude)).parts)
            hidden_ = hidden - {claude}
        elif users and within(real, config):  # the user's AGENTS.md split beside it (#1061)
            mine = real.endswith(".md") and not any(
                part.startswith(".") for part in Path(os.path.relpath(real, config)).parts)
            hidden_ = hidden - {config}
        else:
            mine, hidden_ = users, hidden
        if not mine or any(within(real, h) for h in hidden_):
            return None
        copy = copies / str(len(seen)) / Path(real).name
        copy.parent.mkdir(parents=True, exist_ok=True)
        # replaced whole, never emptied first: a running agent's OpenCode may be reading it (a
        # wb allow rewrites the layer mid-round)
        tmp = copy.with_name(f".{copy.name}.{os.getpid()}.tmp")
        shutil.copyfile(real, tmp)
        os.replace(tmp, copy)
        made.add(copy)
        return copy

    def visit(load: Path, real: str, users: bool, hops: int) -> None:
        seen.add(real)
        out.append(load)
        if hops >= IMPORT_HOPS:
            return
        try:
            text = Path(real).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        for p in imports_in(text):
            target = os.path.realpath(os.path.join(os.path.dirname(real), os.path.expanduser(p)))
            if target not in seen and os.path.isfile(target) and _text_file(target) and \
                    (at := where(target, users)):
                visit(at, target, users, hops + 1)

    for load, source, users in roots:
        if (real := os.path.realpath(source)) not in seen:
            visit(load, real, users, 0)
    for old in copies.glob("*/*") if copies.is_dir() else ():  # imports dropped since
        if old not in made:
            old.unlink(missing_ok=True)
    return out


def _md_under(src: Path) -> list[Path]:
    """The `.md` files under ``src``, through its directory links, each real directory once (a
    link loop ends), no dotted name (a shared rules checkout's .git): Path.rglob follows no
    directory link, so a rules directory shared by a link (`.claude/rules/team -> ~/team-rules`),
    as Claude Code documents, never reached the agent (#828)."""
    out, seen = [], set()
    for d, dirs, files in os.walk(src, followlinks=True):
        real = os.path.realpath(d)
        if real in seen:
            dirs[:] = []
            continue
        seen.add(real)
        dirs[:] = [n for n in dirs if not n.startswith(".")]
        out += [Path(d) / f for f in files if f.endswith(".md") and not f.startswith(".")]
    return sorted(out)


def _copy_rule(real: str, copy: Path) -> None:
    copy.parent.mkdir(parents=True, exist_ok=True)
    tmp = copy.with_name(f".{copy.name}.{os.getpid()}.tmp")  # whole: OpenCode may be reading
    shutil.copyfile(real, tmp)
    os.replace(tmp, copy)


def project_rules(task: store.Task, wt: Path) -> list[tuple[Path, Path, bool]]:
    """The project's tracked `.claude/rules/**/*.md`, as import_files' roots: (what OpenCode
    loads, read from, the user's own). One a committed link takes outside the worktree and the
    repo (`.claude/rules/shared -> ~/company-standards`) is copied into
    rules/<task>/project-rules/, which the lane reads: loaded by its worktree path, the lane
    refused it, and a linked directory's files weren't listed at all, so Claude reviewed against
    rules the agent never received (#828). Not a secret or workbench's own; its imports reach
    what a project's do; copies of rules gone since go too."""
    base, repo = os.path.realpath(wt), os.path.realpath(task.spec["repo"])
    within = lambda p, d: p == d or _under(p, d)  # noqa: E731
    hidden = secret_paths() | private_paths()
    top, out_dir = wt / ".claude" / "rules", rules_dir(task) / "project-rules"
    files: list[Path] = []
    for p in _tracked(wt, ".claude/rules"):  # a link is one entry, its files none
        f = wt / p
        if f.is_symlink() and f.is_dir():
            files += _md_under(f)
        elif p.endswith(".md"):
            files.append(f)
    out, made = [], set()
    for f in files:
        real = os.path.realpath(f)
        if not os.path.isfile(real):
            continue
        if within(real, base) or within(real, repo):
            out.append((f, f, False))
        elif not any(within(real, h) for h in hidden):
            copy = out_dir / f.relative_to(top)
            _copy_rule(real, copy)
            made.add(copy)
            out.append((copy, Path(real), False))
    for old in out_dir.rglob("*.md") if out_dir.is_dir() else ():
        if old not in made:
            old.unlink(missing_ok=True)
    return out


def handbook_for(task: store.Task) -> Path:
    """The handbook this task's agent gets, as rules/<task>/HANDBOOK.md whichever it is (so the
    instruction path and fixed_prompt's "the handbook" hold): the writing role's prose rules
    (agent/WRITING.md), else the coding ones (#1025)."""
    return WRITING if task.spec.get("role") == "writing" else HANDBOOK


def spec_note(task: store.Task) -> str:
    """The handbook copy's last section: where the step's spec is (store.round_prompt of its
    first round, readable in the lane: the rounds' prompts). OpenCode's mid-round compaction keeps
    the system prompt and only a summary of the spec, and the handbook tells the agent to trust
    no summary: a session that only read never got the spec back, and the agent asked Claude for
    it or built from the summary (#1071, 2026-11-23). Rewritten each round, for the step's."""
    path = store.round_prompt(task, task.status.get("step_round") or 1)
    return (f"\n## Your task\nIn full, it is Claude's spec in `{path}`. If your context holds only "
            f"a summary of it (OpenCode compacted your session), read that file again first.\n")


def instruction_files(task: store.Task) -> tuple[list[Path], list[str]]:
    """(files OpenCode should load, nested CLAUDE.md paths the agent reads when it works there).
    Before the runner has checked the worktree out (wb start's fixed_prompt, #529), the user's
    checkout stands in for it."""
    repo = Path(task.spec["repo"])
    wt = task.worktree if task.worktree.is_dir() else repo
    # the agent's own copy: OpenCode shows each instruction file's path, and a path into the
    # workbench repo once led a freshly started agent to go looking there for plans
    handbook = rules_dir(task) / "HANDBOOK.md"
    handbook.write_text(handbook_for(task).read_text() + spec_note(task) + "".join(
        note for name, note in EXPERIMENT_NOTES.items() if experiment(task, name)))
    roots: list[tuple[Path, Path, bool]] = []  # (what OpenCode loads, read from, the user's own)
    # the user's AGENTS.md, never Claude's CLAUDE.md or rules (#1061, USER_AGENTS_MD); a copy: the
    # lane can't read workbench's config directory (private_paths)
    user, mine = user_agents_md(), rules_dir(task) / "AGENTS.md"
    if user.is_file():
        mine.write_bytes(user.read_bytes())  # as it is: a CP1252 byte failed every start (#784)
        roots.append((mine, user, True))
    else:
        mine.unlink(missing_ok=True)
    # the copies of Claude's that an earlier wb made for this task, which the lane could still read
    (rules_dir(task) / "CLAUDE.md").unlink(missing_ok=True)
    shutil.rmtree(rules_dir(task) / "user-rules", ignore_errors=True)
    load: list[Path] = []
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
    proj = [(p, p, False) for p in load] + project_rules(task, wt)  # through their links (#828)
    # The project's AGENTS.md first, the package's before the top's, where OpenCode put them when
    # it read them itself; the project's opencode.json `instructions` last, after the layer's, as
    # OpenCode loaded that file after it (#994). A CLAUDE.md with an AGENTS.md's text is skipped:
    # one copy is enough (one only in the user's checkout isn't loaded, so it can't stand in).
    agents = agents_files(task, wt)
    load = [*agents, handbook, *import_files(task, roots + proj),  # #513
            *project_config_instructions(task, wt)]
    seen, files = set(), []
    for p in load:
        d = _digest(p)
        if d not in seen:
            seen.add(d)
            files.append(p)
    files = _checkout_copies(task, files)
    nested = [p for p in _tracked(wt, "*CLAUDE.md", "*AGENTS.md")
              if "/" in p and Path(p).name in ("CLAUDE.md", "AGENTS.md")
              and not p.startswith(".claude/") and Path(p).parent.as_posix() != sub
              and wt / p not in agents]
    return files, nested


def _checkout_copies(task: store.Task, files: list[Path]) -> list[Path]:
    """For an API task, each of ``files`` the user's checkout holds and the worktree doesn't
    (CLAUDE.local.md, a gitignored package CLAUDE.md, one not committed yet, an import there) as a
    copy in rules/<task>/checkout/, which its lane reads: it doesn't read the checkout
    (read_repo_dirs, #1091). A GPU task's as they are. Copies made afresh each call."""
    copies = rules_dir(task) / "checkout"
    shutil.rmtree(copies, ignore_errors=True)
    if task.spec.get("via") != "api":
        return files
    repo, wt = os.path.realpath(task.spec["repo"]), os.path.realpath(task.worktree)
    out = []
    for p in files:
        real = os.path.realpath(p)
        if _under(real, repo) and not (real == wt or _under(real, wt)):
            copy = copies / os.path.relpath(real, repo)
            copy.parent.mkdir(parents=True, exist_ok=True)
            copy.write_bytes(Path(real).read_bytes())
            p = copy
        out.append(p)
    return out


# Where OpenCode found a project's AGENTS.md, skills and config itself: the directory it runs in
# and each one above it (on up to the home directory; above the worktree is workbench's state,
# not the project, so wb stops at the worktree's top). The runner now shuts that reading out
# (OPENCODE_DISABLE_PROJECT_CONFIG, #994, 2026-11-22: what a project's opencode.json or .opencode/
# sets would load after the task's layer and could lift its rules), and wb hands these back
# through the layer's `instructions` and `skills`. Measured on OpenCode 2.3.2 with
# tests/capture_llm.py: the switch drops the project's AGENTS.md (and those above it), its
# skills in .claude/skills, .agents/skills, .opencode/skills and .opencode/skill (nested ones
# too), its opencode.json and .opencode/ (instructions, agents and their prompts, commands,
# plugins); the global ~/.config/opencode/AGENTS.md and the user's own skills still load.
PROJECT_SKILL_DIRS = (".claude/skills", ".agents/skills", ".opencode/skills", ".opencode/skill")
PROJECT_CONFIGS = ("opencode.json", "opencode.jsonc", ".opencode/opencode.json",
                   ".opencode/opencode.jsonc")


def _project_dirs(task: store.Task, wt: Path) -> list[Path]:
    """The task's working directory (its package, task.json `subdir`) and each one above it,
    up to the worktree's top ``wt``, nearest first."""
    parts = Path(task.spec.get("subdir") or "").parts
    return [wt.joinpath(*parts[:i]) for i in range(len(parts), -1, -1)]


def agents_files(task: store.Task, wt: Path) -> list[Path]:
    """The project's AGENTS.md files OpenCode would have loaded, nearest first."""
    return [d / "AGENTS.md" for d in _project_dirs(task, wt) if (d / "AGENTS.md").is_file()]


def project_skill_dirs(task: store.Task) -> list[Path]:
    """The project's skill directories OpenCode would have listed (PROJECT_SKILL_DIRS), each a
    parent the layer's `skills` lists every skill under."""
    wt = task.worktree if task.worktree.is_dir() else Path(task.spec["repo"])
    return [d / s for d in _project_dirs(task, wt) for s in PROJECT_SKILL_DIRS if (d / s).is_dir()]


def project_config_instructions(task: store.Task, wt: Path) -> list[Path]:
    """The files a project's opencode.json (PROJECT_CONFIGS, in the task's directory or above
    it) names in `instructions`, found as OpenCode finds them: each pattern globbed in the task's
    directory and each one up to the worktree's top. Only what is in the worktree: a URL (OpenCode
    fetched it on every run, a network call nobody allowed), an absolute or `~/` path (outside the
    project, where its other keys stay unread too) are left out. Its other keys stay unread on
    purpose: `plugin` and .opencode/plugins run code in OpenCode's process, outside the rules;
    `formatter` and `lsp` run commands (and may fetch servers) the shell rules never see; `agent`,
    `mode` and `command` replace the prompt and the tools wb measured; `permission(s)`, `mcp`,
    `provider`, `model`, `compaction` and the like are the layer's (#994, 2026-11-22)."""
    dirs = _project_dirs(task, wt)
    top = os.path.realpath(wt)
    patterns: list[str] = []
    for d in dirs:
        for name in PROJECT_CONFIGS:
            try:
                cfg = json.loads(models._strip_jsonc((d / name).read_text()))
            except (OSError, ValueError, UnicodeDecodeError):
                continue
            got = cfg.get("instructions") if isinstance(cfg, dict) else None
            patterns += [p for p in got if isinstance(p, str)] if isinstance(got, list) else []
    out: list[Path] = []
    for pat in dict.fromkeys(patterns):
        if not pat or "://" in pat or pat.startswith(("~", "/")) or os.path.isabs(pat):
            continue
        for d in dirs:
            for rel in sorted(glob.glob(pat, root_dir=d, recursive=True, include_hidden=True)):
                f = d / rel
                if f.is_file() and _under(os.path.realpath(f), top) and f not in out:
                    out.append(f)
    return out


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
    rules = rules_dir(task)
    # at wb start the runner has yet to check the worktree out (#529): the checkout stands in
    wt = task.worktree if task.worktree.is_dir() else Path(task.spec["repo"])
    named = {rules / "HANDBOOK.md": "the handbook", rules / "AGENTS.md": f"~/{USER_AGENTS_MD}"}
    paths = [*files, task.round_dir(1) / "prompt.md"]  # AGENTS.md among the files (#994)
    parts = [("OpenCode's own prompt and tools", OPENCODE_BASE_TOKENS)]
    for p in paths:
        try:
            n = p.stat().st_size // CHARS_PER_TOKEN
        except OSError:
            continue
        if p.is_relative_to(rules / "project-rules"):
            named[p] = f".claude/rules/{p.relative_to(rules / 'project-rules')}"
        elif p.is_relative_to(rules / "checkout"):  # an API task's copy (#1091)
            named[p] = str(p.relative_to(rules / "checkout"))
        label = named.get(p) or ("the spec" if p.name == "prompt.md" else
                                 str(p.relative_to(wt)) if p.is_relative_to(wt) else p.name)
        parts.append((label, n))
    parts.sort(key=lambda x: -x[1])
    return sum(n for _, n in parts), parts


def denied_skills(extra: list[str] | None = None, project: list[Path] | None = None) -> list[str]:
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
    # a project's own skill keeps its name (the personal ones are unreadable anyway): ``project``,
    # its skill directories (project_skill_dirs), nested skills too, as OpenCode lists them
    for own in project or ():
        names -= {s.parent.name for s in own.rglob("SKILL.md")}
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
    # the user's servers commands.json names (#1202): after MCP_RULES' deny, before DENIED_TOOLS,
    # whose `opencode_*` and `execute` the last match keeps denied
    rules += [{"action": f"{n}_*", "resource": "*", "effect": "allow"} for n in task_mcp(task)]
    rules += [{"action": t, "resource": "*", "effect": "deny"} for t in DENIED_TOOLS]
    rules += shell_rules(task)
    rules += external_rules(task)
    rules += read_only_rules(task)
    rules += [{"action": "edit", "resource": f, "effect": "deny"}
              for f in CLAUDE_FILES + OPENCODE_FILES]
    # the skills Claude handed this task, one directory each: OpenCode lists exactly those, and
    # the rest of the library stays out of the prompt (a retired one is simply left out)
    have = skills.available()
    mine = [n for n in task.status.get("skills") or [] if n in have]
    handed = [str(have[n]["path"]) for n in mine]
    own = project_skill_dirs(task)  # the project's, which OpenCode no longer finds itself (#994)
    # A handed skill keeps its name though a personal skill has it too (a library `pdf`, then the
    # app synced the user's own): denied by name, its every load failed while the agent was told
    # to load it, and wb skill list advised rewriting a good skill (#515). OpenCode takes the
    # handed one by its library path, listed after the personal ones (checked on OpenCode 2.3)
    rules += [{"action": "skill", "resource": n, "effect": "deny"}
              for n in denied_skills(task.spec.get("deny_skills"), own)
              if n not in mine or n == "workbench" or n in (task.spec.get("deny_skills") or ())]
    # the skill library is Claude's to write, with or without the kernel's lane
    rules += [{"action": "edit", "resource": f"{p.rstrip('/')}/*", "effect": "deny"}
              for p in _both([skills.library()])]
    return {"instructions": [str(p) for p in files + ([note] if note else [])],
            "permissions": rules, "skills": [*handed, *map(str, own)],
            "agents": json.loads(json.dumps(AGENT_OFF)),  # made afresh in the content (AGENT)
            # the OpenCode plugin is the driver's, never an agent's (#1311, 2026-11-25): removed by
            # its id after the user's global `plugins` add it, so OpenCode never runs its setup
            # here (it still imports the module, which does nothing at import time); restated in
            # the content after the user's own (runner.rules_env)
            "plugins": [PLUGIN_OFF]}
