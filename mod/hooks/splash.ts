// The pane's opening animation (#963, 2026-11-21: the owner asked for about five seconds of the
// apprentice and Claude's Workbench in large colour art, ending on the Tasks page "like normal",
// with "no penalty beyond initial startup"). Claude picked the "forge" concept: on a crate at
// its anvil the apprentice strikes six blows, and comets from each forge the letters of CLAUDE'S
// WORKBENCH, which land hot and cool into their colours. A glint, then its cheer, with a rainbow
// running once through both words (grafted from the "stream" concept). Then the words fall as
// embers into the pane's bottom row and become its modeline ([•_•] Claude's Workbench, #962),
// while the apprentice walks to its spot on the Tasks page. Where the pane can't know that row
// exactly, the words rise away as embers instead, as forge drew it.
//
// A port of forge.py, the reference generator: the same 32-bit hash, the same arithmetic in the
// same order, Python's rounding (half to even), so forge's frames.json checks it frame by frame.
// Each frame is computed when its tick comes, from the font tables below and apprentice.ts's own
// pixels: nothing at module load, no file read. Plain values only: register.tsx plays it.
import { base64, pixels } from './apprentice'
import type { Pose } from './apprentice'

export const SPLASH_MS = 80 // a frame
const DEFAULT = 0x01000000 // the terminal's own colour
const UPPER = 0x2580
const LOWER = 0x2584
const SPACE = 0x20

// ---------------------------------------------------------------- colour: WCAG, and the band
const lin = (c8: number) => {
  const c = c8 / 255
  return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4
}
// Python's round(): half to even. Math.round sends half up, and the frames drifted from forge's.
const pyRound = (v: number) => {
  const f = Math.floor(v)
  const d = v - f
  return d > 0.5 ? f + 1 : d < 0.5 ? f : f % 2 === 0 ? f : f + 1
}
const unlin = (v: number) => {
  v = Math.max(0, Math.min(1, v))
  const c = v <= 0.0031308 ? 12.92 * v : 1.055 * v ** (1 / 2.4) - 0.055
  return pyRound(c * 255)
}
const pack = (r: number, g: number, b: number) => (r << 16) | (g << 8) | b
type RGB = [number, number, number]
const rgb = (c: number): RGB => [(c >> 16) & 255, (c >> 8) & 255, c & 255]
const rgbLin = (c: number): RGB => {
  const [r, g, b] = rgb(c)
  return [lin(r), lin(g), lin(b)]
}
/** WCAG relative luminance. */
export const lum = (c: number) => {
  const [r, g, b] = rgbLin(c)
  return 0.2126 * r + 0.7152 * g + 0.0722 * b
}
/** WCAG contrast ratio. */
export const ratio = (a: number, b: number) => {
  const [la, lb] = [lum(a), lum(b)]
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05)
}

// Every colour the splash draws has a luminance in this band: at least 3.1:1 on black and
// 3.04:1 on white, so it reads on a dark terminal and a light one alike (#963).
export const LO = 0.105
export const HI = 0.295

const step = (c: number, d: number) => {
  const [r, g, b] = rgb(c)
  const k = (x: number) => Math.max(0, Math.min(255, x + d))
  return pack(k(r), k(g), k(b))
}

/** The colour's hue at luminance `target`: linear RGB scaled, or lifted toward white once a
 * channel would clip. Every colour the splash draws passes through here. */
function atLum(c: number, target: number): number {
  target = Math.max(LO, Math.min(HI, target))
  let [r, g, b] = rgbLin(c)
  let l = 0.2126 * r + 0.7152 * g + 0.0722 * b
  if (l <= 0) { r = g = b = 0; l = 0 }
  const s = l ? target / l : 0
  let [r2, g2, b2] = [r * s, g * s, b * s]
  if (Math.max(r2, g2, b2) > 1) { // clip, then mix toward white until the luminance is right
    [r2, g2, b2] = [Math.min(r2, 1), Math.min(g2, 1), Math.min(b2, 1)]
    let lo = 0
    let hi = 1
    for (let i = 0; i < 30; i++) {
      const t = (lo + hi) / 2
      const m = (x: number) => x + t * (1 - x)
      if (0.2126 * m(r2) + 0.7152 * m(g2) + 0.0722 * m(b2) < target) lo = t
      else hi = t
    }
    const m = (x: number) => x + hi * (1 - x)
    ;[r2, g2, b2] = [m(r2), m(g2), m(b2)]
  }
  let out = pack(unlin(r2), unlin(g2), unlin(b2))
  while (lum(out) > HI) out = step(out, -1) // rounding to 8 bits can step just outside
  while (lum(out) < LO) out = step(out, +1)
  return out
}

