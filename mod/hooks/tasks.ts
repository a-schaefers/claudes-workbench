// The Tasks view's rows (#1048): every open task at a glance, several at once, as Claude Code's own
// view of a multi-agent workflow draws each agent (the owner, web chat 2026-11-23: "progress bar
// indicators or something, some type of indications showing me everything that's going on while it
// happens"). One task is at most two lines: who and where (glyph, name, stage, card or provider,
// model, role, spend, elapsed), then what it did last and its meters. Tasks of one request (task.json
// `group`, `wb start --group`) sit under one header. Columns line up within a block and are dropped
// for the whole block, in a fixed order, until the widest row fits, so no row ever wraps: the pane
// counts rows exactly. The layout is the design study's reference renderer (mock.py) ported rule for
// rule; the tests check every mockup of the study character for character.
//
// Progress is only what is known: a bar only for a fraction whose denominator is real (the context
// window, the group's tasks done, the API day cap); counts with a known budget as counts (`fix 1/2`);
// time against this machine's typical length as words with a tilde, never a bar, and only with
// TYPICAL_MIN timed tasks behind it. No percentage of a task: nobody knows how much of one is done.
import type { WbTask } from '../types'

/** One run of a row's text in one style. */
export type Part = { text: string; color?: string; dim?: boolean; bold?: boolean; underline?: boolean }

/** What a task's rows read: WbTask's fields plus the ones the view adds (scan fills them, viewFacts). */
export type TaskFacts = Pick<WbTask, 'name' | 'phase' | 'round' | 'model' | 'label' | 'via' | 'role' |
  'provider' | 'tests' | 'last' | 'createdAt' | 'updatedAt' | 'recent' | 'parked'> & {
  /** task.json `group` ('' for none); `cards` status' card numbers ([] on one card, or an API task) */
  group: string; cards: number[]; tools: number; tokOut: number
  /** status `live_used.used` (0 unless its session is the status' `session`), the window, `tuned.fresh_at` */
  ctxUsed: number; window: number; freshAt: number | null
  autoFixes: number; autoFixMax: number; roundFor: string; reviews: number; steps: number
  /** the step's active seconds before this round (status `active_s` less `step_active_s`, as
   * runner.active_so_far), the round's start (ms, as createdAt; 0 for none), and status `typical` */
  activeS: number; roundStartedAt: number; typicalMin: number; typicalN: number
  /** status `late_min`: the progress checks' first point (the user's --after, else the typical
   * length at least progress.FLOOR_MIN), 0 for none: a task is late only from there */
  lateMin: number
  spend: number | 'plan' | 'none' | null   // null: no api record (a GPU task, or an older wb)
}

const WORKING = new Set(['running', 'testing', 'asking']) // where the context and the estimate mean something
const CLOSED = new Set(['accepted', 'discarded'])
/** Each phase's colour; the glyph and the word say the same without it. */
export const COLOR: Record<string, string> = {
  queued: 'subtle', running: 'claude', testing: 'suggestion', asking: 'warning',
  review: 'success', stuck: 'error', failed: 'error', accepted: 'inactive', discarded: 'inactive',
}
/** Each phase's glyph; a running task's is the spinner frame. */
export const MARK: Record<string, string> = {
  queued: '◌', testing: '◐', asking: '?', review: '◆', stuck: '▲', failed: '✗',
  accepted: '✓', discarded: '–',
}
// the roles as Settings names them (#1023's ROLE_LABELS: keep the two the same); the Stats tab's
// By size and Settings' size rows say a size by these too, never `significant` beside "Complex coding"
export const ROLE_WORDS: Record<string, string> = { significant: 'complex', trivial: 'trivial', writing: 'writing' }
// progress.TYPICAL_MIN: an estimate needs this many timed tasks of the model and size behind it
const TYPICAL_MIN = 3
// meter.CHECKIN_SHARE (#1019): past this share of the day cap its line turns warning
const CHECKIN_SHARE = 0.5

