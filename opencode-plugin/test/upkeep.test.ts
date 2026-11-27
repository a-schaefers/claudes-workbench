import { expect, test } from 'bun:test'
import * as core from '../../mod/hooks/core.ts'
import { ChatLane, lane } from '../chat.ts'
import { autoCeiling, AWAY_NOTE, blocksOf, CODING_RULES, KEEP_ON_COMPACT, MODE_NOTE, openRuns, openTasks, promptTokens, routesSaid, Upkeep,
  TASKS_NOTE, upkeep, WAKE_OFF_NOTE, WORKFLOWS_RULE, type Io, type UpkeepHost } from '../upkeep.ts'
import { CLAUDE_CODE } from '../../mod/hooks/story.ts'

// The driver's conversation kept (#1315), against an in-memory WB_HOME, a fake wb and a fake
// session API: `BUN_BE_BUN=1 opencode test opencode-plugin/test/upkeep.test.ts`.

const HOME = '/state'
// phases from the kernel's tables, never spelled here (scripts/kernel.py phase_literals): one under
// way, one waiting on the driver, one closed
const RUNNING = [...core.ACTIVE].find(p => core.phaseOf({ phase: p, parked: false }).running)!
const WAITING = [...core.NEEDS].find(p => !core.ACTIVE.has(p) && core.phaseOf({ phase: p, parked: false }).waits)!
const CLOSED = [...core.CLOSED][0]!

function world(files: Record<string, string>, outs: Record<string, string> = {}, env: Record<string, string> = {}) {
  const ran: { args: readonly string[]; session?: string; cwd?: string }[] = []
  const io: Io = {
    read: (p) => files[p],
    list: (dir) => [...new Set(Object.keys(files).filter(p => p.startsWith(dir + '/')).map(p => p.slice(dir.length + 1).split('/')[0]!))],
    run: async (args, opts) => { ran.push({ args, ...opts }); return { ok: true, out: outs[args.join(' ')] ?? '' } },
    env: { HOME: '/h', ...env },
    now: () => 1_000_000,
  }
  return { io, ran, files }
}

const task = (files: Record<string, string>, name: string, phase: string, session = 's1') => {
  files[`${HOME}/tasks/${name}/status.json`] = JSON.stringify({ phase })
  files[`${HOME}/tasks/${name}/task.json`] = JSON.stringify({ name, session })
}

function entriesHost(list: { key: string; value: unknown }[] = []) {
  const calls: string[] = []
  const compacted: string[] = []
  const host = {
    options: {},
    session: {
      hook: async () => ({ dispose: async () => {} }),
      compact: async ({ sessionID }: { sessionID: string }) => { compacted.push(sessionID) },
      instructions: { entry: {
        list: async () => ({ data: list }),
        put: async ({ key, value }: { key: string; value: string }) => { calls.push(`put ${key}: ${value.split('\n')[0]}`) },
        remove: async ({ key }: { key: string }) => { calls.push(`remove ${key}`) },
      } },
    },
    event: { subscribe: async function* () {} },
    model: { list: async () => ({ data: [{ providerID: 'p', id: 'm', limit: { context: 100_000, output: 8_000 } },
      { providerID: 'p', id: 'm64', limit: { context: 65_536, output: 8_000 } }] }) },
  }
  return { host: host as unknown as UpkeepHost, calls, compacted }
}

function rules(files: Record<string, string>) {
  files[`${HOME}/notes.md`] = 'Always run the tests.\n'
  files[`${HOME}/coding.json`] = JSON.stringify({ mode: 'only' })
  files[`${HOME}/workflows.json`] = JSON.stringify({ deepmode: 'on' })
  files['/h/.config/workbench/routes.json'] = JSON.stringify({ roles: { significant: { via: 'api', api: 'p/m' } } })
}

