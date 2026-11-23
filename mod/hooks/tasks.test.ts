import { expect, test } from 'claude-code/testing'
import { ago, bar, blocksOf, capRow, clip, compactWanted, ctxOf, etaOf, MARK, placeOf, stageOf, taskRows } from './tasks.js'
import type { Part, TaskFacts } from './tasks.js'

// The Tasks view for parallel work (#1048): the design study's mockups, from the facts of its
// scenes (scenes.py), drawn by taskRows character for character.
const NOW = 1_800_000_000_000
const MIN = 60_000

const fact = (o: Partial<TaskFacts> & { name: string }): TaskFacts => ({
  phase: 'running', round: 1, model: '', label: '', via: 'gpu', role: '', provider: '', tests: null, last: '',
  createdAt: NOW, updatedAt: NOW, recent: [], parked: false, group: '', cards: [], tools: 0, tokOut: 0,
  ctxUsed: 0, window: 0, freshAt: null, autoFixes: 0, autoFixMax: 0, roundFor: '', reviews: 0, steps: 0,
  activeS: 0, roundStartedAt: 0, typicalMin: 0, typicalN: 0, spend: null, ...o,
})
// a round under way since `mins` ago, on a model whose typical step is 20 minutes (3 timed tasks)
const timed = (mins: number) => ({ createdAt: NOW - mins * MIN, roundStartedAt: NOW - mins * MIN, typicalMin: 20, typicalN: 3 })
const ctx = (frac: number) => ({ ctxUsed: Math.round(frac * 1000), window: 1000, freshAt: 0.6 })
const QWEN = { label: 'qwen3-coder 30B', model: 'ollama/qwen3-coder:30b' }
const DEVSTRAL = { label: 'devstral 24B', model: 'ollama/devstral:24b' }
const DEEPSEEK = { via: 'api' as const, provider: 'DeepSeek', model: 'deepseek/deepseek-v4.1-flash' }

const one = fact({ name: 'fix-login', ...QWEN, role: 'significant', ...timed(4), last: 'read src/auth/session.py',
  ...ctx(0.18), tools: 5, tokOut: 600 })
const store = fact({ name: 'hiscore-store', group: 'hiscore', round: 2, roundFor: 'fix', autoFixes: 1, autoFixMax: 2,
  cards: [0], ...QWEN, role: 'significant', ...timed(14), last: 'edit src/score_store.py', ...ctx(0.56), tools: 17, tokOut: 3900 })
const screen = fact({ name: 'hiscore-screen', group: 'hiscore', cards: [1], ...DEVSTRAL, role: 'significant', ...timed(9),
  last: 'write src/ui/hiscore.py', ...ctx(0.71), tools: 9, tokOut: 2100 })
const docs = fact({ name: 'hiscore-docs', group: 'hiscore', phase: 'testing', ...DEEPSEEK, role: 'writing', spend: 0.04,
  createdAt: NOW - 6 * MIN, last: 'tests: python3 -m unittest tests.test_docs', ...ctx(0.22), tools: 6, tokOut: 1200 })
const G = [store, screen, docs]
const slots = fact({ name: 'save-slots', phase: 'queued', ...QWEN, role: 'significant', createdAt: NOW - 2 * MIN,
  last: 'waiting for the GPU' })
