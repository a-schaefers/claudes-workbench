import { expect, test } from 'bun:test'
import {
  BEAT_MS, WorkbenchDriver, applyPin, beatOf, beatOnce, listAll, parseRef, startDriver, storePath,
  type DriverHost, type Ran, type Used,
} from '../driver.ts'

// The driver's heartbeat and RPC (#1316), under OpenCode's own Bun:
// `BUN_BE_BUN=1 opencode test opencode-plugin/test`.

const ok = (stdout = ''): Ran => ({ code: 0, stdout, stderr: '' })

// a wb that records each call and answers from ``answers`` (by its first two words)
function fakeWb(answers: Record<string, string> = {}) {
  const calls: { args: string[]; input?: string }[] = []
  const run = async (args: readonly string[], input?: string) => {
    calls.push({ args: [...args], input })
    return ok(answers[args.slice(0, 2).join(' ')] ?? '')
  }
  return { calls, run }
}

test('the store is OpenCode\'s own database path, as its CLI resolves it', () => {
  expect(storePath({ HOME: '/h' }, 'latest')).toBe('/h/.local/share/opencode/opencode.db')
  expect(storePath({ HOME: '/h', XDG_DATA_HOME: '/x' }, 'prod')).toBe('/x/opencode/opencode.db')
  // a build off the release channels keeps a database of its own
  expect(storePath({ HOME: '/h' }, 'local')).toBe('/h/.local/share/opencode/opencode-local.db')
  expect(storePath({ HOME: '/h', OPENCODE_DISABLE_CHANNEL_DB: '1' }, 'local')).toBe('/h/.local/share/opencode/opencode.db')
  expect(storePath({ HOME: '/h', OPENCODE_DB: 'my.db' }, 'latest')).toBe('/h/.local/share/opencode/my.db')
  expect(storePath({ HOME: '/h', OPENCODE_DB: '/abs/o.db' }, undefined)).toBe('/abs/o.db')
  // none that can be named: its sessions can say live, never gone (store.driver_server)
  expect(storePath({ HOME: '/h', OPENCODE_DB: ':memory:' }, 'latest')).toBeUndefined()
  expect(storePath({ HOME: '/h' }, undefined)).toBeUndefined()
})

test('a model id is parsed as OpenCode parses provider/id#variant', () => {
  expect(parseRef('ollama/qwen3:32b')).toEqual({ providerID: 'ollama', id: 'qwen3:32b' })
  expect(parseRef('openrouter/a/b#high')).toEqual({ providerID: 'openrouter', id: 'a/b', variant: 'high' })
  for (const bad of ['', 'nomodel', '/x', 'p/', 'p/x#', 'p/x#a#b', 'p#q/x']) expect(parseRef(bad)).toBeUndefined()
})

// #1477 (2026-11-26): wb maps a subagent's own shell to its lead by the beat (store.opencode_root)
test('a subagent session\'s row in the beat names its parent, a lead\'s none', () => {
  const beat = beatOf(1, undefined, 0, [{ id: 'kid', parentID: 'lead' }, { id: 'lead' }, { id: 'odd', parentID: '' }], new Map())
  expect(beat.sessions).toEqual({ kid: { parent: 'lead' }, lead: {}, odd: {} })
})

test('a beat holds every session listed, its last primary request over its selection', () => {
  const used = new Map<string, Used>([['s2', { model: 'ollama/big', used: 99 }]])
  const beat = beatOf(42, '/db', 7, [
    { id: 's1', model: { providerID: 'deepinfra', id: 'ds', variant: 'high' }, location: { directory: '/r1' } },
    { id: 's2', model: { providerID: 'deepinfra', id: 'ds', variant: 'high' }, location: { directory: '/r2' } },
    { id: 's3' },
  ], used)
  expect(beat).toEqual({ pid: 42, store: '/db', ts: 7, sessions: {
    s1: { model: 'deepinfra/ds', variant: 'high', dir: '/r1' },
    // the agent's own model, which the session's selection never shows; its variant isn't kept
    s2: { model: 'ollama/big', dir: '/r2', used: 99 },
    s3: {},
  } })
  expect(beatOf(1, undefined, 2, [], new Map())).toEqual({ pid: 1, ts: 2, sessions: {} })
})

test('every page of the store is listed, and a failed page names no whole store', async () => {
  const pages: Record<string, object> = {
    '': { data: [{ id: 'a', time: { updated: 20 } }], cursor: { next: 'c1' } },
    c1: { data: [{ id: 'b', time: { updated: 10 } }], cursor: { next: null } },
  }
  const all = await listAll(async (input) => pages[input?.cursor ?? ''] as never, 100)
  expect(all).toEqual({ rows: [{ id: 'a', time: { updated: 20 } }, { id: 'b', time: { updated: 10 } }], whole: true })
  const broken = await listAll(async (input) => {
    if (input?.cursor) throw new Error('gone')
    return pages['']! as never
  }, 100)
  expect(broken).toEqual({ rows: [{ id: 'a', time: { updated: 20 } }], whole: false })
  expect(await listAll(undefined)).toEqual({ rows: [], whole: false })
})

