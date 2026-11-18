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
// The weekly model check: once a week, in a quiet moment of a session the person is using,
// Claude is asked to look at this machine, what is installed and how it has done, and what is
// newest, and to recommend what to add or retire. /workbench-models asks for it now.
const SCOUT_EVERY_MS = 7 * 24 * 3600 * 1000
const SCOUT_RETRY_MS = 24 * 3600 * 1000 // raised but never finished: ask again a day later
const MODELS = 'workbench-models'
const SCOUT_NOTE =
  '[workbench] The weekly model check is due. As the driver, keep the local agents on the best ' +
  'models this machine can run:\n' +
  '1. `wb scout`: this machine\'s hardware, what is installed, and how each model has done ' +
  '(real tasks), and its watch list: models set aside and what each needs ' +
  'before it is tried again.\n' +
  '2. Look up what is newest: open-weight coding models with tool calling, their sizes and ' +
  'quantizations, which fit this GPU\'s VRAM (with room for the context) and this RAM, and ' +
  'recent releases of the model server this machine runs (whichever it is: `wb scout` lists ' +
  'them). Prefer primary sources (model cards, the server\'s own model library, ' +
  'release notes) and recent reports; name nothing you could not verify.\n' +
  '3. Recommend in a few lines: keep, add (the model and quantization, why it should beat what ' +
  'is installed, its size and how it fits), retire (what it replaces, the disk it frees). ' +
  'Aim for the best results first, then efficiency. For each watch-list item, say whether ' +
  'its conditions are met yet; `wb scout --unwatch N` once one is settled.\n' +
  '4. Ask before downloading or deleting. With a yes: on Ollama `wb model add <provider/model> ' +
  '--pull` in the background, on another server its own download then `wb model add`; ' +
  '`wb model sizes <id> …` if it should only take some task sizes, and ' +
  '`wb model retire <id> [--delete]` for what it replaces. Installed models that are not ' +
  'registered may belong to other tools: ask first.\n' +
  '5. The skill library: rewrite or retire (`wb skill retire`) what `wb scout` flags.\n' +
  '6. `wb scout --done --notes "<what you recommended or changed>"`.\n' +
  'Keep it short; if nothing is worth changing, say so in one line.'
// Workbench mode: /workbench turns it on for the rest of the session and opens the pane, its sign.
// The user then asks for things in plain words, and Claude hands the coding to local agents as
// the skill says. A fresh or compacted conversation is told the mode is still on and where the
// skill is (`workbench-mode` block). Two slash commands end it: /workbench-quit stops this session's
// agents at once (`wb stop --all --repo <cwd> --session <id>…`) and tells Claude so;
// /workbench-quit-and-hand-back-claude stops them and hands their tasks to Claude (`wb takeover
// --all`, the same scope), starting a turn for Claude to finish them. Either closes the pane.
// Under the task list the pane follows one task live: the conversation between Claude and the
// agent, every tool call and test, as `wb watch --json` prints it.
import { atom, read, update } from 'claude-code'
import type { Register } from 'claude-code'

import type { WbChat, WbLine, WbLive, WbTask } from '../types'
import { cells, COLUMNS, FACE, MOUTH, ROWS } from './apprentice'
import type { Pose } from './apprentice'

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
  shape: 'tasks-3',
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
const LIVE_EVERY_MS = 5000 // a running task's transcript is re-read at most this often, unless its status moved
// The engine draws a pane's first 100,000 characters of text and drops the rest, and the
// apprentice comes after the history: a long task's history pushed its words (and its mouth)
// past the cut, leaving an empty bubble (seen 2026-11-16, 119,728 characters). So the history
// drawn is its newest lines within this budget, leaving the rest of the pane room.
const HISTORY_CHARS = 60000

// The web chat (`wb chat serve`): the user's side door while away from the terminal. A session
// listens while workbench mode is on in it (`delegating`), from /workbench until a quit command;
// with several, the first to claim chat/listener.json does, and the others stand by. A message is
// submitted as a prompt (so the terminal shows it too) with a receipt the page shows at once.
const chat = atom({ plugin: 'workbench', key: 'chat' } as const,
  { key: '', delegating: false, webAt: 0, url: '' } as WbChat, { shape: 'chat-2' })
const CHAT_EXPIRE_MS = 6 * 3600_000 // a web message older than this isn't submitted to a new listener
const CHAT_STALE_MS = 15_000 // as wb's LISTENING_FOR: a listener silent this long has gone
const AWAY_MS = 6 * 3600 * 1000 // the user counts as away this long after a web message
const WEB = '[web chat]'
// Only for a delivery into a running turn, which the engine frames as another session's message:
// the rules for answering live in the /workbench skill (The web chat), loaded whenever this listens.
const WEB_NOTE =
  '(The user wrote this in the web chat; the workbench mod delivered it into your running turn. ' +
  'Answer as /workbench\'s "The web chat" says: `wb chat reply --text "…"`.)'

const ACTIVE = new Set(['queued', 'running', 'testing', 'asking'])
const NEEDS = new Set(['review', 'stuck', 'failed', 'asking'])
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
// Each phase's mark in the task list; a running task's is the spinner.
const MARK: Record<string, string> = {
  queued: '◌', testing: '◐', asking: '?', review: '◆', stuck: '▲', failed: '✗',
  accepted: '✓', discarded: '–',
}

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
    case 'review': return { pose: 'tada', say: `${n} is ready for review.`, more: 'Over to you, Claude.', color: 'success' }
    case 'asking': return { pose: 'ask', say: `I asked Claude about ${n}.`, more: 'Waiting for the answer…', color: 'warning' }
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

