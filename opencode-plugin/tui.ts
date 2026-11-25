// The plugin's TUI part: OpenCode's terminal UI loads it for a server plugin that is active
// (tui/src/plugin/context.tsx, `serverTuiPlugins`), so "-workbench" in a task's layer turns it
// off with the server part. The pane, the footer and /workbench come in #1312; the skeleton
// (#1311, 2026-11-25) asks the server part which wb it runs and says so only when it finds none:
// every later piece needs it, and a service started without the user's PATH is the likely cause.
import type { TuiPlugin } from './host.d.ts'
import { Workbench, type Status } from './rpc.ts'

// what the plugin needs and its Git install lacks: wb, from a clone (ocplugin.CLONE says the same)
export const CLONE = 'git clone https://github.com/a-schaefers/workbench, then run bin/wb setup --opencode in it'

const plugin: TuiPlugin = {
  id: 'workbench',
  async setup(ctx) {
    if (process.env.WB_TASK) return
    let st: Status
    try {
      st = (await ctx.client.rpc(Workbench).status({})) as Status
    } catch {
      return // the server part isn't there (a server older than this TUI): nothing to say yet
    }
    if (!st.found) {
      ctx.ui.toast.show({
        title: 'Workbench',
        variant: 'warning',
        message: st.from === 'options'
          ? `wb isn't at ${st.wb}, which the plugin's options name: run bin/wb setup --opencode in your Workbench clone`
          // the Git install brings only this package, never wb (#1311's review, 2026-11-25): say
          // where wb comes from, not a clone the user was never told to make
          : `no wb on OpenCode's PATH, and the plugin doesn't bring one: ${CLONE}`,
      })
    }
  },
}

export default plugin
