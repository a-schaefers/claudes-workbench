<p align="center"><img src="docs/logo.jpg" alt="workbench: a warm light directing three little robot craftsmen at a workbench" width="720"></p>

# workbench

**Claude plans and reviews. Your gaming PC writes the code.**

The goal: move to a hybrid workflow on an ordinary gaming PC without giving up the quality of
Claude (Opus 5.5). Open coding models you can run at home are said to code at roughly the level of
last year's best cloud models, but left alone they wander, overthink and cut corners. workbench
puts Claude in charge of them: Claude plans the work, hands each piece to a local model, checks the
result like a strict senior reviewer, and sends it back until it's right. Claude spends its tokens
on thinking and judging; your GPU spends electricity on typing.

The hope is a smaller Claude bill: a cheaper subscription, or one day no subscription at all, just
the Claude API for the driver and local models for the heavy lifting. Whether it pays off depends
on how much you use it and what your electricity costs (a busy GPU isn't free). But it's a fun
project, and it works.

Built and used on: **Ryzen 5 7600X · Radeon RX 9070 XT (16 GB) · 32 GB DDR5 · Arch Linux.**

## What you need

- **[Claude Code](https://claude.com/claude-code)** with a Claude subscription or an API key
- **[OpenCode](https://opencode.ai)**, which runs the local models as coding agents
- **A local model server**, usually [Ollama](https://ollama.com), and at least one coding model
  registered in OpenCode (no models yet? workbench recommends some for your hardware and helps you
  set them up)
- A GPU with about 16 GB of VRAM or more, Python 3.11+, git, and Linux

## Install

```sh
git clone https://github.com/a-schaefers/workbench ~/repos/workbench
cd ~/repos/workbench
ln -s "$PWD/bin/wb" ~/.local/bin/wb
ln -s "$PWD/skill/delegate" ~/.claude/skills/delegate
claude plugin marketplace add "$PWD"
claude plugin install workbench@workbench
wb doctor
```

## Use it

Open Claude Code in your project and type:

```
/delegate add a high-score screen to the game
```

That's it. Claude takes over from there:

1. It checks your hardware and your local models. If you have none, or better ones would fit your
   GPU, it recommends them and offers to set them up (it always asks before downloading).
2. It plans the work and splits it into small pieces a local model can finish.
3. It decides how hard each piece is and hands it to the right local model: chores (a rename, a
   config tweak, a small test) to a small fast model like MiMo 9B, real work (features,
   refactors, tricky bugs) to the strongest model your GPU runs, like Qwen 27B. Then it watches
   the agent work.
4. It reviews every change, sends it back with notes until it's right, and only then commits it.

A **Workbench** pane in Claude Code shows the agents working live.

| Command | What it does |
|---|---|
| `/delegate <task>` | Claude plans the task and hands it to your local models |
| `/workbench` | open the pane: every delegated task, and the agent's work live |
| `/delegate-models` | check now which local models suit your hardware best (it also happens weekly) |
| `/delegate-quit` | stop every agent now; their work is kept for later |
| `/delegate-quit-and-hand-back-claude` | stop the agents and have Claude finish their tasks itself |

**Away from the computer?** `wb chat url` prints a private link to a small web chat with the
running Claude session (`systemctl --user link ~/repos/workbench/systemd/wb-chat.service &&
systemctl --user enable --now wb-chat` keeps it running). It's `http://localhost:1337`; to use it
from your phone, reach your PC the way you normally do (Tailscale, for example) and change the host
in the link.

## How it works

- **Claude is the lead.** It writes a precise spec for each piece of work, with the files to touch
  and a test that proves it's done.
- **A local model does the typing** in OpenCode, in its own copy of your repo (a git worktree), so
  your working tree is never touched until Claude accepts the change.
- **Tests come first.** Failing tests go back to the agent automatically; Claude only looks at work
  that passes, a question the agent asks, or an agent that got stuck.
- **Claude holds the bar.** It reads every diff itself, never trusts the agent's summary, and fixes
  or discards anything that isn't up to its own standard.
- **Small pieces.** Local models work in a small context, so one concern and a couple of files per
  task. A step that never finished as one big task finished in half an hour as three small ones.
- **Claude builds a skill library for the agents.** When review teaches something the next agent
  will need too (a library's quirk, a pitfall, a check to run), Claude writes it down as a skill.
  Each agent gets only the skills that fit its task, and Claude takes them back when that part is
  done, so a growing library never clutters a small model's prompt. Claude tracks which skills
  actually help and retires the ones that don't.

## What we found (November 2026, on the PC above)

- **Qwen 27B** (Ollama, Q3) does the real work: it fits entirely on a 16 GB card and passed nearly
  every task we gave it. **MiMo 9B** takes the trivial chores.
- Bigger models squeezed onto the card with system RAM were slower, crashed, and were no better.
- Quantization matters (higher is better), but on 16 GB a larger model at about 4 bits beats a
  smaller one at 8.
- Thinking mode off works best for agent work: the model writes code instead of planning it at
  length.

Models change fast. Once a week workbench asks Claude to check what's new for your hardware and
recommend changes; nothing is downloaded without your yes.

## License

MIT. Development notes for contributors (and for Claude) are in [CLAUDE.md](CLAUDE.md).
