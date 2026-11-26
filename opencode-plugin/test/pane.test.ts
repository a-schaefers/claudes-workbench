import { expect, test } from 'bun:test'
import { capOf, COMMAND, diffLines, LINK_MS, LIVE_MS, linkOf, PaneData, parseLines, ROLES_MS, scanTasks, startPane, workbench,
  WorkbenchPane, type Files, type PaneTask } from '../pane.ts'
import { CHANGE, driverRow, footer, LinkPoll, mine, MODELS, modelineOf, panelRows, Poll, runOf, scoped, separator, sidebar, target,
  type Here } from '../pane-view.ts'
import { ROWS } from '../../mod/hooks/apprentice.ts'
import { kernel } from '../../mod/hooks/core.ts'
import { linkDown } from '../../mod/hooks/modeline.ts'
import type { Run } from '../driver.ts'

// Workbench's panel, sidebar and footer in OpenCode (#1312, #1332), under OpenCode's own Bun:
// `BUN_BE_BUN=1 opencode test opencode-plugin/test`. The drawing itself (pane.tsx) needs
// OpenCode's TUI; what it draws is pane-view.ts's, and the data pane.ts's, both checked here.

const HOME = '/state'
// the phases by name from the kernel's tables (store.py's): the plugin spells none of its own
// (`kernel.phase_literals`), so a renamed phase fails here, not in a user's panel
const T = kernel.tables as { openOrder: string[]; closed: string[] }
const [RUNNING, , , QUEUED, REVIEW, STUCK] = T.openOrder as [string, string, string, string, string, string, string]
const ACCEPTED = T.closed[0]!
const NOW = 1_800_000_000_000
// a state dir of tasks: name -> [task.json, status.json]
function files(tasks: Record<string, [object, object]>, reads: string[] = []): Files {
  return {
    dirs: path => (path === `${HOME}/tasks` ? Object.keys(tasks) : []),
    read: path => {
      reads.push(path)
      const m = path.match(/^\/state\/tasks\/([^/]+)\/(task|status)\.json$/)
      const t = m ? tasks[m[1]!] : undefined
      return t ? JSON.stringify(m![2] === 'task' ? t[0] : t[1]) : undefined
    },
  }
}
const spec = (name: string, more: object = {}) => ({ name, created_at: NOW / 1000 - 600, model: 'gpu/m', model_label: 'm', ...more })
const status = (phase: string, more: object = {}) => ({ phase, updated_at: NOW / 1000 - 60, round: 1, last: 'Writing x', ...more })

test('the scan lists each open task with its view facts and owner keys, a closed one for half an hour', () => {
  const reads: string[] = []
  const gone = new Set<string>()
  const fs = files({
    'fix-it': [spec('fix-it', { session: 'ses_a', driver: { tool: 'opencode' }, cwd: '/p', repo: '/p', group: 'g', auto_fix: 2 }),
      status(RUNNING, { tools: 3, tok_out: 1200, auto_fixes: 1 })],
    'old-one': [spec('old-one'), status(ACCEPTED, { updated_at: NOW / 1000 - 3600 })],
    'new-one': [spec('new-one'), status(ACCEPTED)],
    replay: [spec('replay', { bench: true }), status(RUNNING)],
  }, reads)
  const list = scanTasks(HOME, NOW, gone, fs)
  expect(list.map(t => t.name)).toEqual(['fix-it', 'new-one'])
  const t = list[0]!
  expect([t.phase, t.session, t.driver, t.cwd, t.group, t.tools, t.tokOut, t.autoFixes, t.autoFixMax])
    .toEqual([RUNNING, 'ses_a', 'opencode', '/p', 'g', 3, 1200, 1, 2])
  // closed past the half hour: never read again (the mod's rule, the 2026-11-16 audit)
  expect([...gone]).toEqual(['old-one'])
  reads.length = 0
  scanTasks(HOME, NOW, gone, fs)
  expect(reads.some(p => p.includes('/old-one/'))).toBe(false)
})

