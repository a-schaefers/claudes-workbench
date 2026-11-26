// Workbench in OpenCode's own places (#1312, #1332; the host-shape decision, #1296): a section of
// the session's sidebar (`sidebar.content`), a status item in the prompt's footer
// (`prompt.footer.status`, in place of Claude Code's strip above the prompt: a press opens the
// panel), and the panel (`session.panel`, name `workbench`), opened when the session's agent
// becomes Workbench (`/workbench`, a server command, or the agent picker) and closed when it
// stops being it. All three draw from the server part's `workbench-pane` RPC (pane.ts); what
// they show is pane-view.ts's, the shared core's and the kernel's.
//
// The panel: the Tasks view's rows (a press on a task's first line follows it), the followed
// task's transcript below, kept at its end as lines come until the user scrolls up and again
// once they scroll back down (OpenTUI's sticky scroll), or its diff (`wb diff`, the separator's
// press or `d`); the apprentice and what it says; the day cap's line; the modeline. The
// transcript is read only while the panel is open: closing it stops the tail (#1332). The
// splash plays at its first opening (splash.tsx), its words falling into the modeline's row.
//
// The TUI part imports this only in setup (tui.ts), never at load: it imports OpenTUI and Solid,
// which only OpenCode's TUI provides.
import { createEffect, createMemo, createSignal, Index, onCleanup, Show, type JSX } from 'solid-js'
import { TextAttributes } from '@opentui/core'
import { Apprentice, mountSplash, PANEL, replaySplash, type Panel, type SplashHost } from './splash.tsx'
import { AGENT, WorkbenchPane, type Link, type Live, type PaneTask, type Tasks } from './pane.ts'
import { WorkbenchDriver } from './driver.ts'
import {
  driverRow, footer, lineRun, LinkPoll, MODELS, modelineOf, panelRows, pixels, Poll, runOf, scoped, separator, sidebar, spinAt, target,
  TOKEN, WHO, wrap, type DriverInfo, type Here, type Run, type Token,
} from './pane-view.ts'
import { capRow, focusOf, frameOf, speechOf } from '../mod/hooks/tasks.ts'
import { CLOSED } from '../mod/hooks/core.ts'
import { COLUMNS, FACE, ROWS } from '../mod/hooks/apprentice.ts'

export const POLL_MS = 2000        // the tasks, while one is open or the panel shows
export const IDLE_MS = 10_000      // the tasks, otherwise
export const TAIL_MS = 2000        // the followed task's transcript, while the panel is open
export const FRAME_MS = 450        // the apprentice's frame (the mod's FRAME_MS)
export const SETTLE_MS = 500       // after the agent's selection, the TUI's own model selection follows it before the notes are asked
const HIDDEN = new Set(['activity', 'clock', 'meta', 'routed'])

type Colour = { readonly r: number; readonly g: number; readonly b: number; readonly a: number }
type Feedback = 'success' | 'warning' | 'error' | 'info'
/** What the pane reads of OpenCode's TUI context (plugin/src/tui/context.ts `Context`) beside
 * host.d.ts's TuiContext and splash.tsx's SplashHost. */
export interface PaneHost {
  readonly theme: {
    readonly text: { readonly base: Colour; readonly muted: Colour; readonly faint: Colour
      readonly feedback: Readonly<Record<Feedback, { readonly base: Colour }>> }
    readonly hue: Readonly<Record<'accent' | 'interactive', Readonly<Record<400, Colour>>>>
    readonly background: { readonly base: Colour }
    readonly diff: { readonly background: { readonly added: Colour; readonly removed: Colour } }
  }
  readonly client: { readonly rpc: (definition: object) => Record<string, (input?: unknown) => Promise<unknown>> }
  readonly data: {
    on(type: 'session.agent.selected', handler: (e: { readonly data: { readonly sessionID: string; readonly agent: string; readonly previous?: string } }) => void): () => void
    readonly session: { get(sessionID: string): { readonly agent?: string; readonly location?: { readonly directory?: string } } | undefined }
  }
  readonly keymap: {
    layer(factory: () => {
      readonly mode?: string
      readonly enabled?: boolean | (() => boolean)
      readonly commands: readonly { readonly id?: string; readonly title?: string; readonly group?: string; readonly palette?: true
        readonly bind?: string; readonly run: () => void }[]
    }): void
    // a reachable command by id (OpenCode's own "Open plugins" action dispatches `plugins.list` so)
    dispatch?(id: string): void
  }
  readonly ui: {
    // the prompt's selected model, reactive (2.4.1; none in an older TUI): the next turn's driver
    readonly model?: { current(): { readonly providerID: string; readonly modelID: string; readonly variant?: string } | undefined }
    readonly toast: { show(o: { readonly title?: string; readonly message: string; readonly variant?: 'info' | 'warning' }): void }
    readonly panel: {
      open(name: string): boolean
      close(): void
      current(): { readonly name: string; readonly sessionID: string } | undefined
    }
    readonly router: { current(): { readonly type: string; readonly sessionID?: string } }
    readonly slot: (claim: { readonly append: 'sidebar.content' | 'prompt.footer.status'
      readonly render: (input: { readonly sessionID?: string }) => JSX.Element }) => () => void
  }
}
type Host = SplashHost & PaneHost
// the panel's slot input (plugin/src/tui/context.ts `PanelInput`), beyond splash.tsx's Panel
type PanelInput = Panel & { readonly close?: () => void }

