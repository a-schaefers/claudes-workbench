// The driver, the model with the final say (#1316, #1317): whatever the session runs on, picked
// in OpenCode's own /models or set by the `workbench` agent's `model`. wb doesn't own the choice;
// this records it. The server part's heartbeat: every BEAT_MS it lists every session in its store
// (`session.list`, unfiltered, every page) and hands them to `wb driver beat`, which writes
// $WB_HOME/drivers/opencode-<machine_tag>-<pid>.json (workbench/driver.py `write_beat`, read by
// store.heartbeats: ARCHITECTURE.md, *The heartbeat*). Each session's row is its own selection,
// overlaid by the model its last primary request went to (the `model.request` hook: a shell has
// no model variable, and the agent's own `model` never shows in the session's selection) and when
// (`used`), which says who owns a task and which models a local driver holds on the GPU.
//
// The pane's driver row and /workbench's pin come through the `workbench-driver` RPC (`driver`:
// `wb driver --session … --json`, `--model` for the TUI's selection, `--once` for the notes said
// once; `pin`: routes.json's standing `driver` applied with `session.switchModel`), so the pane
// and /workbench (#1312) draw from wb's one answer. Nothing runs at import time.
import { spawn } from 'node:child_process'
import { join, resolve } from 'node:path'
import type { Registration, RpcDefinition, RpcHandlers } from './host.d.ts'
import { findWb, wbEnv, type Options } from './wb.ts'

export const BEAT_MS = 30_000       // store.HEARTBEAT_FRESH (120 s) is four beats
export const FIRST_BEAT_MS = 1_000  // the first soon after setup: a session's wb start can record it
export const WB_TIMEOUT_MS = 30_000

// OpenCode 2.4.1's shapes this module uses, structurally (host.d.ts's rule: no @opencode import):
// schema/src/model.ts `Model.Ref`
export interface ModelRef { readonly id: string; readonly providerID: string; readonly variant?: string }
// schema/src/session.ts `Session.Info`, the fields read here
interface SessionInfo {
  readonly id: string
  readonly parentID?: string                      // a subagent's session: its lead's
  readonly model?: ModelRef
  readonly location?: { readonly directory?: string }
  readonly time?: { readonly updated?: number }  // epoch ms
}
// client/src/promise/generated/types.ts `SessionsResponse`: core/src/session.ts `Session.list`
// gives 50 rows a page by default, newest time_updated first, each next page anchored on the
// last row's (time.updated, id)
interface SessionsPage { readonly data?: readonly SessionInfo[]; readonly cursor?: { readonly next?: string | null } }
// plugin/src/promise/session.ts `SessionModelRequest`
interface ModelRequest { readonly sessionID: string; readonly model: ModelRef; readonly kind: string }
// the event plugin/src/promise/event.ts subscribes to: client types.ts `session.model.selected`
interface ModelSelected { readonly type: string; readonly data?: { readonly sessionID?: string; readonly model?: ModelRef } }

// plugin/src/promise/plugin.ts `Context`, the parts used here, each optional: a server older
// than them (or a test's context) leaves that part out, and the rest still works
export interface DriverHost {
  readonly options: Options
  readonly app?: { readonly channel?: string }
  readonly rpc: { readonly register: <D extends RpcDefinition>(d: D, h: RpcHandlers<D>) => Promise<Registration> }
  readonly session?: {
    readonly list?: (input?: { readonly cursor?: string }) => Promise<SessionsPage>
    readonly get?: (input: { readonly sessionID: string }) => Promise<unknown>
    readonly switchModel?: (input: { readonly sessionID: string; readonly model: ModelRef }) => Promise<unknown>
    readonly hook?: (name: 'model.request', callback: (input: ModelRequest) => void) => Promise<Registration>
  }
  readonly event?: {
    readonly subscribe?: (options: { readonly types: readonly string[]; readonly signal?: AbortSignal }) => AsyncIterable<ModelSelected>
  }
}

export interface Row { model?: string; variant?: string; dir?: string; used?: number; parent?: string }
export interface Beat { readonly pid: number; readonly store?: string; readonly ts: number; readonly sessions: Record<string, Row> }
export interface Used { model: string; variant?: string; used: number }

export interface Ran { readonly code: number; readonly stdout: string; readonly stderr: string }
export type Run = (args: readonly string[], input?: string) => Promise<Ran>

