// The Settings view's checks in a real browser (#1286). e2e_chat.mjs calls check(page, ctx) once
// per device, on a page with the key and the server up; ctx holds { engine, device, desktop, base,
// token, home, env, wb, until, check, sleep, goTo, context, browser } (goTo('settings') taps the
// tab and waits for the view). Leave the page where it was.
//
// The rows come from the server's GET /api/settings and a change goes to its POST (#1280). Until
// that endpoint is in the server (it answers 404), a stand-in routes the page's calls to the
// same table in Python, settings.rows() and settings.apply() on this run's WB_HOME, with #1280's
// shapes and statuses, so a click still runs the real wb and writes the real file.
import { execFile } from 'node:child_process'
import { readFileSync, existsSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')
const bound = new WeakSet()                         // pages given __wbSettings (a binding is made once a page)
const STAND_IN = `
import json, sys, time
from workbench import settings
req = json.loads(sys.argv[1])
if req is None:
    out = 200, {"rows": settings.rows(), "ts": time.time()}
else:
    try:
        r = settings.apply(req["key"], req["value"])
    except KeyError:
        out = 400, {"error": "no such setting"}
    except ValueError:
        out = 400, {"error": "not one of its choices"}
    else:
        rows = settings.rows(fresh=True)
        out = (200, {"ok": True, "said": r["said"], "rows": rows, **({"help": r["help"]} if r.get("help") else {})}) \\
            if r["ok"] else (409, {"ok": False, "error": r["said"], "rows": rows})
print(json.dumps(out))
`
const standIn = (env, req) => new Promise((resolve, reject) =>
  execFile(process.env.PYTHON || 'python3', ['-c', STAND_IN, JSON.stringify(req)], { cwd: ROOT, env, encoding: 'utf8', timeout: 60000 },
    (err, out, stderr) => err ? reject(new Error(String(stderr || err).trim().split('\n').pop())) : resolve(JSON.parse(out))))

// rows of every kind, for the checks the machine's own table can't promise (an API row: only with a provider)
const MOCK_ROWS = [
  { key: 'coding', group: 'shared', label: 'Claude coding', kind: 'choice', value: 'decide', other: false, says: 'Claude uses its judgment.',
    choices: [{ value: 'only', label: 'Only when I say', says: 'You tell Claude.' }, { value: 'ask', label: 'Ask me first', says: 'Claude asks.' },
      { value: 'decide', label: 'Let Claude decide (default)', says: 'Claude uses its judgment.' }] },
  { key: 'workflows', group: 'shared', label: 'Dynamic workflows', kind: 'choice', value: 'off', other: false, says: 'One task at a time.',
    choices: [{ value: 'off', label: 'Off (default)', says: 'One task at a time.' }, { value: 'on', label: 'On', says: 'Tasks at once.' }] },
  { key: 'cap', group: 'shared', label: 'API day cap', kind: 'number', value: 5, unit: 'usd', says: 'API tasks stop for the day at $5.' },
  { key: 'gpu-none', group: 'gpu', label: '', kind: 'note', text: 'No GPU model found (wb models)' },
  // a choice beside the note, as settings.py sends it (the media rows after the no-GPU line): both drawn
  { key: 'media-images', group: 'gpu', label: 'Image generation', kind: 'choice', value: 'off', other: false, says: 'No images.',
    choices: [{ value: 'off', label: 'Off (default)', says: 'No images.' }, { value: 'on', label: 'On', says: 'Images.' }] },
  { key: 'api-connected', group: 'api', label: 'Connected', kind: 'info', text: 'Connected: Provider (key stored)' },
  { key: 'api-writing', group: 'api', label: 'English writing API model', kind: 'choice', value: null, other: true,
    says: 'English writing has no API model: it runs on the GPU.',
    choices: [{ value: 'prov/a', label: 'A model whose name runs long · A provider with a long name too (1049K) · $0.27/$1.10 per M · free',
      says: 'English writing uses prov/a.' }] },
  { key: 'splash', group: 'terminal', label: 'Opening animation', kind: 'choice', value: 'on', other: false, says: 'An animation.',
    choices: [{ value: 'on', label: 'Enabled (default)', says: 'An animation.' }, { value: 'off', label: 'Off', says: 'At once.' }] },
]

export async function check(page, ctx) {
  const { base, token, env, home, until, goTo, desktop } = ctx
  const view = () => page.locator('#settings-rows')
  const row = key => page.locator(`#settings-rows .set-row[data-key="${key}"]`)
  const toast = () => page.evaluate(() => { const t = document.getElementById('toast'); return t.hidden ? '' : t.textContent })
  // shown again, from nothing: the waits below see only what this showing's GET drew
  const reshow = async () => {
    await goTo('chat')
    await page.evaluate(() => document.getElementById('settings-rows').replaceChildren())
    await goTo('settings')
  }

  const probe = await fetch(base + 'api/settings', { headers: { Authorization: 'Bearer ' + token } })
  const real = probe.status === 200
  console.log(`     (the settings rows from ${real ? "the server's /api/settings" : 'a stand-in for /api/settings: settings.py on this WB_HOME'})`)
  // The page's calls to /api/settings answered here: each handler (method, body) gives [status,
  // json] or null for the next, the newest first, and null from all goes to the server. In the
  // page, not through page.route or context.route: the page is its service worker's client, and
  // WebKit sends such a page's fetches past both (every stand-in call there got the server's 404).
  const handlers = []
  if (!real) handlers.push((method, body) => standIn(env, method === 'POST' ? body : null))
  if (!bound.has(page)) {
    bound.add(page)
    await page.exposeBinding('__wbSettings', async (_, method, body) => {
      for (let i = handlers.length - 1; i >= 0; i--) {
        try { const out = await handlers[i](method, body); if (out) return out }
        catch (e) { return [500, { error: String(e.message || e) }] }
      }
      return null
    })
  }
  await page.evaluate(() => {
    if (window.__wbFetch) return
    const real = window.__wbFetch = window.fetch
    window.fetch = async (url, init = {}) => {
      if (new URL(String(url), location.href).pathname !== '/api/settings') return real(url, init)
      const method = (init.method || 'GET').toUpperCase()
      // a hung answer ends as a real fetch's does, at its signal's abort (#1391)
      const out = await new Promise((resolve, reject) => {
        if (init.signal) init.signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')), { once: true })
        window.__wbSettings(method, init.body ? JSON.parse(init.body) : null).then(resolve, reject)
      })
      if (!out) return real(url, init)
      return new Response(JSON.stringify(out[1]), { status: out[0], headers: { 'Content-Type': 'application/json' } })
    }
  })
  const mocks = []                                        // each check's own handler, taken off after it
  const mock = fn => { mocks.push(fn); handlers.push(fn) }
  const unmock = () => { for (const fn of mocks.splice(0)) handlers.splice(handlers.indexOf(fn), 1) }
  const answer = (status, body) => [status, body]
  const fits = async width => {
    const got = await page.evaluate(() => {
      const v = document.getElementById('view-settings'), w = v.getBoundingClientRect()
      const out = [...document.querySelectorAll('#settings-rows .set-card, #settings-rows .set-row > *, #settings-rows .seg button')]
        .map(e => ({ e, r: e.getBoundingClientRect() }))
        .filter(({ r }) => r.width && (r.left < w.left - 1 || r.right > w.right + 1))
        .map(({ e, r }) => `${e.className || e.tagName} ${Math.round(r.left)}..${Math.round(r.right)}`)
      return { width: innerWidth, out, scroll: v.scrollWidth - v.clientWidth, page: document.documentElement.scrollWidth - innerWidth,
        cols: getComputedStyle(document.querySelector('#settings-rows .set-cards')).gridTemplateColumns.split(' ').length }
    })
    if (got.width !== width || got.out.length || got.scroll > 0 || got.page > 0) throw new Error(JSON.stringify(got))
    return got
  }
  try {
    await ctx.check('Settings lists the admin rows above the device cards, the shared group first as "Agents and roles"', async () => {
      await reshow()
      await until(async () => (await page.locator('#settings-rows .set-card[data-group]').count()) > 0)
        .catch(async () => { throw new Error('no rows: ' + await view().textContent()) })
      const got = await page.evaluate(() => ({
        groups: [...document.querySelectorAll('#settings-rows .set-card')].map(c => c.dataset.group + ':' + c.querySelector('h3').textContent),
        above: !!(document.getElementById('settings-rows').compareDocumentPosition(document.getElementById('settings')) & Node.DOCUMENT_POSITION_FOLLOWING),
        coding: !!document.querySelector('#settings-rows .set-card[data-group="shared"] .set-row[data-key="coding"] [role="radio"]') }))
      if (got.groups[0] !== 'shared:Agents and roles' || !got.coding || !got.above) throw new Error(JSON.stringify(got))
      const order = ['shared', 'gpu', 'api', 'terminal', 'experimental']
      const idx = got.groups.map(g => order.indexOf(g.split(':')[0]))
      if (idx.some((v, i) => v < 0 || (i && v <= idx[i - 1]))) throw new Error('the groups out of order: ' + got.groups.join())
    })

    await ctx.check('choosing Claude coding → "Ask me first" lights at once, runs wb coding ask and writes coding.json; the toast is wb\'s line', async () => {
      const file = join(home, 'state', 'coding.json')
      const mode = () => existsSync(file) ? JSON.parse(readFileSync(file, 'utf8')).mode : undefined
      const ask = row('coding').locator('[role="radio"][data-value="ask"]')
      await ask.click()
      if ((await ask.getAttribute('aria-checked')) !== 'true') throw new Error('not lit at once')
      await until(() => mode() === 'ask').catch(() => { throw new Error('coding.json: ' + mode()) })
      await until(async () => (await toast()).length > 0)
      await until(async () => (await row('coding').locator('[aria-checked="true"]').getAttribute('data-value')) === 'ask')
      if (!ctx.wb('coding').startsWith('Claude coding: Ask me first.')) throw new Error('wb coding disagrees: ' + ctx.wb('coding'))
      await row('coding').locator('[role="radio"][data-value="decide"]').click()  // as it was
      await until(() => mode() === 'decide')
    })

    await ctx.check('a refused change (409) puts the control back and toasts wb\'s refusal; one request at a time, the next click waits', async () => {
      let rows = MOCK_ROWS, inFlight = 0, most = 0, posts = []
      mock(async (method, body) => {
        if (method !== 'POST') return answer(200, { rows, ts: 1 })
        posts.push(body)
        inFlight++; most = Math.max(most, inFlight)
        await new Promise(res => setTimeout(res, 400))
        inFlight--
        if (body.key === 'workflows') return answer(409, { ok: false, error: 'wb refused: workflows are off for this test', rows })
        rows = rows.map(x => x.key === body.key ? { ...x, value: body.value } : x)
        return answer(200, { ok: true, said: 'splash: off', rows })
      })
      await reshow()
      await until(async () => (await row('workflows').count()) === 1)
      const on = row('workflows').locator('[data-value="on"]')
      await on.click()
      if ((await on.getAttribute('aria-checked')) !== 'true') throw new Error('not lit at once')
      await row('splash').locator('[data-value="off"]').click()                    // made meanwhile: it waits
      await until(async () => (await toast()).includes('wb refused: workflows are off for this test'))
      await until(async () => (await row('workflows').locator('[aria-checked="true"]').getAttribute('data-value')) === 'off')
      await until(() => posts.length === 2)
      await until(async () => (await row('splash').locator('[aria-checked="true"]').getAttribute('data-value')) === 'off')
      if (most !== 1 || posts.map(p => p.key).join() !== 'workflows,splash') throw new Error(JSON.stringify({ most, posts }))
    })

    await ctx.check('a change whose POST never answers is given up after POST_MS: the toast says so, the next change goes, and the control shows the computer\'s value (#1391)', async () => {
      // #1391: with no deadline the hung change stayed lit as applied, and every later click
      // queued behind it, never sent until a reload
      let rows = MOCK_ROWS, release = null
      const posts = []
      unmock()
      mock(async (method, body) => {
        if (method !== 'POST') return answer(200, { rows, ts: 1 })
        posts.push(body)
        if (body.key === 'workflows') { await new Promise(res => { release = res }); return answer(200, { ok: true, said: 'late', rows }) }
        await new Promise(res => setTimeout(res, 1500))  // the lost one's toast stays up to be read
        rows = rows.map(x => x.key === body.key ? { ...x, value: body.value } : x)
        return answer(200, { ok: true, said: 'images: on', rows })
      })
      try {
        await reshow()
        // not splash: the last check's answer, landing after its end, may have drawn it off already
        await until(async () => (await row('media-images').locator('[aria-checked="true"]').getAttribute('data-value')) === 'off')
        await row('workflows').locator('[data-value="on"]').click()             // its POST hangs
        await until(() => posts.length === 1)
        await row('media-images').locator('[data-value="on"]').click()          // made meanwhile: it waits
        await until(async () => (await toast()).includes("didn't answer"), 30000)
        await until(() => posts.map(p => p.key).join() === 'workflows,media-images')
        await until(async () => (await row('media-images').locator('[aria-checked="true"]').getAttribute('data-value')) === 'on')
        await until(async () => (await row('workflows').locator('[aria-checked="true"]').getAttribute('data-value')) === 'off')
      } finally { if (release) release() }
    })

    await ctx.check('"Other model…" opens a text input, Escape closes it, Enter sends the typed id; a note is a line beside its group\'s rows, an info row a line', async () => {
      let sent = null
      unmock()
      mock(async (method, body) => {
        if (method !== 'POST') return answer(200, { rows: MOCK_ROWS, ts: 1 })
        sent = body
        const rows = MOCK_ROWS.map(x => x.key === sent.key ? { ...x, value: sent.value } : x)
        return answer(200, { ok: true, said: `English writing: API ${sent.value}`, rows })
      })
      await reshow()
      await until(async () => (await row('api-writing').count()) === 1)
      const gpu = await page.textContent('#settings-rows .set-card[data-group="gpu"]')
      const kinds = await page.locator('#settings-rows .set-card[data-group="gpu"] .set-row').evaluateAll(rs => rs.map(r => r.dataset.kind).join())
      if (!gpu.includes('No GPU model found') || kinds !== 'note,choice' || await row('media-images').locator('[role="radio"]').count() !== 2)
        throw new Error(`${gpu} [${kinds}]`)
      if (!(await row('api-connected').textContent()).includes('Connected: Provider')) throw new Error('no info line')
      await fits(page.viewportSize().width)               // a select's long option stays inside (WebKit counted it past the view)
      const sel = row('api-writing').locator('select')
      const labels = await sel.locator('option').allTextContents()
      if (labels[labels.length - 1] !== 'Other model…') throw new Error(labels.join(' | '))
      await sel.selectOption({ label: 'Other model…' })
      const input = row('api-writing').locator('input[data-other]')
      await input.fill('p/typed')
      await input.press('Escape')                         // the row back: no input, the select on the row's value
      await until(async () => (await input.count()) === 0).catch(() => { throw new Error('Escape left the input open') })
      if ((await sel.inputValue()) !== '' || sent) throw new Error(`after Escape: ${JSON.stringify(await sel.inputValue())} ${JSON.stringify(sent)}`)
      await sel.selectOption({ label: 'Other model…' })
      await input.fill('prov/typed-model-1')
      if (sent) throw new Error('sent before Enter: ' + JSON.stringify(sent))
      await input.press('Enter')
      await until(() => sent !== null)
      if (JSON.stringify(sent) !== JSON.stringify({ key: 'api-writing', value: 'prov/typed-model-1' })) throw new Error(JSON.stringify(sent))
      await until(async () => (await toast()).includes('prov/typed-model-1'))
      await until(async () => (await sel.inputValue()) === 'prov/typed-model-1')
    })

    await ctx.check('a number row sends on Enter, and Off sends off; a choice needing Claude\'s help says Claude will offer to set it up', async () => {
      const sent = []
      unmock()
      const help = [...MOCK_ROWS, { key: 'probe', group: 'experimental', label: 'Agent code navigation', kind: 'choice', value: 'off', other: false,
        says: 'Whole files.', choices: [{ value: 'off', label: 'Off (default)', says: 'Whole files.' }, { value: 'on', label: 'On (large files only)', says: 'probe.' }] }]
      mock(async (method, b) => {
        if (method !== 'POST') return answer(200, { rows: help, ts: 1 })
        sent.push(b)
        const rows = help.map(x => x.key === b.key ? { ...x, value: b.value === null ? null : b.key === 'cap' ? Number(b.value) : b.value } : x)
        return answer(200, { ok: true, rows, said: b.key === 'probe' ? "probe: on, not set up: probe isn't installed" : `day cap: ${b.value ?? 'off'}`,
          ...(b.key === 'probe' ? { help: 'Offer to set it up.' } : {}) })
      })
      await reshow()
      await until(async () => (await row('cap').count()) === 1)
      const units = await row('cap').locator('.set-unit').allTextContents()
      if (units.join() !== '$,/day') throw new Error('units: ' + units.join())
      const input = row('cap').locator('input')
      await input.fill('7.5')
      await input.press('Enter')
      await until(() => sent.length === 1)
      await row('cap').locator('.set-off').click()
      await until(() => sent.length === 2)
      await until(async () => (await row('cap').locator('.set-off').getAttribute('aria-pressed')) === 'true')
      await row('probe').locator('[data-value="on"]').click()
      await until(() => sent.length === 3)
      await until(async () => (await toast()) === "probe: on, not set up: probe isn't installed. Claude will offer to set it up.")
      if (JSON.stringify(sent) !== JSON.stringify([{ key: 'cap', value: '7.5' }, { key: 'cap', value: null }, { key: 'probe', value: 'on' }])) throw new Error(JSON.stringify(sent))
    })

    await ctx.check(desktop ? 'the view lays out at 1280 px, two columns of cards, and at 390 px, one, nothing past its edge'
                            : 'the view lays out at the phone\'s width, one column of cards, nothing past its edge', async () => {
      unmock()
      await reshow()                                       // the machine's own rows, or the stand-in's
      await until(async () => (await page.locator('#settings-rows .set-card[data-group]').count()) > 0)
      const vp = page.viewportSize()
      if (desktop) {
        if ((await fits(1280)).cols !== 2) throw new Error('not two columns at 1280')
        await page.setViewportSize({ width: 390, height: 844 })
        try {
          await until(async () => await page.evaluate(() => innerWidth) === 390)
          if ((await fits(390)).cols !== 1) throw new Error('not one column at 390')
        } finally { await page.setViewportSize(vp) }
      } else if ((await fits(vp.width)).cols !== 1) throw new Error('not one column on a phone')
    })
  } finally {
    handlers.length = 0
    await page.evaluate(() => { if (window.__wbFetch) { window.fetch = window.__wbFetch; delete window.__wbFetch } })
  }
}