test('the panel\'s data reads a stopped task\'s transcript again once it moved, a running one every LIVE_MS, moved or not', async () => {
  let now = NOW
  const calls: string[][] = []
  const tasks: Record<string, [object, object]> = { 'fix-it': [spec('fix-it'), status(RUNNING)] }
  const run: Run = async args => {
    calls.push([...args])
    return { code: 0, stdout: '{"s":"agent","t":"hello"}\nnot json\n{"s":"tool","t":"edit x"}\n', stderr: '' }
  }
  const data = new PaneData(HOME, run, () => now, files(tasks))
  const first = await data.transcript('fix-it')
  expect(first.lines).toEqual([{ s: 'agent', t: 'hello' }, { s: 'tool', t: 'edit x' }])
  expect(calls).toEqual([['watch', 'fix-it', '--json', '--last', '600']])
  tasks['fix-it']![1] = status(RUNNING, { updated_at: NOW / 1000 - 5 })
  await data.transcript('fix-it')
  expect(calls.length).toBe(1)          // moved, but read LIVE_MS ago
  now += LIVE_MS
  await data.transcript('fix-it')
  expect(calls.length).toBe(2)
  await data.transcript('fix-it')
  expect(calls.length).toBe(2)          // read under LIVE_MS ago
  // a long step writes no status, but its session moves: read again (the mod's #691 rule)
  now += LIVE_MS
  await data.transcript('fix-it')
  expect(calls.length).toBe(3)
  // stopped: read once it moved, then not until it moves again
  now += LIVE_MS
  tasks['fix-it']![1] = status(REVIEW, { updated_at: NOW / 1000 })
  await data.transcript('fix-it')
  expect(calls.length).toBe(4)
  now += LIVE_MS
  await data.transcript('fix-it')
  expect(calls.length).toBe(4)          // unchanged since
  // a name wb would never give runs nothing: never an option or a path
  expect((await data.transcript('--all')).why).toBe("--all isn't a task's name")
  expect((await data.diff('../x')).why).toBe("../x isn't a task's name")
  expect(calls.length).toBe(4)
})

test('the day cap is read only while an API task works, and kept ROLES_MS', async () => {
  let now = NOW
  const calls: string[][] = []
  const tasks: Record<string, [object, object]> = { a: [spec('a'), status(RUNNING)] }
  const run: Run = async args => {
    calls.push([...args])
    return { code: 0, stdout: JSON.stringify({ day_usd: 5, spend_today: { usd: 0.42, prices: 'yours' } }), stderr: '' }
  }
  const data = new PaneData(HOME, run, () => now, files(tasks))
  expect((await data.tasks()).cap).toBe(null)
  expect(calls).toEqual([])
  tasks.b = [spec('b', { via: 'api' }), status(RUNNING)]
  expect((await data.tasks()).cap).toEqual({ spent: 0.42, cap: 5, est: false })
  await data.tasks()
  expect(calls.length).toBe(1)
  now += ROLES_MS
  await data.tasks()
  expect(calls.length).toBe(2)
})

test('wb\'s answers read as the panel draws them: diff lines styled, the cap, the link', () => {
  expect(diffLines('diff --git a/x b/x\n@@ -1 +1 @@\n-old\n+new\n same\n').map(l => l.s)).toEqual(['head', 'head', 'del', 'add', 'out'])
  expect(capOf('{"spend_today":{"usd":1.5},"day_usd":null}')).toEqual({ spent: 1.5, cap: null, est: true })
  expect(capOf('not json')).toBe(null)
  expect(linkOf({ code: 0, stdout: '{"state":"up","url":"http://h:1/?t=x"}', stderr: '' })).toEqual({ state: 'up', url: 'http://h:1/?t=x' })
  expect(linkOf({ code: 2, stdout: '', stderr: 'boom\nwb: no chat' })).toEqual(linkDown('wb: no chat'))
  expect(parseLines('{"s":"a","t":"1"}\n{"s":"b","t":"2"}\n', 1)).toEqual([{ s: 'b', t: '2' }])
})

