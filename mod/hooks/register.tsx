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
// Two slash commands for the user: /delegate-quit stops every agent at once
// (`wb stop --all`) and tells Claude so; /delegate-quit-and-hand-back-claude stops the agents and
// hands their tasks to Claude (`wb takeover --all`), starting a turn for Claude to finish them.
// Under the task list the pane follows one task live: the conversation between Claude and the
// agent, every tool call and test, as `wb watch --json` prints it.
import { atom, read, update } from 'claude-code'
import type { Register } from 'claude-code'

import type { WbChat, WbLine, WbLive, WbTask } from '../types'

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

// The web chat (`wb chat serve`): the user's side door while away from the terminal. A session
// listens once /delegate has run in it, until /delegate-quit or the hand-back ends delegate mode;
// with several, the first to claim chat/listener.json does, and the others stand by. A message is
// submitted as a prompt (so the terminal shows it too) with a receipt the page shows at once.
const chat = atom({ plugin: 'workbench', key: 'chat' } as const,
  { key: '', delegating: false, webAt: 0, url: '' } as WbChat, { shape: 'chat-2' })
const CHAT_STALE_MS = 15_000 // as wb's LISTENING_FOR: a listener silent this long has gone
const AWAY_MS = 6 * 3600 * 1000 // the user counts as away this long after a web message
const WEB = '[web chat]'
const WEB_NOTE =
  '(Sent by the user from the web chat on their phone, away from the terminal. It carries the ' +
  'same authority as a message typed here. Answer there with `wb chat reply --text "…"`: if the ' +
  'work will take more than a minute or two, first a one-line acknowledgement of what you are ' +
  'doing, then the outcome when it is done (results, what needs them, short; they read it on a ' +
  'phone). The web thread is all they see: read it as its own conversation (`wb chat log`), make ' +
  'each reply make sense without the terminal, and take a yes there only as an answer to a ' +
  'question asked there. Never download, delete or do anything hard to undo on an ambiguous OK. ' +
  'Work as usual otherwise; the terminal keeps the full detail.)'

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
  sys: { color: 'permission' }, add: { color: 'diffAdded' }, del: { color: 'diffRemoved' },
  head: { bold: true }, activity: { color: 'claude' }, clock: { dimColor: true },
}
const SPIN = ['✻', '✳', '✶', '✢', '·', '✢', '✶', '✳']

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

