import { expect, test } from 'bun:test'
import {
  AGENT_NOTE, AWAY_QUESTION, BACK_AFTER_MS, ChatLane, NOTICE_AFTER_MS, REPLY_ID, RETRY_MS, SOURCE, WEB_NOTE, chat,
  chatPrompt, inboxId, replyTool,
} from '../chat.ts'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { waitingFact, WAITING_AFTER_MS } from '../../mod/hooks/waiting.ts'

// The web chat on OpenCode's path (#1319, #1340), under OpenCode's own Bun: the lane against a
// fake wb and a fake session API. wb's side of each call: tests/test_chat_opencode.py.

type Call = { args: string[]; session?: string }

// what a promise rejected with, '' when it resolved
async function failure(p: Promise<unknown>): Promise<string> {
  try { await p } catch (e) { return String((e as Error).message) }
  return ''
}

function world(files: Record<string, string> = {}) {
  const calls: Call[] = []
  const prompts: Record<string, unknown>[] = []
  const answers: Record<string, { ok: boolean; out: string }> = {
    'chat url --json': { ok: true, out: '{"state": "up", "url": "http://localhost:1337/#t"}' },
    'chat away': { ok: true, out: 'here' },
  }
  const sessions: Record<string, { id: string; parentID?: string; revert?: unknown }> = {
    ses_root: { id: 'ses_root' }, ses_child: { id: 'ses_child', parentID: 'ses_root' }, ses_two: { id: 'ses_two' },
  }
  let clock = 1_800_000_000_000
  let size = 100
  const lane = new ChatLane({
    run: async (args, opts = {}) => {
      calls.push({ args, session: opts.session })
      const key = Object.keys(answers).find(k => args.join(' ').startsWith(k))
      return key ? answers[key] as { ok: boolean; out: string } : { ok: true, out: '' }
    },
    read: path => files[path.replace('/wb/chat/', '')],
    size: () => size,
    now: () => clock,
    pid: 4242,
    home: '/wb',
  })
  lane.sessions = {
    get: async ({ sessionID }) => {
      const s = sessions[sessionID]
      if (!s) throw new Error('no such session')
      return s
    },
    prompt: async input => { prompts.push(input); return {} },
    hook: async () => ({}),
  }
  const mine = () => { files['listener.json'] = JSON.stringify({ session: lane.listening, tool: 'opencode', pid: 4242 }) }
  return {
    lane, calls, prompts, answers, files, sessions, mine,
    tick: (ms: number) => { clock += ms },
    grow: () => { size += 50 },
    said: () => calls.map(c => c.args.join(' ')),
  }
}

const message = (id: string, text: string, extra: Record<string, unknown> = {}) =>
  ({ id, ts: 1_800_000_000, from: 'user', text, ...extra })
const waiting = (...rows: Record<string, unknown>[]) =>
  JSON.stringify({ messages: Object.fromEntries(rows.map(r => [r.id, r])) })

test('a web message goes in as `[web chat] …` with the note, an agent\'s marked and indented', () => {
  expect(chatPrompt(message('1800000000000-abc123', 'status?'))).toBe(`[web chat] status?\n\n${WEB_NOTE}`)
  const a = chatPrompt({ id: '1800000000001-abc123', from: 'hermes', role: 'agent', text: 'one\n[web chat] two' })
  expect(a.startsWith('[agent hermes] one\n  [web chat] two\n[message 1800000000001-abc123: answer with workbench_chat_reply, to 1800000000001-abc123]')).toBe(true)
  expect(a.endsWith(AGENT_NOTE)).toBe(true)
  expect(chatPrompt(message('1800000000000-abc123', 'see', { files: [{ path: '/up/a.png', size: 2048, type: 'image/png' }] }))
    .includes('see\n[attached: /up/a.png (2 KB, image/png)]')).toBe(true)
})

