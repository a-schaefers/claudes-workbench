// The runtime tools.ts uses beyond env.d.ts's and consent-env.d.ts's (#1335, 2026-11-26), in a
// file of its own so the lanes building other parts at once each add theirs without touching one
// list. Ambient declarations merge with the others'. OpenCode runs the plugin in its own Bun,
// where all of these exist.

interface AbortSignal {
  removeEventListener(type: 'abort', listener: () => void): void
}

declare module 'node:child_process' {
  // wb in the session's directory
  export function spawn(command: string, args: readonly string[],
                        options: { env: Record<string, string | undefined>; cwd?: string; stdio: readonly string[] }): ChildProcess
}

declare module 'node:fs' {
  // the tests' fake wb
  export function chmodSync(path: string, mode: number): void
}
