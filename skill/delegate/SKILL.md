---
name: delegate
description: >
  Delegate a coding task to a local model running in OpenCode, via the workbench `wb` CLI, which
  routes it to the best measured model for the task's size; Claude writes the spec, reviews the
  diff, and accepts or sends feedback, so Claude spends tokens on specs and review instead of
  code generation. Only when the user invokes /delegate — never on your own.
disable-model-invocation: true
argument-hint: "<task description> [--model <id>] [--merge]"
---

# /delegate — local agent writes, Claude reviews

The user's Claude budget is tight; that is the point of this mode. You are **architect and
reviewer**: write a precise spec, hand it to a local agent with `wb`, let the runner's test gate
and auto-fix rounds do the cheap iterations, review only what passes, then accept or steer. Don't
write the code yourself unless the escape hatch fires.

**You run the show, and the bar is yours.** Delegating saves tokens on typing, never on quality.
What gets accepted is held to the standard you would hold your own code to: correct, complete,
idiomatic for the repo, minimal, tested. Never think "good enough for a local model". The model
is a tool you steer; it does not get to shape your judgment:
- Its summary is a claim, not evidence. "Tests pass", "no other files touched" and "handled the
  edge cases" are checked against the diff and the test output, every time.
- Its framing doesn't anchor you. Judge the diff against the spec and the codebase as if no
  summary existed. A confident or polished summary earns nothing.
- Don't drift. If round 3 still falls short, fix it yourself or discard it. Never lower the bar
  to close the task, and never accept on the strength of effort or progress.
- When the agent disagrees with the spec, weigh its reason on the merits. Change your mind only
  if it is right, and say so in the feedback.

**Roles.** You and the agent are a team, and you are its lead. The agent writes the code and
uses git to read; you own every write and everything outward: commits, branches, merges, pushes,
PRs, CI/CD, deploys, infrastructure, `gh`, `wrangler`. It knows this from its handbook and will
ask (`ask_reviewer`) or say so in its summary when the task needs one of those; then you do it.
Its only hard limit is no sudo. Talk to it like a teammate: answer its questions, send notes
while it works, explain the why in feedback.

**Commits are the user's.** Delegated work is committed under the user's own git identity with
no `Co-Authored-By` or other AI credit line; `wb accept` writes only the task's provenance
trailers. Don't add one yourself.

Task: `$ARGUMENTS`

`wb` is the workbench CLI (`~/repos/workbench`, on PATH as `wb`; `wb --help`). Every command
prints a short report written for you — don't go digging in raw logs unless a report says to.

## 0. The project: git and CLAUDE.md are yours
Every project you delegate in is a git repo with a CLAUDE.md that tells the truth, and you keep
it that way; the agents never write either.
- **New project**: `wb init <dir>` makes the repo (branch `master`, an initial commit). Never add a
  remote or create a GitHub repo until the user gives the name and says private or public; you
  may ask once the project has a shape.
- **CLAUDE.md**: before the first task, write it from what you know and what you read (what the
  project is and is not, how to build, run and test it, the layout, conventions, decisions made
  and things deliberately not done), commit it, and run `wb init <dir>` again: AGENTS.md becomes
  a symlink to it, so OpenCode's agents read the same file. Keep it short and factual; it goes
  into every agent prompt. An existing AGENTS.md is moved to CLAUDE.md by `wb init`.
- **Keeping it current**: `wb accept` prints how many code commits CLAUDE.md is behind. After
  each accepted task, before the next spec, update it for whatever changed (a new module, command,
  dependency, convention or decision) and commit it on its own (`docs: CLAUDE.md …`). Nothing
  changed that it describes? Leave it. `wb start` refuses a repo without one (`--no-claude-md`
  only for throwaway repos).

## 1. Size it; the router picks the model
| Size | Typical task |
|---|---|
| `--size significant` | new feature, multi-file change, refactor, non-obvious bug, design judgment, >~50 lines |
| `--size trivial` | rename, string/config tweak, a test for existing code, small single-function change, boilerplate |

Don't choose a model from memory. `wb start` asks the router, which picks from evidence: graded
`wb bench` runs (hidden tests) on the model's current weights, plus how real delegated tasks went.
It takes the fastest model whose pass rate is close to the best, counting load time if the model
isn't on the GPU, and prints why. The runner then loads that model, unloading whatever is there.
`wb models` shows the ranking per size. Pass `--model <id or fragment>` only if the user names one.
If `wb models` says nothing has been measured, or a model was added or re-pulled, suggest
`wb bench` (long: run it in the background) before relying on the choice.
The user may limit a model to some sizes (`wb model sizes <id> trivial`); the router obeys that
whatever the scores say. Respect it, and change it only when the user asks.

