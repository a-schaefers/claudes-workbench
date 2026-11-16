# workbench

**Claude is the architect and reviewer. Local models do the typing.**

`workbench` lets Claude Code hand a coding task to a local model running in
[OpenCode](https://opencode.ai), and review the result like a pull request. The model is
Qwen3.8 Flash-Next Coder on [Strata](https://github.com/Niko1221/Strata), or Qwen3.8 27B /
MiMo-V2.6 9B on [Ollama](https://ollama.com). Claude writes the spec and judges the diff. The
GPU writes the code. Failing tests go back to the agent automatically, so Claude is only woken
for work that passes, a real question, or a run that got stuck.

```
 you ─/delegate─▶ Claude Code ─ wb start ─▶ runner (detached, one per task)
                     ▲                          │  git worktree on delegate/<task>
                     │                          ▼
                     │                     opencode run ──▶ Strata :8080 / Ollama :11434
                     │                          │   ▲
                     │            events, tests,│   │ ask_reviewer (MCP, blocking, budgeted)
                     │            auto-fix      ▼   │
                     └── workbench mod ◀── ~/.local/state/workbench (task files)
                         pane · status line · toast · wakes Claude with the report
```

## Why

A Claude subscription is the scarce resource. Code generation, test-fix loops and
"try again" rounds are the token-heavy part of agentic coding, and they are exactly the part a
local model can do on its own when it has a good spec and a test to satisfy. Claude's tokens go
to the parts that need judgment: scoping, the spec, the review, and the final call.

## Quick start

```sh
# 1. the CLI and the skill
ln -s ~/repos/workbench/bin/wb ~/.local/bin/wb
ln -s ~/repos/workbench/skill/delegate ~/.claude/skills/delegate

# 2. the Claude Code mod (live pane, status line, wake-ups)
claude plugin marketplace add ~/repos/workbench
claude plugin install workbench@workbench

# 3. check everything is reachable
wb doctor
wb models
```

Then, in Claude Code:

```
/delegate add a --json flag to `report` that prints the summary as JSON
```

Claude scopes the task, writes a spec, and starts it, then ends its turn. The **Workbench** pane
shows the agent's tools, test results and questions as they happen. When the task needs Claude,
the mod starts a turn with the report in it. Claude reviews the diff, then accepts it (a commit
on `delegate/<task>`, optionally merged) or sends numbered feedback for another round in the
same OpenCode session.

## What the local agent knows

OpenCode on its own loads `AGENTS.md` and skills, not `CLAUDE.md`. Each task gets a config layer
(`OPENCODE_CONFIG`, built by `workbench/context.py`) so the agent starts with what Claude would
know in the same repository:

| Source | How it reaches the agent |
|---|---|
| `agent/HANDBOOK.md` | always: how Claude works (read before editing, match the code, smallest diff, verify, never weaken tests, honest summary) |
| `~/.claude/CLAUDE.md` | loaded if present |
| repo `CLAUDE.md`, `.claude/CLAUDE.md`, `CLAUDE.local.md` | loaded: the worktree's copy if committed, the repo's if local or uncommitted |
| `.claude/rules/*.md` | loaded |
| `AGENTS.md` | loaded by OpenCode itself; a CLAUDE.md with the same text is skipped |
| nested `CLAUDE.md` / `AGENTS.md` | listed in a short map; the agent reads one before working in its directory |
| skills in `~/.claude/skills`, `.claude/skills` | listed by OpenCode, loaded on demand with the `skill` tool |
| the spec + protocol | the round-1 prompt: the task, the acceptance command, the question budget |

The same layer applies a policy for an unattended run. `question` is denied, because nobody is
there to answer; the agent uses `ask_reviewer` instead. Also denied:
- code mode and the `opencode_*` session/worktree tools
- `subagent`, which would queue a second model behind the GPU
- `websearch`
- skills that need Claude's own app tools (`~/.claude/skills/synced`), and `/delegate` itself

Denied tools also leave the prompt. That cuts the fixed prompt from about 9.2k to about
3.5k tokens, which saves minutes of prefill on Strata.

## A task's life

1. **start**: `wb start` makes branch `delegate/<task>` in a fresh worktree, writes the round-1
   prompt and spawns the runner. It returns at once.
2. **run**: the runner takes the GPU lock (one model at a time fits in 16 GB), then runs
   `opencode run --standalone --auto --format json` with the task's layer. Each event is
   normalized into `events.jsonl`; `status.json` carries the live summary.
3. **guard**: it stops the agent when it:
   - repeats one call 4 times
   - makes 6 tool errors in a row
   - makes 40 calls without an edit
   - goes silent past `--silence-timeout` (900 s)
   - runs past `--round-timeout` (2400 s)

   Time spent waiting on a question doesn't count as silence.
4. **test gate**: after each round it runs `--test`. On a failure it sends the output back to the
   agent, up to `--auto-fix` times (2 by default), without waking Claude.
5. **notify**: it writes the round report and a `review`, `stuck` or `failed` notification.
   `ask_reviewer` raises a `question` at any time and blocks until `wb answer`.
6. **deliver**: the mod, or `wb wait`, delivers each notification once, and only while it's
   still true. Acting on a task (accept, discard, feedback, answer) acknowledges its
   notifications.
7. **decide**: `wb accept` commits with trailers (rounds, the OpenCode session, a Co-Authored-By
   line), with optional `--merge`. `wb feedback` continues the same session. `wb discard` removes
   the worktree and branch.
8. **learn**: `results.jsonl` gets a row; `wb stats` compares models on real work.

## Commands

| Command | Does |
|---|---|
| `wb start <name> --spec F --repo R [--size trivial\|significant] [--model M] [--test CMD]` | start a task (`--auto-fix N`, `--ask-budget N`, `--base REF`, timeouts, `--force`) |
| `wb wait <name>` | block until the task needs Claude; print the report once |
| `wb status` | all tasks, one line each |
| `wb log <name> [-n N] [-f] [-v]` | the agent's recent events |
| `wb report <name>` | the latest round report |
| `wb diff <name> [--stat] [paths…]` | the change, including new files |
| `wb answer <name> --text T` | answer the agent's question |
| `wb feedback <name> --message F` | another round in the same session |
| `wb accept <name> -m MSG [--merge]` | commit on the branch (and merge) |
| `wb discard <name> [--reason R] [--keep-branch]` | drop it |
| `wb stop <name>` | stop a running agent (it becomes `stuck`) |
| `wb models` · `wb stats` · `wb doctor` | registry and readiness · per-model results · environment check |

## Models

`workbench/models.py` holds the defaults. Override or add entries in
`~/.config/workbench/models.json` (or `$WB_MODELS`), using the same shape.

| key | OpenCode model | tier | notes |
|---|---|---|---|
| `strata` | `strata/strata` | significant | MoE, experts stream from RAM/SSD. Fast decode once warm, slow prefill |
| `qwen27b` | `ollama/qwen3.8-27b` | significant | dense 27B at Q3_K_XL, fully on the GPU |
| `mimo` | `ollama/mimo-v2.6:9b-q8_0` | trivial | ~60 tok/s, 100 % GPU |

`--size significant` picks whichever significant model is ready. `--size trivial` runs on
whatever already holds the GPU instead of evicting it. An Ollama model reports not-ready while
Strata holds the card.

## State

Everything is plain files under `~/.local/state/workbench` (`$WB_HOME`). The CLI, the runners,
the MCP server and the mod coordinate only through them:

```
notify.jsonl  results.jsonl  gpu.lock
tasks/<name>/  task.json status.json events.jsonl opencode.json context.md acked runner.log
               rounds/<n>/{prompt.md,summary.md,tests.txt}  ask/<id>.json|.answer  worktree/
```

## The mod

`mod/` is a Claude Code mod (a hooks module):
- a **Workbench** pane (`/workbench`) with each task's phase, round, model, test state and last
  few events
- a status line (`wb · fix-parser running r2 · 1 awaiting review`)
- a toast per notification
- with the `wake` option on (the default), a prompt that starts Claude's turn with the report and
  the next command

It reads the same files as `wb`, and shares acknowledgements with `wb wait` through each task's
`acked` file.

## Troubleshooting

- `wb doctor` checks opencode and its config, git, each model's readiness and the state directory.
- **Ollama model "not ready"**: Strata holds the GPU. Run `strata launch --stop --preset coder`,
  or use `--model strata`.
- **Strata's first reply takes minutes**: it is reading the prompt (`strata monitor` shows
  progress). Later turns reuse the cached prefix.
- **The agent can't see `ask_reviewer`**: run `PWD=<worktree> OPENCODE_CONFIG=<task>/opencode.json
  opencode mcp list --print-logs`.
- **What did the model actually receive?** Point a provider at a capturing fake server and
  inspect the request. `tests/fake_opencode.py` fakes the other side, OpenCode itself.

## Develop

Standard library Python 3.11+, no dependencies. See [CLAUDE.md](CLAUDE.md) for conventions and
the OpenCode quirks the code relies on.

```sh
python3 -m unittest -v                              # ~13 s: real git, real runners, fake opencode
claude plugin validate mod && claude plugin test mod
```
