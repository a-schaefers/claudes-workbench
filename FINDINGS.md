# Workbench 1.2 production-readiness audit

2026-11-19, at main 084d896 (the published v1.2). No code was changed.

Two independent audits are merged here, duplicates combined:
- **Claude's:** this repo's session, 14 findings and the release gates.
- **Codex's:** its eight-page report, 22 findings,
  `~/Documents/Workbench 1.2 production readiness audit - 084d896.pdf`.

Each entry says where it came from and how it was confirmed:
- **reproduced:** run against real git, OpenCode or the real functions. "Here" means Claude's
  session; otherwise it was Codex's run (fixtures, browsers, an injected interruption, as its
  report says).
- **source:** the code path read and shown to behave as described. Not run.
- **reported:** taken from Codex's report and not re-checked here.

Findings 1–31 of the 1.0 audit (#189–#217) are fixed and not repeated. Two things are left out
on purpose: what is by design (the lane writes /tmp whole, #217; macOS has no Landlock), and
what is already in flight (#236, the macOS nightly).

Each finding has a GitHub issue in milestone **1.2.1** (protect the user's work), **1.3** (recover
correctly, trust the review) or **1.4** (compatibility, polish and release evidence). A fixed one
is struck through and marked **Fixed**, and the docs describe the new behaviour.

## Verdict: 7 out of 10

The ordinary workflow works:
- Both audits ran the full suite green: 508 tests, none skipped. Codex also ran 55 plugin tests
  and 50 browser checks.
- Codex ran two real OpenCode tasks on a Radeon RX 9070 XT. `wb try` passed its hidden check
  both in review and after the merge.
- The core (the store, the runner's guards, the GPU lease, the lane, reaping) holds up, and most
  edges probed were already handled.

It can't be rated above 7 while a supported merge can silently destroy a developer's file (1)
and a connected browser can silently drop a message (5). The way to 9:
- **1.2.1, protect work:** no operation may lose a user's file, provider, runtime directory or
  message (1–6).
- **1.3, recover correctly and trust the review:** every interrupted or refused step ends in one
  true state, and what Claude is shown is what happened (7–17).
- **1.4, platforms:** the compatibility fixes (18–35), and the same delegate → interrupt →
  resume → accept workflow proven on native Apple Silicon, WSL2 and Linux (the release gates).

**What was verified:**
- Claude: Arch Linux, kernel 7.2, Python 3.14, OpenCode 2.3.0 on PATH, Landlock on.
  508 tests, `OK`, none skipped.
- Codex: Linux x86-64, Python 3.14.9, Git 2.56.0, OpenCode 2.3.0, Claude Code 2.1.319, Ollama
  0.40.16 (Vulkan).
- The release nightly: Linux and WSL2 passed. Native macOS failed (#236).
- Not run by either: native macOS or Metal, CUDA, multi-GPU, a WSL2 project on /mnt/c, a server
  other than Ollama, laptop sleep and wake.

## P0: blocks release

### 1.2.1: protect the user's work

**1. `wb accept --merge` can destroy an ignored local file.** #241 · Codex; the first case
reproduced here, the other two reported.
- #189's guard (`gitops._overwritten`, `gitops.py:390`) checks only the paths the branch *adds*,
  by the names git prints. Three histories get past it:
  - **Modify/delete.** `settings.ini` is tracked at the task's base. Main stops tracking it and
    the developer keeps an ignored personal copy. The task edits the tracked version. The merge
    conflicts, `gitops.merge` aborts it, and wb says "failed and was backed out". The personal
    file is gone. Reproduced here with real git: `PERSONAL` before, no file after.
  - **Directory rename.** Main renames a directory that holds an ignored file. With
    `merge.directoryRenames=true`, git moves the task's added file there and replaces the
    personal one on a successful merge.
  - **A non-UTF-8 file name.** The path list is decoded with replacement, so a Latin-1 name
    misses the existence check.
- **Fix:** don't predict git's merge.
  - Merge in a scratch worktree (or `git merge-tree --write-tree`), list every path the result
    writes into the checkout, and refuse when any exists there untracked. Compare raw bytes
    (`-z`, no decoding).
  - Then fast-forward or merge for real.
  - Check that the files that were there still are, byte for byte, after a success and after an
    abort.
  - Keep the task commit on refusal.

## P1: core workflows and advertised platforms

### 1.2.1: protect the user's work

**2. Removing an old remote can delete the provider that replaced it.** #242 · Codex; reported.
- `wb remote studio`'s ownership marker survives after the user repoints the `studio` provider
  at another URL and gives it a custom model.
- `wb remote remove studio` checks only that a marker exists (`models.py:1493`, `:1525`), and
  deletes the replacement provider, its model and its overrides.
- **Fix:** match the current endpoint and provider against what the marker recorded, and refuse
  on a mismatch. The replacement and its overrides stay byte for byte.

**3. Cleanup can delete another profile's runtime directories, and ones wb never made.** #243 ·
Codex; reported; source here.
- `context.drop_stale_runtime_dirs` (`context.py:533`) removes any `wb-run-*` this user owns
  that is older than its cutoff and not in the *current* `$WB_HOME`'s open tasks.
- A second profile's open task, nine days in review, lost its runtime directory. So did an
  unrelated `wb-run-user-notes`. Any task close runs it, once a day (`cli._closed` → `gc`).
- **Fix:** mark each runtime directory as wb's (a marker file naming its `$WB_HOME` and task),
  and remove only the current profile's, for tasks that are closed or gone.

**4. `--copy` can replace a tracked file at a historical base.** #244 · Codex; reported.
- The checkout ignores `generated/`, but the tag passed to `--base` tracks
  `generated/config`.
- `--copy generated` passes `_worktree_setup`'s check (`cli.py:414`: ignored *in the
  checkout*). `runner.prepare` (`runner.py:841`) then puts the developer's local file over the
  tracked one, and it reaches the accepted commit.
- **Fix:** check each copied path against the task's base tree and index too. Refuse a collision
  before touching the worktree, including a directory holding both tracked and ignored files.

**5. The web chat drops a message when the browser's storage is full.** #245 · Codex; reproduced
(Chromium and WebKit).
- With the origin's storage at quota (`QuotaExceededError`) and the server up, Send cleared the
  composer. No pending bubble showed and nothing reached the server.
- The storage helper swallows the error, and the outbox then reads back empty
  (`chat_static/app.js:26`, `:675`).
- **Fix:** clear the composer only after a durable enqueue or the server's acknowledgement. A
  quota error, denied storage, an offline send or a reload keeps the text or shows a failure
  the user can act on.

**6. macOS: process cleanup and lease recovery.** #236 (in flight) · Codex; reported, and the
nightly.
- The release nightly's macOS job: 9 failures, 4 errors, 15 skips.
- `runner.sweep` returns at once without `/proc` (`runner.py:262`), so a child that left the
  group outlives its round.
- `models._started` needs `/proc` (`models.py:1329`), so a recycled pid keeps a dead holder's
  GPU lease alive.
- **Fix:** #236's (`ps` fallbacks). Its acceptance:
  - On native Apple Silicon, a round that started a background server ends and the server is
    gone.
  - A stale holder is simulated, and the next task takes the GPU.
  - Stop, resume and accept run on a real OpenCode/Metal task.
  - Platform-specific skips are reported apart from the rest.

### 1.3: recover correctly

**7. An interrupted accept can contradict git or strand a task.** #246 · Codex; reproduced
(injected interruptions).
- **Merged but not recorded.** Interrupted after the merge and the branch deletion but before
  the phase was written, the retry recorded `merged=false` and said to merge a branch wb had
  already deleted. Git showed the tip already in main.
- **Stranded in review.** A multi-step task whose last step had no new edits, interrupted after
  the worktree was removed, stayed in `review`. Retrying exited 1: the worktree was gone, and
  the earlier step's commit lacked accept's trailer (`_landed`, `cli.py:1698`).
- **Fix:**
  - Before removing anything, write the intended commit and target (status `unrecorded` already
    exists; extend it with `commit`, `merge_into`, `merged_head`).
  - On retry, recover from the commit and its ancestry in the destination, not from the
    branch's name.
  - Interrupt after each step of the close in a test. Every retry must end in one correct
    result, with one results row and a reachable tip.

**8. A refused merge exits 0.** #247 · Codex; reproduced; source here.
- With the checkout dirty, `wb accept --merge` prints `not merged` to stderr, closes the task as
  accepted and exits 0 (`cli.py:1541`).
- A script, the skill or Claude reading the exit code goes on as if main had the change.
- **Fix:** a distinct exit status (e.g. 5, "accepted, not merged") with the commit kept, and the
  results row saying so. Test the exit code, the destination HEAD, the kept branch and the row
  together.

**9. Nothing checks, when a task runs, that the server serves the window OpenCode was told.**
#248 · Claude; source.
- OpenCode, the handover point (`fresh_at`), the compaction buffer and the slice all plan
  against `limit.context`.
- Only `wb doctor` (`cli.py:3094`), `wb scout` and `wb model add` compare it with
  `models.ollama_served_context`. `wb start`, the runner's load (`runner._load` →
  `models.make_resident`) and the reload before each round don't.
- After setup the two drift apart:
  - Ollama restarted without the service's override,
  - `OLLAMA_CONTEXT_LENGTH` changed,
  - an upgrade changed Ollama's default,
  - a remote server whose environment wb can't read.
- Ollama then trims the prompt silently. The agent loses the protocol, the spec or its own work.
  OpenCode never compacts, because it thinks there is room, and the round wanders until a guard
  stops it with no reason given.
- **Fix:** after each load, read `/api/ps`'s `context_length` (or `server_facts` for llama-server
  and vLLM). When it is short, fail before round 1 with the fix, or plan the task to the served
  window and warn once.

**10. Every scratch checkout sets the project up again, from nothing.** #249 · Claude; source.
- `runner._prepare` runs `--setup`/`--copy`/`--sibling` once in the worktree (#201). The base
  check (`verify.on_base` → `verify._run`, `verify.py:83`/`86`), `wb verify` (twice) and `wb
  shot` each clone afresh and run `runner.prepare` again, the setup included (up to
  `SETUP_SECS`, 30 min).
- What that costs:
  - A Rust or C++ project whose setup is a first build compiles three to five times per task.
  - On ext4, Ubuntu's and Debian's default, `cp --reflink=auto` is a full copy
    (`runner.py:852`): `node_modules` is copied each time.
  - The base check's 120 s covers only the test, so round 1 is delayed with nothing said.
  - A git-lfs repo's `--shared` clone has no LFS objects, so each one fetches them again, or
    fails offline.
- **Fix:** make each scratch checkout from the prepared worktree (a reflink or hardlink copy of
  its ignored directories, or a sibling worktree with the same `copy` list), and skip `setup`
  there unless asked. Say in the first report how long setup took.

**11. The bench's replay drops a task's setup and grants.** #250 · Codex; reproduced.
- A task used `--setup` to make an ignored dependency stamp and passed. `wb bench add` then
  rejected the accepted commit itself: its scratch clone had no stamp.
- The task's command grants existed before accept and were empty after: closing deletes the
  rules directory (`cli.py:1605`), where the bench looks later (`bench.py:211`, `:245`).
- **Fix:** keep the replay's inputs (setup, copy, siblings, grants) in task.json or the bench
  case before cleanup, and apply them to the reference, base and replay. A case whose reference
  fails says which dependency it couldn't reproduce.

### 1.3: trust the review

**12. Changed-file lists are unbounded, and unignored build output reaches Claude and the
commit.** #251 · Claude; source.
- `gitops.changed` (`gitops.py:173`) lists every untracked file. It goes whole into:
  - each round report (`runner.py:1090`),
  - each check-in (`runner.py:391`, `:1556`),
  - and the diff's `--stat` when the diff is too big (`runner.py:1034`).
- A `build/`, `coverage/`, `dist/` or `target/` the project doesn't ignore adds a line per file,
  thousands per wake. Claude's tokens are what the product exists to save.
- `wb accept` commits all of it (`gitops.commit_all`, `add -A`, `gitops.py:296`), and nothing
  warns about binaries or a file count.
- `gitops.mark` reads up to 1 MB of each new file at every step, so a build directory also slows
  every guard.
- **Fix:**
  - Cap each list: the first N paths, then a count per directory (`build/ (2,314 files)`).
  - Flag a new top-level directory of untracked files as likely build output.
  - Have `wb accept` refuse more than N new files or any large binary without `--include`.

**13. `wb verify` can credit the current step for a fix an earlier step made.** #252 · Codex;
reproduced.
- Step one added a file. Step two documented it and added a test for it. `wb verify` said
  `proven`, though step one already passed that test.
- It compares against the task's original base (`verify.py:102`, `:136`), which answers a
  whole-task question; at step two's review that is the wrong question.
- **Fix:** name both revisions in the verdict, and compare against the last accepted step when
  reviewing a step. In that fixture the verdict must be "already passes".

**14. A phone can show the wrong project's plan.** #253 · Codex; reproduced.
- When a session asks for a plan card without inline plan text, the fallback picks the newest
  file in the global plans directory (`chat.py:436`, `cli.py:2699`). With two projects planning,
  session A's card showed project B's plan, and the user could send revisions against it.
- **Fix:** bind the card to the session's own plan file. When that isn't known, say which
  project asked and send the user back to its terminal.

**15. The automatic optimizer unloads a model the user marked `keep_loaded`.** #254 · Codex;
reproduced (a recording endpoint); source here.
- `optimize.candidates` (`optimize.py:57`) takes every available Ollama model, and its probe
  unloads, loads and unloads it (`optimize.py:153`). #215's `keep_loaded` protects only from
  eviction.
- So a background measurement can drop the user's autocomplete or chat model.
- **Fix:** leave kept models (and aliases of the same upstream model) out of automatic
  measurement. A recording endpoint must get no unload for them.

**16. The GPU budget can count devices the runtime can't use.** #255 · Codex; reproduced
(fixtures); source here.
- `hardware.visible` (`hardware.py:172`) treats an empty `CUDA_VISIBLE_DEVICES` like an unset
  one, but to CUDA it means no devices.
- With `ROCR_VISIBLE_DEVICES=1,0` and `HIP_VISIBLE_DEVICES=0`, it picked by discovery order
  rather than within ROCR's order, so the 4 GB card instead of the 24 GB one.
- A Vulkan-backed Ollama has its own device mask (`GGML_VK_VISIBLE_DEVICES`), not read at all.
- **Fix:** keep unset apart from empty, apply each filter to the previous one's order, and read
  Vulkan's mask.

**17. Whether a model is ready can disagree with what its server serves.** #256 · Codex and
Claude; reproduced (an HTTP fixture); source here.
- **A keyed Ollama.** It served the model when sent the configured key, but the native `/api`
  probes (`models.py:633`) send none, so it read as unavailable.
- **An empty list.** A server answering `{"data": []}` reads as available and resident
  (`models.py:662`).
- **A strict-name server** (vLLM, SGLang, TGI): any one listed model counts as this one (`"a
  one-model server answers to any name"`, `models.py:665`–`682`). That holds for llama-server.
  The others reject a wrong `model` with 404, so the round dies at its first request as "opencode
  exited 1", with no reason. A vLLM name is usually a long HF path, so a mismatch is likely.
- **Fix:** send the provider's resolved key on every probe. Tell reachable, loadable and served
  apart. When the one listed id isn't `m.name` and the server isn't llama.cpp (`owned_by:
  llamacpp` or `meta`), say "serves X; set the entry's id to X", or make one 1-token request
  and cache the answer.

## P2: narrower compatibility and correctness

### 1.4

**18. A task holds the GPU through its tests and while its server is down.** #257 · Claude;
source.
- All of `runner.drive` runs inside `GpuLease` (`runner.py:1376`). That includes `run_tests` (up
  to `--test-timeout`, every round, `runner.py:1451`) and `_wait_up` after a server dies (up to
  20 min).
- Queued tasks wait through all of it.
- The model stays resident during the tests, so a project whose tests use the GPU (CUDA,
  PyTorch, a game engine) runs them in what is left, and their out-of-memory failures go back to
  the agent as its bug.
- **Fix:** drop to a waiting entry for tests and server waits, and re-admit before the next
  round; FIFO keeps the task's place. Add an option to unload the model before tests.

**19. A command written `VAR=value cmd` is refused.** #258 · Claude; reproduced here (real
OpenCode 2.3).
- OpenCode matches each simple command's raw text, so `FOO=1 ls` is refused while `ls` and
  `env FOO=1 ls` run.
- It's the usual spelling in CLAUDE.md files and READMEs (`PYTHONPATH=. pytest`,
  `RUST_BACKTRACE=1 cargo test`, `CI=1 npm test`).
- `agent/HANDBOOK.md:27` says to use `env`, but a small model copying the project's own command
  doesn't. It then spends a permission ask (a Claude wake) or stops running the tests.
- **Fix:** allow `*=* <cmd> *` for the allowed commands (and accept the slight widening), or say
  "write it as `env NAME=value cmd`" in the refusal path the agent sees. Add a RealOpenCode test.

**20. A machine whose `python3` is older than 3.11 can't run wb, even with a newer Python
installed.** #259 · Claude; source.
- `bin/wb` exits 78 below 3.11 (`bin/wb:14`).
- `python3` is 3.10 on Ubuntu 22.04 LTS and 3.9 on RHEL/Rocky/Alma 9. Their users install
  `python3.11`/`3.12` beside it, and wb still refuses.
- The mod's `wb events` fails the same way, so the pane never wakes.
- **Fix:** before refusing, re-exec the newest `python3.1x` (3.11 or later) on PATH, as the
  UTF-8 re-exec already does, and name it in `wb doctor`.

**21. A copied virtualenv still points at the original.** #260 · Codex; reproduced.
- After `--copy .venv`, the activated copy's `sys.prefix` was the developer's checkout: venvs
  aren't relocatable (absolute paths in `activate` and in console scripts' shebangs,
  `runner.py:865`).
