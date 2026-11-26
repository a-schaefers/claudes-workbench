// The plugin's server part: it runs in OpenCode's server (the background service, or a
// `--standalone` one), where the driver's tools, notices and gates will live (#1296's slices).
// This is the skeleton (#1311, 2026-11-25): the RPC contract's `status`, nothing else yet.
//
// Never inside an agent: wb's runner writes "-workbench" into every task's config layer
// (context.PLUGIN_OFF), which OpenCode applies after the user's global `plugins`, so the
// supervisor drops this plugin before setup (core/src/plugin/supervisor.ts, `remove`). OpenCode
// still imports the module to learn its id, so nothing here runs at import time. WB_TASK is the
// second fence: set in every agent's environment, setup does nothing (#1363).
import type { ServerContext, ServerPlugin } from './host.d.ts'
import { Workbench, type Status } from './rpc.ts'
import { calls } from './calls.ts'
import { chat } from './chat.ts'
import { consent } from './consent.ts'
import { findWb, wbEnv, wbHome } from './wb.ts'
import { registerAgent } from './agent.ts'
import { startDriver, type DriverHost } from './driver.ts'

export const ID = 'workbench'

export function status(options: ServerContext['options'], base = process.env): Status {
  const env = wbEnv(options, base)
  const found = findWb(options, env)
  return { wb: found.wb, found: found.found, from: found.from, home: wbHome(env) }
}

const plugin: ServerPlugin = {
  id: ID,
  async setup(ctx) {
    if (process.env.WB_TASK) return
    await ctx.rpc.register(Workbench, { status: async () => status(ctx.options) })
    // each part's cleanup joins `stops`, and setup returns them together at its end: a part added
    // after another's `return` never ran (the chat-path review, 2026-11-25)
    const stops: (() => Promise<void>)[] = []
    const stopChat = await chat(ctx) // the web chat: in-process turns and chat_reply (#1319)
    if (stopChat) stops.push(stopChat)
    const agent = await registerAgent(ctx).catch(() => undefined) // no skill/ to read: no agent, the rest stands
    if (agent) stops.push(async () => agent.stop())
    stops.push(await startDriver(ctx as ServerContext & DriverHost))  // the driver's heartbeat and RPC (#1316)
    // the consent gates before the calls: their execute.before refusal comes before a call's row (#1314)
    const stopConsent = await consent(ctx)
    if (stopConsent) stops.push(stopConsent)
    const stopCalls = await calls(ctx) // the driver's calls in claude-calls/ (#1331)
    if (stopCalls) stops.push(stopCalls)
    return async () => { for (const stop of stops.reverse()) await stop().catch(() => {}) }
  },
}

export default plugin
