# How Claude's Workbench works

The full account, for agents and contributors: every piece, what it does, how and why.
[README.md](README.md) is for people (the pitch and a quickstart); [CLAUDE.md](CLAUDE.md) is the
compact reference loaded into every session that changes the code, and points here for detail.
A change that alters what this file says (a command, a flag, a default, a guard, a file, a format)
updates it in the same commit.

"The user" is the person running Claude Code; "Claude" is the Claude Code session driving the
work; "the agent" is the model running in OpenCode on one task: on this machine's GPU, or an API
model through a provider the user connected.

Contents: [The pieces](#the-pieces) · [Why it's built this way](#why-its-built-this-way) ·
[A task, start to finish](#a-task-start-to-finish) · [The runner, inside](#the-runner-inside) ·
[What the agent knows](#what-the-agent-knows) · [Talking to a running agent](#talking-to-a-running-agent) ·
[Context, on both sides](#context-on-both-sides) · [Waking Claude, exactly once](#waking-claude-exactly-once) ·
[Seeing what the agent does](#seeing-what-the-agent-does) · [Models](#models-found-loaded-chosen) ·
[Bench](#bench-real-tasks-replayed) · [The skill library](#the-skill-library) · [Images](#images) · [Audio](#audio) · [Git, commits and projects](#git-commits-and-projects) ·
[The web chat](#the-web-chat) · [Stopping everything](#stopping-everything) · [The mod, inside](#the-mod-inside) ·
[Waiting without polling](#waiting-without-polling) · [Every `wb` command](#every-wb-command) ·
[Files and formats](#files-and-formats) · [The tests](#the-tests) · [Couplings](#couplings-change-one-change-the-other)

## The pieces

```
 user ─▶ Claude Code ──── /workbench skill (plan, spec, review) · mod (pane, status line, wake-ups, web chat)
          ▼
         wb ── writes ──▶ $WB_HOME: tasks/<name>/ rules/<name>/ notify.jsonl results.jsonl chat/ ◀── wb events
          │ spawns                ▲
       runner (detached, per task)┘ status, events, reports, notices; GPU lease and the model (an API task: no lease, its key in the keyhole)
          ▼
       lane.py (Landlock) ─▶ opencode run in tasks/<name>/worktree (branch delegate/<name>)
          │      └─ ask_reviewer (MCP, wb-ask-mcp): writes the question, blocks for the answer
          ▼
       local model server (Ollama, or any OpenAI-compatible server on this machine), or an API task's keyhole ─▶ its provider
```

- **`/workbench`** (`skill/workbench/SKILL.md`): Claude's procedure (size, spec, dispatch, review
  in proportion to size, accept). Every line of SKILL.md rides on each of Claude's calls, so it
  holds only what every session needs (46KB on 2026-11-17, 30KB by 11-18); the rest is in
  `reference/*.md`, among them `checks.md` (the compiler, typecheck or lint ahead of a task's
  tests in `--test`: OpenCode 2 runs no language servers, 2026-11-22). Only the user starts the
  mode. The mod keeps it until a quit command, reminds Claude after a compaction or `/clear`,
  counts a delegating `wb` command as the mode, keeps it by session id in `$.store`, and turns it
  on when it adopts a task still delegated (#689; not one parked or taken over, #834).
- **`wb`** (`bin/wb` → `cli.py`): one thing per command, a short plain report.
- **The runner** (`runner.py`): detached, one per task, round after round until the task needs
  Claude, then exits.
- **OpenCode**: the model headless in the worktree, inside the lane (`lane.py`), with a
  read-only `git` (`gitguard.py`).
- **The mod** (`mod/`): the pane, a status line, a turn started the moment a task needs Claude.
- **The web chat** (`chat.py`, `chat_static/`, `webpush.py`): the same session from a phone.

The runner, the ask server and the chat server never call each other or the mod: they share
files in `$WB_HOME` (`store.py`), and waiters sleep in the kernel (inotify, pidfd: `watch.py`).
The mod's only calls are `wb` subcommands (`events`, `watch --json`, `status`, `chat
ack|claim|url|log|ask|wait|answer|on|off`, `stop|takeover --all --repo … --session …`, `update`
and its weekly `update --check --if-due`, `optimize start --if-due` once a session), plus `kill
-0` where there is no `/proc` and `tail -c`. A crash loses nothing; everything reads with `cat`.

`bin/wb` and `bin/wb-ask-mcp` check the interpreter first: below 3.11 they exec the newest
`python3.N` (N ≥ 11) on PATH that runs with ctypes (a pyenv shim exits 127, #526), with `-X
wb-python` against a second hand-over; with none, exit 78 and one line (the chat's unit stops on
78); native Windows is refused in one line; a non-UTF-8 locale re-execs with `-X utf8`.

**Where Python processes start.** Each finds the package by path:

| Process | Started by | How it finds `workbench` |
|---|---|---|
| `wb` | the user, Claude, the mod | `bin/wb` puts its real path's repo root on `sys.path` (symlinks work) |
| the runner | `cli.spawn_runner` | `python -X utf8 -m workbench.runner <task>`, cwd and `PYTHONPATH` the repo root (the caller's in `WB_USER_PYTHONPATH`: `context.detached_env`), own session and, where logind kills at logout, own systemd scope (`cli.detach`, `cli.runner_scope`, #351), output to `runner.log` |
| the ask server | OpenCode, the layer's `mcp.servers.workbench` | `[sys.executable, <root>/bin/wb-ask-mcp]` |
| the git shim | the agent's shell (`<rules>/bin/git`) | a `/bin/sh` wrapper exec'ing `<python> -I -c` (quoted: a shebang splits at a space) with `sys.path.insert` |
| the lane | the runner | `python -I lane.py …`, standard library only |

## Why it's built this way

- **Claude's tokens are scarce; the agent's are free on the GPU and cheap on API.** The agent types; Claude specs and
  reviews; reports are short, logs stay in files.
- **Small local models lose the thread** (repeat, plan for minutes, fill their window, compact
  lossily): tasks sized to the learned slice, guards, handovers to a fresh session, OpenCode's
  compaction only a backstop.
- **The summary is a claim, not evidence.** Claude reads every diff; tests gate every round;
  nothing leaves the worktree until Claude commits it.
- **A teammate with limits, not a prisoner.** Everyday tools run unasked; the rest is asked for
  and granted (`wb allow`). Git writes, credentials, Claude's files and writes outside the lane
  are enforced by the kernel where it can (Landlock), and the tests run under the same limits.
- **Learned, not configured**: model per size, slice, speed, handover point, corrected by
  `results.jsonl` on this machine.
- **Files, not services**: a task survives Claude restarting, a crash, a closed terminal.
- **One ordinary GPU taught** (the router's defaults): a model all on the GPU beats a bigger one
  spilling into RAM; at equal memory a larger model at ~4 bits beats a smaller at 8; thinking off
  works best (hence the think budget); a weekly check, nothing downloaded without the user's yes.

### What a handoff costs Claude, measured
The README's "about half the tokens of writing the code itself" comes from paired runs on
2026-11-18: each request done by Claude alone and handed off, in subagents on the same model,
graded by hidden tests (all passed), tokens weighted by price (input 1, cache write 2 at the
1-hour cache, cache read 0.1, output 5). One 16 GB card, a 27B model at the 40K window `wb
optimize` found all on the GPU, self-review on, the request pasted as the spec:

| Task | Claude writes it | Handed off |
|---|---|---|
| Save and load replay files (C, significant) | 166K | 150K (−10%) |
| Score combos (C, significant) | 176K | 145K (−18%) |
| A date range for a report (Python, trivial) | 50K | 67K (+34%) |
| Rank a high score (C, trivial) | 54K | 83K (+54%: one round sent back) |

The significant pairs start fresh (both pay the skill, ~29K, and the base context, ~50K); the
trivial pairs are warm (a task before them in the session); about 10K of each handed-off figure is the harness waiting on `wb wait`, which the mod's wake replaces.
Warm, the replay-files handoff was ~48K (dispatch 9K, review wake 31K, accept 8K) against ~116K
writing it: the "roughly half". Runs vary ~0.2×; a round sent back costs ~25K. The first
handoffs (2026-11-17) cost 1.6–4.5×. What brought it under 1×: the agent's self-review (each
check it wrote broken and seen to fail), so Claude signs off in one call; the request pasted as
the spec; no wake without work for Claude; the diff in the wake, only what changed after
feedback; a window that fits the card (spilling halves the speed; too small compacts). Where it
doesn't hold: trivial tasks cost as much or more (batch them); four tasks on one card and model,
a demonstration not a benchmark; 6–30 local minutes a task. Energy: 0.03–0.16 kWh a significant
task (1–5 cents at $0.30/kWh) against ~25 cents of Claude tokens saved.

## A task, start to finish

```
 wb start ─▶ queued ─▶ running ──▶ testing ─┬─pass, or fail with auto-fix spent, or no --test─▶ review
              (GPU,      │  ▲  ▲            └─fail: auto-fix round (up to --auto-fix) ─▶ running
               load)     │  │  └─ a note, an interrupt, a nudge, a handover: the next round, straight away
                         │  └──── asking: ask_reviewer blocks until wb answer / wb allow
                         ├─▶ stuck  (a guard, wb stop, a SIGTERM not from wb)            ─┐
                         └─▶ failed (opencode died, a crash, the runner died without a word) ─┤
 review ─▶ wb accept ─▶ accepted                               ├─▶ wb feedback ─▶ queued (a review round)
        ├─ wb next ─▶ queued (the next step, same agent)       ├─▶ wb resume ─▶ queued (a fresh session)
        ├─ wb feedback ─▶ queued                               └─▶ wb discard ─▶ discarded
        └─ wb discard ─▶ discarded            wb takeover (any open task) ─▶ review, Claude finishes it
```

**Phases** (`store.py`): `ACTIVE` = queued, running, testing, asking; `NEEDS_CLAUDE` = review,
stuck, failed, asking; `CLOSED` = accepted, discarded.

| Phase | Set by |
|---|---|
| `queued` | `wb start`, `feedback`, `next`, `resume`; the runner on every start |
| `running` / `asking` | the runner per round; `asking` while a question is open (`ask_mcp.relay`) |
| `testing` | the runner while the acceptance command runs |
| `review` | the runner (tests pass, auto-fix spent, or no `--test`); `wb takeover` |
| `stuck` | the runner (a guard; a SIGTERM not wb's: `runner.SIGNAL_STOP`; `runner.UPDATED`, #603); `wb stop`; `wb accept` whose commit fails on a live task ("accept failed: …") |
| `failed` | the runner (opencode exited non-zero: `runner.died`; setup; load; a crash or full disk); `cli.reap`. All but `runner.died` (save one whose server went down again, `infra` "model", #657) set status `infra`, which `wb bench` doesn't grade (#508) |
| `accepted` / `discarded` | `wb accept` / `wb discard` |

Rounds within one runner have no `queued` between; it exits only on review, stuck, failed, a
SIGTERM or a crash.

**The role picks the model** (#1016, *Roles*). `wb start` resolves the task's role (`--role`, else
from `--size`: `routes.role_and_size`) to its model (`routes.resolve`, through `cli.pick_model`):
routes.json read first (an unreadable one exits 3, never read as all GPU); a `--model` as asked (an
API one on the dialog's yes, `cli.start_gate`); else the role's side (`routes.effective_via`: the
Agents switch over the role's own `via`): the GPU, its pin (`gpu`) or the router's pick
(`router.choose`, exactly as before roles), or API, the role's model by its exact id. **No
fallback, in either direction**: a GPU role with no GPU model exits 3 with the router's line and
never tries its stored API model; an API role whose model can't run exits 3 with one line
(`routes.cant_run`: `trivial role: <id> can't run: <why> (<fix>). Ask the user: fix that, or put
the role on the GPU (wb route trivial gpu)`, or `… set Agents to Both or GPU (wb route all
both|gpu)` under Agents API) and never the GPU; Agents on API with a role that has no API model
exits 3 too (`routes.no_api_model`). The choice is fixed in task.json (`via`, `role`, an API
model's `provider`, `thinking`, `said`; a task from before #1016 has none, and reads as GPU): feedback, next and resume never resolve again, so a role
changed mid-task applies to the next new task only. **A model gone from the catalog mid-task**:
the runner fails an API task whose `models.resolve` raises (`runner._left_catalog`, `infra`
"model", before any round) with `runner.left_catalog`'s line, `task <t>: <id> left OpenCode's
catalog: workbench never switches a task's model. wb takeover <t> to finish it, or wb discard <t>`
(a bare KeyError said `could not load <id>: '<id>'`), and `wb feedback`, `next` and `resume` exit 3
with it first (`cli._model_still_listed`), before any round is written. A catalog whose read failed
lists nothing, so then the line says the model `can't be checked: OpenCode's catalog can't be read
now (<why>)`, never that it left. An API role's model that can't run exits 3 even with `--force`
(a `--model`'s).

**A runner that dies without a word** (SIGKILL, OOM, reboot) left its task live forever
(2026-11-16). `cli.reap`: a live-phase task whose runner isn't alive and whose status is
unchanged for `REAP_GRACE` (15 s: feedback, resume and next set `queued` before `spawn_runner`
writes the pid) becomes `failed` ("the runner died (see runner.log)", an `error` event, one
notice). A status `boot` other than `store.boot_id` is a restart: `last` `RESTARTED`, and the
first reaper takes that session's other tasks (task.json `session`, `cwd`) quietly, with one
notice naming them and `wb resume <a> <b> …` by name (`_restart_notice`, #692, #544). It is
`Task.set_status_if` under `locks/<task>.status.lock`, so one reaper notifies. Called by `wb
status`, `wait`, `feedback`, `resume`, `next`, `steer`, `accept`, `wb events` once a minute,
`wb top` and `wb log -f`. The agent's group (`agent_pid`) is killed if `models.runs` shows it
still runs `run --standalone` (off Linux it outlives its parent), and `runner.sweep` kills what
it left; on Linux `lane.py` sets `PR_SET_PDEATHSIG`.

On a shared state directory, a runner whose status `machine` isn't `store.machine()`
(`/etc/machine-id`, macOS's hardware UUID, else the host name) is alive to `runner_alive`, never
reaped nor signalled (#849: each machine failed the other's live runners): `_stop_runner` refuses
it, `stop --all` and `update --stop` skip it, `wb status` shows "on <host>:". Runners beat
(`locks/<task>.runner.lock` touched every `runner.BEAT_SECS`, 60 s, by `runner._beat`; `.lock` is
`QUIET`, so it wakes no `wb events`; status `beat`); another machine's older than `cli.BEAT_DEAD`
(30 min, for clock skew and sleep) is dead (`cli._beats`, #872): reaped, not as a restart, its
`agent_pid` left alone, cleared by stop and discard without a signal; one with no `beat` is
trusted. `wb doctor` reports `store.network_fs` (the longest mount point holding it: an autofs
trigger lists first, #896); per-machine state (the GPU lease, optimize.json and .log, boxes.json,
wsl-gpus.json, speed.json, and any new such file) lives in `store.machine_dir`.

**A noexec state directory** (#851): the git shim, worktree and scratch checkouts live under
`$WB_HOME`, so every agent git call exited 126. `store.noexec()` (statvfs `ST_NOEXEC`, Linux):
`wb start` refuses and `wb doctor` is BAD, with `cli.noexec_line()`. `X_OK` on a directory is
search, so `os.access` can't tell.

**A full disk** (#342) made every status write, and `reap`, raise. `wb start` refuses without
room (`cli._disk_room`): the base's tracked size (`gitops.checkout_size`: LFS, sparse and partial
clones and submodules at what they really check out, never fetching a blob to size it, #436,
#886) plus `--copy`, times `DISK_COPIES` (3: the worktree, the base check's and `wb verify`'s
scratch checkouts) plus `DISK_SPARE` (2 GB, `runner.CHECKOUT_SPARE`), less open tasks' task.json
`checkout_bytes` until their status has `checked_out` (`runner.pending_checkouts`, #676: four
starts each saw the disk as the first did); unknown free space refuses nothing. The runner checks
again before the checkout and each scratch copy (`runner._no_room_for`, #860). On ENOSPC/EDQUOT
(`store.disk_full`) the crash handler sets `failed` or exits quietly, `reap` says so once on
stderr and leaves the task, other commands print one line (`cli.main`); that line, the refusal
and `wb doctor` (BAD under `DISK_SPARE`) name history past 100 MB (`cli.history_held`, #740). Free is
`store.free_bytes`: on macOS APFS purgeable space counts (`store._important_free`, #852); on WSL at
most the Windows drive's free space (`store.host_drive`: a full C: shows as EIO, #897).

**1. Claude writes a spec** (the skill): one concern within the model's slice, files named, code
to imitate, exact names for interfaces, an acceptance command. Bigger work, or an approved plan,
is a list of such steps, with what stays Claude's marked apart.

**2. `wb start` sets it up** and returns at once (`cli.cmd_start`). Before making anything:
- the name: `store.NAME_RE` (`^[a-z0-9][a-z0-9-]{0,47}\Z`; `$` passed a trailing newline), never
  reused; every command refuses a non-name through `cli.need` (#782: `foo/` opened `tasks/foo`
  while the runner's argv said `foo`, so reap and discard hit a live task);
- a git repo, with git's own reason (`gitops.toplevel` raises `NotARepo` by rerunning under
  `C_LOCALE`); the spec; the `--skill` names; `--base` a commit (`gitops.base_commit`); an
  identity (`cli._identity`: `git var GIT_AUTHOR_IDENT`/`GIT_COMMITTER_IDENT`);
- `CLAUDE.md` or `.claude/CLAUDE.md` (`cli.PROJECT_MEMORY`) committed at the base
  (`--no-claude-md`); a gitignored one passes, the agent getting the checkout's copy
  (`context.instruction_files`).

Then:
- **The model**: the role's (*The role picks the model* above: `--role`, else from `--size`,
  each filling in the other; neither is significant): on the GPU its pin or the router's choice
  for the size and the stack (a monorepo package's own, #820), on API its API model; or `--model`
  (an id or fragment). Refused first when
  `runner.opencode_bin()`'s first word isn't on PATH. No model can run: exit 3 with
  `models.remedy`'s next steps; `--force` queues a `--model` that can't run now (hinted only
  when `models.may_come_up`), never a role's API model.
- **Two projects, one GPU**: when a holder (a task, `wb image`, `wb audio`) leaves the model no
  room (`models.admits`, the lease's rule), exit 4 naming each holder and `--queue` (2026-11-16:
  it queued silently for hours). Holders: gpu.json's live entries plus live runners whose status
  has no `leased` yet (`cli._gpu_holders`; one that stepped aside for its tests isn't, #805), not
  another machine's (`store.elsewhere`, #895); a remote model is checked against its own server's
  pool (`models.pool`, #750). Known limit, by choice: no lock, so two starts at once can both
  pass; the second waits, saying so (`GpuLease._take_alone`). `feedback`/`next`/`resume` always
  queue, printing the holders (`cli._gpu_note`).
- **An API model's provider slots** (#1015): an API task needs no GPU, so none of that applies
  (`models.pool` is None for it, `_gpu_holders` never counts it). Its provider runs at most
  `models.API_PARALLEL` (3) of workbench's tasks at once; a fourth exits 4, "<n> tasks already
  run on <provider>'s API (<holders>): workbench runs at most 3 at once per provider. --queue
  starts it anyway, to wait for a slot; or do this step yourself" (`cli._slots_full`), and with
  `--queue` prints `note: 3 tasks run on <provider>'s API; this task waits as queued for a slot`
  (`_slot_note`, also `feedback`/`next`/`resume`'s note through `_gpu_note`).
- **The limits**: unset (or 0) `--round-timeout`, `--silence-timeout`, unset `--think-timeout`
  take `router.budgets`; those no flag gave (task.json `timeouts_given`) stretch before each round
  (`runner.stretch_limits`, *Budgets and speed*, #814). `--checkin` 0 is off.
- **Not the worktree**: the runner makes it (`runner.checkout`, task.json `checkout: "runner"`,
  status `checked_out`, `checkout_secs`), branch `delegate/<task>` from `--base` (HEAD), since a
  big checkout passed Claude's two-minute Bash limit (#529). `wb start` checks what would refuse
  it: an existing branch, a branch clash (`gitops.branch_clash`, #792), case-only path clashes on
  a case-folding file system (`gitops.case_clash`, #797), unfillable submodules
  (`fill_submodules(dry=True)`). A failed checkout or a `--copy` path the base doesn't ignore
  (#322) fails the task before round 1; one cut short is made again from nothing, and discard
  lifts git's "initializing" lock (`gitops._unlock_cut_short`). The branch is the task's only per
  status `branch_made` (written before `worktree add`, only when none exists): it is deleted only
  via `delete_branch(at=branch_made)`, never one moved or checked out elsewhere
  (`checked_out_in`, #930; discard says why it kept one, #416). `feedback`/`next`/`resume`
  without a worktree refuse (`cli._need_worktree`), except `resume` of one interrupted before
  round 1 (`cli.INTERRUPTED` or `runner.UPDATED`: `cli._interrupted`, `cli._rechecks_out`, #788).
  The user's checkout is never touched. The git dir goes in
  `rules/<task>/gitdir.json` before any agent runs. Submodules aren't checked out: a `.git` file
  could point at a git dir whose config git obeys outside the lane.
- **Not the tests or the setup**: the runner does the setup (`--setup`, `--copy`, `--sibling`,
  commands.json `repos`: *Setting the worktree up*), then the base check (*The base check*), so a
  slow suite can't strand a task (2026-11-16).
- It writes `task.json`, round 1's prompt (the spec plus `cli.PROTOCOL`: git read-only,
  `cli.test_line` (never weaken a test, not the whole suite unless asked: #127), the
  `ask_reviewer` budget, scope, the writable places, a closing summary and `Lesson:` lines), the
  status, `rules/<task>/grants.json` (`--allow`, `--allow-path`), spawns the runner, and prints
  the model and why, the slice, the spec record, skills and `next: wb wait <task>`.

**Waiting for a server that is down** (`models.may_come_up`): `runner._wait_up` waits before
taking the lease, pauses doubling up to a minute (`DOWN_PAUSES`) for `DOWN_WAIT`
(`models.START_TIMEOUT`, 20 min), status `last` "waiting for <id>: …; wb stop gives up"; then
fails. A load under the lease that fails with the server down waits the same way
(`runner._load`). A configuration error fails at once.

**3. The runner takes the GPU** (`models.GpuLease`, *Sharing the GPU*): beside others when the
models fit, else queued first come first served; no guard counts the wait. It unloads other
Ollama models (not those of tasks beside it), stops servers with a stop command, loads its own,
again before each round; the lease is held until exit. A served window
(`models.served_window`) below `limit.context` fails the task (`short_window`; #248: Ollama
restarted without `OLLAMA_CONTEXT_LENGTH` trimmed prompts silently). A server that doesn't say
passes. An API task takes no GPU lease (`models.lease_for`: `NoLease` for `Model.kind == "api"`,
else `GpuLease`): it holds one of its provider's slots, `$WB_HOME/api/<provider>.<i>.lock` (an
`flock` on each of `API_PARALLEL` files, the holder's name written in it; the kernel drops the
lock when the runner dies, SIGKILL too), waiting as `phase: queued`, `last: waiting for an API
slot: 3 tasks run on <provider>` on a watch of the slot files when all are held, and looking
again after `models.API_WAIT` (2 s, doubling to 30 s) when no file event comes. The provider is
`api_provider(m)`: the model's provider, else its id before the first `/`. `api_slots_free` and
`api_holders` read the slots for `wb start`'s refusal and the notes; their probe takes a free
slot `LOCK_SH|LOCK_NB` and lets go, so two probes at once don't read each other as holders (with
`LOCK_EX`, a start beside another start's probe saw every slot taken and exited 4, the review of
#1015), while a holder's `LOCK_EX` still refuses it. It never
appears in gpu.json, loads and unloads nothing, has no `short_window` check and no server-down
wait (`runner._down` is False), runs no `_Power` sampler and no keep-warm thread, and its first
status says `last: starting`, not `waiting for the GPU` (`runner._gpu_side`: from the config
alone, in `drive`'s first write, before `models.resolve` may read the catalog; a GPU task showed
the last run's line meanwhile, the review of #1015). Its tests run under
this machine's pool as any task's (`_tests_gpu`, `--unload-for-tests`). The bench counts it as
no GPU work (`bench._idle`, `bench.api_task`).

**4. The agent works a round**: `opencode run --standalone -m <model> --auto --format json
--title "wb: <task>" [--session <id>] -- <prompt>` in the worktree, in the lane, `$PWD` the
worktree, `OPENCODE_CONFIG` the layer. Later rounds continue the session until a handover.

**5. Guards:**

| Guard | Default | Set by |
|---|---|---|
| the same tool call in a row (its input hashed, cut to 400 characters, #497) | 4 | `events.StuckDetector` |
| failing tool calls in a row | 6 | |
| calls since the last edit to the task's files (outside the worktree doesn't count: 2026-11-18) | 40 | |
| no output (not while waiting on Claude; a background job gets the test timeout if longer) | 15 min | `--silence-timeout` |
| one round, waits on Claude excluded | 40 min | `--round-timeout` |
| mid-round handovers in one review round | more than `HANDOVER_MAX` (5) | `runner.drive` |
| an API task's spend: today at the day cap, or the keyhole refused a request that could pass it (`spend`, *Spend*) | routes.json `day_usd` | `runner._spend_guard`, `runner._go_on` |
| OpenCode alive after closing its output (#83): SIGTERM, then SIGKILL | the silence limit, at most `EXIT_GRACE` (30 s) | `runner.run_round` |

Past the think budget (7 min, `--think-timeout`, 0 off; from the round's first output, paused
on a background command) the runner interrupts with `runner.NUDGE` and raises a check-in; the
next round that stalls again is stuck. Guards use the monotonic clock. Defaults were set on a 27B
at ~36 tokens/s; slower models get longer ones, never shorter (*Budgets and speed*).

**6. The tests gate the round.** After a clean round the runner runs `--test` in the worktree
(`--test-timeout`, 900 s; `rounds/<n>/tests.txt`). A failure with auto-fix left (`--auto-fix`,
2, counted since the last review round or step) goes back as `auto_fix_prompt` (fix the code,
don't weaken tests, check its own expected values by hand, never blame the compiler: 2026-11-18)
without waking Claude. `not_the_code` (#496: a timeout, a lane-refused device, `lane_refused`,
#331) gets none: review, status `last` the cause, the report led by `auto-fix skipped: <cause>`
with the fix (`wb allow --path` of `lane_grant`'s grant, `shlex.quote`d, #670). Except `hang`
(tests `hang {base, limit}`): a timeout where the base check passed the same command in
`HANG_SHARE` (a quarter) of the limit or less is the change's, sent back saying so (#779). A
dropped proxy (`proxy_gap`, #380) still auto-fixes, told not to change code for it; with tests
`proxy` or `lane` set the prompt drops "the bug is in this change". A note arriving during the
tests goes first and that result is dropped.

Self-review (the `self_review` knob, *Self-tuning*; `--self-review`/`--no-self-review`, task.json
`self_review`): the first pass in a review round (status `self_reviewed`, the `[steps, reviews]`
it ran at) gets one more round, `runner.SELF_REVIEW`,
Claude's own review checks (each rejection proved by breaking its check and seeing a test fail).
On by default because a feedback round costs a whole wake (2026-11-18); a model it doesn't help
learns it off.

**7. Claude is woken** with `runner.summary_text` (*The round report*) and a notice; the runner
exits.

**8. Claude reviews** the whole diff (`wb diff`), then:

| Command | What happens |
|---|---|
| `wb accept <task> -m "…"` | commits on its branch, removes the worktree, runtime directory and rules, optionally `--merge`s (deleting the merged branch) |
| `wb feedback <task> --message <file>` / `--text "…"` | exact changes as the next round's prompt (the skill allows three review rounds) |
| `wb next <task> --spec <file> -m "…"` | commits the step and gives the next to the same agent, worktree and (with room) session |
| `wb discard <task> --reason "…"` | stops it, removes worktree, runtime directory and branch (`--keep-branch`) |
| `wb relocate <task> --repo <dir>` | after the repo moved: repairs the worktree's link |

Each closed task leaves a `results.jsonl` row the router, slices, tuning and skill records learn
from. `wb start --trial` (or `wb trial <task>…`) marks a check of workbench itself: kept, but left
out by `store.results()` and the lessons.

**What a task cost Claude** (`spend.py`), read at accept or discard from the session's
transcripts (task.json `session`): `${CLAUDE_CONFIG_DIR:-~/.claude}/projects/*/<session>.jsonl`,
its `subagents/`, and its process's later sessions (`spend.sessions`: a /clear, #451). Every `wb`
call notes its session under `claude-sessions/<CLAUDE_PID>` below a `start` line
(`store.note_claude_session`; read for task.json `claude_pid` under its `claude_start` by
`store.claude_sessions(pid, since, start)`: a reused pid mustn't take a dead process's sessions,
#787), and in the task's own `claude-sessions` when it names the task (`store.note_task_session`:
adopted tasks, #719). Usage is per API call, once per `message.id`, charged only to that
session's tasks:
- a call whose Bash runs `wb … <task>` (split with `shlex`) is that task's, and so are the turn's
  later calls, until a `wb` names others, `wb accept|discard` closes it (`CLOSING`), or an edit
  (`EDITS`) to a file outside the worktree not handed to `wb` (`HANDED`: `--spec`, `--message`,
  `--file`);
- the calls before a turn's first such `wb` are its tasks', back to the last call that ran any
  `wb` or edited another file (82K of other work once, 2026-11-17); a turn a wake opened (`WAKE`:
  the mod's `` [workbench] Task `<name>` `` at the prompt's start, after Claude Code's "The
  workbench plugin sent a message:" line) is the task's whole; a turn naming none is no task's. A turn starts at a
  user row that is no tool result, `isMeta` or `isCompactSummary` (#557);
- a call naming several tasks is split evenly;
- a subagent transcript is its own thread under the same rules, in the turn of the session call
  before it (bisection, #728); one naming no task is charged as that call (#718).

Charging whole turns was wrong (2026-11-17: 5.5M cached tokens of other work on one task). The
row keeps `input`, `cache_write_5m`, `cache_write_1h`, `cache_read`, `output`, `turns`, `calls`;
`spend.weighted` gives input-equivalent tokens (`WEIGHTS`: output 5, cache write 1.25 or 2 for an
hour, read 0.1), which `wb accept`/`discard` (beside the estimate, `savings.line`), `wb stats`
and the router (`_cheaper`) use. Nothing
to read: the row has no `claude`, never a zero (#718: zeros skewed `_cheaper`).

**Each step's share** (`spend.usage_by_step`, #558): `steps[i].claude`, so a one-step bench case
is set against its step. Split at the task's `next_step` events, except a `wb next <task>` call
and its spec's calls (`calls` marks them `ahead`), which belong to the step it hands over; a
failed `wb next` hands nothing (clamped). No split when a charged call lacks a timestamp or the
step counts disagree.

### What a task saved: an estimate of Claude writing it
`savings.py` (the user, 2026-11-18: nothing showed the saving). Fitted on 8 paired runs
(2026-11-17): `SELF_CALLS` 4.9 + lines changed / `SELF_LINES_PER_CALL` 69 calls, each priced at
this task's own cost per call less output (`per_call`), plus `SELF_OUT_PER_LINE` 36 output tokens
a line. Rechecked 2026-11-19 on five `wb bench self` runs: mean error 19%, mostly low; a refit
didn't hold out of sample, so the constants stand (#120); single tasks −36% to +48%, always
called an estimate. `estimate_self(lines, per_call)` is that sum; `of_row(row)` needs `claude`
(with `calls`) and `diff`, gives `{claude, self_est, saved, wh}`; a discarded task saves nothing
(`self_est` 0, `saved` −claude). Lockfiles and generated files (`gitops.generated`:
`gitops.LOCKFILES`, `linguist-generated` or `-diff`) are out of `diff`'s lines, and whole
deletions price as one call (#725: a deleted 2,400-line module "saved ~725K"); `savings.COUNTS`
says so. `line(row)` is the per-task line; `totals(rows, since)` sums them, `of` counting the
period's closed tasks, the energy's `cost` at `price()` (`$WB_SAVINGS`, else `savings.json`
`kwh_price`; `set_price` through `store.edit_config`). `wb savings` prints `summary` (`PERIODS`, default a week): `the last
7 days: 3 task(s) · claude 160K · writing it yourself ~390K (estimate) · saved ~230K (59%) · GPU
1.2 kWh, cost 0.36`.

**The badge** (`savings.score`, #101): 100·(B−A)/B over the summed `self_est` (B) and `claude`
(A), rounded, held to 1–100; `BANDS` 1–9 red, 10–24 orange, 25–49 yellow, 50–100 green; the
sentence "I used about 62% less Claude than if Claude had done this work alone", "about the same
amount" or "about 20% more" (both score 1); else `NO_SCORE`. Energy and price stay out. `wb
savings` ends `score N/100`, `--json` carries `{score, color, pct, sentence}`, the web chat's gear
shows it; `wb savings --card FILE.svg` (`savings.card`) writes an
SVG (`CARD_COLORS`, each past 4.5:1) holding no name, path, host or account.

The energy: the runner's `_Power` thread samples `hardware.gpu_power_w()` every `POWER_EVERY`
(10) s of a round into status `gpu_wh` (`runner.measures_power`: not a remote model, `remote_gpu`;
not `WB_FAKE_MODELS`; not an API task, which runs on no GPU of ours, #1022),
on the monotonic clock, a gap past `FROZEN_SECS` (60 s) adding nothing (#559: a suspend added 1.2
kWh), each reading split over `runner.sharers()` (`models.holders`, #706: two sharers counted
the GPU twice); `gpu_w_*` stay the GPU's own draw. The size: `gitops.footprint` (`files`,
`lines`, `removed`, apart `generated` and `deleted`), per step (`results.step`), summed as the
row's `diff`.

**API tasks: their own line, never in the score** (#1022). The score answers `savings.QUESTION`,
"How much Claude did my GPU save me?": `of_row` returns None for a `kind: "api"` row, so `totals`,
`score`, `card`, `share`, the milestones and the badge stay GPU-only (that check is what keeps
them so), and `of` leaves API rows out. An API task's paid tokens are on the other side, which the
score can't see: it would read "saved 60%" for a task that cost $3. Its money goes beside it,
never netted against Claude's tokens. `api_totals(rows, since)` sums the API rows, trials and
discards included (money spent is spent): `{tasks, cost, unknown, plan, trials, claude, self_est,
by_provider: {p: {tasks, cost, unknown, plan?}}, by_day: {YYYY-MM-DD: $}}` (`claude`/`self_est`
side by side through `_compare`, of_row's comparison of any kind). Dollars are OpenCode's unit
(`Money.USD`), printed with `$` (`savings.usd`: `$0.42`, a cent's fraction to two figures written
out, `$0.0041`, `$0.00004`, never `4e-05`, and 0.009999 as `$0.01`; a free model's `$0`; the mod's
`usd` and the chat's `apiSaved` follow the same rule); the electricity's cost keeps none (#609, #845). A task with no price counts as `N
with no price`, never `$0.00`; a plan model's (`opencode-go`, an Ollama cloud model) as `on plan
usage`. `line(row)` gives an API row `api_line`: `claude: 56K · writing it yourself: ~59K
(estimate) · paid DeepSeek $0.12 (deepseek/deepseek-flash)` (the name from hosted.json,
`provider_labels`), `· paid: unknown (no price for this model)` or `· plan usage (opencode-go)`;
so `wb accept`, `wb discard` and `wb backlog` print it. `wb savings` adds, when the period has API
rows, `api: 6 task(s) · $1.27 (deepseek $0.84, anthropic $0.43) + 1 with no price · claude 210K ·
writing it yourself ~520K (estimate)` (`api_summary`); `--json` gains `api` (api_totals), which the
pane's footer and the chat read. **`wb savings --api`** (`api_report`, with `--week`/`--month`/`--all`):
`api, the last 7 days: 6 task(s), $1.27, 1 with no price (incl. 1 trial)`, then `by provider:`,
`by day:` and `bench replays: $0.30 (2)` (`bench.paid_since`, only when there are any: a replay
writes no results row); `--api --json` is api_totals plus `bench`. The first line names no
provider (`_counts`): the `by provider:` line does.

**What an API task cost** (#1022, `results.api_usage`, at its close; `bench.paid` for a replay).
`kind` is task.json `via` (`results.task_kind`: anything but `"api"` is GPU), `provider` task.json's
or the id before its first `/`. Plan usage first (`_plan`): a model of `PLAN_PROVIDERS`
(`opencode-go`, the meter's list, `meter.PLAN_PROVIDERS`) or one the catalog flags `cloud` (an Ollama cloud model, #1013; read through
`models.lookup(id, cached=True)`, never the name: any provider's model may end in `-cloud`) is
`source: "plan"`, `cost: null`, `plan: <provider>`. Else the sessions, every id in status `sessions`
plus `session` (handovers, revives and `wb next` each start one), each exported
(`transcript.finished_export` for the earlier ones, `export` for the current one); a session's cost
is its `info.cost`, which counts OpenCode's title request that no message carries (real money:
it wins over its assistant messages' `cost` summed, the fallback; `_session_cost`). Every
session's sum is `source: "opencode"` when above 0, or 0 for a model the catalog says is free,
unless the runner's meter counted more: then the meter, `source: "metered"` (it adds the
provider's own `usage.cost` and the usage of answers a steer or stop cut short, which OpenCode
doesn't bill; the row is never below the ledger the day cap counted nor the round report);
OpenCode's 0 for a model the catalog prices (`catalog_price`: hosted.json as it is, `None` with no
price, `0.0` when the model is `free` or every price is 0, a tier's `size` not counted, else `1.0`)
isn't trusted. The meter is the runner's (`metered`: status `api.usd`, #1019's), else the step
events' summed `cost`: the
whole task's, a floor (no step carries the title or compaction requests). When some sessions
exported and some didn't (or had no cost), the exported ones' sum plus the meter's remainder
(the meter less that sum, never below 0) fills the rest, `source: "mixed"`: dropping the whole sum
for one failed export threw away what the others said (the engineer's call, #1022). With no meter
to fill from, that sum would be a false, low figure, so it isn't used. Then the meter alone, `source:
"metered"`. Then a free model's 0, else `source: "unknown"`, `cost: null`, never 0. `tokens` sums the step events (`cache_read`/
`cache_write`, a row from before the split by its `cache`); `backend` is status `served.by`
(OpenRouter's served provider, the keyhole's), the last seen. Live, `results.live_spend` is the
meter, else the steps' summed `cost`, else 0 for a free model, else unknown (`spend_so_far` for the
round report, every step of events.jsonl; `wb top` keeps a running sum, below).

**Sharing it** (`wb savings --share`, `savings.share`, #119): a summary to paste, nothing sent (the user posts it, if they choose; Claude never posts it, a bench report or a badge anywhere without the user's yes):
the machine (`hardware.system()`, `hardware.gpus(windows=False)`), the models' count and weights
(`models.weights`), the tokens and estimate overall and by size, `COUNTS`, the badge, the energy.
No model name, repo, task name or price.

### Spend: what an API task costs
`workbench/meter.py` (#1019). Nothing recorded money for the agent's model, so an API task could
spend without limit unseen, and a cap cut mid-stream became OpenCode's retry loop until the silence
limit (robustness M1). Now every API task is metered live into one ledger, and one day cap covers
every API provider together: routes.json `day_usd` (the user's, #1014; read by `meter.cap`
through `routes.read()`, `$WB_ROUTES`, again only when the file's mtime or size changes), for the
local calendar day, reset at local midnight. A GPU task writes nothing here and costs nothing: `meter.for_task` is None for it (a
task.json without `via` reads as GPU, as every task before #1016 does; drive hands it the model,
else the cached catalog's, `models.lookup(cached=True)`).

**The price** (`meter.price(m)`): the catalog's `cost` from hosted.json (`Model.config`: a list of
`{input, output, cache{read, write}, tier?}` per million tokens; a config model's `{input, output,
cache_read, cache_write}` too), lowest tier first. Missing is None, unknown, never 0; every field 0
is a true 0 (a free model: `meter.is_free`, also OpenCode's `opencode/*-free`, `Model.free`, with no
price listed; a listed price that isn't 0 is capped whatever the name says). A free model's rows
are booked, but the cap has nothing to keep: `refusal`, `stop_reason` and `room` are None for it,
with or without a cap. `meter.metered(prices, tok)` prices tokens the way OpenCode prices
itself (the spike, *API models through OpenCode*): `input` without cache reads, cache reads and
writes at their rates, output and reasoning at the output rate (a listed `reasoning` rate wins),
the context tier past its `size` by input plus cache reads plus cache writes (2.3.2's own rule,
from its bundled source: without the writes, a first turn caching a 150K context priced at the
base tier, 5 times low on a long-context price). The issue's first design priced every
input token at the cache-miss rate; with a 100K cached context that metered 50 times the real
cost, and OpenCode's own figure is this same catalog estimate, so the meter prices as it does.
Never under where the split is unknown: a usage that doesn't name its cache reads prices them as
input, and a missing cache price is the input price.

**Two views, the higher kept.** The keyhole's (`Meter.on_usage`, from the keyhole's thread): each
upstream response's `usage`, merged into one per response (`keyhole._merged`: Anthropic's
`message_start` and `message_delta` each carry part; some servers send a running total in every
chunk) and handed over before the response's last byte, so OpenCode prints that step's
`step_finish` only after it is booked. `meter.usage_tokens` reads Chat Completions
(`prompt_tokens` with `prompt_tokens_details.cached_tokens` or DeepSeek's
`prompt_cache_hit_tokens`, `completion_tokens_details.reasoning_tokens`), the Responses API
(`input_tokens_details`, `output_tokens_details`) and Anthropic (`cache_read_input_tokens`,
`cache_creation_input_tokens`). It sees every request the model gets, titles and compaction too
(which no `step_finish` carries), and a provider's own `usage.cost` (OpenRouter's) wins over the
catalog (`source: provider`). OpenCode's (`Meter.step`, `step_finish.part.cost`, which
`events.normalize` keeps as the step row's `cost`): with the keyhole metering, only a `reconcile`
row when OpenCode's sum for the round passes the keyhole's and the round's earlier reconciles;
without it (answers with no usage, the suite's fake), each step books its own row, OpenCode's
cost (`source: opencode`) or the metered one (`metered`). At each round's end (`runner._spend_end`,
after `measure_speed`) the task's sessions' exported `info.cost` (status `sessions` and `session`,
each once; `transcript.finished_export` for old ones), which counts the title request, adds one
`reconcile` row when it is more than the ledger holds for the task. Never less: what the keyhole saw
is what went out, so the ledger is the higher of the two. **Never a false 0:** an OpenCode cost of
0 for tokens whose price isn't 0 is not trusted (the step keeps the metered figure, `source:
"metered (opencode said 0)"`; an export saying 0 reconciles nothing), and an export that failed
reconciles nothing. What isn't counted: a response cut before its usage arrived, by either view.
Without the keyhole's view (a model `runner.needs_keyhole` starts none for: an Ollama cloud
model, a keyless free one), OpenCode's compaction requests count only as far as its `info.cost` does. A catalog price below a backend's
(an OpenRouter endpoint priced over its listing, without `usage.cost`) meters under.

**The ledger**, `$WB_HOME/spend-api.jsonl` (`store.append_jsonl`, locked; *Files and formats*):
`step`, `reconcile` and `checkin` rows, each with its local `day` (`meter.day`: a round past
midnight books its later steps on the new day). `meter.today()` gives `{usd, unpriced_tok,
plan_tok: {provider: n}, checkin}` from today's rows, read on from the last offset (the file's
inode and size tell a new or rewritten one), so the 5 s tick never re-reads it whole. Status `api`
(`{usd, metered, source, tok}`, per row through `Task.set_status`, under the meter's lock: the
keyhole's thread and the runner's book at once, and a total written after a newer one would leave
it under the ledger) is the task's own; its results row's `api` reads it at the close
(`results.metered`, *What an API task cost*).

**The cap at start** (`meter.refusal(m, role=None, advice=True)`, exit 3; `routes.resolve` on both API paths:
a `--model` before the dialog, and `cli._route_start`'s dry run too, a role's API model before it starts): an API model that isn't a plan or free is refused with no
price (`<id>: OpenCode's catalog gives no price for it, so the day cap can't be kept`) and at the
cap (no cap at all is #1014's to refuse for a role; a task that meets none, a routes.json edited by
hand, is stopped by the guard at once, `meter.NO_CAP`, and its keyhole's room is negative, so it
spends nothing rather than without a limit; a routes.json that can't be read stops it the same way,
saying so, `meter.UNREADABLE`, `routes.json can't be read (<why>), so the day cap can't be kept: wb
route shows it`, never NO_CAP's advice, `meter.no_cap`; an unpriced model that started anyway is
stopped as well, its room negative and `stop_reason` the NO_PRICE line): `trivial role: API spend today is $5.03 of the $5 day cap (it resets at local midnight).
Ask the user: a higher cap (wb route cap <usd>), the role on the GPU (wb route trivial gpu), or
tomorrow` (the busy opt-in's note, `advice=False`, ends at `midnight)`: its role is on the GPU, the start only
waits). `meter.tail(m)` is the start line's part, ` · today $0.42 of $5`, or a plan's ` · plan
usage (OpenCode Go): 1.2M tokens today`, which wb start's model line ends with (`cli.cmd_start`).

**The guard `spend`, inside a round** (`runner._spend_guard`, each tick after the stuck checks, an
API task only): status `api_stop` (`{guard, text}`, written by the keyhole's side: the room's
`spend`, or its `quota` stop, #1020's, the guard taken as written; `_ProviderWait.stop`, earlier in
the tick, consumes it first; cleared at each round's start, below) or `meter.stop_reason(task)`, today's spend at the cap (`the day's API spend
cap is reached: $5.02 of $5 today`, or no cap at all, `meter.no_cap()`: `NO_CAP`, or `UNREADABLE`
for a routes.json that can't be read; or no price, `<id>: NO_PRICE`). It calls `CURRENT["keyhole"].close(why)` first, so every
request from then on gets a 400 with `{"error": {"message": "workbench: <why>"}}`, nothing sent
upstream (`keyhole.Keyhole.close(reason)`; with no reason it shuts the server down as before),
then sets `out["stuck"]`/`out["guard"]` and `_stop`s OpenCode, which ends a response still
streaming: the round stops within one tick of the crossing step, never at the silence limit.
OpenCode 2.3.2 doesn't retry that 400 (`provider.invalid-request`, exit 1 at once, through the
keyhole, both protocols: `ApiSpendKeyhole`'s real-OpenCode check); were the 400 retried after all, the next tick (5 s)
stops it from the ledger. **The room** (`Meter.room(body, size)`, the keyhole's `room` hook before
each request): the cap less today less the request at its worst (its body's length / 4 at the
dearest tier's input price, its `max_tokens`, `max_completion_tokens` or `max_output_tokens`,
else `models.API_OUTPUT`, at the output price). Negative: refused the same way, and status
`api_stop` `{guard: spend}` stops the round on the next tick. Parallel tasks overshoot by at most
one worst request each. `Meter.begin(n)` clears an old `api_stop`, whatever its guard, as each
round starts (a resumed task begins clean). **Between
rounds** (`runner._go_on(task, n, what)`, #1017's helper, before each `continue` in `drive`: the
handover, Claude's notes, the revive, auto-fix, self-review): at the cap, phase `stuck`, status
`guard` `spend`, the keyhole closed, and a `stuck` notice ("… <what> didn't run"), with round n's
report when it has one (`Round <n>'s report (wb report <task> --round <n>):`: auto-fix and
self-review have already moved status `round` on to n + 1, which never ran). Both notices
have `cause` `spend`, not the step's own (`results.THE_STEP`), so the slice isn't shrunk, and lead
with `meter.stop_text`: `<task> stopped: the day's API spend cap is reached ($5.02 of $5 today). Its
work so far is in the worktree. Ask the user: a higher cap (wb route cap <usd>), the role on the
GPU, or tomorrow; then wb resume <task>.` The mod's `GUARD_WORDS.spend` names it, and its wake for a
`cause` `spend` notice ends with `SPEND_NEXT` (ask the user, then `wb resume`), not the stuck wake's
feedback line: feedback would run a round the cap stops again at once.

**The check-in at half** (`CHECKIN_SHARE` 0.5): a priced row that leaves today at or past half the
cap, with no `checkin` row today, appends one (under `spend-api.jsonl.checkin.lock`, so tasks in
parallel add one) and raises `checkin`: `API spend today has passed half the day cap: $2.51 of $5
(<task> $0.88 so far). Nothing to do unless the user wants to change it.` Once a day across every
task.

**Plans** (`meter.is_plan`: `opencode-go/*`, an Ollama cloud model): a quota, not dollars. Their
rows have `usd: null`, `plan: "<provider>"` and the tokens; they never count against the cap, never
stop for `spend`, never reconcile, and `today()["plan_tok"]` sums them. Their quota errors are #1013's
and #1020's.

### Kept awake

Idle sleep froze overnight runs unseen by the monotonic guards (#516). Everything `cli.detach`
starts unattended (runners, queued ones too, the bench driver, `wb optimize`; not the chat
server) holds an inhibitor tied to its pid (`detach(…, awake=<why>)`, `cli.keep_awake`):
- **macOS**: `caffeinate -i -w <pid>`.
- **Linux with systemd**: `systemd-inhibit --what=idle:sleep --mode=block` around `AWAIT_PID`;
  where polkit refuses a sleep lock (SSH) `--what=idle`. It runs in the caller's session, not
  `runner_scope`'s, for that grant, so under `KillUserProcesses=yes` it ends at logout.
- **WSL2**: Windows sleeps regardless of Linux, so (`awake_how` "windows": `context.interop_on`,
  `hardware.powershell`, #777) `AWAIT_PID` pipes into `powershell.exe … WINDOWS_AWAKE`
  (`SetThreadExecutionState` as 2147483649: PowerShell reads `0x80000001` as a negative Int32),
  which holds until the pipe closes; idle sleep only (`WINDOWS_AWAKE_SAID` in `wb doctor`).
  Without interop or powershell.exe, `WSL_SLEEPS` in `wb doctor` and on a `--backlog` batch's
  first task.
- Elsewhere: nothing; `wb doctor` says so.

## The runner, inside

`workbench/runner.py`, started as `python -X utf8 -m workbench.runner <task>` by `cli.spawn_runner`
(cwd and `PYTHONPATH` the repo root, the caller's in `WB_USER_PYTHONPATH`, its own session, output
and stderr to `runner.log`; `spawn_runner` drops `WB_TASK` from it, the mark `sweep` kills by).
SIGTERM stops the process group in flight and raises `Stopped`, a `BaseException`, so no `except
Exception` swallows a stop. wb's own stops mark status `stopping`
first (`cli._stop_runner`: a `stopped` event `by: reviewer`). A SIGTERM without the mark (shutdown,
logout, `kill`) while the phase is live logs `by: signal` and sets `stuck` with `SIGNAL_STOP` ("the
runner was stopped by a signal (…), not by Claude"; status `signalled`) and a `stuck` notice, so the
task is resumable. A shutdown signals every runner at once (one wake a task, #544): `_signal_notice`
waits up to `SIGNAL_SETTLE` (12 s) while another task of its session (`store.owner`) still runs,
then under `locks/signal-stop.lock` raises one notice naming them all (`tasks`, `cause` `signal`,
`wb resume <a> <b> …`, each `steps_line`), acknowledging the rest (`store.ack_batch`); a runner
already named raises none, and one stopped alone keeps its own (`wb resume <n>`). Any other
exception: traceback to `runner.log`, `failed` with its tail. On exit `_end_agent` runs once more;
`pid` and `stopping` are cleared.

**An update under a running task** (#603). The runner loads once, but each round's children (lane,
git shim, ask server) load from the clone afresh, so a mid-task pull paired old and new. `main`
records `code_print()` (sha256 of `runner.CHILD_CODE`) in `CURRENT["code"]` and imports `router`,
`transcript`, `verify` up front; before each round `_updated` compares and on a change sets `stuck`
("workbench was updated under this task: wb resume <task> continues it on the new code", `cause`
`updated`, not held against the step's size, #660); `wb update` refuses while a runner is live
(unless `--stop`), so a manual pull is what this catches. `opencode_moved` (before the lease and
each later round) compares status `opencode` (`M.m`, set by start and by feedback, next, resume
through `cli._opencode_again`) with `opencode --version`: changed, `stuck` with `OPENCODE_MOVED` ("OpenCode was changed under this task (2.3, now
2.5): wb resume <task> continues it on 2.5", an interruption to `wb resume --all`); below 2.0,
`OPENCODE_GET`; `cause` `opencode` (#866: a 1.x reinstall failed every round as the model's fault).
Feedback, next and resume refuse 1.x (exit 3) and note one outside `OPENCODE_TESTED`.

**`drive(task)`**, the loop:
1. Phase `queued`, `pid`, `boot`, `machine`, `host`, `beat` (touched every `BEAT_SECS`, #872);
   resolve the model (unknown: `failed`). With `setup`/`copy`/`siblings` and no status `prepared`: `_prepare`. With
   `--test` while `needs_base_check` (no `base_check`, step one, no review round, no
   `rounds/<n>/summary.md`, not `no_base_check`; gated on round 1, a resumed task skipped it,
   #510): the base check, before the GPU. Setups and base checks take machine-wide turns, a queue
   each (`prep_turn(task, kind)`, `PREP_KINDS`, `PREP_SLOTS` 1, by `created_at`;
   `locks/prepare{,-check}.json`, flock slots `locks/prepare{,-check}-<n>.lock`), `last` "waiting
   for <task>'s setup" / "base check" "(one at a time on this machine)": unqueued, N tasks ran N
   installs at once (#590); one queue held a 5 s check behind a 25-minute build (#868). A base check
   needing the GPU takes the GPU first, its turn only around the run (#679). Round tests take no
   turn. Probe on: round 1's outline (`add_outline`) next.
2. One `GpuLease` holds for the rest, stepping aside (`lease.step_aside`) where no GPU is needed:
   the tests, and waiting for a down server (#257). With `--unload-for-tests` (task.json
   `unload_for_tests`) the tests keep it as a modelless `gpu.json` entry with `tests: true`, so
   nobody joins (`admits`), `wb start` refuses (exit 4) and queued tasks wait (#428); sharers are
   waited out first (`last` "waiting for the GPU (held by …): the tests use it alone",
   `GpuLease._alone_for_tests`, ordered by `since`, so two tasks' GPU tests never overlap), then
   `_tests_gpu` frees every model on the GPU, its own too (`free_gpu` with no `keep`: only
   `keep_loaded` kept, then `media.settle`; servers with a `stop` are stopped and restart on the
   next load, up to `START_TIMEOUT` a round, #943); a model on a wb remote box still has its tests
   here, on this machine's pool taken alone (`verify.gpu`). An unload is decided under the
   `gpu.admit` lock before the lease goes (#327). Phase is `queued` from before `step_aside` to
   `run_tests`'s `testing` (left `running`, `wb steer --now` promised an interrupt it couldn't
   keep, #943). `_rejoin` (phase `queued`, "waiting for the GPU") retakes it
   first-come by the task's start; a review end doesn't rejoin. Scratch-checkout checks (base
   check, `wb verify`, `wb shot`) take no part of the lease; with `--unload-for-tests` each takes
   the GPU alone (`verify.gpu`, #502). `lease.load()` adds to `status.load_secs` (never overwrites,
   #567), `limit.context` to `status.window`, and `tuning.tune` sets `status.tuned` once. A load
   error: `failed`.
3. Each round n: after the first, rejoin and `lease.load()` again (Ollama's keep-alive resets per
   request; a reload adds to `loads`, a `load` event). Pending notes go into `prompt.md` with
   `NOTE_HEAD` (`_early_notes`, #452). For n > 1 `fresh_reason` may start a fresh session
   (`_start_fresh`); a one-round `keep` passes to `run_round` and is cleared. Phase `running`,
   `round_started_at`, a `round` event, `run_round`, `_end_agent` (while the lease is held), then
   `measure_speed`.
4. After the round, in order:
   - full → `_full_round`, unless already `HANDOVER_MAX` (5) handovers: stuck ("handed over to a
     fresh session 5 times in this round without finishing: the step is too big for this model's
     window…"; "without finishing (the last when OpenCode's compaction failed: <its message>): …",
     #969).
   - interrupted, or a note pending and not stuck → `_steer_round`.
   - non-zero exit or a silence stop while the server is down, whatever the model said first
     (#657) → a `load` event, the wait (*Waiting for a server that is down*), the round again,
     once a round, same session. A server wb starts (models.json `start`) that stopped
     (`_stopped`) is restarted by the load (#579). Gone again: `infra` `model`, `cause` `server`.
   - stuck, or `died` (any non-zero exit, whatever it printed, #427; not `lingered`) → the report
     without tests (plus "NOTE: not delivered, the round stopped first: …", `_undelivered`),
     `stuck`/`failed`, a notice, exit; edits stay.
   - else `run_tests` (a note arriving meanwhile → `_steer_round`), the report to
     `rounds/<n>/summary.md`. Failing with auto-fix left, unless `not_the_code` (a timeout, a lane
     refusal) → an auto-fix round.
   - else phase `review`, a `review` notice: the report, `review_diff`, the agent's proposed
     instruction files (*Claude's files*); exit.

`review_diff`: the whole diff up to `REVIEW_DIFF_MAX` (32,000 characters; each fetch re-reads
Claude's session), else its stat, read as it streams (`gitops.diff(limit=)`, #348). After a review
of the same step, only the diff since it (`gitops.snapshot`, a tree in status `reviewed`), an
instruction file again only when changed. Once gc prunes that tree, the whole diff with "the last
review's snapshot is gone (git gc pruned it)" (`gitops.has_tree`, #795). The snapshot leaves bulky
new files out, named ("left out of the review snapshot, too big to store each round: …",
`gitops.snapshot_left`, by count and size before hashing: files of `BINARY_MAX`+, then the
top-level directories with most new files while over `NEW_FILES_MAX`, then the biggest while the
rest pass `SNAPSHOT_MAX` 32 MiB; the next review shows them again), built with `add -u` plus kept files by name: `add -A` filled the user's
object store each round (#431). `wb
verify` takes the tree accept would land (`landing=True`, #642).

Reports and check-ins list 40 changed paths (`gitops.CHANGED_SHOWN`), the rest counted by
top-level directory (a review notice's stat: 40 paths, then Git's total); a new top-level directory of 50+ untracked files is flagged (`BUILD_FILES`).
`wb accept`/`wb next` refuse more than `NEW_FILES_MAX` (300) new files or a new binary of
`BINARY_MAX` (1 MiB) until `--include` (`gitops._bulk`: sizes, `diff`/`filter` attributes, else a
NUL in the first 8000 bytes; LFS-filtered files pass; nothing hashed before it passes, #431; files
and index kept). A file the agent wrote with its edit tools (`runner.agent_wrote`) under an ignored
path (`gitops.ignored_files`) is in no diff or commit (a module under `lib/` was lost, audit round
2 of 1.91): the report says "NOTE: git ignores files the agent wrote (…)" (`gitops.ignored_line`)
and `commit_all` refuses until `--include` (`add -f`). What tests or shell commands wrote there
(build output) isn't counted.

**`run_round(task, n)`.** Writes the config layer and environment (*What the agent knows*),
prepends `skills_note` to the prompt argument (not `prompt.md`), wraps the lane, starts in its own
process group (a stop takes the MCP server), stdin `/dev/null`, stderr to `runner.log`. `_reader` decodes UTF-8 with
U+FFFD and always queues its end marker (a Latin-1 byte once killed it, #440); `_steer_bell` (a
`Watcher` on the task directory and `ask/`) wakes on notes and asks; each tick runs
`ask_mcp.relay`. The loop wakes on a line or every 5 s. Per line: `events.normalize`, the session
id, tool tally, `status.last`, texts, and per `step` the context (input + cache read + cache write +
output + reasoning) against the handover point. Every pass, in order, whatever the line was (a
`continue` past them let unknown events outrun `--round-timeout` and `--now`, #195): asking (phase
`asking`, time not counted), the round clock, silence, the think nudge, a guard stop (a no-edit stop
checks `gitops.mark` first, so shell edits count), a handover, a check-in, a `--now` note
(`interrupted`). `_stop`: SIGTERM to the group, 10 s, SIGKILL. `status.agent_pid` lets `wb stop` end
it; a round ending mid-question clears `asking`. Non-object JSON lines are skipped; `normalize`
reads wrongly shaped parts as empty and stamps a missing `timestamp` with the read time
(#481); `store.loads` turns lone surrogates into U+FFFD (one failed the status write, #438). The
prompt goes after `--` (a leading `-` reads as a flag); OpenCode's `--worktree` isn't used; over
`PROMPT_ARG_MAX` (100K bytes) it is sent as a pointer to `prompt.md` (Linux's 128 KiB argument).

Guard clocks (`_Clock`) use `time.monotonic()` (a laptop shut 20 minutes woke to a silence stop);
a pass over `FROZEN_SECS` (60 s) was a freeze and counts as nothing; `ask_mcp.Waited` has its own
`FROZEN_SECS`: keep them alike. Check-ins use the wall clock, as the files do.
- The think budget starts at the round's first output (OpenCode is silent while the model reads
  its prompt). Silence before it says "no first output in N minutes: the model may be reading its
  prompt slowly…; --silence-timeout gives a new task longer", with the read bound (*Speed*).
- A `retry` line is not output (#491): OpenCode's `headerTimeout` (300 s) and `chunkTimeout` (900
  s) abort and retry, four times, then `error`, exit 1; each retry reset silence (Ollama sends
  headers with the first token, so a slow prompt read never hit the no-first-output stop). Now it
  neither counts nor resets, and takes back what its `step_start` (sent just before a retry)
  moved; `normalize` keeps it (`retry {text,
  attempt}`), and the report says `OpenCode resent N request(s) to the model's server: <error>[; it
  cut the request at the provider's timeout]`. The layer lifts both waits.
- **An API provider's wait** (#1020) is the third pause, beside Claude's answer and a freeze:
  `_Clock.step(t, asking, waiting)` pauses the round's time, silence and the think budget alike
  while `_ProviderWait.waiting` holds (an API task only, `api_task`: task.json `via`, else
  `models.is_api`): status `ratelimit_until` in the future (the keyhole's hold or queue, below), or
  an OpenCode `retry` naming a 429/529, a 5xx, a rate limit or a transport failure, for
  `RETRY_PAUSE_S` (60 s) or to its `nextRetryAt` (the gap before the keyhole's next write; the
  model's own output ends it), and the tick after either ends. So `active_s` and the speed reward
  leave it out, a round with any wait skips `measure_speed`, and `read_bound` records nothing (its
  guard isn't `silence`/`round`). Held past `RATE_WAIT_S` (600 s, the keyhole's `ratelimit.since`, at
  least the round's start), the round stops before any silence check: guard `rate_limited`
  ("<provider> rate-limited the agent's requests for 10 minutes (HTTP 429)") or `outage`
  ("<provider> didn't answer for 10 minutes (HTTP 503)", or the transport's reason); OpenCode
  exiting on such a run sooner (its 10 retries) is the same stop. `queued` never stops a round.
  A `provider.quota` (402) or `provider.auth` (401/403) error, or status `api_stop` (the keyhole's
  quota stop, shared with #1019's spend stop; consumed by the stop), stops it at once: guard
  `quota` or `auth`. `drive` runs a `rate_limited`/`outage` round again once (`_provider_revive`:
  a `load` event "<provider> rate-limited the round: it runs again once the cooldown ends", the
  lease stepped aside, a wait to the provider's `api-health.json` `until`, else `RATE_LIMIT_SECS`,
  at most `RATE_WAIT_S`, a steer note ending it into `_steer_round`, `wb stop` ending the runner),
  continuing the session: one revive a round, as a revive re-sends the whole context, which costs
  money. A second stop, or a quota or key stop, ends it `stuck` with `cause` `provider`, `infra`
  `provider` (a bench replay scores `error`), and `provider_remedy` under the STOPPED line (`wb
  feedback <task> --text "carry on"` once the provider allows, and `wb route <role> gpu (the user's
  yes)` when the task has a role; top up or wait for the plan's reset; /connect for a refused
  key). Its `stopped` event says `by: "provider"`, so it is never a lesson about the agent (the
  same limit on three tasks of one account read as a recurring one). An `api_stop` whose guard is
  `spend` (#1019's day cap) stops the round the same way but is the user's budget, not the
  provider's: `cause` `spend`, `infra` `spend`, `by: "spend"` (`runner.stopped_by`, `SPEND`), no
  revive and no `provider_remedy`; like `provider` it stays outside `results.THE_STEP`, so it never
  counts against the model or shrinks its slice (the engineer's call, 2026-11-23). Each run of waiting is one `rate_limited` event as it ends or the round stops; status
  keys left by a killed runner are cleared as the next round starts, and as each round ends.
- `background_wait` (a shell command past 2 minutes) pauses the think budget and stretches silence
  to max(`--silence-timeout`, `--test-timeout`) (a long suite was nudged and killed every 7
  minutes); the round limit holds. Once the turn is over (last
  `step_finish` reason `stop`) and `BG_GRACE` (120 s, or less with a shorter silence timeout)
  passes, the round is done (a dev server never ends, #658): OpenCode stopped, `left_bg` recorded
  (from `jobs[]`; not `died`), a `left_bg {text}` event, then the tests; the report says `the agent
  left `<job>` running in the background when its turn ended; stopped`.

**Nothing a round starts outlives it** (`_end_agent`, after every round and in `main`'s
`finally`): `_stop`, SIGKILL to the group, then `sweep`: the user's processes whose environment has
the task's `WB_TASK` and `WB_HOME` (OpenCode's shell gives each command its own group; `setsid`
leaves any) and this run's `WB_RUN` (fresh per round, test, setup or verify run: `agent_env(task,
run)`; else a `wb verify` killed a round's agent, #324). With no round id (`main`'s backstop) it
sweeps only runs this runner issued (`ISSUED`, `_issue`, `sweep(task, runs=…)`, #504); a whole-task
`sweep(task)` is for `wb stop`, takeover, accept, discard, `reap`. Linux reads
`/proc/<pid>/environ`, macOS `_sweep_macos` (`ps`, then each PID's `KERN_PROCARGS2` past argv). Why: a leftover writer changed the worktree after
Claude read the diff.

**The handover point** (`fresh_at`, *Self-tuning*). Two checks:
- *Mid-round*, at each `step_finish`: past `fresh_at × window` a fresh session takes over
  (`_full_round`), only when this session changed a file (`gitops.mark`, a hash of the diff and
  untracked files, moved) and
  `status.session_floor` (its context at its first change) sits `HANDOVER_ROOM` (a quarter of the
  window) below the point (seen: twenty handovers with no edit). Otherwise it carries on and a
  `reading_full` event (`_reading_full`, once a round) becomes the report's `reading:` line, waking
  no one. At most `HANDOVER_MAX` a runner, then stuck (2026-11-16 audit: hours of handovers, Claude
  told nothing); only the handover before the last raises a `checkin`. A compaction between steps
  (`compacted_between`, #774: `run --format json` prints none, so a step under half the last
  one's, which was within `fresh_ceiling`'s margin) hands over given a change and room (the floor,
  or the last step's context when nothing changed before it; status `full_compacted`, reason
  `COMPACTED`), else a `compacted` event. A *failed* compaction (`error` type `compaction.failed`, or a message
  opening "Compaction ", `events.compaction_failed`) hands over at once whatever the exit, change,
  `keep` or room: `full` the last step's context (else the window), `compacted` and
  `compact_failed` (the message) set, the `full` event "OpenCode's compaction of the agent's
  session failed (<message>): a fresh one takes over", status `full_compact_failed`, reason
  `COMPACTION_FAILED`; judged before the exit, so the round never fails and tests never run on the
  dead session (#969); past `HANDOVER_MAX`, stuck, and `compact_failed_session` keeps feedback and
  next off it. It counts as compacted (the `fresh` event's `compacted`: `results.step`,
`COMPACT_PENALTY`). `runner._stop` takes
  macOS's `PermissionError` on an emptied group as gone (macOS nightly, 2026-11-22).
- *Between rounds* (`fresh_reason`; `wb next` prints it), first match: `--fresh` or `wb resume`
  → fresh; no session → continue on the step's first round, else fresh ("the last session ended
  before it began"); a session with no status `opencode_home` (begun in the user's OpenCode store
  before v1.91.2, which the agent's OpenCode no longer reaches, #1010) → fresh, `OLD_STORE` ("its
  session is in OpenCode's shared store, which agents no longer reach (v1.91.2)"), even under
  `keep` and on round 1; the `compact_failed_session` → fresh, `COMPACTION_FAILED`, even under `keep`;
  `keep` → continue; `context: full` → fresh (`COMPACTED`/`COMPACTION_FAILED` per
  `full_compacted`/`full_compact_failed`); the export lists a completed compaction (a message `{type: "compaction", status: "completed", summary}`) → fresh; the
  last message with tokens (`context_state`, all-zero ones skipped, #481) at `fresh_at` or more →
  fresh, unless `no_room` (#781: `session_floor` unset or under `HANDOVER_ROOM` below the point, as
  mid-round; not on a step's first round): it carries on, and the report says
  "the next round carries on in this session" (`full_session`). A failed export
  (`transcript.export(sid, strict=True)` None) uses status `live_used`, assumes no compaction, logs
  an `export` event (#262).

**A fresh session's prompt** (`_start_fresh`, `handoff_prompt`). The raw prompt is kept once as
`rounds/<n>/instructions.md` (a rerun rebuilds from it with the first reason, `_fresh_said`, and no
second event, #780; `_early_notes` folds notes into both files). `prompt.md` becomes "You are
picking this task up in a fresh session (why)…", `## The task` (`current_task_prompt`: the step
round's `instructions.md` when the step began fresh, else its `prompt.md`; `store.round_prompt`,
also read by `round_asks`, the lessons and `wb
bench add`), `## The work so far` (`git log --oneline base..HEAD`, `git diff HEAD` with new files;
over `HANDOFF_DIFF_SHARE` (9%) of the window at 4 characters a token, or `HANDOFF_DIFF_MAX` (60K),
its `--stat` and "read the changed files", read only that far, #474; the stat capped at
`CHANGED_SHOWN`, unignored build directories named by `gitops.build_dirs`, "don't read them",
#863), `## Claude's answers to your questions` when any ("An earlier session asked, and Claude
answered; follow these, and don't ask them again:", `Q:`/`A:` from the `ask`/`answer` events since
`step_round`, `runner.answered`, never `NO_ANSWER`, latest first within `ANSWERS_MAX` 4000, #659),
for `OLD_STORE` `## Your last words in the earlier session` (its last assistant text, `LAST_WORDS_MAX`
2000 characters, `runner.last_words`, exported by wb from the user's store), and `## Now`: "Start on the task above" on a step's first round; a `CARRY_ON`/`RESUME` round's own
text; else `round_asks` since the last review feedback under `ASKED` ("What you were asked to
do…"), then "Now:" and the round's instructions (a fresh auto-fix round once lost the feedback).
The old id moves to `sessions`; `session`, `skills_told` cleared; a `fresh` event. A mid-round
handover's instructions are `CARRY_ON` ("Your last session filled up… re-read the files you are
changing, then finish them and run the tests") plus `round_asks`, the last two texts (1200
characters) and new notes; `wb resume` writes `RESUME`. `status.handovers` counts mid-round ones.
Notes folded after an opening are also written to `rounds/<n>/notes.md` (`folded_notes`,
`FOLDED`), which `round_asks` keeps when it skips a carry-on or resume (#677).

**OpenCode's own compaction** is a small model's summary that can lose the task. The layer sets
`compaction: {auto: true, buffer, background: false}`, `buffer = max(usable + reply − window, 0) +
STEP_GROWTH` (`compaction_buffer`; usable `runner.usable_input`: min of `limit.input` and the
window, as OpenCode compacts at usable − buffer; never context − output, #675; reply `options.max_tokens`, else `limit.output`;
`STEP_GROWTH` 4096), so it compacts only past the handover point with room for a full reply.
`compaction_point` is that share of the window (62.5% on 32K, 4K output, 8K cap); `fresh_ceiling`
sits `STEP_GROWTH` (or `tuning.CEILING_MARGIN`, if more) before it, the tuner's limit: the runner
checks at a step's end, OpenCode before the next request with its tool results in (#644). Both
count reasoning tokens. No `limit.context`: no mid-round handover, `auto`/`buffer` or ceiling.
`background: false` everywhere (`RESTATED` repeats it in `OPENCODE_CONFIG_CONTENT`): 2.3.2's
precomputed summary queued the agent's step on its own server and evicted its cache (#661;
`RealOpenCodeRun` checks); 2.3.0 ignores the key. Below `models.MIN_WINDOW` (16K: OpenCode's prompt and tools, PROTOCOL and
the handbook take ~5K) a model can't run
("a 8K window; tasks need 16K+ …"): the router skips it, `--model` refuses (exit 3), `wb model add`
won't register it, `wb doctor` marks it. At 16K no handover fits (`runner.handover_warning`); a
ceiling below every candidate is the value (`tuning.allowed`).

**An API task's window, output and header wait** (#1017). Its model's entry comes capped from
`models.api_limits` (*Models: found, loaded, chosen*: 131072 window, 32768 out), and the layer
file itself carries those pins under `provider.<pid>.models.<name>` (`runner.api_pins`, the
`model_pins` overlay): a catalog model has no entry in any config the agent's OpenCode loads, and
the catalog's own numbers would compact at 1M and ask for 256K out (OpenCode caps `max_tokens` at
`min(limit.output, 256000)`; it asks less as the window's room runs out). `rules_env` restates
them, as it does a GPU model's. So the buffer is 131072 + 32768 − 131072 + 4096 = 36864, the
compaction point 71.9% (about 94K tokens, the backstop for a session that only reads, which never
hands over mid-round), `fresh_ceiling` 66.9% (`fresh_at` 0.4–0.6 allowed, 0.7 and 0.8 not), and
`handover_warning` says nothing. The real-OpenCode check: `ApiSizing`'s `test_real_opencode_*`,
which sees 32768 out and a compaction at 100K tokens only with the pins. The header wait: an API
provider's `headerTimeout` is `runner.API_HEADER_S` (600 s) instead of `false`, since an API reads
no long prompt here and DeepSeek closes a request not started within 10 minutes; `chunkTimeout`
stays the larger of 900 s and silence + 60 s. All of it is decided from the task, not the catalog:
`drive` records the capped entry once, as it resolves the model (`runner.pin_api`, status
`api_entry`: `{limit, options: {max_tokens}}`, null on the GPU), and each round's layer
(`api_pins`, the compaction buffer, the header wait, `rules_env`'s restatement) and
`measure_speed` read it back (`runner.api_entry`). Read again from `hosted.json` each round, a
catalog rebuild that failed mid-task (it keeps no models) took them all away: the next round
compacted at 1M, asked 256K out, waited on headers unbounded and wrote its speeds as this
machine's. A later runner resolves the model again and, on a catalog that lost it, fails the load
(`_load_failed`) rather than run a round unpinned. The spend cap is checked before each of
`drive`'s `continue`s too (`runner._go_on`, *Spend*).

**Notes, nudges and interrupts.** `steer.jsonl` (`{ts, text, now}`) holds notes from `wb steer`,
`wb allow` (to restart the agent under its rule) and the nudge (`by: workbench`);
`status.steer_taken` counts delivered ones. `_steer_round` opens with `INTERRUPT_HEAD` ("Claude,
your reviewer, interrupted you. Stop what you were doing and do this first; it overrides anything
earlier…") or `NOTE_HEAD` ("A note from Claude, your reviewer, before you go on…"). The nudge is a
`now` note, once a round, ending it like an interrupt (`interrupted` `by: workbench`;
`status.nudges`, a `checkin`: "the runner nudged it: N min of thinking with no tool call"). The
next round passing the budget again is stuck ("… even after a nudge"; else a silent command was
nudged, killed and rerun forever), only in the same session (`runner.renudged`, status `nudged_round`, #681). Nudge rounds are marked
`rounds/<n>/nudged` (`NUDGED`), skipped by `round_asks` (#681); a fresh nudge round keeps every
earlier ask (#859). A note pending at exit is never delivered by that runner: the stuck report
lists Claude's ("NOTE: not delivered…"), the next command marks them taken (`cli._stale_notes`);
`wb resume` carries them (`NOTE_HEAD`, after the asks), `wb feedback`/`wb next` print and drop them
("not delivered, dropped …"); nudges drop silently.

**Check-ins** (`checkin_text`), every `checkin` minutes, not while asking: `task X · label · round
n · running <time> · ~Xm left (typically Nm)` (*Progress checks*), then since the last: tools used
and failed, tokens, `now:`, the latest words (400), files changed, and a NOTE for changed files the
spec never names (`outside_spec`: path or basename not in the step's prompt; silent when it names
none).

**Tests** (`run_tests`, `run_in_lane`): `<shell> -c` in the worktree, the agent's lane and
environment, its own group, `--test-timeout` (900 s), the runner's `CURRENT`. Read as it comes
(`_Capture`; a `setsid` server may hold the pipe): ends at the shell's exit, the timeout (exit
`timeout`) or `TEST_OUTPUT_CAP` (20 MB, exit `output limit`), then group kill and `sweep`. Kept:
`KEEP_HEAD` (64 KB) and `KEEP_TAIL` (256 KB) around `[... N bytes of output cut ...]`.
`status.tests` `{ok, exit, tail, round}`, tail `clip_tail(output, 40)` (40 lines, 300 characters a
line, 4000 in all). `rounds/<n>/tests.txt`: `$ cmd`, `exit: N`, `shell: <path>`, output.
`test_shell` is the agent's, as OpenCode 2.3 runs `$SHELL -c`: `$SHELL` when an executable absolute
path not in `NOT_POSIX` (fish, nu, …), else bash, else `/bin/sh`; `/bin/sh` (dash) made `source .venv/bin/activate` fail only for the runner
(#196).

**Setting the worktree up** (`prepare`, `_prepare`, before the base check and the GPU; #201: a
worktree holds only committed files, so checks needing installs, ignored files or `../lib` failed
everywhere). In order:
- `siblings` (`--sibling`): links where the project reaches them (`lib`/`../lib` → `<repo's
  parent>/lib`, `../../shared/lib` two up: `context.sibling_parts`, `sibling_link`,
  `sibling_real`; #261). Further than one up, the worktree nests as `tasks/<task>/tree/<the repo's
  parents' names>/worktree` (`context.tree`, task.json `worktree`); scratch checkouts nest alike.
  `wb start` refuses `.`/`..` inside, above `/`, inside another link or the worktree, or a
  `TASK_FILES` name beside a flat worktree. Read lane only (`context.siblings`, `external_rules`).
- `copy` (`--copy`) from the checkout as it is (`_copy`: `cp -a --reflink=auto`, macOS `cp
  -cRp`), replacing what is there, so start refuses a path with anything tracked under it (#244);
  it must be ignored in the task's worktree too, checked by the runner (#322, #529). Never a
  virtualenv (`runner.venv_in`, a `pyvenv.cfg` in the path or one level inside: it ran the developer's environment outside the lane, #260); the
  refusal says recreate it with `--setup`.
- For an API task, a standing `copy` or `siblings` entry of commands.json's `repos` that this
  command line doesn't name too is refused, exit 3 (`cli._standing_refusal`, its line
  `cli._standing_gap`, `privacy.standing_refusal`; #1021): `commands.json "repos" copies local.cfg into every task, and
  this one is on API (DeepInfra): name it (--copy local.cfg) to send it, or put the role on the
  GPU`; a sibling alike (`links ../shared beside every task`). A copied file and a sibling the lane
  reads reach the provider, so the user names each once. Named ones are kept, and the start's
  where-note lists them. `setup` and GPU tasks are unchanged.
- `setup` (`--setup`), as the tests run, for `setup_limit(spec)`: task.json `setup_timeout`
  (`--setup-timeout`, else commands.json `repos` `setup_timeout`), else `SETUP_SECS` (1800 s), in
  every scratch checkout too (#520). A venv it makes goes first on PATH (`context.own_venv`, *Its
  environment*).

Output to `setup.txt`. Done: status `prepared` (never rerun), `setup_secs` (`setup: Ns, once`), and
`prepared.json` `paths` (`_ignored_paths`), `marks`, `bytes` (`runner.prepared_record`, which also
reads older tasks' status `prepared_paths`/`_marks`/`_bytes`; in status they bloated every write,
#867). Failed: `failed` before any round, its notice with the exit and tail ("the setup command
failed (exit N)", "setting up the worktree failed (copy failed)" or "(timed out after Ns; …)",
`setup_timed_out`), the proxy left out (`runner.proxy_gap`, #380), each lane refusal with its grant
("the lane refused <path>: wb allow <task> --path <grant>, if the setup needs it": `lane_refused`,
`lane_grant`, #824), ending "fix it, then wb resume <task>: it sets the worktree up again (the
agent never ran)"; status `failed_before` `setup`. A load failure before the agent ran
(`_load_failed`) sets `load` and adds "the agent never ran: once the model can, wb resume <task>
starts it". Either way the results row has `ran: false`, which router and tuning skip (#581).

Scratch checkouts (base check, `wb verify`, `wb shot`: `verify._run`) get the same links and a copy
of the prepared paths, not the setup (#249: each rebuilt from nothing): `prepare(at, prepared)`,
reflinked where possible, `_copy_all` in `COPY_BATCH` (500) GNU `cp -a --parents` calls (#867). It
must fit (prepared.json `bytes`) with `CHECKOUT_SPARE` (2 GB) spare unless the filesystem clones
(`runner._reflinks`), else "copy failed" with sizes and what to free (`_no_room_for`, #860: wb
start's disk check predates the setup; no `--setup` offered). A file already there wins; an input
missing or whose parent resolves otherwise under the two resolved roots is "copy failed"
("prepared input <p> changed; use --setup…"), so a link inside a worktree refuses while a
symlinked `WB_HOME` passes (#345). The setup runs in the checkout instead (as `--setup` on `wb verify`/`wb shot` always
does) for a task prepared before paths were recorded, or whose prepared paths hold a virtualenv
(#325), a build tree configured for the worktree (`build_source`: CMake's `CMakeCache.txt`
`CMAKE_HOME_DIRECTORY`, Meson's `meson-info/meson-info.json`, #424), or files the rounds rebuilt
(`marks`, `prepared_mark`: lstat names, sizes, mtimes, minus `PREP_CACHES` and `CACHEDIR.TAG`
directories; a rebuilt `dist` gave the base the candidate's build, #732). A copied build tree for
another checkout's tracked sources is refused (`_foreign_builds`: "<path> is a build tree
configured for <dir>…"; one whose source is ignored passes). Failing there: exit `"setup failed"`. A checkout that isn't the task's
bytes (`verify.filter_gap`, `_filter_failed`) exits `"filtered"`, naming `filter.<driver>` (#932;
*Checkout settings*).

**The base check** (`_base_check`, `base_check`, before round 1 and the GPU): the acceptance command
once on the base in a scratch checkout (`verify.on_base`, a `--shared` clone,
`gitops.scratch_clone`, in the task's lane; in the worktree its writes stayed, #192), at most
`min(BASE_CHECK_SECS (120 s), --test-timeout)`. Output to `base-tests.txt`; status `base_check`
`{ok, exit, tail (8 lines, 1500 characters), limit, secs, cmd}` (`secs` excludes waits, #779).
`base_check_line`: a note when it passes (Claude checks the diff adds tests that fail without the
change), a warning plus an immediate `checkin` when `--test-timeout` cut it, a note past 120 s
(#496), not checked when the checkout failed or was `"filtered"`, else exit and tail. A passing base
is a note, not a check-in (that cost a restart in one task of two, 2026-11-17). `--no-base-check`
(task.json `no_base_check`) skips it.

**The round report** (`summary_text`), line by line:
1. `task X · label · round n · M.m min · local tokens IN in / OUT out` (input excludes cache
   reads; an API task's says `agent tokens`: an API model's tokens are neither local nor free,
   #1022), for an API task then
   `· $0.12 so far` (`results.spend_so_far`: the runner's meter, else the step events' `cost`) or
   `· cost unknown` (`runner._spend_part`); ahead of it in the review notice and `summary.md`,
   `auto-fix skipped: <cause>`.
2. `agent context: U.UK of WK (P%)[, compacted Nx][; the next round starts a fresh session | ;
   the next round carries on in this session: …]` (#781).
3. `STOPPED: why` or `opencode exited N (see …/runner.log)`; past the first step, `steps 1-N
   committed on delegate/<task> (wb next): wb discard <task> drops this step and keeps that
   branch; to land them instead, …` (`steps_line`: reset the worktree, `wb accept --force`; #494; also in the `SIGNAL_STOP` notice). After
   a crash with changes, `opencode exited N: the round didn't finish; what it changed so far stays
   in the worktree (see …)`. `OpenCode resent N request(s) to the model's server: …` (#491).
4. `tests: PASS|FAIL (`cmd`, exit N)`, 25 lines on a failure; `base check: …` while on the first
   step with no review round; `auto-fix rounds used: a/b`.
5. `tools: name×n …`, up to four `tool error:` lines.
6. `agent's summary:` and the last text (2500 characters).
7. `files changed:` (`git status --porcelain`), NOTEs for files outside the spec, Claude's files
   (`claude_files_changed`: CLAUDE.md, CLAUDE.local.md, AGENTS.md, `.claude/`), `NOTE: edits in
   submodule <path> can't land: …` (`gitops.submodule_edits`, #419), `NOTE: new files in submodule
   <path> won't be committed: …` (`gitops.submodule_check`, #889), and the diffstat's last line.

**Speed** (`measure_speed`): after each round the export's new messages give write and read samples
(`router.record_speed`; a cut-short request with a first token still gives a read sample). A round
that ended before the model's first output (only `NOT_THE_MODEL`: `step_start`, `retry`, `error`,
`background_wait`) after `BOUND_MIN_S` (120 s), stopped by silence, the round limit or OpenCode
giving up (not a nudge or interrupt), gives a bound (`read_bound`, `router.record_read_bound`): the
prompt (`prompt.md`, `instructions.md`, instruction files at 4 characters a token, plus
`OPENCODE_PROMPT_TOKENS` 3000) over the seconds waited, low so budgets come out long (#491). No
bound when the server is down or stopped (`_down`, `_stopped`) or a retry wasn't a timeout: a dying
server read as ~12 tokens/s and later rounds waited an hour (#862). Never for an API model: its
time to first token is the network's and the provider's queue, not a read (#1017).

**Prompt text other code depends on.** Each prompt opens with a head that something else matches
by prefix; rewording one silently changes those:

| Text | Written by | Read by |
|---|---|---|
| `\n---\nHow to work` (`cli.PROTOCOL`, `cli.WRITING_PROTOCOL`) | `wb start`, `wb next` | `transcript` folds it away, `bench.FOLD`, `runner.outline_section`'s split (up to `{branch}`) |
| `Lesson:` lines in the agent's summary | the protocol asks for them | `lessons` |
| `Review feedback from the senior reviewer` (`lessons.FEEDBACK_HEAD`) | `wb feedback` | `lessons` (feedback items), `round_asks` (stops there), `transcript` (label) |
| `The acceptance tests fail` | the auto-fix round | `transcript` |
| `Before Claude reviews this` (`SELF_REVIEW`) | the self-review round | `transcript` |
| `You are picking this task up in a fresh session` | `handoff_prompt` | `transcript`, the fake OpenCode (`fill`, `compact`, `compactfail`) |
| `Step N of this task is done and committed` | `wb next` | `transcript` |
| `INTERRUPT_HEAD`, `NOTE_HEAD` | `_steer_round` | `transcript` |
| `NUDGE` (`without a tool call. Stop planning`) | the think nudge | `transcript` (labelled the workbench's, not Claude's) |
| `Claude picked these skills`, `Claude handed you more skills`, `Claude took back:` | `skills_note` | `transcript.SKILLS_HEADS` (skips the note to label what follows) |
| `CARRY_ON`, `RESUME` (first 60 characters) | `_full_round`, `wb resume` | `round_asks` skips them (but their `notes.md`) |
| `agent's summary:` | `summary_text` | `lessons` |
| `--title "wb: <task>"` | `run_round` | `transcript.find_session` |
| `Outline of <file> (from probe symbols):` (`context.OUTLINE_HEAD`), `… N more: `+"`probe symbols <file>`"+` (`OUTLINE_MORE`) | `runner.add_outline`, after round 1's protocol (probe on, *Experiments*) | nothing by prefix: being after the protocol, `transcript`'s fold and `bench._spec_of` leave it out of the spec (a replay makes its own) |

The skills note is prepended to the prompt argument; the transcript skips past it (to its first
blank line) before matching the label, so the prompt keeps its own. The probe outline is the one
text appended, not an opening: keep it after the protocol, where the fold cuts.

**Two protocols, one fold.** A writing task's prompts end with `cli.WRITING_PROTOCOL` instead of
`PROTOCOL` (`cli.protocol_for`, at `wb start` and `wb next`; *The writing role*). It keeps
`PROTOCOL`'s fold and first bullet word for word (`\n\n---\nHow to work (workbench protocol):\n-
You are in a git worktree on branch {branch}.`), its `{test_line}` (`bench.TEST_LINE` reads the
acceptance command back), its `ask_reviewer`, lane and `.proposed` bullets and its `Lesson:` line,
and takes the same `.format` fields. Only the code bullets differ: prose only, no invented facts
(`[TK: …]`), no reflowing. So every reader above treats a writing prompt as any other.

## What the agent knows

An agent should start knowing what Claude would know in the same repo, and nothing that would
distract it. The per-task config layer (`<state>/rules/<task>/opencode.json`, written before every
round by `runner.write_config_layer` from `context.layer`) gives it `instructions`, `permissions`,
`skills`, `compaction`, the `ask_reviewer` server and the providers OpenCode wouldn't load itself
(`models.agent_providers`). For the task's provider it sets `options.headerTimeout: false` and
`chunkTimeout` at the larger of 900 s and `--silence-timeout` + 60 s (`runner.provider_timeouts`,
`PROVIDER_TIMEOUTS`): OpenCode 2.3's 300 s header wait cut every slow request (a `retry` line);
the runner's own limits decide how long a round waits (#491). OpenCode merges provider options, so
the user's entry keeps the rest. Everything in `rules/<task>/` (config, instruction copies, git
shim) is outside the lane, so the agent can't rewrite its own rules for its next round.
An API provider's header wait is 600 s instead (`API_HEADER_S`, *An API task's window, output and header wait*).

**Instructions** (`context.instruction_files`), absolute paths, in this order:
- the project's `AGENTS.md` files (`agents_files`), nearest first (*handed back*, below);
- `agent/HANDBOOK.md`, copied fresh each round to `rules/<task>/HANDBOOK.md` (a path into the
  workbench repo once sent an agent looking for plans). Every prompt carries it: keep it short
  (about 1.3K tokens). A writing task (task.json `role: writing`) gets `agent/WRITING.md` there
  instead, under the same name (`context.handbook_for`, *The writing role*);
- `~/.claude/CLAUDE.md` (`$CLAUDE_CONFIG_DIR`), copied into `rules/<task>/`: the lane can't read
  Claude's directory;
- the user's `rules/**/*.md` from Claude's directory (`user_rules`), copied to
  `rules/<task>/user-rules/<path>`, imports followed, dotted names and links into a secret left
  out, copies of rules removed since deleted, each counted in `fixed_prompt` (#673: Claude reviewed against rules the agent never saw). The
  walk (`_md_under`) follows directory links, each real directory once (`Path.rglob` follows none:
  a linked `~/.claude/rules/team` never arrived, #828). `paths:` frontmatter isn't read;
- the repo's `CLAUDE.md`, `.claude/CLAUDE.md`, `CLAUDE.local.md`: the worktree's (committed) copy,
  else the checkout's, so an uncommitted edit to a committed file isn't seen;
- in a monorepo package (task.json `subdir`, `cli._package`: the nearest directory below the top,
  from `--repo` up, with a committed `CLAUDE.md`/`.claude/CLAUDE.md` or a gitignored one in the
  checkout, `cli._local_package_md`: `wb start` notes `<package>'s CLAUDE.md is gitignored: the
  agent gets your checkout's copy`; never a `.claude` directory), the package's three the same
  way. The agent, setup, tests and session export run there (`Task.workdir`); diff and commits
  stay the whole repo's; a plain subdirectory means the top (#375, #606, #785);
- tracked `.claude/rules/**/*.md` (`project_rules`), and the `.md` under a tracked directory link:
  one resolving outside the worktree and repo is copied to `rules/<task>/project-rules/<path>`
  (unless a secret or workbench's own), counted as `.claude/rules/<path>` (#828);
- `context.md`: a map of nested `CLAUDE.md`/`AGENTS.md` (tracked, not under `.claude/`, not those
  already loaded): "Before you change files under one of these directories, read its file and
  follow it", instead of loading them all;
- last, a project `opencode.json`'s `instructions` (`project_config_instructions`).

Each but the handbook is followed by its `@` imports (`import_files`, depth first): OpenCode reads
an instruction file as a raw string, so `See @README.md` reached the agent as that text (#513).
As Claude Code (`imports_in`, `IMPORT`): `@` at a line's start or after a space, path to the next
unescaped space, `#` fragment dropped, relative to the importer's real directory, `~` expanded, at
most `IMPORT_HOPS` (5) deep, each file once; not in a code span, a fence (`FENCE`) or an HTML
comment; a missing or non-text target (`_text_file`) ignored; an indented code block is read as
text. A target in the worktree or repo loads from the worktree; one elsewhere is copied to
`rules/<task>/imports/<n>/<name>`, replaced whole each round. Claude Code asks before a project
imports from outside it; wb can't ask, so a project's imports reach only the repo and Claude's
directory's Markdown with no dotted part (never `.credentials.json`); the user's CLAUDE.md's reach
anywhere but a secret or workbench's own files (`secret_paths`, `private_paths`). `fixed_prompt`
counts each import as its own part.

**What the project's own config gave, handed back** (#994, 2026-11-22). With the project's config
shut out (`OPENCODE_DISABLE_PROJECT_CONFIG`, *Project config can't lift the rules*) OpenCode stops
reading what it found in the project, so wb lists those from `Task.workdir` and each directory up
to the worktree's top (`_project_dirs`; not above: that is workbench's state, and a `~/AGENTS.md`
would be the user's):
- `agents_files`: each `AGENTS.md`, nearest first, ahead of every other instruction file (only
  OpenCode's global `~/.config/opencode/AGENTS.md`, which still loads, comes before);
- `project_skill_dirs`: each of `PROJECT_SKILL_DIRS` (`.claude/skills`, `.agents/skills`,
  `.opencode/skills`, `.opencode/skill`), in `skills` after the handed ones;
- `project_config_instructions`: the `instructions` of `PROJECT_CONFIGS` (`opencode.json`,
  `.jsonc`, `.opencode/opencode.json`, `.jsonc`), globbed up the tree as OpenCode's `globUp`: files
  in the worktree only, never a URL (fetched every run, a network call nobody allowed) nor an
  absolute or `~/` path.

The rest stays unread on purpose: `plugin`/`.opencode/plugins` run code in OpenCode's process,
outside the rules; `formatter`/`lsp` run commands the shell rules never see; `agent`, `mode`,
`.opencode/agents` replace OpenCode's prompt and tools which wb measured
(`OPENCODE_BASE_TOKENS`); `command` is the TUI's; `permission(s)`, `mcp`, `provider`, `model`, `small_model`,
`compaction`, `tool_output` are the layer's. Measured on OpenCode 2.3.2 (`tests/capture_llm.py`):
the switch drops exactly these and nothing else; RealOpenCode's
`test_a_projects_agents_md_and_skills_reach_the_agent_and_its_config_lifts_nothing` checks it.
OpenCode also reads a `CLAUDE.md` with no `AGENTS.md` beside it (`~/.claude/CLAUDE.md` is a secret
in the lane); files are deduplicated by SHA-256, so an identical file isn't loaded twice.

**Denied tools** (`DENIED_TOOLS`), which also leave the prompt (about 9K to 3.5K tokens, read on
every request): `question` (nobody answers headless), `execute` and `opencode_*` (they move the
session or remove worktrees; denying `execute` makes the `opencode_*` tools direct, so both),
`subagent` (a second model on the GPU behind the accounting), `*worktree*`, `websearch`,
`webfetch`, `experimental_image_generate`. OpenCode 2.3 still lists `opencode_worktree_remove` with them denied; the rule refuses the call.

**Commands, an allowlist** (`context.shell_rules`). OpenCode checks each simple command of a line
(pipes, `&&`, loops, `$(…)`) by its raw text; patterns are anchored, `*` matches anything (spaces
and `/`), `?` one character (a space too), a trailing ` *` is optional, the last match wins, no
match asks (which `--auto` allows). The rules, in order:
1. deny `*`;
2. allow `c *` and `*=* c *` (`ASSIGN`) for each of `ALLOWED_COMMANDS` (about 735): builtins,
   file and text tools, builds, compilers, test runners, linters, interpreters, package managers,
   `bash`/`sh`, `git` (the shim), a test's own local database or service (`psql`, `redis-server`…;
   the skill's `reference/checks.md` says to bind them to a socket or 127.0.0.1; python reaches any
   database anyway, the owner, 2026-11-22), embedded builds compiled and emulated, not flashed. A
   family is one glob where safe (`gcc*`, `clang*`; each entry costs ~135 bytes with its `*=*`
   copy). Tools that also reach out are listed by offline subcommands only (`LIMITED`: `terraform
   fmt`/`validate`/`init *-backend=false`, not `plan` or `refresh`, which read remote state with the
   user's credentials, nor `test`, which creates infrastructure; `helm lint`, `docker?compose
   config`, `ansible-playbook *--syntax-check`…). What sends usage data by default runs only in
   its opt-out spelling (`CDK_DISABLE_CLI_TELEMETRY=true cdk synth`, `SAM_CLI_TELEMETRY=0 sam`,
   `REDOCLY_TELEMETRY=off redocly`, `semgrep *--metrics?off*`, `azurite *--disableTelemetry*`);
   what uploads the project's runs only with that off (`nx *--no-cloud*`/`NX_NO_CLOUD=true nx`,
   `RUSH_BUILD_CACHE_WRITE_ALLOWED=0 rush`); Firebase's emulators only on a `demo-*` project
   (code aimed at a resource without an emulator fails instead of reaching the live one). A `*`
   inside an entry can be steered (`terraform -chdir=. plan -out validate`; `init
   -backend=false -from-module=<src>` fetches any module): review is the guard.
   Not on it, and why: `nix` (builds go through the daemon's socket), `curl`, `wget`, `ssh`,
   `rsync`, `systemctl`, `opencode`; `docker` and `podman` beyond `compose config` (no daemon
   socket in the lane; rootless podman needs setuid `newuidmap`: kind, minikube, Testcontainers are
   Claude's at review); `kubectl` beyond `kustomize`, helm's installs and pushes, `pulumi`, `aws`,
   `gcloud` (its emulators too: its config is hidden), `az`, `ansible` and `ansible-playbook` runs
   (a cluster, registry, cloud or host, with the user's credentials); migration tools (`alembic`,
   `flyway`, `prisma migrate`: a worktree `.env` can name production); `turbo`, `lerna`, `moon`,
   `skaffold`, `vcpkg`, `oasdiff` (telemetry on by default or remote caches uploading with the
   user's login); `buf generate` (remote plugins get the schema) and `buf dep update`; globs that
   could match a tool that reaches out or changes the machine (`wasm-*`: `wasm-to-oci` pushes;
   `qemu-*`: `qemu-nbd`, setuid `qemu-bridge-helper`; `protoc-gen-*`, `mdbook-*`: anyone publishes
   under them), so those families are named one by one.
   The `*=*` form exists because a small model copying the project's `CI=1 npm test` was refused
   and spent a wake (#258); it passes a line with an `=` anywhere before an allowed name, no more
   than `env *` runs. So "not on it" bounds the plain spelling only: the wrappers allowed whole
   (`env`, `timeout`, `nice`, `nohup`, `time`, `command`, `xargs`) and an `=` before an allowed
   name lift every subcommand limit and the default deny (`env terraform plan`, `env pulumi up`);
   step 5's patterns hold behind them, review guards the rest. Package-manager runners are not
   such wrappers for the tools a project installs (step 4, #999);
3. allow `./*`, `../*`, and `<dir>/*` for each lane directory but under `/dev` and `/proc` (a
   debug binary in the temp directory was refused though `bash` could run it, 2026-11-16);
4. allow `PROJECT_BINS` (`.venv/bin/*`, `node_modules/.bin/*`, `build/*`, `target/debug/*`,
   `bin/*`, `dist/*`, `scripts/*`, `tools/*`…, each also behind `*=*`); then the acceptance
   command's tools (`grants.json` `test_tools`, from `context.test_allowances`: each simple
   command with more arguments, `npm test *`, and its tool whole, `<tool> *`, by basename for a
   path), so it can run the tool's other work (`npm run lint`). Never whole a `LIMITED` tool (the
   tool of an opt-out entry is the word after the variable, `context._entry_tool`) nor one of
   `OUTWARD_TOOLS` (a cloud, cluster, registry, host or container daemon: `terragrunt`, `pulumi`,
   `kubectl`, `helm`, `docker`, `aws`, `turbo`, `lerna`…, listed or not): `--test 'terraform
   validate'` granted `terraform apply`, `pulumi preview` `pulumi up` (2026-11-22); those get only
   their line with more arguments. Steps 5 and 6 still hold over a grant: `--test 'npm test'`
   allowed `npm install left-pad` (#646, #915).
   Then the runner forms (`RUNNER_TOOLS`, #999): `npx`, `pnpm`, `yarn`, `bun`, `uv` and
   `node_modules/.bin/` are allowed whole, so `npx turbo` or `uv run semgrep` ran without their
   plain spelling's conditions. For each such tool not allowed whole (`RUNNER_HELD`, with npx's
   package names, `NODE_PACKAGES`): deny `RUNNER_DENY`, its forms under the runners
   (`RUNNER_FORMS`, each starting with `*` so `CI=1` or `env` before is caught; `*npx {t}@*` too, as
   `npx nx@19 release` slipped; pip's tools only the run, exec and bin forms); then allow
   `RUNNER_ALLOW`, their `ALLOWED_COMMANDS` entries under each `RUNNER_PREFIXES` spelling, variables
   first; then the acceptance command's lines with more arguments again, never a tool granted whole
   (`npx *` from `--test 'npx vitest run'` would lift all this). The tools allowed whole keep
   their `OUTWARD` steps under the runners by name (`npx wasm-pack publish`), but a version
   spelled passes (`npx dprint@0.47 upgrade`). A word after a flag is taken for
   the tool (`pnpm --filter cdk build` asks once); `npx -c '…'` passes as `bash -c` does;
5. deny `OUTWARD`, a step off the machine inside an allowed tool, each pattern starting with `*` so
   a launcher or wrapper is caught (but `v up *`, with `* v up *` beside it, so `just dev up` runs) (`pnpm publish`, `./mvnw deploy`, `timeout 600 terraform
   apply`): publishing and pushing (registries, Maven `deploy`, Gradle `publish*`), deploying
   (`cdk`, `wrangler`, `firebase`, `prisma migrate deploy`), changing infrastructure (terraform's
   and tofu's `apply`, `destroy`, `import`, `state`, `test`, `init -upgrade`; kubectl's `apply`,
   `create`, `delete`; helm's installs and `--validate`), `nx release` (npm's publish with the
   readable `~/.npmrc` token) and nx-cloud's verbs, logins, uploads to a service, self-updates
   (they replace the user's program), `hugo server trust` (a system CA), and flashing (`idf.py
   efuse-*`: burned eFuses can't be unburned). Kept for tools later dropped, so a whole grant still
   stops there. A shared build cache written
   or remote execution from the unreviewed worktree (#999): the environment turns Pants's and Nx's
   off (`runner.REMOTE_CACHES_OFF`, *Its environment*), so denied are the spellings that turn them
   back on: Pants's flags (`--remote-cache-write`, `--remote-execution`, which beat its
   environment) and either variable named at all (`*PANTS_REMOTE_CACHE_WRITE*`,
   `*NX_SKIP_REMOTE_CACHE*`, `unset`/`env -u` too); Bazel's and Buck2's cache, executor and
   build-event flags (`--remote_cache`, `--remote_executor`, `--bes_backend`, `-c
   buck2_re_client.`…). A repo's `.bazelrc`, `.buckconfig` or `pants.toml` can turn the same on with
   no flag, and the agent can edit them: those are in the diff, the reviewer's (the engineer's
   call, 2026-11-22). After step 4 (a `cargo *` grant doesn't lift `cargo publish`), before step 7
   (`wb allow` does). Verbs are words, space-bounded (`*mvn* deploy *`): `*mvn* *deploy*` refused
   `mvn -pl deploy-tools test`, as #442's `*` did; Gradle tasks and Maven goals are prefixes, so
   `:publishing:test` or `npm publish --dry-run` asks once. Not `npm login`/`adduser` (they wait on a
   browser). They stop the plain and wrapped spelling only: a script, `make deploy`, `bash -c`, an
   interpreter, an unlisted verb pass, and review is the guard.
   Then deny `ASK_FIRST`: adding a dependency (`pip install *`, `npm install *`, `cargo add *`…,
   and installs into the user's prefix) and fetching a package to run it (`npx -y *`, `pnpm dlx *`,
   `uvx *`, `dub run *`…), each also behind `*=*`; and `RUNNER_ASK`, the package steps of
   `RUNNER_TOOLS` under runners (`npx nx add`). A plain `npx <pkg>` stays allowed, but OpenCode runs
   with `npm_config_yes=false` (`runner.OPENCODE_ENV`; npm says yes by itself with no terminal), so
   it runs only what the project installed; `npx --yes <pkg>` is the form to ask for;
6. allow `INSTALL_LISTED`, installing what the project lists (the optional trailing ` *` makes
   step 5 refuse the bare forms too): `npm install`/`ci`, `yarn`, `pip install -r *`, `pip install
   -e .` and its extras (#671), `pipenv install`, dub's own `run`/`build`/`test`…, and
   `RUNNER_LISTED`; npm/pnpm/bun with one of `INSTALL_FLAGS` or a pair from `INSTALL_FLAG_PAIRS`
   (#916; every ordered pair came to ~80 KB, and no `=*` flag, whose `*` would pass a package name). The pip forms only where pip installs into the worktree's own venv, or
   with no Python environment active while Landlock bounds it (`context.python_installs`),
   otherwise they ask: pip would write the user's env or their Python with no lane. pipenv's
   wherever no venv of the user's is active, lane or none (it makes its own in the worktree,
   `PIPENV_VENV_IN_PROJECT`; #950: taken for pip, it asked on every Mac). While a user's venv is
   active, `ENV_INSTALLS` (`poetry install`/`sync`/`remove`, `uv sync --active`, `uv pip sync`,
   `pip uninstall`) ask first too (#490: the agent's `pip install -e .` repointed the user's venv
   at the worktree, which accept then deleted);
7. allow the user's standing patterns (`commands.json` `allow`) and the task's grants
   (`rules/<task>/grants.json` `allow`: `wb allow`, `--allow`, and the acceptance command's simple
   commands as written, so the agent can run what the protocol tells it to even where steps 5 and
   6 refuse, #514); a `*` here lifts everything above;
8. deny the hard limits (`context.denied_shell()`): `DENIED_SHELL` from `_HARD` (`sudo`, `doas`,
   `pkexec`, `su`, `gh`) and the real git by path (each `git` on the runner's PATH but the shim,
   its real path, `GIT_PATHS`), each as `c *` and behind `_WRAPPERS`, a wrapper's own option
   shapes (`env`, `env -i`, `xargs -0`, `-I {}`, `nohup`, `nice -n ??`, `time -p`, `command -p`,
   `exec`, `timeout ???`, `stdbuf -??`, `setsid`…). Never a `*` before the hard word: `*` matches
   spaces, so `env *`-style forms refused `xargs grep -l gh` and `pytest --tb=short -k gh`, and
   being last nothing could lift them (#442). Each shape spans at most three `?` characters, so it
   can't reach over a command's name; a longer value (`timeout 3600 sudo`) or a variable first
   (`FOO=1 sudo`, `env CI=1 gh`) passes like `bash -c`, and the kernel stands behind it. Not
   `*/git` (refused `go test ./internal/git`), not `time *` (the 2026-11-16 audit).

A refused command comes back as "Permission denied"; the handbook and `ask_reviewer` tell the agent
to ask with `command` set, raising a PERMISSION question naming the `wb allow`. These patterns are
a guardrail on the agent's choices, not a sandbox: they match the command as written, so `bash -c`,
make or an interpreter can run anything. What must hold is held below them, on Linux with Landlock:
no sudo (no-new-privileges, on any Linux), git read-only (the shim; the git directory is outside
the lane), no push or `gh` with the user's keys (credentials unreadable and dropped from the
environment). Without Landlock (macOS, old kernels, other architectures) only the patterns, the
shim and `SECRET_ENV` remain: `bash -c` can write the repo's `.git` and read `~/.ssh`; on macOS
nothing refuses sudo.

**Paths, a lane** (`context.writable`, `context.readable`, `context.external_rules`, `lane.py`).
It keeps an agent's mistakes and its use of the user's credentials in; it is not a jail for a
deliberately hostile process.
- *Writes.* The task's worktree, `ask/` and `opencode/` (its OpenCode's own home, below), not
  the rest of the task directory: `task.json`,
  `status.json`, `steer.jsonl`, the rounds and `runner.log` steer the runner, so writing them would
  lift its guards, and the runner would write through a planted link (the 2026-11-16 audit). Then:
  the temp directories (`gettempdir()`, `TEMP_ROOTS`), `DEV_NODES` (`/dev/null`, `/dev/zero`,
  `/dev/full`, `/dev/random`, `/dev/urandom`, `/dev/tty`, `/dev/ptmx`, `/dev/shm`; not `/dev/pts`,
  so a pty test needs a `/dev/pts` grant); with `--unload-for-tests` the GPU nodes that exist
  (`GPU_NODES`, `gpu_nodes`: without them CUDA/ROCm/Vulkan found no device and said nothing, #331);
  `/proc`; the build caches and toolchain homes under `$HOME` (`TOOL_HOMES`: `.cache`, `.cargo`, `.m2`,
  `.android` (split around `adbkey`), `.texlive*/texmf-var` (each match; not `texmf-config`, which
  every later run reads), caches, never a tool's keystores or settings…; of `go`, `.bun`, `.deno`
  and pnpm's home only their caches; the `.local/share|state` ones under
  `$XDG_DATA_HOME`/`$XDG_STATE_HOME` when set, `tool_homes`), Flutter's `bin/cache`
  (`flutter_caches`: every flutter command locks it), wherever `TOOL_HOME_VARS` moved them
  (`CARGO_HOME`, `GOPATH`'s first entry, `GOMODCACHE`, `CARGO_TARGET_DIR`, `JULIA_DEPOT_PATH`'s
  every entry (`ALL_ENTRIES`), `HF_HOME`…, absolute only; of `GOPATH` and `BUN_INSTALL` only
  `VAR_CACHES`), `$XDG_CACHE_HOME`, and the extra paths
  (`commands.json` `paths`, grants `paths`), each with its real path, only those that exist.
  OpenCode's cache and config directories are made first (a fresh profile couldn't open its log,
  #340). The user's OpenCode data and state homes are not in it, made or not: OpenCode V2 keeps
  every `/connect` credential in `~/.local/share/opencode/opencode.db`, beside every project's
  sessions and snapshots, and the lane wrote that directory (#1010, 2026-11-22). The agent's
  OpenCode keeps its own in the task's home, `tasks/<task>/opencode/` (`context.opencode_home`,
  `share/` and `state/` inside, made 0700 before each round by `make_opencode_home`, a link the
  agent left there replaced): its sessions, `snapshot/`, `tool-output/`, `shell/` (each shell
  command's saved output) and log, no credentials. wb reads it only through `opencode session
  export`/`list`, run in the task's own lane (`transcript.stores`): the agent writes the home, and
  OpenCode run by wb outside the lane wrote through a link it planted there (`state/`, `log/`;
  the #1010 review). `snapshot/`, `tool-output/` and `shell/` are dropped when the task closes
  (`drop_opencode_scratch`, `OPENCODE_SCRATCH`, accept and discard; never through a link at any
  step); the database stays for `wb watch`, the lessons and `finished_export`, and moves with the
  task into `archive/`. OpenCode's `tmp`
  (`/tmp/opencode`, shared by every task and the user's OpenCode) holds no session data, only
  scratch (checked on 2.3.2: a whole round there left it empty; `TMPDIR` would move it): it stays.
  Left out, where a planted file runs later outside the lane: what's on the user's PATH or sourced
  by their shell, and the toolchains those run: `~/.local/lib` (user site-packages, whose `.pth`
  every python runs); `.local/share/uv` (uv's tools and Pythons; its cache is `~/.cache/uv`);
  `NOT_WRITABLE` (`.cargo/bin`, `.cargo/env`, `.dotnet/tools`, `.cabal/bin`, `.ghcup/bin`,
  `.rustup/toolchains`, `.nimble/bin`, and `VAR_NOT_WRITABLE` under the variables), made or not
  (`not_writable_paths`); the user's active Python environments (`context.python_envs`:
  `VIRTUAL_ENV`, `CONDA_PREFIX`, each PATH `bin` beside `pyvenv.cfg` or `conda-meta`, #490). So
  `cargo install`, a new rustup toolchain, a pip install into the user's env are Claude's: no grant
  reaches them (`context.kept_out`, #912).
  Landlock only allows, so a place split around something (`context.split_points`: a secret,
  workbench's own files, an active environment, a `NOT_WRITABLE` place) is granted its existing
  entries one by one and refuses every new top-level one. So what the tool makes there is made
  first, never replacing an entry (`_make_first`): `CACHE_MADE` in a cache home holding an active
  env (#825), `MADE_FIRST` (so the split holds before the tool makes them), `CARGO_MADE` (a fresh
  rustup install refused every build with a dependency, the 0.90 review), `NIMBLE_MADE` with
  `MADE_TEXT` (nimble parses its data file, #911; its first `nimble refresh` is Claude's),
  `SPLIT_MADE` (`~/.m2`). The agent's poetry and pipenv make their venv in the worktree.
  A home kept whole is a documented trade, like `/tmp`: it can hold a program the user's tools run
  later outside the lane (`.dotnet`, `.ghcup`, `.julia`, `~/.cache`'s pre-commit hooks, cargo's
  registry sources, `.tflint.d`'s plugins); splitting would refuse a build's first new entry.
  No entry may resolve to `/` or the home directory, nor contain the task's repo or its git
  directory (`repo_dirs`, the main worktree's for a linked one, #776: with `GOPATH=~/src` the agent
  could write a hook or `core.fsmonitor` that wb's git runs outside the lane): a place holding them
  is split around them, and one naming the repo or inside it grants nothing (refused by `wb allow
  --path` and `--allow-path`). The temp directories stay whole even so (a repo in `/tmp` stays in
  reach). Other programs' files in `/tmp` stay writable (Java, Xvfb's `/tmp/.X11-unix`, test
  suites); the review diff's scratch index lives in `$WB_HOME`. A `$WB_HOME` in `/tmp` (the tests')
  is in the lane whole. `$XDG_RUNTIME_DIR` is out (the bus, SSH and GPG agent sockets): the agent
  gets its own, `runtime_dir`, `/tmp/wb-run-<uid>-<hash>`, 0700.
  A failed test run's refusals are named in the report (`runner.lane_refused`, status
  `tests.lane`, with `wb allow <task> --path <p>`): a line matching `REFUSAL` (with Bundler's and
  RubyGems' wordings, #832) or `PTY_REFUSED` (counted as `/dev/pts`, #829); its absolute paths
  (`refused_paths`: `QUOTED_PATH`, `MARKED_PATH`, `ABS_PATH`; one with spaces once granted all of
  `/mnt/c/Users`, #670) outside the lane, mapped by `lane_grant` to what a grant can reach: the path
  if it exists or is under `/dev`, else the nearest existing directory, the toolchain home it falls
  in (`context.tool_home_of`, made by `wb allow --path`, #824) or a whole RubyGems home
  (`context.gem_home_of`), then the next directory down while the place is split; never `/`, home,
  a secret or the repo. Only a refusal the lane made (#773): none without Landlock, nor one the
  runner shares (`runner_may`: the user's own permissions).
- *Reads.* Everything but credentials and the user's records: `context.SECRETS` under `$HOME`
  (`.ssh`, `.gnupg`, `.netrc`, `.git-credentials`, `.config/gh`, keyrings, the cloud CLIs'
  (`.aws`, `.config/gcloud`, `.kube`, `.docker`…), `.android/adbkey`, registry and deploy logins
  (#992, #999), OpenCode's `service.json` (its service runs outside the lane), OpenCode's data and
  state homes whole (`.local/share/opencode`, `.local/state/opencode`: the database and its
  `-wal`/`-shm`, a release channel's, the legacy `auth.json`, `snapshot/`, `tool-output/`,
  `worktree/`, `repos/`, `prompt-history.jsonl`; and a user's own `OPENCODE_DB`, #1010: no file there
  opens, though names stay listable, as under every secret, *Free* below), password managers,
  browser and mail profiles, shell history, `.claude`, `.claude.json`), the same under the XDG
  directories, wherever `SECRET_VARS` move
  them (`GNUPGHOME`, `DOCKER_CONFIG`, `KUBECONFIG`, `HISTFILE`, `SEMGREP_SETTINGS_FILE`…), and
  Claude's directory (`secret_paths`); and workbench's own files (`private_paths`): `$WB_HOME` and
  its config wherever the `WB_*` variables move it (the audit read the chat token from a lane,
  enough to post to Claude as the user). Of `$WB_HOME`, the task's directory, `rules/<task>/` and
  a repo with its git directory lying there (a bench replay's clone, #858) stay readable.
  Readable on purpose (an unreadable one ends the install): the files a build reads its
  repositories and logins from (`.npmrc`, `.yarnrc.yml`, `.m2/settings.xml`,
  `.gradle/gradle.properties`, `NuGet.Config`, poetry's `auth.toml`, composer's `auth.json`, pub's
  `pub-tokens.json`, `.hex/hex.config`, `pip.conf`, `.bundle/config`, `.bunfig.toml`; an API task's
  lane hides each that holds a credential: *API lanes hide the token files that hold a credential*,
  below). Where a tool stops on a hidden file the agent gets its own
  (*Its environment*); Vercel's and the Firebase CLI don't run in the lane (Firebase's emulators do
  with `XDG_CONFIG_HOME=$(mktemp -d)`). Without Landlock (macOS, older kernels) there is no lane,
  and the user's OpenCode store stays readable to the agent's commands; the task's own home still
  keeps the agent's OpenCode off the user's credentials and sessions. A literal `apiKey` in the
  user's global `opencode.json` stays readable too: the agent's OpenCode loads that file itself.
  Of the home directory's own entries only dotfiles and `HOME_READABLE` (`bin`, `go`, `R`, `perl5`)
  are readable, and of `ONLY_KEPT` (`/dev`, `/mnt`, `/media`, `/run/media`, `/run/user`, `/home`,
  WSL's drives) nothing, beyond: OpenCode's global directory (`OPENCODE_CONFIG_DIR`, #370), the
  task's repo (its secrets still out), its `siblings` and git common directory (read-only),
  workbench itself, the lane, the programs the agent runs (`tool_roots`: each PATH directory, its
  prefix under home or under an `ONLY_KEPT` root short of a user's home or a drive, the install a
  home `bin` link points into, the running Python, conda's by `CONDA_VARS`, #827; never `/`, home or
  a secret's parent), SDKs found by variable (`sdk_roots`: `SDK_VARS`, `SDK_DEFAULTS`, #437) and the search paths
  (`search_roots`: `SEARCH_PATH_VARS`, `PYTHONPATH` via `WB_USER_PYTHONPATH`, #667); not the user's
  documents or other projects. Running a program reads it, so one left out can't start (`Permission
  denied`; for OpenCode itself `workbench lane: can't run <cmd>`): add it to `commands.json`
  `paths`. `readable()` walks down from `/` listing the entries beside each excluded one. Found by
  trying: `python3` running gh in the lane was logged in, and the SSH keys were readable.
  On WSL (`hardware.system()`) never the Windows drives (`windows_drives`: DrvFs mounts and any
  `/mnt/<letter>`): a Windows program runs through interop as the user, outside Landlock and
  no-new-privileges. With the lane on their directories leave the agent's `PATH`
  (`context.agent_path`, which also drops `WSL_INTEROP`) and `tool_roots`, and each drive is an
  `ONLY_KEPT` root wherever mounted (#914); `execve` needs read, so one can't run by path. A Windows program
  inside the lane still runs outside it: `wb doctor` says so (`interop_gap`); `[interop]
  enabled=false` closes it (and stops wb reading Windows' GPUs: set `gpu_gb`, #702).
- **API lanes hide the token files that hold a credential** (`API_HIDDEN`, `api_hidden_paths`,
  `api_hidden(task)` for task.json `via: api`; #1021). In an API task's lane every file the agent
  reads and every command's output goes to the provider, so `cat ~/.npmrc`, or a failing install
  echoing its config, would send a registry token there: the files kept readable above are hidden
  there too, but only one that holds a credential (`privacy.holds_credential`, #1021's decision of
  2026-11-23: the owner wants "just works", and a config with no secret in it costs nothing to
  send). The matcher is small and conservative: a line whose key says secret (`_auth`,
  `_authToken`, `password`, `passphrase`, `token`, `secret`, `apiKey`/`api_key`, `oauth`,
  `npmAuthIdent`, `write_key`-style keys, gradle.properties' `gpr.key=`), a `Bearer` or `Basic`
  value, a private key block, a URL with a user in it (pip's `https://user:pass@…`), or a Bundler
  host's login (`BUNDLE_<HOST>__COM`, not `BUILD__`, `MIRROR__`, `LOCAL__`, `PATH__`, `GEM__`);
  a JSON file (composer's `auth.json`, pub's `pub-tokens.json`) is parsed, and a non-empty value
  under such a key (or composer's `custom-headers`) is one. A file that can't be read, decoded
  (UTF-8) or parsed, or is over 1 MiB (`CREDENTIAL_MAX`), counts as holding one; NuGet's `config/`
  folder holds one when any file in it does. Whole-line comments and XML comments don't count
  (Maven's settings.xml template is full of commented `<password>`s), nor a value that is only an
  environment reference (`${NPM_TOKEN}`, `$NPM_TOKEN`). A file that isn't there is not hidden.
  Each path from its tool's docs or source: `.npmrc`, `.yarnrc.yml`, `.m2/settings.xml`,
  `.gradle/gradle.properties`, NuGet's `.nuget/NuGet/NuGet.Config` and `.config/NuGet/NuGet.Config`
  with their `config/` folders, poetry's `auth.toml` (`.config/pypoetry`, macOS `Library/Application
  Support/pypoetry`), composer's `auth.json` (`.config/composer`, `.composer`), pub's
  `pub-tokens.json` (`.config/dart`, macOS `Library/Application Support/dart`), Hex's `hex.config`
  (`.hex`, `MIX_XDG`'s `.config/hex`), pip's `pip.conf` (`.config/pip`, the legacy `.pip`),
  Bundler's `.bundle/config`, bun's `.bunfig.toml`; under `XDG_CONFIG_HOME` when set, and where
  `API_HIDDEN_VARS` (each with its file inside and its tool) move them (`NPM_CONFIG_USERCONFIG`,
  `GRADLE_USER_HOME`, `POETRY_CONFIG_DIR`, `COMPOSER_HOME`, `HEX_HOME`, `PIP_CONFIG_FILE`,
  `BUNDLE_CONFIG`, `BUNDLE_USER_CONFIG`, `BUNDLE_USER_HOME`, and `XDG_CONFIG_HOME` for bun's
  `.bunfig.toml`). The trade is more than private installs: with its file there but unreadable,
  Yarn Berry, Maven, Gradle, NuGet (every dotnet restore, build and test), poetry, composer,
  `dart pub get` and Bundler (`Settings#load_config`: EACCES is a `PermissionError`) stop on every
  run (`API_HIDDEN_STOPS`, each from its source, 2026-11-23), so in an API lane they don't run at
  all while their file holds a credential; npm (an unreadable `~/.npmrc` still ran `npm test`), Hex
  (a read error is no config), pip (configparser skips a file it can't open) and bun (an
  auto-loaded bunfig it can't read is skipped) fail only a private registry's install.
  `wb start` names the files hidden and what that means (`api_hidden_note`, the tool per file from
  `API_HIDDEN_TOOL`, by name or by folder and name for `.bundle/config`): `note: an API task can't
  read ~/.m2/settings.xml, ~/.npmrc (each holds a credential, which would reach the provider): in
  its lane Maven stops on every run, and npm installs from a private registry fail` (`it holds` for
  one). The writable homes `.m2`, `.gradle`, `.hex` and `.nuget` are split around a hidden one,
  what each makes at its top made first (`SPLIT_MADE`: Gradle's `caches/`, `daemon/`, `native/`,
  `wrapper/`, `jdks/`, `notifications/`, `.tmp/`, `build-scan-data/`; Hex's `packages/`; NuGet's
  `packages/`, since dotnet writes `NuGet.Config` on first use), and the same where
  `GRADLE_USER_HOME` and `HEX_HOME` move them (`SPLIT_MADE_VARS`); with no credential there the
  home is granted whole, as on the GPU. The agent's environment
  already drops the matching variables (`SECRET_ENV`).
- *The kernel's side* (`lane.py`, Landlock, no root): the runner starts OpenCode as `python -I
  lane.py --write P… [--reads --read P…] [--unix P…] -- opencode …`; it restricts itself and execs,
  so everything below inherits. Handled: writes, removes, every make, refer (ABI 2), truncate
  (ABI 3), reads with `--reads`, and from ABI 9 connecting to a UNIX socket made outside the lane,
  allowed only beneath `--unix` (`context.unix_paths`): the `SYSTEM_SOCKETS` that exist, which
  hold nothing of the user's (the log, name and user lookups: without avahi a `.local` model server
  failed in the lane, #913), the `paths` grants split as reads and writes are (`--path /run/postgresql`),
  and a granted `SECRET_SOCKETS` file (Docker Desktop's socket: connected to only, the rest of
  `~/.docker` stays hidden, #441). A `--unix` rule carries the socket right alone (its read right
  made a granted `~/.config`'s gh token readable, the 0.90 review); writable entries carry none, so a
  socket in `/tmp` the user's server made is refused and named (`is_socket`, #826). So the session
  bus, `systemd-run`, the keyring and the SSH and GPG agents are out of reach; sockets the lane's
  own processes make stay theirs. From ABI 6 signals and abstract sockets are scoped: the agent
  signals only what it started, while the runner can still stop it. Directories get every
  right; files write, read, truncate and the socket right. No-new-privileges (no setuid: sudo, su,
  snap-confine, so no snap runs; `wb start` refuses a snap `--test`, `wb doctor` names those on
  PATH) is set on every Linux, Landlock or not. Free: listing directories, the network (*What TCP
  reaches*), and metadata (chmod, chown, utimes, xattrs on outside files: only the patterns).
  Fails closed: Landlock present but refusing prints `workbench lane: the kernel refused the lane
  (<error>); not running <cmd>` and exits 126; an exec failure is `workbench lane: can't run <cmd>
  (<reason>)`, 127 not found, else 126. Without Landlock (macOS, an old kernel, an architecture off
  `lane.UNIFIED`, whose syscall numbers it knows) it execs unrestricted but for no-new-privileges.
  Older ABIs hold less (`lane.gaps`): before ABI 3 truncation outside isn't refused; ABI 1 fails
  cross-directory moves and links even inside (EXDEV); before 6 signals, before 9 sockets aren't
  scoped. `wb doctor` draws a real lane and tries a write outside it (`ok agents write only in their
  lane (Landlock ABI N)` or `BAD the lane didn't hold a write outside it: …`), with a line per gap
  and one for WSL interop.
- *What TCP reaches* (#993, 2026-11-22). Landlock's TCP rule (ABI 4) allows ports, never an
  address, so it can't keep the agent from instance metadata (`169.254.169.254`, the machine's role
  credentials over plain HTTP) or a local daemon. The environment turns the SDKs' metadata lookups
  off (`runner.METADATA_OFF`); a program asking the address itself still gets an answer, as do
  Azure's SDKs and Google's Go client (no switch). `wb doctor` warns `--  port N listens here
  (<what>): the lane doesn't stop TCP, so an agent can reach it` for each `context.DAEMON_PORTS`
  port listening (`listening_daemons`, `/proc/net/tcp{,6}` state `0A`, Landlock only): Docker or
  Podman's API (2375, 2376), adb (5037), OpenOCD (3333), J-Link (2331). Bound to a UNIX socket, such
  a daemon is behind the socket rule.
- *OpenCode's side* (`external_rules`): `external_directory` (file tools' paths, the shell's workdir
  and `cd` targets only) is deny `*`, then allow `<dir>/*` for the lane, `READABLE` (`/usr`, `/etc`,
  `/opt`, `/lib*`, `/bin`, `/sbin`, `/proc`, `/sys`, `/nix`, `/run/current-system`, macOS's
  `/Library`, `/System`, `/Applications`), the installs the lane reads (`read_only`: `tool_roots`,
  the toolchains split out of the write lane, `sdk_roots`; never one inside a lane entry), the
  task's `rounds/` (a long prompt is read from there) and the handed skills' directories; with the
  lane off, allow `*` (said outright: the layer's `*_*` MCP rule would match it). The `read_only`
  places also get an `edit` deny (`read_only_rules`), a lane entry inside one its allow back (#521:
  reads there were refused, and the only remedy made the install writable). This is all that applies without Landlock.
- *Asking for more.* A refused write is "Permission denied"; the agent calls `ask_reviewer` with
  `path`, raising a PERMISSION question naming `wb allow <task> --path <dir>` (read and write;
  `--always` keeps it in `commands.json`; `wb start --allow-path` up front). `_lane_dir` refuses a
  path through a link, naming its target (the agent could plant `/tmp/out` -> another project).
  `context.lane_refusal` refuses `/`, home, a place in a secret, workbench's own files or the skill
  library (a place holding one is granted around it), a place in the repo or its git directory
  (the 0.90 review: "allowed", then refused again), a missing path with a suffix (`debug.keystore`
  made a directory broke keytool, #670), and a `kept_out` place (#912: the PERMISSION notice then
  says no grant reaches it). Then it creates the directory. `lane_paths` drops a `commands.json`
  or grant path the same check refuses. `*` turns the lane off for the task; `"paths": ["*"]` in
  `commands.json` for every task.

**Its environment** (`runner.agent_env`) is that of the `wb` command that last started the runner
(`start`, `feedback`, `next`, `resume`), less `CLAUDE_CODE_OFF` (*Skills*), the runner's own
`PYTHONPATH` (`python_path`, below) and credentials: `SECRET_ENV` (names holding `TOKEN`,
`SECRET`, `PASSW`, `CREDENTIAL`, `API_KEY`, `WEBHOOK` and the like; the `ANTHROPIC_`, `CLAUDE_`,
`GH_`, `GITHUB_`, `AWS_`, `GOOGLE_`, `OPENAI_`, `HF_`… prefixes; the SSH and GPG agents and the
D-Bus addresses) and any value that is a URL with a password (`USERINFO`), except `ENV_KEEP`
(configuration under those prefixes: `HF_HOME`, `AWS_REGION`…). `commands.json` `"env": [names]` passes a needed one through (a
provider's key, `DATABASE_URL`). A proxy with a password is one too (#380: installs and tests
failed with network errors that read as the project's): `wb start` notes each dropped `*_proxy`
(`runner.dropped_proxies`, `env_kept`), and a setup or test failing on `NET_FAILED` says so again
(`proxy_gap`: the setup's `failed` notice, status `tests.proxy`), with the `"env"` line.
It adds: `PWD` (the worktree), `OPENCODE_CONFIG`, `OPENCODE_CONFIG_CONTENT`, `PROJECT_CONFIG_OFF`,
`WB_TASK`, `WB_HOME`, `WB_RUN` (for `sweep`); `METADATA_OFF` whatever the user set
(`AWS_EC2_METADATA_DISABLED=true`, `NO_GCE_CHECK=true`, `METADATA_SERVER_DETECTION=none`; not
`GCE_METADATA_HOST`/`_IP`, which only move the server and which Google's Go client takes as being
on GCE); with the lane on its own `XDG_RUNTIME_DIR` and in it `JUPYTER_RUNTIME_DIR` unless set
(every notebook test failed writing its connection file, #668); always `HELM_REGISTRY_CONFIG`
(`<runtime dir>/helm/registry.json`, not made), `K6_CONFIG` (`k6.json`, made holding `{}` with
`O_EXCL`, never through a link) and `SEMGREP_SETTINGS_FILE` (`semgrep-settings.yml`, not made)
(#992, #999: each tool stops on an unreadable file, not a missing one; k6 forgives only a missing
default);
`REMOTE_CACHES_OFF` whatever the user or repo set (#999: `NX_SKIP_REMOTE_CACHE=true`, since
`NX_NO_CLOUD` stops only Nx Cloud while a self-hosted cache was still written, and no flag undoes
it; `PANTS_REMOTE_CACHE_WRITE=false`, `PANTS_REMOTE_EXECUTION=false`, which beat `pants.toml`: reads
stay, and a repo whose toolchain lives only on remote workers fails in the lane);
`POETRY_VIRTUALENVS_IN_PROJECT=true`, `PIPENV_VENV_IN_PROJECT=1` unless set (lane on, #825); build
daemons of its own (`daemon_env`, lane on: Gradle's registry under the runtime dir in
`GRADLE_OPTS`, `SCCACHE_SERVER_PORT` from `free_port` in `SCCACHE_PORTS`, `DAEMONS_OFF` for MSBuild
and Roslyn): shared daemons ran the user's builds under the lane and the agent's outside it (#669,
not yet run against a real Gradle); `MISE_TRUSTED_CONFIG_PATHS` plus the state directory
when the checkout has a `MISE_CONFIGS` file the user trusts (`context.mise_trusted`, `mise trust
--show` run in the checkout: trust is per path, so every shim failed in the worktree, #674; `wb
start` notes an untrusted one); `PYTHONDONTWRITEBYTECODE=1`; `NO_SIGNING` as `GIT_CONFIG_COUNT`
entries after the user's (`git_config_env`: with `commit.gpgsign` set, every test committing in a
scratch repo failed and the base check blamed the project, #666); and the shim's `PATH`. Repository
variables (`gitops.REPO_ENV`: `GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE`…) reach neither the
agent nor wb: `gitops.drop_repo_env` at `cli.main` and `runner.main`, and `gitops._env` per call
(from a commit hook `worktree add` failed on `GIT_INDEX_FILE`; from vcsh `GIT_DIR` sent calls to
another repo, #798). OpenCode's own environment (`runner.opencode_env`, the round's) adds
`OPENCODE_ENV` and the task's OpenCode home (`context.opencode_home_env`: `XDG_DATA_HOME=<home>/share`,
`XDG_STATE_HOME=<home>/state`, and always `OPENCODE_DB=<home>/share/opencode/opencode.db`, over a
user's own, which would point back at the user's database, #1010; and
`OPENCODE_DISABLE_MODELS_FETCH=1`: each server start fetched the models.dev catalog and cached
it in the database, 13.7 MB in every task's home and one download per task, so the catalog bundled
in the binary serves instead: 320 KB, measured 2026-11-23; an API task's own model comes from
`OPENCODE_MODELS_PATH`, `rules/<task>/catalog.json` (`runner.catalog_path`, when `keyhole_of(task)`
has one: *The task's own catalog*). OpenCode 2.3.2 has no switch of
its own for these: `OPENCODE_DB` moves only the database (and the data directory's legacy
`auth.json` is still imported into it), `OPENCODE_TEST_HOME` only `home`; `XDG_DATA_HOME` moves the
data, log, repos and db together (`opencode debug paths`). Those would move every other tool's data
too (uv's Pythons, pipx, poetry, pnpm), so `SHELL` is the shell shim, `rules/<task>/shell/<name>`
(`runner.write_shell_shim`, rewritten each round; outside the lane and off the agent's `PATH`),
named as the shell it runs: OpenCode picks a command's flags by `$SHELL`'s name (zsh's `+o nomatch
-c`, the PTY's `-l`, the rc file, the tool's "Commands run … using <name>"), and a shim called
`opencode-shell` matched none, so under zsh an unmatched glob failed "no matches found" (the #1010
review):
POSIX sh that puts back the user's `XDG_DATA_HOME`, `XDG_STATE_HOME`, `OPENCODE_DB` and `SHELL`
(each unset where the user had none: `SHIM_RESTORES`, values baked in, paths only) and execs the
shell `test_shell` picks, as OpenCode runs every command (`$SHELL -c`). Only what runs through
`$SHELL` gets them back: the formatters and language servers OpenCode starts itself (with its own
environment, `extendEnv`, checked on 2.3.2; formatters only when the user's global config sets
`formatter`, a project's being shut out) see the task's home as their `XDG_DATA_HOME` and
`XDG_STATE_HOME`, so one whose tool keeps its data there (mise, pnpm) looks in the task's home
(the #1010 review; left so: the layer could only turn them off). The tests' doesn't get any
of it. The tests' environment is `agent_env` too, `METADATA_OFF` and `REMOTE_CACHES_OFF` included:
they run the agent's code. Nothing secret goes in OpenCode's environment (`/proc/<pid>/environ`
is readable in the lane): a local server's stored key goes in the 0600 layer (*Found, loaded,
chosen*).

*The user's Python environment* (`context.python_env`, for the agent, setup, tests, base check and
`wb verify`). The checkout's own active env made `import mypkg` load the checkout, so tests passed
on code they never imported (#490). An active env (`python_envs`) is
the checkout's (`checkout_env`, `checkout_envs`) when inside the repo (not when the repo holds
home), in a per-project venv home (`VENV_HOMES`, moved by `VENV_HOME_VARS`), or when a site-packages
file (`EDITABLE_FILES`: `.pth`, `__editable__` finders, egg-links, `direct_url.json`) names the
repo. Such an env leaves: `VENV_VARS`, a `PYTHONHOME` in it or the repo, its `bin` on PATH. Other
PATH directories in the repo become the worktree's, and the worktree's own venv (`own_venv`:
`.venv` or `venv`, package or top) comes first, as `VIRTUAL_ENV`. The shim's real git is looked
up without those worktree directories; OpenCode by the runner's PATH (`opencode_bin`). Any other
active env stays, written by nothing in the lane (step 6). `wb start` names each one left out
(`venv_line`) and, without `--setup`, how to give the worktree its own. Not covered: `pip install
--user -e .`.

*The user's `PYTHONPATH`* (`context.python_path`). The runner's `PYTHONPATH` is the package root, so
ROS, src layouts and the like got none, and naming it in `env` passed workbench's own path (#512).
`context.detached_env` (`spawn_runner`, `bench.start`, `optimize.start`) keeps the caller's value in
`WB_USER_PYTHONPATH` (`USER_PYTHONPATH`; one already set is kept, so a bench replay gets the
user's). `agent_env` gives it back, less workbench's root and a left-out env's directories, repo
directories mapped to the worktree; none left, no `PYTHONPATH`. The carrier never reaches the agent.

**The keyhole** (`workbench/keyhole.py`, #1012). An API model needs its provider's key on every
request, and nothing OpenCode can read is hidden from its shell: its environment
(`/proc/<pid>/environ`), the config layer, `OPENCODE_CONFIG_CONTENT`, and the lane doesn't restrict
TCP. So the key never enters the lane: `keyhole.start(task, m)` loads it (`load_key`: `opencode auth
export <provider>` with the runner's environment and the user's own OpenCode home, stdout read into
memory, a failure's stderr cut to its first line and redacted, a `Key` whose repr is `Key(<provider>,
api key)`) and serves a `Keyhole` from a daemon thread of the runner, outside the lane and dying with
it, on `127.0.0.1:0` (never `localhost`, which may be `::1` first). It refuses
(`KeyholeError("needs a key wb can hold outside the lane: <why>")`, remedy `keyhole.REMEDY`, "store
an API key with /connect in OpenCode") an OAuth login (its tokens rotate: a copy refreshed would log
the user out), an external credential, request signing (SigV4, Vertex: a header swap can't work),
a key only in the environment (`{env:VAR}`, or `auth list` naming a variable: `SECRET_ENV` drops it
from the agent anyway), and a key written in the user's `opencode.json` or `OPENCODE_CONFIG_CONTENT`
anywhere in the provider's entry, V1 `provider.<pid>` or V2 `providers.<pid>` (`_keys_in`: the
provider's, a model's own, a V2 `settings`, a variant's), as an `apiKey` or as a header (any
`headers`: a name with `auth`, `key` or `token` in it, or a value shaped like a key or `Bearer …`)
("its key is written in <file>, which agents can read: store it with /connect instead"). A model's
own key counts because OpenCode 2.3.2 sends a model's settings over its provider's (the review of
#1015: one in the content went unseen, in the agent's environment). A stored key is trimmed (a paste's newline: OpenCode trims only on
`/connect`) and must then be printable ASCII (`KEY_CHARS`), else "can't use <provider>'s stored key:
it holds a character an HTTP header can't carry" (http.client's refusal quoted the header, key and
all, the 2026-11-23 review). The protocol comes from the catalog's provider package (`protocol_of`,
`PACKAGES`: OpenCode 2.3.2's packages whose model routes post the base plus a `MODEL_PATHS` path):
Anthropic's and `anthropic-compatible` take `x-api-key` (and keep `anthropic-version`); alibaba,
cerebras, cloudflare-workers-ai, deepinfra, deepseek, digitalocean, fireworks, groq, meta, minimax,
mistral, moonshot, openai, openai-compatible(-responses), openrouter, togetherai, venice, xai, zai
and zai-coding-plan take `Authorization: Bearer`. Any other package (Google's `x-goog-api-key` and
`/models/<id>:streamGenerateContent`, Azure's `api-key`, Cohere's native `/chat`, gateways with
their own paths or headers, none at all) can't run: `start` raises "needs a key wb can hold
outside the lane: <provider> takes its key in a way the keyhole doesn't speak" rather than a 401
or 404 on every request. The upstream is the catalog's API base (`m.base_url`), never anything
from a request, with what the package appends to it already appended (`mirror_path`: DeepInfra's
`/openai`). OpenCode gets `layer_options(kh)`:
`baseURL` the keyhole's `base` and `apiKey` its `token` (`secrets.token_urlsafe(32)`, new each
runner). The base mirrors the upstream's path on 127.0.0.1, because a package builds its URL from
the base alone (`new URL(trim(baseURL) + path)`, DeepInfra first appending `/openai` unless the base
ends in it), so OpenCode posts to the path it would have posted upstream and the keyhole sends that
path and its query to the upstream's host: DeepInfra `…/v1/openai/chat/completions`, Anthropic
`…/v1/messages?beta=true` (*API models through OpenCode*; checked on real OpenCode in
`RealOpenCodeAPI`). A package that rewrites its base by the host (Azure) would not map.
A request passes, in order, each refusal a JSON error body: the path (a POST to exactly the base
plus one of `MODEL_PATHS`, `chat/completions`, `responses`, `messages`, `messages/count_tokens`,
no segment between, since OpenCode never posts one and `…/threads/<id>/messages` carried the key
to OpenAI's Assistants API; `GET <base>/models` answered with only the task's model, nothing
forwarded; else 404 `keyhole: <path> isn't a model request`), with a query of printable ASCII
only (`QUERY`; else 400 `keyhole: a query that isn't printable ASCII`: a control character made
http.client fail and marked the model unreachable); the token (`Authorization: Bearer` or
`x-api-key`, `hmac.compare_digest`, never in the URL, which reaches OpenCode's error text and
runner.log; 401 `keyhole: not this task's token`); the body (`BODY_CAP`, 32 MB, else 413 unread,
a chunked body checked before each chunk, each size line hex of at most 8 digits (`CHUNK_SIZE`,
`LINE_CAP`) else 400; UTF-8 JSON with no NaN, else 400; its `model` must be `Model.name` and it
may hold no `models` fallback list, no duplicate key anywhere, and no other top-level key that
folds (NFKC, `casefold`) to `model` or `models`, since Go's encoding/json matches keys
case-insensitively and some parsers keep the first duplicate, so a body read here as the task's
model went on to route elsewhere (`_strict_json`, the 2026-11-23 review): 403
`invalid_request_error` "keyhole: this task may use only <id>", which OpenCode doesn't retry,
logged once per model as `keyhole: refused <model> (this task runs <id>)`). Then the body goes on
as it is, hop-by-hop and every client auth header (`authorization`, `x-api-key`, `api-key`)
dropped and the real one added, `Host` the upstream's, `Accept-Encoding: identity`, on a new
connection per request (`ssl.create_default_context()`, through wb's own `https_proxy` by
`CONNECT` unless `NO_PROXY` covers the host or it is loopback; the timeout the task's silence limit
+ 60 s). A 2xx comes back as it arrives, SSE a whole line at a time (a partial line waits for its
end), its `data:` lines read on the way for the served model (`model`, Anthropic's
`message_start.message.model`, the Responses API's `response.model`) and OpenRouter's `provider`:
status `served: {model, by}` when it first appears or changes, with a `served` event; and `usage`
(top-level, `message.usage`, `response.usage` in `response.completed`) for `on_usage` (for the
meter, *Spend*). The key itself never passes (a body that isn't SSE holds back its last
`len(key) - 1` bytes so a key split across reads is caught), and an SSE line whose JSON has an
`error` goes through `redact`: a mid-stream error arrives inside a 200 (Anthropic's `event: error`,
OpenRouter's `data: {"error": …}`). An error is read whole (`ERROR_CAP`, 64 KB; a longer one is cut
and loses twice a key's length, at least 128 characters, from its end, so a key straddling the cut
leaves no prefix) and passed through `redact` (the key, its first 8 and last 4 characters,
`KEYLIKE`: the `sk-`/`sk-ant-`/`sk-or-`/`xai-`/`gsk-`/`dsk-` shapes, `Bearer …`, any run of 32 or
more key characters, each `[key redacted]`, in one pass): OpenCode prints an upstream body
verbatim. A transport failure is a 502 `keyhole: can't reach <host>: <reason>` (on macOS a
certificate failure names Python's Install Certificates command); a request http.client won't send
(`InvalidURL`, a `ValueError`) is a 502 `keyhole: can't send this request on (<type>)`, the type
only and never `unreachable`. Anything else a handler raises is one line in the log, `keyhole: a
request failed (<type>)` (`_Server.handle_error`), never socketserver's traceback on stderr. Each outcome goes to
`on_health(state, why, until)`: 2xx `ok`; a quota answer (below) or `insufficient_quota` `no_credit`; a 429 or 529
`rate_limited` until `Retry-After`, else the hold's backoff while it holds (below), else `RATE_LIMIT_SECS` (60 s); 5xx and transport failures
`unreachable` (the model's health record takes it with #1013). The start line, `keyhole:
<provider> on 127.0.0.1:<port> for <id>`, the refusals and that failure line are all it logs (the runner's stdout,
runner.log); no request is logged, and no line holds the key or the token. Accepted: the agent's
shell can reach the keyhole too, holding the token (the layer is readable), and spend on the task's
model only, while the runner lives. Without Landlock the user's OpenCode store is readable
anyway; API tasks run there all the same, with no warning (the owner's call on #1008 and #1012:
it just works). Not yet wired: status `served` reaching the results.

*How the runner uses it* (#1015). `drive` resolves the model and, for an API one that needs a
key (`runner.needs_keyhole`: not an Ollama cloud model, whose `key` is `ollama`, nor a free
model whose `key` is `none`), starts it before the first round as `CURRENT["keyhole"]`
(`start_keyhole`: `on_health` is `models.note_api(provider, state, why, until)`, the model's
health record; `on_usage` and `room` are the task's meter's, `meter.Meter.hooks`, #1019) and closes it when
the drive ends. A `KeyholeError` fails the task through `_load_failed` (`<model> can't run:
<why>`, a `failed` notice) with `keyhole.REMEDY` appended unless the text already gives the
remedy (`keyhole_failure`). The layer's provider entry gets `keyhole_overlay(task)`:
`provider.<pid>.options` from `layer_options`, and the same base and token as the task model's
own `provider.api` and `options` (`models.<id>`, with the variants, below) where the model has an
entry anyway: the task's catalog file, or one in a config file or the user's content, V1 or V2
(`models.has_model_entry`); an entry of wb's alone made OpenCode a model its catalog lacks, with
none of its limits, where it should be "Model unavailable". Merged last over
`agent_providers`, the timeouts and a stored key; `rules_env(layer, keyhole_over)` says it again
in `OPENCODE_CONFIG_CONTENT`'s provider overlay, which loads after the user's own content, so a
`baseURL` there can't send the task's requests elsewhere. The model's own entry matters because
OpenCode 2.3.2 sends a model's base over its provider's (`v1/config/migrate.ts` `migrateModel`:
`provider.api`, `options.baseURL`); restating only the provider's, a model entry in the user's
global file or content sent the token to that base (the review of #1015); the keyhole forwards
to the base the user's `api model.list` gave the model, theirs included. And where the user's
content has a V2 `providers.<pid>` for the keyhole's provider, which replaces the V1 entry beside
it in one document (`normalize.ts` `mergeMaps`), `keyhole_v2` puts the same base and token in it
(`settings`, the model's `settings`). `RealOpenCodeAPI` runs each form (content and global file,
V1 model entry, V2 provider and model) against a decoy that must see nothing. `agent_env` adds `127.0.0.1` to
both `NO_PROXY` and `no_proxy`, each set to the union of the two as the user had them (Python
and curl read `no_proxy` first, Go `NO_PROXY`, so one extended alone dropped the user's hosts from
the other), so a proxy in the agent's environment never catches the keyhole. `small_model` is the task's model (with its thinking variant, #1018), as for every task, and `run --title`
skips the title request: OpenCode sends nothing to another model. An upstream error reaches
`events.jsonl` only as the keyhole redacted it, and a round that died on one says it in status
`last` (`opencode exited 1: <error>`, API tasks only); a refused key or a spent balance the
keyhole saw ends the round first as the provider's `auth` or `quota` stop, whose line names it
(`<provider> refused the API key (HTTP 401)`, *The hold*, #1020).

*The task's own catalog* (`rules/<task>/catalog.json`, #1013's comment, #1015). The agent's
OpenCode fetches no catalog (#1010), so a model newer than its binary's bundled snapshot was
unknown to it. `write_catalog` writes `models.catalog_doc(m)`, the model's entry from the user's
`api model.list` (hosted.json) rebuilt as a models.dev v2 catalog of one provider and one model,
outside the lane's writes, and `opencode_env` points `OPENCODE_MODELS_PATH` at it (the fetch
still off). OpenCode reads that file in place of its bundled catalog (`models-dev.ts`: the file
first, the snapshot never) and stores none of it: the database stays about 300 KB, as with the bundled catalog's 320 KB
(the 13.7 MB was the fetched catalog, off for every task). The file leaves `reasoning` unsupported, so it makes
no variants of its own; the entry's variants go in the keyhole overlay as V1
`provider.<pid>.models.<id>.variants` (`catalog_variants`: `{id: settings}`). A V2
`providers.<pid>` beside the layer's V1 `provider.<pid>` would replace it whole, keyhole options
and all (`normalize.ts` `mergeMaps`), and the request went out with no key. On OpenCode 2.3.0
and 2.3.2, `api model.list` then prints the entry field for field as the user's OpenCode does
(`RealOpenCodeAPI`), but for its `limit`, which is the capped window and output the task plans to
(`models.api_limits`, #1017), and a model the binary lacks runs. `catalog_doc` returns None, and the
bundled catalog stays, for a model that isn't the catalog's (a config entry, an Ollama cloud
model), an alias (its id isn't its provider's), or one with headers or a body of its own or a
variant's, which the v2 file or a V1 variant can't carry. A model with no keyhole (a keyless free
one, `key: none`, or an Ollama cloud model) gets no file either: `write_catalog` and
`OPENCODE_MODELS_PATH` go with `keyhole_of(task)`, and the variants with the keyhole overlay, so
such a model newer than the binary is still "Model unavailable" to the agent.

**The hold** (#1020). `read_error` sorts an upstream error: 402, or a 429/403 whose body says the
balance or plan is spent (`QUOTA`: `insufficient_quota`, OpenRouter's `limit_source`, a 5-hour or
weekly limit), is `quota`; 429 and 529 (Anthropic's overloaded) `rate_limited`; 500, 502, 503, 504
`outage`, and so are Cloudflare's 520-524 in front of a provider, as transient (`HOLD_CODES`; another
5xx, a 501, a 505, a 525, goes back at once, its health
still `unreachable`), as are the transient transport failures (`_transient`: refused, reset,
DNS, TLS, a timeout; not a certificate that doesn't verify nor a proxy refusing the tunnel, said at
once). `rate_limited` and `outage` are held: the keyhole waits `Retry-After` (seconds or a date),
else `BACKOFF` 2, 4, 8 … 60 s, and sends again (the provider's cooldown recorded as that
backoff, not `RATE_LIMIT_SECS`, or its own next pass would wait a minute), one OpenCode request
for at most `RATE_HOLD_S` (300 s, `hold=` per keyhole); a wait past what is left of it passes the last answer back at once, redacted,
with its `Retry-After`, and OpenCode prints `retry` and sends again, held again. Before each send it
waits out the provider's cooldown in `api-health.json` (`cooldown`: another task's 429 on the same
account, decision 10's three tasks a provider). It naps in 0.25 s steps and drops the request when
OpenCode hangs up (a stop, a steer) or the keyhole closes. Each held answer writes status
(`held`): `ratelimit_until` (now + the wait, at least `HOLD_FLOOR_S` 60 s ahead) and `ratelimit
{kind, provider, code, since, retry_after, why?}` (`since` kept while the run goes on; the code,
never the body), and `last` (`rate-limited by <provider> (HTTP 429): waiting, retry in 40 s`). A
200 whose SSE carries only keep-alive comments for `QUEUED_AFTER_S` (30 s) is `queued`, marked again
each 30 s; the first event (or a non-SSE body's first bytes) clears both keys (`answered`). `quota`
writes status `api_stop {guard: "quota", text: "<provider> says the account is out of credit (HTTP
402)"}` and answers OpenCode 400 `keyhole: <that>: the round stops`, which it doesn't retry. The
cooldown file the issue named `api-cooldown.json` is #1013's `api-health.json` (`note_api`, through
`on_health`), which `status()` already reads for "rate-limited by <Provider> until HH:MM", with no
request.

**OpenRouter, routed by the keyhole** (#1021). One OpenRouter id is served by many backends that
differ in quantization and data policy, and its `allow_fallbacks` is true by default, so a request
could land on a backend nobody chose. The keyhole's `order` (task.json `order`, the role's, read by
`start`) is set on every OpenRouter request's body (`Keyhole.route_body`): `provider.order` the
role's backends and `allow_fallbacks: false`, over whatever OpenCode sent; the rest of its
`provider` object (`data_collection`, `zdr`, `only`, `ignore`: each only narrows) kept, and with no
order chosen only `allow_fallbacks: false`. Any other provider's body goes on byte for byte. The
layer also records the same under the task's model (`runner.openrouter_routing`), so `wb watch`'s
readers see it. Checked in OpenCode 2.3.2's source (2026-11-23), the V2 path the spike left open:
a V1 model entry's `provider.openrouter.models.<m>.options` becomes the model's `settings`
(`core/src/v1/config/migrate.ts`, `ConfigProviderOptionsV1.model` copies every key), the OpenRouter
package's `model(id, settings)` passes every key but `apiKey`, `baseURL`, `body` and `headers` on
as `providerOptions` (`ai/src/providers/settings.ts` `toConfig`), and `providerOptions.provider`
is the body's `provider` (`OpenRouterProviderRouting`: `order`, `allow_fallbacks`, `only`,
`ignore`, `data_collection`, `zdr`…; `ai/test/provider/openrouter.test.ts` sends
`allow_fallbacks: false` so); a real run against `tests/capture_llm.py` carried `{"order":
["baseten"], "allow_fallbacks": false}`. The keyhole stays the guard: `loadCatalogPackage`
(`core/src/model-resolver.ts`) overwrites `settings.provider` with the provider's `canonical` id
whenever the catalog or config sets one. A response whose `provider` is outside the order
(`privacy.outside_order`: OpenRouter answers with a display name, `DeepInfra`, the order holds
slugs, `deepinfra/fp8`, compared folded, through `OPENROUTER_SLUGS` for the five names of its
2026-11-23 list that fold to something else, `Google` for `google-vertex` among them) is a
`backend` event, `OpenRouter served <x>, not <order>`, and a `NOTE:` line in that round's report
(`runner.backend_lines`). `data_collection` stays the user's OpenRouter account setting.

**Project config can't lift the rules.** OpenCode loads a project's `opencode.json` (worktree,
`.opencode/`, any directory above) after `OPENCODE_CONFIG` and appends its permissions, so an
allow-all there won (the 2026-11-16 audit: the agent could write one for its next round). The rules
then also went in `OPENCODE_CONFIG_CONTENT`, which capped them at 120,000 bytes (#994). Now
`rules_env` always sets `PROJECT_CONFIG_OFF` under both names (`OPENCODE_DISABLE_PROJECT_CONFIG`
and `OPENCODE_CONFIG_PROJECT_DISABLE`, which 2.3.2 reads first, so a user's `=0` there would undo
it), the rules live in the layer file alone whatever their size, and wb hands back what the switch
drops. `opencode debug config` then lists the global files, the layer and the content (2.3.2).
`CONFIG_CONTENT_MAX` (120,000 bytes; one environment string holds 128 KiB) bounds only the content: past it the
user's part is left out (`test_the_rules_stay_out_of_the_environment_however_long_they_grow`).
The user's own `OPENCODE_CONFIG_CONTENT` (`models.users_config_content`, #347) goes under the
rules: its `permissions` and 1.x `permission` (`USERS_RULES`) move into the layer file before the
rules (left in the content, a user's `{"*": "allow"}` lifted every rule: #994's review,
`test_the_users_own_config_contents_old_style_permission_lifts_no_rule`); its other keys stay but
`mcp` and those the layer sets; `provider` merges, the layer's timeouts on top (#491); `share` is
`disabled`. `RESTATED` (`small_model`, `autoupdate`, `compaction`, `tool_output`) goes in the
content too (#519), so should a release load a project's config despite the switch it still
can't change them (a project's `compaction.auto: false` left no backstop past the handover; its
`autoupdate` moved OpenCode past `OPENCODE_TESTED`); so do the task model's `limit` and
`options.max_tokens` (`model_pins`, never a `baseURL` or key: a wider window moved compaction off
the planned handover). The layer's `small_model` is the task's model (its `opencode_model`, #1018), which is how `rules_env`
finds it. With the probe experiment (*Experiments*) the layer sets `tool_output`
(`runner.output_cap`, #857): `max_bytes` `OUTPUT_CAP_SHARE` of `usable_input`, within
`OUTPUT_CAP_MIN`..`OUTPUT_CAP_MAX` (OpenCode's default), `max_lines` per `OUTPUT_CAP_LINE`. It
bounds the shell's output, not the built-in `read`, so the probe note says how to read on. The `edit` rule also denies `OPENCODE_FILES` (`*opencode.json`,
`*opencode.jsonc`, `.opencode/*`, `*/.opencode/*`), and the report flags them with Claude's files.

**The task's thinking variant** (#1018, *Thinking off by default*). The layer's `small_model` is
the task's `opencode_model`, variant and all: `small_model` takes `<id>#<variant>` (checked on 2.3.2;
pinned to the bare id, the title went to the catalog's lowest variant, and a model with none
listed would think on the title). `model_pins` reads the id before the `#`. `wb-off`
(`runner.thinking_layer`, from task.json `thinking_off`) goes under the task's model: a settings
variant in V1 form, `provider.<p>.models.<m>.variants.wb-off`; a body field (DeepSeek's
`thinking`) on a catalog provider only in V2, `providers.<p>.models.<m>.variants: [{id: "wb-off",
body}]`, since V1 keeps only keys the package has a setting for (checked 2026-11-23 on 2.3.2: a V1
`thinking` on the catalog's DeepSeek left the agent's request without it; the title request carries
`thinking: disabled` of OpenCode's own whatever the variant, so only the request with tools tells).
A config provider on an npm package (`@ai-sdk/openai-compatible` at DeepSeek's host) sends V1 keys
in the body as they are, so its `wb-off` stays V1. A document that holds one provider in both forms
loses the V1 entry (checked: `provider.deepseek.options.baseURL` beside `providers.deepseek` went
unread, the request to DeepSeek's own host; a V1 entry in another document, the user's global
file, holds), so that provider's whole entry moves to V2 with it (`runner._as_v2`: `options` as
`settings`, `npm` as `package`, which V2 needs or fails "Unsupported package", a V1 `variants`
object as a list, a model's own `provider.api` (the keyhole's base, *The keyhole*) as its
`settings.baseURL`, the form `keyhole_v2` uses); other providers stay V1 beside it, and V2 variants merge with the catalog's by
id (its `#high` still resolved).
`restate_thinking` says it again in `OPENCODE_CONFIG_CONTENT`, as the model's pins are, its
provider moved to V2 there too with only its timeouts (never a key or baseURL). A catalog `none`
needs nothing in the layer.

**The user's MCP servers stay out.** OpenCode merges the user's global MCP servers into every
agent: one with the user's token would reach every agent, and a remote one runs in OpenCode's
process beyond the lane and `SECRET_ENV`. The layer replaces each with one that can't run
(`runner.users_servers_off`: `{"type": "local", "command": ["false"], "enabled": false}`; OpenCode
2.3 starts a server saying `"enabled": false` all the same), and its permissions start with
`MCP_RULES`: deny `*_*`, allow `workbench_*`, allow `doom_loop`. It sets `"share": "disabled"`,
`small_model` to the task's model (a user's cloud one would get the task's text for titles) and
`"autoupdate": false` (#353). The user's plugins still load: theirs, not the agent's to change.

**What the agent writes is not trusted.** The lane writes only the worktree and `ask/`; grants,
rules and config live in `rules/<task>/`; the runner checks and raises the ask server's questions.
wb writes into `ask/` through `ask_mcp.write_answer` (a temp name, then a rename that replaces a
link), and reads ask files without following links or FIFOs, up to `FILE_MAX`. The acceptance
command runs in the agent's lane and environment, so its code gains nothing when tested.

**Claude's files.** The `edit` permission denies `*CLAUDE.md`, `*CLAUDE.local.md`, `*AGENTS.md`,
`.claude/*`, `*/.claude/*` and the skill library (`<library>/*`); a shell change shows in the round
report. For a change to CLAUDE.md or AGENTS.md the protocol has the agent write the whole new file
as `<name>.proposed` (`runner.PROPOSABLE`, `runner.proposals`: new, untracked at the step's start,
from `git ls-files --others --exclude-standard`, never a link). One the project tracks is its own
file (accept once deleted a team's committed draft, #321). The review notice shows each as a diff
(`proposal_diff`; `review_diff` leaves exactly those out of the main diff by literal path, #79), and
`wb accept`/`wb next` settle them around the commit (`cli._settle_proposals`): `--take-claude-md`
puts each over its file, else it is dropped; never committed as is. A failed commit restores every
proposal and replaced file, and "took"/"dropped" print only once the commit stands (#507). A
proposal for a gitignored file is written over the checkout's copy, or `--take-claude-md` is
refused first where that copy can't take it (#506). A link in the worktree (`AGENTS.md` ->
`CLAUDE.md`) pairs its proposal with the target, so the link stays; `runner.link_problem` refuses
two proposals for one file or a link out of the worktree (#871). Claude making each change
itself cost a pass over its whole context (2026-11-18).

**Git, read-only** (`gitguard.py`; *Git, commits and projects*). `runner.agent_env` writes a `git`
of its own into `rules/<task>/bin/` and puts it first on `PATH`.

**Skills**: the project's (`project_skill_dirs`, listed in `skills` since OpenCode no longer finds
them, #994) and the library skills Claude handed the task (one directory each). Personal
`~/.claude/skills` (synced app skills, `synced/*/*/SKILL.md`, too) and `~/.agents/skills` (#371)
are denied by name (`denied_skills`: an agent read a personal skill as a "newer task" and went into
the user's files), except a name a project skill directory also has, or a handed library skill's
(#515: denied by name, every load failed); `workbench` and task.json `deny_skills` always are. `wb
skill add` refuses the personal names; `wb skill list` says when one shares a library skill's name.
`OPENCODE_DISABLE_CLAUDE_CODE` or `_SKILLS` makes OpenCode skip every `.claude` directory silently
(#384), so `runner.CLAUDE_CODE_OFF` drops both from the agent's environment. Claude's own skills
and plugins never reach the agent: Claude applies the ones that fit and carries them into the spec
(*The skill library*). Workbench names no plugin: which skills the user has is Claude Code's.

**`ask_reviewer`**, an MCP tool for one blocking question to Claude (*Talking to a running agent*),
listed as `workbench_ask_reviewer`.

**Browsers and the web.** Workbench brings no browser; Claude's are its session's, the user's
stated preference first (the skill's). Research is Claude's: the agent's web tools
are denied and `curl`/`wget` aren't listed; what it needs comes in the spec or an answer. Those are
rules on its tools, not the network: the lane leaves it open, so what keeps that cheap is that
credentials stay out (`SECRET_ENV`, `SECRETS`). A browser as a test tool runs in the lane
(`playwright` listed, browsers in `~/.cache`, `/dev/shm` in `DEV_NODES`; Chrome's own sandbox works
inside it, checked 2026-11-17 on ABI 10); headless Chromium needs `--user-data-dir=$(mktemp -d)`
(the handbook). A bare browser isn't listed (it fetches any URL, like `curl`): `wb allow <task>
"chromium *" --always` hands every task one. A snap browser can't run.

**The writing role** (#1025; the roles: #1014, `wb start --role`: #1016). Prose (docs, READMEs,
guides, release notes, stories, copy) runs under its own rules, not the coding ones: task.json
`role: writing` picks them. What changes, and why:
- **The handbook**: `agent/WRITING.md` (under 3,000 bytes, `tests/test_docs.py`: every writing
  prompt carries it) in `rules/<task>/HANDBOOK.md`'s place (`context.handbook_for`), so the
  instruction path and `fixed_prompt`'s "the handbook" hold. It covers reading the brief and the
  files near it for voice, never inventing a fact (`[TK: what is missing]`, listed in the
  summary), not reflowing untouched text, and checking links; the coding handbook's callers,
  helpers and tests would be prefill a writer can't use.
- **The protocol**: `cli.WRITING_PROTOCOL`, at `wb start` and `wb next` (`cli.protocol_for`),
  sharing `PROTOCOL`'s fold (*Two protocols, one fold*).
- **Self-review off** unless `--self-review` (`cli.self_review`: task.json `self_review: false`,
  which `drive` hands `tuning.tune` as a fixed value): it mutates tests a prose task hasn't, a
  round (on an API model, money) for nothing. A writing row is no evidence of the knob
  (`tuning.acted`), so prose never turns the coding check off.
- **Probe off** (`context.PROBE_OFF_WRITING`, `experiments_off`), with no repo scan: no code to
  navigate. Its note and protocol line are left out.
- **`--test` optional**: none, and the round goes straight to review (no base check, no
  auto-fix), as for any task without one. With the project's own offline prose check (a docs
  build, a spell or style lint) it runs as any task's test: base check, auto-fix, in the lane. A
  checker that needs the network fails there. wb looks for none: Claude picks it (SKILL.md doesn't say so yet).
- **The record apart**: results rows carry `role`; `router.score`, `rank`, `choose` and
  `expected` count a model's rows of the task's role (`router.of_role`: `role`, else `size`), so
  prose results never move a coding rank or the other way round. Only a writing task asks by its
  role (`router.record_role`, in `router.choose` from the role `routes.resolve` passes, `progress` and `wb watch`'s clock): #1016 writes a
  role for every task, and a coding one is ranked and timed on its size's record, as before roles. With none yet, every model starts
  from the prior. `tuning.record` sets each row's speed against its own kind's median (`tuning.work`,
the key `of_role` uses), so quick prose steps never mark coding steps slow; writing rows still
count toward `fresh_at`, one knob per model and window. The slice is shared (`router.slice`:
decision 1, writing slices like significant).
- **Unchanged**: the lane, the shell allowlist, the guards (40 calls without an edit still stops a
  writer that only reads), `NUDGE`, the ask server, handovers, feedback and resume (plain review
  notes), the lessons (Claude's raw material, never handed to an agent by role), and the savings
  estimate.

## Talking to a running agent

Claude is the driver, not a spectator, and the agent is a teammate it can reach at any time.

**Questions** (`workbench/ask_mcp.py`, launched as `bin/wb-ask-mcp`). A stdio JSON-RPC MCP server
OpenCode starts with each `opencode run`, as `mcp.servers.workbench` (`type: local`, `codemode:
false`, `timeout.startup` 15 s, `timeout.execution` the ask timeout plus a minute, `environment`
`WB_TASK`, `WB_HOME`). It answers `initialize` (the client's protocol version, else `2025-06-18`),
`ping`, `tools/list`, `tools/call`; one tool, `ask_reviewer {question, context?, command?, path?}`. `tools/list`
touches `ask/.listed` (`LISTED`; removed by `run_round` before each OpenCode starts): a clean round
without it reports `NOTE: the agent's OpenCode never listed workbench_ask_reviewer, so it can't ask
you anything` with the `opencode mcp list` command (2026-11-19 audit, #481).
The server runs in the lane and writes only `ask/`. It rejects a bad `command`/`path`
(`bad_request`: one line, at most `LINE_MAX` 4096; a path absolute) or an empty `question`
(`NO_QUESTION`, the runner's `_judge` text), and past a budget refuses (a courtesy, counted as the
runner counts: status `asks` plus unjudged ask files, #831). Else it writes `ask/<id>.json` (`{id,
ts, round, question, context, command?, path?}`) and blocks (a `Watcher`) until `ask/<id>.answer`
or `--ask-timeout` (20 min), then returns the answer or `NO_ANSWER`. Both sides' waits use
`ask_mcp.Waited` (monotonic, less steps over `FROZEN_SECS` 60 s; the server looks every `STEP_SECS`
30 s), so a suspend doesn't expire a question (#453). A failing call returns `isError: true`,
`ask_reviewer failed: <why>; carry on without an answer and say so in your summary.`; a failing
request gets `-32603` with its own id, never `null` (OpenCode drops those and the call hung, #525).
Requests and ask files parse with `store.loads` (lone surrogates become U+FFFD). `tools/call` runs
on its own thread, so `ping` is answered while a question waits; the silence guard ignores the wait.

The runner relays (`ask_mcp.relay`, each tick, woken by the steer bell). Outside the lane it reads
new ask files (no links, at most `FILE_MAX` 256 KiB, `NEW_MAX` a tick, `ASKS_MAX` in all) and
counts each in status `asks` (`{id: "q" | "p" | "x"}`, out of the agent's reach, so deleting files
returns no budget): questions up to `--ask-budget` (2) across all steps, command/path requests up to
`PERMISSION_BUDGET` (8) apart. A refused one (`x`) is answered with why, without waking Claude.
Otherwise an `ask` event and a `question` notice: `Q: …`, or `PERMISSION to run: `cmd`` /
`PERMISSION to use the directory D (outside its lane)` with the `wb allow` that grants it. The
question is clipped at `QUESTION_CHARS` (2,000), context at `CONTEXT_CHARS` (4,000); continuation
lines are marked `> ` so none passes for workbench's own; `command` and `path` are never clipped.
The phase is `asking` while any question of the round is open (`status.asking` the oldest), set
before the notice is raised; an answer (an `answer` event) or the timeout closes one, and with none
open the phase is `running` again. Each open question's notice gets its own mark (two in one tick
shared one, and acking the first acked both). A question dropped
(timeout, round over) is closed with `NO_ANSWER` as its answer (`ask_mcp.close`; lessons skip it).
However a round ends, `_end_agent` closes every open question (`ask_mcp.close_all`) and clears
`asking`, as `cli.reap` does for a dead runner, and closes never-relayed ask files as `x` (#680:
left, the next round raised one live and held `asking`, pausing every guard). The `wb next` prompt
names the questions left (`ask_mcp.used`).
`wb answer <task> --text|--file` refuses a closed question (`ask_mcp.is_open`); `wb allow` answers
the oldest open permission request (`_permission_asked`), else the question. Every answer goes
through `ask_mcp.answer_once`: the open check and `write_answer` in one step under
`locks/<task>.ask.lock`, as do the runner's `close`, its refusals and the bench's; the loser gets
"<id> was already answered (or closed with NO_ANSWER)", exit 1, status `answered` untouched (#938:
apart, a late answer overwrote `NO_ANSWER` and counted as answered).

**Granting.** `wb allow <task> "<pattern>"… [--path DIR…] [--always]` adds to
`rules/<task>/grants.json` (`context.grant`), and with `--always` to `commands.json`
(`context.allow_always` via `store.edit_config`: locked, through a symlink, mode kept, #495;
refuses a malformed file). It answers a pending question "Claude allowed … Run it again now and
carry on." and, if the agent runs, queues it as a `--now` note: rules are read when OpenCode starts.

**Notes.** `wb steer <task> --text "…"` (queued, running, testing or asking, live runner) is
delivered when the current run ends, before the tests. `--now` wakes the runner (`_steer_bell`),
stops the agent and starts the next round with the note first, marked overriding. During tests it
waits; while waiting for the GPU it rides the round's prompt (`runner._early_notes`), or after the
tests under `--unload-for-tests`; `cli._when_delivered` says which. An undelivered note goes to the
stuck report; `wb resume` carries it, `wb feedback`/`wb next` drop it and say so.

**Check-ins.** `--checkin MIN` raises a `checkin` notice every MIN minutes of a round with a digest
(*The runner, inside*); Claude reports briefly, logs with `wb note` (a `captain` event), steers if
needed. Off by default: each wake re-reads Claude's whole context (the 10-minute default cost 500K
of a task's 1.2M tokens, 2026-11-17). Nudges, repeated handovers and a cut-short base check raise
one regardless; a session that fills up reading is a `reading:` report line.

**Progress checks** (`workbench/progress.py`, #95): opt-in, `wb progress on [--after MIN]` / `off`,
`$WB_HOME/progress.json` `{on, after?, ts}`; `after` survives a bare `on`/`off` (`set_enabled`,
#578), `--after 0` clears it.
- **Active work**: each round adds its time less answers and frozen time (`_Clock.ran`) to status
  `active_s`, on or off; GPU waits never count; results carry `active_minutes`. Per step: `wb next`
  stores `step_active_s` and resets `progress_checks`; checks and the ETA use
  `runner.active_so_far` (#815).
- **The check** fires once at the threshold, twice it, four times… (`progress.due`,
  `progress.passed`, status `progress_checks`), never while asking. Threshold: `--after`, else the
  median `active_minutes` a step for size and model (`progress.typical`, `progress.threshold`,
  `tuning._step_minutes`, as `router.expected`) once `TYPICAL_MIN` (3) are timed, at least
  `FLOOR_MIN` (10); before that `DEFAULT_MIN` (20 trivial, 45 significant) (#724: with no history
no check came, and a 1.5-minute typical made every 1.5 minutes a wake). The pane's
  sentence repeats `DEFAULT_MIN` and `TYPICAL_MIN`: change them together. It is a `checkin` notice
  (`progress: true`): the digest, then `progress.text` (active minutes vs threshold, files changed
  and whether any since the last check, status `progress_mark`, failed tool calls, last tests) and
  one recommendation: a hint (`wb steer --text`) or a smaller step (`wb steer --now --step --text`,
  opening with `STEP_NOTE`).
- **The recommendation**: after `MIN_TRIES` (2) of each kind on the model, whichever helped more
  (`progress.recommend`); before, a smaller step if nothing changed since the last check, else a
  hint.
- It never authorizes an edit: nothing in it touches `coding.json`.
- **Learning**: the first `wb steer` after a check (status `progress_open`) goes in status
  `interventions` `[{kind: hint|smaller, ts, round, step}]` and the results row;
  `progress.helped(model)` / `helped_by_model(rows)` (over `tuning.judged` rows) count tries and
  landings without a takeover (`tuning.agent_landed`); the next check cites them.

**Stats recording** (`workbench/stats.py`, #404) keeps what only the Stats tab reads. The user's
rule (2026-11-20): a free measurement is on by default, and each can be turned off. On unless
stats.json says `recording: false`. With it on: a `round_end {secs, how, guard?}` event (`how`:
`done`, `stuck`, `handover`, `interrupted`, `failed`; `runner._round_end`); the stopping guard named
(`repeat`, `errors`, `no_edit`, `round`, `silence`, `think`, `handovers`, `spend`) and counted in status
`guards`; speed samples with time to first token (`router.speed_samples`) in status `speeds`; and
`results.for_stats` in the row (`started_at`, `load_secs`, `setup_secs`, `nudges`, `asks` by kind,
`answered`, `guards`, median `speed`, `gpu_w` `{mean, peak}` from status `gpu_w_*`, each only when
measured; `_Power` keeps `gpu_w_*` on or off). Off, none of these is written; the `how` is shown by
no reader. What the router, tuning, savings and progress read is outside the setting. `live` and
`every` drive the live view (`wb top`).

**The Stats feed** (`stats.feed`, `wb stats --json`, #405): one object of everything learned. Rows:
`store.results()` kept by `tuning.judged`, except where Claude's cost is counted (savings, saved
milestones): every non-trial row. An empty section is left out, never zeros (a new install: `{ts,
until, settings, tasks: 0}`); `until` is the next local midnight (`_midnight`), when the pane
re-reads. Keys: `periods` (`week`, `month`, `all`, `savings.PERIODS`: tasks, landed, `accepted_pct`,
`active_minutes`, `savings` with `score`, `spec_gaps`, `energy` `{wh, cost?}` from every row's
`gpu_wh` that measured them, another machine's GPU and API rows left out, #568, #569; `api`, below), `streak` `{current, best,
days}` (local days with a task landed; the current run ends today or yesterday, else 0, and an
ended run is never mentioned), `bests`
(`largest_first_try` by `tuning.agent_reviews`, #550; `fastest`; neither with a `took_over` step,
#566), `milestones` `[{kind: landed|saved, n, day}]`, `absorbed` (`auto_fixes`, `auto_fixed_tasks`,
`handovers_landed`), `questions` `{asked, answered}` (only `wb answer`'s, #562), `models` (the `wb
stats` columns, `claude`, `speed`, `write_trend`, and from `TREND_MIN` 6 rows `write_change_pct`),
`model_lines` (#638: `{lines: [{model, variants, windows, last, …}], more, older}`: models OpenCode
has now, `wb optimize` variants under their source via `variant_of`, renamed ids under the current
one via `renamed_from` or the Ollama digest, #818; at most `stats.STATS_MODEL_LINES` 3, the rest in
`more`, idle past `stats.OLD_DAYS` 90 or unconfigured in `older`, each only when nonzero; the most
recently used first; `models` keeps every id split), `sizes`, `stacks` `{tasks, accepted_pct}`,
`days` (30 local days `{day, tasks, landed}`; local-date arithmetic, never `now - 86400`, #563),
`guards`, `helps` (`progress.helped_by_model`), `bench` (each model's newest finished run `{run,
day, passed, of}`, a newer one beside it, never in its place, as `partial` `{…, planned, running}`,
#570; a run is finished once every replay in its plan.json, times `repeat`, is graded or failed to
start; one with no plan is finished), `optimized` (optimize.json's `{window, fits, spill, ts}`).
results.jsonl is read once and the snapshot passed down (#565: 16 reads a feed, 3 s at 20,000 rows). `feed(only=...)` builds just those sections; plain `wb stats` uses `stats.SUMMARY`.

**API tasks in the feed** (#1022). `periods[*].api` is `{tasks, cost, unknown, plan, by_provider:
{p: {tasks, cost, unknown, plan?}}}` (`stats._api_period`, from `savings.api_totals` over every
row, trials in: money spent is spent), only when the period has API rows; `plan` is there so a
plan-only week never reads `API $0`. `model_lines` and `models` take an API model from its rows'
`kind` (discover() lists none) and group by kind as well as by id, so an id run as a GPU model and
then as an API one (an Ollama `:cloud` id, #1013) gives two lines; each has `kind`, an API one
`paid`, its mean dollars a task over the known costs (none known: no `paid`). The trial-free rows
(`tuning.judged`) give those lines. Plain `wb stats`' headline adds `api this week: 6 task(s), $1.27
(deepseek $0.84, anthropic $0.43)`, then `N with no price` and `N on plan usage`
(`stats.api_week_line`). The Stats tab: each period adds `API $1.27 (deepseek $0.84 · anthropic
$0.43)` (`apiPeriod`; `API plan usage` or `API no price` with no known dollars, `apiMoney`), and
an API model's line says `paid ~$0.12/task` in place of its write speed. The footer's API-only
week reads `this week: API $1.27 on 6 tasks (no GPU task to compare)`, or `this week: 1 API task,
no price (…)` / `2 API tasks, plan usage (…)` (`apiAlone`).

**Taking over.** `wb takeover <task>` stops the agent and sets `review` ("Claude took over"):
Claude finishes in the worktree uncommitted (accept adds the trailers; a commit made by hand is
kept as is), then accepts or hands back with `wb feedback`. `wb stop` sets `stuck` ("stopped by the
reviewer", worktree kept); `wb resume` continues a stuck or failed task in a fresh session with
`RESUME` and the step's asks, not counted as a review round.

## Context, on both sides

Local models work in a small context window, and so, in its own way, does Claude.

**The agent's.** Rounds continue one OpenCode session until the runner hands it to a fresh one
(*The runner, inside*); OpenCode's compaction sits at least a step's growth past the handover
point (#644). Reports show the fill (`agent context: 19.4K of 32K (60%)`). `--fresh` (on `wb
feedback`/`wb next`) forces a fresh session; `--keep-context` keeps it. Context carries over only
within a task's worktree.

**The slice** (`router.slice`). The first guess is three files and about 150 new lines on 32K,
scaled with the window up to `GUESS_MAX` (4) times. Each closing step records its files and added
lines (`results.step`): bad when compacted, filled up reading first, stuck, or `BAD_REVIEWS` (2)
agent-caused review rounds (a `--spec-gap` round is Claude's); a handover isn't held against it.
The latest steps decide: the smallest bad one lowers the slice, the size most good ones reached
raises it unless steps that big mostly went badly (#720: the biggest good step ever set it for good). `wb models`, `wb start`, `wb scout` show
it. Check-ins and reports name changed files the spec never mentions (`NOTE: the spec doesn't name
src/util.c`; `src/io.{h,c}` names each file).
The window is what the server serves (*Managing the set*): `wb model add` writes it with an
eighth as output budget; `wb doctor` flags a mismatch or none.

**Claude's record as a spec writer.** `--spec-gap "what it missed"` (`feedback`, `next`, `accept`,
`discard`) marks a round as the spec's fault: `status.spec_gaps` `{round, text}`, counted in the
row, a `spec` lesson for Claude's checklist (not an agent skill), and shown by `wb start`
(`results.spec_gap_line`: `your specs: 2 of the last 10 task(s) needed a round for a spec gap; last: …`).

**Self-tuning** (`workbench/tuning.py`). Model-dependent settings are learned per model and window.
`KNOBS`: `fresh_at`, the handover point (default 0.6, candidates 0.4–0.8), and `self_review`
(default 1, candidates 0 and 1; the agent's own review round, *6. The tests gate the round*). A per-task `--self-review`/`--no-self-review`
wins over record and pin (`tune`'s `fixed`) and is evidence for the value it ran. `tune(task,
ceilings)` picks once per task (`status.tuned`, `status.explored`, `status.tuned_max`): the best so
far, or one time in five (`EXPLORE`) its neighbour. `reward`, per step: 1 if accepted
(`agent_landed`; a takeover scores 0, #647), divided by one plus agent-caused review rounds a step,
less `COMPACT_PENALTY` 0.25 if OpenCode compacted, ± up to `SPEED_WEIGHT` 0.15 against the model's
median active minutes a step of the same kind of work (`tuning.work`: the row's `role`, else its size; #543, #721, #1025). Discards score 0; a `--spec-gap`
discard counts nowhere (`judged`). `Knob.fills` (`fresh_at`) counts a task only if a session
reached the smallest allowed value (`tuning.acted`, row `peak_share`; older rows count; #721: most
tasks never fill 40%, and one fast trivial task flipped the pick); `Knob.passed` (`self_review`)
only if a round's tests passed (`self_review_due`, set whether or not self-review ran; older rows
`tests_ok`; #816: a task without passing tests ran alike under 0 and 1, and its noise could turn
self-review off), and never a writing task's (row `role: writing`: off by its role, not chosen;
#1025). `probe_aggressive` is gone
(#857): an old pin, row `tuned` value or `status.tuned` entry is never read; only `KNOBS` count.
Means are smoothed toward the pool (`PRIOR`, two tasks' worth); a non-default value needs `TRY_MIN`
(3) tasks before it can be picked (an untried value sits at the pooled mean, so one task above it
won); the pick moves one step at a time. Values past
`runner.fresh_ceiling` (a step's growth, at least `tuning.CEILING_MARGIN` 0.05, before compaction)
are never tried; a window under `models.MIN_WINDOW` (16K) is refused. An output cap that puts the
ceiling under every value makes the ceiling the only value, or the smallest when the ceiling isn't
positive (#595: the smallest stood, past compaction, so every long step compacted); `runner.handover_warning` says
so in `wb start` (`note:`) and `wb doctor` ("its output cap … puts OpenCode's compaction at N% of
the window…", or "no handover fits" at `HANDOVER_ROOM` or under), and `optimize.budget` cuts such a
cap to a quarter of the window. Trials aren't evidence. `wb tune` shows the record;
`$WB_HOME/tuning.json` pins (`{"fresh_at": 0.6}`, used as given). A new knob: add it to `KNOBS`,
read it with `tuning.value(task, name)`.

**API self-review** (#1017). Without `--self-review`/`--no-self-review`, an API task's
`self_review` is fixed 0 (`runner.fixed_knobs`, passed as `tune`'s `fixed`): a round Claude never
sees costs money there, so neither the record nor the explore turns it on. `--self-review` is
Claude's word and runs it.

**Claude's.** `wb` holds the delegation state. Whenever Claude's conversation compacts with this
session's tasks open in a session that delegated, the mod tells the summary to keep goals,
decisions and each task's next need, and drop finished tasks' diffs and reports. At a task boundary
in workbench mode (one just closed, none waiting) it compacts early once halfway to the
engine's own compaction (`WB_COMPACT_AT`, a percentage; 0 off). A session that never delegated is never
compacted for a task (2026-11-16 audit: a task closing compacted every session). After a compaction or `/clear`, a context block lists the
open tasks from `wb status --repo <its directory> --session <id>…`.

**What Claude learns about the user** (`wb notes`): cross-project preferences, `wb notes add "…"`
(at most 300 characters, 20 notes) in `$WB_HOME/notes.md` (`- ` lines, `notes.lock`), handed to
every workbench-mode session in a `user-notes` block (#540); `wb notes drop N…`. One repo's facts go
in its CLAUDE.md.

## Waking Claude, exactly once

Notices go to `$WB_HOME/notify.jsonl` (`Task.notify`: `{ts, task, kind, text, round?, id?, cause?,
tasks?}`). Kinds: `review` (report and `review_diff`), `stuck`, `failed`, `question` (with `id`),
`checkin` (periodic, a nudge, a mid-round handover before the last allowed, a base check cut short).
`cause` (`stuck`/`failed` with a round): `guard`, `agent`, `server` (#657), `provider` (an API provider's
`rate_limited`/`outage` after its one revive, `quota`, `auth`: #1020), `spend` (the day's API spend
cap, #1019: `runner.SPEND`), `updated`
(`runner._updated`), `opencode` (`runner.opencode_moved`), `signal` (`SIGNAL_STOP`), `died`
(`reap`), `update` (`wb update --stop`). `tasks` names a batch (restart, shutdown, `wb update
--stop`). `wb` itself writes only `reap`'s `failed` and `cli._stop_for_update`'s `stuck`, one per
owner (#694). The file is out of the agent's lane (2026-11-16 audit: it could forge or wipe wakes).
Read through `store.notices()`, whole rows only (a `NAME_RE` task, a kind, a numeric ts: one row
without a task made every acknowledging command raise). A task's
`acked` file is the time up to which Claude has its news; `noticed` its newest notice's ts (for
every task a batch names; #844). A `ts` is later than every earlier notice and the task's `acked`,
under the lock (`append_jsonl(…, after=)`, `store.newest_ts`; #473: a backward clock hid notices).
A batch's other tasks are acked up to the row's stamp as written, never below their mark
(`store.ack_batch`, #874).

- **Ears.** The mod runs `wb events` (*Waiting without polling*) and re-reads tasks on each line.
- **Still true.** The newest notice under an hour old past `acked` (and every open question) is
  delivered only while true: a check-in while queued, running or testing; a question while
  `asking` with no `ask/<id>.answer`; else while the phase matches the kind. A stale one is acked
  and dropped.
- **Held mid-turn.** While Claude is mid-turn (`mem.busy`, set by `turn.start` or the model's own
  `tool.call`) the wake waits (a prompt submitted then would queue and arrive stale) and is
  rechecked each poll and after the turn. A question doesn't wait (its agent is blocked): it goes into the running turn
  (`session.send`, `MIDTURN_NOTE`); if refused and then closed with `NO_ANSWER`, it wakes Claude
  after the turn with `MISSED_NOTE` (#542). Otherwise the mod writes `acked`, toasts, and submits
  `[workbench] Task <n> <headline>.`, the text and the next commands (`NEXT[kind]` in
  `register.tsx`; a question names `wb answer <n> --id <id>`, a PERMISSION one its `wb allow`); a batch wakes as `[workbench] Tasks <a>, <b> were interrupted.` with
  `BATCH_NEXT` (#544). A prompt the engine drops puts `acked` back (unless something acted since)
  and the safety net retries.
- **Acting acknowledges.** `feedback`, `next`, `answer`, `allow`, `note`, `resume`, `takeover`,
  `accept`, `discard`, `stop` ack the task (`cli._ack_all`), stopping just before the oldest unacked
  notice of the kind the phase stands for (`question` while asking, `checkin` while queued, running
  or testing, else the phase's name), else at now; never backwards (a note seconds after a failure
  once swallowed its notice for an hour). `answer`, `allow` (answering), `feedback`, `next`,
  `resume` move it to when they acted (an answered question came back from `wb wait`), but while
  asking still stop before another open question's notice.
- **The safety net** (`rewake`): a task waiting on Claude (review, asking, stuck, failed) a minute after its last status change,
  newest notice unacked (by notify.jsonl's tail or `noticed`, #844), wakes Claude once with `wb
  report <n>`.
- **Without the mod.** `wb wait [task]` blocks for news past `acked`, prints `[kind] task` and the
  text, and acks it, skipping what the mod would (`_stale`; of a task's questions only the oldest
  open, `_oldest_open`, judged by the answer files), partial rows and gone tasks; it reaps first.
  Exit 2 for a named closed task, a bare wait with nothing open, or `--timeout`. A notice taken
  elsewhere is reprinted "(already delivered once)"; a task stopped by hand prints `[<phase>]
  <task> (no notice: <last>)`; one failed as dead under 10 s ago waits for its notice.
- **Off switch.** The plugin's `wake` setting stops prompts, keeping pane and toasts; a
  `workbench-wake` context block (#540) tells Claude no wake will come.

The state directory is global, so a session wakes, rewakes and compacts only for its own tasks
(`mine`). `wb start` records `CLAUDE_CODE_SESSION_ID` (task.json `session`), `CLAUDE_PID`
(`claude_pid`), its start (`claude_start`, `models._started`) and `claude_machine`; the mod keeps
every session id its process had (`$.session.id()` at start and each poll, since /clear and a
resume change it: the `sessions` atom), and a task with a `session` is the session's whose ids hold
it, wherever it ran (0.90 review: by directory alone, a task started from `~` was every session's
there). Once the process is gone (`cli._gone`, `models._alive`: no `/proc/<pid>`, else `kill -0`
fails, #700; or a recycled pid, #532; never judged for another machine's pid, #922; the mod's
`noteClaude`, a live one rechecked at most every 30 s, a gone one final), the task is also claimed
by directory, so a new `claude` in the repo adopts it (0.90 review: after a crash or an update it
was woken for none). The first claimer wins (#687): the mod, knowing its process from `wb events`'s
hello (`parent`, `parent_start`, `machine`), runs, for an open task whose process is gone and that
it would own, `wb adopt <task> --pid <its process> --session <ids…>` (`claim`), which under the
command lock rewrites `session` (kept when one of the given ids), `claude_pid`, `claude_start`,
`claude_machine`, and refuses (exit 1) while the recorded process lives; the mod re-reads task.json
either way, so every other session sees a live process and drops the task.
The wake is a compare-and-set: `wb ack <task> <ts>` raises `acked` under the `ack` lock only if below
`ts` (`cli.claim_ack`, shared with `wb wait`); exit 3 wakes no one. An undelivered wake is released
by `wb ack <task> <prev> --if <ts>` (`cli.release_ack`), only while the mark is still that `ts`
(#786). A `wb` that can't run leaves the mod's own write, which only raises the mark (`raiseAck`).
Two live sessions in one repo keep their own; a task with a `session` but no `claude_pid` stays its
session's. One without a `session` goes by
directory: its `repo` is or is inside the session's directory, or the session's directory is inside
the task's spot (`repo`/`subdir`, else a `cwd` inside the repo, else the repo: `spotOf`, `_scope`;
#695), or its `cwd` is or is inside the session's directory; by whole components of real paths,
case-folded where the file system ignores case (`mem.fold`, `cli._folds_case`; 2026-11-19 audit). A
session in `~` or `/` claims none by directory; a task with neither key is everyone's. Another
session's notice is at most toasted. `wb status`, a bare `wait`, and `stop`/`takeover`/`resume
--all` from Claude Code's Bash take the caller's scope (`cli._caller`: `CLAUDE_CODE_SESSION_ID`,
`claude-sessions/<CLAUDE_PID>` under this process's start, #787, the directory; `--all-sessions`
for all; #693). Among owners the first to deliver wins. A session with no person (`claude -p`) does
nothing until a surface attaches.

## Seeing what the agent does

**Which model, and why** (#1016): `wb start`'s `model:` line (an API task's
`model: <id> · API (<provider>) · trivial role, set by you 11-20 · thinking off`, `routes.model_line`;
a GPU task's as before), `wb status`'s `via` column, `wb watch`'s `routed:` line (`transcript.routed_text`:
an API task's `API <id> (<provider>) · <role> role, set by you MM-DD · thinking off` from task.json,
else task.json `route`, the router's why), the pane's row badge (`API · deepseek-flash`, `modelOf`)
and its `routed` line under the task line, and the web chat's strip over the thread (`Running:
fix-auth · API deepseek-flash (DeepSeek)`, from `chat.open_tasks`): while away, the chat is all the
user sees.

- **The pane** (opened by `/workbench`, or once per load when a task runs) has tabs **Tasks**,
  **Stats**, **Settings** (keys `1`–`3`, at the foot, since the pane follows its end), and under them
  a modeline: `[•_•] Claude's Workbench` (`FACE.rest`, `MODELINE`) on the theme's `claude` colour
  with `inverseText`, then `activity` (#976, `mod/hooks/modeline.ts`): `mine` open tasks,
  `<name>: round <n>, <phase>` (`SAID`), from the polls alone, never a Claude call. Too wide, it
  scrolls as a ticker: a one-row Raster (key `ticker`) moved every `TICKER_MS` (200 ms) by
  `$.ui.blit`, `TICKER_GAP` (6) blanks; `syncTicker` runs the timer only while a line scrolls.
  **The opening animation** (#963, `mod/hooks/splash.ts`; the owner, 2026-11-21: about five
  seconds, no cost after startup) plays at the `/workbench` that turns the mode on (`skill.prompt`,
  before `showPane`): a body-sized Raster (key `splash`) at `SPLASH_MS` (80 ms), its words landing
  in the modeline's bar on the row from `pageShape`/`splashTarget` (recomputed each draw), or
  rising away when no row is exact (`NARROW`), never on a guessed row. A key skips it and still
  acts; a close, another tab, a small pane, no Raster or a refused blit end it; a resize replans it
  (ending there killed it in every real session, 2026-11-21). Typing doesn't skip it: a
  `prompt.edit` hook can't be removed and would slow every keystroke. The `splash` atom
  (`splash-1`) is set at that `/workbench` and cleared by `workbenchMode` when the mode goes off, so
  reloads and bring-backs never replay it. Big at 56×24 up, small at 40-55 columns or 20-23 rows,
  none below. Frames are computed as they play from font tables and apprentice.ts's `pixels(pose,
  over)`, nothing at module load, no file read;
  non-apprentice colours go through `atLum` (WCAG luminance 0.105-0.295). It ports forge.py, the
  reference generator, exactly (hash, arithmetic order, half-to-even rounding): the tests hold
  frames 0-41 at 60x26 and 40x22.
  Closed by the user (mark, Esc), the pane is minimized to a strip above the prompt (`▸ Workbench`,
  `Tasks`, `Stats`, `Settings`) while workbench mode is on or tasks exist; a press or `/workbench`
  brings it back, a quit clears it, a reload doesn't reopen it.
  The Tasks tab lists every task and follows one as a live transcript: the followed one while live,
  else the newest started running/testing/asking, then queued, else the newest waiting for Claude
  (never by latest status write, which flipped on every tool call). At its foot the apprentice
  shows the phase and says what it does and the time left, or who acts next ("Claude reviews
  next.", #103), as do the transcript's last line and status `last`. Then the week's saving
  (`savedLine` of `wb savings --json`, read every 10 minutes and when a task closes; no currency
  sign, #609), then ` · API $1.27` when its `api` has tasks (`apiMoney`; an API-only week: `this
  week: API $1.27 on 6 tasks (no GPU task to compare)`, #1022). The web chat's link is on Settings
  only.
  The Settings tab (`settingRow`; Buttons, since a Select opened on a click and could pick nothing,
  2026-11-19): a Button per setting naming its value, the default option saying `(default)` (`▸
  Claude coding: Let Claude decide (default)`, key `coding`); a press opens its options (keys `coding:only|ask|decide`,
  `chat:off|on`, `probe:off|on`, the chosen one marked `❯`); picking the current option only folds
  the row (#578). Below, the sentence of the option the ring is on (`ui.focus`, the `pane` atom), or
  on hover (`display: "none"` Boxes under the option's hover scope: no hook runs on hover). **Claude coding** (`CODING_STOPS`, *Claude coding*): `wb
  coding <mode>`, reading `coding.json` each draw. **Web chat** (`CHAT_STOPS`): `wb chat on|off`.
  **Agent code navigation** (`PROBE_STOPS`, *Experiments*; #857): `Off (default)` and `On (large
  files only)`, on saying `The agent may extract one function of a file over 1,500 lines with
  probe, instead of reading it whole (github.com/probelabs/probe).` (a copy of
  `context.PROBE_FILE_LINES`; `tests/test_docs.py` checks both); `wb experiment probe on|off`
  (`setProbe`), read from `experiments.json` (`probeMode`, as `context.setting`: `on`, `true` or an
  old level, `PROBE_OLD_LEVELS`, is on). `(recommended)` only when wb's line ends `; on is
  recommended` (`context.PROBE_RECOMMENDED`). `probeStatus` asks `wb experiment probe` at most every
  `PROBE_RECHECK_MS` (30 s) and after each pick, so on reads `, working` or `, not set up`
  (`probeStops`; 2026-11-20); on but not set up queues `PROBE_SETUP_NOTE` once. The Stats rows, each
  a `wb` command (the mod writes no setting itself): **Stats recording** (`RECORD_STOPS`), **Live view** (`LIVE_STOPS`), **Live
  refresh** (`EVERY_STOPS`), read from stats.json (`statsSettings`); **Progress checks**
  (`PROGRESS_STOPS`, `progressSettings`, `progressStops`: `On, after N min` with a kept `after`);
  **Electricity price per kWh**, an Input (key `price`, `setPrice` via `parsePrice`: one comma or
  point, a sign or code of up to three characters, decimal digits only; empty runs `--price off`;
  #609). **Opening animation** (`SPLASH_STOPS`, `splash:on|off`): `wb splash on|off`, `splash.json`
  (`splashOn`). The ring follows `SETTING_ROWS`. Order: under **Shared** the Agents switch, the busy
  row, the three roles, Claude coding, Dynamic workflows, the day cap, wake, the Stats rows, the
  animation, the price; then **GPU**, **API** (*The roles in Settings*), and last under `Experimental ·
  may change or go away` Web chat and Agent code navigation (the two that may change or go away,
  2026-11-21). A failed `wb` call toasts its last line.
  **The roles in Settings** (#1023): read through `rolesNow` (`wb route --json` and `wb models
  --json`, neither asking a server; kept `ROLES_CACHE_MS` 10 s in `mem.roles`, dropped after a pick).
  Shared: **Agents** (`AGENTS_STOPS`, key `agents`: `wb route all gpu|api|both`), **When the GPU is
  busy** (`BUSY_STOPS`, `busy`: `wb route busy wait|api`), one row per role by its label
  (`roleStops`, `role-<role>`: `wb route <role> gpu|api`; api without a model says `API (set its
  model under API first)`), **Dynamic workflows** (`WORKFLOWS_STOPS`, `workflows`: `wb workflows
  on|off`, read from workflows.json), **API day cap** (an Input, key `cap`: `setCap` via `parseUsd`,
  parsePrice's forms, `wb route cap <usd>`, empty `wb route cap off`), and the read-only line `Wake
  Claude: on · change it in /plugin → workbench → options` (`mem.wake`; the pane can't write a
  plugin option). GPU: per role `<label> GPU model` (`gpuStops`, `gpu-<role>`: `Auto (the router
  picks)` first, then each GPU model with its window and, where `models.card_numbers` knows them,
  its cards, `· GPU 2+3`; `wb route <role> --gpu <id|auto>`), a sizes row for at most `GPU_ROWS` 6
  models (pinned first, then by label; `SIZE_STOPS`, `size-<id>`: `wb model sizes <id> [size]`),
  then `+N more (wb models)`, and `Optimize: …` with a **Measure now** Button (`optimize`: `wb
  optimize start`); last **Images**, **Speech**, **Sound effects**, **Music** (#1047, `mediaRows`,
  `MEDIA_ROWS`, `mediaStops`, keys `media-image|speech|sfx|music`), each only with two or more
  engines of its kind in `wb models --json`'s `media`, every entry its engine's name (`· off this
  GPU` for `gpu` false); a pick runs `wb image|audio --default <name>` with no dialog, and a refusal
  toasts (*The default engines*). API: with no provider whose key is stored and no role holding an API model, the
  one line `No API provider connected: /connect in OpenCode adds one; every role runs on the GPU.`
  (`NO_PROVIDER`); else `Connected: <provider> (stored) · <provider> (<why>)` (`connectedLine`), per
  role `<label> API model` (`apiStops`, `api-<role>`: the role's model, the other roles', those
  tasks ran on, and every runnable one while the list is no longer than `API_MENU` 12, each `<name>
  · <provider> (<window>) · $in/$out per M` with a free model's data-use mark; then `Other model…`,
  which opens the Input `api-<role>-other`, an exact id wb checks; `wb route <role> --api <id>`),
  `<label> thinking` (`THINK_STOPS`, `think-<role>`: `wb route <role> --thinking on|off`, for tasks
  started after it), and `Spend today: $0.42 of $5 (deepseek $0.42)` (`spendLine`, its dollars by the Stats tab's `usd`). A press goes
  through `paneRoute`: `wb route --dry-run --json <args>`, and when it is gated the mod writes its
  `approval` to route-ok.json with `by: pane` (the press is the user's yes; no dialog, no flag
  Claude could pass), then runs `wb route <args>`; wb's refusal (no day cap, an unknown id, a key wb
  can't hold) is a toast and the row keeps its value. A wb without `wb route` (or its `--json`)
  leaves the roles, GPU and API rows out for the one line `wb is older than the mod: wb update`
  (`OLDER_WB`); one whose routes.json can't be read, wb's line. Each change reaches the conversation
  as the `[workbench]` echo (*The mod, inside*).
- **The Stats tab** opens with **Now** (#408): `wb top --json` drawn by `liveLines` (GPU bars and a
  `BUSY_KEEP` 60-sample sparkline, running tasks, the rack with more than `RACK_FOLD_MIN` 2 models
  in `RACK_FOLD` folded into one line, the queue; `wrapParts` wraps). One child (`runTop`,
  `pumpTop`), run by `syncTop` only while the tab is shown, the pane not minimized and Live view on;
  `stopTop` ends it the moment that stops holding. `syncTop` awaits first, then checks and spawns
  synchronously, giving up if `mem.topGen` moved; `stopTop` ends every child in `mem.topKids`
  (#546). A child that ended by itself retries after `TOP_RETRY_MS` 30 s, doubling to
  `RETRY_MAX_MS` 10 min, and from the second tickless run **Now** says why (#553); one ended as
  unwanted isn't backed off (#552). `stopTop` clears the `top` atom (`top-1`), so a return never
  shows the old tick. The tab drawn with no child calls `syncTop` (the engine's re-show says so no
  other way); `showPane` and `restorePane` call `refreshStats` and `syncTop` once their open resolves
  (`paneBack`), not before, when the pane could still read as hidden.
  Below, the history (#406): `wb stats --json` (*The Stats feed*) drawn by `statsLines`, one
  `Label: text` line a section. The pane caps model lines at `STATS_SHOWN` 3 itself, since the mod
  tracks main while `wb` runs from the user's clone (an older wb sends every line, or no
  `model_lines`); What helps, Bench and Optimized name only the shown lines' models. A feed with no
  section says to delegate with /workbench (#549). It is read (`refreshStats`) only while the tab is
  shown: on a tab press; at a poll when the size or mtime of a source changed (`$.fs.stat`:
  results.jsonl, stats.json, optimize.json, bench/runs.jsonl, speed.json, savings.json
  `mem.savingsFile`, each `machines/<id>/`'s optimize.json and speed.json; #549, #894); past the
  feed's `until` (#575); after a Stats pick; once per load (`mem.statsLoaded`). A failed read keeps
  the last feed, retries after `STATS_RETRY_MS` 60 s doubling to `RETRY_MAX_MS`, and says why
  (#553: each 1.5 s poll reran a full history read). No timer: hidden it costs nothing, shown a stat
  a poll. The feed is the `stats` atom (`stats-1`).
- **The status line**: `⚒ wb · add-scores running r2 · fix-typo asks · 1 awaiting review`, or `⚒
  wb · workbench mode`. Outside workbench mode it counts only `mine`, and is empty without one
  (2026-11-17).
- **`wb watch [task]`** (`workbench/transcript.py`): the transcript in a terminal. It re-exports
  only when the task directory or status `session`/`updated_at` changed, or `EXPORT_EVERY` (30 s)
  passed while live (#555); it calls `cli.reap` every `REAP_EVERY` (60 s) while live.
- **`wb status`**, **`wb log [-f]`**, **`wb report [--round N]`**, **`wb diff [--stat] [--all]
  [paths]`**: Claude's compact views. `wb status` reaps a silently dead runner (`failed`) and marks
  one dead within `REAP_GRACE` with `?`.

**The transcript** (`transcript.follow`) merges the exports of the current and earlier sessions
(`status.sessions`) with the task's events, by time, each entry once (keyed by message and part).
`opencode session export <id> --standalone` runs from `/` (30 s); with no id yet, `opencode session
list --standalone` in the worktree finds `wb: <task>`. Standalone so they never need the background
service's port (2026-11-16 audit); a failure is said once per process (`opencode session export
failed: …`). The same export feeds the runner (`transcript.export`). Everything printed for a
person passes `transcript.plain`: control characters but newline and tab (C1 too) become `\xNN`,
so an escape (OSC 52) never reaches the terminal; `wb log` and `wb report` do the same on a
terminal.
Drawn in Claude Code's grammar (results under `  ⎿  `). Prompt labels match the prompt's first
words (`Claude → agent · spec`, `· review feedback`, `· next step`, `· INTERRUPT, high priority`,
`· note`; `workbench → agent · tests failed, sent back automatically`, `· fresh session: the task,
the work so far, what's next`); the protocol folds to `(+ the workbench protocol; -v shows it)`.
`ask_reviewer` calls are left out for their `ask`/`answer` events. Each model call gets `⏱ N tokens
in (M cached), read in Xs · K out in Ys · R tok/s`. `-v` raises the caps. Under it: `activity`
(queued: `Waiting for the GPU…` or `status.last`, #869; testing, asking; running: the server's `GET
<root>/metrics` `live` object `{state, phase, prompt_tokens, generated, tok_s}`, which
`models._server_idle` reads too; without one, a rotating verb) and the clock (`⏲ task T · round N t
· model load t`, the ETA from `router.expected_minutes`, the median active minutes a step on the
task's `weights_id` from the files alone, never the server (#819), against `runner.active_so_far`
(#543), then `context U of WK (P%)`).

**`wb watch <task> --json`** (hidden) is the mod's feed: one pass, `{"s": style, "t": text}` per
line: `meta`, the transcript (styles `claude`, `agent`, `think`, `tool`, `out`, `ok`, `bad`, `sys`,
`add`, `del`, `head`, `""`), `activity` if any, `clock`, the phase line. Text is cut to
`JSON_LINE_CHARS` 2000; `--last N` (hidden) keeps the newest N transcript lines (the engine keeps
only a command's first 4 MiB). Earlier sessions are exported once ever into
`tasks/<t>/.export-<sid>.json` (`transcript.finished_export`, #691). The mod runs it with `--last
2000` while the pane is shown, every 5 s while the followed task is active (#691), at once when the
followed task changes; past 4 MiB it keeps its lines and adds `transcript too long for the pane`;
it cuts to `LINE_CHARS` 2000 and uses `activity` as the apprentice's speech, the pose `work` when it
starts `Writing` or `Running`, else `think` (reword those and update the mod). A new style needs an
entry in the mod's `LINE_STYLE`; changing the shape needs a new `live-1` tag.

### wb top

`workbench/top.py` (#407): the machine's live view, and a feed (`--json`). One long-lived process
ticking every `--every S` (0.2 to 3600, `cli.TOP_EVERY`, #611; default stats.json `every`, else
`EVERY` 2). It exits with its parent, and quietly (0) on SIGTERM, SIGHUP, a closed pipe, `q` or
Ctrl-C, killing its nvidia-smi. Never a model call, a load or a session export.

Each tick reads:
- **GPUs**: sysfs per card (`hardware.drm_cards`): busy, VRAM (an APU, `hardware.apu`, adds GTT,
  `unified`), watts (`hardware.card_watts`); names from lspci once (`card_name`). NVIDIA: one
  streaming `nvidia-smi --query-gpu=… -lms <ms>` (`SmiStream`, `hardware.smi_path`); rows older than
  three intervals drop out, cards named at start stay as `stale`, a dead stream restarts with
  backoff to `SMI_RETRY_MAX` 60 s (#547). An ARM card with `[N/A]` memory (`hardware.shares_ram`)
  shows shared RAM (#572); x86 `[N/A]` stays unknown. macOS: system_profiler once; an Apple GPU's
  `total`/`used` are RAM (`hardware.mac_free`) and `limit` the wired ceiling (`wired_limit`, else
  `models.unified_default`; #574, #701). WSL without nvidia-smi cards: Windows' adapters once. A
  card reporting nothing is no row; with no row giving `total` and none `stale`, a RAM row (#551).
- **Tasks**: task.json, status.json (`_Files`, by mtime and size), a running task's last 64 KiB of
  events.jsonl.
- **The lease**: gpu.json and each remote pool's `gpu-<host>.json` (`models.holders`, dead pids
  dropped); `router.speed`.
- **Every `PROBE_EVERY` (5 s)**: `models.discover()`, Ollama's `/api/tags` and `/api/ps`,
  `models.status` (`/v1/models` once per base URL and key, `models._catalog`, #561), non-Ollama
  `/metrics`; never another machine's model; proxies skipped (`models._open`). The loop probes on a
  thread (`Top._probe_bg`) so a stalled server never holds the tick. Also `cli.reap` for each live
  task (#571).
- **Typical load** (every `LOAD_EVERY` 300 s): the median single load (`models.single_loads`: status
  `loads`, else `load_secs`), not task totals (#577).

`--json`, one object a tick (bytes; a field the platform can't give is left out, never 0):
- `ts`, `every`;
- `gpus`: `[{index, name, driver, used, total, busy (%), watts, unified?, limit?, stale?}]`, then
  `{name: "RAM (no GPU)" (or "RAM"), ram: true, used, total}` when no GPU row gives its memory;
- `running` (queued, running, testing, asking): `[{task, model, label, phase, round, round_s,
  tok_s, tok_s_from, read_tok_s, read_from, context_used, context_window, context_pct, last,
  remote?, server?, via?, provider?, spend?, waits?}]` (the last four an API task's, below); `tok_s_from` is `live` only for the one task in phase `running` on that
  server (#548), else `last call` (the newest step's output over the time since the step before: a
floor); `read_from: "median"`. No first-token time: OpenCode's events carry none.
- `rack`: `[{model, label, state, why?, gpu_bytes?, cpu_bytes?, write_tok_s?, read_tok_s?, load_s?,
  tasks?, server?}]`, `state` `resident`, `split`, `cpu` (`size_vram` 0 with no GPU memory row),
  `idle`, `can't run`, `remote`; `server` a busy server's `live` no task claimed.
- `queue`: `[{task, model, since, waited_s, pool?}]` by `since`; `waited_s` from gpu.json
  `waiting_since`, not the task's age (#573).

**API tasks** (#1022, `Top._api`): an API task (task.json `via: "api"`) is under RUNNING with
`via: "api"`, its `provider`, `spend` (dollars so far, `results.live_spend`: the runner's meter,
else the steps' `cost` over all of events.jsonl, which `_Files.step_cost` sums on from the offset
the last tick reached, again from the start when the file is replaced or cut; the 64 KB
`EVENTS_TAIL` held a fraction of a long task's; absent when unknown, never 0 for a missing
price) and, queued for one of its provider's slots, `waits: "a <provider> slot"`. No live server
rate (no server of ours: the last call's rate still works). It is never in the rack, which stays
`discover()`'s GPU models, nor the queue, the lease's waiters: an API task takes no lease. The text
row ends `  API deepseek · $0.12`; the pane's **Now** row reads `▶ <task> <label> · running ·
round 2 · 3m10s · API deepseek · $0.12` (`apiParts`).

On a terminal: the alternate screen, redrawn each tick, bars in eighths, the card's short name
(`top.short`, the pane's `cardName` alike, #576), cbreak stdin for `q`. Ctrl-Z restores the
terminal before stopping and SIGCONT retakes it and redraws (a wake pipe, #607); the handlers write
straight to the fd (`os.write`), never `sys.stdout` (#950: a reentrant write quit wb top). Too tall,
it folds idle, `can't run` and `remote` models (`top.FOLD`), then cuts the middle with `… N more
lines`, keeping the QUEUE (`top.lines(snap, rows)`, #608). Not on a terminal: one pass, then exit.

## Models: found, loaded, chosen

Code: `workbench/models.py` (discovery, status, residency, the GPU lease, adding and retiring),
`workbench/router.py` (choice, slice, budgets, speeds), `workbench/hardware.py` (the machine),
`cli.cmd_models`, `cmd_model`, `cmd_scout`.

### Running your own server
What each server needs set (from the README's old FAQ, #102). Ollama is where wb automates most
(pulls, deletes, each model's window, two tasks sharing the GPU); any OpenAI-compatible server
works, with size and window read from llama-server, LM Studio and vLLM too, and tasks there take
turns on the GPU. No provider yet: `wb model add <provider>/<model> --url <baseURL>` (`--context
N` when it doesn't say its window).
- Window: at least 16K (`models.MIN_WINDOW`; the README says so, `tests/test_docs.py` checks),
  32K recommended; OpenCode's own prompt is about 10K. Ollama: `OLLAMA_CONTEXT_LENGTH` (its small
  default cuts prompts silently). llama.cpp: `-c`, split across `--parallel N` slots unless
  `--kv-unified`; one agent uses one slot. LM Studio: the length at load (`lms load <model>
  --context-length 32768`), loaded before registering. vLLM: `--max-model-len`. Too big for the
  card spills to RAM at about half speed (`wb optimize` measures Ollama's).
- Tool calls: vLLM needs `--enable-auto-tool-choice --tool-call-parser <parser>`; llama.cpp may
  need `--chat-template-file`; LM Studio's untrained-for-tools model is `tool_call: false`.
- An API key: the provider's `options.apiKey`; a `{env:NAME}` there needs `NAME` in commands.json
  `env` (agents get no secrets by default); else the key `opencode auth login` (or `/connect`)
  stored: OpenCode 2.x keeps it in its database (`opencode auth export`), 1.x in `auth.json`. The
  task's config layer carries such a key for the task's provider (agents' OpenCode has no stored
  credentials, #1010).

#### A server on Windows, from WSL2
WSL2's NAT forwards Windows to WSL's `localhost`, not back, so a server on Windows' 127.0.0.1
never answers from WSL: use `networkingMode=mirrored`, or listen on 0.0.0.0 and `wb remote` /
`wb model add --url` with `local: true`. When nothing answers here, `models.servers_here` (first
run, scout) probes the usual ports at `models.windows_host` (the default gateway in
`/proc/net/route`, else `/etc/resolv.conf`'s nameserver), calls a hit "on Windows", and with
nothing prints `models.WSL_NAT` (#663: Claude proposed a second server inside WSL). A provider
there is `remote` but on the same GPU (`models.same_gpu`): this machine's pool, and `free_gpu`
unloads it (#750: as its own pool it ran beside WSL's tasks on the one card).

#### A model on another machine
Ollama there with `OLLAMA_HOST=0.0.0.0`, reachable only from the client (no login; plain `http://`
carries the code: a firewall or Tailscale), then `wb remote <host>` (`--context N`, since that
Ollama says no window until a model loads; `--remove` undoes it). It adds
a model, never splits one; its energy isn't measured. Another server there: models.json `"local":
true` on its provider, then `wb model add … --url`.

Each client's lease is its own (`gpu-<host>-<port>.*`): nothing of wb runs on the box, and Ollama
shows only `/api/ps` (what is loaded, each `expires_at`, which a finished request moves and a
running one doesn't). So clients coordinate through it (`models.box_others`, `box_quiet`,
`BOX_SHARED`, #812): before loading, wait up to `BOX_WAIT` 2400 s while a model there its task
doesn't run had a request within `BOX_QUIET` 150 s. Not counted: `keep_loaded`, and what this
machine loaded there in the last week (`box_mine`, `boxes.json` `{root: {name: ts}}` in
`store.machine_dir()`), though such a model is watched `INFLIGHT_WATCH` and a request on it now
drops its mark (`_forget_mine`, #893). Last use is `expires_at` less `BOX_KEEP_ALIVE` 300 s; a stamp
standing still may be a long request and is waited out, so `make_resident` loads a box's model for
`BOX_KEEP_ALIVE`, not 30 min (#899: a 30 min stamp read as quiet through the first prefill); box
stamps may be past only by `BOX_SKEW` 10 s (`_past(remote=True)`). Clients thus take the box a round
at a time. Evictions and the task's own model found gone are `gpu` events (`GpuLease.load`,
`box_others(own=True)`); `wb start` notes the box's foreign models. Not stopped: a tool running
past `BOX_QUIET`, a box keeping models longer, a cut-off wait, clients that don't look.

### Found, loaded, chosen
**OpenCode's config.** `models.opencode_config_files()`: `$WB_OPENCODE_CONFIG` alone, else in
order the system directory's `opencode.json`/`.jsonc` (`_system_dir`: `/etc/opencode`, macOS
`/Library/Application Support/opencode`; never written), the global one's (`_config_dir`:
`$OPENCODE_CONFIG_DIR`, else `$XDG_CONFIG_HOME/opencode/`; #370), then `$OPENCODE_CONFIG`
(`_users_config`; never under the state directory: a task's layer). `read_config` merges them as
OpenCode does (later wins, dicts recursively, `_strip_jsonc`), then `OPENCODE_CONFIG_CONTENT`
(`users_config_content`; ignored beside `$WB_OPENCODE_CONFIG`; doctor names it set, never its
keys, #347); a real-OpenCode test compares with `opencode debug config`. A relative `{file:}` is
its defining file's (`_provider_dirs`). `opencode_config_path()`: `opencode.json` if it exists,
else `.jsonc` if it exists, else `opencode.json`. An unparseable file is a `RuntimeError` naming it.

Writes (`write_entry`, `_add_provider`, `remove_remote`, `set_default`) go through
`own_config(change)`: one file's own contents under its lock (`store.edit_config`), never the merge
(#421: that copied the admin's file and a session's `apiKey` into the user's). `set_elsewhere`
refuses removing what another layer also sets, or changing what a later one sets ("… is set in
<layer>, which wb doesn't write: …"). A model for a system-only provider is an overlay in the user's
file; for an `OPENCODE_CONFIG_CONTENT`-only one, refused. `own_config` refuses while two files are
layered (one merged file would undo the layering), a file that isn't strict JSON (a rewrite would
lose its comments) and one that isn't writable (Nix) (`models.config_target`), printing only `change`,
never the config (it may hold an `apiKey`). `store.write_through` (shared with models.json,
commands.json, savings.json, #495): atomic, indent 2, through a symlink, keeping the mode (0600 new).

The agent's `OPENCODE_CONFIG` is the task's layer, so `models.agent_providers()` copies
`PROVIDER_KEYS` from `$WB_OPENCODE_CONFIG` and `$OPENCODE_CONFIG` into it, templates kept, `{file:}`
made absolute, the layer 0600 (the 2026-11-19 audit: such a provider was "Model unavailable").

**Discovery** (`discover`, `_scan`) lists GPU models only (`kind == "gpu"`): about 60 call sites
(the lease, residency, `free_gpu`, media, the router's `choose`/`rank`/`unproven`/`_cheaper`) read
it, so none needs to ask a model its kind, and exploring can never reach an API model (#1013; a test
pins that `discover()` never returns `kind == "api"`). A candidate is an entry under a provider (`provider`/`providers`,
`options`/`settings` `baseURL`) that OpenCode loads (`disabled_providers`, `enabled_providers`) on
this machine (`_this_host`): `localhost`, `0.0.0.0`, `host.docker.internal`, loopback, the host
name, its first label, `<label>.local`, an own address (`_own_addresses`: `getaddrinfo`, `ip -o
addr`, else `ifconfig -a`; reread after `ADDR_SECS` 60 s), or a name for it (`_named_here`: FQDN,
or resolving to loopback or an own address within `RESOLVE_WAIT` 1 s; failures retried after
`DOWN_SECS`, results rechecked against current addresses; #807, #900). Elsewhere it counts only
with models.json `"local": true`, as `remote`. A baseURL's `{env:}`/`{file:}` resolve first
(`_expand`; empty when unset, unreadable, or dropped from the agent's environment by
`runner.SECRET_ENV`). Skipped providers and why: `skipped_providers()`, `why_none()` (in `wb
models`, doctor, `wb model add`, `resolve`, the router's "no GPU models"; an Ollama with only cloud
models: "<id> is an Ollama cloud model, an API model now (set a role to use it)…",
`ollama_cloud_ids`); one with no baseURL is a catalog provider, `hosted()`'s. `resolve()`: the GPU
side first, as before: exact id, exact model or provider name, or a unique case-insensitive
substring; then an API model by its exact id only (`hosted()[wanted]`, asked only for a miss with a
`/` that isn't the GPU side's, `gpu_side`; an exact miss reads the catalog again once, `hosted(fresh=True)`,
when the cache is older than `AUTH_FRESH_SECS` and its read didn't fail, `_hosted_exact`: the catalog
lists only providers that can authenticate and `/connect` changes no stamp, so a just-connected
provider's ids were unknown for `HOSTED_TTL`). A fragment matching nothing raises `unknown model '<x>'; known: <GPU ids>; API models are
named by their exact id, as OpenCode lists them (opencode models)` (`EXACT_ONLY`), never listing
catalog ids (#1013: with 40 or more, `--model flash` could have landed on a paid model nobody
chose).

**API models** (`hosted`, `lookup`, #1013). `models.hosted() -> {id: Model}` with `kind == "api"`:
- *Where from.* `opencode api model.list --standalone` (JSON `{location, data}`: only providers that
  can authenticate, plus OpenCode's free models; the spike's facts, *API models through OpenCode*)
  and `opencode auth list --format json --standalone` for each provider's key source (it never
  prints a secret; `auth export` is never run: only the keyhole reads keys, for a task). Both run in
  the state directory with `OPENCODE_DISABLE_PROJECT_CONFIG=1` (a repo's opencode.json under the
  caller's cwd must not add providers) and `OPENCODE_CONFIG=$WB_OPENCODE_CONFIG` when set
  (`_opencode_json`). OpenCode's own catalog fetch is left alone. Ids split at the first `/` only
  (`deepinfra/deepseek-ai/DeepSeek-V4.1-Flash-1017`: provider `deepinfra`, name the rest).
- *Which.* Every catalog model whose provider has no configured entry on this machine (one that
  `_scan` lists and isn't off it: those are `discover()`'s); the models of a configured provider
  whose baseURL is off this machine and not `local: true` (`_off_machine`: `_scan` still skips it
  with its `local: true` hint); the Ollama cloud models of an Ollama provider on this machine
  (`:cloud`/`-cloud` by name, whatever their `local: true`: key `ollama`, label `Ollama cloud
  (ollama.com)`, `cloud`). `disabled_providers`/`enabled_providers` and models.json `skip` are
  followed. A catalog entry whose `settings.baseURL` is this machine (`_this_host` without its DNS
  lookup: a catalog names dozens of hosts) is left out: a server OpenCode found by itself (2.3.2
  lists a running Ollama at 127.0.0.1 with no config) is `wb model add`'s, not an API model told to
  `/connect`. `gpu_side(id)`: the id's provider is one of the GPU side's (`_sides`: configured, on
  this machine or opted in, skipped or not) and its name no cloud name, so `hosted()` can't hold it
  whether or not `discover()` still does (a renamed or retired model, an old task's id, `wb model
  rename OLD`, a typo): `resolve`, `lookup` and `cli._api_id` ask no catalog for one (it cost two
  OpenCode starts on every `wb watch --json` the pane polls).
- *The model.* `base_url` the catalog's `settings.baseURL` (the upstream; an off-machine or Ollama
  provider's own baseURL), `engine` `api`, `name` the id at the provider (the entry's `modelID`, which
  an alias's differs from, else its `id`), `label` `<Provider name>
  (API)` (the auth list's name, else the id), `remote` true, `package` the catalog's (or the
  config's `npm`), `config` the catalog entry (`limit`, `cost`, `variants` as the catalog's list,
  `capabilities`, `tool_call` from `capabilities.tools`, `reasoning`, `family`, `status`; values
  unchanged, a field of the wrong shape dropped; never `settings`, where 2.3.2 prints the key it
  holds for a layer or a free model, `settings.apiKey`), `free` (`opencode/*-free`, or every price 0; only marked here). `key`, the source
  (never the key): a config `apiKey` first (`env:<NAME>` for a lone `{env:NAME}`, else `config`),
  then auth list's `credential` with `method: "key"` (`stored`), another credential (`oauth`), an
  `env` connection (`env:<NAME>`); a signing provider (`SIGNING`: Bedrock, Vertex) is `signing`;
  `ollama`; else `""` (`_key_source`, the provider's); a keyless free model is `none`, its own: a
  provider holds free and paid models alike, and one source per provider made every sibling `none`.
- *Its window and output* (`api_limits`, #1017; decision 10 of #1008). `config.limit` is capped once
  here: `context` `min(catalog, API_WINDOW)` (131072, the slice's saturation point, `GUESS_MAX` ×
  `BASE_WINDOW`: past it the slice grows only from steps that fit, and every request of a fuller
  window is paid for, up to 1M tokens a request in a session that only reads); `input` at most that
  when the catalog has one (`usable_input` reads the smaller); `output` `min(catalog, API_OUTPUT)`
  (32768; `API_OUTPUT` when none), and `options.max_tokens` that output (DeepSeek honours only
  `max_tokens`, and 384K out put the compaction point below zero; one reply alone can cost dollars).
  An entry's own `options.max_tokens` under those is kept, for both: the user's entries (a provider
  off this machine, an Ollama cloud model) come through here too, and raising theirs to 32768 spent
  output they never chose. The catalog's own numbers stay in `extra["catalog_limit"]`; only
  `wb models --api` shows its window. Every reader takes the capped window
  with no change of its own (*An API task's window, output and header wait*; *Couplings*). An
  entry with no window is left alone: status says it can't run (no guess: it could overrun the
  real window, and OpenCode would never compact). `is_api(id)`: `lookup(id, cached=True)`'s kind,
  never a network or OpenCode call.
- *The cache*, `$WB_HOME/hosted.json` (*Files and formats*): fresh for `HOSTED_TTL` 600 s, rebuilt at
  once when its `stamp` changes (the OpenCode command's files: real path, size, mtime; the mtimes of
  `opencode_config_files()` and models.json; `OPENCODE_CONFIG_CONTENT`'s crc32), or when missing,
  corrupt, of another version or another shape (`providers`/`models` not objects of objects;
  `store.read_json(…, {})`; trusted for nothing but listing). A failed
  read (OpenCode missing, exit not 0, `CATALOG_TIMEOUT` 30 s; each read runs in a session of its own,
  `media.run_group`, killed whole, so a wrapper's OpenCode never outlives it) is cached for `DOWN_SECS`: `hosted()`
  is `{}` and `hosted_error()` the first stderr line, redacted. Writes are `store.write_json`
  (atomic: two rebuilding at once, the last wins). Parsed once per change of the file a process
  (`_HOSTED`). `hosted()` never runs on a GPU path: `resolve` asks only after the GPU side misses,
  `lookup` only for an id `discover()` lacks, both only for one `gpu_side` says isn't the GPU side's.
- `lookup(model_id, cached=False)`: `discover().get(id)`, else `hosted().get(id)`. Only where an API
  task's model must be found: `transcript.model_line` (which reads the catalog's variant list too),
  `tuning._rows`, `router.expected`. The pane's two (`model_line`, `expected`) pass `cached=True`:
  hosted.json as it is, never rebuilt, so a 5 s poll never waits on OpenCode. `transcript._server_live` keeps `discover()`: an API model is
  never probed for `/metrics`; `identity` and `weights` say None and 0 for one, and `keep_warm` skips it.
- *Status* (`_api_status`): no network probe (no completion, no `/models`): `wb models`, scout,
  doctor and top call `status` for every model, and one request each would cost money or
  rate-limit budget. The config's refusals first (`window_too_small`, `tool_call: false`), then no
  `limit.context` ("OpenCode's catalog gives no context window for it, so wb can't plan its
  handovers"), an Ollama cloud model's local `/api/tags` check (not answering, not pulled: this
  machine's Ollama, not the network), the key, then `api-health.json`:

  | State | `why` | `may_come_up` |
  |---|---|---|
  | no key (`key == ""`) | `no key for <Provider>: run /connect in OpenCode and add it` | no |
  | `oauth`, `env:NAME`, `config`, `signing` | `can't run: needs a key wb can hold outside the lane (<Provider>'s is an OAuth login / from the environment, NAME / written in <file> / a cloud login that signs each request): store an API key with /connect` | no |
  | `no_credit` | `out of credit or quota at <Provider> (<line, redacted>): top it up, or wait for the plan's reset; wb never switches models` | no |
  | `rate_limited` | `rate-limited by <Provider> until HH:MM` | yes |
  | `unreachable` | `can't reach <host> (<reason>)` | yes |
  | ok | `{"available": True, "resident": True, "why": "API (<Provider>)"}` | |

  A "no key" answer is checked fresh first (`_fresh_key`: `auth list` again, ignoring the cache, once
  per state directory each `AUTH_FRESH_SECS` 5 s, written back to hosted.json): a user who just ran
  `/connect` isn't told to run it again. `remedy` gives `/connect`, top up, or wait;
  `may_come_up` follows the table (a queued task waits out a rate limit, never a missing key).
- `note_api(pid, state, why="", until=None)` writes `$WB_HOME/api-health.json` under
  `api-health.json.lock`: `no_credit` (402, a 429 saying `insufficient_quota` or a plan limit; no
  `until`: it holds until an `ok`), `rate_limited` (until `Retry-After`, else `RATE_LIMIT_SECS` 60),
  `unreachable` (connect, DNS, TLS, a 5xx: `DOWN_SECS`), `ok` (a 2xx) removes the entry. `why` is
  its first line, key-like strings (bearer tokens, `sk-`-style keys, `api_key=…`, 32+ character
  tokens) replaced by `[redacted]`, at most 200 characters (`_redact`, a simple local one; the
  keyhole's comes later, #1012). An entry past its `until` reads as ok (`api_health`). The keyhole
  calls it.
- *Who may run one.* A role on API (*Roles*, `wb start --role`, #1016), or `wb start --model <api
  id>` on the dialog's yes under Claude Code (`cli.API_RUNS` on; off, it exits 3 with
  `cli.API_NOT_YET` before any dialog); `wb try --model` refuses one (`<id>
  is an API model: wb try runs on GPU models only`, before the sample is copied); `wb model
  sizes|retire|rename` on an API id refuse (`sizes are for GPU models; API models run by role`,
  `cli.API_SIZES`), and `wb bench --model` refuses one (replays take the GPU lease).

**Thinking off by default** (#1018). An API task runs with thinking off unless its role says on
(`thinking: off|on`, decision 5 of #1008; task.json `thinking`, default off). Why off: API
providers mostly think by default (DeepSeek at effort high; not DeepInfra's) and bill reasoning at the output rate
(*API models through OpenCode*, *Cost*); a change to the switch or the effort between requests drops
DeepSeek's prompt cache; and DeepSeek's own V4 numbers that put thinking 5-8 points ahead on
SWE-Verified and Terminal Bench are self-reported, for V4, not V4.1. With tools, DeepSeek wants every
earlier `reasoning_content` sent back: OpenCode 2.3.2 does (checked), so thinking on works there too.
`runner.thinking_variant(m, thinking) -> (variant | None, shown)`, from the model's catalog entry:
- `on` (`runner._thinking_on`): a listed effort, never the bare id beside one: the provider's
  default (`DEFAULT_EFFORT`; `high` where it names none) when listed, else the lowest listed above
  it, else the highest listed, `thinking on (effort high)`; no effort listed, the bare id, `thinking
  on`; a model that doesn't reason, its line. Not every provider's default thinks: a DeepInfra
  model gave 0 reasoning tokens bare and reasoned at `#high`, so a role set to on ran
  without thinking under a `thinking on` line (2026-11-23, the API calibration).
- `reasoning: false` (a config entry), or `capabilities.reasoning.supported: false`: none, `thinking
  off (the model doesn't reason)`.
- the catalog's own variant that turns it off: `none`, or one whose settings or body say so
  (`reasoningEffort: none`, `reasoning.enabled: false`, `reasoning.effort: none`, `thinking.type:
  disabled`): `thinking off`. OpenCode 2.3.2 lists `none` for DeepSeek's, DeepInfra's and most
  OpenAI and OpenRouter models, but for only 2 of Anthropic's 20 (checked 2026-11-23, all 584
  catalog rows).
- variants listed, none of them off (`runner._no_off_listed`): never one that thinks more than the
  bare id, since a variant can turn on thinking the bare id leaves off (the #1018 review: the
  lowest listed sent Anthropic's older models `thinking: enabled` or `adaptive` where the bare id
  sent no `thinking`). Anthropic's whose every variant is `thinking.type: enabled` (manual thinking,
  a `budgetTokens`): the bare id, which OpenCode sends no `thinking` and the API runs without,
  `thinking off`. Anthropic's adaptive ones: the bare id, `thinking as the model's default (it lists
  no lower setting)`: some don't think on it, others do and refuse `thinking: disabled` with a 400,
  and the catalog doesn't tell them apart, so neither `wb-off` nor the lowest listed is safe there.
  Elsewhere the lowest listed effort (`runner.EFFORTS`' order, `none minimal low medium high xhigh
  max`) when it is under the provider's default (`runner.DEFAULT_EFFORT`: OpenAI-compatible and
  OpenRouter `medium`, DeepSeek `high`), `thinking at its lowest effort (it can't turn off)`; else
  the bare id and the default line (OpenRouter's `high`-only reasoning sends no `reasoning` field
  bare, but `reasoning.effort: high` with `#high`).
- no variants but `capabilities.reasoning` without a `toggle`: wb's `wb-off` at the lowest effort
  (OpenRouter `reasoning.effort: minimal`, else `reasoningEffort` the lowest of
  `capabilities.reasoning.effort`, both settings), the same line; with no effort listed, none, `thinking
  on (it can't turn off)`.
- no variants and the model reasons (a config entry's `reasoning: true`, or `capabilities.reasoning`
  with its `toggle`): `wb-off`, `runner.THINKING_OFF` by what the provider speaks (`runner.speaks`:
  the package or npm, else the base URL's host, `SPEAKS_HOST`; anything else is OpenAI-compatible):
  DeepSeek body `thinking: {type: disabled}` (a setting on a provider of the user's config, whose npm
  package sends V1 keys in the body: `runner._off_form`), OpenRouter settings `reasoning: {enabled:
  false}`, Anthropic settings `thinking: {type: disabled}`, OpenAI-compatible settings
  `reasoningEffort: none`.
- else (no variants: OpenCode's catalog lists them for every model it says reasons): none,
  `thinking off (the model doesn't reason)`.

`runner.with_thinking(m, spec)` fixes it in task.json at `wb start` (`opencode_model` `<id>#<variant>`,
`thinking`, `thinking_shown`, `thinking_off` for `wb-off`), for an API model only: a GPU task thinks
as its OpenCode entry says, exactly as before, whatever its role's `thinking`. Nothing after the
start recomputes it (feedback, next, resume, a handover keep `opencode_model`): a role's thinking
changed mid-task applies to the next task, since a change mid-task would drop the cache. The layer
carries it (*What the agent knows*, *The task's thinking variant*); `transcript.model_line` shows
`thinking_shown` (`<label> · thinking off · 1024K context`), never reading `wb-off` from the
user's config; results rows keep `thinking` (the weekly check's evidence for recommending it for
`significant`, in chat only, never set). `wb-off` is reserved: a user's own variant of that name
loses to wb's in the layer.

`wb remote HOST` (`models.add_remote`, #114): checks `/api/version`, writes provider `ollama-<host>`
(`remote_pid`; `remote_root` brackets IPv6, zone `%25`, read back as discovery reads it, #707)
with `local: true` and `remote: <root>`, then adds each `/api/tags` model whose `/api/show`
has `tools` through `wb model add`'s path (cloud rows left out; a refused one gets a `--` line, the
rest go on). That id already there for another baseURL is refused, never reused (#197). `--remove`
drops the provider and its models.json entries, only what wb made (the mark with the baseURL still `<root>/v1`; older:
name `Ollama on <host>`) (#242: it deleted a hand-written provider).

The id is `provider/name`; `root` is the baseURL less `/v1` or `/api` (`server_root`, #585; a
server's probe of an `/api` base asks `<root>/v1/models`, `openai_base`); `server` (`server_key`)
is (host, port, path) with every spelling of this machine one host, wherever roots are compared
(`free_gpu`, `_others_resident`, `can_share`, retire). A model's `name` is its name at the server
(the entry's `"id"`, an alias, else the key) for every probe, load and placement; the id routes
(`cli.served_name`, #588). The engine (`_engine`): models.json `engine`, else a cached `GET
<root>/api/version` (JSON `version` → `ollama`, HTTP error → `server`), else port 11434 or `ollama`
in the id (`engine_known` false). A probe unanswered is remembered `DOWN_SECS` 60 s (`_DOWN`, #337);
a pass of `status` calls shares a `down` set and `catalog` dict (`rank`/`choose`, `cmd_models`,
doctor, top), so a sleeping box costs one timeout (#705). An Ollama emulator is a `server`
(`models.emulator`, `_EMULATORS`): KoboldCpp by `/api/extra/version` or `details.family` (#580).
A disagreeing probe sets `providers()`'s `engine_note`, naming the `engine` override.

Requests to this machine (`_open`) bypass `http_proxy` (the 2026-11-16 audit); other hosts keep it.
`options.apiKey`, resolved as the agent sees it, is a bearer token on every probe to its root
(`_KEYS`, Ollama's endpoints too, #256), never printed or stored; else the key OpenCode stores
(`auth_key`, #336): `opencode auth export <provider> --standalone`, run outside the lane with the
user's environment and parsed in memory (OpenCode 2.x keeps `/connect` and `opencode auth login`
keys in its database and writes no `auth.json`: on 2.3.2 the rows are `{id, integrationID, label,
active, value: {type, key}}`; only `key` rows, the active first, never an OAuth token, whose
refreshed copy would log the user's OpenCode out; an integration OpenCode doesn't know exits 1,
"Integration not found", a miss), else 1.x's `$XDG_DATA_HOME/opencode/auth.json` `{"type": "api",
"key"}`. Cached per process, a miss included (`_AUTH`: each call starts an OpenCode server, about
0.9 s), asked only for a provider on this machine with no `options.apiKey`, and by discovery only
when wanted: such a provider's root is in `_STORED`, and a request `_open` sends there without a
key of the caller's that comes back 401 or 403 asks once and goes again with the stored key (kept
in `_KEYS` for that root). Asked up front, every discovery started OpenCode, the pane's `wb watch
--json` on each refresh too, for a stock Ollama that wants no key (the #1010 review). The layer
(`runner.stored_key`) and `providers()` (`wb doctor`, `wb scout`, `wb model`) ask outright. A
failure is said once
on stderr, its first line, key-like text redacted (`_KEYLIKE`). The agent's OpenCode, in the task's
own home, has no stored credentials (#1010), so the layer carries that key for the task's provider
(`runner.stored_key`, `models.stored_key`: `provider.<pid>.options.apiKey`, the layer 0600 as for a
key copied from the config), only for a provider discovery passes as this machine's or `local:
true`; a cloud provider's key never reaches a layer. A `{env:NAME}` key the agent won't get makes
the model unavailable ("… add NAME to commands.json env").

**Ollama cloud models** (`:cloud`, `-cloud`, a tags row with `remote_host`/`remote_model`) run on
ollama.com, so they are API models (#1013), never GPU candidates, and `local: true` no longer makes
one a candidate (it was a `remote` one the router could explore to, sending a task's code to
ollama.com unchosen). By name, `discover()` leaves one out and `hosted()` lists it (`kind` api,
`key` `ollama`, which needs no keyhole); one Ollama forwards under another name is known only by its
tags row, so `discover()` still lists it and `status` refuses it (`CLOUD_WHY`: never a GPU model, and
not an API model either, as `hosted()` knows them by name: add it by its cloud name), `weights` 0.
`wb model add` refuses a cloud-named one unless models.json gives its id `"local": true`
(`cloud_allowed`: the entry is then written, an API model still), and one under a plain name always
(`local: true` let it in, as a model nothing could run); scout lists them apart; doctor says once `<id> is an Ollama cloud
model, an API model now: set a role to use it`, skipping its other checks.

**models.json** (`$WB_MODELS`, strict JSON, malformed breaks every command; `set_override` via
`store.edit_config`): only what OpenCode can't say, keyed by provider or model id, model over
provider:
- `start`/`stop` (start detached, status every 5 s up to 20 min; stop 300 s); `skip`; `sizes`
  (empty is both); `vram_gb` (decimal GB); `weights_gb` (overrides the server); `label` (also in
  `wb accept`'s commit); `engine` (`ollama`/`server`, per provider); `gpu_gb` (top level: the
  budget by hand); `local: true`;
- `same_gpu: true|false`: a `remote` server on this GPU (unset: true for `models.windows_host`);
- `remote: <root>`: `wb remote` made it (#197);
- `keep_loaded` (top level): ids or Ollama names never unloaded, stopped or measured by `wb
  optimize` (#254); an id only when it starts with a configured provider, else a whole Ollama name,
  untagged as `:latest` (`models._kept_name`, #461);
- `variant_of`/`replaces`: a `wb optimize apply` variant's source and parent; one record
  (`router.lineage`), the variant first (#584, #723); `wb model retire` keeps both;
- `renamed_from`: `wb model rename OLD NEW`, followed back (`router.earlier`): score, slice, knobs
  (`tuning._rows`), speed and Stats line carry over (#818). Without it, a vanished id whose newest
  row ran on the digest the model has now (`weights_id` vs `models.identity`) links the same way;
  `router.speed` only from an identity the caller has (#905: `wb top` and Stats call it per tick).
  A `bytes:` size is too weak to link.

**Status** (`status` → {available, resident, why}). Config first (the fake models meet it too):
`tool_call: false` (missing is capable), then an API model's own path (*API models*), then
`url_problem` (an Ollama provider whose baseURL doesn't end in `/v1`, or an `apiKey` the agent won't
get). Ollama: not answering / not pulled / a cloud row (`CLOUD_WHY`) / pulled / loaded (`/api/tags`, `/api/ps`). A server (`GET <base>/models`): listing
none is unavailable; listing several, it must list this name; a llama-server listing one
(`owned_by: llamacpp` or `meta`) answers any name, any other listing one is strict ("the server
serves <id>, not <name>: set the entry's id to <id>"; basename match, `_serves`, on llama-server
only, #339). A port several models share, each with its own `start` (`_contended`), says by its
listed id which one runs (`_owners`: name, basename without `.gguf`, or `-m <path>`); another reads
available, not resident, when the served one has a `stop` (`make_resident` switches them); else
unavailable. 401/403 is unavailable ("the server refuses its API key" / "it wants an API key"),
neither running nor stopped (#336). Stopped with a `start`; not running without. `remedy(m, state)`;
`may_come_up(state)`: may be up by a queued task's turn.

`weights()`: `weights_gb`, Ollama's size, `server_facts`, the file a `start` serves (`start_file`),
else 0. For ordering only (#704), `guessed_weights` reads a parameter count from name or label
(`PARAMS`: `8x7B` as 56, not an active `a3b`), a byte a parameter; `fits` never uses it.

**What other servers say** (`server_facts(root, name, key)` → `{context, weights, tools}`, None
unsaid; cached `FACTS_TTL` 30 s, misses too; fields from each project's docs and source,
2026-11-17): LM Studio 0.4+ `GET /api/v1/models` (`size_bytes`, a loaded instance's
`config.context_length`, `capabilities.trained_for_tool_use`); llama-server's `meta.size` and `GET
/props?model=<id>&autoload=false` (one slot's `n_ctx`; no autoload in router mode); vLLM's and
SGLang's `max_model_len`. Sharing stays Ollama's: the others can't say what they hold.

**Residency** (`make_resident(m, keep, warn)`, by the runner under the lease before every round). A
model that can't run raises `RuntimeError` before anything is freed. Nothing to do when `m` is
resident alone or all in VRAM with room, unless a local Ollama model partly on the CPU would fit
whole (`_respread`; once per (root, name, size), `_RESPREAD`): after `free_gpu` it is unloaded and
reloaded, as Ollama reuses a runner as placed (#703). LM Studio: a listed model with no instance
(`lmstudio_unloaded`: JIT lists every download) is loaded after `free_gpu` via `POST
/api/v1/models/load` at `limit.context` (#335: left to OpenCode it loaded at 4K); one loaded shorter
(`_lmstudio_short`) is reloaded (#645); with JIT off `status` asks `lmstudio_unloaded` first (#462).
Nothing is freed when `m` fits beside what is resident (`_room_beside`: Ollama only, known budget,
no other server holding memory (`_servers_hold`; LM Studio only with an instance, #808), loaded-model
limit not reached, `/api/ps` sizes plus `need(m)` within `SHARE_AT`; the 2026-11-19 audit: every
round unloaded the user's Open WebUI model).

Else `free_gpu(keep={m, *keep}, of=m)`, skipping Ollamas `apart` from `m`'s: each other running
server with a `stop` is stopped once, after waiting up to 10 min while busy (`_server_idle`:
llama-server's `/slots` `is_processing`, else `/metrics` as JSON `live.state` or Prometheus
`BUSY_GAUGES`; #465, #715; nothing said is idle); one with no `stop` gets a warning. Then up to
`STOP_SETTLE` 30 s until each stopped server stops answering, or with one stopped until used VRAM
falls by `FALLING` 64 MiB (`_vram_used`, #898); one still answering goes in `_STOP_STAYS`, later
waited on by VRAM alone; then `released` until VRAM stops falling (`media.settle` alike; #703: an
immediate load split the model). Every Ollama model loaded is unloaded (`keep_alive: 0`) except
kept and `keep_loaded` ones (a server holding one is never stopped; `_others_resident` skips them).
A server listing several models is one, kept when a kept model is on it, unless contended and
serving another. LM Studio never evicts what `/api/v1/models/load` loaded, so its unkept instances
are unloaded once idle (`_lmstudio_extra`, #645), and it is never stopped. `releasable(m)`: what
`free_gpu` can free; `freed(m, keep)`: what it would.

Before unloading, `_wait_unused` watches `/api/ps` `INFLIGHT_WATCH` 10 s (`_in_use`): a moving
`expires_at`, or one past (`_past`: by more than the machine slept, `slept()`, #716; a box's
`BOX_SKEW`), is a request this account's lease doesn't know (another account, Open WebUI). It warns
("<names> on ollama is answering requests that aren't this account's tasks …"), waits up to
`INFLIGHT_WAIT` 10 min for a quiet watch, polling at most each `INFLIGHT_POLL` 1 s (#459), then
unloads anyway (#378: two accounts unloaded each other's model every round). Missed: one long
outside request that neither ends in the watch nor outlives its keep-alive.

**One lease for every account** (#393): `models.lease_dir()` is `$WB_GPU_DIR`, else `SHARED_GPU`
(`/var/lib/workbench/gpu`) if writable, else `$WB_HOME`, on a network file system
`$WB_HOME/machines/<store.machine()>/` (`_own_lease`, #849). An admin opts in with `sudo install -d
-m 2775 -g <group> /var/lib/workbench/gpu`. Lock files open read-write where writable, else
read-only (`_lease_lock`, `_lease_fd`, `store.lock_fd`; #892: NFSv4 refuses flock on read-only
alone); `gpu.json` is replaced by rename; all files `LEASE_MODE` 0664 (#802), the unreadable named by
doctor (`unshared_lease_files`). Entries carry `uid`/`user`; a pid refusing a signal is dead unless
the entry is another uid's with a `start`, then alive when its start matches or can't be read
(hidepid, #806). Another account's model is usually unknown here, so
they take turns (`busy`: "<user>'s task <t>"). The directory is in every lane's `private_paths` (an
agent's flock would hold every account's GPU). Without it doctor names models no task here holds
(`unheld_loaded`); one this account can't write is BAD.

**Loading.** Ollama: `POST /api/generate {prompt: "", keep_alive: "30m"}` (a box: `BOX_KEEP_ALIVE`;
20 min limit), errors with Ollama's text (`_post`); else the server's `start`. A start exiting
non-zero while nothing answers fails after `START_GRACE` 10 s ("… run it by hand to see why:
<cmd>"); exit 0 keeps waiting (it daemonized); probes paced 5 s on a monotonic clock (the 2026-11-19
audit: back to back). `_placement` warns "<name> is N% on the CPU …". Warnings go to `warn` (status
`last`, a `gpu` event, `■ GPU: …`); seconds to `status.load_secs`. Every round, since each request
resets `OLLAMA_KEEP_ALIVE` (5 min); mid-round, during `background_wait` or `asking`, the runner
renews it every `runner.KEEP_WARM_S` 180 s (`models.keep_warm`: only while `/api/ps` lists the model,
never loading one gone, which would skip `free_gpu`; not on another machine's GPU; #864).

**Sharing the GPU** (`GpuLease`, `admits`, `can_share`, `budget_devices`, `gpu_budget`, `need`).
The budget (`budget_devices` also gives its devices and source, for `cli.gpu_line`): models.json
`gpu_gb` (decimal GB, not a number an error; `gpu_line` names it whenever a compute GPU, `hardware.COMPUTE`, publishes no
size, Intel's i915/xe, with a same-vendor example, `_card_example`; #272, #710, #522). Else the
largest driver group's sum (NVIDIA cards add, another vendor's doesn't), devices under `IGPU_UNDER`
1 GiB left out. `hardware.gpus` marks `discrete` only on evidence (nvidia-smi, amdgpu's
`mem_info_vram_vendor`, a non-APU hwmon, `hardware.apu` False, #435); once any is, only those count
(an APU carve-out is VRAM to amdgpu). Only devices Ollama may see (`hardware.visible` over
`ollama_env()`). An APU: carve-out plus GTT, at most `carve` plus `MemTotal` less `system_reserve`
(#712). An NVIDIA GPU sharing RAM (DGX Spark, Jetson): RAM less `system_reserve` (what the system
uses, `MemTotal` − `MemAvailable`, less this Ollama's `/api/ps`), at least `SYSTEM_FLOOR` 2 GiB
(#524). Apple Silicon: `hardware.wired_limit` (`iogpu.wired_limit_mb`, Ventura's
`debug.iogpu.wired_limit`, `wired_key`, #714), else `unified_default` (two thirds of RAM up to
`UNIFIED_SMALL` 36 GiB, three quarters above); on macOS only an Apple GPU counts (#711). Unknown
VRAM: no sharing. `gpus(windows=False)` asks WSL's PowerShell only when Linux sees no GPU
(`wsl-gpus.json`, hourly), each Windows adapter its own group.

`need`: `vram_gb`, else Ollama's loaded size, else weights × `KV_GUESS` 1.25. `can_share(m)`: all
running models and `m` local Ollama, needs of `m` and the models not `apart` from it within
`SHARE_AT` 0.9 of the budget; the same model again needs an `OLLAMA_NUM_PARALLEL` slot per task (two
running need 3); distinct models within `OLLAMA_MAX_LOADED_MODELS` per server (`max_loaded`; unset
or 0: `OLLAMA_MAX_LOADED` 3 per counted GPU, #466; cards counted under `gpu_gb` too,
`models._read_devices`, #811). Resident `keep_loaded` models on `m`'s cards no task runs
(`_kept_beside`) count in both (#803). Ollama's settings: `hardware.ollama_env_at`.

Admission is under the `gpu.admit` flock. Holders are live `gpu.json` entries (`_alive`: the start
time, `/proc/<pid>/stat` field 22 or `ps -o lstart=`, must match `start`; `models._ps_start` runs ps
with `TZ=UTC0`, `LC_ALL=C`, #848, and `_same_start`/`same_tag` also try the inherited zone, the
older format). `admits(m, held)`: nobody waiting, every holder's model resolves, and no holders or
`can_share`; then `LOCK_SH|LOCK_NB` on `gpu.lock`. Else it is `waiting` ("waiting for the GPU (held
by <task>…)"), first come first served by `since` (task.json `created_at`; `wb image`/`wb audio`:
when asked), then name, so a task sent back keeps its place (#108). A waiting lease holds no lock
(`_take_alone`, #366); only the first waiter tries `LOCK_EX|LOCK_NB`, the rest watch gpu.json
(`watch.Watcher`, at least every `QUEUE_POLL` 1 s). Exclusive, it rewrites gpu.json to itself plus
the waiters; a lease failing or stopped while queued removes its entry. `load()` serializes under
`gpu.load` (lock order `gpu.load` then `gpu.admit`, only to read holders), so admissions and exits
never wait on a load. After a shared load that loaded something, `_fits_beside` checks `/api/ps`:
every kept model loaded just before and every `keep_loaded` one still there, `m` all in VRAM (one
gone before the load was keep-alive, not eviction); else it warns "ollama could not fit … waiting for
the GPU to itself", unloads its own model (unless a holder runs it), waits and loads alone.

`gpu.json`: `{"<pid>": {model, task, since, start, uid, user, waiting?, waiting_since?, tests?,
cards?}}` (`tests`: `--unload-for-tests` tests, `model` null; `cards`: a pinned media engine). A
lease with no model is alone unless pinned (`GpuLease(None, …, cards=)`: shared when every holder is
an Ollama task known on none of its cards, `apart_cards`, or an engine on others; #752). `wb image`
/ `wb audio` take `GpuLease(None, "wb image"/"wb audio", wait=…)`: with `--wait` they queue and hold
the GPU alone, `wb start` refuses meanwhile (exit 4, *Two projects, one GPU*) unless `--queue`, and
`wb feedback`/`next`/`resume` queue behind it; without `--wait` any live holder refuses
(`models.busy`).

**Which pool** (`models.pool`): `gpu` for local models and remote ones on this GPU
(`models.same_gpu`); None for an API model, which takes no GPU lease (#1015); else `gpu-<host>-<port>.{lock,json,admit,load}`, one task at a time, never
waiting on this GPU (#116); exit 4, `--queue` and the bench's preemption apply per pool. `free_gpu`
never touches another machine's models. A same-GPU remote model never shares, but `free_gpu`
(`models.on_this_gpu`) unloads it, `make_resident` frees for it, `releasable` and `media.settle`
count it, its unloaded tests (`step_aside`, `verify`) hold this GPU, `wb optimize` probes it under
one lease. A media engine joins `gpu` when on this GPU (`media.on_gpu`, `media.shares_gpu`), a box's
pool otherwise (`media.boxes`), or none (`"gpu": false`).

**Routing** (`router.rank`, `choose`). Rows: `store.results()` kept by `tuning.judged`, for the
model, its `lineage` and the role (`router.of_role`: a row's `role`, else its `size`; with no role
asked, the size's; *The writing role*). `judged` drops `--spec-gap` discards and rows whose agent never
ran (`ran` false, `failed_before`; older: not accepted, `tok_out` 0; #581).
`router.same_weights`/`weights_now` drop rows on other weights (`weights_id`, `models.identity`:
Ollama's digest, else a server's bytes) from routing, slice and tuning (#726); `router.same_kind` the rows of the other kind (#1022). Quality = (accepted
+ 1) / (tried + 2), accepted being the agent's share (`tuning.credit`: 1 when `tuning.agent_landed`;
after `wb takeover` the agent's steps only, #647; a discard keeps its committed steps,
`landed_steps`, #722); on the repo's stack (`gitops.stack`: up to two languages each ≥ 20% of
tracked source, the package's when it has one, #820) (like_accepted + `LIKE_PRIOR`·overall) /
(like_tried + `LIKE_PRIOR`), 2. Counts decay (`_weight`: half per `HALF_LIFE_DAYS` 60; `Score.w`);
`Score.line`'s `tries`/`wins` stay plain (#648).

Within `QUALITY_MARGIN` 0.15 of the best, the guess (`_guess`): fits the GPU first (`fits`: all in
VRAM, or `vram_gb`/weights within 90% of the budget), then largest for `significant`, smallest for
`trivial` (name-guessed `Score.guessed`; unknown last), then resident. Then `_cheaper`: once the pick
and another have `COST_MIN` 3 tasks of the size with a results `claude`, one `COST_MARGIN` 15%
cheaper per landed step (`_per_step`: weighted `claude` over `tuning.credit` × steps, #903) goes
first, never a spilling one over one that fits; why: on 2026-11-18 a smaller model cost Claude more
in review than a larger one. The rest by quality, unavailable last; only a model that can run is
weighed or fit-checked. `allowed()`: models.json `sizes` (none left: "no GPU model takes <size>
tasks (…: limited by wb model sizes; …)").
**The refusal names the GPU, and the API choice** (#1024): with no model to pick, `choose` returns
`no GPU model can run: <id>: <why>…`, `no GPU model takes <size> tasks …` or `no GPU models: <why
none>` (they said "local", which read on an API-ok setup as "nothing can run"). When a provider has a
key wb can hold (`models.connected()`, a fresh `auth list`), `router.api_choice(role)` appends `; or
put <role> on an API model you connected (the user's yes): wb route <role> <id>, wb models lists
them` (`choose(…, role=)`, from `routes.resolve`; the size's name otherwise): the one line of choices
`wb start` exits 3 with; none while Agents is GPU (`use`), where a role's API model wouldn't apply.
Never a fallback: Claude asks the user.
**Exploring** (`choose(size, stack, task_name)`): one task in `EXPLORE_ONE_IN` 10 (`explores`, a hash
of the name) goes to the usable model outside the band with fewest tasks while under
`EXPLORE_TRIES` 3 (`unproven`; one that would spill only when the pick would too), #648: one bad first task buried a model. No name
(`--set-default`), no exploring. `choose` returns a reason; one whose size is unknown says "sizes
unknown: config order" (#704). `wb models` prints the ranking per size.

**What `wb models` prints** (`cli.cmd_models`, #1026). Each GPU model's state, slice, speed, bench
line and size, the ranking per size, then one API section (`_api_section`), with no completion and no
probe of a provider (hosted.json, status's key source and api-health.json). The catalog is read again
(`hosted_doc`: two OpenCode starts, a hung one `CATALOG_TIMEOUT`) only with no GPU model or a role on
API; a GPU-only user's section reads hosted.json as it is, never rebuilt, so `wb models` waits as it
did before API models (#1026's review: 4 s on a slow read, every `HOSTED_TTL`), and with no hosted.json
yet says `catalog: not read yet (wb models --api <text> reads it)`. Each role (`significant`,
`trivial`, `writing`, from `routes.read`, where each runs by `routes.effective_via`): `gpu (the router's pick above)` or its pinned GPU id, `· API kept: <id>` when one is stored; an
API role its id, `ready` or status's `why` (or `not in OpenCode's catalog now`), `· thinking off|on`,
`· set by you MM-DD` (`_set_on`: none for a `ts` that isn't a time, a hand-edited 1e20 or NaN); a
routes.json that can't be read is one line, `roles: routes.json can't be read: wb route sets them
again`; then `keys:` one per provider with a key source (`stored`, `Ollama's login`, or
`environment` / `OAuth login` / `in a config file` / `cloud signing`: `can't run, needs a key wb can
hold outside the lane`); `catalog:` one count per provider, never the ids (OpenRouter alone lists
300+), a provider's free models noted `free: the provider may keep or train on your code`
(`FREE_NOTE`, #1021's words). With no role on API (or kept), no key and a routes.json that parses,
the section is two lines: `none set up, every role on the GPU` and the catalog's counts. With no GPU
model, the GPU part is `no local models: <why_none()>` and the section still prints, exit 0, when a
role is on API or a key is stored; otherwise it dies as before. `--api TEXT` (`_api_list`) lists the
catalog ids containing TEXT (any case), sorted, at most `API_LIST_MAX` 30, each with the catalog's own window (`catalog_limit`),
`$in/$out per 1M in/out` from its first untiered price and the free note, then `and N more: a longer
text narrows it`; it lists only, never sets a role. `--set-default` stays GPU (`router.choose` over
`discover()`): never an API model as OpenCode's default.

**The slice** (`router.slice(m)`), one per model whatever the role (writing slices like
significant work: the window's measures hold for any text). Guess: s = min(window/32768, `GUESS_MAX` 4) (no window: 1),
files = max(1, round(3s)), lines = max(50, round(15s)·10): 3/150 at 32K (measured 2026-11-16).
Corrected from the last `SLICE_RECENT` 20 steps on that model and window (`judged` rows' `steps`,
files ≥ 1, generated files excluded, `gitops.footprint`). `_went` badly: the step compacted (its last
session or one a handover replaced, the `fresh` event's `compacted`; not one a later step's first
round replaced, #958), filled up reading (`read_full`), got stuck by `guard` or `agent`
(`results.THE_STEP`, an allowlist; old cause-less `SIGNAL_STOP`/reap/`UPDATED` notices
excluded; #582, #660), or took ≥ `BAD_REVIEWS` 2 agent-caused review rounds; well: landed, not taken over.
Files and lines learn apart: the smallest bad step caps (files − 1, lines × 3/4); after `FIT_MIN` 2
good steps the size `FIT_SHARE` 75% reached raises it, past the cap only when under the bad step's
size or steps that big went well at least as often (#720: the biggest good step once beat every
cap, and one lockfile bump left a 128K model at ~2,400 lines). A handover isn't held against a step.

**Budgets and speed** (`router.budgets`, `record_speed`, `speed`). After each round `measure_speed`
reads the export; `record_speed` keeps write speed ((output + reasoning) / (completed − first
token), ≥ 100 tokens over > 1 s) and read speed (input / (first token − created), ≥ 1000 tokens
over > 0.5 s, cut-short requests too), the last `SPEED_KEEP` 60, in `speed.json` (`{"<id>": {"write":
[...], "read": [...]}}`, `speed.lock`) in `store.machine_dir()` (#901). `speed()`: medians of the
first row here among the id, its earlier ids (`renamed_from`, `earlier`'s digest match with the
caller's `weights`), what it `replaces`, its `variant_of` (#906); only if none, the same chain in
`$WB_HOME/speed.json`. It asks no server (#905). `budgets(m)`: think = `THINK_TOKENS` 13,500 / write
+ window / read (either alone, #491), clamped to [420, `THINK_MAX` 1800] s; silence 900 and round
2400 scale by think / 420; no speed, defaults. Written into task.json at start; before each round
`runner.stretch_limits` raises those no flag gave (`timeouts_given`) where `budgets(m)` is longer,
into status `limits` (the guards and `chunkTimeout` via `runner.limit`), with a `limits` event
(#814). A flag's value never changes.

**An API model's budgets** (#1017). `budgets(m)` sends an API model (`m.kind == "api"`) to
`budgets_api`. Its samples are the account's, not this machine's: `$WB_HOME/speed-api.json`
(`{"<id>": {"write": [...], "ttft": [seconds...]}}`, `speed-api.lock`). `record_speed` writes there
for one (its `api`: the task's record, `runner.api_entry`, else `models.is_api`), and
`record_read_bound` and the runner's `read_bound` skip it: its time to first token is the network
and the provider's queue, not a read. `api_speed`: the write median and the time to first token's
90th percentile (`API_TTFT_SHARE`); `speed()` gives its write median. think = 13,500 / write + that
TTFT, clamped to [420, 1800], with no window / read term (a 1M window and a "read" speed from
network time gave 200-500 s of prefill no cached request pays). Silence and round scale by think /
420; silence is at least one reply at the output cap written in silence after the TTFT, plus
`SILENT_SLACK` 60 s (a reasoning step prints nothing until it ends: the spike), but that floor
stops at `SILENCE_MAX`, the GPU's own longest silence (900 × 1800 / 420 = 3857 s): uncapped, 2
tok/s gave 16448 s, and a request hung for good held the round and a provider slot 4.6 hours.
Round is at least the silence (so at most 10285 s). With write 50 tok/s and TTFT 4 s: 274 s, so
420/900/2400; at 20 tok/s 679/1703/3880; at 5, 1800/3857/10285. `thinking_stall` and its `wb
doctor` line skip API models. Measured 2026-11-23 on a hosted flash model (four bench cases replayed by hand, two trivial and two
significant, all passing their hidden checks in one round): write median 116 tok/s, TTFT p90 4.2 s,
so think 121 s and the 420/900/2400 floor holds; rounds of 0.6-3.0 min (the GPU's 2.8-23 min), $0.005-0.04
a task, the fullest session under a third of the 128K window, so the slice's guess stands. Its one
compaction came inside a single step: six shell calls at once, each with pages of compiler errors,
added 93K tokens, past `fresh_ceiling`'s one-step margin.

**Managing the set.** `wb model add <provider/model> [--pull] [--context N]`: pulls, reads
`/api/show`, writes `{name, tool_call, reasoning, limit: {context, input: ctx − ctx/8, output:
ctx/8}}` plus `options.max_tokens` = 2 × output (`ollama_cap`: Ollama ignores
`max_completion_tokens`; uncapped, a degenerate generation filled the context, 2026-11-16). Context:
`--context`, else what Ollama serves (`ollama_served_context`: loaded `context_length`, Modelfile
`num_ctx`, `OLLAMA_CONTEXT_LENGTH`), never a sibling's (#464) or the trained length (only a cap);
with none, refused (the 2026-11-16 audit: a 256K entry on a stock Ollama cut prompts). Another
server: `server_facts`, `--context`, a sibling's or the entry's; none refuses (no window, no
handovers or compaction). A `--context` past the server's gets a `--` line. An Ollama must answer
first (`ollama_answers`); it merges into an existing entry and reads `providers` too. Refused: a
`url_problem`, an Ollama cloud model without `local: true` ("… so it would be an API model, run only
through a role you set …"), one under a plain name always ("… only a cloud name (:cloud, -cloud)
makes one an API model: add it by its cloud name"), a down server of guessed engine with no `start`. First run:
no `ollama` provider and Ollama answering at `ollama_root_here()` → `add_ollama_provider` writes
`{"npm": "@ai-sdk/openai-compatible", "name": "Ollama", "options": {"baseURL": "<root>/v1"}}`.
`--url` (`add_server_provider`) writes the same for `<provider>`, refusing non-http(s) or off-machine
without `local: true`; an existing provider is never touched, and a different baseURL refuses
(#334). `--start`/`--stop` set models.json `start`/`stop`. wb isn't tied to Ollama (2026-11-17);
Ollama keeps pulls, deletes and sharing.

`wb model retire <id> [--delete]` removes the entry, refusing OpenCode's default `model` or
`small_model` (#708); `--delete` the weights, refused while another entry on the same Ollama
(`server_key`, #368; any configured provider, `models.configured_providers`, #458) resolves to them
(#333); results stay. `wb model sizes` sets `sizes`. `wb doctor` asks each Ollama once
(`ollama_answers`: a sleeping box cost 35 s a model, past Claude's 2-minute Bash limit); BAD: no
model can run, an entry without `limit.context`, an Ollama entry without `options.max_tokens`, a
`url_problem`, a `limit.context` over what the server serves (less or unknown: a note).
`ollama_served_context` skips `/api/show` when `/api/ps` didn't answer and reads
`OLLAMA_CONTEXT_LENGTH` only for a local Ollama. `wb scout` names each config file read and each
skipped provider.

**The machine** (`hardware.py`, read fresh each call; `gpu_budget` is on the routing and lease hot
path). `system()`: `macos`, `wsl`, `linux`. `gpus()`: `nvidia-smi` (`smi()`: PATH, else `WSL_SMI`,
which a systemd unit's PATH may lack; names may hold commas); `[N/A]` is unknown except on ARM
(`shares_ram`: `unified`, #524); a Jetson without nvidia-smi rows (`_tegra`) is driver `nvidia
(tegra)` (#713). WSL adds Windows' adapters (`_windows_gpus`: `powershell.exe -EncodedCommand` of
`WINDOWS_SCRIPT`, memory from the registry's `HardwareInformation.qwMemorySize` since `AdapterRAM`
caps at 4 GB), cached `WINDOWS_FOR` 1 h in `wsl-gpus.json`; PowerShell from PATH, else under WSL's
automount root (`hardware.powershell`, `windows_program`, `WINDOWS_POWERSHELL`). An empty read keeps
the last good one and retries after `WINDOWS_RETRY` 60 s (#702). Output is UTF-8 and `hardware._run`
decodes `errors="replace"` (#664), as do the skill library's git, `ip`/`ifconfig`, stop commands and
`ps`; the git shim's `--git-common-dir`, `surrogateescape`. `gpus(windows=False)` (the budget, `wb
image`'s settle) uses Windows' adapters only when Linux sees no GPU (#522).

Linux reads DRM `/sys/class/drm/card*/device` (amdgpu `mem_info_vram_total`/`_used`; an APU with
`mem_info_gtt_total` is `unified`, carve-out plus GTT). `apu(dev)`: True when the hwmon's `in1_label`
is `vddnb`, False with another hwmon, None without (#435). `DISPLAY_ONLY` drivers (BMC, VM, USB
display) are no GPU (#710, #551); NVIDIA without nvidia-smi is listed from sysfs, VRAM unknown.
macOS: `system_profiler SPDisplaysDataType -json`. Every size `wb` prints is decimal GB, the unit
`gpu_gb`/`vram_gb` take (#523; the pane's live view is GiB, labelled); `size` prints `?` only for
None (#717).

`gpu_power_w()` (each task's savings GPU energy; None on macOS): nvidia-smi's `power.draw`, else
hwmon `power1_average`, `power1_input`, else the average from `energy1_input` (i915, xe;
`_ENERGY`, #271); an APU's hwmon, the whole package's, is left out beside a card (#706). `memory()`, `cpu()`.

`ollama_settings()` (the running Ollama's environment and its source; `ollama_env()`, the
environment alone): `OLLAMA_KEYS` and `SET_ONLY` (`VISIBLE`'s masks and `CUDA_DEVICE_ORDER`). The
first source that answers is the whole answer: a readable `ollama serve`'s `/proc/<pid>/environ`
(`PROC`; macOS `_ps_ollama`, #804), lowest pid first; a running systemd `ollama` unit, system then
`--user` (`Environment` and each `EnvironmentFile=`, `_environment_files`; an unreadable one is
`UNREAD` in the source, #804); a running `ollama` container in docker/podman (`Config.Env`, and
`container_mask` from `HostConfig` devices, #463; the container's own masks indexed into its grant,
`hardware._within`, #651, unchecked on a real two-card machine); macOS's brew plist
(`_launchd_ollama`), else `launchctl getenv`; a stopped unit. Another mount namespace or a container
gives only `OLLAMA_KNOBS`; a containerized process defers to its container. Nothing: wb's own
environment, source unknown, which `cli.ollama_line` says (#379).

With several `ollama serve`s, `ollama_env_at(root)` is the one on root's port (`_ollama_processes`;
unset `OLLAMA_HOST` is 11434). The budget (`budget_devices(root)`, `gpu_budget(root)`), the router's
fit (a budget per server, #801), `can_share` and `_room_beside` use the task's server's devices and
knobs (#460). `models.server_devices(root)`: its devices, only when `hardware.ollama_matched(root)` is
sure, else None. `models.apart(a, b)`: both known, no card shared; unknown is never apart.
`free_gpu(…, of=m)` and `_others_resident` touch only servers not apart from `m`'s; `free_gpu()`
without `of` frees every card; `free_gpu(…, cards=)` (a pinned engine, `media.cards`) only those not
known apart (`models.apart_cards`), and `media.settle` waits on those (#752).

`ollama_context()`: trimmed as Ollama reads it, plus the raw value. `visible(devs, env)`: devices a
runtime sees, by number or UUID prefix. CUDA's order is PCI under `CUDA_DEVICE_ORDER=PCI_BUS_ID`,
else fastest first, guessed by `compute_cap` (a separate nvidia-smi query) then memory and flagged
`GUESSED` (#381, unmeasured). The first entry naming no device stops it (`-1` is none); unset is all,
empty too except `CUDA_VISIBLE_DEVICES` (`EMPTY_IS_NONE`); each mask counts within the one before
(HIP within ROCR); `GGML_VK_VISIBLE_DEVICES` counts every card (#255). `report()`: scout's lines.
Unknowns stay unknown; nothing about the machine is hard-coded.

**The weekly model check.** Models change fast. Once a week, in a quiet moment of a session that
delegates, with wakes on, the mod submits SCOUT_NOTE (see *The mod, inside*); `/workbench-models`
asks now, and `/workbench` runs one first when no role can run (the skill's first run; once a role
runs, on the GPU or on API, it never reruns on its own). It covers the GPU and API alike (#1024),
recommending only for API roles. `wb scout` prints the facts
fresh: the machine (`hardware.report`), Ollama's default context, every registered model (size,
quantization, context, slice, record), installed but unregistered models, the watch list
(`watch.json`; `--watch TEXT`, `--unwatch N`), flagged skills, waiting lessons, media engines (an
engine with no `notes` says "licence: not noted"; `audio.coverage`, #398), and its API section
(`cli.scout_api_lines`, when OpenCode lists an API provider or a role has an API model): the roles
(`cli._role_where`: `GPU (router)`, `GPU <id> (pinned)`, `API <id>, set by you MM-DD, thinking off`),
each role's API model's record over `SCOUT_API_DAYS` 30 (`cli._api_record`: tasks, accepted share,
`~$/task` from #1022's `results.row_cost`, rate-limited rounds from the tasks' `rate_limited` events),
the spend over `SCOUT_SPEND_DAYS` 7 beside the day cap (#1022's `savings.api_totals`, trials
included, per provider; its dollars and `~$/task` by `savings.usd`, as `wb savings --api` and the pane say them), and one line per connected provider counting its models with tool calls
(free ones, those the catalog doesn't say, a key wb can't hold): never each id, as a provider may list
hundreds. Only hosted.json's catalog: no network beyond it. Claude researches primary
sources, recommends keep / add / retire and changes nothing without the user's yes; `wb scout --done
--notes "…"` records it (`scout.json` `done_at`); `--due` says whether one is due.

### API models through OpenCode (checked 2026-11-23, OpenCode 2.3.2)
What 2.0 (#1008) builds on, from the spike (#1009). Checked against a recording model server (a
scratch variant of `tests/capture_llm.py`: path, header names, body; modes for reasoning and error
statuses) through the layer, a logging proxy (`HTTPS_PROXY`), the v2.3.2 source (tag `v2.3.2` of
OpenCode's repo; files named below are under its `packages/`), and six paid one-line runs on
DeepInfra. Scratch homes (`XDG_DATA_HOME`/`XDG_STATE_HOME`/`XDG_CACHE_HOME`) wherever credentials
weren't needed. CI pins 2.3.0: not checked there.
- **Catalog source.** models.dev v2 from `https://models.opencode.ai/experimental/v2.0/api.json`,
  fetched by every server start (`run --standalone`, `models`, `api`: the proxy saw `CONNECT
  models.opencode.ai:443` twice each, a retry), then on a TTL. It is cached in `opencode.db`, table
  `kv`, key `models-dev:v2:catalog` (13.7 MB here; key checked, not read), with a bundled snapshot when
  there's neither (`core/src/models-dev.ts`). A failed fetch doesn't slow a start (0.86 s). No file
  under `~/.cache/opencode` (only `bin/`).
- **Reading it.** `opencode api model.list` prints JSON `{location, data: [...]}`, each entry with
  `providerID`, `id`, `family`, `package`, `settings.baseURL` and `settings.apiKey` (a layer's or a free model's key: never copied, #1013), `capabilities{tools,
  input, output, temperature}`, `variants[{id, settings | body}]`, `cost[{input, output,
  cache{read, write}, tier?}]` (USD per million tokens; `tier: {type: "context", size}` prices past a
  size), `limit{context, output}` and `status`. It lists only providers that can authenticate (a stored
  key, an env key, a layer `apiKey`) plus OpenCode's free models. It takes 0.3 s through the service
  and 0.8 s with `--standalone`. `opencode models` prints ids only and takes no flags. A reasoning
  switch shows as the variants; there is no `reasoning` flag.
- **The two models.** `deepinfra/deepseek-ai/DeepSeek-V4.1-Flash-1017`: package
  `@opencode/ai/providers/deepinfra`, base `https://api.deepinfra.com/v1/openai`, context 1048576,
  output 384000, $0.20 in / $0.60 out / $0.006 cache read, variants `none low high max`
  (`reasoningEffort`). `anthropic/claude-haiku-5-5` (`opencode models` lists it): context 1000000,
  output 128000, $0.10 / $0.50 / $0.01 read / $0.125 write, and $0.50 / $2.50 past 100K. Its variants
  are `none` (`thinking: {type: disabled}`) and `low medium high xhigh max` (adaptive thinking plus
  `effort`).
- **Network.** `OPENCODE_DISABLE_MODELS_FETCH=1` stops the fetch (the proxy saw nothing), and the
  bundled snapshot serves the catalog. `OPENCODE_MODELS_PATH=<file>` replaces the catalog (`{}` left
  only config models), but the fetch still runs unless disabled. So real-OpenCode tests offline set
  the first.
- **Ids.** `Model.Ref.parse` (`schema/src/model.ts`): the provider is everything before the first `/`,
  and the variant is everything after the first `#` past it. `-m deepinfra/deepseek-ai/…` exports
  `model: {providerID: "deepinfra", id: "deepseek-ai/…", variant}`, with variant `"default"` when none
  is given. An unknown variant sends no request and exits 1 with
  `{"type":"error","error":{"type":"provider.no-route","message":"Variant unavailable for <id>: <v>"}}`.
- **A layer over a catalog provider.** V1 `provider.<p>.options.{baseURL, apiKey}` keeps the catalog
  model (its cost and variants). DeepInfra's package posts to `<baseURL>/openai/chat/completions`, so
  the override is the root without `/openai`, with the key as `Authorization: Bearer`. Anthropic's
  posts to `<baseURL>/messages?beta=true` with `x-api-key`, `anthropic-version: 2023-06-01` and
  `anthropic-beta`. Neither sends a GET. With no stored key and no layer key, the run exits 1 with
  `provider.no-route` "Model unavailable: <id>".
- **Variants in the layer.** V1 `provider.<p>.models.<m>.variants.<name>: {reasoningEffort: "none"}`
  merges into the catalog model, and `-m <id>#<name>` selects it. A V1 variant key the package has no
  setting for (`chat_template_kwargs`) is dropped silently (`core/src/v1/config/migrate.ts`). V2
  `providers.<p>.models.<m>.variants: [{id, settings, body}]` sends `body` verbatim:
  `chat_template_kwargs: {thinking: false}` reached the request. The request fields recorded: DeepInfra
  `#none`/`#high`/`#max` give `reasoning_effort` with that value; no variant sends no field.
- **DeepInfra thinking off (paid).** On the same reasoning prompt, `#max` thought (110 reasoning
  tokens, `content: [reasoning, text]`) and `#none` didn't (0 reasoning, 2 output). With no variant,
  with `#high`, and with a V2 `chat_template_kwargs: {thinking: true}` variant, one-line prompts got
  0 reasoning: the model thinks only when it judges it needs to. So DeepInfra's off switch is
  `reasoning_effort: "none"` (the catalog's `#none`). `#none` read 8832 cached tokens right after
  `#max`: in this one sample, DeepInfra kept the prompt cache across efforts.
- **Output cap.** `max_tokens` is `min(limit.output, 256000)` (`OUTPUT_TOKEN_MAX`,
  `core/src/session/model-request.ts`): 256000 for the DeepInfra model, 128000 for Sonnet 5.5.
- **Data home.** There is no OpenCode-only switch for the data directory: `util/src/global-roots.ts`
  takes data, cache, config and state from `XDG_*`, tmp from `os.tmpdir()` (`TMPDIR` moves
  `/tmp/opencode`), and home from `OPENCODE_TEST_HOME`. OpenCode's built-in instructions point the
  agent at that shared tmp. `OPENCODE_DB=<file>` moves only the db. Under `--standalone`, `run`, `run
  --session <id>`, `session list` and `session export` all used a scratch DB, and the service's
  export said `Session not found: <id>`. Without `--standalone` a command goes to the background
  service, whose own environment wins: `auth list` with `OPENCODE_DB` set still listed the user's
  keys. With `OPENCODE_DB` alone, `snapshot/<project>/<hash>` and `log/opencode.log` still landed in
  `~/.local/share/opencode`, and the source writes `tool-output/`, `shell/`, `worktree/`, `repos/`,
  `artifacts/` and blobs there too. A fresh DB file imports `<data>/auth.json` (migration
  `20260805200742_import_legacy_credentials`) and `<data>/opencode-next.db`, so `OPENCODE_DB` alone
  would copy a user's legacy V1 keys into the task's DB. `XDG_DATA_HOME` moved all of it.
- **Credentials.** They live in `opencode.db` (table `credential`). A scratch data home or DB has none:
  `auth list` showed only env keys, and `/connect` models were absent and unrunnable (above). `auth
  list --format json`: `[{id, name, connections: [{type: "credential", id, label, method: "key"} |
  {type: "env", name: "<VAR>"}]}]`. `auth export <p>`: `[{id, integrationID, label, active, value:
  {type: "key", key}}]` (checked by key names and types only). Each takes 0.13–0.24 s and needs no
  `--standalone`. An env key (`XAI_API_KEY` here) lists its provider's models even in a scratch home.
- **Cost.** Live: `step_finish.part.cost` (USD) and `part.tokens{input, output, reasoning,
  cache{read, write}}`. Export: `info.cost`/`info.tokens` for the session, and on each assistant
  message `cost`, `tokens`, `model{id, providerID, variant}`. OpenCode prices the usage itself from the
  catalog (`SessionUsage.record(usage, model.cost)`): a capture-server reply got a cost too, so it is an
  estimate, not the provider's bill. `input` excludes cache reads, and reasoning is billed at the output
  rate (the `#max` run: 5233×0.20 + 3712×0.006 + 113×0.60 per million = $0.001137, as reported).
  `info.cost` includes the title request, which no message carries (two turns plus a title = 3×
  $0.000212). `run --title <t>` skips the title request.
- **Errors.** A 429 prints `{"type":"retry","attempt":N,"nextRetryAt":<ms>,"error":{"type":
  "provider.rate-limit","message","status":429,"response":{"body"},"retryAfterMs"}}`, with
  `retryAfterMs` only when `Retry-After` was sent. There are 10 retries, exponential from 2 s capped at
  10 s, jittered, with `Retry-After` as a floor (capped at 15 min; `core/src/model-retry.ts`). Then
  `{"type":"error","error":{"type":"provider.rate-limit",…}}` and exit 1: 86 s and 13 requests here.
  401 and 403 give `provider.auth`, and 402 `provider.quota`: no retry, exit 1 at once. The upstream
  body comes through verbatim (a key-like string in a 401 body reached stdout).
- **Reasoning is silent.** While the model streamed `reasoning_content` for 6 s, `run --format json`
  printed `step_start` (0.8 s) and then nothing until `text` (6.8 s). With `--thinking`, one
  `reasoning` line with the whole block arrives, but only when it ends. The silence and think clocks
  hear a reasoning model only at its step's start and end.
- **Titles.** V1 `small_model` is migrated to V2 `agents.title.model`, and either one pins the title
  model, at its lowest variant (`none`/`minimal`/`low`; Sonnet got `effort: low`). With neither, the
  title goes to the provider's small model, the first of the families `gpt-luna`, `gemini-flash-lite`,
  `gemini-flash`, `claude-haiku` (a Sonnet task's title went to `claude-haiku-5-5`). A provider with
  none of those falls back to the task's model at its lowest variant (DeepInfra: the same model,
  `reasoning_effort: none`). A failed title is tried once more on the task's model
  (`core/src/session/title.ts`, `context.ts` `selectTitle`).
- **Thinking in the layer** (#1018, checked 2026-11-23 on 2.3.2 with `capture_llm.py`, offline). A
  document with `provider.<p>` (V1) and `providers.<p>` (V2) for the same provider reads only the V2
  one: V1 `options.baseURL` was lost and the run went to the catalog's host. Another provider's V1
  entry beside it holds. V2 `settings.{baseURL, apiKey, headerTimeout}` and a V2 model's `limit`
  work as V1's `options` and `limit` do (a 1.5 s `headerTimeout` cut a 4 s header wait; `limit.output`
  1234 sent `max_tokens` 1234). A V2 variant list merges with the catalog's by id. `small_model` (and
  V2 `agents.title.model`) take `#<variant>`: a marker in a `wb-off` body reached the title request.
  DeepSeek's catalog `#none` sends `thinking: {type: disabled}`, the title too (its lowest);
  no variant sends no `thinking`. Anthropic's Haiku with no variant got `thinking: {type:
  adaptive}`. With thinking on and a tool call, the next DeepSeek request carried the assistant's
  `reasoning_content` back.

## Roles: which model a kind of work goes to

Three fixed roles (`routes.ROLES`, #1014): `significant` (complex coding), `trivial` and `writing`
(prose). The pane names them **Complex coding**, **Trivial coding** and **English writing**
(`routes.LABELS`); files and commands keep the role names. Each role stores both sides and the
selector between them, so flipping never loses the other side's model: `via` (`gpu` or `api`), `gpu`
(a GPU model's id, or `auto`: the router picks, the default), `api` (an API model's exact OpenCode
id, kept while the role is on the GPU) and `thinking` (`off`, the default, or `on`). One spend cap
for the day, `day_usd`, asked when the first role goes to API (`DAY_USD_SUGGEST` $5 is only the
suggestion: there is no default cap). Kept in `~/.config/workbench/routes.json` (`$WB_ROUTES`,
`routes.path`), which no agent reads (`context.private_paths` adds `routes.path()`, wherever it is).

**The Agents switch** (#1016, the owner's request in #1023): routes.json's top-level `use`
(`routes.USES`: `gpu`, `api` or `both`; missing is `both`; any other value a `RoutesError`), named
**Agents** on screen (`USE_NAMES`: GPU, API, Both). `routes.effective_via(doc, role)` is `use`
unless it is `both`, else the role's own `via`; everything that decides GPU or API for a role asks
it, never `via`. `wb route all gpu|api|both` sets `use` alone, never a role's `via`, so going back to
Both restores the per-role mix. With Agents on API a role with no API model can't start (exit 3:
`<role> role has no API model, and Agents is set to API: ask the user to pick one (wb route <role>
--api <id>) or set Agents to Both or GPU (wb route all both|gpu)`).

**When the GPU is busy** (#1023, the owner's opt-in, off by default): routes.json's `busy`
(`routes.BUSY`: `wait`, missing, or `api`; any other value a `RoutesError`), set by `wb route busy
wait|api` and the pane's **When the GPU is busy** row. With `api`, a `wb start` that would wait for
the GPU (exit 4, or queued with `--queue`) for a role with an API model, no `--model` asked for and
Agents not GPU (`routes.busy_api`) starts on the role's API model instead (`cli._busy_api`), and says
`GPU busy: on the role's API model, as you set` (`routes.BUSY_LINE`) under its model line and in
task.json's `route` (`busy: true`, `routes.busy_routed`, with an OpenRouter role's `order` as
`routes.resolve`'s). A role with no API model queues or exits 4
as before; one whose API model can't take it now (not in the catalog, no key, its provider's slots
full, a repo or task kept local, a standing commands.json `copy`/`siblings` the command line
doesn't name, `cli._standing_gap`, the day cap reached, said without an API start's advice) does too, after `note: the GPU is busy, and the <role>
role's API model <id> can't take it: <why>`. A running task never moves, and nothing goes from API to
the GPU. Turning it to `api` takes the dialog's yes (it can spend) and a day cap (`NO_CAP`); back to
`wait` none. While it is on, any change that lets a role newly start on an API model while the GPU is
busy (an API model set on a GPU role, Agents back from GPU, an OpenRouter role's new backends:
`routes._busy_newly`, which compares `routes._sent`, the model and its `order`) takes the yes and a
cap too, adding `when the GPU is busy, <role> starts on its API model (your code goes to …; cap $N/day)`. `routes.summary` ends `; when the GPU is busy, a role with an API model runs on it` while
it is on.

**Unset is the GPU, exactly as before.** A missing file or role reads as `{via: gpu, gpu: auto,
thinking: off}` (`routes.read`, or `routes.load`, the same; `normalize`). A file that can't be read or isn't what wb writes (not
JSON, an unknown role, a `via` or `thinking` outside its values, a role on API with no `api`, a cap
that isn't a positive number) raises `RoutesError`, never reads as all GPU: that would move the user
off their API choice without a word. `wb start` exits 3 on one (`<path> can't be read (<why>): fix
it, or wb route <role> … sets it again`, before the model is picked), `wb route` (show) the same,
and `wb doctor` says it as `BAD`. A change starts over from the defaults, and its line says so (`routes.json couldn't be read (<why>), so every role starts over on the GPU: …`), which
takes the dialog's yes.

**Followed by `wb start`** (#1016, *A task, start to finish*): every start resolves a role
(`--role`, else from `--size`), and a role on the GPU with a pinned `gpu` runs that model, the
router's ranking, exploring and `_cheaper` not applying (`the <role> role's GPU model (wb route)`;
a pin no longer found exits 3, never the router's other pick).

**`wb route`** (`cli.cmd_route`): shows each role (`GPU (the router picks)`, `GPU <id> (pinned)`,
`API <id> (<provider>) · thinking off`, `API kept: <id>`, `set by you|Claude MM-DD`: on API from
`by`/`ts`, who sent its code there, which a GPU pin since never replaces; on the GPU the newer of
`by`/`ts` and `gpu_by`/`gpu_ts`,
`(not in OpenCode's catalog now)` for an API model the catalog no longer lists) and the cap,
under a first line while Agents isn't Both (`cli._agents_line`: `Agents: GPU (every role runs on the
GPU; wb route all both goes back to each role's own below)`); `--json` the normalized document (its
`use` and `busy` too) plus `summary`, per role
`label`, `api_status` (`models.status` of its API model from the cached catalog, or not in it) and
`gpu_found`, and for the pane (#1023, `cli._route_pane`) `spend_today` (`{usd, by_provider}`, today's
`step` and `reconcile` rows of the day ledger, spend-api.jsonl; `usd` is `meter.today()`'s, the sum the
cap is checked against) and `providers` (`{<id>: {name, key,
why?}}` from the cached catalog: `key` `stored`, `env`, `oauth` or `none`, with the why for any but
stored). While busy is `api` the show adds `When the GPU is busy: a role with an API model starts on
it (wb route busy wait: queue instead)`.
The changes (each one exit 3 with one line when refused, writing nothing):
- `wb route <role> gpu|api` flips the selector; `api` with no API model kept: `<role> has no API
  model yet: wb route <role> <provider/model>`.
- `wb route <role> <provider/model>` sets the API model and goes to API; `--api <id>` sets it and
  leaves the selector; `--gpu <id|auto>` sets the GPU model (a fragment will do, as `--model`
  takes one: `models.resolve`; an API id refused); `--thinking on|off`; `--day-usd N`.
- `wb route all gpu|api|both` sets the Agents switch (`use`), no role's own selector; `api` with no
  role holding an API model: `no role has an API model yet`; a role with none then can't start, and
  the line says so. `wb route all --thinking on|off` sets every role's thinking.
- `wb route cap <usd>` sets the cap; `wb route cap off` clears it (#1023), refused while a role runs
  on API or the busy opt-in could send one there (`the day cap stays while <roles> can spend on API`).
- `wb route busy wait|api` sets the busy opt-in (*When the GPU is busy*).
- An API model is taken by its exact id only (`models.lookup`: fragments never match an API id,
  #1013), of kind `api`, with a key wb can hold (`key == "stored"`: /connect saved one, which the
  keyhole holds outside the lane), else refused with discovery's why (`cli._route_api`): `not in
  OpenCode's catalog: wb models lists the API models`, a GPU id (`… is a GPU model: wb route <role>
  --gpu <id> pins it`), no key (`… can't be a role's model: no key for <p>: run /connect in OpenCode
  and add it`), an OAuth, environment, config, signing or Ollama login key, or a keyless free model
  (`can't run: needs a key wb can hold outside the lane (…)`). A provider out of credit, rate-limited
  or unreachable is no refusal (`cli.HEALTH`): that passes. The kept API model of a role flipped to
  API is checked the same way.
- The first role to go to API with no cap: `routes.NO_CAP`, `no day cap yet: ask the user for one
  (suggest $5), then add --day-usd 5`.
- No change: `unchanged: <summary>`, nothing written.

**The change line** (`routes.change`, which never writes) is the dialog's text, one part per role
changed, joined by `; `: `trivial: GPU → API deepseek/deepseek-flash · your code goes to DeepSeek ·
thinking off · cap $5/day` (the provider's name from the catalog, `models.provider_name`, else its
id); back to the GPU, `trivial: API <id> → GPU · API kept: <id>`; an API model set while on the GPU,
`API model none → <id> (<provider>), used once it goes to API`; `GPU model the router's pick → <id>`;
`thinking off → on`; and `day cap none → $5/day` (unless a role's part says the new cap). The Agents
switch's part comes first (`routes._agents_part`): `Agents: Both → API (every role on its API model;
writing has no API model and can't start; your code goes to DeepSeek (trivial); cap $5/day)`, `Agents:
Both → GPU (every role on the GPU; each role's own choice is kept for Both)`, `Agents: GPU → Both (each
role as set: …; your code goes to …)`; it takes the yes going to API, or to Both when that puts a role
back on its API model (from GPU; from API it sends nothing new), never going to GPU. A role's own change the switch holds over adds `Agents is GPU, so this
applies once Agents is Both (wb route all both)`. The first role newly on API by any of these with no
cap: `NO_CAP`.
**Several roles in one change** (#1024, the first run's one question): `wb route <role> <choice>
<role> <choice>… [--day-usd N]` (`cli._route_pairs`, the `more` positionals; a choice is `gpu`, `api`
or an API id) is one write, one approval (its canonical `argv` holds every pair) and one echo. All or
nothing: every id is checked first, and any refusal (`<role> <id>: not in OpenCode's catalog: …`, a key
wb can't hold, a role named twice, a role with no choice, an OpenRouter id, which is set in a wb route
of its own, a kept OpenRouter model put on API with no backends, `cli._order_check`, or
`--gpu`/`--api`/`--thinking`/`--order`/`all`/`cap`/`busy` beside pairs) writes nothing.
`routes.change_pairs` applies each pair through `change` and makes one line: each role's move, then
where the code goes once (`your code goes to DeepSeek`; several providers as `_goes_to`), thinking
and the cap once: `trivial: GPU → API <a> · significant: GPU → API <b> · your code goes to DeepSeek ·
thinking off · cap $5/day`. `cli._route_apply` dry-runs or writes either kind of change. A kept API model a role newly runs on is checked again (`cli._route_api`). `Change.gated`: it touches the API side (a selector, an API
model, thinking on a role on API) or the cap; a GPU model pin alone, or thinking on a GPU role, isn't.
Applied (`routes.editing`: `store.edit_config`'s lock, a link kept, nothing written when the body
raises; the line computed again under the lock) it prints `changed: <line>`, and each changed role
gets its record (`routes.stamp`, `routes.STAMPS`): a gated change `said` (the line), `by` and `ts`;
one that needed no yes only `gpu_by` and `gpu_ts`, so Claude's pin on a role on API leaves the record
of the user's yes as it was (a pin once rewrote `by` to `claude`, the user's own yes then reading as
Claude's, 2026-11-23).

**The gate.** Under Claude Code (`CLAUDECODE=1`) a gated change applies only when
`routes.approved({argv, change})` holds: `$WB_HOME/route-ok.json` (`ROUTE_OK`), written by the mod's
dialog (*The consent gate*), fresh for `ROUTE_OK_SECS` 120, its `argv` the canonical command
(`cli._route_argv`: after `wb`, the API id as resolved, flags in one order, so an abbreviation or
`--flag=value` is the same command) and its `change` the line computed now (a file that moved
between the dialog and the call is caught). One call consumes it, matching or not
(`store.take_approval`, shared with `patches._approved`: renamed away, then read). Else exit 3:
`in Claude Code a role changes only through the workbench mod's dialog, and the user approved none
for this change, so nothing changed (is the mod loaded? wb doctor says). Or the user runs it in a
terminal of their own: wb route …`. `by`: `dialog` (the approval's `by` is `pane` for the pane's own
rows, #1023: `pane`), `terminal` (outside Claude Code); `gpu_by`: `terminal`, or `claude` (a change
under Claude Code that needs no dialog). A flag or an environment variable is no consent, and the
approval is one-shot, fresh, and bound to the exact command and line. The mod refuses the file tools
on both files and a Bash command naming `route-ok.json` with the state directory spelled out, but
that check is a speed bump, like the one on routes.json: a command that builds the path (a
variable, `os.environ['WB_HOME']` in Python) still writes an approval wb honours, recording `by:
dialog`, and Claude writing routes.json through Bash (`cat >`, `sed -i`, `python`) gets past them
too. What catches either is the `[workbench]` echo (*The mod, inside*): every change reaches the
conversation, and the user's own view of it, as a row.

**`wb start --model <API id>`** runs on the API model since #1016 (`cli.API_RUNS` on; off, it exits
3 with `API_NOT_YET` before any dialog, and `wb route --dry-run --start` refuses it the same way, so
the mod lets the call through unasked: a dialog once asked the user to send their code to a provider
for a start that then refused, 2026-11-23). It takes the same gate (`cli.start_gate`,
in `pick_model`): under Claude Code it needs `approved({start: {task, model}, change})`, the line
`start <task> on API <id> (not a role's choice) · your code goes to <provider> · thinking off`
(`cli.start_line`), else exit 3 with the same wording. A GPU `--model` and `wb
model sizes` are as before: router-driven, and Claude says the model in its reply. The bench driver
inherits `CLAUDECODE`, so a replay on an API model would meet this gate with no dialog; `wb bench
--model` refuses API models anyway until #1026 decides that path.

### Privacy for API roles
With a GPU model everything the agent reads stays on this machine; with an API model every file it reads and every
command's output is model input, sent to the provider (#1021, `workbench/privacy.py`). Setting a role is the user's
consent, given once in its dialog (#1008's decision 13); the rest says where the code goes wherever it matters, keeps a
local-only repo off API models, and lets nothing extra leave unasked. No warning past these: the owner's "just works".

**Where the code goes** (`privacy.where(m, order)`): `{provider, name, line, policy, free, backends}`, the start's
where-note's wording (`cli._api_notes`). The route dialog shows `wb route … --dry-run --json`'s `change` line
(`routes.change`), which names the provider (`your code goes to <name>`) and an OpenRouter role's backends, not the
policy page or the free mark (those reach the user in the note at the repo's first task to the provider, and in the
pane's model rows, `free_line`). `name` is OpenCode's provider name (`models.provider_name`;
`ollama.com` for an Ollama cloud model). `policy` is `PRIVACY_PAGES[provider]`, the provider's own data-policy page (URLs
only, never a claim about the policy; each fetched and its title read 2026-11-23, x.ai's through a headless browser),
`OLLAMA_PAGE` for a cloud model, else null and the line says `see <name>'s data policy`. The line is `your code goes to
<name>`, then `via OpenRouter to <backends>, no fallbacks (OpenRouter's account setting decides whether backends may keep
it)` for OpenRouter, then `free: the provider may keep or train on your code` for a free model (`Model.free`: OpenCode's
`opencode/*-free`, or every catalog price 0). Free models are never suggested (the skill); `wb doctor` says nothing of
the ones OpenCode lists (a line on every run for models wb never uses: wave 2's review), the line above says it where one is chosen.

**The where-note.** The first API task of each repo and provider prints `note: this task sends the repo, its spec,
CLAUDE.md files and your ~/.claude rules to <name> (data policy: <url>)`, the named copies and siblings added (`… and
local.cfg (--copy), ../shared (--sibling)`), and the line's OpenRouter and free parts (`cli._api_notes`,
`privacy.where_note`). "First" (`privacy.first_for`): no other API task (task.json `via: api`) in `tasks/` or the archive
(else `wb gc` would bring it back) has that repo and provider; not results.jsonl, whose rows before #1022 don't say a
task was on API, and every closed task's directory stays, archived. Task.json's
`order` (the role's, `cli._routed_keys`), the one the keyhole routes by, gives the backends. The token files the lane hides (each that holds a credential) are a note on
every API start (*What the agent knows*, `API_HIDDEN`).

**local_only.** commands.json `repos.<path>.local_only: true` (a bool, else `commands_conf` refuses it), read by the
nearest entry that states it (*Files and formats*), or `wb start --local-only` (task.json `local_only`) for one task.
`wb start` refuses an API model there before any model is picked (`cli._local_only_check`, its line from
`cli.local_only_refusal`: a `--model` resolving to `kind: api`, else the role's side as it runs, `routes.effective_via`
over `routes.read()`), so before `pick_model`'s `--model` gate, exit 3, never the GPU instead: `wb: ~/src/client is local-only (commands.json "repos"): the trivial role is
on API (deepseek/deepseek-flash). Choices: put trivial on the GPU (wb route trivial gpu, the user's yes), or the user
lifts local_only for this repo.` (with `--local-only`: `… or the user drops --local-only for this task`; for a `--model`:
`the task's model is API (<id>, --model). Choices: a GPU model, …`). The mod's dialog for `wb start --model` (#1014's)
asks before `wb start` runs, from `wb route --dry-run --start <its args>` (`cli._route_start`), which parses the start's
`--repo` (else the command's cwd) and `--local-only`, and exits 3 with `cli.local_only_refusal`'s line in
place of a dialog line, so the user is never asked to approve a start that would be refused. A linked worktree of a local-only repo is that repo
(the git-dir match). `wb next`, `wb feedback` and `wb resume` on an API task whose repo became local-only since it started
exit 3 (`cli._local_only_since`, `_local_only_now`): `this repo is local-only now; the task's model is API (<provider>):
wb takeover <t> or wb discard <t>` (the model is fixed per task); `wb resume --all` prints that line for such a task and
goes on with the rest, as for a worktree problem. `wb route all api` is global and reads no repo: the check is per start.
Only the user lifts one: the mod's tool.call hook refuses Claude's Edit, MultiEdit or Write of commands.json
(`$WB_COMMANDS`, else `~/.config/workbench/commands.json`, matched by real path: `/./` or a linked config directory is
the same file) whose new text drops a `"local_only": true` the old text had, or gives an entry a `local_only` other than
`true` it didn't have (a package's `false` lifts the root's: the nearest that states it decides) (`localOnlyRefusal`,
`localOnlyMarks`; a text that doesn't parse counts the marks), with `workbench: not changed: this edit drops
"local_only": true for <repo> in commands.json. Only the user lifts …` (or `sets "local_only" to something other than
true for <repo> …`). A shell's write isn't seen there, as with routes.json.

**OpenRouter** routes to the role's backends alone, enforced by the keyhole (*What the agent knows*, *OpenRouter, routed
by the keyhole*). `wb route <role> openrouter/<vendor>/<model>` without `--order <name>[,<name>]` is refused
(`privacy.order_refusal`: `… is served by many backends that differ in quantization and data policy: pick them with
--order <name>[,<name>] (openrouter.ai/<vendor>/<model>, Providers tab)`), as is any change that newly sends an
OpenRouter role there without them: its own side, the Agents switch, a pair, or the busy opt-in (`cli._order_check`),
so no dialog shows a backend nobody chose;
routes.json's role keeps `order` (`privacy.parse_order`; `routes.normalize` keeps it through every read and edit,
`routes.change` drops it when the role's API model is another provider's), the change line names the backends
(`backends <a>, <b>`, gated: the dialog shows them), and task.json `order` carries it to the keyhole.

## Bench: real tasks replayed

`wb bench` (`workbench/bench.py`) compares two models, or workbench before and after a change, on
the same work: a case is one step of a task Claude accepted, replayed as it was handed out and
graded by the tests it was accepted with (a synthetic suite every model passed was removed
2026-11-16). Shown only (`wb models`, `wb scout`, `wb bench report`); the router doesn't read it.

**A case** (`wb bench add <task> [--step N]`, default the last step) comes from a closed, accepted,
non-trial task. It keeps the task's setup and `--setup-timeout`, copies, siblings, grants (status
`grants_kept`, `cli._keep_grants`, since the rules go at the close, #250), package (`subdir`) and
`unload_for_tests`, and passes them to its base check, reference check, each replay and
`wb bench self` (`bench._inputs`; without the limit a long build stopped at 1800 s and was an `error` every
run, without the flag GPU tests ran beside the resident model, #735; an older case reads them from
its task, `bench._kept`, `bench.subdir`; task gone too: `error` rows). A replay runs from the
package (`--repo <checkout>/<subdir>`: from the root an `answer.txt` passed a check of
`app/answer.txt`, #425). The step's commit is found by its trailer
(`Workbench-Task: <task> (step N)`, or `Workbench-Task: <task>` for the last) from the recorded tip (`gitops.step_commit`). `wb
accept` keeps base..tip in `tasks/<task>/steps.bundle` (`gitops.bundle_steps`, `STEPS_BUNDLE`, made
in a `--shared` scratch clone and named with `--git-dir`, #887), which `bench add` unbundles when
git's gc pruned the steps (`gitops.restore_steps`: objects only, no ref, no FETCH_HEAD): a squash
accept left them in no branch (#649). The bundle lives as long as the task, so a case removed with
`wb bench rm` can be remade (#940). Before #799 accept kept `refs/workbench/accepted/<task>`
(`gitops.ACCEPTED_REF`) in the user's repo; it falls back to that only when the bundle can't be
made, and `wb gc` moves such refs into the bundle. A case holds:
- `base.bundle`: branch `main` at one parentless commit with the base's tree and message
  (`commit-tree`, neutral identity), so nothing after the base (the answer) is in it and the case
  doesn't carry the repo's history (~800 MB a case, #740; a shallow clone bundles too, #478);
  case.json `base` stays the original. It
  has no LFS objects or submodules, so every checkout takes them from the case's repo (#509):
  `_clone_base` shares `filter.lfs.*` and the objects (`gitops.share_lfs`), add's checkouts use
  `gitops.fill_submodules`, and replays make each submodule a repo at its commit with borrowed
  objects (`_submodule_repos`). What the repo can't supply (or a filter it can't run, #932) refuses
  the add and makes a replay an `error`, naming it;
- `spec.md`: the step's prompt (`store.round_prompt`, which falls back to `instructions.md` for a
  fresh session, #503), cut at the protocol's fold (`FOLD`), and the step's acceptance command
  (task.json `test_cmd` keeps only the latest): status `step_tests`, else the protocol's
  `- The acceptance command is` line (`TEST_LINE`, read across lines to the sentence's end, #941);
- the hidden checks: `--hidden` paths, by default the step's changed files that
  `verify.looks_like_test` matches (listed NUL-separated, `gitops.name_status`, #942), copied into
  `hidden/` with their Git mode (#346) and overlaid on the base, the reference and every replay
  (`overlay`); and `--check`, a command that must pass too;
- the grants and the original's model, rounds, reviews and minutes.

Refused: not accepted or a trial, repo or commit gone, no acceptance command, no hidden files nor
`--check`, a setup that fails, hidden checks that already pass on the base (within `BASE_CHECK_SECS`
= 120 s, past which `base_check` says not checked), or checks that fail on the step's own commit.
Both run under `bench/` (in /tmp the git shim would allow writes), in the original task's lane and
environment (`runner.run_in_lane` on `_At`, #81), so a suite that commits or writes outside the lane
can never be a case. A case is worth keeping when the spec pinned the names the hidden tests call.

**A run** (`wb bench run`) writes `bench/runs/<run>/plan.json` (cases × `--model`, default every
runnable GPU model taking the size: `kind == "gpu"` filtered again, so no discovery change can put an
API or Ollama cloud model on a replay unasked, #1026; an API `--model` is refused, #1013) and detaches a driver (`python -m workbench.bench <run>`, no Claude
Code session in its environment). `bench/driver.json` holds its pid, start time and host; it counts
only while that process runs `-m workbench.bench` (`models.runs`), and on a shared state directory
another host's driver lives while `bench/driver.lock` (touched every `runner.BEAT_SECS`) is younger
than `cli.BEAT_DEAD`: `wb bench stop` refuses it ("runs on <host>…: stop it there", #876). For each
pair and `--repeat`, one at a time, it waits while any real task is active or queued; clones the
bundle into `bench/runs/<run>/<task>/repo` and starts a task through `cli.parser()`/`cmd_start`
(`--trial`, task.json `bench` = `{case, run, label}`, the case's size, test and grants, `--ask-budget
0`, `--checkin 0`, `--queue`, `--no-claude-md`, the run's `--skill`; otherwise a real task); waits
for review, stuck or failed (a permission request gets `REFUSED`); at review saves `diff.patch`,
copies the hidden files over the worktree (replacing a planted link, never writing through it: the
driver is outside the lane) and runs the acceptance command and `--check` in the lane
(`grade.txt`); then discards task and clone and appends to `bench/runs.jsonl`. Passed is both exiting
0; stuck or failed is failed, unless status `infra` says the machine failed it: an `error` row,
graded neither way and replayed next run (#508).

A replay stays out of real work: task.json `bench` silences `Task.notify` and `results.record`, hides
it from `wb status` (unless `--all` or named) and the mod; `--trial` keeps its lessons out; its
speeds do go to speed.json. Real work that would wait for the GPU behind a replay (or `wb optimize`)
preempts it (`cli._preempt_bench`, phase stuck, `PREEMPTED`; never another host's, #895), and the
driver replays it later. A driver killed without warning closes nothing (#511), so `wb gc` and the
next `wb bench run` call `bench.sweep`: each unclosed replay whose driver is gone is discarded and
its clones removed (never another host's whose runner beats, #876). A rerun skips each (case, model,
label, repeat) already passed or failed (`done_keys`); rows carry `wb_commit` and `label`, so a
workbench branch run with `--label` compares in `wb bench report --label`.

**Claude writing it** (`wb bench self <case> --yes [--turns N]`, `bench.self_run`, #118) measures
the other side of the handoff's cost: `claude -p --output-format json --max-turns N` (`SELF_TURNS`
40) in a scratch checkout of the base, from its package (`subdir`), the spec plus `SELF_PROMPT`,
with `--permission-mode acceptEdits` and only the acceptance command allowed (`--allowedTools
Bash(<cmd>:*)`, one per simple command), graded as a replay. `usage` becomes spend's shape
(`_self_usage`, priced by `spend.weighted`; `_self_result` takes Claude Code's event list or the
bare result; `calls` counts distinct assistant message ids, since `num_turns` counts parallel tool
calls). The row has `arm: "claude"`, `model`/`label` `"claude"`, `claude`, `turns`, `claude_model`;
`wb bench report` lists it under "Claude writing it" with the step's handoff cost (`handoff_cost`:
the results row's `steps[step-1].claude`, *Each step's share*) and the ratio, or "not comparable"
for a row with only a whole multi-step task's cost (before #558). Refused without `--yes`: it spends
Claude tokens, and nothing runs it on its own.

## Optimize: measuring this machine, when asked

`wb optimize` (`workbench/optimize.py`) is the one place workbench loads a model only to learn about
the machine: per Ollama model, the largest window that keeps model and context all in VRAM. Why
(2026-11-18): a 64K window spilling a tenth of a 27B model wrote ~17-21 tok/s against ~37 at 32K on
the GPU, yet spared review rounds; where the line falls depends on card, model and server, so it is
measured and `advice` gives both facts (Claude and the user choose; `wb start` prints it as a
`note:`; applying it changes their OpenCode config, so ask first).

A run takes a minute or two, so it runs unasked only where a short wait is expected, and only when
`due`: never measured; the `fingerprint` changed (each GPU's name and memory, each answering remote
server's `/api/tags` names and sizes; `changed` compares only servers both know, `remembered` keeps
a sleeping one); a new model (not one `skipped` before its `retry_after`); a `stale` fit (the entry's
window or tags `digest` differs from the row's, #709); or the last run was stopped. It is checked at
the end of `wb setup` and once per Claude Code session after its first quiet turn (the mod's
`measureIfDue`, `quietForJobs`, #541, a toast, no wake), through `wb optimize start --if-due`, which
measures only the new or stale models (`--all` measures every one). `--bench` and anything heavier
wait for the user to ask: an unannounced long run could be cut short and alarm people (the user,
2026-11-18).

`probe` loads at `options.num_ctx` via `/api/generate` and reads `/api/ps` (`size_vram` against
`size`), then unloads. A load is whole only when `/api/ps` `context_length` (`served`) reaches the
window asked; a clamping server records `clamped` and `advice` says so (#338). `fit` tries the served
window (`limit.context`) first, then searches down to `models.MIN_WINDOW` or up to twice it (never
past the trained length nor `MAX_WINDOW` 128K, whose cache could fill RAM) in multiples of `STEP`
4K, about six loads (`loads(m)`). Nothing in VRAM records `cpu: true`, which `advice` ignores. Not
measured: `keep_loaded` models (by id, name or alias, #254), this machine's when `no_gpu_here`
(#809), and an API or Ollama cloud model, whatever discovery lists (`candidates` keeps `kind ==
"gpu"`, not `cloud`: a probe or a `--bench` replay would send code off the machine, #1026). Remote Ollama models (`wb remote`, #115) are measured on their own card under both leases
(`GpuLease(None, HOLDER, wait=False, on=m)`), but skipped, due again, when a task holds that server,
when it has another model loaded (`busy`), or when another client is using the model (not
`box_mine`, or a request within `INFLIGHT_WATCH`, #944): `probe` unloads what it measures.

`estimate`: loads × the last run's `secs_per_load`, else the median of `models.single_loads`, else
`LOAD_GUESS` 20 s. `start` detaches `python -m workbench.optimize` (`optimize.log`, pid in
`optimize.json`), refuses while a task holds the GPU, holds it alone (`HOLDER` "wb optimize") and
`free_gpu`s only before its first local probe (#583). A model it can't measure goes in `skipped`
(`_skip`: `busy`, `failed` for a `RuntimeError`, `unreachable` for an `OSError`) with a
`retry_after` from `RETRY_SECS` (6 h, 1 day, 7 days), and the run carries on (#583: one HTTP 500
stopped the run, and a skipped model was "new" every session); `--all` retries them, and a
measured row clears its entry. Real work stops
it: `cli._preempt_bench` sends SIGTERM and waits up to `OPTIMIZE_EXIT` (30 s) before reading the
GPU's holders (#161); fits so far stay. `--bench` then starts `wb bench run` on every model,
labelled `optimize`. `report` (default) gives a line per model (`wb doctor` repeats those with
something to change); `stop` acts only if `optimize.running`. `models.runs(pid, *words)` is the one
identity check behind every stop (`cli.runner_alive` too): the words in `/proc/<pid>/cmdline`, else
`ps -ww -o command=` (macOS trusted any pid, #82); with neither, the pid is trusted.

`apply <id>…` (after the user's yes) adds the window as an entry beside the model, never in its
place: an Ollama model `entry_name` (the whole name and tag kept, the window as suffix:
`family:32b-q4_K_M-64k`, `m:64k`; an existing name refused) made by `/api/create` `from` with
`num_ctx`, and OpenCode's entry copied with `limit.context` the window and the caps recomputed by
`budget` (output kept while at most half the window and leaving room for a handover before the
compaction, `runner.handover_warning`, #595, else a quarter; input the rest; an entry no request
could fit refused, #332; an alias's `id` dropped, since it names the old model). The variant gets
the source's models.json keys and `variant_of` the first source's id (`replaces` for an apply on a
variant), and its speed until measured (`router.speed`, #906). The router scores the lineage as one
record (`router.lineage`) and prefers the variant (`router.replaced`, chains followed all the way:
one level sent it back to the spilling source, #723); sources stay for `--model`, retiring them is
the user's call. The source's row gets `applied` (and `optimize.applied` also knows the variant by
apply's name for the window), so `advice` goes quiet (#584). The config is checked first
(`models.config_target`: one file, no comments, writable, before anything is made, #266); a variant
made but not yet registered is kept in optimize.json `made` (`<provider>/<name>` → the source's id),
so a retry registers it; a same-named model not there is the user's.

## The skill library

The agents learn nothing between tasks; Claude does. When review teaches something that will come up
again, Claude writes a skill. Code: `workbench/skills.py`, `workbench/scan.py` (security scan),
`workbench/lessons.py`, `cli.cmd_skill*`, `cli.cmd_lessons`.

**Storage.** Its own git repo, `$WB_SKILLS` (`~/.local/share/workbench/skills`; `git init -q` then
`git symbolic-ref HEAD refs/heads/master`, since git before 2.28 has no `init -b`, #527). A skill is
`<lib>/<name>/SKILL.md` plus files; retired, `<lib>/retired/<name>/`; only `<lib>/*/SKILL.md` is
live (`available()`, `resolve()`). Commits use the user's identity (else
`workbench <workbench@localhost>`), unsigned and hookless (`commit.gpgsign=false`, `core.hooksPath` the null
device, `--no-verify`): it is workbench's own record, and a signing prompt or global hook failed
`wb skill add` (2026-11-16). Messages: `<name>: <message>` (+ `Source: <source>`), `<name>: retired:
<message>`, `<name>: restored`; `rev`, `revs`, `history` read them. Never `~/.claude/skills`; one
project's knowledge goes in its `.claude/skills`.

**Signing the user's commits** (`gitops`; only described here). Commits in the user's repos
(`gitops.commit_all`, and the merge of `wb accept --merge`, `gitops.merge`) sign as their git says,
with no terminal or display (`NO_TERMINAL`), detached from the controlling terminal
(`gitops._run(detach=True)`: on WSL2 ssh-keygen prompted on `/dev/tty`, #933), within `COMMIT_SECS`
(90); a refusal says how to unlock (`UNLOCK`, `UNLOCK_HOW`) or to use `--no-sign` (#420: a pinentry
hung accept with a merge staged). `wb start` tries a dry `commit-tree -S` (`sign_problem`,
`SIGN_SECS` 10) so a locked key shows before the work (#374), except for a signer that asks each
time (`gitops.presence_signer`: an `sk-` SSH key, a `gpg.ssh.program` of its own, an agent in
`ASKING_AGENTS`, an OpenPGP card key), which gets `touch_line` instead and is named in refusals
(`_unlock`, `unlock_how`, #528). `--no-sign` (task.json `sign: false`; accept, next) commits
unsigned; the closing record's `sign` keeps a retry's merge like the first. They run no hooks
either (`gitops.NO_HOOKS`, *No git hooks from the worktree*).

**Checked on the way in** (`check`; `add` refuses the denied names and a source inside the library).
`parse` reads front matter between `---` lines (top-level `key: value`, indented lines folded into
the previous key, a block indicator `|`/`>` empty, quotes stripped, a leading BOM skipped, #479;
`name`, `description`, `license`). Refused: a name not matching `^[a-z0-9]+(-[a-z0-9]+)*$` or
differing from the front matter's, `workbench`, the user's personal skills' names
(`context.denied_skills()`: denies are by name) or `retired`; no description or one over 300
characters (all an agent sees before loading); over 8000 bytes; a body path under `references/` that
is missing, or under `scripts/`, `templates/`, `assets/` when the skill has that directory but not
the file (a single `--from` file is checked against the library's copy, #907); a source inside the
library. Warned: a body over 120 lines; a home path or `~/`; a description without
when/for/before/after/if; clutter or more than 10 files; a body like an incident log (3+ issue
numbers or dates: a skill states the rule); a `scripts/`… path with no such directory (likely the
project's, #738).

**Scanned on the way in** (`scan.py`, adapted from Hermes Agent's `tools/skills_guard.py` at
081e8d9, MIT notice kept in its docstring: its critical and high rules, plus Unicode Cf and tag
characters). 43 rules (`PATTERNS`, 27 critical, 16 high): exfiltration (secret variables sent by
curl/wget, `cat` of credential files, staging in /tmp then sending; `~/.ssh` and the like,
`printenv`, paste and webhook sites), prompt injection (each within one line, at most four words
apart: `S`, `W`, #455), destructive commands (rm of `~`, `$HOME` or `/` itself, `> /etc/`, mkfs,
`dd of=/dev/`, sudoers), remote code (pipe to a shell, reverse shells, listeners, tunnels), obfuscation
(base64 or echo piped to an interpreter that runs stdin, `chr()` strings), credentials (PEM keys,
`ghp_`, `github_pat_`, `glpat-`, `sk-ant-`, `sk-`, `AKIA`), and writes to AGENTS.md, CLAUDE.md or
`.claude/settings*`. The rules are drawn to pass ordinary advice (`cat .env.example`, a heredoc
writing `.env`, `rm -rf ~/build`, "Never `cat .env`"; `> /etc/` only high in a Dockerfile or on a
`RUN` line): the exceptions are `_softened`, and `REDIRECTS` match with HTML tags blanked (`TAG`).
Each finding names its rule (`[pipe_to_shell]`). Files: what is scanned is what `skills.add` copies,
following links; a symlink out of the skill or into its `.git`, `symlink_dangling`, `symlink_loop`
(#604) and binary extensions are critical, a link inside is scanned as its target under its own
name; archives, NUL bytes, invalid UTF-8 high; media skipped only when it looks binary (a text
"image" is scanned, 2026-11-16); invisible characters critical with a bidi or tag character, else
high; 200+ blanks then code critical outside prose (a possessive quantifier: Python 3.11+).
`verdict`: dangerous / caution / clean; an update counts only `new_findings` (same file, rule and
stripped line). `content_hash` is display only. A safe critical finding (a rustup `curl … | sh`
line) passes with `--reviewed <rule>` (that rule's critical findings become warnings; a rule with no
finding is a warning), recorded in the commit under "Reviewed (the scan's critical findings, judged
safe):", but never `scan.UNREVIEWABLE` (links out, binaries, invisible characters, padded code, NUL
bytes).

**The flows.** `wb skill add <name> --from <file|dir> -m "<what review taught>" [--source]
[--lesson ID…] [--reviewed RULE…]` checks, scans, copies (`skills._put`: through a scratch `.adding-<name>-…`
renamed into place, so a failed copy leaves the old version, #604), commits only that skill's paths
when something changed, and undoes the copy if the commit fails (`skills._undo_put`). Add, patch,
retire and restore hold `locks/skills.lock` (`skills._library_lock`, reentrant: two sessions once
committed under one message, #822); a failure is one `not added: …` line (`cli._skill_failure`).
`--lesson` closes those lessons as `skill:<name>`; all from one repo, `cli._one_project` warns it
may belong there. `patch --old --new` (text occurring exactly once) re-adds a patched draft: patch
the passage that misled rather than appending. `retire` is `git mv <name> retired/<name>` (an older
retired copy removed first); `restore` the reverse, refused while a live skill has the name. `scan`
exits 0 whatever the verdict. `import <git-url|owner/repo|path>` (`skills.fetch`) shallow-clones
into `$WB_HOME/imports/<slug>` (`GIT_TERMINAL_PROMPT=0`, 300 s), prints the licence (`license_of`),
hash, scan and the `wb skill add … --source "<url>@<rev12> <subdir>, <license>"` to run; a dangerous
one is refused and its clone deleted. Nothing enters by itself: Claude rewrites what fits and adds
it. `hand`/`take <task> <name…>` change a task's skills from its next run (hand validates names,
take doesn't); `wb start --skill` hands at start, `wb next --skill` replaces the set
(`cli._skill_names` dies on an unknown name before anything is created).

**Handed to a task.** Status `skills` (the set), `skills_ever` (results' `skills_offered`),
`skills_rev` (each one's rev when handed), `skills_told`. `context.layer` lists each skill's library
directory in the config layer's `skills`, one directory each, and allows it for the file tools
(`external_rules`). `runner.skills_note` prepends to the round's prompt argument (not prompt.md) the
set at a session's start ("Claude picked these skills…"), then only changes, logging a `skills`
event; a retired skill drops out silently. `skills.loaded(task)` reads the `skill` tool calls, so a
repo's own `.claude/skills` count too. How OpenCode 2.3 lists them (RealOpenCodeSkillFiles): a `skills` entry naming a skill's directory lists that skill, a parent lists every skill under it, one naming a SKILL.md lists nothing (hence one directory each); a `disable-model-invocation` skill isn't listed; the `skill` tool takes `id` and returns the body, its base directory and its files.

**Judged on outcomes** (`skills.usage`, `record_line`, `advice`) over `store.results()` rows that
`tuning.judged` keeps (not a `--spec-gap` discard): offered, loaded, and for loads the accepted
share (`tuning.credit`, #647, #722), first-try (`tuning.agent_landed`), review rounds, against the
same repos' tasks without it, and since its last edit (`skills_rev`). `advice`, first match wins:
never offered; offered 3+ times, never loaded (the description is wrong); 2+ tasks since its last
edit, none accepted; 3+ loads under 50% accepted (patch it); 3+ loads and 3+ without, no better; not
offered in 60 days. `wb skill list` shows record and advice (and a personal skill of the same
name, #515); `wb scout` lists the flagged ones.

**Suggested for a spec** (`skills.suggest`, `wb start` without `--skill`): words
`[a-z][a-z0-9]{3,}` less `STOP`, crudely stemmed, against the skill's name (counted twice) and its
trigger (the description to its first colon); suggested at `SUGGEST_MIN` = 3 shared words, top 3.
One loaded only in other repos says where and on what stack (`cli._elsewhere`), so one project's
knowledge doesn't spread unnoticed.

### Lessons

`workbench/lessons.py` gathers what each task taught into `$WB_HOME/lessons.jsonl`, for Claude to
turn into skills. `wb feedback`, `next`, `resume`, `accept` and `discard` call `harvest` and end
with one `nudge` line: `lessons: N from this task[, M seen in other tasks too] (wb lessons: write or
patch a skill now, while it's fresh)`. A `trial` task gathers nothing; `wb trial` closes its open
lessons. Kinds: `feedback` (each numbered item, `N.` or `N)`, of a round prompt, or
`instructions.md` after a handover, opening `FEEDBACK_HEAD`, "Review feedback from the senior
reviewer.", ≤600 characters); `agent` (`Lesson: …` lines in `summary.md`, which PROTOCOL asks for;
skipped for a discarded task, whose open ones close as dropped: dead ends, not a recipe); `stopped`
("the guard stopped the agent: …", not a stop with `by`); `question` (each answered ask, `wb allow`
included); `spec` ("the spec missed: …", from `status.spec_gaps`, for Claude's spec checklist);
`skill` (at accept/discard with agent-caused review rounds: "loaded X, yet needed N review round(s):
does X say the right thing?").

A row is `{ts, task, repo, skills, id, kind, round, text}`, unique on (task, kind, round, text); `id`
is the shortest free prefix of their sha1, from `ID_LEN` (6) characters, two more while taken (six
alone collided, 2026-11-16). A close row is `{close: id, ts, how: "skill:<name>" | "dropped", why}`.
`survey`: a lesson recurs when one in another task (kind `skill` excepted) shares 3+ words with
Jaccard ≥ 0.35; a closed match means it came back after it was handled. Pairs are found by prefix
filtering (`lessons._pairs`, `_prefix`, exact for that rule) and `nudge` surveys only the task's new
lessons (`survey(only=)`): all against all took 30 s at 5,000 lessons (#556). Stops compare on their
words less `lessons.STOP_WORDS`, the guards' fixed wording (#733). Each lesson is attributed to its
first loaded skill still in the library, else the skill sharing the most words (3+). `wb lessons`
lists the open ones, recurring first, at most 12 (`--all`, `--task`), each naming up to
`AGAIN_SHOWN` (5) other tasks; `wb lessons close <id> -m why`; `wb skill add|patch --lesson <id>`
closes it as written into a skill. The weekly check counts what's waiting.

## Images

Claude makes images for a project with an image model on the user's machine, as art director: it
writes the prompt, `wb image` makes four candidates, Claude looks at each and keeps one, rewrites, or
reuses a seed. The keeper is committed with its `.json` sidecar (prompt, seed, size, engine) so it
can be made again. Agents can't make images; they ask Claude. Setup is optional and never unasked:
Claude proposes a tool and a model whose licence allows the user's use, with the download size. The
development machine (Arch, a 16 GB AMD card) runs stable-diffusion.cpp on Vulkan (`-DSD_CUDA=ON` on
NVIDIA, `-DSD_METAL=ON` on a Mac) with Z-Image-Turbo (Apache-2.0, 8 steps, ~11 GB VRAM at Q8, about
11 GB of downloads):

```sh
sudo pacman -S --needed vulkan-headers spirv-headers shaderc cmake   # or the distro's packages
git clone --recursive https://github.com/leejet/stable-diffusion.cpp ~/.local/opt/stable-diffusion.cpp
cd ~/.local/opt/stable-diffusion.cpp && cmake -S . -B build -DSD_VULKAN=ON -DCMAKE_BUILD_TYPE=Release && cmake --build build -j
ln -s "$PWD/build/bin/sd-cli" ~/.local/bin/sd-cli && mkdir -p ~/models/image && cd ~/models/image
curl -LO https://huggingface.co/leejet/Z-Image-Turbo-GGUF/resolve/main/z_image_turbo-Q8_0.gguf
curl -LO https://huggingface.co/unsloth/Qwen3-4B-Instruct-2507-GGUF/resolve/main/Qwen3-4B-Instruct-2507-Q8_0.gguf
curl -LO https://huggingface.co/Comfy-Org/z_image_turbo/resolve/main/split_files/vae/ae.safetensors
```

`~/.config/workbench/images.json` (`$WB_IMAGES`) maps engine names to entries, the first the default
(unless `"default"` names another: *The default engines*):

```json
{
  "z-image-turbo": {
    "command": "sd-cli --diffusion-model ~/models/image/z_image_turbo-Q8_0.gguf --vae ~/models/image/ae.safetensors --llm ~/models/image/Qwen3-4B-Instruct-2507-Q8_0.gguf -p {prompt} -o {out} -W {width} -H {height} --seed {seed} --cfg-scale 1.0 --steps 8",
    "notes": "licence: Apache-2.0; text to image only; 8 steps; ~11 GB VRAM"
  },
  "server": { "url": "http://127.0.0.1:30000/v1/images/generations", "model": "…" }
}
```

Prefer safetensors or GGUF from the publisher or a known quantizer, and tools that run no code
shipped with a model (ComfyUI's custom nodes do).

**How it works** (`workbench/images.py`, `cli.cmd_image`; shared with audio: `workbench/media.py`).
- Keys: `command` or `url` (with `api`, `steps`, `workflow`), `ref_arg`, `model`, `label`, `notes`
  (shown to Claude: the licence goes there), `timeout` (1800 s), `unload`, `unload_body`, `key`,
  `gpu`, `devices`, `beside`.
- A command engine (`_argv`, `media.fill`): `shlex.split`, `~` per token, then `{prompt}`, `{out}`,
  `{width}`, `{height}`, `{seed}` filled token by token in one pass, never through a shell (a prompt
  saying `{seed}` arrives as written; a whole float without its `.0`, #596); `--ref` images as
  `[ref_arg, path]` (refs need both `command` and `ref_arg`). stdin is /dev/null, output to
  `<out_dir>/engine.log`; a nonzero exit or no file fails with its tail.
- A url engine (`media.post`) speaks one of `images.APIS`: `openai` (`{prompt, n: 1, size,
  response_format: "b64_json", seed}`, `data[0].b64_json` or `.url`), `a1111` (A1111, Forge,
  SD.Next; implied by `/sdapi/`), or `comfyui` (implied by `workflow`: a graph saved with ComfyUI's
  Export (API), not Save: every value an object with `class_type`; a path from the engine file's
  directory or `~`; `images._fill` makes a value that is only `{seed}`, `{width}` or `{height}` a
  number; the first SaveImage output is the image, else a PreviewImage's; it polls `/history` every
  `COMFY_POLL`, and stopped early deletes and interrupts its prompt so it doesn't draw on after the
  lease). Each once got the OpenAI body while listed ready (#753). `key` (`{env:}`, `{file:}`,
  `media.auth`) goes as a Bearer token to the engine's own server only. Errors quote the status and
  the body's first 300 characters (a 401/403 without a key says it wants one); a reply cut off is
  one line (`media.cut_off`, #925, #597).
- `unload` (+ `unload_body`) is POSTed when the batch ends, inside the lease, failed or not
  (`media.unload`; never called before #749, and a server kept 6-12 GB beside the next task). A
  comfyui entry defaults to `/free` with `{"unload_models": true, "free_memory": true}` (`_defaults`:
  ComfyUI keeps every model it loaded; `"unload": ""` opts out). With none, the listing
  says `(no unload URL)`: the server keeps its GPU memory while up.
- Where it runs (`media.turns`, `media.on_gpu`): `"gpu": false`, or by default a url on another
  host, is off this GPU: no lease, nothing freed, it runs beside tasks (#587). An API task takes no
  lease (#1015), so a job on this GPU runs beside it too (not beside its `--unload-for-tests`
  tests, which hold this GPU alone). A url on a remote box
  the tasks use (`media.boxes`) takes that box's lease (`GpuLease(None, "wb image", on=…)`) and
  unloads its Ollama models (not `keep_loaded`), another client's only once `BOX_QUIET`
  (#750, #944); pools are taken in one order, this machine's first, then by pool name (#945: two jobs
  deadlocked). WSL2's Windows host is this GPU (`same_gpu`). Anything else holds this GPU for the
  batch (`media.gpu`): a holder refuses it unless `--wait` (which queues, and makes `wb start`
  refuse without `--queue`), then `free_gpu(keep=media.serving(engine))` (not the engine's own
  server's models, #587) and `media.settle` (up to 90 s for what was freed, `models.freed`, #467;
  skipped under `WB_FAKE_MODELS`). `devices` (as `CUDA_VISIBLE_DEVICES` names cards,
  `models.named_cards`, in nvidia-smi's order, so give the engine `CUDA_DEVICE_ORDER=PCI_BUS_ID`;
  AMD's as `ROCR_VISIBLE_DEVICES`) or `beside` (a model id: its Ollama's cards,
  `models.server_devices`) pin it (`media.cards`, #752): it waits only for tasks on its card and
  unloads only there; unknown (no card named, not a local Ollama, `gpu_gb` set by hand) means every
  card.
- **The default engines** (#1047): `images.json`'s top-level `"default": "<engine name>"` and
  `audio.json`'s `"default_speech"`, `"default_sfx"`, `"default_music"` name the engine used without
  `--engine` (`images.default_name`, `audio.default_of`; read by `media.named_default`). Strings, so
  `media.load` skips them and an older wb reads the file unchanged. A name that isn't an engine (of
  that kind; the listener makes nothing) is ignored: the first entry, as before. `--engine` always
  wins. `wb image --default NAME` / `wb audio --default NAME` (`cli._media_default`,
  `images.set_default`, `audio.set_default`: the engine's own kind picks the key) write it through
  `media.set_default` (`store.edit_config`: locked, a link kept, every other key and entry in its
  order, a key already there in its place); refused with the engines for an unknown name, for the
  listener, for an engine named like the key, and with a prompt (that is `--engine`'s). No dialog:
  nothing spends, nothing leaves the machine. `--list` marks a default the file names (`sd
  (default) (off the GPU): …`, `s2 (speech, CPU, default): …`); without one it lists as before. `wb models --json`'s `media`
  (`cli._media_json`, `images.choices`, `audio.choices`) feeds the pane's rows (*The roles in
  Settings*).
- Ready (`ready`; `wb image --list`, `wb scout`): `media.problem` finds no bad type or URL (else
  "not usable: <why>", refused before the lease, #596, #748); the workflow is API-format JSON with
  `{prompt}`; the command is on PATH or a file, or the url answers a GET within 2 s, 401/403 saying
  it wants or refused the key (#597). `wb scout` (`images.servers_here`, `images.PORTS`) probes
  ComfyUI on 8188 and 8000 and A1111 on 7860 (on `SHARED_PORTS` only ComfyUI's own reply counts; on
  WSL2, when nothing answers here, the Windows host, #923; none under `WB_FAKE_MODELS`) and names a
  server no engine uses, with its entry (#753).
- `generate`: seeds seed…seed+n−1 (default `time() % 1_000_000`). Each candidate is made in a fresh
  `.wb-image-*` in the (absolute, #751) output directory and refused unless it starts like a PNG,
  JPEG or WebP (`images.kind`, `media.starts`: an HTML error page was published, #744) and is whole
  (chunks and sizes walked, #946). It is published with its sidecar `{prompt, seed, width, height,
  engine, refs, secs, ts, format}` (sizes measured) as the first free `<slug>-<seed>.png` (the
  slug: the prompt's first six words, `media.slug`),
  `-2.png`… (`media.free`; `.jpg`/`.webp` kept), claimed by creating the sidecar with `O_EXCL`
  (#267, #591): an earlier candidate is never replaced. `--out` defaults to
  `$WB_HOME/images/<YYYYmmdd-HHMMSS>/`. Tool output is decoded with bad bytes replaced (#743).

## Audio

Claude makes sounds (a voice line, a sound effect, a music loop) as it makes images (*Images*), but
**Claude can't hear**, so each candidate gets checks it can read: length against the one asked,
peak and clipping, lead, tail and gaps, loudness (LUFS, with ffmpeg), a spectrogram `.png` to look
at, and for speech a **listener** whose transcript is scored against the script. Taste is the user's:
Claude shortlists and the user listens, by the web chat when on (*Listening by ear*). The weekly
model check re-checks the audio picks and licences (#398). Setup is optional, never unasked (the
skill's `reference/audio.md`). The recommended runtime is
[audio.cpp](https://github.com/0xShug0/audio.cpp) (Apache-2.0, ggml, no Python at inference, Vulkan,
CUDA, HIP, Metal or CPU, one CLI for speech, effects, music and recognition). Models are picked by
licence first (OmniVoice since 2026-07, Breeze TTS 2, YuE2 and CrisperWhisper aren't free for
commercial use; Stable Audio 3 only below a revenue line), each checked against its licence text:

| Kind | Model | Licence | Note |
|---|---|---|---|
| speech | Kokoro-82M | Apache-2.0 | real time on the CPU, fixed voices |
| speech | Qwen3-TTS 1.7B VoiceDesign (0.6B Base to clone) | Apache-2.0 | a voice from a description |
| sfx | MOSS-SoundEffect v2 | Apache-2.0 | up to 30 s |
| music | ACE-Step 1.5 | MIT | lyrics optional; the 2B turbo fits 6 GB with offload |
| listen | Parakeet TDT 0.6B v3, or Qwen3-ASR 0.6B | CC-BY-4.0, Apache-2.0 | only checks transcripts |

```sh
sudo pacman -S --needed espeak-ng   # Kokoro's phonemizer (GPL-3.0)
git clone --depth 1 --branch v0.9.5 --recurse-submodules https://github.com/0xShug0/audio.cpp ~/.local/opt/audio.cpp
cd ~/.local/opt/audio.cpp && cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DENGINE_ENABLE_VULKAN=ON   # CUDA: -DENGINE_ENABLE_CUDA=ON
cmake --build build -j --target audiocpp_cli && ln -s "$PWD/build/bin/audiocpp_cli" ~/.local/bin/
mkdir -p ~/models/audio && cd ~/models/audio && B=https://huggingface.co/audio-cpp/audio.cpp-gguf/resolve/main
for f in Kokoro-82M-GGUF/kokoro-82m-q8_0.gguf Parakeet-TDT-0.6B-v3-GGUF/parakeet-tdt-0.6b-v3-q8_0.gguf \
         MOSS-SoundEffect-v2-GGUF/moss-soundeffect-v2-q8_0.gguf ACE-Step1.5-GGUF/turbo/ace-step-1.5-turbo-q8_0.gguf
do mkdir -p "$(dirname $f)" && curl -fL -C - -o "$f" "$B/$f"; done   # 11 GB
```

`~/.config/workbench/audio.json` (`$WB_AUDIO`), audio.cpp 0.9.5's flags (some families take
`--seed`, others `--request-option seed=`; a Kokoro voice needs its `--language`, `af_`/`am_`
`en-us`, `bf_`/`bm_` `en-gb`, else "voice/language mismatch": one engine per language):

```json
{
  "kokoro": {"kind": "speech", "gpu": false, "voices": ["af_heart", "am_puck"],
             "command": "audiocpp_cli --task tts --family kokoro_tts --model ~/models/audio/Kokoro-82M-GGUF/kokoro-82m-q8_0.gguf --backend cpu --language en-us --voice-id {voice} --seed {seed} --text {text} --out {out}",
             "notes": "licence: Apache-2.0; English best; no cloning"},
  "moss-sfx": {"kind": "sfx",
               "command": "audiocpp_cli --task gen --family moss_soundeffect --model ~/models/audio/MOSS-SoundEffect-v2-GGUF/moss-soundeffect-v2-q8_0.gguf --backend vulkan --text {text} --request-option duration_sec={seconds} --request-option seed={seed} --out {out}",
               "notes": "licence: Apache-2.0; 0.1-30 s"},
  "ace-step": {"kind": "music", "seconds": 30,
               "command": "audiocpp_cli --task gen --family ace_step --model ~/models/audio/ACE-Step1.5-GGUF/turbo/ace-step-1.5-turbo-q8_0.gguf --backend vulkan --task-route text2music --text {text} --lyrics {lyrics} --duration-seconds {seconds} --seed {seed} --out {out}",
               "notes": "licence: MIT; instrumental when no --lyrics"},
  "parakeet": {"kind": "listen", "gpu": false,
               "command": "audiocpp_cli --task asr --family parakeet_tdt --model ~/models/audio/Parakeet-TDT-0.6B-v3-GGUF/parakeet-tdt-0.6b-v3-q8_0.gguf --backend cpu --audio {in}",
               "notes": "licence: CC-BY-4.0"},
  "server": {"kind": "speech", "url": "http://127.0.0.1:8080/v1/audio/speech", "model": "qwen3-tts",
             "unload": "http://127.0.0.1:8080/v1/tasks/unload_all_models"}
}
```

Measured there (2026-11-20, #396, each call loading its model): Kokoro and Parakeet on the CPU
under 2 s a clip; MOSS 23 s and 5.1 GB for 4 s; ACE-Step turbo 12 s and 10.6 GB for 20 s. A sound
effect's hit came 0.8-1.45 s in (the skill says to trim the lead); ACE-Step peaked near clipping.

**How it works** (`workbench/audio.py`, `cli.cmd_audio`; shared: `workbench/media.py`).
- Keys as for images less `api`, `steps`, `workflow`, plus `kind` (`speech`, `sfx`, `music`,
  `listen`; default `speech`; another is refused, #596), `voices`, `seconds`; a listener's `timeout`
  is 600 s. A command fills `{text}`, `{out}`, `{seed}`, `{seconds}`, `{voice}`, `{lyrics}` (`{in}`
  for a listener) as `wb image` does; a url engine is an OpenAI `/v1/audio/speech` server (speech
  only: effects and music have no common API; POST `{model, input, voice, seed, response_format:
  "wav"}`), a listener a command printing the transcript (plain, JSON `text`, or audio.cpp's
  `text_output=` line) or a `/v1/audio/transcriptions` server. `key` goes to the speech,
  transcription and unload URLs alike. The first listener checks every speech batch. `engine(name,
  kind)`: the named one (refused when of another kind), else `--kind`'s default (`default_of`:
  `default_<kind>`, else its first; *The default engines*), else the first non-listener's kind's.
- `generate` refuses before anything runs a voice not in `voices` and any option the engine has no
  slot for (refused, not dropped). Its lease is `media.turns("wb audio", …)`: none when both engine
  and listener are off this GPU, pinned only when both are, `free_gpu` keeping either's own server. An API task takes no lease
  (#1015), so audio runs beside it (not beside its `--unload-for-tests` tests).
  Each clip is made in `.wb-audio-*`, checked (`inspect`), then published with its sidecar and
  spectrogram as the first free `<slug>-<seed>.wav` (`media.free`). The `unload` URLs of engine and
  listener are each POSTed once at the batch's end (`_unload`, #594).
- `inspect`: with ffmpeg, a 16-bit PCM copy at the clip's rate and channels (`_plain_tag`, since
  `wave` reads WAVE_FORMAT_EXTENSIBLE only from 3.12), `ebur128` loudness, a `showspectrumpic`
  picture, and a 16 kHz mono copy for the listener alone. Without ffmpeg, `wave` alone ("loudness:
  no ffmpeg"; a float WAV noted "unreadable: …"). What isn't audio (`MAGIC`) fails: "audio engine
  <name> wrote no audio (it starts '…')", nothing published (#592). Levels come from each frame's
  loudest channel at the clip's own rate (the mono copy hid one-channel clipping and an inverted
  channel, #586), streamed in 50 ms blocks (`_measure`, `_Meter`: memory doesn't grow, #599).
  Silence is below `SILENT_DB` (-50 dBFS), a gap `GAP_SECS` (1 s), clipped ≥ 0.999 of full scale.
- Scoring (`score`): `_align` finds the fewest word edits (Levenshtein, banded from `ALIGN_BAND` 32
  and widened until exact; past `ALIGN_CELLS` 64M cells "too long to score", a listener failure,
  #947; `difflib` misscored repeated words, #593); `wer` is edits over the script's words,
  uncapped; `differences` the first four spans `script → heard`, ignoring spacing and apostrophes, both from
  one alignment (`inspect` calls `score` once, #947). `words` NFC-normalizes (#747), keeps letters,
  digits, apostrophes and their combining marks (`\w` split Hindi and Thai at vowel signs), and
  splits scripts written without spaces (`UNSPACED`: Thai, Lao, Myanmar, Khmer, kana, CJK; not
  Hangul) into characters; mostly such a script scores CER (`cer: true`, #598). The line shows what
  was heard too, since a WER alone misled (#396: "2026" for "Twenty twenty-six" read 30%). A failing
  listener is reported on the line, not raised.
- The line: `3.4 s (asked 3) · peak -1.2 dBFS · -16 LUFS · lead 0.1 s, tail 0.3 s · gaps … · heard
  "…" WER 30% (twenty twenty six → 2026)` and the spectrogram's path; under 80% of the asked length,
  "cut short". `lines()` (`wb audio --list`, `wb scout`) shows kind, readiness, voices and `notes`
  ("licence: not noted"); `coverage()` is scout's summary.
- **Listening by ear** (#397, `chat.listen`, `wb chat listen <clip>… [--text]`): up to `LISTEN_MAX`
  (8) takes of `AUDIO_TYPES` (what a browser plays), each at most `AUDIO_MAX` (64 MiB), are copied
  into `chat/audio/<batch>/` as `<n>.<ext>` with `audio.json` (names, paths, types, `audio.summary`
  check lines) and sent as a Claude message carrying `audio` (`chat.audio_batch`: `{batch, clips:
  [{n, name, line}]}`); the page plays wb's copy, so a clip changed since plays as sent.
  Links, missing or unplayable clips, too many or too big are refused before any copy; a failed copy
  removes the batch; the newest `LISTEN_KEPT` (20) batches are kept. The pick returns as the user's
  `Keep take <n>: <name>` with `about: {audio, n, name, path}`, name and path from the batch, not
  the page (*The web chat*).
- The sidecar: `{text, kind, seed, seconds, voice, lyrics, refs, engine, secs, ts, duration, peak_db,
  clipped, lead, tail, gaps, silent?, lufs?, transcript?, wer?, differences?, listener?,
  listen_error?, unreadable?}`. `--out` defaults to `$WB_HOME/audio/<YYYYmmdd-HHMMSS>/`.

## Git, commits and projects

- **How workbench reads git** (`gitops.git`): `-c color.ui=never` (and `color.diff|status|branch`),
  `core.quotePath=false`, `log.showSignature=false` (signed commits broke a `%h` read and accept
  exited 1), `submodule.recurse=false` (#888); diffs `--no-ext-diff --no-color`; changed files from
  `status --porcelain` (the user's `diff.external` and colours once changed the review diff: the
  2026-11-16 audit). Output is UTF-8 with U+FFFD for bad bytes. `gitops.mark`, the runner's edit
  fingerprint, streams git's raw bytes into the hash (`git_hash`, #678) and takes untracked files
  with `lstat` (past `MARK_READ_MAX`, 1 MiB, by size and time), so a FIFO or a link to `/dev/zero`
  can't hang it.
- **Pinned to the task's git dir** (`gitops._pinned`). The worktree's `.git` file is the agent's to
  rewrite; pointed at its own git dir, that config (`core.fsmonitor`, hooks) ran from `mark()`
  outside the lane (the 2026-11-16 audit). So `create_worktree` records the real git dir and the
  `.git` text in `rules/<task>/gitdir.json` (rewritten only by `wb relocate`), and every call on a
  task's worktree runs with `--git-dir`/`--work-tree` from it, `core.hooksPath=/dev/null`,
  `core.fsmonitor=false`, and `core.filemode`/`core.ignorecase` set to what the state dir's file
  system does (`gitops._modes_hold`, `_case_folds`, probe files; unknown pins nothing): the repo's
  values committed a 0755 script as 100644 (#323), flipped every file to 100755 on exFAT (#683) and
  hid case-only renames (#492). The agent's reads in the task's repo get the same pins from the shim
  where the repo differs (`gitops.fs_pins`, `differing=True`; #796). On a case-folding file system
  `add -A` misses case-only renames (#656): `gitops.case_renames`/`record_case_renames` stage them
  before the review diff, the snapshot and `commit_all`; `mark` hashes them. A `.git` not holding
  the recorded text is a `GitError` ("the worktree's .git was rewritten"): the runner fails the
  task, accept refuses. Tasks from before the record run unpinned.
- **No git hooks from the worktree.** A relative `core.hooksPath` (husky's `.husky/_`) runs
  agent-written hooks as the user. `commit_all`, `worktree add|remove` and `--merge`'s merge,
  fast-forward and squash (with `--no-verify`, #499) run with `gitops.NO_HOOKS`
  (`core.hooksPath=/dev/null`): no hook runs, the user's included. Signing is kept. Checks belong in
  `--test`, in the lane.
- **Git LFS** (#382). A `--shared` scratch clone has no local config, so it checked out pointers:
  `gitops.share_lfs` gives it the repo's `filter.lfs.*` (`lfs_filter`) and `lfs.storage` at the
  repo's LFS directory. `wb start`'s disk check counts each pointer at its `size`
  (`gitops.checkout_size`, `LFS_POINTER*`). The suite's LFS test skips without git-lfs.
- **Checkout settings** (#426). A scratch clone must check out the task's bytes (`core.autocrlf`
  gave the task CRLF and the base check LF). `verify.checkout_settings` copies the repo's
  `CHECKOUT_KEYS` (`core.eol`, `autocrlf`, `safecrlf`, `symlinks`, `filemode`, `ignorecase`,
  `checkRoundtripEncoding`), its non-LFS `filter.<driver>.*` (`verify.local_filters`, #932) and
  `info/attributes`. A filter keeping state in the git dir (git-crypt) still fails: up to
  `FILTER_SAMPLE` (50) unedited filtered files are compared with the worktree (`verify.filter_gap`),
  and a difference or a failed `required` filter makes the run `"filtered"`: the base check says the
  base wasn't checked, `wb verify` is `inconclusive` (`filtered`), `wb shot`'s side `exit filtered`.
  bench's clones (`bench._clone_base`, #736) compare with what the repo smudges
  (`verify.smudge_gap`, git 2.40+), and a gap is a `RuntimeError` (`bench._filtered`): `bench add`
  refuses, a replay is an `error` row, never graded on other bytes.
- **The split of roles.** The agent writes code and reads git. Everything that leaves its working
  tree is Claude's: commits, branches, merges, pushes, PRs, CI/CD, deploys, infrastructure, and the
  project's `CLAUDE.md`/`AGENTS.md`; the agent asks through `ask_reviewer` or says so in its summary.
- **Git is read-only for the agent, enforced** (`workbench/gitguard.py`). `install` writes
  `rules/<task>/bin/git` every round, a `/bin/sh` wrapper `exec '<python>' -I -c '<code>' "$@"`
  (a shebang splits at a space in the Python's path) calling `gitguard.main(<real git>, <common
  dir>, argv, <temp roots>, [<worktree>, <lane's writable dirs>], <pins>)`, first on the agent's
  `PATH`. `split` skips global options and their values; `reads`: none or `READ` (41) reads;
  `branch`/`tag` only listing (`TAG_LISTS`, #443); `apply` without `APPLY_INDEX` (#890);
  `hash-object` without `-w`; `fsck` without `--lost-found`; `config` reading (`CONFIG_VALUED`,
  #794); `stash list|show`; `worktree list`; `remote` bare, `remote get-url|show`; read forms of
  `reflog`, `notes`, `submodule`, `symbolic-ref`; anything else (aliases, `lfs`) writes. `git
  submodule update|init|sync` in the worktree or a scratch checkout of it is not run: on submodules
  workbench filled (`filled_submodules`) it exits 0 with `FILLED`, an empty one `UNFILLED` and 1
  (builds that init their own submodules went red, #885). Reads run with `GIT_OPTIONAL_LOCKS=0` (no
  index refresh while Claude diffs) and the pins. A write is refused with `REFUSAL` in the task's
  repository (`protected`; a `config --global|--system|-f` write judged by its file,
  `config_files`) or any repository outside the temp roots, the worktree and the lane's writable
  dirs; outside any repository it passes, and throwaway repos there pass (test suites, #444;
  pre-commit, uv, Go, Bazel caches, #665). The roots are fixed at install, so `TMPDIR` can't make a
  project scratch. `init`/`clone` are judged by where the repo lands (`new_repo`, `nested`, else
  `destination`). Git by full path is denied (`context.denied_shell()`) and the task repo's refs are
  outside the lane. Standard library only: it runs on every git call.
- **Claude's files.** OpenCode's `edit` rules refuse `CLAUDE.md`, `CLAUDE.local.md`, `AGENTS.md`,
  `.claude/`; a shell change is named in the round report (`runner.claude_files_changed`).
- **The agent never commits.** `wb diff` shows its work against the base, or the last `wb next` step
  (`--all`: the branch), new files through intent-to-add in a scratch index copied from the
  worktree's (`gitops.with_new_files`: its index and locks untouched, skip-worktree entries not
  deletions). `gitops.add_all` reruns `add -A` with `--sparse` only after a sparse refusal (an older
  git has neither, #493). A git error in the diffstat costs only that line (`(no diffstat: …)`).
- **Nested repositories** (`gitops.nested_repos`) are left out of the diff, noted (`NOTE: <dir>: a
  git repository of its own, …`, `gitops.nested_line`) and refused by name by `commit_all`, so accept
  and next stop in review (a gitlink to nowhere was committed).
- **Edits in a filled submodule** (`gitops.submodule_edits`, `submodule_check`). Filled files are
  read-only, but `sed -i` replaces through the directory (#419). Each filled submodule, nested ones
  too, is recorded in `rules/<task>/submodules.json` with a copy of its index taken after the fill,
  kept with its time (`shutil.copy2`: a fresh mtime hid same-second rewrites, CI, 2026-11-21);
  `diff-files` against the user's submodule checkout finds changes: a NOTE and `commit_all` refuses
  ("not committed: edits in submodule <path> can't land: …"). New files (`ls-files --others
  --exclude-standard`) are only noted ("new files in submodule <path> won't be committed: …"; build
  objects refused every accept, #889); an uncheckable one says "… can't be checked: … wb relocate"
  (#682), never skipped. The directories stay writable: in-tree builds write there.
- **Commits are the user's** (`gitops.commit_all`: `add -A`; refused with nothing staged, a nested
  repo, or, without `--include`, a file git ignores). `wb accept` commits under the user's identity,
  no AI co-author line, only provenance:
  ```
  feat: CSV export for reports

  Delegated to <model> via OpenCode: 3 round(s), 1 automatic test-fix, 1 review round(s).

  Workbench-Task: csv-export
  OpenCode-Session: ses_…
  ```
  Rounds count every runner round (auto-fixes, notes, handovers); review rounds the last step's. `wb next` commits with
  `Workbench-Task: <task> (step N)`. A clean worktree accepts the branch's commits past the base;
  none is refused. An accept that committed then died finishes on retry by its trailer
  (`cli._landed`). `delegate/<task>` is made the commit (`gitops.put_on_branch`), refused when it
  holds commits the accepted one lacks (#417).

  **`--merge`** runs `merge --no-ff --commit --no-squash --no-log` in the user's checkout with git's
  own message (`gitops.merge_message`, #471), no hooks, refused unless the checkout is on a branch
  (`base_branch`, else any) holding the base (#418), has no tracked changes (`gitops._uncommitted`;
  a submodule only if the merge changes its gitlink, #505), no merge under way, and no untracked or
  ignored file where the merge writes (`gitops._overwritten`: ort silently replaces ignored files,
  #189; a file-to-directory change is the merge's, #343; an untracked link is in the way, #367; a
  case twin counts as the tracked file only on the same inode, `gitops._same_file`, #470, #640).
  Those paths come from the merge done first (`git merge-tree --write-tree`, 2.38+; else
  `gitops._trial_merge` in a `--no-checkout` scratch worktree with `NO_SMUDGE`, #469, whose failure
  refuses the merge), compared as raw bytes (#241, #320). `--commit --no-squash` outrank `mergeOptions` (#430), the squash passes
  `--ff` (#429). Success is the branch in HEAD and no `MERGE_HEAD`; a conflict, a signature past
  `COMMIT_SECS`, or SIGTERM/SIGHUP (`cli._ends_cleanly`) is backed out (`gitops._back_out`, only its
  own merge: `_made_by`; #420, #190). Merges into one checkout take turns (`gitops.merge_lock`, a
  flock on `workbench-merge.lock` in its git dir, held from `merge_from` through the merge, since
  the command lock is per task and two accepts interleaved, #639; refused past `MERGE_WAIT`,
  `COMMIT_SECS` + 15 s, never forced, since an agent can hold it; a flock the file system refuses
  is a `GitError`, #892). Without `--merge` it prints `merge with:` (`gitops.merge_by_hand`, in the
  repo's mode: `--merge-as` or commands.json `merge`, #471). A colocated Jujutsu repo
  (`gitops.jj_colocated`) has a detached HEAD: the refusal (`gitops.detached`) and `merge with:`
  give `jj -R <repo> new @- delegate/<task>` (`gitops.jj_merge`, #385). A task from a tag or commit has a null `base_branch` (`cli._merge_into`, #87).
- **The worktree** (`gitops.create_worktree`): `worktree add -b delegate/<task> <task dir>/worktree
  <base sha>` (peeled to its commit, never an annotated tag's id), refused when the branch exists; sparse (`gitops.sparse_of`, `apply_sparse`) when the
  repo is, as are the scratch clones (#263; verify's candidate uses `--ignore-skip-worktree-bits`,
  #493). A failed add removes all it made. `remove_worktree` is `--force`, falling back to deleting
  by hand and `gitops.drop_registration` (#793); nothing runs `git worktree prune` (it dropped a
  worktree on an unmounted drive, #641).

  A task outlives its checkout (#498): repo-level git runs where `gitops.repo_of` says, task.json
  `repo` while it is a checkout of the repository, else the main worktree (from task.json
  `git_common`). A git dir gone (the repo moved): `_pinned`, accept (`cli._repo`), feedback, next,
  resume (`cli._worktree_problem`) and the runner (`runner._repo_gone`) say "the repo moved or was
  deleted … wb relocate <task> --repo <its new path>" before changing anything; the shim and lane
  (`context.repo_dirs`, #776) and `wb bench add` (`bench._repo`, #909; a case's siblings from
  case.json `checkout`) follow `repo_of` too; commands.json `repos` matches a linked worktree by git
  dir (`context.repo_conf`). `wb
  relocate` (`gitops.relocate`) checks the base is there, finds the worktree's admin dir (relative
  links too, #682), refuses a `.git` naming another existing git dir, runs `git worktree repair`, and
  rewrites gitdir.json, task.json `repo`, `git_common`, `cwd` and `submodules.json`'s `src`.

  `wb discard` records status `tip` and deletes the branch (`delete_branch`, `(was <sha>)`) unless
  `--keep-branch`, the repo moved, or it has commits past the base (kept unless `--drop-steps`,
  #494). Accept records `tip` and after `--merge` runs `drop_merged_branch` (`branch -d`). Once the
  branch is gone, `gitops.step_commit`, `cli._landed` and `wb gc` (`_merged_branches`) read `tip`.
- **One command per task at a time** (`cli.one_at_a_time`): accept, discard, next, feedback, resume,
  takeover and stop hold `locks/<task>.cmd.lock` (`store.task_lock`, outside every lane: an agent's
  flock would wedge them), so a second waits and sees what the first did (an accept racing a
  discard deleted the accepted commit). `stop --all` takes each in turn, skipping one closed since.
  The runner never takes it.
- **The repo's stack** (`gitops.stack`, for the router and the skill records): the top two
  `LANGUAGES` by extension over `git ls-files`, each at least 20% of sources; a `.h` counts as the
  most common of `HEADERS`, C with none (#908).
- **`wb init <dir>`**: a repo on `init.defaultBranch` with an initial commit and `AGENTS.md` a
  symlink to `CLAUDE.md` (an `AGENTS.md` becomes `CLAUDE.md`; the reverse link is swapped). A
  gitignored `CLAUDE.md` gets no link (it would dangle in every clone). It refuses home or above, inside another repo, no git
  identity, and adds no remote. Before a first commit it refuses, changing nothing, uncovered or
  staged `cli.SECRET_FILES` and `cli.VENDORED` (#80: an `.env` was committed). In a repo with
  commits it commits only its own change, "chore: AGENTS.md links to CLAUDE.md" (`cli._commit_link`,
  a scratch index from HEAD, `_md_entries`, no hooks), leaving the user's edits (#931), and nothing
  ("not committed: …") for an already-staged path, an uncommitted `CLAUDE.md`, a gitignored target,
  or an operation under way (`gitops.under_way`, `UNDER_WAY`: it once consumed `MERGE_HEAD`).
- **CLAUDE.md stays true.** It goes into every prompt, so `wb accept` prints how many code commits
  it's behind (`cli.claude_md_status`, at `tip`, #269; a `subdir` task's package CLAUDE.md first,
  against the package's commits, the root's only when it has none, #783), and the skill has Claude update it after an accepted task that changes what it describes.

## Claude coding: when Claude writes a task's code

The user chooses (`wb coding`, #92; the pane's Settings row, #93) in `$WB_HOME/coding.json` `{mode,
ts}`, read fresh, default `decide`. The agent always gets the first shot.
- **Let Claude decide** (`decide`): nothing added, nothing gated.
- **Ask me first** (`ask`): after helping the agent, Claude may ask for one named edit; the user's
  yes is recorded with `wb coding allow <task> --edit "…" --said "<their words>"`.
- **Only when I say** (`only`): Claude reports the blocker and keeps the work until the user says.

An allowance (`tasks/<task>/coding.json` `{allowed: [{edit, said, ts, used, when?, started?,
did?}]}`, a `coding` event) is spent when work goes back to the agent (`spawn_runner` marks it
`used`). A **one-task exception** (#94, `--when "<condition>"`) is spent only once Claude has
started (the mod stamps `started`); Claude records `wb coding used <task> --did "…"` and hands back
with `wb feedback`. It is the task's alone: the setting never moves. The pane shows it
(`allowedLine`: `Claude may, once, if <when>: <edit>`). Enforced: `wb takeover` exits 3 without one
(`_coding_gate`) unless `--user` (the mod's hand-back, which records an allowance per task); the mod denies Edit, MultiEdit, Write and
NotebookEdit under `$WB_HOME/tasks/<task>/worktree/` without one (`codingRefusal`, the unmatched
`tool.call` hook), and outside `decide` adds a `claude-coding` block (`CODING_RULES`), a mid-session
change arriving as a row (`sayRules`, #540). Shell edits aren't seen: the block covers them.

**Dynamic workflows** (#1041). Whether Claude may split one request into several tasks that run at once:
the user's choice, `wb workflows on|off` (the pane's Settings row, #1023), kept beside the coding choice in `$WB_HOME/workflows.json` `{mode, ts}`
(`cli.WORKFLOWS`, `workflows_mode`), read fresh, default `off` (no file, unreadable JSON, an unknown mode). Like
`wb coding` it is the user's: Claude runs it only on their yes, and wb gates neither (no dialog: nothing it
starts could not start anyway). **Off**: the skill's rule, one task at a time, as before there was a choice.
**On**: the mod adds a `dynamic-workflows` block (`WORKFLOWS_RULE`, from `workflowsMode`, said with
`/workbench`'s text when that comes first) while delegating or with a task open, as `wanted` reads;
a change either way reaches the conversation as one `[workbench]` row (`sayRules`: the rule, or
`WORKFLOWS_OFF`), however made. The rule: fan out as a handoff plan (each task its own spec, size, role and
acceptance command), one shared name prefix for the group (no `--group` flag: tasks started together sit
together in `wb status` and the pane, which list by start), the plan in one line before the starts, each result
reviewed on its own, merged logic first, then tests, then UI or docs. Each task runs on its own role's model;
nothing new decides what runs together: the GPU lease (`models.apart` cards together, the rest `--queue`)
and the provider slots (#1015). No cap on tasks at once (the owner's call, not asked for). The pane's
Settings row is #1023's.

## Experiments

An experiment changes what the agent may do before it is measured enough: it ships off, the bench
(`--label`) and results rows' `experiments` decide it, and one that doesn't earn its place is
removed. `context.EXPERIMENTS` names each. `wb experiment <name> on|off` (or the pane) sets
`$WB_HOME/experiments.json`; `wb start` records task.json `experiments`
(`context.task_experiments`), so a flip affects only later tasks. Off, unable to work or unable to
help, the agent's prompt and rules are exactly those without it.

**probe** (`probe-code`, an outside program the user installs): the agent outlines a file (`probe
symbols`) or extracts one function (`probe extract <file>#<name>`) instead of reading it whole (half
the measured steps compacted or handed over, 2026-11-20). It needs a probe on the agent's PATH
(`context.probe_bin`) whose `--help` lists `symbols` (crates.io's 0.6.0 doesn't); `wb experiment`,
`wb doctor` and the pane say when it's missing (`PROBE_HOME`, `PROBE_RELEASES`, `PROBE_SETUP_NOTE`).
Off, it isn't run at all (a non-UTF-8 `--help` failed a start, #518; read with `errors="replace"`).
On:
- shell rules allow only `context.PROBE_READS` (`symbols`, `extract`, `search`, `grep`, `query`) and
  refuse `--lsp`: `probe *` let the agent start `lsp` and change `config` (2026-11-21);
- the handbook copy ends with `EXPERIMENT_NOTES["probe"]` (`agent/HANDBOOK.md` unchanged): extract
  from `PROBE_FILE_LINES` (1500) lines, `--allow-tests`, search `--dry-run -o plain`, never re-spell
  a search, `grep -n` after two misses, never pipe into `head` (0.6.0 aborts on EPIPE), what "File
  too large" means, how to page a long `read`;
- the protocol carries `EXPERIMENT_PROTOCOL["probe"]` (`context.experiment_protocol`), a condition,
  not an order: "When you need one function from a file over 1500 lines, `probe extract
  <file>#<name>` it instead of reading the whole file.";
- `PROBE_MAX_TOKENS` (the user's, else 2000) in its environment (`runner.agent_env`; one search
  printed 169 KB, #857); it caps matched code, not output, hence the note's `--dry-run -o plain`;
- **a read cap**: the layer's `tool_output` at a tenth of the window (`runner.output_cap`), which
  bounds shell output but not `read` (2,000 lines or 50 KB; OpenCode 2.3.2, 2026-11-21);
- **an outline in round 1** (`runner.add_outline`, once, a restarted runner adds nothing; before the
  GPU lease, since a hanging probe would hold the GPU, the review, 2026-11-21; status `outline`, `[]`
  when none): `probe symbols --allow-tests -o json` (`runner.probe_symbols`, `OUTLINE_TIMEOUT` 20 s;
  JSON, since the text's signatures span lines) on each long file in a probe language the spec names
  (`context.long_files`, lines counted by `context._Lines`, never read whole), as `OUTLINE_HEAD` and
  one `<first>-<last> <signature>` line per symbol, longest first, `OUTLINE_MORE` when cut, named
  from `task.workdir`, within `PROBE_OUTLINE_TOKENS` (1000) shared evenly. A failing probe adds
  nothing and never fails a round.

task.json `experiments.probe` is `"on"` or `false` (an old task's level or `true` reads as on),
`probe_files` the spec's long files (`context.long_files` at the base: `spec_paths`, `brace_names`,
the package first); the results row's `probe_long` says whether there were any. `wb status`: `probe: 2 call(s); whole reads of files
over 300 lines: 1` (`LONG_FILE_LINES`). The pane says "recommended" only when
`context.PROBE_RECOMMENDED` (`wb experiment`'s line then ends `; on is recommended`), which Claude
sets once a measurement finds on at least as good as off (#857's step 6): false, after the third
A/B.

**On (large files only)** (the owner, 2026-11-22, #857): off (default) or on, in `wb experiment probe`
and the pane. It had levels from 2026-11-21 (moderate, today's on; aggressive, an order that cost
more; adaptable, via a `probe_aggressive` knob, which always chose moderate), so they went. An old
level or `true` still reads as on (`context.setting`, `context.OLD_LEVELS`; a running A/B calls `wb
experiment probe adaptable`), hidden from help.

**Only where probe can help** (the owner, 2026-11-21, #857: "No sense in clogging up the subagents
with irrelevant information"). Set on and installed, probe is off for a task whose repo has no file
in a probe language over `PROBE_FILE_LINES` (`context.probe_off`, at `wb start`): task.json
`experiments: {probe: false}`, `experiments_off: {probe: "<why>"}`, everything byte for byte a
probe-off task's, and no `experiments` or `probe_*` in its results row. Reasons: `PROBE_OFF_SHORT`,
`PROBE_OFF_GAVE_UP` (`PROBE_SCAN_BYTES`, 64 MiB, ran out), `PROBE_OFF_PARTIAL` (a partial clone, git
before 2.44), `PROBE_OFF_HELP`, `PROBE_OFF_WRITING` (a writing task: no repo scan; *The writing role*).
Not installed: `{probe: false}`, no `experiments_off`.
- **Which files count**: the base's blobs (a `subdir` task's package) plus files the spec names,
  probe languages only, counted largest first through `gitops.feed_blobs` a chunk at a time, never
  whole; a blob of at most `PROBE_FILE_LINES` bytes isn't read; generated blobs (over
  `PROBE_SPARSE_BYTES`, 2 MiB, per `PROBE_FILE_LINES` lines in the first `PROBE_SPARSE_AFTER`,
  64 KiB) skipped (`gitops.SKIP_BLOB`), so a minified bundle can't eat the budget;
  only what the sparse patterns keep (`gitops.sparse_keeps`); in a partial clone with
  `GIT_NO_LAZY_FETCH=1` (#436). Submodules and symlinks don't count.
- **Which languages**: the installed probe's `probe search --help` `--language` values
  (`context.probe_language_names`, read from that option only) mapped by `context.PROBE_LANGUAGES`
  (made by trying 0.6.0's values; `yaml` and `swift` map to nothing on purpose; a new language needs
  only its row), cached per path and version in `probe-languages.json` (`context.probe_languages`;
  a probe with no version is asked each time). Unreadable, or no value with a row: off, uncached.
- **The outline** and `probe_files` take only those languages (`runner.outline_section`).
- **Image and audio generation** start no runner, so no experiment (`ProbeWhereItHelps`).

**Measured** on one 27B at Q3, a 40K window, a 16 GB card, with cases from workbench's commits on
1,500–3,000-line files, paired off/on replays (`wb bench run --label`, `wb experiment probe on|off`
between arms; `wb bench report --label <arm>` prints the use per arm, `bench.use_lines`):
- **the first A/B** (2026-11-21): probe never used in 14 replays with the note in the handbook only:
  hence the protocol line.
- **the second A/B** (2026-11-21, the order "before you read a file over 300 lines, run probe
  symbols"): used in every on replay, halved time and tokens on the 2,700-line file, but explored
  2–3× more elsewhere and handed over (median tokens 55K → 134K): hence the condition, the 1500-line
  threshold and `--dry-run -o plain`.
- **the quick A/B** (2026-11-21): the harder the wording pushed probe the worse it did; the condition
  alone was never worse than off. OpenCode's `read` filled the window in every arm: hence the cap
  and the note's paging sentence. A `probe` shim is the lever left untried.
- **the third A/B** (2026-11-22, #857's step 6, the condition with the cap and outline, 18 pairs,
  rule fixed before the run: used, at most one fewer pass, P(faster or fewer tokens) ≥ 0.8 by a
  Bayesian bootstrap, handovers not up): passed 15 off, 13 on; P(faster) 0.39, P(fewer tokens) 0.50;
  handovers 15 → 11; long whole reads 5 → 0. So `PROBE_RECOMMENDED` stays false and on stays
  opt-in. Another model or window may differ; the same replays check it.

## Verifying a fix against the base

`wb verify <task> [--test CMD] [--files PATH…] [--expect TEXT] [--timeout N]` (`workbench/verify.py`,
#96) runs one test twice at review, in scratch checkouts in the task's lane and environment
(`runner.run_in_lane` on a `verify._At`), set up like the worktree (`runner.prepare`), never in the
user's checkout or the worktree:
- **The base**: a `--shared` clone (`gitops.scratch_clone`, `bundle_steps`'s too; in a shallow repo,
  where git ignores `--shared`, made by hand in its object format with the repo's objects as
  alternates, #884) at the task's base, or the previous approved step (`step_base`, #252, #652),
  with the candidate's test files copied in (`--files`, else the changed files `TEST_PATH`/`looks_like_test` matches:
  `tests/`, `fixtures/`, `testdata/`, `test_*`, `*.test.*` and the like, test data included, #500,
  #731; paths as `:(literal)`, #194) and its deleted tests deleted
  (`verify.removed_tests`, #643). No test file: refused. A non-test `--files` path is
  `code_copied`, said in the report.
- **The candidate**: that clone with the tree accept would commit (`gitops.snapshot(landing=True)`,
  also `wb shot`'s and the staleness check's, not the review snapshot, #431): only what accept
  refuses without `--include` (a new binary of `BINARY_MAX` or more, directories past
  `NEW_FILES_MAX`) is `left_out`, a big text file is in (the review's `SNAPSHOT_MAX` dropped one and
  verify said proven, #642), and a check with anything left out is never `proven`.

The verdict: `proven` (the base fails on an assertion, the candidate passes), `base-passes`,
`candidate-fails`, or `inconclusive` (a timeout, overrun, failed setup, left-out files, or a base
failure that isn't proof). Proof is positive only: `--expect TEXT` in the base's output, else an
`ASSERTION` line or `FAILED_TEST` header (#730), unless first a `SETUP` error (#326) or a `MISSING`
file the candidate adds (#500). Never proof: `MISSING_API` (an API the candidate adds), or pytest
whose first `E` line (`PYTEST_E`) isn't `PYTEST_PROOF` (#729); a bare "expected" matched
`SyntaxError: expected ':'` (#193). `assertion` is the line shown (pytest's first `E` line, else
the first assertion line, never a `>` line, else the header). Runs are kept in
`tasks/<task>/verify/<n>/` (`base.txt`, `candidate.txt`, `result.json` `{cmd, files, tree, ts, base:
{rev, exit, assertion}, candidate: {…}, verdict, dir, expect?, uncopied?, removed, missing,
code_copied, left_out?, filtered?}`); `verify.last` adds `stale`. `wb report` ends with the newest
check (#97); `wb accept` prints `verify.note` when it isn't `proven` or is stale.
An API task's checks are the same, with no model call: with `--unload-for-tests`, `wb verify`, `wb
shot` and the runner's tests (`_tests_gpu`, for a lease with no pool) take this machine's pool alone
(`verify.gpu`), whatever the model's (#1026).

### Screenshots of the base and the candidate

`wb shot <task> --capture CMD [--viewport WxH] [--files PATH…] [--timeout N]` (`verify.shot`, #98)
runs the project's capture command on both sides, with verify's checkouts and base (`_review_base`,
#501), in the agent's shell (`runner.test_shell`, #517), writing a PNG to `$WB_SHOT_OUT` (absolute,
a fresh `.wb-shot-<random>.png`: #344, #433) at `$WB_SHOT_VIEWPORT` (1280x800). workbench ships no
browser. Pairs are kept in `tasks/<task>/shots/<n>/` (`base.png`, `candidate.png`, `base.txt`,
`candidate.txt`, `shots.json` `{task, step, round, cmd, viewport, files, ts, base: {rev, exit, ok,
image, error?, pruned?}, candidate: {…}, left_out?}`; `pruned` by `store.slim_archive`, #821;
`left_out` from `gitops.snapshot_left` adds `verify.LEFT_OUT`, #836). A side is `ok` only on exit 0
with a PNG (checked by signature, never through a link: `verify.shot_ok`; #268); else its `error`
names the exit and output file, even with a PNG ("the PNG it wrote anyway"). A failed capture never
blocks or passes a review.

`--chat` posts the pair (#99; `chat.shots`, *Files* under *The web chat*). "Reply about these" sends
`about: {task, n}` (`localStorage` `wb-chat-about`); the server fills step and round, and the mod
gives Claude `[web chat] (about <task>, step <s>, screenshots <n>) <their words>`, for `wb feedback`.
**Audio takes** (#397, `wb chat listen`, *Audio*, *Listening by ear*) come the same way: "Keep this
one" sends `Keep take <n>: <name>` with `about: {audio, n}`, and Claude gets `[web chat] (keeps take
<n> of the shortlist: <path>) Keep take <n>: <name>`.

## The web chat

`wb chat serve` (`workbench/chat.py`, `chat_static/`) serves a page on port 1337 that reaches the
Claude session in workbench mode while the user is away: messages both ways, Claude Code's
dialogs, push notifications (installable and notifying over https only: `tailscale serve --bg
1337`). Off until `wb chat on` (the pane's Settings row; `wb chat off`; the choice is kept).

**Keeping it up.** One `chat._Server` per bound address, binding without HTTPServer's host-name
lookup (it stalled macOS, #236). `systemd/wb-chat.service`: a user unit, `Restart=always`, no start
limit, `RestartSteps` 10 s to 5 min (retrying is what brings it up when an address arrives late; a
start limit gave up for good), stopping only on exit 78 (`RestartPreventExitStatus`: bin/wb's
Python and Windows guards, and `chat.ConfigError`, #534). Rebinding and a code reload re-exec in
place, so the unit sees no exit (#200). Where logind's `KillUserProcesses=yes` would kill what wb
leaves running (`cli.kills_sessions`, via `loginctl show-session`), each runner, the bench driver
(`bench.start`), `wb optimize` (`optimize.start`) and the server `wb chat on` starts (`chat.start`)
runs in its own scope (`cli.detach`, `cli.runner_scope`: `systemd-run --user --scope --quiet
--collect`, which execs, so the pid is the command's; #531); `wb doctor` warns when that scope
can't be had (`cli.logout_stops`), and `wb chat on`/`wb doctor` say when logind reports
`Linger=no`. macOS: `launchd/workbench.chat.plist` (`RunAtLoad`, `KeepAlive`, `exec
"$HOME/.local/bin/wb" chat serve` through `/bin/sh`, Homebrew's `python3` first; it retries even
exit 78; unchecked on a Mac, #449). Messages name the OS's service (`chat.service_name`,
`service_hint`, #756). Without a service manager the revive below restarts a chat `wb chat on`
started. `wb chat url` prints `http://localhost:<port>/#token=…` (the port from `chat/port.json`,
else `chat/serve.pid`, else 1337; `--port`, #349; the fragment never leaves the browser).

- **The path of a message.** The page posts it to `chat/messages.jsonl`; the listening session's
  mod submits `[web chat] <the user's words>` and nothing more (plus `(about <task>, step <s>,
  screenshots <n>)`, `(keeps take <n> of the shortlist: <path>)`, and per file `[attached: <path>
  (<size>, <type>)]`, `attachedLines`): into the running turn (`session.send`) or as the next
  prompt; a receipt (`wb chat ack`) shows at once. Claude answers with `wb chat reply` (the skill).
- **Attachments, each uploaded once** (#623). Each file gets its id on the page when picked and is
  uploaded by it before Send (`PUT /api/upload/<id>`, after `GET` says it isn't there); the server
  stores an id once (`chat.save_upload`), so a retry, reload or second tab never makes a copy. The
  body goes to `.part-<hex>` and becomes the file only whole. A message names files by id; an
  unknown id is 400; one file per sha256 per message. Limits, read per request: `upload_mb` (25),
  `upload_message_mb` (50), `UPLOAD_FILES` 10; refused (413) before the body is read, then read on a
  moment (`_linger`) so the page gets the answer, not a reset. Saved as `chat/uploads/<id>/<name>`
  (0700/0600) through directory fds `O_NOFOLLOW`, files `O_EXCL`: a link in the way is 409. A
  path-like name (`/`, `\`, `.`, `..`) is 400; others lose unprintables and leading dots and are cut
  to `UPLOAD_NAME` 140 bytes of UTF-8 (eCryptfs: 143), keeping the extension, never splitting a
  character (#742). A disk error is 507 (`ENOSPC`/`EDQUOT`) or 500, `{error: "not saved: …"}`,
  `final: true` for `FINAL_ERRNOS`; the page stops on `final` or the `SERVER_TRIES`-th (3) 5xx,
  else backs off (it re-sent forever, #742). `wb gc` drops uploads past `--days` whose message has
  a receipt (expired too, #741) or that no deliverable message names (`chat.drop_old_uploads`).
- **The switch.** `chat/switch.json` `{on}` (absent: off), written by `wb chat on|off`. `on` sets it
  only once a server of this account's answers (else `web chat left off: …`, #690), starting
  `bin/wb chat serve --port <port>` detached (`chat.start`: `chat/serve.log`, `chat/serve.pid`
  `{pid, port, start}`) when nothing answers at a planned address (a wildcard probed at its
  family's loopback, `chat._probe`, #450), then waiting `START_WAIT` 10 s (`chat.wait_up`; an exit
  fails at once with serve.log's last line, #200). `chat.who_answers`: a response whose
  `X-Workbench-Chat` (`chat.MARK`, on every response) equals `chat.identity(token)` (HMAC-SHA256 of
  a fixed string keyed by the token) is ours; anything else is "other" and `on` refuses (#554).
  `chat.running` is `who_answers == "workbench"`. Off: nobody listens or relays, no link, away ends,
  `listener.json` removed; a running server stays up; `off` stops only the process wb started
  (`chat.stop_started`, by pid and command line).
- **Coming back** (#449). `wb chat url` first restarts the server (`chat.revive`, silently: the mod
  reads the output as the link) when the switch is on, `serve.pid`'s server is gone, nothing answers
  on its port, and no service keeps it (`chat._unit_keeps_it`: `systemctl --user is-enabled`, on
  macOS `launchctl print`, #756). The mod runs it at session start and every 10 minutes while on.
  `wb doctor`: `cli.chat_gap`.
- **One listener.** A session listens while workbench mode and the chat are on; with several, the
  first live claim on `chat/listener.json` (its `wb events` pid and start).
- **Claude knows where the user is.** A web message has a typed message's authority, with a note:
  reply there, briefly; a yes there answers only a question asked there; nothing hard to undo on an
  ambiguous OK; while away, report there what finishes or needs them.
- **Away is the device the user last used** (#959). Away once they use the chat after last typing
  in the terminal (a web message, or a page shown, focused and in use: `POST /api/active`), until
  they type or answer a dialog in the terminal (answering counts: a tapped notice made the user away
  and the answer submitted no prompt); at most 6 h after that use, longer while one of this
  session's `--backlog` tasks is open (#110). `chat/away.json` holds it, the user's, not one
  session's (#688).
- **Claude Code's dialogs.** While away, every delegating session's mod puts them on the page
  (`kind`): `permission` (Deny and Allow alike in weight, #103; with Bash's `description` as
  `detail.purpose`, "Allow Claude to <purpose>? (Bash)"; a deny tells Claude to ask in the chat);
  `question` (AskUserQuestion: options, a box, Skip/Answer; picks reach Claude as `answers`);
  `plan` (ExitPlanMode: folded past 900 characters or 14 lines; Keep planning with a note; approving
  stays in the terminal: Claude Code ignores a hook's allow there but takes a deny, checked live
  2026-11-18). A tap settles the dialog; after 30 minutes or an interrupt it stays in the terminal.
  `prefs.json` `away: "allow"` allows a permission at once; questions and plans still ask; deny
  rules never reach a dialog. At the terminal, a permission unanswered after `NOTICE_AFTER_MS` 30 s
  gets one notice and a push (no buttons; one open per session; never for plans or questions),
  settled when answered so it is never pushed late (#959 review); only for a chat user (a page used
  within `PAGE_RECENT`, a day, or push on). The mod's side: *The web chat listener*, under *The mod*.

**Other agents** (#1042). The API is documented for other clients in `docs/chat-api.md` (every
endpoint, the stream, the agent flow with curl; `test_docs` fails on a path in `chat.py` it doesn't
name), and `skill/agents/claude-workbench/` is a portable skill (agentskills.io: SKILL.md, and
`scripts/ask_claude.py`, standard library only) the user points another agent at. The page is one
client; the server needs no browser.
- **Tokens.** `wb chat token add <name>` (`chat.agent_add`: `AGENT_NAME` `[a-z0-9-]{1,32}`, not
  `AGENT_RESERVED`, one per name) prints `wba-` + `token_urlsafe(32)` once and keeps its sha256 in
  `chat-agents.json` beside the token file (`chat.agents_path`, 0600, under `chat-agents.lock`);
  `list`, `revoke` (`chat.agent_revoke`). `Handler._authed` returns `"user"` or `"agent <name>"`
  (`chat.agent_for`, constant time, the file read per request so a revoke is at once) and sets
  `Handler.agent`; a held wait or open stream checks again at every wake (`Handler._revoked`:
  both went on reading its replies after a revoke, the #1042 review). An agent gets 403 `agent tokens can't …` on `AGENT_DENIED` (`/api/answer`,
  `/api/prefs`, `/api/push`, `/api/active`, `/api/seen`), and 429 `{error, retry_after}` past
  `AGENT_SENDS_PER_MIN` 30 sends in any 60 s (`chat.RateLimit`, per agent, in the server's memory).
  No switch of its own: on whenever the chat is and a token exists.
- **Marked.** Its message is `{from: <name>, role: "agent"}` (`chat._inbound`: indexed in
  `open.json`, receipted, expired, listed by `pending` as the user's), reaches Claude as `[agent
  <name>] …` (the mod, *The web chat listener*), never makes the user away, shows on the page with
  its name (`agentMeta`, `.row.agent`; no *send again*), and `wb chat log` says `agent <name>`. The
  skill: a teammate's request, answered with `wb chat reply --to <id>`, the user's consent gates
  untouched.
- **Send, then wait.** `wb chat reply` writes `re` (`--to`, else `chat.taken`, the user's alone
  when it holds one of theirs: an agent's message delivered into the user's turn read every plain
  reply to them, the #1042 review; `taken`: the messages whose newest receipt is `now` by the listening session, under `MESSAGE_EXPIRE` old, that no
  `turns.jsonl` row lists: a session that died mid-turn left its agent's message taken, and the
  next session's replies went to that agent alone, the #1042 review) and `to`
  (whose: `"user"` or agent names; `chat.reply`; an unknown `--to` exits 1). `GET /api/wait?after=
  <id>&timeout=<0–600, WAIT_DEFAULT 300>&until=done|reply` (`Handler._wait`, `chat.wait_state`)
  returns Claude's rows after `after` `addressed` to the caller (`to` names it; no `to` is the
  user's) and `done`: since the newest of them, else since `after`, a turn has ended (a message to
  Claude: a `turns.jsonl` row lists it; a reply: a row `ended` at or after its `ts`). It holds on
  the `Hub` until a reply, `done` (with `until=done`), nobody listening (at once: `listener:
  null`) or the timeout; at most `MAX_WAITS` 16 at once (503), each looking every `WAIT_LOOK` 1 s
  whether its client hung up (`Handler._gone`: an aborted wait held its slot to its timeout, and
  16 of them refused every wait, the #1042 review). It also answers `waiting_on_user`
  (`chat.waiting_on_user`: an open ask, or a notice not yet answered, under `ASK_STALE` old), and
  returns when that turns true during the wait (once: true at its start, it holds). `wb chat reply
  --to user` addresses the user's thread whatever the turn took: a consent question during an
  agent's request went to that agent alone, pushed to no phone (the review). `GET /api/health`:
  `{api, listening}`. `/api/state` carries `api` (`API_VERSION` 1).
- **Kept apart.** A `cid` another sender used is 409 (`chat.send`: an agent's send could get the
  user's message back); an agent's stream never makes the oldest give way (503 at once); the mod
  indents an agent's later lines, so none starts as `[web chat]` or `[message …]` (`chatPrompt`).
  An agent's `about` is 400 `agent messages can't carry about` (what a message answers is the
  user's to say).
- **An agent's view.** An agent reads only its own conversation: round one let its token read the
  whole state and stream, the user's messages and dialogs included. `Handler._snap` gives it
  `chat.agent_snapshot` (`{api, messages, listener, uploads, expire, now}`, `messages` only the rows
  `chat.in_view` passes: its own, and Claude's whose `to` names it; `chat.thread(keep=…)`) for
  `/api/state`, `/api/stream` and `/api/send`'s `state`; no asks, prefs, push, savings or build.
  `/api/wait` on a row not in its view is 404 (`chat.wait_state`). `AGENT_UNREAD` (`/api/latest`,
  `/api/ask/`, `/api/shot`, `/api/audio`) is 403 `agent tokens can't …` (`Handler._agent_unread`,
  before the path's own handler). Its upload keeps `by: <name>` in `.meta.json` (`save_upload(by=)`);
  a PUT on an id stored by anyone else is 409 `that upload id is another sender's`
  (`chat._not_theirs`: answered with the first upload, it read the user's file's name and path); a
  send naming another's upload is 400 `no upload <id>: …`; GET `/api/upload/<id>` and
  `/api/file/<id>` are 404 unless it uploaded the file or a row in its view carries it
  (`chat.agent_may_read`).
- **Dialogs go to the human during an agent's work** (the owner, 2026-11-23). From an `[agent
  <name>]` prompt until that turn ends, the mod puts Claude Code's dialogs on the page with a push,
  as while the user is away, even at the terminal, where the dialog stands too; whichever answers
  first settles it. The ask carries `for: [names]` (`wb chat ask --for <name>`), so the card says
  `for <name>` (`.ask-for`) and the push's title `(for <name>)` (`sw.js`); with the user at the
  terminal it also takes `--here`, and their away setting (`prefs.json` `away: "allow"`) doesn't
  answer it (`chat.ask(here=)`). An agent's token still can't answer it (403), and
  `waiting_on_user` counts it. The mod's side: *The web chat listener*.

**Files** (`$WB_HOME/chat/`):
- `messages.jsonl`: `{id: "<ms>-<6 hex>", ts, from: "user"|"claude"|"notice"|<agent name>, text, cid?, shots?,
  audio?, about?, files?, role?: "agent", re?: [ids], to?: ["user"|<agent name>]}`. A `notice` (`wb chat notice`) has `text: "Claude is waiting on a permission|plan|question in the
  terminal"` and adds `tool, what?` (2000 characters): news like Claude's
  (`chat.NEWS`: pushed, `/api/latest`) but not a reply, so the owed reply (#767) reads only `from:
  claude`; `chat.thread` adds `answered` once settled. `files`: `[{id, name, size, type, path}]`.
  A post whose `cid` is known returns that row (`_by_cid`, the newest `CIDS_KEPT` 5000, #199), and
  a user row so found missing from `open.json` without a receipt is put back (`_reopen`, #446).
  `shots` (`chat.shots`): `{task, n, step, round, viewport, base: {rev, image, exit?},
  candidate: {…}}` (#268). `about`: `{task, n, step, round}` from the pair's shots.json, never the
  page; or `{audio: <batch>, n, name, path}` from the batch's audio.json. `audio`
  (`chat.audio_batch`, `wb chat listen`): `{batch, clips: [{n, name, line}]}`.
- `audio/<batch>/`: `<n>.<ext>` copies and `audio.json` `{batch, ts, clips: [{n, name, path, file,
  type, line, engine?, seed?, voice?}]}`; the newest `LISTEN_KEPT` 20 kept.
- `receipts.jsonl`: `{id, ts, by, how: "now"|"queued"}` from `wb chat ack` (newest per id stands;
  `queued` is acked `now` when its turn ends); `{by: "wb", how: "expired"}` (retired unread);
  `{by: "terminal", how: "answered"}` (`chat.notice_done`, which also marks it seen).
- `open.json`: `{messages, asks, answered}`: the unreceipted user messages and unanswered asks
  (`chat.open_work`), under `open.lock`, kept by send/ack/ask/answer, rebuilt once when missing
  (`_open_rebuild`). Everything that asks what is open reads it, never the files' tails (#329).
  `answered` keeps each ask's first answer `ANSWER_KEPT` (a day) for `chat.find_ask` (#447).
- `claims.json`: `{<id>: {by, ts}}` under `claims.lock`: `wb chat claim` gives a message to the
  first session, again to it, or to another after `CLAIM_STALE` 120 s; else exit 1, and for an
  expired one `<id> expired: over 6 hours unread, so not passed to Claude; the page offers to send
  it again`.
- `away.json`: `{at, by}` (seconds), by the mod's listener on a web message (`by` its key) or the
  server on page use while on (`by: "page"`); `{at: 0}` on a prompt typed in any session, a terminal
  dialog answered there after a later use, or the chat off.
- `page.json`: `{at}`, the last page use, chat on or not (for `PAGE_RECENT`).
- `listener.json`: `{session, cwd, pid, start, machine, busy}` (from the `wb events` hello). Counts
  while its pid is this user's and started at `start` (`chat._alive` = `models._alive`: a root
  daemon on a recycled pid answered EPERM, #532), without a pid while its `ts` is under 15 s, and
  always when `machine` is another's (`chat.listener`, #922).
- `asks.jsonl`: asks `{id: "ask-<ms>-<8 hex>", ts, tool (200), text (2000), kind:
  "permission"|"plan"|"question", detail?, pid?, for?: [agent names]}` (no `kind`: a permission; `for`:
  asked while Claude worked on those agents' request, #1042), then answers `{id, ts,
  answer: "allow"|"deny"|"expired", by: "web"|"setting"|"timeout"|"interrupted"|"terminal"|"cli",
  note?, answers?}` (`answers` keyed by the question's text). The first answer row wins, so the
  page's tap and the mod's expiry can't both stand; `wb chat answer` prints the one that stands. Once `pid` (the session's `wb events`) fails `kill -0`, the next `chat.asks` expires it by
  `interrupted` (`_orphaned`, #538); an ask without `pid` only goes `stale`. `detail` is at most `DETAIL_MAX` 40,000 bytes of UTF-8 JSON
  (ASCII escapes refused a Japanese plan, #535); over it `chat.fit_detail` cuts the plan, else
  previews, else descriptions, ending `CUT_NOTE` " (cut: the rest is in the terminal)"; only a question that can't fit is refused. A plan's
  `{plan}` comes from the input, else `chat.session_plan`: `{plan_file}`, else the last plan file
  the `--session`'s transcript names, only a `*.md` directly in `$CLAUDE_CONFIG_DIR/plans` (the
  newest file there showed another project's plan, #253); none: `{plan: "", project}`, expired by
  `terminal` at once, the page naming that project's terminal. The page lists the newest 20 plus every open one; `wb chat pending` lists
  every unreceipted message (#265). Open past `ASK_STALE` (an hour): `stale: true`, no buttons. A
  user message unread past `MESSAGE_EXPIRE` (6 h, the one threshold) is `expired: true`; claim
  refuses it and the page offers *send again* (a new cid, the same uploads and `about`, #843). The
  first `chat.open_work` to see it retires it (`_retire`: out of `open.json`, an `expired`
  receipt that claim then reads, `_expired_receipt`, #536, #741).
- `uploads/<id>/`: `<name>` and `.meta.json` `{id, name, size, type, sha256, ts, path, by?}`, written
  last (it marks the upload whole; `by`: the agent that uploaded it, #1042); `uploads.lock`: one
  finishing at a time.
- `prefs.json`: `{away: "ask"|"allow"}` (else `ask`), read by `chat.ask`.
- `turns.jsonl`: `{ids, ended, by}`: a main turn of the listening session ended, having taken the
  messages `ids` (none: it only wrote to the thread), from `wb chat ended` (the mod's
  `chatTurnEnd`, #1042); read from its last 4 MiB.
- `push.json`: `{subs: [{endpoint, ts, ua}]}` (`MAX_SUBS` 20, `push.lock`); `seen.jsonl`: `{id, ts,
  by: "page"|"push"|"refused", endpoint?, status?}` (with `endpoint`, that device only, #537; an
  old push row without one counts for all); `push-test.json` `{ts}`. The VAPID key is `<token
  file>-push` (600), written whole to a fsynced temp and linked (renamed without hard links) into
  place, an empty or unparsable one replaced (`webpush.private_key`, #924).

**Settings.** The token: `$WB_CHAT_TOKEN`, else `~/.config/workbench/chat-token` (`O_EXCL`, 600,
`token_urlsafe(32)`; under 16 characters refused), read once at start. `$WB_CHAT_CONFIG`, else
`~/.config/workbench/chat.json`: `upload_mb`, `upload_message_mb` (read per request straight from
the file, `chat.upload_limits`, so a broken `bind` can't fail requests); `bind`, replacing
localhost: `localhost`, an address, `tailscale` (from `tailscale status --json`: the IPv4, else the
first IPv6, #937; the CLI on PATH or `TAILSCALE_APP`, never Windows' under WSL, #920; skipped as
`tailscale (not running)`, `(no Tailscale address)`, `(CLI not found: …)`), `0.0.0.0` or `::`;
`hosts`, extra names (`*`: any). Each is a list of strings (a string is one item); anything else is
`chat.ConfigError`, exit 78. A wildcard drops its family's specific binds, and `::` beside IPv4
gets `IPV6_V6ONLY` (`plan()["v6only"]`): each further socket failed `EADDRINUSE` (#534). `--bind`
replaces, `--host` adds, `--port`. Answered names (`chat.plan`, at startup): loopback, each bind's
own names (a wildcard's: the host's names and addresses), `hosts`, and on any bind this machine's
full MagicDNS name while Tailscale is up (only the full name, which no other site's DNS can answer for; `chat.tailnet_name`, at most every `REPLAN_EVERY` 30 s, for an unanswered
Host: `tailscale serve` passes it, #448; under WSL without a Linux CLI, Windows' `tailscale.exe`,
`tailscale(windows=True)`, `TAILSCALE_WINDOWS` via `hardware.windows_program`). An address it can't
listen on ends `serve` with one line (`can't listen on <addr>:<port>: the port is taken: …; --port
picks another`; `serve` prints its binds, the skipped ones, and warnings for a wildcard or `hosts: *`). With a bind skipped or a `tailscale` bind it rechecks every `REPLAN_EVERY` and
re-execs in place when the address set differs (#200). Every `RELOAD_EVERY` 10 s it stamps its
`*.py` (`code_stamp`, `watch_code`); changed and then still for a look, it re-execs
`sys.orig_argv` in the same process (an old server served the new page for a day and a half,
2026-11-18). A `bind` with neither loopback nor a wildcard makes `wb chat url` name its first
address (#534).

**The HTTP API** (`Handler`, HTTP/1.0, no keep-alive, no CORS; `TIMEOUT` 30 s stall closes):
- Every request: `Host` (port, brackets stripped, lowercased) must be an answered name or the
  connection's own address (`Handler._local`, #533), else 403 `unknown host <host>: add it to
  chat.json's hosts`: this stops other pages and DNS rebinding, which needs a name, never an address. Headers: `Cache-Control: no-store`
  (not `/api/file`), `X-Frame-Options: DENY`, `nosniff`, `no-referrer`, a CSP (`default-src 'self'`,
  inline styles only, images also and media only from `blob:`, no framing, no forms).
- `GET /`, `/<name>` with a known suffix (`TYPES`: html, js, css, svg, png, webmanifest) from
  `chat_static/`, else 404; `/manifest.webmanifest?k=<token>` (constant time) gives `start_url:
  "/?token=<it>"`, any other `k` the plain `/`.
- The API wants `Authorization: Bearer <token>` (constant time), the user's or an agent's
  (*Other agents*), else 401 "bad or missing token: open the link from `wb chat url`".
- `GET /api/health` → `{api, listening}`; `GET /api/wait` (*Other agents*).
- `GET /api/latest` (`chat.latest`): for a push: the newest open unstale ask (a plan without
  `detail`), Claude's last message (from the last `LATEST_BYTES` 1 MiB; a settled notice skipped), `push`: the
  snapshot's shape, which `sw.js` reads.
- `GET /api/state`: `{api, messages (the last 200 from `THREAD_BYTES` 4 MiB, user rows with
  `received`), asks (lean once settled, `chat.page_ask`: a plan cut at `PLAN_PREVIEW` 1,200 with
  `detail.cut`; #755), prefs, push ({key or null, `chat._push_key_or_none`, #924; test}), listener,
  savings (`chat.week_saved`: `wb savings --json` over 7 days, no `period`; its `api` adds each
  provider's name, `names`, #1022), uploads (`{file,
  message, files}`), build, expire (`MESSAGE_EXPIRE` in seconds), tasks (`chat.open_tasks`, #1016: `[{name,
  phase, model, via, provider_name}]` for each open task, no bench replay; the page's strip over the
  thread, `Running: fix-auth · API deepseek-flash (DeepSeek)`, hidden for none or an older server; the
  stream sends it when the chat directory wakes it, not on a task's own status write), now}`. `build`
  (`chat.build`, a crc32 of the page files' names, mtimes, sizes) makes a stale page reload.
- `GET /api/ask/<id>` (`chat.find_ask`): the whole ask, else 404 "no such prompt".
- `GET /api/shot?task=&n=&side=base|candidate`: a PNG, only for a task passing `store.NAME_RE`, an
  `n` with shots.json and a capture that worked, `O_NOFOLLOW`, at most `SHOT_MAX` 16 MB; `pruned`
  (`store.SLIM_DAYS`, #821) 410 "expired"; else 404 "no such screenshot". `GET /api/audio?batch=&n=`: a take, the batch matching
  `BATCH_RE`, a real directory with audio.json, a regular file `O_NOFOLLOW`, at most `AUDIO_MAX` 64
  MiB; else 404 "no such take". The page shows both as blob URLs fetched with the token.
- `GET /api/stream`: SSE: `event: state` at once, then `event: patch` (`chat.patch` against what
  this stream sent: `messages`/`asks` as `{put, drop}`, every other field whole so a stale busy flag
  can't stick; #755), `: ping` after 15 s idle. `MAX_STREAMS` 16: the oldest gives way (`evicted`),
  the new one waits 5 s (503), since phones drop streams silently. Linux: `TCP_USER_TIMEOUT`
  `UNACKED_MS` 45 s. One `Hub` thread watches `chat/` and a pidfd on the listener and wakes every
  stream; without inotify it wakes them only for a change, the listener's pid failing `kill 0`, or
  `IDLE_BUMP_S` 60 s (#841). A `_watch` failure restarts a second later. It keeps its `chat/`
  (`Hub.dir`) and stops at `close()` (#134).
- `POST /api/send`: JSON (415), ≤ 32 KB (413), `{text: ≤ 8000 (empty with files), cid?: ≤ 64,
  about?: {task, n} | {audio, n}, files?: ≤ 10 ids}` (400 "no such screenshots", "no such take",
  "no upload <id>: …"; over `upload_message_mb` 413; an agent past its rate 429) → `{message, listener, state}`
  (an agent's: `from: <name>, role: "agent"`).
- `PUT /api/upload/<id>` (8–64 of `[A-Za-z0-9-]`): a raw body (no multipart: `cgi` is gone in 3.13),
  the name URI-encoded in `X-File-Name`; 411 without `Content-Length`; 413 `too big: <size>, the
  limit is <limit>` → `{upload, new}` (`new: false` for an id already stored). `GET
  /api/upload/<id>`: `{upload}` or 404. `GET /api/file/<id>`: `SHOWN` images as themselves, else
  `application/octet-stream` as an attachment, so no upload renders as a page; cached for a year
  (`FILE_CACHE`, `Vary: Authorization`: an upload never changes under its id, #754).
- `POST /api/answer` (the user's token only, as the four below): `{id, answer: "allow"|"deny", note?, answers?: ≤ 4}` (`expired` is the mod's
  alone; `answers` only and always for an allowed question) → `{ask, state}`, the answer that stands.
- `POST /api/prefs` `{away}`; `POST /api/active` `{}` (`chat.active`: `page.json`, and `away.json`
  while on); `POST /api/seen` `{ids: ≤ 200, endpoint?}`; `POST /api/push` `{endpoint, on}` (only
  `https`:443 at `webpush.PUSH_HOSTS`, so the token can't make this machine post anywhere) or
  `{test: true}` → `{sent, ok, hint, reasons}`.

**The page** (`chat_static/`; no inline script, for the CSP). The token comes from the fragment (or
`?token=`) into `localStorage` `wb-chat-token` and leaves the address bar (other keys: `wb-chat-thread`, the
  last 50 for a reload; `wb-chat-seen`, 500 ids not replayed; `wb-chat-draft`, `wb-chat-about`); over https the manifest
link carries `?k=<token>`, since an iPhone app has its own storage. The stream is a `fetch` with the
bearer header (EventSource can't send one): each connection starts with a whole state, patches
merge by id (`merge`), and a POST reply or `/api/state` older than the newest state's `now`
(`stamp`) is skipped (#755); it asks `/api/state` at once beside the stream, and a page hidden
`HIDDEN_GRACE_MS` 60 s drops its stream. The outbox is one localStorage key per message (`wb-chat-out-<cid>`;
the old shared `wb-chat-outbox`, moved into them on load, lost a tab's message, #199), read afresh, sent in order, dropped only when the
reply names its cid or a snapshot lists it (#264, #934: a captive portal's 200 cleared it); a
`storage` event flushes, so any online tab sends all. A message the browser won't store is `held` in
the composer until the server has it (#245). A 4xx but 401 marks it not sent, except 408/425/429
(`transient`, retried after `retryAfter` or the backoff, #936). Each `post` gives up after `POST_MS`
15 s of silence, re-armed per chunk (#935); `flush` clears `flushing` in a `finally`. The stream: a
`DEAD_MS` 40 s watchdog counts as a failure (jittered backoff 0.5–30 s); an abort cancels the body's
reader (WebKit keeps the socket); back on screen after `AWAY_MS` 3 s, or from the bfcache silent for
`STALE_MS` 20 s, or on the worker's `{kind: "push"}`, it dials at once (2026-11-20); a new `build`
reloads (`reloadOnce`, once a minute). Use counts (`reportActive`, #959): on screen or a
`pointerdown`/`keydown`, only while `!document.hidden` and `document.hasFocus()`, at most every
`ACTIVE_MS` 30 s, never retried on a timer (#959 review). Attachments: chips (`drawTray`) in
`wb-chat-files`, bytes in IndexedDB (`wb-chat`, `files`) until the message lands; `pump` uploads one
at a time (XHR; `STALL_MS` 30 s without progress retries the same id); edit restores the files, the
`about` (`setAbout`, #843) and the words before the box's (#958); thumbnails load near view
(`whenNear`, #754); `sweepSoon` revokes unused object URLs (#746). The receipt texts, dialog cards
(`makeAsk`), notices (`makeNotice`) and the gear's sections (`#away-when` states the away rule
above; `drawSaved` shows no currency sign, as `wb savings`, #845, and after it `apiSaved`'s `API
models: 6 tasks, $1.27 (DeepSeek $0.84, Anthropic $0.43).`, or that alone, #1022) are in `app.js`. Over plain HTTP
off localhost the page isn't a secure context: own ids, `copyText` (#745), no install or push.

**The app and notifications.** The manifest ("Claude's Workbench", `short_name` "Workbench") is offered over https only (`app.js` removes its link
over http: localhost counts as secure and one computer got two apps). The `badge` is
`badge-96.png`, white on transparent (Android uses alpha only, #757). `sw.js` (`updateViaCache:
'none'`) caches nothing and answers no fetch (an offline cache gave iOS white screens). Pushes carry
no payload (RFC 8030), signed with VAPID (RFC 8292, ES256 in plain integers in `webpush.py`, checked
against RFC 6979 and openssl), so nothing written passes through a push service: the worker fetches
`/api/latest` with the key from Cache Storage (`wb-chat` `/key`, deleted before each write for
WebKit) and shows the newest unstale open dialog unless Claude's latest message or notice is newer
(#538; a notice's text as the title and its command as is, since `plain` drops `*`; "Notifications
work" for a test), tagged `wb-chat`. Turning notifications on subscribes with the VAPID key, again
when it changed. A page posts what it showed to `/api/seen`: for every device while in use
(visible, focused, an input within `USING_MS` 2 min), else with its `endpoint` if subscribed (#537).
A reply only to agents (`to` without `"user"`, `chat._for_user`) is no news: neither pushed nor
`/api/latest`'s (#1042). The `Pusher` thread (on each change, and every `PUSH_DELAY`/2) pushes each device news no page
showed within `PUSH_DELAY` 10 s and under `PUSH_WINDOW` 15 min old (never a backlog; nothing worked
out with no subscriber; seen.jsonl read on incrementally, `_seen_update`, #456), marking `push` per
device. Transient failures (`_transient`: unreached, 429, 5xx) retry after `PUSH_RETRY` 30 s (#86);
404/410 drop the device; other refusals are `by: "refused"` with `status`. `webpush.send` returns
`(status, reason)`; an uncheckable certificate is `webpush.CERT` (-1), final, not transient; each distinct reason is
printed once to serve.log (`web chat: a push to <host> failed: <reason>`); `webpush.tls()` adds `MAC_CAS` when the store is empty (python.org's
Python). A test's `chat.push_hint` (`cert`, `clock` for a 403, `unreached`, `refused`) tells the
page what to say (#539).

A change to `chat.py` or `chat_static/` also needs the browser test (*The tests*).

## Stopping everything

Only the user's two slash commands stop the delegation, never words in a message:

- **`/workbench-quit`**: `wb stop --all --park-waiting` with `scope()` (`--repo <the session's
  directory> --session <id>…`, every id the process has had): each task keeps its worktree; a toast and QUIT_NOTE (don't finish,
  restart or clean up unless asked; tasks left alone are another session's; on failure, the agents
  may still run); no turn. Every stopped task, and each of the session's already waiting on Claude,
  is `parked` with its news acknowledged (`cli._park_waiting`, #698): out of the status line's
  count, rewake, the boundary, the jobs' gate and `wb status` without `--all`, until `spawn_runner`
  or `wb takeover` clears it (#541).
- **`/workbench-quit-and-hand-back-claude`**: `wb takeover --all --user` with the same scope, then
  DONE_NOTE as a turn (leave the mode; per task taken over `wb diff`, finish it, run the acceptance
  command, `wb accept`, `--merge` only if asked; carry on without delegating) whenever the output
  has a `took over` line, even on a non-zero exit over a refused task (a toast says some weren't
  taken, the note carries the refusals, #918); none taken over, no turn.

The scope keeps them to this session's tasks (`mine`, *Waking Claude, exactly once*): another
session's agents keep running, listed as left alone (the 2026-11-16 audit). If `wb` fails, the
command says so. Typed in a terminal without a scope, both take every task. Both end workbench
mode and close the pane (`/workbench` turns it back on).

## The mod, inside

`mod/` is a Claude Code plugin: `.claude-plugin/plugin.json` (name `workbench`, `types`, one
setting `userConfig.wake` `on`|`off`, default `on`, changed with `claude plugin configure
workbench@workbench`, which reloads the module), `hooks/hooks.json` (`{"modules":
["./register.tsx"]}`), `hooks/register.tsx` (everything), `hooks/apprentice.ts`, `hooks/splash.ts`
(#963), `hooks/modeline.ts` (#976), `hooks/register.test.tsx` (`claude plugin test mod`), `types/index.d.ts`
(the atom types), `.claude-plugin/icon.png` (the `work` pose).
The root's `.claude-plugin/marketplace.json` (source `./mod`) makes the id `workbench@workbench`,
read from the clone: `wb update` plus `/reload-plugins` upgrades it. `mod/tsconfig.json` extends
the generated, gitignored `./.claude-plugin/types/tsconfig.json`.

**The `$` rule.** `claude plugin validate` follows every use of `$`; `$` may be passed only to
functions declared at the top level of `register.tsx` (a declaration or a const bound to one;
imports from the module's own files pass too). The helpers type `$` as `any`, so validate's listing
(hooks, calls, env reads, "gating hook without .catch") is the check. Env reads are string
literals: `HOME`, `WB_HOME`, `WB_BIN`, `CLAUDE_CONFIG_DIR`, `WB_SAVINGS`, `WB_COMPACT_AT`.

**State.** Ten `$.state` atoms survive a module reload; one read under an old shape tag is absent,
so change the tag when its type changes: `tasks` (`tasks-9`, WbTask[]: name, phase, round, model,
label, via, role, provider, routed (#1016), tests, last, createdAt, updatedAt, tokens, recent, repo, cwd, subdir, session, claude_pid,
claude_start, backlog, parked, allowed), `live` (`live-1`, {task, lines: {s, t}[]}), `chat`
(`chat-4`, {key, delegating, url, web}: workbench mode, also kept by session id in `$.store`
`modes`, #689), `pane` (`pane-1`, {tab, open, focus, minimized}), `sessions` (`sessions-1`,
string[]: the process's session ids), `savings` (`savings-1`, string: the pane's saving line), `stats` (`stats-1`, {key,
feed}: `wb stats --json` and its files' stamp), `top` (`top-1`, {snap, busy}: `wb top --json`'s
newest tick, each GPU's busy %), `said` (`said-3`, WbSaid | null, {notes, wakeOff, coding, routes, workflows}:
an atom since #697 (`routes` since #1014, `workflows` since #1041: a stored `said-1` or `said-2` is declined, read as nothing said), as in `mem` a reload emptied it and `sayRules` waited for good), `splash`
(`splash-1`, boolean: the opening `/workbench` came, so a reload never replays it; cleared when the mode goes off). Everything else
is `mem`, reset by a reload. Task facts are re-read from `$WB_HOME`; the mod writes only
`tasks/<n>/acked`, scout.json's `offered_at` and `chat/listener.json`.

**session.start.** `mem.root`, `mem.wb`, `mem.skill` (`$CLAUDE_CONFIG_DIR` or `~/.claude`, then
`skills/workbench/SKILL.md`); `WB_HOME`, `CLAUDE_CONFIG_DIR` and `WB_SAVINGS` resolved by
`absolutePath` as `store.absolute_env` does (settings.json passes `~` literally, #454), `WB_BIN` too
when it has a `/` or starts with `~` (#699); the chat key (`<start ms>-<6 base36>`, kept across
reloads); `mem.cwd`, `mem.home` (which
session-less tasks are `mine`); `mem.ids`. Workbench mode comes back when `$.store` `modes` holds
one of those ids (`modeKept`; `keepMode` writes every id when the mode turns on and a /clear's new
id while on, #840, drops the process's ids when off, ages entries out at `MODES_KEEP_MS` 30 days;
#689: `claude --continue` came back with the mode off), and on a successful `wb adopt` of a task
still delegated (`delegatedWork`: running, or waiting on Claude, not parked or `taken_over`, #834).
It registers the commands, runs `wb setup` once for a stale `skills/delegate` link, and starts the
ears and timers only when a person can be there (`isInteractive`, or a surface): a `claude -p` or
SDK session stays quiet until `session.attach` (it took the terminal's wakes, the 2026-11-16 audit).

**Ears and timers.** `ears` spawns `wb events` (*Waiting without polling*): the hello gives
`mem.eventsPid` and `mem.claudePid` (#687); every line runs `poll()`; it is respawned 2 s after it
ends. Until a hello in this load (`mem.earsOk`), a failure toasts once (`can't run \`wb events\`
(<why>)…: put wb on Claude Code's PATH or set WB_BIN`) and the wait doubles to a minute. A 1.5 s
timer polls with no events child, a task queued/running/testing/asking, a turn queued, or
`SAFETY_MS` 30 s since the last poll. A 450 ms timer animates the apprentice. Each poll reads `chat/switch.json` (`readSwitch`: off
clears the link and ends away, `back`); while on,
every 10 minutes `wb chat url` refreshes the link (`chatLink`; only a `…#token=…` URL). Limits: `wb`
120 s (`wb update` 600 s), `wb watch` and `tail` 30 s, `kill -0` 5 s. `runWb` returns the exit
status: a failing `wb` is said as a failure (`brief`), never passed on as its answer. Growing files
are read by `readTail`: whole up to 1 MiB (events 64 KiB), else `tail -c` from a whole line, since
`$.fs.read` rejects a file over 4 MiB (no wake came again, the 2026-11-16 audit).

**poll()** (re-entrant calls set `again`): `scan` reads every `tasks/*/status.json`, then
`task.json` for the tasks shown (closed ones drop out 30 min after their last update and aren't
read again that load; open ones get their last 4 event lines, `recentEvents`, cached by
size@mtime, #739); the status line; opens the pane once per load for a queued/running/testing task
of the session's or the mode on (unasked only on a wide terminal; one the user closed stays
closed); refreshes the transcript only while the Tasks tab shows (#838; `wb watch <task> --json
--last 2000` every `LIVE_EVERY_MS` 5 s while active, #691; `focusOf` sticks to the followed task);
submits one queued turn when idle (a dropped one waits a minute); the boundary compaction; the
weekly check, the update check beside it; then `deliver`, `rewake`, `listen`.

**Busy.** `mem.busy` is set by `turn.start` (main loop) and by the model's own main-loop
`tool.call` (`next.origin.plugin === 'engine'`: a module reloaded mid-turn never saw `turn.start`;
not a subagent's or another plugin's, which no `turn.complete` would clear), cleared only by the
main loop's `turn.complete`.

**Waking** (`deliver`). Only when notify.jsonl's size or mtime changed, Claude is busy, or a wake
was held (rows cached in `mem.notifyRows`, #921). Per task, the newest notify row under an hour old
and every question row past `acked`; still true when a check-in and the task is
queued/running/testing, a question while the phase is `asking` and `ask/<id>.json` has no
`<id>.answer`, or the phase equals the kind; a stale one is acked and dropped. A true one is
toasted once (`workbench: <name> <headline>`); a task not `mine` (its `session` among the ids, else
by directory) gets nothing more. Busy: a question goes into the turn (`intoTurn`: `acked` first, put
back if refused, `mem.heldAsks`), anything else waits; a held question whose `<id>.answer` opens
with ask_mcp's NO_ANSWER gets the MISSED_NOTE wake. Else `acked = row.ts` and `wake` submits
`[workbench] Task \`<n>\` <headline>.\n\n<row.text>\n\n<NEXT[kind]>`, plus `\n\n<AWAY_NOTE>`
while away (every wake and `intoTurn`: `awayToo`, #540) (HEADLINE and NEXT at the top of
register.tsx; a question's NEXT is `wb answer <n> --id <id>`, a PERMISSION request's the `wb allow`
it names). A dropped submit (`{drop}`) puts `acked` back if unchanged, records `mem.dropped`,
toasts once. With `wake` off it is only toasted and `acked` isn't written, so `wb wait` still gets
it. `rewake`: idle, wake on, a `mine` task waiting on Claude whose status changed between
`REWAKE_MS` 60 s and `REWAKE_MAX_MS` 12 h ago, whose news Claude hasn't had and whose wake wasn't
dropped in the last minute, gets `acked = now` and a prompt with `wb report <n>` and NEXT. Had the
news: `acked` at or past the later of `mem.noticed` (each task's newest notice in the tail, a
batch's for each task it names) and the task's `noticed` file (the runner's last status write lands
after its notice, so `updated_at` alone woke twice, #844), else past `updated_at`. `cli._ack_all`
stops short of news still true. Wakes are submitted as the plugin, not `asUser`. The state
directory is global, so each session acts only on its own tasks (the 2026-11-16 audit); among a
task's sessions the first to deliver wins (the `acked` write is not locked).

**Workbench mode** (`workbenchMode(on)`, `chat.delegating`). On: the `workbench` skill's prompt
(`skill.prompt`, which opens the pane via `showPane`; again while on with no task, read by
`firstTask` from the skill's "**First task:** \`…\`" line, it returns AGAIN_NOTE instead), or a
Bash call running `wb start|next|feedback|steer|answer|resume` (`delegates`, `HANDS_OUT`; not
`accept` or `takeover`, which a quit's notes ask for). Only a command counts, not text (#409), read
as bash reads it (`simpleCommands`, #560): quotes undone within a word, comments and heredoc
bodies dropped (`$(…)` and backticks in unquoted heredocs and double quotes still run), split at
`;`, `&`, `|`, newlines, `(`, `)`, `$(`, backticks; the command word past `VAR=x`, the reserved
words and the wrappers with their options (`PREFIXES`: `env`, `time`, `nohup`, `exec`, `command`,
`timeout` and its duration, `nice`, `ionice`, `stdbuf`) must be `wb` or end in `/wb`. Syntactic:
`true || wb steer` counts. Off: either quit. While on: the `workbench-mode`, `user-notes` (and
`claude-coding`, `dynamic-workflows`) blocks, the idle status line, pane auto-open, chat listening, the boundary
compaction. `mem.delegated` remembers it was on in this load, so a hand-back's compaction still
keeps the state. No `$.ui.invalidate`: rewriting the
first message would cost the prompt cache.

**Commands.** All are `/workbench` (the skill) or start with `workbench-`, so `/workbench` lists
them. `showPane` first closes a pane the engine holds but doesn't show. The quits: *Stopping
everything*. `/workbench-models` records the check as raised (scout.json `offered_at`,
`scoutAsked`) and queues SCOUT_NOTE. `/workbench-update` runs `wb update` and, when it updated,
`/reload-plugins` (`$.command.run`). A command can't start a turn itself: it pushes onto
`mem.queued`, which the next idle poll submits.

**Context blocks** (`prompt.context`, in this order; computed at the first message and after a
compaction or `/clear`, not every turn): `workbench-mode` while delegating (MODE_NOTE: until a quit,
follow the skill, every plan is a handoff plan, the SKILL.md path); `user-notes` while delegating
with notes; `dynamic-workflows` with Dynamic workflows on, delegating or a task open (*Dynamic workflows*); `web-chat` while away (`awayNow`; AWAY_NOTE: "The user is away from the terminal: they
last used the web chat", reply with `wb chat reply`); `workbench-wake` with `wake` off, delegating
or a `mine` task open (WAKE_OFF_NOTE: no wake will come, tell the user); `workbench` when `wb
status` with `scope()` exits 0 listing open tasks (plus `wb report`/`wb watch`; a failure toasts
once). None without a person.

**The rules said later** (#540). A block can't be added after the first message, so a session that
entered the mode later got no notes, no wake-off note, no coding rule. `mem.said` (`Said`, set by
`prompt.context`) records what was told; `unsaid` returns what is missing and counts it said (the
notes once, the wake-off note once, the coding rule whenever the mode differs; CODING_DECIDE on a
return to "Let Claude decide"; the workflows rule whenever it differs, WORKFLOWS_OFF on a return to off). `/workbench` appends them to its text and sets `mem.skillTurn`, so a
`prompt.context` in that turn leaves them out (a compaction or the turn's end clears it). Every poll runs `sayRules`: unsaid rules go in as a
user-role row (`$.session.append`, no turn), or, refused, a queued `[workbench]` turn. Away isn't
one: it rides on each wake (`awayToo`).

**Compaction.** `session.compact` (main conversation, a `mine` task open, delegating now or in this
load) appends KEEP_ON_COMPACT: keep the user's goals and decisions, the tasks and what each needs,
what was accepted, merged or discarded; drop finished tasks' diffs, reports, transcripts. At a task
boundary in the mode (a task of the last open set gone, Claude idle, none waiting on Claude) it
compacts once usage reaches `WB_COMPACT_AT`% (default `COMPACT_AT` 0.5; 0 off) of the auto-compact
threshold (or the window).

**Status line and pane.** `⚒ wb · <name> <phase> r<round>` for queued/running/testing, `<name>
asks`, `<N> awaiting review` (review, stuck, failed, not parked: `waitsOnClaude`, #541; `mine`
only, #693), joined by ` · `; `⚒ wb · workbench mode` with nothing listed. The pane: a row per task
(marks: spinner running, ◌ queued, ◐ testing, ? asking, ◆ review, ▲ stuck, ✗ failed, ✓ accepted, –
discarded) with its recent lines; the followed task's transcript (lines cut to `LINE_CHARS` 2000,
within `HISTORY_CHARS` 60,000: Claude Code drops pane text past 100,000), following the newest
unless scrolled up; the stage (the apprentice as pixels at 20 rows by 40 columns or more, else its
one-line face; text wrapped by `wrapWords`, as Text's own wrapping beside the raster drew empty
rows); the saving and the tabs. The Stats and Settings tabs and the strip (`AbovePrompt`): *Seeing
what the agent does*. The apprentice (`apprentice.ts`): 16×12 pixel art in half blocks into a
`Raster` (cells as little-endian u32 triplets, base64), a pose per state (rest, blink, think, work,
swing, tada, ask, slump, sleep), FACE the one-line faces; the bubble is `speechOf`.

**The web chat listener** (`listen`: workbench mode, chat on, an events child). Another session's
live claim makes this one stand by: its pid alive and, with a `start`, still started then (`alive`,
from `/proc/<pid>/stat` or `ps -o lstart=` as wb reads it, UTC then the local zone, #848; when
neither can say, alive while listener.json is under 15 s old, #700); without a pid, `ts` within
15 s; asked only of another's claim, at most every `PID_CHECK_MS` 30 s (#839); another `machine`'s
always lives (#922; a crashed one there stands until a session on that machine or a quit clears
it). Otherwise it writes `chat/listener.json` on a change and delivers each
unreceipted message from `open.json` once `wb chat claim <id> --by <key>` grants it (refused:
retried a minute later; two sessions both submitted one, #84): idle → `prompt.submit('[web chat]
<words>', asUser)`, receipt `now`; busy → `session.send` with WEB_NOTE, else a queued prompt
(`queued`, acked `now` when that turn ends: `mem.chatQueued`, `readQueued`); `wb chat ack <id> --by
<key> --how …` once it entered (a dropped prompt is retried a minute later); `chat/away.json` `{at:
now, by: <key>}`. Away is read by every session (`away()`); a typed prompt writes `{at: 0}`
(`back`), as does a terminal dialog answered there after that use (`termDialogDone`). Typed is
`byUser` on the engine's `origin.kind`: `composer`, `bridge`, `sdk`, `auto-continuation`, or an
`unclassified` or unstamped one not `isMeta` (a /loop, another plugin's or a peer's prompt ended away
for everyone, #835). An expired message (claim refuses it) isn't tried again this load
(`mem.chatExpired`; one toast, #536). Quitting stops delivery and empties listener.json if held.

Each message passed on is owed a chat reply (`mem.chatOwed`, `owedAtTurnEnd`, #767: phone
questions were answered in the terminal only for two releases): a `wb chat reply`/`listen` or `wb
shot … --chat` in Bash, a `from: claude` row, or a typed prompt settles it. At the end of the main
turn that had it (`grace` 1 when it was queued behind a running turn or behind a prompt the mod
submitted that poll, `mem.submitted`, #837) with no reply, one OWED_NOTE turn is queued (taken back
out of `mem.queued`, and skipped by `sendQueued`, once nothing is owed, #837); that turn too
ending without one, OWED_POSTED (`wb chat reply --text="Claude answered in the terminal; nothing
was sent here." --to=<id>`) goes to the chat, once per message (`--to`, so an agent's wait gets it, #1042).

**Agents' messages and the turn-end record** (#1042). A message with `role: "agent"` and a name
wb makes (`agentOf`: `[a-z0-9-]{1,32}`) is delivered as the user's are but submitted as `[agent
<name>] <words>` (with `about` and attachments as above) and a last line `[message <id>: answer
with wb chat reply --to <id>]` (`chatPrompt`; mid-turn with AGENT_NOTE), toasts `a message from
agent <name>`, and writes no `away.json` (an agent never makes the user away); a prompt starting
`[agent ` is never the user typing (`prompt.submit`). Each message given to Claude goes into
`mem.chatTurn` with its `grace`, as `mem.chatOwed`; at each main turn's end (`chatTurnEnd`) those
at 0 are recorded with `wb chat ended <ids> --by <key>`, and with none, a turn in which
`chat/messages.jsonl` grew (stat at `turn.start`, `chatTurnStart`; a reply, maybe a later report)
is recorded with no ids. Nothing while the chat is off.

Dialogs (`classic.PermissionRequest`; the terminal's dialog stands meanwhile and the hook's answer
settles it), unless a settings hook decided or the session isn't delegating with the chat on. At
the terminal: noted in `mem.termDialogs` (`{tool, agent, at, kind, text, id}`) and passed on;
`termNotices` runs `wb chat notice --tool=<t> --kind=permission --text=<text>` for one past
`NOTICE_AFTER_MS` while no other notice of the session's is open (a failure: `warnOnce`); the
engine's `tool.call` ending (its permission check and its run) settles the oldest noted dialog of that tool in that
loop (`agentId`, in the hook's `finally`), and `turn.complete` any left: `termDialogDone` drops it,
runs `back` when `away.json`'s `at` is at or after the dialog's, and `wb chat notice --done <id>`.
Away (`awayNow`: `at` within `AWAY_MS` 6 h, or a backlog task of this session's open): `askOnChat`
runs `wb chat ask --tool=<t> --kind=<KIND[tool], else permission> --text=<the input's command,
file_path, url, path or pattern, else its JSON, cut at 1500>` (each `--opt=value`, so a leading `-`
isn't a flag; `--pid=<mem.eventsPid>`, #538; `--detail=` a question's `{questions}` with previews
cut to `PREVIEW_CHARS` 2000 and CUT_NOTE, `askQuestions`, or a plan's `{plan}` (its first
`DETAIL_CHARS` 40,001) else `{plan_file}` from `planFilePath`, with `--session <id> --repo <dir>`,
#253). If it fails, the mod posts `Claude is waiting on a plan|question|permission in the terminal`
(`wb chat reply --to=user`: the user's, in an agent's turn too, #1042), toasts and leaves the dialog (#535). It prints `<id> <state>`: `allow` (the
setting) answers `{behavior: 'allow'}`; `expired` leaves the dialog; else the id goes in
`mem.asksOpen` and `waitOnChat` runs `wb chat wait <id> --timeout 20 --json` (`ASK_CHUNK_S`)
repeatedly, so an interrupt (`next.signal`) shows within 20 s, until `ASK_WAIT_MS` 30 min. `allow`
→ `{behavior: 'allow'}` (a question's with `updatedInput: {...input, answers}`); `deny` →
`{behavior: 'deny', message}` with the note (a plan: revise and present again; else ask the user
there). Anything else writes `wb chat answer <id> expired --by timeout|interrupted` and passes the
event on. Esc drops a waiting hook outright (seen live, 2026-11-18), so `turn.complete` expires every
id left in `mem.asksOpen` by `interrupted`. A failing hook is skipped, leaving the dialog.

**Dialogs in an agent's turn** (#1042, the owner's rule: permission requests go to the human,
never the agent). Each agent's message given to Claude goes into `mem.agentTurn` (id → name);
`agentsFor` names those whose `mem.chatTurn` grace is 0 (the running turn took them), until
`chatTurnEnd` drops them with their turn. While any is named, the `classic.PermissionRequest` hook
sends the dialog through `askOnChat` as away does, even with the user at the terminal, adding
`--for=<name>` for each (`askForArgs`) and, at the terminal, `--here` (their away setting doesn't
answer it); the toast says `… for agent <name> is in the web chat too: answer it here or there`,
and a deny's message names `wb chat reply --to user`. Whichever answers first settles it: the
page's answer is the hook's decision, which settles the terminal's dialog; the terminal's withdraws
the card (`watchAsk`: `mem.chatAsks` `{tool, agent, input, here, at}` by id; at the hook's
`next.signal` abort (an interrupt: a terminal answer doesn't abort it), or when the engine's call
of that tool in that loop with the dialog's input (`sameCall`: every argument both carry equal, at
least one shared unless the call has none, as ExitPlanMode's) ends, `askSettledHere` in the
`tool.call` hook's `finally`; so a long command allowed in the terminal keeps its card until it
ends, 22 s in the live check of 2026-11-23: this engine has no `tool.run` event, and an unknown
event stops the module loading), `withdrawAsk` writing `wb chat answer <id> expired --by interrupted`, which the page
shows as "settled in the terminal"; one asked with `--here` and settled so runs `back` when
`away.json`'s `at` is at or after the ask's (its push tapped), as `termDialogDone`. Matched by tool
and loop alone, a Read a rule allowed, finishing beside one whose card waited, withdrew that card,
leaving an away user's dialog in an empty terminal (the review). Away or not, the same withdrawal
applies to every ask (a card left after the terminal answered could still be tapped).

**The weekly model check** (`scoutIfDue`): in a session delegating now or in this load, with wakes
on (another project's session spent the offer, #919; such a session leaves scout.json alone), once
`wb events` said hello, Claude idle `SCOUT_QUIET_MS` (a minute) after a main-loop turn (raised at
once, it buried Claude's question), quiet (`quietForJobs`: no task anywhere active, none of this
session's unparked ones waiting on Claude within `REWAKE_MAX_MS`, #541), once per load, when
scout.json's `done_at` is 7+ days old and `offered_at` 1+ day: it writes `offered_at` and submits
SCOUT_NOTE (`wb scout`, research, recommend keep/add/retire, nothing changed without a yes; GPU and
API alike, #1024: for roles on API `wb scout`'s API section, newer or cheaper models from the same
providers on their own pages, whether thinking would pay for significant, and recommend in chat
only, never `wb route` or an API `--model` unasked; it names no model; the
audio and image engines too, each licence checked and its `notes` kept true, #398; then `wb scout
--done --notes`).

**The weekly update check**: once a load, after the first main-loop turn once wb runs, beside the
poll (a stalled fetch held every wake): `wb update --check --if-due` (origin fetched at most every `UPDATE_EVERY`, a
week; in between counted locally, else `update.json`'s `behind`); `update available` becomes a toast naming `/workbench-update`, once a
session until installed, and so does `local changes in the marketplace clone` (`LOCAL_CHANGES`, `patches.CHECK_LINE`),
which comes alone when nothing is new upstream (only `update available` was toasted, so the warning reached nobody,
#1005).

**The consent gate** (`sendRefusal`, in the `tool.call` hook every call passes, #1005). Nothing goes to workbench
upstream without the user's yes to that very call, in a dialog: Claude's, a subagent's or another plugin's alike, from
Bash, Monitor (its `command` runs in the same shell) or a GitHub MCP tool. A rule in the skill is advice Claude may
misread; this is the one place every call passes. Outward (`sendsOut`):
`gh pr create|comment|edit|merge|review|close|reopen|ready|update-branch|lock|unlock`,
`gh issue create|comment|edit|close|reopen|delete|lock|unlock|transfer|pin|unpin|develop`,
`gh repo fork|edit|delete|rename|archive|unarchive|sync`, `gh workflow run|enable|disable`, `gh run rerun|cancel|delete`,
`gh release create|edit|delete|upload|delete-asset`, `gh label create|edit|delete|clone` (`GH_WRITES`, past
`-R`/`--repo`), `gh api` with a method other than GET or HEAD, or with fields (`apiWrites`), `git push` (past git's own
options, `GIT_VALUED`), and `wb patch send` (`wb` or a path to it, or Python running `…/wb` or `-m workbench`:
`wbArgs`); command words lowercased (macOS finds `GIT` as git). Read as bash reads it (`simpleCommands`, `commandAt` as
in `delegates`, past `sudo`, `doas`, `xargs` and the other `PREFIXES`): echo and heredoc text isn't a command, `$(…)`
and backticks are, and `bash|sh|zsh|dash -c '…'` and `eval` are read inside; a line naming none of `gh`, `git`, `wb`,
`workbench` isn't parsed. Text a shell or an interpreter in the line runs (`RUNS_CODE`: from a heredoc, a here-string,
a pipe, `-c`) is matched by its words (`OUTWARD_TEXT`; `bash <<EOF … git push` once went through). Aimed at upstream
(`upstreamAim`): `wb patch send` always; another outward command when the line names a gated directory (as written,
through `~`, `$HOME` or `$WB_HOME`: `mentions`) or runs in one (`cd`, `cd --` and `pushd` followed from the session's
directory, git's `-C`; by real path too, a symlinked spelling), or runs in Claude Code's plugins directory in a
checkout whose origin is upstream (a marketplace renamed), or when the line anywhere names the upstream's owner/repo
(`export GH_REPO=<slug> && gh issue create` named it outside the outward command), unless that runs in a developer's own
clone of upstream (`developerClone`: `git -C <dir> remote get-url origin`, 5 s, `originIs`; never a gated directory or
inside the plugins directory), which is no user's Claude. The gated directories (`gateConf`): the patch clone,
`patches.json`'s `source`, else `WB_SOURCE`, else `~/.local/share/workbench/source`; `$WB_HOME/upstream/`; and Claude
Code's own `<CLAUDE_CONFIG_DIR or ~/.claude>/plugins/` (and `CLAUDE_CODE_PLUGIN_CACHE_DIR`) `marketplaces/workbench` and
`cache/workbench`: the marketplace install's clone has upstream as its origin, and was once exempted as a developer's
(the review of #1005). The upstream: `patches.json`'s `upstream`, else `UPSTREAM` (`a-schaefers/claudes-workbench`).
A GitHub MCP tool (`GITHUB_MCP`, `mcp__…github…__<name>`) not named `get_`/`list_`/`search_` whose input names the
upstream, or a repository of its name (a fork), asks the same (`mcpAimsUpstream`), its input shown up to `MCP_CHARS`
4000. With the user away on the web chat (`away`) it denies at once, as the dialog would wait at an empty terminal
(`NOT_SENT_AWAY`: `workbench: not sent: the user is away: it waits until they are at the terminal. …`). Else `$.ui.ask`
(`askToSend`): `Claude wants to send this to GitHub (<slug>), publicly: <the command>`, the whole line up to
`SHOW_CHARS` 600, a longer one as the outward command in full, then the line's start (`shownCommand`: cut from the
start, a long line hid the gh command and its body); for `wb patch send` its `--title` and the body file's first
`BODY_LINES` 20 lines (`sendQuestion`, `sendArgs`: the last `--title`/`--body-file` given, as argparse takes them; wb
refuses abbreviations). A send it can't show is refused without asking (`NOT_SHOWN`: no slug, `--title` or
`--body-file`, a `$` or backtick in either, an unreadable body, or a send inside text another program runs). The
options `Send it` and `Don't send`. `Send it` lets that one call through, and for `wb patch send` writes
`$WB_HOME/send-ok.json` (what wb checks under Claude Code: *Sending a patch upstream*); nothing is remembered (a yes to
one pull request is no yes to the next). Anything else, a failed dialog too, denies: `NOT_SENT`, `workbench: not
sent: the user did not approve it in the dialog. Tell them it was not sent; try again only if they ask for it.` (it
once said to show them and ask, and Claude opened a dialog of its own pleading for the yes just refused). A live check
of the gate runs the real gh form with a fake gh on PATH: an `echo` of it is text, never asked about. No `$.state`
atom.

**The roles' dialog** (`routeRefusal`, beside `sendRefusal` in the same hook, #1014; *Roles*). In Bash and Monitor
commands, read as above (`runsIn`: `simpleCommands`, `bash -c` and `eval` inside; `wbArgs`), each `wb route …` and each
`wb start … --m…` (any abbreviation of `--model`) is a call (`routeCalls`); a show (`wb route`, `--json`) and a dry run
change nothing. For the one call, the mod runs `wb route --dry-run --json <its args>` (a start's as `--start <wb start's
args>`, parsed there by wb start's own parser: the task's name and an abbreviated `--model` as wb takes them), 60 s.
A dry run that exits non-zero (wb refuses the change itself), prints nothing gated (a GPU model pin, a GPU `--model`, the Agents switch to GPU)
or can't run lets the call through: wb refuses a gated change under Claude Code without the approval anyway. Else, away
on the web chat it denies at once (`NOT_CHANGED_AWAY`: `workbench: not changed: the user is away: say in the web chat
what you'd change; they approve it in the dialog once back.`); at the terminal `$.ui.ask(<the dry run's change line>,
['Allow', "Don't"])`. `Allow` writes `$WB_HOME/route-ok.json` (`{...approval, at}`, the dry run's `approval`: `{argv,
change}` or `{start: {task, model}, change}`) and lets the call through; anything else denies (`NOT_CHANGED`:
`workbench: not changed: the user did not approve it in the dialog. Tell them it was not changed; ask again only if
they ask for it.`). Refused without asking (`ROUTE_OWN`, `run wb route (or wb start --model) as a command of its own`):
two calls in one line, and one inside text another program runs (`ROUTE_TEXT`, with a `RUNS_CODE` program in the
line). The file tools (`EDIT_TOOLS`) on routes.json (`routesPath`: `WB_ROUTES`, else `~/.config/workbench/routes.json`,
by real path too) or `$WB_HOME/route-ok.json` are denied (`routesFileRefusal`, `ROUTES_EDIT`), and so is a command
naming `route-ok.json` with the state directory (as written, through `~`, or `$WB_HOME`: `ROUTE_OK_OWN`); a `grep` for
the name in the code passes. A speed bump, not a wall: a command that builds the path gets past it (*Roles*, the gate).

**The roles in the conversation.** `WbSaid.routes` (the `said-3` atom) is the roles' summary as the mod reads
routes.json (`readRoutes`, `routesSummary`, which says what `routes.summary` says: Couplings) and the cap (`. Day cap
$N`: in what is compared, so a cap changed alone is a row too; it once wasn't, 2026-11-23), '' while every role is
the default and no cap is set (nothing to tell), `routes.json can't be read (wb route says why)` for a file wb wouldn't read
(`routesValid`); read in `wanted` while delegating or with a task open. `unsaid` pushes `[workbench] The roles changed:
<summary>. <ROUTES_RULE>` whenever it differs from what was said (the first time `ROUTES_NOTE`), so any change, the
dialog's, the pane's, a terminal's or a hand edit, reaches the conversation as one row. `prompt.context` adds the
`workbench-routes` block (delegating, or a task open, as `wanted` reads) once any role has an API model (a GPU pin alone
adds none): `Roles (the user's, wb route): <summary>. Day cap $5. Never pass --model for an API model or change a role without the user's yes (the mod asks them in a dialog);
recommend only.` (`No day cap yet.` without one). The `/workbench` skill's text carries it the same way (`unsaid`).

**Tests** (`register.test.tsx`: an in-memory filesystem, a fake `wb events`). Not covered: check-in
wakes, the one-hour cutoff, scrolling, `WB_COMPACT_AT`.

**Troubleshooting.** With `wb` missing from Claude Code's PATH or failing, the mod toasts once
(`can't run \`wb events\``, `wb status failed`) and runs on from the task files: set `WB_BIN`,
which moves only the mod's calls (Claude's Bash still needs `wb` on PATH). `claude plugin validate
mod` lists hooks, calls and env reads; `claude --debug` shows hook errors.

## Waiting without polling

`workbench/watch.py`. Anything that waits for a file or a process sleeps in the kernel.

- **`Watcher(*dirs, recursive=False, skip=())`**: inotify through ctypes (`inotify_init1`,
  `inotify_add_watch` from `CDLL(None)`, so musl works) for modify, attribute, close-write, create,
  delete, moves. An overflow or a vanished directory reports every watched directory changed;
  recursive, an overflow first rewalks each root (`add(root, again=True)`, #472). `recursive` adds
  new subdirectories, never through a link, and leaves out one it can't list (#439); `skip` matches
  patterns against the path below the root. `wait(timeout, settle=0.02)` returns changed paths raw
  (duplicates, temp and lock files). A directory past `max_user_watches` is stat-polled every
  `POLL_S` 0.25 s. Without inotify (macOS, or `inotify_init1` failing) `wait` returns within a second
  whatever the timeout, so every caller loops and re-checks: callers use `wait()`, not the
  descriptor; the chat `Hub`, which selects on it beside a pidfd, switches to `wait()` when any
  directory is polled.
- **`wait_exit(pid, timeout)`**: `pidfd_open` and `select`; without pidfd (macOS, kernels before
  5.3) `kill(pid, 0)` every 0.1 s.
- Writers help: `store.write_json` renames a dot-temp file, `read_jsonl` stops at the last newline
  (a half-written row waits), `append_jsonl` holds a lock file.

Every waiter and its bounded wake (a timeout to re-check what the kernel can't signal):

| Caller | Waits on | Re-checks every |
|---|---|---|
| `wb events` | `$WB_HOME`, recursive, skipping `EVENT_SKIP` (`tasks/*/worktree`, `tasks/*/tree`, `tasks/*/rounds`, `rules`, `imports`, `images`, `audio`, `archive`, `chat/audio`, `bench/.?*`, `bench/runs`, `chat/uploads`, `bench/cases`, and `SCRATCH`: `.?*`, `wb-index-*`, `wb-merge-*`, neither watched nor reported: they took 2 watches to 605, #373, and 1,209 of 1,211, #439) and the tasks already closed when it starts | 10 s (exits when its parent changes); every minute it reaps dead runners |
| `wb wait` | the state directory and the named task's directory | 60 s (a dead runner: `reap`) |
| `wb log -f` | the task directory | 30 s |
| `wb watch` | the task directory | 1.5 s on a terminal while the task is live, 30 s otherwise; the session exported again only on a change or every 30 s while live (#555) |
| the runner's steer bell | the task directory (`steer.jsonl`) and `ask/` | 5 s, while OpenCode runs |
| `ask_reviewer` | `ask/` | 30 s (`STEP_SECS`), until the ask timeout |
| the chat `Hub` | `chat/` and the listener's pidfd (without inotify: `kill 0` each second) | 60 s |
| `wb stop` / `takeover` | `wait_exit(pid, 15)` | |
| `wb top` | `select` on its nvidia-smi stream and, on a terminal, the keys | each tick (`--every`), when it also checks its parent |

`wb events` prints `{"hello": <its pid>, "start": <its start>, "parent": <its parent's pid>,
"parent_start": …, "machine": <store.machine()>}` (the listener's identity and its Claude Code
process, #687, #922), then `{"changed": [paths relative to $WB_HOME]}` per batch, dropping names
ending `.lock`, `.tmp`, `runner.log` and dotfiles; it creates `tasks/` and `chat/`, and exits on a
broken pipe or when its parent process changes.

## Every `wb` command

`workbench/cli.py` is argparse: one subparser per command, `set_defaults(f=cmd_<name>)`, dispatched
as `a.f(a)`; a new command is a parser block plus a `cmd_*`. `die(msg, code=1)` prints `wb: <msg>`
on stderr. Exit codes: 1 an error; 2 a usage error, or `wait` on a closed task, with no open task
or at a timeout; 3 no model can run (`start`); 4 the GPU held by a task the new one can't run
beside (`start` without `--queue`); 5 an accept whose merge failed; `scout --due` 0 when due, 1 when not;
`doctor` 0/1. Errors wb raises (`RuntimeError`, a config that doesn't parse, an unreadable file:
`wb: <path>: No such file or directory`) print as a message, never a traceback. Any other exception is a bug:
its traceback, then `wb crashed: a bug in workbench. Claude can fix it for you as a local patch (wb patch) and offer it
upstream (<root>/CONTRIBUTING.md)`, exit 1 (*Local patches*); in-process (`main(argv)`, the tests) it propagates, as
a crash caught as `SystemExit` passed ~160 refusal tests (#1005). `KeyboardInterrupt` and `SystemExit` pass through. cli writes
notify.jsonl only in `reap` (`_restart_notice`) and `wb update --stop` (`_stop_for_update`); the
runner writes the rest (#882). `wb --version`: `workbench.__version__`.

**Acknowledging.** `_ack_all(task)` moves the task's `acked` mark, never back, under the task's `ack`
lock (the one `claim_ack` takes; unlocked, a `wb ack` between read and write was written over and a
question delivered twice, #786). With an unacked notice of the kind the phase stands for
(`question` while `asking`, `checkin` while queued/running/testing, else the phase name), it stops
just before the oldest, so unseen news stays deliverable (else to now) (a `wb note` once swallowed a failure for
an hour). Called by feedback, next, answer, allow, note, resume, takeover, accept, discard, stop;
not by steer, `skill hand|take`, diff, report, log. `upto=t` (answer, allow answering a request,
feedback, next, resume) moves it to when the command acted, still stopping before another open
question (`ask_mcp.is_open`). `feedback`, `next` and `resume` act on a task one at a time
(`one_at_a_time`), refuse a gone worktree (`_need_worktree`: `wb discard <task> --keep-branch`), and
mark notes the last runner never delivered as taken (`_stale_notes`, *Notes*).

### Running tasks
| Command | Flags (defaults) | What it does |
|---|---|---|
| `wb start <task> --spec <file\|->` | `--repo` (.), `--role significant\|trivial\|writing` (from `--size`: trivial, else significant), `--size trivial\|significant` (from `--role`: trivial for the trivial role, else significant; both given, the role picks the model and the size the slice and the GPU router's size), `--model <id or fragment>`, `--test <cmd>` (granted to the shell too: `context.test_allowances`), `--auto-fix N` (2 on the GPU, 1 on API: `cli.AUTO_FIX`, `cli.auto_fix`; a given value is kept and the start line prints the one used, #1017), `--self-review`/`--no-self-review` (the `self_review` knob), `--checkin MIN` (0 off), `--ask-budget N` (2), `--ask-timeout S` (1200), `--round-timeout S`, `--silence-timeout S` (unset or 0: measured budgets), `--think-timeout S` (0 off), `--test-timeout S` (900), `--unload-for-tests` (task.json `unload_for_tests`: every model on the GPU but `keep_loaded` let go before each test run, the GPU held meanwhile, the GPU held from other tasks while tests run, a remote model's tests too, #428, #943, its device nodes in the lane, `context.gpu_nodes`), `--no-sign` (task.json `sign: false`; without it `gitops.sign_problem` and `gitops.presence_signer` are said at start), `--no-base-check` (`no_base_check`; else `--test` runs once on the base first, *The base check*), `--base REF` (HEAD), `--skill NAME…`, `--allow PATTERN…`, `--allow-path DIR…`, `--setup CMD`, `--setup-timeout S` (1800, `setup_timeout`), `--copy PATH…`, `--sibling PATH…` (*Setting the worktree up*), `--force` (queue a `--model` that can't run now), `--queue` (wait behind the GPU's holders, or for an API provider's slot), `--trial`, `--local-only` (task.json `local_only`: an API model is refused for this task, as for a repo commands.json marks local-only, *Privacy for API roles*), `--backlog NAME` (task.json `backlog`; a finished batch's name, its newest start `BACKLOG_GAP` 6 h old, is refused with a dated one, `cli._backlog_done`, #880), `--no-claude-md` | Refuses before writing anything: a bad or reused name (`^[a-z0-9][a-z0-9-]{0,47}\Z`), a spec not strict UTF-8 (`cli.read_text`, #873), a repo `_lane_fits` refuses (home, where credentials live; a snap `--test`), opencode missing or 1.x (exit 3, `runner.OPENCODE_GET`, #654), no runnable model (exit 3), an API model (a `--model`, or the role's side as it runs, `routes.effective_via`) in a local-only spot, before any model is picked (exit 3, `cli._local_only_check`, #1021), an unreadable routes.json (exit 3, *Roles*), a role that can't run (exit 3, one line, never the other side: an API role's model without a key, gone from the catalog or out of credit, `routes.cant_run`; Agents on API with a role that has no API model; a GPU role with no GPU model, the router's line; a pinned GPU model not found; *A task, start to finish*, #1016), an API model with no price or today's spend at the cap (exit 3, `meter.refusal` in `routes.resolve`, both API paths: a `--model`'s before the dialog, *Spend*, #1019; no cap at all is #1014's to refuse, and the guard stops such a task, `meter.NO_CAP`), a `--model` naming an API model under Claude Code without the dialog's yes (exit 3, `cli.start_gate`, #1014), a task's worktree as repo, a `--base` naming no commit, no git identity, no committed or gitignored CLAUDE.md (or a package's: task.json `subdir`), a `_worktree_setup` problem, a noexec state dir (`store.noexec`), no disk room (`cli._disk_room`), a clashing branch (`gitops.branch_clash`, `case_clash`), the GPU (exit 4; with routes.json `busy` api a role with an API model starts on it instead, `cli._busy_api`, #1023), an API model's provider already running `API_PARALLEL` (3) tasks (exit 4, `cli._slots_full`; `--queue` waits for a slot, #1015). Writes `starting.json` (pid, start; undone by the next start or `wb gc` if killed, #376), round 1's prompt (spec + PROTOCOL, a writing task's `WRITING_PROTOCOL`) before task.json (a failed write leaves a dead start for gc, not a task with no status), task.json (`session` from `CLAUDE_CODE_SESSION_ID`, `claude_pid` from `CLAUDE_PID`), status; spawns the runner, which makes the checkout (*A task, start to finish*, #529). Notes (`_missing_dirs` with `INSTALL_DIRS`, `context.home_gap`, `context.mise_trusted`, `runner.dropped_proxies`, `context.on_windows`, `cli.WSL_SLEEPS`, `context.fixed_prompt` at `FIXED_SHARE` 40%, `runner.untested`), the model and why (an API task's `model: <id> · API (<provider>) · <role> role, set by you MM-DD · thinking off`, `routes.model_line`; its `started … on <id> (API)`; then `cli._api_notes`: the first API task in a repo for a provider adds the where-note, `note: this task sends the repo, its spec, CLAUDE.md files … to <provider> (the <role> role's model; data policy: <url>)`, its OpenRouter backends task.json's `order`, and `context.api_hidden_note`, *Privacy for API roles*), `next: wb wait <task>`. A writing task (task.json `role: writing`, *The writing role*) gets self-review off unless `--self-review` (`cli.self_review`), probe off (`experiments_off` `a writing task`), the writing handbook, and `--test` stays optional (no note asks for one). |
| `wb feedback <task> --text "…" \| --message <file\|->` | `--fresh` \| `--keep-context`, `--spec-gap "…"…` | Reaps first. Review/stuck/failed with no live runner; refuses a moved repo (`wb relocate`), OpenCode 1.x (exit 3, `_opencode_again`, #866), an API task whose model left OpenCode's catalog (exit 3, `_model_still_listed`, #1016; next and resume too), an API task whose repo became local-only (exit 3, `_local_only_since`, #1021), empty text. Prompt: `FEEDBACK_HEAD` + "Make exactly these changes, then re-run the acceptance command and finish with a short summary:" (re-run only with a test) + text; reviews+1. Warns past three reviews. Gathers lessons. |
| `wb next <task> --spec <file> -m "…"` | `--include` (bulk additions, large binaries, ignored files the agent wrote), `--no-sign`, `--test` (replaces it, granted to the shell too), `--skill` (replaces the set), `--fresh`, `--allow PATTERN…` (adds to the grants), `--spec-gap`, `--take-claude-md` (*Claude's files*) | Review only; an API task whose repo became local-only exits 3 (`_local_only_since`, #1021). Commits the step (`<m>` + `Workbench-Task: <task> (step N)` + `OpenCode-Session:`; an empty step advances uncommitted, #507); prompt "Step N of this task is done and committed (<sha>). The next step:" + spec + the task's protocol (`cli.protocol_for`: a writing task keeps `WRITING_PROTOCOL`); advances round, steps, step_round, `reviews` reset, `step_base` the worktree's HEAD (#652), phase queued in one status write right before the spawn (#191: written earlier, an interrupt left the step under review with no runner). A retry after its commit landed (`cli._step_landed`) measures from `HEAD~1` (#85). Says `runner.fresh_reason`'s verdict. Gathers lessons. |
| `wb answer <task> --text "…" \| --file <f>` | `--id` | `ask/<id>.answer` via `ask_mcp.write_answer`; the id into status `answered`. Refuses empty, over `ask_mcp.FILE_MAX` 256 KiB (`too_long`: never read, the agent waited out its timeout, #605), or closed, racing too (`answer_once`, #938). |
| `wb allow <task> ["<pattern>"…]` | `--path DIR…`, `--always`, `--text "…"` (the whole answer checked against `FILE_MAX` before any grant, #605) | Into `rules/<task>/grants.json` (`--always`: commands.json, once `context.commands_conf` passes); answers the oldest permission request (else the question) "Claude allowed … for this task. Run it again now and carry on." (`from now on` with `--always`), sent to a live agent as a `--now` steer. `--path` through `_lane_dir` (no link; not what `lane_refusal` or `kept_out` refuses, #912; not in the task's repo or its git dir, where a planted hook runs outside the lane; a missing path named like a file refused, #670; made if missing); `*` turns the lane off. |
| `wb progress [on\|off]` | `--after MIN` (default the task's typical length on its model; refused with `off` or while off, #602; `0` clears) | The opt-in to *Progress checks*. |
| `wb steer <task> --text "…" \| --message <file>` | `--now`, `--step` (opens with `STEP_NOTE`: a `smaller` intervention after a progress check, else a `hint`) | Queued/running/testing/asking with a live runner only. steer.jsonl, delivered when the run ends, before the tests; `--now` at once. |
| `wb note <task> --text "…"` | | A `captain` event. |
| `wb splash [on\|off]` | | `$WB_HOME/splash.json`: the pane's opening (#963). |
| `wb experiment [<name> [on\|off]]` | | *Experiments* (`cli.experiment_lines`, `context.EXPERIMENTS`, use over `USE_RECENT` 5 rows, `cli.use_recent`); sets `experiments.json` for later tasks; old probe levels `context.OLD_LEVELS` mean on; `; on is recommended` while `context.PROBE_RECOMMENDED`. |
| `wb workflows [on\|off]` | | *Dynamic workflows* (#1041): `$WB_HOME/workflows.json`; off by default. |
| `wb coding [only\|ask\|decide]` | `allow <task> --edit "…" --said "…" [--when "…"]`, `used <task> --did "…"` | *Claude coding*: the mode and allowances; `allow` records the user's yes for one task until it goes back to the agent; `used` what Claude changed (exit 1 with none). |
| `wb shot <task>` | `--capture CMD` (required), `--viewport WxH` (1280x800), `--files PATH…`, `--timeout N` (300), `--chat`, `--setup` | *Screenshots of the base and the candidate*. |
| `wb takeover [<task> \| --all]` | `--repo DIR`, `--session ID`, `--all-sessions`, `--user` (the user's ask, recorded) | Stops the runner: phase `review`, status `taken_over`. Refuses (exit 1) an unfinished checkout (the hand-back remade it and lost Claude's edits, #655). Outside "Let Claude decide" needs the user's yes, else exit 3. `--all` takes the rest past a refusal (#601), skipping unready checkouts (#918). Scope (`_scope`, for stop, resume, wait, status too): from Claude Code's Bash without `--repo`/`--session`, the caller's (`cli._caller`: every id in `claude-sessions/<CLAUDE_PID>`, and its directory, #693), else `--all-sessions` or a terminal: every session's; by `session` id wherever it ran, or by directory (`repo`, package, `cwd`; real paths, case-folded where the file system is; never home or `/`) when no `session`, no `--session` given, or its `claude_pid` is gone (#695); a task with neither key is everyone's. Others: `left alone (other sessions'): …`. |
| `wb verify <task>` | `--test CMD`, `--files PATH…`, `--expect TEXT`, `--timeout N` (300), `--setup` | *Verifying a fix against the base*; refused while the agent runs. SIGTERM/SIGHUP end it cleanly (`cli._ends_cleanly`, #372), like `wb image`, `wb audio`, `wb bench add`, `wb bench self` (`media.run_group`: the child's whole process group killed, since killing only a wrapper left its worker holding the GPU, #589). |
| `wb stop [<task> \| --all]` | `--repo DIR`, `--session ID`, `--all-sessions`, `--park-waiting` | Status `stopping`, SIGTERM (15 s, `wait_exit`, then SIGKILL to its session and the agent's), `runner.sweep`, phase `stuck`, acked; worktree kept. `--all` (`stop_everything`): a `stopped` event, status `parked` (`store.parked`: out of `wb status` and the mod's counts until `spawn_runner` or a takeover clears it, #541); `--park-waiting` (with `--all`, the mod's quit) parks review/stuck/failed too, acking their news (`_park_waiting`, #698); unscoped, `bench.stop_driver` first (another host's left, #876). Others: `left running (other sessions'): …`. |
| `wb resume <task> [<task>…]` | several names (start order, failures said, exit 1, #692), `--all` (`last` in `cli.INTERRUPTED` or starting `runner.UPDATED`: `cli._interrupted`, #788; never a guard's stop or a crash; in `store.all_tasks` order, each under its command lock, #544; a worktree problem or an API task whose repo became local-only is said and skipped, the rest go on), `--repo DIR`, `--session ID…`, `--all-sessions`, `--backlog NAME` | Reaps first. Stuck/failed, no live runner; an API task whose repo became local-only exits 3 (`_local_only_since`, #1021). Round n+1 on a fresh session (`runner.RESUME`), undelivered notes as `NOTE_HEAD`. Never ran a round: round 1 again (`_early_notes`); checkout cut short: remade (`_rechecks_out`). |
| `wb accept <task> -m "…"` | `--include`, `--no-sign`, `--merge`, `--merge-as merge\|ff\|squash` (implies `--merge`; commands.json `repos` `merge` sets the default, without implying it), `--force` (any open phase; stops a live runner), `--spec-gap`, `--take-claude-md` | Reaps; a moved repo refused first (#498). Review only; a closed one only when status `unrecorded` (`_finish_record`, #88). A live task with nothing changed on its first step is refused before its runner stops; a failed commit leaves it `stuck`, not live with no runner. Commits `<m>` + "Delegated to <label> via OpenCode: R round(s), F automatic test-fix, V review round(s)." + `Workbench-Task:` + `OpenCode-Session:` (no AI co-author line); a retry past it (`cli._accept_landed`) measures from `HEAD~1` (#879) and puts `delegate/<task>` on that commit (`gitops.put_on_branch`, refused when the branch holds commits it lacks, #417). Removes the worktree and runtime dir, records `tip`, keeps base..tip in `steps.bundle` (`gitops.bundle_steps`; `refs/workbench/accepted/<task>`, `gitops.keep_accepted`, only if it fails, #799, deleted once a merge lands the tip, else by gc). `--merge`: *Git, commits and projects* (`gitops.merge_lock`; failure: the accept stands, exit 5 `NOT_MERGED`, #247). A retry resumes from status `closing` (#246) while the worktree is gone or clean (`cli._gained_work`); meanwhile (`cli._accepting`) discard, feedback, next, resume and takeover refuse it (#878: discard recorded committed work as discarded); a merge already made is found by ancestry (`gitops.contains`), a squash by `closing`'s `merge_from` (#432). Writes the row, drops the rules (`_closed`), says how far CLAUDE.md is behind and the `claude:` cost. |
| `wb relocate <task> --repo <dir>` | | Refuses a repo without the base or a linked worktree; refuses a `.git` naming another git dir; repairs the link (`git worktree repair`), rewrites `gitdir.json`, task.json `repo`, `git_common`, `cwd`, `submodules.json` (`gitops.relocate`, #498, #682). |
| `wb discard <task>` | `--reason "…"`, `--spec-gap`, `--keep-branch`, `--drop-steps` | Stops, records, removes worktree, runtime dir, rules (`_closed`) and branch (`deleted (was <sha>)`, status `tip`, in the repo `gitops.repo_of` finds), not with `--keep-branch`, a moved repo, or committed steps unless `--drop-steps` (#494); a cut-short checkout's only while `branch_made` holds (#416). Closed: refused unless `unrecorded`. |

### Watching
| Command | Flags | What it does |
|---|---|---|
| `wb wait [<task>]` | `--timeout S` (0 none); unnamed: `--repo DIR`, `--session ID…`, `--all-sessions` | Reaps, blocks (inotify), prints `[<kind>] <task>\n<text>`, claiming it (`cli.claim_ack`: one waiter or the mod wins, #696); skips notices no longer true. Named: closed exits 2; taken elsewhere "(already delivered once)"; no notice `[<phase>] <task> (no notice: <last>)`. Bare, no open task: exit 2. |
| `wb status [<task>]` | `--all`, `--repo DIR`, `--session ID`, `--all-sessions` | Reaps. A line per open task; `?` when its runner died within `REAP_GRACE`; a `via` column (`gpu`, `api`) before the model, 24 wide (`cli.status_model`: an API task's `provider/model`, the model part cut, the provider never; a GPU task's last segment, #1016). Named: `worktree:`, `cli.use_line`, `experiments_off`. Parked tasks fold into one `parked by wb stop --all …` line without `--all` (#541); scoped, `open elsewhere (other sessions'): …`. |
| `wb watch [<task>]` | `-v`, `--once`, `--no-color`, `--json` (hidden, the pane's), `--last N` (hidden) | `transcript.follow`; default the latest open task, never a bench replay (#555). The header's `routed:` line, and with `--json` a `routed` row after `meta` (`transcript.routed_text`, #1016). |
| `wb log <task>` | `-n N` (0 all), `-f`, `-v` | The events, one line per `transcript.EVENT_KINDS` kind (#861); `-n` counts printed lines, `step` rows dropped first (#861); `-f` follows until it needs Claude, reaping, flushing (#90, #545). |
| `wb report <task>` | `--round N` | summary.md. |
| `wb diff <task> [paths…]` | `--stat`, `--all` | Against the base or the last step (`--all`: the branch); worktree gone: the git command up to `tip` (#477). |
| `wb top` | `--json`, `--every S` (0.2 to 3600; stats.json `every`, else 2) | GPUs, tasks, models, the lease queue. Never a model call, load or export. |
| `wb adopt <task>` | `--session ID…` (required), `--pid PID` (`CLAUDE_PID`, else wb's parent) | The mod's (#687): a task whose Claude Code process is gone becomes the claimer's (`session`, `claude_pid`, `claude_start`, `claude_machine`); exit 1 while it lives or ran elsewhere (#922). |
| `wb ack <task> <ts>` | `--if TS` (roll back an undelivered wake: `cli.release_ack`) | The mod's, before a wake (`cli.claim_ack`); exit 3 when taken first (#786). |
| `wb events` | | `{"hello", "start", "parent", "parent_start", "machine"}` (`store.machine`, #922), then `{"changed": [paths]}` on `$WB_HOME` changes (not worktrees, rounds, closed tasks, rules, imports, media, bench, link targets, logs, locks, temp or dotfiles); reaps each minute; exits with its parent. The mod's ears. |

### Models, skills, lessons, notes
| Command | Flags | What it does |
|---|---|---|
| `wb models` | `--set-default`, `--json`, `--api TEXT` | State, slice, speeds, `bench:`, `size:`, `router.weights_changed`; the ranking, models under `router.EXPLORE_TRIES` marked (*Exploring*); the API section, and `--api TEXT` the catalog ids containing TEXT, at most 30 (*What `wb models` prints*). `--set-default` (a GPU model only): `models.set_default` (refused under `OPENCODE_CONFIG_CONTENT`, #421). `--json` (#1023, the pane's lists, `cli._models_json`, asking no server and rebuilding no catalog): `{gpu: [{id, label, window, sizes, cards}], api: [{id, name, provider, provider_name, window, price {input, output}, free}] (stored keys only), used (API models tasks ran on), free_line, optimize {done_at, models, running}, media {image\|speech\|sfx\|music: {default, engines: [{name, gpu}]}} (#1047; a file that doesn't parse leaves its kinds out)}`. |
| `wb model add <provider/model>` | `--pull`, `--label`, `--context N`, `--url URL`, `--start CMD`, `--stop CMD` | *Managing the set*: `--url` via `models.add_server_provider` (an existing provider never touched, one for another baseURL refuses; off this machine needs `local: true`); `--start`/`--stop` to models.json; refuses Ollama cloud models (API models) without `local: true` (one under a plain name always), unknown contexts, down servers. |
| `wb optimize [start\|estimate\|report\|stop\|apply] [<id>…]` | `--bench`, `--if-due` | *Optimize*; `start` detached, refused while a task holds the GPU, stopped by a task that wants it; `--bench` then runs `wb bench run`; `apply` adds the measured window as its own entry (`optimize.entry_name`; refused when that name exists). |
| `wb remote <host>` | `--port N` (11434), `--context N`, `--remove` | Provider `ollama-<host>`, `local: true`, its tool models added (IPv6 bracketed, #707); exit 1 when it doesn't answer; `--remove` not while `model`/`small_model` is one (#708). |
| `wb model retire <id>` | `--delete` | Not OpenCode's `model` or `small_model` (#708); keeps `variant_of`, `replaces` (#723); `--delete` drops Ollama's weights; an API id refused (`cli.API_SIZES`, #1013). |
| `wb model rename <old id> <new id>` | | models.json `renamed_from` (#818); refused while the old id is configured, or for an API id. |
| `wb model sizes <id> [trivial] [significant]` | | The sizes it's routed; another action's flag refused (#602); an API id refused: `sizes are for GPU models; API models run by role`. |
| `wb route [<role> gpu\|api\|<provider/model>] [<role> <choice>…]` (several roles, one change, #1024) / `wb route all gpu\|api\|both` (the Agents switch, `use`) / `wb route cap <usd>\|off` / `wb route busy wait\|api` (#1023) | `--gpu ID\|auto`, `--api ID`, `--thinking on\|off`, `--day-usd N`, `--order NAME[,NAME]` (an OpenRouter role's backends, `privacy.parse_order`; an OpenRouter model set or switched to without them exits 3, `privacy.order_refusal`, #1021), `--json` (show: the roles, `busy`, `summary`, each API model's `api_status`, `spend_today`, `providers`; with `--dry-run`: `{change, gate, approval}`), `--dry-run` (the change line, nothing written, no approval needed), `--start ARGS` (with `--dry-run`: the dialog line for `wb start ARGS` on an API `--model`, the mod's; exit 3 with `API_NOT_YET` while `cli.API_RUNS` is off) | *Roles*. No role: shows them. A change prints `changed: <line>` (`unchanged: <summary>` for none); exit 3 for an API id not in the catalog, a GPU id, one with no key wb can hold, `api` with none kept, the first API role with no cap, and under Claude Code a gated change the mod's dialog didn't approve (`route-ok.json`). A GPU pin needs no dialog. |
| `wb scout` | `--done [--notes "…"]` (`--notes` alone refused, #602), `--due` (7 days), `--watch "…"`, `--unwatch N` (1-based) | The weekly facts: `cli.gpu_line`, `cli.ollama_line`, `cli.server_lines` (`models.SERVER_COMMANDS`, `models.SERVER_PORTS`, `NEEDS_KEY`, `models.WSL_NAT`, `models.server_key`), `audio.coverage`, `cli.image_server_lines`; the API section (`cli.scout_api_lines`: roles, API records, spend, a count per provider, #1024). |
| `wb skill list \| show <n> \| scan <n\|path>` | | The library, a skill's history, the scan. |
| `wb skill add <n> --from <src> -m "…"` | `--source "<origin>, <license>"`, `--lesson ID…`, `--reviewed RULE…` (never `scan.UNREVIEWABLE`) | Check, scan, copy, commit. |
| `wb skill patch <n> --old … --new … -m "…"` | `--old-file`, `--new-file`, `--lesson` | An exact, unique replacement, then as add. |
| `wb skill import <url\|owner/repo\|path>` | `--path DIR`, `--as NAME` | Into `imports/`, scanned; never adds. |
| `wb skill retire <n> -m "…"` / `restore <n>` | | To and from `retired/`. |
| `wb skill hand\|take <task> <n…>` | | From the task's next run. |
| `wb lessons` / `wb lessons close <id…> -m "…"` | `--task`, `--all` | Open lessons, recurring first (12 unless `--all`). |
| `wb notes` / `add "…"` / `drop N…` | | `notes.md`: at most 20 `- ` lines of 300 characters. |
| `wb bench add <task>` | `--step N` (the last), `--hidden PATH…`, `--check CMD`, `--name CASE` | *Bench*. Refuses what no replay could pass (no test or hidden checks, ones the base passes or its commit fails). |
| `wb bench rm <case…>` | | `bench.remove`; rows stay; refused for an unknown case or while a run goes (#740). |
| `wb bench list` / `wb bench report` / `wb bench stop` | `report`: `--run ID`, `--label TEXT` | Cases; per label and model results (`local tok`; with an API replay shown `agent tok` and `paid` as `wb stats`', from runs.jsonl's `paid`, #1022; `bench.use_lines`); stop the driver. |
| `wb bench self <case>` | `--yes` (required), `--turns N` (40) | `claude -p` on the case; a row `arm: claude` and the cost ratio. |
| `wb bench run` | `--model ID…` (default: every GPU model that can run and takes the case's size), `--case NAME…`, `--skill NAME…`, `--label TEXT`, `--repeat N` (1) | A detached driver, one replay at a time behind real tasks, preempted by one; refused while another run runs; trims runs past `RUNS_KEPT` 20 (`bench.prune_runs`), `bench.sweep`s; skips what the label already passed or failed (an `error` runs again); an API `--model` refused (#1013). |
| `wb backlog [NAME]` | `--json` | A `--backlog` batch, archived included: per task, then `savings.summary`; `--json` `{backlog, tasks, totals}`, unnamed `[{backlog, tasks, open}]`. Unknown: exit 1. |
| `wb savings` | `--week` \| `--month` \| `--all`, `--json`, `--share`, `--card FILE.svg`, `--price X\|off`, `--api` | `savings.totals`, trials out, then the `api:` line when the period has API tasks; `--json` `{period, tasks, of, claude, self_est, saved, wh, cost, remote, score, price, api}` (the pane reads it); `--api`: the API money by provider and day, trials and bench replays in, `--json` api_totals plus `bench` (*What a task saved*, #1022); `--price` (positive, finite, else exit 1) writes savings.json `kwh_price` (`off`: `set_price(None)`, #609). |
| `wb stats [set KEY VALUE]` / `wb tune` / `wb trial <task…>` | `set recording\|live on\|off`, `set every 1\|2\|5`, `--json` (*The Stats feed*), `--all` | `set`: stats.json; `wb stats`: `stats.summary`, then results by model, kind and role (a row before roles by size; the model column as wide as the longest id, never cut, the role cut to 11) with columns `tasks`, `accepted`, `rounds`, `autofix`, `reviews`, `min`, `claude`; with an API row shown also `kind`, `role`, `agent tok` and `paid` (mean $ a task over the known costs: `-` GPU, `?` none known, `~$0.12` some unknown; `results.paid_cell`), within 120 columns for ids up to 30 (`stats.STATS_MODEL_LINES` 3, #638, #1022); a GPU-only table is as before #1022: `size` (`role` once a writing row shows) and `local tok`; `tune`: the knobs; `trial`: closed tasks' rows marked, lessons dropped (an open task refused: it has no row). |
| `wb gc` | `--days N` (`ARCHIVE_DAYS` 30) | `cli.gc`, writing `gc.json`: archives tasks closed N days (`store.archive`); old handled notices to `notify.old.jsonl` (`_rotate_notices`; `_cap_old_notices` at `NOTIFY_OLD_MAX` 8 MiB); rules of gone tasks (`RULES_GRACE` 1 h); `context.drop_stale_runtime_dirs`; `store.SCRATCH_PREFIXES` scratches (named `<prefix><maker's pid>.<start tag>.<machine tag>-` by `store.scratch`; `wb bench add`'s `.<case>.<pid>.tmp`) `SCRATCH_AGE` old whose maker is gone (`_drop_scratches`, `_maker_alive`: a reused pid with another start is gone, #532; `_scratch_homes`, #589; another machine's after `FAR_SCRATCH_AGE`, as its pid isn't in this process table, #876); half-made starts (`_dead_start`, `_drop_start`: worktree unlocked and removed, and the branch only when `starting.json` says this start made it and it still points at `branch_at`, `delete_branch(at=)`, #416, #376); chat uploads (`chat.drop_old_uploads`); landed `delegate/` branches (`_merged_branches`, `gitops.drop_landed_branch`, never one checked out, `gitops.checked_out_in`, nor one moved past the recorded tip, #930; status `branch_gone`, #686; one squash-merged elsewhere holds commits its target lacks and stays); accepted refs (`_released_refs`, `gitops.drop_accepted`, `accepted_ref_gone`: deleted when the tip is in `base_branch`, else moved into `steps.bundle` first; a `steps.bundle` stays as long as its task, #940); archived tasks past `store.SLIM_DAYS` 90 slimmed (`store.slim_archive`, `SLIM_GLOBS`: logs, test outputs, old exports, shot PNGs with shots.json marked `pruned` first, #821; task.json, status, prompts, summaries, events and asks stay: names stay taken, bench add and the lessons read them); `_drop_leftovers` (old `imports/`, dead `claude-sessions/<pid>`). Then (not in `cli.gc`) `bench.sweep` (#511). Accept and discard run it without the branches once per `GC_EVERY`. |

### Other
| Command | Flags | What it does |
|---|---|---|
| `wb image "<prompt>"` | `--n` (4), `--size WxH` (1024x1024), `--seed`, `--ref <img>…`, `--out DIR`, `--engine`, `--list`, `--default NAME`, `--wait` | *Images*; refuses while the GPU is held (`--wait` queues), an engine off this GPU (`"gpu": false`, another host) runs beside tasks; a bare `list`/`ls` prompt is `--list`. `--default NAME` (no prompt) sets images.json's `"default"` (*The default engines*). |
| `wb audio "<text>"` | `--kind speech\|sfx\|music`, `--n` (3), `--seconds S` (the entry's `seconds`, else `audio.DEFAULT_SECONDS`: 5 sfx, 30 music, 10 otherwise), `--voice NAME` (the entry's first of `voices`), `--ref <clip>…`, `--lyrics FILE\|-`, `--seed`, `--engine`, `--out DIR`, `--no-check`, `--list`, `--default NAME`, `--wait` | *Audio*; `--n` below 1 refused before any lease (exit 2, #592); a clip that isn't audio fails it; GPU as `wb image`; an option the engine lacks is refused. `--default NAME` (no text) sets audio.json's `"default_<its kind>"`; never the listener (*The default engines*). |
| `wb chat [log\|on\|off\|serve\|url\|reply\|listen\|ack\|claim\|pending\|ask\|notice\|wait\|answer\|token\|ended]` | `listen <clip>… [--text]`, `--port` (`chat/port.json`, else 1337), `--bind`, `--host`, `--text`, `--message <file\|->`, `-n` (10), `ack <id> --by --how`, `claim <id> --by`, `ask --tool --kind permission\|plan\|question --text --detail <json> --session --repo --pid --for <agent> --here` (last line `<id> open\|allow\|expired`, the mod parses it), `wait <id> --timeout` (30) `--json` (allow, deny, expired, or `open`), `answer <id> allow\|deny\|expired --by --note`, `notice --tool --kind --text` (its id, or `skipped: …`), `notice --done <id>` (`answered <id>`; exit 1 for no notice); `claim` exit 1: another session's; `reply --to <id>` (`user`: the user's thread; again for more; else what this turn took: `re`, `to`, #1042), `token add <name>` (prints it once), `token list`, `token revoke <name>`, `ended <id>… --by` (the mod's turn end) | The web chat (*The web chat*); `on` refuses, switch off, when something else answers the port (#690); `off` stops only a server `on` started. |
| `wb try` | `--model <id>` (a GPU model's fragment will do; an API id never matches one) | A sample trial (#112): `workbench/try/project/` copied to `~/.local/share/workbench/try/<n>` (not the state dir, hidden from the lane, #164), started as `try-<n>` on `workbench/try/spec.md`, `--size trivial --trial` (so the trivial role's model, an API one when the user put it there, #1016), its test `sys.executable` (#530); Claude's hidden check `workbench/try/check.py <worktree>`; a refusal removes the copy (#161). An API `--model`: exit 3, `<id> is an API model: wb try runs on GPU models only`, before the copy (#1014). A sample on the trivial role's API model (task.json `via`/`role`, #1016) adds `wb try: the trivial role is API (<id>, <Provider>): the sample's code goes there, and its cost counts toward today's $<cap> cap` (`_try_api_line`, #1026). |
| `wb init <dir>` | | git, a first commit, AGENTS.md → CLAUDE.md (#931; nothing while `gitops.under_way`). Refuses home or above, inside another repo (but a home repo nests, #684), a repo git refuses (`gitops.NotARepo`), no git identity, likely secrets or uncovered installed directories. |
| `wb setup` | `--force` (a link pointing elsewhere; never a file) | Links `~/.local/bin/wb` and `skills/workbench`, removes a dead `skills/delegate` link (the mod runs `wb setup` when it finds only that), exit 1 when something is in the way, PATH advice (`cli.path_advice`), the optimize when due, then `first run:` (`cli.first_run`, with the API keys line `cli.api_key_lines`); names no model (*Installing*). |
| `wb update` | `--check` (`update available: …` or `up to date`; on a marketplace clone with local changes also `patches.CHECK_LINE`, `local changes in the marketplace clone: wb update would refuse (Claude Code re-clones it); wb patch init keeps them`, alone when nothing is new; the mod toasts both; also `cli._mod_behind`), `--if-due` (only with `--check`, #602: `UPDATE_EVERY`, `update.json`; never with `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`), `--stop` (`_stop_for_update`: `last` `UPDATE_STOP`, one `stuck` notice per `store.owner`, #694) | *Installing*, *Local patches*. A marketplace install first looks for local changes in its clone (`patches.marketplace_refusal`) and, with any, saves them under `rescued/` and refuses before anything else: Claude Code's update re-clones it and would wipe them (#1005). A clone: `git pull --ff-only` (uncommitted tracked changes refused); on branch `local`, after the fetch, each pull request `wb patch send` opened (`patches.json` `sent`) is asked of gh, `gh pr view <n> -R <owner/repo> --json state,mergeCommit`, at most `GH_SECS` each (`patches.settle_sent`; never with `--check`, which the mod runs on a timer): merged, its merge commit (when gh names one) in the fetched upstream (else nothing yet: `PENDING`), its local commit (matched by ident) is left out of the rebase and the row marked `merged`; closed, at the end once (`closed_lines`, `closed_said`, the patch's sha as it is after the rebase) `your pull request #<n> was closed without merging: kept the local patch <subject> (wb patch check <sha> tells whether upstream fixed it another way)`; open, nothing; not asked (gh missing, offline, a timeout, any other answer) `couldn't ask GitHub about pull request #<n> (<why, scrubbed>): kept the local patch`, never a failure. These lines, and `couldn't check the pull requests wb patch send opened: …`, come after the `updated …` or `already up to date` line, never ahead of it: the mod's `/workbench-update` reloads the plugin only on output that starts `updated` (#1005, 2026-11-22); a refusal after the fetch prints them first, and a plugin step that fails after the branch moved first prints the merged and landed lines too (said then or never, #1005). Then, diverged and behind or with a merged pull request's patch to leave out, its patches rebased in a scratch worktree (`patches.rebase_patches`, `check_move`) before any task is stopped and the branch moved after (`move_branch`), the report `updated clone a..b (N commit(s))` (N: upstream's news only), then `your pull request #<n> merged: dropped the local patch <subject>` per merged one, `landed upstream, dropped from local: <subject>` per patch upstream has, then `N local patch(es) on top of upstream (local)`; a conflict refuses naming the patch, ending `(wb patch check <sha> first: if upstream fixed it another way, drop it)`, its stdout first saying each pull request closed unmerged (`closed_lines`, HEAD's shas: nothing moved; `closed_said` set, so the next run doesn't repeat it); any other branch diverged is refused; on `local` (not `--check`) it writes `patches.json`. At the end, on any clone, each merged pull request whose local commit is gone from `local` is forgotten and its `pr/<slug>` retired (`retire_sent`: `pull request #<n> merged, removed pr/<slug>, its worktree and its body file`), then each `pr/<slug>` of `wb patch upstream`'s (`patches.held`: its worktree under `upstream/`, or its body file there) that landed upstream (`_landed_pr`) goes, worktree, branch and body: `landed upstream, removed pr/<slug>, its worktree and its body file` (`retire_landed`; a worktree with changes of its own is kept, and said). Then `claude plugin marketplace update workbench` and `claude plugin update workbench@workbench`, each step at most 300 s. Refuses while a runner lives (`cli._running`, `_whose`); every refusing check, a patch's conflict too, runs before `--stop` stops anything (#775). The fetch (`_fetch_origin`, the upstream's remote `branch.<name>.remote`, #875) never prompts: `GIT_TERMINAL_PROMPT=0`, `SSH_ASKPASS_REQUIRE=never`, `FETCH_SECS` 60, the user's `GIT_SSH_COMMAND`/`GIT_SSH`/`core.sshCommand` kept, else `BatchMode`. |
| `wb patch init` | `--dir DIR` (else `WB_SOURCE`, else `~/.local/share/workbench/source`: `patches.source_dir`) | *Local patches*. On a clone install a note (`patches.clone_note`), exit 0. Refuses, changing nothing: `claude` not on PATH, a running task (`cli._running`), DIR inside Claude Code's marketplaces directory, a non-empty DIR that isn't a clone of the same origin (or, without `local`, has uncommitted changes), carried changes with no git identity. Saves the marketplace clone's changes (`rescued/`), clones (or fetches into the reused DIR), makes `local` at the marketplace clone's merge-base with its upstream (the code doesn't change; `origin/<branch>` when the clone lacks it), tracking `origin/<that clone's branch>`, then `git am --3way` the commits and `git apply --index` the uncommitted diff, committed as `CARRIED` (`Local changes carried over from the marketplace clone`). A failure removes a DIR it cloned, puts a reused one back on its branch without `local`, and leaves the install as it was. A DIR with `local` already is kept and nothing carried (a rerun goes on from there). Then, from DIR: `claude plugin marketplace add DIR`, `DIR/bin/wb setup --force`, `claude plugin update workbench@workbench`, each failure saying what is done and how to finish; writes `patches.json`; `workbench runs from DIR now, branch local tracking origin/<b>; N patch(es) carried over …`, stashes and other branches named as kept only in `rescued/`, and `/reload-plugins`. |
| `wb patch list` | | A clone: `<root>: branch B, tracking U` (warned off `local`), one row per patch oldest first `<short>  <subject>  [<Scope> \| no Scope trailer]`, ending ` (no test)` for one that touches no test file (`patches.has_test`, `is_test`: under `tests/`, or a `test_*.py`, `*.test.ts`, `*.test.tsx` anywhere; `wb patch check` can't judge it), `uncommitted: …`, `upstream branch pr/<slug>  <worktree \| (no worktree)>`. A marketplace install: why it can't keep patches, its local changes, `wb patch init`. |
| `wb patch drop <commit>` | | A clone on `local`, tracked files clean, the commit one of `@{u}..HEAD`: `rebase --onto <c>^ <c>` in a scratch worktree (a later patch that no longer applies: `without <c>, the later patch <x> doesn't apply: nothing changed`), `check_move`, `move_branch`; `wb update refreshes the mod`. |
| `wb patch upstream <commit> --name <slug>` | | A clone; the slug `[a-z0-9][a-z0-9-]*` (`patches.SLUG`); an existing `pr/<slug>` or worktree refused. Fetches the upstream's remote, `worktree add -b pr/<slug> $WB_HOME/upstream/<slug> <local@{u}>`, cherry-picks the commit (a conflict stays there for `git cherry-pick --continue`); a commit with no test file says `it has no test: add one before sending (upstream asks for one, and wb patch check needs it)` (`NO_TEST_SEND`); a `Scope: me` commit is flagged. Writes `patches.json`, its `prepared` row the picked commit's ident. Prints, under `next (none of this has run):`, the tests (and the plugin's two when `mod/` changed), the body file to fill (`upstream/<slug>.md`, a copy of `.github/pull_request_template.md` when it exists), the one `wb patch send <slug> --title "<the commit's subject>" --body-file <it>`, `patches.gh_line`, and that send needs the user's yes; off GitHub, that send works only there. |
| `wb patch send <slug> --title T --body-file F` | `--draft`, `--anyway` (sends a `Scope: me` commit) | *Sending a patch upstream*. Refuses before any network (`patches._refuse_send`): no `pr/<slug>` in its worktree, a cherry-pick under way, uncommitted changes there, no commits over the target, an empty body or the template unchanged, a `Scope: me` commit without `--anyway`; then a remote off GitHub, gh not `ready` (`gh_line`), a `.github/workflows/` change over https without the token's `workflow` scope. Under Claude Code (`CLAUDECODE=1`) it then refuses unless the mod's dialog approved exactly this send (`patches._approved`, `send-ok.json`), saying the user may run it in a terminal of their own. Then `gh repo fork` (not when the login owns upstream) and which repository the fork is (`_fork_of`), the push to it in gh's protocol, `gh pr create`; prints `pushed …` and `opened <url>` (`opened <url> (already open)` when a pull request for the branch is open already); on both, `patches.json`'s `sent` row records it (`note_sent`), which `wb update` asks gh about. A failed step says what is done already (a rerun goes on from there). No abbreviated flags (`allow_abbrev=False`: the dialog reads `--title` and `--body-file` as written). The mod asks the user first (*The consent gate*). |
| `wb patch check <commit>` | | *Local patches*. A clone; the commit one of `local@{u}..local` (else `isn't one of local's patches`, exit 1; `no such commit`, exit 1). Its test files (`is_test`) alone, as a diff, `git apply --3way` onto the upstream `wb update` last fetched, in a scratch worktree under `patches/` (removed afterwards, a failure too); then only what the patch added: each `def test_…` its diff adds, as `python -m unittest <module>.<Class>.<method>` (the class read from the file there with `ast`), run by `patches.RUN_TESTS` (a `-c` script, `python -m unittest`'s loader and cwd, telling apart exit 1 a failure or an error, 3 a skip, an expected failure, an unexpected success or no test run, 0 a pass), and `claude plugin test mod` when a `*.test.ts(x)` changed (its exit 0 a pass), with `WB_HOME` inside the scratch directory and `CHECK_SECS` 900 each (the process group killed past it, a failure). One verdict, exit 0, the first that holds: `upstream fails this patch's tests (<subject>): keep it` (any failure); `<subject>'s tests didn't all run here (<why>), so wb patch check can't tell: run them where they can (a skip is not a pass)` (`a test was skipped`, `no test class holds <names>`: an added `def test_…` `_test_ids` placed in no class, `claude isn't on PATH for the mod's tests`; joined by `; `), never a pass: a test skipped without Landlock or claude was called `already passes`, inviting a drop (#1005, 2026-11-22); `<subject> has no test of its own, so wb patch check can't tell: write one (upstream asks for one in a pull request too)` (no test file, or nothing it found to run); `upstream already passes this patch's tests (<subject>): it likely fixed the same thing. Drop it with the user's yes: wb patch drop <sha>`. Before any, `<subject>'s tests don't apply to upstream any more: compare by hand`. It drops nothing. No abbreviated flags. |
| `wb doctor` | | `ok`/`BAD` set the exit code, `--` informs: opencode (`runner.OPENCODE_TESTED`), git (`gitops.GIT_FLOOR` 2.34), models (an Ollama cloud one: `--  <id> is an Ollama cloud model, an API model now: set a role to use it`, #1013), `limit.context`, the roles (`BAD <path> can't be read (…)`, else `--  roles: <summary> (wb route)` once any is set, #1014), `optimize.advice`, `router.thinking_stall`, the state dir (2 GB, `store.host_drive`), `idle_line` (`IDLE_DAYS` 3), `models.unheld_loaded`, the lane (`lane.gaps`, `context.interop_gap`, `context.home_gap`, `context.listening_daemons`), commands.json, `context.TOOLCHAINS`, `wb` on PATH, `cli.logout_stops`, `cli.awake_line`, `webpush.has_cas`, the mod (`cli._mod_behind`), then `patches.doctor_lines`: `ok  install: marketplace at P`, `your own clone at P, branch B (wb patch init)` (on `local`, or patches.json's `source`) or `developer clone at P, branch B`; `--  local patches: N (scopes)` (a clone on `local`; a marketplace clone: its local changes and `wb patch init`); `gh: ready as L over P` (`ok`) or `--  gh: <state>: <hint>`, asked only with patches or a `wb patch upstream` branch that hasn't landed upstream (`held`, `_landed_pr`, by the local refs alone); none sets the exit code. Then each experiment. |

## Files and formats

```
$WB_HOME (~/.local/state/workbench)
  notify.jsonl                   notices that need Claude, all tasks (the runner writes them)
  notify.old.jsonl               handled notices older than ARCHIVE_DAYS, moved by gc (_rotate_notices); past NOTIFY_OLD_MAX its newest half kept
  claude-sessions/<pid>          "<ts> <session id>" lines after a "start <its start>" line (#787): sessions a Claude Code process ran wb in (spend.sessions, cli._caller)
  gc.json                        the last gc: {at, archived, notices, rules, runtime, scratch, starts, uploads, branches, refs, …}
  archive/<task>/                a task closed and untouched for ARCHIVE_DAYS (30), moved by gc
  coding.json                    {mode: only|ask|decide, ts}; none: decide
  experiments.json  .lock        wb experiment: {<name>: "on"|"off"}; probe's old levels adaptable|moderate|aggressive, or true: on; else off
  probe-languages.json           {probe (real path), version, languages, at} from `probe search --help`; re-read when path or version differs (context.probe_languages)
  progress.json                  progress checks' opt-in: {on, after?, ts}; none: off
  splash.json                    the pane's opening (wb splash): {on, ts}; on unless "on": false
  workflows.json                 Dynamic workflows (wb workflows, #1041): {mode: on|off, ts}; none: off
  results.jsonl                  one row per closed task
  stats.json  stats.lock         the Stats tab's settings
  lessons.jsonl                  what each task taught, open until written into a skill or closed
  notes.md  notes.lock           wb notes
  tuning.json                    optional pins ({"fresh_at": 0.6})
  speed.json  speed.lock         each model's measured write and read speeds
  speed-api.json  speed-api.lock each API model's write speeds and times to first token (the account's, #1017)
  gpu.lock gpu.json gpu.admit gpu.load   the GPU lease: lock, holders/waiters, admission, one model load at a time; in models.lease_dir():
                                 $WB_GPU_DIR, /var/lib/workbench/gpu when accounts share it (#393), machines/<machine>/ on a network FS (#849)
  machines/<machine>/            on a network FS, one machine's own (store.machine_dir): GPU lease, optimize.json/.log, boxes.json, wsl-gpus.json, speed.json
  gpu-<host>-<port>.{lock,json,admit,load}   the same for a remote server's models (models.pool)
  scout.json  watch.json         the weekly model check; the watch list
  hosted.json                    API models (models.hosted, #1013): {v: 1, ts, stamp: {opencode: [[path, size, mtime_ns]…],
                                 configs: [[path, mtime_ns]…], content: crc32}, error ("" or the failed read's first line),
                                 providers: {<pid>: {name, key (its source: stored|oauth|env:<NAME>|config|signing|ollama|""),
                                 package, api (its base URL)}}, models: {<id>: {name (at the provider: modelID), limit, cost, variants,
                                 capabilities, reasoning, tool_call, family, status, free, cloud?, and from the catalog (catalog: true)
                                 type, title, released (ms), compatibility, package, api, overlay (bool: headers or a body of its own,
                                 never their values): what models.catalog_doc rebuilds the task's catalog from, #1015}}}; fresh HOSTED_TTL 600 s
                                 (an error DOWN_SECS), rebuilt on a changed stamp; never holds a key
  api-health.json  .lock         each API provider's last bad answer (models.note_api): {<pid>: {state: no_credit|rate_limited|
                                 unreachable, until (epoch or null), why (redacted), ts}}; ok removes it; past until reads as ok; the provider's cooldown every keyhole waits out, and the revive's wait (#1020)
  api/<provider>.<i>.lock        an API provider's task slots (models.NoLease, #1015): API_PARALLEL (3) files, each flocked by the
                                 runner holding it, its task's name inside (models.api_holders); never in gpu.json
  spend-api.jsonl  .lock .checkin.lock  the API ledger (meter.py, #1019; *Spend*), one row each, every row {ts, day (local
                                 %Y-%m-%d)}: step {task, round, provider, model, usd (null: a plan or unpriced), metered,
                                 source (keyhole|provider|opencode|metered|metered (opencode said 0)), plan?, tok {in, out,
                                 cache_read, cache_write, reasoning}}; reconcile {task, round, provider, model, usd (OpenCode
                                 said more), source opencode}; checkin {task, usd_today, cap}: today's one check-in at half
  update.json                    wb update's last check
  patches.json                   {source (the clone install), upstream (owner/repo on GitHub, else null)}: patches.note_install, by wb patch init, upstream, send and wb update (not --check) on `local`; the mod's consent gate reads it.
                                 prepared {<slug>: {author, email, date (unix), subject}}: the local commit wb patch upstream picked;
                                 sent {<slug>: {repo (owner/repo), pr (number), url, author, email, date, subject, merged?, closed_said?}}: the pull request
                                 wb patch send opened (note_sent), asked of gh by wb update (settle_sent); merged: GitHub said so, the row goes once the
                                 local commit is (retire_sent); closed_said: the closed line was printed. A retired pr/<slug> loses both rows (_forget).
                                 Each writer changes only its own keys, read-modify-write under patches.json.lock (_noting)
  send-ok.json                   {slug, title, body_file (absolute), body (its text), draft, anyway, at}: the mod's "Send it" to one wb patch send; patches._approved consumes it (renamed away, then read), fresh for SEND_OK_SECS 120
  route-ok.json                  {argv (the canonical wb route command) | start {task, model}, change (the line), at, by?}: the mod's "Allow" to one role change or API start; routes.approved consumes it (store.take_approval), fresh for ROUTE_OK_SECS 120
  rescued/<UTC %Y%m%d-%H%M%S>[-n]/   a marketplace clone's local changes, saved before a refusal or wb patch init: 0001-*.patch per commit, uncommitted.diff
                                 (a scratch index's diff --binary against HEAD, untracked files in), stash-<n>.diff, branch-<name>/*.patch, base (the commit they apply to)
  upstream/<slug>/  <slug>.md    wb patch upstream's worktree on branch pr/<slug>; its pull request's body (the template copied), outside the worktree so it isn't a change send refuses
  patches/tree-*/                scratch worktrees the patch rebases run in (patches.scratch), removed after each, on a failure too
  boxes.json                     {root: {name: ts}}: what this machine loaded on each wb remote box (models.box_mine; not another client's unless seen in use)
  wsl-gpus.json                  WSL: {ts, gpus: [{name, vram}], tried?}, re-read hourly (hardware.WINDOWS_FOR); a failed read keeps the last, stamps tried (WINDOWS_RETRY)
  bench/cases/<case>/            case.json  spec.md  base.bundle  hidden/<path>…  reference.patch
  bench/runs/<run>/              plan.json  driver.log  <task>/{wb.log,diff.patch,grade.txt} (repo/ while it runs)
  bench/runs.jsonl  driver.json  one row per replay; {pid, run, start, machine, host} (driver.lock: its beat); bench.jsonl, bench/repos/: unread
  optimize.json  optimize.log    wb optimize: {pid, start, began, done, stopped, hardware ([name, vram] per GPU, then ["remote", root, [[name, size]…]] per remote Ollama), secs_per_load,
                                 models: {<id>: {window, fits, spill, clamped, cpu? (nothing in VRAM), applied?, digest? (its weights'), tried: [{window, size, vram, served}], ts}},
                                 skipped: {<id>: {outcome (busy|failed|unreachable), why, ts, retry_after}}, made: {<provider>/<name>: <source id>} (unregistered variant)}
  imports/<slug>/                wb skill import's fetches, for Claude to read
  images/<YYYYmmdd-HHMMSS>/      wb image's candidates (unless --out), .json sidecars, engine.log
  audio/<YYYYmmdd-HHMMSS>/       wb audio's clips: .wav, .json sidecar, .png spectrogram, engine.log
  chat/                          messages.jsonl receipts.jsonl asks.jsonl prefs.json listener.json claims.json switch.json ({on})
                                 serve.pid ({pid, port, start}: wb chat url revives from it) serve.log port.json ({port, pid, start})
                                 away.json ({at, by}; 0 once the user types in the terminal) page.json ({at})
                                 audio/<batch>/ (wb chat listen: <n>.<ext>, audio.json; newest 20) uploads/<id>/ (<name> 0600, .meta.json) uploads.lock
                                 turns.jsonl ({ids, ended, by}: the mod's turn ends, wb chat ended; #1042)
  rules/<task>/                  outside the lane: opencode.json (the config layer), HANDBOOK.md, CLAUDE.md (the user's, copied),
                                 user-rules/<path> (their ~/.claude/rules: context.user_rules), project-rules/<path> (a rule a link takes outside the repo: context.project_rules),
                                 imports/<n>/<name> (an import from outside the repo: context.import_files), bin/git (the shim), grants.json (what Claude allowed: {"allow", "paths", "test_tools"}),
                                 shell/<name> (OpenCode's $SHELL, named as the shell it runs: the user's XDG_DATA_HOME, XDG_STATE_HOME, OPENCODE_DB and SHELL back, then that shell: runner.write_shell_shim, #1010),
                                 gitdir.json (the real git dir, the .git's text: {"gitdir", "dotgit"}), submodules.json ([{"path", "src" (the user's checkout), "sha", "index"}]), submodule-<n>.index,
                                 catalog.json (an API task's own models.dev v2 catalog: runner.write_catalog, models.catalog_doc; OPENCODE_MODELS_PATH, #1015);
                                 removed on close (cli._closed) and by gc for a closed or vanished task
  locks/<task>.runner.lock       the runner's for its life (its pid; touched every BEAT_SECS): one runner a task
  locks/<task>.cmd.lock          one wb command on the task at a time
  locks/<task>.<file>.lock       one writer of status, events, steer, grants
  locks/<task>.ack.lock          one taker of a notice (cli.claim_ack; _ack_all, release_ack)
  locks/<task>.ask.lock          one answer to a question (ask_mcp.answer_once)
                                 (store.task_lock: locks live here, out of every lane: in the task dir an agent flocked runner.lock and status.lock and froze every runner and wb stop, the 0.90 review)
  locks/prepare.json, prepare-<n>.lock   the setups' queue ({pid: {task, since, start, holding}}), PREP_SLOTS turns (runner.prep_turn)
  locks/prepare-check.json, prepare-check-<n>.lock   the base checks' own
  locks/signal-stop.lock         one runner writing a shutdown's one notice (runner._signal_notice)
  locks/skills.lock              one skill-library change at a time, add to commit (skills._library_lock)
  locks/config-<key>.lock        not here: store.config_lock_dir ($XDG_RUNTIME_DIR/workbench, else 0700 workbench-<uid> in the temp dir, else here),
                                 shared by every WB_HOME of the account (#939); never beside the file (a stray in the user's dotfiles, #495)
  tasks/<task>/                  readable from the lane; only worktree/, ask/ and opencode/ writable
    task.json status.json events.jsonl steer.jsonl acked noticed (newest notice's ts) context.md runner.log
    claude-sessions              "<ts> <session id>": Claude Code sessions a wb call naming the task ran in (spend.sessions)
    coding.json                  {allowed: [{edit, said, ts, used, when?, started?, did?}]} (*Claude coding*)
    verify/<n>/                  base.txt candidate.txt result.json
    shots/<n>/                   base.png candidate.png base.txt candidate.txt shots.json
    base-tests.txt  setup.txt    the base check's output; the setup's
    starting.json                a start under way: repo, worktree, wb start's pid, start, machine (pre-#529: branch, commit); gone once task.json is
    prepared.json                for scratch checkouts (runner.PREPARED): {paths (ignored paths copied instead of rerunning the setup, unless a virtualenv
                                 or rebuilt), marks ({path: fingerprint}, runner.prepared_mark), bytes (checked against free space)}
    steps.bundle                 an accepted task's base..tip (gitops.bundle_steps, STEPS_BUNDLE), kept with the task for wb bench add
    .export-<sid>.json           an earlier session's export, made once before a handover, for wb watch
    <name> → ../<name>           a sibling's link beside the worktree (`--sibling`; not one of context.TASK_FILES)
    tree/<parents>/worktree/     the worktree instead, for a sibling more than one level up (task.json `worktree`)
    rounds/<n>/                  prompt.md instructions.md (before a handover rewrote it) summary.md tests.txt
                                 notes.md (Claude's notes folded into prompt.md: runner.folded_notes) nudged (prompt.md is the think nudge; round_asks skips it)
    ask/<id>.json  <id>.answer   questions and permission requests; answers
    opencode/{share,state}/      the agent's OpenCode's own data and state (0700, context.opencode_home, #1010): share/opencode/opencode.db
                                 (its sessions, no credentials), log/, snapshot/, tool-output/ and shell/ (dropped at accept and discard: OPENCODE_SCRATCH)
    worktree/                    the agent's git worktree, branch delegate/<task>
~/.local/share/workbench/skills/ ($WB_SKILLS) the skill library, its own git repo; retired/
~/.local/share/workbench/try/<n>/ wb try's sample projects (task try-<n>; beside $WB_SKILLS)
~/.local/share/workbench/source/ ($WB_SOURCE) wb patch init's clone: branch local, the patches; refs/workbench/before-update, the tip before wb moved it (its reflog: every earlier one)
~/.config/workbench/             writes to models.json, commands.json, savings.json, routes.json, images.json, audio.json, OpenCode's config (models.own_config): store.edit_config
  models.json ($WB_MODELS)       what OpenCode can't say about each model or provider
  images.json ($WB_IMAGES)  audio.json ($WB_AUDIO)   image engines; audio engines (speech, sfx, music, a listener);
                                 top-level strings "default" (images), "default_speech|sfx|music" (audio): the default engines (#1047)
  commands.json ($WB_COMMANDS)   {"allow": [patterns], "paths": [dirs], "env": [names]} (lists of strings, or refused: context.commands_conf),
                                 "repos": {"<repo path>": {"setup", "setup_timeout", "copy", "siblings", "merge": "merge|ff|squash",
                                 "local_only": true|false (API models never get it; a bool or refused, #1021)}}
  chat.json ($WB_CHAT_CONFIG)    {"bind": [...], "hosts": [...], "upload_mb": 25, "upload_message_mb": 50}
  chat-token ($WB_CHAT_TOKEN)    mode 600
  chat-agents.json               beside chat-token, mode 600: {"agents": {"<name>": {"sha256", "ts"}}} (wb chat token; *Other agents*, #1042)
  savings.json ($WB_SAVINGS)     {"kwh_price": N} (wb savings --price; off removes it; other keys kept)
  routes.json ($WB_ROUTES)       the roles (*Roles*, #1014): {"v": 1, "use": "gpu"|"api"|"both" (the Agents switch, #1016; missing: both),
                                 "busy": "wait"|"api" (when the GPU is busy, #1023; missing: wait), "day_usd": N|null, "roles": {"significant"|"trivial"|"writing":
                                 {"via": "gpu"|"api", "gpu": "<id>"|"auto", "api": "<exact id>" (kept on the GPU), "thinking": "off"|"on",
                                 "said": the gated change's line, "by": "dialog"|"pane"|"terminal", "ts", "gpu_by": "terminal"|"claude", "gpu_ts"
                                 (a change that needed no yes: routes.stamp), "order": [backend…] (an OpenRouter role's, wb route --order,
                                 #1021)}}}; a missing role is the GPU's
                                 default; wb route writes it (store.edit_config), the mod reads it (readRoutes); its "day_usd" is the API day
                                 cap the meter keeps (*Spend*, #1019)
~/.config/opencode/opencode.json + .jsonc (layered, under $XDG_CONFIG_HOME; then $OPENCODE_CONFIG; $WB_OPENCODE_CONFIG) the models
/tmp/wb-run-<uid>-<hash>/        the agent's XDG_RUNTIME_DIR (lane on; context.runtime_dir), 0700, .wb-owner {home, task} (RUNTIME_OWNER)
```

The runtime dir is kept while the task is open and removed by wb accept and discard (`drop_runtime_dir`); one another
user holds means a mkdtemp `wb-run-*`; gc removes one untouched for `STALE_RUNTIME_DAYS` (7) only when it names this
profile and a closed or gone task, never another profile's or one wb didn't make (`drop_stale_runtime_dirs`).
commands.json `repos` (`context.repo_conf`): the key nearest the task's spot (the repo, or its package: `subdir`)
wins, the spot itself else the nearest directory above it, so the root's holds for a package without its own and a
package's never for the root or another package (#778: matched by git dir alone, every task got the first package
entry); by real path, else git dir and the path inside the worktree; wb accept reads it the same way. `--setup`,
`--setup-timeout` (a positive whole number) replace; `--copy`, `--sibling` add; `merge` is `wb accept --merge`'s way
(`context.MERGE_AS`; `--merge-as` overrides). `local_only` is read further up: the nearest entry that states it decides
(`context.repo_entries`, nearest first; `privacy.local_only`), so a package's entry for its setup alone doesn't lift
the root's, and a package can say `false` (or `true` alone); an API task never takes standing `copy`/`siblings`
unnamed (*Setting the worktree up*).

**Store primitives** (`workbench/store.py`). `home()` is `$WB_HOME`, else `~/.local/state/workbench`. `PATH_VARS`
(`WB_HOME`, `WB_MODELS`, `WB_SKILLS`, `WB_IMAGES`, `WB_AUDIO`, `WB_COMMANDS`, `WB_CHAT_TOKEN`, `WB_CHAT_CONFIG`,
`WB_OPENCODE_CONFIG`, `WB_SAVINGS`, `WB_GPU_DIR`, `WB_SOURCE`, `WB_ROUTES`, and the user's `OPENCODE_CONFIG`, `CLAUDE_CONFIG_DIR`,
`OPENCODE_CONFIG_DIR`, `XDG_CONFIG_HOME`, `XDG_DATA_HOME`) take `~` and relative paths: `absolute_env()`, first in
`cli.main` and the runner, makes them absolute against wb's cwd (not resolved), so every child sees one place (a
relative one had wb and the runner, which runs from the package dir, disagree: the 2026-11-16 audit, #328, #650); the
mod's `absolutePath` does the same for `WB_HOME`, `CLAUDE_CONFIG_DIR`, `WB_SAVINGS`, `WB_BIN` (#454).
- `write_json`: an exclusively created `.<name>.<pid>.<random>.tmp` beside the file, fsynced (`store._synced`, `write_through` too), renamed
  (renamed unsynced, a power loss could leave `task.json` empty, #475); it replaces a symlink with a plain file. `replace_bytes` does
  the same for whole non-JSON rewrites (results.jsonl in `mark_trials`, notify.jsonl in gc, `acked`, notes.md: unsynced, a crash could empty the history or every notice, #734).
- The user's config files: `edit_config(path, read, change)` under `config_lock(path)`, through `write_through`:
  `writable_target` follows a symlink (refused when the target isn't writable: a Nix store), temp file beside the
  target, 0600, given its mode; nothing written when `change` raises; a file whose inode, mtime or size changed
  meanwhile is left alone ("changed while wb was editing it; nothing was written: run the command again", #939).
- `read_json`: the default for a missing or broken file. `append_jsonl`: one line under `<file>.lock` (a task's under
  `store.task_lock`), first ending a line a write cut short. `read_jsonl(path, offset)`: whole lines and the next
  offset, bad lines skipped; `read_jsonl_tail(path, max_bytes)`.
- `scrub` turns lone surrogates into U+FFFD; `loads` is `json.loads` + `scrub`, for outside JSON (#438); writers escape
  any left (`backslashreplace`, #784). Git's byte paths show through `gitops.shown`; `~/.claude/CLAUDE.md` is copied as
  bytes. All I/O is UTF-8 whatever the locale; the runner runs under `-X utf8`.
- `Task.spec` is a `store.Record`: a missing key raises `store.Damaged` (a `KeyError` to readers, a `RuntimeError` to `cli.main`: "task <t>'s task.json is damaged (it has no
  'model'…)"), and the runner fails the task (`_load_failed`). `spec` and `status` re-read on every access;
  `set_status(**kw)` is locked read-modify-write adding `updated_at`; `set_status_if(check, **kw)` a compare-and-set
  (`cli.reap`). Never write status.json directly.
- `results(trials=False)` drops trials and re-parses only when the file changed (#727); rows are shared: don't mutate.
- `all_tasks()`: `tasks/` with a task.json, oldest first (`archived=True` adds `archive/`, for history: `wb backlog`). `archive(days)` moves tasks
  closed and untouched for `ARCHIVE_DAYS` and drops their locks, since every `wb wait` wake, `wb events` minute and
  `wb start` reads every task; `task_dir(name)` falls back to `archive/<name>`, so the name stays taken.

**task.json** (by `wb start`; `wb next --test` rewrites `test_cmd`, `wb relocate` `repo`, `git_common`, `cwd`):
`name`, `repo`, `git_common` (the repo's git dir, real path: `gitops.repo_of` finds the repo once `repo` is gone),
`cwd` (where `wb start` ran), `session` (`CLAUDE_CODE_SESSION_ID`; with `cwd`/`repo`, whose task it is), `claude_pid` (`CLAUDE_PID`: once gone,
the directory rule adopts the task), `claude_start` (`models._started`, against a recycled pid), `claude_machine` (a process on another
machine is never gone here, `cli._gone`, nor checked by the mod's `noteClaude`: two machines adopted each other's tasks,
#922), `base` (full sha), `base_ref`, `base_branch` (where `--merge` lands: `HEAD`'s branch, or `--base` naming a local one; null on a detached HEAD, tag or commit),
`branch`, `model`, `model_label`, `opencode_model` (`<id>#<variant>` for an API task's thinking, `runner.with_thinking`), `weights_id` (`models.identity`), `route` (the router's why; an API task's routing line), `via` (`gpu`|`api`) and `role` (#1016, always; `writing`: *The writing role*; a task from before it has neither, and reads as GPU),
`provider`, `provider_name` (the catalog's), `thinking` (`off`|`on`, the role's, default off), `said` (the role's `said`, or `asked for[, approved in the dialog]` for a `--model`), and `said_ts` (the role's `ts`) (API only, fixed at start as `model` is), `thinking_shown` (what it does: `thinking off`, `thinking at its lowest effort (it can't turn off)`, `thinking as the model's default (it lists no lower setting)`…), `thinking_off` (`wb-off`'s `{settings}` or `{body}`, #1018), `size`, `stack`, `trial`, `backlog`,
`bench` (`{case, run, label}`), `test_cmd`, `no_base_check`, `unload_for_tests`, `sign` (false: `--no-sign`), `subdir` (a monorepo package), `setup`,
`setup_timeout`, `copy`, `siblings`, `local_only` (`--local-only`, #1021), `order` (an OpenRouter role's backends, which the keyhole routes to and the where-note names: `routes.resolve` copies the role's row, `cli._routed_keys`; none for a `--model` start, whose note says `its choice of backend`), `worktree` (when nested), `checkout` (`"runner"`; missing before #529),
`checkout_bytes` (wb start's disk-check size, counted as taken until the runner checks out; missing when free space was
unknown), `auto_fix`, `self_review` (null: learned; false for a writing task unless asked), `ask_budget`, `ask_timeout`, `checkin`, `round_timeout`,
`silence_timeout`, `think_timeout`, `timeouts_given` (which of `round`, `silence`, `think` a flag gave; the rest
stretch, `runner.stretch_limits`), `test_timeout`, `created_at`, `wb_version`, `experiments` (`{probe: "on"|false}`,
`context.task_experiments`; old `"moderate"`, `"aggressive"`, `true` read as on; missing: off), `probe_files` (probe
on: `{path: lines}`, spec-named files over `PROBE_FILE_LINES` at the base, `context.long_files`; old `probe_setting`,
`probe_explored` unread), `experiments_off` (`{probe: "<why>"}`, `context.probe_off`: no file in a language probe reads over `PROBE_FILE_LINES`, the
count gave up, the languages unreadable, or `a writing task`; `wb status <task>` shows it). Read, written by no command:
`stuck` (guard thresholds), `deny_skills`. A missing key takes the runner's default (auto-fix 0, think and check-ins
off), not `wb start`'s.

**status.json** keys:

| Key | Meaning; set by |
|---|---|
| `phase`, `last`, `pid` | the phase; the last action; the runner's pid (cleared on exit) |
| `parked` | `wb stop --all` or `--park-waiting`; counts only in review, stuck, failed (`store.parked`, `PARKS`); cleared by `spawn_runner`, `wb takeover` |
| `signalled` | a SIGTERM not from wb stopped it (`SIGNAL_STOP`); a shutdown's tasks batch by it (`runner._signal_notice`) |
| `boot` | `store.boot_id` at start; `reap` reads another as a restart |
| `machine`, `host` | `store.machine()`, `store.host()`: another machine's runner is never reaped, stopped or signalled here while its lock beats (`store.elsewhere`) |
| `beat` | `runner.BEAT_SECS`; silent past `cli.BEAT_DEAD`, another machine's runner is dead |
| `round`, `round_started_at` | the round (every kind counts); its start |
| `limits` | `{think?, silence?, round?}`: unflagged limits raised for a model measured slower (`runner.stretch_limits`); null when none |
| `api_entry` | an API task's capped `{limit, options: {max_tokens}}`, written as `drive` resolves its model (`runner.pin_api`), read every round instead of the catalog (#1017); null or absent on the GPU |
| `steps`, `step_round`, `step_sizes` | `wb next`: step number, its first round, the closed steps' `results.step` |
| `step_tests` | `{"<step>": cmd or null}` (`wb start`, `wb next`): what `wb bench add` reads; task.json `test_cmd` is only the latest |
| `step_base` | `wb next`: HEAD as the step advanced: `wb verify`/`wb shot`'s base (`verify._review_base`; without it the newest earlier step's commit by trailer, else the base, #652) |
| `reviews` | review rounds this step (`wb feedback` +1, `wb next` resets) |
| `auto_fixes`, `auto_fixes_total` | since the last review or step; earlier |
| `self_reviewed` | `[steps, reviews]` at the last self-review (once per review round) |
| `reviewed` | `{step, tree}`: the tree the last review notice showed (`gitops.snapshot`, bulky new files left out); the next shows the diff since (the whole diff once git gc pruned it) |
| `tests` | `{ok, exit, tail, round, lane?, proxy?, hang?}` (`exit`: code, `timeout`, `output limit`; `lane`: devices refused; `proxy`: variables left out, on a connection failure; `hang`: `{base, limit}`, a timeout where the base check ran it in a quarter of the limit or less) |
| `base_check` | `{ok, exit, tail, limit, secs, cmd}`, `runner.base_check` in the lane before round 1, with `--test` and no `--no-base-check` (`exit` may be `"timeout"`, `"setup failed"`, `"filtered"`: not the task's bytes) |
| `infra` | the machine failed it: `setup` (`_prepare`), `model` (`_load_failed`: no load, or the server down past `DOWN_WAIT`), `provider` (an API provider's stop, #1020), `spend` (the day's API spend cap, #1019), `crash`, `disk full` (the crash handler), `runner died` (`cli.reap`); cleared at runner start; a replay with it is an `error` row |
| `prepared`, `setup_secs` | the worktree is set up (`_prepare`), so a later runner doesn't again; how long (older tasks: `prepared_paths`, `prepared_marks`, `prepared_bytes`, `runner.prepared_record`) |
| `checked_out`, `checkout_secs` | `runner.checkout` made the worktree; how long |
| `taken_over` | `wb takeover`: `runner.checkout` then fails a task not checked out rather than recheck it out (#655); cleared by the hand-back once checked out, for the mod's `delegatedWork` (#926) |
| `branch_made` | the commit `runner.checkout` makes `delegate/<task>` at, recorded before `worktree add` when no such branch exists: the only branch a cut-short cleanup deletes (#416) |
| `failed_before` | `setup` or `load`: before the agent ran; the row copies it |
| `stopping` | `cli._stop_runner`; cleared by `spawn_runner` and the runner's exit |
| `tools`, `tok_in`, `tok_out` | task-wide calls and tokens (input without cache reads) |
| `outline` | probe on: files whose outline round 1 got (`runner.add_outline`); `[]` for none; set once, before round 1's first call |
| `experiment_use`, `long_reads` | `{name: calls}` (`context.experiment_use`, `events.shell_programs`); whole reads of files over `context.LONG_FILE_LINES` (300) |
| `live_used` | `{session, used}` from the round's last `step_finish`: the between-rounds check when the export fails |
| `gpu_wh`; `gpu_w_sum`, `gpu_w_n`, `gpu_w_peak` | GPU energy (`_Power`, its share when sharing); the watt samples for the row's `gpu_w` |
| `speeds` | `{write, read, ttft}` (`router.speed_samples`), newest `TASK_SPEED_KEEP` (200), stats recording on |
| `guards` | `{<guard>: n}`, stats recording on |
| `opencode` | OpenCode's `M.m` when the runner last started; `runner.opencode_moved` stops the task when it changes |
| `leased` | the pid of the runner that entered its lease (`drive`): alive and not in gpu.json, it stepped aside (its tests, a server's wait) and `cli._gpu_holders` doesn't count it |
| `remote_gpu` | its model runs elsewhere; energy not measured |
| `unrecorded` | `{outcome, step, extra}` until the results row is written: what a retried accept/discard writes |
| `closing` | `wb accept` after its commit: `{commit, step, merge, merge_into, merge_as, sign, merge_from?, merged_head?}`, with `tip`; `sign` false keeps a retry's merge unsigned; a retry uses that object and destination (`gitops.squashed` from `merge_from`), a recovered refusal still exits 5 (#246, #432); while it stands, discard, feedback, next, resume, takeover refuse (`cli._accepting`) |
| `grants_kept` | `{allow, paths}` at close, for `context.grants` afterwards (`wb bench add`) |
| `session`, `sessions`, `session_mark`, `session_floor` | the session; earlier ones; `gitops.mark` at its start; context read at its first change |
| `opencode_home` | `true` once a round ran in the task's own OpenCode home (`run_round`, #1010); a `session` without it began in the user's store, and the next round starts fresh (`fresh_reason`: `OLD_STORE`, the old session's last words exported from there) |
| `context` | one-round instruction: `fresh`, `keep`, `full` |
| `full_pct`, `handovers`, `full_compacted`, `full_compact_failed` | fill at the last mid-round handover; count; compacted first, or failed to |
| `compact_failed_session` | while still `session`, `fresh_reason` hands the next round to a fresh one whatever `context` says (#969) |
| `window`, `load_secs`, `loads` | the window; load time summed (`runner._loaded`); each load |
| `tuned`, `explored`, `tuned_max` | knob values (`tuning.tune`) |
| `self_review_due`, `peak_share` | evidence for `tuning.acted`: tests passed; the fullest share of the window (`runner.peak`) |
| `steer_taken`, `nudges`, `nudged_round`, `checkin_at` | notes delivered; nudges; the round that ended in one; last check-in |
| `active_s`, `step_active_s` | active seconds, rounds less answers and a frozen machine (*Progress checks*); its value when the step began |
| `progress_checks`, `progress_mark`, `progress_open`, `interventions` | checks this step; mark at the last; one open; the steers that answered one, `[{kind, ts, round, step}]` |
| `skills`, `skills_ever`, `skills_rev`, `skills_told` | handed set; ever held; library revs; what this session was told |
| `asking`, `agent_pid` | oldest open question; the agent's pid in a round (cleared by `_end_agent` or `reap`) |
| `tip` | the branch tip at `wb accept` (or `wb gc`): `gitops.step_commit`, `_landed` read it once the branch is gone; accept keeps base..tip in `steps.bundle` (`gitops.bundle_steps`), from which `wb bench add` restores pruned commits (no bundle, or before #799: `refs/workbench/accepted/<task>`, `gitops.ACCEPTED_REF`) |
| `branch_gone`, `accepted_ref_gone` | later `wb gc` runs skip the task (`_merged_branches`, `_released_refs`) |
| `asks` | the runner's count of ask files, `{id: "q" \| "p" \| "x"}` (question, permission, refused): the budgets |
| `answered` | ids `wb answer` answered, written outside the lane (an answer file the agent wrote isn't one): `questions.answered` |
| `spec_gaps` | `[{round, text}]` from `--spec-gap` |
| `served` | `{model, by}`: the model an API task's responses named and OpenRouter's provider (or null), from the keyhole when it first appears or changes |
| `ratelimit_until`, `ratelimit` | the keyhole's hold or queue (#1020): epoch seconds, at least 60 s ahead; `{kind: rate_limited\|outage\|queued, provider, code, since, retry_after, why?}`; cleared at the first data, at each round's start and end |
| `api` | an API task's spend (#1019, *Spend*): `{usd (the task's ledger total; null for a plan), metered, source (the last row's), tok {in, out, cache_read, cache_write, reasoning}, plan?}`, per ledger row |
| `api_stop` | `{guard, text}`: stop the round now: the keyhole's `quota` (#1020), or `spend` (`Meter.room` refused a request, #1019); consumed by the runner's stop (`_ProviderWait.stop`, whose `stopped_by` makes `spend` its own cause), and cleared, whatever its guard, as each round begins (`Meter.begin`) |
| `guard` | `spend` when `runner._go_on` stopped the task before a round (the day cap), with `infra` `spend` |
| `updated_at` | every write |

**events.jsonl** rows are `{ts, round, kind, …}`. From OpenCode (`events.normalize`): `tool {tool, brief, ok, error?}`
(`brief`: command, path, pattern, url, query or description, 90 chars; a skill's id), `text {text}` (2000), `step {in,
out, cache, cache_read, cache_write, reasoning?, cost?}` (reasoning apart from `out`; `cache` the read and write summed,
which the handover check reads; `cost` OpenCode's `part.cost` in USD, only when a number: missing is unknown, never 0, #1022; the meter's step price too, #1019), `error {text}`, `retry {text, attempt}` (OpenCode resent a request). From the runner: `round`, `stopped {text}`, `steer
{now, by: "workbench"}` (a nudge), `interrupted`, `tests {ok, text}`, `skills`, `fresh {overflow, compacted}` (`compacted` after a failed compaction too),
`autofix`, `self_review`, `reading_full`, `full`, `compacted {text}` (mid-round, and it carried on), `load` (a reload before a round), `limits {text}` (stretched),
`left_bg {text}` (a job left in the background), `round_end {secs, how, guard?}` (stats recording on), `export` (the
export failed: judged by `live_used`), `stopped {by: "signal"|"provider"|"spend"}` (`provider`: an API provider's stop, #1020; `spend`: the day's spend cap, #1019; `lessons.gather` skips any with `by`), `gpu {text}` (a load warning), `ask {id}`, `answer {id}`, `served {model, by, text}` (the keyhole: the served model changed; `wb watch` and `wb log` show the text), `rate_limited {provider, code, state, since, secs, text}` (a run of an API provider's waiting ended, or the round stopped: #1020), `backend {by, text}` (the keyhole: OpenRouter served a backend outside the task's `order`, `OpenRouter served <x>, not <order>`; a line in the round's report too, #1021). From
`wb`: `spec_gap`, `next_step`, `stopped` (bail out), `resumed`, `allow`, `steer {now}`, `captain`, `takeover`, `coding` (the user allowed Claude an edit), `error`
(`reap`: the runner died without a report).

**results.jsonl** (`results.record`): `ts`, `task`, `outcome` (`accepted`/`discarded`), `model`, `size`, `role` (task.json's, when set: `router.of_role`), `repo`,
`stack`, `subdir`, `rounds`, `auto_fixes`, `reviews`, `spec_rounds`, `tests_ok`, `minutes` (wall clock, queueing and review included), `tok_in`, `tok_out`, `ran` (`results.ran`: a session
began or a token was written),
`failed_before`, `skills_offered`, `skills_loaded`, `skills_rev`, `experiments`, `steps`, `window`, `weights_id`,
`thinking` (an API task's, `off`|`on`, #1018), `tuned`, `explored`, `tuned_max`, `peak_share`, `self_review_due`, `spec_gaps`, `claude` (`spend.usage`: `{turns,
calls, input, cache_write_5m, cache_write_1h, cache_read, output}`), `gpu_wh`, `remote_gpu`, `active_minutes`
(status `active_s`), `interventions`, `diff` (accepted: `{files, added, removed, generated?, deleted?}`, `gitops.footprint`),
`landed_steps` (discarded: the steps `wb next` committed and the discard kept; `steps_dropped` after `--drop-steps`), `backlog`, `trial`, `experiment_use`, `long_reads`,
`kind` (`gpu`|`api`, task.json `via`; a row without it is GPU, an old `:cloud` one too), `role` (task.json `role`, or null),
`provider` (task.json `provider`, else the model id before its first `/`), and for an API row `api` (`results.api_usage`:
`{cost (USD or null, never 0 for no price), tokens {in, out, cache_read, cache_write, reasoning}, source (opencode | mixed |
metered | plan | unknown), plan?, backend?}`) and no `gpu_wh` or `remote_gpu` (#1022),
`probe_long` (task.json `probe_files` named any; old rows' `probe_level`, `probe_setting`, `probe_aggressive`: unread); stats recording on
(`results.for_stats`): `started_at`, `load_secs`, `setup_secs`, `nudges`, `asks` (`{questions, permissions, refused,
answered}`), `guards`, `speed` (medians of status `speeds`), `gpu_w` (`{mean, peak}`); `commit`, `merged`, `merge_refused` on accept, `reason` on
discard. A step (`results.step`): `files`, `lines` (added against HEAD, new files included), `removed`, `window`, `landed`, `compacted`, `handovers` (with
overflow; both leave
out the `fresh` event a later step began with, #958), `read_full`, `reviews`, `spec_rounds` (a gap given when closing
the step included, #904), `stuck`, `took_over`, `claude` (its share, `spend.usage_by_step`, when split).

**bench/cases/<case>/case.json** (`bench.add`): `name`, `task`, `step`, `repo`, `checkout` (task.json `repo`, which
`siblings` resolve from lexically, #909), `git_common` (`bench._repo`), `base`, `reference`, `size`, `stack`,
`test_cmd`, `test_timeout`, `check_cmd`, `hidden` (paths; their files in `hidden/`), `allow`, `paths` (the grants, `grants_kept` once rules are gone), `setup`, `copy`, `siblings`, `setup_timeout`,
`unload_for_tests`, `subdir` ('' for the top), `skills`, `original` (`model`, `rounds`, `reviews`, `minutes`, `steps`),
`base_check` (`fails` or `not checked (timed out)`), `created_at`. **bench/runs.jsonl** (`bench.replay`): `ts`, `run`,
`label`, `rep`, `case`, `model`, `task`, `wb_commit`, `skills`, `outcome` (`passed`, `failed`, `preempted`, `error`:
not started, or the machine broke it, `<infra>: <last>`), `why`, `phase`, `rounds`, `auto_fixes`, `tok_in`, `tok_out`, `minutes`, `asks_refused`,
`visible_ok` (before the hidden files), `handovers`, `experiments`, `experiment_use`, `long_reads`, `probe_level` (`"on"`; older rows the level),
on an API model `kind: "api"` and `paid {cost, source}` (`bench.paid`, from `results.api_usage`, #1022). A
`wb bench self` row (`bench.self_run`): `arm`, `model`, `label` all `"claude"`, `claude`, `turns`, `claude_model`,
`minutes`, `outcome`, `why`.

**Other formats.**
- `steer.jsonl`: `{ts, text, now}`. `acked`: a float timestamp, replaced whole (`store.write_ack`): polled readers saw 0
  between a truncate and a write.
- `ask/<id>.json`: `{id, ts, round, question, context, command?, path?}` (the agent's: checked); `<id>.answer`: the text
  (`ask_mcp.write_answer`), `NO_ANSWER` for one the runner dropped (timed out, round ended, stopped,
  runner died).
- `lessons.jsonl`: `{ts, task, repo, skills, id, kind, round, text}`; a close `{close: id, ts, how: "skill:<name>"|"dropped", why}`.
- `speed.json`: `{"<model id>": {"write": [tok/s…], "read": [tok/s…]}}`, last 60 each.
- `speed-api.json`: `{"<API model id>": {"write": [tok/s…], "ttft": [s…]}}`, last 60 each; in `$WB_HOME`, not `machine_dir`.
- `stats.json`: `{recording, live, every, ts}`; `every` 1, 2 or 5 s; bad or missing key: `stats.DEFAULTS` (on, on, 2); `set_setting` changes
  one key under `stats.lock` (#564).
- `gpu.json`: `{"<pid>": {model, task, since, start, uid, user, waiting?, waiting_since?, tests?}}` (`uid`, `user`: for a lease accounts share; `tests`: unload-for-
  tests holds the GPU, `model` null; `since`: queue place, the task's `created_at`; `waiting_since`: kept across rewrites until admitted, `wb top`'s
  `waited_s`; `start`: `/proc/<pid>/stat` field 22, else
  `ps -o lstart=` in UTC, against a recycled pid).
- `scout.json`: `{done_at, notes, offered_at}`; `watch.json`: `[{ts, text}]`; `update.json`: `{checked_at, behind,
  kind}` (`marketplace`|`clone`; `checked_at` written before the fetch, so a failed check waits a week too);
  `tuning.json`: `{"<knob>": value}`; `notes.md`: `- <note>` lines.
- Image sidecar: `{prompt, seed, width, height, engine, refs, secs, ts, format}` (png, jpeg or webp, from the file's first bytes).
- The chat files: *The web chat*.

## Installing

**Users** (README's quickstart):
```sh
claude plugin marketplace add a-schaefers/claudes-workbench
claude plugin install workbench@workbench
"${CLAUDE_CONFIG_DIR:-$HOME/.claude}"/plugins/marketplaces/workbench/bin/wb setup
```
then `/workbench`; `wb setup` ends with the `first run:` checklist (`cli.first_run`: OpenCode, a
model server, a registered model, the API keys, Landlock). Its API keys line (#1024,
`cli.api_key_lines`, from `models.connected()`: `opencode auth list` in wb's environment, never a
lane, never a secret): `ok  API keys: deepseek (stored via /connect); no role uses one yet` (or
`trivial and significant use it`), a `--` line for a key wb can't hold (`xai's key is in XAI_API_KEY:
wb can't hold it outside the lane yet; /connect stores one it can`, `anthropic is signed in with
OAuth: …`), `NO_KEY` for none. With a stored key and no local model the GPU lines read as optional
(`NO_SERVER_OPTIONAL`, `NO_LOCAL_OPTIONAL`). No GPU is needed for a role on API (*Roles*). The plugin install copies only `mod/` into the plugin
cache; `marketplace add` keeps a full clone (`installLocation` in `known_marketplaces.json`, or under
`CLAUDE_CODE_PLUGIN_CACHE_DIR`), from which `wb setup` links `wb` and the skill. `wb update` (`/workbench-update` also
reloads plugins) runs `claude plugin marketplace update workbench` and `claude plugin update workbench@workbench`; the
mod runs `wb update --check --if-due` once a session. Claude Code's marketplace update never pulls that clone: whenever
its HEAD differs from the source's head it deletes it and clones again, one commit deep, so a commit, an edit, a stash
or a branch there is gone with no backup, even with nothing new upstream (Claude Code 2.1.323, 2026-11-22). So `wb
update` refuses over local changes there, saving them first, and a user who patches workbench moves to a clone of
their own (*Local patches*). `mod/.claude-plugin/plugin.json` has no `version`, on purpose:
Claude Code versions it by commit, so every merge to main is an update; a `version` froze installs until bumped
(2026-11-18), which doesn't scale to contributors. `claude plugin validate`'s warning about it is expected. Main is the
marketplace.

**Developing**: the clone is the marketplace, so plugin, `wb` and skill read the working tree:
```sh
git clone https://github.com/a-schaefers/claudes-workbench ~/repos/workbench
cd ~/repos/workbench
ln -s "$PWD/bin/wb" ~/.local/bin/wb
ln -s "$PWD/skill/workbench" ~/.claude/skills/workbench
claude plugin marketplace add "$PWD" && claude plugin install workbench@workbench
```
(`bin/wb setup` makes the same links; `wb setup --force` repoints them from the copy you want.) `wb update` pulls
`--ff-only`, refuses while tasks run, then runs the two `claude plugin` steps (a path marketplace is copied too).
Claude Code never resets a directory marketplace, so a clone keeps its commits; on branch `local` they are patches.

**Patched** (`wb patch init`): a user's own clone, by default `~/.local/share/workbench/source` (`WB_SOURCE`), added as a
directory marketplace in place of the git one, `wb` and the skill relinked to it; it updates like a developer's
clone, its patches rebased (*Local patches*). `wb doctor`'s `install:` line says which of the three this copy is.

### Local patches

`workbench/patches.py`, `wb patch` (#1005): a user's own fixes to workbench, kept across updates and offered upstream.
Every git call in it runs with `gitops.PLAIN` and `NO_HOOKS` (a hook of the user's once ran from wb's own calls,
2026-11-21), stdin closed, its own session; a fetch or clone never prompts (`_net_env`, `_fetch_origin`'s rule,
`NET_SECS` 600). It imports everything at the top: `wb patch init`'s `marketplace add` deletes the copy wb runs from.

- **The marketplace clone keeps nothing.** `local_changes` finds HEAD's commits over `@{u}` (with no upstream, what no
  remote has), uncommitted changes (`--untracked-files=all`, never ignored files), stashes, and other local branches'
  commits no remote has (Claude Code's re-clone drops those too). `rescue` saves them under `rescued/<UTC time>/`
  (`DIFF_PLAIN`: the user's diff settings off, so another git applies them; uncommitted.diff from a copy of the index,
  so the user's is never touched). `wb update` then refuses: `the marketplace clone <root> has N local commit(s)…:
  Claude Code's update re-clones it and would wipe them, so nothing was updated. Saved in <dir>; wb patch init …` (it
  once said `updated marketplace <patched>..<upstream>`, exit 0, having wiped the patch). `--check` says it as
  `CHECK_LINE`, which the mod toasts.
- **`wb patch init`** moves the install to a full clone the user owns: every refusal first, then the rescue, the clone,
  branch `local` (`PATCH_BRANCH`) at the marketplace clone's base, the changes applied as the first patches, and the
  switch (`marketplace add`, `wb setup --force`, `plugin update`), each later failure saying what is done and how to
  finish (*Every `wb` command*). Stashes and other branches aren't applied: `rescued/` keeps them, and it says so.
- **A patch is a commit on `local`**, tracking upstream, with a `Scope: me|everybody` trailer that `wb patch list`
  reads (`%(trailers:key=Scope)`) and `upstream` and `send` check. `wb update` on `local`, diverged and behind:
  `rebase_patches` rebases `@{u}..HEAD` onto the fetched upstream in a detached worktree under `patches/`
  (`scratch`; `REBASE_PLAIN`: no `updateRefs`, `autoSquash` or `autoStash`), never the live tree (mid-conflict there,
  every wb call from the clone died with a SyntaxError). A conflict refuses, nothing changed, naming the patch: `the
  local patch <sha> <subject> no longer applies on upstream's <up>: nothing changed. Either rework it by hand (…) or drop
  it (wb patch drop <sha>), then wb update (wb patch check <sha> first: if upstream fixed it another way, drop it)`. `check_move` (a dry `read-tree -m -u -n`, the merge `reset --keep` does)
  refuses an untracked file the rebased tip would overwrite (a failed `reset --keep` once came after the stop and said
  "the pull is done"). Both run before any task is stopped. After it, `move_branch` refuses a HEAD that moved meanwhile,
  runs `reset --keep`, then keeps the old tip at `refs/workbench/before-update` (`BEFORE`, its own reflog listing every
  earlier tip). A patch upstream already has drops out of the rebase; `landed` finds it by author, date and subject.
  Any other branch keeps the diverged refusal. `wb patch drop` is the same rebase, `--onto <c>^ <c>`.
- **Knowing when upstream took it** (#1005, 2026-11-22): the rebase alone sees only a clean cherry-pick or an empty
  result, so a pull request upstream squashed, reworded or edited left the patch to conflict or double up. Three
  signals, two that drop by themselves and one that only recommends:
  1. *The patch's own pull request* (drops): `wb patch send` records it (`sent`, *Files and formats*), and `wb update`
     (not `--check`) asks gh its state before the rebase (`settle_sent`). Merged, and its merge commit (gh's
     `mergeCommit`) in the fetched upstream, the local commit whose ident (author, email, author date, subject: what a rebase keeps) matches is left
     out of the rebase (`rebase_patches(root, skip)`: an interactive rebase whose todo `DROP_EDITOR` rewrites, still
     in the scratch worktree), said `your pull request #<n> merged: dropped the local patch <subject>`, and its
     `pr/<slug>` retired once the branch moved (`retire_sent`; a run that died first leaves the row `merged`, so the
     next drops it without asking). Closed, it is kept and said once; gh not ready or not answering, kept and said.
  2. *The rebase* (drops what it can see): as above; its conflict refusal now names `wb patch check`.
  3. *The patch's own tests on plain upstream* (recommends only): `wb patch check <commit>` (*Every `wb` command*)
     applies the patch's test files alone to upstream and runs only the tests it added; passing there says upstream
     likely fixed the same thing (a skip, or a test it couldn't run, is no pass: it says it can't tell), and the drop is the user's yes, through `wb patch drop`. A patch with no test can't
     be judged: `wb patch list` marks it ` (no test)` and `wb patch upstream` asks for one.
  Every drop is recoverable: `move_branch` is still the only path that moves the branch, and `BEFORE`'s reflog keeps
  the old tip.
- **A crash** (*Every `wb` command*) points at `wb patch` and `CONTRIBUTING.md`: Claude fixes it locally, then offers it.

### Sending a patch upstream

`wb patch upstream <commit> --name <slug>` cuts `pr/<slug>` from fresh upstream in `upstream/<slug>/` with only that
commit, copies the pull request template to `upstream/<slug>.md`, and prints the one `wb patch send`; it pushes
nothing. `wb patch send` is the only command in wb that forks, pushes or opens anything, through gh (GitHub's CLI); in
Claude Code the mod asks the user first (*The consent gate*) and wb itself refuses a send the mod's dialog didn't
approve; outside it the user typing it is the yes. CONTRIBUTING.md is the contributor's side; the skill's
`reference/patching.md`, Claude's.

- **gh's state** (`gh_state`): `gh auth status --hostname github.com --json hosts` (`GH_SECS` 20; never plain `gh auth
  status`, which prints a masked token, nor `--show-token` or `gh auth token`): `missing` (`install_hint`: one line for
  this OS from `OS_RELEASE`, labelled official or community as GitHub's docs label it, `GH_DOCS`, `GH_RELEASES`; never
  snap; the distro's own apt `gh` warned off; `the user runs: ! ` before a sudo or pamac (polkit) line, `the user
  runs, as root:` before apk's, as Claude's Bash can't answer a password; brew offered with no sudo), `logged-out`
  (also a 401: `! gh auth login --hostname github.com --git-protocol <p>`, `_guess_protocol`: an insteadOf rule by its
  target, the `url.<base>` key (`GH_SSH`, `GH_HTTPS`: one rewriting `git@github.com:` to https forces https, and was once
  read as ssh by its value), else ssh with a key, else the remote's URL; https adds `! gh auth setup-git`, an edit to
  the user's global git config), `error` (unreachable, `NET_ERR`; a gh too old for `--json`), `ready` (login, git protocol, scopes, token
  source; a `GH_TOKEN`/`GITHUB_TOKEN` overriding the stored login is said). `gh_line` prints it. Claude never handles a
  token: the login is the user's own `! gh auth login`.
- **The send**: `_refuse_send`'s checks, then gh `ready`, then, under Claude Code (`CLAUDECODE=1`, which its Bash
  sets), the mod's approval (`_approved`): `send-ok.json`, written by the dialog's `Send it`, renamed away so one send
  consumes it, matching or not, and taken only when fresh (`SEND_OK_SECS` 120) with this slug, title, absolute body
  path, body text and flags. Without the mod (not installed, disabled, its hooks failing to load) nothing asked the
  user, and Claude's Bash sent anyway; it refuses, naming the command for the user's own terminal. Then `gh repo fork
  <owner/repo> --clone=false --remote=false` (`--remote` renames origin; skipped when the login owns upstream) and the
  fork's real name (`_fork_of`: gh's `<owner/name> already exists`, else `gh api repos/<login>/<name>` whose `parent` is
  upstream, else the newest of `gh api repos/<owner/repo>/forks` owned by the login; an unrelated repository of that
  name is never pushed into, and a 404 is a fork not there yet), `git push` of `pr/<slug>` to the fork's URL in gh's
  protocol (a new fork appears asynchronously: a push finding no repository is retried after `PUSH_WAITS` 2, 4, 8, 16
  s; any other failure, and a push with no answer in `NET_SECS`, fails the step at once), `gh pr create -R <owner/repo>
  --base <target> --head <the fork's owner>:pr/<slug> --title … --body-file … [--draft]`; a pull request for the branch
  open already (an earlier send's answer lost) is `opened <url> (already open)`, exit 0. The
  target is `_tracked`: the branch `local` tracks, else HEAD's, else origin's default. gh runs with no terminal,
  `GH_PROMPT_DISABLED`, `GH_NO_UPDATE_NOTIFIER` (`_gh`); every message from gh or git passes `scrub` (`TOKEN`:
  `gh[oprsu]_…`, `github_pat_…`), as a pushed URL or an error may carry one, and a remote's URL printed loses its
  `user:password@` first (`_shown_url`: a GitLab token was printed whole).
- **After the merge**: `wb update` asks gh about each sent pull request (`settle_sent`, *Local patches*' first
  signal): merged, it drops the local patch however upstream reworded or squashed it, and retires `pr/<slug>`
  (`retire_sent`); closed, it keeps it and says so once, pointing at `wb patch check`. Without a `sent` row (gh
  printed no pull request URL; no ident found: no `prepared` row and no reflog for `pr/<slug>`; a branch sent before
  `wb patch send` kept `sent` rows; or one sent by hand), the rebase drops what it can see (`landed`) and `retire_landed`
  removes the `pr/<slug>` branch, its worktree and its body file (`_landed_pr`: every commit since the cut, the
  branch's first reflog entry, is upstream's, merged as is, by patch id, or by author, date and subject; nothing
  committed yet is never landed).
- **`patches.json`** (`note_install`): the clone install and its upstream's owner/repo (`_owner_repo` of the tracked
  remote's URL; null off GitHub), for the mod's gate; `prepared` and `sent`, wb's own (`note_sent`).
- The GitHub plugin for Claude Code is not needed for any of it; its tools that write to upstream pass the same gate.

## The tests

From the repo root: `python3 -m unittest` (`tests/__init__.py` makes `tests` a package), `tests.test_workbench.Context
[.test_…]`, `-k handover`; the mod: `claude plugin validate mod && claude plugin test mod`; the chat:
`PLAYWRIGHT=…/node_modules/playwright node tests/e2e_chat.mjs`.

**CI.** `check.yml` on every push to main and pull request: `suite` in six shards (`WB_TEST_SHARD=K/6`, `fail-fast`
off) on Python 3.11, the floor; `mod` with `CLAUDE_CODE_VERSION`; `check`, the required one, `needs` both, runs
`always()` (a skipped required check passes) and is green only when both succeeded, so the ruleset waits on one name. A pull request touching only
`README.md`, `docs/releases/` or images in `docs/` (the `Scope` step, its merge commit against its base) runs only
`tests.test_docs` and `tests.test_workflows` in shard 1; the suite reads ARCHITECTURE.md, CLAUDE.md, the skill, the
handbook and the release checklist, so a change to any runs it all, as does every push to main. No path filter on the trigger: a required check must report on every pull request.
`nightly.yml`, daily: the suite on the newest Claude Code (red: a new Claude Code broke the mod, not main), plus `macos` (Apple silicon, 3.11, OpenCode's `darwin-arm64` zip
with its own hash, `WB_TEST_SHARD=K/5`) and `wsl` (WSL2's Debian on a Windows runner via `scripts/ci-wsl.sh`: dash as
`/bin/sh`, non-root, a clone in WSL's FS; no third-party action). Not required, but a release waits for a green nightly
on its commit (`docs/release-checklist.md`; the 2026-11-19 audit: macOS failed unseen, nothing ran WSL).
`opencode.yml`: the suite daily on the newest OpenCode (the highest release, not GitHub's "Latest"). Sharding:
`tests/__init__.py`'s `load_tests` deals the i-th test of discovery's sorted order to shard i mod N, once, by the
outermost `load_tests` (nested, it sharded twice); `python3 -m unittest tests` is sharded too, named modules, classes or
tests run as named; no test has class or module fixtures.
The macOS shards run `python3 -X faulthandler -m unittest -v`, each test named in the log as it runs (unnamed, a
cancelled hour reported nothing, #950). Every suite job sets `WB_TEST_HANG_SECS` (600, via `WSLENV` for
WSL): `SIGALRM` around each test dumps every thread's stack and fails it with `tests.Hung: <test id> hung past
WB_TEST_HANG_SECS (N s)`, a `BaseException` (as a `TimeoutError` it was swallowed by `socket.getfqdn`'s `except
OSError`, 2026-11-22). Job limits 60 min, `check.yml`'s shards 30. Linux jobs install `OPENCODE_VERSION` via
`scripts/install-opencode.sh` against `OPENCODE_SHA256` (2.x isn't on npm), print the Landlock ABI, and `check.yml`'s
`Git LFS` step installs git-lfs. Credential-hiding tests skip when the home is in a temp root (`home_outside_the_lane`);
a profile without a git identity gets a scratch one. Credential-read enforcement needs Landlock, the Snap launcher test
Linux; the killed-runner test checks kernel cleanup on Linux, cleanup after reaping on both. `chat.yml` runs the browser test (`PLAYWRIGHT_VERSION`) when
`chat.py`, `webpush.py`, `chat_static/`, the test or itself change; not required. A newer push cancels a branch's run,
never main's (shields.io shows a cancelled run as failing). Dependabot moves the SHA pins. Not in CI: a real model. The real run, after a change to how OpenCode is invoked or read: a scratch repo from `wb init` with a committed CLAUDE.md, `wb start <t> --trial --size trivial --spec <f> --repo <dir> --test '<cmd>'`, `wb watch <t>`, `wb report <t>`, then `wb discard <t>`; it takes the GPU and writes the real state directory, and `--trial` keeps it out of the router's evidence and the lessons.

**Floors** live here; the README names only Python 3.11, git 2.34, OpenCode 2.3 (`runner.OPENCODE_TESTED`'s floor), a
32K window recommended, 16K the least (OpenCode's own prompt is ~10K). Claude Code 2.1.287 (mods' floor); the mod's
`MIN_CLAUDE_CODE` (= `CLAUDE_CODE_VERSION`) toasts once below it (`this Claude Code (<v>) is older than <min>…: claude
update if they misbehave`, also when `$.session.version` is missing) and carries on. OpenCode 2.2.7 passes the real-OpenCode tests
(2.2.0 never lists `workbench_ask_reviewer`). Python 3.11: before 3.14 `Path.exists`/`is_dir`/`is_file` raise under an
unreadable parent, so probes of others' paths use `os.path` (#850, `UnreadableParents`). git 2.34 (`gitops.GIT_FLOOR`;
fallbacks for `merge-tree --write-tree`, 2.38, and `GIT_NO_LAZY_FETCH`, 2.44; `wb doctor` BAD below).

**Files.** `test_workbench.py` (end to end, one class per feature); `test_gpu.py` (sharing, `can_share`, the budget);
`test_portable.py` (discovery against real HTTP servers on port 0, `ServerAway`, the polling watcher, the launchers);
`test_sizing.py` (the slice guess, corrected from real steps); `test_workflows.py` (the merge gate in plain text: every action pinned to a commit with its version,
every workflow its own permissions, `check` never path-filtered and required from GitHub Actions, green only when every
`suite` shard and `mod` are, the shards dealing each test once, the ruleset without bypass, the plugin without
`version`, `owner-merge.yml` approving only the owner's pull requests without running their code); `test_docs.py`
(every command and flag in *Every `wb` command*, every module in CLAUDE.md's layout, every `WB_*` variable in CLAUDE.md,
CLAUDE.md under 25,000 characters); `fake_opencode.py`, `capture_llm.py`, `e2e_chat.mjs`. The tests' own
`HTTPServer`/`ThreadingHTTPServer` (`_Listens`, 0.02 s tick) listen at once: http.server's looked the name up first, 35 s
on macOS runners (#950); not patched suite-wide, so `WebChat` still catches chat.py doing it.

**The fixture** (`Base`): `WB_HOME=<tmp>/state`, `WB_OPENCODE_BIN` (the fake, `shlex`-split), `WB_FAKE_MODELS=1`,
`WB_OPENCODE_CONFIG` (`ollama` on 127.0.0.1:11434 with `mimo-v2.6:9b-q8_0`, 32K, and `qwen3.8-27b` with no
`limit.context`; `strata` on :8080; a remote `cloud` discovery drops, `hosted()`'s as an API model), `WB_MODELS`, `WB_SKILLS`, `WB_COMMANDS`,
`store.config_lock_dir` patched into the temp state; a repo on `main` with a committed CLAUDE.md and its own identity;
the spec "Write the answer to answer.txt.", the test `grep -qx 42 answer.txt`. `self.start(name, scenario, *extra)` sets
`FAKE_SCENARIO`, runs `wb start … --model mimo`. tearDown kills nothing: a test stops its own `hang`/`slow` tasks. Helpers: `run_wb` (in-process, returns stdout;
a `die` is `SystemExit`, its message on stderr), `wait_phase(name, phases, timeout=30)` (phase reached and runner pid cleared; on a timeout it raises with the whole status.json, the task's `runner.log` under the fixture's `WB_HOME`; `running(name)`
for the agent's pid), `POLL` 0.05 s, `wait_gone`, `backdate` (past `cli.REAP_GRACE`), `write_skill`, `run_calls`; a
production cap a test isn't about (`STOP_SETTLE`, `SIGN_SECS`, `OPTIMIZE_EXIT`) is patched short. The runner is real
(config layer, shim, lane, git, gpu.lock); `WB_FAKE_MODELS` short-circuits every model-server call, `WB_FAKE_GPU_GB`
sizes the fake GPU (unset: no sharing). A runner keeps its spawn env: a `FAKE_*` change reaches only later runners.

**The fake OpenCode** handles `--version` (`FAKE_VERSION`, else `opencode v2.3.0`), `run …` (prompt last, `--session`
continues; starts the config's MCP server and lists its tools unless `FAKE_NO_MCP`; a `-` prompt without `--` prints
usage, exit 1; with `OPENCODE_DB` set a new session is recorded in that home, `<db>.sessions`), `session export`
(tokens `FAKE_FILL` or 1200, answer.txt 41 → 42; `FAKE_COMPACTED`; `info.home` the database read; in a home that
records sessions one it doesn't hold is "Session not found", exit 1), `session list` (the home's recorded ones); session
commands fail without `--standalone`; `auth export <provider>` prints `FAKE_AUTH_EXPORT`'s rows for it (2.3.2's shape),
else `[]`. It streams one JSON event per line (`step_start`, `tool_use`,
`text`, `step_finish` with tokens). Live tool names are 1.x-style (`bash`, `filePath`; 2.x `shell`,
`path`: `events._brief` reads both). Records `FAKE_PROMPTS`, `FAKE_RUNS` (each run's `session`, `prompt`, `cwd`,
`pwd`, its `opencode_db`, `xdg_data`, `xdg_state`, `shell`, `models_path` (`OPENCODE_MODELS_PATH`), and `shell_sees`: what a `$SHELL -c` command gets); knobs `FAKE_LATE`, `FAKE_SLEEP`,
`FAKE_LESSON`, `FAKE_BG`, `FAKE_STRAY`, `FAKE_SURROGATE`, `FAKE_LATIN1`, `FAKE_LINGER`, `FAKE_EXIT`. `FAKE_SCENARIO`:
`ok`, `fix`, `loop`, `hang`, `crash` (`FAKE_DOWN`), `crashtalk` (`FAKE_CRASH_HANG`), `devserver`, `slow`, `fill`,
`read`, `floor` (`HANDOVER_ROOM`), `refill` (`HANDOVER_MAX`), `compact` (`FAKE_COMPACT_READ`), `compactfail` (OpenCode
2.3's `{"type": "error", …, "error": {"type": "compaction.failed", "message": "Compaction produced no summary"}}`;
`FAKE_COMPACT_MSG`, `FAKE_COMPACT_EXIT`, `FAKE_COMPACT_EDIT`, `FAKE_REFAIL`), `bgwait` (`FAKE_BGWAIT`), `toolhang`,
`retry` (`FAKE_RETRY_EVERY`), `say`, `spend` (#1019: a read and a priced step every `FAKE_STEP_EVERY` s, `FAKE_STEPS` of
them, then `ok`'s answer). A new fake behaviour is a new scenario.

**API models in the fake** (#1011, from the spike for #1009 on OpenCode 2.3.2; the real-OpenCode checks below hold on
2.3.0 too). `WB_FAKE_CATALOG` names a JSON file of `api model.list` entries (a list, or `{"data", "auth"}` to give
`auth list` its own rows; unset, empty). The fake prints `api model.list` as `{location, data}`; `models` as
`<provider>/<id>` lines, refusing a provider argument as 2.3.2 does; `auth list --format json` as `[{id, name,
connections: [{type: "credential", id, label, method: "key"}]}]` per catalog provider. `run -m <provider>/<id>#<variant>`
splits at the first `/` and the first `#` after it: a catalog provider's unknown model is `{"type": "error", "error":
{"type": "provider.no-route", "message": "Model unavailable: <id>"}}`, an unknown variant (neither the catalog's nor the
config's `provider.<p>.models.<m>.variants`, V1 or V2, from `OPENCODE_CONFIG` and `OPENCODE_CONFIG_CONTENT`) `Variant
unavailable for <id>: <v>`, both exit 1 before any step; a config model passes as before. Every `step_finish` has
`part.cost` (from the catalog's first price, its context tier past `size` by input plus cache reads and writes: `input` without cache reads, reasoning at
the output rate; `FAKE_COST` for an unpriced model, else 0; `FAKE_STEP_COST` plays OpenCode's figure, a number or
`none` for no `part.cost`) and `part.tokens{input, output, reasoning, cache{read, write}}` (`FAKE_STEP_IN` the input,
120); `session export` gives the assistant message `cost` (`FAKE_COST`, else 0) and `info.cost`/`info.tokens`
their sums. The API scenarios: `ratelimit` (`step_start`, then `retry` events, attempts 2 to `FAKE_RETRIES`+1, 10 by
default, `FAKE_RETRY_EVERY` apart, each `{type: "retry", attempt, nextRetryAt, messageID, error: {type:
"provider.rate-limit", message, status: 429, response: {body}, retryAfterMs}}`, `retryAfterMs` only with
`FAKE_RETRY_AFTER`; then that error, exit 1; with `FAKE_RETRY_OK` the retries end in an answer, as `ok`), `down`
(the same with `provider.transport` ECONNREFUSED, checked on 2.3.2), `api` (#1020: one request to a keyhole,
`FAKE_API` "<base> <token> <model>", silent while held; a 429 or 5xx back is a `retry` and the request again, 10 at
most, `FAKE_RETRY_EVERY` apart; 402 `provider.quota`, 401/403 `provider.auth`, any other `provider.invalid-request`
(the fake's guess at OpenCode's type), exit 1; then as `ok`), `auth` (401, `provider.auth`) and `quota` (402, `provider.quota`), both at once with no retry, and `nokey`
(the no-route error); the upstream message (`FAKE_ERROR_BODY`) comes through verbatim in `message` and
`response.body`. `http` (#1015) makes the request for real: the provider's `options` from `OPENCODE_CONFIG`, then
`OPENCODE_CONFIG_CONTENT` over them, a POST to `<baseURL>/chat/completions` with `Authorization: Bearer <apiKey>`
(`/messages?beta=true` with `x-api-key` for `anthropic`) through urllib, so the agent's proxies apply; each request
in `FAKE_HTTP_LOG`; a 2xx plays `ok`, an HTTP error the matching error event with the body's `error.message`, an
unreachable base `provider.transport`. With `OPENCODE_MODELS_PATH` the fake lists that file's models in place of
`WB_FAKE_CATALOG`'s (as 2.3.2 does), their variants the config's only. `WB_FAKE_API=<provider>[/<id>]=<state>,…` gives a model (its own entry first; a config model too) or a provider one
of those states (or `ok`): its runs play that scenario, and a `nokey` provider drops out of `api model.list`, `models`
and `auth list`, as one OpenCode can't authenticate does. `FAKE_CALLS` (a file) logs each `api`, `models` and `auth`
call's argv (`ApiDiscovery` counts catalog reads); `FAKE_CATALOG_FAIL` makes `api model.list` and `models` fail (stderr,
exit 1). Only the fake reads `WB_FAKE_API` and `WB_FAKE_CATALOG`: discovery is driven through it, with no product-side
fake (`ApiDiscovery`, #1013); `Real`'s fixture (`test_portable.py`) runs it too, so a lookup a test misses never reaches
the user's own OpenCode. `ApiStats` (#1022) writes an API task's task.json as `wb start --role` will (`via`, `provider`,
`role`) and prices each exported session with `FAKE_COST`. **The roles** (`Routes`, #1014) run on that catalog (`ROUTES_CATALOG`, a `deepseek` provider
named DeepSeek with a stored key): the document, the change line, the gate's consumed approval (stale, another
command, another line), every gated kind (a flip, an API model, thinking on API, `--api` on the GPU, the cap), a pin
keeping the record of the user's yes, the flips, the refusals (a kept model checked again, one gone from the catalog, a
provider's passing state), the Agents switch (its line, its gate, `use` alone set), the start refused before any dialog (`API_RUNS` patched off) and its gate, `wb try`'s
refusal, an unreadable file; `Base` points `WB_ROUTES` at the
test's own file, never the user's. The mod's half (the dialog, the file-tool denies, the echo, `said-3`) is in
register.test.tsx (`routeProbe`). **Dynamic workflows** (`DynamicWorkflows`, #1041): off with no file, an unreadable
or unknown one, `wb workflows on|off` kept, the skill's two rules, and a fan-out of three (one per role, under
Claude Code, the API role set through the dialog's approval): the two GPU tasks on Ollamas on separate cards
(`models.server_devices` patched; models that don't fit one card together) let in together without `--queue`, the API
task past its provider's three held slots exit 4 then queued with `--queue` until one frees, every start line
naming its role and model (the runners are the test's: the leases taken as a runner takes them); register.test.tsx: the block (once with
`/workbench`'s text), one row per change, nothing without the file, a stored `said-2` declined.

**The spend cap's tests** (#1019): `ApiSpend` writes a task the way `wb start` will (#1016: started on a GPU model with
`spawn_runner` patched, task.json then naming the API model) and runs `runner.drive` in the test's process, the keyhole `drive`
starts (#1015) recording each `close` told a reason (`record_closes`); `ApiSpendKeyhole` drives the keyhole's `close(reason)`, `room` and one usage per
response against `_KeyholeTest`'s upstream, and its `test_real_opencode_*` runs the real OpenCode through a closed
keyhole (the 400, not retried).

**Real-OpenCode checks** (`RealOpenCode`, `RealOpenCodeSkillFiles`, `RealOpenCodeRun`, `RealOpenCodeAPI`, `ApiHarness`'s
`test_real_opencode_*`, `ApiSizing`'s and `ApiSpendKeyhole`'s `test_real_opencode_*`; skip without `opencode`, the
lane ones without `lane.abi()`). `capture_llm.py <port> <out.jsonl> [calls] [prompt tokens]` records every request,
answers tool-offering ones with the next scripted `{"name", "arguments"}`, reports `prompt tokens` read (to fill the
window), returns the compaction template's headings for a compaction request (else `compaction.failed`), and listens at
once; `CAPTURE_HEADER_DELAY` tests `headerTimeout` (#491). A path ending `/messages` is answered as Anthropic's
Messages API (SSE events, `tool_use` blocks), any other as OpenAI chat completions. For the API routes (#1011):
`CAPTURE_HEADERS=<file>` records each request's `{method, path, headers}` (GETs too; the out file keeps bodies only);
`CAPTURE_STATUS=<code>` answers with that status and the protocol's error body (`CAPTURE_STATUS_TIMES=<n>`: only the
first n), `CAPTURE_RETRY_AFTER` adds `Retry-After`, `CAPTURE_ERROR_BODY` sets the message (a key-like string);
`CAPTURE_USAGE` the usage, in OpenAI's shape, mapped to Anthropic's (`input_tokens` less the cached,
`cache_read_input_tokens`); `CAPTURE_REASONING=<s>` streams `reasoning_content` deltas (Anthropic: a `thinking`
block) that long first. `ApiHarness` checks the fake and the server, and on the real OpenCode, offline (scratch
`XDG_DATA_HOME`/`XDG_STATE_HOME`/`XDG_CACHE_HOME`/`XDG_CONFIG_HOME`, `OPENCODE_DISABLE_MODELS_FETCH=1`, a dead
`HTTPS_PROXY`, no `*_API_KEY`, the layer in `OPENCODE_CONFIG_CONTENT` pointing the built-in `deepinfra` and
`anthropic` at the server with `dummy-key`): DeepInfra posts `/v1/openai/chat/completions` with `Authorization:
Bearer`, Anthropic `/v1/messages?beta=true` with `x-api-key` and `anthropic-version`, no GET; the body's `model` is
the id after the first `/`; a step's cost is the catalog price of its tokens; a layer variant `#wb-off`
(`reasoningEffort: "none"`) sends `reasoning_effort: "none"`, no variant no field, an unknown one `provider.no-route`;
a pinned `small_model` keeps the title request on the task's model (unpinned, it goes to the provider's small model);
a 429 prints `retry` events (`retryAfterMs` only with `Retry-After`), a 401 or 402 one error at once, the body
verbatim. It takes the spike's models from the bundled catalog, else another of the provider's. The first two cover the MCP shape, skills, denials, compound
shell lines, rule order, git by path, the edit rule, PATH, the lane, `tool_output` (cuts the shell's output, not `read`'s; a project config can't lift it), and the project config handed back
with it shut out (#994). `run_calls(name, calls, *start_flags)` runs `opencode run --auto` through `agent_env` and
`lane.wrap`; refusals read `permission.rejected`, `external_directory` or `Permission denied`. `RealOpenCodeRun` is the
runner on real OpenCode (`wb start --model cap`): argv, `run --format json` events, `session export`
(`context_state`), `session list` (`find_session`), `--session`, a real compaction, the task's own OpenCode home
(no credentials in it, the user's store given no session, its commands seeing the user's `XDG_DATA_HOME`, #1010)
(CLAUDE.md's *OpenCode quirks* points here). They are the only checks of the quirks, interop and lane: a skip is not a pass.
`ApiThinking`'s `test_real_opencode_*` is one too (#1018): a `wb-off` body variant and the catalog's `none` reach a DeepSeek-shaped server as `thinking: {type: disabled}` in the agent's request, and the npm provider's V1 `wb-off`.

**The browser test** (`e2e_chat.mjs`): the real `bin/wb chat serve` on 13371 (`WB_E2E_PORT`), WebKit as an iPhone 13
and Chromium (`$CHROMIUM`, `/usr/bin/chromium`, else Playwright's) as a Pixel 7: sends land exactly once through
outages, reloads and a SIGSTOP past the 40 s watchdog; cards, plans, settings, install over https, `/api/seen` and
`/api/active` only while in use, notices, shot pairs, listen's shortlist, the keyboard, uploads, an agent's message
shown with its name and its reply through `/api/wait`, its state without the user's, a dialog for it saying so on the
card and in the push (#1042); and `sw.js` in a node `vm` (`swSays`) for a push's text.
`WebChat` runs the agents' client, `skill/agents/claude-workbench/scripts/ask_claude.py`, against its server with a
listener played in the test (#1042).

**Rate limits** (`RateLimits`, #1020): the runner in-process (`runner.drive`) on a task written as `wb start --role`
writes it (`via: api`, `provider`, `role`), the keyhole started in the test as `drive` will start it (#1015,
`on_health` → `note_api`), `_RateUpstream` the provider (a status until a time, `Retry-After`, keep-alives before
the answer); limits scaled by patching `RATE_WAIT_S`, `QUEUED_AFTER_S` and the keyhole's `hold`. Each acceptance
test carries its control (the pause patched off, the queue mark off, the cause as `guard`).

**Privacy's tests** (`Privacy`, sharing `_KeyholeTest`; #1021): an API start made as #1016's will be
(`pick_model`'s answer patched in, the GPU wait, the bench and the runner left out), local_only at start and since,
standing copies and siblings, the API lane on Landlock and the split homes (moved ones too), the start's note naming
the tools that stop, OpenRouter's routing through the keyhole to `capture_llm.py` and in the written layer, a backend
outside the order (its report line, `wb watch`'s entry, the names that don't fold), `wb resume` and `--all` past a
local-only task, the where-line, `wb doctor` saying nothing of free models.

**The keyhole's tests** (`Keyhole`, `RealOpenCodeAPI`, sharing `_KeyholeTest`): canary keys only
(`CANARY`), never a real one, and no network: `_Upstream`, a provider in the test, records each
request's path, headers and body and answers OpenAI or Anthropic SSE, an error status with a body
echoing the key, chunks spaced in time, a served model and an OpenRouter provider. `fake_auth`
stands in for `opencode auth export|list` (`FAKE_AUTH`). `RealOpenCodeAPI` runs real OpenCode
(scratch homes, `OPENCODE_DISABLE_MODELS_FETCH=1`, an environment without the user's keys) on the
built-in `deepinfra` and `anthropic` providers through the keyhole: one request upstream with the
canary, OpenCode's own with only the token, at the path OpenCode would have used upstream. The
2026-11-23 reviews' findings each have a `Keyhole` test: bodies other JSON parsers route elsewhere,
a key with a newline, bad queries and chunk sizes (`raw_request`, for what http.client won't send),
an error body cut across the key, errors inside a 200 stream (`_Upstream`'s "raw" mode), packages
the keyhole can't speak, DeepInfra's appended `/openai`, a key written as a header, a key in a
model's own entry or the V2 form (#1015's review). Its
`test_the_tasks_own_catalog_*` (#1015) takes real `api model.list` entries through `hosted()`,
`catalog_doc` and the runner's overlay: real OpenCode reads them back field for field (the `limit` capped), and a model
the binary lacks fails without the file and runs through the keyhole with it.

**API tasks in the tests** (`ApiTasks` in test_workbench.py, `ApiLease` in test_gpu.py, sharing
`_ApiTask`, #1015). `_ApiTask.start_api` is `run_wb("start", …, "--model", FLASH)` in process,
outside Claude Code so no dialog (`FLASH`, from `API_CATALOG` in `WB_FAKE_CATALOG`, its key a
canary row of `FAKE_AUTH_EXPORT`, a $100 day cap in the test's routes.json, without which the
meter stops the task before it spends, #1019). **Roles at start** (`RoleResolution`, #1016): the no-fallback
matrix (an unset role on the router's pick, an API role on its model with task.json's keys, no key
and no GPU model each exit 3 with no task directory and never the other side, Agents on API with a
role that has none, an unreadable routes.json, `--role`/`--size` each from the other, a GPU pin),
the start line, `wb status`'s via column, `wb watch --json`'s `routed`, the chat's snapshot, and a
model gone from the catalog (feedback, next, resume, the runner) and a catalog that can't be read
(never said as gone). With `spawn=False` the runner is
not spawned and `drive_here` runs `runner.drive` in this process, for a test that patches
`models` (a counted `make_resident`, a `models.resolve` returning an API model on a local
`_Upstream`, since `_build_hosted` leaves out loopback bases). The fake's `http` scenario then
sends the agent's real request to that upstream through the keyhole.
**The first run for API roles** (`FirstRunApi`, #1024): `wb setup`'s API keys line for a stored key
(and which roles use it, the Agents switch heeded), an environment key, an OAuth login, several
providers and none; the GPU lines optional only with a stored key and no local model; several roles in
one `wb route` (one line, one approval, all or nothing, the refusal naming the pair, OpenRouter set
alone, `--order` refused beside pairs, a kept OpenRouter model with no backends refused); the router's `no GPU …` refusal with the API choice only for a stored key (never while Agents is GPU), and `wb start`'s
exit 3 with it; `wb scout`'s API section (one line per provider at 300 models, a task's mean cost by
`results.row_cost`, the spend line by `savings.api_totals`) and none with no provider and no API role.

**The busy-GPU opt-in** (`BusyGpu`, #1023): with `busy` api a start the GPU would make wait runs on the
role's API model (task.json `busy`, the start line with today's spend, the role's thinking, API's
auto-fix); `wait`, Agents GPU, a `--model` and a free GPU never do; a role with no API model, full
slots, the day cap, `--local-only`, a repo kept local and an unnamed standing `copy` leave the start
as before after a note; an OpenRouter role keeps its backends (`order`), `wb route busy api` refuses
one with none, and new backends on a busy role count as newly sent (`routes._busy_newly`).

**Conventions for a new test.** Names read as sentences; one class per feature, a one-line docstring; end to end through
`self.start`/`wait_phase`/`run_wb`, asserting the phrases Claude reads and the state files; real processes and servers
on port 0 (only `chat.tailscale` and Hardware's probes are mocked); a regression test cites the incident and date; task
names `t-<slug>`; OpenCode-dependent tests through `run_calls`; kernel ones `@unittest.skipUnless(lane.abi(), …)`;
reuse helpers by import or assignment, never by subclassing a class with tests; new classes above `__main__`.

**Not hermetic.** The real HOME (`~/.claude/CLAUDE.md`; the lane tests' `~/.wb-sec-test-*`, `~/.wb-lane-test-*`,
`~/.wb-sock-*`; `/tmp/wb-run-*`), OpenCode's own session storage, 127.0.0.1:11434, real hardware. Never the real state
directory, gpu.lock or a model: safe beside running tasks.

## Shipping to main

main takes no direct push: every change is a pull request that merges once `check` is green and it is approved.
`.gitignore` ignores `/*.md` but README.md, CLAUDE.md and ARCHITECTURE.md (a tracked root file stays tracked; a new root
doc needs its own `!/` line).

- **The ruleset** (`.github/rulesets/main.json`): no deletion, no force push, a pull request with one approval (stale
  approvals dismissed on push), `check` green from GitHub Actions (integration 15368, so nobody else's status counts):
  `check.yml`'s summary job over its six `suite` shards and `mod`. No bypass actor.
- **Auto-merge for the owner only.** `owner-merge.yml` (`pull_request_target`) approves a pull request whose author and
  event sender are both user id 26800291 (ids: logins can be renamed and taken), from this repository, not a draft, at
  the head it was woken for, then arms auto-merge if nobody has. Claude sessions push as the owner. Anyone else's
  (people, Dependabot, forks), or the owner's after someone else pushed, waits for the owner by hand; a draft holds one
  back. It runs main's copy, never the pull request's code: no checkout, no `uses:`, nothing from the event in a shell
  but numbers, the head hash, the repository and the token (`tests/test_workflows.py` holds it to that).
- **Who arms auto-merge**: a merge armed with the workflow's token is GitHub Actions' push, which starts no workflow, so
  `scripts/ship.sh` arms it as the owner; when `owner-merge.yml` arms it, the nightly is main's only run.
- **`scripts/ship.sh`**, the one road to main, run only when the user has said to ship (it publishes the branch and opens a pull request as them): from a branch or worktree (a detached HEAD ships as `agent/<worktree>`),
  pushes, opens or finds the pull request, arms auto-merge with `--match-head-commit`, waits or names the failing check;
  refuses main, acts only on this repository's pull request at the commit it pushed. `--title`, `--body-file`,
  `--squash`, `--no-wait`, `--draft`. A red check leaves auto-merge armed: fix, commit, rerun. Ten failed `gh pr view`s
  in a row end the wait (exit 2), still armed (2026-11-22).
- **`scripts/github-settings.sh check|apply`** keeps the settings in git (`check` names drift, `apply` writes, `gh` as admin): the ruleset; Actions may approve pull
  requests, the default token read only; auto-merge, merge commits, delete branch on merge, update branch; only
  GitHub-owned and verified actions, pinned to commits; fork workflows always await approval; secret scanning with push
  protection; Dependabot alerts and security fixes. `owner-merge.yml` must be on main before the ruleset is first
  applied, or the owner's next pull request has nobody to approve it.
- **The emergency door**: when `check` is broken and its fix can't pass it, the owner sets the ruleset's `enforcement`
  to `disabled`, merges the fix, sets it back (`check` shows the drift). Never a bypass actor.

## Couplings: change one, change the other

- **A file a round's children load** (lane, git shim, ask server, their imports) → `runner.CHILD_CODE`, so an update under a running task is caught.
- **An `ALLOWED_COMMANDS` entry spelled with its opt-out** (`REDOCLY_TELEMETRY=off redocly lint`) → the skill's
  `reference/checks.md` (*Spell it the way the agent's shell allows*) and the shell rules' step 2, so Claude's `--test` uses a spelling the agent may run. A tool listed by
  subcommands is `LIMITED` by itself (`_entry_tool`); one reaching out but unlisted → `OUTWARD_TOOLS` by hand. A tool a
  package manager installs into the project → `context.RUNNER_TOOLS`, npx's other name in `NODE_PACKAGES` (#999); its `ALLOWED_COMMANDS`, `ASK_FIRST`,
  `INSTALL_LISTED` entries follow by themselves.
- **An experiment** (`context.EXPERIMENTS`) → `shell_rules`, `EXPERIMENT_NOTES`, `EXPERIMENT_PROTOCOL`,
  `task_experiments`, `cli.experiment_lines`, the pane row (`PROBE_STOPS`, `probeMode`, `probeStatus`, `probeSaid`, which read
  `experiment_lines`' wording, `PROBE_SETUP_NOTE`), *Experiments*. `context.PROBE_FILE_LINES` → `PROBE_STOPS`' `a file over 1,500 lines` and the
  pane row here (`tests/test_docs.py`). `context.OLD_LEVELS` → `context.setting`, `cmd_experiment`, the mod's
  `PROBE_OLD_LEVELS`. `context.PROBE_LANGUAGES`, `probe_language_names` → `probe_off`, `long_files`' `kinds`,
  `runner.outline_section`, the tests' `SEARCH_HELP`, *Only where probe can help*. What a checkout holds
  (`gitops.sparse_keeps`, `GIT_NO_LAZY_FETCH`) → both `gitops.checkout_size` and `context.probe_off`: one skipping either fetches or counts what the worktree
  won't have (#436).
  `context.PROBE_RECOMMENDED` → `; on is recommended`, which `probeSaid` reads. What on adds (`runner.output_cap`,
  `RESTATED`, `runner.add_outline`, `OUTLINE_HEAD`/`OUTLINE_MORE`) → ProbeOn's byte-for-byte off check and
  ProbeWhereItHelps.
- **`context.API_HIDDEN`** and `SECRETS`' comment on what stays readable (the `.npmrc` trade) change together; a file
  added to either side → the API lane test in `Privacy` and #992's in `Lane`, `API_HIDDEN_TOOL` (its tool) and, when
  that tool stops on an unreadable one, `API_HIDDEN_STOPS`; a new file's credential syntax →
  `privacy.holds_credential`'s matcher and `Privacy.WITH_AND_WITHOUT` (a file with one and without); a hidden file in a writable tool home → `SPLIT_MADE`, and
  `SPLIT_MADE_VARS` when a variable moves that home. **`privacy.OPENROUTER_SLUGS`** follows OpenRouter's provider list
  (`/api/v1/providers`: a display name that doesn't fold to its slug). **The where-line** (`privacy.where`'s `line`, `PRIVACY_PAGES`) is the start's where-note (`cli._api_notes`); the
  route dialog's text is `routes.change`'s line, which the mod reads from `wb route … --dry-run --json` (#1014's), never words of its own. **The local_only edit guard**
  (`localOnlyRefusal` in register.tsx) and commands.json's `repos` shape change together.
- **The keyhole's paths** (`keyhole.MODEL_PATHS`, the base mirroring the upstream's path in `layer_options`,
  `mirror_path`) and **its packages** (`keyhole.PACKAGES`) → what OpenCode posts for each package and how it sends the
  key (*API models through OpenCode*; packages/ai/src/providers): re-check with `RealOpenCodeAPI` and the source on
  each OpenCode bump. `keyhole.redact` → any new place an upstream error is shown.
- **The keyhole overlay** (`runner.keyhole_overlay`) → `write_config_layer`'s provider merge and `rules_env`'s
  restatement in `OPENCODE_CONFIG_CONTENT` (`agent_env` passes it): one without the other lets the user's content
  point the provider elsewhere; its V1 `provider.<pid>` must not meet a V2 `providers.<pid>` (*The keyhole*), and
  where the user's content has one, `keyhole_v2` says the base and token there too. It restates the task model's own
  `provider.api` and `options` as well as the provider's; a new field through which OpenCode sets a base or a key
  (re-check `migrateModel` on each OpenCode bump) → this overlay, `keyhole._keys_in`, and
  `RealOpenCodeAPI.test_no_base_of_the_users_takes_the_token_past_the_keyhole`.
- **`models.catalog_doc`** → OpenCode's models.dev v2 schema (packages/core/src/models-dev.ts: `SourceModel`,
  `SourceCost`, `api.<protocol>`) and the hosted.json fields it reads; re-check
  `RealOpenCodeAPI.test_the_tasks_own_catalog_*` on each OpenCode bump, and the fake's `OPENCODE_MODELS_PATH` listing.
- **`models.API_PARALLEL`** → `cli._slots_full` and `_slot_note`'s text, `NoLease`'s queued `last`, SKILL.md's
  dispatch line.
- **An API model's window and output** (`models.api_limits`, `API_WINDOW`, `API_OUTPUT`) are the one source for `runner.model_pins`/`api_pins` (the layer), `compaction_buffer`, `router.slice`, status `window` (the steps, `tuning._key`): read them from `m.config` (each round of an API task: status `api_entry`, `runner.pin_api`), never the catalog's `extra["catalog_limit"]` (#1017), which only `wb models --api` shows.
- **What a handoff costs Claude, measured** → the README's sentence.
- **Anything a reader looks up** → this file, CLAUDE.md if it names it, the skill if Claude uses it; README only for the
  quickstart (CLAUDE.md's *Keeping the docs true*).
- **`media.py`** (`problem`, `ready`, `auth`, `post`, `fill`, `settle`, `gpu`, `on_gpu`, `shares_gpu`, `cards`,
  `serving`, `free`, `publish`) → both `wb image` and `wb audio` (`tests.Images`, `tests.Audio`). A new media kind → its
  path in `store.PATH_VARS` and `context.private_paths`, its output dir in `cli.EVENT_SKIP` (and the mod's ears), its
  engines in `wb scout`.
- **`models.same_gpu`** (a remote server on this GPU, #750) → `pool`, `on_this_gpu` (`free_gpu`, `_others_resident`), `releasable`,
  `make_resident`, `GpuLease.step_aside`/`_unload_unshared`, `optimize.busy` and its lease, `runner._tests_gpu`,
  `media.boxes`, `media.shares_gpu`: one left out takes a second lease in one process (waits on itself) or leaves a
  model loaded.
- **`models.BOX_QUIET`, `BOX_WAIT`** (a wb remote box's turns) → OpenCode's 2-minute background move (`BG_GRACE`), which
  bounds the gap between a round's requests, and `router.ROUND_DEFAULT_S`;
  `wb start`'s note; `media.turns` (`box_quiet`).
- **`progress.DEFAULT_MIN`, `TYPICAL_MIN`** → `PROGRESS_STOPS` in `register.tsx`.
- **`ask_mcp.NO_ANSWER`'s opening** ("No answer arrived in time.") → the mod's `NO_ANSWER`, which tells one timed out unseen from one answered.
- **`store.absolute_env`, `home()`** → the mod's `absolutePath` (#454).
- **A process wb leaves running** (a runner, the bench driver, `wb optimize`, `wb chat on`'s server) → `cli.detach` (own scope where logind kills sessions), `awake=` when unattended
  (*Kept awake*), named in `cli.logout_stops`.
- **A file in a task's directory** → `context.TASK_FILES`, the names a `--sibling` can't take.
- **Where a task's worktree is** (`Task.worktree`) → `gitops._record`, `spend._theirs`, `EVENT_SKIP`, the mod's
  `codingRefusal`.
- **A prompt's opening words** → `transcript` labels, `round_asks`, `lessons`, the fake OpenCode, the tests quoting them.
- **Where a round's own prompt is** (`instructions.md` once `_start_fresh` makes prompt.md the handoff) → `store.round_prompt` (`current_task_prompt`, `round_asks`, `lessons`,
  `bench._step_prompt`).
- **A task.json `bench` key** → `Task.notify`, `results.record`, `cmd_status`, `_preempt_bench`, the mod's task
  listing, which all leave a replay out of real work; the Bench tests and the mod's replay test.
- **`cli.PROTOCOL`'s acceptance line or fold, `wb next`'s step opening** → `bench.py`, which reads them back out of old prompts (`TEST_LINE`
  without status `step_tests`, `FOLD`, `STEP_HEAD`). **The trailers `wb next` and `wb accept` write** → `gitops.step_commit`; `bundle_steps` (base..tip) must
  hold every step it looks for.
- **The mod's wake opening** (`` [workbench] Task `<name>` ``) **or Claude Code's transcript rows** (`type`, `isMeta`,
  `isCompactSummary`, `message.id`, `message.usage`, a Bash `tool_use`'s `command`, `timestamp`, `subagents/`, the line
  before a plugin's message) → `spend.py` (`WAKE`, `_prompt`, `calls`), the ClaudeSpend rows.
- **`next_step`'s `ts` or the row's `steps` count** → `spend.usage_by_step`, `results.record`, `bench.handoff_cost`.
- **`cli.PROTOCOL`** → the fold marker `\n---\nHow to work`, the `Lesson:` parser, the handbook, every prefill; `cli.WRITING_PROTOCOL` shares its fold, first bullet, `{test_line}`, ask, lane and `.proposed` bullets and `Lesson:` line (change them together; the Writing tests); `agent/WRITING.md`: every writing prompt, under 3,000 bytes (`tests/test_docs.py`).
- **`agent/HANDBOOK.md`** → every agent prompt; each token costs prefill.
- **How OpenCode is invoked or read** (`run_round`'s argv, `normalize`'s fields, the export fields `context_state`,
  `measure_speed`, `transcript` read) → `tests/fake_opencode.py` in the same commit, `RealOpenCodeRun` for a new field,
  one real run (*The tests*).
- **`Model.kind`** (#1013) → `pool()` (None for an API model, #1015: `models.holders(pool=None)` is empty,
  `cli._gpu_holders` counts no API task, as `discover()` lists none, `_gpu_note` says the provider's slots
  instead, and `runner._tests_gpu` takes None as "not this GPU's pool" and holds this machine's GPU alone
  through `verify.gpu`), `models.lease_for` and `NoLease`, `runner.CURRENT["api"]` (no
  `_Power`, no keep-warm, no `_down`, no `short_window`), `bench.api_task`, the
  router reading `discover()` only (never an API model, so exploring and `_cheaper` can't reach one),
  `cli.pick_model`'s refusal and `API_SIZES`, `bench`'s refusal, `identity`/`weights`/`keep_warm` (never a probe of a
  provider). `note_api`'s states → the keyhole's mapping of provider answers → `_api_status`'s table, `remedy`,
  `may_come_up` (*API models*). A `hosted.json` key → `_build_hosted`, `_api_model`, *Files and formats*; what a
  catalog read depends on → `_stamp`.
- **What the keyhole can hold** (`keyhole.load_key`'s refusals: an OAuth login, an external credential, request
  signing, a key only in the environment or written in a config) → `Model.key`'s states, `models.status`'s and
  `cli._route_api`'s `can't run: needs a key wb can hold outside the lane`, never "available".
- **An OpenCode fact the API harness copies** (`api model.list`, `auth list`, `-m` ids and variants, `part.cost`,
  the `retry`/`error` shapes, each protocol's path and key header) → `tests/fake_opencode.py`, `tests/capture_llm.py`
  and `ApiHarness`'s `test_real_opencode_*` together (*The tests*); its `max_tokens` cap and compaction point, `ApiSizing`'s.
- **The provider's wait** (#1020): `keyhole.RATE_HOLD_S` (300) stays under the layer's header guard (600 s, #1017),
  or OpenCode cuts a held request itself; `runner.PROVIDER_GUARDS` → the `provider` cause and `runner.SPEND` → the
  `spend` cause (`runner.stopped_by`, also status `infra`, the `stopped` event's `by`, `bench.replay`'s `error`), both
  outside `results.THE_STEP`; `api_stop` is shared with #1019's spend stop (guard `spend`); `HOLD_CODES`/`QUOTA` → `note_api`'s states and
  `_ProviderWait.on_retry`/`on_error`'s reading of OpenCode's `retry` and `error`.
- **A model server's API fields** (`models.server_facts`, `ollama_served_context`, `weights`) → `test_portable.py`'s
  `Real` tests, *Running your own server*.
- **What `wb top` reads** (`live_used`, `window`, `round`, `round_started_at`, `load_secs`, `last`; a `step` row's
  `ts`, `round`, `out`; gpu.json's `waiting`, `since`, `waiting_since`) → `top.py`, the Top tests; `--json` → *wb top*.
- **A git call on a task's worktree** → `gitops.git` (pinned, no hooks), never `git -C`; parsed output: porcelain or `-z`.
- **Permissions or the config layer** (`context.py`, `write_config_layer`) → the real-OpenCode tests, unskipped.
- **The lane** (`context.writable`, `readable`, `SECRETS`, `lane.py`) → the Lane and RealOpenCode tests on Landlock, the
  protocol's lane line, the handbook. What it leaves writable that runs later outside it (`TOOL_HOMES`, `NOT_WRITABLE`), what WSL's interop
  runs outside it (`windows_drives`, `agent_path`, `interop_gap`) → the lane's *Writes* and *Reads*, `wb doctor`. A secret in a `TOOL_HOMES` entry → `SPLIT_MADE`; one a
  tool opens at every start (helm, k6) → its path in `agent_env`.
- **`wb update`'s flags or output** (`--check --if-due`; `update available…`, `updated …`) → register.tsx's `checkUpdate`
  (toasts only `update available` and `LOCAL_CHANGES`, the opening of `patches.CHECK_LINE`) and `UPDATE` (reloads only
  after `updated`).
- **`patches.json`'s keys** (`source`, `upstream`) **and `patches.source_dir`'s default** → the mod's `upstreamRefusal`.
  **Where workbench lives on GitHub** → the mod's `UPSTREAM` (patches.py keeps no constant: `note_install` reads the
  remote's URL), CONTRIBUTING.md's `gh issue` lines. **What sends** (`wb patch send`'s name, the gh and git commands it
  runs, any new outward command) → `sendsOut`, `GH_WRITES`, `OUTWARD_TEXT`, the gate's tests in `register.test.tsx`.
  **`wb patch send`'s flags** → the mod's `sendArgs` (it reads `--title`, `--body-file`, `--draft`, `--anyway` as
  written: keep `allow_abbrev=False`). **`send-ok.json`'s keys** (`slug`, `title`, `body_file`, `body`, `draft`,
  `anyway`, `at`) → the mod's `upstreamRefusal` and `patches._approved`, together.
- **`.github/pull_request_template.md`** → `wb patch upstream` (copies it to `upstream/<slug>.md`), `_refuse_send`
  (refuses it unchanged). **The crash hint's `CONTRIBUTING.md`** → that file at the root. **`patches.CARRIED`, the
  `Scope:` trailer** (`%(trailers:key=Scope)`, `me`) → `wb patch list`, `upstream`, `_refuse_send`, `doctor_lines`,
  CONTRIBUTING.md, the skill's `reference/patching.md`. **`PATCH_BRANCH`** → `cmd_update`, `doctor_lines`, the docs.
- **The lines the skill keys on** (#1005): `wb update`'s `your pull request #<n> merged: dropped the local patch`, `was
  closed without merging: kept the local patch`, `couldn't ask GitHub about pull request`, the conflict's `(wb patch
  check <sha> first: …)`, and `wb patch check`'s five verdicts (`upstream already passes this patch's tests`,
  `upstream fails this patch's tests`, `tests didn't all run here`, `has no test of its own`, `tests don't apply to
  upstream any more`), `wb patch
  list`'s ` (no test)` and `NO_TEST_SEND` → `reference/patching.md`, CONTRIBUTING.md, the LocalPatches tests.
  **The ident `sent` matches by** (`_ident`, `_ident_row`: author, email, author date, subject) → `rebase_patches`'s
  `landed`, `_landed_pr`, which read the same fields.
- **A file read from the task directory, or one the ask server writes** → the lane's part only `worktree/`, `ask/` and
  `opencode/` (OpenCode's own home, read by wb only through `opencode session export`/`list` run in the task's lane,
  `transcript.stores`, parsed as data), checked by the runner (`ask_mcp.relay`): nothing the agent writes steers code
  outside its lane.
- **Where the agent's OpenCode keeps its sessions** (`context.opencode_home_env`) → `runner.opencode_env` (the round),
  `transcript.stores` (`export`, `find_session`, and through them `finished_export`, `wb watch`, `context_state`,
  `measure_speed`, `last_words`), the fake's `<db>.sessions`, `RealOpenCodeRun`. A variable it moves → `runner.SHIM_RESTORES`,
  which the shell shim gives back; the shim's shell and its name ↔ `runner.test_shell` (the tests run where the agent's
  commands do; OpenCode picks its flags by the name).
- **The layer's `permissions`** → the layer file alone, the project's config shut out (`rules_env`,
  `PROJECT_CONFIG_OFF`, #994); never `OPENCODE_CONFIG_CONTENT` (`CONFIG_CONTENT_MAX`); the user's own (`USERS_RULES`)
  before them. A key a project config mustn't change → `RESTATED`.
- **What OpenCode reads from a project** → `context.PROJECT_SKILL_DIRS`, `agents_files`, `PROJECT_CONFIGS`,
  `project_config_instructions`; RealOpenCode's #994 test; *What the project's own config gave, handed back*.
- **The roles** (#1014): **routes.json's keys** (`routes.normalize`) → the mod's reader (`readRoutes`, `routesValid`,
  `routesPath` with `routes.path`) and the pane's rows (#1023); **`routes.summary`** ↔ the mod's `routesSummary` (the
  context block and the echo say it; `Routes` and register.test.tsx check one fixture each); **the change line**
  (`routes.change`, `cli.start_line`) → the mod's dialog, which shows what `wb route --dry-run --json` prints (an API
start's dry run exits 3 while `cli.API_RUNS` is off; on since #1016; `wb try --model` stays refused), and
  `route-ok.json`'s `approval` (`cli._route_argv`) ↔ `routes.approved`; **a `wb route` or `wb start` flag** →
  `routeCalls` (`--m` is `--model`'s only prefix among wb start's flags; a new `--m…` flag there breaks that).
  **The pane's stop tables** (#1023: `AGENTS_STOPS`, `BUSY_STOPS`, `roleStops`, `gpuStops`, `apiStops`, `THINK_STOPS`,
  `SIZE_STOPS`; #1047's `mediaStops`) ↔ `wb route`'s and `wb model sizes`' choices (`routes.USES`, `routes.BUSY`, `VIAS`, `THINKING`, `AUTO`,
  `router.SIZES`); `parseUsd` ↔ `wb route cap`'s parser (the same forms); **`wb route --json`** and **`wb models --json`**
  → the `WbRoutes`/`WbModels` types (`media`: `WbMedia`, #1047) and `rolesNow`; `routes.BUSY`/`busy` → `routesValid`, `readRoutes`, `routesSummary`.
- **The first run for API roles** (#1024): **the router's refusal** (`router.choose`'s `no GPU model…`,
  `api_choice`) ↔ the skill's first-run trigger ("no role can run") and `wb setup`'s API keys line
  (`cli.api_key_lines`, `models.connected`); **SCOUT_NOTE's opening** (`[workbench] The weekly model check is
  due.`) → its readers, which match it (register.test.tsx); **`routes.change_pairs`' line** → the skill's
  first-run question and register.test.tsx's two-role dialog.
- **`wb watch --json`** → the mod's `LINE_STYLE`, its pose regex, `live-1`; `--last`, `JSON_LINE_CHARS` with
  `KEEP_LINES`, `LINE_CHARS`.
- **task.json's routing keys** (`via`, `role`, `provider`, `provider_name`, #1016) → the mod's row builder (`scan`,
  `modelOf`'s badge) and `chat.open_tasks` with `chat_static/app.js`'s strip; **the `routed` watch kind** → the mod's
  `pageParts` (left out of the history, as `meta`) and `pageShape`; **`routes.routed_line`** → `transcript.routed_text`
  and `routes.model_line`; **`routes.USES`/`use`** → the mod's `routesValid` and `routesSummary`.
- **A `$.state` atom's type** → its tag (`tasks-9`, `live-1`, `chat-4`, `sessions-1`, `savings-1`, `pane-1`,
  `stats-1`, `top-1`, `said-3`, `splash-1`).
- **The Tasks page's layout** → `pageParts`, `pageShape` (#963); **the modeline's look** (`MODELINE`, `claude`) →
  splash.ts's `BAR`, modeline.ts's `BAR`, the room `register.tsx` counts; **a task phase** → modeline.ts's `SAID`.
- **Stats tab folds**: `top.FOLD` → `RACK_FOLD`; `stats.STATS_MODEL_LINES` → `STATS_SHOWN`; `model_lines`'
  `more`/`older`/`variants` → `statsLines`, `cli.cmd_stats`.
- **`cli.CODING`** → the mod's `CODING_STOPS`, `CODING_RULES`, the skill's *Claude coding* bullet.
- **`savings.totals`' keys** → `savedLine`, `drawSaved`; the cost's label (no currency sign) → `wb savings`, the Stats
  tab, `savedLine`, `drawSaved`.
- **API money** (#1022): `savings.of_row`'s `kind` check is what keeps the score, card, share, milestones and badge
  GPU-only; `savings.api_totals`' keys → `stats._api_period`, `savings.api_summary`/`api_report`, `stats.api_week_line`, the mod's `apiMoney`,
  `apiPeriod`, the chat's `apiSaved`; `savings.usd` → the mod's `usd`, `apiSaved`'s; the `kind` default (`gpu`) →
  `results.api_row` (a row; `models.is_api` takes an id), `router.same_kind` (score, expected, slice, `tuning._rows`), `stats._energy`. `local tok` → `agent tok` (with an API row shown) in `results.table`, the bench
  table and the round report's `agent tokens` (an API task's) together. `WbTop`'s type is unchanged (the API fields are optional keys of `running` rows): `top-1` stays.
- **Which session a task is** → the mod's `mine` and `cli._scope`: the same rule, the home-or-`/` exception included;
  `scope()` passes every id.
- **A notice kind or its text** → the mod's `HEADLINE`, `NEXT`, both still-true rules (`_ack_all`'s, the mod's), `wb wait`.
- **A task.json or status key** → *Files and formats*; a results key a reader uses → `results.record` and defaults for
  old rows.
- **The GPU admission rule** (`models.admits`, `can_share`) → the lease, `wb start`'s refusal, `cli._gpu_holders`,
  `_gpu_note`, the Sharing tests.
- **A new event kind** → `transcript.task_entries`, `transcript.EVENT_KINDS` (which `wb log` uses; unknown kinds are
  never shown, #846, #861), *Files and formats*; `Watch` checks every `.event({… "kind": …})` literal (#865).
- **A new tuned knob** → `tuning.KNOBS`, `tuning.value(task, name)`, a ceiling in `drive`; an evidence flag (`fills`,
  `passed`) → `tuning.acted` and its row field (`peak_share`, `self_review_due`).
- **`chat.py` or `chat_static/`** → the browser test. **`/api/state`'s asks or messages** → `sw.js`.
- **A message's `from`** (`user`, `claude`, `notice`, an agent's name with `role: "agent"`) → `chat.NEWS`,
  `chat._inbound`, `chat.AGENT_RESERVED`, `sw.js`, `makeRow`, `metaFor`, `agentMeta`, `reportSeen`, `wb chat log`, the
  mod's `unread`/`agentOf`/`chatPrompt` and `owedAtTurnEnd` (`claude` only), docs/chat-api.md.
- **The web chat's API** (a path, a field, an error, a limit) → docs/chat-api.md (`test_docs` checks the paths),
  `skill/agents/claude-workbench/` (SKILL.md, `ask_claude.py`); a change that breaks a client → `chat.API_VERSION` and
  the doc's *Versions*. **What an agent token may do** → `chat.AGENT_DENIED`, the doc, the agents' skill. **What an
  agent may read** → `chat.in_view`, `chat.agent_snapshot`, `chat.AGENT_UNREAD`, `chat.agent_may_read`, the doc's
  *An agent's view*.
- **An agent's turn** (`mem.agentTurn`, `agentsFor`) → the `classic.PermissionRequest` hook, `askForArgs`, `wb chat ask
  --for --here`, the ask's `for` (`makeAsk`'s `.ask-for`, `sw.js`); **an ask settled elsewhere** (`watchAsk`,
  `askSettledHere`, `sameCall`, `withdrawAsk`) → `expired` by `interrupted`, the page's "settled in the terminal".
- **The turn-end record** (`turns.jsonl`: the mod's `chatTurnEnd`, `wb chat ended`) → `chat.taken`, `chat.wait_state`,
  `wb chat reply`'s default `re`; the `[agent <name>] … [message <id>: …]` prompt → `chatPrompt`, `AGENT`, the
  `prompt.submit` guard, the /workbench skill's *Agents on the web chat*.
- **What makes the user away** (`chat/away.json`: `listen`, `back`, `termDialogDone`, `chat.active`) → `awayNow`,
  AWAY_NOTE, `#away-when`, *The web chat*.
- **When a notice is settled** (`tool.call` `finally`, `turn.complete`, `wb chat notice --done`) → `chat.notice_done`,
  the `answered` receipt (`chat.thread`, `chat.latest`), `metaFor`.
- **A message's `about`** (`{task, n, step, round}`, `{audio, n, name, path}`) → `listen`'s prefix, `aboutTag`,
  `aboutBody`, `/api/send`'s check.
- **`wb chat ask|wait|answer`'s output** (`<id> <state>` last, `wait --json`) → `askOnChat`, `waitOnChat`.
- **AskUserQuestion's or ExitPlanMode's input** → `askOnChat` (`--detail`), app.js's `makeAsk`.
- **`$` in `register.tsx`** → only through top-level functions, or `claude plugin validate mod` fails.
- **The Claude Code version verified** → `CLAUDE_CODE_VERSION` in `check.yml`, `MIN_CLAUDE_CODE` (raise it for a newer
  call), `register.test.tsx`'s `TESTED`; the floor → *The tests*.
- **The OpenCode version verified** → `OPENCODE_VERSION`, `OPENCODE_SHA256` in `check.yml` and `nightly.yml`; the floor →
  *The tests*, the "checked against" lines here and in CLAUDE.md; a newer range → `runner.OPENCODE_TESTED`.
- **OpenCode's own prompt size** (re-measured with `capture_llm.py` after an OpenCode update or a `DENIED_TOOLS` change) → `context.OPENCODE_BASE_TOKENS`.
- **The Python floor** → the launchers' guard, `check.yml`'s and `macos`'s `python-version`, *The tests*, the README's
  *What you need* (a new OpenCode major too).
- **The merge gate** (`owner-merge.yml`, the ruleset, `check` and its `needs`, the shard count, `WB_TEST_SHARD`) →
  `tests/test_workflows.py`, `scripts/github-settings.sh apply`, *Shipping to main*.
- **`mod/` for users** → nothing: no `version` in plugin.json (*Installing*). **A release** (by hand, when the owner
  calls it) → `__version__` in `workbench/__init__.py` (`wb --version`, `wb_version`, the ask server's `serverInfo`),
  `docs/release-checklist.md` filled in as `docs/releases/<version>.md` (a green nightly on the commit, every README
  command run at the tagged commit (#422), platforms by hand, each run recorded with SHA, OS, filesystem, versions,
  hardware, result, skip reason; a skip is not a gate), a `v<version>` tag (trailing `.0` dropped, `v0.90`), `gh release
  create v<version> --generate-notes` linking the checklist. **`wb setup`'s links** → README's quickstart, *Installing*.
- **`runner.thinking_variant`** (`THINKING_OFF`, `OFF_VARIANT`, `DEFAULT_EFFORT`, `_no_off_listed`, `_off_form`, the V2 move in `_as_v2`) → `fake_opencode.py`'s variant check (`layer_variants`: V1 and V2, the layer file and the content), `transcript.model_line` (`thinking_shown`), *Thinking off by default*.
- **A model name** → never in code or the skill: OpenCode's config, the router, the weekly check.
- **A runner guard** (`out["guard"]`: `repeat`, `errors`, `no_edit`, `round`, `silence`, `think`, `handovers`, `spend`) → the mod's `GUARD_WORDS`, *Stats recording*'s list.
- **The spend stop** (#1019): `meter.stop_reason` ↔ `runner._go_on` (#1017, before each `continue` in `drive`) ↔ `runner._spend_guard` ↔ the keyhole's `close(reason)` and `room` hook (`Meter.hooks()`, which `runner.start_keyhole` passes to `keyhole.start`) ↔ status `api_stop` (the keyhole's `quota` stop writes it too, #1020; `_ProviderWait.stop` consumes it, and `Meter.begin` clears it at each round's start, whatever its guard). `events.normalize`'s step `cost` ↔ `tests/fake_opencode.py`'s `part.cost` (`FAKE_STEP_COST`) ↔ `meter.step_tokens`. A `spend` notice's `cause` ↔ the mod's `SPEND_NEXT`.
