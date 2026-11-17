// The web chat end to end, in real browsers (WebKit as an iPhone, and Chromium): the page, the
// real `wb chat serve`, and `wb chat` on the Claude side. Run: node tests/e2e_chat.mjs
// Needs Playwright: PLAYWRIGHT=/path/to/node_modules/playwright (it isn't a workbench dependency).
import { spawn, execFileSync } from 'node:child_process'
import { mkdtempSync, writeFileSync, readFileSync, existsSync, rmSync } from 'node:fs'
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
  writeFileSync(join(home, 'state/chat/listener.json'), '{}')
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
    await until(async () => (await page.locator('.row.user .meta').last().textContent()).includes('delivered'))
    if (await page.inputValue('#input')) throw new Error('the box was not cleared')
  })
  await check('a receipt shows as read', async () => {
    wb('chat', 'ack', messages()[0].id, '--by', 's1')
    await until(async () => (await page.locator('.row.user .meta').last().textContent()).includes('read by Claude'))
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
