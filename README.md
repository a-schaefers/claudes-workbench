<p align="center"><img src="docs/logo.jpg" alt="Claude's Workbench: a warm light directing three little robot craftsmen at a workbench" width="720"></p>

# Claude's Workbench

**Claude plans and reviews. A model on your GPU writes and tests the code.** Workbench connects
the two inside your Claude Code session.

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

Claude runs on Anthropic's servers, on your subscription or API key. The coding agents run on your
machine, through [OpenCode](https://opencode.ai) and your model server.

- **Claude reads every diff before accepting it**, and sends it back with exact notes until it's
  right.
- **Pieces sized to your models:** chores to a small fast model, real work to the strongest one
  your GPU runs.
- **Recurring corrections become instructions** for future tasks
  ([the skill library](ARCHITECTURE.md#the-skill-library)).
- **Your repo stays yours:** each agent works in its own git worktree, and your plugins, skills
  and settings keep working.

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

`wb setup` ends with a checklist of what's still missing; Claude takes care of it next.

### In Claude Code, inside your project

```
/workbench
```

The first time, Claude finishes the setup and proves it on a tiny sample task. If `wb doctor`
reports no model that can run, `wb models` says why (the server is down, the model isn't pulled).

### Try one task

```
/workbench add a test for an empty input to the parser
```

The **Workbench** pane opens beside the chat, and the agent's work streams into it. When its tests
pass, the apprentice says the task is ready for review, and Claude reads the diff, then tells you
it committed the change or what it sent back.

<p align="center"><img src="docs/first-task.png" alt="Claude Code with the Workbench pane. On the left, Claude says it handed the test to a local 9B model and will review the change when the runner wakes it, and asks before changing the model's context window. On the right, the agent reads the test file, adds test_empty_input_gives_empty_dict, runs the tests (OK), and the apprentice shows the task's time and context; below it, the Claude coding setting." width="900"></p>

`/workbench-quit` stops the agents and keeps their work for later.
`/workbench-quit-and-hand-back-claude` has Claude finish their tasks itself.

## Where the work runs

The agents write only inside their own worktree and use git only to read. Commits, branches,
pushes, deploys and your `CLAUDE.md` stay with Claude. On Linux with Landlock the kernel enforces
that; on macOS, or Linux without it, it's a guardrail rather than a wall. `wb doctor` says which
you have, and [What the agent knows](ARCHITECTURE.md#what-the-agent-knows) gives the details and
the gaps.

## Roadmap and goals

The goal is for your own machine to finish more of your work. Today Claude plans, reviews and can
step in to fix code; your local agent writes and tests it. As local models improve, you can hand
them more: the pane's **Claude coding** setting runs from *Let Claude decide* to *Ask me first*
and *Only when I say*.

You should be able to walk away and come back to a change you can judge: a test that failed
before the fix and passes after (`wb verify`), or both versions of a visual change on your phone
(`wb shot`).
[Reliability](https://github.com/a-schaefers/claudes-workbench/issues/75) ·
[Delegation and review](https://github.com/a-schaefers/claudes-workbench/issues/77)

Next we measure on everyday projects and machines: how often local work is accepted, how often
Claude has to write code, and how much of your time each change takes. Our first paired runs, on
one machine, used fewer Claude tokens on two substantial tasks and more on two small ones
([how we measured it](THESIS.md)); `wb savings` shows yours.

## Where to go next

- **Use it from your phone:** [the web chat](ARCHITECTURE.md#the-web-chat)
- **Running your own server:** [connect it](ARCHITECTURE.md#which-server),
  [size its context window](ARCHITECTURE.md#the-context-window),
  [fix tool calls](ARCHITECTURE.md#tool-calls),
  [use a GPU on another machine](ARCHITECTURE.md#a-model-on-another-machine),
  [pass an API key](ARCHITECTURE.md#a-server-that-wants-an-api-key)
- **Every command:** [Every `wb` command](ARCHITECTURE.md#every-wb-command)
- **Updates:** Workbench tells you when there's one; `/workbench-update` installs it. Working on
  Workbench itself? [Installing](ARCHITECTURE.md#installing)
- **How it all works:** [ARCHITECTURE.md](ARCHITECTURE.md)

## License

MIT.
