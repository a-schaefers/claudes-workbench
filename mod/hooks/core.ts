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

export type PhaseFacts = Parameters<typeof K.phaseOf>[0]
export type Phase = ReturnType<typeof K.phaseOf>
// What a task's phase means (Kernel.Phase): no caller yet; #1325's M1 moves register.tsx's
// ACTIVE/NEEDS/RUNNING/waitsOnClaude and tasks.ts's CLOSED onto it
export const phaseOf = (facts: PhaseFacts): Phase => K.phaseOf(facts)
