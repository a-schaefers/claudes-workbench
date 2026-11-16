# workbench

Claude delegates coding tasks to local models in OpenCode and reviews the results. README.md
has the architecture; read it first.

## Layout
- `workbench/` is the Python package, standard library only (Python 3.11+, developed on 3.14). Do not add dependencies.
  - `store.py`: task directories, atomic JSON, locked JSONL. Every other part talks through these files.
  - `runner.py`: the detached per-task driver. `cli.py`: `wb`. `ask_mcp.py`: the MCP tool server.
  - `models.py`: the model registry and readiness checks. `events.py`: event normalization and the stuck detector.
  - `context.py`: what the agent knows. It adds CLAUDE.md files and rules, the nested-instructions map and the handbook to the config layer's `instructions`, and it sets the denied tools and skills.
- `agent/HANDBOOK.md`: Claude's working habits for the local agent. Every agent prompt carries it,
  so keep it short: on Strata each token costs prefill time (~38 tok/s).
- `bin/wb`, `bin/wb-ask-mcp`: launchers that find the package from their own real path. Never
  use `python -I` with `PYTHONPATH`, because `-I` drops it.
- `mod/`: the Claude Code mod (TypeScript hooks module). `$` may only be passed to functions
  declared at the top level of `register.tsx`, so `claude plugin validate` can follow it.
- `skill/delegate/SKILL.md`: the `/delegate` procedure. `~/.claude/skills/delegate` links here.

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
    Denying `execute` (code mode) turns the `opencode_*` tools into direct tools, so deny `opencode_*` too.
    `opencode_worktree_remove` stays listed whatever you do; the rule still refuses the call.
- To see exactly what OpenCode sends a model, point a provider at `tests/capture_llm.py`, which records
  each request, and inspect the system prompt and tools. That is how the facts above were found.

## Checks before committing
```sh
python3 -m unittest            # ~15 s: real git, real runners, fake opencode, plus one real-opencode check
claude plugin validate mod && claude plugin test mod
```
A change to how OpenCode is invoked also needs one real run: `wb start` on MiMo against a
scratch repo, as the README describes.
