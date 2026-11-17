# Claude's Workbench

Claude delegates coding tasks to local models in OpenCode and reviews the results. `wb` (the CLI)
starts a task in its own git worktree, a detached runner drives OpenCode round by round, and the
Claude Code mod wakes Claude when a task needs it. Everything talks through plain files.

The short name, `workbench`, stays on everything inside: the package, the plugin
(`workbench@workbench`), the state and config directories, and the clone path `~/repos/workbench`
ARCHITECTURE.md's *Installing* suggests for developing (users install from the marketplace; nothing
depends on it: `bin/wb` finds the package from its real path, and the systemd unit runs
`~/.local/bin/wb`).

## Keeping the docs true
README.md is for people and stays simple: the pitch, a quickstart, then a pointer. ARCHITECTURE.md
is for agents and contributors: every feature, how it works and why, every command and flag, every
file format, the tests, and what to change together (*Couplings*). This file is the compact
reference for changing the code. New detail goes in ARCHITECTURE.md, not here or in README.md.

**A change isn't done until the docs say what the code now does, in the same commit.** The docs
once fell months behind and took a whole-codebase audit to fix (2026-11-16); the user doesn't want
that again. So before every commit:
1. For each name the diff adds, renames, removes or changes the meaning of (a command, flag,
   default, constant, guard, status or task.json key, event or notice kind, file, prompt text,
   environment variable), `grep` both CLAUDE.md and ARCHITECTURE.md for it and fix every hit.
2. Find the ARCHITECTURE.md section that describes the behaviour you changed and reread it: if
   it no longer says what the code does, rewrite it. *Couplings* lists what else must change.
3. A new command or flag goes in *Every `wb` command*; a new key or file in *Files and formats*;
   a new module in this file's *Layout*; a new environment variable in *State and config*;
   anything Claude does differently in `skill/delegate/SKILL.md`.
4. `tests/test_docs.py` fails when a command, flag, module or environment variable is missing
   from the docs, or this file passes 25,000 characters. It can't see defaults or behaviour:
   steps 1 and 2 are how those stay true.
A commit message that describes a behaviour change while the docs still describe the old one is
the drift this rule exists to stop.

## Commands
```sh
python3 -m unittest                                   # from the repo root: the whole suite, about two minutes
python3 -m unittest tests.test_workbench.Context      # one class (or .Context.test_…); -k handover by name
claude plugin validate mod && claude plugin test mod  # the mod: the $ rule, then mod/hooks/register.test.tsx
PLAYWRIGHT=…/node_modules/playwright node tests/e2e_chat.mjs   # the web chat in WebKit and Chromium
wb doctor                                             # opencode, its config, git, each model, the state dir, Landlock
```
The suite runs real git and real runner processes against a fake OpenCode
(`tests/fake_opencode.py`, `WB_OPENCODE_BIN`) with no model servers (`WB_FAKE_MODELS`). Its
real-OpenCode tests (RealOpenCode*, through `tests/capture_llm.py`) skip without `opencode` on
PATH, and the lane tests without Landlock: a skip is not a pass, so a change to `context.py`, the
config layer, `gitguard.py` or `lane.py` needs them run where they don't skip.

## Checks before committing
main takes no direct push: commit on a branch or worktree, then `scripts/ship.sh` (pushes, opens
the pull request, arms auto-merge, waits). The owner's pull requests merge once `check` is green;
anyone else's wait for the owner (ARCHITECTURE.md, *Shipping to main*).
- The docs match the change (*Keeping the docs true*).
- `python3 -m unittest` (`tests/test_docs.py` included), and `claude plugin validate mod &&
  claude plugin test mod`.
- A change to the web chat (`chat.py`, `chat_static/`): the browser test too (sends, receipts, an
  outage, a reload; WebKit as an iPhone, Chromium as a Pixel). Playwright isn't a dependency.