test("a Workbench session's entries are the kernel's blocks under the mod's names, each put once", async () => {
  const w = world({}, { route: 'significant  API p/m', status: 't1  running' })
  rules(w.files)
  task(w.files, 't1', RUNNING)
  task(w.files, 't2', RUNNING, 's2') // another session's: never this one's
  const { host, calls } = entriesHost([{ key: 'workbench-routes', value: 'stale' }, { key: 'someone-else', value: 'x' }])
  const u = new Upkeep(core, host, w.io, HOME)
  u.setAgent(u.sess('s1'), 'workbench')
  await u.refresh('s1')
  // tellPlan's order, the mod's block names (SKILL.md names them); a stale one of ours replaced,
  // another plugin's left alone
  expect(calls.map(c => c.slice(0, c.indexOf(':')))).toEqual(['put workbench-mode', 'put claude-coding', 'put user-notes',
    'put dynamic-workflows', 'put workbench-routes', 'put workbench'])
  const said = new Map(u.sess('s1').put)
  expect(said.get('workbench-mode')).toBe(MODE_NOTE)
  expect(said.get('claude-coding')).toBe(CODING_RULES.only)
  expect(said.get('user-notes')).toBe("The user's delegation notes (`wb notes`):\nAlways run the tests.")
  expect(said.get('dynamic-workflows')).toBe(WORKFLOWS_RULE)
  expect(said.get('workbench-routes')!.split('\n').slice(0, 2)).toEqual(["Roles (the user's, `wb route`):", 'significant  API p/m'])
  expect(said.get('workbench')!.split('\n').slice(0, 2)).toEqual(['Tasks delegated to agents with wb (your Workbench instructions) are open:', 't1  running'])
  expect(said.has('someone-else')).toBe(false) // never ours to list, put or remove
  // wb status ran as the session; nothing again while nothing changed
  expect(w.ran.filter(r => r.args[0] === 'status')).toEqual([{ args: ['status'], session: 's1', cwd: '' }])
  calls.length = 0
  await u.refresh('s1')
  expect(calls).toEqual([])
  expect(w.ran.filter(r => r.args[0] === 'status').length).toBe(1) // read again only when its tasks change
  // with the entries API, the requests carry none of it themselves: OpenCode sends the entries
  const system: { type: 'text'; text: string }[] = []
  await u.context({ sessionID: 's1', agent: 'workbench', system })
  expect(system).toEqual([])
})

test("switching away from the Workbench agent drops the mode's blocks, and nothing open drops the rest", async () => {
  const w = world({}, { route: 'significant  API p/m', status: 't1  running' })
  rules(w.files)
  task(w.files, 't1', RUNNING)
  const { host, calls } = entriesHost()
  const u = new Upkeep(core, host, w.io, HOME)
  u.setAgent(u.sess('s1'), 'workbench')
  await u.refresh('s1')
  calls.length = 0
  // OpenCode says the removal to the model itself ("… no longer applies"): the mod's midway row
  await u.event({ type: 'session.agent.selected', data: { sessionID: 's1', agent: 'build' } })
  expect(calls).toEqual(['remove workbench-mode', 'remove user-notes'])
  calls.length = 0
  task(w.files, 't1', CLOSED)
  await u.refresh('s1')
  expect(calls.sort()).toEqual(['remove claude-coding', 'remove dynamic-workflows', 'remove workbench', 'remove workbench-routes'])
})

test('a switch to the Workbench agent puts its blocks before the session makes a request', async () => {
  const w = world({})
  const { host, calls } = entriesHost()
  const u = new Upkeep(core, host, w.io, HOME)
  await u.event({ type: 'session.agent.selected', data: { sessionID: 's5', agent: 'build' } })
  expect(calls).toEqual([]) // a session no hook named, on another agent: not this part's
  await u.event({ type: 'session.agent.selected', data: { sessionID: 's5', agent: 'workbench' } })
  expect(calls).toEqual([`put workbench-mode: ${MODE_NOTE}`])
})

test('the blocks follow the kernel: no roles block without an API role, the default coding said by nothing', () => {
  const want = { notes: '', wakeOff: false, coding: 'decide', routes: '', workflows: 'off' }
  expect(blocksOf(core, want, true, '', '').map(b => b[0])).toEqual(['workbench-mode'])
  expect(blocksOf(core, want, false, '', '')).toEqual([])
  expect(blocksOf(core, { ...want, coding: 'ask' }, false, '', 't1').map(b => b[0])).toEqual(['claude-coding', 'workbench'])
})

test('#1515: the open-tasks note follows a task with wb wait, never a bare wb watch, in the mod\'s words', () => {
  // wb watch follows a task until it closes: a driver that ran it on a task at review waited out
  // its shell's timeout. wb wait ends when the task needs the driver
  const note = TASKS_NOTE('t1   review')
  expect(note.includes('`wb wait <name>`')).toBe(true)
  expect(note.includes('`wb watch <name>`')).toBe(false)
  expect(note.endsWith(`\n${CLAUDE_CODE['tasks.pickup']}`)).toBe(true) // the mod's block ends with the same line
})