/** A token's colour in the theme; none for a token the theme lacks (an older OpenCode). */
export function colour(theme: PaneHost['theme'], token: Token | undefined): Colour | undefined {
  switch (token) {
    case undefined: return undefined
    case 'accent': case 'interactive': return theme.hue?.[token]?.[400]
    case 'base': case 'muted': case 'faint': return theme.text?.[token]
    case 'added': case 'removed': return theme.diff?.background?.[token]
    default: return theme.text?.feedback?.[token]?.base
  }
}
const attrs = (r: Run) => (r.bold ? TextAttributes.BOLD : 0) | (r.italic ? TextAttributes.ITALIC : 0) |
  (r.underline ? TextAttributes.UNDERLINE : 0)

/** One row of runs: a <text> of <span>s, never wrapped (the rows are cut to the width already). */
function Runs(props: { readonly theme: PaneHost['theme']; readonly runs: readonly Run[]; readonly onPress?: () => void }): JSX.Element {
  return (
    <text wrapMode="none" flexShrink={0} onMouseUp={props.onPress}>
      <Index each={props.runs}>
        {r => <span style={{ fg: colour(props.theme, r().fg), bg: colour(props.theme, r().bg), attributes: attrs(r()) }}>{r().text}</span>}
      </Index>
    </text>
  )
}

/** The tasks as the server part last read them, polled for the whole TUI: the sidebar, the footer
 * and the panel all draw from it. */
class Feed {
  readonly state = createSignal<Tasks | null>(null)
  readonly link = createSignal<Link>({ state: 'checking' })
  private readonly loop: Poll<Tasks>
  // the web app's link, for the modeline: asked when the panel draws and each LINK_MS while it shows (#1518)
  private readonly links: LinkPoll
  constructor(private readonly rpc: Record<string, (input?: unknown) => Promise<unknown>>, private readonly busy: () => boolean) {
    this.loop = new Poll(() => this.rpc.tasks!({}) as Promise<Tasks>, t => this.state[1](t), () => {
      const open = (this.state[0]()?.tasks ?? []).some(t => !CLOSED.has(t.phase))
      return open || this.busy() ? POLL_MS : IDLE_MS
    })
    this.links = new LinkPoll(() => this.rpc.link!({}) as Promise<Link>, l => this.link[1](l), busy)
  }
  start() { void this.loop.now() }
  stop() { this.loop.stop(); this.links.stop() }
  /** Read now (a press that opens the panel): still the one loop. */
  poll() { return this.loop.now() }
  wantLink() { void this.links.want() }
}

const hereOf = (host: Host, sessionID: string): Here => ({
  sessionID, dir: host.data.session.get(sessionID)?.location?.directory ?? '',
  home: process.env.HOME ?? '', fold: process.platform === 'darwin',
})
const modeOf = (host: Host, sessionID: string | undefined) => !!sessionID && host.data.session.get(sessionID)?.agent === AGENT
// the TUI's selected model as OpenCode writes a ref, `provider/id#variant`; '' in a TUI without `ui.model`
const selectedRef = (host: Host): string => {
  const m = host.ui.model?.current()
  return m ? `${m.providerID}/${m.modelID}${m.variant ? `#${m.variant}` : ''}` : ''
}

/** The panel's page: the tasks, the followed task's transcript or diff, the apprentice, the cap
 * and the modeline, pinned to the panel's foot. */
