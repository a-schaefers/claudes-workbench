import { expect, test } from 'bun:test'
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { codingGate, gateOf } from '../../mod/hooks/core.ts'
import {
  ANSWER, Asker, CODING_SAID, Consent, Gates, NOT_CHANGED, NOT_SENT, NO_FORM, consent, formFields, gatedChange,
  tools, writeApproval, type Answer, type Deps, type GateDeps, type Ran,
} from '../consent.ts'
import { consentTui } from '../consent-tui.ts'
import {
  APPROVAL_OWN, ONLY, ROUTES_EDIT, SEND_VIA_TOOL, VIA_TOOL, localOnlyPatchRefusal, localOnlyRefusal, places,
  shellRefusal, type Places,
} from '../gates.ts'
import { simpleWords } from '../words.ts'

// The consent gates on OpenCode's path (#1314, 2026-11-25), under OpenCode's own Bun:
// `BUN_BE_BUN=1 opencode test opencode-plugin/test`. The kernel is the committed one, as the
// plugin runs it (gateOf, codingGate through mod/hooks/core.ts).

const scratch = () => mkdtempSync(join(tmpdir(), 'wb-consent-'))
const P = (home: string): Places => ({ home, routes: '/u/.config/workbench/routes.json', commands: '/u/.config/workbench/commands.json', user: '/u' })
const has = (text: string, part: string) => expect(text.includes(part)).toBe(true)
const words = (lines: string[]) => lines.map(simpleWords)

test('a simple command\'s words come with their quotes undone, and its redirects\' files apart', () => {
  expect(simpleWords(`wb route trivial 'deepseek/x' "--day-usd" 5`).words).toEqual(['wb', 'route', 'trivial', 'deepseek/x', '--day-usd', '5'])
  expect(simpleWords('echo hi > a.txt 2>&1 >>b').writes).toEqual(['a.txt', 'b'])
  expect(simpleWords('echo hi 2>&1').writes).toEqual([])
  expect(simpleWords('cat &> "$WB_HOME/route-ok.json"').writes).toEqual(['$WB_HOME/route-ok.json'])
  expect(simpleWords('echo a\\ b').words).toEqual(['echo', 'a b'])
  // a heredoc's body is data, never words (#1364)
  const h = simpleWords("cat > notes.md <<'EOF'\nwb route trivial x --api y\nEOF")
  expect(h.words).toEqual(['cat'])
  expect(h.writes).toEqual(['notes.md'])
  expect(simpleWords('echo "$HOME"').dynamic).toBe(true)
})

test('a gated shell command is denied with the tool that asks, a show or an ungated change is not', async () => {
  const gated: string[][] = []
  const dry = async (args: ReadonlyArray<string>) => { gated.push([...args]); return args.includes('deepseek/x') }
  const run = (line: string) => shellRefusal(words([line]), '/repo', P('/s'), gateOf, dry)
  expect(await run('wb route trivial deepseek/x --day-usd 5')).toBe(VIA_TOOL('workbench_route'))
  expect(await run('wb route significant --gpu qwen')).toBe('')                  // the dry run says ungated
  expect(await run('wb route')).toBe('')                                          // a show: no dry run
  expect(await run('wb route --json')).toBe('')
  expect(await run('wb route --dry-run --json trivial deepseek/x')).toBe('')
  expect(gated).toEqual([['trivial', 'deepseek/x', '--day-usd', '5'], ['significant', '--gpu', 'qwen']])
  expect(await run('wb start t --model deepseek/x')).toBe(VIA_TOOL('workbench_start'))
  expect(await run('wb settings set roles.trivial deepseek/x')).toBe(VIA_TOOL('workbench_settings_set'))
  expect(await run('wb patch send fix --title T --body-file b.md')).toBe(SEND_VIA_TOOL)
  // a mention is no call (#1364): the kernel reads the command's own words
  expect(await run("gh issue create --title 'wb route trivial deepseek/x'")).toBe('')
  expect(await run('git commit -m "wb patch send it"')).toBe('')
})

