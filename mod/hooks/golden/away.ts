// Kernel.Away's awayNow: whether the user is away from the terminal, as the mod's register.test.tsx
// held it before the move (#110, #959, #1402), so the OpenCode plugin judges it alike (#1325's M2).
import type { Golden } from './run.js'

const hour = 3600_000
const t0 = 1_800_000_000_000
const task = (phase: string, mine = true, parked = false) => ({ phase, parked, mine })

export default {
  module: 'Kernel.Away',
  cases: [
    { fn: 'awayNow', in: { webAt: t0, now: t0 + 5 * hour, tasks: [] }, out: true, why: 'within six hours of the web chat' },
    { fn: 'awayNow', in: { webAt: t0, now: t0 + 6 * hour, tasks: [] }, out: false, why: 'six hours on, it has lapsed' },
    { fn: 'awayNow', in: { webAt: t0, now: t0 + 7 * hour, tasks: [task('running')] }, out: true, why: 'a night’s batch runs on past six hours (#110)' },
    { fn: 'awayNow', in: { webAt: t0, now: t0 + 7 * hour, tasks: [task('queued')] }, out: true, why: 'a queued task is open too' },
    { fn: 'awayNow', in: { webAt: t0, now: t0 + 7 * hour, tasks: [task('review')] }, out: true, why: 'or one waits on Claude (#1402)' },
    { fn: 'awayNow', in: { webAt: t0, now: t0 + 7 * hour, tasks: [task('review', true, true)] }, out: false, why: 'a parked one waits for no one' },
    { fn: 'awayNow', in: { webAt: t0, now: t0 + 7 * hour, tasks: [task('accepted')] }, out: false, why: 'done: lapsed' },
    { fn: 'awayNow', in: { webAt: t0, now: t0 + 7 * hour, tasks: [task('running', false)] }, out: false, why: 'another session’s task holds no one away' },
    { fn: 'awayNow', in: { webAt: t0, now: t0 + 7 * hour, tasks: [task('paused')] }, out: false, why: 'a phase no table names holds no one either' },
    { fn: 'awayNow', in: { webAt: 0, now: t0, tasks: [task('running')] }, out: false, why: 'never used the web chat, or back at the terminal since' },
  ],
} satisfies Golden
