// What wb sends, in one place (#1323): the files and JSON its readers in TypeScript read, each
// shape as the Python writes it. The Claude Code mod (hooks/register.tsx, tasks.ts, modeline.ts),
// the OpenCode plugin (opencode-plugin/) and the web app (workbench/chat_static/src/types.d.ts, by
// `import()` types, as its pages are plain scripts) all take them from here, so a field wb adds,
// renames or drops is changed once and each reader's type check meets it. (The mod's state atoms,
// types/index.d.ts, may import nothing: its WbLine and WbChat's link restate WatchLine and
// ChatLink, which modeline.ts holds equal.) It lives in the mod because the mod is installed as
// its own directory (marketplace.json's `./mod`): it can import nothing outside it, while the other
// two can import from it.
//
// A declaration file, types only: nothing here runs, nothing is bundled, and OpenCode's Git install
// needs none of it at run time (package.json's `files`). Every field read from a file is optional:
// the readers take `{}` for a file missing or torn, and a wb from before a field writes none. The
// task files and notices end in an index signature (`unknown`): wb writes more than any reader
// reads. Change a shape with the Python that writes it (ARCHITECTURE.md, *Couplings*).

// ---- the task files: tasks/<task>/task.json (store.Task.spec, `wb start`) and status.json
// (Task.set_status, the runner)

/** task.json's `driver` (store.driver_record, #1309): the OpenCode session that started it. */
export type TaskDriver = { tool?: string; model?: string; session?: string; [key: string]: unknown }

/** tasks/<task>/task.json, as the pane, the owner check and the notices read it. */
export type TaskFile = {
  name?: string; model?: string; model_label?: string
  /** 'api' for a task on an API model (#1016), else 'gpu' */
  via?: 'gpu' | 'api'; role?: string; provider?: string; provider_name?: string
  /** how its model was chosen: the routing line */
  route?: string
  /** seconds (store.now) */
  created_at?: number
  /** whose task it is (the kernel's owns): the repo, the directory `wb start` ran in, the
   * monorepo package (#695), the Claude Code session and process, or the OpenCode driver */
  repo?: string; cwd?: string; subdir?: string; session?: string; driver?: TaskDriver | null
  claude_pid?: number; claude_start?: string; claude_machine?: string
  backlog?: string
  /** a replay of wb bench's: no one's task */
  bench?: unknown
  /** `wb start --group` (#1048), `--auto-fix`, and the run an item is of (#1278) */
  group?: string; auto_fix?: number; run?: string
  [key: string]: unknown
}

/** tasks/<task>/status.json, as the pane and the notices read it. */
export type StatusFile = {
  phase?: string
  /** seconds (store.now) */
  updated_at?: number
  round?: number; last?: string
  /** review, stuck or failed and stopped by `wb stop --all --park-waiting` (#541, #698) */
  parked?: boolean
  /** handed back to Claude (`wb takeover`, #834): no longer delegated work */
  taken_over?: unknown
  tests?: { ok?: boolean; [key: string]: unknown } | null
  tok_in?: number; tok_out?: number; tools?: number; steps?: number; reviews?: number
  /** the context window, and the session's fill of it while `session` is the status' own (#262) */
  window?: number; session?: string; live_used?: { used?: number; session?: string } | null
  tuned?: { fresh_at?: number; [key: string]: unknown } | null
  /** the cards it holds ([] on one card, or an API task) */
  cards?: unknown[]
  auto_fixes?: number; round_for?: string
  /** runner.active_so_far's parts, seconds; the round's start (seconds); the typical length and
   * the progress checks' first point, minutes */
  active_s?: number; step_active_s?: number; round_started_at?: number
  typical?: { min?: number; n?: number } | null; late_min?: number
  /** an API task's money (meter.py, #1019): `usd` null for a plan's provider or no price;
   * `prices` meter.basis's (#1135, missing before it: the catalog's) */
  api?: { usd?: number | null; plan?: boolean; prices?: 'catalog' | 'yours' | 'provider'; [key: string]: unknown } | null
  [key: string]: unknown
}

// ---- notices: notify.jsonl's rows (store.Task.notify), as store.notices keeps them whole

export type NoticeRow = {
  /** seconds, after every notice before it and the task's acknowledgement (#473) */
  ts: number; task: string
  /** a run's own notice (runs.Run.notify, #1278): its run, no `task`, `kind` run_checkpoint,
   * run_done, run_paused, run_stopped or run_failed, and `phase` the run's while it is still true */
  run?: string; phase?: string
  /** review, stuck, failed, question, checkin, … */
  kind: string; text?: string
  /** the tasks a batch notice is for (one restart or update stopped them all) */
  tasks?: string[]
  round?: number
  /** why it stopped (`agent`, `opencode`, `provider`, `spend`, `local_only`, …), and the guard
   * of a spend stop (`agents`) */
  cause?: string; guard?: string
  /** a question's id (ask_mcp) */
  id?: string
  [key: string]: unknown
}

// ---- the chat (workbench/chat.py): its rows, its state (GET /api/state, the stream's `state`
// and `patch`, chat.snapshot), the listener's claim and the open work

