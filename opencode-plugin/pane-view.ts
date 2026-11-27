// What Workbench's panel, sidebar section and footer item show (#1312, #1332), apart from drawing
// it: pane.tsx draws these with OpenTUI, and the tests read them here under bare Bun (nothing of
// OpenTUI or Solid is imported). The decisions are the shared core's and the kernel's: the
// Tasks view's rows (tasks.ts `taskLines`, Claude Code's own), the task followed (`focusOf`), the
// apprentice's words (`speechOf`), the modeline (modeline.ts), whose tasks are this session's
// (core.ts `owns`) and the phase counts (`openCounts`). Only the colours (OpenCode's theme
// tokens in place of Claude Code's names) and the panel's own geometry are this host's.
import { CLOSED, openCounts, owns } from '../mod/hooks/core.ts'
import { modeline, type Link } from '../mod/hooks/modeline.ts'
import { ROWS } from '../mod/hooks/apprentice.ts'
import { clip, compactWanted, taskLines, type Part } from '../mod/hooks/tasks.ts'
import { AGENT, LINK_MS, type PaneTask } from './pane.ts'

/** The panel's name: `ui.panel.open(PANEL)` (splash.tsx's PANEL, the same word). */
export const NAME = 'Workbench'
/** Who reviews and answers in OpenCode: the Workbench agent (speechOf's `who`). */
export const WHO = 'Workbench'
export const MARK = '⚒'

/** A read on a timer, one loop however often it's asked to read at once (`now`: each press that
 * opens the panel): the pending timer goes, and an older read still in flight neither draws nor
 * schedules. The review of #1332: every press started another loop that ran for the life of the
 * TUI, each one a scan of every task every 2 s. A failed read keeps the last one drawn. */
type Timers = { set(f: () => void, ms: number): unknown; clear(h: unknown): void }
const TIMERS: Timers = { set: (f, ms) => setTimeout(f, ms), clear: h => clearTimeout(h as ReturnType<typeof setTimeout>) }

export class Poll<T> {
  private timer: unknown
  private gen = 0
  private stopped = false
  constructor(private readonly read: () => Promise<T>, private readonly draw: (t: T) => void,
    private readonly delay: () => number, private readonly timers: Timers = TIMERS) {}
  async now(): Promise<void> {
    if (this.stopped) return
    if (this.timer !== undefined) this.timers.clear(this.timer)
    this.timer = undefined
    const gen = ++this.gen
    let got: { t: T } | undefined
    try { got = { t: await this.read() } } catch { /* the server part is restarting */ }
    if (this.stopped || gen !== this.gen) return
    if (got) this.draw(got.t)
    this.timer = this.timers.set(() => void this.now(), this.delay())
  }
  stop() { this.stopped = true; if (this.timer !== undefined) this.timers.clear(this.timer) }
}

/** The web app's link for the modeline: asked when the panel draws (`want`: the page's opening,
 * each of the splash's layouts), at most once a LINK_MS (the server part keeps its answer as
 * long, pane.ts `chatLink`), and asked again LINK_MS after each answer while the panel shows
 * (`shown`), as the mod rechecks its chat: a panel opened while the web app was down said
 * `web app down` until it was closed and opened again (#1518, 2026-11-26). */
export class LinkPoll {
  private at = -Infinity
  private timer: unknown
  private stopped = false
  constructor(private readonly read: () => Promise<Link>, private readonly draw: (l: Link) => void,
    private readonly shown: () => boolean, private readonly now: () => number = Date.now,
    private readonly timers: Timers = TIMERS) {}
  async want(): Promise<void> {
    if (this.stopped || this.now() - this.at < LINK_MS) return
    this.at = this.now()
    if (this.timer !== undefined) this.timers.clear(this.timer)
    this.timer = undefined
    try { this.draw(await this.read()) } catch { /* the server part is restarting: the next ask */ }
    if (this.stopped || this.timer !== undefined || !this.shown()) return
    this.timer = this.timers.set(() => { this.timer = undefined; if (this.shown()) void this.want() }, LINK_MS)
  }
  stop() { this.stopped = true; if (this.timer !== undefined) this.timers.clear(this.timer) }
}

