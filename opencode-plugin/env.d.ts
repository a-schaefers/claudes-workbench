// The runtime this plugin is type-checked against, declared here so `bun check` (OpenCode's own
// Bun: BUN_BE_BUN=1 opencode check -p opencode-plugin) needs no node_modules and nothing installed
// (#1311, 2026-11-25). Only what the plugin's files use: OpenCode runs the plugin in its own Bun,
// where all of these exist. A new use adds its line here.

declare const process: {
  readonly env: Record<string, string | undefined>
  readonly platform: string
}

declare module 'node:fs' {
  export function statSync(path: string, options: { throwIfNoEntry: false }): { isFile(): boolean; mode: number } | undefined
}

declare module 'node:path' {
  export const delimiter: string
  export function join(...parts: string[]): string
  export function isAbsolute(path: string): boolean
}

declare module 'bun:test' {
  export function test(name: string, body: () => unknown): void
  export function expect(value: unknown): {
    toBe(expected: unknown): void
    toEqual(expected: unknown): void
    toBeUndefined(): void
  }
}
