/** One delegated task as the pane draws it, read from the task's status.json and task.json. */
export type WbTask = {
  name: string
  phase: string
  round: number
  model: string
  label: string
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
  /** the Claude Code session `wb start` ran in ('' by hand, or before 0.90): whose task it is */
  session: string
  /** the Claude Code process it ran in (0 when unknown): once gone, the directory rule adopts it */
  claude_pid: number
  /** task.json's backlog ('' for none): while one of this session's is open, the user stays away */
  backlog: string
  /** the user's unspent allowance for Claude to edit it (`wb coding allow`), as the pane says it;
   * '' for none */
  allowed: string
}

/** One line of `wb watch --json`: a style name and the text. */
export type WbLine = { s: string; t: string }

/** The task the pane follows live, and its transcript so far (the newest lines). */
export type WbLive = { task: string; lines: WbLine[] }

/** This session and the web chat: whether it listens, when the user last wrote from there, and
 * whether the user has it on (`wb chat on|off`: off, no link and no listening). */
export type WbChat = { key: string; delegating: boolean; webAt: number; url: string; web: boolean }

/** The pane's view: its tab ('tasks' or 'settings'), the settings row open ('' none, 'coding',
 * 'chat'), the option the focus ring is on ('' none), so its sentence shows, and whether the user
 * closed it (a strip above the prompt stands in for it). */
export type WbPane = { tab: string; open: string; focus: string; minimized: boolean }

declare module 'claude-code' {
  interface PluginState {
    workbench: {
      tasks: Shaped<WbTask[]>; live: Shaped<WbLive>; chat: Shaped<WbChat>; sessions: Shaped<string[]>
      /** the pane's footer: the week's saving, `savedLine` of `wb savings --json` ('' for none) */
      savings: Shaped<string>
      /** the pane's settings rows (WbPane) */
      pane: Shaped<WbPane>
    }
  }
}