const ago = (ms: number) => {
  const s = Math.max(0, Math.round(ms / 1000))
  return s < 90 ? `${s}s` : s < 5400 ? `${Math.round(s / 60)}m` : `${Math.round(s / 3600)}h`
}

const clip = (text: string, n: number) => (text.length <= n ? text : text.slice(0, n - 1) + '…')

// Words broken into lines of at most n characters (a longer word is cut across lines). The
// bubble wraps with this, not with Text's own wrapping: in a row that shares its width, that
// laid out the rows and drew them empty (seen 2026-11-16).
export const wrapWords = (text: string, n: number): string[] => {
  const lines: string[] = []
  let line = ''
  for (let word of text.split(/\s+/).filter(Boolean)) {
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
const NEXT: Record<string, (n: string, id?: string) => string> = {
  review: n => `Review it as /workbench says, from the diff above (\`wb diff ${n}\` when it was too ` +
    `big to include), then \`wb accept ${n} -m "…"\` or \`wb feedback ${n} --message <file>\`.`,
  question: (n, id) => `Answer with \`wb answer ${n}${id ? ` --id ${id}` : ''} --text "…"\`; it is blocked until then. A ` +
    `PERMISSION request (a refused command or directory): the \`wb allow ${n}\` it names ` +
    `(\`--always\` to keep it) when it is reasonable, which answers it and restarts the agent ` +
    `with it; else answer with another way.`,
  stuck: n => `Steer it with \`wb feedback ${n} --message <file>\`, or \`wb discard ${n}\`.`,
  failed: n => `Look at \`wb log ${n}\`; then \`wb feedback ${n} --message <file>\` or ` +
    `\`wb discard ${n}\`.`,
  checkin: n => `As the driver: tell the user in two or three plain lines how it is going ` +
    `(progress, time, anything off), and log the same with \`wb note ${n} --text "…"\`. ` +
    `If it is heading the wrong way, \`wb steer ${n}\` (\`--now\` to interrupt). Keep it short.`,
}
const RUNNING = new Set(['queued', 'running', 'testing'])

// /workbench run again with the mode already on and no task: the pane is back, and Claude needn't
// reread the skill to say so.
const AGAIN_NOTE = (skill: string) =>
  '[workbench] The user ran /workbench again: workbench mode is still on, and the Workbench ' +
  `pane is open again. Carry on as the skill says (read ${skill} if it is not in your context); ` +
  'reply in one line.'

const MODE_NOTE = (skill: string) =>
  'Workbench mode is on in this session: the user ran /workbench, and it lasts until /workbench-quit. ' +
  'Treat each request as the /workbench skill says: hand its coding to local agents with wb, ' +
  `then review and accept. Every plan you write, in plan mode or not, is a handoff plan: steps ` +
  `sized to the agents' slice, each with its files, exact interfaces and acceptance command (the ` +
  `skill's "Planning work you will hand off"). If that procedure is not in your context, read ` +
  `${skill} first.`

// What Claude learned about how this user wants delegation run (`wb notes`), in any project:
// Claude Code's own memory is per project, so these come back wherever workbench mode is on.
const NOTES_NOTE = (notes: string) =>
  'How this user wants delegation run, learned across their projects (`wb notes`; ' +
  '`wb notes add "…"` when they tell you something that holds in any repo, `wb notes drop N` ' +
  `when one no longer does):\n${notes}`

// Every slash command is /workbench or starts with it, so typing /workbench lists them all.
// /workbench is the skill; once the mode is on, running it again brings the pane back
// (2026-11-17: the commands were /delegate-*, and /delegate-pane did that).
const SKILL = 'workbench'
const UPDATE = 'workbench-update'
const QUIT = 'workbench-quit'
const HAND_BACK = 'workbench-quit-and-hand-back-claude'
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

// The module's own memory: reset by a reload, which runs register and session.start again.
const mem = {
  wake: true,
  root: '',
  startedAt: 0,
  lastDrawn: '',
  lastNotify: '-', // notify.jsonl's size and mtime when last read
  paneOpened: false,
  polling: false,
  watching: false,
  wb: 'wb',
  liveKey: '', // the followed task and its updated_at when last read
  liveAt: 0, // when its transcript was last read
  cwd: '', // the session's directory, resolved: which old tasks are this session's (mine)
  home: '', // the home directory, resolved: a session there claims no task by directory
  ids: new Set<string>(), // the `sessions` atom: which tasks are this session's (mine)
  deadPids: new Set<number>(), // Claude Code processes tasks recorded that are gone: final (mine)
  pidAt: new Map<number, number>(), // when a live one was last checked (noteClaude)
  person: false, // someone can be at this session (not a bare `claude -p` or SDK run)
  started: false, // the ears and timers run
  busy: false, // Claude's main loop is mid-turn: a wake would queue and arrive stale
  queued: [] as string[], // turns for Claude that a command asked for
  queuedRetryAt: 0, // a queued turn the engine dropped is tried again from then
  open: new Set<string>(), // this session's delegated tasks open at the last poll
  delegated: false, // workbench mode was on in this load (a hand-back's compaction still keeps the state)
  turnEndedAt: 0, // when Claude's last main-loop turn ended
  follow: true, // the pane sticks to the newest lines until the person scrolls up
  sawTurn: false, // the person has used this session: a weekly check may follow
  scoutAsked: false, // the weekly model check was raised in this load
  updateAsked: false, // `wb update --check --if-due` ran in this load (wb keeps it weekly)
  boundary: false, // one closed since: a moment to make room in Claude's context
  toasted: new Set<string>(),
  noticed: new Map<string, number>(), // each task's newest notice (its ts), as deliver last read
  dropped: new Map<string, number>(), // a task whose wake the engine dropped, and when: rewake retries after a minute
  warned: new Set<string>(), // one-time toasts already shown (a file unreadable, wb failing)
  chatSent: new Set<string>(), // web messages submitted by this load, awaiting their receipt
  chatRetry: new Map<string, number>(), // a web message whose prompt was dropped: tried again from then
  chatUrlAt: 0, // when the web chat's link was last looked up
  eventsPid: 0, // the `wb events` child: its lines wake poll(); the chat counts it as this session
  earsOk: false, // `wb events` has said hello in this load: wb runs
  gone: new Set<string>(), // task directories closed past KEEP_CLOSED_MS: final, never read again
  again: false, // something changed while a poll ran: run another after it
  hasActive: false, // a task is running: the clock redraws
  lastPoll: 0,
  listenerBody: '', // what this session last wrote to chat/listener.json
  tick: 0, // the apprentice's frame count
  posed: '' as Pose | '', // its pose as last drawn in a Raster: the timer animates it while set
  shown: '' as Pose | '', // the frame on screen now
  skill: '', // the /workbench skill's SKILL.md, for the workbench-mode block
}

// Whether path b is path a or inside it, by whole components: /x/ab is not inside /x/a.
const parts = (p: string) => p.split('/').filter(c => c && c !== '.')
const within = (a: string, b: string) => {
  const [pa, pb] = [parts(a), parts(b)]
  return pa.length <= pb.length && pa.every((c, i) => c === pb[i])
}

// Whether a task is this session's. One `wb start` ran in this process's sessions (task.json
// `session`) is, wherever it ran: by directory alone, a `cd /tmp/x && wb start` task was no one's
// (the 0.90 review). One without (started by hand, or before 0.90) goes by directory: its repo
// is the session's directory, inside it or holds it (a session in a subdirectory, or in a folder
// of several repos), or where `wb start` ran (`cwd`) is the session's directory or inside it. So
// does another session's whose Claude Code process (`claude_pid`) is gone: a new process in the
// project adopts it (the 0.90 review: after a crash or an update, plain `claude` in the same repo
// was woken for none of them). A session in the home directory or / claims none by directory: a
// task started from ~ belonged to every session under it (the 0.90 review). A task with no key
// at all is everyone's. `wb`'s _scope judges the same.
const near = (t: { repo?: string; cwd?: string }) => {
  const at = parts(mem.cwd).join('/')
  if (!at || (mem.home && at === parts(mem.home).join('/'))) return false
  return (!!t.cwd && within(mem.cwd, t.cwd)) ||
    (!!t.repo && (within(mem.cwd, t.repo) || within(t.repo, mem.cwd)))
}
const mine = (t: { session?: string; repo?: string; cwd?: string; claude_pid?: number }) => {
  if (t.session && mem.ids.size) {
    return mem.ids.has(t.session) || (mem.deadPids.has(Number(t.claude_pid ?? 0)) && near(t))
  }
  if (!mem.cwd || (!t.cwd && !t.repo)) return true
  return near(t)
}

// Whether another session's task's Claude Code process is gone (mem.deadPids, for `mine`): `kill
// -0`, a live one at most every PID_CHECK_MS. A gone one stays gone; one wb can't ask about
// (kill failed to run) stays unknown, so its task stays the other session's.
const PID_CHECK_MS = 30_000
async function noteClaude($: any, t: { session?: string; claude_pid?: number }, now: number): Promise<void> {
  const pid = Number(t.claude_pid ?? 0)
  if (!pid || !t.session || mem.ids.has(t.session) || mem.deadPids.has(pid)) return
  if (now - (mem.pidAt.get(pid) ?? -Infinity) < PID_CHECK_MS) return
  mem.pidAt.set(pid, now)
  try {
    const r = await $.process.run(['kill', '-0', String(pid)], { timeoutMs: 5000 })
    if (r.exitCode !== 0) mem.deadPids.add(pid)
  } catch {}
}

// This session's id joins the ids this process has had: at the start and at each poll, so a
// /clear's new one counts before its first `wb start` is read.
async function noteSession($: any): Promise<void> {
  const id = await $.session.id().catch(() => '')
  if (!id || mem.ids.has(id)) return
  mem.ids.add(id)
  await update($, sessions, ids => (ids.includes(id) ? ids : [...ids, id]))
}

// A toast shown once per load: something is wrong that the person should hear about once.
const warnOnce = ($: any, key: string, text: string) => {
  if (mem.warned.has(key)) return
  mem.warned.add(key)
  $.ui.toast(`workbench: ${text}`)
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
    const sp = await readJson($, `${base}/task.json`)
    if (!sp.name || sp.bench) continue // a replay of wb bench's: no one's task, never shown or woken
    let recent: string[] = []
    if (ACTIVE.has(phase) || NEEDS.has(phase)) {
      await noteClaude($, sp, now)
      const text = (await readTail($, `${base}/events.jsonl`, EVENTS_TAIL))?.text ?? ''
      recent = parseLines(text).map(eventLine).filter(Boolean).slice(-4) as string[]
    }
    list.push({
      name: sp.name, phase, round: Number(st.round ?? 1), model: String(sp.model ?? ''),
      label: String(sp.model_label ?? ''),
      tests: st.tests ? (st.tests.ok ? 'pass' : 'fail') : null,
      last: String(st.last ?? ''), createdAt: Number(sp.created_at ?? 0) * 1000, updatedAt,
      tokens: Number(st.tok_in ?? 0) + Number(st.tok_out ?? 0), recent,
      repo: String(sp.repo ?? ''), cwd: String(sp.cwd ?? ''), session: String(sp.session ?? ''),
      claude_pid: Number(sp.claude_pid ?? 0),
    })
  }
  for (const name of mem.gone) if (!names.has(name)) mem.gone.delete(name) // removed by hand

  return list.sort((a, b) => a.createdAt - b.createdAt)
}

async function scoutIfDue($: any, now: number): Promise<void> {
  const path = `${mem.root}/scout.json`
  const st = await readJson($, path)
  const done = Number(st.done_at ?? 0) * 1000
  const offered = Number(st.offered_at ?? 0) * 1000
  if (now - done < SCOUT_EVERY_MS || now - offered < SCOUT_RETRY_MS) return
  await scoutOffered($, now)
  void $.prompt.submit({ text: SCOUT_NOTE })
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
    const r = await $.session.compact({ instructions: KEEP_ON_COMPACT })
    if (!r.skip) {
      $.ui.toast(`workbench: a task closed; compacted Claude's context ` +
        `(${Math.round(context.tokens / 1000)}K tokens) to make room`)
    }
  } catch {
    // a turn began meanwhile: the next boundary tries again
  }
}

