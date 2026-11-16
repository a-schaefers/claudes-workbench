"""What the agent knows before it starts, so it works the way Claude would in the same repo.

OpenCode reads AGENTS.md by itself but not CLAUDE.md, and it lists every skill under
~/.claude/skills and the project's .claude/skills. This module adds the rest to the task's
config layer: the workbench handbook, the user's and the repo's Claude instructions, a map of
the nested CLAUDE.md files to read on the way, and a skill policy (the user's personal skills
and /delegate itself are hidden, leaving the project's own; so is `question`, which nobody
answers headless).
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from . import gitops, store

PKG_ROOT = Path(__file__).resolve().parent.parent
HANDBOOK = PKG_ROOT / "agent" / "HANDBOOK.md"
ROOT_FILES = ("CLAUDE.md", ".claude/CLAUDE.md", "CLAUDE.local.md")
# Tools an unattended run in a worktree must not use. Nobody answers `question`. Code mode
# (`execute`) and the opencode_* tools can move the session or remove worktrees. A `subagent`
# would queue a second model behind the GPU lock. Denied tools also leave the prompt, which cuts
# the fixed prompt from ~9k to ~3.5k tokens: less to read on every request. OpenCode 2.3 still
# lists opencode_worktree_remove; the rule then refuses the call.
DENIED_TOOLS = ("question", "experimental_image_generate", "execute", "subagent", "websearch",
                "opencode_*", "*worktree*")
# The one hard limit on the shell: no root. OpenCode checks each part of a compound command, so
# `cd x && sudo ...` is refused too. Everything else is the agent's to use, guided by the handbook.
DENIED_SHELL = ("sudo", "sudo *", "doas", "doas *", "pkexec *", "su", "su *")


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tracked(worktree: Path, *patterns: str) -> list[str]:
    out = gitops.git(worktree, "ls-files", "-z", "--", *patterns)
    return sorted(p for p in out.split("\0") if p)


def instruction_files(task: store.Task) -> tuple[list[Path], list[str]]:
    """(files OpenCode should load, nested CLAUDE.md paths the agent reads when it works there)."""
    wt, repo = task.worktree, Path(task.spec["repo"])
    # the agent's own copy: OpenCode shows each instruction file's path, and a path into the
    # workbench repo once led a freshly started agent to go looking there for plans
    handbook = task.dir / "HANDBOOK.md"
    handbook.write_text(HANDBOOK.read_text())
    load: list[Path] = [handbook]
    user = Path.home() / ".claude" / "CLAUDE.md"
    if user.is_file():
        load.append(user)
    for rel in ROOT_FILES:
        # the worktree's copy when the file is committed; the repo's when it is local or uncommitted
        for base in (wt, repo):
            if (base / rel).is_file():
                load.append(base / rel)
                break
    load += [wt / p for p in _tracked(wt, ".claude/rules/*.md")]
    # OpenCode already loads AGENTS.md: skip a CLAUDE.md that is the same file or the same text
    agents = {_digest(p) for p in (wt / "AGENTS.md", repo / "AGENTS.md") if p.is_file()}
    seen, files = set(agents), []
    for p in load:
        d = _digest(p)
        if d not in seen:
            seen.add(d)
            files.append(p)
    nested = [p for p in _tracked(wt, "*CLAUDE.md", "*AGENTS.md")
              if "/" in p and Path(p).name in ("CLAUDE.md", "AGENTS.md")
              and not p.startswith(".claude/")]
    return files, nested


def denied_skills(extra: list[str] | None = None) -> list[str]:
    """The user's personal skills (~/.claude/skills, the synced Claude-app ones included) are
    theirs, about their other work: an agent sees only the project's own .claude/skills. On
    2026-11-16 an agent building a game, its context full, summarised a personal publishing skill
    into a made-up "newer task" and went off reading the user's private library."""
    home = Path.home() / ".claude" / "skills"
    names = {"delegate", *(extra or [])}
    names |= {s.parent.name for s in home.glob("*/SKILL.md")}
    names |= {s.parent.name for s in (home / "synced").glob("*/*/SKILL.md")}
    return sorted(names)


def write_map(task: store.Task, nested: list[str]) -> Path | None:
    """A short note pointing at the directory-level instructions, read only where they apply."""
    if not nested:
        return None
    path = task.dir / "context.md"
    lines = ["# Directory instructions in this repository", "",
             "Before you change files under one of these directories, read its file and follow it:"]
    lines += [f"- `{p}`" for p in nested]
    path.write_text("\n".join(lines) + "\n")
    return path


def layer(task: store.Task) -> dict:
    """The config-layer keys that give the agent its context and its skill policy."""
    files, nested = instruction_files(task)
    note = write_map(task, nested)
    rules = [{"action": t, "resource": "*", "effect": "deny"} for t in DENIED_TOOLS]
    rules += [{"action": "shell", "resource": c, "effect": "deny"} for c in DENIED_SHELL]
    rules += [{"action": "skill", "resource": n, "effect": "deny"}
              for n in denied_skills(task.spec.get("deny_skills"))]
    return {"instructions": [str(p) for p in files + ([note] if note else [])],
            "permissions": rules}
