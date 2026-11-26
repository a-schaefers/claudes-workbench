// A flow as a slash command (#1295), for both hosts: the Claude Code mod (register.tsx) and the
// OpenCode plugin (opencode-plugin/flows.ts). A flow of the user's or a shipped one with a
// `command` key is `/<command> <args>`; what the hosts list is `wb run flows --json`'s rows with a
// command (runs.flow_rows: runs.validate checked each, a repo's flow never has one). The line
// after the name is never parsed here: it goes to `wb run start --flow <flow> --args '<line>'`,
// whose one parser (runs.parse_args, tests/fixtures/flow_args.json) serves both hosts. The command
// starts nothing itself: it hands the driver a `[workbench]` turn that says what was asked.
// Pure, and nothing runs at import.
import type { FlowRow } from '../types/wire.js'

export interface FlowCommand {
  readonly command: string
  readonly flow: string
  readonly description: string
  /** the flow's params, by name, as `wb run flows --json` gives them ({type, description, …}) */
  readonly params: FlowRow['params']
}

/** The commands in `wb run flows --json`'s output, one per command (the first wins); [] for text that isn't its rows. */
export function flowCommands(json: string): FlowCommand[] {
  let rows: unknown
  try {
    rows = JSON.parse(json)
  } catch {
    return []
  }
  if (!Array.isArray(rows)) return []
  const out: FlowCommand[] = []
  const seen = new Set<string>()
  for (const r of rows) {
    if (!r || typeof r !== 'object') continue
    const { command, name, description, params, where } = r as Partial<Record<keyof FlowRow, unknown>>
    if (typeof command !== 'string' || typeof name !== 'string' || where === 'repo' || seen.has(command)) continue
    if (!/^[a-z][a-z0-9-]{0,31}$/.test(command)) continue
    seen.add(command)
    out.push({ command, flow: name, description: typeof description === 'string' ? description : '',
      params: params && typeof params === 'object' && !Array.isArray(params) ? params as FlowCommand['params'] : {} })
  }
  return out
}

/** What follows the command's name: its one param whole, or each as `k=…`; '' for none. */
export function flowHint(c: FlowCommand): string {
  const names = Object.keys(c.params)
  return names.length === 1 ? `<${names[0]}>` : names.map(n => `${n}=…`).join(' ')
}

/** The line a host lists the command with: the flow's description, then (`withHint`, for a host
 * with no argument hint of its own) what is typed after it. */
export function flowDescription(c: FlowCommand, withHint = true): string {
  const hint = withHint && flowHint(c) ? ` (/${c.command} ${flowHint(c)})` : ''
  return `workbench flow ${c.flow}: ${c.description || 'a deep workflow'}${hint}`.slice(0, 200)
}

/** One shell word: the line as typed, single quoted. */
export const shellWord = (s: string) => `'${s.replace(/'/g, `'\\''`)}'`

/** The driver's turn for `/<command> <args>`. */
export const FLOW_NOTE = (c: FlowCommand, args: string) => {
  const line = args.trim()
  return `[workbench] The user ran /${c.command}${line ? ` ${line}` : ''}: flow ${c.flow} as a slash command, ` +
    `their ask to run it as a deep workflow. As the /workbench skill's *Deep workflows* says, say its stages, ` +
    `roles and estimate in at most three lines, then start it once: \`wb run start <a run name of yours> ` +
    `--flow ${c.flow}${line ? ` --args ${shellWord(line)}` : ''}\` (\`--dry-run\` first shows the same without ` +
    `starting), and wait for its notices with \`wb wait <run>\` in the background. If wb run start refuses ` +
    `the arguments, say why in one line and how to type it again.`
}
