# workbench

Claude delegates coding tasks to local models in OpenCode and reviews the results. README.md is
for people: the pitch and a quickstart, then (below its rule) "How it works", the full account of
every feature. This file is the technical reference for changing the code. A change that alters
what either says (a command, a flag, a default, a guard, a file) updates it in the same commit.

## Layout
- `workbench/` is the Python package, standard library only (Python 3.11+, developed on 3.14). Do not add dependencies.
  - `store.py`: task directories, atomic JSON, locked JSONL. Every other part talks through these files.
  - `runner.py`: the detached per-task driver. `cli.py`: `wb`. `ask_mcp.py`: the MCP tool server.
  - `gitops.py`: the task's worktree and branch, the review diff (intent-to-add, so new files
    show), commits. `results.py`: results.jsonl rows (`wb accept`/`discard`) and `wb stats`.
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
  - `skills.py`: Claude's skill library for the agents (`$WB_SKILLS`, default
    ~/.local/share/workbench/skills, its own git repo): checks, add/retire/restore, what each task
    was offered and loaded. `wb skill` drives it; a task holds the skills in `status.skills`.
  - `context.py`: what the agent knows. It adds CLAUDE.md files and rules, the nested-instructions map and the handbook to the config layer's `instructions`, and it sets the denied tools and skills.
- Never name a model in code or the skill. Models come from OpenCode's config and choices from
  the router and the weekly model check, so newer models slot in without code changes. (README's
  quickstart and findings are the only places that name what this machine runs.)
- `agent/HANDBOOK.md`: Claude's working habits for the local agent. Every agent prompt carries it
  (a copy in the task directory), so keep it short: each token costs prefill time.
- `bin/wb`, `bin/wb-ask-mcp`: launchers that find the package from their own real path. Never
  use `python -I` with `PYTHONPATH`, because `-I` drops it.
- `mod/`: the Claude Code mod (TypeScript hooks module). `$` may only be passed to functions
  declared at the top level of `register.tsx`, so `claude plugin validate` can follow it.
- `skill/delegate/SKILL.md`: the `/delegate` procedure. `~/.claude/skills/delegate` links here.
  Skills for the agents: Claude's library (`wb skill`), handed per task, or a project's own
  `.claude/skills`, committed with it. The user's personal `~/.claude/skills` are denied to agents.
- `docs/`: the README's images: `logo.jpg` (generated with x.ai), `driver-and-agent.jpg` and
  `web-chat.png` (screenshots; black out any `wb chat url` link, it carries the token).

## How a task runs
1. `wb start` refuses a repo without a committed CLAUDE.md (`--no-claude-md`), asks the router for
   a model (unless `--model`), makes branch `delegate/<task>` in a fresh worktree, writes the
   round-1 prompt (the spec plus `cli.PROTOCOL`), records any `--skill`, spawns the detached
   runner, and returns.
2. The runner takes the GPU lock (one model at a time), makes the model resident, and runs
   `opencode run --standalone --auto --format json` with the task's config layer. Events go to
   `events.jsonl`; `status.json` is the live summary.
3. Guards stop the agent on: one call repeated 4 times, 6 tool errors in a row, 40 calls without
   an edit, silence past `--silence-timeout` (900 s), a round past `--round-timeout` (2400 s).
   Generating past the think budget (`--think-timeout`, 420 s) without acting gets one automatic
   nudge a round (a `--now` steer). Significant tasks raise a `checkin` notice every `--checkin`
   minutes (10) while they run. `wb steer` queues a note for the end of the run; `--now`
   interrupts it (`_steer_bell` wakes the round on steer.jsonl). Either continues the session.
4. After each round the runner runs `--test`; failures go back to the agent up to `--auto-fix`
   times (2) without waking Claude. Rounds continue one session until it is 60% full or OpenCode
   compacted it; then a fresh session gets the task, the diff and the instructions.
5. It writes the round report and a `review`/`stuck`/`failed` notification; `ask_reviewer` (MCP,
   budgeted) raises a `question` at any time and blocks until `wb answer`.
6. The mod (or `wb wait`) delivers each notification once, while still true; acting on a task
   acknowledges it. A notice Claude never saw is re-raised after a minute (`rewake`).
