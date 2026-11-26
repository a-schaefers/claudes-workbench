import { expect, test } from 'bun:test'
import { fileURLToPath } from 'node:url'
import { join } from 'node:path'
import { readFileSync, readdirSync } from 'node:fs'
import { HOST_LINES, SKILL_ID, hostLineMatches, partId, readSkill, render, skillFor, type Where } from '../skill.ts'
import { AGENT, NAME, OURS, defineAgent, defineSkills, intro, keepOurs, registerAgent, skillRoot, where, withPrompt } from '../agent.ts'
import { OWN } from '../wb.ts'
import type { AgentInfo, SkillInfo } from '../host.d.ts'

// The skill on OpenCode (#1335), under OpenCode's own Bun: `BUN_BE_BUN=1 opencode test
// opencode-plugin/test`. The source is this checkout's skill/workbench/, as it is.
const ROOT = fileURLToPath(new URL('../..', import.meta.url)).replace(/\/$/, '')
const DIR = join(ROOT, 'skill', 'workbench')
const FILES = ['SKILL.md', ...readdirSync(join(DIR, 'reference')).filter((n) => n.endsWith('.md')).sort().map((n) => `reference/${n}`)]
const TEXTS: Record<string, string> = Object.fromEntries(FILES.map((f) => [f, readFileSync(join(DIR, f), 'utf8')]))
const W: Where = { root: '/opt/workbench', wb: '/opt/workbench/bin/wb', onPath: false, docs: true }
const R = render(TEXTS, W)
const OPENCODE: Record<string, string> = Object.fromEntries([['prompt', R.prompt], ...R.parts.map((p) => [p.id, p.content])])

// Each line of `text` that `bad` matches, as `<name>:<line>: <text>`
const offenders = (bad: RegExp): string[] => Object.entries(OPENCODE).flatMap(([name, text]) =>
  text.split('\n').flatMap((l, i) => (bad.test(l) ? [`${name}:${i + 1}: ${l.trim()}`] : [])))

test("Claude Code's copy of every skill file is the file itself, byte for byte (#1296's open question 1)", () => {
  for (const f of FILES) expect(skillFor('claude-code', f, TEXTS[f] ?? '', W) === TEXTS[f]).toBe(true)
  expect(FILES.length > 1).toBe(true)
})

test('every host line still matches the Claude Code text it rewrites: a reworded SKILL.md fails here by name', () => {
  const unmatched = hostLineMatches(TEXTS).flatMap((n, i) => (n === 0 ? [HOST_LINES[i]?.from.source] : []))
  expect(unmatched).toEqual([])
})