test('/workbench applies the pin, switches the session to the Workbench agent, then sends what was typed after it', async () => {
  const did: unknown[] = []
  const host = {
    options: {},
    rpc: { register: async () => ({ dispose: async () => {} }) },
    session: {
      switchAgent: async (i: object) => { did.push(['agent', i]) },
      switchModel: async (i: object) => { did.push(['model', i]) },
      prompt: async (i: object) => { did.push(['prompt', i]) },
    },
  }
  const run: Run = async args => ({ code: 0, stdout: args[0] === 'route' ? '{"driver":"deepinfra/m"}' : '', stderr: '' })
  await workbench(host, { sessionID: 'ses_1', prompt: { text: '  add a flag  ' }, delivery: 'steer' }, run)
  // the pin first: the agent's selection is when the TUI says the driver's notes, the pinned model's
  expect(did).toEqual([
    ['model', { sessionID: 'ses_1', model: { providerID: 'deepinfra', id: 'm' } }],
    ['agent', { sessionID: 'ses_1', agent: 'workbench' }],
    ['prompt', { sessionID: 'ses_1', text: '  add a flag  ', delivery: 'steer' }],
  ])
  did.length = 0
  await workbench(host, { sessionID: 'ses_1', prompt: { text: '' }, delivery: 'steer' }, run)
  expect(did.map(d => (d as unknown[])[0])).toEqual(['model', 'agent'])
  // what the TUI attached goes with the request, even with nothing typed (the review of #1312)
  did.length = 0
  const files = [{ type: 'file', url: 'file:///p/a.ts' }]
  await workbench(host, { sessionID: 'ses_1', prompt: { text: '', files, agents: [] }, delivery: 'queue' }, run)
  expect(did.at(-1)).toEqual(['prompt', { sessionID: 'ses_1', text: '', files, agents: [], delivery: 'queue' }])
  // no Workbench agent: said, and nothing sent
  did.length = 0
  const refused = { ...host, session: { ...host.session, switchAgent: async () => { throw new Error('agent not found') } } }
  let said = ''
  await workbench(refused, { sessionID: 'ses_1', prompt: { text: 'x' }, delivery: 'steer' }, run).catch(e => { said = String(e) })
  expect(said.includes("the Workbench agent isn't there")).toBe(true)
  expect(did.map(d => (d as unknown[])[0])).toEqual(['model'])   // the pin, the user's own standing choice; nothing sent
})

test('the server part registers the panel\'s RPC and /workbench, and the RPC alone where commands aren\'t', async () => {
  const registered: string[] = []
  const commands: string[] = []
  const rpc = { register: async (def: { id: string; methods: object }) => {
    registered.push(def.id, ...Object.keys(def.methods))
    return { dispose: async () => {} }
  } }
  const run: Run = async () => ({ code: 0, stdout: '', stderr: '' })
  type Editor = { add(d: { name: string }): void }
  const stop = await startPane({ options: {}, rpc, command: { transform: async (cb: (e: Editor) => void) => {
    cb({ add: (d: { name: string }) => commands.push(d.name) })
    return { dispose: async () => {} }
  } } } as never, run)
  expect(registered).toEqual([WorkbenchPane.id, 'tasks', 'live', 'diff', 'link'])
  expect(commands).toEqual([COMMAND])
  await stop()
  registered.length = 0
  await startPane({ options: {}, rpc } as never, run)
  expect(registered.length).toBe(5)
})

// ---------------------------------------------------------------- pane-view.ts

test('the tasks are read by one loop however often a press asks for a read at once, and an older read in flight never draws', async () => {
  const pending = new Map<number, () => void>()
  let next = 0
  const timers = { set: (f: () => void) => { pending.set(++next, f); return next }, clear: (h: unknown) => { pending.delete(h as number) } }
  const answers: Array<(n: number) => void> = []
  const drawn: number[] = []
  const loop = new Poll(() => new Promise<number>(r => answers.push(r)), n => drawn.push(n), () => 2000, timers)
  const first = loop.now()
  answers[0]!(1); await first
  expect([drawn, pending.size]).toEqual([[1], 1])
  // three presses: still one timer, and the reads they overtook draw nothing
  const presses = [loop.now(), loop.now(), loop.now()]
  answers[3]!(4); answers[1]!(2); answers[2]!(3)
  await Promise.all(presses)
  expect([drawn, pending.size]).toEqual([[1, 4], 1])
  // the timer's own read is the same loop, and a stop ends it
  const tick = [...pending.values()][0]!
  tick(); answers[4]!(5); await Promise.resolve(); await Promise.resolve()
  expect([drawn, pending.size]).toEqual([[1, 4, 5], 1])
  loop.stop()
  expect(pending.size).toBe(0)
  await loop.now()
  expect(answers.length).toBe(5)
})

