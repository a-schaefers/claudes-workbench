import { expect, mock, test } from 'claude-code/testing'
import { awayNow, delegates,priceOf, savedLine, spark, statsLines, wrapWords } from './register.js'

const PANE = {
  plugin: 'workbench',
  component: 'Pane',
  requestId: 'workbench',
  props: {
    title: 'Workbench',
    isFocused: false,
    bodyColumns: 50,
    placement: 'dock',
    scroll: { offset: 0, bodyRows: 20 },
    view: {},
  },
} as const

test('with no tasks the pane says how to start one, on every surface', async ($, on) => {
  on('clock.now', () => ({ value: 1_800_000_000_000 }))
  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({ ...PANE, surface })
    expect((await ui.find({ type: 'Text', text: /No delegated tasks/ }))?.text)
      .toBe('No delegated tasks.')
    await ui.unmount()
  }
})

test('/workbench run again brings the pane back; alone, it is all it does', async ($, on) => {
  let opened = ''
  let closed = ''
  let held: any[] = []
  on('command.register', (_: any, e: any) => ({ value: { command: e.name } }))
  on('ui.panes', () => ({ value: held }))
  on('ui.close', (_, e) => {
    closed = e.id
    held = []
    return { value: undefined }
  })
  on('ui.open', (_, e) => {
    opened = e.id
    return { value: { isPlaced: true } }
  })
  on('env.get', () => ({ value: '/nonexistent' }))
  on('clock.now', () => ({ value: 1_800_000_000_000 }))
  on('clock.every', () => ({ value: { cancel: () => {} } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('skill.prompt', (_: any, e: any) => ({ text: e.text }))
  on('fs.exists', () => ({ value: false }))                      // the mode's poll: no state yet
  on('process.run', () => ({ value: { exitCode: 0, stdout: '', stderr: '' } }))
  const skill = (task: string) => $.skill.prompt({ skill: 'workbench', frontmatter: {}, source: 'userSettings',
    text: `- **First task:** \`${task}\`. Empty? Reply in one line that workbench mode is on.` })

  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  expect((await skill('')).text).toContain('First task')         // the skill, the first time
  expect([closed, opened]).toEqual(['', 'workbench'])
  // closed by accident, the engine still holding it out of sight: /workbench brings it back,
  // and Claude isn't handed the whole skill again to say so (2026-11-17: was /delegate-pane)
  held = [{ id: 'workbench', title: 'Workbench', isShown: false, isFocused: false, isPlaced: true }]
  opened = ''
  expect((await skill('')).text).toContain('The user ran /workbench again')
  expect([closed, opened]).toEqual(['workbench', 'workbench'])
  // with a task, the skill comes as usual
  expect((await skill('add a high-score screen')).text).toContain('add a high-score screen')
})

test('a Claude Code older than the one the mod is tested on is told so once, and the mod still runs', async ($, on) => {
  // the 2026-11-19 audit: installs track main, so an older Claude Code met calls it lacks as bare errors
  const clock = mock.clock(on, { now: 1_800_000_000_000 })
  const files: Record<string, string> = {}
  task(files, 'fix-it', 'review', { repo: '/home/u/a', cwd: '/home/u/a' })
  notice(files, { ts: ts0, task: 'fix-it', kind: 'review', text: 'REPORT-fix-it' })
  fakeTasks(on, files)
  on('session.id', () => ({ value: 'sess-new' }))
  on('process.run', () => ({ value: { exitCode: 0, stdout: '', stderr: '' } }))
  const toasts: string[] = []
  on('ui.toast', (_: any, e: any) => { toasts.push(e.text); return { value: undefined } })
  const woken: string[] = []
  on('prompt.submit', (_: any, e: any) => { woken.push(e.text); return { text: e.text } })
  try {
    engine.version = { version: '2.1.290', base: '2.1.290' }
    await $.session.start({ cwd: '/home/u/a', surface: 'terminal', isInteractive: true })
    await clock.advance(3000)
    expect(toasts.filter(t => t.includes('is older than'))).toEqual([
      'workbench: this Claude Code (2.1.290) is older than 2.1.311, which the pane and wake-ups are tested on: `claude update` if they misbehave'])
    expect(woken.filter(t => t.includes('REPORT-fix-it'))).toHaveLength(1)   // it carries on
    engine.version = undefined
    await $.session.start({ cwd: '/home/u/a', surface: 'terminal', isInteractive: true })
    expect(toasts.filter(t => t.includes('is older than'))).toHaveLength(1)  // once per load
  } finally {
    engine.version = TESTED
  }
})

test('a Claude Code older than $.session.version itself is told so too', async ($, on) => {
  const clock = mock.clock(on, { now: 1_800_000_000_000 })
  fakeTasks(on, {}, [])
  on('process.run', () => ({ value: { exitCode: 0, stdout: '', stderr: '' } }))
  const toasts: string[] = []
  on('ui.toast', (_: any, e: any) => { toasts.push(e.text); return { value: undefined } })
  try {
    engine.version = undefined
    await $.session.start({ cwd: '/home/u/a', surface: 'terminal', isInteractive: true })
    await clock.advance(3000)
  } finally {
    engine.version = TESTED
  }
  expect(toasts).toContain('workbench: this Claude Code is older than 2.1.311, which the pane and wake-ups are tested on: `claude update` if they misbehave')
})

test('the Claude Code the mod is tested on, or a newer one, hears nothing about versions', async ($, on) => {
  const clock = mock.clock(on, { now: 1_800_000_000_000 })
  fakeTasks(on, {}, [])
  on('process.run', () => ({ value: { exitCode: 0, stdout: '', stderr: '' } }))
  const toasts: string[] = []
  on('ui.toast', (_: any, e: any) => { toasts.push(e.text); return { value: undefined } })
  try {
    for (const base of ['2.1.311', '2.1.400-dev', '2.2.0', '3.0.1']) {
      engine.version = { version: base, base }
      await $.session.start({ cwd: '/home/u/a', surface: 'terminal', isInteractive: true })
    }
    await clock.advance(3000)
  } finally {
    engine.version = TESTED
  }
  expect(toasts.join('\n')).not.toContain('older than')
})

test('a copy updated from /delegate gets its /workbench link from wb setup', async ($, on) => {
  const ran: string[][] = []
  const toasts: string[] = []
  on('command.register', (_: any, e: any) => ({ value: { command: e.name } }))
  on('env.get', (_: any, e: any) => ({ value: ({ HOME: '/home/u', WB_HOME: '/wb' } as any)[e.name] }))
  on('clock.now', () => ({ value: 1_800_000_000_000 }))
  on('clock.every', () => ({ value: { cancel: () => {} } }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('fs.stat', (_: any, e: any) => {
    if (e.path !== '/home/u/.claude/skills/delegate') throw new Error(`ENOENT: ${e.path}`)
    return { value: { kind: 'other', size: 0, mtimeMs: 0, isLink: true } }   // the old link, leading nowhere
  })
  on('process.run', (_: any, e: any) => {
    ran.push(e.argv)
    return { value: { exitCode: 0, stdout: '', stderr: '' } }
  })
  on('ui.toast', (_: any, e: any) => {
    toasts.push(e.text)
    return { value: undefined }
  })
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: false })
  expect(ran).toContainEqual(['wb', 'setup'])
  expect(toasts.join('\n')).toContain('/delegate is now /workbench')
})

// A file system in memory: what `wb` and its runner would have written. `ui` records the pane
// opening and closing and each status line.
// The web chat as the user turned it on (`wb chat on`): off by default (the user, 2026-11-19)
function chatOn(files: Record<string, string>) {
  files['/wb/chat/switch.json'] = JSON.stringify({ on: true })
  return files
}

// The Claude Code fakeTasks plays: the one the mod is tested on, unless a test says otherwise
// (undefined: one older than $.session.version itself)
const engine: { version?: { version: string; base: string } } = {}
const TESTED = { version: '2.1.311', base: '2.1.311' }
engine.version = TESTED

function fakeTasks(on: any, files: Record<string, string>, dirs = ['fix-it'], scouted = true, ui: string[] = [], ears = true) {
  on('session.version', () => {
    if (!engine.version) throw new Error('no implementation for session.version')
    return { value: engine.version }
  })
  // the weekly model check was done just now, unless the test is about it
  if (scouted) files['/wb/scout.json'] = JSON.stringify({ done_at: 1_800_000_000 })
  on('env.get', (_: any, e: any) => ({ value: ({ WB_HOME: '/wb', HOME: '/home/u' } as any)[e.name] }))
  const reads: string[] = []
  on('fs.read', (_: any, e: any) => {
    reads.push(e.path)
    if (!(e.path in files)) throw new Error(`ENOENT: ${e.path}`)
    return { value: files[e.path] }
  })
  on('fs.write', (_: any, e: any) => {
    files[e.path] = e.text
    return { value: undefined }
  })
  on('fs.exists', (_: any, e: any) => ({ value: Object.keys(files).some(p => p.startsWith(e.path)) }))
  on('fs.stat', (_: any, e: any) => {
    if (!(e.path in files)) throw new Error(`ENOENT: ${e.path}`)
    return { value: { kind: 'file', size: files[e.path].length, mtimeMs: 0, isLink: false } }
  })
  on('fs.list', () => ({ value: dirs.map(name => ({ name, kind: 'dir' })) }))
  on('command.register', (_: any, e: any) => ({ value: { command: e.name } }))
  // the pane is shown from its open until its close
  let shown = false
  on('ui.panes', () => ({ value: shown ? [{ id: 'workbench', title: 'Workbench', isShown: true, isFocused: false, isPlaced: true }] : [] }))
  on('ui.open', (_: any, e: any) => { ui.push(`open ${e.id}`); shown = true; return { value: { isPlaced: true } } })
  on('ui.close', (_: any, e: any) => { ui.push(`close ${e.id}`); shown = false; return { value: undefined } })
  on('ui.status', (_: any, e: any) => { ui.push(`status ${e.text ?? ''}`); return { value: undefined } })
  // `wb events`: says hello and then stays open, as the real watcher does
  if (ears) on('process.spawn', async function* () {
    yield { stream: 'stdout', text: '{"hello": 4242}\n' }
    await new Promise(() => {})
    return { code: 0, signal: null }
  })
  on('session.start', (_: any, e: any) => ({ cwd: e.cwd }))
  on('turn.start', (_: any, e: any) => ({ turnId: e.turnId }))
  on('turn.complete', () => ({ text: '' }))
  return { show: (is: boolean) => { shown = is }, reads } // the person closing or opening the pane; what was read
}

test('a review notice waits for the turn to end, and is dropped if it went stale', async ($, on) => {
  const clock = mock.clock(on, { now: 1_800_000_000_000 })
  const ts = 1_800_000_000 // seconds, as the runner writes them
  const files: Record<string, string> = {
    '/wb/notify.jsonl': JSON.stringify({ ts, task: 'fix-it', kind: 'review', text: 'REPORT' }) + '\n',
    '/wb/tasks/fix-it/task.json': JSON.stringify({ name: 'fix-it', model: 'qwen27b', created_at: ts }),
    '/wb/tasks/fix-it/status.json': JSON.stringify({ phase: 'review', round: 1, updated_at: ts }),
  }
  fakeTasks(on, files)
  const woken: string[] = []
  const toasts: string[] = []
  on('prompt.submit', (_: any, e: any) => {
    woken.push(e.text)
    return { text: e.text }
  })
  on('ui.toast', (_: any, e: any) => {
    toasts.push(e.text)
    return { value: undefined }
  })

  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await $.turn.start({ text: 'busy with something else', turnId: 't1' })
  await clock.advance(3000)
  expect(toasts.length).toBe(1)        // seen at once
  expect(woken.length).toBe(0)         // but no wake while Claude is mid-turn
  expect(files['/wb/tasks/fix-it/acked']).toBe(undefined)

  await $.turn.complete({ answer: '', durationMs: 1, isAborted: false, turnId: 't1', reason: 'answer' })
  await clock.advance(3000)
  expect(woken.length).toBe(1)         // delivered once the turn ended
  expect(woken[0]).toContain('REPORT')
  await clock.advance(3000)
  expect(woken.length).toBe(1)         // and only once

  // A second notice the turn acted on (accepted) before it ended: no wake, just acknowledged.
  files['/wb/notify.jsonl'] += JSON.stringify({ ts: ts + 60, task: 'fix-it', kind: 'review', text: 'R2' }) + '\n'
  await $.turn.start({ text: '', turnId: 't2' })
  files['/wb/tasks/fix-it/status.json'] = JSON.stringify({ phase: 'accepted', round: 2, updated_at: ts + 90 })
  await $.turn.complete({ answer: '', durationMs: 1, isAborted: false, turnId: 't2', reason: 'answer' })
  await clock.advance(3000)
  expect(woken.length).toBe(1)
  expect(files['/wb/tasks/fix-it/acked']).toBe(String(ts + 60))
})

// A task's files as wb writes them: task.json, status.json, and its notices in notify.jsonl.
const ts0 = 1_800_000_000
function task(files: Record<string, string>, name: string, phase: string, extra: Record<string, any> = {}, updated = ts0) {
  files[`/wb/tasks/${name}/task.json`] = JSON.stringify({ name, model: 'm', created_at: ts0 - 60, ...extra })
  files[`/wb/tasks/${name}/status.json`] = JSON.stringify({ phase, round: 1, updated_at: updated })
}
const notice = (files: Record<string, string>, row: Record<string, any>) => {
  files['/wb/notify.jsonl'] = (files['/wb/notify.jsonl'] ?? '') + JSON.stringify(row) + '\n'
}
const endTurn = (turnId: string) => ({ answer: '', durationMs: 1, isAborted: false, turnId, reason: 'answer' } as const)

test('a session wakes for its own project\'s tasks only: another project\'s are left for their session', async ($, on) => {
  // the 2026-11-16 audit: two projects at once, each session was woken for the other's tasks
  const clock = mock.clock(on, { now: 1_800_000_000_000 })
  const files: Record<string, string> = {}
  task(files, 'ours', 'review', { repo: '/home/u/a', cwd: '/home/u/a' })
  task(files, 'sub', 'review', { repo: '/home/u/a/lib', cwd: '/home/u/a/lib' })   // inside this session's dir
  task(files, 'theirs', 'review', { repo: '/home/u/b', cwd: '/home/u/b' })
  // the 0.90 review: started from ~ (or a parent) for another repo, it was every session's below
  task(files, 'fromhome', 'review', { repo: '/home/u/b', cwd: '/home/u' })
  task(files, 'sibling', 'review', { repo: '/home/u/ab', cwd: '/home/u/ab' })     // not inside /home/u/a
  const names = ['ours', 'sub', 'theirs', 'fromhome', 'sibling']
  for (const name of names) notice(files, { ts: ts0, task: name, kind: 'review', text: `REPORT-${name}` })
  fakeTasks(on, files, names)
  const runs: string[][] = []
  on('process.run', (_: any, e: any) => { runs.push(e.argv); return { value: { exitCode: 0, stdout: 'ours  review', stderr: '' } } })
  on('ui.toast', () => ({ value: undefined }))
  on('prompt.context', (_: any, e: any) => ({ blocks: e.blocks }))
  const woken: string[] = []
  on('prompt.submit', (_: any, e: any) => { woken.push(e.text); return { text: e.text } })
  await $.session.start({ cwd: '/home/u/a', surface: 'terminal', isInteractive: true })
  await clock.advance(3000)
  expect(woken.filter(t => t.includes('REPORT-ours'))).toHaveLength(1)
  expect(woken.filter(t => t.includes('REPORT-sub'))).toHaveLength(1)
  expect(woken.join('\n')).not.toContain('theirs')
  expect(woken.join('\n')).not.toContain('fromhome')
  expect(woken.join('\n')).not.toContain('sibling')
  expect(files['/wb/tasks/theirs/acked']).toBe(undefined)        // its own session (or wb wait) still gets it
  expect(files['/wb/tasks/fromhome/acked']).toBe(undefined)
  await clock.advance(120_000)
  expect(woken.join('\n')).not.toContain('theirs')               // no rewake for it either
  // the open-tasks block asks wb for this session's tasks
  await $.prompt.context({ blocks: [] })
  expect(runs).toContainEqual(['wb', 'status', '--repo', '/home/u/a'])
})

test('a task wb start ran in this session is its own wherever it ran, also after a /clear', async ($, on) => {
  // the 0.90 review: `cd /tmp/scratch && wb start` made a task no session woke for, and a session
  // in ~ woke for every task
  const clock = mock.clock(on, { now: 1_800_000_000_000 })
  const files: Record<string, string> = {}
  task(files, 'scratch', 'review', { repo: '/tmp/scratch', cwd: '/tmp/scratch', session: 'sess-1' })
  task(files, 'other', 'review', { repo: '/home/u/a', cwd: '/home/u', session: 'sess-x' })
  task(files, 'byhand', 'review', { repo: '/home/u/a', cwd: '/home/u/a' })   // no session: by directory
  const names = ['scratch', 'other', 'byhand']
  for (const name of names) notice(files, { ts: ts0, task: name, kind: 'review', text: `REPORT-${name}` })
  fakeTasks(on, files, names)
  let id = 'sess-1'
  on('session.id', () => ({ value: id }))
  const runs: string[][] = []
  on('process.run', (_: any, e: any) => { runs.push(e.argv); return { value: { exitCode: 0, stdout: 'stopped: scratch', stderr: '' } } })
  on('ui.toast', () => ({ value: undefined }))
  const woken: string[] = []
  on('prompt.submit', (_: any, e: any) => { woken.push(e.text); return { text: e.text } })
  await $.session.start({ cwd: '/home/u', surface: 'terminal', isInteractive: true })   // a session in ~
  await clock.advance(3000)
  expect(woken.filter(t => t.includes('REPORT-scratch'))).toHaveLength(1)
  expect(woken.join('\n')).not.toContain('REPORT-other')       // another session's, under ~ too
  expect(woken.join('\n')).not.toContain('REPORT-byhand')      // ~ claims nothing by directory
  expect(files['/wb/tasks/other/acked']).toBe(undefined)
  expect(files['/wb/tasks/byhand/acked']).toBe(undefined)
  // /clear: the process goes on under another id, and the first one's tasks are still its own
  id = 'sess-2'
  task(files, 'scratch', 'review', { repo: '/tmp/scratch', cwd: '/tmp/scratch', session: 'sess-1' }, ts0 + 60)
  notice(files, { ts: ts0 + 60, task: 'scratch', kind: 'review', text: 'REPORT-again' })
  await clock.advance(31_000)                                  // the safety poll reads it
  expect(woken.filter(t => t.includes('REPORT-again'))).toHaveLength(1)
  await $.command.run({ command: 'workbench-quit', args: '' })
  expect(runs.at(-1)).toEqual(['wb', 'stop', '--all', '--repo', '/home/u', '--session', 'sess-1', '--session', 'sess-2'])
})

test('a new Claude Code process in the project adopts the tasks of one that is gone', async ($, on) => {
  // the 0.90 review: Claude Code exited (a crash, an update) and plain `claude` in the same repo
  // got a new session id: none of the earlier process's tasks woke it, and its quit skipped them
  const clock = mock.clock(on, { now: 1_800_000_000_000 })
  const files: Record<string, string> = {}
  task(files, 'orphan', 'review', { repo: '/home/u/a', cwd: '/home/u/a', session: 'sess-old', claude_pid: 4242 })
  task(files, 'live', 'review', { repo: '/home/u/a', cwd: '/home/u/a', session: 'sess-other', claude_pid: 4343 })
  task(files, 'far', 'review', { repo: '/home/u/b', cwd: '/home/u/b', session: 'sess-old', claude_pid: 4242 })
  const names = ['orphan', 'live', 'far']
  for (const name of names) notice(files, { ts: ts0, task: name, kind: 'review', text: `REPORT-${name}` })
  fakeTasks(on, files, names)
  on('session.id', () => ({ value: 'sess-new' }))
  const kills: string[] = []
  on('process.run', (_: any, e: any) => {
    if (e.argv[0] === 'kill') {
      kills.push(e.argv[2])
      return { value: { exitCode: e.argv[2] === '4242' ? 1 : 0, stdout: '', stderr: '' } }
    }
    return { value: { exitCode: 0, stdout: '', stderr: '' } }
  })
  on('ui.toast', () => ({ value: undefined }))
  const woken: string[] = []
  on('prompt.submit', (_: any, e: any) => { woken.push(e.text); return { text: e.text } })
  await $.session.start({ cwd: '/home/u/a', surface: 'terminal', isInteractive: true })
  await clock.advance(3000)
  expect(woken.filter(t => t.includes('REPORT-orphan'))).toHaveLength(1)  // its process is gone
  expect(woken.join('\n')).not.toContain('REPORT-live')       // a live session's stays its own
  expect(woken.join('\n')).not.toContain('REPORT-far')        // another project's: not by directory
  expect(files['/wb/tasks/live/acked']).toBe(undefined)
  const asked = kills.length
  await clock.advance(6000)
  expect(kills.filter(p => p === '4242')).toHaveLength(1)      // gone is final: asked once
  expect(kills.length - asked).toBeLessThanOrEqual(1)          // a live one, not at every poll
})

test('on a file system that ignores case, a task under ~/Repos is a session in ~/repos\'s', async ($, on) => {
  // the 2026-11-19 audit (finding 28): by the raw strings it was no one's
  const clock = mock.clock(on, { now: 1_800_000_000_000 })
  const files: Record<string, string> = {}
  task(files, 'cased', 'review', { repo: '/Users/u/Repos/a', cwd: '/Users/u/Repos/a' })
  notice(files, { ts: ts0, task: 'cased', kind: 'review', text: 'REPORT-cased' })
  fakeTasks(on, files, ['cased'])
  on('session.id', () => ({ value: 'sess-new' }))
  on('process.run', () => ({ value: { exitCode: 0, stdout: '', stderr: '' } }))
  on('ui.toast', () => ({ value: undefined }))
  const woken: string[] = []
  on('prompt.submit', (_: any, e: any) => { woken.push(e.text); return { text: e.text } })
  files['/uSERS/U/REPOS/A'] = ''  // the directory, every letter's case swapped: APFS finds it
  await $.session.start({ cwd: '/Users/u/repos/a', surface: 'terminal', isInteractive: true })
  await clock.advance(3000)
  expect(woken.filter(t => t.includes('REPORT-cased'))).toHaveLength(1)
})

test('where case counts, a task under another directory differing only in case is not this session\'s', async ($, on) => {
  const clock = mock.clock(on, { now: 1_800_000_000_000 })
  const files: Record<string, string> = {}
  task(files, 'cased', 'review', { repo: '/home/u/Repos/a', cwd: '/home/u/Repos/a' })
  notice(files, { ts: ts0, task: 'cased', kind: 'review', text: 'REPORT-cased' })
  fakeTasks(on, files, ['cased'])
  on('session.id', () => ({ value: 'sess-new' }))
  on('process.run', () => ({ value: { exitCode: 0, stdout: '', stderr: '' } }))
  on('ui.toast', () => ({ value: undefined }))
  const woken: string[] = []
  on('prompt.submit', (_: any, e: any) => { woken.push(e.text); return { text: e.text } })
  await $.session.start({ cwd: '/home/u/repos/a', surface: 'terminal', isInteractive: true })
  await clock.advance(3000)
  expect(woken.join('\n')).not.toContain('REPORT-cased')
})

test('a scripted session (claude -p, the SDK) takes no wake and runs no wb, until a person attaches', async ($, on) => {
  // the 2026-11-16 audit: a `claude -p` run took the terminal session's review wake
  const clock = mock.clock(on, { now: 1_800_000_000_000 })
  const files: Record<string, string> = {}
  task(files, 'fix-it', 'review')
  notice(files, { ts: ts0, task: 'fix-it', kind: 'review', text: 'REPORT' })
  fakeTasks(on, files, ['fix-it'], false)
  const runs: string[][] = []
  on('process.run', (_: any, e: any) => { runs.push(e.argv); return { value: { exitCode: 0, stdout: 'fix-it review', stderr: '' } } })
  on('ui.toast', () => ({ value: undefined }))
  on('prompt.context', (_: any, e: any) => ({ blocks: e.blocks }))
  on('session.attach', (_: any, e: any) => ({ clientId: e.clientId }))
  const woken: string[] = []
  on('prompt.submit', (_: any, e: any) => { woken.push(e.text); return { text: e.text } })
  await $.session.start({ cwd: '/', surface: null, isInteractive: false })
  await $.turn.start({ text: 'scripted', turnId: 't1' })
  await $.turn.complete(endTurn('t1'))
  const ctx = await $.prompt.context({ blocks: [] })
  await clock.advance(120_000)
  expect(woken).toEqual([])
  expect(files['/wb/tasks/fix-it/acked']).toBe(undefined)
  expect(files['/wb/scout.json']).toBe(undefined)               // nor the weekly check
  expect(runs).toEqual([])                                      // no wb status before each prompt
  expect(ctx.blocks.map((b: any) => b.name)).not.toContain('workbench')

  await $.session.attach({ surface: 'desktop', clientId: 'desktop:default' })
  await clock.advance(3000)
  expect(woken.filter(t => t.includes('REPORT'))).toHaveLength(1)
})

test('a wake the engine drops is not acknowledged, and comes again a minute later', async ($, on) => {
  // the 2026-11-16 audit: a settings hook blocked the wake, the notice was acked, and lost for good
  const clock = mock.clock(on, { now: 1_800_000_000_000 })
  const files: Record<string, string> = {}
  task(files, 'fix-it', 'review')
  notice(files, { ts: ts0, task: 'fix-it', kind: 'review', text: 'REPORT' })
  fakeTasks(on, files)
  on('process.run', () => ({ value: { exitCode: 0, stdout: '', stderr: '' } }))
  const toasts: string[] = []
  on('ui.toast', (_: any, e: any) => { toasts.push(e.text); return { value: undefined } })
  const tried: string[] = []
  on('prompt.submit', (_: any, e: any) => {
    tried.push(e.text)
    return tried.length === 1 ? { drop: 'blocked by a hook' } : { text: e.text }
  })
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.advance(3000)
  expect(tried).toHaveLength(1)
  expect(Number(files['/wb/tasks/fix-it/acked'] ?? 0)).toBeLessThan(ts0)   // not acknowledged
  expect(toasts.join('\n')).toContain('dropped (blocked by a hook)')
  await clock.advance(70_000)
  expect(tried).toHaveLength(2)                                 // the safety net tries again
  expect(tried[1]).toContain('Task `fix-it` is ready for review')
  await clock.advance(120_000)
  expect(tried).toHaveLength(2)                                 // once it went through
})

test('a notice already delivered (before a reload, or by wb wait) is not rewoken as lost', async ($, on) => {
  // the 2026-11-16 audit: after /reload-plugins, "no notice reached Claude" for a review it had
  const clock = mock.clock(on, { now: 1_800_000_000_000 })
  const files: Record<string, string> = {}
  task(files, 'fix-it', 'review', {}, ts0 + 0.05)               // the runner's last write lands after its notice
  notice(files, { ts: ts0, task: 'fix-it', kind: 'review', text: 'REPORT' })
  files['/wb/tasks/fix-it/acked'] = String(ts0)
  task(files, 'older', 'stuck', {}, ts0 - 7200 + 0.05)          // two hours ago: past the wake's hour
  notice(files, { ts: ts0 - 7200, task: 'older', kind: 'stuck', text: 'STUCK' })
  files['/wb/tasks/older/acked'] = String(ts0 - 7200)
  fakeTasks(on, files, ['fix-it', 'older'])
  on('process.run', () => ({ value: { exitCode: 0, stdout: '', stderr: '' } }))
  on('ui.toast', () => ({ value: undefined }))
  const woken: string[] = []
  on('prompt.submit', (_: any, e: any) => { woken.push(e.text); return { text: e.text } })
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.advance(120_000)
  expect(woken).toEqual([])
})

test('two wakes due in one turn both arrive, the second after the first one\'s turn', async ($, on) => {
  // the 2026-11-16 audit: the second report never came, only a rewake a minute later
  const clock = mock.clock(on, { now: 1_800_000_000_000 })
  const files: Record<string, string> = {}
  task(files, 'a', 'review')
  task(files, 'b', 'review')
  fakeTasks(on, files, ['a', 'b'])
  on('process.run', () => ({ value: { exitCode: 0, stdout: '', stderr: '' } }))
  on('ui.toast', () => ({ value: undefined }))
  const woken: string[] = []
  on('prompt.submit', (_: any, e: any) => {
    woken.push(e.text)
    if (woken.length === 1) void $.turn.start({ text: e.text, turnId: 'w1' }) // the engine starts it at once
    return { text: e.text }
  })
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await $.turn.start({ text: 'busy', turnId: 't1' })
  notice(files, { ts: ts0, task: 'a', kind: 'review', text: 'REPORT-A' })
  notice(files, { ts: ts0 + 1, task: 'b', kind: 'review', text: 'REPORT-B' })
  await clock.advance(3000)
  await $.turn.complete(endTurn('t1'))
  await clock.advance(3000)
  await $.turn.complete(endTurn('w1'))
  await clock.advance(3000)
  expect(woken.filter(t => t.includes('REPORT-A'))).toHaveLength(1)
  expect(woken.filter(t => t.includes('REPORT-B'))).toHaveLength(1)
  expect(woken.join('\n')).not.toContain('no notice reached Claude')
})

test('two questions asked at once both reach Claude, each with its own id', async ($, on) => {
  // the 2026-11-16 audit: the newest alone was delivered, and the first stayed blocked
  const clock = mock.clock(on, { now: 1_800_000_000_000 })
  const files: Record<string, string> = {}
  task(files, 'fix-it', 'asking')
  for (const id of ['q1', 'q2']) {
    files[`/wb/tasks/fix-it/ask/${id}.json`] = JSON.stringify({ id })
    notice(files, { ts: ts0 + (id === 'q1' ? 0 : 1), task: 'fix-it', kind: 'question', id, text: `Q: ${id}?` })
  }
  fakeTasks(on, files)
  on('process.run', () => ({ value: { exitCode: 0, stdout: '', stderr: '' } }))
  on('ui.toast', () => ({ value: undefined }))
  const woken: string[] = []
  on('prompt.submit', (_: any, e: any) => { woken.push(e.text); return { text: e.text } })
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.advance(3000)
  expect(woken.filter(t => t.includes('Q: q1?') && t.includes('wb answer fix-it --id q1'))).toHaveLength(1)
  expect(woken.filter(t => t.includes('Q: q2?') && t.includes('wb answer fix-it --id q2'))).toHaveLength(1)
})

test('a question whose task no longer asks is acknowledged without a wake', async ($, on) => {
  // the 0.90 review: its ask file had no answer, so a question that timed out woke Claude for it
  const clock = mock.clock(on, { now: 1_800_000_000_000 })
  const files: Record<string, string> = {}
  task(files, 'fix-it', 'running')
  files['/wb/tasks/fix-it/ask/q1.json'] = JSON.stringify({ id: 'q1' })
  notice(files, { ts: ts0, task: 'fix-it', kind: 'question', id: 'q1', text: 'Q: q1?' })
  fakeTasks(on, files)
  on('process.run', () => ({ value: { exitCode: 0, stdout: '', stderr: '' } }))
  on('ui.toast', () => ({ value: undefined }))
  const woken: string[] = []
  on('prompt.submit', (_: any, e: any) => { woken.push(e.text); return { text: e.text } })
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.advance(3000)
  expect(woken.filter(t => t.includes('Q: q1?'))).toHaveLength(0)
  expect(Number(files['/wb/tasks/fix-it/acked'])).toBe(ts0)
})

test('another plugin\'s tool call between turns does not hold the wakes', {
  plugins: [{
    name: 'peek',
    register(on) {
      on('session.start', async ($, e, next) => {
        await $.command.register({ name: 'peek', description: 'reads a file' })
        return next(e)
      })
      on('command.run', { command: 'peek' }, async $ => {
        await $.tool.call({ tool: 'Read', file_path: '/x' } as any).catch(() => {})
        return { text: '' }
      })
    },
  }],
}, async ($, on) => {
  // the 2026-11-16 audit: a plugin's $.tool.call marked Claude busy until a turn the user started
  const clock = mock.clock(on, { now: 1_800_000_000_000 })
  const files: Record<string, string> = {}
  task(files, 'fix-it', 'review')
  fakeTasks(on, files)
  on('process.run', () => ({ value: { exitCode: 0, stdout: '', stderr: '' } }))
  on('ui.toast', () => ({ value: undefined }))
  on('tool.call', () => ({ result: {}, text: '' }))
  const woken: string[] = []
  on('prompt.submit', (_: any, e: any) => { woken.push(e.text); return { text: e.text } })
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await $.command.run({ command: 'peek', args: '' })
  notice(files, { ts: ts0, task: 'fix-it', kind: 'review', text: 'REPORT' })
  await clock.advance(3000)
  expect(woken.filter(t => t.includes('REPORT'))).toHaveLength(1)
})

test('the weekly update check runs beside the wakes, not in front of them', async ($, on) => {
  // the 2026-11-16 audit: a stalled fetch held every wake for up to two minutes
  const clock = mock.clock(on, { now: 1_800_000_000_000 })
  const files: Record<string, string> = {}
  task(files, 'fix-it', 'review')
  fakeTasks(on, files)
  on('process.run', async (_: any, e: any) => {
    if (e.argv[1] === 'update') await new Promise(() => {})    // the network hangs
    return { value: { exitCode: 0, stdout: '', stderr: '' } }
  })
  on('ui.toast', () => ({ value: undefined }))
  const woken: string[] = []
  on('prompt.submit', (_: any, e: any) => { woken.push(e.text); return { text: e.text } })
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await $.turn.start({ text: 'hi', turnId: 't1' })
  notice(files, { ts: ts0, task: 'fix-it', kind: 'review', text: 'REPORT' })
  await $.turn.complete(endTurn('t1'))
  await clock.advance(3000)
  expect(woken.filter(t => t.includes('REPORT'))).toHaveLength(1)
})

test('a notify.jsonl past what one read takes is read from its end, and still wakes Claude', async ($, on) => {
  // the 2026-11-16 audit: past 4 MiB the read failed, deliver read '' and no wake came again
  const clock = mock.clock(on, { now: 1_800_000_000_000 })
  const files: Record<string, string> = {}
  task(files, 'fix-it', 'review')
  files['/wb/notify.jsonl'] = (JSON.stringify({ ts: ts0 - 86400, task: 'gone', kind: 'review', text: 'old' }) + '\n').repeat(30_000)
  notice(files, { ts: ts0, task: 'fix-it', kind: 'review', text: 'REPORT' })
  fakeTasks(on, files)
  const runs: string[][] = []
  on('process.run', (_: any, e: any) => {
    runs.push(e.argv)
    if (e.argv[0] === 'tail') return { value: { exitCode: 0, stdout: files[e.argv[3]].slice(-Number(e.argv[2])), stderr: '' } }
    return { value: { exitCode: 0, stdout: '', stderr: '' } }
  })
  on('ui.toast', () => ({ value: undefined }))
  const woken: string[] = []
  on('prompt.submit', (_: any, e: any) => { woken.push(e.text); return { text: e.text } })
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.advance(3000)
  expect(runs).toContainEqual(['tail', '-c', String(1 << 20), '/wb/notify.jsonl'])
  expect(woken.filter(t => t.includes('REPORT'))).toHaveLength(1)
})

test('outside workbench mode another session\'s running task opens no pane and fills no status line', async ($, on) => {
  // 2026-11-17: a session opened in another project showed the pane and the task's status line for
  // a task this session never started, and /workbench-quit couldn't clear it
  const clock = mock.clock(on, { now: 1_800_000_000_000 })
  const files: Record<string, string> = {}
  task(files, 'theirs', 'running', { repo: '/home/u/b', cwd: '/home/u/b' })
  const ui: string[] = []
  const dirs = ['theirs']
  fakeTasks(on, files, dirs, true, ui)
  await $.session.start({ cwd: '/home/u/a', surface: 'terminal', isInteractive: true })
  await clock.advance(3000)
  expect(ui).not.toContain('open workbench')
  expect(ui.filter(u => u.includes('theirs'))).toEqual([])
  // its own task does both
  task(files, 'ours', 'running', { repo: '/home/u/a', cwd: '/home/u/a' })
  dirs.push('ours')
  await clock.advance(6000)
  expect(ui).toContain('open workbench')
  expect(ui.filter(u => u.startsWith('status ')).at(-1)).toBe('status ⚒ wb · ours running r1')
})

test('the pane follows the running task: its whole history, then what it is doing now', async ($, on) => {
  const clock = mock.clock(on, { now: 1_800_000_000_000 })
  const ts = 1_800_000_000
  const files: Record<string, string> = {
    '/wb/tasks/fix-it/task.json': JSON.stringify({ name: 'fix-it', model: 'm', created_at: ts }),
    '/wb/tasks/fix-it/status.json': JSON.stringify({ phase: 'running', round: 1, updated_at: ts }),
  }
  fakeTasks(on, files)
  const runs: string[][] = []
  on('process.run', (_: any, e: any) => {
    runs.push(e.argv)
    if (e.argv[1] === 'chat' && e.argv[2] === 'url') {
      return { value: { exitCode: 0, stdout: 'https://box.tail.ts.net/#token=abc\n', stderr: '' } }
    }
    const lines = [
      { s: 'claude', t: 'Claude → agent · spec' },
      { s: 'think', t: '  ✻ thought for 2.0s' },
      { s: 'add', t: '    +42' },
      { s: 'meta', t: 'M · reasoning on, default effort' },
      { s: 'activity', t: 'Writing… 3.0s · ↓ 45 tokens · 41 tok/s' },
      { s: 'clock', t: '⏲ task 4m · ~5m left (typically 9m)' },
    ]
    return { value: { exitCode: 0, stdout: lines.map(l => JSON.stringify(l)).join('\n'), stderr: '' } }
  })

  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.advance(3000)
  const watches = () => runs.filter(r => r[1] === 'watch')
  expect(watches()[0]).toEqual(['wb', 'watch', 'fix-it', '--json', '--last', '2000'])

  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect(await ui.find({ type: 'Text', text: /── fix-it · ↑↓ scrolls/ })).toBeTruthy()
  expect(await ui.find({ type: 'Text', text: 'Claude → agent · spec' })).toBeTruthy()
  expect(await ui.find({ type: 'Text', text: '    +42' })).toBeTruthy()
  expect(await ui.find({ type: 'Text', text: /Writing… 3\.0s · ↓ 45 tokens/ })).toBeTruthy()
  expect(await ui.find({ type: 'Text', text: /reasoning on/ })).toBeTruthy()
  expect(await ui.find({ type: 'Text', text: /~5m left/ })).toBeTruthy()
  await ui.unmount()

  // a running task is re-read every few seconds, since its session moves between status writes
  await clock.advance(6000)
  expect(watches().length).toBeGreaterThan(1)
})

test('/workbench-quit stops everything; /workbench-quit-and-hand-back-claude hands the tasks to Claude', async ($, on) => {
  const clock = mock.clock(on, { now: 1_800_000_000_000 })
  fakeTasks(on, {})
  const runs: string[][] = []
  on('process.run', (_: any, e: any) => {
    runs.push(e.argv)
    return { value: { exitCode: 0, stdout: e.argv[1] === 'stop' ? 'stopped: fix-it' : 'took over fix-it', stderr: '' } }
  })
  const submitted: string[] = []
  on('prompt.submit', (_: any, e: any) => {
    submitted.push(e.text)
    return { text: e.text }
  })
  on('ui.toast', () => ({ value: undefined }))
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })

  const quit = await $.command.run({ command: 'workbench-quit', args: '' })
  expect(runs.at(-1)).toEqual(['wb', 'stop', '--all', '--repo', '/'])  // this session's tasks
  expect(quit.text).toContain('stopped: fix-it')
  expect(JSON.stringify(quit.context)).toContain("Don't finish, restart")  // Claude is told
  expect(submitted.length).toBe(0)                                         // without a turn

  await $.command.run({ command: 'workbench-quit-and-hand-back-claude', args: '' })
  expect(runs.at(-1)).toEqual(['wb', 'takeover', '--all', '--user', '--repo', '/'])
  await clock.advance(2000)
  expect(submitted.at(-1)).toContain('took over fix-it')                  // Claude starts now
  expect(submitted.at(-1)).toContain('Leave workbench mode')

  // Typed words never stop anything: only the commands do.
  const before = runs.length
  await $.prompt.submit({ text: 'if I said bail out claude, would it stop?' })
  expect(runs.length).toBe(before)
})

test('wb failing is said as a failure: not open tasks, not agents stopped', async ($, on) => {
  // the 2026-11-16 audit: a broken wb's traceback reached Claude as "Tasks … are open"
  const clock = mock.clock(on, { now: 1_800_000_000_000 })
  fakeTasks(on, {})
  const trace = 'Traceback (most recent call last):\n  File "wb"\nSyntaxError: invalid syntax'
  on('process.run', () => ({ value: { exitCode: 1, stdout: '', stderr: trace } }))
  const toasts: string[] = []
  on('ui.toast', (_: any, e: any) => { toasts.push(e.text); return { value: undefined } })
  on('prompt.context', (_: any, e: any) => ({ blocks: e.blocks }))
  const submitted: string[] = []
  on('prompt.submit', (_: any, e: any) => { submitted.push(e.text); return { text: e.text } })
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  const ctx = await $.prompt.context({ blocks: [] })
  expect(ctx.blocks.map((b: any) => b.name)).not.toContain('workbench')
  expect(toasts.join('\n')).toContain('wb status failed: SyntaxError: invalid syntax')

  const quit = await $.command.run({ command: 'workbench-quit', args: '' })
  expect(quit.text).toContain('not stopped')
  expect(JSON.stringify(quit.context)).toContain('failed, so its agents may still be running')
  const back = await $.command.run({ command: 'workbench-quit-and-hand-back-claude', args: '' })
  expect(back.text).toContain('wb takeover --all failed')
  await clock.advance(3000)
  expect(submitted.join('\n')).not.toContain('Leave workbench mode')   // no turn to finish what wasn't handed over
})

test('a closed task compacts Claude early, keeping the delegation state; open tasks come back after', async ($, on) => {
  const clock = mock.clock(on, { now: 1_800_000_000_000 })
  const ts = 1_800_000_000
  const files: Record<string, string> = {
    '/wb/tasks/fix-it/task.json': JSON.stringify({ name: 'fix-it', model: 'q', created_at: ts }),
    '/wb/tasks/fix-it/status.json': JSON.stringify({ phase: 'running', round: 1, updated_at: ts }),
  }
  fakeTasks(on, files)
  on('process.run', () => ({ value: { exitCode: 0, stdout: 'fix-it   running   1 q', stderr: '' } }))
  on('ui.toast', () => ({ value: undefined }))
  on('prompt.context', (_: any, e: any) => ({ blocks: e.blocks }))
  let tokens = 300_000
  on('session.usage', () => ({ value: { startedAt: 0, rateLimits: [], context: {
    tokens, window: 1_000_000, percent: 30,
    breakdown: { isAutoCompactEnabled: true, autoCompactThreshold: 400_000 } } } }))
  const compactions: (string | undefined)[] = []
  on('session.compact', (_: any, e: any) => {
    compactions.push(e.instructions)
    return { messages: [{ role: 'user', text: 'summary', toolUses: [] }] }
  })
  on('skill.prompt', (_: any, e: any) => ({ text: e.text }))
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await $.skill.prompt({ skill: 'workbench', text: 'x', frontmatter: {}, source: 'userSettings' })
  await clock.advance(2000)
  expect(compactions.length).toBe(0)              // the task is still running

  // Any compaction meanwhile (here a /compact) is told to keep the delegation state.
  await $.session.compact({ instructions: 'the plan', trigger: 'manual', messages: [{ role: 'user', text: 'hi', toolUses: [] }] } as any)
  expect(compactions.at(-1)).toContain('the plan')
  expect(compactions.at(-1)).toContain('Workbench delegation is in progress')

  // A fresh or compacted conversation is handed the open tasks.
  const ctx = await $.prompt.context({ blocks: [] })
  expect(ctx.blocks.find((b: any) => b.name === 'workbench')?.text).toContain('fix-it   running')

  // The task closes: at 300K of 400K (75%, past the halfway mark) Claude is compacted once.
  files['/wb/tasks/fix-it/status.json'] = JSON.stringify({ phase: 'accepted', round: 1, updated_at: ts + 5 })
  await clock.advance(2000)
  expect(compactions.length).toBe(2)
  expect(compactions.at(-1)).toContain('Drop the diffs')
  await clock.advance(2000)
  expect(compactions.length).toBe(2)              // once per boundary

  // Below the mark, a boundary leaves the context alone.
  tokens = 100_000
  files['/wb/tasks/fix-it/status.json'] = JSON.stringify({ phase: 'running', round: 2, updated_at: ts + 9 })
  await clock.advance(2000)
  files['/wb/tasks/fix-it/status.json'] = JSON.stringify({ phase: 'discarded', round: 2, updated_at: ts + 12 })
  await clock.advance(2000)
  expect(compactions.length).toBe(2)
})

test('a session that never delegated is not compacted when a task closes', async ($, on) => {
  // the 2026-11-16 audit: a task closing compacted every session's context, delegating or not
  const clock = mock.clock(on, { now: 1_800_000_000_000 })
  const files: Record<string, string> = {}
  task(files, 'fix-it', 'running')
  fakeTasks(on, files)
  on('process.run', () => ({ value: { exitCode: 0, stdout: 'fix-it   running', stderr: '' } }))
  on('ui.toast', () => ({ value: undefined }))
  on('session.usage', () => ({ value: { startedAt: 0, rateLimits: [], context: {
    tokens: 300_000, window: 1_000_000, percent: 30,
    breakdown: { isAutoCompactEnabled: true, autoCompactThreshold: 400_000 } } } }))
  const compactions: (string | undefined)[] = []
  on('session.compact', (_: any, e: any) => {
    compactions.push(e.instructions)
    return { messages: [{ role: 'user', text: 'summary', toolUses: [] }] }
  })
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.advance(2000)
  task(files, 'fix-it', 'accepted', {}, ts0 + 5)
  await clock.advance(4000)
  expect(compactions).toEqual([])
  await $.session.compact({ instructions: 'the plan', trigger: 'manual', messages: [{ role: 'user', text: 'hi', toolUses: [] }] } as any)
  expect(compactions).toEqual(['the plan'])                     // a /compact of its own, untouched
})

test('the live header names the followed task', async ($, on) => {
  const clock = mock.clock(on, { now: 1_800_000_000_000 })
  const ts = 1_800_000_000
  const files: Record<string, string> = {}
  files['/wb/tasks/fix-it/task.json'] = JSON.stringify({ name: 'fix-it', model: 'q27', model_label: 'Qwen 27B', created_at: ts })
  files['/wb/tasks/fix-it/status.json'] = JSON.stringify({ phase: 'running', round: 1, updated_at: ts })
  fakeTasks(on, files, ['fix-it'])
  let n = 0
  on('process.run', () => {
    n += 1  // the transcript grows every poll
    const lines = Array.from({ length: n }, (_, i) => ({ s: 'tool', t: `⏺ Read(f${i}.py)` }))
    return { value: { exitCode: 0, stdout: lines.map(l => JSON.stringify(l)).join('\n'), stderr: '' } }
  })
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  await clock.advance(1600)
  await clock.advance(1600)
  // (the kit has no pane to scroll, so following the end is checked in a real session)
  expect(await ui.find({ type: 'Text', text: /── fix-it/ })).toBeTruthy()
  await ui.unmount()
})

test('the transcript is read only while the pane is shown', async ($, on) => {
  // the 2026-11-16 audit: every session ran OpenCode exports back to back, pane open or not
  const clock = mock.clock(on, { now: 1_800_000_000_000 })
  const files: Record<string, string> = {}
  task(files, 'fix-it', 'running')
  const pane = fakeTasks(on, files)
  const watches: string[][] = []
  on('process.run', (_: any, e: any) => {
    if (e.argv[1] === 'watch') watches.push(e.argv)
    return { value: { exitCode: 0, stdout: JSON.stringify({ s: 'tool', t: '⏺ Read(f.py)' }), stderr: '' } }
  })
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.advance(3000)
  expect(watches.length).toBe(1)
  pane.show(false)                                              // the person closes it
  const before = watches.length
  await clock.advance(30_000)
  expect(watches.length).toBe(before)
  pane.show(true)
  await clock.advance(6000)
  expect(watches.length).toBeGreaterThan(before)
})

test('with two tasks running the pane keeps following the one it follows', async ($, on) => {
  // the 2026-11-16 audit: it swapped to whichever agent made the latest tool call
  const clock = mock.clock(on, { now: 1_800_000_000_000 })
  const files: Record<string, string> = {}
  task(files, 'a', 'running')
  task(files, 'b', 'running')
  fakeTasks(on, files, ['a', 'b'])
  const watched: string[] = []
  on('process.run', (_: any, e: any) => {
    if (e.argv[1] === 'watch') watched.push(e.argv[2])
    return { value: { exitCode: 0, stdout: JSON.stringify({ s: 'tool', t: `⏺ ${e.argv[2]}` }), stderr: '' } }
  })
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  for (let i = 1; i <= 8; i++) {                                // each agent's tool calls in turn
    task(files, i % 2 ? 'a' : 'b', 'running', {}, ts0 + i)
    await clock.advance(1600)
  }
  expect(new Set(watched).size).toBe(1)
  expect(watched.length).toBeGreaterThan(1)
})

test('a transcript cut at the engine\'s limit keeps the lines shown and says so', async ($, on) => {
  // the 2026-11-16 audit: the pane froze on old lines, with no hint why
  const clock = mock.clock(on, { now: 1_800_000_000_000 })
  const files: Record<string, string> = {}
  task(files, 'fix-it', 'running')
  fakeTasks(on, files)
  let cut = false
  on('process.run', () => ({
    value: { exitCode: 0, stdout: JSON.stringify({ s: 'tool', t: cut ? '⏺ OLD-START' : '⏺ Read(f.py)' }), stderr: '', isStdoutTruncated: cut },
  }))
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.advance(3000)
  cut = true
  await clock.advance(6000)
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect(await ui.find({ type: 'Text', text: '⏺ Read(f.py)' })).toBeTruthy()
  expect(await ui.find({ type: 'Text', text: 'transcript too long for the pane: wb watch fix-it' })).toBeTruthy()
  await ui.unmount()
})

test('one very long transcript line is cut, and the lines before it still show', async ($, on) => {
  // the 2026-11-16 audit: a heredoc command took the whole history budget alone
  const clock = mock.clock(on, { now: 1_800_000_000_000 })
  const files: Record<string, string> = {}
  task(files, 'fix-it', 'running')
  fakeTasks(on, files)
  const lines = [{ s: 'tool', t: '⏺ Read(early.py)' }, { s: 'tool', t: `⏺ Bash(cat > f <<'EOF' ${'x'.repeat(70_000)})` }]
  on('process.run', () => ({ value: { exitCode: 0, stdout: lines.map(l => JSON.stringify(l)).join('\n'), stderr: '' } }))
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.advance(3000)
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect(await ui.find({ type: 'Text', text: '⏺ Read(early.py)' })).toBeTruthy()
  expect((await ui.find({ type: 'Text', text: /^⏺ Bash\(cat/ }))?.text.length).toBeLessThanOrEqual(2000)
  await ui.unmount()
})

test('the weekly model check comes once, in a quiet moment of a session in use; /workbench-models asks now', async ($, on) => {
  const clock = mock.clock(on, { now: 1_800_000_000_000 })
  const files: Record<string, string> = {}
  fakeTasks(on, files, [], false)
  on('ui.toast', () => ({ value: undefined }))
  const asked: string[] = []
  on('prompt.submit', (_: any, e: any) => {
    asked.push(e.text)
    return { text: e.text }
  })
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.advance(3000)
  expect(asked.length).toBe(0)                       // nobody has used the session yet

  await $.turn.start({ text: 'hi', turnId: 't1' })
  await $.turn.complete({ answer: '', durationMs: 1, isAborted: false, turnId: 't1', reason: 'answer' })
  await clock.advance(2000)
  expect(asked.length).toBe(0)                       // not at once: Claude may have just asked something
  await clock.advance(90_000)                        // a quiet minute later
  expect(asked.length).toBe(1)
  expect(asked[0]).toContain('The weekly model check is due')
  expect(asked[0]).toContain('`wb scout`')
  expect(JSON.parse(files['/wb/scout.json']).offered_at).toBeGreaterThan(1_800_000_000)  // raised: not again today

  await clock.advance(10_000)
  expect(asked.length).toBe(1)

  // done this week: nothing more, until the user asks for it
  files['/wb/scout.json'] = JSON.stringify({ done_at: 1_800_000_010 })
  await $.command.run({ command: 'workbench-models', args: '' })
  await clock.advance(2000)
  expect(asked.length).toBe(2)
  expect(asked[1]).toContain('The weekly model check is due')
})

test('/workbench-models when the weekly check is due raises it once, not twice', async ($, on) => {
  // the 2026-11-16 audit: the weekly check followed at once, burying Claude's question to the user
  const clock = mock.clock(on, { now: 1_800_000_000_000 })
  const files: Record<string, string> = {}
  fakeTasks(on, files, [], false)
  on('ui.toast', () => ({ value: undefined }))
  const asked: string[] = []
  on('prompt.submit', (_: any, e: any) => { asked.push(e.text); return { text: e.text } })
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await $.command.run({ command: 'workbench-models', args: '' })
  await clock.advance(2000)
  await $.turn.start({ text: 'the check', turnId: 't1' })
  await $.turn.complete(endTurn('t1'))                          // Claude asks "shall I pull it?"
  await clock.advance(120_000)
  expect(asked.filter(t => t.includes('The weekly model check is due'))).toHaveLength(1)
})

test('when wb can\'t run the mod says so once, backs off, and raises no model check', async ($, on) => {
  // the 2026-11-16 audit: `wb events` was respawned every 2 s for good, and nobody was told
  const clock = mock.clock(on, { now: 1_800_000_000_000 })
  const files: Record<string, string> = {}
  fakeTasks(on, files, [], false, [], false)
  let spawns = 0
  on('process.spawn', async function* () { spawns += 1; throw new Error('spawn wb ENOENT') })
  on('process.run', () => { throw new Error('spawn wb ENOENT') })
  const toasts: string[] = []
  on('ui.toast', (_: any, e: any) => { toasts.push(e.text); return { value: undefined } })
  const asked: string[] = []
  on('prompt.submit', (_: any, e: any) => { asked.push(e.text); return { text: e.text } })
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await $.turn.start({ text: 'hi', turnId: 't1' })
  await $.turn.complete(endTurn('t1'))
  await clock.advance(120_000)
  expect(toasts.filter(t => t.includes("can't run `wb events`"))).toHaveLength(1)
  expect(spawns).toBeLessThan(10)                               // 2 s, 4 s, … up to a minute
  expect(asked).toEqual([])
})

test('an update is checked once a session is in use, and /workbench-update installs it', async ($, on) => {
  const clock = mock.clock(on, { now: 1_800_000_000_000 })
  fakeTasks(on, {}, [])
  let behind = 3
  const runs: string[][] = []
  const limits: Record<string, number> = {}
  on('process.run', (_: any, e: any) => {
    runs.push(e.argv)
    limits[e.argv.slice(1).join(' ')] = e.init?.timeoutMs
    if (e.argv[1] === 'optimize') return { value: { exitCode: 0, stdout: '', stderr: '' } }  // not due
    const said = e.argv[2] === '--check'
      ? (behind ? `update available: ${behind} new commit(s); /workbench-update (or wb update) installs it` : 'up to date')
      : behind ? 'updated marketplace 1111111..2222222 (3 commit(s))' : 'already up to date'
    return { value: { exitCode: 0, stdout: said, stderr: '' } }
  })
  const toasts: string[] = []
  on('ui.toast', (_: any, e: any) => { toasts.push(e.text); return { value: undefined } })
  on('prompt.submit', (_: any, e: any) => ({ text: e.text }))
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.advance(3000)
  expect(runs.some(r => r[1] === 'update')).toBe(false)          // nobody has used the session yet

  await $.turn.start({ text: 'hi', turnId: 't1' })
  await $.turn.complete({ answer: '', durationMs: 1, isAborted: false, turnId: 't1', reason: 'answer' })
  await clock.advance(2000)
  expect(runs.filter(r => r[1] === 'update')).toEqual([['wb', 'update', '--check', '--if-due']])  // wb keeps it weekly
  expect(toasts.at(-1)).toContain('/workbench-update')
  await clock.advance(5000)
  expect(runs.filter(r => r[1] === 'update').length).toBe(1)     // once per load
  expect(runs.filter(r => r[1] === 'optimize')).toEqual([['wb', 'optimize', 'start', '--if-due']])
  expect(toasts.some(t => t.includes('measuring'))).toBe(false)  // not due: nothing said

  const out = await $.command.run({ command: 'workbench-update', args: '' })
  expect(runs.at(-1)).toEqual(['wb', 'update'])
  expect(limits.update).toBe(600_000)                            // its steps take up to 300 s each
  expect(out.text).toContain('updated marketplace')
  behind = 0
  expect((await $.command.run({ command: 'workbench-update', args: '' })).text).toContain('already up to date')
})

test('a reply to a pair of screenshots reaches Claude with its task and step (#99)', async ($, on) => {
  const clock = mock.clock(on, { now: 1_800_000_000_000 })
  const files: Record<string, string> = {}
  fakeTasks(on, files, [])
  chatOn(files)                                                   // the user turned the web chat on
  on('ui.toast', () => ({ value: undefined }))
  on('session.cwd', () => ({ value: '/home/u/project' }))
  on('skill.prompt', (_: any, e: any) => ({ text: e.text }))
  on('process.run', () => ({ value: { exitCode: 0, stdout: 'no open tasks', stderr: '' } }))
  const asked: string[] = []
  on('prompt.submit', (_: any, e: any) => { asked.push(e.text); return { text: e.text } })
  files['/wb/chat/messages.jsonl'] = JSON.stringify({ id: 'm1', ts: 1_800_000_000, from: 'user', text: 'the button moved',
    about: { task: 'fix-nav', n: 2, step: 1, round: 3 } }) + '\n'
  await $.session.start({ cwd: '/home/u/project', surface: 'terminal', isInteractive: true })
  await $.skill.prompt({ skill: 'workbench', text: 'x', frontmatter: {}, source: 'userSettings' })
  await clock.advance(3000)
  // the task and step the screenshots were of, whichever task is active now
  expect(asked).toEqual(['[web chat] (about fix-nav, step 1, screenshots 2) the button moved'])
})

test('the web chat reaches Claude only in /workbench mode, with a receipt, and one session listens', async ($, on) => {
  const clock = mock.clock(on, { now: 1_800_000_000_000 })
  const files: Record<string, string> = {}
  fakeTasks(on, files, [])
  chatOn(files)                                                   // the user turned the web chat on
  on('ui.toast', () => ({ value: undefined }))
  on('session.cwd', () => ({ value: '/home/u/project' }))
  on('skill.prompt', (_: any, e: any) => ({ text: e.text }))  // the engine's expansion
  const runs: string[][] = []
  on('process.run', (_: any, e: any) => {
    runs.push(e.argv)
    if (e.argv[1] === 'chat' && e.argv[2] === 'ack') {
      files['/wb/chat/receipts.jsonl'] = (files['/wb/chat/receipts.jsonl'] ?? '') +
        JSON.stringify({ id: e.argv[3], by: e.argv[5] }) + '\n'
    }
    return { value: { exitCode: 0, stdout: 'no open tasks', stderr: '' } }
  })
  const asked: string[] = []
  on('prompt.submit', (_: any, e: any) => {
    asked.push(e.text)
    return { text: e.text }
  })
  files['/wb/chat/messages.jsonl'] = JSON.stringify({ id: 'm1', ts: 1_800_000_000, from: 'user', text: 'status?' }) + '\n'
  await $.session.start({ cwd: '/home/u/project', surface: 'terminal', isInteractive: true })
  await clock.advance(3000)
  expect(asked.length).toBe(0)                                   // not in workbench mode: nobody listens
  expect(files['/wb/chat/listener.json']).toBeUndefined()

  await $.skill.prompt({ skill: 'workbench', text: 'x', frontmatter: {}, source: 'userSettings' })
  await clock.advance(3000)
  expect(asked.length).toBe(1)
  expect(asked[0]).toBe('[web chat] status?')                    // their words, nothing else
  expect(JSON.parse(files['/wb/chat/listener.json']).cwd).toBe('/home/u/project')
  expect(runs.some(a => a.join(' ').startsWith('wb chat ack m1'))).toBe(true)
  await clock.advance(3000)
  expect(asked.length).toBe(1)                                   // once

  // another session holds the chat: this one stands by
  files['/wb/chat/listener.json'] = JSON.stringify({ session: 'other', cwd: '/x', ts: 1_800_000_009 })
  files['/wb/chat/messages.jsonl'] += JSON.stringify({ id: 'm2', ts: 1_800_000_009, from: 'user', text: 'hi' }) + '\n'
  await clock.advance(2000)
  expect(asked.length).toBe(1)

  // /workbench-quit ends workbench mode, and with it the listening
  await $.command.run({ command: 'workbench-quit', args: '' })
  await clock.advance(20_000)
  expect(asked.length).toBe(1)
})

test('a web message wb holds open is delivered past the files tails, and an old one is named', async ($, on) => {
  // #329: an unread message behind 4 MiB of replies fell out of the tail the mod read
  const clock = mock.clock(on, { now: 1_800_000_000_000 })
  const files: Record<string, string> = {}
  fakeTasks(on, files, [])
  chatOn(files)
  const toasts: string[] = []
  on('ui.toast', (_: any, e: any) => { toasts.push(String(e.text ?? e.message ?? JSON.stringify(e))); return { value: undefined } })
  on('session.cwd', () => ({ value: '/home/u/project' }))
  on('skill.prompt', (_: any, e: any) => ({ text: e.text }))
  on('process.run', () => ({ value: { exitCode: 0, stdout: 'no open tasks', stderr: '' } }))
  const asked: string[] = []
  on('prompt.submit', (_: any, e: any) => {
    asked.push(e.text)
    return { text: e.text }
  })
  files['/wb/chat/messages.jsonl'] = JSON.stringify({ id: 'r1', ts: 1_800_000_000, from: 'claude', text: 'x' }) + '\n'
  files['/wb/chat/open.json'] = JSON.stringify({ asks: {}, messages: {
    m1: { id: 'm1', ts: 1_800_000_000, from: 'user', text: 'are you there?' },
    m0: { id: 'm0', ts: 1_800_000_000 - 7 * 3600, from: 'user', text: 'from this morning' } } })
  await $.session.start({ cwd: '/home/u/project', surface: 'terminal', isInteractive: true })
  await $.skill.prompt({ skill: 'workbench', text: 'x', frontmatter: {}, source: 'userSettings' })
  await clock.advance(3000)
  expect(asked).toEqual(['[web chat] are you there?'])              // only in wb's index: delivered
  expect(toasts.filter(t => t.includes('wb chat pending')).length).toBe(1)  // the old one, named once
  await clock.advance(3000)
  expect(toasts.filter(t => t.includes('wb chat pending')).length).toBe(1)
})

test('a web message another session claimed first is not submitted here', async ($, on) => {
  // #84: two sessions starting together both took the chat and both submitted the message
  const clock = mock.clock(on, { now: 1_800_000_000_000 })
  const files: Record<string, string> = {}
  fakeTasks(on, files, [])
  chatOn(files)                                                   // the user turned the web chat on
  on('ui.toast', () => ({ value: undefined }))
  on('session.cwd', () => ({ value: '/home/u/project' }))
  on('skill.prompt', (_: any, e: any) => ({ text: e.text }))
  let theirs = true
  on('process.run', (_: any, e: any) => {
    const refused = e.argv[1] === 'chat' && e.argv[2] === 'claim' && theirs
    return { value: { exitCode: refused ? 1 : 0, stdout: refused ? '' : 'no open tasks', stderr: '' } }
  })
  const asked: string[] = []
  on('prompt.submit', (_: any, e: any) => {
    asked.push(e.text)
    return { text: e.text }
  })
  files['/wb/chat/messages.jsonl'] = JSON.stringify({ id: 'm1', ts: 1_800_000_000, from: 'user', text: 'status?' }) + '\n'
  await $.session.start({ cwd: '/home/u/project', surface: 'terminal', isInteractive: true })
  await $.skill.prompt({ skill: 'workbench', text: 'x', frontmatter: {}, source: 'userSettings' })
  await clock.advance(3000)
  expect(asked.length).toBe(0)                                   // the other session submits it
  theirs = false                                                 // gone, its claim stale
  await clock.advance(61_000)
  expect(asked).toEqual(['[web chat] status?'])
})

test('a permission prompt while the user is away goes to the web chat, and their answer settles it', async ($, on) => {
  // the dialog would wait for nobody (the user, 2026-11-18)
  const clock = mock.clock(on, { now: 1_800_000_000_000 })
  const files: Record<string, string> = {}
  fakeTasks(on, files, [])
  chatOn(files)                                                   // the user turned the web chat on
  const toasts: string[] = []
  on('ui.toast', (_: any, e: any) => { toasts.push(e.text); return { value: undefined } })
  on('session.cwd', () => ({ value: '/home/u/project' }))
  on('skill.prompt', (_: any, e: any) => ({ text: e.text }))
  on('prompt.submit', (_: any, e: any) => ({ text: e.text }))
  let said: any = { answer: 'allow' }, posted = 'ask-1-ab open'
  const runs: string[][] = []
  on('process.run', (_: any, e: any) => {
    runs.push(e.argv)
    const [, cmd, act] = e.argv
    if (cmd === 'chat' && act === 'ask') return { value: { exitCode: 0, stdout: posted + '\n', stderr: '' } }
    if (cmd === 'chat' && act === 'wait') return { value: { exitCode: said ? 0 : 1, stdout: JSON.stringify(said), stderr: '' } }
    return { value: { exitCode: 0, stdout: 'no open tasks', stderr: '' } }
  })
  on('classic.PermissionRequest', () => ({}))                 // no settings hook decides: the dialog
  const ask = (tool_name: string, tool_input: unknown) =>
    $.classic.PermissionRequest({ tool_name, tool_input } as any) as Promise<any>
  await $.session.start({ cwd: '/home/u/project', surface: 'terminal', isInteractive: true })
  await clock.advance(3000)
  expect((await ask('Bash', { command: 'git push' })).decision).toBeUndefined()  // at the terminal: the dialog
  expect(runs.some(a => a[2] === 'ask')).toBe(false)

  await $.skill.prompt({ skill: 'workbench', text: 'x', frontmatter: {}, source: 'userSettings' })
  files['/wb/chat/messages.jsonl'] = JSON.stringify({ id: 'm1', ts: 1_800_000_003, from: 'user', text: 'hi' }) + '\n'
  await clock.advance(3000)                                     // a web message: the user is away
  expect((await ask('Bash', { command: 'git push origin main', description: 'Push the fix' })).decision).toEqual({ behavior: 'allow' })
  expect(runs.find(a => a[2] === 'ask')).toEqual(['wb', 'chat', 'ask', '--tool', 'Bash', '--kind', 'permission', '--text',
    'git push origin main', '--detail', '{"purpose":"Push the fix"}'])  // the card leads with it (#103)
  expect(runs.find(a => a[2] === 'wait')?.slice(0, 4)).toEqual(['wb', 'chat', 'wait', 'ask-1-ab'])
  expect(toasts.some(t => t.includes('waiting for you in the web chat'))).toBe(true)

  said = { answer: 'deny', note: 'not on main' }
  const no = (await ask('Write', { file_path: '/etc/hosts', content: 'x' })).decision
  expect(no.behavior).toBe('deny')
  expect(no.message).toContain('wb chat reply')                  // Claude asks the user what instead
  expect(no.message).toContain('not on main')                    // with their words
  expect(runs.filter(a => a[2] === 'ask').at(-1)?.at(-1)).toBe('/etc/hosts')

  // a finished plan: approved, or sent back with a note
  said = { answer: 'allow' }
  expect((await ask('ExitPlanMode', {})).decision).toEqual({ behavior: 'allow' })
  expect(runs.filter(a => a[2] === 'ask').at(-1)?.slice(3, 7)).toEqual(['--tool', 'ExitPlanMode', '--kind', 'plan'])
  said = { answer: 'deny', note: 'split step 2' }
  const back = (await ask('ExitPlanMode', {})).decision
  expect([back.behavior, back.message.includes('split step 2'), back.message.includes('Revise the plan')]).toEqual(['deny', true, true])
  // #253 (the audit of 1.2): the card is this session's plan, by its file, never the newest of any
  // project's; one wb can't read is answered in the terminal, so the dialog comes up there
  said = { answer: 'allow' }
  await ask('ExitPlanMode', { planFilePath: '/home/u/.claude/plans/a-b-c.md' })
  const plan = runs.filter(a => a[2] === 'ask').at(-1)!
  expect(plan[plan.indexOf('--detail') + 1]).toBe('{"plan_file":"/home/u/.claude/plans/a-b-c.md"}')
  expect(plan.slice(-2)).toEqual(['--repo', '/home/u/project'])     // whose plan it is
  posted = 'ask-3-ef expired'
  const waited = runs.filter(a => a[2] === 'wait').length
  expect((await ask('ExitPlanMode', {})).decision).toBeUndefined()
  expect(runs.filter(a => a[2] === 'wait').length).toBe(waited)    // nothing to wait for on the page
  posted = 'ask-1-ab open'

  // a question: the options they picked become the tool's answers
  const questions = [{ question: 'Which DB?', header: 'DB', options: [{ label: 'SQLite', description: '' }, { label: 'Postgres', description: '' }], multiSelect: false }]
  said = { answer: 'allow', answers: { 'Which DB?': 'SQLite' } }
  const picked = (await ask('AskUserQuestion', { questions })).decision
  expect(picked).toEqual({ behavior: 'allow', updatedInput: { questions, answers: { 'Which DB?': 'SQLite' } } })
  expect(JSON.parse(runs.filter(a => a[2] === 'ask').at(-1)!.at(-1)!)).toEqual({ questions })

  // their setting says go ahead while away: allowed at once, no wait, no toast
  posted = 'ask-2-cd allow'
  const [waits, toastsBefore] = [runs.filter(a => a[2] === 'wait').length, toasts.length]
  expect((await ask('Bash', { command: 'make' })).decision).toEqual({ behavior: 'allow' })
  expect([runs.filter(a => a[2] === 'wait').length, toasts.length]).toEqual([waits, toastsBefore])
  posted = 'ask-1-ab open'

  said = ''                                                     // wb can't wait: it expires, the dialog comes
  expect((await ask('Bash', { command: 'rm -rf build' })).decision).toBeUndefined()
  expect(runs.at(-1)).toEqual(['wb', 'chat', 'answer', 'ask-1-ab', 'expired', '--by', 'timeout'])

  await $.prompt.submit({ text: 'back now' } as any)            // typed in the terminal: back
  const n = runs.length
  said = { answer: 'allow' }
  expect((await ask('Bash', { command: 'git push' })).decision).toBeUndefined()
  expect(runs.slice(n).some(a => a[2] === 'ask')).toBe(false)
})

test('running wb counts as workbench mode, and a web message reaches a running turn', async ($, on) => {
  const clock = mock.clock(on, { now: 1_800_000_000_000 })
  const files: Record<string, string> = {}
  fakeTasks(on, files, [])
  chatOn(files)                                                   // the user turned the web chat on
  on('ui.toast', () => ({ value: undefined }))
  on('session.cwd', () => ({ value: '/home/u/project' }))
  on('session.id', () => ({ value: 'sess-1' }))
  const sent: any[] = []
  on('session.send', (_: any, e: any) => { sent.push(e); return { isDelivered: true } })
  const runs: string[][] = []
  on('process.run', (_: any, e: any) => { runs.push(e.argv); return { value: { exitCode: 0, stdout: 'no open tasks', stderr: '' } } })
  const asked: string[] = []
  on('prompt.submit', (_: any, e: any) => { asked.push(e.text); return { text: e.text } })
  on('tool.call', (_: any, e: any) => ({ result: { stdout: '' }, text: '' }))  // the engine's Bash
  files['/wb/chat/messages.jsonl'] = JSON.stringify({ id: 'm1', ts: 1_800_000_000, from: 'user', text: 'still there?' }) + '\n'
  await $.session.start({ cwd: '/home/u/project', surface: 'terminal', isInteractive: true })
  // no turn.start: the module loaded mid-turn; the tool call itself says a turn runs
  await $.tool.call({ tool: 'Bash', command: 'cd ~ && wb start step-2 --spec s.md --repo .' })
  await clock.advance(3000)
  expect(asked).toEqual([])                                     // mid-turn: not a queued prompt…
  expect(sent.length).toBe(1)                                   // …but into the running turn
  expect(sent[0].to).toBe('sess-1')                              // its own session
  expect(sent[0].text).toContain('[web chat] still there?')
  expect(sent[0].text).toContain('wb chat reply')               // framed as a peer's: one line says whose
  expect(runs.some(a => a.join(' ') === 'wb chat ack m1 --by ' + a[5] + ' --how now')).toBe(true)
})

test('only wb run as a command counts as delegating, not wb written in a string, a heredoc or a comment (#409)', () => {
  // 2026-11-20: a Python patch whose string held "split it (wb next)" turned workbench mode on
  for (const run of ['wb start t --spec s.md', 'cd ~ && wb start step-2 --spec s.md --repo .', 'wb next t',
    'git add -A; wb feedback t --text x', 'true || wb steer t "go"', 'WB_HOME=/x wb answer t q1 yes',
    'env WB_HOME=/x wb next t', '~/repos/workbench/bin/wb start t', 'out=$(wb start t --spec s.md)',
    'cat <<EOF > s.md\nwrite it\nEOF\nwb start t --spec s.md', '(cd repo; wb next t)'])
    expect([run, delegates(run)]).toEqual([run, true])
  for (const data of ['python3 - <<\'EOF\'\ns = "split it (wb next) or take it over"\nEOF',
    'python3 -c "print(\'(wb next)\')"', 'git commit -m "wb start now records the session"',
    'grep -n "wb (start|next)" ARCHITECTURE.md', 'echo done # then wb next t', "echo 'wb answer t q1'",
    'wb status', 'wb accept t', 'cat <<-X\n\twb start t\n\tX', 'awb next t', 'wb nextstep'])
    expect([data, delegates(data)]).toEqual([data, false])
})

test('a heredoc that mentions wb leaves workbench mode off; running it turns it on (#409)', async ($, on) => {
  const clock = mock.clock(on, { now: 1_800_000_000_000 })
  const files: Record<string, string> = {}
  const ui: string[] = []
  fakeTasks(on, files, [], true, ui)
  on('process.run', () => ({ value: { exitCode: 0, stdout: '', stderr: '' } }))
  on('tool.call', () => ({ result: { stdout: '' }, text: '' }))  // the engine's Bash
  await $.session.start({ cwd: '/home/u/project', surface: 'terminal', isInteractive: true })
  const mode = async () => ui.includes('status ⚒ wb · workbench mode')   // the mode's sign on the status line
  await $.tool.call({ tool: 'Bash', command: 'python3 - <<\'EOF\'\nmsg = "split it (wb next) or take it over"\nEOF' })
  await clock.advance(1000)
  expect(await mode()).toBe(false)
  expect(ui).not.toContain('open workbench')
  await $.tool.call({ tool: 'Bash', command: 'cd ~ && wb next t' })
  await clock.advance(1000)
  expect(await mode()).toBe(true)
})

test('a task left waiting with no notice reaching Claude wakes it anyway, once', async ($, on) => {
  const clock = mock.clock(on, { now: 1_800_000_000_000 })
  const ts = 1_800_000_000 - 600  // failed ten minutes ago
  const files: Record<string, string> = {
    '/wb/tasks/lost/task.json': JSON.stringify({ name: 'lost', model: 'm', created_at: ts - 60 }),
    '/wb/tasks/lost/status.json': JSON.stringify({ phase: 'failed', round: 3, updated_at: ts }),
    '/wb/tasks/lost/acked': String(ts - 1),             // a note ran just before the failure
    '/wb/tasks/seen/task.json': JSON.stringify({ name: 'seen', model: 'm', created_at: ts - 60 }),
    '/wb/tasks/seen/status.json': JSON.stringify({ phase: 'review', round: 1, updated_at: ts }),
    '/wb/tasks/seen/acked': String(ts),                 // Claude had this one
  }
  fakeTasks(on, files, ['lost', 'seen'])
  on('process.run', () => ({ value: { exitCode: 0, stdout: '', stderr: '' } }))
  const asked: string[] = []
  on('prompt.submit', (_: any, e: any) => { asked.push(e.text); return { text: e.text } })
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.advance(3000)
  expect(asked.filter(t => t.includes('no notice reached Claude'))).toHaveLength(1)
  expect(asked.join('\n')).toContain('Task `lost` failed')
  expect(asked.join('\n')).not.toContain('`seen`')
  await clock.advance(10_000)
  expect(asked.filter(t => t.includes('no notice reached Claude'))).toHaveLength(1)
})

test('a bench replay is no one\'s: not shown, and no wake even when it waits with no notice', async ($, on) => {
  const clock = mock.clock(on, { now: 1_800_000_000_000 })
  const files: Record<string, string> = {}
  task(files, 'bench-case-1', 'failed', { bench: { case: 'case', run: 'r1' } }, ts0 - 600)
  fakeTasks(on, files, ['bench-case-1'])
  on('process.run', () => ({ value: { exitCode: 0, stdout: '', stderr: '' } }))
  const asked: string[] = []
  on('prompt.submit', (_: any, e: any) => { asked.push(e.text); return { text: e.text } })
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.advance(3000)
  await clock.advance(120_000)
  expect(asked.join('\n')).not.toContain('bench-case-1')
})

test('a task closed long ago is not read again', async ($, on) => {
  // the 2026-11-16 audit: every poll read both files of every task ever created
  const clock = mock.clock(on, { now: 1_800_000_000_000 })
  const files: Record<string, string> = {}
  task(files, 'old', 'accepted', {}, ts0 - 7200)
  task(files, 'waits', 'review', {}, ts0 - 7200)                // waiting on Claude: shown however old
  const { reads } = fakeTasks(on, files, ['old', 'waits'])
  on('process.run', () => ({ value: { exitCode: 0, stdout: '', stderr: '' } }))
  on('ui.toast', () => ({ value: undefined }))
  on('prompt.submit', (_: any, e: any) => ({ text: e.text }))
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.advance(70_000)
  expect(reads.filter(r => r === '/wb/tasks/old/status.json')).toHaveLength(1)
  expect(reads.filter(r => r === '/wb/tasks/old/task.json')).toHaveLength(0)
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect(await ui.find({ type: 'Text', text: 'waits' })).toBeTruthy()
  expect(await ui.find({ type: 'Text', text: 'old' })).toBeFalsy()
  await ui.unmount()
})

test('the apprentice says how the followed task stands: in pixels in a terminal, by its face elsewhere', async ($, on) => {
  const clock = mock.clock(on, { now: 1_800_000_000_000 })
  const ts = 1_800_000_000
  const files: Record<string, string> = {
    '/wb/tasks/fix-it/task.json': JSON.stringify({ name: 'fix-it', model: 'm', created_at: ts }),
    '/wb/tasks/fix-it/status.json': JSON.stringify({ phase: 'running', round: 1, updated_at: ts }),
  }
  fakeTasks(on, files)
  on('process.run', () => ({
    value: { exitCode: 0, stdout: JSON.stringify({ s: 'activity', t: 'Writing… 3.0s' }), stderr: '' },
  }))
  const blits: string[] = []
  on('ui.blit', (_: any, e: any) => { blits.push(e.key); return { value: {} } })
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.advance(1600)

  // at work: the wrench swings now and then between draws, not on every frame
  let ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect(await ui.find({ type: 'Raster' })).toBeTruthy()
  expect(await ui.find({ type: 'Text', text: /Writing… 3\.0s/ })).toBeTruthy()
  await clock.advance(5400) // twelve frames: two strokes, up and down
  expect(blits).toContain('apprentice')
  expect(blits.length).toBeLessThanOrEqual(5)
  await ui.unmount()

  // done: it says so, and the task's mark says so too
  files['/wb/tasks/fix-it/status.json'] = JSON.stringify({ phase: 'review', round: 1, updated_at: ts + 5 })
  await clock.advance(1600)
  ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect(await ui.find({ type: 'Text', text: 'fix-it is done.' })).toBeTruthy()
  expect(await ui.find({ type: 'Text', text: 'Claude reviews next.' })).toBeTruthy()  // who acts next (#103)
  expect(await ui.find({ type: 'Text', text: /^◆/ })).toBeTruthy()
  await ui.unmount()

  // no Raster on the desktop: the face on the line instead
  ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  expect(await ui.find({ type: 'Raster' })).toBeFalsy()
  expect(await ui.find({ type: 'Text', text: /\[\^_\^\]/ })).toBeTruthy()
  await ui.unmount()
})

test('/workbench alone turns workbench mode on for the session: the pane opens, and a quit ends it', async ($, on) => {
  const clock = mock.clock(on, { now: 1_800_000_000_000 })
  const files: Record<string, string> = {}
  const ui: string[] = []
  fakeTasks(on, files, [], true, ui)
  on('ui.toast', () => ({ value: undefined }))
  on('session.cwd', () => ({ value: '/home/u/project' }))
  on('skill.prompt', (_: any, e: any) => ({ text: e.text }))
  on('process.run', () => ({ value: { exitCode: 0, stdout: 'no open tasks', stderr: '' } }))
  on('prompt.context', (_: any, e: any) => ({ blocks: e.blocks }))
  const panes = () => ui.filter(l => !l.startsWith('status'))
  const status = () => ui.filter(l => l.startsWith('status')).at(-1)
  await $.session.start({ cwd: '/home/u/project', surface: 'terminal', isInteractive: true })
  await clock.advance(2000)
  expect(panes()).toEqual([])                                    // not delegating: no pane
  expect((await $.prompt.context({ blocks: [] })).blocks.map((b: any) => b.name)).not.toContain('workbench-mode')

  await $.skill.prompt({ skill: 'workbench', text: 'x', frontmatter: {}, source: 'userSettings',
    path: '/home/u/.claude/skills/workbench/SKILL.md' })
  await clock.advance(2000)
  expect(panes()).toEqual(['open workbench'])                    // the mode's sign
  expect(status()).toBe('status ⚒ wb · workbench mode')
  const pane = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect(await pane.find({ type: 'Text', text: 'Workbench mode is on.' })).toBeTruthy()
  await pane.unmount()
  // a compacted or cleared conversation is told the mode is on, and where the skill is
  const block = (await $.prompt.context({ blocks: [] })).blocks.find((b: any) => b.name === 'workbench-mode')
  expect(block?.text).toContain('/home/u/.claude/skills/workbench/SKILL.md')
  // what Claude learned about the user, in any project, comes with the mode
  expect((await $.prompt.context({ blocks: [] })).blocks.map((b: any) => b.name)).not.toContain('user-notes')
  files['/wb/notes.md'] = '- Review strictly: reject any diff without tests\n'
  const notes = (await $.prompt.context({ blocks: [] })).blocks.find((b: any) => b.name === 'user-notes')
  expect(notes?.text).toContain('reject any diff without tests')

  await $.command.run({ command: 'workbench-quit', args: '' })
  await clock.advance(2000)
  expect(panes()).toEqual(['open workbench', 'close workbench'])
  expect(status()).toBe('status ')
  expect((await $.prompt.context({ blocks: [] })).blocks.map((b: any) => b.name)).not.toContain('workbench-mode')
  expect((await $.prompt.context({ blocks: [] })).blocks.map((b: any) => b.name)).not.toContain('user-notes')
})

test('with wakes off, Claude is told no wake will come', { options: { wake: 'off' } }, async ($, on) => {
  // the 2026-11-16 audit: the skill said to end the turn and wait for a wake that never came
  const clock = mock.clock(on, { now: 1_800_000_000_000 })
  fakeTasks(on, {}, [])
  on('ui.toast', () => ({ value: undefined }))
  on('skill.prompt', (_: any, e: any) => ({ text: e.text }))
  on('process.run', () => ({ value: { exitCode: 0, stdout: 'no open tasks', stderr: '' } }))
  on('prompt.context', (_: any, e: any) => ({ blocks: e.blocks }))
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  expect((await $.prompt.context({ blocks: [] })).blocks.map((b: any) => b.name)).not.toContain('workbench-wake')
  await $.skill.prompt({ skill: 'workbench', text: 'x', frontmatter: {}, source: 'userSettings' })
  await clock.advance(2000)
  const block = (await $.prompt.context({ blocks: [] })).blocks.find((b: any) => b.name === 'workbench-wake')
  expect(block?.text).toContain('Wakes are off')
})

test('the bubble wraps its words into lines that fit, never cutting a line short', () => {
  const said = 'read /home/me/.local/state/workbench/tasks/fix-parser/worktree/src/parser.c and then the tests'
  const lines = wrapWords(said, 30)
  expect(lines.every(l => l.length <= 30)).toBe(true)
  expect(lines.join(' ').replace(/\s+/g, '')).toBe(said.replace(/\s+/g, ''))  // nothing lost
  expect(wrapWords('All caught up.', 30)).toEqual(['All caught up.'])
  expect(wrapWords('', 30)).toEqual([''])
})

test('the Tasks tab ends with the week\'s saving, read from wb savings; the web chat link is the Settings tab\'s', async ($, on) => {
  const clock = mock.clock(on, { now: 1_800_000_000_000 })
  fakeTasks(on, chatOn({}))
  const week = { period: 'week', tasks: 3, claude: 160000, self_est: 1390000, saved: 1230000, wh: 900, cost: null }
  on('process.run', (_: any, e: any) => {
    if (e.argv[1] === 'savings') return { value: { exitCode: 0, stdout: JSON.stringify(week), stderr: '' } }
    if (e.argv[1] === 'chat' && e.argv[2] === 'url')
      return { value: { exitCode: 0, stdout: 'https://box.tail.ts.net/#token=abc\n', stderr: '' } }
    return { value: { exitCode: 0, stdout: '', stderr: '' } }
  })
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.advance(3000)
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  const texts = (await ui.findAll({ type: 'Text' })).map(t => t.text)
  const at = texts.indexOf('this week: saved ~1.2M Claude tokens (88%, estimate) · 900 Wh')
  expect(at).toBeGreaterThan(-1)
  expect(texts.slice(at + 1)).toEqual([])                                     // never under the tasks (the user, 2026-11-19)
  await ui.press({ key: 'tab-settings' })
  expect((await ui.findAll({ type: 'Text' })).map(t => t.text).slice(-3)).toEqual(['web chat https://box.tail.ts.net/#token=abc',
    'web chat ', 'https://box.tail.ts.net/#token=abc'])
  await ui.press({ key: 'tab-tasks' })
  await ui.unmount()
})

test('the saving reads in thousands or millions, says when it cost more, and is empty with nothing to show', () => {
  const t = (o: object) => JSON.stringify({ tasks: 1, claude: 50000, self_est: 80000, saved: 30000, wh: 0, ...o })
  expect(savedLine(t({}))).toBe('this week: saved ~30K Claude tokens (38%, estimate)')
  expect(savedLine(t({ wh: 1500 }))).toBe('this week: saved ~30K Claude tokens (38%, estimate) · 1.5 kWh')
  expect(savedLine(t({ saved: -4000 }))).toBe('this week: cost ~4K more Claude tokens (estimate)')
  expect(savedLine(t({ tasks: 0 }))).toBe('')
  expect(savedLine('usage: wb …')).toBe('')
})

test('a night\'s backlog keeps the user away past six hours; without one, away lapses', () => {
  // #110 (2026-11-18): away lapsed after 6 h, and an overnight batch's permission prompts then
  // waited in the terminal until morning
  const t0 = 1_800_000_000_000, hour = 3600_000
  const night = (phase: string, backlog = 'night-1') => ({ name: 'n', phase, backlog } as any)
  const ours = () => true
  expect(awayNow(t0, t0 + 5 * hour, [], ours)).toBe(true)                          // within six hours
  expect(awayNow(t0, t0 + 7 * hour, [], ours)).toBe(false)                         // lapsed
  expect(awayNow(t0, t0 + 7 * hour, [night('running')], ours)).toBe(true)          // the batch runs on
  expect(awayNow(t0, t0 + 7 * hour, [night('review')], ours)).toBe(true)           // or waits on Claude
  expect(awayNow(t0, t0 + 7 * hour, [night('accepted')], ours)).toBe(false)        // done: lapsed
  expect(awayNow(t0, t0 + 7 * hour, [night('running', '')], ours)).toBe(false)     // not a backlog task
  expect(awayNow(t0, t0 + 7 * hour, [night('running')], () => false)).toBe(false)  // another session's
  expect(awayNow(0, t0, [night('running')], ours)).toBe(false)                     // never wrote from the web
})

test('Claude coding: its own edit in a task\'s worktree waits for the user\'s yes, unless Claude decides', async ($, on) => {
  // #92: the user's choice of when Claude writes a delegated task's code
  const clock = mock.clock(on, { now: 1_800_000_000_000 })
  const ts = 1_800_000_000
  const files: Record<string, string> = {
    '/wb/tasks/fix-it/task.json': JSON.stringify({ name: 'fix-it', model: 'm', created_at: ts }),
    '/wb/tasks/fix-it/status.json': JSON.stringify({ phase: 'review', round: 1, updated_at: ts }),
  }
  fakeTasks(on, files)
  on('process.run', () => ({ value: { exitCode: 0, stdout: '', stderr: '' } }))
  on('tool.call', () => ({ result: {}, text: '' }))                // the engine's own tool
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.advance(1000)
  const edit = (path = '/wb/tasks/fix-it/worktree/power.py') =>
    $.tool.call({ tool: 'Edit', file_path: path, old_string: 'w', new_string: 'w / 1000' } as any) as Promise<any>
  expect((await edit()).deny).toBeUndefined()                      // no saved choice: as before
  files['/wb/coding.json'] = JSON.stringify({ mode: 'ask' })
  expect((await edit()).deny).toContain('"Ask me first"')
  expect((await edit('/home/u/project/power.py')).deny).toBeUndefined()   // not a task's: Claude's own
  files['/wb/tasks/fix-it/coding.json'] = JSON.stringify({ allowed: [{ edit: 'w / 1000', used: null }] })
  expect((await edit()).deny).toBeUndefined()                      // the user said yes
  files['/wb/tasks/fix-it/coding.json'] = JSON.stringify({ allowed: [{ edit: 'w / 1000', used: ts }] })
  files['/wb/coding.json'] = JSON.stringify({ mode: 'only' })
  expect((await edit()).deny).toContain('report the blocker to the user')   // handed back: spent
  const w = await $.tool.call({ tool: 'Write', file_path: '/wb/tasks/fix-it/worktree/new.py', content: '' } as any) as any
  expect(w.deny).toContain('"Only when I say"')
  // a worktree nested for a sibling two levels up (#261, the audit of 1.2) is the task's too
  expect((await edit('/wb/tasks/fix-it/tree/apps/worktree/power.py')).deny).toContain('"Only when I say"')
  expect((await edit('/wb/tasks/fix-it/tree/shared/lib/power.py')).deny).toBeUndefined()   // a sibling's link
})

test('turning the probe experiment on without a probe that has symbols says so in a toast', async ($, on) => {
  // the user, 2026-11-20: the row read "On" while wb said it changed nothing yet
  const clock = mock.clock(on, { now: 1_800_000_000_000 })
  const files: Record<string, string> = {}
  fakeTasks(on, files, [])
  const toasts: string[] = []
  on('ui.toast', (_: any, e: any) => { toasts.push(e.text); return { value: undefined } })
  let found = false
  on('process.run', (_: any, e: any) => {
    if (e.argv[1] !== 'experiment') return { value: { exitCode: 0, stdout: '', stderr: '' } }
    files['/wb/experiments.json'] = JSON.stringify({ probe: e.argv[3] })
    const said = e.argv[3] === 'off' ? 'probe: off (wb experiment probe on)'
      : found ? 'probe: on, found (probe-code 0.6.0)'
      : 'probe: on, but no probe with `symbols` is on PATH, so it changes nothing yet (its release binary: github.com/probelabs/probe/releases)'
    return { value: { exitCode: 0, stdout: `${said}; tasks already started keep theirs\n`, stderr: '' } }
  })
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.advance(1000)
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  await ui.press({ key: 'tab-settings' })
  await ui.press({ key: 'probe' })
  await ui.press({ key: 'probe:on' })
  expect(toasts.filter(t => t.includes('probe'))).toEqual([
    'workbench: probe: on, but no probe with `symbols` is on PATH, so it changes nothing yet (its release binary: github.com/probelabs/probe/releases); tasks already started keep theirs'])
  await ui.press({ key: 'probe' })
  await ui.press({ key: 'probe:off' })
  found = true
  await ui.press({ key: 'probe' })
  await ui.press({ key: 'probe:on' })
  expect(toasts.filter(t => t.includes('probe'))).toHaveLength(1)   // off, or on and found: nothing to say
  await ui.unmount()
})

test('the settings rows open to Buttons, pick through wb, and say the sentence of the option the ring is on', async ($, on) => {
  // #93, and the user, 2026-11-19: a Select's sentence followed only the saved choice, and the options
  // should take a click or the keys
  const clock = mock.clock(on, { now: 1_800_000_000_000 })
  const files: Record<string, string> = {}
  fakeTasks(on, files, [])
  const runs: string[][] = []
  const ring: any[] = []
  on('ui.focus', (_: any, e: any) => { ring.push(e); return { value: {} } })   // the engine, beneath: the ring moves
  on('process.run', (_: any, e: any) => {
    runs.push(e.argv)
    if (e.argv[1] === 'chat' && e.argv[2] === 'on') files['/wb/chat/switch.json'] = JSON.stringify({ on: true })
    if (e.argv[1] === 'coding') files['/wb/coding.json'] = JSON.stringify({ mode: e.argv[2] })  // what wb writes
    if (e.argv[1] === 'experiment') files['/wb/experiments.json'] = JSON.stringify({ [e.argv[2]]: e.argv[3] })
    if (e.argv[1] === 'chat' && e.argv[2] === 'url')
      return { value: { exitCode: 0, stdout: 'http://localhost:1337/#token=abc\n', stderr: '' } }
    return { value: { exitCode: 0, stdout: '', stderr: '' } }
  })
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.advance(1000)
  for (const rows of [24, 8]) {                                      // the apprentice in pixels, then its face
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal', props: { ...PANE.props, scroll: { offset: 0, bodyRows: rows } } })
    expect(await ui.find({ key: 'coding' })).toBeUndefined()                                           // a tab of their own
    expect((await ui.findAll({ type: 'Button' })).slice(-3).map(b => b.props.label)).toEqual(['Tasks', 'Stats', 'Settings'])  // last
    await ui.press({ key: 'tab-settings' })
    expect((await ui.find({ key: 'coding' }))?.props.label).toBe('▸ Claude coding: Let Claude decide (default)')  // no saved choice
    expect((await ui.find({ key: 'chat' }))?.props.label).toBe('▸ Web chat: Off (default)')                      // off by default
    expect((await ui.find({ key: 'probe' }))?.props.label).toBe('▸ Agent code navigation (experimental, may go away): Off (default)')  // opt-in
    expect(await ui.find({ key: 'coding:ask' })).toBeUndefined()                                       // one row each at rest
    expect(await ui.find({ type: 'Text', text: /web chat / })).toBeUndefined()                         // and no link
    await ui.press({ key: 'tab-tasks' })
    await ui.unmount()
  }
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  await ui.press({ key: 'tab-settings' })
  await ui.press({ key: 'coding' })
  expect((await ui.findAll({ type: 'Button' })).map(b => b.props.label).filter((l: string) => / {2}[❯ ] /.test(l)))
    .toEqual(['    Only when I say', '    Ask me first', '  ❯ Let Claude decide (default)'])
  const says = async () => ((await ui.find({ key: 'coding-says' })) as any)?.children?.[0]?.children?.[0]?.trim()
  expect(await says()).toBe('Claude uses its judgment to guide the local agent or make a fix itself.')  // the chosen one's
  await $.ui.focus({ component: 'Pane', requestId: 'workbench', element: 'coding:only', origin: { kind: 'person' } } as any)  // the person's Tab, as the engine raises it
  expect(await says()).toBe('You tell Claude when to write code.')                                         // the ring's
  await ui.press({ key: 'coding:ask' })
  expect(runs.at(-1)).toEqual(['wb', 'coding', 'ask'])
  expect((await ui.find({ key: 'coding' }))?.props.label).toBe('▸ Claude coding: Ask me first')
  expect(await ui.find({ key: 'coding:ask' })).toBeUndefined()
  await ui.press({ key: 'chat' })
  await ui.press({ key: 'chat:on' })
  expect(runs.some(r => r.join(' ') === 'wb chat on')).toBe(true)
  expect((await ui.find({ key: 'chat' }))?.props.label).toBe('▸ Web chat: On')
  await ui.press({ key: 'probe' })
  await ui.press({ key: 'probe:on' })
  expect(runs.at(-1)).toEqual(['wb', 'experiment', 'probe', 'on'])
  expect((await ui.find({ key: 'probe' }))?.props.label).toBe('▸ Agent code navigation (experimental, may go away): On')
  const texts = (await ui.findAll({ type: 'Text' })).map(t => t.text)
  expect(texts.at(-1)).toBe('http://localhost:1337/#token=abc')                                   // the link, at once
  await ui.press({ key: 'tab-tasks' })
  expect(await ui.find({ type: 'Text', text: /token=abc/ })).toBeUndefined()                    // not under the tasks
  await ui.press({ key: 'tab-settings' })
  await ui.unmount()
  const phone = await $.ui.mount({ ...PANE, surface: 'mobile' })    // the same rows there
  expect((await phone.find({ key: 'coding' }))?.props.label).toBe('▸ Claude coding: Ask me first')
  await phone.unmount()
})

const FEED = {
  ts: 1_800_000_000, tasks: 9, settings: { recording: true, live: true, every: 2 },
  periods: {
    week: { tasks: 4, landed: 3, accepted_pct: 75, savings: { saved: 1_230_000, wh: 900, cost: null }, score: { score: 62 } },
    all: { tasks: 9, landed: 8, accepted_pct: 89 },
  },
  streak: { current: 0, best: 3, days: 5 },
  absorbed: { auto_fixes: 7, auto_fixed_tasks: 3, questions: 2 },
  models: { 'ollama/m': { tasks: 9, accepted_pct: 89, rounds: 1.4, minutes: 14, speed: { write: 38 },
    write_trend: [[1, 30], [2, 40]], write_change_pct: 12 } },
  days: [{ day: '2026-11-19', tasks: 0 }, { day: '2026-11-20', tasks: 2 }],
  guards: { silence: 1, repeat: 2 },
}

test('the Stats tab draws the history, read from wb stats only while it is shown and again when a task closes', async ($, on) => {
  // #406: no timer, and nothing at all while another tab, or no pane, is shown
  const clock = mock.clock(on, { now: 1_800_000_000_000 })
  const files: Record<string, string> = { '/wb/results.jsonl': '{}\n' }
  const pane = fakeTasks(on, files, [])
  const reads: string[][] = []
  on('process.run', (_: any, e: any) => {
    if (e.argv[1] === 'stats' && e.argv[2] === '--json') {
      reads.push(e.argv)
      return { value: { exitCode: 0, stdout: JSON.stringify(FEED), stderr: '' } }
    }
    return { value: { exitCode: 0, stdout: '', stderr: '' } }
  })
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  pane.show(true)                                                        // the pane open, on its Tasks tab
  await clock.advance(31_000)
  expect(reads.length).toBe(0)                                           // the Tasks tab: never read
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal', props: { ...PANE.props, bodyColumns: 200 } })
  await ui.press({ key: 'tab-stats' })
  await clock.advance(10)
  expect(reads.length).toBe(1)
  const texts = (await ui.findAll({ type: 'Text' })).map(t => t.text)
  expect(texts).toContain('This week: score 62/100 · saved ~1.2M Claude tokens (estimate) · 4 tasks · 3 landed (75%) · 900 Wh')
  expect(texts).toContain('Streak: best: 3 days in a row with a task landed')          // a run that ended isn't said
  expect(texts.some(t => t.includes('the same call repeated 2 · silence 1'))).toBe(true)
  expect(texts.some(t => t.includes('writes ~38 tok/s ▆█ (+12%)'))).toBe(true)
  await clock.advance(31_000)
  expect(reads.length).toBe(1)                                           // nothing changed: not read again
  files['/wb/results.jsonl'] = '{}\n{}\n'                                // a task closed
  await clock.advance(31_000)
  expect(reads.length).toBe(2)
  pane.show(false)                                                       // put away
  files['/wb/results.jsonl'] = '{}\n{}\n{}\n'
  await clock.advance(31_000)
  expect(reads.length).toBe(2)
  await ui.press({ key: 'tab-tasks' })
  await ui.unmount()
})

test('the Stats tab\'s settings pick through wb, progress checks stay off by default, and the price is typed', async ($, on) => {
  const clock = mock.clock(on, { now: 1_800_000_000_000 })
  const files: Record<string, string> = {}
  fakeTasks(on, files, [])
  const runs: string[][] = []
  on('process.run', (_: any, e: any) => {
    runs.push(e.argv)
    if (e.argv[1] === 'stats' && e.argv[2] === 'set')
      files['/wb/stats.json'] = JSON.stringify({ [e.argv[3]]: e.argv[3] === 'every' ? Number(e.argv[4]) : e.argv[4] === 'on' })
    if (e.argv[1] === 'savings' && e.argv[2] === '--price')
      return { value: { exitCode: 0, stdout: JSON.stringify({ tasks: 0, price: Number(e.argv[3]) }), stderr: '' } }
    return { value: { exitCode: 0, stdout: '', stderr: '' } }
  })
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.advance(1000)
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  await ui.press({ key: 'tab-settings' })
  expect((await ui.find({ key: 'record' }))?.props.label).toBe('▸ Stats recording: On (default)')
  expect((await ui.find({ key: 'live' }))?.props.label).toBe('▸ Live view: On (default)')
  expect((await ui.find({ key: 'every' }))?.props.label).toBe('▸ Live refresh: 2 s (default)')
  expect((await ui.find({ key: 'progress' }))?.props.label).toBe('▸ Progress checks: Off (default)')
  await ui.press({ key: 'record' })
  await ui.press({ key: 'record:off' })
  expect(runs.some(r => r.join(' ') === 'wb stats set recording off')).toBe(true)
  expect((await ui.find({ key: 'record' }))?.props.label).toBe('▸ Stats recording: Off')
  await ui.press({ key: 'every' })
  await ui.press({ key: 'every:5' })
  expect(runs.some(r => r.join(' ') === 'wb stats set every 5')).toBe(true)
  await ui.press({ key: 'progress' })
  await ui.press({ key: 'progress:on' })
  expect(runs.some(r => r.join(' ') === 'wb progress on')).toBe(true)
  await ui.input({ key: 'price', text: 'cheap' })                        // not a number: wb isn't asked
  expect(runs.some(r => r[2] === '--price')).toBe(false)
  await ui.input({ key: 'price', text: '$0.30' })
  expect(runs.some(r => r.join(' ') === 'wb savings --price 0.3 --json')).toBe(true)
  expect((await ui.find({ key: 'price' }))?.props.value).toBe('0.3')
  await ui.press({ key: 'tab-tasks' })
  await ui.unmount()
})

const TICK = {
  ts: 1_800_000_000, every: 2,
  gpus: [{ index: 0, name: 'Advanced Micro Devices, Inc. [AMD/ATI] Navi 48 [Radeon RX 9070 XT]', used: 2 ** 33, total: 2 ** 34, busy: 40, watts: 120 }],
  running: [{ task: 'fix-it', model: 'ollama/m', label: 'Model M', phase: 'running', round: 2, round_s: 75, tok_s: 41.2, tok_s_from: 'live', context_pct: 31 }],
  rack: [{ model: 'ollama/m', label: 'Model M', state: 'resident', gpu_bytes: 2 ** 33, write_tok_s: 40, read_tok_s: 900, load_s: 9, tasks: ['fix-it'] },
    { model: 'ollama/n', label: 'Model N', state: 'idle', write_tok_s: 20 }],
  queue: [{ task: 'later', model: 'ollama/n', waited_s: 30 }],
}

test('the live view runs wb top only while the Stats tab is on screen with Live view on, and ends it when left', async ($, on) => {
  // #408: hidden, minimized, another tab or Live view off: no child at all
  const clock = mock.clock(on, { now: 1_800_000_000_000 })
  const files: Record<string, string> = { '/wb/results.jsonl': '' }
  const pane = fakeTasks(on, files, [], true, [], false)
  let starts = 0, ends = 0
  on('process.spawn', async function* (_: any, e: any) {
    if (e.argv[1] !== 'top') {                                           // wb events, as fakeTasks's
      yield { stream: 'stdout', text: '{"hello": 4242}\n' }
      await new Promise(() => {})
    }
    starts += 1
    try {
      for (;;) {
        yield { stream: 'stdout', text: JSON.stringify(TICK) + '\n' }
        await new Promise(r => setTimeout(r, 5))
      }
    } finally {
      ends += 1
    }
  })
  on('process.run', (_: any, e: any) => ({ value: { exitCode: 0, stdout: e.argv[1] === 'stats' ? JSON.stringify({ tasks: 0 }) : '', stderr: '' } }))
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  pane.show(true)
  await clock.advance(31_000)
  expect(starts).toBe(0)                                                 // the Tasks tab
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal', props: { ...PANE.props, bodyColumns: 200 } })
  await ui.press({ key: 'tab-stats' })
  await new Promise(r => setTimeout(r, 30))
  expect(starts).toBe(1)
  const texts = (await ui.findAll({ type: 'Text' })).map(t => t.text)
  expect(texts.some(t => t.startsWith('GPU 0 Radeon RX 9070 XT ██████░░░░░░ 8.0/16.0 GiB · busy ███▎░░░░ 40% · 120 W'))).toBe(true)
  expect(texts.some(t => t.includes('fix-it Model M · running · round 2 · 1m15s · 41 tok/s · context 31%'))).toBe(true)
  expect(texts.some(t => t.includes('● Model M resident 8.0 GiB on the GPU · writes ~40 · reads ~900 · loads ~9s ← fix-it'))).toBe(true)
  expect(texts.some(t => t.includes('◌ later waits for the GPU (30s)'))).toBe(true)
  await clock.advance(31_000)
  expect(starts).toBe(1)                                                 // one child, not one a poll
  await ui.press({ key: 'tab-tasks' })
  await new Promise(r => setTimeout(r, 30))
  expect(ends).toBe(1)                                                   // left: ended
  files['/wb/stats.json'] = JSON.stringify({ live: false })
  await ui.press({ key: 'tab-stats' })
  await clock.advance(31_000)
  await new Promise(r => setTimeout(r, 30))
  expect(starts).toBe(1)                                                 // Live view off: never started
  expect((await ui.findAll({ type: 'Text' })).some(t => t.text === 'Now')).toBe(false)
  await ui.press({ key: 'tab-tasks' })
  await ui.unmount()
})

test('the history says nothing it has no data for, and a new install how to begin', () => {
  expect(statsLines(null)).toEqual([{ label: '', text: 'Reading the history…' }])
  expect(statsLines({ tasks: 0 })[0].text).toMatch(/^No finished tasks yet/)
  const lines = statsLines({ tasks: 1, periods: { week: { tasks: 1, landed: 1, accepted_pct: 100 } } })
  expect(lines).toEqual([{ label: 'This week', text: '1 task · 1 landed (100%)' }])   // no savings, no zeros
  expect(spark([0, 0])).toBe('')
  expect(spark([1, 4, 8])).toBe('▂▅█')
  expect(priceOf('{"price": 0.3}')).toBe(0.3)
  expect(priceOf('{"price": null}')).toBe(null)
  expect(priceOf('not json')).toBe(null)
})

test('a one-task exception shows beside its task and is under way from Claude\'s first edit', async ($, on) => {
  // #94: the next hand-back spends it once Claude has started on it
  const clock = mock.clock(on, { now: 1_800_000_000_000 })
  const ts = 1_800_000_000
  const files: Record<string, string> = {
    '/wb/coding.json': JSON.stringify({ mode: 'only' }),
    '/wb/tasks/fix-it/task.json': JSON.stringify({ name: 'fix-it', model: 'm', created_at: ts }),
    '/wb/tasks/fix-it/status.json': JSON.stringify({ phase: 'review', round: 2, updated_at: ts }),
    '/wb/tasks/fix-it/coding.json': JSON.stringify({ allowed: [
      { edit: 'divide by 1000', said: 'ok', when: 'the next try still shows kW as W', used: null }] }),
  }
  fakeTasks(on, files)
  on('process.run', () => ({ value: { exitCode: 0, stdout: '', stderr: '' } }))
  on('tool.call', () => ({ result: {}, text: '' }))
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await clock.advance(3000)
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  const said = (await ui.findAll({ type: 'Text' })).map(t => t.text.trim()).join(' ')
  expect(said).toContain('Claude may, once, if the next try still shows kW as W: divide by 1000')  // whole, wrapped
  await ui.unmount()
  const r = await $.tool.call({ tool: 'Edit', file_path: '/wb/tasks/fix-it/worktree/power.py', old_string: 'w', new_string: 'w / 1000' } as any) as any
  expect(r.deny).toBeUndefined()
  expect(JSON.parse(files['/wb/tasks/fix-it/coding.json']).allowed[0].started).toBe(1_800_000_003)
  await clock.advance(1000)                                        // the poll the call started settles
})

test('the strip above the prompt stays the engine\'s until the user closes the pane', async ($, on) => {
  // the user, 2026-11-19: closed by hand the pane minimizes to a strip, as backseat-driver's. A test
  // can't raise the person's ui.close, so this holds the other half: no strip while it is up
  const clock = mock.clock(on, { now: 1_800_000_000_000 })
  fakeTasks(on, {}, [])
  on('skill.prompt', (_: any, e: any) => ({ text: e.text }))
  on('process.run', () => ({ value: { exitCode: 0, stdout: '', stderr: '' } }))
  on('ui.render', { component: 'AbovePrompt' }, ($: any, e: any) => { const { Text } = $.ui.resolve(e); return <Text>engine</Text> })
  const BAND = { plugin: 'workbench', component: 'AbovePrompt', requestId: 'above-prompt', props: { bodyColumns: 120 } } as any
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await $.skill.prompt({ skill: 'workbench', text: 'x', frontmatter: {}, source: 'userSettings' })
  await clock.advance(1000)
  const band = await $.ui.mount({ ...BAND, surface: 'terminal' })
  expect(await band.find({ key: 'restore' })).toBeUndefined()
  expect((await band.find({ type: 'Text' }))?.text).toBe('engine')
  await band.unmount()
})
