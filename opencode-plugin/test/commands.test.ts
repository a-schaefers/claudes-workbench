import { expect, test } from 'bun:test'
import { AFTER, definitions, DONE_NOTE, HAND_BACK, HAND_ONE, MODELS, QUIT, registerCommands, SCOUT_NOTE, UPDATE, UPDATE_MS,
  type CommandHost, type Deps } from '../commands.ts'
import { ACTIVE, NEEDS } from '../../mod/hooks/core.ts'
import * as core from '../../mod/hooks/core.ts'
import { upkeep, type Io, type UpkeepHost } from '../upkeep.ts'

// the phases by name from core.ts's sets: a quoted phase name in a test is the kernel check's sign
// of a copied phase set
const P = Object.fromEntries([...ACTIVE, ...NEEDS].map(p => [p, p])) as unknown as { readonly running: string; readonly review: string }

// The user's Workbench commands in OpenCode (#1315), against a fake wb and session API:
// `BUN_BE_BUN=1 opencode test opencode-plugin/test/commands.test.ts`.

function world(agent: string, outs: Record<string, { ok: boolean; out: string }> = {}) {
  const ran: { args: readonly string[]; session?: string; cwd?: string; timeout?: number }[] = []
  const done: string[] = []
  const posted: { text: string; description?: string; resume?: boolean }[] = []
  const files: Record<string, string> = {}
  const host: CommandHost = {
    options: {},
    session: {
      get: async ({ sessionID }) => ({ id: sessionID, agent, location: { directory: '/repo' } }),
      switchAgent: async ({ agent: to }) => { done.push(`switch ${to}`) },
      synthetic: async (input) => { posted.push(input) },
    },
  }
  const deps: Deps = {
    run: async (args, opts) => { ran.push({ args, ...opts }); return outs[args[0]!] ?? { ok: true, out: `${args[0]} done` } },
    home: '/state',
    now: () => 1_700_000_000_000,
    read: (p) => files[p],
    write: (p, t) => { files[p] = t },
  }
  const run = async (name: string) => {
    const def = definitions(host, deps).find(d => d.name === name)!
    await def.execute({ sessionID: 's1' })
  }
  return { host, deps, ran, done, posted, files, run }
}

test("/workbench-quit stops the session's agents as that session in its directory and leaves the Workbench agent, starting no turn", async () => {
  const w = world('workbench')
  await w.run(QUIT)
  expect(w.ran).toEqual([{ args: ['stop', '--all', '--park-waiting'], session: 's1', cwd: '/repo' }])
  expect(w.done).toEqual([`switch ${AFTER}`])
  expect(w.posted.length).toBe(1)
  expect(w.posted[0]!.text.startsWith(`[workbench] The user ran /${QUIT}: this session's delegated agents were stopped`)).toBe(true)
  expect(w.posted[0]!.resume).toBe(false)
  // on another agent already: no switch
  const other = world('build')
  await other.run(QUIT)
  expect(other.done).toEqual([])
})

test('/workbench-quit-and-hand-back hands the tasks over and starts the turn that finishes them; nothing handed over is an error', async () => {
  const w = world('workbench', { takeover: { ok: true, out: 'took over t1' } })
  await w.run(HAND_BACK)
  expect(w.ran[0]).toEqual({ args: ['takeover', '--all', '--user'], session: 's1', cwd: '/repo' })
  expect(w.done).toEqual([`switch ${AFTER}`])
  expect(w.posted).toEqual([{ sessionID: 's1', text: DONE_NOTE('took over t1'), description: 'Workbench: agents stopped; taking over. took over t1',
    metadata: { source: 'workbench.command' }, resume: true } as never])
  // one refused (the coding gate), the rest taken over: still the turn (#918)
  const some = world('workbench', { takeover: { ok: false, out: 'took over t1\nrefused t2: Only when I say' } })
  await some.run(HAND_BACK)
  expect(some.posted.length).toBe(1)
  const none = world('workbench', { takeover: { ok: false, out: 'no task to take over' } })
  let error = ''
  await none.run(HAND_BACK).catch(e => { error = String(e) })
  expect(error.includes('wb takeover --all failed')).toBe(true)
  expect(none.done).toEqual([])
  expect(none.posted).toEqual([])
})

