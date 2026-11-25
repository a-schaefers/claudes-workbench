// The Stats view's checks in a real browser (#1285). e2e_chat.mjs calls check(page, ctx) once per
// device, on a page with the key and the server up; ctx holds { engine, device, desktop, base,
// token, home, env, wb, until, check, sleep, goTo, context, browser } (goTo('stats') taps the tab
// and waits for the view). The page is left on the view it was on.
//
// /api/stats and /api/top are the chat server's (#1280). Where the server under test answers
// neither yet, the page's own fetch stands in for them (Playwright's routes stop holding requests
// once the service worker registers, #1223): /api/stats with the real `wb stats --json` of this
// run's WB_HOME, so the fixture results.jsonl below is read by wb either way; /api/top with a
// stream held open, as the server's is. The page's fetch counts each /api/top it opens and each
// one let go (aborted, or its body cancelled or ended), the real server's too. Now's drawing is
// always fed fixed ticks.
import { writeFileSync, rmSync } from 'node:fs'
import { join } from 'node:path'

// The history's labels in statsLines' order: a row's place is checked against this
const ORDER = ['This week', 'All time', 'Streak', 'Without waking Claude', 'Questions', '*', 'Other models', 'By size',
  'Last 30 days', 'Bests', 'Milestone', 'What stopped agents', 'What helps', 'Bench', 'Optimized']