test('a shell write of an approval file or routes.json is denied, a cd followed, a mention never', async () => {
  const p = P('/s')
  const run = (lines: string[], dir = '/repo') => shellRefusal(words(lines), dir, p, gateOf, async () => false)
  expect(await run(['echo {} > /s/route-ok.json'])).toBe(APPROVAL_OWN('route-ok.json'))
  expect(await run(['echo {} > "$WB_HOME/send-ok.ses_abc.json"'])).toBe(APPROVAL_OWN('send-ok.ses_abc.json'))
  expect(await run(['tee ~/.config/workbench/routes.json'])).toBe(ROUTES_EDIT)
  expect(await run(['cd /s', 'cp x route-ok.ses_q.json'])).toBe(APPROVAL_OWN('route-ok.ses_q.json'))
  expect(await run(['cd "$X"', 'cp x route-ok.json'])).toBe('')                     // a cd only the shell resolves
  expect(await run(['echo route-ok.json > notes.txt'])).toBe('')
  expect(await run(['echo hi > /s/route-ok.json.bak'])).toBe('')
})

// A form answered by the test, through the same events a TUI and OpenCode's bus give
function harness(answer: Answer | 'late') {
  const emitted: [string, Record<string, unknown>][] = []
  let asker!: Asker
  asker = new Asker(async (name, data) => {
    emitted.push([name, data])
    if (name !== 'ask' || answer === 'none') return
    const formID = String(data.formID), sessionID = String(data.sessionID)
    void Promise.resolve().then(() => {
      asker.shown(formID)
      if (answer !== 'late') {
        asker.event({ type: 'form.replied', data: { id: 'frm_other', sessionID, answer: { [ANSWER]: 'yes' } } })
        asker.event({ type: answer === 'no' ? 'form.cancelled' : 'form.replied',
          data: { id: formID, sessionID, answer: { [ANSWER]: answer } } })
      }
    })
  }, 50)
  return { asker, emitted }
}

test('a form is asked by an id the server part picks and answered from OpenCode\'s bus', async () => {
  const yes = harness('yes')
  expect(await yes.asker.ask('ses_1', { title: 'Workbench', question: 'q?', yes: 'Allow', no: "Don't" })).toBe('yes')
  const [name, data] = yes.emitted[0]!
  expect(name).toBe('ask')
  expect(String(data.formID).startsWith('frm_')).toBe(true)                // Form.ID's check
  expect(data.question).toBe('q?')
  expect(await harness('no').asker.ask('ses_1', { title: 'W', question: 'q', yes: 'y', no: 'n' })).toBe('no')
  // no TUI showed it in time: nothing asked, a late form closed
  const none = harness('none')
  expect(await none.asker.ask('ses_1', { title: 'W', question: 'q', yes: 'y', no: 'n' })).toBe('none')
  expect(none.emitted.map(e => e[0])).toEqual(['ask', 'close'])
  // a reply of another session's form of the same id is never this one's
  const other = harness('late')
  const pending = other.asker.ask('ses_1', { title: 'W', question: 'q', yes: 'y', no: 'n' })
  await new Promise(r => setTimeout(() => r(undefined), 5))
  const formID = String(other.emitted[0]![1].formID)
  other.asker.event({ type: 'form.replied', data: { id: formID, sessionID: 'ses_2', answer: { [ANSWER]: 'yes' } } })
  other.asker.event({ type: 'form.replied', data: { id: formID, sessionID: 'ses_1', answer: { [ANSWER]: 'no' } } })
  expect(await pending).toBe('no')
  expect(formFields({ question: 'q', yes: 'Allow', no: "Don't" })[0]!.options.map(o => o.value)).toEqual(['yes', 'no'])
})

function deps(home: string, answer: Answer, dry: Ran, calls: string[][]): Deps {
  return {
    run: async (args, opts) => {
      calls.push([opts.session, ...args])
      return args.includes('--dry-run') ? dry : { code: 0, out: `ran ${args[0]}`, err: '' }
    },
    git: async () => '',
    asker: { ask: async () => answer },
    home: () => home,
    dir: async () => home,
    read: path => { try { return readFileSync(path, 'utf8') } catch { return undefined } },
    now: () => 1_000_000,
  }
}

