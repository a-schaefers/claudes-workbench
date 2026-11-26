// The driver's conversation, kept (#1315, G4 of #1296's parity matrix): what a Workbench session is
// told of the user's rules, what a compaction keeps, and the compaction at a task boundary. The
// same outcomes as the Claude Code mod's prompt.context blocks, `unsaid` rows, KEEP_ON_COMPACT and
// `compactAtBoundary`, OpenCode-native (the host-shape decision, #1296):
// - The rules are session instruction entries (`session.instructions.entry`, plugin/src/promise/
//   session.ts; core/src/session/instruction-entry.ts), one per context block of the mod's, under
//   the mod's block names (SKILL.md names them). OpenCode sends each as `<context key="…">` with
//   its other instructions, again after every compaction (its summary prompt leaves instructions
//   out: "the next agent receives current versions of all of these separately"), and says a
//   change or a removal to the model itself at the next step ("The context under … changed and
//   supersedes …", "… no longer applies"): the mod's midway `[workbench]` rows. Which blocks a
//   session should have is the kernel's (`tellPlan`, Kernel.Mode): this module puts and removes
//   entries until the session's match. A server without the API (it is experimental) gets the same
//   blocks from the `context` hook, appended to each of the session's requests (the fallback).
// - Workbench mode is the Workbench agent: a session is delegating while its agent is `workbench`.
// - Any compaction of a session that delegated, with a task of its own open, and the boundary
//   compaction itself are told what to keep
//   (KEEP_ON_COMPACT, the mod's words) by the `compaction` hook. OpenCode's own automatic
//   compaction stays on; the boundary compaction (`compactPlan`, Kernel.Mode) runs before it: when
//   one of the session's tasks closed since the last look, none waits for the driver and the
//   session is idle, `session.compact` once the last step's prompt reached WB_COMPACT_AT percent
//   (unset: half; 0: never) of the way to OpenCode's own automatic compaction.
// The facts are the host's: the task files under $WB_HOME (a task is the session's when `wb start`
// recorded it there, task.json `session`, #1309), notes.md, coding.json, workflows.json, the roles
// (`wb route`, read when routes.json changes), `wb status` and `wb chat away` as the session (the
// `web-chat` block while the user is away, removed when they are back), and the plugin's `wake`
// option (`workbench-wake` while it is "off", as notices.ts reads it). The kernel comes from
// the membrane (../mod/hooks/core.ts), imported when the server part sets this up: a copy of the
// plugin without it (an older wb setup --opencode copy) skips this part and keeps the rest.
// Nothing runs at import time.
import { readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'
import type * as Core from '../mod/hooks/core.ts'
import { AGENT } from './agent.ts'
import { REPLY_ID, typedByUser } from './chat.ts'
import { sessionRunner, whereOf, type SessionRun } from './commands.ts'
import { wbEnv, wbHome, type Options } from './wb.ts'

export type Kernel = Pick<typeof Core, 'ACTIVE' | 'NEEDS' | 'phaseOf' | 'tellPlan' | 'compactPlan'>
type Said = Core.Said

// The mod's words for what a compaction keeps (mod/hooks/register.tsx KEEP_ON_COMPACT, word for word)
export const KEEP_ON_COMPACT =
  'Workbench delegation is in progress. Keep: the user\'s goals and decisions, which tasks were ' +
  'delegated (names, repos, branches, what each is for), which are open and what each needs ' +
  'next, and what was accepted, merged or discarded. Drop the diffs, reports and transcripts of ' +
  'finished tasks; `wb status` and `wb report <name>` bring back any detail.'

// Each block's entry key: the mod's context block names, which SKILL.md names (`claude-coding`,
// `dynamic-workflows`, `user-notes`, `workbench-mode`, `workbench`, `web-chat`); InstructionEntry.Key
// is lowercase alphanumerics and `.`, `_`, `-`
export const KEYS: Readonly<Record<string, string>> = {
  mode: 'workbench-mode', coding: 'claude-coding', notes: 'user-notes', workflows: 'dynamic-workflows',
  away: 'web-chat', wakeOff: 'workbench-wake', routes: 'workbench-routes',
}
export const TASKS_KEY = 'workbench'
export const OURS = new Set([...Object.values(KEYS), TASKS_KEY])

export const MODE_NOTE =
  'Workbench mode is on in this session: you are the Workbench agent, until the user runs ' +
  '/workbench-quit or switches to another agent. Treat each request as your Workbench instructions ' +
  'say: hand its coding to agents with wb, then review and accept. Every plan you write is a handoff ' +
  'plan: steps sized to the agents\' slice, each with its files, exact interfaces and acceptance ' +
  'command (*Planning work you will hand off*).'
export const CODING_RULES: Readonly<Record<string, string>> = {
  ask: 'Your coding is "Ask me first" (the user\'s choice; `wb coding` shows it): your edits in a ' +
    'task\'s worktree need the user\'s yes first; record it with `wb coding allow <task> --edit "…" ' +
    '--said "<their words>"`.',
  only: 'Your coding is "Only when I say" (the user\'s choice; `wb coding` shows it). Never write ' +
    'a task\'s code yourself, not a one-line fix in review nor `wb takeover`: guide the agent, ' +
    'and when it stays stuck, report the blocker to the user and keep the work with the agent. ' +
    'They say when you write code; then record their words with `wb coding allow <task> --edit ' +
    '"…" --said "<their words>"`.',
}
export const NOTES_NOTE = (notes: string) => `The user's delegation notes (\`wb notes\`):\n${notes}`
export const WORKFLOWS_RULE = 'Dynamic workflows are on (the user\'s choice; `wb deepmode` shows it): run a substantive ' +
  'request as a deep workflow on their agents, as your Workbench instructions\' *Deep workflows* says: pick a flow ' +
  '(`wb run flows`), say it in at most three lines, start it once with `wb run start`, then wait for its notices ' +
  'with `wb wait <run>` in the background (the shell tool\'s `background: true`), again after each. Never fan out ' +
  'by starting several tasks at once yourself: the run starts its items, each on its own role\'s model, never one ' +
  'you pick, and the GPU lease, the provider slots and the day cap decide what runs together. Review each result ' +
  'on its own merits; merge logic first, then tests, then UI or docs.'
export const ROUTES_NOTE = (routes: string) => `Roles (the user's, \`wb route\`):\n${routes}\n` +
  'Never pass --model for an API model or change a role without the user\'s yes (wb refuses it without ' +
  'their yes); recommend only.'
// The mod's AWAY_NOTE and WAKE_OFF_NOTE in OpenCode's words: the web chat's tool, the plugin's
// `wake` option (notices.ts), the TUI's toasts
export const AWAY_NOTE =
  'The user is away from the terminal: they last used the web chat. Tell them there (the ' +
  `${REPLY_ID} tool) when something they asked for is done or needs them; \`wb chat log\` shows the ` +
  'thread. Typing in the terminal, or answering a prompt there, ends this.'
export const WAKE_OFF_NOTE =
  'Wakes are off (the workbench plugin\'s `wake` option): no [workbench] message will say when a task ' +
  'needs you. After `wb start`, tell the user it runs and that a toast says when it is ready; pick it ' +
  'up when they ask (`wb report <name>`), and wait (`wb wait <name>`) only if they ask you to.'
export const TASKS_NOTE = (status: string) =>
  `Tasks delegated to agents with wb (your Workbench instructions) are open:\n${status}\n` +
  'Pick them up from here: `wb report <name>` for one, `wb watch <name>` to follow it.'

// What the facts are read with: the files, wb as the session, the clock and the environment
export interface Io {
  readonly read: (path: string) => string | undefined
  readonly list: (dir: string) => readonly string[]
  readonly run: SessionRun
  readonly env: Record<string, string | undefined>
  readonly now: () => number
}

export interface Task { readonly name: string; readonly phase: string; readonly parked: boolean }

const json = (text: string | undefined): Record<string, unknown> => {
  try {
    const v = JSON.parse(text ?? '')
    return v && typeof v === 'object' && !Array.isArray(v) ? v as Record<string, unknown> : {}
  } catch {
    return {}
  }
}

// The session's open tasks (active or waiting on the driver): those `wb start` recorded for it
export function openTasks(k: Kernel, io: Io, home: string, sessionID: string): Task[] {
  const dir = join(home, 'tasks')
  const out: Task[] = []
  for (const name of io.list(dir)) {
    const st = json(io.read(join(dir, name, 'status.json')))
    // a task without a status yet is no one's open task until its first one (the next look sees it)
    const phase = String(st.phase ?? '')
    if (!k.ACTIVE.has(phase) && !k.NEEDS.has(phase)) continue
    const sp = json(io.read(join(dir, name, 'task.json')))
    if (!sp.name || sp.bench || sp.session !== sessionID) continue
    out.push({ name, phase, parked: !!st.parked })
  }
  return out.sort((a, b) => a.name < b.name ? -1 : a.name > b.name ? 1 : 0)
}

// The roles' file as wb reads it (WB_ROUTES, `~` expanded, else ~/.config/workbench/routes.json)
export function routesPath(env: Record<string, string | undefined>): string {
  const home = env.HOME ?? '~'
  const given = env.WB_ROUTES
  if (!given) return join(home, '.config', 'workbench', 'routes.json')
  return given === '~' ? home : given.startsWith('~/') ? join(home, given.slice(2)) : given
}

// Whether `wb route`'s lines are said: routes.json is there and not the plain default (every role
// on the GPU's automatic model, thinking off, no fallback, no day cap, no use or busy setting), or
// can't be read: the mod's `readRoutes` `said`, so a GPU-only change (a pinned model, thinking on)
// reaches the driver too (#1315 review)
export function routesSaid(text: string | undefined): boolean {
  if (text === undefined) return false
  let doc: unknown
  try {
    doc = JSON.parse(text)
  } catch {
    return true // wb route says why
  }
  if (!doc || typeof doc !== 'object' || Array.isArray(doc)) return true
  const d = doc as Record<string, any>
  if (d.roles !== undefined && (!d.roles || typeof d.roles !== 'object' || Array.isArray(d.roles))) return true
  const rows = Object.values((d.roles ?? {}) as Record<string, any>)
  const plain = d.day_usd == null && (d.use ?? 'both') === 'both' && (d.busy ?? 'wait') === 'wait' &&
    rows.every(r => !!r && typeof r === 'object' && (r.via ?? 'gpu') === 'gpu' && (r.gpu ?? 'auto') === 'auto' && !r.api &&
      (r.thinking ?? 'off') === 'off' && (r.thinking_review ?? 'off') === 'off' && !r.fallback)
  return !plain
}

// The blocks a session should have, by key, in the kernel's order: `want` as the mod's `wanted`
// reads it, the roles as `wb route` says them, the open tasks as `wb status` does
// and `away` while the user is away (the entry removed when they are back: "… no longer applies")
export function blocksOf(k: Kernel, want: Said, delegating: boolean, routes: string, tasks: string, away = false): [string, string][] {
  const plan = k.tellPlan({ first: true, want, was: want, known: false, any: true, delegating, away })
  const out: [string, string][] = []
  for (const say of plan.say) {
    const key = KEYS[say]
    if (!key) continue
    if (say === 'mode') out.push([key, MODE_NOTE])
    else if (say === 'coding' && CODING_RULES[want.coding]) out.push([key, CODING_RULES[want.coding]!])
    else if (say === 'notes') out.push([key, NOTES_NOTE(want.notes)])
    else if (say === 'workflows') out.push([key, WORKFLOWS_RULE])
    else if (say === 'routes' && routes) out.push([key, ROUTES_NOTE(routes)])
    else if (say === 'away') out.push([key, AWAY_NOTE])
    else if (say === 'wakeOff') out.push([key, WAKE_OFF_NOTE])
  }
  if (tasks) out.push([TASKS_KEY, TASKS_NOTE(tasks)])
  return out
}

// OpenCode's automatic compaction point for a window, as it computes it with no `buffer` set
// (core/src/session/compaction.ts calculateCeiling: 15% of the window kept free, at least 16K
// when the window holds twice that)
export function autoCeiling(window: number): number {
  if (!(window > 0)) return 0
  return window - Math.max(Math.floor(window * 0.15), window >= 32_000 ? 16_000 : 0)
}

// schema/src/model.ts `Model.Info.limit`: the input window (core/src/session/compaction.ts inputWindow)
export function inputWindow(limit: { context?: number; input?: number } | undefined): number {
  if (!limit) return 0
  const c = Number(limit.context) || 0, i = Number(limit.input) || 0
  return i && c ? Math.min(i, c) : i || c
}

// The prompt a step sent, as OpenCode measures it (schema/src/token-usage.ts): its input, cache
// reads and cache writes
export function promptTokens(tokens: unknown): number {
  const t = (tokens ?? {}) as { input?: unknown; cache?: { read?: unknown; write?: unknown } }
  return (Number(t.input) || 0) + (Number(t.cache?.read) || 0) + (Number(t.cache?.write) || 0)
}

// OpenCode 2.4.1's shapes this module uses (host.d.ts's rule: each from its source)
interface ModelRef { readonly providerID: string; readonly id: string }
interface Entry { readonly key: string; readonly value: unknown }
export interface UpkeepHost {
  readonly options: Options
  readonly session: {
    readonly hook: (name: string, callback: (input: any) => unknown) => Promise<{ dispose(): Promise<void> }>
    readonly get?: (input: { readonly sessionID: string }) => Promise<unknown>
    readonly compact?: (input: { readonly sessionID: string }) => Promise<unknown>
    // plugin/src/promise/session.ts `Pick<ExperimentalSessionApi, "instructions">`
    readonly instructions?: { readonly entry?: {
      readonly list: (input: { readonly sessionID: string }) => Promise<unknown>
      readonly put: (input: { readonly sessionID: string; readonly key: string; readonly value: string }) => Promise<unknown>
      readonly remove: (input: { readonly sessionID: string; readonly key: string }) => Promise<unknown>
    } }
  }
  readonly event: { readonly subscribe: (options: { readonly types: readonly string[]; readonly signal?: AbortSignal }) => AsyncIterable<{ readonly type: string; readonly id?: string; readonly data?: any }> }
  // client `ModelListOutput`: each model's limit
  readonly model?: { readonly list?: (input?: object) => Promise<unknown> }
}

// One session's state in this server, from the hooks and events that name it
export interface Sess {
  agent: string
  dir: string
  delegated: boolean           // its agent was the Workbench agent at some point in this process
  open: string[]               // its open tasks at the last look
  boundary: boolean            // compactPlan's mark between looks
  keepNext: boolean            // our boundary compaction asked for: KEEP_ON_COMPACT though no task is open
  busy: boolean
  tokens: number               // the last step's prompt
  model?: ModelRef
  blocks: [string, string][]   // what it should have now
  put: Map<string, string> | undefined  // the entries it has (undefined: not listed yet)
  tasksSig: string             // the open tasks the `workbench` block was read for
  tasksText: string
  chain: Promise<void>         // its refreshes, in order
}

export const EVENTS = ['session.agent.selected', 'session.execution.started', 'session.execution.succeeded',
  'session.execution.failed', 'session.execution.interrupted', 'session.step.ended', 'session.compaction.ended']

export class Upkeep {
  readonly sessions = new Map<string, Sess>()
  private routesSeen: string | undefined = undefined
  private routesText = ''
  private windows = new Map<string, number>()

  constructor(readonly k: Kernel, readonly host: UpkeepHost, readonly io: Io, readonly home: string) {}

  get entries() { return this.host.session.instructions?.entry }

  sess(sessionID: string): Sess {
    let s = this.sessions.get(sessionID)
    if (!s) {
      s = { agent: '', dir: '', delegated: false, open: [], boundary: false, keepNext: false, busy: false, tokens: 0, blocks: [],
        put: undefined, tasksSig: '', tasksText: '', chain: Promise.resolve() }
      this.sessions.set(sessionID, s)
    }
    return s
  }

  setAgent(s: Sess, agent: string): boolean {
    const was = s.agent
    s.agent = agent
    if (agent === AGENT) s.delegated = true
    return was !== agent
  }

  // The session's agent and directory from OpenCode, once
  async learn(sessionID: string): Promise<Sess> {
    const s = this.sess(sessionID)
    if (!s.dir) {
      const where = await whereOf(this.host as never, sessionID)
      s.dir = where.dir
      if (where.agent && !s.agent) this.setAgent(s, where.agent)
    }
    return s
  }

  // the roles as `wb route` says them, read again only when routes.json changed
  async routes(): Promise<string> {
    const text = this.io.read(routesPath(this.io.env))
    if (!routesSaid(text)) return ''
    if (text !== this.routesSeen) {
      const r = await this.io.run(['route'], {})
      this.routesText = r.ok ? r.out : 'routes.json can\'t be read (wb route says why)'
      this.routesSeen = text
    }
    return this.routesText
  }

  // Whether the user is away for this session (wb's `chat.away`: the web chat used last, within
  // six hours or while the session has a task open): `wb chat away` as the session, only when
  // chat/away.json has a time at all (no time is never away: no wb run at every prompt)
  async away(sessionID: string): Promise<boolean> {
    if (!(Number(json(this.io.read(join(this.home, 'chat', 'away.json'))).at) > 0)) return false
    const r = await this.io.run(['chat', 'away'], { session: sessionID })
    return r.ok && r.out.trim() === 'away'
  }

  // What the session should be told now (the mod's `wanted`), and its blocks: one at a time per
  // session, so a prompt's and an idle look's never put and remove across each other. `back`: the
  // prompt it is for was typed by the user, who is then not away (chat.ts's `wb chat back` for it
  // runs beside this, not before)
  refresh(sessionID: string, back = false): Promise<void> {
    const s = this.sess(sessionID)
    const next = s.chain.then(() => this.refreshNow(sessionID, s, back))
    s.chain = next.catch(() => {})
    return next
  }

  private async refreshNow(sessionID: string, s: Sess, back: boolean): Promise<void> {
    const delegating = s.agent === AGENT
    const tasks = openTasks(this.k, this.io, this.home, sessionID)
    const any = delegating || tasks.length > 0
    const read = (f: string) => this.io.read(join(this.home, f))
    const coding = any ? String(json(read('coding.json')).mode ?? '') : ''
    const routes = any ? await this.routes() : ''
    const want: Said = {
      notes: delegating ? (read('notes.md') ?? '').trim() : '',
      wakeOff: any && this.host.options?.wake === 'off', // notices.ts's `wake` option (the mod's)
      coding: coding in CODING_RULES ? coding : 'decide',
      routes,
      workflows: any && json(read('workflows.json')).mode === 'on' ? 'on' : 'off',
    }
    // the open tasks as wb lists them, for this session in its directory; read again when they change
    const sig = tasks.map(t => `${t.name}:${t.phase}`).join(' ')
    if (sig !== s.tasksSig) {
      s.tasksSig = sig
      s.tasksText = ''
      if (tasks.length) {
        const r = await this.io.run(['status'], { session: sessionID, cwd: s.dir })
        if (r.ok && r.out && !r.out.startsWith('no open tasks')) s.tasksText = r.out
      }
    }
    const away = any && !back && await this.away(sessionID)
    s.blocks = blocksOf(this.k, want, delegating, routes, s.tasksText, away)
    await this.sync(sessionID, s)
  }

  // The session's entries made its blocks: each put that differs, each of ours it shouldn't have
  // removed; listed once, so a server restarted keeps what is there
  async sync(sessionID: string, s: Sess): Promise<void> {
    const api = this.entries
    if (!api) return // the fallback: the context hook sends `blocks`
    if (!s.put) {
      const got = await api.list({ sessionID })
      const rows = (Array.isArray(got) ? got : (got as { data?: unknown })?.data) as Entry[] | undefined
      s.put = new Map((Array.isArray(rows) ? rows : []).filter(e => OURS.has(e?.key) && typeof e.value === 'string')
        .map(e => [e.key, e.value as string]))
    }
    const want = new Map(s.blocks)
    for (const [key, text] of want) {
      if (s.put.get(key) === text) continue
      await api.put({ sessionID, key, value: text })
      s.put.set(key, text)
    }
    for (const key of [...s.put.keys()]) {
      if (want.has(key)) continue
      await api.remove({ sessionID, key })
      s.put.delete(key)
    }
  }

  // The context hook: the agent noted (a switch read again, before the request when it can), the
  // model for the window; without the entries API, the blocks added to the request's system prompt
  async context(input: { sessionID: string; agent: string; model?: ModelRef; system: { type: 'text'; text: string }[] }): Promise<void> {
    const s = this.sess(input.sessionID)
    if (input.model?.providerID && input.model.id) s.model = { providerID: input.model.providerID, id: input.model.id }
    if (this.setAgent(s, input.agent)) await within(this.refresh(input.sessionID), REFRESH_WAIT_MS)
    if (this.entries) return
    for (const [key, text] of s.blocks) input.system.push({ type: 'text', text: `<context key="${key}">\n${text}\n</context>` })
  }

  // The compaction hook: what to keep, in a session that delegated with a task of its own open, or
  // in our own boundary compaction, which follows the last task closing as often as not (the
  // mod's compactAtBoundary always passes KEEP_ON_COMPACT; #1315 review)
  compaction(input: { sessionID: string; system: { type: 'text'; text: string }[] }): void {
    const s = this.sessions.get(input.sessionID)
    if (!s?.delegated) return
    if (!s.keepNext && !openTasks(this.k, this.io, this.home, input.sessionID).length) return
    if (input.system.some(p => typeof p?.text === 'string' && p.text.includes(KEEP_ON_COMPACT))) return
    input.system.push({ type: 'text', text: KEEP_ON_COMPACT })
  }

  // A look at the session, idle after a turn: its tasks, its blocks, and the boundary compaction
  async look(sessionID: string): Promise<void> {
    const s = this.sess(sessionID)
    const tasks = openTasks(this.k, this.io, this.home, sessionID)
    const open = tasks.map(t => t.name)
    const at = { on: s.agent === AGENT, was: s.open, open, boundary: s.boundary, busy: s.busy,
      waiting: tasks.some(t => this.k.phaseOf(t).waits) }
    const plan = this.k.compactPlan({ ...at, percentSet: false, percent: 0, tokens: 0, window: 0, autoCompact: false, threshold: 0 })
    s.open = open
    s.boundary = plan.boundary
    await this.refresh(sessionID).catch(() => {})
    if (plan.check) await this.compactAtBoundary(sessionID, s, at)
  }

  async window(model: ModelRef | undefined): Promise<number> {
    if (!model || !this.host.model?.list) return 0
    const key = `${model.providerID}/${model.id}`
    if (!this.windows.has(key)) {
      try {
        const got = await this.host.model.list({})
        const rows = (Array.isArray(got) ? got : (got as { data?: unknown })?.data) as
          { providerID?: string; id?: string; limit?: { context?: number; input?: number } }[] | undefined
        for (const m of Array.isArray(rows) ? rows : []) {
          if (m?.providerID && m.id) this.windows.set(`${m.providerID}/${m.id}`, inputWindow(m.limit))
        }
      } catch { /* none read: unknown, never compacted */ }
    }
    return this.windows.get(key) ?? 0
  }

  async compactAtBoundary(sessionID: string, s: Sess, at: Omit<Parameters<Kernel['compactPlan']>[0], 'percentSet' | 'percent' | 'tokens' | 'window' | 'autoCompact' | 'threshold'>): Promise<boolean> {
    const set = this.io.env.WB_COMPACT_AT // a percentage; 0 turns it off (the kernel's)
    const percentSet = !!set && Number.isFinite(Number(set))
    if (!this.host.session.compact) return false
    const window = await this.window(s.model)
    if (!(window > 0)) return false // an unknown window: OpenCode never compacts it by itself either
    const plan = this.k.compactPlan({ ...at, percentSet, percent: percentSet ? Number(set) : 0, tokens: s.tokens,
      window, autoCompact: true, threshold: autoCeiling(window) })
    if (!plan.compact) return false
    s.keepNext = true // cleared by session.compaction.ended
    try {
      await this.host.session.compact({ sessionID })
      return true
    } catch {
      s.keepNext = false
      return false // a turn began meanwhile: the next boundary tries again
    }
  }

  // One event from OpenCode's stream, for a session this server's hooks have named
  async event(ev: { type: string; data?: any }): Promise<void> {
    const sid = String(ev.data?.sessionID ?? '')
    // a switch to the Workbench agent names a session too: its blocks are there before its first request
    if (ev.type === 'session.agent.selected' && ev.data?.agent === AGENT && sid && !this.sessions.has(sid)) await this.learn(sid)
    const s = this.sessions.get(sid)
    if (!s) return
    switch (ev.type) {
      case 'session.agent.selected':
        if (this.setAgent(s, String(ev.data?.agent ?? '')) || !s.put) await this.refresh(sid)
        return
      case 'session.execution.started': s.busy = true; return
      case 'session.step.ended': s.tokens = promptTokens(ev.data?.tokens); return
      case 'session.compaction.ended': s.tokens = 0; s.keepNext = false; return
      case 'session.execution.succeeded':
      case 'session.execution.failed':
      case 'session.execution.interrupted':
        s.busy = false
        return this.look(sid)
    }
  }
}

export const REFRESH_WAIT_MS = 3_000 // the most a hook waits for the blocks before the request goes

// a promise awaited at most `ms`: a slow wb never holds a request
export async function within(p: Promise<unknown>, ms: number): Promise<void> {
  let timer: unknown
  await Promise.race([p.catch(() => {}), new Promise(r => { timer = setTimeout(() => r(undefined), ms) })])
  clearTimeout(timer as never)
}

// The server part's setup calls this: the hooks and the event feed; nothing without the kernel,
// the session hooks or the event stream
export async function upkeep(host: UpkeepHost, k?: Kernel, io?: Io): Promise<(() => Promise<void>) | undefined> {
  if (!host.session?.hook || !host.event?.subscribe) return undefined
  const kernel = k ?? await import('../mod/hooks/core.ts').catch(() => undefined)
  if (!kernel) return undefined
  const env = wbEnv(host.options)
  const u = new Upkeep(kernel, host, io ?? {
    read: (path) => { try { return readFileSync(path, 'utf8') } catch { return undefined } },
    list: (dir) => { try { return readdirSync(dir) } catch { return [] } },
    run: sessionRunner(host.options),
    env,
    now: () => Date.now(),
  }, wbHome(env))
  const regs = [
    // a prompt: the session's blocks before its turn
    await host.session.hook('prompt', async (e: { sessionID: string; prompt?: { text?: string }; metadata?: Record<string, unknown> }) => {
      if (!e?.sessionID) return
      await within(u.learn(e.sessionID).then(() => u.refresh(e.sessionID, typedByUser(e))), REFRESH_WAIT_MS)
    }),
    await host.session.hook('context', (input) => u.context(input)),
    await host.session.hook('compaction', (input) => u.compaction(input)),
  ]
  const stop = new AbortController()
  void (async () => {
    while (!stop.signal.aborted) {
      try {
        for await (const ev of host.event.subscribe({ types: EVENTS, signal: stop.signal })) await u.event(ev).catch(() => {})
      } catch { /* the stream broke: again in a moment */ }
      if (!stop.signal.aborted) await new Promise(r => setTimeout(() => r(undefined), 2_000))
    }
  })()
  return async () => {
    stop.abort()
    for (const r of regs) await r.dispose().catch(() => {})
  }
}
