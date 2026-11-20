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

## Verdict: 8 out of 10

Without findings 1–3 this audit would have said 8.5. They came from another reviewer's leads,
and all three reproduced here:
- the GPU queue can deadlock for good (1);
- a merge replaces a user's symlink (2);
- retiring a model deletes the weights of OpenCode's default (3).

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

Beyond 1–3, what is left falls into two kinds:
- Places where wb's model of the world is narrower than OpenCode's or Claude Code's: where
  OpenCode keeps its config and skills (4, 5), and how long Claude's Bash lets a command run (6).
- Costs that grow with the user's setup: watching scratch clones (7), signing (8), monorepos (9)
  and leftovers (10).

Fixing 1–10, each with a regression test, gets it to 9. Findings 11–18 take it past that, for
the shared machines and the less common setups.

## Blocker

**1. Two tasks waiting for the GPU deadlock after the active task finishes, and every later task
queues behind them.** Reproduced (the real `GpuLease` in three processes).
- **Case:** three tasks whose models wb guessed would fit together (`need` before a load is
  weights × `KV_GUESS`).
  - The first is running. Two more are admitted beside it, sharing.
  - Each one's load then finds that Ollama couldn't keep everything on the GPU
    (`_fits_beside` false), which is what a short guess produces.
- **What happened:**
  - Each misfit lease marked itself waiting and called `_take_alone`. w1 was first in the queue,
    w2 second.
  - The active task released at 4 s. At 20 s both were still waiting, and gpu.json held only
    their two waiting entries.
  - Nothing ends it: the runners sit before their round (phase `queued`), where no silence or
    round clock runs.
  - Every task started after them queues behind the waiting entries, so the GPU stays idle until
    `wb stop`.
- **Why:**
  - `load()` hands `_take_alone` a lease that still holds its shared `gpu.lock`.
  - Only the first waiter ever calls `flock`, so the second keeps its shared lock while it
    waits.
  - The first waiter's `LOCK_EX` can then never succeed, and the second waits for the first.
- **Fix:**
  - In `_take_alone`, a lease that isn't first drops its lock (`LOCK_UN`) before waiting, or
    `load()` drops it before calling.
  - Test this case (three leases, two misfits) beside the existing queue tests. It needs no GPU:
    `make_resident` and `_fits_beside` stubbed, as here.
- **Source:** models.py:1388, models.py:1418, models.py:1445.

## High: the user's files and models

**2. `wb accept --merge` replaces an ignored directory symlink in the user's checkout.**
Reproduced (the real `gitops.merge`, Git 2.56).
- **Case:**
  - The user keeps `data` as an ignored symlink to a directory elsewhere (a shared dataset,
    node_modules on another disk, a cache).
  - The task un-ignored `data/` and committed `data/added.txt` there, as #189's task did with
    `settings.ini`.
- **What happened:**
  - `_overwritten` reported nothing in the way, and the merge went through.
  - `data` in the user's checkout was then a plain directory holding only `added.txt`.
  - Their link was gone. Its target survived, but the project no longer reached it.
- **Why:** walking the paths the merge writes, `os.path.isdir(here)` follows the link, so an
  ignored symlink at a parent reads as an ordinary directory. git won't write through a link,
  and removes the ignored one to make the directory.
- **Fix:**
  - Count a parent that is a symlink (`os.path.islink`) as in the way, whatever it points at, and
    refuse with the usual "move it aside".
  - Test an ignored directory link, a file link, and a link to a tracked directory's target.
- **Source:** gitops.py:653.

**3. Retiring a model with `--delete` deletes the weights OpenCode's default model still uses.**
Reproduced (a recording fake Ollama).
- **Case:**
  - Two providers name one Ollama, spelled differently: `ollama` at `http://localhost:11434/v1`
    and `ollama-long` at `http://127.0.0.1:11434/v1`. That is a longer-window alias, or one
    `wb model add` and one written by hand.
  - OpenCode's default is `ollama-long/qwen3:30b`, and the user retires
    `ollama/qwen3:30b --delete`.
