import { expect, test } from 'bun:test'
import {
  AWAY_TAIL, JOB_MS, MIDTURN_NOTE, NoticeLane, SOURCE, WorkbenchNotices, noticeId, notices, routesPath, wakeText,
} from '../notices.ts'
import { QUIET_MS } from '../jobs.ts'
import { shown } from '../notices-tui.ts'
import { ChatLane, lane as chatLane } from '../chat.ts'
import { ACTIVE, CLOSED, NEEDS } from '../../mod/hooks/core.ts'
import * as core from '../../mod/hooks/core.ts'
import { upkeep, type Io, type UpkeepHost } from '../upkeep.ts'

// Each phase, and the notice kinds that share its word, by name from core.ts's sets: a quoted phase
// name in opencode-plugin, its tests too, is the kernel check's sign of a copied phase set
interface Phases { readonly asking: string; readonly failed: string; readonly review: string; readonly running: string; readonly stuck: string }
const P = Object.fromEntries([...ACTIVE, ...NEEDS, ...CLOSED].map(p => [p, p])) as unknown as Phases

// Notices and background jobs on OpenCode's path (#1313, #1334), under OpenCode's own Bun: the
// lane against an in-memory state dir, a fake wb and a fake session API. wb's side of each call:
// tests/test_workbench.py (Scout, SettingsOffers) and the ack compare-and-set's own tests.

const HOME = '/state'
type Synth = { sessionID: string; id?: string; text: string; delivery?: string; resume?: boolean; metadata?: Record<string, unknown> }

function world(opts: { wake?: boolean; ack?: number; failSynthetic?: boolean } = {}) {
  const files: Record<string, string> = {}
  const calls: string[] = []
  const limits: Record<string, number | undefined> = {} // each call's time limit, by its line
  const admitted: Synth[] = []      // what the inbox holds: one per id
  const offered: Synth[] = []       // every call to synthetic, a resend too
  const toasts: { key: string; message: string; sessionID: string; notify: boolean }[] = []
  const sessions: Record<string, { id: string; parentID?: string; location?: { directory?: string } }> = {
    ses_drv: { id: 'ses_drv' }, ses_sub: { id: 'ses_sub', parentID: 'ses_drv' }, ses_two: { id: 'ses_two' },
  }
  let clock = 1_800_000_000_000
  const answers: Record<string, { code: number; out: string }> = {
    'chat away': { code: 0, out: 'here' },
    'scout --if-due': { code: 0, out: 'the weekly model check is due: ask your driver to run it (wb scout lists what to check)' },
    'update --check --if-due': { code: 0, out: 'up to date' },
    'optimize start --if-due': { code: 0, out: 'not due' },
  }
  const lane = new NoticeLane({
    run: async (args, session, timeoutMs) => {
      const line = args.join(' ')
      calls.push(session ? `${line} @${session}` : line)
      limits[args[0] === 'ack' ? 'ack' : line] = timeoutMs
      if (args[0] === 'ack' && !args.includes('--if')) return { code: opts.ack ?? 0, out: '' }
      return answers[line] ?? { code: 0, out: '' }
    },
    read: (path, tail) => { const t = files[path]; return t === undefined ? undefined : tail ? t.slice(-tail) : t },
    size: path => files[path] === undefined ? -1 : files[path]!.length,
    list: dir => [...new Set(Object.keys(files).filter(p => p.startsWith(`${dir}/`))
      .map(p => p.slice(dir.length + 1).split('/')[0]!))],
    now: () => clock, pid: 4242, home: HOME, routes: '/config/routes.json', wake: opts.wake ?? true,
    alive: pid => pid === 999, userHome: '/home/u',
  })
  lane.sessions = {
    get: async ({ sessionID }) => { const s = sessions[sessionID]; if (!s) throw new Error('not found'); return s },
    synthetic: async (input) => {
      offered.push(input)
      if (opts.failSynthetic) throw new Error('refused')
      if (!admitted.some(a => a.id === input.id)) admitted.push(input)
      return {}
    },
  }
  lane.emit = t => { toasts.push(t) }
  lane.earsOk = true
  const task = (name: string, phase: string, over: Record<string, unknown> = {}, session = 'ses_drv') => {
    files[`${HOME}/tasks/${name}/task.json`] = JSON.stringify({ name, session, driver: { tool: 'opencode' }, ...over })
    files[`${HOME}/tasks/${name}/status.json`] = JSON.stringify({ phase, updated_at: clock / 1000 })
  }
  const notify = (row: Record<string, unknown>) => {
    const f = `${HOME}/notify.jsonl`
    files[f] = (files[f] ?? '') + JSON.stringify(row) + '\n'
  }
  return {
    lane, files, calls, limits, admitted, offered, toasts, task, notify, sessions,
    tick: (ms: number) => { clock += ms },
    get now() { return clock },
    acks: () => calls.filter(c => c.startsWith('ack ')),
  }
}