test('#1491: a run waiting at review with no task open is said in the workbench entry, its lines alone', async () => {
  // wb status prints a run's lines after `no open tasks`: dropping the whole text, and asking wb
  // status only with a task open, hid a run done overnight (a scheduled flow's) from the session
  const status = 'no open tasks (wb status --all shows closed ones)\nrun aud · flow triage · review · $0 of $0\n  why: its final stage is Claude\'s'
  const w = world({}, { status })
  w.files[`${HOME}/runs/aud/status.json`] = JSON.stringify({ phase: WAITING })
  w.files[`${HOME}/runs/old/status.json`] = JSON.stringify({ phase: 'closed' })
  const { host } = entriesHost()
  const u = new Upkeep(core, host, w.io, HOME)
  await u.refresh('s1')
  const said = u.sess('s1').put?.get('workbench') ?? ''
  expect(said.split('\n').slice(1, 3)).toEqual(['run aud · flow triage · review · $0 of $0', "  why: its final stage is Claude's"])
  expect(said.includes('no open tasks')).toBe(false)
  expect(openRuns(w.io, HOME)).toEqual([`aud:${WAITING}`])            // a closed run is nothing open
  // nothing open in wb's answer (another session's run): no entry
  const quiet = world({}, { status: 'no open tasks (wb status --all shows closed ones)' })
  quiet.files[`${HOME}/runs/aud/status.json`] = JSON.stringify({ phase: WAITING })
  const v = new Upkeep(core, entriesHost().host, quiet.io, HOME)
  await v.refresh('s1')
  expect(v.sess('s1').put?.has('workbench') ?? false).toBe(false)
})

test("without OpenCode's entries API the blocks go into the session's own requests' system prompt", async () => {
  const w = world({}, { route: 'significant  API p/m' })
  rules(w.files)
  const { host } = entriesHost()
  delete (host.session as { instructions?: unknown }).instructions
  const u = new Upkeep(core, host, w.io, HOME)
  const system: { type: 'text'; text: string }[] = []
  await u.context({ sessionID: 's1', agent: 'workbench', system })
  expect(system.map(p => p.text.split('\n')[0])).toEqual(['<context key="workbench-mode">', '<context key="claude-coding">',
    '<context key="user-notes">', '<context key="dynamic-workflows">', '<context key="workbench-routes">'])
  const other: { type: 'text'; text: string }[] = []
  await u.context({ sessionID: 's2', agent: 'build', system: other })
  expect(other).toEqual([])
})

test("the web-chat block is the session's while wb says the user is away, and goes when they are back", async () => {
  const w = world({}, { 'chat away': 'away' })
  const { host, calls } = entriesHost()
  const u = new Upkeep(core, host, w.io, HOME)
  u.setAgent(u.sess('s1'), 'workbench')
  await u.refresh('s1')
  // no time in chat/away.json: never away, and no wb run to learn it
  expect(w.ran.filter(r => r.args[0] === 'chat')).toEqual([])
  expect(u.sess('s1').put!.has('web-chat')).toBe(false)
  w.files[`${HOME}/chat/away.json`] = JSON.stringify({ at: 999, by: 's1' })
  calls.length = 0
  await u.refresh('s1')
  expect(calls).toEqual([`put web-chat: ${AWAY_NOTE}`]) // OpenCode says the new context at the next step
  expect(AWAY_NOTE.includes('workbench_chat_reply')).toBe(true) // the tool's name as the model sees it
  expect(w.ran.filter(r => r.args[0] === 'chat')).toEqual([{ args: ['chat', 'away'], session: 's1' }]) // wb decides, as the session
  // the user typed in the terminal (chat.ts's `wb chat back`): the entry goes ("… no longer applies")
  w.files[`${HOME}/chat/away.json`] = JSON.stringify({ at: 0 })
  calls.length = 0
  await u.refresh('s1')
  expect(calls).toEqual(['remove web-chat'])
  // wb says here (the six hours past, the chat off): no block either
  w.files[`${HOME}/chat/away.json`] = JSON.stringify({ at: 999 })
  const here = world({ ...w.files }, { 'chat away': 'here' })
  const v = new Upkeep(core, entriesHost().host, here.io, HOME)
  v.setAgent(v.sess('s1'), 'workbench')
  await v.refresh('s1')
  expect(v.sess('s1').put!.has('web-chat')).toBe(false)
  // a session out of the mode with nothing open isn't Workbench's to tell: no block, no wb run
  const other = world({ [`${HOME}/chat/away.json`]: JSON.stringify({ at: 999 }) }, { 'chat away': 'away' })
  const o = new Upkeep(core, entriesHost().host, other.io, HOME)
  o.setAgent(o.sess('s3'), 'build')
  await o.refresh('s3')
  expect([o.sess('s3').put!.size, other.ran.length]).toEqual([0, 0])
})

