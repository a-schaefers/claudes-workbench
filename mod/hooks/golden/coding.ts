// Kernel.Coding: the coding gate's rules (#92, #94, #1322) and each host's file tools. The modes
// as wb's cli.coding_mode reads them (a mode it doesn't know is "decide"), the first allowance not
// handed back, and a one-task exception stamped at its first edit.
import type { Golden } from './run.js'

const a = (used: boolean, when = false, started = false) => ({ used, when, started })

export default {
  module: 'Kernel.Coding',
  cases: [
    { fn: 'codingGate', in: { mode: 'decide', allowed: [] }, out: { verdict: 'pass', open: -1 }, why: 'Let Claude decide: no gate' },
    { fn: 'codingGate', in: { mode: 'constructor', allowed: [] }, out: { verdict: 'pass', open: -1 }, why: 'a mode wb doesn’t know is decide, never a refusal' },
    { fn: 'codingGate', in: { mode: 'ask', allowed: [] }, out: { verdict: 'ask', open: -1 }, why: 'Ask me first, no yes yet: the dialog' },
    { fn: 'codingGate', in: { mode: 'only', allowed: [a(true)] }, out: { verdict: 'refuse', open: -1 }, why: 'Only when I say, every yes handed back: refused' },
    { fn: 'codingGate', in: { mode: 'only', allowed: [a(true), a(false)] }, out: { verdict: 'pass', open: 1 }, why: 'the user’s yes not yet handed back' },
    { fn: 'codingGate', in: { mode: 'ask', allowed: [a(false, true)] }, out: { verdict: 'start', open: 0 }, why: 'a one-task exception’s first edit: stamped started (#94)' },
    { fn: 'codingGate', in: { mode: 'ask', allowed: [a(false, true, true), a(false, true)] }, out: { verdict: 'pass', open: 0 }, why: 'an exception under way: only the first open one counts' },
    {
      fn: 'editTools', in: null, why: 'each host’s file tools and the field that names the file',
      out: [
        { host: 'claude-code', tool: 'Edit', field: 'file_path' },
        { host: 'claude-code', tool: 'MultiEdit', field: 'file_path' },
        { host: 'claude-code', tool: 'Write', field: 'file_path' },
        { host: 'claude-code', tool: 'NotebookEdit', field: 'notebook_path' },
        { host: 'opencode', tool: 'edit', field: 'path' },
        { host: 'opencode', tool: 'write', field: 'path' },
      ],
    },
  ],
} satisfies Golden