test('a review notice wakes its idle session once, as a queued synthetic bound to the notice', async () => {
  const w = world()
  w.task('t1', P.review)
  const ts = w.now / 1000
  w.notify({ task: 't1', kind: P.review, ts, text: 'diff here' })
  await w.lane.scan()
  expect(w.acks()).toEqual([`ack t1 ${ts}`])          // wb's compare-and-set first: the once-only guarantee
  expect(w.admitted.length).toBe(1)
  const s = w.admitted[0]!
  expect(s.sessionID).toBe('ses_drv')
  expect(s.delivery).toBe('queue')
  expect(s.id).toBe(noticeId('t1', P.review, ts))
  expect(s.metadata?.source).toBe(SOURCE)
  expect(s.text.startsWith('[workbench] Task `t1` is ready for review.\n\ndiff here\n\n')).toBe(true)
  expect(w.toasts.map(t => [t.message, t.sessionID, t.notify])).toEqual([['t1 is ready for review', 'ses_drv', true]])
  // its mark now past it (wb ack wrote it): a second look, or a restarted server, adds nothing
  w.files[`${HOME}/tasks/t1/acked`] = String(ts)
  w.notify({ task: 'other', kind: P.review, ts: ts + 1, text: '' })  // the file changed
  await w.lane.scan()
  expect(w.admitted.length).toBe(1)
  expect(w.acks().length).toBe(1)
})

// #1545 (2026-11-26): OpenCode runs no prompt hook for a synthetic message, so a wake's turn got the
// `workbench` entry of the last idle look, the task still running, beside "is ready for review",
// and the driver said it was running and ended its turn
test('a wake puts the session\'s open-tasks entry as the tasks stand now before the synthetic goes in (#1545)', async () => {
  const w = world()
  const order: string[] = []
  const phase = () => String(JSON.parse(w.files[`${HOME}/tasks/t1/status.json`]!).phase)
  const io: Io = {
    read: p => w.files[p],
    list: dir => [...new Set(Object.keys(w.files).filter(p => p.startsWith(`${dir}/`)).map(p => p.slice(dir.length + 1).split('/')[0]!))],
    run: async args => ({ ok: true, out: args[0] === 'status' ? `t1  ${phase()}` : '' }),
    env: { HOME: '/h' },
    now: () => w.now,
  }
  const hooks = new Map<string, (e: unknown) => unknown>()
  const host = {
    options: { env: { WB_HOME: HOME } },
    session: {
      hook: async (name: string, cb: (e: unknown) => unknown) => { hooks.set(name, cb); return { dispose: async () => {} } },
      get: async () => ({ data: { agent: 'workbench', location: { directory: '/r' } } }),
      instructions: { entry: {
        list: async () => ({ data: [] }),
        put: async ({ key, value }: { key: string; value: string }) => { if (key === 'workbench') order.push(`put ${value.split('\n')[1]}`) },
        remove: async () => {},
      } },
    },
    event: { subscribe: async function* () {} },
  }
  const stop = await upkeep(host as unknown as UpkeepHost, core, io)
  try {
    w.task('t1', P.running)
    await hooks.get('prompt')!({ sessionID: 'ses_drv', prompt: { text: 'go' } }) // the start: the task running
    const synthetic = w.lane.sessions!.synthetic
    w.lane.sessions!.synthetic = async input => { order.push(`synthetic ${input.text.split('\n')[0]}`); return synthetic(input) }
    w.task('t1', P.review)
    w.notify({ task: 't1', kind: P.review, ts: w.now / 1000, text: 'diff here' })
    await w.lane.scan()
    expect(order).toEqual([`put t1  ${P.running}`, `put t1  ${P.review}`, 'synthetic [workbench] Task `t1` is ready for review.'])
  } finally {
    await stop!()
  }
})

