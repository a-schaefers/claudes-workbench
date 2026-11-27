// Deep workflows' words in OpenCode (#1278, S13): what the user types and /deepmode, the same
// outcomes as the Claude Code mod's, OpenCode-native (the host-shape decision, #1296).
// - The keywords (../mod/hooks/keywords.ts, both hosts' one copy, held to tests/fixtures/
//   keywords.json): `deepcode` in a prompt the user typed, or wrote in the web chat, adds wb's rule
//   (`wb deepmode --rule`, runs.DEEPCODE_RULE) to that prompt's requests as a system part (the
//   `context` hook; the mod's hidden context entry); a line `deepmode on|off` is the user's own
//   yes, so wb deepmode runs as the session, and its turn is told it ran. Never a prompt another
//   part or plugin made (metadata), an agent's (`[agent `) or a `[workbench]` one.
// - `/workbench-deepmode [on|off]` (and `/deepmode`, its alias): a server command (OpenCode's `command.transform`), listed in the TUI:
//   it sets or shows the choice, and says it without starting a turn. upkeep.ts's
//   `dynamic-workflows` entry follows workflows.json at the session's next prompt.
// Its prompt hook is registered before upkeep's (server.ts), so a toggle is in workflows.json
// before upkeep reads it for that prompt's rules. Nothing runs at import time.
import { keywords, onlyToggles } from '../mod/hooks/keywords.ts'
import { SOURCE as CHAT_SOURCE, WEB } from './chat.ts'
import { sessionRunner, whereOf, type Command, type CommandHost, type SessionRun } from './commands.ts'

export const DEEPMODE = 'deepmode'
// the family's name, as /workbench-quit's (#1628); /deepmode stays its alias
export const WORKBENCH_DEEPMODE = 'workbench-deepmode'
const SOURCE = 'workbench.command'
export const FALLBACK = 'The user wrote `deepcode` (Workbench\'s word, not Claude\'s `ultracode`): run this ' +
  'request as a deep workflow, as your Workbench instructions\' *Deep workflows* says.'
export const TOGGLED = (on: boolean, out: string, ok: boolean, only: boolean) =>
  ok ? `[workbench] The user's \`deepmode ${on ? 'on' : 'off'}\` line set it (wb ran \`wb deepmode ${on ? 'on' : 'off'}\`): ${out}` +
    (only ? ' Nothing else to do for it: say so in one line.' : '')
    : `[workbench] The user's \`deepmode ${on ? 'on' : 'off'}\` line didn't take: wb deepmode failed: ${out}`

type Row = Record<string, unknown>
interface Reg { dispose(): Promise<void> }
// plugin/src/promise/session.ts `SessionPrompt` (its text, metadata and message id) and
// `SessionContext` (`system`, and `messages`, each with the stored message's id: 2.4.1)
export interface PromptEvent { readonly sessionID: string; readonly messageID?: string; readonly prompt?: { readonly text?: string }; readonly metadata?: Row }
export interface ContextEvent { readonly sessionID: string; system: { type: 'text'; text: string }[]; readonly messages?: readonly { readonly role?: string; readonly id?: string }[] }
export interface DeepHost extends CommandHost {
  readonly session?: CommandHost['session'] & {
    readonly hook?: (name: 'prompt' | 'context', cb: (e: never) => unknown) => Promise<Reg>
  }
}

// Whether the user wrote it, and their words: typed (no metadata, as chat.ts's typedByUser), or a
// web message of the user's that chat.ts delivered
export function usersWords(e: PromptEvent): string | null {
  const text = String(e?.prompt?.text ?? '')
  const meta = e?.metadata
  if (meta && meta.source === CHAT_SOURCE) return text.startsWith(WEB) ? text.slice(WEB.length) : null
  if (meta && Object.keys(meta).length) return null
  if (text.startsWith(WEB) || text.startsWith('[agent ') || text.startsWith('[workbench]')) return null
  return text
}

export class Deep {
  private rule = ''
  // per session: the prompt whose requests carry the notes, and the notes
  private readonly notes = new Map<string, { message: string; texts: string[] }>()
  constructor(private readonly host: DeepHost, private readonly run: SessionRun) {}