export const ago = (ms: number) => {
  const s = Math.max(0, Math.round(ms / 1000))
  return s < 90 ? `${s}s` : s < 5400 ? `${Math.round(s / 60)}m` : `${Math.round(s / 3600)}h`
}

export const clip = (text: string, n: number) => (text.length <= n ? text : text.slice(0, Math.max(0, n - 1)) + '…')

const BLOCKS = '▏▎▍▌▋▊▉█'
/** ``frac`` (0..1) of ``width`` cells, in eighths, the rest light shade */
export function bar(frac: number, width: number): string {
  const eighths = Math.round(Math.max(0, Math.min(1, frac)) * width * 8)
  const full = Math.floor(eighths / 8), part = eighths % 8
  const drawn = '█'.repeat(full) + (part ? BLOCKS[part - 1] : '')
  return drawn + '░'.repeat(Math.max(0, width - drawn.length))
}

// the context bar, with the handover point (`fresh_at`) marked while the fill is short of it
const ctxBar = (frac: number, freshAt: number | null, w: number) => {
  const b = [...bar(frac, w)]
  if (freshAt) {
    const i = Math.min(w - 1, Math.round(freshAt * w))
    if (b[i] === '░') b[i] = '┊'
  }
  return b.join('')
}

// the pane passes wave 2's usd() (savings.usd's rule); this, a free model's $0, else cents, is the default
const plainUsd = (x: number) => (x === 0 ? '$0' : `$${x.toFixed(2)}`)

const tokens = (n: number) =>
  n < 100 ? `${n} out` : n < 100_000 ? `${(n / 1000).toFixed(1)}K out` : n < 1e6 ? `${Math.round(n / 1000)}K out`
    : `${(n / 1e6).toFixed(1)}M out`

// the queued stage from what the runner says it waits on (the runner's and models' `say` texts, the
// prefixes transcript.activity matches): long and short words
const QUEUED: [RegExp, string, string][] = [
  [/^waiting for the GPU/, 'waits for the GPU', 'waits GPU'],
  [/^waiting for an API slot/, 'waits for a slot', 'waits slot'],
  [/^waiting/, 'waiting', 'waiting'],
  [/^checking out/, 'checking out', 'checkout'],
  [/^setting up/, 'setting up', 'setup'],
  [/^checking the acceptance command/, 'base check', 'base'],
  [/^(loading|reloading|starting|unloading|stopping|sharing)/, 'loading model', 'loading'],
]

/** The stage in words, long or short (a narrow pane), and its colour. */
export function stageOf(t: TaskFacts, short: boolean): { text: string; color: string } {
  const color = COLOR[t.phase] ?? 'claude'
  const r = short ? `r${t.round}` : `round ${t.round}`
  const fix = `fix ${t.autoFixes}/${t.autoFixMax}`
  const say = (long: string, brief: string) => ({ text: short ? brief : long, color })
  switch (t.phase) {
    case 'queued': {
      const hit = QUEUED.find(([re]) => re.test(t.last))
      if (hit) return say(hit[1], hit[2])
      return say(t.round > 1 ? `queued · round ${t.round}` : 'queued', 'queued')
    }
    case 'running':
      switch (t.roundFor) {
        case 'fix': return { text: `${r} · ${fix}`, color }
        case 'self_review': return say(`${r} · self-review`, `${r} · self-rev`)
        case 'feedback': return say(`${r} · feedback ${t.reviews}`, `${r} · fb ${t.reviews}`)
        case 'steer': return { text: `${r} · steered`, color }
        case 'fresh': return say(`${r} · fresh session`, `${r} · fresh`)
        case 'next': return say(`step ${t.steps} · ${r}`, `s${t.steps} · ${r}`)
        case 'resume': return { text: `${r} · resumed`, color }
        default: return { text: r, color } // 'start', or an older wb with no round_for
      }
    case 'testing': return say(t.roundFor === 'fix' && t.autoFixes > 0 ? `testing · ${fix}` : 'testing', 'testing')
    case 'asking': return say('asks Claude', 'asks')
    case 'review':
      if (t.parked) return say('review · parked', 'parked')
      if (t.tests === 'pass') return say('review · tests ✓', 'review ✓')
      if (t.tests === 'fail') return say('review · tests ✗', 'review ✗')
      return say('review · no tests', 'review')
    default: return { text: t.phase, color } // stuck, failed, accepted, discarded
  }
}

