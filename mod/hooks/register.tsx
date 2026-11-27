// workbench: the Claude Code side of delegating to local OpenCode agents.
//
// It reads the task files `wb` and its runners write (~/.local/state/workbench, or $WB_HOME) and
// does three things with them: draws a live pane of the tasks, keeps a one-line status, and, when
// a task needs Claude (ready for review, a question, stuck, failed), starts a turn for Claude with
// the task's report in it, so nobody has to poll. Acknowledgements are shared with `wb wait`
// through each task's `acked` file, so a notification wakes Claude once, by whichever got it first.
// A wake is held while Claude's main loop is mid-turn (a prompt submitted then would queue and
// arrive stale) and checked against the task's phase when it is finally sent.
// A session acts only on its own tasks (`mine`): those `wb start` ran in it (task.json `session`,
// one of the ids this process has had), wherever it ran; a task without one (started by hand, or
// before 0.90), or whose Claude Code process is gone, by directory, and an old task with neither
// that nor a directory is everyone's.
// Another session wakes for, stops and hands back its own (the 2026-11-16 audit: two
// projects at once, each session was woken for and quit the other's). A session with no person
// at it (`claude -p`, the SDK, until a surface attaches) stays quiet.
// Claude's own context. In workbench mode, whatever compacts it (the engine at its limit, /compact,
// this mod), the summary is told to keep the delegation state, and after a compaction or /clear
// the open tasks come back as a context block. At a task boundary (one of this session's just
// closed, none waiting for Claude) the mod compacts early once Claude is half of the way to the
// engine's own compaction (`WB_COMPACT_AT`; the kernel's `compactPlan`), so a long delegation
// session sheds finished tasks' detail at a clean point instead of mid-review.
const KEEP_ON_COMPACT =
  'Workbench delegation is in progress. Keep: the user\'s goals and decisions, which tasks were ' +
  'delegated (names, repos, branches, what each is for), which are open and what each needs ' +
  'next, and what was accepted, merged or discarded. Drop the diffs, reports and transcripts of ' +
  'finished tasks; `wb status` and `wb report <name>` bring back any detail.'
// The weekly model check: once a week, in a quiet moment of a session the person is using, one
// toast says it is due; /workbench-models asks Claude to look at this machine, what is installed
// and how it has done, and what is newest, and to recommend what to add or retire. It was
// submitted as a turn of its own, about 650 tokens, raised again daily until done (#1322).
const SCOUT_EVERY_MS = 7 * 24 * 3600 * 1000
const MODELS = 'workbench-models'
const SCOUT_DUE = `workbench: the weekly model check is due: /${MODELS}`
// what `wb scout` flags (the skill library, the media engines' licences) it says how to act on itself
const SCOUT_NOTE =
  '[workbench] The weekly model check is due. As the driver, keep the agents on the best ' +
  'models for each role, GPU and API alike:\n' +
  '1. `wb scout`: this machine\'s hardware, what is installed, and how each model has done ' +
  '(real tasks), and its watch list: models set aside and what each needs ' +
  'before it is tried again. Act on what it flags.\n' +
  '2. Look up what is newest: open-weight coding models with tool calling, their sizes and ' +
  'quantizations, which fit this GPU\'s VRAM (with room for the context) and this RAM, and ' +
  'recent releases of the model server this machine runs (whichever it is: `wb scout` lists ' +
  'them). For roles on API, `wb scout`\'s API section: each role\'s model, its record and spend; ' +
  'look up newer or cheaper models from the same connected providers on their own pages, and ' +
  'whether thinking would pay for significant (its record). ' +
  'Prefer primary sources (model cards, the server\'s own model library, ' +
  'release notes) and recent reports; name nothing you could not verify.\n' +
  '3. Recommend in a few lines: keep, add (the model and quantization, why it should beat what ' +
  'is installed, its size and how it fits), retire (what it replaces, the disk it frees). ' +
  'Aim for the best results first, then efficiency. For each watch-list item, say whether ' +
  'its conditions are met yet; `wb scout --unwatch N` once one is settled. Ask before ' +
  'downloading or deleting (the skill\'s models reference says how).\n' +
  '4. `wb scout --done --notes "<what you recommended or changed>"`.\n' +
  'Keep it short; if nothing is worth changing, say so in one line.'
// Workbench mode: /workbench turns it on for the rest of the session and opens the pane, its sign.
// The user then asks for things in plain words, and Claude hands the coding to OpenCode agents as
// the skill says. A fresh or compacted conversation is told the mode is still on and where the
// skill is (`workbench-mode` block). Two slash commands end it: /workbench-quit stops this session's
// agents at once (`wb stop --all --repo <cwd> --session <id>…`) and tells Claude so;
// /workbench-quit-and-hand-back-claude stops them and hands their tasks to Claude (`wb takeover
// --all`, the same scope), starting a turn for Claude to finish them. Either closes the pane.
// Under the task list the pane follows one task live: the conversation between Claude and the
// agent, every tool call and test, as `wb watch --json` prints it.
import { atom, read, update } from 'claude-code'
import type { Register } from 'claude-code'

import type { WbChat, WbLine, WbLive, WbPane, WbSaid, WbTask } from '../types'
import type { ChatOpen, ListenerClaim, ModelsJson as WbModels, NoticeRow, RouteJson as WbRoutes, StatusFile, TaskFile } from '../types/wire.js'
import { cells, COLUMNS, FACE, MOUTH, ROWS } from './apprentice'
import type { Pose } from './apprentice'
import { frameCells, frameCount, packed, plan, SPLASH_MS } from './splash'
import type { Plan, Target } from './splash'
import { modeline } from './modeline'
import { keywords, onlyToggles } from './keywords'
import { FLOW_NOTE, flowCommands, flowDescription, flowHint, type FlowCommand } from './flows'
import { sameWait, waitingFact } from './waiting'
import { CLAUDE_CODE as SAY } from './story'
import type { Waiting } from './waiting'
import type { Link } from './modeline'
import { ago, apiWorking, capRow, clip, compactWanted, focusOf as focusOfTasks, frameOf, isEst, speechOf, taskLines, viewFacts } from './tasks'
// moved to tasks.ts, shared with the OpenCode plugin's panel (#1312); still exported from here
export { isEst, viewFacts }
import { ACTIVE, awayNow as awayOf, CHAT_STALE_MS, CLOSED, codingGate, compactPlan, editFields, gateOf, modesPlan, NEEDS, NO_ANSWER, noticePlan, noticesDue, openCounts, owns, PARKS, phaseOf, rewakePlan, runNoticePlan, runNoticesDue, tellPlan, WAKE_OPENING } from './core'
import type { Gate as CommandGate } from './core'
import type { Part as RowPart } from './tasks'
// the command line as bash reads it and the consent gate's reading of it, shared with the OpenCode
// plugin's upstream gate (#1296); still exported from here
import { absolutePath, delegates, gatedWhere, GITHUB_MCP, inside, mcpAimsUpstream, mentions, originIs, runs, shownCommand,
  shownInput, spawns, upstreamAim, W } from './shell'
import type { Aim } from './shell'
export { absolutePath, delegates, mcpAimsUpstream, mentions, originIs, shownCommand, upstreamAim }
export type { Aim }

const PANE = 'workbench'
// Nothing waits on a timer for news: `wb events` (inotify) prints a line the moment a task, a
// notification or a chat message changes, and each line runs poll() at once. The timer only
// redraws running tasks' clocks, and checks in now and then should the watcher be gone.
const POLL_MS = 1500 // the clock's redraw while a task runs
const SAFETY_MS = 30_000 // a poll at least this often, whatever the watcher says
const KEEP_CLOSED_MS = 30 * 60 * 1000 // closed tasks stay in the pane for half an hour
// a settings change from the web app older than an hour is no news, as a notice that old wakes
// no one (the kernel's noticesDue)
const OLDEST_WAKE_MS = 60 * 60 * 1000

// Change the tag when WbTask's fields change: a reload declines what the earlier shape stored.
const tasks = atom({ plugin: 'workbench', key: 'tasks' } as const, [] as WbTask[], {
  shape: 'tasks-12',
})

// Every Claude Code session id this process has had (`$.session.id()`): /clear and a resume go on
// under another, and a task started before still belongs here. Kept across a reload.
const sessions = atom({ plugin: 'workbench', key: 'sessions' } as const, [] as string[], {
  shape: 'sessions-1',
})

const live = atom({ plugin: 'workbench', key: 'live' } as const, { task: '', lines: [] } as WbLive, {
  shape: 'live-1',
})

const KEEP_LINES = 2000 // the pane scrolls back through this much history (`wb watch --last`)
// One heredoc command or minified write is a single line of 100 KB or more, and alone it took the
// whole HISTORY_CHARS budget, hiding everything before it (the 2026-11-16 audit): each line is
// cut to this first.
const LINE_CHARS = 2000
const LIVE_EVERY_MS = 5000 // a running task's transcript is re-read this often, and no more often though its status moves (#691)
// The engine draws a pane's first 100,000 characters of text and drops the rest, and the
// apprentice comes after the history: a long task's history pushed its words (and its mouth)
// past the cut, leaving an empty bubble (seen 2026-11-16, 119,728 characters). So the history
// drawn is its newest lines within this budget, leaving the rest of the pane room.
const HISTORY_CHARS = 60000

// The web chat (`wb chat serve`): the user's side door while away from the terminal, and since 2.0
// the web app with its Stats and Settings (#1283): the terminal stays the primary way to work with
// Claude, the web chat a second lane, and the web app's Settings are where the settings live. A
// session listens while workbench mode is on in it (`delegating`), from /workbench until a quit
// command; with several, the first to claim chat/listener.json does, and the others stand by. A
// message is submitted as a prompt (so the terminal shows it too) with a receipt the page shows at
// once. `link` is the web app as `wb chat url --json` says it (chatLink): on by default, and after
// `wb chat off` (state `off`) no session listens or sends it prompts.
const chat = atom({ plugin: 'workbench', key: 'chat' } as const,
  { key: '', delegating: false, link: { state: 'checking' } } as WbChat, { shape: 'chat-5' })
// Whether the web chat is on (the user's switch): wb says `off` only after `wb chat off`
const webOn = (c: WbChat) => c.link.state !== 'off'
// The pane is Tasks only (#1283: its Stats and Settings tabs went to the web app, the owner,
// 2026-11-24). Closed by the user (its mark, Esc), the pane is minimized, as backseat-driver's is
// (the user, 2026-11-19): a strip above the prompt stands in for it, a press there or /workbench
// brings it back.
const pane = atom({ plugin: 'workbench', key: 'pane' } as const,
  { open: '', focus: '', minimized: false } as WbPane, { shape: 'pane-2' })
// Whether this session's first /workbench has come (#963): the opening animation plays at that
// one alone. An atom, so a module reload (which runs session.start again with `mem` empty) can't
// play it again; /workbench again and a bring-back from the strip find it set.
const splashed = atom({ plugin: 'workbench', key: 'splash' } as const, false, { shape: 'splash-1' })
const STRIP_HINT = 'minimized · restore: click, or Ctrl+X Tab then Enter'
// The web app's link, looked up at session start and every CHAT_CHECK_MS (`wb chat url --json`,
// which starts the app if it isn't up), and again CHAT_RECHECK_MS after a start or a failure
const CHAT_CHECK_MS = 60_000
const CHAT_RECHECK_MS = 3_000
// Whether the user is away (the kernel's awayNow, which the OpenCode plugin judges by too): six
// hours after they last used the web chat (chat/away.json's `at`, #959), or for as long as this
// session has a task open (#110, #1402)
export const awayNow = (webAt: number, now: number, list: WbTask[], isMine: (t: WbTask) => boolean): boolean =>
  awayOf(webAt, now, list.map(t => ({ phase: t.phase, parked: t.parked, mine: isMine(t) })))
// Whether the weekly model check and the automatic optimize may run: no task anywhere active (the
// GPU's, and a check's question beside a running task's), and none of this session's waiting on
// Claude lately. Any task open at any age once gated both, so one stuck, failed or unreviewed
// task left after a quit stopped them for good (#541); one idle past rewake's 12 hours waits for a
// session long gone, as rewake judges it; a parked one waits for no one.
export function quietForJobs(list: WbTask[], now: number, isMine: (t: WbTask) => boolean): boolean {
  return !list.some(t => ACTIVE.has(t.phase) || rewakeOf(t, now, isMine(t)).recent)
}
// Away is the user's, not one session's (#688): the listening session records the last web
// message in chat/away.json and a prompt typed in any session clears it, so every delegating
// session posts its dialogs' waiting line at once and says AWAY_NOTE. Kept in the listener's own state, only it did:
// another session's overnight backlog left its permission dialogs in the empty terminal (#110 again).
// The chat server writes it too when a page is in use (#959): the owner typed in the terminal,
// then picked up the phone, and a prompt neither reached it nor was allowed by their setting. So
// away is the device last used: the chat, used after the last prompt typed here.
const awayFile = () => `${mem.root}/chat/away.json`
// Whether a prompt is the user's own (at the terminal, or through Remote Control), by the
// engine's stamp: a /loop or scheduled firing, another plugin's prompt or an MCP channel's
// message ended away for every session and cancelled the #767 follow-up (#835). An unstamped
// prompt goes by isMeta, as before the stamp; an `unclassified` one by isMeta too (the engine
// stamps some queued typed prompts so).
const USER_ORIGINS = new Set(['composer', 'bridge', 'sdk', 'auto-continuation'])
const byUser = (e: { isMeta?: boolean; origin?: { kind?: string } }) => {
  const kind = e.origin?.kind
  if (!kind || kind === 'unclassified') return !e.isMeta
  return USER_ORIGINS.has(kind)
}
async function webAt($: any): Promise<number> {
  return Number((await readJson($, awayFile())).at ?? 0) * 1000 || 0
}
async function away($: any, st: WbChat): Promise<boolean> {
  return webOn(st) && awayNow(await webAt($), await $.clock.now(), await read($, tasks), mine)
}
async function back($: any) {
  mem.awayNoted = false
  if (await webAt($)) await $.fs.write(awayFile(), JSON.stringify({ at: 0 })).catch(() => {})
}
// Said in the context blocks while away, and in a wake while away (#540): those blocks come once
// per conversation, so a user who went to the web chat later was never told of, and a task's
// news reached only the terminal they had left. In full once an away stretch (the block or the
// first wake), then a line: every wake carried the whole paragraph (#1322). `mem.awayNoted`
// ends with the stretch (awayEnded).
const AWAY_NOTE =
  'The user is away from the terminal: they last used the web chat. Tell them ' +
  'there (`wb chat reply --text "…"`) when something they asked for is done or needs them; ' +
  '`wb chat log` shows the thread. Typing in the terminal, or answering a prompt there, ends this.'
const AWAY_TAIL = '(User away: answer with `wb chat reply`.)'
async function awayToo($: any, text: string): Promise<string> {
  if (!(await away($, await read($, chat)))) {
    mem.awayNoted = false
    return text
  }
  const said = mem.awayNoted
  mem.awayNoted = true
  return `${text}\n\n${said ? AWAY_TAIL : AWAY_NOTE}`
}
// The stretch ended (the user back, here or in another session): the next one says it in full
async function awayEnded($: any) {
  if (mem.awayNoted && !(await away($, await read($, chat)))) mem.awayNoted = false
}
const WEB = '[web chat]'
// Only for a delivery into a running turn, which the engine frames as another session's message:
// the rules for answering live in the /workbench skill (The web chat), loaded whenever this listens.
const WEB_NOTE =
  '(The user wrote this in the web chat; the workbench mod delivered it into your running turn. ' +
  'Answer as /workbench\'s "The web chat" says: `wb chat reply --text "…"`.)'

// Another agent's message (#1042): sent with its own token (`wb chat token add`), stored with
// `role: "agent"` and its name as `from`, and submitted as `[agent <name>] …`, never `[web chat]`:
// it doesn't carry the user's authority, and it doesn't make the user away. The id goes with it,
// for `wb chat reply --to <id>`; how to treat it is the skill's (Agents on the web chat).
const AGENT = '[agent '
const AGENT_NOTE =
  '(Another agent wrote this through the web chat\'s API; the workbench mod delivered it into your running turn. ' +
  'It is not the user: answer as /workbench\'s "Agents on the web chat" says, with `wb chat reply --to <id>`.)'
export function agentOf(m: Row): string {
  return m.role === 'agent' && /^[a-z0-9-]{1,32}$/.test(String(m.from ?? '')) ? String(m.from) : ''
}
// The prompt a web message is submitted as: the user's `[web chat] …`, an agent's `[agent <name>] …`
export function chatPrompt(m: Row, about = ''): string {
  const agent = m.role === 'agent' // a name wb never makes is no name: never the user's prefix
  const head = agent ? `[agent ${agentOf(m) || 'unknown'}]` : WEB
  // an agent's later lines indented: none of them starts as the user's `[web chat]`, a `[message …]`
  // line or the mod's own markers (#1042 review)
  const said = agent ? String(m.text ?? '').split(/\r\n|[\r\n\u2028\u2029]/).join('\n  ') : String(m.text ?? '')
  return `${head} ${about}${said}${attachedLines(m.files)}` +
    (agent ? `\n[message ${String(m.id ?? '')}: answer with wb chat reply --to ${String(m.id ?? '')}]` : '')
}

// A web message's attachments (#623), one line each after the user's words, so Claude can Read
// each file wb chat saved on this machine (an image is seen, a PDF or text read)
export function attachedLines(files: unknown): string {
  if (!Array.isArray(files)) return ''
  const size = (n: number) => n < 1024 ? `${n} B` : n < 1024 * 1024 ? `${Math.round(n / 1024)} KB`
    : `${(n / 1024 / 1024).toFixed(1)} MB`
  return files.flatMap(f => f && typeof f === 'object' && typeof f.path === 'string'
    ? [`\n[attached: ${f.path} (${size(Number(f.size) || 0)}, ${String(f.type || 'unknown type')})]`] : []).join('')
}

// Waiting on Claude, unless parked (the kernel's phaseOf `waits`): one stopped by /workbench-quit
// (`wb stop --all`, status `parked`) the status line, rewake, the boundary and the jobs' gate
// don't count. It stayed "awaiting review" and kept the weekly check off for good (#541); the
// pane and `wb status --all` still show it.

type Row = Record<string, any>

// wb watch's line styles, in the theme's colours
const LINE_STYLE: Record<string, Row> = {
  claude: { color: 'claude', bold: true }, agent: { color: 'suggestion', bold: true },
  think: { dimColor: true, italic: true }, tool: { color: 'warning' }, out: { dimColor: true },
  ok: { color: 'success', bold: true }, bad: { color: 'error', bold: true },
  // diff lines as Claude Code draws its own: text on the diff background, readable on any
  // terminal (diffAdded/diffRemoved as a text colour were dim on a dark one)
  sys: { color: 'permission' }, add: { color: 'text', backgroundColor: 'diffAdded' },
  del: { color: 'text', backgroundColor: 'diffRemoved' },
  head: { bold: true }, activity: { color: 'claude' }, clock: { dimColor: true },
}
const SPIN = ['✻', '✳', '✶', '✢', '·', '✢', '✶', '✳']

// The apprentice (apprentice.ts) at the foot of the pane: how it stands and what it says about
// the followed task. A timer (FRAME_MS) swings the wrench now and then while it works and blinks
// it otherwise: a stroke every frame was a distraction beside the transcript.
const FRAME_MS = 450
const APPRENTICE = 'apprentice' // its Raster's key, for $.ui.blit
// What the Tasks page is made of, apart from its drawing: the render draws it and pageShape
// measures it, so the two can't disagree (#963).
function pageParts(list: WbTask[], watched: WbLive, delegating: boolean, now: number, spin: string, cols: number,
  pixels: boolean, bodyRows = 24, cap: { spent: number; cap: number | null; est?: boolean } | null = null) {
  const rows = listRows(list, watched.task, now, spin, cols, bodyRows)
  const capLine = cap ? capRow(list, cap.spent, cap.cap, cols, usd, cap.est) : null
  // The history is drawn, newest within HISTORY_CHARS: the engine scrolls the pane (arrows,
  // while focused) and follows the end as lines arrive. What the agent is doing now is the
  // apprentice's to say.
  const all = watched.lines.filter(l => !['activity', 'clock', 'meta', 'routed'].includes(l.s))
  let from = all.length // (each line was cut to LINE_CHARS when read)
  for (let used = 0; from > 0 && used + (all[from - 1].t?.length ?? 0) + 1 <= HISTORY_CHARS; from--)
    used += (all[from - 1].t?.length ?? 0) + 1
  const history = all.slice(from)
  const doing = watched.lines.find(l => l.s === 'activity')
  const clock = watched.lines.find(l => l.s === 'clock')
  const meta = watched.lines.find(l => l.s === 'meta')
  const routed = watched.lines.find(l => l.s === 'routed') // how its model was chosen (#1016)
  const focus = focusOf(list, watched.task)
  const sp = speechOf(focus, doing?.t ?? '', list, delegating)
  const busy = sp.pose === 'work' || sp.pose === 'think'
  return {
    rows, capLine, from, history, meta, routed, focus, sp, busy,
    // The bubble's inner width beside the apprentice (its columns, the mouth's, the border and
    // padding), or beside the face on one line; each line drawn whole, never squeezed.
    room: Math.max(10, pixels ? cols - COLUMNS - 1 - 4 : cols - 4),
    say: `${busy && doing ? `${spin} ` : ''}${sp.say}`,
    more: sp.more,
    clock: clock && focus?.name === watched.task ? clock.t : '',
  }
}

// Text a terminal draws one column a character, for sure: ASCII, Latin, Greek and Cyrillic, and
// the punctuation, arrows, shapes and dingbats the pane uses, less the emoji drawn two wide.
// Anything else (wide East Asian, emoji, combining marks, controls, a newline) and a line's
// height isn't known.
const NARROW = /^[\x20-\x7e -¬®-˿Ͱ-ԯ‐-‧‰- ⁰-⌙⌜-⌨⌫-⏨⏭-⏯⏱⏲⏴-◼◿-☓☖-♇♔-♾⚀-⚒⚔-⚠⚢-⚩⚬-⚼⚿-⛃⛆-⛍⛏-⛓⛕-⛩⛫-⛱⛴⛶-⛹⛻⛼⛾-✄✆-✉✌-✧✩-❋❍❏-❒❖❘-➔➘-➯➱-➾⟀-⬚⬝-⭏⭑-⭔⭖-⯿]*$/

/**
 * The Tasks page's height in rows, as the render lays it out, for the opening animation, whose
 * words fall into the modeline's row and whose apprentice walks to its spot (#963). `rows` is
 * null where a line may wrap: a Text wider than the pane wraps at Ink's own word boundaries,
 * which can't be known here exactly, and `atLeast` counts each such line as one row. `stage` is
 * the row the apprentice's Raster starts on, `tail` the rows from there to the end.
 */
export function pageShape(list: WbTask[], watched: WbLive, delegating: boolean, link: Link, now: number,
  cols: number, bodyRows = 24): { rows: number | null; atLeast: number; stage: number; tail: number | null; head: number } {
  const spin = SPIN[0] ?? '✻'
  const page = pageParts(list, watched, delegating, now, spin, cols, true, bodyRows, capOf(list))
  let exact = true
  const line = (text: string) => { // a Text that wraps: one row when it fits
    if (!NARROW.test(text) || text.length > cols) exact = false
    return 1
  }
  // the task list: one row each, none wider than the pane (taskLines), and the closing gap
  let rows = 0
  for (const r of page.rows) rows += line(r.parts.map(p => p.text).join(''))
  if (page.rows.length) rows += 1
  if (watched.task) {
    rows += line(clip(`── ${watched.task} · ↑↓ scrolls ` + '─'.repeat(cols), cols))
    if (page.meta) rows += line(page.meta.t)
    if (page.routed) rows += line(page.routed.t)
    if (page.from > 0) rows += line(`… ${page.from} earlier lines: \`wb watch ${watched.task}\` has them all`)
    for (const l of page.history) rows += line(l.t || ' ')
  }
  const said = wrapWords(page.say, page.room).length + (page.more ? wrapWords(page.more, page.room).length : 0) +
    (page.clock ? wrapWords(page.clock, page.room).length : 0)
  const stage = rows + 1 // the stage's marginTop
  // the stage's height (the apprentice, the mouth, the bubble below its marginTop), the cap's
  // line, then the header: the modeline and, when the link doesn't fit on it, the link's rows (#1283)
  const head = 1 + modeline(link, cols).rows.length
  const tail = Math.max(ROWS, MOUTH + 1, MOUTH - 1 + 2 + said) + (page.capLine ? 1 : 0) + head
  return { rows: exact ? stage + tail : null, atLeast: stage + tail, stage, tail, head }
}

// Where the splash's last frame puts the apprentice and the modeline's bar (its first row: the
// link's own rows, when it has them, come under it), in a pane body of
// `body` rows: the page's own rows when it fits (a docked pane draws it from the top; an inline
// one, sized to it, keeps its top where it was), else its end, which the pane follows. A page
// that may wrap and doesn't surely overflow has no exact bar row: the words rise away instead.
export function splashTarget(shape: ReturnType<typeof pageShape>, body: number): Target {
  if (shape.rows !== null && shape.rows <= body) return { stage: shape.stage, bar: shape.rows - shape.head }
  // more than the pane surely: its end, the header the pane's last rows (the stage as near as known)
  if (shape.atLeast > body) return { stage: body - (shape.tail ?? shape.atLeast - shape.stage), bar: body - shape.head }
  return { stage: shape.stage, bar: null }
}

const parseLines = (text: string): Row[] =>
  text.split('\n').flatMap(line => {
    if (!line.trim()) return []
    try {
      return [JSON.parse(line)]
    } catch {
      return []
    }
  })

// a JSON file of wb's (one of wire.d.ts's shapes, `T`), {} for one missing or torn
const readJson = async <T extends object = Row>($: any, path: string): Promise<Partial<T>> =>
  $.fs.read(path).then((t: string) => JSON.parse(t)).catch(() => ({}))

// Words broken into lines of at most n characters (a longer word is cut across lines). The
// bubble wraps with this, not with Text's own wrapping: in a row that shares its width, that
// laid out the rows and drew them empty (seen 2026-11-16).
export const wrapWords = (text: string, n: number): string[] => {
  const lines: string[] = []
  let line = ''
  for (let word of text.split(/[^\S\u00a0]+/).filter(Boolean)) { // a no-break space (NB) holds a unit whole
    if (line && line.length + 1 + word.length > n) {
      lines.push(line)
      line = ''
    }
    while (word.length > n) {
      lines.push(word.slice(0, n))
      word = word.slice(n)
    }
    line = line ? `${line} ${word}` : word
  }
  if (line) lines.push(line)
  return lines.length ? lines : ['']
}

const eventLine = (r: Row): string | null => {
  switch (r.kind) {
    case 'tool': return `${r.tool} ${r.brief ?? ''}${r.ok ? '' : '  ✗'}`.trim()
    case 'tests': return `tests ${r.ok ? 'pass' : 'FAIL'}`
    case 'autofix': return 'tests failed → sent back to the agent'
    case 'ask': return `asks: ${r.text}`
    case 'answer': return `answered: ${r.text}`
    case 'allow': return `Claude ${r.text}`
    case 'stopped': return `stopped: ${r.text}`
    case 'error': return `error: ${r.text}`
    case 'text': {
      const first = String(r.text ?? '').trim().split('\n')[0]
      return first ? `“${first}”` : null
    }
    default: return null
  }
}

