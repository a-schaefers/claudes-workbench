// The golden runner both hosts share: `claude plugin test mod` (mod/hooks/kernel.test.ts) and
// OpenCode's Bun (opencode-plugin/test/kernel.test.ts) run the same cases on the same kernel.js
// through it, so the two engines are held to one answer (kernel/BOUNDARY.md, *Move protocol*). It
// imports nothing: Bun can't resolve claude-code/testing, and Claude Code's runner has no bun:test.

// One input and its answer. `in: null` is a value export (`tables`), compared as it is; otherwise
// the case calls `k[fn](in)`. `why` says what the case holds, `twin` a Python name that judges
// the same thing (called with `in` as keyword arguments), where there is one.
export type Case = { fn: string; in: Record<string, unknown> | null; out: unknown; why: string; twin?: string }
export type Golden = { module: string; cases: ReadonlyArray<Case> }

// JSON with every object's keys sorted, so field order never decides a comparison
export const canonical = (v: unknown): string => JSON.stringify(v, (_k, x: unknown) =>
  x && typeof x === 'object' && !Array.isArray(x)
    ? Object.fromEntries(Object.entries(x as Record<string, unknown>).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0)))
    : x)

// Every case that fails on kernel `k`, one line each naming the module, the case and both answers
export function goldenFailures(k: Readonly<Record<string, unknown>>, all: ReadonlyArray<Golden>): string[] {
  const out: string[] = []
  for (const g of all) {
    g.cases.forEach((c, i) => {
      const where = `${g.module} #${i + 1} ${c.fn} (${c.why})`
      const f = k[c.fn]
      let got: unknown
      try {
        if (c.in === null) got = f
        else if (typeof f === 'function') got = (f as (x: unknown) => unknown)(c.in)
        else { out.push(`${where}: the kernel exports no function ${c.fn}`); return }
      } catch (e) {
        out.push(`${where}: threw ${String(e)}`)
        return
      }
      if (got === undefined) { out.push(`${where}: the kernel exports no ${c.fn}`); return }
      if (canonical(got) !== canonical(c.out)) out.push(`${where}: got ${canonical(got)}, want ${canonical(c.out)}`)
    })
  }
  return out
}
