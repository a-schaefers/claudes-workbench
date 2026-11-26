import { expect, test } from 'bun:test'
import { flowDefinitions, registerFlows } from '../flows.ts'
import { FLOW_NOTE, flowCommands } from '../../mod/hooks/flows.ts'
import type { CommandHost } from '../commands.ts'

// A flow as a slash command in OpenCode (#1295), against a fake wb and session API:
// `BUN_BE_BUN=1 opencode test opencode-plugin/test/flows.test.ts`.

const ROWS = JSON.stringify([
  { name: 'tests', where: 'yours', description: 'a test per file', command: 'tests-for', params: { src: { type: 'files' } } },
  { name: 'audit', where: 'shipped', description: 'finders, then a verifier', command: null, params: {} },
  { name: 'theirs', where: 'repo', description: "the repo's", command: 'theirs', params: {} },
])

function host() {
  const posted: Record<string, unknown>[] = []
  const added: string[] = []
  const h: CommandHost = {
    options: {},
    command: { transform: async (cb) => { cb({ add: (d) => { added.push(d.name) } }); return { dispose: async () => {} } } },
    session: { synthetic: async (input) => { posted.push(input as never) } },
  }
  return { h, posted, added }
}

test("each flow with a command, the user's or a shipped one, is a server command; a repo's never", async () => {
  const w = host()
  const ran: (readonly string[])[] = []
  const stop = await registerFlows(w.h, async (args) => { ran.push(args); return { ok: true, out: ROWS } })
  expect(ran).toEqual([['run', 'flows', '--json']])
  expect(w.added).toEqual(['tests-for'])
  expect(typeof stop).toBe("function")
  // no flow with one, or wb failing: nothing registered
  const none = host()
  expect(await registerFlows(none.h, async () => ({ ok: false, out: 'wb: no' }))).toBeUndefined()
  expect(none.added).toEqual([])
})

test("running it posts the driver the turn that starts the flow, its line as typed, the Claude Code mod's words", async () => {
  const w = host()
  const [def] = flowDefinitions(w.h, ROWS)
  await def!.execute({ sessionID: 's1', prompt: { text: "src/*.py it's" } })
  expect(w.posted).toEqual([{ sessionID: 's1', text: FLOW_NOTE(flowCommands(ROWS)[0]!, "src/*.py it's"),
    description: 'Workbench: flow tests', metadata: { source: 'workbench.command' }, resume: true }])
  expect(String(w.posted[0]!.text).includes("wb run start <a run name of yours> --flow tests --args 'src/*.py it'\\''s'")).toBe(true)
})
