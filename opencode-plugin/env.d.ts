// The runtime this plugin is type-checked against, declared here so `bun check` (OpenCode's own
// Bun: BUN_BE_BUN=1 opencode check -p opencode-plugin) needs no node_modules and nothing installed
// (#1311, 2026-11-25). Only what the plugin's files use: OpenCode runs the plugin in its own Bun,
// where all of these exist. A new use adds its line here.

declare const process: {
  readonly env: Record<string, string | undefined>
  readonly platform: string
  readonly pid: number
  cwd(): string
}

// driver.ts's beat timer and wb's child (#1316)
declare function setTimeout(callback: () => unknown, ms: number): { readonly __timer: unique symbol }
declare function clearTimeout(timer: ReturnType<typeof setTimeout> | undefined): void

declare interface AbortSignal {
  readonly aborted: boolean
}

declare class AbortController {
  readonly signal: AbortSignal
  abort(): void
}

declare module 'node:child_process' {
  interface Readable { on(event: 'data', listener: (chunk: unknown) => void): void }
  interface ChildProcess {
    readonly stdout: Readable
    readonly stderr: Readable
    readonly stdin: { end(data: string): void }
    on(event: 'error', listener: (error: unknown) => void): void
    on(event: 'close', listener: (code: number | null) => void): void
    kill(signal?: string): boolean
  }
  export function spawn(command: string, args: readonly string[],
                        options: { env: Record<string, string | undefined>; stdio: readonly string[] }): ChildProcess
}

interface ImportMeta {
  readonly url: string
}

declare class URL {
  constructor(url: string, base?: string)
  readonly href: string
}

declare module 'node:url' {
  export function fileURLToPath(url: string | URL): string
}

declare module 'node:fs' {
  export function statSync(path: string, options: { throwIfNoEntry: false }): { isFile(): boolean; isDirectory(): boolean; mode: number; size: number } | undefined
  export function readFileSync(path: string, encoding: 'utf8'): string
  export function readdirSync(path: string): string[]
}

declare module 'node:path' {
  export const delimiter: string
  export function join(...parts: string[]): string
  export function isAbsolute(path: string): boolean
  export function resolve(...parts: string[]): string
  export function dirname(path: string): string
}

declare module 'bun:test' {
  export function test(name: string, body: () => unknown): void
  export function expect(value: unknown): {
    toBe(expected: unknown): void
    toEqual(expected: unknown): void
    toBeUndefined(): void
  }
}
