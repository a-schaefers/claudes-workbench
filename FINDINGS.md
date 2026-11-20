# Workbench 1.6 production-readiness audit

2026-11-20. Claude's audit of v1.6 (origin/main ad43797, the v1.6 tag's merge). No code was
changed. Arch Linux, kernel 7.2 (Landlock), Python 3.14.9, Git 2.56.0, OpenCode 2.3.0, no
git-lfs.

The audits of 1.0 (#189–#217), 1.2 (#241–#274) and 1.4 (#320–#353) are fixed or closed, and
none of their findings is repeated here. The goal is to find what still doesn't work for some
users: their hardware, their operating system, how they keep their config, how their repos are
laid out, how they use the product. Security is out of scope, except where a rule breaks a
user's own setup.

How each finding was confirmed:
- **Reproduced:** run here, against the real wb, the real OpenCode 2.3 or real git.
- **Source:** the code path was read and shown to behave as described, but not run.
- **Not checked:** it depends on a tool or setup that isn't here.

Source lines are at ad43797.

## Verdict: 8.5 out of 10

1.6 closed every finding of the 1.4 audit, and most of the edges probed this time were already
handled:
- logout and linger;
- the chat's code reload;
- suspend and frozen clocks;
- shell edits counting as edits;
- version managers in the lane;
- proxies to local servers;
- legacy locales;
- old Pythons;
- case-folding file systems.

The suite passes: 591 tests, 1 skipped, in 474 s.

What is left falls into two kinds:
- Places where wb's model of the world is narrower than OpenCode's or Claude Code's: where
  OpenCode keeps its config and skills (1, 2), and how long Claude's Bash lets a command run (3).
- Costs that grow with the user's setup: watching scratch clones (4), signing (5), monorepos (6)
  and leftovers (7).

Fixing 1–7, each with a regression test, gets it to 9. Findings 8–15 take it past that, for the
shared machines and the less common setups.

## High

**1. wb doesn't read OpenCode's config where `OPENCODE_CONFIG_DIR` or a system config puts it.**
Reproduced (`OPENCODE_CONFIG_DIR`); Source (OpenCode's bundle) for the system config.
- **Case:**
  - A user keeps OpenCode's config in a directory of their own, with `OPENCODE_CONFIG_DIR` set
    in their shell (a dotfiles repo, a work and a home profile).
  - Or an admin ships providers in the system config: `/etc/opencode/opencode.json` on Linux,
    `/Library/Application Support/opencode/` on macOS (managed installs).
- **What happened:** `OPENCODE_CONFIG_DIR=<dir>` held one local provider.
  - `opencode debug config --standalone` listed `<dir>/opencode.json` and loaded it.
  - `wb models` said "no local models: OpenCode has no local provider in
    ~/.config/opencode/opencode.json".
- **Also:**
  - `wb model add`, `wb optimize` and `wb remote` would write `~/.config/opencode/opencode.json`,
    which OpenCode then ignores.
  - `wb doctor` checks the wrong file.
- **Why:**
  - OpenCode 2.3 takes its global directory from `process.env.OPENCODE_CONFIG_DIR ??
    ~/.config/opencode`, and layers the system directory before it.
  - `models._config_dir` knows only `$XDG_CONFIG_HOME/opencode`.
- **Fix:**
  - Read the global directory as OpenCode does, and layer the system file first in `read_config`.
  - `config_target` refuses to write the system file.
  - Pass `OPENCODE_CONFIG_DIR` through to the agent unchanged (it is today, as a non-secret).
  - Test both through `opencode debug config --standalone`, beside `wb models`.
- **Source:** models.py:99, models.py:113.

**2. Personal skills in `~/.agents/skills` reach every agent.** Reproduced.
- **Case:** a user keeps skills in `~/.agents/skills`, the cross-tool location other agents read
  too.
- **What happened:** OpenCode 2.3 was pointed at `tests/capture_llm.py`, with one skill in
  `~/.agents/skills` and one in `~/.claude/skills`. Both were offered to the model.
- **Why:**
  - OpenCode's compatibility layer reads `skills/` under both `~/.claude` and `~/.agents`, and
    under the project's `.claude` and `.agents`.
  - `context.denied_skills` denies only the `~/.claude/skills` names.
- **The cost:**
  - Each personal skill's description is prefill on every round.
  - It is also the drift CLAUDE.md records from 2026-11-16: an agent summarised a personal skill
    into a made-up task.
- **Fix:**
  - Deny `~/.agents/skills` (and `$XDG_CONFIG_HOME`'s, if OpenCode reads one there) like
    `~/.claude/skills`.
  - Keep a project's own `.agents/skills`, as its `.claude/skills` is kept.
  - Add the case to the real-OpenCode skills test.
- **Source:** context.py:1043.

**3. `wb verify` and `wb shot` outrun Claude's Bash, and a kill leaves their scratch clones and
tests running.** Source.
- **Case:** a suite that takes minutes, or a setup to copy, on a review.
  - `wb verify` runs the base and then the candidate, each up to 300 s (`verify.LIMIT`), after
    copying the prepared paths into each scratch clone.
  - The skill tells Claude to run it with no word on running it in the background.
  - Claude's Bash stops a command at 2 minutes by default, 10 at most.
- **What happens:**
  - The Bash tool kills `wb verify`. The CLI has no SIGTERM handler, so its
    `TemporaryDirectory` is never removed.
  - A full clone, with its copy of node_modules or target/, stays in `$WB_HOME/.verify-*`.
  - The test run, started in a session of its own (`start_new_session`), keeps running in it.
  - Claude gets no verdict, and the review often goes on without the evidence.
- **Fix:**
  - Run verify and shot detached, like the runner. Write the result to the task's directory and
    raise it as a notice, or tell Claude to use `run_in_background`, as the skill already does
    for `wb wait`.
  - Either way, a SIGTERM handler that kills the run's group and removes the scratch.
  - Test a 150 s check under a 120 s kill: nothing is left.
- **Source:** cli.py:1582, verify.py:89, skill/workbench/SKILL.md:328.

**4. Every `wb events` watches each scratch clone, directory by directory, and wakes its mod on
every write.** Reproduced (the watches); Source (the polls).
- **Case:** the base check of every task with `--test`, `wb verify`, `wb shot`, a bench replay
  and a submodule fill. Each makes a clone or index under `$WB_HOME` with a dot name.
- **What happened:** a Watcher was built as `wb events` builds it. A `.verify-abc/repo` holding a
  node_modules of 300 packages took it from 2 watched directories to 605.
- **Why:**
  - `EVENT_SKIP` lists `tasks/*/worktree`, `rules`, `archive` and others, but not the dot-named
    scratches.
  - The dot filter in `cmd_events` only stops them being reported by name.
  - Each change under them is a `{"changed": …}` line, and the mod polls on every line, a fresh
    `wb` each time.
- **The cost:**
  - Every Claude Code session runs its own `wb events`, so this costs a stream of polls in every
    session, for every base check.
  - Where the kernel's `max_user_watches` is 8,192 (kernels before 5.11: Ubuntu 20.04, Debian
    11, RHEL 8), a copied node_modules exhausts it:
    - editors and other tools lose their file watching;
    - wb falls back to scanning those directories four times a second.
- **Fix:**
  - Skip `.*` at the top of `$WB_HOME` (and `wb-index-*`, `wb-merge-*`) in `EVENT_SKIP`, or make
    the scratches outside the watched tree.
  - Test that a base check of a repo with a copied node_modules adds no watches and prints no
    lines.
- **Source:** cli.py:2809, watch.py:93, mod/hooks/register.tsx:843.

**5. `wb accept` fails at the very end for a user who signs commits with a passphrase.**
Reproduced (git with SSH signing).
- **Case:** `commit.gpgsign=true` with a passphrase-protected key and no agent holding it, or a
  pinentry that needs a terminal. That is the usual setup for GitHub's verified commits.
- **What happened:**
  - Signing's prompt has no terminal under Claude's Bash: `git commit` without one failed in
    8 ms with "incorrect passphrase supplied to decrypt private key? fatal: failed to write
    commit object".
  - `commit_all` keeps signing on purpose. So `wb accept` and `wb next` fail only after the whole
    task, with a message that blames the passphrase.
  - A graphical pinentry instead pops a dialog on a desktop that may be unattended, and the
    command waits out the Bash limit.
- **Fix:** check signing at `wb start`, as `_identity` checks the identity.
  - A dry `git commit-tree -S` of the base's tree, with no terminal and a short timeout.
  - Said once: "your commits are signed and the key needs unlocking: `ssh-add` / `gpg-connect-agent
    reloadagent` before accept, or `--no-sign` for this task".
  - At accept, turn git's error into that sentence.
- **Source:** gitops.py:492, cli.py:416.

## High: project shapes

**6. A monorepo package with its own CLAUDE.md is refused, and the agent works from the root.**
Reproduced (the refusal); Source (the root).
- **Case:** `packages/app/CLAUDE.md` is committed and the root has none. Claude Code runs in
  `packages/app`, where it reads that file as the project's memory.
- **What happened:** `wb start --repo .` from `packages/app` refused: "<root> has no CLAUDE.md (or
  .claude/CLAUDE.md) at HEAD".
  - With `--no-claude-md`, the agent's session, `--setup` and `--test` all run at the worktree's
    root (`cwd=task.worktree`).
  - The package's file is only a pointer to read "where it applies", so `npm test` runs the
    workspace root's script, or none.
- **Fix:**
  - Record the package as task.json `subdir` (the `--repo` path below the top level).
  - Accept its CLAUDE.md for the check, and load it as a root instruction.
  - Start the agent, the setup and the tests there. The diff and the commit stay the whole repo's.
  - Test a two-package repo: the agent's first `pwd`, the test's cwd, and the instructions it
    gets.
- **Source:** cli.py:404, context.py:977, runner.py:544.

**7. Nothing cleans what a killed command leaves.** Source.
- **Case:** a `wb start` killed by Claude's Bash limit: a big checkout, on `/mnt/c` most of all,
  where the start's own note says git is many times slower. Or a runner killed during its base
  check, or a verify killed (3).
- **What happens:**
  - The start leaves `tasks/<name>/` without a task.json, branch `delegate/<name>`, a registered
    worktree and maybe `rules/<name>`. The name is refused for good ("exists without a
    task.json").
  - The kills leave `$WB_HOME/.verify-*`, `.base-*`, `.check-*`, `.submodule-*`, `wb-index-*` and
    `wb-merge-*`. Each can be a full clone with copied dependencies.
  - `wb gc` removes rules and runtime directories, not these. `_disk_room` then counts the space
    they hold against every new task.
- **Fix:**
  - `wb gc` (and the daily run in accept and discard) removes:
    - scratch directories older than an hour whose creator is gone;
    - task directories without a task.json older than `RULES_GRACE`, with their branch and
      worktree registration.
  - `wb start` writes task.json before the checkout, phase `starting`, so an interrupted start is
    a task `wb discard` can drop.
  - Test a `wb start` killed during `git worktree add`, then the same name again after gc.
- **Source:** cli.py:629, cli.py:1798, verify.py:89, gitops.py:245, gitops.py:390.

## Medium: shared machines and servers

**8. On a shared machine, one user's task unloads another's model mid-round.** Source.
- **Case:** a lab box or a family desktop: one system Ollama, two accounts each using wb. Or one
  account with Open WebUI or an editor's autocomplete on the same Ollama.
- **Why:**
  - The GPU lease is `$WB_HOME/gpu.lock`, one per account.
  - `free_gpu` unloads "every model Ollama has loaded, ours or another tool's" but the
    `keep_loaded` ones.
  - So each account's runner evicts the other's model before every round. Both then pay a reload
    on every request, and the silence and think budgets run during it.
- **Fix:**
  - Before unloading a model wb didn't load, look for a request in flight on it (`/api/ps`'s
    `expires_at` moving, or the server's own queue). Say so, and queue, rather than unload.
  - Document `gpu_gb` and `keep_loaded` for shared servers in `wb doctor`'s output when
    `/api/ps` lists models no task holds.
  - A machine-wide lease (a lock under `/run/user` won't do for two users; a group-writable
    `/var/lib/workbench`, opt-in) is the full answer.
- **Source:** models.py:1022.

**9. Ollama's settings are read from systemd, launchd or wb's own environment, not from the
Ollama that runs.** Source.
- **Case:** Ollama in Docker (the `ollama/ollama` image), an
  `ollama serve` started by hand in another terminal, or Ollama for Windows reached from WSL.
- **Why:**
  - `hardware.ollama_env` reads `systemctl show ollama`, `launchctl getenv`, then wb's own
    environment.
  - None of them is that server's. So `OLLAMA_NUM_PARALLEL`, `OLLAMA_MAX_LOADED_MODELS`,
    `OLLAMA_CONTEXT_LENGTH` and the visible-device masks are read as unset.
- **The result:**
  - Sharing is refused that would fit, or allowed where Ollama will swap.
  - The served-window advice is wrong.
- **Fix:**
  - For an Ollama process of this user, read `/proc/<pid>/environ`, the way `sweep` reads it.
  - For a container, `docker inspect`'s `Config.Env` when `docker` answers.
  - Otherwise say "unknown" in `wb doctor` and `wb scout`, and point at models.json's `vram_gb`
    and `gpu_gb`.
  - The windows OpenCode is told are already checked against `/api/show` (#248). The sharing
    knobs aren't.
- **Source:** hardware.py:284, models.py:1208.

**10. An authenticated proxy is dropped from the setup's and the tests' environment without a
word.** Source.
- **Case:** a corporate network where `https_proxy=http://user:pass@proxy:3128`.
- **Why:** `runner.USERINFO` drops every variable whose value holds `user:pass@`, on purpose. So
  `--setup 'npm ci'`, `pip install` or `cargo fetch` in the lane fail with network errors that
  read as the project's.
- **Fix:**
  - At `wb start`, when a `*_proxy` variable was dropped, one line: "the agent's environment
    leaves out https_proxy (it holds a password): commands.json env passes it through".
  - The same in `lane_refused`'s style of report when a setup fails on a connection error.
- **Source:** runner.py:211, runner.py:220.

## Low and worth checking

**11. A mixed NVIDIA rig can budget the wrong card.** Source; hardware not checked.
- `hardware.visible` maps `CUDA_VISIBLE_DEVICES=0` to nvidia-smi's first card, in PCI order.
- CUDA's own default order is fastest first (`CUDA_DEVICE_ORDER=FASTEST_FIRST`), so on a 3090
  beside a 1060 in a lower slot, Ollama's card 0 and wb's differ.
- Fix: honour `CUDA_DEVICE_ORDER` from Ollama's environment. Without it, map numeric ids by
  compute capability and memory as CUDA does, or say "set CUDA_DEVICE_ORDER=PCI_BUS_ID or use
  UUIDs".
- **Source:** hardware.py:175.

**12. Git LFS is untested.** Not checked (no git-lfs here).
- `_disk_room` counts LFS pointer sizes, not the files they stand for.
- The `--shared` scratch clones of the base check and verify run the LFS smudge filter against
  a local-path remote, which needs a git-lfs with standalone transfer, or leaves pointer files
  where the tests expect data.
- Fix: one suite test under `git lfs install --local` when git-lfs is present, and CI installs
  it.
- **Source:** cli.py:547, verify.py:93.

**13. Nested submodules stay empty.** Source.
- `fill_submodules` reads only the top `.gitmodules` and fills each submodule's own tree, so a
  submodule's submodules are empty directories.
- Fix: recurse with the same scratch-index fill, and add their paths to the start's note when
  they can't be filled.
- **Source:** gitops.py:216.

**14. `OPENCODE_DISABLE_CLAUDE_CODE` or `OPENCODE_DISABLE_CLAUDE_CODE_SKILLS` in the user's
environment silently drops the project's `.claude/skills` for the agent.** Source (OpenCode's
flags).
- OpenCode 2.3 reads both. They reach the agent's environment like any non-secret.
- Fix: `wb doctor` says so when one is set and a repo has `.claude/skills`. Or the agent's
  environment unsets them, since wb hands the instructions itself.

**15. A colocated Jujutsu repo can't use `--merge`.** Source.
- jj keeps Git's HEAD detached, so `start_branch` records none and `wb accept --merge` refuses
  "a detached HEAD".
- Fix: say "merge with jj" in that refusal when `.jj/` is beside `.git`.
- **Source:** gitops.py:561.

## What would earn 9/10

1. Read OpenCode's config and skills where OpenCode 2.3 reads them (1, 2), with real-OpenCode
   tests for `OPENCODE_CONFIG_DIR`, the system file and `~/.agents/skills`.
2. Nothing Claude runs may outlast its Bash call or leave a scratch behind (3, 7):
   - verify and shot detached or backgrounded;
   - a SIGTERM handler in the CLI;
   - gc for scratches and half-made starts.
3. `wb events` blind to scratch clones (4).
4. Signing checked at start and explained at accept (5).
5. Monorepo packages as a first-class `--repo` (6).

Past 9: shared machines and other Ollama installs (8, 9), the proxy line (10), then the low
findings. A release gate worth adding to `docs/release-checklist.md`: one real run on a kernel
with an 8,192-watch limit, and one with `OPENCODE_CONFIG_DIR` set.

## Test runs

- `python3 -m unittest` at ad43797: 591 tests, OK (1 skipped), 474 s.
- **Finding 1:** `OPENCODE_CONFIG_DIR=<dir>` with one provider.
  - `opencode debug config --standalone` listed `<dir>/opencode.json`.
  - `wb models` said "no local models … in ~/.config/opencode/opencode.json".
- **Finding 2:** a scratch `HOME` with a skill in `~/.agents/skills` and one in
  `~/.claude/skills`, and `opencode run --standalone` against `tests/capture_llm.py`. Both skill
  names were in the requests.
- **Finding 4:** `Watcher(home, recursive=True, skip=cli.EVENT_SKIP)`. 300 package directories
  made under `.verify-abc/repo/node_modules` took it from 2 to 605 watched directories.
- **Finding 5:** an ed25519 key with a passphrase, `gpg.format=ssh`, `commit.gpgsign=true`,
  `setsid git commit` with no agent and stdin from /dev/null. It exited 128: "incorrect
  passphrase supplied … failed to write commit object".
- **Finding 6:** a repo with only `packages/app/CLAUDE.md`, and `wb start --repo .` from
  `packages/app`. Refused: "has no CLAUDE.md (or .claude/CLAUDE.md) at HEAD".
