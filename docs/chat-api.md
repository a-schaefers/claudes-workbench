# The web chat's HTTP API

`wb chat serve` (or `wb chat on`, which starts it) is a JSON API behind a bearer token. The page
it serves (`workbench/chat_static/`) is one client of it; another agent with a token of its own is
another. No browser is needed: the server runs headless, and everything below works with curl.

This page is the reference for both. The internals are in ARCHITECTURE.md, *The web chat*.

**Handing it to another agent:** make it a token (`wb chat token add my-agent`) and point it at
`skill/agents/claude-workbench/` in this repository. That directory is a portable skill (the
agentskills.io format, `SKILL.md` with `name` and `description`), and its `scripts/ask_claude.py`
does the whole flow below with the standard library alone.

## Contents
- [Base URL and auth](#base-url-and-auth)
- [The agent flow, end to end](#the-agent-flow-end-to-end)
- [An agent's view](#an-agents-view)
- [Endpoints](#endpoints)
- [Rows: messages and asks](#rows-messages-and-asks)
- [The stream's events](#the-streams-events)
- [Versions](#versions)

## Base URL and auth

- **Base URL:** `http://localhost:1337` by default (the port is `wb chat serve --port`; `wb chat
  url` prints the page's link, whose host and port are the base). The server listens on
  localhost unless `~/.config/workbench/chat.json` says otherwise (`bind`: an address,
  `tailscale`, or `0.0.0.0`; `hosts`: more names to answer to). Reaching it from another machine
  is the user's own setup, for instance `tailscale serve --bg 1337` for https on the tailnet.
- **Host check:** a request whose `Host` header is a name the server doesn't answer to gets
  `403 {"error": "unknown host <host>: add it to chat.json's hosts"}`. An address it listens on
  always passes.
- **Auth:** every `/api/` path wants `Authorization: Bearer <token>`. A missing or wrong token
  is `401 {"error": "bad or missing token: open the link from `wb chat url`"}`.
- **Two kinds of token:**
  - **The user's** (`~/.config/workbench/chat-token`, or `$WB_CHAT_TOKEN`): everything here.
  - **An agent's** (`wb chat token add <name>`, printed once and kept only as its sha256 in
    `chat-agents.json` beside the user's token, mode 0600; `<name>` is 1 to 32 of `a-z`, `0-9`
    and `-`). It can check health, send, upload, wait for replies, and read its own conversation
    (*An agent's view*): never the user's messages, Claude Code's dialogs or the user's settings.
    - It can't answer Claude Code's dialogs (`/api/answer`), change the user's settings
      (`/api/prefs`), change notifications (`/api/push`), or mark the user away or what they have
      seen (`/api/active`, `/api/seen`); nor read a dialog (`/api/ask/<id>`), a push's summary
      (`/api/latest`), a screenshot (`/api/shot`) or an audio take (`/api/audio`). Each is `403
      {"error": "agent tokens can't …"}`.
    - Its sends are limited to `AGENT_SENDS_PER_MIN` (30) a minute, then `429 {"error",
      "retry_after": <seconds>}`.
    - `wb chat token list` names the agents; `wb chat token revoke <name>` ends one at once
      (the next request is 401; a held wait answers 401 and an open stream closes).
- **Agents never carry the user's authority.** An agent's message is stored with `role:
  "agent"` and `from: "<name>"`. Claude gets it as `[agent <name>] …`, not as the user's
  `[web chat] …`, and treats it as a teammate's request. Anything that needs the user's yes
  (spending, pushing, opening a pull request, deleting) waits for the user; Claude says so in
  its reply.
- **Agent access** is on whenever the chat is on and at least one agent token exists. There is no
  other switch.
- **Transport:** HTTP/1.0, one request per connection, no CORS. A connection that sends nothing
  for 30 s is closed. Every response has `Cache-Control: no-store` (but `/api/file`) and a
  `X-Workbench-Chat` header naming whose chat it is.

## The agent flow, end to end

```sh
export CLAUDE_WB_URL=http://localhost:1337 CLAUDE_WB_TOKEN=wba-…   # from wb chat token add my-agent
auth=(-H "Authorization: Bearer $CLAUDE_WB_TOKEN")

# 1. Is a Claude Code session in workbench mode listening? If not: do it yourself, or try later.
curl -s "${auth[@]}" "$CLAUDE_WB_URL/api/health"
# {"api": 1, "listening": true}

# 2. (optional) Upload a file under an id of your own (8 to 64 of A-Z a-z 0-9 -).
id=$(python3 -c 'import uuid; print(uuid.uuid4())')
curl -s "${auth[@]}" -T notes.txt -H "X-File-Name: notes.txt" -H "Content-Type: text/plain" \
  "$CLAUDE_WB_URL/api/upload/$id"
# {"upload": {"id": "…", "name": "notes.txt", "size": 812, "type": "text/plain", "path": "…"}, "new": true}

# 3. Send one request: the repo, the goal, what "done" means.
curl -s "${auth[@]}" -H 'Content-Type: application/json' \
  -d '{"text": "In ~/src/app: add a --json flag to `app list`. Done: tests pass, on a branch.", "files": ["'$id'"]}' \
  "$CLAUDE_WB_URL/api/send"
# {"message": {"id": "1795436900218-c2fe2e", "from": "my-agent", "role": "agent", …}, "listener": {…}, "state": {…}}

# 4. Wait for Claude's replies to it, until its turn is done.
curl -s "${auth[@]}" "$CLAUDE_WB_URL/api/wait?after=1795436900218-c2fe2e&timeout=300"
# {"replies": [{"id": "1795436900301-a1b2c3", "from": "claude", "text": "On it: …", "re": [...], "to": ["my-agent"]}],
#  "done": false, "listener": {…}, "waiting_on_user": false}

# 5. Keep reading from the last reply's id until "done" is true.
curl -s "${auth[@]}" "$CLAUDE_WB_URL/api/wait?after=1795436900301-a1b2c3&timeout=300"
# {"replies": [], "done": true, "listener": {…}, "waiting_on_user": false}

# 6. A later report (Claude hands work to local agents, ends its turn, and is woken when they
#    finish): wait for the next reply, however long the gap.
curl -s "${auth[@]}" "$CLAUDE_WB_URL/api/wait?after=1795436900301-a1b2c3&timeout=600&until=reply"
```

- **`listener: null`** (from `/api/send` or `/api/wait`) means no session is listening: the
  message waits in the queue, and is read once a session in workbench mode with the chat on
  listens. A message unread for 6 hours (`expire` in the state) is never passed on.
- **What `done` means:** the Claude turn that took the message (or that wrote the newest reply
  returned) has ended. Claude may still report later, after work it handed off: its reply says
  so, and `until=reply` waits for that report.
- **`waiting_on_user: true`**: Claude is paused on the user (a permission, a plan, a question, or
  a consent it asked in words). While Claude works on your request, Claude Code's dialogs go to
  the user on the web page, with a push, marked as being for your agent, and stay in the
  terminal: whichever the user answers first settles it. Only the user can answer it; your token
  can't. Keep waiting.
- **Follow-ups** are new messages: send again, and wait on the new id.

## An agent's view

An agent's token reads only its own conversation:
- **`/api/state` and `/api/stream`** carry `{api, messages, listener, uploads, expire, now}`, and
  `messages` holds only its own messages and Claude's rows whose `to` names it. No `asks`, `prefs`,
  `push`, `savings` or `build`: those are the user's. `/api/send`'s `state` is the same view.
- **`/api/wait`** on an id that is neither its own message nor a reply to it is `404 {"error":
  "no such message"}`.
- **Files:** `GET /api/upload/<id>` and `/api/file/<id>` answer only for a file it uploaded or one
  attached to a row in its view; any other id is 404, as if there were none. A `PUT` under an id
  someone else stored is `409 {"error": "that upload id is another sender's"}`, and a send can
  attach only its own uploads.
- **403:** `/api/latest`, `/api/ask/<id>`, `/api/shot`, `/api/audio`, and the user's `POST`s.
- **`/api/health`** and the `listener` are as for the user.

The user's own token reads everything, as below.

## Endpoints

Every `/api/` path below needs the bearer token. *User only* means an agent token gets 403.

### `GET /api/health`
`{"api": 1, "listening": bool}`. `listening` is true while a Claude Code session in workbench
mode, with the web chat on, is listening. An agent's first call.

### `GET /api/state`
The whole state the page draws (an agent's token gets its own view: *An agent's view*):
- `api`: this API's version (1).
- `messages`: the thread's last 200 rows (*Rows*), each of the user's and agents' with
  `received` (Claude's receipt, or null) and `expired: true` once unread past `expire`.
- `asks`: Claude Code's dialogs for the user (while they are away, or while Claude works on an
  agent's request): the newest 20 and every open one (*Rows*). A
  settled plan's text is cut at 1,200 characters with `detail.cut` (the whole is `/api/ask/<id>`).
- `listener`: the listening session `{session, cwd, pid, start, machine?, busy}`, or null.
- `prefs`: `{away: "ask"|"allow"}`.
- `push`: `{key: the VAPID public key or null, test: the last test push's time or null}`.
- `savings`: the week's `wb savings` totals and `score`.
- `uploads`: `{file, message, files}`: bytes per file, bytes per message, files per message.
- `build`: the page files' stamp (a page whose stamp differs reloads).
- `expire`: seconds a message waits before it is never passed on (21600).
- `now`: the server's time (seconds).

### `GET /api/stream`
Server-sent events (`text/event-stream`): see *The stream's events*; an agent's carry its own view.
At most 16 streams are open;
a 17th makes the oldest give way, and waits 5 s for a place before `503 {"error": "too many open
streams"}`. An agent's 17th never pushes another out: it is 503 at once. A client sends the bearer header, so it uses `fetch`, not `EventSource`.

### `GET /api/wait?after=<id>&timeout=<s>&until=done|reply`
A long poll for Claude's replies after the message `after` (a message's or a reply's id).
- `timeout`: 0 to 600 seconds (default 300). Behind a proxy with a shorter idle limit, ask less.
- `until`: `done` (default) returns at the first reply or once `done`; `reply` returns only at a
  new reply (for a later report after a turn already done).
- It also returns at once when nobody is listening (`listener: null`), at the timeout, and when
  Claude comes to wait on the user during it (`waiting_on_user` turns true; one already true at
  the start doesn't return it again).
- Response: `{"replies": [...], "done": bool, "listener": {...}|null, "waiting_on_user": bool}`.
  - `replies`: Claude's rows after `after` that are for the caller: their `to` names the agent,
    or, for the user's token, `to` names `"user"` or is absent.
  - `done`: a turn has ended since the newest reply returned, or, with none, since `after` (for a
    message to Claude: the turn that took it).
  - `waiting_on_user`: Claude is paused on the user: one of Claude Code's dialogs is open in the
    chat, or one in the terminal that the phone was told of. The work is waiting on the human,
    not stuck: keep waiting.
- Errors: 400 for a bad `after`, `timeout` or `until`; `404 {"error": "no such message"}` for an
  id not in the thread's recent part (the last 4 MiB), or, for an agent, not in its view; `503`
  when 16 waits are open already.

### `POST /api/send`
JSON body, `Content-Type: application/json` (else 415 `json only`), at most 32 KB (else 413 `too
long`):
- `text`: 1 to 8,000 characters (may be empty with `files`).
- `cid`: optional, 1 to 64 characters: your id for this send. A send whose `cid` the thread has
  returns that message again, so a retry never lands twice; one that another sender used is `409
  {"error": "that cid is another sender's"}`.
- `files`: optional, up to 10 upload ids (uploaded first; the same content twice is kept once).
- `about`: optional, the user's only, what it answers: `{task, n}` (a pair of `wb shot`
  screenshots) or `{audio: <batch>, n}` (a take of a `wb chat listen` shortlist). An agent's send
  with `about` is `400 {"error": "agent messages can't carry about"}`.

Response: `{"message": {...}, "listener": {...}|null, "state": {...}}`. Errors: 400 `a message is
1 to 8000 characters`, `bad cid`, `bad json`, `no such screenshots`, `no such take`, `no upload
<id>: it must be uploaded before the message` (for an agent, also another's upload), `files is a
list of up to 10 upload ids`, `agent messages can't carry about`; 409 for another sender's `cid`;
413 `too much for one message: …`; 429 for an agent over its limit.

### `PUT /api/upload/<id>`
Store a file under an id of yours (`[A-Za-z0-9-]{8,64}`), once: the same id again stores nothing
and answers with the first upload. The body is the raw bytes; headers: `Content-Length`
(required, else 411), `X-File-Name` (the name, URI-encoded), `Content-Type` (the file's type).
- Response: `{"upload": {id, name, size, type, path}, "new": bool}`.
- Errors: 400 `bad upload id`, `that file name can't be saved` (a path, `.` or `..`), `the upload
  was cut short: it is tried again`; 409 when something else is where the file goes, or `that
  upload id is another sender's` (an agent's PUT under an id someone else stored); 413 `too big:
  <size>, the limit is <limit>` (25 MB by default, `upload_mb` in chat.json), answered before the
  body is read; 507 or 500 `not saved: …` with `"final": true` when trying again can't help.

### `GET /api/upload/<id>`
`{"upload": {id, name, size, type, path}}` when stored, else `404 {"error": "no such upload"}`
(for an agent, also when it isn't its own: *An agent's view*).

### `GET /api/file/<id>`
The upload's bytes: a PNG, JPEG, GIF, WebP or AVIF image as itself, anything else as an
`application/octet-stream` attachment. Cached for a year (an id's bytes never change). 404 `no
such file` (for an agent, also when it isn't its own).

### `GET /api/ask/<id>` (user only)
One of Claude Code's dialogs whole, answered or not: `{"ask": {...}}`, else 404 `no such prompt`.

### `GET /api/latest` (user only)
What a push notification shows: `{now, asks: [the newest open dialog, a plan without its text],
messages: [Claude's last message for the user, or notice], push: {test}}`. A reply only to
agents is never pushed.

### `GET /api/shot?task=<task>&n=<n>&side=base|candidate` (user only)
One side of a `wb shot` pair as a PNG (at most 16 MB). 410 `expired` once pruned with its task,
else 404 `no such screenshot`.

### `GET /api/audio?batch=<batch>&n=<n>` (user only)
One take of a `wb chat listen` shortlist, as its audio type (at most 64 MiB). 404 `no such take`.

### `POST /api/answer` (user only)
Answer a dialog: `{id, answer: "allow"|"deny", note?: ≤ 2000 characters, answers?: {question:
answer} (≤ 4, a question's only)}` → `{"ask": the answer that stands, "state": {...}}`. 400 for a
bad body or answers that don't fit the question; 404 `no such prompt`.

### `POST /api/prefs` (user only)
`{away: "ask"|"allow"}` (allow: Claude's permission prompts are allowed while the user is away) →
`{"state": {...}}`. 400 otherwise.

### `POST /api/active` (user only)
`{}`: a page is in use on this device, so the user is away from the terminal while the chat is
on → `{}`.

### `POST /api/seen` (user only)
`{ids: [up to 200 ids], endpoint?: this device's push URL}`: what a page showed, so no push is
sent for it (to every device, or with `endpoint` to that one) → `{}`.

### `POST /api/push` (user only)
`{endpoint: a push service's https URL, on: true|false}` subscribes this device or stops it →
`{"on": bool}`; `{test: true}` pushes every device → `{sent, ok, hint, reasons}` (`hint`:
`cert`, `clock`, `unreached`, `refused`, or "" when one went through).

### The page itself (no token)
`GET /` (or `/index.html`) and the page's files (`/app.js`, `/app.css`, `/sw.js`, icons,
`/manifest.webmanifest`); anything else is 404.

Any other `/api/` method or path is `404 {"error": "not found"}`. Every `POST` takes JSON as
above: a bad `Content-Length` is 400 `bad length`, an unparsable body 400 `bad json`.

## Rows: messages and asks

A **message** (`messages`, `replies`, `message`):
- `id` (`<ms>-<6 hex>`), `ts` (seconds), `text`.
- `from`: `"user"`, `"claude"`, `"notice"` (a dialog waits in the terminal), or an agent's name
  with `role: "agent"`.
- On a message to Claude: `cid?`, `files?: [{id, name, size, type, path}]`, `about?`, and in the
  state `received: {id, ts, by, how: "now"|"queued"} | null`, `expired?`.
- On Claude's reply: `re?: [message ids]` (what it answers: `wb chat reply --to`, else the
  messages Claude was given in that turn, the user's alone when the user's are among them),
  `to?: ["user" | agent names]` (whose those messages are, or `"user"` for a question to the
  user, `wb chat reply --to user`; absent means the user's), `shots?`, `audio?`.

An **ask** (`asks`, `/api/ask/<id>`): `{id: "ask-…", ts, tool, text, kind:
"permission"|"plan"|"question", detail?, for?: [agent names], answer: null|"allow"|"deny"|"expired",
answered?, by?, note?, answers?, stale?}`. `for`: Claude was working on those agents' request
(the page says "for <name>"); `by: "interrupted"` on an expired one: settled in the terminal.

## The stream's events

- `event: state`, `data: <the whole state, as GET /api/state>`: first, on every connection (so a
  reconnect is a resync).
- `event: patch`, `data: {...}`: what changed since: `messages` and `asks` as `{put: [rows new or
  changed], drop: [ids gone]}` (each only when it changed), every other field of the state whole,
  and `now`. Merge rows by `id`.
- `: ping` (a comment) after 15 s with nothing to send.

## Versions

`api` in `/api/state` and `/api/health` is this API's version. A change that breaks a client
bumps it; adding a field or an endpoint doesn't.
- **1** (2026-11-23): agent tokens, `/api/health`, `/api/wait`, `role`/`re`/`to` on rows, an
  agent's view of its own conversation, `for` on asks.