// The release channels whose store is opencode.db (cli/src/database-path.ts `databasePath`)
const PLAIN_CHANNELS = ['latest', 'dev', 'beta', 'next', 'prod']

// The server's database, as OpenCode resolves it (cli/src/database-path.ts, util/src/global-roots.ts):
// OPENCODE_DB against the data dir, else opencode.db (opencode-<channel>.db off the release
// channels); undefined for an in-memory one, or with no channel to name it by: a beat without a
// store says sessions live, never gone (store.driver_server)
export function storePath(env: Record<string, string | undefined>, channel: string | undefined): string | undefined {
  const root = env.XDG_DATA_HOME || (env.HOME ? join(env.HOME, '.local', 'share') : '')
  if (!root) return undefined
  const off = env.OPENCODE_DISABLE_CHANNEL_DB === '1' || env.OPENCODE_DISABLE_CHANNEL_DB === 'true'
  const file = env.OPENCODE_DB ?? (channel === undefined ? undefined
    : PLAIN_CHANNELS.includes(channel) || off ? 'opencode.db'
    : `opencode-${channel.replace(/[^a-zA-Z0-9._-]/g, '-')}.db`)
  if (file === undefined || file === ':memory:') return undefined
  return resolve(join(root, 'opencode'), file)
}

export const refText = (m: ModelRef): string => `${m.providerID}/${m.id}`
// A ref's variant, none for `default`: OpenCode's word for the model's own (session.ts
// `switchModel` compares `variant ?? "default"`), which a session's selection carries (seen live,
// 2026-11-26: the beat said `#default`)
export const variantOf = (m: ModelRef): string | undefined => (m.variant && m.variant !== 'default' ? m.variant : undefined)

// `provider/id#variant` as OpenCode parses it (schema/src/model.ts `Model.Ref.parse`)
export function parseRef(text: string): ModelRef | undefined {
  const slash = text.indexOf('/')
  if (slash <= 0) return undefined
  const providerID = text.slice(0, slash)
  const hash = text.indexOf('#', slash + 1)
  const id = text.slice(slash + 1, hash === -1 ? undefined : hash)
  const variant = hash === -1 ? undefined : text.slice(hash + 1)
  if (!id || providerID.includes('#') || (variant !== undefined && (!variant || variant.includes('#')))) return undefined
  return { providerID, id, ...(variant ? { variant } : {}) }
}

// Every session in the store, each page: `whole` false when a page failed (the beat then names
// no store, so its gaps can't read as deleted sessions). The pages are newest-updated first, so a
// session updated while they are read moves ahead of the cursor and was missed, yet the beat named
// the store and store.driver_server read its task as gone (the review of #1316, 2026-11-25): the
// first page is read again at the end, its rows not yet seen added, and the listing is whole only
// when that page reaches back past ``since`` (the listing's start, ms): everything updated since
// is on it.
export async function listAll(list: NonNullable<DriverHost['session']>['list'],
                              since: number = Date.now()): Promise<{ rows: SessionInfo[]; whole: boolean }> {
  if (!list) return { rows: [], whole: false }
  const rows: SessionInfo[] = []
  let cursor: string | undefined
  for (let page = 0; ; page++) {
    if (page >= 1000) return { rows, whole: false }
    let got: SessionsPage
    try {
      got = await list(cursor ? { cursor } : {})
    } catch {
      return { rows, whole: false }
    }
    rows.push(...(got?.data ?? []))
    const next = got?.cursor?.next
    if (!next || next === cursor) break
    cursor = next
  }
  let first: readonly SessionInfo[]
  try {
    first = (await list({}))?.data ?? []
  } catch {
    return { rows, whole: false }
  }
  const seen = new Set(rows.map((r) => r?.id))
  rows.push(...first.filter((r) => r && !seen.has(r.id)))
  const oldest = first.at(-1)?.time?.updated
  return { rows, whole: first.length === 0 || (typeof oldest === 'number' && oldest < since) }
}

