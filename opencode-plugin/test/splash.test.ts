import { expect, test } from 'bun:test'
import { BAR, frameCells, frameCount, LINES, plan as forge } from '../../mod/hooks/splash.ts'
import type { Target } from '../../mod/hooks/splash.ts'
import { cells, COLUMNS, ROWS } from '../../mod/hooks/apprentice.ts'
import type { Pose } from '../../mod/hooks/apprentice.ts'
import { apprentice, cellsOf, Play, restTarget, spans, splashOn, TITLE, words } from '../spans.ts'
import type { Span } from '../spans.ts'
import tui from '../tui.ts'

// The apprentice and the opening animation as OpenTUI spans (#1333), under OpenCode's own Bun:
// `BUN_BE_BUN=1 opencode test opencode-plugin/test`. The spans must be splash.ts's frames and
// apprentice.ts's cells exactly, so Claude Code and OpenCode play one animation.

// OpenCode's plan: splash.ts's, forging Workbench's name alone
const plan = (cols: number, rows: number, target: Target) => forge(cols, rows, target, TITLE)

const mismatches = (a: Uint32Array, b: Uint32Array) => {
  if (a.length !== b.length) return Infinity
  let n = 0
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) n++
  return n
}
const themed = (rows: Span[][]) => rows.flatMap((row, r) => {
  let c = 0
  return row.flatMap(s => {
    const at = c
    c += [...s.text].length
    return [s.fg, s.bg].flatMap(p => (p !== null && typeof p === 'object' ? [{ r, c: at, n: [...s.text].length, ...p }] : []))
  })
})

// forge's design (60x26, the words falling into a modeline row) and the small font (40x22,
// rising away), as the mod's tests hold them, and a big wide pane
const SIZES: [number, number, Target][] = [
  [60, 26, { stage: 13, bar: 24 }], [40, 22, { stage: 12, bar: null }], [120, 40, { stage: 20, bar: 37 }],
]

test('every frame the panel plays decodes to splash.ts\'s cells for it', () => {
  for (const [cols, rows, target] of SIZES) {
    const play = new Play()
    expect(play.fit(cols, rows, target)).toBe(true)
    const ref = plan(cols, rows, target)!
    let frames = 0
    do {
      const got = cellsOf(play.frame()!, cols)
      expect([cols, rows, play.f, mismatches(got, frameCells(ref, play.f))]).toEqual([cols, rows, play.f, 0])
      frames++
    } while (play.step())
    expect(frames).toBe(frameCount(ref))                 // all of them, then done
    expect([play.done, play.frame()]).toEqual([true, null])
  }
})

test('the bar the words fall into is the theme\'s accent, its whole row, and nothing else is', () => {
  const [cols, rows, target] = SIZES[0]!
  const p = plan(cols, rows, target)!
  let last: Uint32Array = new Uint32Array()
  for (let f = 0; f < frameCount(p); f++) last = frameCells(p, f) // its ending, and so its length, decided as it plays
  expect(frameCount(p)).toBe(62)
  const bar = themed(spans(last, cols)).filter(t => t.rgb === BAR)
  expect(bar.map(t => [t.r, t.token])).toEqual([[target.bar, 'accent']])
  expect([bar[0]!.c, bar[0]!.n]).toEqual([0, cols])
})

test('in every frame only the apprentice\'s lamp takes a theme colour beside the bar', () => {
  // the lamp is themed by its colour (apprentice.ts LAMP), so no colour of the art may be one
  for (const [cols, rows, target] of SIZES) {
    const p = plan(cols, rows, target)!
    for (let f = 0; f < frameCount(p); f++) {
      const lamp = themed(spans(frameCells(p, f), cols)).filter(t => !(t.rgb === BAR && t.token === 'accent'))
      expect([cols, f, lamp.reduce((n, t) => n + t.n, 0) <= 2]).toEqual([cols, f, true])
    }
  }
})

test('the apprentice in each pose is apprentice.ts\'s cells, its lamp the phase\'s theme colour', () => {
  const lamp: Record<Pose, string> = {
    rest: 'accent', blink: 'accent', think: 'accent', work: 'accent', swing: 'accent',
    tada: 'success', ask: 'warning', slump: 'error', sleep: 'faint',
  }
  for (const pose of Object.keys(lamp) as Pose[]) {
    const rows = apprentice(pose)
    expect(rows.length).toBe(ROWS)
    expect([pose, mismatches(cellsOf(rows, COLUMNS), words(cells(pose)))]).toEqual([pose, 0])
    // the lamp's two pixels, side by side in one cell row: one span, themed for the pose
    expect(themed(rows).map(t => [pose, t.token, t.n])).toEqual([[pose, lamp[pose], 2]])
  }
})

