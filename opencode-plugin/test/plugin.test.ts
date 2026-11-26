import { expect, test } from 'bun:test'
import server, { ID, status } from '../server.ts'
import tui from '../tui.ts'
import { Workbench } from '../rpc.ts'
import { OWN, findWb, optionEnv, wbEnv, wbHome } from '../wb.ts'

// The plugin's skeleton (#1311, 2026-11-25), under OpenCode's own Bun:
// `BUN_BE_BUN=1 opencode test opencode-plugin/test`.

// A context that records every use (and throws at it): setup under WB_TASK must touch none of it.
// The record is what the test asserts on, not the throw: tui.ts catches its RPC's failure, so a
// throw alone let its WB_TASK guard be deleted with the test still passing (#1311's review,
// 2026-11-25)
function untouchable() {
  const used: string[] = []
  const ctx = new Proxy({}, {
    get: (_, key) => { used.push(String(key)); throw new Error(`setup used ctx.${String(key)}`) },
    has: (_, key) => { used.push(`in ${String(key)}`); return false },
  })
  return { ctx, used }
}

test('the server and TUI parts are plugins by OpenCode\'s own check: an id and a setup', () => {
  for (const part of [server, tui]) {
    expect(part.id).toBe('workbench')
    expect(typeof part.setup).toBe('function')
  }
  expect(ID).toBe('workbench')            // what context.PLUGIN_OFF removes ("-workbench")
  expect(Workbench.id).toBe('workbench')
})

test('under WB_TASK neither part does anything at all (#1363)', async () => {
  const was = process.env.WB_TASK
  process.env.WB_TASK = 't-agent'
  const { ctx, used } = untouchable()
  try {
    expect(await server.setup(ctx as never)).toBeUndefined()
    expect(await tui.setup(ctx as never)).toBeUndefined()
    expect(used).toEqual([])
  } finally {
    if (was === undefined) delete process.env.WB_TASK
    else process.env.WB_TASK = was
  }
})

test('the server part registers the status method, the Workbench agent and its skills (#1335), the driver\'s (#1316) and the panel\'s (#1312)', async () => {
  const was = process.env.WB_TASK
  delete process.env.WB_TASK
  const registered: string[] = []
  const transform = (what: string) => async () => { registered.push(what); return { dispose: async () => {} } }
  let stop: unknown
  try {
    stop = await server.setup({
      options: { wb: '/nonexistent/bin/wb' },
      rpc: { register: async (def: { id: string; methods: object }, handlers: object) => {
        registered.push(def.id, ...Object.keys(def.methods), ...Object.keys(handlers))
        return { dispose: async () => {} }
      } },
      skill: { transform: transform('skill.transform'), list: async () => ({ data: [] }) },
      agent: { transform: transform('agent.transform') },
      session: { hook: transform('session.hook') },
      event: { subscribe: async function* () {} },
    } as never)
  } finally {
    if (was !== undefined) process.env.WB_TASK = was
  }
  expect(registered).toEqual(['workbench', 'status', 'status', 'skill.transform', 'agent.transform', 'session.hook',
    'session.hook', 'workbench-driver', 'driver', 'pin', 'driver', 'pin',  // the driver's model.request hook
    'workbench-pane', 'tasks', 'live', 'diff', 'link', 'tasks', 'live', 'diff', 'link'])
  // one cleanup for every part's stop (the agent's event stream and the driver's beat among them)
  expect(typeof stop).toBe('function')
  await (stop as () => Promise<void>)()
})

test('the options\' wb is the one run, even when another is on PATH', () => {
  const exec = (p: string) => p === '/clone/bin/wb' || p === '/usr/bin/wb'
  expect(findWb({ wb: '/clone/bin/wb' }, { PATH: '/usr/bin' }, exec)).toEqual(
    { wb: '/clone/bin/wb', found: true, from: 'options' })
  // a missing one is said as missing, never swapped for PATH's
  expect(findWb({ wb: '/gone/bin/wb' }, { PATH: '/usr/bin' }, exec)).toEqual(
    { wb: '/gone/bin/wb', found: false, from: 'options' })
})

test('without options the package\'s own wb is run before PATH\'s (#1311\'s review)', () => {
  // the Git install brings bin/wb beside opencode-plugin/: the plugin and its wb, one commit
  expect(OWN.endsWith('/bin/wb')).toBe(true)
  expect(OWN.includes('/opencode-plugin/')).toBe(false)
  const exec = (p: string) => p === '/pkg/bin/wb' || p === '/b/wb'
  expect(findWb(undefined, { PATH: '/b' }, exec, '/pkg/bin/wb')).toEqual(
    { wb: '/pkg/bin/wb', found: true, from: 'package' })
  // the options' still come first
  expect(findWb({ wb: '/b/wb' }, { PATH: '' }, exec, '/pkg/bin/wb').from).toBe('options')
})

test('without options or a wb of its own, wb is found by name on PATH, else not found', () => {
  const exec = (p: string) => p === '/b/wb'
  expect(findWb(undefined, { PATH: '/a:/b' }, exec, '/pkg/bin/wb')).toEqual({ wb: '/b/wb', found: true, from: 'PATH' })
  expect(findWb({}, { PATH: '/a' }, exec, '/pkg/bin/wb')).toEqual({ wb: 'wb', found: false, from: 'none' })
})

test('the options\' WB_* paths reach wb over the service\'s own environment', () => {
  expect(optionEnv({ env: { WB_HOME: '/state', WB_BAD: 3 } })).toEqual({ WB_HOME: '/state' })
  const env = wbEnv({ env: { WB_HOME: '/state' } }, { WB_HOME: '/other', PATH: '/p' })
  expect(env.WB_HOME).toBe('/state')
  expect(env.PATH).toBe('/p')
  expect(wbHome({ HOME: '/h' })).toBe('/h/.local/state/workbench')
  // a `~` or relative WB_HOME as store.home expands it (store.PATH_VARS allows both)
  expect(wbHome({ HOME: '/h', WB_HOME: '~/wb' }, '/cwd')).toBe('/h/wb')
  expect(wbHome({ HOME: '/h', WB_HOME: '~' }, '/cwd')).toBe('/h')
  expect(wbHome({ HOME: '/h', WB_HOME: 'state/wb' }, '/cwd')).toBe('/cwd/state/wb')
  // a service started with an empty environment still finds the state dir setup named
  expect(status({ wb: '/nonexistent/wb', env: { WB_HOME: '/state' } }, {}).home).toBe('/state')
})