test('a prompt the user typed is them back at the terminal: that turn gets no web-chat block, a web message\'s does', async () => {
  const w = world({ [`${HOME}/chat/away.json`]: JSON.stringify({ at: 999 }) }, { 'chat away': 'away' })
  const { host, calls } = entriesHost()
  const hooks = new Map<string, (e: unknown) => unknown>()
  ;(host.session as { hook: unknown }).hook = async (name: string, cb: (e: unknown) => unknown) => {
    hooks.set(name, cb)
    return { dispose: async () => {} }
  }
  ;(host.session as { get: unknown }).get = async () => ({ data: { agent: 'workbench', location: { directory: '/r' } } })
  ;(host as { options: unknown }).options = { env: { WB_HOME: HOME } }
  const stop = await upkeep(host, core, w.io)
  // chat.ts's own delivery (its metadata): the user is still away, and wb says so
  await hooks.get('prompt')!({ sessionID: 's1', prompt: { text: '[web chat] hi' }, metadata: { source: 'workbench.chat' } })
  expect(calls.includes(`put web-chat: ${AWAY_NOTE}`)).toBe(true)
  // typed in the terminal: away.json may still have its time (chat.ts's `wb chat back` runs beside
  // this hook, not before it), and the block goes before the turn all the same
  calls.length = 0
  await hooks.get('prompt')!({ sessionID: 's1', prompt: { text: 'carry on' } })
  expect(calls).toEqual(['remove web-chat'])
  await stop!()
})

// #1492 (2026-11-26): `back` asked whether a TUI showed the session, but the TUI shows a new
// session only after its first prompt, so a user back from the phone typing in a new session got
// the away block telling the driver to answer on the phone
test('a prompt typed in a new session no TUI has shown yet, with the chat lane up, gets no web-chat block', async () => {
  const w = world({ [`${HOME}/chat/away.json`]: JSON.stringify({ at: 999 }) }, { 'chat away': 'away' })
  const { host, calls } = entriesHost()
  const hooks = new Map<string, (e: unknown) => unknown>()
  ;(host.session as { hook: unknown }).hook = async (name: string, cb: (e: unknown) => unknown) => {
    hooks.set(name, cb)
    return { dispose: async () => {} }
  }
  ;(host.session as { get: unknown }).get = async () => ({ data: { agent: 'workbench', location: { directory: '/r' } } })
  ;(host as { options: unknown }).options = { env: { WB_HOME: HOME } }
  // the lane chat() always makes on this server, which no TUI has told of ses_new
  lane(() => new ChatLane({ run: async () => ({ ok: true, out: '' }), read: () => undefined, size: () => -1, now: () => 1, pid: 1, home: HOME }))
  try {
    const stop = await upkeep(host, core, w.io)
    await hooks.get('prompt')!({ sessionID: 'ses_new', prompt: { text: 'carry on' } })
    expect(calls.filter(c => c.startsWith('put ')).map(c => c.slice(0, c.indexOf(':')))).toEqual(['put workbench-mode'])
    await stop!()
  } finally {
    delete (globalThis as Record<string, unknown>).__workbenchChatLane
  }
})