test('a session updated while the pages are read is listed, and a listing that may miss one is not whole', async () => {
  // the review of #1316 (2026-11-25): newest-updated first, so `c` updated after page one was read
  // moved ahead of the cursor; the store was named whole without it, and its task read as gone
  let reads = 0
  const moving = async (input?: { cursor?: string }) => {
    if (input?.cursor) return { data: [{ id: 'b', time: { updated: 10 } }], cursor: { next: null } } as never
    reads++
    return (reads === 1
      ? { data: [{ id: 'a', time: { updated: 20 } }], cursor: { next: 'c1' } }
      : { data: [{ id: 'c', time: { updated: 150 } }, { id: 'a', time: { updated: 20 } }], cursor: { next: 'c2' } }) as never
  }
  const got = await listAll(moving, 100)
  expect(got.rows.map((r) => r.id)).toEqual(['a', 'b', 'c'])
  expect(got.whole).toBe(true)
  // the first page read again is all newer than the listing: what is past it may be missed too
  const busy = async (input?: { cursor?: string }) => (input?.cursor
    ? { data: [], cursor: { next: null } }
    : { data: [{ id: 'x', time: { updated: 150 } }], cursor: { next: 'c1' } }) as never
  expect((await listAll(busy, 100)).whole).toBe(false)
  // a row without its time can't show that, so the listing names no store
  const bare = async () => ({ data: [{ id: 'x' }] }) as never
  expect(await listAll(bare, 100)).toEqual({ rows: [{ id: 'x' }], whole: false })
})

test('a beat goes to wb driver beat on stdin, with the store only for a whole listing', async () => {
  const wb = fakeWb()
  const host = (fail: boolean): DriverHost => ({
    options: {}, app: { channel: 'latest' },
    rpc: { register: async () => ({ dispose: async () => {} }) },
    session: { list: async () => { if (fail) throw new Error('busy'); return { data: [{ id: 's1', time: { updated: 0 } }] } } },
  })
  await beatOnce(host(false), wb.run, new Map(), 4242)
  expect(wb.calls[0]!.args).toEqual(['driver', 'beat'])
  const sent = JSON.parse(wb.calls[0]!.input!)
  expect(sent.pid).toBe(4242)
  expect(typeof sent.store).toBe('string')
  expect(sent.sessions).toEqual({ s1: {} })
  expect(Math.abs(sent.ts - Date.now() / 1000) < 60).toBe(true)  // seconds, as wb reads it
  // nothing listed at all: no beat (an old one goes stale, read as unknown, never gone)
  expect(await beatOnce(host(true), wb.run, new Map(), 4242)).toBeUndefined()
  expect(wb.calls.length).toBe(1)
  // a page failed after the first: those listed go, with no store (they can say live, never gone)
  const partial: DriverHost = { ...host(false), session: { list: async (input) => {
    if (input?.cursor) throw new Error('busy')
    return { data: [{ id: 's1' }], cursor: { next: 'c1' } } as never
  } } }
  await beatOnce(partial, wb.run, new Map(), 4242)
  const cut = JSON.parse(wb.calls[1]!.input!)
  expect([cut.store, cut.sessions]).toEqual([undefined, { s1: {} }])
})

test('the driver is recorded from primary requests only, and a /models pick moves it', async () => {
  let hooked: ((input: never) => void) | undefined
  let release: () => void = () => {}
  const host: DriverHost = {
    options: {},
    rpc: { register: async () => ({ dispose: async () => {} }) },
    session: { hook: async (_name, cb) => { hooked = cb as never; return { dispose: async () => {} } } },
    event: {
      subscribe: () => ({
        async *[Symbol.asyncIterator]() {
          await new Promise<void>((r) => { release = r })
          yield { type: 'session.model.selected', data: { sessionID: 's1', model: { providerID: 'p', id: 'new' } } }
        },
      }),
    },
  }
  const used = new Map<string, Used>()
  const stop = await startDriver(host, fakeWb().run, 1, used)
  hooked!({ sessionID: 's1', kind: 'primary', model: { providerID: 'p', id: 'old', variant: 'v' } } as never)
  // a title or compaction request may go to a small model: never the driver
  hooked!({ sessionID: 's1', kind: 'title', model: { providerID: 'p', id: 'small' } } as never)
  hooked!({ sessionID: 's2', kind: 'compaction', model: { providerID: 'p', id: 'small' } } as never)
  expect([...used.keys()]).toEqual(['s1'])
  const at = used.get('s1')!
  expect([at.model, at.variant]).toEqual(['p/old', 'v'])
  expect(Math.abs(at.used - Date.now() / 1000) < 60).toBe(true)
  release()
  await new Promise<void>((r) => setTimeout(() => r(), 10))
  expect(used.get('s1')).toEqual({ model: 'p/new', variant: undefined, used: at.used })
  await stop()
})

