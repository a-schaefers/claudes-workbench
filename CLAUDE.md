# Claude's Workbench

Claude delegates coding tasks to models in OpenCode (GPU or API) and reviews the results. The short name
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
- `cli.py`: `wb` and `PROTOCOL` (the agent's rules; `WRITING_PROTOCOL`, the writing role's, same fold). `runner.py`: the detached per-task driver,
  and OpenCode's environment (`opencode_env`: the task's home, the shell shim).
  `events.py`: OpenCode's events, the stuck detector. `ask_mcp.py`: `ask_reviewer`, both sides.
- `context.py`: what the agent knows and may do (instructions: the user's are `USER_AGENTS_MD`,
  `~/.config/workbench/AGENTS.md`, never Claude's `~/.claude` files, #1061; denies, the shell allowlist, the
  lane's paths: `writable`, the worktree, `ask/` and the task's `opencode/`, `opencode_home`;
  `API_HIDDEN`, the token files an API task's lane hides too when they hold a credential; grants). `lane.py`: the Landlock lane. `gitguard.py`: the agent's read-only `git`.
  Neither imports the rest of the package: the lane runs as a file, the shim on every git call.
- `keyhole.py`: the per-task key proxy, outside the lane: only the task's model, the key swapped
  in, errors redacted (ARCHITECTURE.md, *The keyhole*); `runner.drive` starts it for an API task,
  and `keyhole_overlay` goes in both the layer and `rules_env`'s restatement.
- `gitops.py`: worktrees, the review diff, commits (no hooks); a call on a task's worktree is
  pinned to its git dir (`gitdir.json`).
- `patches.py`: `wb patch`, the user's own fixes to workbench (ARCHITECTURE.md, *Local patches*):
  commits on branch `local` of a clone they own, rebased by `wb update` in a scratch worktree,
  never the live tree; a marketplace clone's changes saved before Claude Code's re-clone wipes
  them; `wb patch send`, the one fork, push and pull request (through `gh`, never a token
  printed; under Claude Code only on the mod's dialog's yes). Its git calls run without hooks or a terminal, and it imports everything up front:
  `wb patch init` deletes the copy wb runs from.
- `privacy.py`: where an API task's code goes (`where`, `PRIVACY_PAGES`, the first-task note),
  `local_only`, OpenRouter's routing, `holds_credential` (which token files an API lane hides) (ARCHITECTURE.md, *Privacy for API roles*).
- `results.py`: results.jsonl rows (`kind`, `role`, `provider`; an API task's money, `api_usage`); read them through `store.results()`, which leaves out trials.
- `router.py`: the model per size, slices, budgets. `tuning.py`: learned knobs (a new one goes in
  `KNOBS`, read with `tuning.value`). `models.py`: discovery (`discover`, GPU only; API models from
  OpenCode's catalog, `hosted`, `lookup`, `note_api`, `catalog_doc`), residency, `GpuLease`, and
  `lease_for`: an API task's `NoLease` (provider slots, never the GPU). `routes.py`: the
  roles (`significant`, `trivial`, `writing`: GPU or API, each side's model, thinking; the day cap)
  in routes.json, the change line `wb route` prints and the mod's dialog shows, and the dialog's
  one-shot approval (`approved`); `use`, the Agents switch (`effective_via`); `busy`, the busy-GPU
  opt-in (`busy_api`); `resolve`, wb start's model.
  `hardware.py`: read fresh, unknowns stay unknown, nothing hard-coded.
- `skills.py`: the agents' skill library; `scan.py`, its scan (from Hermes Agent's: keep its MIT
  notice).
- `spend.py`, `savings.py`: a task's cost to Claude, and against writing it (the score GPU-only; API dollars apart, `api_totals`). `meter.py`: an API
  task's money (the ledger `spend-api.jsonl`, the day cap, the `spend` guard: *Spend*). `bench.py`,
  `verify.py`, `optimize.py`, `top.py` (API tasks under RUNNING, never in rack or queue), `images.py`, `audio.py` (`media.py`, what those two
  share): their `wb` commands. `progress.py`, `stats.py`, `lessons.py`: progress checks, the Stats
  tab, lessons.jsonl. `transcript.py`: `wb watch`, and the runner's session `export`. `watch.py`:
  inotify and pidfd waits. `chat.py` + `chat_static/`, `webpush.py`: the web chat. `try/`:
  `wb try`'s sample.
- `agent/HANDBOOK.md`: in every agent prompt, so keep it short (prefill time). `agent/WRITING.md`:
  the writing role's, in its place in every writing prompt: keep it short too (under 3,000 bytes).
- `skill/workbench/`: the `/workbench` procedure; `skill/agents/`: other agents' skill for the chat's API. `mod/`: the plugin (`hooks/register.tsx`, with
  the consent gates on sends to upstream and on the roles; `plugin.json` has no version, so installs track main).
  `bin/`: the launchers. `tests/`: ARCHITECTURE.md's *The tests*. `.github/`, `scripts/`: CI and `ship.sh` (*Shipping to main*).
  `systemd/`, `launchd/`: keep the chat up. `docs/`: images (a replaced one gets a new name, for
  GitHub's cache), the release checklist and notes, `chat-api.md` (the chat's HTTP API).

## How a task runs
Each step in full: ARCHITECTURE.md, *A task, start to finish*. What a change must not break:
1. `wb start` resolves the role (`--role`, else from `--size`; unset: GPU): GPU through the router, API to the
   role's model, never the other side (exit 3). It checks (a committed CLAUDE.md, the model, GPU room, or for an API model a free
   provider slot of `API_PARALLEL`: exit 4 unless `--queue`),
   writes round 1's prompt (spec plus `PROTOCOL`), spawns the runner and returns at once:
   anything slow (checkout, setup, base check) is the runner's, as Claude's Bash stops at 2 min.
2. The runner holds the GPU (`GpuLease`) and loads the model before every round, or for an API
   model holds a provider slot (`NoLease`, never in gpu.json) and starts the keyhole (the key stays
   there, outside the lane); it runs
   `opencode run … -- <prompt>` in the worktree through `lane.wrap`, with the layer
   `rules/<task>/opencode.json`. Guards stop a stuck agent (an API task at the day cap too:
   `spend`, in the round and before each next one); an API provider's rate limit,
   outage or queue pauses their clocks, and held 10 minutes stops the round as `rate_limited`
   (or `outage`), the provider's and never silence, run again once; `wb steer` queues a note.
3. `--test` runs in the agent's lane, environment and shell (its code gains no rights when
   tested); failures go back up to `--auto-fix` times (2, on API 1) without waking Claude. At `fresh_at` a
   fresh session takes over; OpenCode's compaction is a backstop just past it, never ahead.
4. The runner writes the report and a `review`/`stuck`/`failed` notice; nothing a round started
   outlives it (`runner.sweep`); `cli.reap` fails a runner that died silently. The runner checks
   and raises `ask_reviewer` questions: nothing the agent writes is trusted outside it.
5. The mod (or `wb wait`) delivers each notice once, while still true, to the session the task
   is `mine` to; acting on a task acknowledges it (`_ack_all`).
6. `wb accept` commits with provenance trailers, never an AI co-author line; its `results.jsonl`
   row is what the router, slices, tuning and skill records learn from.

## Troubleshooting
- `wb doctor` (the install), `wb models` (a model that can't run), `wb route` (an API role's).
- A task: `wb log <t> -v`, `wb watch <t> -v`, `tasks/<t>/runner.log` (OpenCode's stderr, lane refusals).
- No `ask_reviewer`: `PWD=<worktree> OPENCODE_CONFIG=<state>/rules/<task>/opencode.json opencode
  mcp list --print-logs`.
- The mod silent: `wb` on Claude Code's PATH (`WB_BIN` moves only the mod's calls); a task wakes
  the live session that started it; `claude --debug`.
- A test: `runner.log` is under its temp `WB_HOME`; an interrupted run can leave runners and fakes.

## State and config
Plain files under `$WB_HOME` (`~/.local/state/workbench`); config in `~/.config/workbench/` (its
`AGENTS.md`: the user's instructions for every agent, copied to `rules/<task>/`); the
skill library in `$WB_SKILLS` (`~/.local/share/workbench/skills`, its own git repo). Every file,
key and format: ARCHITECTURE.md, *Files and formats*. commands.json `repos.<path>.local_only: true`
keeps API models off a repo; only the user lifts it. Inside `$WB_HOME` the agent's lane
writes only `tasks/<task>/worktree/`, `ask/` and `opencode/` (its OpenCode's own home; the rest of
its lane: *What the agent knows*); `rules/<task>/` (with the shell shim, `shell/<name>`, and an
API task's `catalog.json`, its `OPENCODE_MODELS_PATH`), `api/` (the provider slots) and
`locks/` (`store.task_lock`) stay outside it (an agent once held the runner's lock: the 0.90 review). `wb patch` keeps `patches.json` (the
clone install and its upstream, which the mod reads; the pull requests it sent), `send-ok.json` (the mod's yes to one send),
`rescued/`, `upstream/` and `patches/` there; `route-ok.json` is the dialog's yes to one role change
(`wb route`), or an API `--model` start. `~/.config/workbench/routes.json` (`$WB_ROUTES`) holds the roles: missing
is all GPU, unreadable is an error (never read as all GPU). API spend: `spend-api.jsonl`
(`meter.py`), against routes.json's `day_usd`.

OpenCode's config is written only through `models.own_config`: one unlayered file without
comments, and a refusal names the change, never the config (it can hold an `apiKey`). Requests to
this machine go through `models._open` (no HTTP proxy). Any OpenAI-compatible server works (`wb
model add --url`): never steer a user to one. Which models count and what an entry needs:
ARCHITECTURE.md, *Models: found, loaded, chosen*. GPU paths and the router read `discover()` only;
API models (Ollama cloud ones too) are `hosted()`'s, cached in `hosted.json`, named by exact id.

Environment (each in ARCHITECTURE.md): paths `WB_HOME`, `WB_MODELS`, `WB_SKILLS`, `WB_IMAGES`,
`WB_AUDIO`, `WB_COMMANDS`, `WB_CHAT_TOKEN`, `WB_CHAT_CONFIG`, `WB_SAVINGS`, `WB_GPU_DIR`,
`WB_OPENCODE_CONFIG`, `CLAUDE_CONFIG_DIR`, `WB_SOURCE` (`wb patch init`'s clone, else
`~/.local/share/workbench/source`), `WB_ROUTES` (the roles; `wb start`'s local-only check reads it) (relative or `~` allowed: a new
one joins `store.PATH_VARS`); the mod's `WB_BIN`, `WB_COMPACT_AT`; the agent's `WB_TASK`, `WB_RUN`,
`WB_USER_PYTHONPATH`; `wb shot`'s `WB_SHOT_OUT`, `WB_SHOT_VIEWPORT`; Claude Code's
`CLAUDE_CODE_SESSION_ID`, `CLAUDE_PID` (whose task it is), `CLAUDECODE` (under it `wb patch send`
sends and `wb route` changes the API side only on the mod's dialog's yes: `send-ok.json`,
`route-ok.json`; `wb start --model` on an API model the same, `cli.start_gate`). Tests only: `WB_OPENCODE_BIN`,
`WB_FAKE_MODELS`, `WB_FAKE_GPU_GB`, `WB_FAKE_CATALOG` (the fake's OpenCode catalog), `WB_FAKE_API`
(an API model's state in the fake: `nokey`, `auth`, `quota`, `ratelimit`, `down`), `WB_TEST_SHARD`,
`WB_TEST_HANG_SECS`.

## Conventions
- `wb` output is read by Claude, whose tokens are the scarce resource: short, plain, one report
  per command. Raw logs stay in files.
- State changes go through `Task.set_status` and `append_jsonl`; never write status.json directly.
- Never name a model in code or the skill: models come from OpenCode's config, choices from the
  router and the weekly model check (only test fixtures and ARCHITECTURE.md's image and audio
  setup name one); API ids from the user's roles and OpenCode's catalog, never from code.
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
- An API model's limits, cost and variants come from the catalog (`opencode api model.list`);
  `OPENCODE_MODELS_PATH` replaces it: the agent's is the task's `catalog.json`, the fetch off.
- Cost is OpenCode's estimate from the catalog's prices, not the bill: `step_finish.part.cost`
  live, `info.cost` in the export (the title request included).
- A project's config loads after the layer and could lift every deny, so
  `OPENCODE_DISABLE_PROJECT_CONFIG` is always on (both names: `PROJECT_CONFIG_OFF`); it drops the
  project's AGENTS.md, skills and `instructions` too, which wb hands back in the layer.
- OpenCode reads `~/.claude/CLAUDE.md` itself unless `OPENCODE_DISABLE_CLAUDE_CODE_PROMPT`
  (`runner.OPENCODE_ENV`): only the lane hid it, so with no lane it reached agents (#1061).
- Rules are `permissions: [{action, resource, effect}]` (`shell`: each simple command's raw text;
  `edit`: the file path; `external_directory`: `<dir>/*`), anchored, `*` matches spaces and `/`, the last match wins, no match is ask (`--auto`
  allows it); `shell` sees each simple command's raw text, wrappers not unwrapped (`DENIED_SHELL`).
  Denying `execute` makes the `opencode_*` tools direct: deny them too.
- A session is bound to the directory it was made in: context carries over within a task's
  worktree, never to a new task.
- `OPENCODE_DB` moves only the db (the data dir's `auth.json` is still imported into it);
  `XDG_DATA_HOME` moves the whole data dir (`opencode debug paths`), so agents get the task's.
  It picks a command's flags by `$SHELL`'s name (zsh's `+o nomatch`): the shim bears its shell's.
- The user's global MCP servers start even when `"enabled": false`, and a provider's
  `headerTimeout`/`chunkTimeout` cut a slow request: the layer undoes both (an API provider's
  header wait is 600 s, not none).
- `-m <provider>/<id>#<variant>` picks a variant (the provider ends at the first `/`); an unknown
  one is a resolution error, exit 1 before any request. A V1 layer variant on a catalog package
  keeps only its settings (an npm package sends V1 keys in the body), so a body field there
  (DeepSeek's `thinking`) needs V2 `providers`, and one provider in both forms loses its V1 entry
  (`runner._as_v2` moves it whole; V2 says `package`, not `npm`). `small_model` takes `#<variant>`.
  The title request to a catalog Anthropic or DeepSeek model sends `thinking: disabled` by itself:
  check a variant on the request with tools.
