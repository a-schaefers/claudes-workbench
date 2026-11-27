<p align="center"><img src="docs/logo.jpg" alt="Workbench: a warm light directing three little robot craftsmen at a workbench" width="720"></p>

# Workbench

Workbench is a harness for OpenCode and Claude Code. The model you talk to plans and reviews the work, and OpenCode agents via API or GPU write the code.

<p align="center"><a href="https://github.com/sponsors/a-schaefers"><img alt="Sponsor me on GitHub" src="https://img.shields.io/badge/Sponsor%20me-on%20GitHub-ea4aaa?style=for-the-badge&logo=githubsponsors&logoColor=white"></a></p>

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

You describe a change to the model you talk to (the driver), which splits it into steps, writes a
test for each one, and hands each step to an agent running a cheaper model on your GPU or an API.
Every agent works on its own branch, in its own copy of the project. After each attempt the step's
test runs; if it fails, the agent gets the failure and tries again without involving the driver.
When the test passes, the driver reviews the change and reports to you. Small models do well on
short, tested steps, so each step is sized to the model that writes it
([why](ARCHITECTURE.md#why-its-built-this-way)).

## What you need

- [OpenCode](https://opencode.ai) 2.3 or newer, even if you drive from Claude Code, because the
  agents run in it: `curl -fsSL https://opencode.ai/v2/install | bash`
- Git 2.34 and Python 3.11 or newer, on Linux, macOS or WSL2.
- A model for the agents: an API provider connected in `opencode` with `/connect`, or a local model
  with tool calls and a 32K context window (16K at the least). With neither, the driver suggests a
  model for your card and can install Ollama and pull it for you.
- To drive from Claude Code, [Claude Code](https://claude.com/claude-code) with a Claude
  subscription or an API key.

Workbench runs in the OpenCode TUI and the Claude Code CLI. Desktop apps and IDE extensions are not
supported.

## Install

Workbench is a plugin for OpenCode and for Claude Code. Install it in whichever one you already
use.

### OpenCode

```sh
opencode plugin add 'github:a-schaefers/workbench#main'
```

Open OpenCode in your project, choose the driver in `/models`, and type `/workbench`. The driver
reviews every change, so choose the strongest model you have, local or API. OpenCode keeps the
plugin up to date.

### Claude Code

```sh
claude plugin marketplace add a-schaefers/workbench
claude plugin install workbench@workbench
```

Open Claude Code in your project and type `/workbench`. If it isn't listed, run `/reload-skills`
first. Claude is the driver. Run `/workbench-update` to update the plugin.

## Your first task

After `/workbench`, say `do a trial run`. The driver checks for OpenCode, a model server, a model
the agents can use and any API keys you have connected, offers to set up what is missing, then
runs a small sample task. For an API model, it recommends one of your provider's models and a daily
spending cap of $5. From here on the driver runs Workbench's `wb` command for you.

Then ask for what you want in plain words, for example "add a dark mode switch to the settings
page". Your project needs to be a git repository, and the driver can create one. Before the first
real task it writes a short CLAUDE.md with notes for the agents and commits it; an existing
AGENTS.md becomes that CLAUDE.md, so your notes are kept, and AGENTS.md links to it for OpenCode.

You can follow the agents in the Workbench pane (Claude Code) or panel (OpenCode). When their work
has passed its review, the driver commits each change on its own branch and reports back. Read the
report, then say "merge it" to bring the change into your branch, or say what to change. When you
are done, `/workbench-quit` stops the agents and keeps their work for later.

## Models and cost

Workbench is free and open source. A local GPU costs nothing per task; API models bill through
your provider, under a daily cap. In Claude Code, handing a larger task to the agents costs Claude
about half the tokens of writing it ([measured](ARCHITECTURE.md#what-a-handoff-costs-claude-measured)),
while small tasks cost as much or more, so batch them.

Workbench sorts the agents' work into large steps, small steps and writing such as docs, and lets
you give each of these [roles](ARCHITECTURE.md#roles-which-model-a-kind-of-work-goes-to) its own
model. You might, for example, send large steps to an API model and keep the rest on your GPU.

On a GPU, Workbench [detects your hardware](ARCHITECTURE.md#models-found-loaded-chosen) and learns
from your results which local model suits each kind of work. Once a week it checks for better ones.
Ollama has the fullest support, and any OpenAI-compatible server
[works too](ARCHITECTURE.md#running-your-own-server), on this machine or another.

Any provider you have connected in OpenCode with an API key works, and so does a GitHub Copilot
sign-in. Paid API usage counts against one daily
[spending cap](ARCHITECTURE.md#spend-what-an-api-task-costs) in dollars, across all providers; a
plan such as Copilot uses its own quota instead. At the cap, running API tasks stop and queued
ones wait.

## Safety and privacy

The agents never touch your checkout, and their work is merged only when you say so. Each one runs
in a [sandbox](ARCHITECTURE.md#what-the-agent-knows), Landlock on Linux and Seatbelt on macOS, that
keeps its writes inside its copy of the project, lets it use git only to read, allows only listed
commands, and keeps your API key out of reach. These limits, and the spending cap, are enforced by
code, never left to a model. On a kernel without Landlock, only OpenCode's permission rules apply,
and `wb doctor` says so.

Agents on your own GPU send your code to no provider, though the driver still sees what it reviews
unless it is local too. With an API model, whatever the agent reads goes to that provider. To keep a repository off every API, [mark it](ARCHITECTURE.md#privacy-for-api-roles)
`local_only` in `~/.config/workbench/commands.json`.

## What else it does

- Add the word `deepcode` to a request and the driver runs several agents in parallel that check
  each other's work, then judges what they produce. Workbench ships
  [flows](ARCHITECTURE.md#deep-workflows) for building, auditing, fixing and comparing attempts,
  and you can write your own.
- You can [schedule](ARCHITECTURE.md#scheduled-runs) work to run on a timer, with neither OpenCode
  nor Claude Code open.
- Tasks run in the background, survive a restart of OpenCode or Claude Code, and
  [keep the machine from sleeping](ARCHITECTURE.md#kept-awake). After a reboot, ask the driver to
  resume them.
- Accepted commits carry your name as author and a
  [trailer](ARCHITECTURE.md#git-commits-and-projects) naming the model that wrote the code.
- A web app shows your settings and stats, and its [chat](ARCHITECTURE.md#the-web-chat) lets you
  talk to the driver and follow tasks from your phone over Tailscale (ask the driver to turn it on).
- When a review turns up a lesson, the driver records it as a
  [skill](ARCHITECTURE.md#the-skill-library) and gives it to later agents.
- The driver can also direct local [image](ARCHITECTURE.md#images) and
  [audio](ARCHITECTURE.md#audio) models to make a project's icons, artwork, voice lines, sound
  effects and music.
- To have the driver finish the agents' open tasks itself when you quit, use
  `/workbench-quit-and-hand-back` (in Claude Code, `/workbench-quit-and-hand-back-claude`).

Every command, setting and file is described in [ARCHITECTURE.md](ARCHITECTURE.md).

## Contributing

Report bugs in the issue tracker. If you fix one yourself, `wb patch` keeps your fix across updates
and can send it upstream as a pull request, or as a .zip you email if you'd rather not use GitHub.
[CONTRIBUTING.md](CONTRIBUTING.md) shows how to set it up.

## License

MIT. Workbench is an independent project, not made by Anthropic or the OpenCode team.
