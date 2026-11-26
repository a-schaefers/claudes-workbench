// The runtime chat.ts uses beyond env.d.ts's (#1319, 2026-11-25), in a file of its own so the
// lanes building other parts at once each add theirs without touching one list. Ambient module
// declarations merge with env.d.ts's. OpenCode runs the plugin in its own Bun, where all exist.

declare function setInterval(callback: () => void, ms: number): unknown
declare function clearInterval(id: unknown): void
declare function setTimeout(callback: () => void, ms: number): unknown

interface AbortSignal {
  readonly aborted: boolean
}
declare var AbortController: {
  new (): { readonly signal: AbortSignal; abort(): void }
}

declare module 'node:fs' {
  export function readFileSync(path: string, encoding: 'utf8'): string
}

declare module 'node:child_process' {
  export function execFile(
    file: string,
    args: readonly string[],
    options: { env?: Record<string, string>; cwd?: string; timeout?: number; maxBuffer?: number },
    callback: (error: Error | null, stdout: string, stderr: string) => void,
  ): unknown
}
