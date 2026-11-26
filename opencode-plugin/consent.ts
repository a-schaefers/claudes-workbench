// The consent gates on OpenCode's path (#1314; the host-shape decision on #1296, 2026-11-25): a
// change only the user may make goes through a typed tool that asks them in a session form, never
// through a permission ask (OpenCode's autoaccept answers those, never a form:
// tui/src/routes/session/index.tsx), and wb, the system of record, checks the yes the form wrote.
//
// - The tools, in the `workbench` namespace (the model sees `workbench_<name>`): `route` (wb route),
//   `start` (wb start), `settings_set` (wb settings set) and `patch_send` (wb patch send). Each runs
//   wb's own dry run first (`wb route --dry-run --json`, `--start`, `wb settings set … --dry-run
//   --json`), as the mod does; only a gated change asks: a GPU pin, a show, a start on a GPU model
//   or a Settings row no role needs runs at once. A send always asks, showing its title and body.
// - The form. A server plugin can't create one (2.4.1's promise Context has no form API), so the
//   server part picks the form's id, is subscribed to `form.replied` and `form.cancelled` from its
//   setup on, and emits the RPC event `ask`; each TUI attached (consent-tui.ts) creates the form
//   in the session with that id (a second TUI's create is a conflict: one form) and calls the RPC
//   method `shown`. No `shown` within SHOWN_MS (no TUI: `opencode run`, the web app alone) fails
//   closed: nothing changes, and the refusal says why. The form's answer is `form.replied`'s, from
//   OpenCode's own bus.
// - The yes. On Allow the plugin writes the approval wb checks (store.take_approval) as
//   `$WB_HOME/<stem>.<session>.json` (route-ok.ses_x.json, send-ok.ses_x.json: store.approval_name),
//   never the mod's shared file, so one driver's yes is never spent by the other's call (#1363),
//   then runs the call as that session (OPENCODE_SESSION_ID: wb reads its identity from it, #1309).
// - The gates (gates.ts) on OpenCode's permission hook: a gated shell command is denied with the
//   tool to use; an edit or a shell write of routes.json or an approval file is denied for every
//   agent; an edit in a task's worktree meets the coding gate, whose "Ask me first" asks with the
//   same form. execute.before refuses an edit of commands.json that lifts a repo's local_only.
//
// Nothing runs at import (server.ts's rule): setup builds it. The kernel (mod/hooks/core.ts, the
// membrane: gateOf, codingGate) is imported at setup and may be missing beside a copy of this
// directory alone: then the shell gate is wb's own refusal, and an edit in a task's worktree under
// "Ask me first" or "Only when I say" is refused, never let through unasked.
import { execFile } from 'node:child_process'
import { existsSync, mkdirSync, readFileSync, realpathSync, renameSync, writeFileSync } from 'node:fs'
import { isAbsolute, join, resolve } from 'node:path'
import type { ServerContext } from './host.d.ts'
import {
  APPROVAL_OWN, NOT_EDITED, ONLY, ROUTES_EDIT, allowances, codingMode, editedText, guarded, localOnlyPatchRefusal,
  localOnlyRefusal, patchFiles, patchLines, places, shellRefusal, taskOfPath, type Classify, type Coding, type Env, type Places,
} from './gates.ts'
import { findWb, NAMESPACE_DESCRIPTION, wbEnv } from './wb.ts'
import { simpleWords } from './words.ts'

export const NAMESPACE = 'workbench'
export const SHOWN_MS = 10_000    // a TUI shows the form this soon, or none is attached: fail closed
export const DRY_MS = 60_000      // a dry run's limit (the mod's)
export const RUN_MS = 120_000     // a wb call's
export const SEND_MS = 600_000    // wb patch send's: a fork, a push and a pull request
export const BODY_LINES = 20      // the body's lines the send's form shows (the mod's)
export const FRESH_S = 120        // what wb takes an approval for (routes.ROUTE_OK_SECS, patches.SEND_OK_SECS)
export const SESSION_RE = /^[\w-]{1,128}$/  // store.SESSION_RE: an id wb takes, and a file name
export const UPSTREAM = 'a-schaefers/workbench'