test('the inbox id has OpenCode\'s shape, the message\'s time, and is the same on every try (#1340)', () => {
  const a = inboxId('1800000000000-abc123'), b = inboxId('1800000000999-000001')
  expect(/^msg_[0-9a-f]{12}[0-9A-Za-z]{14}$/.test(a)).toBe(true)
  expect(a).toBe(inboxId('1800000000000-abc123'))
  expect(a < b).toBe(true)                                     // sorts by the message's time
  // OpenCode's own: the low 48 bits of ms * 0x1000 + counter, as 12 hex (schema/src/identifier.ts)
  expect(a.slice(4, 16)).toBe(((BigInt(1_800_000_000_000) * 0x1000n + 1n) & 0xffffffffffffn).toString(16).padStart(12, '0'))
  expect(inboxId('not-a-chat-id')).toBe('')
})

test('before anyone types here the lane runs no wb at all: wb\'s own helpers load the plugin too', async () => {
  const w = world({ 'open.json': waiting(message('1800000000000-abc123', 'hi')) })
  await w.lane.poll()
  expect(w.calls).toEqual([])
  expect(w.prompts).toEqual([])
})

test('a prompt typed in a root session takes the chat; a child\'s, a marked one or our own does not', async () => {
  const w = world()
  await w.lane.typed({ sessionID: 'ses_child', prompt: { text: 'hi' } })
  await w.lane.typed({ sessionID: 'ses_root', prompt: { text: 'go' }, metadata: { automation: { id: 'a' } } })
  await w.lane.typed({ sessionID: 'ses_root', prompt: { text: '[web chat] hi' }, metadata: {} })
  expect(w.lane.listening).toBe('')
  await w.lane.typed({ sessionID: 'ses_root', prompt: { text: 'go' } })
  expect(w.said()).toEqual(['chat url --json', 'chat back', 'chat listener ses_root --pid 4242'])
  expect(w.calls.at(-1)?.session).toBe('ses_root')
  expect([w.lane.listening, w.lane.holding]).toEqual(['ses_root', true])
})

test('a waiting message is claimed, prompted in as a steer, and acked once OpenCode delivers it', async () => {
  const id = '1800000000000-abc123'
  const w = world({ 'open.json': waiting(message(id, 'status?')) })
  await w.lane.typed({ sessionID: 'ses_root', prompt: { text: 'go' } })
  w.mine()
  w.calls.length = 0
  await w.lane.poll()
  expect(w.said()).toEqual([`chat claim ${id} --by ses_root`])
  expect(w.prompts).toEqual([{ sessionID: 'ses_root', id: inboxId(id), text: chatPrompt(message(id, 'status?')),
    metadata: { source: SOURCE, chat: id }, delivery: 'steer' }])
  await w.lane.poll()
  expect(w.prompts.length).toBe(1)                              // once
  await w.lane.event({ id: 'e1', type: 'session.inbox.delivered', data: { sessionID: 'ses_root', inboxID: inboxId(id) } })
  await w.lane.event({ id: 'e1', type: 'session.inbox.delivered', data: { sessionID: 'ses_root', inboxID: inboxId(id) } })
  expect(w.said().filter(s => s.startsWith('chat ack'))).toEqual([`chat ack ${id} --by ses_root --how now --away`])
})