test('a resize plans the splash again from the same frame and ending, and a small panel ends it', () => {
  const play = new Play()
  expect(play.fit(60, 26, { stage: 13, bar: 24 })).toBe(true)
  while (play.f < 50) { play.step(); play.frame() }    // past RISE: the words are falling (decided as drawn)
  const ending = play.plan!.ending
  expect(ending).toBe('fall')
  expect(play.fit(64, 30, { stage: 15, bar: 28 })).toBe(true)
  expect([play.plan!.cols, play.plan!.rows, play.f, play.plan!.ending]).toEqual([64, 30, 50, ending])
  const ref = plan(64, 30, { stage: 15, bar: 28 })!
  ref.ending = ending
  expect(mismatches(cellsOf(play.frame()!, 64), frameCells(ref, 50))).toBe(0)
  expect(play.fit(39, 30)).toBe(false)                  // too narrow for either font: the page
  expect([play.done, play.fit(60, 26)]).toEqual([true, false]) // and it never comes back
})

test('without a page to say where, the apprentice ends at the bottom left and the words rise away', () => {
  expect(restTarget(26)).toEqual({ stage: 26 - ROWS, bar: null })
  expect(restTarget(3)).toEqual({ stage: 0, bar: null })
  const play = new Play()
  play.fit(60, 26)
  while (play.step()) { /* to the end */ }
  expect(play.done).toBe(true)
  const p = plan(60, 26, restTarget(26))!
  frameCells(p, 50)
  expect(p.ending).toBe('rise')
})

test('OpenCode\'s splash forges WORKBENCH alone, Claude Code\'s CLAUDE\'S WORKBENCH as before', () => {
  const word = (p: ReturnType<typeof forge>) => p!.glyphs.length
  expect(TITLE).toEqual(['WORKBENCH'])
  expect(LINES).toEqual(["CLAUDE'S", 'WORKBENCH'])
  for (const [cols, rows, target] of SIZES) {
    expect([word(plan(cols, rows, target)), word(forge(cols, rows, target))]).toEqual([9, 17])
    expect(new Play().fit(cols, rows, target)).toBe(true)
  }
  // the one line keeps WORKBENCH's own place in the colours: cool, as under CLAUDE'S
  const alone = plan(60, 26, SIZES[0]![2])!
  const both = forge(60, 26, SIZES[0]![2])!
  const fin = (p: NonNullable<typeof alone>, i: number) => p.glyphs[i]!.fin[0]
  expect(alone.glyphs.map((_, i) => fin(alone, i))).toEqual(both.glyphs.slice(8).map((_, i) => fin(both, 8 + i)))
})

test('splash.json turns it off only when it says on: false', () => {
  expect(splashOn(() => '{"on": false, "ts": 1}')).toBe(false)
  expect(splashOn(() => '{"on": true}')).toBe(true)
  expect(splashOn(() => '{}')).toBe(true)
  expect(splashOn(() => { throw new Error('ENOENT') })).toBe(true)
  expect(splashOn(() => 'not json')).toBe(true)
})

test('a TUI that can\'t load the OpenTUI views still sets the plugin up, with nothing said', async () => {
  // Bun alone has no solid-js or @opentui/solid, as an OpenCode TUI without OpenTUI's plugin
  // runtime wouldn't: tui.ts imports splash.tsx in setup, and its failure stays there
  const was = process.env.WB_TASK
  delete process.env.WB_TASK
  const said: unknown[] = []
  try {
    const ctx = {
      options: {},
      client: { rpc: () => ({ status: async () => ({ wb: '/w/bin/wb', found: true, from: 'options', home: '/state' }) }) },
      ui: { toast: { show: (o: unknown) => said.push(o) } },
    }
    const stop = await tui.setup(ctx as never)  // the notices' cleanup alone (#1313)
    expect(typeof stop).toBe('function')
    ;(stop as () => void)()
  } finally {
    if (was !== undefined) process.env.WB_TASK = was
  }
  expect(said).toEqual([])
})