test('a resent notice under its fixed id admits nothing new: the inbox keeps the first', async () => {
  const a = noticeId('t1', P.review, 1_800_000_000.5)
  expect(a).toBe(noticeId('t1', P.review, 1_800_000_000.5))
  expect(a === noticeId('t1', P.stuck, 1_800_000_000.5)).toBe(false)
  expect(a === noticeId('t1', P.review, 1_800_000_001.5)).toBe(false)
  expect(a === noticeId('t2', P.review, 1_800_000_000.5)).toBe(false)
  // OpenCode's own shape (msg_, 12 hex of the time, 14 more), sorting by the notice's time as
  // OpenCode's own ids do (its 48 bits of ms * 4096 wrap every 2.2 years, theirs too)
  expect(/^msg_[0-9a-f]{12}[0-9a-z]{14}$/.test(a)).toBe(true)
  expect(noticeId('z', P.review, 1_800_000_000) < noticeId('a', P.review, 1_800_000_100)).toBe(true)
  // a second server on the same store: its wb ack is refused (exit 3), and it sends nothing
  const w = world({ ack: 3 })
  w.task('t1', P.review)
  w.notify({ task: 't1', kind: P.review, ts: w.now / 1000, text: '' })
  await w.lane.scan()
  expect(w.offered).toEqual([])
})

test('a subagent\'s task wakes its driver, the root session', async () => {
  const w = world()
  w.task('t1', P.failed, {}, 'ses_sub')
  w.notify({ task: 't1', kind: P.failed, ts: w.now / 1000, text: 'boom' })
  await w.lane.scan()
  expect(w.admitted.map(s => s.sessionID)).toEqual(['ses_drv'])
})

test('another server\'s or Claude Code\'s task is left alone: no ack, no toast', async () => {
  const w = world()
  w.task('elsewhere', P.review, {}, 'ses_gone')                      // not in this store
  // a Claude Code task, in a session this store holds: only its driver keeps it out
  w.task('claude', P.review, { driver: { tool: 'claude' } }, 'ses_two')
  w.notify({ task: 'elsewhere', kind: P.review, ts: w.now / 1000, text: '' })
  w.notify({ task: 'claude', kind: P.review, ts: w.now / 1000 + 1, text: '' })
  await w.lane.scan()
  expect(w.acks()).toEqual([])
  expect(w.offered).toEqual([])
  expect(w.toasts).toEqual([])
})

test('with wake off a notice is toasted and never wakes the session (G5)', async () => {
  const w = world({ wake: false })
  w.task('t1', P.review)
  w.notify({ task: 't1', kind: P.review, ts: w.now / 1000, text: '' })
  await w.lane.scan()
  expect(w.toasts.length).toBe(1)
  expect(w.offered).toEqual([])
  expect(w.acks()).toEqual([])  // left for wb wait
})