function mix(a: number, b: number, t: number): number {
  const [ra, rb] = [rgbLin(a), rgbLin(b)]
  const m = (i: 0 | 1 | 2) => unlin(ra[i] + (rb[i] - ra[i]) * t)
  return pack(m(0), m(1), m(2))
}

function ramp(stops: number[], u: number): number {
  u = Math.max(0, Math.min(1, u)) * (stops.length - 1)
  const i = Math.min(Math.trunc(u), stops.length - 2)
  return mix(stops[i]!, stops[i + 1]!, u - i)
}

// The palette: hues are named here, their luminance set where they are used. Made on the first
// play, not at load.
const WARM = [0xf0a030, 0xd97757, 0xe0485a] // CLAUDE'S: amber, Claude's coral, rose
const COOL = [0x10a8a0, 0x2a8cf0, 0x7a66f5, 0xc850e0] // WORKBENCH: teal, azure, indigo, violet
// The modeline's background: the theme's `claude` colour (rgb(215,119,87) in Claude Code's
// dark and light themes), which the bar the embers make must match: luminance 0.283, in the band.
export const BAR = 0xd77757
type Palette = { HEAT: number[]; SPARK: number[]; STEEL: number; WS: Record<string, number>; FLOOR: number
  DROP: number; TWINKLE: number[]; RING: number[]; SHINE: number[] }
let made: Palette | null = null
function palette(): Palette {
  if (made) return made
  const HEAT = [ // a letter's colour by its age in frames: gold, orange, red-orange, red, cherry, plum
    atLum(0xffc020, 0.295), atLum(0xff9a10, 0.29), atLum(0xff6010, 0.26),
    atLum(0xf03020, 0.20), atLum(0xc01838, 0.15), atLum(0x8a2a8a, 0.13),
  ]
  const STEEL = atLum(0x6c7a88, 0.18)
  // The cheer's rainbow (from "stream", #963): RING_STEPS hues at one luminance, and a lighter
  // shine for the sweep's leading edge, few enough colour pairs for the Raster's 1024.
  const hue = (h: number) => {
    const k = (n: number) => (n + h / 60) % 6
    const ch = (n: number) => Math.round(255 * (1 - Math.max(0, Math.min(k(n), 4 - k(n), 1))))
    return pack(ch(5), ch(3), ch(1))
  }
  const RING: number[] = []
  const SHINE: number[] = []
  for (let i = 0; i < RING_STEPS; i++) {
    RING.push(atLum(hue((i * 360) / RING_STEPS), 0.2))
    SHINE.push(atLum(mix(hue((i * 360) / RING_STEPS), 0xffffff, 0.4), 0.29))
  }
  made = {
    HEAT,
    SPARK: [atLum(0xffd040, 0.295), atLum(0xff9010, 0.27), atLum(0xf04818, 0.20), atLum(0xb02020, 0.13)],
    STEEL,
    WS: { F: atLum(0x9aa8b6, 0.29), S: STEEL, D: atLum(0x4c5864, 0.11), W: atLum(0xb07840, 0.22), w: atLum(0x80502a, 0.11) },
    FLOOR: atLum(0x808890, 0.16),
    DROP: atLum(0x2a90ff, 0.24),
    TWINKLE: [atLum(0xffc030, 0.295), atLum(0x30e0ff, 0.295)],
    RING,
    SHINE,
  }
  return made
}
const RING_STEPS = 24

