// Workbench's panel, sidebar and footer, the server's half (#1312, #1332): the data they draw,
// over the `workbench-pane` RPC (the host-shape decision, #1296: data over RPC, wb the system of
// record), and `/workbench`, a server command, so it works in the TUI, Desktop and the web app
// alike. The TUI part draws (pane.tsx); nothing here knows OpenTUI.
//
// `tasks`: every task directory under $WB_HOME/tasks as the Claude Code mod's scan reads it
// (task.json and status.json, the closed ones for half an hour, a closed one never read again),
// each task's view facts (tasks.ts `viewFacts`, shared with the mod) and its owner keys, which the
// TUI judges through the kernel (core.ts `owns`) against the session it shows. With an API task
// working, the day cap's numbers (`wb route --json`, kept ROLES_MS). `live`: the followed task's
// transcript (`wb watch <task> --json --last N`), asked only while the panel is open (#1332), a
// task still running read at most every LIVE_MS. `diff`: `wb diff <task>`, wb's pinned git
// (never a `location` or `/diff` on the agent's worktree, whose `.git` the agent can rewrite:
// the decision's critic 1). `link`: the web app's (`wb chat url --json`), for the modeline.
//
// `/workbench [request]`: the Workbench agent (`session.switchAgent`, the mode, #1315's agent),
// the standing driver pin applied (driver.ts `applyPin`, #1316), and the request, when one was
// typed after it, sent on to that agent. The TUI opens the panel when the session's agent
// becomes Workbench, whichever way it did (pane.tsx). Nothing runs at import time.
import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import type { Registration, RpcDefinition, RpcHandlers } from './host.d.ts'
import { applyPin, wbRunner, type DriverHost, type Run } from './driver.ts'
import { wbEnv, wbHome, type Options } from './wb.ts'
import { apiWorking, isEst, UNWRITTEN, viewFacts, type TaskFacts } from '../mod/hooks/tasks.ts'
import { LINK_STATES, linkDown, type Link } from '../mod/hooks/modeline.ts'
import { ACTIVE, CLOSED, NEEDS, PARKS } from '../mod/hooks/core.ts'

export const AGENT = 'workbench'        // the Workbench agent's id (agent.ts, #1335)
export const COMMAND = 'workbench'
export const KEEP_CLOSED_MS = 30 * 60 * 1000 // closed tasks stay listed half an hour, as the mod's
export const LIVE_MS = 5000             // a running task's transcript, read again no sooner (the mod's LIVE_EVERY_MS, #691)
export const LIVE_LINES = 600           // the newest transcript lines the panel keeps
export const ROLES_MS = 10_000          // `wb route --json`, kept (the mod's ROLES_CACHE_MS)
export const LINK_MS = 60_000           // `wb chat url --json`, kept (the chat's CHECK_MS)
export const DIFF_CHARS = 200_000       // the most of a diff the panel takes

type Row = Record<string, any>

/** One task as the panel reads it: the Tasks view's facts, and its owner keys for the kernel. */
export type PaneTask = TaskFacts & {
  readonly session: string; readonly driver: string; readonly cwd: string; readonly repo: string; readonly subdir: string
}
export interface Cap { readonly spent: number; readonly cap: number | null; readonly est: boolean }
export interface Tasks { readonly now: number; readonly home: string; readonly tasks: PaneTask[]; readonly cap: Cap | null }
export interface Line { readonly s: string; readonly t: string }
export interface Live { readonly task: string; readonly lines: Line[]; readonly why?: string }
export type { Link }

const OBJ = { type: 'object' } as const
const TASK_IN = {
  type: 'object', properties: { task: { type: 'string' } }, required: ['task'], additionalProperties: false,
} as const
// The panel's RPC: data only, importing it does nothing
export const WorkbenchPane = {
  id: 'workbench-pane',
  methods: {
    tasks: { input: { type: 'object', properties: {}, additionalProperties: false }, output: OBJ },
    live: { input: TASK_IN, output: OBJ },
    diff: { input: TASK_IN, output: OBJ },
    link: { input: { type: 'object', properties: {}, additionalProperties: false }, output: OBJ },
  },
  events: {},
} as const