test('mid-turn a review waits for the turn to end; a question goes into the turn as a steer', async () => {
  const w = world()
  w.task('t1', P.review)
  w.task('t2', P.asking)
  w.files[`${HOME}/tasks/t2/ask/q1.json`] = '{}'
  w.lane.started('ses_drv')
  w.notify({ task: 't1', kind: P.review, ts: w.now / 1000, text: '' })
  w.notify({ task: 't2', kind: 'question', id: 'q1', ts: w.now / 1000 + 1, text: 'Q: which?' })
  await w.lane.scan()
  expect(w.admitted.map(s => [s.id, s.delivery])).toEqual([[noticeId('t2', 'question', w.now / 1000 + 1), 'steer']])
  expect(w.admitted[0]!.text.endsWith(MIDTURN_NOTE)).toBe(true)
  expect(w.admitted[0]!.text.includes('wb answer t2 --id q1')).toBe(true)
  await w.lane.ended('ses_drv')
  expect(w.admitted.map(s => s.id)).toEqual([noticeId('t2', 'question', w.now / 1000 + 1), noticeId('t1', P.review, w.now / 1000)])
})

test('the idle hook delivers what a busy turn held', async () => {
  const w = world()
  w.task('t1', P.review)
  w.lane.started('ses_drv')
  w.notify({ task: 't1', kind: P.review, ts: w.now / 1000, text: '' })
  await w.lane.scan()
  expect(w.admitted).toEqual([])
  w.lane.idle('ses_drv')
  await new Promise(r => setTimeout(() => r(undefined), 5))
  expect(w.admitted.length).toBe(1)
})

test('a wake OpenCode refuses gives the mark back, and the net tries again after a minute', async () => {
  const w = world({ failSynthetic: true })
  w.task('t1', P.stuck)
  const ts = w.now / 1000
  w.notify({ task: 't1', kind: P.stuck, ts, text: '' })
  await w.lane.scan()
  expect(w.calls.includes(`ack t1 0 --if ${ts}`)).toBe(true)
  expect(w.lane.dropped.has('t1')).toBe(true)
})

test('news no longer true is acknowledged in silence', async () => {
  const w = world()
  w.task('t1', P.running) // it was sent back since its review notice
  w.notify({ task: 't1', kind: P.review, ts: w.now / 1000, text: '' })
  await w.lane.scan()
  expect(w.acks()).toEqual([`ack t1 ${w.now / 1000}`])
  expect(w.offered).toEqual([])
})

test('a task waiting with no news wakes the idle session once after a minute; wake off, never (G5)', async () => {
  for (const wake of [true, false]) {
    const w = world({ wake })
    w.task('t1', P.review)
    await w.lane.scan()
    expect(w.offered).toEqual([])  // under a minute: its notice may still come
    w.tick(61_000)
    await w.lane.scan()
    w.tick(61_000)
    await w.lane.scan()
    if (!wake) { expect(w.offered).toEqual([]); continue }
    expect(w.offered.length).toBe(1)
    expect(w.offered[0]!.text.includes('[workbench] Task `t1` is ready for review, 61s ago, and no notice reached you.')).toBe(true)
    expect(w.offered[0]!.id).toBe(noticeId('t1', 'rewake', (w.now - 122_000) / 1000))
  }
})

test('while the user is away a wake says to answer in the web chat', async () => {
  const w = world()
  ;(w.lane.deps as { run: unknown }).run = async (args: string[]) =>
    ({ code: 0, out: args.join(' ') === 'chat away' ? 'away' : '' })
  w.task('t1', P.review)
  w.notify({ task: 't1', kind: P.review, ts: w.now / 1000, text: '' })
  await w.lane.scan()
  expect(w.admitted[0]!.text.endsWith(AWAY_TAIL)).toBe(true)
})

test('a spend stop\'s next step asks for a cap or its raise, as routes.json has one', () => {
  const row = { task: 't1', kind: P.stuck, ts: 1, text: 'day cap', cause: 'spend' }
  expect(wakeText(row, 't1', false).includes('Ask the user for a day cap')).toBe(true)
  expect(wakeText(row, 't1', true).includes('a higher cap')).toBe(true)
  expect(routesPath({ HOME: '/h' })).toBe('/h/.config/workbench/routes.json')
  expect(routesPath({ HOME: '/h', WB_ROUTES: '~/r.json' })).toBe('/h/r.json')
})

