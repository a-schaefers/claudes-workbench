// The TUI part's import of a task's agent sessions as child sessions of the session that started
// it (#1331; children.ts has the why). Every CHILDREN_MS it reads the tasks (the panel's
// `workbench-pane` RPC), and for each task an OpenCode session drives (task.json `driver`,
// `session`: the lead) that has stopped (any phase but the active ones) and changed since it last
// looked, it asks the server part for the sessions' messages and imports each one the lead's
// server lacks, or holds fewer messages of (a round of feedback carries on in the same session:
// the old copy is removed first, only one Workbench imported). A lead this server doesn't have (a
// task of another store or machine) is skipped, as is a session of the same id that isn't
// Workbench's (one of a task from before the task's own OpenCode home, v1.91.2, in this store
// already). While a task runs, the panel's tail is the view.
//
// `experimental.session.import` is experimental (the decision's risks): with neither it nor its
// deprecated alias `session.import` on the client, nothing starts, and the panel's tail stays the
// one view of an agent's session (RealOpenCodePlugin's import test fails by name when OpenCode
// drops the route).
import type { TuiContext } from './host.d.ts'
import { WorkbenchPane, type PaneTask, type Tasks } from './pane.ts'
import { childImport, childMessages, markOf, WorkbenchChildren, type ImportInput, type Lead, type Sessions } from './children.ts'
import { ACTIVE } from '../mod/hooks/core.ts'
import { UNWRITTEN } from '../mod/hooks/tasks.ts'

export const CHILDREN_MS = 10_000 // how often the tasks are looked at for a stop

type Info = { readonly id?: string; readonly parentID?: unknown; readonly projectID?: unknown; readonly metadata?: unknown
  readonly location?: { readonly directory?: unknown } }
/** What the import uses of OpenCode's client (client/src/promise/generated/client.ts): `session.get`
 * and `session.remove`, and `experimental.session.import` (or its deprecated `session.import`). */
export interface ChildClient {
  readonly session: {
    get(input: { sessionID: string }): Promise<Info>
    remove?(input: { sessionID: string }): Promise<unknown>
    import?(input: ImportInput): Promise<unknown>
  }
  readonly experimental?: { readonly session?: { import?(input: ImportInput): Promise<unknown> } }
}
export type Importer = (input: ImportInput) => Promise<unknown>

/** The client's import, the experimental name first; undefined when it has neither. */
export function importerOf(client: Partial<ChildClient> | undefined): Importer | undefined {
  const exp = client?.experimental?.session
  if (typeof exp?.import === 'function') return input => exp.import!(input)
  const s = client?.session
  if (typeof s?.import === 'function') return input => s.import!(input)
  return undefined
}

const leadOf = (info: Info | undefined, id: string): Lead | undefined => {
  const dir = info?.location?.directory
  return info && typeof info.projectID === 'string' && typeof dir === 'string' && dir
    ? { id, projectID: info.projectID, location: { directory: dir } } : undefined
}

export const TRIES = 3 // looks at one stop whose sessions wb couldn't give, before the next status write

/** The import's state between looks: what was looked at, by the task's status time. */
export class Children {
  readonly seen = new Map<string, { readonly at: number; tries: number }>()
  private busy = false

  constructor(readonly client: ChildClient, readonly importer: Importer,
    readonly sessions: (task: string) => Promise<Sessions>, readonly now: () => number = Date.now) {}

  /** One look at the tasks: each stopped one of an OpenCode driver's, changed since, imported. */
  async sync(tasks: readonly PaneTask[]): Promise<void> {
    if (this.busy) return
    this.busy = true
    try {
      for (const t of tasks) {
        if (t.driver !== 'opencode' || !t.session) continue
        if (ACTIVE.has(t.phase) || t.phase === UNWRITTEN) { this.seen.delete(t.name); continue }
        const was = this.seen.get(t.name)
        const seen = was?.at === t.updatedAt ? was : { at: t.updatedAt, tries: 0 }
        if (seen.tries >= TRIES) continue
        this.seen.set(t.name, seen)
        seen.tries = (await this.task(t).catch(() => true)) ? seen.tries + 1 : TRIES
      }
      for (const name of this.seen.keys()) if (!tasks.some(t => t.name === name)) this.seen.delete(name)
    } finally {
      this.busy = false
    }
  }

  // whether to look again: wb couldn't give the sessions, or some of them (a busy store, wb itself)
  private async task(t: PaneTask): Promise<boolean> {
    const lead = leadOf(await this.client.session.get({ sessionID: t.session }).catch(() => undefined), t.session)
    if (!lead) return false // not this server's session: its own TUI imports
    const got = await this.sessions(t.name)
    if (got.why) return true
    const n = got.sessions.length
    let again = (got.missing?.length ?? 0) > 0
    for (const [k, s] of got.sessions.entries()) {
      const count = childMessages(s.messages).length
      if (!count) continue
      const had = await this.client.session.get({ sessionID: s.session }).catch(() => undefined)
      if (had) {
        const mark = markOf(had, lead.id, t.name)
        if (!mark || mark.messages >= count || !this.client.session.remove) continue // not ours, or as new
        await this.client.session.remove({ sessionID: s.session })
      }
      // a failure is most often another TUI's import of the same session (a conflict: there
      // already); after a removal, the next look sees whether it is there
      await this.importer(childImport(lead, t.name, s, k, n, this.now())).catch(() => { again ||= !!had })
    }
    return again
  }
}

/** The TUI part's piece. Returns its cleanup; nothing runs where the client can't import. */
export function childrenTui(ctx: TuiContext): () => void {
  const client = (ctx as unknown as { client: ChildClient & TuiContext['client'] }).client
  const importer = importerOf(client)
  if (!importer || typeof client?.session?.get !== 'function') return () => {}
  const children = new Children(client, importer,
    task => client.rpc(WorkbenchChildren).sessions!({ task }) as Promise<Sessions>)
  let stopped = false
  let timer: ReturnType<typeof setTimeout> | undefined
  const look = async () => {
    try {
      const got = await (client.rpc(WorkbenchPane).tasks!({}) as Promise<Tasks>)
      if (!stopped) await children.sync(got.tasks ?? [])
    } catch { /* no server part, or wb failed: the next look */ }
    if (!stopped) timer = setTimeout(look, CHILDREN_MS)
  }
  timer = setTimeout(look, 0)
  return () => { stopped = true; if (timer) clearTimeout(timer) }
}
