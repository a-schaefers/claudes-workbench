// The Workbench procedure's everyday wb commands as typed tools (#1335; the host-shape decision,
// #1296: "tool schemas replace the command recipes"). Each is one `wb` command run as the calling
// session (OPENCODE_SESSION_ID, which wb reads its identity and scope from, #1309) in that
// session's directory, its fields the command's flags, its answer wb's own short report: the
// model fills a schema instead of composing shell text, and nothing it writes is quoted by a shell
// (a spec, feedback or a reason goes on stdin or as one argv word). wb stays the system of record:
// every tool is a wb call, and each says the same as the shell's `wb` (which still works).
//
// In the `workbench` namespace, so the model sees `workbench_<name>`: `status`, `diff`,
// `feedback`, `accept`, `discard`, `wait` here; `start` is consent.ts's (an API `--model` asks the
// user first), typed by `START_INPUT` and `startCall` from here. `codemode: false`: each is a
// direct tool, never one reached only from Code Mode. A tool OpenCode declares needs no permission
// rule: none of these asks one (core/src/tool.ts reads `options.permission` only to hide a denied
// tool).
//
// Nothing runs at import (server.ts's rule): setup registers them (`procedureTools`).
import { spawn } from 'node:child_process'
import type { ServerContext } from './host.d.ts'
import { findWb, NAMESPACE_DESCRIPTION, wbEnv, type Options } from './wb.ts'

export const NAMESPACE = 'workbench'
export const RUN_MS = 120_000          // a call's limit: wb status, diff, feedback, discard
export const ACCEPT_MS = 600_000       // wb accept's: a commit a signing key may wait on a touch for
export const WAIT_MIN = 10             // wb wait's default, in minutes: then the model looks again
export const WAIT_MAX_MIN = 60         // the longest one call blocks its turn
export const STOP_GRACE_MS = 30_000    // a wait's own --timeout ends it first; this ends a hung one

export type Row = Record<string, unknown>
export interface Ran { readonly code: number; readonly out: string; readonly err: string }
export interface RunOpts {
  readonly session: string
  readonly cwd?: string
  readonly input?: string          // stdin: a spec or feedback text (`-` in argv)
  readonly timeout?: number
  readonly signal?: AbortSignal
}
export type Run = (args: readonly string[], opts: RunOpts) => Promise<Ran>

// wb as the session runs it: the options' wb and WB_* paths (wb.ts), the session's identity, never
// Claude Code's or an agent's marks (consent.ts's runner; this one feeds stdin and stops on the
// call's signal)
export function runner(options: Options, base: Record<string, string | undefined> = process.env): Run {
  return (args, opts) => new Promise(done => {
    const env: Record<string, string> = {}
    for (const [k, v] of Object.entries(wbEnv(options, base))) if (typeof v === 'string') env[k] = v
    for (const k of ['CLAUDECODE', 'CLAUDE_PID', 'CLAUDE_CODE_SESSION_ID', 'WB_TASK']) delete env[k]
    env.OPENCODE_SESSION_ID = opts.session
    let out = '', err = '', settled = false
    const finish = (r: Ran) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      opts.signal?.removeEventListener?.('abort', stop)
      done(r)
    }
    const child = spawn(findWb(options, env).wb, [...args], { env, cwd: opts.cwd, stdio: ['pipe', 'pipe', 'pipe'] })
    const stop = () => { child.kill('SIGTERM') }
    const timer = setTimeout(stop, opts.timeout ?? RUN_MS)
    if (opts.signal?.aborted) stop()
    else opts.signal?.addEventListener?.('abort', stop)
    child.stdout.on('data', b => { out += String(b) })
    child.stderr.on('data', b => { err += String(b) })
    child.on('error', e => finish({ code: 127, out, err: String(e) }))
    child.on('close', code => finish({ code: code ?? 1, out, err }))
    // a wb gone before it read stdin: its pipe's error is the exit's to report, never a throw
    ;(child.stdin as unknown as { on?(event: 'error', listener: () => void): void }).on?.('error', () => {})
    child.stdin.end(opts.input ?? '')
  })
}

// --- the schemas: each a wb command's flags, by name
const STR = (description: string) => ({ type: 'string', description })
const BOOL = (description: string) => ({ type: 'boolean', description })
const STRS = (description: string) => ({ type: 'array', items: { type: 'string' }, description })
const TASK = STR("the task's name, as wb start was given it")
const SPEC_GAP = STRS('this round was the spec\'s fault: what it missed, one entry each (--spec-gap)')
const IMAGES = STRS('images the agent should see: PNG, JPEG, GIF or WebP paths, up to 4 (--image)')
const object = (properties: Row, required: string[] = []) =>
  ({ type: 'object', properties, required, additionalProperties: false })