function Page(props: { readonly host: Host; readonly feed: Feed; readonly panel: PanelInput
  readonly picked: () => string; readonly pick: (task: string) => void }): JSX.Element {
  const host = props.host, theme = host.theme
  const rpc = host.client.rpc(WorkbenchPane)
  const [size, setSize] = createSignal({ w: Math.max(30, props.panel.width || 60), h: 24 })
  const [live, setLive] = createSignal<Live | null>(null)
  const [view, setView] = createSignal<'transcript' | 'diff'>('transcript')
  const [followed, setFollowed] = createSignal('')
  const [tick, setTick] = createSignal(0)
  const [driver, setDriver] = createSignal<DriverInfo | null>(null)
  const state = () => props.feed.state[0]()
  const tasks = (): PaneTask[] => state()?.tasks ?? []
  const now = () => state()?.now ?? Date.now()
  const cols = () => Math.max(30, size().w)
  const focus = () => focusOf(tasks(), followed(), props.picked())
  props.feed.wantLink()
  // the driver row (#1316): wb's answer for the model the TUI has selected, asked again at each
  // change of it (the row's "change", OpenCode's own /models, the pin), as OpenCode commits a pick
  // to the session only at its next prompt (seen live, 2026-11-26); an answer to an older ask drops
  let asked = 0
  createEffect(() => {
    const model = selectedRef(host), mine = ++asked
    void (host.client.rpc(WorkbenchDriver).driver!({ sessionID: props.panel.sessionID, ...(model ? { model } : {}) }) as Promise<DriverInfo>)
      .then(d => { if (mine === asked) setDriver(d && typeof d === 'object' ? d : null) }, () => {})
  })

  // the tail: only while this page is drawn, so closing the panel stops it (#1332). One loop: a
  // press (a task, the separator, `d`) bumps `gen` and starts it again at once, and an answer
  // to an older press is dropped, so a transcript still on its way never covers the diff asked
  // for after it (seen in the live check, 2026-11-26), nor do presses stack up loops.
  let stopped = false
  let timer: ReturnType<typeof setTimeout> | undefined
  let shown = ''
  let gen = 0
  const tail = async () => {
    const mine = ++gen
    if (timer) clearTimeout(timer)
    const f = focus(), want = view()
    try {
      if (!f) { setLive(null); shown = '' }
      else if (want === 'diff') {
        const key = `diff:${f.name}@${f.updatedAt}`
        if (key !== shown) {
          const got = await rpc.diff!({ task: f.name }) as Live
          if (mine === gen) { setLive(got); shown = key }
        }
      } else {
        const got = await rpc.live!({ task: f.name }) as Live
        const key = `live:${f.name}:${JSON.stringify(got.lines.at(-1) ?? null)}:${got.lines.length}:${got.why ?? ''}`
        if (mine === gen && key !== shown) { setLive(got); shown = key }
      }
      if (f && mine === gen) setFollowed(f.name)
    } catch { /* the server part is restarting: again next time */ }
    if (!stopped && mine === gen) timer = setTimeout(() => void tail(), TAIL_MS)
  }
  void tail()
  const frames = setInterval(() => setTick(tick() + 1), FRAME_MS)
  onCleanup(() => { stopped = true; gen++; if (timer) clearTimeout(timer); clearInterval(frames) })
  const toggle = () => { setView(view() === 'diff' ? 'transcript' : 'diff'); shown = ''; void tail() }
  const follow = (task: string) => { props.pick(task); setView('transcript'); shown = ''; void tail() }

  // Escape closes it and `d` shows the diff, while the panel has the keyboard
  host.keymap.layer(() => ({
    enabled: () => props.panel.focused,
    commands: [
      { bind: 'escape', title: 'close Workbench', run: () => props.panel.close?.() },
      { bind: 'd', title: 'diff or transcript', run: toggle },
    ],
  }))

  const lines = () => (live()?.task === focus()?.name ? live()?.lines ?? [] : [])
  const history = () => lines().filter(l => !HIDDEN.has(l.s))
  const said = (s: string) => lines().find(l => l.s === s)?.t ?? ''
  const pix = () => pixels(size().h, cols())
  const speech = () => speechOf(focus(), said('activity'), tasks(), modeOf(host, props.panel.sessionID), WHO)
  const busy = () => speech().pose === 'work' || speech().pose === 'think'
  const room = () => Math.max(10, pix() ? cols() - COLUMNS - 4 : cols() - 8)
  const bubble = () => {
    const sp = speech(), max = pix() ? ROWS - 2 : 1
    const say = wrap(`${busy() && said('activity') ? `${spinAt(now())} ` : ''}${sp.say}`, room(), max)
    const more = wrap([sp.more, focus()?.name === followed() ? said('clock') : ''].filter(Boolean).join(' · '), room(), Math.max(0, max - say.length))
    return { say, more, colour: colour(theme, TOKEN[sp.color]) }
  }
  const cap = () => {
    const c = state()?.cap
    return c ? capRow(tasks(), c.spent, c.cap, cols(), undefined, c.est) : null
  }
  const ml = () => modelineOf(props.feed.link[0](), cols())

  return (
    <box flexDirection="column" flexGrow={1} minHeight={0} minWidth={0} paddingX={1}
      onSizeChange={function () { setSize({ w: this.width - 2, h: this.height }) }}>
      <Show when={driverRow(driver(), cols())}>
        <Runs theme={theme} runs={driverRow(driver(), cols()) ?? []} onPress={() => host.keymap.dispatch?.(MODELS)} />
      </Show>
      <box flexDirection="column" flexShrink={0} maxHeight={Math.max(3, Math.floor(size().h / 2))} overflow="hidden">
        <Index each={panelRows(tasks(), cols(), size().h, now(), followed())}>
          {r => <Runs theme={theme} runs={r().runs} onPress={r().first ? () => follow(r().task) : undefined} />}
        </Index>
      </box>
      <Show when={focus()} fallback={<box flexGrow={1} minHeight={0} />}>
        <Runs theme={theme} runs={[{ text: separator(focus()?.name ?? '', view(), cols()), fg: 'muted' }]} onPress={toggle} />
        <Show when={said('meta')}><text wrapMode="none" fg={theme.text.muted}>{said('meta')}</text></Show>
        <Show when={said('routed')}><text wrapMode="none" fg={theme.text.muted}>{said('routed')}</text></Show>
        <Show when={live()?.why}><text wrapMode="word" fg={colour(theme, 'error')}>{live()?.why ?? ''}</text></Show>
        <scrollbox flexGrow={1} minHeight={0} stickyScroll stickyStart="bottom" scrollbarOptions={{ visible: false }}>
          <Index each={history()}>
            {l => {
              // a memo, never a const: an Index row is made once for its index and only its item
              // changes, so a line read once here kept the text it was first drawn with (#1472: `d`
              // showed the transcript under the diff's separator, a 600-line tail froze, 2026-11-26)
              const r = createMemo(() => lineRun(l()))
              return <text wrapMode={view() === 'diff' ? 'none' : 'word'} fg={colour(theme, r().fg)}>
                <span style={{ bg: colour(theme, r().bg), attributes: attrs(r()) }}>{r().text}</span>
              </text>
            }}
          </Index>
        </scrollbox>
      </Show>
      <box flexDirection="row" flexShrink={0} height={pix() ? ROWS : 1} marginTop={0}>
        <Show when={pix()} fallback={<text wrapMode="none" fg={bubble().colour}>{`${FACE[speech().pose]} `}</text>}>
          <Apprentice theme={theme} pose={frameOf(speech().pose, tick())} />
        </Show>
        <box flexDirection="column" flexGrow={1} minWidth={0} paddingLeft={1} border={pix()} borderStyle="rounded"
          borderColor={bubble().colour}>
          <Index each={bubble().say}>{l => <text wrapMode="none" fg={bubble().colour}><span style={{ attributes: TextAttributes.BOLD }}>{l()}</span></text>}</Index>
          <Index each={bubble().more}>{l => <text wrapMode="none" fg={theme.text.muted}>{l()}</text>}</Index>
        </box>
      </box>
      <Show when={cap()}>
        <Runs theme={theme} runs={(cap() ?? []).map(runOf)} />
      </Show>
      <box flexDirection="column" flexShrink={0} backgroundColor={colour(theme, 'accent')}>
        <text wrapMode="none" fg={theme.background.base} paddingX={1}><span style={{ attributes: TextAttributes.BOLD }}>{ml().bar}</span></text>
        <Index each={ml().rows}>{l => <text wrapMode="none" fg={theme.background.base}>{l()}</text>}</Index>
      </box>
    </box>
  )
}