// The files the scan reads, so tests give it a fake directory
export interface Files {
  dirs(path: string): string[]
  read(path: string): string | undefined
}
export const realFiles: Files = {
  dirs: path => {
    try {
      return readdirSync(path, { withFileTypes: true }).filter(e => e.isDirectory()).map(e => e.name)
    } catch {
      return []
    }
  },
  read: path => {
    try {
      return readFileSync(path, 'utf8')
    } catch {
      return undefined
    }
  },
}

const json = (text: string | undefined): Row => {
  try {
    const v = JSON.parse(text ?? '')
    return v && typeof v === 'object' && !Array.isArray(v) ? v : {}
  } catch {
    return {}
  }
}

// task.json `driver` as store.driver_of names it: `opencode`, else `claude` with a session, else ''
const driverOf = (sp: Row): string =>
  typeof sp.driver === 'string' ? sp.driver : sp.driver?.tool === 'opencode' ? 'opencode' : sp.session ? 'claude' : ''

/** Every listed task, read from its task.json and status.json (the mod's `scan`, less what only
 * Claude Code's process does: its pid checks and adoption). `gone`: tasks closed for longer than
 * KEEP_CLOSED_MS, never read again (a closed phase is final: the 2026-11-16 audit). */
export function scanTasks(home: string, now: number, gone: Set<string>, files: Files = realFiles): PaneTask[] {
  const dir = join(home, 'tasks')
  const names = new Set(files.dirs(dir))
  for (const name of gone) if (!names.has(name)) gone.delete(name) // removed by hand
  const list: PaneTask[] = []
  for (const name of names) {
    if (gone.has(name)) continue
    const st = json(files.read(join(dir, name, 'status.json')))
    const phase = String(st.phase ?? UNWRITTEN)
    const updatedAt = Number(st.updated_at ?? 0) * 1000
    if (!ACTIVE.has(phase) && !NEEDS.has(phase) && now - updatedAt > KEEP_CLOSED_MS) {
      if (CLOSED.has(phase)) gone.add(name)
      continue
    }
    const sp = json(files.read(join(dir, name, 'task.json')))
    if (!sp.name || sp.bench) continue // a replay of wb bench's: no one's task
    list.push({
      name: String(sp.name), phase, round: Number(st.round ?? 1), model: String(sp.model ?? ''),
      label: String(sp.model_label ?? ''), via: sp.via === 'api' ? 'api' : 'gpu', role: String(sp.role ?? ''),
      provider: String(sp.provider_name ?? sp.provider ?? ''),
      tests: st.tests ? (st.tests.ok ? 'pass' : 'fail') : null,
      last: String(st.last ?? ''), createdAt: Number(sp.created_at ?? 0) * 1000, updatedAt, recent: [],
      parked: PARKS.has(phase) && !!st.parked, // as store.parked (#698)
      session: String(sp.session ?? ''), driver: driverOf(sp), cwd: String(sp.cwd ?? ''),
      repo: String(sp.repo ?? ''), subdir: String(sp.subdir ?? ''),
      ...viewFacts(sp, st),
    })
  }
  return list.sort((a, b) => a.createdAt - b.createdAt)
}

/** wb's JSON lines (`wb watch --json`): one {s, t} a line, anything else skipped. */
export function parseLines(text: string, last = LIVE_LINES): Line[] {
  const out: Line[] = []
  for (const line of text.split('\n')) {
    if (!line.trim()) continue
    try {
      const r = JSON.parse(line)
      if (r && typeof r === 'object') out.push({ s: String(r.s ?? ''), t: String(r.t ?? '') })
    } catch { /* not a line of wb's */ }
  }
  return out.slice(-last)
}