const HEADLINE: Record<string, string> = {
  review: 'is ready for review',
  question: 'asks the reviewer a question (the agent is waiting)',
  stuck: 'was stopped: it got stuck',
  failed: 'failed',
  checkin: 'check-in (still running)',
}
// A question's notice opens `Q: ` or `PERMISSION to ` (ask_mcp.notice: an API); only a permission
// request gets the `wb allow` paragraph, which every question once carried (#1322)
const PERMISSION = 'PERMISSION to '
const NEXT: Record<string, (n: string, id?: string, text?: string) => string> = {
  review: n => `Review it as /workbench says, from the diff above (\`wb diff ${n}\` when it was too ` +
    `big to include), then \`wb accept ${n} -m "…"\` or \`wb feedback ${n} --message <file>\`.`,
  question: (n, id, text) => `Answer with \`wb answer ${n}${id ? ` --id ${id}` : ''} --text "…"\`; it is blocked until then.` +
    (text?.startsWith(PERMISSION) ? ` A PERMISSION request (a refused command, directory or port): the \`wb allow ${n}\` it ` +
      `names (\`--always\` to keep it) when it is reasonable, which answers it and restarts the agent ` +
      `with it; else answer with another way.` : ''),
  stuck: n => `Steer it with \`wb feedback ${n} --message <file>\`, or \`wb discard ${n}\`.`,
  failed: n => `Look at \`wb log ${n}\`; then \`wb feedback ${n} --message <file>\` or ` +
    `\`wb discard ${n}\`.`,
  checkin: n => `As the driver: tell the user in two or three plain lines how it is going ` +
    `(progress, time, anything off), and log the same with \`wb note ${n} --text "…"\`. ` +
    `If it is heading the wrong way, \`wb steer ${n}\` (\`--now\` to interrupt). Keep it short.`,
}
// One notice for the tasks a restart or a shutdown interrupted (its `tasks`: cli._restart_notice,
// runner._signal_notice). Nothing went wrong in them, yet the wake ended with the failure's
// `wb log`, feedback or discard after the notice's own `wb resume --all` line (#544).
const BATCH_NEXT = (names: string[]) => names.length > 1
  ? `Nothing went wrong in the tasks themselves: \`wb resume ${names.join(' ')}\` continues ` +
    'them all, in the order they were started (`wb resume <name>` one, `wb discard <name>` drops one).'
  : `Nothing went wrong in the task itself: \`wb resume ${names[0]}\` continues it in a fresh ` +
    `session, \`wb discard ${names[0]}\` drops it.`
const batchOf = (row: NoticeRow): string[] | null =>
  Array.isArray(row.tasks) && row.tasks.length ? row.tasks.map(String) : null
// A stop at the day's API spend cap (#1019, cause `spend`) is the user's call: feedback would run
// a round that stops again at once, so the wake asks for their decision, as the notice does. Each
// remedy has its own next step: "the role on the GPU … then wb resume" resumed the task on its API
// model (fixed per task) into the same stop, and with no cap set "tomorrow" changes nothing
// (#1095, 2026-11-23). `capped`: routes.json has a day cap. The way off API is the notice's
// (meter.stop_text, from routes.json): `wb route <role> gpu` here changed nothing under Agents API
// or for a task the busy opt-in sent, and the next start was refused the same way (#1218, 2026-11-24).
const SPEND_NEXT = (n: string, capped: boolean) => (capped
  ? `Ask the user: a higher cap (\`wb route cap <usd>\`) or tomorrow, then \`wb resume ${n}\`; `
  : `Ask the user for a day cap (\`wb route cap <usd>\`), then \`wb resume ${n}\`; `) +
  `or the route change the notice names, if any, for the next tasks (on their yes), with \`wb takeover ${n}\` ` +
  `or \`wb discard ${n}\` for this one: resume keeps its API model. Never retry it with feedback yourself.`
// A stop for the Agents switch on GPU (cause `spend`, guard `agents`, #1213): no cap lets it go on, and
// resume or feedback is refused until the user sets Agents back
const AGENTS_NEXT = (n: string) =>
  `Ask the user: Agents back to Both or API (the web app's Settings, or \`wb route all both\` on their yes), ` +
  `then \`wb resume ${n}\`; or \`wb takeover ${n}\` or \`wb discard ${n}\`. Never retry it with feedback yourself.`
// A stop by the task's API provider (cause `provider`: runner.PROVIDER_GUARDS, a rate limit or an
// outage held 10 minutes, a quota or a refused key) isn't the agent's, yet it woke as "was stopped:
// it got stuck" ending "Steer it with wb feedback …, or wb discard": a feedback round a quota or
// key stop fails again at once, or work that is fine thrown away, next to the notice's own remedy
// (#1124, 2026-11-23). The headline is the toast's too; its next step is OUTSIDE_NEXT.
const PROVIDER_HEADLINE = 'was stopped by its API provider'
// A stop because the task's repo was marked local-only after it started (cause `local_only`:
// runner._local_only_stop, #1215) isn't the agent's either, yet it woke as "was stopped: it got
// stuck" ending "Steer it with wb feedback …", which wb refuses (exit 3) while the repo stays
// local-only: the task's API model is fixed per task (the bar loop, #1232, 2026-11-24). Its next
// step is OUTSIDE_NEXT: the notice names takeover or discard.
const LOCAL_ONLY_HEADLINE = 'was stopped: its repo became local-only'
// Every other stop outside the step (a `cause` not in results.THE_STEP, the allowlist of the
// agent's own: a model that left OpenCode's catalog, a repo that moved, a setup, checkout, load,
// full disk, model server or reaped runner) has its remedy in the notice itself, from wb, which
// knows it: the wake ended with the failed or stuck one's "wb feedback …", which wb refused
// (exit 3) for a model gone from the catalog, and for a moved repo ran a runner that failed at
// once, next to the notice's own takeover or relocate (the bar loop, #1247, 2026-11-24; #1232's
// shape). One rule for them all, so a new such stop needs only its cause. The provider's and the
// local-only stop had next steps of their own, restating the notice and that Claude never changes
// a role or lifts local-only (#1322): wb's notice says each remedy.
const THE_STEP = new Set(['guard', 'agent'])
const outsideTheStep = (row: NoticeRow) => typeof row.cause === 'string' && !THE_STEP.has(row.cause)
const OUTSIDE_HEADLINE: Record<string, string> = {
  stuck: 'was stopped (not its agent\'s doing)',
  failed: 'failed (not its agent\'s doing)',
}
// Some name no step (a model failure once the round reached the agent: `can't run: <e>`, no remedy
// line): those go on with "carry on" once the cause is gone, and never discard work that is fine
// (#1322's first cut dropped the clause). A local-only stop always names takeover or discard, and
// wb refuses its feedback: OUTSIDE_REMEDY alone.
const OUTSIDE_REMEDY = 'Not the agent\'s doing: its remedy is the notice\'s, above; tell the user when it needs them.'
const OUTSIDE_NEXT = (n: string) => `${OUTSIDE_REMEDY} A notice that names no step: ` +
  `\`wb feedback ${n} --text "carry on"\` once that cause is gone; never discard it for this.`

// A question delivered into a running turn (#542): the engine frames it as another session's
// message, so one line says whose it is and that it can't wait for the turn to end.
const MIDTURN_NOTE = '(The workbench mod delivered this into your running turn: the agent is ' +
  'blocked until it is answered, and gives up after a while. Answer it now, then carry on.)'
// A question held for a turn that could not take it, which then timed out: said after the turn
// rather than acknowledged in silence (#542)
const MISSED_NOTE = (n: string) => `No answer reached it in time (you were busy in a turn), so the ` +
  `agent went on with its own assumption and states it in its summary. Check that assumption ` +
  `when you review, or \`wb steer ${n} --text "…"\` now if it is wrong.`

// /workbench run again with the mode already on and no task: the pane is back, and Claude needn't
// reread the skill to say so.
const AGAIN_NOTE = (skill: string) =>
  '[workbench] The user ran /workbench again: workbench mode is still on, and the Workbench ' +
  `pane is open again. Carry on as the skill says (read ${skill} if it is not in your context); ` +
  'reply in one line.'

const MODE_NOTE = (skill: string) =>
  'Workbench mode is on in this session: the user ran /workbench, and it lasts until /workbench-quit. ' +
  'Treat each request as the /workbench skill says: hand its coding to agents with wb, ' +
  `then review and accept. Every plan you write, in plan mode or not, is a handoff plan: steps ` +
  `sized to the agents' slice, each with its files, exact interfaces and acceptance command (the ` +
  `skill's "Planning work you will hand off"). If that procedure is not in your context, read ` +
  `${skill} first.`

// What Claude learned about how this user wants delegation run (`wb notes`), in any project:
// Claude Code's own memory is per project, so these come back wherever workbench mode is on.
// The add and drop housekeeping is the skill's (#1322: it was said in every conversation).
const NOTES_NOTE = (notes: string) => `The user's delegation notes (\`wb notes\`):\n${notes}`

// Every slash command is /workbench or starts with it, so typing /workbench lists them all.
// /workbench is the skill; once the mode is on, running it again brings the pane back
// (2026-11-17: the commands were /delegate-*, and /delegate-pane did that).
const SKILL = 'workbench'
const UPDATE = 'workbench-update'
const QUIT = 'workbench-quit'
const HAND_BACK = 'workbench-quit-and-hand-back-claude'
// The one that doesn't: /deepmode is the user's own word for Dynamic workflows (#1278), as
// `deepmode on` typed on a line of its own is
const DEEPMODE = 'deepmode'

// Claude coding (#92), the user's choice (`wb coding`): when Claude may write code for a delegated
// task. "decide" (no saved choice) adds nothing, so Claude works as before there was a choice.
const CODING_RULES: Record<string, string> = {
  // the mod's dialog asks at the edit (codingRefusal): Claude once had to ask in chat, wait, and
  // record the yes itself with --edit and --said (#1322)
  ask: 'Claude coding is "Ask me first" (the user\'s choice; `wb coding` shows it): your edits in a ' +
    'task\'s worktree ask the user first.',
  only: 'Claude coding is "Only when I say" (the user\'s choice; `wb coding` shows it). Never write ' +
    'a task\'s code yourself, not a one-line fix in review nor `wb takeover`: guide the agent, ' +
    'and when it stays stuck, report the blocker to the user and keep the work with the agent. ' +
    'They say when you write code; then record their words with `wb coding allow <task> --edit ' +
    '"…" --said "<their words>"`.',
}

// Dynamic workflows (#1041), the user's choice (`wb workflows`): whether Claude may run several
// tasks at once for one request. Off (the default) adds nothing: the skill says one at a time.
// #1278: on, a substantive request is a deep workflow (wb run), its fan-out the run's, not tasks
// Claude starts at once; its notices wake Claude as a task's do (deliverRuns, #1278's S9; before it
// the wake was a background `wb wait <run>`: the review of #1278, 2026-11-25, told "don't poll",
// Claude was never woken)
const WORKFLOWS_RULE = 'Dynamic workflows are on (the user\'s choice; `wb deepmode` shows it): run a substantive ' +
  'request as a deep workflow on their agents, as the /workbench skill\'s *Deep workflows* says: pick a flow ' +
  '(`wb run flows`), say it in at most three lines, start it once with `wb run start`, then end your turn: its ' +
  'notices (a checkpoint, a pause, the end) wake you as a task\'s do. Never fan out by starting several tasks at once ' +
  'yourself: the run starts its items, each on its own role\'s model, never one you pick, and the GPU lease, the ' +
  'provider slots and the day cap decide what runs together. Review each result on its own merits; merge logic ' +
  'first, then tests, then UI or docs.'
const WORKFLOWS_OFF = 'Dynamic workflows are now off (the user\'s choice; `wb deepmode` shows it): run one task ' +
  'at a time again, as the /workbench skill says.'

// The keywords (#1278, keywords.ts): `deepmode on|off` typed is the user's yes, so the mod runs
// `wb deepmode` itself, and sayRules' row tells Claude; `deepcode` adds wb's rule to that one
// prompt (`wb deepmode --rule`, runs.DEEPCODE_RULE, read once a load), as the context the user
// never sees. Only the user's own words count: never a [workbench] turn, an agent's or a slash command.
const DEEPCODE_FALLBACK = 'The user wrote `deepcode` (Workbench\'s word, not Claude\'s `ultracode`): run this ' +
  'request as a deep workflow, as the /workbench skill\'s *Deep workflows* says.'
async function deepcodeRule($: any): Promise<string> {
  if (mem.deepcodeRule) return mem.deepcodeRule
  const { ok, out } = await runWb($, ['deepmode', '--rule'])
  if (ok) mem.deepcodeRule = out
  return ok ? out : DEEPCODE_FALLBACK
}
async function setDeepmode($: any, on: boolean): Promise<string> {
  const { ok, out } = await runWb($, ['deepmode', on ? 'on' : 'off'])
  const said = ok ? `workbench: ${out}` : `workbench: wb deepmode ${on ? 'on' : 'off'} failed: ${brief(out)}`
  $.ui.toast(said)
  return said
}

// deepmode's own key (#1507, 2026-11-26): 2.1's `mode` on meant Claude may start several tasks at
// once; read as 2.2's deepmode it made every substantive request a deep workflow, unasked
async function workflowsMode($: any): Promise<string> {
  try {
    return JSON.parse(await $.fs.read(`${mem.root}/workflows.json`)).deepmode === 'on' ? 'on' : 'off'
  } catch {
    return 'off'
  }
}

// Once a load with a person there: a 2.1 Dynamic workflows on (workflows.json `mode: on`, no
// `deepmode`) starts 2.2 off, and `wb deepmode --moved` records it and says so once, the story's
// `moved.deepmode` line, toasted (#1507); the OpenCode plugin's jobs.ts does the same
async function deepmodeMoved($: any): Promise<void> {
  let doc: any = {}
  try { doc = JSON.parse(await $.fs.read(`${mem.root}/workflows.json`)) ?? {} } catch { return }
  if (doc?.mode !== 'on' || 'deepmode' in doc) return
  const { ok, out } = await runWb($, ['deepmode', '--moved'])
  if (ok && out !== '(no output)') $.ui.toast(`workbench: ${out}`) // nothing: another host said it
}

const SEND_ASK = 'Send it'
// Said after a no: it once read "Show them exactly what it sends and ask", and Claude opened a
// dialog of its own pleading for the yes the user had just refused (the review of #1005)
const NOT_SENT = 'workbench: not sent: the user did not approve it in the dialog. Tell them it was not ' +
  'sent; try again only if they ask for it.'
const NOT_SHOWN = (why: string) => `workbench: not sent: the dialog can't show what it sends (${why}). Run ` +
  'wb patch send as a command of its own, its --title written out and --body-file naming the file, so the ' +
  'user sees both.'


const BODY_LINES = 20


type Sending = { question: string; approval?: Row } | { deny: string }

// A commit's `Scope: me` trailer, said in the question: wb patch send refused it unless --anyway,
// a second yes after this dialog's (#1322). The branch's commits no remote-tracking ref has: those
// over upstream, as wb patch send counts them.
const SLUG_RE = /^[a-z0-9][a-z0-9-]*$/
const PERSONAL = 'A personal patch: a commit\'s Scope trailer is me.'
async function personal($: any, wt: string): Promise<boolean> {
  try {
    const r = await $.process.run(['git', '-C', wt, 'log', '--format=%(trailers:key=Scope,valueonly,separator=%x2C)',
      'HEAD', '--not', '--remotes', '--'], { timeoutMs: 5000 })
    return r.exitCode === 0 && String(r.stdout ?? '').split(/[\n,]/).some(x => x.trim().toLowerCase() === 'me')
  } catch {
    return false
  }
}

// The question: what goes public, word for word. For wb patch send its title and the body's
// start too, which the command line only names, and what the gate tells wb it approved; one
// whose title or body it can't read is refused, never asked about (#1005)
async function sendQuestion($: any, command: string, slug: string, aim: Aim, home: string): Promise<Sending> {
  const head = `Claude wants to send this to GitHub (${slug}), publicly: ${shownCommand(command, aim)}`
  if (aim.why !== 'send') return { question: `${head} — Send it?` }
  if (!aim.words.length) return { deny: NOT_SHOWN('it is inside text another program runs') }
  // wb patch send's own words, read by the kernel (Kernel.Gate's `send`), and what it lacks
  const args = gateOf(aim.words).send
  const lacks: Record<string, string> = { slug: 'no slug', title: 'no --title', bodyFile: 'no --body-file',
    shell: 'a $ or a backtick in --title or --body-file, whose value only the shell knows' }
  if (args.lack) return { deny: NOT_SHOWN(lacks[args.lack] ?? args.lack) }
  const path = absolutePath(args.bodyFile, home, aim.at)
  const text: string | undefined = await $.fs.read(path).catch(() => undefined)
  if (text === undefined) return { deny: NOT_SHOWN(`can't read ${path}`) }
  const lines = text.split('\n')
  const body = lines.slice(0, BODY_LINES).join('\n') + (lines.length > BODY_LINES ? '\n…' : '')
  const mine = SLUG_RE.test(args.slug) && await personal($, `${mem.root}/upstream/${args.slug}`)
  return {
    question: `${head}\n\n${mine ? `${PERSONAL}\n\n` : ''}Title: ${args.title}\n\nBody:\n${body}\n\nSend it?`,
    approval: { slug: args.slug, title: args.title, body_file: path, body: text, draft: args.draft, anyway: args.anyway },
  }
}

async function originOf($: any, dir: string): Promise<string> {
  try {
    const r = await $.process.run(['git', '-C', dir, 'remote', 'get-url', 'origin'], { timeoutMs: 5000 })
    return r.exitCode === 0 ? String(r.stdout ?? '') : ''
  } catch {
    return ''
  }
}

// A developer's clone of upstream (its origin the upstream), not a gated directory and not
// Claude Code's plugins directory: the marketplace install's own clone has that origin too, and
// was exempted as a developer's (the review of #1005's blocker). Asked of git only once all
// else matched.
async function developerClone($: any, dir: string, slug: string | string[], notIn: string[]): Promise<boolean> {
  if (notIn.some(d => inside(dir, d))) return false
  return originIs(await originOf($, dir), slug)
}

// Whether the signed-in GitHub user can push to ``slug`` (`gh api repos/<slug> --jq .permissions.push`,
// gh's word only, never its token): upstream's maintainer or a collaborator, whose own Claude's
// writes there are their own work, not a send upstream. The gate is for a user's Claude reaching
// the maintainer's repository; it held the owner's own Claude's issue comments from his clone
// until he clicked (2026-11-26: "it never should halt our work"). A yes is kept for the session, a
// no or no answer (no gh, no network) PUSH_RECHECK_MS, so a user's gated calls don't each wait on gh
const PUSH_RECHECK_MS = 10 * 60_000
async function canPush($: any, slug: string): Promise<boolean> {
  const now: number = await $.clock.now()
  const had = mem.pushable.get(slug)
  if (had && (had.ok || now - had.at < PUSH_RECHECK_MS)) return had.ok
  let ok = false
  try {
    const r = await $.process.run(['gh', 'api', `repos/${slug}`, '--jq', '.permissions.push'], { timeoutMs: 8000 })
    ok = r.exitCode === 0 && String(r.stdout ?? '').trim() === 'true'
  } catch { /* no gh: asked */ }
  mem.pushable.set(slug, { ok, at: now })
  return ok
}

// Whether the user can push to every name upstream answers to (gatedWhere's slugs: patches.json's
// upstream, UPSTREAM, its former names), in order, stopping at the first no. patches.json's
// upstream alone is a contributor's own fork when their clone tracks it, and they can push to
// that: their Claude's gh and MCP writes to the maintainer's repository went out unasked (#1549,
// 2026-11-26). A former name redirects, so gh answers it as it does UPSTREAM.
async function canPushAll($: any, slugs: string[]): Promise<boolean> {
  for (const slug of slugs) if (!await canPush($, slug)) return false
  return slugs.length > 0
}

// What the gate reads each time: patches.json's clone and upstream, the gated directories
type Gate = { home: string; cwd: string; slug: string; slugs: string[]; dirs: string[]; plugins: string[] }
async function gateConf($: any): Promise<Gate> {
  const conf = await readJson($, `${mem.root}/patches.json`)
  const home = (await $.env.get('HOME').catch(() => '')) || mem.home
  const cwd = (await $.session.cwd().catch(() => '')) || mem.cwd
  // patches.json's, else where wb patch init would put it (patches.source_dir); Claude Code's
  // plugins: the marketplace clone (the README's install) and the cache (shell.ts's gatedWhere)
  const w = gatedWhere({ patches: conf, home, cwd, root: mem.root, source: await $.env.get('WB_SOURCE').catch(() => ''),
    claudeConfig: await $.env.get('CLAUDE_CONFIG_DIR').catch(() => ''),
    pluginCache: await $.env.get('CLAUDE_CODE_PLUGIN_CACHE_DIR').catch(() => '') })
  return { home, cwd, ...w }
}

// The marketplace's full clone, which the mod runs wb from when no wb is on its PATH (#1190): a
// fresh install has none until `wb setup` links one, and Claude Code puts the plugin's bin/
// launcher on the Bash tool's PATH only, not the mod's. The same places in the same order as that
// launcher (mod/bin/wb), the clones gateConf counts: known_marketplaces.json's installLocation
// (a directory marketplace), then the clone under CLAUDE_CODE_PLUGIN_CACHE_DIR, then under
// CLAUDE_CONFIG_DIR or ~/.claude. '' when none holds a bin/wb.
async function marketplaceClone($: any, home: string, cwd: string): Promise<string> {
  const config = absolutePath((await $.env.get('CLAUDE_CONFIG_DIR').catch(() => '')) || `${home}/.claude`, home, cwd)
  const cacheDir = await $.env.get('CLAUDE_CODE_PLUGIN_CACHE_DIR').catch(() => '')
  const cache = cacheDir ? absolutePath(cacheDir, home, cwd) : ''
  const listed = (await readJson($, `${cache || `${config}/plugins`}/known_marketplaces.json`))?.workbench?.installLocation
  for (const dir of [typeof listed === 'string' ? listed : '', cache && `${cache}/marketplaces/workbench`, `${config}/plugins/marketplaces/workbench`]) {
    if (dir && await $.fs.stat(`${dir}/bin/wb`).catch(() => undefined)) return dir
  }
  return ''
}

// Whether a command of that name is in one of PATH's directories; null when PATH can't be read
async function onPath($: any, name: string): Promise<boolean | null> {
  const path: string = (await $.env.get('PATH').catch(() => '')) || ''
  if (!path) return null
  for (const dir of path.split(':')) {
    if (dir.startsWith('/') && await $.fs.stat(`${dir}/${name}`).catch(() => undefined)) return true
  }
  return false
}

// A fresh install (#1190), read from the task files so the mod keeps no state: no task ever ran
// (nothing under tasks/, no results.jsonl rows) and no role is on API. /workbench alone then starts
// the skill's *First run* at once: told to wait for a first request, a new user saw nothing happen.
async function freshInstall($: any): Promise<boolean> {
  if ((await $.fs.list(`${mem.root}/tasks`).catch(() => []))?.length) return false
  if ((await $.fs.stat(`${mem.root}/results.jsonl`).catch(() => undefined))?.size) return false
  return !(await readRoutes($)).api
}

// What a fresh install's /workbench alone carries after the skill's text; SKILL.md's *First
// task* reads it by this name
const FIRSTRUN = 'workbench-firstrun'
const FIRSTRUN_NOTE = `[${FIRSTRUN}] A fresh install: no task has run here yet and no role is on API. ` +
  'Run the skill\'s *First run* now, from its step 1, each install or download on the user\'s yes; ' +
  'don\'t wait for a first request.'

// `wb setup` on a session's start when the /workbench skill isn't linked (#1190): a fresh install,
// which `claude plugin install` alone leaves without it. It makes only links (and starts the
// optimize when due), what the user installed the plugin for: no question asked. A skill linked
// mid-session may not be listed until /reload-skills (Claude Code watches only the skill
// directories there at its start), so the mod runs that too, and says so if it can't.
async function setUp($: any, { ok, out, aborted }: WbRun) {
  if (aborted) return // a reload under way: the next session start runs it again
  if (!ok) {
    const bad = out.split('\n').find(l => l.startsWith('BAD ')) ?? out
    $.ui.toast(`workbench: wb setup failed: ${brief(bad)}`)
    return
  }
  const reloaded = await $.command.run({ command: 'reload-skills', args: '' }).then(() => true, () => false)
  $.ui.toast(`workbench: set up (wb and /workbench linked); ${reloaded ? '' : '/reload-skills, then '}type /workbench`)
}

// The consent dialog's answer, '' when $.ui.ask rejected: dismissed, a `-p` run, or a surface that
// couldn't draw it, each a no (the gates fail closed). Off the terminal the person may never have
// seen it (#1200), so a toast says where it does show; on the terminal alone a rejection is the
// person's own Esc, and nothing more is said. Away on the web chat, consent says there in one line
// that the dialog waits in the terminal (CONSENT_WAITING).
async function askOrNo($: any, question: string, options: string[], outcome: string): Promise<string> {
  try {
    return await $.ui.ask(question, options)
  } catch {
    if ([...mem.surfaces].some(s => s !== 'terminal'))
      $.ui.toast(`workbench: the question went unanswered, so ${outcome}. If it didn't show here, ` +
        'ask again from Claude Code in a terminal, where it does.')
    return ''
  }
}

// The dialog, or its refusal: '' lets the call through on exactly 'Send it'
async function askToSend($: any, question: string): Promise<string> {
  return (await consent($, question, SEND_ASK, "Don't send", 'nothing was sent')) ? '' : NOT_SENT
}

// A consent dialog (a send upstream, a role change, an edit of a task's code): the user's own
// click, in the terminal's dialog. The owner's rule (#1340, 2026-11-24): consent is answered in the
// terminal, locally or over ssh and tmux, never relayed to the web chat for answering; while the
// user is away on it the chat says in one line that Claude waits there (CONSENT_WAITING). #1322's
// first cut asked on the chat page: it cut a long question at the chat's 2000 characters unmarked,
// and a user back at the terminal had no dialog for up to 30 minutes; the refusal while away before
// it ("they approve it in the dialog once back") was a round trip for nothing. A dismissed dialog
// (an interrupt) rejects: no, and askOrNo says where it does show (#1200). ``outcome``: what a
// no leaves, as its toast says it.
const CONSENT_WAITING: string = SAY['refusal.consent']  // the story's (#1355)
async function consent($: any, question: string, yes: string, no: string, outcome: string): Promise<boolean> {
  if (await away($, await read($, chat))) await runWb($, ['chat', 'reply', `--text=${CONSENT_WAITING}`, '--to=user'], 15_000)
  // the page's state line says this one waits too (#1355); it is never a notice row (due never:
  // the reply above says it while away), and its answer, whatever it is, ends it
  const d: TermDialog = { tool: 'workbench', agent: '', input: {}, at: await $.clock.now(), due: Infinity, kind: 'consent',
    text: '', id: '', sending: false, done: false }
  mem.termDialogs.push(d)
  try {
    return (await askOrNo($, question, [yes, no], outcome)) === yes
  } finally {
    d.done = true
    mem.termDialogs = mem.termDialogs.filter(x => x !== d)
  }
}

const realPath = async ($: any, p: string): Promise<string> =>
  (await $.fs.stat(p, { resolve: true }).catch(() => undefined))?.realPath ?? p

// The deny for a Bash or Monitor command that would send to upstream without the user's yes now, else ''
async function upstreamRefusal($: any, command: string): Promise<string> {
  if (!/\b(?:gh|git|wb|workbench)\b/i.test(command)) return '' // most calls: no parse
  const g = await gateConf($)
  let aim = upstreamAim(command, g.cwd, g.home, g.slugs, g.dirs, mem.root, g.plugins)
  if (!aim.why && aim.outAt.length) { // a symlinked spelling of a gated directory: by real path
    const dirs = await Promise.all(g.dirs.map(d => realPath($, d)))
    for (const p of aim.outAt) {
      const real = await realPath($, p)
      if (real !== p && dirs.some(d => inside(real, d))) { aim = { ...aim, why: 'dir', at: p }; break }
    }
  }
  if (!aim.why) return ''
  if (aim.why === 'plugin' && !aim.named && !originIs(await originOf($, aim.at), g.slugs)) return ''
  if (aim.why === 'slug' && await developerClone($, aim.at, g.slugs, [...g.dirs, ...g.plugins])) return ''
  // the maintainer's own: never asked, but `wb patch send`, whose yes it checks (send-ok.json)
  if (aim.why !== 'send' && await canPushAll($, g.slugs)) return ''
  const q = await sendQuestion($, command, g.slug, aim, g.home)
  if ('deny' in q) return q.deny
  const no = await askToSend($, q.question)
  if (no || !q.approval) return no
  // wb patch send checks this under Claude Code: without it nothing asked the user (patches._approved)
  await $.fs.write(`${mem.root}/send-ok.json`, JSON.stringify({ ...q.approval, at: (await $.clock.now()) / 1000 })).catch(() => {})
  return ''
}


async function mcpRefusal($: any, tool: string, input: Row): Promise<string> {
  const g = await gateConf($)
  if (!mcpAimsUpstream(tool, input, g.slugs)) return ''
  if (await developerClone($, g.cwd, g.slugs, [...g.dirs, ...g.plugins])) return ''
  if (await canPushAll($, g.slugs)) return ''
  return askToSend($, `Claude wants to send this to GitHub (${g.slug}), publicly: ${tool} ${shownInput(input)} — Send it?`)
}