- The task's tests then run the developer's environment, and writes go outside the lane or
  fail.
- **Fix:** detect a venv (`pyvenv.cfg`) in `--copy` and say to recreate it with `--setup` (`uv
  venv && uv sync`, `python -m venv .venv && pip install -r …`) instead.

**22. A dependency more than one level up isn't linked.** #261 · Claude; source.
- `--sibling` takes one path component and links it beside the worktree (`runner.py:875`), so
  only `../<name>` resolves.
- These still break with nothing to configure:
  - Cargo `path = "../../shared/lib"`,
  - a pnpm or npm workspace whose root is two levels up,
  - Go `replace ../../x`.
- **Fix:** accept a relative path, and mirror the levels above the worktree inside the task
  directory (or put the worktree at `tasks/<name>/w/<repo-name>`).

**23. A failed session export reads as an empty session.** #262 · Claude; source.
- `transcript.export` returns `{}` on any failure (a 30 s timeout, a busy store).
- Then `runner.context_state` sees no usage and no compactions, `fresh_reason` says carry on,
  and a full or compacted session continues. `measure_speed` records nothing.
- With several tasks at once, the shared session store is likeliest to be busy.
- **Fix:** fall back to the runner's live counters (the last `step` event's tokens), and log the
  failure as an event.

**24. A sparse-checkout monorepo gets a full checkout per task.** #263 · Claude; source.
- `gitops.create_worktree` adds the worktree with no sparse settings, so a task holds the whole
  tree, plus the base check's clone of it (10).
