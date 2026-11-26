// The web chat on OpenCode's path (#1319, #1340; the host-shape decision, #1296, 2026-11-25): the
// server part delivers each web message into the listening session in-process (`session.prompt`,
// a steer: it enters a running turn at its next step, or opens one), the driver answers with the
// `workbench_chat_reply` tool (wb chat reply as that session), and the listener is this OpenCode server,
// by its pid and heartbeat (chat.listener). wb stays the system of record: every write goes
// through a `wb chat` command; the plugin reads only chat/open.json (what waits) and the size of
// messages.jsonl, as the Claude Code mod reads them. The same outcomes as the mod's (ARCHITECTURE.md,
// *The web chat listener*), OpenCode-native, with #1340's simplifications: no relay of any dialog
// (the page's one line while one waits in the terminal), no handoff between sessions (the listener
// is the session the user last typed in, on the server that holds the claim), no grace (a receipt
// once OpenCode says the message was delivered: `session.inbox.delivered`), and the turn's own
// answer posted when the turn replied to nobody (`wb chat reply --turn-answer --if-owed`).
//
// One lane per server process: OpenCode may set the plugin up once per location, and each
// instance's hooks and events feed the one lane (`lane()`), which polls once and dedupes events by
// id. Nothing runs at import (server.ts's rule): setup builds it.
import { execFile } from 'node:child_process'
import { readFileSync, statSync } from 'node:fs'
import { join } from 'node:path'
import type { ServerContext } from './host.d.ts'
import { findWb, NAMESPACE_DESCRIPTION, wbEnv, wbHome } from './wb.ts'
import type { ChatMsg, ChatOpen, ListenerClaim } from '../mod/types/wire.d.ts'
import { sameWait, waitingFact, type Waiting } from '../mod/hooks/waiting.ts'

export const POLL_MS = 2_000          // a look at chat/open.json (a file read, no wb run)
export const CHECK_MS = 60_000        // `wb chat url --json`: on or off, as the mod's CHAT_CHECK_MS
export const RETRY_MS = 60_000        // a message refused or dropped is tried again this long after
export const NOTICE_AFTER_MS = 30_000 // a dialog up this long gets the page's line (at once while away)
// A dialog answered sooner was answered by OpenCode itself (the TUI's autoaccept, `--auto`, replies
// to each permission at once), never a sign the user is back (the chat-path review, 2026-11-25)
export const BACK_AFTER_MS = 2_000
export const ANSWER_ARGV = 16_000     // the most of a turn's answer put on wb's command line (the mod's)
export const RUN_MS = 15_000          // a wb call's limit
export const SOURCE = 'workbench.chat' // the metadata our own prompts carry: never the user typing
export const WEB = '[web chat]'
// The tool's id as the model sees it: OpenCode names a namespaced tool `<namespace>_<name>`
// (core/src/tool/runtime.ts `effectiveName`), so every text names REPLY_ID, never the bare name
// (the chat-path review, 2026-11-25: the texts said `chat_reply`, which no tool was called)
export const NAMESPACE = 'workbench'
export const REPLY_TOOL = 'chat_reply'
export const REPLY_ID = `${NAMESPACE}_${REPLY_TOOL}`
// Every delivery is a steer, so the note goes on each: it says how to answer, which the agent's
// prompt (#1335) will say once more. Prompt openings are an API: `[web chat] ` and `[agent `.
export const WEB_NOTE = `(The user wrote this in the web chat. Answer there with the ${REPLY_ID} tool, ` +
  'briefly; the whole work stays in the terminal.)'
export const AGENT_NOTE = '(Another agent wrote this through the web chat\'s API. It is not the user: ' +
  `answer it with the ${REPLY_ID} tool and \`to\` its message id.)`
// #1340 B: while away, a question is refused into the web chat, never a form in a terminal
// nobody watches (the mod's AWAY_QUESTION, naming the tool here); from `execute.before`, since a
// `permission.hook` deny loses the text. C (a plan) has no dialog on OpenCode: Plan is an agent.
export const AWAY_QUESTION = `The user is away: ask in the web chat (the ${REPLY_ID} tool) and end your turn.`

