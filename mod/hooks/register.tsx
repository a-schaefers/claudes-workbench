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
// closed, none waiting for Claude) the mod compacts early once Claude is COMPACT_AT of the way to
// the engine's own compaction, so a long delegation session sheds finished tasks' detail at a
// clean point instead of mid-review.
const COMPACT_AT = 0.5
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

import type { WbChat, WbLine, WbLive, WbMedia, WbModels, WbPane, WbRoutes, WbSaid, WbStats, WbTask, WbTop } from '../types'
import { cells, COLUMNS, FACE, MOUTH, ROWS } from './apprentice'
import type { Pose } from './apprentice'
import { frameCells, frameCount, packed, plan, SPLASH_MS } from './splash'
import type { Plan, Target } from './splash'
import { activity, loopLength, TICKER_MS, tickerCells } from './modeline'
import { ago, apiWorking, bar, capRow, clip, compactWanted, ROLE_WORDS, taskLines } from './tasks'
import type { Part as RowPart } from './tasks'

const PANE = 'workbench'
// Nothing waits on a timer for news: `wb events` (inotify) prints a line the moment a task, a
// notification or a chat message changes, and each line runs poll() at once. The timer only
// redraws running tasks' clocks, and checks in now and then should the watcher be gone.
const POLL_MS = 1500 // the clock's redraw while a task runs
const SAFETY_MS = 30_000 // a poll at least this often, whatever the watcher says
const KEEP_CLOSED_MS = 30 * 60 * 1000 // closed tasks stay in the pane for half an hour
const OLDEST_WAKE_MS = 60 * 60 * 1000 // never wake for a notification older than an hour

