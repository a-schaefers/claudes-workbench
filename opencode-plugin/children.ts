// A task's agent sessions as child sessions of the session that started it, in OpenCode's own
// subagent picker (#1331; the host-shape decision on #1296: "imported child sessions at the
// lead's location, plus the panel's tail"). The server part answers `workbench-children` with each
// of a task's sessions' messages (`wb watch <task> --export`, which exports them in the task's own
// lane); the TUI part (children-tui.ts) imports them through its client's
// `experimental.session.import` once the task stops, and the panel's live tail (pane.ts) is the
// view while it runs, and the fallback where that API is missing.
//
// The membrane builds the child's info, never the export: id, parent, location, permissions,
// metadata. The export's `info` comes from the agent's own database, in the `opencode/` home its
// lane writes (the decision's critic 2), and its location is the task's worktree, which as a
// session's location would make the user's service load that directory's config and plugins and
// run git through a `.git` the agent can rewrite (critic 1). So the child is at the lead's own
// location, under the lead, denied every tool, and only messages come from the export, less any
// that would move a session (`location-switched`). Nothing runs at import time.
import type { Registration, RpcDefinition, RpcHandlers } from './host.d.ts'
import { wbRunner, type Run } from './driver.ts'
import type { Options } from './wb.ts'

type Row = Record<string, unknown>

// a task's name as wb names one (store.NAME_RE): never an option or a path
const NAME = /^[a-z0-9][a-z0-9-]{0,47}$/
// a message type whose import would move the child's location (schema/src/session-message.ts
// `LocationSwitched`): to the worktree, the one place it must never be
export const DROPPED = new Set(['location-switched'])
// every tool, every resource, denied: the session's rules come after the agent's and the last
// match wins (schema/src/session.ts `permissions`), so nothing typed in the child runs anything
export const DENY_ALL = [{ action: '*', resource: '*', effect: 'deny' }] as const
// the child's title: never "wb: <task>", the agent's own, which transcript.find_session looks for
export const titleOf = (task: string, k: number, n: number) =>
  `Workbench agent: ${task}${n > 1 ? ` (session ${k + 1} of ${n})` : ''}`

const OBJ = { type: 'object' } as const
export const WorkbenchChildren = {
  id: 'workbench-children',
  methods: {
    sessions: {
      input: { type: 'object', properties: { task: { type: 'string' } }, required: ['task'], additionalProperties: false },
      output: OBJ,
    },
  },
  events: {},
} as const

/** One of a task's sessions as `wb watch --export` gives it. */
export interface Exported { readonly session: string; readonly current: boolean; readonly messages: readonly unknown[] }
// `missing`: the sessions whose export failed (a busy store), to ask for again
export interface Sessions { readonly task: string; readonly sessions: Exported[]; readonly missing?: string[]; readonly why?: string }

/** `wb watch <task> --export`'s answer, or why there is none. */
export function sessionsOf(task: string, ran: { code: number; stdout: string; stderr: string }): Sessions {
  const why = (ran.stderr || ran.stdout).trim().split('\n').at(-1) || `wb watch ${task} --export failed`
  if (ran.code !== 0) return { task, sessions: [], why }
  try {
    const r = JSON.parse(ran.stdout) as { sessions?: unknown; missing?: unknown }
    const list = Array.isArray(r?.sessions) ? r.sessions : []
    return {
      task,
      missing: Array.isArray(r?.missing) ? r.missing.filter((m): m is string => typeof m === 'string') : [],
      sessions: list.filter((s): s is Row => !!s && typeof s === 'object')
        .filter(s => typeof s.session === 'string' && s.session.startsWith('ses') && Array.isArray(s.messages))
        .map(s => ({ session: s.session as string, current: s.current === true, messages: s.messages as unknown[] })),
    }
  } catch {
    return { task, sessions: [], why }
  }
}

