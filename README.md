<p align="center"><img src="docs/logo.jpg" alt="Workbench: a warm light directing three little robot craftsmen at a workbench" width="720"></p>

# Workbench

Workbench is a harness for OpenCode and Claude Code. An orchestrator agent plans and reviews the
work, and OpenCode subagents via API or GPU write the code.

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

You describe a change to the orchestrator agent, which splits it into steps, writes a test for each
one, and hands each step to a subagent running a cheaper model on your GPU or an API. Each subagent
works on its own branch in a separate copy of the project. After each attempt the step's test runs;
if it fails, the subagent sees the error and tries again without involving the orchestrator agent.
When the test passes, the orchestrator agent reviews the change and reports to you. Small models do
well on short, tested steps, so each step is sized to the model that writes it
([why](ARCHITECTURE.md#why-its-built-this-way)).

## What you need

- [OpenCode](https://opencode.ai) 2.3 or newer, even if you work in Claude Code, because the
  subagents run in it.
- Git 2.34 and Python 3.11 or newer, on Linux, macOS or WSL2.
- A model for the subagents: an API provider connected in `opencode` with `/connect`, or a local
  model with tool calls and a 32K context window (at least 16K). If you have neither, the
  orchestrator agent suggests a model for your GPU and can install Ollama and pull it for you.
- To use it in [Claude Code](https://claude.com/claude-code), a Claude subscription or an API key.

Workbench runs in the OpenCode CLI and the Claude Code CLI. Desktop apps and IDE extensions are not
supported.

## Install

Install the plugin in whichever one you use.

### OpenCode

```sh
opencode plugin add 'github:a-schaefers/workbench#main'
```

Open OpenCode in your project, pick the strongest model you have, local or API, for the orchestrator
agent in `/models`, and type `/workbench`. OpenCode keeps the plugin up to date.

### Claude Code

```sh
claude plugin marketplace add a-schaefers/workbench
claude plugin install workbench@workbench
```

Open Claude Code in your project, choose the strongest Claude model for the orchestrator agent with
`/model`, and type `/workbench`.

## Using it

After `/workbench`, work with the orchestrator agent as you normally would. The first time, it asks
a few questions to set things up. Workbench shows a link to its web app,
`http://localhost:1337/#token=…`, where you can follow the subagents' tasks, see your stats and
change settings.

## Models and cost

Workbench is free and open source. A task on a local GPU costs nothing. Your provider bills the
subagents' API use. In Claude Code, handing a larger task off costs Claude about half the tokens of
writing it ([measured](ARCHITECTURE.md#what-a-handoff-costs-claude-measured)). Small tasks cost as
much or more, so batch them.

Workbench sorts the subagents' work into large steps, small steps and writing such as docs, and lets
you give each of these [roles](ARCHITECTURE.md#roles-which-model-a-kind-of-work-goes-to) its own
model. For example, large steps can go to an API model and the rest to your GPU.

On a GPU, Workbench [detects your hardware](ARCHITECTURE.md#models-found-loaded-chosen) and learns
from your results which local model suits each kind of work. Once a week it checks for better ones.
Ollama has the fullest support. Any OpenAI-compatible server [works
too](ARCHITECTURE.md#running-your-own-server), even one on another machine.

Any provider you have connected in OpenCode with an API key works for the subagents, as does a
GitHub Copilot sign-in. Their paid API usage counts against one daily [spending
cap](ARCHITECTURE.md#spend-what-an-api-task-costs) in dollars, across all providers; a plan such as
Copilot uses its own quota instead. At the cap, running API tasks stop and queued ones wait.

## Safety and privacy

The subagents never touch your checkout, and their work is merged only after you accept it. Each
subagent runs in a [sandbox](ARCHITECTURE.md#what-the-agent-knows) that confines its writes to its
copy of the project and never lets it see your API key. It can run only listed commands, and git
only to read. The sandbox uses Landlock on Linux and Seatbelt on macOS; on a kernel without
Landlock, only OpenCode's permission rules apply. Workbench's code enforces these limits and the
spending cap.

Subagents on your own GPU don't send your code to any provider. A subagent on an API model sends
what it reads to that model's provider. The orchestrator agent sends what it reads to its own
model's provider, unless that model is local. To keep a repository's subagents off every API, [mark
it](ARCHITECTURE.md#privacy-for-api-roles) `local_only` in `~/.config/workbench/commands.json`.

## What else it does

- Add the word `deepcode` to a request and the orchestrator agent runs several subagents in parallel
  that check each other's work, then judges what they produce. Workbench ships
  [flows](ARCHITECTURE.md#deep-workflows) for building, auditing, fixing and comparing attempts, and
  you can write your own.
- You can [schedule](ARCHITECTURE.md#scheduled-runs) work to run on a timer, even with OpenCode and
  Claude Code closed.
- Tasks run in the background, survive a restart of OpenCode or Claude Code, and [keep the machine
  from sleeping](ARCHITECTURE.md#kept-awake). After a reboot, ask the orchestrator agent to resume
  them.
- Accepted commits carry your name as author and a
  [trailer](ARCHITECTURE.md#git-commits-and-projects) naming the model that wrote the code.
- The web app has a [chat](ARCHITECTURE.md#the-web-chat) for talking to the orchestrator agent and
  following tasks. To use it from your phone over Tailscale, ask the orchestrator agent.
- When a review turns up a lesson, the orchestrator agent records it as a
  [skill](ARCHITECTURE.md#the-skill-library) and gives it to later subagents.
- The orchestrator agent can also direct local [image](ARCHITECTURE.md#images) and
  [audio](ARCHITECTURE.md#audio) models to make a project's icons, artwork, voice lines, sound
  effects and music.
- To have the orchestrator agent finish the subagents' open tasks itself when you quit, use
  `/workbench-quit-and-hand-back` (in Claude Code, `/workbench-quit-and-hand-back-claude`).

Every command, setting and file is described in [ARCHITECTURE.md](ARCHITECTURE.md).

## Contributing

Report bugs in the issue tracker. If you fix one yourself, Workbench keeps your fix across updates
and can send it upstream as a pull request, or as a .zip to email instead of using GitHub.
[CONTRIBUTING.md](CONTRIBUTING.md) shows how to set it up.

## License

MIT. Workbench is an independent project, not made by Anthropic or the OpenCode team.