// `wb <args>`: ok when it exited 0, and what it said. A failing wb's traceback is not output to
// pass on as if it were the answer (the 2026-11-16 audit: it reached Claude as "open tasks").
// `wb update` takes the longest the engine allows: its steps may take 300 s each.
async function runWb($: any, args: string[], timeoutMs = 120_000): Promise<{ ok: boolean; out: string }> {
  try {
    const r = await $.process.run([mem.wb, ...args], { timeoutMs })
    return { ok: r.exitCode === 0, out: `${r.stdout}${r.stderr}`.trim() || '(no output)' }
  } catch (err) {
    return { ok: false, out: `could not run wb ${args.join(' ')}: ${err}` }
  }
}

// `--session <id>` for each id this process has had, and `--repo <the session's directory>` for
// tasks without one: a quit or the status block takes this session's tasks only, as `mine` does.
async function scope($: any): Promise<string[]> {
  await noteSession($)
  return [...(mem.cwd ? ['--repo', mem.cwd] : []), ...[...mem.ids].flatMap(id => ['--session', id])]
}

// The task worth watching. The one followed stays while it runs: with two running, choosing by
// the latest status write swapped the pane between them on every tool call (the 2026-11-16
// audit). Else the newest started that runs (running, testing, asking before queued: a queued one
// shows nothing yet), else the newest waiting for Claude.
const focusOf = (list: WbTask[], followed: string): WbTask | undefined => {
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
  const pane = ((await $.ui.panes().catch(() => [])) as Row[]).find(p => p.id === PANE)
  if (!pane?.isShown) return // read when it shows again: the key still differs
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
    // Outside workbench mode, only this session's tasks show: another session's task running
    // opened the pane and filled the status line in a session the user kept off workbench, and
    // /workbench-quit couldn't clear it (2026-11-17)
    const shown = on ? list : list.filter(t => mine(t))
    const live = shown.filter(t => ACTIVE.has(t.phase) && t.phase !== 'asking')
    const asking = shown.filter(t => t.phase === 'asking')
    const waiting = shown.filter(t => NEEDS.has(t.phase) && t.phase !== 'asking')
    const parts = [
      ...live.map(t => `${t.name} ${t.phase} r${t.round}`),
      ...asking.map(t => `${t.name} asks`),
      ...(waiting.length ? [`${waiting.length} awaiting review`] : []),
    ]
    $.ui.status(parts.length ? `⚒ wb · ${parts.join(' · ')}` : on ? '⚒ wb · workbench mode' : undefined)
    if ((live.length || on) && !mem.paneOpened) { // open once per load, in workbench mode or when its work starts
      mem.paneOpened = true
      void $.ui.open({ id: PANE, title: 'Workbench' })
    }
    void refreshLive($, list, now)
    if (!mem.busy && mem.queued.length && now >= mem.queuedRetryAt) void sendQueued($, mem.queued.shift()!)
    const isOpen = (t: WbTask) => ACTIVE.has(t.phase) || NEEDS.has(t.phase)
    // Only this session's tasks, and only in workbench mode, make a boundary: a task closing
    // compacted every session's context, delegating or not (the 2026-11-16 audit).
    const open = new Set(list.filter(t => isOpen(t) && mine(t)).map(t => t.name))
    if (on && [...mem.open].some(name => !open.has(name))) mem.boundary = true
    if (!on) mem.boundary = false
    mem.open = open
    if (mem.boundary && !mem.busy && !list.some(t => NEEDS.has(t.phase) && mine(t))) {
      mem.boundary = false
      await compactAtBoundary($)
    }
    // the weekly check needs wb to run (`wb events` said hello) and a quiet minute after a turn
    if (mem.sawTurn && mem.earsOk && !mem.busy && !mem.scoutAsked && !list.some(isOpen) &&
      now - mem.turnEndedAt >= SCOUT_QUIET_MS) await scoutIfDue($, now)
    if (mem.sawTurn && mem.earsOk && !mem.updateAsked) {
      mem.updateAsked = true
      void checkUpdate($) // off the poll's path: a stalled fetch held every wake (the 2026-11-16 audit)
    }
    await deliver($, now)
    await rewake($, now, list)
    await listen($, now)
    if (now - mem.chatUrlAt > 10 * 60_000) { // the link changes only with the token
      mem.chatUrlAt = now
      const { ok, out } = await runWb($, ['chat', 'url'])
      const link = ok && /^https?:\/\/\S+#token=\S+$/.test(out) ? out : ''
      if (link !== (await read($, chat)).url) await update($, chat, c => ({ ...c, url: link }))
    }
  } finally {
    mem.polling = false
  }
  if (mem.again) void poll($)
}