test('the jobs: the update check after a turn, the model check after a quiet minute, each once a load', async () => {
  const w = world()
  w.task('t1', P.review)
  w.files[`${HOME}/tasks/t1/acked`] = String(w.now / 1000 + 10) // its news had: nothing to wake for
  await w.lane.scan()
  expect(w.calls.filter(c => !c.startsWith('chat'))).toEqual([]) // no turn yet
  w.tick(13 * 3600_000) // past rewake's 12 hours: no longer recent, so quiet
  await w.lane.ended('ses_drv')
  await new Promise(r => setTimeout(() => r(undefined), 5))
  expect(w.calls.includes('update --check --if-due')).toBe(true)
  expect(w.calls.includes('optimize start --if-due')).toBe(true)
  expect(w.calls.includes('scout --if-due')).toBe(false) // a quiet minute after the turn first
  w.tick(QUIET_MS)
  await w.lane.scan()
  await w.lane.scan()
  expect(w.calls.filter(c => c === 'scout --if-due').length).toBe(1)
  expect(w.toasts.map(t => t.key)).toEqual(['scout'])
})

test('#1507: a 2.1 Dynamic workflows on is said once a load, its line toasted, and nothing once deepmode has its own key', async () => {
  // 2.1's `mode: on` meant the driver may start several tasks at once; 2.2's deepmode runs every
  // substantive request as a deep workflow, so it starts off and wb deepmode --moved says so once
  const w = world()
  w.files[`${HOME}/workflows.json`] = JSON.stringify({ mode: 'on', ts: 1 })
  const line = 'Deepmode is off: … /deepmode on turns it on.'
  const run = w.lane.deps.run
  ;(w.lane.deps as { run: typeof run }).run = async (args, session, ms) => {
    if (args.join(' ') !== 'deepmode --moved') return run(args, session, ms)
    w.calls.push('deepmode --moved')
    w.files[`${HOME}/workflows.json`] = JSON.stringify({ deepmode: 'off', ts: 2 }) // as wb writes it
    return { code: 0, out: `${line}\n` }
  }
  await w.lane.ended('ses_drv')
  await new Promise(r => setTimeout(() => r(undefined), 5))
  await w.lane.scan()
  expect(w.calls.filter(c => c.startsWith('deepmode'))).toEqual(['deepmode --moved'])
  expect(w.toasts.filter(t => t.key === 'deepmode').map(t => t.message)).toEqual([line])
  // a later load reads deepmode's own key: nothing run, nothing said
  const next = world()
  next.files[`${HOME}/workflows.json`] = w.files[`${HOME}/workflows.json`]!
  await next.lane.ended('ses_drv')
  await new Promise(r => setTimeout(() => r(undefined), 5))
  expect(next.calls.filter(c => c.startsWith('deepmode'))).toEqual([])
})

test('the jobs\' wb calls get the mod\'s two minutes, as the update check fetches; an ack keeps the short limit', async () => {
  const w = world()
  w.task('t1', P.review)
  w.notify({ task: 't1', kind: P.review, ts: w.now / 1000, text: '' })
  await w.lane.scan()
  w.tick(13 * 3600_000)
  await w.lane.ended('ses_drv')
  await new Promise(r => setTimeout(() => r(undefined), 5))
  expect(w.limits['update --check --if-due']).toBe(JOB_MS)
  expect(w.limits['optimize start --if-due']).toBe(JOB_MS)
  expect('ack' in w.limits && w.limits.ack === undefined).toBe(true)
})

