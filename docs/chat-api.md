# The web chat's HTTP API

`wb chat serve` (or `wb chat on`, which starts it) is a JSON API behind a bearer token. The page
it serves (`workbench/chat_static/`) is one client of it; another agent with a token of its own is
another. No browser is needed: the server runs headless, and everything below works with curl.

What reads and answers the messages is **the driver**: the Claude Code or OpenCode session in
workbench mode that listens on the chat (Claude's Workbench, or OpenCode Workbench with any model
OpenCode runs). The API is the same for both, and says which one listens and which one wrote each
reply (`driver`, *The driver*).

This page is the reference for both. The same API, machine-readable, is `docs/chat-api.yaml`
beside it (OpenAPI 3.1: the `/api/v1/` paths, bearer auth, the one error schema, the
`Idempotency-Key`), for a tool that builds a client from one; a test keeps its paths exactly the
routes the server serves. The internals are in ARCHITECTURE.md, *The web chat*.

**Handing it to another agent:** make it a token (`wb chat token add my-agent`) and point it at
`skill/agents/ask-workbench/` in this repository. That directory is a portable skill (the
agentskills.io format, `SKILL.md` with `name` and `description`), and its
`scripts/ask_workbench.py` does the whole flow below with the standard library alone. An agent
that speaks MCP uses its `scripts/mcp_server.py` instead ([An MCP server](#an-mcp-server)). Before
workbench 2.2 the skill was `skill/agents/claude-workbench/` (`ask_claude.py`): that path still
runs the new scripts.

## Contents
- [The driver](#the-driver)
- [Base URL and auth](#base-url-and-auth)
- [Errors](#errors)
- [Retry safety](#retry-safety)
- [The agent flow, end to end](#the-agent-flow-end-to-end)
- [An agent's view](#an-agents-view)
- [An MCP server](#an-mcp-server)
- [Endpoints](#endpoints)
- [Rows: messages](#rows-messages)
- [The stream's events](#the-streams-events)
- [Versions](#versions)

## The driver

- **Which driver listens:** `GET /api/health` says `driver` while one does, and the `listener`
  (in `/api/state`, `/api/wait` and `/api/send`) carries the same `driver`: `{"tool": "claude"}`
  for Claude Code, `{"tool": "opencode", "model": "<provider>/<id>"}` for OpenCode (the model of
  that session, when its server says).
- **Which driver answered:** each reply carries `driver`, its writer, in the same shape. Show it
  as the answer's author; the client script prints `[answered by …]`, and the web page names the
  driver the same way: a reply by its `driver`, a receipt by its `by` (OpenCode's session ids
  start `ses_`), the header and "Waiting for a(n) … session" by the listener's (with none
  listening, the last reply's), a push's title by the
  reply's, else the listener's; Claude Code where nothing says.
- **One at a time:** with both drivers open on one machine, one listens (it holds the chat's
  claim, `listener`), and only its replies thread onto an agent's message; the other's plain
  reports go to the user.
- **The names kept:** a driver's reply is `from: "claude"` whichever driver wrote it, and
  `claude_pid` is Claude Code's alone. Branch on `driver`, never on `from`, to tell them apart. A
  chat before 2.2 has no `driver` anywhere.

## Base URL and auth

- **Base URL:** `http://localhost:8090` by default (another is `wb chat on <port>`'s; `wb chat
  url` prints the page's link, whose host and port are the base). The server listens on
  `127.0.0.1` only. To reach it from another device, point a tunnel or proxy of your own at
  `localhost:8090` and add the name it passes as the Host to `~/.config/workbench/chat.json`'s
  `hosts` (`*` for any; read again when the file changes, no restart); the token still applies.
- **Paths:** every `/api/<x>` below is also served at `/api/v1/<x>`, by the same handler with the
  same rules (an agent's denials too). New clients use `/api/v1/` (workbench 2.1 and later);
  `/api/` stays for the page and older clients, and `ask_workbench.py` keeps it, so it works with
  any server.
- **Host check:** a request whose `Host` header is a name the server doesn't answer to gets
  `403 {"error": "unknown host <host>: add it to chat.json's hosts", "code": "unknown_host"}`.
  `localhost`, `127.0.0.1` and `::1` always pass.
- **Auth:** every `/api/` path wants `Authorization: Bearer <token>`. A missing or wrong token
  is `401 {"error": "bad or missing token: open the link from `wb chat url`", "code":
  "bad_token"}`; a revoked agent token is `401` with `"code": "token_revoked"`.
- **Two kinds of token:**
  - **The user's** (`~/.config/workbench/chat-token`, or `$WB_CHAT_TOKEN`): everything here.
  - **An agent's** (`wb chat token add <name>`, printed once and kept only as its sha256 in
    `chat-agents.json` beside the user's token, mode 0600; `<name>` is 1 to 32 of `a-z`, `0-9`
    and `-`). It can check health, send, upload, wait for replies, and read its own conversation
    (*An agent's view*): never the user's messages or the user's settings.
    - It works on an allow-list: `GET /api/health`, `/api/wait`, `/api/state`, `/api/stream`,
      `/api/upload/<id>` and `/api/file/<id>`; `PUT /api/upload/<id>`; `POST /api/send` (each
      at `/api/v1/` too). Every other path and method is `403 {"error": "agent tokens can't …",
      "code": "agent_forbidden"}`: `use <path>` for most, the older wording where there was one
      (`change notifications` for `/api/push`, `mark the user away` for `/api/active`, `mark
      what the user has seen` for `/api/seen`, `read the user's notifications` for
      `/api/latest`, `read screenshots` for `/api/shot`, `read audio takes` for `/api/audio`,
      `read tasks` for `/api/task`, `transcribe the user's voice clips` for
      `/api/transcribe/<id>`). An endpoint added later is the user's alone unless this list
      names it.
    - It works only from this machine: a loopback peer with no proxy's forwarding header. A
      request with an agent token that comes through a tunnel or proxy (it carries
      `X-Forwarded-For`, `X-Forwarded-Host`, `X-Forwarded-Proto`, `Forwarded`, `X-Real-IP`, `Via`,
      `True-Client-IP`, `Cf-Connecting-Ip` or `Tailscale-User-Login`), or from any other peer, is
      ``403 {"error": "agent tokens work only from this machine, at the address `wb chat url`
      prints", "code": "not_local"}``. The user's token is unaffected.
    - Its sends are limited to `AGENT_SENDS_PER_MIN` (30) a minute, then `429 {"error", "code":
      "rate_limited", "retry_after": <seconds>}`.
    - `wb chat token list` names the agents; `wb chat token revoke <name>` ends one at once
      (the next request is 401 `token_revoked`; a held wait answers it too and an open stream
      closes).
- **Agents never carry the user's authority.** An agent's message is stored with `role:
  "agent"` and `from: "<name>"`. The driver gets it as `[agent <name>] …`, not as the user's
  `[web chat] …`, and treats it as a teammate's request. Anything that needs the user's yes
  (spending, pushing, opening a pull request, deleting) waits for the user; the driver says so in
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

## Errors

Every error answer is JSON of one shape: `{"error": "<a plain line, for a person>", "code":
"<a stable code, for a program>"}`, some with more fields (`retry_after`, `final`). Branch on
`code`; `error`'s wording may change. It holds on every path, and for the server's own refusals
too (an unsupported method, a malformed request line):

| Status | `code` | What it means |
|---|---|---|
| 400 | `bad_request` | the request is malformed: the line says which part |
| 400 | `bad_idempotency_key` | an `Idempotency-Key` that isn't 1 to 255 printable characters without spaces |
| 401 | `bad_token` | no token, or not one the chat knows |
| 401 | `token_revoked` | an agent token the user revoked: ask them for a new one |
| 403 | `not_local` | an agent token from another machine: agents work only from this one |
| 403 | `agent_forbidden` | the user's alone (*An agent's view*) |
| 403 | `unknown_host` | a `Host` the chat doesn't answer to |
| 403 | `forbidden` | refused otherwise |
| 404 | `not_found` | no such path, message, upload, file, screenshot, take, open task or setting's listener (for an agent, also one not in its view) |
| 409 | `conflict` | another sender's `cid` or upload id, something else where an upload goes, wb refusing a setting, or the GPU busy with a task (a transcription) |
| 410 | `expired` | a message never passed to the driver (unread past 6 hours: send it again), or a screenshot pruned with its task |
| 411 | `length_required` | an upload without its `Content-Length` |
| 413 | `too_large` | a body, file or message over its limit (or a request line or headers too long) |
| 415 | `json_only` | a `POST` that isn't `application/json` |
| 415 | `not_audio` | an upload to transcribe that isn't a voice clip |
| 422 | `idempotency_key_reused` | an `Idempotency-Key` sent with another request in the last 24 hours |
| 429 | `rate_limited` | an agent over its sends a minute: try again after `retry_after` seconds |
| 500 | `server_error` | the disk or the server failed (`"final": true`: trying again can't help) |
| 501 | `not_implemented` | a method the server doesn't serve (only `GET`, `PUT` and `POST`) |
| 502 | `bad_gateway` | the listener failed a transcription |
| 503 | `busy` | too many waits or streams open, or a listener not answering: try again in a moment |
| 507 | `disk_full` | no room to store an upload |

Nobody listening is not an error: `listener: null` on a 200 (*The agent flow*).

## Retry safety

A send cut short (a reset connection, a proxy's 502, the client's own timeout) may or may not have
landed. Send it again with the same key and the driver is never asked twice:
- **`Idempotency-Key: <1 to 255 printable characters, no spaces>`** on `POST /api/send`, say a
  fresh UUID per request. The same key from the same token within 24 hours answers with the
  message the first send made, sends nothing, and carries `Idempotent-Replayed: true` (the
  `listener` and `state` are as of now). The same key on another body is `422
  idempotency_key_reused`, never a new request dropped. Only a send that landed is remembered: a
  refused one sent nothing, so its retry is checked afresh. A replay costs none of an agent's
  sends a minute.
- **`cid`** in the body does the same within the thread (*`POST /api/send`*); `ask_workbench.py`
  sends both, the same value, and tries a cut send again for up to two minutes. Still cut, it
  exits 2 naming the key, and `--key <key>` sends the same request again under it; its files'
  upload ids come from the key and their bytes, so the body matches (keep a key's body the same:
  a new upload id is a new body, `422`).
- **An upload** needs no key: `PUT /api/upload/<id>` stores an id once, and the same id again
  answers with the first upload.

## The agent flow, end to end

```sh
export WORKBENCH_URL=http://localhost:8090 WORKBENCH_TOKEN=wba-…   # from wb chat token add my-agent
auth=(-H "Authorization: Bearer $WORKBENCH_TOKEN")

# 1. Is a driver (Claude Code or OpenCode in workbench mode) listening? If not: do it yourself, or try later.
curl -s "${auth[@]}" "$WORKBENCH_URL/api/health"
# {"api": 1, "listening": true, "driver": {"tool": "opencode", "model": "deepinfra/…"}}

# 2. (optional) Upload a file under an id of your own (8 to 64 of A-Z a-z 0-9 -).
id=$(python3 -c 'import uuid; print(uuid.uuid4())')
curl -s "${auth[@]}" -T notes.txt -H "X-File-Name: notes.txt" -H "Content-Type: text/plain" \
  "$WORKBENCH_URL/api/upload/$id"
# {"upload": {"id": "…", "name": "notes.txt", "size": 812, "type": "text/plain", "path": "…"}, "new": true}

# 3. Send one request: the repo, the goal, what "done" means.
curl -s "${auth[@]}" -H 'Content-Type: application/json' \
  -d '{"text": "In ~/src/app: add a --json flag to `app list`. Done: tests pass, on a branch.", "files": ["'$id'"]}' \
  "$WORKBENCH_URL/api/send"
# {"message": {"id": "1795436900218-c2fe2e", "from": "my-agent", "role": "agent", …}, "listener": {…}, "state": {…}}

# 4. Wait for the driver's replies to it, until its turn is done.
curl -s "${auth[@]}" "$WORKBENCH_URL/api/wait?after=1795436900218-c2fe2e&timeout=300"
# {"replies": [{"id": "1795436900301-a1b2c3", "from": "claude", "driver": {"tool": "opencode", …}, "text": "On it: …", "re": [...], "to": ["my-agent"]}],
#  "done": false, "listener": {…}, "waiting_on_user": false}

# 5. Keep reading from the last reply's id until "done" is true.
curl -s "${auth[@]}" "$WORKBENCH_URL/api/wait?after=1795436900301-a1b2c3&timeout=300"
# {"replies": [], "done": true, "listener": {…}, "waiting_on_user": false}

# 6. A later report (the driver hands work to its agents, ends its turn, and is woken when they
#    finish): wait for the next reply, however long the gap.
curl -s "${auth[@]}" "$WORKBENCH_URL/api/wait?after=1795436900301-a1b2c3&timeout=600&until=reply"
```

- **`listener: null`** (from `/api/send` or `/api/wait`) means no session is listening: the
  message waits in the queue, and is read once a session in workbench mode with the chat on
  listens. A message unread for 6 hours (`expire` in the state) is never passed on.
- **What `done` means:** the driver's turn that took the message (or that wrote the newest reply
  returned) has ended; another session's turn never counts. The driver may still report later, after work it handed off: its reply says
  so, and `until=reply` waits for that report.
- **`waiting_on_user: true`**: the driver is paused on the user (a permission, a plan, a question,
  or a consent it asked in words). The driver's dialogs are answered in its terminal, by the user
  alone; the web page shows a line while one waits. Keep waiting.
- **Follow-ups** are new messages: send again, and wait on the new id.

## An agent's view

An agent's token reads only its own conversation:
- **`/api/state` and `/api/stream`** carry `{api, messages, listener, uploads, expire, now}`, and
  `messages` holds only its own messages and the driver's rows whose `to` names it. No
  `push`, `savings`, `build`, `tasks`, `runs`, `dictate`, `driver` or `settings`: those are the user's. `/api/send`'s `state` is the same view.
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

## An MCP server

`skill/agents/ask-workbench/scripts/mcp_server.py` puts the agent flow behind three MCP tools,
for an agent or tool that speaks MCP. It is a stdio server (newline-delimited JSON-RPC), standard
library only, started by the agent's MCP client. It reads the same `WORKBENCH_URL` and
`WORKBENCH_TOKEN` as `ask_workbench.py`, from its environment (`CLAUDE_WB_URL` and
`CLAUDE_WB_TOKEN`, their names before 2.2, still work; so does the old path,
`skill/agents/claude-workbench/scripts/mcp_server.py`, and the tool's old name, `ask_claude`).

It is for the user's own agents on this machine, talking to the user's own driver.
It calls only the `/api/v1/` paths, so it needs a chat from workbench 2.1 or later (an older one
is said so, nothing sent). It listens on nothing but stdio. It refuses a `WORKBENCH_URL` whose host isn't this machine's
(`localhost`, `127.*` or `::1`): every tool says so, and nothing is sent. It follows no redirect.

- **`ask_workbench {text, key?}`**: checks `/api/v1/health` (not listening: refused, nothing
  sent), sends the request and returns its id at once, naming the driver that listens. `key` (optional, 1 to 64 of `A-Z a-z 0-9 . _ :
  -`; a new one per call by default) goes as the send's `cid` and its `Idempotency-Key` header, so
  a retry under the same key lands once: it answers `replayed: true` and says it was not sent
  again. A send cut off by a restart is sent again under it. The same key on another request is
  refused (`idempotency_key_reused`), nothing sent.
- **`get_reply {id, wait?}`**: the driver's replies to that request, which driver answered
  (`driver`, each reply's too), and its status:
  - `done`: the turn that took it has ended. A later report can still come (its reply says so);
    `get_reply` shows it then.
  - `still working`.
  - `waiting on the user`: paused on the user's yes, not stuck.
  - `queued`: no session listens yet.
  - `expired`: unread past 6 hours, never passed on (the chat's `410 expired`).

  It holds up to `wait` seconds (default 20, at most 300) for the turn to end.
- **`list_requests {}`**: the agent's 20 newest requests, how far each got, and which driver
  listens, if any.

The config, one line. For OpenCode (`opencode.json`; `codemode: false` offers the tools as
themselves, not behind `execute`):

```json
"mcp": {"servers": {"ask-workbench": {"type": "local", "command": ["python3", "/path/to/workbench/skill/agents/ask-workbench/scripts/mcp_server.py"], "environment": {"WORKBENCH_URL": "http://localhost:8090", "WORKBENCH_TOKEN": "wba-…"}, "codemode": false}}}
```

For clients that read an `mcpServers` map (Cursor's `mcp.json`, Gemini CLI's `settings.json`):

```json
"mcpServers": {"ask-workbench": {"command": "python3", "args": ["/path/to/workbench/skill/agents/ask-workbench/scripts/mcp_server.py"], "env": {"WORKBENCH_URL": "http://localhost:8090", "WORKBENCH_TOKEN": "wba-…"}}}
```

The token is the agent's own (`wb chat token add <name>`), with an agent's view and limits.

## Endpoints

Every `/api/` path below needs the bearer token. *User only* means an agent token gets 403.

### `GET /api/health`
`{"api": 1, "listening": bool, "driver"?: {"tool": "claude"|"opencode", "model"?}}`. `listening`
is true while a driver (a Claude Code or OpenCode session in workbench mode, with the web chat
on) is listening, and `driver` says which, only then (*The driver*). An agent's first call.

### `GET /api/state`
The whole state the page draws (an agent's token gets its own view: *An agent's view*):
- `api`: this API's version (1).
- `messages`: the thread's last 200 rows (*Rows*), each of the user's and agents' with
  `received` (the driver's receipt, or null) and `expired: true` once unread past `expire`. A notice
  (a dialog waiting in the terminal) is left out once it settles.
- `listener`: the listening session `{session, cwd, pid, start, machine?, parent?, busy, driver}`,
  or null (`driver`: *The driver*). Claude Code's claim: `pid` its `wb events` process, `parent`
  its Claude Code. OpenCode's: `{session: "<OpenCode session id>", tool: "opencode", cwd, pid,
  start, machine, busy, driver}`, `pid` and `start` the OpenCode server's whose plugin delivers
  the messages into that session, no `parent`; it listens while that server runs and no
  heartbeat of it taken since the claim leaves the session out (one deleted there).
- `push`: `{key: the VAPID public key or null, test: the last test push's time or null}`.
- `savings`: the week's `wb savings` totals and `score`, and `api`: the week's API tasks apart
  (`tasks`, `cost` in dollars, `unknown`, `plan`, `trials`, `claude`, `self_est`, `by_provider`,
  `by_day`, `prices`: `catalog` (an estimate, shown `≈`), `yours` or `provider`, the total's and each
  provider's, and `names`: each provider's name), never part of the score.
- `uploads`: `{file, message, files}`: bytes per file, bytes per message, files per message.
- `build`: the page files' stamp (a page whose stamp differs reloads).
- `tasks`: the open tasks, for the strip over the thread: `[{name, phase, model, label, via: "gpu"|"api",
  provider_name}]` (none accepted, discarded or parked by `wb stop --all`); `label` is the model's
  name (a GPU model's label, an API model's name in OpenCode's catalog, `""` for none).
- `runs`: the deep workflows not closed (`wb run`), for the runs strip: `[{run, flow, phase, why,
  stages: {<stage>: {done, running, null, filtered, not_run}}, spent_usd, budget_usd,
  budget_min, gpu_min}]`, by their start. `phase` is `running`, `checkpoint`, `paused`, `stopped`,
  `review` or `failed`; `budget_usd` null for none. Each run's
  Stop and Summary are its Settings row (`run-<run>`, *GET /api/settings*).
- `dictate`: true while the computer has a listener (a speech recognizer in `wb audio`'s
  audio.json) for `POST /api/transcribe/<id>`: the page shows its mic only then.
- `driver`: the driver's state, the line the page keeps at the top of the chat (#1355): `{state:
  "off"|"none"|"idle"|"working"|"waiting", driver: "claude-code"|"opencode"|"any", who, where?, waiting?:
  {kind: "permission"|"question"|"plan"|"elicitation"|"consent", tool, since, what?}, line, next,
  grant?: {line, command}}`. Waiting (a prompt up in the terminal: the listener's own `waiting`
  fact, never another session's notice) beats working beats idle; no session listening is `none`;
  none while the chat is switched off (`wb chat off`) is `off`, and `next` says `wb chat on`.
  `line` and `next` are the sentences to show, in the story's words (workbench/story.py); `grant`,
  only for a permission, is the driver's way to run with permissions granted (`command` to copy).
  Nothing answers a prompt here: only the terminal does.
- `settings`: the newest Settings change, `{ts, key, label, to, by}` (`by`: `web`, `claude`,
  `opencode`, `terminal`, `agent`), or null: a page with Settings open reads them again when it moves.
- `expire`: seconds a message waits before it is never passed on (21600).
- `now`: the server's time (seconds).

### `GET /api/stream`
Server-sent events (`text/event-stream`): see *The stream's events*; an agent's carry its own view.
At most 16 streams are open;
a 17th makes the oldest give way, and waits 5 s for a place before `503 {"error": "too many open
streams"}`. An agent's 17th never pushes another out: it is 503 at once. A client sends the bearer header, so it uses `fetch`, not `EventSource`.

### `GET /api/wait?after=<id>&timeout=<s>&until=done|reply`
A long poll for the driver's replies after the message `after` (a message's or a reply's id).
- `timeout`: 0 to 600 seconds (default 300). Behind a proxy with a shorter idle limit, ask less.
- `until`: `done` (default) returns at the first reply or once `done`; `reply` returns only at a
  new reply (for a later report after a turn already done).
- It also returns at once when nobody is listening (`listener: null`), at the timeout, and when
  the driver comes to wait on the user during it (`waiting_on_user` turns true; one already true at
  the start doesn't return it again).
- Response: `{"replies": [...], "done": bool, "listener": {...}|null, "waiting_on_user": bool}`.
  - `replies`: the driver's rows after `after` that are for the caller: their `to` names the agent,
    or, for the user's token, `to` names `"user"` or is absent.
  - `done`: a turn has ended since the newest reply returned (one of the session that wrote it:
    its `turn_by`), or, with none, since `after` (for a message to the driver: the turn that took it).
  - `waiting_on_user`: the driver is paused on the user: one of its dialogs waits in the
    terminal, and the phone was told of it. The work is waiting on the human, not stuck: keep
    waiting.
- Errors: 400 for a bad `after`, `timeout` or `until`; `404 {"error": "no such message", "code":
  "not_found"}` for an id not in the thread's recent part (the last 4 MiB), or, for an agent, not
  in its view; `410 expired` at once for a message unread past 6 hours with no reply (it was never
  passed to the driver: send it again); `503` when 16 waits are open already.
- A wait can be cut: the server restarts itself on an update (a reset connection, or 502 to 504
  from a proxy in front meanwhile), and `503` passes. Wait again, after a short pause, with the
  same `after`: nothing was lost (`ask_workbench.py` does for up to two minutes in a row, then exits
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
  (a task, from the page's task sheet: any task wb has, but a bench replay; the driver gets `(about
  task <task>)` and decides whether to steer it). An agent's send with `about` is `400 {"error":
  "agent messages can't carry about"}`.

Header: `Idempotency-Key`, optional (*Retry safety*).

Response: `{"message": {...}, "listener": {...}|null, "state": {...}}`. Errors: 400 `a message is
1 to 8000 characters`, `bad cid`, `bad json`, `no such screenshots`, `no such take`, `no such task`, `no upload
<id>: it must be uploaded before the message` (for an agent, also another's upload), `files is a
list of up to 10 upload ids`, `agent messages can't carry about`, and `bad_idempotency_key`; 409
for another sender's `cid`; 413 `too much for one message: …`; 422 `idempotency_key_reused`; 429
for an agent over its limit.

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
What a push notification shows: `{now, messages: [the driver's last message for the user, or an
open notice], listener, push: {test}}` (`listener` as in `/api/state`: its `driver` names a push
whose row says none). A reply only to agents is never pushed.

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
  `created_at` and `updated_at` (seconds; age and idle are `now` minus them), `model`, `label`
  (the model's name: a GPU model's label, an API model's name in OpenCode's catalog, `""` for
  none), `via`, `provider_name`, `report` (the round's summary.md, or what stands instead, cut at 64 KB with
  `report_cut: true`) and `verify` (the regression check's lines, `[]` for none). Read from the
  report: `agent_summary` (the agent's final words, null with no report), `changed` (the paths of
  the files it changed, `[]` for none, null with no report), `tests` (`tests: PASS (…)` or
  `tests: FAIL (…)`, `""` for none) and, for a failed or stuck task, `happened`: one plain line of
  what happened (`The agent stopped before writing anything (OpenCode exited).`; with no report,
  `The task stopped partway (…): what the agent changed so far is in the worktree.` when the
  worktree has changes, `The task stopped before the agent ran (…).` for a stop before the agent,
  else `The task stopped (…).`; `""` otherwise).
- `diff`: against `base` (the task's base commit, or `HEAD` after `wb next`: the current step,
  as `wb diff`, the gitignored files the agent wrote included): `stat` (`wb diff --stat`) and `files` (`[{path, added, deleted}]`, null counts
  for a binary file). With `full=1`, `diff` instead of the stat: the whole diff, or with
  `path=<one of files>` that file's (else 404 `no such file in the diff`), each line cut at
  2,000 characters (`lines_cut`: how many), or `diff: null` with `cut: true` past 256 KB. A task
  with no worktree yet, or a git failure, answers `note` and no files.
- `log`: `rows`, the last `n` (1 to 200, default 50) of `wb log -v`'s rows, each `{ts, round,
  kind, text}` (`wb log`'s words after the time, cut at 2,000 characters; `ok` for a tool call),
  and `more: true` when older ones were left out.

A path inside the task's worktree, in the summary's `report`, `last` and `verify` and in each log
row, is written relative to it (`read src/f0.py`; the worktree itself is `.`).

400 for another `part` or a bad `n`. All of it is text the agent wrote: show it as text.

### `POST /api/transcribe/<id>` (user only)
`{}`: the words of an upload that is a voice clip (`audio/*`, `video/*` or
`application/octet-stream`), heard by the computer's listener → `{"text": "..."}`, for the user to
edit and send as a message (the clip itself is never sent to the driver). The words are kept with the
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
The web app's Settings: `{"rows": [...], "changes": [...], "ts": <seconds>}`. Each row is `{key, group, label, kind,
…}`: `group` one of `significant`, `trivial`, `writing` (a card per role, #1553), a role of the user's own by its
name (its card after those, titled by its role row's `card`, #1625), `shared`, `gpu`, `api`, `setup`,
`workflows`, `terminal`, `experimental` (in that order); `more: true`, a row shown under its card's More (a role's model
for the side it isn't on, and thinking); `hidden: true`, a row the page doesn't show while it can do nothing; `fixed: true`, a
choice shown as its `says` alone; `ask`, a row with a problem or a request the page offers to hand to the driver
(true, or a string: the request); `brief`, what a folded card's summary says of it; `kind` one
of `choice` (`value`, `choices: [{value, label, says, needs?, group?}]` (`needs`: the row whose value that choice needs
first, a role's API its `api-<role>`; `group`: an API model's provider, or `In use` for the role's own and the
others' in use, the page's `<optgroup>`s: an API row lists every catalog model, #1617), `says`, `other`: any id is taken),
`number` (`value`: a number or null for off, `unit`: `usd`, `says`), `info`
(`text`), `action` (`text`, `choices` as buttons), `form` (`add-role`, #1625: `text`, `fields: [{name, label,
kind: text|select, pattern?, max?, hint?, optional?, choices?}]`, `choices` its one button; its value is the fields
as one JSON string, `{name, label, like, about?}`) or `note` (`text`, standing where its group's
rows would be). A row may carry `help` (*POST /api/settings*), and `changed: {ts, by}`, its newest
recorded change within 7 days (#1355). `changes`: the newest 10 recorded changes, newest first,
`{ts, key, label, from, to, by}`. Read afresh on each request, so a change made at the terminal
shows (#1392). The `workflows` group holds deepmode (`workflows`), which side the runs' items take
(`deep-side`: `roles`, `gpu` or `api`, #1627), a `run-<run>` action per run not
closed (its `text` the run's flow, phase, stage counts and spend; its choices `stop`, while it runs, and
`summary`) and the flows there are (`flows`, info).

### `POST /api/settings` (user only)
`{key, value}` (`value`: a string, a number, or null for a number row's off), JSON only (else 415
`json only`). It runs that row's `wb` command as the user, not as the driver:
- 200 `{"ok": true, "said": <wb's line>, "rows": [...], "changes": [...], "help"?: <note>, "detail"?: <text>, "page"?: <a note in
  the page's words, such as "Set Agents to Both for this to apply.">}`. `help` comes when the
  value just chosen needs the driver's help to work (Agent code navigation on with probe not set
  up). The server returns it and records it (below), and the driver in the terminal offers to set it up
  (installing nothing without the user's yes); showing it is the Settings view's job. `detail` comes
  with a run's `summary`: `wb run report <run>` (cut past 16,000 characters), its agents' results among it
  (show it as text, never markup); `said` is its first line.
- 400 `{"error": "no such setting"}` for an unknown `key`; 400 `{"error": <why>}` for a value the row doesn't
  take (`'x' isn't one of coding's choices`, the cap's `a positive number, or off`; `not one of its choices` for
  a value neither string, number nor null; nothing runs for any).
- 409 `{"ok": false, "error": <wb's line>, "page"?: <the refusal in the page's words>, "rows": [...]}` when wb
  refused (`page`: by the row's label, with no `wb:` and no command, where the server has the words).

A change that went through is recorded (`settings-changes.jsonl` in the state directory: `{ts,
key, label, from, to, said, help?, by: "web"}`), so the driver in the terminal hears of it; a
choice or a number set to the value it had is no change, and isn't; nor is a run's `stop` or `summary`
(a `run-` row changes no setting: a stop from here is said to the run's session as its own notice). The driver changes a row
through the same writer with `wb settings set`, on the user's yes in the conversation; its rows
say who ran it (`by`: `claude` under Claude Code, `opencode` from an OpenCode session, `agent`,
or `terminal`), and so does a row's own `wb` command typed anywhere (`wb coding ask` at a
terminal is `by: "terminal"`). Each recorded change moves the state's `settings`, so an open page draws it.

### `GET /api/stats` (user only)
The Stats view's history: `{"feed": <wb stats --json>, "routes": <wb route --json, or null>,
"models": <wb models --json, or null>}`. `routes` and `models` name the models (and `routes`'
`spend_today` and `day_usd` give Now's `API today` line); both come from
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

Any other `/api/` path wants the token too (401 without it, 403 `agent_forbidden` for an
agent's) and is then `404 {"error": "not found", "code": "not_found"}`; any other method is `501
not_implemented`. Every `POST` takes JSON as above: a bad `Content-Length` is 400 `bad length`,
an unparsable body 400 `bad json`.

## Rows: messages

A **message** (`messages`, `replies`, `message`):
- `id` (`<ms>-<6 hex>`), `ts` (seconds), `text`.
- `from`: `"user"`, `"claude"` (the driver's, whichever driver wrote it: *The driver*), `"notice"`
  (a dialog waits in the terminal: `text` the waiting line,
  `tool`, `kind`: `permission`, `plan`, `question` or `elicitation`, `what?`; no buttons, since
  the driver's dialogs are answered in the terminal), or an agent's name
  with `role: "agent"`.
- On a message to the driver: `cid?`, `files?: [{id, name, size, type, path}]`, `about?`, and in the
  state `received: {id, ts, by, how: "now"|"into"|"queued"} | null`, `expired?` (`into`: an
  agent's message read into a turn it may not have opened: answer it with `--to`; `queued`: held
  for the running turn, `now` once it ends, an agent's `into`).
- On the driver's reply: `re?: [message ids]` (what it answers: `wb chat reply --to`, else the
  messages that opened that turn or the user's delivered into it, the user's alone when the
  user's are among them; from another session than the listening one, of either driver, none:
  it is the user's), `driver?: {tool, model?}` (which driver wrote it: *The driver*),
  `to?: ["user" | agent names]` (whose those messages are, or `"user"` for a question to the
  user, `wb chat reply --to user`; absent means the user's), `shots?`, `audio?`, `auto?: true`
  (the turn's own answer, which the mod posted when a turn that had the message ended with no
  reply to it: at most 4,000 characters, the whole in the terminal), `claude_pid?`
  (the Claude Code process that wrote it, on every row Claude Code writes: a reply, a screenshot, a listen),
  and on a `wb chat reply` from Claude Code `session?`, `claude_start?`, `claude_machine?` beside
  it (the Claude Code session that wrote it, so the user's answer goes back to that session),
  `turn_by?` (on the listening session's reply: whose turn's end makes a wait on it `done`); on a
  row an OpenCode session wrote, `driver: {tool: "opencode", model?}` and `opencode_session?`
  (that session's id, for the plugin's check that a turn still owes the user an answer), never
  `session` or `claude_*`. A notice from OpenCode says `OpenCode is waiting for you in the
  terminal.` and carries `driver: {tool: "opencode"}`.

## The stream's events

- `event: state`, `data: <the whole state, as GET /api/state>`: first, on every connection (so a
  reconnect is a resync).
- `event: patch`, `data: {...}`: what changed since: `messages` as `{put: [rows new or
  changed], drop: [ids gone]}` (only when it changed), every other field of the state whole,
  and `now`. Merge rows by `id`.
- A blank line (an empty event: nothing to read) 50 ms after each event, on its own, so a browser
  that holds a body's last bytes until more come (WebKit, past 8 KB) hands the event over.
- `: ping` (a comment) after 15 s with nothing to send.

A patch comes when the thread, the listener or the driver's state changes, and when an open task's
status does (its phase, or the step it is on: each `tasks` row's `updated_at`), within a moment.

## Versions

`api` in `/api/state` and `/api/health` is this API's version. A change that breaks a client
bumps it; adding a field or an endpoint doesn't (the admin endpoints, `/api/settings`,
`/api/stats` and `/api/top`, came in 2.0 under version 1).
- **1** (2026-11-23): agent tokens, `/api/health`, `/api/wait`, `role`/`re`/`to` on rows, an
  agent's view of its own conversation. Before 2.0 shipped, the user's dialog cards and their
  endpoints left it (#1350): the driver's dialogs are answered in the terminal. Added since, in
  the same version (workbench 2.1): the `/api/v1/` paths, `code` on every error (`error` kept as
  the text), a revoked token's own code, `410 expired` from `/api/wait`, and `Idempotency-Key`;
  (workbench 2.2) `driver` on `/api/health`, the listener and each reply (*The driver*);
  `/api/phone-qr.svg` came in 2.2 and went with #1616 (the web app listens on localhost only).
