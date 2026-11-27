// The web chat end to end, in real browsers (WebKit as an iPhone, Chromium as a Pixel and at a
// desktop's 1280×800): the page, the real `wb chat serve`, and `wb chat` on the Claude side.
// Run: node tests/e2e_chat.mts (Node runs it as it is: its types are stripped, #1323)
// Needs Playwright: PLAYWRIGHT=/path/to/node_modules/playwright (it isn't a workbench dependency).
import { spawn, execFileSync } from 'node:child_process'
import type { ChildProcess } from 'node:child_process'
import { mkdtempSync, mkdirSync, writeFileSync, appendFileSync, copyFileSync, readFileSync, readdirSync, existsSync, rmSync, statSync, utimesSync, chmodSync } from 'node:fs'
import { createRequire } from 'node:module'
import { createServer, connect as dial } from 'node:net'
import type { Socket } from 'node:net'
import { runInNewContext } from 'node:vm'
import { tmpdir } from 'node:os'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import * as statsView from './e2e_stats.mts'
import * as settingsView from './e2e_settings.mts'

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')
// untyped: Playwright isn't a dependency, and its types come with it
const pw: any = createRequire(import.meta.url)(process.env.PLAYWRIGHT || 'playwright')
const home = mkdtempSync(join(tmpdir(), 'wbchat-'))
// The user's roles, OpenCode config and OpenCode stay out too (#1286): the Settings rows run `wb
// route --json` and `wb models --json`, and the user's catalog, read in the background into this
// WB_HOME's hosted.json, renamed the Stats check's providers on the next engine
const env = { ...process.env, WB_HOME: join(home, 'state'), WB_CHAT_TOKEN: join(home, 'token'),
  WB_CHAT_CONFIG: join(home, 'chat.json'), WB_SAVINGS: join(home, 'savings.json'), WB_AUDIO: join(home, 'audio.json'),
  WB_ROUTES: join(home, 'routes.json'), WB_OPENCODE_CONFIG: join(home, 'opencode.json'), WB_OPENCODE_BIN: 'false',
  // a GPU listener's turn (#1203): the lease here, never a shared GPU's, and no model of the
  // user's unloaded for it (WB_FAKE_MODELS: free_gpu does nothing; no config of theirs read)
  WB_GPU_DIR: join(home, 'state'), WB_FAKE_MODELS: '1',
  WB_MODELS: join(home, 'wb-models.json') }  // not the user's
// a system Chromium when there is one (CHROMIUM names another), else Playwright's own
const CHROMIUM = process.env.CHROMIUM || (existsSync('/usr/bin/chromium') ? '/usr/bin/chromium' : '')
const PORT = Number(process.env.WB_E2E_PORT || 13371)
const wb = (...args: string[]) => execFileSync(join(ROOT, 'bin', 'wb'), args, { env, encoding: 'utf8' })
const sleep = (ms: number) => new Promise(r => setTimeout(r, ms))
// a phone is one of Playwright's devices; the desktop, Chromium at 1280×800 (#1284)
const DESKTOP = 'Desktop 1280×800'
const devices: Record<string, object> = { [DESKTOP]: { viewport: { width: 1280, height: 800 } } }
const ctxFor = (device?: string) => device ? (devices[device] || pw.devices[device]) : {}

let server!: ChildProcess  // started by run() before any use
function startServer() {
  server = spawn(join(ROOT, 'bin', 'wb'), ['chat', 'serve', '--port', String(PORT), '--bind', '127.0.0.1'],
    { env, stdio: 'ignore' })
}
async function stopServer() {
  server.kill('SIGTERM')
  await new Promise(r => server.once('exit', r))
}
const messages = () => existsSync(join(home, 'state/chat/messages.jsonl'))
  ? readFileSync(join(home, 'state/chat/messages.jsonl'), 'utf8').trim().split('\n').filter(Boolean).map(l => JSON.parse(l)) : []
const listen = (busy: boolean) => writeFileSync(join(home, 'state/chat/listener.json'),
  JSON.stringify({ session: 's1', cwd: '/home/u/project', pid: process.pid, busy }))

let failed = 0
async function check(name: string, fn: () => Promise<unknown>) {
  try { await fn(); console.log('ok   ' + name) }
  catch (e) { failed++; console.log('FAIL ' + name + '\n     ' + String((e as Error).message || e).split('\n')[0]) }
}
// Every wait is on its condition, never a fixed sleep, and generous: CI's runners are slow at
// random, and a different check failed each run (#1223). A sleep below is a claim that something
// does not happen, or a span the page's own clock must pass.
const until = async (fn: () => unknown, ms = 20000) => {
  const end = Date.now() + ms
  for (;;) {
    try { if (await fn()) return } catch {}
    if (Date.now() > end) throw new Error('timed out: ' + fn.toString().slice(0, 120))
    await sleep(100)
  }
}

