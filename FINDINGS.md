# Workbench 1.0 production-readiness audit

2026-11-19, at commit e791b71 (version 1.0.0). No code was changed.

Two independent audits merged into one list, duplicates combined: Claude's (this repo's session)
and Codex's (its eight-page report, 23 findings,
`~/Documents/Workbench 1.0 production readiness audit - e791b71.pdf`). Every Codex finding was
checked again here against the code. Each entry says where it came from and how it was confirmed:

- **reproduced:** run here, against real git or the real functions.
- **source:** the code path read here and shown to behave as described. Not run.
- **reported:** taken from the other audit, not re-checked here.

Each finding has a GitHub issue (#189–#217, milestones 1.0.1, 1.1, 1.2). A fixed one is marked
**Fixed** with its issue, and the docs describe the new behaviour.

## Verdict: 7 out of 10

The ordinary Linux workflow works. The core (store, runner, lane, git handling, GPU lease) has
clearly been through several audits, and most edges probed there were already handled. The gap to
9 is in three places:
- **Preserving user work.** Accepting can overwrite a local file, and removing a remote can delete
  a hand-made provider.
- **Trustworthy results.** Merges, verdicts and step transitions can be reported as done when they
  aren't.
- **Recovery and compatibility.** Real projects, other platforms, other hardware and transient
  server failures.

**What was verified:**
- Here: Arch Linux, kernel 7.2, Python 3.14.9. The full suite passed: 447 tests, `OK`, none
  skipped.
- Codex: Linux x86_64, Git 2.56.0, OpenCode 2.3.0, Claude Code 2.1.319. 447/447 Python tests,
  50/50 hook tests and 48/48 WebKit/Chromium browser checks passed.
- Codex reports that with the clone and the user profile both under `/tmp`, 11 of 447 tests fail,
  all GitGuard, Lane or real-OpenCode isolation checks (reported).
- Neither audit ran native macOS, WSL2 or physical GPU inference.

## P1: release blockers

### Losing work and settings

**1. ~~Accepting with `--merge` overwrites an ignored local file.~~** **Fixed (#189).** Codex; reproduced.
- The main checkout holds an ignored `settings.ini` with personal settings. The task un-ignores it
  and commits a default at that path. `wb accept --merge` succeeds and replaces the personal copy.
- Reproduced with real git here: `PERSONAL` became `DEFAULT`, and merge exited 0.
- Why: the clean check uses `--untracked-files=no` (`gitops.py:346`), and git's merge overwrites
  ignored files (`gitops.py:358`, `cli.py:1409`).
- **Fix:** merge with `--no-overwrite-ignore`, or check the incoming paths first. Refuse and name
  the path, and keep the task commit available.

**2. ~~Removing a remote can delete a hand-configured provider.~~** **Fixed (#197).** Codex; source.
- `remote_pid` derives the provider id from the hostname alone (`models.py:1303`).
- `add_remote` keeps an existing provider of that id even when its endpoint or port differs, yet
  returns the new endpoint (`models.py:1317`).
- `remove_remote` deletes whatever provider has that id, with its models and overrides, without
  knowing whether wb created it (`models.py:1347`).
- **Fix:** record what wb created and refuse a name collision. Add and remove must leave unrelated
  provider settings alone.

**3. ~~Two offline web-chat tabs lose a queued message.~~** **Fixed (#199).** Codex; source.
- Each tab loads the outbox from localStorage once (`app.js:56`), then saves its own in-memory
  copy over the shared key (`app.js:657`). The last tab to write wins.
- Codex: after reconnecting, only the second tab's message reached the server.
- **Fix:** store each item under its own key by client id, merge and drain across tabs, and check
  message ids on the server beyond the current dedup window (`chat.py:148`).

### Reporting success that didn't happen

**4. ~~A merge left unfinished is reported as merged.~~** **Fixed (#190).** Codex; reproduced.
- With `branch.main.mergeOptions=--no-commit`, `git merge` exits 0, HEAD doesn't move and
  `MERGE_HEAD` stays. Reproduced here.
- `gitops.merge` returns HEAD's hash, and accept records the task as merged (`gitops.py:358`,
  `cli.py:1411`). The developer is left mid-merge.
- **Fix:** pass `--commit` explicitly. Before reporting success, check that the branch is
  reachable from HEAD and that no `MERGE_HEAD` remains. Refuse when a merge is already in
  progress.

**5. ~~An interrupted `wb next` strands the next step.~~** **Fixed (#191).** Codex; source.
- `cmd_next` commits, then sets `round`/`steps`/`step_round` while the phase is still `review`
  (`cli.py:642`), then runs `fresh_reason`, which can take up to 30 s, before setting `queued` and
  spawning the runner (`cli.py:649`).
- Interrupted in that window, the task shows step 2 under review with no runner. A retried
  `wb next` dies with "nothing to commit". `wb accept` then closes the task on step 1's commit
  (`cli.py:1390`), and step 2 never ran.
- **Fix:** record the pending transition and resume it on retry. Accept must notice a step that
  never started.

**6. ~~The base check runs in the agent's own worktree.~~** **Fixed (#192).** Codex; source.
- `base_check` runs the acceptance command in `task.worktree` before round 1 (`runner.py:784`,
  `runner.py:1168`). Whatever it writes stays for the agent: a build stamp, generated fixtures,
  updated snapshots.
- That can make a later test pass that shouldn't, and the files show up in the review diff.
- **Fix:** run the base check in a disposable checkout, as `wb verify` already does.

**7. ~~`wb verify` calls a setup error a "proven" regression.~~** **Fixed (#193).** Codex; reproduced.
- `ASSERTION` (`verify.py:29`) matches `expected`, so `SyntaxError: expected ':'` counts as an
  assertion. `verdict(1, "SyntaxError: expected ':'", 0)` returns `proven` here.
- A failed-test header also qualifies without any assertion (`verify.py:54`, `verify.py:92`).
- **Fix:** syntax, import, collection and fixture errors are inconclusive. "Proven" needs an
  assertion line or a failure signature the caller supplies.

**8. ~~`wb verify` breaks on a renamed test.~~** **Fixed (#194).** Codex; source.
- `test_files` uses `--no-renames` (`verify.py:46`), so a renamed test lists both names. The base
  overlay then runs `git checkout <tree> -- <deleted name>` (`verify.py:79`), which stops on a
  pathspec error before any test runs.
- **Fix:** build the overlay from name-status, skipping deletions and handling renames.

**9. ~~A stream of unrecognized events defeats the round's time limits.~~** **Fixed (#195).** Codex; source.
- In `run_round`, an unparsable line, a JSON line that isn't an object, or an event `normalize`
  drops all `continue` before the wall-clock, silence, think and steer checks (`runner.py:475`,
  `runner.py:496`, `runner.py:549`).
- An OpenCode release that prints a new progress event often enough can run past
  `--round-timeout`, ignore `wb steer --now` and hold the GPU lease.
- **Fix:** run the clocks and the steer check on every pass, whatever the line was.

**10. ~~The optimizer can unload the remote model an active task is using.~~** **Fixed (#198).** Codex; source.
- `optimize.main` takes the local lease (`GpuLease(None, …)` is pool `gpu`), not the remote
  server's `gpu-<host>` pool (`optimize.py:327`, `models.py:1159`).
- `busy()` lists only other models on that server, leaving out the same model (`optimize.py:124`).
- `probe` then unloads it (`optimize.py:152`). `start` checks only local holders as well.
- **Fix:** take the target server's lease before probing, and make an automatically due run skip
  a busy remote server.

**11. ~~Tests run in `/bin/sh`, the agent in bash.~~** **Fixed (#196).** Claude; source.
- `run_in_lane` runs `/bin/sh -c` (`runner.py:719`), and `verify.py` and `bench.py` go through
  it. The agent's OpenCode shell is bash or `$SHELL`.
- On Debian and Ubuntu `/bin/sh` is dash: `source .venv/bin/activate && pytest`, `[[ ]]` and
  arrays fail for the runner while passing for the agent. Each false failure costs an auto-fix
  round and then wakes Claude.
- **Fix:** run tests in the shell OpenCode uses (bash when present) and record which shell it was.

## P1: making the core workflow work for more users

**12. ~~A task's worktree isn't the project as the user has it.~~** **Fixed (#201).** Both; source.
- The worktree checks out only committed files (`gitops.py:107`/`120`). It has no
  `node_modules`, `.venv`, `target/`, build directories, `.env`, generated fixtures or local
  tools.
- It lives under `~/.local/state/…` (`store.py:212`), so sibling-relative dependencies break:
  Cargo `path = "../x"`, a pnpm/npm workspace in a parent directory, Go `replace ../`, editable
  sibling packages.
- Consequences:
  - The base check fails or times out for missing dependencies, not for the step.
  - Every task reinstalls, and a Rust or C++ project rebuilds from scratch, using up round time.
  - Poetry makes a new virtualenv per path.
- Codex reproduced a project whose check passed in the checkout and failed both base and
  candidate in the task.
- **Fix:**
  - A repeatable setup step per task or per repo (`wb start --setup 'npm ci'`, or commands.json),
    run before the base check and inside the lane.
  - Optionally, copy named ignored directories (`cp --reflink=auto`), keeping user-specific
    settings out.
  - A configurable worktree root, for example beside the repo.
  - A diagnosis of missing setup before any model round.

**13. ~~A provider defined outside the default config passes discovery, then vanishes at run
time.~~** **Fixed (#202).** Both; source.
- wb reads `$WB_OPENCODE_CONFIG` (`models.py:114`), but the agent's OpenCode gets
  `OPENCODE_CONFIG` pointed at the per-task layer, which has no `provider` block (`runner.py:85`,
  `runner.py:176`).
- Codex saw "Model unavailable" before any request reached the server. A user's own
  `OPENCODE_CONFIG` gets only a note from `wb doctor` (`cli.py:2784`).
- **Fix:** discovery, preflight and execution must use the same providers. Merge them into the
  layer, or refuse at `wb start`.

**14. ~~wb rejects a model alias that OpenCode runs.~~** **Fixed (#203).** Codex; source.
- An OpenCode entry can be keyed `friendly` with `"id": "cap"`, the name the server knows. wb uses
  the key as the server-side name everywhere (`models.py:391`, `models.py:404`).
- `status` then says "the server lists N models, not friendly" (`models.py:575`), while OpenCode
  itself sends `cap` and succeeds.
- **Fix:** keep both: the key for routing and selection, the effective upstream id for
  availability, loading and placement.

**15. ~~Localized git blocks `wb init`, and with it the first-run sample.~~** **Fixed (#207).** Codex; source (no
French locale installed here).
- `cmd_init` recognizes an empty directory by the English text "not a git repository"
  (`cli.py:2310`), and the sample calls it (`cli.py:2602`). `wb start` has the same check
  (`cli.py:391`).
- **Fix:** run machine-read git calls with `LC_ALL=C`, or classify by exit code
  (`rev-parse --is-inside-work-tree`), keeping git's own message for the user.

**16. ~~The GPU budget can be wrong for the hardware local-model users favor.~~** **Fixed (#206).** Both; source.
- **Unified-memory Linux.** `hardware.gpus`/`models.budget_devices` (`hardware.py:94`,
  `models.py:876`) handle unified memory only on Apple Silicon.
  - AMD Strix Halo: amdgpu reports only the BIOS carve-out as VRAM, and `mem_info_gtt_total` is
    never read. A carve-out under 1 GiB is dropped as an iGPU.
  - DGX Spark / Jetson: `nvidia-smi` reports `memory.total` as `[N/A]`.
  - The result is no budget: no sharing, and the router can't keep a too-big model off.
- **Device visibility.** The budget sums every card of one driver (`models.py:890`/`905`,
  `router.py:155`). With two 8 GiB cards and the server limited to one by
  `CUDA_VISIBLE_DEVICES=0` (or its service's own setting), the budget says 16 GiB. Nothing reads
  `CUDA_VISIBLE_DEVICES`/`HIP_VISIBLE_DEVICES`.
- **Fix:**
  - Read GTT for APUs, and treat NVIDIA `[N/A]` as unified memory sized from MemTotal.
  - Prefer the serving runtime's measured placement where available.
  - Add a per-server budget setting as the escape hatch. Today only the test-only
    `WB_FAKE_GPU_GB` exists.

**17. ~~A model server that is only briefly away fails the task for good.~~** **Fixed (#205).** Both; source.
- `make_resident` raises at once when the model isn't available (`models.py:738`), and the
  runner's `_load_failed` ends the task (`runner.py:1150`, `runner.py:1182`).
- So `wb start --force`, which `wb start` suggests for "its server is just down" (`cli.py:414`),
  queues a task that fails on its first check. A server or remote PC that comes up seconds later
  doesn't save it.
- The same holds for an Ollama restart, an out-of-memory kill or CUDA error between rounds, and a
  round where OpenCode exits non-zero with no text (`runner.py:1224`).
- **Fix:** a bounded, cancellable wait with backoff for "server down", then reload and retry once.
  Keep failing at once on configuration errors.

**18. Nothing is cleaned up, and the cost grows with history.** Both; source, with measurements
from the state directory here.
- After 101 tasks in about three days:
  - `notify.jsonl` is 529 KB for 158 notices, because each carries a diff of up to 32K.
  - `rules/<task>` is never removed: 50 are left over.
  - Merged `delegate/*` branches are never deleted after `--merge` (`cli.py:1410`): 8 merged ones
    remain in one repo.
  - 13 `/tmp/wb-run-*` directories are left over.
- `store.all_tasks()` reads every task.json and status.json on each `wb wait` wake, each minute of
  `wb events`, and each `wb start`. `store.notices()` parses the whole notify file on every wake.
- Chat history: `thread()` parses the whole `messages.jsonl` to return the last 200
  (`chat.py:186`, `chat.py:701`, `store.py:185`). Codex measured 0.6 ms at 200 rows and 56 ms at
  20,000.
- **Fix:**
  - Archive closed tasks after N days, and rotate `notify.jsonl` (only notices newer than an ack
    matter).
  - Read JSONL tails with a bound.
  - Delete the rules and runtime directories at close, and run `git branch -d` after a merge.

## P2: needed for 9+

**19. ~~A rejected API key counts as runnable.~~** **Fixed (#204).** Codex; source. A server answering 401/403 is
marked available and resident (`models.py:560`), on purpose: a key-protected server is running.
But a wrong key passes too, and the router can pick that model over a working one
(`router.py:168`). **Fix:** keep "reachable" separate from "ready". Retry the list with the key
the agent will send, and if that's refused, diagnose the key.

**20. ~~Starting a model server can spin on its readiness check.~~** **Fixed (#205).** Codex; source.
- When a start command exits 0 at once (systemctl, docker, `cmd &`), `p.wait(timeout=5)` returns
  immediately on every pass. The loop then probes `status()` back to back (`models.py:754`,
  `models.py:775`).
- Codex counted 3,095 requests in 0.8 s, and a slow model load could run that for minutes.
- **Fix:** pace the probes on a monotonic deadline, whether or not the launcher has exited.

**21. Optimizing two tags of one model writes the same name.** Codex; source. `apply` names the
new entry `<family>-<N>k` with the tag removed (`optimize.py:230`). `family:7b` and `family:32b`
both become `family-16k`, in Ollama and in OpenCode's config (`optimize.py:239`). Earlier tasks
and the router's history then point at a name whose meaning changed. **Fix:** derive the name
from the whole source (tag and quantization), and refuse when an incompatible one exists.

**22. `OLLAMA_NUM_PARALLEL` is treated as on or off.** Codex; source. For the same model,
`can_share` admits any number of tasks once the value is 2 or more (`models.py:946`/`968`). A
third task's requests then queue inside Ollama while its silence budget runs. **Fix:** count
holders per server and model against the limit, and start a task's clocks only once it's
admitted.

**23. ~~macOS lacks parts of the Linux safety net.~~** **Fixed (#208);** the native macOS run is a 1.2 release gate. Both; source.
- `sweep` needs /proc (`runner.py:231`), and PDEATHSIG is Linux-only (`lane.py:163`). `reap`
  clears `agent_pid` without killing its group (`cli.py:159`). After an abrupt runner death, the
  agent (`runner.py:438`) keeps editing a task reported as failed and keeps the model busy.
- `_own_addresses` uses `ip` (`models.py:198`), which macOS doesn't have. A baseURL with the
  Mac's own LAN IP counts as remote, so the model is excluded.
- **Fix:** kill the saved agent's group in `reap` (checked with `models.runs()`), fall back to
  `ifconfig`, and add a native macOS stop-and-recover test.

**24. ~~`wb chat on` reports success when another program owns the port.~~** **Fixed (#200).** Codex; source.
`chat.running()` only tries a TCP connect (`chat.py:437`). The start path prints the URL after a
fixed half-second sleep (`cli.py:2420`). **Fix:** check for an identifiable workbench response,
and wait a bounded time for the started server before reporting.

**25. ~~A VPN-only chat stays on loopback after the VPN comes up.~~** **Fixed (#200).** Codex; reproduced.
- With `bind: ["tailscale"]`, `plan()` returns `['127.0.0.1']` while Tailscale is down and
  `['100.64.0.1']` once it's up. Reproduced here.
- The replan thread restarts only when the list gets longer (`chat.py:887`), so the change is
  never noticed.
- Where it does restart, `os._exit(75)` ends a chat started without a supervisor (`chat.py:448`).
- **Fix:** compare address sets, and re-exec in place when there's no supervisor.

**26. OpenCode version drift goes unnoticed.** Claude; source. Only `wb doctor` checks the
version, and only that the major version is at least 2 (`cli.py:2767`). The permission rules
depend on behaviour checked against 2.3, so a 3.x would pass silently. **Fix:** keep a tested
range, have `wb start` warn outside it, and check after round 1 that `workbench_ask_reviewer` was
listed.

**27. The fixed prompt isn't checked against the model's context window.** Claude; source.
CLAUDE.md, nested instruction files, the HANDBOOK, the system prompt and the tools are never
estimated at `wb start`. A 25 KB CLAUDE.md on a 32K window starts near the handover point, and
the agent loops between "reading full" and handovers. **Fix:** estimate at about 4 characters per
token, and warn or refuse above roughly 40% of the window.

**28. "Whose task is this" compares raw path strings.** Claude; source. The mod's `within`/`near`
(`register.tsx:518`) and Python's `_scope` (`cli.py:856`, which resolves only `here`) don't
normalise paths. Symlinked project directories, `/tmp` vs `/private/tmp`, and case-insensitive
APFS all miss. This only matters for adopting tasks after a crash or restart, and for tasks
started by hand. **Fix:** compare real paths, case-folded on macOS.

**29. Every other Ollama model is unloaded before every round.** Claude; source. `free_gpu`
(`models.py:800`) runs before each round, even when everything would fit. That includes the
user's Open WebUI or IDE autocomplete model, and an embedding model the project's tests need.
**Fix:** evict only when what's needed plus what's resident exceeds the budget, and add a "keep
loaded" list.

**30. ~~A repo on WSL's `/mnt/c` gets no warning.~~** **Fixed (#201).** Claude; source. git, and `gitops.mark()` at
every step, are much slower on the Windows filesystem. **Fix:** a note at `wb start`.

**31. ~~The first automatic optimize on a fresh state directory crashes.~~** **Fixed (#198).** Codex; source.
`optimize.start` opens `optimize.log` in `WB_HOME` without creating the directory first
(`optimize.py:265`). `wb setup` catches the error and carries on (`cli.py:2522`), so the first
measurement silently doesn't happen. **Fix:** create the directory first, and test first run with
an empty profile.

## P3: polish

- **~~Locale:~~** **Fixed (#207: the launchers re-exec with `-X utf8`).** The `wb` CLI itself, unlike the runner (which uses `-X utf8`), reads and writes
  several files without an encoding (`cli.py:965, 1180, 1895, 1910, 2008, 2437`). Under a legacy
  non-UTF-8 locale this can raise. Give every text read and write an explicit UTF-8 encoding, as
  `store.py` already does.
- **Temp-file names:** `store.write_json` names its temp file by pid only (`store.py:77`). Two
  chat-server threads writing the same unlocked file (`push-test.json`) can collide and return a
  500. Use `mkstemp`.
- **WSL:** fall back to `/usr/lib/wsl/lib/nvidia-smi` when it isn't on PATH, as can happen under
  a systemd unit.
- **Alpine/musl:** `find_library("c")` can return None, so `watch.py` drops to polling. Use
  `CDLL(None)`.
- **README:** the setup path hard-codes `~/.claude`, which is wrong for `CLAUDE_CONFIG_DIR` users.
- **The mod:** it declares no minimum Claude Code version and doesn't guard its calls to the
  plugin API. Because installs track main, someone on an older Claude Code gets runtime errors
  rather than a message. `claude plugin validate` warns about the missing version (reported by
  Codex; the missing version is deliberate).

## Release gates for 9/10

1. **Data and verdicts.** A regression test for each P1 item above (1–11). Each must check the
   resulting file, commit, task state or message independently, not just wb's own report.
2. **Supported environments.**
   - Run install → sample → delegate → answer → review → interrupt → resume → accept on Linux
     (Debian/Ubuntu, for dash), native Apple Silicon macOS and WSL2.
   - Include Python 3.11, a non-English git locale, spaces in paths, a WSL project on both the
     Linux and the Windows filesystem, and a laptop sleep and wake.
   - Record pass, skip and failure separately.
3. **Models and long-running use.**
   - Cover CPU-only or low-VRAM inference, a discrete GPU, Metal, a unified-memory Linux box, a
     multi-GPU server limited to some cards, a remote Ollama and a keyed multi-model server.
   - Queue past capacity and interrupt loads.
   - Use the web chat in two tabs with reconnects and a large history.
4. **CI matches what's advertised.**
   - Today the only required check is Linux (`check.yml:29`).
   - The macOS nightly is `continue-on-error` and doesn't install OpenCode (`nightly.yml:62`).
   - There is no WSL2 job (`rulesets/main.json:24`).
   - Make the advertised-platform smoke runs a recorded condition of release.
   - Look into the 11 isolation tests Codex saw fail with the clone and profile under `/tmp`.