/** Where it runs: `GPU`, `GPU 1`, `GPU 0+1`; `API DeepSeek`, short `API`. */
export function placeOf(t: TaskFacts, short: boolean): string {
  if (t.via === 'api') return short || !t.provider ? 'API' : `API ${t.provider}`
  return t.cards?.length ? `GPU ${t.cards.join('+')}` : 'GPU'
}

// the step's active seconds so far: before this round, plus the round under way
const activeOf = (t: TaskFacts, now: number) =>
  (t.activeS || 0) + (WORKING.has(t.phase) && t.roundStartedAt ? Math.max(0, now - t.roundStartedAt) / 1000 : 0)
const typicalOf = (t: TaskFacts) => (t.typicalN >= TYPICAL_MIN && t.typicalMin > 0 ? t.typicalMin * 60 : 0)
// late from the progress checks' own points, 1x and 2x status `late_min`, never from a raw median:
// a typical length of 4 minutes coloured a 5-minute task late, where no check comes before
// progress.FLOOR_MIN (step A's review, #1048); '' before the first, or while it isn't working.
// And only once the estimate says `over`: the user's --after of 5 under a typical 20 drew the
// elapsed time late beside `~12m left`, the colour saying what no word did (the #1048 review, seen
// live); with no estimate shown there is no word to repeat, so no colour either.
const lateOf = (t: TaskFacts, now: number): '' | 'warning' | 'error' => {
  const typ = typicalOf(t)
  if (!(t.lateMin > 0) || !typ || !WORKING.has(t.phase)) return ''
  const active = activeOf(t, now), at = t.lateMin * 60
  if (active < typ) return ''
  return active >= 2 * at ? 'error' : active >= at ? 'warning' : ''
}

/** Time against this machine's typical length: `~6m left`, `3m over` (warning from the progress
 * checks' first point, error from their second); null without TYPICAL_MIN timed tasks behind it,
 * or while it isn't working. */
export function etaOf(t: TaskFacts, now: number): { text: string; color?: string } | null {
  const typ = typicalOf(t)
  if (!typ || !WORKING.has(t.phase)) return null
  const active = activeOf(t, now), left = typ - active
  if (left > 0) return { text: `~${ago(left * 1000)} left` }
  const late = lateOf(t, now)
  return { text: `${ago(-left * 1000)} over`, ...(late ? { color: late } : {}) }
}

/** The context window's fill and the handover point, while it is working; null when unknown. */
export function ctxOf(t: TaskFacts): { frac: number; freshAt: number | null } | null {
  if (!WORKING.has(t.phase) || !(t.window > 0) || !(t.ctxUsed > 0)) return null
  return { frac: t.ctxUsed / t.window, freshAt: t.freshAt }
}

/** The tasks in blocks: each request's group (two or more tasks of one task.json `group`), and the
 * ungrouped ones as one block (group ''); blocks by their first task's start, tasks by their own. */
export function blocksOf(list: TaskFacts[]): { group: string; tasks: TaskFacts[] }[] {
  const sorted = [...list].sort((a, b) => a.createdAt - b.createdAt)
  const n = new Map<string, number>()
  for (const t of sorted) if (t.group) n.set(t.group, (n.get(t.group) ?? 0) + 1)
  const blocks = new Map<string, TaskFacts[]>()
  for (const t of sorted) {
    const g = t.group && (n.get(t.group) ?? 0) >= 2 ? t.group : ''
    if (!blocks.has(g)) blocks.set(g, [])
    blocks.get(g)!.push(t)
  }
  return [...blocks].map(([group, tasks]) => ({ group, tasks }))
}