- **Fix:** when the checkout has `core.sparseCheckout`, add with `--no-checkout`, copy its cone,
  then check out.

**25. A web message retried after a lost reply can stay pending forever.** #264 · Codex;
reproduced (Chromium).
- A message was retried after 205 newer ones. The server answered 200 and logged it once.
- The client kept the outbox item and its pending bubble. It clears entries from the latest
  200-message snapshot and ignores the POST's own acknowledgement (`app.js:487`, `:663`,
  `chat.py:895`).
- **Fix:** clear an item when the POST response names its client id. Test with a lost first
  response and 205 intervening messages.

**26. History limits hide work that still needs an answer.** #265 · Codex; reproduced.
- An open question vanished from the web ask list after 20 newer answered ones, though looking
  it up directly found it open (`chat.py:224`).
- `wb chat pending` returned nothing after 200 newer replies pushed an unacknowledged message
  out (`chat.py:392`). The mod reads its own tail, so this alone doesn't prove a lost
  submission.
- **Fix:** track pending work apart from the display window: every open question and every
  unacknowledged message, with expired ones marked.

**27. Applying an optimized model can stop half done.** #266 · Codex; reproduced (fixture).
- `wb optimize`'s apply created the Ollama variant first, then refused to rewrite a config file
  with comments (`optimize.py:243`, `models.py:221`).