export const START_INPUT = object({
  name: STR('the task\'s kebab-case name'),
  spec: STR('the spec, its whole text (sent on stdin: --spec -)'),
  spec_file: STR('the spec as a file instead of `spec` (--spec <file>)'),
  repo: STR('the repository (or the package in it) the agent works in (--repo; default: this session\'s directory)'),
  role: { type: 'string', enum: ['significant', 'trivial', 'writing'], description: 'the kind of work, whose model the user set (--role)' },
  size: { type: 'string', enum: ['significant', 'trivial'], description: 'the slice and the GPU router\'s size (--size)' },
  test: STR('the acceptance command, run in the worktree after every round (--test)'),
  model: STR('a model id or a fragment of one (--model): a GPU model only when the user names one or the record argues for it; an API model only the one the user named, and they are asked first'),
  skill: STRS('library skills to hand the agent, 0-2 that fit (--skill)'),
  image: IMAGES,
  setup: STR('the command that sets the worktree up before the base check, e.g. npm ci (--setup)'),
  auto_fix: { type: 'integer', minimum: 0, description: 'rounds that send failing tests back without waking you (--auto-fix; default 2 on the GPU, 1 on API)' },
  args: STRS('any other wb start flags, as on its command line (e.g. ["--copy", "node_modules", "--checkin", "10"]); ' +
    'without `name`, the whole command line after `wb start`'),
})  // nothing required: `{args}` alone is the tool's first shape, which the gates' refusals name

// wb start's argv and stdin from the typed input. A value goes as `--flag=value`, one argv word,
// so a value starting with `-` is never read as a flag. An input with `args` alone (no `name`) is
// that argv as it is: the tool's first shape, `{args}` (#1314).
export function startCall(input: unknown): { args: string[]; stdin?: string } {
  const i = (input ?? {}) as Row
  const extra = Array.isArray(i.args) ? i.args.map(String) : []
  const name = typeof i.name === 'string' ? i.name.trim() : ''
  if (!name) return { args: extra }
  const args = [name]
  let stdin: string | undefined
  if (typeof i.spec === 'string' && i.spec.trim()) {
    args.push('--spec', '-')
    stdin = i.spec
  } else if (typeof i.spec_file === 'string' && i.spec_file) args.push(`--spec=${i.spec_file}`)
  for (const key of ['repo', 'role', 'size', 'test', 'model', 'setup'] as const) {
    const v = i[key]
    if (typeof v === 'string' && v) args.push(`--${key}=${v}`)
  }
  for (const key of ['skill', 'image'] as const) {
    const v = i[key]
    if (Array.isArray(v)) for (const x of v) if (String(x)) args.push(`--${key}=${String(x)}`)
  }
  if (typeof i.auto_fix === 'number' && Number.isInteger(i.auto_fix) && i.auto_fix >= 0) args.push(`--auto-fix=${i.auto_fix}`)
  return { args: [...args, ...extra], stdin }
}

// --- each tool's call
const str = (i: Row, k: string): string => (typeof i[k] === 'string' ? (i[k] as string) : '')
const flag = (i: Row, k: string, f: string): string[] => (i[k] === true ? [f] : [])
const many = (i: Row, k: string, f: string): string[] =>
  Array.isArray(i[k]) ? (i[k] as unknown[]).map(String).filter(Boolean).map(v => `${f}=${v}`) : []
const need = (i: Row, k: string, what: string): string => {
  const v = str(i, k).trim()
  if (!v) throw new Error(`workbench: ${what}: no ${k} given`)
  return v
}

export interface Call { readonly args: string[]; readonly stdin?: string; readonly timeout?: number }

export interface Spec {
  readonly name: string
  readonly command: string           // the wb command it runs, for its description and the docs
  readonly description: string
  readonly input: object
  readonly call: (i: Row) => Call
  readonly soft?: number             // an exit wb uses for an answer, not a failure (wait's 2)
}