/** The sidebar's Workbench section: its counts, a line a task; a press opens the panel on it. */
function Section(props: { readonly host: Host; readonly feed: Feed; readonly sessionID: string; readonly open: (task: string) => void }): JSX.Element {
  const [w, setW] = createSignal(38)
  const theme = props.host.theme
  const shown = () => {
    const s = props.feed.state[0]()
    if (!s) return null
    const mode = modeOf(props.host, props.sessionID)
    return sidebar(scoped(s.tasks, hereOf(props.host, props.sessionID), mode), w(), s.now, spinAt(s.now), '')
  }
  return (
    <Show when={shown()}>
      <box flexDirection="column" onSizeChange={function () { setW(this.width) }}>
        <Runs theme={theme} runs={shown()?.head ?? []} onPress={() => props.open('')} />
        <Index each={shown()?.rows ?? []}>
          {r => <Runs theme={theme} runs={r().runs} onPress={() => props.open(r().task)} />}
        </Index>
      </box>
    </Show>
  )
}

/** The footer's status item (#1332: in place of the strip): a press opens or closes the panel. */
function Footer(props: { readonly host: Host; readonly feed: Feed; readonly sessionID: string; readonly toggle: () => void }): JSX.Element {
  const runs = () => {
    const s = props.feed.state[0]()
    const mode = modeOf(props.host, props.sessionID)
    return footer(scoped(s?.tasks ?? [], hereOf(props.host, props.sessionID), mode), mode)
  }
  return (
    <Show when={runs()}>
      <box flexShrink={0} marginTop={0} paddingLeft={1}>
        <Runs theme={props.host.theme} runs={runs() ?? []} onPress={props.toggle} />
      </box>
    </Show>
  )
}

