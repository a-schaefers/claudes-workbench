// Notices on OpenCode's path (#1313; the host-shape decision, #1296, 2026-11-25): a task's news
// (notify.jsonl: review, question, stuck, failed, check-in) reaches the OpenCode session that owns
// the task once, while it is still true, as a `synthetic` message: OpenCode's own delivery, a
// question into a running turn as a steer, the rest queued (a turn of its own when the session is
// idle). The same outcomes as the Claude Code mod's deliver and rewake (ARCHITECTURE.md, *Notices
// on OpenCode*), the decisions the kernel's (noticesDue, noticePlan, rewakePlan, owns through
// mod/hooks/core.ts, the membrane), wb the system of record: the plugin reads the task files and
// writes only through `wb ack` (the compare-and-set that stays the once-only guarantee). A
// synthetic id is bound to the task, the kind and the notice's ts (`noticeId`), so a resend from
// this server after a restart, or from another server on the same store, admits nothing new
// (OpenCode's inbox returns the first admission: core/src/session/inbox.ts `reconcile`).
//
// One lane per server process (`lanes()`, on globalThis, as the chat's): one `wb events` child
// (its ears: a line on every change), one timer (SCAN_MS, the fallback when the experimental
// `experimental.idle` hook or the event stream is missing), every instance's hooks and events
// feeding it. Nothing runs at import (server.ts's rule): setup builds it. The toasts go to the
// TUI part over the `workbench-notices` RPC's `toast` event (notices-tui.ts), which shows them
// and a desktop notification.
import { spawn } from 'node:child_process'
import { closeSync, openSync, readFileSync, readSync, readdirSync, statSync } from 'node:fs'
import { join, resolve } from 'node:path'
import type { Registration, RpcDefinition, RpcHandlers, ServerContext } from './host.d.ts'
import { findWb, wbEnv, wbHome, type Options } from './wb.ts'
import { ACTIVE, NEEDS, NO_ANSWER, WAKE_OPENING, noticePlan, noticesDue, owns, rewakePlan, runNoticePlan, runNoticesDue } from '../mod/hooks/core.ts'
import type { NoticeRow, StatusFile, TaskFile } from '../mod/types/wire.d.ts'
import { Jobs } from './jobs.ts'

export const SCAN_MS = 15_000          // the timer's look: held notices, the rewake net, the jobs
export const EARS_RETRY_MS = 2_000     // wb events respawned this long after it ends, doubling to a minute
export const RUN_MS = 15_000           // a wb call's limit (ack, chat away)
export const JOB_MS = 120_000          // a background job's (jobs.ts): the mod's runWb, as `wb update --check` fetches
export const NOTIFY_TAIL = 1 << 20     // the mod's: far more than an hour of notices
export const SOURCE = 'workbench.notice' // our synthetic messages' metadata `source`
// The kinds a desktop notification goes with too, and their toasts' look. Keyed by the kinds' bare
// names, never a quoted one: a quoted phase name in opencode-plugin is the kernel check's sign of a
// copied phase set (scripts/kernel.py, phase_literals), and these kinds share the phases' words.
export const ALERT: Readonly<Record<string, 'info' | 'warning'>> = { review: 'info', question: 'info', stuck: 'warning', failed: 'warning' }
export const WAKE_KINDS: ReadonlySet<string> = new Set(Object.keys(ALERT))

// ---- the words (wording stays in each host: kernel/BOUNDARY.md). The mod's HEADLINE, NEXT and
// the rest in register.tsx, said to an OpenCode driver (ARCHITECTURE.md, *Couplings*). The
// commands are wb's own, which the driver runs; a wake opens with WAKE_OPENING, then the task.
export const HEADLINE: Record<string, string> = {
  review: 'is ready for review',
  question: 'asks the reviewer a question (the agent is waiting)',
  stuck: 'was stopped: it got stuck',
  // a failed one says its kind's own name, as a kind without a headline does (headlineOf)
  checkin: 'check-in (still running)',
}
const PERMISSION = 'PERMISSION to ' // ask_mcp.notice's opening of a permission request (an API)
export const NEXT: Record<string, (n: string, id?: string, text?: string) => string> = {
  review: n => `Review it as Workbench's procedure says, from the diff above (\`wb diff ${n}\` when it was too ` +
    `big to include), then \`wb accept ${n} -m "…"\` or \`wb feedback ${n} --message <file>\`.`,
  question: (n, id, text) => `Answer with \`wb answer ${n}${id ? ` --id ${id}` : ''} --text "…"\`; it is blocked until then.` +
    (text?.startsWith(PERMISSION) ? ` A PERMISSION request (a refused command, directory or port): the \`wb allow ${n}\` it ` +
      'names (`--always` to keep it) when it is reasonable, which answers it and restarts the agent with it; ' +
      'else answer with another way.' : ''),
  stuck: n => `Steer it with \`wb feedback ${n} --message <file>\`, or \`wb discard ${n}\`.`,
  failed: n => `Look at \`wb log ${n}\`; then \`wb feedback ${n} --message <file>\` or \`wb discard ${n}\`.`,
  checkin: n => 'As the driver: tell the user in two or three plain lines how it is going (progress, time, ' +
    `anything off), and log the same with \`wb note ${n} --text "…"\`. If it is heading the wrong way, ` +
    `\`wb steer ${n}\` (\`--now\` to interrupt). Keep it short.`,
}
export const BATCH_NEXT = (names: string[]) => names.length > 1
  ? `Nothing went wrong in the tasks themselves: \`wb resume ${names.join(' ')}\` continues them all, in the ` +
    'order they were started (`wb resume <name>` one, `wb discard <name>` drops one).'
  : `Nothing went wrong in the task itself: \`wb resume ${names[0]}\` continues it in a fresh session, ` +
    `\`wb discard ${names[0]}\` drops it.`
