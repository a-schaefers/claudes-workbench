import { expect, test } from 'bun:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { keywords, onlyToggles } from '../../mod/hooks/keywords.ts'
import { Deep, DEEPMODE, WORKBENCH_DEEPMODE, deep, FALLBACK, usersWords, type ContextEvent, type DeepHost } from '../deep.ts'
import type { Ran, SessionRun } from '../commands.ts'

// Deep workflows' words in OpenCode (#1278): `BUN_BE_BUN=1 opencode test opencode-plugin/test/deep.test.ts`.

test('keywords.ts reads every case of tests/fixtures/keywords.json as runs.keywords does', () => {
  const doc = JSON.parse(readFileSync(fileURLToPath(new URL('../../tests/fixtures/keywords.json', import.meta.url)), 'utf8'))
  expect(doc.cases.length > 10).toBe(true)
  for (const c of doc.cases) expect([c.text, keywords(c.text)]).toEqual([c.text, { deepcode: c.deepcode, deepmode: c.deepmode }])
  expect(onlyToggles('  deepmode on\n')).toBe(true)
  expect(onlyToggles('deepmode on\nrename the flag')).toBe(false)
  expect(onlyToggles('rename the flag')).toBe(false)
})

function world(outs: Record<string, Ran> = {}) {
  const ran: { args: readonly string[]; session?: string; cwd?: string }[] = []
  const posted: { text: string; resume?: boolean }[] = []
  const run: SessionRun = async (args, opts) => {
    ran.push({ args, session: opts.session, cwd: opts.cwd })
    const key = args.join(' ')
    return outs[key] ?? { ok: true, out: key === 'deepmode --rule' ? 'RULE: a deep workflow' : `Dynamic workflows (deepmode): ${args[1] === 'off' ? 'Off' : 'On'}.` }
  }
  const host: DeepHost = {
    options: {},
    session: {
      get: async ({ sessionID }) => ({ id: sessionID, agent: 'workbench', location: { directory: '/repo' } }),
      synthetic: async (input) => { posted.push(input) },
    },
  }
  const d = new Deep(host, run)
  const ctx = (sessionID = 's1', messages?: ContextEvent['messages']): string[] => {
    const input: ContextEvent = { sessionID, system: [{ type: 'text', text: 'base' }], messages }
    d.context(input)
    return input.system.slice(1).map(p => p.text)
  }
  return { d, ran, posted, ctx, host }
}

test("deepcode typed adds wb's rule to that prompt's requests alone; another plugin's prompt, an agent's or backticks add nothing", async () => {
  const w = world()
  await w.d.prompted({ sessionID: 's1', messageID: 'm1', prompt: { text: 'deepcode fix the flaky login test' } })
  expect(w.ctx('s1', [{ role: 'user', id: 'm1' }, { role: 'assistant', id: 'a1' }])).toEqual(['RULE: a deep workflow'])
  expect(w.ctx('s2')).toEqual([])                                                  // that session only
  expect(w.ctx('s1', [{ role: 'user', id: 'm1' }, { role: 'user', id: 'm2' }])).toEqual([])  // a newer user message: done
  for (const e of [{ prompt: { text: 'deepcode it' }, metadata: { source: 'goal' } },
    { prompt: { text: '[agent bot] deepcode it' } }, { prompt: { text: '[workbench] deepcode' } },
    { prompt: { text: 'what does `deepcode` do?' } }, { prompt: { text: 'ultracode the migration' } },
    { prompt: { text: '[agent bot] deepcode it' }, metadata: { source: 'workbench.chat' } }]) {
    await w.d.prompted({ sessionID: 's1', messageID: 'm3', ...e })
    expect([e.prompt.text, w.ctx('s1')]).toEqual([e.prompt.text, []])
  }
  // the user's own web message, as chat.ts delivers it
  await w.d.prompted({ sessionID: 's1', messageID: 'm4', prompt: { text: '[web chat] deepcode audit the parser\n\n(The user wrote this…)' },
    metadata: { source: 'workbench.chat', chat: 'c1' } })
  expect(w.ctx('s1')).toEqual(['RULE: a deep workflow'])
  expect(w.ran.filter(r => r.args[0] === 'deepmode')).toEqual([{ args: ['deepmode', '--rule'], session: 's1', cwd: undefined }])  // read once
  // a wb that can't say its rule: the plugin's own words, said all the same
  const old = world({ 'deepmode --rule': { ok: false, out: 'wb: unknown flag' } })
  await old.d.prompted({ sessionID: 's1', prompt: { text: 'deepcode it' } })
  expect(old.ctx()).toEqual([FALLBACK])
  expect(usersWords({ sessionID: 's', prompt: { text: 'x' }, metadata: {} })).toBe('x')
})

