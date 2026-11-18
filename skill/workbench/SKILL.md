---
name: workbench
description: >
  Workbench mode for the rest of the session: Claude hands the coding of each request to local
  models running in OpenCode, via the workbench `wb` CLI, which routes each task to the best
  local model for its size; Claude writes the spec, reviews the diff, and accepts or sends
  feedback, so Claude spends tokens on specs and review instead of code generation. Only when
  the user invokes /workbench — never on your own.
disable-model-invocation: true
argument-hint: "[first task] [--model <id>] [--merge]"
---

# /workbench — local agent writes, Claude reviews

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

**Roles.** You and the agent are a team, and you are its lead. The agent writes the code; you
own the architecture, CLAUDE.md, AGENTS.md and `.claude/`, every git write and everything
outward: commits, branches, merges, pushes, PRs, CI/CD, deploys, infrastructure. It knows this
from its handbook and will ask (`ask_reviewer`) or say so in its summary when the task needs one
of those; then you do it. Its hard limits: no sudo, git read-only (a `git` of workbench's own on
its PATH; scratch repos under the temp directory, which tests make, stay writable), no `gh`, and
no edits to CLAUDE.md, AGENTS.md or `.claude/`. The shell rules refuse the direct forms; behind
them, where `wb doctor` reports Landlock, the kernel refuses root and writes to the repo's git,
and no credential is in the agent's reach, so `gh` or a push gets no login. Without Landlock
(macOS, old kernels) only the rules hold: read the report's flagged files closely and check
`git -C <repo> log` after a task. The rules stop mistakes, not a determined agent: the
round report flags any of your files it changed anyway (through the shell). Talk to it like a teammate: answer its questions, send notes while
it works, explain the why in feedback.

**Commits are the user's.** Delegated work is committed under the user's own git identity with
no `Co-Authored-By` or other AI credit line; `wb accept` writes only the task's provenance
trailers. Don't add one yourself.

## Workbench mode
`/workbench` turns workbench mode on for the rest of this session; the Workbench pane opening is
the user's sign that it is on. The user then works with you as usual, in plain words: each
request is handled the way this skill says, until they run `/workbench-quit` or
`/workbench-quit-and-hand-back-claude`. Running `/workbench` again while it is on only brings
the pane back (the mod answers it; with a task, the task is yours to take as usual).
- **First task:** `$ARGUMENTS`. Empty? Reply in one line that workbench mode is on and wait for
  their first request. Don't scan the repo or check models until there is work.
- **What goes to the agents:** the coding: features, fixes, refactors, tests, boilerplate.
  Answer questions, read code, plan, and do what is yours (git, CLAUDE.md, `.claude/`, anything
  outward) yourself, as always. An edit that costs less to make than to spec (a typo, one line)
  you may just make.
- **After a compaction or `/clear`**, a `workbench-mode` context block says the mode is still on
  and where this file is: read it again if its procedure is no longer in your context.
- **Plans are handoff plans**, in plan mode or not: see "Planning work you will hand off" below.

`wb` is the workbench CLI (on PATH as `wb`; `wb --help`). Its code, ARCHITECTURE.md included, is
two directories above `readlink -f "$(command -v wb)"`. Every command
prints a short report written for you — don't go digging in raw logs unless a report says to.