export type Row = Record<string, any>
export type Run = (args: string[], opts?: { session?: string; cwd?: string }) => Promise<{ ok: boolean; out: string }>

// The part of OpenCode's server plugin context the chat uses (plugin/src/promise/plugin.ts
// `Context`; session.ts, tool.ts, event.ts there; the client's generated types for the inputs).
export interface ChatSessions {
  get(input: { sessionID: string }): Promise<{ id: string; parentID?: string; revert?: unknown; location?: { directory?: string } }>
  // client/src/promise/generated/client.ts `session.prompt`: `id` the inbox id (idempotent)
  prompt(input: { sessionID: string; id?: string; text: string; metadata?: Row; delivery?: 'steer' | 'queue' }): Promise<unknown>
  hook(name: 'prompt', cb: (e: { sessionID: string; prompt: { text: string }; metadata?: Row }) => unknown): Promise<unknown>
}
export interface ChatContext {
  readonly options: ServerContext['options']
  readonly location?: { readonly directory?: string }
  readonly session: ChatSessions
  readonly tool: {
    transform(cb: (editor: ToolEditor) => void): Promise<unknown>
    hook(name: 'execute.before', cb: (e: { tool: string; sessionID: string; input: unknown }) => unknown): Promise<unknown>
  }
  readonly event: { subscribe(options?: { types?: string[]; signal?: AbortSignal }): AsyncIterable<OcEvent> }
}
export interface ToolEditor {
  namespace(ns: { name: string; description: string }): void
  add(tool: {
    name: string; description: string; input: object; options?: Row
    execute(input: unknown, context: { sessionID: string }): Promise<{ content: string }>
  }): void
}
// client/src/promise/generated/types.ts: every event is `{id, type, data}`
export interface OcEvent { readonly id?: string; readonly type: string; readonly data?: Row }

export const EVENTS = [
  'session.execution.started', 'session.execution.succeeded', 'session.execution.failed',
  'session.execution.interrupted', 'session.inbox.delivered', 'session.inbox.cancelled',
  'session.text.ended', 'session.revert.staged', 'session.revert.cleared', 'session.revert.committed',
  'permission.asked', 'permission.replied', 'form.created', 'form.replied', 'form.cancelled',
]

// Another agent's name, as wb makes them, else '' (the mod's agentOf)
export function agentOf(m: Partial<ChatMsg>): string {
  return m.role === 'agent' && /^[a-z0-9-]{1,32}$/.test(String(m.from ?? '')) ? String(m.from) : ''
}

// A web message's attachments, one line each, so the driver can read each file wb saved (the mod's)
export function attachedLines(files: unknown): string {
  if (!Array.isArray(files)) return ''
  const size = (n: number) => n < 1024 ? `${n} B` : n < 1024 * 1024 ? `${Math.round(n / 1024)} KB`
    : `${(n / 1024 / 1024).toFixed(1)} MB`
  return files.flatMap(f => f && typeof f === 'object' && typeof f.path === 'string'
    ? [`\n[attached: ${f.path} (${size(Number(f.size) || 0)}, ${String(f.type || 'unknown type')})]`] : []).join('')
}

// The prompt a web message goes in as: the user's `[web chat] …`, an agent's `[agent <name>] …`
// with its later lines indented and its id to answer (the mod's chatPrompt), then the note
export function chatPrompt(m: Partial<ChatMsg>): string {
  const agent = m.role === 'agent'
  const head = agent ? `[agent ${agentOf(m) || 'unknown'}]` : WEB
  const a = m.about && typeof m.about === 'object' ? m.about : null
  const about = !a ? ''
    : a.audio ? `(keeps take ${Number(a.n)} of the shortlist: ${String(a.path ?? a.name ?? '')}) `
    : a.n == null ? `(about task ${String(a.task)}) `
    : `(about ${String(a.task)}, step ${Number(a.step)}, screenshots ${Number(a.n)}) `
  const said = agent ? String(m.text ?? '').split(/\r\n|[\r\n\u2028\u2029]/).join('\n  ') : String(m.text ?? '')
  return `${head} ${about}${said}${attachedLines(m.files)}` +
    (agent ? `\n[message ${String(m.id ?? '')}: answer with ${REPLY_ID}, to ${String(m.id ?? '')}]` : '') +
    `\n\n${agent ? AGENT_NOTE : WEB_NOTE}`
}