async function run(engine: string, device?: string) {
  console.log(`\n== ${engine}${device ? ' as ' + device : ''}`)
  const desktop = device === DESKTOP
  wb('chat', 'log')  // makes the chat directory
  wb('chat', 'url')  // and the key
  writeFileSync(join(home, 'state/chat/messages.jsonl'), '')
  writeFileSync(join(home, 'state/chat/receipts.jsonl'), '')
  writeFileSync(join(home, 'state/chat/listener.json'), '{}')
  rmSync(join(home, 'state/chat/open.json'), { force: true })  // the last engine's unread, rebuilt from the files
  writeFileSync(join(home, 'state/results.jsonl'), '')
  const token = readFileSync(env.WB_CHAT_TOKEN, 'utf8').trim()
  startServer()
  const base = `http://localhost:${PORT}/`
  await until(async () => (await fetch(base)).status > 0)  // listening: 800 ms was a guess (#1223)
  const browser = await pw[engine].launch(engine === 'chromium' && CHROMIUM ? { executablePath: CHROMIUM } : {})
  const ctx = await browser.newContext(ctxFor(device))
  const page = await ctx.newPage()
  // the server's thread held back 1.5 s while this tab's sessionStorage says `e2e-hold`, by the
  // page's own fetch: Playwright's routes saw these requests but no longer held them once the
  // service worker was registered, in Chromium as in WebKit (#1223)
  await page.addInitScript(() => {
    const real = window.fetch
    window.fetch = (u, o) => {
      let hold = false
      try { hold = !!sessionStorage.getItem('e2e-hold') && /\/api\/(state|stream)$/.test(String(u)) } catch {}
      return hold ? new Promise(r => setTimeout(r, 1500)).then(() => real(u, o)) : real(u, o)
    }
  })
  const errors: string[] = []
  // WebKit reports a same-origin fetch that a reload cut off as "Fetch API cannot load … due to
  // access control checks", a console message Playwright hands over as a page error (a pair's
  // images asked for as the page unloaded, #1223). Every fetch here is same-origin, so no access
  // control applies: that wording is only the cut-off load, never the page's script
  const onError = (e: unknown) => { if (!/^(\w*Error: )?Fetch API cannot load .+ due to access control checks\.?$/.test(String(e))) errors.push(String(e)) }
  page.on('pageerror', onError)

  await check('opens with the key, keeps it, drops it from the address', async () => {
    await page.goto(base + '#token=' + token)
    await until(async () => (await page.textContent('#presence-text')).includes('no session listening'))
    if (page.url().includes('token')) throw new Error('token still in the URL: ' + page.url())
    // #1339: no driver known yet, so the box and the story name both, never "your driver"
    await until(async () => (await page.getAttribute('#input', 'placeholder')) === 'Message Claude or OpenCode'
      && (await page.textContent('#intro p')).includes('Message Claude or OpenCode from anywhere'))
  })
  await check('shows a listening session at once', async () => {
    listen(false)
    await until(async () => (await page.textContent('#presence-text')).includes('listening in ~/project'))
    await until(async () => (await page.getAttribute('#input', 'placeholder')) === 'Message Claude')  // the listener's (#1339)
  })
  // #1341: the chat says what it is for, in the empty state, and over a thread as a hint until
  // dismissed, naming the driver that listens (#1355: the story's `purpose`, story.ts)
  const PURPOSE = 'Message Claude from anywhere: most work can run from here.'
  await check('the empty state says what the chat is for, and no hint repeats it', async () => {
    if (!(await page.textContent('#intro p')).replace(/\s+/g, ' ').includes(PURPOSE)) throw new Error(await page.textContent('#intro p'))
    if (await page.locator('#hint').isVisible()) throw new Error('the hint shows over the empty state')
    // #1339: the starter chips, the first tap on the simple path, are 44 px to tap
    const chips = await page.$$eval('#intro .chip', (x: Element[]) => x.map(c => Math.round(c.getBoundingClientRect().height)))
    if (!chips.length || chips.some(h => h < 44)) throw new Error('the chips: ' + chips)
  })
  await check('a sent message lands once, with its tick', async () => {
    await page.fill('#input', 'hello from the phone')
    await page.click('#send')
    await until(() => messages().length === 1)
    await until(async () => (await page.locator('.row.user .meta').last().textContent()).includes('Delivered'))
    if (await page.inputValue('#input')) throw new Error('the box was not cleared')
    // #1339: a row says whose it is and when; its tick is drawn, not read out
    const named = await page.evaluate(() => { const r = [...document.querySelectorAll('.row.user')].at(-1)!
      return { role: r.getAttribute('role'), label: r.getAttribute('aria-label'), tick: r.querySelector('.tick')!.getAttribute('aria-hidden') } })
    if (named.role !== 'group' || !/^You, \d/.test(named.label || '') || named.tick !== 'true') throw new Error(JSON.stringify(named))
    // #1339: what the chat is for is known once a message is sent: the hint goes, for good
    if (await page.locator('#hint').isVisible() || await page.evaluate(() => localStorage.getItem('wb-chat-hint')) !== 'true')
      throw new Error('the hint stayed after the first message sent')
  })
  await check('over a thread the hint shows once, hides while the state line says the same, dismisses, and stays dismissed after a reload', async () => {
    await page.evaluate(() => localStorage.removeItem('wb-chat-hint'))  // this device never sent: as another one's thread
    await page.reload()
    await until(async () => await page.locator('#hint').isVisible())
    // no session listening: the state line says how to start one, and the hint, the same, hides (#1339)
    writeFileSync(join(home, 'state/chat/listener.json'), '{}')
    await until(async () => (await page.locator('#driver-state').isVisible()) && await page.locator('#hint').isHidden())
    listen(false)
    await until(async () => (await page.locator('#driver-state').isHidden()) && await page.locator('#hint').isVisible())
    const said = (await page.textContent('#hint-text')).replace(/\s+/g, ' ')
    for (const want of [PURPOSE, 'waits in its terminal', 'permissions granted', 'settings live here'])
      if (!said.includes(want)) throw new Error(said)
    const x = await page.evaluate(() => document.getElementById('hint-x')!.getBoundingClientRect().toJSON())
    if (x.width < 44 || x.height < 44) throw new Error('the hint\'s ✕: ' + JSON.stringify(x))  // #1339
    await page.click('#hint-x')
    await until(async () => await page.locator('#hint').isHidden())
    if (await page.evaluate(() => localStorage.getItem('wb-chat-hint')) !== 'true') throw new Error('the dismissal was not kept')
    await page.reload()
    await until(async () => (await page.locator('.row').count()) >= 1)
    await sleep(500)  // a claim that it does not come back
    if (await page.locator('#hint').isVisible()) throw new Error('the hint came back after a reload')
  })
  await check('where storage throws, the hint still shows and dismisses, and the page still works', async () => {
    const p = await ctx.newPage()
    const errs: string[] = []
    p.on('pageerror', (e: unknown) => errs.push(String(e)))
    await p.addInitScript(() => {
      for (const f of ['getItem', 'setItem', 'removeItem', 'key']) Storage.prototype[f] = () => { throw new DOMException('denied', 'SecurityError') }
    })
    try {
      await p.goto(base + '#token=' + token)
      await until(async () => (await p.locator('.row').count()) >= 1)
      await until(async () => await p.locator('#hint').isVisible())
      await p.click('#hint-x')
      await until(async () => await p.locator('#hint').isHidden())
      if (errs.length) throw new Error(errs.join(' | '))
    } finally { await p.close() }
  })
  await check('a receipt shows as read', async () => {
    wb('chat', 'ack', messages()[0].id, '--by', 's1')
    await until(async () => (await page.locator('.row.user .meta').last().textContent()).includes('Read by Claude'))
  })
  await check('working shows while the turn runs, then Claude\'s reply is revealed in full', async () => {
    listen(true)
    await until(async () => (await page.locator('.thread .working').count()) === 1)
    wb('chat', 'reply', '--text', 'On it.\n\n- **step 2** in review\n- `make test` passes')
    listen(false)
    await until(async () => (await page.locator('.row.claude .bubble').last().textContent()).includes('make test passes'))
    await until(async () => (await page.locator('.row.claude .bubble strong').count()) === 1)
    if (await page.locator('.thread .working').count()) throw new Error('still shows working')
    const named = await page.evaluate(() => { const r = [...document.querySelectorAll('.row.claude')].at(-1)!
      return { label: r.getAttribute('aria-label'), copy: r.querySelector('.meta button')!.getAttribute('aria-label') } })
    if (!/^Claude, \d/.test(named.label || '') || named.copy !== 'Copy this reply') throw new Error(JSON.stringify(named))  // #1339
  })
  await check("Claude's reply copies over plain http at a LAN address, where navigator.clipboard is missing", async () => {
    // #745 (the 2026-11-21 audit, finding 104): navigator.clipboard exists only in a secure
    // context, so over http://100.x.y.z:1337 every tap said "copy failed"
    const p = await ctx.newPage()
    await p.addInitScript(() => {
      Object.defineProperty(Navigator.prototype, 'clipboard', { get: () => undefined, configurable: true })
      Object.defineProperty(window, 'isSecureContext', { get: () => false, configurable: true })
      const real = document.execCommand.bind(document)
      window.copied = []
      document.execCommand = (cmd, ...rest) => {
        const el = document.activeElement
        if (cmd === 'copy') window.copied.push(el && (el as HTMLInputElement).value !== undefined ? (el as HTMLInputElement).value.slice((el as HTMLInputElement).selectionStart!, (el as HTMLInputElement).selectionEnd!) : String(getSelection()))
        return real(cmd, ...rest)
      }
      // what the button said, once it changed: a state event redraws the row (and the button) at
      // once. Read 50 ms after the tap, a slow runner's copy had not answered yet (#1223)
      window.said = []
      addEventListener('click', e => {
        const t = e.target as HTMLElement, was = t.textContent
        if (t.tagName !== 'BUTTON') return
        const seen = new MutationObserver(() => { if (t.textContent !== was) { seen.disconnect(); window.said.push(t.textContent) } })
        seen.observe(t, { childList: true, characterData: true, subtree: true })
      }, true)
    })
    try {
      await p.goto(base)
      const row = p.locator('.row.claude').last()
      await until(async () => (await row.locator('.bubble').textContent()).includes('make test passes'))
      const button = row.locator('.meta button', { hasText: 'copy' })
      await button.click()
      await until(async () => (await p.evaluate(() => window.said)).length === 1)
      const said = await p.evaluate(() => window.said)
      if (said[0] !== 'copied') throw new Error('the button said ' + said[0])
      const got = await p.evaluate(() => window.copied)
      if (got.length !== 1 || !got[0].includes('`make test` passes')) throw new Error('copied ' + JSON.stringify(got))
      if (await p.locator('textarea:not(#input)').count()) throw new Error('the copy textarea was left behind')
    } finally { await p.close() }
  })
  await check('a message sent while the server is down lands exactly once when it is back', async () => {
    await stopServer()
    await page.fill('#input', 'sent while you were down')
    await page.click('#send')
    await until(async () => (await page.locator('.row.user.pending').count()) === 1)
    await sleep(1500)
    startServer()
    await until(() => messages().filter(m => m.text === 'sent while you were down').length === 1, 20000)
    await until(async () => (await page.locator('.row.user.pending').count()) === 0, 20000)
    await sleep(1500)
    const n = messages().filter(m => m.text === 'sent while you were down').length
    if (n !== 1) throw new Error(`landed ${n} times`)
  })
  await check('the stream comes back after the outage: a new reply arrives', async () => {
    wb('chat', 'reply', '--text', 'Back again.')
    await until(async () => (await page.locator('.row.claude .bubble').last().textContent()).includes('Back again.'), 20000)
    await until(async () => await page.locator('#net').isHidden())
  })
  await check('while the computer can\'t be reached the header says so, never listening, and says listening again once it is back (#1415)', async () => {
    // #1415 (2026-11-25): the banner said "Can't reach the computer", the header under it "Claude ·
    // listening in …" with its green dot, the whole time
    const said = () => page.evaluate(() => ({ text: document.getElementById('presence-text')!.textContent, cls: document.getElementById('presence')!.className }))
    await stopServer()
    try {
      await until(async () => { const s = await said(); return s.text === 'computer unreachable · messages wait' && !/listening|working/.test(s.cls) })
        .catch(async () => { throw new Error(JSON.stringify(await said())) })
    } finally { startServer() }
    await until(async () => (await said()).text.includes('listening in ~/project'), 40000)
    await until(async () => await page.locator('#net').isHidden())
  })
  await check('a reload shows the same thread, nothing doubled, still keyed', async () => {
    await page.reload()
    await until(async () => (await page.locator('.row').count()) === 4)
    const texts = await page.locator('.row .bubble').allTextContents()
    if (new Set(texts).size !== texts.length) throw new Error('duplicates: ' + texts.join(' | '))
  })
  await check('the app installs only over https: no manifest over localhost, a keyed one over https', async () => {
    // a localhost install beside the tailnet's left two apps of one name (2026-11-18)
    if (await page.locator('link[rel=manifest]').count()) throw new Error('a manifest over http://localhost')
    const sec = await browser.newContext(ctxFor(device))
    await sec.route('https://chat.test/**', async (r: any) => {  // an https origin, served by the real server
      const url = r.request().url().replace('https://chat.test/', base)
      if (url.includes('/api/')) return r.abort()
      await r.fulfill({ response: await r.fetch({ url }) })
    })
    const p = await sec.newPage()
    await p.goto('https://chat.test/#token=' + token)
    await until(async () => ((await p.getAttribute('link[rel=manifest]', 'href')) || '').endsWith('?k=' + token))
    await sec.unrouteAll({ behavior: 'ignoreErrors' }); await sec.close()
  })
  // Playwright's WebKit doesn't route a page's requests once a service worker is registered
  if (engine === 'chromium') await check('a reload whose state is slow to come shows the last thread, connecting', async () => {
    await ctx.route('**/api/**', (r: any) => r.abort())
    await page.reload()
    await until(async () => (await page.locator('.row').count()) === 4)
    if (!(await page.textContent('#presence-text')).includes('connecting')) throw new Error(await page.textContent('#presence-text'))
    await ctx.unroute('**/api/**')
    await until(async () => (await page.textContent('#presence-text')).includes('listening'), 40000)
  })
  await check('an update that changes no row re-adds none of their lines, and a new reply is said once, whole, outside the thread (#1408)', async () => {
    // #1408 (2026-11-25): the thread was the live region, and each update re-added every row's
    // time and `copy`, and a reply's reveal each growing fragment, for a screen reader to read out
    await page.evaluate(() => {
      window._metas = []
      new MutationObserver(ms => { for (const m of ms) for (const n of m.addedNodes) if (n.nodeType === 1 && (n as Element).closest('.meta')) window._metas.push(n.textContent) })
        .observe(document.getElementById('thread')!, { childList: true, subtree: true })
    })
    if (await page.evaluate(() => !!document.getElementById('thread')!.closest('[aria-live]'))) throw new Error('the thread is in a live region')
    listen(true)                                           // the presence alone changes
    await until(async () => (await page.textContent('#presence-text')).includes('working in'))
    listen(false)
    await until(async () => (await page.textContent('#presence-text')).includes('listening in'))
    const metas = await page.evaluate(() => window._metas)
    if (metas.length) throw new Error('lines made again: ' + JSON.stringify(metas))
    wb('chat', 'reply', '--text', 'Said once.\n\n- **whole** at the end')
    await until(async () => (await page.locator('.row.claude .bubble strong').last().textContent()) === 'whole')
    const said = await page.evaluate(() => [...document.querySelectorAll('#say p')].map(p => p.textContent).filter(t => t.includes('Said')))
    if (JSON.stringify(said) !== JSON.stringify(['Said once.\n\n- whole at the end'])) throw new Error(JSON.stringify(said))
  })
  await check("a message's copy button keeps the focus through an update (#1409)", async () => {
    // #1409 (2026-11-25): each update made every row's line anew, and the focus on its button fell to the page
    await page.locator('.row.claude').last().locator('.meta button').focus()
    const on = () => page.evaluate(() => { const a = document.activeElement; return !!(a && a.textContent === 'copy' && a.closest('.row.claude')) })
    if (!(await on())) throw new Error('not focused to begin with')
    listen(true)
    await until(async () => (await page.textContent('#presence-text')).includes('working in'))
    listen(false)
    await until(async () => (await page.textContent('#presence-text')).includes('listening in'))
    if (!(await on())) throw new Error('the focus went to ' + await page.evaluate(() => document.activeElement && document.activeElement.tagName))
    await page.evaluate(() => (document.activeElement as HTMLElement).blur())
  })
  await check('the message box, focused, shows a ring at 3:1 or more against the page (#1411)', async () => {
    // #1411 (2026-11-25): outline none, and the border's tint alone, 1.6:1 against the unfocused one
    await page.focus('#input')
    const got = await page.locator('#input').evaluate(settingsView.ringContrast, true)
    await page.evaluate(() => document.getElementById('input')!.blur())
    if (!got.visible || got.ratio < 3) throw new Error(JSON.stringify(got))
  })
  await check("a reply's lead-in line, its bullets with no blank line between and a line after them show as a paragraph, a list and a paragraph (#1416)", async () => {
    // #1416 (2026-11-25): a block was a list only when every line was a bullet, so Claude's usual
    // lead-in and bullets showed as one paragraph of '- ' lines
    wb('chat', 'reply', '--text', 'Two tasks done, one failed:\n- **config-loader** waits\n- **parser-fix** failed\nBoth wait for you.')
    const bubble = page.locator('.row.claude .bubble').last()
    await until(async () => (await bubble.textContent()).includes('Both wait for you.') && !(await bubble.getAttribute('class')).includes('flowing'))
    const got = await bubble.evaluate((b: HTMLElement) => ({ kids: [...b.children].map(c => c.tagName).join(), items: [...b.querySelectorAll('li')].map(l => l.textContent),
      dashes: [...b.querySelectorAll('p')].some(p => /(^|\n)- /.test(p.innerText)) }))
    if (got.kids !== 'P,UL,P' || got.items.join('|') !== 'config-loader waits|parser-fix failed' || got.dashes) throw new Error(JSON.stringify(got))
  })
  // the shell's views (#1284): a tap on the nav (the rail on a desktop), then the body shows it
  const goTo = async (name: string) => {
    await page.click(`#tabbar .tab[data-view="${name}"]`)
    await until(() => page.evaluate((n: string) => document.body.dataset.view === n, name))
  }
  await check('Settings holds what the gear\'s sheet did, as cards, and no way to answer Claude\'s prompts from here (#1350)', async () => {
    await goTo('stats')                                    // What it saved went to Stats (#1284)
    if (!(await page.textContent('#saved')).includes('Nothing to compare yet')) throw new Error('a saving shown with no task')
    await goTo('settings')
    await until(async () => await page.locator('#settings').isVisible())
    if (!(await page.textContent('#settings')).includes('second lane')) throw new Error('no word on what this chat is')
    const heads = await page.$$eval('#settings .card h3', (h: Element[]) => h.map(x => x.textContent).join(' | '))
    if (heads !== 'How the chat works | While away | Notifications | Install the app | This chat | This device') throw new Error(heads)
    if (await page.locator('[data-away]').count()) throw new Error('"Let Claude go ahead" is back')
    const r = await fetch(base + 'api/prefs', { method: 'POST', headers: { Authorization: 'Bearer ' + token,
      'Content-Type': 'application/json' }, body: '{"away":"allow"}' })
    if (r.status !== 404) throw new Error(`/api/prefs answered ${r.status}`)
  })
  await check('the settings say you are away once you use this page after the terminal, and that prompts are answered in the terminal, a line here saying one waits (#959, #1350)', async () => {
    await goTo('settings')                                 // where the last check left it
    await until(async () => await page.locator('#settings').isVisible())
    const said = await page.textContent('#away-when')
    for (const want of ['use the chat on another device (open it or send a message here) after using the terminal',
                        'until you type or answer a prompt in the terminal again',
                        "Stats and Settings don't count, nor this page opened on the computer itself"])  // #1374
      if (!said.includes(want)) throw new Error(said)
    const prompts = await page.textContent('#away-prompts')
    for (const want of ['are answered in the terminal, never here', 'Run Claude with permissions granted to work from the chat.'])
      if (!prompts.includes(want)) throw new Error(prompts)
    // the three rules, the story's words for the driver listening (#1355)
    const how = (await page.textContent('#how')).replace(/\s+/g, ' ')
    for (const want of ['The terminal is where Claude lives', 'a prompt Claude raises is answered there, never in the chat',
                        'more so with Claude run with permissions granted', "Settings are the source of truth: Claude changes one only when you ask and say yes"])
      if (!how.includes(want)) throw new Error(how)
    await goTo('chat')
    await until(async () => await page.locator('#settings').isHidden())
  })
  await check('the page coming on screen in use tells the computer, and one hidden or unfocused does not (#959)', async () => {
    // #959: only a message sent here made the user away, so a phone picked up after the terminal
    // got none of the prompts. Using the page now counts; a background tab or one beside the terminal doesn't
    const at = () => existsSync(join(home, 'state/chat/page.json')) ? JSON.parse(readFileSync(join(home, 'state/chat/page.json'), 'utf8')).at : 0
    const show = (h: boolean) => page.evaluate((h: boolean) => {
      Object.defineProperty(document, 'hidden', { value: h, configurable: true })
      document.dispatchEvent(new Event('visibilitychange'))
    }, h)
    await until(() => at() > 0)                                  // opened: in use
    const sw = join(home, 'state/chat/switch.json')
    const swWas = existsSync(sw) ? readFileSync(sw, 'utf8') : JSON.stringify({ on: true, v: 2 })
    writeFileSync(sw, JSON.stringify({ on: true, v: 2 }))
    try {
      let was = at()
      await page.evaluate(() => { document._hasFocus = document.hasFocus; document.hasFocus = () => false })
      await show(true); await show(false)                        // on screen, but the terminal has the focus
      await page.evaluate(() => dispatchEvent(new Event('focus')))
      await sleep(1500)
      if (at() !== was) throw new Error('an unfocused page said it was in use')
      await page.evaluate(() => { document.hasFocus = document._hasFocus })
      await show(true); await show(false)                        // back on screen, in use
      await until(() => at() > was)
      // this page is the computer's own (its Host is localhost): in use, never away (#1374); a
      // phone's Host is another, the server's tests' (WebChat)
      const away = () => existsSync(join(home, 'state/chat/away.json')) ? JSON.parse(readFileSync(join(home, 'state/chat/away.json'), 'utf8')) : {}
      if (away().by === 'page') throw new Error('a localhost page made the user away: ' + JSON.stringify(away()))
      was = at()
      await show(true)
      await page.evaluate(() => { Object.defineProperty(document, 'hidden', { value: false, configurable: true }) })
      await page.evaluate(() => dispatchEvent(new Event('focus')))   // focused again
      await until(() => at() > was)
      was = at()
      await page.locator('.thread').click({ position: { x: 5, y: 5 } })  // a tap within 30 s of that: no more
      await sleep(1500)
      if (at() !== was) throw new Error('reported again within 30 s')
    } finally {
      // as the checks before left it, on by default (#1281): left off, every listener the checks
      // after it write listens to nobody, as the chat off says now (#1537, 2026-11-26)
      writeFileSync(sw, swWas)
      // the document's own hidden again: left at false, the reload's own visibilitychange read
      // as visible and dialled the stream from a page being unloaded (a WebKit page error)
      await page.evaluate(() => { delete (document as any).hidden; if (document._hasFocus) document.hasFocus = document._hasFocus })
    }
  })
  await check('a failed activity post is not tried again on a timer, only on the next use (the #959 review)', async () => {
    // the #959 review: a page left in front on a phone put down retried every 5 s until the line
    // came back, after the user had typed in the terminal, and made them away with no use at all
    // a dead line, by the page's own fetch: WebKit routes no requests once the worker is registered
    await page.evaluate(() => {
      window._fetch = window.fetch; window._tries = 0
      window.fetch = (u, o) => String(u).includes('/api/active')
        ? (window._tries++, Promise.reject(new TypeError('Load failed'))) : window._fetch(u, o)
    })
    const tries = () => page.evaluate(() => window._tries)
    try {
      await sleep(31_000)                                        // past the last report's 30 s
      await page.locator('.thread').click({ position: { x: 5, y: 5 } })
      await until(async () => await tries() === 1)
      await sleep(6500)                                          // the old retry came at 5 s
      if (await tries() !== 1) throw new Error(`tried ${await tries()} times with no use`)
      await page.locator('.thread').click({ position: { x: 5, y: 5 } })  // used again: at once
      await until(async () => await tries() === 2)
    } finally {
      await page.evaluate(() => { window.fetch = window._fetch })
    }
  })
  await check('a click in Stats or Settings, or a tap on the nav leaving the chat, does not tell the computer the page is in use; back on Chat does (#1374)', async () => {
    // #1374: the web app is the desk's admin too, and a click in Settings made the user at the
    // terminal away, their next plan or question refused into the chat
    await page.evaluate(() => {
      window._fetch = window.fetch; window._active = []
      window.fetch = (u, o) => {
        if (String(u).includes('/api/active')) { window._active.push(document.body.dataset.view); return Promise.reject(new TypeError('Load failed')) }
        return window._fetch(u, o)
      }
    })
    const posted = () => page.evaluate(() => window._active)
    try {
      // each post refused, so none holds back the next for 30 s (ACTIVE_MS): the last check's wasn't either
      await goTo('settings')                                     // a tap on the nav, from the chat
      await page.locator('#view-settings').click({ position: { x: 5, y: 5 } })
      await page.keyboard.press('Shift')
      await goTo('stats')
      await page.locator('#view-stats').click({ position: { x: 5, y: 5 } })
      await page.evaluate(() => dispatchEvent(new Event('focus')))
      await sleep(1500)                                          // a claim that none was made
      if ((await posted()).length) throw new Error('posted from ' + (await posted()).join())
      await goTo('chat')                                         // the chat shown: in use
      await until(async () => { const p = await posted(); return p.length && p.every((v: string) => v === 'chat') })
    } finally {
      await page.evaluate(() => { window.fetch = window._fetch })
    }
  })
  await check('a prompt waiting in the terminal is the state line at the top, Copy its one button, no thread row and no answer card, and its settling clears it (#959, #1350, #1355)', async () => {
    // the wait is the listener's claim's, as the host writes it (#1481: another session's notice never is)
    const waits = (w?: object) => writeFileSync(join(home, 'state/chat/listener.json'),
      JSON.stringify({ session: 's1', cwd: '/home/u/project', pid: process.pid, busy: false, ...(w ? { waiting: w } : {}) }))
    const id = wb('chat', 'notice', '--tool', 'Bash', '--text', 'rm -rf runs/*').trim()
    if (id.startsWith('skipped')) throw new Error(id)
    waits({ kind: 'permission', tool: 'Bash', since: Date.now() / 1000, what: 'rm -rf runs/*' })
    const line = page.locator('#driver-state')
    await until(async () => (await line.isVisible()) && (await line.textContent()).includes('Claude is waiting for you in the terminal: a permission for Bash.'))
    if ((await line.locator('.dstate-what').textContent()) !== 'rm -rf runs/*') throw new Error(await line.textContent())
    if (!(await line.locator('.dstate-grant').textContent()).includes('claude --permission-mode auto')) throw new Error(await line.textContent())
    if ((await line.locator('button').count()) !== 1 || (await line.locator('button').textContent()) !== 'Copy') throw new Error('a button besides Copy')
    if ((await line.getAttribute('role')) !== 'status') throw new Error('not a status')
    // #1339: Copy is 44 px to tap, though the line is no taller: a tap 5 px over or under it lands on it
    const lands = await page.evaluate(() => { const b = document.querySelector('.dstate-copy')!, r = b.getBoundingClientRect()
      return [r.top - 5, r.bottom + 5].map(y => b.contains(document.elementFromPoint(r.left + r.width / 2, y))) })
    if (lands.join() !== 'true,true') throw new Error('Copy\'s hit area: ' + lands)
    if (!(await page.textContent('#presence-text')).includes('waiting for you in the terminal')) throw new Error(await page.textContent('#presence-text'))
    if (await page.locator('.row.notice').count()) throw new Error('a thread row for a wait')
    if (await page.locator('.row.ask, .card .choices').count()) throw new Error('an answer card on the page')
    // a message sent meanwhile waits behind the prompt
    await page.fill('#input', 'sent while it waits')
    await page.click('#send')
    await until(async () => (await page.locator('.row.user .meta').last().textContent()).includes('Waiting behind the prompt in the terminal'))
    const plan = wb('chat', 'notice', '--tool', 'ExitPlanMode', '--kind', 'plan').trim()
    if (!wb('chat', 'notice', '--done', id).startsWith('answered')) throw new Error('not settled')
    waits({ kind: 'plan', tool: 'ExitPlanMode', since: Date.now() / 1000 })
    // the oldest open prompt is the one named: now the plan's, which names no permissions
    await until(async () => (await line.textContent()).includes('a plan to approve'))
    if ((await line.textContent()).includes('permissions granted') || (await line.locator('button').count())) throw new Error('a plan names permissions')
    // settled there: the line clears
    if (!wb('chat', 'notice', '--done', plan).startsWith('answered')) throw new Error('not settled')
    waits()
    await until(async () => await line.isHidden())
  })
  await check('Stats shows what the week\'s tasks saved Claude, once: the badge and its one line', async () => {
    const usage = { turns: 2, calls: 5, input: 1000, cache_write_5m: 0, cache_write_1h: 10000, cache_read: 200000, output: 3000 }
    writeFileSync(join(home, 'state/results.jsonl'), JSON.stringify({ ts: Date.now() / 1000, outcome: 'accepted',
      claude: usage, diff: { files: 4, added: 61, removed: 1 }, gpu_wh: 600 }) + '\n')
    await page.reload()
    await goTo('stats')
    await until(async () => (await page.textContent('#saved')).includes('The GPU used 600 Wh.'))
    // #1322: the saving is the badge's alone, not said again in the paragraph
    const said = await page.textContent('#saved')
    if (said.includes('3K') || said.includes('estimate')) throw new Error(said)
    // #845: a price in the user's own currency showed as dollars on the phone
    writeFileSync(env.WB_SAVINGS, JSON.stringify({ kwh_price: 0.3 }))
    await page.reload()                                    // on #stats still: the hash is the view
    await goTo('stats')
    await until(async () => (await page.textContent('#saved')).includes('The GPU used 600 Wh, cost 0.18.'))
    if ((await page.textContent('#saved')).includes('$')) throw new Error('a currency sign: ' + await page.textContent('#saved'))
    // #1022: an API task's dollars carry a `$` (OpenCode's unit), apart from the saving, while the
    // energy's cost still has none (#845)
    appendFileSync(join(home, 'state/results.jsonl'), JSON.stringify({ ts: Date.now() / 1000, outcome: 'accepted', kind: 'api', provider: 'deepseek',
        model: 'deepseek/deepseek-flash', api: { cost: 0.42, source: 'opencode' } }) + '\n')
    // #1135: at the catalog's prices a figure is an estimate (≈); at the user's own (wb model price) it isn't
    appendFileSync(join(home, 'state/results.jsonl'), JSON.stringify({ ts: Date.now() / 1000, outcome: 'accepted', kind: 'api', provider: 'deepinfra',
        model: 'deepinfra/acme-flash', api: { cost: 0.1, source: 'metered', prices: 'yours' } }) + '\n')
    await page.reload()
    await goTo('stats')
    await until(async () => (await page.textContent('#saved')).includes('API models: 2 tasks, ≈$0.52 (deepseek ≈$0.42, deepinfra $0.10).'))
    if (!(await page.textContent('#saved')).includes('The GPU used 600 Wh, cost 0.18. API')) throw new Error(await page.textContent('#saved'))
    writeFileSync(env.WB_SAVINGS, '{}')  // no price again, for the next engine
    // #101: the badge, its number and its words, legible at phone width
    if (await page.locator('#badge').isHidden()) throw new Error('no badge')
    const n = await page.textContent('#badge-n'), cls = await page.getAttribute('#badge-n', 'class')
    if (n !== '5 / 100' || cls !== 'badge-n red') throw new Error(`${n} ${cls}`)
    const s = await page.textContent('#badge-s')
    if (s !== '~3K Claude tokens saved this week') throw new Error(s)
    const box = await page.locator('#badge').boundingBox()
    if (box.width > (await page.evaluate(() => innerWidth))) throw new Error('wider than the phone')
    await goTo('chat')
    await until(async () => await page.locator('#saved').isHidden())
  })
  await check('the app: its worker runs, holds the key, and Settings says how to install it and get notifications', async () => {
    await goTo('settings')
    await until(() => page.evaluate(async () => !!(await navigator.serviceWorker.getRegistration())))
    await until(() => page.evaluate(async () => (await (await caches.open('wb-chat')).match('/key')) !== undefined))
    if (!(await page.textContent('#app')).includes('installs from the https address')) throw new Error('install offered over http')
    // its command as code, never its backticks as text (#1323)
    const cmd = await page.$$eval('#app code', (c: Element[]) => c.map(x => x.textContent))
    if (JSON.stringify(cmd) !== '["tailscale serve --bg 1337"]' || (await page.textContent('#app')).includes('`'))
      throw new Error('the install card: ' + await page.textContent('#app'))
    const notify = await page.textContent('#notify')
    if (engine === 'webkit' ? !notify.includes('installed app') : !(await page.locator('#push').count())) throw new Error('notifications: ' + notify)
    await goTo('chat')
    await until(async () => await page.locator('#settings').isHidden())
  })
  await shell()
  if (desktop) return finish()   // the desktop: the shell and its views; the thread's checks are the phones'
  await check('what the page showed on screen is reported, so it is not pushed', async () => {
    await page.locator('.thread').click({ position: { x: 5, y: 5 } })  // in use: touched just now
    const shown = JSON.parse(readFileSync(join(home, 'state/chat/messages.jsonl'), 'utf8').trim().split('\n').filter(l => l.includes('"claude"')).at(-1)!).id
    await until(() => existsSync(join(home, 'state/chat/seen.jsonl')) && readFileSync(join(home, 'state/chat/seen.jsonl'), 'utf8').includes(shown))
  })
  await check("a page on screen but not in use doesn't stop the push, and reports it once it is used", async () => {
    // #537 (the 2026-11-20 audit): a desktop tab left visible silenced every device's pushes
    const seen = () => existsSync(join(home, 'state/chat/seen.jsonl')) ? readFileSync(join(home, 'state/chat/seen.jsonl'), 'utf8') : ''
    await page.evaluate(() => { document._hasFocus = document.hasFocus; document.hasFocus = () => false })
    wb('chat', 'reply', '--text', 'Nobody is at this tab.')
    await until(async () => (await page.locator('.row.claude .bubble').last().textContent()).includes('Nobody is at this tab.'))
    const id = messages().filter(m => m.text === 'Nobody is at this tab.').at(-1).id
    await sleep(1500)
    if (seen().includes(id)) throw new Error('an unfocused page reported it')
    await page.evaluate(() => { document.hasFocus = document._hasFocus })
    await page.locator('.thread').click({ position: { x: 5, y: 5 } })
    await until(() => seen().includes(id))
  })
  await check('a server that takes calls and never answers: the page heals itself once it is back, no reload', async () => {
    // the 2026-11-18 report: "Reconnecting" until a hard refresh
    await page.evaluate(() => { window.sameLoad = true })
    server.kill('SIGSTOP')  // accepts at the kernel, answers nothing: past the page's 40 s watchdog
    await page.fill('#input', 'sent while it hung')
    await page.click('#send')
    await sleep(46000)
    server.kill('SIGCONT')
    await until(() => messages().filter(m => m.text === 'sent while it hung').length === 1, 40000)
    wb('chat', 'reply', '--text', 'Healed.')
    await until(async () => (await page.locator('.row.claude .bubble').last().textContent()).includes('Healed.'), 40000)
    await until(async () => await page.locator('#net').isHidden(), 20000)
    if (!(await page.evaluate(() => window.sameLoad))) throw new Error('the page reloaded')
    if (messages().filter(m => m.text === 'sent while it hung').length !== 1) throw new Error('landed twice')
  })
  await check('back on screen after the line died in its sleep: a new message shows at once, and on a push', async () => {
    // the owner, 2026-11-20: the push came at once, the open app showed the message 10 to 30 s
    // later. A proxy whose old connections go silent, as a phone's do after a sleep, new ones fine
    const socks = new Set<{ c: Socket; up: Socket; dead: boolean }>()
    const proxy = createServer(c => {
      const up = dial(PORT, '127.0.0.1'), s = { c, up, dead: false }
      socks.add(s)
      c.on('data', d => { if (!s.dead) up.write(d) })
      up.on('data', d => { if (!s.dead) c.write(d) })
      for (const x of [c, up]) x.on('error', () => {}).on('close', () => { c.destroy(); up.destroy(); socks.delete(s) })
    })
    await new Promise<void>(r => proxy.listen(PORT + 7, '127.0.0.1', r))
    const p = await ctx.newPage()
    p.on('pageerror', onError)
    const hide = (h: boolean) => p.evaluate((h: boolean) => {
      Object.defineProperty(document, 'hidden', { value: h, configurable: true })
      document.dispatchEvent(new Event('visibilitychange'))
    }, h)
    try {
      await p.goto(`http://localhost:${PORT + 7}/#token=${token}`)
      await until(async () => (await p.textContent('#presence-text')).includes('listening'))
      for (const [n, wake] of [[1, () => hide(false)] as const,
                               [2, () => p.evaluate(() => navigator.serviceWorker.dispatchEvent(
                                 new MessageEvent('message', { data: { kind: 'push' } })))] as const]) {
        for (const s of socks) s.dead = true                 // the sleep: the line goes silent
        if (n === 1) await hide(true)
        await sleep(n === 1 ? 4000 : 1000)
        wb('chat', 'reply', '--text', `While you slept ${n}.`)
        await sleep(500)
        await wake()
        await until(async () => (await p.locator('.row.claude .bubble').last().textContent()).includes(`While you slept ${n}.`), 5000)
      }
    } finally {
      await p.close()
      for (const s of socks) { s.c.destroy(); s.up.destroy() }
      proxy.close()
    }
  })
  await check('an updated page reloads itself, keyed, with the thread', async () => {
    await page.evaluate(() => { window.sameLoad = true })
    const css = join(ROOT, 'workbench/chat_static/app.css'), was = statSync(css)
    try {
      utimesSync(css, was.atime, new Date())
      wb('chat', 'reply', '--text', 'After the update.')  // any change sends the new build
      await until(async () => !(await page.evaluate(() => window.sameLoad)), 20000)
      await until(async () => (await page.locator('.row.claude .bubble').last().textContent()).includes('After the update.'))
    } finally { utimesSync(css, was.atime, was.mtime) }
  })
  await check('the strip over the thread names each open task\'s model, GPU or API, by the web app\'s one name for it (#1016, #1339)', async () => {
    const t = join(home, 'state/tasks/fix-auth')
    mkdirSync(t, { recursive: true })
    writeFileSync(join(t, 'task.json'), JSON.stringify({ name: 'fix-auth', model: 'deepseek/deepseek-flash', via: 'api',
      provider_name: 'DeepSeek', created_at: Date.now() / 1000 }))
    writeFileSync(join(t, 'status.json'), JSON.stringify({ phase: 'running', round: 1, updated_at: Date.now() / 1000 }))
    try {
      wb('chat', 'reply', '--text', 'Started fix-auth.')  // a chat change wakes the stream
      // #1339: no catalog here names it, so the id's last part; the provider is the sheet's note
      // a phone's strip is one line of counts and names, the model left to the sheet (#1339)
      await until(async () => (await page.textContent('#tasks')) === (desktop ? 'Running: fix-auth · API · deepseek-flash' : '1 running: fix-auth'))
      if (await page.locator('#tasks').isHidden()) throw new Error('the strip is hidden')
      const width = await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)
      if (!width) throw new Error('the page scrolls sideways')
    } finally { rmSync(t, { recursive: true, force: true }) }
    wb('chat', 'reply', '--text', 'Done with fix-auth.')
    await until(async () => await page.locator('#tasks').isHidden())
  })
  await check('a backlog on the strip names three, counts the queued apart and leaves the thread the screen (#1097)', async () => {
    // 25 open tasks were 22 lines of "Running: …" on an iPhone, the queued read as running, and
    // the thread a 36 px sliver; a task wb stop --all parked stays off it (chat.open_tasks); one
    // asking waits on Claude, never counted running (the #1097 review)
    const made: string[] = []
    const task = (name: string, phase: string, i: number, status = {}) => {
      const t = join(home, 'state/tasks', name)
      mkdirSync(t, { recursive: true }); made.push(t)
      writeFileSync(join(t, 'task.json'), JSON.stringify({ name, created_at: Date.now() / 1000 + i,
        ...(i % 2 ? { model: 'deepinfra/deepseek-ai/DeepSeek-V4.1-Flash', via: 'api', provider_name: 'DeepInfra' }
          : { model: 'qwen3-coder-30b' }) }))
      writeFileSync(join(t, 'status.json'), JSON.stringify({ phase, round: 1, updated_at: Date.now() / 1000, ...status }))
    }
    for (let i = 0; i < 25; i++) task(`backlog-step-${String(i).padStart(2, '0')}`, i < 3 ? 'queued' : i < 5 ? 'running' : i === 5 ? 'asking' : 'queued', i)
    task('old-parked', 'review', -1, { parked: Date.now() / 1000 })
    try {
      wb('chat', 'reply', '--text', 'Started the backlog.')
      if (desktop) {
        await until(async () => (await page.textContent('#tasks')).startsWith('2 running, 1 waiting on Claude, 22 queued: backlog-step-03 · '))
        const text = await page.textContent('#tasks')
        if (!text.endsWith('; +22 more') || text.includes('old-parked') || !text.includes('; backlog-step-05 (asking) · '))
          throw new Error('the strip says: ' + text)
      } else await until(async () => (await page.textContent('#tasks')) === '2 running, 1 need Claude, 22 queued: backlog-step-03, backlog-step-04, backlog-step-05 +22')
      const got = await page.evaluate(() => ({ strip: document.getElementById('tasks')!.getBoundingClientRect().height,
        thread: document.getElementById('scroll')!.getBoundingClientRect().height, height: innerHeight, line: parseFloat(getComputedStyle(document.getElementById('tasks')!).lineHeight) || 18 }))
      if (got.strip > got.line * (desktop ? 3 : 1) + 16) throw new Error(`the strip is ${got.strip} px tall`)
      if (got.thread < got.height * 0.4) throw new Error(`the thread has ${got.thread} of ${got.height} px`)
    } finally { for (const t of made) rmSync(t, { recursive: true, force: true }) }
    wb('chat', 'reply', '--text', 'Backlog done.')
    await until(async () => await page.locator('#tasks').isHidden())
  })
  await check('a tap on the strip opens the task sheet: summary, diff and log as text, and a reply tagged with the task (#1201)', async () => {
    // the report, diff and log are agent-written: drawn as text, never markup, on a page that
    // answers Claude Code's dialogs
    const repo = join(home, 'sheet-repo'), t = join(home, 'state/tasks/fix-sheet')
    const git = (...a: string[]) => execFileSync('git', ['-C', repo, '-c', 'user.email=t@t', '-c', 'user.name=t', ...a], { encoding: 'utf8' })
    mkdirSync(repo, { recursive: true }); git('init', '-q', '-b', 'main')
    writeFileSync(join(repo, 'README.md'), 'hello\n'); git('add', '.'); git('commit', '-qm', 'init')
    const base = git('rev-parse', 'HEAD').trim()
    mkdirSync(t, { recursive: true }); git('worktree', 'add', '-q', '-b', 'wb/fix-sheet', join(t, 'worktree'), base)
    writeFileSync(join(t, 'worktree/README.md'), 'hello\nworld <img src=x onerror="window.pwned=1">\n')
    writeFileSync(join(t, 'task.json'), JSON.stringify({ name: 'fix-sheet', model: 'qwen3-coder-30b', model_label: 'Qwen3 Coder 30B', repo, base,
      branch: 'wb/fix-sheet', created_at: Date.now() / 1000 - 300 }))
    writeFileSync(join(t, 'status.json'), JSON.stringify({ phase: 'review', round: 1, last: 'tests pass', updated_at: Date.now() / 1000 - 60 }))
    mkdirSync(join(t, 'rounds/1'), { recursive: true })
    writeFileSync(join(t, 'rounds/1/summary.md'), 'Changed the README. <b id="bold">not bold</b>\n')
    // more rows than the Log's box holds, the worktree's path in each (#1339: written away, and
    // the Log opens at its newest row)
    writeFileSync(join(t, 'events.jsonl'), Array.from({ length: 40 }, (_, i) => JSON.stringify({ kind: 'tool', round: 1, tool: 'read',
      brief: join(t, 'worktree', `src/f${i}.py`), ok: true, ts: Date.now() / 1000 }) + '\n').join('')
      + JSON.stringify({ kind: 'tool', round: 1, tool: 'bash', brief: 'pytest -q', ok: true, ts: Date.now() / 1000 }) + '\n')
    try {
      wb('chat', 'reply', '--text', 'Started fix-sheet.')
      await until(async () => (await page.textContent('#tasks')).includes('fix-sheet'))
      await page.click('#tasks')
      await until(async () => await page.locator('#task-sheet').isVisible())
      await page.click('.ts-row:has-text("fix-sheet")')
      await until(async () => (await page.textContent('#ts-body')).includes('Changed the README. <b id="bold">not bold</b>'))
      if (await page.locator('#bold').count()) throw new Error('the report was drawn as markup')
      const sum = await page.textContent('#ts-body')
      if (!sum.includes('review · round 1 · GPU Qwen3 Coder 30B') || !sum.includes('tests pass') || !sum.includes('started 5m ago'))
        throw new Error('the summary says: ' + sum)
      await page.click('#ts-tabs [data-part="diff"]')
      await until(async () => (await page.textContent('#ts-body')).includes('README.md | 1 +'))
      await page.click('text=Show the whole diff')
      await until(async () => (await page.locator('.ts-diff .add').count()) > 0)
      if (!(await page.textContent('.ts-diff')).includes('+world <img src=x onerror="window.pwned=1">')) throw new Error(await page.textContent('.ts-diff'))
      await page.click('#ts-tabs [data-part="log"]')
      await until(async () => (await page.textContent('#ts-body')).includes('r1 bash     pytest -q'))
      const log = await page.evaluate(() => { const l = document.querySelector<HTMLElement>('.ts-log')!
        return { text: l.textContent, end: l.scrollHeight - l.scrollTop - l.clientHeight, over: l.scrollHeight > l.clientHeight } })
      if (!log.text!.includes('r1 read     src/f39.py\n') || log.text!.includes('/worktree/')) throw new Error('the log says: ' + log.text!.slice(0, 200))
      if (!log.over || log.end > 2) throw new Error('the log is not at its newest row: ' + JSON.stringify(log))
      // #1559: a task's own status write wakes the stream, no chat write needed, and the open Log
      // follows its new rows, still at its end
      appendFileSync(join(t, 'events.jsonl'), JSON.stringify({ kind: 'tool', round: 1, tool: 'bash', brief: 'pytest -q again', ok: true, ts: Date.now() / 1000 }) + '\n')
      writeFileSync(join(t, 'status.json'), JSON.stringify({ phase: 'review', round: 1, last: 'tests pass', updated_at: Date.now() / 1000 }))
      await until(async () => (await page.textContent('#ts-body')).includes('r1 bash     pytest -q again'), 10000)
      if (await page.evaluate(() => { const l = document.querySelector<HTMLElement>('.ts-log')!; return l.scrollHeight - l.scrollTop - l.clientHeight }) > 2)
        throw new Error('the Log left its newest row')
      if (await page.evaluate(() => window.pwned)) throw new Error('agent text ran as markup')
      if (!(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))) throw new Error('the page scrolls sideways')
      // #1339: Reply is pinned to the sheet's foot, and under it the next steps of a task in review
      await page.click('#ts-tabs [data-part="summary"]')
      await until(async () => (await page.textContent('#ts-body')).includes('Full report, round 1'))
      const foot = await page.evaluate(() => ({ reply: document.getElementById('ts-reply')!.getBoundingClientRect().bottom,
        sheet: document.getElementById('task-sheet')!.getBoundingClientRect().bottom, h: innerHeight,
        next: [...document.querySelectorAll('#ts-next .chip')].map(c => c.textContent) }))
      if (foot.reply > Math.min(foot.sheet, foot.h) + 1 || foot.next.join() !== 'Review it') throw new Error('the foot: ' + JSON.stringify(foot))
      await page.click('#ts-reply')
      await until(async () => (await page.textContent('#about-text')) === 'Replying about fix-sheet')
      if (await page.locator('#task-sheet').isVisible()) throw new Error('the sheet stayed open')
      await page.fill('#input', 'is it on track?')
      await page.click('#send')
      await until(() => { const r = messages().at(-1); return r && r.text === 'is it on track?' && r.about })
      if (JSON.stringify(messages().at(-1).about) !== JSON.stringify({ task: 'fix-sheet' })) throw new Error(JSON.stringify(messages().at(-1).about))
      await until(async () => (await page.$$eval('.about-tag', (x: Element[]) => x.at(-1)?.textContent)) === 'About fix-sheet')
      // a failed task leads with what happened, and its next steps fill the box, tagged (#1339)
      writeFileSync(join(t, 'status.json'), JSON.stringify({ phase: 'failed', round: 1, last: 'opencode exited 1', updated_at: Date.now() / 1000 }))
      writeFileSync(join(t, 'rounds/1/summary.md'), `task fix-sheet\nopencode exited 1 (see ${t}/runner.log)\n\nagent's summary:\n(the agent wrote no final message)\n\nfiles changed:\n(none)\n`)
      wb('chat', 'reply', '--text', 'fix-sheet failed.')
      await page.click('#tasks')
      await until(async () => (await page.textContent('.ts-row:has-text("fix-sheet")')).includes('· failed'))
      await page.click('.ts-row:has-text("fix-sheet")')
      await until(async () => (await page.textContent('.ts-happened')) === 'The agent stopped before writing anything (OpenCode exited).')
      const next = await page.$$eval('#ts-next .chip', (x: Element[]) => x.map(c => c.textContent).join())
      if (next !== 'Why did it fail?,Try it again,Discard it') throw new Error('the next steps: ' + next)
      await page.click('#ts-next .chip:has-text("Why did it fail?")')
      await until(async () => (await page.inputValue('#input')) === 'Why did it fail?' && (await page.textContent('#about-text')) === 'Replying about fix-sheet')
      if (await page.locator('#task-sheet').isVisible()) throw new Error('the sheet stayed open')
      await page.click('#about-x'); await page.fill('#input', '')
    } finally {
      rmSync(t, { recursive: true, force: true }); rmSync(repo, { recursive: true, force: true })
    }
    wb('chat', 'reply', '--text', 'Done with fix-sheet.')
    await until(async () => await page.locator('#tasks').isHidden())
  })
  await check('the runs strip names each run and its progress; a tap opens its Settings row, whose Summary and Stop run wb (#1278)', async () => {
    // #1278's S12: a deep workflow seen from the phone: its flow, phase, stages and spend over the
    // thread; its Summary (wb run report) drawn as text under its row, its Stop said to its session
    const d = join(home, 'state/runs/night')
    mkdirSync(d, { recursive: true })
    const flow = JSON.parse(readFileSync(join(ROOT, 'agent/flows/audit.json'), 'utf8'))
    writeFileSync(join(d, 'run.json'), JSON.stringify({ name: 'night', flow, flow_name: 'audit', params: { areas: ['a.py'] },
      created: Date.now() / 1000 }))
    writeFileSync(join(d, 'status.json'), JSON.stringify({ phase: 'running', why: '', updated_at: Date.now() / 1000,
      stages: { find: { done: 1, running: 2, null: 0 } }, spent_usd: 0.25, budget_usd: 2 }))
    try {
      wb('chat', 'reply', '--text', 'Started run night.')  // a chat change wakes the stream
      await until(async () => (await page.textContent('#runs')) === 'Run: night (audit) · running · find 1/3 · $0.25 of $2.00')
      const width = await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)
      if (!width) throw new Error('the page scrolls sideways')
      // Settings opened once in this page (its rows loaded), then the chat: the tap's second
      // visit, which landed at the top of Settings (#1527, 2026-11-26)
      await goTo('settings')
      await until(async () => (await page.locator('.set-card[data-group="workflows"]').count()) === 1)
      await goTo('chat')
      await page.click('#runs')
      await until(() => page.evaluate(() => document.body.dataset.view === 'settings'))
      const row = page.locator('#settings-rows .set-row[data-key="run-night"]')
      await until(async () => (await row.count()) === 1)
      if (!(await row.textContent()).includes('flow audit · running (no driver) · find: 1 done, 2 running · $0.25 of $2.00'))
        throw new Error('the row says: ' + await row.textContent())
      if (!(await page.locator('.set-card[data-group="workflows"]').isVisible())) throw new Error('the workflows group is not shown')
      // in view, not only shown: on that second visit the card stayed 2,000 px down on a phone
      const inView = () => page.evaluate(() => {
        const top = document.querySelector('.set-card[data-group="workflows"]')!.getBoundingClientRect().top
        return top >= 0 && top < innerHeight
      })
      try { await until(inView, 3000) } catch { throw new Error('the workflows card is out of view') }
      await row.locator('button[data-value="summary"]').click()
      await until(async () => (await row.locator('pre.set-detail').count()) === 1)
      const detail = await row.locator('pre.set-detail').textContent()
      if (!detail.startsWith('run night · flow audit · running')) throw new Error('the summary says: ' + detail)
      await row.locator('button[data-value="stop"]').click()
      await until(() => JSON.parse(readFileSync(join(d, 'status.json'), 'utf8')).phase === 'stopped')
      const notices = readFileSync(join(home, 'state/notify.jsonl'), 'utf8').trim().split('\n').map(l => JSON.parse(l))
      if (!notices.some(n => n.run === 'night' && n.kind === 'run_stopped')) throw new Error('the stop told no one')
      await until(async () => (await row.locator('button[data-value="stop"]').count()) === 0)  // stopped: nothing to stop
      await goTo('chat')
      wb('chat', 'reply', '--text', 'Stopped run night.')  // the strip moves with the chat's state, as the tasks' does
      await until(async () => (await page.textContent('#runs')).startsWith('Run: night (audit) · stopped'))
    } finally { rmSync(d, { recursive: true, force: true }) }
    wb('chat', 'reply', '--text', 'Run night closed.')
    await until(async () => await page.locator('#runs').isHidden())
  })
  await check("wb shot's pair: both images labelled, one above the other on a phone, a reply tied to their step", async () => {
    const d = join(home, 'state/tasks/fix-nav/shots/1')
    mkdirSync(d, { recursive: true })
    for (const side of ['base', 'candidate']) copyFileSync(join(ROOT, 'workbench/chat_static/icon-192.png'), join(d, side + '.png'))
    const of = (side: string) => ({ rev: side === 'base' ? '1a2b3c4d5e6f' : '9f8e7d6c5b4a', image: join(d, side + '.png') })
    writeFileSync(join(d, 'shots.json'), JSON.stringify({ step: 1, round: 2, viewport: '390x844', base: of('base'), candidate: of('candidate') }))
    const shots = { task: 'fix-nav', n: 1, step: 1, round: 2, viewport: '390x844',
      base: { rev: '1a2b3c4d5e6f', image: true }, candidate: { rev: '9f8e7d6c5b4a', image: true } }
    appendFileSync(join(home, 'state/chat/messages.jsonl'), JSON.stringify({ id: 'shots-1', ts: Date.now() / 1000, from: 'claude',
      text: 'screenshots of fix-nav. Reply here to send feedback on it.', shots }) + '\n')
    await page.reload()
    await until(async () => (await page.$$eval('.shots img', (im: HTMLImageElement[]) => im.filter(i => i.complete && i.naturalWidth > 0).length)) === 2)
    const caps = await page.$$eval('.shots figcaption', (c: Element[]) => c.map(x => x.textContent))
    if (caps.join('|') !== 'Base · 1a2b3c4d5e|Candidate · 9f8e7d6c5b') throw new Error(caps.join('|'))
    const said = await page.textContent('.shots-of')
    if (!said.includes('fix-nav · step 1 · round 2 · 390x844')) throw new Error(said)
    const [a, b] = await page.$$eval('.shots figure', (f: Element[]) => f.map(x => x.getBoundingClientRect().toJSON()))
    if (!(b.top >= a.bottom)) throw new Error('side by side on a phone')
    const width = await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)
    if (!width) throw new Error('the page scrolls sideways')
    // Claude's words revealed in full first: the bubble growing over the pair moved the button
    // from under the tap on a slow runner (#1223), as it did Keep's (#397)
    await until(async () => (await page.locator('.bubble.flowing').count()) === 0)
    await page.click('text=Reply about these')
    await until(async () => (await page.textContent('#about-text')) === 'Replying about fix-nav, step 1')
    await page.fill('#input', 'the menu overlaps the logo')
    await page.click('#send')
    await until(() => { const rows = messages(); const r = rows.at(-1); return r && r.text === 'the menu overlaps the logo' && r.about })
    const r = messages().at(-1)
    if (JSON.stringify(r.about) !== JSON.stringify({ task: 'fix-nav', n: 1, step: 1, round: 2 })) throw new Error(JSON.stringify(r.about))
    if (!(await page.locator('#about').isHidden())) throw new Error('still replying about them')
    await until(async () => (await page.$$eval('.about-tag', (t: Element[]) => t.at(-1)?.textContent)) === 'About fix-nav, step 1')
  })
  await check('a pair whose screenshots were dropped with its archived task says they expired, not that they failed (#821)', async () => {
    const d = join(home, 'state/tasks/fix-old/shots/1')
    mkdirSync(d, { recursive: true })
    writeFileSync(join(d, 'shots.json'), JSON.stringify({ step: 1, round: 1, viewport: '390x844',
      base: { rev: 'aaaaaaaaaaaa', ok: true, pruned: true }, candidate: { rev: 'bbbbbbbbbbbb', ok: true, pruned: true } }))
    const pair = { task: 'fix-old', n: 1, step: 1, round: 1, viewport: '390x844',  // as recorded then
      base: { rev: 'aaaaaaaaaaaa', image: true }, candidate: { rev: 'bbbbbbbbbbbb', image: true } }
    appendFileSync(join(home, 'state/chat/messages.jsonl'), JSON.stringify({ id: 'shots-old', ts: Date.now() / 1000, from: 'claude',
      text: 'screenshots of fix-old.', shots: pair }) + '\n')
    await page.reload()
    const expired = async () => (await page.$$eval('.shots .missing', (m: Element[]) => m.map(x => x.textContent)))
      .filter((t: string) => t.includes('This screenshot has expired')).length
    await until(async () => (await expired()) === 2)
  })
  await check("wb audio's shortlist: each take plays from its blob, with its checks, and Keep sends the pick (#397)", async () => {
    const d = join(home, 'takes'); mkdirSync(d, { recursive: true })
    const wav = (secs: number, rate = 8000) => {  // a 440 Hz tone, 16-bit mono PCM
      const n = secs * rate, b = Buffer.alloc(44 + 2 * n)
      b.write('RIFF', 0); b.writeUInt32LE(36 + 2 * n, 4); b.write('WAVEfmt ', 8); b.writeUInt32LE(16, 16)
      b.writeUInt16LE(1, 20); b.writeUInt16LE(1, 22); b.writeUInt32LE(rate, 24); b.writeUInt32LE(2 * rate, 28)
      b.writeUInt16LE(2, 32); b.writeUInt16LE(16, 34); b.write('data', 36); b.writeUInt32LE(2 * n, 40)
      for (let i = 0; i < n; i++) b.writeInt16LE(Math.round(8000 * Math.sin(2 * Math.PI * 440 * i / rate)), 44 + 2 * i)
      return b
    }
    const clips = [1, 2].map(i => {
      const p = join(d, `a-door-slam-${i}.wav`)
      writeFileSync(p, wav(i))
      writeFileSync(join(d, `a-door-slam-${i}.json`), JSON.stringify({ text: 'a door slam', kind: 'sfx', seed: i, engine: 'fx',
        duration: i, peak_db: -12.3, clipped: 0, lead: 0, tail: 0, gaps: [] }))
      return p
    })
    wb('chat', 'listen', ...clips, '--text', 'Two takes of the door. Take 1 has the cleaner tail.')
    await until(async () => (await page.locator('.take audio').count()) === 2)
    await until(async () => (await page.$$eval('.take audio', (a: HTMLAudioElement[]) => a.filter(x => x.src.startsWith('blob:') && x.readyState >= 1).length)) === 2)
    const secs = await page.$$eval('.take audio', (a: HTMLAudioElement[]) => a.map(x => Math.round(x.duration)))
    if (secs.join(',') !== '1,2') throw new Error('durations ' + secs.join(','))
    const caps = await page.$$eval('.take figcaption', (c: Element[]) => c.map(x => x.textContent))
    if (caps.join('|') !== 'Take 1 · a-door-slam-1.wav|Take 2 · a-door-slam-2.wav') throw new Error(caps.join('|'))
    const line = await page.locator('.take .take-line').first().textContent()
    if (!line.includes('1.0 s · peak -12.3 dBFS')) throw new Error(line)
    const width = await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)
    if (!width) throw new Error('the page scrolls sideways')
    // Claude's words revealed in full first: the bubble growing under a tap moved the button away
    await until(async () => (await page.locator('.bubble.flowing').count()) === 0)
    await page.fill('#input', 'a draft kept as it was')
    await page.locator('.take .keep').nth(1).click()
    await until(() => { const r = messages().at(-1); return r && r.from === 'user' && r.about && r.about.audio })
    const r = messages().at(-1)
    if (r.text !== 'Keep take 2: a-door-slam-2.wav' || r.about.n !== 2 || r.about.path !== clips[1]) throw new Error(JSON.stringify(r))
    if ((await page.inputValue('#input')) !== 'a draft kept as it was') throw new Error('the draft was touched')
    await page.fill('#input', '')
    await until(async () => (await page.$$eval('.about-tag', (t: Element[]) => t.at(-1)?.textContent)) === 'Kept take 2 · a-door-slam-2.wav')
  })
  // the phone keyboard (2026-11-19): no browser here can raise one, so the touches are made by hand
  const touches = (moves: number[], on = 'main') => page.evaluate(([moves, on]: [number[], string]) => {
    const el = document.querySelector(on)!, r = el.getBoundingClientRect()
    const at = (y: number, x = r.left + r.width / 2) => new Touch({ identifier: 1, target: el, clientX: x, clientY: y, screenX: x, screenY: y })
    const fire = (type: string, t?: Touch) => el.dispatchEvent(new TouchEvent(type, { bubbles: true, touches: t ? [t] : [], changedTouches: [t || at(moves.at(-1)!)] }))
    fire('touchstart', at(moves[0]))
    for (const y of moves.slice(1)) fire('touchmove', at(y))
    fire('touchend')
  }, [moves, on])
  const focused = () => page.evaluate(() => document.activeElement?.id === 'input')
  if (engine === 'chromium') await check('the keyboard goes away with a swipe down or a tap on the thread, not a tap on the composer or a button', async () => {
    await page.focus('#input'); await touches([200, 230, 270])
    if (await focused()) throw new Error('a swipe down kept it')
    await page.focus('#input'); await touches([200])
    if (await focused()) throw new Error('a tap on the thread kept it')
    await page.focus('#input'); await touches([20], '#composer')
    if (!(await focused())) throw new Error('a tap on the composer put it away')
    await page.focus('#input'); await touches([10], 'main button')
    if (!(await focused())) throw new Error('a tap on a button in the thread put it away')
    await page.focus('#input'); await touches([300, 250, 200])
    if (!(await focused())) throw new Error('a swipe up put it away')
  })
  await check('once the keyboard is down the composer is back at the foot, the page unscrolled', async () => {
    await page.focus('#input')
    await page.evaluate(() => { document.body.style.transform = 'translateY(300px)'; document.body.style.height = '400px' })
    await page.evaluate(() => document.getElementById('input')!.blur())
    // the page fits again on a ladder of timers up to 1.2 s: waited for, not slept past (#1223)
    const at = () => page.evaluate(() => ({ t: document.body.style.transform, h: document.body.style.height,
      foot: Math.round(document.getElementById('composer')!.getBoundingClientRect().bottom), vh: innerHeight, y: scrollY,
      nav: document.getElementById('tabbar')!.getBoundingClientRect().toJSON() }))
    // the composer on the bar, the bar at the foot (#1284)
    const down = (a: any) => !a.t && !a.h && !a.y && Math.abs(a.foot - a.nav.top) <= 2 && Math.abs(a.nav.bottom - a.vh) <= 2
    await until(async () => down(await at())).catch(async () => { throw new Error(JSON.stringify(await at())) })
  })
  await check('two tabs offline, both closed, reopened: each message lands exactly once', async () => {
    // #199 (the 2026-11-19 audit): each tab saved its own copy of one outbox array over the
    // other's, and only the second tab's message ever reached the server
    const a = await ctx.newPage(), b = await ctx.newPage()
    for (const t of [a, b]) { t.on('pageerror', onError); await t.goto(base); await until(async () => await t.locator('#input').isEditable()) }
    await stopServer()
    for (const [t, text] of [[a, 'queued in tab one'], [b, 'queued in tab two']]) {
      await t.fill('#input', text)
      await t.click('#send')
      await until(async () => (await t.locator('.row.user.pending', { hasText: text }).count()) === 1)
    }
    await a.close(); await b.close()
    startServer()
    await until(async () => (await fetch(base)).status > 0)  // listening (WebKit's worker hid the wait)
    const c = await ctx.newPage()
    c.on('pageerror', onError)
    await c.goto(base)
    for (const text of ['queued in tab one', 'queued in tab two']) {
      await until(() => messages().filter(m => m.text === text).length === 1, 20000)
    }
    await until(async () => (await c.locator('.row.user.pending').count()) === 0, 20000)
    await sleep(1500)
    for (const text of ['queued in tab one', 'queued in tab two']) {
      const n = messages().filter(m => m.text === text).length
      if (n !== 1) throw new Error(`${text}: landed ${n} times`)
    }
    await c.close()
  })
  await check("storage full: a message stays in the box until the server has it, then lands once", async () => {
    // #245 (the audit of 1.2): at quota, Send cleared the box, no pending bubble showed, and
    // nothing reached the server
    const q = await ctx.newPage()
    q.on('pageerror', onError)
    await q.goto(base)
    await until(async () => await q.locator('#input').isEditable())
    await q.evaluate(() => { Storage.prototype.setItem = () => { throw new DOMException('full', 'QuotaExceededError') } })
    await q.fill('#input', 'sent with storage full')
    await q.click('#send')
    await until(() => messages().filter(m => m.text === 'sent with storage full').length === 1)
    await until(async () => (await q.inputValue('#input')) === '')
    await stopServer()
    await q.fill('#input', 'full and the server down')
    await q.click('#send')
    await sleep(1500)
    if ((await q.inputValue('#input')) !== 'full and the server down') throw new Error('the box was cleared before the server had it')
    if (!(await q.textContent('#net')).includes('storage is full')) throw new Error('no word on the full storage: ' + await q.textContent('#net'))
    startServer()
    await until(() => messages().filter(m => m.text === 'full and the server down').length === 1, 30000)
    await until(async () => (await q.inputValue('#input')) === '', 20000)
    await sleep(1500)
    for (const text of ['sent with storage full', 'full and the server down']) {
      const n = messages().filter(m => m.text === text).length
      if (n !== 1) throw new Error(`${text}: landed ${n} times`)
    }
    await q.close()
  })
  await check('a retry after a lost reply and 205 newer messages clears its pending bubble', async () => {
    // #264 (the audit of 1.2): the server logged it once and answered the retry, but the page
    // cleared an outbox item only from the newest 200 in the snapshot, so it stayed pending
    let lost = false
    await page.route('**/api/send', async (route: any) => {
      if (lost) return route.continue()
      lost = true
      await route.fetch()                                   // the server takes it,
      const file = join(home, 'state/chat/messages.jsonl')
      for (let i = 0; i < 205; i++) {
        appendFileSync(file, JSON.stringify({ id: `filler-${i}`, ts: Date.now() / 1000, from: 'claude', text: `filler ${i}` }) + '\n')
      }
      await route.abort('connectionreset')                  // and its reply is lost
    })
    await page.fill('#input', 'retried after a lost reply')
    await page.click('#send')
    await until(() => messages().filter(m => m.text === 'retried after a lost reply').length === 1)
    await until(async () => (await page.locator('.row.user.pending').count()) === 0, 30000)
    await page.unroute('**/api/send')
    const n = messages().filter(m => m.text === 'retried after a lost reply').length
    if (n !== 1) throw new Error(`landed ${n} times`)
  })
  await check("storage full: a 200 that doesn't name the message (a portal's page, an empty body) leaves it in the box and it is sent again", async () => {
    // #934 (the review of 1.90.24): any 200 cleared a held message's box, its only copy, though
    // nothing had reached the server
    // its own context with no service worker: WebKit's routing doesn't see a page's fetches
    // while one is registered
    const qc = await browser.newContext({ ...(ctxFor(device)), serviceWorkers: 'block' })
    const q = await qc.newPage()
    q.on('pageerror', onError)
    await q.goto(base + '#token=' + token)
    await until(async () => await q.locator('#input').isEditable())
    await q.evaluate(() => { Storage.prototype.setItem = () => { throw new DOMException('full', 'QuotaExceededError') } })
    const foreign = [{ body: '' }, { contentType: 'text/html', body: '<html>Welcome to the Wi-Fi</html>' },
      { contentType: 'application/json', body: JSON.stringify({ message: { cid: 'someone-else', text: 'not yours' } }) }]
    let n = 0
    await q.route('**/api/send', (route: any) => n < foreign.length ? route.fulfill({ status: 200, ...foreign[n++] }) : route.continue())
    try {
      const text = 'held through a captive portal'
      await q.fill('#input', text)
      await q.click('#send')
      for (let i = 1; i <= foreign.length; i++) {
        await until(() => n >= i, 30000)
        await sleep(200)
        if ((await q.inputValue('#input')) !== text) throw new Error(`the box was cleared by foreign 200 number ${i}`)
        if (messages().some(m => m.text === text)) throw new Error('reached the server through a foreign 200')
      }
      await until(() => messages().filter(m => m.text === text).length === 1, 20000)
      await until(async () => (await q.inputValue('#input')) === '')
      await sleep(1500)
      const rows = messages().filter(m => m.text === text)
      if (rows.length !== 1 || new Set(rows.map(m => m.cid)).size !== 1) throw new Error(`landed ${rows.length} times`)
    } finally { await qc.close() }
  })
  await check('a send whose reply body stalls after the headers is given up and sent again, and a later message still goes', async () => {
    // #935 (the review of 1.90.24): post() cleared its timer at the headers, so a body that never
    // came held flush, and every later send, until the page was reloaded. A proxy that answers
    // the first send's headers and one byte, then holds the line
    let stalled = 0
    const socks = new Set<{ c: Socket; up: Socket; held: boolean }>()
    const proxy = createServer(c => {
      const up = dial(PORT, '127.0.0.1'), s = { c, up, held: false }
      socks.add(s)
      c.on('data', d => {
        if (!s.held && !stalled && d.toString('latin1').startsWith('POST /api/send')) {
          stalled++; s.held = true
          c.write('HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 400\r\n\r\n{')
        }
        if (!s.held) up.write(d)
      })
      up.on('data', d => { if (!s.held) c.write(d) })
      for (const x of [c, up]) x.on('error', () => {}).on('close', () => { c.destroy(); up.destroy(); socks.delete(s) })
    })
    await new Promise<void>(r => proxy.listen(PORT + 8, '127.0.0.1', r))
    const p = await ctx.newPage()
    p.on('pageerror', onError)
    try {
      await p.goto(`http://localhost:${PORT + 8}/#token=${token}`)
      await until(async () => await p.locator('#input').isEditable())
      for (const text of ['its reply stalled', 'sent after the stall']) {
        await p.fill('#input', text)
        await p.click('#send')
        await until(() => stalled === 1)
      }
      // POST_MS (15 s), then the first backoff
      await until(() => ['its reply stalled', 'sent after the stall'].every(t => messages().some(m => m.text === t)), 25000)
      await until(async () => (await p.locator('.row.user.pending').count()) === 0)
      await sleep(1500)
      for (const text of ['its reply stalled', 'sent after the stall']) {
        const n = messages().filter(m => m.text === text).length
        if (n !== 1) throw new Error(`${text}: landed ${n} times`)
      }
    } finally {
      await p.close()
      for (const s of socks) { s.c.destroy(); s.up.destroy() }
      proxy.close()
    }
  })
  await check("a 429 from something in front of the chat is sent again by itself, and a 400 still shows as not sent", async () => {
    // #936 (the review of 1.90.24): every 4xx but 401 was a refusal, so a throttled send waited
    // for a tap on retry; uploads already took 408 and 429 as "try again later". No service
    // worker: WebKit's routing doesn't see a page's fetches while one is registered
    const tc = await browser.newContext({ ...(ctxFor(device)), serviceWorkers: 'block' })
    const t = await tc.newPage()
    t.on('pageerror', onError)
    try {
      await t.goto(base + '#token=' + token)
      await until(async () => await t.locator('#input').isEditable())
      let throttled = 0
      await t.route('**/api/send', (route: any) => throttled++ ? route.continue()
        : route.fulfill({ status: 429, headers: { 'Retry-After': '1' }, contentType: 'text/plain', body: 'slow down' }))
      await t.fill('#input', 'throttled once')
      await t.click('#send')
      await until(() => messages().filter(m => m.text === 'throttled once').length === 1)
      if (throttled !== 2) throw new Error(`sent ${throttled} times`)
      await until(async () => (await t.locator('.row.user.pending').count()) === 0)
      if (await t.locator('.row.user.failed').count()) throw new Error('marked not sent')
      await t.unroute('**/api/send')
      await t.route('**/api/send', (route: any) => route.fulfill({ status: 400, contentType: 'application/json', body: JSON.stringify({ error: 'bad request' }) }))
      await t.fill('#input', 'refused for good')
      await t.click('#send')
      const row = t.locator('.row.user.failed', { hasText: 'refused for good' })
      await until(async () => (await row.count()) === 1)
      if (!(await row.locator('.meta').textContent()).includes('Not sent: bad request')) throw new Error(await row.locator('.meta').textContent())
      await row.locator('.meta button', { hasText: 'delete' }).click()
      await until(async () => (await t.locator('.row.user.failed').count()) === 0)
      await sleep(1000)
      if (messages().filter(m => m.text === 'throttled once').length !== 1) throw new Error('throttled once landed twice')
    } finally { await tc.close() }
  })
  await check('storage full: a send whose reply trickles in over more than 15 s still clears the box, sent once', async () => {
    // the review of #935: post() gave the whole reply body 15 s, so a link too slow to carry its
    // snapshot in that time aborted every reply, and a held message, cleared only by its reply,
    // stayed in the box for good. A proxy that relays each send's reply a twelfth every 1.5 s
    let sends = 0
    type Paced = { c: Socket; up: Socket; slow: boolean; buf: Buffer; pacer: ReturnType<typeof setInterval> | 0; upClosed?: boolean }
    const socks = new Set<Paced>()
    const proxy = createServer(c => {
      const up = dial(PORT, '127.0.0.1'), s: Paced = { c, up, slow: false, buf: Buffer.alloc(0), pacer: 0 }
      socks.add(s)
      c.on('data', d => {
        if (d.toString('latin1').startsWith('POST /api/send')) { sends++; s.slow = true }
        up.write(d)
      })
      up.on('data', d => {
        if (!s.slow) return c.write(d)
        s.buf = Buffer.concat([s.buf, d])
        if (s.pacer) return
        const cut = s.buf.indexOf('\r\n\r\n')
        if (cut < 0) return
        const length = Number(/content-length:\s*(\d+)/i.exec(s.buf.subarray(0, cut).toString('latin1'))![1])
        c.write(s.buf.subarray(0, cut + 4)); s.buf = s.buf.subarray(cut + 4)
        const piece = Math.ceil(length / 12)
        let left = length
        s.pacer = setInterval(() => {
          const n = Math.min(piece, left, s.buf.length)
          c.write(s.buf.subarray(0, n)); s.buf = s.buf.subarray(n); left -= n
          if (left <= 0) {
            clearInterval(s.pacer); s.pacer = 0; s.slow = false
            if (s.buf.length) c.write(s.buf)
            s.buf = Buffer.alloc(0)
            if (s.upClosed) c.end()
          }
        }, 1500)
      })
      // the server closes after each reply (HTTP/1.0): the client's side stays open until the
      // trickle has carried it all
      up.on('error', () => {}).on('close', () => { s.upClosed = true; if (!s.pacer) c.destroy() })
      c.on('error', () => {}).on('close', () => { clearInterval(s.pacer); up.destroy(); socks.delete(s) })
    })
    await new Promise<void>(r => proxy.listen(PORT + 9, '127.0.0.1', r))
    const p = await ctx.newPage()
    p.on('pageerror', onError)
    try {
      await p.goto(`http://localhost:${PORT + 9}/#token=${token}`)
      await until(async () => await p.locator('#input').isEditable())
      await p.evaluate(() => { Storage.prototype.setItem = () => { throw new DOMException('full', 'QuotaExceededError') } })
      const text = 'held over a slow link'
      await p.fill('#input', text)
      await p.click('#send')
      await until(() => messages().filter(m => m.text === text).length === 1)
      await sleep(2000)
      if ((await p.inputValue('#input')) !== text) throw new Error('the box was cleared before the reply came')
      await until(async () => (await p.inputValue('#input')) === '', 30000)   // 18 s of reply
      if (sends !== 1) throw new Error(`sent ${sends} times: the trickling reply was given up`)
      const n = messages().filter(m => m.text === text).length
      if (n !== 1) throw new Error(`landed ${n} times`)
    } finally {
      await p.close()
      for (const s of socks) { clearInterval(s.pacer); s.c.destroy(); s.up.destroy() }
      proxy.close()
    }
  })
  await check('a message over 6 h unread says it was not passed to Claude, never Delivered, and sends again', async () => {
    // #536 (the 2026-11-20 audit): the mod skipped it, and the page said "Delivered" for good
    listen(false)
    // #843: about a pair of screenshots, sent again without it: Claude got the words, no task
    const about = { task: 'fix-nav', n: 1, step: 1, round: 2 }
    appendFileSync(join(home, 'state/chat/messages.jsonl'), JSON.stringify({ id: 'old-1', ts: Date.now() / 1000 - 7 * 3600,
      from: 'user', text: 'run the backlog overnight', about }) + '\n')
    const row = page.locator('.row.user', { hasText: 'run the backlog overnight' })
    await until(async () => (await row.locator('.meta').textContent()).includes('Not passed to Claude (over 6 h old)'))
    if ((await row.locator('.meta').textContent()).includes('Delivered')) throw new Error('says Delivered')
    await row.locator('.meta button', { hasText: 'send again' }).click()
    await until(() => messages().filter(m => m.text === 'run the backlog overnight').length === 2)
    const again = messages().filter(m => m.text === 'run the backlog overnight').at(-1)
    if (Date.now() / 1000 - again.ts > 60) throw new Error('not sent as a new message')
    if (JSON.stringify(again.about) !== JSON.stringify(about)) throw new Error('about lost: ' + JSON.stringify(again.about))
    await until(async () => (await page.locator('.row.user', { hasText: 'run the backlog overnight' }).last().locator('.meta').textContent()).includes('Delivered'))
  })
  await check('once the copy it sent has landed, "send again" can be pressed again, and sends another (the #1408 review)', async () => {
    // the #1408 review (2026-11-25): a row's line kept while its words stay the same kept the
    // pressed button disabled too, until a reload
    const again = page.locator('.row.user', { hasText: 'run the backlog overnight' }).first().locator('.meta button', { hasText: 'send again' })
    await until(async () => !(await again.isDisabled())).catch(() => { throw new Error('still disabled after the copy landed') })
    await again.click()
    await until(() => messages().filter(m => m.text === 'run the backlog overnight').length === 3)
    await until(async () => !(await again.isDisabled()))
  })
  await check('a refused message about a pair of screenshots, edited, is still about them (#843)', async () => {
    // #843: edit put the words and files back in the box but not what they were about. The pair
    // again, newest: the first one may have left the thread's newest 200 by now
    const shots = { task: 'fix-nav', n: 1, step: 1, round: 2, viewport: '390x844',
      base: { rev: '1a2b3c4d5e6f', image: true }, candidate: { rev: '9f8e7d6c5b4a', image: true } }
    appendFileSync(join(home, 'state/chat/messages.jsonl'), JSON.stringify({ id: 'shots-again', ts: Date.now() / 1000, from: 'claude',
      text: 'screenshots of fix-nav again.', shots }) + '\n')
    // its own row's button: #821's expired fix-old pair can sort after it in the thread
    const pair = page.locator('.row', { hasText: 'screenshots of fix-nav again.' }).locator('text=Reply about these')
    await until(async () => (await pair.count()) > 0)
    await until(async () => (await page.locator('.bubble.flowing').count()) === 0)  // as above (#1223)
    await pair.last().click()
    await until(async () => (await page.textContent('#about-text')) === 'Replying about fix-nav, step 1')
    await page.fill('#input', 'x'.repeat(8001))                    // past MAX_TEXT: refused
    await page.click('#send')
    const row = page.locator('.row.user.pending', { hasText: 'Not sent' })
    await until(async () => (await row.count()) === 1)
    if (!(await page.locator('#about').isHidden())) throw new Error('the box still said it before the edit')
    await row.locator('.meta button', { hasText: 'edit' }).click()
    await until(async () => (await page.textContent('#about-text')) === 'Replying about fix-nav, step 1')
    await page.fill('#input', 'the logo is cut off')
    await page.click('#send')
    await until(() => { const r = messages().at(-1); return r && r.text === 'the logo is cut off' })
    const r = messages().at(-1)
    if (JSON.stringify(r.about) !== JSON.stringify({ task: 'fix-nav', n: 1, step: 1, round: 2 })) throw new Error(JSON.stringify(r.about))
  })
  await check('a refused message edited after typing a new one: both are in the box, nothing lost', async () => {
    // #958 (the 2026-11-21 audit): edit put the refused words in the box over what was typed since
    await page.fill('#input', 'y'.repeat(8001))                    // past MAX_TEXT: refused
    await page.click('#send')
    const row = page.locator('.row.user.pending', { hasText: 'Not sent' })
    await until(async () => (await row.count()) === 1)
    await page.fill('#input', 'and the footer too')
    await row.locator('.meta button', { hasText: 'edit' }).click()
    await until(async () => (await row.count()) === 0)
    const box = await page.inputValue('#input')
    if (box !== 'y'.repeat(8001) + '\n\nand the footer too') throw new Error('the box holds ' + JSON.stringify(box.slice(-40)))
    if (await page.evaluate(() => localStorage.getItem('wb-chat-draft')) !== JSON.stringify(box)) throw new Error('the saved draft differs')
    await page.fill('#input', '')
  })
  await check('a photo attached, the page reloaded before sending: the chip is back, nothing uploaded twice; one message with one path reaches Claude', async () => {
    // #623, the owner (2026-11-20): never a file uploaded twice by a refresh
    const puts = []
    const count = (r: any) => { if (r.method() === 'PUT' && r.url().includes('/api/upload/')) puts.push(r.url()) }
    page.on('request', count)
    const png = readFileSync(join(ROOT, 'workbench/chat_static/icon-192.png'))
    const uploads = () => existsSync(join(home, 'state/chat/uploads')) ? readdirSync(join(home, 'state/chat/uploads')) : []
    const before = uploads().length
    await page.setInputFiles('#pick', { name: 'photo.png', mimeType: 'image/png', buffer: png })
    await until(async () => (await page.locator('#tray .att.done').count()) === 1)
    await page.fill('#input', 'what is this?')
    await page.reload()
    await until(async () => (await page.locator('#tray .att.done').count()) === 1)
    await until(() => page.$eval('#tray .face', (i: HTMLImageElement) => i.complete && i.naturalWidth > 0))
    if ((await page.inputValue('#input')) !== 'what is this?') throw new Error('the words were lost')
    if (await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)) throw new Error('the page scrolls sideways')
    await sleep(1000)
    if (puts.length !== 1 || uploads().length !== before + 1) throw new Error(`uploaded ${puts.length} times, ${uploads().length - before} stored`)
    await page.click('#send')
    await until(() => messages().some(m => m.files))
    await until(async () => (await page.locator('#tray').isHidden()))
    await sleep(1000)
    const sent = messages().filter(m => m.files)
    if (sent.length !== 1 || sent[0].text !== 'what is this?' || sent[0].files.length !== 1) throw new Error(JSON.stringify(sent))
    const got = wb('chat', 'pending').trim().split('\n').map(l => JSON.parse(l)).filter(m => m.files)  // what the mod delivers
    if (got.length !== 1 || got[0].files.length !== 1) throw new Error('Claude gets ' + JSON.stringify(got))
    if (!readFileSync(got[0].files[0].path).equals(png)) throw new Error('not the bytes sent')
    if (puts.length !== 1) throw new Error(`uploaded ${puts.length} times`)
    page.off('request', count)
    await until(() => page.$eval('.row.user .files .pic img', (i: HTMLImageElement) => i.complete && i.naturalWidth > 0))
    await page.click('.row.user .files .pic')
    await until(async () => (await page.locator('.viewer img').count()) === 1)
    await page.click('.viewer')
    await until(async () => (await page.locator('.viewer').count()) === 0)
  })
  await check("the thread's photos are fetched as they scroll near the view, not all at each load", async () => {
    // #754 (the 2026-11-21 audit, finding 101): every photo in the last 200 messages was fetched
    // whole, uncached, on every load, to fill 132-pixel thumbnails. And not for the frames the
    // thread saved at the last visit shows one near its end, before the server's newer messages
    // put it far above: CI (2026-11-24) saw that fetch from the load that first showed the fillers
    const now = Date.now() / 1000
    for (let i = 0; i < 60; i++) appendFileSync(join(home, 'state/chat/messages.jsonl'), JSON.stringify({ id: `fill-${i}`,
      ts: now + i / 100, from: 'claude', text: `filler reply ${i}\n\n` + 'a line of words to make it tall\n'.repeat(4) }) + '\n')
    const gets = []
    const count = (r: any) => { if (r.url().includes('/api/file/')) gets.push(r.url()) }
    page.on('request', count)
    // the server's thread held back, so the saved one is on screen for many frames, not one or none
    const hold = (on: boolean) => page.evaluate((on: boolean) => on ? sessionStorage.setItem('e2e-hold', '1') : sessionStorage.removeItem('e2e-hold'), on)
    await hold(true)
    try {
      await page.reload()  // rows written behind the server's back: a load shows them (and keeps them)
      await until(async () => (await page.locator('.row.claude .bubble').last().textContent()).includes('filler reply 59'))
      await hold(false)
      await sleep(1500)
      if (gets.length) throw new Error(`fetched ${gets.length} photo(s) the saved thread showed, far above the view in the server's`)
      await page.reload()
      await until(async () => (await page.locator('.row.claude .bubble').last().textContent()).includes('filler reply 59'))
      await sleep(1500)
      if (gets.length) throw new Error(`fetched ${gets.length} photo(s) far above the view`)
      await page.$eval('.row.user .files', (f: Element) => f.scrollIntoView())
      await until(() => gets.length === 1)
      await until(() => page.$eval('.row.user .files .pic img', (i: HTMLImageElement) => i.complete && i.naturalWidth > 0))
    } finally { page.off('request', count); await hold(false) }
  })
  await check('a file over the limit is refused on its chip, and ✕ drops it', async () => {
    writeFileSync(env.WB_CHAT_CONFIG, JSON.stringify({ upload_mb: 0.001 }))
    try {
      await page.setInputFiles('#pick', { name: 'big.txt', mimeType: 'text/plain', buffer: Buffer.alloc(4096, 97) })
      await until(async () => ((await page.locator('#tray .att.refused').textContent()) || '').includes('too big: 4 KB, the limit is 1 KB'))
      if (!(await page.locator('#send').isDisabled())) throw new Error('Send is on with nothing to send')
      await page.click('#tray .att .x')
      await until(async () => await page.locator('#tray').isHidden())
    } finally { writeFileSync(env.WB_CHAT_CONFIG, '{}') }
  })
  await check('photos picked and removed let go of their previews: no blob URL outlives its chip', async () => {
    // #746 (Codex's audit of 1.90.6, finding 17): four picked and removed, four Blob URLs made,
    // none revoked, each holding a full-size photo until the page closed
    const p = await ctx.newPage()
    await p.addInitScript(() => {
      window.live = new Set()
      const make = URL.createObjectURL, drop = URL.revokeObjectURL
      URL.createObjectURL = (b: Blob | MediaSource) => { const u = make(b); if (b instanceof File) window.live.add(u); return u }  // a picked file's, not the thread's
      URL.revokeObjectURL = (u: string) => { window.live.delete(u); drop(u) }
    })
    try {
      await p.goto(base)
      await until(async () => (await p.textContent('#presence-text')).length > 0)
      const png = readFileSync(join(ROOT, 'workbench/chat_static/icon-192.png'))
      for (let i = 0; i < 4; i++) {
        await p.setInputFiles('#pick', { name: `p${i}.png`, mimeType: 'image/png', buffer: Buffer.concat([png, Buffer.from([i])]) })
        await until(() => p.$eval('#tray .face', (img: HTMLImageElement) => img.complete && img.naturalWidth > 0))
        await p.click('#tray .att .x')
        await until(async () => await p.locator('#tray').isHidden())
      }
      await until(async () => (await p.evaluate(() => window.live.size)) === 0)
    } finally { await p.close() }
  })
  await check('a disk that refuses for good shows its reason on the chip, and a server failure stops after three tries', async () => {
    // #742 (the 2026-11-21 audit): a full disk or a name it can't save answered 500 or 507 to
    // every try, and the page re-sent the whole file forever, saying only "waiting for the connection"
    const puts = []
    const count = (r: any) => { if (r.method() === 'PUT' && r.url().includes('/api/upload/')) puts.push(r.url()) }
    page.on('request', count)
    const root = join(home, 'state/chat/uploads')
    mkdirSync(root, { recursive: true })
    chmodSync(root, 0o500)  // EACCES: a refusal another try can't fix, so `final`
    try {
      await page.setInputFiles('#pick', { name: 'full.txt', mimeType: 'text/plain', buffer: Buffer.from('full') })
      await until(async () => ((await page.locator('#tray .att.refused').textContent()) || '').includes('not saved: Permission denied'))
      await sleep(2000)
      if (puts.length !== 1) throw new Error(`a final refusal was sent ${puts.length} times`)
      await page.click('#tray .att .x')
      await until(async () => await page.locator('#tray').isHidden())
    } finally { chmodSync(root, 0o700); page.off('request', count) }
    if (engine === 'webkit') return  // its routing doesn't see an XHR's PUT; Chromium checks the rest
    const flaky = []
    await page.route('**/api/upload/*', (route: any) => {
      if (route.request().method() !== 'PUT') return route.continue()
      flaky.push(route.request().url())
      return route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ error: 'not saved: Input/output error' }) })
    })
    try {
      await page.setInputFiles('#pick', { name: 'flaky.txt', mimeType: 'text/plain', buffer: Buffer.from('flaky') })
      await until(async () => ((await page.locator('#tray .att.refused').textContent()) || '').includes('Input/output error'), 30000)
      await sleep(2500)
      if (flaky.length !== 3) throw new Error(`sent ${flaky.length} times`)
      await page.click('#tray .att .x')
      await until(async () => await page.locator('#tray').isHidden())
    } finally { await page.unroute('**/api/upload/*') }
  })
  await check('an upload check that never answers is given up after POST_MS and tried again: its message and a plain one after it both land (#1390)', async () => {
    // #1390: the GET that asks whether the computer has a file had no deadline, so one that never
    // answered held pump and flush, and every later message sat at Sending… until a reload. Hung
    // by the page's own fetch (WebKit routes none once the worker is registered), as a dead line
    // hangs: until its signal aborts it
    await page.evaluate(() => {
      window._fetch = window.fetch; window._checks = 0
      window.fetch = (u, o = {}) => {
        if (!(/\/api\/upload\//.test(String(u)) && (o.method || 'GET') === 'GET' && !window._checks++)) return window._fetch(u, o)
        return new Promise((_, rej) => o.signal && o.signal.addEventListener('abort', () => rej(new DOMException('aborted', 'AbortError'))))
      }
    })
    try {
      await page.setInputFiles('#pick', { name: 'hung.txt', mimeType: 'text/plain', buffer: Buffer.from('hung') })
      await until(async () => await page.evaluate(() => window._checks) === 1)
      await page.fill('#input', 'its file was checked on a dead line')
      await page.click('#send')
      await page.fill('#input', 'and a plain one after it')
      await page.click('#send')
      await until(() => messages().some(m => m.text === 'and a plain one after it'), 60000)
      await until(() => messages().some(m => m.text === 'its file was checked on a dead line'), 30000)
      const sent = messages().filter(m => m.text === 'its file was checked on a dead line')
      if (sent.length !== 1 || !sent[0].files || sent[0].files.length !== 1) throw new Error(JSON.stringify(sent))
      await until(async () => (await page.locator('.row.user.pending').count()) === 0)
    } finally { await page.evaluate(() => { window.fetch = window._fetch }) }
  })
  await check("another agent's message shows with its name, and Claude's reply to it, named and never counted unread, comes back through /api/wait (#1042, #1128)", async () => {
    const agent = wb('chat', 'token', 'add', `e2e-${engine}`).trim()
    const api = (path: string, body?: object): Promise<[number, any]> => fetch(base + path.slice(1), { method: body ? 'POST' : 'GET',
      headers: { Authorization: `Bearer ${agent}`, ...(body ? { 'Content-Type': 'application/json' } : {}) },
      body: body ? JSON.stringify(body) : undefined }).then(async r => [r.status, await r.json()])
    const [code, sent] = await api('/api/send', { text: 'review the parser change' })
    if (code !== 200 || sent.message.role !== 'agent') throw new Error(`send: ${code} ${JSON.stringify(sent)}`)
    const row = page.locator('.row.agent').last()
    await until(async () => (await row.locator('.bubble').textContent()) === 'review the parser change')
    await until(async () => (await row.locator('.meta .agent-name').textContent()) === `e2e-${engine}`)
    if ((await row.locator('.meta').textContent()).includes('send again')) throw new Error('offered to the user to send')
    wb('chat', 'reply', '--to', sent.message.id, '--text', 'Reviewed: two nits.')
    const [, got] = await api(`/api/wait?after=${sent.message.id}&timeout=5`)
    if (got.replies.map((r: any) => r.text).join() !== 'Reviewed: two nits.') throw new Error(JSON.stringify(got))
    if ((await api('/api/settings', { key: 'coding', value: 'ask' }))[0] !== 403) throw new Error('an agent changed the settings')
    // its view is its own conversation: none of the user's messages, no settings
    const [, st] = await api('/api/state')
    if (st.push || st.savings) throw new Error('an agent read the user\'s state: ' + Object.keys(st))
    if (st.messages.some((m: any) => m.from === 'user')) throw new Error('an agent read the user\'s messages')
    // #1128: Claude's reply to the agent alone read as one to the user, and counted in the tab's
    // unread while the page was hidden; it names the agent, and only a reply to the user counts
    const reply = page.locator('.row.claude').last()
    await until(async () => (await reply.locator('.bubble').textContent()).includes('Reviewed: two nits.'))
    if ((await reply.locator('.meta').textContent()).indexOf(`Claude to e2e-${engine} · `) !== 0
        || !(await reply.getAttribute('class')).includes('to-agent')) throw new Error(await reply.locator('.meta').textContent())
    await page.evaluate(() => Object.defineProperty(document, 'hidden', { value: true, configurable: true }))
    try {
      wb('chat', 'reply', '--to', sent.message.id, '--text', 'One more nit.')
      await until(async () => (await page.locator('.row.claude .bubble').last().textContent()).includes('One more nit.'))
      if (await page.title() !== 'Chat · Workbench') throw new Error('counted unread: ' + await page.title())
      wb('chat', 'reply', '--to', 'user', '--text', 'The agent has its review.')
      await until(async () => await page.title() === '(1) Chat · Workbench')
      if ((await page.locator('.row.claude .meta').last().textContent()).includes(' to ')) throw new Error('the user\'s reply named an addressee')
    } finally {
      await page.evaluate(() => { delete (document as any).hidden; document.dispatchEvent(new Event('visibilitychange')) })
    }
    await until(async () => await page.title() === 'Chat · Workbench')
  })
  await check("the mic shows only with a listener; a clip recorded, or a voice memo picked over plain http, comes back as words in the box and nothing is sent; one that meets a busy GPU or a proxy's 502 waits (#1203)", async () => {
    if (await page.locator('#mic').isVisible()) throw new Error('a mic with no listener')
    writeFileSync(env.WB_AUDIO, JSON.stringify({ ear: { kind: 'listen', gpu: false, command: 'sh -c "echo Run the tests again." sh {in}' } }))
    const sent = messages().length
    const uploads = () => existsSync(join(home, 'state/chat/uploads')) ? readdirSync(join(home, 'state/chat/uploads')) : []
    const before = new Set(uploads())
    const p = await ctx.newPage(), q = await ctx.newPage()
    // a stand-in microphone and recorder: Playwright's browsers have no microphone to grant
    const recorder = () => {
      Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: { getUserMedia: async () => ({ getTracks: () => [{ stop() {} }] }) } })
      ;(window as any).MediaRecorder = class {
        [k: string]: any
        static isTypeSupported(t: string) { return t === 'audio/webm' }
        constructor(stream: unknown, o?: { mimeType?: string }) { this.mimeType = (o && o.mimeType) || '' }
        start() {}
        stop() { this.ondataavailable({ data: new Blob(['\x1aE\xdf\xa3 a recorded clip'], { type: this.mimeType }) }); this.onstop() }
      }
    }
    const say = async (pg: any) => {  // a tap to record, a tap to stop
      await pg.click('#mic')
      await until(async () => await pg.getAttribute('#mic', 'aria-label') === 'Stop recording')
      await pg.click('#mic')
    }
    const waiting = (pg: any) => pg.evaluate(() => JSON.parse(localStorage.getItem('wb-chat-dictate') || '[]').length)
    await p.addInitScript(recorder)
    await q.addInitScript(() => Object.defineProperty(window, 'isSecureContext', { get: () => false, configurable: true }))
    try {
      await p.goto(base)
      await until(async () => await p.locator('#mic').isVisible())
      await p.fill('#input', 'Then')
      await p.click('#mic')
      if (!(await p.getAttribute('#mic', 'class')).includes('rec') || await p.getAttribute('#mic', 'aria-label') !== 'Stop recording') throw new Error('not recording')
      await p.click('#mic')
      await until(async () => (await p.inputValue('#input')) === 'Then Run the tests again.')
      await until(async () => !(await p.getAttribute('#mic', 'class')).includes('busy'))
      const fresh = uploads().filter(u => !before.has(u))
      if (fresh.length !== 1 || !existsSync(join(home, 'state/chat/uploads', fresh[0], '.transcript.txt'))) throw new Error('stored: ' + fresh)
      if (!readdirSync(join(home, 'state/chat/uploads', fresh[0])).some(n => n.endsWith('.webm'))) throw new Error('not saved as webm')
      if (await p.evaluate(() => document.documentElement.scrollWidth > innerWidth)) throw new Error('the page scrolls sideways')
      await p.fill('#input', '')
      // a GPU listener while a task holds the GPU: the server says 409 at once, and the clip is
      // kept until the GPU is free. A 409 dropped it, for the user to say again (#1203)
      writeFileSync(env.WB_AUDIO, JSON.stringify({ ear: { kind: 'listen', command: 'sh -c "echo Run the tests again." sh {in}' } }))
      const task = spawn('python3', ['-c', 'import sys\nsys.path.insert(0, sys.argv[1])\nfrom workbench import models\n'
        + "with models.GpuLease(None, 't-busy', lambda *_: None):\n    print('held', flush=True)\n    sys.stdin.read()\n", ROOT],
        { env, stdio: ['pipe', 'pipe', 'inherit'] })
      try {
        await new Promise((ok, no) => { task.stdout.once('data', ok); task.once('exit', no) })
        await say(p)
        await until(async () => await p.getAttribute('#mic', 'aria-label') === 'Waiting for the GPU…')
        await until(async () => ((await p.textContent('#net')) || '').includes('A task has the GPU: the clip waits'))
        if (await waiting(p) !== 1) throw new Error('the clip was not kept')
        await sleep(1500)  // asked again meanwhile, and still kept
        if (await waiting(p) !== 1 || await p.inputValue('#input')) throw new Error('heard while the GPU was a task\'s')
      } finally { task.stdin.end() }
      await new Promise<void>(r => task.exitCode !== null ? r() : task.once('exit', () => r()))
      await until(async () => (await p.inputValue('#input')) === 'Run the tests again.', 40000)  // BACKOFF's steps
      await until(async () => await p.getAttribute('#mic', 'aria-label') === 'Dictate')
      if (await waiting(p)) throw new Error('the clip is still waiting')
      await p.fill('#input', '')
      writeFileSync(env.WB_AUDIO, JSON.stringify({ ear: { kind: 'listen', gpu: false, command: 'sh -c "echo Run the tests again." sh {in}' } }))
      // a proxy's 5xx (tailscale serve while the chat restarts) is in the way, not the listener's
      // verdict: kept and asked again. A page route sees no request a service worker makes
      const vc = await browser.newContext({ ...(device ? pw.devices[device] : {}), serviceWorkers: 'block' })
      try {
        const v = await vc.newPage()
        v.on('pageerror', (e: unknown) => errors.push(String(e)))
        await v.addInitScript(recorder)
        let asked = 0
        await v.route('**/api/transcribe/**', (route: any) => ++asked === 1
          ? route.fulfill({ status: 502, contentType: 'text/html', body: '<html>Bad Gateway</html>' }) : route.continue())
        await v.goto(base + '#token=' + token)
        await until(async () => await v.locator('#mic').isVisible())
        await say(v)
        await until(async () => (await v.inputValue('#input')) === 'Run the tests again.')
        if (asked !== 2) throw new Error('asked ' + asked + ' times')
      } finally { await vc.close() }
      // over plain http there is no microphone: the mic opens the phone's recorder or a voice memo
      await q.goto(base)
      await until(async () => await q.locator('#mic').isVisible())
      const [chooser] = await Promise.all([q.waitForEvent('filechooser'), q.click('#mic')])
      if (await chooser.element().getAttribute('accept') !== 'audio/*') throw new Error('the chooser takes ' + await chooser.element().getAttribute('accept'))
      await chooser.setFiles({ name: 'memo.m4a', mimeType: 'audio/mp4', buffer: Buffer.from('\0\0\0\x18ftypM4A a voice memo') })
      await until(async () => (await q.inputValue('#input')) === 'Run the tests again.')
      await q.fill('#input', '')
      // a listener that fails says so, and the clip isn't tried for good
      writeFileSync(env.WB_AUDIO, JSON.stringify({ ear: { kind: 'listen', gpu: false, command: 'sh -c "echo no model >&2; exit 3" sh {in}' } }))
      const [again] = await Promise.all([q.waitForEvent('filechooser'), q.click('#mic')])
      await again.setFiles({ name: 'memo2.m4a', mimeType: 'audio/mp4', buffer: Buffer.from('\0\0\0\x18ftypM4A another memo') })
      await until(async () => ((await q.textContent('#net')) || '').includes('Not transcribed: the listener failed: listener ear exited 3: no model'))
      await until(async () => !(await q.getAttribute('#mic', 'class')).includes('busy'))
      if (await q.inputValue('#input')) throw new Error('words in the box from a failed clip')
      if (messages().length !== sent) throw new Error('a message was sent')
    } finally {
      writeFileSync(env.WB_AUDIO, '{}')
      await p.close(); await q.close()
    }
  })
  await check('with OpenCode listening, the page names OpenCode: the header, the receipt, its reply, and with none listening the wait (#1319)', async () => {
    // the plugin's claim as chat.hold writes it: the server's pid (this process stands in), `tool` (#1319)
    writeFileSync(join(home, 'state/chat/listener.json'), JSON.stringify({ session: 'ses_e2e', tool: 'opencode', driver: 'opencode',
      cwd: '/home/u/project', pid: process.pid, busy: false }))
    await until(async () => (await page.textContent('#presence-text')).startsWith('OpenCode · listening in ~/project'))
    const n = messages().length
    await page.fill('#input', 'over to OpenCode')
    await page.click('#send')
    await until(() => messages().length === n + 1)
    wb('chat', 'ack', messages().at(-1).id, '--by', 'ses_e2e')
    await until(async () => (await page.locator('.row.user .meta').last().textContent()).includes('Read by OpenCode'))
    // its reply from the session's shell, stamped `driver` by chat.writer_driver: never Claude Code's here
    execFileSync(join(ROOT, 'bin', 'wb'), ['chat', 'reply', '--text', 'OpenCode on it.'],
      { env: { ...env, CLAUDECODE: '', CLAUDE_PID: '', CLAUDE_CODE_SESSION_ID: '', OPENCODE_SESSION_ID: 'ses_e2e' }, encoding: 'utf8' })
    if (JSON.stringify(messages().at(-1).driver) !== '{"tool":"opencode"}') throw new Error(JSON.stringify(messages().at(-1)))
    await until(async () => (await page.locator('.row.claude .meta').last().textContent()).startsWith('OpenCode · '))
    writeFileSync(join(home, 'state/chat/listener.json'), '{}')
    await until(async () => (await page.textContent('#presence-text')).includes('no session listening'))
    await page.fill('#input', 'still there?')
    await page.click('#send')
    await until(async () => (await page.locator('.row.user .meta').last().textContent()).includes('Waiting for an OpenCode session'))
    if ((await page.locator('.row.user .meta').nth(-2).textContent()).includes('Claude')) throw new Error('a receipt still names Claude')
  })
  await check('with the chat switched off, the state line says so and names wb chat on, never a driver to start (#1526)', async () => {
    // #1526: a server a service kept up told the user to start their driver and run /workbench,
    // which can't make it listen while the switch is off (what `wb chat off` writes)
    const sw = join(home, 'state/chat/switch.json'), line = page.locator('#driver-state')
    const was = existsSync(sw) ? readFileSync(sw, 'utf8') : JSON.stringify({ on: true, v: 2 })
    writeFileSync(sw, JSON.stringify({ on: false, v: 2 }))
    writeFileSync(join(home, 'state/chat/listener.json'), '{}')  // a write the streams wake on
    try {
      await until(async () => (await line.isVisible()) && (await line.textContent()).includes('The web chat is off'))
      if ((await line.locator('code').textContent()) !== 'wb chat on') throw new Error(await line.textContent())
      if ((await line.textContent()).includes('/workbench')) throw new Error(await line.textContent())
      writeFileSync(sw, JSON.stringify({ on: true, v: 2 }))
      writeFileSync(join(home, 'state/chat/listener.json'), '{}')
      // on again: none's line, how to start a driver (it shows `next` alone)
      await until(async () => { const t = await line.textContent(); return !t.includes('is off') && t.includes('it answers here') })
    } finally {
      writeFileSync(sw, was)  // as the checks before left it
      writeFileSync(join(home, 'state/chat/listener.json'), '{}')
    }
  })
  await check('jump-to-newest goes at once for a reader who prefers less motion (#1339)', async () => {
    // #1339 (2026-11-26): the one motion the stylesheet's reduce block can't reach, a smooth
    // scroll of 2.5 s over a long thread
    for (let i = 0; i < 30; i++) wb('chat', 'reply', '--text', `Line ${i} of a long thread.\n\nAnd its second paragraph.`)
    const rc = await browser.newContext({ ...ctxFor(device), reducedMotion: 'reduce', serviceWorkers: 'block' })
    const p = await rc.newPage()
    try {
      await p.goto(base + '#token=' + token)
      await until(async () => (await p.locator('.row.claude .bubble').last().textContent()).includes('Line 29'))
      await p.evaluate(() => { const m = document.getElementById('scroll')!; m.dispatchEvent(new WheelEvent('wheel', { deltaY: -100 })); m.scrollTop = 0 })
      await until(async () => await p.locator('#jump').isVisible())
      const end = await p.evaluate(() => new Promise(r => {
        const m = document.getElementById('scroll')!
        document.getElementById('jump')!.click()
        requestAnimationFrame(() => r(m.scrollHeight - m.scrollTop - m.clientHeight))
      }))
      if (Number(end) > 2) throw new Error(`still ${end} px from the end a frame after the tap`)
    } finally { await rc.close() }
  })
  await finish()

  async function finish() {
    await check('no script errors', async () => { if (errors.length) throw new Error(errors.join('; ')) })
    await browser.close()
    await stopServer()
  }

  // The shell (#1284): the nav, the views by the hash, the Chat tab's badge, the nav and the
  // keyboard, window.WB; then each view's own checks (e2e_stats.mts, e2e_settings.mts)
  async function shell() {
    await check(desktop ? 'a left rail of Chat, Stats and Settings, the chat a centred column; no gear'
                        : 'three tabs at the foot, Chat, Stats and Settings, under the composer; no gear', async () => {
      await goTo('chat')
      if (await page.locator('#gear').count()) throw new Error('the gear is still there')
      const g = await page.evaluate(() => {
        const r = (s: string) => document.querySelector(s)!.getBoundingClientRect().toJSON()
        return { nav: r('#tabbar'), chat: r('#view-chat'), composer: r('#composer'), thread: r('#thread'), w: innerWidth, h: innerHeight,
          tabs: [...document.querySelectorAll<HTMLElement>('#tabbar .tab')].map(b => ({ view: b.dataset.view, label: b.querySelector('.tab-lbl')!.textContent,
            icon: !!b.querySelector('svg'), w: b.getBoundingClientRect().width, current: b.getAttribute('aria-current') })) }
      })
      const say = () => JSON.stringify(g)
      if (g.tabs.map((t: any) => `${t.view}:${t.label}:${t.icon}`).join() !== 'chat:Chat:true,stats:Stats:true,settings:Settings:true') throw new Error(say())
      if (g.tabs.map((t: any) => t.current).join() !== 'page,,') throw new Error('the current tab: ' + say())
      const near = (a: number, b: number) => Math.abs(a - b) <= 2
      if (desktop) {
        if (!near(g.nav.left, 0) || !near(g.nav.width, 76) || !near(g.nav.top, 0) || !near(g.nav.height, g.h)) throw new Error('the rail: ' + say())
        if (g.chat.left < g.nav.right - 1) throw new Error('the chat under the rail: ' + say())
        if (g.thread.width > 761 || !near(g.thread.left + g.thread.width / 2, g.chat.left + g.chat.width / 2)) throw new Error('the thread: ' + say())
      } else {
        if (!near(g.nav.left, 0) || !near(g.nav.width, g.w) || !near(g.nav.bottom, g.h) || g.nav.height < 56) throw new Error('the bar: ' + say())
        if (!near(g.composer.bottom, g.nav.top)) throw new Error('the composer is not on the bar: ' + say())
        if (g.tabs.some((t: any) => !near(t.w, g.w / 3))) throw new Error('unequal tabs: ' + say())
      }
    })
    if (desktop) await check('an iPhone held sideways, its notch on the left: the rail takes the inset beside its 76 px, each tab wide enough for its label, and the views pad for it no more (#1413)', async () => {
      // #1413 (2026-11-25): inside a 76 px rail padded 59 px each tab was 16 px wide, its icon past
      // the rail's edge, its label cut to 'C.'. No browser here has a notch: app.css with an
      // iPhone's sideways insets written in for env()
      const css = readFileSync(join(ROOT, 'workbench/chat_static/app.css'), 'utf8')
        .replace(/env\(safe-area-inset-(left|right)\)/g, '59px').replace(/env\(safe-area-inset-bottom\)/g, '21px').replace(/env\(safe-area-inset-top\)/g, '0px')
      const sc = await browser.newContext({ viewport: { width: 844, height: 390 }, serviceWorkers: 'block' })
      await sc.route('**/app.css', (r: any) => r.fulfill({ contentType: 'text/css', body: css }))
      const p = await sc.newPage()
      try {
        await p.goto(base + '#token=' + token)
        await until(async () => await p.evaluate(() => getComputedStyle(document.body).display === 'grid'))
        const g = await p.evaluate(() => ({ rail: document.getElementById('tabbar')!.getBoundingClientRect().right,
          tabs: [...document.querySelectorAll('#tabbar .tab')].map(t => { const l = t.querySelector('.tab-lbl')
            return { w: Math.round(t.getBoundingClientRect().width), cut: l!.scrollWidth > l!.clientWidth, icon: t.querySelector('svg')!.getBoundingClientRect().right } }),
          pads: ['.top', '#thread', '#composer'].map(s => getComputedStyle(document.querySelector(s)!).paddingLeft) }))
        if (g.tabs.some((t: any) => t.w < 44 || t.cut || t.icon > g.rail) || g.pads.join() !== '16px,16px,12px') throw new Error(JSON.stringify(g))
      } finally { await sc.close() }
    })
    if (desktop) await check('a short window has a short header, and with the keyboard up the rail gives its column back (#1339)', async () => {
      // #1339 (2026-11-26): held sideways a phone's thread had 41% of the screen, and none with
      // the keyboard up, the hidden rail's 76 px column still kept
      const sc = await browser.newContext({ viewport: { width: 844, height: 390 }, serviceWorkers: 'block' })
      const p = await sc.newPage()
      try {
        await p.goto(base + '#token=' + token)
        await until(async () => await p.evaluate(() => getComputedStyle(document.body).display === 'grid'))
        const head = await p.evaluate(() => document.querySelector('.top')!.getBoundingClientRect().height)
        if (head > 44) throw new Error(`the header is ${head} px tall`)
        const cols = await p.evaluate(() => { document.body.classList.add('kb-open'); return getComputedStyle(document.body).gridTemplateColumns })
        if (!cols.startsWith('0px')) throw new Error('the columns: ' + cols)
      } finally { await sc.close() }
    })
    await check('the card views\' styles stay out of the chat: a plan card\'s text and code, and the intro\'s code, as before', async () => {
      // the #1284 review: `.view .card p` and `.view code` matched #view-chat, a .view too, and
      // shrank a plan's <p> (its margins 0 and 8px) and code, and the intro's code, to 13px
      const got = await page.evaluate(() => {
        const row = document.createElement('div'); row.className = 'row ask'
        row.innerHTML = '<div class="card"><div class="plan"><p>Plan <code>x</code></p></div></div>'
        document.getElementById('thread')!.append(row)
        const cs = (s: string) => getComputedStyle(row.querySelector(s)!), ip = document.querySelector('#intro p')!
        const out = { card: cs('.card').fontSize, p: cs('p').fontSize, margins: cs('p').marginTop + ' ' + cs('p').marginBottom,
          code: cs('code').fontSize, intro: getComputedStyle(ip).fontSize, introCode: getComputedStyle(ip.querySelector('code')!).fontSize }
        row.remove()
        return out
      })
      if (got.p !== got.card || got.margins !== `${got.p} ${got.p}` || got.code !== got.p || got.introCode !== got.intro) throw new Error(JSON.stringify(got))
    })
    if (desktop) await check('Stats and Settings are 1040 px of cards, two columns from 1100 px', async () => {
      await goTo('settings')
      const got = await page.evaluate(() => ({ w: document.querySelector('#view-settings .view-in')!.getBoundingClientRect().width,
        cols: getComputedStyle(document.getElementById('settings')!).gridTemplateColumns.split(' ').length }))
      if (got.w > 1041 || got.cols !== 2) throw new Error(JSON.stringify(got))
      await goTo('chat')
    })
    await check('the hash picks the view, Back walks back through them, anything else is the chat, and a notification\'s click opens it', async () => {
      const at = () => page.evaluate(() => ({ view: document.body.dataset.view, hash: location.hash,
        shown: ['chat', 'stats', 'settings'].filter(v => getComputedStyle(document.getElementById('view-' + v)!).display !== 'none'),
        current: [...document.querySelectorAll<HTMLElement>('#tabbar .tab[aria-current="page"]')].map(b => b.dataset.view),
        title: document.title, h1: [...document.querySelectorAll('h1')].filter(h => h.closest('.view')!.id === 'view-' + document.body.dataset.view).map(h => h.textContent) }))
      // #1339: the tab's title and the view's (hidden) heading name the view too
      const NAMES: Record<string, string> = { chat: 'Chat', stats: 'Stats', settings: 'Settings' }
      const is = async (v: string) => until(async () => { const a = await at(); return a.view === v && a.shown.join() === v && a.current.join() === v
        && a.title === `${NAMES[v]} · Workbench` && a.h1.join() === NAMES[v] })
        .catch(async () => { throw new Error(JSON.stringify(await at())) })
      await goTo('stats'); await is('stats')
      if ((await at()).hash !== '#stats') throw new Error(JSON.stringify(await at()))
      await goTo('settings'); await is('settings')
      await page.evaluate(() => history.back()); await is('stats')
      await page.evaluate(() => history.back()); await is('chat')
      await page.evaluate(() => { location.hash = 'settings' }); await is('settings')
      await page.evaluate(() => { location.hash = 'nowhere' }); await is('chat')
      await page.reload(); await is('chat')                // #nowhere is the chat still
      await page.evaluate(() => { location.hash = 'stats' }); await is('stats')
      await page.reload(); await is('stats')               // a reload stays on its view
      // the worker's message on a notification's click (sw.js: checked in node below)
      await page.evaluate(() => navigator.serviceWorker.dispatchEvent(new MessageEvent('message', { data: { kind: 'view', view: 'chat' } })))
      await is('chat')
      if ((await at()).hash !== '#chat') throw new Error(JSON.stringify(await at()))
    })
    await check('the Chat tab counts what Claude sends and the lines that it waits in the terminal while another view is shown, and clears on Chat', async () => {
      const badge = () => page.evaluate(() => {
        const b = document.querySelector<HTMLElement>('#tabbar .tab-badge')!, t = b.closest('.tab')!
        return { n: b.hidden ? '' : b.textContent, label: t.getAttribute('aria-label') }
      })
      await goTo('stats')
      wb('chat', 'reply', '--text', 'Badge one.')
      wb('chat', 'reply', '--text', 'Badge two.')
      const id = wb('chat', 'notice', '--tool', 'Bash', '--text', 'make badge').trim()
      await until(async () => (await badge()).n === '3').catch(async () => { throw new Error(JSON.stringify(await badge())) })
      if ((await badge()).label !== 'Chat, 3 new') throw new Error(JSON.stringify(await badge()))
      if (!(await page.locator('#tabbar .tab-badge').isVisible())) throw new Error('counted but not shown')
      await goTo('settings')                               // still away from the thread: still counted
      if ((await badge()).n !== '3') throw new Error(JSON.stringify(await badge()))
      await goTo('chat')
      await until(async () => (await badge()).n === '' && (await badge()).label === 'Chat')
      await until(async () => (await page.locator('.row.claude .bubble').last().textContent()).includes('Badge two.'))
      wb('chat', 'notice', '--done', id)                    // settled in the terminal: the line clears
      await until(async () => await page.locator('#driver-state').isHidden())
      wb('chat', 'reply', '--text', 'Seen on Chat.')       // shown: no count
      await until(async () => (await page.locator('.row.claude .bubble').last().textContent()).includes('Seen on Chat.'))
      if ((await badge()).n !== '') throw new Error('counted on Chat: ' + JSON.stringify(await badge()))
    })
    if (!desktop) await check('the bar steps aside while the keyboard is up, so the composer sits on the keyboard', async () => {
      // no browser here raises a keyboard: the visual viewport made short by hand, as one does
      const kb = (up: boolean) => page.evaluate((up: boolean) => {
        const vv = visualViewport
        if (up) { const h = innerHeight - 300; Object.defineProperty(vv!, 'height', { get: () => h, configurable: true }) }
        else delete (vv as any).height
        vv!.dispatchEvent(new Event('resize'))
      }, up)
      const at = () => page.evaluate(() => ({ kb: document.body.classList.contains('kb-open'), h: innerHeight - 300,
        nav: getComputedStyle(document.getElementById('tabbar')!).display, foot: document.getElementById('composer')!.getBoundingClientRect().bottom }))
      try {
        await kb(true)
        await until(async () => { const a = await at(); return a.kb && a.nav === 'none' && Math.abs(a.foot - a.h) <= 2 })
          .catch(async () => { throw new Error(JSON.stringify(await at())) })
        // and the hint with it (#1339)
        if (await page.evaluate(() => getComputedStyle(document.getElementById('hint')!).display) !== 'none') throw new Error('the hint stayed up')
      } finally { await kb(false) }
      await until(async () => { const a = await at(); return !a.kb && a.nav !== 'none' })
    })
    if (!desktop && engine === 'chromium') await check('on Android, where the whole page shrinks with the keyboard, the bar steps aside too (#1414)', async () => {
      // #1414 (2026-11-25): under interactive-widget=resizes-content Android's Chrome shrinks the
      // layout viewport with the visual one, so their difference never said a keyboard was up, and
      // the bar stayed between the composer and the keyboard. The page made short, as it does
      const vp = page.viewportSize()
      const at = () => page.evaluate(() => ({ kb: document.body.classList.contains('kb-open'), h: innerHeight,
        nav: getComputedStyle(document.getElementById('tabbar')!).display, foot: document.getElementById('composer')!.getBoundingClientRect().bottom }))
      await page.focus('#input')
      try {
        await page.setViewportSize({ width: vp.width, height: vp.height - 300 })
        await until(async () => { const a = await at(); return a.kb && a.nav === 'none' && Math.abs(a.foot - a.h) <= 2 })
          .catch(async () => { throw new Error(JSON.stringify(await at())) })
      } finally { await page.setViewportSize(vp) }
      await page.evaluate(() => document.getElementById('input')!.blur())
      await until(async () => { const a = await at(); return !a.kb && a.nav !== 'none' })
    })
    await check('window.WB gives the views the key, the API, the view and its changes, the state and a toast', async () => {
      const got = await page.evaluate(async () => {
        const seen: string[] = []
        window._states = 0
        WB.on('view', (v: string) => seen.push(v))
        WB.on('state', (s: any) => { if (Array.isArray(s.messages)) window._states++ })
        const keys = Object.keys(WB).sort().join()
        const r = await WB.api('/api/state'), state = await r.json()
        const p = await WB.api('/api/seen', { method: 'POST', body: { ids: [] } })   // a JSON body, its type set
        const q = await WB.post('/api/seen', { ids: [] })                             // under POST_MS (#1391)
        location.hash = 'settings'
        for (let i = 0; i < 100 && WB.current() !== 'settings'; i++) await new Promise(r => setTimeout(r, 50))
        WB.toast('Saved.')
        const t = document.getElementById('toast')!
        return { keys, status: r.status, messages: Array.isArray(state.messages), posted: p.status, timed: [q.status, JSON.stringify(await q.json())], cur: WB.current(), seen,
          token: WB.token().length > 20, toast: !t.hidden && t.textContent }
      })
      if (JSON.stringify(got) !== JSON.stringify({ keys: 'api,current,on,post,toast,token,view', status: 200, messages: true, posted: 200, timed: [200, '{}'],
        cur: 'settings', seen: ['settings'], token: true, toast: 'Saved.' })) throw new Error(JSON.stringify(got))
      const was = await page.evaluate(() => window._states)
      wb('chat', 'reply', '--text', 'A state for the views.')
      await until(async () => await page.evaluate(() => window._states) > was)
      await goTo('chat')
    })
    const sh = { engine, device, desktop, base, token, home, env, wb, until, check, sleep, goTo, context: ctx, browser }
    await statsView.check(page, sh)
    await settingsView.check(page, sh)
    await goTo('chat')
  }
}

