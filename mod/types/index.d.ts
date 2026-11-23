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
}

/** One line of `wb watch --json`: a style name and the text. */
export type WbLine = { s: string; t: string }

/** The task the pane follows live, and its transcript so far (the newest lines). */
export type WbLive = { task: string; lines: WbLine[] }

/** This session and the web chat: whether it listens and whether the user has it on (`wb chat
 * on|off`: off, no link and no listening). When the user last wrote from there is chat/away.json,
 * every session's (#688). */
export type WbChat = { key: string; delegating: boolean; url: string; web: boolean }

/** The pane's view: its tab ('tasks', 'stats' or 'settings'), the settings row open ('' none,
 * 'coding', 'chat', 'record', 'live', 'every', 'progress', 'splash', 'probe', and #1023's 'agents', 'busy',
 * 'workflows', 'role-<role>', 'gpu-<role>', 'size-<model id>', 'api-<role>', 'think-<role>'; #1047's
 * 'media-image|speech|sfx|music'), the option the focus ring is on ('' none),
 * so its sentence shows, and whether the user closed it (a strip above the prompt stands in for it). */
export type WbPane = { tab: string; open: string; focus: string; minimized: boolean }

/** `wb route --json` as the Settings tab reads it (#1023): routes.json normalized (`use` the Agents
 * switch, `busy` the busy-GPU opt-in, the day cap, each role's selector and both models), spend
 * today from the day ledger, and each API provider's key state (only `stored` runs). */
export type WbRoutes = {
  v: 1
  use: 'gpu' | 'api' | 'both'
  busy: 'wait' | 'api'
  day_usd: number | null
  roles: Record<'significant' | 'trivial' | 'writing', { via: 'gpu' | 'api'; gpu: string; api?: string;
    thinking: 'off' | 'on'; label: string }>
  spend_today: { usd: number; by_provider: Record<string, number> }
  providers: Record<string, { name: string; key: 'stored' | 'env' | 'oauth' | 'none'; why?: string }>
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

/** The Stats tab's history: `wb stats --json` as last read (null before the first read), and the
 * key of the files it was read from (results.jsonl, stats.json, optimize.json, bench/runs.jsonl,
 * speed.json and savings.json: size and mtime). */
export type WbStats = { key: string; feed: Record<string, any> | null }

/** The Stats tab's live view (#408): `wb top --json`'s newest tick (null before the first), and
 * each GPU's busy % since the tab was shown, for its sparkline (memory only: never on disk). */
export type WbTop = { snap: Record<string, any> | null; busy: Record<string, number[]> }

/** What this conversation was told of the rules that depend on the state (#540): the user's
 * notes, that wakes are off, the Claude-coding rule, the roles (#1014: routes.summary and
 * `. Day cap $N`, '' while every role is the default and no cap is set), Dynamic workflows (#1041:
 * `on` or `off`); null before its first context block. */
export type WbSaid = { notes: string; wakeOff: boolean; coding: string; routes: string; workflows: string }

declare module 'claude-code' {
  interface PluginState {
    workbench: {
      tasks: Shaped<WbTask[]>; live: Shaped<WbLive>; chat: Shaped<WbChat>; sessions: Shaped<string[]>
      /** the pane's footer: the week's saving, `savedLine` of `wb savings --json` ('' for none) */
      savings: Shaped<string>
      /** the pane's settings rows (WbPane) */
      pane: Shaped<WbPane>
      /** the Stats tab's history (WbStats, #406) */
      stats: Shaped<WbStats>
      /** the Stats tab's live view (WbTop, #408) */
      top: Shaped<WbTop>
      /** what the conversation was told of the rules (WbSaid, #540): kept across a reload (#697) */
      said: Shaped<WbSaid | null>
      /** this session's first /workbench has come, and with it the opening animation (#963):
       * kept across a reload, so nothing plays it again */
      splash: Shaped<boolean>
    }
  }
}
