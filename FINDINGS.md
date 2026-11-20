# Workbench 1.4 production-readiness audit

2026-11-20. Two audits of 1.4, merged, with duplicates folded together. No code was changed.
- **Claude's audit**, at origin/main d80af4f: v1.4 plus #318, which changes only `wb update`.
  Arch Linux, kernel 7.2 (Landlock ABI 10), Python 3.14, OpenCode 2.3, Ollama 0.40.16.
- **Codex's audit**, at the v1.4 tag 594c0bc, in an isolated checkout. Linux, Python 3.14.9,
  Git 2.56.0 and a Git 2.34.1 built for the merge case, OpenCode 2.3.0. Provider, context and
  GPU handoffs used HTTP fixtures. Its full report is
  `~/Downloads/workbench-v1.4-production-readiness-audit.pdf`.

The two codes differ only in `wb update`, so every finding applies to both. The 1.0 audit
(#189–#217) and the 1.2 audit (#241–#274) are fixed or closed, and none of their findings is
repeated here. Left out as decided:
- #275: by-hand runs on every platform (closed, not planned).
- macOS has no Landlock.
- What the lane withholds on purpose to keep credentials safe.

Security is out of scope, except where a lane rule breaks a project's own build or tests.

How each finding was confirmed, and by whom:
- **Reproduced (Codex):** Codex ran it and reports the result. Not re-run for this report.
- **Reproduced (Claude):** run here, against the real functions, the real lane or real commands.
- **Source:** the code path read and shown to behave as described. Not run.
- **Source, server not checked:** depends on a model server or tool that isn't installed here.

Source lines are at 594c0bc.

## Verdict: 7.5 out of 10

The two audits scored it 7 (Codex) and 8.5 (Claude). They looked at different things:
- Codex reproduced failures in accept, merge and verify, the code that decides what lands in
  the user's repo.
- Claude found failures at the hardware, server and workflow edges.

Together they put it at 7.5. The core loop is sound: the store, the runner's guards and clocks,
the GPU queue, reaping, the lane, git pinning and UTF-8 handling. Most edges both audits probed
were already handled. What keeps it from 9:
- **A failed merge can delete a file Git can't restore** (1), on Git before 2.38. Ubuntu 22.04
  ships Git 2.34.1.
- **What is accepted can differ from what was reviewed** (2–4): a deleted draft, a copied input
  committed, a lost execute bit.
- **Review evidence can be wrong** (5–7): a verify kills a resumed agent, runs the wrong
  virtualenv, or calls a syntax error proof.
- **Shared GPUs and non-Ollama servers:**
  - one task unloads another's model (9);
  - GPU tests can't reach the GPU (8);
  - the optimizer writes impossible limits (10);
  - aliases, URLs, keys, LM Studio and down servers are misjudged (11–15).
- **Unattended use:** chat work drops out of the queue (17), and nothing watches the disk (18).

Fixing the blocker and the high findings (1–18), each with a regression test, gets it to 9. The
medium findings take it past that.

## Blocker

**1. A failed merge deletes an ignored local file (Git before 2.38).** Reproduced (Codex, on Git
2.34.1).
- **Case:** main stopped tracking `settings.ini`, and the developer kept a personal copy under
  `.gitignore`. The task modified the formerly tracked file.
- **What happened:**
  - `wb accept --merge` hit a modify/delete conflict, aborted, and said the merge was backed out.
  - The personal copy was gone.
- **Why:** `gitops._overwritten`'s fallback for Git before 2.38 (no `merge-tree --write-tree`)
  checks only the paths the branch adds. The modify/delete path is missed, as #241 found for the
  main path.
- **Fix:**
  - When Git can't list every path the merge may touch, refuse the automatic merge before
    writing anything to the checkout, or preflight the merge in a scratch worktree.
  - Run the fixture on the oldest supported Git and compare the local files byte for byte after
    the refusal.
- **Source:** gitops.py:529, tests/test_workbench.py:1313.

## High: what is accepted is what was reviewed

**2. Accept settles proposals the agent never made, and searches the whole worktree for them.**
Reproduced (Codex); the search cost is Source (Claude).
- **Case:** a `CLAUDE.md.proposed` draft was committed in the repo before the task started. The
  agent added only `answer.txt`, and `wb diff` showed only that.
- **What happened:** `wb accept --merge` also deleted the tracked draft.
  - `runner.proposals` takes every `CLAUDE.md.proposed` and `AGENTS.md.proposed` in the
    worktree, the ones already there included.
  - `wb accept` and `wb next` settle them all (`cli._settle_proposals`).
- **The search's cost (Claude):** `proposals` walks the whole worktree with `rglob`, from
  `review_diff` (twice) and from accept.
  - With `--copy` or `--setup`, that includes node_modules, target/ and .venv: hundreds of
    thousands of entries per review notice.
- **Fix:**
  - Take as proposals only the files the current step created or changed, against its starting
    tree.
  - List them with `git ls-files --others --exclude-standard` (plus the changed tracked ones),
    which skips ignored directories.
  - Show every deletion or replacement in the review.
  - Test default accept, `--take-claude-md` and `wb next` with a draft already tracked.
- **Source:** runner.py:1045, cli.py:733.

**3. An ignored local input enters a commit at a historical base.** Reproduced (Codex).
- **Case:** an ignore rule was added after a tag. The task started at the tag with `--copy
  local-input.dat`.
- **What happened:**
  - `wb start` accepted the copy.
  - `wb accept` committed the local contents on the delegate branch.
- **Why:** the check (#244) reads the current checkout's ignore rules and the old tree's
  tracked files. At the old tree the path isn't ignored.
- **Fix:**
  - Check ignore behaviour in the task's own checkout.
  - Keep copied inputs out of staging unless explicitly included.
  - Test a tag that predates the ignore rule, and inspect the accepted tree with `git show`.
- **Source:** cli.py:433, gitops.py:409.

**4. A script that ran in the task lands without its execute bit (`core.filemode=false`).**
Reproduced (Codex, on Linux).
- **Case:** with `core.filemode=false`, `run-tool` was created at 0755 in the worktree and ran
  there.
- **What happened:**
  - `wb accept --merge` stored it as 100644.
  - A fresh checkout can't run it directly.
- **Who meets it:** the repo distrusts mode bits on its own filesystem, while the task's
  worktree is on a POSIX one. That is the WSL2 case of a repo on `/mnt/c` with a worktree in
  `~/.local/state`.
- **Fix:**
  - Record executable modes from the task's filesystem, or surface the difference before
    accept.
  - Compare reviewed and committed modes, and run the file from a fresh checkout.
  - Check a real `/mnt/c` repo separately.
- **Source:** gitops.py:311, gitops.py:398.

## High: review evidence

**5. A finishing `wb verify` can kill a newly resumed agent.** Reproduced (Codex).
- **Case:** `wb verify` was started and was running its base check when `wb feedback` was sent.
  The new agent started.
- **What happened:**
  - When verify's base command ended, its cleanup (`runner.sweep`, which kills every process
    carrying the task's `WB_TASK`/`WB_HOME`) killed the agent.
  - The task failed with "opencode exited -9".
  - Two overlapping verifies also killed each other's slow checks.
- **Fix:**
  - Give each verify, capture and agent round its own process identity for cleanup.
  - Serialize phase changes against evidence runs.
  - Test verify beside verify, and verify beside feedback, and require every unrelated process
    to survive.
- **Source:** runner.py:263, runner.py:838, cli.py:1503.

**6. Scratch checkouts run the task's own virtualenv.** Reproduced (Codex).
- **Case:** a `.venv` was made with the recommended `--setup`. Since #249 the scratch checkouts
  copy what setup made (`prepared_paths`), so `.venv` came from the task's worktree.
- **What happened:**
  - Activated in a scratch checkout, `sys.prefix` still pointed into the task's worktree.
  - The task's own test passed, its base check failed, and default `wb verify` failed on both
    sides.
  - The same verify with `--setup` passed on both.
  - Editable installs and entry-point scripts are the most exposed.
- **Fix:**
  - Recreate location-bound environments in each checkout (a pyvenv.cfg found among
    `prepared_paths`: run the setup there instead of copying).
  - Test a console script and an editable install, and check which checkout supplies the
    interpreter and the imported code.
- **Source:** cli.py:425, runner.py:928, verify.py:97.

**7. A syntax error on the base counts as a proven fix.** Reproduced (Codex).
- **Case:** on the base, `calc.py` didn't import. Its SyntaxError traceback printed the source
  line `assert 2 = 2`. The candidate ran normally.
- **What happened:** `wb verify` reported "proven".
  - It matches the word `assert` before it checks for setup or collection errors.
  - No base test body ran.
- **Fix:**
  - Require evidence of a failing test assertion.
  - Let parse, import and collection failures outrank assertion-like source text.
  - The same case should report "inconclusive", with both raw outputs kept.
- **Source:** verify.py:32, verify.py:129.

## High: the GPU and the models

**8. Tests that need the GPU can't open it in the lane, and `--unload-for-tests` doesn't change
that.** Reproduced (Claude: Landlock ABI 10, an RX 9070).
- **Why:** the lane's device allowlist is `context.DEV_NODES` (null, zero, full, random,
  urandom, tty, ptmx, shm). /dev is in `ONLY_KEPT`, so every other node is out of reach:
  - /dev/dri/renderD* (Mesa, Vulkan, OpenGL);
  - /dev/kfd (ROCm);
  - /dev/nvidia0, nvidiactl and nvidia-uvm (CUDA);
  - /dev/dxg (WSL2).
- **What happened:** through `lane.py` with a read lane like the agent's, `os.open("/dev/kfd",
  O_RDWR)` and `/dev/dri/renderD128` both fail with "Permission denied".
- **Who meets it:** SKILL.md sends these projects to `--unload-for-tests`, which frees the GPU's
  memory, but the device is still unreachable:
  - PyTorch's `torch.cuda.is_available()` turns False, and the tests run on the CPU or skip.
  - CUDA says "no CUDA-capable device", ROCm `hipErrorNoDevice`, Vulkan "no physical device".
  - None of these says "permission", so the agent spends auto-fix rounds on what it reads as
    its own bug.
  - `wb allow --path /dev/dri` works, but only a code comment says so.
- **Fix:**
  - Have `--unload-for-tests` (or a new `--gpu`) add the GPU's nodes to the task's `paths`: the
    ones `hardware.gpus` found, plus nvidiactl and nvidia-uvm, or /dev/kfd. Say so in the skill.
  - For /dev/kvm, /dev/fuse and /dev/pts: when a test's tail names a `/dev/` path with
    "Permission denied", add a report line naming the `wb allow --path`.

**9. One task can unload the next task's model.** Reproduced (Codex, with real locks and an
Ollama fixture).
- **Case:** task A was paused just after `--unload-for-tests` released its lease. Task B took
  the GPU and confirmed the model was loaded. A then resumed.
- **What happened:**
  - A sent `keep_alive: 0` for the same model.
  - B still held the lease, but nothing was resident.
- **Why:** `GpuLease.step_aside` decides to unload from the holders it saw before it let go.
- **Fix:**
  - Unload and admit under the same pool transition, rechecking live owners.
  - Decide whether GPU tests need the GPU exclusively for the whole test run.
  - A same-model task coming in must keep the model resident across the handoff.
- **Source:** models.py:1265, runner.py:1556.

**10. Optimizing for a smaller card writes an impossible context budget.** Reproduced (Codex).
- **Case:** a 16,384-token fit was applied to an entry with a 131,072 context, 32,768 output and
  `max_tokens` 65,536.
- **What happened:**
  - wb reported success.
  - It wrote an input limit of −16,384 and kept both larger output caps.
  - That is exactly the downsizing the optimizer is for.
- **Fix:**
  - Recompute input, output and request caps together when the window shrinks.
  - Validate the entry before writing the variant.
  - Round-trip it through OpenCode, with a request inside its limits.
- **Source:** optimize.py:249, runner.py:93.

**11. Retiring one alias deletes the weights the default alias uses.** Reproduced (Codex).
- **Case:** two OpenCode aliases pointed at one Ollama model. One was made the default, and the
  other was retired with `--delete`.
- **What happened:**
  - wb deleted the shared model.
  - The default went from available to "not pulled".
- **Why:** the default's protection compares only the entry names.
- **Fix:**
  - Before deleting weights, find every remaining entry that resolves to that server and model.
  - Refuse, or name them.
- **Source:** cli.py:2559, cli.py:2572.

**12. An explicit `--url` is ignored for an existing provider name.** Reproduced (Codex).
- **Case:** a model was registered with an existing provider's name and a `--url` on another
  port, where nothing listened.
- **What happened:**
  - wb reported success.
  - The model kept the old endpoint.
  - Two servers with the same model name can then do plausible work on the wrong machine.
- **Fix:** compare the requested endpoint with the provider's before writing. Require a new
  provider id, or an explicit endpoint change.
- **Source:** models.py:1527, cli.py:2456.

**13. LM Studio's on-demand loading gets past the window check, and the first round fails.**
Source, server not checked (Claude).
- **Why:**
  - With just-in-time loading on (LM Studio's default), `/v1/models` lists every downloaded
    model. `models.status` reads that as "running", so `make_resident` loads nothing.
  - `served_window` gets no context (no `loaded_instances` yet), so `short_window` (#248)
    passes.
- **What happens:**
  - OpenCode's first request loads the model with LM Studio's default settings, often a 4K
    context.
  - LM Studio refuses longer prompts ("tokens to keep … greater than the context length").
  - The task fails at round 1 with "opencode exited 1".
- **Fix:**
  - For an LM Studio server, load the model before the round with OpenCode's window: `POST
    /api/v1/models/load` with `context_length`, or `lms load <key> --context-length N`.
  - At least, refuse a model that isn't loaded, naming the `lms load` command.

**14. A key kept where OpenCode keeps keys (`opencode auth login`) is invisible to wb.** Source,
server not checked (Claude).
- **Why:**
  - OpenCode's docs set a custom provider's key with `opencode auth login` → "Other", stored in
    `~/.local/share/opencode/auth.json`, with no `options.apiKey` in the config.
  - The lane keeps auth.json readable on purpose, so the agent's OpenCode works.
  - wb's probes read only `options.apiKey` (`discover`, `_KEYS`).
- **What happens:**
  - The probe gets a 401, and `status()` says "it wants an API key".
  - The router passes the model over, and `wb start` refuses it, though OpenCode would run it.
- **Who meets it:** a LiteLLM proxy, `vllm serve --api-key`, llama-server `--api-key`, LM Studio
  with auth on.
- **Fix:**
  - When `options.apiKey` is empty, read the provider's `key` from auth.json
    (`$XDG_DATA_HOME/opencode/auth.json`). Never print it.
  - `wb doctor` says where each key came from.

**15. The engine probe re-probes a server that is down, and misjudges an Ollama behind a key.**
Reproduced (Claude: the timings); reproduced (Codex: the keyed Ollama).
- **Why:** `models._engine` probes `/api/version` (1.5 s timeout) on each `discover()`, and
  caches only a server that answered. `discover()` runs many times per command and per round:
  `admits`, `_room_beside`, `_others_resident`, `free_gpu`, `_contended` (in every `status()`),
  `resolve` and the router.
- **Measured (Claude):** one provider at an address that doesn't answer (marked `local: true`,
  as `wb remote` writes it), next to a local server.

  | | the box off | without it |
  |---|---|---|
  | one `discover()` | 1.5 s | |
  | `wb models` | 12.1 s | 0.1 s |
  | `wb doctor` | 4.8 s | |
  | `free_gpu` (each round's load) | 3.0 s | |
  | `admits` | 1.5 s | |

- **Who meets it:** a `wb remote` user away from home, a LAN box asleep, a Tailscale peer
  offline. `add_remote` writes no `engine`, so nothing skips the probe.
- **The keyed Ollama (Codex):** an Ollama that wants a key on `/api/version` is probed before
  its key is in `_KEYS`. It is classed and cached as a generic server, and its Ollama
  management (load, unload, pull, the window check) is then skipped.
- **Fix:**
  - Have `wb remote` and `wb model add` write the engine they found into models.json.
  - Fill `_KEYS` before the first probe.
  - Cache a failed probe for the process, and pass `status()`'s `down` set through to
    `discover`'s callers.
- **Source:** models.py:353, models.py:477.

## High: installation and the user's config

**16. A relative `OPENCODE_CONFIG` vanishes between `wb start` and the runner.** Reproduced
(Codex).
- **Case:** `OPENCODE_CONFIG=my-providers.json`, run from the project directory.
- **What happened:**
  - `wb start` found three models, picked one, made the branch and reported success.
  - The runner starts in the package directory (`spawn_runner`, cwd=PKG_ROOT). It found no
    providers and failed the task with "unknown model".
- **Why:** `store.absolute_env` makes the `WB_*` path variables absolute, but not
  `OPENCODE_CONFIG`.
- **Fix:**
  - Add `OPENCODE_CONFIG` to `store.PATH_VARS`.
  - Test start, feedback and resume from outside the install.
- **Source:** models.py:118, store.py:54, cli.py:121.

## High: unattended and long-running use

**17. Unread chat messages and open questions drop out of the queue.** Reproduced (Codex).
- **Case:** one user message was left unread, then 540 replies of 7,900 characters were
  appended. In another log, 120 large plan cards were appended after an open question.
- **What happened:**
  - The message is still on disk, but `wb chat pending` lost it.
  - The 120 cards pushed the open question out of `find_ask`, so it can no longer be answered.
- **Why:**
  - Both reads stop at 4 MiB (`read_jsonl_tail`).
  - The mod reads only the last 1 MiB, and skips user messages older than six hours.
  - Someone back from a long run sees an empty queue while work waits.
- **Fix:**
  - Keep outstanding work in its own durable index, apart from the display history.
  - Say when an old message expires, and how to recover it.
  - Test delivery after a sleep, a restart and a burst past both limits.
- **Source:** chat.py:246, chat.py:411, mod/hooks/register.tsx:1095, register.tsx:133.

**18. Nothing watches the disk, and a full one wedges every task.** Source (Claude).
- **What fills it:** each task holds a full checkout, the `--copy` paths and the setup's build
  output.
  - On ext4 (Ubuntu's and Debian's default), `cp --reflink=auto` is a full copy: gigabytes of
    node_modules or target/ per task.
  - The base check, verify and shot add scratch checkouts.
  - Only closed tasks give the space back; stuck and failed ones keep their worktrees.
- **No check guards it:**
  - `wb start` doesn't look at free space.
  - `wb doctor` doesn't report the state directory's size.
  - `wb status` doesn't say what each open task holds.
- **What happens when it fills:**
  - `store.write_json` raises mid-round.
  - The runner's crash handler fails on its own writes, and the task stays in a live phase.
  - `reap`'s `set_status_if` then raises, so `wb status`, `wb wait` and the mod's poll end in a
    traceback until space is freed.
- **Fix:**
  - Have `wb start` refuse below a floor (the checkout's size × 3 + 2 GB), naming the open tasks
    and their sizes.
  - Have `wb doctor` print the size and the free space.
  - Have `reap` and the crash handler report "disk full" once, not a traceback.

## Medium

**19. A clean file-to-directory refactor is refused at merge.** Reproduced (Codex, Git 2.56).
- **Case:** a task replaced the tracked file `pkg` with `pkg/init.py`.
- **What happened:**
  - `wb accept --merge` called the new path an untracked obstruction and exited 5.
  - A plain `git merge` of the kept branch then succeeded.
- **Fix:**
  - Tell a tracked parent the merge replaces apart from an untracked obstruction.
  - Test both directions, with real ignored files in the same fixtures.
- **Source:** gitops.py:539.

**20. A no-op screenshot command passes with an old image.** Reproduced (Codex).
- **Case:** a real PNG was already tracked at `.wb-shot.png`.
- **What happened:**
  - `wb shot --capture true` reported both images captured.
  - Both were the old file. The check accepts any PNG present after exit 0 (#268 covered only a
    missing or invalid file).
- **Fix:**
  - Give each capture a fresh, absent output path.
  - Exit 0 with no new file, or with only an old one, should fail the capture.
- **Source:** verify.py:198, verify.py:220.

**21. A symlinked `WB_HOME` breaks the prepared-input check.** Reproduced (Codex).
- **Case:** `WB_HOME` pointed at a symlink to a state directory.
- **What happened:**
  - The setup made `deps/stamp`, and the task's test passed.
  - The base check said the prepared input had changed, though it hadn't.
- **Why:** `runner.prepare` refuses any symlink in either absolute prefix, the configured state
  directory's own link included. The `store.absolute_env` comment says a symlinked home is
  kept, so the two disagree.
- **Fix:**
  - Resolve the trusted roots first, and check the nested path against them.
  - Test a symlinked state volume beside a changed link inside the task.
- **Source:** runner.py:928, store.py:64.

**22. A bench replay loses the hidden test's execute bit.** Reproduced (Codex).
- **Case:** an accepted task's `./test_check.sh` was executable and passed on the reference
  commit.
- **What happened:**
  - Replaying the right answer, the overlay wrote it at 0644.
  - The check exited 126, Permission denied.
- **Fix:**
  - Keep the hidden file's Git mode.
  - Validate the reference through the same overlay the replays are graded with.
- **Source:** bench.py:119, bench.py:220, bench.py:266.

**23. The optimizer can record a window the server never served.** Reproduced (Codex, Ollama
fixture).
- **Case:** Ollama reported `context_length` 16,384 at every load, all in VRAM.
- **What happened:** the optimizer recorded a 65,536 fit.
  - It copies the requested window into its result and checks only placement.
  - `models.served_window` already knows how to read the real window.
- **Fix:**
  - Use the loaded instance's served context in the fit search.
  - Report a clamp, or say the measurement is unverified.
- **Source:** optimize.py:159, models.py:1675.

**24. A one-model strict server's id matches by its last path component.** Reproduced (Codex).
- **Case:** a server listed `studio/coder`.
- **What happened:** `personal/coder` was marked available.
- **Why:** `_serves` compares basenames, a llama-server allowance applied to vLLM-style servers
  that refuse any other name.
- **Fix:**
  - Exact ids for strict servers.
  - Filename matching only where `server_facts` says llama.cpp.
- **Source:** models.py:590, models.py:695.

**25. A first `wb start` in a clean Linux profile can fail before any model request.**
Reproduced (Codex).
- **Case:** a fresh home outside /tmp, OpenCode 2.3.0, a direct `wb start`.
- **What happened:**
  - The task failed opening `~/.local/share/opencode/log/opencode.log`. No request reached the
    fixture.
  - With the OpenCode data, state, config and cache directories made first, the same task
    reached review.
  - `wb setup` followed by `wb try` reached review in another clean profile.
- **Why:** the lane writes only directories that exist.
- **Fix:**
  - Create OpenCode's directories before drawing the lane.
  - Name a missing directory in the failure.
- **Source:** context.py:417, context.py:459, runner.py:514.

**26. The user's own `OPENCODE_CONFIG_CONTENT` is replaced for the agent, and never read by wb.**
Source (Claude).
- **Why:**
  - `runner.rules_env` sets the variable to the rules alone, over the user's value.
  - `models.opencode_config_files` reads only the files and `OPENCODE_CONFIG`.
- **What happens:** providers, instructions or permissions kept there are lost to the agent,
  and a provider defined only there is invisible to discovery.
- **Fix:**
  - Merge the user's JSON under the rules, with `permissions` and `share` last.
  - Layer it in `read_config` after the files.
  - Have `wb doctor` say when it is set.

**27. A bounded review notice still builds the whole patch in memory.** Reproduced (Codex).
- **Case:** a generated text file of 19 MB.
- **What happened:**
  - The review notice was 182 characters.
  - `review_diff`'s traced peak was 55 MiB, plus git's own.
- **Why:** the whole patch is captured and decoded before the size limit picks the stat.
- **Fix:**
  - Stream the diff under a byte limit, or check `--numstat` sizes first.
  - Keep full diffs when asked for.
- **Source:** runner.py:1087, gitops.py:81.

**28. The pane's chat link forgets the port the server runs on.** Reproduced (Codex).
- **Case:** a chat server ran on port 51583 and answered.
- **What happened:**
  - `wb chat url` returned 1337.
  - The Settings tab calls it without a port, so its link is wrong for anyone who chose a free
    port as the CLI advises.
- **Fix:**
  - Record the effective port with the running server, and read it back.
- **Source:** cli.py:2748, cli.py:3646, mod/hooks/register.tsx:1071.

**29. Submodules are empty in the task's worktree.** Source (Claude).
- **Why:** for the agent's safety nothing checks them out: each one's `.git` would be a gitfile
  the agent could repoint. `wb start` prints a note.
- **Who meets it:** C/C++, embedded and game projects, and vendored schema repos. For these the
  base check and every round's tests fail on missing files.
- **Fix:**
  - Fill each submodule from the user's checkout without a `.git`: a reflink copy, or `git -C
    <repo>/<path> archive <the commit the base records> | tar -x`.
  - The gitlink keeps them out of the diff and the commit.
  - Keep the note when the user's checkout isn't at that commit.

**30. The web chat and the runners stop at logout on some systemd setups.** Source (Claude).
- **The chat:** `wb-chat.service` is a user unit, which stops with the user's last session
  unless lingering is on. That happens on a headless box reached by SSH, or a desktop logged
  out overnight. Nothing mentions `loginctl enable-linger`.
- **The runners:** they stay in the login session's scope. With logind's
  `KillUserProcesses=yes`, a logout kills them, and an overnight backlog is lost.
- **Fix:**
  - `wb chat on` checks Linger and prints the line to turn it on.
  - `wb doctor` warns when `KillUserProcesses=yes`.
  - `spawn_runner` uses `systemd-run --user --scope` where a user manager runs.

**31. `wb accept --merge` always makes a merge commit.** Source (Claude).
- **Why:** `gitops.merge` runs `git merge --no-ff`.
- **Who meets it:** repos kept to linear history (a "Require linear history" rule, or a team
  that rebases or squashes). The push is refused, or the merge is undone by hand on every task.
- **Fix:**
  - `--merge=ff|rebase|squash`, or commands.json `repos` `merge`.
  - Keep today's guards.

**32. Worth one real-OpenCode check: global OpenCode settings that reach the agent.** Source,
OpenCode not checked (Claude).
- **Why:** the layer doesn't pin `small_model`, `autoupdate`, or the user's `plugin` list and
  `~/.config/opencode/plugin/` directory.
- **What could happen:**
  - A cloud `small_model`, whose key auth.json holds, could send the task's text off the
    machine.
  - A plugin runs in every round.
  - An auto-update could move OpenCode past `OPENCODE_TESTED` mid-task.
- **Fix:**
  - Record with `tests/capture_llm.py` what OpenCode 2.3's `run` does with each.
  - Pin `small_model` to the task's model, and `autoupdate: false`, if they act.

## What would earn 9/10

The fixes:
- **Accept and merge (1–4, 19):** run the fixtures on Git 2.34.1 and a current Git.
  - Compare the index, tracked files and ignored local files before and after every refused or
    aborted merge.
  - On success, the accepted tree and file modes match the reviewed patch, proposals and
    old-tag tasks included.
- **Evidence under concurrency (5–7, 20–22):**
  - Run verify, feedback and capture together with slow commands.
  - Check the interpreter and editable installs in both scratch checkouts.
  - Require a real base test failure before "proven", and a newly written image before a
    capture passes.
- **Models and providers (8–15, 23–24):**
  - Two tasks sharing a model, and aliases sharing weights.
  - Requests at the generated limits, with the served window checked.
  - The endpoint and the exact model id confirmed.
  - A keyed server, an LM Studio JIT load, a down remote, and the GPU's device nodes for tests.
- **Config and first use (16, 25–26):** a fresh profile with a direct `wb start`, and relative
  config paths.
- **Unattended (17–18, 27–28, 30):**
  - Outstanding chat work through a restart, a sleep and an overnight backlog.
  - A non-default chat port.
  - A disk floor.
  - Peak memory on a large generated change.

Codex also asks for real end-to-end runs on a fresh Linux profile, Apple silicon, and WSL2 with
repos on both its own filesystem and `/mnt/c`. It would add a low-memory GPU, an authenticated
remote server and an overnight disconnect. #275, which asked for the same, was closed as not
planned on 2026-11-20. They are listed here as evidence worth having, not as a release gate.
Finding 4's `/mnt/c` case is the one a fixture can't stand in for.

## Test runs

- **Claude, on d80af4f:** `python3 -m unittest`, 549 tests, OK, 1 skipped (the macOS-only
  `KERN_PROCARGS2` test), 440 s. The real-OpenCode and Landlock tests ran.
- **Codex, on 594c0bc:**
  - 548 tests, 11 skipped. Of the ten skips that apply on Linux, rerun in a fresh home, nine
    passed and one exposed finding 25. It passed once the OpenCode directories existed.
  - The browser test: 56 checks, WebKit and Chromium. The plugin tests: 55.
- No finding above is covered by a failing test. Each is a path the suite doesn't exercise.