  private async isChild(sessionID: string): Promise<boolean> {
    try {
      const got = (await this.host.session?.get?.({ sessionID })) as { data?: unknown } | undefined
      const info = (got && typeof got === 'object' && 'data' in got ? got.data : got) as { parentID?: unknown } | undefined
      return !!info?.parentID
    } catch {
      return false
    }
  }

  private async where(sessionID: string): Promise<string> {
    return (await whereOf(this.host, sessionID)).dir
  }

  async prompted(e: PromptEvent): Promise<void> {
    if (!e?.sessionID) return
    const words = usersWords(e)
    this.notes.delete(e.sessionID) // a new prompt: the last one's notes are done
    if (words === null) return
    const kw = keywords(words)
    if (!kw.deepcode && kw.deepmode === null) return
    // a subagent's prompt comes unmarked too (core/src/tool/plugin/subagent.ts): it is the driver's
    // words, never the user's yes, so a `deepmode off` line in it, or a `deepcode`, is nobody's (#1325)
    if (await this.isChild(e.sessionID)) return
    const texts: string[] = []
    if (kw.deepmode !== null) {
      const on = kw.deepmode
      const r = await this.run(['deepmode', on ? 'on' : 'off'], { session: e.sessionID, cwd: await this.where(e.sessionID) })
      texts.push(TOGGLED(on, r.out, r.ok, onlyToggles(words)))
    }
    if (kw.deepcode) {
      if (!this.rule) {
        const r = await this.run(['deepmode', '--rule'], { session: e.sessionID })
        if (r.ok && r.out) this.rule = r.out
      }
      texts.push(this.rule || FALLBACK)
    }
    if (texts.length) this.notes.set(e.sessionID, { message: String(e.messageID ?? ''), texts })
  }

  // Each request of that prompt's turn: the notes as system parts. With the request's messages
  // (2.4.1) only while that prompt is still the newest user message; without them, until the next prompt
  context(input: ContextEvent): void {
    const n = this.notes.get(input?.sessionID)
    if (!n || !Array.isArray(input.system)) return
    const users = Array.isArray(input.messages) ? input.messages.filter(m => m?.role === 'user' && m.id) : []
    if (n.message && users.length && users[users.length - 1]!.id !== n.message) return
    for (const text of n.texts) input.system.push({ type: 'text', text })
  }

  command(name: string = WORKBENCH_DEEPMODE): Command {
    return {
      name,
      description: name === WORKBENCH_DEEPMODE
        ? 'workbench: Deepmode (dynamic workflows): on or off, alone to see it; on, the panel shows [deepcode]'
        : 'workbench: Deepmode, as /workbench-deepmode: on or off, alone to see it',
      execute: async ({ sessionID, prompt }) => {
        const arg = String(prompt?.text ?? '').trim().toLowerCase()
        if (arg && arg !== 'on' && arg !== 'off') throw new Error(`/${name} on, /${name} off, or /${name} alone to see it`)
        const r = await this.run(['deepmode', ...(arg ? [arg] : [])], { session: sessionID, cwd: await this.where(sessionID) })
        if (!r.ok) throw new Error(`wb deepmode failed: ${r.out}`)
        if (!this.host.session?.synthetic) return
        await this.host.session.synthetic({ sessionID, text: `[workbench] The user ran /${name}${arg ? ` ${arg}` : ''}: ${r.out}\nNothing to do now.`,
          description: `Workbench: ${r.out.split('\n')[0]}`, metadata: { source: SOURCE }, resume: false })
      },
    }
  }
}

// The server part's setup calls this: the hooks and /deepmode, each where the server has it
export async function deep(host: DeepHost, run?: SessionRun): Promise<(() => Promise<void>) | undefined> {
  const d = new Deep(host, run ?? sessionRunner(host.options))
  const regs: Reg[] = []
  const hook = host.session?.hook
  if (hook) {
    try {
      regs.push(await hook('prompt', ((e: PromptEvent) => d.prompted(e).catch(() => {})) as never))
      regs.push(await hook('context', ((e: ContextEvent) => d.context(e)) as never))
    } catch { /* no session hooks: the command alone */ }
  }
  const transform = host.command?.transform
  if (transform) {
    try {
      regs.push(await transform((editor) => { editor.add(d.command()); editor.add(d.command(DEEPMODE)) }))
    } catch { /* no commands on this server */ }
  }
  if (!regs.length) return undefined
  return async () => { for (const r of regs) await r.dispose().catch(() => {}) }
}