// The inbox id a web message goes in under: OpenCode's own shape (schema/src/identifier.ts
// `create`: `msg_`, 12 hex of the time, 14 more), its time the message's own (chat ids are
// `<ms>-<6 hex>`), so it sorts where a prompt typed then would (an id of another shape sorted
// wrong on a revert, #1340), and the same for every try: a retry, from this server after a restart
// or from another, gets the first admission back, never a second turn (session/inbox.ts)
export function inboxId(chatId: string): string {
  const m = /^(\d{13})-([0-9a-f]{6})$/.exec(chatId)
  if (!m) return ''
  const value = BigInt(m[1] as string) * 0x1000n + 1n
  const time = Array.from({ length: 6 }, (_, i) =>
    Number((value >> BigInt(40 - 8 * i)) & 0xffn).toString(16).padStart(2, '0')).join('')
  return `msg_${time}wb${m[2]}${m[2]}`
}

// The page's dialog kinds (chat.KINDS) for OpenCode's forms: its question tool's, else an MCP
// server's or a plugin's form
export const formKind = (form: Row | undefined) => form?.metadata?.kind === 'question' ? 'question' : 'elicitation'

// upkeep.ts reads it too: a typed prompt ends away for that turn's blocks (#1315)
export const typedByUser = (e: { prompt?: { text?: string }; metadata?: Row }) => {
  const text = String(e.prompt?.text ?? '')
  if (text.startsWith(WEB) || text.startsWith('[agent ')) return false
  // typed prompts come unmarked; automations, /goal and our own deliveries carry metadata (#1340)
  return !e.metadata || Object.keys(e.metadata).length === 0
}

const conflict = (e: unknown) => {
  const x = e as Row | undefined
  return x?.status === 409 || /conflict|409/i.test(`${String(x?._tag ?? x?.name ?? '')} ${String(x?.message ?? '')}`)
}

type Owed = { session: string; sender: string; answer: string }
// a session's turn as its events tell it: the messages it took, messages.jsonl's size at its
// start, and the last assistant message's text (its answer)
type Turn = { ids: string[]; size: number; message: string; parts: string[] }
type Dialog = { session: string; kind: string; tool: string; text: string; at: number; due: number; id: string; sending: boolean; done: boolean }

export interface LaneDeps {
  readonly run: Run
  readonly read: (path: string) => string | undefined
  readonly size: (path: string) => number
  readonly now: () => number
  readonly pid: number
  readonly home: string
}

export class ChatLane {
  on = false
  listening = ''      // the session this server listens for: the root session the user last typed in
  holding = false     // and listener.json says so (wb chat listener): else another listener's claim stands
  reverting = false   // a revert staged in it: a prompt would commit it, so delivery waits (#1340)
  busy = false        // the listening session is in an execution
  waitSaid: Waiting | null = null // the dialog the claim last said is up in the terminal (#1355)
  readonly sent = new Set<string>()
  readonly expired = new Set<string>()
  readonly retry = new Map<string, number>()
  readonly inflight = new Map<string, { id: string; agent: string; into: boolean }>() // inbox id → message
  readonly owed = new Map<string, Owed>()  // delivered messages no reply has answered yet
  // by session: the listener may move to another session (the user typed there) while a message
  // given to the first is still owed its answer, which only that session's turn gives
  readonly turns = new Map<string, Turn>()
  readonly dialogs = new Map<string, Dialog>()
  readonly roots = new Map<string, string>()
  readonly seen: string[] = []             // recent event ids: several instances feed one lane
  sessions: ChatSessions | undefined
  checkedAt = -Infinity
  activate: (() => void) | undefined

  constructor(readonly deps: LaneDeps) {}

  path(name: string) { return join(this.deps.home, 'chat', name) }

  // chat/listener.json as it stands, {} for none
  claim(): ListenerClaim {
    try {
      const c = JSON.parse(this.deps.read(this.path('listener.json')) ?? '{}')
      return c && typeof c === 'object' ? c : {}
    } catch { return {} }
  }