/** The export's messages a child takes: objects with a message id and a type, less DROPPED. */
export function childMessages(messages: readonly unknown[]): Row[] {
  return messages.filter((m): m is Row => !!m && typeof m === 'object' && !Array.isArray(m))
    .filter(m => typeof m.id === 'string' && m.id.startsWith('msg_') && typeof m.type === 'string' && !DROPPED.has(m.type))
}

/** The lead session, as OpenCode's client `session.get` answers (schema/src/session.ts `Info`). */
export interface Lead {
  readonly id: string
  readonly projectID: string
  readonly location: { readonly directory: string }
}

/** What the child's metadata says, so a later look knows it, and how much of it, is Workbench's. */
export interface Mark { readonly task: string; readonly session: string; readonly messages: number }

// client/src/promise/generated/types.ts `ExperimentalSessionImportInput`, the fields set here
export interface ImportInput {
  readonly info: {
    readonly id: string
    readonly parentID: string
    readonly projectID: string
    readonly cost: number
    readonly tokens: { readonly input: number; readonly output: number; readonly reasoning: number; readonly cache: { readonly read: number; readonly write: number } }
    readonly time: { readonly created: number; readonly updated: number }
    readonly title: string
    readonly location: { readonly directory: string }
    readonly metadata: { readonly workbench: Mark }
    readonly permissions: readonly { readonly action: string; readonly resource: string; readonly effect: 'deny' }[]
  }
  readonly messages: Row[]
  readonly location: { readonly directory: string }
}

const createdOf = (m: Row | undefined): number | undefined => {
  const t = (m?.time as { created?: unknown } | undefined)?.created
  return typeof t === 'number' && Number.isFinite(t) && t > 0 ? t : undefined
}

/**
 * The import of one agent session (the `k`th of the task's `n`) as a child of `lead`: the
 * membrane's info, the export's messages (childMessages). The child keeps the agent session's
 * id, so a second import of it is OpenCode's conflict, never a second copy. Cost and tokens stay
 * 0: the agent's spend is wb's to count (its meter, its results), and a child's would read as
 * the driver's.
 */
export function childImport(lead: Lead, task: string, s: Exported, k: number, n: number, now: number): ImportInput {
  const messages = childMessages(s.messages)
  const created = createdOf(messages[0]) ?? now
  const location = { directory: lead.location.directory }
  return {
    info: {
      id: s.session, parentID: lead.id, projectID: lead.projectID, cost: 0,
      tokens: { input: 0, output: 0, reasoning: 0, cache: { read: 0, write: 0 } },
      time: { created, updated: Math.max(created, now) },
      title: titleOf(task, k, n), location,
      metadata: { workbench: { task, session: s.session, messages: messages.length } },
      permissions: DENY_ALL.map(r => ({ ...r })),
    },
    messages,
    location,
  }
}

/** The mark of a session Workbench imported under `lead` for `task`, or undefined for any other. */
export function markOf(info: { parentID?: unknown; metadata?: unknown } | undefined, lead: string, task: string): Mark | undefined {
  const w = (info?.metadata as { workbench?: Partial<Mark> } | undefined)?.workbench
  if (!info || info.parentID !== lead || !w || w.task !== task) return undefined
  return { task, session: String(w.session ?? ''), messages: Number(w.messages ?? 0) || 0 }
}

export interface ChildrenHost {
  readonly options: Options
  readonly rpc: { readonly register: <D extends RpcDefinition>(d: D, h: RpcHandlers<D>) => Promise<Registration> }
}

/** The server part's piece: the `workbench-children` RPC. Returns its cleanup. */
export async function startChildren(host: ChildrenHost, run: Run = wbRunner(host.options)): Promise<() => Promise<void>> {
  const reg = await host.rpc.register(WorkbenchChildren, {
    sessions: async (input) => {
      const task = String((input as { task?: unknown } | undefined)?.task ?? '')
      if (!NAME.test(task)) return { task, sessions: [], why: `${task} isn't a task's name` }
      return sessionsOf(task, await run(['watch', task, '--export']))
    },
  })
  return async () => { await reg.dispose().catch(() => {}) }
}
