// The runtime notices.ts and jobs.ts use beyond env.d.ts's and chat-env.d.ts's (#1313, #1334), in a
// file of their own (chat-env.d.ts's rule: each part adds its own). Ambient module declarations
// merge with the others'. OpenCode runs the plugin in its own Bun, where all exist.

declare module 'node:fs' {
  export function readdirSync(path: string): string[]
  export function openSync(path: string, flags: 'r'): number
  export function readSync(fd: number, buffer: Uint8Array, offset: number, length: number, position: number): number
  export function closeSync(fd: number): void
  export function realpathSync(path: string): string
}

declare class TextDecoder {
  decode(input?: Uint8Array): string
}