const rank = label => { const i = ORDER.indexOf(label.replace(/ \(.*$/, '')); return i < 0 ? ORDER.indexOf('*') : i }

const GiB = 2 ** 30
const tickOf = busy => ({ ts: 1, every: 2,
  gpus: [{ index: 0, name: 'Advanced Micro Devices, Inc. [AMD/ATI] Navi 31 [Radeon RX 7900 XTX] (rev c8)', used: 20 * GiB, total: 24 * GiB, busy, watts: 211 },
    { name: 'RAM', ram: true, used: 30 * GiB, total: 64 * GiB }],
  running: [{ task: 'fix-nav', model: 'ollama/fake-coder:30b', label: 'fake-coder 30B', phase: 'running', round: 2, round_s: 190,
    tok_s: 61.4, tok_s_from: 'live', context_pct: 34 },
  { task: 'fix-api', model: 'deepinfra/acme/Fast-Coder-1', via: 'api', phase: 'running', round: 1, round_s: 12, spend: 0.12, prices: 'catalog' }],
  rack: [{ model: 'ollama/fake-coder:30b', label: 'fake-coder 30B', state: 'resident', gpu_bytes: 19 * GiB, write_tok_s: 61, tasks: ['fix-nav'] },
    ...['a', 'b', 'c'].map(x => ({ model: `ollama/idle-${x}:7b`, label: `idle-${x} 7B`, state: 'idle' }))],
  queue: [{ task: 'fix-later', waited_s: 75 }] })

export async function check(page, { desktop, home, wb, until, check, sleep, goTo }) {
  const was = await page.evaluate(() => WB.current())
  const results = join(home, 'state', 'results.jsonl')
  const real = await page.evaluate(async () => (await WB.api('/api/stats')).status === 200)
  await page.exposeFunction('e2eFeed', () => wb('stats', '--json')).catch(() => {})  // once a page
  await page.evaluate(({ real, ticks }) => {
    const realFetch = window.e2eRealFetch || window.fetch
    window.e2eRealFetch = realFetch
    const top = window.e2eTop = { opened: 0, closed: 0, mode: real ? 'real' : 'hold', ticks, n: 0 }
    const enc = new TextEncoder()
    window.fetch = async (u, o = {}) => {
      const path = new URL(String(u), location.href).pathname
      if (path === '/api/stats' && !real) return new Response(JSON.stringify({ feed: JSON.parse(await window.e2eFeed()), routes: null, models: null }),
        { status: 200, headers: { 'Content-Type': 'application/json' } })
      if (path !== '/api/top') return realFetch(u, o)
      top.opened++
      let done = false
      const end = () => { if (!done) { done = true; top.closed++ } }
      if (o.signal) o.signal.addEventListener('abort', end, { once: true })
      if (top.mode === 'real') {
        try {
          const r = await realFetch(u, o)
          if (!r.body) { end(); return r }
          const pass = new TransformStream({ flush: end })
          r.body.pipeTo(pass.writable).catch(end)
          return new Response(pass.readable, { status: r.status, headers: r.headers })
        } catch (e) { end(); throw e }
      }
      const mode = top.mode
      const body = new ReadableStream({
        start(c) {
          if (mode === 'ticks') {
            for (let i = 0; i < 3; i++) c.enqueue(enc.encode(`event: top\ndata: ${JSON.stringify(top.ticks[top.n++ % top.ticks.length])}\n\n`))
            c.close(); end()
          }
          // held: open until the page lets go of it
        },
        cancel: end,
      })
      return new Response(body, { status: 200, headers: { 'Content-Type': 'text/event-stream' } })
    }
  }, { real, ticks: [20, 50, 80].map(tickOf) })
  const top = () => page.evaluate(() => ({ opened: window.e2eTop.opened, closed: window.e2eTop.closed }))
  const again = async () => { await goTo('chat'); await goTo('stats') }   // shown afresh: fetched afresh
  const rows = () => page.evaluate(() => [...document.querySelectorAll('#stats-body .st-row')]
    .map(r => ({ label: r.querySelector('dt').textContent, text: r.querySelector('dd').textContent, chart: r.querySelectorAll('dd .st-day').length })))
  const history = () => page.evaluate(() => (document.querySelector('#stats-body .st-hist-box') || {}).textContent || '')
  try {
    const now = Date.now() / 1000
    writeFileSync(results, [0, 1, 2].map(i => JSON.stringify({ ts: now - 3600 * i, task: `e2e-${i}`, model: 'ollama/fake-coder:7b',
      kind: 'gpu', outcome: i < 2 ? 'accepted' : 'discarded', ran: true, size: i ? 'trivial' : 'significant', rounds: 1, minutes: 3,
      ...(i === 2 ? { guards: { spend: 1 } } : {}) }) + '\n').join(''))

    await check('Stats: with tasks in results.jsonl, This week says how many, and the history keeps the pane\'s labels and order', async () => {
      await again()
      await until(async () => (await rows()).some(r => r.label === 'This week' && /\b3 tasks\b/.test(r.text)))
        .catch(async () => { throw new Error(JSON.stringify(await rows()) + ' ' + await history()) })
      const got = await rows()
      const say = () => JSON.stringify(got)
      const of = label => (got.find(r => r.label === label) || {}).text
      if (!/^3 tasks · 2 landed \(67%\)/.test(of('This week'))) throw new Error(say())
      if (of('By size') !== 'complex 1 (100%) · trivial 2 (50%)') throw new Error(say())
      if (of('What stopped agents') !== 'the API day cap 1') throw new Error(say())
      for (let i = 1; i < got.length; i++) if (rank(got[i].label) < rank(got[i - 1].label)) throw new Error('out of order: ' + say())
      const days = got.find(r => r.label === 'Last 30 days')
      if (!days || days.chart !== 30 || !days.text.includes('tasks per day')) throw new Error('the 30 days as bars: ' + say())
    })

    await check('Stats: the live stream opens when Stats is shown with Live view on, and closes on leaving it', async () => {
      await goTo('chat')
      await until(async () => { const t = await top(); return t.opened === t.closed })
      const before = (await top()).opened
      await goTo('stats')
      await until(async () => (await top()).opened > before).catch(() => { throw new Error('no /api/top asked for') })
      await sleep(500)                                     // a claim: it stays open while shown
      const t = await top()
      if (t.closed !== before) throw new Error('closed while shown: ' + JSON.stringify(t))
      await goTo('chat')
      await until(async () => (await top()).closed === t.opened).catch(async () => { throw new Error('/api/top still open after leaving Stats: ' + JSON.stringify(await top())) })
      await sleep(1500)                                    // a claim: nothing dials it again off the view
      if ((await top()).opened !== t.opened) throw new Error('dialled again: ' + JSON.stringify(await top()))
    })

    await check('Stats: with Live view off, Now isn\'t shown and no stream is opened', async () => {
      wb('stats', 'set', 'live', 'off')
      try {
        const before = (await top()).opened
        await goTo('stats')
        await until(async () => /3 tasks/.test(await history()))
        await sleep(1500)                                  // a claim: the stream is not asked for
        const now = await page.evaluate(() => { const b = document.querySelector('#stats-body .st-now-box'); return b ? b.hidden : null })
        if ((await top()).opened !== before || now !== true) throw new Error(JSON.stringify({ asked: (await top()).opened - before, hidden: now }))
      } finally {
        wb('stats', 'set', 'live', 'on')
        await goTo('chat')
      }
    })

    // Now, from fixed ticks (whatever server is under test): a stream of three, then its end
    await page.evaluate(() => { window.e2eTop.mode = 'ticks' })
    const nowSays = () => page.evaluate(() => {
      const box = document.querySelector('#stats-body .st-now-box')
      const q = s => [...box.querySelectorAll(s)]
      return { gpus: q('.st-gpu h3').map(e => e.textContent), meters: q('.st-gpu .st-meter').map(e => e.textContent),
        spark: (box.querySelector('.st-gpu .st-spark-line') || { getAttribute: () => '' }).getAttribute('points').split(' ').filter(Boolean).length,
        running: q('.st-run').map(e => e.textContent), rack: q('.st-model').map(e => e.textContent), queue: q('.st-wait').map(e => e.textContent),
        text: box.textContent }
    })
    await check('Stats: Now draws a card per GPU and for RAM with bars and a busy sparkline, the running tasks, the rack folded, the queue', async () => {
      await goTo('stats')
      await until(async () => (await nowSays()).spark >= 3).catch(async () => { throw new Error(JSON.stringify(await nowSays())) })
      const s = await nowSays(), say = () => JSON.stringify(s)
      if (s.gpus.join() !== 'GPU 0 Radeon RX 7900 XTX,RAM') throw new Error(say())
      if (!s.meters[0].includes('20.0/24.0 GiB') || !s.meters.some(m => /Busy.*%/.test(m)) || !s.meters.some(m => m.includes('30.0/64.0 GiB'))) throw new Error(say())
      if (!s.text.includes('211 W')) throw new Error(say())
      if (!s.running[0].includes('fix-nav fake-coder 30B · running · round 2 · 3m10s · 61 tok/s · context 34%')) throw new Error(say())
      if (!s.running[1].includes('fix-api API deepinfra Fast-Coder-1 · running · round 1 · 12s · ≈$0.12')) throw new Error(say())
      if (s.rack.length !== 2 || !s.rack[0].includes('← fix-nav') || !s.rack[1].includes('+3 more models idle, can\'t run or remote (wb top)')) throw new Error(say())
      if (s.queue.join() !== '◌fix-later waits for the GPU (1m15s)') throw new Error(say())
    })

    await check(`Stats: the view fits ${desktop ? 1280 : 'a phone\'s'} width with no sideways scroll${desktop ? ', its cards two columns' : ''}`, async () => {
      const got = await page.evaluate(() => {
        const v = document.getElementById('view-stats'), w = innerWidth
        const cards = [...v.querySelectorAll('.card')].map(c => c.getBoundingClientRect()).filter(r => r.width)
        return { w, page: document.documentElement.scrollWidth, view: v.scrollWidth - v.clientWidth,
          out: cards.filter(r => r.left < -1 || r.right > w + 1).length, cards: cards.length,
          cols: getComputedStyle(v.querySelector('.st-now')).gridTemplateColumns.split(' ').length }
      })
      if (got.page > got.w || got.view > 0 || got.out || got.cards < 5) throw new Error(JSON.stringify(got))
      if (desktop ? got.w !== 1280 || got.cols !== 2 : got.w > 600 || got.cols !== 1) throw new Error(JSON.stringify(got))
    })

    await check('Stats: with no results, the empty state', async () => {
      rmSync(results, { force: true })
      await again()
      await until(async () => (await history()).includes('No finished tasks yet: delegate one with /workbench, and how it went shows here.'))
        .catch(async () => { throw new Error(await history()) })
    })
  } finally {
    rmSync(results, { force: true })
    await goTo(was)
    await page.evaluate(() => { window.fetch = window.e2eRealFetch }).catch(() => {})
  }
}
