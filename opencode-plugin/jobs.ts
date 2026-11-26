// Background jobs on OpenCode's path (#1334; the host-shape decision, #1296, 2026-11-25): what the
// Claude Code mod runs beside its notices, OpenCode-native. Each due rule lives in wb, behind
// `--if-due` (kernel/BOUNDARY.md: routes, the settings offer and scout timing stay in wb); the
// notices' lane (notices.ts) looks at them when a session here goes idle (`experimental.idle`, a
// turn's end, the timer) and shows a toast, or for a settings change with help a turn that offers
// it:
// - the weekly update check: `wb update --check --if-due`, once a load after the first turn here
//   once wb events said hello; `update available …` and the marketplace clone's local changes
//   (patches.CHECK_LINE) toasted, wb's line as it is;
// - the automatic optimize: `wb optimize start --if-due`, once a load, quiet; `measuring …` toasted;
// - the weekly model check: `wb scout --if-due`, once a load, in a server that delegated in this
//   load, wakes on, quiet, a minute after a turn (QUIET_MS: raised at once, it buried the driver's
//   question, the mod's 2026-11-16 audit); wb's offer line toasted (scout.json `offered_at`, which
//   the mod writes too: one offer a due period for both);
// - the web app's settings changes: `wb settings offers --if-due --json` once settings-changes.jsonl
//   grew, into the session the web chat listens to here, else the one the user last typed in, and
//   never while another listener's claim stands (that host says them); a change with help is a
//   queued turn that offers to set it up, and toasted; the rest a note the next turn reads
//   (a steer with `resume: false`: published with the user's next prompt, no turn of its own). Wakes off, the offers are toasted only.
// "Quiet": no task anywhere active, and none of this server's waiting on its driver lately
// (rewakePlan's `recent`): the mod's quietForJobs.
import { ACTIVE, rewakePlan } from '../mod/hooks/core.ts'
import { JOB_MS, noticeId, type NoticeLane } from './notices.ts' // a cycle: used only at call time
import type { ListenerClaim, SettingsOffers } from '../mod/types/wire.d.ts'

export const QUIET_MS = 60_000
export const LOCAL_CHANGES = 'local changes in the marketplace clone' // patches.CHECK_LINE's opening (the mod's)
export const CHANGES = 'settings-changes.jsonl'                       // chat.CHANGES

type Task = { name: string; phase: string; parked: boolean; updatedAt: number }

export class Jobs {
  updateAsked = false
  optimizeAsked = false
  scoutAsked = false
  changesSize = -1   // settings-changes.jsonl's size at the last offers asked

  constructor(readonly lane: NoticeLane) {}

  quiet(now: number, list: readonly Task[]): boolean {
    const l = this.lane
    return !list.some(t => ACTIVE.has(t.phase) || (l.delegated.has(t.name) && rewakePlan({
      phase: t.phase, parked: t.parked, mine: true, updatedAt: t.updatedAt, now, busy: false, wake: l.deps.wake,
      dropped: 0, acked: 0, seen: -1, marked: 0 }).recent))
  }

  async tick(now: number, list: readonly Task[]) {
    const l = this.lane
    if (!l.sawTurn || !l.earsOk) return
    const idle = ![...l.busy.values()].some(Boolean)
    const quiet = this.quiet(now, list)
    if (!this.updateAsked) {
      this.updateAsked = true
      void l.deps.run(['update', '--check', '--if-due'], undefined, JOB_MS).then(r => {
        if (r.code === 0 && (r.out.startsWith('update available') || r.out.includes(LOCAL_CHANGES))) {
          l.toast('update', r.out, '', false)
        }
      }, () => {})
    }
    if (!this.optimizeAsked && quiet) {
      this.optimizeAsked = true
      void l.deps.run(['optimize', 'start', '--if-due'], undefined, JOB_MS).then(r => {
        if (r.code === 0 && r.out.startsWith('measuring')) l.toast('optimize', r.out, '', false)
      }, () => {})
    }
    if (!this.scoutAsked && quiet && idle && l.deps.wake && l.delegated.size && now - l.turnEndedAt >= QUIET_MS) {
      this.scoutAsked = true
      const r = await l.deps.run(['scout', '--if-due'], undefined, JOB_MS)
      if (r.code === 0 && r.out) l.toast('scout', r.out, '', false)
    }
    await this.offers()
  }

  // The session the web's changes are said to: the chat's listener when it is this server's,
  // else the one the user last typed in here; '' while another listener's claim stands, or none
  target(): string {
    const l = this.lane
    let claim: ListenerClaim = {}
    try { claim = JSON.parse(l.deps.read(l.path('chat', 'listener.json')) ?? '{}') ?? {} } catch { /* none */ }
    if (claim && typeof claim.session === 'string' && claim.session) {
      if (claim.tool === 'opencode' && Number(claim.pid) === l.deps.pid) return claim.session
      if (Number(claim.pid) > 0 && l.deps.alive(Number(claim.pid))) return '' // Claude Code's, or another server's
    }
    return l.typed
  }

  async offers() {
    const l = this.lane
    const size = l.deps.size(l.path(CHANGES))
    if (size < 0 || size === this.changesSize) return
    const sid = this.target()
    if (!sid) return // read again once a session here can take them
    const r = await l.deps.run(['settings', 'offers', '--if-due', '--json'], sid, JOB_MS)
    if (r.code !== 0) return
    this.changesSize = size
    let got: SettingsOffers = {}
    try { got = JSON.parse(r.out) } catch { return }
    for (const o of got.offers ?? []) {
      const key = `settings:${String(o.key ?? '')}`
      l.toast(`${key}:${o.ts}`, String(o.toast ?? ''), sid, false)
      if (l.deps.wake) await l.send(sid, offerId(o), String(o.text ?? ''), 'queue', { kind: 'settings', key: o.key, ts: o.ts,
        title: String(o.toast ?? '') })
    }
    const notes = (got.notes ?? []).map(n => String(n.text ?? '')).filter(Boolean)
    if (notes.length) {
      // a steer that doesn't resume: OpenCode publishes pending steers together with the next
      // turn's own (the user's prompt), so the note is read in that turn; a queued one would be
      // promoted alone after it, a turn of its own (2.4.1's inbox.ts promote, the 2026-11-26 review)
      const last: { ts?: number } = (got.notes ?? []).at(-1) ?? {}
      await l.send(sid, offerId({ key: 'notes', ts: last.ts }), `[workbench] ${notes.join('\n')}`, 'steer',
        { kind: 'settings', ts: last.ts, title: 'settings changed on the web app' }, false)
    }
  }
}

// an offer's inbox id, bound to its row (notices.ts's noticeId: idempotent across servers)
const offerId = (o: { key?: unknown; ts?: unknown }) => noticeId(`settings:${String(o.key ?? '')}`, 'offer', Number(o.ts) || 0)
