import { expect, test } from 'bun:test'
import { existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { callFile, callRows, calls, Declines, denied, rowText, tasksNamed } from '../calls.ts'

// The OpenCode driver's own calls in a task's worktree, kept in rules/<task>/claude-calls/ for wb
// accept (#1331, 2026-11-25), under OpenCode's own Bun.

const same = (x: string) => x

test('an edit, a write and a patch in a task\'s worktree are its rows, by OpenCode\'s tool names', () => {
  const home = '/s'
  const ok = (p: string) => p === '/s/tasks/t1/task.json' || p === '/s/tasks/t2/task.json'
  expect(callRows('edit', { path: '/s/tasks/t1/worktree/a.py' }, '/repo', home, '/u', same, ok))
    .toEqual({ tasks: ['t1'], row: { tool: 'edit', path: '/s/tasks/t1/worktree/a.py' } })
  // a relative path is the session's directory's
  expect(callRows('write', { path: 'b.py', content: '' }, '/s/tasks/t2/worktree', home, '/u', same, ok))
    .toEqual({ tasks: ['t2'], row: { tool: 'write', path: '/s/tasks/t2/worktree/b.py' } })
  const patch = '*** Begin Patch\n*** Update File: /s/tasks/t1/worktree/a.py\n@@\n-x\n+y\n*** Add File: /s/tasks/t2/worktree/n.py\n+z\n*** End Patch'
  expect(callRows('patch', { patchText: patch }, '/repo', home, '/u', same, ok)).toEqual({ tasks: ['t1', 't2'],
    row: { tool: 'patch', paths: ['/s/tasks/t1/worktree/a.py', '/s/tasks/t2/worktree/n.py'] } })
  expect(callRows('edit', { path: '/repo/a.py' }, '/repo', home, '/u', same, ok).tasks).toEqual([])
  expect(callRows('edit', { path: '/s/tasks/gone/worktree/a.py' }, '/repo', home, '/u', same, ok).tasks).toEqual([])   // no task.json
  expect(callRows('read', { path: '/s/tasks/t1/worktree/a.py' }, '/repo', home, '/u', same, ok).tasks).toEqual([])
})

test('a shell command that runs in a task\'s directory or names it is its row, with its cwd', () => {
  const ok = () => true
  expect(callRows('shell', { command: 'npm test' }, '/s/tasks/t1/worktree', '/s', '/u', same, ok))
    .toEqual({ tasks: ['t1'], row: { tool: 'shell', command: 'npm test', cwd: '/s/tasks/t1/worktree' } })
  expect(callRows('shell', { command: 'make', workdir: '/s/tasks/t2/worktree/sub' }, '/repo', '/s', '/u', same, ok).tasks).toEqual(['t2'])
  expect(tasksNamed('cd $WB_HOME/tasks/t3/worktree && make', '/repo', ['/s'], ['/u'])).toEqual(['t3'])
  expect(tasksNamed('ls ~/.local/state/workbench/tasks/t4/', '/repo', ['/u/.local/state/workbench'], ['/u'])).toEqual(['t4'])
  expect(callRows('shell', { command: 'ls' }, '/repo', '/s', '/u', same, ok).tasks).toEqual([])
})

test('a row is written begun, then over, or denied when a permission refused the call', () => {
  expect(callFile(1234.6, 'call_ab/c')).toBe('1235-call_abc.json')
  expect(JSON.parse(rowText({ tool: 'edit', path: 'p' }, 2000, 'begun', 9000))).toEqual({ tool: 'edit', path: 'p', at: 2, end: null })
  expect(JSON.parse(rowText({ tool: 'edit', path: 'p' }, 2000, 'denied', 9000))).toEqual({ tool: 'edit', path: 'p', at: 2, end: 9, denied: true })
  expect(denied({ _tag: 'Tool.Error', error: { _tag: 'Permission.BlockedError' } })).toBe(true)
  expect(denied({ _tag: 'Tool.Error', error: { _tag: 'FileSystem.Error' } })).toBe(false)
})

test('the server part\'s hooks keep a driver\'s worktree edit in the task\'s claude-calls/', async () => {
  const home = mkdtempSync(join(tmpdir(), 'wb-calls-'))
  try {
    mkdirSync(join(home, 'tasks', 't1', 'worktree'), { recursive: true })
    writeFileSync(join(home, 'tasks', 't1', 'task.json'), '{}')
    const hooks: Record<string, (e: Record<string, unknown>) => unknown> = {}
    const ctx = {
      options: { env: { WB_HOME: home } },
      session: { get: async () => ({ location: { directory: '/repo' } }) },
      tool: { hook: async (name: string, cb: (e: Record<string, unknown>) => unknown) => { hooks[name] = cb } },
    }
    await calls(ctx as never)
    const file = join(home, 'tasks', 't1', 'worktree', 'a.py')
    await hooks['execute.before']!({ tool: 'edit', sessionID: 's', id: 'call_1', input: { path: file } })
    const dir = join(home, 'rules', 't1', 'claude-calls')
    const [name] = readdirSync(dir)
    const row = () => JSON.parse(readFileSync(join(dir, name!), 'utf8'))
    expect([row().tool, row().path, row().end]).toEqual(['edit', file, null])
    await hooks['execute.after']!({ tool: 'edit', sessionID: 's', id: 'call_1', status: 'completed' })
    expect(typeof row().end).toBe('number')
    await hooks['execute.before']!({ tool: 'edit', sessionID: 's', id: 'call_2', input: { path: '/repo/x.py' } })
    expect(existsSync(join(home, 'rules', 't1', 'claude-calls', name!))).toBe(true)
    expect((readdirSync(dir)).length).toBe(1)        // a call elsewhere: no row
  } finally {
    rmSync(home, { recursive: true, force: true })
  }
})

test('a declined permission ends its call denied, though OpenCode fires no execute.after for it', () => {
  const d = new Declines()
  expect(d.event({ type: 'permission.asked', data: { id: 'per_1', source: { type: 'tool', messageID: 'm', id: 'call_1' } } })).toBeUndefined()
  expect(d.event({ type: 'permission.asked', data: { id: 'per_2', source: { type: 'tool', messageID: 'm', id: 'call_2' } } })).toBeUndefined()
  expect(d.event({ type: 'permission.replied', data: { sessionID: 's', requestID: 'per_1', reply: 'reject' } })).toBe('call_1')
  expect(d.event({ type: 'permission.replied', data: { sessionID: 's', requestID: 'per_2', reply: 'once' } })).toBeUndefined()
  expect(d.event({ type: 'permission.replied', data: { sessionID: 's', requestID: 'per_9', reply: 'reject' } })).toBeUndefined()
})

test('the server part writes a declined edit\'s row denied from the bus, not left begun', async () => {
  const home = mkdtempSync(join(tmpdir(), 'wb-calls-'))
  try {
    mkdirSync(join(home, 'tasks', 't1', 'worktree'), { recursive: true })
    writeFileSync(join(home, 'tasks', 't1', 'task.json'), '{}')
    const hooks: Record<string, (e: Record<string, unknown>) => unknown> = {}
    const queue: { type: string; data?: Record<string, unknown> }[] = []
    let wake: (() => void) | undefined
    const push = (e: { type: string; data?: Record<string, unknown> }) => { queue.push(e); wake?.() }
    const ctx = {
      options: { env: { WB_HOME: home } },
      session: { get: async () => ({ location: { directory: '/repo' } }) },
      tool: { hook: async (name: string, cb: (e: Record<string, unknown>) => unknown) => { hooks[name] = cb } },
      event: {
        subscribe: () => ({
          [Symbol.asyncIterator]: () => ({
            next: async () => {
              while (!queue.length) await new Promise<void>(r => { wake = r })
              return { done: false, value: queue.shift()! }
            },
            return: async () => ({ done: true, value: undefined }),
          }),
        }),
      },
    }
    const stop = await calls(ctx as never)
    const file = join(home, 'tasks', 't1', 'worktree', 'a.py')
    await hooks['execute.before']!({ tool: 'edit', sessionID: 's', id: 'call_1', input: { path: file } })
    const dir = join(home, 'rules', 't1', 'claude-calls')
    const [name] = readdirSync(dir)
    const row = () => JSON.parse(readFileSync(join(dir, name!), 'utf8'))
    expect(row().end).toBe(null)
    push({ type: 'permission.asked', data: { id: 'per_1', sessionID: 's', source: { type: 'tool', messageID: 'm', id: 'call_1' } } })
    push({ type: 'permission.replied', data: { sessionID: 's', requestID: 'per_1', reply: 'reject' } })
    for (let i = 0; i < 50 && row().denied !== true; i++) await new Promise<void>(r => setTimeout(() => r(), 5))
    expect([row().denied, typeof row().end]).toEqual([true, 'number'])
    push({ type: 'permission.asked', data: {} })   // the loop still turns; nothing to end
    await stop?.()
  } finally {
    rmSync(home, { recursive: true, force: true })
  }
})
