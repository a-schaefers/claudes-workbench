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
import { childrenTui } from './children-tui.ts'

// where wb comes from when this install brings none (ocplugin.GET_WB, word for word): the Git
// install of the whole repository does (#1311's review, 2026-11-25)
export const GET_WB = "opencode plugin add 'github:a-schaefers/workbench#main' brings it, or bin/wb setup --opencode in a clone"

// The server part's status, asked again until it answers (#1488, the owner's own machine,
// 2026-11-26): on a cold OpenCode service the location for $HOME boots on the TUI's first RPC and
// loads its plugins only after answering it, so that first call fails `rpc.unavailable` and the
// next one, half a second on, finds the route. Bounded: an older server with no Workbench RPC
// answers `rpc.unavailable` for good, so the tries end (about 15 s) and nothing is mounted.
export const STATUS_TRIES = 30
export const STATUS_MS = 500
const sleep = (ms: number) => new Promise<void>(r => setTimeout(r, ms))

/** `ask` until it answers, at most `tries` times `STATUS_MS` apart; undefined when none did, or
 * once `stopped` (the TUI unloaded the plugin meanwhile). */
export async function askStatus(ask: () => Promise<Status>, stopped: () => boolean,
  tries = STATUS_TRIES, wait: (ms: number) => Promise<void> = sleep): Promise<Status | undefined> {
  for (let i = 0; i < tries; i++) {
    if (i) await wait(STATUS_MS)
    if (stopped()) return undefined
    try {
      return await ask()
    } catch { /* the service is still loading the plugin's server part, or it has none */ }
  }
  return undefined
}

/** The status's warning toast and the views: what setup does once the server part answered. */
async function mount(ctx: TuiContext, st: Status, stopped: () => boolean): Promise<(() => void) | undefined> {
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
    return stopped() ? undefined : mountPane(ctx as TuiContext & SplashHost & PaneHost, st.home)
  } catch {
    return undefined // a TUI that can't load a plugin's OpenTUI views: the rest works without them
  }
}

const plugin: TuiPlugin = {
  id: 'workbench',
  async setup(ctx) {
    if (process.env.WB_TASK) return
    const stopConsent = consentTui(ctx) // the consent gates' forms (#1314)
    const stopNotices = noticesTui(ctx) // the notices' toasts and desktop notifications (#1313)
    const stopChildren = childrenTui(ctx) // the agents' sessions as the lead's child sessions (#1331)
    let stopped = false
    let stopPane: (() => void) | undefined
    // the rest once the server part answers, never awaited here (#1488): OpenCode's TUI activates
    // its plugins one after another, and a plugin's ui.panel.open works only once its setup has
    // returned (tui/src/plugin/context.tsx `activate`, api.tsx `registry.active`)
    const ready = askStatus(() => ctx.client.rpc(Workbench).status({}) as Promise<Status>, () => stopped)
      .then(st => (st && !stopped ? mount(ctx, st, () => stopped) : undefined))
      .then(stop => { if (stopped) stop?.(); else stopPane = stop })
    const stop = () => { stopped = true; stopPane?.(); stopConsent?.(); stopNotices(); stopChildren() }
    return Object.assign(stop, { ready })
  },
}

export default plugin
