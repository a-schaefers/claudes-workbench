import { expect, test } from 'bun:test'
import {
  childImport, childMessages, DENY_ALL, markOf, sessionsOf, startChildren, titleOf, WorkbenchChildren, type Exported,
} from '../children.ts'
import { Children, importerOf, TRIES, type ChildClient } from '../children-tui.ts'
import type { PaneTask } from '../pane.ts'

// #1331: a task's agent sessions as child sessions of its lead, at the lead's location (the
// host-shape decision on #1296, critics 1 and 2): the membrane's info, only the export's messages.

const LEAD = { id: 'ses_lead', projectID: 'prj_1', location: { directory: '/home/u/repo' } }
const msg = (id: string, type: string, created = 1000, extra: object = {}) => ({ id, type, time: { created }, ...extra })
// an export as the agent's own database could hold it: its info is the agent's to write
const EXPORT: Exported = {
  session: 'ses_agent1', current: true,
  messages: [msg('msg_1', 'user', 1700, { text: 'the spec' }), msg('msg_2', 'location-switched', 1800,
    { location: { directory: '/state/tasks/t-one/worktree' } }), msg('msg_3', 'assistant', 1900, { content: [] }),
  'not a message', { id: 'bad', type: 'user' }, { id: 'msg_4' }],
}

test('the child is built by the membrane: the lead\'s parent and location, every tool denied, the export only its messages', () => {
  const got = childImport(LEAD, 't-one', EXPORT, 0, 1, 5000)
  expect(got.info.id).toBe('ses_agent1')          // the agent session's own id: a second import conflicts
  expect(got.info.parentID).toBe('ses_lead')
  expect(got.info.projectID).toBe('prj_1')
  expect(got.info.location).toEqual({ directory: '/home/u/repo' })
  expect(got.location).toEqual({ directory: '/home/u/repo' })   // never the worktree (critic 1)
  expect(got.info.permissions).toEqual([{ action: '*', resource: '*', effect: 'deny' }])
  expect(got.info.metadata).toEqual({ workbench: { task: 't-one', session: 'ses_agent1', messages: 2 } })
  expect(got.info.title).toBe('Workbench agent: t-one')
  expect(got.info.cost).toBe(0)                    // the agent's spend is wb's, never the driver's
  expect(got.info.time).toEqual({ created: 1700, updated: 5000 })
  // the messages less a location switch, a non-object, a non-message id and a typeless one
  expect(got.messages.map(m => m.id)).toEqual(['msg_1', 'msg_3'])
  expect(JSON.stringify(got).includes('worktree')).toBe(false)
  expect(DENY_ALL[0]?.effect).toBe('deny')
})

test('a task of more than one session titles each by its place, and never as the agent\'s own `wb: <task>`', () => {
  expect(titleOf('t-one', 1, 3)).toBe('Workbench agent: t-one (session 2 of 3)')
  expect(titleOf('t-one', 0, 1).startsWith('wb: ')).toBe(false)   // transcript.find_session's title
})

test('wb watch --export\'s answer is read, and its failure said', () => {
  const ok = sessionsOf('t-one', { code: 0, stderr: '', stdout: JSON.stringify({ task: 't-one', sessions: [
    { session: 'ses_a', current: false, messages: [msg('msg_1', 'user')] }, { session: 'nope', messages: [] },
    { session: 'ses_b', messages: 'x' }] }) })
  expect(ok).toEqual({ task: 't-one', missing: [], sessions: [{ session: 'ses_a', current: false, messages: [msg('msg_1', 'user')] }] })
  const part = sessionsOf('t-one', { code: 0, stderr: '', stdout: '{"sessions": [], "missing": ["ses_busy", 3]}' })
  expect(part.missing).toEqual(['ses_busy'])
  expect(sessionsOf('t-one', { code: 2, stdout: '', stderr: 'wb: no task t-one\n' }).why).toBe('wb: no task t-one')
  expect(!!sessionsOf('t-one', { code: 0, stdout: 'garbage', stderr: '' }).why).toBe(true)
})

test('the server part\'s RPC runs wb watch --export for a task\'s name, never for an option', async () => {
  const ran: string[][] = []
  let handlers: Record<string, (i: unknown) => Promise<unknown>> = {}
  const stop = await startChildren({
    options: {},
    rpc: { register: async (def, h) => { expect(def.id).toBe(WorkbenchChildren.id); handlers = h as never; return { dispose: async () => {} } } },
  }, async args => { ran.push([...args]); return { code: 0, stdout: '{"sessions": []}', stderr: '' } })
  expect(await handlers.sessions!({ task: 't-one' })).toEqual({ task: 't-one', missing: [], sessions: [] })
  expect(!!((await handlers.sessions!({ task: '--all' })) as { why?: string }).why).toBe(true)
  expect(ran).toEqual([['watch', 't-one', '--export']])
  await stop()
})

test('only a session Workbench imported under that lead for that task is its mark', () => {
  const meta = { workbench: { task: 't-one', session: 'ses_agent1', messages: 2 } }
  expect(markOf({ parentID: 'ses_lead', metadata: meta }, 'ses_lead', 't-one')?.messages).toBe(2)
  expect(markOf({ parentID: 'ses_other', metadata: meta }, 'ses_lead', 't-one')).toBeUndefined()
  expect(markOf({ parentID: 'ses_lead', metadata: meta }, 'ses_lead', 't-two')).toBeUndefined()
  expect(markOf({ parentID: 'ses_lead' }, 'ses_lead', 't-one')).toBeUndefined()   // the agent's own, in this store
})

