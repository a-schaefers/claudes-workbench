import { expect, test } from 'bun:test'
import { kernel } from '../../mod/hooks/core.ts'
import { GOLDENS, goldenFailures } from '../../mod/hooks/golden/index.ts'

// The kernel's goldens under OpenCode's Bun (`BUN_BE_BUN=1 opencode test opencode-plugin/test`),
// the same cases and the same kernel.js that mod/hooks/kernel.test.ts runs on Claude Code (#1356).
test('every golden case holds on the committed kernel under Bun', () => {
  expect(goldenFailures(kernel, GOLDENS)).toEqual([])
})
