import { expect, mock, test } from 'claude-code/testing'
import { wrapWords } from './register.js'

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

test('/workbench opens the pane and counts open tasks', async ($, on) => {
  let opened = ''
  let closed = ''
  let held: any[] = []
  on('command.register', () => ({ value: { command: 'workbench' } }))
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

  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  const out = await $.command.run({ command: 'workbench', args: '' })
  expect(opened).toBe('workbench')
  expect(closed).toBe('')
  expect(JSON.stringify(out)).toContain('no open tasks')
  // closed by accident, the engine still holding it out of sight: /workbench brings it back
  held = [{ id: 'workbench', title: 'Workbench', isShown: false, isFocused: false, isPlaced: true }]
  opened = ''
  await $.command.run({ command: 'workbench', args: '' })
  expect([closed, opened]).toEqual(['workbench', 'workbench'])
})

// A file system in memory: what `wb` and its runner would have written. `ui` records the pane
// opening and closing and each status line.
function fakeTasks(on: any, files: Record<string, string>, dirs = ['fix-it'], scouted = true, ui: string[] = []) {
  // the weekly model check was done just now, unless the test is about it
  if (scouted) files['/wb/scout.json'] = JSON.stringify({ done_at: 1_800_000_000 })
  on('env.get', (_: any, e: any) => ({ value: ({ WB_HOME: '/wb', HOME: '/home/u' } as any)[e.name] }))
  on('fs.read', (_: any, e: any) => {
    if (!(e.path in files)) throw new Error(`ENOENT: ${e.path}`)
    return { value: files[e.path] }
  })
  on('fs.write', (_: any, e: any) => {
    files[e.path] = e.text
    return { value: undefined }
  })
  on('fs.exists', (_: any, e: any) => ({ value: Object.keys(files).some(p => p.startsWith(e.path)) }))
  on('fs.list', () => ({ value: dirs.map(name => ({ name, kind: 'dir' })) }))
  on('command.register', () => ({ value: { command: 'workbench' } }))
  on('ui.open', (_: any, e: any) => { ui.push(`open ${e.id}`); return { value: { isPlaced: true } } })
  on('ui.close', (_: any, e: any) => { ui.push(`close ${e.id}`); return { value: undefined } })
  on('ui.status', (_: any, e: any) => { ui.push(`status ${e.text ?? ''}`); return { value: undefined } })
  // `wb events`: says hello and then stays open, as the real watcher does
  on('process.spawn', async function* () {
    yield { stream: 'stdout', text: '{"hello": 4242}\n' }
    await new Promise(() => {})
    return { code: 0, signal: null }
  })
  on('session.start', (_: any, e: any) => ({ cwd: e.cwd }))
  on('turn.start', (_: any, e: any) => ({ turnId: e.turnId }))
  on('turn.complete', () => ({ text: '' }))
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
  expect(runs[0]).toEqual(['wb', 'watch', 'fix-it', '--json'])

  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect(await ui.find({ type: 'Text', text: /── fix-it · ↑↓ scrolls/ })).toBeTruthy()
  expect(await ui.find({ type: 'Text', text: 'Claude → agent · spec' })).toBeTruthy()
  expect(await ui.find({ type: 'Text', text: '    +42' })).toBeTruthy()
  expect(await ui.find({ type: 'Text', text: /Writing… 3\.0s · ↓ 45 tokens/ })).toBeTruthy()
  expect(await ui.find({ type: 'Text', text: /reasoning on/ })).toBeTruthy()
  expect(await ui.find({ type: 'Text', text: /~5m left/ })).toBeTruthy()
  // the web chat's link is the last line, so it stays in view while the pane follows the end
  const texts = await ui.findAll({ type: 'Text' })
  expect(texts[texts.length - 1].text).toBe('https://box.tail.ts.net/#token=abc')
  expect(texts[texts.length - 2].text).toBe('web chat ')
  await ui.unmount()

  // a running task is re-read every poll, since its session moves between status writes
  await clock.advance(3000)
  expect(runs.length).toBeGreaterThan(1)
})