test('an open panel asks for the web app\'s link again each LINK_MS, so a web app started meanwhile reaches the modeline (#1518)', async () => {
  // a panel opened while the web app was down said `web app down` until it was closed and opened
  // again: the link was asked only as the panel drew (2026-11-26)
  let now = NOW, shown = true
  const pending = new Map<number, () => void>()
  let id = 0
  const timers = { set: (f: () => void, _ms: number) => { pending.set(++id, f); return id }, clear: (h: unknown) => { pending.delete(h as number) } }
  const answers = [linkDown('not started'), { state: 'up' as const, url: 'http://h/' }]
  let asked = 0
  const drawn: string[] = []
  const links = new LinkPoll(async () => answers[Math.min(asked++, 1)]!, l => drawn.push(l.state), () => shown, () => now, timers)
  await links.want()
  await links.want()                       // the splash's next layout, within the minute: no second ask
  expect([asked, drawn, pending.size]).toEqual([1, [answers[0]!.state], 1])
  now += LINK_MS
  const tick = [...pending.values()][0]!
  pending.clear()
  tick(); await Promise.resolve(); await Promise.resolve()
  expect([asked, drawn]).toEqual([2, [answers[0]!.state, 'up']])
  // the panel closed: the next tick asks nothing and schedules nothing
  expect(pending.size).toBe(1)
  shown = false
  now += LINK_MS
  const last = [...pending.values()][0]!
  pending.clear()
  last(); await Promise.resolve()
  expect([asked, pending.size]).toEqual([2, 0])
  links.stop()
})

const task = (name: string, phase: string, more: Partial<PaneTask> = {}): PaneTask => ({
  name, phase, round: 1, model: 'gpu/m', label: 'm', via: 'gpu', role: 'trivial', provider: '', tests: null,
  last: 'Writing x', createdAt: NOW - 600_000, updatedAt: NOW - 60_000, recent: [], parked: false,
  group: '', cards: [], tools: 0, tokOut: 0, ctxUsed: 0, window: 0, freshAt: null, autoFixes: 0, autoFixMax: 2,
  roundFor: '', reviews: 0, steps: 0, activeS: 0, roundStartedAt: 0, typicalMin: 0, typicalN: 0, lateMin: 0, spend: null,
  session: '', driver: '', cwd: '', repo: '', subdir: '', ...more,
})
const here: Here = { sessionID: 'ses_me', dir: '/p', home: '/h', fold: false }

test('a session\'s own tasks are its id\'s or near it, never another live session\'s, through the kernel', () => {
  expect(mine(task('a', RUNNING, { session: 'ses_me', driver: 'opencode', cwd: '/elsewhere' }), here)).toBe(true)
  expect(mine(task('b', RUNNING, { session: 'ses_other', driver: 'opencode', cwd: '/p' }), here)).toBe(false)
  expect(mine(task('c', RUNNING, { cwd: '/p/sub', repo: '/p' }), here)).toBe(true)  // by hand: by directory
  const list = [task('a', RUNNING, { session: 'ses_me' }), task('b', REVIEW, { session: 'ses_other' })]
  expect(scoped(list, here, false).map(t => t.name)).toEqual(['a'])
  expect(scoped(list, here, true).map(t => t.name)).toEqual(['a', 'b'])     // the mode lists every task
})

test('the footer item counts the open tasks by phase, a stopped one in error, and shows only with something to bring back', () => {
  expect(footer([], false)).toBe(null)
  expect(footer([task('a', ACCEPTED)], false)).toBe(null)
  expect(footer([], true)!.map(r => r.text)).toEqual(['⚒ wb'])
  const runs = footer([task('a', RUNNING), task('b', RUNNING), task('c', STUCK)], false)!
  expect(runs.map(r => r.text).join('')).toBe('⚒ wb · 2 running · 1 stuck')
  expect(runs.find(r => r.text === '1 stuck')!.fg).toBe('error')
})

