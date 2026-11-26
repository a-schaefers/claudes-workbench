import { expect, test } from 'claude-code/testing'
import { sameWait, waitingFact, WAITING_AFTER_MS } from './waiting.js'

// The driver's wait (#1355) on Claude Code's runtime. The one module both hosts import; under
// OpenCode's Bun, opencode-plugin/test/chat.test.ts runs every case of
// tests/fixtures/story/dialogs.json through it (a hooks test reads no file), and
// tests/test_chat_driver.py reads each fact as wb does.
test('waitingFact names the oldest dialog up two seconds or more, never one answered sooner', () => {
  expect(WAITING_AFTER_MS).toBe(2000)
  expect(waitingFact([], 10_000)).toBe(null)
  expect(waitingFact([{ kind: 'permission', tool: 'Bash', at: 10_000 }], 11_999)).toBe(null)
  expect(waitingFact([{ kind: 'permission', tool: 'Bash', at: 10_000, text: 'rm -rf runs/*' }], 12_000))
    .toEqual({ kind: 'permission', tool: 'Bash', since: 10, what: 'rm -rf runs/*' })
  expect(waitingFact([{ kind: 'plan', tool: 'ExitPlanMode', at: 15_000 }, { kind: 'question', tool: 'AskUserQuestion', at: 12_000 }, { kind: 'permission', tool: 'Bash', at: 17_000 }], 20_000))
    .toEqual({ kind: 'question', tool: 'AskUserQuestion', since: 12 })
  expect(waitingFact([{ kind: 'permission', tool: 'Bash', at: 1000, done: true }], 20_000)).toBe(null)
  expect(waitingFact([{ kind: 'consent', tool: 'workbench', at: 5000 }], 9000)).toEqual({ kind: 'consent', tool: 'workbench', since: 5 })
  expect(waitingFact([{ kind: 'form', tool: 'x', at: 5000 }], 9000)).toEqual({ kind: 'permission', tool: 'x', since: 5 })
})

test('sameWait says whether the claim needs writing again', () => {
  const a = waitingFact([{ kind: 'permission', tool: 'Bash', at: 0 }], 5000)
  expect(sameWait(a, waitingFact([{ kind: 'permission', tool: 'Bash', at: 0 }], 9000))).toBe(true)
  expect(sameWait(a, null)).toBe(false)
  expect(sameWait(null, null)).toBe(true)
})