test("the workbench-wake block says wakes are off while the plugin's wake option is off, in the mode or with a task open", async () => {
  const w = world({}, { status: 't1  running' })
  const { host, calls } = entriesHost()
  ;(host as { options: unknown }).options = { wake: 'off' }
  const u = new Upkeep(core, host, w.io, HOME)
  u.setAgent(u.sess('s1'), 'build')
  await u.refresh('s1')
  expect(calls).toEqual([]) // out of the mode with nothing open: nothing to say
  task(w.files, 't1', RUNNING)
  await u.refresh('s1')
  expect(calls).toEqual([`put workbench-wake: ${WAKE_OFF_NOTE}`, 'put workbench: Tasks delegated to agents with wb (your Workbench instructions) are open:'])
  const on = world({}, {})
  const v = new Upkeep(core, entriesHost().host, on.io, HOME)
  v.setAgent(v.sess('s1'), 'workbench')
  await v.refresh('s1')
  expect(v.sess('s1').put!.has('workbench-wake')).toBe(false) // wakes on (no option): no block
})

// a session in the mode, its first step's prompt `first` long (the fixed prompt: 1K unless
// given), its task t1 open at one look and closed at the next, the last step's prompt `tokens`
// long, on a 100K window (OpenCode compacts by itself at 84K)
async function boundary(tokens: number, opts: { env?: Record<string, string>; agent?: string; also?: string; model?: string; first?: number } = {}) {
  const w = world({}, {}, opts.env)
  task(w.files, 't1', RUNNING)
  if (opts.also) task(w.files, 't9', opts.also)
  const { host, compacted } = entriesHost()
  const u = new Upkeep(core, host, w.io, HOME)
  await u.context({ sessionID: 's1', agent: opts.agent ?? 'workbench', model: { providerID: 'p', id: opts.model ?? 'm' }, system: [] })
  await u.event({ type: 'session.execution.started', data: { sessionID: 's1' } })
  await u.event({ type: 'session.step.ended', data: { sessionID: 's1', tokens: { input: opts.first ?? 1_000 } } })
  await u.event({ type: 'session.execution.succeeded', data: { sessionID: 's1' } })
  expect(compacted).toEqual([]) // nothing closed yet
  task(w.files, 't1', CLOSED)
  await u.event({ type: 'session.execution.started', data: { sessionID: 's1' } })
  await u.event({ type: 'session.step.ended', data: { sessionID: 's1', tokens: { input: tokens - 20_000, output: 900, reasoning: 0, cache: { read: 20_000, write: 0 } } } })
  await u.event({ type: 'session.execution.succeeded', data: { sessionID: 's1' } })
  const first = [...compacted]
  await u.event({ type: 'session.execution.succeeded', data: { sessionID: 's1' } }) // checked once
  return { first, compacted }
}

test("a task closing compacts the Workbench session once it is halfway to OpenCode's own compaction", async () => {
  expect(autoCeiling(100_000)).toBe(84_000)
  expect(promptTokens({ input: 1, output: 5, cache: { read: 2, write: 3 } })).toBe(6)
  // halfway from the 1K first step to 84K: 42.5K
  const past = await boundary(43_000)
  expect(past.first).toEqual(['s1'])
  expect(past.compacted).toEqual(['s1'])
  expect((await boundary(42_000)).compacted).toEqual([])
})

// #1569 (2026-11-26): a 64K OpenCode driver (the fit note's) compacted at every task close: its
// fixed prompt, the procedure and OpenCode's own (about 28K), was over half of its 49.5K
// compaction point, and each compaction freed about 2K
test('the boundary compaction of a 64K OpenCode driver waits for its conversation, not its fixed prompt (#1569)', async () => {
  const ceiling = autoCeiling(65_536)
  expect(ceiling).toBe(49_536)
  const at = (last: number) => boundary(last, { model: 'm64', first: 28_000 })
  expect((await at(30_700)).compacted).toEqual([])
  expect((await at(28_000 + 0.5 * (ceiling - 28_000) + 1)).compacted).toEqual(['s1'])
  // a first step over the compaction point (#1487's band): OpenCode compacts every request itself
  expect((await boundary(80_000, { model: 'm64', first: 50_000 })).compacted).toEqual([])
})

