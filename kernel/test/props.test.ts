import { expect, test } from 'bun:test'
// @ts-ignore: props.js is written beside this file by `python3 scripts/kernel.py props`, in a
// scratch copy, from Props.purs and kernel/src; it is never committed
import { props } from './props.js'

// The kernel's seeded properties (Props.purs) under Bun: three seeds, a thousand facts each, in
// well under ten seconds (#1356). A failure names its seed and case, so it replays as it is.
const SEEDS = [20261124, 1, 2]
const N = 1000

test('the kernel holds its properties on a thousand seeded facts for each seed', () => {
  const got = props({ seeds: SEEDS, n: N }) as { cases: number; failures: string[] }
  expect(got.failures).toEqual([])
  expect(got.cases).toBe(SEEDS.length * N)
}, 10_000)