export type Row = Record<string, unknown>

// --- the RPC contract between the server part and the TUIs (JSON Schema; data only)
const STR = { type: 'string' } as const
export const Consent = {
  id: 'workbench-consent',
  methods: {
    // a TUI made the form (or found it made): the server part waits for its answer from here
    shown: {
      input: { type: 'object', properties: { formID: STR }, required: ['formID'], additionalProperties: false },
      output: { type: 'object', properties: { ok: { type: 'boolean' } }, required: ['ok'], additionalProperties: false },
    },
  },
  events: {
    ask: {
      schema: {
        type: 'object',
        properties: { sessionID: STR, formID: STR, title: STR, question: STR, yes: STR, no: STR },
        required: ['sessionID', 'formID', 'title', 'question', 'yes', 'no'],
        additionalProperties: false,
      },
    },
    // the ask is over unanswered (no TUI in time, the call stopped): a late TUI's form goes
    close: {
      schema: {
        type: 'object', properties: { sessionID: STR, formID: STR, message: STR },
        required: ['sessionID', 'formID', 'message'], additionalProperties: false,
      },
    },
  },
} as const

export interface Ask {
  readonly sessionID: string
  readonly formID: string
  readonly title: string
  readonly question: string
  readonly yes: string
  readonly no: string
}

// The form's one field: the question (its description, as OpenCode's question tool puts one), and
// the two answers as options, no free text
export const ANSWER = 'answer'
export function formFields(a: Pick<Ask, 'question' | 'yes' | 'no'>) {
  return [{
    key: ANSWER, type: 'string' as const, description: a.question, required: true, custom: false,
    options: [{ value: 'yes', label: a.yes }, { value: 'no', label: a.no }],
  }]
}

// --- the part of OpenCode's server plugin context this uses (plugin/src/promise/plugin.ts
// `Context`: rpc.ts, event.ts, tool.ts, permission.ts, session.ts there)
export interface OcEvent { readonly type: string; readonly data?: Row }
export interface ConsentContext {
  readonly options: ServerContext['options']
  readonly location?: { readonly directory?: string }
  readonly rpc: {
    register(definition: typeof Consent, handlers: { shown: (input: unknown) => Promise<unknown> }):
      Promise<{ dispose(): Promise<void>; events: { emit(name: string, data: Row): Promise<void> } }>
  }
  readonly event: { subscribe(options?: { types?: string[] }): AsyncIterable<OcEvent> }
  readonly session: { get(input: { sessionID: string }): Promise<{ location?: { directory?: string }; subpath?: string }> }
  readonly tool: {
    transform(cb: (editor: ToolEditor) => void): Promise<unknown>
    hook(name: 'execute.before', cb: (e: { tool: string; sessionID: string; input: unknown }) => unknown): Promise<unknown>
  }
  readonly permission: { hook(name: 'evaluate', cb: (e: Evaluation) => unknown): Promise<unknown> }
}
export interface ToolEditor {
  namespace(ns: { name: string; description: string }): void
  add(tool: {
    name: string; description: string; input: object; options?: Row
    execute(input: unknown, context: { sessionID: string; signal?: AbortSignal }): Promise<{ content: string }>
  }): void
}
// plugin/src/promise/permission.ts `PermissionEvaluation`: `effect` and `message` are the hook's to set
export interface Evaluation {
  readonly sessionID: string
  readonly action: string
  readonly resources: ReadonlyArray<string>
  readonly metadata?: Row
  effect: string
  message?: string
}

// --- asking: one form per gated call
export type Answer = 'yes' | 'no' | 'none'
type Waiter = { shown: (ok: boolean) => void; answer: (a: Answer) => void; sessionID: string }

export class Asker {
  private waiting = new Map<string, Waiter>()
  constructor(private emit: (name: 'ask' | 'close', data: Row) => Promise<void>, private shownMs = SHOWN_MS) {}

  // the TUI's `shown`
  shown(formID: string): boolean {
    const w = this.waiting.get(formID)
    w?.shown(true)
    return !!w
  }

