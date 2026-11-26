// The driver's wait (#1355), for both hosts: the Claude Code mod (register.tsx) and the OpenCode
// plugin (opencode-plugin/chat.ts). Each host keeps the dialogs up in its terminal; the oldest one
// up WAITING_AFTER_MS or more is the fact it writes into its listener claim (chat/listener.json
// `waiting`, or `wb chat listener --waiting`), and wb turns that into the web app's state line
// (chat.driver_state: "Claude is waiting for you in the terminal: a permission for Bash."). One
// answered sooner never shows: an auto-accept (OpenCode's `--auto`, a rule) replies at once, and a
// line that came and went in a second said nothing true. The words are wb's (workbench/story.py),
// never here; kernel/BOUNDARY.md keeps this filter out of the kernel (each host's own records in,
// one fact out, no state machine). tests/fixtures/story/dialogs.json holds both hosts to it.
// Pure, and nothing runs at import.

export const WAITING_AFTER_MS = 2_000
// the kinds wb's state line names (chat.WAIT_KINDS); any other is a permission
export const WAIT_KINDS = ['permission', 'question', 'plan', 'elicitation', 'consent'] as const

export interface OpenDialog {
  readonly kind: string
  readonly tool: string
  /** when it came up, ms */
  readonly at: number
  /** what a permission would run, or a form's title: shown under the line */
  readonly text?: string
  readonly done?: boolean
}
export interface Waiting { kind: string; tool: string; since: number; what?: string }

const WHAT_MAX = 1500

// The oldest dialog up WAITING_AFTER_MS or more at ``now`` (ms), as the claim says it (`since` in
// seconds, wb's clock), or null for none
export function waitingFact(dialogs: readonly OpenDialog[], now: number): Waiting | null {
  let oldest: OpenDialog | null = null
  for (const d of dialogs) {
    if (d.done || !(now - d.at >= WAITING_AFTER_MS)) continue
    if (!oldest || d.at < oldest.at) oldest = d
  }
  if (!oldest) return null
  const kind = (WAIT_KINDS as readonly string[]).includes(oldest.kind) ? oldest.kind : 'permission'
  const what = typeof oldest.text === 'string' ? oldest.text.slice(0, WHAT_MAX) : ''
  return { kind, tool: String(oldest.tool ?? '').slice(0, 200), since: oldest.at / 1000, ...(what ? { what } : {}) }
}

// Two facts the same, so a host writes its claim only on a change
export const sameWait = (a: Waiting | null, b: Waiting | null) => JSON.stringify(a) === JSON.stringify(b)