// ---------------------------------------------------------------- the fonts
// BIG: 5 px wide (W 7, N 6, ' 2), 10 tall: two-pixel strokes, bars on whole cells (no seams).
// prettier-ignore
const BIG: Record<string, string[]> = {
  C: ['.XXXX', 'XXXXX', 'XX...', 'XX...', 'XX...', 'XX...', 'XX...', 'XX...', 'XXXXX', '.XXXX'],
  L: ['XX...', 'XX...', 'XX...', 'XX...', 'XX...', 'XX...', 'XX...', 'XX...', 'XXXXX', 'XXXXX'],
  A: ['.XXX.', 'XXXXX', 'XX.XX', 'XX.XX', 'XXXXX', 'XXXXX', 'XX.XX', 'XX.XX', 'XX.XX', 'XX.XX'],
  U: ['XX.XX', 'XX.XX', 'XX.XX', 'XX.XX', 'XX.XX', 'XX.XX', 'XX.XX', 'XX.XX', 'XXXXX', '.XXX.'],
  D: ['XXXX.', 'XXXXX', 'XX.XX', 'XX.XX', 'XX.XX', 'XX.XX', 'XX.XX', 'XX.XX', 'XXXXX', 'XXXX.'],
  E: ['XXXXX', 'XXXXX', 'XX...', 'XX...', 'XXXX.', 'XXXX.', 'XX...', 'XX...', 'XXXXX', 'XXXXX'],
  "'": ['XX', 'XX', '.X', 'X.', '..', '..', '..', '..', '..', '..'],
  S: ['.XXXX', 'XXXXX', 'XX...', 'XX...', 'XXXX.', '.XXXX', '...XX', '...XX', 'XXXXX', 'XXXX.'],
  W: ['XX...XX', 'XX...XX', 'XX...XX', 'XX...XX', 'XX.X.XX', 'XX.X.XX', 'XXXXXXX', 'XXXXXXX', 'XXX.XXX', 'XX...XX'],
  O: ['.XXX.', 'XXXXX', 'XX.XX', 'XX.XX', 'XX.XX', 'XX.XX', 'XX.XX', 'XX.XX', 'XXXXX', '.XXX.'],
  R: ['XXXX.', 'XXXXX', 'XX.XX', 'XX.XX', 'XXXXX', 'XXXX.', 'XX.XX', 'XX.XX', 'XX.XX', 'XX.XX'],
  K: ['XX.XX', 'XX.XX', 'XX.XX', 'XXXX.', 'XXX..', 'XXX..', 'XXXX.', 'XX.XX', 'XX.XX', 'XX.XX'],
  B: ['XXXX.', 'XXXXX', 'XX.XX', 'XX.XX', 'XXXX.', 'XXXXX', 'XX.XX', 'XX.XX', 'XXXXX', 'XXXX.'],
  N: ['XX..XX', 'XXX.XX', 'XXX.XX', 'XXXXXX', 'XXXXXX', 'XX.XXX', 'XX.XXX', 'XX..XX', 'XX..XX', 'XX..XX'],
  H: ['XX.XX', 'XX.XX', 'XX.XX', 'XX.XX', 'XXXXX', 'XXXXX', 'XX.XX', 'XX.XX', 'XX.XX', 'XX.XX'],
}
// SMALL: 3 px wide (W 5, N 4, ' 1), 5 tall and a blank row: the narrow or short pane's.
// prettier-ignore
const SMALL: Record<string, string[]> = {
  C: ['XXX', 'X..', 'X..', 'X..', 'XXX'], L: ['X..', 'X..', 'X..', 'X..', 'XXX'],
  A: ['.X.', 'X.X', 'XXX', 'X.X', 'X.X'], U: ['X.X', 'X.X', 'X.X', 'X.X', 'XXX'],
  D: ['XX.', 'X.X', 'X.X', 'X.X', 'XX.'], E: ['XXX', 'X..', 'XX.', 'X..', 'XXX'],
  "'": ['X', 'X', '.', '.', '.'], S: ['XXX', 'X..', 'XXX', '..X', 'XXX'],
  W: ['X...X', 'X...X', 'X.X.X', 'X.X.X', '.X.X.'], O: ['XXX', 'X.X', 'X.X', 'X.X', 'XXX'],
  R: ['XX.', 'X.X', 'XX.', 'X.X', 'X.X'], K: ['X.X', 'X.X', 'XX.', 'X.X', 'X.X'],
  B: ['XX.', 'X.X', 'XX.', 'X.X', 'XX.'], N: ['X..X', 'XX.X', 'X.XX', 'X..X', 'X..X'],
  H: ['X.X', 'X.X', 'XXX', 'X.X', 'X.X'],
}
// The words it forges: Claude Code's. OpenCode's plugin forges its own name alone (#1333: the owner,
// 2026-11-24, the pane there "would just only be called workbench"); its colours by line, the last
// line's cool (WORKBENCH), any above it warm (CLAUDE'S).
export const LINES: readonly string[] = ["CLAUDE'S", 'WORKBENCH']
const glyphOf = (font: Record<string, string[]>, ch: string) =>
  font === SMALL ? [...SMALL[ch]!, '.'.repeat(SMALL[ch]![0]!.length)] : BIG[ch]!
const lineWidth = (font: Record<string, string[]>, text: string) =>
  [...text].reduce((n, c) => n + font[c]![0]!.length, 0) + text.length - 1