  // form.replied / form.cancelled, from OpenCode's bus
  event(ev: OcEvent): void {
    const id = String(ev.data?.id ?? '')
    const w = this.waiting.get(id)
    if (!w || String(ev.data?.sessionID ?? '') !== w.sessionID) return
    if (ev.type === 'form.replied') {
      const answer = (ev.data?.answer as Row | undefined)?.[ANSWER]
      w.answer(answer === 'yes' ? 'yes' : 'no')
    } else if (ev.type === 'form.cancelled') w.answer('no')
  }

  // The user's answer in a form of the session: `none` when no TUI showed it in time (nothing
  // asked them: the caller fails closed), `no` on their no, a dismissal or the call stopped
  async ask(sessionID: string, q: { title: string; question: string; yes: string; no: string },
            signal?: AbortSignal): Promise<Answer> {
    const formID = `frm_workbench${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`
    let shown!: (ok: boolean) => void, answer!: (a: Answer) => void
    const isShown = new Promise<boolean>(r => { shown = r })
    const answered = new Promise<Answer>(r => { answer = r })
    this.waiting.set(formID, { shown, answer, sessionID })
    const close = (message: string) => this.emit('close', { sessionID, formID, message }).catch(() => {})
    const stop = new Promise<'stopped'>(r => {
      if (signal?.aborted) r('stopped')
      signal?.addEventListener?.('abort', () => r('stopped'))
    })
    try {
      try {
        await this.emit('ask', { sessionID, formID, ...q })
      } catch {
        return 'none'
      }
      let timer: unknown
      const late = new Promise<boolean>(r => { timer = setTimeout(() => r(false), this.shownMs) })
      const got = await Promise.race([isShown, late, stop])
      clearTimeout(timer)
      if (got !== true) {
        await close(got === 'stopped' ? 'the call was stopped' : 'no answer: nothing was changed')
        return got === 'stopped' ? 'no' : 'none'
      }
      const a = await Promise.race([answered, stop])
      if (a === 'stopped') {
        await close('the call was stopped')
        return 'no'
      }
      return a
    } finally {
      this.waiting.delete(formID)
    }
  }
}

// --- running wb, as the session that asked (OPENCODE_SESSION_ID), in its directory
export interface Ran { readonly code: number; readonly out: string; readonly err: string }
export type Run = (args: string[], opts: { session: string; cwd?: string; timeout?: number }) => Promise<Ran>

export function runner(options: ServerContext['options'], base: Env = process.env): Run {
  return (args, opts) => new Promise(done => {
    const env: Record<string, string> = {}
    for (const [k, v] of Object.entries(wbEnv(options, base))) if (typeof v === 'string') env[k] = v
    // never Claude Code's, nor an agent's: the call is this session's, and wb gates it as its
    for (const k of ['CLAUDECODE', 'CLAUDE_PID', 'CLAUDE_CODE_SESSION_ID', 'WB_TASK']) delete env[k]
    env.OPENCODE_SESSION_ID = opts.session
    const child = execFile(findWb(options, env).wb, args,
      { env, cwd: opts.cwd, timeout: opts.timeout ?? RUN_MS, maxBuffer: 1 << 22 },
      (e, stdout, stderr) => done({
        code: e ? (typeof e.code === 'number' ? e.code : 1) : 0, out: String(stdout ?? ''),
        err: String(stderr ?? '') || (e && typeof e.code !== 'number' ? String(e.message) : ''),
      }))
    child.stdin?.end()
  })
}

// The approval wb takes for this session's call (store.approval_name: `<stem>.<session>.json`),
// written whole (a temporary file renamed over it), `at` now
export function writeApproval(home: string, name: 'route-ok.json' | 'send-ok.json', session: string,
                              approval: Row, now = Date.now()): string {
  if (!SESSION_RE.test(session)) throw new Error(`workbench: not changed: ${session} is no session id wb takes`)
  const file = join(home, name.replace(/\.json$/, `.${session}.json`))
  mkdirSync(home, { recursive: true })
  const tmp = `${file}.${Math.random().toString(36).slice(2)}.tmp`
  writeFileSync(tmp, JSON.stringify({ ...approval, at: now / 1000 }))
  renameSync(tmp, file)
  return file
}