/** The one-line-a-task form: a short pane (the 14-row box), or more tasks than leave the
 * transcript, the apprentice, the tabs and the modeline their rows. */
export function compactWanted(tasks: number, headers: number, bodyRows: number): boolean {
  return bodyRows < 20 || 2 * tasks + headers > bodyRows - 12
}

// ---------------------------------------------------------------- the layout (mock.py, rule for rule)

type Style = Omit<Part, 'text'>
type Meter = Part[]
/** One task as the layout reads it: every text already worded. */
type Row = {
  t: TaskFacts; mark: Part; name: string; underline: boolean
  stage: string; stageS: string; stageColor: string; fixAt: boolean
  where: string; whereS: string; placeStyle: Style; model: string; role: string; spend: string
  elapsed: string; elapsedStyle: Style
  eta: { text: string; color?: string } | null; doing: string | null; doingStyle: Style
  ctx: { frac: number; freshAt: number | null } | null; tools: number; tok: string
}

// line 1's columns, left to right, and what goes first when there is no room: the role, the
// model, the long stage (`round 2` becomes `r2`), the long place (`API DeepSeek` becomes `API`),
// the place. The glyph, the name, the stage, the spend and the elapsed time always stay.
const COLS1 = ['where', 'model', 'role', 'spend', 'elapsed'] as const
type Col = typeof COLS1[number]
const DROP1 = ['role', 'model', 'long-stage', 'long-where', 'where']
// line 2's meters: the tokens go first, then the bar narrows from 8 cells to 5, the tool count,
// the bar (`ctx 56%` stays), the estimate. What it is doing keeps MIN_DOING columns.
const DROP2 = ['tok', 'bar8', 'tools', 'bar5', 'eta']
const MIN_DOING = 22
const MIN_STAGE = 4 // past every drop: the name narrows (to MIN_NAME) before the stage goes under this
const MIN_NAME = 6

const colOf = (r: Row, k: Col): string =>
  k === 'where' ? r.where : k === 'model' ? r.model : k === 'role' ? r.role : k === 'spend' ? r.spend : r.elapsed
const widest = (xs: string[]) => xs.reduce((m, x) => Math.max(m, x.length), 0)
const textOf = (parts: Part[]) => parts.map(p => p.text).join('')

// what it is doing now, for line 2, on one line; null for none (a passing review, a parked or
// closed task). An ask_reviewer question, a failure's error and a test command can span lines (the
// #1048 review): a newline would break the row in two and the pane's exact row count with it.
const doingOf = (t: TaskFacts): string | null => {
  const said = doingText(t)
  return said === null ? null : said.replace(/\s+/g, ' ').trim()
}
const doingText = (t: TaskFacts): string | null => {
  if (CLOSED.has(t.phase) || t.parked) return null
  if (t.phase === 'stuck' || t.phase === 'failed') return t.last ? `${t.last} · Claude steers it next` : 'Claude steers it next'
  if (t.phase === 'review') return t.tests === 'pass' || /Claude reviews next$/.test(t.last) || !t.last ? null : t.last
  if (t.phase === 'asking') {
    const asked = [...(t.recent ?? [])].reverse().find(l => l.startsWith('asks: '))
    return asked ? asked.slice(6) : t.last
  }
  return t.last || (ctxOf(t) || t.tools ? '' : null)
}
const DOING_STYLE: Record<string, Style> = {
  testing: { color: 'suggestion' }, asking: { color: 'warning' }, stuck: { color: 'error' }, failed: { color: 'error' },
}

