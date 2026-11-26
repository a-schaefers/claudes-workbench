// The runtime upstream.ts's tests use beyond the other parts' declarations (#1296), in a file of its
// own as consent-env.d.ts asks: ambient declarations merge. OpenCode runs the plugin in its own Bun,
// where it exists.
declare module 'node:fs' {
  export function symlinkSync(target: string, path: string): void
}