// --- the words the model reads after a form
export const NOT_CHANGED = 'workbench: not changed: the user did not approve it in the form. Tell them it was ' +
  'not changed; ask again only if they ask for it.'
export const NOT_SENT = 'workbench: not sent: the user did not approve it in the form. Tell them it was not ' +
  'sent; try again only if they ask for it.'
export const NO_FORM = (what: string, command: string) => `workbench: ${what}: no OpenCode window is open on ` +
  'this session to ask the user in (the form shows in the terminal UI), so nothing was asked and nothing ' +
  `changed. Tell them; once one is open, ask again. Or the user runs it in a terminal of their own: ${command}`
export const CANT_SHOW = (why: string) => `workbench: not sent: the form can't show what it sends (${why}). ` +
  'Name the body file and the title as they are, so the user sees both.'

const quoted = (w: string) => /^[\w@%+=:,./-]+$/.test(w) ? w : `'${w.replace(/'/g, `'\\''`)}'`
const line = (args: ReadonlyArray<string>) => args.map(quoted).join(' ')
const said = (r: Ran) => (r.out + (r.code ? r.err : '')).trim() || (r.code ? `exit ${r.code}` : 'done')

// wb's answer to the model: its output, or for a refusal an error with it (the call failed)
function answer(r: Ran): { content: string } {
  if (r.code) throw new Error(said(r))
  return { content: said(r) }
}

// a dry run's gated change, or null (a GPU pin, nothing gated, or wb refuses it: the real call says why)
export function gatedChange(r: Ran): { change: string; approval: Row } | null {
  if (r.code) return null
  try {
    const q = JSON.parse(r.out)
    return q?.gate && typeof q.change === 'string' && q.change && q.approval && typeof q.approval === 'object'
      ? { change: q.change, approval: q.approval } : null
  } catch {
    return null
  }
}

export const ROUTE_ASK = { yes: 'Allow', no: "Don't" }
export const SEND_ASK = { yes: 'Send it', no: "Don't send" }

export interface Deps {
  readonly run: Run
  // git in a directory (the send's personal-patch line): its stdout, '' when it fails
  readonly git: (args: string[], cwd: string) => Promise<string>
  readonly asker: Pick<Asker, 'ask'>
  readonly home: () => string
  readonly dir: (session: string) => Promise<string>
  readonly read: (path: string) => string | undefined
  readonly now?: () => number
}

// One gated wb call: its dry run, the form on a gated change, the approval, the call
async function gated(d: Deps, session: string, signal: AbortSignal | undefined, dry: string[] | null,
                     real: string[], what: string, timeout = RUN_MS): Promise<{ content: string }> {
  const cwd = await d.dir(session)
  const q = dry && gatedChange(await d.run(dry, { session, cwd, timeout: DRY_MS }))
  if (q) {
    const a = await d.asker.ask(session, { title: 'Workbench', question: q.change, ...ROUTE_ASK }, signal)
    if (a === 'none') throw new Error(NO_FORM(what, `wb ${line(real)}`))
    if (a === 'no') throw new Error(NOT_CHANGED)
    writeApproval(d.home(), 'route-ok.json', session, q.approval, d.now?.())
  }
  return answer(await d.run(real, { session, cwd, timeout }))
}

const argsOf = (input: unknown): string[] => {
  const a = (input as Row | undefined)?.args
  return Array.isArray(a) ? a.map(String) : []
}
const ARGS = (what: string) => ({
  type: 'object',
  properties: { args: { type: 'array', items: { type: 'string' }, description: what } },
  required: ['args'],
  additionalProperties: false,
})

export function tools(d: Deps) {
  const opts = { namespace: NAMESPACE, codemode: false }
  return [
    {
      name: 'route',
      description: 'Show or change the roles (wb route): which model each kind of work goes to, the day cap, ' +
        'the Agents switch. A change to the API side asks the user in a form first, and applies only on ' +
        'their yes; a GPU pin or a show runs at once. Change a role only when the user asks.',
      input: ARGS('wb route\'s arguments, as on its command line (none: show the roles)'),
      options: opts,
      execute: async (input: unknown, ctx: { sessionID: string; signal?: AbortSignal }) => {
        const args = argsOf(input)
        return gated(d, ctx.sessionID, ctx.signal, ['route', '--dry-run', '--json', ...args], ['route', ...args],
          'not changed')
      },
    },
    {
      name: 'start',
      description: 'Start a delegated task (wb start). With a --model on an API model it asks the user in a ' +
        'form first and starts only on their yes; otherwise it starts at once.',
      input: ARGS('wb start\'s arguments, as on its command line (the task name first)'),
      options: opts,
      execute: async (input: unknown, ctx: { sessionID: string; signal?: AbortSignal }) => {
        const args = argsOf(input)
        // any abbreviation of --model (wb start's parser takes one), as the kernel's `start` gate
        const dry = args.some(a => a.startsWith('--m')) ? ['route', '--dry-run', '--json', '--start', ...args] : null
        return gated(d, ctx.sessionID, ctx.signal, dry, ['start', ...args], 'not started')
      },
    },
    {
      name: 'settings_set',
      description: 'Set one Settings row (wb settings set <key> <value>). A row that changes a role\'s API side ' +
        'asks the user in a form first; the others apply at once.',
      input: {
        type: 'object',
        properties: { key: { type: 'string' }, value: { type: 'string' } },
        required: ['key', 'value'], additionalProperties: false,
      },
      options: opts,
      execute: async (input: unknown, ctx: { sessionID: string; signal?: AbortSignal }) => {
        const key = String((input as Row)?.key ?? ''), value = String((input as Row)?.value ?? '')
        return gated(d, ctx.sessionID, ctx.signal, ['settings', 'set', key, value, '--dry-run', '--json'],
          ['settings', 'set', key, value], 'not changed')
      },
    },
    {
      name: 'patch_send',
      description: 'Send a patch wb patch upstream prepared to GitHub (wb patch send): a fork, a push and a ' +
        'pull request, public. It shows the user the title and the body and sends only on their yes.',
      input: {
        type: 'object',
        properties: {
          slug: { type: 'string' }, title: { type: 'string' },
          body_file: { type: 'string', description: 'the pull request\'s body, a file filled in from the template' },
          draft: { type: 'boolean' }, anyway: { type: 'boolean' },
        },
        required: ['slug', 'title', 'body_file'], additionalProperties: false,
      },
      options: opts,
      execute: async (input: unknown, ctx: { sessionID: string; signal?: AbortSignal }) =>
        send(d, ctx.sessionID, (input ?? {}) as Row, ctx.signal),
    },
  ]
}

async function send(d: Deps, session: string, input: Row, signal?: AbortSignal): Promise<{ content: string }> {
  const slug = String(input.slug ?? ''), title = String(input.title ?? '')
  const draft = !!input.draft, anyway = !!input.anyway
  const cwd = await d.dir(session)
  const raw = String(input.body_file ?? '')
  if (!slug || !title || !raw) throw new Error(CANT_SHOW(!slug ? 'no slug' : !title ? 'no title' : 'no body file'))
  const file = isAbsolute(raw) ? raw : resolve(cwd, raw)
  const body = d.read(file)
  if (body === undefined) throw new Error(CANT_SHOW(`can't read ${file}`))
  const real = ['patch', 'send', slug, '--title', title, '--body-file', file, ...(draft ? ['--draft'] : []),
    ...(anyway ? ['--anyway'] : [])]
  const lines = body.split('\n')
  const shown = lines.slice(0, BODY_LINES).join('\n') + (lines.length > BODY_LINES ? '\n…' : '')
  const upstream = upstreamOf(d.read(join(d.home(), 'patches.json')))
  const mine = /^[a-z0-9][a-z0-9-]*$/.test(slug) && await personal(d, join(d.home(), 'upstream', slug))
  const question = `The driver wants to send this to GitHub (${upstream}), publicly: wb ${line(real)}\n\n` +
    `${mine ? 'A personal patch: a commit\'s Scope trailer is me.\n\n' : ''}Title: ${title}\n\nBody:\n${shown}\n\nSend it?`
  const a = await d.asker.ask(session, { title: 'Workbench', question, ...SEND_ASK }, signal)
  if (a === 'none') throw new Error(NO_FORM('not sent', `wb ${line(real)}`))
  if (a === 'no') throw new Error(NOT_SENT)
  writeApproval(d.home(), 'send-ok.json', session,
    { slug, title, body_file: file, body, draft, anyway }, d.now?.())
  return answer(await d.run(real, { session, cwd, timeout: SEND_MS }))
}