- After the user made the config plain JSON, the same apply refused because the variant already
  existed. It stayed unregistered.
- **Fix:** check the config is writable before creating the variant, and let a retry finish
  registering a variant this operation made, without touching a user's own model of that name.

**28. `wb image` can overwrite an earlier candidate, or relabel an old image.** #267 · Codex;
reproduced (fixture).
- Two prompts with the same first six words and seed wrote the same PNG and sidecar in one
  output directory, so the second replaced the first (`images.py:86`).
- An engine that exited 0 without writing anything was reported successful because the old PNG
  existed, and its sidecar was rewritten for the new prompt (`images.py:103`, `:166`).
- **Fix:** generate into a fresh temporary path, check the output is new, then publish the image
  and its sidecar together. Keep earlier candidates unless replacing is asked for.

**29. A failed screenshot passes for visual evidence.** #268 · Codex; reproduced.
- A capture that wrote a PNG and exited 7, on both base and candidate, printed two
  normal-looking paths. The exits were in the metadata only (`verify.py:204`, `:221`).
- **Fix:** show each side's exit, and treat a nonzero capture as failed even with a PNG. The same
  goes for the pair forwarded to the chat.

**30. A successful accept says the repo has no CLAUDE.md.** #269 · Codex; reproduced; source
here.
- After `wb accept --merge`, `claude_md_status(repo, spec["branch"])` runs after
  `drop_merged_branch` has deleted that branch (`cli.py:1547`, `:1557`, `:2492`). So both real
  trials, `wb try` included, ended "the repo has no CLAUDE.md: write one (wb init)" on a repo
  that has one.
