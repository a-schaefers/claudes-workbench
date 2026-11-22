// The modeline's activity (#976; the owner, 2026-11-21: "[roobot-guy] Claude's Workbench - [current
// thing we are doing one liner here scrolls animated like a leaderboard ...]"). The line comes from
// the tasks the pane already polls, never from Claude: a tool call re-reads Claude's whole
// context, so a line Claude wrote at each dispatch would cost more than it says. A line wider than
// the bar's room scrolls like a ticker in a one-row Raster repainted by $.ui.blit, as the
// apprentice is: no pane redraw per step.
import type { WbTask } from '../types'
import { base64 } from './apprentice'
import { BAR } from './splash'

/** What each phase says on the modeline. */
export const SAID: Record<string, string> = {
  queued: 'queued', running: 'writing', testing: 'testing', asking: 'asking Claude',
  review: 'awaiting review', stuck: 'stuck', failed: 'failed',
}

/** The ticker's step: a column every TICKER_MS. */
export const TICKER_MS = 200
/** The blank columns between the line's end and its next start as it loops. */
export const TICKER_GAP = 6

/** The text after the name: each open task's name, round and phase, the oldest first; '' with none. */
export function activity(open: WbTask[]): string {
  return [...open]
    .filter(t => SAID[t.phase] && !t.parked)
    .sort((a, b) => a.createdAt - b.createdAt)
    .map(t => `${t.name}: ${t.round > 0 ? `round ${t.round}, ` : ''}${SAID[t.phase]}`)
    .join('  ·  ')
}

// On the bar's coral (splash.ts's BAR, the theme's `claude`), the dark theme's `inverseText`: a
// Raster's cells take colours, not theme keys. Black on the coral is 6.7:1. The engine keeps a
// Raster's colours at four bits a channel, so BAR is drawn rgb(221,119,85) beside the theme's
// (215,119,87): as near as a Raster gets, not told apart by eye (checked in truecolor, 2026-11-21).
const INK = 0x000000

/** One column a character for sure, as the Raster's grid needs: anything else is drawn as '?'. */
const glyph = (ch: string) => (/^[\x20-\x7e -ɏ·]$/.test(ch) ? ch.codePointAt(0)! : 0x3f)

/**
 * The ticker's `width` cells at `offset`: the line, then TICKER_GAP blanks, looping. Row-major
 * [codePoint, fg, bg] words, packed as a Raster takes them.
 */
export function tickerCells(line: string, offset: number, width: number): string {
  const loop = [...line, ...' '.repeat(TICKER_GAP)]
  const view = new DataView(new ArrayBuffer(width * 12))
  for (let col = 0; col < width; col++) {
    const ch = loop[(offset + col) % loop.length]!
    view.setUint32(col * 12, glyph(ch), true)
    view.setUint32(col * 12 + 4, INK, true)
    view.setUint32(col * 12 + 8, BAR, true)
  }
  return base64(new Uint8Array(view.buffer))
}

/** The steps one loop takes: the line and its gap. */
export const loopLength = (line: string) => [...line].length + TICKER_GAP
