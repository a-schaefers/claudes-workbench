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
 * 'coding', 'chat', 'record', 'live', 'every', 'progress', 'splash', 'probe'), the option the focus ring is on ('' none),
 * so its sentence shows, and whether the user closed it (a strip above the prompt stands in for it). */
export type WbPane = { tab: string; open: string; focus: string; minimized: boolean }

/** The Stats tab's history: `wb stats --json` as last read (null before the first read), and the
 * key of the files it was read from (results.jsonl, stats.json, optimize.json, bench/runs.jsonl,
 * speed.json and savings.json: size and mtime). */
export type WbStats = { key: string; feed: Record<string, any> | null }

/** The Stats tab's live view (#408): `wb top --json`'s newest tick (null before the first), and
 * each GPU's busy % since the tab was shown, for its sparkline (memory only: never on disk). */
export type WbTop = { snap: Record<string, any> | null; busy: Record<string, number[]> }

/** What this conversation was told of the rules that depend on the state (#540): the user's
 * notes, that wakes are off, the Claude-coding rule, the roles (#1014: routes.summary and
 * `. Day cap $N`, '' while every role is the default and no cap is set); null before its first
 * context block. */
export type WbSaid = { notes: string; wakeOff: boolean; coding: string; routes: string }

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