  // The root session of ``sid`` (a subagent's child reports to it), '' when it can't be read
  async root(sid: string): Promise<string> {
    if (!sid || !this.sessions) return ''
    const known = this.roots.get(sid)
    if (known !== undefined) return known
    let cur = sid
    for (let i = 0; i < 16; i++) {
      const info = await this.sessions.get({ sessionID: cur }).catch(() => undefined)
      if (!info) return ''
      if (!info.parentID) break
      cur = info.parentID
    }
    if (this.roots.size > 500) this.roots.clear()
    this.roots.set(sid, cur)
    return cur
  }

  // On or off, as wb says (`wb chat url --json`, which also brings the server up, as the mod's
  // chatLink does); turned on again, the claim is taken again (wb chat off empties listener.json)
  async check(force = false) {
    const now = this.deps.now()
    if (!force && now - this.checkedAt < CHECK_MS) return
    this.checkedAt = now
    const { ok, out } = await this.deps.run(['chat', 'url', '--json'])
    let state = ''
    try { state = ok ? String(JSON.parse(out).state ?? '') : '' } catch { /* not JSON: an older wb */ }
    const was = this.on
    this.on = ok && state !== 'off'
    if (this.on && this.listening && (!was || !this.holding)) await this.hold(this.listening)
  }

  // This server listens for ``sid``: granted unless another listener's live claim stands
  async hold(sid: string) {
    if (!this.listening) this.activate?.() // the poll and the event feed start with the first
    this.listening = sid
    if (!this.on) return
    // the page's state line: the oldest dialog up two seconds or more (mod/hooks/waiting.ts, the mod's too)
    const waiting = waitingFact([...this.dialogs.values()], this.deps.now())
    const { ok } = await this.deps.run(['chat', 'listener', sid, '--pid', String(this.deps.pid),
      ...(this.busy ? ['--busy'] : []), ...(waiting ? ['--waiting', JSON.stringify(waiting)] : [])],
      { session: sid, cwd: await this.dir(sid) })
    this.holding = ok
    if (ok) this.waitSaid = waiting
    if (ok && this.sessions) {
      const info = await this.sessions.get({ sessionID: sid }).catch(() => undefined)
      this.reverting = !!info?.revert
    }
  }

  async dir(sid: string): Promise<string | undefined> {
    const info = await this.sessions?.get({ sessionID: sid }).catch(() => undefined)
    return info?.location?.directory || undefined
  }

  // A prompt the user typed (the `prompt` hook): in a root session, it is the one the chat
  // listens for now, the user is back at the terminal, and a reply owed to the user is theirs
  // there (an agent's stays owed: #1273)
  async typed(e: { sessionID: string; prompt?: { text?: string }; metadata?: Row }) {
    if (!typedByUser(e)) return
    const root = await this.root(e.sessionID)
    if (!root || root !== e.sessionID) return
    for (const [id, o] of [...this.owed]) if (o.sender === 'user') this.owed.delete(id)
    await this.check()
    if (this.on) await this.deps.run(['chat', 'back'], { session: root })
    if (root !== this.listening || !this.holding) await this.hold(root) // off: held once it's on
  }