/**
 * The sidebar section, the footer item and the panel, the palette's command, and the panel's
 * opening at the mode's start (its close at its end). `home`: wb's state dir (the splash's
 * splash.json). Returns their removal.
 */
export function mountPane(host: Host, home: string): () => void {
  const rpc = host.client.rpc(WorkbenchPane)
  const [picked, pick] = createSignal('')
  const panelOpen = () => host.ui.panel.current()?.name === PANEL
  const feed = new Feed(rpc, panelOpen)
  const open = (task = '') => {
    if (task) pick(task)
    if (!panelOpen()) host.ui.panel.open(PANEL)
    void feed.poll()
  }
  const toggle = () => (panelOpen() ? host.ui.panel.close() : open())
  const stops: (() => void)[] = []
  stops.push(mountSplash(host, home,
    panel => <Page host={host} feed={feed} panel={panel as PanelInput} picked={picked} pick={pick} />,
    (rows, cols) => { feed.wantLink(); return target(rows, cols, feed.link[0](), !!feed.state[0]()?.cap) }))
  stops.push(host.ui.slot({
    append: 'sidebar.content',
    render: input => <Section host={host} feed={feed} sessionID={input.sessionID ?? ''} open={open} />,
  }))
  stops.push(host.ui.slot({
    append: 'prompt.footer.status',
    render: input => (input.sessionID ? <Footer host={host} feed={feed} sessionID={input.sessionID} toggle={toggle} /> : null),
  }))
  // global, as OpenCode's own /diff: the palette lists what is reachable while it is open
  host.keymap.layer(() => ({
    mode: 'global',
    commands: [{ id: 'workbench.panel', title: 'Workbench: show or hide its panel', group: 'Workbench', palette: true, run: toggle }],
  }))
  // the mode's start opens the panel and says the driver's notes once; its end closes it, and the
  // next start plays the splash again (#963's rule, as the mod's quit)
  stops.push(host.data.on('session.agent.selected', e => {
    const { sessionID, agent, previous } = e.data
    const route = host.ui.router.current()
    const here = route.type === 'session' && route.sessionID === sessionID
    if (agent === AGENT && previous !== AGENT && here) {
      open()
      // asked only for the session on screen (--once records each note as said, so one asked for a
      // session elsewhere would be spent unseen), of the model the TUI selects once it follows the
      // new agent (SETTLE_MS)
      setTimeout(() => {
        const model = selectedRef(host)
        void (host.client.rpc(WorkbenchDriver).driver!({ sessionID, once: true, ...(model ? { model } : {}) }) as Promise<{ notes?: unknown }>)
          .then(d => {
            const notes = Array.isArray(d?.notes) ? d.notes.map(n => String((n as { text?: unknown })?.text ?? n)).filter(Boolean) : []
            if (notes.length) host.ui.toast.show({ title: 'Workbench', message: notes.join(' '), variant: 'info' })
          }, () => {})
      }, SETTLE_MS)
    } else if (previous === AGENT && agent !== AGENT) {
      if (here && panelOpen()) host.ui.panel.close()
      replaySplash(host)
    }
  }))
  feed.start()
  return () => {
    feed.stop()
    for (const stop of stops.reverse()) {
      try { stop() } catch { /* already gone */ }
    }
  }
}
