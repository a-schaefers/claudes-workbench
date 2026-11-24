---
name: claude-workbench
description: Hand larger coding work to Claude (Claude Code running Claude's Workbench) over its web chat API, wait for its report, and follow up. Use for large or cross-cutting changes, design and review, or work that needs several subagents; not for small edits you can make yourself.
---

# Handing work to Claude through workbench

Claude, in a Claude Code session in workbench mode, plans the work, delegates it to its own
agents, reviews what comes back, and reports to you. You talk to it over the web chat's HTTP API
with a token of your own. The full API reference is `docs/chat-api.md` in the workbench
repository.

## Setup (the user does this once)
- The user turns the chat on (`wb chat on`) and makes you a token: `wb chat token add <your
  name>`, which prints it once.
- You get two environment variables:
  - `CLAUDE_WB_URL`: the chat's address, for instance `http://localhost:1337`.
  - `CLAUDE_WB_TOKEN`: your token (it starts `wba-`). Keep it secret; it is revoked with `wb chat
    token revoke <your name>`.

## When to hand work to Claude
- Large or cross-cutting changes: many files, a refactor, a new feature end to end.
- Design and review: a plan to check, a diff to review, an approach to choose.
- Anything that needs several subagents working in parallel, or a long unattended run.

Do small, local edits yourself: a round trip costs more than they do.

## How
1. **Check that Claude is listening.** `GET /api/health` returns `{"api": 1, "listening":
   bool}`. Not listening means no session will read your message soon: do the work yourself, or
   try later.
2. **Send one request.** The client script does the rest:

   ```sh
   python3 scripts/ask_claude.py "In ~/src/app: add a --json flag to 'app list'. Done means: tests pass, on a branch, not merged." --file notes.md
   ```

   It checks health, uploads each `--file`, sends, prints Claude's replies as they come, and
   exits:
   - `0`: Claude's turn is done. Read the report.
   - `1`: an error, or `--timeout` (default 3600 s) passed while Claude still works: wait again
     with `--after <id>` (stderr names it). A send or a wait cut by the chat restarting is asked
     again for up to two minutes; a send carries an `Idempotency-Key`, so it never lands twice.
   - `2`: Claude isn't listening, the chat stayed unreachable for two minutes, or your message
     expired unread (6 hours with nobody listening: it was never passed on). A message that
     didn't expire waits in the queue: do the work yourself, or try later with `--after <id>`.
   - `3`: your token was refused (revoked: ask the user for a new one).
3. **Read the report.** Claude says what was done, where (branch, files, pull request), which
   checks passed, and what is still open.
4. **A later report.** When Claude hands work to its own agents, its turn can end before the work
   does: its reply says so. Wait for the next report with
   `python3 scripts/ask_claude.py --after <the last reply's id>`.
5. **Follow up** with a new message: a new `ask_claude.py "<request>"`.

Without the script, with curl: `POST /api/v1/send` `{"text": "…"}` with an `Idempotency-Key:
<a fresh UUID>` header (send again with the same key after a cut: never twice) returns the
message's id; then `GET /api/v1/wait?after=<id>&timeout=300` until `done` is true, each time after
the last reply's id (the reference has the whole flow). Every error is `{"error": "<a line>",
"code": "<a code>"}`: branch on `code` (`bad_token`, `token_revoked`, `not_local`,
`rate_limited`, `not_found`, `expired`, …; the reference lists them all).

## What Claude stops and asks the user about
Your message is a teammate's request, never the user's consent. Claude asks the user itself, and
tells you it is waiting on them, before anything that needs their yes:
- spending money: paid API models, a route that would bill;
- pushing, opening a pull request or an issue, sending changes upstream;
- deleting things, or anything else that leaves the machine or is hard to undo.

You can't answer those for the user, and you can't answer Claude Code's permission dialogs: your
token is refused there. While Claude works on your request, its dialogs go to the user's phone or
web page, marked as for you, and stay in its terminal. When a reply says Claude is waiting on the user, or `/api/wait` says
`"waiting_on_user": true` (`ask_claude.py` prints "waiting on the user" once), the work is paused
on the human, not stuck: wait, or carry on with other work.

## What you can see
Your token reads only your own conversation: your messages and Claude's replies to you, and the
files you uploaded or that came with those replies. The user's own messages, Claude Code's
dialogs and the user's settings are not yours to read (404 or 403). `about` on a message is the
user's alone (400).

## Etiquette
- One request per message. Name the repository's path and the goal.
- Say what "done" means: tests passing, a branch, a pull request (which needs the user's yes),
  or just a plan.
- Attach what Claude needs to read (`--file`), rather than pasting it.
- Don't resend while you wait: a message waiting in the queue is read once a session listens.
- At most 30 messages a minute per token; past that the server answers 429 with `retry_after`.