  // The poll: messages that wait, delivered into the listening session; dialogs' lines due.
  // Nothing before the user has typed in a session here: wb's own --standalone helpers load the
  // user's global plugin too (store.DRIVERS), and none of them may start the chat or claim it
  async poll() {
    if (!this.listening) return
    await this.check()
    if (!this.on) return
    // listener.json still ours? `wb chat off` and on again empty it between two checks: taken
    // again at once; another listener's claim is tried again at the next check (it may end)
    const claim = this.claim()
    if (!(claim.tool === 'opencode' && claim.pid === this.deps.pid && claim.session === this.listening)) {
      this.holding = false
      if (!claim.session) await this.hold(this.listening)
      if (!this.holding) return
    }
    // a dialog up two seconds, or one settled: the claim says so (#1355)
    if (!sameWait(waitingFact([...this.dialogs.values()], this.deps.now()), this.waitSaid)) await this.hold(this.listening)
    await this.notices()
    if (this.reverting || !this.sessions) return
    let work: ChatOpen = {}
    try { work = JSON.parse(this.deps.read(this.path('open.json')) ?? '{}') } catch { return }
    const waiting: Partial<ChatMsg>[] = Object.values((work.messages && typeof work.messages === 'object') ? work.messages : {})
    const now = this.deps.now()
    for (const m of waiting.sort((a, b) => Number(a.ts ?? 0) - Number(b.ts ?? 0))) {
      const id = String(m.id ?? '')
      const agent = agentOf(m)
      if ((m.from !== 'user' && !agent) || !id || this.sent.has(id) || this.expired.has(id)) continue
      if ((this.retry.get(id) ?? 0) > now) continue
      const sid = this.listening
      // under wb's lock: one session submits it (#84), never one too old (#536)
      const claimed = await this.deps.run(['chat', 'claim', id, '--by', sid], { session: sid })
      if (!claimed.ok) {
        if (/ expired: /.test(claimed.out)) this.expired.add(id)
        else this.retry.set(id, now + RETRY_MS)
        continue
      }
      const inbox = inboxId(id)
      if (!inbox) continue // no id of wb's shape (chat.send's): never one to hand over
      this.sent.add(id)
      // an agent's into a turn it didn't open is `into`: that turn's plain reply isn't its answer (#1175)
      this.inflight.set(inbox, { id, agent, into: !!agent && this.busy })
      try {
        await this.sessions.prompt({ sessionID: sid, id: inbox, text: chatPrompt(m),
          metadata: { source: SOURCE, chat: id }, delivery: 'steer' })
      } catch (err) {
        this.inflight.delete(inbox)
        if (conflict(err)) continue // that id is in another session already: delivered there
        this.sent.delete(id)
        this.retry.set(id, now + RETRY_MS)
      }
    }
  }

  turn(sid: string): Turn {
    let t = this.turns.get(sid)
    if (!t) this.turns.set(sid, t = { ids: [], size: -1, message: '', parts: [] })
    return t
  }

  // OpenCode delivered it: the receipt, and away for the user's (the mod writes the same)
  async delivered(sid: string, inbox: string) {
    const f = this.inflight.get(inbox)
    if (!f) return
    this.inflight.delete(inbox)
    await this.deps.run(['chat', 'ack', f.id, '--by', sid, '--how', f.into ? 'into' : 'now',
      ...(f.agent ? [] : ['--away'])], { session: sid })
    this.owed.set(f.id, { session: sid, sender: f.agent || 'user', answer: '' })
    this.turn(sid).ids.push(f.id)
  }

  // The user cancelled it in OpenCode (only Session.cancelInbox publishes this: the TUI's or the
  // app's cancel of a pending item): dismissed at the terminal, never a failed delivery, so it is
  // settled with a receipt and never prompted again (sent again a minute later, each cancel came
  // back for six hours: the chat-path review, 2026-11-25); not owed, and the user is back
  async cancelled(sid: string, inbox: string) {
    const f = this.inflight.get(inbox)
    if (!f) return
    this.inflight.delete(inbox)
    const by = sid || this.listening
    await this.deps.run(['chat', 'ack', f.id, '--by', by, '--how', 'now'], { session: by })
    if (!f.agent) await this.deps.run(['chat', 'back'], { session: by })
  }

  // the listening session's, or one still owing an answer to a message it was given
  ours(sid: string) {
    return !!sid && (sid === this.listening || [...this.owed.values()].some(o => o.session === sid))
  }

  async started(sid: string) {
    if (!this.ours(sid)) return
    const t = this.turn(sid) // its ids stay: a message delivered to an idle session comes first
    t.size = this.deps.size(this.path('messages.jsonl'))
    t.message = ''
    t.parts = []
    if (sid !== this.listening) return
    this.busy = true
    if (this.holding) await this.hold(sid) // listener.json's `busy`, which the page shows
  }

  // the last assistant message's text, which is the turn's answer when it ends
  text(sid: string, message: string, text: string) {
    const t = this.turns.get(sid)
    if (!t) return
    if (message !== t.message) { t.message = message; t.parts = [] }
    if (text.trim()) t.parts.push(text)
  }

