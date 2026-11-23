# Claude's Workbench

Claude delegates coding tasks to local models in OpenCode and reviews the results. The short name
`workbench` stays on the package, the plugin (`workbench@workbench`), the state and config
directories and the clone path `~/repos/workbench` (which nothing needs).

## Keeping the docs true
ARCHITECTURE.md has every feature, command, flag, format and test, and *Couplings* (what changes
together); README.md stays a pitch and a quickstart. New detail goes in ARCHITECTURE.md, not here.

**A change isn't done until the docs say what the code does, in the same commit** (they once fell
months behind: the 2026-11-16 audit). Before every commit:
1. `grep` both docs for each name the diff adds, renames, removes or changes the meaning of (a
   command, flag, default, constant, guard, key, notice, file, prompt text, variable); fix every hit.
2. Reread the ARCHITECTURE.md section on the behaviour you changed; rewrite what's no longer true.
3. A command or flag: *Every `wb` command*; a key or file: *Files and formats*; a module: *Layout*;
   an environment variable: *State and config*; what Claude does: `skill/workbench/SKILL.md`.
4. `tests/test_docs.py` checks names and this file's size, never defaults or behaviour.

## Commands
```sh
python3 -m unittest                                   # the whole suite, about 25 minutes
python3 -m unittest tests.test_workbench.Context      # one class (.Context.test_…); -k handover by name
claude plugin validate mod && claude plugin test mod  # the mod
PLAYWRIGHT=…/node_modules/playwright node tests/e2e_chat.mjs   # the web chat
wb doctor
```
A skip is not a pass: the real-OpenCode tests skip without `opencode` on PATH, the lane tests
without Landlock, so a change to `context.py`, the config layer, `gitguard.py` or `lane.py` needs
them run where they don't skip.

## Checks before committing
main takes no direct push: commit on a branch or worktree. `scripts/ship.sh` pushes, opens the
PR and arms auto-merge as you, so run it only when the user has said to ship, in the background
(it waits for the merge: ARCHITECTURE.md, *Shipping to main*). Never push, open a PR or an issue,
or comment without the user's yes for it.
- The docs match the change; `python3 -m unittest`; the mod's two commands.
- Versions are cut by hand: the last number runs .1, .2, .3 at most, then the next minor (1.91,
  1.91.1, 1.91.2, 1.91.3, then 1.92, never 1.91.4); 2.0 only for a change big enough.
