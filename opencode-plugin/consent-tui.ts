// The TUI's half of the consent gates (#1314, 2026-11-25; consent.ts has the whole of it): on the
// server part's RPC event `ask` it creates the session form with the id the server part picked
// (`client.session.form.create`, which a server plugin can't call in 2.4.1), then says `shown`;
// on `close` it cancels a form nobody answered. Every TUI attached does the same: the second
// create of one id is OpenCode's conflict (409), so one form shows, in the TUI, the web app and a
// paired phone alike. The answer goes from the form to OpenCode's bus (`form.replied`), never
// through here. Nothing runs at import.
import { Consent, formFields, type Ask } from './consent.ts'

// The part of the TUI's context this uses (plugin/src/tui/context.ts: `client` is the promise
// OpenCodeClient: client/src/promise/generated/client.ts `session.form`, client/src/promise/rpc.ts)
export interface ConsentTuiContext {
  readonly client: {
    rpc(definition: typeof Consent): {
      shown(input: { formID: string }): Promise<unknown>
      readonly events: { on(name: 'ask' | 'close', handler: (event: { data: Record<string, unknown> }) => unknown): () => void }
    }
    readonly session: {
      readonly form: {
        create(input: { sessionID: string; id: string; title: string; fields: unknown; metadata?: Record<string, unknown> }): Promise<unknown>
        cancel(input: { sessionID: string; formID: string; message?: string }): Promise<unknown>
      }
    }
  }
}

// a create that failed because another TUI made the form first (the server's ConflictError)
const conflict = (e: unknown) => /\b409\b|conflict|already exists/i.test(String((e as { message?: unknown })?.message ?? e))

export function consentTui(ctx: unknown): (() => void) | undefined {
  const c = ctx as Partial<ConsentTuiContext>
  const rpc = c.client?.rpc?.(Consent)
  const forms = c.client?.session?.form
  if (!rpc?.events?.on || !forms) return undefined
  const offAsk = rpc.events.on('ask', async ev => {
    const a = ev.data as unknown as Ask
    if (!a?.formID || !a.sessionID) return
    try {
      await forms.create({ sessionID: a.sessionID, id: a.formID, title: a.title, fields: formFields(a),
        metadata: { kind: 'workbench.consent' } })
    } catch (e) {
      if (!conflict(e)) return // not made: the server part fails closed
    }
    await rpc.shown({ formID: a.formID }).catch(() => {})
  })
  const offClose = rpc.events.on('close', async ev => {
    const d = ev.data
    await forms.cancel({ sessionID: String(d.sessionID ?? ''), formID: String(d.formID ?? ''),
      message: String(d.message ?? '') }).catch(() => {})
  })
  return () => { offAsk(); offClose() }
}