- **What happened:**
  - wb said "its weights were deleted from Ollama", and the server got `DELETE /api/delete
    {"model": "qwen3:30b"}`.
  - The default was still `ollama-long/qwen3:30b`, now with no weights: the next task, and the
    user's own OpenCode, fail until the model is pulled again.
  - With both URLs spelled `127.0.0.1`, the same command refused, as #333 intends.
- **Why:**
  - The sharing check compares the providers' `root` strings.
  - `Model.server` already makes `localhost`, `127.0.0.1` and the host's own name one server,
    but this check doesn't use it.
  - The default-model guard compares only the exact id being retired.
- **Fix:**
  - Compare servers with `Model.server`.
  - Check the default and `small_model`'s weights, not only their ids.
  - Ask Ollama which tags share the manifest's digest before deleting.
  - Test both spellings and a default on the other provider.
- **Source:** cli.py:2658.

## High

**4. wb doesn't read OpenCode's config where `OPENCODE_CONFIG_DIR` or a system config puts it.**
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

**5. Personal skills in `~/.agents/skills` reach every agent.** Reproduced.
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

**6. `wb verify` and `wb shot` outrun Claude's Bash, and a kill leaves their scratch clones and
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

**7. Every `wb events` watches each scratch clone, directory by directory, and wakes its mod on
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

**8. `wb accept` fails at the very end for a user who signs commits with a passphrase.**
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

**9. A monorepo package with its own CLAUDE.md is refused, and the agent works from the root.**
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

**10. Nothing cleans what a killed command leaves.** Source.
- **Case:** a `wb start` killed by Claude's Bash limit: a big checkout, on `/mnt/c` most of all,
  where the start's own note says git is many times slower. Or a runner killed during its base
  check, or a verify killed (6).
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

**11. On a shared machine, one user's task unloads another's model mid-round.** Source.
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

**12. Ollama's settings are read from systemd, launchd or wb's own environment, not from the
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

**13. An authenticated proxy is dropped from the setup's and the tests' environment without a
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

**14. A mixed NVIDIA rig can budget the wrong card.** Source; hardware not checked.
- `hardware.visible` maps `CUDA_VISIBLE_DEVICES=0` to nvidia-smi's first card, in PCI order.
- CUDA's own default order is fastest first (`CUDA_DEVICE_ORDER=FASTEST_FIRST`), so on a 3090
  beside a 1060 in a lower slot, Ollama's card 0 and wb's differ.
- Fix: honour `CUDA_DEVICE_ORDER` from Ollama's environment. Without it, map numeric ids by
  compute capability and memory as CUDA does, or say "set CUDA_DEVICE_ORDER=PCI_BUS_ID or use
  UUIDs".
- **Source:** hardware.py:175.

**15. Git LFS is untested.** Not checked (no git-lfs here).
- `_disk_room` counts LFS pointer sizes, not the files they stand for.
- The `--shared` scratch clones of the base check and verify run the LFS smudge filter against
  a local-path remote, which needs a git-lfs with standalone transfer, or leaves pointer files
  where the tests expect data.
- Fix: one suite test under `git lfs install --local` when git-lfs is present, and CI installs
  it.
- **Source:** cli.py:547, verify.py:93.

**16. Nested submodules stay empty.** Source.
- `fill_submodules` reads only the top `.gitmodules` and fills each submodule's own tree, so a
  submodule's submodules are empty directories.
- Fix: recurse with the same scratch-index fill, and add their paths to the start's note when
  they can't be filled.
- **Source:** gitops.py:216.

**17. `OPENCODE_DISABLE_CLAUDE_CODE` or `OPENCODE_DISABLE_CLAUDE_CODE_SKILLS` in the user's
environment silently drops the project's `.claude/skills` for the agent.** Source (OpenCode's
flags).
- OpenCode 2.3 reads both. They reach the agent's environment like any non-secret.
- Fix: `wb doctor` says so when one is set and a repo has `.claude/skills`. Or the agent's
  environment unsets them, since wb hands the instructions itself.