async function checkUpdate($: any) {
  const { ok, out } = await runWb($, ['update', '--check', '--if-due'])
  if (ok && out.startsWith('update available')) $.ui.toast(`workbench: ${out}`)
}

// A turn a command asked for. One the engine drops (a settings hook's block, a Stop, a host's
// limit) goes back on the queue and is tried again in a minute: the hand-back's turn was lost
// after its agents were already stopped (the 2026-11-16 audit).
async function sendQueued($: any, text: string) {
  const r = await $.prompt.submit({ text }).catch((err: unknown) => ({ drop: String(err) }))
  if (!r?.drop) return
  mem.queued.unshift(text)
  mem.queuedRetryAt = (await $.clock.now()) + REWAKE_MS
  warnOnce($, `queued:${text.slice(0, 40)}`, `a turn for Claude was dropped (${r.drop}); trying again in a minute`)
}

// Whether a process lives: `kill -0`, since macOS has no /proc.
async function alive($: any, pid: number): Promise<boolean> {
  try {
    return (await $.process.run(['kill', '-0', String(pid)], { timeoutMs: 5000 })).exitCode === 0
  } catch {
    return false
  }
}

async function listen($: any, now: number) {
  const st = await read($, chat)
  if (!st.delegating) return
  const dir = `${mem.root}/chat`
  if (!mem.eventsPid) return // the chat knows a listener by its live `wb events` process
  const claim = await readJson($, `${dir}/listener.json`)
  const theirs = claim.session && claim.session !== st.key
  if (theirs && (claim.pid ? await alive($, Number(claim.pid))
    : now - Number(claim.ts ?? 0) * 1000 < CHAT_STALE_MS)) return // another session listens
  if (!(await $.fs.exists(dir))) await runWb($, ['chat', 'log']) // wb makes the directory
  const body = JSON.stringify({ session: st.key, cwd: mem.cwd, pid: mem.eventsPid, busy: mem.busy })
  if (body !== mem.listenerBody || theirs || claim.session !== st.key) { // only on change: a write is an event
    mem.listenerBody = body
    await $.fs.write(`${dir}/listener.json`, body)
  }
  // the tails: both files only grow, and past 4 MiB the whole read failed and the chat went silent
  const messages = await readTail($, `${dir}/messages.jsonl`, NOTIFY_TAIL)
  const receipts = await readTail($, `${dir}/receipts.jsonl`, NOTIFY_TAIL)
  if (!messages || !receipts) {
    warnOnce($, 'chat', `can't read the web chat's files in ${dir}: wb doctor`)
    return
  }
  const got = new Set(receipts.text.split('\n')
    .map(l => { try { return JSON.parse(l).id } catch { return undefined } }))
  for (const line of messages.text.split('\n')) {
    let m: Row
    try { m = JSON.parse(line) } catch { continue }
    const id = String(m.id ?? '')
    if (m.from !== 'user' || !id || got.has(id) || mem.chatSent.has(id)) continue
    if (now - Number(m.ts ?? 0) * 1000 > CHAT_EXPIRE_MS) continue // stale: the page shows it undelivered
    if ((mem.chatRetry.get(id) ?? 0) > now) continue // its prompt was dropped: again in a minute
    mem.chatSent.add(id)
    // The terminal shows only `[web chat] <their words>`: how to answer is the skill's to say.
    const text = `${WEB} ${String(m.text ?? '')}`
    // Mid-turn, into the running turn, as a message typed mid-turn would be; else, or if that is
    // refused, as a prompt of its own (which waits for the turn to end).
    if (mem.busy) {
      const r = await $.session.send({ to: { sessionId: await $.session.id() }, text: `${text}\n\n${WEB_NOTE}` })
        .catch(() => ({ isDelivered: false }))
      if (r.isDelivered) await runWb($, ['chat', 'ack', id, '--by', st.key, '--how', 'now'])
      else void sendWeb($, id, text, 'queued', st.key)
    } else void sendWeb($, id, text, 'now', st.key)
    await update($, chat, c => ({ ...c, webAt: now }))
    $.ui.toast('workbench: a message from the web chat')
  }
}