export const SPEND_NEXT = (n: string, capped: boolean) => (capped
  ? `Ask the user: a higher cap (\`wb route cap <usd>\`) or tomorrow, then \`wb resume ${n}\`; `
  : `Ask the user for a day cap (\`wb route cap <usd>\`), then \`wb resume ${n}\`; `) +
  `or the route change the notice names, if any, for the next tasks (on their yes), with \`wb takeover ${n}\` ` +
  `or \`wb discard ${n}\` for this one: resume keeps its API model. Never retry it with feedback yourself.`
export const AGENTS_NEXT = (n: string) =>
  'Ask the user: Agents back to Both or API (the web app\'s Settings, or `wb route all both` on their yes), ' +
  `then \`wb resume ${n}\`; or \`wb takeover ${n}\` or \`wb discard ${n}\`. Never retry it with feedback yourself.`
export const PROVIDER_HEADLINE = 'was stopped by its API provider'
export const LOCAL_ONLY_HEADLINE = 'was stopped: its repo became local-only'
const THE_STEP = new Set(['guard', 'agent']) // results.THE_STEP: the agent's own causes
export const OUTSIDE_HEADLINE: Record<string, string> = {
  stuck: 'was stopped (not its agent\'s doing)',
  failed: 'failed (not its agent\'s doing)',
}
export const OUTSIDE_REMEDY = 'Not the agent\'s doing: its remedy is the notice\'s, above; tell the user when it needs them.'
export const OUTSIDE_NEXT = (n: string) => `${OUTSIDE_REMEDY} A notice that names no step: ` +
  `\`wb feedback ${n} --text "carry on"\` once that cause is gone; never discard it for this.`
// a question steered into a running turn (#542): whose it is, and that it can't wait
export const MIDTURN_NOTE = '(Workbench delivered this into your running turn: the agent is blocked until ' +
  'it is answered, and gives up after a while. Answer it now, then carry on.)'
export const MISSED_NOTE = (n: string) => 'No answer reached it in time (you were busy in a turn), so the ' +
  'agent went on with its own assumption and states it in its summary. Check that assumption when you ' +
  `review, or \`wb steer ${n} --text "…"\` now if it is wrong.`
// while the user is away (wb chat away): answer in the web chat, the chat's tool (chat.ts REPLY_ID)
export const AWAY_TAIL = '(The user is away from the terminal: tell them in the web chat with the ' +
  'workbench_chat_reply tool when something they asked for is done or needs them.)'

export type Row = Record<string, any>

const batchOf = (row: NoticeRow): string[] | null =>
  Array.isArray(row.tasks) && row.tasks.length ? row.tasks.map(String) : null
const outsideTheStep = (row: NoticeRow) => typeof row.cause === 'string' && !THE_STEP.has(row.cause)

// A notice's headline (the toast's too), by its kind and cause: the mod's headlineOf
export function headlineOf(row: NoticeRow): string {
  const batch = batchOf(row)
  return batch ? (batch.length > 1 ? `and ${batch.length - 1} more were interrupted` : 'was interrupted')
    : row.cause === 'provider' ? PROVIDER_HEADLINE
    : row.cause === 'local_only' ? LOCAL_ONLY_HEADLINE
    : (outsideTheStep(row) ? OUTSIDE_HEADLINE[row.kind] : undefined) ?? HEADLINE[row.kind] ?? String(row.kind)
}

// Its next step: the mod's nextOf (`capped`: routes.json has a day cap)
export function nextOf(row: NoticeRow, name: string, capped: boolean): string {
  const batch = batchOf(row)
  return batch ? BATCH_NEXT(batch)
    : row.cause === 'spend' ? (row.guard === 'agents' ? AGENTS_NEXT(name) : SPEND_NEXT(name, capped))
    : row.cause === 'local_only' ? OUTSIDE_REMEDY
    : row.cause === 'provider' || (outsideTheStep(row) && OUTSIDE_HEADLINE[row.kind]) ? OUTSIDE_NEXT(name)
    : (NEXT[row.kind] ?? NEXT.failed!)(name, row.id ? String(row.id) : undefined, String(row.text ?? ''))
}

// The wake's text: `[workbench] Task `<n>` <headline>.`, the notice, the next step
export function wakeText(row: NoticeRow, name: string, capped: boolean): string {
  const batch = batchOf(row)
  const who = batch && batch.length > 1 ? `[workbench] Tasks ${batch.map(n => `\`${n}\``).join(', ')} were interrupted`
    : `${WAKE_OPENING} \`${name}\` ${headlineOf(row)}`
  return `${who}.\n\n${String(row.text ?? '')}\n\n${nextOf(row, name, capped)}`
}