const eventLine = (r: Row): string | null => {
  switch (r.kind) {
    case 'tool': return `${r.tool} ${r.brief ?? ''}${r.ok ? '' : '  ✗'}`.trim()
    case 'tests': return `tests ${r.ok ? 'pass' : 'FAIL'}`
    case 'autofix': return 'tests failed → sent back to the agent'
    case 'ask': return `asks: ${r.text}`
    case 'answer': return `answered: ${r.text}`
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
  question: n => `Answer with \`wb answer ${n} --text "…"\`; it is blocked until then.`,
  stuck: n => `Steer it with \`wb feedback ${n} --message <file>\`, or \`wb discard ${n}\`.`,
  failed: n => `Look at \`wb log ${n}\`; then \`wb feedback ${n} --message <file>\` or ` +
    `\`wb discard ${n}\`.`,
  checkin: n => `As the driver: tell the user in two or three plain lines how it is going ` +
    `(progress, time, anything off), and log the same with \`wb note ${n} --text "…"\`. ` +
    `If it is heading the wrong way, \`wb steer ${n}\` (\`--now\` to interrupt). Keep it short.`,
}
const RUNNING = new Set(['queued', 'running', 'testing'])

const QUIT = 'delegate-quit'
const HAND_BACK = 'delegate-quit-and-hand-back-claude'
const QUIT_NOTE = (out: string) =>
  `[workbench] The user ran /${QUIT}: everything delegated was stopped (\`wb stop --all\`):\n` +
  `${out}\nDon't finish, restart or clean up any of it unless they ask; real tasks keep their ` +
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
  chatSent: new Set<string>(), // web messages submitted by this load, awaiting their receipt
  chatUrlAt: 0, // when the web chat's link was last looked up
  eventsPid: 0, // the `wb events` child: its lines wake poll(); the chat counts it as this session
  again: false, // something changed while a poll ran: run another after it
  hasActive: false, // a task is running: the clock redraws
  lastPoll: 0,
  listenerBody: '', // what this session last wrote to chat/listener.json
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
    const live = list.filter(t => ACTIVE.has(t.phase) && t.phase !== 'asking')
    mem.hasActive = list.some(t => ACTIVE.has(t.phase))
    const asking = list.filter(t => t.phase === 'asking')
    const waiting = list.filter(t => NEEDS.has(t.phase) && t.phase !== 'asking')
    const parts = [
      ...live.map(t => `${t.name} ${t.phase} r${t.round}`),
      ...asking.map(t => `${t.name} asks`),
      ...(waiting.length ? [`${waiting.length} awaiting review`] : []),
    ]
    $.ui.status(parts.length ? `wb · ${parts.join(' · ')}` : undefined)
    if (live.length && !mem.paneOpened) { // open once per load, when work starts
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

async function listen($: any, now: number) {
  const st = await read($, chat)
  if (!st.delegating) return
  const dir = `${mem.root}/chat`
  if (!mem.eventsPid) return // the chat knows a listener by its live `wb events` process
  const claim = await readJson($, `${dir}/listener.json`)
  const theirs = claim.session && claim.session !== st.key
  if (theirs && (claim.pid ? await $.fs.exists(`/proc/${claim.pid}`)
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
    mem.chatSent.add(id)
    const text = `${WEB} ${String(m.text ?? '')}\n\n${WEB_NOTE}`
    // Mid-turn, into the running turn, as a message typed mid-turn would be; else, or if that is
    // refused, as a prompt of its own (which waits for the turn to end).
    let how = 'now'
    if (mem.busy) {
      const r = await $.session.send({ to: { sessionId: await $.session.id() }, text })
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
    if (mem.busy && current && mem.wake) continue // deliver once the turn ends
    await $.fs.write(ackPath, String(row.ts)) // acknowledge before waking: never wake twice
    if (!current || !mem.wake) continue
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
async function rewake($: any, now: number, list: WbTask[]) {
  if (mem.busy || !mem.wake) return
  for (const t of list) {
    if (!NEEDS.has(t.phase) || now - t.updatedAt < REWAKE_MS) continue
    const ackPath = `${mem.root}/tasks/${t.name}/acked`
    const acked = Number(await $.fs.read(ackPath).catch(() => '0')) || 0
    if (acked * 1000 >= t.updatedAt) continue
    await $.fs.write(ackPath, String(now / 1000))
    const kind = t.phase === 'asking' ? 'question' : t.phase
    void $.prompt.submit({
      text: `[workbench] Task \`${t.name}\` ${HEADLINE[kind] ?? kind}, ${ago(now - t.updatedAt)} ago, ` +
        `and no notice reached Claude. \`wb report ${t.name}\` shows where it is.\n\n` +
        (NEXT[kind] ?? NEXT.failed)(t.name),
    })
  }
}

export const register: Register = (on, options) => {
  mem.wake = (options as Row | undefined)?.wake !== 'off'

  on('session.start', async ($, e, next) => {
    const home = await $.env.get('HOME')
    const wbHome = await $.env.get('WB_HOME')
    mem.root = wbHome || `${home}/.local/state/workbench`
    mem.wb = (await $.env.get('WB_BIN')) || 'wb'
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

    return next(e)
  })

  // A tool call means a turn is running, even one that began before this module (re)loaded and
  // so never reached turn.start here; turn.complete clears it.
  on('tool.call', async ($, e, next) => {
    if (!mem.busy) {
      mem.busy = true
      void poll($)
    }
    return next(e)
  })

  // Delegating with wb counts as delegate mode too (a session resumed mid-delegation, or one
  // where /delegate ran before this mod could see it)
  on('tool.call', { tool: 'Bash' }, async ($, e, next) => {
    if (/(^|[\s;&|(])wb (start|next|feedback|accept|steer|answer|takeover)\b/.test(e.command) &&
        !(await read($, chat)).delegating) {
      await update($, chat, c => ({ ...c, delegating: true }))
      void poll($)
    }
    return next(e)
  })

  // /delegate turns the web chat on in this session
  on('skill.prompt', { skill: 'delegate' }, async ($, e, next) => {
    await update($, chat, c => ({ ...c, delegating: true }))
    void poll($) // listen now: a message may already be waiting
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
    await update($, chat, c => ({ ...c, delegating: false }))
    const out = await runWb($, ['stop', '--all'])
    $.ui.toast('workbench: everything delegated is stopped')
    return { text: `Delegation stopped. ${out}`, context: [QUIT_NOTE(out)] }
  })

  on('command.run', { command: MODELS }, async () => {
    mem.queued.push(SCOUT_NOTE) // a command may not start a turn itself: the next poll does
    return { text: 'Claude will check the local models against this machine now.' }
  })

  on('command.run', { command: HAND_BACK }, async $ => {
    await update($, chat, c => ({ ...c, delegating: false }))
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
    const blocks = away ? [...r.blocks, {
      name: 'web-chat',
      text: 'The user is away from the terminal and last wrote from the web chat. Tell them ' +
        'there (`wb chat reply --text "…"`) when something they asked for is done or needs them; ' +
        '`wb chat log` shows the thread. Typing in the terminal again ends this.',
    }] : r.blocks
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
    await $.ui.open({ id: PANE, title: 'Workbench' })
    const list = await read($, tasks)
    const open = list.filter(t => ACTIVE.has(t.phase) || NEEDS.has(t.phase))

    return { text: open.length ? `Workbench: ${open.length} open task(s).` : 'Workbench: no open tasks.' }
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text } = $.ui.resolve(e)
    const list = await read($, tasks)
    const watched = await read($, live)
    const { url } = await read($, chat)
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
    if (list.length === 0) {
      return (
        <Box flexDirection="column">
          <Text dimColor>No delegated tasks.</Text>
          <Text dimColor>/delegate hands one to a local model.</Text>
          {pinned}
        </Box>
      )
    }

    // the followed task's own recent lines are in its transcript below, in full
    const recentOf = (t: WbTask) => (t.name === watched.task ? [] : t.recent)
    const when = (t: WbTask) =>
      ACTIVE.has(t.phase) || NEEDS.has(t.phase) ? `for ${ago(now - t.createdAt)}` : `${ago(now - t.updatedAt)} ago`
    // The whole history is drawn: the engine scrolls the pane (arrows, while focused) and
    // follows the end as lines arrive. The activity row, last, carries the spinner.
    const history = watched.lines.filter(l => !['activity', 'clock', 'meta'].includes(l.s))
    const doing = watched.lines.find(l => l.s === 'activity')
    const clock = watched.lines.find(l => l.s === 'clock')
    const meta = watched.lines.find(l => l.s === 'meta')

    return (
      <Box flexDirection="column">
        {list.map(t => (
          <Box flexDirection="column" marginBottom={1}>
            <Text>
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
            {history.map(l => (
              <Text {...(LINE_STYLE[l.s] ?? {})}>{l.t || ' '}</Text>
            ))}
            {doing && (
              <Text {...LINE_STYLE.activity}>
                {SPIN[Math.floor(now / 500) % SPIN.length]} {doing.t}
              </Text>
            )}
            {clock && <Text dimColor>{clock.t}</Text>}
          </Box>
        )}
        {pinned}
      </Box>
    )
  })
}
