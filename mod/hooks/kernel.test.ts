import { expect, test } from 'claude-code/testing'
import { kernel } from './core.js'
import { GOLDENS, goldenFailures } from './golden/index.js'

// The kernel's goldens on Claude Code's runtime; opencode-plugin/test/kernel.test.ts runs the same
// cases under OpenCode's Bun (#1356).
test('every golden case holds on the committed kernel', () => {
  expect(goldenFailures(kernel, GOLDENS)).toEqual([])
})

test('a wrong golden fails, naming the case and both answers', () => {
  const wrong = [{ module: 'Kernel.Phase', cases: [{ fn: 'phaseOf', in: { phase: 'running', parked: false }, out: { active: false }, why: 'wrong on purpose' }] }]
  const got = goldenFailures(kernel, wrong)
  expect(got.length).toBe(1)
  expect(got[0]).toContain('Kernel.Phase #1 phaseOf (wrong on purpose): got {"active":true,')
})

test('a golden for a function the kernel lacks fails rather than passing', () => {
  const missing = [{ module: 'Kernel.Gone', cases: [{ fn: 'gone', in: {}, out: null, why: 'no such export' }] }]
  expect(goldenFailures(kernel, missing)).toEqual(['Kernel.Gone #1 gone (no such export): the kernel exports no function gone'])
})