// The beat for one listing begun at ``ts`` (seconds): each session's selection, directory and
// parent, its last primary request's model and time over it
export function beatOf(pid: number, store: string | undefined, ts: number, rows: readonly SessionInfo[],
                       used: ReadonlyMap<string, Used>): Beat {
  const sessions: Record<string, Row> = {}
  for (const info of rows) {
    if (!info || typeof info.id !== 'string') continue
    const row: Row = {}
    if (info.model?.providerID && info.model.id) {
      row.model = refText(info.model)
      const v = variantOf(info.model)
      if (v) row.variant = v
    }
    if (info.location?.directory) row.dir = info.location.directory
    // a subagent's session names its lead: wb takes its calls for the lead's (store.opencode_root,
    // #1477, 2026-11-26), as the wakes go to the root (notices.ts `root`)
    if (typeof info.parentID === 'string' && info.parentID) row.parent = info.parentID
    const u = used.get(info.id)
    if (u) {
      row.model = u.model
      if (u.variant) row.variant = u.variant
      else delete row.variant
      row.used = u.used
    }
    sessions[info.id] = row
  }
  return { pid, ...(store ? { store } : {}), ts, sessions }
}

// wb, as the plugin runs it: the options' (or the package's, or PATH's) with the options' WB_* paths
export function wbRunner(options: Options, base: Record<string, string | undefined> = process.env): Run {
  return (args, input) => new Promise((done) => {
    const env = wbEnv(options, base)
    const found = findWb(options, env)
    if (!found.found) return done({ code: 127, stdout: '', stderr: `wb not found (${found.wb})` })
    let stdout = '', stderr = ''
    const child = spawn(found.wb, [...args], { env, stdio: ['pipe', 'pipe', 'pipe'] })
    const timer = setTimeout(() => child.kill('SIGKILL'), WB_TIMEOUT_MS)
    child.stdout.on('data', (b) => { stdout += String(b) })
    child.stderr.on('data', (b) => { stderr += String(b) })
    child.on('error', (e) => { clearTimeout(timer); done({ code: 127, stdout, stderr: String(e) }) })
    child.on('close', (code) => { clearTimeout(timer); done({ code: code ?? 1, stdout, stderr }) })
    child.stdin.end(input ?? '')
  })
}

// The RPC the pane and /workbench call (#1312 draws them): data only
export const WorkbenchDriver = {
  id: 'workbench-driver',
  methods: {
    // `wb driver --session <id> --json` (model: the TUI's selection, `--model`; once: only the
    // notes not said before, then recorded)
    driver: {
      input: {
        type: 'object',
        properties: { sessionID: { type: 'string' }, model: { type: 'string' }, once: { type: 'boolean' } },
        required: ['sessionID'],
        additionalProperties: false,
      },
      output: { type: 'object' },
    },
    // routes.json's standing `driver` applied to the session (session.switchModel)
    pin: {
      input: { type: 'object', properties: { sessionID: { type: 'string' } }, required: ['sessionID'], additionalProperties: false },
      output: {
        type: 'object',
        properties: { applied: { type: 'boolean' }, model: { type: 'string' }, why: { type: 'string' } },
        required: ['applied'],
        additionalProperties: false,
      },
    },
  },
  events: {},
} as const

export interface Pinned { readonly applied: boolean; readonly model?: string; readonly why?: string }

const sessionOf = (input: unknown): string => {
  const sid = (input as { sessionID?: unknown } | undefined)?.sessionID
  return typeof sid === 'string' ? sid : ''
}

// The pin, applied: routes.json's `driver` (`wb route driver --json`), unless it is `session`,
// can't run (`runs: false`: OpenCode's switchModel takes any id, and every turn then failed
// 'Model unavailable', #1530, 2026-11-26; the panel's toast says wb's why) or the session
// already runs on it
export async function applyPin(host: DriverHost, sessionID: string, run: Run, used: ReadonlyMap<string, Used>): Promise<Pinned> {
  const r = await run(['route', 'driver', '--json'])
  let pin = '', runs: unknown, said: unknown
  try {
    const doc = JSON.parse(r.stdout) as { driver?: unknown; runs?: unknown; why?: unknown }
    pin = String(doc.driver ?? '')
    runs = doc.runs
    said = doc.why
  } catch {
    return { applied: false, why: (r.stderr || r.stdout).trim() || 'wb route driver failed' }
  }
  if (!pin || pin === 'session') return { applied: false, why: 'no pin: each session keeps its own /models choice' }
  if (runs === false) return { applied: false, model: pin, why: `${pin} not applied: ${String(said ?? '') || "it can't run"}` }
  const model = parseRef(pin)
  if (!model) return { applied: false, model: pin, why: `${pin} isn't an OpenCode model id` }
  const now = used.get(sessionID)
  if (now && now.model === refText(model) && (now.variant ?? '') === (model.variant ?? '')) {
    return { applied: false, model: pin, why: 'the session already runs on it' }
  }
  if (!host.session?.switchModel) return { applied: false, model: pin, why: "this OpenCode can't switch a session's model" }
  try {
    await host.session.switchModel({ sessionID, model })
  } catch (e) {
    return { applied: false, model: pin, why: `OpenCode refused it: ${String(e)}` }
  }
  return { applied: true, model: pin }
}

