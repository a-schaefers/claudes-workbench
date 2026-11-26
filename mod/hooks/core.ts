// The membrane (kernel/BOUNDARY.md): the one file that imports ./kernel.js, the PureScript kernel
// compiled by scripts/kernel.py. It maps the kernel's wire records to what TypeScript uses and
// decides nothing. Both hosts reach the kernel only through here: the Claude Code mod, and the
// OpenCode plugin (#1296), so a decision made in the kernel is made the same way on each (#1356).
import * as K from './kernel.js'

// Every export by name, for the goldens (golden/run.ts calls each case's `fn` on it).
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
// What a task's phase means (Kernel.Phase): no caller yet; #1325's M1 moves register.tsx's
// RUNNING/waitsOnClaude and tasks.ts's phase checks onto it
export const phaseOf = (facts: PhaseFacts): Phase => K.phaseOf(facts)

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
