import { expect, test } from 'bun:test'
import { AFTER, definitions, DONE_NOTE, HAND_BACK, HAND_ONE, MODELS, QUIT, registerCommands, SCOUT_NOTE, UPDATE, UPDATE_MS,
  type CommandHost, type Deps } from '../commands.ts'

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
