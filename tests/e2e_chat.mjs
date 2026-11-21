// The web chat end to end, in real browsers (WebKit as an iPhone, and Chromium): the page, the
// real `wb chat serve`, and `wb chat` on the Claude side. Run: node tests/e2e_chat.mjs
// Needs Playwright: PLAYWRIGHT=/path/to/node_modules/playwright (it isn't a workbench dependency).
import { spawn, execFileSync } from 'node:child_process'
import { mkdtempSync, mkdirSync, writeFileSync, appendFileSync, copyFileSync, readFileSync, readdirSync, existsSync, rmSync, statSync, utimesSync, chmodSync } from 'node:fs'
import { createRequire } from 'node:module'
import { createServer, connect as dial } from 'node:net'
import { runInNewContext } from 'node:vm'
import { tmpdir } from 'node:os'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')
const pw = createRequire(import.meta.url)(process.env.PLAYWRIGHT || 'playwright')
const home = mkdtempSync(join(tmpdir(), 'wbchat-'))
const env = { ...process.env, WB_HOME: join(home, 'state'), WB_CHAT_TOKEN: join(home, 'token'),
  WB_CHAT_CONFIG: join(home, 'chat.json'), WB_SAVINGS: join(home, 'savings.json') }  // not the user's
// a system Chromium when there is one (CHROMIUM names another), else Playwright's own
const CHROMIUM = process.env.CHROMIUM || (existsSync('/usr/bin/chromium') ? '/usr/bin/chromium' : '')
const PORT = Number(process.env.WB_E2E_PORT || 13371)
const wb = (...args) => execFileSync(join(ROOT, 'bin', 'wb'), args, { env, encoding: 'utf8' })
const sleep = ms => new Promise(r => setTimeout(r, ms))

let server
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
const listen = busy => writeFileSync(join(home, 'state/chat/listener.json'),
  JSON.stringify({ session: 's1', cwd: '/home/u/project', pid: process.pid, busy }))

let failed = 0
async function check(name, fn) {
  try { await fn(); console.log('ok   ' + name) }
  catch (e) { failed++; console.log('FAIL ' + name + '\n     ' + String(e.message || e).split('\n')[0]) }
}
const until = async (fn, ms = 8000) => {
  const end = Date.now() + ms
  for (;;) {
    try { if (await fn()) return } catch {}
    if (Date.now() > end) throw new Error('timed out: ' + fn.toString().slice(0, 120))
    await sleep(100)
  }
}

