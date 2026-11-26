// The OpenCode driver's own tool calls in a task's worktree, kept for wb as Claude Code's are
// (#1331; the host-shape decision on #1296: `tool.hook("execute.after")` writes the same
// `claude-calls/`, no transcript parsing). One row a call in rules/<task>/claude-calls/
// (runner.CLAUDE_CALLS): an edit, a write or a patch of a file there, or a shell command that runs in
// a task's directory or names it, written as the call begins (`end` null: running) and again once it
// is over, with `end`, or `denied` when a permission refused it. wb accept and wb next tell by them
// what the driver wrote by hand in the worktree from what a build or test command it ran there
// wrote (#1211, #1272), as they do for the mod's rows. The rows carry OpenCode's tool names (`edit`,
// `write`, `patch` with `paths`, `shell`), which runner.claude_calls reads beside Claude Code's.
// The mapping is the mod's (register.tsx: callRows, tasksNamed, callFile, noteCall), its rules the
// same. Nothing runs at import.
import { existsSync, mkdirSync, realpathSync, renameSync, writeFileSync } from 'node:fs'
import { isAbsolute, join, resolve } from 'node:path'
import type { ServerContext } from './host.d.ts'
import { patchFiles, places, taskOfPath, type Env } from './gates.ts'
import { dirOfSession } from './tools.ts'
import { wbEnv } from './wb.ts'

export type Row = Record<string, unknown>

const inside = (path: string, dir: string) => path === dir || path.startsWith(dir.endsWith('/') ? dir : `${dir}/`)

// The tasks a shell command runs in (``cwd`` inside tasks/<t>/) or names the directory of
// (tasks/<t>/ under one of ``roots``, as written, through `~` or $HOME, or $WB_HOME): register.tsx's
// tasksNamed, the same rule
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
    for (const m of command.matchAll(new RegExp(`${esc(w)}/tasks/([a-z0-9][a-z0-9-]*)(?=$|[/\\s'"\`;&|)])`, 'g'))) out.add(m[1]!)
  }
  return [...out]
}

export interface Kept { readonly tasks: string[]; readonly row: Row }

// The driver's call as a row and the tasks it is for (with a task.json), or no tasks for any other
// call. ``dir`` is the session's directory, where a relative path or a shell's default cwd is.
export function callRows(tool: string, input: Row, dir: string, home: string, user: string,
                         real: (p: string) => string, exists: (p: string) => boolean): Kept {
  const none: Kept = { tasks: [], row: {} }
  const at = (p: string) => isAbsolute(p) ? p : resolve(dir, p)
  const taskOf = (p: string) => taskOfPath(p, home) || taskOfPath(real(p), real(home))
  let tasks: string[]
  let row: Row
  if (tool === 'edit' || tool === 'write') {
    const path = typeof input.path === 'string' ? at(input.path) : ''
    if (!path) return none
    const t = taskOf(path)
    tasks = t ? [t] : []
    row = { tool, path }
  } else if (tool === 'patch') {
    const paths = patchFiles(String(input.patchText ?? '')).map(at)
    tasks = [...new Set(paths.map(taskOf).filter(Boolean))]
    row = { tool, paths }
  } else if (tool === 'shell' && typeof input.command === 'string') {
    const cwd = typeof input.workdir === 'string' && input.workdir ? at(input.workdir) : dir
    if (!input.command.includes('/tasks/') && !cwd.includes('/tasks/')) return none // most calls
    tasks = tasksNamed(input.command, cwd, [home, real(home)], [user])
    row = { tool, command: input.command, cwd }
  } else return none
  return { tasks: tasks.filter(t => exists(join(home, 'tasks', t, 'task.json'))), row }
}

// The file of a call's row: one file a call (a row in a shared file would be lost to a parallel
// call's write), named by when it began (ms) and OpenCode's call id
export function callFile(at: number, id: string): string {
  return `${Math.round(at)}-${id.replace(/[^A-Za-z0-9_-]/g, '') || Math.random().toString(36).slice(2)}.json`
}

// The row's text: begun (`end` null), over (`end` now), or denied (it never ran)
export function rowText(row: Row, at: number, how: 'begun' | 'over' | 'denied', now: number): string {
  const end = how === 'begun' ? null : now / 1000
  return JSON.stringify({ ...row, at: at / 1000, end, ...(how === 'denied' ? { denied: true } : {}) })
}

// A call a permission refused (OpenCode's Permission.BlockedError or a decline, under the tool's
// own error), so it never ran
export function denied(error: unknown): boolean {
  const e = error as { _tag?: unknown; error?: { _tag?: unknown } } | undefined
  const tag = String(e?.error?._tag ?? e?._tag ?? '')
  return tag.startsWith('Permission.')
}

