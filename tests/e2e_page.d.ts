// What the browser tests leave on the page's window and document from page.evaluate (each a probe
// or a stand-in one check installs and reads back), and the page's own types: the views' API,
// window.WB, as the web app declares it (#1323). Global, as those scripts run in the page.
/// <reference path="../workbench/chat_static/src/types.d.ts" />
interface Window {
  copied: string[]; said: (string | null)[]; _metas: (string | null)[]; _fetch: typeof fetch; _tries: number
  _active: (string | undefined)[]; _checks: number; _states: number; sameLoad: boolean; pwned: unknown; live: Set<string>
}
interface Document { _hasFocus: () => boolean }
interface Window {
  e2eRealFetch: typeof fetch; e2eFeed: () => Promise<string>; e2eDown: boolean
  e2eTop: { opened: number; closed: number; mode: string; ticks: unknown[]; n: number; statsDown?: boolean }
  __wbFetch?: typeof fetch; __wbSettings: (method: string, body: unknown) => Promise<[number, unknown] | null>
}
// what e2e_chat.mts hands each view's check(page, ctx) (e2e_stats.mts, e2e_settings.mts)
interface E2eCtx {
  engine: string; device?: string; desktop: boolean; base: string; token: string; home: string
  env: Record<string, string | undefined>; wb: (...args: string[]) => string
  until: (fn: () => unknown, ms?: number) => Promise<void>; check: (name: string, fn: () => Promise<unknown>) => Promise<void>
  sleep: (ms: number) => Promise<unknown>; goTo: (name: string) => Promise<void>; context: any; browser: any
}