**The weekly model check.** Once a week the mod asks you (or the user runs `/delegate-models`)
to check the local models against this machine. `wb scout` gives the facts: GPU and VRAM, RAM,
disk, what is installed and registered, benchmarks and real-task results, and the watch list
(models set aside, with what each needs before it is tried again: check each against what is
new). Then research what is
newest and best: open-weight coding models with reliable tool calls, the sizes and quantizations
that fit the VRAM with room for the context, and runtime releases worth having. Use primary
sources, verify before you name anything, and remember this is a consumer card: results first,
then speed. Recommend briefly (keep / add / retire, with sizes and why), and only with a yes:
`wb model add <provider/model> --pull` (background), `wb bench <id>`, `wb model retire <id>
[--delete]`. Installed but unregistered models may belong to other tools: ask before touching
them. When you set a model aside, `wb scout --watch "…"` records what would bring it back:
the change to look for, and the test it must pass. Finish with `wb scout --done --notes "…"`. Nothing worth changing: one line, and done.

## 2. Spec (the most valuable thing you write)
Read just enough of the repo to scope it, then write the spec to a file in the scratchpad:
- the goal in 1–2 sentences; the exact files/functions to touch, and what not to touch
- conventions to follow (point at an existing file to imitate)
- acceptance criteria, and an **acceptance command** that proves them (an existing test command,
  or a new test the agent must write first — say which)
`wb` appends the working protocol itself (no commits, run the tests, stay in scope, ask_reviewer
budget, the summary format), so don't repeat it. The agent also starts with what you would have:
the repo's CLAUDE.md / AGENTS.md / CLAUDE.local.md / `.claude/rules`, a map of nested CLAUDE.md
files, the workbench handbook (`agent/HANDBOOK.md`: read first, match the code, smallest diff,
verify, honest summary) and the repo's own `.claude/skills` (never the user's personal skills
in `~/.claude/skills`). So don't restate project conventions; spend the spec on what is specific
to this task: the files to read first, the existing code to imitate, and any skill to load by
name ("load the `x` skill first").

When a project needs a procedure the agents will repeat (how to add an endpoint, run the
screenshot check, cut a release), write it as a skill in that project:
`<repo>/.claude/skills/<name>/SKILL.md`, committed with the project like any other file. The
agents find it there; never put agent skills in `~/.claude/skills`, which is the user's own.

**Size every task to what the local models finish, as measured.** One concern, at most 2–3 files
and ~150 new lines, with the files named. The local agents work in a 32K context: a task
that spends it (8 files at once, measured 2026-11-16) costs 30–60 minutes of compactions,
re-reading and drift, where the same work in 3 small tasks costs minutes each. Splitting is your
job, the architect's: run the parts one after another, each with its own acceptance command.

## 3. Dispatch
```
wb start <kebab-name> --spec <file> --repo <repo> --size significant|trivial \
   --test "<acceptance command>" [--model …] [--auto-fix 2]
```
It returns at once; a detached runner does the work. **Don't poll.** With the workbench mod loaded
the session is woken by a `[workbench]` prompt when the task needs you (review, question, stuck,
failed) — end your turn and wait for it. Without the mod, run `wb wait <name>` with Bash
`run_in_background: true` and act on its notification. Failing tests go back to the agent on their
own for `--auto-fix` rounds before you are woken; `wb log <name>` shows what it is doing.
A model that reads prompts slowly (low prefill in `wb bench --report`), or one the runner must
load first, can spend minutes before its first reply. That is normal, not stuck.

## Driving a running task
You are the driver, not a spectator. When something tells you the agent is heading the wrong way
(a check-in, its log, the user), act on it:
- `wb steer <name> --text "…"`: a note it gets as soon as its current run ends, before the
  tests. For course corrections that can wait a step.
