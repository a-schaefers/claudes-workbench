# Checks in the test gate: a compiler, typechecker or linter before the tests

`--test` runs after every clean round, and what fails goes back to the agent as an auto-fix round
without waking you. So a check that catches a broken name, a wrong type or a syntax error belongs
there, ahead of the tests: `--test 'mypy && python3 -m pytest -q'`. The agent hears of the mistake
from the round it made it in, at local cost, instead of from your review. This is what an
editor's language server would tell it, done the way OpenCode 2 itself recommends (it runs no
language servers: "replace workflows that depend on those capabilities with the project's lint,
typecheck, or compiler commands"). The owner's decision, 2026-11-22: no LSP, no setting.

## When to add one
1. **The project's own first.** A check its CI, Makefile, `package.json` scripts, `justfile`,
   `tox.ini`, `noxfile.py` or pre-commit config already runs, word for word (`npm run typecheck`,
   `make lint`): it is the project's standard, configured as the project wants.
2. **Else a check the project has configured**: the signal column below (a `[tool.mypy]`, a
   `tsconfig.json`, a `.rubocop.yml`). Configured means the project means it.
3. **Else, always fine: the syntax-only checks** (the last column). They need no config and never
   disagree with the project: a file that fails them is broken.
4. **An installed checker the project doesn't configure** (shellcheck, clj-kondo) only when it
   passes clean on the base, and say so in the report: it is a standard you chose.

Every one must pass on the base: the runner's base check runs `--test` once before round 1 and the
first report says what it saw. A check that fails on the base is noise the agent didn't cause:
drop it, or narrow it to the files the task touches. Never install a checker, add its config or
its dependency yourself: that is the user's (ask, and say what it would catch).

## Writing the command
- **It must fail by exit status.** Some exit 0 on what you want caught: Guile's `guild compile`
  on every warning, SBCL's `compile-file` on a style warning (an undefined function). Capture and
  test (the forms below); never `check | tail`, which reports tail's status.
