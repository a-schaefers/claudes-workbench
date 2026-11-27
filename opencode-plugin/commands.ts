// The user's Workbench commands in OpenCode (#1315): /workbench-quit, /workbench-quit-and-hand-back,
// /workbench-models and /workbench-update, server commands (OpenCode's `command.transform`,
// plugin/src/promise/command.ts), so the TUI, Desktop and the web app all list them. The same
// outcomes as the Claude Code mod's, OpenCode-native (the host-shape decision, #1296): wb does the
// work, run as the session that ran the command (OPENCODE_SESSION_ID in its own directory, which
// `cli._caller` scopes to that session's tasks, #1309), and what the driver must know comes as a
// `synthetic` message, which starts a turn only where the mod's command started one (the hand-back
// and the model check). Workbench mode is the Workbench agent here, so leaving it is switching the
// session to Build. A failure is thrown: OpenCode shows a command's error to the user.
// Nothing runs at import time.
import { execFile } from 'node:child_process'
import { readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { AGENT } from './agent.ts'
import { findWb, wbEnv, wbHome, type Options } from './wb.ts'

export const QUIT = 'workbench-quit'
export const HAND_BACK = 'workbench-quit-and-hand-back'
export const MODELS = 'workbench-models'
export const UPDATE = 'workbench-update'
// the agent a quit leaves the session on: OpenCode's own default primary agent
export const AFTER = 'build'
export const RUN_MS = 120_000         // wb stop, takeover: the agents stop first
export const UPDATE_MS = 600_000      // wb update: a pull and its checks, as the mod's

// wb as one OpenCode session runs it: the options' wb and WB_* paths (wb.ts), its identity and its
// directory, never Claude Code's or an agent's marks
export interface Ran { readonly ok: boolean; readonly out: string }
export type SessionRun = (args: readonly string[], opts: { session?: string; cwd?: string; timeout?: number }) => Promise<Ran>

export function sessionRunner(options: Options, base: Record<string, string | undefined> = process.env): SessionRun {
  return (args, opts) => new Promise(done => {
    const env: Record<string, string> = {}
    for (const [k, v] of Object.entries(wbEnv(options, base))) if (typeof v === 'string') env[k] = v
    for (const k of ['CLAUDECODE', 'CLAUDE_PID', 'CLAUDE_CODE_SESSION_ID', 'WB_TASK', 'OPENCODE_SESSION_ID']) delete env[k]
    if (opts.session) env.OPENCODE_SESSION_ID = opts.session
    const wb = findWb(options, env).wb
    execFile(wb, args, { env, cwd: opts.cwd || undefined, timeout: opts.timeout ?? RUN_MS, maxBuffer: 1 << 20 },
      (err, stdout, stderr) => done({ ok: !err, out: `${String(stdout ?? '')}${err ? String(stderr ?? '') : ''}`.trim() }))
  })
}

// OpenCode 2.4.1's shapes these use (host.d.ts's rule: declared here, each from its source):
// plugin/src/promise/command.ts `CommandInvocation`, `CommandDefinition`, `CommandEditor`
export interface Invocation { readonly sessionID: string; readonly prompt?: { readonly text?: string } }
export interface Command { readonly name: string; readonly description?: string; readonly execute: (input: Invocation) => Promise<void> }
export interface CommandHost {
  readonly options: Options
  readonly command?: { readonly transform?: (callback: (editor: { add(definition: Command): void }) => void) => Promise<{ dispose(): Promise<void> }> }
  readonly session?: {
    // schema/src/session.ts `Session.Info`: its agent (none: the default) and its directory
    readonly get?: (input: { readonly sessionID: string }) => Promise<unknown>
    readonly switchAgent?: (input: { readonly sessionID: string; readonly agent: string }) => Promise<unknown>
    // client `SessionSyntheticInput`: `resume: false` waits for the next wake instead of starting a turn
    readonly synthetic?: (input: { readonly sessionID: string; readonly text: string; readonly description?: string;
      readonly metadata?: Record<string, unknown>; readonly resume?: boolean }) => Promise<unknown>
  }
}

export interface Deps {
  readonly run: SessionRun
  readonly home: string
  readonly now: () => number
  readonly read: (path: string) => string | undefined
  readonly write: (path: string, text: string) => void
}

const SOURCE = 'workbench.command' // the metadata of every message these post

export const QUIT_NOTE = (out: string, ok: boolean) =>
  `[workbench] The user ran /${QUIT}: ` + (ok
    ? `this session's delegated agents were stopped (\`wb stop --all\`):\n`
    : `but \`wb stop --all\` failed, so its agents may still be running (\`wb status\`):\n`) +
  `${out}\nWorkbench mode is off: this session is on the ${AFTER} agent now, so work directly, without ` +
  `delegating. Don't finish, restart or clean up any of the stopped work unless they ask; real tasks keep ` +
  `their worktrees (\`wb status --all\`). Tasks left alone belong to another session. Nothing to do now.`
// a hand-back that took nothing over (#1550): how the user hands the driver one parked task
export const HAND_ONE = 'To hand a parked task over, ask for it by name (`wb takeover <name>`; `wb status --all` lists them).'
export const DONE_NOTE = (out: string) =>
  `[workbench] The user ran /${HAND_BACK}: this session's agents were stopped and their tasks ` +
  `handed to you (\`wb takeover --all\`):\n${out}\n\nWorkbench mode is off (this session is on the ${AFTER} ` +
  `agent now): finish the work yourself, without pausing to ask. For each task taken over: read what the ` +
  `agent left (\`wb diff <name>\`), complete it in its worktree to your own standard, run its acceptance ` +
  `command, then \`wb accept <name> -m "…"\` (add --merge only if the user asked for merging). ` +
  // a best-of hand-back had all three attempts finished and merged (#1576, 2026-11-26)
  `A run's attempts named as alternatives for one change get one accept, the best one's, and a ` +
  `\`wb discard\` each for the rest; a report item has nothing to commit. Tasks left ` +
  `alone belong to another session: don't touch them unless the user asks. Then carry on with the rest of ` +
  `the user's request directly, without delegating, and report briefly at the end.`
// the mod's SCOUT_NOTE, its reference named as the plugin's skill
export const SCOUT_NOTE =
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
  'downloading or deleting (the `workbench-models` skill says how).\n' +
  '4. `wb scout --done --notes "<what you recommended or changed>"`.\n' +
  'Keep it short; if nothing is worth changing, say so in one line.'

interface Where { readonly agent: string; readonly dir: string }

// The session's agent and directory, '' each when OpenCode doesn't say
export async function whereOf(host: CommandHost, sessionID: string): Promise<Where> {
  try {
    const got = (await host.session?.get?.({ sessionID })) as { data?: unknown } | undefined
    const info = ((got && typeof got === 'object' && 'data' in got ? got.data : got) ?? {}) as
      { agent?: unknown; location?: { directory?: unknown } }
    return { agent: typeof info.agent === 'string' ? info.agent : '', dir: typeof info.location?.directory === 'string' ? info.location.directory : '' }
  } catch {
    return { agent: '', dir: '' }
  }
}

// Out of Workbench mode: the session leaves the Workbench agent for Build (only when it is on it)
async function leave(host: CommandHost, sessionID: string, where: Where): Promise<void> {
  if (where.agent !== AGENT) return
  await host.session?.switchAgent?.({ sessionID, agent: AFTER })
}

async function say(host: CommandHost, sessionID: string, text: string, description: string, resume: boolean): Promise<void> {
  if (!host.session?.synthetic) throw new Error(`${description}\n${text}`) // nowhere else to say it
  await host.session.synthetic({ sessionID, text, description, metadata: { source: SOURCE }, resume })
}

export function definitions(host: CommandHost, deps: Deps): Command[] {
  const brief = (out: string) => out.split('\n')[0]?.slice(0, 200) ?? ''
  return [
    {
      name: QUIT,
      description: "workbench: stop this session's agents and leave Workbench mode",
      async execute({ sessionID }) {
        const where = await whereOf(host, sessionID)
        // waiting tasks too, as they stand: a review left at the quit stayed "awaiting review" (#698)
        const { ok, out } = await deps.run(['stop', '--all', '--park-waiting'], { session: sessionID, cwd: where.dir })
        await leave(host, sessionID, where)
        await say(host, sessionID, QUIT_NOTE(out, ok), ok ? `Workbench: delegation stopped. ${brief(out)}` : `Workbench: delegation not stopped: ${brief(out)}`, false)
      },
    },
    {
      name: HAND_BACK,
      description: "workbench: stop this session's agents and hand their tasks to you to finish",
      async execute({ sessionID }) {
        const where = await whereOf(host, sessionID)
        const { ok, out } = await deps.run(['takeover', '--all', '--user'], { session: sessionID, cwd: where.dir })
        // a refusal (the coding gate) still takes over the rest: a turn for those whenever one was
        // taken (#918); nothing handed over on a failure is no turn, and the mode stays
        if (!ok && !/^took over /m.test(out)) throw new Error(`wb takeover --all failed; the agents may still be running (wb status):\n${out}`)
        await leave(host, sessionID, where)
        // exit 0 with none taken over (each one parked by an earlier quit, or none open) is no
        // turn either: DONE_NOTE told the driver to finish tasks still parked (#1550, 2026-11-26)
        if (!/^took over /m.test(out)) return say(host, sessionID, `${out}\n${HAND_ONE}`, `Workbench: nothing handed over. ${brief(out)}`, false)
        await say(host, sessionID, DONE_NOTE(out), `Workbench: agents stopped; taking over. ${brief(out)}`, true)
      },
    },
    {
      name: MODELS,
      description: 'workbench: check the models for each role now, GPU and API',
      async execute({ sessionID }) {
        // raised: the weekly check won't raise it again straight after (the mod's scoutOffered)
        const path = join(deps.home, 'scout.json')
        let st: Record<string, unknown> = {}
        try {
          const doc = JSON.parse(deps.read(path) ?? '{}')
          if (doc && typeof doc === 'object' && !Array.isArray(doc)) st = doc
        } catch { /* unreadable: written anew */ }
        try {
          deps.write(path, JSON.stringify({ ...st, offered_at: deps.now() / 1000 }))
        } catch { /* the check runs all the same */ }
        await say(host, sessionID, SCOUT_NOTE, 'Workbench: the model check', true)
      },
    },
    {
      name: UPDATE,
      description: 'workbench: update workbench (wb update)',
      async execute({ sessionID }) {
        const where = await whereOf(host, sessionID)
        const { ok, out } = await deps.run(['update'], { session: sessionID, cwd: where.dir, timeout: UPDATE_MS })
        if (!ok) throw new Error(`wb update failed: ${out}`)
        // OpenCode reloads the plugin by itself once wb update moved its entry (ocplugin.refresh)
        await say(host, sessionID, `[workbench] The user ran /${UPDATE}: ${out}\nNothing to do now.`, `Workbench: ${brief(out)}`, false)
      },
    },
  ]
}

// The server part's setup calls this: the commands, or nothing on a server without commands
export async function registerCommands(host: CommandHost, deps?: Deps): Promise<(() => Promise<void>) | undefined> {
  const transform = host.command?.transform
  if (!transform) return undefined
  const d: Deps = deps ?? {
    run: sessionRunner(host.options),
    home: wbHome(wbEnv(host.options)),
    now: () => Date.now(),
    read: (path) => { try { return readFileSync(path, 'utf8') } catch { return undefined } },
    write: (path, text) => writeFileSync(path, text),
  }
  const defs = definitions(host, d)
  const reg = await transform((editor) => { for (const def of defs) editor.add(def) })
  return async () => { await reg.dispose() }
}
