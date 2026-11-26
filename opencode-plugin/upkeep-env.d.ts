// The runtime upkeep.ts and commands.ts use beyond env.d.ts's and chat-env.d.ts's (#1315,
// 2026-11-25), in a file of their own so the lanes building other parts at once each add theirs
// without touching one list. Ambient module declarations merge with the others'. OpenCode runs the
// plugin in its own Bun, where all exist.

declare module 'node:fs' {
  export function writeFileSync(path: string, data: string): void
}
