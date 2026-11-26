// What Workbench's panel, sidebar and footer (pane.ts, pane.tsx: #1312, #1332) use of the
// runtime and of the modules OpenCode's TUI hands a plugin, beside env.d.ts's and opentui.d.ts's,
// in a file of its own so the lanes building other parts at once each add theirs without touching
// one list (chat-env.d.ts's rule). Ambient declarations merge with theirs. Each shape is read
// from OpenCode 2.4.1's TUI (tui/src/feature-plugins/*, routes/session/sidebar.tsx,
// component/dialog-shell-output.tsx) and OpenTUI's renderables as the TUI uses them.

declare module 'node:fs' {
  export function readdirSync(path: string, options: { withFileTypes: true }): { readonly name: string; isDirectory(): boolean }[]
}

declare module '@opentui/solid/jsx-runtime' {
  export namespace JSX {
    interface Layout {
      readonly onMouseUp?: () => void
      readonly backgroundColor?: Colour
      readonly paddingLeft?: number
      readonly paddingRight?: number
      readonly paddingX?: number
      readonly gap?: number
      readonly marginTop?: number
      readonly maxHeight?: number
      readonly border?: boolean
      readonly borderStyle?: 'single' | 'rounded'
      readonly borderColor?: Colour
    }
    interface IntrinsicElements {
      // a scrolled box: `stickyScroll` with `stickyStart` bottom keeps it at its end as lines come,
      // until the user scrolls up, and again once they scroll back down (OpenTUI's ScrollBox)
      scrollbox: Layout & {
        readonly stickyScroll?: boolean
        readonly stickyStart?: 'top' | 'bottom'
        readonly scrollbarOptions?: { readonly visible?: boolean }
      }
    }
  }
}

declare module '@opentui/core' {
  // a span's style attributes, OR-ed (OpenTUI's TextAttributes)
  export const TextAttributes: { readonly BOLD: number; readonly DIM: number; readonly ITALIC: number; readonly UNDERLINE: number }
}

declare module 'solid-js' {
  import type { JSX } from '@opentui/solid/jsx-runtime'
  export function createMemo<T>(fn: () => T): () => T
  export function createEffect(fn: () => void): void
  export function For<T>(props: { readonly each: readonly T[]; readonly children: (item: T, index: () => number) => JSX.Element }): JSX.Element
}

// the tests' read of a module's own source (test/pane.test.ts), which Bun has
declare const Bun: { file(url: URL): { text(): Promise<string> } }