test('the turn\'s answer goes to the message it took unless the session replied, then the turn\'s end', async () => {
  const id = '1800000000000-abc123'
  const w = world({ 'open.json': waiting(message(id, 'status?')) })
  await w.lane.typed({ sessionID: 'ses_root', prompt: { text: 'go' } })
  w.mine()
  await w.lane.event({ id: 's', type: 'session.execution.started', data: { sessionID: 'ses_root' } })
  await w.lane.poll()
  await w.lane.event({ id: 'd', type: 'session.inbox.delivered', data: { sessionID: 'ses_root', inboxID: inboxId(id) } })
  await w.lane.event({ id: 't1', type: 'session.text.ended', data: { sessionID: 'ses_root', assistantMessageID: 'msg_1', text: 'thinking aloud' } })
  await w.lane.event({ id: 't2', type: 'session.text.ended', data: { sessionID: 'ses_root', assistantMessageID: 'msg_2', text: 'All green.' } })
  await w.lane.event({ id: 't2', type: 'session.text.ended', data: { sessionID: 'ses_root', assistantMessageID: 'msg_2', text: 'All green.' } }) // a second instance's feed: once
  await w.lane.event({ id: 't3', type: 'session.text.ended', data: { sessionID: 'ses_child', assistantMessageID: 'msg_3', text: 'a subagent' } })
  // a subagent's own turn is no turn of the chat's: no record of it
  await w.lane.event({ id: 'cs', type: 'session.execution.started', data: { sessionID: 'ses_child' } })
  w.grow()
  await w.lane.event({ id: 'cx', type: 'session.execution.succeeded', data: { sessionID: 'ses_child' } })
  expect(w.said().some(s => s.endsWith('--by ses_child'))).toBe(false)
  w.calls.length = 0
  await w.lane.event({ id: 'x', type: 'session.execution.succeeded', data: { sessionID: 'ses_root' } })
  expect(w.said()).toEqual([
    `chat reply --turn-answer --if-owed --text=All green. --to=${id}`, // the last message's text
    `chat ended ${id} --by ses_root`,                                  // after the post (#1352)
    'chat listener ses_root --pid 4242',                               // busy off
  ])
  expect(w.lane.owed.size).toBe(0)
})

test('a turn that said nothing leaves the message owed, and the next turn\'s end posts its answer', async () => {
  const id = '1800000000000-abc123'
  const w = world({ 'open.json': waiting(message(id, 'status?')) })
  await w.lane.typed({ sessionID: 'ses_root', prompt: { text: 'go' } })
  w.mine()
  await w.lane.poll()
  await w.lane.event({ id: 'd', type: 'session.inbox.delivered', data: { sessionID: 'ses_root', inboxID: inboxId(id) } })
  await w.lane.event({ id: 'x1', type: 'session.execution.interrupted', data: { sessionID: 'ses_root', reason: 'user' } })
  expect(w.said().some(s => s.startsWith('chat reply'))).toBe(false)
  await w.lane.event({ id: 's2', type: 'session.execution.started', data: { sessionID: 'ses_root' } })
  await w.lane.event({ id: 't', type: 'session.text.ended', data: { sessionID: 'ses_root', assistantMessageID: 'm', text: 'Done.' } })
  await w.lane.event({ id: 'x2', type: 'session.execution.succeeded', data: { sessionID: 'ses_root' } })
  expect(w.said().includes(`chat reply --turn-answer --if-owed --text=Done. --to=${id}`)).toBe(true)
})

test('an agent\'s message into a running turn is acked `into`, never away, and answered in a post of its own', async () => {
  const u = '1800000000000-aaaaaa', a = '1800000000001-bbbbbb'
  const w = world({ 'open.json': waiting(message(u, 'hi'), { id: a, ts: 1_800_000_001, from: 'hermes', role: 'agent', text: 'sum' }) })
  await w.lane.typed({ sessionID: 'ses_root', prompt: { text: 'go' } })
  w.mine()
  await w.lane.event({ id: 's', type: 'session.execution.started', data: { sessionID: 'ses_root' } })
  await w.lane.poll()
  for (const id of [u, a]) await w.lane.event({ id: `d${id}`, type: 'session.inbox.delivered', data: { sessionID: 'ses_root', inboxID: inboxId(id) } })
  expect(w.said().includes(`chat ack ${a} --by ses_root --how into`)).toBe(true)
  await w.lane.event({ id: 't', type: 'session.text.ended', data: { sessionID: 'ses_root', assistantMessageID: 'm', text: 'ok' } })
  await w.lane.event({ id: 'x', type: 'session.execution.succeeded', data: { sessionID: 'ses_root' } })
  const posts = w.said().filter(s => s.startsWith('chat reply'))
  expect(posts).toEqual([`chat reply --turn-answer --if-owed --text=ok --to=${u}`, `chat reply --turn-answer --if-owed --text=ok --to=${a}`])
})

