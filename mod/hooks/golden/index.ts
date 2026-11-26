// Every kernel export's golden cases, in kernel/BOUNDARY.md's `## Exports` order: a new export
// adds its file here (tests/test_kernel.py holds the golden `fn`s equal to the exports).
import away from './away.js'
import owner from './owner.js'
import phase from './phase.js'
import type { Golden } from './run.js'
import tables from './tables.js'

export { canonical, goldenFailures } from './run.js'
export type { Case, Golden } from './run.js'
export const GOLDENS: ReadonlyArray<Golden> = [tables, phase, owner, away]