/** OpenCode's own command that gives the keyboard back to the prompt beside a panel
 * (session-frame.tsx; `<leader>left` by default), the one its diff panel shows. */
export const FOCUS_PROMPT = 'pane.focus.left'

/** Where the keyboard goes when the panel opens: OpenCode's panel host takes it, as for its own
 * /diff, which suits an open the user asked for (the palette, the footer, the sidebar), but after
 * /workbench or the agent picker what was typed next went nowhere until Esc (#1509, 2026-11-26).
 * `want(true)` at such an open; `due(focused)` once the panel is drawn: true the once it has
 * taken the keyboard, to hand it back (FOCUS_PROMPT). */
export class PromptFocus {
  private wanted = false
  want(on: boolean) { this.wanted = on }
  due(focused: boolean): boolean {
    if (!this.wanted || !focused) return false
    this.wanted = false
    return true
  }
}

/** Whether the panel opens as the TUI comes to a session (#1517): once for each arrival, when
 * the session's agent is Workbench, as on `opencode -s` of a Workbench session or a new TUI on
 * one, where OpenCode publishes no agent selection (the agent is already it) and the panel, the
 * TUI's own state, starts closed. `see(sessionID, agent)` at each change of the route's session
 * or of what the TUI knows of its agent (undefined: not known yet); `settle()` once the panel's
 * state for this arrival is decided otherwise (the agent selected on screen). */
export class Landing {
  private at = ''
  private done = true
  see(sessionID: string, agent: string | undefined): boolean {
    if (sessionID !== this.at) { this.at = sessionID; this.done = !sessionID }
    if (this.done || agent === undefined) return false
    this.done = true
    return agent === AGENT
  }
  settle() { this.done = true }
}

// ---------------------------------------------------------------- colours

/** What a style resolves to: a theme token's path, or none (the text's own colour). */
export type Token = 'accent' | 'info' | 'success' | 'warning' | 'error' | 'muted' | 'faint' | 'base' | 'interactive'
  | 'added' | 'removed'
/** One run, as pane.tsx draws it: text, colours as tokens, attributes. */
export interface Run {
  readonly text: string
  readonly fg?: Token
  readonly bg?: Token
  readonly bold?: boolean
  readonly dim?: boolean
  readonly italic?: boolean
  readonly underline?: boolean
}

/** Claude Code's colour names (tasks.ts and register.tsx's LINE_STYLE) as OpenCode's theme tokens:
 * Claude's coral is the theme's accent, a suggestion its info, a permission its interactive hue;
 * subtle muted, inactive faint. */
export const TOKEN: Readonly<Record<string, Token>> = {
  claude: 'accent', suggestion: 'info', success: 'success', warning: 'warning', error: 'error',
  subtle: 'muted', inactive: 'faint', permission: 'interactive', text: 'base',
}

export const runOf = (p: Part): Run => ({
  text: p.text,
  ...(p.color && TOKEN[p.color] ? { fg: TOKEN[p.color] } : p.dim ? { fg: 'muted' as const } : {}),
  ...(p.bold ? { bold: true } : {}),
  ...(p.underline ? { underline: true } : {}),
})

/** wb watch's line styles (`s`), as register.tsx's LINE_STYLE draws them in Claude Code. */
export const LINE: Readonly<Record<string, Omit<Run, 'text'>>> = {
  claude: { fg: 'accent', bold: true }, agent: { fg: 'info', bold: true },
  think: { fg: 'muted', italic: true }, tool: { fg: 'warning' }, out: { fg: 'muted' },
  ok: { fg: 'success', bold: true }, bad: { fg: 'error', bold: true }, sys: { fg: 'interactive' },
  add: { fg: 'base', bg: 'added' }, del: { fg: 'base', bg: 'removed' },
  head: { bold: true }, activity: { fg: 'accent' }, clock: { fg: 'muted' },
}
export const lineRun = (l: { s: string; t: string }): Run => ({ text: l.t || ' ', ...(LINE[l.s] ?? {}) })

// ---------------------------------------------------------------- whose tasks