// #1598 (2026-11-27): the hand-back's note goes in as a synthetic message, which runs no prompt hook,
// so its turn opened with the last idle look's `workbench` entry, each task taken over still running
// ("`wb wait <name>` to follow it"), beside the note saying it was taken over
test('the hand-back puts the open-tasks entry as the takeover left the tasks before its turn starts (#1598)', async () => {
  const files: Record<string, string> = {}
  const state = (phase: string) => { files['/state/tasks/t-x/status.json'] = JSON.stringify({ phase }) }
  files['/state/tasks/t-x/task.json'] = JSON.stringify({ name: 't-x', session: 's1', driver: { tool: 'opencode' } })
  state(P.running)
  const order: string[] = []
  const phase = () => String(JSON.parse(files['/state/tasks/t-x/status.json']!).phase)
  const io: Io = {
    read: p => files[p],
    list: dir => [...new Set(Object.keys(files).filter(p => p.startsWith(`${dir}/`)).map(p => p.slice(dir.length + 1).split('/')[0]!))],
    run: async args => ({ ok: true, out: args[0] === 'status' ? `t-x  ${phase()}` : '' }),
    env: { HOME: '/h' },
    now: () => 1_700_000_000_000,
  }
  const hooks = new Map<string, (e: unknown) => unknown>()
  const upHost = {
    options: { env: { WB_HOME: '/state' } },
    session: {
      hook: async (name: string, cb: (e: unknown) => unknown) => { hooks.set(name, cb); return { dispose: async () => {} } },
      get: async () => ({ data: { agent: 'workbench', location: { directory: '/repo' } } }),
      instructions: { entry: {
        list: async () => ({ data: [] }),
        put: async ({ key, value }: { key: string; value: string }) => { if (key === 'workbench') order.push(`put ${value.split('\n')[1]}`) },
        remove: async () => {},
      } },
    },
    event: { subscribe: async function* () {} },
  }
  const stop = await upkeep(upHost as unknown as UpkeepHost, core, io)
  try {
    await hooks.get('prompt')!({ sessionID: 's1', prompt: { text: 'go' } }) // the start: the task running
    const w = world('workbench')
    const host: CommandHost = { ...w.host, session: { ...w.host.session,
      synthetic: async input => { order.push(`synthetic ${input.text.split('\n')[0]}`) } } }
    const deps: Deps = { ...w.deps, run: async args => {
      if (args[0] !== 'takeover') return { ok: true, out: '' }
      state(P.review)
      return { ok: true, out: 'took over t-x: the agent is stopped' }
    } }
    await definitions(host, deps).find(d => d.name === HAND_BACK)!.execute({ sessionID: 's1' })
    expect(order).toEqual([`put t-x  ${P.running}`, `put t-x  ${P.review}`,
      `synthetic ${DONE_NOTE('took over t-x: the agent is stopped').split('\n')[0]}`])
  } finally {
    await stop!()
  }
})

test('#1550: a hand-back that exits 0 having taken nothing over leaves the Workbench agent but starts no turn, and says how to hand one over', async () => {
  const out = 'left parked by an earlier quit (leave them unless the user asks): t1'
  const w = world('workbench', { takeover: { ok: true, out } })
  await w.run(HAND_BACK)
  expect(w.done).toEqual([`switch ${AFTER}`])
  expect(w.posted).toEqual([{ sessionID: 's1', text: `${out}\n${HAND_ONE}`, description: `Workbench: nothing handed over. ${out}`,
    metadata: { source: 'workbench.command' }, resume: false } as never])
  expect(HAND_ONE.includes('`wb takeover <name>`')).toBe(true)
})

test("/workbench-models marks the weekly check raised and starts the driver's model check", async () => {
  const w = world('workbench')
  w.files['/state/scout.json'] = JSON.stringify({ done_at: 5 })
  await w.run(MODELS)
  expect(JSON.parse(w.files['/state/scout.json']!)).toEqual({ done_at: 5, offered_at: 1_700_000_000 })
  expect(w.posted[0]!.text).toBe(SCOUT_NOTE)
  expect(w.posted[0]!.resume).toBe(true)
  expect(SCOUT_NOTE.includes('the `workbench-models` skill')).toBe(true) // the plugin's skill, never the mod's reference path
})

test('/workbench-update runs wb update with its long limit, and its failure is the command\'s error', async () => {
  const w = world('workbench', { update: { ok: true, out: 'updated to abc1234' } })
  await w.run(UPDATE)
  expect(w.ran).toEqual([{ args: ['update'], session: 's1', cwd: '/repo', timeout: UPDATE_MS }])
  expect(w.posted[0]!.resume).toBe(false)
  const bad = world('workbench', { update: { ok: false, out: 'not a clone' } })
  let error = ''
  await bad.run(UPDATE).catch(e => { error = String(e) })
  expect(error.includes('wb update failed: not a clone')).toBe(true)
})

test('the four commands register through command.transform, and nothing without it', async () => {
  const names: string[] = []
  const host = { options: {}, command: { transform: async (cb: (e: { add(d: { name: string }): void }) => void) => {
    cb({ add: (d) => names.push(d.name) })
    return { dispose: async () => {} }
  } } }
  const stop = await registerCommands(host as unknown as CommandHost, world('build').deps)
  expect(names).toEqual([QUIT, HAND_BACK, MODELS, UPDATE])
  await stop!()
  expect(await registerCommands({ options: {} })).toBeUndefined()
})
