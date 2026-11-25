// Kernel.Tables, as generated from workbench's Python (scripts/kernel.py tables). A change to a
// value it copies (store.ACTIVE, NEEDS_CLAUDE, PARKS, CLOSED, ask_mcp.NO_ANSWER, chat.LISTENING_FOR)
// changes this case too, in the same commit: it is what both hosts will read.
import type { Golden } from './run.js'

export default {
  module: 'Kernel.Tables',
  cases: [
    {
      fn: 'tables', in: null, why: 'the values wb holds, sorted',
      out: {
        active: ['asking', 'queued', 'running', 'testing'],
        needsClaude: ['asking', 'failed', 'review', 'stuck'],
        parks: ['failed', 'review', 'stuck'],
        closed: ['accepted', 'discarded'],
        noAnswer: 'No answer arrived in time.',
        chatStaleMs: 15000,
      },
    },
  ],
} satisfies Golden