- `chat.py`, `webpush.py`, `chat_static/`: the browser test too (Playwright isn't a dependency).
- How OpenCode is invoked or read (argv, event or export fields): `tests/fake_opencode.py` in the
  same commit, and one real run, `wb start --trial` (steps: ARCHITECTURE.md, *The tests*).

## Layout
`workbench/` is the Python package: standard library only, 3.11+ (CI's floor; developed on 3.14).
No dependencies, no packaging: it runs from the clone, on Linux, WSL2 and macOS (Landlock only on
Linux). How each part works: ARCHITECTURE.md, from *The pieces*.
- `store.py`: task files, atomic JSON, locked JSONL, `Task`. Every other part talks through them.
- `cli.py`: `wb` and `PROTOCOL` (the agent's rules). `runner.py`: the detached per-task driver.
  `events.py`: OpenCode's events, the stuck detector. `ask_mcp.py`: `ask_reviewer`, both sides.
- `context.py`: what the agent knows and may do (instructions, denies, the shell allowlist, the
  lane's paths, grants). `lane.py`: the Landlock lane. `gitguard.py`: the agent's read-only `git`.
  Neither imports the rest of the package: the lane runs as a file, the shim on every git call.
- `gitops.py`: worktrees, the review diff, commits (no hooks); a call on a task's worktree is
  pinned to its git dir (`gitdir.json`).
- `results.py`: results.jsonl rows; read them through `store.results()`, which leaves out trials.
- `router.py`: the model per size, slices, budgets. `tuning.py`: learned knobs (a new one goes in
  `KNOBS`, read with `tuning.value`). `models.py`: discovery, residency, `GpuLease`.
  `hardware.py`: read fresh, unknowns stay unknown, nothing hard-coded.
- `skills.py`: the agents' skill library; `scan.py`, its scan (from Hermes Agent's: keep its MIT
  notice).
- `spend.py`, `savings.py`: a task's cost to Claude, and against writing it. `bench.py`,
  `verify.py`, `optimize.py`, `top.py`, `images.py`, `audio.py` (`media.py`, what those two
  share): their `wb` commands. `progress.py`, `stats.py`, `lessons.py`: progress checks, the Stats
  tab, lessons.jsonl. `transcript.py`: `wb watch`, and the runner's session `export`. `watch.py`:
  inotify and pidfd waits. `chat.py` + `chat_static/`, `webpush.py`: the web chat. `try/`:
  `wb try`'s sample.
- `agent/HANDBOOK.md`: in every agent prompt, so keep it short (prefill time).
- `skill/workbench/`: the `/workbench` procedure. `mod/`: the plugin (`hooks/register.tsx`;
  `plugin.json` has no version, so installs track main). `bin/`: the launchers. `tests/`:
  ARCHITECTURE.md's *The tests*. `.github/`, `scripts/`: CI and `ship.sh` (*Shipping to main*).
  `systemd/`, `launchd/`: keep the chat up. `docs/`: images (a replaced one gets a new name, for
  GitHub's cache), the release checklist and notes.

## How a task runs
Each step in full: ARCHITECTURE.md, *A task, start to finish*. What a change must not break:
1. `wb start` checks (a committed CLAUDE.md, the model, GPU room: exit 4 unless `--queue`),
   writes round 1's prompt (spec plus `PROTOCOL`), spawns the runner and returns at once:
   anything slow (checkout, setup, base check) is the runner's, as Claude's Bash stops at 2 min.
2. The runner holds the GPU (`GpuLease`), loads the model before every round, and runs
   `opencode run … -- <prompt>` in the worktree through `lane.wrap`, with the layer
   `rules/<task>/opencode.json`. Guards stop a stuck agent; `wb steer` queues a note.
3. `--test` runs in the agent's lane, environment and shell (its code gains no rights when
   tested); failures go back up to `--auto-fix` times without waking Claude. At `fresh_at` a
   fresh session takes over; OpenCode's compaction is a backstop just past it, never ahead.
4. The runner writes the report and a `review`/`stuck`/`failed` notice; nothing a round started
   outlives it (`runner.sweep`); `cli.reap` fails a runner that died silently. The runner checks
   and raises `ask_reviewer` questions: nothing the agent writes is trusted outside it.
5. The mod (or `wb wait`) delivers each notice once, while still true, to the session the task
   is `mine` to; acting on a task acknowledges it (`_ack_all`).
6. `wb accept` commits with provenance trailers, never an AI co-author line; its `results.jsonl`
   row is what the router, slices, tuning and skill records learn from.

## Troubleshooting
- `wb doctor` (the install), `wb models` (a model that can't run).
- A task: `wb log <t> -v`, `wb watch <t> -v`, `tasks/<t>/runner.log` (OpenCode's stderr, lane refusals).
- No `ask_reviewer`: `PWD=<worktree> OPENCODE_CONFIG=<state>/rules/<task>/opencode.json opencode
  mcp list --print-logs`.
- The mod silent: `wb` on Claude Code's PATH (`WB_BIN` moves only the mod's calls); a task wakes
  the live session that started it; `claude --debug`.
- A test: `runner.log` is under its temp `WB_HOME`; an interrupted run can leave runners and fakes.

## State and config
Plain files under `$WB_HOME` (`~/.local/state/workbench`); config in `~/.config/workbench/`; the
skill library in `$WB_SKILLS` (`~/.local/share/workbench/skills`, its own git repo). Every file,
key and format: ARCHITECTURE.md, *Files and formats*. Inside `$WB_HOME` the agent's lane
writes only `tasks/<task>/worktree/` and `ask/` (the rest of its lane: *What the agent knows*); `rules/<task>/` and `locks/` (`store.task_lock`) stay outside
it (an agent once held the runner's lock: the 0.90 review).

OpenCode's config is written only through `models.own_config`: one unlayered file without
comments, and a refusal names the change, never the config (it can hold an `apiKey`). Requests to
this machine go through `models._open` (no HTTP proxy). Any OpenAI-compatible server works (`wb
model add --url`): never steer a user to one. Which models count and what an entry needs:
ARCHITECTURE.md, *Models: found, loaded, chosen*.

Environment (each in ARCHITECTURE.md): paths `WB_HOME`, `WB_MODELS`, `WB_SKILLS`, `WB_IMAGES`,
`WB_AUDIO`, `WB_COMMANDS`, `WB_CHAT_TOKEN`, `WB_CHAT_CONFIG`, `WB_SAVINGS`, `WB_GPU_DIR`,
`WB_OPENCODE_CONFIG`, `CLAUDE_CONFIG_DIR` (relative or `~` allowed: a new one joins
`store.PATH_VARS`); the mod's `WB_BIN`, `WB_COMPACT_AT`; the agent's `WB_TASK`, `WB_RUN`,
`WB_USER_PYTHONPATH`; `wb shot`'s `WB_SHOT_OUT`, `WB_SHOT_VIEWPORT`; Claude Code's
`CLAUDE_CODE_SESSION_ID`, `CLAUDE_PID` (whose task it is). Tests only: `WB_OPENCODE_BIN`,
`WB_FAKE_MODELS`, `WB_FAKE_GPU_GB`, `WB_TEST_SHARD`, `WB_TEST_HANG_SECS`.

## Conventions
- `wb` output is read by Claude, whose tokens are the scarce resource: short, plain, one report
  per command. Raw logs stay in files.
- State changes go through `Task.set_status` and `append_jsonl`; never write status.json directly.
- Never name a model in code or the skill: models come from OpenCode's config, choices from the
  router and the weekly model check (only test fixtures and ARCHITECTURE.md's image and audio
  setup name one).
- `-I` drops `PYTHONPATH`: use it only where the path is inserted in code (the git shim) or
  nothing outside the standard library is imported (the lane).
- Prompt openings are an API, matched by prefix (ARCHITECTURE.md, *The runner, inside*): reword
  one, update its readers.
- Comments say why, with the incident and its date; a regression test cites it; test names read
  as sentences.
- The mod keeps no state that matters: it reads the task files. `$` goes only to functions
  declared at the top level of `register.tsx`; a `$.state` atom whose type changes gets a new
  shape tag (*Couplings*); a slash command never starts a turn: it pushes onto `mem.queued`.

## OpenCode quirks this code relies on
Checked against OpenCode 2.3 by the real-OpenCode tests (`tests/capture_llm.py`; which covers
what: ARCHITECTURE.md, *The tests*; the detail: *The runner, inside*, *What the agent knows*).
- The project comes from `$PWD`, not the cwd. The prompt goes after `--` (one starting with `-`
  is read as a flag).
- A project's config loads after the layer and could lift every deny, so
  `OPENCODE_DISABLE_PROJECT_CONFIG` is always on (both names: `PROJECT_CONFIG_OFF`); it drops the
  project's AGENTS.md, skills and `instructions` too, which wb hands back in the layer.
- Rules are `permissions: [{action, resource, effect}]` (`shell`: each simple command's raw text;
  `edit`: the file path; `external_directory`: `<dir>/*`), anchored, `*` matches spaces and `/`, the last match wins, no match is ask (`--auto`
  allows it); `shell` sees each simple command's raw text, wrappers not unwrapped (`DENIED_SHELL`).
  Denying `execute` makes the `opencode_*` tools direct: deny them too.
- A session is bound to the directory it was made in: context carries over within a task's
  worktree, never to a new task.
- The user's global MCP servers start even when `"enabled": false`, and a provider's
  `headerTimeout`/`chunkTimeout` cut a slow request: the layer undoes both.