// the stuck scene; scenes.py has hiscore-screen at 31m, older than hiscore-store at 23m yet listed
// after it, which no start order gives: here it started 3 minutes after hiscore-store (20m)
const G2 = [
  fact({ name: 'hiscore-store', group: 'hiscore', round: 3, roundFor: 'self_review', cards: [0], ...QWEN, role: 'significant',
    ...timed(23), last: 'read tests/test_store.py', ...ctx(0.31), tools: 24, tokOut: 5000 }),
  fact({ name: 'hiscore-screen', group: 'hiscore', phase: 'stuck', round: 2, cards: [1], ...DEVSTRAL, role: 'significant',
    createdAt: NOW - 20 * MIN, updatedAt: NOW - 10 * MIN, last: 'no output for 10 minutes' }),
  fact({ name: 'hiscore-docs', group: 'hiscore', phase: 'review', tests: 'pass', ...DEEPSEEK, role: 'writing', spend: 0.07,
    createdAt: NOW - 15 * MIN, updatedAt: NOW - 7 * MIN, last: 'tests passed: Claude reviews next' }),
]
const QUEUED = [
  fact({ name: 'save-slots', phase: 'queued', ...QWEN, role: 'significant', createdAt: NOW - 2 * MIN,
    last: 'waiting for the GPU (held by hiscore-store, hiscore-screen)' }),
  fact({ name: 'save-export', phase: 'queued', ...DEVSTRAL, role: 'trivial', createdAt: NOW - 40_000,
    last: 'setting up the worktree: npm ci' }),
  fact({ name: 'tidy-api', phase: 'queued', ...DEEPSEEK, role: 'trivial', createdAt: NOW - 15_000,
    last: 'waiting for an API slot: 2 tasks run on deepseek' }),
]
const DONE = [
  fact({ name: 'save-slots', phase: 'accepted', createdAt: NOW - 50 * MIN, updatedAt: NOW - 12 * MIN }),
  fact({ name: 'old-try', phase: 'discarded', createdAt: NOW - 45 * MIN, updatedAt: NOW - 20 * MIN }),
  fact({ name: 'menu-tweak', phase: 'review', round: 2, tests: 'pass', cards: [0], ...QWEN, role: 'trivial',
    createdAt: NOW - 30 * MIN, updatedAt: NOW - 3 * MIN, last: 'tests passed: Claude reviews next' }),
  fact({ name: 'fix-login', phase: 'review', tests: 'fail', ...QWEN, role: 'significant', createdAt: NOW - 25 * MIN,
    updatedAt: NOW - 10_000, last: 'auto-fix skipped: the base fails the same test' }),
]
const API = [fact({ name: 'port-parser', via: 'api', provider: 'Anthropic', model: 'anthropic/haiku-5.5', role: 'significant',
  spend: 0.31, createdAt: NOW - 12 * MIN, last: 'edit src/parser/lexer.ts', ...ctx(0.44), tools: 31, tokOut: 8800 })]

const text = (row: Part[]) => row.map(p => p.text).join('')
const draw = (list: TaskFacts[], cols: number, compact = false, followed = '') =>
  taskRows(list, { cols, now: NOW, spin: '✻', followed, compact }).map(text)