test("OpenCode's copy has no run_in_background, no $CLAUDE_CODE_SESSION_ID, and Claude only in Claude Code", () => {
  expect(offenders(/run_in_background/)).toEqual([])
  expect(offenders(/CLAUDE_CODE_SESSION_ID|CLAUDE_CONFIG_DIR/)).toEqual([])
  // the skill's own arguments are Claude Code's; a command file's `$ARGUMENTS` (deep.md's flow
  // import) is both hosts' word
  expect(offenders(/\$ARGUMENTS/).filter((l) => l.startsWith('prompt:'))).toEqual([])
  // "Claude Code" may wrap at a line end ("a restart of Claude\nCode"): test the whole text
  const claude = Object.entries(OPENCODE).flatMap(([name, text]) =>
    [...text.matchAll(/Claude(?!\s+Code)/g)].map((m) => `${name}: …${text.slice(Math.max(0, (m.index ?? 0) - 40), (m.index ?? 0) + 20)}…`))
  expect(claude).toEqual([])
  // nor Claude Code's tools, plan mode, the mod, its hand-back or the reference files' paths
  expect(offenders(/AskUserQuestion|ExitPlanMode|plan mode|your own Workflow|\bBash call|your Bash|Bash's|hand-back-claude|reference\/\w+\.md|beside this file/)).toEqual([])
  expect(offenders(/\b(the|The|workbench) mod\b(?!\/|'s tests)/)).toEqual([])
})

test('the prompt is SKILL.md\'s body, without its frontmatter, and the workbench skill is the same text', () => {
  expect(R.prompt.startsWith('# /workbench — an OpenCode agent writes, you review\n')).toBe(true)
  expect(R.skill).toEqual({ id: SKILL_ID, name: SKILL_ID, description: R.skill.description, content: R.prompt })
  expect(R.skill.description.startsWith('Workbench mode: you hand the coding of each request to OpenCode agents')).toBe(true)
  // the scheduled run is Claude Code headless, never this agent: its section is left out
  expect(offenders(/^## Headless/)).toEqual([])
})

test("until the plugin delivers wakes (#1313), the driver waits with wb wait in the background, never for a wake (the #1335 review)", () => {
  expect(R.prompt.includes("**Don't poll.** Run `wb wait <name>` with the shell tool's `background: true`")).toBe(true)
  expect(/woken\s+by\s+a\s+`\[workbench\]`\s+prompt|end\s+your\s+turn\s+and\s+wait\s+for\s+it/.test(R.prompt)).toBe(false)
  // the user's two commands aren't OpenCode's yet (#1312): their words do the same
  expect(R.prompt.includes("Where those commands aren't offered, the user may ask for the same in words")).toBe(true)
})

test('every reference file is a workbench-<name> skill, and every one the text names is given', () => {
  expect(R.parts.map((p) => p.id)).toEqual(FILES.slice(1).map(partId))
  const named = new Set(Object.values(OPENCODE).flatMap((t) => [...t.matchAll(/`(workbench-[a-z]+)` skill/g)].map((m) => m[1])))
  expect([...named].filter((id) => !R.parts.some((p) => p.id === id))).toEqual([])
  expect(named.size > 3).toBe(true)
  // a pointer reads "load", the skill tool being how a part comes in
  expect(offenders(/\bread\s+the `workbench-/i)).toEqual([])
})

test("the wb line names wb's path unless OpenCode's PATH has it, and ARCHITECTURE.md only where it is", () => {
  const cli = (w: Where) => render(TEXTS, w).prompt.split('\n').find((l) => l.startsWith('`wb` is the workbench CLI')) ?? ''
  expect(cli({ ...W, onPath: true }).startsWith('`wb` is the workbench CLI (on PATH as `wb`; `wb --help`). Its code, ARCHITECTURE.md included, is at `/opt/workbench`. Every')).toBe(true)
  expect(cli(W).includes('`/opt/workbench/bin/wb` (run it by that path')).toBe(true)
  expect(cli({ ...W, docs: false }).includes("ARCHITECTURE.md, its design, is in a clone of github.com/a-schaefers/workbench (OpenCode's install leaves it out)")).toBe(true)
  expect(R.parts.find((p) => p.id === 'workbench-patching')?.content.includes("CONTRIBUTING.md at the root of workbench's code (at `/opt/workbench`)")).toBe(true)
})

test('the skills replace the Claude Code workbench skill OpenCode scans, so it lists one, and none is offered to other agents', () => {
  const skills = new Map<string, SkillInfo>([['workbench', { id: 'workbench', name: 'workbench', path: '/home/u/.claude/skills/workbench/SKILL.md', content: TEXTS['SKILL.md'] ?? '', autoinvoke: false }]])
  defineSkills({
    list: () => [...skills.values()],
    get: (id) => skills.get(id),
    add: (s) => { skills.set(s.id, s) },
    remove: (id) => { skills.delete(id) },
  }, R)
  expect([...skills.keys()].sort()).toEqual(['workbench', ...R.parts.map((p) => p.id)].sort())
  const ours = skills.get('workbench')
  expect([ours?.path, ours?.content === R.prompt, ours?.autoinvoke]).toEqual(['skill://workbench/SKILL.md', true, false])
  expect([...skills.values()].every((s) => s.autoinvoke === false && s.path === `skill://${s.id}/SKILL.md`)).toBe(true)
})

test('the Workbench agent is a primary agent, with the question tool as Build has', () => {
  const agents = new Map<string, AgentInfo>()
  // an agent of that id already there (the user's config, say) as a subagent: ours is primary
  const fresh = (): AgentInfo => ({ name: 'x', mode: 'subagent', hidden: false, permissions: [{ action: '*', resource: '*', effect: 'allow' }] })
  defineAgent({
    get: (id) => agents.get(id),
    update: (id, f) => { const a = agents.get(id) ?? fresh(); f(a); agents.set(id, a) },
    remove: (id) => { agents.delete(id) },
  })
  const a = agents.get(AGENT)
  expect([AGENT, a?.name, a?.mode, a?.hidden]).toEqual(['workbench', NAME, 'primary', false])
  expect(a?.permissions).toEqual([{ action: '*', resource: '*', effect: 'allow' }, { action: 'question', resource: '*', effect: 'allow' }])
})

test("the prompt goes last in the system prompt of the Workbench agent's requests, and no other agent's", () => {
  // OpenCode 2.4.1 has no agent `instructions`: set, it reached no model (live, 2026-11-25)
  const request = (agent: string) => ({ sessionID: 's', agent, system: [{ type: 'text' as const, text: "OpenCode's own" }] })
  const ours = request(AGENT)
  withPrompt(ours, R.prompt)
  expect(ours.system).toEqual([{ type: 'text', text: "OpenCode's own" }, { type: 'text', text: intro + R.prompt }])
  expect(intro).toBe('You are the Workbench agent. Follow these instructions while you are the Workbench agent:\n\n')
  for (const other of ['build', 'plan', 'general']) {
    const theirs = request(other)
    withPrompt(theirs, R.prompt)
    expect(theirs.system.length).toBe(1)
  }
})

test("skill/ comes from the package, else from the clone the options' wb runs from (wb setup --opencode's copy has none)", () => {
  expect(skillRoot(undefined, OWN)).toBe(ROOT)
  expect(skillRoot({ wb: join(ROOT, 'bin', 'wb') }, '/nonexistent/copy/bin/wb')).toBe(ROOT)
  expect(skillRoot({ wb: '/nonexistent/clone/bin/wb' }, '/nonexistent/copy/bin/wb')).toBeUndefined()
  expect(where(ROOT, { wb: join(ROOT, 'bin', 'wb') }, {})).toEqual({ root: ROOT, wb: join(ROOT, 'bin', 'wb'), onPath: false, docs: true })
  expect(readSkill('/nonexistent')).toBeUndefined()
})

// An event stream of `n` skill.updated events, then the end
async function* updates(n: number) {
  for (let i = 0; i < n; i++) yield { type: 'skill.updated' }
}

test('setup registers a skill transform, an agent transform and the context hook from the package\'s skill/', async () => {
  const done: string[] = []
  const reg = (what: string) => async () => { done.push(what); return { dispose: async () => {} } }
  const got = await registerAgent({
    options: {}, rpc: { register: reg('rpc') }, agent: { transform: reg('agent') }, session: { hook: reg('context') },
    skill: { transform: reg('skill'), list: async () => ({ data: [] }) }, event: { subscribe: () => updates(0) },
  } as never)
  expect(done).toEqual(['skill', 'agent', 'context'])
  expect(got?.rendered.prompt === render(readSkill(ROOT) ?? {}, where(ROOT, {})).prompt).toBe(true)
  got?.stop()
})

test('when a config source\'s workbench wins (applied after plugins), the transform registers again, last, and only then', async () => {
  // OpenCode applies ~/.claude/skills after every plugin: seen live, its copy replaced ours
  const done: string[] = []
  const lists = [{ data: [{ id: 'workbench', path: '/home/u/.claude/skills/workbench/SKILL.md' }] }, { data: [{ id: 'workbench', path: OURS }] }]
  const ctx = {
    skill: {
      transform: async () => { done.push('transform'); return { dispose: async () => { done.push('dispose') } } },
      list: async () => lists.shift() ?? { data: [] },
    },
    event: { subscribe: () => updates(3) },
  }
  const first = await ctx.skill.transform()
  await keepOurs(ctx as never, () => {}, first, new AbortController().signal)
  // the first update finds the ~/.claude copy: dispose and register again; the next finds ours
  // (and the third, none listed): nothing more
  expect(done).toEqual(['transform', 'dispose', 'transform'])
})
