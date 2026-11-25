// Kernel.Phase's phaseOf: each phase wb has (store.py), the parked mark where it counts and where
// it doesn't (store.parked: only in PARKS, #698), and phases no table names.
import type { Golden } from './run.js'

const none = { active: false, running: false, needsClaude: false, waits: false, closed: false }
const running = { ...none, active: true, running: true }
const waiting = { ...none, needsClaude: true, waits: true }

export default {
  module: 'Kernel.Phase',
  cases: [
    { fn: 'phaseOf', in: { phase: 'queued', parked: false }, out: running, why: 'queued counts as running, as the mod’s RUNNING has it' },
    { fn: 'phaseOf', in: { phase: 'running', parked: false }, out: running, why: 'a round under way' },
    { fn: 'phaseOf', in: { phase: 'testing', parked: false }, out: running, why: 'the tests running' },
    { fn: 'phaseOf', in: { phase: 'asking', parked: false }, out: { ...none, active: true, needsClaude: true, waits: true }, why: 'asking is active and needs Claude, not running' },
    { fn: 'phaseOf', in: { phase: 'asking', parked: true }, out: { ...none, active: true, needsClaude: true, waits: true }, why: 'a parked mark counts only in PARKS: an asking task still waits' },
    { fn: 'phaseOf', in: { phase: 'review', parked: false }, out: waiting, why: 'a review waits on Claude' },
    { fn: 'phaseOf', in: { phase: 'stuck', parked: false }, out: waiting, why: 'stuck waits on Claude' },
    { fn: 'phaseOf', in: { phase: 'failed', parked: false }, out: waiting, why: 'failed waits on Claude' },
    { fn: 'phaseOf', in: { phase: 'review', parked: true }, out: { ...none, needsClaude: true }, why: 'parked by a quit: needs Claude, waits for no one (#698)' },
    { fn: 'phaseOf', in: { phase: 'failed', parked: true }, out: { ...none, needsClaude: true }, why: 'a parked failure waits for no one either' },
    { fn: 'phaseOf', in: { phase: 'accepted', parked: false }, out: { ...none, closed: true }, why: 'accepted is closed' },
    { fn: 'phaseOf', in: { phase: 'discarded', parked: true }, out: { ...none, closed: true }, why: 'discarded is closed, a stale mark or not' },
    { fn: 'phaseOf', in: { phase: '', parked: false }, out: none, why: 'no phase: the safe answer, nothing at all' },
    { fn: 'phaseOf', in: { phase: 'Running', parked: false }, out: none, why: 'phases are exact: no case folding' },
    { fn: 'phaseOf', in: { phase: 'paused', parked: true }, out: none, why: 'a phase a newer wb might add: the safe answer, never a throw' },
    { fn: 'phaseOf', in: { phase: 'révïew', parked: false }, out: none, why: 'non-ASCII: no locale, no normalising' },
    { fn: 'phaseOf', in: { phase: 'review\u{1F527}', parked: false }, out: none, why: 'an astral character: both engines compare code units alike' },
  ],
} satisfies Golden