test('a settings change with help is offered once, in the session the user typed in', async () => {
  const w = world()
  w.files[`${HOME}/settings-changes.jsonl`] = '{"ts": 1}\n'
  ;(w.lane.deps as { run: unknown }).run = async (args: string[], session?: string) => {
    w.calls.push(`${args.join(' ')}${session ? ` @${session}` : ''}`)
    return args[0] === 'settings' ? { code: 0, out: JSON.stringify({
      offers: [{ ts: 1, key: 'probe', text: '[workbench] The user changed probe… Offer to help', toast: 'probe changed' }],
      notes: [{ ts: 2, key: 'splash', text: 'The user changed splash from on to off on the web app.' }],
    }) } : { code: 0, out: '' }
  }
  w.lane.sawTurn = true
  await w.lane.scan()
  expect(w.calls.filter(c => c.startsWith('settings'))).toEqual([]) // no session to tell yet
  await w.lane.prompted({ sessionID: 'ses_two' })
  await w.lane.scan()
  await w.lane.scan()
  expect(w.calls.filter(c => c.startsWith('settings'))).toEqual(['settings offers --if-due --json @ses_two'])
  expect(w.admitted.map(s => [s.sessionID, s.delivery, s.resume])).toEqual([['ses_two', 'queue', undefined], ['ses_two', 'steer', false]])
  expect(w.toasts.map(t => t.message)).toEqual(['probe changed'])
  // another listener's live claim: that host says them
  w.files[`${HOME}/chat/listener.json`] = JSON.stringify({ session: 'cc', pid: 999 })
  w.files[`${HOME}/settings-changes.jsonl`] += '{"ts": 3}\n'
  await w.lane.scan()
  expect(w.calls.filter(c => c.startsWith('settings')).length).toBe(1)
})

test('the TUI shows each toast once, with a desktop notification for a task that waits', async () => {
  const toasts: unknown[] = []
  const notes: unknown[] = []
  const ctx = { ui: { toast: { show: (o: unknown) => { toasts.push(o) } } },
    attention: { notify: async (o: unknown) => { notes.push(o) } }, client: { rpc: () => ({}) } }
  const seen: string[] = []
  const t = { key: 't1:1', title: 'Workbench', message: 't1 is ready for review', variant: 'info' as const, sessionID: 's', notify: true }
  shown(ctx, seen, t)
  shown(ctx, seen, t) // a second instance's emit of the same
  shown(ctx, seen, { ...t, key: 'update', notify: false, sessionID: '' })
  expect(toasts).toEqual([{ title: 'Workbench', message: 't1 is ready for review', variant: 'info', sessionID: 's' },
    { title: 'Workbench', message: 't1 is ready for review', variant: 'info' }])
  expect(notes).toEqual([{ title: 'Workbench', message: 't1 is ready for review', notification: { when: 'blurred' } }])
})

test('setup registers the notices\' RPC with its toast event, and its cleanup stops every part', async () => {
  const registered: string[] = []
  const disposed: string[] = []
  const hooks: string[] = []
  const stop = await notices({
    options: { wb: '/nonexistent/bin/wb', env: { WB_HOME: '/nonexistent/state' } },
    rpc: { register: async (def: { id: string; events: object }) => {
      registered.push(def.id, ...Object.keys(def.events))
      return { dispose: async () => { disposed.push(def.id) }, events: { emit: async () => {} } }
    } },
    session: { get: async () => ({ id: 'x' }), synthetic: async () => ({}),
      hook: async (name: string) => { hooks.push(name); return { dispose: async () => { disposed.push(name) } } } },
    event: { subscribe: () => ({ [Symbol.asyncIterator]: () => ({ next: () => new Promise(() => {}) }) }) },
  } as never)
  expect(registered).toEqual([WorkbenchNotices.id, 'toast'])
  expect(hooks).toEqual(['experimental.idle', 'prompt'])
  await stop!()
  expect(disposed.sort()).toEqual(['experimental.idle', 'prompt', 'workbench-notices'])
  expect((globalThis as Record<string, unknown>).__workbenchNotices).toBeUndefined()
  // a server without the session or event domains gets none of it
  expect(await notices({ options: {}, rpc: { register: async () => ({ dispose: async () => {} }) } } as never)).toBeUndefined()
})

// A run's own notices (#1278's S9): `run`, no `task`, `phase` the run's when raised
const runFiles = (w: ReturnType<typeof world>, name: string, phase: string, session = 'ses_drv', kind = 'opencode') => {
  w.files[`${HOME}/runs/${name}/run.json`] = JSON.stringify({ name, session, session_kind: kind })
  w.files[`${HOME}/runs/${name}/status.json`] = JSON.stringify({ phase })
}