/** a message's file (chat.upload): its id, and where wb saved it (`path`) for the driver to read */
export type ChatFile = { id: string; name: string; size: number; type: string; path?: string }
/** what a message is about: a task, a pair of screenshots (n, step, round) or a kept take (audio, n,
 * name, path) */
export type About = { task?: string | null; n?: number; step?: number; round?: number; audio?: string; name?: string; path?: string }
export type ShotSide = { pruned?: boolean; image?: unknown; exit?: number | null; rev?: string }
export type Shots = { task: string; step: number; round: number; n: number; viewport: string; base: ShotSide; candidate: ShotSide }
export type Listen = { batch: string; clips?: { n: number; name: string; line?: string }[] }
/** which driver (#1366, #1319): on the listener, and on each reply as its writer (none before 2.2) */
export type Driver = { tool: string; model?: string }
/** one row of chat/messages.jsonl (chat.send), as the thread and chat/open.json carry it */
export type ChatMsg = {
  id: string; cid?: string; from: string; role?: string; text: string; ts: number; to?: string[]
  local?: boolean; error?: string; about?: About; files?: ChatFile[]
  received?: { how?: string; ts: number; by?: string }; expired?: boolean; what?: string; shots?: Shots; audio?: Listen
  driver?: Driver
  /** the Claude Code session and process `wb chat reply` ran under (#1096, #1129) */
  session?: string; claude_pid?: number
}
/** chat/listener.json: the session listening (the mod's `listen`, or an OpenCode server's claim,
 * chat.hold: `tool: "opencode"`, `driver: "opencode"`); `{}` for none */
export type ListenerClaim = {
  session?: string; cwd?: string; pid?: number; start?: string; machine?: string
  /** the Claude Code process (#1129) */
  parent?: number
  busy?: boolean; tool?: string; driver?: string
  /** an older claim's time, seconds: one without a pid is gone LISTENING_FOR after it */
  ts?: number
}
/** the listener as the state serves it (chat.listener_view): the claim, with its driver */
export type Listener = Omit<ListenerClaim, 'driver' | 'cwd'> & { cwd: string; driver?: Driver | null }
/** chat/open.json: the user's messages without a receipt (#329), by id */
export type ChatOpen = { messages?: Record<string, ChatMsg> }
/** `wb chat url --json` (chat.ensure, #1283): the web app's link while up, why not otherwise */
export type ChatLink = { state: 'up' | 'started' | 'off' | 'blocked' | 'failed'; url?: string; why?: string }

/** chat.open_tasks: the strip's and the task sheet's */
export type OpenTask = { name: string; phase: string; model?: string; via?: string; provider_name?: string }
/** chat.open_runs: the runs strip's (runs.snapshot's rows, #1278): each stage's counts by state */
export type OpenRun = {
  run: string; flow?: string | null; phase?: string | null; why?: string
  stages?: Record<string, Record<string, number>>; spent_usd?: number; budget_usd?: number | null
  budget_min?: number | null; gpu_min?: number
}
export type ApiCost = { tasks: number; unknown: number; plan?: number; cost: number; prices?: string }
export type ApiWeek = ApiCost & { by_provider?: Record<string, ApiCost>; names?: Record<string, string> }
/** chat.week_saved */
export type Savings = { score?: { score: number; color: string } | null; saved: number; tasks?: number; wh?: number; cost?: number | null; api?: ApiWeek | null }
export type Limits = { file: number; message: number; files: number }
/** chat.driver_state (#1355): the driver's state line, in the story's words. `driver` the story's
 * name (claude-code, opencode, any); `waiting` the dialog up in the terminal; `grant` only for a
 * permission, the driver's way to run with permissions granted; `off` the chat switched off (#1526) */
export type DriverState = {
  state: 'off' | 'none' | 'idle' | 'working' | 'waiting'; driver: string; who: string; where?: string
  waiting?: { kind: string; tool: string; since: number; what?: string }
  line: string; next: string; grant?: { line: string; command: string }
}
/** chat/settings-rev.json (settings.record, #1355): the newest Settings change */
export type SettingsRev = { ts: number; key: string; label: string; to?: unknown; by: string }
export type ChatState = {
  messages: ChatMsg[]; push: { key?: string; test?: number }; listener: Listener | null; savings?: Savings | null
  expire?: number; uploads?: Limits | null; tasks?: OpenTask[]; runs?: OpenRun[]; dictate?: boolean
  driver?: DriverState | null; settings?: SettingsRev | null
  /** chat.API_VERSION */
  api?: number
}
/** a state as the server sends it, whole or a patch's fields (`now`, its clock; `build`, its files) */
export type Snapshot = Partial<Omit<ChatState, 'messages'>> & { messages?: ChatMsg[]; now?: number; build?: string }
export type Patch = Omit<Snapshot, 'messages'> & { messages?: { put?: ChatMsg[]; drop?: string[] } }