// ---------------------------------------------------------------- determinism
/** forge.py's 32-bit hash of small ints, to [0, 1). */
function h32(...xs: number[]): number {
  let h = 0x811c9dc5
  for (const x of xs) {
    h = (h ^ (x >>> 0)) >>> 0
    h = Math.imul(h, 0x01000193) >>> 0
    h = (h ^ (h >>> 15)) >>> 0
    h = Math.imul(h, 0x2c1b3c6d) >>> 0
    h = (h ^ (h >>> 12)) >>> 0
  }
  return h / 2 ** 32
}

// ---------------------------------------------------------------- the timeline (frame numbers)
const STRIKES = [6, 10, 14, 18, 22, 26] // impacts: swing at s, s+1; the wrench up again at s+2, s+3
const GLINT = [33, 42] as const // the glint's sweep
const SWEEP = [42, 47] as const // the cheer's rainbow, once through both words (#963)
const HOP: Record<number, [number, number]> = { 45: [-2, -3], 46: [-4, -2], 47: [-6, 3], 48: [-7, 6] }
const WALK = [49, 54] as const // to its spot on the Tasks page, there at 53
const RISE = 46 // the words leave as embers: rising away (forge), or falling into the bar
const FALL = 5 // frames an ember takes to fall into the bar, the last its landing
const SLIDE = [57, 60] as const // the bar moves from the pane's foot to the modeline's row, when that is higher
const FORGE_FRAMES = 57 // 4.56 s, as forge: the words rise away
const BAR_FRAMES = 62 // 4.96 s: the words fall into the bar

type SplashPose = Pose | 'wipe' | 'step_a' | 'step_b'
// The splash's own poses, in apprentice.ts's style (rows that differ from REST); the pane's own
// poses don't change.
const OWN: Record<string, Record<number, string>> = {
  wipe: { // the forearm drawn across the brow, over the visor's top edge
    3: '.yyyyyyyyyyyy...', 4: '.SSSSSSSSVVs....', 5: 'S.sVEEVVEEVs....',
    6: '..sVVVVVVVVs....', 7: '...ssssssss.....', 8: '.SSSSSSSSSSSS...',
  },
  step_a: { 11: '.dd.......dd....' }, // walking left: feet apart, then together
  step_b: { 10: 'S.SSSSSSSSSS.S..', 11: '...dd....dd.....' },
}

/** The apprentice's pose at frame f. */
export function poseAt(f: number): SplashPose {
  if (f <= 3) return f === 2 ? 'blink' : 'rest'
  if (f <= 5) return 'work'
  for (let i = 0; i < STRIKES.length; i++) {
    const s = STRIKES[i]!
    if (s <= f && f < s + 2) return 'swing'
    if (s + 2 <= f && f < s + 4) return i + 1 < STRIKES.length ? 'work' : 'rest'
  }
  if (f < 30) return 'rest'
  if (f < 36) return 'think'
  if (f >= 36 && f < 42) return (['ask', 'wipe', 'ask', 'wipe', 'ask', 'rest'] as const)[f - 36]!
  if (f >= 42 && f <= 46) return 'tada'
  if (f === 47 || f === 48) return 'rest'
  if (f >= WALK[0] && f < WALK[1]) return (f - WALK[0]) % 2 === 0 ? 'step_a' : 'step_b'
  return f === 55 || f === 60 ? 'blink' : 'rest'
}
const sprite = (pose: SplashPose) => (pose in OWN ? pixels('rest', OWN[pose]) : pixels(pose as Pose))

// ---------------------------------------------------------------- the plan
type Glyph = {
  line: number; x: number; y: number; w: number; h: number; u0: number; ul: number
  px: [number, number][]; fin: number[]; launch: number; born: number; tx: number; ty: number
}
/** Where the Tasks page will be, in the Raster's rows: the apprentice's top row (its stage) and
 * the modeline's row, or null where the pane can't know it exactly (the words rise away). */
export type Target = { stage: number; bar: number | null }
export type Plan = {
  cols: number; rows: number; big: boolean; glyphs: Glyph[]; X0: number; Y0: number; floorY: number
  sparks: [number, number, number, number, number, number][]; target: Target
  /** decided as the words leave (RISE): 'fall' into the bar, or 'rise' away; null before */
  ending: 'fall' | 'rise' | null
  barAt: number[]; left: number; top: number
}

/** The font a pane of cols x rows gets: big from 56 columns and 24 rows, small from 40 and 20,
 * none below (where the pane already draws the apprentice as its one-line face). */