// Change the tag when WbTask's fields change: a reload declines what the earlier shape stored.
const tasks = atom({ plugin: 'workbench', key: 'tasks' } as const, [] as WbTask[], {
  shape: 'tasks-11',
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

// The web chat (`wb chat serve`): the user's side door while away from the terminal. A session
// listens while workbench mode is on in it (`delegating`), from /workbench until a quit command;
// with several, the first to claim chat/listener.json does, and the others stand by. A message is
// submitted as a prompt (so the terminal shows it too) with a receipt the page shows at once.
// The user turns it on or off (`wb chat on|off`, or the pane's row; chat/switch.json): off by
// default, and then no session listens, sends it prompts or shows its link (the user, 2026-11-19).
const chat = atom({ plugin: 'workbench', key: 'chat' } as const,
  { key: '', delegating: false, url: '', web: false } as WbChat, { shape: 'chat-4' })
// The pane's view: a Tasks tab and a Settings tab, as backseat-driver's (the user, 2026-11-19: the
// settings belong on a page of their own). A settings row names its value, a press opens its
// options under it as Buttons (a click or the ring's Enter picks), the option under the ring or the
// pointer shows its sentence. A Select there opened on a click and could pick nothing (2026-10-05),
// and its sentence followed only the saved choice (the user, 2026-11-19).
// Closed by the user (its mark, Esc), the pane is minimized, as backseat-driver's is (the user,
// 2026-11-19): a strip above the prompt stands in for it, a press there or /workbench brings it back.
const pane = atom({ plugin: 'workbench', key: 'pane' } as const,
  { tab: 'tasks', open: '', focus: '', minimized: false } as WbPane, { shape: 'pane-1' })
// Whether this session's first /workbench has come (#963): the opening animation plays at that
// one alone. An atom, so a module reload (which runs session.start again with `mem` empty) can't
// play it again; /workbench again and a bring-back from the strip find it set.
const splashed = atom({ plugin: 'workbench', key: 'splash' } as const, false, { shape: 'splash-1' })
const STRIP_HINT = 'minimized · restore: click, or Ctrl+X Tab then Enter'
// Stats between them (#406): looked at more often than Settings
const MODELINE = "Claude's Workbench" // the bar under the tabs
const TABS: [string, string, string][] = [['tasks', 'Tasks', '1'], ['stats', 'Stats', '2'], ['settings', 'Settings', '3']]
// The Stats tab's history (`wb stats --json`), read while the tab is shown: when it is first
// shown in a load (`mem.statsLoaded`: the atom outlives a reload), on each press of the tab, again
// when a file it is made from changes (refreshStats), and once the feed's `until` (the next local
// midnight) has passed. No timer.
const statsFeed = atom({ plugin: 'workbench', key: 'stats' } as const, { key: '', feed: null } as WbStats,
  { shape: 'stats-1' })
// The Stats tab's live view (#408): one `wb top --json` child, only while the tab is on screen
// with the live view on, ended the moment it isn't; each line its newest tick
const topNow = atom({ plugin: 'workbench', key: 'top' } as const, { snap: null, busy: {} } as WbTop,
  { shape: 'top-1' })
const BUSY_KEEP = 60 // busy % samples kept per GPU for the sparkline: two minutes at 2 s
const TOP_RETRY_MS = 30_000 // a `wb top` that ended by itself (wb missing, failing) is tried again this long after, doubling
// A `wb stats --json` that failed or timed out is tried again after STATS_RETRY_MS, doubling to
// RETRY_MAX_MS; each poll ran it again, a full-history read a core kept busy on a slow host (#553)
const STATS_RETRY_MS = 60_000
const RETRY_MAX_MS = 10 * 60_000
const CHAT_STALE_MS = 15_000 // as wb's LISTENING_FOR: a listener silent this long has gone
// the user counts as away this long after they last used the web chat: a web message, or a page
// in use (chat/away.json's `at`, written by `wb chat serve`'s /api/active, #959)
const AWAY_MS = 6 * 3600 * 1000
// ...or for as long as this session has a backlog task open: a night's batch outlasts six hours,
// and its permission prompts then waited in the terminal until morning (#110, 2026-11-18)
export function awayNow(webAt: number, now: number, list: WbTask[], isMine: (t: WbTask) => boolean): boolean {
  if (!webAt) return false
  if (now - webAt < AWAY_MS) return true
  return list.some(t => t.backlog && isMine(t) && (ACTIVE.has(t.phase) || waitsOnClaude(t)))
}
// Whether the weekly model check and the automatic optimize may run: no task anywhere active (the
// GPU's, and a check's question beside a running task's), and none of this session's waiting on
// Claude lately. Any task open at any age once gated both, so one stuck, failed or unreviewed
// task left after a quit stopped them for good (#541); one idle past REWAKE_MAX_MS waits for a
// session long gone, as rewake judges it; a parked one waits for no one.
export function quietForJobs(list: WbTask[], now: number, isMine: (t: WbTask) => boolean): boolean {
  return !list.some(t => ACTIVE.has(t.phase) ||
    (waitsOnClaude(t) && isMine(t) && now - t.updatedAt <= REWAKE_MAX_MS))
}
// Away is the user's, not one session's (#688): the listening session records the last web
// message in chat/away.json and a prompt typed in any session clears it, so every delegating
// session relays its dialogs and says AWAY_NOTE. Kept in the listener's own state, only it did:
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
  return st.web && awayNow(await webAt($), await $.clock.now(), await read($, tasks), mine)
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

const ACTIVE = new Set(['queued', 'running', 'testing', 'asking'])
const NEEDS = new Set(['review', 'stuck', 'failed', 'asking'])
// Waiting on Claude, unless parked: stopped by /workbench-quit (`wb stop --all`, status `parked`),
// which the status line, rewake, the boundary and the jobs' gate don't count. It stayed "awaiting
// review" and kept the weekly check off for good (#541); the pane and `wb status --all` still show it.
const waitsOnClaude = (t: WbTask) => NEEDS.has(t.phase) && !t.parked
const COLOR: Record<string, string> = {
  queued: 'subtle', running: 'claude', testing: 'suggestion', asking: 'warning',
  review: 'success', stuck: 'error', failed: 'error', accepted: 'inactive', discarded: 'inactive',
}

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
const BLINK_EVERY = 11 // frames
const STROKE_EVERY = 6 // frames: the wrench comes up once in this many, else rests at its side
const APPRENTICE = 'apprentice' // its Raster's key, for $.ui.blit
type Speech = { pose: Pose; say: string; more: string; color: string }

const speechOf = (focus: WbTask | undefined, doing: string, list: WbTask[], on: boolean): Speech => {
  if (!focus) {
    const more = on ? 'Ask Claude for anything: the coding comes to me.' : '/workbench turns workbench mode on.'
    if (list.length) return { pose: 'rest', say: 'All caught up.', more, color: 'inactive' }
    return on
      ? { pose: 'rest', say: 'Workbench mode is on.', more, color: 'claude' }
      : { pose: 'sleep', say: 'No delegated tasks.', more, color: 'inactive' }
  }
  const n = focus.name
  switch (focus.phase) {
    // each handoff names who acts next: "done" read as tests passed, accepted or landed (#103)
    case 'review': return { pose: 'tada', say: focus.tests === 'pass' ? `Local tests passed on ${n}.` : `${n} is done.`,
      more: 'Claude reviews next.', color: 'success' }
    case 'asking': return { pose: 'ask', say: `I asked Claude about ${n}.`, more: 'Claude answers next.', color: 'warning' }
    case 'stuck': return { pose: 'slump', say: `I'm stuck on ${n}.`, more: focus.last, color: 'error' }
    case 'failed': return { pose: 'slump', say: `${n} failed.`, more: focus.last, color: 'error' }
  }
  // the hands move while it writes or tests; everything else is the model thinking or waiting
  const working = /^(Writing|Running)/.test(doing)
  return { pose: working ? 'work' : 'think', say: doing || focus.last || `${focus.phase}…`, more: '', color: COLOR[focus.phase] ?? 'claude' }
}

// The frame a pose shows at a tick: a stroke of the wrench now and then while working, else a
// blink now and then.
const frameOf = (pose: Pose, tick: number): Pose => {
  if (pose === 'work') return tick % STROKE_EVERY ? 'swing' : 'work'
  if (pose === 'sleep' || pose === 'slump') return pose
  return tick % BLINK_EVERY === 0 ? 'blink' : pose
}

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
export function pageShape(list: WbTask[], watched: WbLive, delegating: boolean, now: number,
  cols: number, bodyRows = 24): { rows: number | null; atLeast: number; stage: number; tail: number | null } {
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
  // line, the tabs' marginTop, the tabs, the modeline (the week's saving had a line too, #1322)
  const tail = Math.max(ROWS, MOUTH + 1, MOUTH - 1 + 2 + said) + (page.capLine ? 1 : 0) + 3
  return { rows: exact ? stage + tail : null, atLeast: stage + tail, stage, tail }
}

// Where the splash's last frame puts the apprentice and the modeline's bar, in a pane body of
// `body` rows: the page's own rows when it fits (a docked pane draws it from the top; an inline
// one, sized to it, keeps its top where it was), else its end, which the pane follows. A page
// that may wrap and doesn't surely overflow has no exact bar row: the words rise away instead.
export function splashTarget(shape: ReturnType<typeof pageShape>, body: number): Target {
  if (shape.rows !== null && shape.rows <= body) return { stage: shape.stage, bar: shape.rows - 1 }
  // more than the pane surely: its end, the modeline the pane's last row (the stage as near as known)
  if (shape.atLeast > body) return { stage: body - (shape.tail ?? shape.atLeast - shape.stage), bar: body - 1 }
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

const readJson = async ($: any, path: string): Promise<Row> =>
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

// A line of coloured parts broken into lines of at most n characters at spaces, each further
// line indented: the Stats tab's live rows were cut at the pane's edge (truncate-end), so a GPU
// with a long name lost its busy bar, watts and sparkline, and a model its load time, to "…"
// (2026-11-21). A word longer than a line stays whole; the Text still cuts it at the edge.
type Part = { color?: string; dim?: boolean; bold?: boolean; text: string }
export const wrapParts = (parts: Part[], n: number, indent = 2): Part[][] => {
  const lines: Part[][] = []
  let line: Part[] = []
  let width = 0
  const add = (p: Part, text: string) => {
    const last = line[line.length - 1]
    if (last && last.color === p.color && last.dim === p.dim && last.bold === p.bold) last.text += text
    else line.push({ ...p, text })
    width += text.length
  }
  for (const p of parts) {
    for (const tok of p.text.split(/([^\S\u00a0]+)/).filter(Boolean)) { // NB holds a unit whole
      if (/^[^\S\u00a0]/.test(tok)) {
        if (width > (lines.length ? indent : 0)) add(p, tok)
        continue
      }
      if (width > (lines.length ? indent : 0) && width + tok.length > n) {
        const end = line[line.length - 1]
        if (end) end.text = end.text.trimEnd()
        lines.push(line)
        line = [{ text: ' '.repeat(indent) }]
        width = indent
      }
      add(p, tok)
    }
  }
  if (line.length) {
    const end = line[line.length - 1]
    end.text = end.text.trimEnd()
    lines.push(line)
  }
  return lines.length ? lines : [[{ text: '' }]]
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
const batchOf = (row: Row): string[] | null =>
  Array.isArray(row.tasks) && row.tasks.length ? row.tasks.map(String) : null
const RUNNING = new Set(['queued', 'running', 'testing'])
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
  `Ask the user: Agents back to Both or API (the workbench pane, or \`wb route all both\` on their yes), ` +
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
const outsideTheStep = (row: Row) => typeof row.cause === 'string' && !THE_STEP.has(row.cause)
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
// ask_mcp.NO_ANSWER's opening: what the runner answers a question nobody answered in time
const NO_ANSWER = 'No answer arrived in time.'
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
// Claude starts at once; and the mod wakes Claude for no run row yet, so the wake is a background
// `wb wait <run>` (the review of #1278, 2026-11-25: told "don't poll", Claude was never woken)
const WORKFLOWS_RULE = 'Dynamic workflows are on (the user\'s choice; `wb deepmode` shows it): run a substantive ' +
  'request as a deep workflow on their agents, as the /workbench skill\'s *Deep workflows* says: pick a flow ' +
  '(`wb run flows`), say it in at most three lines, start it once with `wb run start`, then wait for its notices ' +
  'with `wb wait <run>` in the background, again after each. Never fan out by starting several tasks at once ' +
  'yourself: the run starts its items, each on its own role\'s model, never one you pick, and the GPU lease, the ' +
  'provider slots and the day cap decide what runs together. Review each result on its own merits; merge logic ' +
  'first, then tests, then UI or docs.'
const WORKFLOWS_OFF = 'Dynamic workflows are now off (the user\'s choice; `wb workflows` shows it): run one task ' +
  'at a time again, as the /workbench skill says.'

async function workflowsMode($: any): Promise<string> {
  try {
    return JSON.parse(await $.fs.read(`${mem.root}/workflows.json`)).mode === 'on' ? 'on' : 'off'
  } catch {
    return 'off'
  }
}

// The pane's Claude coding row (#93), in order; the choice is wb's (`wb coding`), the sentence is
// what the pane shows for the option under the ring or the pointer. The default says so, to show
// how it was at the start (the user, 2026-11-19)
const CODING_STOPS: [string, string, string][] = [
  ['only', 'Only when I say', 'You tell Claude when to write code.'],
  ['ask', 'Ask me first', 'Claude asks you in a dialog before it edits code in a task.'],
  ['decide', 'Let Claude decide (default)', 'Claude uses its judgment to guide the agent or make a fix itself.'],
]

// The web chat's row: the choice is wb's (`wb chat on|off`, chat/switch.json), off by default
const CHAT_STOPS: [string, string, string][] = [
  ['off', 'Off (default)', 'No web server: Claude is reached in this terminal only.'],
  ['on', 'On', 'A localhost web server to interact with Claude.'],
]

// The probe experiment's row (ARCHITECTURE.md, *Experiments*): wb's choice (`wb experiment probe
// on|off`, experiments.json), off by default. A task keeps what it started with. Off and on, on
// is large files only (the owner, 2026-11-22, #857: its levels adaptable, moderate and aggressive
// went; on is what moderate did). On's sentence says "with probe", where a working probe's
// version goes.
const PROBE_STOPS: [string, string, string][] = [
  ['off', 'Off (default)', 'The agent reads whole files, as without the experiment.'],
  ['on', 'On (large files only)', 'The agent may extract one function of a file over 1,500 lines with probe, instead of reading it whole (github.com/probelabs/probe).'],
]
// probe's levels from before it was off and on, each read as on (context.OLD_LEVELS)
const PROBE_OLD_LEVELS = ['adaptable', 'moderate', 'aggressive']

// What wb says of it: off, on and working (with probe's version), or on but not set up
// (no probe with `symbols` on PATH), and whether on is recommended (context.PROBE_RECOMMENDED,
// set once a measurement says so). The row says which, since it read "On" whether or not anything
// changed (the user, 2026-11-20). Asked of wb (`wb experiment probe`) only while the Settings tab
// draws, at most every PROBE_RECHECK_MS, and again after each pick: a probe installed meanwhile
// shows without a restart. Off too, since the recommendation is wb's to give; off, wb runs no probe.
const PROBE_RECHECK_MS = 30_000
type ProbeState = { state: 'off' | 'ready' | 'missing' | 'unknown'; version: string; recommended: boolean }
function probeSaid(out: string): ProbeState {
  const line = out.split('\n').find(l => l.startsWith('probe:')) ?? ''
  const recommended = line.includes('on is recommended')
  if (line.startsWith('probe: off')) return { state: 'off', version: '', recommended }
  if (line.includes('not set up')) return { state: 'missing', version: '', recommended }
  if (line.startsWith('probe: on'))
    return { state: 'ready', version: /and working \(([^)]*)\)/.exec(line)?.[1] ?? '', recommended }
  return { state: 'unknown', version: '', recommended }
}

async function probeStatus($: any): Promise<ProbeState> {
  const now = await $.clock.now()
  if (mem.probe && now - mem.probe.at < PROBE_RECHECK_MS) return mem.probe.said
  const { ok, out, aborted } = await runWb($, ['experiment', 'probe'], 15_000)
  const said = ok ? probeSaid(out) : { state: 'unknown' as const, version: '', recommended: false }
  if (aborted) return mem.probe?.said ?? said // not kept: the next draw asks again (#1142)
  mem.probe = { at: now, said }
  return said
}

// The row's options: on marked recommended when wb says so, and, chosen, whether it works
function probeStops(s: ProbeState, mode: string): [string, string, string][] {
  return PROBE_STOPS.map(([v, label, said]): [string, string, string] => {
    const named = v === 'on' && s.recommended ? `${label} (recommended)` : label
    if (v !== mode || v === 'off') return [v, named, said]
    if (s.state === 'ready') return [v, `${named}, working`, said.replace('with probe', `with probe${s.version ? ` (${s.version})` : ''}`)]
    if (s.state === 'missing') return [v, `${named}, not set up`, "probe isn't installed, so nothing changes yet: Claude can set it up."]
    return [v, named, said]
  })
}

// Claude's turn when the user turns it on and probe isn't there: offer to set it up (the user,
// 2026-11-20: "let Claude offer to help the user get it fully set up"). It was about 150 tokens,
// with a toast beside it, and a toast for one that worked too (#1322): the row says which.
const PROBE_SETUP_NOTE = "[workbench] probe isn't on PATH: offer to install its release binary " +
  '(github.com/probelabs/probe/releases), then `wb experiment probe`.'

// Turns the probe experiment on or off through wb; on but not set up, it asks Claude to offer the
// setup (once while it's queued). The row says the rest (`, working` or `, not set up`).
async function setProbe($: any, value: string) {
  const { ok, out } = await runWb($, ['experiment', 'probe', value])
  mem.probe = null // the row asks wb again
  if (!ok) { $.ui.toast(`workbench: ${brief(out)}`); return }
  if (value === 'off') return
  if (probeSaid(out).state === 'missing' && !mem.queued.includes(PROBE_SETUP_NOTE)) mem.queued.push(PROBE_SETUP_NOTE)
}

// experiments.json's probe setting as wb reads it (context.setting): "on", true or an old level
// is on, anything else off
async function probeMode($: any): Promise<string> {
  try {
    const v = JSON.parse(await $.fs.read(`${mem.root}/experiments.json`)).probe
    return v === 'on' || v === true || PROBE_OLD_LEVELS.includes(v) ? 'on' : 'off'
  } catch {
    return 'off'
  }
}

// Progress checks, off by default since each one wakes Claude: wb's choice, `wb progress`
// (progress.json). The live view and its refresh had rows here too, on by default, each wb's
// (`wb stats set`): the pane keeps them on, and Stats recording is always on (#1322).
const PROGRESS_OFF: [string, string, string] = ['off', 'Off (default)', 'No progress checks: each one wakes Claude, which spends its tokens.']
const PROGRESS_STOPS: [string, string, string][] = [
  PROGRESS_OFF,
  // the defaults are progress.py's DEFAULT_MIN and TYPICAL_MIN (#724)
  ['on', 'On', 'A task past its typical length (20 min trivial, 45 significant, until 3 of the size are timed on its model) gets a check, again at 2x and 4x, each with one recommendation, waking Claude.'],
]
// With a threshold set (`wb progress on --after N`, kept through the row's bare on/off) On says
// it: the typical length it named instead was the wrong one (#578)
function progressStops(after: number): [string, string, string][] {
  if (!after) return PROGRESS_STOPS
  return [PROGRESS_OFF, ['on', `On, after ${after} min`,
    `A task past ${after} min of active work gets a check and one recommendation, waking Claude.`]]
}
const SETTING_ROWS = /^(coding|chat|probe|progress|splash|agents|busy|workflows|(?:role|gpu|api|think|media)-[a-z]+|size-[^:]+):/ // the rows whose ring the pane follows

// One settings row: a Button naming the choice, which opens its options under it, each a Button
// (a click, or Enter on the ring), the chosen one marked. Under them the sentence of the option
// the ring is on, else the chosen one's; the pointer on an option lays its own over it (a hover
// reveal: no hook runs on hover). A pick folds the row again, which redraws it; the option
// already chosen only folds it, running nothing (#578: picking On again ran a bare
// `wb progress on`, which then dropped a `--after` threshold).
function settingRow(kit: Row, $: any, name: string, title: string, stops: [string, string, string][],
  value: string, view: WbPane, pick: (value: string) => Promise<unknown>) {
  const { Box, Text, Button } = kit
  // a row longer than the pane goes on under its words, not under its mark (hangWords): wrapped by
  // the pane, `▸ Complex coding API model: Claude Haiku 5.5 ·` went on with "Anthropic (195K) · …"
  // at the left edge, read as a row of its own (#1048's review at 120 columns); the label stays the
  // control's name
  const row = (props: Row, label: string, indent: number) => mem.settingsCols && label.length > mem.settingsCols
    ? <Button {...props} label={label} plain><Text>{hangWords(label, mem.settingsCols, indent).join('\n')}</Text></Button>
    : <Button {...props} label={label} plain />
  const chosen = stops.find(s => s[0] === value) ?? stops[stops.length - 1]
  if (view.open !== name)
    return row({ key: name, onPress: () => { void update($, pane, p => ({ ...p, open: name, focus: '' })) } }, `▸ ${title}: ${chosen[1]}`, 2)
  const ringed = stops.find(s => `${name}:${s[0]}` === view.focus) ?? chosen
  const width = Math.max(...stops.map(s => s[2].length))
  return (
    <Box flexDirection="column">
      {row({ key: name, onPress: () => { void update($, pane, p => ({ ...p, open: '', focus: '' })) } }, `▾ ${title}: ${chosen[1]}`, 2)}
      {stops.map(([v, label]) => row({ key: `${name}:${v}`, dimColor: v !== chosen[0], hover: { scope: `wb-${name}-${v}` },
        onPress: () => { void (v === chosen?.[0] ? Promise.resolve() : pick(v)).then(() => update($, pane, p => ({ ...p, open: '', focus: '' }))) } },
        `  ${v === chosen[0] ? '❯' : ' '} ${label}`, 4))}
      {/* in from the edge as the options, its wrapped lines too (#1048's review) */}
      <Box key={`${name}-says`} paddingLeft={4}>
        <Box>
          <Text dimColor>{ringed[2].padEnd(width)}</Text>
          {stops.map(([v, , said]) => (
            <Box position="absolute" top={0} left={0} display="none" hover={{ display: 'flex', scope: `wb-${name}-${v}` }}>
              <Text dimColor>{said.padEnd(width)}</Text>
            </Box>
          ))}
        </Box>
      </Box>
    </Box>
  )
}

// A read-only line among the settings rows: dim, in from the edge by the `▸ ` of the rows, its
// wrapped lines too (a hanging indent: "  Connected: …" went on at the pane's left edge, under the
// rows' marks, #1048's review at 120 columns)
function infoLine(kit: Row, key: string, text: string) {
  const { Box, Text } = kit
  return <Box key={key} paddingLeft={2}><Text dimColor>{text}</Text></Box>
}

// A section's heading, on both tabs as Stats' Now: bold, in the theme's claude colour; `note` dim after it
function heading(kit: Row, text: string) {
  const { Text } = kit
  return <Text bold color="claude">{text}</Text>
}

// A line in lines of at most `n`, each after the first indented `indent`, as Now's rows (wrapParts)
// wrap: the Stats history's `Label: text`, whose label then stands alone at the left edge, and a
// long settings row (settingRow). The text's own leading spaces stay on its first line.
export function hangWords(text: string, n: number, indent = 2): string[] {
  const lead = /^ */.exec(text)![0]
  const [first, ...rest] = wrapWords(text.slice(lead.length), n - lead.length)
  const head = lead + first!
  return rest.length ? [head, ...wrapWords(rest.join(' '), n - indent).map(l => ' '.repeat(indent) + l)] : [head]
}

// Turns the web chat on or off through wb, which keeps the choice (and starts a server for on),
// then reads it back at once: the link shows (or goes) with the pick, not at the next poll
// wb's reason is shown when it fails, as setProbe does: ignored, the row read On and the pane
// pinned a link that gave a 401 or nothing on the phone (#690). wb leaves the switch off then,
// so no link is pinned.
async function setWeb($: any, value: string) {
  const { ok, out } = await runWb($, ['chat', value], 15_000)
  if (!ok) $.ui.toast(`workbench: ${brief(out)}`)
  mem.chatUrlAt = 0
  await readSwitch($)
  await chatLink($)
}

// stats.json's settings as wb reads them (stats.py): each its default unless the file says
// otherwise, a mistyped value ignored
async function statsSettings($: any): Promise<{ live: boolean; every: number }> {
  let d: any = {}
  try { d = JSON.parse(await $.fs.read(`${mem.root}/stats.json`)) } catch { /* the defaults */ }
  const on = (v: unknown) => (typeof v === 'boolean' ? v : true)
  return { live: on(d?.live), every: [1, 2, 5].includes(d?.every) ? d.every : 2 }
}

// progress.json as wb reads it (progress.py): on only when it says so; a fixed threshold when
// `after` is a number other than 0 (wb's falsy), else 0
async function progressSettings($: any): Promise<{ on: boolean; after: number }> {
  let d: any = {}
  try { d = JSON.parse(await $.fs.read(`${mem.root}/progress.json`)) } catch { /* off */ }
  const after = typeof d?.after === 'number' && Number.isFinite(d.after) ? d.after : 0
  return { on: d?.on === true, after }
}

// The Stats tab's history: `wb stats --json`, only while the tab is shown, and only when what it
// is made from changed since the last read (results.jsonl: a task closed; stats.json: a setting;
// optimize.json, bench/runs.jsonl and speed.json, which the feed reads too and a stale tab kept for
// good, #549), the day it was counted from has ended (its `until`: last week's numbers stayed for
// days, #575), or not yet in this load.
// `force` on the tab's press and after a settings pick. A stat a file is all a poll costs while
// the tab is shown; nothing at all while it isn't.
async function refreshStats($: any, force = false) {
  if (mem.statsReading || (await read($, pane)).tab !== 'stats') return
  const shown = ((await $.ui.panes().catch(() => [])) as Row[]).find(p => p.id === PANE)
  if (!shown?.isShown) return
  const stamp = async (f: string) => {
    try { const s = await $.fs.stat(f.startsWith('/') ? f : `${mem.root}/${f}`); return `${s.size}@${s.mtimeMs}` } catch { return '-' }
  }
  // on a network home each machine keeps its optimize.json and speed.json in machines/<id>/
  // (store.machine_dir, #894): every machine's, since which one is this is wb's to say
  const machines = await $.fs.list(`${mem.root}/machines`).catch(() => []) as { name: string; kind: string }[]
  const own = machines.filter(e => e.kind === 'dir').flatMap(e =>
    ['optimize.json', 'speed.json'].map(f => `machines/${e.name}/${f}`))
  const key = (await Promise.all(['results.jsonl', 'stats.json', 'optimize.json', 'bench/runs.jsonl',
    'speed.json', ...own].map(stamp))).join(' ')
  const had = await read($, statsFeed)
  const now = await $.clock.now()
  const dayOver = typeof had.feed?.until === 'number' && now / 1000 >= had.feed.until
  if (!force && had.feed && key === had.key && mem.statsLoaded && !dayOver) return
  if (!force && now < mem.statsRetryAt) return // it failed: not at every poll (#553)
  mem.statsReading = true
  // wb missing, failing or slow: the last feed stays, the reason is said, and the next try waits
  const failed = (why: string) => {
    mem.statsBackoff = Math.min(mem.statsBackoff ? mem.statsBackoff * 2 : STATS_RETRY_MS, RETRY_MAX_MS)
    mem.statsRetryAt = now + mem.statsBackoff
    mem.statsWhy = why
    $.ui.invalidate('ui.render')
  }
  try {
    const r = await $.process.run([mem.wb, 'stats', '--json'], { timeoutMs: 30_000 })
    if (r.exitCode !== 0) return failed(brief(r.stderr || r.stdout) || `exit ${r.exitCode}`)
    const feed = JSON.parse(r.stdout)
    if (!feed || typeof feed !== 'object') return failed('not a feed')
    mem.statsWhy = ''
    mem.statsBackoff = mem.statsRetryAt = 0
    // the models' labels and the providers' names the tab says them by (namesNow), when not read
    // in the last ROLES_CACHE_MS: a read of the feed only, never a poll's
    await rolesNow($).catch(() => null)
    await update($, statsFeed, () => ({ key, feed }))
    mem.statsLoaded = true
  } catch (err) {
    if (!isAbort(err)) failed(brief(String(err))) // an abort isn't wb's failure: no line, no wait (#1142)
  } finally {
    mem.statsReading = false
  }
}

// Whether the live view should run now: the Stats tab on screen (its tab, the pane shown and not
// minimized) with the live view on. Anything else and `wb top` isn't running.
async function topWanted($: any): Promise<boolean> {
  const view = await read($, pane)
  if (view.tab !== 'stats' || view.minimized) return false
  const shown = ((await $.ui.panes().catch(() => [])) as Row[]).find(p => p.id === PANE)
  return !!shown?.isShown && (await statsSettings($)).live
}

// Starts or ends the live view's child to match topWanted: at each poll, on a tab's press, a
// minimize and a Live view pick, and on each line it prints (so leaving the tab ends it within a
// tick, whatever else happens). Every await comes first and the check and the spawn after them,
// with none between: a poll and a tab press at once both saw no child across the clock's await,
// both started one, and only the second was ever ended by a close (#546)
async function syncTop($: any) {
  const gen = mem.topGen
  const now = await $.clock.now()
  const want = await topWanted($)
  if (gen !== mem.topGen) return // started or stopped meanwhile: that call's view is newer
  if (want && !mem.top && now >= mem.topRetryAt) runTop($)
  else if (!want && (mem.top || mem.topKids.size)) stopTop($)
}

// Ends every child started, and forgets the last tick: shown again, the tab said a finished task
// was still "running · round 2" under Now until the next child's first line (#552)
function stopTop($: any) {
  mem.top = null
  mem.topGen += 1
  for (const child of mem.topKids)
    try { void child?.return?.(undefined)?.catch?.(() => {}) } catch { /* already ended */ }
  mem.topKids.clear()
  void update($, topNow, () => ({ snap: null, busy: {} })).catch(() => {})
}

// Starts the child and claims it at once (mem.top, mem.topKids), before any await; pumpTop reads it
function runTop($: any) {
  mem.topGen += 1
  const child = $.process.spawn({ argv: [mem.wb, 'top', '--json'] })
  mem.top = child
  mem.topKids.add(child)
  void pumpTop($, child)
}

async function pumpTop($: any, child: any) {
  await update($, topNow, () => ({ snap: null, busy: {} })) // the sparkline starts as the tab opens
  let buf = '', err = '', ticked = false, putAway = false
  try {
    for await (const { stream, text } of child) {
      if (!mem.topKids.has(child)) break // stopped meanwhile: its lines aren't the view's
      if (stream === 'stderr') err = (err + text).slice(-2000) // why it ended, should it (#553)
      if (stream !== 'stdout') continue
      buf += text
      let cut
      while ((cut = buf.indexOf('\n')) >= 0) {
        const line = buf.slice(0, cut)
        buf = buf.slice(cut + 1)
        let snap: Row
        try { snap = JSON.parse(line) } catch { continue }
        if (!snap || typeof snap !== 'object') continue
        ticked = true
        mem.topFails = mem.topBackoff = 0
        mem.topWhy = ''
        await update($, topNow, prev => {
          const busy: Record<string, number[]> = {}
          for (const g of (snap.gpus ?? []) as Row[])
            if (typeof g.busy === 'number') busy[String(g.index ?? 0)] = [...(prev.busy[String(g.index ?? 0)] ?? []), g.busy].slice(-BUSY_KEEP)
          return { snap, busy }
        })
      }
      if (!mem.topKids.has(child)) break
      if (!(await topWanted($))) { putAway = true; break } // off screen now: leaving the loop ends the child
    }
  } catch (e) {
    err ||= String(e) // it could not start, or its stream closed: tried again after a while
  }
  const mine = mem.topKids.delete(child)
  if (mine && mem.top === child) { // it ended by itself, not by stopTop
    mem.top = null
    // put away by the engine is no failure: booked as one, a pane shown again 5 s later got no
    // child for 30 s, its last tick drawn as Now meanwhile (#552). Only an exit, a stream error or
    // a child that printed nothing waits, doubling, and says why (#553)
    if (putAway) await update($, topNow, () => ({ snap: null, busy: {} }))
    else {
      if (!ticked) mem.topFails += 1
      mem.topWhy = brief(err) || 'it ended'
      mem.topBackoff = Math.min(mem.topBackoff ? mem.topBackoff * 2 : TOP_RETRY_MS, RETRY_MAX_MS)
      mem.topRetryAt = (await $.clock.now()) + mem.topBackoff
      $.ui.invalidate('ui.render')
    }
  }
}

const gib = (n: number) => (n / 2 ** 30).toFixed(1)
const secs = (s: number) => (s < 60 ? `${Math.round(s)}s` : s < 3600 ? `${Math.floor(s / 60)}m${String(Math.round(s % 60)).padStart(2, '0')}s`
  : `${Math.floor(s / 3600)}h${String(Math.floor((s % 3600) / 60)).padStart(2, '0')}m`)
// lspci's long name cut to the card's own, as `wb top` draws it (top.short): its last bracket
// when the name ends with one, else what follows the last bracket, the vendor's ("[AMD/ATI]
// Phoenix1" was shown as AMD/ATI, #576); "(rev c4)" dropped
export function cardName(name: string): string {
  const n = name.replace(/\s*\(rev [0-9a-f]+\)\s*$/i, '').trim()
  if (n.endsWith(']')) return [...n.matchAll(/\[([^\]]+)\]/g)].at(-1)?.[1] ?? n
  return n.slice(n.lastIndexOf(']') + 1).trim() || n
}
// cpu: all in RAM on a machine with no GPU memory, its normal state, not a split (#551)
const RACK_MARK: Record<string, string> = { resident: '●', cpu: '●', split: '◑', idle: '○', "can't run": '✗', remote: '⇢' }
const RACK_COLOR: Record<string, string> = { resident: 'success', cpu: 'success', split: 'warning', "can't run": 'error' }

// The rack rows folded into one line when more than RACK_FOLD_MIN of them (`wb top`'s top.FOLD):
// the owner, 2026-11-21: a machine with many models tried drew a row each, and the tab's menu sat
// a long scroll below a new user's first look. `wb top` and `wb models` still list every one.
const RACK_FOLD = new Set(['idle', "can't run", 'remote'])
const RACK_FOLD_MIN = 2

// The live view as lines of coloured parts: each GPU (memory and busy as bars, watts, busy's
// sparkline), what runs now, the rack (every model resident or with a task: where it is, its
// speeds, its load time, the task on it; the idle, can't-run and remote ones on one line past
// RACK_FOLD_MIN) and the queue. Nothing is said for what a platform can't tell.
export function liveLines(t: WbTop, why = '', names: Names = NO_NAMES): { color?: string; dim?: boolean; bold?: boolean; text: string }[][] {
  const s = t.snap
  // `why`: what `wb top` said as it ended, once it has failed twice: "Reading…" was all ever said (#553)
  if (!s) return [[{ dim: true, text: why ? `This machine can't be read: ${why} (wb top --json)` : 'Reading this machine…' }]]
  const out: { color?: string; dim?: boolean; bold?: boolean; text: string }[][] = []
  for (const g of (s.gpus ?? []) as Row[]) {
    const row: { color?: string; dim?: boolean; bold?: boolean; text: string }[] = [
      { bold: true, text: g.ram ? 'RAM ' : `GPU ${g.index ?? ''} ` }, { dim: true, text: `${cardName(String(g.name ?? ''))} ` }]
    if (typeof g.used === 'number' && g.total) {
      const f = g.used / g.total
      row.push({ color: f > 0.95 ? 'error' : f > 0.8 ? 'warning' : 'success', text: bar(f, 12) },
        { text: ` ${gib(g.used)}/${gib(g.total)} GiB ` })
    } else if (g.total) row.push({ dim: true, text: `${gib(g.total)} GiB${g.unified ? ' unified' : ''} ` })
    // an Apple GPU: its bar is the RAM, and the most of it the GPU may wire is said here (#701)
    if (typeof g.limit === 'number' && g.limit) row.push({ dim: true, text: `· GPU may wire ${gib(g.limit)} GiB ` })
    if (typeof g.busy === 'number')
      row.push({ dim: true, text: '· busy ' }, { color: g.busy > 80 ? 'warning' : 'success', text: bar(g.busy / 100, 8) },
        { text: ` ${g.busy}% ` })
    if (typeof g.watts === 'number') row.push({ text: `· ${Math.round(g.watts)} W ` })
    const line = spark(t.busy[String(g.index ?? 0)] ?? [])
    if (line.length > 1) row.push({ dim: true, text: line.slice(-24) })
    out.push(row)
  }
  for (const r of (s.running ?? []) as Row[]) {
    const parts = [r.phase, r.round ? `round ${r.round}` : '', typeof r.round_s === 'number' ? secs(r.round_s) : '',
      typeof r.tok_s === 'number' ? `${Math.round(r.tok_s)} tok/s${r.tok_s_from === 'live' ? '' : ' (last call)'}` : '',
      typeof r.context_pct === 'number' ? `context ${r.context_pct}%` : '', ...apiParts(r)].filter(Boolean)
    // an API task's model as the Tasks view says it, its place first: wb top's label is the bare id
    const model = r.via === 'api' ? modelWord(String(r.model ?? ''), true, names) : r.label || r.model
    out.push([{ color: 'claude', text: '▶ ' }, { bold: true, text: `${r.task} ` }, { dim: true, text: `${model} · ${parts.join(' · ')}` }])
  }
  if (!(s.running ?? []).length) out.push([{ dim: true, text: 'Nothing running.' }])
  const rack = (s.rack ?? []) as Row[]
  const folds = rack.filter(m => RACK_FOLD.has(m.state) && !m.tasks?.length)
  const fold = folds.length > RACK_FOLD_MIN ? folds.length : 0
  for (const m of fold ? rack.filter(m => !folds.includes(m)) : rack) {
    const where = m.state === 'cpu' && typeof m.cpu_bytes === 'number' ? ` ${gib(m.cpu_bytes)} GiB in RAM`
      : typeof m.gpu_bytes === 'number'
        ? ` ${gib(m.gpu_bytes)} GiB on the GPU${m.cpu_bytes ? ` + ${gib(m.cpu_bytes)} on the CPU` : ''}` : ''
    // with its unit, as the history's `writes ~61 tok/s`: "writes ~61" said 61 of nothing (#1048's review)
    const facts = [m.write_tok_s ? `writes ~${Math.round(m.write_tok_s)}${NB}tok/s` : '', m.read_tok_s ? `reads ~${Math.round(m.read_tok_s)}${NB}tok/s` : '',
      m.load_s ? `loads ~${secs(m.load_s)}` : '', m.why ?? ''].filter(Boolean)
    out.push([{ color: RACK_COLOR[m.state], dim: !RACK_COLOR[m.state], text: `${RACK_MARK[m.state] ?? '•'} ` },
      { text: `${m.label || m.model} ` }, { color: RACK_COLOR[m.state], dim: !RACK_COLOR[m.state], text: `${m.state}${where}` },
      { dim: true, text: facts.length ? ` · ${facts.join(' · ')}` : '' },
      ...(m.tasks?.length ? [{ color: 'claude', text: ` ← ${m.tasks.join(', ')}` }] : [])])
  }
  if (fold) out.push([{ dim: true, text: `+${fold} more (wb top)` }])
  for (const q of (s.queue ?? []) as Row[])
    out.push([{ color: 'warning', text: '◌ ' }, { text: `${q.task} ` },
      { dim: true, text: `waits for the GPU${typeof q.waited_s === 'number' ? ` (${secs(q.waited_s)})` : ''}` }])
  return out
}

// The commands that hand work out: run as a command, not merely written down. #409 (2026-11-20):
// a Python patch whose string held "split it (wb next)" turned a session's workbench mode on. So
// the command is read as bash reads it (#560, the text matcher before it disagreed both ways: it
// missed `"…/bin/wb" start`, `if wb start`, `! wb start`, `echo "$(wb start)"`, `timeout 60 wb
// start`, anything after a `<<<` here-string, and `wb resume`, and took `echo foo \; wb start` for
// two commands): heredoc bodies and comments dropped, quotes and backslashes undone within a word,
// a `$(…)` or backtick inside double quotes read as the commands it runs, and only a simple
// command's own word counts, past `VAR=x`, the reserved words and the wrappers (with their
// options): `wb` or a path ending in `/wb`.
const HANDS_OUT = new Set(['start', 'next', 'feedback', 'steer', 'answer', 'resume'])
const RESERVED = new Set(['if', 'then', 'elif', 'else', 'while', 'until', 'do', '!', '{'])
// A wrapper that runs the rest: the options of it that take a value, and the words it takes
// before the command (timeout's duration)
const PREFIXES: Record<string, [string[], number]> = {
  env: [['-u', '-C', '-S', '--unset', '--chdir', '--split-string'], 0],
  time: [['-f', '-o', '--format', '--output'], 0],
  nohup: [[], 0], exec: [['-a'], 0], command: [[], 0],
  timeout: [['-s', '-k', '--signal', '--kill-after'], 1],
  nice: [['-n', '--adjustment'], 0], ionice: [['-c', '-n', '--class', '--classdata'], 0],
  stdbuf: [['-i', '-o', '-e', '--input', '--output', '--error'], 0],
  // `sudo git push`, `xargs -I{} git push` hid the command from the consent gate (#1005)
  sudo: [['-u', '-g', '-h', '-p', '-C', '-U', '-r', '-t', '-D', '-R', '-T', '--user', '--group', '--host',
    '--prompt', '--close-from', '--other-user', '--role', '--type', '--chdir', '--chroot', '--command-timeout'], 0],
  doas: [['-u', '-C'], 0],
  xargs: [['-I', '-L', '-l', '-n', '-P', '-s', '-d', '-E', '-e', '-a', '--max-args', '--max-procs', '--max-lines',
    '--max-chars', '--delimiter', '--arg-file', '--eof', '--replace', '--process-slot-var'], 0],
}

// Where the `(` at `open` closes, quotes skipped (the end when it never does)
function closing(src: string, open: number): number {
  let depth = 0
  for (let i = open; i < src.length; i++) {
    const c = src[i]
    if (c === '\\') i++
    else if (c === "'") { const j = src.indexOf("'", i + 1); i = j < 0 ? src.length : j }
    else if (c === '(') depth++
    else if (c === ')' && --depth === 0) return i
  }
  return src.length
}

// The commands an unquoted heredoc's body runs: its `$(…)` and backticks (`\\` escapes)
function expansions(body: string): string[][] {
  const out: string[][] = []
  for (let j = 0; j < body.length; j++) {
    if (body[j] === '\\') j++
    else if (body[j] === '$' && body[j + 1] === '(') {
      const k = closing(body, j + 1)
      out.push(...simpleCommands(body.slice(j + 2, k)))
      j = k
    } else if (body[j] === '`') {
      const k = body.indexOf('`', j + 1)
      const end = k < 0 ? body.length : k
      out.push(...simpleCommands(body.slice(j + 1, end)))
      j = end
    }
  }
  return out
}

// The simple commands a bash command line runs, each as its words (quotes undone)
function simpleCommands(src: string): string[][] {
  const out: string[][] = []
  let words: string[] = []
  let word: string | null = null // null: between words
  const heredocs: { tag: string; strip: boolean; quoted: boolean }[] = []
  const endWord = () => { if (word !== null) words.push(word); word = null }
  const endCommand = () => { endWord(); if (words.length) out.push(words); words = [] }
  const n = src.length
  let i = 0
  while (i < n) {
    const c = src[i]!
    if (c === '\\') {
      if (src[i + 1] !== '\n') word = (word ?? '') + (src[i + 1] ?? '') // `\;` is a `;` in a word
      i += 2
    } else if (c === "'") {
      const j = src.indexOf("'", i + 1)
      const end = j < 0 ? n : j
      word = (word ?? '') + src.slice(i + 1, end)
      i = end + 1
    } else if (c === '"') {
      let j = i + 1
      let text = ''
      while (j < n && src[j] !== '"') {
        if (src[j] === '\\') { text += src[j + 1] ?? ''; j += 2 }
        else if (src[j] === '$' && src[j + 1] === '(') {
          const k = closing(src, j + 1)
          out.push(...simpleCommands(src.slice(j + 2, k)))
          j = k + 1
        } else if (src[j] === '`') {
          const k = src.indexOf('`', j + 1)
          const end = k < 0 ? n : k
          out.push(...simpleCommands(src.slice(j + 1, end)))
          j = end + 1
        } else text += src[j++]
      }
      word = (word ?? '') + text
      i = j + 1
    } else if (c === '#' && word === null) { // a comment, to the line's end
      const j = src.indexOf('\n', i)
      i = j < 0 ? n : j
    } else if (c === '\n') {
      endCommand()
      i++
      for (const h of heredocs) { // their bodies start on the next line: up to the line that is the tag
        let body = ''
        while (i < n) {
          const j = src.indexOf('\n', i)
          const line = src.slice(i, j < 0 ? n : j)
          i = j < 0 ? n : j + 1
          if ((h.strip ? line.replace(/^\t+/, '') : line) === h.tag) break
          body += `${line}\n`
        }
        if (!h.quoted) out.push(...expansions(body)) // text, but `$(…)` in it runs
      }
      heredocs.length = 0
    } else if (c === '<' && src.startsWith('<<<', i)) { // a here-string: its word follows
      endWord()
      i += 3
    } else if (c === '<' && src[i + 1] === '<') { // a heredoc: its tag, quotes undone
      endWord()
      let j = i + 2
      const strip = src[j] === '-'
      if (strip) j++
      while (src[j] === ' ' || src[j] === '\t') j++
      const m = /^(?:'([^']*)'|"([^"]*)"|\\?([^\s;&|<>()'"]+))/.exec(src.slice(j))
      if (m) heredocs.push({ tag: m[1] ?? m[2] ?? m[3]!, strip, quoted: m[3] === undefined || m[0].startsWith('\\') })
      i = j + (m ? m[0].length : 0)
    } else if (c === '$' && src[i + 1] === '{') { // ${…}: part of the word
      const j = src.indexOf('}', i)
      const end = j < 0 ? n : j
      word = (word ?? '') + src.slice(i, end + 1)
      i = end + 1
    } else if (c === '$' && src[i + 1] === '(') { // $(…): the commands inside are commands too
      endCommand()
      i += 2
    } else if (';&|()`'.includes(c)) {
      endCommand()
      i++
    } else if (c === ' ' || c === '\t' || c === '<' || c === '>') {
      endWord()
      i++
    } else {
      word = (word ?? '') + c
      i++
    }
  }
  endCommand()
  return out
}

// Where a simple command's own word is: past `VAR=x`, the reserved words and the wrappers
function commandAt(words: string[]): number {
  let i = 0
  for (;;) {
    const w = words[i] ?? ''
    if (/^[A-Za-z_]\w*=/.test(w) || RESERVED.has(w)) { i++; continue }
    const prefix = PREFIXES[w]
    if (!prefix) break
    i++
    let positional = prefix[1]
    while (i < words.length) {
      const o = words[i]!
      if (o === '--') { i++; break }
      if (o.startsWith('-') && o.length > 1) { i += prefix[0].includes(o) ? 2 : 1; continue }
      if (positional > 0) { positional--; i++; continue }
      break
    }
  }
  return i
}

export function delegates(command: string): boolean {
  for (const words of simpleCommands(command)) {
    const i = commandAt(words)
    const cmd = words[i] ?? ''
    if ((cmd === 'wb' || cmd.endsWith('/wb')) && HANDS_OUT.has(words[i + 1] ?? '')) return true
  }
  return false
}

// The consent gate (#1005, 2026-11-22). The owner's rule, absolute: a user's Claude never forks,
// pushes, opens or comments on an issue or pull request at workbench upstream without the user's
// yes for that exact action. A rule in the skill is advice Claude may misread; this is the one
// place every Bash and Monitor call (and the GitHub MCP tools' calls) passes, so the yes is the
// user's own, in a dialog, each time (nothing is remembered: a yes to one pull request is no yes
// to the next). What sends: these gh forms, a gh api call that writes, a git push, and wb patch
// send (the turnkey path's one send, whose single dialog shows its title and body). Read as bash
// reads it (simpleCommands, as `delegates`), so an echo's or a heredoc's text doesn't count, and
// `bash -c '…'` and `eval` are read inside; text a shell or an interpreter runs from a heredoc,
// a here-string, a pipe or -c is caught by its words (OUTWARD_TEXT): `bash <<EOF … git push` once
// went through (the review of #1005).
const UPSTREAM = 'a-schaefers/workbench' // the GitHub home; patches.json's upstream when it has one (Couplings)
// Its names before: the repo was renamed from claudes-workbench (the owner, 2026-11-24) and GitHub
// redirects the old name, so a send naming it, or a clone whose origin and patches.json still say
// it, still lands upstream and still asks first
const FORMER_UPSTREAM = ['a-schaefers/claudes-workbench']
const GH_WRITES: Record<string, Set<string>> = {
  pr: new Set(['create', 'comment', 'edit', 'merge', 'review', 'close', 'reopen', 'ready', 'update-branch', 'lock', 'unlock']),
  issue: new Set(['create', 'comment', 'edit', 'close', 'reopen', 'delete', 'lock', 'unlock', 'transfer', 'pin', 'unpin', 'develop']),
  repo: new Set(['fork', 'edit', 'delete', 'rename', 'archive', 'unarchive', 'sync']),
  workflow: new Set(['run', 'enable', 'disable']),
  run: new Set(['rerun', 'cancel', 'delete']),
  release: new Set(['create', 'edit', 'delete', 'upload', 'delete-asset']),
  label: new Set(['create', 'edit', 'delete', 'clone']),
}
const GIT_VALUED = new Set(['-C', '-c', '--git-dir', '--work-tree', '--namespace', '--config-env'])
const SHELLS = new Set(['bash', 'sh', 'zsh', 'dash'])
const PYTHON = /^python[\d.]*$/
// what runs code it is handed as text (stdin, -c, -e): its text is read for an outward command
const RUNS_CODE = /^(?:bash|sh|zsh|dash|ksh|ash|fish|python[\d.]*|node|perl|ruby|php|deno|bun)$/
const SEND_TEXT = /(?:\bwb|-m\s*workbench[\w.]*)\s+patch\s+send\b/i
const OUTWARD_TEXT = new RegExp(String.raw`\bgit\b[^\n]*\bpush\b|\bgh\s+(?:pr|issue|repo|api|release|workflow|run|label)\b|` +
  SEND_TEXT.source, 'i')
const SEND_ASK = 'Send it'
// Said after a no: it once read "Show them exactly what it sends and ask", and Claude opened a
// dialog of its own pleading for the yes the user had just refused (the review of #1005)
const NOT_SENT = 'workbench: not sent: the user did not approve it in the dialog. Tell them it was not ' +
  'sent; try again only if they ask for it.'
const NOT_SHOWN = (why: string) => `workbench: not sent: the dialog can't show what it sends (${why}). Run ` +
  'wb patch send as a command of its own, its --title written out and --body-file naming the file, so the ' +
  'user sees both.'

// gh api writes with a method other than GET, or with fields (POST by default; with an explicit
// GET gh sends them as the query)
function apiWrites(args: string[]): boolean {
  let method = '', fields = false
  for (let j = 0; j < args.length; j++) {
    const a = args[j]!
    const m = /^(?:-X|--method(?==|$))=?(.*)$/.exec(a)
    if (m) method = (m[1] || args[++j] || '').toUpperCase()
    else if (/^-[fF]/.test(a) || /^--(?:field|raw-field|input)(?:=|$)/.test(a)) fields = true
  }
  return method ? method !== 'GET' && method !== 'HEAD' : fields
}

// A command word as the system finds it: its name, lowercased (macOS finds `GIT` as git)
const wordOf = (w: string | undefined) => (w ?? '').split('/').pop()!.toLowerCase()

// The words wb gets when this command runs it: `wb` or a path to it, or Python running a
// `…/wb` or `-m workbench…` (`python3 <source>/bin/wb patch send` was no send, the review of #1005)
function wbArgs(cmd: string, args: string[]): string[] | null {
  if (cmd === 'wb') return args
  if (!PYTHON.test(cmd)) return null
  for (let j = 0; j < args.length; j++) {
    const a = args[j]!
    if (a.startsWith('-m')) {
      const mod = a === '-m' ? args[j + 1] ?? '' : a.slice(2)
      return mod.startsWith('workbench') ? args.slice(j + (a === '-m' ? 2 : 1)) : null
    }
    if (a === '-X' || a === '-W') j++
    else if (!a.startsWith('-')) return wordOf(a) === 'wb' ? args.slice(j + 1) : null
  }
  return null
}

// What one simple command sends: 'send' (wb patch send), 'out' (another outward form), '' (none),
// and the directories it names with git's -C
function sendsOut(words: string[]): { kind: '' | 'send' | 'out'; dirs: string[] } {
  const i = commandAt(words)
  const cmd = wordOf(words[i])
  const args = words.slice(i + 1)
  if (cmd === 'gh') {
    const pos: string[] = []
    for (let j = 0; j < args.length && pos.length < 2; j++) {
      const a = args[j]!
      if (a === '-R' || a === '--repo') j++
      else if (!a.startsWith('-')) pos.push(a.toLowerCase())
    }
    const [group = '', sub = ''] = pos
    const out = group === 'api' ? apiWrites(args) : !!GH_WRITES[group]?.has(sub)
    return { kind: out ? 'out' : '', dirs: [] }
  }
  if (cmd === 'git') {
    const dirs: string[] = []
    let j = 0
    for (; j < args.length && args[j]!.startsWith('-'); j++) {
      if (args[j] === '-C') dirs.push(args[j + 1] ?? '')
      if (GIT_VALUED.has(args[j]!)) j++
    }
    return { kind: (args[j] ?? '').toLowerCase() === 'push' ? 'out' : '', dirs }
  }
  const wb = wbArgs(cmd, args)
  if (wb && wb[0] === 'patch' && wb[1] === 'send') return { kind: 'send', dirs: [] }
  // wb patch zip only writes a file the user may email themselves: nothing outward (#1291)
  return { kind: '', dirs: [] }
}

const inside = (p: string, dir: string) => !!dir && (p === dir || p.startsWith(`${dir}/`))

// Whether the text names a directory: as written, through `~` or $HOME, or $WB_HOME for the state's
export function mentions(text: string, dir: string, home: string, root = ''): boolean {
  if (!dir) return false
  const ways = [dir]
  if (home && inside(dir, home) && dir !== home) {
    const rest = dir.slice(home.length)
    ways.push(`~${rest}`, `$HOME${rest}`, `\${HOME}${rest}`)
  }
  if (root && inside(dir, root) && dir !== root) {
    const rest = dir.slice(root.length)
    ways.push(`$WB_HOME${rest}`, `\${WB_HOME}${rest}`)
  }
  const esc = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  return ways.some(w => new RegExp(`${esc(w)}(?=$|[/\\s'"\`;&|)])`).test(text))
}

// A git remote's URL is the upstream's (https, ssh or scp form, with or without .git)
export function originIs(url: string, slug: string | string[]): boolean {
  const u = url.trim().toLowerCase().replace(/\/+$/, '').replace(/\.git$/, '')
  return (Array.isArray(slug) ? slug : [slug]).some(one => {
    const s = one.toLowerCase()
    return u === s || u.endsWith(`/${s}`) || u.endsWith(`:${s}`)
  })
}

// why: 'send' (wb patch send), 'dir' (it names, or runs in, a gated directory), 'plugin' (it runs
// in Claude Code's plugins directory, `at`: gated when that checkout's origin is upstream), 'slug'
// (the line names the upstream's owner/repo, run `at` that directory: a developer's own clone of
// upstream there is no user's Claude). `named`: the line names the slug; `outAt`: every directory
// an outward command in it runs in, for the hook to check by real path; `shown`: the outward text
// when it isn't a command of its own (fed to a shell)
// A cd or pushd's target word as written (`cd --` and the flags skipped): undefined for none
// (home), '-' for the last directory. Shared by upstreamAim and routeCalls (#1125).
function cdTarget(words: string[], i: number): string | undefined {
  const rest = words.slice(i + 1)
  const k = rest.indexOf('--')
  return k >= 0 ? rest[k + 1] : rest.find(w => !/^-[LPe@]+$/.test(w))
}

export type Aim = { why: '' | 'send' | 'dir' | 'plugin' | 'slug'; at: string; words: string[]; named: boolean; outAt: string[]; shown?: string }

// Whether a command line sends something to workbench upstream, and why. `dirs` are the gated
// directories (the patch clone, `wb patch upstream`'s worktrees, Claude Code's workbench
// marketplace clone and cache), `plugins` Claude Code's plugins directories. The slug is matched
// in the whole line, whenever any command in it is outward: `export GH_REPO=<slug> && gh issue
// create` or `gh repo set-default <slug> && …` named it outside the outward command and went
// through (the review of #1005). `cd`, `cd --` and pushd are followed from `cwd`.
export function upstreamAim(command: string, cwd: string, home: string, slug: string | string[], dirs: string[], root = '',
  plugins: string[] = []): Aim {
  let here = cwd
  const lines = (src: string): string[][] => simpleCommands(src).flatMap(words => {
    const i = commandAt(words)
    const cmd = wordOf(words[i])
    const c = words.findIndex((w, j) => j > i && /^-[a-z]*c$/.test(w))
    if (SHELLS.has(cmd) && c > 0 && words[c + 1] !== undefined) return [words, ...lines(words[c + 1]!)]
    if (cmd === 'eval') return [words, ...lines(words.slice(i + 1).join(' '))]
    return [words]
  })
  const textNames = dirs.some(d => mentions(command, d, home, root))
  const named = (Array.isArray(slug) ? slug : [slug]).some(s => command.toLowerCase().includes(s.toLowerCase()))
  const outAt: string[] = []
  const inPlugins = (ps: string[]) => ps.find(p => plugins.some(d => inside(p, d)))
  let found: Aim | null = null
  let runner = ''
  for (const words of lines(command)) {
    const i = commandAt(words)
    const cmd = wordOf(words[i])
    if (cmd === 'cd' || cmd === 'pushd') {
      const to = cdTarget(words, i)
      if (to !== '-') here = absolutePath(to ?? (home || '/'), home, here)
      continue
    }
    if (!runner && RUNS_CODE.test(cmd)) runner = here
    const { kind, dirs: viaC } = sendsOut(words)
    if (!kind) continue
    if (kind === 'send') return { why: 'send', at: here, words, named, outAt }
    const at = [here, ...viaC.map(d => absolutePath(d, home, here))]
    outAt.push(...at)
    if (textNames || at.some(p => dirs.some(d => inside(p, d)))) return { why: 'dir', at: here, words, named, outAt }
    const plugin = inPlugins(at)
    if (!found && plugin) found = { why: 'plugin', at: plugin, words, named, outAt }
    if (!found && named) found = { why: 'slug', at: here, words, named, outAt }
  }
  if (found) return { ...found, outAt }
  // the backstop: text a shell or an interpreter in the line runs, which isn't parsed as commands
  if (runner && OUTWARD_TEXT.test(command)) {
    outAt.push(runner)
    const shown = command.split('\n').filter(l => OUTWARD_TEXT.test(l)).join('\n')
    const aim = (why: Aim['why'], at = runner): Aim => ({ why, at, words: [], named, outAt, shown })
    if (SEND_TEXT.test(command)) return aim('send')
    if (textNames || dirs.some(d => inside(runner, d))) return aim('dir')
    if (inPlugins([runner])) return aim('plugin')
    if (named) return aim('slug')
  }
  return { why: '', at: '', words: [], named, outAt }
}

// wb patch send's own words: its slug, --title and --body-file (the last given, as argparse
// takes them; wb refuses abbreviations, so only these spellings count), --draft, --anyway
export function sendArgs(words: string[]) {
  const i = commandAt(words)
  const rest = (wbArgs(wordOf(words[i]), words.slice(i + 1)) ?? []).slice(2)
  let slug: string | undefined, title: string | undefined, file: string | undefined
  let draft = false, anyway = false
  for (let j = 0; j < rest.length; j++) {
    const a = rest[j]!
    if (a === '--') { slug ??= rest[j + 1]; break }
    if (a === '--title') title = rest[++j]
    else if (a === '--body-file') file = rest[++j]
    else if (a.startsWith('--title=')) title = a.slice('--title='.length)
    else if (a.startsWith('--body-file=')) file = a.slice('--body-file='.length)
    else if (a === '--draft') draft = true
    else if (a === '--anyway') anyway = true
    else if (!a.startsWith('-')) slug ??= a
  }
  return { slug, title, file, draft, anyway }
}

const SHOW_CHARS = 600
const BODY_LINES = 20
const MCP_CHARS = 4000
const quoted = (w: string) => /^[\w@%+=:,./-]+$/.test(w) ? w : `'${w.replace(/'/g, `'\\''`)}'`

// The command as the dialog shows it: the whole line when short; else the outward command in
// full, then the line's start. Cut from the line's start, a long line hid the gh command and
// its inline --body entirely (the review of #1005)
export function shownCommand(command: string, aim: Aim): string {
  if (command.length <= SHOW_CHARS) return command
  const own = aim.shown ?? aim.words.map(quoted).join(' ')
  return `${own}\n\n(in a longer line: ${command.slice(0, SHOW_CHARS)}…)`
}

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
  const args = sendArgs(aim.words)
  if (!aim.words.length) return { deny: NOT_SHOWN('it is inside text another program runs') }
  if (!args.slug) return { deny: NOT_SHOWN('no slug') }
  if (args.title === undefined) return { deny: NOT_SHOWN('no --title') }
  if (args.file === undefined) return { deny: NOT_SHOWN('no --body-file') }
  if (/[$`]/.test(args.title) || /[$`]/.test(args.file)) return { deny: NOT_SHOWN('a $ or a backtick in --title or --body-file, whose value only the shell knows') }
  const path = absolutePath(args.file, home, aim.at)
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

// What the gate reads each time: patches.json's clone and upstream, the gated directories
type Gate = { home: string; cwd: string; slug: string; slugs: string[]; dirs: string[]; plugins: string[] }
async function gateConf($: any): Promise<Gate> {
  const conf = await readJson($, `${mem.root}/patches.json`)
  const home = (await $.env.get('HOME').catch(() => '')) || mem.home
  const cwd = (await $.session.cwd().catch(() => '')) || mem.cwd
  // patches.json's, else where wb patch init would put it (patches.source_dir)
  const given = typeof conf.source === 'string' && conf.source ? conf.source
    : (await $.env.get('WB_SOURCE').catch(() => '')) || `${home}/.local/share/workbench/source`
  const source = absolutePath(given, home, cwd)
  const slug = typeof conf.upstream === 'string' && conf.upstream.trim() ? conf.upstream.trim() : UPSTREAM
  // Claude Code's plugins: the marketplace clone (the README's install) and the cache
  const cc = (await $.env.get('CLAUDE_CONFIG_DIR').catch(() => '')) || `${home}/.claude`
  const cache = await $.env.get('CLAUDE_CODE_PLUGIN_CACHE_DIR').catch(() => '')
  const plugins = [`${absolutePath(cc, home, cwd)}/plugins`, ...(cache ? [absolutePath(cache, home, cwd)] : [])]
  const dirs = [source, `${mem.root}/upstream`, ...plugins.flatMap(p => [`${p}/marketplaces/workbench`, `${p}/cache/workbench`])]
  // every name upstream answers to: the one in use and its former ones (FORMER_UPSTREAM)
  const slugs = [...new Set([slug, UPSTREAM, ...FORMER_UPSTREAM].map(x => x.toLowerCase()))]
  return { home, cwd, slug, slugs, dirs, plugins }
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
const CONSENT_WAITING = 'Claude is waiting on a workbench dialog in the terminal: answer it there.'
async function consent($: any, question: string, yes: string, no: string, outcome: string): Promise<boolean> {
  if (await away($, await read($, chat))) await runWb($, ['chat', 'reply', `--text=${CONSENT_WAITING}`, '--to=user'], 15_000)
  return (await askOrNo($, question, [yes, no], outcome)) === yes
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
  const q = await sendQuestion($, command, g.slug, aim, g.home)
  if ('deny' in q) return q.deny
  const no = await askToSend($, q.question)
  if (no || !q.approval) return no
  // wb patch send checks this under Claude Code: without it nothing asked the user (patches._approved)
  await $.fs.write(`${mem.root}/send-ok.json`, JSON.stringify({ ...q.approval, at: (await $.clock.now()) / 1000 })).catch(() => {})
  return ''
}

// The GitHub MCP tools (the github plugin): a call that isn't a read and names the upstream, or a
// repository of its name (a fork's), asks the same; Bash was the only gate, and patching.md
// offers that plugin (the review of #1005)
const GITHUB_MCP = /^mcp__.*github.*__(.+)$/i
export function mcpAimsUpstream(tool: string, input: Row, slug: string | string[]): boolean {
  const m = GITHUB_MCP.exec(tool)
  if (!m || /^(?:get|list|search)_/i.test(m[1]!)) return false
  const repo = String(input.repo ?? input.repository ?? '').toLowerCase()
  return (Array.isArray(slug) ? slug : [slug]).some(one => {
    const s = one.toLowerCase()
    const name = s.split('/')[1] ?? s
    return repo === s || repo === name || repo.endsWith(`/${name}`) || JSON.stringify(input).toLowerCase().includes(s)
  })
}

async function mcpRefusal($: any, tool: string, input: Row): Promise<string> {
  const g = await gateConf($)
  if (!mcpAimsUpstream(tool, input, g.slugs)) return ''
  if (await developerClone($, g.cwd, g.slugs, [...g.dirs, ...g.plugins])) return ''
  const text = JSON.stringify(input, null, 1)
  const shown = text.length > MCP_CHARS ? `${text.slice(0, MCP_CHARS)}… (${text.length - MCP_CHARS} more characters not shown)` : text
  return askToSend($, `Claude wants to send this to GitHub (${g.slug}), publicly: ${tool} ${shown} — Send it?`)
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
// text a shell or an interpreter runs, not parsed as commands: a route change hidden in it is refused
const ROUTE_TEXT = /(?:\bwb|-m\s*workbench[\w.]*)\s+(?:route\b|start\b[^\n]*\s--m)/i
// Said with the roles. Roles are settings, and Claude never changes one unasked (the owner's locked
// rule, 2026-11-24): #1322 tried "Run it when it is the right call", and it went back
const ROUTES_RULE = 'Never pass --model for an API model or change a role without the user\'s yes (the mod ' +
  'asks them in a dialog); recommend only.'

// The simple commands a line runs, `bash -c '…'` and `eval`'s inside, as upstreamAim reads them
function runsIn(src: string): string[][] {
  return simpleCommands(src).flatMap(words => {
    const i = commandAt(words)
    const cmd = wordOf(words[i])
    const c = words.findIndex((w, j) => j > i && /^-[a-z]*c$/.test(w))
    if (SHELLS.has(cmd) && c > 0 && words[c + 1] !== undefined) return [words, ...runsIn(words[c + 1]!)]
    if (cmd === 'eval') return [words, ...runsIn(words.slice(i + 1).join(' '))]
    return [words]
  })
}

// Each wb route and each wb start with a --model (any abbreviation: --m…) in a command line, as
// the arguments `wb route --dry-run --json` takes for it: a route's own, or `--start` and the
// start's (parsed there by wb start's own parser). `hidden`: a route or such a start inside text
// another program runs, which can't be shown. `at`: the directory each call runs in, `cd` and
// pushd followed from ``cwd`` as upstreamAim follows them: the dry run ran in the session's
// directory, so `cd other && wb start … --model <API id>` (--repo defaults to `.`) was judged
// local-only, or not, by the wrong repo (#1125, 2026-11-23). '' where the mod can't say: without
// a ``cwd``, and after a cd to a word with `$` or a backquote in it (`cd "$REPO"`), to none (an
// unquoted `$(…)` leaves none) or to `-`; read as a literal path, it failed the dry run to spawn,
// so the call skipped the dialog into wb's "is the mod loaded?" (the review of #1125). An
// absolute one after it is known again.
export function routeCalls(command: string, cwd = '', home = ''): { calls: string[][]; hidden: boolean; at: string[] } {
  const calls: string[][] = []
  const at: string[] = []
  let here = cwd
  let runner = false
  for (const words of runsIn(command)) {
    const i = commandAt(words)
    const cmd = wordOf(words[i])
    if (cmd === 'cd' || cmd === 'pushd') {
      const to = cdTarget(words, i)
      if (!to || to === '-' || /[$`]/.test(to)) here = ''
      else if (here || to.startsWith('/')) here = absolutePath(to, home, here)
      continue
    }
    if (RUNS_CODE.test(cmd)) runner = true
    const wb = wbArgs(cmd, words.slice(i + 1))
    if (wb?.[0] === 'route') calls.push(wb.slice(1))
    else if (wb?.[0] === 'start' && wb.some(w => /^--m/.test(w))) calls.push(['--start', ...wb.slice(1)])
    else continue
    at.push(here)
  }
  return { calls, hidden: !calls.length && runner && ROUTE_TEXT.test(command), at }
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
// was said for a model that can't turn it off (#1263, 2026-11-24)
export function thinkingSaid(row: Row): string {
  const thinking = row.thinking === 'on' ? 'on' : 'off'
  const t = row.thinks
  return t && typeof t === 'object' && t.api === row.api && typeof t[thinking] === 'string' ? t[thinking] : `thinking ${thinking}`
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
    return text + fallback
  })
  // the Agents switch (#1016): routes.json `use`, over each role's own while it isn't Both
  const use = doc.use === 'gpu' ? 'GPU' : doc.use === 'api' ? 'API' : ''
  // the busy opt-in (#1023): said, or a change of it alone would never reach the conversation
  const busy = doc.busy === 'api' ? '; when the GPU is busy, a role with an API model runs on it' : ''
  return (use ? `Agents ${use}, over each role's own: ` : '') + roles.join('; ') + busy
}