// The study's §2, as it printed them (the pane-edge row left off).
const MOCKUPS: [string, TaskFacts[], number, boolean, string[]][] = [
  ['one task', [one], 71, false, [
    '✻ fix-login round 1                   GPU  qwen3-coder 30B  complex  4m',
    '  ⎿ read src/auth/session.py     ctx █▌░░░┊░░ 18% · ~16m left · 5 tools']],
  ['three at once', G, 71, false, [
    '▾ hiscore · 3 tasks · 2 running · 1 testing    $0.04  ░░░░░░░░ 0/3 done',
    '├ ✻ hiscore-store  round 2 · fix 1/2           GPU 0                14m',
    '│   ⎿ edit src/score_store.py    ctx ████▌┊░░ 56% · ~6m left · 17 tools',
    '├ ✻ hiscore-screen round 1                     GPU 1                 9m',
    '│   ⎿ write src/ui/hiscore.py    ctx █████▋░░ 71% · ~11m left · 9 tools',
    '└ ◐ hiscore-docs   testing                     API DeepSeek  $0.04   6m',
    '    ⎿ tests: python3 -m unitte…              ctx █▊░░░┊░░ 22% · 6 tools']],
  ['three at once, wide', G, 89, false, [
    '▾ hiscore · 3 tasks · 2 running · 1 testing                      $0.04  ░░░░░░░░ 0/3 done',
    '├ ✻ hiscore-store  round 2 · fix 1/2        GPU 0         qwen3-coder 30B             14m',
    '│   ⎿ edit src/score_store.py           ctx ████▌┊░░ 56% · ~6m left · 17 tools · 3.9K out',
    '├ ✻ hiscore-screen round 1                  GPU 1         devstral 24B                 9m',
    '│   ⎿ write src/ui/hiscore.py           ctx █████▋░░ 71% · ~11m left · 9 tools · 2.1K out',
    '└ ◐ hiscore-docs   testing                  API DeepSeek  deepseek-v4.1-flash  $0.04   6m',
    '    ⎿ tests: python3 -m unittest test…              ctx █▊░░░┊░░ 22% · 6 tools · 1.2K out']],
  ['narrow', G, 53, false, [
    '▾ hiscore · 3 tasks · 2 running · 1 testing  0/3 done',
    '├ ✻ hiscore-store  r2 · fix 1/2     GPU 0         14m',
    '│   ⎿ edit src/score_store.py      ctx 56% · ~6m left',
    '├ ✻ hiscore-screen r1               GPU 1          9m',
    '│   ⎿ write src/ui/hiscore.py     ctx 71% · ~11m left',
    '└ ◐ hiscore-docs   testing          API    $0.04   6m',
    '    ⎿ tests: python3 -m unittes…              ctx 22%']],
  ['compact, narrow', [...G, slots], 53, true, [
    '▾ hiscore · 2 running · 1 testing            0/3 done',
    '├ ✻ hiscore-store  r2 · fix 1/2     GPU 0         14m',
    '├ ✻ hiscore-screen r1               GPU 1          9m',
    '└ ◐ hiscore-docs   testing          API    $0.04   6m',
    '◌ save-slots waits for the GPU                GPU  2m']],
  ['compact, the box', [...G, slots], 76, true, [
    '▾ hiscore · 2 running · 1 testing                                   0/3 done',
    '├ ✻ hiscore-store  round 2 · fix 1/2                GPU 0                14m',
    '├ ✻ hiscore-screen round 1                          GPU 1                 9m',
    '└ ◐ hiscore-docs   testing                          API DeepSeek  $0.04   6m',
    '◌ save-slots waits for the GPU             GPU  qwen3-coder 30B  complex  2m']],
  ['a stuck one', G2, 71, false, [
    '▾ hiscore · 3 tasks · 1 running · 1 review · 1 stuck    $0.07  1/3 done',
    '├ ✻ hiscore-store  round 3 · self-review       GPU 0                23m',
    '│   ⎿ read tests/test_store.py    ctx ██▌░░┊░░ 31% · 3m over · 24 tools',
    // the study's 31m: see G2
    '├ ▲ hiscore-screen stuck                       GPU 1                20m',
    '│   ⎿ no output for 10 minutes · Claude steers it next',
    '└ ◆ hiscore-docs   review · tests ✓            API DeepSeek  $0.07   8m']],
  ['queued', QUEUED, 71, false, [
    '◌ save-slots  waits for the GPU  GPU           qwen3-coder 30B       2m',
    '  ⎿ waiting for the GPU (held by hiscore-store, hiscore-screen)',
    '◌ save-export setting up         GPU           devstral 24B         40s',
    '  ⎿ setting up the worktree: npm ci',
    '◌ tidy-api    waits for a slot   API DeepSeek  deepseek-v4.1-flash  15s',
    '  ⎿ waiting for an API slot: 2 tasks run on deepseek']],
  ['done', DONE, 71, false, [
    '◆ menu-tweak review · tests ✓   GPU 0  qwen3-coder 30B  trivial  3m ago',
    '◆ fix-login  review · tests ✗   GPU    qwen3-coder 30B  complex     now',
    '  ⎿ auto-fix skipped: the base fails the same test',
    '✓ save-slots accepted 12m ago · – old-try discarded 20m ago']],
  ['an API task', API, 71, false, [
    '✻ port-parser round 1     API Anthropic  haiku-5.5  complex  $0.31  12m',
    '  ⎿ edit src/parser/lexer.ts     ctx ███▌░┊░░ 44% · 31 tools · 8.8K out']],
]

test('every mockup of the design study is drawn character for character', () => {
  for (const [name, list, cols, compact, want] of MOCKUPS) expect([name, ...draw(list, cols, compact)]).toEqual([name, ...want])
})

// Every column at its widest (the #1048 review: the mockups alone never reach the name's narrowing,
// so dropping it passed): names past 22, a long provider and model, no price, a day and more of
// elapsed time, two cards, every meter, a question on two lines.
const HARD = [
  fact({ name: 'a-task-name-well-past-twenty-two', group: 'a-fan-out', round: 12, roundFor: 'feedback', reviews: 3,
    cards: [0, 1], label: 'L'.repeat(30), role: 'significant', ...timed(1700), last: 'x'.repeat(90), ...ctx(0.95),
    tools: 1234, tokOut: 9_000_000 }),
  fact({ name: 'another-long-task-name-here', group: 'a-fan-out', phase: 'asking', via: 'api',
    provider: 'A Provider With A Long Name', model: 'p/' + 'm'.repeat(30), role: 'writing', spend: 'none',
    createdAt: NOW - 30 * 3600_000, recent: ['asks: which\nway?'], ...ctx(0.5), tools: 3 }),
  fact({ name: 'priced', group: 'a-fan-out', phase: 'testing', via: 'api', provider: 'DeepSeek', spend: 1234.5678,
    roundFor: 'fix', autoFixes: 9, autoFixMax: 10, createdAt: NOW - 5 * MIN, last: 'tests: make check', ...ctx(0.3) }),
]

