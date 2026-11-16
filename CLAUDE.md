# workbench

Claude delegates coding tasks to local models in OpenCode and reviews the results. README.md is
for people (what it is, install, use); this file is the technical reference.

## Layout
- `workbench/` is the Python package, standard library only (Python 3.11+, developed on 3.14). Do not add dependencies.
  - `store.py`: task directories, atomic JSON, locked JSONL. Every other part talks through these files.
  - `runner.py`: the detached per-task driver. `cli.py`: `wb`. `ask_mcp.py`: the MCP tool server.
  - `models.py`: discovers local models from opencode.json, their status, and GPU residency (make_resident);
    adds and retires them (Ollama's pull/delete, opencode.json entries) for the weekly model check.
  - `chat.py`: the web chat (`wb chat serve`, stdlib HTTP, a token and a Host check; `systemd/wb-chat.service`
    keeps it up). The mod hands its messages to the /delegate session as prompts; Claude replies with `wb chat reply`.
  - `watch.py`: waiting without polling: inotify (ctypes) on state directories and pidfd on processes.
    `wb events` streams state changes to the mod; `wb wait`, `log -f`, `watch`, the agent's
    question wait, steer interrupts and the chat stream all sleep in the kernel until a file changes.
  - `chat_static/`: the web chat's page (served by chat.py): a fetch-streamed event stream with resync
    on reconnect, an idempotent outbox, the paced reveal.
  - `hardware.py`: GPU/VRAM, RAM, CPU and disk, read fresh each time for `wb scout`. Nothing about the machine is hard-coded.
  - `router.py`: picks a model per task size: an educated guess (the largest model allowed significant
    work, the smallest for trivial) corrected by real results (accepted / tried, per size).
  - `events.py`: event normalization and the stuck detector.
  - `transcript.py`: `wb watch`, the human view: the OpenCode session export merged with the task's
    events. Its `--json` lines feed the mod's pane.
  - `context.py`: what the agent knows. It adds CLAUDE.md files and rules, the nested-instructions map and the handbook to the config layer's `instructions`, and it sets the denied tools and skills.
- Never name a model in code or the skill. Models come from OpenCode's config and choices from
  the router and the weekly model check, so newer models slot in without code changes. (README's
  findings section is the one place that names what this machine runs.)
- `agent/HANDBOOK.md`: Claude's working habits for the local agent. Every agent prompt carries it
  (a copy in the task directory), so keep it short: each token costs prefill time.
- `bin/wb`, `bin/wb-ask-mcp`: launchers that find the package from their own real path. Never
  use `python -I` with `PYTHONPATH`, because `-I` drops it.
- `mod/`: the Claude Code mod (TypeScript hooks module). `$` may only be passed to functions
  declared at the top level of `register.tsx`, so `claude plugin validate` can follow it.
- `skill/delegate/SKILL.md`: the `/delegate` procedure. `~/.claude/skills/delegate` links here.
  Skills for the agents live in each project's own `.claude/skills`, committed with it; the
  user's personal `~/.claude/skills` are denied to agents.
- `docs/logo.jpg`: the README's banner (generated with x.ai).

## How a task runs
1. `wb start` asks the router for a model (unless `--model`), makes branch `delegate/<task>` in a
   fresh worktree, writes the round-1 prompt, spawns the detached runner, and returns.
2. The runner takes the GPU lock (one model at a time), makes the model resident, and runs
   `opencode run --standalone --auto --format json` with the task's config layer. Events go to
   `events.jsonl`; `status.json` is the live summary.
3. Guards stop the agent on: one call repeated 4 times, 6 tool errors in a row, 40 calls without
   an edit, silence past `--silence-timeout` (900 s), a round past `--round-timeout` (2400 s).
   Generating past the think budget without acting gets one automatic nudge (a `--now` steer).
4. After each round the runner runs `--test`; failures go back to the agent up to `--auto-fix`
   times (2) without waking Claude. Rounds continue one session until it is 60% full or OpenCode
   compacted it; then a fresh session gets the task, the diff and the instructions.
5. It writes the round report and a `review`/`stuck`/`failed` notification; `ask_reviewer` (MCP,
   budgeted) raises a `question` at any time and blocks until `wb answer`.
6. The mod (or `wb wait`) delivers each notification once, while still true; acting on a task
   acknowledges it. A notice Claude never saw is re-raised after a minute (`rewake`).
7. `wb accept` commits with provenance trailers (no AI co-author line), optionally merging;
   `wb next` commits a step and gives the same agent the next; `wb feedback` sends another round;
   `wb discard` drops it. `results.jsonl` gets a row, which the router learns from.

## State and config
Everything is plain files under `~/.local/state/workbench` (`$WB_HOME`):
```
notify.jsonl  results.jsonl  gpu.lock  scout.json  watch.json  chat/
tasks/<name>/  task.json status.json events.jsonl opencode.json HANDBOOK.md acked runner.log
               rounds/<n>/{prompt.md,summary.md,tests.txt}  ask/<id>.json|.answer  worktree/
```
A model is a candidate when its OpenCode provider's `baseURL` is on this machine
(`~/.config/opencode/opencode.json`). `~/.config/workbench/models.json` (`$WB_MODELS`) holds only
what OpenCode can't say, by provider or model id: `start`/`stop` commands for a one-model server,
`skip`, and `sizes` (`wb model sizes`). Ollama entries need `options.max_tokens` (Ollama ignores
OpenCode's `max_completion_tokens`); `wb model add` writes it and `wb doctor` checks it.

## Troubleshooting
- `wb doctor`: opencode and its config, git, each model's readiness, the state directory.
- A model "can't run": `wb models` says why (server down, not pulled, no `start` command).
- The agent can't see `ask_reviewer`: `PWD=<worktree> OPENCODE_CONFIG=<task>/opencode.json
  opencode mcp list --print-logs`.

## Conventions
- Output from `wb` is read by Claude, whose tokens are the scarce resource: short, plain, no
  decoration, one report per command. Raw logs stay in files.
- State changes go through `Task.set_status` (locked read-modify-write) and `append_jsonl`.
  Never write status.json directly.
- OpenCode quirks this code relies on, each covered by a test:
  - It resolves the project from `$PWD`, not from the cwd.
  - The 2.x config shape is `mcp.servers.<name>`, with `timeout.{startup,execution}`; `codemode: false` exposes a plain tool.
  - `--worktree` takes `=name`. The runner makes its own worktrees anyway.
  - `--standalone` servers inherit the env and share session storage with the background service.
  - It loads AGENTS.md itself but never CLAUDE.md, which reaches it through `instructions` (absolute paths).
  - Skills come from `~/.claude/skills` and `.claude/skills`. A `disable-model-invocation` skill is not listed.
  - `permissions: [{action, resource, effect: "deny"}]` removes a tool or skill from the prompt.
    For the `shell` action the resource matches each part of a compound command (`cd x && sudo y`
    is caught), and a later rule overrides an earlier one. The agent's only shell limit is sudo;
    git writes and deploys are the driver's by convention (the handbook), not by rule.
    Denying `execute` (code mode) turns the `opencode_*` tools into direct tools, so deny `opencode_*` too.
    `opencode_worktree_remove` stays listed whatever you do; the rule still refuses the call.
- A session is bound to the directory it was created in: continued or `--fork`ed from another
  worktree, the model is still told the first one. So an agent's context carries over only
  within its own worktree (rounds, `wb next`), never to a new task.
- `session export` lists OpenCode's compactions as messages `{type: "compaction", status, summary}`;
  an assistant message's `tokens` (input + cache read/write + output) is the context the next
  request carries, which the runner's fresh-session check reads.
- To see exactly what OpenCode sends a model, point a provider at `tests/capture_llm.py`, which records
  each request, and inspect the system prompt and tools. That is how the facts above were found.

## Checks before committing
```sh
python3 -m unittest            # ~15 s: real git, real runners, fake opencode, plus one real-opencode check
claude plugin validate mod && claude plugin test mod
```
A change to the web chat (`chat.py`, `chat_static/`) also needs the browser test: the real server and page
in WebKit as an iPhone and Chromium as a Pixel, through sends, receipts, an outage and a reload.
Playwright isn't a dependency; point `PLAYWRIGHT` at any install:
`PLAYWRIGHT=…/node_modules/playwright node tests/e2e_chat.mjs`.
A change to how OpenCode is invoked also needs one real run: `wb start` on MiMo against a
scratch repo, as the README describes.