function rowOf(t: TaskFacts, now: number, spin: string, followed: string, grouped: boolean, usd: (x: number) => string): Row {
  const long = stageOf(t, false), short = stageOf(t, true)
  let elapsed: string
  if (t.phase === 'review') {
    // a request's finished task says how long it ran, beside its siblings' times; on its own, how
    // long it has waited for Claude
    const waited = now - t.updatedAt
    elapsed = grouped ? ago(t.updatedAt - t.createdAt) : waited < 60_000 ? 'now' : `${ago(waited)} ago`
  } else elapsed = ago(now - t.createdAt)
  const late = lateOf(t, now)
  return {
    t, mark: { text: t.phase === 'running' ? spin : MARK[t.phase] ?? '·', color: COLOR[t.phase] },
    name: t.name, underline: t.name === followed,
    stage: long.text, stageS: short.text, stageColor: long.color, fixAt: /fix \d+\/\d+/.test(long.text),
    where: placeOf(t, false), whereS: placeOf(t, true),
    placeStyle: t.via === 'api' ? { color: 'suggestion' } : { dim: true },
    // an API task's the id's last part (its place names the provider, #1016's badge), a GPU task's label
    model: (t.via === 'api' ? '' : t.label) || (t.model ?? '').split('/').pop() || '',
    role: ROLE_WORDS[t.role] ?? t.role ?? '',
    spend: typeof t.spend === 'number' ? usd(t.spend) : t.spend === 'plan' ? 'plan' : t.spend === 'none' ? 'no price' : '',
    elapsed, elapsedStyle: late ? { color: late } : { dim: true },
    eta: etaOf(t, now), doing: doingOf(t), doingStyle: DOING_STYLE[t.phase] ?? { dim: true },
    ctx: ctxOf(t), tools: t.tools || 0, tok: t.tokOut > 0 ? tokens(t.tokOut) : '',
  }
}

const meters = (r: Row, drop: string[]): Meter[] => {
  const dim = (text: string): Meter => [{ text, dim: true }]
  const eta = (): Meter => [{ text: r.eta!.text, ...(r.eta!.color ? { color: r.eta!.color } : { dim: true }) }]
  if (!r.ctx) {
    const out: Meter[] = []
    if (!drop.includes('eta') && r.eta) out.push(eta())
    if (r.tools && !drop.includes('tools')) out.push(dim(`${r.tools} tools`))
    return out
  }
  const { frac, freshAt } = r.ctx
  const pct = `${Math.round(frac * 100)}%`
  const color = frac >= 0.9 ? 'error' : freshAt != null && frac >= freshAt ? 'warning' : 'success'
  const barW = drop.includes('bar5') ? 0 : drop.includes('bar8') ? 5 : 8
  const out: Meter[] = [barW
    ? [{ text: 'ctx ', dim: true }, { text: `${ctxBar(frac, freshAt, barW)} ${pct}`, color }]
    : [{ text: 'ctx ', dim: true }, { text: pct, color }]]
  if (!drop.includes('eta') && r.eta) out.push(eta())
  if (!drop.includes('tools') && r.tools) out.push(dim(`${r.tools} tools`))
  if (!drop.includes('tok') && r.tok) out.push(dim(r.tok))
  return out
}
const meterText = (ms: Meter[]) => ms.map(textOf).join(' · ')

type Layout = { w: Record<string, number>; keep: string[]; ls: boolean; lw: boolean; drop: string[]; mw: number }