const GATED: Ran = { code: 0, out: JSON.stringify({ change: 'trivial: GPU → API x', gate: true,
  approval: { argv: ['route', 'trivial', 'x'], change: 'trivial: GPU → API x' } }), err: '' }

test('the route tool asks on a gated change and writes the session\'s own approval before the call', async () => {
  const home = scratch()
  try {
    const calls: string[][] = []
    const route = tools(deps(home, 'yes', GATED, calls)).find(t => t.name === 'route')!
    expect(await route.execute({ args: ['trivial', 'x'] }, { sessionID: 'ses_a' })).toEqual({ content: 'ran route' })
    expect(calls).toEqual([['ses_a', 'route', '--dry-run', '--json', 'trivial', 'x'], ['ses_a', 'route', 'trivial', 'x']])
    // store.approval_name's file: never the mod's route-ok.json, which Claude Code's calls take
    expect(existsSync(join(home, 'route-ok.json'))).toBe(false)
    expect(JSON.parse(readFileSync(join(home, 'route-ok.ses_a.json'), 'utf8'))).toEqual(
      { argv: ['route', 'trivial', 'x'], change: 'trivial: GPU → API x', at: 1000 })
  } finally {
    rmSync(home, { recursive: true, force: true })
  }
})

test('the route tool refuses on a no, and fails closed with no TUI, writing no approval', async () => {
  const home = scratch()
  try {
    for (const [answer, why] of [['no', NOT_CHANGED], ['none', NO_FORM('not changed', 'wb route trivial x')]] as const) {
      const calls: string[][] = []
      const route = tools(deps(home, answer, GATED, calls)).find(t => t.name === 'route')!
      let said = ''
      await route.execute({ args: ['trivial', 'x'] }, { sessionID: 'ses_a' }).catch(e => { said = e.message })
      expect(said).toBe(why)
      expect(calls.length).toBe(1)                                     // the dry run alone
      expect(existsSync(join(home, 'route-ok.ses_a.json'))).toBe(false)
    }
    // an ungated change runs at once, unasked; a start without a model has no dry run
    const calls: string[][] = []
    const t = tools(deps(home, 'none', { code: 0, out: '{"change": "x", "gate": false}', err: '' }, calls))
    expect(await t.find(x => x.name === 'route')!.execute({ args: ['significant', '--gpu', 'q'] }, { sessionID: 's' })).toEqual({ content: 'ran route' })
    expect(await t.find(x => x.name === 'start')!.execute({ args: ['t', '--spec', 'f'] }, { sessionID: 's' })).toEqual({ content: 'ran start' })
    expect(calls.map(c => c[1])).toEqual(['route', 'route', 'start'])
    expect(gatedChange({ code: 3, out: GATED.out, err: '' })).toBe(null)   // wb refuses it: the call says why
  } finally {
    rmSync(home, { recursive: true, force: true })
  }
})

test('the typed start (#1335) asks on an API model with its typed argv, writes the approval, then starts with the spec on stdin', async () => {
  const home = scratch()
  try {
    const calls: string[][] = []
    const inputs: (string | undefined)[] = []
    const d = deps(home, 'yes', GATED, calls)
    const run = d.run
    const start = tools({ ...d, run: async (args, opts) => { inputs.push(opts.input); return run(args, opts) } }).find(t => t.name === 'start')!
    expect(await start.execute({ name: 't', spec: 'Do x.\n', role: 'trivial', test: '-x', model: 'deepseek/x' }, { sessionID: 'ses_a' }))
      .toEqual({ content: 'ran start' })
    const argv = ['t', '--spec', '-', '--role=trivial', '--test=-x', '--model=deepseek/x']
    expect(calls).toEqual([['ses_a', 'route', '--dry-run', '--json', '--start', ...argv], ['ses_a', 'start', ...argv]])
    expect(inputs).toEqual([undefined, 'Do x.\n'])                   // the dry run reads no stdin (wb's rule)
    expect(existsSync(join(home, 'route-ok.ses_a.json'))).toBe(true)
  } finally {
    rmSync(home, { recursive: true, force: true })
  }
})

