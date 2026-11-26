---
name: claude-workbench
description: Moved to the ask-workbench skill (skill/agents/ask-workbench in the workbench repository), which hands larger coding work to Workbench's driver, Claude Code or OpenCode. Read its SKILL.md instead; these scripts still run it.
---

# Moved: ask-workbench

This skill is now `ask-workbench`, beside this directory: `../ask-workbench/SKILL.md`. The
driver that answers can be Claude Code or OpenCode, so the name no longer says Claude.

- `scripts/ask_claude.py` here runs `../ask-workbench/scripts/ask_workbench.py`, with the same
  arguments and exit status.
- `scripts/mcp_server.py` here runs `../ask-workbench/scripts/mcp_server.py`. Its request tool
  is `ask_workbench`; the old name, `ask_claude`, still reaches it.
- `WORKBENCH_URL` and `WORKBENCH_TOKEN` are the variables' new names; `CLAUDE_WB_URL` and
  `CLAUDE_WB_TOKEN` still work.

Point your agent at `../ask-workbench/` when you next set it up.
