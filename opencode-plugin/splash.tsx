// The apprentice and the opening animation on OpenTUI (#1333, slice 4c of #1296): spans.ts's rows
// drawn as <span>s in `ctx.theme`'s colours, in Workbench's panel (`session.panel`, name
// `workbench`). The animation plays the first time the panel opens in a TUI (#963's rule: a
// reload or a reopen never replays it; `replaySplash` lets the mode's end arm it again), unless
// the user turned it off (`wb splash off`, splash.json; the web app's Settings); then the page.
// A key or a click skips it and still acts, closing the panel ends it, a resize plans it again,
// a panel too small for it shows the page at once (splash.ts `splashFont`).
//
// The TUI part imports this only in setup (tui.ts), never at load: it imports OpenTUI and Solid,
// which only OpenCode's TUI provides.
import { createEffect, createSignal, Index, onCleanup, Show, type JSX } from 'solid-js'
import { useKeyboard } from '@opentui/solid'
import { readFileSync } from 'node:fs'
import { apprentice, Play, restTarget, SPLASH_MS, splashOn } from './spans.ts'
import type { Paint, Pose, Span, Target, Token } from './spans.ts'

/** The panel's name: `ui.panel.open(PANEL)` opens Workbench's panel (#1312). */
export const PANEL = 'workbench'
const WAIT = 25 // ticks (2 s) the splash waits for the panel's first layout, as the mod's SPLASH_WAIT

type Colour = { readonly r: number; readonly g: number; readonly b: number; readonly a: number }
/** What the views read of OpenCode's TUI context (plugin/src/tui/context.ts `Context`; theme/src/tui/types.ts
 * `ResolvedThemeTokens`), beside host.d.ts's `TuiContext`. */
export interface SplashHost {
  readonly theme: {
    readonly text: {
      readonly faint: Colour
      readonly feedback: Readonly<Record<'success' | 'warning' | 'error', { readonly base: Colour }>>
    }
    // the theme's semantic hues, each a scale of steps 100-900 that flips with its dark and light modes
    readonly hue: { readonly accent: Readonly<Record<400, Colour>> }
  }
  // `storage.memory`: survives a plugin's hot reload, gone when the TUI exits
  readonly storage: {
    memory<V extends object>(key: string, options: { readonly initial: V }): readonly [V, (mutation: (draft: V) => void) => void]
  }
  readonly ui: {
    // `ui.slot`, the `session.panel` slot and its input (`PanelInput`)
    readonly slot: (claim: { readonly append: 'session.panel'; readonly render: (panel: Panel) => JSX.Element }) => () => void
  }
}
export interface Panel {
  readonly name: string
  readonly sessionID: string
  readonly width: number
  readonly focused: boolean
}

/** A theme colour for each token. The accent (the bar, the coral lamp) is the theme's accent hue at
 * step 400, the feedback colours' step (the primary action's background is "transparent" in
 * OpenCode's own theme: seen live, 2026-11-25). */
function themed(theme: SplashHost['theme'], token: Token): Colour {
  switch (token) {
    case 'accent': return theme.hue.accent[400]
    case 'faint': return theme.text.faint
    default: return theme.text.feedback[token].base
  }
}
const hex = (rgb: number) => '#' + rgb.toString(16).padStart(6, '0')
const colour = (theme: SplashHost['theme'], p: Paint) =>
  p === null ? undefined : typeof p === 'number' ? hex(p) : themed(theme, p.token)

/** Rows of spans, one <text> a row, none wrapped. */
export function Spans(props: { readonly theme: SplashHost['theme']; readonly rows: Span[][] }): JSX.Element {
  return (
    <box flexDirection="column" flexShrink={0}>
      <Index each={props.rows}>
        {row => (
          <text wrapMode="none" flexShrink={0}>
            <Index each={row()}>
              {s => <span style={{ fg: colour(props.theme, s().fg), bg: colour(props.theme, s().bg) }}>{s().text}</span>}
            </Index>
          </text>
        )}
      </Index>
    </box>
  )
}

/** The apprentice in a pose (apprentice.ts), its lamp the theme's colour for the phase. */
export function Apprentice(props: { readonly theme: SplashHost['theme']; readonly pose: Pose }): JSX.Element {
  return <Spans theme={props.theme} rows={apprentice(props.pose)} />
}