async function run(engine, device) {
  console.log(`\n== ${engine}${device ? ' as ' + device : ''}`)
  wb('chat', 'log')  // makes the chat directory
  wb('chat', 'url')  // and the key
  writeFileSync(join(home, 'state/chat/messages.jsonl'), '')
  writeFileSync(join(home, 'state/chat/receipts.jsonl'), '')
  writeFileSync(join(home, 'state/chat/asks.jsonl'), '')
  writeFileSync(join(home, 'state/chat/listener.json'), '{}')
  rmSync(join(home, 'state/chat/open.json'), { force: true })  // the last engine's unread, rebuilt from the files
  writeFileSync(join(home, 'state/results.jsonl'), '')
  const token = readFileSync(env.WB_CHAT_TOKEN, 'utf8').trim()
  startServer(); await sleep(800)
  const browser = await pw[engine].launch(engine === 'chromium' && CHROMIUM ? { executablePath: CHROMIUM } : {})
  const ctx = await browser.newContext(device ? pw.devices[device] : {})
  const page = await ctx.newPage()
  const errors = []
  page.on('pageerror', e => errors.push(String(e)))
  const base = `http://localhost:${PORT}/`

  await check('opens with the key, keeps it, drops it from the address', async () => {
    await page.goto(base + '#token=' + token)
    await until(async () => (await page.textContent('#presence-text')).includes('no session listening'))
    if (page.url().includes('token')) throw new Error('token still in the URL: ' + page.url())
  })
  await check('shows a listening session at once', async () => {
    listen(false)
    await until(async () => (await page.textContent('#presence-text')).includes('listening in ~/project'))
  })
  await check('a sent message lands once, with its tick', async () => {
    await page.fill('#input', 'hello from the phone')
    await page.click('#send')
    await until(() => messages().length === 1)
    await until(async () => (await page.locator('.row.user .meta').last().textContent()).includes('Delivered'))
    if (await page.inputValue('#input')) throw new Error('the box was not cleared')
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
        if (cmd === 'copy') window.copied.push(el && el.value !== undefined ? el.value.slice(el.selectionStart, el.selectionEnd) : String(getSelection()))
        return real(cmd, ...rest)
      }
      // what the button said: a state event redraws the row (and the button) at once
      window.said = []
      addEventListener('click', e => { const t = e.target; if (t.tagName === 'BUTTON') setTimeout(() => window.said.push(t.textContent), 50) }, true)
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
  await check('a reload shows the same thread, nothing doubled, still keyed', async () => {
    await page.reload()
    await until(async () => (await page.locator('.row').count()) === 4)
    const texts = await page.locator('.row .bubble').allTextContents()
    if (new Set(texts).size !== texts.length) throw new Error('duplicates: ' + texts.join(' | '))
  })
  await check('the app installs only over https: no manifest over localhost, a keyed one over https', async () => {
    // a localhost install beside the tailnet's left two apps of one name (2026-11-18)
    if (await page.locator('link[rel=manifest]').count()) throw new Error('a manifest over http://localhost')
    const sec = await browser.newContext(device ? pw.devices[device] : {})
    await sec.route('https://chat.test/**', async r => {  // an https origin, served by the real server
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
    await ctx.route('**/api/**', r => r.abort())
    await page.reload()
    await until(async () => (await page.locator('.row').count()) === 4)
    if (!(await page.textContent('#presence-text')).includes('connecting')) throw new Error(await page.textContent('#presence-text'))
    await ctx.unroute('**/api/**')
    await until(async () => (await page.textContent('#presence-text')).includes('listening'), 40000)
  })
  await check('a permission prompt shows Allow and Deny, and a tap answers it once', async () => {
    const id = wb('chat', 'ask', '--tool', 'Bash', '--text', 'git push origin main').split(' ')[0]
    await until(async () => (await page.locator('.row.ask .choices button').count()) === 2)
    const card = await page.locator('.row.ask .card').last().textContent()
    if (!card.includes('Claude wants to use Bash') || !card.includes('git push origin main')) throw new Error(card)
    const box = await page.locator('.row.ask .allow').boundingBox()
    if (box.height < 44) throw new Error(`Allow is ${box.height}px tall: too small to tap`)
    await page.click('.row.ask .allow')
    await until(() => wb('chat', 'wait', id, '--timeout', '0').trim() === 'allow')
    await until(async () => (await page.locator('.row.ask .meta').last().textContent()).includes('you allowed it'))
    if (await page.locator('.row.ask .choices').count()) throw new Error('the buttons are still there')
  })
  await check('a prompt whose purpose Claude gave leads with it, the command under it', async () => {
    // #103: the tool's name said less than the decision asked
    const id = wb('chat', 'ask', '--tool', 'Bash', '--text', 'make test-gpu', '--detail',
      JSON.stringify({ purpose: 'Run the GPU tests.' })).split(' ')[0]
    await until(async () => (await page.locator('.row.ask .choices').count()) === 1)
    const head = await page.locator('.row.ask .asks').last().textContent()
    if (head !== 'Allow Claude to run the GPU tests? (Bash)') throw new Error(head)
    if (!(await page.locator('.row.ask pre').last().textContent()).includes('make test-gpu')) throw new Error('no command')
    const [allow, deny] = await Promise.all(['.allow', '.deny'].map(c => page.locator('.row.ask .choices ' + c).last()
      .evaluate(b => getComputedStyle(b).backgroundColor)))
    if (allow !== deny) throw new Error(`Allow ${allow} and Deny ${deny} differ`)
    wb('chat', 'answer', id, 'deny', '--by', 'cli')
    await until(async () => (await page.locator('.row.ask .choices').count()) === 0)
  })
  await check('a prompt left unanswered says it waits in the terminal', async () => {
    const id = wb('chat', 'ask', '--tool', 'Write', '--text', '/etc/hosts').split(' ')[0]
    await until(async () => (await page.locator('.row.ask .choices').count()) === 1)
    wb('chat', 'answer', id, 'expired', '--by', 'timeout')
    await until(async () => (await page.locator('.row.ask .meta').last().textContent()).includes('waits in the terminal'))
    if (await page.locator('.row.ask .choices').count()) throw new Error('the buttons are still there')
  })
  await check('a plan shows folded, and Keep planning sends a note back', async () => {
    const plan = '# The plan\n\n' + Array.from({ length: 30 }, (_, i) => `${i + 1}. step ${i + 1}`).join('\n')
    const id = wb('chat', 'ask', '--tool', 'ExitPlanMode', '--kind', 'plan', '--detail', JSON.stringify({ plan })).split(' ')[0]
    await until(async () => (await page.locator('.row.ask').last().locator('.plan').count()) === 1)
    const last = page.locator('.row.ask').last()
    if (!(await last.locator('.plan.folded').count())) throw new Error('a long plan is not folded')
    await last.locator('.more').click()
    await last.locator('.choices .deny').click()            // opens the note
    await last.locator('.note').fill('split step 2')
    await last.locator('.choices .deny').click()            // sends it back
    await until(() => { const a = JSON.parse(wb('chat', 'wait', id, '--timeout', '0', '--json')); return a.answer === 'deny' && a.note === 'split step 2' })
    await until(async () => (await page.locator('.row.ask .meta').last().textContent()).includes('you asked for changes'))
  })
  await check('a settled plan comes cut, and Show the whole plan fetches the rest', async () => {
    // #755: thirty answered plans were most of a 922 KB state, sent on every change
    const plan = '# Long\n\n' + Array.from({ length: 400 }, (_, i) => `${i + 1}. step ${i + 1}`).join('\n') + '\n\nTHE END'
    const id = wb('chat', 'ask', '--tool', 'ExitPlanMode', '--kind', 'plan', '--detail', JSON.stringify({ plan })).split(' ')[0]
    await until(async () => (await page.locator('.row.ask').last().locator('.choices .deny').count()) === 1)
    wb('chat', 'answer', id, 'deny', '--by', 'cli')
    await until(async () => (await page.locator('.row.ask .meta').last().textContent()).includes('you asked for changes'))
    const last = page.locator('.row.ask').last()
    if ((await last.locator('.plan').textContent()).includes('THE END')) throw new Error('the whole plan came in the state')
    await last.locator('.more').click()
    await until(async () => (await last.locator('.plan').textContent()).includes('THE END'))
    if (await last.locator('.plan.folded, .more').count()) throw new Error('still folded')
  })
  await check("Claude's question: pick an option, answer", async () => {
    const questions = [{ question: 'Which DB?', header: 'DB', multiSelect: false,
      options: [{ label: 'SQLite', description: 'one file' }, { label: 'Postgres', description: 'a server' }] }]
    const id = wb('chat', 'ask', '--tool', 'AskUserQuestion', '--kind', 'question', '--detail', JSON.stringify({ questions })).split(' ')[0]
    await until(async () => (await page.locator('.row.ask').last().locator('.opt').count()) === 2)
    const card = page.locator('.row.ask').last()
    if (!(await card.locator('.choices .allow').isDisabled())) throw new Error('Answer is enabled with nothing picked')
    await card.locator('.opt', { hasText: 'SQLite' }).click()
    await card.locator('.choices .allow').click()
    await until(() => JSON.parse(wb('chat', 'wait', id, '--timeout', '0', '--json')).answers?.['Which DB?'] === 'SQLite')
    await until(async () => (await page.locator('.row.ask .picked').last().textContent()) === 'SQLite')
  })
  await check('the gear opens the settings; "Let Claude go ahead" is saved and allows the next prompt', async () => {
    await page.click('#gear')
    await until(async () => await page.locator('#settings').isVisible())
    if (!(await page.textContent('#settings')).includes('second lane')) throw new Error('no word on what this chat is')
    if (!(await page.textContent('#saved')).includes('Nothing to compare yet')) throw new Error('a saving shown with no task')
    await page.click('[data-away="allow"]')
    await until(() => JSON.parse(readFileSync(join(home, 'state/chat/prefs.json'), 'utf8')).away === 'allow')
    if (wb('chat', 'ask', '--tool', 'Bash', '--text', 'make').split(' ')[1].trim() !== 'allow') throw new Error('not allowed at once')
    await until(async () => (await page.locator('.row.ask .meta').last().textContent()).includes('allowed by your setting'))
    await page.click('[data-away="ask"]')
    await until(() => JSON.parse(readFileSync(join(home, 'state/chat/prefs.json'), 'utf8')).away === 'ask')
  })
  await check('the gear shows what the week\'s tasks saved Claude, as an estimate', async () => {
    const usage = { turns: 2, calls: 5, input: 1000, cache_write_5m: 0, cache_write_1h: 10000, cache_read: 200000, output: 3000 }
    writeFileSync(join(home, 'state/results.jsonl'), JSON.stringify({ ts: Date.now() / 1000, outcome: 'accepted',
      claude: usage, diff: { files: 4, added: 61, removed: 1 }, gpu_wh: 600 }) + '\n')
    await page.reload()
    await page.click('#gear')
    await until(async () => (await page.textContent('#saved')).includes('In the last 7 days, 1 task: Claude used 56K tokens'))
    const said = await page.textContent('#saved')
    if (!said.includes('about 59K (an estimate). Saved about 3K (5%). The GPU used 600 Wh.')) throw new Error(said)
    // #845: a price in the user's own currency showed as dollars on the phone
    writeFileSync(env.WB_SAVINGS, JSON.stringify({ kwh_price: 0.3 }))
    await page.click('#gear')
    await until(async () => await page.locator('#settings').isHidden())
    await page.reload()
    await page.click('#gear')
    await until(async () => (await page.textContent('#saved')).includes('The GPU used 600 Wh, cost 0.18.'))
    if ((await page.textContent('#saved')).includes('$')) throw new Error('a currency sign: ' + await page.textContent('#saved'))
    writeFileSync(env.WB_SAVINGS, '{}')  // no price again, for the next engine
    // #101: the badge, its number and its words, legible at phone width
    if (await page.locator('#badge').isHidden()) throw new Error('no badge')
    const n = await page.textContent('#badge-n'), cls = await page.getAttribute('#badge-n', 'class')
    if (n !== '5 / 100' || cls !== 'badge-n red') throw new Error(`${n} ${cls}`)
    const s = await page.textContent('#badge-s')
    if (s !== 'I used about 5% less Claude than if Claude had done this work alone.') throw new Error(s)
    const box = await page.locator('#badge').boundingBox()
    if (box.width > (await page.evaluate(() => innerWidth))) throw new Error('wider than the phone')
    await page.click('#gear')
    await until(async () => await page.locator('#settings').isHidden())
  })
  await check('the app: its worker runs, holds the key, and the gear says how to install it and get notifications', async () => {
    await until(() => page.evaluate(async () => !!(await navigator.serviceWorker.getRegistration())))
    await until(() => page.evaluate(async () => (await (await caches.open('wb-chat')).match('/key')) !== undefined))
    if (!(await page.textContent('#app')).includes('installs from the https address')) throw new Error('install offered over http')
    const notify = await page.textContent('#notify')
    if (engine === 'webkit' ? !notify.includes('installed app') : !(await page.locator('#push').count())) throw new Error('notifications: ' + notify)
    await page.keyboard.press('Escape')
    await until(async () => await page.locator('#settings').isHidden())
  })
  await check('what the page showed on screen is reported, so it is not pushed', async () => {
    await page.locator('.thread').click({ position: { x: 5, y: 5 } })  // in use: touched just now
    const shown = JSON.parse(readFileSync(join(home, 'state/chat/messages.jsonl'), 'utf8').trim().split('\n').filter(l => l.includes('"claude"')).at(-1)).id
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
    const socks = new Set()
    const proxy = createServer(c => {
      const up = dial(PORT, '127.0.0.1'), s = { c, up, dead: false }
      socks.add(s)
      c.on('data', d => { if (!s.dead) up.write(d) })
      up.on('data', d => { if (!s.dead) c.write(d) })
      for (const x of [c, up]) x.on('error', () => {}).on('close', () => { c.destroy(); up.destroy(); socks.delete(s) })
    })
    await new Promise(r => proxy.listen(PORT + 7, '127.0.0.1', r))
    const p = await ctx.newPage()
    p.on('pageerror', e => errors.push(String(e)))
    const hide = h => p.evaluate(h => {
      Object.defineProperty(document, 'hidden', { value: h, configurable: true })
      document.dispatchEvent(new Event('visibilitychange'))
    }, h)
    try {
      await p.goto(`http://localhost:${PORT + 7}/#token=${token}`)
      await until(async () => (await p.textContent('#presence-text')).includes('listening'))
      for (const [n, wake] of [[1, () => hide(false)],
                               [2, () => p.evaluate(() => navigator.serviceWorker.dispatchEvent(
                                 new MessageEvent('message', { data: { kind: 'push' } })))]]) {
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
  await check("wb shot's pair: both images labelled, one above the other on a phone, a reply tied to their step", async () => {
    const d = join(home, 'state/tasks/fix-nav/shots/1')
    mkdirSync(d, { recursive: true })
    for (const side of ['base', 'candidate']) copyFileSync(join(ROOT, 'workbench/chat_static/icon-192.png'), join(d, side + '.png'))
    const of = side => ({ rev: side === 'base' ? '1a2b3c4d5e6f' : '9f8e7d6c5b4a', image: join(d, side + '.png') })
    writeFileSync(join(d, 'shots.json'), JSON.stringify({ step: 1, round: 2, viewport: '390x844', base: of('base'), candidate: of('candidate') }))
    const shots = { task: 'fix-nav', n: 1, step: 1, round: 2, viewport: '390x844',
      base: { rev: '1a2b3c4d5e6f', image: true }, candidate: { rev: '9f8e7d6c5b4a', image: true } }
    appendFileSync(join(home, 'state/chat/messages.jsonl'), JSON.stringify({ id: 'shots-1', ts: Date.now() / 1000, from: 'claude',
      text: 'screenshots of fix-nav. Reply here to send feedback on it.', shots }) + '\n')
    await page.reload()
    await until(async () => (await page.$$eval('.shots img', im => im.filter(i => i.complete && i.naturalWidth > 0).length)) === 2)
    const caps = await page.$$eval('.shots figcaption', c => c.map(x => x.textContent))
    if (caps.join('|') !== 'Base · 1a2b3c4d5e|Candidate · 9f8e7d6c5b') throw new Error(caps.join('|'))
    const said = await page.textContent('.shots-of')
    if (!said.includes('fix-nav · step 1 · round 2 · 390x844')) throw new Error(said)
    const [a, b] = await page.$$eval('.shots figure', f => f.map(x => x.getBoundingClientRect().toJSON()))
    if (!(b.top >= a.bottom)) throw new Error('side by side on a phone')
    const width = await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)
    if (!width) throw new Error('the page scrolls sideways')
    await page.click('text=Reply about these')
    await until(async () => (await page.textContent('#about-text')) === 'Replying about fix-nav, step 1')
    await page.fill('#input', 'the menu overlaps the logo')
    await page.click('#send')
    await until(() => { const rows = messages(); const r = rows.at(-1); return r && r.text === 'the menu overlaps the logo' && r.about })
    const r = messages().at(-1)
    if (JSON.stringify(r.about) !== JSON.stringify({ task: 'fix-nav', n: 1, step: 1, round: 2 })) throw new Error(JSON.stringify(r.about))
    if (!(await page.locator('#about').isHidden())) throw new Error('still replying about them')
    await until(async () => (await page.$$eval('.about-tag', t => t.at(-1)?.textContent)) === 'About fix-nav, step 1')
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
    const expired = async () => (await page.$$eval('.shots .missing', m => m.map(x => x.textContent)))
      .filter(t => t.includes('This screenshot has expired')).length
    await until(async () => (await expired()) === 2)
  })
  await check("wb audio's shortlist: each take plays from its blob, with its checks, and Keep sends the pick (#397)", async () => {
    const d = join(home, 'takes'); mkdirSync(d, { recursive: true })
    const wav = (secs, rate = 8000) => {  // a 440 Hz tone, 16-bit mono PCM
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
    await until(async () => (await page.$$eval('.take audio', a => a.filter(x => x.src.startsWith('blob:') && x.readyState >= 1).length)) === 2)
    const secs = await page.$$eval('.take audio', a => a.map(x => Math.round(x.duration)))
    if (secs.join(',') !== '1,2') throw new Error('durations ' + secs.join(','))
    const caps = await page.$$eval('.take figcaption', c => c.map(x => x.textContent))
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
    await until(async () => (await page.$$eval('.about-tag', t => t.at(-1)?.textContent)) === 'Kept take 2 · a-door-slam-2.wav')
  })
  // the phone keyboard (2026-11-19): no browser here can raise one, so the touches are made by hand
  const touches = (moves, on = 'main') => page.evaluate(([moves, on]) => {
    const el = document.querySelector(on), r = el.getBoundingClientRect()
    const at = (y, x = r.left + r.width / 2) => new Touch({ identifier: 1, target: el, clientX: x, clientY: y, screenX: x, screenY: y })
    const fire = (type, t) => el.dispatchEvent(new TouchEvent(type, { bubbles: true, touches: t ? [t] : [], changedTouches: [t || at(moves.at(-1))] }))
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
    await page.evaluate(() => document.getElementById('input').blur())
    await sleep(1400)
    const at = await page.evaluate(() => ({ t: document.body.style.transform, h: document.body.style.height,
      foot: Math.round(document.getElementById('composer').getBoundingClientRect().bottom), vh: innerHeight, y: scrollY }))
    if (at.t || at.h || at.y || Math.abs(at.foot - at.vh) > 2) throw new Error(JSON.stringify(at))
  })
  await check('two tabs offline, both closed, reopened: each message lands exactly once', async () => {
    // #199 (the 2026-11-19 audit): each tab saved its own copy of one outbox array over the
    // other's, and only the second tab's message ever reached the server
    const a = await ctx.newPage(), b = await ctx.newPage()
    for (const t of [a, b]) { t.on('pageerror', e => errors.push(String(e))); await t.goto(base); await until(async () => await t.locator('#input').isEditable()) }
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
    c.on('pageerror', e => errors.push(String(e)))
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
    q.on('pageerror', e => errors.push(String(e)))
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
    await page.route('**/api/send', async route => {
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
  await check('a refused message about a pair of screenshots, edited, is still about them (#843)', async () => {
    // #843: edit put the words and files back in the box but not what they were about. The pair
    // again, newest: the first one may have left the thread's newest 200 by now
    const shots = { task: 'fix-nav', n: 1, step: 1, round: 2, viewport: '390x844',
      base: { rev: '1a2b3c4d5e6f', image: true }, candidate: { rev: '9f8e7d6c5b4a', image: true } }
    appendFileSync(join(home, 'state/chat/messages.jsonl'), JSON.stringify({ id: 'shots-again', ts: Date.now() / 1000, from: 'claude',
      text: 'screenshots of fix-nav again.', shots }) + '\n')
    await until(async () => (await page.locator('text=Reply about these').count()) > 0)
    await page.locator('text=Reply about these').last().click()
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
  await check('a photo attached, the page reloaded before sending: the chip is back, nothing uploaded twice; one message with one path reaches Claude', async () => {
    // #623, the owner (2026-11-20): never a file uploaded twice by a refresh
    const puts = []
    const count = r => { if (r.method() === 'PUT' && r.url().includes('/api/upload/')) puts.push(r.url()) }
    page.on('request', count)
    const png = readFileSync(join(ROOT, 'workbench/chat_static/icon-192.png'))
    const uploads = () => existsSync(join(home, 'state/chat/uploads')) ? readdirSync(join(home, 'state/chat/uploads')) : []
    const before = uploads().length
    await page.setInputFiles('#pick', { name: 'photo.png', mimeType: 'image/png', buffer: png })
    await until(async () => (await page.locator('#tray .att.done').count()) === 1)
    await page.fill('#input', 'what is this?')
    await page.reload()
    await until(async () => (await page.locator('#tray .att.done').count()) === 1)
    await until(() => page.$eval('#tray .face', i => i.complete && i.naturalWidth > 0))
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
    await until(() => page.$eval('.row.user .files .pic img', i => i.complete && i.naturalWidth > 0))
    await page.click('.row.user .files .pic')
    await until(async () => (await page.locator('.viewer img').count()) === 1)
    await page.click('.viewer')
    await until(async () => (await page.locator('.viewer').count()) === 0)
  })
  await check("the thread's photos are fetched as they scroll near the view, not all at each load", async () => {
    // #754 (the 2026-11-21 audit, finding 101): every photo in the last 200 messages was fetched
    // whole, uncached, on every load, to fill 132-pixel thumbnails
    const now = Date.now() / 1000
    for (let i = 0; i < 60; i++) appendFileSync(join(home, 'state/chat/messages.jsonl'), JSON.stringify({ id: `fill-${i}`,
      ts: now + i / 100, from: 'claude', text: `filler reply ${i}\n\n` + 'a line of words to make it tall\n'.repeat(4) }) + '\n')
    await page.reload()  // rows written behind the server's back: a load shows them (and keeps them)
    await until(async () => (await page.locator('.row.claude .bubble').last().textContent()).includes('filler reply 59'))
    const gets = []
    const count = r => { if (r.url().includes('/api/file/')) gets.push(r.url()) }
    page.on('request', count)
    try {
      await page.reload()
      await until(async () => (await page.locator('.row.claude .bubble').last().textContent()).includes('filler reply 59'))
      await sleep(1500)
      if (gets.length) throw new Error(`fetched ${gets.length} photo(s) far above the view`)
      await page.$eval('.row.user .files', f => f.scrollIntoView())
      await until(() => gets.length === 1)
      await until(() => page.$eval('.row.user .files .pic img', i => i.complete && i.naturalWidth > 0))
    } finally { page.off('request', count) }
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
      URL.createObjectURL = b => { const u = make(b); if (b instanceof File) window.live.add(u); return u }  // a picked file's, not the thread's
      URL.revokeObjectURL = u => { window.live.delete(u); drop(u) }
    })
    try {
      await p.goto(base)
      await until(async () => (await p.textContent('#presence-text')).length > 0)
      const png = readFileSync(join(ROOT, 'workbench/chat_static/icon-192.png'))
      for (let i = 0; i < 4; i++) {
        await p.setInputFiles('#pick', { name: `p${i}.png`, mimeType: 'image/png', buffer: Buffer.concat([png, Buffer.from([i])]) })
        await until(() => p.$eval('#tray .face', img => img.complete && img.naturalWidth > 0))
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
    const count = r => { if (r.method() === 'PUT' && r.url().includes('/api/upload/')) puts.push(r.url()) }
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
    await page.route('**/api/upload/*', route => {
      if (route.request().method() !== 'PUT') return route.continue()
      flaky.push(route.request().url())
      return route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ error: 'not saved: Input/output error' }) })
    })
    try {
      await page.setInputFiles('#pick', { name: 'flaky.txt', mimeType: 'text/plain', buffer: Buffer.from('flaky') })
      await until(async () => ((await page.locator('#tray .att.refused').textContent()) || '').includes('Input/output error'), 15000)
      await sleep(2500)
      if (flaky.length !== 3) throw new Error(`sent ${flaky.length} times`)
      await page.click('#tray .att .x')
      await until(async () => await page.locator('#tray').isHidden())
    } finally { await page.unroute('**/api/upload/*') }
  })
  await check('no script errors', async () => { if (errors.length) throw new Error(errors.join('; ')) })
  await browser.close()
  await stopServer()
}

// The worker's notification text, run in node against a given state (no browser shows a push)
async function swSays(state) {
  const ctx = { self: { addEventListener() {} }, caches: { open: async () => ({ match: async () => null }) },
    fetch: async url => { if (url !== '/api/latest') throw new Error('fetched ' + url); return { ok: true, json: async () => state } }, Date, Math, String }
  runInNewContext(readFileSync(join(ROOT, 'workbench/chat_static/sw.js'), 'utf8'), ctx)
  return ctx.what()
}
await check("a push shows Claude's newer reply over an older or stale prompt, and an open newer prompt over it", async () => {
  // #538 (the 2026-11-20 audit): a dead session's card beat every later reply in every push
  const ask = { id: 'ask-1', ts: 100, tool: 'Bash', text: 'rm -rf build', kind: 'permission', answer: null }
  const reply = { id: 'r1', ts: 200, from: 'claude', text: 'Tests pass, ready for review' }
  const said = async s => (await swSays({ now: 300, push: {}, ...s })).body
  if (await said({ asks: [ask], messages: [reply] }) !== 'Tests pass, ready for review') throw new Error('the older prompt won')
  if (await said({ asks: [{ ...ask, ts: 250, stale: true }], messages: [reply] }) !== 'Tests pass, ready for review') throw new Error('a stale prompt won')
  if (await said({ asks: [{ ...ask, ts: 250 }], messages: [reply] }) !== 'rm -rf build') throw new Error('the newer prompt lost')
})

try {
  await run('webkit', 'iPhone 13')
  await run('chromium', 'Pixel 7')
} finally {
  if (server && server.exitCode === null) server.kill('SIGTERM')
  rmSync(home, { recursive: true, force: true })
}
console.log(failed ? `\n${failed} failed` : '\nall passed')
process.exit(failed ? 1 : 0)
