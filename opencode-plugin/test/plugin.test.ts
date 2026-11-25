import { expect, test } from 'bun:test'
import server, { ID, status } from '../server.ts'
import tui from '../tui.ts'
import { Workbench } from '../rpc.ts'
import { findWb, optionEnv, wbEnv, wbHome } from '../wb.ts'

// The plugin's skeleton (#1311, 2026-11-25), under OpenCode's own Bun:
// `BUN_BE_BUN=1 opencode test opencode-plugin/test`.

// A context whose every use throws: setup under WB_TASK must touch none of it
const untouchable = new Proxy({}, { get: (_, key) => { throw new Error(`setup used ctx.${String(key)}`) } })

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
  try {
    expect(await server.setup(untouchable as never)).toBeUndefined()
    expect(await tui.setup(untouchable as never)).toBeUndefined()
  } finally {
    if (was === undefined) delete process.env.WB_TASK
    else process.env.WB_TASK = was
  }
})

test('the server part registers the status method and nothing else', async () => {
  const was = process.env.WB_TASK
  delete process.env.WB_TASK
  const registered: string[] = []
  try {
    await server.setup({
      options: { wb: '/nonexistent/bin/wb' },
      rpc: { register: async (def: { id: string; methods: object }, handlers: object) => {
        registered.push(def.id, ...Object.keys(def.methods), ...Object.keys(handlers))
        return { dispose: async () => {} }
      } },
    } as never)
  } finally {
    if (was !== undefined) process.env.WB_TASK = was
  }
  expect(registered).toEqual(['workbench', 'status', 'status'])
})

test('the options\' wb is the one run, even when another is on PATH', () => {
  const exec = (p: string) => p === '/clone/bin/wb' || p === '/usr/bin/wb'
  expect(findWb({ wb: '/clone/bin/wb' }, { PATH: '/usr/bin' }, exec)).toEqual(
    { wb: '/clone/bin/wb', found: true, from: 'options' })
  // a missing one is said as missing, never swapped for PATH's
  expect(findWb({ wb: '/gone/bin/wb' }, { PATH: '/usr/bin' }, exec)).toEqual(
    { wb: '/gone/bin/wb', found: false, from: 'options' })
})

test('without options wb is found by name on PATH, else not found', () => {
  const exec = (p: string) => p === '/b/wb'
  expect(findWb(undefined, { PATH: '/a:/b' }, exec)).toEqual({ wb: '/b/wb', found: true, from: 'PATH' })
  expect(findWb({}, { PATH: '/a' }, exec)).toEqual({ wb: 'wb', found: false, from: 'none' })
})

test('the options\' WB_* paths reach wb over the service\'s own environment', () => {
  expect(optionEnv({ env: { WB_HOME: '/state', WB_BAD: 3 } })).toEqual({ WB_HOME: '/state' })
  const env = wbEnv({ env: { WB_HOME: '/state' } }, { WB_HOME: '/other', PATH: '/p' })
  expect(env.WB_HOME).toBe('/state')
  expect(env.PATH).toBe('/p')
  expect(wbHome({ HOME: '/h' })).toBe('/h/.local/state/workbench')
  // a service started with an empty environment still finds the state dir setup named
  expect(status({ wb: '/nonexistent/wb', env: { WB_HOME: '/state' } }, {}).home).toBe('/state')
})