## 0. The project: git and CLAUDE.md are yours
Every project you delegate in is a git repo with a CLAUDE.md that tells the truth, and you keep
it that way; the agents never write either.
- **New project**: `wb init <dir>` makes the repo (git's default branch, an initial commit); `<dir>`
  is the project's own directory, never ~. When it refuses likely secrets or installed directories,
  write a `.gitignore` for them (ask the user about a secret it names) and run it again. Never add a
  remote or create a GitHub repo until the user gives the name and says private or public; you
  may ask once the project has a shape.
- **CLAUDE.md**: before the first task, write it from what you know and what you read (what the
  project is and is not, how to build, run and test it, the layout, conventions, decisions made
  and things deliberately not done), commit it, and run `wb init <dir>` again: AGENTS.md becomes
  a symlink to it, so OpenCode's agents read the same file. Keep it short and factual; it goes
  into every agent prompt. An existing AGENTS.md is moved to CLAUDE.md by `wb init`.
- **Keeping it current**: when a task changes something CLAUDE.md describes (a new module,
  command, dependency, convention or decision), edit CLAUDE.md in the task's worktree at review,
  before `wb accept`, so it lands in the same commit as the code (one edit, no commit of its own).
  `wb accept` prints how many code commits CLAUDE.md is behind. Nothing changed that it describes?
  Leave it. It says what the project is, never what comes next: keep
  the plan and the next step in your own notes. An agent that loses its task takes up whatever
  CLAUDE.md says is next (seen: a compaction summary that recounted CLAUDE.md, and the agent
  built its "Next:" line instead of its spec). `wb start` refuses a repo without one (CLAUDE.md or
  `.claude/CLAUDE.md`; `--no-claude-md` only for throwaway repos). It also refuses an unknown
  `--base` and a repo with no git identity for the commits: fix those, don't work around them.
- **Submodules** are not checked out in the agent's worktree (`wb start` says so): a step whose
  build or tests need one is yours, or its acceptance command must not need it.

## Planning work you will hand off
In workbench mode every plan you write is for agents weaker than you, who see only the spec you
send, so the plan the user approves is already the list of specs, each sized and checked by §1
and §2. That holds in plan mode, which is read-only: read the code, `wb models` and `wb status`
if you can, and start nothing until the plan is approved; then write the first spec and
`wb start` at once. Per step:
- **One concern**, no bigger than the slice `wb models` gives the model that will take it, and
  its size (trivial or significant). A step you can't size is two steps. Size it by the bytes the
  agent will read (`wc -c`), not the files: a two-line edit in a 2,000-line file still gets the
  file read (seen: 39% of a 32K window gone before the first edit, then a compaction).
- **The files**: those to read first, those to create or change, and the existing code to imitate.
- **Exact interfaces** for anything later steps or other code rely on: names, signatures,
  constants, file formats. Settle them in the plan; agents rename what isn't pinned.
- **Its acceptance command**, and the worked examples it tests, each number checked against
  every rule. A step whose done-ness you can't state as a command isn't ready to hand off.
  The runner runs it once on the base, in the agent's lane, before round 1; what it saw comes in
  the first report. A test that writes outside the lane or races under load fails there whatever
  the agent does, and the agent spends its rounds on failures it didn't cause (seen: `whoami` and
  /tmp probes): `wb discard` and start again with a better one. A command that already passes on
  the base is normal when the agent writes the step's tests (the report says so; don't restart):
  then those tests are the check, so the spec names what they must cover, and at review you see
  they fail without the change. Otherwise the command should fail only for what the step changes,
  and check every listed change (each old string gone, each new one there), not just the theme: a
  small model skips one item of a list.
- **Order**: logic, then its tests, then UI; a step that needs another's interface comes after it,
  through `wb next` on the same agent when it is the same area.
- **What stays yours**, marked apart: git, CLAUDE.md, choices and setup only the user can make
  (downloads, licences, public repos), and the checks you run at review (a screenshot to look at,
  tests to break on purpose, a grader).
Keep code out of the plan except those interfaces: the agents write it, you hold the bar. The plan
lives in your notes or the scratchpad, never in CLAUDE.md: an agent that loses its task takes up
whatever CLAUDE.md says is next.

## 1. Size it; the router picks the model
| Size | Typical task |
|---|---|
| `--size significant` | new feature, multi-file change, refactor, non-obvious bug, design judgment, >~50 lines |
| `--size trivial` | rename, string/config tweak, a test for existing code, small single-function change, boilerplate |

`wb start` asks the router, which makes an educated guess and corrects it with real results:
the largest model allowed significant work, the smallest allowed trivial work, unless a model's
record of accepted tasks at that size is clearly worse. It prints why; the runner then loads that
model, unloading whatever is there. `wb models` shows the ranking per size. Pass
`--model <id or fragment>` only if the user names one, or when the record argues against the guess.
Which sizes a model takes is set with `wb model sizes <id> trivial|significant`: set it when you
add a model you judge fit only for small work, and otherwise change it only when the user asks.
A newly added model has no record, so the router won't pick it over one with a good record:
replay the bench on it first (`wb bench run --model <id>`, §6), then give it its first few
real tasks with `--model` (ones you'd review closely anyway).
Model servers (none runnable, which server, a model on another machine), updating workbench, and
**the weekly model check** (the mod asks once a week; `/workbench-models`): read
`reference/models.md` beside this file first.

## 2. Spec (the most valuable thing you write)
Read just enough of the repo to scope it, then write the spec to a file in the scratchpad:
- the goal in 1–2 sentences; the exact files/functions to touch, and what not to touch
- conventions to follow (point at an existing file to imitate)
- acceptance criteria, and an **acceptance command** that proves them (an existing test command,
  or a new test the agent must write first — say which)

**Before `wb start`: check the spec.** Spec gaps cause review rounds. You check the numbers you
compute, but they hide in the sentences you didn't execute. Go through these, each from a real
miss:
1. **Every rule has a test.** For each "must", "rejects", "at most" or "only", name the test
   that fails if it's broken. A rule without one is a wish (seen: "one trailing newline"
   was stated, no test rejected a blank row, and the parser overflowed on one).