test('after a compaction the boundary is measured from the first step that follows it (#1569)', async () => {
  const w = world({})
  task(w.files, 't1', RUNNING)
  const { host, compacted } = entriesHost()
  const u = new Upkeep(core, host, w.io, HOME)
  const step = async (input: number) => {
    await u.event({ type: 'session.execution.started', data: { sessionID: 's1' } })
    await u.event({ type: 'session.step.ended', data: { sessionID: 's1', tokens: { input } } })
    await u.event({ type: 'session.execution.succeeded', data: { sessionID: 's1' } })
  }
  await u.context({ sessionID: 's1', agent: 'workbench', model: { providerID: 'p', id: 'm' }, system: [] })
  await step(1_000)
  await u.event({ type: 'session.compaction.ended', data: { sessionID: 's1' } }) // OpenCode's own, say
  await step(30_000) // the summary and the fixed prompt: the new floor
  task(w.files, 't1', CLOSED)
  await step(50_000) // 20K past the floor, under half of the 54K left: measured from 1K it would compact
  expect(compacted).toEqual([])
})

// #1578 (2026-11-26): the TUI opens on Build, so a session's first step was often Build's 10K
// prompt, and #1569's floor stayed there after /workbench: the boundary compaction came about
// 10K early and freed about 2K
test('a session that began on Build is measured from its first Workbench step (#1578)', async () => {
  const fromBuild = async (last: number) => {
    const w = world({})
    task(w.files, 't1', RUNNING)
    const { host, compacted } = entriesHost()
    const u = new Upkeep(core, host, w.io, HOME)
    const step = async (agent: string, input: number) => {
      await u.context({ sessionID: 's1', agent, model: { providerID: 'p', id: 'm64' }, system: [] })
      await u.event({ type: 'session.execution.started', data: { sessionID: 's1' } })
      await u.event({ type: 'session.step.ended', data: { sessionID: 's1', tokens: { input } } })
      await u.event({ type: 'session.execution.succeeded', data: { sessionID: 's1' } })
    }
    await step('build', 10_300)
    await step('workbench', 28_100)
    task(w.files, 't1', CLOSED)
    await step('workbench', last)
    return { compacted, floor: u.sess('s1').floor }
  }
  expect(await fromBuild(30_200)).toEqual({ compacted: [], floor: 28_100 })
  expect((await fromBuild(28_100 + 0.5 * (autoCeiling(65_536) - 28_100) + 1)).compacted).toEqual(['s1'])
})

test('the boundary compaction follows WB_COMPACT_AT, waits for every task waiting on the driver, and is the mode\'s alone', async () => {
  expect((await boundary(2_000, { env: { WB_COMPACT_AT: '1' } })).first).toEqual(['s1'])
  expect((await boundary(80_000, { env: { WB_COMPACT_AT: '0' } })).compacted).toEqual([])
  expect((await boundary(80_000, { also: WAITING })).compacted).toEqual([])
  expect((await boundary(80_000, { agent: 'build' })).compacted).toEqual([])
  // a model whose window OpenCode doesn't list: never compacted early, as OpenCode never compacts it
  expect((await boundary(80_000, { model: 'unlisted' })).compacted).toEqual([])
})

test('a compaction of a session that delegated, with a task of its own open, is told what to keep, once', async () => {
  const w = world({})
  task(w.files, 't1', RUNNING)
  const { host } = entriesHost()
  const u = new Upkeep(core, host, w.io, HOME)
  const system: { type: 'text'; text: string }[] = []
  u.setAgent(u.sess('s1'), 'build')
  u.compaction({ sessionID: 's1', system })
  expect(system).toEqual([]) // a session never in the mode here
  u.setAgent(u.sess('s1'), 'workbench')
  u.compaction({ sessionID: 's1', system })
  u.compaction({ sessionID: 's1', system })
  expect(system).toEqual([{ type: 'text', text: KEEP_ON_COMPACT }])
  task(w.files, 't1', CLOSED)
  const none: { type: 'text'; text: string }[] = []
  u.compaction({ sessionID: 's1', system: none })
  expect(none).toEqual([])
})

