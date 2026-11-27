// The Settings view's checks in a real browser (#1286). e2e_chat.mts calls check(page, ctx) once
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
            if r["ok"] else (409, {"ok": False, "error": r["said"], "rows": rows, **({"page": r["page"]} if r.get("page") else {})})
print(json.dumps(out))
`
const standIn = (env: E2eCtx['env'], req: unknown) => new Promise<[number, unknown]>((resolve, reject) =>
  execFile(process.env.PYTHON || 'python3', ['-c', STAND_IN, JSON.stringify(req)], { cwd: ROOT, env, encoding: 'utf8', timeout: 60000 },
    (err, out, stderr) => err ? reject(new Error(String(stderr || err).trim().split('\n').pop())) : resolve(JSON.parse(out))))

// rows of every kind, for the checks the machine's own table can't promise (an API row: only with a provider)
const MOCK_ROWS: SettingRow[] = [
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
  { key: 'api-connected', group: 'api', label: 'Connected', kind: 'info', text: 'Connected: Provider (key stored), run `opencode auth login p`' },
  { key: 'api-writing', group: 'api', label: 'English writing API model', kind: 'choice', value: null, other: true,
    says: 'English writing has no API model: it runs on the GPU.',
    choices: [{ value: 'prov/a', label: 'A model whose name runs long · A provider with a long name too (1049K) · $0.27/$1.10 per M · free',
      says: 'English writing uses prov/a.' }] },
  { key: 'splash', group: 'terminal', label: 'Opening animation', kind: 'choice', value: 'on', other: false, says: 'An animation.',
    choices: [{ value: 'on', label: 'Enabled (default)', says: 'An animation.' }, { value: 'off', label: 'Off', says: 'At once.' }] },
]

// In the page, of a focused element: whether its ring shows (:focus-visible, an outline) and the
// ring's contrast against what is under it, its own fill or the nearest one behind it (WCAG's
// ratio); `outside`, a ring drawn outside the element: the fill behind the element
export function ringContrast(el: Element, outside = false) {
  const rgb = (s: string) => { const n = (s.match(/[\d.]+/g) || []).map(Number); return s.startsWith('color(') ? n.map(x => x * 255) : n }
  const lum = (c: number[]) => { const [r, g, b] = c.slice(0, 3).map(v => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4 }); return 0.2126 * r + 0.7152 * g + 0.0722 * b }
  const cs = getComputedStyle(el)
  let under = outside ? el.parentElement : el
  while (under && (rgb(getComputedStyle(under).backgroundColor)[3] ?? 1) === 0) under = under.parentElement
  const a = lum(rgb(cs.outlineColor)), b = lum(rgb(getComputedStyle(under || document.body).backgroundColor))
  return { visible: el.matches(':focus-visible') && cs.outlineStyle !== 'none' && parseFloat(cs.outlineWidth) >= 2,
    ratio: Math.round((Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05) * 100) / 100, ring: cs.outlineColor,
    under: getComputedStyle(under || document.body).backgroundColor }
}

export async function check(page: any, ctx: E2eCtx) {
  const { base, token, env, home, until, goTo, desktop } = ctx
  const view = () => page.locator('#settings-rows')
  const row = (key: string) => page.locator(`#settings-rows .set-row[data-key="${key}"]`)
  const toast = () => page.evaluate(() => { const t = document.getElementById('toast')!; return t.classList.contains('on') ? t.textContent : '' })
  // shown again, from nothing: the waits below see only what this showing's GET drew
  const reshow = async () => {
    await goTo('chat')
    await page.evaluate(() => document.getElementById('settings-rows')!.replaceChildren())
    await goTo('settings')
  }

  const probe = await fetch(base + 'api/settings', { headers: { Authorization: 'Bearer ' + token } })
  const real = probe.status === 200
  console.log(`     (the settings rows from ${real ? "the server's /api/settings" : 'a stand-in for /api/settings: settings.py on this WB_HOME'})`)
  // The page's calls to /api/settings answered here: each handler (method, body) gives [status,
  // json] or null for the next, the newest first, and null from all goes to the server. In the
  // page, not through page.route or context.route: the page is its service worker's client, and
  // WebKit sends such a page's fetches past both (every stand-in call there got the server's 404).
  // a handler's answer: [status, json], or nothing for the next
  type Handler = (method: string, body: any) => unknown
  const handlers: Handler[] = []
  if (!real) handlers.push((method, body) => standIn(env, method === 'POST' ? body : null))
  if (!bound.has(page)) {
    bound.add(page)
    await page.exposeBinding('__wbSettings', async (_: unknown, method: string, body: unknown) => {
      for (let i = handlers.length - 1; i >= 0; i--) {
        try { const out = await handlers[i](method, body); if (out) return out }
        catch (e) { return [500, { error: String((e as Error).message || e) }] }
      }
      return null
    })
  }
  await page.evaluate(() => {
    if (window.__wbFetch) return
    const real = window.__wbFetch = window.fetch
    window.fetch = async (url: RequestInfo | URL, init: RequestInit = {}) => {
      if (new URL(String(url), location.href).pathname !== '/api/settings') return real(url, init)
      const method = (init.method || 'GET').toUpperCase()
      // a hung answer ends as a real fetch's does, at its signal's abort (#1391)
      const out = await new Promise<[number, unknown] | null>((resolve, reject) => {
        if (init.signal) init.signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')), { once: true })
        window.__wbSettings(method, init.body ? JSON.parse(init.body as string) : null).then(resolve, reject)
      })
      if (!out) return real(url, init)
      return new Response(JSON.stringify(out[1]), { status: out[0], headers: { 'Content-Type': 'application/json' } })
    }
  })
  const mocks: Handler[] = []                                        // each check's own handler, taken off after it
  const mock = (fn: Handler) => { mocks.push(fn); handlers.push(fn) }
  const unmock = () => { for (const fn of mocks.splice(0)) handlers.splice(handlers.indexOf(fn), 1) }
  const answer = (status: number, body: unknown) => [status, body]
  const fits = async (width: number) => {
    const got = await page.evaluate(() => {
      const v = document.getElementById('view-settings')!, w = v.getBoundingClientRect()
      const out = [...document.querySelectorAll('#settings-rows .set-card, #settings-rows .set-row > *, #settings-rows .seg button')]
        .map(e => ({ e, r: e.getBoundingClientRect() }))
        .filter(({ r }) => r.width && (r.left < w.left - 1 || r.right > w.right + 1))
        .map(({ e, r }) => `${e.className || e.tagName} ${Math.round(r.left)}..${Math.round(r.right)}`)
      return { width: innerWidth, out, scroll: v.scrollWidth - v.clientWidth, page: document.documentElement.scrollWidth - innerWidth,
        cols: Number(getComputedStyle(document.querySelector('#settings-rows .set-cards')!).columnCount) || 1 }  // CSS columns on a desktop
    })
    if (got.width !== width || got.out.length || got.scroll > 0 || got.page > 0) throw new Error(JSON.stringify(got))
    return got
  }
  // the folded cards open, so a check reaches their rows on a phone too (their own check is below)
  const FOLDED = ['gpu', 'api', 'setup', 'workflows', 'terminal', 'experimental', 'changes']
  const openAll = () => page.evaluate((keys: string[]) => {
    try { localStorage.setItem('wb-settings-open', JSON.stringify(Object.fromEntries(keys.map(k => [k, true])))) } catch {}
  }, FOLDED)
  await openAll()
  try {
    await ctx.check('Settings lists the admin rows above the device cards, a card per role first, its GPU model under More, then "Agents and roles" (#1553)', async () => {
      await reshow()
      await until(async () => (await page.locator('#settings-rows .set-card[data-group]').count()) > 0)
        .catch(async () => { throw new Error('no rows: ' + await view().textContent()) })
      const got = await page.evaluate(() => ({
        groups: [...document.querySelectorAll<HTMLElement>('#settings-rows .set-card')].map(c => c.dataset.group + ':' + c.querySelector('h3')!.textContent),
        above: !!(document.getElementById('settings-rows')!.compareDocumentPosition(document.getElementById('settings')!) & Node.DOCUMENT_POSITION_FOLLOWING),
        coding: !!document.querySelector('#settings-rows .set-card[data-group="shared"] .set-row[data-key="coding"] [role="radio"]'),
        // the role's own row unlabelled (its card says it), the rest of the role under a closed More
        role: !!document.querySelector('#settings-rows .set-card[data-group="significant"] > .set-row[data-key="role-significant"] [role="radio"]')
          && !document.querySelector('.set-row[data-key="role-significant"] .set-label'),
        more: [...document.querySelectorAll<HTMLElement>('#settings-rows .set-card[data-group="significant"] details.set-more:not([open]) .set-row')].map(r => r.dataset.key) }))
      if (got.groups.slice(0, 4).join() !== 'significant:Complex coding,trivial:Trivial coding,writing:English writing,shared:Agents and roles'
        || !got.coding || !got.above || !got.role || got.more.some((k: string) => !/^(gpu|api|think|review)-significant$/.test(k))) throw new Error(JSON.stringify(got))
      const order = ['significant', 'trivial', 'writing', 'shared', 'web', 'gpu', 'api', 'setup', 'workflows', 'terminal', 'experimental']  // settings.GROUPS (web: #1355; the roles: #1553)
      const idx = got.groups.map((g: string) => order.indexOf(g.split(':')[0]))
      if (idx.some((v: number, i: number) => v < 0 || (i && v <= idx[i - 1]))) throw new Error('the groups out of order: ' + got.groups.join())
    })

    await ctx.check('choosing Claude coding → "Ask me first" lights at once, runs wb coding ask and writes coding.json; the toast says it in the page\'s words', async () => {
      const file = join(home, 'state', 'coding.json')
      const mode = () => existsSync(file) ? JSON.parse(readFileSync(file, 'utf8')).mode : undefined
      const ask = row('coding').locator('[role="radio"][data-value="ask"]')
      await ask.click()
      if ((await ask.getAttribute('aria-checked')) !== 'true') throw new Error('not lit at once')
      await until(() => mode() === 'ask').catch(() => { throw new Error('coding.json: ' + mode()) })
      // the row's label and the choice's, then its new sentence: never wb's stdout (the web app's review, 2026-11-26)
      await until(async () => (await toast()).startsWith('Claude coding: Ask me first. '))
        .catch(async () => { throw new Error('toast: ' + await toast()) })
      await until(async () => (await row('coding').locator('[aria-checked="true"]').getAttribute('data-value')) === 'ask')
      if (!ctx.wb('coding').startsWith('Claude coding: Ask me first.')) throw new Error('wb coding disagrees: ' + ctx.wb('coding'))
      await row('coding').locator('[role="radio"][data-value="decide"]').click()  // as it was
      await until(() => mode() === 'decide')
    })

    await ctx.check('a refused change (409) puts the control back, toasts the refusal by the row\'s label and keeps it under the row past the toast; one request at a time, the next click waits (#1563)', async () => {
      let rows = MOCK_ROWS, inFlight = 0, most = 0, posts: any[] = []
      mock(async (method: string, body: any) => {
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
      await until(async () => (await toast()).includes("Dynamic workflows wasn't changed: wb refused: workflows are off for this test"))
      await until(async () => (await row('workflows').locator('[aria-checked="true"]').getAttribute('data-value')) === 'off')
      await until(() => posts.length === 2)
      await until(async () => (await row('splash').locator('[aria-checked="true"]').getAttribute('data-value')) === 'off')
      if (most !== 1 || posts.map(p => p.key).join() !== 'workflows,splash') throw new Error(JSON.stringify({ most, posts }))
      // #1563: the toast went in 3.5 s, and the reverted row said nothing of the refusal
      await until(async () => !(await toast()).includes('workflows are off'), 30000)
      const kept = await row('workflows').locator('.set-refused').textContent()
      if (!kept.includes("wasn't changed") || !(await row('workflows').locator('.set-ask').count())) throw new Error('kept: ' + kept)
    })

    await ctx.check('a change whose POST never answers is given up after POST_MS: the toast says so, the next change goes, and the control shows the computer\'s value (#1391)', async () => {
      // #1391: with no deadline the hung change stayed lit as applied, and every later click
      // queued behind it, never sent until a reload
      let rows = MOCK_ROWS, release = null as ((v?: unknown) => void) | null  // set in the handler: not narrowed to null
      const posts: any[] = []
      unmock()
      mock(async (method: string, body: any) => {
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

    await ctx.check('a change made outside the page is drawn once the page takes the focus, and a click on the value shown still sends (#1392)', async () => {
      // #1392: Settings beside the terminal kept a value changed there, and a click on it to put
      // it back sent nothing, the setting left as the terminal made it
      let rows = MOCK_ROWS
      const posts: any[] = []
      unmock()
      mock(async (method: string, body: any) => {
        if (method !== 'POST') return answer(200, { rows, ts: 1 })
        posts.push(body)
        rows = rows.map(x => x.key === body.key ? { ...x, value: body.value } : x)
        return answer(200, { ok: true, said: 'Claude coding: as you chose.', rows })
      })
      const lit = async () => await row('coding').locator('[aria-checked="true"]').getAttribute('data-value')
      await reshow()
      await until(async () => await lit() === 'decide')
      rows = rows.map(x => x.key === 'coding' ? { ...x, value: 'ask' } : x)   // `wb coding ask` at the terminal
      await page.evaluate(() => dispatchEvent(new Event('focus')))             // back to the page
      await until(async () => await lit() === 'ask')
      rows = rows.map(x => x.key === 'coding' ? { ...x, value: 'only' } : x)  // again, not drawn yet
      await row('coding').locator('[data-value="ask"]').click()                // the value shown, put back
      await until(() => posts.length === 1)
      if (JSON.stringify(posts) !== JSON.stringify([{ key: 'coding', value: 'ask' }])) throw new Error(JSON.stringify(posts))
      await until(async () => await lit() === 'ask')
    })

    await ctx.check('a change Claude makes (wb settings set under Claude Code) is drawn with no reload, stamped by Claude, listed in Recent changes and toasted by its choice labels (#1355)', async () => {
      unmock()
      const lit = async () => await row('coding').locator('[aria-checked="true"]').getAttribute('data-value')
      const asClaude = (value: string) => new Promise<void>((resolve, reject) => execFile(join(ROOT, 'bin', 'wb'),
        ['settings', 'set', 'coding', value], { env: { ...env, CLAUDECODE: '1' }, encoding: 'utf8', timeout: 60000 },
        (err, _out, stderr) => err ? reject(new Error(String(stderr || err).trim())) : resolve()))
      await reshow()
      await until(async () => await lit() === 'decide')
      await page.evaluate(() => { (window as any).__wbNoReload = 1 })
      await asClaude('ask')
      await until(async () => await lit() === 'ask', 3000).catch(async () => { throw new Error('not drawn: ' + await lit()) })
      if (await page.evaluate(() => (window as any).__wbNoReload) !== 1) throw new Error('the page reloaded')
      const stamp = await row('coding').locator('.set-changed').textContent()
      if (!stamp.startsWith('Changed by Claude')) throw new Error(stamp)
      // by the row's choice labels, the raw value only for a row not drawn (the web app's review, 2026-11-26)
      await until(async () => (await toast()).includes('Claude coding set to Ask me first by Claude')).catch(async () => { throw new Error('toast: ' + await toast()) })
      const listed = await page.locator('#settings-rows .set-changes').textContent()
      if (!listed.includes('Claude coding: Let Claude decide → Ask me first, by Claude')) throw new Error(listed)
      await asClaude('decide')                               // as it was
      await until(async () => await lit() === 'decide', 3000)
    })

    await ctx.check('a choice made by keyboard keeps the focus on its control, and a number sent with Enter keeps its box (#1409)', async () => {
      // #1409 (2026-11-25): the redraw took the focused radio away, the focus fell to the page,
      // and the next Tab began again at the first row
      let rows = MOCK_ROWS
      unmock()
      mock(async (method: string, body: any) => {
        if (method !== 'POST') return answer(200, { rows, ts: 1 })
        rows = rows.map(x => x.key === body.key ? { ...x, value: body.value } : x)
        return answer(200, { ok: true, said: `${body.key}: ${body.value}`, rows })
      })
      const at = () => page.evaluate(() => { const a = document.activeElement as HTMLElement | null, r = a && a.closest<HTMLElement>('.set-row')
        return r ? `${r.dataset.key}:${a!.tagName}:${a!.dataset.value || ''}:${a!.getAttribute('aria-checked') || ''}` : String(a && a.tagName) })
      await reshow()
      await until(async () => (await row('splash').locator('[aria-checked="true"]').getAttribute('data-value')) === 'on')
      await row('splash').locator('[data-value="off"]').focus()
      await page.keyboard.press('Space')
      if (await at() !== 'splash:BUTTON:off:true') throw new Error('after the press: ' + await at())
      await until(async () => (await toast()) === 'Opening animation: Off.')
      if (await at() !== 'splash:BUTTON:off:true') throw new Error('after the answer: ' + await at())
      await row('cap').locator('input').fill('6')
      await row('cap').locator('input').press('Enter')
      await until(async () => (await toast()) === 'API day cap: $6/day.')
      if (await at() !== 'cap:INPUT::') throw new Error('after Enter: ' + await at())
      await page.evaluate(() => (document.activeElement as HTMLElement).blur())
    })

    await ctx.check('the focus ring on a Settings choice, lit or not, stands 3:1 or more against what is under it (#1410)', async () => {
      // #1410 (2026-11-25): the ring was the accent, drawn on the lit choice's own accent fill, and
      // 2.99:1 on the panel
      for (const [v, from] of [['off', 'on'], ['on', 'off']]) {  // splash is off since the last check: lit, then not
        const b = row('splash').locator(`[data-value="${v}"]`)
        await row('splash').locator(`[data-value="${from}"]`).focus()
        await page.keyboard.press('ArrowRight')            // the arrows move the focus within the group, round
        const got = await b.evaluate(ringContrast)
        if (!got.visible || got.ratio < 3) throw new Error(`${v}: ${JSON.stringify(got)}`)
      }
      await page.evaluate(() => (document.activeElement as HTMLElement).blur())
    })

    await ctx.check('a Settings radio group is one Tab stop, its checked choice, and the arrows move the focus within it, picking nothing', async () => {
      // the web app's review (2026-11-26): 45 Tab stops, and arrows that did nothing in a radio group
      const got = await page.evaluate(() => [...document.querySelectorAll<HTMLElement>('.set-row[data-key="splash"] [role="radio"]')]
        .map(b => `${b.dataset.value}:${b.tabIndex}:${b.getAttribute('aria-checked')}`).join())
      if (got !== 'on:-1:false,off:0:true') throw new Error(got)
      await row('splash').locator('[data-value="off"]').focus()
      await page.keyboard.press('ArrowLeft')
      const at = await page.evaluate(() => (document.activeElement as HTMLElement).dataset.value)
      await page.keyboard.press('End')
      const end = await page.evaluate(() => (document.activeElement as HTMLElement).dataset.value)
      if (at !== 'on' || end !== 'off' || (await row('splash').locator('[aria-checked="true"]').getAttribute('data-value')) !== 'off')
        throw new Error(`${at} ${end}`)
      await page.evaluate(() => (document.activeElement as HTMLElement).blur())
    })

    await ctx.check('Settings opened while the computer is out of reach offers Try again, and draws its rows once the line is back, with no tab switch (#1412)', async () => {
      // #1412 (2026-11-25): opened before the line was back, as a phone waking, it said it
      // couldn't load until a tab switch. A page of its own: this one's rows are drawn already
      const p = await ctx.context.newPage()
      await p.addInitScript(() => {
        window.e2eDown = true
        const real = window.fetch
        window.fetch = (u: RequestInfo | URL, o?: RequestInit) => window.e2eDown && /^\/api\/(settings|state|stream)$/.test(new URL(String(u), location.href).pathname)
          ? Promise.reject(new TypeError('Failed to fetch')) : real(u, o)
      })
      try {
        await p.goto(base + '#settings')
        await until(async () => (await p.textContent('#settings-rows')).includes("Settings couldn't load: the computer can't be reached"))
        if (!(await p.locator('#settings-rows .set-retry').isVisible())) throw new Error('no Try again')
        await p.evaluate(() => { window.e2eDown = false })   // the stream's next try gets through
        await until(async () => (await p.locator('#settings-rows .set-card[data-group]').count()) > 0)
          .catch(async () => { throw new Error(await p.textContent('#settings-rows')) })
        if (await p.evaluate(() => document.body.dataset.view) !== 'settings') throw new Error('the view changed')
      } finally { await p.close() }
    })

    await ctx.check('"Other model…" opens a text input, Escape closes it, Enter sends the typed id; a note is a line beside its group\'s rows, an info row a line', async () => {
      let sent: any = null
      unmock()
      mock(async (method: string, body: any) => {
        if (method !== 'POST') return answer(200, { rows: MOCK_ROWS, ts: 1 })
        sent = body
        const rows = MOCK_ROWS.map(x => x.key === sent.key ? { ...x, value: sent.value } : x)
        return answer(200, { ok: true, said: `English writing: API ${sent.value}`, rows })
      })
      await reshow()
      await until(async () => (await row('api-writing').count()) === 1)
      const gpu = await page.textContent('#settings-rows .set-card[data-group="gpu"]')
      const kinds = await page.locator('#settings-rows .set-card[data-group="gpu"] .set-row').evaluateAll((rs: HTMLElement[]) => rs.map(r => r.dataset.kind).join())
      if (!gpu.includes('No GPU model found') || kinds !== 'note,choice' || await row('media-images').locator('[role="radio"]').count() !== 2)
        throw new Error(`${gpu} [${kinds}]`)
      if (!(await row('api-connected').textContent()).includes('Connected: Provider')) throw new Error('no info line')
      // a `…` span is code, the backticks gone (the web app's review, 2026-11-26)
      const code = await row('api-connected').locator('p code').allTextContents()
      if (code.join() !== 'opencode auth login p' || (await row('api-connected').textContent()).includes('`')) throw new Error('code: ' + code.join())
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

    await ctx.check('a number row sends on Enter, and Off sends off; a choice needing the driver\'s help says it will offer to set it up', async () => {
      const sent: any[] = []
      unmock()
      const help: SettingRow[] = [...MOCK_ROWS, { key: 'probe', group: 'experimental', label: 'Agent code navigation', kind: 'choice', value: 'off', other: false,
        says: 'Whole files.', choices: [{ value: 'off', label: 'Off (default)', says: 'Whole files.' }, { value: 'on', label: 'On (large files only)', says: 'probe.' }] }]
      mock(async (method: string, b: any) => {
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
      // named as the state names the driver (#1355): "Your driver" with none known
      await until(async () => /^Agent code navigation: On \(large files only\)\. (Claude|OpenCode|Your driver) will offer to set it up\.$/.test(await toast()))
      if (JSON.stringify(sent) !== JSON.stringify([{ key: 'cap', value: '7.5' }, { key: 'cap', value: null }, { key: 'probe', value: 'on' }])) throw new Error(JSON.stringify(sent))
    })

    await ctx.check('with no API provider one line stands for the API controls: a role reads its sentence alone, a hidden row is not drawn', async () => {
      // the web app's review (2026-11-26): six API controls came before the line saying there was no provider
      const gpuOnly: SettingRow[] = [
        { key: 'role-significant', group: 'significant', label: 'Complex coding', kind: 'choice', value: 'gpu', other: false, fixed: true,
          says: 'Complex coding runs on this machine\'s GPU.', choices: [{ value: 'gpu', label: 'GPU' }, { value: 'api', label: 'API' }] },
        { key: 'gpu-only', group: 'shared', label: '', kind: 'note', text: 'Every role runs on your GPU. To add an API model: /connect in OpenCode, or ask Claude.' },
        { key: 'agents', group: 'shared', label: 'Agents', kind: 'choice', value: 'both', other: false, hidden: true, says: 'x',
          choices: [{ value: 'gpu', label: 'GPU' }, { value: 'both', label: 'Both' }] },
        ...MOCK_ROWS]
      unmock()
      mock(async (method: string) => method === 'POST' ? null : answer(200, { rows: gpuOnly, ts: 1 }))
      await reshow()
      await until(async () => (await row('gpu-only').count()) === 1)
      const got = await page.evaluate(() => ({
        role: document.querySelector('.set-row[data-key="role-significant"]')!.textContent,
        controls: document.querySelectorAll('.set-row[data-key="role-significant"] button, .set-row[data-key="role-significant"] select').length,
        agents: document.querySelectorAll('.set-row[data-key="agents"]').length }))
      if (got.role !== "Complex coding runs on this machine's GPU." || got.controls || got.agents) throw new Error(JSON.stringify(got))
      // the line's Ask link: the chat's box filled with what it is about, sent by the user, not by the tap
      const link = await row('gpu-only').locator('.set-ask').textContent()
      if (!/^Ask (Claude|OpenCode|your driver)$/.test(link)) throw new Error('link: ' + link)
      await row('gpu-only').locator('.set-ask').click()
      await until(() => page.evaluate(() => document.body.dataset.view === 'chat'))
      const box = await page.locator('#input').inputValue()
      if (!box.startsWith('Help me with Settings › Agents and roles: Every role runs on your GPU.')) throw new Error('box: ' + box)
      if (!(await page.evaluate(() => document.activeElement === document.getElementById('input')))) throw new Error('the box not focused')
      await page.locator('#input').fill('')
    })

    await ctx.check('a role\'s API choice with no model opens its API model list under the row; a pick sends the model, then the role\'s API (#1558)', async () => {
      // #1558 (2026-11-26): the tap was refused with a terminal command, the list 2,000 px away under another card
      let roles: SettingRow[] = [
        { key: 'role-writing', group: 'writing', label: 'English writing', kind: 'choice', value: 'gpu', other: false, says: 'On the GPU.',
          choices: [{ value: 'gpu', label: 'GPU', says: 'On the GPU.' }, { value: 'api', label: 'API (pick its model)', says: 'Pick its API model first.', needs: 'api-writing' }] },
        ...MOCK_ROWS.map(r => r.key === 'api-writing' ? { ...r, group: 'writing', more: true } : r)]
      const posts: any[] = []
      unmock()
      mock(async (method: string, body: any) => {
        if (method !== 'POST') return answer(200, { rows: roles, ts: 1 })
        posts.push(body)
        roles = roles.map(x => x.key === body.key ? { ...x, value: body.value } : x)
        return answer(200, { ok: true, said: `${body.key}: ${body.value}`, rows: roles })
      })
      await reshow()
      await until(async () => (await row('role-writing').count()) === 1)
      await row('role-writing').locator('[data-value="api"]').click()
      const pick = row('role-writing').locator('.set-pick select')
      await until(async () => (await pick.count()) === 1).catch(() => { throw new Error('no list under the row') })
      if (posts.length) throw new Error('sent on the tap: ' + JSON.stringify(posts))
      const labels = await pick.locator('option').allTextContents()
      if (labels[labels.length - 1] !== 'Other model…' || !labels.some((l: string) => l.startsWith('A model whose name runs long'))) throw new Error(labels.join(' | '))
      await pick.selectOption('prov/a')
      await until(() => posts.length === 2)
      if (JSON.stringify(posts) !== JSON.stringify([{ key: 'api-writing', value: 'prov/a' }, { key: 'role-writing', value: 'api' }])) throw new Error(JSON.stringify(posts))
      await until(async () => (await row('role-writing').locator('[aria-checked="true"]').getAttribute('data-value')) === 'api')
      if (await row('role-writing').locator('.set-pick').count()) throw new Error('the list stayed open')
    })

    await ctx.check('the toast hides by its look, never display:none, lasts 60 ms a character (3.5 s at least), and waits while hovered', async () => {
      // the web app's review (2026-11-26): a live region that starts display:none is missed by some
      // screen readers, and a 236-character refusal went in 3.5 s (#1563)
      const got = await page.evaluate(async () => {
        const t = document.getElementById('toast')!, wait = (ms: number) => new Promise(r => setTimeout(r, ms))
        WB.toast('x'.repeat(100))                          // 6 s
        await wait(4500)
        const long = t.classList.contains('on')
        await wait(2500)
        const gone = !t.classList.contains('on') && getComputedStyle(t).display !== 'none' && getComputedStyle(t).visibility !== 'hidden'
        WB.toast('Short.')
        t.dispatchEvent(new MouseEvent('mouseenter'))
        await wait(4000)
        const held = t.classList.contains('on')
        t.dispatchEvent(new MouseEvent('mouseleave'))
        await wait(4000)
        return { long, gone, held, after: t.classList.contains('on') }
      })
      if (JSON.stringify(got) !== JSON.stringify({ long: true, gone: true, held: true, after: false })) throw new Error(JSON.stringify(got))
    })

    await ctx.check('the power groups are folded cards with a line of their state, closed on a phone and open wider, each one\'s state kept on this device', async () => {
      // the web app's review (2026-11-26): Settings ran 6 to 8 phone screens, Notifications 5,000 px down
      unmock()
      mock(async (method: string) => method === 'POST' ? null : answer(200, { rows: MOCK_ROWS.map(r => r.key === 'api-connected' ? { ...r, brief: 'Provider' } : r), ts: 1 }))
      await page.evaluate(() => { try { localStorage.removeItem('wb-settings-open') } catch {} })
      try {
        await reshow()
        await until(async () => (await page.locator('#settings-rows details.set-card[data-group="terminal"]').count()) === 1)
        const got = () => page.evaluate(() => Object.fromEntries([...document.querySelectorAll<HTMLDetailsElement>('#settings-rows details.set-card')]
          .map(d => [d.dataset.group, `${d.open}|${d.querySelector('.set-sum') ? d.querySelector('.set-sum')!.textContent : ''}`])))
        const first = await got()
        const wide = await page.evaluate(() => innerWidth > 600)
        if (first.terminal !== `${wide}|Opening animation: Enabled` || first.api !== `${wide}|Provider` || 'shared' in first)
          throw new Error(JSON.stringify(first))
        await page.locator('#settings-rows details.set-card[data-group="terminal"] > summary').click()
        await reshow()
        await until(async () => (await got()).terminal === `${!wide}|Opening animation: Enabled`)
          .catch(async () => { throw new Error(JSON.stringify(await got())) })
        if ((await got()).api !== `${wide}|Provider`) throw new Error('another card moved: ' + JSON.stringify(await got()))
      } finally { await openAll() }
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