// patches.json's upstream (wb patch's), else this repository's home
export function upstreamOf(text: string | undefined): string {
  try {
    const up = JSON.parse(text ?? '').upstream
    return typeof up === 'string' && up.trim() ? up.trim() : UPSTREAM
  } catch {
    return UPSTREAM
  }
}

// A personal patch (a commit's `Scope: me` trailer, which wb patch send refused unless --anyway
// before #1322): said in the question, as the mod's `personal` says it, from the worktree's commits
// no remote-tracking ref has; without git's answer, no line
async function personal(d: Deps, wt: string): Promise<boolean> {
  const out = await d.git(['log', '--format=%(trailers:key=Scope,valueonly,separator=%x2C)', 'HEAD', '--not',
    '--remotes', '--'], wt)
  return out.split(/[\n,]/).some(x => x.trim().toLowerCase() === 'me')
}

// --- the gates on OpenCode's permission hook and execute.before

export interface GateDeps {
  readonly places: () => Places
  readonly dir: (session: string) => Promise<string>
  readonly read: (path: string) => string | undefined
  readonly write: (path: string, text: string) => void
  readonly real: (path: string) => string
  readonly classify: Classify | undefined
  readonly coding: Coding | undefined
  readonly dry: (args: ReadonlyArray<string>, cwd: string, session: string) => Promise<boolean>
  readonly asker: Pick<Asker, 'ask'>
  readonly allow: (task: string, session: string) => Promise<boolean>
  readonly now?: () => number
}