export const ago = (ms: number) => {
  const s = Math.max(0, Math.round(ms / 1000))
  return s < 90 ? `${s}s` : s < 5400 ? `${Math.round(s / 60)}m` : `${Math.round(s / 3600)}h`
}

// The inbox id a notice goes in under: OpenCode's own shape (schema/src/identifier.ts `create`:
// `msg_`, 12 hex of the time, 14 more), its time the notice's own, so it sorts where a message
// sent then would (chat.ts's inboxId found another shape sorted wrong on a revert, #1340), and
// the 14 bound to the task, the kind and the notice's ts (its sequence): the same for every try,
// from any server on the store, so a resend is idempotent (the host-shape decision's critic, 3)
export function noticeId(task: string, kind: string, ts: number): string {
  const value = BigInt(Math.max(0, Math.floor(ts * 1000))) * 0x1000n + 2n
  const time = Array.from({ length: 6 }, (_, i) =>
    Number((value >> BigInt(40 - 8 * i)) & 0xffn).toString(16).padStart(2, '0')).join('')
  let h = 0xcbf29ce484222325n // FNV-1a, 64 bits
  for (const ch of `${task}\0${kind}\0${ts}`) {
    h ^= BigInt(ch.codePointAt(0)!)
    h = (h * 0x100000001b3n) & 0xffffffffffffffffn
  }
  return `msg_${time}wn${h.toString(16).padStart(16, '0').slice(0, 12)}`
}

// ---- the lane

export interface Ran { readonly code: number; readonly out: string }
export type Run = (args: readonly string[], session?: string, timeoutMs?: number) => Promise<Ran>

// plugin/src/promise/session.ts's domain, the parts used here (client types.ts
// `SessionSyntheticInput`: `id` the inbox id, `delivery` steer or queue, `resume` false: no wake)
export interface NoticeSessions {
  get(input: { sessionID: string }): Promise<{ id: string; parentID?: string; location?: { directory?: string } }>
  synthetic(input: { sessionID: string; id?: string; text: string; description?: string; metadata?: Row;
    delivery?: 'steer' | 'queue'; resume?: boolean }): Promise<unknown>
}

export interface Toast {
  readonly key: string; readonly title: string; readonly message: string
  readonly variant: 'info' | 'success' | 'warning' | 'error'; readonly sessionID: string; readonly notify: boolean
}

export interface LaneDeps {
  readonly run: Run
  readonly read: (path: string, tail?: number) => string | undefined
  readonly size: (path: string) => number
  readonly list: (dir: string) => string[]
  readonly now: () => number
  readonly pid: number
  readonly home: string
  readonly routes: string     // routes.json (WB_ROUTES, else ~/.config/workbench/routes.json: routes.path)
  readonly wake: boolean      // the config entry's options `wake` isn't "off" (the mod's)
  readonly alive: (pid: number) => boolean
}

// a JSON object wb wrote (wire.d.ts's shape `T`), {} for none
const json = <T extends object = Row>(text: string | undefined): Partial<T> => {
  try {
    const v = JSON.parse(text ?? '')
    return v && typeof v === 'object' ? v : {}
  } catch { return {} }
}
export const parseLines = (text: string): NoticeRow[] => text.split('\n').flatMap(line => {
  try {
    const r = JSON.parse(line)
    return r && typeof r === 'object' && typeof r.task === 'string' && typeof r.ts === 'number' ? [r as NoticeRow] : []
  } catch { return [] }
})
// a run's own notices (runs.Run.notify, #1278's S9): `run`, no `task`
export const parseRunLines = (text: string): NoticeRow[] => text.split('\n').flatMap(line => {
  try {
    const r = JSON.parse(line)
    return r && typeof r === 'object' && !('task' in r) && typeof r.run === 'string' && typeof r.ts === 'number'
      ? [r as NoticeRow] : []
  } catch { return [] }
})
export const RUN_NAME = /^[a-z0-9][a-z0-9-]{0,23}$/ // runs.RUN_RE
// a run notice's toast, by its kind (the mod's RUN_HEADLINE)
export const RUN_HEADLINE: Record<string, string> = { run_checkpoint: 'is at a checkpoint', run_done: 'is done',
  run_paused: 'is paused', run_stopped: 'was stopped', run_failed: 'has failed' }

export class NoticeLane {
  sessions: NoticeSessions | undefined
  emit: (t: Toast) => void = () => {}
  readonly startedAt: number
  readonly busy = new Map<string, boolean>()    // a session's execution under way
  readonly toasted = new Set<string>()          // `${task}:${ts}`: toasted once
  readonly heldAsks = new Set<string>()         // a question a busy turn refused (#542)
  readonly dropped = new Map<string, number>()  // task → when its wake failed (ms): a minute before the net
  readonly rewoken = new Set<string>()          // `${task}:${updatedAt}`: one re-wake each
  readonly noticed = new Map<string, number>()  // task → its newest notice's ts this load
  readonly roots = new Map<string, string>()
  readonly delegated = new Set<string>()        // tasks this server owned in this load
  readonly jobs: Jobs
  lastSize = -1
  earsOk = false
  sawTurn = false     // a turn ended here in this load
  turnEndedAt = -Infinity
  typed = ''          // the root session the user last typed in here
  scanning = false
  again = false