test('the send tool shows the title and body, and writes what wb patch send checks on the yes', async () => {
  const home = scratch()
  try {
    const body = join(home, 'body.md')
    writeFileSync(body, 'line\n'.repeat(30))
    const asked: string[] = []
    const calls: string[][] = []
    const d = { ...deps(home, 'yes', GATED, calls), asker: { ask: async (_s: string, q: { question: string }) => { asked.push(q.question); return 'yes' as const } } }
    const send = tools(d).find(t => t.name === 'patch_send')!
    expect(await send.execute({ slug: 'fix', title: 'Fix it', body_file: body }, { sessionID: 'ses_b' })).toEqual({ content: 'ran patch' })
    has(asked[0]!, 'to GitHub (a-schaefers/workbench), publicly')
    has(asked[0]!, 'Title: Fix it')
    has(asked[0]!, '\n…\n\nSend it?')                                    // 20 lines of 30, then the cut
    expect(JSON.parse(readFileSync(join(home, 'send-ok.ses_b.json'), 'utf8'))).toEqual(
      { slug: 'fix', title: 'Fix it', body_file: body, body: 'line\n'.repeat(30), draft: false, anyway: false, at: 1000 })
    expect(calls).toEqual([['ses_b', 'patch', 'send', 'fix', '--title', 'Fix it', '--body-file', body]])
    const no = tools({ ...deps(home, 'no', GATED, []) }).find(t => t.name === 'patch_send')!
    let said = ''
    await no.execute({ slug: 'fix', title: 'Fix it', body_file: join(home, 'gone.md') }, { sessionID: 's' }).catch(e => { said = e.message })
    has(said, "can't read")
    await no.execute({ slug: 'fix', title: 'Fix it', body_file: body }, { sessionID: 's' }).catch(e => { said = e.message })
    expect(said).toBe(NOT_SENT)
  } finally {
    rmSync(home, { recursive: true, force: true })
  }
})

test('an approval is never written under a session id wb would not take', () => {
  let said = ''
  try { writeApproval('/nowhere', 'route-ok.json', '../x', {}) } catch (e) { said = (e as Error).message }
  has(said, 'no session id wb takes')
})

function gateDeps(home: string, answer: Answer, over: Partial<GateDeps> = {}): GateDeps & { allowed: string[] } {
  const allowed: string[] = []
  return {
    allowed,
    places: () => ({ ...P(home), routes: join(home, 'routes.json'), commands: join(home, 'commands.json') }),
    dir: async () => '/repo',
    read: path => { try { return readFileSync(path, 'utf8') } catch { return undefined } },
    write: (path, text) => writeFileSync(path, text),
    real: x => x,
    classify: gateOf,
    coding: codingGate,
    dry: async () => false,
    asker: { ask: async () => answer },
    allow: async task => { allowed.push(task); return true },
    now: () => 5000,
    ...over,
  }
}

const ev = (action: string, resources: string[]) => ({ sessionID: 'ses_c', action, resources, effect: 'ask' } as
  { sessionID: string; action: string; resources: string[]; effect: string; message?: string })

test('every agent\'s edit of routes.json or an approval file is denied with the words', async () => {
  const home = scratch()
  try {
    const g = new Gates(gateDeps(home, 'yes'))
    for (const [file, why] of [[join(home, 'routes.json'), ROUTES_EDIT], [join(home, 'route-ok.json'), APPROVAL_OWN('route-ok.json')],
                               [join(home, 'send-ok.ses_x.json'), APPROVAL_OWN('send-ok.ses_x.json')]] as const) {
      const e = ev('edit', [file])
      await g.evaluate(e)
      expect([e.effect, e.message]).toEqual(['deny', why])
    }
    const fine = ev('edit', ['src/a.ts'])
    await g.evaluate(fine)
    expect(fine.effect).toBe('ask')                                    // OpenCode's own rule stands
  } finally {
    rmSync(home, { recursive: true, force: true })
  }
})