2. **Every edge input is named:** empty, one, the maximum, one past it, and for text, separators
   that lead, double or trail.
3. **Rules checked against each other and against the algorithm.** A size limit and a method
   can conflict: a heap that skips stale entries holds one entry per push, not per item. Give
   each fixed size its bound and why it holds.
4. **Every number run, not reasoned:** compute each expected value with a script, through the
   limits the spec sets (a clamp, a cap, integer division, float rounding). Seen: 24,900 of
   32,768 printed as 75%.
5. **Every fact about the repo checked in the repo:** each name, field, file and build step you
   mention (`grep` it). Memory is not a source. Seen: a field called `in.x` that was `move_x`,
   and "add it to the Makefile" for a Makefile that finds files by itself.
6. **Only the task.** Nothing about later steps, in the spec or in CLAUDE.md. An agent takes
   any "next" as its job.
7. **The reading fits.** Name only the files the agent must read, headers before sources, and
   put a short fact it needs (a signature, a constant) in the spec instead of a file to read.
   The repo's CLAUDE.md is already in its instructions: asking it to read that is reading twice.
   Spec plus named files (`wc -c`, ~4 characters a token) well under half the window, since the
   agent reads more than you name. Seen: a step whose reading filled 60% of a 32K window
   before the first edit, twenty sessions in a row.
8. **The old tests still hold.** A change of behaviour breaks tests that assumed the old one:
   run the whole suite against your simulated change (or read each test the change touches)
   and say in the spec which tests change and how. Seen: a smarter pursuit reached its target
   sooner, so a test measuring distance from the starting point failed, and a new collision rule
   erased an input a replay test relied on: two of the agent's questions, both the spec's.
9. **Every insertion point is a line to quote.** In a file the agent reads only in part, say
   where new code goes by the exact line it follows, unique in the file, not "after function
   X": it can't see where X ends. Seen: new tests put after a test's first `wait` call, its
   last eleven lines deleted, and the summary claiming otherwise.
After review, when a round was the spec's fault, say so: `--spec-gap "what it missed"` on
`wb feedback`, `next`, `accept` or `discard`. `wb start` shows your record from those,
and a gap that recurs belongs in this list.

`wb` appends the working protocol itself (no commits, run the tests, stay in scope, ask_reviewer
budget, the summary format), so don't repeat it. The agent also starts with what you would have:
the repo's CLAUDE.md / AGENTS.md / CLAUDE.local.md / `.claude/rules`, a map of nested CLAUDE.md
files, the workbench handbook (`agent/HANDBOOK.md`: read first, match the code, smallest diff,
verify, honest summary) and the repo's own `.claude/skills` (never the user's personal skills
in `~/.claude/skills`). So don't restate project conventions; spend the spec on what is specific
to this task: the files to read first, the existing code to imitate, and the skills to hand it
(below).

**The web is yours**: research for a spec, a question or the model check uses this session's
browsing tools; a 403, a CAPTCHA or an empty page is not checked: say so, never fill it from
memory. Put what the agent needs in the spec, not a URL (its web tools are denied). How, and a
browser check of the agent's work as a test: `reference/driving.md`.

**Your skills reach the agent as decisions.** Before writing a spec, check which skills and tools
in your own session fit the task, whatever the field: a design plugin for a UI, a writing guide
for docs, a security checklist for auth code, the user's own. Apply them yourself and put the
result in the spec: the decisions and the concrete rules this task must meet (the palette as
tokens, the terms and tone, the checks the code must pass), never the skill itself or "follow X":
the agent can't load your skills, and they are written for you. Review the diff against them too.
When the same correction from one of your skills comes up in a second task, it is a lesson: give
the agents a short library skill of just its rules for doing the work (`wb skill add`, or `wb
skill import` from the original when its license allows), handed out on the tasks it fits.

