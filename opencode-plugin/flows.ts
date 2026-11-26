// A flow as a slash command in OpenCode (#1295, with #1315's commands): each flow of the user's or a
// shipped one with a `command` key is a server command (OpenCode's `command.transform`, as
// commands.ts's), so the TUI lists `/<command>`. Running it posts the driver a `synthetic` turn
// (resume: true, the user's own ask), the same words as the Claude Code mod's queued turn
// (../mod/hooks/flows.ts, both hosts' one copy): the driver starts `wb run start --flow <flow>
// --args '<line>'`, whose parser (runs.parse_args) reads the line for both hosts. The list is
// `wb run flows --json` at the server's setup: a flow saved later is there after a restart or
// reload. A flow named like one of OpenCode's or wb's own commands never gets here: runs.validate
// refuses it. Nothing runs at import time.
import { FLOW_NOTE, flowCommands, flowDescription } from '../mod/hooks/flows.ts'
import { sessionRunner, type Command, type CommandHost, type SessionRun } from './commands.ts'

const SOURCE = 'workbench.command'

export function flowDefinitions(host: CommandHost, listed: string): Command[] {
  return flowCommands(listed).map(c => ({
    name: c.command,
    description: flowDescription(c),
    async execute({ sessionID, prompt }) {
      if (!host.session?.synthetic) throw new Error(`/${c.command}: this OpenCode can't post the turn that runs flow ${c.flow}`)
      // `prompt.text` is the line after the command's name, as typed
      await host.session.synthetic({ sessionID, text: FLOW_NOTE(c, String(prompt?.text ?? '')),
        description: `Workbench: flow ${c.flow}`, metadata: { source: SOURCE }, resume: true })
    },
  }))
}

// The server part's setup calls this: the flows' commands, or nothing (no commands API, no wb, no flow with one)
export async function registerFlows(host: CommandHost, run?: SessionRun): Promise<(() => Promise<void>) | undefined> {
  const transform = host.command?.transform
  if (!transform) return undefined
  const { ok, out } = await (run ?? sessionRunner(host.options))(['run', 'flows', '--json'], {})
  const defs = ok ? flowDefinitions(host, out) : []
  if (!defs.length) return undefined
  const reg = await transform((editor) => { for (const def of defs) editor.add(def) })
  return async () => { await reg.dispose() }
}