/** The session the TUI shows: its id, its directory, the user's home, whether paths fold case. */
export interface Here { readonly sessionID: string; readonly dir: string; readonly home: string; readonly fold: boolean }

/** This session's tasks (the kernel's `owns`, as `wb status` judges them: its own id, else by
 * directory; another session's never, its session unknown or live, as `unknown` is never gone). */
export const mine = (t: PaneTask, here: Here): boolean => owns({
  session: t.session, ids: here.sessionID ? [here.sessionID] : [], driver: t.driver, host: 'opencode', gone: false,
  cwd: t.cwd, repo: t.repo, subdir: t.subdir, here: here.dir, home: here.home, fold: here.fold,
}).mine

/** What the sidebar and the footer count: in workbench mode every task, as the panel lists them;
 * else this session's (the mod's taskParts: another session's task filled the status line of a
 * session kept off workbench, 2026-11-17). */
export const scoped = (tasks: readonly PaneTask[], here: Here, mode: boolean): PaneTask[] =>
  mode ? [...tasks] : tasks.filter(t => mine(t, here))

// ---------------------------------------------------------------- the footer item

/** The footer's status item, in place of Claude Code's strip (#1332): `⚒ wb` and the open
 * tasks' counts by phase (the kernel's openCounts, a stopped one in error), while there is
 * something to bring back (the mode, or a task of this session's). A parked task waits for no
 * one and is left out, as the strip leaves it out (the mod's taskParts): after /workbench-quit
 * the footer said `⚒ wb · 2 review` for the tasks it parked (#1516, 2026-11-26). null: nothing
 * to show. */
export function footer(tasks: readonly PaneTask[], mode: boolean): Run[] | null {
  const open = tasks.filter(t => !CLOSED.has(t.phase) && !t.parked)
  if (!mode && !open.length) return null
  const runs: Run[] = [{ text: `${MARK} wb`, fg: 'accent', bold: true }]
  for (const c of openCounts(open).counts) {
    runs.push({ text: ' · ', fg: 'muted' }, { text: `${c.n} ${c.phase}`, fg: c.stopped ? 'error' : 'muted' })
  }
  return runs
}

// ---------------------------------------------------------------- the sidebar section

/** The sidebar's Workbench section: its heading with the counts, then a line a task (the Tasks
 * view's one-line form at the sidebar's width), each with the task it is; none without tasks. */
export function sidebar(tasks: readonly PaneTask[], cols: number, now: number, spin: string, followed: string):
  { head: Run[]; rows: { runs: Run[]; task: string }[] } | null {
  if (!tasks.length) return null
  const open = tasks.filter(t => !CLOSED.has(t.phase))
  const counts = openCounts(open).counts
  const said = counts.length ? clip(` (${counts.map(c => `${c.n} ${c.phase}`).join(', ')})`, Math.max(0, cols - NAME.length)) : ''
  const head: Run[] = [{ text: NAME, bold: true }, ...(said ? [{ text: said, fg: 'muted' as const }] : [])]
  const rows = taskLines([...tasks], { cols: Math.max(30, cols), now, spin, followed, compact: true, who: WHO })
    .map(l => ({ runs: l.parts.map(runOf), task: l.first ? l.task : '' }))
  return { head, rows }
}

// ---------------------------------------------------------------- the panel

export const SPIN = ['✻', '✳', '✶', '✢', '·', '✢', '✶', '✳']
export const spinAt = (now: number) => SPIN[Math.floor(now / 500) % SPIN.length] ?? '✻'

/** The panel's task rows (the Tasks view, two lines a task unless the panel is short), each with
 * the task it draws and whether it is the line a press follows. */
export function panelRows(tasks: readonly PaneTask[], cols: number, rows: number, now: number, followed: string):
  { runs: Run[]; task: string; first: boolean }[] {
  const open = tasks.filter(t => !CLOSED.has(t.phase))
  const headers = new Set(open.map(t => t.group).filter(g => g && open.filter(t => t.group === g).length >= 2)).size
  return taskLines([...tasks], { cols: Math.max(30, cols), now, spin: spinAt(now), followed, who: WHO,
    compact: compactWanted(open.length, headers, rows) })
    .map(l => ({ runs: l.parts.length ? l.parts.map(runOf) : [{ text: ' ' }], task: l.task, first: l.first }))
}