/** `wb diff`'s text as the panel's lines, each styled as `wb watch` styles a diff (add, del, head). */
export function diffLines(text: string): Line[] {
  const cut = text.length > DIFF_CHARS
  const lines = (cut ? text.slice(0, DIFF_CHARS) : text).replace(/\n$/, '').split('\n').map(t => ({
    s: /^(\+\+\+|---|diff |index |@@)/.test(t) ? 'head' : t.startsWith('+') ? 'add' : t.startsWith('-') ? 'del' : 'out', t,
  }))
  return cut ? [...lines, { s: 'bad', t: `… cut at ${DIFF_CHARS} characters: wb diff has it all` }] : lines
}

/** The day cap's numbers from `wb route --json` (routes.json's `day_usd`, the ledger's `spend_today`) */
export function capOf(routeJson: string): Cap | null {
  const r = json(routeJson)
  if (!r.spend_today && r.day_usd === undefined) return null
  return { spent: Number(r.spend_today?.usd ?? 0) || 0, cap: typeof r.day_usd === 'number' ? r.day_usd : null,
    est: isEst(r.spend_today?.prices) }
}

export function linkOf(ran: { code: number; stdout: string; stderr: string }): Link {
  const r = json(ran.stdout)
  const state = String(r.state ?? '')
  if (ran.code === 0 && LINK_STATES.has(state)) {
    return { state: state as Link['state'], ...(r.url ? { url: String(r.url) } : {}), ...(r.why ? { why: String(r.why) } : {}) }
  }
  return linkDown((ran.stderr || ran.stdout).trim().split('\n').at(-1) || 'wb chat url failed')
}

const taskOf = (input: unknown): string => {
  const t = (input as { task?: unknown } | undefined)?.task
  return typeof t === 'string' ? t : ''
}
// a task's name as wb names one (store.NAME_RE): never an option or a path
const NAME = /^[a-z0-9][a-z0-9-]{0,47}$/

/** The panel's data, kept between calls: what each RPC answers. */
export class PaneData {
  readonly gone = new Set<string>()
  roles: { at: number; cap: Cap | null } | null = null
  link: { at: number; link: Link } | null = null
  live = new Map<string, { key: string; at: number; live: Live }>()

  constructor(readonly home: string, readonly run: Run, readonly now: () => number = Date.now,
    readonly files: Files = realFiles) {}

  async tasks(): Promise<Tasks> {
    const now = this.now()
    const tasks = scanTasks(this.home, now, this.gone, this.files)
    let cap: Cap | null = null
    if (apiWorking(tasks)) {
      if (!this.roles || now - this.roles.at >= ROLES_MS) {
        const r = await this.run(['route', '--json'])
        this.roles = { at: now, cap: r.code === 0 ? capOf(r.stdout) : this.roles?.cap ?? null }
      }
      cap = this.roles.cap
    }
    return { now, home: this.home, tasks, cap }
  }

  // The transcript of `task`: a stopped one read again when its status moved; a running one every
  // LIVE_MS, moved or not, as its session moves between status writes (a long step, the model
  // streaming, the tests) and no sooner, as each read is an OpenCode export (the mod's rule, #691)
  async transcript(task: string): Promise<Live> {
    if (!NAME.test(task)) return { task, lines: [], why: `${task} isn't a task's name` }
    const now = this.now()
    const st = json(this.files.read(join(this.home, 'tasks', task, 'status.json')))
    const key = `${st.updated_at ?? ''}`
    const had = this.live.get(task)
    if (had && (ACTIVE.has(String(st.phase)) ? now - had.at < LIVE_MS : had.key === key)) return had.live
    const r = await this.run(['watch', task, '--json', '--last', String(LIVE_LINES)])
    const live: Live = r.code === 0 ? { task, lines: parseLines(r.stdout) }
      : { task, lines: had?.live.lines ?? [], why: (r.stderr || r.stdout).trim().split('\n').at(-1) || `wb watch ${task} failed` }
    this.live.set(task, { key, at: now, live })
    if (this.live.size > 20) this.live.delete(this.live.keys().next().value!)
    return live
  }

  async diff(task: string): Promise<Live> {
    if (!NAME.test(task)) return { task, lines: [], why: `${task} isn't a task's name` }
    const r = await this.run(['diff', task])
    return r.code === 0 ? { task, lines: diffLines(r.stdout) }
      : { task, lines: [], why: (r.stderr || r.stdout).trim().split('\n').at(-1) || `wb diff ${task} failed` }
  }