/** One play of the animation, filling its box; `onDone` once, after the last frame or a skip. */
export function Splash(props: { readonly theme: SplashHost['theme']; readonly target?: (rows: number, cols: number) => Target; readonly onDone: () => void }): JSX.Element {
  const play = new Play()
  const [frame, setFrame] = createSignal<Span[][]>([])
  let waited = 0
  let timer: number | undefined
  const stop = () => {
    if (timer !== undefined) clearInterval(timer)
    timer = undefined
  }
  const end = () => {
    if (timer === undefined) return
    stop()
    play.end()
    queueMicrotask(props.onDone) // not inside OpenTUI's layout or key dispatch
  }
  timer = setInterval(() => {
    if (!play.plan) {
      if (++waited > WAIT) end()
      return
    }
    if (play.step()) setFrame(play.frame() ?? [])
    else end()
  }, SPLASH_MS)
  onCleanup(stop) // the panel closed: nothing of it runs after
  useKeyboard(end)
  return (
    <box flexGrow={1} minWidth={0} minHeight={0} overflow="hidden" onMouseDown={end}
      onSizeChange={function () {
        if (play.fit(this.width, this.height, props.target?.(this.height, this.width) ?? restTarget(this.height))) setFrame(play.frame() ?? [])
        else end()
      }}>
      <Spans theme={props.theme} rows={frame()} />
    </box>
  )
}

const MEMORY = 'splash'
/** The splash plays at the panel's next opening again (the mode's end: the mod's `splashed` atom). */
export function replaySplash(host: SplashHost) {
  host.storage.memory(MEMORY, { initial: { played: false } })[1](d => { d.played = false })
}

/** The page, after the splash when this is the panel's first opening in this TUI. */
export function SplashThen(props: {
  readonly host: SplashHost
  readonly home: string // wb's state dir, where splash.json is
  readonly target?: (rows: number, cols: number) => Target
  readonly children: JSX.Element
}): JSX.Element {
  const [seen, mark] = props.host.storage.memory(MEMORY, { initial: { played: false } })
  const play = !seen.played && splashOn(() => readFileSync(`${props.home}/splash.json`, 'utf8'))
  if (play) mark(d => { d.played = true })
  const [playing, setPlaying] = createSignal(play)
  return (
    <Show when={playing()} fallback={props.children}>
      <Splash theme={props.host.theme} target={props.target} onDone={() => setPlaying(false)} />
    </Show>
  )
}

/** Until the Tasks page comes (#1312), the panel's page: the apprentice resting at its bottom left,
 * where the splash walks it (spans.ts `restTarget`). */
function Resting(props: { readonly theme: SplashHost['theme'] }): JSX.Element {
  return (
    <box flexGrow={1} flexDirection="column" justifyContent="flex-end">
      <Apprentice theme={props.theme} pose="rest" />
    </box>
  )
}

/** Nothing drawn: `watch` called with the panel as it changes (its focus), splash or page, and
 * with null once it closes. */
function Watch(props: { readonly panel: Panel; readonly watch: (panel: Panel | null) => void }): JSX.Element {
  createEffect(() => props.watch(props.panel))
  onCleanup(() => props.watch(null))
  return null as unknown as JSX.Element
}

/**
 * Workbench's panel: `page` after the splash. The Tasks page (#1312) passes itself and where it
 * puts the apprentice and the modeline in a panel of that many rows and columns (`target`,
 * splash.ts `Target`), so the words fall into the modeline's row; without one they rise away.
 * `watch`: the panel's input as it changes while it shows, the splash's time too (pane.tsx hands
 * the keyboard back to the prompt through it, #1509). Returns the slot's removal.
 */
export function mountSplash(host: SplashHost, home: string, page?: (panel: Panel) => JSX.Element,
  target?: (rows: number, cols: number) => Target, watch?: (panel: Panel | null) => void): () => void {
  return host.ui.slot({
    append: 'session.panel',
    render: panel => (
      <Show when={panel.name === PANEL}>
        {watch ? <Watch panel={panel} watch={watch} /> : null}
        <SplashThen host={host} home={home} target={target}>
          {page ? page(panel) : <Resting theme={host.theme} />}
        </SplashThen>
      </Show>
    ),
  })
}
