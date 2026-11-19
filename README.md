<p align="center"><img src="docs/logo.jpg" alt="Claude's Workbench: a warm light directing three little robot craftsmen at a workbench" width="720"></p>

# Claude's Workbench

**Claude orchestrates. An OpenCode local LLM on your GPU writes the code.** Workbench is a
Claude Code plugin that connects the two inside your normal Claude Code session.

<p align="center">
<a href="https://github.com/a-schaefers/claudes-workbench/actions/workflows/check.yml"><img alt="tests" src="https://img.shields.io/github/actions/workflow/status/a-schaefers/claudes-workbench/check.yml?branch=main&label=tests"></a>
<a href="https://github.com/a-schaefers/claudes-workbench/actions/workflows/nightly.yml"><img alt="newest Claude Code" src="https://img.shields.io/github/actions/workflow/status/a-schaefers/claudes-workbench/nightly.yml?branch=main&label=newest%20Claude%20Code"></a>
<a href="https://github.com/a-schaefers/claudes-workbench/actions/workflows/opencode.yml"><img alt="newest OpenCode" src="https://img.shields.io/github/actions/workflow/status/a-schaefers/claudes-workbench/opencode.yml?branch=main&label=newest%20OpenCode"></a>
<a href="https://github.com/a-schaefers/claudes-workbench/releases"><img alt="release" src="https://img.shields.io/github/v/release/a-schaefers/claudes-workbench?label=release&color=blue"></a>
<a href="#what-you-need"><img alt="Python, standard library only" src="https://img.shields.io/badge/Python-stdlib%20only-3776ab"></a>
<a href="#what-you-need"><img alt="Linux, macOS, WSL2" src="https://img.shields.io/badge/runs%20on-Linux%20%C2%B7%20macOS%20%C2%B7%20WSL2-555"></a>
<a href="LICENSE"><img alt="MIT license" src="https://img.shields.io/github/license/a-schaefers/claudes-workbench"></a>
<a href="https://github.com/a-schaefers/claudes-workbench/commits/main"><img alt="last commit" src="https://img.shields.io/github/last-commit/a-schaefers/claudes-workbench"></a>
</p>

- **Claude reads every diff before accepting it**, and sends it back with exact notes until it's
  right.
- **Pieces sized to your models:** chores to a small fast model, real work to the strongest one
  your GPU runs.
- **Recurring corrections become instructions** for future tasks
  ([the skill library](ARCHITECTURE.md#the-skill-library)).
- **Your repo stays yours:** each agent works in its own git worktree, and your other plugins,
  skills and settings keep working.

## Quickstart

### What you need

- [Claude Code](https://claude.com/claude-code), with a Claude subscription or an API key
- [OpenCode](https://opencode.ai) 2.x
- Git and Python 3.11 or newer, on Linux, macOS, or Windows through WSL2
- A local model. **Already run a server** that speaks the OpenAI API? Claude uses it (tool calls
  and a 16K context window at least). **None yet?** Claude suggests one that fits your hardware and
  asks before installing or downloading anything.

### In your terminal

```sh
claude plugin marketplace add a-schaefers/claudes-workbench
claude plugin install workbench@workbench
~/.claude/plugins/marketplaces/workbench/bin/wb setup   # puts wb on your PATH and /workbench in your skills
```

`wb setup` ends with a checklist of what's still missing.

### In Claude Code, inside your project

```
/workbench
```

The first time, Claude finishes the setup and proves it on a tiny sample task.

Then work as you always do. On the left is Claude, as usual; on the right, the local agents it's
directing for you.

<p align="center"><img src="docs/first-task.png" alt="Claude Code on the left; on the right, the Workbench pane with a local agent at work." width="900"></p>

`/workbench-quit` stops the agents and keeps their work for later.
`/workbench-quit-and-hand-back-claude` has Claude finish their tasks itself.
`/workbench-update` installs the latest workbench (Claude Code tells you when there's one).

## License

MIT.