function layout(rows: Row[], cols: number, grouped: boolean): Layout {
  const c1 = grouped ? 2 : 0
  const w: Record<string, number> = { name: Math.min(cols >= 70 ? 22 : 16, widest(rows.map(r => r.name))) }
  for (const k of COLS1) w[k] = widest(rows.map(r => colOf(r, k)))
  const keep: string[] = COLS1.filter(k => w[k])
  const need = (keep: string[]): [number, Record<string, number>] => {
    const ww = { ...w }
    const ls = keep.includes('long-stage')
    const stage = Math.min(24, widest(rows.map(r => (ls ? r.stage : r.stageS || r.stage))))
    if (!keep.includes('long-where')) ww.where = widest(rows.map(r => r.whereS || r.where))
    const ks = keep.filter(k => (COLS1 as readonly string[]).includes(k))
    const right = ks.reduce((s, k) => s + ww[k]!, 0) + 2 * Math.max(0, ks.length - 1)
    return [c1 + 2 + w.name! + 1 + stage + 2 + right, ww]
  }
  keep.push('long-stage', 'long-where')
  for (const k of DROP1) {
    if (need(keep)[0] <= cols) break
    if (keep.includes(k)) keep.splice(keep.indexOf(k), 1)
  }
  Object.assign(w, need(keep)[1])
  const drop: string[] = []
  const need2 = (drop: string[]): [number, number] => {
    const mw = widest(rows.filter(r => r.doing !== null).map(r => meterText(meters(r, drop))))
    return [c1 + 4 + MIN_DOING + 2 + mw, mw]
  }
  let mw = 0
  for (const k of [null, ...DROP2]) {
    if (k) drop.push(k)
    const [n, m] = need2(drop)
    mw = m
    if (n <= cols) break
  }
  // past every drop (a pane near its 30-column floor): the name gives way before the stage does
  const right = COLS1.filter(k => keep.includes(k) && w[k])
  const rlen = right.reduce((s, k) => s + w[k]!, 0) + 2 * Math.max(0, right.length - 1)
  const room = cols - (c1 + 3 + w.name!) - rlen - 2
  if (room < MIN_STAGE) w.name = Math.max(Math.min(MIN_NAME, w.name!), w.name! - (MIN_STAGE - room))
  return { w, keep, ls: keep.includes('long-stage'), lw: keep.includes('long-where'), drop, mw }
}

// trailing blanks off, as Python's rstrip
const trimEnd = (parts: Part[]): Part[] => {
  const out = parts.filter(p => p.text)
  while (out.length) {
    const last = out[out.length - 1]!
    const t = last.text.trimEnd()
    if (t) { out[out.length - 1] = { ...last, text: t }; break }
    out.pop()
  }
  return out
}

// cut to n columns with an ellipsis, in the style of the run it falls in
const clipParts = (parts: Part[], n: number): Part[] => {
  if (textOf(parts).length <= n) return parts
  const out: Part[] = []
  let left = Math.max(0, n - 1)
  for (const p of parts) {
    if (left <= 0 && out.length) break
    const text = p.text.slice(0, left)
    out.push({ ...p, text })
    left -= text.length
  }
  out[out.length - 1] = { ...out[out.length - 1]!, text: out[out.length - 1]!.text + '…' }
  return out.filter(p => p.text)
}

function line1(r: Row, cols: number, conn: string, L: Layout): Part[] {
  const { w } = L
  const name = clip(r.name, w.name!)
  const parts: Part[] = []
  if (conn) parts.push({ text: conn, dim: true })
  parts.push(r.mark, { text: ' ' })
  parts.push({ text: name, bold: true, ...(r.underline ? { underline: true } : {}) }, { text: ' '.repeat(w.name! - name.length) + ' ' })
  const right: Part[][] = []
  for (const k of COLS1) {
    if (!L.keep.includes(k) || !w[k]) continue
    let v = colOf(r, k)
    if (k === 'where' && !L.lw) v = r.whereS || v
    const text = k === 'spend' || k === 'elapsed' ? v.padStart(w[k]!) : v.padEnd(w[k]!)
    const style: Style = k === 'where' ? r.placeStyle : k === 'elapsed' ? r.elapsedStyle : k === 'spend' ? {} : { dim: true }
    right.push([{ text, ...style }])
  }
  const rlen = right.reduce((s, p) => s + p[0]!.text.length, 0) + 2 * Math.max(0, right.length - 1)
  const room = cols - textOf(parts).length - rlen - 2
  const stage = clip(L.ls ? r.stage : r.stageS || r.stage, room)
  const at = r.fixAt ? stage.search(/fix \d/) : -1
  if (at >= 0) parts.push({ text: stage.slice(0, at), color: r.stageColor }, { text: stage.slice(at), color: 'warning' })
  else parts.push({ text: stage, color: r.stageColor })
  parts.push({ text: ' '.repeat(Math.max(0, room - stage.length)) + '  ' })
  right.forEach((p, i) => { if (i) parts.push({ text: '  ' }); parts.push(...p) })
  return clipParts(trimEnd(parts), cols)
}