test('a message given to one session is answered by that session\'s turn, though the user typed in another since', async () => {
  const id = '1800000000000-abc123'
  const w = world({ 'open.json': waiting(message(id, 'status?')) })
  await w.lane.typed({ sessionID: 'ses_root', prompt: { text: 'go' } })
  w.mine()
  await w.lane.event({ id: 's1', type: 'session.execution.started', data: { sessionID: 'ses_root' } })
  await w.lane.poll()
  await w.lane.event({ id: 'd', type: 'session.inbox.delivered', data: { sessionID: 'ses_root', inboxID: inboxId(id) } })
  await w.lane.event({ id: 'x0', type: 'session.execution.interrupted', data: { sessionID: 'ses_root', reason: 'user' } })
  w.lane.owed.get(id)!.sender = 'hermes'                         // an agent's: typing elsewhere settles nothing
  await w.lane.typed({ sessionID: 'ses_two', prompt: { text: 'other work' } })
  expect(w.lane.listening).toBe('ses_two')
  await w.lane.event({ id: 's3', type: 'session.execution.started', data: { sessionID: 'ses_root' } }) // its next turn
  await w.lane.event({ id: 's2', type: 'session.execution.started', data: { sessionID: 'ses_two' } })
  await w.lane.event({ id: 't2', type: 'session.text.ended', data: { sessionID: 'ses_two', assistantMessageID: 'm2', text: 'unrelated' } })
  await w.lane.event({ id: 'x2', type: 'session.execution.succeeded', data: { sessionID: 'ses_two' } })
  expect(w.said().some(s => s.startsWith('chat reply'))).toBe(false)
  await w.lane.event({ id: 't1', type: 'session.text.ended', data: { sessionID: 'ses_root', assistantMessageID: 'm1', text: 'Summary.' } })
  await w.lane.event({ id: 'x1', type: 'session.execution.succeeded', data: { sessionID: 'ses_root' } })
  expect(w.said().includes(`chat reply --turn-answer --if-owed --text=Summary. --to=${id}`)).toBe(true)
  expect(w.said().includes(`chat ended ${id} --by ses_root`)).toBe(true) // the interrupted turn's record took it
})

test('a typed prompt settles what the user is owed (they are back), never an agent\'s', async () => {
  const w = world()
  w.lane.owed.set('u', { session: 'ses_root', sender: 'user', answer: '' })
  w.lane.owed.set('a', { session: 'ses_root', sender: 'hermes', answer: '' })
  await w.lane.typed({ sessionID: 'ses_root', prompt: { text: 'go' } })
  expect([...w.lane.owed.keys()]).toEqual(['a'])
})

test('while a revert is staged nothing is delivered: a prompt would commit it (#1340)', async () => {
  const id = '1800000000000-abc123'
  const w = world({ 'open.json': waiting(message(id, 'hi')) })
  await w.lane.typed({ sessionID: 'ses_root', prompt: { text: 'go' } })
  w.mine()
  await w.lane.event({ id: 'r', type: 'session.revert.staged', data: { sessionID: 'ses_root' } })
  await w.lane.poll()
  expect(w.prompts).toEqual([])
  await w.lane.event({ id: 'c', type: 'session.revert.cleared', data: { sessionID: 'ses_root' } })
  await w.lane.poll()
  expect(w.prompts.length).toBe(1)
})

test('with another listener\'s claim standing this server delivers nothing', async () => {
  const id = '1800000000000-abc123'
  const w = world({ 'open.json': waiting(message(id, 'hi')) })
  w.answers['chat listener'] = { ok: false, out: 'wb: standing by: claude listens in /c' }
  await w.lane.typed({ sessionID: 'ses_root', prompt: { text: 'go' } })
  w.files['listener.json'] = JSON.stringify({ session: 'claude-key', pid: 1, cwd: '/c' })
  await w.lane.poll()
  expect(w.lane.holding).toBe(false)
  expect(w.prompts).toEqual([])
  expect(w.said().some(s => s.startsWith('chat claim'))).toBe(false)
})