**Size every task to what the model that takes it finishes, as measured.** One concern, with the
files named, and no bigger than its slice: `wb models` (and `wb start`) give each model's, e.g.
`~3 files, ~150 new lines per task`. The slice comes from the context window the model is served
(on 32K, 2–3 files and ~150 lines, measured 2026-11-16; a task that spends it, 8 files at once,
cost 30–60 minutes of compactions, re-reading and drift) and is corrected by every step that
closes: the biggest that went well raise it, the smallest that went badly (compacted, stuck, or
two review rounds the agent caused) lower it; a handover to a fresh session doesn't count against
a step. So a strong
machine's big window earns big tasks; don't hold it to the 32K numbers. When the last few steps
went well, try the next one a little bigger than the slice; that is how it learns to grow.
Splitting is your job, the architect's: run the parts one after another, each with its own acceptance command.
A good split is logic, then its tests, then the UI: `wb next` gives each part to the same agent,
which already knows the code. (Measured: a step that never landed in an hour as one task landed
in ~30 minutes as three, every hidden check passing.)
- Names, signatures and constants that other code relies on: write them in the spec and say
  "exactly"; agents otherwise rename them to fit their own taste.
- The agent cannot see what it draws. For anything visual, look at the screenshot yourself;
  after one miss on a visual detail, fix it yourself rather than describing it again.

**Skills for the agents** are yours to write, hand out (`wb start … --skill <name>`, 0–2 that fit
the task) and keep honest. `reference/skills.md` has how: read it before you write, patch, import
or retire one, or triage lessons. **Images** (a sprite, an icon): a local engine draws them, you
art-direct (`wb image --list`); read `reference/images.md` first.