  constructor(readonly deps: LaneDeps) {
    this.startedAt = deps.now()
    this.jobs = new Jobs(this)
  }

  path(...parts: string[]) { return join(this.deps.home, ...parts) }
  num(path: string): number { return Number((this.deps.read(path) ?? '').trim()) || 0 }

  // The root of ``sid`` (a subagent that ran wb start reports to its driver), '' when unknown
  async root(sid: string): Promise<string> {
    if (!sid || !this.sessions) return ''
    const known = this.roots.get(sid)
    if (known !== undefined) return known
    let cur = sid
    for (let i = 0; i < 16; i++) {
      const info = await this.sessions.get({ sessionID: cur }).catch(() => undefined)
      if (!info) return '' // not in this server's store: another server's, or deleted (not cached)
      if (!info.parentID) break
      cur = info.parentID
    }
    if (this.roots.size > 500) this.roots.clear()
    this.roots.set(sid, cur)
    return cur
  }

  // The session here a task is delivered to, '' for none: its recorded `session` (OpenCode's,
  // `driver.tool`), when this server's store holds it, judged by the kernel's owns (by session:
  // adoption by directory is wb adopt's)
  async ownerOf(spec: TaskFile): Promise<string> {
    const sid = typeof spec.session === 'string' ? spec.session : ''
    const tool = spec.driver && typeof spec.driver === 'object' ? String(spec.driver.tool ?? '') : ''
    if (!sid || tool !== 'opencode') return ''
    const root = await this.root(sid)
    const mine = owns({ session: sid, ids: [root ? sid : '-'], driver: tool, host: 'opencode', gone: false,
      cwd: '', repo: '', subdir: '', here: '', home: '', fold: false }).mine
    return mine && root ? root : ''
  }

  // `wb ack <task> <ts>`: true taken, false another took it (exit 3), null wb couldn't say
  async take(name: string, ts: number, run = false): Promise<boolean | null> {
    const r = await this.deps.run(['ack', name, String(ts), ...(run ? ['--run'] : [])])
    return r.code === 0 ? true : r.code === 3 ? false : null
  }
  async release(name: string, prev: number, ts: number, run = false) {
    await this.deps.run(['ack', name, String(prev), '--if', String(ts), ...(run ? ['--run'] : [])])
  }

  // into the session as OpenCode's own synthetic message: whether it was admitted
  async send(sid: string, id: string, text: string, delivery: 'steer' | 'queue', meta: Row, resume = true): Promise<boolean> {
    if (!this.sessions) return false
    let said = text
    if (resume && (await this.deps.run(['chat', 'away'], sid)).out.trim() === 'away') said = `${text}\n\n${AWAY_TAIL}`
    try {
      await this.sessions.synthetic({ sessionID: sid, id, text: said, description: `Workbench: ${String(meta.title ?? '')}`.trim(),
        metadata: { source: SOURCE, ...meta }, delivery, ...(resume ? {} : { resume: false }) })
      return true
    } catch (e) {
      // that id in the session already (a resend: the inbox gives back the first) or elsewhere
      const x = e as Row | undefined
      return x?.status === 409 || /conflict|409/i.test(`${String(x?._tag ?? x?.name ?? '')} ${String(x?.message ?? '')}`)
    }
  }

  toast(key: string, message: string, sessionID: string, notify: boolean, variant: Toast['variant'] = 'info') {
    try { this.emit({ key, title: 'Workbench', message, variant, sessionID, notify }) } catch { /* no TUI */ }
  }

  // whether routes.json has a day cap, for a stop at it (cause `spend`): read only then (the mod's readRoutes)
  capped(row: NoticeRow | null): boolean {
    if (row?.cause !== 'spend') return false
    return typeof json(this.deps.read(this.deps.routes)).day_usd === 'number'
  }

  // A look at everything, never two at once (one asked meanwhile runs after)
  async scan() {
    if (this.scanning) { this.again = true; return }
    this.scanning = true
    this.again = false
    try {
      const now = this.deps.now()
      await this.deliver(now)
      const list = this.tasks(now)
      await this.rewake(now, list)
      await this.jobs.tick(now, list)
    } finally {
      this.scanning = false
    }
    if (this.again) await this.scan()
  }

  // The open tasks, from their status.json, with the session here each is delivered to
  tasks(now: number): { name: string; phase: string; parked: boolean; updatedAt: number; spec: TaskFile }[] {
    const out: { name: string; phase: string; parked: boolean; updatedAt: number; spec: TaskFile }[] = []
    let names: string[] = []
    try { names = this.deps.list(this.path('tasks')) } catch { return out }
    for (const name of names) {
      if (!/^[a-z0-9][a-z0-9-]{0,47}$/.test(name)) continue
      const st = json<StatusFile>(this.deps.read(this.path('tasks', name, 'status.json')))
      const phase = String(st.phase ?? '')
      if (!ACTIVE.has(phase) && !NEEDS.has(phase)) continue
      out.push({ name, phase, parked: !!st.parked, updatedAt: (Number(st.updated_at) || 0) * 1000,
        spec: json<TaskFile>(this.deps.read(this.path('tasks', name, 'task.json'))) })
    }
    void now
    return out
  }

