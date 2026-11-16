// workbench: the Claude Code side of delegating to local OpenCode agents.
//
// It reads the task files `wb` and its runners write (~/.local/state/workbench, or $WB_HOME) and
// does three things with them: draws a live pane of the tasks, keeps a one-line status, and, when
// a task needs Claude (ready for review, a question, stuck, failed), starts a turn for Claude with
// the task's report in it, so nobody has to poll. Acknowledgements are shared with `wb wait`
// through each task's `acked` file, so a notification wakes Claude once, by whichever got it first.
// A wake is held while Claude's main loop is mid-turn (a prompt submitted then would queue and
// arrive stale) and checked against the task's phase when it is finally sent.
// Two phrases from the user act at once, before Claude reads them: "bail out claude" stops the
// benchmark and every agent (`wb stop --all`); "just get it done claude" stops the agents and hands
// their tasks to Claude (`wb takeover --all`) to finish directly. The message reaches Claude with
// what was done and what to do next appended.
// Under the task list the pane follows one task live: the conversation between Claude and the
// agent, every tool call and test, as `wb watch --json` prints it.
import { atom, read, update } from 'claude-code'
import type { Register } from 'claude-code'

import type { WbLine, WbLive, WbTask } from '../types'

const PANE = 'workbench'
const POLL_MS = 1500
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

const BAIL_OUT = /\bbail\s+out,?\s+claude\b/i
const GET_IT_DONE = /\bjust\s+get\s+it\s+done,?\s+claude\b/i
const BAIL_NOTE = (out: string) =>
  `[workbench] The user said "bail out claude", so everything delegated was stopped at once ` +
  `(\`wb stop --all\`):\n${out}\n\nDon't finish, restart or clean up anything. Tell the user in ` +
  `a few lines what was running and where it was left (real tasks keep their worktrees; ` +
  `\`wb status --all\`), then wait for their next instruction.`
const DONE_NOTE = (out: string) =>
  `[workbench] The user said "just get it done claude": the agents were stopped and their tasks ` +
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
  toasted: new Set<string>(),
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
    }
  } catch {
    // wb missing or slow: the task list still shows; try again next poll
  } finally {
    mem.watching = false
  }
}

async function poll($: any) {
  if (mem.polling || !mem.root) return // a slow file system must not stack polls
  mem.polling = true
  try {
    const now = await $.clock.now()
    const list = await scan($, mem.root, now)
    const drawn = JSON.stringify(list)
    if (drawn !== mem.lastDrawn) { // write only on change: every write redraws the pane
      mem.lastDrawn = drawn
      await update($, tasks, () => list)
    }
    const live = list.filter(t => ACTIVE.has(t.phase) && t.phase !== 'asking')
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
    await deliver($, now)
  } finally {
    mem.polling = false
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

export const register: Register = (on, options) => {
  mem.wake = (options as Row | undefined)?.wake !== 'off'

  on('session.start', async ($, e, next) => {
    const home = await $.env.get('HOME')
    const wbHome = await $.env.get('WB_HOME')
    mem.root = wbHome || `${home}/.local/state/workbench`
    mem.wb = (await $.env.get('WB_BIN')) || 'wb'
    mem.startedAt = await $.clock.now()
    await $.command.register({
      name: 'workbench',
      description: 'Show the tasks delegated to local agents in a pane',
    })
    $.clock.every(POLL_MS, () => {
      void poll($)
    })

    return next(e)
  })

  on('prompt.submit', async ($, e, next) => {
    const bail = BAIL_OUT.test(e.text)
    if (e.command || e.text.startsWith('[workbench]') || (!bail && !GET_IT_DONE.test(e.text))) {
      return next(e)
    }
    const argv = [mem.wb, bail ? 'stop' : 'takeover', '--all']
    let out: string
    try {
      const r = await $.process.run(argv, { timeoutMs: 120_000 })
      out = `${r.stdout}${r.stderr}`.trim() || '(no output)'
    } catch (err) {
      out = `could not run ${argv.join(' ')}: ${err}`
    }
    $.ui.toast(bail ? 'workbench: bailed out, everything stopped' : 'workbench: Claude is taking over')
    return next({ ...e, text: `${e.text}\n\n${bail ? BAIL_NOTE(out) : DONE_NOTE(out)}` })
  })

  // Only the main loop's turns: a subagent's run doesn't hold the prompt.
  on('turn.start', async ($, e, next) => {
    if (!e.agentId) mem.busy = true
    return next(e)
  })
  on('turn.complete', async ($, e, next) => {
    if (!e.agentId) mem.busy = false
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
    const props = (e.props ?? {}) as Row
    const cols = Math.max(30, Number(props.bodyColumns ?? e.viewport?.columns ?? 60))
    const now = await $.clock.now()
    if (list.length === 0) {
      return (
        <Box flexDirection="column">
          <Text dimColor>No delegated tasks.</Text>
          <Text dimColor>/delegate hands one to a local model.</Text>
        </Box>
      )
    }

    // the followed task's own recent lines are in its transcript below, in full
    const recentOf = (t: WbTask) => (t.name === watched.task ? [] : t.recent)
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
                {' '}r{t.round} · {t.model}
                {t.tests ? ` · tests ${t.tests}` : ''} · {ago(now - t.createdAt)}
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
            <Text dimColor>{clip(`── live: ${watched.task} · ↑↓ scroll · wb watch ${watched.task} in a terminal `
              + '─'.repeat(cols), cols)}</Text>
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
      </Box>
    )
  })
}
