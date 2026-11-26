// Kernel.Phase's phaseOf: each phase wb has (store.py), the parked mark where it counts and where
// it doesn't (store.parked: only in PARKS, #698), and phases no table names; and openCounts, the
// tally the pane's header and the status line draw from (#1325's M1).
import type { Golden } from './run.js'

const none = { active: false, running: false, working: false, needsClaude: false, waits: false, stopped: false, done: false, closed: false }
const queued = { ...none, active: true, running: true }
const working = { ...queued, working: true }
const review = { ...none, needsClaude: true, waits: true, done: true }
const stopped = { ...none, needsClaude: true, waits: true, stopped: true }
const asking = { ...none, active: true, working: true, needsClaude: true, waits: true }
const closed = { ...none, done: true, closed: true }
const t = (phase: string, parked = false) => ({ phase, parked })
const row = (phase: string, n: number, waiting: number, active: boolean, stopped = false) => ({ phase, n, waiting, active, stopped })

export default {
  module: 'Kernel.Phase',
  cases: [
    { fn: 'phaseOf', in: t('queued'), out: queued, why: 'queued counts as running, as the status line has it, but no round is under way' },
    { fn: 'phaseOf', in: t('running'), out: working, why: 'a round under way' },
    { fn: 'phaseOf', in: t('testing'), out: working, why: 'the tests running' },
    { fn: 'phaseOf', in: t('asking'), out: asking, why: 'asking is active, working and needs Claude, not running' },
    { fn: 'phaseOf', in: t('asking', true), out: asking, why: 'a parked mark counts only in PARKS: an asking task still waits' },
    { fn: 'phaseOf', in: t('review'), out: review, why: 'a review waits on Claude, its work done' },
    { fn: 'phaseOf', in: t('stuck'), out: stopped, why: 'stuck waits on Claude, stopped short' },
    { fn: 'phaseOf', in: t('failed'), out: stopped, why: 'failed waits on Claude, stopped short' },
    { fn: 'phaseOf', in: t('review', true), out: { ...review, waits: false }, why: 'parked by a quit: needs Claude, waits for no one (#698)' },
    { fn: 'phaseOf', in: t('failed', true), out: { ...stopped, waits: false }, why: 'a parked failure waits for no one either' },
    { fn: 'phaseOf', in: t('accepted'), out: closed, why: 'accepted is closed' },
    { fn: 'phaseOf', in: t('discarded', true), out: closed, why: 'discarded is closed, a stale mark or not' },
    { fn: 'phaseOf', in: t(''), out: none, why: 'no phase: the safe answer, nothing at all' },
    { fn: 'phaseOf', in: t('Running'), out: none, why: 'phases are exact: no case folding' },
    { fn: 'phaseOf', in: t('paused', true), out: none, why: 'a phase a newer wb might add: the safe answer, never a throw' },
    { fn: 'phaseOf', in: t('révïew'), out: none, why: 'non-ASCII: no locale, no normalising' },
    { fn: 'phaseOf', in: t('review\u{1F527}'), out: none, why: 'an astral character: both engines compare code units alike' },
    { fn: 'openCounts', in: { tasks: [] }, out: { counts: [], done: 0 }, why: 'no tasks: no rows, none done' },
    {
      fn: 'openCounts',
      in: { tasks: [t('queued'), t('review'), t('running'), t('stuck'), t('running'), t('asking'), t('testing'), t('failed')] },
      out: {
        counts: [row('running', 2, 0, true), row('testing', 1, 0, true), row('asking', 1, 1, true), row('queued', 1, 0, true),
          row('review', 1, 1, false), row('stuck', 1, 1, false, true), row('failed', 1, 1, false, true)],
        done: 1,
      },
      why: 'every open phase in store.OPEN_ORDER, the work under way first, whatever order the tasks come in',
    },
    {
      fn: 'openCounts',
      in: { tasks: [t('review', true), t('review'), t('stuck', true), t('asking', true)] },
      out: { counts: [row('asking', 1, 1, true), row('review', 2, 1, false), row('stuck', 1, 0, false, true)], done: 2 },
      why: 'a parked task is counted, but waits for no one; a parked mark on asking counts for nothing',
    },
    {
      fn: 'openCounts',
      in: { tasks: [t('accepted'), t('discarded'), t('review'), t('paused'), t('')] },
      out: { counts: [row('review', 1, 1, false)], done: 3 },
      why: 'closed tasks are done but in no row; a phase no table names is in neither',
    },
  ],
} satisfies Golden