/** The modeline (#1283's, as Claude Code's): `[•_•] Workbench - <the web app's link>`, the link
 * on rows of its own when it doesn't fit. */
export const modelineOf = (link: Link, cols: number, deep = false) => modeline(link, cols, NAME, deep)

/** Whether the apprentice is drawn in pixels: rows and columns enough for it beside its bubble
 * (the mod's rule: 20 rows, 40 columns), else its face on the line. */
export const pixels = (rows: number, cols: number) => rows >= 20 && cols >= 40

/** Where the splash ends (splash.ts `Target`): the panel's foot is the modeline, under it the
 * link's rows, over it the cap's line, over that the stage, the apprentice at its top, all
 * pinned to the panel's bottom, so its rows are known exactly from the panel's size. */
export function target(rows: number, cols: number, link: Link, cap: boolean): { stage: number; bar: number } {
  const head = 1 + modelineOf(link, cols).rows.length
  const stage = pixels(rows, cols) ? ROWS : 1
  return { stage: Math.max(0, rows - head - (cap ? 1 : 0) - stage), bar: Math.max(0, rows - head) }
}

/** The separator over the followed task's transcript: its name, and which view shows. */
export const separator = (task: string, view: 'transcript' | 'diff', cols: number) =>
  [...`── ${task} · ${view === 'diff' ? 'diff' : 'transcript'} ${'─'.repeat(cols)}`].slice(0, cols).join('')

/** A bubble's lines: `text` broken at words into lines of `width`, at most `max`. */
export function wrap(text: string, width: number, max = Infinity): string[] {
  const out: string[] = []
  let line = ''
  for (const word of text.split(/\s+/).filter(Boolean)) {
    if (!line) line = word
    else if (line.length + 1 + word.length <= width) line += ` ${word}`
    else { out.push(line); line = word }
    while (line.length > width) { out.push(line.slice(0, width)); line = line.slice(width) }
  }
  if (line) out.push(line)
  return out.length > max ? [...out.slice(0, max - 1), clip(out.slice(max - 1).join(' '), width)] : out
}

// ---------------------------------------------------------------- the driver

/** What `wb driver --session <id> --json` says (driver.info, over the `workbench-driver` RPC):
 * the parts the panel's driver row reads. */
export interface DriverInfo {
  readonly model?: string; readonly label?: string; readonly kind?: string; readonly pin?: string; readonly error?: string
}
/** The TUI's own model picker, `/models` (tui/src/app.tsx's `model.list`), which the row opens. */
export const MODELS = 'model.list'
export const CHANGE = 'change'

/** The panel's driver row (#1316): `driver: <provider/id#variant> (GPU|API)`, the pin when it
 * names another model (routes.json's `driver`, applied at the next /workbench), and "change": the
 * row's press opens OpenCode's own /models (pane.tsx), as the session's model is OpenCode's
 * choice and wb never makes it. Cut to ``cols`` from the pin first, then the model, "change"
 * always shown. None before wb answers, or when it failed (the handler's `error`). */
export function driverRow(d: DriverInfo | null | undefined, cols: number): Run[] | null {
  if (!d?.label) return null
  const side = d.model ? ({ gpu: ' (GPU)', api: ' (API)' } as Record<string, string>)[d.kind ?? ''] ?? '' : ''
  const head = `driver: ${d.model ? d.label : 'not known yet'}${side}`
  const tail = ` · ${CHANGE}`
  const pinned = d.pin && d.pin !== 'session' && d.pin !== d.label ? ` · pin ${d.pin}` : ''
  const pin = head.length + pinned.length + tail.length <= cols ? pinned : ''
  return [
    { text: clip(head, Math.max(1, cols - tail.length)), fg: 'muted' },
    ...(pin ? [{ text: pin, fg: 'muted' as const }] : []),
    { text: ' · ', fg: 'muted' },
    { text: CHANGE, fg: 'interactive', underline: true },
  ]
}