function line2(r: Row, cols: number, conn2: string, L: Layout, mw: number): Part[] {
  const ms = meters(r, L.drop)
  const m = meterText(ms)
  const pad = m ? Math.max(0, mw - m.length) : 0
  const lead: Part[] = [...(conn2 ? [{ text: conn2, dim: true }] : []), { text: '  ' }, { text: '⎿', dim: true }, { text: ' ' }]
  const room = cols - textOf(lead).length - (m ? pad + m.length + 2 : 0)
  const doing = clip(r.doing ?? '', room)
  const parts: Part[] = [...lead, { text: doing, ...r.doingStyle }, { text: ' '.repeat(Math.max(0, room - doing.length)) }]
  if (m) {
    parts.push({ text: '  ' + ' '.repeat(pad) })
    ms.forEach((x, i) => { if (i) parts.push({ text: ' · ', dim: true }); parts.push(...x) })
  }
  return clipParts(trimEnd(parts), cols)
}

const COUNTED = ['running', 'testing', 'asking', 'queued', 'review', 'stuck', 'failed']
function header(group: string, tasks: TaskFacts[], cols: number, compact: boolean, usd: (x: number) => string): Part[] {
  const n = tasks.length
  const done = tasks.filter(t => t.phase === 'review' || CLOSED.has(t.phase)).length
  const counts = COUNTED.flatMap(p => {
    const k = tasks.filter(t => t.phase === p).length
    return k ? [{ text: `${k} ${p}`, ...(p === 'stuck' || p === 'failed' ? { color: 'error' } : { dim: true }) }] : []
  })
  const paid = tasks.filter(t => typeof t.spend === 'number')
  const spend = paid.length ? usd(paid.reduce((s, t) => s + (t.spend as number), 0)) : ''
  const left: Part[] = [{ text: '▾ ', dim: true }, { text: group, bold: true }]
  if (!compact) left.push({ text: ` · ${n} tasks`, dim: true })
  for (const c of counts) left.push({ text: ' · ', dim: true }, c)
  const tally: Part = { text: `${done}/${n} done`, dim: true }
  const fill: Part[] = [{ text: bar(done / n, 8), color: 'success' }, { text: ' ' }, tally]
  const lead = spend ? [{ text: spend }, { text: '  ' }] : []
  let right: Part[] = compact ? [tally] : [...lead, ...fill]
  const ll = textOf(left).length
  if (!compact && ll + 2 + textOf(right).length > cols) right = [...lead, tally]
  if (!compact && ll + 2 + textOf(right).length > cols) right = [tally]
  // then `· 3 tasks` (the tally says it): at 53 columns a stuck group's header cut `· 1 stuck` off
  // while the count of tasks stayed (the #1048 review, seen live)
  if (!compact && ll + 2 + textOf(right).length > cols) left.splice(2, 1)
  const rl = textOf(right).length
  const cut = clipParts(left, cols - rl - 2)
  return clipParts([...cut, { text: ' '.repeat(Math.max(0, cols - textOf(cut).length - rl)) }, ...right], cols)
}