test('/delegate-quit stops everything; /delegate-quit-and-hand-back-claude hands the tasks to Claude', async ($, on) => {
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

  const quit = await $.command.run({ command: 'delegate-quit', args: '' })
  expect(runs.at(-1)).toEqual(['wb', 'stop', '--all'])
  expect(quit.text).toContain('stopped: fix-it')
  expect(JSON.stringify(quit.context)).toContain("Don't finish, restart")  // Claude is told
  expect(submitted.length).toBe(0)                                         // without a turn

  await $.command.run({ command: 'delegate-quit-and-hand-back-claude', args: '' })
  expect(runs.at(-1)).toEqual(['wb', 'takeover', '--all'])
  await clock.advance(2000)
  expect(submitted.at(-1)).toContain('took over fix-it')                  // Claude starts now
  expect(submitted.at(-1)).toContain('Leave delegate mode')

  // Typed words never stop anything: only the commands do.
  const before = runs.length
  await $.prompt.submit({ text: 'if I said bail out claude, would it stop?' })
  expect(runs.length).toBe(before)
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
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
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

test('the weekly model check comes once, in a quiet moment of a session in use; /delegate-models asks now', async ($, on) => {
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
  expect(asked.length).toBe(1)
  expect(asked[0]).toContain('The weekly model check is due')
  expect(asked[0]).toContain('`wb scout`')
  expect(JSON.parse(files['/wb/scout.json']).offered_at).toBeGreaterThan(1_800_000_000)  // raised: not again today

  await clock.advance(10_000)
  expect(asked.length).toBe(1)

  // done this week: nothing more, until the user asks for it
  files['/wb/scout.json'] = JSON.stringify({ done_at: 1_800_000_010 })
  await $.command.run({ command: 'delegate-models', args: '' })
  await clock.advance(2000)
  expect(asked.length).toBe(2)
  expect(asked[1]).toContain('The weekly model check is due')
})

test('the web chat reaches Claude only in /delegate mode, with a receipt, and one session listens', async ($, on) => {
  const clock = mock.clock(on, { now: 1_800_000_000_000 })
  const files: Record<string, string> = {}
  fakeTasks(on, files, [])
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
  expect(asked.length).toBe(0)                                   // not in delegate mode: nobody listens
  expect(files['/wb/chat/listener.json']).toBeUndefined()

  await $.skill.prompt({ skill: 'delegate', text: 'x', frontmatter: {}, source: 'userSettings' })
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

  // /delegate-quit ends delegate mode, and with it the listening
  await $.command.run({ command: 'delegate-quit', args: '' })
  await clock.advance(20_000)
  expect(asked.length).toBe(1)
})

test('running wb counts as delegate mode, and a web message reaches a running turn', async ($, on) => {
  const clock = mock.clock(on, { now: 1_800_000_000_000 })
  const files: Record<string, string> = {}
  fakeTasks(on, files, [])
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
  expect(await ui.find({ type: 'Text', text: 'fix-it is ready for review.' })).toBeTruthy()
  expect(await ui.find({ type: 'Text', text: /^◆/ })).toBeTruthy()
  await ui.unmount()

  // no Raster on the desktop: the face on the line instead
  ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  expect(await ui.find({ type: 'Raster' })).toBeFalsy()
  expect(await ui.find({ type: 'Text', text: /\[\^_\^\]/ })).toBeTruthy()
  await ui.unmount()
})

test('/delegate alone turns delegate mode on for the session: the pane opens, and a quit ends it', async ($, on) => {
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
  expect((await $.prompt.context({ blocks: [] })).blocks.map((b: any) => b.name)).not.toContain('delegate-mode')

  await $.skill.prompt({ skill: 'delegate', text: 'x', frontmatter: {}, source: 'userSettings',
    path: '/home/u/.claude/skills/delegate/SKILL.md' })
  await clock.advance(2000)
  expect(panes()).toEqual(['open workbench'])                    // the mode's sign
  expect(status()).toBe('status ⚒ wb · delegate mode')
  const pane = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect(await pane.find({ type: 'Text', text: 'Delegate mode is on.' })).toBeTruthy()
  await pane.unmount()
  // a compacted or cleared conversation is told the mode is on, and where the skill is
  const block = (await $.prompt.context({ blocks: [] })).blocks.find((b: any) => b.name === 'delegate-mode')
  expect(block?.text).toContain('/home/u/.claude/skills/delegate/SKILL.md')
  // what Claude learned about the user, in any project, comes with the mode
  expect((await $.prompt.context({ blocks: [] })).blocks.map((b: any) => b.name)).not.toContain('user-notes')
  files['/wb/notes.md'] = '- Review strictly: reject any diff without tests\n'
  const notes = (await $.prompt.context({ blocks: [] })).blocks.find((b: any) => b.name === 'user-notes')
  expect(notes?.text).toContain('reject any diff without tests')

  await $.command.run({ command: 'delegate-quit', args: '' })
  await clock.advance(2000)
  expect(panes()).toEqual(['open workbench', 'close workbench'])
  expect(status()).toBe('status ')
  expect((await $.prompt.context({ blocks: [] })).blocks.map((b: any) => b.name)).not.toContain('delegate-mode')
  expect((await $.prompt.context({ blocks: [] })).blocks.map((b: any) => b.name)).not.toContain('user-notes')
})

test('the bubble wraps its words into lines that fit, never cutting a line short', () => {
  const said = 'read /home/me/.local/state/workbench/tasks/fix-parser/worktree/src/parser.c and then the tests'
  const lines = wrapWords(said, 30)
  expect(lines.every(l => l.length <= 30)).toBe(true)
  expect(lines.join(' ').replace(/\s+/g, '')).toBe(said.replace(/\s+/g, ''))  // nothing lost
  expect(wrapWords('All caught up.', 30)).toEqual(['All caught up.'])
  expect(wrapWords('', 30)).toEqual([''])
})
