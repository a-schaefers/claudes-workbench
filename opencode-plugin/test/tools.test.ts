import { expect, test } from 'bun:test'
import { chmodSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import {
  NAMESPACE, SPECS, START_INPUT, WAIT_MAX_MIN, WAIT_MIN, procedureTools, runner, sessionDirs, startCall, tools,
  type Ran, type RunOpts,
} from '../tools.ts'
import { NAMESPACE_DESCRIPTION } from '../wb.ts'

// The procedure's typed wb tools (#1335), under OpenCode's own Bun: `BUN_BE_BUN=1 opencode test
// opencode-plugin/test`. A fake wb stands in for wb: each tool is one wb command.

type Seen = { args: readonly string[]; opts: RunOpts }
const fake = (ran: Ran = { code: 0, out: 'ok', err: '' }) => {
  const seen: Seen[] = []
  const tools = procedureTools({ run: async (args, opts) => { seen.push({ args, opts }); return ran }, dir: async s => `/repo/${s}` })
  const tool = (name: string) => tools.find(t => t.name === name)!
  return { seen, tool }
}

test('each tool is one wb command, its fields the flags by name, run as the calling session in its directory', async () => {
  const { seen, tool } = fake()
  await tool('status').execute({}, { sessionID: 's1' })
  await tool('status').execute({ task: 'a', all: true, all_sessions: true }, { sessionID: 's1' })
  await tool('diff').execute({ task: 'a', stat: true, all: true, paths: ['x.py', '-y'] }, { sessionID: 's1' })
  await tool('accept').execute({ task: 'a', message: 'feat: -x', merge: true, merge_as: 'squash', take_claude_md: true,
    spec_gap: ['the test'] }, { sessionID: 's1' })
  await tool('discard').execute({ task: 'a', reason: '-wrong', drop_steps: true }, { sessionID: 's1' })
  expect(seen.map(s => s.args)).toEqual([
    ['status'],
    ['status', 'a', '--all', '--all-sessions'],
    ['diff', '--stat', '--all', '--', 'a', 'x.py', '-y'],
    // a value is one argv word, `--flag=value`: one starting with `-` is never read as a flag
    ['accept', 'a', '--message=feat: -x', '--merge', '--merge-as=squash', '--take-claude-md', '--spec-gap=the test'],
    ['discard', 'a', '--reason=-wrong', '--drop-steps'],
  ])
  expect(seen.every(s => s.opts.session === 's1' && s.opts.cwd === '/repo/s1')).toBe(true)
  expect(seen[3]!.opts.timeout).toBe(600_000)                       // a commit may wait on a signing key
})

test('feedback sends its whole text on stdin, never on a command line a shell or argparse could misread', async () => {
  const { seen, tool } = fake()
  const text = '1. In parse(), return 0 for "".\n--fresh is not a flag here; $HOME stays.\n'
  await tool('feedback').execute({ task: 'a', text, fresh: true, spec_gap: ['x'], image: ['/s.png'] }, { sessionID: 's' })
  expect(seen[0]!.args).toEqual(['feedback', 'a', '--message', '-', '--fresh', '--spec-gap=x', '--image=/s.png'])
  expect(seen[0]!.opts.input).toBe(text.trim())
  let said = ''
  await tool('feedback').execute({ task: 'a', text: '  ' }, { sessionID: 's' }).catch(e => { said = e.message })
  expect(said).toBe('workbench: not sent: no text given')
  expect(seen.length).toBe(1)                                        // nothing ran
})

test("a refusal is wb's own words, thrown; wait's exit 2 (timed out, closed) is its answer", async () => {
  let said = ''
  await fake({ code: 3, out: '', err: 'wb: a is closed' }).tool('accept').execute({ task: 'a' }, { sessionID: 's' })
    .catch(e => { said = e.message })
  expect(said).toBe('wb: a is closed\n(exit 3)')
  const w = fake({ code: 2, out: '', err: 'wb: timed out waiting' })
  expect(await w.tool('wait').execute({ task: 'a' }, { sessionID: 's' })).toEqual({ content: 'wb: timed out waiting' })
  expect(w.seen[0]!.args).toEqual(['wait', 'a', `--timeout=${WAIT_MIN * 60}`])
  // a call holds the turn at most WAIT_MAX_MIN; wb's own timeout ends it before the kill
  await w.tool('wait').execute({ minutes: 600 }, { sessionID: 's' })
  expect(w.seen[1]!.args).toEqual(['wait', `--timeout=${WAIT_MAX_MIN * 60}`])
  expect(w.seen[1]!.opts.timeout! > WAIT_MAX_MIN * 60_000).toBe(true)
  let other = ''
  await fake({ code: 1, out: '', err: 'boom' }).tool('wait').execute({}, { sessionID: 's' }).catch(e => { other = e.message })
  expect(other).toBe('boom\n(exit 1)')
})

test('the typed start gives wb start its argv and the spec on stdin, and {args} alone stays as it was', () => {
  expect(startCall({ name: 't', spec: 'Do x.', repo: '.', role: 'trivial', test: '-q pytest', skill: ['a', 'b'],
    image: ['/i.png'], setup: 'npm ci', auto_fix: 0, model: 'q', args: ['--copy', 'node_modules'] })).toEqual({
    args: ['t', '--spec', '-', '--repo=.', '--role=trivial', '--test=-q pytest', '--model=q', '--setup=npm ci',
      '--skill=a', '--skill=b', '--image=/i.png', '--auto-fix=0', '--copy', 'node_modules'],
    stdin: 'Do x.',
  })
  expect(startCall({ name: 't', spec_file: 'spec.md' })).toEqual({ args: ['t', '--spec=spec.md'], stdin: undefined })
  expect(startCall({ args: ['t', '--spec', 'f', '--model', 'x'] })).toEqual({ args: ['t', '--spec', 'f', '--model', 'x'] })
  // every field is a property of the schema, and the schema takes no other
  const props = Object.keys((START_INPUT as { properties: object }).properties)
  expect(props.sort()).toEqual(['args', 'auto_fix', 'image', 'model', 'name', 'repo', 'role', 'setup', 'size', 'skill', 'spec', 'spec_file', 'test'])
})

test('every schema names only fields its call reads, and its required ones', async () => {
  for (const spec of SPECS) {
    const schema = spec.input as { properties: Record<string, unknown>; required: string[]; additionalProperties: boolean }
    expect(schema.additionalProperties).toBe(false)
    expect(schema.required.filter(r => !(r in schema.properties))).toEqual([])
  }
  expect(SPECS.map(s => s.name)).toEqual(['status', 'diff', 'feedback', 'accept', 'discard', 'wait'])
})

test("the runner runs the options' wb as the session, without Claude Code's or an agent's marks, stdin fed, and stops on the call's signal", async () => {
  const dir = mkdtempSync(join(tmpdir(), 'wb-tools-'))
  try {
    const wb = join(dir, 'wb')
    writeFileSync(wb, '#!/bin/sh\nif [ "$1" = sleep ]; then exec sleep 30; fi\n' +
      'printf "%s|%s|%s|%s|" "$*" "$OPENCODE_SESSION_ID" "${CLAUDECODE:-none}" "${WB_TASK:-none}"; cat; pwd\n')
    chmodSync(wb, 0o755)
    const run = runner({ wb }, { PATH: '/usr/bin:/bin', CLAUDECODE: '1', WB_TASK: 't', OPENCODE_SESSION_ID: 'other' })
    const r = await run(['status', 'a b'], { session: 'ses_x', cwd: dir, input: 'the spec' })
    expect(r).toEqual({ code: 0, out: `status a b|ses_x|none|none|the spec${dir}\n`, err: '' })
    const stop = new AbortController()
    const at = Date.now()
    const pending = run(['sleep'], { session: 's', signal: stop.signal })
    setTimeout(() => stop.abort(), 50)
    expect((await pending).code === 0).toBe(false)
    expect(Date.now() - at < 10_000).toBe(true)
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test("a session's directory is its location with its subpath, read once; the plugin's without one", async () => {
  let asked = 0
  const dir = sessionDirs({
    location: { directory: '/plugin' },
    session: { get: async ({ sessionID }) => { asked++; return sessionID === 'a' ? { location: { directory: '/r' }, subpath: 'pkg' } : {} } },
  })
  expect([await dir('a'), await dir('a'), await dir('b')]).toEqual(['/r/pkg', '/r/pkg', '/plugin'])
  expect(asked).toBe(2)
})

test("setup registers the six in the workbench namespace, direct tools, under the namespace's one description, and nothing without a tool domain", async () => {
  const added: { name: string; options: { namespace: string; codemode: boolean } }[] = []
  const namespaces: unknown[] = []
  let disposed = false
  const stop = await tools({
    options: {},
    tool: {
      transform: async (cb: (e: { namespace(n: unknown): void; add(t: unknown): void }) => void) => {
        cb({ namespace: n => namespaces.push(n), add: t => added.push(t as never) })
        return { dispose: async () => { disposed = true } }
      },
    },
  } as never)
  expect(added.map(t => t.name)).toEqual(SPECS.map(s => s.name))
  expect(added.every(t => t.options.namespace === NAMESPACE && t.options.codemode === false)).toBe(true)
  expect(namespaces).toEqual([{ name: 'workbench', description: NAMESPACE_DESCRIPTION }])
  await stop!()
  expect(disposed).toBe(true)
  expect(await tools({ options: {} } as never)).toBeUndefined()
})

test('the docs list each tool by its id, as the model sees it', () => {
  const docs = readFileSync(fileURLToPath(new URL('../../ARCHITECTURE.md', import.meta.url)), 'utf8')
  for (const s of SPECS) expect(docs.includes(`workbench_${s.name}`)).toBe(true)
})
