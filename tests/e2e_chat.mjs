// The web chat end to end, in real browsers (WebKit as an iPhone, and Chromium): the page, the
// real `wb chat serve`, and `wb chat` on the Claude side. Run: node tests/e2e_chat.mjs
// Needs Playwright: PLAYWRIGHT=/path/to/node_modules/playwright (it isn't a workbench dependency).
import { spawn, execFileSync } from 'node:child_process'
import { mkdtempSync, mkdirSync, writeFileSync, appendFileSync, copyFileSync, readFileSync, existsSync, rmSync, statSync, utimesSync } from 'node:fs'
import { createRequire } from 'node:module'
import { tmpdir } from 'node:os'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')
const pw = createRequire(import.meta.url)(process.env.PLAYWRIGHT || 'playwright')
const home = mkdtempSync(join(tmpdir(), 'wbchat-'))
const env = { ...process.env, WB_HOME: join(home, 'state'), WB_CHAT_TOKEN: join(home, 'token') }
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
    const shown = JSON.parse(readFileSync(join(home, 'state/chat/messages.jsonl'), 'utf8').trim().split('\n').filter(l => l.includes('"claude"')).at(-1)).id
    await until(() => existsSync(join(home, 'state/chat/seen.jsonl')) && readFileSync(join(home, 'state/chat/seen.jsonl'), 'utf8').includes(shown))
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
  await check('no script errors', async () => { if (errors.length) throw new Error(errors.join('; ')) })
  await browser.close()
  await stopServer()
}

try {
  await run('webkit', 'iPhone 13')
  await run('chromium', 'Pixel 7')
} finally {
  if (server && server.exitCode === null) server.kill('SIGTERM')
  rmSync(home, { recursive: true, force: true })
}
console.log(failed ? `\n${failed} failed` : '\nall passed')
process.exit(failed ? 1 : 0)
