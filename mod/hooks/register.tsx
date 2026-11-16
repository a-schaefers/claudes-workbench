// workbench: the Claude Code side of delegating to local OpenCode agents.
//
// It reads the task files `wb` and its runners write (~/.local/state/workbench, or $WB_HOME) and
// does three things with them: draws a live pane of the tasks, keeps a one-line status, and, when
// a task needs Claude (ready for review, a question, stuck, failed), starts a turn for Claude with
// the task's report in it, so nobody has to poll. Acknowledgements are shared with `wb wait`
// through each task's `acked` file, so a notification wakes Claude once, by whichever got it first.
import { atom, read, update } from 'claude-code'
import type { Register } from 'claude-code'

import type { WbTask } from '../types'

const PANE = 'workbench'
const POLL_MS = 1500
const KEEP_CLOSED_MS = 30 * 60 * 1000 // closed tasks stay in the pane for half an hour
const OLDEST_WAKE_MS = 60 * 60 * 1000 // never wake for a notification older than an hour

// Change the tag when WbTask's fields change: a reload declines what the earlier shape stored.
const tasks = atom({ plugin: 'workbench', key: 'tasks' } as const, [] as WbTask[], {
  shape: 'tasks-1',
})

const ACTIVE = new Set(['queued', 'running', 'testing', 'asking'])
const NEEDS = new Set(['review', 'stuck', 'failed', 'asking'])
const COLOR: Record<string, string> = {
  queued: 'subtle', running: 'claude', testing: 'suggestion', asking: 'warning',
  review: 'success', stuck: 'error', failed: 'error', accepted: 'inactive', discarded: 'inactive',
}

type Row = Record<string, any>

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
}
const NEXT: Record<string, (n: string) => string> = {
  review: n => `Review it as /delegate says: \`wb diff ${n}\`, then \`wb accept ${n} -m "…"\` ` +
    `or \`wb feedback ${n} --message <file>\`.`,
  question: n => `Answer with \`wb answer ${n} --text "…"\`; it is blocked until then.`,
  stuck: n => `Steer it with \`wb feedback ${n} --message <file>\`, or \`wb discard ${n}\`.`,
  failed: n => `Look at \`wb log ${n}\`; then \`wb feedback ${n} --message <file>\` or ` +
    `\`wb discard ${n}\`.`,
}

// The module's own memory: reset by a reload, which runs register and session.start again.
const mem = {
  wake: true,
  root: '',
  startedAt: 0,
  lastDrawn: '',
  lastNotifyLength: -1,
  paneOpened: false,
  polling: false,
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
    await deliver($, now)
  } finally {
    mem.polling = false
  }
}

// Notifications nobody has acknowledged yet: one toast each, and a turn for Claude if waking.
async function deliver($: any, now: number) {
  const root = mem.root
  const text = await $.fs.read(`${root}/notify.jsonl`).catch(() => '')
  if (text.length === mem.lastNotifyLength) return
  mem.lastNotifyLength = text.length
  const latest = new Map<string, Row>() // one wake per task: its newest notification
  for (const row of parseLines(text)) {
    if (row.ts * 1000 < Math.max(mem.startedAt, now) - OLDEST_WAKE_MS) continue
    latest.set(row.task, row)
  }
  for (const [name, row] of latest) {
    const ackPath = `${root}/tasks/${name}/acked`
    const acked = Number(await $.fs.read(ackPath).catch(() => '0')) || 0
    if (row.ts <= acked) continue
    await $.fs.write(ackPath, String(row.ts)) // acknowledge first: never wake twice
    // Only wake for news still true: a review notice for a task since discarded, accepted or
    // sent back is acknowledged and dropped.
    const status = await readJson($, `${root}/tasks/${name}/status.json`)
    if (status.phase !== (row.kind === 'question' ? 'asking' : row.kind)) continue
    const headline = HEADLINE[row.kind] ?? row.kind
    $.ui.toast(`workbench: ${name} ${headline}`)
    if (!mem.wake) continue
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

  on('command.run', { command: 'workbench' }, async $ => {
    await $.ui.open({ id: PANE, title: 'Workbench' })
    const list = await read($, tasks)
    const open = list.filter(t => ACTIVE.has(t.phase) || NEEDS.has(t.phase))

    return { text: open.length ? `Workbench: ${open.length} open task(s).` : 'Workbench: no open tasks.' }
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text } = $.ui.resolve(e)
    const list = await read($, tasks)
    const cols = Math.max(30, Number((e.props as Row)?.bodyColumns ?? e.viewport?.columns ?? 60))
    const now = await $.clock.now()
    if (list.length === 0) {
      return (
        <Box flexDirection="column">
          <Text dimColor>No delegated tasks.</Text>
          <Text dimColor>/delegate hands one to a local model.</Text>
        </Box>
      )
    }

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
            {t.recent.map(line => (
              <Text dimColor>  {clip(line, cols - 2)}</Text>
            ))}
            {t.recent.length === 0 && <Text dimColor>  {clip(t.last, cols - 2)}</Text>}
          </Box>
        ))}
      </Box>
    )
  })
}