  // The turn ended: its answer posted to whoever it still owes (one post per sender, #1273), then
  // the turn-end record (after the post, #1352), the dialogs' lines cleared, `busy` off
  async ended(sid: string) {
    const wasBusy = sid === this.listening && this.busy
    if (sid === this.listening) this.busy = false
    const t = this.turns.get(sid)
    this.turns.delete(sid)
    if (t) await this.answered(sid, t)
    for (const [key, d] of [...this.dialogs]) if (d.session === sid) await this.dialogDone(key, false)
    if (wasBusy && this.holding) await this.hold(sid)
  }

  async answered(sid: string, t: Turn) {
    const answer = t.parts.join('\n\n').trim().slice(0, ANSWER_ARGV)
    const due = new Map<string, string[]>()
    for (const [id, o] of this.owed) {
      if (o.session !== sid) continue
      o.answer ||= answer
      if (!o.answer) continue // an interrupt, thinking only: the next turn's end tries again
      const key = JSON.stringify([o.sender, o.answer])
      due.set(key, [...due.get(key) ?? [], id])
    }
    for (const [key, ids] of due) {
      const text = JSON.parse(key)[1] as string
      const { ok } = await this.deps.run(['chat', 'reply', '--turn-answer', '--if-owed', `--text=${text}`,
        ...ids.map(id => `--to=${id}`)], { session: sid })
      if (ok) for (const id of ids) this.owed.delete(id) // posted, or replied to already
    }
    const grew = t.size >= 0 && this.deps.size(this.path('messages.jsonl')) > t.size
    if (this.on && (t.ids.length || grew)) await this.deps.run(['chat', 'ended', ...t.ids, '--by', sid], { session: sid })
  }

  // A dialog came up (a permission, a form) in the listening session or a child of it: the page
  // says so after NOTICE_AFTER_MS, at once while away (#1340 E)
  async dialogUp(key: string, sid: string, kind: string, tool: string, text: string) {
    if (!this.on || !this.holding || (await this.root(sid)) !== this.listening) return
    const away = (await this.deps.run(['chat', 'away'], { session: this.listening })).out.trim() === 'away'
    const now = this.deps.now()
    this.dialogs.set(key, { session: this.listening, kind, tool, text: text.slice(0, 1500), at: now,
      due: now + (away ? 0 : NOTICE_AFTER_MS), id: '', sending: false, done: false })
    if (away) await this.notices()
  }

  // A dialog's line once due, one open at a time (the mod's termNotices)
  async notices() {
    const all = [...this.dialogs.values()]
    if (all.some(d => d.sending || (d.id && !d.done))) return
    const d = all.find(d => !d.done && !d.id && this.deps.now() >= d.due)
    if (!d) return
    d.sending = true
    try {
      const { ok, out } = await this.deps.run(['chat', 'notice', `--tool=${d.tool}`, `--kind=${d.kind}`,
        `--text=${d.text}`], { session: d.session })
      const id = (out.split('\n').pop() || '').trim()
      d.id = ok && /^\d+-[0-9a-f]+$/.test(id) ? id : 'none' // 'none': skipped (no chat user), or failed
    } finally {
      d.sending = false
    }
    if (d.done) await this.lineDone(d)
  }

  // The dialog settled: answered at the terminal (the user is back, unless OpenCode answered it
  // itself, at once: BACK_AFTER_MS), or ended with its turn
  async dialogDone(key: string, answered: boolean) {
    const d = this.dialogs.get(key)
    if (!d) return
    d.done = true
    this.dialogs.delete(key)
    if (answered && this.deps.now() - d.at >= BACK_AFTER_MS) await this.deps.run(['chat', 'back'], { session: d.session })
    if (!d.sending) await this.lineDone(d)
  }

  async lineDone(d: Dialog) {
    if (d.id && d.id !== 'none') await this.deps.run(['chat', 'notice', '--done', d.id], { session: d.session })
  }