// A document wb would read (routes.normalize's checks, in short): else it can't be read
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
    [undefined, 'off', 'on'].includes(row.thinking) && (row.via !== 'api' || (typeof row.api === 'string' && !!row.api)) &&
    (row.order == null || (Array.isArray(row.order) && row.order.every((b: unknown) => typeof b === 'string' && !!b.trim()))) &&
    (row.fallback == null || (typeof row.fallback === 'string' && !!row.fallback.trim() && row.fallback !== 'none')))
}

// What the conversation is told of the roles: `said`, their summary and the day cap ('. Day cap
// $N'), '' while every role is the default and no cap is set (nothing to tell); `api`, whether any
// role has an API model. The cap is in `said`, which is what a change is found by: a cap changed
// alone (a terminal's wb route cap, the pane, a hand edit) never reached the conversation (2026-11-23)
type Routes = { said: string; api: boolean; cap: number | null }
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
    !r.api && (r.thinking ?? 'off') === 'off' && !r.fallback)
  return { said: plain ? '' : routesSummary(doc) + (cap !== null ? `. Day cap ${money(cap)}` : ''), api: rows.some(r => !!r.api), cap }
}
const DEFAULT_ROUTES = routesSummary({})
const ROUTES_NOTE = (r: Routes) => `Roles (the user's, wb route): ${r.said}. ` + (r.cap !== null ? '' : 'No day cap yet. ') +
  ROUTES_RULE