export function splashFont(cols: number, rows: number): 'big' | 'small' | null {
  if (cols < 40 || rows < 20 || cols > 512 || rows > 256) return null
  return cols >= 56 && rows >= 24 ? 'big' : 'small'
}

/**
 * Lays the splash out for a Raster of cols x rows: forge's design in the rows above the bottom
 * one (forge's last row, under its floor, is where the Tasks page's tabs are), and the bottom row
 * for the bar. Null where the pane is too small. `lines`, the words (no wider than the font's
 * pane: WORKBENCH alone fits either).
 */
export function plan(cols: number, rows: number, target: Target, lines: readonly string[] = LINES): Plan | null {
  const font = splashFont(cols, rows)
  if (!font) return null
  const F = font === 'big' ? BIG : SMALL
  const H = rows - 1 // forge's rows
  const gh = glyphOf(F, 'C').length
  const floorY = 2 * (H - 2) // the floor's pixel row: the upper half of row H-2
  const Y0 = floorY - 18 // the apprentice on its crate (6 px tall)
  const X0 = Math.floor((cols - 28) / 2) // the apprentice, crate and anvil centred (28 px)
  const titleRows = lines.length * Math.floor(gh / 2) + lines.length - 1 // the lines and a row between each
  const top = Math.max(1, Math.floor((Math.floor(Y0 / 2) - 1 - titleRows) / 2) + 1)
  const glyphs: Glyph[] = []
  lines.forEach((text, li) => {
    const ul = lineWidth(F, text)
    let x = Math.floor((cols - ul) / 2)
    const y = 2 * (top + li * (Math.floor(gh / 2) + 1))
    const u0 = x
    for (const ch of text) {
      const g = glyphOf(F, ch)
      const px: [number, number][] = []
      g.forEach((r, j) => [...r].forEach((c, i) => { if (c === 'X') px.push([x + i, y + j]) }))
      glyphs.push({ line: li, x, y, w: g[0]!.length, h: gh, u0, ul, px, fin: [], launch: 0, born: 0, tx: 0, ty: 0 })
      x += g[0]!.length + 1
    }
  })
  // when each glyph's comet leaves the anvil and when the glyph is born (lands)
  const per = Math.ceil(glyphs.length / STRIKES.length)
  glyphs.forEach((g, j) => {
    const s = STRIKES[Math.floor(j / per)]!
    const k = j % per
    g.launch = s + (k > 1 ? 1 : 0)
    g.born = s + 2 + k
    g.tx = g.x + Math.floor(g.w / 2)
    g.ty = g.y + Math.floor(g.h / 2)
    // each pixel's final colour, once: lit from above, like metal
    const stops = g.line === lines.length - 1 ? COOL : WARM
    g.fin = g.px.map(([x, y]) => atLum(ramp(stops, (x - g.u0) / Math.max(1, g.ul - 1)),
      0.275 - 0.13 * ((y - g.y) / Math.max(1, g.h - 1))))
  })
  // every spark, closed form: (born, x0, y0, vx, vy, life)
  const sparks: Plan['sparks'] = []
  const [sx, sy] = [X0 + 15, Y0 + 11]
  STRIKES.forEach((s, si) => {
    for (let k = 0; k < 14; k++) {
      const vx = (h32(si, k, 1) - 0.5) * 5.0 + 0.6
      const vy = -1.5 - h32(si, k, 2) * 3.0
      sparks.push([s, sx, sy, vx, vy, 3 + Math.trunc(h32(si, k, 3) * 4)])
    }
  })
  glyphs.forEach((g, gi) => {
    for (let k = 0; k < 6; k++) {
      const a = h32(gi, k, 4) * 2 * Math.PI
      const sp = 0.8 + h32(gi, k, 5) * 1.4
      sparks.push([g.born, g.tx, g.ty, Math.cos(a) * sp, Math.sin(a) * sp - 0.5, 2 + Math.trunc(h32(gi, k, 6) * 2)])
    }
  })
  // where the falling embers land in the bar, and when: each column lights at its first ember,
  // the ones none reach from the nearest landing, `k` columns a frame (#963)
  const landed = new Map<number, number>()
  for (const g of glyphs) {
    for (const [x, y] of g.px) {
      const at = leaveAt(cols, x, y) + FALL - 1
      const c = Math.max(0, Math.min(cols - 1, pyRound(x + fallJitter(x, y) * FALL)))
      landed.set(c, Math.min(landed.get(c) ?? Infinity, at))
    }
  }
  const lit = (k: number) => Array.from({ length: cols }, (_, c) => {
    let at = Infinity
    for (const [e, a] of landed) at = Math.min(at, a + Math.ceil(Math.abs(c - e) / k))
    return at
  })
  // The title keeps its width at any pane width, so one column a frame left a wide pane's bar
  // unlit, and its label unwritten, at the last frame (2026-11-21, review of #963: 23 columns at
  // 100, 121 at 200): the light spreads as fast as it must to fill the row before it slides up.
  // One a frame wherever that is enough, so a 60-column pane plays as it was judged.
  const due = SLIDE[0] - 1
  let [lo, hi] = [1, cols]
  while (lo < hi) {
    const k = (lo + hi) >> 1
    if (Math.max(...lit(k)) <= due) hi = k
    else lo = k + 1
  }
  const barAt = lit(lo).map(a => Math.min(a, due))
  const left = Math.min(...glyphs.map(g => g.u0))
  return { cols, rows, big: font === 'big', glyphs, X0, Y0, floorY, sparks, target, ending: null, barAt, left, top: 2 * top }
}
const leaveAt = (cols: number, x: number, y: number) => RISE + Math.trunc((x / cols) * 4 + h32(x, y, 21) * 3)
const fallJitter = (x: number, y: number) => (h32(x, y, 22) - 0.5) * 1.2