interface CallsContext {
  readonly options: ServerContext['options']
  readonly location?: { readonly directory?: string }
  readonly session: { get(input: { sessionID: string }): Promise<{ location?: { directory?: string }; subpath?: string }> }
  readonly tool: {
    hook(name: 'execute.before', cb: (e: { tool: string; sessionID: string; id: string; input: unknown }) => unknown): Promise<unknown>
    hook(name: 'execute.after', cb: (e: { tool: string; sessionID: string; id: string; status: string; error?: unknown }) => unknown): Promise<unknown>
  }
  readonly event?: { subscribe(options?: { types?: string[] }): AsyncIterable<{ type: string; data?: Row }> }
}

// The calls a declined permission ended. OpenCode fires no execute.after for a plain decline
// (core/src/permission.ts turns Permission.DeclinedError into a defect, and tool.ts's executeTool
// triggers execute.after only for a Tool.Error), so the bus says it instead: `permission.asked`
// names the call it asks for (`source.id`, the call id execute.before saw), and a
// `permission.replied` of `reject` for that request ends it denied (the consent review,
// 2026-11-25: a declined edit stayed begun for CALL_RUNS and counted as the driver's hand edit).
export class Declines {
  private readonly asked = new Map<string, string>()
  // the call a permission event ends denied, if any
  event(e: { type: string; data?: Row }): string | undefined {
    const d = e.data ?? {}
    const request = String(d.id ?? d.requestID ?? '')
    if (!request) return undefined
    if (e.type === 'permission.asked') {
      const source = d.source as { type?: unknown; id?: unknown } | undefined
      if (source?.type === 'tool' && typeof source.id === 'string') this.asked.set(request, source.id)
      return undefined
    }
    if (e.type !== 'permission.replied') return undefined
    const call = this.asked.get(request)
    this.asked.delete(request)
    return d.reply === 'reject' ? call : undefined
  }
}

const realOf = (p: string) => {
  try { return realpathSync(p) } catch { return p }
}

function write(path: string, text: string) {
  try {
    mkdirSync(path.slice(0, path.lastIndexOf('/')), { recursive: true })
    const tmp = `${path}.${Math.random().toString(36).slice(2)}.tmp`
    writeFileSync(tmp, text)
    renameSync(tmp, path)
  } catch { /* a row lost is a call wb doesn't know of, never a failed call */ }
}

// The server part's call rows: execute.before writes each one begun, execute.after over or denied
export async function calls(ctx: ServerContext): Promise<(() => Promise<void>) | undefined> {
  const c = ctx as unknown as Partial<CallsContext>
  if (!c.tool || !c.session) return undefined
  const sessions = c.session
  const running = new Map<string, { tasks: string[]; row: Row; at: number; name: string; home: string }>()
  const dirOf = async (sessionID: string) => {
    try {
      const got = dirOfSession(await sessions.get({ sessionID }))
      if (got) return got
    } catch { /* the plugin's location */ }
    return c.location?.directory ?? process.cwd()
  }
  const note = (k: { tasks: string[]; row: Row; at: number; name: string; home: string }, how: 'begun' | 'over' | 'denied') => {
    const text = rowText(k.row, k.at, how, Date.now())
    for (const t of k.tasks) write(join(k.home, 'rules', t, 'claude-calls', k.name), text)
  }
  await c.tool.hook('execute.before', async e => {
    if (e.tool !== 'edit' && e.tool !== 'write' && e.tool !== 'patch' && e.tool !== 'shell') return
    const p = places(wbEnv(ctx.options) as Env, process.cwd())
    const kept = callRows(e.tool, (e.input ?? {}) as Row, await dirOf(e.sessionID), p.home, p.user, realOf, existsSync)
    if (!kept.tasks.length) return
    const at = Date.now()
    const k = { ...kept, at, name: callFile(at, String(e.id ?? '')), home: p.home }
    running.set(String(e.id), k)
    note(k, 'begun')
  })
  await c.tool.hook('execute.after', e => {
    const k = running.get(String(e.id))
    if (!k) return
    running.delete(String(e.id))
    note(k, e.status === 'error' && denied(e.error) ? 'denied' : 'over')
  })
  // a declined call's row ends here, as no execute.after comes for it
  const declines = new Declines()
  const stream = c.event?.subscribe({ types: ['permission.asked', 'permission.replied'] })[Symbol.asyncIterator]()
  let open = true
  if (stream) {
    void (async () => {
      while (open) {
        const next = await stream.next().catch(() => ({ done: true as const, value: undefined }))
        if (next.done) break
        const call = declines.event(next.value)
        const k = call === undefined ? undefined : running.get(call)
        if (!k || call === undefined) continue
        running.delete(call)
        note(k, 'denied')
      }
    })()
  }
  return async () => {
    open = false
    await stream?.return?.(undefined).catch(() => {})
    for (const k of running.values()) note(k, 'over')
    running.clear()
  }
}