  // Each notice due a look (the kernel's noticesDue), judged by noticePlan and carried out: the
  // mod's deliver, OpenCode-native. A notice held for a busy turn leaves the file unseen, so the
  // look after the turn (its end, the idle hook, the timer) sends it.
  async deliver(now: number) {
    const file = this.path('notify.jsonl')
    const size = this.deps.size(file)
    const anyBusy = [...this.busy.values()].some(Boolean)
    if (size === this.lastSize && !anyBusy && !this.heldAsks.size) return
    const text = this.deps.read(file, NOTIFY_TAIL)
    if (text === undefined) return
    const rows = parseLines(text)
    const { due, noticed } = noticesDue(rows, this.startedAt, now)
    for (const n of noticed) this.noticed.set(n.task, Math.max(this.noticed.get(n.task) ?? 0, n.ts))
    let held = false
    for (const d of due) {
      const name = d.task
      if (!/^[a-z0-9][a-z0-9-]{0,47}$/.test(name)) continue
      const picks = d.picks.map(i => rows[i]!).filter(Boolean)
      let acked = this.num(this.path('tasks', name, 'acked'))
      if (picks.every(r => r.ts <= acked)) continue
      const spec = json<TaskFile>(this.deps.read(this.path('tasks', name, 'task.json')))
      const sid = await this.ownerOf(spec)
      if (!sid) continue // another driver's, another server's or no session's: theirs to deliver (or wb wait's)
      this.delegated.add(name)
      const status = json<StatusFile>(this.deps.read(this.path('tasks', name, 'status.json')))
      for (const row of picks) {
        const key = `${name}:${row.ts}`
        const kind = String(row.kind ?? '')
        const id = row.id ? String(row.id) : ''
        const asks = row.ts > acked && kind === 'question' && !!id
        const plan = noticePlan({
          kind, hasId: !!id, phase: String(status.phase ?? ''), questionOpen: asks && this.asking(name, id),
          ts: Number(row.ts), acked, own: true, wake: this.deps.wake, busy: !!this.busy.get(sid),
          toasted: this.toasted.has(key), heldAsk: this.heldAsks.has(key),
        })
        if (plan.act === 'skip') continue
        if (plan.toast) {
          this.toasted.add(key)
          this.toast(key, `${name} ${headlineOf(row)}`, sid, WAKE_KINDS.has(kind), ALERT[kind] ?? 'info')
        }
        if (plan.act === 'none') continue // the toast only: wakes are off
        const meta = { task: name, kind, ts: row.ts, title: `${name} ${headlineOf(row)}` }
        if (plan.act === 'hold') { held = true; continue }
        if (plan.act === 'steer') {
          const took = await this.take(name, row.ts)
          if (took === null) { held = true; continue } // wb couldn't run: tried again at the next look
          if (!took) { acked = row.ts; continue }      // another server's or wb wait's
          if (await this.send(sid, noticeId(name, kind, row.ts), `${wakeText(row, name, this.capped(row))}\n\n${MIDTURN_NOTE}`, 'steer', meta)) {
            acked = row.ts
            this.heldAsks.delete(key)
            continue
          }
          await this.release(name, acked, row.ts)
          this.heldAsks.add(key)
          held = true
          continue
        }
        const prev = acked
        const took = await this.take(name, row.ts)
        if (took === null) { held = true; continue }
        if (!took) { acked = row.ts; continue }
        acked = row.ts
        if (plan.act !== 'wake') {
          // no longer true: acknowledged; a question held through a busy turn that timed out is said (#542)
          this.heldAsks.delete(key)
          if (plan.act === 'missed' && (this.deps.read(this.path('tasks', name, 'ask', `${id}.answer`)) ?? '').startsWith(NO_ANSWER)) {
            await this.send(sid, noticeId(name, 'missed', row.ts), `${WAKE_OPENING} \`${name}\` asked a question while ` +
              `you were busy.\n\n${String(row.text ?? '')}\n\n${MISSED_NOTE(name)}`, 'queue', { ...meta, kind: 'missed' })
          }
          continue
        }
        this.heldAsks.delete(key)
        if (!(await this.send(sid, noticeId(name, kind, row.ts), wakeText(row, name, this.capped(row)), 'queue', meta))) {
          this.dropped.set(name, this.deps.now())
          await this.release(name, prev, row.ts)
        }
      }
    }
    if (await this.deliverRuns(now, text)) held = true
    if (!held) this.lastSize = size
  }

