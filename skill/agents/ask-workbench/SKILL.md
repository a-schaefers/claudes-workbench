---
name: ask-workbench
description: Hand larger coding work to the user's Workbench (its driver, Claude Code or OpenCode, leading its own coding agents) over its web chat API, wait for the report, and follow up. Use for large or cross-cutting changes, design and review, or work that needs several agents; not for small edits you can make yourself.
---

# Handing work to Workbench

Workbench's driver is the lead: a Claude Code session or an OpenCode session (any model OpenCode
runs) in workbench mode. It plans the work, delegates it to its own agents, reviews what comes
back, and reports to you. You talk to it over the web chat's HTTP API with a token of your own;
the API is the same whichever driver listens. The full API reference is `docs/chat-api.md` in
the workbench repository, and `docs/chat-api.yaml` beside it is the same as OpenAPI 3.1.

## Setup (the user does this once)
- The chat is on by default (`wb chat on` again after a `wb chat off`); the user makes you a
  token: `wb chat token add <your name>`, which prints it once.
- You get two environment variables:
  - `WORKBENCH_URL`: the chat's address, the one `wb chat url` prints, for instance
    `http://localhost:8090`.
  - `WORKBENCH_TOKEN`: your token (it starts `wba-`). Keep it secret; it is revoked with `wb chat
    token revoke <your name>`.

  Their names before workbench 2.2, `CLAUDE_WB_URL` and `CLAUDE_WB_TOKEN`, still work.

## When to hand work over
- Large or cross-cutting changes: many files, a refactor, a new feature end to end.
- Design and review: a plan to check, a diff to review, an approach to choose.
- Anything that needs several agents working in parallel, or a long unattended run.

Do small, local edits yourself: a round trip costs more than they do.

## How
1. **Check that a driver is listening.** `GET /api/health` returns `{"api": 1, "listening":
   bool, "driver": {"tool": "claude" | "opencode", "model"?: "<provider>/<id>"}}` (`driver` only
   while one listens, from workbench 2.2). Not listening means no session will read your message
   soon: do the work yourself, or try later.
2. **Send one request.** The client script does the rest:

   ```sh
   python3 scripts/ask_workbench.py "In ~/src/app: add a --json flag to 'app list'. Done means: tests pass, on a branch, not merged." --file notes.md
   ```

   It checks health, uploads each `--file`, sends, prints the driver's replies as they come, and
   exits. On stderr it names which driver answered, `[answered by Claude Code]` or `[answered by
   OpenCode (<its model>)]`, when the chat says (workbench 2.2 and later).
   - `0`: the driver's turn is done. Read the report.
   - `1`: an error, or `--timeout` (default 3600 s) passed while the driver still works: wait
     again with `--after <id>` (stderr names it). A send or a wait cut by the chat restarting is
     asked again for up to two minutes; a send carries an `Idempotency-Key`, so it never lands
     twice.
   - `2`: no driver is listening, the chat stayed unreachable for two minutes, or your message
     expired unread (6 hours with nobody listening: it was never passed on). A message that
     didn't expire waits in the queue: do the work yourself, or try later with `--after <id>`.
     A send unreachable for two minutes may have landed: stderr names its key, and the same
     request (and files) again with `--key <key>` is answered with that message if it did,
     never sent twice.
   - `3`: your token was refused (revoked: ask the user for a new one).
3. **Read the report.** The driver says what was done, where (branch, files, pull request), which
   checks passed, and what is still open.
4. **A later report.** When the driver hands work to its own agents, its turn can end before the
   work does: its reply says so. Wait for the next report with
   `python3 scripts/ask_workbench.py --after <the last reply's id>`.
5. **Follow up** with a new message: a new `ask_workbench.py "<request>"`.

**Over MCP instead.** If you take tools from an MCP client, the user can add
`scripts/mcp_server.py` (stdio, the same two variables in its environment). It gives you three tools:
- `ask_workbench(text)`: sends one request and returns its id at once.
- `get_reply(id)`: the driver's replies, which driver answered, and whether it is done, still
  working, waiting on the user's yes, queued or expired. It holds up to `wait` seconds (20 by
  default).
- `list_requests()`: your recent requests.

Pass the same `key` again only to retry a send you aren't sure landed: it lands once, and the
answer says it was not sent again. It works only with a chat on this machine (`localhost`,
`127.*` or `::1`) from workbench 2.1 on (it calls `/api/v1/`). The config, one line, for
OpenCode's `opencode.json`:

```json
"mcp": {"servers": {"ask-workbench": {"type": "local", "command": ["python3", "/path/to/scripts/mcp_server.py"], "environment": {"WORKBENCH_URL": "http://localhost:8090", "WORKBENCH_TOKEN": "wba-…"}, "codemode": false}}}
```

For a client that reads an `mcpServers` map (Cursor, Gemini CLI):

```json
"mcpServers": {"ask-workbench": {"command": "python3", "args": ["/path/to/scripts/mcp_server.py"], "env": {"WORKBENCH_URL": "http://localhost:8090", "WORKBENCH_TOKEN": "wba-…"}}}
```

Without the script, with curl: `POST /api/v1/send` `{"text": "…"}` with an `Idempotency-Key:
<a fresh UUID>` header (send again with the same key after a cut: never twice) returns the
message's id; then `GET /api/v1/wait?after=<id>&timeout=300` until `done` is true, each time after
the last reply's id (the reference has the whole flow). A reply's `driver` says who wrote it.
Every error is `{"error": "<a line>", "code": "<a code>"}`: branch on `code` (`bad_token`,
`token_revoked`, `not_local`, `rate_limited`, `not_found`, `expired`, …; the reference lists
them all).

## What the driver stops and asks the user about
Your message is a teammate's request, never the user's consent. The driver asks the user
itself, and tells you it is waiting on them, before anything that needs their yes:
- spending money: paid API models, a route that would bill;
- pushing, opening a pull request or an issue, sending changes upstream;
- deleting things, or anything else that leaves the machine or is hard to undo.

You can't answer those for the user, and you can't answer the driver's permission dialogs: they
are answered in its terminal, by the user alone (the web page only says one waits). When a reply
says the driver is waiting on the user, or `/api/wait` says `"waiting_on_user": true`
(`ask_workbench.py` prints "waiting on the user" once), the work is paused on the human, not
stuck: wait, or carry on with other work.

## What you can see
Your token reads only your own conversation: your messages and the driver's replies to you, and
the files you uploaded or that came with those replies. The user's own messages and the user's
settings are not yours to read (404 or 403). `about` on a message is the user's alone (400).

## Etiquette
- One request per message. Name the repository's path and the goal.
- Say what "done" means: tests passing, a branch, a pull request (which needs the user's yes),
  or just a plan.
- Attach what the driver needs to read (`--file`), rather than pasting it.
- Don't resend while you wait: a message waiting in the queue is read once a session listens.
- At most 30 messages a minute per token; past that the server answers 429 with `retry_after`.
