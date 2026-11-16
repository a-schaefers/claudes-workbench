// The apprentice: the local agent as the pane draws it, a small robot in a hard hat at the foot
// of the transcript, with a speech bubble saying what it is doing. Its pose follows the followed
// task's phase and its chest lamp is the phase's colour, so a glance says how things stand.
// Truecolor pixels in a terminal's Raster (two pixels to a cell, one above the other); a
// one-line face where there is no Raster. Plain values only: register.tsx draws and animates it.

export type Pose = 'rest' | 'blink' | 'think' | 'work' | 'swing' | 'tada' | 'ask' | 'slump' | 'sleep'

// One letter a colour, `.` see-through. `L` is the chest lamp, coloured by the pose.
const PALETTE: Record<string, number> = {
  Y: 0xf5c242, // hard hat
  y: 0xd99a1e, // its brim
  s: 0x5f7385, // head frame, shaded
  S: 0x9db0c0, // body
  V: 0x16212b, // visor
  E: 0x7ae7ff, // eyes
  e: 0x3f7f8f, // eyes, downcast
  d: 0x4a5865, // feet
  W: 0x8e9aa6, // wrench
}

const LAMP: Record<Pose, number> = {
  rest: 0xd97757, blink: 0xd97757, think: 0xd97757, work: 0xd97757, swing: 0xd97757,
  tada: 0x5fbf6a, ask: 0xffa630, slump: 0xe5484d, sleep: 0x34404b,
}

// prettier-ignore
const REST = [
  '.....YYYY.......', // 0
  '...YYYYYYYY.....', // 1
  '..YYYYYYYYYY....', // 2
  '.yyyyyyyyyyyy...', // 3
  '..sVVVVVVVVs....', // 4
  '..sVEEVVEEVs....', // 5
  '..sVVVVVVVVs....', // 6
  '...ssssssss.....', // 7
  '.SSSSSSSSSSSS...', // 8
  'S.SSSSLLSSSS.S..', // 9
  'S.SSSSSSSSSS.S..', // 10
  '..dd......dd....', // 11
]

const EYES_OFF = '..sVVVVVVVVs....'
// Each pose: the rows that differ from REST, by row number.
const POSES: Record<Pose, Record<number, string>> = {
  rest: {},
  blink: { 5: EYES_OFF },
  think: { 4: '..sVVEEVVEEs....', 5: EYES_OFF }, // eyes up and aside
  work: { // the wrench raised
    4: '..sVVVVVVVVsW.W.', 5: '..sVEEVVEEVs.W..', 6: '..sVVVVVVVVs.S..', 7: '...ssssssss..S..',
    8: '.SSSSSSSSSSSSS..', 9: 'S.SSSSLLSSSS....', 10: 'S.SSSSSSSSSS....',
  },
  swing: { // and brought down
    9: 'S.SSSSLLSSSS.S.W', 10: 'S.SSSSSSSSSS.SW.', 11: '..dd......dd...W',
  },
  tada: { // both arms up
    6: 'S.sVVVVVVVVs.S..', 7: 'S..ssssssss..S..', 8: 'SSSSSSSSSSSSSS..',
    9: '..SSSSLLSSSS....', 10: '..SSSSSSSSSS....',
  },
  ask: { // a hand up
    5: 'S.sVEEVVEEVs....', 6: 'S.sVVVVVVVVs....', 7: 'S..ssssssss.....', 8: 'SSSSSSSSSSSSS...',
  },
  slump: { 5: EYES_OFF, 6: '..sVeeVVeeVs....' },
  sleep: { 5: EYES_OFF },
}

export const COLUMNS = REST[0].length
export const ROWS = REST.length / 2
// The cell row the speech bubble's tail meets: the jaw.
export const MOUTH = 3

const DEFAULT_COLOR = 0x01000000 // the terminal's own
const UPPER_HALF = 0x2580
const LOWER_HALF = 0x2584
const SPACE = 0x20
const B64 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'

const base64 = (bytes: Uint8Array) => {
  let out = ''
  for (let i = 0; i < bytes.length; i += 3) {
    const n = (bytes[i] << 16) | ((bytes[i + 1] ?? 0) << 8) | (bytes[i + 2] ?? 0)
    out += B64[(n >> 18) & 63] + B64[(n >> 12) & 63] +
      (i + 1 < bytes.length ? B64[(n >> 6) & 63] : '=') + (i + 2 < bytes.length ? B64[n & 63] : '=')
  }
  return out
}

/** The picture in a pose, as rows of colours (null: see-through). */
export function pixels(pose: Pose): (number | null)[][] {
  const rows = REST.map((row, i) => POSES[pose][i] ?? row)
  return rows.map(row => [...row].map(ch => (ch === 'L' ? LAMP[pose] : PALETTE[ch] ?? null)))
}

const drawn = new Map<Pose, string>()

/** A Raster's `cells` for the pose: each cell an upper half block, top pixel over bottom. */
export function cells(pose: Pose): string {
  const known = drawn.get(pose)
  if (known) return known
  const grid = pixels(pose)
  const view = new DataView(new ArrayBuffer(COLUMNS * ROWS * 12))
  let at = 0
  const put = (word: number) => { view.setUint32(at, word, true); at += 4 }
  for (let row = 0; row < ROWS; row++) {
    for (let col = 0; col < COLUMNS; col++) {
      const top = grid[row * 2][col]
      const bottom = grid[row * 2 + 1][col]
      if (top === null && bottom === null) [SPACE, DEFAULT_COLOR, DEFAULT_COLOR].forEach(put)
      else if (top === null) [LOWER_HALF, bottom!, DEFAULT_COLOR].forEach(put)
      else [UPPER_HALF, top, bottom ?? DEFAULT_COLOR].forEach(put)
    }
  }
  const out = base64(new Uint8Array(view.buffer))
  drawn.set(pose, out)
  return out
}

/** The face alone, for a surface without a Raster. */
export const FACE: Record<Pose, string> = {
  rest: '[•_•]', blink: '[-_-]', think: '[°_°]', work: '[•_•]⚒', swing: '[•_•]⚒', tada: '\\[^_^]/',
  ask: '[•_•]?', slump: '[;_;]', sleep: '[-_-]ᶻ',
}