/** How many frames this plan plays: more when the words fall into the bar. */
export const frameCount = (p: Plan) => (p.ending === 'fall' ? BAR_FRAMES : FORGE_FRAMES)

// ---------------------------------------------------------------- the frame
function glyphColor(p: Plan, g: Glyph, i: number, f: number): number {
  const { HEAT, RING, SHINE } = palette()
  const [x, y] = g.px[i]!
  const a = f - g.born
  const fin = g.fin[i]!
  if (a < HEAT.length) {
    let k = a
    if (a < 5 && h32(x, y, f, 7) < 0.3) k = Math.max(0, a - 1) // embers flicker a step hotter
    return HEAT[k]!
  }
  const t = (a - HEAT.length + 1) / 5
  const last = HEAT[HEAT.length - 1]!
  if (t < 1) return atLum(mix(last, fin, t), lum(last) + (lum(fin) - lum(last)) * t)
  // the cheer's rainbow: a diagonal band left to right, a lighter shine at its front (#963)
  if (SWEEP[0] <= f && f < SWEEP[1]) {
    const pos = x - p.left + (y - p.top) * 0.6
    const d = (f - SWEEP[0]) * 16 + 6 - pos
    if (d >= 0 && d < 20) {
      const h = ((pyRound((pos * 9 - (f - SWEEP[0]) * 30) / 15) % RING_STEPS) + RING_STEPS) % RING_STEPS
      return (d < 3 ? SHINE : RING)[h]!
    }
  }
  if (GLINT[0] <= f && f <= GLINT[1]) {
    const band = (f - GLINT[0]) * 8 - 12
    const d = x + (y - g.y) - band
    if (d >= 0 && d < 4) return atLum(mix(fin, 0xffffff, 0.35), HI)
  }
  return fin
}

const CRATE = ['WWWWWWWWWWWW', 'wWWWWWWWWWWw', 'wwwwwwwwwwww', 'wWWWWWWWWWWw', 'wWWWWWWWWWWw', 'wwwwwwwwwwww']
const ANVIL = ['FFFFFFFFFFFFFF', 'SSSSSSSSSSSSS.', '.SSSSSSSSSS...', '....SSSSS.....', '...SSSSSSS....', '..DDDDDDDDD...']
const GRAVITY = 0.55
const TWINKLES: [number, number, number, number][] = [[39, 1, -1, -1], [41, 12, 6, 1], [43, 7, 4, -1], [40, 15, 5, 9]]
const SWEAT: [number, number][] = [[13, 3], [15, 1], [17, 1], [18, 4]] // frames 37-40

/** The bar's row at frame f: the pane's foot, then up to the modeline's row once it is lit. */
function barRow(p: Plan, f: number): number {
  const foot = p.rows - 1
  const to = Math.max(0, Math.min(foot, p.target.bar ?? foot))
  if (f < SLIDE[0]) return foot
  if (f >= SLIDE[1] - 1) return to
  return pyRound(foot + ((to - foot) * (f - SLIDE[0] + 1)) / (SLIDE[1] - SLIDE[0]))
}

/**
 * Frame f's cells, row-major [codePoint, fg, bg] (0x01000000 the terminal's own colour), as a
 * Raster packs them. The ending is chosen at the first frame of it from the plan's target.
 */
