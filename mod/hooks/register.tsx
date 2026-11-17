// workbench: the Claude Code side of delegating to local OpenCode agents.
//
// It reads the task files `wb` and its runners write (~/.local/state/workbench, or $WB_HOME) and
// does three things with them: draws a live pane of the tasks, keeps a one-line status, and, when
// a task needs Claude (ready for review, a question, stuck, failed), starts a turn for Claude with
// the task's report in it, so nobody has to poll. Acknowledgements are shared with `wb wait`
// through each task's `acked` file, so a notification wakes Claude once, by whichever got it first.
// A wake is held while Claude's main loop is mid-turn (a prompt submitted then would queue and
// arrive stale) and checked against the task's phase when it is finally sent.
// Claude's own context. Whatever compacts it (the engine at its limit, /compact, this mod), the
// summary is told to keep the delegation state, and after a compaction or /clear the open tasks
// come back as a context block. At a task boundary (one just closed, none waiting for Claude) the
// mod compacts early once Claude is COMPACT_AT of the way to the engine's own compaction, so a
// long delegation session sheds finished tasks' detail at a clean point instead of mid-review.
const COMPACT_AT = 0.5
const KEEP_ON_COMPACT =
  'Workbench delegation is in progress. Keep: the user\'s goals and decisions, which tasks were ' +
  'delegated (names, repos, branches, what each is for), which are open and what each needs ' +
  'next, and what was accepted, merged or discarded. Drop the diffs, reports and transcripts of ' +
  'finished tasks; `wb status` and `wb report <name>` bring back any detail.'
// The weekly model check: once a week, in a quiet moment of a session the person is using,
// Claude is asked to look at this machine, what is installed and how it has done, and what is
// newest, and to recommend what to add or retire. /delegate-models asks for it now.
const SCOUT_EVERY_MS = 7 * 24 * 3600 * 1000
const SCOUT_RETRY_MS = 24 * 3600 * 1000 // raised but never finished: ask again a day later
const MODELS = 'delegate-models'
const SCOUT_NOTE =
  '[workbench] The weekly model check is due. As the driver, keep the local agents on the best ' +
  'models this machine can run:\n' +
  '1. `wb scout`: this machine\'s hardware, what is installed, and how each model has done ' +
  '(real tasks), and its watch list: models set aside and what each needs ' +
  'before it is tried again.\n' +
  '2. Look up what is newest: open-weight coding models with tool calling, their sizes and ' +
  'quantizations, which fit this GPU\'s VRAM (with room for the context) and this RAM, and ' +
  'recent releases of the runtimes. Prefer primary sources (model cards, the Ollama library, ' +
  'release notes) and recent reports; name nothing you could not verify.\n' +
  '3. Recommend in a few lines: keep, add (the model and quantization, why it should beat what ' +
  'is installed, its size and how it fits), retire (what it replaces, the disk it frees). ' +
  'Aim for the best results first, then efficiency. For each watch-list item, say whether ' +
  'its conditions are met yet; `wb scout --unwatch N` once one is settled.\n' +
  '4. Ask before downloading or deleting. With a yes: `wb model add <provider/model> --pull` in ' +
  'the background, `wb model sizes <id> …` if it should only take some task sizes, and ' +
  '`wb model retire <id> [--delete]` for what it replaces. Installed models that are not ' +
  'registered may belong to other tools: ask first.\n' +
  '5. The skill library: rewrite or retire (`wb skill retire`) what `wb scout` flags.\n' +
  '6. `wb scout --done --notes "<what you recommended or changed>"`.\n' +
  'Keep it short; if nothing is worth changing, say so in one line.'
// Delegate mode: /delegate turns it on for the rest of the session and opens the pane, its sign.
// The user then asks for things in plain words, and Claude hands the coding to local agents as
// the skill says. A fresh or compacted conversation is told the mode is still on and where the
// skill is (`delegate-mode` block). Two slash commands end it: /delegate-quit stops every agent
// at once (`wb stop --all`) and tells Claude so; /delegate-quit-and-hand-back-claude stops the
// agents and hands their tasks to Claude (`wb takeover --all`), starting a turn for Claude to
// finish them. Either closes the pane.
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
  shape: 'tasks-1',
})