test('a run\'s own notice wakes the session that started it once, its text whole, taken with wb ack --run', async () => {
  const w = world()
  const ts = w.now / 1000
  runFiles(w, 'audit-1', P.review)
  runFiles(w, 'cc-run', P.review, 'claude-sess', '')                 // a Claude Code session's: the mod's
  const text = '[workbench] Run `audit-1` is done: 7 result(s). Read it: wb run report audit-1'
  w.notify({ run: 'audit-1', kind: 'run_done', phase: P.review, ts, text })
  w.notify({ run: 'cc-run', kind: 'run_done', phase: P.review, ts, text: 'THEIRS' })
  await w.lane.scan()
  expect(w.acks()).toEqual([`ack audit-1 ${ts} --run`])
  expect(w.admitted.map(s => [s.sessionID, s.text, s.delivery, s.id])).toEqual([['ses_drv', text, 'queue', noticeId('run:audit-1', 'run_done', ts)]])
  expect(w.toasts.map(t => t.message)).toEqual(['run audit-1 is done'])
  w.files[`${HOME}/runs/audit-1/acked`] = String(ts)               // wb ack wrote it
  w.notify({ run: 'cc-run', kind: 'run_done', phase: P.review, ts: ts + 1, text: '' })
  await w.lane.scan()
  expect(w.admitted.length).toBe(1)                                 // once
})

test('#1491: a run\'s notice hours old still wakes its session once: a run has no rewake under the hour\'s cutoff', async () => {
  const w = world()
  const ts = w.now / 1000 - 7 * 3600
  runFiles(w, 'aud', P.review)
  w.notify({ run: 'aud', kind: 'run_done', phase: P.review, ts, text: '[workbench] Run `aud` is done' })
  await w.lane.scan()
  expect(w.acks()).toEqual([`ack aud ${ts} --run`])
  expect(w.admitted.map(a => a.text)).toEqual(['[workbench] Run `aud` is done'])
})

test('a run or task that records no session wakes a session here in its repo once; one elsewhere or in ~ gets nothing (#1493)', async () => {
  // a scheduled flow's run.json has no session (schedule.py), nor has a `wb start` typed in a
  // terminal: ownerOf gave none, and the OpenCode user opening the repo next morning was never told
  const w = world()
  const ts = w.now / 1000
  w.sessions.ses_home = { id: 'ses_home', location: { directory: '/home/u' } }
  w.sessions.ses_else = { id: 'ses_else', location: { directory: '/src/other' } }
  w.sessions.ses_repo = { id: 'ses_repo', location: { directory: '/src/app/web' } }
  w.files[`${HOME}/runs/nightly-1/run.json`] = JSON.stringify({ name: 'nightly-1', flow: 'nightly', cwd: '/src/app', repo: '/src/app' })
  w.files[`${HOME}/runs/nightly-1/status.json`] = JSON.stringify({ phase: P.review })
  w.notify({ run: 'nightly-1', kind: 'run_done', phase: P.review, ts, text: 'DONE' })
  w.task('t9', P.review, { cwd: '/src/app', repo: '/src/app' }, '')
  w.notify({ task: 't9', kind: P.review, ts: ts + 1, text: '' })
  w.lane.typed = 'ses_home'                                         // a session in ~ claims none by directory
  w.lane.started('ses_else')
  await w.lane.ended('ses_else')
  expect([w.acks(), w.admitted, w.toasts]).toEqual([[], [], []])
  w.lane.started('ses_repo')                                        // the user opens the repo
  await w.lane.ended('ses_repo')
  expect(w.acks()).toEqual([`ack t9 ${ts + 1}`, `ack nightly-1 ${ts} --run`])
  expect(w.admitted.map(s => [s.sessionID, s.metadata?.task ?? s.metadata?.run])).toEqual([['ses_repo', 't9'], ['ses_repo', 'nightly-1']])
})

