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
- [Rows: messages](#rows-messages)
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
    (*An agent's view*): never the user's messages or the user's settings.
    - It works on an allow-list: `GET /api/health`, `/api/wait`, `/api/state`, `/api/stream`,
      `/api/upload/<id>` and `/api/file/<id>`; `PUT /api/upload/<id>`; `POST /api/send`. Every
      other path and method is `403 {"error": "agent tokens can't …"}`: `use <path>` for most,
      the older wording where there was one (`change notifications` for `/api/push`,
      `mark the user away` for `/api/active`, `mark what the user has seen` for `/api/seen`,
      `read the user's notifications` for `/api/latest`, `read screenshots` for `/api/shot`,
      `read audio takes` for `/api/audio`, `read tasks` for `/api/task`, `transcribe the user's
      voice clips` for `/api/transcribe/<id>`). An
      endpoint added later is the user's alone unless this list names it.
    - It works only from this machine: a request with an agent token from any other machine
      (a tailnet or LAN peer, when the chat is bound there for the user's phone) is ``403
      {"error": "agent tokens work only from this machine, at the address `wb chat url`
      prints"}``. On a chat bound only to the tailnet or a LAN IP, an agent here uses that address
      (localhost isn't served). The user's token is unaffected.
    - Its sends are limited to `AGENT_SENDS_PER_MIN` (30) a minute, then `429 {"error",
      "retry_after": <seconds>}`.
    - `wb chat token list` names the agents; `wb chat token revoke <name>` ends one at once
      (the next request is 401; a held wait answers 401 and an open stream closes).
- **Agents never carry the user's authority.** An agent's message is stored with `role:
  "agent"` and `from: "<name>"`. Claude gets it as `[agent <name>] …`, not as the user's
  `[web chat] …`, and treats it as a teammate's request. Anything that needs the user's yes
  (spending, pushing, opening a pull request, deleting) waits for the user; Claude says so in
  its reply.
- **Agent access** is on while the chat's server runs and at least one agent token exists. There
  is no other switch. With the chat off (`wb chat off`) no session listens: `/api/health` says
  `listening: false`, and a message sent waits, unread, until a session listens with the chat on,
  or is never passed on past 6 hours. `wb chat off` stops a server it started; one a service keeps
  up runs on until the service is stopped.
- **CSRF and origin:** the token rides an `Authorization: Bearer` header, never a cookie, and
  the server sends no CORS headers. A JSON body (every `POST`) forces a browser's preflight,
  which the server never answers, so a page on another site can't write; the Host check stops
  DNS rebinding. Nothing more is added.
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
  returned) has ended; another Claude Code session's turn never counts. Claude may still report later, after work it handed off: its reply says
  so, and `until=reply` waits for that report.
- **`waiting_on_user: true`**: Claude is paused on the user (a permission, a plan, a question, or
  a consent it asked in words). Claude Code's dialogs are answered in its terminal, by the user
  alone; the web page shows a line while one waits. Keep waiting.
- **Follow-ups** are new messages: send again, and wait on the new id.

## An agent's view

An agent's token reads only its own conversation:
- **`/api/state` and `/api/stream`** carry `{api, messages, listener, uploads, expire, now}`, and
  `messages` holds only its own messages and Claude's rows whose `to` names it. No
  `push`, `savings`, `build`, `tasks` or `dictate`: those are the user's. `/api/send`'s `state` is the same view.
- **`/api/wait`** on an id that is neither its own message nor a reply to it is `404 {"error":
  "no such message"}`.
- **Files:** `GET /api/upload/<id>` and `/api/file/<id>` answer only for a file it uploaded or one
  attached to a row in its view; any other id is 404, as if there were none. A `PUT` under an id
  someone else stored is `409 {"error": "that upload id is another sender's"}`, and a send can
  attach only its own uploads.
- **403:** every path outside the allow-list (*Base URL and auth*): `/api/latest`, `/api/shot`, `/api/audio`, `/api/task`, the admin endpoints (`/api/settings`, `/api/stats`,
  `/api/top`), and the user's `POST`s (`/api/transcribe/<id>` among them).
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
  `received` (Claude's receipt, or null) and `expired: true` once unread past `expire`. A notice
  (a dialog waiting in the terminal) is left out once it settles.
- `listener`: the listening session `{session, cwd, pid, start, machine?, parent?, busy}`, or null.
- `push`: `{key: the VAPID public key or null, test: the last test push's time or null}`.
- `savings`: the week's `wb savings` totals and `score`, and `api`: the week's API tasks apart
  (`tasks`, `cost` in dollars, `unknown`, `plan`, `trials`, `claude`, `self_est`, `by_provider`,
  `by_day`, `prices`: `catalog` (an estimate, shown `≈`), `yours` or `provider`, the total's and each
  provider's, and `names`: each provider's name), never part of the score.
- `uploads`: `{file, message, files}`: bytes per file, bytes per message, files per message.
- `build`: the page files' stamp (a page whose stamp differs reloads).
- `tasks`: the open tasks, for the strip over the thread: `[{name, phase, model, via: "gpu"|"api",
  provider_name}]` (none accepted, discarded or parked by `wb stop --all`).
- `dictate`: true while the computer has a listener (a speech recognizer in `wb audio`'s
  audio.json) for `POST /api/transcribe/<id>`: the page shows its mic only then.
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
  - `done`: a turn has ended since the newest reply returned (one of the session that wrote it:
    its `turn_by`), or, with none, since `after` (for a message to Claude: the turn that took it).
  - `waiting_on_user`: Claude is paused on the user: one of Claude Code's dialogs waits in the
    terminal, and the phone was told of it. The work is waiting on the human, not stuck: keep
    waiting.
- Errors: 400 for a bad `after`, `timeout` or `until`; `404 {"error": "no such message"}` for an
  id not in the thread's recent part (the last 4 MiB), or, for an agent, not in its view; `503`
  when 16 waits are open already.
- A wait can be cut: the server restarts itself on an update (a reset connection, or 502 to 504
  from a proxy in front meanwhile), and `503` passes. Wait again, after a short pause, with the
  same `after`: nothing was lost (`ask_claude.py` does for up to two minutes in a row, then exits
  2 naming `--after`: a chat down that long is off, asleep or out of reach, not restarting).

### `POST /api/send`
JSON body, `Content-Type: application/json` (else 415 `json only`), at most 32 KB (else 413 `too
long`):
- `text`: 1 to 8,000 characters (may be empty with `files`).
- `cid`: optional, 1 to 64 characters: your id for this send. A send whose `cid` the thread has
  returns that message again, so a retry never lands twice; one that another sender used is `409
  {"error": "that cid is another sender's"}`.
- `files`: optional, up to 10 upload ids (uploaded first; the same content twice is kept once).
- `about`: optional, the user's only, what it answers: `{task, n}` (a pair of `wb shot`
  screenshots), `{audio: <batch>, n}` (a take of a `wb chat listen` shortlist) or `{task}` alone
  (a task, from the page's task sheet: any task wb has, but a bench replay; Claude gets `(about
  task <task>)` and decides whether to steer it). An agent's send with `about` is `400 {"error":
  "agent messages can't carry about"}`.

Response: `{"message": {...}, "listener": {...}|null, "state": {...}}`. Errors: 400 `a message is
1 to 8000 characters`, `bad cid`, `bad json`, `no such screenshots`, `no such take`, `no such task`, `no upload
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

### `GET /api/latest` (user only)
What a push notification shows: `{now, messages: [Claude's last message for the user, or an
open notice], push: {test}}`. A reply only to
agents is never pushed.

### `GET /api/shot?task=<task>&n=<n>&side=base|candidate` (user only)
One side of a `wb shot` pair as a PNG (at most 16 MB). 410 `expired` once pruned with its task,
else 404 `no such screenshot`.

### `GET /api/audio?batch=<batch>&n=<n>` (user only)
One take of a `wb chat listen` shortlist, as its audio type (at most 64 MiB). 404 `no such take`.

### `GET /api/task?name=<task>&part=summary|diff|log` (user only)
One open task, as `wb status`, `wb report`, `wb diff` and `wb log` show it: the page's task sheet.
Read only, from the task's files and its worktree; it never starts OpenCode. Only a task the
`tasks` strip lists (open, not parked, not a bench replay); any other is `404 {"error": "no such
open task"}`. Every answer is `{task, part, …, now}`:
- `summary` (the default): `phase`, `round`, `step` (above 1 after `wb next`), `last`,
  `created_at` and `updated_at` (seconds; age and idle are `now` minus them), `model`, `via`,
  `provider_name`, `report` (the round's summary.md, or what stands instead, cut at 64 KB with
  `report_cut: true`) and `verify` (the regression check's lines, `[]` for none).
- `diff`: against `base` (the task's base commit, or `HEAD` after `wb next`: the current step,
  as `wb diff`): `stat` (`wb diff --stat`) and `files` (`[{path, added, deleted}]`, null counts
  for a binary file). With `full=1`, `diff` instead of the stat: the whole diff, or with
  `path=<one of files>` that file's (else 404 `no such file in the diff`), each line cut at
  2,000 characters (`lines_cut`: how many), or `diff: null` with `cut: true` past 256 KB. A task
  with no worktree yet, or a git failure, answers `note` and no files.
- `log`: `rows`, the last `n` (1 to 200, default 50) of `wb log -v`'s rows, each `{ts, round,
  kind, text}` (`wb log`'s words after the time, cut at 2,000 characters; `ok` for a tool call),
  and `more: true` when older ones were left out.

400 for another `part` or a bad `n`. All of it is text the agent wrote: show it as text.

### `POST /api/transcribe/<id>` (user only)
`{}`: the words of an upload that is a voice clip (`audio/*`, `video/*` or
`application/octet-stream`), heard by the computer's listener → `{"text": "..."}`, for the user to
edit and send as a message (the clip itself is never sent to Claude). The words are kept with the
upload, so asking again never transcribes twice. Errors: 404 `no such upload`, `dictation is off:
…` (no listener); 415 `not a voice clip (<type>)`; 409 `{error, busy: true, why}` while a task
holds the GPU a GPU listener needs (it never waits for it: ask again later, as the page does);
503 `{error, retry: true}` while a url listener's server doesn't answer (ask again too); 502 `the
listener failed: …` (it ran and failed). It answers once the listener is done: give it minutes,
not seconds.

### `POST /api/active` (user only)
`{}`: a page is in use on this device, so the user is away from the terminal while the chat is
on → `{}`. A request whose Host is `localhost`, `127.*` or `::1` (a page on the computer itself)
records only that a page is in use, never away (#1374).

### `POST /api/seen` (user only)
`{ids: [up to 200 ids], endpoint?: this device's push URL}`: what a page showed, so no push is
sent for it (to every device, or with `endpoint` to that one) → `{}`.

### `POST /api/push` (user only)
`{endpoint: a push service's https URL, on: true|false}` subscribes this device or stops it →
`{"on": bool}`; `{test: true}` pushes every device → `{sent, ok, hint, reasons}` (`hint`:
`cert`, `clock`, `unreached`, `refused`, or "" when one went through).

### `GET /api/settings` (user only)
The web app's Settings: `{"rows": [...], "ts": <seconds>}`. Each row is `{key, group, label, kind,
…}`: `group` one of `shared`, `gpu`, `api`, `terminal`, `experimental` (in that order); `kind` one
of `choice` (`value`, `choices: [{value, label, says}]`, `says`, `other`: any id is taken),
`number` (`value`: a number or null for off, `unit`: `usd`, `says`), `info`
(`text`), `action` (`text`, `choices` as buttons) or `note` (`text`, standing where its group's
rows would be). A row may carry `help` (*POST /api/settings*). Read afresh on each request, so a
change made at the terminal shows (#1392).

### `POST /api/settings` (user only)
`{key, value}` (`value`: a string, a number, or null for a number row's off), JSON only (else 415
`json only`). It runs that row's `wb` command as the user, not as Claude:
- 200 `{"ok": true, "said": <wb's line>, "rows": [...], "help"?: <note>}`. `help` comes when the
  value just chosen needs Claude's help to work (Agent code navigation on with probe not set up).
  The server returns it and records it (below), and Claude in the terminal offers to set it up
  (installing nothing without the user's yes); showing it is the Settings view's job.
- 400 `{"error": "no such setting"}` for an unknown `key`; 400 `{"error": "not one of its
  choices"}` for a value the row doesn't take (nothing runs for either).
- 409 `{"ok": false, "error": <wb's line>, "rows": [...]}` when wb refused.

A change that went through is recorded (`settings-changes.jsonl` in the state directory: `{ts,
key, label, from, to, said, help?, by: "web"}`), so Claude in the terminal hears of it. Claude
changes a row through the same writer with `wb settings set`, on the user's yes in the
conversation; its rows say `by: "claude"`.

### `GET /api/stats` (user only)
The Stats view's history: `{"feed": <wb stats --json>, "routes": <wb route --json, or null>,
"models": <wb models --json, or null>}`. `routes` and `models` name the models; both come from
the Settings rows' cache (10 s), null when wb couldn't read them.

### `GET /api/top` (user only)
The live view, as server-sent events (`text/event-stream`; send the bearer header, so `fetch`,
not `EventSource`):
- `event: top`, `data: <one wb top --json object, verbatim>`: the newest at once on connect, then
  each tick (every 1, 2 or 5 s: stats.json's `every`, `wb stats set every`). Each event is followed by a blank line, as on
  `/api/stream`.
- `event: down`, `data: {"why": str, "retry": <seconds>}`: the server's `wb top` ended; it is
  tried again after `retry` (30 s, doubling to 10 minutes), and `top` events come again once it
  runs.
- `: ping` after 15 s with nothing to send.

One `wb top` runs per server, whoever watches; it stops 15 s after the last viewer leaves. At most
4 streams are open: a 5th is `503 {"error": "too many live views open"}`.

### The page itself (no token)
`GET /` (or `/index.html`) and the page's files (`/app.js`, `/app.css`, `/sw.js`, icons,
`/manifest.webmanifest`); anything else is 404.

Any other `/api/` method or path wants the token too (401 without it, 403 for an agent's) and
is then `404 {"error": "not found"}`. Every `POST` takes JSON as above: a bad `Content-Length` is
400 `bad length`, an unparsable body 400 `bad json`.

## Rows: messages

A **message** (`messages`, `replies`, `message`):
- `id` (`<ms>-<6 hex>`), `ts` (seconds), `text`.
- `from`: `"user"`, `"claude"`, `"notice"` (a dialog waits in the terminal: `text` the waiting line,
  `tool`, `kind`: `permission`, `plan`, `question` or `elicitation`, `what?`; no buttons, since
  Claude Code's dialogs are answered in the terminal), or an agent's name
  with `role: "agent"`.
- On a message to Claude: `cid?`, `files?: [{id, name, size, type, path}]`, `about?`, and in the
  state `received: {id, ts, by, how: "now"|"into"|"queued"} | null`, `expired?` (`into`: an
  agent's message read into a turn it may not have opened: answer it with `--to`; `queued`: held
  for the running turn, `now` once it ends, an agent's `into`).
- On Claude's reply: `re?: [message ids]` (what it answers: `wb chat reply --to`, else the
  messages that opened that turn or the user's delivered into it, the user's alone when the
  user's are among them; from another Claude Code session than the listening one, none: it is
  the user's),
  `to?: ["user" | agent names]` (whose those messages are, or `"user"` for a question to the
  user, `wb chat reply --to user`; absent means the user's), `shots?`, `audio?`, `auto?: true`
  (the turn's own answer, which the mod posted when a turn that had the message ended with no
  reply to it: at most 4,000 characters, the whole in the terminal), `claude_pid?`
  (the Claude Code process that wrote it, on every Claude row: a reply, a screenshot, a listen),
  and on a `wb chat reply` from Claude Code `session?`, `claude_start?`, `claude_machine?` beside
  it (the Claude Code session that wrote it, so the user's answer goes back to that session),
  `turn_by?` (on the listening session's reply: whose turn's end makes a wait on it `done`).

## The stream's events

- `event: state`, `data: <the whole state, as GET /api/state>`: first, on every connection (so a
  reconnect is a resync).
- `event: patch`, `data: {...}`: what changed since: `messages` as `{put: [rows new or
  changed], drop: [ids gone]}` (only when it changed), every other field of the state whole,
  and `now`. Merge rows by `id`.
- A blank line (an empty event: nothing to read) 50 ms after each event, on its own, so a browser
  that holds a body's last bytes until more come (WebKit, past 8 KB) hands the event over.
- `: ping` (a comment) after 15 s with nothing to send.

## Versions

`api` in `/api/state` and `/api/health` is this API's version. A change that breaks a client
bumps it; adding a field or an endpoint doesn't (the admin endpoints, `/api/settings`,
`/api/stats` and `/api/top`, came in 2.0 under version 1).
- **1** (2026-11-23): agent tokens, `/api/health`, `/api/wait`, `role`/`re`/`to` on rows, an
  agent's view of its own conversation. Before 2.0 shipped, the user's dialog cards and their
  endpoints left it (#1350): Claude Code's dialogs are answered in the terminal.
