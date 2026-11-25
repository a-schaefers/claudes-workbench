<p align="center"><img src="docs/logo.jpg" alt="Claude's Workbench: a warm light directing three little robot craftsmen at a workbench" width="720"></p>

# Claude's Workbench

**Claude orchestrates. OpenCode agents via API or GPU write the code.** Workbench is a
Claude Code plugin that connects the two inside your normal Claude Code session.

<p align="center">
<a href="https://github.com/a-schaefers/workbench/actions/workflows/check.yml"><img alt="tests" src="https://img.shields.io/github/actions/workflow/status/a-schaefers/workbench/check.yml?branch=main&label=tests"></a>
<a href="https://github.com/a-schaefers/workbench/actions/workflows/nightly.yml"><img alt="newest Claude Code" src="https://img.shields.io/github/actions/workflow/status/a-schaefers/workbench/nightly.yml?branch=main&label=newest%20Claude%20Code"></a>
<a href="https://github.com/a-schaefers/workbench/actions/workflows/opencode.yml"><img alt="newest OpenCode" src="https://img.shields.io/github/actions/workflow/status/a-schaefers/workbench/opencode.yml?branch=main&label=newest%20OpenCode"></a>
<a href="https://github.com/a-schaefers/workbench/releases"><img alt="release" src="https://img.shields.io/github/v/release/a-schaefers/workbench?label=release&color=blue"></a>
<a href="#what-you-need"><img alt="Python, standard library only" src="https://img.shields.io/badge/Python-stdlib%20only-3776ab"></a>
<a href="#what-you-need"><img alt="Linux, macOS, WSL2" src="https://img.shields.io/badge/runs%20on-Linux%20%C2%B7%20macOS%20%C2%B7%20WSL2-555"></a>
<a href="LICENSE"><img alt="MIT license" src="https://img.shields.io/github/license/a-schaefers/workbench"></a>
<a href="https://github.com/a-schaefers/workbench/commits/main"><img alt="last commit" src="https://img.shields.io/github/last-commit/a-schaefers/workbench"></a>
</p>

- **Claude is the lead, your OpenCode agents are the crew:** Claude assigns each step to a model
  sized for it, reviews what comes back, and returns it until it's right.
- **OpenCode agents get Claude's know-how:** a library of skills Claude keeps and grows, handed out
  per task.
- **It's still just Claude Code.** You talk to Claude as usual, your plugins, skills and settings
  keep working, and the agents work on their own branches, so your checkout is untouched until
  you accept.
- **Beyond code: images and audio.** Optionally, Claude directs local image and audio models
  for a project's icons, illustrations, voice lines, sound effects and music.

On bigger coding tasks, once the session is already open, handing the coding off to the
agents typically costs Claude about half the tokens of writing the code itself.

## Quickstart

### What you need

- [Claude Code](https://claude.com/claude-code), with a Claude subscription or an API key
- [OpenCode](https://opencode.ai) 2.3 or newer: `curl -fsSL https://opencode.ai/v2/install | bash`
  (or `opencode upgrade --major` from 1.x; npm's `opencode-ai` and GitHub's Latest release are 1.x)
- Git 2.34 and Python 3.11 or newer, on Linux, macOS, or Windows through WSL2
- A model: one you connected in OpenCode (`/connect`), or a local one. **Already run a server**
  that speaks the OpenAI API? Claude uses it (tool calls and a 32K context window recommended,
  16K at the least). **None yet?** Claude suggests one that fits your hardware and asks before
  installing or downloading anything.
- (optional) An image or audio model, for pictures and sound. The first time a project needs one,
  Claude proposes a model that fits your machine and your use's licence, and asks before
  downloading it. Small speech models run on the CPU; music wants a GPU.

Tested in the Claude Code CLI only, not the Claude desktop app.

### In your terminal

```sh
claude plugin marketplace add a-schaefers/workbench
claude plugin install workbench@workbench
"${CLAUDE_CONFIG_DIR:-$HOME/.claude}"/plugins/marketplaces/workbench/bin/wb setup   # puts wb on your PATH and /workbench in your skills
```

### In Claude Code, inside your project

```
/workbench
```

The first time, Claude finishes the setup and proves it on a tiny sample task.

Then work as you always do. On the left is Claude, as usual; on the right, the agents it's
directing for you.

`/workbench-quit` stops the agents and keeps their work for later.
`/workbench-quit-and-hand-back-claude` has Claude finish their tasks itself.
`/workbench-update` installs the latest workbench (Claude Code tells you when there's one).

## Contributing

Fixes are welcome: [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT.
