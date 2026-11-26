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
# approves what the rules don't deny); `reply` how the driver answers in the chat (the mod's
# Bash, the OpenCode plugin's tool: opencode-plugin/chat.ts's REPLY_ID); `prompts` the kinds of
# prompt it raises in its terminal (OpenCode has no plan dialog: Plan is an agent).
DRIVERS: dict[str, dict[str, str]] = {
    "claude-code": {"who": "Claude", "app": "Claude Code", "listen": "and run /workbench",
                    "grant": "claude --permission-mode auto", "reply": "`wb chat reply`",
                    "prompts": "a permission, an AskUserQuestion question, a plan, an MCP server's form"},
    "opencode": {"who": "OpenCode", "app": "OpenCode", "listen": "and type in a session", "grant": "opencode --auto",
                 "reply": "the workbench_chat_reply tool", "prompts": "a permission, a question, an MCP server's form"},
    "any": {"who": "your driver", "app": "your driver", "listen": "and run /workbench", "grant": "",
            "reply": "`wb chat reply`", "prompts": "a permission, a question, a plan, an MCP server's form"},
}

# Each sentence once. Keys by family: `purpose` (the chat's first-open line, #1341), `rule.*` (the
# three rules), `state.*` (the driver's state line) and `next.*` (what to do about it), `wait.*`
# (what a waiting dialog is, for `state.waiting`'s {what}), `note.*` (the thread's waiting row,
# whose bytes the mod's tests and the phone's pushes know), `receipt.*` (a message's meta),
# `prompts` and `lane` (the Settings cards), `doc.rule` (the docs' block), `link.*` (wb's own
# lines on where the web app is: wb setup, wb chat on, wb doctor), `moved.deepmode` (`wb deepmode
# --moved`'s one line, which both hosts toast once: a 2.1 Dynamic workflows on starts 2.2 off,
# #1507), `refusal.*` (what a host tells
# its driver instead of a dialog nobody watches: the mod's AWAY_QUESTION, AWAY_PLAN and
# CONSENT_WAITING, the plugin's AWAY_QUESTION, through the generated mod/hooks/story.ts), and
# `skill.*` (the driver's own procedure: SKILL.md's and reference/driving.md's blocks, which the
# OpenCode plugin's skill.ts swaps for OpenCode's text from the same module), and `tasks.pickup`
# (the open-tasks context block's last line, both hosts' word for word: it named `wb watch`, which
# follows a task until it closes, so a driver on one at review waited out its shell's timeout,
# #1515, 2026-11-26; `wb wait` ends when the task needs the driver).
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
    "link.where": "Open the web app to message {who} from anywhere and change your settings: its link is on the "
                  "/workbench pane's modeline, and `wb chat url` prints it (the token in it is the key: keep it private).",
    "link.reach": "To open it on your phone over Tailscale: `wb chat reach tailnet`.",
    "moved.deepmode": "Deepmode is off: in 2.2, on runs every substantive request as a deep workflow (wb run) on "
                      "your agents, within the day cap, so 2.1's Dynamic workflows on didn't carry over. "
                      "/deepmode on turns it on.",
    "tasks.pickup": "Pick them up from here: `wb report <name>` for one, `wb wait <name>` to follow it until it "
                    "needs you.",
    "refusal.question": "The user is away: ask in the web chat ({reply}) and end your turn.",
    "refusal.plan": "Post the plan in the web chat and end your turn; approval is at the terminal.",
    "refusal.consent": "{Who} is waiting on a workbench dialog in the terminal: answer it there.",
    "skill.rule": "The terminal is primary: it is where you live (this machine, or over `ssh` + `tmux`), the work "
                  "is asked for and reviewed here, and a prompt you raise is answered here, never in the chat; "
                  "the web chat is a second lane for while the user is away, and most work still runs from it, "
                  "more so when they run you with permissions granted (`{grant}`). The settings live in the web "
                  "app's Settings (the link on the pane's modeline), the source of truth: what they say is what "
                  "wb runs by, and you never undo a change the user made there.",
    "skill.prompts": "Your prompts ({prompts}) are answered only in the terminal, never in the chat: the terminal "
                     "is where work gets done, the chat is for a message in a pinch. While one waits, the chat "
                     "shows one line at the top within seconds, \"{Who} is waiting for you in the terminal\" and "
                     "what it is (a permission's adds the way to run you with permissions granted, `{grant}`, to "
                     "copy); their phone is notified at once while they're away, after half a minute while they're "
                     "at the terminal; it clears when the prompt is answered.",
}

# The drivers a key is said for, when not all: the grant names a command only those two have.
# A refusal and a skill block are a host's own (OpenCode has no plan dialog to refuse).
HOSTS = ("claude-code", "opencode")
ONLY: dict[str, tuple[str, ...]] = {"next.grant": HOSTS, "refusal.question": HOSTS, "refusal.plan": ("claude-code",),
                                    "refusal.consent": HOSTS, "skill.rule": HOSTS, "skill.prompts": HOSTS,
                                    "tasks.pickup": HOSTS}

# What the hosts' generated mod/hooks/story.ts carries, filled for each host a key is said for:
# the mod (register.tsx) and the OpenCode plugin (chat.ts, skill.ts) import it.
HOST: tuple[str, ...] = ("refusal.question", "refusal.plan", "refusal.consent", "skill.rule", "skill.prompts",
                         "tasks.pickup")

# What the web app's generated story.ts carries: the keys its page reads (`data-story`, app.ts).
PAGE: tuple[str, ...] = ("purpose", "rule.terminal", "rule.chat", "rule.settings", "receipt.blocked", "prompts", "lane")

# The doc blocks gen_story.py fills: {repo path: ((key, driver), ...)}, each one's text between
# `<!-- story:KEY[:driver] -->` and `<!-- /story -->`.
# The skill's blocks are Claude Code's text; on OpenCode skill.ts puts the host module's in their place.
DOCS: dict[str, tuple[tuple[str, str], ...]] = {
    "README.md": (("doc.rule", "any"),),
    "skill/workbench/SKILL.md": (("skill.rule", "claude-code"),),
    "skill/workbench/reference/driving.md": (("skill.prompts", "claude-code"),),
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