const ROUTES_CHANGED = (said: string) => `The roles changed: ${said || DEFAULT_ROUTES}. ${ROUTES_RULE}`

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

// The deny for a Bash or Monitor command that changes a role, or starts a task on an API model,
// without the user's yes in the dialog now, else ''
async function routeRefusal($: any, e: Row): Promise<string> {
  const tool = String(e.tool ?? '')
  if ((tool !== 'Bash' && tool !== 'Monitor') || typeof e.command !== 'string') return ''
  const command = e.command
  if (!/\b(?:route|start)\b|route-ok\.json/i.test(command)) return '' // most calls: no parse
  if (command.includes(ROUTE_OK) && (mentions(command, mem.root, await routesHome($)) ||
    /\$\{?WB_HOME\b/.test(command))) return ROUTE_OK_OWN
  const { calls, hidden, at } = routeCalls(command, (await $.session.cwd().catch(() => '')) || mem.cwd, await routesHome($))
  if (hidden) return ROUTE_OWN
  // a show (`wb route`, `--json`) and a dry run change nothing
  const live = calls.flatMap((c, i) => !c.includes('--dry-run') && (c[0] === '--start' || c.some(a => a !== '--json'))
    ? [{ args: c, cwd: at[i] }] : [])
  if (!live.length) return ''
  if (live.length > 1) return ROUTE_OWN
  let q: Row
  try {
    // a start's in the directory it runs in: wb start's --repo is `.` unless given (#1125). Only a
    // start's (wb route doesn't read its directory), and only one that exists: a directory the
    // line makes (`mkdir x && cd x`) failed the dry run to spawn, and the call skipped the
    // dialog; the session's is the better guess (the review of #1125, 2026-11-23)
    const { args, cwd } = live[0]!
    const there = args[0] === '--start' && cwd && await $.fs.exists(cwd).catch(() => false) ? { cwd } : {}
    const r = await $.process.run([mem.wb, 'route', '--dry-run', '--json', ...args], { timeoutMs: 60_000, ...there })
    if (r.exitCode !== 0) return '' // wb refuses it itself, saying why
    q = JSON.parse(String(r.stdout ?? ''))
  } catch {
    return '' // no dry run: wb itself refuses the change under Claude Code without route-ok.json
  }
  if (!q?.gate || typeof q.change !== 'string' || !q.change) return '' // a GPU model pin, or nothing
  if (!(await consent($, q.change, ROUTE_ALLOW, "Don't", 'nothing changed'))) return NOT_CHANGED
  await $.fs.write(`${mem.root}/${ROUTE_OK}`, JSON.stringify({ ...q.approval, at: (await $.clock.now()) / 1000 })).catch(() => {})
  return ''
}

const SPARKS = '▁▂▃▄▅▆▇█'
// Values as one character each, the highest the full block; none for all zero
export function spark(values: number[]): string {
  const top = Math.max(0, ...values)
  return top > 0 ? values.map(v => SPARKS[Math.min(7, Math.round((Math.max(0, v) / top) * 7))]).join('') : ''
}

const GUARD_WORDS: Record<string, string> = {
  repeat: 'the same call repeated', errors: 'tool errors in a row', no_edit: 'no file changed',
  round: 'the round time limit', silence: 'silence', think: 'thinking with no tool call',
  handovers: 'handovers without finishing',
  spend: 'the API day cap', // Settings' and the Tasks view's words for it
}

// stats.STATS_MODEL_LINES, kept here too: the mod tracks main while wb runs from the user's clone,
// so an older wb's feed (every model line, or only `models`) must not draw a row per model; and
// with no line to go by (no task yet), what Optimized, Bench and What helps name (2026-11-21)
const STATS_SHOWN = 3

// The Stats tab's history as lines (a label in bold, then the text): what the feed holds, each
// section left out when it has nothing.
export function statsLines(f: Record<string, any> | null, why = '', names: Names = NO_NAMES): { label: string; text: string }[] {
  // `why`: the last `wb stats --json` failed, said (#553): with no feed yet "Reading…" never ended
  if (!f) return [{ label: '', text: why ? `History unavailable: ${why} (wb stats --json)` : 'Reading the history…' }]
  if (why) return [{ label: '', text: `Not updated: ${why} (wb stats --json)` }, ...statsLines(f, '', names)]
  // only when the feed has no section at all: with no judged task, the optimized windows and the
  // bench results still have something to say (#549)
  if (!f.tasks && !Object.keys(f).some(k => !['ts', 'until', 'settings', 'tasks'].includes(k)))
    return [{ label: '', text: 'No finished tasks yet: delegate one with /workbench, and how it went shows here.' }]
  const out: { label: string; text: string }[] = []
  const period = (name: string, label: string) => {
    const p = f.periods?.[name]
    if (!p) return
    // the score, the savings estimate, spec-gap discards and the energy and its cost were here
    // too: precision nobody acted on (#1322)
    const parts = [
      `${p.tasks} task${p.tasks === 1 ? '' : 's'}`,
      `${p.landed} landed${p.accepted_pct !== undefined ? ` (${p.accepted_pct}%)` : ''}`,
      apiPeriod(p.api, names),
    ]
    out.push({ label, text: parts.filter(Boolean).join(' · ') })
  }
  period('week', 'This week')
  period('all', 'All time')
  // the models OpenCode has now, a wb optimize variant folded under its source, the most recently
  // used first, the rest on one line (`model_lines`, #638); an older wb's feed: its `models`.
  // Capped here as well as in wb: a wb from before the cap sends every line (2026-11-21)
  const all: [string, any][] = f.model_lines
    ? (f.model_lines.lines ?? []).map((m: any) => [String(m.model), m])
    : Object.entries(f.models ?? {})
  const shown = all.slice(0, STATS_SHOWN), past = all.slice(STATS_SHOWN)
  // a model as Now and the Tasks view name it (modelWord); Bench, Optimized and What helps by the same
  const apiIds = new Set(all.filter(([, m]) => m.kind === 'api').map(([model]) => model))
  const word = (model: string) => modelWord(model, apiIds.has(model), names)
  for (const [model, m] of shown) {
    const trend = spark((m.write_trend ?? []).map((p: number[]) => p[1]))
    const speed = m.kind === 'api' ? (typeof m.paid === 'number' ? ` · paid ~${usd(m.paid)}/task` : '')
      : m.speed?.write ? ` · writes ~${Math.round(m.speed.write)}${NB}tok/s` : ''
    const change = typeof m.write_change_pct === 'number' && m.write_change_pct
      ? ` (${m.write_change_pct > 0 ? '+' : ''}${m.write_change_pct}%)` : ''
    const windows = (m.windows ?? []).length > 1 ? ` (${m.windows.map((w: number) => `${Math.round(w / 1024)}K`).join(', ')})` : ''
    out.push({ label: word(model) + windows, text: `${m.tasks} task${m.tasks === 1 ? '' : 's'} · ${m.accepted_pct}% landed` +
      (m.rounds != null ? ` · ${m.rounds} rounds` : '') + (m.minutes != null ? ` · ${m.minutes} min` : '') +
      speed + (trend ? ` ${trend}` : '') + change })
  }
  // the recent ones past stats.STATS_MODEL_LINES (and past this cap) and the older ones, one row;
  // labelled "Other models" so the text after it doesn't say "more models" twice (2026-11-21)
  const older = f.model_lines?.older
  const moreModels = (f.model_lines?.more?.models ?? 0) + past.length
  const moreTasks = (f.model_lines?.more?.tasks ?? 0) + past.reduce((n, [, m]) => n + (Number(m.tasks) || 0), 0)
  const others = moreModels + (older?.models ?? 0), otherTasks = moreTasks + (older?.tasks ?? 0)
  if (others) out.push({ label: 'Other models',
    text: [moreModels ? `${moreModels} more` : '', older?.models ? `${older.models} older` : ''].filter(Boolean).join(' · ') +
      ` model${others === 1 ? '' : 's'}: ${otherTasks} task${otherTasks === 1 ? '' : 's'} (wb stats --all)` })
  // Optimized, Bench and What helps name only the models shown above, a line's variants with it,
  // the rest counted, with where to see them (2026-11-21: 30 models' windows and runs made rows
  // taller than the screen); no model line yet (no task, or only older ones): the first STATS_SHOWN
  const ids = shown.length
    ? new Set<string>(shown.flatMap(([model, m]) => [model, ...(m.variants ?? [])])) : null
  const pick = <T,>(entries: [string, T][]): [[string, T][], number] => {
    const kept = entries.filter(([m], i) => (ids ? ids.has(m) : i < STATS_SHOWN))
    return [kept, entries.length - kept.length]
  }
  const counted = (kept: string[], rest: number, report: string) => kept.length
    ? kept.join(' · ') + (rest ? ` · ${rest} more (${report})` : '') : `${rest} model${rest === 1 ? '' : 's'} (${report})`
  const sized = Object.entries(f.sizes ?? {}) as [string, any][]
  if (sized.length) out.push({ label: 'By size', text: sized.map(([k, v]) => `${ROLE_WORDS[k] ?? k} ${v.tasks} (${v.accepted_pct}%)`).join(' · ') })
  const guards = Object.entries(f.guards ?? {}) as [string, number][]
  if (guards.length) out.push({ label: 'What stopped agents', text: guards.sort((a, b) => b[1] - a[1])
    .map(([g, n]) => `${GUARD_WORDS[g] ?? g} ${n}`).join(' · ') })
  const [helps, helpsRest] = pick(Object.entries(f.helps ?? {}) as [string, any][])
  const helped = helps.map(([model, h]) => [model, Object.entries(h).filter(([, v]: any) => v[1])
    .map(([k, v]: any) => `a ${k === 'smaller' ? 'smaller step' : k} helped ${v[0]} of ${v[1]}`)] as [string, string[]])
    .filter(([, said]) => said.length)
  helped.forEach(([model, said]) => out.push({ label: `What helps (${word(model)})`, text: said.join(' · ') }))
  // the rest on a row of their own, not on the last model's, where it read as that model's (2026-11-21);
  // no text command prints them, the feed does
  if (helpsRest) out.push({ label: 'What helps', text: `${helped.length ? `${helpsRest} more` : helpsRest} model${helpsRest === 1 ? '' : 's'} (wb stats --json)` })
  const [bench, benchRest] = pick(Object.entries(f.bench ?? {}) as [string, any][])
  // the newest finished run, and a newer one beside it, never in its place (#570)
  const sofar = (p: any) => `${p.of} of ${p.planned} ${p.running ? 'so far' : 'before it stopped'}` +
    (p.of ? `, ${p.passed} passed` : '')
  if (bench.length || benchRest) out.push({ label: 'Bench', text: counted(bench.map(([m, b]) => `${word(m)} ` +
    [b.run !== undefined ? `${b.passed}/${b.of} (${b.day})` : '', b.partial ? `newer run ${sofar(b.partial)}` : '']
      .filter(Boolean).join(', ')), benchRest, 'wb bench report') })
  const [opt, optRest] = pick(Object.entries(f.optimized ?? {}) as [string, any][])
  if (opt.length || optRest) out.push({ label: 'Optimized', text: counted(opt.map(([m, o]) => `${word(m)} ${Math.round(o.window / 1024)}K` +
    (o.spill ? ` (${o.spill}% on the CPU)` : ' all on the GPU')), optRest, 'wb optimize report') })
  return out
}

// The file tools' path fields: an edit in a task's worktree is writing that task's code
const EDIT_TOOLS: Record<string, string> = { Edit: 'file_path', MultiEdit: 'file_path', Write: 'file_path', NotebookEdit: 'notebook_path' }

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
  const field = EDIT_TOOLS[e.tool]
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
  if (mode === 'decide') return ''
  const file = `${mem.root}/tasks/${task}/coding.json`
  let allowed: Row[] = []
  try {
    allowed = JSON.parse(await $.fs.read(file)).allowed || []
  } catch {}
  const open = allowed.find(x => !x.used)
  if (open) {
    // a one-task exception (#94) is under way from its first edit: the next hand-back spends it
    if (open.when && !open.started) {
      open.started = (await $.clock.now()) / 1000
      await $.fs.write(file, JSON.stringify({ allowed })).catch(() => {})
    }
    return ''
  }
  if (mode === 'ask') {
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
  const out: string[] = []
  if (want.notes && !was.notes) out.push(NOTES_NOTE(want.notes))
  if (want.wakeOff && !was.wakeOff) out.push(WAKE_OFF_NOTE)
  const any = delegating || mem.open.size > 0 // out of the mode with nothing open: nothing changes
  if (any && want.coding !== was.coding) out.push(want.coding === 'decide' ? CODING_DECIDE : CODING_RULES[want.coding])
  // the roles (#1014): any change, however made (the dialog, the pane, a terminal, a hand edit)
  if (any && want.routes !== was.routes) out.push(was.routes ? ROUTES_CHANGED(want.routes) : ROUTES_NOTE(await readRoutes($)))
  if (any && want.workflows !== was.workflows) out.push(want.workflows === 'on' ? WORKFLOWS_RULE : WORKFLOWS_OFF) // #1041
  await setSaid($, { notes: was.notes || want.notes, wakeOff: was.wakeOff || want.wakeOff, coding: any ? want.coding : was.coding,
    routes: any ? want.routes : was.routes, workflows: any ? want.workflows : was.workflows })
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
  wake: true,
  asksOpen: new Set<string>(),  // chat asks this session is waiting on (askOnChat)
  root: '',
  startedAt: 0,
  lastDrawn: '',
  lastNotify: '-', // notify.jsonl's size and mtime when last read
  notifyRows: { key: '', rows: [] as Row[] }, // its rows as last parsed, under that key (#921)
  paneOpened: false,
  polling: false,
  watching: false,
  picked: '', // the task whose row the user pressed: followed while listed (#1048)
  rolesReading: false, // a rolesNow for the Tasks tab's day-cap line under way (capNow)
  wb: 'wb',
  surfaces: new Set<string>(), // where this session is drawn: session.start's, then each attach's (#1200)
  liveKey: '', // the followed task and its updated_at when last read
  liveAt: 0, // when its transcript was last read
  statsReading: false, // a `wb stats --json` under way (refreshStats)
  statsLoaded: false, // the history was read in this load: the atom's feed may be an older load's
  statsWhy: '', // why the last `wb stats --json` failed ('' when it didn't): said on the tab (#553)
  awayNoted: false, // AWAY_NOTE said in full this away stretch (awayToo)
  statsRetryAt: 0, // not again before then, unless the tab is pressed
  statsBackoff: 0, // the wait after the last failure, doubling
  topWhy: '', // why the last `wb top` ended by itself: its stderr's last line
  topFails: 0, // `wb top` runs in a row that ended with no tick: said under Now from the second
  topBackoff: 0, // the wait before the next try, doubling
  top: null as any, // the `wb top --json` child while the live view runs (syncTop)
  topKids: new Set<any>(), // every child started and not yet ended: stopTop ends them all (#546)
  topGen: 0, // bumped by each start and stop: a syncTop whose awaits saw it change starts nothing
  topRetryAt: 0, // not before then, after one ended by itself
  roles: null as null | PaneRoles, // the Settings tab's roles and models, as wb last said them (rolesNow, #1023)
  settingsCols: 0, // the Settings tab's body width at this draw (0 unknown): settingRow folds a long row to it
  provNames: new Map<string, string>(), // each API provider's id → its name, from the tasks' task.json (namesNow)
  apiOther: '', // the role whose API row's Other model… box is open (#1023)
  cwd: '', // the session's directory, resolved: which old tasks are this session's (mine)
  home: '', // the home directory, resolved: a session there claims no task by directory
  ids: new Set<string>(), // the `sessions` atom: which tasks are this session's (mine)
  deadPids: new Set<string>(), // Claude Code processes tasks recorded that are gone (pid@start): final (mine)
  pidAt: new Map<string, number>(), // when a live one was last checked (noteClaude)
  person: false, // someone can be at this session (not a bare `claude -p` or SDK run)
  started: false, // the ears and timers run
  busy: false, // Claude's main loop is mid-turn: a wake would queue and arrive stale
  queued: [] as string[], // turns for Claude that a command asked for
  probe: null as null | { at: number; said: ProbeState }, // wb's last word on the probe experiment (probeStatus)
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
  agentTurn: new Map<string, string>(), // an agent's message given to Claude (id → its name): its turn's dialogs go to the page (#1042)
  chatAsks: new Map<string, ChatAsk>(), // dialogs on the page this session waits on: whose call (#1042)
  listenerAt: new Map<string, number>(), // another session's chat listener (pid@start), when last seen alive (#839)
  submitted: false, // this mod submitted a prompt while Claude was idle, and no turn has started since (#837)
  chatUrlAt: 0, // when the web chat's link was last looked up
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
  tick: 0, // the apprentice's frame count
  // the opening animation while it plays (#963): its plan once the pane drew it, the frame on
  // screen, the ticks it waited for a first draw, its timer
  splash: null as null | { plan: Plan | null; f: number; waited: number; timer: { cancel: () => void } | null },
  // the modeline's ticker while its line is wider than the bar (#976): the line, its columns, the step
  ticker: null as null | { line: string; room: number; off: number; timer: { cancel: () => void } },
  posed: '' as Pose | '', // its pose as last drawn in a Raster: the timer animates it while set
  shown: '' as Pose | '', // the frame on screen now
  skill: '', // the /workbench skill's SKILL.md, for the workbench-mode block
  commandsFile: '', // commands.json ($WB_COMMANDS): Claude's edit never drops a repo's local_only (#1021)
  fold: false, // the session's directory is on a file system that ignores case (macOS): `within`
  termDialogs: [] as TermDialog[], // dialogs up in the terminal while the user wasn't away (#959)
}

// Whether path b is path a or inside it, by whole components: /x/ab is not inside /x/a. In lower
// case where the file system ignores it (`mem.fold`, macOS): ~/Repos typed for ~/repos once
// missed (the 2026-11-19 audit). wb records task paths real (getcwd, git's toplevel).
const parts = (p: string) => (mem.fold ? p.toLowerCase() : p).split('/').filter(c => c && c !== '.')
const within = (a: string, b: string) => {
  const [pa, pb] = [parts(a), parts(b)]
  return pa.length <= pb.length && pa.every((c, i) => c === pb[i])
}

// Whether a task is this session's. One `wb start` ran in this process's sessions (task.json
// `session`) is, wherever it ran: by directory alone, a `cd /tmp/x && wb start` task was no one's
// (the 0.90 review). One without (started by hand, or before 0.90) goes by directory: its repo
// is the session's directory or inside it (a folder of several repos), or the session is inside
// the task's spot (`spotOf`: its package, else its cwd, else the repo; #695), or where `wb start` ran (`cwd`) is the session's directory or inside it. So
// does another session's whose Claude Code process (`claude_pid`) is gone: a new process in the
// project adopts it (the 0.90 review: after a crash or an update, plain `claude` in the same repo
// was woken for none of them). A session in the home directory or / claims none by directory: a
// task started from ~ belonged to every session under it (the 0.90 review). A task with no key
// at all is everyone's. `wb`'s _scope judges the same.
// A session inside the repo counts only inside the task's own spot: its package (repo/subdir),
// else where `wb start` ran when that is inside the repo, else the repo. Inside the repo alone, a
// session in a monorepo's sibling package was woken for another package's review (#695).
const spotOf = (t: { repo?: string; cwd?: string; subdir?: string }) =>
  t.subdir ? `${t.repo}/${t.subdir}` : t.cwd && within(t.repo!, t.cwd) ? t.cwd : t.repo!
const near = (t: { repo?: string; cwd?: string; subdir?: string }) => {
  const at = parts(mem.cwd).join('/')
  if (!at || (mem.home && at === parts(mem.home).join('/'))) return false
  return (!!t.cwd && within(mem.cwd, t.cwd)) ||
    (!!t.repo && (within(mem.cwd, t.repo) || within(spotOf(t), mem.cwd)))
}
const mine = (t: { session?: string; repo?: string; cwd?: string; subdir?: string; claude_pid?: number; claude_start?: string }) => {
  if (t.session && mem.ids.size) {
    return mem.ids.has(t.session) || (mem.deadPids.has(pidKey(t)) && near(t))
  }
  if (!mem.cwd || (!t.cwd && !t.repo)) return true
  return near(t)
}

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
async function claim($: any, name: string, sp: Row): Promise<Row> {
  if (!mem.claudePid || !sp.session || !mem.ids.size || !mem.deadPids.has(pidKey(sp)) || ownPid(sp)) return sp
  if (!mem.ids.has(sp.session) && !near(sp)) return sp
  const id = await $.session.id().catch(() => '')
  const ids = [...new Set([id, ...mem.ids].filter(Boolean))]
  const r = await runWb($, ['adopt', name, '--pid', String(mem.claudePid), ...ids.flatMap(i => ['--session', i])], 15_000)
  const now = await readJson($, `${mem.root}/tasks/${name}/task.json`)
  if (!r.ok && now.claude_pid === sp.claude_pid) mem.deadPids.delete(pidKey(sp)) // wb couldn't say: not ours to claim
  // a task adopted into this session was being delegated: the mode comes back with it (#689).
  // Not one the user's quit parked, nor one handed back to Claude (taken over): adopting those
  // turned the mode back on against /workbench-quit or the hand-back (#834).
  if (r.ok && delegatedWork(await readJson($, `${mem.root}/tasks/${name}/status.json`))) await workbenchMode($, true)
  return now.name ? now : sp
}

// Whether a task (its status.json) is still delegated work: running, or waiting on Claude and
// neither parked by a quit (store.parked, #698) nor taken over by Claude (#834)
const delegatedWork = (st: Row) => {
  const phase = String(st.phase ?? 'queued')
  if (ACTIVE.has(phase)) return true
  return NEEDS.has(phase) && !(['review', 'stuck', 'failed'].includes(phase) && st.parked) && !st.taken_over
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
const NOTIFY_TAIL = 1 << 20 // far more than OLDEST_WAKE_MS's hour of notices
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
    const st = await readJson($, `${base}/status.json`)
    const phase = String(st.phase ?? 'queued')
    const updatedAt = Number(st.updated_at ?? 0) * 1000
    if (!ACTIVE.has(phase) && !NEEDS.has(phase) && now - updatedAt > KEEP_CLOSED_MS) {
      if (phase === 'accepted' || phase === 'discarded') mem.gone.add(ent.name)
      continue
    }
    let sp = await readJson($, `${base}/task.json`)
    if (!sp.name || sp.bench) continue // a replay of wb bench's: no one's task, never shown or woken
    let recent: string[] = []
    if (ACTIVE.has(phase) || NEEDS.has(phase)) {
      await noteClaude($, sp, now)
      sp = await claim($, ent.name, sp)
      recent = await recentEvents($, ent.name, `${base}/events.jsonl`)
    }
    if (typeof sp.provider === 'string' && typeof sp.provider_name === 'string' && sp.provider_name)
      mem.provNames.set(sp.provider, sp.provider_name)
    list.push({
      name: sp.name, phase, round: Number(st.round ?? 1), model: String(sp.model ?? ''),
      label: String(sp.model_label ?? ''),
      // how it was routed (#1016): task.json's via, role and provider, and its routing line
      via: sp.via === 'api' ? 'api' : 'gpu', role: String(sp.role ?? ''), provider: String(sp.provider_name ?? sp.provider ?? ''),
      routed: String(sp.route ?? ''),
      tests: st.tests ? (st.tests.ok ? 'pass' : 'fail') : null,
      last: String(st.last ?? ''), createdAt: Number(sp.created_at ?? 0) * 1000, updatedAt,
      tokens: Number(st.tok_in ?? 0) + Number(st.tok_out ?? 0), recent,
      repo: String(sp.repo ?? ''), cwd: String(sp.cwd ?? ''), subdir: String(sp.subdir ?? ''),
      session: String(sp.session ?? ''),
      claude_pid: Number(sp.claude_pid ?? 0), claude_start: String(sp.claude_start ?? ''),
      backlog: String(sp.backlog ?? ''),
      parked: ['review', 'stuck', 'failed'].includes(phase) && !!st.parked, // as store.parked (#698)
      allowed: allowedLine((await readJson($, `${base}/coding.json`)).allowed),
      ...viewFacts(sp, st),
    })
  }
  for (const name of mem.gone) if (!names.has(name)) mem.gone.delete(name) // removed by hand
  for (const name of mem.recent.keys()) if (!names.has(name) || mem.gone.has(name)) mem.recent.delete(name)

  return list.sort((a, b) => a.createdAt - b.createdAt)
}