  // A run's own notices (#1278's S9: run_checkpoint, run_done, run_paused, run_stopped, run_failed):
  // the one wake a run gives, to the session here that ran `wb run start` (run.json `session`,
  // `session_kind` opencode), once (`wb ack --run`, which `wb wait <run>` shares), while the run is
  // still in the phase it was raised in (runNoticePlan). Its text is the wake whole; its items'
  // notices are its driver's (runs/<run>/notify.jsonl), never here. True when one is held.
  async deliverRuns(now: number, text: string): Promise<boolean> {
    const rows = parseRunLines(text)
    if (!rows.length) return false
    let held = false
    for (const d of runNoticesDue(rows, this.startedAt, now)) {
      const name = d.task
      if (!RUN_NAME.test(name)) continue
      let acked = this.num(this.path('runs', name, 'acked'))
      const picks = d.picks.map(i => rows[i]!).filter(Boolean)
      if (picks.every(r => r.ts <= acked)) continue
      const spec = json<Row>(this.deps.read(this.path('runs', name, 'run.json')))
      const sid = await this.ownerOf({ session: spec.session, driver: { tool: spec.session_kind === 'opencode' ? 'opencode' : '' } } as TaskFile)
      if (!sid) continue // another driver's, another server's or no session's: theirs (or wb wait's)
      const status = json<Row>(this.deps.read(this.path('runs', name, 'status.json')))
      for (const row of picks) {
        const key = `run:${name}:${row.ts}`
        const kind = String(row.kind ?? '')
        const plan = runNoticePlan({ phase: String(status.phase ?? ''), raisedIn: String(row.phase ?? ''), ts: Number(row.ts),
          acked, own: true, wake: this.deps.wake, busy: !!this.busy.get(sid), toasted: this.toasted.has(key) })
        if (plan.act === 'skip') continue
        const title = `run ${name} ${RUN_HEADLINE[kind] ?? kind}`
        if (plan.toast) {
          this.toasted.add(key)
          this.toast(key, title, sid, true, kind === 'run_done' ? 'info' : 'warning')
        }
        if (plan.act === 'none') continue
        if (plan.act !== 'wake' && plan.act !== 'ack') { held = true; continue } // mid-turn: sent at its end
        const took = await this.take(name, row.ts, true)
        if (took === null) { held = true; continue }
        const prev = acked
        acked = row.ts
        if (!took || plan.act !== 'wake') continue
        if (!(await this.send(sid, noticeId(`run:${name}`, kind, row.ts), String(row.text ?? ''), 'queue',
          { run: name, kind, ts: row.ts, title }))) {
          await this.release(name, prev, row.ts, true)
          held = true // tried again at the next look
        }
      }
    }
    return held
  }

  asking(name: string, id: string): boolean {
    const dir = this.path('tasks', name, 'ask')
    return this.deps.size(join(dir, `${id}.json`)) >= 0 && this.deps.size(join(dir, `${id}.answer`)) < 0
  }

  // The safety net under deliver (the kernel's rewakePlan): a task of a session here waiting on
  // its driver whose news never reached it wakes the session once, after a minute of it idle.
  // Once per notice (`rewoken`, and `wb ack` to now): the idle hook and the timer both look.
  async rewake(now: number, list: ReturnType<NoticeLane['tasks']>) {
    for (const t of list) {
      if (!NEEDS.has(t.phase)) continue
      const sid = await this.ownerOf(t.spec)
      if (!sid) continue
      this.delegated.add(t.name)
      const facts = { phase: t.phase, parked: t.parked, mine: true, updatedAt: t.updatedAt, now,
        busy: !!this.busy.get(sid), wake: this.deps.wake, dropped: this.dropped.get(t.name) ?? 0 }
      if (!rewakePlan({ ...facts, acked: 0, seen: -1, marked: 0 }).candidate) continue
      const acked = this.num(this.path('tasks', t.name, 'acked'))
      const marked = this.num(this.path('tasks', t.name, 'noticed'))
      const plan = rewakePlan({ ...facts, acked, seen: this.noticed.get(t.name) ?? -1, marked })
      const key = `${t.name}:${t.updatedAt}`
      if (!plan.due || this.rewoken.has(key)) continue
      const ts = now / 1000
      if ((await this.take(t.name, ts)) !== true) continue
      this.rewoken.add(key)
      // its newest notice says why it stopped, as deliver's wake would have (#1384)
      const text = this.deps.read(this.path('notify.jsonl'), NOTIFY_TAIL) ?? ''
      const found = parseLines(text).filter(r => r.task === t.name || batchOf(r)?.includes(t.name)).at(-1)
      const row = found?.kind === plan.kind ? (batchOf(found) ? { ...found, tasks: [t.name] } : found) : null
      const said = `${WAKE_OPENING} \`${t.name}\` ${row ? headlineOf(row) : HEADLINE[plan.kind] ?? plan.kind}, ` +
        `${ago(now - t.updatedAt)} ago, and no notice reached you. \`wb report ${t.name}\` shows where it is.\n\n` +
        (row ? nextOf(row, t.name, this.capped(row)) : (NEXT[plan.kind] ?? NEXT.failed!)(t.name))
      if (!(await this.send(sid, noticeId(t.name, 'rewake', t.updatedAt / 1000), said, 'queue',
        { task: t.name, kind: 'rewake', ts: t.updatedAt / 1000, title: `${t.name} ${HEADLINE[plan.kind] ?? plan.kind}` }))) {
        this.dropped.set(t.name, this.deps.now())
        this.rewoken.delete(key)
        await this.release(t.name, acked, ts)
      }
    }
  }