- That is a new user's very first result.
- **Fix:** check the recorded commit (or the destination HEAD after a merge), not the branch name.

**31. Archived tasks drop out of `wb backlog`.** #270 · Codex; reproduced; source here.
- `cmd_backlog` reads `store.all_tasks()` (`cli.py:1814`), which leaves out `archive/`
  (`store.py:298`). After 30 days, a partly archived batch shows wrong totals, and a fully
  archived one shows none.
- **Fix:** read archived tasks for backlog queries, or say in the output where history stops.

## P3: hardware reporting and polish

### 1.4

**32. GPU power is read from `power1_average` only.** #271 · Claude; source, hardware not
checked.
- Some amdgpu generations expose only `power1_input`, and Intel's i915/xe an `energy1_input`
  counter (`hardware.py:212`).
- On those cards `gpu_wh` stays 0 and the savings say the energy wasn't measured.
- **Fix:** read `power1_input` when `power1_average` is missing, and integrate `energy1_input`.

**33. GPUs that publish no VRAM size get no budget.** #272 · Claude; source, hardware not
checked.
- Outside NVIDIA and amdgpu, `hardware.gpus` reads `mem_info_vram_total`, which Intel Arc (i915,
  xe) doesn't publish.
- With no budget, tasks never share, and `_room_beside` unloads the user's other Ollama models
  every round (#215's problem).
- `gpu_gb` fixes it by hand, but nothing points there.
- **Fix:** `wb doctor`/`wb scout` say "set gpu_gb" when a GPU's VRAM is unknown. Read xe's
  `tile0/vram0` size where present.

**34. The README's floors aren't the tested ones.** #273 · Claude; source.
- "OpenCode 2.x", but `runner.OPENCODE_TESTED` starts at 2.3.
- "a 16K context window at least", while OpenCode's own prompt is about 10K tokens, so a task
  hands over almost at once.
- **Fix:** say 2.3 or newer, and 32K recommended (16K minimum).

**35. `wb setup`'s PATH advice stops at "add it".** #274 · Claude; source.
- On macOS, and in a Claude Code started from the Dock or an IDE, PATH comes from places users
  don't know.
- **Fix:** print the line for the user's `$SHELL`, and say to restart Claude Code.

## Release gates for 9/10

#275 (1.4). A green Linux suite and one working GPU are necessary, not sufficient. Each gate is
one real run of install → `wb try` → delegate → answer a question → interrupt → resume → accept
→ merge. Record the OS, filesystem, Python, OpenCode, device ids and measured placement. Then
check independently that the merged content is right and that the next task gets the GPU.
1. **Native macOS on Apple Silicon:** the nightly green (#236), and a real task each with
   Ollama.app and LM Studio (MLX).
2. **WSL2:**
   - A project on `/mnt/c` and one in `~`.
   - Ollama on the Windows host in NAT mode, where it is a remote server and finding 9 applies
     in full.
   - The same in mirrored mode, where it is "this machine" but `ollama_env()` reads WSL's
     systemd, not Windows'.
3. **Servers:** one task each on vLLM, llama-server and LM Studio (findings 17, 9).
4. **Claude Code surfaces:** the VS Code and JetBrains extensions and the desktop app. Does the
   pane render, and do wakes arrive? If not, the skill falls back to `wb wait` and says so.
5. **Stock distributions:** Ubuntu 22.04, Debian 12, RHEL 9: dash as `/bin/sh`, the system
   Python (20), Landlock's state, ext4 (10).
6. **Hardware:** an NVIDIA card (CUDA), an AMD APU or Strix Halo (unified memory), an Intel Arc,
   and a restricted multi-GPU host (findings 16, 32, 33).
7. **Stress:** laptop sleep and wake mid-round, a CPU-only machine, offline and restart, a
   protected model kept loaded, and more queued tasks than the GPU can hold.

Until 1 is fixed, the safe way to work: accept onto the task branch, and look at the incoming
paths in a disposable checkout before merging.