// The Tasks view's facts of a task (#1048, tasks.ts), from the task.json and status.json scan
// already read: nothing new is read for them
export function viewFacts(sp: Row, st: Row) {
  const num = (x: unknown) => (Number.isFinite(Number(x)) ? Number(x) : 0)
  const live = st.live_used && typeof st.live_used === 'object' ? st.live_used : {}
  const api = st.api && typeof st.api === 'object' ? st.api : null
  const typical = st.typical && typeof st.typical === 'object' ? st.typical : {}
  const fresh = Number((st.tuned ?? {}).fresh_at)
  return {
    group: typeof sp.group === 'string' ? sp.group : '',
    cards: Array.isArray(st.cards) ? st.cards.filter((c: unknown) => Number.isInteger(c)) as number[] : [],
    tools: num(st.tools), tokOut: num(st.tok_out),
    // the window's fill only while it is this session's: a fresh session's first step reads 0 (#262)
    ctxUsed: live.session && live.session === st.session ? num(live.used) : 0,
    window: num(st.window), freshAt: Number.isFinite(fresh) && fresh > 0 ? fresh : null,
    autoFixes: num(st.auto_fixes), autoFixMax: num(sp.auto_fix), roundFor: String(st.round_for ?? ''),
    reviews: num(st.reviews), steps: num(st.steps),
    // runner.active_so_far's parts: the step's rounds so far, and the one under way from its start
    activeS: Math.max(0, num(st.active_s) - num(st.step_active_s)), roundStartedAt: num(st.round_started_at) * 1000,
    typicalMin: num(typical.min), typicalN: num(typical.n), lateMin: num(st.late_min),
    spend: !api ? null : api.plan ? 'plan' as const : typeof api.usd === 'number' ? api.usd as number : 'none' as const,
    est: !!api && isEst(api.prices),
  }
}