test('the experimental import is taken first, the deprecated alias next, and none means no import at all', async () => {
  const calls: string[] = []
  const exp = { import: async () => { calls.push('experimental') } }
  const old = { get: async () => ({}), import: async () => { calls.push('session') } }
  await importerOf({ experimental: { session: exp }, session: old } as never)!({} as never)
  await importerOf({ session: old } as never)!({} as never)
  expect(calls).toEqual(['experimental', 'session'])
  expect(importerOf({ session: { get: async () => ({}) } } as never)).toBeUndefined()
  expect(importerOf(undefined)).toBeUndefined()
})

// A server of sessions, as the client sees it: get, remove and import, each call recorded
function fakeServer(sessions: Record<string, object>) {
  const log: string[] = []
  const client: ChildClient = {
    session: {
      get: async ({ sessionID }) => {
        log.push(`get ${sessionID}`)
        const s = sessions[sessionID]
        if (!s) throw new Error('404')
        return s
      },
      remove: async ({ sessionID }) => { log.push(`remove ${sessionID}`); delete sessions[sessionID] },
    },
  }
  const importer = async (input: ReturnType<typeof childImport>) => {
    log.push(`import ${input.info.id} ${input.messages.length}`)
    if (sessions[input.info.id]) throw new Error('409')
    sessions[input.info.id] = input.info
  }
  return { client, importer, log, sessions }
}
const task = (over: Partial<PaneTask> = {}): PaneTask => ({
  name: 't-one', phase: 'review', round: 1, model: '', label: '', via: 'gpu', role: '', provider: '', tests: null,
  last: '', createdAt: 0, updatedAt: 1, recent: [], parked: false, session: 'ses_lead', driver: 'opencode',
  cwd: '', repo: '', subdir: '', ...over,
} as PaneTask)

test('a stopped task\'s sessions are imported once, a running one\'s never, and a longer session replaces its copy', async () => {
  const lead = { id: 'ses_lead', projectID: 'prj_1', location: { directory: '/home/u/repo' } }
  const srv = fakeServer({ ses_lead: lead })
  let msgs = [msg('msg_1', 'user'), msg('msg_2', 'assistant')]
  const asked: string[] = []
  const c = new Children(srv.client, srv.importer as never, async name => {
    asked.push(name)
    return { task: name, sessions: [{ session: 'ses_old', current: false, messages: [msg('msg_0', 'user')] },
      { session: 'ses_agent1', current: true, messages: msgs }] }
  }, () => 9000)
  await c.sync([task({ phase: 'running' })])
  expect(asked).toEqual([])                      // the panel's tail while it runs
  await c.sync([task()])
  expect(srv.log.filter(l => l.startsWith('import'))).toEqual(['import ses_old 1', 'import ses_agent1 2'])
  const child = srv.sessions.ses_agent1 as { parentID: string; title: string }
  expect([child.parentID, child.title]).toEqual(['ses_lead', 'Workbench agent: t-one (session 2 of 2)'])
  await c.sync([task()])                          // the same stop: not looked at again
  expect(asked).toEqual(['t-one'])
  // feedback: it runs again in the same session, and stops with more of it
  await c.sync([task({ phase: 'running', updatedAt: 2 })])
  msgs = [...msgs, msg('msg_5', 'user'), msg('msg_6', 'assistant')]
  srv.log.length = 0
  await c.sync([task({ round: 2, updatedAt: 3 })])
  expect(srv.log.filter(l => !l.startsWith('get'))).toEqual(['remove ses_agent1', 'import ses_agent1 4'])
  expect((srv.sessions.ses_agent1 as { metadata: { workbench: { messages: number } } }).metadata.workbench.messages).toBe(4)
})

test('a lead this server lacks, a driver that isn\'t OpenCode and a session that isn\'t Workbench\'s are left alone', async () => {
  const own = { parentID: undefined, metadata: {} }  // the agent's session in this store (a task from before v1.91.2)
  const srv = fakeServer({ ses_lead: LEAD, ses_agent1: own })
  const asked: string[] = []
  const c = new Children(srv.client, srv.importer as never, async name => {
    asked.push(name)
    return { task: name, sessions: [{ session: 'ses_agent1', current: true, messages: [msg('msg_1', 'user')] }] }
  })
  await c.sync([task({ name: 't-gone', session: 'ses_elsewhere' }), task({ name: 't-cc', driver: 'claude' }),
    task({ name: 't-none', session: '' }), task()])
  expect(asked).toEqual(['t-one'])
  expect(srv.log.filter(l => !l.startsWith('get'))).toEqual([])
  expect(srv.sessions.ses_agent1).toBe(own)
})

test('wb failing to give the sessions is looked at again, a few times at most until the task\'s status moves', async () => {
  const srv = fakeServer({ ses_lead: LEAD })
  let asked = 0
  const c = new Children(srv.client, srv.importer as never, async name => { asked++; return { task: name, sessions: [], why: 'busy' } })
  for (let i = 0; i < TRIES + 2; i++) await c.sync([task()])
  expect(asked).toBe(TRIES)
  await c.sync([task({ updatedAt: 2 })])
  expect(asked).toBe(TRIES + 1)
})

test('a session wb couldn\'t export is asked for again, beside the ones imported', async () => {
  const srv = fakeServer({ ses_lead: LEAD })
  let asked = 0
  const c = new Children(srv.client, srv.importer as never, async name => {
    asked++
    return { task: name, missing: asked < 2 ? ['ses_busy'] : [],
      sessions: [{ session: 'ses_agent1', current: true, messages: [msg('msg_1', 'user')] }] }
  })
  await c.sync([task()])
  await c.sync([task()])
  await c.sync([task()])
  expect(asked).toBe(2)            // the second look had them all
  expect(srv.log.filter(l => l.startsWith('import'))).toEqual(['import ses_agent1 1'])   // there already the second time
})