**18. A colocated Jujutsu repo can't use `--merge`.** Source.
- jj keeps Git's HEAD detached, so `start_branch` records none and `wb accept --merge` refuses
  "a detached HEAD".
- Fix: say "merge with jj" in that refusal when `.jj/` is beside `.git`.
- **Source:** gitops.py:561.

## What would earn 9/10

1. The GPU queue can't deadlock (1), the merge never replaces a symlink (2), and retirement never
   deletes weights another entry uses (3). These go first, in a release of their own.
2. Read OpenCode's config and skills where OpenCode 2.3 reads them (4, 5), with real-OpenCode
   tests for `OPENCODE_CONFIG_DIR`, the system file and `~/.agents/skills`.
3. Nothing Claude runs may outlast its Bash call or leave a scratch behind (6, 10):
   - verify and shot detached or backgrounded;
   - a SIGTERM handler in the CLI;
   - gc for scratches and half-made starts.
4. `wb events` blind to scratch clones (7).
5. Signing checked at start and explained at accept (8).
6. Monorepo packages as a first-class `--repo` (9).

Past 9: shared machines and other Ollama installs (11, 12), the proxy line (13), then the low
findings. A release gate worth adding to `docs/release-checklist.md`: one real run on a kernel
with an 8,192-watch limit, and one with `OPENCODE_CONFIG_DIR` set.

## Test runs

- `python3 -m unittest` at ad43797: 591 tests, OK (1 skipped), 474 s.
- **Finding 1:** three processes with real `GpuLease`s.
  - `WB_FAKE_MODELS=1`, `WB_FAKE_GPU_GB=16`, three 4 GB models in models.json,
    `OLLAMA_MAX_LOADED_MODELS=3`.
  - `make_resident` returned 1 s and `_fits_beside` false in the two loading leases.
  - "active" held for 4 s. w1 and w2 were admitted beside it, and each load said "could not fit …
    waiting for the GPU to itself".
  - Both were still waiting at 20 s, killed by the watchdog.
  - The same three without the misfit, and four leases with sharing, a step-aside and a rejoin,
    each ran in order.
- **Finding 2:** a repo ignoring `data`, with `data` a symlink to a directory holding `keep.txt`,
  and branch `delegate/t1` committing `data/added.txt`.
  - `gitops._overwritten` returned `[]`, and `gitops.merge(r, "t1", into="main")` merged.
  - `data` was then a directory holding `added.txt`.
- **Finding 3:** a fake Ollama on port 18778 logging DELETE, and `WB_OPENCODE_CONFIG` with
  providers `ollama` (`localhost`) and `ollama-long` (`127.0.0.1`), both listing `qwen3:30b`.
  The default was `ollama-long/qwen3:30b`.
  - `wb model retire ollama/qwen3:30b --delete` logged `DELETE /api/delete {"model": "qwen3:30b"}`.
  - With both spelled `127.0.0.1` it refused, and nothing was deleted.
- **Finding 4:** `OPENCODE_CONFIG_DIR=<dir>` with one provider.
  - `opencode debug config --standalone` listed `<dir>/opencode.json`.
  - `wb models` said "no local models … in ~/.config/opencode/opencode.json".
- **Finding 5:** a scratch `HOME` with a skill in `~/.agents/skills` and one in
  `~/.claude/skills`, and `opencode run --standalone` against `tests/capture_llm.py`. Both skill
  names were in the requests.
- **Finding 7:** `Watcher(home, recursive=True, skip=cli.EVENT_SKIP)`. 300 package directories
  made under `.verify-abc/repo/node_modules` took it from 2 to 605 watched directories.
- **Finding 8:** an ed25519 key with a passphrase, `gpg.format=ssh`, `commit.gpgsign=true`,
  `setsid git commit` with no agent and stdin from /dev/null. It exited 128: "incorrect
  passphrase supplied … failed to write commit object".
- **Finding 9:** a repo with only `packages/app/CLAUDE.md`, and `wb start --repo .` from
  `packages/app`. Refused: "has no CLAUDE.md (or .claude/CLAUDE.md) at HEAD".