// The worker's notification text, run in node against a given state (no browser shows a push)
async function swSays(state: object) {
  const ctx: any = { self: { addEventListener() {} }, caches: { open: async () => ({ match: async () => null }) },
    fetch: async (url: string) => { if (url !== '/api/latest') throw new Error('fetched ' + url); return { ok: true, json: async () => state } }, Date, Math, String }
  runInNewContext(readFileSync(join(ROOT, 'workbench/chat_static/sw.js'), 'utf8'), ctx)
  return ctx.what()
}
await check('a push for a prompt waiting in the terminal says so, with the command as it is (#959)', async () => {
  const text = 'Claude is waiting for you in the terminal. Run it with permissions granted to work from the chat.'
  const note = { id: 'n1', ts: 250, from: 'notice', tool: 'Bash', what: 'rm -rf runs/*', text }
  const got = await swSays({ now: 300, push: {}, messages: [note] })
  if (got.title !== text || got.body !== 'rm -rf runs/*') throw new Error(JSON.stringify(got))
  const reply = await swSays({ now: 300, push: {}, messages: [note, { id: 'r1', ts: 260, from: 'claude', text: 'Tests pass' }] })
  if (reply.body !== 'Tests pass') throw new Error(JSON.stringify(reply))
})

await check("a push's title names the driver: a reply's own `driver`, else the listener's, else Claude (#1319)", async () => {
  const oc = { id: 'r2', ts: 260, from: 'claude', text: 'Done', driver: { tool: 'opencode', model: 'p/m' } }
  const titles = [
    (await swSays({ now: 300, push: {}, messages: [oc] })).title,
    (await swSays({ now: 300, push: {}, messages: [{ id: 'r3', ts: 260, from: 'claude', text: 'Done' }], listener: { tool: 'opencode', driver: { tool: 'opencode' } } })).title,
    (await swSays({ now: 300, push: {}, messages: [{ id: 'r4', ts: 260, from: 'claude', text: 'Done', driver: { tool: 'claude' } }], listener: { driver: { tool: 'opencode' } } })).title,
    (await swSays({ now: 300, push: {}, messages: [{ id: 'r5', ts: 260, from: 'claude', text: 'Done' }], listener: null })).title,
    (await swSays({ now: 300, push: {}, messages: [], listener: { driver: { tool: 'opencode' } } })).title,
  ]
  if (JSON.stringify(titles) !== JSON.stringify(['OpenCode', 'OpenCode', 'Claude', 'Claude', 'OpenCode'])) throw new Error(JSON.stringify(titles))
  const test = await swSays({ now: 300, push: { test: 299 }, messages: [], listener: { driver: { tool: 'opencode' } } })
  if (test.body !== "You'll hear from OpenCode here while you're away.") throw new Error(JSON.stringify(test))
})

