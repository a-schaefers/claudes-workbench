import { expect, test } from 'bun:test'
import { mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, symlinkSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import type { Answer } from '../consent.ts'
import { NOT_SENT, SEND_VIA_TOOL } from '../gates.ts'
import { NO_FORM_SENT, Upstream, githubTool, packageCaches, type UpstreamBefore } from '../upstream.ts'

// The upstream gate on OpenCode's path (#1296's parity row 14, 2026-11-26): a gh write, a git push
// or a GitHub MCP write aimed at workbench upstream asks the user in the session's form, as the
// mod's dialog does; under OpenCode's own Bun: `BUN_BE_BUN=1 opencode test opencode-plugin/test`.

const UP = 'https://github.com/a-schaefers/workbench.git'
const has = (text: string | undefined, part: string) => expect((text ?? '').includes(part)).toBe(true)

function setup(answer: Answer) {
  const origins: Record<string, string> = {}  // a directory's origin, as git would say it
  const top = realpathSync(mkdtempSync(join(tmpdir(), 'wb-upstream-')))
  const home = join(top, 'home'), root = join(top, 'state'), work = join(top, 'work')
  for (const d of [home, join(root, 'upstream', 'fix'), work]) mkdirSync(d, { recursive: true })
  const asked: string[] = []
  const at = { dir: work }  // the session's directory
  const up = new Upstream({
    env: () => ({ HOME: home, WB_HOME: root }),
    dir: async () => at.dir,
    read: path => { try { return readFileSync(path, 'utf8') } catch { return undefined } },
    real: path => { try { return realpathSync(path) } catch { return path } },
    origin: async dir => origins[dir] ?? '',
    asker: { ask: async (_s, q) => { asked.push(q.question); return answer } },
  })
  const shell = (command: string, workdir = ''): UpstreamBefore =>
    ({ tool: 'shell', sessionID: 'ses_1', id: 'c1', input: { command, ...(workdir ? { workdir } : {}) } })
  return { top, home, root, work, up, asked, shell, origins, at, done: () => rmSync(top, { recursive: true, force: true }) }
}

test('a git push in a gated directory asks in the form: sent on the yes, refused on a no and with no TUI', async () => {
  for (const [answer, want] of [['yes', ''], ['no', NOT_SENT], ['none', NO_FORM_SENT]] as const) {
    const t = setup(answer)
    try {
      const fix = join(t.root, 'upstream', 'fix')
      expect(await t.up.refusal(t.shell(`cd ${fix} && git push -u origin HEAD`))).toBe(want)
      expect(t.asked.length).toBe(1)
      has(t.asked[0], `publicly: cd ${fix} && git push -u origin HEAD`)
      has(t.asked[0], 'GitHub (a-schaefers/workbench)')
      // the call's workdir is where it runs, and $WB_HOME spells the state's directory
      expect(await t.up.refusal(t.shell('git push', fix))).toBe(want)
      expect(await t.up.refusal(t.shell('git -C "$WB_HOME/upstream/fix" push'))).toBe(want)
      // before() throws the refusal, which OpenCode hands the model; the yes lets the call run
      expect(await t.up.before(t.shell('git push', fix)).then(() => '', (e: Error) => e.message)).toBe(want)
    } finally { t.done() }
  }
})

test('a push or gh write aimed elsewhere asks nothing, and text naming one is no command', async () => {
  const t = setup('no')
  try {
    expect(await t.up.refusal(t.shell('git push origin main'))).toBe('')            // the user's own repo
    expect(await t.up.refusal(t.shell('gh issue create --repo someone/else --title x --body y'))).toBe('')
    expect(await t.up.refusal(t.shell('echo "gh issue comment 1 --repo a-schaefers/workbench"'))).toBe('')
    expect(await t.up.refusal(t.shell(`git log ${t.root}/upstream/fix`))).toBe('')  // no outward form
    expect(await t.up.refusal({ tool: 'read', sessionID: 's', input: { filePath: '/x' } })).toBe('')
    expect(t.asked).toEqual([])
  } finally { t.done() }
})

test('a gh write naming upstream asks, the opaque --body "$(cat <<EOF …)" form and a bash -c too, unless in a developer\'s clone', async () => {
  const t = setup('no')
  try {
    const body = "gh issue create --repo a-schaefers/workbench --title t --body \"$(cat <<'EOF'\nit can't (really)\nEOF\n)\""
    expect(await t.up.refusal(t.shell(body))).toBe(NOT_SENT)
    expect(await t.up.refusal(t.shell("bash -c 'gh pr comment 3 -R a-schaefers/workbench -b hi'"))).toBe(NOT_SENT)
    // the former name GitHub redirects
    expect(await t.up.refusal(t.shell('gh issue comment 3 --repo a-schaefers/claudes-workbench -b hi'))).toBe(NOT_SENT)
    expect(t.asked.length).toBe(3)
  } finally { t.done() }
  const dev = setup('no')
  try {
    // the same line from a developer's own clone of upstream (its origin upstream): no user's driver
    dev.origins[dev.work] = UP
    expect(await dev.up.refusal(dev.shell('gh issue comment 3 --repo a-schaefers/workbench -b hi'))).toBe('')
    // but a gated directory is gated whatever its origin
    dev.origins[join(dev.root, 'upstream', 'fix')] = UP
    expect(await dev.up.refusal(dev.shell('git push', join(dev.root, 'upstream', 'fix')))).toBe(NOT_SENT)
    // and a GitHub MCP write from a session in one, a clone of upstream though it is (the mod's rule)
    const repo = { owner: 'a-schaefers', repo: 'workbench' }
    const mcp: UpstreamBefore = { tool: 'github_create_issue', sessionID: 'ses_1', input: repo }
    expect(await dev.up.refusal(mcp)).toBe('')
    dev.at.dir = join(dev.root, 'upstream', 'fix')
    expect(await dev.up.refusal(mcp)).toBe(NOT_SENT)
    expect(dev.asked.length).toBe(2)
  } finally { dev.done() }
})

test('a symlinked spelling of a gated directory is gated by its real path', async () => {
  const t = setup('no')
  try {
    const link = join(t.top, 'link')
    symlinkSync(join(t.root, 'upstream', 'fix'), link)
    expect(await t.up.refusal(t.shell(`git -C ${link} push`))).toBe(NOT_SENT)
  } finally { t.done() }
})

test('a wb patch send in a shell is refused with the tool that shows its title and body, no form', async () => {
  const t = setup('yes')
  try {
    expect(await t.up.refusal(t.shell('wb patch send fix --title T --body-file b.md'))).toBe(SEND_VIA_TOOL)
    expect(await t.up.refusal(t.shell("bash -c 'wb patch send fix --title T --body-file b.md'"))).toBe(SEND_VIA_TOOL)
    expect(t.asked).toEqual([])
  } finally { t.done() }
})

test('a GitHub MCP write naming upstream asks with its input; a read, another repo or another server does not', async () => {
  const t = setup('no')
  try {
    const info = (name: string, namespace = 'github') => ({ name, options: { namespace } })
    const call = (tool: string, input: Record<string, unknown>, i?: ReturnType<typeof info>): UpstreamBefore =>
      ({ tool, sessionID: 'ses_1', input, ...(i ? { info: i } : {}) })
    const repo = { owner: 'a-schaefers', repo: 'workbench', title: 'x' }
    expect(await t.up.refusal(call('github_create_issue', repo, info('create_issue')))).toBe(NOT_SENT)
    has(t.asked[0], 'publicly: github_create_issue {')
    expect(await t.up.refusal(call('github_create_issue', repo))).toBe(NOT_SENT)  // no info: by its name
    expect(await t.up.refusal(call('github_get_issue', repo, info('get_issue')))).toBe('')
    expect(await t.up.refusal(call('github_create_issue', { owner: 'x', repo: 'y' }, info('create_issue')))).toBe('')
    expect(await t.up.refusal(call('notes_create_issue', repo, info('create_issue', 'notes')))).toBe('')
    expect(t.asked.length).toBe(2)
  } finally { t.done() }
  expect(githubTool('my-github_add_issue_comment')).toBe('add_issue_comment')
  expect(githubTool('workbench_route')).toBe('')
  expect(packageCaches({ HOME: '/h', XDG_CACHE_HOME: '/x' })).toEqual(['/x/opencode/npm', '/h/.cache/opencode/npm'])
})

test('the patch clone patches.json names, and OpenCode\'s package cache by its origin, are gated too', async () => {
  const t = setup('no')
  try {
    const clone = join(t.top, 'clone')
    mkdirSync(clone, { recursive: true })
    writeFileSync(join(t.root, 'patches.json'), JSON.stringify({ source: clone }))
    expect(await t.up.refusal(t.shell(`cd ${clone} && git push`))).toBe(NOT_SENT)
    const cached = join(t.home, '.cache/opencode/npm/x')
    mkdirSync(cached, { recursive: true })
    expect(await t.up.refusal(t.shell(`cd ${cached} && git push`))).toBe('')          // its origin isn't upstream
    t.origins[cached] = UP
    expect(await t.up.refusal(t.shell(`cd ${cached} && git push`))).toBe(NOT_SENT)
  } finally { t.done() }
})
