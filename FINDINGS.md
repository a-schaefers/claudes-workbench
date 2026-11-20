# Workbench 1.2 production-readiness audit

2026-11-19, at main 084d896 (version 1.2, the release merge; the code is the same as a5b7263).
No code was changed. This is a fresh audit. Findings 1–31 of the 1.0 audit (#189–#217) are fixed
and not repeated here. Neither is what is in flight or by design:
- #236: the macOS nightly. That covers `models._started` and `runner.sweep` without /proc, and
  the tests' macOS assumptions.
- The lane writes /tmp whole (#217, `context.home_gap`).
- macOS has no Landlock.
- The macos and wsl nightly jobs aren't required checks.

The focus is what stops some users, on some hardware, platforms or working styles, from having
it just work. Security is out of scope. Each finding says how it was confirmed:
- **reproduced:** run here against real OpenCode, git or the real functions.
- **source:** the code path read and shown to behave as described. Not run.
- **source, hardware not checked:** depends on hardware or drivers that weren't available here.

## Verdict: 8 out of 10

The core loop is sound and has clearly been through several audits:
- the store, the runner's guards and clocks, the GPU lease, accept and merge, reaping, the lane.
- Most edges probed this time were already handled, many with the incident cited in a comment.

What keeps it from 9:
- **The model can be given less than it was promised.** Nothing checks, when a task runs, that
  the server still serves the window OpenCode was told. Prompts are then cut silently (1).
- **Projects that need setting up pay for it several times.** Every scratch checkout runs the
  setup and copies again, from nothing (2).
- **Claude's tokens can leak into noise.** Lists of changed files are unbounded, and build
  output that isn't ignored lands in reports and commits (3).
- **Compatibility beyond Ollama and the newest distros:** strict-name servers (4), older default
  Pythons (7), some GPUs (11, 12), and platforms and Claude Code surfaces no gate runs (release
  gates).

**What was verified:**
- Arch Linux, kernel 7.2, Python 3.14, git, OpenCode 2.3.0 (real, on PATH).
- The full suite: see *Test run* at the end.
- One finding was reproduced against real OpenCode 2.3 (6), through the suite's own
  `RealOpenCode.run_calls` harness and its recording model server.
- Not run: native macOS, WSL2, a GPU other than this RX 9070, a server other than Ollama.

## P1: needed for 9

**1. Nothing checks, when a task runs, that the server serves the window OpenCode was told.**
Source.
- `limit.context` in OpenCode's entry is what OpenCode, the handover point (`fresh_at`), the
  compaction buffer and the slice all plan against. Ollama serves its own `num_ctx`.
- Only `wb doctor` (`cli.py:3094`), `wb scout` and `wb model add` compare the two
  (`models.ollama_served_context`). `wb start`, the runner's load (`runner._load`,
  `models.make_resident`) and every reload before a round never do.
- The two drift apart after setup:
  - Ollama restarted without the service's override (run by hand, or the app on macOS).
  - `OLLAMA_CONTEXT_LENGTH` changed.
  - An Ollama upgrade changed its default.
  - A remote server (`wb remote`) whose environment wb can't read.
- Then Ollama trims the prompt to fit, silently. The agent loses the protocol, the spec or its
  own earlier work, OpenCode never compacts (it thinks there is room), and the round wanders
  until a guard stops it. Nothing in the report says why.
- **Fix:** after each load, read `/api/ps`'s `context_length` for the model (or `server_facts`
  for llama-server and vLLM). If it is below `limit.context`, fail the task before round 1 with
  the fix, or (better) plan the task to the served window and warn once.

**2. Every scratch checkout sets the project up again, from nothing.** Source.
- #201 gave tasks `--setup`, `--copy` and `--sibling`. `runner._prepare` runs them in the task's
  worktree, once.
- The base check (`runner.base_check` → `verify.on_base` → `verify._run`, `verify.py:83`/`86`)
  makes a fresh `git clone --shared` and runs `runner.prepare` in it again: the copies and the
  whole setup command (up to `SETUP_SECS`, 30 min).