- A change to how OpenCode is invoked or read (the runner's argv, the event or export fields):
  update `tests/fake_opencode.py` in the same commit, and do one real run: a scratch repo from
  `wb init` with a committed CLAUDE.md, `wb start <t> --trial --size trivial --spec <f> --repo
  <dir> --test '<cmd>'`, `wb watch <t>`, `wb report <t>`, then `wb discard <t>`. `--trial` keeps it
  out of the router's evidence and the lessons (`wb trial <task>` marks one after the fact). It
  takes the GPU and writes the real state directory.

## Layout
`workbench/` is the Python package, standard library only (3.11+, developed on 3.14). Do not add
dependencies. No packaging: it runs from the clone.
- `store.py`: task directories, atomic JSON, locked JSONL, `Task` (`set_status`, `notify`). Every
  other part talks through these files.
- `cli.py`: `wb` (argparse, `cmd_*`), `PROTOCOL` (the agent's working rules), `spawn_runner`,
  `_ack_all`.
- `runner.py`: the detached per-task driver: the GPU lease, rounds, guards, the think nudge,
  steering, tests and auto-fix, the report, handovers to a fresh session (`fresh_reason`,
  `handoff_prompt`, `HANDOVER_ROOM`), the config layer (the user's MCP servers off, the rules
  again as `OPENCODE_CONFIG_CONTENT`: `rules_env`) and the agent's environment (credentials
  dropped: `SECRET_ENV`, `USERINFO`, but `ENV_KEEP`; `commands.json` `env` passes named ones
  through).
- `events.py`: OpenCode event normalization and the stuck detector.
- `ask_mcp.py` (+ `bin/wb-ask-mcp`): the `ask_reviewer` MCP server, in the lane, writing only
  `ask/`; and the runner's side (`relay`: checks each ask file, counts the budgets in status
  `asks`, raises the notice, keeps the phase `asking`), `write_answer` (never through a link).
