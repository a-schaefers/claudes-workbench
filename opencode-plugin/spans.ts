// The apprentice and the opening animation as OpenTUI spans (#1333, slice 4c of #1296): the
// cells mod/hooks/splash.ts and apprentice.ts compute for Claude Code's Raster, turned into rows
// of runs that OpenCode's TUI draws as <span>s (splash.tsx). The host-shape decision
// (https://github.com/a-schaefers/workbench/issues/1296#issuecomment-7158435888) has them in
// `ctx.theme`'s colours: the terminal's own colour is the panel's surface (no colour: the span is
// see-through), and the colours that mean something take the theme's (the bar the words fall
// into, and the chest lamp: the phase's colour), while the art keeps its own, all in a luminance
// band that reads on a dark theme and a light one (splash.ts `LO`, `HI`). Everything else is
// splash.ts's: the same frames, so Claude Code and OpenCode play one animation, but for its words:
// here Workbench's own name alone (`TITLE`).
//
// Pure: no OpenTUI or Solid here, so OpenCode's Bun tests it with nothing installed, and nothing
// runs at import (the plugin's rule: OpenCode imports a removed plugin to learn its id).
import { BAR, frameCells, frameCount, plan, splashFont } from '../mod/hooks/splash.ts'
import type { Plan, Target } from '../mod/hooks/splash.ts'
import { cells, COLUMNS, LAMP, ROWS } from '../mod/hooks/apprentice.ts'
import type { Pose } from '../mod/hooks/apprentice.ts'

export { SPLASH_MS } from '../mod/hooks/splash.ts'
export type { Pose, Target }

/** The words OpenCode's splash forges: the pane there "would just only be called workbench" (the
 * owner, 2026-11-24, #1296); Claude Code's keeps CLAUDE'S WORKBENCH (splash.ts `LINES`). */
export const TITLE: readonly string[] = ['WORKBENCH']

const DEFAULT = 0x01000000 // a Raster's "the terminal's own colour"
const SPACE = 0x20

/** The theme colours a span may take, resolved by splash.tsx from `ctx.theme`. */
export type Token = 'accent' | 'success' | 'warning' | 'error' | 'faint'
/** A colour: none (the surface's own), an RGB the art keeps, or a theme colour (`rgb`: Claude
 * Code's, which `cellsOf` gives back). */
export type Paint = null | number | { readonly token: Token; readonly rgb: number }
export interface Span {
  readonly text: string
  readonly fg: Paint
  readonly bg: Paint
}

// The lamp's colours (apprentice.ts `LAMP`) and what each means: Claude's coral is the accent,
// the cheer's green success, the raised hand's amber a question, the slump's red trouble, sleep's
// grey faint. By value: none of them is a colour of the art (the tests check every frame).
const LAMP_TOKEN: Record<Pose, Token> = {
  rest: 'accent', blink: 'accent', think: 'accent', work: 'accent', swing: 'accent',
  tada: 'success', ask: 'warning', slump: 'error', sleep: 'faint',
}
let lamps: Map<number, Token> | null = null
const lampToken = (rgb: number): Token | undefined => {
  lamps ??= new Map(Object.entries(LAMP).map(([pose, rgb]) => [rgb, LAMP_TOKEN[pose as Pose]]))
  return lamps.get(rgb)
}

function paintOf(rgb: number): Paint {
  if (rgb === DEFAULT) return null
  const token = lampToken(rgb)
  return token ? { token, rgb } : rgb
}

const same = (a: Paint, b: Paint) =>
  a === b || (typeof a === 'object' && typeof b === 'object' && a !== null && b !== null && a.token === b.token && a.rgb === b.rgb)

/**
 * A Raster's cells ([codePoint, fg, bg] words, row-major, `cols` a row) as rows of spans, each
 * run of one fg and bg one span. The bar the embers make (a blank cell on splash.ts's `BAR`) is
 * the theme's accent, as the modeline it becomes; the lamp the theme's colour for its phase.
 */
