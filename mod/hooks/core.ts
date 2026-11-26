// The membrane (kernel/BOUNDARY.md): the one file that imports ./kernel.js, the PureScript kernel
// compiled by scripts/kernel.py. It maps the kernel's wire records to what TypeScript uses and
// decides nothing. Both hosts reach the kernel only through here: the Claude Code mod, and the
// OpenCode plugin (#1296), so a decision made in the kernel is made the same way on each (#1356).
import * as K from './kernel.js'

// Every export by name, untyped, for the goldens (golden/run.ts calls each case's `fn` on it): only
// tests and golden/ import it (scripts/kernel.py's offline check); the hosts take the typed names below.
export const kernel: Readonly<Record<string, unknown>> = K

// The phase sets, from workbench/store.py through Kernel.Tables (generated: never a hand copy).
export const ACTIVE: ReadonlySet<string> = new Set(K.tables.active)
export const NEEDS: ReadonlySet<string> = new Set(K.tables.needsClaude)
export const PARKS: ReadonlySet<string> = new Set(K.tables.parks)
export const CLOSED: ReadonlySet<string> = new Set(K.tables.closed)
// ask_mcp.NO_ANSWER's opening: a question closed unanswered starts with it
export const NO_ANSWER: string = K.tables.noAnswer
// chat.LISTENING_FOR in ms: a web chat listener older than this is gone
export const CHAT_STALE_MS: number = K.tables.chatStaleMs
// spend.WAKE_OPENING: every wake starts with it, then the task's name in backticks; spend.WAKE
// matches it to charge the turn to the task
export const WAKE_OPENING: string = K.tables.wakeOpening

export type PhaseFacts = Parameters<typeof K.phaseOf>[0]
export type Phase = ReturnType<typeof K.phaseOf>
// What a task's phase means (Kernel.Phase): active, running (queued counts), working (a round
// under way), needs Claude, waits (needs Claude, not parked), stopped, done, closed
export const phaseOf = (facts: PhaseFacts): Phase => K.phaseOf({ phase: String(facts.phase ?? ''), parked: !!facts.parked })
export type Counts = ReturnType<typeof K.openCounts>
// A list of tasks tallied by phase, in store.OPEN_ORDER (Kernel.Phase): the pane's group header and
// the status line count by it
export const openCounts = (tasks: ReadonlyArray<PhaseFacts>): Counts =>
  K.openCounts({ tasks: tasks.map(t => ({ phase: String(t.phase ?? ''), parked: !!t.parked })) })

export type OwnerFacts = Parameters<typeof K.owns>[0]
// Whose task it is (Kernel.Owner): `mine`, and `claims` (it would be, were its session gone);
// cli._scope judges the same. The host gathers the facts: its ids, its directory, `gone`
export const owns = (facts: OwnerFacts): ReturnType<typeof K.owns> => K.owns(facts)

// Whether the user is away from the terminal (Kernel.Away): the web chat used within six hours,
// or one of the session's own tasks open since
export const awayNow = (webAt: number, now: number, tasks: ReadonlyArray<PhaseFacts & { mine: boolean }>): boolean =>
  K.awayNow({ webAt: Number(webAt) || 0, now, tasks: tasks.map(t => ({ phase: String(t.phase ?? ''), parked: !!t.parked, mine: !!t.mine })) })

// Which notices are due a look (Kernel.Notice): per task its questions and newest notice, none
// over an hour old (`picks`: indices into `rows`), and each task's newest notice (`noticed`)
export const noticesDue = (rows: ReadonlyArray<Record<string, unknown>>, startedAt: number, now: number): ReturnType<typeof K.noticesDue> =>
  K.noticesDue({
    rows: rows.map(r => ({ task: String(r.task ?? ''), kind: String(r.kind ?? ''), ts: Number(r.ts) || 0,
      tasks: Array.isArray(r.tasks) ? r.tasks.map(String) : [] })),
    startedAt, now,
  })
// One due notice judged (Kernel.Notice): still true, toasted, and the act: skip, none, steer,
// hold, wake, ack or missed
export const noticePlan = (facts: Parameters<typeof K.noticePlan>[0]): ReturnType<typeof K.noticePlan> => K.noticePlan(facts)
// The rewake check (Kernel.Notice): recent, a candidate now, due, and the kind its phase stands for
export const rewakePlan = (facts: Parameters<typeof K.rewakePlan>[0]): ReturnType<typeof K.rewakePlan> =>
  K.rewakePlan({ ...facts, phase: String(facts.phase ?? ''), parked: !!facts.parked })

export type Said = Parameters<typeof K.tellPlan>[0]['want']
// What to tell a conversation of the rules (Kernel.Mode): at its first message the context
// blocks, midway the rules it lacks, and what it has been told after (`said`)
export const tellPlan = (facts: Parameters<typeof K.tellPlan>[0]): { say: string[]; said: Said } => {
  const r = K.tellPlan(facts)
  return { say: [...r.say], said: { ...r.said } }
}
// The sessions kept in workbench mode (Kernel.Mode), from `$.store`'s `modes` (id → ms): whether
// one of `ids` is among them, and the record to keep (none 30 days old; `on`, the ids at `now`)
export const modesPlan = (modes: unknown, ids: Iterable<string>, now: number, on: boolean): { kept: boolean; modes: Record<string, number> } => {
  const rows = Object.entries(modes && typeof modes === 'object' ? modes : {})
    .filter(([, at]) => typeof at === 'number').map(([id, at]) => ({ id, at: at as number }))
  const r = K.modesPlan({ modes: rows, ids: [...ids], now, on })
  return { kept: r.kept, modes: Object.fromEntries(r.modes.map(m => [m.id, m.at])) }
}
// A task boundary (Kernel.Mode): the mark kept, whether to read the context's usage now, and
// whether it is far enough along to compact
export const compactPlan = (facts: Parameters<typeof K.compactPlan>[0]): ReturnType<typeof K.compactPlan> => K.compactPlan(facts)

// Which gate one simple command meets (Kernel.Gate, #1364, #1314): its words as the host's parser
// read them (Claude Code's shellparse, OpenCode's own scan) and the files its redirects write in;
// the command's own word, the gate (closed), what that gate needs (cd's target, the dry run's
// arguments, git's -C, the send's arguments), whether it hands work out, and every file it writes
export type GateName = '' | 'cd' | 'send' | 'outward' | 'route' | 'start' | 'settings'
export type Gate = Omit<ReturnType<typeof K.gateOf>, 'gate'> & { readonly gate: GateName }
export const gateOf = (words: ReadonlyArray<string>, writes: ReadonlyArray<string> = []): Gate =>
  K.gateOf({ words, writes }) as Gate

// The coding gate's rules (Kernel.Coding, #92): an edit of a task's code under the user's mode and
// the task's allowances; `open`, the allowance a `start` stamps (-1 for none)
export type CodingFacts = Parameters<typeof K.codingGate>[0]
export type CodingVerdict = 'pass' | 'start' | 'ask' | 'refuse'
export type Coding = Omit<ReturnType<typeof K.codingGate>, 'verdict'> & { readonly verdict: CodingVerdict }
export const codingGate = (facts: CodingFacts): Coding => K.codingGate(facts) as Coding
// A host's file tools, each with the field that names the file it writes (Kernel.Coding's editTools);
// no prototype, so a tool named `constructor` finds no field
export const editFields = (host: 'claude-code' | 'opencode'): Readonly<Record<string, string>> =>
  Object.assign(Object.create(null),
    Object.fromEntries(K.editTools.filter(t => t.host === host).map(t => [t.tool, t.field])))
