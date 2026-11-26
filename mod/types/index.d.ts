/** One delegated task as the pane draws it, read from the task's status.json and task.json. */
export type WbTask = {
  name: string
  phase: string
  round: number
  model: string
  label: string
  /** task.json's via (#1016): 'api' for a task on an API model (its row's badge), else 'gpu' */
  via: 'gpu' | 'api'
  /** task.json's role ('' before roles) and its provider's name ('' for a GPU task) */
  role: string
  provider: string
  /** task.json's route: how its model was chosen, the routing line ('' for none) */
  routed: string
  tests: 'pass' | 'fail' | null
  last: string
  createdAt: number
  updatedAt: number
  tokens: number
  /** the last few events of an active task, already formatted */
  recent: string[]
  /** task.json's repo and the directory `wb start` ran in ('' when an old task has none): whose
   * session's task it is */
  repo: string
  cwd: string
  /** task.json's subdir, the monorepo package it is for ('' for none): a session in a sibling
   * package doesn't claim it (#695) */
  subdir: string
  /** the Claude Code session `wb start` ran in ('' by hand, or before 0.90): whose task it is */
  session: string
  /** its driver as store.driver_of names it (`opencode`, `claude`, '' for none): a session never
   * adopts another driver's task by directory (#1309, the kernel's owns) */
  driver?: string
  /** the Claude Code process it ran in (0 when unknown): once gone, the directory rule adopts it */
  claude_pid: number
  /** when that process started (task.json `claude_start`, '' when unknown): a recycled pid isn't it */
  claude_start: string
  /** task.json's backlog ('' for none): while one of this session's is open, the user stays away */
  backlog: string
  /** review, stuck or failed and status `parked`: stopped or left by /workbench-quit (`wb stop --all
   * --park-waiting`), waiting on no one (#541, #698) */
  parked: boolean
  /** the user's unspent allowance for Claude to edit it (`wb coding allow`), as the pane says it;
   * '' for none */
  allowed: string
  /** The Tasks view's facts (#1048, mod/hooks/tasks.ts's TaskFacts): task.json `group` ('' none);
   * status `cards` ([] for none: one card, an API task, an older wb), `tools`, `tok_out` */
  group: string
  cards: number[]
  tools: number
  tokOut: number
  /** status `live_used.used` (0 unless its session is status `session`), `window`, `tuned.fresh_at` */
  ctxUsed: number
  window: number
  freshAt: number | null
  /** status `auto_fixes` and task.json `auto_fix`; status `round_for` ('' from an older wb),
   * `reviews`, `steps` */
  autoFixes: number
  autoFixMax: number
  roundFor: string
  reviews: number
  steps: number
  /** the step's active seconds before this round (status `active_s` less `step_active_s`), the
   * round's start (ms; 0 none), status `typical` {min, n} (0 none) and `late_min` (0 none) */
  activeS: number
  roundStartedAt: number
  typicalMin: number
  typicalN: number
  lateMin: number
  /** status `api.usd` (wave 2's meter, #1019); 'plan' for a plan's provider, 'none' for no price,
   * null with no `api` record (a GPU task) */
  spend: number | 'plan' | 'none' | null
}

/** One line of `wb watch --json`: a style name and the text. */
export type WbLine = { s: string; t: string }

/** The task the pane follows live, and its transcript so far (the newest lines). */
export type WbLive = { task: string; lines: WbLine[] }

/** This session and the web chat: whether it is in workbench mode (`delegating`), and the web app's
 * link as `wb chat url --json` (chat.ensure) last said it (#1283): `state` `off` after `wb chat
 * off` (then no session listens or sends it prompts), `checking` before the first answer, `up` or
 * `started` with its `url`, `blocked` or `failed` with `why`. When the user last wrote from there is
 * chat/away.json, every session's (#688). */
export type WbChat = { key: string; delegating: boolean
  link: { state: 'up' | 'started' | 'off' | 'blocked' | 'failed' | 'checking'; url?: string; why?: string } }

/** The pane: Tasks only (#1283: Stats and Settings are the web app's). `open` and `focus` are kept
 * for the shape; `minimized`, whether the user closed it (a strip above the prompt stands in). */
export type WbPane = { open: string; focus: string; minimized: boolean }

/** `wb route --json` as the Tasks view's day-cap line reads it (#1023, #1048): routes.json normalized (`use` the Agents
 * switch, `busy` the busy-GPU opt-in, the day cap, each role's selector and both models), spend
 * today from the day ledger, and each API provider's key state (`stored`, `copilot` and an `env`
 * one with `runs` run tasks; `ollama` runs its cloud models). */
export type WbRoutes = {
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

/** `wb models --json` (#1023): the GPU models (cards: the numbers its Ollama sees, null unknown),
 * the API models wb can run (stored keys only), those tasks ran on, optimize's state. */
export type WbModels = {
  gpu: { id: string; label: string; window: number | null; sizes: string[]; cards: number[] | null }[]
  api: { id: string; name: string; provider: string; provider_name: string; window: number | null;
    price: { input: number | null; output: number | null } | null; free: boolean }[]
  used: string[]
  free_line: string
  optimize: { done_at: number | null; models: number; running: boolean }
  /** #1047: each media kind's engines and the one used without --engine (absent from an older wb,
   * and a kind whose file doesn't parse is left out) */
  media?: Partial<Record<'image' | 'speech' | 'sfx' | 'music', WbMedia>>
}

/** One media kind in `wb models --json`'s media (#1047): its default (null with no engine) and its
 * engines in file order, `gpu` false for one off this GPU (media.on_gpu). */
export type WbMedia = { default: string | null; engines: { name: string; gpu: boolean }[] }

/** What this conversation was told of the rules that depend on the state (#540): the user's
 * notes, that wakes are off, the Claude-coding rule, the roles (#1014: routes.summary and
 * `. Day cap $N`, '' while every role is the default and no cap is set), Dynamic workflows (#1041:
 * `on` or `off`); null before its first context block. */
export type WbSaid = { notes: string; wakeOff: boolean; coding: string; routes: string; workflows: string }

declare module 'claude-code' {
  interface PluginState {
    workbench: {
      tasks: Shaped<WbTask[]>; live: Shaped<WbLive>; chat: Shaped<WbChat>; sessions: Shaped<string[]>
      /** the pane (WbPane) */
      pane: Shaped<WbPane>
      /** what the conversation was told of the rules (WbSaid, #540): kept across a reload (#697) */
      said: Shaped<WbSaid | null>
      /** this session's first /workbench has come, and with it the opening animation (#963):
       * kept across a reload, so nothing plays it again */
      splash: Shaped<boolean>
    }
  }
}
