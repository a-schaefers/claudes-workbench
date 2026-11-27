// The web app's types (#1323): what the chat server sends, taken from the one module the mod and
// the OpenCode plugin read it through too (mod/types/wire.d.ts: chat.py's state, patches and
// /api/task; settings.py's rows), and the views' API, window.WB, that app.ts sets and stats.ts and
// settings.ts build against. Global, not a module: the pages are plain scripts, and a module's
// `export {}` would stop the browser loading them as they are loaded now; `import()` types keep it
// global. A shape follows the server's: change it in wire.d.ts with the Python (ARCHITECTURE.md,
// *Couplings*), and the browser test says whether the page still works.

// ---- the chat's state (GET /api/state, the stream's `state` and `patch`, chat.snapshot) ----
type WbFile = import('../../../mod/types/wire.js').ChatFile
// a draft's file (wb-chat-files): picked, uploaded once by its id, sent by id
interface DraftFile extends WbFile { mod: number; state: 'up' | 'retry' | 'done' | 'refused'; why?: string }
type About = import('../../../mod/types/wire.js').About
type Shots = import('../../../mod/types/wire.js').Shots
type Listen = import('../../../mod/types/wire.js').Listen
type Driver = import('../../../mod/types/wire.js').Driver
type Msg = import('../../../mod/types/wire.js').ChatMsg
// a message in the outbox (wb-chat-out-<cid>), until the server has it
interface Outgoing { cid: string; text: string; ts: number; error?: string; about?: About; files?: WbFile[] }
type Listener = import('../../../mod/types/wire.js').Listener
type OpenTask = import('../../../mod/types/wire.js').OpenTask
type OpenRun = import('../../../mod/types/wire.js').OpenRun
type ApiCost = import('../../../mod/types/wire.js').ApiCost
type ApiWeek = import('../../../mod/types/wire.js').ApiWeek
type Savings = import('../../../mod/types/wire.js').Savings
type Limits = import('../../../mod/types/wire.js').Limits
type ChatState = import('../../../mod/types/wire.js').ChatState
type Snapshot = import('../../../mod/types/wire.js').Snapshot
type Patch = import('../../../mod/types/wire.js').Patch

// ---- GET /api/task: one open task's summary, diff or log (chat.task_summary, task_diff, task_log) ----
type TaskPart = import('../../../mod/types/wire.js').TaskPart

// ---- GET /api/settings: settings.py's rows ----
type SettingChoice = import('../../../mod/types/wire.js').SettingChoice
type SettingRow = import('../../../mod/types/wire.js').SettingRow
type SettingChange = import('../../../mod/types/wire.js').SettingChange

// ---- the views' API (app.ts's window.WB, frozen) ----
// a POST under app.ts's deadline (`post`): the reply read whole, its JSON on asking
interface Posted { ok: boolean; status: number; headers: Headers; json(): Promise<any> }
// WB.api's init: fetch's, with a plain object as the body sent as JSON
interface WbInit extends Omit<RequestInit, 'body'> { body?: BodyInit | object | null }
interface WbView { mount?(at: HTMLElement): void; show?(): void; hide?(): void }
interface WbApi {
  token(): string
  api(path: string, init?: WbInit): Promise<Response>
  post(path: string, body: unknown): Promise<Posted>
  view(name: string, mod: WbView): void
  toast(text: unknown): void
  on(event: 'state' | 'view', fn: (arg: any) => void): void
  current(): string
}
declare var WB: WbApi

// ---- what lib.dom lacks that the page reads ----
interface Navigator { readonly standalone?: boolean }  // iOS Safari: opened from the home screen
interface BeforeInstallPromptEvent extends Event { prompt(): Promise<void>; readonly userChoice: Promise<unknown> }
interface WindowEventMap {
  beforeinstallprompt: BeforeInstallPromptEvent; appinstalled: Event
  // another view asks the chat to open a task's sheet (app.ts's openTask; stats.ts's Running rows, #1339)
  'wb:open-task': CustomEvent<{ task: string }>
}
// a thumbnail waiting to come near the view, with what loads it (app.ts's whenNear)
interface Near extends HTMLElement { wbLoad(): unknown }

// ---- the Stats view's feeds: wb stats --json (GET /api/stats, stats.feed) and wb top --json's ticks
// (GET /api/top). Wide, Python's, and read field by field with a fallback for each, so plain JSON ----
type Json = any