test('the pin applies routes.json\'s driver unless it is session or already the session\'s', async () => {
  const picks: object[] = []
  const host: DriverHost = {
    options: {}, rpc: { register: async () => ({ dispose: async () => {} }) },
    session: { switchModel: async (input) => { picks.push(input) } },
  }
  const pinned = (driver: string) => fakeWb({ 'route driver': JSON.stringify({ driver }) }).run
  expect((await applyPin(host, 's', pinned('session'), new Map())).applied).toBe(false)
  const same = new Map<string, Used>([['s', { model: 'p/m', variant: 'high', used: 1 }]])
  expect(await applyPin(host, 's', pinned('p/m#high'), same)).toEqual(
    { applied: false, model: 'p/m#high', why: 'the session already runs on it' })
  expect(await applyPin(host, 's', pinned('p/m'), same)).toEqual({ applied: true, model: 'p/m' })
  expect(picks).toEqual([{ sessionID: 's', model: { providerID: 'p', id: 'm' } }])
  // a wb that can't say: nothing switched
  const broken = async () => ({ code: 3, stdout: '', stderr: 'routes.json can\'t be read' })
  expect(await applyPin(host, 's', broken, new Map())).toEqual({ applied: false, why: 'routes.json can\'t be read' })
})

test('the driver RPC answers wb driver --json for the session, --once when asked', async () => {
  const handlers: Record<string, (input: unknown) => Promise<unknown>> = {}
  const host: DriverHost = {
    options: {},
    rpc: { register: async (def, h) => {
      expect(def.id).toBe(WorkbenchDriver.id)
      Object.assign(handlers, h)
      return { dispose: async () => {} }
    } },
  }
  const wb = fakeWb({ 'driver --session': JSON.stringify({ tool: 'opencode', label: 'p/m', notes: [] }) })
  const stop = await startDriver(host, wb.run, 1)
  expect(await handlers.driver!({ sessionID: 'ses_1', once: true })).toEqual({ tool: 'opencode', label: 'p/m', notes: [] })
  expect(wb.calls.at(-1)!.args).toEqual(['driver', '--session', 'ses_1', '--json', '--once'])
  await handlers.driver!({ sessionID: 'ses_1' })
  expect(wb.calls.at(-1)!.args).toEqual(['driver', '--session', 'ses_1', '--json'])
  await stop()
  expect(BEAT_MS).toBe(30_000)  // store.HEARTBEAT_FRESH (120 s) is four beats: keep them apart
})

test('the driver RPC hands wb the TUI\'s selection, which no beat has before the next prompt, and only a model ref', async () => {
  // the live check of #1316's pane (2026-11-26): a /models pick is the session's only at its next
  // prompt, so the row (and /workbench's notes) said the model it replaced, or none in a new session
  const handlers: Record<string, (input: unknown) => Promise<unknown>> = {}
  const host: DriverHost = {
    options: {},
    rpc: { register: async (_def, h) => { Object.assign(handlers, h); return { dispose: async () => {} } } },
  }
  const wb = fakeWb({ 'driver --session': JSON.stringify({ tool: 'opencode', label: 'p/new#high', notes: [] }) })
  const stop = await startDriver(host, wb.run, 7)
  await handlers.driver!({ sessionID: 'ses_1', model: 'p/new#high', once: true })
  expect(wb.calls.at(-1)!.args).toEqual(['driver', '--session', 'ses_1', '--json', '--model', 'p/new#high', '--once'])
  for (const model of ['', 'not a ref', 7]) {
    await handlers.driver!({ sessionID: 'ses_1', model })
    expect(wb.calls.at(-1)!.args).toEqual(['driver', '--session', 'ses_1', '--json'])
  }
  await stop()
})

test('OpenCode\'s `default` variant is the model\'s own: a beat names none', () => {
  const beat = beatOf(1, undefined, 0, [{ id: 's1', model: { providerID: 'p', id: 'm', variant: 'default' } },
    { id: 's2', model: { providerID: 'p', id: 'm', variant: 'high' } }], new Map())
  expect(beat.sessions).toEqual({ s1: { model: 'p/m' }, s2: { model: 'p/m', variant: 'high' } })
})