export const CODING_ASK = { yes: 'Allow', no: "Don't" }
export const CODING_SAID = 'Allow, in the workbench form'
export const NO_KERNEL = (task: string) => `workbench: not edited: ${task}'s coding gate can't be read here (the ` +
  'plugin was installed without mod/hooks/core.ts beside it), and the user\'s coding choice keeps it closed. ' +
  'Run bin/wb setup --opencode in a Workbench clone, or the user lets it through with wb coding allow.'

export class Gates {
  private asking = new Map<string, Promise<string>>() // one form per task at a time: its answer shared
  constructor(private d: GateDeps) {}

  // OpenCode's permission hook: a deny sets `effect` and the words the model reads
  async evaluate(e: Evaluation): Promise<void> {
    if (e.effect === 'deny') return
    const why = await this.refusal(e).catch(() => '')
    if (why) {
      e.effect = 'deny'
      e.message = why
    }
  }

  async refusal(e: Evaluation): Promise<string> {
    if (e.action === 'shell') {
      // a line OpenCode couldn't scan (metadata.reason) is one text: wb's own refusal guards it
      if (e.metadata?.reason !== undefined || !this.d.classify) return ''
      if (!e.resources.some(r => /\b(?:wb|workbench|route|start|settings|patch|-ok)\b|routes\.json|>|\btee\b|\bcp\b|\bmv\b|\bdd\b|\binstall\b|\bln\b|\brsync\b/.test(r))) return ''
      const dir = await this.d.dir(e.sessionID)
      const commands = e.resources.map(r => simpleWords(r))
      return shellRefusal(commands, dir, this.d.places(), this.d.classify,
        (args, cwd) => this.d.dry(args, cwd, e.sessionID), this.d.real)
    }
    if (e.action !== 'edit') return ''
    const dir = await this.d.dir(e.sessionID)
    const p = this.d.places()
    for (const r of e.resources) {
      const abs = isAbsolute(r) ? r : resolve(dir, r)
      const kind = guarded(abs, p, this.d.real)
      if (kind === 'routes') return ROUTES_EDIT
      if (kind === 'approval') return APPROVAL_OWN(abs.slice(abs.lastIndexOf('/') + 1))
      const why = await this.coding(abs, p, e.sessionID)
      if (why) return why
    }
    return ''
  }