7. `wb accept` commits with provenance trailers (no AI co-author line), optionally merging;
   `wb next` commits a step and gives the same agent the next; `wb feedback` sends another round;
   `wb discard` drops it. `results.jsonl` gets a row, which the router and the skill records
   learn from. `wb takeover` stops the agent and leaves the task in `review` for Claude to finish.

## State and config
Everything is plain files under `~/.local/state/workbench` (`$WB_HOME`):
```
notify.jsonl  results.jsonl  gpu.lock  scout.json  watch.json
chat/          messages.jsonl receipts.jsonl listener.json
tasks/<name>/  task.json status.json events.jsonl opencode.json HANDBOOK.md context.md
               steer.jsonl acked runner.log  rounds/<n>/{prompt.md,summary.md,tests.txt}
               ask/<id>.json|.answer  worktree/
```
The skill library is `~/.local/share/workbench/skills` (`$WB_SKILLS`), a git repo of its own
(retired skills in `retired/`). The web chat's token is `~/.config/workbench/chat-token`
(`$WB_CHAT_TOKEN`, mode 600).

A model is a candidate when its OpenCode provider's `baseURL` is on this machine
(`~/.config/opencode/opencode.json`). `~/.config/workbench/models.json` (`$WB_MODELS`) holds only
what OpenCode can't say, by provider or model id: `start`/`stop` commands for a one-model server,
`skip`, `sizes` (`wb model sizes`), `weights_gb` (the router's size for a server that can't
say) and `label`. Ollama entries need `options.max_tokens` (Ollama ignores
OpenCode's `max_completion_tokens`); `wb model add` writes it and `wb doctor` checks it.

Environment: `WB_HOME`, `WB_MODELS`, `WB_SKILLS`, `WB_CHAT_TOKEN` (paths above);
`WB_OPENCODE_CONFIG` (OpenCode's config, default `~/.config/opencode/opencode.json`);
`WB_COMPACT_AT` (the mod's early compaction, a percentage, 0 off); `WB_BIN` (the `wb` the mod runs).
Tests only: `WB_OPENCODE_BIN` (a fake opencode), `WB_FAKE_MODELS` (no model servers).
`WB_TASK` names the task for the ask server and the agent's environment.

## The mod
`mod/hooks/register.tsx` keeps no state of its own that matters: it reads the task files.
- Its ears are a `wb events` child (inotify); each line runs `poll()`. The 1.5 s timer only redraws
  running clocks and polls every 30 s should the child be gone.
- A wake is a submitted prompt for a task's newest notice past its `acked` mark, only while that
  news is still true (the phase matches; a check-in while the task runs), never for one older than
  an hour, and held while Claude's main loop is mid-turn (`mem.busy`, set by `turn.start` or any
  `tool.call`). It writes `acked` before submitting. `rewake` covers a task waiting on Claude for a
  minute with no notice delivered. `_ack_all` in cli.py stops the mark short of news still true.
- A slash command may not start a turn itself: it pushes onto `mem.queued`, which the next poll
  submits.
- Compaction: `session.compact` adds KEEP_ON_COMPACT whenever tasks are open; at a task boundary
  it compacts once Claude passes COMPACT_AT of the auto-compact threshold; `prompt.context` adds
  the open tasks (`wb status`) and, for six hours after a web message, the `web-chat` block.
- The web chat listener: a session that ran /delegate (or a delegating `wb` command) claims
  chat/listener.json with its `wb events` pid; the first live claim wins. User messages go in as
  prompts (`asUser`), into the running turn via `session.send` when busy, with a receipt.
- The weekly model check: in a quiet moment (the person has had a turn, no task open) it submits
  SCOUT_NOTE when scout.json's `done_at` is a week old, marking `offered_at` so it asks at most daily.
- Atom shapes are tagged (`tasks-1`, `live-1`, `chat-2`): change the tag when the type changes.

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
    The config's `skills` list adds more: a path to one skill's directory lists that skill, a parent
    directory lists every skill in it, a path to the SKILL.md itself lists nothing. Handed skills
    go in one directory each, so the rest of the library stays out of the prompt.
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