test('no row is wider than the pane, at every width from 30 to 120, for every mockup\'s tasks', () => {
  const lists: [string, TaskFacts[]][] = [...MOCKUPS.map(([n, l]): [string, TaskFacts[]] => [n, l]), ['hard', HARD],
    ['hard, ungrouped', HARD.map(t => ({ ...t, group: '' }))]]
  for (const [name, list] of lists) for (let cols = 30; cols <= 120; cols++) for (const compact of [false, true])
    for (const usd of [undefined, (x: number) => `$${x.toFixed(4)}`]) {
      const rows = taskRows(list, { cols, now: NOW, spin: '✻', followed: '', compact, usd }).map(text)
      const wide = rows.filter(r => r.length > cols || r.includes('\n'))
      expect([name, cols, compact, wide]).toEqual([name, cols, compact, []])
    }
  // and the spend and the elapsed time stay on each task's line, down to the narrowest pane
  for (let cols = 30; cols <= 120; cols++) {
    const rows = draw(HARD, cols)
    expect([cols, / no price +30h$/.test(rows.find(r => r.includes('? anoth'))!)]).toEqual([cols, true])
    expect([cols, / \$1234\.57 +5m$/.test(rows.find(r => r.includes('◐ pric'))!)]).toEqual([cols, true])
    expect([cols, / 28h$/.test(rows.find(r => r.includes('✻ a-ta'))!)]).toEqual([cols, true])
  }
})

test('an asking task\'s second line is its newest question, on one line', () => {
  const t = fact({ name: 'q', phase: 'asking', last: 'ask_reviewer', ...ctx(0.3),
    recent: ['asks: an older one?', 'answered: yes', 'read a.py', 'asks: keep the old API,\nor drop it?'] })
  const rows = taskRows([t], { cols: 71, now: NOW, spin: '✻', followed: '', compact: false })
  expect(rows.map(text)[1]).toMatch(/^ {2}⎿ keep the old API, or drop it\? +ctx /)
  expect(partWith(rows[1]!, /keep/)?.color).toBe('warning')
  // a failure's error can span lines too
  expect(draw([fact({ name: 'f', phase: 'failed', last: 'model: out of\nmemory' })], 71))
    .toEqual([expect.stringMatching(/^✗ f failed +GPU  0s$/), '  ⎿ model: out of memory · Claude steers it next'])
})

test('three tasks of one request on GPU 0, GPU 1 and DeepSeek draw one header and six aligned rows', () => {
  for (const cols of [53, 71, 89]) {
    const rows = draw(G, cols)
    expect(rows).toHaveLength(7)
    expect(rows.filter(r => r.startsWith('▾ '))).toHaveLength(1)
    const ones = [rows[1]!, rows[3]!, rows[5]!]
    // the place column starts at one column, and the elapsed time ends at the pane's edge
    expect(new Set(ones.map(r => r.search(/GPU|API/))).size).toBe(1)
    expect(ones.map(r => r.length)).toEqual([cols, cols, cols])
    expect(ones.map(r => r.slice(0, 2))).toEqual(['├ ', '├ ', '└ '])
    expect([rows[2]!, rows[4]!, rows[6]!].map(r => r.slice(0, 6))).toEqual(['│   ⎿ ', '│   ⎿ ', '    ⎿ '])
  }
})

test('a narrow pane drops the role, then the model, then the long forms, and never the spend or the elapsed time', () => {
  let had = { role: true, model: true, stage: true, place: true }
  for (let cols = 120; cols >= 40; cols--) {
    const rows = draw(G, cols), all = rows.join('\n')
    const now = { role: all.includes('complex'), model: all.includes('qwen3-coder'), stage: all.includes('round 2'),
      place: all.includes('API DeepSeek') }
    // each goes only once the one before it has gone, and none comes back
    if (!now.model) expect([cols, now.role]).toEqual([cols, false])
    if (!now.stage) expect([cols, now.model]).toEqual([cols, false])
    if (!now.place) expect([cols, now.stage]).toEqual([cols, false])
    for (const k of ['role', 'model', 'stage', 'place'] as const) if (!had[k]) expect([cols, k, now[k]]).toEqual([cols, k, false])
    had = now
    const docsRow = rows.find(r => r.includes('hiscore-docs'))!
    expect([cols, /\$0\.04 +6m$/.test(docsRow)]).toEqual([cols, true])
    expect([cols, / 14m$/.test(rows.find(r => r.includes('hiscore-store'))!)]).toEqual([cols, true])
  }
  expect(had).toEqual({ role: false, model: false, stage: false, place: false })
})