test('the coding gate: decide passes, ask asks once with the form, only refuses, a one-task exception starts', async () => {
  const home = scratch()
  try {
    mkdirSync(join(home, 'tasks', 't1', 'worktree'), { recursive: true })
    writeFileSync(join(home, 'tasks', 't1', 'task.json'), '{}')
    const file = join(home, 'tasks', 't1', 'worktree', 'a.py')
    const mode = (m: string) => writeFileSync(join(home, 'coding.json'), JSON.stringify({ mode: m }))
    const run = async (d: GateDeps) => { const e = ev('edit', [file]); await new Gates(d).evaluate(e); return e }
    expect((await run(gateDeps(home, 'none'))).effect).toBe('ask')              // no coding.json: decide
    mode('only')
    expect((await run(gateDeps(home, 'yes'))).message).toBe(ONLY('t1'))
    mode('ask')
    const yes = gateDeps(home, 'yes')
    expect((await run(yes)).effect).toBe('ask')
    expect(yes.allowed).toEqual(['t1'])                                         // wb coding allow, --said
    has(CODING_SAID, 'workbench form')
    expect((await run(gateDeps(home, 'no'))).effect).toBe('deny')
    expect((await run(gateDeps(home, 'none'))).effect).toBe('deny')             // no TUI: fail closed
    // two edits at once ask one form
    let asks = 0
    const once = gateDeps(home, 'yes', { asker: { ask: async () => { asks++; await new Promise(r => setTimeout(() => r(undefined), 5)); return 'yes' } } })
    const g = new Gates(once)
    await Promise.all([g.evaluate(ev('edit', [file])), g.evaluate(ev('edit', [file]))])
    expect(asks).toBe(1)
    // a one-task exception's first edit stamps it started
    writeFileSync(join(home, 'tasks', 't1', 'coding.json'), JSON.stringify({ allowed: [{ edit: 'x', when: 'tests fail' }] }))
    expect((await run(gateDeps(home, 'none'))).effect).toBe('ask')
    expect(JSON.parse(readFileSync(join(home, 'tasks', 't1', 'coding.json'), 'utf8')).allowed[0].started).toBe(5)
    expect((await run(gateDeps(home, 'none'))).effect).toBe('ask')              // under way: passes, unasked
    // without the kernel beside the plugin, a closed gate stays closed
    expect((await run(gateDeps(home, 'yes', { coding: undefined }))).effect).toBe('deny')
  } finally {
    rmSync(home, { recursive: true, force: true })
  }
})

test('a shell call through the permission hook: OpenCode\'s scan, the dry run as the session', async () => {
  const home = scratch()
  try {
    const seen: string[] = []
    const g = new Gates(gateDeps(home, 'yes', { dry: async (args, _cwd, session) => { seen.push(session); return true } }))
    const e = ev('shell', ['cd /tmp', 'wb route trivial x'])
    await g.evaluate(e)
    expect([e.effect, e.message]).toEqual(['deny', VIA_TOOL('workbench_route')])
    expect(seen).toEqual(['ses_c'])
    const opaque = { ...ev('shell', ['wb route trivial "$(cat x)"']), metadata: { reason: 'command-substitution' } }
    await g.evaluate(opaque)
    expect(opaque.effect).toBe('ask')                                  // wb's own refusal guards it
  } finally {
    rmSync(home, { recursive: true, force: true })
  }
})

