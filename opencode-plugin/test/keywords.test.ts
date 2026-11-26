import { expect, test } from 'bun:test'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { keywords, TOGGLED } from '../../mod/hooks/keywords.ts'
import { AGENT } from '../agent.ts'
import { SOURCE as CHAT_SOURCE, WEB_NOTE } from '../chat.ts'
import { forDriver, keywordHooks, Keywording, userWords, type Context, type KeywordsHost } from '../keywords.ts'
import { SOURCE as NOTICE_SOURCE } from '../notices.ts'

// The keywords (#1278) on OpenCode, against a fake wb and a fake session API:
// `BUN_BE_BUN=1 opencode test opencode-plugin/test/keywords.test.ts`.

const RULE = "The user wrote `deepcode`. Follow the /workbench skill's *Deep workflows*: run `wb run start` once."
const ON = 'Dynamic workflows (deepmode): On. substantive requests run as deep workflows (wb run)'

test('the shared module reads every fixture case as wb\'s runs.keywords does', () => {
  // the one reference for the mod, this plugin and wb: tests/fixtures/keywords.json
  const cases = JSON.parse(readFileSync(fileURLToPath(new URL('../../tests/fixtures/keywords.json', import.meta.url)), 'utf8')).cases
  expect(cases.length > 10).toBe(true)
  for (const c of cases) expect([c.text, keywords(c.text)]).toEqual([c.text, { deepcode: c.deepcode, deepmode: c.deepmode }])
})

function world(roots: Record<string, boolean> = {}) {
  const ran: { args: string; session?: string }[] = []
  const run = async (args: readonly string[], opts: { session?: string }) => {
    ran.push({ args: args.join(' '), session: opts.session })
    const a = args.join(' ')
    return { ok: true, out: a === 'deepmode --rule' ? RULE : a === 'deepmode on' ? ON : '' }
  }
  const k = new Keywording(run, async sid => roots[sid] ?? true)
  return { k, ran }
}
const request = (agent: string, ...msgs: { id: string; text: string; metadata?: Record<string, unknown> }[]): Context => ({
  sessionID: 's1', agent,
  messages: msgs.map(m => ({ id: m.id, role: 'user', content: [{ type: 'text', text: m.text }], ...(m.metadata ? { metadata: m.metadata } : {}) })),
})
const texts = (c: Context, i = 0) => c.messages[i]!.content.map(p => p.text)

test('deepcode typed in a root session gives the Workbench agent wb\'s rule beside the words, read once', async () => {
  const { k, ran } = world()
  await k.prompt({ sessionID: 's1', messageID: 'm1', prompt: { text: 'deepcode audit the parser' } })
  await k.prompt({ sessionID: 's1', messageID: 'm2', prompt: { text: 'please DeepCode this' } })
  expect(ran.map(r => r.args)).toEqual(['deepmode --rule'])
  const c = request(AGENT, { id: 'm0', text: 'hello' }, { id: 'm1', text: 'deepcode audit the parser' })
  await k.context(c)
  expect(texts(c, 0)).toEqual(['hello'])
  expect(texts(c, 1)).toEqual(['deepcode audit the parser', forDriver(RULE)])
  expect(forDriver(RULE).includes("your Workbench instructions' *Deep workflows*")).toBe(true)
  expect(c.messages[1]!.metadata).toEqual({}) // so OpenCode makes it a message again, as one with metadata
  const again = request(AGENT, { id: 'm1', text: 'deepcode audit the parser' })
  await k.context(again)
  expect(texts(again)).toEqual(texts(c, 1)) // every request alike: the prompt cache holds
  const build = request('build', { id: 'm1', text: 'deepcode audit the parser' })
  await k.context(build)
  expect(texts(build)).toEqual(['deepcode audit the parser']) // no Workbench instructions to follow there
})

test('a deepmode on line runs wb deepmode on as the session, and the model reads that it is done, on any agent', async () => {
  const { k, ran } = world()
  await k.prompt({ sessionID: 's1', messageID: 'm1', prompt: { text: 'deepmode on' } })
  expect(ran).toEqual([{ args: 'deepmode on', session: 's1' }])
  const c = request('build', { id: 'm1', text: 'deepmode on' })
  await k.context(c)
  expect(texts(c)).toEqual(['deepmode on', TOGGLED(true, ON)])
})

test('only the user\'s words count: no notice, command, agent\'s message or subagent prompt, and no wb run without a keyword', async () => {
  const { k, ran } = world({ child: false })
  for (const [text, metadata] of [['deepcode x', { source: NOTICE_SOURCE }], ['[workbench] Task `t` deepcode', undefined],
    ['[agent hermes] deepcode x', { source: CHAT_SOURCE }], ['deepcode x', { source: 'goal' }], ['the deepcoder repo', undefined]] as const)
    await k.prompt({ sessionID: 's1', messageID: `m-${text}`, prompt: { text }, metadata })
  await k.prompt({ sessionID: 'child', messageID: 'c1', prompt: { text: 'deepmode on\ndeepcode it' } })
  expect(ran).toEqual([])
  expect(k.marks.size).toBe(0)
  // the user's web chat message: their words alone, as the mod reads the message's text
  const web = { source: CHAT_SOURCE, chat: 'x' }
  expect(userWords(`[web chat] deepmode on\n\n${WEB_NOTE}`, web)).toBe('deepmode on')
  expect(userWords(`[web chat] (about task fix-it) look\n[attached: /h/deepcode.png (2 KB, image/png)]\n\n${WEB_NOTE}`, web)).toBe('look')
  await k.prompt({ sessionID: 's1', messageID: 'w1', prompt: { text: '[web chat] deepcode fix login\n\n(note)' }, metadata: { source: CHAT_SOURCE, chat: 'x' } })
  expect(k.marks.get('w1')).toEqual({ deepcode: true, toggled: '' })
})

test('the server part registers both hooks and they act on the session\'s own prompts', async () => {
  const hooks: Record<string, (e: any) => unknown> = {}
  const host: KeywordsHost = {
    options: {},
    session: { hook: async (name, cb) => { hooks[name] = cb; return { dispose: async () => {} } }, get: async () => ({ data: { id: 's1' } }) },
  }
  const ran: string[] = []
  const stop = await keywordHooks(host, async args => { ran.push(args.join(' ')); return { ok: true, out: RULE } })
  expect(Object.keys(hooks).sort()).toEqual(['context', 'prompt'])
  await hooks.prompt!({ sessionID: 's1', messageID: 'm1', prompt: { text: 'deepcode now' }, delivery: 'steer' })
  const c = request(AGENT, { id: 'm1', text: 'deepcode now' })
  await hooks.context!(c)
  expect(texts(c)).toEqual(['deepcode now', forDriver(RULE)])
  expect(ran).toEqual(['deepmode --rule'])
  await stop!()
})