test('the footer item leaves out the tasks /workbench-quit parked, as Claude Code\'s strip does (#1516)', () => {
  const parked = (name: string) => task(name, REVIEW, { parked: true })
  expect(footer([parked('a'), parked('b')], false)).toBe(null)
  expect(footer([parked('a'), task('b', RUNNING)], false)!.map(r => r.text).join('')).toBe('⚒ wb · 1 running')
  expect(footer([parked('a')], true)!.map(r => r.text)).toEqual(['⚒ wb'])  // the mode still shows its mark
})

test('the sidebar section heads its tasks with their counts and gives each line its task', () => {
  expect(sidebar([], 40, NOW, '✻', '')).toBe(null)
  const s = sidebar([task('fix-it', RUNNING), task('docs', REVIEW, { tests: 'pass' })], 40, NOW, '✻', '')!
  expect(s.head.map(r => r.text).join('')).toBe('Workbench (1 running, 1 review)')
  expect(s.rows.map(r => r.task)).toEqual(['fix-it', 'docs'])
  for (const r of s.rows) expect(r.runs.map(x => x.text).join('').length <= 40).toBe(true)
  // a head too long for the sidebar is cut to its width, never wrapped (seen at 140 columns)
  const many = sidebar([task('a', RUNNING), task('b', REVIEW), task('c', QUEUED), task('d', STUCK)], 30, NOW, '✻', '')!
  expect(many.head.map(r => r.text).join('').length <= 30).toBe(true)
})

test('the panel\'s rows are the Tasks view\'s, a press on each task\'s first line, none wider than the panel', () => {
  const rows = panelRows([task('fix-it', RUNNING, { tools: 4 }), task('docs', REVIEW)], 50, 40, NOW, 'fix-it')
  expect(rows.filter(r => r.first).map(r => r.task)).toEqual(['fix-it', 'docs'])
  for (const r of rows) expect(r.runs.map(x => x.text).join('').length <= 50).toBe(true)
  // the followed task's name is underlined, as Claude Code's pane draws it
  expect(rows[0]!.runs.some(x => x.text === 'fix-it' && x.underline === true)).toBe(true)
  // a short panel takes the one-line form (tasks.ts compactWanted)
  expect(panelRows([task('fix-it', RUNNING, { tools: 4 })], 50, 14, NOW, '').length).toBe(1)
  // a stopped task says Workbench steers it next, never Claude, in OpenCode
  const stuck = panelRows([task('fix-it', STUCK, { last: 'no progress' })], 80, 40, NOW, '').map(r => r.runs.map(x => x.text).join(''))
  expect(stuck.some(l => l.includes('Workbench steers it next'))).toBe(true)
  expect(stuck.some(l => l.includes('Claude'))).toBe(false)
  // the separator fills the row exactly, no ellipsis: `─` is one column
  expect([...separator('fix-it', 'diff', 40)].length).toBe(40)
  expect(separator('fix-it', 'diff', 40).startsWith('── fix-it · diff ──')).toBe(true)
  expect(separator('fix-it', 'diff', 40).endsWith('──')).toBe(true)
})

test('Claude Code\'s colour names become the theme\'s tokens', () => {
  expect(runOf({ text: 'x', color: 'claude', bold: true })).toEqual({ text: 'x', fg: 'accent', bold: true })
  expect(runOf({ text: 'x', color: 'suggestion' }).fg).toBe('info')
  expect(runOf({ text: 'x', dim: true }).fg).toBe('muted')
  expect(runOf({ text: 'x' })).toEqual({ text: 'x' })
})

