// Kernel.Notice's noticesDue, noticePlan and rewakePlan: which notices are due, what a host does
// with each, and the rewake net, as the mod's deliver and rewake did before the move (#1325's M3,
// M4), so the OpenCode plugin's notices (#1313) follow the same plan.
import type { Golden } from './run.js'

const min = 60_000
const hour = 60 * min
const now = 1_800_000_000_000
const row = (task: string, kind: string, agoMs: number, tasks: string[] = []) => ({ task, kind, ts: (now - agoMs) / 1000, tasks })
const plan = {
  kind: 'review', hasId: false, phase: 'review', questionOpen: false, ts: 100, acked: 50,
  own: true, wake: true, busy: false, toasted: false, heldAsk: false,
}
const rw = {
  phase: 'review', parked: false, mine: true, updatedAt: now - 5 * min, now, busy: false, wake: true,
  dropped: 0, acked: 0, seen: -1, marked: 0,
}
const at = (s: number) => (now - s) / 1000

export default {
  module: 'Kernel.Notice',
  cases: [
    { fn: 'noticesDue', in: { rows: [], startedAt: now, now, runs: false }, out: { due: [], noticed: [] }, why: 'no notices: nothing due' },
    {
      fn: 'noticesDue',
      in: { rows: [row('a', 'checkin', 3 * min), row('b', 'review', 2 * min), row('a', 'question', 2 * min), row('a', 'question', min), row('a', 'review', 0)], startedAt: now - hour, now, runs: false },
      out: { due: [{ task: 'a', picks: [2, 3, 4] }, { task: 'b', picks: [1] }], noticed: [{ task: 'a', ts: at(0) }, { task: 'b', ts: at(2 * min) }] },
      why: 'per task its every question and its newest notice, in the order the tasks came due (the 2026-11-16 audit)',
    },
    {
      fn: 'noticesDue',
      in: { rows: [row('a', 'review', 2 * hour), row('c', 'stuck', 30 * min, ['c', 'd'])], startedAt: now - 3 * hour, now, runs: false },
      out: { due: [{ task: 'c', picks: [1] }], noticed: [{ task: 'a', ts: at(2 * hour) }, { task: 'c', ts: at(30 * min) }, { task: 'd', ts: at(30 * min) }] },
      why: 'over an hour old wakes no one but still counts as noticed; a batch counts for every task it names',
    },
    {
      fn: 'noticesDue',
      in: { rows: [row('a', 'review', 10 * min)], startedAt: now + 20 * min, now, runs: false },
      out: { due: [{ task: 'a', picks: [0] }], noticed: [{ task: 'a', ts: at(10 * min) }] },
      why: 'the hour runs from the later of the load’s start and now',
    },
    {
      fn: 'noticesDue',
      in: { rows: [row('nightly', 'run_done', 270 * min), row('old', 'run_done', 13 * hour)], startedAt: now, now, runs: true },
      out: { due: [{ task: 'nightly', picks: [0] }], noticed: [{ task: 'nightly', ts: at(270 * min) }, { task: 'old', ts: at(13 * hour) }] },
      why: 'a run’s notice is due twelve hours, so a flow done overnight is told in the morning (#1493)',
    },
    { fn: 'noticePlan', in: plan, out: { current: true, toast: true, act: 'wake' }, why: 'a review of this session’s, still true, idle: toast and wake' },
    { fn: 'noticePlan', in: { ...plan, toasted: true }, out: { current: true, toast: false, act: 'wake' }, why: 'toasted once only' },
    { fn: 'noticePlan', in: { ...plan, acked: 100 }, out: { current: true, toast: false, act: 'skip' }, why: 'acknowledged already: nothing, not even a toast' },
    { fn: 'noticePlan', in: { ...plan, own: false }, out: { current: true, toast: true, act: 'none' }, why: 'another session’s: the toast only, left for its own session or wb wait' },
    { fn: 'noticePlan', in: { ...plan, wake: false }, out: { current: true, toast: true, act: 'none' }, why: 'wakes off: the toast only' },
    { fn: 'noticePlan', in: { ...plan, busy: true }, out: { current: true, toast: true, act: 'hold' }, why: 'busy: a review waits for the turn to end' },
    { fn: 'noticePlan', in: { ...plan, busy: true, kind: 'question', hasId: true, questionOpen: true, phase: 'asking' }, out: { current: true, toast: true, act: 'steer' },
      why: 'busy: a question goes into the running turn, its agent blocked (#542)' },
    { fn: 'noticePlan', in: { ...plan, phase: 'running' }, out: { current: false, toast: false, act: 'ack' }, why: 'sent back since: no longer true, acknowledged in silence' },
    { fn: 'noticePlan', in: { ...plan, busy: true, phase: 'accepted' }, out: { current: false, toast: false, act: 'ack' }, why: 'closed since, busy or not: acknowledged' },
    { fn: 'noticePlan', in: { ...plan, kind: 'question', hasId: true, phase: 'asking', questionOpen: false }, out: { current: false, toast: false, act: 'ack' },
      why: 'a question answered or dropped by the runner woke Claude for nobody (the 0.90 review)' },
    { fn: 'noticePlan', in: { ...plan, kind: 'question', hasId: false, phase: 'asking' }, out: { current: true, toast: true, act: 'wake' }, why: 'an old question without an id stands while the task asks' },
    { fn: 'noticePlan', in: { ...plan, kind: 'question', hasId: true, phase: 'running', heldAsk: true }, out: { current: false, toast: false, act: 'missed' },
      why: 'a question held through the turn, its task still open: said if it timed out unseen (#542)' },
    { fn: 'noticePlan', in: { ...plan, kind: 'question', hasId: true, phase: 'accepted', heldAsk: true }, out: { current: false, toast: false, act: 'ack' },
      why: 'its task closed since: nothing to say' },
    { fn: 'noticePlan', in: { ...plan, kind: 'checkin', phase: 'queued' }, out: { current: true, toast: true, act: 'wake' }, why: 'a check-in is news while the task still runs, queued too' },
    { fn: 'noticePlan', in: { ...plan, kind: 'checkin', phase: 'asking' }, out: { current: false, toast: false, act: 'ack' }, why: 'not while it asks' },
    { fn: 'noticePlan', in: { ...plan, kind: '', phase: '' }, out: { current: false, toast: false, act: 'ack' }, why: 'no kind and no phase: never true' },
    { fn: 'noticePlan', in: { ...plan, kind: 'paused', phase: 'paused' }, out: { current: true, toast: true, act: 'wake' }, why: 'a kind a newer wb adds is true while the phase names it' },
    { fn: 'rewakePlan', in: rw, out: { recent: true, candidate: true, due: true, kind: 'review' }, why: 'a review waiting five minutes, no news had: woken' },
    { fn: 'rewakePlan', in: { ...rw, acked: at(5 * min) }, out: { recent: true, candidate: true, due: false, kind: 'review' }, why: 'acked since it changed, no notice seen: had the news' },
    { fn: 'rewakePlan', in: { ...rw, seen: at(6 * min), acked: at(6 * min) }, out: { recent: true, candidate: true, due: false, kind: 'review' },
      why: 'acked its newest notice: the status write after it never wakes twice' },
    { fn: 'rewakePlan', in: { ...rw, seen: at(6 * min), marked: at(4 * min), acked: at(6 * min) }, out: { recent: true, candidate: true, due: true, kind: 'review' },
      why: 'the task’s own noticed mark is newer than what was acked (#844)' },
    { fn: 'rewakePlan', in: { ...rw, updatedAt: now - 30_000 }, out: { recent: true, candidate: false, due: false, kind: 'review' }, why: 'under a minute: not yet' },
    { fn: 'rewakePlan', in: { ...rw, updatedAt: now - 13 * hour }, out: { recent: false, candidate: false, due: false, kind: 'review' },
      why: 'over twelve hours: a session long gone, and no longer holds the weekly jobs off' },
    { fn: 'rewakePlan', in: { ...rw, busy: true }, out: { recent: true, candidate: false, due: false, kind: 'review' }, why: 'busy: never mid-turn' },
    { fn: 'rewakePlan', in: { ...rw, wake: false }, out: { recent: true, candidate: false, due: false, kind: 'review' }, why: 'wakes off' },
    { fn: 'rewakePlan', in: { ...rw, dropped: now - 30_000 }, out: { recent: true, candidate: false, due: false, kind: 'review' }, why: 'a wake dropped half a minute ago: once a minute' },
    { fn: 'rewakePlan', in: { ...rw, parked: true }, out: { recent: false, candidate: false, due: false, kind: 'review' }, why: 'parked by a quit: waits for no one (#541)' },
    { fn: 'rewakePlan', in: { ...rw, mine: false }, out: { recent: false, candidate: false, due: false, kind: 'review' }, why: 'another session’s' },
    { fn: 'rewakePlan', in: { ...rw, phase: 'asking' }, out: { recent: true, candidate: true, due: true, kind: 'question' }, why: 'an asking task stands for a question' },
    { fn: 'rewakePlan', in: { ...rw, phase: 'running' }, out: { recent: false, candidate: false, due: false, kind: 'running' }, why: 'running waits on no one' },
  ],
} satisfies Golden
