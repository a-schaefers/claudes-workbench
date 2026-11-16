---
name: delegate
description: >
  Delegate a coding task to a local model (Qwen3.8 Flash-Next Coder on Strata or Qwen3.8 27B on
  Ollama for significant work, MiMo 9B on Ollama for trivial items) running in OpenCode, via the
  workbench `wb` CLI; Claude writes the spec, reviews the diff, and accepts or sends feedback, so
  Claude spends tokens on specs and review instead of code generation. Only when the user invokes
  /delegate — never on your own.
disable-model-invocation: true
argument-hint: "<task description> [--model strata|qwen27b|mimo] [--merge]"
---

# /delegate — local agent writes, Claude reviews

The user's Claude budget is tight; that is the point of this mode. You are **architect and
reviewer**: write a precise spec, hand it to a local agent with `wb`, let the runner's test gate
and auto-fix rounds do the cheap iterations, review only what passes, then accept or steer. Don't
write the code yourself unless the escape hatch fires.

Task: `$ARGUMENTS`

`wb` is the workbench CLI (`~/repos/workbench`, on PATH as `wb`; `wb --help`). Every command
prints a short report written for you — don't go digging in raw logs unless a report says to.

## 1. Size it, pick the model
| Task | Model | `--size` / `--model` |
|---|---|---|
| **Significant**: new feature, multi-file change, refactor, non-obvious bug, design judgment, >~50 lines | Strata Flash-Next Coder, or Qwen3.8 27B | `--size significant` (picks whichever is ready), or `--model strata` / `--model qwen27b` |
| **Trivial**: rename, string/config tweak, a test for existing code, small single-function change, boilerplate | MiMo 9B | `--size trivial` |

`wb models` shows what can run now. One model fits on the GPU at a time: Strata holds the card
while it runs, so the Ollama models report not-ready then. Trivial work runs on whatever is loaded
rather than evicting it. A significant task whose models are all down → ask the user (start Strata
with `strata launch`, or `ollama stop` the other model) rather than silently using MiMo. If the
user names a model, use it.

## 2. Spec (the most valuable thing you write)
Read just enough of the repo to scope it, then write the spec to a file in the scratchpad:
- the goal in 1–2 sentences; the exact files/functions to touch, and what not to touch
- conventions to follow (point at an existing file to imitate)
- acceptance criteria, and an **acceptance command** that proves them (an existing test command,
  or a new test the agent must write first — say which)
`wb` appends the working protocol itself (no commits, run the tests, stay in scope, ask_reviewer
budget, the summary format), so don't repeat it. One function/module/feature per task; split big
asks and run the parts one after another.

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

## 4. When woken
- **review** — the report says tests PASS/FAIL, the agent's summary and the files. Read
  `wb diff <name> --stat`, then `wb diff <name>` (or `wb diff <name> <path>` for big ones). Review
  as strictly as any code review: correctness, the spec met, scope creep, stray files, security.
- **question** — answer it briefly and decisively: `wb answer <name> --text "…"`. The agent is
  blocked until you do.
- **stuck / failed** — `wb log <name> -n 30` if the report isn't enough; then feedback that unblocks
  it, or discard.

## 5. Feedback
Numbered, concrete changes in a file → `wb feedback <name> --message <file>`. Small models need
explicit instructions ("in parse(), return 0 for an empty string"), not critique. **Max 3 review
rounds.** Still wrong after that: make the small remaining fix yourself in the task's worktree
(`~/.local/state/workbench/tasks/<name>/worktree`) and accept, or `wb discard <name> --reason …`.
If MiMo fails a "trivial" task twice, it wasn't trivial: discard and restart on a significant model.

## 6. Finish
- Approved: `wb accept <name> -m "<conventional commit subject>"` commits on `delegate/<name>`
  with the task and OpenCode session recorded. Add `--merge` only if the user passed `--merge` or
  said to merge; otherwise give them the merge command it prints.
- `wb discard <name> --reason "…"` drops it. `wb status` lists open tasks (e.g. after a context
  reset); `wb stats` compares the models on finished work.

## Report to the user
Short: what was built, on which model, rounds (automatic and review), test result, what you
fixed or flagged, the branch. Mention the local tokens from the report next to the rough size of
your own review, so the saving is visible.