// the block's closed tasks, folded to one dim line: `✓ save-slots accepted 12m ago · – old-try …`
function closedLine(tasks: TaskFacts[], cols: number, now: number): Part[] {
  const text = tasks.map(t => `${MARK[t.phase]} ${t.name} ${t.phase} ${ago(now - t.updatedAt)} ago`).join(' · ')
  return clipParts([{ text, color: 'inactive' }], cols)
}

/** Every row of the task list, at most `cols` columns each: a block a request (its header, then
 * each task's line 1 and, unless compact, line 2), the block's closed tasks folded to one dim line,
 * an empty row between blocks. `usd`: how spend is written (the pane passes wave 2's usd()). */
export function taskRows(list: TaskFacts[], o: { cols: number; now: number; spin: string;
  followed: string; compact: boolean; usd?: (x: number) => string }): Part[][] {
  return taskLines(list, o).map(l => l.parts)
}

/** taskRows' rows, each with the task it draws ('' for a header, a gap or the closed line) and
 * whether it is that task's first line: the pane presses a task's first line to follow it, and
 * draws its other lines after its last one. */
export function taskLines(list: TaskFacts[], o: { cols: number; now: number; spin: string;
  followed: string; compact: boolean; usd?: (x: number) => string }): { parts: Part[]; task: string; first: boolean }[] {
  const usd = o.usd ?? plainUsd
  const out: { parts: Part[]; task: string; first: boolean }[] = []
  const add = (parts: Part[], task = '', first = false) => out.push({ parts, task, first })
  for (const b of blocksOf(list)) {
    const grouped = !!b.group
    const open = b.tasks.filter(t => !CLOSED.has(t.phase))
    const closed = b.tasks.filter(t => CLOSED.has(t.phase))
    if (o.compact && !grouped && !open.length) continue
    if (!o.compact && out.length) add([{ text: '' }])
    if (grouped) add(header(b.group, b.tasks, o.cols, o.compact, usd))
    if (open.length) {
      const rows = open.map(t => rowOf(t, o.now, o.spin, o.followed, grouped, usd))
      const L = layout(rows, o.cols, grouped)
      rows.forEach((r, i) => {
        const last = i === rows.length - 1
        add(line1(r, o.cols, grouped ? (last ? '└ ' : '├ ') : '', L), r.name, true)
        if (!o.compact && r.doing !== null)
          add(line2(r, o.cols, grouped ? (last ? '  ' : '│ ') : '', L, r.ctx || r.tools ? L.mw : 0), r.name)
      })
    }
    if (!o.compact && closed.length) add(closedLine(closed, o.cols, o.now))
  }
  return out
}

/** Whether an API task is working (running, testing, asking): the day cap's line shows only then.
 * A queued one waiting for its provider's slot drew it too (the #1048 review, seen live): the owner
 * chose "only while an API task runs". */
export const apiWorking = (list: Pick<TaskFacts, 'via' | 'phase'>[]) => list.some(t => t.via === 'api' && WORKING.has(t.phase))

/** The API day cap's line, only while an API task is working (the owner, 2026-11-23: on Tasks only
 * then; Settings has it always): `API today $0.42 of the $5 day cap  █▍░░░░░░░░`, warning from
 * CHECKIN_SHARE of the cap, error at it; without a cap, today's spend alone. */
export function capRow(list: TaskFacts[], spent: number, cap: number | null, cols: number,
  usd: (x: number) => string = plainUsd): Part[] | null {
  if (!apiWorking(list)) return null
  if (!(cap && cap > 0)) return clipParts([{ text: `API today ${usd(spent)}`, dim: true }], cols)
  const share = spent / cap
  const color = share >= 1 ? 'error' : share >= CHECKIN_SHARE ? 'warning' : 'success'
  const capText = Number.isInteger(cap) ? `$${cap}` : usd(cap)
  return clipParts([{ text: `API today ${usd(spent)} of the ${capText} day cap  `, dim: true },
    { text: bar(share, 10), color }], cols)
}
