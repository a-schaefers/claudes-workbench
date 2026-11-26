// The keywords on OpenCode (#1278): `deepcode` and `deepmode on|off` in what the user wrote, read
// by the Claude Code mod's own module (../mod/hooks/keywords.ts), so both hosts read a prompt the
// same way. The outcomes match the mod's, done OpenCode's way (the host-shape decision, #1296):
// - The `prompt` hook sees each prompt as it is admitted. One the user typed in a root session
//   (it comes with no metadata) or wrote in the web chat (chat.ts's, `[web chat] …`, with metadata
//   `source: workbench.chat`) is read. A synthetic notice, a command, a subagent's prompt and an
//   agent's web message never are. A `deepmode on|off` line runs `wb deepmode on|off` as the
//   session, because the user's own line is their yes. Upkeep's `dynamic-workflows` entry then
//   follows the setting, as it does for any change. For `deepcode`, wb's rule
//   (`wb deepmode --rule`) is read once a process. Each wb run holds the prompt WB_MS at most.
// - The `context` hook, before each of the session's requests, adds to that prompt's message what
//   the model must read beside it. The rule goes in while the session's agent is the Workbench
//   agent, whose prompt holds *Deep workflows*; outside it nothing could follow the rule. A toggle
//   gets TOGGLED. Each is a text part after the user's words, in the request only: the stored
//   message, which the TUI shows, keeps just what the user wrote. Every request carries the
//   message the same way, so the prompt cache holds across steps. The hook finds the message by
//   the id the `prompt` hook gave it (session/session.ts `Session.prompt`: the inbox id is the
//   message's; a real server's request shows it, RealOpenCodePlugin's deepcode test). It never
//   waits for wb: a rule not read yet is left out.
// This server keeps the marks while it runs. After a restart a message marked before it goes out
// as stored.
// Nothing runs at import time.
import { keywords, RULE_ARGS, TOGGLED, toggleArgs } from '../mod/hooks/keywords.ts'
import { AGENT } from './agent.ts'
import { SOURCE as CHAT_SOURCE, WEB, WEB_NOTE } from './chat.ts'
import { sessionRunner, type SessionRun } from './commands.ts'
import type { Options } from './wb.ts'

// The most a prompt waits for wb: a hung one never holds the user's prompt longer
export const WB_MS = 15_000

// SKILL.md's Claude Code words in wb's rule, as the Workbench agent's prompt says them
export const forDriver = (rule: string): string =>
  rule.replace("the /workbench skill's *Deep workflows*", "your Workbench instructions' *Deep workflows*")

// OpenCode 2.4.1's shapes this module uses (host.d.ts's rule: each from its source):
// plugin/src/promise/session.ts `SessionPrompt` (`prompt.text` as admitted, `metadata`, the
// message's id) and `SessionContext` (`messages`: ai/src/schema/messages.ts, a user message's
// `content` its text and media parts; `metadata` the stored message's, for reading)
export interface Prompted {
  readonly sessionID: string
  readonly messageID: string
  readonly prompt: { readonly text: string }
  readonly metadata?: Record<string, unknown>
}
interface Part { readonly type: string; readonly text?: string }
export interface RequestMessage { readonly id?: string; readonly role: string; readonly content: readonly Part[]; readonly metadata?: Record<string, unknown> }
export interface Context { readonly sessionID: string; readonly agent: string; messages: RequestMessage[] }
export interface KeywordsHost {
  readonly options: Options
  readonly session: {
    readonly hook: (name: string, callback: (input: any) => unknown) => Promise<{ dispose(): Promise<void> }>
    // schema/src/session.ts `Session.Info`: `parentID` on a subagent's session
    readonly get?: (input: { readonly sessionID: string }) => Promise<unknown>
  }
}