- `wb verify` does it twice more (base and candidate), and `wb shot` too.
- The same project pays its setup three to five times:
  - A Rust or C++ project whose setup is a first build compiles from scratch each time, because
    `target/` and `build/` aren't shared.
  - A Node project copies `node_modules` each time. On ext4 (Ubuntu's and Debian's default),
    `cp --reflink=auto` is a full copy (`runner.py:852`): gigabytes per checkout.
- The base check's own 120 s limit covers only the test. The setup before it delays round 1
  with nothing said.
- A repo using git-lfs: the `--shared` clone has none of the LFS objects, so each scratch
  checkout fetches them from the remote again, or fails offline.
- **Fix:**
  - Make each scratch checkout from the prepared worktree (a reflink or hardlink copy of its
    ignored directories, or `git worktree add` beside it with the same `copy` list), and skip
    `setup` there unless asked.
  - Run the base check after `_prepare`, and time the setup separately.
  - Say in the first report how long setup took, and where.

**3. Changed-file lists are unbounded, and unignored build output reaches Claude and the
commit.** Source.
- `gitops.changed` (`gitops.py:173`) is `git status --porcelain --untracked-files=all`, put whole
  into:
  - every round report (`runner.py:1090`),
  - each check-in (`runner.py:391`),
  - the last handover check-in (`runner.py:1556`),
  - and, when the diff is too big, its whole `--stat` (`runner.py:1034`).
- A CMake `build/`, a `coverage/`, `dist/` or `target/` that the project doesn't ignore, which
  the agent creates by running the build, adds one line per file. A few thousand lines of
  report reach Claude's context on every wake. Claude's tokens are the resource the product
  exists to save.
- `wb accept` then commits all of it (`gitops.commit_all`: `add -A`, `gitops.py:296`).
  Nothing warns about binaries or a file count.
- `gitops.mark` also reads up to 1 MB of each untracked file at every step, so a build dir
  slows every guard check.
- **Fix:**
  - Cap each list (the first N paths, then a count per top directory, e.g. `build/ (2,314
    files)`) in reports, check-ins and the stat.
  - Flag a new top-level directory of untracked files as "build output? gitignore it" in the
    report.
  - Have `wb accept` refuse (or ask for `--include`) when the commit adds more than N files or
    any large binary, naming the directory.

## P2: compatibility and robustness

**4. A one-model server that checks names reads as running under any name.** Source.
- `models.status` (`models.py:665`–`682`) accepts a server that lists exactly one model whatever
  `m.name` is ("a one-model server answers to any name"). That's true of llama-server. It isn't
  true of vLLM, SGLang or TGI: they reject a request whose `model` isn't the served name (404,
  "model does not exist").
- With an OpenCode entry whose key or `id` doesn't match vLLM's `--served-model-name`, the router
  picks the model and the round dies at its first request: "opencode exited 1", no text, a
  `failed` notice. Nothing says the name is wrong. The usual vLLM name is a long HF path, so
  this mistake is likely.
- **Fix:** when the one listed id differs from `m.name` and the server isn't llama.cpp
  (`owned_by: llamacpp` or `meta`, as `server_facts` already tells), say so in `status`
  ("serves X; set the entry's id to X"), or probe one 1-token completion with `m.name` once and
  cache the answer.

**5. A task holds the GPU through its tests and while its server is down.** Source.
- The whole of `runner.drive` runs inside `GpuLease` (`runner.py:1376`). That includes:
  - `run_tests` (up to `--test-timeout`, 15 min, every round, `runner.py:1451`),
  - the self-review rounds' tests,
  - and `_wait_up` after a server dies mid-task (up to `START_TIMEOUT`, 20 min).
- On a one-GPU machine every queued task waits through all of it, though the model isn't
  needed.
- The model also stays resident while the tests run. A project whose tests use the GPU (CUDA,
  PyTorch, a game engine, shader tests) runs them in what the model leaves: out-of-memory
  failures that go back to the agent as its bug.