test('the boundary compaction after the last task closed is told what to keep, once, and a later one without a task is not', async () => {
  const w = world({})
  task(w.files, 't1', RUNNING)
  const { host } = entriesHost()
  const systems: string[][] = []
  const u = new Upkeep(core, host, w.io, HOME)
  // OpenCode runs every compaction request through the `compaction` hook (core/src/session/model-request.ts)
  const compact = (sessionID: string) => {
    const system: { type: 'text'; text: string }[] = []
    u.compaction({ sessionID, system })
    systems.push(system.map(p => p.text))
  }
  ;(host.session as { compact: unknown }).compact = async ({ sessionID }: { sessionID: string }) => {
    compact(sessionID)
    await u.event({ type: 'session.compaction.ended', data: { sessionID } })
  }
  await u.context({ sessionID: 's1', agent: 'workbench', model: { providerID: 'p', id: 'm' }, system: [] })
  await u.event({ type: 'session.step.ended', data: { sessionID: 's1', tokens: { input: 1_000 } } })
  await u.event({ type: 'session.execution.succeeded', data: { sessionID: 's1' } })
  task(w.files, 't1', CLOSED)
  await u.event({ type: 'session.step.ended', data: { sessionID: 's1', tokens: { input: 60_000 } } })
  await u.event({ type: 'session.execution.succeeded', data: { sessionID: 's1' } })
  expect(systems).toEqual([[KEEP_ON_COMPACT]])
  compact('s1') // the user's own /compact afterwards, nothing open: as before, nothing added
  expect(systems[1]).toEqual([])
})

test("the roles block is said for any setup but the plain default, a GPU-only one too, as the mod's", async () => {
  expect(routesSaid(undefined)).toBe(false)
  expect(routesSaid('{}')).toBe(false)
  expect(routesSaid(JSON.stringify({ roles: { trivial: { via: 'gpu', gpu: 'auto', thinking: 'off' } } }))).toBe(false)
  expect(routesSaid(JSON.stringify({ roles: { trivial: { thinking: 'on' } } }))).toBe(true)
  expect(routesSaid(JSON.stringify({ day_usd: 2 }))).toBe(true)
  expect(routesSaid('{not json')).toBe(true)
  const w = world({}, { route: 'trivial  GPU g/pinned' })
  w.files['/h/.config/workbench/routes.json'] = JSON.stringify({ roles: { trivial: { gpu: 'g/pinned' } } })
  const { host, calls } = entriesHost()
  const u = new Upkeep(core, host, w.io, HOME)
  await u.context({ sessionID: 's1', agent: 'workbench', system: [] })
  expect(calls.includes("put workbench-routes: Roles (the user's, `wb route`):")).toBe(true)
})

test("a session's open tasks are those wb start recorded for it, active or waiting on it, never a bench replay", () => {
  const w = world({})
  task(w.files, 'a', RUNNING)
  task(w.files, 'b', WAITING)
  task(w.files, 'c', CLOSED)
  task(w.files, 'd', RUNNING, 's2')
  w.files[`${HOME}/tasks/e/status.json`] = JSON.stringify({ phase: RUNNING })
  w.files[`${HOME}/tasks/e/task.json`] = JSON.stringify({ name: 'e', session: 's1', bench: true })
  expect(openTasks(core, w.io, HOME, 's1').map(t => t.name)).toEqual(['a', 'b'])
})

test('a task that records no session is the open task of a session near it by directory, never one elsewhere or in ~ (#1493)', () => {
  // a `wb start` typed in a terminal, or a scheduled flow's: notices.ts wakes the session in its
  // repo for it, and the session's context names it (wb status as the session, in its directory)
  const w = world({})
  task(w.files, 'mine', RUNNING)
  w.files[`${HOME}/tasks/t9/status.json`] = JSON.stringify({ phase: WAITING })
  w.files[`${HOME}/tasks/t9/task.json`] = JSON.stringify({ name: 't9', cwd: '/src/app', repo: '/src/app' })
  const at = (dir: string) => openTasks(core, w.io, HOME, 's1', { dir, home: '/home/u', fold: false }).map(t => t.name)
  expect(at('/src/app/web')).toEqual(['mine', 't9'])
  expect(at('/src/other')).toEqual(['mine'])
  expect(at('/home/u')).toEqual(['mine'])
  expect(openTasks(core, w.io, HOME, 's1').map(t => t.name)).toEqual(['mine'])
})

test('the setup takes the kernel from the membrane and registers the prompt, context and compaction hooks', async () => {
  const hooks: string[] = []
  const stop = await upkeep({
    options: {},
    session: { hook: async (name: string) => { hooks.push(name); return { dispose: async () => {} } } },
    event: { subscribe: async function* () {} },
  } as unknown as UpkeepHost)
  expect(hooks).toEqual(['prompt', 'context', 'compaction'])
  await stop!()
  // a server without the session hooks or the event stream: nothing
  expect(await upkeep({ options: {} } as unknown as UpkeepHost)).toBeUndefined()
})