// What the user wrote, from a prompt: a typed one whole; a web chat one as the mod reads it, the
// message's own text, so without chat.ts's head, its `(about …)` line, the attachments' lines
// (a file named deepcode.png) and its note; undefined for anything else
export function userWords(text: string, metadata: Record<string, unknown> | undefined): string | undefined {
  const marked = !!metadata && Object.keys(metadata).length > 0
  if (!marked) return text.startsWith(WEB) || text.startsWith('[agent ') || text.startsWith('[workbench]') ? undefined : text
  if (metadata!.source !== CHAT_SOURCE || !text.startsWith(`${WEB} `)) return undefined
  const words = text.slice(WEB.length + 1).replace(/^\((?:about|keeps take) [^)\n]*\) /, '')
  const end = [words.indexOf('\n[attached: '), words.lastIndexOf(`\n\n${WEB_NOTE}`)].filter(i => i >= 0)
  return end.length ? words.slice(0, Math.min(...end)) : words
}

export class Keywording {
  // each marked message's id: the rule's wanted (`deepcode`) and the toggle's line, if any
  readonly marks = new Map<string, { deepcode: boolean; toggled: string }>()
  private rule: Promise<string> | undefined
  private said = '' // the rule once read: the context hook never waits for wb

  constructor(readonly run: SessionRun, readonly isRoot: (sessionID: string) => Promise<boolean>) {}

  // wb's rule, read once; a failed read is read again at the next `deepcode`
  ruleText(): Promise<string> {
    this.rule ??= this.run([...RULE_ARGS], { timeout: WB_MS }).then(r => {
      if (!r.ok || !r.out) this.rule = undefined
      else this.said = r.out
      return r.ok ? r.out : ''
    })
    return this.rule
  }

  async prompt(e: Prompted): Promise<void> {
    const words = userWords(String(e?.prompt?.text ?? ''), e?.metadata)
    if (words === undefined || !e.messageID) return
    const k = keywords(words)
    if (!k.deepcode && k.deepmode === null) return
    if (!(await this.isRoot(e.sessionID))) return // a subagent's prompt is the driver's words
    let toggled = ''
    if (k.deepmode !== null) {
      const r = await this.run(toggleArgs(k.deepmode), { session: e.sessionID, timeout: WB_MS })
      if (r.ok) toggled = TOGGLED(k.deepmode, r.out)
    }
    if (k.deepcode) await this.ruleText()
    if (this.marks.size > 1000) this.marks.clear()
    this.marks.set(e.messageID, { deepcode: k.deepcode, toggled })
  }

  async context(input: Context): Promise<void> {
    if (!this.marks.size || !Array.isArray(input?.messages)) return
    for (let i = 0; i < input.messages.length; i++) {
      const m = input.messages[i]!
      const mark = m?.role === 'user' && m.id ? this.marks.get(m.id) : undefined
      if (!mark) continue
      const notes = mark.toggled ? [mark.toggled] : []
      if (mark.deepcode && input.agent === AGENT && this.said) notes.push(forDriver(this.said))
      if (!notes.length) continue
      // metadata kept (an empty one where there was none): OpenCode makes a message of it again
      // before the request, stripping it (session/model-request.ts)
      input.messages[i] = { ...m, metadata: m.metadata ?? {}, content: [...m.content, ...notes.map(text => ({ type: 'text', text }))] }
    }
  }
}

// The server part's setup calls this: the two hooks, registered before upkeep's so that a toggle
// is set before upkeep reads the setting for the same prompt; nothing without the session hooks
export async function keywordHooks(host: KeywordsHost, run?: SessionRun): Promise<(() => Promise<void>) | undefined> {
  if (!host.session?.hook) return undefined
  const isRoot = async (sessionID: string) => {
    const got = await host.session.get?.({ sessionID }).catch(() => undefined) as { data?: unknown } | undefined
    const info = (got && typeof got === 'object' && 'data' in got ? got.data : got) as { parentID?: unknown } | undefined
    return !info?.parentID
  }
  const k = new Keywording(run ?? sessionRunner(host.options), isRoot)
  const regs = [
    await host.session.hook('prompt', (e: Prompted) => k.prompt(e).catch(() => {})),
    await host.session.hook('context', (input: Context) => k.context(input).catch(() => {})),
  ]
  return async () => { for (const r of regs) await r.dispose().catch(() => {}) }
}