export const SPECS: readonly Spec[] = [
  {
    name: 'status', command: 'wb status',
    description: 'This session\'s open tasks at a glance (wb status): each one\'s phase, model, round and worktree; ' +
      'with `task`, that one in full (its worktree\'s path among it).',
    input: object({ task: STR('one task (default: every open one of this session)'), all: BOOL('closed ones too (--all)'),
      all_sessions: BOOL('every session\'s, not this one\'s alone (--all-sessions)') }),
    call: i => ({ args: ['status', ...(str(i, 'task') ? [str(i, 'task')] : []), ...flag(i, 'all', '--all'), ...flag(i, 'all_sessions', '--all-sessions')] }),
  },
  {
    name: 'diff', command: 'wb diff',
    description: 'The agent\'s changes against the task\'s base (wb diff): the current step\'s, or every step\'s with `all`. ' +
      'Only when a wake says the diff was too big to include, or after you changed something.',
    input: object({ task: TASK, stat: BOOL('a summary of the files changed (--stat)'),
      all: BOOL('every step, not just the current one (--all)'), paths: STRS('only these paths') }, ['task']),
    // the flags first: argparse takes `--` and the paths only after the name, with no flag between
    call: i => ({ args: ['diff', ...flag(i, 'stat', '--stat'), ...flag(i, 'all', '--all'), '--', need(i, 'task', 'no diff'),
      ...(Array.isArray(i.paths) ? (i.paths as unknown[]).map(String).filter(Boolean) : [])] }),
  },
  {
    name: 'feedback', command: 'wb feedback',
    description: 'Send the agent your review for another round (wb feedback): numbered, concrete changes, each with ' +
      'its file. It returns at once; the task wakes you again when it needs you.',
    input: object({
      task: TASK, text: STR('the feedback, its whole text (sent on stdin: --message -)'),
      fresh: BOOL('start the agent on a fresh session, handed the task and the work so far (--fresh)'),
      keep_context: BOOL('continue the session even if it is getting full (--keep-context)'),
      spec_gap: SPEC_GAP, image: IMAGES,
    }, ['task', 'text']),
    call: i => ({
      args: ['feedback', need(i, 'task', 'not sent'), '--message', '-', ...flag(i, 'fresh', '--fresh'),
        ...flag(i, 'keep_context', '--keep-context'), ...many(i, 'spec_gap', '--spec-gap'), ...many(i, 'image', '--image')],
      stdin: need(i, 'text', 'not sent'),
    }),
  },
  {
    name: 'accept', command: 'wb accept',
    description: 'Approve the task (wb accept): commit its work on its branch, with provenance trailers, and close it; ' +
      '`merge` merges the branch too. Only after your review passed every check.',
    input: object({
      task: TASK, message: STR('the commit\'s subject, a conventional commit subject (-m; none for a report task)'),
      merge: BOOL('merge the branch into the base too (--merge)'),
      merge_as: { type: 'string', enum: ['merge', 'ff', 'squash'], description: 'how the merge lands: a merge commit, fast-forward only, or one squashed commit (--merge-as; implies merge)' },
      include: BOOL('commit it even with many new files, a large new binary, or files git ignores, after reviewing them (--include)'),
      take_claude_md: BOOL('put the agent\'s proposed CLAUDE.md/AGENTS.md over the file before committing (--take-claude-md)'),
      no_sign: BOOL('commit it unsigned, whatever commit.gpgsign says (--no-sign)'),
      spec_gap: STRS('a round this task took was the spec\'s fault: what it missed (--spec-gap)'),
    }, ['task']),
    call: i => ({
      args: ['accept', need(i, 'task', 'not accepted'), ...(str(i, 'message') ? [`--message=${str(i, 'message')}`] : []),
        ...flag(i, 'merge', '--merge'), ...(str(i, 'merge_as') ? [`--merge-as=${str(i, 'merge_as')}`] : []),
        ...flag(i, 'include', '--include'), ...flag(i, 'take_claude_md', '--take-claude-md'),
        ...flag(i, 'no_sign', '--no-sign'), ...many(i, 'spec_gap', '--spec-gap')],
      timeout: ACCEPT_MS,
    }),
  },
  {
    name: 'discard', command: 'wb discard',
    description: 'Drop the task\'s work and close it (wb discard), with the reason.',
    input: object({
      task: TASK, reason: STR('why, in a line (--reason)'), spec_gap: STRS('the spec\'s part in it: what it missed (--spec-gap)'),
      keep_branch: BOOL('keep its branch (--keep-branch)'),
      drop_steps: BOOL('delete the branch even when it holds steps wb next committed (--drop-steps)'),
    }, ['task']),
    call: i => ({
      args: ['discard', need(i, 'task', 'not discarded'), ...(str(i, 'reason') ? [`--reason=${str(i, 'reason')}`] : []),
        ...many(i, 'spec_gap', '--spec-gap'), ...flag(i, 'keep_branch', '--keep-branch'), ...flag(i, 'drop_steps', '--drop-steps')],
    }),
  },
  {
    name: 'wait', command: 'wb wait',
    description: 'Block until the task needs you (wb wait): its review, question, stop or failure, printed once. ' +
      'Only when no wake will come (wakes are off) or the user asks you to wait: otherwise end your turn, and ' +
      `the plugin wakes this session. It returns after \`minutes\` (default ${WAIT_MIN}, at most ${WAIT_MAX_MIN}) with the task still running.`,
    input: object({
      task: STR('the task (default: whichever of this session\'s needs you first)'),
      minutes: { type: 'integer', minimum: 1, maximum: WAIT_MAX_MIN, description: `how long to wait at most (default ${WAIT_MIN})` },
    }),
    call: i => {
      const m = typeof i.minutes === 'number' && i.minutes >= 1 ? Math.min(Math.floor(i.minutes), WAIT_MAX_MIN) : WAIT_MIN
      return { args: ['wait', ...(str(i, 'task') ? [str(i, 'task')] : []), `--timeout=${m * 60}`], timeout: m * 60_000 + STOP_GRACE_MS }
    },
    soft: 2,  // timed out, closed, nothing open: wb's line is the answer
  },
]

