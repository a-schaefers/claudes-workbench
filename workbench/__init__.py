"""workbench: Claude as architect and reviewer, local OpenCode agents as the hands.

A task is a spec handed to a local model running in OpenCode inside its own git
worktree. A detached runner streams the agent's events, gates every round on the task's tests,
feeds failures back on its own for a bounded number of rounds, stops runs that are stuck, and only
then asks for Claude's review. Everything a task is lives in one directory of files, so the CLI,
the Claude Code mod and the agent's ask_reviewer tool all read and write the same state.
"""

__version__ = "1.91.2"