const partWith = (row: Part[], re: RegExp) => row.find(p => re.test(p.text))
test('the context bar marks the handover point and turns warning past it', () => {
  const at = (frac: number) => taskRows([fact({ name: 't', last: 'edit a.py', ...ctx(frac) })],
    { cols: 89, now: NOW, spin: '✻', followed: '', compact: false })[1]!
  expect(ctxOf(fact({ name: 't', ...ctx(0.3) }))).toEqual({ frac: 0.3, freshAt: 0.6 })
  expect(partWith(at(0.3), /%/)).toEqual({ text: '██▍░░┊░░ 30%', color: 'success' })
  expect(partWith(at(0.7), /%/)).toEqual({ text: '█████▋░░ 70%', color: 'warning' })
  expect(partWith(at(0.95), /%/)?.color).toBe('error')
  // unknown until the agent's session reports it, and only while it works
  expect(ctxOf(fact({ name: 't' }))).toBe(null)
  expect(ctxOf(fact({ name: 't', phase: 'review', ...ctx(0.3) }))).toBe(null)
})

test('the time estimate shows only with three timed tasks behind it, and says over past it', () => {
  expect(etaOf(fact({ name: 't', ...timed(5), typicalN: 2 }), NOW)).toBe(null)
  expect(etaOf(fact({ name: 't', ...timed(5) }), NOW)).toEqual({ text: '~15m left' })
  // the rounds before this one count too (status active_s), and the wall clock outside rounds doesn't
  expect(etaOf(fact({ name: 't', ...timed(5), createdAt: NOW - 90 * MIN, activeS: 600 }), NOW)).toEqual({ text: '~5m left' })
  expect(etaOf(fact({ name: 't', ...timed(23) }), NOW)).toEqual({ text: '3m over', color: 'warning' })
  expect(etaOf(fact({ name: 't', ...timed(45) }), NOW)).toEqual({ text: '25m over', color: 'error' })
  expect(etaOf(fact({ name: 't', phase: 'queued', ...timed(5) }), NOW)).toBe(null)
  // the elapsed time says the same in colour
  const elapsed = (mins: number) => taskRows([fact({ name: 't', ...timed(mins) })],
    { cols: 71, now: NOW, spin: '✻', followed: '', compact: false })[0]!.at(-1)
  expect(elapsed(5)).toEqual({ text: '5m', dim: true })
  expect(elapsed(23)).toEqual({ text: '23m', color: 'warning' })
  expect(elapsed(45)).toEqual({ text: '45m', color: 'error' })
})

test('a stuck task\'s second line says why and who acts next', () => {
  const rows = taskRows([G2[1]!], { cols: 71, now: NOW, spin: '✻', followed: '', compact: false })
  expect(rows.map(text)[1]).toBe('  ⎿ no output for 10 minutes · Claude steers it next')
  expect(partWith(rows[1]!, /Claude steers/)?.color).toBe('error')
  expect(draw([fact({ name: 'f', phase: 'failed', last: 'model: out of memory' })], 71)[1])
    .toBe('  ⎿ model: out of memory · Claude steers it next')
  // parked by /workbench-quit: waiting on no one, so nothing to say
  expect(draw([fact({ name: 's', phase: 'stuck', parked: true, last: 'x' })], 71)).toHaveLength(1)
})