- **Checks first, tests after**, joined with `&&`: a type error is cheaper to read than the test
  failure it causes. Keep the whole under `--test-timeout` (900 s); a slow whole-program
  analysis (dialyzer's first PLT, `nix flake check`, `sbt` cold) is narrowed or left to you.
- **The lane.** The tool caches are writable (`~/.cache`, `~/.cargo`, `~/.m2`, `~/.gradle`,
  `~/.mix`, `~/.hex`, `~/.cabal`, `~/.stack`, `~/.sbt`, `~/.ivy2`, `~/.nuget`, `~/.gem` and the
  rest of `context.TOOL_HOMES`), so builds work. Downloads go in `--setup` (`npm ci`, `mvn -q
  dependency:go-offline`, `terraform init -backend=false`, `dotnet restore`), and the check runs
  offline (`-o`, `--offline`, `--no-restore`, `npx --no-install`). A checker that talks to a daemon
  over a UNIX socket outside the lane (the Nix daemon, Docker, so Testcontainers) can't run there:
  run it yourself at review. A test's own Postgres or Redis runs from a temp directory, its socket
  there too, in one script so the variable lasts (`d=$(mktemp -d); initdb -D $d/pg && pg_ctl -D
  $d/pg -o "-k $d -c listen_addresses=''" -l $d/pg.log start`, then `PGHOST=$d`; `redis-server
  --port 0 --unixsocket $d/r.sock --save '' --dir $d`): sockets only, so no port meets the user's
  own server, and the default `/run/postgresql` is outside the lane.
- **The project's environment.** Run the checker the way the project does: `uv run mypy`,
  `poetry run pyright`, `bundle exec rubocop`, `vendor/bin/phpstan`, `./gradlew`, `npx
  --no-install tsc`; never a global copy of a different version.
- **Per-file syntax checks** take every file: `find src -name '*.php' -print0 | xargs -0 -n1 php
  -l >/dev/null` (xargs exits non-zero when any one fails).

## By language
Checked on this machine (2026-11-22): Guile and SBCL, for the exit-status forms below. The rest is
each tool's documented command line; the base check proves a command before round 1, so a wrong
one shows there, not in the agent's rounds.

| Language | Configured: the signal → the check | Syntax only, no config |
|---|---|---|
| C, C++ | the build (`make`, `cmake --build build`, `meson compile -C build`): it uses the project's own compiler and warnings, so it is the check; `.clang-tidy` + `compile_commands.json` → `clang-tidy -p build <files>` (clang's parser: on a GCC-only project it can refuse GCC's flags, so only where the project already runs it); `.cppcheck`/CI → `cppcheck --error-exitcode=1 <dirs>` | the project's compiler, never `cc`, which is GCC on Linux and clang on macOS: see *C and C++: which compiler* |
| Python | `[tool.mypy]`/`mypy.ini`/`setup.cfg [mypy]` → `mypy`; `pyrightconfig.json`/`[tool.pyright]` → `pyright`; `[tool.basedpyright]` → `basedpyright`; `ruff.toml`/`[tool.ruff]` → `ruff check`; `.flake8`/`[flake8]` → `flake8` | `python3 -m compileall -q <src>` |
| Go | always: `go vet ./...` (it type-checks); `staticcheck.conf`/CI → `staticcheck ./...`; `.golangci.yml` → `golangci-lint run` | `gofmt -e -l <dir> >/dev/null` |
| Rust | always: `cargo check --all-targets` (`--workspace` in a workspace); `clippy.toml` or CI's clippy → `cargo clippy --all-targets -- -D warnings` | (cargo check is it) |
| TypeScript | `tsconfig.json` → `npx --no-install tsc --noEmit -p .` (project references: `tsc -b`); an ESLint config → `npx --no-install eslint .` | (tsc is it) |
| JavaScript | an ESLint config → `npx --no-install eslint .`; `checkJs` in a `jsconfig.json`/`tsconfig.json` → `tsc --noEmit -p .` | `node --check <file>` |
| HTML | `.htmlvalidate.json` → `npx --no-install html-validate <files>` | none worth trusting |
| CSS | `.stylelintrc*` → `npx --no-install stylelint "**/*.css"` | none |
| JSON | a schema the project checks in CI → its command | `jq empty <files>`, or `python3 -m json.tool <file> >/dev/null` |
| YAML | `.yamllint*` → `yamllint .`; GitHub workflows with `actionlint` in CI → `actionlint` | `yq . <file> >/dev/null` where installed |
| Markdown | `.markdownlint*` → `npx --no-install markdownlint-cli2 "**/*.md"` | none (it has no syntax errors) |
| Bash, sh | `.shellcheckrc` or CI → `shellcheck <scripts>` | `bash -n <file>` (`sh -n` for POSIX sh) |
| Lua | `.luacheckrc` → `luacheck .`; `selene.toml` → `selene .` | `luac -p <file>` (the project's Lua version) |
| Ruby | `.rubocop.yml` → `bundle exec rubocop`; `sorbet/config` → `bundle exec srb tc`; `Steepfile` → `bundle exec steep check` | `ruby -wc <file> >/dev/null` |
| PHP | `phpstan.neon(.dist)` → `vendor/bin/phpstan analyse --no-progress`; `psalm.xml` → `vendor/bin/psalm --no-cache` | `php -l <file>` |
| Java | Maven → `mvn -q -o test-compile`; Gradle → `./gradlew --offline --no-daemon compileTestJava`; Checkstyle, SpotBugs, Error Prone: the goals the build already wires in | (the compile is it) |
| C# | `dotnet build --no-restore` (warnings as errors only where the project sets `TreatWarningsAsErrors`) | (the build is it) |
| Scala | sbt → `sbt -batch Test/compile`; `scala-cli` → `scala-cli compile .`; Mill → `./mill __.compile` (slow to start: mind the timeout) | (the compile is it) |
| Elixir | `mix compile --warnings-as-errors` (Elixir 1.18+ type-checks there too); `.credo.exs` → `mix credo --strict`; dialyxir with a built PLT → `mix dialyzer` | (mix compile is it) |
| Erlang | `rebar3 compile` (`warnings_as_errors` in `rebar.config`'s `erl_opts` makes warnings fail) and `rebar3 xref`; a built PLT → `rebar3 dialyzer` | `erlc -Wall +warnings_as_errors -o "$(mktemp -d)" <file>` |
| Haskell | Cabal → `cabal build all`; Stack → `stack build --fast`; `.hlint.yaml` → `hlint .` | (the build is it) |
| OCaml | dune → `dune build @check` (type-checks without linking) | (dune is it) |
| Zig | `zig build` (a `check` step in `build.zig` → `zig build check`); Zig analyses only reached code, so `zig build test` covers more | `zig ast-check <file>` |
| Terraform | `--setup 'terraform init -backend=false -input=false'`, then `terraform validate -no-color`; `.tflint.hcl` → `tflint`; `terraform fmt -check -recursive` only where CI enforces it; `plan` and `test` (it creates real infrastructure) are yours at review | (validate is it) |
| Nix | `statix`/`deadnix` configs → `statix check`, `deadnix --fail`; `nix flake check` needs the daemon: yours at review | `nix-instantiate --parse <file> >/dev/null` |
| Clojure | `.clj-kondo/` → `clj-kondo --lint src:test`; Leiningen → `lein check` (compiles every namespace) | (clj-kondo is it) |
| Racket | `raco make -v <main>.rkt` (a module's unbound names and syntax fail at compile; `#lang typed/racket` type-checks there) | (raco make is it) |
| Scheme: Guile | `guild compile` with the bug-catching warnings, failed on them: the form below (unbound variables, arity, format) | (that is it) |
| Scheme: CHICKEN | `csc -analyze-only <file>` (the scrutinizer's type and arity warnings) | (that is it) |
| Scheme: Chez, Gauche, Chibi, MIT | no checker worth trusting: the tests are the check (compiling a Chez program catches only syntax) | none |
| Common Lisp (SBCL) | ASDF: the form below; SBCL's compiler infers types, so a type error is a compile-time warning | one file: the form below |
| Forth | none: words compile as the file loads, which runs it; the tests are the check | none |

## C and C++: which compiler
Not every project builds with both GCC and clang: GCC-only extensions and flags (nested functions,
`-fanalyzer`, some `__attribute__`s), clang-only warnings and flags, or a cross compiler. So a
check never picks one; it uses the one the project builds with:
- **Prefer the build.** With a build system, the build itself is the check, through its own
  configuration; add `-Werror` only where the project already uses it.
- **Find the compiler** when you need a syntax-only check: `compile_commands.json`'s `command` or
  `arguments` (the first word), `CMakeCache.txt`'s `CMAKE_C_COMPILER`/`CMAKE_CXX_COMPILER`, the
  Makefile's `CC`/`CXX`, `meson introspect --compilers build`, or the README and CI. Then
  `"$CC" -fsyntax-only <the project's -I, -D and -std flags> <file>`: GCC and clang both take
  `-fsyntax-only`.
- **With `compile_commands.json`**, each file's own command is exact: run it with `-fsyntax-only`
  in place of `-c` and its `-o <obj>` dropped. A per-file loop over the files the task touches
  stays fast.
- **A project built with both** (CI with a GCC and a clang job): the check runs both, `make CC=gcc
  && make clean && make CC=clang`, when the base passes both.
- **clang-tidy on a GCC project**: only where the project runs it; it parses with clang, and
  GCC-only flags in `compile_commands.json` make it fail on the base (`--extra-arg=-Wno-unknown-warning-option`
  quiets only unknown warnings, not unknown flags).

The two forms that need care, each failing by exit status (run here, 2026-11-22):
```sh
# Guile: guild exits 0 on every warning, so fail on its output (one file; loop for more). Name the
# warnings: -W3 adds "possibly unused module", which every script gets
out=$(guild compile -Wunbound-variable -Warity-mismatch -Wformat -Wmacro-use-before-definition \
  -o "$(mktemp -d)/x.go" FILE.scm 2>&1) && ! printf '%s' "$out" | grep -q 'warning:'

# SBCL, an ASDF system (the worktree's own .asd first: ASDF wouldn't find it): a warning, a type
# error among them, fails the compile
sbcl --non-interactive --no-userinit --eval '(require :asdf)' \
  --eval '(push (uiop:getcwd) asdf:*central-registry*)' \
  --eval '(setf asdf:*compile-file-failure-behaviour* :error)' \
  --eval '(asdf:compile-system :SYSTEM :force t)'

# SBCL, one file: exit 1 on a warning (a type error), 2 on a style warning (an undefined function)
sbcl --non-interactive --no-userinit --eval '(multiple-value-bind (o w fail)
  (compile-file "FILE.lisp" :output-file "/tmp/wb-check.fasl") (declare (ignore o))
  (sb-ext:exit :code (cond (fail 1) (w 2) (t 0))))'
```
The fasl goes to the temp directory, never beside the source, where it would join the diff.