// Any tool call: Bash's and Monitor's command (Monitor runs its script in the same shell), a GitHub MCP tool's input
async function sendRefusal($: any, e: Row): Promise<string> {
  const tool = String(e.tool ?? '')
  if (tool === 'Bash' || tool === 'Monitor') return typeof e.command === 'string' ? upstreamRefusal($, e.command) : ''
  if (!GITHUB_MCP.test(tool)) return ''
  const { tool: _t, agentId: _a, ...input } = e
  return mcpRefusal($, tool, input)
}

// The roles' consent gate (#1014): which model a kind of work goes to is the user's choice, and
// Claude never changes the API side of it (a role's selector, its API model, thinking on API,
// the day cap) without the user's yes in this dialog, nor starts a task on an API model it
// picked (`wb start --model <API id>`). The dialog's text is what `wb route --dry-run --json`
// prints for that very command, so the user sees the change wb will make, word for word; on
// Allow the mod writes $WB_HOME/route-ok.json, the approval wb checks under Claude Code
// (routes.approved: one-shot, fresh for 120 s, bound to the canonical command and the line). A GPU
// model pin needs no dialog (wb's dry run says so); every change reaches the conversation as a
// [workbench] row (unsaid). As #1005's gate, read as bash reads it, `bash -c` and `eval` inside.
const ROUTE_OK = 'route-ok.json'
const ROUTE_ALLOW = 'Allow'
const ROLES = ['significant', 'trivial', 'writing'] as const
const NOT_CHANGED = 'workbench: not changed: the user did not approve it in the dialog. Tell them it was not ' +
  'changed; ask again only if they ask for it.'
const ROUTE_OWN = 'workbench: not changed: run wb route (or wb start --model) as a command of its own, one ' +
  'to a call, so the dialog shows exactly what changes.'
const ROUTE_OK_OWN = `workbench: not run: ${ROUTE_OK} is the mod's dialog's to write, on the user's yes; ` +
  'no command writes it.'
const ROUTES_EDIT = 'workbench: not edited: the roles change only through wb route, on the user\'s yes in the ' +
  'mod\'s dialog (wb route shows them).'
// a route change (a role's `wb settings set` row too, #1287) in an interpreter's code that runs
// commands (`spawns`): refused, as one can't be shown
const ROUTE_TEXT = new RegExp(String.raw`(?:\bwb|-m${W}workbench[\w.]*)${W}(?:route\b|settings${W}set\b|start\b[^\n]*[\s'",]--m)`, 'i')
// Said with the roles. Roles are settings, and Claude never changes one unasked (the owner's locked
// rule, 2026-11-24): #1322 tried "Run it when it is the right call", and it went back
const ROUTES_RULE = 'Never pass --model for an API model or change a role without the user\'s yes (the mod ' +
  'asks them in a dialog); recommend only.'