test('a queued task\'s stage follows its runner\'s words (GPU, slot, setting up, base check)', () => {
  const said: [string, string, string][] = [
    ['waiting for the GPU', 'waits for the GPU', 'waits GPU'],
    ['waiting for the GPU: the tests use it alone', 'waits for the GPU', 'waits GPU'],
    ['waiting for an API slot: 3 tasks run on x', 'waits for a slot', 'waits slot'],
    ["waiting for other's setup (1 ahead)", 'waiting', 'waiting'],
    ['checking out the base', 'checking out', 'checkout'],
    ['setting up the worktree', 'setting up', 'setup'],
    ['checking the acceptance command on the base', 'base check', 'base'],
    ['loading m into ollama', 'loading model', 'loading'],
    ['reloading m: it was partly on the CPU', 'loading model', 'loading'],
    ['starting', 'loading model', 'loading'],
    ['', 'queued', 'queued'],
  ]
  for (const [last, long, short] of said) {
    const t = fact({ name: 'q', phase: 'queued', last })
    expect([last, stageOf(t, false).text, stageOf(t, true).text]).toEqual([last, long, short])
  }
  expect(stageOf(fact({ name: 'q', phase: 'queued', round: 3 }), false).text).toBe('queued · round 3')
})

test('a running round says why it runs, long and short', () => {
  const said: [Partial<TaskFacts>, string, string][] = [
    [{ roundFor: 'start' }, 'round 2', 'r2'],
    [{ roundFor: 'fix', autoFixes: 1, autoFixMax: 2 }, 'round 2 · fix 1/2', 'r2 · fix 1/2'],
    [{ roundFor: 'self_review' }, 'round 2 · self-review', 'r2 · self-rev'],
    [{ roundFor: 'feedback', reviews: 1 }, 'round 2 · feedback 1', 'r2 · fb 1'],
    [{ roundFor: 'steer' }, 'round 2 · steered', 'r2 · steered'],
    [{ roundFor: 'fresh' }, 'round 2 · fresh session', 'r2 · fresh'],
    [{ roundFor: 'next', steps: 3 }, 'step 3 · round 2', 's3 · r2'],
    [{ roundFor: 'resume' }, 'round 2 · resumed', 'r2 · resumed'],
  ]
  for (const [o, long, short] of said) {
    const t = fact({ name: 'r', round: 2, ...o })
    expect([stageOf(t, false).text, stageOf(t, true).text]).toEqual([long, short])
  }
  // the fix count in the warning colour, the rest in the phase's
  const row = taskRows([G[0]!], { cols: 71, now: NOW, spin: '✻', followed: '', compact: false })[0]!
  expect(row.filter(p => /round|fix/.test(p.text))).toEqual([{ text: 'round 2 · ', color: 'claude' }, { text: 'fix 1/2', color: 'warning' }])
})

test('a passing review folds to one line, and closed tasks fold to one dim line', () => {
  const rows = taskRows(DONE, { cols: 71, now: NOW, spin: '✻', followed: '', compact: false })
  expect(rows.map(text).filter(r => r.includes('menu-tweak'))).toHaveLength(1)
  expect(rows.at(-1)).toEqual([{ text: '✓ save-slots accepted 12m ago · – old-try discarded 20m ago', color: 'inactive' }])
  // passing tests say it all, whatever its last word; one whose last word is the standard handoff
  // has nothing more to say either
  expect(draw([fact({ name: 'r', phase: 'review', tests: 'pass', last: 'agent finished' })], 71)).toHaveLength(1)
  expect(draw([fact({ name: 'r', phase: 'review', tests: null, last: 'done: Claude reviews next' })], 71)).toHaveLength(1)
  // compact: the closed line goes too
  expect(draw(DONE, 71, true)).toHaveLength(2)
  // in a request's group, closed tasks count as done
  const g = [fact({ name: 'a', group: 'g', phase: 'accepted', createdAt: NOW - 9 * MIN, updatedAt: NOW - MIN }),
    fact({ name: 'b', group: 'g', createdAt: NOW - 8 * MIN, last: 'edit b' })]
  expect(draw(g, 71)[0]).toMatch(/· 2 tasks · 1 running .* 1\/2 done$/)
})

test('an API task shows its spend, and one with no price says no price, never $0', () => {
  const spendOf = (spend: TaskFacts['spend']) => draw([fact({ name: 'p', ...DEEPSEEK, spend })], 71)[0]!
  expect(spendOf(0.31)).toMatch(/ \$0\.31  0s$/)
  expect(spendOf('none')).toMatch(/ no price  0s$/)
  expect(spendOf('none')).not.toContain('$0')
  expect(spendOf('plan')).toMatch(/ plan  0s$/)
  expect(spendOf(0)).toMatch(/ \$0  0s$/) // a free model
  expect(spendOf(null)).not.toContain('$')
  // wave 2's usd(), once step E passes it
  expect(taskRows([fact({ name: 'p', ...DEEPSEEK, spend: 0.0041 })], { cols: 71, now: NOW, spin: '✻', followed: '',
    compact: false, usd: x => `$${x}` }).map(text)[0]).toContain('$0.0041')
  // tokens: only those written (the owner, 2026-11-23: "3.9K out")
  expect(draw(G, 89)[2]).toMatch(/· 3\.9K out$/)
})