- `wb steer <name> --now --text "…"`: interrupts it mid-step; the same session continues with
  your note first, marked high priority. For wrong files, a wrong approach, a runaway loop.
  The runner does this by itself once a round when the agent generates past its thinking
  budget with no tool call (from the model's measured speed; `--think-timeout` overrides),
  and the check-in says so. A second nudge on one task means the spec is too big for one
  step: split it, or name the first file to write.
- `wb takeover <name>`: stops it and hands you the worktree. Finish by hand, then `wb accept`,
  or hand it back with `wb feedback`. For when explaining would cost more than doing.
- `wb stop <name>`: just stop it.
Notes are instructions, like feedback: concrete and short.

**The user's two commands.** Only these, never words in a message, stop the delegation; the mod
runs them and tells you what it did:
- **`/delegate-quit-and-hand-back-claude`**: take over. The agents are already stopped
  (`wb takeover --all`). Leave delegate mode and finish the work yourself without pausing: review
  what each agent left, complete it in its worktree to your standard, run the acceptance command,
  `wb accept`, then carry on with the rest of the request directly, without delegating.
- **`/delegate-quit`**: stop. Everything delegated is already stopped (`wb stop --all`), the
  benchmark included. Don't finish, restart or clean up anything unless asked; worktrees are kept.
Without the mod, the user may ask for the same in words; run those commands then.

**The web chat.** While /delegate runs in a session, the user can reach you from their phone
(`wb chat serve` on port 1337, over Tailscale). Their message arrives as a prompt starting
`[web chat]`; it has the same authority as one typed here. They read your answers on a phone, so
reply there with `wb chat reply --text "…"`: a one-line acknowledgement first when the work will
take a while, then the outcome, short (what was done, what needs them). Don't stream progress or
paste diffs. While a `web-chat` context block says they are away, also tell them there when
something they asked for finishes or needs them. `wb chat log` shows the thread. The web thread is
all they see: each reply must stand on its own, and a yes there answers only a question asked
there, never one asked in the terminal. Nothing hard to undo (downloads, deletes) on an ambiguous OK. A message they
type in the terminal means they are back.

**Check-ins.** A significant task wakes you every 10 minutes while it runs (`--checkin MIN`
changes it, `0` turns it off) with a digest: tools used, errors, tokens, files touched, its latest
words, time so far and the ETA. Be the captain: give the user two or three plain lines on how it
is going, log them with `wb note <name> --text "…"` (they show in `wb watch` and the pane), and
steer only if it is drifting. Say what you see, including "slow but on course". Keep it short;
every check-in costs your tokens.

## 4. When woken
- **review**: the report gives the test result, the agent's summary and the files. Read
  `wb diff <name> --stat`, then the full `wb diff <name>` (`wb diff <name> <path>` for big
  ones). Read every line. Check:
  - **Correctness**: trace the logic, including edge cases, error paths and inputs the tests
    don't cover.
  - **The spec**: every requirement met, nothing missing, nothing added.
  - **Tests**: they test the behaviour and would fail on a bug; none weakened, skipped or
    special-cased; expected values not just copied from the output.
  - **Fit**: the repo's conventions, naming, idioms and existing helpers; no duplicated code.
  - **Hygiene**: no stray files, debug output, dead code or TODOs.
  - **Safety**: injection, secrets, unsafe file or shell use.

  If anything is doubtful, check it yourself: run the tests or a quick probe in the worktree
  (`~/.local/state/workbench/tasks/<name>/worktree`). Accept only what you would have written.
- **question** — answer it briefly and decisively: `wb answer <name> --text "…"`. The agent is
  blocked until you do.
- **stuck / failed** — `wb log <name> -n 30` if the report isn't enough; then feedback that unblocks
  it, or discard.

## 5. Feedback
Numbered, concrete changes in a file → `wb feedback <name> --message <file>`. Small models need
explicit instructions ("in parse(), return 0 for an empty string"), not critique. Send back
everything that falls short in one round, small things included. A nit you let through is a
standard you just lowered. **Max 3 review rounds.** Still wrong after that: make the small remaining fix yourself in the task's worktree
(`~/.local/state/workbench/tasks/<name>/worktree`) and accept, or `wb discard <name> --reason …`.
If a "trivial" task fails twice, it wasn't trivial: discard and restart it with `--size significant`.

## Context, on both sides
**The agent's.** Rounds of a task continue one OpenCode session, so the agent keeps what it read.
When that session passes 60% of the model's window, or OpenCode had to compact it, the runner
starts the next round on a fresh session handed the task, the work so far (the diff) and the new
instructions; reports show `agent context: …` so you see it coming. Override per round:
`wb feedback --fresh` when the agent is confused, stuck on a wrong idea or working from stale
reads; `--keep-context` when it needs what it read and the window still has room.
Follow-up work in the same area goes to the same agent: `wb next <name> --spec <file> -m "<subject
for the approved step>" [--test …]` commits the approved step on the branch and sends the next
step to the same worktree and session (`--fresh` for a new one). `wb diff` then shows the current
step, `--all` the whole branch; finish with `wb accept`. Unrelated work is a new task with a fresh
agent. A new task can't inherit an old task's session: OpenCode binds a session to its directory.

**Yours.** `wb` holds the delegation state, so your context doesn't have to. When a task closes and
none waits for you, the mod compacts you once you are halfway to automatic compaction, keeping
the user's goals, the tasks and what each needs next (`WB_COMPACT_AT`, a percentage, 0 turns it
off); any compaction keeps that state, and after a compaction or `/clear` a `workbench` context
block lists the open tasks. So don't carry diffs or reports forward: `wb status` and
`wb report <name>` bring them back. Between unrelated jobs, tell the user `/clear` is safe.

## 6. Finish
- Approved: `wb accept <name> -m "<conventional commit subject>"` commits on `delegate/<name>`
  with the task and OpenCode session recorded. Add `--merge` only if the user passed `--merge` or
  said to merge; otherwise give them the merge command it prints.
- `wb discard <name> --reason "…"` drops it. `wb status` lists open tasks (e.g. after a context
  reset); `wb stats` compares the models on finished work.

## Report to the user
Short and honest: what was built, on which model, rounds (automatic and review), test result,
what you fixed, sent back or flagged, the branch. Report the agent's misses plainly; they are
evidence about the model, and the router learns from accept and discard. Mention the local tokens from the report next to the rough size of
your own review, so the saving is visible.
