// The web app's shared types (#1323): what the chat server sends (chat.py's state, patches and
// /api/task; settings.py's rows) and the views' API, window.WB, that app.ts sets and stats.ts and
// settings.ts build against. Global, not a module: the pages are plain scripts, and a module's
// `export {}` would stop the browser loading them as they are loaded now. A shape here follows the
// server's: change it with the Python (ARCHITECTURE.md, *Couplings*), and the browser test says
// whether the page still works.

// ---- the chat's state (GET /api/state, the stream's `state` and `patch`, chat.snapshot) ----
interface WbFile { id: string; name: string; size: number; type: string }
// a draft's file (wb-chat-files): picked, uploaded once by its id, sent by id
interface DraftFile extends WbFile { mod: number; state: 'up' | 'retry' | 'done' | 'refused'; why?: string }
// what a message is about: a task, a pair of screenshots (n, step) or a kept take (audio, n, name)
interface About { task?: string | null; n?: number; step?: number; audio?: string; name?: string }
interface ShotSide { pruned?: boolean; image?: unknown; exit?: number | null; rev?: string }
interface Shots { task: string; step: number; round: number; n: number; viewport: string; base: ShotSide; candidate: ShotSide }
interface Listen { batch: string; clips?: { n: number; name: string; line?: string }[] }
// which driver (#1366, #1319): on the listener, and on each reply as its writer (none before 2.2)
interface Driver { tool: string; model?: string }
interface Msg {
  id: string; cid?: string; from: string; role?: string; text: string; ts: number; to?: string[]
  local?: boolean; error?: string; about?: About; files?: WbFile[]
  received?: { how?: string; ts: number; by?: string }; expired?: boolean; what?: string; shots?: Shots; audio?: Listen
  driver?: Driver
}
// a message in the outbox (wb-chat-out-<cid>), until the server has it
interface Outgoing { cid: string; text: string; ts: number; error?: string; about?: About; files?: WbFile[] }
// an OpenCode server's claim says `tool: "opencode"` too (chat.hold)
interface Listener { cwd: string; busy?: boolean; tool?: string; driver?: Driver }
// chat.open_tasks: the strip's and the task sheet's
interface OpenTask { name: string; phase: string; model?: string; via?: string; provider_name?: string }
interface ApiCost { tasks: number; unknown: number; plan?: number; cost: number; prices?: string }
interface ApiWeek extends ApiCost { by_provider?: Record<string, ApiCost>; names?: Record<string, string> }
// chat.week_saved
interface Savings { score?: { score: number; color: string } | null; saved: number; tasks?: number; wh?: number; cost?: number | null; api?: ApiWeek | null }
interface Limits { file: number; message: number; files: number }
interface ChatState {
  messages: Msg[]; push: { key?: string; test?: number }; listener: Listener | null; savings?: Savings | null
  expire?: number; uploads?: Limits | null; tasks?: OpenTask[]; dictate?: boolean
}
// a state as the server sends it, whole or a patch's fields (`now`, its clock; `build`, its files)
interface Snapshot extends Partial<Omit<ChatState, 'messages'>> { messages?: Msg[]; now?: number; build?: string }
interface Patch extends Omit<Snapshot, 'messages'> { messages?: { put?: Msg[]; drop?: string[] } }

// ---- GET /api/task: one open task's summary, diff or log (chat.task_summary, task_diff, task_log) ----
interface TaskPart {
  error?: string; task?: string; phase?: string; round?: number; step?: number; model?: string; via?: string
  provider_name?: string; now: number; created_at?: number; updated_at?: number; last?: string; report?: string
  report_cut?: boolean; verify?: string[]; base?: string; note?: string; stat?: string; cut?: boolean; diff: string
  lines_cut?: number; files: { path: string; added: number | null; deleted?: number }[]
  rows: { ok?: boolean; ts?: number; round?: number; text: string }[]; more?: boolean
}

// ---- GET /api/settings: settings.py's rows ----
interface SettingChoice { value: string; label: string; says?: string }
interface SettingRow {
  key: string; group: string; kind: string; label: string; value?: string | number | null; text?: string
  says?: string; choices?: SettingChoice[]; other?: boolean; unit?: string
}

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
interface WindowEventMap { beforeinstallprompt: BeforeInstallPromptEvent; appinstalled: Event }
// a thumbnail waiting to come near the view, with what loads it (app.ts's whenNear)
interface Near extends HTMLElement { wbLoad(): unknown }

// ---- the Stats view's feeds: wb stats --json (GET /api/stats, stats.feed) and wb top --json's ticks
// (GET /api/top). Wide, Python's, and read field by field with a fallback for each, so plain JSON ----
type Json = any