test('a scheduled flow done overnight still wakes the session that opens its repo in the morning, while the run waits in that phase (#1493)', async () => {
  // the kernel dropped a run notice an hour old, and runs have no rewake net: the flow done at
  // night was never told (#1493, 2026-11-26)
  const w = world()
  const ts = w.now / 1000 - 270 * 60
  w.sessions.ses_repo = { id: 'ses_repo', location: { directory: '/src/app' } }
  w.files[`${HOME}/runs/nightly-1/run.json`] = JSON.stringify({ name: 'nightly-1', flow: 'nightly', cwd: '/src/app', repo: '/src/app' })
  w.files[`${HOME}/runs/nightly-1/status.json`] = JSON.stringify({ phase: P.review })
  w.notify({ run: 'nightly-1', kind: 'run_done', phase: P.review, ts, text: 'DONE' })
  w.lane.started('ses_repo')
  await w.lane.ended('ses_repo')
  expect(w.acks()).toEqual([`ack nightly-1 ${ts} --run`])
  expect(w.admitted.map(s => [s.sessionID, s.metadata?.run])).toEqual([['ses_repo', 'nightly-1']])
})

// #1542 (2026-11-26): `started` put every executing session into `lately`, and `nearest` took
// one no TUI showed: a script's `opencode run` in the repo acked the run's one wake and got an
// unwatched turn on it, and the user's TUI opened there later got nothing
test('a sessionless run\'s wake never goes to a session no TUI shows (a script\'s opencode run), and the TUI session opened near the repo later gets it (#1542)', async () => {
  const w = world()
  const ts = w.now / 1000 - 6 * 3600
  w.sessions.ses_script = { id: 'ses_script', location: { directory: '/src/app' } }
  w.sessions.ses_tui = { id: 'ses_tui', location: { directory: '/src/app' } }
  w.files[`${HOME}/runs/nightly-1/run.json`] = JSON.stringify({ name: 'nightly-1', flow: 'nightly', cwd: '/src/app', repo: '/src/app' })
  w.files[`${HOME}/runs/nightly-1/status.json`] = JSON.stringify({ phase: P.review })
  w.notify({ run: 'nightly-1', kind: 'run_done', phase: P.review, ts, text: 'DONE' })
  // the lane chat() makes on this server: only what a TUI said it shows counts
  const chat = chatLane(() => new ChatLane({ run: async () => ({ ok: true, out: '' }), read: () => undefined, size: () => -1, now: () => w.now, pid: 1, home: HOME }))
  try {
    w.lane.started('ses_script')
    await w.lane.ended('ses_script')
    expect([w.acks(), w.admitted, w.toasts]).toEqual([[], [], []])
    w.tick(3600_000)
    chat.lane.shown('ses_tui')
    w.lane.started('ses_tui')
    await w.lane.ended('ses_tui')
    expect(w.acks()).toEqual([`ack nightly-1 ${ts} --run`])
    expect(w.admitted.map(s => [s.sessionID, s.metadata?.run])).toEqual([['ses_tui', 'nightly-1']])
  } finally {
    delete (globalThis as Record<string, unknown>).__workbenchChatLane
  }
})

test('a run\'s notice no longer true is acknowledged unsaid, and a busy session gets it once the turn ends', async () => {
  const w = world()
  const ts = w.now / 1000
  runFiles(w, 'resumed', P.running)                                 // paused, then resumed
  runFiles(w, 'waits', 'checkpoint')
  w.notify({ run: 'resumed', kind: 'run_paused', phase: 'paused', ts, text: 'PAUSED' })
  w.notify({ run: 'waits', kind: 'run_checkpoint', phase: 'checkpoint', ts, text: 'MERGE' })
  w.lane.started('ses_drv')
  await w.lane.scan()
  expect(w.acks()).toEqual([`ack resumed ${ts} --run`])             // acknowledged, never said
  expect(w.admitted).toEqual([])                                    // the checkpoint held for the turn
  await w.lane.ended('ses_drv')
  expect(w.admitted.map(s => s.text)).toEqual(['MERGE'])
})
