<p align="center"><img src="docs/logo.jpg" alt="Workbench: a warm light directing three little robot craftsmen at a workbench" width="720"></p>

# Home of Claude's Workbench & OpenCode Workbench

**Workbench is a harness: OpenCode agents via API or GPU write the code.** Drive them from OpenCode with any model, or from Claude Code with `/workbench`.

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

**TL;DR.** A [harness](#whats-a-harness) is the setup agents work inside: what they know, what
they may do, and how their work is checked.

- **You** say what you want, and decide what lands in your checkout.
- **The lead** (Workbench calls it the *driver*) is the model you talk to: it splits the work into
  small steps and reviews every change.
- **The crew** are OpenCode agents, on your GPU or an API model. Each one works on its own copy of
  your project, on its own branch. Your tests run after each try, and a failure goes back to the
  agent first, not to the lead. The agents never touch your checkout: only what you accept is
  merged.
- **Which one:** Claude leading on your Claude subscription? Claude's Workbench. Otherwise,
  OpenCode Workbench ([pick](#which-one-is-for-me)).

**Contents:** [Which one is for me?](#which-one-is-for-me) · [Quickstart](#quickstart) ·
[What it does](#what-it-does) · [Also in the box](#also-in-the-box) · [FAQ](#faq) ·
[Contributing](#contributing) · [License](#license)

## Which one is for me?

**Everyone needs [OpenCode](https://opencode.ai).** The agents that write the code run there.
They need a model of their own: one on your GPU, or an API model you connected in OpenCode
(`/connect`). Your Claude subscription, or the model you pick in `/models`, is only the lead.

**Then one question: do you want Claude to lead, on your Claude subscription?**

| | **No, or not sure** | **Yes** |
|---|---|---|
| **Install** | [**OpenCode Workbench**](#opencode-workbench), a plugin inside OpenCode | [**Claude's Workbench**](#claudes-workbench), a plugin inside Claude Code |
| **The lead** | Any model you pick in OpenCode's `/models`: one on your GPU, or an API model | Claude |
| **Pick it if** | You want to choose the lead, or you already work in OpenCode | You pay for Claude, or you already work in Claude Code |
| **Then type** | `/workbench` | `/workbench` |

*Why two?* Claude Code is where a Claude subscription leads. OpenCode lets any model lead.
Everything under the lead is the same: the agents, the tasks, the web app and your settings.

Both run in the terminal: the OpenCode CLI and the Claude Code CLI.

## Quickstart

### What you need

- [OpenCode](https://opencode.ai) 2.3 or newer: `curl -fsSL https://opencode.ai/v2/install | bash`
- Git 2.34 and Python 3.11 or newer, on Linux, macOS, or Windows through WSL2
- A model for the agents: one you connected in OpenCode (`/connect`), or a local one. None yet?
  The lead suggests one that fits your machine, and asks before installing anything.
- For Claude's Workbench only: [Claude Code](https://claude.com/claude-code), with a Claude
  subscription or an API key

### OpenCode Workbench

```sh
opencode plugin add 'github:a-schaefers/workbench#main'
```

Then open OpenCode in your project, pick the lead with `/models`, and type `/workbench`. (No
provider in `/models` yet? `/connect` it first.) That's the whole setup: the plugin brings
Workbench with it, and OpenCode keeps it up to date.

### Claude's Workbench

```sh
claude plugin marketplace add a-schaefers/workbench
claude plugin install workbench@workbench
```

Then open Claude Code in your project and type `/workbench`. The first time, Claude finishes the
setup, offers what's missing, and proves it on a tiny sample task. (`/workbench` not listed?
`/reload-skills`; still not, ask Claude to run `wb setup`.) It's still Claude Code: your plugins,
skills and settings keep working, with the agents' tasks in a pane beside the chat.

### Your first task

First time? Type `/workbench` and say "show me it works": the lead checks what's missing, asks
before installing anything, and runs a tiny sample task.

Two things happen once, before the first real task:
- On an API model, the lead proposes which of your provider's models the agents use and a day cap
  ($5 suggested), and asks your yes in a dialog.
- In a new project, the lead writes a short CLAUDE.md (the project's notes for the agents;
  AGENTS.md links to it) and commits it.

Then:

1. **You ask** in plain words: "add a dark mode switch to the settings page".
2. **The lead writes the steps**, each small, each with a test that proves it.
3. **The test runs on your untouched code first**, to catch a test that can't run.
4. **An agent writes the step**, and a failing test goes back to the agent, not the lead.
5. **The lead reviews** the passing change. Meanwhile the task shows in the Workbench pane (Claude
   Code) or panel (OpenCode), and the lead comes back with a report.
6. **You decide what lands.** Each good change is committed on its own branch. Say "merge it", or say what
   to change.

When you're done:
- `/workbench-quit` stops the agents and keeps their work for later.
- `/workbench-quit-and-hand-back` (in Claude Code, `/workbench-quit-and-hand-back-claude`) has the
  lead finish their tasks itself.
- `/workbench-update` installs the latest Workbench in Claude Code. In OpenCode, OpenCode updates
  it by itself.

### The web app

Workbench also runs a web app. Its link is on the bottom line of the Workbench pane (Claude Code)
or panel (OpenCode): `[•_•] Claude's Workbench - http://localhost:1337/#token=…` in Claude Code,
`[•_•] Workbench - …` in OpenCode. Settings and Stats live in the web app (or ask the lead to
change a setting for you). From its chat you message the lead from anywhere: from your phone over
Tailscale (your tailnet), once the lead runs `wb chat reach tailnet` or you set the Web app reach
row in Settings.
<!-- story:doc.rule -->
The terminal is where Claude or OpenCode lives (locally, or over `ssh` + `tmux`), and a prompt it raises is answered there. Most work can still run from the web app's chat, more so with Claude or OpenCode run with permissions granted. The web app's Settings are the source of truth: Claude or OpenCode changes one only on your ask and yes, and never undoes a change you made.
<!-- /story -->

## What it does

You talk to the lead. It runs Workbench for you, so you never need to learn its commands: the
`wb …` commands on this page are for the lead, so ask it to run one (from OpenCode's install, `wb`
isn't in your own terminal). Each line links to the detail in [ARCHITECTURE.md](ARCHITECTURE.md).

### Say what you want, and get back a tested, reviewed change

- Each task gets its own branch and its own copy of your project.
- Your test runs on the untouched code first, then after every try. Failures go back to the agent.
  Then the agent checks its own change, and the lead reviews it.
  ([A task, start to finish](ARCHITECTURE.md#a-task-start-to-finish))
- A stuck agent is stopped, and a fresh session takes over before an agent's memory fills.
  ([Context, on both sides](ARCHITECTURE.md#context-on-both-sides))
- An agent can ask the lead a question mid-task. The lead can steer it, or take it over.
  ([Talking to a running agent](ARCHITECTURE.md#talking-to-a-running-agent))
- Hand it a mockup or a screenshot, compare screenshots before and after, and rewind a task to an
  earlier try. ([Screenshots](ARCHITECTURE.md#screenshots-of-the-base-and-the-candidate))

### Many tasks at once, while you do something else

- Type `deepcode` in a request, and the lead runs it as a flow: many tasks at once, checking each
  other, with the lead deciding at the end. Shipped flows build, audit, fix, compare several tries,
  and triage; you can write your own. `deepmode on` makes it the default for bigger requests.
  ([Deep workflows](ARCHITECTURE.md#deep-workflows))
- Put work on a timer, with no session open. ([Scheduled runs](ARCHITECTURE.md#scheduled-runs))
- A provider's rate limit or outage is waited out, and a kind of work can fall back to another
  model. ([Roles](ARCHITECTURE.md#roles-which-model-a-kind-of-work-goes-to))
- Tasks run in the background, carry on if OpenCode or Claude Code restarts (after a reboot, one
  command resumes them), and keep the machine awake while they run.
  ([Kept awake](ARCHITECTURE.md#kept-awake))
- From your phone, in the web app: talk to the lead, follow each task, and get notified.
  ([The web chat](ARCHITECTURE.md#the-web-chat))

### Any model, on the hardware you have

- There are three kinds of work: big steps, small steps and prose. Each goes to your GPU or to an
  API model, and you can mix them.
- On a GPU, Workbench reads your hardware, suggests a model that fits your card, learns which model
  does which work best from your own results, and checks for better models once a week. It asks
  before it downloads or changes anything.
  ([Models: found, loaded, chosen](ARCHITECTURE.md#models-found-loaded-chosen))
- Any server that speaks the OpenAI API works, on this machine or another one.
  ([Running your own server](ARCHITECTURE.md#running-your-own-server))
- Any API model you've connected in OpenCode with a key works, and GitHub Copilot's sign-in too.
  ([Roles](ARCHITECTURE.md#roles-which-model-a-kind-of-work-goes-to))
- You set a daily spending cap, and one mark keeps a repo off every API.
  ([Spend](ARCHITECTURE.md#spend-what-an-api-task-costs), [Privacy](ARCHITECTURE.md#privacy-for-api-roles))

### It gets better the more you use it

- The lead keeps a library of skills, written from its reviews, and hands them to later agents.
  ([The skill library](ARCHITECTURE.md#the-skill-library))
- Your own instructions for agents (`~/.config/workbench/AGENTS.md`) and the MCP servers you allow
  reach every agent. ([What the agent knows](ARCHITECTURE.md#what-the-agent-knows))
- Replay your accepted tasks on your GPU models, to compare them on your real work.
  ([Bench](ARCHITECTURE.md#bench-real-tasks-replayed))
- You choose when the lead may write code itself.
  ([When the lead writes the code](ARCHITECTURE.md#claude-coding-when-claude-writes-a-tasks-code))

## Also in the box

- **Images and audio**, with the lead directing: local models make a project's icons, pictures,
  voice lines, sound effects and music, and Workbench asks before downloading one.
  ([Images](ARCHITECTURE.md#images), [Audio](ARCHITECTURE.md#audio))
- **A sandbox for the agents** on Linux and macOS, with read-only git, and they never see your API
  key. ([What the agent knows](ARCHITECTURE.md#what-the-agent-knows))
- **Fix Workbench yourself** from a clone of it, keep your fixes across updates, and send them
  upstream, with or without GitHub. ([Contributing](#contributing))

## FAQ

- **Choosing:** [What's a harness?](#whats-a-harness) · [More than plain OpenCode?](#what-do-i-get-that-plain-opencode-doesnt-give-me) ·
  [Must Claude lead?](#do-i-have-to-use-claude-as-the-lead) · [Both at once?](#can-i-have-it-in-claude-code-and-opencode-at-once)
- **Hardware and models:** [GPU, API or both?](#gpu-only-api-only-or-both) · [Which model?](#how-do-i-know-which-model-to-run) ·
  [Which local servers?](#which-local-gpu-servers-does-it-work-with) · [Several GPUs?](#several-gpus-or-a-gpu-on-another-machine) ·
  [Rate limits](#what-about-rate-limits-outages-and-bad-keys) · [Versions](#which-opencode-version-and-how-big-a-window)
- **Money:** [The day cap](#how-does-the-day-cap-work) · [Token savings](#does-it-save-claude-tokens)
- **Using it:** [deepcode](#how-does-deepcode-compare-with-claude-codes-ultracode) · [What needs my yes?](#what-runs-on-its-own-and-what-needs-my-yes) ·
  [How safe?](#how-safe-are-the-agents) · [Recovery](#can-runs-recover-and-can-i-audit-them) · [Context](#how-is-context-managed) ·
  [Phone](#can-i-use-it-from-my-phone) · [Another agent](#can-another-agent-drive-workbench-hermes-agent-for-example) ·
  [Platforms](#which-platforms-and-which-apps) · [Updating and files](#updating-uninstalling-and-where-things-live) · [Privacy](#where-does-my-code-go)

### Choosing

#### What's a harness?

An agent framework is a library for building your own agent. A harness is the finished setup an
agent works inside: what goes into its context, which tools it has and with what rights, when it
stops, and how its work is checked before it reaches you. OpenCode runs the agents; Workbench gives
each task its own branch and copy of your project, a spec, the skills it needs, a sandbox, your
tests after every try, guards against loops, and the lead's review. You don't write agent code:
you say what you want.

Why it helps: smaller models lose the thread on long, open-ended work, and do well on small,
checked steps. So the steps are sized to the model, tests decide pass or fail, a fresh session
takes over before the context fills, and what must be exact (pass or fail, permissions, spend,
merges) stays in code, not in a model. ([Why it's built this way](ARCHITECTURE.md#why-its-built-this-way))

#### What do I get that plain OpenCode doesn't give me?

Plain OpenCode is a very good single agent: you pick a model, talk to it, and it edits your
project. Keep using it that way for quick work. Workbench turns it into a crew with a lead, for
work bigger than one conversation:

| | Plain OpenCode | With OpenCode Workbench |
|---|---|---|
| Who does the work | The model you talk to, in your checkout | The lead plans and reviews; agents write the code, each on its own branch and copy |
| How big a job | What fits in one session | Small steps sized for each model, many tasks at once, whole flows with `deepcode` |
| Checked how | You read what it did | Your tests on the untouched code, then after every try; then the lead's review |
| Which model | One at a time | A model per kind of work, GPU or API, learned from your results |
| Your checkout | Changed as it goes | Never touched by the agents; merged when you say so |
| Getting better | Starts fresh each time | A skill library the lead writes and hands out |

Your OpenCode stays as it is: its config, providers, keys, `/models` and `/connect`. Workbench is
a plugin on top.

#### Do I have to use Claude as the lead?

No. In OpenCode Workbench any model you pick in `/models` leads: DeepSeek, ChatGPT, Gemini, a model
on your own GPU, or Claude through an API key. Claude leads only in Claude's Workbench, which is the
way to lead on a Claude subscription. The lead holds the final say on every change, so a stronger
lead reviews better. ([The driver](ARCHITECTURE.md#the-driver))

#### Can I have it in Claude Code and OpenCode at once?

Yes, both installed and `/workbench` open in each. They share one state directory: a task belongs
to the session that started it, and only that session is woken for it. Either app's lead can
accept a task you name. The GPU, the API slots and the day's spend cap are counted once for both.
Each app's yes in its consent dialog is its own, and the other app's calls never use it up. Only
the lead listening to the web app answers your phone. `wb doctor` lists each install and the `wb` it runs.
([Both drivers on one machine](ARCHITECTURE.md#both-drivers-on-one-machine))

### Hardware and models

#### GPU only, API only, or both?

- **GPU only:** nothing to pay per task, and your code never leaves your machine. You need a model
  server (Ollama is the easiest) and a model that fits your card.
- **API only:** no GPU needed. Connect a provider in OpenCode (`/connect`), and a daily spending
  cap covers every provider together.
- **Both:** each kind of work has its own setting. They're called *significant* (big steps),
  *trivial* (small steps) and *writing* (prose). A common mix is big steps on an API model and the
  rest on the GPU. One switch sends everything to the GPU, everything to an API, or each kind its
  own way; an opt-in sends work to the API while the GPU is busy; and a kind can fall back to
  another model when its provider is down or out of credit.

No GPU and no key? The lead can let you try it first on OpenCode's free models, with no account
(their provider may keep or train on the code). The lead never moves your code to an API by
itself: a change of where a kind of work runs needs your yes in a dialog.
([Roles](ARCHITECTURE.md#roles-which-model-a-kind-of-work-goes-to))

#### How do I know which model to run?

You don't have to. Workbench reads your machine fresh each time (NVIDIA, AMD, Apple silicon,
Jetson, and Windows' cards from WSL2) and guesses nothing. With nothing that can run, the lead
suggests a model that fits your card, and asks before installing or downloading anything. With
Ollama it can pull a model and set its window, and measure the largest window that stays fully on
your GPU. Once a week the lead reviews what you run, how each model did on your tasks, and what's
new, then recommends keeping, adding or retiring one, and changes nothing without your yes.
([Models](ARCHITECTURE.md#models-found-loaded-chosen), [Optimize](ARCHITECTURE.md#optimize-measuring-this-machine-when-asked))

#### Which local GPU servers does it work with?

- **Ollama** gets the most done for you: models pulled and removed, each model's window set, and
  two tasks sharing the GPU.
- **llama.cpp (`llama-server`), LM Studio, vLLM and SGLang** work too. Workbench reads each model's
  window from the server, and tasks take turns on the GPU.
- **Any other server that speaks the OpenAI API** works: `wb model add <provider>/<model> --url
  <its URL>`, plus `--context N` if it doesn't say its window.

Each server's settings (the window, tool calls): [Running your own server](ARCHITECTURE.md#running-your-own-server).

#### Several GPUs, or a GPU on another machine?

- **Several cards in one machine:** NVIDIA cards add up to one budget, which Ollama spreads a model
  over; cards of different makers don't add up, and the largest group counts.
- **A GPU on another machine:** run Ollama there and `wb remote <host>`, or add any OpenAI-compatible
  server on your network by URL. That machine takes turns of its own, apart from this one's GPU.
  From WSL2, a server on Windows works the same way.

Workbench adds models from each server; it never splits one model across machines.
([A model on another machine](ARCHITECTURE.md#a-model-on-another-machine))

#### What about rate limits, outages and bad keys?

A rate limit or an outage is waited out, and the task's clocks pause meanwhile. Held for 10
minutes, the try stops and runs once more, then goes to its fallback model if you set one. Out of
credit skips the wait: it goes to the fallback, or stops. A refused key stops at once, with one
line naming the provider. Your day cap holds: each running task can go over it by at most one
request. ([Spend](ARCHITECTURE.md#spend-what-an-api-task-costs))

#### Which OpenCode version, and how big a window?

OpenCode 2.3 or newer. On 1.x, `opencode upgrade --major` (npm's `opencode-ai` and GitHub's Latest
release are 1.x). For a local model: tool calls, and a 32K context window recommended,
16K at the least (OpenCode's own prompt takes about 10K). `wb doctor` checks both.

### Money

#### How does the day cap work?

Every API task counts against one daily cap in dollars, across every provider together. You set it
with your first API model (the lead suggests $5), and change it in the web app's Settings. At the
cap, running API tasks stop and queued ones wait. The cost is OpenCode's estimate from the model's
prices, unless the provider reports its own. GPU tasks cost nothing.
([Spend](ARCHITECTURE.md#spend-what-an-api-task-costs))

#### Does it save Claude tokens?

In Claude's Workbench, on bigger coding tasks, once the session is already open, handing the coding
off to the agents typically costs Claude about half the tokens of writing the code itself. Small
tasks cost as much or more, so batch them. That comes from paired runs, each request done both
ways: [What a handoff costs Claude, measured](ARCHITECTURE.md#what-a-handoff-costs-claude-measured).
In OpenCode Workbench the lead can be any model, so there's no such figure; Stats shows what
OpenCode reports.

### Using it

#### How does deepcode compare with Claude Code's ultracode?

Both split one request into many pieces of work that run at once and end with the lead deciding.
In ultracode the workers are Claude. In `deepcode` they are your OpenCode agents on your GPU or an
API model, each one a Workbench task with its own branch, tests and sandbox, from a flow file you
can read and change. The lead starts the run once, and is woken only at checkpoints and at the end.
On an API, the run's budget is held to the day cap. `deepcode` works in both Claude's Workbench and
OpenCode Workbench. ([Deep workflows](ARCHITECTURE.md#deep-workflows))

#### What runs on its own, and what needs my yes?

- **On its own:** the agents' tries, the tests and automatic fixes, fresh sessions, the stuck
  guards, the lead's review, and committing a good change on its own branch.
- **Your yes, in a dialog:** sending code to an API model (a change of where a kind of work runs,
  or one task on an API model), and sending a fix to Workbench upstream (`wb patch send`).
- **Your yes, asked first:** installing or downloading a model, a server or a tool; the weekly
  model check's changes; giving an agent a command or a directory for good; anything outward, such
  as a push, a pull request or a comment.
- **Yours alone:** merging into your checkout (only when you say to merge), lifting a repo's
  local-only mark, and the day cap.

#### How safe are the agents?

A teammate with limits, not a prisoner. By default an agent writes only its own copy of the
project, uses git read-only (Workbench commits, only when a change is accepted), runs commands from
an allowlist with your credentials stripped from its environment, and never sees an API key: a
small proxy per task holds it. Neither the project's own OpenCode config nor the agent can lift
those rules. When it hits a wall it asks, and the lead can grant one more command or directory,
for one task or for good. Your tests run under the same limits. On Linux, Landlock enforces the sandbox, and on macOS,
Seatbelt; where neither is available only OpenCode's permission rules apply, and `wb doctor` says
so. ([What the agent knows](ARCHITECTURE.md#what-the-agent-knows))

#### Can runs recover, and can I audit them?

Yes. Each task runs in its own background process, so it carries on if OpenCode or Claude Code
restarts or the terminal closes, and the lead is told when it's back. A task whose process died is
marked failed with the reason; after a reboot, one notice names every task that was interrupted, and
one command (`wb resume --all`) starts them again. Every task keeps its whole record (each prompt,
the test output, the events, the agent's transcript and the report), and every accepted commit
names the model and carries the task and its session, never an AI co-author line. It all stays
on your machine as plain files. ([A task, start to finish](ARCHITECTURE.md#a-task-start-to-finish),
[Git, commits and projects](ARCHITECTURE.md#git-commits-and-projects))

#### How is context managed?

- **The agents':** steps are sized for the agent's model and window, learned from your results.
  Before an agent's context fills, a fresh session takes over with the task, what's done and
  what's left, and OpenCode's own compaction is only a backstop. Context never carries between
  tasks.
- **The lead's:** `wb` keeps the state, not the conversation, and answers in short reports. When
  the lead's conversation compacts, it keeps your goals and the open tasks and drops finished work.

([Context, on both sides](ARCHITECTURE.md#context-on-both-sides))

#### Can I use it from my phone?

Yes, in the web app's chat: message the lead, follow each task, change settings, and get
notified. Reach it over Tailscale, your tailnet (`wb chat reach tailnet`). To install it as an app and get
notifications, serve it over HTTPS, for example with `tailscale serve --bg 1337`, then "Add to Home
Screen" on iPhone or "Install app" on Android. A prompt the lead raises in the terminal waits there.
([The web chat](ARCHITECTURE.md#the-web-chat))

#### Can another agent drive Workbench (Hermes Agent, for example)?

Yes, through the API. Make it a token (`wb chat token add <its name>`) and give it the skill in
[`skill/agents/ask-workbench/`](skill/agents/ask-workbench/SKILL.md), or its MCP server. It hands
your lead (in Claude Code or OpenCode) a request over the web chat's API, waits for the report,
and follows up, and it is told which app answered. Its message is a teammate's request, never
your yes: spending, pushing or deleting still waits for you. The API:
[docs/chat-api.md](docs/chat-api.md).

#### Which platforms, and which apps?

Linux, macOS, and Windows through WSL2. Workbench supports the two CLIs: the OpenCode TUI and the
Claude Code CLI. OpenCode Desktop, `opencode web`, the Claude desktop app and IDE extensions aren't
supported. From WSL2, a model server on Windows works ([A server on Windows, from
WSL2](ARCHITECTURE.md#a-server-on-windows-from-wsl2)).

#### Updating, uninstalling, and where things live

- **Updating:** in OpenCode, OpenCode updates the plugin by itself. In Claude Code,
  `/workbench-update`, and Workbench tells you when there's a new one.
- **Where things live:** plain files. Tasks and their records in `~/.local/state/workbench`, your
  settings in `~/.config/workbench/`, the skill library in `~/.local/share/workbench/skills` (its
  own git repo). ([Files and formats](ARCHITECTURE.md#files-and-formats))
- **Uninstalling:** ask the lead to turn the web app off (`wb chat off`), then remove the plugin as
  your app removes one: Workbench's entry in OpenCode's `plugins`, or Claude Code's `/plugin`. In
  Claude Code, also stop the web app's service (`systemctl --user disable --now wb-chat`; on macOS
  `launchctl bootout gui/$(id -u)/workbench.chat`, then delete
  `~/Library/LaunchAgents/workbench.chat.plist`) and delete the links `~/.local/bin/wb` and
  `~/.claude/skills/workbench`. Then delete those directories if you like.

#### Where does my code go?

On the GPU, nowhere: it stays on your machine. With an API model, everything the agent reads goes
to that provider, which you agreed to when you chose it. To keep a repo off every API, mark it
`local_only` in `~/.config/workbench/commands.json`; only you can lift it.
([Privacy for API roles](ARCHITECTURE.md#privacy-for-api-roles))

## Contributing

Found a bug? Open an issue. Fixed one? Keep it as a local patch (`wb patch`), which survives
updates, and send it upstream as a pull request, or as a .zip to email if you'd rather not use
GitHub: [CONTRIBUTING.md](CONTRIBUTING.md). `wb patch` works on a clone of Workbench: on
OpenCode's install, `git clone https://github.com/a-schaefers/workbench`, then
`bin/wb setup --opencode` there.

## License

MIT. Workbench is an independent project, not made by Anthropic or the OpenCode team.
