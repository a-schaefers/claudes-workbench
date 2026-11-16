<p align="center"><img src="docs/logo.jpg" alt="Claude's Workbench: a warm light directing three little robot craftsmen at a workbench" width="720"></p>

# Claude's Workbench

**Claude plans and reviews. Your gaming PC writes the code.**

The goal: move to a hybrid workflow on an ordinary gaming PC without giving up the quality of
a current-generation Claude. Open coding models you can run at home are said to code at roughly the level of
last year's best cloud models, but left alone they wander, overthink and cut corners. workbench
puts Claude in charge of them: Claude plans the work, hands each piece to a local model, checks the
result like a strict senior reviewer, and sends it back until it's right. Claude spends its tokens
on thinking and judging; your GPU spends electricity on typing.

The hope is a smaller Claude bill: a cheaper subscription, or one day no subscription at all, just
the Claude API for the driver and local models for the heavy lifting. Whether it pays off depends
on how much you use it and what your electricity costs (a busy GPU isn't free). But it's a fun
project, and it works.

- **Claude is the lead.** It writes a precise spec for each piece of work, with the files to touch
  and a test that proves it's done.
- **A local model does the typing** in OpenCode, in its own copy of your repo (a git worktree), so
  your working tree is never touched until Claude accepts the change.
- **Tests come first.** Failing tests go back to the agent automatically; Claude only looks at work
  that passes, a question the agent asks, or an agent that got stuck.
- **Claude holds the bar.** It reads every diff itself, never trusts the agent's summary, and fixes
  or discards anything that isn't up to its own standard.
- **Small pieces.** Local models work in a small context, so one concern and a couple of files per
  task. A step that never finished as one big task finished in half an hour as three small ones.
- **Claude builds a skill library for the agents.** When review teaches something the next agent
  will need too (a library's quirk, a pitfall, a check to run), Claude writes it down as a skill.
  Each agent gets only the skills that fit its task, and Claude takes them back when that part is
  done, so a growing library never clutters a small model's prompt. Claude tracks which skills
  actually help and retires the ones that don't.

Built and used on: **Ryzen 5 7600X · Radeon RX 9070 XT (16 GB) · 32 GB DDR5 · Arch Linux.**

## Quickstart

### What you need

- **[Claude Code](https://claude.com/claude-code)** with a Claude subscription or an API key
- **[OpenCode](https://opencode.ai)**, which runs the local models as coding agents
- **A local model server**, usually [Ollama](https://ollama.com), and at least one coding model
  registered in OpenCode (no models yet? workbench recommends some for your hardware and helps you
  set them up)
- A GPU with about 16 GB of VRAM or more, Python 3.11+, git, and Linux

### Install

```sh
git clone https://github.com/a-schaefers/claudes-workbench ~/repos/workbench
cd ~/repos/workbench
ln -s "$PWD/bin/wb" ~/.local/bin/wb
ln -s "$PWD/skill/delegate" ~/.claude/skills/delegate
claude plugin marketplace add "$PWD"
claude plugin install workbench@workbench
wb doctor
```

### Use it

Open Claude Code in your project and type:

```
/delegate add a high-score screen to the game
```

That's it. Claude takes over from there:

1. It checks your hardware and your local models. If you have none, or better ones would fit your
   GPU, it recommends them and offers to set them up (it always asks before downloading).
2. It plans the work and splits it into small pieces a local model can finish.
3. It decides how hard each piece is and hands it to the right local model: chores (a rename, a
   config tweak, a small test) to a small fast model like MiMo 9B, real work (features,
   refactors, tricky bugs) to the strongest model your GPU runs, like Qwen 27B. Then it watches
   the agent work.
4. It reviews every change, sends it back with notes until it's right, and only then commits it.

A **Workbench** pane in Claude Code shows the agents working live. You talk to the driver, Claude,
on the left; Claude talks to the local agent, which does the work on the right:

<p align="center"><img src="docs/driver-and-agent.jpg" alt="Claude Code with the Workbench pane: on the left Claude writes review feedback for a task; on the right the local agent's live transcript shows its build, its passing tests and 'done: waiting for Claude's review'" width="900"></p>

Here Claude (left) has just sent the agent review feedback on a game step: two exact changes, and
why. On the right the agent made them, built the game, ran the tests, and summed up its work;
the tests passed, so the task now says **done: waiting for Claude's review**, and Claude is woken
to read the diff.

| Command | What it does |
|---|---|
| `/delegate <task>` | Claude plans the task and hands it to your local models |
| `/workbench` | open the pane: every delegated task, and the agent's work live |
| `/delegate-models` | check now which local models suit your hardware best (it also happens weekly) |
| `/delegate-quit` | stop every agent now; their work is kept for later |
| `/delegate-quit-and-hand-back-claude` | stop the agents and have Claude finish their tasks itself |

**Away from the computer?** `wb chat url` prints a private link to a small web chat with the
running Claude session (`systemctl --user link ~/repos/workbench/systemd/wb-chat.service &&
systemctl --user enable --now wb-chat` keeps it running). It's `http://localhost:1337`; to use it
from your phone, reach your PC the way you normally do (Tailscale, for example) and change the host
in the link.

<p align="center"><img src="docs/web-chat.png" alt="The web chat: Claude's report on which model takes which tasks, the user's reply, a receipt saying Claude has it and will read it after its current step, and 'Claude is on it'" width="420"></p>

**Watching from a terminal?** `wb watch` follows the newest task live, and `wb status` lists the
open ones.

## What we found (November 2026, on the PC above)

- **Qwen 27B** (Ollama, Q3) does the real work: it fits entirely on a 16 GB card and passed nearly
  every task we gave it. **MiMo 9B** takes the trivial chores.
- Bigger models squeezed onto the card with system RAM were slower, crashed, and were no better.
- Quantization matters (higher is better), but on 16 GB a larger model at about 4 bits beats a
  smaller one at 8.
- Thinking mode off works best for agent work: the model writes code instead of planning it at
  length.

Models change fast. Once a week workbench asks Claude to check what's new for your hardware and
recommend changes; nothing is downloaded without your yes.

<hr>

## How it works

This part is the full picture: every piece, what it does, and why it's built that way.
Development notes for contributors (and for Claude) are in [CLAUDE.md](CLAUDE.md).

### The pieces

```
 you ─▶ Claude Code ─────────────────────────────────────────────────────────────┐
         │ /delegate skill: plan, spec, review          workbench mod: pane,     │
         │ (skill/delegate/SKILL.md)                    status line, wake-ups,   │
         ▼                                              web chat, compaction     │
        wb ──────── writes ────▶  ~/.local/state/workbench/  ◀── watches (wb events)
         │ spawns                   tasks/<name>/…  notify.jsonl  results.jsonl  chat/
         ▼                                 ▲
      runner (detached, one per task) ─────┤ writes status, events, reports, notices
         │ GPU lock, loads the model       │
         ▼                                 │
      opencode run  in tasks/<name>/worktree (branch delegate/<name>)
         │      └─ ask_reviewer (MCP server) ─ writes the question, blocks for the answer
         ▼
      local model server (Ollama, or any OpenAI-compatible server on this machine)
```

- **`/delegate`** (`skill/delegate/SKILL.md`) is Claude's procedure: size the work, write the
  spec, dispatch, drive, review, accept. Only you start it; Claude never delegates on its own.
- **`wb`** is the CLI Claude drives. Every command does one thing and prints a short, plain
  report, because Claude reads it and Claude's tokens are the scarce resource.
- **The runner** is a detached process per task. It drives the agent round after round until the
  task needs Claude, then exits. Nothing has to stay open for a task to run.
- **OpenCode** runs the local model as a coding agent, headless, in the task's own git worktree.
- **The workbench mod** is a Claude Code plugin. It draws the pane, keeps a status line, and starts
  a turn for Claude the moment a task needs it, so Claude never polls.
- **The web chat** is a small page and server for reaching the same Claude session from a phone.

Everything talks through plain files in one state directory. The CLI, the runner, the agent's
`ask_reviewer` tool, the mod and the web chat never call each other; they read and write the same
files, and anything that waits sleeps in the kernel (inotify on directories, pidfd on processes)
until one of them changes. A crashed piece loses nothing, and every piece can be inspected with
`cat`.

workbench is Python's standard library only, plus the TypeScript mod. No model is named anywhere
in the code: models come from OpenCode's config and the router decides, so a new model is one
config entry away.

### A task, start to finish

```
 wb start ─▶ queued ─▶ running ─▶ testing ──pass──▶ review ─▶ wb accept ─▶ accepted
               ▲  (GPU)    │  ▲         │                 │
               │           │  └─ fail: auto-fix round     ├─ wb feedback / wb next ─▶ queued
               │           │     (up to --auto-fix)       └─ wb discard ─▶ discarded
               │           ├─▶ asking: ask_reviewer, blocked until wb answer
               │           └─▶ stuck / failed ─▶ wb feedback ─▶ queued, or wb discard
               └── a steer note or an interrupt continues the same session
```

**1. Claude writes a spec.** The skill tells Claude to size every task to what the local models
actually finish: one concern, at most two or three files and about 150 new lines, with the files
named, the code to imitate pointed at, exact names for anything other code relies on, and an
acceptance command that proves it's done (an existing test, or a new one the agent writes first).
Bigger work is split into steps run one after another.

**2. `wb start` sets it up** and returns at once:
- It checks the task name, that the repo is a git repo, and that it has a committed `CLAUDE.md`
  (an agent without one starts blind; `--no-claude-md` is for throwaway repos).
- It picks the model: the router's choice for the task's size (`--size trivial|significant`),
  or `--model`. A model that can't run right now (server down, not pulled) is refused unless
  `--force`.
- It makes branch `delegate/<task>` in a fresh git worktree from `--base` (default `HEAD`). Your
  own checkout is never touched, and uncommitted changes in it stay out of the agent's copy (it
  says so).
- It writes round 1's prompt: the spec plus a short working protocol (stay in scope, leave changes
  uncommitted, run the acceptance command, the `ask_reviewer` budget, how to finish).
- It records the task (`task.json`), spawns the runner, and prints what it did and why it chose
  that model.

**3. The runner takes the GPU.** A consumer card fits one coding model, so runners take
`gpu.lock` in turn: a second task waits as `queued`. The runner then makes the task's model
resident: it unloads every other model Ollama holds (yours or another tool's), stops other
model servers that have a stop command (waiting up to ten minutes for a reply in progress to
finish), and loads or starts the one it needs.

**4. The agent works a round.** The runner runs
`opencode run --standalone --auto --format json` in the worktree, with `$PWD` set to it (OpenCode
finds the project from `$PWD`) and a per-task config layer (`OPENCODE_CONFIG`, below). Rounds after
the first continue the same OpenCode session, so the agent keeps what it has read. Every event
streams into `events.jsonl` (tool calls, messages, token counts, errors) and `status.json` keeps
the live summary: phase, round, the last action, tools used, tokens in and out.

**5. Guards stop a run that's going nowhere.** Small models that lose the thread repeat
themselves, so the runner stops the agent on:

| Guard | Default |
|---|---|
| the same tool call (same input) several times in a row | 4 |
| failing tool calls in a row | 6 |
| tool calls without changing a file | 40 |
| no output at all (not counted while it waits on Claude) | 15 min (`--silence-timeout`) |
| one round running too long | 40 min (`--round-timeout`) |

A model generating for minutes with no tool call is usually planning instead of doing. Past the
think budget (7 minutes, `--think-timeout`; 0 turns it off) the runner interrupts it once a round
with a nudge (act now: the smallest edit first, one file per call, run the tests soon) and tells
Claude.

**6. The tests gate the round.** After a clean round the runner runs the acceptance command
(`--test`) in the worktree, with a 15-minute limit and the output saved per round. If it fails and
auto-fix rounds remain (`--auto-fix`, default 2), the failure output goes straight back to the
agent as the next round's prompt ("fix the code, don't weaken the tests"), and Claude isn't woken.

**7. Claude is woken with a report.** When the round passes (or auto-fix runs out), the runner
writes a compact report and a notification and exits. The report has the round's time and local
tokens, how full the agent's context is, the test result (the failing tail if any), the tool
tally and errors, the agent's own summary, and the files changed with a diffstat. Stuck and failed
rounds get the same report, with why it stopped.

**8. Claude reviews.** The skill is strict about this: the summary is a claim, not evidence.
Claude reads the whole diff (`wb diff`), checks correctness, the spec, that the tests would catch a
bug, the repo's conventions, hygiene and safety, and runs things in the worktree when in doubt.
Then one of:

| Command | What happens |
|---|---|
| `wb accept <task> -m "…"` | commits the work on its branch, removes the worktree, optionally `--merge`s (`--no-ff`, only into a clean checkout) |
| `wb feedback <task> --message <file>` / `--text "…"` | a numbered list of exact changes becomes the next round's prompt; the runner starts again. The skill allows three review rounds before Claude fixes it itself or discards it |
| `wb next <task> --spec <file> -m "…"` | commits the approved step on the branch and gives the next step to the same agent, in the same worktree and session |
| `wb discard <task> --reason "…"` | stops it, removes the worktree and the branch (`--keep-branch` keeps it) |

Every accepted or discarded task leaves a row in `results.jsonl`: the model, the size, rounds,
auto-fixes, reviews, minutes, local tokens, the skills it was offered and loaded, the outcome. The
router learns from those rows.

### What the agent knows

An agent should start a task knowing what Claude would know in the same repo, and nothing that
would distract it. The per-task OpenCode config layer (`tasks/<name>/opencode.json`, written
before every round) gives it:

- **Instructions**, as absolute paths OpenCode loads into the system prompt:
  - the workbench handbook (`agent/HANDBOOK.md`, copied into the task directory so its path never
    points the agent outside its worktree): read before editing, imitate the codebase, smallest
    diff, never weaken a test, change approach after two failures, stay in the working directory,
    leave every git write and deploy to Claude, and an honest final summary;
  - your own `~/.claude/CLAUDE.md`, and the repo's `CLAUDE.md`, `.claude/CLAUDE.md`,
    `CLAUDE.local.md` and `.claude/rules/*.md` (the worktree's copy when committed, the repo's when
    local). OpenCode reads `AGENTS.md` itself but never `CLAUDE.md`; a `CLAUDE.md` with the same
    contents as `AGENTS.md` is skipped so nothing is loaded twice;
  - a short map of nested `CLAUDE.md`/`AGENTS.md` files in subdirectories, to read before working
    there, instead of loading them all.
- **Denied tools**, which also leave the prompt (it shrinks from about 9K to 3.5K tokens, which a
  local model reads on every request): `question` (nobody answers it headless), code mode and the
  `opencode_*` tools (they can move the session or remove worktrees), `subagent` (a second model
  would queue behind the GPU lock), web search and image generation.
- **One hard shell limit**: no `sudo`, `doas`, `pkexec` or `su`, checked in every part of a
  compound command. Everything else is the agent's; git writes and deploys are Claude's by the
  handbook, not by a rule.
- **Skills**: the repo's own `.claude/skills`, plus the library skills Claude handed this task.
  Your personal `~/.claude/skills` (including synced Claude app skills) and `/delegate` itself are
  denied: an agent once read a personal skill as a "newer task" and went off into your files.
- **`ask_reviewer`**, an MCP tool for one blocking question to Claude (below).

### Talking to a running agent

Claude is the driver, not a spectator, and the agent is a teammate it can reach at any time:

- **Questions.** `ask_reviewer` is a tiny MCP server (`workbench/ask_mcp.py`) OpenCode starts per
  task. A call writes the question to the task directory, sets the phase to `asking`, raises a
  `question` notification and blocks until `wb answer` writes the answer file, or 20 minutes pass
  (`--ask-timeout`). Each task has a budget (`--ask-budget`, default 2); past it, or on timeout, the
  agent is told to make the reasonable choice and say so. The silence guard doesn't count the wait.
- **Notes.** `wb steer <task> --text "…"` queues a note the agent gets when its current run ends,
  before the tests. `--now` interrupts it mid-step: the runner is woken by the note itself, stops
  the agent within seconds and continues the same session with the note first, marked as
  overriding anything earlier.
- **Check-ins.** A significant task wakes Claude every 10 minutes while it runs (`--checkin MIN`, 0
  for off; off by default for trivial tasks) with a digest of what changed since the last one:
  tools used and failed, tokens read and written, what it's doing now, its latest words, files
  changed, time so far and the ETA. Claude tells you how it's going in two or three lines, logs the
  same with `wb note` (a "captain's log" line in the transcript), and steers if it's drifting.
- **Taking over.** `wb takeover <task>` stops the agent and hands the worktree to Claude, to finish
  by hand and accept, or hand back with `wb feedback`. `wb stop <task>` just stops it.

### Context, on both sides

Local models work in a small context window, and so, in its own way, does Claude.

**The agent's.** Rounds continue one OpenCode session. Before each round the runner reads the
session's export: once it's 60% full, or OpenCode had to compact it (a small model's summary of a
coding session drops details), the next round starts a fresh session instead, handed exactly what
it needs: the task (the current step's spec), the steps already committed, the uncommitted diff
(or the list of changed files when the diff is over about 3K tokens), and the new instructions.
Reports show the fill (`agent context: 19.4K of 32K (60%)`) so Claude sees it coming.
`wb feedback --fresh` forces a fresh session (a confused agent), `--keep-context` keeps it for one
more round. A session stays bound to the directory it was made in, so context carries over only
within a task (rounds, `wb next`), never to a new one.

**Claude's.** `wb` holds the delegation state, so Claude's context doesn't have to. Whoever
compacts Claude's conversation (the engine, `/compact`, the mod), the mod tells the summary to keep
your goals and decisions, the tasks and what each needs next, and to drop finished tasks' diffs and
reports. At a task boundary (one just closed, none waiting for Claude) the mod compacts early once
Claude is halfway to the engine's own compaction (`WB_COMPACT_AT`, a percentage; 0 turns it off),
so a long session sheds detail at a clean point instead of mid-review. After a compaction or
`/clear`, a context block lists the open tasks from `wb status`.

### Waking Claude, exactly once

The runner and the `ask_reviewer` server append notifications (`review`, `stuck`, `failed`,
`question`, `checkin`) to `notify.jsonl`. Each task's `acked` file holds the time up to which
Claude has had its news.

- The mod runs `wb events`, which prints a line the moment anything in the state directory
  changes (inotify; worktrees and logs ignored). Each line makes the mod re-read the tasks. For
  each task's newest notification past its `acked` mark it shows a toast and starts a turn for
  Claude with the report and the next commands to run.
- It wakes only for news still true: a review notice for a task that was since discarded or sent
  back is acknowledged and dropped. While Claude is mid-turn the wake is held (a prompt submitted
  then would queue and arrive stale) and checked again when the turn ends. Nothing older than an
  hour wakes anyone.
- Acting on a task (`feedback`, `next`, `answer`, `note`, `accept`, ...) acknowledges it, but the
  mark stops just short of news still true, so a note written seconds after a failure can't
  swallow the failure notice Claude hasn't seen.
- The safety net: a task that has waited on Claude for a minute with no notice reaching it (a lost
  or swallowed one) wakes Claude anyway, once.
- Without the mod, `wb wait [task]` blocks until there's news and prints it; a named wait on a
  task whose news was already taken elsewhere says so rather than block forever.
- The plugin's `wake` setting (`on`/`off`) turns the wake-ups off and keeps the pane.

### Seeing what the agent does

- **The pane** (`/workbench`, or opened by itself when work starts) lists every task: phase, round,
  model, test result, age and its last few events. Under the list it follows the newest running
  task (else the newest one waiting for Claude) as a live transcript, scrolling with the newest
  lines unless you scroll up. The web chat's link is pinned at the bottom.
- **The status line** reads like `wb · add-scores running r2 · fix-typo asks · 1 awaiting review`.
- **`wb watch [task]`** is the same transcript in a terminal. It merges the OpenCode session export
  (the prompts Claude sent, the agent's reasoning, every tool call with its input and output, its
  messages, compactions) with the task's own events (tests, questions and answers, notes, steers,
  stops, fresh sessions, skills handed), drawn in Claude Code's own grammar: `⏺ Update(file)` with
  the diff, `⏺ Bash(…)` with its output. Prompts are labelled by what they are (spec, review
  feedback, automatic test-fix, interrupt, next step). Under it: what's happening right now
  (waiting for the GPU, loading the model, reading the prompt, thinking, writing, with live token
  rates from servers that report them), and a clock line: task and round time, model load time, an
  ETA from this model's past tasks of this size, context fill, tokens written. `-v` shows
  everything in full; `--once` prints and exits.
- **`wb status`**, **`wb log [-f]`**, **`wb report [--round N]`** and **`wb diff [--stat] [--all]
  [paths]`** are the compact views Claude itself uses. `wb status` marks a task whose runner died
  without saying so with a `?`.

### Models: found, loaded, chosen

**Discovery.** A model is a candidate when it's registered in OpenCode's config
(`~/.config/opencode/opencode.json`) under a provider whose `baseURL` is on this machine. A
provider on Ollama's port is driven as Ollama (models load on demand); anything else is a
one-model server. `~/.config/workbench/models.json` adds only what OpenCode can't say, per
provider or model id: `start`/`stop` commands for a one-model server, `skip`, `sizes`, and
`weights_gb` for a server that can't report its size.

**Status.** `wb models` and `wb doctor` say for each model whether it can run and why not: Ollama
not answering, the model not pulled, a server down with no start command.

**Routing.** The router makes an educated guess and corrects it with real results. A model's
quality on a task size is its acceptance rate with a uniform prior, (accepted + 1) / (tried + 2),
so one lucky task doesn't crown it. Among the models within 0.15 of the best, the guess decides:
the largest model (by weight bytes) for significant work, the smallest for trivial work, and at
equal size the one already on the GPU. `wb model sizes <id> trivial` limits a model to some task
sizes, whatever its record. `wb models` prints the ranking per size with each model's record; a new
model has none, so Claude gives it its first few tasks by hand. `wb models --set-default` makes the
router's pick OpenCode's own default.

**Managing the set.** `wb model add <provider/model> [--pull]` downloads a model into Ollama
(progress every 5%) and registers it in OpenCode with what Ollama says about it: tool calling,
reasoning, and the context the provider's other models are served at (else the trained one).
Ollama entries get an `options.max_tokens` cap, because Ollama ignores the
`max_completion_tokens` OpenCode sends, and an uncapped model in a degenerate generation runs
until its context is full. `wb model retire <id> [--delete]` unregisters it (and deletes its
weights); its results stay in the history.

**The weekly model check.** Models change fast, so once a week, in a quiet moment (you've used the
session and no task is open), the mod asks Claude to check. `wb scout` gives the facts, read fresh
each time: GPU and VRAM, RAM and swap, CPU, free disk where Ollama keeps models, Ollama's default
context, every registered model with its size, quantization, context and real-task record,
installed models that aren't registered, a watch list of models set aside and what would bring
each back, and skills the library flags. Claude researches what's newest from primary sources,
recommends keep / add / retire in a few lines, and changes nothing without your yes. A check
raised but never finished is asked again a day later; `/delegate-models` asks now.

### The skill library

The agents learn nothing between tasks; Claude does. When review teaches something that will come
up again (a library's quirk, a pitfall, a check to run), Claude writes a skill.

- **Where.** The library is its own git repository (`~/.local/share/workbench/skills`,
  `$WB_SKILLS`), so every skill has a history of why it was written or changed. It never lives in
  `~/.claude/skills`, which is yours. Knowledge of one project that only some of its tasks need
  goes in that project's own `.claude/skills` instead, committed with it.
- **Checked on the way in.** `wb skill add <name> --from <file> -m "<what review taught>"` refuses
  a skill without a matching name or a description (at most 300 characters: it's all an agent sees
  before deciding to load it) or over 8 KB, and warns about one over 120 lines, one that names a
  path outside the repo (agents go looking for those), or a description that doesn't say when to
  use it.
- **Handed per task.** `wb start --skill <name>` hands a skill to a task, `wb next --skill` sets a
  new step's, `wb skill hand` / `wb skill take` change them mid-task. Each handed skill reaches
  OpenCode as its own directory in the config layer's `skills` list, so the agent sees exactly
  those and the rest of the library stays out of its prompt. The agent is told what it holds at the
  start of a session, and after that only what was handed or taken back.
- **Kept honest.** Results record which skills each task was offered and which the agent actually
  loaded. `wb skill list` shows each one's record, and flags a skill offered three times and never
  loaded (its description is wrong), one loaded in tasks that mostly failed (it may say the wrong
  thing), and one not offered in 60 days. `wb skill retire` moves one to `retired/`;
  `wb skill restore` brings it back.

### Git, commits and projects

- **The agent never commits.** Everything it does stays uncommitted in its worktree, and
  `wb diff` shows it (new files included) against the task's base, or after `wb next` against
  the last committed step (`--all` for the whole branch).
- **Commits are yours.** `wb accept` commits under your git identity with no AI co-author line,
  only provenance:
  ```
  feat: high-score screen

  Delegated to <model> via OpenCode: 3 round(s), 1 automatic test-fix, 1 review round(s).

  Workbench-Task: high-scores
  OpenCode-Session: ses_…
  ```
  It merges only with `--merge`; otherwise it prints the merge command.
- **`wb init <dir>`** sets a project up the way Claude keeps every project: a git repo on
  `master` with an initial commit, and `AGENTS.md` as a symlink to `CLAUDE.md`, so OpenCode's
  agents and Claude read the same file. It never adds a remote.
- **CLAUDE.md stays true.** It goes into every agent prompt, so `wb accept` prints how many code
  commits it's behind, and the skill has Claude update it after each accepted task that changes
  what it describes.

### The web chat

`wb chat serve` (kept up by `systemd/wb-chat.service`) serves a small page on port 1337 for
reaching the Claude session that runs `/delegate` while you're away.

- **The path of a message.** The page posts it; the server appends it to `chat/messages.jsonl`. The
  listening session's mod submits it to Claude as a prompt starting `[web chat]` (into the running
  turn if Claude is mid-turn, else as the next prompt) and writes a receipt the page shows at once
  (Claude has it now, or will read it after its current step). Claude answers with `wb chat reply`, which the page shows. The page says
  whether a session is listening and whether Claude is working.
- **One listener.** A session listens once `/delegate` (or a delegating `wb` command) has run in
  it, until a quit command. With several sessions the first to claim `chat/listener.json` listens;
  it is known by its `wb events` process, so it stops counting the moment that session ends.
- **Claude knows where you are.** A web message carries the same authority as one typed in the
  terminal, with a note: reply there, briefly; the web thread is all you see; a yes there answers
  only a question asked there; nothing hard to undo on an ambiguous OK. For six hours after a web
  message (until you type in the terminal again) Claude also tells you there when something you
  asked for finishes or needs you.
- **Locked down.** What a message can do is what typing in the terminal can do, so: a secret token
  (`~/.config/workbench/chat-token`, mode 600, made on first use) that `wb chat url` puts in the
  link's fragment, which never leaves the browser; the API wants it as a bearer header; the server
  answers only to the host names it serves (localhost and this machine's Tailscale names), which
  stops other web pages and DNS rebinding; it binds to localhost and the Tailscale address, never
  `0.0.0.0`; strict security headers, size limits and a cap on open streams.
- **Robust on a phone.** The server streams the whole thread on every change, so every reconnect
  is a full resync and nothing arrives half-received. A message goes into a local outbox with its
  own id and is retried until the server has it; the server ignores an id it already holds, so
  nothing is sent twice. A heartbeat watchdog, jittered backoff and a resync when the page or the
  network comes back keep it connected. It installs as a home-screen app.

### Stopping everything

Only your two slash commands stop the delegation, never words in a message:

- **`/delegate-quit`** runs `wb stop --all`: every running agent stops, every task keeps its
  worktree for later, and Claude is told not to finish, restart or clean up anything unless asked.
- **`/delegate-quit-and-hand-back-claude`** runs `wb takeover --all` and starts a turn for Claude
  to leave delegate mode, finish each task itself to its own standard, accept it, and carry on with
  the rest of your request without delegating.

Both stop the web chat listening in that session.

### Every `wb` command

| Command | What it does |
|---|---|
| `wb start <task> --spec <file> [--repo] [--size] [--test] [--skill] [--model]` | hand a task to a local agent (more knobs: `--auto-fix`, `--checkin`, `--ask-budget`, `--ask-timeout`, `--round-timeout`, `--silence-timeout`, `--think-timeout`, `--base`, `--force`, `--no-claude-md`) |
| `wb feedback <task> --message <file> \| --text "…" [--fresh \| --keep-context]` | another round with review feedback |
| `wb next <task> --spec <file> -m "…" [--test] [--skill] [--fresh]` | commit the approved step, send the next one to the same agent |
| `wb answer <task> --text "…" \| --file <f>` | answer the agent's question |
| `wb steer <task> --text "…" [--now]` | a note to a running agent; `--now` interrupts |
| `wb note <task> --text "…"` | Claude's captain's-log line in the transcript |
| `wb takeover [<task> \| --all]` | stop the agent and finish by hand |
| `wb stop [<task> \| --all]` | stop it; the work is kept |
| `wb accept <task> -m "…" [--merge] [--force]` | commit the work and close the task |
| `wb discard <task> [--reason] [--keep-branch]` | drop the work and close the task |
| `wb wait [<task>] [--timeout]` | block until a task needs Claude |
| `wb status [<task>] [--all]` | open tasks at a glance |
| `wb watch [<task>] [-v] [--once]` | the live transcript |
| `wb log <task> [-n N] [-f] [-v]` | the agent's events |
| `wb report <task> [--round N]` | a round's report |
| `wb diff <task> [--stat] [--all] [paths]` | the work under review |
| `wb models [--set-default]` | the local models and the router's ranking per size |
| `wb model add\|retire\|sizes <provider/model> …` | add (`--pull`), retire (`--delete`), or limit a model's task sizes |
| `wb scout [--done --notes] [--due] [--watch TEXT] [--unwatch N]` | the weekly model check's facts and its watch list |
| `wb skill list\|show\|add\|retire\|restore\|hand\|take …` | the skill library |
| `wb stats` | real results by model and size |
| `wb chat [serve\|url\|reply\|log]` | the web chat |
| `wb init <dir>` | set a project up: git on master, AGENTS.md → CLAUDE.md |
| `wb events` | a line whenever workbench's state changes (what the mod listens to) |
| `wb doctor` | check opencode, its config, git, each model, the state directory |

### Where everything lives

```
~/.local/state/workbench/        ($WB_HOME) every task, notice and result
  notify.jsonl                   notifications that need Claude, all tasks
  results.jsonl                  one row per closed task: what the router and skill records learn from
  gpu.lock                       held by the runner whose model is generating
  scout.json  watch.json         the weekly model check: when it was done, the watch list
  chat/                          messages.jsonl  receipts.jsonl  listener.json
  tasks/<name>/
    task.json  status.json       what the task is; what it's doing now
    events.jsonl  runner.log     the agent's normalized events; the runner's own output
    opencode.json                the config layer: instructions, permissions, skills, ask_reviewer
    HANDBOOK.md  context.md      the agent's copy of the handbook; the nested-instructions map
    steer.jsonl  acked           Claude's notes to the agent; how far Claude has had the news
    rounds/<n>/                  prompt.md  summary.md  tests.txt
    ask/<id>.json  .answer       questions and answers
    worktree/                    the agent's git worktree, branch delegate/<name>
~/.local/share/workbench/skills/ ($WB_SKILLS) the skill library, its own git repo
~/.config/workbench/             models.json ($WB_MODELS), chat-token ($WB_CHAT_TOKEN)
~/.config/opencode/opencode.json the models (OpenCode's own config)
```

## License

MIT.