test('an emptied claim (wb chat off, then on) is taken again at once', async () => {
  const w = world()
  await w.lane.typed({ sessionID: 'ses_root', prompt: { text: 'go' } })
  w.files['listener.json'] = '{}'
  w.calls.length = 0
  await w.lane.poll()
  expect(w.said()).toEqual(['chat listener ses_root --pid 4242'])
})

test('a refused claim waits a minute; an expired one is never tried again', async () => {
  const a = '1800000000000-aaaaaa', b = '1800000000001-bbbbbb'
  const w = world({ 'open.json': waiting(message(a, 'old'), message(b, 'theirs')) })
  await w.lane.typed({ sessionID: 'ses_root', prompt: { text: 'go' } })
  w.mine()
  w.answers[`chat claim ${a}`] = { ok: false, out: `wb: ${a} expired: over 6 hours unread` }
  w.answers[`chat claim ${b}`] = { ok: false, out: `wb: ${b} is another session's to submit` }
  await w.lane.poll()
  w.calls.length = 0
  await w.lane.poll()
  expect(w.said().filter(s => s.startsWith('chat claim'))).toEqual([])
  w.tick(61_000)
  await w.lane.poll()
  expect(w.said().filter(s => s.startsWith('chat claim'))).toEqual([`chat claim ${b} --by ses_root`])
  expect(w.prompts).toEqual([])
})

// The chat-path review (2026-11-25): a cancel was taken for a failed delivery and sent again a
// minute later, so the user's cancel never stuck
test('a message the user cancels in OpenCode is settled with a receipt and never prompted again', async () => {
  const id = '1800000000000-abc123'
  const w = world({ 'open.json': waiting(message(id, 'status?')) })
  await w.lane.typed({ sessionID: 'ses_root', prompt: { text: 'go' } })
  w.mine()
  await w.lane.poll()
  expect(w.prompts.length).toBe(1)
  w.calls.length = 0
  await w.lane.event({ id: 'c', type: 'session.inbox.cancelled', data: { sessionID: 'ses_root', inboxID: inboxId(id) } })
  expect(w.said()).toEqual([`chat ack ${id} --by ses_root --how now`, 'chat back'])
  w.tick(RETRY_MS + 1)
  await w.lane.poll()
  expect(w.prompts.length).toBe(1)
  expect(w.lane.owed.has(id)).toBe(false)
})

test('a dialog in the listening session gets the page\'s line after 30 s, and answering it is being back (#1340 E)', async () => {
  const w = world()
  await w.lane.typed({ sessionID: 'ses_root', prompt: { text: 'go' } })
  w.mine()
  w.answers['chat notice --tool'] = { ok: true, out: '1800000000500-cafe01' }
  await w.lane.event({ id: 'p', type: 'permission.asked', data: { id: 'per_1', sessionID: 'ses_child', action: 'shell', resources: ['rm -rf build'] } })
  await w.lane.event({ id: 'q', type: 'permission.asked', data: { id: 'per_2', sessionID: 'ses_two', action: 'edit', resources: ['x'] } })
  w.calls.length = 0
  await w.lane.poll()
  expect(w.said().some(s => s.startsWith('chat notice'))).toBe(false)
  w.tick(NOTICE_AFTER_MS)
  await w.lane.poll()
  expect(w.said().filter(s => s.startsWith('chat notice'))).toEqual(['chat notice --tool=shell --kind=permission --text=rm -rf build'])
  await w.lane.event({ id: 'r', type: 'permission.replied', data: { sessionID: 'ses_child', requestID: 'per_1', reply: 'once' } })
  expect(w.said().slice(-2)).toEqual(['chat back', 'chat notice --done 1800000000500-cafe01'])
  expect(w.lane.dialogs.size).toBe(0)                           // ses_two isn't the listener's
})

