// The plugin's TUI part: OpenCode's terminal UI loads it for a server plugin that is active
// (tui/src/plugin/context.tsx, `serverTuiPlugins`), so "-workbench" in a task's layer turns it
// off with the server part. The pane, the footer and /workbench come in #1312; the skeleton
// (#1311, 2026-11-25) asks the server part which wb it runs and says so only when it finds none:
// every later piece needs it, and a service started without the user's PATH is the likely cause.
import type { TuiPlugin } from './host.d.ts'
import { Workbench, type Status } from './rpc.ts'

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
          ? `wb isn't at ${st.wb}, which the plugin's options name: run wb setup --opencode from your clone`
          : "no wb on OpenCode's PATH: run wb setup --opencode from your clone",
      })
    }
  },
}

export default plugin
