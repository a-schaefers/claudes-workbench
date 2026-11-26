// The notices' toasts in OpenCode's terminal UI (#1313): the server part (notices.ts) emits each on
// the `workbench-notices` RPC's `toast` event; this shows it (`ui.toast.show`, with its session, so
// a toast for a session not on screen offers to open it) and, for a task that waits on its driver
// (review, question, stuck, failed), a desktop notification through OpenCode's own attention
// settings while the terminal isn't focused (`attention.notify`). Each key once: a server emits
// from every instance it set up (one per location), and the TUI may hear more than one.
import type { TuiContext } from './host.d.ts'
import { WorkbenchNotices, type Toast } from './notices.ts'

// plugin/src/tui/context.ts: `attention.notify`, and the RPC client's `events.on`
// (client/src/promise/rpc.ts), the parts used here, each optional on an older TUI
interface NoticesTui {
  readonly ui: { readonly toast: { show(o: { message: string; title?: string; variant?: Toast['variant']; sessionID?: string }): void } }
  readonly attention?: { notify(o: { title?: string; message: string; notification?: boolean | { when?: 'always' | 'focused' | 'blurred' } }): Promise<unknown> }
  readonly client: { rpc(d: typeof WorkbenchNotices): { readonly events?: {
    on(name: 'toast', handler: (e: { data?: Partial<Toast> }) => void): () => void } } }
}

export const SEEN_KEEP = 200

export function shown(ctx: NoticesTui, seen: string[], t: Partial<Toast>) {
  const key = String(t.key ?? '')
  if (!key || !t.message || seen.includes(key)) return
  seen.push(key)
  if (seen.length > SEEN_KEEP) seen.splice(0, SEEN_KEEP / 2)
  ctx.ui.toast.show({ title: t.title || 'Workbench', message: t.message, variant: t.variant ?? 'info',
    ...(t.sessionID ? { sessionID: t.sessionID } : {}) })
  if (t.notify) void ctx.attention?.notify({ title: t.title || 'Workbench', message: t.message,
    notification: { when: 'blurred' } }).catch(() => {})
}

// Returns the cleanup; nothing when this TUI's client has no RPC events
export function noticesTui(ctx: TuiContext): () => void {
  const c = ctx as unknown as NoticesTui
  const seen: string[] = []
  try {
    const off = c.client.rpc(WorkbenchNotices).events?.on('toast', e => { shown(c, seen, e?.data ?? {}) })
    return off ?? (() => {})
  } catch {
    return () => {}
  }
}