// A web message as a prompt of its own; the receipt only once it entered. A dropped one had a
// "read" receipt for a message Claude never got (the 2026-11-16 audit): it is tried again.
async function sendWeb($: any, id: string, text: string, how: string, by: string) {
  const r = await $.prompt.submit({ text, asUser: true }).catch((err: unknown) => ({ drop: String(err) }))
  if (r?.drop) {
    mem.chatSent.delete(id)
    mem.chatRetry.set(id, (await $.clock.now()) + REWAKE_MS)
    return
  }
  const { ok, out } = await runWb($, ['chat', 'ack', id, '--by', by, '--how', how])
  if (!ok) warnOnce($, 'chat-ack', `wb chat ack failed: ${brief(out)}`)
}

// Notifications nobody has acknowledged yet: one toast each, and, for this session's tasks, a
// turn for Claude if waking. Another session's task is left for its own session (or `wb wait`):
// not acknowledged here.
async function deliver($: any, now: number) {
  const root = mem.root
  const file = await readTail($, `${root}/notify.jsonl`, NOTIFY_TAIL)
  if (!file) {
    warnOnce($, 'notify', `can't read ${root}/notify.jsonl, so no wake can come: wb doctor`)
    return
  }
  // While Claude is mid-turn, look every poll (the turn may act on the task) but hold the wake:
  // a prompt submitted now would queue and arrive after the task has moved on.
  if (file.key === mem.lastNotify && !mem.busy) return
  // Per task, its newest notice and every question: two asked at once (parallel tool calls) are
  // both open, and the newest alone hid the first, its agent blocked (the 2026-11-16 audit).
  const due = new Map<string, Row[]>()
  for (const row of parseLines(file.text)) {
    // Every notice counts for rewake, however old: a notice delivered before a reload (or by
    // another session) is news Claude had, not a lost one (the 2026-11-16 audit).
    mem.noticed.set(row.task, Math.max(mem.noticed.get(row.task) ?? 0, row.ts))
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
    const spec = await readJson($, `${root}/tasks/${name}/task.json`)
    await noteClaude($, spec, now)
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
      const headline = HEADLINE[row.kind] ?? row.kind
      const key = `${name}:${row.ts}`
      if (current && !mem.toasted.has(key)) {
        mem.toasted.add(key)
        $.ui.toast(`workbench: ${name} ${headline}`)
      }
      if (!own || !mem.wake) continue // the toast only: the notice stays for its session or wb wait
      if (mem.busy && current) { // deliver once the turn ends
        held = true
        continue
      }
      const prev = acked
      await $.fs.write(ackPath, String(row.ts)) // acknowledge before waking: never wake twice
      acked = row.ts
      if (!current) continue
      const next = (NEXT[row.kind] ?? NEXT.failed)(name, row.id ? String(row.id) : undefined)
      void wake($, name, `[workbench] Task \`${name}\` ${headline}.\n\n${row.text}\n\n${next}`, prev, row.ts)
    }
  }
  if (!held) mem.lastNotify = file.key
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
  const r = await $.prompt.submit({ text }).catch((err: unknown) => ({ drop: String(err) }))
  if (!r?.drop) return
  mem.dropped.set(name, await $.clock.now())
  const ackPath = `${mem.root}/tasks/${name}/acked`
  if (Number(await $.fs.read(ackPath).catch(() => '0')) === ts) await $.fs.write(ackPath, String(prev))
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
    if (!NEEDS.has(t.phase) || !mine(t) || now - t.updatedAt < REWAKE_MS || now - t.updatedAt > REWAKE_MAX_MS) continue
    if (now - (mem.dropped.get(t.name) ?? 0) < REWAKE_MS) continue // dropped: once a minute
    const ackPath = `${mem.root}/tasks/${t.name}/acked`
    const acked = Number(await $.fs.read(ackPath).catch(() => '0')) || 0
    // Claude had the news if it acked the task's latest notice: the runner's last status write
    // (its pid cleared) lands just after that notice, so updated_at alone would wake it twice
    const noticed = mem.noticed.get(t.name)
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

