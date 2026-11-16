# workbench

Claude delegates coding tasks to local models in OpenCode and reviews the results. README.md
has the architecture; read it first.

## Layout
- `workbench/` is the Python package, standard library only (Python 3.11+, developed on 3.14). Do not add dependencies.
  - `store.py`: task directories, atomic JSON, locked JSONL. Every other part talks through these files.
  - `runner.py`: the detached per-task driver. `cli.py`: `wb`. `ask_mcp.py`: the MCP tool server.
  - `models.py`: the model registry and readiness checks. `events.py`: event normalization and the stuck detector.
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

## Checks before committing
```sh
python3 -m unittest            # ~12 s, real git, real runner processes, fake opencode
claude plugin validate mod && claude plugin test mod
```
A change to how OpenCode is invoked also needs one real run: `wb start` on MiMo against a
scratch repo, as the README describes.
