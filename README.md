# workbench

Claude as architect and reviewer; local models as the hands.

Claude writes a spec and hands it to a local model (Qwen3.8 Flash-Next Coder on
[Strata](https://github.com/Niko1221/Strata), or Qwen3.8 27B / MiMo-V2.6 9B on Ollama) running
in [OpenCode](https://opencode.ai). The agent works in its own git worktree. A detached runner
streams what it does, runs the acceptance tests after every round, and sends failures back to the
agent by itself. Claude is only woken for work that passes, a question, or a run that got stuck.
Claude then reviews the diff and accepts it or steers it. Code generation happens on the GPU;
Claude's tokens go to specs and review.

```
 Claude Code ──/delegate──▶ wb start ──▶ runner (detached) ──▶ opencode run ──▶ Strata / Ollama
     ▲                                     │  events, tests,        │
     │                                     │  auto-fix rounds       └─ ask_reviewer (MCP) ─┐
     └── workbench mod: pane, status, wake ◀── ~/.local/state/workbench ◀───────────────────┘
```

## Parts

| Part | Where | What it does |
|---|---|---|
| `wb` CLI | `bin/wb`, `workbench/cli.py` | start, feedback, answer, wait, status, log, report, diff, accept, discard, stop, models, stats, doctor |
| runner | `workbench/runner.py` | one detached process per task: rounds, event streaming, test gate, auto-fix, stuck/silence/time limits, GPU lock |
| ask_reviewer | `workbench/ask_mcp.py`, `bin/wb-ask-mcp` | an MCP tool the agent can use to ask Claude a blocking question (budgeted) |
| mod | `mod/` | Claude Code mod: live **Workbench** pane, status line, toasts, and a turn for Claude when a task needs it |
| skill | `skill/delegate/SKILL.md` | the `/delegate` procedure Claude follows |
| models | `workbench/models.py` | the registry (`strata`, `qwen27b`, `mimo`) and whether each can run now |

All state is files under `~/.local/state/workbench` (`$WB_HOME`): one directory per task with
`task.json`, `status.json`, `events.jsonl`, the round prompts and reports, the worktree, and
the questions. The CLI, the runner, the MCP server and the mod coordinate only through those files.

## A task's life

1. `wb start <name> --spec spec.md --repo . --size significant --test "pytest -q"` makes branch
   `delegate/<name>` in a new worktree, writes the round-1 prompt (the spec plus the working
   protocol), and starts the runner. It returns immediately.
2. The runner waits for the GPU lock, then runs `opencode run --standalone` with a per-task config
   layer (`OPENCODE_CONFIG`) that adds the `ask_reviewer` server. Each event goes to
   `events.jsonl` and `status.json`.
3. It stops the agent if the agent repeats the same call 4 times, has 6 tool errors in a row, makes
   40 calls without editing a file, stays silent past the timeout, or runs past the round limit.
4. After the round it runs the test command. A failure goes back to the agent as the next round,
   up to `--auto-fix` times, without waking Claude.
5. It writes the round report and raises a notification: `review`, `stuck` or `failed`. A
   `question` comes from the MCP tool at any time.
6. The mod (or `wb wait`) delivers the notification once. Claude reviews with `wb diff`, then
   runs `wb accept` (commit on the branch, optional `--merge`), `wb feedback` (next round, same
   OpenCode session) or `wb discard`.
7. `results.jsonl` gets a row; `wb stats` compares the models on real work.

## Install

```sh
ln -s ~/repos/workbench/bin/wb ~/.local/bin/wb                 # the CLI
ln -s ~/repos/workbench/skill/delegate ~/.claude/skills/delegate # the /delegate skill
claude plugin marketplace add ~/repos/workbench               # the mod, read from this folder
claude plugin install workbench@workbench
```

Python 3.11+ standard library only. Needs `opencode` 2.x, `git`, and at least one model server
(`wb doctor` checks them all). Add or change models in `~/.config/workbench/models.json`, using the
same shape as `DEFAULT_MODELS`.

## Develop

```sh
python3 -m unittest -v               # end to end with a fake opencode (tests/fake_opencode.py)
claude plugin validate mod && claude plugin test mod
```