export function frameCells(p: Plan, f: number): Uint32Array {
  const P = palette()
  const { cols, rows, X0, Y0, floorY: fy } = p
  if (f >= RISE && p.ending === null) p.ending = p.target.bar === null ? 'rise' : 'fall'
  const px = new Int32Array(cols * rows * 2).fill(-1)
  const put = (x: number, y: number, c: number) => {
    const [ix, iy] = [pyRound(x), pyRound(y)]
    if (ix >= 0 && ix < cols && iy >= 0 && iy < rows * 2) px[iy * cols + ix] = c
  }
  // the workshop: in by a pixel dissolve (f0-2), out the same way once the apprentice is off it
  const shown = (x: number, y: number, salt: number) => {
    const h = h32(x, y, salt)
    if (f < 3) return h < (f + 1) / 3
    if (f >= 48) return h >= (f - 47) / 4
    return true
  }
  for (let x = 0; x < cols; x++) if (shown(x, fy, 11)) put(x, fy, P.FLOOR)
  CRATE.forEach((row, j) => [...row].forEach((ch, i) => {
    if (shown(X0 + 1 + i, Y0 + 12 + j, 12)) put(X0 + 1 + i, Y0 + 12 + j, P.WS[ch]!)
  }))
  const last = STRIKES[STRIKES.length - 1]!
  ANVIL.forEach((row, j) => [...row].forEach((ch, i) => {
    if (ch === '.') return
    const [x, y] = [X0 + 13 + i, Y0 + 12 + j]
    let c = P.WS[ch]!
    if (STRIKES.includes(f) && j === 0 && Math.abs(x - (X0 + 15)) <= 3) c = P.HEAT[0]! // the blow lights the face
    if (shown(x, y, 13)) put(x, y, c)
  }))
  // the ingot on the face: kindles, glows through the strikes, cools after the last
  let ic: number
  if (f < 6) ic = P.HEAT[Math.max(0, 5 - f)]!
  else if (f <= last + 1) ic = STRIKES.includes(f) ? P.HEAT[0]! : P.HEAT[1 + (f % 2)]!
  else ic = f < last + 12 ? P.HEAT[Math.min(P.HEAT.length - 1, 2 + Math.floor((f - last - 2) / 2))]! : P.STEEL
  for (let i = 0; i < 5; i++) if (shown(X0 + 14 + i, Y0 + 11, 14)) put(X0 + 14 + i, Y0 + 11, ic)

  // the words: each glyph from its birth (a two-frame reveal from where its comet lands)
  const falling: [number, number, number][] = []
  const foot = 2 * (rows - 1)
  for (const g of p.glyphs) {
    const a = f - g.born
    if (a < 0) continue
    g.px.forEach(([x, y], i) => {
      if (a === 0 && Math.abs(x - g.tx) + Math.abs(y - g.ty) * 0.6 > 3) return
      let col = glyphColor(p, g, i, f)
      if (f >= RISE) {
        const k = f - leaveAt(cols, x, y)
        if (p.ending === 'fall') {
          if (k >= FALL - 1) return // landed: it is the bar now
          if (k >= 0) { // falling, faster as it goes, cooling, in front of the apprentice
            const c0 = glyphColor(p, g, i, f - k)
            const u = (k + 1) / FALL
            falling.push([x + fallJitter(x, y) * (k + 1), y + (foot - y) * u * u, atLum(c0, lum(c0) - 0.03 * k)])
            return
          }
        } else { // rising away as embers, a wave from the left (forge)
          if (k >= 3) return
          if (k >= 0) {
            const jit = (h32(x, y, 22) - 0.5) * 1.6
            x = x + jit * k
            y = y - 2 * k - (k > 1 ? 1 : 0)
            col = atLum(col, lum(col) - 0.06 * k)
          }
        }
      }
      put(x, y, col)
    })
  }

  // twinkles on the finished words: a four-point star, grown then shrunk
  TWINKLES.forEach(([t0, gi, dx, dy], n) => {
    if (gi >= p.glyphs.length) return
    const k = f - t0
    if (k < 0 || k >= 3) return
    const g = p.glyphs[gi]!
    // its centre two pixels down from the top at most, so its upper arm isn't cut off where the
    // title starts at the pane's top (24 rows big, 20 small; 2026-11-21, review of #963)
    const [cx, cy] = [g.x + dx, Math.max(2, g.y + dy)]
    const col = P.TWINKLE[n % 2]!
    put(cx, cy, col)
    for (let d = 1; d <= [1, 2, 0][k]!; d++)
      for (const [ox, oy] of [[d, 0], [-d, 0], [0, d], [0, -d]] as const) put(cx + ox, cy + oy, col)
  })

  // the comets: a spark from the anvil to each glyph, on a curve, with a cooling tail
  const [sx, sy] = [X0 + 15, Y0 + 11]
  for (const g of p.glyphs) {
    if (!(g.launch <= f && f < g.born)) continue
    const side = g.tx < sx ? -1 : 1
    const [cx, cy] = [(sx + g.tx) / 2 + side * 6, Math.min(sy, g.ty) - 6]
    const at = (s: number): [number, number] => [
      (1 - s) * (1 - s) * sx + 2 * (1 - s) * s * cx + s * s * g.tx,
      (1 - s) * (1 - s) * sy + 2 * (1 - s) * s * cy + s * s * g.ty,
    ]
    const s = (f - g.launch + 1) / (g.born - g.launch + 1)
    P.SPARK.forEach((col, k) => {
      const q = at(Math.max(0, s - 0.09 * k))
      put(q[0], q[1], col)
    })
    const [hx, hy] = at(s)
    put(hx + 1, hy, P.SPARK[0]!)
  }

  // the apprentice: on its crate, then off it and to its place on the Tasks page
  let [ax, ay] = [X0, Y0]
  const hop = HOP[f]
  if (hop) [ax, ay] = [X0 + hop[0], Y0 + hop[1]]
  else if (f >= WALK[0]) {
    const [hx, hy] = [X0 + HOP[48]![0], Y0 + HOP[48]![1]]
    const [tx, ty] = [0, 2 * Math.max(0, Math.min(rows - 6, p.target.stage))]
    const t = Math.min(1, (f - WALK[0] + 1) / (WALK[1] - WALK[0]))
    ;[ax, ay] = t >= 1 ? [tx, ty] : [pyRound(hx + (tx - hx) * t), pyRound(hy + (ty - hy) * t)]
  }
  sprite(poseAt(f)).forEach((row, j) => row.forEach((c, i) => { if (c !== null) put(ax + i, ay + j, c) }))
  // a bead of sweat, flicked off as it wipes
  if (f >= 37 && f <= 40) {
    const [dx, dy] = SWEAT[f - 37]!
    put(ax + dx, ay + dy, P.DROP)
    put(ax + dx, ay + dy + 1, P.DROP)
  }
  for (const [x, y, c] of falling) put(x, y, c)

  // the sparks, in front of everything
  for (const [b, x0, y0, vx, vy, life] of p.sparks) {
    const t = f - b
    if (t < 0 || t >= life) continue
    const i = Math.min(P.SPARK.length - 1, Math.trunc((t / life) * P.SPARK.length))
    put(x0 + vx * t, y0 + vy * t + (GRAVITY * t * t) / 2, P.SPARK[i]!)
    if (t) put(x0 + vx * (t - 0.5), y0 + vy * (t - 0.5) + (GRAVITY * (t - 0.5) ** 2) / 2, P.SPARK[Math.min(3, i + 1)]!)
  }

  // into cells: an upper half block, top pixel over bottom
  const out = new Uint32Array(cols * rows * 3)
  for (let r = 0; r < rows; r++) {
    for (let c = 0; c < cols; c++) {
      const [top, bot] = [px[2 * r * cols + c]!, px[(2 * r + 1) * cols + c]!]
      const o = (r * cols + c) * 3
      if (top < 0 && bot < 0) out.set([SPACE, DEFAULT, DEFAULT], o)
      else if (top < 0) out.set([LOWER, bot, DEFAULT], o)
      else out.set([UPPER, top, bot < 0 ? DEFAULT : bot], o)
    }
  }
  // the bar the embers make, over everything in its row: each column lit by its first ember (a
  // gold flash, then the modeline's colour) (#963). No words on it: the modeline's are the
  // theme's `inverseText` and bold, which a Raster's cells can't name, so drawn here they changed
  // colour and weight as the page took over (2026-11-21, review of #963); the page's own modeline
  // brings them, on the row the bar has become.
  if (p.ending === 'fall') {
    const r = barRow(p, f)
    for (let c = 0; c < cols; c++) {
      const at = p.barAt[c]!
      if (f < at) continue
      out.set([SPACE, DEFAULT, f === at ? P.HEAT[0]! : BAR], (r * cols + c) * 3)
    }
  }
  return out
}

/** A frame's cells as the Raster's `cells` string. */
export const packed = (cells: Uint32Array) => base64(new Uint8Array(cells.buffer, cells.byteOffset, cells.byteLength))