  // The coding gate for an edit of ``abs``: '' to let it through
  async coding(abs: string, p: Places, session: string): Promise<string> {
    const task = taskOfPath(abs, p.home) || taskOfPath(this.d.real(abs), this.d.real(p.home))
    if (!task || !existsTask(this.d.read, p.home, task)) return ''
    const mode = codingMode(this.d.read(join(p.home, 'coding.json')))
    if (mode === 'decide') return ''
    if (!this.d.coding) return NO_KERNEL(task)
    const file = join(p.home, 'tasks', task, 'coding.json')
    const allowed = allowances(this.d.read(file))
    const { verdict, open } = this.d.coding({
      mode, allowed: allowed.map(x => ({ used: !!x.used, when: !!x.when, started: !!x.started })),
    })
    if (verdict === 'start') {
      // a one-task exception (#94) is under way from its first edit: the next hand-back spends it
      allowed[open]!.started = (this.d.now?.() ?? Date.now()) / 1000
      try { this.d.write(file, JSON.stringify({ allowed })) } catch { /* the edit goes on */ }
      return ''
    }
    if (verdict === 'pass') return ''
    if (verdict === 'ask') {
      const key = `${session}\0${task}`
      let pending = this.asking.get(key)
      if (!pending) {
        pending = this.askCoding(task, session).finally(() => this.asking.delete(key))
        this.asking.set(key, pending)
      }
      return pending
    }
    return ONLY(task)
  }

  private async askCoding(task: string, session: string): Promise<string> {
    const a = await this.d.asker.ask(session, { title: 'Workbench', question: `Let the driver edit ${task}'s code now?`, ...CODING_ASK })
    if (a === 'none') return NO_FORM('not edited', `wb coding allow ${task}`)
    if (a === 'no') return NOT_EDITED(task)
    return (await this.d.allow(task, session)) ? '' : NOT_EDITED(task)
  }

  // execute.before: an edit of commands.json that lifts a repo's local_only (the permission hook
  // sees only the path, never the text)
  async before(e: { tool: string; sessionID: string; input: unknown }): Promise<void> {
    if (e.tool !== 'edit' && e.tool !== 'write' && e.tool !== 'patch') return
    const input = (e.input ?? {}) as Row
    const p = this.d.places()
    const file = this.d.real(p.commands)
    const dir = await this.d.dir(e.sessionID)
    const at = (f: string) => this.d.real(isAbsolute(f) ? f : resolve(dir, f))
    const now = this.d.read(file)
    if (now === undefined) return
    let why = ''
    if (e.tool === 'patch') {
      const text = String(input.patchText ?? '')
      if (!patchFiles(text).some(f => at(f) === file)) return
      const deleted = [...text.matchAll(/^\*\*\* Delete File: (.+?)\s*$/gm)].some(m => at(m[1]!) === file)
      if (deleted) why = localOnlyRefusal(now, '')
      else {
        const { removed, added } = patchLines(text, file, at)
        why = localOnlyPatchRefusal(removed, added)
      }
    } else {
      if (at(String(input.path ?? '')) !== file) return
      const next = editedText(e.tool, input, now)
      if (next !== undefined) why = localOnlyRefusal(now, next)
    }
    if (why) throw new Error(why)
  }
}

const existsTask = (read: GateDeps['read'], home: string, task: string) =>
  read(join(home, 'tasks', task, 'task.json')) !== undefined

// --- setup

