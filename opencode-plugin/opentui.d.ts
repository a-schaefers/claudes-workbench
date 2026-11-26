// What the TUI part's OpenTUI views (splash.tsx, #1333) use of the modules OpenCode's TUI hands a
// plugin, declared here as env.d.ts and host.d.ts declare the rest, so `bun check` needs nothing
// installed. OpenCode's TUI gives a plugin its own `solid-js`, `@opentui/solid` and
// `@opentui/core` (tui/src/plugin/runtime-plugin-support.bun.ts: OpenTUI's
// `ensureRuntimePluginSupport`, which rewrites a plugin's imports of them to the host's and runs
// a .tsx through Solid's JSX transform for OpenTUI). Each shape is read from the TUI's own uses
// (tui/src/feature-plugins/*, component/panel-host.tsx, routes/home.tsx). Bun alone (the tests)
// has none of these: a module that imports them is imported only by the TUI, after setup starts.
// A new use adds its line here.

declare module '@opentui/solid/jsx-runtime' {
  export namespace JSX {
    // what Solid's universal renderer inserts: OpenTUI's renderables, text, or nothing
    type Element = { readonly __opentui: never } | string | number | boolean | null | undefined | Element[]
    interface ElementChildrenAttribute { children: {} }
    // @opentui/core RGBA, or a colour string it parses ("#rrggbb")
    type Colour = string | { readonly r: number; readonly g: number; readonly b: number; readonly a: number }
    interface Layout {
      readonly children?: Element
      readonly flexDirection?: 'row' | 'column'
      readonly flexGrow?: number
      readonly flexShrink?: number
      readonly justifyContent?: 'flex-start' | 'flex-end' | 'center'
      readonly width?: number | '100%'
      readonly height?: number
      readonly minWidth?: number
      readonly minHeight?: number
      readonly overflow?: 'hidden' | 'visible'
      readonly onMouseDown?: () => void
      // `this` is the renderable, laid out: its width and height in cells
      readonly onSizeChange?: (this: { readonly width: number; readonly height: number }) => void
    }
    interface IntrinsicElements {
      box: Layout
      text: Layout & { readonly wrapMode?: 'none' | 'word' | 'char'; readonly fg?: Colour }
      // `attributes`: @opentui/core TextAttributes, OR-ed (the panel's bold and underlined runs, #1312)
      span: { readonly children?: Element; readonly style?: { readonly fg?: Colour; readonly bg?: Colour; readonly attributes?: number } }
    }
  }
}

declare module 'solid-js' {
  import type { JSX } from '@opentui/solid/jsx-runtime'
  export type { JSX }
  export type Accessor<T> = () => T
  export function createSignal<T>(value: T): [Accessor<T>, (value: T) => T]
  export function onCleanup(fn: () => void): void
  export function Show<T>(props: { readonly when: T | undefined | null | false; readonly fallback?: JSX.Element; readonly children: JSX.Element }): JSX.Element
  export function Index<T>(props: { readonly each: readonly T[]; readonly children: (item: Accessor<T>, index: number) => JSX.Element }): JSX.Element
}

declare module '@opentui/solid' {
  // every key the renderer reads, whatever has focus; it doesn't take the key from it
  export function useKeyboard(handler: (event: { readonly name: string }) => void): void
}

// the timers and file read the views use, which Bun has
declare function setInterval(fn: () => void, ms: number): number
declare function clearInterval(id: number | undefined): void
declare function queueMicrotask(fn: () => void): void
declare function atob(data: string): string
declare module 'node:fs' {
  export function readFileSync(path: string, encoding: 'utf8'): string
}