test('while away a dialog\'s line goes at once, and a form ended with its turn clears without "back"', async () => {
  const w = world()
  await w.lane.typed({ sessionID: 'ses_root', prompt: { text: 'go' } })
  w.mine()
  w.answers['chat away'] = { ok: true, out: 'away' }
  w.answers['chat notice --tool'] = { ok: true, out: '1800000000500-cafe02' }
  await w.lane.event({ id: 'f', type: 'form.created', data: { form: { id: 'frm_1', sessionID: 'ses_root', title: 'Server asks', metadata: { kind: 'mcp' } } } })
  expect(w.said().includes('chat notice --tool=form --kind=elicitation --text=Server asks')).toBe(true)
  w.calls.length = 0
  await w.lane.event({ id: 'x', type: 'session.execution.interrupted', data: { sessionID: 'ses_root', reason: 'user' } })
  expect(w.said().includes('chat notice --done 1800000000500-cafe02')).toBe(true)
  expect(w.said().includes('chat back')).toBe(false)
})

// The chat-path review (2026-11-25): the TUI's autoaccept (`--auto`) replies to each permission at
// once, and every reply ended away mode though the user was still away
test('a dialog OpenCode answers at once (autoaccept) is not the user being back', async () => {
  const w = world()
  await w.lane.typed({ sessionID: 'ses_root', prompt: { text: 'go' } })
  w.mine()
  await w.lane.event({ id: 'p', type: 'permission.asked', data: { id: 'per_1', sessionID: 'ses_root', action: 'shell', resources: ['ls'] } })
  w.calls.length = 0
  await w.lane.event({ id: 'r', type: 'permission.replied', data: { sessionID: 'ses_root', requestID: 'per_1', reply: 'once' } })
  expect(w.said().includes('chat back')).toBe(false)
  await w.lane.event({ id: 'p2', type: 'permission.asked', data: { id: 'per_2', sessionID: 'ses_root', action: 'shell', resources: ['ls'] } })
  w.tick(BACK_AFTER_MS)
  await w.lane.event({ id: 'r2', type: 'permission.replied', data: { sessionID: 'ses_root', requestID: 'per_2', reply: 'once' } })
  expect(w.said().includes('chat back')).toBe(true)
})

// #1355: the page's state line, from the oldest dialog up two seconds (mod/hooks/waiting.ts, the
// mod's too); the same cases on Claude Code's runtime in mod/hooks/waiting.test.ts
test('waitingFact gives every case of tests/fixtures/story/dialogs.json under OpenCode\'s Bun', () => {
  const doc = JSON.parse(readFileSync(fileURLToPath(new URL('../../tests/fixtures/story/dialogs.json', import.meta.url)), 'utf8'))
  expect(doc.cases.length > 5).toBe(true)
  for (const c of doc.cases) expect([c.why, waitingFact(c.dialogs, c.now)]).toEqual([c.why, c.out])
})

test('a dialog up two seconds is in the claim from the next poll, and settled it is out (#1355)', async () => {
  const w = world()
  await w.lane.typed({ sessionID: 'ses_root', prompt: { text: 'go' } })
  w.mine()
  await w.lane.event({ id: 'p', type: 'permission.asked', data: { id: 'per_1', sessionID: 'ses_child', action: 'shell', resources: ['rm -rf build'] } })
  w.calls.length = 0
  await w.lane.poll()                                            // up 0 s: an auto-accept's would be gone
  expect(w.said().some(s => s.startsWith('chat listener'))).toBe(false)
  w.tick(WAITING_AFTER_MS)
  await w.lane.poll()
  const since = (1_800_000_000_000) / 1000
  expect(w.said().filter(s => s.startsWith('chat listener'))).toEqual([
    `chat listener ses_root --pid 4242 --waiting ${JSON.stringify({ kind: 'permission', tool: 'shell', since, what: 'rm -rf build' })}`])
  w.calls.length = 0
  await w.lane.poll()                                            // said: not again
  expect(w.said().some(s => s.startsWith('chat listener'))).toBe(false)
  await w.lane.event({ id: 'r', type: 'permission.replied', data: { sessionID: 'ses_child', requestID: 'per_1', reply: 'once' } })
  await w.lane.poll()
  expect(w.said().filter(s => s.startsWith('chat listener'))).toEqual(['chat listener ses_root --pid 4242'])
})