/** GET /api/task: one open task's summary, diff or log (chat.task_summary, task_diff, task_log) */
export type TaskPart = {
  error?: string; task?: string; phase?: string; round?: number; step?: number; model?: string; via?: string
  provider_name?: string; now: number; created_at?: number; updated_at?: number; last?: string; report?: string
  report_cut?: boolean; verify?: string[]; base?: string; note?: string; stat?: string; cut?: boolean; diff: string
  lines_cut?: number; files: { path: string; added: number | null; deleted?: number }[]
  rows: { ok?: boolean; ts?: number; round?: number; text: string }[]; more?: boolean
}

// ---- settings (workbench/settings.py, #1279)

export type SettingChoice = { value: string; label: string; says?: string }
/** one of settings.rows() (GET /api/settings, `wb settings --json`): `kind` choice, number (`value`
 * a float or null for off, `unit` usd or per_kwh), info (`text`), action (its choices buttons) or
 * note (one line for the whole group); `other`: any model id is taken; `group` a role's name for its
 * card (#1553), `more` a row drawn under that card's More; `hidden` a row the page doesn't draw while it
 * can do nothing, `fixed` a choice drawn as its `says` alone */
export type SettingRow = {
  key: string; group: string; kind: string; label: string; value?: string | number | null; text?: string
  says?: string; choices?: SettingChoice[]; other?: boolean; unit?: string; more?: boolean; hidden?: boolean; fixed?: boolean
  /** its newest change within a week (settings.changed, #1355): when, and who (web, claude, opencode, terminal, agent) */
  changed?: { ts: number; by: string }
}
/** one of settings.changes() (GET /api/settings's `changes`, newest first) */
export type SettingChange = { ts: number; key: string; label: string; from?: unknown; to?: unknown; by: string }
/** `wb settings offers --json` (settings.offers): the web's changes due a word, with help (offers)
 * or without (notes) */
export type SettingsOffers = {
  offers?: { ts: number; key: string; label: string; text: string; toast: string }[]
  notes?: { ts: number; key: string; text: string }[]
}

// ---- wb's JSON outputs the panes read

/** One line of `wb watch --json`: a style name and the text. */
export type WatchLine = { s: string; t: string }

/** `wb route --json` (#1023, #1048): routes.json normalized (`use` the Agents switch, `busy` the
 * busy-GPU opt-in, the day cap, each role's selector and both models), spend today from the day
 * ledger, and each API provider's key state (`stored`, `copilot` and an `env` one with `runs` run
 * tasks; `ollama` runs its cloud models). */
export type RouteJson = {
  v: 1
  use: 'gpu' | 'api' | 'both'
  busy: 'wait' | 'api'
  day_usd: number | null
  /** `thinks`: what the role's API model does with each thinking setting (routes.with_thinks, #1263) */
  roles: Record<'significant' | 'trivial' | 'writing', { via: 'gpu' | 'api'; gpu: string; api?: string;
    thinking: 'off' | 'on'; label: string; thinks?: { api: string; off: string; on: string } }>
  /** `prices`: the dollars' basis (meter.basis, #1135): catalog (an estimate, ≈), yours (wb model price), provider */
  spend_today: { usd: number; prices?: 'catalog' | 'yours' | 'provider'; by_provider: Record<string, number> }
  // runs: a key in the environment the keyhole reads (#1189), which runs tasks as a stored one does;
  // copilot: GitHub Copilot's sign-in, held as a key (#1191)
  providers: Record<string, { name: string; key: 'stored' | 'copilot' | 'ollama' | 'env' | 'oauth' | 'none'; why?: string; runs?: boolean }>
}

/** One media kind in `wb models --json`'s media (#1047): its default (null with no engine) and its
 * engines in file order, `gpu` false for one off this GPU (media.on_gpu). */
export type MediaJson = { default: string | null; engines: { name: string; gpu: boolean }[] }

/** `wb models --json` (#1023): the GPU models (cards: the numbers its Ollama sees, null unknown),
 * the API models wb can run (stored keys only), those tasks ran on, optimize's state. */
export type ModelsJson = {
  gpu: { id: string; label: string; window: number | null; sizes: string[]; cards: number[] | null }[]
  api: { id: string; name: string; provider: string; provider_name: string; window: number | null;
    price: { input: number | null; output: number | null } | null; free: boolean }[]
  used: string[]
  free_line: string
  optimize: { done_at: number | null; models: number; running: boolean }
  /** #1047: each media kind's engines and the one used without --engine (absent from an older wb,
   * and a kind whose file doesn't parse is left out) */
  media?: Partial<Record<'image' | 'speech' | 'sfx' | 'music', MediaJson>>
}

/** One flow in `wb run flows --json` (runs.flow_rows, #1295): `where` yours, shipped or repo; `mark`
 * a copy of yours that shadows a shipped one (`shadows shipped`, `; shipped changed since`), else '';
 * `command` its slash command (the user's and shipped flows only, valid) or null; `params` each
 * param as an object (runs.PARAM_KEYS). */
export type FlowRow = {
  name: string
  where: 'yours' | 'shipped' | 'repo'
  description: string
  mark: string
  command: string | null
  params: Record<string, { type: string; description?: string; default?: unknown; choices?: (string | number)[]; optional?: boolean }>
}