  async chatLink(): Promise<Link> {
    const now = this.now()
    if (this.link && now - this.link.at < LINK_MS) return this.link.link
    const link = linkOf(await this.run(['chat', 'url', '--json']))
    this.link = { at: now, link }
    return link
  }
}

// OpenCode 2.4.1's shapes used here, structurally (host.d.ts's rule): plugin/src/promise/command.ts
// `CommandDomain.transform` and `CommandDefinition`; session.ts `SessionDomain` `switchAgent` and
// `prompt` (client types.ts `SessionSwitchAgentInput`, `SessionPromptInput`)
export interface CommandInvocation {
  readonly sessionID: string
  // what the TUI attached to the request goes with it (OpenCode's own /goal forwards them too)
  readonly prompt: { readonly text?: string; readonly files?: readonly unknown[]; readonly agents?: readonly unknown[]; readonly skills?: readonly unknown[] }
  readonly delivery: 'steer' | 'queue'
}
export interface PaneHost {
  readonly options: Options
  readonly rpc: { readonly register: <D extends RpcDefinition>(d: D, h: RpcHandlers<D>) => Promise<Registration> }
  readonly command?: {
    readonly transform?: (cb: (editor: { add(d: { name: string; description?: string; execute: (i: CommandInvocation) => Promise<void> }): void }) => void) => Promise<Registration>
  }
  readonly session?: DriverHost['session'] & {
    readonly switchAgent?: (input: { readonly sessionID: string; readonly agent: string }) => Promise<unknown>
    readonly prompt?: (input: CommandInvocation['prompt'] & { readonly sessionID: string; readonly text: string; readonly delivery?: 'steer' | 'queue' }) => Promise<unknown>
  }
}

export const DESCRIPTION = 'Workbench mode: hand coding to OpenCode agents through wb (the Workbench agent and its panel)'

/** /workbench, run: the Workbench agent, the pin, then the request typed after it, if any. */
export async function workbench(host: PaneHost, input: CommandInvocation, run: Run): Promise<void> {
  const session = host.session
  if (!session?.switchAgent) throw new Error("this OpenCode can't switch a session's agent")
  try {
    await session.switchAgent({ sessionID: input.sessionID, agent: AGENT })
  } catch (e) {
    // no Workbench agent: the plugin's skill/ wasn't found to make it (agent.ts registerAgent)
    throw new Error(`the Workbench agent isn't there (${String(e)}): wb doctor says why`)
  }
  await applyPin(host as DriverHost, input.sessionID, run, new Map()).catch(() => undefined)
  // the text as typed, untrimmed, so a mention's offsets into it still hold
  const prompt = input.prompt ?? {}, text = prompt.text ?? ''
  const attached = [prompt.files, prompt.agents, prompt.skills].some(x => x !== undefined && x.length > 0)
  if ((text.trim() || attached) && session.prompt)
    await session.prompt({ ...prompt, sessionID: input.sessionID, text, delivery: input.delivery })
}

/** The server part's pane piece: the RPC and /workbench. Returns its cleanup. */
export async function startPane(host: PaneHost, run: Run = wbRunner(host.options)): Promise<() => Promise<void>> {
  const data = new PaneData(wbHome(wbEnv(host.options)), run)
  const regs: Registration[] = []
  regs.push(await host.rpc.register(WorkbenchPane, {
    tasks: async () => data.tasks(),
    live: async (input) => data.transcript(taskOf(input)),
    diff: async (input) => data.diff(taskOf(input)),
    link: async () => data.chatLink(),
  }))
  if (host.command?.transform) {
    try {
      regs.push(await host.command.transform(editor => editor.add({
        name: COMMAND, description: DESCRIPTION, execute: input => workbench(host, input, run),
      })))
    } catch { /* no plugin commands in this OpenCode: the agent picker still reaches Workbench */ }
  }
  return async () => { for (const r of regs) await r.dispose().catch(() => {}) }
}