export const said = (r: Ran, soft?: number): string =>
  (r.out + (r.code && r.code !== soft || !r.out.trim() ? r.err : '')).trim() || (r.code ? `exit ${r.code}` : 'done')

export interface Deps {
  readonly run: Run
  readonly dir: (session: string) => Promise<string>
}

// The tool definitions OpenCode's ToolEditor takes. A nonzero exit is wb's own words, thrown: the
// call failed (a refusal, a closed task), and the model reads why.
export function procedureTools(d: Deps) {
  const options = { namespace: NAMESPACE, codemode: false }
  return SPECS.map(spec => ({
    name: spec.name,
    description: spec.description,
    input: spec.input,
    options,
    execute: async (input: unknown, ctx: { sessionID: string; signal?: AbortSignal }) => {
      const call = spec.call((input ?? {}) as Row)
      const r = await d.run(call.args, { session: ctx.sessionID, cwd: await d.dir(ctx.sessionID), input: call.stdin,
        timeout: call.timeout, signal: ctx.signal })
      // its exit too: the procedure reads some by number (exit 3 names the fix, accept's 5: not merged)
      if (r.code && r.code !== spec.soft) throw new Error(`${said(r)}\n(exit ${r.code})`)
      return { content: said(r, spec.soft) }
    },
  }))
}

// --- setup: the part of OpenCode's server context this uses (plugin/src/promise/tool.ts,
// session.ts there)
export interface ToolsContext {
  readonly options: ServerContext['options']
  readonly location?: { readonly directory?: string }
  readonly session?: { get(input: { sessionID: string }): Promise<{ location?: { directory?: string }; subpath?: string }> }
  readonly tool?: {
    transform(cb: (editor: { namespace(ns: { name: string; description: string }): void; add(tool: object): void }) => void):
      Promise<{ dispose(): Promise<void> } | unknown>
  }
}

// A session's directory: session.get's `location.directory`, already the whole directory. Its
// `subpath` is that directory relative to the project's (OpenCode sets it so), so joining the two
// doubled a subdirectory (<repo>/pkg/web/pkg/web) and every typed tool failed ENOENT in a session
// opened below the repo's root (#1485, 2026-11-26). consent.ts and calls.ts read it here too.
export const dirOfSession = (s: { location?: { directory?: string } } | undefined): string | undefined =>
  s?.location?.directory || undefined

// A session's directory, read once; the plugin's location without one
export function sessionDirs(c: Pick<ToolsContext, 'location' | 'session'>): (session: string) => Promise<string> {
  const known = new Map<string, string>()
  return async sessionID => {
    const had = known.get(sessionID)
    if (had) return had
    let got = c.location?.directory ?? process.cwd()
    try {
      got = dirOfSession(await c.session?.get({ sessionID })) ?? got
    } catch { /* the plugin's location */ }
    known.set(sessionID, got)
    return got
  }
}

// The server part's setup of the tools; nothing on a server without the tool domain
export async function tools(ctx: ServerContext): Promise<(() => Promise<void>) | undefined> {
  const c = ctx as unknown as ToolsContext
  if (!c.tool?.transform) return undefined
  const d: Deps = { run: runner(ctx.options), dir: sessionDirs(c) }
  const reg = await c.tool.transform(editor => {
    editor.namespace({ name: NAMESPACE, description: NAMESPACE_DESCRIPTION })
    for (const t of procedureTools(d)) editor.add(t)
  })
  return async () => { await (reg as { dispose?: () => Promise<void> } | undefined)?.dispose?.() }
}