test('while away the question tool is refused into the web chat; at the terminal it asks (#1340 B)', async () => {
  const w = world()
  await w.lane.typed({ sessionID: 'ses_root', prompt: { text: 'go' } })
  await w.lane.beforeTool({ tool: 'question', sessionID: 'ses_child' }) // here: no refusal
  w.answers['chat away'] = { ok: true, out: 'away' }
  expect((await failure(w.lane.beforeTool({ tool: 'question', sessionID: 'ses_child' }))).includes(AWAY_QUESTION)).toBe(true)
  await w.lane.beforeTool({ tool: 'question', sessionID: 'ses_two' })   // not the listener's
  await w.lane.beforeTool({ tool: 'shell', sessionID: 'ses_root' })
})

test('chat_reply runs wb chat reply as the calling session\'s root, and says wb\'s refusal', async () => {
  const w = world()
  const runs: Call[] = []
  let answer = { ok: true, out: 'sent to the web chat' }
  const tool = replyTool(w.lane, async (args, opts = {}) => { runs.push({ args, session: opts.session }); return answer })
  expect(tool.options.namespace).toBe('workbench')
  // OpenCode's effective id (core/src/tool/runtime.ts effectiveName), and every text names it
  expect(`${tool.options.namespace.replaceAll('.', '_')}_${tool.name.replace(/[^a-zA-Z0-9_-]/g, '_')}`).toBe(REPLY_ID)
  for (const t of [WEB_NOTE, AGENT_NOTE, AWAY_QUESTION, chatPrompt({ id: '1800000000001-abc123', from: 'x', role: 'agent', text: 'a' })]) {
    expect(t.includes(REPLY_ID)).toBe(true)
    expect(t.replaceAll(REPLY_ID, '').includes('chat_reply')).toBe(false)
  }
  expect(await tool.execute({ text: ' hi ', to: ['1800000000001-bbbbbb'] }, { sessionID: 'ses_child' }))
    .toEqual({ content: 'sent to the web chat' })
  expect(runs).toEqual([{ args: ['chat', 'reply', '--text=hi', '--to=1800000000001-bbbbbb'], session: 'ses_root' }])
  answer = { ok: false, out: 'wb: wb chat reply: no message x to Claude in the thread' }
  expect((await failure(tool.execute({ text: 'x', to: ['x'] }, { sessionID: 'ses_root' }))).includes('no message x')).toBe(true)
  expect((await failure(tool.execute({ text: ' ' }, { sessionID: 'ses_root' }))).includes('empty')).toBe(true)
})

test('setup adds the tool and the hooks, and starts no poll or feed until a session listens', async () => {
  const added: string[] = [], hooks: string[] = []
  let subscribed = 0
  const stop = await chat({
    options: { wb: '/nonexistent/bin/wb' },
    rpc: { register: async () => ({ dispose: async () => {} }) },
    session: { get: async () => ({ id: 'x' }), prompt: async () => ({}), hook: async (name: string) => { hooks.push(name); return {} } },
    tool: {
      transform: async (cb: (e: unknown) => void) => cb({ namespace: () => {}, add: (t: { name: string }) => added.push(t.name) }),
      hook: async (name: string) => { hooks.push(name); return {} },
    },
    event: { subscribe: () => { subscribed++; return (async function* () {})() } },
  } as never)
  try {
    expect(added).toEqual(['chat_reply'])
    expect(hooks).toEqual(['prompt', 'execute.before'])
    expect(subscribed).toBe(0)
    expect(((globalThis as unknown as Record<string, { timer?: unknown } | undefined>).__workbenchChatLane)?.timer).toBeUndefined()
  } finally {
    await stop?.()
  }
  expect((globalThis as Record<string, unknown>).__workbenchChatLane).toBeUndefined()
})