test('the splash ends where the panel pins its modeline and apprentice: the foot, exactly', () => {
  const checking = { state: 'checking' as const }
  // the modeline the last row, the apprentice's ROWS above it
  expect(target(30, 60, checking, false)).toEqual({ stage: 30 - 1 - ROWS, bar: 29 })
  // the cap's line between them
  expect(target(30, 60, checking, true)).toEqual({ stage: 30 - 2 - ROWS, bar: 29 })
  // a link too long for the bar takes rows of its own under it
  const long = { state: 'up' as const, url: 'http://127.0.0.1:8765/?token=' + 'x'.repeat(90) }
  const head = 1 + modelineOf(long, 40).rows.length
  expect(head > 1).toBe(true)
  expect(target(30, 40, long, false)).toEqual({ stage: 30 - head - ROWS, bar: 30 - head })
  // a small panel: the face on one line, not the pixels
  expect(target(14, 60, checking, false)).toEqual({ stage: 12, bar: 13 })
  expect(modelineOf({ state: 'up', url: 'http://h/' }, 60).bar).toBe('[•_•] Workbench - http://h/')
})

test('no pane module hands OpenCode a task\'s worktree: no location, move or vcs call (the host-shape decision\'s critic 1)', async () => {
  for (const f of ['pane.ts', 'pane-view.ts', 'pane.tsx']) {
    const src = await Bun.file(new URL(`../${f}`, import.meta.url)).text()
    expect([/\.move\(/, /\bvcs\b/, /location:\s*\{/, /worktree/].filter(re => re.test(src.replace(/^\s*\/\/.*$/gm, '')))
      .map(String)).toEqual([])
  }
})

test('no Index row of the panel reads its item once at its top, so a row redraws when its line changes (#1472)', async () => {
  // Solid makes an Index row once per index and changes only its item: a `const r = f(l())` at
  // the row's top kept the first text, so `d` showed the transcript under the diff's separator
  // and a 600-line tail froze (2026-11-26). Each row's body before its JSX may read the item only
  // through a memo.
  let rows = 0
  for (const f of ['pane.tsx', 'splash.tsx']) {
    const src = (await Bun.file(new URL(`../${f}`, import.meta.url)).text()).replace(/^\s*\/\/.*$/gm, '')
    const stale = [...src.matchAll(/<Index each=\{[^\n]*\}>\s*\{\s*(\w+)\s*=>\s*\{([\s\S]*?)\breturn\b/g)].flatMap(m => {
      rows++
      return m[2]!.split('\n').filter(line => line.includes(`${m[1]}()`) && !line.includes('createMemo('))
    })
    expect([f, stale]).toEqual([f, []])
  }
  expect(rows > 0).toBe(true)  // the history's row is one: the scan finds what it checks
})

test('the panel\'s driver row says the session\'s model, its side and another pin, and offers OpenCode\'s own /models', () => {
  const text = (r: ReturnType<typeof driverRow>) => (r ?? []).map(x => x.text).join('')
  const api = { model: 'deepinfra/m', label: 'deepinfra/m#high', kind: 'api', pin: 'session' }
  expect(text(driverRow(api, 80))).toBe('driver: deepinfra/m#high (API) · change')
  expect(driverRow(api, 80)!.at(-1)).toEqual({ text: CHANGE, fg: 'interactive', underline: true })
  expect(MODELS).toBe('model.list')   // the TUI's /models (tui/src/app.tsx), which the row's press dispatches
  expect(text(driverRow({ ...api, kind: 'gpu', pin: 'ollama/q' }, 80))).toBe('driver: deepinfra/m#high (GPU) · pin ollama/q · change')
  expect(text(driverRow({ ...api, pin: 'deepinfra/m#high' }, 80))).toBe('driver: deepinfra/m#high (API) · change')
  // a model wb finds neither side of is still named; none known yet is said short
  expect(text(driverRow({ ...api, kind: 'unknown' }, 80))).toBe('driver: deepinfra/m#high · change')
  expect(text(driverRow({ label: 'not known yet (the plugin records it at the session\'s next request)', kind: 'unknown' }, 80)))
    .toBe('driver: not known yet · change')
  // narrow: the pin goes first, then the model is cut, and "change" stays
  expect(text(driverRow({ ...api, pin: 'ollama/q' }, 44))).toBe('driver: deepinfra/m#high (API) · change')
  const narrow = text(driverRow(api, 24))
  expect([narrow.length, narrow.endsWith(' · change'), narrow.includes('…')]).toEqual([24, true, true])
  // wb not answered, or failed: no row
  expect(driverRow(null, 80)).toBe(null)
  expect(driverRow({ label: '', error: 'wb not found' }, 80)).toBe(null)
})