- `context.py`: what the agent knows and may do: instructions, denied tools and skills, the shell
  allowlist (`shell_rules`: `ALLOWED_COMMANDS`, `PROJECT_BINS`, `ASK_FIRST`, `INSTALL_LISTED`,
  `DENIED_SHELL` with its wrappers, `denied_shell()` adding git by path), the lane (`writable`:
  the worktree and `ask/`, not the rest of the task directory; `readable`, `SECRETS`,
  `private_paths` (workbench's own state and config), `lane_refusal`, `tool_roots`, on WSL
  `windows_drives`/`agent_path`/`interop_gap` (Windows programs run outside the lane),
  `runtime_dir`, `unix_paths`, `external_rules`), `commands_conf` (commands.json, checked),
  `rules_dir` (the agent's rules, outside its lane), `grants`/`grant` (what Claude allowed the
  task: `rules/<task>/grants.json`).
- `lane.py`: Landlock: the runner starts OpenCode through it (`wrap`); restricts writes, reads,
  connecting to outside UNIX sockets (ABI 9) and signals (ABI 6), sets no-new-privileges (on any
  Linux), execs. Fails closed: Landlock present but refusing the lane exits 126
  (`workbench lane: the kernel refused the lane`); a command it can't exec, `workbench lane: can't
  run <cmd> (<reason>)`, 127 when not found, else 126 (outside the read lane: Permission denied);
  without Landlock (or off `lane.UNIFIED`'s architectures) it runs unrestricted and `wb doctor`
  says so. Standard library only.
- `gitguard.py`: the read-only `git` shim first on the agent's PATH. Standard library only: it's
  imported on every git call.
- `gitops.py`: worktrees and branches, the review diff (intent-to-add, so new files show),
  `mark` (has the worktree changed), commits (no hooks), the repo's stack. Calls on a task's
  worktree are pinned to its recorded git dir (`rules/<task>/gitdir.json`).
- `results.py`: results.jsonl rows (`record`), each step's size and fit (`step`), `wb stats`,
  Claude's spec record (`spec_gap_line`). Read rows through `store.results()`, which leaves out
  trials.
- `router.py`: the model per task size and stack, each model's slice, its time budgets and speeds.
- `tuning.py`: knobs learned per model and window (`KNOBS`; `fresh_at` is the handover point).
  A new knob: add it to `KNOBS`, read it with `tuning.value(task, name)`.
- `models.py`: discovers local models from OpenCode's config, their status, residency
  (`make_resident`, `free_gpu`), GPU sharing (`GpuLease`, `can_share`), adding and retiring them.
- `hardware.py`: OS, GPU/VRAM, RAM, CPU, disk, read fresh for `wb scout` and the GPU budget
  (Linux sysfs and /proc, WSL2, macOS). Unknowns stay unknown; nothing is hard-coded.
- `skills.py`: Claude's skill library for the agents (its own git repo): checks, add/patch/
  retire/restore, import, suggestions for a spec, the outcome record. `scan.py`: the security scan
  every skill passes (ported from Hermes Agent's skills guard: keep its MIT notice).
- `lessons.py`: what each task taught, gathered into lessons.jsonl by feedback/next/resume/accept/discard.
- `transcript.py`: `wb watch` (the session export merged with the task's events; `--json` feeds
  the mod's pane). `transcript.export` is also the runner's view of the session.
- `watch.py`: waiting without polling: inotify (ctypes) and pidfd, with a stat fallback.
- `chat.py` + `chat_static/`: the web chat (stdlib HTTP, a token and a Host check).
- `images.py`: `wb image`, local image engines from `images.json`; holds the GPU.
- `agent/HANDBOOK.md`: Claude's working habits for the agent. Every agent prompt carries it, so
  keep it short: each token costs prefill time.
- `skill/delegate/SKILL.md`: the `/delegate` procedure (`~/.claude/skills/delegate` links here; `wb setup` makes the link).
- `mod/`: the Claude Code plugin (`hooks/register.tsx`, `hooks/apprentice.ts` the pane's
  character, `hooks/register.test.tsx`, `types/index.d.ts`; `.claude-plugin/plugin.json` has its
  own version). The repo root's `.claude-plugin/marketplace.json` makes the repo its marketplace.
- `bin/wb`, `bin/wb-ask-mcp`: launchers that find the package from their own real path.
- `tests/`: `test_workbench.py` (end to end, one class per feature), `test_gpu.py`,
  `test_portable.py`, `test_sizing.py`, `test_docs.py` (the docs cover the code),
  `test_workflows.py` (the merge gate),
  `fake_opencode.py`, `capture_llm.py`, `e2e_chat.mjs`.
- `.github/`: CI (`workflows/check.yml`, the required check, on each push and pull request;
  Python 3.11, the floor; `nightly.yml` on the newest Claude Code, plus macOS; README badges show both), `owner-merge.yml` (approves
  the owner's pull requests only), `rulesets/main.json`, dependabot for the actions' pins.
- `scripts/`: `ship.sh` (the road to main), `github-settings.sh check|apply` (the repo's settings).
- `systemd/wb-chat.service`: keeps the web chat up. `docs/`: the README's images (a replaced image
  gets a new file name, for GitHub's cache).

Runs on Linux, WSL2 and macOS. Native Windows doesn't (fcntl, `os.killpg`, POSIX signals): the
launchers refuse it, and Python before 3.11, before importing the package. Off
Linux there is no Landlock and watch.py falls back to timed waits.

## How a task runs
1. `wb start` refuses a repo without a committed CLAUDE.md or `.claude/CLAUDE.md`
   (`--no-claude-md`), asks the router for a model (unless `--model`), refuses (exit 4) when the
   GPU's holders leave its model no room (`--queue` waits behind them), makes branch
   `delegate/<task>` in a fresh worktree, writes round 1's prompt (the spec plus `PROTOCOL`),
   spawns the runner and returns at once. The runner runs `--test` once on the untouched base
   first (the base check; the first report says what).
2. The runner takes the GPU (`GpuLease`: shared beside other tasks when the models fit, else
   alone), makes the model resident (again before every round), and runs `opencode run
   --standalone --auto --format json … -- <prompt>` in
   the worktree, through `lane.wrap`, with the config layer `rules/<task>/opencode.json`. Events
   go to `events.jsonl`; `status.json` is the live summary.
3. Guards stop the agent (`stuck`): one call repeated 4 times, 6 tool errors in a row, 40 calls
   without an edit, silence past `--silence-timeout`, a round past `--round-timeout`, more than
   `HANDOVER_MAX` handovers in a round. Past the think budget (`--think-timeout`, from the round's
   first output, paused while OpenCode waits on a background job) it gets a nudge; the round after
   it that stalls again is stopped. The guards' clocks are monotonic. Limits stretch for a model
   measured slow (`router.budgets`, written into task.json at start). `wb steer` queues a note;
   `--now` interrupts. Notes a runner never delivered: `wb resume` carries them, feedback and
   next drop them and say so.
4. After a clean round the runner runs `--test`, in the agent's lane and environment; failures go back to the agent up to `--auto-fix`
   times without waking Claude (`--test-timeout`, 900 s). A session that reaches the handover point (`fresh_at`, tuned per
   model) after changing a file, with room left for a successor, is stopped mid-round and a fresh
   session gets the task, the diff and its last words; between rounds, a session over the point
   or compacted is replaced. OpenCode's own compaction is set just past it, as a backstop.
5. The runner writes the round report and a `review`/`stuck`/`failed` notice and exits; nothing
   the agent or the tests started outlives a round (`runner.sweep`). A runner that died without a
   word is failed by `cli.reap` (status, wait, events and the acting commands call it).
   `ask_reviewer` writes a question at any time and blocks until `wb answer` or `wb allow`; the
   runner checks it and raises the `question` (nothing the agent writes is trusted outside it).
6. The mod (or `wb wait`) delivers each notice once, while still true, in the session the task
   is `mine` to (task.json `cwd`/`repo` and the session's directory); acting on a task
   acknowledges it (`_ack_all`, which stops short of news still true).
7. `wb accept` commits with provenance trailers (no AI co-author line), optionally merging;
   `wb next` commits a step and gives the same agent the next; `wb feedback` sends another round;
   `wb discard` drops it; `wb takeover` leaves it to Claude. `results.jsonl` gets a row, which the
   router, the slices, the tuning and the skill records learn from; lessons are gathered.

## State and config
Plain files under `$WB_HOME` (`~/.local/state/workbench`); the full tree and every format are in
ARCHITECTURE.md (*Files and formats*):
```
notify.jsonl results.jsonl lessons.jsonl notes.md tuning.json speed.json gpu.{lock,json,admit,load}
scout.json update.json watch.json imports/ images/ chat/{messages,receipts}.jsonl chat/listener.json
rules/<task>/  opencode.json HANDBOOK.md CLAUDE.md bin/git grants.json gitdir.json (no lane)
locks/<task>.{runner,cmd}.lock   (no lane reads them: one runner, one wb command a task)
tasks/<task>/  task.json status.json events.jsonl steer.jsonl acked context.md base-tests.txt runner.log
               (the lane: worktree/, ask/)
               rounds/<n>/{prompt,instructions,summary}.md rounds/<n>/tests.txt ask/<id>.{json,answer} worktree/
```
The skill library is `~/.local/share/workbench/skills` (`$WB_SKILLS`). `~/.config/workbench/`
holds `models.json` (what OpenCode can't say: `start`/`stop`, `skip`, `sizes`, `vram_gb`,
`weights_gb`, `label`, `engine`, `local`), `commands.json` (standing `allow` patterns, lane
`paths`, and `env` names passed to the agent), `images.json`, `chat.json`, `chat-token`.

A model is a candidate when OpenCode loads its provider (`disabled_providers`/`enabled_providers`
followed) and the provider's `baseURL` (`{env:}`/`{file:}` resolved) is on this machine (loopback,
`localhost`, this host's name) or models.json marks the provider `local: true`. Not an Ollama
cloud model (`:cloud`, `-cloud`, a tags row with `remote_host`) unless models.json marks that
model's id `local: true`, nor an entry with `tool_call: false`. OpenCode's config is
`$XDG_CONFIG_HOME/opencode/opencode.json` and `.jsonc`, layered in that order and merged by
`models.read_config`; written only when one file holds it and it has no comments (through a
symlink, keeping its mode; a refusal prints the change, never the config's keys). Probes to this
machine skip any HTTP proxy. Ollama entries need `options.max_tokens` (Ollama ignores
OpenCode's `max_completion_tokens`) and a `limit.context` matching what Ollama serves (never the
trained length); every entry needs `limit.context` (a server other than Ollama: `wb model add
--context N`); `wb model add` writes both and `wb doctor` checks them.

Environment: `WB_HOME`, `WB_MODELS`, `WB_SKILLS`, `WB_IMAGES`, `WB_CHAT_TOKEN`, `WB_CHAT_CONFIG`,
`WB_COMMANDS` (the paths above); `WB_OPENCODE_CONFIG` (OpenCode's config); these may be relative
or start with `~` (`store.absolute_env` makes them absolute against wb's cwd); `WB_COMPACT_AT` (the
mod's early compaction, a percentage, 0 off); `WB_BIN` (the `wb` the mod runs); `WB_TASK` (set
for the ask server and the agent); `CLAUDE_CODE_SESSION_ID` (Claude Code's: `wb start` records it,
which session's task it is). Tests only: `WB_OPENCODE_BIN` (a command line, split with
`shlex`: the fake), `WB_FAKE_MODELS` (no model servers), `WB_FAKE_GPU_GB` (the fake GPU's size;
unset: unknown, so no sharing).

## Conventions
- Output from `wb` is read by Claude, whose tokens are the scarce resource: short, plain, no
  decoration, one report per command. Raw logs stay in files.
- State changes go through `Task.set_status` (locked read-modify-write) and `append_jsonl`.
  Never write status.json directly.
- Never name a model in code or the skill. Models come from OpenCode's config and choices from
  the router and the weekly model check, so newer models slot in without code changes. (README's
  quickstart, the test fixtures, the real-run instruction above and ARCHITECTURE.md's image setup
  are the only places that name one.)
- The launchers and the runner find the package by path. `-I` drops `PYTHONPATH`, so use it only
  where the path is inserted in code (the git shim) or nothing outside the standard library is
  imported (the lane, run as a file); never rely on `PYTHONPATH` under `-I`.
- Prompt openings are an API: the transcript's labels, `round_asks` and the lessons match them by
  prefix (ARCHITECTURE.md, *The runner, inside*). Reword one and update its readers.
- Comments say why, often with the incident and its date; a regression test cites the incident.
  Test names read as sentences.
- The mod keeps no state of its own that matters: it reads the task files. `$` may only be passed
  to functions declared at the top level of `register.tsx`, so `claude plugin validate` can follow
  it. `$.state` atoms are tagged (`tasks-3`, `live-1`, `chat-2`, `sessions-1`): change the tag when the type
  changes. A slash command may not start a turn itself: it pushes onto `mem.queued`.
- Skills for the agents: Claude's library (`wb skill`), handed per task, or a project's own
  `.claude/skills`, committed with it. The user's personal `~/.claude/skills` are denied to agents.

## OpenCode quirks this code relies on
Checked against OpenCode 2.3 with `tests/capture_llm.py` (a recording model server: point a
provider at it and read the system prompt, tools and tool results). The real-OpenCode tests cover
the MCP shape, skills, denials, compound shell commands, rule order, git by path, the edit rule,
PATH and the lane; the rest is only imitated by the fake.
- It resolves the project from `$PWD`, not from the cwd.
- The 2.x config shape is `mcp.servers.<name>`, with `timeout.{startup,execution}`;
  `codemode: false` exposes a plain tool (listed as `workbench_ask_reviewer`).
- `--standalone` servers inherit the env and share session storage with the background service;
  `session export`/`list` use it too, so no workbench call needs the service's fixed port.
- Project config (`opencode.json` in the worktree, `.opencode/`, or any directory above it) loads
  after `OPENCODE_CONFIG` and its permissions come later, so it could lift every deny;
  `OPENCODE_CONFIG_CONTENT` loads last of all (`OPENCODE_DISABLE_PROJECT_CONFIG` also drops
  AGENTS.md and the project's skills). The user's global MCP servers merge in too, and one whose
  entry says `"enabled": false` still starts: the layer swaps its command for `false`.
- It loads AGENTS.md itself but never CLAUDE.md, which reaches it through `instructions`
  (absolute paths).
- Skills come from `~/.claude/skills` and `.claude/skills`. A `disable-model-invocation` skill is
  not listed. The config's `skills` list adds more: a path to one skill's directory lists that
  skill, a parent directory lists every skill in it, a path to the SKILL.md itself lists nothing,
  so handed skills go in one directory each. Loading one (the `skill` tool, argument `id`) returns
  its body, its base directory and its files, readable from the worktree.
- `permissions: [{action, resource, effect}]`: a `deny` removes a tool or skill from the prompt.
  Patterns are anchored, `*` matches anything (spaces and `/` too), a trailing ` *` is optional,
  and the last matching rule wins; no match is "ask", which `--auto` allows. For `shell` the
  resource is each simple command's raw text in a compound line (`cd x && sudo y` is caught; a
  wrapper isn't unwrapped, so `DENIED_SHELL` lists `env sudo *` and the like itself). For `edit` it is the file path. For `external_directory` it is `<dir>/*` for each
  directory outside the session's, from the file tools' paths and the shell's workdir and `cd`
  targets only (`touch ~/x` passes, which is what Landlock is for).
- Denying `execute` (code mode) turns the `opencode_*` tools into direct tools, so deny
  `opencode_*` too. `opencode_worktree_remove` stays listed whatever you do; the rule still refuses
  the call.
- OpenCode's shell inherits the runner's `PATH`, which is how the git shim gets called first.
- A session is bound to the directory it was created in: continued or `--fork`ed from another
  worktree, the model is still told the first one. So context carries over only within a task's
  own worktree (rounds, `wb next`), never to a new task.
- `session export` lists compactions as messages `{type: "compaction", status, summary}`; an
  assistant message's `tokens` (input + cache read/write + output) is the context the next request
  carries (the between-rounds check). The live `step_finish` tokens (input + cache read + cache
  write + output) drive the mid-round check.
- `--worktree` takes `=name`; the runner makes its own worktrees anyway.
- `run` reads a message that starts with `-` as a flag (usage, exit 1): the prompt goes after `--`.
- `run --format json` prints nothing while a tool runs or the model reads its prompt; a shell
  command past 2 minutes goes to the background and the turn ends with a `background_wait` line.

## Troubleshooting
- `wb doctor`: opencode (2.x) and its config files, git, each model's readiness (BAD when none
  can run), skipped providers and why, an Ollama baseURL without `/v1`, a missing `limit.context`,
  each Ollama entry's `max_tokens` and context, the state directory, the GPU budget and the devices
  it counts, a real lane around a test write, `wb` on PATH, the `/delegate` link (and another
  copy's), whether the mod is listed as installed.
- A model "can't run": `wb models` says why (server down, not pulled, no `start` command), and
  `wb start` adds the next step.
- A task misbehaves: `wb log <t> -v`, `wb watch <t> -v`, `tasks/<t>/runner.log` (the runner's
  output and OpenCode's stderr, including `workbench lane: the kernel refused the lane`).
- The agent can't see `ask_reviewer`: `PWD=<worktree> OPENCODE_CONFIG=<state>/rules/<task>/opencode.json
  opencode mcp list --print-logs`.
- The mod does nothing: `wb` must be on Claude Code's PATH (`WB_BIN` moves only the mod's calls;
  Claude's Bash still runs `wb` by name; the mod toasts once when it can't run `wb events`); a
  task started from another project's directory wakes that session, not this one; `claude --debug`
  shows hook errors.
- A test fails: the task's `runner.log` is under the test's temp `WB_HOME` (deleted at tearDown);
  `wait_phase` prints the status when it times out. An interrupted run can leave detached runners
  and fakes sleeping.
