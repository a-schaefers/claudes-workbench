// The modeline (#1283, the owner, 2026-11-24): the pane's last row, `[•_•] Claude's Workbench - <the
// web app's link>`, always. Stats and Settings moved to the web app, so the link is how the user
// gets there; the tasks' news stays in the Tasks view, the status line and the notices (the
// ticker the modeline carried before, #976, went with it). `wb chat url --json` (chat.ensure)
// says how the web app stands: its link while up, why not otherwise.
import type { WbChat, WbLine } from '../types'
import type { ChatLink, WatchLine } from '../types/wire.js'
import { FACE } from './apprentice'

export const MODELINE = "Claude's Workbench"

/** The link a modeline shows: `wb chat url --json` as the mod last read it (WbChat's `link`). */
export type Link = WbChat['link']
// every state chat.ensure says (wire.d.ts's ChatLink), and none it doesn't
const STATES: Record<ChatLink['state'], 1> = { up: 1, started: 1, off: 1, blocked: 1, failed: 1 }
/** What `wb chat url --json` says, read as a Link (OpenCode's panel's, pane.ts `linkOf`), and the
 * link it is when that can't be read: the plugin spells no phase, and `failed` is one (#1312). */
export const LINK_STATES: ReadonlySet<string> = new Set(Object.keys(STATES))
export const linkDown = (why: string): Link => ({ state: 'failed', why })

// types/index.d.ts restates wire.d.ts's WatchLine and ChatLink (Claude Code's contract imports
// nothing): held equal here, so a change to one fails the plugin's type check (#1323)
type Same<A, B> = [A] extends [B] ? ([B] extends [A] ? true : false) : false
type Holds<T extends true> = T
export type WireHeld = [Holds<Same<WbLine, WatchLine>>, Holds<Same<Link, Omit<ChatLink, 'state'> & { state: ChatLink['state'] | 'checking' }>>]

const width = (s: string) => [...s].length
// cut to n columns, an ellipsis last when cut (every character the modeline draws is one column)
const cut = (s: string, n: number) => (width(s) <= n ? s : n <= 0 ? '' : [...s].slice(0, n - 1).join('') + '…')

/**
 * What the modeline draws in a pane `cols` wide: `bar`, its bar row's text (in from each edge by
 * the bar's padding), and `rows`, the link's own rows under it when not even `[•_•] <url>` fits
 * there: the link cut by characters, `cols` a row and never at a word, so it stays one link.
 * `name`: the pane's name (OpenCode's panel says `Workbench` alone, #1312).
 */
export function modeline(link: Link, cols: number, name: string = MODELINE): { bar: string; rows: string[] } {
  const room = Math.max(1, cols - 2) // paddingX 1
  const head = `${FACE.rest} ${name}`
  const url = link.url ?? ''
  if ((link.state === 'up' || link.state === 'started') && url) {
    const whole = `${head} - ${url}`
    if (width(whole) <= room) return { bar: whole, rows: [] }
    const short = `${FACE.rest} ${url}`
    if (width(short) <= room) return { bar: short, rows: [] }
    const chars = [...url], rows: string[] = []
    const n = Math.max(1, cols)
    for (let i = 0; i < chars.length; i += n) rows.push(chars.slice(i, i + n).join(''))
    return { bar: cut(head, room), rows }
  }
  const said = link.state === 'checking' ? 'starting the web app…'
    : link.state === 'off' ? 'web app off (wb chat on)'
    : link.state === 'blocked' || link.state === 'failed' ? `web app down: ${link.why || link.state}`
    : '' // up with no link wb would give: the name alone
  return { bar: cut(said ? `${head} - ${said}` : head, room), rows: [] }
}