// Each wb route and each wb start with a --model (any abbreviation: --m…) in a command line, as
// the arguments `wb route --dry-run --json` takes for it: a route's own, or `--start` and the
// start's (parsed there by wb start's own parser); and each `wb settings set` (#1287), as
// `--settings` and its own, whose dry run is `wb settings set … --dry-run --json`. `hidden`: a
// route, a settings set or such a start inside text a shell is fed, or in an interpreter's code
// that runs commands, which can't be shown; a heredoc's or a quoted argument's text naming one is
// no call (#1364). `at`: the directory each call runs in, `cd` and
// pushd followed from ``cwd`` as upstreamAim follows them (a shell's fed ones aren't: those are
// hidden, #1364): the dry run ran in the session's
// directory, so `cd other && wb start … --model <API id>` (--repo defaults to `.`) was judged
// local-only, or not, by the wrong repo (#1125, 2026-11-23). '' where the mod can't say: without
// a ``cwd``, and after a cd to a word with `$` or a backquote in it (`cd "$REPO"`), to none (an
// unquoted `$(…)` leaves none) or to `-`; read as a literal path, it failed the dry run to spawn,
// so the call skipped the dialog into wb's "is the mod loaded?" (the review of #1125). An
// absolute one after it is known again. Which commands those are, and their dry runs' arguments,
// is the kernel's (Kernel.Gate: `route`, `start`, `settings`).
export function routeCalls(command: string, cwd = '', home = ''): { calls: string[][]; hidden: boolean; at: string[] } {
  const calls: string[][] = []
  const at: string[] = []
  let here = cwd
  let hidden = false
  for (const { fed, code, gate: g } of runs(command)) {
    const cmd = g.word
    if (g.gate === 'cd') {
      if (fed) continue
      const to = g.args[0]
      if (!to || to === '-' || /[$`]/.test(to)) here = ''
      else if (here || to.startsWith('/')) here = absolutePath(to, home, here)
      continue
    }
    if (code !== undefined && spawns(cmd, code) && ROUTE_TEXT.test(code)) hidden = true
    if (g.gate !== 'route' && g.gate !== 'start' && g.gate !== 'settings') continue
    if (fed) { hidden = true; continue }
    calls.push([...g.args])
    at.push(here)
  }
  return { calls, hidden, at }
}

// An interpreter's code that writes a file: Python's, Node's, Perl's, Ruby's, PHP's usual calls
const WRITES_CODE = new RegExp([
  // open(…, 'w') with the path built inside (`os.path.join(…)`, two levels deep), the mode a string
  // of its own: `open(os.path.join(home, NAME), 'w')` went through (the review of #1364)
  String.raw`\bopen\s*\((?:[^()]|\((?:[^()]|\([^()]*\))*\))*?['"][rbt]*[wax+][rbtwax+]*['"]`, String.raw`\.write_(?:text|bytes)\s*\(`, String.raw`\bjson\.dump\s*\(`,
  String.raw`\bshutil\.(?:copy\w*|move)\s*\(`, String.raw`\bos\.(?:rename|replace|link|symlink)\s*\(`,
  String.raw`\.(?:rename|symlink_to|hardlink_to|touch)\s*\(`, String.raw`\b(?:writeFile|appendFile|copyFile|rename)(?:Sync)?\s*\(`,
  String.raw`\bcreateWriteStream\s*\(`, String.raw`\bDeno\.write\w*`, String.raw`\bBun\.write\b`, String.raw`\bFile\.(?:write|open)\b`,
  String.raw`\bIO\.write\b`, String.raw`\bfile_put_contents\b`, String.raw`\bfopen\s*\([^)]*['"][wax]`,
  String.raw`\bopen\s*\(?\s*(?:my\s+)?\$?\w*\s*,\s*['"]\+?>`,
].join('|'))

// Whether a command line writes ``file`` (#1364): a redirect's target, tee's file, a copy's
// destination (its directory with a source of its name too) or dd's of=, a shell's fed commands'
// too (Kernel.Gate's `writes`); or an interpreter's code with a write call (WRITES_CODE) and the
// file in a string that is a path, the file's directory named (as written, `~`, $WB_HOME,
// `WB_HOME`) or the one it runs in.
// A word the mod can't resolve (another variable, a glob) is no write, and a mention never is: a
// gh issue create whose heredoc named route-ok.json was refused as writing it (2026-11-24).
export function writesTo(command: string, file: string, cwd: string, home: string, root: string): boolean {
  const name = file.slice(file.lastIndexOf('/') + 1)
  const dir = file.slice(0, file.lastIndexOf('/')) || '/'
  const resolve = (w: string, at: string): string => {
    const v = w.replace(/^\$(?:\{WB_HOME\}|WB_HOME\b)/, root).replace(/^\$(?:\{HOME\}|HOME\b)/, home)
    if (/[$`*?[]/.test(v) || (v.startsWith('~') && !home) || (!/^[/~]/.test(v) && !at)) return ''
    return absolutePath(v, home, at)
  }
  const base = (w: string) => w.replace(/\/+$/, '').split('/').pop()
  const esc = name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  // backquotes are strings only in JavaScript: in Python's text they are Markdown's (a docs edit
  // writing "`$WB_HOME/<the file>`" was refused as writing it: the review of #1364, 2026-11-25)
  const literal = (cmd: string) => new RegExp(/^(?:node|deno|bun)$/.test(cmd)
    ? `['"\`][^'"\`\\s]*\\b${esc}['"\`]` : `['"][^'"\\s]*\\b${esc}['"]`)
  let here = cwd
  let fedHere = cwd
  for (const run of runs(command)) {
    if (!run.fed) fedHere = here
    const at = run.fed ? fedHere : here
    const cmd = run.gate.word
    if (run.gate.gate === 'cd') {
      const to = run.gate.args[0]
      const p = to === undefined ? home : to === '-' ? '' : resolve(to, at)
      if (run.fed) fedHere = p
      else here = p
      continue
    }
    for (const t of run.gate.writes) {
      const p = resolve(t.to, at)
      if (p === file || (p === dir && t.names.some(n => base(n) === name))) return true
    }
    if (run.code !== undefined && literal(cmd).test(run.code) && WRITES_CODE.test(run.code) &&
      (mentions(run.code, dir, home, root) || (dir === root && /\bWB_HOME\b/.test(run.code)) || at === dir)) return true
  }
  return false
}

const routesHome = async ($: any) => (await $.env.get('HOME').catch(() => '')) || mem.home

// routes.json: $WB_ROUTES, else ~/.config/workbench/routes.json (routes.path)
async function routesPath($: any): Promise<string> {
  const home = await routesHome($)
  const cwd = (await $.session.cwd().catch(() => '')) || mem.cwd
  const given = await $.env.get('WB_ROUTES').catch(() => '')
  return given ? absolutePath(given, home, cwd) : `${home}/.config/workbench/routes.json`
}

const money = (v: number) => Number.isInteger(v) ? `$${v}` : `$${v.toFixed(2)}`

// What a role's thinking does on its API model, as routes.thinking_said says it: the row's `thinks`
// (wb writes it with every change, from the catalog) for the setting, else the setting. `thinking off`
// was said for a model that can't turn it off (#1263, 2026-11-24). A role's `thinking_review` on (#1059):
// REVIEW_ONLY, with the thinking variant's effort, where its coding rounds don't think (routes.role_thinks)
const REVIEW_ONLY = 'thinking: review only'
export function thinkingSaid(row: Row): string {
  const thinking = row.thinking === 'on' ? 'on' : 'off'
  const t = row.thinks
  const said = (k: string) => t && typeof t === 'object' && t.api === row.api && typeof t[k] === 'string' ? t[k] : `thinking ${k}`
  const text = said(thinking)
  if (row.thinking_review === 'on' && text === 'thinking off' && said('on').startsWith('thinking on'))
    return REVIEW_ONLY + said('on').slice('thinking on'.length)
  return text
}

// The roles in one line, as routes.summary says it (Couplings: the two read alike)
export function routesSummary(doc: Row): string {
  const rows = (doc.roles ?? {}) as Record<string, Row>
  const roles = ROLES.map(r => {
    const row = rows[r] ?? {}
    const gpu = typeof row.gpu === 'string' && row.gpu ? row.gpu : 'auto'
    const thinking = row.thinking === 'on' ? 'on' : 'off'
    // an OpenRouter role's backends (routes.json `order`): where its code goes, so a change of them
    // alone is a row too; it reached the conversation as nothing (#1126, 2026-11-23)
    const backends = row.api && Array.isArray(row.order) && row.order.length ? `, backends ${row.order.join(', ')}` : ''
    // the role's fallback (routes.json `fallback`, #1196): where its code may go too, so a change of it alone is a row
    const fallback = typeof row.fallback === 'string' && row.fallback ? `, fallback ${row.fallback === 'gpu' ? 'GPU' : row.fallback}` : ''
    if (row.via === 'api') return `${r} API ${row.api}${backends}, ${thinkingSaid(row)}${fallback}`
    let text = `${r} GPU ${gpu === 'auto' ? '(the router picks)' : gpu}`
    if (row.api) text += `, API kept: ${row.api}${backends}`
    if (thinking === 'on') text += ', thinking on'
    else if (row.thinking_review === 'on') text += `, ${REVIEW_ONLY}`
    return text + fallback
  })
  // the Agents switch (#1016): routes.json `use`, over each role's own while it isn't Both
  const use = doc.use === 'gpu' ? 'GPU' : doc.use === 'api' ? 'API' : ''
  // the busy opt-in (#1023): said, or a change of it alone would never reach the conversation
  const busy = doc.busy === 'api' ? '; when the GPU is busy, a role with an API model runs on it' : ''
  return (use ? `Agents ${use}, over each role's own: ` : '') + roles.join('; ') + busy
}

// A document wb would read (routes.normalize's checks, in short): else it can't be read. The
// OpenCode path's top-level `driver` (#1316) is left unread on purpose: normalize reads any value
// that isn't an OpenCode id as no pin, never as an unreadable file, so neither does this
function routesValid(doc: unknown): doc is Row {
  if (!doc || typeof doc !== 'object' || Array.isArray(doc)) return false
  const d = doc as Row
  if (d.day_usd != null && !(typeof d.day_usd === 'number' && d.day_usd > 0)) return false
  if (![undefined, 'gpu', 'api', 'both'].includes(d.use)) return false
  if (![undefined, 'wait', 'api'].includes(d.busy)) return false
  if (d.roles === undefined) return true
  if (!d.roles || typeof d.roles !== 'object' || Array.isArray(d.roles)) return false
  return Object.entries(d.roles as Row).every(([r, row]: [string, any]) => (ROLES as readonly string[]).includes(r) &&
    row && typeof row === 'object' && [undefined, 'gpu', 'api'].includes(row.via) &&
    [undefined, 'off', 'on'].includes(row.thinking) && [undefined, 'off', 'on'].includes(row.thinking_review) &&
    (row.via !== 'api' || (typeof row.api === 'string' && !!row.api)) &&
    (row.order == null || (Array.isArray(row.order) && row.order.every((b: unknown) => typeof b === 'string' && !!b.trim()))) &&
    (row.fallback == null || (typeof row.fallback === 'string' && !!row.fallback.trim() && row.fallback !== 'none')))
}

// What the conversation is told of the roles: `said`, their summary and the day cap ('. Day cap
// $N'), '' while every role is the default and no cap is set (nothing to tell); `api`, whether any
// role has an API model. The cap is in `said`, which is what a change is found by: a cap changed
// alone (a terminal's wb route cap, a hand edit) never reached the conversation (2026-11-23).
// `web`: the newest change a role records was the web app's (`by`/`gpu_by` "web", #1283); `stamp`:
// that change's time (s), so a later change that stamps no role (the day cap, Agents, busy) isn't
// credited to it
type Routes = { said: string; api: boolean; cap: number | null; web?: boolean; stamp?: number }
async function readRoutes($: any): Promise<Routes> {
  const text: string | undefined = await $.fs.read(await routesPath($)).catch(() => undefined)
  if (text === undefined) return { said: '', api: false, cap: null }
  let doc: unknown
  try {
    doc = JSON.parse(text)
  } catch {
    doc = null
  }
  if (!routesValid(doc)) return { said: 'routes.json can\'t be read (wb route says why)', api: false, cap: null }
  const rows = Object.values((doc.roles ?? {}) as Record<string, Row>)
  const cap = typeof doc.day_usd === 'number' ? doc.day_usd : null
  const plain = cap === null && (doc.use ?? 'both') === 'both' && (doc.busy ?? 'wait') === 'wait' && rows.every(r => (r.via ?? 'gpu') === 'gpu' && (r.gpu ?? 'auto') === 'auto' &&
    !r.api && (r.thinking ?? 'off') === 'off' && (r.thinking_review ?? 'off') === 'off' && !r.fallback)
  let newest = -Infinity, by = ''
  for (const r of rows) {
    for (const [ts, who] of [[r.ts, r.by], [r.gpu_ts, r.gpu_by]]) {
      if (typeof ts === 'number' && ts > newest) [newest, by] = [ts, String(who ?? '')]
    }
  }
  return { said: plain ? '' : routesSummary(doc) + (cap !== null ? `. Day cap ${money(cap)}` : ''), api: rows.some(r => !!r.api), cap,
    web: by === 'web', stamp: newest > -Infinity ? newest : undefined }
}
const DEFAULT_ROUTES = routesSummary({})
const ROUTES_FRESH_S = 120 // a role stamp this new, with none told this load, is this change's
const ROUTES_NOTE = (r: Routes) => `Roles (the user's, wb route): ${r.said}. ` + (r.cap !== null ? '' : 'No day cap yet. ') +
  ROUTES_RULE
const ROUTES_CHANGED = (said: string, web = false) =>
  `The roles changed: ${said || DEFAULT_ROUTES}${web ? ' (the user, on the web app)' : ''}. ${ROUTES_RULE}`

// The deny for an edit of routes.json or route-ok.json with the file tools, else ''
async function routesFileRefusal($: any, path: string): Promise<string> {
  const home = await routesHome($)
  const cwd = (await $.session.cwd().catch(() => '')) || mem.cwd
  const abs = absolutePath(path, home, cwd)
  const gated = [await routesPath($), `${mem.root}/${ROUTE_OK}`]
  if (gated.includes(abs)) return ROUTES_EDIT
  const real = await realPath($, abs)
  return real !== abs && (await Promise.all(gated.map(g => realPath($, g)))).includes(real) ? ROUTES_EDIT : ''
}

// A call's dry run when it prints a gated change (its `change` line and `approval`), else null: a
// GPU model pin or nothing gated, a refusal (wb refuses the change itself, saying why), or no dry
// run (wb itself refuses a gated change under Claude Code without route-ok.json)
async function routeDry($: any, args: string[], cwd: string): Promise<Row | null> {
  try {
    // a start's in the directory it runs in: wb start's --repo is `.` unless given (#1125). Only a
    // start's (wb route doesn't read its directory), and only one that exists: a directory the
    // line makes (`mkdir x && cd x`) failed the dry run to spawn, and the call skipped the
    // dialog; the session's is the better guess (the review of #1125, 2026-11-23)
    const there = args[0] === '--start' && cwd && await $.fs.exists(cwd).catch(() => false) ? { cwd } : {}
    // a Settings row's (#1287): `wb settings set <key> <value> --dry-run --json` is its wb route's
    // dry run, or no gate for a row that isn't a role's
    const dry = args[0] === '--settings' ? ['settings', 'set', ...args.slice(1), '--dry-run', '--json']
      : ['route', '--dry-run', '--json', ...args]
    const r = await $.process.run([mem.wb, ...dry], { timeoutMs: 60_000, ...there })
    if (r.exitCode !== 0) return null
    const q = JSON.parse(String(r.stdout ?? ''))
    return q?.gate && typeof q.change === 'string' && q.change ? q : null
  } catch {
    return null
  }
}

// The deny for a Bash or Monitor command that changes a role, or starts a task on an API model,
// without the user's yes in the dialog now, else ''
async function routeRefusal($: any, e: Row): Promise<string> {
  const tool = String(e.tool ?? '')
  if ((tool !== 'Bash' && tool !== 'Monitor') || typeof e.command !== 'string') return ''
  const command = e.command
  if (!/\b(?:route|start|settings)\b|route-ok\.json/i.test(command)) return '' // most calls: no parse
  const cwd = (await $.session.cwd().catch(() => '')) || mem.cwd
  if (command.includes(ROUTE_OK) && writesTo(command, `${mem.root}/${ROUTE_OK}`, cwd, await routesHome($), mem.root)) return ROUTE_OK_OWN
  const { calls, hidden, at } = routeCalls(command, cwd, await routesHome($))
  if (hidden) return ROUTE_OWN
  // a show (`wb route`, `--json`) and a dry run change nothing
  const live = calls.flatMap((c, i) => !c.includes('--dry-run') && (c[0] === '--start' || c.some(a => a !== '--json'))
    ? [{ args: c, cwd: at[i] }] : [])
  if (!live.length) return ''
  // each call's dry run: only a gated change needs a call of its own. Two Settings rows nothing
  // gates (`coding ask` and `splash off` in one line) were refused unasked, though only what
  // wb route gates waits for the dialog (the review of #1287, 2026-11-24)
  const gated: Row[] = []
  for (const { args, cwd } of live) {
    const q = await routeDry($, args, cwd)
    if (q) gated.push(q)
  }
  if (!gated.length) return '' // a GPU model pin, nothing, or wb refuses it itself
  if (live.length > 1) return ROUTE_OWN // a gated change beside another call: the dialog shows one
  const q = gated[0]!
  if (!(await consent($, q.change, ROUTE_ALLOW, "Don't", 'nothing changed'))) return NOT_CHANGED
  await $.fs.write(`${mem.root}/${ROUTE_OK}`, JSON.stringify({ ...q.approval, at: (await $.clock.now()) / 1000 })).catch(() => {})
  return ''
}

// The file tools' path fields: an edit in a task's worktree is writing that task's code (Kernel.Coding's
// editTools, Claude Code's rows)
const EDIT_FIELDS = editFields('claude-code')

async function codingMode($: any): Promise<string> {
  try {
    const mode = JSON.parse(await $.fs.read(`${mem.root}/coding.json`)).mode
    return mode in CODING_RULES ? mode : 'decide'
  } catch {
    return 'decide'
  }
}

// Each repo commands.json gives a `local_only`, with its value (#1021), or null when the text isn't JSON
function localOnlyMarks(text: string): Map<string, unknown> | null {
  try {
    const repos = JSON.parse(text)?.repos
    if (!repos || typeof repos !== 'object') return new Map()
    return new Map(Object.entries(repos).filter(([, v]: [string, any]) => v && typeof v === 'object' && 'local_only' in v)
      .map(([k, v]: [string, any]) => [k, v.local_only]))
  } catch {
    return null
  }
}

// The text an Edit, MultiEdit or Write would leave in the file, from its text now
function editedText(e: Row, now: string): string {
  if (e.tool === 'Write') return String(e.content ?? '')
  const edits = e.tool === 'MultiEdit' && Array.isArray(e.edits) ? e.edits : [e]
  let text = now
  for (const ed of edits) {
    const from = String(ed.old_string ?? ''), to = String(ed.new_string ?? '')
    text = ed.replace_all ? text.split(from).join(to) : text.replace(from, () => to)
  }
  return text
}

const marksOf = (text: string, value: string) => (text.match(new RegExp(`"local_only"\\s*:\\s*${value}\\b`, 'g')) ?? []).length

// Why Claude's edit of commands.json is refused, or '': only the user lifts a repo's local_only
// (#1021). Refused: an edit that drops a `"local_only": true`, and one that writes a `false` the
// text didn't have: a package's `false` under a local-only root lifted it while the nearest entry
// decided (#1021's review); since #1255 any entry's `true` holds for its whole repo, and the
// guard stays, so Claude never writes the user's local_only either way. A text that isn't JSON
// counts the marks instead. The file is matched by real path, so another spelling of it (`/./`,
// a linked config directory) is the same file. A shell's write isn't seen here, as with
// routes.json (#1014's gate).
async function localOnlyRefusal($: any, e: Row, path: string): Promise<string> {
  if (!mem.commandsFile) return ''
  const file = await realPath($, mem.commandsFile)
  if (absolutePath(path, '', mem.cwd) !== file && await realPath($, absolutePath(path, '', mem.cwd)) !== file) return ''
  const now = await $.fs.read(file).catch(() => '')
  const text = editedText(e, now)
  const before = localOnlyMarks(now), after = localOnlyMarks(text)
  let dropped: string[], lifted: string[]
  if (before && after) {
    dropped = [...before].filter(([k, v]) => v === true && after.get(k) !== true).map(([k]) => k)
    lifted = [...after].filter(([k, v]) => v !== true && before.get(k) !== v && !dropped.includes(k)).map(([k]) => k)
  } else {  // not JSON before or after: by the marks' count
    const named = before ? [...before].filter(([, v]) => v === true).map(([k]) => k) : ['a repo']
    dropped = marksOf(text, 'true') < marksOf(now, 'true') ? named : []
    lifted = marksOf(text, 'false') > marksOf(now, 'false') ? ['a repo'] : []
  }
  const why = [...(dropped.length ? [`drops "local_only": true for ${dropped.join(', ')}`] : []),
    ...(lifted.length ? [`sets "local_only" to something other than true for ${lifted.join(', ')}`] : [])]
  if (!why.length) return ''
  return `workbench: not changed: this edit ${why.join(' and ')} in commands.json. ` +
    'Only the user lifts a repo\'s local_only (it keeps API models off that repo); tell them, and leave it as it is.'
}

// The task whose worktree holds the file, else '': tasks/<t>/worktree, or tasks/<t>/tree/…/worktree
// for a sibling further up (#261)
export function taskOfPath(path: string, root: string): string {
  if (!root || !path.startsWith(`${root}/tasks/`)) return ''
  return /^([^/]+)\/(?:tree\/(?:[^/]+\/)+)?worktree\//.exec(path.slice(root.length + 7))?.[1] ?? ''
}

// The tasks a shell command runs in (`cwd` inside tasks/<t>/) or names the directory of
// (tasks/<t>/ under one of `roots`, as written, through `~` or $HOME, or $WB_HOME): which tasks'
// rows a Bash call of Claude's goes in (callRows, #1211)
export function tasksNamed(command: string, cwd: string, roots: string[], homes: string[]): string[] {
  const out = new Set<string>()
  const esc = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const ways = new Set(['$WB_HOME', '${WB_HOME}'])
  for (const root of roots.filter(Boolean)) {
    ways.add(root)
    for (const home of homes.filter(h => h && inside(root, h) && root !== h)) {
      const rest = root.slice(home.length)
      for (const w of [`~${rest}`, `$HOME${rest}`, `\${HOME}${rest}`]) ways.add(w)
    }
    if (cwd.startsWith(`${root}/tasks/`)) {
      const at = /^([a-z0-9][a-z0-9-]*)(?:\/|$)/.exec(cwd.slice(root.length + 7))?.[1]
      if (at) out.add(at)
    }
  }
  if (!command.includes('/tasks/')) return [...out]
  for (const w of ways) {
    for (const m of command.matchAll(new RegExp(`${esc(w)}/tasks/([a-z0-9][a-z0-9-]*)(?=$|[/\\s'"\`;&|)])`, 'g'))) out.add(m[1])
  }
  return [...out]
}

// Claude's own tool call in a task's worktree, kept for wb as one row a call in
// rules/<task>/claude-calls/ (runner.CLAUDE_CALLS): an edit tool's file there, or a Bash command
// that runs in a task's directory or names it. wb tells by them what Claude wrote by hand in the
// worktree from what a build or test command it ran there wrote: `mvn test` beside a --test of
// grep, tsc beside jest's, wrote reports and dist/ that wb accept and wb next refused as files
// written by hand (#1211, 2026-11-24). The tasks it is for (with a task.json) and the row
// without its times; no tasks for any other call.
async function callRows($: any, e: Row): Promise<{ tasks: string[]; row: Row }> {
  const none = { tasks: [] as string[], row: {} }
  if (!mem.root) return none
  const field = EDIT_FIELDS[e.tool]
  let tasks: string[] = [], row: Row
  if (field) {
    const path = e[field]
    if (typeof path !== 'string' || !path.includes('/tasks/')) return none
    const real = await realPath($, mem.root)
    const t = taskOfPath(path, mem.root) || taskOfPath(path, real)
    tasks = t ? [t] : []
    row = { tool: e.tool, path }
  } else if (e.tool === 'Bash' && typeof e.command === 'string') {
    const cwd = (await $.session.cwd().catch(() => '')) || mem.cwd
    if (!e.command.includes('/tasks/') && !cwd.includes('/tasks/')) return none // most calls: no stat
    const home = (await $.env.get('HOME').catch(() => '')) || ''
    tasks = tasksNamed(e.command, cwd, [mem.root, await realPath($, mem.root)], [home, mem.home])
    row = { tool: 'Bash', command: e.command, cwd }
  } else return none
  const found: string[] = []
  for (const t of tasks) if (await $.fs.exists(`${mem.root}/tasks/${t}/task.json`).catch(() => false)) found.push(t)
  return { tasks: found, row }
}

// The file of a call's row: one file a call, as a row in a shared file would be lost to a
// parallel call's write
function callFile(at: number, id: string): string {
  return `${Math.round(at)}-${id.replace(/[^A-Za-z0-9_-]/g, '') || Math.random().toString(36).slice(2)}.json`
}

// The row callRows made, with when the call began (`at`, ms) and when it ended: written as it
// begins with `end` null (running), then again once it is over, with `end`, or `denied` if it
// never ran. A build and `wb accept` in one Bash call, as the skill says, had no row while accept
// read them, and the build's dist/ was refused as files written by hand (#1272, 2026-11-24)
async function noteCall($: any, tasks: string[], row: Row, at: number, name: string,
                        how: 'begun' | 'over' | 'denied') {
  const end = how === 'begun' ? null : (await $.clock.now()) / 1000
  const text = JSON.stringify({ ...row, at: at / 1000, end, ...(how === 'denied' ? { denied: true } : {}) })
  for (const t of tasks) await $.fs.write(`${mem.root}/rules/${t}/claude-calls/${name}`, text).catch(() => {})
}

// Why Claude may not make this edit now, or '': outside a task's worktree, under "decide", or
// with the user's yes recorded for that task and not yet handed back (`wb coding allow`). Under
// "Ask me first" the first such edit asks the user in a dialog (consent), and Allow records the
// allowance, as the roles' dialog writes route-ok.json (#1322).
const CODING_ALLOW = 'Allow'
const CODING_SAID = 'Allow, in the workbench dialog'
const NOT_EDITED = (task: string) => `workbench: not edited: the user said no to editing ${task}'s code. ` +
  'Keep the work with the agent; ask again only if they ask for it.'
async function codingRefusal($: any, path: string): Promise<string> {
  const task = taskOfPath(path, mem.root)
  if (!task) return ''
  const mode = await codingMode($)
  const file = `${mem.root}/tasks/${task}/coding.json`
  let allowed: Row[] = []
  try {
    const rows = JSON.parse(await $.fs.read(file)).allowed
    allowed = Array.isArray(rows) ? rows.filter(x => x && typeof x === 'object') : []
  } catch {}
  // the rules are the kernel's (Kernel.Coding), so both hosts gate an edit alike
  const { verdict, open } = codingGate({ mode, allowed: allowed.map(x => ({ used: !!x.used, when: !!x.when, started: !!x.started })) })
  if (verdict === 'start') {
    // a one-task exception (#94) is under way from its first edit: the next hand-back spends it
    allowed[open]!.started = (await $.clock.now()) / 1000
    await $.fs.write(file, JSON.stringify({ allowed })).catch(() => {})
  }
  if (verdict === 'pass' || verdict === 'start') return ''
  if (verdict === 'ask') {
    if (!(await consent($, `Let Claude edit ${task}'s code now?`, CODING_ALLOW, "Don't", 'nothing was edited'))) return NOT_EDITED(task)
    await runWb($, ['coding', 'allow', task, `--said=${CODING_SAID}`], 15_000)
    return ''
  }
  return `workbench: not edited: ${task} is the agent's. ${CODING_RULES[mode]}`
}
// Tasks of another session are listed as left alone: theirs to stop or finish.
const QUIT_NOTE = (out: string, ok: boolean) =>
  `[workbench] The user ran /${QUIT}: ` + (ok
    ? `this session's delegated agents were stopped (\`wb stop --all\`):\n`
    : `but \`wb stop --all\` failed, so its agents may still be running (\`wb status\`):\n`) +
  `${out}\nWorkbench mode is off: from now on work directly, without delegating. Don't finish, ` +
  `restart or clean up any of the stopped work unless they ask; real tasks keep their ` +
  `worktrees (\`wb status --all\`). Tasks left alone belong to another session.`
// a hand-back that took nothing over (#1550): how the user hands Claude one parked task
const HAND_ONE = 'To hand a parked task to Claude, ask for it by name (`wb takeover <name>`; `wb status --all` lists them).'
const DONE_NOTE = (out: string) =>
  `[workbench] The user ran /${HAND_BACK}: this session's agents were stopped and their tasks ` +
  `handed to you (\`wb takeover --all\`):\n${out}\n\nLeave workbench mode and finish the work ` +
  `yourself, without pausing to ask. For each task taken over: read what the agent left (\`wb diff <name>\`), ` +
  `complete it in its worktree to your own standard, run its acceptance command, then ` +
  `\`wb accept <name> -m "…"\` (add --merge only if the user asked for merging). Tasks left ` +
  `alone belong to another session: don't touch them unless the user asks. Then carry on ` +
  `with the rest of the user's request directly, without delegating, and report briefly at the end.`
// With the plugin's wake setting off, no [workbench] prompt comes: Claude must not end its turn
// waiting for one (the 2026-11-16 audit).
const WAKE_OFF_NOTE =
  'Wakes are off in this session (the workbench plugin\'s wake setting): no [workbench] prompt ' +
  'will say when a task needs you. After `wb start`, tell the user it runs and that a toast says ' +
  'when it is ready; pick it up when they ask (`wb report <name>`), and wait (`wb wait <name>`) ' +
  'only if they ask you to.'

// Back to "Let Claude decide" after a rule the conversation was given (#540)
const CODING_DECIDE = 'Claude coding is now "Let Claude decide" (the user\'s choice; `wb coding` shows it): ' +
  'the earlier Claude coding rule no longer applies; work as the /workbench skill says.'

// What this conversation was told of the rules that depend on the state (#540): the user's notes,
// that wakes are off, the Claude-coding rule. prompt.context fires once per conversation, at its
// first message (again after a compaction or /clear), and a block the engine doesn't route can't
// be added later: a session that entered workbench mode afterwards got no notes and no wake-off
// note, and a coding change from the pane reached only the file-tool gates.
type Said = WbSaid
// Kept across a reload within the session (#697): in `mem` alone a reload (a plugin option
// changed, /workbench-update, /reload-plugins) emptied it, and since prompt.context doesn't fire
// again in that conversation, sayRules waited for it for good: turning wakes off mid-session
// never said WAKE_OFF_NOTE, and Claude ended its turn waiting for a wake that would not come.
// A conversation's first context block (a /clear's, a compaction's) sets it again.
const saidState = atom({ plugin: 'workbench', key: 'said' } as const, null as WbSaid | null, { shape: 'said-3' })
async function setSaid($: any, said: Said) {
  mem.said = said
  await update($, saidState, () => said).catch(() => {}) // mem has it for this load either way
}

async function wanted($: any, delegating: boolean, notesToo = true): Promise<Said> {
  const notes = delegating && notesToo ? (await $.fs.read(`${mem.root}/notes.md`).catch(() => '')).trim() : ''
  const any = delegating || mem.open.size > 0
  return { notes, wakeOff: !mem.wake && any, coding: any ? await codingMode($) : 'decide',
    routes: any ? (await readRoutes($)).said : '', workflows: any ? await workflowsMode($) : 'off' }
}

// The rules this conversation lacks now, as text, counted as said. Notes come once (Claude adds
// them itself, `wb notes add`); the coding rule again whenever it changes. Without a context block
// answered in this load (a reload), nothing is known of what it holds: it is all still to say.
async function unsaid($: any, delegating: boolean): Promise<string[]> {
  const want = await wanted($, delegating, !mem.said?.notes) // said once: not read at every poll
  const was = mem.said ?? { notes: '', wakeOff: false, coding: 'decide', routes: '', workflows: 'off' } // after the await: no other caller in between
  const any = delegating || mem.open.size > 0 // out of the mode with nothing open: nothing changes
  const plan = tellPlan({ first: false, want, was, known: true, any, delegating, away: false })
  const out: string[] = []
  for (const say of plan.say) {
    if (say === 'notes') out.push(NOTES_NOTE(want.notes))
    else if (say === 'wakeOff') out.push(WAKE_OFF_NOTE)
    else if (say === 'coding') out.push(want.coding === 'decide' ? CODING_DECIDE : CODING_RULES[want.coding])
    else if (say === 'workflows') out.push(want.workflows === 'on' ? WORKFLOWS_RULE : WORKFLOWS_OFF) // #1041
    else { // the roles (#1014): any change, however made (the dialog, the web app, a terminal, a hand edit); the web app's says so (#1283)
      const now = await readRoutes($)
      // the web app's only when this change stamped a role there: newer than the stamp last told, or
      // (none told this load) made since this poll's window began (review of #1283, 2026-11-25: a day
      // cap set in the terminal after a role last set on the web app read "(the user, on the web app)")
      const since = mem.routesStamp ?? (await $.clock.now()) / 1000 - ROUTES_FRESH_S
      out.push(say === 'routesChanged' ? ROUTES_CHANGED(want.routes, !!now.web && (now.stamp ?? 0) > since) : ROUTES_NOTE(now))
      mem.routesStamp = Math.max(now.stamp ?? since, since)
    }
  }
  await setSaid($, plan.said)
  return out
}

// Mid-conversation, a rule the conversation lacks goes in as a row the model reads, starting no
// turn; refused (an older Claude Code, a policy), a turn of its own.
async function sayRules($: any, delegating: boolean) {
  if (!mem.said) return // no context answered yet: the first message's blocks will say it
  const lines = await unsaid($, delegating)
  if (!lines.length) return
  const text = `[workbench] ${lines.join('\n\n')}`
  const r = await $.session.append({ message: { type: 'user', content: [{ type: 'text', text }] } })
    .catch((err: unknown) => ({ deny: String(err) }))
  if (r?.deny) mem.queued.push(`${text}\n\nNothing to do now; reply in one line.`)
}

// The module's own memory: reset by a reload, which runs register and session.start again.
const mem = {
  flows: new Map<string, FlowCommand>(), // the flows' slash commands, registered at session.start (#1295)
  pushable: new Map<string, { ok: boolean; at: number }>(), // canPush's answers by slug
  wake: true,
  root: '',
  startedAt: 0,
  lastDrawn: '',
  lastNotify: '-', // notify.jsonl's size and mtime when last read
  notifyRows: { key: '', rows: [] as NoticeRow[] }, // its rows as last parsed, under that key (#921)
  paneOpened: false,
  polling: false,
  watching: false,
  picked: '', // the task whose row the user pressed: followed while listed (#1048)
  rolesReading: false, // a rolesNow for the Tasks tab's day-cap line under way (capNow)
  wb: 'wb',
  surfaces: new Set<string>(), // where this session is drawn: session.start's, then each attach's (#1200)
  liveKey: '', // the followed task and its updated_at when last read
  liveAt: 0, // when its transcript was last read
  awayNoted: false, // AWAY_NOTE said in full this away stretch (awayToo)
  roles: null as null | PaneRoles, // the roles as wb last said them, for the day-cap line (rolesNow, #1023)
  cwd: '', // the session's directory, resolved: which old tasks are this session's (mine)
  home: '', // the home directory, resolved: a session there claims no task by directory
  ids: new Set<string>(), // the `sessions` atom: which tasks are this session's (mine)
  deadPids: new Set<string>(), // Claude Code processes tasks recorded that are gone (pid@start): final (mine)
  pidAt: new Map<string, number>(), // when a live one was last checked (noteClaude)
  person: false, // someone can be at this session (not a bare `claude -p` or SDK run)
  started: false, // the ears and timers run
  busy: false, // Claude's main loop is mid-turn: a wake would queue and arrive stale
  queued: [] as string[], // turns for Claude that a command asked for
  deepcodeRule: '', // `wb deepmode --rule`, read at the first deepcode (deepcodeRule)
  queuedRetryAt: 0, // a queued turn the engine dropped is tried again from then
  open: new Set<string>(), // this session's delegated tasks open at the last poll
  heldAsks: new Set<string>(), // questions (task:ts) held for a turn that could not take them (#542)
  said: null as Said | null, // what the conversation was told of the state's rules (#540); null: no context yet
  skillTurn: false, // /workbench's text said them in this turn: the context block computed after it doesn't
  delegated: false, // workbench mode was on in this load (a hand-back's compaction still keeps the state)
  turnEndedAt: 0, // when Claude's last main-loop turn ended
  follow: true, // the pane sticks to the newest lines until the person scrolls up
  sawTurn: false, // the person has used this session: a weekly check may follow
  scoutAsked: false, // the weekly model check was raised in this load
  updateAsked: false, // `wb update --check --if-due` ran in this load (wb keeps it weekly)
  optimizeAsked: false, // `wb optimize start --if-due` ran in this load (wb says when it's due)
  boundary: false, // one closed since: a moment to make room in Claude's context
  toasted: new Set<string>(),
  noticed: new Map<string, number>(), // each task's newest notice (its ts), as deliver last read
  dropped: new Map<string, number>(), // a task whose wake the engine dropped, and when: rewake retries after a minute
  warned: new Set<string>(), // one-time toasts already shown (a file unreadable, wb failing)
  chatSent: new Set<string>(), // web messages submitted by this load, awaiting their receipt
  chatRetry: new Map<string, number>(), // a web message whose prompt was dropped: tried again from then
  chatExpired: new Set<string>(), // web messages wb said are too old to pass on (#536): not asked again
  chatQueued: new Map<string, { by: string; how: string }>(), // web messages whose receipt says queued: acked `how` once a turn ends
  chatOwed: new Map<string, Owed>(), // web messages passed to Claude with no reply in the chat yet (#767)
  chatTurn: new Map<string, number>(), // web messages given to Claude: turn ends to skip before theirs (#1042)
  chatTurnSize: Promise.resolve(-1), // messages.jsonl's size when the turn started, once read: grown, it may hold a reply (#1042)
  listenerAt: new Map<string, number>(), // another session's chat listener (pid@start), when last seen alive (#839)
  submitted: false, // this mod submitted a prompt while Claude was idle, and no turn has started since (#837)
  chatChecking: false, // a `wb chat url --json` under way (chatLink)
  chatAgain: false, // asked for again meanwhile: once more after it
  switchKey: '', // chat/switch.json as last read: changed (`wb chat on|off`), the link is looked up now
  settingsKey: '', // settings-changes.jsonl's size@mtime when last read (settingsTurns)
  settingsSeen: new Set<string>(), // its rows this load delivered (ts:key)
  routesStamp: undefined as number | undefined, // the newest role stamp when the roles were last told (unsaid)
  eventsPid: 0, // the `wb events` child: its lines wake poll(); the chat counts it as this session
  eventsStart: '', // its start, as its hello says: with the pid, listener.json's proof of life (#532)
  claudePid: 0, // this Claude Code process, the `wb events` child's parent, as its hello says: 0 unknown (#687)
  machine: '', // this machine, as the hello says (store.machine): '' from a wb before #922
  claudeStart: '', // and its start
  earsOk: false, // `wb events` has said hello in this load: wb runs
  gone: new Set<string>(), // task directories closed past KEEP_CLOSED_MS: final, never read again
  recent: new Map<string, { key: string; recent: string[] }>(), // each task's last events, by events.jsonl's size@mtime (recentEvents)
  again: false, // something changed while a poll ran: run another after it
  hasActive: false, // a task is running: the clock redraws
  lastPoll: 0,
  listenerBody: '', // what this session last wrote to chat/listener.json
  waitSaid: null as Waiting | null, // the wait the claim last said (listen): a change wakes the poll (#1355)
  tick: 0, // the apprentice's frame count
  // the opening animation while it plays (#963): its plan once the pane drew it, the frame on
  // screen, the ticks it waited for a first draw, its timer
  splash: null as null | { plan: Plan | null; f: number; waited: number; timer: { cancel: () => void } | null },
  posed: '' as Pose | '', // its pose as last drawn in a Raster: the timer animates it while set
  shown: '' as Pose | '', // the frame on screen now
  skill: '', // the /workbench skill's SKILL.md, for the workbench-mode block
  commandsFile: '', // commands.json ($WB_COMMANDS): Claude's edit never drops a repo's local_only (#1021)
  fold: false, // the session's directory is on a file system that ignores case (macOS): `within`
  termDialogs: [] as TermDialog[], // dialogs up in the terminal, for the page's waiting line (#959, #1350)
}

// Whether a task is this session's: the kernel's owns (Kernel.Owner), which `wb`'s _scope judges
// the same and the OpenCode plugin judges by too. One `wb start` ran in this process's sessions
// (task.json `session`) is, wherever it ran; once that Claude Code process is gone (`claude_pid`,
// mem.deadPids) a new process near it adopts it, never another driver's (#1309); one without a
// session goes by directory, in ~ or / by none; one with no key at all is everyone's. Paths by whole
// components (/x/ab is not inside /x/a), in lower case where the file system ignores it
// (`mem.fold`, macOS: ~/Repos typed for ~/repos once missed, the 2026-11-19 audit). wb records
// task paths real (getcwd, git's toplevel). The facts are gathered here; the kernel compares them.
type OwnerKeys = { session?: string; repo?: string; cwd?: string; subdir?: string; claude_pid?: number; claude_start?: string; driver?: unknown }
// a task's driver as store.driver_of names it: task.json `driver.tool` opencode, else claude with
// a session, else none (WbTask carries it as scan read it)
const driverOf = (t: { session?: string; driver?: unknown }): string =>
  typeof t.driver === 'string' ? t.driver
    : (t.driver as { tool?: unknown } | null)?.tool === 'opencode' ? 'opencode' : t.session ? 'claude' : ''
const ownerOf = (t: OwnerKeys) => owns({
  session: String(t.session ?? ''), ids: [...mem.ids], driver: driverOf(t), host: 'claude',
  gone: mem.deadPids.has(pidKey(t)), cwd: String(t.cwd ?? ''), repo: String(t.repo ?? ''),
  subdir: String(t.subdir ?? ''), here: mem.cwd, home: mem.home, fold: mem.fold,
})
const mine = (t: OwnerKeys) => ownerOf(t).mine

// Whether another session's task's Claude Code process is gone (mem.deadPids, for `mine`): `kill
// -0` and its start (task.json `claude_start`: a recycled pid is another process, #532), a live
// one at most every PID_CHECK_MS. A gone one stays gone; one wb can't ask about (kill failed to
// run) stays unknown, so its task stays the other session's. Keyed by pid and start.
const PID_CHECK_MS = 30_000
const pidKey = (t: { claude_pid?: number; claude_start?: string }) =>
  `${Number(t.claude_pid ?? 0)}@${t.claude_start ?? ''}`
// A pid recorded on another machine (task.json `claude_machine`, listener.json `machine`): it
// means nothing in this one's /proc. On a state directory two machines share, each machine's mod
// read the other's live session as gone and adopted its tasks, back and forth, and took the chat's
// listener from it too (#922; the runners' own pids got the same fix in #849).
const elsewhere = (machine: unknown) => typeof machine === 'string' && !!machine && !!mem.machine && machine !== mem.machine

async function noteClaude($: any, t: { session?: string; claude_pid?: number; claude_start?: string; claude_machine?: string },
  now: number): Promise<void> {
  const pid = Number(t.claude_pid ?? 0)
  const key = pidKey(t)
  if (!pid || !t.session || mem.deadPids.has(key) || elsewhere(t.claude_machine)) return
  // this session's own, unless stamped by an older process of it (`claude --resume`): that one's
  // death is what lets this process claim it back from the others (#687)
  if (mem.ids.has(t.session) && (!mem.claudePid || ownPid(t))) return
  if (now - (mem.pidAt.get(key) ?? -Infinity) < PID_CHECK_MS) return
  mem.pidAt.set(key, now)
  const live = await alive($, pid, t.claude_start)
  if (live === false) mem.deadPids.add(key)
}

const ownPid = (t: { claude_pid?: number; claude_start?: string }) =>
  Number(t.claude_pid ?? 0) === mem.claudePid && (!t.claude_start || !mem.claudeStart || t.claude_start === mem.claudeStart)

// A task whose Claude Code process is gone is claimed, once, by the first session that would own
// it (`wb adopt`, under the task's lock): task.json then names this process, so every other
// session drops it. Unrecorded, it stayed every nearby session's for good, and the one resumed
// with `claude --resume` too: both woke for its review and both could act on it (#687). Returns
// task.json as it stands after the claim, whoever won.
async function claim($: any, name: string, sp: Partial<TaskFile>): Promise<Partial<TaskFile>> {
  if (!mem.claudePid || !sp.session || !mem.ids.size || !mem.deadPids.has(pidKey(sp)) || ownPid(sp)) return sp
  if (!ownerOf(sp).claims) return sp // neither its id nor near it: never this session's to adopt
  const id = await $.session.id().catch(() => '')
  const ids = [...new Set([id, ...mem.ids].filter(Boolean))]
  const r = await runWb($, ['adopt', name, '--pid', String(mem.claudePid), ...ids.flatMap(i => ['--session', i])], 15_000)
  const now = await readJson<TaskFile>($, `${mem.root}/tasks/${name}/task.json`)
  if (!r.ok && now.claude_pid === sp.claude_pid) mem.deadPids.delete(pidKey(sp)) // wb couldn't say: not ours to claim
  // a task adopted into this session was being delegated: the mode comes back with it (#689).
  // Not one the user's quit parked, nor one handed back to Claude (taken over): adopting those
  // turned the mode back on against /workbench-quit or the hand-back (#834).
  if (r.ok && delegatedWork(await readJson<StatusFile>($, `${mem.root}/tasks/${name}/status.json`))) await workbenchMode($, true)
  return now.name ? now : sp
}

// Whether a task (its status.json) is still delegated work: running, or waiting on Claude and
// neither parked by a quit (store.parked, #698) nor taken over by Claude (#834)
const delegatedWork = (st: Partial<StatusFile>) => {
  const phase = String(st.phase ?? 'queued')
  const p = phaseOf({ phase, parked: !!st.parked })
  return p.active || (p.waits && !st.taken_over)
}

// This session's id joins the ids this process has had: at the start and at each poll, so a
// /clear's new one counts before its first `wb start` is read.
async function noteSession($: any): Promise<void> {
  const id = await $.session.id().catch(() => '')
  if (!id || mem.ids.has(id)) return
  mem.ids.add(id)
  await update($, sessions, ids => (ids.includes(id) ? ids : [...ids, id]))
  if (mem.delegated && (await read($, chat)).delegating) await keepMode($, true) // a /clear's id resumes in the mode too (#689)
}

// A toast shown once per load: something is wrong that the person should hear about once.
const warnOnce = ($: any, key: string, text: string) => {
  if (mem.warned.has(key)) return
  mem.warned.add(key)
  $.ui.toast(`workbench: ${text}`)
}

// The Claude Code the mod is verified with (check.yml's CLAUDE_CODE_VERSION). Installs track
// main, so an older one met a call it lacks as a bare runtime error (the 2026-11-19 audit): it
// hears why once instead, and the mod carries on, since most of it may still work there.
const MIN_CLAUDE_CODE = '2.1.311'
const older = (a: string, b: string) => {
  const [x, y] = [a, b].map(v => v.split('.').map(n => parseInt(n, 10) || 0))
  const i = [0, 1, 2].find(k => x[k] !== y[k])
  return i !== undefined && x[i] < y[i]
}
async function checkVersion($: any) {
  let at: string | undefined
  try {
    const v = await $.session.version()
    at = (v?.base ?? v?.version ?? '').match(/^\d+\.\d+\.\d+/)?.[0]
  } catch { /* a Claude Code older than the call itself */ }
  if (at && !older(at, MIN_CLAUDE_CODE)) return
  warnOnce($, 'version', `this Claude Code${at ? ` (${at})` : ''} is older than ${MIN_CLAUDE_CODE}, ` +
    'which the pane and wake-ups are tested on: `claude update` if they misbehave')
}

// The last line of what a failing command said (a traceback ends with the error itself).
const brief = (out: string) => out.trim().split('\n').filter(Boolean).at(-1) ?? ''

// A file's text, only its last `max` bytes (from a whole line) when it is longer; '' when it is
// missing, null when it can't be read. $.fs.read rejects a file over 4 MiB, and notify.jsonl and
// the chat's files only grow: past that, deliver read '' and no wake came again (the 2026-11-16
// audit). `key` (size and mtime) tells a change without reading.
async function readTail($: any, path: string, max: number): Promise<{ text: string; key: string } | null> {
  const gone = async () => !(await $.fs.exists(path).catch(() => true))
  let st
  try {
    st = await $.fs.stat(path)
  } catch {
    return (await gone()) ? { text: '', key: '' } : null
  }
  const key = `${st.size}@${st.mtimeMs}`
  if (st.size <= max) {
    try {
      return { text: await $.fs.read(path), key }
    } catch {
      return (await gone()) ? { text: '', key: '' } : null
    }
  }
  try {
    const r = await $.process.run(['tail', '-c', String(max), path], { timeoutMs: 30_000 })
    if (r.exitCode !== 0) return null
    return { text: r.stdout.slice(r.stdout.indexOf('\n') + 1), key }
  } catch {
    return null
  }
}
const NOTIFY_TAIL = 1 << 20 // far more than an hour of notices (noticesDue's)
const EVENTS_TAIL = 64 << 10 // the pane shows a task's last four events

// A task's last four events, read again only when events.jsonl's size or mtime changed: every
// poll (each 1.5 s while one task runs) read each open task's file, past EVENTS_TAIL by spawning
// `tail`, for tasks in review whose files hadn't changed in hours: 10-30 spawns a second in every
// session with the mod (#739, the 2026-11-21 audit).
async function recentEvents($: any, name: string, path: string): Promise<string[]> {
  let key = ''
  try {
    const st = await $.fs.stat(path)
    key = `${st.size}@${st.mtimeMs}`
  } catch {}
  const had = mem.recent.get(name)
  if (key && had?.key === key) return had.recent
  const got = await readTail($, path, EVENTS_TAIL)
  const recent = parseLines(got?.text ?? '').map(eventLine).filter(Boolean).slice(-4) as string[]
  if (got?.key) mem.recent.set(name, { key: got.key, recent })
  else mem.recent.delete(name)
  return recent
}

// Every task directory, as the pane draws it. Closed tasks drop out after half an hour, and are
// then never read again in this load (a closed phase is final): every poll read both files of
// every task ever created (the 2026-11-16 audit).
async function scan($: any, root: string, now: number): Promise<WbTask[]> {
  const dir = `${root}/tasks`
  const entries = (await $.fs.exists(dir)) ? await $.fs.list(dir) : []
  const list: WbTask[] = []
  const names = new Set<string>()
  for (const ent of entries) {
    if (ent.kind !== 'dir') continue
    names.add(ent.name)
    if (mem.gone.has(ent.name)) continue
    const base = `${dir}/${ent.name}`
    const st = await readJson<StatusFile>($, `${base}/status.json`)
    const phase = String(st.phase ?? 'queued')
    const updatedAt = Number(st.updated_at ?? 0) * 1000
    if (!ACTIVE.has(phase) && !NEEDS.has(phase) && now - updatedAt > KEEP_CLOSED_MS) {
      if (CLOSED.has(phase)) mem.gone.add(ent.name)
      continue
    }
    let sp = await readJson<TaskFile>($, `${base}/task.json`)
    if (!sp.name || sp.bench) continue // a replay of wb bench's: no one's task, never shown or woken
    let recent: string[] = []
    if (ACTIVE.has(phase) || NEEDS.has(phase)) {
      await noteClaude($, sp, now)
      sp = await claim($, ent.name, sp)
      recent = await recentEvents($, ent.name, `${base}/events.jsonl`)
    }
    list.push({
      name: String(sp.name), phase, round: Number(st.round ?? 1), model: String(sp.model ?? ''),
      label: String(sp.model_label ?? ''),
      // how it was routed (#1016): task.json's via, role and provider, and its routing line
      via: sp.via === 'api' ? 'api' : 'gpu', role: String(sp.role ?? ''), provider: String(sp.provider_name ?? sp.provider ?? ''),
      routed: String(sp.route ?? ''),
      tests: st.tests ? (st.tests.ok ? 'pass' : 'fail') : null,
      last: String(st.last ?? ''), createdAt: Number(sp.created_at ?? 0) * 1000, updatedAt,
      tokens: Number(st.tok_in ?? 0) + Number(st.tok_out ?? 0), recent,
      repo: String(sp.repo ?? ''), cwd: String(sp.cwd ?? ''), subdir: String(sp.subdir ?? ''),
      session: String(sp.session ?? ''), driver: driverOf(sp),
      claude_pid: Number(sp.claude_pid ?? 0), claude_start: String(sp.claude_start ?? ''),
      backlog: String(sp.backlog ?? ''),
      parked: PARKS.has(phase) && !!st.parked, // as store.parked (#698)
      allowed: allowedLine((await readJson($, `${base}/coding.json`)).allowed),
      ...viewFacts(sp, st),
    })
  }
  for (const name of mem.gone) if (!names.has(name)) mem.gone.delete(name) // removed by hand
  for (const name of mem.recent.keys()) if (!names.has(name) || mem.gone.has(name)) mem.recent.delete(name)

  return list.sort((a, b) => a.createdAt - b.createdAt)
}

// The Tasks list's rows (#1048, tasks.ts): one or two a task, one when the pane can't hold two
// (compactWanted), each with its task; the user's allowance for a task (`wb coding allow`) after
// its rows. The render draws them and pageShape counts them, one row each: none wraps.
type ListRow = { parts: RowPart[]; task: string; first: boolean }
function listRows(list: WbTask[], followed: string, now: number, spin: string, cols: number, bodyRows: number): ListRow[] {
  const open = list.filter(t => !CLOSED.has(t.phase))
  const headers = new Set(open.map(t => t.group).filter(g => g && open.filter(t => t.group === g).length >= 2)).size
  const lines = taskLines(list, { cols, now, spin, followed, compact: compactWanted(open.length, headers, bodyRows), usd })
  const out: ListRow[] = []
  lines.forEach((l, i) => {
    out.push(l)
    const t = l.task && lines[i + 1]?.task !== l.task ? list.find(x => x.name === l.task) : undefined
    if (t?.allowed) for (const w of wrapWords(t.allowed, cols - 4)) out.push({ parts: [{ text: `    ${w}`, color: 'claude' }], task: '', first: false })
  })
  return out
}

// The day cap's line's numbers on the Tasks tab (#1048): `wb route --json` as rolesNow last read it,
// only while an API task works (the owner, 2026-11-23: "only while an API task runs"); null
// otherwise, or before the first read
function capOf(list: WbTask[]): { spent: number; cap: number | null; est: boolean } | null {
  if (!apiWorking(list)) return null
  const r = mem.roles?.routes
  return r ? { spent: Number(r.spend_today?.usd ?? 0), cap: typeof r.day_usd === 'number' ? r.day_usd : null,
    est: isEst(r.spend_today?.prices) } : null
}

// Reads them again past ROLES_CACHE_MS while an API task works, off the render's path (a
// `wb route --json` there held the draw), then redraws
async function capNow($: any, list: WbTask[]) {
  if (mem.rolesReading || !apiWorking(list)) return
  if (mem.roles && (await $.clock.now()) - mem.roles.at < ROLES_CACHE_MS) return
  mem.rolesReading = true
  try {
    await rolesNow($)
  } catch {
    return
  } finally {
    mem.rolesReading = false
  }
  $.ui.invalidate('ui.render')
}

// A press on a task's row follows it (the owner, 2026-11-23): its transcript below, read at once
async function followTask($: any, name: string) {
  mem.picked = name
  mem.liveKey = ''
  await refreshLive($, await read($, tasks), await $.clock.now())
  $.ui.invalidate('ui.render')
}

// The user's unspent allowance for a task (`wb coding allow`), for its line in the pane: what
// Claude may edit, and when, for a one-task exception (#94); '' for none
export function allowedLine(rows: unknown): string {
  const x = Array.isArray(rows) ? rows.find((r: Row) => r && !r.used) : undefined
  if (!x) return ''
  return x.when ? `Claude may, once, if ${x.when}: ${x.edit}` : `Claude may: ${x.edit}`
}

// At most one toast while the check is due: raised since it fell due (by any session, or by
// /workbench-models), not again until it is done and due once more
async function scoutIfDue($: any, now: number): Promise<void> {
  const path = `${mem.root}/scout.json`
  const st = await readJson($, path)
  const done = Number(st.done_at ?? 0) * 1000
  const offered = Number(st.offered_at ?? 0) * 1000
  if (now - done < SCOUT_EVERY_MS || offered >= done + SCOUT_EVERY_MS) return
  await scoutOffered($, now)
  $.ui.toast(SCOUT_DUE)
}

// The model check is raised: not again in this load, nor by any session for a day. Also when
// /workbench-models raised it: the weekly check came straight after (the 2026-11-16 audit).
async function scoutOffered($: any, now: number): Promise<void> {
  mem.scoutAsked = true
  const path = `${mem.root}/scout.json`
  const st = await readJson($, path)
  await $.fs.write(path, JSON.stringify({ ...st, offered_at: now / 1000 })).catch(() => {})
}

async function compactAtBoundary($: any, at: Omit<Parameters<typeof compactPlan>[0], 'percentSet' | 'percent' | 'tokens' | 'window' | 'autoCompact' | 'threshold'>): Promise<void> {
  const set = await $.env.get('WB_COMPACT_AT') // a percentage; 0 turns it off
  const percentSet = !!set && Number.isFinite(Number(set))
  if (percentSet && !Number(set)) return
  let context
  try {
    ({ context } = await $.session.usage({ breakdown: 'summary' }))
  } catch {
    return // no reading yet (a session before its first turn)
  }
  const b = context.breakdown
  const plan = compactPlan({ ...at, percentSet, percent: percentSet ? Number(set) : 0, tokens: Number(context.tokens) || 0,
    window: Number(context.window) || 0, autoCompact: !!b?.isAutoCompactEnabled, threshold: Number(b?.autoCompactThreshold) || 0 })
  if (!plan.compact) return
  try {
    await $.session.compact({ instructions: KEEP_ON_COMPACT }) // no toast: Claude Code shows it (#1322)
  } catch {
    // a turn began meanwhile: the next boundary tries again
  }
}

// `wb <args>`: ok when it exited 0, and what it said. A failing wb's traceback is not output to
// pass on as if it were the answer (the 2026-11-16 audit: it reached Claude as "open tasks").
// `wb update` takes the longest the engine allows: its steps may take 300 s each. `aborted`: the
// engine cancelled the call (a plugin reload, a redraw superseding the one in flight; never a
// timeout, which is wb hung and said as a failure), which is not wb failing, so it says nothing: "could not run wb route --json: ... aborted" showed on the
// pane's old Settings tab after /reload-plugins and stayed ROLES_CACHE_MS, for a command that takes 0.12 s
// (#1142, 2026-11-23). A caller that keeps what it read keeps its last good value instead.
export type WbRun = { ok: boolean; out: string; aborted?: boolean }
// The flows with a `command` (#1295), each a slash command of this session: wb's list at the
// session's start, so a flow saved later is there at the next start or reload. One that would
// take a command another plugin or Claude Code has is refused by the engine, and left out.
async function registerFlows($: any): Promise<void> {
  const { ok, out } = await runWb($, ['run', 'flows', '--json'])
  if (!ok) return
  for (const c of flowCommands(out)) {
    if (mem.flows.has(c.command)) continue
    try {
      const hint = flowHint(c)
      await $.command.register({ name: c.command, description: flowDescription(c, false), ...(hint ? { argumentHint: hint } : {}) })
      mem.flows.set(c.command, c)
    } catch { /* taken: the engine's or another plugin's */ }
  }
}

async function runWb($: any, args: string[], timeoutMs = 120_000): Promise<WbRun> {
  try {
    const r = await $.process.run([mem.wb, ...args], { timeoutMs })
    return { ok: r.exitCode === 0, out: `${r.stdout}${r.stderr}`.trim() || '(no output)' }
  } catch (err) {
    return wbThrew(args, err)
  }
}

// What runWb says when `$.process.run` threw: an abort says nothing (#1142), anything else is that
// wb could not run
export const wbThrew = (args: string[], err: unknown): WbRun =>
  isAbort(err) ? { ok: false, out: '', aborted: true } : { ok: false, out: `could not run wb ${args.join(' ')}: ${err}` }

// A `$` call the engine cancelled (its hook's call was): `HooksError: workbench: $.process.run(wb)
// aborted` (#1142), or a DOM-style AbortError. Only a bare "aborted" at the end: the engine words a
// timeout `... aborted: still running after 15000ms` (Claude Code 2.1.325), and that is wb hung,
// which must still say so and back off (#553); /\baborted\b/ took it for an abort (#1142's review,
// 2026-11-23)
export const isAbort = (err: unknown): boolean =>
  (err as any)?.name === 'AbortError' || /\baborted$/.test(String((err as any)?.message ?? err).trim())

// `--session <id>` for each id this process has had, and `--repo <the session's directory>` for
// tasks without one: a quit or the status block takes this session's tasks only, as `mine` does.
async function scope($: any): Promise<string[]> {
  await noteSession($)
  return [...(mem.cwd ? ['--repo', mem.cwd] : []), ...[...mem.ids].flatMap(id => ['--session', id])]
}

// The task worth watching (tasks.ts `focusOf`), the one the user pressed first
const focusOf = (list: WbTask[], followed: string, picked = mem.picked): WbTask | undefined =>
  focusOfTasks(list, followed, picked)

// Re-read the followed task's transcript, while the pane is shown: while it runs (the agent's
// session moves between status writes) at most every LIVE_EVERY_MS, or once when it changed. Each
// read is an OpenCode export, which every session ran back to back, pane or not (the 2026-11-16
// audit). Off the poll's path: an export takes a moment.
async function refreshLive($: any, list: WbTask[], now: number) {
  if (mem.watching) return
  const followed = (await read($, live)).task
  const focus = focusOf(list, followed)
  const key = focus ? `${focus.name}@${focus.updatedAt}` : ''
  if (key === mem.liveKey && !(focus && ACTIVE.has(focus.phase) && now - mem.liveAt >= LIVE_EVERY_MS)) return
  // A running task writes its status on every tool call, and each write ran another `wb watch`
  // (a Python start and an OpenCode export) back to back for the whole round, a core kept busy
  // beside the model (#691): the same task, still running, is read at most every LIVE_EVERY_MS,
  // the change kept for the clock's next poll. Another task, or one that stopped, is read at once.
  if (focus && ACTIVE.has(focus.phase) && mem.liveKey.startsWith(`${focus.name}@`) &&
    now - mem.liveAt < LIVE_EVERY_MS) return
  const shown = ((await $.ui.panes().catch(() => [])) as Row[]).find(p => p.id === PANE)
  if (!shown?.isShown) return // read when it shows again: the key still differs
  mem.watching = true
  try {
    if (!focus) {
      mem.liveKey = ''
      await update($, live, () => ({ task: '', lines: [] }))
      return
    }
    const r = await $.process.run([mem.wb, 'watch', focus.name, '--json', '--last', String(KEEP_LINES)],
      { timeoutMs: 30_000 })
    if (r.exitCode !== 0) return
    mem.liveKey = key
    mem.liveAt = now
    const prev = await read($, live)
    // Cut at the engine's 4 MiB, the newest lines are the ones lost: keep what was shown and say so
    if (r.isStdoutTruncated) {
      const why = { s: 'bad', t: `transcript too long for the pane: wb watch ${focus.name}` }
      if (prev.task === focus.name && prev.lines.at(-1)?.t !== why.t) {
        await update($, live, () => ({ task: focus.name, lines: [...prev.lines, why] }))
      }
      return
    }
    const lines = parseLines(r.stdout)
      .map(x => ({ s: String(x.s ?? ''), t: clip(String(x.t ?? ''), LINE_CHARS) }))
      .slice(-KEEP_LINES)
    if (prev.task !== focus.name || JSON.stringify(prev.lines) !== JSON.stringify(lines)) {
      await update($, live, () => ({ task: focus.name, lines }))
      // The engine keeps a window at the end once it is there, but the pane opens at its top:
      // bring the newest lines into view, unless the person scrolled up to read.
      if (mem.follow) void $.ui.scroll({ in: PANE, to: 'end' }).catch(() => {})
    }
  } catch {
    // wb missing or slow: the task list still shows; try again next poll
  } finally {
    mem.watching = false
  }
}

// The ears: a `wb events` child for the session's life, respawned if it dies. One that never says
// hello (wb not on Claude Code's PATH, `wb setup` never run, a Python too old) was respawned every
// 2 s for good, and nobody was told (the 2026-11-16 audit): it is said once, and the respawns back
// off to a minute until wb has answered once.
async function ears($: any) {
  let wait = 2000
  for (;;) {
    let why = 'it exited at once'
    let hello = false
    try {
      const child = $.process.spawn({ argv: [mem.wb, 'events'] })
      let buf = ''
      for await (const { stream, text } of child) {
        if (stream !== 'stdout') {
          if (!hello && text.trim()) why = brief(text)
          continue
        }
        buf += text
        let cut
        while ((cut = buf.indexOf('\n')) >= 0) {
          const line = buf.slice(0, cut)
          buf = buf.slice(cut + 1)
          let ev: Row
          try { ev = JSON.parse(line) } catch { continue }
          if (ev.hello) {
            mem.eventsPid = Number(ev.hello)
            mem.eventsStart = typeof ev.start === 'string' ? ev.start : '' // listener.json's start (#532)
            mem.claudePid = Number(ev.parent ?? 0) || 0 // whose tasks `wb adopt` claims (#687)
            mem.claudeStart = typeof ev.parent_start === 'string' ? ev.parent_start : ''
            mem.machine = typeof ev.machine === 'string' ? ev.machine : ''
            mem.earsOk = hello = true
            wait = 2000
          }
          void poll($)
        }
      }
    } catch (err) {
      why = brief(String(err)) // the child could not start or its stream closed: start another below
    }
    mem.eventsPid = 0
    mem.eventsStart = ''
    if (!mem.earsOk) {
      warnOnce($, 'ears', `can't run \`${mem.wb} events\` (${why}): the pane runs on the task ` +
        `files alone. Put wb on Claude Code's PATH or set WB_BIN; wb doctor`)
      wait = Math.min(wait * 2, 60_000)
    }
    try {
      await $.clock.sleep(wait)
    } catch {
      return // no clock to wait on (a test): the timer's safety poll stands in
    }
  }
}

// The weekly check waits for a quiet minute after a turn: raised the moment the first turn ended,
// it buried the question Claude had just asked the person (the 2026-11-16 audit).
const SCOUT_QUIET_MS = 60_000

// API dollars (#1022): OpenCode's unit, so a `$`: meter.usd's rule, so the CLI and the pane agree:
// $0.42, `<$0.01` under a cent, a free model's $0 (#1322: one rule, where two said one cap two ways)
export function usd(x: number): string {
  if (x === 0) return '$0'
  return Math.abs(x) < 0.01 ? '<$0.01' : `$${x.toFixed(2)}`
}


// `wb route --json` (and `wb models --json`), for the Tasks view's day-cap line, kept ROLES_CACHE_MS
const ROLES_CACHE_MS = 10_000
const OLDER_WB = 'wb is older than the mod: wb update'

// `wb route --json` and `wb models --json`, kept ROLES_CACHE_MS. `why`: wb's line when the roles can't
// be read (routes.json broken), or OLDER_WB for a wb without them; '' with no routes: nothing drawn.
// Only wb's answer is kept (#1142, 2026-11-23): an aborted read draws the last good roles (else
// nothing) and a failed one wb's line, and either reads again on the next draw. A cached failure,
// or an abort cached as one, stayed on the tab ROLES_CACHE_MS (rolesRead).
export type PaneRoles = { at: number; routes: WbRoutes | null; models: WbModels | null; why: string }
async function rolesNow($: any): Promise<PaneRoles> {
  const now = await $.clock.now()
  if (mem.roles && now - mem.roles.at < ROLES_CACHE_MS) return mem.roles
  const routes = await runWb($, ['route', '--json'], 15_000)
  const models = routes.aborted ? null : await runWb($, ['models', '--json'], 15_000)
  const { shown, keep } = rolesRead(mem.roles, now, routes, models)
  if (keep) mem.roles = shown
  return shown
}

// One read of the roles (#1142): what the tab draws, and whether it is kept ROLES_CACHE_MS. Kept when
// wb answered: the roles, or OLDER_WB (a wb without them stays so until it is updated); `last`, the
// last good read, is drawn for an aborted one, with none nothing (why ''); a failure (routes.json
// broken) shows wb's line and is never kept. `modelsRun` is null when the roles' read aborted: it
// isn't run then.
export function rolesRead(last: PaneRoles | null, now: number, routesRun: WbRun, modelsRun: WbRun | null): { shown: PaneRoles; keep: boolean } {
  if (routesRun.aborted || !modelsRun || modelsRun.aborted)
    return { shown: last ?? { at: now, routes: null, models: null, why: '' }, keep: false }
  const json = (r: WbRun): [any, string] => {
    try {
      if (r.ok) return [JSON.parse(r.out), '']
    } catch { /* an older wb: no --json */ }
    return [null, !r.ok && !/invalid choice|unrecognized arguments/.test(r.out) && r.out !== '(no output)' ? brief(r.out) : OLDER_WB]
  }
  const [routes, why] = json(routesRun)
  const [models] = json(modelsRun)
  const shown: PaneRoles = { at: now, routes: routes?.roles ? routes : null, models: Array.isArray(models?.gpu) ? models : null, why: routes?.roles ? '' : why || OLDER_WB }
  return { shown, keep: !!shown.routes || shown.why === OLDER_WB }
}

async function poll($: any) {
  if (!mem.root || !mem.person) return
  if (mem.polling) { // a slow file system must not stack polls, but no change may be missed
    mem.again = true
    return
  }
  mem.polling = true
  mem.again = false
  mem.lastPoll = await $.clock.now()
  try {
    await noteSession($)
    const now = await $.clock.now()
    const list = await scan($, mem.root, now)
    const drawn = JSON.stringify(list)
    if (drawn !== mem.lastDrawn) { // write only on change: every write redraws the pane
      mem.lastDrawn = drawn
      await update($, tasks, () => list)
    }
    const on = (await read($, chat)).delegating
    mem.hasActive = list.some(t => ACTIVE.has(t.phase))
    const { live, parts } = taskParts(list, on)
    $.ui.status(parts.length ? `⚒ wb · ${parts.join(' · ')}` : on ? '⚒ wb · workbench mode' : undefined)
    if ((live.length || on) && !mem.paneOpened) { // open once per load, in workbench mode or when its work starts
      mem.paneOpened = true
      if (!(await read($, pane)).minimized) void $.ui.open({ id: PANE, title: 'Workbench' }) // put away stays away
    }
    void refreshLive($, list, now)
    if (!mem.busy && mem.queued.length && now >= mem.queuedRetryAt) void sendQueued($, mem.queued.shift()!)
    const isOpen = (t: WbTask) => ACTIVE.has(t.phase) || NEEDS.has(t.phase)
    // Only this session's tasks, and only in workbench mode, make a boundary: a task closing
    // compacted every session's context, delegating or not (the 2026-11-16 audit).
    const open = new Set(list.filter(t => isOpen(t) && mine(t)).map(t => t.name))
    const was = [...mem.open]
    mem.open = open
    await sayRules($, on)
    const at = { on, was, open: [...open], boundary: mem.boundary, busy: mem.busy, waiting: list.some(t => phaseOf(t).waits && mine(t)) }
    const plan = compactPlan({ ...at, percentSet: false, percent: 0, tokens: 0, window: 0, autoCompact: false, threshold: 0 })
    mem.boundary = plan.boundary
    if (plan.check) await compactAtBoundary($, at)
    // the weekly check needs wb to run (`wb events` said hello) and a quiet minute after a turn,
    // in a session that delegates with wakes on: the plugin runs in every session, and one for
    // another project got the check, spent the day's offer and ignored wake off (#919)
    const quiet = quietForJobs(list, now, mine)
    if ((on || mem.delegated) && mem.wake &&
      mem.sawTurn && mem.earsOk && !mem.busy && !mem.scoutAsked && quiet &&
      now - mem.turnEndedAt >= SCOUT_QUIET_MS) await scoutIfDue($, now)
    if (mem.sawTurn && mem.earsOk && !mem.updateAsked) {
      mem.updateAsked = true
      void checkUpdate($) // off the poll's path: a stalled fetch held every wake (the 2026-11-16 audit)
    }
    if (mem.sawTurn && mem.earsOk && !mem.optimizeAsked && quiet) {
      mem.optimizeAsked = true
      void measureIfDue($)
    }
    await deliver($, now)
    await rewake($, now, list)
    await listen($, now)
    await readSwitch($)
    await settingsTurns($, now)
    await awayEnded($)
  } finally {
    mem.polling = false
  }
  if (mem.again) void poll($)
}

// A minute or two of measuring the GPU for the local models, when wb says it's due (never run,
// the GPU changed, a new model); the user is told, Claude is not woken (the user, 2026-11-18).
async function measureIfDue($: any) {
  const { ok, out } = await runWb($, ['optimize', 'start', '--if-due'])
  if (ok && out.startsWith('measuring')) $.ui.toast(`workbench: ${out.trim()}`)  // else not due
}

// Claude Code's dialogs and the web chat (#1350, from #1340's decision, on the owner's bottom
// line: the terminal is where things get done, the chat is for a message in a pinch). No dialog
// is answered on the phone: a permission, a question, a plan or an MCP server's form waits in the
// terminal, and the web app shows one line while it does, "Claude is waiting for you in the
// terminal" (a permission's adds "Run it with permissions granted to work from the chat": the
// driver's own modes and rules are the one permission policy, #1350's comment). Every kind, away
// or not, an agent's turn too (#1042's cards are gone with the rest). At the terminal the line
// goes only for a dialog still up after NOTICE_AFTER_MS: written at once, every prompt approved
// in two seconds buzzed the phone ten seconds later saying it still waited (the #959 review);
// while the user is away, at once. One open line at a time: a burst of prompts buzzed once per
// prompt. The dialog settled (its call went on, the form answered, the turn ended) clears the
// line and counts as the user being back (termDialogDone): they answered in the terminal, which
// submits no prompt. The relay this replaces put each dialog on the page as a card to answer, and
// an away setting answered permissions for an away user (#1350).
const NOTICE_AFTER_MS = 30_000
const DIALOG_KIND: Record<string, string> = { ExitPlanMode: 'plan', AskUserQuestion: 'question' }
type TermDialog = { tool: string; agent: string; input: Record<string, unknown>; at: number; due: number; kind: string; text: string; id: string; sending: boolean; done: boolean }

// Whether the tool.call input `e` is the call the dialog's `input` asked about: every argument both
// carry is the same, and they share one unless the call has none of its own (the engine adds some
// to the dialog's: ExitPlanMode's model input is empty, its dialog's carries the plan and its file).
// By tool and loop alone, a Read allowed by a rule finishing beside one asking cleared that one's
// line and marked an away user back, with the dialog still in a terminal nobody watched (the
// #1042 review; again in #1350's review, which had dropped it with the cards)
const CALL_KEYS = new Set(['tool', 'tool_use_id', 'agentId', 'consent', 'requestMeta'])
export function sameCall(input: Record<string, unknown>, e: Record<string, unknown>): boolean {
  const keys = Object.keys(input).filter(k => k in e)
  return (keys.length > 0 || !Object.keys(e).some(k => !CALL_KEYS.has(k)))
    && keys.every(k => JSON.stringify(input[k]) === JSON.stringify(e[k]))
}

function askText(input: unknown): string {
  const i = (input && typeof input === 'object' ? input : {}) as Record<string, unknown>
  const what = [i.command, i.file_path, i.url, i.path, i.pattern].find(v => typeof v === 'string')
  const text = typeof what === 'string' ? what : JSON.stringify(i)
  return text.length > 1500 ? text.slice(0, 1500) + '…' : text
}

// While the user is away, a question or a plan is refused into the web chat, never a dialog (#1351,
// #1340 B and C): with no card on the phone, one would block the turn in a terminal nobody
// watches, and the phone's messages would queue behind it. The refusal tells Claude what to do
// instead; at the terminal both stay ordinary dialogs. On the OpenCode path the same words come
// from `execute.before` (a `permission.hook` deny loses the text, #1319). The words are the
// story's (workbench/story.py `refusal.*`), through the generated ./story both hosts import (#1355).
export const AWAY_QUESTION: string = SAY['refusal.question']
export const AWAY_PLAN: string = SAY['refusal.plan']
const AWAY_REFUSAL: Record<string, string> = { AskUserQuestion: AWAY_QUESTION, ExitPlanMode: AWAY_PLAN }
async function awayRefusal($: any, tool: string): Promise<string> {
  const why = AWAY_REFUSAL[tool]
  if (!why) return ''
  const st = await read($, chat)
  return st.delegating && (await away($, st)) ? why : ''
}

// A dialog came up in the terminal: noted, in the session the chat listens to (workbench mode,
// the web chat on), for its line on the page
async function dialogUp($: any, kind: string, tool: string, agent: string, input: unknown, text: string) {
  const st = await read($, chat)
  if (!st.delegating || !webOn(st)) return
  const at = await $.clock.now(), isAway = await away($, st)
  const args = (input && typeof input === 'object' ? input : {}) as Record<string, unknown>
  mem.termDialogs.push({ tool, agent, input: args, at, due: at + (isAway ? 0 : NOTICE_AFTER_MS), kind, text, id: '', sending: false, done: false })
  if (isAway) void termNotices($, at)
}

// The poll's tick: a dialog still up past its due time gets its line, unless another line of
// this session's is still open
async function termNotices($: any, now: number) {
  if (mem.termDialogs.some(d => d.sending || (d.id && !d.done))) return
  const d = mem.termDialogs.find(d => !d.done && !d.id && now >= d.due)
  if (!d) return
  d.sending = true
  try {
    const { ok, out } = await runWb($, ['chat', 'notice', `--tool=${d.tool}`, `--kind=${d.kind}`, `--text=${d.text}`], 15_000)
    const id = (out.split('\n').pop() || '').trim()
    if (!ok) warnOnce($, 'chat-notice', `the web chat wasn't told of a dialog here (${brief(out)})`)
    d.id = ok && /^\d+-[0-9a-f]+$/.test(id) ? id : 'none' // 'none': wb skipped it, or failed: not again
    if (d.done) await noticeDone($, d)
  } finally {
    d.sending = false
  }
}

// The line's dialog settled here: no push owed for it, and the page's line clears
async function noticeDone($: any, d: TermDialog) {
  if (!d.id || d.id === 'none') return
  const { ok, out } = await runWb($, ['chat', 'notice', '--done', d.id], 15_000)
  if (!ok) warnOnce($, 'chat-notice-done', `the web chat wasn't told a dialog here was answered (${brief(out)})`)
}

// A dialog that came up at the terminal is over (its call went on, its form was answered, or the
// turn ended): the user answered it there, so they are back (#1350; a phone used while it was
// up, the line tapped, no longer counts as the device last used, #959), and its line clears
async function termDialogDone($: any, d: TermDialog) {
  d.done = true
  mem.termDialogs = mem.termDialogs.filter(x => x !== d)
  await back($)
  if (!d.sending) void noticeDone($, d)
}

// wb's patches.CHECK_LINE: a marketplace clone whose local changes Claude Code's update would
// wipe. It comes alone when nothing is new upstream, and only lines that began "update available"
// were toasted, so the warning reached nobody (#1005, 2026-11-22)
const LOCAL_CHANGES = 'local changes in the marketplace clone'

async function checkUpdate($: any) {
  const { ok, out } = await runWb($, ['update', '--check', '--if-due'])
  if (ok && (out.startsWith('update available') || out.includes(LOCAL_CHANGES))) $.ui.toast(`workbench: ${out}`)
}

// A turn a command asked for. One the engine drops (a settings hook's block, a Stop, a host's
// limit) goes back on the queue and is tried again in a minute: the hand-back's turn was lost
// after its agents were already stopped (the 2026-11-16 audit).
async function sendQueued($: any, text: string) {
  mem.submitted = true
  const r = await $.prompt.submit({ text }).catch((err: unknown) => ({ drop: String(err) }))
  if (!r?.drop) return
  mem.submitted = false // dropped: no turn of its own comes
  mem.queued.unshift(text)
  mem.queuedRetryAt = (await $.clock.now()) + REWAKE_MS
  warnOnce($, `queued:${text.slice(0, 40)}`, `a turn for Claude was dropped (${r.drop}); trying again in a minute`)
}

// When a process started, as wb's models._started says it: /proc/<pid>/stat's 22nd field, else
// `ps -o lstart=` (macOS has no /proc) in UTC, since ps prints local time and a laptop that
// changed zones read every live session as gone (#848); '' when neither says.
async function startOf($: any, pid: number): Promise<string> {
  try {
    const stat = String(await $.fs.read(`/proc/${pid}/stat`))
    const field = stat.slice(stat.lastIndexOf(')') + 1).trim().split(/\s+/)[19]
    if (field) return field
  } catch {}
  return psStart($, pid, true)
}

// ps's lstart, in UTC, or (utc false) in the zone ps inherits, as stamps written before #848 were.
async function psStart($: any, pid: number, utc: boolean): Promise<string> {
  try {
    const r = await $.process.run(['env', 'LC_ALL=C', ...(utc ? ['TZ=UTC0'] : []), 'ps', '-o', 'lstart=', '-p', String(pid)], { timeoutMs: 5000 })
    if (r.exitCode === 0) return r.stdout.trim().split(/\s+/).join(' ')
  } catch {}
  return ''
}

// Whether a process lives and, given the start recorded beside its pid, is still the one that
// wrote it: a recycled pid is another process (#532), as wb's models._alive judges. Only two
// starts of one kind compare (both /proc's tick count, or both ps's date). Where there is /proc
// (Linux), /proc/<pid> says, else `kill -0` (macOS): a container without procps has no `kill`, so
// no gone process was ever seen, nobody adopted a crashed session's tasks, and two listening
// sessions took the chat's listener from each other on every poll (#700). null: unknown.
async function alive($: any, pid: number, start?: string): Promise<boolean | null> {
  if (await $.fs.exists('/proc/self/stat').catch(() => false)) {
    if (!(await $.fs.exists(`/proc/${pid}/stat`).catch(() => false))) return false
  } else {
    try {
      if ((await $.process.run(['kill', '-0', String(pid)], { timeoutMs: 5000 })).exitCode !== 0) return false
    } catch {
      return null
    }
  }
  if (!start) return true
  const now = await startOf($, pid)
  if (!now || /^\d+$/.test(now) !== /^\d+$/.test(start) || now === start) return true
  // a ps date recorded before #848, in local time, is still that process
  return !/^\d+$/.test(now) && (await psStart($, pid, false)) === start
}

// The web chat's switch (chat/switch.json, `wb chat on|off`): changed, the link is looked up now,
// not up to CHAT_CHECK_MS later: what it means is wb's to say (chat.is_on: on unless it says off)
async function readSwitch($: any) {
  const key = await $.fs.read(`${mem.root}/chat/switch.json`).catch(() => '-')
  if (key === mem.switchKey) return
  if (mem.switchKey) void chatLink($) // off the poll's path: starting the app waits for it up to 10 s
  mem.switchKey = key
}

// The web app's link (#1283): `wb chat url --json` (chat.ensure: its state, the link, why not), at
// session start, every CHAT_CHECK_MS, and CHAT_RECHECK_MS after it says started, failed or blocked
// (only when that is new: an app that stays down is asked again each minute, not every 3 s). A wb
// older than --json (a nonzero exit, or output that isn't JSON): plain `wb chat url`, read as up.
// A url is taken only whole, `#token=` and all. Off after `wb chat off`: away ends with it, so no
// prompt is sent where nobody reads. An abort says nothing (#1142): the next look asks again.
const LINK_STATES = new Set(['up', 'started', 'off', 'blocked', 'failed', 'checking'])
const LINK_URL = /^https?:\/\/\S+#token=\S+$/
export function linkOf(run: WbRun, plain = false): Link | null {
  if (plain) {
    const out = run.out.trim()
    return run.ok && LINK_URL.test(out) ? { state: 'up', url: out }
      : { state: 'failed', why: brief(run.ok ? `wb chat url printed no link${out ? `: ${out}` : ''}` : run.out) }
  }
  if (!run.ok) return null
  let j: any
  try { j = JSON.parse(run.out) } catch { return null }
  if (!j || typeof j !== 'object' || !LINK_STATES.has(j.state)) return null
  const url = typeof j.url === 'string' && LINK_URL.test(j.url) ? { url: j.url as string } : {}
  const why = typeof j.why === 'string' && j.why.trim() ? { why: brief(j.why) } : {}
  return { state: j.state, ...(j.state === 'off' ? {} : url), ...why }
}
async function chatLink($: any): Promise<void> {
  if (mem.chatChecking) { mem.chatAgain = true; return }
  mem.chatChecking = true
  mem.chatAgain = false
  try {
    const was = (await read($, chat)).link
    const json = await runWb($, ['chat', 'url', '--json'], 30_000)
    if (json.aborted) return
    let link = linkOf(json)
    if (!link) {
      const plain = await runWb($, ['chat', 'url'], 30_000)
      if (plain.aborted) return
      link = linkOf(plain, true)!
    }
    if (['started', 'failed', 'blocked'].includes(link.state) && link.state !== was.state)
      void Promise.resolve().then(() => $.clock.sleep(CHAT_RECHECK_MS)).then(() => chatLink($), () => {})
    if (JSON.stringify(link) !== JSON.stringify(was)) {
      await update($, chat, c => ({ ...c, link: link! }))
      $.ui.invalidate('ui.render')
    }
    if (link.state === 'off' && was.state !== 'off') await back($)
  } catch {
    // the engine gone meanwhile (a reload): the next look reads it
  } finally {
    mem.chatChecking = false
  }
  if (mem.chatAgain) return chatLink($)
}

// A setting the user changed in the web app's Settings (#1283; chat-admin-api appends each to
// settings-changes.jsonl: {ts, key, label?, from, to, said, help?, by: "web"}) reaches Claude once,
// in the session the web chat listens to (any session while none does): one with `help` (the
// value just chosen is on but can't work yet, as Agent code navigation without probe) as a
// [workbench] turn that asks Claude to offer to set it up, queued (mem.queued) as the pane's
// Settings once queued it; any other as a row Claude reads, starting no turn. The roles, Claude
// coding and Dynamic workflows aren't said twice: their own rows say every change (unsaid). A row
// older than OLDEST_WAKE_MS is no news. While the user is away on the web chat the offer's turn says
// to answer there (#1555, 2026-11-26: it was made in the terminal they had left). The newest
// delivered is kept in $.store, every session's, so a reload or another session never says it again.
const SETTINGS_CHANGES = 'settings-changes.jsonl'
const SETTINGS_TAIL = 64 << 10
const SAID_ELSEWHERE = /^(?:agents|busy|cap|coding|workflows|(?:role|gpu|api|think)-[a-z]+)$/
const one = (v: unknown, n: number) => String(v ?? '').replace(/\s+/g, ' ').trim().slice(0, n)
const valueWord = (v: unknown) => v === null || v === undefined || v === '' ? 'off' : one(v, 120)
export function settingSaid(r: Row): string {
  const said = one(r.said, 300)
  return `The user changed ${one(r.label, 80) || one(r.key, 80)} from ${valueWord(r.from)} to ${valueWord(r.to)} ` +
    `on the web app.${said ? ` wb said: ${said}` : ''}`
}
export const SETTING_HELP = (r: Row) => `[workbench] ${settingSaid(r)}\n${String(r.help).trim().slice(0, 4000)}\n` +
  'Offer to help with it, and install or change nothing without their yes.'
async function settingsTurns($: any, now: number) {
  const path = `${mem.root}/${SETTINGS_CHANGES}`
  const stat = await $.fs.stat(path).catch(() => undefined)
  const key = stat ? `${stat.size}@${stat.mtimeMs}` : ''
  if (!key || key === mem.settingsKey) return
  const st = await read($, chat)
  const claim = await readJson<ListenerClaim>($, `${mem.root}/chat/listener.json`)
  if (await claimStands($, claim, st.key, now)) return // the listening session's: read again once it is this one's
  const got = await readTail($, path, SETTINGS_TAIL)
  if (!got) return
  mem.settingsKey = key
  let mark = 0
  try { mark = Number(await $.store.get('settingsSeen')) || 0 } catch { /* no store: this load's set alone */ }
  const notes: string[] = []
  let newest = mark
  for (const r of parseLines(got.text)) {
    const ts = Number(r.ts) || 0, id = `${ts}:${String(r.key ?? '')}`
    if (r.by !== 'web' || ts <= mark || mem.settingsSeen.has(id) || ts * 1000 < now - OLDEST_WAKE_MS) continue
    mem.settingsSeen.add(id)
    newest = Math.max(newest, ts)
    const help = typeof r.help === 'string' && r.help.trim()
    if (help) {
      $.ui.toast(`workbench: ${one(r.label, 80) || one(r.key, 80)} changed on the web app: Claude will offer to set it up`)
      mem.queued.push(await awayToo($, SETTING_HELP(r)))
    } else if (!SAID_ELSEWHERE.test(String(r.key ?? ''))) notes.push(settingSaid(r))
  }
  if (newest > mark) try { await $.store.set('settingsSeen', newest) } catch { /* no store */ }
  if (!notes.length || !mem.said) return // no conversation yet: nothing it lacks
  const text = `[workbench] ${notes.join('\n')}`
  const r = await $.session.append({ message: { type: 'user', content: [{ type: 'text', text }] } })
    .catch((err: unknown) => ({ deny: String(err) }))
  if (r?.deny) mem.queued.push(`${text}\n\nNothing to do now; reply in one line.`)
}

// Claude's rows in the thread's tail, oldest first, for answersTo (#1096)
const THREAD_TAIL = 64 << 10 // the user's unread message is recent: so is the row it answers
async function claudeRows($: any, dir: string): Promise<Row[]> {
  const t = await readTail($, `${dir}/messages.jsonl`, THREAD_TAIL)
  return (t?.text ?? '').split('\n').flatMap(line => {
    try {
      const r = JSON.parse(line)
      return r && typeof r === 'object' && r.from === 'claude' ? [r as Row] : []
    } catch {
      return []
    }
  })
}

// The Claude row a user's web message answers (#1096): Claude's last one to the user before it,
// when it names the session that wrote it (`wb chat reply` stamps `session`, `claude_pid`,
// `claude_start`, `claude_machine`, as task.json has them), else null: the listener's, as ever.
export function answersTo(thread: Row[], m: Row): Row | null {
  let last: Row | null = null
  for (const r of thread) {
    if (r.from !== 'claude' || !(Number(r.ts) < Number(m.ts))) continue
    if (Array.isArray(r.to) && r.to.length && !r.to.includes('user')) continue // to an agent alone
    last = r
  }
  return last && typeof last.session === 'string' && last.session ? last : null
}
// how long the listener leaves a message to the live session it answers before taking it itself
// (that session out of workbench mode, or its chat off): ten of its polls and more
const HANDOFF_MS = 30_000
const ELSEWHERE_NOTE = '(This follows another Claude Code session\'s message in the chat, one that session ' +
  'didn\'t take: if it is about that session\'s task (`wb status` lists it as open elsewhere), leave the task ' +
  'to it and tell the user with `wb chat reply` to answer it at that session\'s terminal.) '

// Whether another session's claim on the chat's listener (chat/listener.json) stands: its process
// alive, or, whether it lives unknown, the file fresh. Both the listener and settingsTurns ask it:
// a session that ended in workbench mode leaves its claim, so read as standing, a web change's
// help reached no session ever after (review of #1283, 2026-11-24).
async function claimStands($: any, claim: ListenerClaim, self: string, now: number): Promise<boolean> {
  if (!claim.session || claim.session === self) return false
  // whether it lives unknown, its claim stands while the file is fresh: read as gone, two
  // sessions took the listener from each other on every poll, each write the other's event (#700).
  // Asked only of another session's claim, and a live one at most every PID_CHECK_MS: on macOS
  // (no /proc) each ask is a `kill` and a `ps`, run on every poll, 80 a minute, even for this
  // session's own claim, whose answer went unused (#839).
  let lives: boolean | null = false
  if (claim.pid && elsewhere(claim.machine)) lives = true // can't be asked here (#922)
  else if (claim.pid) {
    const key = `${Number(claim.pid)}@${claim.start ?? ''}`
    if (now - (mem.listenerAt.get(key) ?? -Infinity) < PID_CHECK_MS) lives = true
    else {
      lives = await alive($, Number(claim.pid), claim.start ? String(claim.start) : '')
      mem.listenerAt.clear() // one claim at a time
      if (lives === true) mem.listenerAt.set(key, now)
    }
  }
  const fresh = async () => now - ((await $.fs.stat(`${mem.root}/chat/listener.json`).catch(() => undefined))?.mtimeMs ?? 0) < CHAT_STALE_MS
  return claim.pid ? lives === true || (lives === null && await fresh())
    : now - Number(claim.ts ?? 0) * 1000 < CHAT_STALE_MS
}

async function listen($: any, now: number) {
  const st = await read($, chat)
  const waiting = waitingFact(mem.termDialogs, now) // the dialog the page says Claude waits on (#1355)
  mem.waitSaid = waiting // said, or not this session's to say: either way the tick needn't poll again for it
  if (!st.delegating || !webOn(st)) return
  const dir = `${mem.root}/chat`
  if (!mem.eventsPid) return // the chat knows a listener by its live `wb events` process
  const claim = await readJson<ListenerClaim>($, `${dir}/listener.json`)
  const theirs = claim.session && claim.session !== st.key
  // another session listens: this one stands by, but for the user's answers to its own rows (#1096)
  const standby = await claimStands($, claim, st.key, now)
  if (!standby) {
    if (!(await $.fs.exists(dir))) await runWb($, ['chat', 'log']) // wb makes the directory
    // parent: this Claude Code process, so `wb chat reply` threads onto this session's messages only
    // when it runs under it (chat.caller_listens): another session's report went to the agent this
    // one was answering, pushed to no phone (#1129, 2026-11-23)
    const body = JSON.stringify({ session: st.key, cwd: mem.cwd, pid: mem.eventsPid,
      ...(mem.eventsStart ? { start: mem.eventsStart } : {}), ...(mem.machine ? { machine: mem.machine } : {}),
      ...(mem.claudePid ? { parent: mem.claudePid } : {}), busy: mem.busy, ...(waiting ? { waiting } : {}) })
    if (body !== mem.listenerBody || theirs || claim.session !== st.key) { // only on change: a write is an event
      mem.listenerBody = body
      await $.fs.write(`${dir}/listener.json`, body)
    }
  }
  let thread: Row[] | null = null // Claude's rows, read once a poll, only for a user's message
  for (const m of await unread($, dir)) {
    const id = String(m.id ?? '')
    if ((m.from !== 'user' && !agentOf(m)) || !id || mem.chatSent.has(id) || mem.chatExpired.has(id)) continue
    if ((mem.chatRetry.get(id) ?? 0) > now) continue // its prompt was dropped: again in a minute
    // Whose it is (#1096): one session listens for all, so with two delegating the user's answer
    // to session B's report reached A, which the skill tells to leave B's tasks alone, and B's
    // task waited for the terminal. The page has no reply-to, so it answers Claude's last row to
    // the user before it, which `wb chat reply` stamps with its session: that session takes it,
    // listening or not. No taker within HANDOFF_MS, the listener does, saying whose it followed;
    // its process gone, the listener takes it at once and plain: that session's nearby tasks are
    // this one's now (mine), and its terminal is gone, so the note sent the user to a terminal
    // that no longer exists, on the first message after any fresh `claude` start (review of
    // #1096, 2026-11-23).
    let elsewhereNote = ''
    if (m.from !== 'user') {
      if (standby) continue // an agent's message is the listener's
    } else {
      thread ??= await claudeRows($, dir)
      const to = answersTo(thread, m)
      const ours = !!to && mem.ids.has(String(to.session))
      if (standby && !ours) continue
      if (!standby && to && !ours) {
        await noteClaude($, to, now)
        if (!mem.deadPids.has(pidKey(to))) {
          if (now - Number(m.ts ?? 0) * 1000 < HANDOFF_MS) continue // its session's to take
          elsewhereNote = ELSEWHERE_NOTE
        }
      }
    }
    // Claimed under wb's lock: two sessions starting together both took the chat and both
    // submitted it, each guard knowing only its own sends (#84). Refused: another session's, or
    // too old to spring on Claude, by wb's one threshold, which the page shows too (#536): the mod
    // skipped it on its own clock, and the phone said "Delivered" for a message Claude never got.
    const claimed = await runWb($, ['chat', 'claim', id, '--by', st.key])
    if (!claimed.ok) {
      if (/ expired: /.test(claimed.out)) {
        mem.chatExpired.add(id)
        warnOnce($, 'chat-old', `${brief(claimed.out).replace(/^wb: \S+ /, 'a web chat message ')}`) // said, not dropped in silence (#329)
      } else mem.chatRetry.set(id, now + REWAKE_MS)
      continue
    }
    mem.chatSent.add(id)
    // The terminal shows only `[web chat] <their words>`: how to answer is the skill's to say. A
    // reply to wb shot's pair says which task and step it's about, whichever is active now (#99);
    // a take kept from Claude's shortlist (wb chat listen), which take and where it is (#397); a
    // reply from the page's task sheet, which task (#1201): steering it is Claude's to decide.
    const a = m.about && typeof m.about === 'object' ? m.about : null
    const about = !a ? ''
      : a.audio ? `(keeps take ${Number(a.n)} of the shortlist: ${String(a.path ?? a.name ?? '')}) `
      : a.n == null ? `(about task ${String(a.task)}) `
      : `(about ${String(a.task)}, step ${Number(a.step)}, screenshots ${Number(a.n)}) `
    // the keywords in the user's own web message (keywords.ts), never an agent's: a plugin's prompt
    // carries no hidden context, so deepcode's rule rides in the text, as `about` does
    let deep = ''
    if (m.from === 'user' && m.role !== 'agent') {
      const kw = keywords(String(m.text ?? ''))
      if (kw.deepmode !== null) await setDeepmode($, kw.deepmode)
      if (kw.deepcode) deep = `(${await deepcodeRule($)}) `
    }
    const text = chatPrompt(m, elsewhereNote + about + deep)
    // Mid-turn, into the running turn, as a message typed mid-turn would be; else, or if that is
    // refused, as a prompt of its own (which waits for the turn to end).
    if (mem.busy) {
      const r = await $.session.send({ to: { sessionId: await $.session.id() }, text: `${text}\n\n${agentOf(m) ? AGENT_NOTE : WEB_NOTE}` })
        .catch(() => ({ isDelivered: false }))
      // an agent's is `into`, not `now`: it didn't open this turn, so the turn's plain `wb chat
      // reply` isn't its answer. A wake turn's report to the away user went to the agent alone,
      // pushed to no phone (#1175, 2026-11-24); AGENT_NOTE says to answer it with `--to <id>`.
      // The user's stays `now`: a plain reply in an agent's turn is theirs (the #1042 review).
      // Refused, it is queued: it enters this turn at a step or opens a later one, maybe after a
      // wake's, so an agent's is `into` once read too: acked `now`, the next wake turn's report to
      // the away user went to the agent alone, pushed to no phone (#1212, 2026-11-24).
      if (r.isDelivered) await runWb($, ['chat', 'ack', id, '--by', st.key, '--how', agentOf(m) ? 'into' : 'now'])
      else void sendWeb($, id, text, 'queued', st.key, agentOf(m) ? 'into' : 'now')
      mem.chatOwed.set(id, { at: now, grace: r.isDelivered ? 0 : 1, answer: '', agent: agentOf(m) }) // queued: maybe the next turn's
    } else {
      // behind a prompt submitted this poll (a wake, a queued turn, another web message) whose
      // turn hasn't started yet, it enters only the turn after: that turn's end, Claude not yet
      // having seen it, sent the follow-up for a message Claude then answered (#837). An agent's
      // is `now` only with nothing ahead: behind a wake, the wake's report went to it (#1212).
      mem.chatOwed.set(id, { at: now, grace: mem.submitted ? 1 : 0, answer: '', agent: agentOf(m) })
      void sendWeb($, id, text, agentOf(m) && mem.submitted ? 'into' : 'now', st.key)
    }
    mem.chatTurn.set(id, mem.chatOwed.get(id)?.grace ?? 0) // its turn's end goes to turns.jsonl (#1042)
    if (agentOf(m)) { $.ui.toast(`workbench: a message from agent ${agentOf(m)}`); continue } // never the user away
    // no toast for the user's own message: they just sent it (#1322)
    await $.fs.write(awayFile(), JSON.stringify({ at: now / 1000, by: st.key })).catch(() => {})
  }
}

// The user's messages without a receipt: wb's index of open work (chat/open.json), whatever the
// files' size. The tails it replaces lost a message behind 4 MiB of replies (#329, the audit of
// 1.4); they remain for a wb from before the index.
async function unread($: any, dir: string): Promise<Row[]> {
  const work = await readJson<ChatOpen>($, `${dir}/open.json`)
  if (work.messages && typeof work.messages === 'object') {
    return (Object.values(work.messages) as Row[]).sort((a, b) => Number(a.ts ?? 0) - Number(b.ts ?? 0))
  }
  // the tails: both files only grow, and past 4 MiB the whole read failed and the chat went silent
  const messages = await readTail($, `${dir}/messages.jsonl`, NOTIFY_TAIL)
  const receipts = await readTail($, `${dir}/receipts.jsonl`, NOTIFY_TAIL)
  if (!messages || !receipts) {
    warnOnce($, 'chat', `can't read the web chat's files in ${dir}: wb doctor`)
    return []
  }
  const got = new Set(receipts.text.split('\n')
    .map(l => { try { return JSON.parse(l).id } catch { return undefined } }))
  return messages.text.split('\n').flatMap(line => {
    try {
      const m = JSON.parse(line)
      return m && !got.has(String(m.id ?? '')) ? [m as Row] : []
    } catch { return [] }
  })
}

// A web message as a prompt of its own; the receipt only once it entered. A dropped one had a
// "read" receipt for a message Claude never got (the 2026-11-16 audit): it is tried again.
// `read`: what a `queued` receipt becomes once the running turn ends (readQueued).
async function sendWeb($: any, id: string, text: string, how: string, by: string, read = 'now') {
  mem.submitted = true
  const r = await $.prompt.submit({ text, asUser: true }).catch((err: unknown) => ({ drop: String(err) }))
  if (r?.drop) {
    mem.submitted = false
    mem.chatSent.delete(id)
    mem.chatOwed.delete(id) // not given to Claude: nothing owed yet
    mem.chatTurn.delete(id)
    mem.chatRetry.set(id, (await $.clock.now()) + REWAKE_MS)
    return
  }
  const { ok, out } = await runWb($, ['chat', 'ack', id, '--by', by, '--how', how])
  if (!ok) warnOnce($, 'chat-ack', `wb chat ack failed: ${brief(out)}`)
  else if (how === 'queued') mem.chatQueued.set(id, { by, how: read })
}

// A web message Claude was given and hasn't answered in the chat (#767): the skill and WEB_NOTE
// say to answer with `wb chat reply`, and nothing checked. For two release cycles the owner wrote
// "Keep me updated over here" and "Status" from the phone, Claude answered every one in the
// terminal only, and the phone heard nothing (2026-11-21). At the end of the turn that had it
// (`grace`: a message queued behind a running turn may only enter the next, #837), with no reply
// since, the mod posts that turn's own answer there itself (`wb chat reply --turn-answer`, an
// `auto` row wb cuts to about 4,000 characters; #1352, from #1340 F). It replaced a nudge turn
// (OWED_NOTE, then a note), which cost Claude a turn, would leave the phone with nothing on a
// driver that doesn't follow the skill (OpenCode's, #1296), and was settled by a regex on Bash
// even when the post failed. A reply is a row of Claude's in messages.jsonl since the message
// was given; a prompt typed in the terminal ends it (the user is back). A failed post, or a turn
// that said nothing (thinking only, an interrupt), leaves it owed: the next turn's end tries
// again, with the failed post's answer, the one that turn gave.
// An agent's message (`agent`, its name) is owed its own reply, `--to` it (#1273, 2026-11-24):
// any reply to the user settled the agent's debt too, and the agent's wait heard nothing. Only a
// reply to it settles it: a row whose `re` holds its id or whose `to` its name; the turn's answer
// goes to it in a post of its own, never in one to the user's phone.
type Owed = { at: number; grace: number; answer: string; agent: string }
// what goes on wb's command line: past wb's cut, under Linux's 128 KiB for one argument
const ANSWER_ARGV = 16_000
// The user's debts only: the user back at the terminal is answered there, never an agent (#1273)
function userOwedClear() {
  for (const [id, o] of [...mem.chatOwed]) if (!o.agent) mem.chatOwed.delete(id)
}
async function owedAtTurnEnd($: any, answer: string) {
  if (!mem.chatOwed.size) return
  const tail = await readTail($, `${mem.root}/chat/messages.jsonl`, NOTIFY_TAIL)
  // a reply settles it when it answers the message (`re`) or this Claude Code wrote it to the
  // message's sender (`claude_pid`, unknown: yes; `to`, none: the user's): another session's
  // report, posted meanwhile, read as the answer (#1129); a reply to the user, as an agent's (#1273).
  // The mod's own post (`auto`) only by `re`: always this Claude Code's, m1's answer settled m2,
  // given in the same poll, and m2's answer never went (the #1352 review, 2026-11-25)
  const mine = (m: any) => !m.auto && (!mem.claudePid || !Number(m.claude_pid ?? 0) || Number(m.claude_pid) === mem.claudePid)
  const toWhom = (m: any, o: Owed) => (Array.isArray(m.to) && m.to.length ? m.to : ['user']).includes(o.agent || 'user')
  const replied = parseLines(tail?.text ?? '').filter(m => m.from === 'claude')
  // an answer to post, for one sender → the messages it answers
  const due = new Map<string, { text: string; ids: string[] }>()
  for (const [id, o] of [...mem.chatOwed]) {
    if (replied.some(m => Number(m.ts ?? 0) * 1000 >= o.at && ((mine(m) && toWhom(m, o)) || (Array.isArray(m.re) && m.re.includes(id))))) {
      mem.chatOwed.delete(id)
      continue
    }
    if (o.grace > 0) { o.grace--; continue }
    o.answer ||= answer.trim().slice(0, ANSWER_ARGV)
    if (!o.answer) continue
    const key = JSON.stringify([o.agent, o.answer])
    due.set(key, { text: o.answer, ids: [...due.get(key)?.ids ?? [], id] })
  }
  for (const { text, ids } of due.values()) {
    const { ok, out, aborted } = await runWb($, ['chat', 'reply', '--turn-answer', `--text=${text}`, ...ids.map(id => `--to=${id}`)], 15_000)
    if (ok) for (const id of ids) mem.chatOwed.delete(id)
    else if (!aborted) warnOnce($, 'chat-owed', `the turn's answer didn't reach the web chat (${brief(out)}); trying again when the next turn ends`)
  }
}

// The turn-end record (#1042): `wb chat ended <ids>` once a main-loop turn ends, the ids the web
// messages that turn took (a queued one, `grace`, only the turn after), so an agent's GET /api/wait
// knows Claude is done with its message; with none, when messages.jsonl grew in the turn (a reply
// Claude wrote, maybe a later report), so a wait on that reply ends too. Nothing while the chat is off.
// The start's size is a promise the end awaits, taken and cleared at once: set when its stat came
// back, a turn that ended first read the previous turn's size, and the late one became the next
// turn's, so a turn that wrote nothing was recorded for a row written before it (Claude Code
// 2.1.330 ends a test's turn before its start's stat returns; the nightly run, 2026-11-24).
function chatTurnStart($: any) {
  mem.chatTurnSize = (async () => webOn(await read($, chat))
    ? (await $.fs.stat(`${mem.root}/chat/messages.jsonl`).catch(() => undefined))?.size ?? 0 : -1)().catch(() => -1)
}
async function chatTurnEnd($: any, after: Promise<unknown> = Promise.resolve()) {
  const ids: string[] = []
  for (const [id, grace] of [...mem.chatTurn]) {
    if (grace > 0) mem.chatTurn.set(id, grace - 1)
    else { mem.chatTurn.delete(id); ids.push(id) }
  }
  const start = mem.chatTurnSize
  mem.chatTurnSize = Promise.resolve(-1)
  const was = await start
  await after // the turn's answer is posted first (#1352); the start's size was taken above
  const grew = was >= 0 && ((await $.fs.stat(`${mem.root}/chat/messages.jsonl`).catch(() => undefined))?.size ?? 0) > was
  if (!ids.length && !grew) return
  const { ok, out } = await runWb($, ['chat', 'ended', ...ids, '--by', (await read($, chat)).key], 15_000)
  if (!ok) warnOnce($, 'chat-ended', `wb chat ended failed: ${brief(out)}`)
}

// A queued web message enters at the running turn's next step or as the next turn, so once that
// turn ends Claude has it: its receipt becomes read. The phone said "Queued for Claude" for good
// on messages Claude had read and acted on (2026-11-21). The user's becomes `now`, an agent's
// `into`: which turn took it is unknown, and `now` made it the next wake turn's (#1212).
async function readQueued($: any) {
  for (const [id, { by, how }] of [...mem.chatQueued]) {
    mem.chatQueued.delete(id)
    await runWb($, ['chat', 'ack', id, '--by', by, '--how', how])
  }
}

// Notifications nobody has acknowledged yet: one toast each, and, for this session's tasks, a
// turn for Claude if waking. Another session's task is left for its own session (or `wb wait`):
// not acknowledged here.
async function deliver($: any, now: number) {
  const root = mem.root
  const path = `${root}/notify.jsonl`
  // The stat first, as recentEvents does (#739): every poll read the whole file (up to
  // NOTIFY_TAIL, past it a `tail` spawned) before the size@mtime check, which then skipped only
  // the parse; a few weeks of notices are hundreds of KB, read at each status write (#921).
  let key = ''
  try {
    const st = await $.fs.stat(path)
    key = `${path}:${st.size}@${st.mtimeMs}`
  } catch {}
  // While Claude is mid-turn, look every poll (the turn may act on the task) but hold the wake:
  // a prompt submitted now would queue and arrive after the task has moved on.
  if (key && key === mem.lastNotify && !mem.busy) return
  let file = key && key === mem.notifyRows.key ? mem.notifyRows : null // mid-turn: parsed already
  if (!file) {
    const got = await readTail($, path, NOTIFY_TAIL)
    if (!got) {
      warnOnce($, 'notify', `can't read ${path}, so no wake can come: wb doctor`)
      return
    }
    // wb writes each row whole (store.Task.notify); the kernel's noticesDue coerces each field it reads
    file = { key: got.key && `${path}:${got.key}`, rows: parseLines(got.text) as NoticeRow[] }
    if (file.key) mem.notifyRows = file
  }
  if (file.key === mem.lastNotify && !mem.busy) return
  // Per task, its newest notice and every question, none over an hour old: the kernel's
  // noticesDue (Kernel.Notice). Every notice counts for rewake, however old, a batch's for each
  // task it names (`noticed`): news Claude had, not a lost one (the 2026-11-16 audit).
  // a task's rows alone: a run's own (`run`, no `task`) are deliverRuns', and read as a task named
  // '' they went to tasks//acked (#1278's S9)
  const rows0 = file.rows.filter(r => typeof r.task === 'string' && r.task !== '')
  const { due: picked, noticed } = noticesDue(rows0, mem.startedAt, now)
  for (const n of noticed) mem.noticed.set(n.task, Math.max(mem.noticed.get(n.task) ?? 0, n.ts))
  const due = picked.map(d => [d.task, d.picks.map(i => rows0[i]!)] as const)
  // A wake held for the turn leaves the file unseen, so the poll after the turn sends it: a second
  // task's wake held while the first one's turn ran was never sent (the 2026-11-16 audit).
  let held = false
  for (const [name, rows] of due) {
    const ackPath = `${root}/tasks/${name}/acked`
    let acked = Number(await $.fs.read(ackPath).catch(() => '0')) || 0
    if (rows.every(r => r.ts <= acked)) continue
    let spec = await readJson<TaskFile>($, `${root}/tasks/${name}/task.json`)
    await noteClaude($, spec, now)
    spec = await claim($, name, spec)
    const own = mine(spec)
    // Only wake for news still true: a review notice for a task since discarded, accepted or
    // sent back is acknowledged and dropped.
    const status = await readJson<StatusFile>($, `${root}/tasks/${name}/status.json`)
    for (const row of rows) {
      const key = `${name}:${row.ts}`
      // what to do with it is the kernel's noticePlan (Kernel.Notice): still true (a check-in
      // while the task runs; this question, still unanswered, while the task asks: one the runner
      // dropped woke Claude for nobody, the 0.90 review; else the phase its kind names), toasted
      // once, and the act. The host reads the facts and carries it out
      const question = row.ts > acked && row.kind === 'question' && !!row.id
      const plan = noticePlan({
        kind: String(row.kind ?? ''), hasId: !!row.id, phase: String(status.phase ?? ''),
        questionOpen: question && await asking($, name, String(row.id)), ts: Number(row.ts), acked,
        own, wake: mem.wake, busy: mem.busy, toasted: mem.toasted.has(key), heldAsk: mem.heldAsks.has(key),
      })
      if (plan.act === 'skip') continue
      const batch = batchOf(row)
      const headline = headlineOf(row)
      if (plan.toast) {
        mem.toasted.add(key)
        $.ui.toast(`workbench: ${name} ${headline}`)
      }
      if (plan.act === 'none') continue // the toast only: the notice stays for its session or wb wait
      const next = await nextOf($, row, name)
      const who = batch && batch.length > 1 ? `Tasks ${batch.map(n => `\`${n}\``).join(', ')} were interrupted`
        : `Task \`${name}\` ${headline}`
      const text = `[workbench] ${who}.\n\n${row.text}\n\n${next}`
      if (plan.act === 'steer' || plan.act === 'hold') {
        // A question can't wait for the turn to end: its agent is blocked, and after the ask
        // timeout it guesses (a 20-minute review turn, #542). It goes into the running turn, as
        // a web message does; refused, it is held like the rest, which deliver once the turn ends.
        if (plan.act === 'steer') {
          if (!(await take($, name, row.ts))) { acked = row.ts; continue } // another session's wake took it
          await raiseAck($, ackPath, row.ts) // acknowledge before sending: never twice
          if (await intoTurn($, text)) {
            acked = row.ts
            continue
          }
          await release($, name, acked, row.ts) // only over this session's own mark (#786)
          mem.heldAsks.add(key)
        }
        held = true
        continue
      }
      const prev = acked
      if (plan.act === 'wake' && !(await take($, name, row.ts))) { acked = row.ts; continue } // taken by another
      await raiseAck($, ackPath, row.ts) // acknowledge before waking: never wake twice
      acked = row.ts
      if (plan.act !== 'wake') {
        // held through the turn and timed out unseen: said, not acknowledged in silence (#542)
        mem.heldAsks.delete(key)
        if (plan.act === 'missed' && (await $.fs.read(`${root}/tasks/${name}/ask/${row.id}.answer`).catch(() => '')).startsWith(NO_ANSWER)) {
          void wake($, name, `${WAKE_OPENING} \`${name}\` asked a question while you were busy.\n\n${row.text}\n\n` +
            MISSED_NOTE(name), prev, row.ts)
        }
        continue
      }
      mem.heldAsks.delete(key)
      void wake($, name, text, prev, row.ts)
    }
  }
  if (await deliverRuns($, now, file.rows)) held = true
  if (!held) mem.lastNotify = file.key
}

// A run's own notices (#1278's S9: run_checkpoint, run_done, run_paused, run_stopped, run_failed;
// rows with `run` and no `task`, runs.Run.notify): the one wake a run gives, to the session it is
// `mine` to as a task's is (run.json: the session that ran `wb run start`, else by directory: a
// scheduled run's is the session near its repo in the morning, its notice due twelve hours: a run has no
// rewake, core.ts runNoticesDue, #1491), once (`wb ack --run`, the
// compare-and-set `wb wait <run>` shares), and only while still true, the run in the phase it
// was raised in (core.ts runNoticePlan). Its text is the wake whole: it opens `[workbench] Run
// `<run>``, spend.RUN_WAKE's, and names its next step. A run's items' notices are its driver's
// (runs/<run>/notify.jsonl), never here. Before this Claude was told to wait in the background
// with `wb wait <run>`, the only wake a run had (the review of #1278, 2026-11-25). True when one
// is held for the turn.
const RUN_NAME = /^[a-z0-9][a-z0-9-]{0,23}$/ // runs.RUN_RE
const RUN_HEADLINE: Record<string, string> = { run_checkpoint: 'is at a checkpoint', run_done: 'is done',
  run_paused: 'is paused', run_stopped: 'was stopped', run_failed: 'has failed' }
type RunFile = { session?: string; session_kind?: string; cwd?: string; repo?: string; claude_pid?: number; claude_start?: string }
async function deliverRuns($: any, now: number, rows: NoticeRow[]): Promise<boolean> {
  const mine0 = rows.filter(r => typeof r.run === 'string' && !('task' in r))
  if (!mine0.length) return false
  let held = false
  for (const d of runNoticesDue(mine0, mem.startedAt, now)) {
    const name = d.task
    if (!RUN_NAME.test(name)) continue
    const dir = `${mem.root}/runs/${name}`
    const ackPath = `${dir}/acked`
    let acked = Number(await $.fs.read(ackPath).catch(() => '0')) || 0
    const picks = d.picks.map(i => mine0[i]!)
    if (picks.every(r => r.ts <= acked)) continue
    const spec = await readJson<RunFile>($, `${dir}/run.json`)
    const keys = { session: spec.session, cwd: spec.cwd, repo: spec.repo, claude_pid: spec.claude_pid,
      claude_start: spec.claude_start, driver: spec.session_kind === 'opencode' ? 'opencode' : spec.session ? 'claude' : '' }
    await noteClaude($, keys, now)
    const own = mine(keys)
    const status = await readJson<{ phase?: string }>($, `${dir}/status.json`)
    for (const row of picks) {
      const key = `run:${name}:${row.ts}`
      const plan = runNoticePlan({ phase: String(status.phase ?? ''), raisedIn: String(row.phase ?? ''), ts: Number(row.ts),
        acked, own, wake: mem.wake, busy: mem.busy, toasted: mem.toasted.has(key) })
      if (plan.act === 'skip') continue
      if (plan.toast) {
        mem.toasted.add(key)
        $.ui.toast(`workbench: run ${name} ${RUN_HEADLINE[row.kind] ?? row.kind}`)
      }
      if (plan.act === 'none') continue // the toast only: its own session's, or wb wait's
      if (plan.act !== 'wake' && plan.act !== 'ack') { held = true; continue } // mid-turn: once it ends
      const prev = acked
      if (plan.act === 'wake' && !(await take($, name, row.ts, true))) { acked = row.ts; continue } // taken by another
      await raiseAck($, ackPath, row.ts)
      acked = row.ts
      if (plan.act === 'wake') void wake($, name, String(row.text ?? ''), prev, row.ts, true)
    }
  }
  return held
}

// A notice's headline and next step, by its kind and cause: deliver's and rewake's (#1384)
function headlineOf(row: NoticeRow): string {
  const batch = batchOf(row)
  return batch ? (batch.length > 1 ? `and ${batch.length - 1} more were interrupted`
    : 'was interrupted') : row.cause === 'provider' ? PROVIDER_HEADLINE
    : row.cause === 'local_only' ? LOCAL_ONLY_HEADLINE
    : (outsideTheStep(row) ? OUTSIDE_HEADLINE[row.kind] : undefined) ?? HEADLINE[row.kind] ?? row.kind
}

async function nextOf($: any, row: NoticeRow, name: string): Promise<string> {
  const batch = batchOf(row)
  // a batch's wake ends with its resume, not the failure's next step (#544)
  return batch ? BATCH_NEXT(batch) : row.cause === 'spend' ? (row.guard === 'agents' ? AGENTS_NEXT(name) : SPEND_NEXT(name, (await readRoutes($)).cap !== null))
    : row.cause === 'local_only' ? OUTSIDE_REMEDY
    : row.cause === 'provider' || (outsideTheStep(row) && OUTSIDE_HEADLINE[row.kind]) ? OUTSIDE_NEXT(name)
    : (NEXT[row.kind] ?? NEXT.failed)(name, row.id ? String(row.id) : undefined, String(row.text ?? ''))
}

// A notice taken for this session's wake, under wb's lock (`wb ack`, a compare-and-set shared with
// `wb wait`): two sessions woken by one `wb events` line each read `acked`, then wrote it, and
// both woke Claude for one review (#687). False only when wb says another took it first; a wb
// that can't run leaves the plain write after it to decide, as before.
async function take($: any, name: string, ts: number, run = false): Promise<boolean> {
  try {
    return (await $.process.run([mem.wb, 'ack', name, String(ts), ...(run ? ['--run'] : [])], { timeoutMs: 15_000 })).exitCode !== 3
  } catch {
    return true
  }
}

// The plain write after a take, for a wb that couldn't run: only ever up. Written unchecked, it
// lowered a mark another session or `wb wait` had claimed since `wb ack` wrote this one, and that
// later notice was delivered twice (#786).
async function raiseAck($: any, ackPath: string, ts: number) {
  if ((Number(await $.fs.read(ackPath).catch(() => '0')) || 0) < ts) await $.fs.write(ackPath, String(ts))
}

// A wake taken but not delivered goes back to `prev`, only while the mark is still this session's
// `ts`: a compare-and-set under wb's lock (`wb ack <task> <prev> --if <ts>`); the plain write put
// the old mark back over a notice claimed since, delivered twice then (#786). A wb that can't
// run leaves the check-then-write, as before.
async function release($: any, name: string, prev: number, ts: number, run = false) {
  try {
    const r = await $.process.run([mem.wb, 'ack', name, String(prev), '--if', String(ts), ...(run ? ['--run'] : [])], { timeoutMs: 15_000 })
    if (r.exitCode === 0 || r.exitCode === 3) return
  } catch {}
  const ackPath = `${mem.root}/${run ? 'runs' : 'tasks'}/${name}/acked`
  if (Number(await $.fs.read(ackPath).catch(() => '0')) === ts) await $.fs.write(ackPath, String(prev))
}

// Into the running turn, as a message typed mid-turn would be (`listen` does the same for the web
// chat): whether the engine took it.
async function intoTurn($: any, text: string): Promise<boolean> {
  const r = await $.session.send({ to: { sessionId: await $.session.id() }, text: `${await awayToo($, text)}\n\n${MIDTURN_NOTE}` })
    .catch(() => ({ isDelivered: false }))
  return !!r?.isDelivered
}

// Whether a question is still open: asked (ask/<id>.json) and not answered.
async function asking($: any, name: string, id: string): Promise<boolean> {
  const dir = `${mem.root}/tasks/${name}/ask`
  return (await $.fs.exists(`${dir}/${id}.json`)) && !(await $.fs.exists(`${dir}/${id}.answer`))
}

// A wake whose `acked` mark this load just moved from `prev` to `ts`. A prompt the engine drops (a
// settings hook's block, a Stop, a host's limit) never reached Claude, yet was acknowledged and
// lost for good (the 2026-11-16 audit): the mark goes back, unless something acted on the task
// since, and rewake tries again after a minute.
async function wake($: any, name: string, text: string, prev: number, ts: number, run = false) {
  mem.submitted = true
  const r = await $.prompt.submit({ text: await awayToo($, text) }).catch((err: unknown) => ({ drop: String(err) }))
  if (!r?.drop) return
  mem.submitted = false
  mem.dropped.set(name, await $.clock.now())
  await release($, name, prev, ts, run)
  if (run) mem.lastNotify = '-' // a run has no rewake: the next poll reads notify.jsonl again and tries it
  warnOnce($, `drop:${name}`, `the wake for ${name} was dropped (${r.drop}); trying again in a minute`)
}

// The safety net under deliver: a task of this session's waiting on Claude whose news never
// reached it (a lost or swallowed notice; on 2026-11-16 a failure sat unseen for an hour) wakes
// Claude anyway, once, after a minute of Claude being idle: the kernel's rewakePlan (Kernel.Notice).
// `acked` past the task's newest notice (or, with none, the moment it entered that state) means
// Claude has had the news; one idle past 12 hours waited for a session long gone (wb status shows it).
// A minute, as rewake waits: a failed send to the web chat, or a queued prompt, is tried again then
const REWAKE_MS = 60_000
const rewakeOf = (t: WbTask, now: number, own: boolean, had = { acked: 0, seen: -1, marked: 0 }) => rewakePlan({
  phase: t.phase, parked: t.parked, mine: own, updatedAt: t.updatedAt, now, busy: mem.busy, wake: mem.wake,
  dropped: mem.dropped.get(t.name) ?? 0, ...had,
})
async function rewake($: any, now: number, list: WbTask[]) {
  for (const t of list) {
    const own = mine(t)
    if (!rewakeOf(t, now, own).candidate) continue // dropped: once a minute too
    const ackPath = `${mem.root}/tasks/${t.name}/acked`
    const acked = Number(await $.fs.read(ackPath).catch(() => '0')) || 0
    // Claude had the news if it acked the task's latest notice: the runner's last status write
    // (its pid cleared) lands just after that notice, so updated_at alone would wake it twice.
    // The task's own `noticed` mark too (Task.notify): after a reload mem.noticed holds only
    // notify.jsonl's last 1 MiB, and a review whose notice had left it woke Claude again (#844)
    const marked = Number(await $.fs.read(`${mem.root}/tasks/${t.name}/noticed`).catch(() => '')) || 0
    const plan = rewakeOf(t, now, own, { acked, seen: mem.noticed.get(t.name) ?? -1, marked })
    if (!plan.due) continue
    await $.fs.write(ackPath, String(now / 1000))
    const kind = plan.kind
    // its newest notice says why it stopped, as deliver's wake would have: from the phase alone a
    // provider's, the day cap's, a local-only or an Agents-GPU stop woke as "got stuck … Steer it
    // with wb feedback", which wb refuses or which runs a round on an agent that wasn't stuck
    // (#1384, 2026-11-25). One of a batch is its own resume; none in the tail: the phase's.
    const found = mem.notifyRows.rows.filter(r => r.task === t.name || batchOf(r)?.includes(t.name)).at(-1)
    const row = found?.kind === kind ? (batchOf(found) ? { ...found, tasks: [t.name] } : found) : null
    void wake($, t.name, `${WAKE_OPENING} \`${t.name}\` ${row ? headlineOf(row) : HEADLINE[kind] ?? kind}, ` +
      `${ago(now - t.updatedAt)} ago, and no notice reached Claude. \`wb report ${t.name}\` shows where it is.\n\n` +
      (row ? await nextOf($, row, t.name) : (NEXT[kind] ?? NEXT.failed)(t.name)), acked, now / 1000)
  }
}

// The ears and the timers, once a person can be at the session.
function startEars($: any) {
  if (mem.started) return
  mem.started = true
  void ears($)
  $.clock.every(POLL_MS, async () => {
    const now = await $.clock.now()
    if (mem.termDialogs.length) void termNotices($, now)
    // the page's state line within a tick of a dialog's two seconds (#1355): the claim says the
    // wait, and the idle poll is 30 s apart
    const waitMoved = !sameWait(waitingFact(mem.termDialogs, now), mem.waitSaid)
    if (!mem.eventsPid || mem.hasActive || mem.queued.length || waitMoved || now - mem.lastPoll > SAFETY_MS) void poll($)
  })
  $.clock.every(CHAT_CHECK_MS, () => { void chatLink($) }) // the web app's link (#1283)
  $.clock.every(FRAME_MS, async () => {
    if (!mem.posed) return // not drawn in pixels, or the pane is closed
    const frame = frameOf(mem.posed, ++mem.tick)
    if (frame === mem.shown) return
    mem.shown = frame
    const r = await $.ui.blit({ requestId: PANE, key: APPRENTICE, cells: cells(frame) })
      .catch(() => ({ deny: 'gone' }))
    if (r.deny) mem.posed = '' // the next draw sets it again
  })
}

// The opening animation (splash.ts; #963, 2026-11-21: the owner's "it needs to end by pulling
// in the Tasks page like normal and have no penalty beyond initial startup"). The /workbench that
// turns the mode on starts it (a session's first, or the first after /workbench-quit or the
// hand-back: the owner expected the quit and /workbench again to open with it, 2026-11-21),
// unless the user turned it off (`wb splash off`, splash.json; the web app's Settings); a reload, /workbench
// while the mode is on, a bring-back from the strip or a pane a task opened never do (the
// `splashed` atom, which workbenchMode clears when the mode goes off). While it plays the pane is one Raster the size of
// its body, repainted by its own timer through $.ui.blit; polling and loading carry on under it,
// so the page it hands over to is current. It ends after its last frame, at a key in the pane
// (Tab, a click, arrows and scroll), or when the pane is closed, too small or can't take a blit
// (a resize plans it again at the new size),
// and the Tasks page draws as it always does. Its timer is cancelled then: nothing of it runs
// after. Not at a key typed in the prompt: that needs a prompt.edit hook, which can't be taken
// off again, so every keystroke of the session would wait on the mod for an animation long over
// (2026-11-21, review of #963); typing goes on beside it, and it's over in five seconds.
const SPLASH = 'splash' // its Raster's key, for $.ui.blit
const SPLASH_WAIT = 25 // ticks (2 s) it waits for the pane's first draw before giving up
// splash.json as wb writes it (`wb splash on|off`): on unless it says off
async function splashOn($: any): Promise<boolean> {
  try {
    return JSON.parse(await $.fs.read(`${mem.root}/splash.json`)).on !== false
  } catch {
    return true
  }
}

function startSplash($: any) {
  const s: NonNullable<typeof mem.splash> = { plan: null, f: 0, waited: 0, timer: null }
  mem.splash = s
  s.timer = $.clock.every(SPLASH_MS, () => { void splashTick($, s) })
}

async function splashTick($: any, s: NonNullable<typeof mem.splash>) {
  if (mem.splash !== s) return
  if (!s.plan) { // the pane hasn't drawn it yet
    if (++s.waited > SPLASH_WAIT) endSplash($)
    return
  }
  if (s.f + 1 >= frameCount(s.plan)) return endSplash($)
  s.f += 1
  const r = await $.ui.blit({ requestId: PANE, key: SPLASH, cells: packed(frameCells(s.plan, s.f)) })
    .catch(() => ({ deny: 'gone' }))
  if (r?.deny && mem.splash === s) endSplash($) // the pane is gone or drew something else: the page then
}

// Ends it: the timer cancelled, the page drawn (`redraw`: unless the render ending it draws it)
function endSplash($: any, redraw = true) {
  const s = mem.splash
  if (!s) return
  mem.splash = null
  try { s.timer?.cancel() } catch { /* already stopped */ }
  if (redraw) $.ui.invalidate('ui.render')
}

// Turning workbench mode on or off: the chat listens while it is on. The context block follows
// at the next compaction or /clear, not at once: invalidating it would rewrite the first message
// and cost the whole conversation's cache, and the skill or the quit note says it meanwhile.
// The quit commands close the pane, the mode's sign.
async function workbenchMode($: any, on: boolean) {
  if (on) mem.delegated = true
  if ((await read($, chat)).delegating === on) return
  await update($, chat, c => ({ ...c, delegating: on }))
  await keepMode($, on)
  if (!on) {
    await update($, pane, p => ({ ...p, minimized: false })) // quit: no strip either
    await update($, splashed, () => false) // the next /workbench opens it again, with the animation (#963)
    await $.ui.close({ id: PANE }).catch(() => {})
    const path = `${mem.root}/chat/listener.json` // stop listening: the page and other sessions see it now
    const claim = await readJson<ListenerClaim>($, path)
    if (claim.session === (await read($, chat)).key) {
      await $.fs.write(path, '{}').catch(() => {})
      mem.listenerBody = ''
    }
  }
  void poll($) // listen now: a message may already be waiting
}

// Workbench mode by session id, on disk (`$.store` `modes`: id → when it was turned on), so a
// session resumed in a new process (`claude --continue`/`--resume`) comes back in it: the atom
// died with the old process, and until Claude happened to run a delegating command the chat
// didn't listen, the pane and status line didn't show it, and a compaction dropped it (#689).
// Kept 30 days (the kernel's `modesPlan`); a quit clears every id this process has had.
async function keepMode($: any, on: boolean) {
  try {
    const now = await $.clock.now()
    await $.store.set('modes', modesPlan(await $.store.get('modes'), mem.ids, now, on).modes)
  } catch {} // no store (an older Claude Code, a test without one): the mode lasts this process
}
async function modeKept($: any): Promise<boolean> {
  try {
    return modesPlan(await $.store.get('modes'), mem.ids, 0, false).kept
  } catch {
    return false
  }
}

// Opens the pane, or brings it back: a pane the engine still holds but doesn't show (a tab behind
// another, or left undrawn) only retitles on open, so it is closed first.
async function showPane($: any) {
  const held = (await $.ui.panes()).find((p: any) => p.id === PANE)
  if (held && (!held.isShown || !held.isPlaced)) await $.ui.close({ id: PANE }).catch(() => {})
  mem.paneOpened = true
  const opening = $.ui.open({ id: PANE, title: 'Workbench' })
  if ((await read($, pane)).minimized) await update($, pane, p => ({ ...p, minimized: false }))
  return opening
}

// Brings the pane back from the strip. Opened first, while the press is still the person's:
// backseat-driver's opened after its awaits counted as its own, and an open the mod makes after a
// close by hand waits for a wide terminal (2026-10-07).
async function restorePane($: any) {
  const opening = $.ui.open({ id: PANE, title: 'Workbench' })
  mem.paneOpened = true
  await update($, pane, p => ({ ...p, minimized: false }))
  $.ui.invalidate('ui.render')
  return opening
}

// What the status line and the strip say of the tasks: the running ones, those asking, how many wait.
// Outside workbench mode only this session's: another session's task running opened the pane and
// filled the status line in a session the user kept off workbench, and /workbench-quit couldn't
// clear it (2026-11-17)
const WAITING_WORD: Record<string, string> = { review: 'awaiting review' }
function taskParts(list: WbTask[], on: boolean) {
  const shown = on ? list : list.filter(t => mine(t))
  const live = shown.filter(t => phaseOf(t).running)
  // what waits on Claude is this session's alone, in the mode too: another session's reviews
  // counted in this one's "N awaiting review", as if this Claude had them to do (#693)
  const own = shown.filter(t => mine(t))
  const asking = own.filter(t => { const p = phaseOf(t); return p.active && p.needsClaude })
  // a stuck or failed task is counted as such: "2 awaiting review" said so for one review and one
  // stuck task (seen live, #1048). The kernel's openCounts tallies them, as the pane's header does;
  // a parked one waits for no one
  const waiting = openCounts(own).counts.filter(c => !c.active && c.waiting > 0)
  return { live, parts: [
    ...live.map(t => `${t.name} ${t.phase} r${t.round}`),
    ...asking.map(t => `${t.name} asks`),
    ...waiting.map(c => `${c.waiting} ${WAITING_WORD[c.phase] ?? c.phase}`),
  ] }
}


// The wb the mod runs without WB_BIN: `wb` by name while a directory on PATH has one, else wb
// setup's link, ~/.local/bin/wb. A desktop app or an IDE started from the GUI (macOS above all)
// may not have ~/.local/bin on its PATH, and the mod only said "Put wb on Claude Code's PATH"
// (#1200). PATH is read, not tried: no wb run at every session start. wb doctor says the same
// (cli.mod_wb).
async function findWb($: any, home: string): Promise<string> {
  const path = String((await $.env.get('PATH').catch(() => '')) ?? '')
  for (const dir of path.split(':'))
    if (dir.startsWith('/') && (await $.fs.stat(`${dir.replace(/\/+$/, '')}/wb`).catch(() => undefined))?.kind === 'file') return 'wb'
  const linked = `${home}/.local/bin/wb`
  return home && (await $.fs.stat(linked).catch(() => undefined))?.kind === 'file' ? linked : 'wb'
}

// The task /workbench was given, as the skill's text has it after expansion ("**First task:**
// `<arguments>`."): empty when the user typed /workbench alone.
const firstTask = (text: string) => /\*\*First task:\*\* `([\s\S]*?)`\. Empty\?/.exec(text)?.[1].trim() ?? ''

export const register: Register = (on, options) => {
  mem.wake = (options as Row | undefined)?.wake !== 'off'

  on('session.start', async ($, e, next) => {
    const home = await $.env.get('HOME')
    const wbHome = await $.env.get('WB_HOME')
    const real = async (p: string) => (await $.fs.stat(p, { resolve: true }).catch(() => undefined))?.realPath ?? p
    mem.cwd = await real(e.cwd)
    const abs = (v: string) => absolutePath(v, home ?? '', mem.cwd) // as wb reads it (#454)
    mem.root = wbHome ? abs(wbHome) : `${home}/.local/state/workbench`
    // a path as wb reads its own (a `~` or a relative one, settings.json's env passes it as typed:
    // `~/repos/workbench/bin/wb` ran a literal `~`, and the mod couldn't run wb at all, #699); a
    // bare name still goes through PATH
    const bin = await $.env.get('WB_BIN')
    mem.wb = bin ? (bin.includes('/') || bin.startsWith('~') ? abs(bin) : bin) : await findWb($, home ?? '')
    // none on the mod's PATH nor wb setup's link (a fresh install): the marketplace clone's (#1190)
    if (!bin && mem.wb === 'wb' && await onPath($, 'wb') === false) {
      const clone = await marketplaceClone($, home ?? '', mem.cwd)
      if (clone) mem.wb = `${clone}/bin/wb`
    }
    const config = await $.env.get('CLAUDE_CONFIG_DIR')
    mem.skill = `${config ? abs(config) : `${home}/.claude`}/skills/workbench/SKILL.md`
    const commands = await $.env.get('WB_COMMANDS')
    mem.commandsFile = commands ? abs(commands) : `${home}/.config/workbench/commands.json`
    mem.startedAt = await $.clock.now()
    // No /workbench skill: a fresh install (#1190; `setUp`), or a copy updated across 2026-11-17,
    // when /delegate became /workbench, with only the old skill link, which leads nowhere. wb
    // setup makes the link (and drops the old one). A fresh install's only with a person there:
    // a `claude -p` or SDK session runs no wb (below).
    const skills = mem.skill.slice(0, -'/workbench/SKILL.md'.length)
    if (!(await $.fs.stat(mem.skill).catch(() => undefined))) {
      const renamed = (await $.fs.stat(`${skills}/delegate`).catch(() => undefined))?.isLink
      if (renamed) {
        const { ok, out } = await runWb($, ['setup'])
        $.ui.toast(ok ? 'workbench: /delegate is now /workbench (wb setup linked it)'
          : `workbench: /delegate is now /workbench, but wb setup failed: ${brief(out)}`)
      } else if (e.isInteractive || e.surface != null) void setUp($, await runWb($, ['setup'])).catch(() => {})
    }
    if (!(await read($, chat)).key) {
      const key = `${Math.floor(mem.startedAt)}-${Math.random().toString(36).slice(2, 8)}`
      await update($, chat, c => ({ ...c, key }))
    }
    await $.command.register({
      name: MODELS,
      description: 'Check now which models suit each role best, GPU and API (weekly on its own)',
    })
    await $.command.register({
      name: UPDATE,
      description: 'Update workbench now: wb, the /workbench skill and this pane (checked weekly)',
    })
    await $.command.register({
      name: QUIT,
      description: 'Stop this session\'s delegated agents now, finishing nothing',
    })
    await $.command.register({
      name: DEEPMODE,
      description: 'Dynamic workflows (deepmode): /deepmode on or off, alone to see it; on, substantive requests run as deep workflows',
    })
    await $.command.register({
      name: HAND_BACK,
      description: 'Stop this session\'s agents and hand their tasks back to Claude to finish',
    })
    // one wb call, not waited on, with a person there (a `claude -p` fire types no command)
    if (e.isInteractive || e.surface != null) void registerFlows($).catch(() => {})
    // the file system ignores case when some component of the directory is there in its own
    // parent with its letters' case swapped, as store.folds_case: the whole path swapped read
    // /MNT/C/uSERS/… on WSL2, missing on the ext4 root, so drvfs read case-sensitive (#1217,
    // 2026-11-24); any component, as WSL makes its own directories there case-sensitive
    const swap = (s: string) => [...s].map(c => c === c.toLowerCase() ? c.toUpperCase() : c.toLowerCase()).join('')
    const comps = mem.cwd.split('/')
    mem.fold = false
    for (let i = comps.length - 1; i > 0 && !mem.fold; i--) {
      const twin = swap(comps[i])
      if (twin !== comps[i]) mem.fold = !!(await $.fs.stat([...comps.slice(0, i), twin].join('/')).catch(() => undefined))
    }
    mem.home = home ? await real(home) : ''
    mem.ids = new Set(await read($, sessions))
    mem.said = mem.said ?? await read($, saidState) // a reload: what this conversation was told stands (#697)
    await noteSession($)
    if ((await read($, chat)).delegating) {
      // a reload in the mode (/workbench-update, an option change, /reload-plugins): mem.delegated
      // starts false, so a /clear's new id was never kept and `claude --continue` into it came
      // back with the mode off, #689 again (#840)
      mem.delegated = true
      await keepMode($, true)
    } else if (await modeKept($)) await workbenchMode($, true) // a resume (#689)
    // A `claude -p` run or an SDK session has no person at it until a surface attaches: it took
    // other sessions' wakes, the weekly check and a `wb status` before every prompt (the
    // 2026-11-16 audit). It stays quiet until then.
    mem.person = e.isInteractive || e.surface != null
    if (e.surface) mem.surfaces.add(e.surface)
    if (mem.person) {
      await checkVersion($)
      startEars($)
      void chatLink($) // the modeline's link, now (#1283)
      void deepmodeMoved($).catch(() => {}) // 2.1's Dynamic workflows on, said once (#1507)
    }
    return next(e)
  })

  on('session.attach', async ($, e, next) => {
    if (e.surface) mem.surfaces.add(e.surface)
    if (!mem.person && mem.root) {
      mem.person = true
      startEars($)
      void chatLink($)
    }
    return next(e)
  })

  // A tool call means a turn is running, even one that began before this module (re)loaded and
  // so never reached turn.start here; turn.complete clears it. Only the model's own (the
  // engine's): another plugin's $.tool.call between turns has no agentId either, and no
  // turn.complete would clear it, holding every wake (the 2026-11-16 audit).
  on('tool.call', async ($, e, next) => {
    if (!mem.busy && !e.agentId && next.origin.plugin === 'engine') { // a subagent's call doesn't hold the main loop
      mem.busy = true
      void poll($)
    }
    // the user away: a question or a plan goes to the web chat instead of the terminal (#1351)
    const awayWhy = await awayRefusal($, String(e.tool))
    if (awayWhy) return { deny: awayWhy }
    // Claude coding (#92): Claude's own edit in a task's worktree waits for the user's choice
    const field = EDIT_FIELDS[e.tool]
    const path = field ? (e as any)[field] : undefined
    if (typeof path === 'string') {
      const why = (await codingRefusal($, path)) || (await routesFileRefusal($, path))
        || (await localOnlyRefusal($, e as Row, path))
      if (why) return { deny: why }
    }
    // Nothing goes to workbench upstream without the user's yes to that very command (#1005,
    // sendRefusal): Claude's, a subagent's or another plugin's call alike; Bash, Monitor and the
    // GitHub MCP tools
    const sending = await sendRefusal($, e as Row)
    if (sending) return { deny: sending }
    // No role's API side changes, and no task starts on an API model, without the user's yes in
    // the dialog (#1014, routeRefusal): anyone's call alike
    const routing = await routeRefusal($, e as Row)
    if (routing) return { deny: routing }
    // Claude's own call in a task's worktree is kept for wb as it begins and once it is over,
    // marked if refused (callRows, #1211, noteCall, #1272): anyone's alike, a subagent's or
    // another plugin's
    const kept = await callRows($, e as Row)
    const at = kept.tasks.length ? await $.clock.now() : 0
    const name = kept.tasks.length ? callFile(at, String((e as Row).tool_use_id ?? '')) : ''
    if (kept.tasks.length) await noteCall($, kept.tasks, kept.row, at, name, 'begun').catch(() => {})
    if (next.origin.plugin !== 'engine') {
      if (!kept.tasks.length) return next(e)
      const r = await next(e)
      await noteCall($, kept.tasks, kept.row, at, name, (r as Row)?.deny ? 'denied' : 'over').catch(() => {})
      return r
    }
    // The engine's call settles its permission check, a dialog included, before it resolves: one
    // whose dialog came up at the terminal is over once it does, answered there (#959): the oldest
    // dialog of that call in that loop (sameCall: another call of the tool, or another loop's,
    // settles none of its)
    let denied = false
    try {
      const r = await next(e)
      denied = !!(r as Row)?.deny
      return r
    } finally {
      if (kept.tasks.length) await noteCall($, kept.tasks, kept.row, at, name, denied ? 'denied' : 'over').catch(() => {})
      const agent = e.agentId ?? ''
      const d = mem.termDialogs.find(d => d.tool === e.tool && d.agent === agent && sameCall(d.input, e as Row))
      if (d) await termDialogDone($, d)
    }
  })

  // A dialog in the terminal gets the page's waiting line (dialogUp, #1350): the moment it would
  // come up. A settings hook's own answer stands, and shows no dialog.
  on('classic.PermissionRequest', async ($, e, next) => {
    const r = await next(e)
    if (r.decision) return r
    const kind = DIALOG_KIND[e.tool_name] ?? 'permission'
    await dialogUp($, kind, e.tool_name, (e as any).agent_id ?? '', e.tool_input, kind === 'permission' ? askText(e.tool_input) : '')
    return r
  })

  // An MCP server's form (an elicitation) waits in the terminal too, until its answer (#1350)
  on('classic.Elicitation', async ($, e, next) => {
    const r = await next(e)
    if (r.block) return r
    const text = String(e.message ?? '')
    await dialogUp($, 'elicitation', `mcp:${e.mcp_server_name}`, (e as any).agent_id ?? '', {}, text.length > 1500 ? text.slice(0, 1500) + '…' : text)
    return r
  })
  on('classic.ElicitationResult', async ($, e, next) => {
    const d = mem.termDialogs.find(d => d.kind === 'elicitation' && d.tool === `mcp:${e.mcp_server_name}`)
    if (d) await termDialogDone($, d)
    return next(e)
  })

  // Delegating with wb counts as workbench mode too (a session resumed mid-delegation, or one
  // where /workbench ran before this mod could see it)
  on('tool.call', { tool: 'Bash' }, async ($, e, next) => {
    if (delegates(String(e.command ?? ''))) {
      await workbenchMode($, true)
    }
    return next(e)
  })

  // /workbench turns workbench mode on for the session, with or without a task, and shows the pane.
  // Run again while it is on, it brings the pane back; alone, that is all it does, except on a
  // fresh install (FIRSTRUN_NOTE).
  on('skill.prompt', { skill: SKILL }, async ($, e, next) => {
    if (e.path) mem.skill = e.path
    const again = (await read($, chat)).delegating
    // what the context block doesn't carry yet goes with the skill's text: before the poll the
    // mode starts, which would say it as a row of its own (#540)
    const extra = await unsaid($, true)
    if (extra.length) mem.skillTurn = true
    const add = (text: string) => extra.length ? `${text}\n\n${extra.join('\n\n')}` : text
    mem.paneOpened = true // before the poll workbenchMode starts, which would open it too
    // the /workbench that turns the mode on opens with the animation, before the pane's first draw (#963)
    if (!(await read($, splashed))) {
      await update($, splashed, () => true)
      if (await splashOn($)) startSplash($)
    }
    await workbenchMode($, true)
    await showPane($)
    // alone on a fresh install, the skill's whole text and the note to run its *First run* now
    // (#1190), the mode on already or not: in this text, since prompt.context fires only at a
    // conversation's first message, and /workbench may come later in it
    const first = !firstTask(e.text) && await freshInstall($)
    if (again && !firstTask(e.text) && !first) return { text: add(AGAIN_NOTE(mem.skill)) }
    const r = await next(e)
    return { ...r, text: add(first ? `${r.text}\n\n${FIRSTRUN_NOTE}` : r.text) }
  })

  // Typed in the terminal: the user is back, so answers go here again
  on('prompt.submit', async ($, e, next) => {
    const typed = byUser(e) && !e.text.startsWith(WEB) && !e.text.startsWith(AGENT) && !e.text.startsWith('[workbench]')
    if (typed) {
      await back($)
      userOwedClear() // back at the terminal: an answer there reaches them (#767), never an agent (#1273)
    }
    if (!typed || e.command) return next(e)
    const kw = keywords(e.text)
    if (kw.deepmode !== null) {
      const said = await setDeepmode($, kw.deepmode)
      if (onlyToggles(e.text)) return { drop: said } // nothing else in it for Claude: no turn
    }
    if (!kw.deepcode) return next(e)
    return next({ ...e, context: [...(e.context ?? []), await deepcodeRule($)] })
  })

  on('command.run', { command: DEEPMODE }, async ($, e) => {
    const arg = String(e.args ?? '').trim().toLowerCase()
    if (arg && arg !== 'on' && arg !== 'off') return { text: 'workbench: /deepmode on, /deepmode off, or /deepmode alone to see it' }
    const { ok, out } = await runWb($, ['deepmode', ...(arg ? [arg] : [])])
    return { text: ok ? out : `workbench: wb deepmode failed: ${out}` } // a change reaches Claude as sayRules' row
  })

  // A flow's own slash command (#1295): the turn that starts it, queued, as a command never starts
  // one itself; any other command goes on
  on('command.run', async ($, e, next) => {
    const flow = mem.flows.get(e.command)
    if (!flow) return next(e)
    mem.queued.push(FLOW_NOTE(flow, e.args ?? ''))
    return { text: `Claude will run flow ${flow.flow} now.` }
  })

  // This session's tasks only (`--repo`): another session keeps its own running.
  on('command.run', { command: QUIT }, async $ => {
    await workbenchMode($, false)
    // waiting tasks too, as they stand: a review left at the quit stayed "awaiting review" and in
    // every new conversation's pick-up block (#698)
    const { ok, out } = await runWb($, ['stop', '--all', '--park-waiting', ...(await scope($))])
    if (!ok) $.ui.toast(`workbench: wb stop --all failed: ${brief(out)}`) // success: the reply says it (#1322)
    return { text: ok ? `Delegation stopped. ${out}` : `Delegation not stopped: ${out}`, context: [QUIT_NOTE(out, ok)] }
  })

  on('command.run', { command: MODELS }, async $ => {
    await scoutOffered($, await $.clock.now()) // the weekly check won't raise it again straight after
    mem.queued.push(SCOUT_NOTE) // a command may not start a turn itself: the next poll does
    return { text: 'Claude will check the models for each role now, GPU and API.' }
  })

  on('command.run', { command: UPDATE }, async $ => {
    const { ok, out } = await runWb($, ['update'], 600_000)
    if (!ok || !out.startsWith('updated')) {
      return { text: `workbench: ${out}` + (ok ? '' : '\n(If it got as far as updating, /reload-plugins starts using it.)') }
    }
    try {
      await $.command.run({ command: 'reload-plugins', args: '' }) // the new pane, now
      return { text: `workbench: ${out}` }
    } catch {
      return { text: `workbench: ${out}. Run /reload-plugins to start using it.` }
    }
  })

  on('command.run', { command: HAND_BACK }, async $ => {
    await workbenchMode($, false)
    const { ok, out } = await runWb($, ['takeover', '--all', '--user', ...(await scope($))])
    // A refusal (the coding gate) still takes over the rest: a turn for those whenever one was
    // taken, the refusals said with them. Keyed on the exit code alone, one refused task left
    // Claude no turn for the ones whose agents it had just stopped (#918).
    if (!ok && !/^took over /m.test(out)) { // nothing was handed over: no turn telling Claude to finish it
      $.ui.toast(`workbench: wb takeover --all failed: ${brief(out)}`)
      return { text: `wb takeover --all failed; the agents may still be running (wb status):\n${out}` }
    }
    // exit 0 with none taken over (each one parked by an earlier quit, or none open) is no turn
    // either: DONE_NOTE told Claude to finish tasks still parked (#1550, 2026-11-26)
    if (!/^took over /m.test(out)) return { text: `Nothing was handed over to Claude. ${out}\n${HAND_ONE}` }
    if (!ok) $.ui.toast(`workbench: some tasks weren't taken over: ${brief(out)}`)
    mem.queued.push(DONE_NOTE(out)) // a command may not start a turn itself: the next poll does
    return { text: `Agents stopped; Claude is taking over. ${out}` }
  })

  // Whoever compacts the main conversation, the summary keeps the delegation state: in a session
  // that delegated in this load, with a task of its own open. Any session's compaction got it
  // (the 2026-11-16 audit).
  on('session.compact', async ($, e, next) => {
    if (!e.agentId) mem.skillTurn = false // the blocks computed after it say everything again
    if (e.agentId || !mem.open.size || e.instructions?.includes(KEEP_ON_COMPACT)) return next(e)
    if (!mem.delegated && !(await read($, chat)).delegating) return next(e)
    return next({ ...e, instructions: [e.instructions, KEEP_ON_COMPACT].filter(Boolean).join('\n\n') })
  })

  // A fresh or just-compacted conversation is told which of its tasks are open, from wb itself.
  on('prompt.context', async ($, e, next) => {
    const r = await next(e)
    if (!mem.person) return r // a scripted run: no `wb status` before every prompt
    const st = await read($, chat)
    const isAway = await away($, st)
    const blocks = [...r.blocks]
    const want = await wanted($, st.delegating)
    // /workbench as the first message said them in its text already (#540)
    const had = mem.skillTurn ? mem.said : null
    mem.skillTurn = false
    const plan = tellPlan({ first: true, want, was: had ?? want, known: !!had, any: true, delegating: !!st.delegating, away: isAway })
    await setSaid($, plan.said)
    for (const say of plan.say) {
      if (say === 'mode') blocks.push({ name: 'workbench-mode', text: MODE_NOTE(mem.skill) })
      else if (say === 'coding') blocks.push({ name: 'claude-coding', text: CODING_RULES[want.coding] })
      else if (say === 'notes') blocks.push({ name: 'user-notes', text: NOTES_NOTE(want.notes) })
      else if (say === 'workflows') blocks.push({ name: 'dynamic-workflows', text: WORKFLOWS_RULE })
      else if (say === 'away') {
        blocks.push({ name: 'web-chat', text: AWAY_NOTE })
        mem.awayNoted = true
      } else if (say === 'wakeOff') blocks.push({ name: 'workbench-wake', text: WAKE_OFF_NOTE })
      else if (say === 'routes') { // read while delegating or with a task open, as `wanted` does
        const routes = await readRoutes($) // once any role has an API model (#1014)
        if (routes.api) blocks.push({ name: 'workbench-routes', text: ROUTES_NOTE(routes) })
      }
    }
    const { ok, out: status, aborted } = await runWb($, ['status', ...(await scope($))])
    if (!ok && !aborted) warnOnce($, 'status', `wb status failed: ${brief(status)}; wb doctor`)
    // with no task open, a run's lines still count: wb status prints them after `no open tasks`, and
    // dropping the whole text hid a run waiting at review after /clear or overnight, a scheduled
    // flow's (#1491, 2026-11-26); the OpenCode plugin's upkeep reads them the same way
    const open = status.startsWith('no open tasks') ? (status.match(/^run [\s\S]*/m)?.[0] ?? '') : status
    if (!ok || !open) return { blocks }
    return {
      blocks: [...blocks, {
        name: 'workbench',
        // the pick-up line is the story's, the OpenCode plugin's word for word (#1515)
        text: `Tasks delegated to agents with wb (the /workbench skill) are open:\n${open}\n${SAY['tasks.pickup']}`,
      }],
    }
  })

  // Only the main loop's turns: a subagent's run doesn't hold the prompt.
  on('turn.start', async ($, e, next) => {
    if (!e.agentId) {
      mem.busy = true
      mem.submitted = false
      chatTurnStart($)
      void poll($) // the chat page shows "working" now
    }
    return next(e)
  })
  on('turn.complete', async ($, e, next) => {
    if (!e.agentId) {
      // the dialogs at the terminal are over too, an interrupt's included (#959): the turn's
      // subagents ended with it, and one left open here would hold every later notice back
      for (const d of [...mem.termDialogs]) await termDialogDone($, d)
      mem.busy = false
      mem.skillTurn = false
      if (mem.chatQueued.size) void readQueued($)
      // the turn's answer to an owed web message goes first, then the turn-end record: raced, an
      // agent's wait could end on the record before the answer was in the thread (#1352). The
      // record takes its start size at once, before a next turn's start replaces it, and the end
      // waits on the post alone, as it did, not on the record's stat
      const owed = owedAtTurnEnd($, String(e.answer ?? '')).catch(() => {})
      void chatTurnEnd($, owed)
      await owed
      mem.sawTurn = true
      mem.turnEndedAt = await $.clock.now()
      void poll($) // held wakes and chat messages go now, not at the next tick
    }
    return next(e)
  })

  // The person's scrolling decides whether the pane follows: up to read stops it, back down to
  // the end starts it again.
  on('ui.scroll', { requestId: PANE }, async ($, e, next) => {
    if (e.origin.kind === 'person') endSplash($) // a key skips the opening animation, and still scrolls (#963)
    if (e.origin.kind === 'person') mem.follow = e.offset + e.bodyRows >= e.contentRows - 1
    return next(e)
  })

  // The user's own close (its mark, Esc) puts the pane away: the strip above the prompt stands in
  on('ui.close', async ($, e, next) => {
    if (e.id === PANE && e.origin?.kind === 'person') {
      endSplash($, false)
      await update($, pane, p => ({ ...p, minimized: true }))
      $.ui.invalidate('ui.render')
    }
    return next(e)
  })

  // Minimized: the strip, while there is something to bring back (workbench mode, or this session's
  // tasks): its name, a way back, and what the tasks are doing
  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (!(await read($, pane)).minimized || e.props?.hasSurvey) return next(e)
    const { delegating } = await read($, chat)
    const { parts } = taskParts(await read($, tasks), delegating)
    if (!delegating && !parts.length) return next(e)
    const { Box, Text, Button } = $.ui.resolve(e) as Row
    return (
      <Box flexDirection="row" columnGap={2}>
        <Box flexShrink={0}>
          <Button key="restore" label="▸ Workbench" plain onPress={() => { void restorePane($) }} />
        </Box>
        <Text dimColor wrap="truncate-end">{parts.length ? `${parts.join(' · ')} · ${STRIP_HINT}` : STRIP_HINT}</Text>
      </Box>
    )
  })

  // A key in the pane skips the opening animation; let through
  on('ui.focus', { requestId: PANE }, async ($, e, next) => {
    if (e.origin.kind === 'person') endSplash($) // Tab or a click skips the opening animation, and still moves (#963)
    return next(e)
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const kit = $.ui.resolve(e) as Row
    const { Box, Text, Raster, Button } = kit
    const list = await read($, tasks)
    const watched = await read($, live)
    const { link, delegating } = await read($, chat)
    if (mem.splash) { // the opening animation, in place of the Tasks page while it plays (#963)
      const props = (e.props ?? {}) as Row
      const [cols, rows] = [Number(props.bodyColumns ?? 0), Number(props.scroll?.bodyRows ?? 0)]
      const s = mem.splash
      const fits = (e as Row).surface === 'terminal' && props.placement !== 'widget'
      // where the page will put the apprentice and the modeline, as it stands now: the walk and the
      // bar go there. A row the page can't measure any more (a line that may wrap came) is no
      // row: an earlier exact one was measured without that line, so the page is taller now and
      // the bar would land above the real modeline (2026-11-21, review of #963). Before the words
      // leave, that makes them rise away; once they fall, the bar goes to the row the page
      // reaches at least.
      const shape = fits ? pageShape(list, watched, delegating, link, await $.clock.now(), cols) : null
      let target = shape ? splashTarget(shape, rows) : null
      if (target && target.bar === null && s.plan?.ending === 'fall') target = { ...target, bar: Math.min(rows - 1, shape!.atLeast - 1) }
      // a new size plans again, from the same frame: a dock's second draw is a row shorter than its
      // first (its close mark's row), and ending at a resize ended every opening there after one
      // frame (2026-11-21, the owner: "theres no splash"; seen at 64x44, then 64x43)
      if (target && (!s.plan || s.plan.cols !== cols || s.plan.rows !== rows)) {
        const ending = s.plan?.ending ?? null
        s.plan = plan(cols, rows, target) // none when it is too small now: the page then
        if (s.plan) s.plan.ending = ending
      } else if (target && s.plan) s.plan.target = target
      if (target && s.plan) {
        return <Raster key={SPLASH} columns={cols} rows={rows} cells={packed(frameCells(s.plan, s.f))} />
      }
      endSplash($, false) // too small, or not on a terminal: the page at once
    }
    // last, since the pane follows its end, a modeline as in Emacs or Vim: the pane's name, the
    // apprentice's face and the web app's link (#1283: Stats and Settings are there), in the
    // theme's own colours so it reads on a dark or a light terminal (the user, 2026-11-21); in a
    // narrow pane the link alone, or the link on rows of its own under the name (modeline.ts)
    const bodyCols = Number(((e.props ?? {}) as Row).bodyColumns ?? 0) || Number(e.viewport?.columns ?? 0) || 60
    const ml = modeline(link, bodyCols)
    const header = (
      <Box key="modeline" flexDirection="column">
        <Box backgroundColor="claude" paddingX={1}>
          <Text color="inverseText" bold wrap="truncate-end">{ml.bar}</Text>
        </Box>
        {ml.rows.map((row, i) => (
          <Box key={`modeline-url-${i}`} backgroundColor="claude">
            <Text color="inverseText" wrap="truncate-end">{row}</Text>
          </Box>
        ))}
      </Box>
    )
    const props = (e.props ?? {}) as Row
    const cols = Math.max(30, Number(props.bodyColumns ?? e.viewport?.columns ?? 60))
    const now = await $.clock.now()
    const spin = SPIN[Math.floor(now / 500) % SPIN.length] ?? '✻'

    // The apprentice, in pixels where the pane has the rows for it (it takes six), else its face
    // on the line. Drawing it in pixels arms the timer that animates it. The stage keeps its
    // height beside a long history, and the bubble wraps its words onto more rows rather than
    // cutting them off: the pane can't be made wider.
    const pixels = (e as Row).surface === 'terminal' && Number(props.scroll?.bodyRows ?? 24) >= 20 && cols >= 40
    const bodyRows = Number(props.scroll?.bodyRows ?? 24)
    void capNow($, list)
    const page = pageParts(list, watched, delegating, now, spin, cols, pixels, bodyRows, capOf(list))
    const { from, history, meta, routed, sp, busy } = page
    const lines = (text: string, props: Record<string, unknown>) =>
      wrapWords(text, page.room).map(l => <Text {...props} wrap="truncate-end">{l}</Text>)
    const said = [
      ...lines(page.say, busy ? LINE_STYLE.activity : { color: sp.color, bold: true }),
      ...(page.more ? lines(page.more, { dimColor: true }) : []),
      ...(page.clock ? lines(page.clock, { dimColor: true }) : []),
    ]
    let stage
    if (pixels) {
      const frame = frameOf(sp.pose, mem.tick)
      mem.posed = sp.pose
      mem.shown = frame
      stage = (
        <Box flexDirection="row" marginTop={1} flexShrink={0}>
          <Box flexShrink={0}>
            <Raster key={APPRENTICE} columns={COLUMNS} rows={ROWS} cells={cells(frame)} />
          </Box>
          <Box flexDirection="column" flexShrink={0}>
            {Array.from({ length: MOUTH + 1 }, (_, i) => (
              <Text color={sp.color}>{i === MOUTH ? '─' : ' '}</Text>
            ))}
          </Box>
          <Box flexDirection="column" flexGrow={1} flexShrink={0} marginTop={MOUTH - 1} borderStyle="round"
            borderColor={sp.color} paddingX={1}>
            {said}
          </Box>
        </Box>
      )
    } else {
      stage = (
        <Box flexDirection="row" marginTop={list.length ? 1 : 0} flexShrink={0}>
          <Box flexShrink={0}>
            <Text color={sp.color}>{FACE[sp.pose]} </Text>
          </Box>
          <Box flexDirection="column" flexGrow={1}>{said}</Box>
        </Box>
      )
    }

    return (
      <Box flexDirection="column">
        {page.rows.length ? (
          // every task at a glance (#1048): one or two rows each, none wider than the pane; a press
          // on a task's first row follows it
          <Box flexDirection="column" marginBottom={1}>
            {page.rows.map(r => {
              const runs = r.parts.length
                ? r.parts.map(p => <Text color={p.color} dimColor={p.dim} bold={p.bold} underline={p.underline}>{p.text}</Text>)
                : [<Text> </Text>]
              return r.first
                ? <Button key={`task-${r.task}`} plain onPress={() => { void followTask($, r.task) }}><Text wrap="truncate-end">{runs}</Text></Button>
                : <Text wrap="truncate-end">{runs}</Text>
            })}
          </Box>
        ) : null}
        {watched.task && (
          <Box flexDirection="column">
            <Text dimColor>{clip(`── ${watched.task} · ↑↓ scrolls ` + '─'.repeat(cols), cols)}</Text>
            {meta && <Text dimColor>{meta.t}</Text>}
            {routed && <Text dimColor>{routed.t}</Text>}
            {from > 0 && <Text dimColor>… {from} earlier lines: `wb watch {watched.task}` has them all</Text>}
            {history.map(l => (
              <Text {...(LINE_STYLE[l.s] ?? {})}>{l.t || ' '}</Text>
            ))}
          </Box>
        )}
        {stage}
        {page.capLine && <Text wrap="truncate-end">{page.capLine.map(p => <Text color={p.color} dimColor={p.dim}>{p.text}</Text>)}</Text>}
        {header}
      </Box>
    )
  })
}
