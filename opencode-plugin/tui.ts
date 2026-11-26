// The plugin's TUI part: OpenCode's terminal UI loads it for a server plugin that is active
// (tui/src/plugin/context.tsx, `serverTuiPlugins`), so "-workbench" in a task's layer turns it
// off with the server part. The panel, the sidebar section and the footer item are pane.tsx's
// (#1312, #1332), /workbench the server part's (pane.ts); the skeleton
// (#1311, 2026-11-25) asks the server part which wb it runs and says so only when it finds none:
// every later piece needs it, and a service started without the user's PATH is the likely cause.
import type { TuiContext, TuiPlugin } from './host.d.ts'
import type { SplashHost } from './splash.tsx'
import type { PaneHost } from './pane.tsx'
import { Workbench, type Status } from './rpc.ts'
import { consentTui } from './consent-tui.ts'
import { noticesTui } from './notices-tui.ts'

// where wb comes from when this install brings none (ocplugin.GET_WB, word for word): the Git
// install of the whole repository does (#1311's review, 2026-11-25)
export const GET_WB = "opencode plugin add 'github:a-schaefers/workbench#main' brings it, or bin/wb setup --opencode in a clone"

const plugin: TuiPlugin = {
  id: 'workbench',
  async setup(ctx) {
    if (process.env.WB_TASK) return
    const stopConsent = consentTui(ctx) // the consent gates' forms (#1314)
    const stopNotices = noticesTui(ctx) // the notices' toasts and desktop notifications (#1313)
    const stopAll = () => { stopConsent?.(); stopNotices() }
    let st: Status
    try {
      st = (await ctx.client.rpc(Workbench).status({})) as Status
    } catch {
      return stopAll // the server part isn't there (a server older than this TUI): nothing to say yet
    }
    if (!st.found) {
      ctx.ui.toast.show({
        title: 'Workbench',
        variant: 'warning',
        message: st.from === 'options'
          ? `wb isn't at ${st.wb}, which the plugin's options name: run bin/wb setup --opencode in your Workbench clone`
          // a copy of opencode-plugin/ alone, without the package's bin/wb beside it, and none
          // on the service's PATH (#1311's review, 2026-11-25): say where wb comes from
          : `this install of the plugin brings no wb, and OpenCode's PATH has none: ${GET_WB}`,
      })
    }
    // Workbench's panel with the apprentice and the opening animation (#1333), its sidebar section
    // and footer item (#1312, #1332): imported here, not at load, as OpenTUI and Solid are only the
    // TUI's; the context has what SplashHost and PaneHost name (plugin/src/tui/context.ts), which
    // host.d.ts leaves out
    try {
      const { mountPane } = await import('./pane.tsx')
      const stopPane = mountPane(ctx as TuiContext & SplashHost & PaneHost, st.home)
      return () => { stopPane(); stopAll() }
    } catch {
      return stopAll // a TUI that can't load a plugin's OpenTUI views: the rest works without them
    }
  },
}

export default plugin