  // OpenCode's events: a session's execution started, or ended (then a look: a held notice goes)
  started(sid: string) { if (sid) this.busy.set(sid, true) }
  async ended(sid: string) {
    if (!sid) return
    this.busy.set(sid, false)
    this.sawTurn = true
    this.turnEndedAt = this.deps.now()
    await this.scan()
  }
  // `experimental.idle`: the session is about to go idle; what a look admits then runs in the
  // same execution (plugin/src/promise/session.ts `SessionIdle`)
  idle(sid: string) {
    if (!sid) return
    this.busy.set(sid, false)
    this.sawTurn = true
    this.turnEndedAt = this.deps.now()
    void this.scan().catch(() => {})
  }
  // a prompt the user typed (unmarked: automations and our own deliveries carry metadata)
  async prompted(e: { sessionID: string; metadata?: Row }) {
    if (e.metadata && Object.keys(e.metadata).length) return
    const root = await this.root(e.sessionID)
    if (root && root === e.sessionID) this.typed = root
  }
}

// ---- wiring

// The RPC whose `toast` event the TUI part shows (notices-tui.ts): data only
export const WorkbenchNotices = {
  id: 'workbench-notices',
  methods: {},
  events: {
    toast: {
      schema: {
        type: 'object',
        properties: {
          key: { type: 'string' }, title: { type: 'string' }, message: { type: 'string' },
          variant: { type: 'string', enum: ['info', 'success', 'warning', 'error'] },
          sessionID: { type: 'string' }, notify: { type: 'boolean' },
        },
        required: ['key', 'title', 'message', 'variant', 'sessionID', 'notify'],
        additionalProperties: false,
      },
    },
  },
} as const

// wb, as the plugin runs it: the options' wb and WB_* paths, never as Claude Code or an agent,
// as the OpenCode session ``session`` when given (chat.ts's runner, with the exit status)
export function wbRun(options: Options, base: Record<string, string | undefined> = process.env): Run {
  return (args, session, timeoutMs = RUN_MS) => new Promise(done => {
    const env: Record<string, string | undefined> = { ...wbEnv(options, base) }
    for (const k of ['CLAUDECODE', 'CLAUDE_PID', 'CLAUDE_CODE_SESSION_ID', 'WB_TASK', 'OPENCODE_SESSION_ID']) delete env[k]
    if (session) env.OPENCODE_SESSION_ID = session
    const found = findWb(options, env)
    if (!found.found) return done({ code: 127, out: `wb not found (${found.wb})` })
    let out = ''
    const child = spawn(found.wb, [...args], { env, stdio: ['pipe', 'pipe', 'pipe'] })
    const timer = setTimeout(() => child.kill('SIGKILL'), timeoutMs)
    child.stdout.on('data', b => { out += String(b) })
    child.on('error', e => { clearTimeout(timer); done({ code: 127, out: String(e) }) })
    child.on('close', code => { clearTimeout(timer); done({ code: code ?? 1, out: out.trim() }) })
    child.stdin.end('')
  })
}

// routes.json as routes.path reads it: WB_ROUTES (a `~` or relative one expanded as store.home
// does: store.PATH_VARS), else ~/.config/workbench/routes.json
export function routesPath(env: Record<string, string | undefined>, cwd: string = process.cwd()): string {
  const home = env.HOME ?? '~'
  const v = env.WB_ROUTES
  if (!v) return join(home, '.config', 'workbench', 'routes.json')
  return resolve(cwd, v === '~' ? home : v.startsWith('~/') ? join(home, v.slice(2)) : v)
}

// A file's text, or its last ``tail`` bytes from a whole line on; undefined when it can't be read
export function readText(path: string, tail = 0): string | undefined {
  try {
    const size = statSync(path, { throwIfNoEntry: false })?.size
    if (size === undefined) return undefined
    if (!tail || size <= tail) return readFileSync(path, 'utf8')
    const fd = openSync(path, 'r')
    try {
      const buf = new Uint8Array(tail)
      const n = readSync(fd, buf, 0, tail, size - tail)
      const text = new TextDecoder().decode(buf.subarray(0, n))
      return text.slice(text.indexOf('\n') + 1)
    } finally { closeSync(fd) }
  } catch { return undefined }
}

// `wb events`, the ears: its hello says wb runs, every line a look; respawned after it ends
export function ears(options: Options, lane: NoticeLane, stopped: () => boolean): () => void {
  let child: ReturnType<typeof spawn> | undefined
  let wait = EARS_RETRY_MS
  const start = () => {
    if (stopped()) return
    const env: Record<string, string | undefined> = { ...wbEnv(options) }
    for (const k of ['CLAUDECODE', 'CLAUDE_PID', 'CLAUDE_CODE_SESSION_ID', 'WB_TASK', 'OPENCODE_SESSION_ID']) delete env[k]
    const found = findWb(options, env)
    if (!found.found) { setTimeout(start, 60_000); return }
    let buf = ''
    try {
      child = spawn(found.wb, ['events'], { env, stdio: ['pipe', 'pipe', 'pipe'] })
    } catch { setTimeout(start, wait = Math.min(wait * 2, 60_000)); return }
    child.stdout.on('data', b => {
      buf += String(b)
      let at: number
      while ((at = buf.indexOf('\n')) >= 0) {
        const line = buf.slice(0, at)
        buf = buf.slice(at + 1)
        const r = json(line)
        if ('hello' in r) { lane.earsOk = true; wait = EARS_RETRY_MS }
        void lane.scan().catch(() => {})
      }
    })
    child.on('error', () => {})
    child.on('close', () => {
      child = undefined
      if (!stopped()) setTimeout(start, lane.earsOk ? EARS_RETRY_MS : (wait = Math.min(wait * 2, 60_000)))
    })
  }
  start()
  return () => { child?.kill('SIGTERM') }
}

