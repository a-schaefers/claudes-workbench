# How Claude's Workbench works

The full account, for agents and contributors: every piece, what it does, how and why.
[README.md](README.md) is for people (the pitch and a quickstart); [CLAUDE.md](CLAUDE.md) is the
compact reference loaded into every session that changes the code, and points here for detail.
A change that alters what this file says (a command, a flag, a default, a guard, a file, a format)
updates it in the same commit.

"The user" is the person running Claude Code; "Claude" is the Claude Code session driving the
work; "the agent" is the local model running in OpenCode on one task.

Contents: [The pieces](#the-pieces) · [Why it's built this way](#why-its-built-this-way) ·
[A task, start to finish](#a-task-start-to-finish) · [The runner, inside](#the-runner-inside) ·
[What the agent knows](#what-the-agent-knows) · [Talking to a running agent](#talking-to-a-running-agent) ·
[Context, on both sides](#context-on-both-sides) · [Waking Claude, exactly once](#waking-claude-exactly-once) ·
[Seeing what the agent does](#seeing-what-the-agent-does) · [Models](#models-found-loaded-chosen) ·
[The skill library](#the-skill-library) · [Images](#images) · [Git, commits and projects](#git-commits-and-projects) ·
[The web chat](#the-web-chat) · [Stopping everything](#stopping-everything) · [The mod, inside](#the-mod-inside) ·
[Waiting without polling](#waiting-without-polling) · [Every `wb` command](#every-wb-command) ·
[Files and formats](#files-and-formats) · [The tests](#the-tests) · [Couplings](#couplings-change-one-change-the-other)

## The pieces

```
 user ─▶ Claude Code ────────────────────────────────────────────────────────────┐
          │ /delegate skill: plan, spec, review          workbench mod: pane,     │
          │ (skill/delegate/SKILL.md)                    status line, wake-ups,   │
          ▼                                              web chat, compaction     │
         wb ──────── writes ────▶  $WB_HOME (~/.local/state/workbench) ◀── wb events (inotify)
          │ spawns                   tasks/<name>/…  rules/<name>/  notify.jsonl  results.jsonl  chat/
          ▼                                 ▲
       runner (detached, one per task) ─────┤ writes status, events, reports, notices
          │ GPU lease, loads the model      │
          ▼                                 │
       lane.py (Landlock) ─▶ opencode run  in tasks/<name>/worktree (branch delegate/<name>)
          │      └─ ask_reviewer (MCP server, wb-ask-mcp) ─ writes the question, blocks for the answer
          ▼
       local model server (Ollama, or any OpenAI-compatible server on this machine)
```

- **`/delegate`** (`skill/delegate/SKILL.md`, linked to `~/.claude/skills/delegate`) turns
  delegate mode on for the session and is Claude's procedure in it: size the work, write the
  spec, dispatch, drive, review, accept. The user types it once, then asks for things as usual;
  the mod keeps the mode (and reminds Claude of it after a compaction or `/clear`) until a quit
  command. Only the user starts it; Claude never delegates on its own (the mod also counts a
  delegating `wb` command as the mode, for a session resumed mid-delegation).
- **`wb`** (`bin/wb` → `workbench/cli.py`) is the CLI Claude drives. Every command does one thing
  and prints a short, plain report, because Claude reads it and Claude's tokens are the scarce
  resource.
- **The runner** (`workbench/runner.py`) is a detached process per task. It drives the agent
  round after round until the task needs Claude, then exits. Nothing has to stay open for a task
  to run.
- **OpenCode** runs the local model as a coding agent, headless, in the task's own git worktree,
  inside the task's lane (`workbench/lane.py`) and with a read-only `git` (`workbench/gitguard.py`).
- **The workbench mod** (`mod/`) is a Claude Code plugin. It draws the pane, keeps a status line,
  and starts a turn for Claude the moment a task needs it, so Claude never polls.
- **The web chat** (`workbench/chat.py`, `workbench/chat_static/`) is a small page and server for
  reaching the same Claude session from a phone.

Everything talks through plain files in one state directory (`$WB_HOME`, `workbench/store.py`).
The runner, the agent's `ask_reviewer` server and the chat server never call each other or the
mod; they read and write the same files, and anything that waits sleeps in the kernel (inotify on
directories, pidfd on processes, `workbench/watch.py`) until one of them changes. The mod's only
calls are `wb` subcommands (`wb events`, `wb watch --json`, `wb status`, `wb chat ack|url|log`,
`wb stop|takeover --all`). A crashed piece loses nothing, and every piece can be inspected with
`cat`.

workbench is Python's standard library only (3.11+), plus the TypeScript mod. There is no
packaging: it runs from its clone. `bin/wb` and `bin/wb-ask-mcp` check the interpreter before they
import the package and exit 1 with one line on Python older than 3.11 (`wb: needs Python 3.11 or
newer; <python> is <version>. Put a newer python3 first on PATH.`) or on native Windows (`wb:
native Windows is not supported …; run Claude Code and wb inside WSL2`), not with a traceback from
deep inside it (the 2026-11-16 audit). Everything else starts from their `sys.executable`. No model is named anywhere in the code: models come from
OpenCode's config and the router decides, so a new model is one config entry away.

**Where Python processes start.** Five places, and each finds the package by path, not by an
installed module:

| Process | Started by | How it finds `workbench` |
|---|---|---|
| `wb` | the user, Claude, the mod | `bin/wb` puts its real path's repo root on `sys.path` (so the `~/.local/bin/wb` symlink works) |
| the runner | `cli.spawn_runner` | `python -m workbench.runner <task>`, cwd and `PYTHONPATH` = the repo root, own session, output to `runner.log` |
| the ask server | OpenCode, from the config layer's `mcp.servers.workbench` | `[sys.executable, <root>/bin/wb-ask-mcp]`, like `bin/wb` |
| the git shim | the agent's shell (`<rules>/bin/git`) | `#!<python> -I` plus an explicit `sys.path.insert` |
| the lane | the runner, before OpenCode | `python -I lane.py …`, a standard-library-only script |

`-I` drops `PYTHONPATH`, so it is used only where the path is inserted in code; never rely on
`PYTHONPATH` under `-I`.

## Why it's built this way

- **Claude's tokens are the scarce resource; local tokens are free.** So the local model does the
  typing, Claude writes the spec and reviews, and every `wb` report is short. Raw logs stay in
  files.
- **Small local models lose the thread.** They repeat a call, plan for minutes without acting,
  fill their window reading, and summarise their own context lossily. So tasks are sized to what
  the model actually finishes (its slice, learned), guards stop runs going nowhere, the runner
  hands a full session to a fresh one with exactly what it needs, and OpenCode's own compaction
  is only a backstop.
- **The summary is a claim, not evidence.** Claude reads every diff; acceptance tests gate every
  round; nothing the agent does leaves its worktree until Claude commits it.
- **The agent is a teammate with limits, not a prisoner.** The everyday tools of software work
  run without asking; what's outside (a new dependency, another directory, a network tool) is
  asked for and granted in seconds (`wb allow`). The limits that matter (git writes, credentials,
  Claude's own files, writes outside the lane) are enforced by the kernel where it can, and the
  acceptance tests run under the same limits.
- **Learned, not configured.** Which model takes which size, how big a task can be, how fast a
  model is, when to hand over: each starts as a guess and is corrected by real results on this
  machine (`results.jsonl`), so a new model or a new machine needs no code change.
- **Files, not services.** A task survives Claude restarting, a crash, or the user closing the
  terminal; any piece can be read with `cat` and fixed by hand.
- **What running local models on one ordinary GPU taught** (the reasoning behind the router's
  defaults and the weekly check):
  - A model that fits entirely in GPU memory beats a bigger one that spills into system RAM:
    those were slower, crashed more, and did no better. So the router puts models that fit first.
  - Quantization matters (higher is better), but at the same memory a larger model at about 4
    bits beats a smaller one at 8.
  - Thinking mode off works best for agent work: the model writes code instead of planning it at
    length (hence the think budget).
  - Models change fast, so once a week Claude checks what's new for the hardware and recommends
    changes; nothing is downloaded without the user's yes.

## A task, start to finish

```
 wb start ─▶ queued ─▶ running ──▶ testing ─┬─pass, or fail with auto-fix spent, or no --test─▶ review
              (GPU,      │  ▲  ▲            └─fail: auto-fix round (up to --auto-fix) ─▶ running
               load)     │  │  └─ a note, an interrupt, a nudge, a handover: the next round, straight away
                         │  └──── asking: ask_reviewer blocks until wb answer / wb allow
                         ├─▶ stuck  (a guard, wb stop)        ─┐
                         └─▶ failed (opencode died, a crash)  ─┤
 review ─▶ wb accept ─▶ accepted                               ├─▶ wb feedback ─▶ queued (a review round)
        ├─ wb next ─▶ queued (the next step, same agent)       ├─▶ wb resume ─▶ queued (a fresh session)
        ├─ wb feedback ─▶ queued                               └─▶ wb discard ─▶ discarded
        └─ wb discard ─▶ discarded            wb takeover (any open task) ─▶ review, Claude finishes it
```

**Phases** (`store.py`): `ACTIVE` = queued, running, testing, asking; `NEEDS_CLAUDE` = review,
stuck, failed, asking (`asking` is in both); `CLOSED` = accepted, discarded. Who sets each:

| Phase | Set by |
|---|---|
| `queued` | `wb start`, `feedback`, `next`, `resume`; the runner on every start ("waiting for the GPU") |
| `running` | the runner at each round's start; the ask server when a question is answered or times out |
| `testing` | the runner while the acceptance command runs |
| `asking` | the ask server while `ask_reviewer` blocks |
| `review` | the runner (tests pass; tests fail with auto-fix spent; no `--test`); `wb takeover` |
| `stuck` | the runner (a guard stop); `wb stop` |
| `failed` | the runner (opencode exited non-zero with no text; the model couldn't load; a crash) |
| `accepted` / `discarded` | `wb accept` / `wb discard` |

Inside one runner, rounds follow each other with no `queued` in between: a steer, a handover, an
auto-fix and a note arriving during the tests all go straight back to `running`. The runner exits
only on review, stuck or failed, a SIGTERM, or a crash.

**1. Claude writes a spec.** The skill tells Claude to size every task to what the model that
takes it actually finishes: one concern, no bigger than that model's slice, with the files named,
the code to imitate pointed at, exact names for anything other code relies on, and an acceptance
command that proves it's done (an existing test, or a new one the agent writes first). Bigger work
is split into steps run one after another. Planning, in Claude Code's plan mode or not, works the
same way: in delegate mode the plan the user approves is already that list of steps, each with
its size, files, exact interfaces and acceptance command, and what stays Claude's (git,
CLAUDE.md, the user's decisions, the review checks) marked apart.

**2. `wb start` sets it up** and returns at once (`cli.cmd_start`):
- It checks the task name (`^[a-z0-9][a-z0-9-]{0,47}$`, not used before by any task, open or
  closed), that the repo is a git repo, the spec (a file or `-` for stdin), the `--skill` names,
  and that `CLAUDE.md` is committed at the base (an agent without one starts blind;
  `--no-claude-md` is for throwaway repos).
- It picks the model: the router's choice for the task's size (`--size trivial|significant`,
  default significant) and the repo's stack (its main languages, from its tracked files), or
  `--model` (an id, or a fragment that names one model). No model can run: exit 3. A `--model`
  that can't run right now (server down, not pulled) is refused unless `--force`.
- It works out the limits: `--round-timeout` and `--silence-timeout` unset (or 0) take the
  model's measured budgets (`router.budgets`), as does `--think-timeout` unset (0 turns the
  think nudge off); `--checkin` defaults to 10 minutes for significant tasks, 0 for trivial.
- It makes branch `delegate/<task>` in a fresh worktree under the task directory from `--base`
  (default `HEAD`). The user's own checkout is never touched, and uncommitted changes in it stay
  out of the agent's copy (it says so).
- It writes `task.json`, round 1's prompt (the spec plus the working protocol, `cli.PROTOCOL`:
  git is read-only, the acceptance command and never weaken a test, the `ask_reviewer` budget,
  stay in scope, where it can write and how to ask for a directory, finish with a summary and any
  `Lesson:` lines), the initial status (with `--skill`), the grants (`--allow`, `--allow-path`, in
  `rules/<task>/grants.json`), and spawns the runner.
- It prints the model and why, the slice, Claude's spec record, the handed or suggested skills,
  any notes, and `next: wb wait <task>`.

**3. The runner takes the GPU** (`models.GpuLease`; *Sharing the GPU* below). Tasks run side by
side when their models fit the GPU together; otherwise a task waits as `queued` for the GPU to
itself, first come first served. Then it makes the task's model resident: it unloads every other
model Ollama holds (the user's or another tool's) except those of tasks running beside it, stops
other model servers that have a stop command, and loads or starts the one it needs. The lease is
held for every round and every test run until the runner exits; the model loads once per runner.

**4. The agent works a round.** The runner runs `opencode run --standalone -m <model> --auto
--format json --title "wb: <task>" [--session <id>] <prompt>` in the worktree, inside the lane,
with `$PWD` set to the worktree (OpenCode finds the project from `$PWD`) and the task's config
layer (`OPENCODE_CONFIG`). Rounds after the first continue the same OpenCode session, so the agent
keeps what it has read, until the session is handed over. Every event streams into
`events.jsonl` and `status.json` keeps the live summary.

**5. Guards stop a run that's going nowhere.** Small models that lose the thread repeat
themselves, so the runner stops the agent:

| Guard | Default | Set by |
|---|---|---|
| the same tool call (same input) in a row | 4 | `events.StuckDetector` |
| failing tool calls in a row | 6 | |
| tool calls since the last successful edit (an edit through the shell counts) | 40 | |
| no output at all (not counted while it waits on Claude) | 15 min | `--silence-timeout` |
| one round running too long (not counting waits on Claude) | 40 min | `--round-timeout` |

A model generating for minutes with no tool call is usually planning instead of doing. Past the
think budget (7 minutes, `--think-timeout`; 0 turns it off) the runner interrupts it with a nudge
(`runner.NUDGE`: act now, the smallest edit first, one file per call, run the tests soon) and
raises a check-in for Claude.

These limits were set on a 27B model that writes ~36 tokens a second. A slower machine gets
longer ones, learned per model (*Budgets and speed* below); none goes below its default.

**6. The tests gate the round.** After a round that ended cleanly (not stuck, not failed, not
interrupted, not handed over) the runner runs the acceptance command (`--test`) in the worktree,
through the shell, with a 15-minute limit (`--test-timeout`); the output is saved per round (`tests.txt`). If it
fails and auto-fix rounds remain (`--auto-fix`, default 2, counted since the last review round or
step), the failure goes straight back to the agent as the next round's prompt (fix the code,
don't weaken the tests), and Claude isn't woken. A note that arrives during the tests goes to
the agent first; that test result is dropped.

**7. Claude is woken with a report** (`runner.summary_text`, *The round report* below). When the
round passes (or auto-fix runs out, or there is no test), the runner writes the report and a
notice and exits. Stuck and failed rounds get the same report, with why it stopped.

**8. Claude reviews.** The skill is strict about this: the summary is a claim, not evidence.
Claude reads the whole diff (`wb diff`), checks correctness, the spec, that the tests would catch
a bug, the repo's conventions, hygiene and safety, and runs things in the worktree when in doubt.
Then one of:

| Command | What happens |
|---|---|
| `wb accept <task> -m "…"` | commits the work on its branch, removes the worktree, optionally `--merge`s |
| `wb feedback <task> --message <file>` / `--text "…"` | a numbered list of exact changes becomes the next round's prompt; the runner starts again. The skill allows three review rounds before Claude fixes it itself or discards it |
| `wb next <task> --spec <file> -m "…"` | commits the approved step on the branch and gives the next step to the same agent, in the same worktree (and session, while it has room) |
| `wb discard <task> --reason "…"` | stops it, removes the worktree and the branch (`--keep-branch` keeps it) |

Every accepted or discarded task leaves a row in `results.jsonl` (*Files and formats*). The
router, the slice, the tuning and the skill records learn from those rows. A check of workbench
itself on a scratch repo isn't evidence of how a model does real work: `wb start --trial` marks
it (its row is kept but left out by `store.results()`, and its lessons aren't gathered), and
`wb trial <task>…` marks a closed one after the fact.

## The runner, inside

`workbench/runner.py`, started as `python -m workbench.runner <task>` by `cli.spawn_runner` (cwd
and `PYTHONPATH` the repo root, `WB_HOME` set, its own session, stdout and stderr appended to the
task's `runner.log`). `main` installs a SIGTERM handler that stops the agent's process group and
raises `Stopped` (a `BaseException`, so no `except Exception` on the way swallows a stop); a stop
logs a `stopped` event (`by: reviewer`, "stopped by the reviewer"); any other exception
writes the traceback to `runner.log`, sets `failed` and raises a `failed` notice with the
traceback's tail; `pid` is cleared on the way out.

**`drive(task)`**, the loop:
1. Phase `queued`, `pid`; resolve the model (unknown: `failed`).
2. Hold one `GpuLease` for everything that follows: `lease.load()` once (the seconds go to
   `status.load_secs`), the window (`limit.context`) into `status.window`, and `tuning.tune` picks
   the task's knob values once (`status.tuned`). A load error: `failed`, with a notice.
3. Each round n: for n > 1, `fresh_reason` decides whether a fresh session takes over
   (`_start_fresh`); a one-round `keep` is cleared and passed to `run_round`, so that round never
   hands over. Phase `running`, `round_started_at`, a `round` event, then `run_round`, then
   `measure_speed`.
4. After the round, in this order: full → `_full_round` (a handover; the successor's prompt
   carries any pending notes). Interrupted, or a note pending and the round not stuck →
   `_steer_round` (the next round carries the notes). Stuck, or
   opencode exited non-zero with no text → the report without tests, phase `stuck`/`failed`, a
   notice, exit. Otherwise `run_tests`; a note that arrived meanwhile → `_steer_round`. The report
   goes to `rounds/<n>/summary.md`. Failing tests with auto-fix left → an auto-fix round (round
   n+1's prompt is the failure). Otherwise phase `review`, a `review` notice, exit.

**`run_round(task, n)`.** Writes the config layer and the agent's environment (*What the agent
knows*), prepends the skills note (`skills_note`) to the prompt argument (not to `prompt.md`),
wraps the command in the lane, and starts it in its own process group (so a stop takes the MCP
server too), stdin `/dev/null`, stderr to `runner.log`. A reader thread queues stdout lines; a
steer bell (`_steer_bell`, a `Watcher` on the task directory) queues a wake when `steer.jsonl`
changes. The loop wakes on a line or every 5 s. Per line: `events.normalize` (dropping the rest),
the session id from the first `sessionID`, the tool tally and `status.last`, every text kept, and
for each `step` the context in use (input + cache read + cache write + output) against the handover point.
Then, every pass, in this order: asking (phase `asking`) or not, and time spent asking is
neither the round's nor silence; the round's wall clock; silence; the think nudge; a guard stop (`stopped` event; before a no-edit stop it checks
`gitops.mark`, so an edit made through the shell, `sed -i` or `cat >`, resets that guard); a handover; a check-in; a
pending `--now` note (an `interrupted` event). Any stop goes through `_stop`: SIGTERM to the
process group, 10 s for the leader, then SIGKILL. The agent's pid is kept in `status.agent_pid`
while it runs, so `wb stop` can end it if the runner can't. A round that ends mid-question
clears `asking`.

A prompt (with its skills note) over `PROMPT_ARG_MAX` (100K bytes) is sent as a pointer to its
`prompt.md` instead, since Linux takes at most 128 KiB in one argument.

**The handover point** (`fresh_at`, a tuned knob: *Self-tuning*). Two checks:
- *Mid-round*, at each `step_finish`: once the context in use reaches `fresh_at × window`, the
  runner stops the agent and a fresh session takes over (`_full_round`), but only when the
  session has changed a file (`gitops.mark`, a hash of the diff and the untracked files, differs
  from when the session began) and a successor would have room to work: the context the session
  had read when it first changed something (`status.session_floor`) must sit at least
  `HANDOVER_ROOM` (a quarter of the window) below the handover point. Otherwise it carries on
  (OpenCode compacts it if it must), and Claude gets a `checkin` saying the step needs more reading
  than the window holds (`_reading_full`, once a round). Seen: twenty handovers in twelve minutes
  with no edit made; and a 16K floor under a 19K handover point handing over three times in four
  minutes, 3K of work each.
- *Between rounds* (`fresh_reason`, for n > 1, also used by `wb next` to say what will happen):
  Claude asked (`--fresh`, `wb resume`) → fresh, even with no session yet; no session → continue
  on the step's first round, else fresh ("the last session ended before it began", so a session
  that never started still hands the next round the task); `keep` → continue; the last round handed over (`context: full`) → fresh; OpenCode compacted the
  session (`session export` lists a completed compaction) → fresh; the session's last assistant
  message carried `fresh_at` or more of the window (input + cache read + cache write + output) →
  fresh. This check doesn't look at whether files changed.

**A fresh session's prompt** (`_start_fresh`, `handoff_prompt`). The raw prompt is kept as
`rounds/<n>/instructions.md`, and `prompt.md` becomes: "You are picking this task up in a fresh
session (why)…", `## The task` (the current step's prompt, `current_task_prompt`: `rounds/<step_round>/`
`instructions.md` when the step began in a fresh session, else its `prompt.md`; the spec plus the
protocol), `## The work so far` (the committed steps, `git log --oneline
base..HEAD`, and the uncommitted diff, `git diff HEAD` with new files; a diff over
`HANDOFF_DIFF_SHARE` (9%) of the window at 4 characters a token, about 12K characters on 32K, and never
over `HANDOFF_DIFF_MAX` (60K characters), is replaced by its `--stat` and "read the changed files"), and `## Now`: "Start on the task above"
on a step's first round, else the round's own instructions. Status: the old session id moves to
`sessions`, `session` and `skills_told` are cleared, a `fresh` event is logged. After a mid-round
handover the instructions are `CARRY_ON` ("Your last session filled up… re-read the files you are
changing, then finish them and run the tests"), plus what the step's rounds asked since the last
review feedback (`round_asks`, so a feedback round survives handovers), the agent's last words
(the last two texts, 1200 characters) and any notes that came in as it filled. `wb resume` writes `RESUME` the same way. `status.handovers`
counts mid-round handovers.

**OpenCode's own compaction** is a small model's summary of its session, which drops details and
can lose the task itself (seen: an agent that built CLAUDE.md's "next step" after one). The config
layer sets `compaction: {auto: true, buffer}` with `buffer = max(usable + reply − window, 0) +
STEP_GROWTH` (usable: `limit.input`, else context − output; reply: `options.max_tokens`, else
`limit.output`; `STEP_GROWTH` 4096), so OpenCode compacts only past the handover point, leaving
room for one reply at the output cap and one step's growth (`compaction_buffer`). `fresh_ceiling`
is the same point as a share of the window: 62.5% on a 32K window with a 4K output limit and an
8K cap; the tuner never tries a handover point past it. A model without `limit.context` gets no
mid-round handover, no compaction setting and no ceiling.

**Notes, nudges and interrupts.** `steer.jsonl` holds every note (`{ts, text, now}`), written by
`wb steer`, by `wb allow` (to restart a running agent with its new rule) and by the runner's own
nudge (`by: workbench`); `status.steer_taken` counts the delivered ones. A round carrying notes (`_steer_round`)
starts with `INTERRUPT_HEAD` ("Claude, your reviewer, interrupted you. Stop what you were doing
and do this first; it overrides anything earlier…") when it interrupted, else `NOTE_HEAD` ("A
note from Claude, your reviewer, before you go on…"). The nudge is a `now` note, sent at most once a
round, so it ends the round like an interrupt (an `interrupted` event `by: workbench`) and the
next round starts with it; `status.nudges` counts them, and each raises a `checkin` ("the runner
nudged it: N min of thinking with no tool call"). A round right after a nudge that again passes
the think budget with no tool call and no text is stopped as stuck ("… even after a nudge").

**Check-ins** (`checkin_text`), every `checkin` minutes of a round (not while asking): `task X ·
label · round n · running <time> · ~Xm left (typically Nm)`, then since the last check-in: tools
used and failed, tokens read and written, `now:` the last action, the agent's latest words (400
characters), the files changed so far, and a NOTE naming changed files the spec never mentions
(`outside_spec`: a changed path or its basename that the step's prompt doesn't contain; silent
when the spec names none of them).

**Tests** (`run_tests`): `/bin/sh -c` in the worktree, inside the agent's lane with the agent's
environment (`agent_env`: the git shim, credentials dropped), its own process group (killed whole on a timeout), timeout `test_timeout` (`wb start
--test-timeout`, 900 s), the output decoded, the last 40 lines kept as `status.tests`
`{ok, exit, tail, round}`, the whole output in `rounds/<n>/tests.txt` (`$ cmd`, `exit: N`, output).

**The round report** (`summary_text`), line by line:
1. `task X · label · round n · M.m min · local tokens IN in / OUT out` (this round; input
   excludes cache reads).
2. `agent context: U.UK of WK (P%)[, compacted Nx][; the next round starts a fresh session]`.
3. `STOPPED: why` or `opencode exited N (see …/runner.log)`, when either.
4. `tests: PASS|FAIL (`cmd`, exit N)`, the last 25 lines on a failure, `auto-fix rounds used: a/b`.
5. `tools: name×n …`, then up to four `tool error:` lines.
6. `agent's summary:` and the agent's last text (2500 characters).
7. `files changed:` (`git status --short`), a NOTE for files outside the spec, a NOTE when the
   agent changed Claude's files (`claude_files_changed`: CLAUDE.md, CLAUDE.local.md, AGENTS.md,
   `.claude/`), and the diffstat's last line against the task's base.

**Speed** (`measure_speed`): after every round, the session export's messages created since the
round began give write speed and read speed samples (`router.record_speed`).

**Prompt text other code depends on.** Each prompt opens with a head that something else matches
by prefix; rewording one silently changes those:

| Text | Written by | Read by |
|---|---|---|
| `\n---\nHow to work` (`cli.PROTOCOL`) | `wb start`, `wb next` | `transcript` folds it away |
| `Lesson:` lines in the agent's summary | the protocol asks for them | `lessons` |
| `Review feedback from the senior reviewer` (`lessons.FEEDBACK_HEAD`) | `wb feedback` | `lessons` (feedback items), `round_asks` (stops there), `transcript` (label) |
| `The acceptance tests fail` | the auto-fix round | `transcript` |
| `You are picking this task up in a fresh session` | `handoff_prompt` | `transcript`, the fake OpenCode (`fill`) |
| `Step N of this task is done and committed` | `wb next` | `transcript` |
| `INTERRUPT_HEAD`, `NOTE_HEAD` | `_steer_round` | `transcript` |
| `NUDGE` (`without a tool call. Stop planning`) | the think nudge | `transcript` (labelled the workbench's, not Claude's) |
| `Claude picked these skills`, `Claude handed you more skills`, `Claude took back:` | `skills_note` | `transcript.SKILLS_HEADS` (skips the note to label what follows) |
| `CARRY_ON`, `RESUME` (first 60 characters) | `_full_round`, `wb resume` | `round_asks` skips them |
| `agent's summary:` | `summary_text` | `lessons` |
| `--title "wb: <task>"` | `run_round` | `transcript.find_session` |

The skills note is prepended to the prompt argument; the transcript skips past it (to its first
blank line) before matching the label, so the prompt keeps its own.

## What the agent knows

An agent should start a task knowing what Claude would know in the same repo, and nothing that
would distract it. The per-task OpenCode config layer (`<state>/rules/<task>/opencode.json`,
written before every round by `runner.write_config_layer` from `context.layer`) gives it
`instructions`, `permissions`, `skills`, the `compaction` setting and the `ask_reviewer` server.
Everything in `rules/<task>/` (the config, the instruction copies, the git shim) lives outside the
agent's lane, so it can't rewrite its own rules for its next round.

**Instructions** (`context.instruction_files`), as absolute paths OpenCode loads into the system
prompt, in this order:
- the workbench handbook (`agent/HANDBOOK.md`), copied fresh into `rules/<task>/HANDBOOK.md`
  every round, so its path never points the agent at the workbench repo (a path there once led an
  agent to go looking for plans): unattended work, read before editing, imitate the codebase,
  smallest diff, never guess an API, never weaken a test, change approach after two failures,
  stay in the working directory, leave every git write and deploy to Claude, an honest final
  summary. Every agent prompt carries it, so it stays short (about 1.3K tokens);
- the user's `~/.claude/CLAUDE.md` (`$CLAUDE_CONFIG_DIR` moves it), as a copy in `rules/<task>/`,
  because the agent can't read Claude's directory;
- the repo's `CLAUDE.md`, `.claude/CLAUDE.md` and `CLAUDE.local.md`: the worktree's copy when
  it exists there (committed), else the user's checkout's (local or uncommitted), so an
  uncommitted edit to a committed file isn't seen;
- the tracked `.claude/rules/**/*.md` from the worktree;
- `context.md` in the task directory: a map of nested `CLAUDE.md`/`AGENTS.md` files in
  subdirectories (tracked, not under `.claude/`), "Before you change files under one of these
  directories, read its file and follow it", instead of loading them all.

OpenCode reads `AGENTS.md` itself but never `CLAUDE.md`. Files are deduplicated by content
(SHA-256), seeded with the worktree's `AGENTS.md`, so a `CLAUDE.md` identical
to `AGENTS.md` (or to an earlier file) isn't loaded twice.

**Denied tools** (`DENIED_TOOLS`), which also leave the prompt (it shrinks from about 9K to 3.5K
tokens, which a local model reads on every request): `question` (nobody answers it headless),
code mode (`execute`) and the `opencode_*` tools (they can move the session or remove worktrees;
denying `execute` turns the `opencode_*` tools into direct tools, so they are denied too), `subagent`
(a second agent inside the task, beside the GPU accounting), `*worktree*`, web search, web fetch and
image generation.

**Commands, an allowlist** (`context.shell_rules`). OpenCode's `shell` permission rules are
checked against each simple command of a line (pipes, `&&`, loops, `$(…)`), its raw text as the
resource. OpenCode anchors each pattern, lets `*` match anything (spaces and `/` too), treats a
trailing ` *` as optional (`npm install *` also matches `npm install`), and the last matching rule
wins; no match means ask, which `--auto` allows. The rules, in order:
1. deny `*`;
2. allow `c` and `c *` for each of `ALLOWED_COMMANDS` (about 215: the shell's builtins and
   everyday tools, files and text, archivers, builds and compilers, test runners, interpreters,
   package managers, `bash`/`sh`, `sqlite3`, `git` (the shim)); not on it: `curl`, `wget`, `ssh`,
   `rsync`, `docker`, `systemctl`, `opencode`;
3. allow `./*`, `../*`, and `<dir>/*` for every directory of the lane but `/dev` and `/proc` (a program the agent built
   runs by its path anywhere it can write: a debug binary in the temp directory was refused
   though `bash` could run it);
4. allow `PROJECT_BINS`, what a project builds or installs for itself, by its path:
   `.venv/bin/*`, `venv/bin/*`, `node_modules/.bin/*`, `build/*`, `target/debug/*`,
   `target/release/*`, `bin/*`, `out/*`, `dist/*`, `zig-out/bin/*`;
5. deny `ASK_FIRST`: adding a dependency (`pip install *`, `npm install *`, `npm add *`,
   `pnpm i *`, `yarn global add *`, `bun i *`, `deno add *`, `deno install *`, `cargo add *`,
   `go get *`, `dotnet add * package *`, …) and fetching a package to run it (`npx -y *`,
   `npx --yes *`, `npm exec *`, `pnpm dlx *`, `yarn dlx *`, `bunx *`, `uvx *`);
6. allow `INSTALL_LISTED`, installing what the project already lists, because with the optional
   trailing ` *` the rules above also refuse the bare forms: `npm install`, `npm i`, `npm ci`,
   `pnpm install`, `pnpm i`, `bun install`, `bun i`, `yarn`, `yarn install`, `pip install -r *`,
   `pip install -e .` (and `pip3`, `python -m pip`, `python3 -m pip`, `uv pip` forms);
7. allow the user's standing patterns (`commands.json` `allow`) and the task's grants
   (`rules/<task>/grants.json` `allow`: `wb allow`, `wb start|next --allow`); a `*` there lifts
   everything above;
8. deny the hard limits (`DENIED_SHELL`): `_HARD` (`sudo`, `doas`, `pkexec`, `su`, `gh`, and the
   real git by its path, `*/git`), each bare and with ` *`, and each again behind the
   `_WRAPPERS` (`env`, `env *`, `xargs`, `xargs *`, `nohup`, `nice`, `nice *`, `time`, `command`,
   `exec`, `timeout *`, `stdbuf *`, `setsid`), so `env gh …` and `timeout 5 sudo …` are refused.

A refused command comes back to the agent as "Permission denied"; the handbook and the
`ask_reviewer` tool tell it to ask with `command` set, which raises a PERMISSION question naming
the `wb allow` that grants it. These patterns are a guardrail on the agent's own choices, not a
sandbox: they match the command as written, so `bash -c '…'`, make or any interpreter can still
run something the list refuses. What must hold is held below the patterns: no sudo (the lane sets
no-new-privileges), git read-only (the shim, and the repo's git directory is outside the lane), no
push or `gh` with the user's keys (credentials are unreadable and dropped from the environment, so
gh finds no login).

**Paths, a lane** (`context.writable`, `context.readable`, `context.external_rules`, `lane.py`).
- *Writes.* The lane is the task directory (the worktree and the files the ask server writes),
  the temp directories (`gettempdir()`, `/tmp`, `/var/tmp`), `/dev`, `/proc`, the build caches and
  toolchain homes under `$HOME` (`TOOL_HOMES`: `.cache`, `.cargo`, `.rustup`, `go`, `.npm`, `.bun`,
  `.deno`, `.yarn`, `.pnpm-store`, `.local/share/pnpm`, `.local/share/uv`, `.gradle`, `.m2`,
  `.nuget`, `.dotnet`, `.gem`, `.julia`, `.cabal`, `.stack`, `.ghcup`, `.platformio`, `.conan2`,
  `.hex`, `.mix`, `.sbt`, `.ivy2`, `.local/lib`) and wherever the environment moved them
  (`TOOL_HOME_VARS`, each that is set: `CARGO_HOME`, `RUSTUP_HOME`, `GOPATH`'s first entry,
  `GOMODCACHE`, `GOCACHE`, `npm_config_cache`, `PIP_CACHE_DIR`, `UV_CACHE_DIR`,
  `GRADLE_USER_HOME`, `BUN_INSTALL`, `DENO_DIR`, `MIX_HOME`, `HEX_HOME`), OpenCode's own data
  (`.local/share/opencode`, `.local/state/opencode`, and `opencode` under `$XDG_DATA_HOME` and
  `$XDG_STATE_HOME` when set), `$XDG_CACHE_HOME`, `$XDG_RUNTIME_DIR`, `$WB_HOME/notify.jsonl` and its lock (the ask server raises questions), and
  the extra paths (`commands.json` `paths`, the task's grants `paths`), each with its real path too, keeping
  only those that exist. No entry may resolve to `/` or the home directory (this covers
  `commands.json` `paths` and the grants too).
- *Reads.* Everything is readable except where credentials and the user's own records live:
  `context.SECRETS` under `$HOME` (`.ssh`, `.gnupg`, `.password-store`, `.netrc`,
  `.git-credentials`, `.config/gh`, keyrings, `.aws`, `.azure`, `.config/gcloud`, `.kube`,
  `.docker`, `.pypirc`, cargo and gem credentials, browser and mail profiles, shell history,
  `.claude`, `.claude.json`, and the like) and Claude's directory; and of the home directory's own
  entries only the dotfiles, `bin` and `go` are readable, plus the task's repo, workbench itself
  and the lane, not the user's documents or other projects. Landlock only allows, so `readable()`
  walks down from `/` listing the entries beside each excluded one. Found by trying: `python3`
  running gh inside the lane was logged in, and the SSH keys that push were readable.
- *The kernel's side* (`lane.py`, Linux Landlock, no root): the runner starts OpenCode as
  `python -I lane.py --write P… [--reads --read P…] -- opencode …`; it restricts itself and execs,
  so OpenCode, its shell and everything they run inherit the limit. Handled rights: file writes,
  removes and every make, plus refer (ABI 2: moves inside the lane work) and truncate (ABI 3), and
  file reads when `--reads` is given. Directories get every right; files only write, read and
  truncate. It also sets no-new-privileges, which disables setuid programs (sudo, su). Execution,
  directory listing and the network stay free. It fails closed: when Landlock is there but the
  lane can't be drawn, it prints `workbench lane: the kernel refused the lane (<error>); not
  running <cmd>` to `runner.log` and exits 126. Without Landlock (macOS, an old kernel, or an
  architecture whose syscall numbers differ: `lane.UNIFIED` lists the ones it runs on, x86_64,
  i386/i686, aarch64, armv7l/armv6l, riscv64, ppc64/ppc64le, s390x, loongarch64) it execs
  unrestricted. `wb doctor` draws a real lane and tries a write outside it: `ok agents write only
  in their lane (Landlock ABI N)`, `BAD the lane didn't hold a write outside it: …`, or a `--` line
  without Landlock.
- *OpenCode's side* (`external_rules`): its `external_directory` permission, checked for its file
  tools' paths and the shell's working directory and `cd` targets (not other arguments), is deny
  `*`, then allow `<dir>/*` for the lane, `READABLE` (`/usr`, `/etc`, `/opt`, `/lib*`, `/bin`,
  `/sbin`, `/proc`, `/sys`, `/dev`, `/nix`, `/run/current-system`, and macOS's `/Library`,
  `/System`, `/Applications`) and the handed skills' directories. This is all that applies
  without Landlock.
- *Asking for more.* A refused write is "Permission denied" (the protocol says so, and not to
  probe it); the agent calls `ask_reviewer` with `path`, which raises a PERMISSION question naming
  `wb allow <task> --path <dir>`. That grants read and write (`--always` keeps it in
  `commands.json`; `wb start --allow-path` grants it up front). `_lane_dir` refuses `/` and the
  home directory, resolves the path and creates it; `*` turns the lane off for the task, as
  `"paths": ["*"]` in `commands.json` does for every task.

**Its environment** (`runner.agent_env`) is that of the `wb` command that last started its runner
(`start`, `feedback`, `next`, `resume`) less credentials and the runner's `PYTHONPATH`
(`SECRET_ENV`: names containing `TOKEN`, `SECRET`, `PASSW`, `CREDENTIAL`, `API_KEY`, `ACCESS_KEY`,
`PRIVATE_KEY`; the `ANTHROPIC_`, `CLAUDE_`, `GH_`, `GITHUB_`, `GITLAB_`, `AWS_`, `AZURE_`,
`GOOGLE_`, `OPENAI_`, `HF_` prefixes; `SSH_AUTH_SOCK`, `SSH_AGENT_PID`, `GPG_AGENT_INFO`,
`DBUS_SESSION_BUS_ADDRESS`), plus `PWD` (the worktree), `OPENCODE_CONFIG`, `WB_TASK`, `WB_HOME`,
`PYTHONDONTWRITEBYTECODE=1` (no `__pycache__` in the diff) and the shim's `PATH`. A provider that
needs a key from the environment names it in `commands.json` `"env": [names]`, which passes it
through.

**The task directory is writable** (the ask server writes the task's status there), which is why
the grants and the rules live in `rules/<task>/`. The acceptance command runs in the same lane and
environment as the agent, so code the agent wrote gets no more rights when the runner tests it.

**Claude's files.** The `edit` permission denies `*CLAUDE.md`, `*CLAUDE.local.md`, `*AGENTS.md`,
`.claude/*` and `*/.claude/*`, and the skill library (`<library>/*`), so OpenCode's file tools
can't change them; a change made from the
shell shows in the round report.

**Git, read-only** (`gitguard.py`; *Git, commits and projects*). `runner.agent_env` writes a `git`
of its own into `rules/<task>/bin/` and puts it first on `PATH`.

**Skills**: the repo's own `.claude/skills`, plus the library skills Claude handed this task (one
directory each in the config's `skills` list). The user's personal `~/.claude/skills` (including
synced Claude app skills, `synced/*/*/SKILL.md`) are denied by name (`denied_skills`): an agent
once read a personal skill as a "newer task" and went off into the user's files. A name the
worktree's own `.claude/skills` also has stays allowed (the project's skill wins); `delegate` and
task.json's `deny_skills` are always denied. `wb skill add` refuses the personal names.

**`ask_reviewer`**, an MCP tool for one blocking question to Claude (*Talking to a running agent*).
OpenCode lists it as `workbench_ask_reviewer`.


## Talking to a running agent

Claude is the driver, not a spectator, and the agent is a teammate it can reach at any time.

**Questions** (`workbench/ask_mcp.py`, launched as `bin/wb-ask-mcp`). A stdio JSON-RPC MCP server
OpenCode starts with each `opencode run` (so once a round), configured as
`mcp.servers.workbench` (`type: local`, `codemode: false` so it's a plain tool, `timeout.startup`
15 s, `timeout.execution` the ask timeout plus a minute, `environment` `WB_TASK` and `WB_HOME`).
It answers `initialize` (echoing the client's protocol version, else `2025-06-18`), `ping`,
`tools/list` and `tools/call`; one tool, `ask_reviewer {question, context?, command?, path?}`. A
call writes `ask/<id>.json` (`{id, ts, round, question, context, command?, path?}`), sets phase
`asking` and `status.asking`, logs an `ask` event, raises a `question` notice (`Q: …`, or
`PERMISSION to run: `cmd`` / `PERMISSION to use the directory D (outside its lane)` with the `wb
allow` that grants it), and blocks (a `Watcher` on `ask/`) until `ask/<id>.answer` appears or the
ask timeout passes (`--ask-timeout`, 20 minutes). Then it restores the phase and returns the
answer (with an `answer` event), or, on timeout, "No answer arrived in time. Make the most reasonable
assumption…". Budgets count the task's `ask/*.json` files, across all steps: plain questions up to
`--ask-budget` (2), and command or path requests together up to `PERMISSION_BUDGET` (8), apart
from the questions; past either, the tool returns a refusal without asking. The prompt `wb next`
writes names the questions left, not the whole budget. A `tools/call` runs on its own thread
(replies written under a lock), so `ping` and the rest are answered while a question waits. The silence guard
doesn't count the wait. `wb answer <task> --text|--file` answers; `wb allow` answers a pending
question too.

**Granting.** `wb allow <task> "<pattern>"… [--path DIR…] [--always]` adds to the task's grants
(`rules/<task>/grants.json`, through `context.grant`; and to `commands.json` with `--always`, which
refuses a `commands.json` that doesn't parse), answers any pending question with
"Claude allowed … Run it again now and carry on.", and when the agent is running, also queues that
as a `--now` note so the agent restarts on its session with the new rule (the rules are read when
OpenCode starts).

**Notes.** `wb steer <task> --text "…"` (queued, running, testing or asking, with a live runner)
queues a note the agent gets when its current run ends, before the tests. `--now` interrupts it:
the runner is woken by the note itself (`_steer_bell`), stops the agent within seconds and
starts the next round with the note first, marked as overriding anything earlier. The next round
continues the session unless the between-rounds check hands it to a fresh one; during the tests
there is nothing to interrupt, so the note waits for them.

**Check-ins.** A significant task raises a `checkin` notice every 10 minutes of a round while it
runs (`--checkin MIN`, 0 off; off by default for trivial tasks) with a digest of what changed since
the last one (*The runner, inside*). Claude tells the user how it's going in two or three lines,
logs the same with `wb note` (a `captain` event: a "captain's log" line in the transcript), and
steers if it's drifting. Nudges and a reading-full session raise check-ins too, even with check-ins
off.

**Taking over.** `wb takeover <task>` stops the agent and sets `review` ("Claude took over"):
Claude finishes in the worktree and accepts, or hands back with `wb feedback`. `wb stop <task>`
just stops it (`stuck`, "stopped by the reviewer"; the worktree is kept), and `wb resume <task>`
continues a stuck or failed task in a fresh session with `RESUME` and what the step's rounds asked,
not counted as a review round.

## Context, on both sides

Local models work in a small context window, and so, in its own way, does Claude.

**The agent's.** Rounds continue one OpenCode session. The runner reads the context every step
reports and hands a full session to a fresh one with exactly what it needs (*The runner, inside*:
the handover point, the two checks, the handoff prompt). OpenCode's compaction is set as a backstop
just past the handover point. Reports show the fill (`agent context: 19.4K of 32K (60%)`) so
Claude sees it coming. `wb feedback --fresh` (or `wb next --fresh`) forces a fresh session (a
confused agent); `--keep-context` asks to keep it at the next round's start. A session stays
bound to the directory it was made in (continued or forked from another worktree, the model is
still told the first one), so context carries over only within a task (rounds, `wb next`), never
to a new one.

**The slice.** How much one task should hold on a model is learned like the router's choice
(`router.slice`). The first guess scales with the window it is served: three files and about 150
new lines on 32K (what was measured), proportionally more on a bigger window, up to four times
that (`GUESS_MAX`). Each step that closes (`wb next`, `accept`, `discard`) records its files and
added lines (`results.step`) and how it went: badly when its session was compacted or filled up
reading before any change, the agent got stuck, or it needed two review rounds the agent caused
(`BAD_REVIEWS`; a round marked `--spec-gap` is Claude's, not the agent's); well when it landed
short of that. A handover is counted but not held against the step: it is how the runner gets
through a big one, and its cost shows in the task's time. The biggest step that went well raises
the slice past the guess; the smallest that went badly lowers it below its size. `wb models`,
`wb start` and `wb scout` show it (`slice: ~3 files, ~150 new lines per task (32K window; 4 of 5
measured steps went well, 1 badly)`). Check-ins and reports name changed files the spec never
mentions (`NOTE: the spec doesn't name src/util.c`), where an agent's drift shows first.

The window itself comes from what Ollama serves the model (*Managing the set*): `wb model add`
writes it, with an output budget of an eighth, and `wb doctor` flags an entry that claims more
than Ollama serves (prompts would be cut off) or less (tasks could be bigger).

**Claude's record as a spec writer.** Most review rounds trace back to the spec: a rule with no
test, two rules that conflict, a name taken from memory. The skill has a checklist to go through
before `wb start`. When a round was the spec's fault anyway, Claude says so with `--spec-gap
"what it missed"` (on `feedback`, `next`, `accept`, `discard`; stored in `status.spec_gaps` as
`{round, text}` and counted in the results row). That becomes a `spec` lesson for Claude's own
checklist, not an agent skill, and `wb start` shows the record as the next task goes out
(`results.spec_gap_line`: `your specs: 2 of the last 10 task(s) needed a round for a spec gap;
last: …`).

**Self-tuning** (`workbench/tuning.py`). A setting whose best value depends on the model is
learned per model and window on this machine, not fixed. `KNOBS` holds them; the first is
`fresh_at`, the handover point (default 0.6, candidates 0.4 to 0.8). Earlier means more fresh
sessions, each re-reading files; later means working in a crowded context, where a model gets
sloppier. `tune(task, ceilings)` picks each knob once per task (`status.tuned`, with
`status.explored` and `status.tuned_max`): usually the best value so far, while one task in five
(`EXPLORE`) tries the value next to it. When a task closes, its value scores the result (`reward`):
1 for accepted, divided by one plus the review rounds the agent caused (every step's), less
`COMPACT_PENALTY` 0.25 if OpenCode still had to compact, and up to ± `SPEED_WEIGHT` 0.15 for being
faster or slower than that model's usual task. Discarded scores 0; a discard Claude marks
`--spec-gap` is its own miss and counts for nothing, here or in the router's and the skills'
records (`judged`). Each value's mean is smoothed toward the others' (`PRIOR`, two tasks' worth),
so one lucky task moves nothing, and the pick walks one step at a time toward whatever scores
best. Values past the model's compaction point (`runner.fresh_ceiling`) are never tried. Trial
runs aren't evidence. `wb tune` shows each model's record and pick; `$WB_HOME/tuning.json` pins a
value (`{"fresh_at": 0.6}`, used as given, ceiling or not). A new knob: add it to `KNOBS`, read it
with `tuning.value(task, name)`.

**Claude's.** `wb` holds the delegation state, so Claude's context doesn't have to. Whoever
compacts Claude's conversation while tasks are open (the engine, `/compact`, the mod), the mod tells the summary to
keep the user's goals and decisions, the tasks and what each needs next, and to drop finished
tasks' diffs and reports. At a task boundary (one just closed, none waiting for Claude) the mod
compacts early once Claude is halfway to the engine's own compaction (`WB_COMPACT_AT`, a
percentage; 0 turns it off), so a long session sheds detail at a clean point instead of
mid-review. After a compaction or `/clear`, a context block lists the open tasks from `wb status`.

**What Claude learns about the user** (`wb notes`). Claude Code keeps its own memory per project,
so what Claude learned about how the user likes delegation run in one repo would be gone in the
next. When the user says something that holds anywhere (their review bar, whether to merge, how
much to ask them), Claude keeps it with `wb notes add "…"` (one sentence, at most 300
characters; at most 20 notes) in `$WB_HOME/notes.md` (`- ` lines, under `notes.lock`). Every
session in delegate mode, in any project, is handed them in a `user-notes` block. `wb notes`
lists them and `wb notes drop N…` removes ones that have stopped holding. A fact about one repo
goes in its CLAUDE.md instead.

## Waking Claude, exactly once

The runner and the ask server append notices to `$WB_HOME/notify.jsonl` (`Task.notify`: `{ts,
task, kind, text, round?, id?}`). Kinds: `review`, `stuck`, `failed` (the round report as text;
a load failure or crash without a round), `question` (with the question's `id`), `checkin`
(periodic, a nudge, a reading-full session). `wb` itself never writes one. Each task's `acked`
file holds the time up to which Claude has had its news.

- **Ears.** The mod runs `wb events`, which prints a line the moment anything in the state
  directory changes (*Waiting without polling*). Each line makes the mod re-read the tasks.
- **Still true.** For each task, the newest notice under an hour old and past its `acked` mark
  is delivered only while it's still true: a check-in while the task is queued, running or
  testing; otherwise while the phase matches the kind (`question` ↔ `asking`). A stale one (a
  review for a task since discarded or sent back) is acknowledged and dropped.
- **Held mid-turn.** While Claude's main loop is mid-turn (`mem.busy`, set by `turn.start` or any
  `tool.call`) the wake is held (a prompt submitted then would queue and arrive stale) and
  checked again on every poll. Otherwise the mod writes `acked` first, shows a toast, and submits
  a prompt: `[workbench] Task <n> <headline>.`, the notice's text, and the next commands to run
  (`NEXT[kind]` in `register.tsx`; a PERMISSION question names the `wb allow` that answers it).
- **Acting acknowledges.** `feedback`, `next`, `answer`, `allow`, `note`, `resume`, `takeover`,
  `accept`, `discard` and `stop --all` acknowledge the task (`cli._ack_all`), but the mark stops
  just short of news still true: when an unacked notice of the kind the phase stands for exists
  (`question` while asking, `checkin` while queued, running or testing, else the phase's name),
  the mark stops before the oldest one, else it moves to now; it never moves back. A note written seconds
  after a failure once swallowed the failure notice for an hour.
- **The safety net** (`rewake`). A task that has waited on Claude (review, asking, stuck, failed)
  for a minute after its last status change with no notice delivered wakes Claude anyway, once,
  with `wb report <n>` and the next commands.
- **Without the mod.** `wb wait [task]` blocks until there's news past a task's `acked`, prints
  `[kind] task` and the text, and acks it (a review, stuck or failed notice for a task that is
  live again is skipped). A named wait on a closed task exits 2; on a task
  already waiting whose notice was taken elsewhere it prints it again "(already delivered once)"
  rather than block forever. `--timeout` exits 2 when it passes.
- **Off switch.** The plugin's `wake` setting (`on`/`off`) stops the prompts and keeps the pane.

The mod runs in every Claude Code session with the plugin and the state directory is global: the
first session to deliver a notice wins.

## Seeing what the agent does

- **The pane** (opened by `/delegate`, or once per load when a task runs, and closed by a quit command;
  `/delegate-pane` opens it any time) lists every task: phase, round, model, test result, age and
  its last few events. Under the list it follows one task (the newest queued, running, testing
  or asking one, else the newest waiting for Claude) as a live transcript, scrolling with the
  newest lines unless the user scrolls up. At its foot stands the apprentice, the local agent
  drawn as a small robot in a hard hat: it swings a wrench while it works and raises its arms when
  a task is ready for review; its chest lamp is the phase's colour, and its speech bubble says
  what it is doing now and how long the task has left. A short pane gets its face on one line
  instead. The web chat's link is pinned at the bottom.
- **The status line** reads like `⚒ wb · add-scores running r2 · fix-typo asks · 1 awaiting
  review`, or `⚒ wb · delegate mode` with nothing open.
- **`wb watch [task]`** (`workbench/transcript.py`) is the same transcript in a terminal.
- **`wb status`**, **`wb log [-f]`**, **`wb report [--round N]`** and **`wb diff [--stat] [--all]
  [paths]`** are the compact views Claude itself uses. `wb status` marks a task whose runner died
  without saying so with a `?`.

**The transcript** (`transcript.follow`). It merges, every loop, the OpenCode session export of
the current session and every earlier one (`status.sessions`) with the task's events, sorted by
time, each entry printed once (keyed by message and part; a part's `start` and `end` print
separately). `opencode session export <id>` runs from `/` (30 s); with no session id yet,
`opencode session list` in the worktree finds the one titled `wb: <task>`. The same export feeds
the runner's between-rounds check and its speed samples (`transcript.export`).

Drawn in Claude Code's own grammar, results under `  ⎿  `:
- prompts: a label (`Claude → agent · spec`, `· review feedback`, `· next step`, `· INTERRUPT,
  high priority`, `· note`; `workbench → agent · tests failed, sent back automatically`, `· fresh
  session: the task, the work so far, what's next`), matched by the prompt's first words, and the
  body under `  │ ` (40 lines, 10,000 with `-v`), with the protocol folded to `(+ the workbench
  protocol; -v shows it)`;
- `✻ Thought for Xs` and six lines of reasoning; `⏺` and each finished text in full;
- tools: `⏺ Bash(cmd)` with the output's tail (8 lines), `⏺ Update(path)` with `N line(s) added,
  M removed` and the diff (14 lines), `⏺ Write(path)` with its line count and content,
  `⏺ Read(path, from line N)`, `⏺ Search("pat", in p)` / `⏺ Find(…)` with the count and the first
  hit, `⏺ Skill(id)`, anything else as `⏺ name(input)`; errors in red; paths relative to the
  worktree; `ask_reviewer` calls are left out (its `ask`/`answer` events show instead);
- `⟲ OpenCode compacted the agent's context…`; `■ the model call failed: …`;
- after each model call, a step line: `⏱ N tokens in (M cached), read in Xs · K out in Ys · R tok/s`;
- the task's events: round headers, `■ tests PASS/FAIL (cmd)` with the tail, auto-fixes,
  questions and answers, `Claude (captain):` notes, steers, skills handed, allow, interruptions,
  fresh sessions and handovers, next steps, takeovers, stops, errors;
- a phase line on every phase change.

`-v` raises the caps (400 lines of output, diffs and reasoning) but still caps. Under the
transcript (on a terminal, one row rewritten in place): what's happening now (`activity`) and a
clock line:
- *Now:* queued: `Waiting for the GPU…`, or `status.last` itself while it loads, starts, stops,
  unloads or shares a model (`Loading …`); testing:
  ``Running the acceptance tests… `cmd` ``; asking: `Waiting for Claude's answer…`; running: from
  the model server's `GET <root>/metrics` when it returns a `live` object (`{state, phase,
  prompt_tokens, generated, tok_s}`): `Reading the prompt… t · N tokens`, `Thinking|Writing… t · ↓
  N tokens · R tok/s`, `Model server <state>… t`; without one (Ollama), a rotating verb (`Thinking`,
  `Pondering`, `Tinkering`…) and the time since the newest message. `models._server_idle` reads
  the same `live.state`.
- *Clock:* `⏲ task T · round N t · model load t`, then while it runs (queued, running, testing, asking) the ETA (`~X left (typically
  Nm)`, `X past its typical Nm`, or no ETA yet; typical is `router.expected_minutes`, the mean
  wall time of this model's judged results of this size, against the time since the task was
  created), `context U of WK (P%)` and tokens written.

A header first: the model line (label, reasoning effort, variant, context), repo, branch, test
command, and why it was routed there. Options: `--once` prints one pass and exits (as for a
closed task); `--no-color`; colour only on a terminal; with no task named, the newest-updated open
task, else the newest.

**`wb watch <task> --json`** (hidden) is the mod's feed: one pass, one JSON object per line,
`{"s": style, "t": text}` (UTF-8 unescaped), in this order: `meta` (the header's model line), the transcript
lines (styles `claude`, `agent`, `think`, `tool`, `out`, `ok`, `bad`, `sys`, `add`, `del`, `head`,
or `""` for prompt bodies), `activity` if any, `clock` (always), then the phase line. The mod
re-runs it on every poll while the followed task is active (else once per `name@updatedAt`),
keeps the last 2000 lines, shows the newest 60,000 characters of history, puts `meta` under the
rule and `clock` under the speech, and uses `activity` as the apprentice's speech (the pose is
`work` when it starts `Writing` or `Running`, else `think`). A new style needs an
entry in the mod's `LINE_STYLE`; changing the shape needs a new `live-1` tag.

## Models: found, loaded, chosen

Code: `workbench/models.py` (discovery, status, residency, the GPU lease, adding and retiring),
`workbench/router.py` (choice, slice, budgets, speeds), `workbench/hardware.py` (the machine),
`cli.cmd_models`, `cmd_model`, `cmd_scout`.

**OpenCode's config.** `models.opencode_config_path()`: `$WB_OPENCODE_CONFIG`, else
`$XDG_CONFIG_HOME/opencode/` (default `~/.config/opencode/`) `opencode.json` if it exists, else
`opencode.jsonc` if it exists, else `opencode.json`. `read_config` always parses through
`_strip_jsonc` (comments and trailing commas removed); `write_config` refuses unless the file is
strict JSON (no comments, no trailing commas) and then says what to change by hand; a write
reformats the file (atomic, indent 2).

**Discovery** (`discover`). A model is a candidate when it is an entry under a provider
(`provider` or `providers`; `options`/`settings` with a `baseURL`) whose host is this machine:
`localhost`, `0.0.0.0`, `host.docker.internal`, any loopback IP, the host name, its first label,
or `<label>.local`, or any of the machine's own addresses (`_own_addresses`: the host name's
`getaddrinfo` and `ip -o addr`, so a LAN or Tailscale IP counts) (`_this_host`). A
provider elsewhere counts when models.json gives it `"local": true`; it is then `remote` and never
shares the GPU. The id is `provider/name`; `root` is the baseURL without `/v1`. The engine
(`_engine`): models.json `engine`, else a cached probe of `GET <root>/api/version` (a JSON
`version` → `ollama`, an HTTP error → `server`), else a guess from the port (11434 → ollama).
`resolve()` takes an exact id, an exact model or provider name, or a unique case-insensitive
substring.

**models.json** (`$WB_MODELS`, default `~/.config/workbench/models.json`, strict JSON; a
malformed file breaks every command) holds only what OpenCode can't say, keyed by provider id or
model id; a model's entry is merged over its provider's (`{**provider, **model}`):
- `start` / `stop`: shell commands for a server (start detached, then status every 5 s up to 20
  min; stop with a 300 s limit);
- `skip`: leave the model (or every model of the provider) out;
- `sizes`: the task sizes it may take (`wb model sizes`; empty means both);
- `vram_gb`: the GPU memory it takes (decimal GB), for sharing and for `fits`;
- `weights_gb`: its size, which always overrides Ollama's figure (servers can't report one);
- `label`: the display name (also in `wb accept`'s commit message);
- `engine` (`ollama`/`server`) and `local: true`: per provider only.

**Status** (`status` → {available, resident, why}): Ollama: not answering / `<name>` is not
pulled / pulled / loaded (`/api/tags`, `/api/ps`). A server (`GET <base>/models`): running (a
server listing several models must list this one: "the server lists N models, not …"); stopped
with a `start` ("wb starts it"); not running with no start command. `wb models` and `wb doctor`
print the reason. `weights()`: `weights_gb`, else Ollama's size, else 0.

**Residency** (`make_resident(m, keep)`, called by the runner under the lease). Nothing to do when
`m` is resident and nothing else is. Else `free_gpu(keep={m, *keep})`: every other running
server with a `stop` command is stopped once per root, after waiting up to 10 minutes while its
`/metrics` says a reply is in progress (a running server with no `stop` is left, with a warning
that it may hold GPU memory); every model loaded on each discovered Ollama root is unloaded
(`POST /api/generate {keep_alive: 0}`) except kept ones. Then Ollama loads the model (`POST
/api/generate {prompt: "", keep_alive: "30m"}`, 20 min limit) or the server is started. The
seconds it took go to `status.load_secs`. A server that lists several models swaps them itself:
`free_gpu` treats a root as one server.

**Sharing the GPU** (`GpuLease`, `can_share`, `gpu_budget`, `need`). The budget is the sum of the
GPUs' total VRAM (`hardware.gpus()`; on unified memory macOS's `iogpu.wired_limit_mb`, else 75% of
RAM); unknown VRAM means no sharing. A model's need: `vram_gb`, else Ollama's loaded size
(weights and context), else weights × `KV_GUESS` 1.25. `can_share(m)`: every running model and `m`
are Ollama and local, and their needs sum to at most `SHARE_AT` 0.9 of the budget; the same model
twice needs Ollama's `OLLAMA_NUM_PARALLEL` ≥ 2 (read from the ollama service's environment,
`hardware.ollama_env`). Admission happens under the `gpu.admit` flock: holders are the
`gpu.json` entries whose pids are alive; a runner joins with `LOCK_SH|LOCK_NB` on `gpu.lock` when
no holder is waiting, every holder's model still resolves, and there are no holders or
`can_share` passes (so a task alone holds the lock shared). Otherwise it marks itself `waiting`
and blocks on `LOCK_EX`, first come first served; once it holds the lock exclusively nobody joins
it. `load()` runs `make_resident` under `gpu.admit`, keeping the other holders' models, so loads
are serialized. `gpu.json` is `{"<pid>": {model, task, waiting?}}`. `wb image` takes `gpu.lock`
through `GpuLease(None, "wb image")`: it queues in `gpu.json` like a task with no model, waits for
the running tasks to finish, then has the GPU to itself, and tasks started meanwhile wait for it. A
remote model (`local: true`) takes no lease at all, and `free_gpu` never unloads or stops a
remote provider's models.

**Routing** (`router.rank`, `choose`). Rows: `store.results()` (no trials) that `tuning.judged`
keeps, for this model and size. Quality is the acceptance rate with a uniform prior, overall =
(accepted + 1) / (tried + 2); on the repo's stack (`gitops.stack`: up to two languages, each at
least 20% of the tracked source files; a row's stack is the one recorded, else read from its repo)
quality = (like_accepted + `LIKE_PRIOR`·overall) / (like_tried + `LIKE_PRIOR`), `LIKE_PRIOR` = 2.
The available models within `QUALITY_MARGIN` 0.15 of the best are ordered by the guess
(`_guess`): those that fit the GPU before those that would spill (`fits`: Ollama says it's all in
VRAM, or `vram_gb`/weights within 90% of the budget; unknown is neither), then the largest by
weight bytes for `significant` work, the smallest for `trivial` (a model of unknown size ranks last), then
the one already resident. The rest follow by quality, unavailable models last. `allowed()`:
models.json `sizes`. `choose` returns the pick and a reason ("the largest … that fits the GPU",
"the best record on significant C work", … "(vs …)"). `wb models` prints the ranking per size;
`--set-default` makes the pick OpenCode's default. A new model has no record, so Claude gives it
its first tasks by hand (`--model`).

**The slice** (`router.slice(m)`, printed by `wb start`, `wb models`, `wb scout`). The guess
scales with the window (`limit.context`; unknown → 32K): files = max(1, round(3s)), new lines =
max(50, round(15s)·10), s = min(window/32768, `GUESS_MAX` 4): 3 files/150 lines on 32K (measured
2026-11-16), 12/600 from 128K. Correction from every closed step on that model and window
(results rows' `steps`, with files ≥ 1, from rows `tuning.judged` keeps): `_went` is badly when the step compacted (in its last session, or in one a handover replaced: the `fresh` event's `compacted`), filled up
reading (`read_full`), got stuck, or needed ≥ `BAD_REVIEWS` 2 agent-caused review rounds; well
when it landed and wasn't taken over; else no evidence. A bad step caps the slice below its size
(files − 1, lines × 3/4); the biggest good step raises it to its size and wins over the cap.
A handover is counted, not held against a step.

**Budgets and speed** (`router.budgets`, `record_speed`, `speed`). After every round the runner
reads the session export (`measure_speed`) and `record_speed` keeps, per model, the write speed
((output + reasoning) / (completed − first token), samples of ≥ 100 tokens over > 1 s) and read
speed (input / (first token − created), ≥ 1000 tokens over > 0.5 s), the last `SPEED_KEEP` 60 of
each, in `speed.json` (`{"<id>": {"write": [...], "read": [...]}}`, under `speed.lock`); `speed()`
takes the medians. At `wb start`, `budgets(m)` sets think = `THINK_TOKENS` 13,500 / write + window
/ read, clamped to [420, `THINK_MAX` 1800] s, and scales silence (900) and round (2400) by
think / 420. No measured speed: the defaults. They are written into task.json once.

**Managing the set.** `wb model add <provider/model> [--pull] [--context N]` (Ollama): pulls
(`/api/pull`, progress every 5%), asks `/api/show` for capabilities and context, and writes the
whole opencode.json entry: `{name, tool_call, reasoning, limit: {context, input: ctx − ctx/8,
output: ctx/8}}` plus `options.max_tokens` = 2 × output (`ollama_cap`: Ollama ignores the
`max_completion_tokens` OpenCode sends and honours `max_tokens`; uncapped, a degenerate
generation runs until the context is full, seen as 20K-token turns with no edit). The context is
`--context`, else what Ollama serves (`ollama_served_context`: the loaded `context_length`, the
Modelfile's `num_ctx`, `OLLAMA_CONTEXT_LENGTH`), else the provider's other models' setting, else
the trained length. It merges into an existing entry, so hand-added keys and options stay; a config with
`providers` instead of `provider` is read too. A model Ollama can't describe gets "is it pulled?
(--pull downloads it)". `wb model retire <id>
[--delete]` removes the entry (refusing OpenCode's default model) and with `--delete` the weights;
its results stay. `wb model sizes <id> trivial|significant|…` sets `sizes`. `wb doctor` checks each
Ollama entry for `options.max_tokens` and compares `limit.context` with what Ollama serves (more is
BAD: prompts would be cut off silently; less is a note: tasks could be bigger).

**The machine** (`hardware.py`, read fresh on each call; it is also on the hot path:
`gpu_budget` runs in routing and in the lease). `system()`: `macos`, `wsl` (`/proc/version`
contains "microsoft"), `linux`. `gpus()`: `nvidia-smi` (name, MiB total/used, driver) on Linux and
WSL (`[N/A]` reads as unknown; a line that isn't four fields is skipped); WSL adds Windows' adapters from `powershell.exe Get-CimInstance Win32_VideoController` (no
VRAM: AdapterRAM caps at 4 GB); Linux scans `/sys/class/drm/card*/device` (amdgpu's
`mem_info_vram_total`/`_used`; names from `lspci`; NVIDIA left to nvidia-smi when it answered,
else listed from sysfs, VRAM unknown); macOS reads `system_profiler SPDisplaysDataType -json`
(Apple Silicon: unified memory, `wired_limit`; an adapter with only shared memory, an Intel iGPU,
has VRAM unknown).
`memory()`: `/proc/meminfo` or `vm_stat`/`vm.swapusage`. `cpu()`, `ollama_env()` (systemctl or
launchctl, then the process env), `ollama_models_dir()`, `report()` (the lines `wb scout`
prints). Unknowns stay unknown; nothing about the machine is hard-coded.

**The weekly model check.** Models change fast. Once a week, in a quiet moment, the mod submits
SCOUT_NOTE (see *The mod, inside*); `/delegate-models` asks now, and `/delegate` runs one first when
no local model can run. `wb scout` prints the facts, read fresh: the machine (`hardware.report`),
Ollama's default context, every registered model (size, quantization, context, slice, real-task
record), installed models that aren't registered, the watch list (`watch.json`: models set aside
and what would bring each back; `--watch TEXT`, `--unwatch N`), flagged skills, waiting lessons,
image engines. Claude researches what's newest from primary sources, recommends keep / add /
retire in a few lines and changes nothing without the user's yes; `wb scout --done --notes "…"`
records it (`scout.json` `done_at`, notes). `--due` says whether one is due.

## The skill library

The agents learn nothing between tasks; Claude does. When review teaches something that will come
up again (a library's quirk, a pitfall, a check to run), Claude writes a skill. Code:
`workbench/skills.py` (library, checks, record, suggestions), `workbench/scan.py` (security scan),
`workbench/lessons.py` (what tasks taught), `cli.cmd_skill*` and `cli.cmd_lessons`.

**Where and how it's stored.** The library is its own git repository: `$WB_SKILLS`, default
`~/.local/share/workbench/skills`, created on first use with `git init -q -b master`
(`skills._git`). A skill is `<lib>/<name>/SKILL.md` plus any files beside it; a retired one is
`<lib>/retired/<name>/`. Only `<lib>/*/SKILL.md` is live (`available()`: name → {path,
description}; `resolve()` raises KeyError on an unknown name). Commits go under the user's git
identity, or `workbench <workbench@localhost>` when none is set (`_commit`). Messages:
`<name>: <message>` (plus `\n\nSource: <source>` from `--source`), `<name>: retired: <message>`,
`<name>: restored`. `rev(name)` is the last commit touching the skill (`%h`), `revs()` maps
names to revs, `history()` the last five `%ad %s` lines over `<name>` and `retired/<name>`
(`wb skill show`). The library never lives in `~/.claude/skills` (the user's), and knowledge of
one project that only some of its tasks need belongs in that project's `.claude/skills` instead.

**Front matter** (`parse`): the file must open with `---\n…\n---`; only top-level `key: value`
lines count, indented lines fold into the previous key, block indicators (`|`, `>`) start empty,
quotes are stripped. Fields used: `name`, `description`, `license`.

**Checked on the way in** (`check` → problems that refuse, warnings Claude reads):
- Refused: no file; a name not matching `^[a-z0-9]+(-[a-z0-9]+)*$`; a front-matter name that
  differs; no description, or one over 300 characters (it's all an agent sees before loading);
  the whole file over 8000 UTF-8 bytes; a body path `references/…`, `scripts/…`, `templates/…` or
  `assets/…` that doesn't exist beside the source file.
- Warned: a body over 120 lines; `/home/` or `~/` anywhere (agents go looking for those paths); a
  description without when/for/before/after/if; for a SKILL.md in a directory, clutter
  (README.md, CHANGELOG.md, .env, .gitignore, LICENSE.txt) or more than 10 other files; a body that
  reads like an incident log (3+ of `#NN`, `PR NN`, `issue NN`, `20YY-MM-DD`): a skill states the
  rule.

**Scanned on the way in** (`scan.py`, adapted from Hermes Agent's `tools/skills_guard.py` at
081e8d9, MIT notice kept in its docstring; it keeps the critical and high rules, drops trust tiers
and the medium rules, and adds Unicode Cf and tag characters). 43 regex rules, 27 critical and 16
high, case-insensitive and multiline:
- exfiltration (critical: curl/wget sending a `$…KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL` variable
  to a non-loopback URL, `cat` reading .env/credentials/.netrc/.pgpass/.npmrc/.pypirc (not a heredoc writing one, not
  `.env.example`), staging in /tmp
  then piping out; high: `~/.ssh`/`.aws`/`.gnupg`/`.kube`/`.docker`, `printenv`/`env |`, a
  markdown link carrying a variable, "send/post/upload … to http", webhook.site, requestbin,
  pipedream, hookbin, pastebin);
- prompt injection (critical: ignore previous instructions, disregard your rules, system prompt
  override, no restrictions, without filters, DAN; high: "you are now", "pretend to be", don't
  tell the user/reviewer, when no one is watching, output the system prompt, instructions in an
  HTML comment, a hidden `display:none` div);
- destructive (critical: recursive rm of the home directory itself (`~`, `~/`, `~/*`, `$HOME`,
  `${HOME}`, not a directory under it) or of `/`, `> /etc/`, mkfs, `dd of=/dev/`,
  sudoers/visudo);
- remote code (critical: curl|wget piped to a shell, `/bin/sh -i … >/dev/tcp`, a reverse shell
  (`reverse_shell`: nc/ncat/netcat with `-e` or `-c`, socat to `exec:`/`system:`/`pty`),
  `python -c "import socket`; high: a listener (`listener`: `nc -l`, socat `tcp-listen:`), ngrok,
  localtunnel, serveo);
- obfuscation (critical: base64 decode or echo piped to an interpreter that runs
  what it reads (`| sh`, `| python3 -`; not `| python3 -m json.tool`); high: strings built from
  `chr()`);
- credentials (critical: PEM private keys, `ghp_`, `github_pat_`, `glpat-`, `sk-ant-`, `sk-`,
  `AKIA`);
- Claude's files (critical: a shell write to AGENTS.md, CLAUDE.md or `.claude/settings*` through
  `>`, `>>`, `sed -i` or `tee`; high: a line telling the agent to edit one).

File rules: a symlink resolving outside the skill and binary extensions are critical; archives,
NUL bytes and invalid UTF-8 are high; media files aren't scanned. Per line: invisible characters
(zero-width, bidi controls, tag characters, any other Cf except the soft hyphen) are critical when
a bidi or tag character is present, else high (a BOM at the very start is fine); 200+ blanks
followed by code is critical outside prose files (.md .txt .html .xml .tex .css; the pattern
uses a possessive quantifier, so Python 3.11+). `verdict` is dangerous (any critical) / caution /
clean. On an update only `new_findings` count: the same (file, rule, stripped line) is the same
finding whatever its line number. `content_hash` is a sha256 over each regular file's relative
path and bytes (display only). The rules are drawn to pass ordinary advice (`cat .env.example`, a heredoc
writing `.env`, `rm -rf ~/build`; `nc -l 8080` is only high); a skill still refused is reworded,
not forced.

**The flows.**
- `wb skill add <name> --from <file|dir> -m "<what review taught>" [--source …] [--lesson ID…]`
  (`skills.add`): check, then scan (critical findings become problems, the rest warnings), then
  copy: a directory source replaces the skill's directory (rmtree + copytree, `.git` ignored); a
  file source replaces only SKILL.md. `git add -A -- <name>`, commit only if something changed
  (an identical re-add prints "updated" and commits nothing). Refuses the names in
  `context.denied_skills()` (`delegate` and the user's personal skills), because skill denies
  are by name, and `retired` (the library's own directory, refused by `check`); refuses a source
  inside the library (it would be scanned against itself: use `patch`, or add a copy from
  elsewhere). `--lesson` closes those lessons as `skill:<name>`; when every given lesson came from
  one repo, `cli._one_project` warns the skill may belong in that repo's `.claude/skills`.
- `wb skill patch <name> --old … --new … -m why` (or `--old-file`/`--new-file`): the old text must
  occur exactly once in SKILL.md; the skill is copied to a temp draft, patched and re-added, so it
  is checked, scanned and committed like an add. Patch the passage that misled, in place, rather
  than appending a correction.
- `wb skill retire <name> -m why`: `git mv <name> retired/<name>` (an older retired copy is
  removed first, from the disk and from the index with `git rm --cached`, touching nothing else).
  `wb skill restore <name>`: the reverse; refused while a live skill has the name (retire it
  first).
- `wb skill scan <name|path>`: the scan alone; it exits 0 whatever the verdict.
- `wb skill show <name>`: the skill and its history (`wb skill list` has the record).
- `wb skill import <git-url|owner/repo|path> [--path <dir>] [--as <name>]` (`skills.fetch`): a
  local path is read in place; a URL (`://` or `git@`; `owner/repo` means GitHub) is shallow-cloned
  (`GIT_TERMINAL_PROMPT=0`, 300 s) into `$WB_HOME/imports/<slug>`, replacing an earlier clone.
  Several skills found and no `--path`: it lists up to 40 and stops. One: it prints the source
  and commit, the license (`license_of`: front matter, else the nearest `LICEN[CS]E*` file up
  the tree), the content hash and the scan, then the suggested `wb skill add … --source
  "<url>@<rev12> <subdir>, <license>"`. A dangerous one is refused, and a clone under `imports/`
  is deleted. Nothing enters the library by itself: Claude reads it, keeps what fits, rewrites it
  in the library's shape and adds it.
- `wb skill hand <task> <name…>` / `wb skill take <task> <name…>` change a task's skills from its
  next run (hand validates names; take doesn't). `wb start --skill` hands at start; `wb next
  --skill` replaces the set for the next step. Names may be repeated or comma-separated
  (`cli._skill_names`, which dies on an unknown name before anything is created).

**Handed to a task.** `status.skills` is the current set, `status.skills_ever` every skill the
task has held (what results call `skills_offered`; take never shrinks it), `status.skills_rev`
the library commit of each when handed, `status.skills_told` what the current session has been
told. `context.layer` puts each handed skill's directory (still in the library) into the config
layer's `skills` list, one directory each, so OpenCode lists exactly those, and allows the
directory for the file tools (`external_rules`). `runner.skills_note` prepends to the round's
prompt argument (not prompt.md): the whole set at a session's start ("Claude picked these
skills…"), afterwards only what was handed or taken back; it logs a `skills` event. A retired
skill drops out silently. `skills.loaded(task)` reads the `skill` tool calls from events.jsonl
(the event's brief is the skill id), so it also counts a repo's own `.claude/skills`.

**Judged on outcomes** (`skills.usage`, `record_line`, `advice`). Rows: `store.results()` (trials
left out) that `tuning.judged` keeps (a discard marked `--spec-gap` is dropped). Per skill:
offered (in `skills_offered`), loaded (in `skills_loaded`), and for loads: accepted, first-try
(accepted with no agent-caused review round), review rounds a task; the same repos' tasks that
didn't load it; "since its last edit" (rows whose `skills_rev[name]` is the current rev); repos
loaded in. `advice`, first match wins: never offered; offered 3+ times and never loaded (its
description is wrong); 2+ tasks since its last edit and none accepted; 3+ loads under 50%
accepted (patch it with what review taught); 3+ loads and 3+ without, no better than without; not
offered in 60 days. `wb skill list` shows each skill's record and advice; `wb scout` lists the
flagged ones.

**Suggested for a spec** (`skills.suggest`, used by `wb start` when no `--skill` is given). Words
are `[a-z][a-z0-9]{3,}` minus 61 stopwords (`STOP`, grown after a spec matched an unrelated skill on "rows", "keep", "step"), stemmed by stripping the first of ing/ers/er/es/ed/s/e
that leaves 4+ characters. A skill is matched on its name and its trigger (the description up
to its first colon, else all of it). It's suggested when the spec shares at least `SUGGEST_MIN`
= 3 distinct words with them (one shared word is chance); the score counts name words twice; the
top 3 are shown. One loaded so far only in other repos says where, and on what stack when that
differs (`cli._elsewhere`; the other repo's stack is read from disk now), so one project's
knowledge doesn't spread unnoticed.

### Lessons

`workbench/lessons.py` gathers what each task taught into `$WB_HOME/lessons.jsonl`, for Claude
to turn into skills. `wb feedback`, `wb next`, `wb accept` and `wb discard` call `harvest` and end
with one `nudge` line: `lessons: N from this task[, M seen in other tasks too] (wb lessons: write
or patch a skill now, while it's fresh)`. A task whose task.json has `trial` gathers nothing, and `wb trial` closes the open lessons of
the tasks it marks.

Sources, by `kind`:
- `feedback`: each round's prompt (`instructions.md` when a handover replaced `prompt.md`) starting with `FEEDBACK_HEAD` ("Review feedback from
  the senior reviewer."); one lesson per numbered item (`N.` or `N)`), else the whole body; capped
  at 600 characters, anything of 8 or fewer dropped.
- `agent`: the agent's `Lesson: …` lines in `summary.md` after "agent's summary:" (PROTOCOL asks
  for them; capped at 400). Skipped for a discarded task, and on a discard the task's open `agent`
  lessons are closed as dropped: a task that never found a working method has dead ends, not a
  recipe.
- `stopped`: every guard's `stopped` event, as "the guard stopped the agent: …"; a stop by the
  reviewer or the user (an event with `by`) is not a lesson.
- `question`: each answered `ask/*.json`, question and answer (permission requests answered by
  `wb allow` included).
- `spec`: each `status.spec_gaps` entry (`--spec-gap`), as "the spec missed: …": a lesson for
  Claude's spec checklist, not an agent skill.
- `skill`: at accept/discard only, when agent-caused review rounds > 0: one per loaded skill,
  "loaded X, yet needed N review round(s): does X say the right thing?".

A row is `{ts, task, repo, skills, id, kind, round, text}`; `id` is sha1(task, kind, round,
text)[:6], so re-gathering adds nothing. A close row is `{close: id, ts, how: "skill:<name>" |
"dropped", why}`. `survey` finds recurrence: a lesson recurs when one in another task (open or
closed, kind `skill` excepted) shares 3+ words with Jaccard ≥ 0.35; a closed match means it came
back after it was handled. Each lesson is attributed to the first loaded skill still in the
library, else the library skill whose name and description share the most words (3+). `wb lessons`
lists the open ones, recurring first, at most 12 (`--all`, `--task`); `wb lessons close <id> -m
why` closes one (a one-task detail, an environment problem, a dead end); `wb skill add|patch
--lesson <id>` closes it as written into a skill. The weekly check counts what's waiting.

## Images

Claude can make images for a project (icons, illustrations, backgrounds, a README picture) with an
image model on the user's machine. Claude acts as art director: it writes the prompt, `wb image`
generates four candidates, and Claude looks at each one. Then it keeps one, rewrites the prompt
and tries again, or reuses a seed it liked. The image it keeps goes into the project and is
committed like any other change. Each image has a `.json` file beside it with the prompt, seed,
size and engine, so it can be made again. The coding agents can't make images; when one needs an
asset, it asks Claude.

**Setting it up is optional.** The first time a project needs an image, Claude offers to set it up.
It looks at the OS and GPU, then proposes a tool and a model whose licence allows the user's use,
and gives the download size. It installs nothing until the user says yes. To do it by hand instead,
here is what the development machine runs (Arch Linux, a 16 GB AMD card), about 11 GB of downloads:

```sh
sudo pacman -S --needed vulkan-headers spirv-headers shaderc cmake   # or the distro's packages
git clone --recursive https://github.com/leejet/stable-diffusion.cpp ~/.local/opt/stable-diffusion.cpp
cd ~/.local/opt/stable-diffusion.cpp && git submodule update --init --recursive
cmake -S . -B build -DSD_VULKAN=ON -DCMAKE_BUILD_TYPE=Release && cmake --build build -j
ln -s "$PWD/build/bin/sd-cli" ~/.local/bin/sd-cli
mkdir -p ~/models/image && cd ~/models/image
curl -LO https://huggingface.co/leejet/Z-Image-Turbo-GGUF/resolve/main/z_image_turbo-Q8_0.gguf
curl -LO https://huggingface.co/unsloth/Qwen3-4B-Instruct-2507-GGUF/resolve/main/Qwen3-4B-Instruct-2507-Q8_0.gguf
curl -LO https://huggingface.co/Comfy-Org/z_image_turbo/resolve/main/split_files/vae/ae.safetensors
```

Z-Image-Turbo is Apache-2.0, takes 8 steps and needs about 11 GB of VRAM at Q8 (smaller
quantizations fit smaller cards). On NVIDIA, build with `-DSD_CUDA=ON`; on a Mac, `-DSD_METAL=ON`.
Then describe the engine in `~/.config/workbench/images.json` (`$WB_IMAGES`). Two kinds work:

```json
{
  "z-image-turbo": {
    "command": "sd-cli --diffusion-model ~/models/image/z_image_turbo-Q8_0.gguf --vae ~/models/image/ae.safetensors --llm ~/models/image/Qwen3-4B-Instruct-2507-Q8_0.gguf -p {prompt} -o {out} -W {width} -H {height} --seed {seed} --cfg-scale 1.0 --steps 8",
    "label": "Z-Image-Turbo Q8 on stable-diffusion.cpp (Vulkan)",
    "notes": "licence: Apache-2.0; text to image only; 8 steps; ~11 GB VRAM"
  },
  "server": { "url": "http://127.0.0.1:30000/v1/images/generations", "model": "…" }
}
```

- A **command** is any command-line tool. `{prompt}`, `{out}`, `{width}`, `{height}` and
  `{seed}` are filled in, and the prompt is passed as a single argument, never through a shell.
  `ref_arg` (e.g. `"-r"`) adds `--ref` images, for models that edit or keep a character
  consistent.
- A **url** is any server with the OpenAI images API (SGLang, vLLM-Omni, LocalAI and others).
- `notes` is shown to Claude. Put the model's licence there: some licences limit where and how
  their output may be used.

A generation takes the GPU lock and unloads the coding models first, just as a task does, so the
two never share VRAM. `wb scout` and `wb image --list` show each engine and whether it's ready.
Prefer weights in safetensors or GGUF from the model's own publisher or a well-known
quantizer. Prefer tools that load models without running code that comes with them (ComfyUI's
custom-node packs do run their own Python).

**How it works** (`workbench/images.py`, `cli.cmd_image`).
- `images.json` (`$WB_IMAGES`, default `~/.config/workbench/images.json`) maps an engine name to an
  entry; the first entry is the default, non-object values are ignored. Keys: `command` or `url`,
  `ref_arg`, `model`, `label`, `notes` (shown to Claude: put the licence there), `timeout` (s,
  default 1800).
- A command engine (`_argv`): `shlex.split(command)`, `~` expanded per token, then `{prompt}`,
  `{out}`, `{width}`, `{height}`, `{seed}` filled in that order, token by token, never through a
  shell; `--ref` images are appended as `[ref_arg, path]` pairs (both `command` and `ref_arg` are
  required for refs). stdin is /dev/null; stdout and stderr go to `<out_dir>/engine.log`; a
  nonzero exit or no file at `{out}` fails with the log's last lines.
- A url engine (`_server`): POST `{prompt, n: 1, size: "WxH", response_format: "b64_json", seed[,
  model]}` (no auth header), decode `data[0].b64_json`.
- Ready (`ready`, shown by `wb image --list` and `wb scout`): a command whose first token is on
  PATH or a file; a url whose server answers a GET (any status) within 2 s.
- `generate`: holds the GPU for the whole batch through `models.GpuLease(None, "wb image")` (it
  queues in gpu.json behind the running tasks, and tasks started meanwhile wait for it), calls
  `models.free_gpu()` with nothing kept, then `_settle` (up to 90 s for the resident models,
  Ollama's and running servers, to go and used VRAM to stop falling, after
  an engine started a second after the unload found 1.1 GB of 16 free; skipped under
  `WB_FAKE_MODELS`). Seeds are seed … seed+n−1 (default seed `time() % 1_000_000`). Files are
  `<slug>-<seed>.png` (the slug is the prompt's first six words), each with a `.json` sidecar
  `{prompt, seed, width, height, engine, refs, secs, ts}`. `--out` defaults to
  `$WB_HOME/images/<YYYYmmdd-HHMMSS>/`.

## Git, commits and projects

- **The split of roles.** The agent writes code and reads git freely (`status`, `diff`, `log`,
  `show`, `blame`). Everything that leaves its working tree is Claude's: commits, branches,
  merges, pushes, pull requests, CI/CD, deploys and infrastructure (`gh`, `wrangler` and the like),
  and the project's `CLAUDE.md` and `AGENTS.md`. The handbook and the working protocol tell the
  agent so, and to ask through `ask_reviewer` or say so in its summary when the task needs one of
  those.
- **Git is read-only for the agent, enforced** (`workbench/gitguard.py`). `install` writes
  `rules/<task>/bin/git` every round (`#!<python> -I`, the package path, then `gitguard.main(<real
  git>, <the task repo's common dir>, argv)`) and puts its directory first on the agent's `PATH`,
  so it's the git called from the shell, a script or `make`. `split` skips git's global options
  (and the values of `-C`, `-c`, `--git-dir`, `--work-tree`, `--namespace`, `--super-prefix`,
  `--attr-source`) to find the subcommand; `reads` decides: no subcommand or one of `READ` (35:
  `status`, `diff`, `log`, `show`, `blame`, `grep`, `rev-parse`, `ls-files`, …) reads; `branch` and
  `tag` read only when listing; `config` only with a read option or one name; `stash` only `list`
  and `show`; `worktree` only `list`; `remote` without arguments, `get-url`, `show` (with or without `-v`); `reflog`,
  `notes`, `submodule` and `symbolic-ref` in their read forms; anything else (aliases and `lfs`
  included) is a write. A read is exec'd with `GIT_OPTIONAL_LOCKS=0`, so the agent's `git status`
  never refreshes the index while Claude diffs. A write is refused, with `REFUSAL` (leave the
  change uncommitted for Claude; to undo an edit, edit it back or `git show HEAD:<path>`; ask if
  the task needs a git write), when it lands in the task's repository (`protected`: the same
  common dir) or in any repository outside the temp directories; outside any repository it passes,
  and throwaway repositories under the temp directories, which test suites create, stay writable
  (the temp roots are fixed when the shim is written, so an agent's `TMPDIR` can't make a project
  count as scratch). For `init` and `clone` it judges by where the new repository lands
  (`destination`, skipping options and their values). The real git
  called by its full path is refused by OpenCode's rules (`*/git *`), and the task repo's refs are
  outside the lane. That also means an agent can't wipe its own unfinished work by mistake. The
  shim is standard library only: it's imported on every git call.
- **Claude's files.** OpenCode's `edit` rules refuse edits to `CLAUDE.md`, `CLAUDE.local.md`,
  `AGENTS.md` and `.claude/`. If the agent changes one anyway (from the shell), the round report
  says so (`runner.claude_files_changed`).
- **The agent never commits.** Everything it does stays uncommitted in its worktree, and
  `wb diff` shows it (new files included: `gitops.with_new_files` runs git with intent-to-add in a
  scratch index, so the worktree's own index, and a concurrent git command's lock, are left alone) against the task's
  base, or after `wb next` against the last committed step (`--all` for the whole branch).
- **Commits are the user's** (`gitops.commit_all`: `add -A`, refused when nothing is staged).
  `wb accept` commits under the user's git identity with no AI co-author line, only provenance:
  ```
  feat: CSV export for reports

  Delegated to <model> via OpenCode: 3 round(s), 1 automatic test-fix, 1 review round(s).

  Workbench-Task: csv-export
  OpenCode-Session: ses_…
  ```
  Rounds count every runner round (auto-fixes, notes and handovers included); review rounds count
  the last step's. `wb next` commits each step as `<message>` with `Workbench-Task: <task> (step
  N)` and `OpenCode-Session:`. A last step with no new changes accepts the steps already committed.
  `wb accept` merges only with `--merge` (`merge --no-ff --no-edit` in the user's checkout, which
  must have the task's `base_ref` out, unless that is `HEAD`, and no changes to tracked files; a
  conflict is backed out with `merge --abort` and reported as not merged); otherwise it prints the
  merge command.
- **The worktree** (`gitops.create_worktree`): `worktree add -b delegate/<task> <task
  dir>/worktree <base sha>`, refused when the branch exists. `remove_worktree` (`--force` on
  discard) and `prune`; `delete_branch` (`branch -D`) unless `--keep-branch`.
- **The repo's stack** (`gitops.stack`): from `git ls-files`, the top two languages by file
  extension that each hold at least 20% of the source files. The router and the skill records use
  it.
- **`wb init <dir>`** sets a project up the way Claude keeps every project: a git repo on git's
  default branch (`init.defaultBranch`) with an initial commit, and `AGENTS.md` as a symlink to
  `CLAUDE.md` (an existing `AGENTS.md` becomes `CLAUDE.md`), so OpenCode's agents and Claude read
  the same file. It refuses a directory inside another repository or without a git identity, and
  never adds a remote.
- **CLAUDE.md stays true.** It goes into every agent prompt, so `wb accept` prints how many code
  commits it's behind (`cli.claude_md_status`), and the skill has Claude update it after each
  accepted task that changes what it describes.

## The web chat

`wb chat serve` (`workbench/chat.py`; kept up by `systemd/wb-chat.service`, a user unit running
`%h/.local/bin/wb chat serve` with `Restart=always`, giving up after five starts in five minutes:
`StartLimitBurst`) serves a small page on port 1337 for reaching
the Claude session in delegate mode while the user is away.

- **The path of a message.** The page posts it; the server appends it to `chat/messages.jsonl`.
  The listening session's mod submits it to Claude as `[web chat] <the user's words>`, nothing
  more, so the terminal shows what the user wrote; how to answer is in the `/delegate` skill. It
  goes into the running turn if Claude is mid-turn (`session.send`, with one line saying it's from
  the user), else as the next prompt. The mod also writes a receipt (`wb chat ack`) that the page
  shows at once (Claude has it now, or will read it after its current step). Claude answers with
  `wb chat reply`, which the page shows. The page says whether a session is listening and whether
  Claude is working.
- **One listener.** A session listens while delegate mode is on in it (from `/delegate`, or a
  delegating `wb` command, until a quit command). With several sessions the first live claim on
  `chat/listener.json` listens; it is known by its `wb events` process's pid.
- **Claude knows where the user is.** A web message carries the same authority as one typed in
  the terminal, with a note: reply there, briefly; the web thread is all the user sees; a yes there
  answers only a question asked there; nothing hard to undo on an ambiguous OK. For six hours after
  a web message (until the user types in the terminal again) Claude also tells the user there
  when something they asked for finishes or needs them.

**Files** (`$WB_HOME/chat/`):
- `messages.jsonl`: `{id: "<ms>-<6 hex>", ts, from: "user"|"claude", text, cid?}`. A post whose
  `cid` is among the last 500 messages returns that row instead of adding one.
- `receipts.jsonl`: `{id, ts, by, how: "now"|"queued"}`, written by `wb chat ack` (the mod).
  `queued`: a mid-turn delivery didn't go through, so it was submitted as a prompt.
- `listener.json`: `{session, cwd, pid, busy}`, written by the mod when it changes. A listener
  counts while its pid answers `kill -0` (or, with no pid, its `ts` is under 15 s old).

**Settings.** The token: `$WB_CHAT_TOKEN`, else `~/.config/workbench/chat-token` (created on first
use with `O_EXCL`, mode 600, `token_urlsafe(32)`; kept at 600; one under 16 characters is refused
with a message), read once when the server starts, so a new token needs a restart. The settings:
`$WB_CHAT_CONFIG`, else `~/.config/workbench/chat.json`, with two keys. `bind`: addresses to listen
on, which replace localhost (`localhost`, an address, `tailscale`: the tailnet IPv4 from
`tailscale status --json`, skipped when tailscale isn't running; `0.0.0.0` or `::`: every
interface). `hosts`: extra host names to answer to (`*`: any). `--bind` replaces chat.json's
binds, `--host` adds to its hosts, `--port` changes the port. Answered host names (`chat.plan`,
worked out once at startup): `localhost`, `127.0.0.1`, `::1` always; for `tailscale` its DNS name,
first label and host name; for a wildcard the host's name, its first label, its FQDN and whatever
the host name resolves to; any other bind as itself; plus `hosts`. `serve` prints what it listens
on, binds it skipped, and warnings for a wildcard bind or `hosts: *`. With a bind skipped (tailscale
not up yet), it rechecks every `REPLAN_EVERY` 30 s and exits 75 once more binds are available,
for the service manager (`Restart=always`) to start it again with them. `wb chat url` prints
`http://localhost:<port>/#token=…` (the fragment never leaves the browser).

**The HTTP API** (`Handler`, HTTP/1.0, no keep-alive, no CORS):
- Every request: the `Host` header (port and brackets stripped, lowercased) must be an answered
  name, else 403 `{"error": "unknown host"}`: this stops other web pages and DNS rebinding.
  Responses carry `Cache-Control: no-store`, `X-Frame-Options: DENY`, `nosniff`, `no-referrer` and
  a CSP (`default-src 'self'`, inline styles only, no framing, no forms).
- `GET /`, `/index.html`, `/<name>` with a known suffix (html, js, css, svg, webmanifest) from
  `chat_static/`; anything else 404.
- The API wants `Authorization: Bearer <token>` (the `Bearer ` prefix required, compared in
  constant time); else 401 "bad or
  missing token: open the link from `wb chat url`".
- `GET /api/state`: `{messages (the last 200, each user row with its receipt as `received`),
  listener, now}`.
- `GET /api/stream`: server-sent events (with the security headers), at most 16 open (503 past it): `event: state` with the
  snapshot at once and whenever it changes, `: ping` after 15 s idle. One `Hub` thread watches
  `chat/` (inotify, or `wait()` when it fell back to polling) and a pidfd on the listener and
  wakes every stream.
- `POST /api/send`: JSON only (415), at most 32 KB (413), `{text: 1–8000 characters, cid?: 1–64
  characters}` (400), → 200 `{message, listener, state}`.

**The page** (`chat_static/`: `index.html`, `app.js`, `app.css`, `manifest.webmanifest`,
`icon.svg`; no inline script, as the CSP requires). The token is taken from the address's
fragment into `localStorage` (`wb-chat-token`) and stripped from the address bar; a missing or
refused one shows a screen that accepts a pasted link or token. The stream is a `fetch` with the
bearer header (EventSource can't send one), parsed by hand; every state event is a full resync, so
nothing arrives half-received. A message goes into a local outbox (`wb-chat-outbox`, each with a
`crypto.randomUUID()` cid) and is sent in order until the server has it; the server ignores a cid
it already holds, so nothing is sent twice; a 4xx other than 401 marks it not sent, with retry,
edit and delete. A 40 s watchdog (pings count), jittered backoff (0.5 s to 30 s, a banner after
three failures) and a reconnect when the page or the network comes back keep it connected; a page
hidden for a minute drops its stream. Rendering is DOM only (inline code, bold, links, fenced code,
lists); receipts read "✓ delivered", "✓ waiting for a Claude session", "✓✓ read by Claude",
"✓✓ Claude has it, reading after its current step"; "Claude is on it" shows while the listener is
busy with the user's latest message. Claude's messages flow in at a reading pace (`flows`) once
the first render is done; the backlog a first visit loads, a message that arrives while the page
is hidden (it counts as unread in the title) and reduced motion show whole, and each shown message
is remembered (`wb-chat-seen`, the last 500) so a reload doesn't replay it. Light and dark follow `prefers-color-scheme` (tokens on
`:root`, `color-scheme` meta, a theme colour per scheme); reduced motion turns animations off. It
can be added to a home screen (a manifest and an SVG icon; no service worker). Away from
localhost over plain HTTP the page isn't a secure context, so the browser's UUID and clipboard
are missing (the page falls back) and it won't install as an app.

A change to `chat.py` or `chat_static/` also needs the browser test (*The tests*).

## Stopping everything

Only the user's two slash commands stop the delegation, never words in a message:

- **`/delegate-quit`** runs `wb stop --all`: every running agent stops, every task keeps its
  worktree for later, and Claude is told not to finish, restart or clean up anything unless asked.
- **`/delegate-quit-and-hand-back-claude`** runs `wb takeover --all` and starts a turn for Claude
  to leave delegate mode, finish each task itself to its own standard (`wb diff`, the acceptance
  command, `wb accept`), and carry on with the rest of the user's request without delegating.

Both end delegate mode: Claude works directly again and the pane closes (`/delegate-pane` brings it
back).

## The mod, inside

`mod/` is a Claude Code plugin: `mod/.claude-plugin/plugin.json` (name `workbench`, `types:
./types/index.d.ts`, one setting `userConfig.wake`, `on`|`off`, default `on`; changed with
`claude plugin configure workbench@workbench`, which reloads the module), `mod/hooks/hooks.json`
(`{"modules": ["./register.tsx"]}`), `mod/hooks/register.tsx` (everything), `mod/hooks/apprentice.ts`
(the character's art), `mod/hooks/register.test.tsx` (run by `claude plugin test mod`),
`mod/types/index.d.ts` (the atom types), `mod/.claude-plugin/icon.png` (the `work` pose).
The repo root's `.claude-plugin/marketplace.json` makes the repo a folder marketplace (`workbench`,
plugin source `./mod`), so the plugin id is `workbench@workbench` and it is read from the clone:
`git pull` plus a restart or `/reload-plugins` upgrades it. `mod/tsconfig.json` extends
`./.claude-plugin/types/tsconfig.json`, which Claude Code generates (gitignored, absent in a fresh
clone).

**The `$` rule.** `claude plugin validate` follows every use of the module's `$`; `$` may be
passed only to functions declared at the top level of `register.tsx` (a function declaration or a
const bound to one; imports from the module's own files also pass). The helpers type `$` as
`any`, so tsc doesn't check calls inside them; validate's listing (hooks, calls, env reads,
"gating hook without .catch" notes) is the check. Env reads must be string literals: `HOME`,
`WB_HOME`, `WB_BIN`, `CLAUDE_CONFIG_DIR`, `WB_COMPACT_AT`.

**State.** Three `$.state` atoms survive a module reload within a session; each has a shape tag,
and an atom read under an old tag is absent, so change the tag when its type changes:
`tasks` (`tasks-1`, WbTask[]: name, phase, round, model, label, tests, last, createdAt,
updatedAt, tokens, recent), `live` (`live-1`, {task, lines: {s, t}[]}: the transcript lines),
`chat` (`chat-2`, {key, delegating, webAt, url}: delegate mode lives here, plus the session's
listener key). Everything else is `mem`, a plain object reset by a reload (busy, queued turns,
the open set, follow, sawTurn, scoutAsked, toasts sent, the events pid, …). The task facts
themselves are re-read from `$WB_HOME`; the mod writes `tasks/<n>/acked`, scout.json's
`offered_at` and `chat/listener.json`.

**session.start.** Sets `mem.root` (`WB_HOME` or `~/.local/state/workbench`), `mem.wb` (`WB_BIN`
or `wb`), `mem.skill` (`$CLAUDE_CONFIG_DIR` or `~/.claude`, then `skills/delegate/SKILL.md`),
the chat key (`<start ms>-<6 base36>`, kept across reloads), registers the four commands,
starts the ears and two timers.

**Ears and timers.** `ears` spawns `wb events` (cli.cmd_events: `{"hello": pid}` first, then
`{"changed": [...]}` per batch; it skips tasks' `worktree/` and `rounds/`, `imports/`, `images/`,
`runner.log`, `*.lock`, `*.tmp` and dotfiles, and exits when its parent changes); every line runs `poll()`; when the child ends
it is respawned after 2 s. A 1.5 s timer polls when there's no events child, a task is
queued/running/testing/asking, a turn is queued, or 30 s passed since the last poll (`SAFETY_MS`). A
450 ms timer animates the apprentice, blitting only changed frames. Every 10 minutes `wb chat
url` refreshes the pinned link (only a `…#token=…` URL is pinned; the call creates the token file
if missing). Subprocess limits: `wb` 120 s, `wb watch` 30 s, `kill -0` 5 s.

**poll()** (re-entrant calls set `again` and run once more after): `scan` reads every
`tasks/*/task.json` + `status.json` (closed tasks drop out 30 min after their last update; open
ones get their last 4 event lines as `recent`), sorted by creation; sets the status line; opens
the pane once per load when a task is queued/running/testing or delegate mode is on (an open the
user didn't ask for is placed only on a wide terminal; a pane the user closed stays closed for that
load); refreshes the live transcript (`wb watch <task> --json`, every poll while the focused task
runs, else once per `name@updatedAt`; the last 2000 lines kept; the focus is the newest running
task, else the newest one waiting for Claude); submits one queued turn when idle; detects a task
boundary and compacts; offers the weekly check; then `deliver`, `rewake`, `listen`.

**Busy.** `mem.busy` is set by `turn.start` (main loop) and by a main-loop `tool.call` (a
module reloaded mid-turn never saw `turn.start`; a subagent's call doesn't count), and cleared
only by the main loop's `turn.complete`. Held wakes are rechecked on every poll.

**Waking** (`deliver`). Only when notify.jsonl's length changed or Claude is busy. Per task, the
newest notify row under an hour old and past the task's `acked`; it is still true when it's a
check-in and the task is queued/running/testing, or the phase equals the kind (`question` ↔
`asking`). A stale one is acked and dropped. A true one is toasted once (`workbench: <name>
<headline>`); if Claude is busy it waits; else the mod writes `acked = row.ts` and submits
`[workbench] Task \`<n>\` <headline>.\n\n<row.text>\n\n<NEXT[kind]>` (HEADLINE and NEXT are the
per-kind texts at the top of register.tsx; NEXT names the commands to run, and the question text
says a PERMISSION request is answered by the `wb allow` it names). With the `wake` setting off
it is only toasted: `acked` isn't written, so `wb wait` still gets it. `deliver` remembers each
task's newest notice ts (`mem.noticed`). `rewake`: when idle, with wake on, a task waiting on
Claude (review/asking/stuck/failed) whose status changed between 60 s (`REWAKE_MS`) and 12 h
(`REWAKE_MAX_MS`) ago and whose news Claude hasn't had gets `acked = now` and a prompt saying no
notice reached Claude, with `wb report <n>` and NEXT. Had the news: `acked` is at or past the
newest notice the mod read (the runner's last status write, clearing its pid, lands just after
its notice, so `updated_at` alone woke Claude twice), else `acked` is past `updated_at`. `cli._ack_all` stops the mark short of news still
true. Wake prompts are submitted as the plugin, not `asUser`. The mod runs in every session with
the plugin installed and the state directory is global: the first session to deliver a notice
wins (the `acked` write is not locked).

**Delegate mode** (`delegateMode(on)`, `chat.delegating`). On: the `delegate` skill's prompt
(`skill.prompt`, which also records its path and opens the pane), or a Bash tool call matching
`/(^|[\s;&|(])wb (start|next|feedback|steer|answer)\b/` (checked before it runs; a resumed
session or a /delegate the mod missed; a `wb` named by absolute path isn't seen; not `accept` or
`takeover`, which a done note after a quit asks for). Off:
either quit command, which also closes the pane. While on: the `delegate-mode` and `user-notes`
context blocks, the idle status line, pane auto-open, web chat listening, the apprentice's
"Delegate mode is on.". No `$.ui.invalidate`: rewriting the first message would cost the
conversation's prompt cache.

**Commands.** Every one is `/delegate` or starts with `delegate-`, so typing `/delegate` lists
them all (2026-11-16: `/workbench` became `/delegate-pane`, no alias). `/delegate-pane` opens the pane and says how many tasks are open; a pane the engine
still holds but doesn't show (a tab behind another, or undrawn) is closed first, so it comes back,
and a pane that can't be drawn here says why (`opened.reason`). `/delegate-quit`:
delegate mode off, `wb stop --all`, a toast, and hidden context (QUIT_NOTE: don't finish,
restart or clean up anything unless asked); it starts no turn. `/delegate-quit-and-hand-back-claude`:
mode off, `wb takeover --all`, and DONE_NOTE queued as a turn (leave delegate mode; per task `wb
diff`, finish in its worktree, run the acceptance command, `wb accept` (`--merge` only if asked);
carry on without delegating). `/delegate-models` queues SCOUT_NOTE now. `/delegate-update` runs
`wb update` and, when it updated, `/reload-plugins` (`$.command.run`; if that throws, it says to
run it). A command can't start a
turn itself: it pushes onto `mem.queued`, which the next idle poll submits. `/delegate` is the
skill, not a mod command.

**Context blocks** (`prompt.context`, appended in this order; Claude Code computes them at the
conversation's first message and after a compaction or `/clear`, not every turn):
`delegate-mode` while delegating (MODE_NOTE: the mode lasts until a quit, follow the skill,
every plan is a handoff plan, the SKILL.md path); `user-notes` while delegating and notes.md has
notes; `web-chat` for six hours after a web message, until a prompt is typed in the terminal
(reply with `wb chat reply`, `wb chat log` shows the thread); `workbench` whenever `wb status`
lists open tasks (the status, plus `wb report`/`wb watch`).

**Compaction.** `session.compact` (main conversation only, while a task is open) appends
KEEP_ON_COMPACT: keep the user's goals and decisions, the tasks and what each needs next, what
was accepted, merged or discarded; drop finished tasks' diffs, reports and transcripts. At a task
boundary (a task in the last poll's open set is gone, Claude idle, none waiting for Claude) it
compacts once usage reaches `WB_COMPACT_AT`% (default `COMPACT_AT` 0.5; 0 off) of the
auto-compact threshold (or the window when auto-compact is off).

**Status line and pane.** Status: `⚒ wb · <name> <phase> r<round>` for queued/running/testing,
`<name> asks`, `<N> awaiting review` (review + stuck + failed), joined by ` · `; `⚒ wb ·
delegate mode` with nothing listed. Pane: one row per task (a mark per phase: spinner running, ◌
queued, ◐ testing, ? asking, ◆ review, ▲ stuck, ✗ failed, ✓ accepted, – discarded; name, phase,
round, label or model, tests, age since creation, or "ago" once closed), its recent lines; then
the followed task's transcript (its newest lines, oldest at the top, within `HISTORY_CHARS` 60,000: Claude Code drops pane
text past 100,000 characters, which once left the bubble empty), scrolling with the newest lines
unless the user scrolled up (`ui.scroll`); then the stage (the apprentice with a speech bubble,
pixels on a terminal at least 20 rows by 40 columns, else its one-line face; text wrapped by
`wrapWords` because Text's own wrapping beside the raster drew empty rows); the web chat link
last.

**The apprentice** (`apprentice.ts`): 16×12 pixel art as letters, drawn with half blocks
(▀/▄) into a `Raster` (cells encoded as little-endian u32 triplets, base64); a pose per state
(rest, blink, think, work, swing, tada, ask, slump, sleep), the chest lamp `L` one colour per pose
(orange while working, green tada, amber ask, red slump, slate sleep); `work` swings the wrench one
frame in six, other poses blink one in eleven, except `sleep` and `slump`; FACE has the one-line faces (`[•_•]⚒`, `\[^_^]/`,
…). The bubble says what the agent is doing (`speechOf`) in the phase's colour.

**The web chat listener** (`listen`, delegate mode only, needs the events child): another
session's live claim (its pid answers `kill -0`, else its `ts` within 15 s) makes this one stand
by; otherwise it writes `chat/listener.json` `{session, cwd, pid, busy}` when that changes, and
delivers each user message without a receipt: idle → `prompt.submit('[web chat] <words>', asUser)`,
receipt `how=now`; busy → `session.send` into the running turn with WEB_NOTE, else a queued
prompt (`how=queued`); then `wb chat ack <id> --by <key> --how …`, `webAt = now`, a toast.
A web message over 6 h old (`CHAT_EXPIRE_MS`) isn't submitted. Quitting stops delivery and, when
this session holds listener.json, writes it empty, so the page and other sessions see nobody
listening at once.

**The weekly model check** (`scoutIfDue`): after a main-loop turn, idle, no task open, once per
load: when scout.json's `done_at` is 7+ days old and `offered_at` 1+ day old, it writes
`offered_at` and submits SCOUT_NOTE (`wb scout`, research, recommend keep/add/retire, change
nothing without a yes, `wb scout --done --notes`).

**The weekly update check**: after the first main-loop turn of a load, once, `wb update --check
--if-due` (wb asks origin at most weekly and answers from `update.json` in between); an `update
available` answer becomes a toast naming `/delegate-update`. So a pending update is mentioned once
per session until it's installed.

**Tests** (`register.test.tsx`, an in-memory filesystem and a fake `wb events`): the empty pane,
`/delegate-pane`, a wake held mid-turn and delivered once (and a stale one dropped), the transcript
and pinned link, both quit commands (typed words stop nothing), KEEP_ON_COMPACT and the boundary
compaction, the live header label, the weekly check, the chat listener (exact text, receipt,
standing by, stopping after a quit), Bash `wb start` turning the mode on and mid-turn delivery,
rewake, Raster vs face, `/delegate` alone, the update check and `/delegate-update`, `wrapWords`. Not covered: wake off, check-in and
question wakes, the one-hour cutoff, scrolling, `WB_COMPACT_AT`, the ears respawn.

**Troubleshooting.** If `wb` isn't on Claude Code's PATH the mod fails quietly (the ears respawn
every 2 s, the `workbench` context block is left out): set `WB_BIN`. `claude plugin validate mod`
lists the hooks, calls and env reads; `claude --debug` shows hook errors.

## Waiting without polling

`workbench/watch.py`. Anything that waits for a file or a process sleeps in the kernel.

- **`Watcher(*dirs, recursive=False, skip=())`**: inotify through ctypes (`inotify_init1`,
  `inotify_add_watch`) on directories, for modify, attribute changes, close-write, create, delete and moves; a queue
  overflow or a directory that went away reports every watched directory as changed rather than
  losing the news. `recursive` adds new subdirectories as they appear; `skip` leaves out
  directories by pattern, matched against the path below the root (`tasks/*/worktree`).
  `wait(timeout, settle=0.02)` returns the changed paths (raw: duplicates, temp and lock files
  included), sleeping 20 ms after a wake to gather a burst. A directory inotify can't watch (past
  `max_user_watches`, a permission error) is polled instead: its entries are stat'ed every 0.25 s
  (`POLL_S`). With no inotify at all (macOS, or `inotify_init1` failing) `wait` returns within a
  second whatever the timeout, so every caller loops and re-checks its condition. Callers use
  `wait()`, not the file descriptor, so the polling fallback works; the chat `Hub`, which selects
  on the descriptor beside a pidfd, switches to `wait()` when any directory is polled.
- **`wait_exit(pid, timeout)`**: `pidfd_open` and `select`; without pidfd (macOS, kernels before
  5.3) `kill(pid, 0)` every 0.1 s.
- Writers help: `store.write_json` writes a dot-temp file and renames it, `read_jsonl` stops at the
  last newline (a half-written row waits for the next read), `append_jsonl` holds a lock file.

Every waiter and its bounded wake (a timeout to re-check what the kernel can't signal):

| Caller | Waits on | Re-checks every |
|---|---|---|
| `wb events` | `$WB_HOME`, recursive, skipping `tasks/*/worktree`, `tasks/*/rounds`, `imports`, `images` | 10 s (exits when its parent changes) |
| `wb wait` | the state directory and the named task's directory | 60 s (a dead runner) |
| `wb log -f` | the task directory | 30 s |
| `wb watch` | the task directory | 1.5 s on a terminal, 30 s otherwise |
| the runner's steer bell | the task directory (`steer.jsonl`) | 5 s, while OpenCode runs |
| `ask_reviewer` | `ask/` | 60 s, until the ask timeout |
| the chat `Hub` | `chat/` and the listener's pidfd | 60 s |
| `wb stop` / `takeover` | `wait_exit(pid, 15)` | |

`wb events` prints `{"hello": <its pid>}` (the mod's chat listener identity), then `{"changed":
[paths relative to $WB_HOME]}` for each batch of changes, dropping names
ending `.lock`, `.tmp`, `runner.log` and dotfiles; it creates `tasks/` and `chat/`, and exits on a
broken pipe or when its parent process changes.

## Every `wb` command

`workbench/cli.py` is argparse: `main()` builds one subparser per command with
`set_defaults(f=cmd_<name>)` and dispatches `a.f(a)`; a new command is a parser block plus a
`cmd_*` function. `die(msg, code=1)` prints `wb: <msg>` on stderr. Exit codes: 1 an error; 2 a
usage error, or `wait` on a closed task or a timeout; 3 no model can run (`start`); `scout --due`
0 when due, 1 when not; `doctor` 0/1. cli never writes notify.jsonl (the runner and the ask server
do). `wb --version` prints the version (`workbench.__version__`).

**Acknowledging.** `_ack_all(task)` moves the task's `acked` mark: if an unacked notice of the
kind its phase stands for exists (`question` while `asking`, `checkin` while queued, running or
testing, else the phase name), the mark stops
just before the oldest such notice, so news Claude hasn't seen stays deliverable; otherwise it
moves to now; it never moves back (after a `wb note` seven seconds after a failure swallowed the
failure notice for an hour). It's called by feedback, next, answer, allow, note, resume,
takeover, accept, discard and `stop --all`; not by steer, `stop <task>`, `skill hand|take`, diff,
report or log. Errors `wb` raises itself (`RuntimeError`, a config that doesn't parse) print as a
message, not a traceback.

### Running tasks
| Command | Flags (defaults) | What it does |
|---|---|---|
| `wb start <task> --spec <file\|->` | `--repo` (.), `--size trivial\|significant` (significant), `--model <id or fragment>`, `--test <cmd>`, `--auto-fix N` (2), `--checkin MIN` (10 significant, 0 trivial), `--ask-budget N` (2), `--ask-timeout S` (1200), `--round-timeout S`, `--silence-timeout S`, `--think-timeout S` (0 off; unset or 0 for the other two: the measured budgets), `--test-timeout S` (900), `--no-base-check`, `--base REF` (HEAD), `--skill NAME…`, `--allow PATTERN…`, `--allow-path DIR…`, `--force` (queue a `--model` that can't run now), `--trial`, `--no-claude-md` | Checks the name (`^[a-z0-9][a-z0-9-]{0,47}$`, never reused), the repo, the spec and the skills; picks the model (router, or `--model`; none runnable: exit 3); checks `CLAUDE.md` is committed at the base; makes the worktree and branch `delegate/<task>`; writes task.json, round 1's prompt (spec + PROTOCOL) and status; spawns the runner. Prints the model and why, the slice, Claude's spec record, the handed or suggested skills, notes, `next: wb wait <task>`. With `--test` (and not `--no-base-check`) it first runs the acceptance command once in the fresh worktree, the base, inside the agent's lane (`runner.base_check`, at most `BASE_CHECK_SECS` = 120 s, the output in `base-tests.txt`, the result in status `base_check`) and prints what it saw: a warning when it already passes (it can't tell when the step is done), else its exit and last 8 lines, for Claude to check it fails only for what the step changes; past the limit, a note that it wasn't checked. |
| `wb feedback <task> --text "…" \| --message <file\|->` | `--fresh` \| `--keep-context`, `--spec-gap "…"…` | Phase review/stuck/failed and no live runner. Writes the next round's prompt (FEEDBACK_HEAD + "Make exactly these changes, then re-run the acceptance command and finish with a short summary:" (the re-run only when there is one) + the text), reviews+1, spawns the runner. Warns past three review rounds. Gathers lessons. |
| `wb next <task> --spec <file> -m "…"` | `--test` (replaces the acceptance command), `--skill` (replaces the set), `--fresh`, `--allow PATTERN…` (adds to the task's grants), `--spec-gap` | Phase review. Reads the spec (a file or `-`; empty or unreadable: nothing changes), records the step's size and fit, commits the work (`<m>` + `Workbench-Task: <task> (step N)` + `OpenCode-Session:`), writes the next step's prompt ("Step N of this task is done and committed (<sha>). The next step:" + spec + PROTOCOL), records the spec gaps, resets reviews, spawns. Says whether the next step gets a fresh session and why (`runner.fresh_reason`). Gathers lessons. |
| `wb answer <task> --text "…" \| --file <f>` | `--id` (the pending question) | Writes `ask/<id>.answer` (atomically); the blocked `ask_reviewer` returns it. |
| `wb allow <task> ["<pattern>"…]` | `--path DIR…`, `--always`, `--text "…"` | Adds to the task's grants (`rules/<task>/grants.json`; and commands.json with `--always`, refused when it doesn't parse); answers any pending question with "Claude allowed … Run it again now and carry on."; if the agent is live, sends that as a `--now` steer so it restarts on its session with the rule. `--path` goes through `_lane_dir` (refuses `/` and home, creates the directory; `*` turns the lane off for the task). |
| `wb steer <task> --text "…" \| --message <file>` | `--now` | Only while queued/running/testing/asking with a live runner. Appends to steer.jsonl: delivered when the current run ends (before the tests), or at once with `--now`. |
| `wb note <task> --text "…"` | | A `captain` event: Claude's log line in the transcript. |
| `wb takeover [<task> \| --all]` | | Stops the runner, phase `review`, "Claude took over"; Claude edits in the worktree and accepts, or hands back with feedback. `--all`: every task that isn't closed. |
| `wb stop [<task> \| --all]` | | A live task only. SIGTERM the runner (15 s, `wait_exit`, then SIGKILL to its session and the agent's), phase `stuck`, "stopped by the reviewer"; the worktree is kept. `--all` (`stop_everything`): every open task, a `stopped` event "bail out: stopped by the user". |
| `wb resume <task>` | | Phase stuck/failed, no live runner: a fresh session (round n+1, `runner.RESUME` + what the step's rounds asked), not a review round. Acks the task and gathers lessons. |
| `wb accept <task> -m "…"` | `--merge`, `--force` (any open phase; stops a live runner), `--spec-gap` | Phase review; refuses a closed task. Records the step, commits (a last step with no new changes accepts the committed steps) (`<m>` + "Delegated to <label> via OpenCode: R round(s), F automatic test-fix, V review round(s)." + `Workbench-Task:` + `OpenCode-Session:`; no AI co-author line), removes the worktree, `--merge`s (`merge --no-ff --no-edit` into `base_ref`, which the main checkout must have out unless it is `HEAD`, with no changes to tracked files; a conflict is backed out; a failure is reported and the accept stands), writes the results row, prints how far CLAUDE.md is behind. Gathers lessons. |
| `wb discard <task>` | `--reason "…"`, `--spec-gap`, `--keep-branch` | Stops the runner, records the step, removes the worktree and (unless `--keep-branch`) the branch, writes the results row. Gathers lessons. Refuses a closed task. |

### Watching
| Command | Flags | What it does |
|---|---|---|
| `wb wait [<task>]` | `--timeout S` (0: none) | Blocks (inotify) until a notice past a task's `acked` arrives, prints `[<kind>] <task>\n<text>` and acks it (skipping a review, stuck or failed notice for a task live again). Named: a closed task exits 2; a task already waiting whose notice was taken elsewhere prints it again "(already delivered once)". |
| `wb status [<task>]` | `--all` | One line per open task: name, phase, round, model, age, idle, last action; `?` after queued/running/testing whose runner died. Named: also `worktree: <path>` while it exists. |
| `wb watch [<task>]` | `-v`, `--once`, `--no-color`, `--json` (hidden: one pass of JSON lines, the mod's pane) | The live transcript (`transcript.follow`); default task: the most recently updated open one, else the most recently updated task. |
| `wb log <task>` | `-n N` (0 all), `-f`, `-v` | The agent's events: tool calls (`HH:MM:SS rN tool brief  ✗ error`), text, questions, answers, stops, auto-fixes, rounds, tests, errors. |
| `wb report <task>` | `--round N` | The round's summary.md. |
| `wb diff <task> [paths…]` | `--stat`, `--all` | The worktree against the task's base, or after `wb next` against the last committed step (`--all`: the whole branch); new files included. |
| `wb events` | | `{"hello": pid}`, then `{"changed": [paths]}` whenever `$WB_HOME` changes (not tasks' worktrees and rounds, `imports/`, `images/`, `runner.log`, `*.lock`, `*.tmp`, dotfiles); exits when its parent does. The mod's ears. |

### Models, skills, lessons, notes
| Command | Flags | What it does |
|---|---|---|
| `wb models` | `--set-default` | Each model's state, slice and speeds; the router's ranking per size. `--set-default` writes the significant-work pick as OpenCode's `model`. |
| `wb model add <provider/model>` | `--pull`, `--label`, `--context N` | Registers an Ollama model in opencode.json, merged into any existing entry (see *Managing the set*). |
| `wb model retire <id>` | `--delete` | Unregisters it (not OpenCode's default) and drops its models.json overrides; `--delete` removes Ollama's weights. |
| `wb model sizes <id> [trivial] [significant]` | | Limits the sizes it's routed (none: every size). |
| `wb scout` | `--done [--notes "…"]`, `--due`, `--watch "…"`, `--unwatch N` | The weekly check's facts; `--done` records it; `--due` exits 0 when one is due (7 days); the watch list. |
| `wb skill list \| show <n> \| scan <n\|path>` | | The library, a skill with its history, the scan alone. |
| `wb skill add <n> --from <src> -m "…"` | `--source "<origin>, <license>"`, `--lesson ID…` | Check, scan, copy, commit. |
| `wb skill patch <n> --old … --new … -m "…"` | `--old-file`, `--new-file`, `--lesson` | An exact, unique replacement, then as add. |
| `wb skill import <url\|owner/repo\|path>` | `--path DIR`, `--as NAME` | Fetch into `imports/`, show license, hash, scan; never adds. |
| `wb skill retire <n> -m "…"` / `restore <n>` | | Move to and from `retired/`. |
| `wb skill hand\|take <task> <n…>` | | Change a task's skills from its next run. |
| `wb lessons` / `wb lessons close <id…> -m "…"` | `--task`, `--all` | Open lessons, recurring first (12 unless `--all`); close with why. |
| `wb notes` / `add "…"` / `drop N…` | | The user's standing delegation preferences (`notes.md`, `- ` lines, at most 20 of 300 characters). |
| `wb stats` / `wb tune` / `wb trial <task…>` | | Results by model and size (judged rows: it says how many spec-gap discards and trials it left out); the tuned settings; mark closed tasks as trials (results rows only). |

### Other
| Command | Flags | What it does |
|---|---|---|
| `wb image "<prompt>"` | `--n` (4), `--size WxH` (1024x1024), `--seed`, `--ref <img>…`, `--out DIR`, `--engine`, `--list` | Candidates from a local image engine (see *Images*). |
| `wb chat [log\|serve\|url\|reply\|ack\|pending]` | `--port` (1337), `--bind` (replaces chat.json's), `--host` (adds), `--text`, `--message <file\|->` (reply reads stdin without either), `-n` (10); `ack <id> --by --how` (the mod) | The web chat: the thread, the server, the link with its token, Claude's reply. |
| `wb init <dir>` | | A project the way Claude keeps one: git on the default branch, an initial commit, AGENTS.md → CLAUDE.md (refuses inside another repo or without a git identity). |
| `wb setup` | `--force` (replaces a link that points elsewhere; never a real file) | Links `~/.local/bin/wb` to this copy's `bin/wb` and Claude Code's `skills/delegate` to its `skill/delegate`, warns when `~/.local/bin` isn't on PATH; exit 1 when it left something in the way (see *Installing*). |
| `wb update` | `--check` (only say whether origin has news: `update available: N new commit(s)…` or `up to date`), `--if-due` (with `--check`: ask origin at most weekly, `UPDATE_EVERY`, else answer from `update.json`; never with `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`) | Updates this copy (see *Installing*): refuses while any runner is live; a marketplace copy runs `claude plugin marketplace update workbench` then `claude plugin update workbench@workbench`; a clone (refusing uncommitted tracked changes) runs `git pull --ff-only` first. Prints `updated <kind> a..b (N commit(s))` or `already up to date`. The fetch never prompts (`GIT_TERMINAL_PROMPT=0`, ssh `BatchMode`). |
| `wb doctor` | | `opencode --version`, the OpenCode config, git, models found and each one's readiness, each Ollama entry's `options.max_tokens` and context against what Ollama serves, the state directory, a real lane drawn around a test write (`ok agents write only in their lane (Landlock ABI N)`), `wb` on PATH (or `WB_BIN`) for the mod, where the mod lives. `ok`/`BAD` lines set the exit code; `--` lines inform. |

## Files and formats

```
$WB_HOME (~/.local/state/workbench)
  notify.jsonl                   notices that need Claude, all tasks (the runner and the ask server write them)
  results.jsonl                  one row per closed task: what the router, slices, tuning and skill records learn from
  lessons.jsonl                  what each task taught, open until written into a skill or closed
  notes.md  notes.lock           wb notes: how the user wants delegation run
  tuning.json                    optional: pin a tuned setting ({"fresh_at": 0.6})
  speed.json  speed.lock         each model's measured write and read speeds
  gpu.lock  gpu.json  gpu.admit  the GPU lease: the lock, who holds or waits for it, the admission lock
  scout.json  watch.json         the weekly model check: when it was done and offered; the watch list
  update.json                    wb update: when origin was last asked, how far behind this copy was
  imports/<slug>/                skills fetched by wb skill import, for Claude to read
  images/<YYYYmmdd-HHMMSS>/      wb image's candidates (unless --out), their .json sidecars, engine.log
  chat/                          messages.jsonl  receipts.jsonl  listener.json
  rules/<task>/                  outside the lane: opencode.json (the config layer), HANDBOOK.md,
                                 CLAUDE.md (a copy of the user's), bin/git (the shim),
                                 grants.json grants.lock (what Claude allowed: {"allow", "paths"})
  tasks/<task>/                  inside the lane (the ask server writes here)
    task.json  status.json  status.lock
    events.jsonl(.lock)  steer.jsonl(.lock)  acked  context.md  runner.log
    rounds/<n>/                  prompt.md  instructions.md (before a handover rewrote it)  summary.md  tests.txt
    ask/<id>.json  <id>.answer   questions and permission requests; their answers
    worktree/                    the agent's git worktree, branch delegate/<task>
~/.local/share/workbench/skills/ ($WB_SKILLS) the skill library, its own git repo; retired/
~/.config/workbench/
  models.json ($WB_MODELS)       what OpenCode can't say about each model or provider
  images.json ($WB_IMAGES)       image engines
  commands.json ($WB_COMMANDS)   the user's standing allowances: {"allow": [patterns], "paths": [dirs], "env": [names]}
  chat.json ($WB_CHAT_CONFIG)    the web chat's {"bind": [...], "hosts": [...]}
  chat-token ($WB_CHAT_TOKEN)    the web chat's token, mode 600
~/.config/opencode/opencode.json (or .jsonc under $XDG_CONFIG_HOME; $WB_OPENCODE_CONFIG) the models
```

**Store primitives** (`workbench/store.py`). `home()` is `$WB_HOME`, else
`~/.local/state/workbench`. `write_json` writes `.<name>.<pid>.tmp` beside the file and renames it
(no fsync); `read_json` returns the default for a missing or broken file. `append_jsonl` appends
one line under `<file>.lock` (flock); `read_jsonl(path, offset)` returns the whole lines and the
offset after the last one, skipping bad lines. `results(trials=False)` leaves out trial rows;
`mark_trials` rewrites results.jsonl under its lock. `Task(name)`: `spec` (task.json) and `status`
are re-read on every access; `set_status(**kw)` is a locked read-modify-write that adds
`updated_at`; never write status.json directly. `all_tasks()` lists task directories with a
task.json, oldest first.

**task.json** (written by `wb start`; `wb next --test` rewrites `test_cmd`): `name`, `repo`,
`base` (the full sha), `base_ref`, `branch`, `model`, `model_label`, `opencode_model`, `route` (the
router's reason), `size`, `stack`, `trial` (when set), `test_cmd`, `auto_fix`, `ask_budget`,
`ask_timeout`, `checkin`, `round_timeout`, `silence_timeout`, `think_timeout`, `test_timeout`,
`created_at`, `wb_version`. Read but written by no command: `stuck` (the guards' thresholds),
`deny_skills`. A missing key takes the runner's own default (auto-fix 0, think and
check-ins off), not `wb start`'s.

**status.json** keys and who sets them:

| Key | Meaning; set by |
|---|---|
| `phase`, `last`, `pid` | the phase; the last action in words; the runner's pid (cleared when it exits) |
| `round`, `round_started_at` | the runner round (every kind counts); when it started |
| `steps`, `step_round`, `step_sizes` | `wb next`: the step number, the round it began, the closed steps' `results.step` |
| `reviews` | review rounds in this step (`wb feedback` +1, `wb next` resets) |
| `auto_fixes`, `auto_fixes_total` | auto-fix rounds since the last review or step; earlier ones |
| `tests` | `{ok, exit, tail, round}` of the last test run |
| `tools`, `tok_in`, `tok_out` | task-wide tool calls and tokens (input without cache reads) |
| `session`, `sessions`, `session_mark`, `session_floor` | the OpenCode session; earlier ones; `gitops.mark` when it began; the context it had read at its first change |
| `context` | a one-round instruction: `fresh`, `keep`, `full` (a handover happened) |
| `full_pct`, `handovers` | the fill at the last mid-round handover; how many |
| `window`, `load_secs` | the model's context window; the load time |
| `tuned`, `explored`, `tuned_max` | the knob values this task runs at (`tuning.tune`) |
| `steer_taken`, `nudges`, `nudged_round`, `checkin_at` | notes delivered; nudges sent; the round that ended in one; the last check-in |
| `skills`, `skills_ever`, `skills_rev`, `skills_told` | the handed set; every skill held; the library rev of each; what this session was told |
| `asking`, `agent_pid` | the open question's id; the agent's pid while a round runs |
| `spec_gaps` | `[{round, text}]` from `--spec-gap` |
| `updated_at` | every write |

**events.jsonl** rows are `{ts, round, kind, …}`. From OpenCode, normalized (`events.normalize`):
`tool {tool, brief, ok, error?}` (`brief`: the input's command, path, pattern, url, query or
description, 90 characters; a skill's id), `text {text}` (2000 characters), `step {in, out,
cache}`, `error {text}`. From the runner: `round`, `stopped {text}`, `steer {now, by:
"workbench"}` (a nudge), `interrupted`, `tests {ok, text}`, `skills`, `fresh {overflow,
compacted}`,
`autofix`, `reading_full`, `full`. From the ask server: `ask {id}`, `answer {id}`. From `wb`:
`spec_gap`, `next_step`, `stopped` (bail out), `resumed`, `allow`, `steer {now}`, `captain`,
`takeover`.

**results.jsonl** (`results.record`, on accept and discard): `ts`, `task`, `outcome`
(`accepted`/`discarded`), `model`, `size`, `repo`, `stack`, `rounds`, `auto_fixes`, `reviews`
(every step's), `spec_rounds`, `tests_ok`, `minutes` (wall clock since `wb start`, queueing and
review included), `tok_in`, `tok_out`, `skills_offered`, `skills_loaded`, `skills_rev`, `steps`
(one `results.step` per step), `window`, `tuned`, `explored`, `tuned_max`, `spec_gaps`, `trial`
(when set); `commit` and `merged` on accept, `reason` on discard. A step (`results.step`): `files`,
`lines` (added, against HEAD, new files included), `window`, `landed`, `compacted` (its last
session, or one a handover replaced: a `fresh` event's `compacted`), `handovers` (fresh sessions
with overflow), `read_full`, `reviews`, `spec_rounds`, `stuck`, `took_over`.

**Other formats.**
- `steer.jsonl`: `{ts, text, now}`. `acked`: a float timestamp as text.
- `ask/<id>.json`: `{id, ts, round, question, context, command?, path?}`; `<id>.answer`: the text.
- `lessons.jsonl`: `{ts, task, repo, skills, id, kind, round, text}`; a close is `{close: id, ts,
  how: "skill:<name>"|"dropped", why}` (*Lessons*).
- `speed.json`: `{"<model id>": {"write": [tok/s…], "read": [tok/s…]}}`, the last 60 of each.
- `gpu.json`: `{"<pid>": {model, task, waiting?}}`.
- `scout.json`: `{done_at, notes, offered_at}` (`offered_at` from the mod); `watch.json`: `[{ts, text}]`.
- `update.json`: `{checked_at, behind, kind}` (`kind`: `marketplace` or `clone`); a failed check
  writes only `checked_at`, so it waits a week too.
- `tuning.json`: `{"<knob>": value}`, a pin for every model.
- `notes.md`: one `- <note>` line each.
- The image sidecar: `{prompt, seed, width, height, engine, refs, secs, ts}`.
- The chat files: *The web chat*.

## Installing

**Users** install from the marketplace (README's quickstart):
```sh
claude plugin marketplace add a-schaefers/claudes-workbench
claude plugin install workbench@workbench
~/.claude/plugins/marketplaces/workbench/bin/wb setup
wb doctor
```
The plugin install copies only `mod/` (the marketplace entry's `source`) into Claude Code's plugin
cache, but `marketplace add` keeps a full clone of the repo under
`~/.claude/plugins/marketplaces/<the marketplace's name>` (`installLocation` in
`known_marketplaces.json`; elsewhere under `CLAUDE_CODE_PLUGIN_CACHE_DIR`). `wb setup` links `wb`
and the `/delegate` skill from that clone, so `wb` runs from the clone the way it does from a
developer's. Upgrading is `wb update` (or `/delegate-update`, which reloads the plugins after):
`claude plugin marketplace update workbench` pulls the clone (the links follow it), `claude plugin
update workbench@workbench` refreshes the mod. The mod runs `wb update --check --if-due` once per
session, so a new commit is noticed within a week.
The mod's copy moves only when `mod/.claude-plugin/plugin.json`'s `version` does, so a change to
`mod/` that users should get bumps it. A third-party marketplace doesn't auto-update unless the
user turns that on (`/plugin` → Marketplaces).

**Developing**: a clone, with the links pointing into it and the clone itself as the marketplace,
so the plugin, `wb` and the skill all read the working tree (the README's install until
2026-11-16):
```sh
git clone https://github.com/a-schaefers/claudes-workbench ~/repos/workbench
# to upgrade later: git -C ~/repos/workbench pull, then restart Claude Code (or /reload-plugins);
# the links and the plugin read from the clone, and your state and skills stay as they are
cd ~/repos/workbench
mkdir -p ~/.local/bin ~/.claude/skills   # ~/.local/bin must be on your PATH
ln -s "$PWD/bin/wb" ~/.local/bin/wb
ln -s "$PWD/skill/delegate" ~/.claude/skills/delegate
claude plugin marketplace add "$PWD"
claude plugin install workbench@workbench
wb doctor
```
(`bin/wb setup` makes the same two links; `wb update` pulls with `--ff-only`, then the same two
`claude plugin` steps, since a path marketplace is copied too.) Moving from one to the other: `wb setup --force` from
the copy you want repoints the links; the state directory and the skill library don't move.

## The tests

Run from the repo root (`tests/__init__.py` makes `tests` a package; the other modules import
`tests.test_workbench`): `python3 -m unittest` (about 155 tests in six modules; about two minutes),
one class or test with `python3 -m unittest tests.test_workbench.Context[.test_…]`, by name with
`-k handover`. The mod's tests: `claude plugin validate mod && claude plugin test mod`
(`mod/hooks/register.test.tsx`). The web chat in real browsers: `PLAYWRIGHT=…/node_modules/playwright
node tests/e2e_chat.mjs`.

**CI.** `.github/workflows/check.yml` runs `python3 -m unittest` and `claude plugin validate mod
&& claude plugin test mod` on every push to main and every pull request, with the Claude Code the
mod was last verified with (`CLAUDE_CODE_VERSION`); `nightly.yml` runs the same once a day on the
newest Claude Code, so a red nightly means a new release broke the mod, not that main is broken.
The README shows both as badges, beside the floor the mod needs (Claude Code 2.1.287, the floor
for mods). A GitHub runner has no `opencode` and may lack Landlock, so the RealOpenCode and Lane
tests skip there: CI is not where they count. Not run in CI: the browser test and a real run.
`.github/dependabot.yml` moves the actions' SHA pins weekly. `check` is the required check on
main (*Shipping to main*).

**Files.**
- `tests/test_workbench.py`: end to end, one class per feature (Lifecycle, Context, Steering,
  Watch, AskServer, ConfigLayer, AgentContext, SkillLibrary, RealOpenCode, GitGuard, Lane,
  Permission, Images, Discovery, Routing, Tuning, Notes, Projects, ModelSet, WebChat, Hardware,
  Detector, SkillLearning, RealOpenCodeSkillFiles, Drift).
- `tests/test_gpu.py`: sharing the GPU (side by side, taking turns, unknown VRAM, an exclusive
  holder) and `can_share`.
- `tests/test_portable.py`: discovery against real HTTP servers on port 0 (the Ollama probe, the
  port guess, multi-model servers, loopback and host names, `local: true`, `opencode.jsonc` under
  `XDG_CONFIG_HOME`, `CLAUDE_CONFIG_DIR`), the polling watcher (`watch._libc = False`), the
  launchers on an old Python or native Windows, fit, budgets.
- `tests/test_sizing.py`: the slice guess and its correction from real steps.
- `tests/test_workflows.py`: the merge gate, in plain text (no YAML library): every action pinned
  to a commit with its version, every workflow with its own permissions, `check` never
  path-filtered and required by the ruleset from GitHub Actions, the ruleset with no bypass, and
  `owner-merge.yml` approving only the owner's pull requests without running their code.
- `tests/test_docs.py`: the docs cover the code: every `wb` command and flag (from `cli.main`'s
  parser) in *Every `wb` command*, every module in CLAUDE.md's layout, every `WB_*` variable the
  code or the mod reads in CLAUDE.md, CLAUDE.md under 25,000 characters.
- `tests/fake_opencode.py`: the fake OpenCode; `tests/capture_llm.py`: a recording model server
  for real-OpenCode checks; `tests/e2e_chat.mjs`: the browser test.

**The fixture** (`Base`). A temp dir with `WB_HOME=<tmp>/state`, `WB_OPENCODE_BIN` = the fake
(a command line, split with `shlex`), `WB_FAKE_MODELS=1`, `WB_OPENCODE_CONFIG` (a fixture with
providers `ollama` on 127.0.0.1:11434 holding `mimo-v2.6:9b-q8_0`, which has a 32K
`limit.context`, and `qwen3.8-27b` with none; `strata` on :8080; `cloud` on a remote host that
discovery drops), `WB_MODELS`, `WB_SKILLS`, `WB_COMMANDS`; a git repo `<tmp>/repo` on `main`
with README.md and a committed CLAUDE.md and a repo-local identity; a spec "Write the answer to
answer.txt."; the acceptance command `grep -qx 42 answer.txt`. `self.start(name, scenario,
*extra)` sets `FAKE_SCENARIO` and runs `wb start … --model mimo`. tearDown restores the env and
deletes the temp dir; it kills nothing, so a test stops its own `hang`/`slow` tasks.

**Helpers.** `run_wb(*argv)` runs `cli.main` in-process and returns stdout (a `die` is
`SystemExit`, its message on stderr: `assertRaises(SystemExit)` + `redirect_stderr`).
`wait_phase(name, phases, timeout=30)` waits until the phase is one of `phases` and the runner has
cleared its pid (so it can't wait for `running`; poll status for that). `write_skill`,
`RealOpenCode.run_calls`, and per-class helpers.

**What's real.** `wb start` spawns the real runner (`python -m workbench.runner`), which runs the
fake through the real config layer, git shim and lane; real git, real worktrees, real tests, real
gpu.lock. `WB_FAKE_MODELS` short-circuits every model-server call (probe, status, weights,
residency, free_gpu, budget, need, Ollama's show/pull/delete, images' settle); `WB_FAKE_GPU_GB`
sets the fake GPU's size (unset: VRAM unknown, so no sharing). The runner keeps the env it was
spawned with: a `FAKE_*` change reaches only runners spawned later (feedback, next, resume spawn
new ones).

**The fake OpenCode** (`fake_opencode.py`). Handles `run …` (prompt = last argument, `--session`
continues) and `session export <sid>` (a fixed export: one user message, one assistant message
with tokens `FAKE_FILL` or 1200, an edit of answer.txt 41 → 42, text; a compaction message when
`FAKE_COMPACTED`). It streams one JSON event per line (`step_start`, `tool_use`, `text`,
`step_finish` with tokens). It records each run's prompt to `FAKE_PROMPTS` and `{session,
prompt}` to `FAKE_RUNS`; `FAKE_SLEEP` pauses after `step_start`; `FAKE_LESSON` adds a `Lesson:`
line. Scenarios (`FAKE_SCENARIO`): `ok` (writes 42), `fix` (41 in a new session, 42 when
continued: an auto-fix round passes), `loop` (the repeat guard stops it), `hang`, `crash` (exit 1,
no events), `slow` (hangs in a new session, ok when continued), `fill` (a 25K-token step at 76%
of 32K, then sleeps: the runner hands over mid-round; the handover prompt turns it into `ok`),
`read` (25K tokens before any change: it carries on), `floor` (a re-read floor too high to hand
over: `HANDOVER_ROOM`). `session list` lists nothing. Its
live tool names are 1.x-style (`bash`, `filePath`); real OpenCode 2.x says `shell`, `path`
(`events._brief` reads both).

**Real-OpenCode checks** (`RealOpenCode`, `RealOpenCodeSkillFiles`, skipped without `opencode`
on PATH; the lane ones also need Landlock, `lane.abi()`). `capture_llm.py <port> <out.jsonl>
[calls]` is an OpenAI-compatible server that records every request and answers each request
that offers tools with the next scripted tool call (`{"name", "arguments"}`), so a test sees what
OpenCode does with it. `run_calls(name, calls, *start_flags)` starts a task, points its config
layer at the capture server, runs `opencode run --auto` in the worktree through `agent_env` and
`lane.wrap`, and returns the task and the tool results, in order. Refusals read `permission.rejected` (a
rule), `external_directory` (the file tools, the workdir) or `Permission denied` (Landlock).
These tests are the only checks of the OpenCode quirks and the lane: a skip is not a pass, so a
change to `context.py`, the config layer, `gitguard.py` or `lane.py` needs them run where they
don't skip. The event stream and the session export are only imitated by the fake: a change to
how the runner invokes or reads OpenCode updates `fake_opencode.py` in the same commit and needs
one real run (CLAUDE.md, *Checks before committing*).

**The browser test** (`e2e_chat.mjs`): the real `bin/wb chat serve` on port 13371 (`WB_E2E_PORT`) against a temp
`WB_HOME`, a fake listener claim, WebKit as an iPhone 13 and Chromium (`$CHROMIUM`, else
`/usr/bin/chromium` when it exists, else Playwright's own) as a
Pixel 7: the token leaves the address bar, a send lands once with its tick, the receipt, "working"
then the reply, a message sent during an outage lands exactly once, the stream recovers, a
reload doubles nothing, no script errors.

**Conventions for a new test.** Names read as sentences (`test_a_note_does_not_swallow_a_failure_claude_has_not_seen`);
one class per feature with a one-line docstring; end to end through `self.start`/`wait_phase`/
`run_wb`, asserting the phrases Claude reads and the state files; real processes and servers on
port 0 rather than mocks (only `chat.tailscale` and Hardware's probes are mocked); a regression
test cites the incident and its date; task names `t-<slug>`; a new fake behaviour is a new
`FAKE_SCENARIO`; anything that depends on OpenCode goes through `run_calls`; kernel-specific
tests `@unittest.skipUnless(lane.abi(), …)`; reuse helpers by import or assignment, never by
subclassing a class that has tests; new classes go above the `__main__` block (each module also
runs as a script).

**Not hermetic.** The real HOME is used (`~/.claude/CLAUDE.md` enters task instructions; the lane
test makes and removes `~/.wb-lane-test-*`), the real-OpenCode tests use OpenCode's own session
storage, test_portable probes 127.0.0.1:11434, `wb scout` reads the real hardware. It never
touches the real state directory, gpu.lock or a model, so it's safe beside running tasks.

## Shipping to main

Ported from backseat-driver (2026-11-16), which has run it since 2026-10-10. main takes no direct
push: every change is a pull request that merges once `check` is green and it is approved.

- **The ruleset** (`.github/rulesets/main.json`, on the default branch): no deletion, no force
  push, a pull request with one approval (stale approvals dismissed on push), and `check` green
  from GitHub Actions (integration 15368, so a status posted by anyone else doesn't count). No
  bypass actor: the owner and every agent get the same rule.
- **Auto-merge for the owner only.** `.github/workflows/owner-merge.yml` (`pull_request_target`)
  approves a pull request whose author and event sender are both user id 26800291 (a-schaefers;
  ids, since a login can be renamed and taken), from this repository, not a draft, at the head it
  was woken for, then arms auto-merge if nobody has. Claude sessions open pull requests and push as
  the owner, so they count as the owner. Anyone else's (other people, Dependabot, forks) waits for
  the owner's approval by hand; so does the owner's own after someone else pushes to it. A draft
  is how the owner holds one back. It runs main's copy of the file and never the pull request's
  code: no checkout, no `uses:`, nothing from the event in a shell but numbers, the head hash, the
  repository and the token (`tests/test_workflows.py` holds it to that).
- **Who arms auto-merge** decides whether main gets its own `check` run: a merge armed with the
  workflow's token is GitHub Actions' push, which starts no workflow. So `scripts/ship.sh` arms it
  as the owner; `owner-merge.yml` arms it only when nobody has, and then the nightly is main's only
  run.
- **`scripts/ship.sh`**, the one road to main: from a branch or a worktree (a detached HEAD ships
  as `agent/<worktree>`), it pushes, opens the pull request or finds the open one, arms auto-merge
  as you with `--match-head-commit`, and waits for the merge or names the failing check. It
  refuses to ship from main and acts only on this repository's own pull request at the commit it
  pushed. `--title`, `--body-file`, `--squash`, `--no-wait`, `--draft`. A red check leaves
  auto-merge armed: fix, commit, run it again.
- **`scripts/github-settings.sh check|apply`** keeps the repository's settings in git (`check`
  names any drift, `apply` writes them; `gh` as admin): the ruleset; Actions may approve pull
  requests, the default token read only; auto-merge, merge commits, delete branch on merge, update
  branch; only GitHub-owned and verified actions, each pinned to a commit; a fork's workflows wait
  for approval every time; secret scanning with push protection; Dependabot alerts and security
  fixes. The first time, `owner-merge.yml` must be on main before the ruleset is applied, or the
  owner's next pull request has nobody to approve it.
- **The emergency door**: when `check` itself is broken and its fix can't pass it, the owner sets
  the ruleset's `enforcement` to `disabled` (Settings → Rules, or the JSON and `apply`), merges the
  fix, and sets it back (`check` shows the drift if that's forgotten). Never a bypass actor.

## Couplings: change one, change the other

- **Anything a reader looks up** (a command, flag, default, guard, key, file, prompt text,
  environment variable) → this file, CLAUDE.md if it names it, and the skill
  (`skill/delegate/SKILL.md`) if Claude uses it there; README only for the quickstart. CLAUDE.md's
  *Keeping the docs true* is the procedure; `tests/test_docs.py` checks what it can (every command
  and flag here, every module and environment variable in CLAUDE.md, CLAUDE.md's size).
- **A prompt's opening words** (the heads in *The runner, inside*) → `transcript` labels,
  `round_asks`, `lessons`, the fake OpenCode, and the tests that quote them.
- **`cli.PROTOCOL`** → the transcript's fold marker `\n---\nHow to work`, the `Lesson:` parser,
  the handbook (they shouldn't contradict), and every agent's prefill (keep it short).
- **`agent/HANDBOOK.md`** → every agent prompt carries it; each token costs prefill time.
- **How OpenCode is invoked or read** (`run_round`'s argv, the event fields `normalize` reads, the
  export fields `context_state`, `measure_speed` and `transcript` read) → `tests/fake_opencode.py`
  in the same commit, and one real run (`wb start --trial`, CLAUDE.md).
- **Permissions or the config layer** (`context.py`, `write_config_layer`) → the real-OpenCode
  tests, run where they don't skip.
- **The lane** (`context.writable`, `readable`, `SECRETS`, `lane.py`) → the Lane and RealOpenCode
  tests on a Landlock machine; the protocol's lane line and the handbook's boundaries.
- **`wb watch --json`** (a style, the order, the activity wording) → the mod's `LINE_STYLE`, its
  pose regex, the `live-1` tag.
- **A `$.state` atom's type** in the mod → its tag (`tasks-1`, `live-1`, `chat-2`).
- **A notice kind or its text** → the mod's `HEADLINE` and `NEXT`, `_ack_all`'s still-true rule,
  the mod's still-true rule, `wb wait`.
- **A task.json or status key** → *Files and formats*; a key the router, tuning or skills read from
  results → `results.record` and the readers' defaults for old rows.
- **A new tuned knob** → `tuning.KNOBS`, read with `tuning.value(task, name)`, a ceiling in
  `drive` if it has one.
- **`chat.py` or `chat_static/`** → the browser test.
- **The `$` uses in `register.tsx`** → only through functions declared at its top level, or
  `claude plugin validate mod` fails.
- **The Claude Code version** the mod is verified with → `CLAUDE_CODE_VERSION` in
  `.github/workflows/check.yml`; the floor it needs → the README's badge and *What you need*.
- **The merge gate** (`owner-merge.yml`, the ruleset, `check.yml`'s job name) →
  `tests/test_workflows.py`, `scripts/github-settings.sh apply`, *Shipping to main*.
- **`mod/` for users** → bump `mod/.claude-plugin/plugin.json`'s `version`, or a marketplace
  install keeps the old copy (*Installing*). **`wb setup`'s links** → README's quickstart and
  *Installing*.
- **A model name** → never in code or the skill: models come from OpenCode's config, choices from
  the router and the weekly check.