// Turning workbench mode on or off: the chat listens while it is on. The context block follows
// at the next compaction or /clear, not at once: invalidating it would rewrite the first message
// and cost the whole conversation's cache, and the skill or the quit note says it meanwhile.
// The quit commands close the pane, the mode's sign.
async function workbenchMode($: any, on: boolean) {
  if (on) mem.delegated = true
  if ((await read($, chat)).delegating === on) return
  await update($, chat, c => ({ ...c, delegating: on }))
  if (!on) {
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

// Opens the pane, or brings it back: a pane the engine still holds but doesn't show (a tab behind
// another, or left undrawn) only retitles on open, so it is closed first.
async function showPane($: any) {
  const held = (await $.ui.panes()).find((p: any) => p.id === PANE)
  if (held && (!held.isShown || !held.isPlaced)) await $.ui.close({ id: PANE }).catch(() => {})
  mem.paneOpened = true
  return $.ui.open({ id: PANE, title: 'Workbench' })
}

// The task /workbench was given, as the skill's text has it after expansion ("**First task:**
// `<arguments>`."): empty when the user typed /workbench alone.
const firstTask = (text: string) => /\*\*First task:\*\* `([\s\S]*?)`\. Empty\?/.exec(text)?.[1].trim() ?? ''

export const register: Register = (on, options) => {
  mem.wake = (options as Row | undefined)?.wake !== 'off'

  on('session.start', async ($, e, next) => {
    const home = await $.env.get('HOME')
    const wbHome = await $.env.get('WB_HOME')
    mem.root = wbHome || `${home}/.local/state/workbench`
    mem.wb = (await $.env.get('WB_BIN')) || 'wb'
    mem.skill = `${(await $.env.get('CLAUDE_CONFIG_DIR')) || `${home}/.claude`}/skills/workbench/SKILL.md`
    mem.startedAt = await $.clock.now()
    // 2026-11-17: /delegate became /workbench. A copy updated across it still has only the old
    // skill link, which now leads nowhere: wb setup makes the new one and drops the old.
    const skills = mem.skill.slice(0, -'/workbench/SKILL.md'.length)
    if (!(await $.fs.stat(mem.skill).catch(() => undefined))
      && (await $.fs.stat(`${skills}/delegate`).catch(() => undefined))?.isLink) {
      const { ok, out } = await runWb($, ['setup'])
      $.ui.toast(ok ? 'workbench: /delegate is now /workbench (wb setup linked it)'
        : `workbench: /delegate is now /workbench, but wb setup failed: ${brief(out)}`)
    }
    if (!(await read($, chat)).key) {
      const key = `${Math.floor(mem.startedAt)}-${Math.random().toString(36).slice(2, 8)}`
      await update($, chat, c => ({ ...c, key }))
    }
    await $.command.register({
      name: MODELS,
      description: 'Check now which local models suit this machine best (weekly on its own)',
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
    const real = async (p: string) => (await $.fs.stat(p, { resolve: true }).catch(() => undefined))?.realPath ?? p
    mem.cwd = await real(e.cwd)
    mem.home = home ? await real(home) : ''
    mem.ids = new Set(await read($, sessions))
    await noteSession($)
    // A `claude -p` run or an SDK session has no person at it until a surface attaches: it took
    // other sessions' wakes, the weekly check and a `wb status` before every prompt (the
    // 2026-11-16 audit). It stays quiet until then.
    mem.person = e.isInteractive || e.surface != null
    if (mem.person) startEars($)
    return next(e)
  })

  on('session.attach', async ($, e, next) => {
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
    return next(e)
  })

  // Delegating with wb counts as workbench mode too (a session resumed mid-delegation, or one
  // where /workbench ran before this mod could see it)
  on('tool.call', { tool: 'Bash' }, async ($, e, next) => {
    // the commands that hand work out (accept and takeover also close what a hand-back gave Claude)
    if (/(^|[\s;&|(])wb (start|next|feedback|steer|answer)\b/.test(e.command)) {
      await workbenchMode($, true)
    }
    return next(e)
  })

  // /workbench turns workbench mode on for the session, with or without a task, and shows the pane.
  // Run again while it is on, it brings the pane back; alone, that is all it does.
  on('skill.prompt', { skill: SKILL }, async ($, e, next) => {
    if (e.path) mem.skill = e.path
    const again = (await read($, chat)).delegating
    mem.paneOpened = true // before the poll workbenchMode starts, which would open it too
    await workbenchMode($, true)
    await showPane($)
    if (again && !firstTask(e.text)) return { text: AGAIN_NOTE(mem.skill) }
    return next(e)
  })

  // Typed in the terminal: the user is back, so answers go here again
  on('prompt.submit', async ($, e, next) => {
    if (!e.isMeta && !e.text.startsWith(WEB) && !e.text.startsWith('[workbench]') && (await read($, chat)).webAt) {
      await update($, chat, c => ({ ...c, webAt: 0 }))
    }
    return next(e)
  })

  // This session's tasks only (`--repo`): another session keeps its own running.
  on('command.run', { command: QUIT }, async $ => {
    await workbenchMode($, false)
    const { ok, out } = await runWb($, ['stop', '--all', ...(await scope($))])
    $.ui.toast(ok ? 'workbench: this session\'s delegated agents are stopped'
      : `workbench: wb stop --all failed: ${brief(out)}`)
    return { text: ok ? `Delegation stopped. ${out}` : `Delegation not stopped: ${out}`, context: [QUIT_NOTE(out, ok)] }
  })

  on('command.run', { command: MODELS }, async $ => {
    await scoutOffered($, await $.clock.now()) // the weekly check won't raise it again straight after
    mem.queued.push(SCOUT_NOTE) // a command may not start a turn itself: the next poll does
    return { text: 'Claude will check the local models against this machine now.' }
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
    const { ok, out } = await runWb($, ['takeover', '--all', ...(await scope($))])
    if (!ok) { // nothing was handed over: no turn telling Claude to finish it
      $.ui.toast(`workbench: wb takeover --all failed: ${brief(out)}`)
      return { text: `wb takeover --all failed; the agents may still be running (wb status):\n${out}` }
    }
    mem.queued.push(DONE_NOTE(out)) // a command may not start a turn itself: the next poll does
    return { text: `Agents stopped; Claude is taking over. ${out}` }
  })

  // Whoever compacts the main conversation, the summary keeps the delegation state: in a session
  // that delegated in this load, with a task of its own open. Any session's compaction got it
  // (the 2026-11-16 audit).
  on('session.compact', async ($, e, next) => {
    if (e.agentId || !mem.open.size || e.instructions?.includes(KEEP_ON_COMPACT)) return next(e)
    if (!mem.delegated && !(await read($, chat)).delegating) return next(e)
    return next({ ...e, instructions: [e.instructions, KEEP_ON_COMPACT].filter(Boolean).join('\n\n') })
  })

  // A fresh or just-compacted conversation is told which of its tasks are open, from wb itself.
  on('prompt.context', async ($, e, next) => {
    const r = await next(e)
    if (!mem.person) return r // a scripted run: no `wb status` before every prompt
    const st = await read($, chat)
    const away = st.webAt && (await $.clock.now()) - st.webAt < AWAY_MS
    const blocks = [...r.blocks]
    if (st.delegating) blocks.push({ name: 'workbench-mode', text: MODE_NOTE(mem.skill) })
    const notes = st.delegating ? (await $.fs.read(`${mem.root}/notes.md`).catch(() => '')).trim() : ''
    if (notes) blocks.push({ name: 'user-notes', text: NOTES_NOTE(notes) })
    if (away) blocks.push({
      name: 'web-chat',
      text: 'The user is away from the terminal and last wrote from the web chat. Tell them ' +
        'there (`wb chat reply --text "…"`) when something they asked for is done or needs them; ' +
        '`wb chat log` shows the thread. Typing in the terminal again ends this.',
    })
    if (!mem.wake && (st.delegating || mem.open.size)) blocks.push({ name: 'workbench-wake', text: WAKE_OFF_NOTE })
    const { ok, out: status } = await runWb($, ['status', ...(await scope($))])
    if (!ok) warnOnce($, 'status', `wb status failed: ${brief(status)}; wb doctor`)
    if (!ok || status.startsWith('no open tasks')) return { blocks }
    return {
      blocks: [...blocks, {
        name: 'workbench',
        text: `Tasks delegated to local agents with wb (the /workbench skill) are open:\n${status}\n` +
          'Pick them up from here: `wb report <name>` for one, `wb watch <name>` to follow it.',
      }],
    }
  })

  // Only the main loop's turns: a subagent's run doesn't hold the prompt.
  on('turn.start', async ($, e, next) => {
    if (!e.agentId) {
      mem.busy = true
      void poll($) // the chat page shows "working" now
    }
    return next(e)
  })
  on('turn.complete', async ($, e, next) => {
    if (!e.agentId) {
      mem.busy = false
      mem.sawTurn = true
      mem.turnEndedAt = await $.clock.now()
      void poll($) // held wakes and chat messages go now, not at the next tick
    }
    return next(e)
  })

  // The person's scrolling decides whether the pane follows: up to read stops it, back down to
  // the end starts it again.
  on('ui.scroll', { requestId: PANE }, async ($, e, next) => {
    if (e.origin.kind === 'person') mem.follow = e.offset + e.bodyRows >= e.contentRows - 1
    return next(e)
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text, Raster } = $.ui.resolve(e) as Row
    const list = await read($, tasks)
    const watched = await read($, live)
    const { url, delegating } = await read($, chat)
    // the web chat's link, always at hand to copy: last, since the pane follows its end
    const pinned = url ? (
      <Box marginTop={1}>
        <Text>
          <Text bold color="claude">web chat </Text>
          <Text dimColor>{url}</Text>
        </Text>
      </Box>
    ) : null
    const props = (e.props ?? {}) as Row
    const cols = Math.max(30, Number(props.bodyColumns ?? e.viewport?.columns ?? 60))
    const now = await $.clock.now()
    const spin = SPIN[Math.floor(now / 500) % SPIN.length]

    // the followed task's own recent lines are in its transcript below, in full
    const recentOf = (t: WbTask) => (t.name === watched.task ? [] : t.recent)
    const when = (t: WbTask) =>
      ACTIVE.has(t.phase) || NEEDS.has(t.phase) ? `for ${ago(now - t.createdAt)}` : `${ago(now - t.updatedAt)} ago`
    // The history is drawn, newest within HISTORY_CHARS: the engine scrolls the pane (arrows,
    // while focused) and follows the end as lines arrive. What the agent is doing now is the
    // apprentice's to say.
    const all = watched.lines.filter(l => !['activity', 'clock', 'meta'].includes(l.s))
    let from = all.length // (each line was cut to LINE_CHARS when read)
    for (let used = 0; from > 0 && used + (all[from - 1].t?.length ?? 0) + 1 <= HISTORY_CHARS; from--)
      used += (all[from - 1].t?.length ?? 0) + 1
    const history = all.slice(from)
    const doing = watched.lines.find(l => l.s === 'activity')
    const clock = watched.lines.find(l => l.s === 'clock')
    const meta = watched.lines.find(l => l.s === 'meta')

    // The apprentice, in pixels where the pane has the rows for it (it takes six), else its face
    // on the line. Drawing it in pixels arms the timer that animates it. The stage keeps its
    // height beside a long history, and the bubble wraps its words onto more rows rather than
    // cutting them off: the pane can't be made wider.
    const focus = focusOf(list, watched.task)
    const sp = speechOf(focus, doing?.t ?? '', list, delegating)
    const busy = sp.pose === 'work' || sp.pose === 'think'
    const pixels = (e as Row).surface === 'terminal' && Number(props.scroll?.bodyRows ?? 24) >= 20 && cols >= 40
    // The bubble's inner width beside the apprentice (its columns, the mouth's, the border and
    // padding), or beside the face on one line; each line drawn whole, never squeezed.
    const room = Math.max(10, pixels ? cols - COLUMNS - 1 - 4 : cols - 4)
    const lines = (text: string, props: Record<string, unknown>) =>
      wrapWords(text, room).map(l => <Text {...props} wrap="truncate-end">{l}</Text>)
    const said = [
      ...lines(`${busy && doing ? `${spin} ` : ''}${sp.say}`,
        busy ? LINE_STYLE.activity : { color: sp.color, bold: true }),
      ...(sp.more ? lines(sp.more, { dimColor: true }) : []),
      ...(clock && focus?.name === watched.task ? lines(clock.t, { dimColor: true }) : []),
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
        {list.map(t => (
          <Box flexDirection="column" marginBottom={1}>
            <Text>
              <Text color={COLOR[t.phase] ?? 'text'}>
                {t.phase === 'running' ? spin : MARK[t.phase] ?? '•'}{' '}
              </Text>
              <Text bold>{t.name}</Text>
              <Text color={COLOR[t.phase] ?? 'text'}> {t.phase}</Text>
              <Text dimColor>
                {' '}· round {t.round} · {t.label || t.model}
                {t.tests ? ` · tests ${t.tests}` : ''} · {when(t)}
              </Text>
            </Text>
            {recentOf(t).map(line => (
              <Text dimColor>  {clip(line, cols - 2)}</Text>
            ))}
            {t.name !== watched.task && t.recent.length === 0 && (
              <Text dimColor>  {clip(t.last, cols - 2)}</Text>
            )}
          </Box>
        ))}
        {watched.task && (
          <Box flexDirection="column">
            <Text dimColor>{clip(`── ${watched.task} · ↑↓ scrolls ` + '─'.repeat(cols), cols)}</Text>
            {meta && <Text dimColor>{meta.t}</Text>}
            {from > 0 && <Text dimColor>… {from} earlier lines: `wb watch {watched.task}` has them all</Text>}
            {history.map(l => (
              <Text {...(LINE_STYLE[l.s] ?? {})}>{l.t || ' '}</Text>
            ))}
          </Box>
        )}
        {stage}
        {pinned}
      </Box>
    )
  })
}