- **Fix:**
  - Release the lease (or downgrade it to a waiting entry) for tests and server waits, and
    re-admit before the next round. The FIFO order keeps the task's place.
  - Add a per-repo or per-task option to unload the model before tests (`commands.json` `repos`
    `gpu_tests: true`).

**6. A command written `VAR=value cmd` is refused.** Reproduced (real OpenCode 2.3).
- OpenCode matches each simple command's raw text, so `FOO=1 ls` matched no allow rule and was
  refused, while `ls` and `env FOO=1 ls` ran.
- It's the usual spelling in CLAUDE.md files, READMEs and test commands: `PYTHONPATH=. pytest`,
  `RUST_BACKTRACE=1 cargo test`, `CI=1 npm test`, `NODE_ENV=test jest`.
- `agent/HANDBOOK.md:27` tells the agent to use `env`. A small model copying the project's own
  command doesn't, gets "Permission denied: shell", and either spends a permission ask (a
  Claude wake) or gives up on running the tests.
- **Fix:** allow `<NAME>=<value> ` before each allowed command. A rule per command such as
  `[A-Za-z_]*=* pytest *` is too broad with OpenCode's `*`. Either generate `*=* <cmd> *` only
  for the allowed commands and accept the small widening, or rewrite the agent's view: say in
  the refusal text itself (the ask tool's description) "write it as `env NAME=value cmd`".
  Add a RealOpenCode test either way.

**7. A machine whose `python3` is older than 3.11 can't run wb, even with a newer Python
installed.** Source.
- `bin/wb` (`bin/wb:14`) runs under `#!/usr/bin/env python3` and exits 78 below 3.11.
- `python3` is 3.10 on Ubuntu 22.04 LTS and 3.9 on RHEL/Rocky/Alma 9, which are still widely
  used. Those users install `python3.11` or `python3.12` beside it (deadsnakes, `dnf install
  python3.11`), and wb still refuses.
- The mod's `wb events` fails the same way, so the pane never wakes. That is the "the mod does
  nothing" case in the troubleshooting.
- **Fix:** before refusing, re-exec the first of `python3.14` … `python3.11` found on PATH (as the
  UTF-8 re-exec already does), and name it in `wb doctor`. Do the same in `bin/wb-ask-mcp`, though
  the runner passes its own interpreter there already.

**8. A dependency more than one level up isn't linked.** Source.
- `--sibling` links `tasks/<name>/<sibling>` beside the worktree (`runner.py:875`), so
  `../<sibling>` resolves. The name must be one path component (`cli._worktree_setup`).
- These break, as they did before #201, and nothing can be configured to fix them:
  - a Cargo `path = "../../shared/lib"`,
  - a pnpm or npm workspace whose root is two levels up (the repo is one package of a bigger
    tree),
  - a Go `replace ../../x`.
- **Fix:** accept a relative path (`--sibling ../../shared`), and mirror the needed levels of
  directories above the worktree in the task directory (or move the worktree down to
  `tasks/<name>/w/<repo-name>`, so one level up is a directory the task owns).

**9. A failed session export reads as an empty session.** Source.
- `transcript.export` returns `{}` on any failure (a timeout at 30 s, a busy store, a crash).
- `runner.context_state` then reports no usage and no compactions, so `fresh_reason` says "carry
  on", and a session that was full or compacted continues into the next round.
  `measure_speed` records nothing.
- With several tasks at once, OpenCode's shared session store is the likeliest to be busy.
- **Fix:** on an export failure, fall back to the live counters the runner already has (the
  last `step` event's tokens, status `tok_in`), or start a fresh session when the last round's
  live usage passed the handover point. Log the failure as a `load` event.

**10. A sparse-checkout monorepo gets a full checkout per task.** Source.
- `gitops.create_worktree` runs `git worktree add` with no sparse settings, so the task's
  worktree is the whole tree, whatever cone the user works in.
- On a large monorepo that is minutes and many gigabytes per task, plus the base check's
  clone (2).
- **Fix:** when the checkout has `core.sparseCheckout` on, add the worktree with
  `--no-checkout`, copy the cone (`git sparse-checkout list`), then check out.

## P3: hardware and polish

**11. GPU power is read from `power1_average` only.** Source, hardware not checked.
- `hardware.gpu_power_w` (`hardware.py:212`) reads amdgpu's hwmon `power1_average`.
- Some amdgpu generations expose only `power1_input`. Intel's i915 and xe expose an
  `energy1_input` counter, not a power reading.
- On those cards `gpu_wh` stays 0 and the savings say the energy wasn't measured.
- **Fix:** read `power1_input` when `power1_average` is missing, and integrate `energy1_input`
  deltas where that is all there is.

**12. GPUs that publish no VRAM size get no budget.** Source, hardware not checked.
- Off NVIDIA and amdgpu, `hardware.gpus` reads `mem_info_vram_total`, which Intel Arc (i915, xe)
  doesn't publish. Neither do others outside those two drivers.
- VRAM then reads unknown, `budget_devices` finds none, and tasks never share. `_room_beside`
  then unloads the user's other Ollama models every round, as before #215.
- `gpu_gb` in models.json fixes it by hand, but nothing points there.
- **Fix:** when a GPU is found with unknown VRAM and Ollama is in use, have `wb doctor` and
  `wb scout` say "set gpu_gb in models.json". Read xe's `tile0/vram0` size where it exists.

**13. The README's floor isn't the tested floor.**
- "OpenCode 2.x" in *What you need*, while `runner.OPENCODE_TESTED` is 2.3 up to 3.0.
  `wb start` warns below 2.3, but the user installed what the README said.
- "a 16K context window at least" is `MIN_WINDOW`, while OpenCode's own prompt is about 10K
  tokens (`cli.py`'s `wb model add` message). At 16K a task hands over almost at once
  (`_fixed_line`).
- **Fix:** say 2.3 or newer, and 32K recommended (16K minimum).

**14. `wb setup`'s PATH advice stops at "add it".** `wb setup` prints `~/.local/bin isn't on
PATH: add it`. On macOS (zsh, no `~/.local/bin` by default), and in a Claude Code started from
the Dock or an IDE, PATH comes from places users don't know. Print the line to add for the
user's shell (`$SHELL`), and the note that Claude Code must be restarted.

## Release gates for 9/10

What the suite and CI don't run, each needing one real task end to end before calling a
release complete:
1. **Native macOS on Apple Silicon:** the nightly green (#236, in flight), plus one real task
   with Ollama.app and one with LM Studio (MLX).
2. **WSL2 with Ollama on the Windows host,** in both NAT and mirrored networking. This is the
   most common WSL setup for AMD and Intel GPUs, which WSL hides from Linux:
   - In NAT mode the Windows host is a remote server (`local: true`), its environment
     unreadable, so finding 1 applies in full.
   - In mirrored mode it is "this machine", and `ollama_env()` reads WSL's systemd instead of
     Windows'.
3. **A strict-name OpenAI server:** one task each on vLLM, llama-server and LM Studio (finding
   4 and `server_facts`).
4. **Claude Code surfaces other than the terminal:** the VS Code and JetBrains extensions and
   the desktop app. Does the mod's pane render, and do wakes arrive? If not, the skill should
   fall back to `wb wait` and say so.
5. **Ubuntu 22.04 / Debian 12 / RHEL 9 defaults:** dash as `/bin/sh`, the system Python
   (finding 7), Landlock's state in each stock kernel (`wb doctor` reports it), ext4 (finding
   2).
6. **One NVIDIA card, one AMD APU or Strix Halo, one Intel Arc:** `wb doctor`'s budget line and
   one shared run each (findings 11, 12).

## Test run

`python3 -m unittest` at a5b7263 (the same code as 084d896): 508 tests, `OK`, none skipped, 420 s,
real OpenCode 2.3.0 on PATH and Landlock on. The suite is green; the findings above are what it
doesn't exercise. Finding 6's probe used `RealOpenCode.run_calls` with five shell calls: `FOO=1 ls`
refused, `ls` and `env FOO=1 ls` ran, `just` and `pipenv` refused (expected: not on the list).