const readText = (path: string): string | undefined => {
  try { return readFileSync(path, 'utf8') } catch { return undefined }
}
const realOf = (path: string): string => {
  try { return realpathSync(path) } catch { return path }
}
const writeText = (path: string, text: string) => {
  const tmp = `${path}.${Math.random().toString(36).slice(2)}.tmp`
  writeFileSync(tmp, text)
  renameSync(tmp, path)
}

// The kernel's membrane, when it is beside this directory (the Git install's files and a clone
// bring it; a copy of opencode-plugin/ alone doesn't): its gate classifier and coding gate
export async function kernelGates(): Promise<{ classify?: Classify; coding?: Coding }> {
  try {
    const core = await import('../mod/hooks/core.ts') as { gateOf: Classify; codingGate: Coding }
    return { classify: core.gateOf, coding: core.codingGate }
  } catch {
    return {}
  }
}

// The server part's consent gates: the tools, the RPC, the form events and the hooks at once.
// Nothing without the context's rpc, event, session, tool and permission domains (a server too
// old for them, a test's ctx).
export async function consent(ctx: ServerContext): Promise<(() => Promise<void>) | undefined> {
  const c = ctx as unknown as Partial<ConsentContext>
  if (!c.rpc || !c.event || !c.session || !c.tool || !c.permission) return undefined
  const run = runner(ctx.options)
  const env = () => wbEnv(ctx.options) as Env
  const place = () => places(env(), process.cwd())
  const dirs = new Map<string, string>()
  const sessions = c.session
  const dir = async (sessionID: string): Promise<string> => {
    const known = dirs.get(sessionID)
    if (known) return known
    let got = c.location?.directory ?? process.cwd()
    try {
      const s = await sessions.get({ sessionID })
      if (s.location?.directory) got = s.subpath ? join(s.location.directory, s.subpath) : s.location.directory
    } catch { /* the plugin's location */ }
    dirs.set(sessionID, got)
    return got
  }
  let asker: Asker | undefined
  const rpc = await c.rpc.register(Consent, {
    shown: async input => ({ ok: asker?.shown(String((input as Row | undefined)?.formID ?? '')) ?? false }),
  })
  asker = new Asker((name, data) => rpc.events.emit(name, data))
  // subscribed to the answers from setup on, before any ask
  const stream = c.event.subscribe({ types: ['form.replied', 'form.cancelled'] })[Symbol.asyncIterator]()
  let open = true
  void (async () => {
    while (open) {
      const next = await stream.next().catch(() => ({ done: true, value: undefined }))
      if (next.done) break
      asker?.event(next.value as OcEvent)
    }
  })()
  const k = await kernelGates()
  const git = (args: string[], cwd: string) => new Promise<string>(done => {
    const child = execFile('git', ['-C', cwd, ...args], { timeout: 5000, maxBuffer: 1 << 20 },
      (e, stdout) => done(e ? '' : String(stdout ?? '')))
    child.stdin?.end()
  })
  const d: Deps = { run, git, asker, home: () => place().home, dir, read: readText }
  await c.tool.transform(editor => {
    editor.namespace({ name: NAMESPACE, description: NAMESPACE_DESCRIPTION })
    for (const t of tools(d)) editor.add(t)
  })
  const gates = new Gates({
    places: place, dir, read: readText, write: writeText, real: realOf, classify: k.classify, coding: k.coding,
    asker,
    dry: async (args, cwd, session) => {
      const argv = args[0] === '--settings' ? ['settings', 'set', ...args.slice(1), '--dry-run', '--json']
        : ['route', '--dry-run', '--json', ...args]
      return gatedChange(await run(argv, { session, cwd: existsSync(cwd) ? cwd : undefined, timeout: DRY_MS })) !== null
    },
    allow: async (task, session) =>
      (await run(['coding', 'allow', task, `--said=${CODING_SAID}`], { session, timeout: RUN_MS })).code === 0,
  })
  await c.permission.hook('evaluate', e => gates.evaluate(e))
  await c.tool.hook('execute.before', e => gates.before(e))
  return async () => {
    open = false
    await stream.return?.().catch(() => {})
    await rpc.dispose().catch(() => {})
  }
}