test('an edit of commands.json that lifts a repo\'s local_only is refused before it runs', async () => {
  const home = scratch()
  try {
    const conf = join(home, 'commands.json')
    writeFileSync(conf, JSON.stringify({ repos: { '/r': { local_only: true } } }, null, 2))
    const g = new Gates(gateDeps(home, 'yes'))
    let said = ''
    await g.before({ tool: 'edit', sessionID: 's', input: { path: conf, oldString: '"local_only": true', newString: '"local_only": false' } })
      .catch(e => { said = e.message })
    has(said, 'drops "local_only": true for /r')
    said = ''
    await g.before({ tool: 'write', sessionID: 's', input: { path: conf, content: '{"repos": {}}' } }).catch(e => { said = e.message })
    has(said, 'drops')
    said = ''
    await g.before({ tool: 'patch', sessionID: 's', input: { patchText: `*** Begin Patch\n*** Update File: ${conf}\n@@\n-    "local_only": true\n+    "local_only": false\n*** End Patch` } })
      .catch(e => { said = e.message })
    has(said, 'not changed')
    await g.before({ tool: 'edit', sessionID: 's', input: { path: conf, oldString: '"/r": {', newString: '"/r": {"x": 1,' } })   // kept: no throw
    expect(localOnlyRefusal('{"repos": {}}', '{"repos": {"/x": {"local_only": true}}}')).toBe('')
    expect(localOnlyPatchRefusal('', '"local_only": true')).toBe('')
  } finally {
    rmSync(home, { recursive: true, force: true })
  }
})

test('the TUI makes the form with the server\'s id, says shown on a conflict too, and closes one late', async () => {
  const made: Record<string, unknown>[] = [], shown: string[] = [], cancelled: string[] = []
  const handlers: Record<string, (e: { data: Record<string, unknown> }) => unknown> = {}
  let fail = false
  const ctx = {
    client: {
      rpc: (def: { id: string }) => {
        expect(def.id).toBe(Consent.id)
        return { shown: async (i: { formID: string }) => { shown.push(i.formID) },
          events: { on: (name: string, h: (e: { data: Record<string, unknown> }) => unknown) => { handlers[name] = h; return () => {} } } }
      },
      session: { form: {
        create: async (i: Record<string, unknown>) => { if (fail) throw new Error('409 Conflict'); made.push(i) },
        cancel: async (i: { formID: string }) => { cancelled.push(i.formID) },
      } },
    },
  }
  expect(typeof consentTui(ctx)).toBe('function')
  await handlers.ask!({ data: { sessionID: 's', formID: 'frm_1', title: 'Workbench', question: 'q', yes: 'Allow', no: "Don't" } })
  expect(made).toEqual([{ sessionID: 's', id: 'frm_1', title: 'Workbench', fields: formFields({ question: 'q', yes: 'Allow', no: "Don't" }), metadata: { kind: 'workbench.consent' } }])
  fail = true
  await handlers.ask!({ data: { sessionID: 's', formID: 'frm_2', title: 'W', question: 'q', yes: 'y', no: 'n' } })
  expect(shown).toEqual(['frm_1', 'frm_2'])                           // another TUI made it: one form
  await handlers.close!({ data: { sessionID: 's', formID: 'frm_3', message: 'm' } })
  expect(cancelled).toEqual(['frm_3'])
  expect(consentTui({})).toBeUndefined()
})

test('the server part wires the tools, the RPC and both hooks, and nothing without OpenCode\'s domains', async () => {
  const registered: string[] = []
  const hooks: string[] = []
  const ctx = {
    options: {},
    rpc: { register: async (def: { id: string }) => { registered.push(def.id); return { dispose: async () => {}, events: { emit: async () => {} } } } },
    event: { subscribe: () => ({ [Symbol.asyncIterator]: () => ({ next: () => new Promise<never>(() => {}), return: async () => ({ done: true }) }) }) },
    session: { get: async () => ({}) },
    tool: {
      transform: async (cb: (e: { namespace: (n: unknown) => void; add: (t: { name: string }) => void }) => void) =>
        cb({ namespace: () => {}, add: t => registered.push(t.name) }),
      hook: async (name: string) => { hooks.push(name) },
    },
    permission: { hook: async (name: string) => { hooks.push(name) } },
  }
  const stop = await consent(ctx as never)
  expect(registered).toEqual([Consent.id, 'route', 'start', 'settings_set', 'patch_send'])
  expect(hooks).toEqual(['evaluate', 'execute.before'])
  await stop!()
  expect(await consent({ options: {} } as never)).toBeUndefined()
  expect(places({ HOME: '/h' }, '/c').routes).toBe('/h/.config/workbench/routes.json')
})