const live = atom({ plugin: 'workbench', key: 'live' } as const, { task: '', lines: [] } as WbLive, {
  shape: 'live-1',
})
const KEEP_LINES = 2000 // the pane scrolls back through this much history
// The engine draws a pane's first 100,000 characters of text and drops the rest, and the
// apprentice comes after the history: a long task's history pushed its words (and its mouth)
// past the cut, leaving an empty bubble (seen 2026-11-16, 119,728 characters). So the history
// drawn is its newest lines within this budget, leaving the rest of the pane room.
const HISTORY_CHARS = 60000

// The web chat (`wb chat serve`): the user's side door while away from the terminal. A session
// listens while delegate mode is on in it (`delegating`), from /delegate until a quit command;
// with several, the first to claim chat/listener.json does, and the others stand by. A message is
// submitted as a prompt (so the terminal shows it too) with a receipt the page shows at once.
const chat = atom({ plugin: 'workbench', key: 'chat' } as const,
  { key: '', delegating: false, webAt: 0, url: '' } as WbChat, { shape: 'chat-2' })
const CHAT_EXPIRE_MS = 6 * 3600_000 // a web message older than this isn't submitted to a new listener
const CHAT_STALE_MS = 15_000 // as wb's LISTENING_FOR: a listener silent this long has gone
const AWAY_MS = 6 * 3600 * 1000 // the user counts as away this long after a web message
const WEB = '[web chat]'
// Only for a delivery into a running turn, which the engine frames as another session's message:
// the rules for answering live in the /delegate skill (The web chat), loaded whenever this listens.
const WEB_NOTE =
  '(The user wrote this in the web chat; the workbench mod delivered it into your running turn. ' +
  'Answer as /delegate\'s "The web chat" says: `wb chat reply --text "…"`.)'

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
    const more = on ? 'Ask Claude for anything: the coding comes to me.' : '/delegate turns delegate mode on.'
    if (list.length) return { pose: 'rest', say: 'All caught up.', more, color: 'inactive' }
    return on
      ? { pose: 'rest', say: 'Delegate mode is on.', more, color: 'claude' }
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
const NEXT: Record<string, (n: string) => string> = {
  review: n => `Review it as /delegate says: \`wb diff ${n}\`, then \`wb accept ${n} -m "…"\` ` +
    `or \`wb feedback ${n} --message <file>\`.`,
  question: n => `Answer with \`wb answer ${n} --text "…"\`; it is blocked until then. A ` +
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

const MODE_NOTE = (skill: string) =>
  'Delegate mode is on in this session: the user ran /delegate, and it lasts until /delegate-quit. ' +
  'Treat each request as the /delegate skill says: hand its coding to local agents with wb, ' +
  `then review and accept. Every plan you write, in plan mode or not, is a handoff plan: steps ` +
  `sized to the agents' slice, each with its files, exact interfaces and acceptance command (the ` +
  `skill's "Planning work you will hand off"). If that procedure is not in your context, read ` +
  `${skill} first.`

// What Claude learned about how this user wants delegation run (`wb notes`), in any project:
// Claude Code's own memory is per project, so these come back wherever delegate mode is on.
const NOTES_NOTE = (notes: string) =>
  'How this user wants delegation run, learned across their projects (`wb notes`; ' +
  '`wb notes add "…"` when they tell you something that holds in any repo, `wb notes drop N` ' +
  `when one no longer does):\n${notes}`

const QUIT = 'delegate-quit'
const HAND_BACK = 'delegate-quit-and-hand-back-claude'
const QUIT_NOTE = (out: string) =>
  `[workbench] The user ran /${QUIT}: everything delegated was stopped (\`wb stop --all\`):\n` +
  `${out}\nDelegate mode is off: from now on work directly, without delegating. Don't finish, ` +
  `restart or clean up any of the stopped work unless they ask; real tasks keep their ` +
  `worktrees (\`wb status --all\`).`
const DONE_NOTE = (out: string) =>
  `[workbench] The user ran /${HAND_BACK}: the agents were stopped and their tasks ` +
  `handed to you (\`wb takeover --all\`):\n${out}\n\nLeave delegate mode and finish the work ` +
  `yourself, without pausing to ask. For each task: read what the agent left (\`wb diff <name>\`), ` +
  `complete it in its worktree to your own standard, run its acceptance command, then ` +
  `\`wb accept <name> -m "…"\` (add --merge only if the user asked for merging). Then carry on ` +
  `with the rest of the user's request directly, without delegating, and report briefly at the end.`

// The module's own memory: reset by a reload, which runs register and session.start again.
const mem = {
  wake: true,
  root: '',
  startedAt: 0,
  lastDrawn: '',
  lastNotifyLength: -1,
  paneOpened: false,
  polling: false,
  watching: false,
  wb: 'wb',
  liveKey: '', // the followed task and its updated_at when last read
  busy: false, // Claude's main loop is mid-turn: a wake would queue and arrive stale
  queued: [] as string[], // turns for Claude that a command asked for
  open: new Set<string>(), // delegated tasks open at the last poll
  follow: true, // the pane sticks to the newest lines until the person scrolls up
  sawTurn: false, // the person has used this session: a weekly check may follow
  scoutAsked: false, // the weekly model check was raised in this load
  boundary: false, // one closed since: a moment to make room in Claude's context
  toasted: new Set<string>(),
  noticed: new Map<string, number>(), // each task's newest notice (its ts), as deliver last read
  chatSent: new Set<string>(), // web messages submitted by this load, awaiting their receipt
  chatUrlAt: 0, // when the web chat's link was last looked up
  eventsPid: 0, // the `wb events` child: its lines wake poll(); the chat counts it as this session
  again: false, // something changed while a poll ran: run another after it
  hasActive: false, // a task is running: the clock redraws
  lastPoll: 0,
  listenerBody: '', // what this session last wrote to chat/listener.json
  tick: 0, // the apprentice's frame count
  posed: '' as Pose | '', // its pose as last drawn in a Raster: the timer animates it while set
  shown: '' as Pose | '', // the frame on screen now
  skill: '', // the /delegate skill's SKILL.md, for the delegate-mode block
}

// Every task directory, as the pane draws it. Closed tasks drop out after half an hour.
async function scan($: any, root: string, now: number): Promise<WbTask[]> {
  const dir = `${root}/tasks`
  const entries = (await $.fs.exists(dir)) ? await $.fs.list(dir) : []
  const list: WbTask[] = []
  for (const ent of entries) {
    if (ent.kind !== 'dir') continue
    const base = `${dir}/${ent.name}`
    const [st, sp] = await Promise.all([
      readJson($, `${base}/status.json`), readJson($, `${base}/task.json`),
    ])
    if (!sp.name) continue
    const phase = String(st.phase ?? 'queued')
    const updatedAt = Number(st.updated_at ?? 0) * 1000
    if (!ACTIVE.has(phase) && !NEEDS.has(phase) && now - updatedAt > KEEP_CLOSED_MS) continue
    let recent: string[] = []
    if (ACTIVE.has(phase) || NEEDS.has(phase)) {
      const text = await $.fs.read(`${base}/events.jsonl`).catch(() => '')
      recent = parseLines(text).map(eventLine).filter(Boolean).slice(-4) as string[]
    }
    list.push({
      name: sp.name, phase, round: Number(st.round ?? 1), model: String(sp.model ?? ''),
      label: String(sp.model_label ?? ''),
      tests: st.tests ? (st.tests.ok ? 'pass' : 'fail') : null,
      last: String(st.last ?? ''), createdAt: Number(sp.created_at ?? 0) * 1000, updatedAt,
      tokens: Number(st.tok_in ?? 0) + Number(st.tok_out ?? 0), recent,
    })
  }

  return list.sort((a, b) => a.createdAt - b.createdAt)
}

async function scoutIfDue($: any, now: number): Promise<void> {
  const path = `${mem.root}/scout.json`
  const st = await readJson($, path)
  const done = Number(st.done_at ?? 0) * 1000
  const offered = Number(st.offered_at ?? 0) * 1000
  if (now - done < SCOUT_EVERY_MS || now - offered < SCOUT_RETRY_MS) return
  mem.scoutAsked = true
  await $.fs.write(path, JSON.stringify({ ...st, offered_at: now / 1000 }))
  void $.prompt.submit({ text: SCOUT_NOTE })
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

async function runWb($: any, args: string[]): Promise<string> {
  try {
    const r = await $.process.run([mem.wb, ...args], { timeoutMs: 120_000 })
    return `${r.stdout}${r.stderr}`.trim() || '(no output)'
  } catch (err) {
    return `could not run wb ${args.join(' ')}: ${err}`
  }
}

// The task worth watching: the newest running one, else the newest waiting for Claude.
const focusOf = (list: WbTask[]): WbTask | undefined => {
  const newest = (ts: WbTask[]) => [...ts].sort((a, b) => b.updatedAt - a.updatedAt)[0]
  return newest(list.filter(t => ACTIVE.has(t.phase))) ?? newest(list.filter(t => NEEDS.has(t.phase)))
}

// Re-read the followed task's transcript: while it runs (the agent's session moves between
// status writes), or once when it changed. Off the poll's path: an export takes a moment.
async function refreshLive($: any, list: WbTask[]) {
  const focus = focusOf(list)
  const key = focus ? `${focus.name}@${focus.updatedAt}` : ''
  if (mem.watching || (key === mem.liveKey && !(focus && ACTIVE.has(focus.phase)))) return
  mem.watching = true
  try {
    if (!focus) {
      mem.liveKey = ''
      await update($, live, () => ({ task: '', lines: [] }))
      return
    }
    const r = await $.process.run([mem.wb, 'watch', focus.name, '--json'], { timeoutMs: 30_000 })
    if (r.exitCode !== 0) return
    const lines = parseLines(r.stdout)
      .map(x => ({ s: String(x.s ?? ''), t: String(x.t ?? '') }))
      .slice(-KEEP_LINES)
    mem.liveKey = key
    const prev = await read($, live)
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

// The ears: a `wb events` child for the session's life, respawned if it dies.
async function ears($: any) {
  for (;;) {
    try {
      const child = $.process.spawn({ argv: [mem.wb, 'events'] })
      let buf = ''
      for await (const { stream, text } of child) {
        if (stream !== 'stdout') continue
        buf += text
        let cut
        while ((cut = buf.indexOf('\n')) >= 0) {
          const line = buf.slice(0, cut)
          buf = buf.slice(cut + 1)
          let ev: Row
          try { ev = JSON.parse(line) } catch { continue }
          if (ev.hello) mem.eventsPid = Number(ev.hello)
          void poll($)
        }
      }
    } catch {
      // the child could not start or its stream closed: start another below
    }
    mem.eventsPid = 0
    try {
      await $.clock.sleep(2000)
    } catch {
      return // no clock to wait on (a test): the timer's safety poll stands in
    }
  }
}

async function poll($: any) {
  if (!mem.root) return
  if (mem.polling) { // a slow file system must not stack polls, but no change may be missed
    mem.again = true
    return
  }
  mem.polling = true
  mem.again = false
  mem.lastPoll = await $.clock.now()
  try {
    const now = await $.clock.now()
    const list = await scan($, mem.root, now)
    const drawn = JSON.stringify(list)
    if (drawn !== mem.lastDrawn) { // write only on change: every write redraws the pane
      mem.lastDrawn = drawn
      await update($, tasks, () => list)
    }
    const on = (await read($, chat)).delegating
    const live = list.filter(t => ACTIVE.has(t.phase) && t.phase !== 'asking')
    mem.hasActive = list.some(t => ACTIVE.has(t.phase))
    const asking = list.filter(t => t.phase === 'asking')
    const waiting = list.filter(t => NEEDS.has(t.phase) && t.phase !== 'asking')
    const parts = [
      ...live.map(t => `${t.name} ${t.phase} r${t.round}`),
      ...asking.map(t => `${t.name} asks`),
      ...(waiting.length ? [`${waiting.length} awaiting review`] : []),
    ]
    $.ui.status(parts.length ? `⚒ wb · ${parts.join(' · ')}` : on ? '⚒ wb · delegate mode' : undefined)
    if ((live.length || on) && !mem.paneOpened) { // open once per load, in delegate mode or when work starts
      mem.paneOpened = true
      void $.ui.open({ id: PANE, title: 'Workbench' })
    }
    void refreshLive($, list)
    if (!mem.busy && mem.queued.length) void $.prompt.submit({ text: mem.queued.shift()! })
    const open = new Set(list.filter(t => ACTIVE.has(t.phase) || NEEDS.has(t.phase))
      .map(t => t.name))
    if ([...mem.open].some(name => !open.has(name))) mem.boundary = true
    mem.open = open
    if (mem.boundary && !mem.busy && !list.some(t => NEEDS.has(t.phase))) {
      mem.boundary = false
      await compactAtBoundary($)
    }
    if (mem.sawTurn && !mem.busy && !mem.scoutAsked && open.size === 0) await scoutIfDue($, now)
    await deliver($, now)
    await rewake($, now, list)
    await listen($, now)
    if (now - mem.chatUrlAt > 10 * 60_000) { // the link changes only with the token
      mem.chatUrlAt = now
      const out = await runWb($, ['chat', 'url'])
      const link = /^https?:\/\/\S+#token=\S+$/.test(out) ? out : ''
      if (link !== (await read($, chat)).url) await update($, chat, c => ({ ...c, url: link }))
    }
  } finally {
    mem.polling = false
  }
  if (mem.again) void poll($)
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
  const body = JSON.stringify({ session: st.key, cwd: await $.session.cwd(), pid: mem.eventsPid, busy: mem.busy })
  if (body !== mem.listenerBody || theirs || claim.session !== st.key) { // only on change: a write is an event
    mem.listenerBody = body
    await $.fs.write(`${dir}/listener.json`, body)
  }
  const rows = (await $.fs.read(`${dir}/messages.jsonl`).catch(() => '')).split('\n')
  const got = new Set((await $.fs.read(`${dir}/receipts.jsonl`).catch(() => '')).split('\n')
    .map(l => { try { return JSON.parse(l).id } catch { return undefined } }))
  for (const line of rows) {
    let m: Row
    try { m = JSON.parse(line) } catch { continue }
    const id = String(m.id ?? '')
    if (m.from !== 'user' || !id || got.has(id) || mem.chatSent.has(id)) continue
    if (now - Number(m.ts ?? 0) * 1000 > CHAT_EXPIRE_MS) continue // stale: the page shows it undelivered
    mem.chatSent.add(id)
    // The terminal shows only `[web chat] <their words>`: how to answer is the skill's to say.
    const text = `${WEB} ${String(m.text ?? '')}`
    // Mid-turn, into the running turn, as a message typed mid-turn would be; else, or if that is
    // refused, as a prompt of its own (which waits for the turn to end).
    let how = 'now'
    if (mem.busy) {
      const r = await $.session.send({ to: { sessionId: await $.session.id() }, text: `${text}\n\n${WEB_NOTE}` })
        .catch(() => ({ isDelivered: false }))
      if (!r.isDelivered) {
        how = 'queued'
        void $.prompt.submit({ text, asUser: true })
      }
    } else void $.prompt.submit({ text, asUser: true })
    await runWb($, ['chat', 'ack', id, '--by', st.key, '--how', how])
    await update($, chat, c => ({ ...c, webAt: now }))
    $.ui.toast('workbench: a message from the web chat')
  }
}

// Notifications nobody has acknowledged yet: one toast each, and a turn for Claude if waking.
async function deliver($: any, now: number) {
  const root = mem.root
  const text = await $.fs.read(`${root}/notify.jsonl`).catch(() => '')
  // While Claude is mid-turn, look every poll (the turn may act on the task) but hold the wake:
  // a prompt submitted now would queue and arrive after the task has moved on.
  if (text.length === mem.lastNotifyLength && !mem.busy) return
  if (!mem.busy) mem.lastNotifyLength = text.length
  const latest = new Map<string, Row>() // one wake per task: its newest notification
  for (const row of parseLines(text)) {
    if (row.ts * 1000 < Math.max(mem.startedAt, now) - OLDEST_WAKE_MS) continue
    latest.set(row.task, row)
  }
  for (const [name, row] of latest) {
    const ackPath = `${root}/tasks/${name}/acked`
    const acked = Number(await $.fs.read(ackPath).catch(() => '0')) || 0
    if (row.ts <= acked) continue
    // Only wake for news still true: a review notice for a task since discarded, accepted or
    // sent back is acknowledged and dropped.
    const status = await readJson($, `${root}/tasks/${name}/status.json`)
    const current = row.kind === 'checkin'
      ? RUNNING.has(status.phase) // a check-in is news while the task still runs
      : status.phase === (row.kind === 'question' ? 'asking' : row.kind)
    const headline = HEADLINE[row.kind] ?? row.kind
    const key = `${name}:${row.ts}`
    if (current && !mem.toasted.has(key)) {
      mem.toasted.add(key)
      $.ui.toast(`workbench: ${name} ${headline}`)
    }
    mem.noticed.set(name, Math.max(mem.noticed.get(name) ?? 0, row.ts))
    if (!mem.wake) continue // the toast only: the notice stays for wb wait
    if (mem.busy && current) continue // deliver once the turn ends
    await $.fs.write(ackPath, String(row.ts)) // acknowledge before waking: never wake twice
    if (!current) continue
    const next = (NEXT[row.kind] ?? NEXT.failed)(name)
    void $.prompt.submit({
      text: `[workbench] Task \`${name}\` ${headline}.\n\n${row.text}\n\n${next}`,
    })
  }
}

// The safety net under deliver: a task waiting on Claude whose news never reached it (a lost or
// swallowed notice; on 2026-11-16 a failure sat unseen for an hour) wakes Claude anyway, once,
// after a minute of Claude being idle. `acked` past the moment the task entered that state
// means Claude has had the news.
const REWAKE_MS = 60_000
const REWAKE_MAX_MS = 12 * 3600_000 // older than this, it waited for a session long gone: wb status shows it
async function rewake($: any, now: number, list: WbTask[]) {
  if (mem.busy || !mem.wake) return
  for (const t of list) {
    if (!NEEDS.has(t.phase) || now - t.updatedAt < REWAKE_MS || now - t.updatedAt > REWAKE_MAX_MS) continue
    const ackPath = `${mem.root}/tasks/${t.name}/acked`
    const acked = Number(await $.fs.read(ackPath).catch(() => '0')) || 0
    // Claude had the news if it acked the task's latest notice: the runner's last status write
    // (its pid cleared) lands just after that notice, so updated_at alone would wake it twice
    const noticed = mem.noticed.get(t.name)
    if (noticed !== undefined ? acked >= noticed : acked * 1000 >= t.updatedAt) continue
    await $.fs.write(ackPath, String(now / 1000))
    const kind = t.phase === 'asking' ? 'question' : t.phase
    void $.prompt.submit({
      text: `[workbench] Task \`${t.name}\` ${HEADLINE[kind] ?? kind}, ${ago(now - t.updatedAt)} ago, ` +
        `and no notice reached Claude. \`wb report ${t.name}\` shows where it is.\n\n` +
        (NEXT[kind] ?? NEXT.failed)(t.name),
    })
  }
}

// Turning delegate mode on or off: the chat listens while it is on. The context block follows
// at the next compaction or /clear, not at once: invalidating it would rewrite the first message
// and cost the whole conversation's cache, and the skill or the quit note says it meanwhile.
// The quit commands close the pane, the mode's sign.
async function delegateMode($: any, on: boolean) {
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

export const register: Register = (on, options) => {
  mem.wake = (options as Row | undefined)?.wake !== 'off'

  on('session.start', async ($, e, next) => {
    const home = await $.env.get('HOME')
    const wbHome = await $.env.get('WB_HOME')
    mem.root = wbHome || `${home}/.local/state/workbench`
    mem.wb = (await $.env.get('WB_BIN')) || 'wb'
    mem.skill = `${(await $.env.get('CLAUDE_CONFIG_DIR')) || `${home}/.claude`}/skills/delegate/SKILL.md`
    mem.startedAt = await $.clock.now()
    if (!(await read($, chat)).key) {
      const key = `${Math.floor(mem.startedAt)}-${Math.random().toString(36).slice(2, 8)}`
      await update($, chat, c => ({ ...c, key }))
    }
    await $.command.register({
      name: 'workbench',
      description: 'Show the tasks delegated to local agents in a pane',
    })
    await $.command.register({
      name: QUIT,
      description: 'Stop every delegated agent now, finishing nothing',
    })
    await $.command.register({
      name: MODELS,
      description: 'Check now which local models suit this machine best (weekly on its own)',
    })
    await $.command.register({
      name: HAND_BACK,
      description: 'Stop the agents and hand their tasks back to Claude to finish',
    })
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

    return next(e)
  })

  // A tool call means a turn is running, even one that began before this module (re)loaded and
  // so never reached turn.start here; turn.complete clears it.
  on('tool.call', async ($, e, next) => {
    if (!mem.busy && !e.agentId) { // a subagent's call doesn't hold the main loop
      mem.busy = true
      void poll($)
    }
    return next(e)
  })

  // Delegating with wb counts as delegate mode too (a session resumed mid-delegation, or one
  // where /delegate ran before this mod could see it)
  on('tool.call', { tool: 'Bash' }, async ($, e, next) => {
    // the commands that hand work out (accept and takeover also close what a hand-back gave Claude)
    if (/(^|[\s;&|(])wb (start|next|feedback|steer|answer)\b/.test(e.command)) {
      await delegateMode($, true)
    }
    return next(e)
  })

  // /delegate turns delegate mode on for the session, with or without a task, and shows the pane
  on('skill.prompt', { skill: 'delegate' }, async ($, e, next) => {
    if (e.path) mem.skill = e.path
    await delegateMode($, true)
    mem.paneOpened = true
    void $.ui.open({ id: PANE, title: 'Workbench' })
    return next(e)
  })

  // Typed in the terminal: the user is back, so answers go here again
  on('prompt.submit', async ($, e, next) => {
    if (!e.isMeta && !e.text.startsWith(WEB) && !e.text.startsWith('[workbench]') && (await read($, chat)).webAt) {
      await update($, chat, c => ({ ...c, webAt: 0 }))
    }
    return next(e)
  })

  on('command.run', { command: QUIT }, async $ => {
    await delegateMode($, false)
    const out = await runWb($, ['stop', '--all'])
    $.ui.toast('workbench: everything delegated is stopped')
    return { text: `Delegation stopped. ${out}`, context: [QUIT_NOTE(out)] }
  })

  on('command.run', { command: MODELS }, async () => {
    mem.queued.push(SCOUT_NOTE) // a command may not start a turn itself: the next poll does
    return { text: 'Claude will check the local models against this machine now.' }
  })

  on('command.run', { command: HAND_BACK }, async $ => {
    await delegateMode($, false)
    const out = await runWb($, ['takeover', '--all'])
    mem.queued.push(DONE_NOTE(out)) // a command may not start a turn itself: the next poll does
    return { text: `Agents stopped; Claude is taking over. ${out}` }
  })

  // Whoever compacts the main conversation, the summary keeps the delegation state.
  on('session.compact', async ($, e, next) => {
    if (e.agentId || !mem.open.size || e.instructions?.includes(KEEP_ON_COMPACT)) return next(e)
    return next({ ...e, instructions: [e.instructions, KEEP_ON_COMPACT].filter(Boolean).join('\n\n') })
  })

  // A fresh or just-compacted conversation is told which tasks are open, from wb itself.
  on('prompt.context', async ($, e, next) => {
    const r = await next(e)
    const st = await read($, chat)
    const away = st.webAt && (await $.clock.now()) - st.webAt < AWAY_MS
    const blocks = [...r.blocks]
    if (st.delegating) blocks.push({ name: 'delegate-mode', text: MODE_NOTE(mem.skill) })
    const notes = st.delegating ? (await $.fs.read(`${mem.root}/notes.md`).catch(() => '')).trim() : ''
    if (notes) blocks.push({ name: 'user-notes', text: NOTES_NOTE(notes) })
    if (away) blocks.push({
      name: 'web-chat',
      text: 'The user is away from the terminal and last wrote from the web chat. Tell them ' +
        'there (`wb chat reply --text "…"`) when something they asked for is done or needs them; ' +
        '`wb chat log` shows the thread. Typing in the terminal again ends this.',
    })
    const status = await runWb($, ['status'])
    if (status.startsWith('no open tasks') || status.startsWith('could not run')) return { blocks }
    return {
      blocks: [...blocks, {
        name: 'workbench',
        text: `Tasks delegated to local agents with wb (the /delegate skill) are open:\n${status}\n` +
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

  on('command.run', { command: 'workbench' }, async $ => {
    // A pane the engine still holds but doesn't show (a tab behind another, or left undrawn)
    // only retitles on open: close it first, so the person's /workbench always brings it up.
    const held = (await $.ui.panes()).find(p => p.id === PANE)
    if (held && (!held.isShown || !held.isPlaced)) await $.ui.close({ id: PANE }).catch(() => {})
    mem.paneOpened = true
    const opened = await $.ui.open({ id: PANE, title: 'Workbench' })
    const list = await read($, tasks)
    const open = list.filter(t => ACTIVE.has(t.phase) || NEEDS.has(t.phase))
    const why = opened.isPlaced ? '' : ` (the pane can't be drawn here: ${opened.reason})`
    return { text: (open.length ? `Workbench: ${open.length} open task(s).` : 'Workbench: no open tasks.') + why }
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
    let from = all.length
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
    const focus = focusOf(list)
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