  // `execute.before`: while the user is away, the question tool is refused into the web chat
  async beforeTool(e: { tool: string; sessionID: string }) {
    if (e.tool !== 'question' || !this.on || !this.listening) return
    if ((await this.root(e.sessionID)) !== this.listening) return
    if ((await this.deps.run(['chat', 'away'], { session: this.listening })).out.trim() === 'away') {
      throw new Error(AWAY_QUESTION)
    }
  }

  // One event from OpenCode's stream, once however many instances feed it
  async event(ev: OcEvent) {
    if (ev.id) {
      if (this.seen.includes(ev.id)) return
      this.seen.push(ev.id)
      if (this.seen.length > 500) this.seen.splice(0, 250)
    }
    const d = ev.data ?? {}
    const sid = String(d.sessionID ?? '')
    switch (ev.type) {
      case 'session.execution.started': return this.started(sid)
      case 'session.execution.succeeded':
      case 'session.execution.failed':
      case 'session.execution.interrupted': return this.ended(sid)
      case 'session.inbox.delivered': return this.delivered(sid, String(d.inboxID ?? ''))
      case 'session.inbox.cancelled': return this.cancelled(sid, String(d.inboxID ?? ''))
      case 'session.text.ended': return this.text(sid, String(d.assistantMessageID ?? ''), String(d.text ?? ''))
      case 'session.revert.staged': if (sid === this.listening) this.reverting = true; return
      case 'session.revert.cleared':
      case 'session.revert.committed': if (sid === this.listening) this.reverting = false; return
      case 'permission.asked':
        return this.dialogUp(`p:${String(d.id ?? '')}`, sid, 'permission', String(d.action ?? 'permission'),
          Array.isArray(d.resources) ? d.resources.join(' ') : '')
      case 'permission.replied': return this.dialogDone(`p:${String(d.requestID ?? '')}`, true)
      case 'form.created': {
        const form = (d.form ?? {}) as Row
        return this.dialogUp(`f:${String(form.id ?? '')}`, String(form.sessionID ?? ''), formKind(form),
          formKind(form) === 'question' ? 'question' : 'form', String(form.title ?? ''))
      }
      case 'form.replied': return this.dialogDone(`f:${String(d.id ?? '')}`, true)
      case 'form.cancelled': return this.dialogDone(`f:${String(d.id ?? '')}`, false)
    }
  }
}

// wb, run as the plugin runs it: the options' wb and WB_* paths (wb.ts), as the OpenCode session
// ``session`` (OPENCODE_SESSION_ID, which wb reads for the driver's identity, #1309), never as
// Claude Code or an agent
export function runner(options: ServerContext['options'], base: Record<string, string | undefined> = process.env): Run {
  return (args, opts = {}) => new Promise(done => {
    const env: Record<string, string> = {}
    for (const [k, v] of Object.entries(wbEnv(options, base))) if (typeof v === 'string') env[k] = v
    for (const k of ['CLAUDECODE', 'CLAUDE_PID', 'CLAUDE_CODE_SESSION_ID', 'WB_TASK', 'OPENCODE_SESSION_ID']) delete env[k]
    if (opts.session) env.OPENCODE_SESSION_ID = opts.session
    const wb = findWb(options, env).wb
    execFile(wb, args, { env, cwd: opts.cwd, timeout: RUN_MS, maxBuffer: 1 << 20 }, (err, stdout, stderr) =>
      done({ ok: !err, out: `${String(stdout ?? '')}${err ? String(stderr ?? '') : ''}`.trim() }))
  })
}