test('each phase has its own glyph and word, so no state is told by colour alone', () => {
  const phases = ['queued', 'running', 'testing', 'asking', 'review', 'stuck', 'failed', 'accepted', 'discarded']
  const glyph = (p: string) => (p === 'running' ? '✻' : MARK[p])
  expect(new Set(phases.map(glyph)).size).toBe(phases.length)
  const words = phases.map(p => stageOf(fact({ name: 't', phase: p }), false).text)
  expect(new Set(words).size).toBe(phases.length)
  for (const p of phases.filter(p => p !== 'accepted' && p !== 'discarded'))
    expect(draw([fact({ name: 't', phase: p })], 71)[0]!.startsWith(`${glyph(p)} t `)).toBe(true)
})

test('an older wb\'s task (no round_for, typical, cards, group or api) draws as round N on GPU, ungrouped', () => {
  const old = fact({ name: 'old', round: 3, ...QWEN, createdAt: NOW - 7 * MIN, last: 'edit x.py' })
  expect(stageOf(old, false).text).toBe('round 3')
  expect(placeOf(old, false)).toBe('GPU')
  expect(etaOf(old, NOW)).toBe(null)
  expect(blocksOf([old])).toEqual([{ group: '', tasks: [old] }])
  expect(draw([old], 71)).toEqual([
    '✻ old round 3                                  GPU  qwen3-coder 30B  7m',
    '  ⎿ edit x.py'])
})

test('a group of one is drawn ungrouped, and blocks keep their tasks\' start order', () => {
  const a = fact({ name: 'a', group: 'solo', createdAt: 1 }), b = fact({ name: 'b', group: 'pair', createdAt: 2 })
  const c = fact({ name: 'c', createdAt: 3 }), d = fact({ name: 'd', group: 'pair', createdAt: 4 })
  expect(blocksOf([d, c, b, a]).map(x => [x.group, x.tasks.map(t => t.name)])).toEqual([['', ['a', 'c']], ['pair', ['b', 'd']]])
  // an empty row between blocks, none after the last
  const rows = draw([d, c, b, a], 71)
  expect(rows.filter(r => r === '')).toHaveLength(1)
  expect(rows.at(-1)).not.toBe('')
})

test('the followed task\'s name is underlined, so the transcript below is told without colour', () => {
  const row = taskRows(G, { cols: 71, now: NOW, spin: '✻', followed: 'hiscore-screen', compact: false })
  expect(row.flat().filter(p => p.underline)).toEqual([{ text: 'hiscore-screen', bold: true, underline: true }])
})

test('the pane goes compact in the box, or when two lines a task would push the transcript out', () => {
  expect(compactWanted(1, 0, 14)).toBe(true)
  expect(compactWanted(3, 1, 40)).toBe(false)
  expect(compactWanted(12, 1, 30)).toBe(true)
})

test('the API day cap shows on Tasks only while an API task works, in its colour', () => {
  // the study drew this bar by hand (█▍ for $0.42 of $5 is 0.14 of it): the true share is 0.084
  expect(text(capRow(API, 0.42, 5, 71)!)).toBe('API today $0.42 of the $5 day cap  ▉░░░░░░░░░')
  expect(capRow(API, 0.42, 5, 71)!.at(-1)?.color).toBe('success')
  expect(capRow(API, 3, 5, 71)!.at(-1)?.color).toBe('warning')
  expect(capRow(API, 5, 5, 71)!.at(-1)?.color).toBe('error')
  expect(capRow([one], 0.42, 5, 71)).toBe(null)
  expect(capRow(G2, 0.42, 5, 71)).toBe(null) // its API task is in review
  expect(text(capRow(API, 0.42, null, 71)!)).toBe('API today $0.42')
})

test('the small helpers the pane shares', () => {
  expect(bar(0.5, 4)).toBe('██░░')
  expect(clip('abcdef', 4)).toBe('abc…')
  expect(clip('abc', 0)).toBe('…')
  expect([ago(40_000), ago(14 * MIN), ago(3 * 3600_000)]).toEqual(['40s', '14m', '3h'])
})