export function spans(cellWords: Uint32Array, cols: number): Span[][] {
  const out: Span[][] = []
  for (let at = 0; at < cellWords.length; at += cols * 3) {
    const row: Span[] = []
    let run: { text: string; fg: Paint; bg: Paint } | null = null
    for (let i = at; i < at + cols * 3; i += 3) {
      const [cp, fg, bg] = [cellWords[i]!, cellWords[i + 1]!, cellWords[i + 2]!]
      // the bar: only a blank cell has its colour as a background (frameCells' last step)
      const paint = { fg: paintOf(fg), bg: cp === SPACE && bg === BAR ? { token: 'accent' as const, rgb: BAR } : paintOf(bg) }
      const text = String.fromCodePoint(cp)
      if (run && same(run.fg, paint.fg) && same(run.bg, paint.bg)) run.text += text
      else {
        if (run) row.push(run)
        run = { text, ...paint }
      }
    }
    if (run) row.push(run)
    out.push(row)
  }
  return out
}

const rgbOf = (p: Paint) => (p === null ? DEFAULT : typeof p === 'number' ? p : p.rgb)

/** The spans back as the cells they came from: what the tests hold against splash.ts. */
export function cellsOf(rows: Span[][], cols: number): Uint32Array {
  const out = new Uint32Array(rows.length * cols * 3)
  rows.forEach((row, r) => {
    let c = 0
    for (const s of row) {
      for (const ch of s.text) {
        out.set([ch.codePointAt(0)!, rgbOf(s.fg), rgbOf(s.bg)], (r * cols + c) * 3)
        c += 1
      }
    }
  })
  return out
}

/** A Raster's `cells` string (base64 of little-endian words) as its words. */
export function words(b64: string): Uint32Array {
  const bin = atob(b64)
  const bytes = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i)
  const view = new DataView(bytes.buffer)
  const out = new Uint32Array(bytes.length / 4)
  for (let i = 0; i < out.length; i++) out[i] = view.getUint32(i * 4, true)
  return out
}

const drawn = new Map<Pose, Span[][]>()
/** The apprentice in a pose, `COLUMNS` x `ROWS` cells, from apprentice.ts's own `cells`. */
export function apprentice(pose: Pose): Span[][] {
  let rows = drawn.get(pose)
  if (!rows) drawn.set(pose, (rows = spans(words(cells(pose)), COLUMNS)))
  return rows
}
export { COLUMNS, ROWS }

/** splash.json as wb writes it (`wb splash on|off`): on unless it says off, as the mod's
 * `splashOn` reads it (unreadable or missing: on). */
export function splashOn(read: () => string): boolean {
  try {
    return JSON.parse(read()).on !== false
  } catch {
    return true
  }
}

/** Where the splash ends when the page under it says nothing: the apprentice at the panel's
 * bottom left, and the words rising away (no modeline row is known, so no bar: splash.ts never
 * lands the words on a guessed row). */
export const restTarget = (rows: number): Target => ({ stage: Math.max(0, rows - ROWS), bar: null })

/**
 * One play of the splash, as the panel drives it: `fit` at each size the panel has (the first
 * plans it; a new one plans again from the same frame and ending, as the mod does, since a
 * resize ending it ended every real opening, 2026-11-21), `step` at each tick. `done` once it
 * has played, was skipped or the panel is too small for it (splash.ts `splashFont`).
 */
export class Play {
  plan: Plan | null = null
  f = 0
  done = false

  /** The panel is cols x rows now, the page under it wants `target`: false when it can't play. */
  fit(cols: number, rows: number, target: Target = restTarget(rows)): boolean {
    if (this.done) return false
    if (!splashFont(cols, rows)) return this.end()
    if (!this.plan || this.plan.cols !== cols || this.plan.rows !== rows) {
      const ending = this.plan?.ending ?? null
      this.plan = plan(cols, rows, target, TITLE)
      if (!this.plan) return this.end()
      this.plan.ending = ending
    } else this.plan.target = target
    return true
  }

  /** The next frame: false (and done) after the last. */
  step(): boolean {
    if (this.done || !this.plan) return false
    if (this.f + 1 >= frameCount(this.plan)) return this.end()
    this.f += 1
    return true
  }

  /** The frame now, as spans; none before the first fit or once done. */
  frame(): Span[][] | null {
    if (this.done || !this.plan) return null
    return spans(frameCells(this.plan, this.f), this.plan.cols)
  }

  end(): false {
    this.done = true
    this.plan = null
    return false
  }
}