## 3. Dispatch
```
wb start <kebab-name> --spec <file> --repo <repo> --size significant|trivial \
   --test "<acceptance command>" [--skill <name>] [--model …] [--auto-fix 2]
```
It returns at once; a detached runner does the work. **Don't poll.** With the workbench mod loaded
the session is woken by a `[workbench]` prompt when the task needs you (review, question, stuck,
failed) — end your turn and wait for it. Without the mod, run `wb wait <name>` with Bash
`run_in_background: true` and act on its notification. If the context says wakes are off (the
plugin's wake setting), none will come: tell the user it runs, and wait only if they ask.
A session is woken only for its own tasks (those `wb start` ran in it, wherever; one started by
hand, or one whose Claude Code process has exited, from its directory or a repo in it): start a task from the session that will review it. Failing tests go back to the agent on their
own for `--auto-fix` rounds before you are woken; `wb log <name>` shows what it is doing.
A model that reads prompts slowly, or one the runner must load first, can spend minutes before
its first reply. That is normal, not stuck.
**Exit 4: the GPU is taken** by another task (often another session's project) or `wb image`,
which the model can't run beside. Don't wait silently: tell the user who holds it, then either
do the step yourself or `--queue` it to start when the GPU is free (it may be hours).

## Driving a running task
You are the driver, not a spectator. When something tells you the agent is heading the wrong way
(a check-in, its log, the user), act on it:
- `wb steer <name> --text "…"`: a note it gets as soon as its current run ends, before the
  tests. For course corrections that can wait a step.
- `wb steer <name> --now --text "…"`: interrupts it mid-step; the same session continues with
  your note first, marked high priority. For wrong files, a wrong approach, a runaway loop.
  While its tests run or it waits for the GPU there is nothing to interrupt: the note waits, and
  `wb steer` says so.
  The runner does this by itself once a round when the agent generates past its thinking
  budget with no tool call (7 minutes from its first output; `--think-timeout` overrides),
  and the check-in says so; a round that stalls again right after is stopped. Two nudges on one
  task mean the spec is too big for one step: split it, or name the first file to write.
- A note the runner never delivered (the round stopped first) is in the stuck report. `wb resume`
  carries it; `wb feedback` and `wb next` drop it and say so: put it in the feedback if it still
  matters.
- `wb takeover <name>`: stops it and hands you the worktree. Finish by hand, leaving it
  uncommitted (`wb accept` commits it), then `wb accept`, or hand it back with `wb feedback`. For when explaining would cost more than doing.
- **Commands and directories it asks for** (a `PERMISSION` question): lean to yes for what the
  task needs (`wb allow <name> "<pattern>"`, `wb allow <name> --path <dir>`); no to what reaches
  outside it. sudo, git writes and `gh` stay refused whatever you allow. The rules and the
  traps (links, toolchains, `/dev/pts`): `reference/driving.md`.
- `wb stop <name>`: just stop it. `wb resume <name>` continues it in a fresh session (not a
  review round), e.g. after fixing workbench under it. A task failed with "the runner died" (a
  reboot, a kill) or stuck with "stopped by a signal" resumes the same way.
- A check-in saying the session "reached N% of its window before it changed a file" (or "had
  read N% before its first change": a fresh session would have little room after re-reading it):
  the step needs more reading than the window holds. Let it carry on; next time name fewer files to read,
  or put what it needs (a signature, a constant) in the spec.
Notes are instructions, like feedback: concrete and short.

**The user's two commands.** Only these, never words in a message, stop the delegation and end
workbench mode; the mod runs them, closes the pane and tells you what it did:
- **`/workbench-quit-and-hand-back-claude`**: take over. This session's agents are already stopped
  (`wb takeover --all --repo <here> --session "$CLAUDE_CODE_SESSION_ID"`). Leave workbench mode and finish the work yourself without pausing: review
  what each agent left, complete it in its worktree to your standard, run the acceptance command,
  `wb accept`, then carry on with the rest of the request directly, without delegating.
- **`/workbench-quit`**: stop. This session's delegated agents are already stopped (`wb stop
  --all --repo <here> --session "$CLAUDE_CODE_SESSION_ID"`). Work directly from now on. Don't finish, restart or clean up anything
  unless asked; worktrees are kept.
Tasks listed as left alone belong to another live session: leave them unless asked.
Without the mod, the user may ask for the same in words; run those commands then.

**The web chat.** A prompt that is just `[web chat] <their words>` is the user on their phone,
with the same authority as here: reply there with `wb chat reply --text "…"`, short; read
`reference/driving.md` (the web chat) before your first reply.

**Check-ins.** A task wakes you on trouble (a nudge, a full window, a handover). With `--checkin
MIN` (off by default: each wake costs your whole context) it also wakes you every MIN minutes with a digest: tools used, errors, tokens, files touched, its latest
words, time so far and the ETA. Be the captain: give the user two or three plain lines on how it
is going, and steer only if it is drifting. Say what you see, including "slow but on course".
Each check-in is a turn that re-reads your context, so answer it in that one turn: no `wb log`
unless the digest shows trouble, and a `wb note <name> --text "…"` (it shows in `wb watch` and
the pane) only for something the user should find later.

## 4. When woken
- **review**: the wake carries the report and, when it is small, the whole diff (`wb diff <name>`
  only when it says the diff was too big, or after you change something). Read every line. Check:
  - **Correctness**: trace the logic, including edge cases, error paths and inputs the tests
    don't cover.
  - **The spec**: every requirement met, nothing missing, nothing added.
  - **Tests**: they test the behaviour and would fail on a bug; none weakened, skipped or
    special-cased; expected values not just copied from the output.
  - **Fit**: the repo's conventions, naming, idioms and existing helpers; no duplicated code.
  - **Hygiene**: no stray files, debug output, dead code or TODOs.
  - **Safety**: injection, secrets, unsafe file or shell use.

  **The bar is the same for every task; the checking is in proportion to it.** Each call you make
  re-reads your whole context, so a review of a dozen calls can cost more than writing the code
  (measured 2026-11-17: a trivial task cost 4.5 times as much handed off, mostly in review extras).
  - **Trivial**: reading the diff against the spec and the report's test result is the review. Run
    something only when a line leaves you in doubt.
  - **Significant**: also check the tests catch bugs: break the code in a scratch copy two or
    three ways, the bugs you would most expect (an off-by-one, a missed case from the spec), in
    one script that applies each break and runs the tests, and see each one fail. More is not
    safer, only dearer: twelve breaks cost more than the task's own review (2026-11-17). A UI change:
    run it from the worktree and look with your own browser tool.
  - Put the checks you do run in one Bash call (`wb status <name>` prints the worktree's path).
  Accept only what you would have written.
- **question** — answer it briefly and decisively: `wb answer <name> --text "…"`. The agent is
  blocked until you do.
- **stuck / failed** — `wb log <name> -n 30` if the report isn't enough; then feedback that unblocks
  it, or discard.
- **Lessons**: when the `lessons:` line says one was seen in more than one task, or you just
  corrected something the next agent will get wrong too, triage it now (`reference/skills.md`).
  A one-task detail needs no call.

## 5. Feedback
Numbered, concrete changes in a file → `wb feedback <name> --message <file>` (or `--text "…"`). Small models need
explicit instructions ("in parse(), return 0 for an empty string"), not critique. Send back
everything the agent must redo in one round. A nit you can fix in less than it takes to describe
it (a name, a blank line, a duplicated test line), fix yourself in the worktree and accept: the
bar holds, and a round costs a wake and a second review. Confirm or correct each assumption the summary lists, in the same
message: an assumption left unanswered comes back as a question, and the agent waits for it.
When the fix is for something your spec missed, add `--spec-gap "…"` (checklist item it broke).
**Max 3 review rounds.** Still wrong after that: make the small remaining fix yourself in the task's worktree
(`wb status <name>` prints its path) and accept, or `wb discard <name> --reason …`.
If a "trivial" task fails twice, it wasn't trivial: discard and restart it with `--size significant`.

## Context, on both sides
**The agent's.** Rounds of a task continue one OpenCode session, so the agent keeps what it read.
When that session passes its handover point (60% of the window until `wb tune` learns better
for that model; less on windows under ~32K, and a model served under 16K can't take tasks), the runner hands the task to a fresh session
at once, mid-round if need be: the task, the work so far (the diff), the agent's last words and
the new instructions. A round that hands over 5 times is stopped as stuck (the step is too big for
the window: split it), and from the second handover each one is a check-in. OpenCode's own compaction (the model summarising itself, which can lose the
task) is only a backstop past that. Reports show `agent context: …` so you see it coming. A
check-in or report that says `NOTE: the spec doesn't name …` lists files outside the spec: drift
until you've looked, so read them and steer (`--now`) if the agent left its task. `NOTE: <dir>: a
git repository of its own` is a nested repo the diff leaves out and `wb accept` refuses: have its
`.git` removed, or gitignore it. Override per round:
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

**What you learn about the user.** When the user tells you how they want delegation run (a
review bar, how much to ask them, what to merge, a style they always want), and it would hold in
any project, `wb notes add "<it, in one sentence>"`. Your own memory stays in one project; these
notes come back in every session in workbench mode (a `user-notes` block). A fact about one repo
goes in that repo's CLAUDE.md instead. Drop a note that has stopped holding (`wb notes drop N`).

## 6. Finish
- Approved: `wb accept <name> -m "<conventional commit subject>"` commits on `delegate/<name>`
  with the task and OpenCode session recorded. Add `--merge` only if the user passed `--merge` or
  said to merge; otherwise give them the merge command it prints.
- `wb discard <name> --reason "…"` drops it. `wb status` lists open tasks (e.g. after a context
  reset); `wb stats` compares the models on finished work.
- **Keep the good ones as bench cases.** An accepted step whose spec pinned the names its tests
  call, and whose tests fail on its base and pass in the agent's lane (one that commits or writes
  outside it never can: add refuses it), is a case: `wb bench add <name>` (`--step N` for a
  `wb next` step; `--hidden <paths>` or `--check "<cmd>"` when the tests it changed aren't the
  ones that tell). `wb bench run [--model <id>…] [--label <variant>]` replays the cases in the
  background, one at a time, after any real task and stopped by one; it wakes no one and costs
  you nothing. `wb bench report` compares models (or a `--label`, such as a workbench branch or
  a skill handed with `--skill`) on the same work. It is evidence for your `--model` and the
  weekly check, never a reason to skip review: the router doesn't read it.

## Report to the user
Short and honest: what was built, on which model, rounds (automatic and review), test result,
what you fixed, sent back or flagged, the branch. Report the agent's misses plainly; they are
evidence about the model, and the router learns from accept and discard. Mention the local tokens from the report next to the
`claude:` line `wb accept` prints (what the task cost you, measured), so the saving is visible.