// The chat_reply tool (in the `workbench` namespace, so the model sees `workbench_chat_reply`): `wb chat reply` as the calling session's
// root, so a subagent's reply is its driver's, and wb threads it onto the messages that session
// took (chat.caller_listens by OPENCODE_SESSION_ID)
export function replyTool(lane: ChatLane, run: Run) {
  return {
    name: REPLY_TOOL,
    description: 'Reply to the user (or to another agent) in Workbench\'s web chat: for a [web chat] ' +
      'message, briefly, and for what finishes or needs them while they are away. `to`: the ids of the ' +
      'messages it answers (an agent\'s [message <id>]); none: the messages this turn took, the user\'s.',
    input: {
      type: 'object',
      properties: {
        text: { type: 'string', description: 'the reply, plain text' },
        to: { type: 'array', items: { type: 'string' }, description: 'message ids it answers, or "user"' },
      },
      required: ['text'],
      additionalProperties: false,
    },
    options: { namespace: NAMESPACE },
    async execute(input: unknown, context: { sessionID: string }) {
      const i = (input ?? {}) as { text?: unknown; to?: unknown }
      const text = String(i.text ?? '').trim()
      if (!text) throw new Error('the reply is empty')
      const to = Array.isArray(i.to) ? i.to.map(String).filter(Boolean) : []
      const sid = (await lane.root(context.sessionID)) || context.sessionID
      const { ok, out } = await run(['chat', 'reply', `--text=${text}`, ...to.map(t => `--to=${t}`)], { session: sid })
      if (!ok) throw new Error(out || 'wb chat reply failed')
      return { content: out }
    },
  }
}

// One lane per server process (globalThis: a module may be loaded more than once, and OpenCode may
// set the plugin up once per location): each instance adds its session API and its event feed
type Shared = { lane: ChatLane; users: number; apis: ChatSessions[]; feeds: (() => void)[]; timer?: unknown }
const KEY = '__workbenchChatLane'

export function lane(make: () => ChatLane): Shared {
  const g = globalThis as Record<string, unknown>
  let s = g[KEY] as Shared | undefined
  if (!s) {
    const made: Shared = { lane: make(), users: 0, apis: [], feeds: [] }
    made.lane.activate = () => {
      if (!made.timer) {
        let polling = false
        made.timer = setInterval(() => {
          if (polling) return
          polling = true
          void made.lane.poll().catch(() => {}).finally(() => { polling = false })
        }, POLL_MS)
      }
      for (const feed of made.feeds) feed()
    }
    g[KEY] = s = made
  }
  s.users++
  return s
}

// The server part's setup of the chat: the tool and the hooks at once; the poll and the event
// feed once a session here listens (`activate`). Nothing without the context's session, tool and
// event domains (a server too old for them, a test's ctx).
export async function chat(ctx: ServerContext): Promise<(() => Promise<void>) | undefined> {
  const c = ctx as unknown as Partial<ChatContext>
  if (!c.session || !c.tool || !c.event) return undefined
  const run = runner(ctx.options)
  const home = wbHome(wbEnv(ctx.options))
  const s = lane(() => new ChatLane({
    run, home, pid: process.pid, now: () => Date.now(),
    read: path => { try { return readFileSync(path, 'utf8') } catch { return undefined } },
    size: path => statSync(path, { throwIfNoEntry: false })?.size ?? -1,
  }))
  const l = s.lane
  const sessions = c.session
  s.apis.push(sessions)
  l.sessions = s.apis[0]
  await c.tool.transform(editor => {
    editor.namespace({ name: NAMESPACE, description: NAMESPACE_DESCRIPTION })
    editor.add(replyTool(l, run))
  })
  await sessions.hook('prompt', e => { void l.typed(e).catch(() => {}) })
  await c.tool.hook('execute.before', e => l.beforeTool(e))
  const abort = new AbortController()
  const events = c.event
  let fed = false
  const feed = () => {
    if (fed || abort.signal.aborted) return
    fed = true
    void (async () => {
      while (!abort.signal.aborted) {
        try {
          for await (const ev of events.subscribe({ types: EVENTS, signal: abort.signal })) await l.event(ev).catch(() => {})
        } catch { /* the stream broke: again in a moment */ }
        if (!abort.signal.aborted) await new Promise(r => setTimeout(() => r(undefined), POLL_MS))
      }
    })()
  }
  s.feeds.push(feed)
  if (l.listening) feed()
  return async () => {
    abort.abort()
    s.apis.splice(s.apis.indexOf(sessions), 1)
    s.feeds.splice(s.feeds.indexOf(feed), 1)
    l.sessions = s.apis[0]
    if (--s.users > 0) return
    if (s.timer) clearInterval(s.timer)
    delete (globalThis as Record<string, unknown>)[KEY]
    if (l.listening) await run(['chat', 'listener', '--drop', '--pid', String(process.pid)])
  }
}
