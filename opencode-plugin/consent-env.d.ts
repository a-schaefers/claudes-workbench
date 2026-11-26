// The runtime the consent gates (consent.ts, gates.ts) and the driver's call rows (calls.ts) use
// beyond env.d.ts's (#1314, #1331, 2026-11-25), in a file of their own so the lanes building
// other parts at once each add theirs without touching one list. Ambient declarations merge with
// env.d.ts's (and another part's, an identical overload being harmless). OpenCode runs the plugin
// in its own Bun, where all of these exist.

interface AbortSignal {
  readonly aborted: boolean
  addEventListener(type: 'abort', listener: () => void): void
}

declare function setTimeout(callback: () => void, ms: number): unknown
declare function clearTimeout(id: unknown): void

declare module 'node:fs' {
  export function readFileSync(path: string, encoding: 'utf8'): string
  export function writeFileSync(path: string, data: string): void
  export function renameSync(from: string, to: string): void
  export function mkdirSync(path: string, options: { recursive: true }): unknown
  export function realpathSync(path: string): string
  export function existsSync(path: string): boolean
  export function readdirSync(path: string): string[]
  // the tests' scratch directories
  export function mkdtempSync(prefix: string): string
  export function rmSync(path: string, options: { recursive: true; force: true }): void
}

declare module 'node:os' {
  export function tmpdir(): string
}

declare module 'node:path' {
  export function dirname(path: string): string
  export function basename(path: string): string
}

declare module 'node:child_process' {
  export function execFile(
    file: string,
    args: readonly string[],
    options: { env?: Record<string, string>; cwd?: string; timeout?: number; maxBuffer?: number },
    callback: (error: (Error & { code?: number | string }) | null, stdout: string, stderr: string) => void,
  ): { readonly stdin?: { end(): void } | null }
}
