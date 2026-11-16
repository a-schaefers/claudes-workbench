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

Task: `$ARGUMENTS`

`wb` is the workbench CLI (`~/repos/workbench`, on PATH as `wb`; `wb --help`). Every command
prints a short report written for you — don't go digging in raw logs unless a report says to.

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
verify, honest summary) and the skills in `~/.claude/skills` and the repo. So don't restate
project conventions; spend the spec on what is specific to this task: the files to read first,
the existing code to imitate, and any skill to load by name ("load the `x` skill first").
One function/module/feature per task; split big asks and run the parts one after another.

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
- `wb takeover <name>`: stops it and hands you the worktree. Finish by hand, then `wb accept`,
  or hand it back with `wb feedback`. For when explaining would cost more than doing.
- `wb stop <name>`: just stop it.
Notes are instructions, like feedback: concrete and short.

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