test('deepmode on typed runs wb deepmode on as the session, and its turn is told; a mention toggles nothing; /deepmode sets or shows it', async () => {
  const w = world()
  await w.d.prompted({ sessionID: 's1', messageID: 'm1', prompt: { text: 'deepmode on' } })
  expect(w.ran).toEqual([{ args: ['deepmode', 'on'], session: 's1', cwd: '/repo' }])
  expect(w.ctx()).toEqual(['[workbench] The user\'s `deepmode on` line set it (wb ran `wb deepmode on`): Dynamic workflows (deepmode): On. ' +
    'Nothing else to do for it: say so in one line.'])
  await w.d.prompted({ sessionID: 's1', prompt: { text: 'deepmode off\nthen rename the flag' } })
  expect(w.ctx()[0]!.endsWith('Dynamic workflows (deepmode): Off.')).toBe(true)
  await w.d.prompted({ sessionID: 's1', prompt: { text: 'turn deepmode on please' } })
  expect(w.ran.length).toBe(2)
  expect(w.d.command().name).toBe(WORKBENCH_DEEPMODE)  // #1628: the family's name, /deepmode its alias
  const cmd = w.d.command(DEEPMODE)
  expect(cmd.name).toBe(DEEPMODE)
  await cmd.execute({ sessionID: 's1', prompt: { text: ' OFF ' } })
  await cmd.execute({ sessionID: 's1', prompt: { text: '' } })
  expect(w.ran.slice(2).map(r => r.args)).toEqual([['deepmode', 'off'], ['deepmode']])
  expect(w.posted.map(p => [p.text, p.resume])).toEqual([
    ['[workbench] The user ran /deepmode off: Dynamic workflows (deepmode): Off.\nNothing to do now.', false],
    ['[workbench] The user ran /deepmode: Dynamic workflows (deepmode): On.\nNothing to do now.', false]])
  expect(await cmd.execute({ sessionID: 's1', prompt: { text: 'maybe' } }).then(() => '', (e: Error) => e.message))
    .toBe('/deepmode on, /deepmode off, or /deepmode alone to see it')
})

test("a subagent's prompt is the driver's words, never the user's: its deepmode line toggles nothing and its deepcode adds no rule (#1325)", async () => {
  const w = world()
  const get = w.host.session!.get!
  Object.assign(w.host.session!, { get: async (input: { sessionID: string }) =>
    input.sessionID === 'child' ? { data: { id: 'child', parentID: 's1', agent: 'general', location: { directory: '/repo' } } } : get(input) })
  await w.d.prompted({ sessionID: 'child', messageID: 'c1', prompt: { text: 'You are a subagent spawned by another session.\ndeepmode off\ndeepcode it' } })
  expect([w.ran, w.ctx('child')]).toEqual([[], []])
  await w.d.prompted({ sessionID: 's1', messageID: 'm1', prompt: { text: 'deepmode off' } })  // the root session's own: still the user's
  expect(w.ran.map(r => r.args)).toEqual([['deepmode', 'off']])
})

test('the server part takes the prompt and context hooks and adds /workbench-deepmode and /deepmode, each where the server has it', async () => {
  const hooks: string[] = []
  const added: string[] = []
  const host: DeepHost = {
    options: {},
    session: { hook: async (name) => { hooks.push(name); return { dispose: async () => {} } } },
    command: { transform: async (cb) => { cb({ add: (d) => added.push(d.name) }); return { dispose: async () => {} } } },
  }
  expect(typeof await deep(host, async () => ({ ok: true, out: '' }))).toBe('function')
  expect(hooks).toEqual(['prompt', 'context'])
  expect(added).toEqual([WORKBENCH_DEEPMODE, DEEPMODE])
  expect(await deep({ options: {} })).toBeUndefined()
})
