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
}

/** One line of `wb watch --json`: a style name and the text. */
export type WbLine = { s: string; t: string }

/** The task the pane follows live, and its transcript so far (the newest lines). */
export type WbLive = { task: string; lines: WbLine[] }

/** This session and the web chat: whether it listens, and when the user last wrote from there. */
export type WbChat = { key: string; delegating: boolean; webAt: number; url: string }

declare module 'claude-code' {
  interface PluginState {
    workbench: { tasks: Shaped<WbTask[]>; live: Shaped<WbLive>; chat: Shaped<WbChat> }
  }
}