// The Tasks list's rows (#1048, tasks.ts): one or two a task, one when the pane can't hold two
// (compactWanted), each with its task; the user's allowance for a task (`wb coding allow`) after
// its rows. The render draws them and pageShape counts them, one row each: none wraps.
type ListRow = { parts: RowPart[]; task: string; first: boolean }
function listRows(list: WbTask[], followed: string, now: number, spin: string, cols: number, bodyRows: number): ListRow[] {
  const open = list.filter(t => t.phase !== 'accepted' && t.phase !== 'discarded')
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

async function compactAtBoundary($: any): Promise<void> {
  const set = await $.env.get('WB_COMPACT_AT') // a percentage; 0 turns it off
  const at = set && Number.isFinite(Number(set)) ? Number(set) / 100 : COMPACT_AT
  if (!at) return
  let context
  try {
    ({ context } = await $.session.usage({ breakdown: 'summary' }))
  } catch {
    return // no reading yet (a session before its first turn)
  }
  const b = context.breakdown
  const limit = b?.isAutoCompactEnabled && b.autoCompactThreshold ? b.autoCompactThreshold : context.window
  if (!context.tokens || context.tokens < at * limit) return
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
// Settings tab after /reload-plugins and stayed ROLES_CACHE_MS, for a command that takes 0.12 s
// (#1142, 2026-11-23). A caller that keeps what it read keeps its last good value instead.
export type WbRun = { ok: boolean; out: string; aborted?: boolean }
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

// The task worth watching. The one followed stays while it runs: with two running, choosing by
// the latest status write swapped the pane between them on every tool call (the 2026-11-16
// audit). Else the newest started that runs (running, testing, asking before queued: a queued one
// shows nothing yet), else the newest waiting for Claude. A task the user pressed (mem.picked,
// #1048) is followed while it is listed, whatever its phase.
const focusOf = (list: WbTask[], followed: string, picked = mem.picked): WbTask | undefined => {
  const chosen = picked ? list.find(t => t.name === picked) : undefined
  if (chosen) return chosen
  const kept = list.find(t => t.name === followed && ACTIVE.has(t.phase))
  if (kept) return kept
  const by = (ts: WbTask[], k: 'createdAt' | 'updatedAt') => [...ts].sort((a, b) => b[k] - a[k])[0]
  return by(list.filter(t => ACTIVE.has(t.phase) && t.phase !== 'queued'), 'createdAt') ??
    by(list.filter(t => t.phase === 'queued'), 'createdAt') ??
    by(list.filter(t => NEEDS.has(t.phase)), 'updatedAt')
}

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
  // only the Tasks tab draws the transcript: on Stats or Settings each running round was exported
  // every LIVE_EVERY_MS for nothing (#838); the key still differs, so it is read on the way back
  if ((await read($, pane)).tab !== 'tasks') return
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

// API dollars (#1022): OpenCode's unit, so a `$`, unlike the electricity's cost (the user's own
// price, #609): meter.usd's rule, so the CLI and the pane agree: $0.42, `<$0.01` under a cent,
// a free model's $0 (#1322: one rule, where two said one cap two ways)
export function usd(x: number): string {
  if (x === 0) return '$0'
  return Math.abs(x) < 0.01 ? '<$0.01' : `$${x.toFixed(2)}`
}

// The words one model or provider goes by on every tab (#1048's review, seen live): a GPU model by
// its label (`qwen3-coder 30B`, as the Tasks view and Now say it), not its id in the history under
// Now; an API model as the Tasks view's place and model (`API DeepSeek deepseek-v4.1-flash`, the
// id's last part, tasks.ts); a provider by its name (`DeepSeek`), never its id on one tab and its
// name on the next. From what the pane has read already (wb models --json, wb route --json's
// providers, the tasks' task.json, wb top's rack): an id nothing named stays the id.
export type Names = { models: Record<string, string>; providers: Record<string, string> }
export const NO_NAMES: Names = { models: {}, providers: {} }
export const providerWord = (id: string, n: Names) => n.providers[id] || id
export function modelWord(id: string, api: boolean, n: Names): string {
  if (!api) return n.models[id] || id
  const cut = id.indexOf('/') // the provider ends at the first /
  return cut > 0 ? `API ${providerWord(id.slice(0, cut), n)} ${id.split('/').pop()}` : `API ${id}`
}
function namesNow(list: WbTask[], top: WbTop | null): Names {
  const n: Names = { models: {}, providers: {} }
  for (const g of mem.roles?.models?.gpu ?? []) if (g.label) n.models[g.id] = g.label
  for (const m of (top?.snap?.rack ?? []) as Row[]) if (m.label && !n.models[m.model]) n.models[m.model] = m.label
  for (const t of list) if (t.via !== 'api' && t.label && !n.models[t.model]) n.models[t.model] = t.label
  for (const [id, p] of Object.entries(mem.roles?.routes?.providers ?? {})) if (p.name) n.providers[id] = p.name
  for (const [id, name] of mem.provNames) if (!n.providers[id]) n.providers[id] = name
  return n
}

// API dollars are estimates at the catalog's prices unless the user set the model's (wb model price,
// #1135, 2026-11-23: the catalog ran about 2x DeepInfra's bill): `prices` is meter.basis's, missing
// on rows from before it (the catalog's)
export const isEst = (prices: unknown) => prices !== 'yours' && prices !== 'provider'
// the `≈` alone says it: a "(catalog prices)" note after it said it twice (#1322)
export const estUsd = (x: number, prices: unknown) => (x && isEst(prices) ? '≈' : '') + usd(x)

// A period's API money as the Stats tab says it: never $0 for tasks with no price; '' with none
export function apiMoney(a: any): string {
  if (!a || !a.tasks) return ''
  const known = a.tasks - (a.unknown ?? 0) - (a.plan ?? 0)
  return known > 0 ? estUsd(a.cost ?? 0, a.prices) : a.plan ? 'plan usage' : 'no known cost'
}

// A Stats period's API part (the feed's periods[*].api): API $1.27 (DeepSeek $0.84 · Anthropic $0.43)
export function apiPeriod(a: any, names: Names = NO_NAMES): string {
  const money = apiMoney(a)
  if (!money) return ''
  const provs = Object.entries(a.by_provider ?? {}).filter(([, v]: any) => v.tasks > (v.unknown ?? 0) + (v.plan ?? 0))
    .sort((x: any, y: any) => y[1].cost - x[1].cost).map(([p, v]: any) => `${providerWord(p, names)} ${estUsd(v.cost, v.prices)}`)
  const inside = provs.join(' · ')
  return `API ${money}${inside ? ` (${inside})` : ''}${a.unknown && money !== 'no known cost' ? ` + ${a.unknown} with no known cost` : ''}`
}

// An API task's RUNNING row's parts after its phase (wb top's `spend`, `waits`); its place and model
// lead the row, as on the Tasks view (`API DeepSeek deepseek-flash`, modelWord)
export function apiParts(r: Row): string[] {
  if (r.via !== 'api') return []
  return [typeof r.spend === 'number' ? estUsd(r.spend, r.prices) : '', r.waits ? `waits for ${r.waits}` : '']
}

// A typed amount (the day cap's, parseUsd; it was the electricity price row's, #1322): "0,30",
// "€0.30", "0.30 EUR" and "$0.3" are 0.3 (one comma or point, a currency sign or code before or
// after); empty is 'off' (it clears it); anything else, "0x10" or "1e3" among them, is null.
// Number() took "0x10" as 16, and "0,30" and "€0.30" as nothing, with no hint why (#609).
export function parsePrice(text: string): number | 'off' | null {
  const s = text.trim()
  if (!s) return 'off'
  const m = /^(?:[^\d\s.,+-]{1,3}\s*)?(\d+(?:[.,]\d+)?|[.,]\d+)(?:\s*[^\d\s.,+-]{1,3})?$/u.exec(s)
  const v = m ? Number(m[1].replace(',', '.')) : NaN
  return v > 0 ? v : null
}

// The Settings tab's roles (#1023): Shared (the Agents switch, the busy row, each role's GPU | API,
// Dynamic workflows, the day cap, wake), GPU (each role's GPU model, the sizes, optimize) and API
// (connected providers, each role's API model and thinking, spend today). Read through wb (`wb route
// --json`, `wb models --json`: neither asks a server, #705), kept ROLES_CACHE_MS and dropped after a
// pick; routes.json stays flat, only the screen groups. A press is the user's yes: the mod writes the
// dialog's one-shot approval (route-ok.json, `by` pane) for exactly that change, as `wb route
// --dry-run --json` gives it, then runs it, under the same gate Claude meets; nothing is a flag on
// the command line, which Claude could pass. A GPU pin or a size takes no yes (dry run: no gate).
const ROLES_CACHE_MS = 10_000
const upper = (w: string) => w.charAt(0).toUpperCase() + w.slice(1)
// a no-break space inside a unit the pane's edge must not split: `GPU 0+1` wrapped as "GPU" and
// "0+1", `per M` as "per" and "M" (#1048's review, seen at 120 and 80 columns)
const NB = '\u00a0'
const GPU_ROWS = 6 // GPU models with a sizes row; the rest are counted (wb models), as STATS_SHOWN
const API_MENU = 12 // an API dropdown lists every runnable model up to this; past it, the ones in use
const ROLE_LABELS: Record<string, string> = { significant: 'Complex coding', trivial: 'Trivial coding', writing: 'English writing' }
const OLDER_WB = 'wb is older than the mod: wb update'
const NO_PROVIDER = 'No API provider connected: /connect in OpenCode adds one; every role runs on the GPU.'
const AGENTS_STOPS: [string, string, string][] = [
  // tasks already started too: an API task stops before its next round (#1213, 2026-11-24: only
  // wb start read the switch, and a queued API task spent after 'nothing spends')
  ['gpu', 'GPU', 'Every role runs on its GPU model: nothing spends, and an API task already started stops before its next round. Each role\'s own choice is kept for Both.'],
  ['api', 'API', 'Every role runs on its API model; a role with none can\'t start.'],
  ['both', 'Both (default)', 'Each role runs where its own row says.'],
]
// every value capitalized, as every other row's (#1048's review: "wait (default)" beside "Both (default)")
const BUSY_STOPS: [string, string, string][] = [
  ['wait', 'Wait (default)', 'A task waits until the GPU is free.'],
  ['api', 'Use the role\'s API model', 'A task whose role has an API model starts on it while the GPU is busy; a running task never moves.'],
]
const WORKFLOWS_STOPS: [string, string, string][] = [
  ['off', 'Off (default)', 'Claude runs one task at a time.'],
  ['on', 'On', 'Claude may split a request into tasks that run at once, each on its own role\'s model.'],
]
const THINK_STOPS: [string, string, string][] = [
  ['off', 'Off (default)', 'Faster and cheaper; for tasks started after this.'],
  ['on', 'On', 'Better on hard steps, slower and costs more; for tasks started after this.'],
]
// A role's thinking row: THINK_STOPS, each stop its API model can't keep saying what the model does
// instead (the row's `thinks`, thinkingSaid's): `Off (default)`, `Faster and cheaper` sat on a model
// that can't turn thinking off, so the user kept it and paid for its reasoning (#1263, 2026-11-24).
// Only a model's default or a thinking that stays on (runner's THINKING_DEFAULT, THINKING_STAYS) is
// no cheaper: its lowest effort (THINKING_LOWEST, below the provider's default) still is, and the row
// said On cost the same (#1263's review, 2026-11-24)
const LOWEST_SAID = 'thinking at its lowest effort'
export function thinkStops(row: Row): [string, string, string][] {
  const said = (v: string) => thinkingSaid({ ...row, thinking: v })
  const off = said('off'), on = said('on')
  const offWhy = off.startsWith(LOWEST_SAID)
    ? 'This model can\'t turn thinking off, but its lowest effort is still cheaper than On; for tasks started after this.'
    : 'This model can\'t turn thinking off, so it isn\'t cheaper; for tasks started after this.'
  return [
    off.startsWith('thinking off') ? THINK_STOPS[0]! : ['off', `Off (default): ${off}`, offWhy],
    on.startsWith('thinking off') ? ['on', `On: ${on}`, 'This model doesn\'t reason, so this changes nothing; for tasks started after this.'] : THINK_STOPS[1]!,
  ]
}
// a size in the roles' words (tasks.ts ROLE_WORDS): `significant only` sat under "Complex coding"
const SIZE_STOPS: [string, string, string][] = [
  ['all', 'Any size (default)', 'The router may give it any task.'],
  ['trivial', `${upper(ROLE_WORDS.trivial!)} only`, 'The router gives it trivial coding tasks only.'],
  ['significant', `${upper(ROLE_WORDS.significant!)} only`, 'The router gives it complex coding tasks only.'],
]
// A role's GPU | API row. `name`: its API model as the API group's row names it (the catalog's name),
// so one model isn't `deepseek/deepseek-flash` here and `DeepSeek Flash` three rows down
export function roleStops(label: string, api: string | undefined, name = api): [string, string, string][] {
  return [['gpu', 'GPU', `${label} runs on this machine's GPU.`],
    ['api', api ? `API (${name})` : 'API (set its model under API first)', api ? `${label} runs on ${api}; your code goes to its provider.` : 'Pick its API model under API first.']]
}

// A model's window as the pane says it: 40K
const windowK = (n: unknown) => typeof n === 'number' && n > 0 ? ` (${Math.round(n / 1024)}K)` : ''

// The GPU dropdown's entries: Auto first (the router, today's behaviour), then each GPU model with its
// window and, on a machine with several cards, the ones its Ollama sees (`GPU 2+3`; nothing unknown)
export function gpuStops(label: string, gpu: WbModels['gpu'], pinned: string): [string, string, string][] {
  const out: [string, string, string][] = [['auto', 'Auto (the router picks)', `The router picks ${label}'s GPU model for each task.`]]
  for (const g of gpu)
    out.push([g.id, `${g.label}${windowK(g.window)}${g.cards?.length ? ` · GPU${NB}${g.cards.join('+')}` : ''}`, `Pins ${label} to ${g.id}: the router doesn't pick for it.`])
  if (pinned !== 'auto' && !gpu.some(g => g.id === pinned)) out.push([pinned, `${pinned} (not found: wb models)`, 'A task of this role exits 3 until it is found again or another is picked.'])
  return out
}

// The API dropdown's entries: the role's own model, the other roles', those used before, and with a
// catalog no longer than API_MENU every runnable one (grouped by provider); then Other model…, a typed
// id wb checks. Only providers whose key wb can hold are listed (wb models --json); a free model
// carries its data-use mark (#1021).
export function apiStops(label: string, m: WbModels | null, current: string, others: string[]): [string, string, string][] {
  const listed = new Map((m?.api ?? []).map(x => [x.id, x]))
  const price = (p: WbModels['api'][number]['price']) => p && typeof p.input === 'number' && typeof p.output === 'number'
    ? ` · $${p.input}/$${p.output} per${NB}M` : ''
  const line = (id: string): [string, string, string] => {
    const x = listed.get(id)
    if (!x) return [id, id, `${label} uses ${id}.`]
    return [id, `${x.name} · ${x.provider_name}${windowK(x.window)}${price(x.price)}${x.free ? ` · ${m?.free_line || 'free'}` : ''}`, `${label} uses ${id} on API; your code goes to ${x.provider_name}.`]
  }
  const ids: string[] = []
  const add = (id: string | undefined) => { if (id && !ids.includes(id)) ids.push(id) }
  add(current)
  others.forEach(add)
  ;(m?.used ?? []).filter(id => listed.has(id)).forEach(add)
  if ((m?.api.length ?? 0) <= API_MENU)
    [...(m?.api ?? [])].sort((a, b) => `${a.provider_name}\0${a.name}`.localeCompare(`${b.provider_name}\0${b.name}`)).forEach(x => add(x.id))
  return [...(current ? [] : [['none', '(none)', `${label} has no API model: it runs on the GPU.`] as [string, string, string]]),
    ...ids.map(line), ['other', 'Other model…', 'Type an exact id (provider/model); wb checks it.']]
}

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

// A pane press on the roles: the change as wb computes it, approved for exactly that when it needs the
// user's yes (the press is it), then made; wb's refusal (no day cap, an unknown id, a key wb can't hold)
// in a toast, and the row keeps its old value. A change that took the yes says its line in a toast:
// the press is the yes, so no dialog showed what it agreed to (where the code goes, what the model does
// with thinking: #1263, 2026-11-24)
async function paneRoute($: any, args: string[]): Promise<boolean> {
  mem.roles = null
  const dry = await runWb($, ['route', '--dry-run', '--json', ...args], 60_000)
  let q: Row | null = null
  try { q = dry.ok ? JSON.parse(dry.out) : null } catch { q = null }
  if (!q) { $.ui.toast(`workbench: ${brief(dry.out)}`); return false }
  if (q.gate && q.approval)
    await $.fs.write(`${mem.root}/${ROUTE_OK}`, JSON.stringify({ ...q.approval, by: 'pane', at: (await $.clock.now()) / 1000 })).catch(() => {})
  const { ok, out } = await runWb($, ['route', ...args])
  mem.roles = null
  if (!ok) $.ui.toast(`workbench: ${brief(out)}`)
  else if (q.gate && q.change) $.ui.toast(`workbench: ${q.change}`)
  return ok
}

// The day cap as typed, as `wb route cap` takes it: "$5", "5,00", "5 USD" are 5; empty is 'off'
// (clears it); else null. parsePrice's forms (Couplings)
export function parseUsd(text: string): number | 'off' | null {
  return parsePrice(text)
}

async function setCap($: any, text: string) {
  const v = parseUsd(text)
  if (v === null) { $.ui.toast('workbench: the API day cap is dollars, like 5 or $5 (empty clears it)'); return }
  await paneRoute($, ['cap', String(v)])
  $.ui.invalidate('ui.render')
}

// Spend today, from `wb route --json`: `≈$0.42 of $5 (DeepSeek $0.28 · Anthropic $0.14)`; the dollars by
// `usd`, the Stats tab's (#1022), so the two never say one sum two ways (wave 2's merge), and each
// provider by its name and the Stats tab's ` · ` (#1048's review: `deepseek $0.28, anthropic`)
export function spendLine(r: WbRoutes): string {
  const s: WbRoutes['spend_today'] = r.spend_today ?? { usd: 0, by_provider: {} }
  const by = Object.entries(s.by_provider ?? {}).filter(([, v]) => v > 0).map(([p, v]) => `${r.providers?.[p]?.name || p} ${usd(v)}`)
  // ≈ unless at the user's own prices or the provider's figure (#1135): once, the providers' share it
  const each = by.length ? ` (${by.join(' · ')})` : ''
  return `Spend today: ${estUsd(s.usd ?? 0, s.prices)}${r.day_usd != null ? ` of ${money(r.day_usd)}${each}` : `${each} · no day cap`}`
}

// The Connected line: each provider, a stored key as working ("key stored": "stored" alone said
// nothing of what), the rest with why they can't run
export function connectedLine(r: WbRoutes): string {
  return 'Connected: ' + Object.values(r.providers ?? {}).map(p => p.key === 'stored' ? `${p.name} (key stored)` : `${p.name} (${p.why ?? p.key})`).join(' · ')
}

// The rows of the Settings tab's three groups (#1023), as elements: Shared's first part (above Claude
// coding), its second (the cap, wake), GPU and API. `role`: the roles' read
async function roleRows(kit: Row, $: any, view: WbPane, role: PaneRoles) {
  const { Box, Text, Button } = kit
  const r = role.routes, m = role.models
  const dim = (key: string, text: string) => infoLine(kit, key, text)
  if (!r && !role.why) return { top: [], cap: [], gpu: [], api: [] } // an aborted first read (#1142)
  if (!r) {
    const line = dim('roles-why', role.why)
    return { top: [line], cap: [], gpu: [line, ...mediaRows(kit, $, view, m)], api: [] }
  }
  const roles = ROLES.map(name => ({ name, label: ROLE_LABELS[name]!, row: r.roles[name] }))
  const top = [
    settingRow(kit, $, 'agents', 'Agents', AGENTS_STOPS, r.use, view, v => paneRoute($, ['all', v])),
    settingRow(kit, $, 'busy', 'When the GPU is busy', BUSY_STOPS, r.busy ?? 'wait', view, v => paneRoute($, ['busy', v])),
    ...roles.map(({ name, label, row }) => settingRow(kit, $, `role-${name}`, label,
      roleStops(label, row.api, m?.api.find(x => x.id === row.api)?.name), row.via, view, v => paneRoute($, [name, v]))),
  ]
  const cap = [
    <kit.Input key="cap" label="▸ API day cap" value={r.day_usd != null ? String(r.day_usd) : ''}
      placeholder="not set: $5 when the first role goes to API" submitLabel="set" onSubmit={(v: string) => { void setCap($, v) }} />,
  ]
  const gpu: any[] = []
  if (!m) gpu.push(dim('gpu-why', OLDER_WB))
  else if (!m.gpu.length) gpu.push(dim('gpu-none', 'No GPU model found (wb models)'))
  else {
    for (const { name, label, row } of roles)
      gpu.push(settingRow(kit, $, `gpu-${name}`, `${label} GPU model`, gpuStops(label, m.gpu, row.gpu), row.gpu, view, v => paneRoute($, [name, '--gpu', v])))
    const pinned = new Set(roles.map(x => x.row.gpu))
    const shown = [...m.gpu].sort((a, b) => Number(pinned.has(b.id)) - Number(pinned.has(a.id)) || a.label.localeCompare(b.label)).slice(0, GPU_ROWS)
    for (const g of shown) {
      const size = g.sizes.length === 1 ? g.sizes[0]! : 'all'
      // "<model> tasks": the model's name alone (`devstral 24B: trivial only`) didn't say what the row sets
      gpu.push(settingRow(kit, $, `size-${g.id}`, `${g.label} tasks`, SIZE_STOPS, size, view,
        v => runWb($, ['model', 'sizes', g.id, ...(v === 'all' ? [] : [v])]).then(({ ok, out }) => { mem.roles = null; if (!ok) $.ui.toast(`workbench: ${brief(out)}`) })))
    }
    if (m.gpu.length > GPU_ROWS) gpu.push(dim('gpu-more', `+${m.gpu.length - GPU_ROWS} more (wb models)`))
    const o = m.optimize
    gpu.push(
      // the date as the Stats tab's (2026-11-20), not 11-20 (#1048's review)
      <Box key="optimize-row" flexDirection="row" flexWrap="wrap" columnGap={1} paddingLeft={2}>
        <Text dimColor>{`Optimize: ${o.running ? 'measuring now' : o.models ? `measured ${o.models} model${o.models === 1 ? '' : 's'}${o.done_at ? ` on ${new Date(o.done_at * 1000).toISOString().slice(0, 10)}` : ''}` : 'not run yet'} ·`}</Text>
        <Button key="optimize" label="▸ Measure now" plain onPress={() => {
          void runWb($, ['optimize', 'start']).then(({ out }) => { mem.roles = null; $.ui.toast(`workbench: ${brief(out)}`) })
        }} />
      </Box>)
  }
  gpu.push(...mediaRows(kit, $, view, m)) // Images and Audio, after the models (#1047)
  // Ollama cloud's login runs tasks as a stored key does: NO_PROVIDER's /connect was no fix for it (#1077's review, 2026-11-23);
  // so does a key the keyhole reads from the environment (`runs`, #1189), and GitHub Copilot's
  // sign-in, which the keyhole holds as a key (#1191)
  const stored = Object.values(r.providers ?? {}).some(p => p.key === 'stored' || p.key === 'ollama' || p.key === 'copilot' || p.runs === true)
  const anyApi = roles.some(x => !!x.row.api)
  const api: any[] = []
  if (!stored && !anyApi) api.push(dim('api-none', NO_PROVIDER))
  else {
    if (Object.keys(r.providers ?? {}).length) api.push(dim('api-connected', connectedLine(r)))
    for (const { name, label, row } of roles) {
      const others = roles.filter(x => x.name !== name).map(x => x.row.api).filter((x): x is string => !!x)
      api.push(settingRow(kit, $, `api-${name}`, `${label} API model`, apiStops(label, m, row.api ?? '', others), row.api ?? 'none', view,
        async v => { if (v === 'other') { mem.apiOther = name; return } if (v !== 'none') await paneRoute($, [name, '--api', v]) }))
      if (mem.apiOther === name)
        api.push(<kit.Input key={`api-${name}-other`} label="    Exact id: " value="" placeholder="provider/model, as OpenCode names it"
          submitLabel="set" onSubmit={(v: string) => {
            const id = v.trim()
            mem.apiOther = ''
            void (id ? paneRoute($, [name, '--api', id]) : Promise.resolve(false)).then(() => $.ui.invalidate('ui.render'))
          }} />)
    }
    for (const { name, label, row } of roles)
      api.push(settingRow(kit, $, `think-${name}`, `${label} thinking`, thinkStops(row), row.thinking ?? 'off', view, v => paneRoute($, [name, '--thinking', v])))
    api.push(dim('api-spend', spendLine(r)))
  }
  return { top, cap, gpu, api }
}

// The GPU group's Images and Audio rows (#1047): a row per media kind with a choice (two or more
// engines, from `wb models --json`'s media), the engine wb image / wb audio use without --engine;
// one off this GPU ("gpu": false, another host) says so. A pick runs `wb image|audio --default NAME`
// (wb writes images.json's "default" or audio.json's "default_<kind>"); no dialog: nothing spends
// and nothing leaves the machine. An older wb's models carry no media: no rows.
// each titled as what it picks: "Images: flux" read as images on or off (#1048's review)
const MEDIA_ROWS: [string, string, string][] = [['image', 'Image engine', 'image'], ['speech', 'Speech engine', 'audio'],
  ['sfx', 'Sound effects engine', 'audio'], ['music', 'Music engine', 'audio']]
export function mediaStops(cmd: string, kind: WbMedia): [string, string, string][] {
  return kind.engines.map(e => [e.name, `${e.name}${e.gpu ? '' : ' · off this GPU'}`, `wb ${cmd} runs ${e.name} unless --engine names another.`])
}
function mediaRows(kit: Row, $: any, view: WbPane, m: WbModels | null) {
  const out: any[] = []
  for (const [kind, title, cmd] of MEDIA_ROWS) {
    const k = m?.media?.[kind as keyof NonNullable<WbModels['media']>]
    if (!k || k.engines.length < 2) continue
    out.push(settingRow(kit, $, `media-${kind}`, title, mediaStops(cmd, k), k.default ?? k.engines[0]!.name, view,
      v => runWb($, [cmd, '--default', v]).then(({ ok, out }) => { mem.roles = null; if (!ok) $.ui.toast(`workbench: ${brief(out)}`) })))
  }
  return out
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
    void refreshStats($)
    void syncTop($)
    if (!mem.busy && mem.queued.length && now >= mem.queuedRetryAt) void sendQueued($, mem.queued.shift()!)
    const isOpen = (t: WbTask) => ACTIVE.has(t.phase) || NEEDS.has(t.phase)
    // Only this session's tasks, and only in workbench mode, make a boundary: a task closing
    // compacted every session's context, delegating or not (the 2026-11-16 audit).
    const open = new Set(list.filter(t => isOpen(t) && mine(t)).map(t => t.name))
    if (on && [...mem.open].some(name => !open.has(name))) mem.boundary = true
    if (!on) mem.boundary = false
    mem.open = open
    await sayRules($, on)
    if (mem.boundary && !mem.busy && !list.some(t => waitsOnClaude(t) && mine(t))) {
      mem.boundary = false
      await compactAtBoundary($)
    }
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
    await chatLink($)
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

// Claude Code's dialogs while the user is away on the web chat: the dialog would wait for nobody,
// so it goes to the chat and the user's answer there settles it (the user, 2026-11-18): a
// permission (Allow or Deny), a question (AskUserQuestion: the options they pick), a finished
// plan (ExitPlanMode: Keep planning, with a note). Approving a plan stays in the terminal: Claude
// Code ignores a hook's allow there, with or without a setMode (seen live, 2026-11-18), though it
// takes a deny. Unanswered in ASK_WAIT_MS, or the
// turn interrupted, it expires there and the dialog comes up as usual. It waits one `wb chat wait`
// at a time (a hook's budget stands still while a `$` call runs, and one may take ten minutes at
// most), so an interrupt is seen soon.
const ASK_WAIT_MS = 30 * 60_000
const ASK_CHUNK_S = 20
const KIND: Record<string, string> = { ExitPlanMode: 'plan', AskUserQuestion: 'question' }

function askText(input: unknown): string {
  const i = (input && typeof input === 'object' ? input : {}) as Record<string, unknown>
  const what = [i.command, i.file_path, i.url, i.path, i.pattern].find(v => typeof v === 'string')
  const text = typeof what === 'string' ? what : JSON.stringify(i)
  return text.length > 1500 ? text.slice(0, 1500) + '…' : text
}

// What a dialog sends wb, within an argument's size: a plan's first DETAIL_CHARS (one past wb's
// budget, so wb cuts it and says so) and each option's preview to PREVIEW_CHARS. wb measures the
// rest and cuts it to its own budget; a long plan was refused whole, and the dialog sat in the
// terminal with the phone told nothing (#535)
const DETAIL_CHARS = 40_001
const PREVIEW_CHARS = 2000
const CUT_NOTE = ' (cut: the rest is in the terminal)'
export function askQuestions(questions: unknown): unknown {
  if (!Array.isArray(questions)) return questions
  return questions.map(q => !q || typeof q !== 'object' || !Array.isArray((q as any).options) ? q : {
    ...q, options: (q as any).options.map((o: any) => o && typeof o.preview === 'string' && o.preview.length > PREVIEW_CHARS
      ? { ...o, preview: o.preview.slice(0, PREVIEW_CHARS) + CUT_NOTE } : o),
  })
}

// The PermissionRequest answer from the user's answer in the chat, else undefined (the dialog)
async function askOnChat($: any, tool: string, input: unknown, signal?: AbortSignal, o: AskFor = {}) {
  const kind = KIND[tool] ?? 'permission'
  const i = (input && typeof input === 'object' ? input : {}) as Record<string, unknown>
  // `--opt=value`: a value starting with `-` was read as a flag (#535)
  const args = ['chat', 'ask', `--tool=${tool}`, `--kind=${kind}`, `--text=${kind === 'permission' ? askText(input) : ''}`]
  // this session's `wb events` process: once it's gone wb expires the ask, so a card left by a
  // crashed or closed session doesn't stay open for good, and pushes don't show it (#538)
  if (mem.eventsPid) args.push(`--pid=${mem.eventsPid}`)
  args.push(...askForArgs(o))
  if (kind === 'question') args.push(`--detail=${JSON.stringify({ questions: askQuestions(i.questions) })}`)
  // what the call is for, as Claude described it (Bash's description): the card leads with it
  if (kind === 'permission' && typeof i.description === 'string' && i.description.trim())
    args.push(`--detail=${JSON.stringify({ purpose: i.description.trim().slice(0, 300) })}`)
  // a plan: its text, else its file, and whose it is; wb read the newest in the plans directory,
  // any project's, so a card could show another session's plan (#253, the audit of 1.2)
  if (kind === 'plan') {
    if (typeof i.plan === 'string' && i.plan) args.push(`--detail=${JSON.stringify({ plan: i.plan.slice(0, DETAIL_CHARS) })}`)
    else if (typeof i.planFilePath === 'string') args.push(`--detail=${JSON.stringify({ plan_file: i.planFilePath })}`)
    const session = await $.session.id().catch(() => '')
    if (session) args.push('--session', session)
    if (mem.cwd) args.push('--repo', mem.cwd)
  }
  const posted = await runWb($, args, 15_000)
  const [id, state] = (posted.out.split('\n').pop() || '').split(' ')
  if (!posted.ok || !id?.startsWith('ask-')) { // the away user is told where it waits, not left guessing (#535)
    const what = kind === 'plan' ? 'a plan' : kind === 'question' ? 'a question' : 'a permission'
    await runWb($, ['chat', 'reply', `--text=Claude is waiting on ${what} in the terminal: it couldn't be shown here.`, '--to=user'], 15_000)
    $.ui.toast(`workbench: ${what} couldn't go to the web chat (${brief(posted.out)}): answer it here`)
    return undefined
  }
  if (state === 'allow') return { behavior: 'allow' as const }  // their setting: go ahead while away
  if (state === 'expired') return undefined  // a plan wb can't show: the dialog, in the terminal
  mem.asksOpen.add(id)
  const unwatch = watchAsk($, id, { tool, agent: o.agent ?? '', input: i, here: !!o.here, at: await $.clock.now() }, signal)
  try {
    return await waitOnChat($, id, kind, i, signal, o)
  } finally {
    mem.asksOpen.delete(id)
    unwatch()
  }
}

// Dialogs while Claude works on another agent's request (#1042, the owner's rule): from an
// `[agent <name>]` prompt until that turn ends, Claude Code's dialogs go to the human on the web
// page, with a push, as they do while the user is away, even when the user is at the terminal;
// the terminal's dialog stands too, and whichever answers first settles it. An agent's token
// can't answer them (wb chat serve's 403), and the page says whose request it is ("for <name>").
// At the terminal the user's away setting ("Let Claude go ahead") doesn't answer it (`--here`).
type AskFor = { for?: string[]; here?: boolean; agent?: string }

// The agents whose message the running turn took: given now (grace 0), their turn not yet ended
export function agentsFor(turn: Map<string, number>, agents: Map<string, string>): string[] {
  return [...new Set([...agents].filter(([id]) => turn.get(id) === 0).map(([, name]) => name))]
}

export function askForArgs(o: AskFor): string[] {
  return [...(o.for ?? []).map(n => `--for=${n}`), ...(o.for?.length && o.here ? ['--here'] : [])]
}

// An ask on the page is withdrawn once its dialog is settled elsewhere: the hook's signal aborts
// (an interrupt), or the engine's call of it ends (askSettledHere, in tool.call's finally). The
// page was left with a card the terminal had already answered, which the user could still tap.
// A terminal answer doesn't abort the signal, and this engine has no event as the call starts
// (`tool.run` came later; an unknown event stops the whole module loading), so a long command
// allowed in the terminal keeps its card until it ends (the #1042 live check, 2026-11-23: 22 s). Withdrawn is `expired` by `interrupted`: the page says "settled in the
// terminal". Returns the unwatch.
type ChatAsk = { tool: string; agent: string; input: Record<string, unknown>; here: boolean; at: number }
function watchAsk($: any, id: string, ask: ChatAsk, signal?: AbortSignal): () => void {
  mem.chatAsks.set(id, ask)
  const stop = () => { void withdrawAsk($, id) }
  signal?.addEventListener?.('abort', stop, { once: true })
  return () => {
    mem.chatAsks.delete(id)
    signal?.removeEventListener?.('abort', stop)
  }
}

// One asked with the user at the terminal (`here`) and settled there counts as using the terminal,
// as termDialogDone: the push it sent, tapped, made the user away, and the answer here submitted
// no prompt, so later dialogs went to the phone, or "Let Claude go ahead", with them at the terminal.
async function withdrawAsk($: any, id: string) {
  if (!mem.asksOpen.has(id)) return // answered on the page, or expired already
  const ask = mem.chatAsks.get(id)
  mem.asksOpen.delete(id)
  mem.chatAsks.delete(id)
  await runWb($, ['chat', 'answer', id, 'expired', '--by', 'interrupted'], 15_000)
  if (ask?.here && await webAt($) >= ask.at) await back($)
}

// The engine's call `e` went on past its permission check: its dialog, still open on the page, was
// answered in the terminal. Its own dialog only, matched by its input too (sameCall): by tool and
// loop alone, a Read allowed by a rule finishing beside one asking withdrew that one's card, and a
// user away was left with the dialog in a terminal nobody watched (the #1042 review).
async function askSettledHere($: any, e: Row) {
  const agent = String(e.agentId ?? '')
  const id = [...mem.chatAsks].find(([k, a]) => a.tool === e.tool && a.agent === agent && mem.asksOpen.has(k)
    && sameCall(a.input, e))?.[0]
  if (id) await withdrawAsk($, id)
}

// Whether the tool.call input `e` is the call the dialog's `input` asked about: every argument both
// carry is the same, and they share one unless the call has none of its own (the engine adds some
// to the dialog's: ExitPlanMode's model input is empty, its dialog's carries the plan and its file)
const CALL_KEYS = new Set(['tool', 'tool_use_id', 'agentId', 'consent', 'requestMeta'])
export function sameCall(input: Record<string, unknown>, e: Record<string, unknown>): boolean {
  const keys = Object.keys(input).filter(k => k in e)
  return (keys.length > 0 || !Object.keys(e).some(k => !CALL_KEYS.has(k)))
    && keys.every(k => JSON.stringify(input[k]) === JSON.stringify(e[k]))
}

// A dialog while the user is at the terminal stays there, and the phone is told one waits (#959):
// typing in the terminal and then picking up the phone left a prompt there with the phone told
// nothing. A notice, not a card (only the terminal can answer it), pushed as Claude's messages
// are; wb skips it for a user who doesn't use the chat. Only a permission, as the owner asked, and
// only one still unanswered after NOTICE_AFTER_MS: written at once, every prompt approved at the
// terminal in two seconds buzzed the phone ten seconds later saying it still waited (the #959
// review). One open notice at a time: a burst of prompts buzzed once per prompt.
// Answering the dialog is using the terminal (termDialogDone): tapping the notice opened the page,
// which made the user away, and the answer at the terminal submitted no prompt, so the next
// prompt went to the phone, or was allowed by "Let Claude go ahead", with the user at the
// terminal (the #959 review).
const NOTICE_AFTER_MS = 30_000
type TermDialog = { tool: string; agent: string; at: number; kind: string; text: string; id: string; sending: boolean; done: boolean }

// The poll's tick: each permission dialog still up past NOTICE_AFTER_MS gets its notice, unless
// another notice of this session's is still open
async function termNotices($: any, now: number) {
  if (mem.termDialogs.some(d => d.sending || (d.id && !d.done))) return
  const d = mem.termDialogs.find(d => d.kind === 'permission' && !d.done && !d.id && now - d.at >= NOTICE_AFTER_MS)
  if (!d) return
  d.sending = true
  try {
    const { ok, out } = await runWb($, ['chat', 'notice', `--tool=${d.tool}`, '--kind=permission', `--text=${d.text}`], 15_000)
    const id = (out.split('\n').pop() || '').trim()
    if (!ok) warnOnce($, 'chat-notice', `the web chat wasn't told of a dialog here (${brief(out)})`)
    d.id = ok && /^\d+-[0-9a-f]+$/.test(id) ? id : 'none' // 'none': wb skipped it, or failed: not again
    if (d.done) await noticeDone($, d)
  } finally {
    d.sending = false
  }
}

// The notice's dialog was answered here: no push owed for it, and the page says so
async function noticeDone($: any, d: TermDialog) {
  if (!d.id || d.id === 'none') return
  const { ok, out } = await runWb($, ['chat', 'notice', '--done', d.id], 15_000)
  if (!ok) warnOnce($, 'chat-notice-done', `the web chat wasn't told a dialog here was answered (${brief(out)})`)
}

// A dialog that came up at the terminal is over (its call went on, or the turn ended): the user
// answered it there, so the phone used while it was up (the notice tapped) no longer counts as
// the device last used, and its notice is settled
async function termDialogDone($: any, d: TermDialog) {
  d.done = true
  mem.termDialogs = mem.termDialogs.filter(x => x !== d)
  if (await webAt($) >= d.at) await back($)
  if (!d.sending) void noticeDone($, d)
}

async function waitOnChat($: any, id: string, kind: string, i: Record<string, unknown>, signal?: AbortSignal, o: AskFor = {}) {
  const what = kind === 'plan' ? 'the plan' : kind === 'question' ? "Claude's question" : 'a permission prompt'
  $.ui.toast(o.here && o.for?.length ? `workbench: ${what} for agent ${o.for.join(', ')} is in the web chat too: answer it here or there`
    : `workbench: ${what} is waiting for you in the web chat`)
  const ask = o.for?.length ? 'wb chat reply --to user' : 'wb chat reply' // in an agent's turn, the user's thread (#1042)
  const deadline = (await $.clock.now()) + ASK_WAIT_MS
  while (!signal?.aborted) {
    const left = deadline - (await $.clock.now())
    if (left <= 0) break
    const secs = Math.max(1, Math.min(ASK_CHUNK_S, Math.ceil(left / 1000)))
    const { ok, out } = await runWb($, ['chat', 'wait', id, '--timeout', String(secs), '--json'], (secs + 15) * 1000)
    let a: any = null
    try { a = ok ? JSON.parse(out) : null } catch {}
    if (!a) break
    if (a.answer === 'allow') {
      if (kind === 'question') return { behavior: 'allow' as const, updatedInput: { ...i, answers: a.answers } }
      return { behavior: 'allow' as const }
    }
    if (a.answer === 'deny') {
      const note = a.note ? ` They said: "${a.note}"` : ''
      return { behavior: 'deny' as const, message: kind === 'plan'
        ? `The user wants changes to the plan before you go ahead (from the web chat).${note} Revise the plan, then present it again.`
        : kind === 'question'
          ? `The user didn't pick an answer in the web chat.${note} Ask them there (${ask}) instead.`
          : `The user said no in the web chat.${note} Ask them there (${ask}) what to do instead.` }
    }
    if (a.answer) break  // expired elsewhere
  }
  await runWb($, ['chat', 'answer', id, 'expired', '--by', signal?.aborted ? 'interrupted' : 'timeout'], 15_000)
  return undefined
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
  if (isOwedNote(text) && !owedNudged(text)) return // answered, or the user came back, since it was queued (#837)
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

// The web chat's switch, as the user left it (chat/switch.json, written by `wb chat on|off`): turned
// off, the link goes and the away state with it, so no prompt is sent where nobody reads; turned
// on, the link is looked up at once.
async function readSwitch($: any) {
  const web = !!(await readJson($, `${mem.root}/chat/switch.json`)).on
  if (web === (await read($, chat)).web) return
  if (web) mem.chatUrlAt = 0
  await update($, chat, c => ({ ...c, web, ...(web ? {} : { url: '' }) }))
  if (!web) await back($)
}

// The web chat's link while it is on, looked up again every ten minutes (it changes only with the token)
async function chatLink($: any) {
  const now = await $.clock.now()
  if (!(await read($, chat)).web || now - mem.chatUrlAt <= 10 * 60_000) return
  mem.chatUrlAt = now
  const { ok, out, aborted } = await runWb($, ['chat', 'url'])
  if (aborted) { mem.chatUrlAt = 0; return } // the pinned link stays and the next poll reads (#1142)
  const link = ok && /^https?:\/\/\S+#token=\S+$/.test(out) ? out : ''
  if (link !== (await read($, chat)).url) await update($, chat, c => ({ ...c, url: link }))
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

async function listen($: any, now: number) {
  const st = await read($, chat)
  if (!st.delegating || !st.web) return
  const dir = `${mem.root}/chat`
  if (!mem.eventsPid) return // the chat knows a listener by its live `wb events` process
  const claim = await readJson($, `${dir}/listener.json`)
  const theirs = claim.session && claim.session !== st.key
  // whether it lives unknown, its claim stands while the file is fresh: read as gone, two
  // sessions took the listener from each other on every poll, each write the other's event (#700).
  // Asked only of another session's claim, and a live one at most every PID_CHECK_MS: on macOS
  // (no /proc) each ask is a `kill` and a `ps`, run on every poll, 80 a minute, even for this
  // session's own claim, whose answer went unused (#839).
  let lives: boolean | null = false
  if (theirs && claim.pid && elsewhere(claim.machine)) lives = true // can't be asked here (#922)
  else if (theirs && claim.pid) {
    const key = `${Number(claim.pid)}@${claim.start ?? ''}`
    if (now - (mem.listenerAt.get(key) ?? -Infinity) < PID_CHECK_MS) lives = true
    else {
      lives = await alive($, Number(claim.pid), claim.start ? String(claim.start) : '')
      mem.listenerAt.clear() // one claim at a time
      if (lives === true) mem.listenerAt.set(key, now)
    }
  }
  const fresh = async () => now - ((await $.fs.stat(`${dir}/listener.json`).catch(() => undefined))?.mtimeMs ?? 0) < CHAT_STALE_MS
  // another session listens: this one stands by, but for the user's answers to its own rows (#1096)
  const standby = !!theirs && (claim.pid ? lives === true || (lives === null && await fresh())
    : now - Number(claim.ts ?? 0) * 1000 < CHAT_STALE_MS)
  if (!standby) {
    if (!(await $.fs.exists(dir))) await runWb($, ['chat', 'log']) // wb makes the directory
    // parent: this Claude Code process, so `wb chat reply` threads onto this session's messages only
    // when it runs under it (chat.caller_listens): another session's report went to the agent this
    // one was answering, pushed to no phone (#1129, 2026-11-23)
    const body = JSON.stringify({ session: st.key, cwd: mem.cwd, pid: mem.eventsPid,
      ...(mem.eventsStart ? { start: mem.eventsStart } : {}), ...(mem.machine ? { machine: mem.machine } : {}),
      ...(mem.claudePid ? { parent: mem.claudePid } : {}), busy: mem.busy })
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
    const text = chatPrompt(m, elsewhereNote + about)
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
      mem.chatOwed.set(id, { at: now, grace: r.isDelivered ? 0 : 1, nudged: false, agent: agentOf(m) }) // queued: maybe the next turn's
    } else {
      // behind a prompt submitted this poll (a wake, a queued turn, another web message) whose
      // turn hasn't started yet, it enters only the turn after: that turn's end, Claude not yet
      // having seen it, sent the follow-up for a message Claude then answered (#837). An agent's
      // is `now` only with nothing ahead: behind a wake, the wake's report went to it (#1212).
      mem.chatOwed.set(id, { at: now, grace: mem.submitted ? 1 : 0, nudged: false, agent: agentOf(m) })
      void sendWeb($, id, text, agentOf(m) && mem.submitted ? 'into' : 'now', st.key)
    }
    mem.chatTurn.set(id, mem.chatOwed.get(id)?.grace ?? 0) // its turn's end goes to turns.jsonl (#1042)
    if (agentOf(m)) { mem.agentTurn.set(id, agentOf(m)); $.ui.toast(`workbench: a message from agent ${agentOf(m)}`); continue } // never the user away
    // no toast for the user's own message: they just sent it (#1322)
    await $.fs.write(awayFile(), JSON.stringify({ at: now / 1000, by: st.key })).catch(() => {})
  }
}

// The user's messages without a receipt: wb's index of open work (chat/open.json), whatever the
// files' size. The tails it replaces lost a message behind 4 MiB of replies (#329, the audit of
// 1.4); they remain for a wb from before the index.
async function unread($: any, dir: string): Promise<Row[]> {
  const work = await readJson($, `${dir}/open.json`)
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
// (`grace`: a message queued behind a running turn may only enter the next), no reply yet means
// one follow-up turn naming `wb chat reply`; that one ending without a reply too, a plain note
// goes to the chat, so the phone always hears something. A reply is a `wb chat reply` (or
// `listen`, or `wb shot --chat`) in Bash, or a row of Claude's in messages.jsonl since; a prompt
// typed in the terminal ends it (the user is back).
// An agent's message (`agent`, its name) is owed its own reply, `--to` it (#1273, 2026-11-24): the
// follow-up said the user wrote and named a plain `wb chat reply`, which went to the user's phone,
// and the agent's wait heard nothing; any reply to the user settled the agent's debt too, so not
// even OWED_POSTED reached it. Only a reply to it settles it: a `--to` naming it, or a row whose
// `re` holds its id or whose `to` its name.
type Owed = { at: number; grace: number; nudged: boolean; agent: string }
const OWED_NOTE =
  '[workbench] The user wrote from the web chat and hasn\'t heard back: answer there with ' +
  '`wb chat reply --text "…"` (the web thread is all they see), briefly. Nothing else to do.'
const AGENT_OWED = '[workbench] Agent '
const AGENT_OWED_NOTE = (agent: string, id: string) =>
  `${AGENT_OWED}${agent}'s message ${id} has no answer: reply with \`wb chat reply --to ${id} --text "…"\` ` +
  '(a plain reply goes to the user, not the agent), briefly. Nothing else to do.'
const owedNote = (id: string, o: Owed) => o.agent ? AGENT_OWED_NOTE(o.agent, id) : OWED_NOTE
const isOwedNote = (text: string) => text === OWED_NOTE || text.startsWith(AGENT_OWED)
const OWED_POSTED = 'Claude answered in the terminal; nothing was sent here.'
const REPLIES = /\bwb\b[^\n|;&]*\bchat\s+(reply|listen)\b|\bwb\b[^\n|;&]*\bshot\b[^\n]*--chat\b/
// The follow-up is owed while a message it was queued for is still unanswered: a reply or the
// user's return before it went left it in mem.queued, and it went anyway (#837)
const owedNudged = (text: string) => [...mem.chatOwed].some(([id, o]) => o.nudged && owedNote(id, o) === text)
function owedSettled() {
  for (let i = mem.queued.length - 1; i >= 0; i--)
    if (isOwedNote(mem.queued[i]) && !owedNudged(mem.queued[i])) mem.queued.splice(i, 1)
}
// The user's debts only: a plain reply, or the user back at the terminal, answers the user, never an agent (#1273)
function userOwedClear() {
  for (const [id, o] of [...mem.chatOwed]) if (!o.agent) mem.chatOwed.delete(id)
  owedSettled()
}
async function owedAtTurnEnd($: any) {
  if (!mem.chatOwed.size) return owedSettled()
  const tail = await readTail($, `${mem.root}/chat/messages.jsonl`, NOTIFY_TAIL)
  // a reply settles it when it answers the message (`re`) or this Claude Code wrote it to the
  // message's sender (`claude_pid`, unknown: yes; `to`, none: the user's): another session's
  // report, posted meanwhile, read as the answer (#1129); a reply to the user, as an agent's (#1273)
  const mine = (m: any) => !mem.claudePid || !Number(m.claude_pid ?? 0) || Number(m.claude_pid) === mem.claudePid
  const toWhom = (m: any, o: Owed) => (Array.isArray(m.to) && m.to.length ? m.to : ['user']).includes(o.agent || 'user')
  const replied = parseLines(tail?.text ?? '').filter(m => m.from === 'claude')
  const nudge: string[] = []
  for (const [id, o] of [...mem.chatOwed]) {
    if (replied.some(m => Number(m.ts ?? 0) * 1000 >= o.at && ((mine(m) && toWhom(m, o)) || (Array.isArray(m.re) && m.re.includes(id))))) {
      mem.chatOwed.delete(id)
      continue
    }
    if (o.grace > 0) { o.grace--; continue }
    if (!o.nudged) { o.nudged = true; nudge.push(owedNote(id, o)); continue }
    mem.chatOwed.delete(id)
    await runWb($, ['chat', 'reply', `--text=${OWED_POSTED}`, `--to=${id}`], 15_000)
  }
  for (const note of nudge) if (!mem.queued.includes(note)) mem.queued.push(note)
  owedSettled()
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
  mem.chatTurnSize = (async () => (await read($, chat)).web
    ? (await $.fs.stat(`${mem.root}/chat/messages.jsonl`).catch(() => undefined))?.size ?? 0 : -1)().catch(() => -1)
}
async function chatTurnEnd($: any) {
  const ids: string[] = []
  for (const [id, grace] of [...mem.chatTurn]) {
    if (grace > 0) mem.chatTurn.set(id, grace - 1)
    else { mem.chatTurn.delete(id); ids.push(id) }
  }
  for (const id of [...mem.agentTurn.keys()]) if (!mem.chatTurn.has(id)) mem.agentTurn.delete(id) // their turn is over
  for (const id of [...mem.chatAsks.keys()]) if (!mem.asksOpen.has(id)) mem.chatAsks.delete(id) // a hook dropped outright
  const start = mem.chatTurnSize
  mem.chatTurnSize = Promise.resolve(-1)
  const was = await start
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
    file = { key: got.key && `${path}:${got.key}`, rows: parseLines(got.text) }
    if (file.key) mem.notifyRows = file
  }
  if (file.key === mem.lastNotify && !mem.busy) return
  // Per task, its newest notice and every question: two asked at once (parallel tool calls) are
  // both open, and the newest alone hid the first, its agent blocked (the 2026-11-16 audit).
  const due = new Map<string, Row[]>()
  for (const row of file.rows) {
    // Every notice counts for rewake, however old: a notice delivered before a reload (or by
    // another session) is news Claude had, not a lost one (the 2026-11-16 audit).
    // a batch's notice is news for each task it names (acked with it), or rewake woke for the rest
    for (const n of [row.task, ...(batchOf(row) ?? [])]) mem.noticed.set(n, Math.max(mem.noticed.get(n) ?? 0, row.ts))
    if (row.ts * 1000 < Math.max(mem.startedAt, now) - OLDEST_WAKE_MS) continue
    due.set(row.task, [...(due.get(row.task) ?? []).filter(r => r.kind === 'question'), row])
  }
  // A wake held for the turn leaves the file unseen, so the poll after the turn sends it: a second
  // task's wake held while the first one's turn ran was never sent (the 2026-11-16 audit).
  let held = false
  for (const [name, rows] of due) {
    const ackPath = `${root}/tasks/${name}/acked`
    let acked = Number(await $.fs.read(ackPath).catch(() => '0')) || 0
    if (rows.every(r => r.ts <= acked)) continue
    let spec = await readJson($, `${root}/tasks/${name}/task.json`)
    await noteClaude($, spec, now)
    spec = await claim($, name, spec)
    const own = mine(spec)
    // Only wake for news still true: a review notice for a task since discarded, accepted or
    // sent back is acknowledged and dropped.
    const status = await readJson($, `${root}/tasks/${name}/status.json`)
    for (const row of rows) {
      if (row.ts <= acked) continue
      const current = row.kind === 'checkin'
        ? RUNNING.has(status.phase) // a check-in is news while the task still runs
        : row.kind === 'question' && row.id
          // this question, still unanswered, while the task still asks: a question the runner
          // dropped (timed out, its round ended or stopped) woke Claude for nobody (the 0.90 review)
          ? status.phase === 'asking' && await asking($, name, String(row.id))
          : status.phase === (row.kind === 'question' ? 'asking' : row.kind)
      const batch = batchOf(row)
      const headline = batch ? (batch.length > 1 ? `and ${batch.length - 1} more were interrupted`
        : 'was interrupted') : row.cause === 'provider' ? PROVIDER_HEADLINE
        : row.cause === 'local_only' ? LOCAL_ONLY_HEADLINE
        : (outsideTheStep(row) ? OUTSIDE_HEADLINE[row.kind] : undefined) ?? HEADLINE[row.kind] ?? row.kind
      const key = `${name}:${row.ts}`
      if (current && !mem.toasted.has(key)) {
        mem.toasted.add(key)
        $.ui.toast(`workbench: ${name} ${headline}`)
      }
      if (!own || !mem.wake) continue // the toast only: the notice stays for its session or wb wait
      // a batch's wake ends with its resume, not the failure's next step (#544)
      const next = batch ? BATCH_NEXT(batch) : row.cause === 'spend' ? (row.guard === 'agents' ? AGENTS_NEXT(name) : SPEND_NEXT(name, (await readRoutes($)).cap !== null))
        : row.cause === 'local_only' ? OUTSIDE_REMEDY
        : row.cause === 'provider' || (outsideTheStep(row) && OUTSIDE_HEADLINE[row.kind]) ? OUTSIDE_NEXT(name)
        : (NEXT[row.kind] ?? NEXT.failed)(name, row.id ? String(row.id) : undefined, String(row.text ?? ''))
      const who = batch && batch.length > 1 ? `Tasks ${batch.map(n => `\`${n}\``).join(', ')} were interrupted`
        : `Task \`${name}\` ${headline}`
      const text = `[workbench] ${who}.\n\n${row.text}\n\n${next}`
      if (mem.busy && current) {
        // A question can't wait for the turn to end: its agent is blocked, and after the ask
        // timeout it guesses (a 20-minute review turn, #542). It goes into the running turn, as
        // a web message does; refused, it is held like the rest, which deliver once the turn ends.
        if (row.kind === 'question') {
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
      if (current && !(await take($, name, row.ts))) { acked = row.ts; continue } // taken by another
      await raiseAck($, ackPath, row.ts) // acknowledge before waking: never wake twice
      acked = row.ts
      if (!current) {
        // held through the turn and timed out unseen: said, not acknowledged in silence (#542)
        if (mem.heldAsks.delete(key) && row.id && (ACTIVE.has(status.phase) || NEEDS.has(status.phase))
          && (await $.fs.read(`${root}/tasks/${name}/ask/${row.id}.answer`).catch(() => '')).startsWith(NO_ANSWER)) {
          void wake($, name, `[workbench] Task \`${name}\` asked a question while you were busy.\n\n${row.text}\n\n` +
            MISSED_NOTE(name), prev, row.ts)
        }
        continue
      }
      mem.heldAsks.delete(key)
      void wake($, name, text, prev, row.ts)
    }
  }
  if (!held) mem.lastNotify = file.key
}

// A notice taken for this session's wake, under wb's lock (`wb ack`, a compare-and-set shared with
// `wb wait`): two sessions woken by one `wb events` line each read `acked`, then wrote it, and
// both woke Claude for one review (#687). False only when wb says another took it first; a wb
// that can't run leaves the plain write after it to decide, as before.
async function take($: any, name: string, ts: number): Promise<boolean> {
  try {
    return (await $.process.run([mem.wb, 'ack', name, String(ts)], { timeoutMs: 15_000 })).exitCode !== 3
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
async function release($: any, name: string, prev: number, ts: number) {
  try {
    const r = await $.process.run([mem.wb, 'ack', name, String(prev), '--if', String(ts)], { timeoutMs: 15_000 })
    if (r.exitCode === 0 || r.exitCode === 3) return
  } catch {}
  const ackPath = `${mem.root}/tasks/${name}/acked`
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
async function wake($: any, name: string, text: string, prev: number, ts: number) {
  mem.submitted = true
  const r = await $.prompt.submit({ text: await awayToo($, text) }).catch((err: unknown) => ({ drop: String(err) }))
  if (!r?.drop) return
  mem.submitted = false
  mem.dropped.set(name, await $.clock.now())
  await release($, name, prev, ts)
  warnOnce($, `drop:${name}`, `the wake for ${name} was dropped (${r.drop}); trying again in a minute`)
}

// The safety net under deliver: a task of this session's waiting on Claude whose news never
// reached it (a lost or swallowed notice; on 2026-11-16 a failure sat unseen for an hour) wakes
// Claude anyway, once, after a minute of Claude being idle. `acked` past the task's newest notice
// (or, with none, the moment it entered that state) means Claude has had the news.
const REWAKE_MS = 60_000
const REWAKE_MAX_MS = 12 * 3600_000 // older than this, it waited for a session long gone: wb status shows it
async function rewake($: any, now: number, list: WbTask[]) {
  if (mem.busy || !mem.wake) return
  for (const t of list) {
    if (!waitsOnClaude(t) || !mine(t) || now - t.updatedAt < REWAKE_MS || now - t.updatedAt > REWAKE_MAX_MS) continue
    if (now - (mem.dropped.get(t.name) ?? 0) < REWAKE_MS) continue // dropped: once a minute
    const ackPath = `${mem.root}/tasks/${t.name}/acked`
    const acked = Number(await $.fs.read(ackPath).catch(() => '0')) || 0
    // Claude had the news if it acked the task's latest notice: the runner's last status write
    // (its pid cleared) lands just after that notice, so updated_at alone would wake it twice.
    // The task's own `noticed` mark too (Task.notify): after a reload mem.noticed holds only
    // notify.jsonl's last 1 MiB, and a review whose notice had left it woke Claude again (#844)
    const marked = Number(await $.fs.read(`${mem.root}/tasks/${t.name}/noticed`).catch(() => '')) || 0
    const seen = mem.noticed.get(t.name)
    const noticed = seen !== undefined || marked ? Math.max(seen ?? 0, marked) : undefined
    if (noticed !== undefined ? acked >= noticed : acked * 1000 >= t.updatedAt) continue
    await $.fs.write(ackPath, String(now / 1000))
    const kind = t.phase === 'asking' ? 'question' : t.phase
    void wake($, t.name, `[workbench] Task \`${t.name}\` ${HEADLINE[kind] ?? kind}, ${ago(now - t.updatedAt)} ago, ` +
      `and no notice reached Claude. \`wb report ${t.name}\` shows where it is.\n\n` +
      (NEXT[kind] ?? NEXT.failed)(t.name), acked, now / 1000)
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
    if (!mem.eventsPid || mem.hasActive || mem.queued.length || now - mem.lastPoll > SAFETY_MS) void poll($)
  })
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
// unless the Settings row turned it off (`wb splash off`, splash.json); a reload, /workbench
// while the mode is on, a bring-back from the strip or a pane a task opened never do (the
// `splashed` atom, which workbenchMode clears when the mode goes off). While it plays the pane's Tasks tab is one Raster the size of
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
const SPLASH_STOPS: [string, string, string][] = [
  ['on', 'Enabled (default)', 'The first /workbench of a session opens with a short animation; any key skips it.'],
  ['off', 'Off', 'The Tasks page opens at once.'],
]

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
// The modeline's ticker (#976, modeline.ts): a step a TICKER_MS, by blit alone, while the line is
// wider than its room; stopped when it fits, goes, or the pane can't take a blit (closed: the next
// draw starts it again, from where it was if the line is the same).
const TICKER = 'ticker' // its Raster's key
function syncTicker($: any, line: string, room: number): number {
  const t = mem.ticker
  if (t && t.line === line && t.room === room) return t.off
  const off = t && t.line === line ? t.off % loopLength(line) : 0
  stopTicker()
  if (!line) return 0
  const k: NonNullable<typeof mem.ticker> = { line, room, off, timer: { cancel: () => {} } }
  mem.ticker = k
  k.timer = $.clock.every(TICKER_MS, async () => {
    if (mem.ticker !== k) return
    k.off = (k.off + 1) % loopLength(k.line)
    const r = await $.ui.blit({ requestId: PANE, key: TICKER, cells: tickerCells(k.line, k.off, k.room) })
      .catch(() => ({ deny: 'gone' }))
    if (r?.deny && mem.ticker === k) stopTicker(true)
  })
  return off
}
function stopTicker(keepLine = false) {
  const t = mem.ticker
  if (!t) return
  try { t.timer.cancel() } catch { /* already stopped */ }
  mem.ticker = keepLine ? { ...t, room: -1, timer: { cancel: () => {} } } : null // -1: none runs
}

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
    const claim = await readJson($, path)
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
// Kept MODES_KEEP_MS; a quit clears every id this process has had.
const MODES_KEEP_MS = 30 * 24 * 3600_000
async function keepMode($: any, on: boolean) {
  try {
    const now = await $.clock.now()
    const was = (await $.store.get('modes')) as Record<string, number> | undefined
    const modes: Record<string, number> = {}
    for (const [id, at] of Object.entries(was && typeof was === 'object' ? was : {})) {
      if (typeof at === 'number' && now - at < MODES_KEEP_MS && !mem.ids.has(id)) modes[id] = at
    }
    if (on) for (const id of mem.ids) modes[id] = now
    await $.store.set('modes', modes)
  } catch {} // no store (an older Claude Code, a test without one): the mode lasts this process
}
async function modeKept($: any): Promise<boolean> {
  try {
    const modes = (await $.store.get('modes')) as Record<string, number> | undefined
    return !!modes && typeof modes === 'object' && [...mem.ids].some(id => typeof modes[id] === 'number')
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
  void paneBack($, opening)
  return opening
}

// Once an open lands, the Stats tab's history and live view catch up: /workbench again and a
// bring-back from the strip waited for the next poll, up to 30 s with no task (#552), and a
// syncTop before the open resolved could still see the pane hidden
async function paneBack($: any, opening: Promise<unknown>) {
  await Promise.resolve(opening).catch(() => {})
  void refreshStats($)
  void syncTop($)
}

// Brings the pane back from the strip, on the tab asked for. Opened first, while the press is still
// the person's: backseat-driver's opened after its awaits counted as its own, and an open the mod
// makes after a close by hand waits for a wide terminal (2026-10-07).
async function restorePane($: any, tab: string) {
  const opening = $.ui.open({ id: PANE, title: 'Workbench' })
  mem.paneOpened = true
  await update($, pane, p => ({ ...p, tab: tab || p.tab, minimized: false }))
  void paneBack($, opening)
  $.ui.invalidate('ui.render')
  return opening
}

// What the status line and the strip say of the tasks: the running ones, those asking, how many wait.
// Outside workbench mode only this session's: another session's task running opened the pane and
// filled the status line in a session the user kept off workbench, and /workbench-quit couldn't
// clear it (2026-11-17)
function taskParts(list: WbTask[], on: boolean) {
  const shown = on ? list : list.filter(t => mine(t))
  const live = shown.filter(t => ACTIVE.has(t.phase) && t.phase !== 'asking')
  // what waits on Claude is this session's alone, in the mode too: another session's reviews
  // counted in this one's "N awaiting review", as if this Claude had them to do (#693)
  const asking = shown.filter(t => t.phase === 'asking' && mine(t))
  const waiting = shown.filter(t => waitsOnClaude(t) && t.phase !== 'asking' && mine(t))
  // a stuck or failed task is counted as such: "2 awaiting review" said so for one review and one
  // stuck task (seen live, #1048)
  const count = (phase: string, word: string) => {
    const n = waiting.filter(t => t.phase === phase).length
    return n ? [`${n} ${word}`] : []
  }
  return { live, parts: [
    ...live.map(t => `${t.name} ${t.phase} r${t.round}`),
    ...asking.map(t => `${t.name} asks`),
    ...count('review', 'awaiting review'), ...count('stuck', 'stuck'), ...count('failed', 'failed'),
  ] }
}

// A path variable as wb reads it (store.absolute_env: os.path.abspath(os.path.expanduser(v))):
// `~` expanded, relative to the directory wb runs in (the session's, where $.process.run starts
// it), `.`, `..` and repeated or trailing slashes gone. settings.json's `env` passes WB_HOME
// literally, and the mod took `~/state/wb` as given: the pane stayed empty and no wake came; a
// trailing slash made `…/workbench//tasks/`, and the Claude-coding guard never matched (#454).
export function absolutePath(v: string, home: string, cwd: string): string {
  let p = v === '~' ? home : v.startsWith('~/') ? `${home}${v.slice(1)}` : v
  if (!p.startsWith('/')) p = `${cwd}/${p}`
  const out: string[] = []
  for (const part of p.split('/')) {
    if (part === '..') out.pop()
    else if (part && part !== '.') out.push(part)
  }
  return `/${out.join('/')}`
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
      name: HAND_BACK,
      description: 'Stop this session\'s agents and hand their tasks back to Claude to finish',
    })
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
    }
    return next(e)
  })

  on('session.attach', async ($, e, next) => {
    if (e.surface) mem.surfaces.add(e.surface)
    if (!mem.person && mem.root) {
      mem.person = true
      startEars($)
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
    // Claude coding (#92): Claude's own edit in a task's worktree waits for the user's choice
    const field = EDIT_TOOLS[e.tool]
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
    // dialog of that tool in that loop (another loop's call of the tool settles none of its)
    let denied = false
    try {
      const r = await next(e)
      denied = !!(r as Row)?.deny
      return r
    } finally {
      if (kept.tasks.length) await noteCall($, kept.tasks, kept.row, at, name, denied ? 'denied' : 'over').catch(() => {})
      const agent = e.agentId ?? ''
      const d = mem.termDialogs.find(d => d.tool === e.tool && d.agent === agent)
      if (d) await termDialogDone($, d)
      await askSettledHere($, e as Row) // its card on the page too (#1042)
    }
  })

  // A dialog while the user is away on the web chat goes there (askOnChat): the moment it would
  // come up, in the session the chat listens to. A settings hook's own answer stands, and a
  // failed hook leaves the dialog to come up as usual. At the terminal, the dialog stays there and
  // is noted (mem.termDialogs, #959): a permission still up after NOTICE_AFTER_MS gets one notice
  // on the phone (termNotices), and its answer here counts as terminal input (termDialogDone).
  on('classic.PermissionRequest', async ($, e, next) => {
    const r = await next(e)
    if (r.decision) return r
    const st = await read($, chat)
    if (!st.delegating || !st.web) return r
    const isAway = await away($, st), forAgents = agentsFor(mem.chatTurn, mem.agentTurn) // an agent's turn: the page too (#1042)
    if (!isAway && !forAgents.length) {
      const kind = KIND[e.tool_name] ?? 'permission'
      mem.termDialogs.push({ tool: e.tool_name, agent: (e as any).agent_id ?? '', at: await $.clock.now(), kind,
        text: kind === 'permission' ? askText(e.tool_input) : '', id: '', sending: false, done: false })
      return r
    }
    const decision = await askOnChat($, e.tool_name, e.tool_input, next.signal, { for: forAgents, here: !isAway, agent: (e as any).agent_id ?? '' })
    return decision ? { ...r, decision } : r
  })

  // Delegating with wb counts as workbench mode too (a session resumed mid-delegation, or one
  // where /workbench ran before this mod could see it)
  on('tool.call', { tool: 'Bash' }, async ($, e, next) => {
    if (delegates(String(e.command ?? ''))) {
      await workbenchMode($, true)
    }
    // answered in the chat (#767): a plain reply the user, `--to` the messages it names (#1273)
    if (REPLIES.test(String(e.command ?? ''))) {
      const to = [...String(e.command).matchAll(/--to[= ]+['"]?([^\s'"]+)/g)].map(x => x[1])
      if (!to.length || to.includes('user')) userOwedClear()
      for (const id of to) mem.chatOwed.delete(id)
      owedSettled()
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
    if (byUser(e) && !e.text.startsWith(WEB) && !e.text.startsWith(AGENT) && !e.text.startsWith('[workbench]')) {
      await back($)
      userOwedClear() // back at the terminal: an answer there reaches them (#767), never an agent (#1273)
    }
    return next(e)
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
    if (st.delegating) blocks.push({ name: 'workbench-mode', text: MODE_NOTE(mem.skill) })
    const want = await wanted($, st.delegating)
    // /workbench as the first message said them in its text already (#540)
    const had = mem.skillTurn ? mem.said : null
    mem.skillTurn = false
    await setSaid($, want)
    if (want.coding !== 'decide' && had?.coding !== want.coding) blocks.push({ name: 'claude-coding', text: CODING_RULES[want.coding] })
    if (want.notes && !had?.notes) blocks.push({ name: 'user-notes', text: NOTES_NOTE(want.notes) })
    if (want.workflows === 'on' && had?.workflows !== 'on') blocks.push({ name: 'dynamic-workflows', text: WORKFLOWS_RULE })
    if (isAway) {
      blocks.push({ name: 'web-chat', text: AWAY_NOTE })
      mem.awayNoted = true
    }
    if (want.wakeOff && !had?.wakeOff) blocks.push({ name: 'workbench-wake', text: WAKE_OFF_NOTE })
    if (want.routes && had?.routes !== want.routes) { // read while delegating or with a task open, as `wanted` does
      const routes = await readRoutes($) // once any role has an API model (#1014)
      if (routes.api) blocks.push({ name: 'workbench-routes', text: ROUTES_NOTE(routes) })
    }
    const { ok, out: status, aborted } = await runWb($, ['status', ...(await scope($))])
    if (!ok && !aborted) warnOnce($, 'status', `wb status failed: ${brief(status)}; wb doctor`)
    if (!ok || status.startsWith('no open tasks')) return { blocks }
    return {
      blocks: [...blocks, {
        name: 'workbench',
        text: `Tasks delegated to agents with wb (the /workbench skill) are open:\n${status}\n` +
          'Pick them up from here: `wb report <name>` for one, `wb watch <name>` to follow it.',
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
      // An interrupt drops a waiting PermissionRequest hook outright, so its own expiry never
      // runs: the card would wait on the page for ever (seen live, 2026-11-18)
      for (const id of mem.asksOpen) void runWb($, ['chat', 'answer', id, 'expired', '--by', 'interrupted'], 15_000)
      mem.asksOpen.clear()
      // the dialogs at the terminal are over too, an interrupt's included (#959): the turn's
      // subagents ended with it, and one left open here would hold every later notice back
      for (const d of [...mem.termDialogs]) await termDialogDone($, d)
      mem.busy = false
      mem.skillTurn = false
      if (mem.chatQueued.size) void readQueued($)
      void chatTurnEnd($)
      await owedAtTurnEnd($)
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
      stopTop($) // put away: the live view ends with it
      $.ui.invalidate('ui.render')
    }
    return next(e)
  })

  // Minimized: the strip, while there is something to bring back (workbench mode, or this session's
  // tasks): its name, what the tasks are doing, and its tabs, each a way back
  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (!(await read($, pane)).minimized || e.props?.hasSurvey) return next(e)
    const { delegating } = await read($, chat)
    const { parts } = taskParts(await read($, tasks), delegating)
    if (!delegating && !parts.length) return next(e)
    const { Box, Text, Button } = $.ui.resolve(e) as Row
    return (
      <Box flexDirection="row" columnGap={2}>
        <Box flexShrink={0}>
          <Button key="restore" label="▸ Workbench" plain onPress={() => { void restorePane($, '') }} />
        </Box>
        {TABS.map(([tab, label]) => (
          <Box flexShrink={0}>
            <Button key={`restore-${tab}`} label={label} plain dimColor onPress={() => { void restorePane($, tab) }} />
          </Box>
        ))}
        <Text dimColor wrap="truncate-end">{parts.length ? `${parts.join(' · ')} · ${STRIP_HINT}` : STRIP_HINT}</Text>
      </Box>
    )
  })

  // Where the ring is in the pane, so an open settings row shows that option's sentence; let through
  on('ui.focus', { requestId: PANE }, async ($, e, next) => {
    if (e.origin.kind === 'person') endSplash($) // Tab or a click skips the opening animation, and still moves (#963)
    const focus = e.element && SETTING_ROWS.test(e.element) ? e.element : ''
    if (focus !== (await read($, pane)).focus) await update($, pane, p => ({ ...p, focus }))
    return next(e)
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const kit = $.ui.resolve(e) as Row
    const { Box, Text, Raster, Button } = kit
    const list = await read($, tasks)
    const watched = await read($, live)
    const { url, web, delegating } = await read($, chat)
    const view = await read($, pane)
    if (mem.splash) { // the opening animation, in place of the Tasks page while it plays (#963)
      const props = (e.props ?? {}) as Row
      const [cols, rows] = [Number(props.bodyColumns ?? 0), Number(props.scroll?.bodyRows ?? 0)]
      const s = mem.splash
      const fits = (e as Row).surface === 'terminal' && view.tab === 'tasks' && props.placement !== 'widget'
      // where the page will put the apprentice and the modeline, as it stands now: the walk and the
      // bar go there. A row the page can't measure any more (a line that may wrap came) is no
      // row: an earlier exact one was measured without that line, so the page is taller now and
      // the bar would land above the real modeline (2026-11-21, review of #963). Before the words
      // leave, that makes them rise away; once they fall, the bar goes to the row the page
      // reaches at least.
      const shape = fits ? pageShape(list, watched, delegating, await $.clock.now(), cols) : null
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
      endSplash($, false) // too small, or not the Tasks tab on a terminal: the page at once
    }
    // the web chat's link while it is on, on the Settings tab only (the user, 2026-11-19: never under
    // the tasks), then the tabs: last, since the pane follows its end (a row at the top scrolled away
    // under a long transcript)
    const pinned = web && url ? (
      <Box marginTop={1}>
        <Text>
          <Text bold color="claude">web chat </Text>
          <Text dimColor>{url}</Text>
        </Text>
      </Box>
    ) : null
    // under the tabs, on every tab, a modeline as in Emacs or Vim: the pane's name and the
    // apprentice's face, in the theme's own colors so it reads on a dark or a light terminal (the
    // user, 2026-11-21)
    // what this session's agents are doing, after the name (#976): scrolling when wider than the bar
    const doing = activity(list.filter(t => (ACTIVE.has(t.phase) || NEEDS.has(t.phase)) && mine(t)))
    const room = Number(((e.props ?? {}) as Row).bodyColumns ?? 0) - 2 - `${FACE.rest} ${MODELINE} - `.length
    const scrolls = !!doing && !!Raster && (e as Row).surface === 'terminal' && room >= 8 && [...doing].length > room
    const offset = syncTicker($, scrolls ? doing : '', room)
    const tabs = (
      <Box flexDirection="column" marginTop={1}>
        <Box flexDirection="row" columnGap={2}>
          {TABS.map(([tab, label, hotkey]) => (
            <Button key={`tab-${tab}`} label={label} hotkey={hotkey} plain dimColor={view.tab !== tab}
              onPress={() => { void update($, pane, p => ({ ...p, tab, open: '', focus: '' })).then(() => { void refreshStats($, true); void syncTop($) }) }} />
          ))}
        </Box>
        <Box key="modeline" backgroundColor="claude" paddingX={1} flexDirection="row">
          <Text color="inverseText" bold>{`${FACE.rest} ${MODELINE}${doing ? ' - ' : ''}`}</Text>
          {scrolls
            ? <Raster key={TICKER} columns={room} rows={1} cells={tickerCells(doing, offset, room)} />
            : doing ? <Text color="inverseText">{doing}</Text> : null /* not bold: a scrolling line's Raster can't be */}
        </Box>
      </Box>
    )
    // the Settings tab: Shared (the Agents switch, the busy row and the roles first, #1023; Claude
    // coding, #93; Dynamic workflows, #1041; the day cap and wake; progress checks, the opening
    // animation), one row each at rest; the day cap is typed (an Input: wb checks it). Then GPU and
    // API (#1023; API left out with no provider and no API role but its one line). Last, apart
    // under Experimental, the probe experiment (the user, 2026-11-21); the web chat is Shared's
    // last row, and the headings say no caveat (#1322)
    if (view.tab === 'settings') {
      const probe = await probeStatus($)
      const probeAt = await probeMode($)
      const prog = await progressSettings($)
      mem.settingsCols = Number(((e.props ?? {}) as Row).bodyColumns ?? 0) // settingRow folds to it
      const roles = await roleRows(kit, $, view, await rolesNow($)) // Shared, GPU and API (#1023)
      return (
        <Box flexDirection="column">
          <Box flexDirection="column">
            {heading(kit, 'Shared')}
            {roles.top}
            {settingRow(kit, $, 'coding', 'Claude coding', CODING_STOPS, await codingMode($), view,
              v => runWb($, ['coding', v]))}
            {settingRow(kit, $, 'workflows', 'Dynamic workflows', WORKFLOWS_STOPS, await workflowsMode($), view,
              v => runWb($, ['workflows', v]))}
            {roles.cap}
            {infoLine(kit, 'wake', `Wake Claude: ${mem.wake ? 'on' : 'off'} · change it in /plugin → workbench → options`)}
            {settingRow(kit, $, 'progress', 'Progress checks', progressStops(prog.after), prog.on ? 'on' : 'off', view,
              v => runWb($, ['progress', v]))}
            {settingRow(kit, $, 'splash', 'Opening animation', SPLASH_STOPS, (await splashOn($)) ? 'on' : 'off', view,
              v => runWb($, ['splash', v]).then(({ ok, out }) => { if (!ok) $.ui.toast(`workbench: ${brief(out)}`) }))}
            {settingRow(kit, $, 'chat', 'Web chat', CHAT_STOPS, web ? 'on' : 'off', view, v => setWeb($, v))}
          </Box>
          <Box flexDirection="column" marginTop={1}>
            {heading(kit, 'GPU')}
            {roles.gpu}
          </Box>
          {roles.api.length ? (
            <Box flexDirection="column" marginTop={1}>
              {heading(kit, 'API')}
              {roles.api}
            </Box>
          ) : null}
          <Box flexDirection="column" marginTop={1}>
            {heading(kit, 'Experimental')}
            {settingRow(kit, $, 'probe', 'Agent code navigation', probeStops(probe, probeAt), probeAt, view,
              v => setProbe($, v))}
          </Box>
          {pinned}
          {tabs}
        </Box>
      )
    }
    // the Stats tab: the live view (#408, `wb top --json` while shown, with Live view on), then
    // the history (#406, `wb stats --json`)
    if (view.tab === 'stats') {
      // drawn with no child: the engine shows the pane again (nothing else says so), so the live
      // view starts now, not at the next poll, 30 s off with no task (#552)
      if (!mem.top && !mem.topKids.size) void syncTop($)
      const { feed } = await read($, statsFeed)
      const cols = Math.max(30, Number(((e.props ?? {}) as Row).bodyColumns ?? e.viewport?.columns ?? 60))
      const top = await read($, topNow)
      const names = namesNow(list, top)
      const now = (await statsSettings($)).live ? liveLines(top, mem.topFails >= 2 ? mem.topWhy : '', names) : []
      return (
        <Box flexDirection="column">
          {now.length ? (
            <Box flexDirection="column" marginBottom={1}>
              {heading(kit, 'Now')}
              {now.flatMap(parts => wrapParts(parts, cols)).map(parts => (
                <Text wrap="truncate-end">{parts.map(p => <Text color={p.color} dimColor={p.dim} bold={p.bold}>{p.text}</Text>)}</Text>
              ))}
            </Box>
          ) : null}
          {statsLines(feed, mem.statsWhy, names).map(({ label, text }) => (
            <Box flexDirection="column" marginBottom={label ? 0 : 1}>
              {label
                ? hangWords(`${label}: ${text}`, cols).map((l, i) => i === 0 && l.startsWith(label)
                  ? <Text><Text bold>{label}</Text><Text dimColor>{l.slice(label.length)}</Text></Text>
                  : <Text dimColor>{l}</Text>)
                : <Text dimColor>{text}</Text>}
            </Box>
          ))}
          {tabs}
        </Box>
      )
    }
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
        {tabs}
      </Box>
    )
  })
}