// The part of OpenCode's server plugin context the notices use (plugin/src/promise/plugin.ts)
export interface NoticeContext {
  readonly options: Options
  readonly rpc: { readonly register: <D extends RpcDefinition>(d: D, h: RpcHandlers<D>) => Promise<Registration & {
    readonly events?: { readonly emit: (name: string, data: object) => Promise<void> } }> }
  readonly session: NoticeSessions & {
    hook(name: 'experimental.idle' | 'prompt', cb: (e: { sessionID: string; metadata?: Row }) => unknown): Promise<Registration>
  }
  readonly event: { subscribe(options?: { types?: string[]; signal?: AbortSignal }): AsyncIterable<{ id?: string; type: string; data?: Row }> }
}

export const EVENTS = ['session.execution.started', 'session.execution.succeeded', 'session.execution.failed',
  'session.execution.interrupted']

type Shared = { lane: NoticeLane; users: number; apis: NoticeSessions[]; emitters: ((t: Toast) => void)[];
  seen: string[]; timer?: unknown; stopEars?: () => void; stopped: boolean }
const KEY = '__workbenchNotices'

export function lanes(make: () => NoticeLane): Shared {
  const g = globalThis as Record<string, unknown>
  let s = g[KEY] as Shared | undefined
  if (!s) {
    const made: Shared = { lane: make(), users: 0, apis: [], emitters: [], seen: [], stopped: false }
    made.lane.emit = t => { for (const e of made.emitters) e(t) } // the TUI dedupes by key
    g[KEY] = s = made
  }
  s.users++
  return s
}

// The server part's setup of the notices: the RPC, the hooks and the event feed per instance, the
// ears and the timer once per process. Nothing without the context's session and event domains
// (a server too old for them, a test's ctx).
export async function notices(ctx: ServerContext): Promise<(() => Promise<void>) | undefined> {
  const c = ctx as unknown as Partial<NoticeContext>
  if (!c.session?.synthetic || !c.event?.subscribe) return undefined
  const options = ctx.options
  const env = wbEnv(options)
  const s = lanes(() => new NoticeLane({
    run: wbRun(options), home: wbHome(env), pid: process.pid, now: () => Date.now(),
    routes: routesPath(env),
    read: readText,
    size: path => statSync(path, { throwIfNoEntry: false })?.size ?? -1,
    list: dir => readdirSync(dir),
    wake: options?.wake !== 'off',
    alive: pid => { try { return (process as unknown as { kill(p: number, s: number): boolean }).kill(pid, 0) } catch { return false } },
  }))
  const l = s.lane
  const sessions = c.session
  s.apis.push(sessions)
  l.sessions = s.apis[0]
  const regs: Registration[] = []
  let emitter: ((t: Toast) => void) | undefined
  try {
    const reg = await c.rpc!.register(WorkbenchNotices as unknown as RpcDefinition, {} as never)
    regs.push(reg)
    if (reg.events?.emit) {
      const emit = reg.events.emit
      emitter = t => { void emit('toast', t).catch(() => {}) }
      s.emitters.push(emitter)
    }
  } catch { /* no RPC events here: no toasts, the wakes still go */ }
  // experimental: a server without it leaves the turn's end to the event stream and the timer
  try { regs.push(await sessions.hook('experimental.idle', e => { l.idle(e.sessionID) })) } catch { /* the timer */ }
  try { regs.push(await sessions.hook('prompt', e => { void l.prompted(e).catch(() => {}) })) } catch { /* no target for offers but the listener */ }
  const abort = new AbortController()
  void (async () => {
    while (!abort.signal.aborted) {
      try {
        for await (const ev of c.event!.subscribe({ types: EVENTS, signal: abort.signal })) {
          if (ev.id) {
            if (s.seen.includes(ev.id)) continue
            s.seen.push(ev.id)
            if (s.seen.length > 500) s.seen.splice(0, 250)
          }
          const sid = String(ev.data?.sessionID ?? '')
          if (ev.type === 'session.execution.started') l.started(sid)
          else void l.ended(sid).catch(() => {})
        }
      } catch { /* the stream broke: again in a moment */ }
      if (!abort.signal.aborted) await new Promise(r => setTimeout(() => r(undefined), EARS_RETRY_MS))
    }
  })()
  if (!s.timer) {
    s.timer = setInterval(() => { void l.scan().catch(() => {}) }, SCAN_MS)
    s.stopEars = ears(options, l, () => s.stopped)
  }
  return async () => {
    abort.abort()
    for (const r of regs) await r.dispose().catch(() => {})
    s.apis.splice(s.apis.indexOf(sessions), 1)
    if (emitter) s.emitters.splice(s.emitters.indexOf(emitter), 1)
    l.sessions = s.apis[0]
    if (--s.users > 0) return
    s.stopped = true
    if (s.timer) clearInterval(s.timer)
    s.stopEars?.()
    delete (globalThis as Record<string, unknown>)[KEY]
  }
}
