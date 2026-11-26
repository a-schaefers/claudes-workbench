// Kernel.Tables, as generated from workbench's Python (scripts/kernel.py tables). A change to a
// value it copies (store.ACTIVE, NEEDS_CLAUDE, PARKS, CLOSED, WORKING, STOPPED, OPEN_ORDER,
// ask_mcp.NO_ANSWER, chat.LISTENING_FOR, spend.WAKE_OPENING) changes this case too, in the same commit: it is what both hosts read. Its
// twin is the Python that writes Tables, so tests/test_kernel.py's Twins holds it to the Python.
import type { Golden } from './run.js'

export default {
  module: 'Kernel.Tables',
  cases: [
    {
      fn: 'tables', in: null, why: 'the values wb holds, sorted but OPEN_ORDER', twin: 'kernel.tables_values',
      out: {
        active: ['asking', 'queued', 'running', 'testing'],
        needsClaude: ['asking', 'failed', 'review', 'stuck'],
        parks: ['failed', 'review', 'stuck'],
        closed: ['accepted', 'discarded'],
        working: ['asking', 'running', 'testing'],
        stopped: ['failed', 'stuck'],
        openOrder: ['running', 'testing', 'asking', 'queued', 'review', 'stuck', 'failed'],
        noAnswer: 'No answer arrived in time.',
        chatStaleMs: 15000,
        wakeOpening: '[workbench] Task',
      },
    },
  ],
} satisfies Golden
