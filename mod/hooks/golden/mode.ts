// Kernel.Mode's tellPlan, modesPlan and compactPlan: what a conversation is told of the rules,
// which sessions resume in workbench mode, and when a task boundary compacts, as the mod's
// unsaid, prompt.context, keepMode and poll did before the move (#1325), so the OpenCode plugin's
// instructions and compaction (#1312, #1315) follow the same plan.
import type { Golden } from './run.js'

const none = { notes: '', wakeOff: false, coding: 'decide', routes: '', workflows: 'off' }
const all = { notes: 'use tabs', wakeOff: true, coding: 'never', routes: 'significant: api', workflows: 'on' }
const mid = { first: false, want: all, was: none, known: true, any: true, delegating: true, away: false }
const first = { first: true, want: all, was: all, known: false, any: true, delegating: true, away: false }
const day = 24 * 3600_000
const now = 1_800_000_000_000
const cp = { on: true, was: ['a'], open: [], boundary: false, busy: false, waiting: false, percentSet: false, percent: 0,
  tokens: 0, window: 0, autoCompact: false, threshold: 0 }

export default {
  module: 'Kernel.Mode',
  cases: [
    { fn: 'tellPlan', in: mid, out: { say: ['notes', 'wakeOff', 'coding', 'routes', 'workflows'], said: all }, why: 'midway, everything it lacks, in order' },
    { fn: 'tellPlan', in: { ...mid, was: all }, out: { say: [], said: all }, why: 'told already: nothing again' },
    { fn: 'tellPlan', in: { ...mid, any: false }, out: { say: ['notes', 'wakeOff'], said: { ...none, notes: all.notes, wakeOff: true } },
      why: 'out of the mode with nothing open: only the notes and the wake-off note, the rest unchanged' },
    { fn: 'tellPlan', in: { ...mid, want: { ...none, routes: 'r2' }, was: { ...none, routes: 'r1' } }, out: { say: ['routesChanged'], said: { ...none, routes: 'r2' } },
      why: 'a role changed after one was said (#1014)' },
    { fn: 'tellPlan', in: { ...mid, want: none, was: { ...none, coding: 'always', notes: 'x', wakeOff: true } },
      out: { say: ['coding'], said: { ...none, notes: 'x', wakeOff: true } }, why: 'back to decide is said too; notes and wakes off stay said' },
    { fn: 'tellPlan', in: first, out: { say: ['mode', 'coding', 'notes', 'workflows', 'wakeOff', 'routes'], said: all }, why: 'a first message: every block that applies' },
    { fn: 'tellPlan', in: { ...first, known: true }, out: { say: ['mode'], said: all }, why: '/workbench’s own first message said them already (#540)' },
    { fn: 'tellPlan', in: { ...first, want: none, delegating: false, away: true }, out: { say: ['away'], said: none }, why: 'away: the web chat block, nothing else' },
    { fn: 'tellPlan', in: { ...first, known: true, was: { ...all, coding: 'always' } }, out: { say: ['mode', 'coding'], said: all },
      why: 'the coding mode changed since /workbench said it' },
    { fn: 'modesPlan', in: { modes: [{ id: 's1', at: now - day }, { id: 's2', at: now - 30 * day }, { id: 's3', at: now }], ids: ['s3'], now, on: false },
      out: { kept: true, modes: [{ id: 's1', at: now - day }] }, why: 'a quit: this process’s ids gone, thirty days old gone, the rest kept (#689)' },
    { fn: 'modesPlan', in: { modes: [{ id: 's1', at: now - day }], ids: ['s2', 's3'], now, on: true },
      out: { kept: false, modes: [{ id: 's1', at: now - day }, { id: 's2', at: now }, { id: 's3', at: now }] }, why: 'turned on: each id now' },
    { fn: 'modesPlan', in: { modes: [], ids: [], now, on: true }, out: { kept: false, modes: [] }, why: 'nothing kept, no id yet' },
    { fn: 'compactPlan', in: cp, out: { boundary: false, check: true, compact: false }, why: 'a task of this session’s closed, idle: read the usage' },
    { fn: 'compactPlan', in: { ...cp, tokens: 100_000, window: 200_000 }, out: { boundary: false, check: true, compact: true }, why: 'half the window: compact' },
    { fn: 'compactPlan', in: { ...cp, tokens: 99_999, window: 200_000 }, out: { boundary: false, check: true, compact: false }, why: 'under half: not yet' },
    { fn: 'compactPlan', in: { ...cp, tokens: 90_000, window: 200_000, autoCompact: true, threshold: 160_000 }, out: { boundary: false, check: true, compact: true },
      why: 'half of the way to the engine’s own compaction, not of the window' },
    { fn: 'compactPlan', in: { ...cp, tokens: 150_000, window: 200_000, percentSet: true, percent: 0 }, out: { boundary: false, check: true, compact: false },
      why: 'WB_COMPACT_AT=0 turns it off' },
    { fn: 'compactPlan', in: { ...cp, tokens: 70_000, window: 200_000, percentSet: true, percent: 30 }, out: { boundary: false, check: true, compact: true }, why: 'WB_COMPACT_AT=30' },
    { fn: 'compactPlan', in: { ...cp, busy: true }, out: { boundary: true, check: false, compact: false }, why: 'busy: the mark kept for the next look' },
    { fn: 'compactPlan', in: { ...cp, was: [], boundary: true, waiting: true }, out: { boundary: true, check: false, compact: false }, why: 'a task waits on Claude: kept, never mid-review' },
    { fn: 'compactPlan', in: { ...cp, on: false, boundary: true, tokens: 150_000, window: 200_000 }, out: { boundary: false, check: false, compact: false },
      why: 'out of the mode no task makes a boundary (the 2026-11-16 audit)' },
    { fn: 'compactPlan', in: { ...cp, open: ['a'] }, out: { boundary: false, check: false, compact: false }, why: 'still open: no boundary' },
  ],
} satisfies Golden