// The server part's driver piece: the hook, the selection events, the beat and the RPC. Returns
// its cleanup. Each piece is skipped when this server lacks it.
export async function startDriver(host: DriverHost, run: Run = wbRunner(host.options),
                                  pid: number = process.pid,
                                  used: Map<string, Used> = new Map()): Promise<() => Promise<void>> {
  const regs: Registration[] = []
  const stop = new AbortController()
  // a hook or stream this server refuses costs only its piece: never the plugin's setup
  if (host.session?.hook) {
    try {
      regs.push(await host.session.hook('model.request', (input) => {
        // the agent loop's own requests only: a title or compaction request may use another model
        if (input.kind !== 'primary' || !input.model) return
        used.set(input.sessionID, { model: refText(input.model), variant: variantOf(input.model), used: Date.now() / 1000 })
      }))
    } catch { /* no `model.request` here: the beat says each session's selection alone */ }
  }
  if (host.event?.subscribe) {
    // a /models pick: the new selection is the driver from now, before its first request
    void (async () => {
      try {
        const events = host.event!.subscribe!({ types: ['session.model.selected'], signal: stop.signal })
        for await (const ev of events) {
          const sid = ev.data?.sessionID, m = ev.data?.model
          const was = sid ? used.get(sid) : undefined
          if (ev.type === 'session.model.selected' && sid && m && was) {
            used.set(sid, { model: refText(m), variant: variantOf(m), used: was.used })
          }
        }
      } catch { /* the stream ended with the server: nothing to record */ }
    })()
  }
  regs.push(await host.rpc.register(WorkbenchDriver, {
    driver: async (input) => {
      const sid = sessionOf(input)
      const once = (input as { once?: unknown }).once === true
      // the TUI's selection, which OpenCode commits to the session (and so to a beat) only at its
      // next prompt: a /models pick, or a new session's model (seen live, 2026-11-26)
      const model = (input as { model?: unknown }).model
      const next = typeof model === 'string' && parseRef(model) ? ['--model', model] : []
      const r = await run(['driver', '--session', sid, '--json', ...next, ...(once ? ['--once'] : [])])
      try {
        return JSON.parse(r.stdout) as object
      } catch {
        return { tool: 'opencode', session: sid, label: '', notes: [], error: (r.stderr || r.stdout).trim() }
      }
    },
    pin: async (input) => applyPin(host, sessionOf(input), run, used),
  }))
  let timer: ReturnType<typeof setTimeout> | undefined
  let stopped = false
  const beat = async () => {
    if (stopped) return
    try {
      await beatOnce(host, run, used, pid)
    } finally {
      if (!stopped) timer = setTimeout(beat, BEAT_MS)
    }
  }
  if (host.session?.list) timer = setTimeout(beat, FIRST_BEAT_MS)
  return async () => {
    stopped = true
    if (timer) clearTimeout(timer)
    stop.abort()
    for (const r of regs) await r.dispose().catch(() => {})
  }
}

// One beat: list, then `wb driver beat` with it on stdin. Its exit is wb's to explain: a beat
// that failed is simply missing, and store.HEARTBEAT_FRESH reads it as unknown, never gone
export async function beatOnce(host: DriverHost, run: Run, used: ReadonlyMap<string, Used>,
                               pid: number = process.pid): Promise<Ran | undefined> {
  const began = Date.now()
  const ts = began / 1000
  const { rows, whole } = await listAll(host.session?.list, began)
  if (!whole && rows.length === 0) return undefined
  const store = whole ? storePath(process.env, host.app?.channel) : undefined
  return run(['driver', 'beat'], JSON.stringify(beatOf(pid, store, ts, rows, used)))
}
