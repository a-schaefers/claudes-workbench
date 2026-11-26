"""The story (#1355): every sentence the chat, the web app and the docs tell the user about where
the driver lives and what the chat is for, in one place, with the driver's name filled in. The
owner's rule (2026-11-24, on #1340 and #1355): the terminal is where the driver lives, and a
prompt it raises is answered there; most work can still run from the chat, more so with
permissions granted; the web app's Settings are the source of truth. The README, the page, the
chat's waiting line and its state line said it in their own words, and drifted: the page said
"Run Claude" to an OpenCode user, and the docs said the chat was off by default after 2.0 turned
it on (#1281).

`TEXTS` holds each sentence once, a template whose `{field}`s come from `DRIVERS` (the driver's
`who`, `app`, `grant`) and the caller (`where`, `what`, `tool`); `{Field}` is the same value with
its first letter capitalized. `text(key, driver)` fills one: Python's callers (chat.py's notice and
state lines) call it, and `scripts/gen_story.py` writes the same tables and `fill` into the web
app's `chat_static/src/story.ts` and the doc blocks (`<!-- story:KEY[:driver] -->`), which its
`--check` (and tests/test_story.py) holds to this file. One fixture, tests/fixtures/story/fill.json,
holds Python's fill and the page's to the same answers. No sentence lives in the kernel
(kernel/BOUNDARY.md: wording is never the kernel's).

Standard library only, nothing at import but the tables.
"""
from __future__ import annotations

import re

# The drivers by the name the story uses: `claude-code`, `opencode`, and `any` (no driver known:
# the page before a session listened, the README). `who` is the name the user talks to; `app` the
# program they run; `listen` how a session there starts listening to the chat (Claude Code's in
# workbench mode; OpenCode's plugin, the session the user last typed in, #1319); `grant` the way
# to run it with permissions granted, said only for a
# permission (Claude Code's `--permission-mode auto` still asks on some calls, which the line
# doesn't hide: it says "with permissions granted", never "never asks"; OpenCode's `--auto`
# approves what the rules don't deny).
DRIVERS: dict[str, dict[str, str]] = {
    "claude-code": {"who": "Claude", "app": "Claude Code", "listen": "and run /workbench",
                    "grant": "claude --permission-mode auto"},
    "opencode": {"who": "OpenCode", "app": "OpenCode", "listen": "and type in a session", "grant": "opencode --auto"},
    "any": {"who": "your driver", "app": "your driver", "listen": "and run /workbench", "grant": ""},
}

# Each sentence once. Keys by family: `purpose` (the chat's first-open line, #1341), `rule.*` (the
# three rules), `state.*` (the driver's state line) and `next.*` (what to do about it), `wait.*`
# (what a waiting dialog is, for `state.waiting`'s {what}), `note.*` (the thread's waiting row,
# whose bytes the mod's tests and the phone's pushes know), `receipt.*` (a message's meta),
# `prompts` and `lane` (the Settings cards), `doc.rule` (the docs' block).
TEXTS: dict[str, str] = {
    "purpose": "Message {who} from anywhere: most work can run from here. A prompt {who} raises waits in "
               "its terminal (`ssh` + `tmux`); run {who} with permissions granted to skip those. "
               "Your settings live here too.",
    "rule.terminal": "The terminal is where {who} lives, on this machine or over `ssh` + `tmux`: "
                     "a prompt {who} raises is answered there, never in the chat.",
    "rule.chat": "Most work can still run from the chat, more so with {who} run with permissions granted.",
    "rule.settings": "The web app's Settings are the source of truth: {who} changes one only when you ask and say yes.",
    "state.none": "No session is listening: messages wait.",
    "state.idle": "{Who} is listening in {where}.",
    "state.working": "{Who} is working in {where}.",
    "state.waiting": "{Who} is waiting for you in the terminal: {what}.",
    "next.none": "Start {app} in a terminal {listen}: it answers here.",
    "next.idle": "Send a message: {who} answers here.",
    "next.working": "A message now reaches {who} while it works.",
    "next.waiting": "Answer it in the terminal; a message sent now waits behind it.",
    "next.grant": "To work from the chat, run {app} with permissions granted: `{grant}`.",
    "wait.permission": "a permission for {tool}",
    "wait.question": "a question",
    "wait.plan": "a plan to approve",
    "wait.elicitation": "a form from {tool}",
    "wait.consent": "a yes for {tool}",
    "note.waiting": "{Who} is waiting for you in the terminal.",
    "note.grant": "Run it with permissions granted to work from the chat.",
    "receipt.blocked": "Waiting behind the prompt in the terminal",
    "lane": "A second lane to {who}, not a copy of the terminal. What you send here reaches the session "
            "listening on your computer; {who} answers here with what matters: results, questions, its plans. "
            "Its prompts wait in the terminal, and the terminal's full output stays on the computer. "
            "Ask {who} to keep you posted here, and it will.",
    "prompts": "{Who}'s prompts (a permission, a question, a plan) are answered in the terminal, never here. "
               "While one waits, the line at the top of the chat says so. Run {who} with permissions "
               "granted to work from the chat.",
    "doc.rule": "The terminal is where {who} lives (locally, or over `ssh` + `tmux`), and a prompt it raises "
                "is answered there. Most work can still run from the web app's chat, more so with {who} run "
                "with permissions granted. The web app's Settings are the source of truth: {who} changes one "
                "only on your ask and yes, and never undoes a change you made.",
}

# The drivers a key is said for, when not all: the grant names a command only those two have.
ONLY: dict[str, tuple[str, ...]] = {"next.grant": ("claude-code", "opencode")}

# What the web app's generated story.ts carries: the keys its page reads (`data-story`, app.ts).
PAGE: tuple[str, ...] = ("purpose", "rule.terminal", "rule.chat", "rule.settings", "receipt.blocked", "prompts", "lane")

# The doc blocks gen_story.py fills: {repo path: ((key, driver), ...)}, each one's text between
# `<!-- story:KEY[:driver] -->` and `<!-- /story -->`.
DOCS: dict[str, tuple[tuple[str, str], ...]] = {
    "README.md": (("doc.rule", "any"),),
}

FIELD = re.compile(r"\{([A-Za-z_]+)\}")


def fill(t: str, fields: dict[str, str], partial: bool = False) -> str:
    """Template ``t`` with each `{name}` from ``fields``, `{Name}` capitalized. A name ``fields``
    lacks raises KeyError, or with ``partial`` stays as written (the page fills it later)."""
    def one(m: re.Match) -> str:
        name = m.group(1)
        low = name[:1].lower() + name[1:]
        if low in fields:
            v = str(fields[low])
            return v[:1].upper() + v[1:] if name != low else v
        if partial:
            return m.group(0)
        raise KeyError(name)
    return FIELD.sub(one, t)


def text(key: str, driver: str = "any", **fields) -> str:
    """Sentence ``key`` for ``driver`` (DRIVERS), with ``fields`` beside the driver's own. KeyError
    for no such key or driver, or a field left unfilled; ValueError for a driver the key isn't
    said for (ONLY)."""
    t, d = TEXTS[key], DRIVERS[driver]
    if key in ONLY and driver not in ONLY[key]:
        raise ValueError(f"{key} is said only for {', '.join(ONLY[key])}")
    return fill(t, {**d, **{k: str(v) for k, v in fields.items()}})


def driver_of(tool: str | None) -> str:
    """The story's driver for a listener's or a row's `driver.tool` (`claude`, `opencode`)."""
    return {"claude": "claude-code", "opencode": "opencode"}.get(tool or "", "any")