await check("a notification's click brings the thread: the open page told to show Chat, else a new one at #chat (#1284)", async () => {
  const on: Record<string, (e: any) => void> = {}, sent: unknown[] = [], opened: string[] = []
  let focused = 0
  const ctx = { self: { addEventListener: (ev: string, fn: (e: any) => void) => { on[ev] = fn }, location: { origin: 'http://x' },
    clients: { matchAll: async () => wins, openWindow: async (u: string) => { opened.push(u) } } }, caches: {}, URL, Date, Math, String }
  let wins = [{ url: 'http://x/#stats', postMessage: (m: unknown) => sent.push(m), focus: async () => { focused++ } }]
  runInNewContext(readFileSync(join(ROOT, 'workbench/chat_static/sw.js'), 'utf8'), ctx)
  const click = async () => { let p; on.notificationclick({ notification: { close() {} }, waitUntil: (x: unknown) => { p = x } }); await p }
  await click()
  if (JSON.stringify(sent) !== '[{"kind":"view","view":"chat"}]' || focused !== 1 || opened.length) throw new Error(JSON.stringify({ sent, focused, opened }))
  wins = []
  await click()
  if (JSON.stringify(opened) !== '["/#chat"]') throw new Error(JSON.stringify(opened))
})

try {
  await run('webkit', 'iPhone 13')
  await run('chromium', 'Pixel 7')
  await run('chromium', DESKTOP)
} finally {
  if (server && server.exitCode === null) server.kill('SIGTERM')
  rmSync(home, { recursive: true, force: true })
}
console.log(failed ? `\n${failed} failed` : '\nall passed')
process.exit(failed ? 1 : 0)
