// The Stats view (#1285): Now, live from /api/top, and the history, from /api/stats. The pane's
// Stats tab ported (register.tsx liveLines and statsLines), label for label and in its order,
// then drawn as cards with the charts a terminal couldn't: inline SVG built here, no library, so
// the CSP holds, coloured by the page's tokens (stats.css). Built against window.WB (app.js).
(() => {
  'use strict'
  if (!window.WB) return
  const BUSY_KEEP = 60        // busy % samples kept per GPU for its sparkline: two minutes at 2 s
  const STATS_SHOWN = 3       // model lines shown (stats.STATS_MODEL_LINES), the rest one row
  const RACK_FOLD = new Set(['idle', "can't run", 'remote']), RACK_FOLD_MIN = 2  // top.FOLD
  const REFETCH_MS = 60000    // the history again, and at feed.until (the next local midnight)
  const TOP_RETRY_MS = [5000, 10000, 20000, 40000, 60000]  // a stream that gave nothing, again
  const SVG = 'http://www.w3.org/2000/svg'
  const NB = ' '

  // ---- words: the pane's (its Names), so the terminal and the page agree ----
  const ROLE_WORDS = { significant: 'complex', trivial: 'trivial', writing: 'writing' }
  const GUARD_WORDS = {
    repeat: 'the same call repeated', errors: 'tool errors in a row', no_edit: 'no file changed',
    round: 'the round time limit', silence: 'silence', think: 'thinking with no tool call',
    handovers: 'handovers without finishing',
    spend: 'the API day cap', // Settings' and the Tasks view's words for it
  }
  const RACK_MARK = { resident: '●', cpu: '●', split: '◑', idle: '○', "can't run": '✗', remote: '⇢' }
  const RACK_TONE = { resident: 'ok', cpu: 'ok', split: 'warn', "can't run": 'bad' }
  const gib = n => (n / 2 ** 30).toFixed(1)
  const secs = s => (s < 60 ? `${Math.round(s)}s` : s < 3600 ? `${Math.floor(s / 60)}m${String(Math.round(s % 60)).padStart(2, '0')}s`
    : `${Math.floor(s / 3600)}h${String(Math.floor((s % 3600) / 60)).padStart(2, '0')}m`)
  const kilo = x => (Math.abs(x) >= 1e6 ? `${(x / 1e6).toFixed(1)}M` : `${Math.round(x / 1e3)}K`)
  const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`
  // lspci's long name cut to the card's own, as `wb top` draws it (top.short)
  function cardName(name) {
    const n = name.replace(/\s*\(rev [0-9a-f]+\)\s*$/i, '').trim()
    if (n.endsWith(']')) { const all = [...n.matchAll(/\[([^\]]+)\]/g)]; return all.length ? all[all.length - 1][1] : n }
    return n.slice(n.lastIndexOf(']') + 1).trim() || n
  }
  // API dollars, savings.usd's rule: a cent's fraction to two figures, written out; a free model's $0
  function usd(x) {
    if (x === 0) return '$0'
    if (Math.abs(x) >= 0.01) return `$${x.toFixed(2)}`
    const r = Number(x.toPrecision(2))
    return Math.abs(r) >= 0.01 ? `$${r.toFixed(2)}` : `$${r.toFixed(2 - Math.floor(Math.log10(Math.abs(r)))).replace(/0+$/, '')}`
  }
  const isEst = prices => prices !== 'yours' && prices !== 'provider'
  const estUsd = (x, prices) => (x && isEst(prices) ? '≈' : '') + usd(x)
  const PRICES = { catalog: 'catalog prices', yours: 'your prices' }
  const pricesNote = prices => PRICES[typeof prices === 'string' ? prices : 'catalog'] ?? ''
  function apiMoney(a) {
    if (!a || !a.tasks) return ''
    const known = a.tasks - (a.unknown ?? 0) - (a.plan ?? 0)
    return known > 0 ? estUsd(a.cost ?? 0, a.prices) : a.plan ? 'plan usage' : 'no price'
  }
  // The words one model or provider goes by (the pane's Names): a GPU model by its label, an API
  // model as `API <provider's name> <the id's last part>`, a provider by its name; an id nothing
  // named stays the id. From the roles and models /api/stats carries, and wb top's rack.
  const providerWord = (id, n) => n.providers[id] || id
  function modelWord(id, api, n) {
    if (!api) return n.models[id] || id
    const cut = id.indexOf('/')  // the provider ends at the first /
    return cut > 0 ? `API ${providerWord(id.slice(0, cut), n)} ${id.split('/').pop()}` : `API ${id}`
  }
  function apiPeriod(a, n) {
    const money = apiMoney(a)
    if (!money) return ''
    const provs = Object.entries(a.by_provider ?? {}).filter(([, v]) => v.tasks > (v.unknown ?? 0) + (v.plan ?? 0))
      .sort((x, y) => y[1].cost - x[1].cost).map(([p, v]) => `${providerWord(p, n)} ${estUsd(v.cost, v.prices)}`)
    const inside = [provs.join(' · '), a.cost ? pricesNote(a.prices) : ''].filter(Boolean).join('; ')
    return `API ${money}${inside ? ` (${inside})` : ''}${a.unknown && money !== 'no price' ? ` + ${a.unknown} with no price` : ''}`
  }
  function apiParts(r) {
    if (r.via !== 'api') return []
    return [typeof r.spend === 'number' ? estUsd(r.spend, r.prices) : '', r.waits ? `waits for ${r.waits}` : '']
  }

  // ---- the history: statsLines, label for label; a row may carry a chart's numbers too ----
  // A model line's write trend (`trend`) and Last 30 days (`days`) are drawn, not spelled in
  // block characters; every other row is its text.
  function statsRows(f, why, n) {
    if (!f) return [{ label: '', text: why ? `History unavailable: ${why} (wb stats --json)` : 'Reading the history…' }]
    if (why) return [{ label: '', text: `Not updated: ${why} (wb stats --json)`, stale: true }, ...statsRows(f, '', n)]
    if (!f.tasks && !Object.keys(f).some(k => !['ts', 'until', 'settings', 'tasks'].includes(k)))
      return [{ label: '', text: 'No finished tasks yet: delegate one with /workbench, and how it went shows here.', empty: true }]
    const out = []
    const period = (name, label) => {
      const p = f.periods?.[name]
      if (!p) return
      const s = p.score
      const saved = p.savings?.saved ?? 0
      const parts = [
        s ? `score ${s.score}/100` : '',
        p.savings ? (saved >= 0 ? `saved ~${kilo(saved)} Claude tokens (estimate)` : `cost ~${kilo(-saved)} more Claude tokens (estimate)`) : '',
        plural(p.tasks, 'task'),
        `${p.landed} landed${p.accepted_pct !== undefined ? ` (${p.accepted_pct}%)` : ''}`,
        p.spec_gaps ? `and ${p.spec_gaps} discard${p.spec_gaps === 1 ? '' : 's'} put down to the spec` : '',
        p.energy?.wh ? `${p.energy.wh >= 1000 ? `${(p.energy.wh / 1000).toFixed(1)} kWh` : `${Math.round(p.energy.wh)} Wh`}` +
          (p.energy.cost != null ? `, cost ${p.energy.cost.toFixed(2)}` : '') : '',
        apiPeriod(p.api, n),
      ]
      out.push({ label, text: parts.filter(Boolean).join(' · ') })
    }
    period('week', 'This week')
    period('all', 'All time')
    if (f.streak) out.push({ label: 'Streak', text: f.streak.current
      ? `${plural(f.streak.current, 'day')} in a row with a task landed · best ${f.streak.best}`
      : `best: ${plural(f.streak.best, 'day')} in a row with a task landed` })
    const ab = f.absorbed
    if (ab) {
      const parts = [
        ab.auto_fixes ? `${ab.auto_fixes} test failure${ab.auto_fixes === 1 ? '' : 's'} sent back to the agent` +
          (ab.auto_fixed_tasks ? ` (${plural(ab.auto_fixed_tasks, 'task')} then green)` : '') : '',
        ab.handovers_landed ? `${ab.handovers_landed} landed after a handover to a fresh session` : '',
      ].filter(Boolean)
      if (parts.length) out.push({ label: 'Without waking Claude', text: parts.join(' · ') })
    }
    const q = f.questions
    if (q?.asked) out.push({ label: 'Questions', text: `${q.asked} asked mid-task` + (q.answered ? ` · ${q.answered} answered` : '') })
    const all = f.model_lines
      ? (f.model_lines.lines ?? []).map(m => [String(m.model), m])
      : Object.entries(f.models ?? {})
    const shown = all.slice(0, STATS_SHOWN), past = all.slice(STATS_SHOWN)
    const apiIds = new Set(all.filter(([, m]) => m.kind === 'api').map(([model]) => model))
    const word = model => modelWord(model, apiIds.has(model), n)
    for (const [model, m] of shown) {
      const trend = (m.write_trend ?? []).map(p => p[1]).filter(v => typeof v === 'number')
      const speed = m.kind === 'api' ? (typeof m.paid === 'number' ? ` · paid ~${usd(m.paid)}/task` : '')
        : m.speed?.write ? ` · writes ~${Math.round(m.speed.write)}${NB}tok/s` : ''
      const change = typeof m.write_change_pct === 'number' && m.write_change_pct
        ? ` (${m.write_change_pct > 0 ? '+' : ''}${m.write_change_pct}%)` : ''
      const windows = (m.windows ?? []).length > 1 ? ` (${m.windows.map(w => `${Math.round(w / 1024)}K`).join(', ')})` : ''
      out.push({ label: word(model) + windows, trend: trend.length > 1 && Math.max(...trend) > 0 ? trend : null,
        text: `${plural(m.tasks, 'task')} · ${m.accepted_pct}% landed` +
          (m.rounds != null ? ` · ${m.rounds} rounds` : '') + (m.minutes != null ? ` · ${m.minutes} min` : '') +
          speed + change + (m.claude ? ` · Claude ~${kilo(m.claude)}/task` : '') })
    }
    const older = f.model_lines?.older
    const moreModels = (f.model_lines?.more?.models ?? 0) + past.length
    const moreTasks = (f.model_lines?.more?.tasks ?? 0) + past.reduce((s, [, m]) => s + (Number(m.tasks) || 0), 0)
    const others = moreModels + (older?.models ?? 0), otherTasks = moreTasks + (older?.tasks ?? 0)
    if (others) out.push({ label: 'Other models',
      text: [moreModels ? `${moreModels} more` : '', older?.models ? `${older.models} older` : ''].filter(Boolean).join(' · ') +
        ` model${others === 1 ? '' : 's'}: ${plural(otherTasks, 'task')} (wb stats --all)` })
    const ids = shown.length ? new Set(shown.flatMap(([model, m]) => [model, ...(m.variants ?? [])])) : null
    const pick = entries => {
      const kept = entries.filter(([m], i) => (ids ? ids.has(m) : i < STATS_SHOWN))
      return [kept, entries.length - kept.length]
    }
    const counted = (kept, rest, report) => kept.length
      ? kept.join(' · ') + (rest ? ` · ${rest} more (${report})` : '') : `${rest} model${rest === 1 ? '' : 's'} (${report})`
    const sized = Object.entries(f.sizes ?? {})
    if (sized.length) out.push({ label: 'By size', text: sized.map(([k, v]) => `${ROLE_WORDS[k] ?? k} ${v.tasks} (${v.accepted_pct}%)`).join(' · ') })
    if (f.days?.length && f.days.some(d => d.tasks > 0)) out.push({ label: 'Last 30 days', text: 'tasks per day', days: f.days })
    const bests = [
      f.bests?.largest_first_try ? `largest landed first try: ${f.bests.largest_first_try.lines} lines (${f.bests.largest_first_try.task})` : '',
      f.bests?.fastest ? `fastest landing: ${f.bests.fastest.minutes} min (${f.bests.fastest.task})` : '',
    ].filter(Boolean)
    if (bests.length) out.push({ label: 'Bests', text: bests.join(' · ') })
    const last = (f.milestones ?? [])[(f.milestones ?? []).length - 1]
    if (last) out.push({ label: 'Milestone', text: last.kind === 'landed'
      ? `${plural(last.n, 'task')} landed${last.day ? ` (${last.day})` : ''}`
      : `~${kilo(last.n)} Claude tokens saved (estimate${last.day ? `, ${last.day}` : ''})` })
    const guards = Object.entries(f.guards ?? {})
    if (guards.length) out.push({ label: 'What stopped agents', text: guards.sort((a, b) => b[1] - a[1])
      .map(([g, k]) => `${GUARD_WORDS[g] ?? g} ${k}`).join(' · ') })
    const [helps, helpsRest] = pick(Object.entries(f.helps ?? {}))
    const helped = helps.map(([model, h]) => [model, Object.entries(h).filter(([, v]) => v[1])
      .map(([k, v]) => `a ${k === 'smaller' ? 'smaller step' : k} helped ${v[0]} of ${v[1]}`)]).filter(([, said]) => said.length)
    helped.forEach(([model, said]) => out.push({ label: `What helps (${word(model)})`, text: said.join(' · ') }))
    if (helpsRest) out.push({ label: 'What helps', text: `${helped.length ? `${helpsRest} more` : helpsRest} model${helpsRest === 1 ? '' : 's'} (wb stats --json)` })
    const [bench, benchRest] = pick(Object.entries(f.bench ?? {}))
    const sofar = p => `${p.of} of ${p.planned} ${p.running ? 'so far' : 'before it stopped'}` + (p.of ? `, ${p.passed} passed` : '')
    if (bench.length || benchRest) out.push({ label: 'Bench', text: counted(bench.map(([m, b]) => `${word(m)} ` +
      [b.run !== undefined ? `${b.passed}/${b.of} (${b.day})` : '', b.partial ? `newer run ${sofar(b.partial)}` : '']
        .filter(Boolean).join(', ')), benchRest, 'wb bench report') })
    const [opt, optRest] = pick(Object.entries(f.optimized ?? {}))
    if (opt.length || optRest) out.push({ label: 'Optimized', text: counted(opt.map(([m, o]) => `${word(m)} ${Math.round(o.window / 1024)}K` +
      (o.spill ? ` (${o.spill}% on the CPU)` : ' all on the GPU')), optRest, 'wb optimize report') })
    return out
  }

  // ---- drawing ----
  function el(tag, cls, ...kids) {
    const e = document.createElement(tag)
    if (cls) e.className = cls
    for (const k of kids) if (k != null && k !== false && k !== '') e.append(k)
    return e
  }
  function svg(tag, attrs = {}, ...kids) {
    const e = document.createElementNS(SVG, tag)
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, String(v))
    for (const k of kids) if (k) e.append(k)
    return e
  }
  const tip = text => { const t = document.createElementNS(SVG, 'title'); t.textContent = text; return t }
  // A line of values against a fixed top (busy %: 100) or their own highest: the pane's `spark`
  function sparkline(values, top, label) {
    const W = 120, H = 28, hi = top || Math.max(1e-9, ...values)
    const step = values.length > 1 ? W / (values.length - 1) : W
    const pts = values.map((v, i) => `${(i * step).toFixed(1)},${(H - 2 - (Math.max(0, Math.min(v, hi)) / hi) * (H - 4)).toFixed(1)}`)
    return svg('svg', { class: 'st-spark', viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: 'none', role: 'img', 'aria-label': label },
      svg('polyline', { points: [`0,${H}`, ...pts, `${((values.length - 1) * step).toFixed(1)},${H}`].join(' '), class: 'st-spark-fill' }),
      svg('polyline', { points: pts.join(' '), class: 'st-spark-line' }))
  }
  // Last 30 days: a bar a day, its tasks, the landed part of it in the accent
  function dayBars(days) {
    const W = 10, H = 56, top = Math.max(1, ...days.map(d => d.tasks || 0))
    const bars = days.map((d, i) => {
      const t = d.tasks || 0, l = Math.min(t, d.landed || 0)
      const ht = t ? Math.max(2, (t / top) * H) : 0, hl = l ? Math.max(2, (l / top) * H) : 0
      return svg('g', { class: 'st-day' }, tip(`${d.day}: ${plural(t, 'task')}, ${l} landed`),
        svg('rect', { x: i * W + 1, y: 0, width: W - 2, height: H, class: 'st-day-hit' }),
        t ? svg('rect', { x: i * W + 1, y: H - ht, width: W - 2, height: ht, rx: 1.5, class: 'st-day-all' }) : null,
        l ? svg('rect', { x: i * W + 1, y: H - hl, width: W - 2, height: hl, rx: 1.5, class: 'st-day-landed' }) : null)
    })
    const total = days.reduce((s, d) => s + (d.tasks || 0), 0), landed = days.reduce((s, d) => s + (d.landed || 0), 0)
    const chart = svg('svg', { class: 'st-days', viewBox: `0 0 ${days.length * W} ${H}`, preserveAspectRatio: 'none', role: 'img',
      'aria-label': `Tasks per day over the last ${days.length} days: ${total}, ${landed} landed` },
    svg('line', { x1: 0, y1: H - 0.5, x2: days.length * W, y2: H - 0.5, class: 'st-axis' }), ...bars)
    const ends = el('div', 'st-days-ends', el('span', '', days[0].day), el('span', '', days[days.length - 1].day))
    return el('div', 'st-days-box', chart, ends)
  }
  // A fill bar (memory, busy), its tone by how full: the pane's thresholds
  function meter(label, frac, said, tone) {
    const fill = el('i', `st-fill ${tone}`)
    fill.style.width = `${Math.round(Math.max(0, Math.min(1, frac)) * 1000) / 10}%`
    const track = el('span', 'st-track', fill)
    track.setAttribute('role', 'meter'); track.setAttribute('aria-label', label)
    track.setAttribute('aria-valuemin', '0'); track.setAttribute('aria-valuemax', '100'); track.setAttribute('aria-valuenow', String(Math.round(frac * 100)))
    return el('div', 'st-meter', el('span', 'st-meter-l', label), track, el('span', 'st-meter-n', said))
  }
  // a GPU's samples by its index; the RAM row has none, and kept under GPU 0's it drew GPU 0's line
  const busyKey = g => (g.ram ? 'ram' : String(g.index ?? 0))
  const card = (cls, title, ...kids) => el('div', `card ${cls}`, title ? el('h3', '', title) : null, ...kids)

  // Now: liveLines, as cards: a card per GPU (and RAM), Running, Models (the rack), Queue
  function nowCards(t, why, n) {
    if (!t) return [card('st-now-wait', '', el('p', 'small', why ? `This machine can't be read: ${why} (wb top --json)` : 'Reading this machine…'))]
    const out = []
    for (const g of t.gpus ?? []) {
      const title = el('span', '', g.ram ? 'RAM' : `GPU ${g.index ?? ''}`.trim())
      // a RAM row's name is "RAM" or "RAM (no GPU)": its title says the first word already
      const name = g.ram ? String(g.name ?? '').replace(/^RAM\s*/, '') : cardName(String(g.name ?? ''))
      const head = el('h3', 'st-gpu-h', title, name ? el('span', 'st-dim', ' ' + name) : null)
      const body = [head]
      if (typeof g.used === 'number' && g.total) {
        const f = g.used / g.total
        body.push(meter('Memory', f, `${gib(g.used)}/${gib(g.total)} GiB`, f > 0.95 ? 'bad' : f > 0.8 ? 'warn' : 'ok'))
      } else if (g.total) body.push(el('p', 'small', `${gib(g.total)} GiB${g.unified ? ' unified' : ''}`))
      const facts = [
        typeof g.limit === 'number' && g.limit ? `GPU may wire ${gib(g.limit)} GiB` : '',
        g.stale ? 'no reading' : '',
        typeof g.watts === 'number' ? `${Math.round(g.watts)} W` : '',
      ].filter(Boolean)
      if (typeof g.busy === 'number') body.push(meter('Busy', g.busy / 100, `${g.busy}%`, g.busy > 80 ? 'warn' : 'ok'))
      const samples = typeof g.busy === 'number' ? t.busy[busyKey(g)] ?? [] : []
      if (samples.length > 1) body.push(sparkline(samples, 100, `Busy over the last ${samples.length} readings`))
      if (facts.length) body.push(el('p', 'small st-facts', facts.join(' · ')))
      out.push(el('div', 'card st-gpu', ...body))
    }
    const running = t.running ?? []
    out.push(card('st-running', 'Running', running.length ? el('ul', 'st-list', ...running.map(r => {
      const parts = [r.phase, r.round ? `round ${r.round}` : '', typeof r.round_s === 'number' ? secs(r.round_s) : '',
        typeof r.tok_s === 'number' ? `${Math.round(r.tok_s)} tok/s${r.tok_s_from === 'live' ? '' : ' (last call)'}` : '',
        typeof r.context_pct === 'number' ? `context ${r.context_pct}%` : '', ...apiParts(r)].filter(Boolean)
      const model = r.via === 'api' ? modelWord(String(r.model ?? ''), true, n) : r.label || r.model
      return el('li', 'st-run', el('span', 'st-mark claude', '▶'), el('b', '', String(r.task ?? '')),
        el('span', 'st-dim', ` ${model} · ${parts.join(' · ')}`))
    })) : el('p', 'small', 'Nothing running.')))
    const rack = t.rack ?? []
    const folds = rack.filter(m => RACK_FOLD.has(m.state) && !(m.tasks ?? []).length)
    const fold = folds.length > RACK_FOLD_MIN ? folds.length : 0
    const rows = (fold ? rack.filter(m => !folds.includes(m)) : rack).map(m => {
      const where = m.state === 'cpu' && typeof m.cpu_bytes === 'number' ? ` ${gib(m.cpu_bytes)} GiB in RAM`
        : typeof m.gpu_bytes === 'number' ? ` ${gib(m.gpu_bytes)} GiB on the GPU${m.cpu_bytes ? ` + ${gib(m.cpu_bytes)} on the CPU` : ''}` : ''
      const facts = [m.write_tok_s ? `writes ~${Math.round(m.write_tok_s)}${NB}tok/s` : '', m.read_tok_s ? `reads ~${Math.round(m.read_tok_s)}${NB}tok/s` : '',
        m.load_s ? `loads ~${secs(m.load_s)}` : '', m.state === 'remote' ? 'another machine: no telemetry' : m.why ?? ''].filter(Boolean)
      const tone = RACK_TONE[m.state] || 'dim'
      return el('li', 'st-model', el('span', `st-mark ${tone}`, RACK_MARK[m.state] ?? '•'), el('span', '', `${m.label || m.model} `),
        el('span', tone, `${m.state}${where}`), facts.length ? el('span', 'st-dim', ` · ${facts.join(' · ')}`) : null,
        (m.tasks ?? []).length ? el('span', 'claude', ` ← ${m.tasks.join(', ')}`) : null)
    })
    if (fold) rows.push(el('li', 'st-model st-fold st-dim', `+${fold} more models idle, can't run or remote (wb top)`))
    if (rows.length) out.push(card('st-rack', 'Models', el('ul', 'st-list', ...rows)))
    const queue = t.queue ?? []
    if (queue.length) out.push(card('st-queue', 'Queue', el('ul', 'st-list', ...queue.map(q => el('li', 'st-wait',
      el('span', 'st-mark warn', '◌'), el('span', '', `${q.task} `),
      el('span', 'st-dim', `waits for the GPU${typeof q.waited_s === 'number' ? ` (${secs(q.waited_s)})` : ''}`))))))
    return out
  }

  // the rows as one card; a row with no label (Reading, unavailable, Not updated, the empty state)
  // is a line of its own above the rest
  function historyCards(rows) {
    const dl = el('dl', 'st-rows')
    const head = []
    for (const r of rows) {
      if (!r.label) { head.push(el('p', `small${r.stale ? ' st-note' : ''}${r.empty ? ' st-empty' : ''}`, r.text)); continue }
      const dd = el('dd', '')
      if (r.days) dd.append(dayBars(r.days), el('span', 'small', r.text))
      else {
        dd.append(r.text)
        if (r.trend) dd.append(sparkline(r.trend, 0, `${r.label}: its writing speed over its last tasks`))
      }
      dl.append(el('div', 'st-row', el('dt', '', r.label), dd))
    }
    return [card('st-history', '', ...head, dl.childElementCount ? dl : null)]
  }

  // ---- the data ----
  let shown = false
  let feed = null, routes = null, models = null, statsWhy = '', statsCtrl = null, statsTimer = 0
  let snap = null, busy = {}, topWhy = '', topCtrl = null, topTimer = 0, topFails = 0
  function names() {
    const n = { models: {}, providers: {} }
    for (const g of (models && models.gpu) || []) if (g && g.label) n.models[g.id] = g.label
    for (const m of (snap && snap.rack) || []) if (m && m.label && !n.models[m.model]) n.models[m.model] = m.label
    for (const r of (snap && snap.running) || []) if (r && r.via !== 'api' && r.label && !n.models[r.model]) n.models[r.model] = r.label
    for (const [id, p] of Object.entries((routes && routes.providers) || {})) if (p && p.name) n.providers[id] = p.name
    for (const a of (models && models.api) || []) if (a && a.provider && a.provider_name && !n.providers[a.provider]) n.providers[a.provider] = a.provider_name
    return n
  }
  const live = () => !!(feed && feed.settings && feed.settings.live === true)
  const brief = e => String((e && e.message) || e || '').split('\n')[0].slice(0, 200) || 'it failed'
  async function refusal(r) {
    if (r.status === 401) return 'that key was not accepted'
    try { const j = await r.json(); if (j && j.error) return String(j.error) } catch {}
    return `HTTP ${r.status}`
  }

  // Now and the history in boxes of their own: a tick every 2 s redraws Now alone, so the history
  // isn't rebuilt under a reader selecting its text
  let nowBox = null, histBox = null
  function drawNow() {
    if (!nowBox) return
    nowBox.hidden = !live()
    nowBox.replaceChildren(...(live() ? [el('h2', 'st-h', 'Now'),
      el('div', 'cards st-now', ...nowCards(snap && { ...snap, busy }, topWhy, names()))] : []))
  }
  function drawHistory() {
    if (!histBox) return
    histBox.replaceChildren(el('h2', 'st-h', 'History'), el('div', 'cards st-hist', ...historyCards(statsRows(feed, statsWhy, names()))))
  }
  function draw() { drawNow(); drawHistory() }

  async function loadStats() {
    clearTimeout(statsTimer); statsTimer = 0
    if (!shown) return
    if (statsCtrl) statsCtrl.abort()
    const ctrl = new AbortController(); statsCtrl = ctrl
    try {
      const r = await WB.api('/api/stats', { signal: ctrl.signal })
      if (!r.ok) throw new Error(await refusal(r))
      const d = await r.json()
      if (!d || !d.feed || typeof d.feed !== 'object') throw new Error('not a feed')
      feed = d.feed; routes = d.routes || null; models = d.models || null; statsWhy = ''
    } catch (e) {
      if (ctrl.signal.aborted) return
      statsWhy = brief(e)
    }
    if (statsCtrl !== ctrl) return
    statsCtrl = null
    if (!shown) return
    draw()
    syncTop()
    // again every REFETCH_MS, and once the feed's day ends (its streak and periods count from today)
    const left = feed && typeof feed.until === 'number' ? feed.until * 1000 - Date.now() + 1000 : REFETCH_MS
    statsTimer = setTimeout(loadStats, Math.max(1000, Math.min(REFETCH_MS, left)))
  }

  // The live stream runs only while the view is shown (and so the page visible: the shell hides
  // the view with the page) and Live view is on; leaving aborts it, so the server stops `wb top`
  function syncTop() {
    if (shown && live()) { if (!topCtrl && !topTimer) openTop() }
    else stopTop()
  }
  function stopTop() {
    if (topCtrl) topCtrl.abort()
    topCtrl = null
    clearTimeout(topTimer); topTimer = 0
    snap = null; busy = {}; topWhy = ''; topFails = 0  // shown again, never the old tick as Now (#552)
  }
  function tick(s) {
    if (!s || typeof s !== 'object') return
    const next = {}
    for (const g of s.gpus ?? []) {
      const k = busyKey(g)
      if (typeof g.busy === 'number') next[k] = [...(busy[k] ?? []), g.busy].slice(-BUSY_KEEP)
    }
    const first = !snap
    snap = s; busy = next; topWhy = ''; topFails = 0
    first ? draw() : drawNow()  // the rack's labels name the history's models too
  }
  async function openTop() {
    const ctrl = new AbortController(); topCtrl = ctrl
    let ticked = false, why = 'the stream ended'
    try {
      const r = await WB.api('/api/top', { signal: ctrl.signal })
      if (!r.ok || !r.body) throw new Error(await refusal(r))
      const reader = r.body.getReader(), dec = new TextDecoder()
      // aborting the fetch alone can leave WebKit's socket open, and the server's child with it
      ctrl.signal.addEventListener('abort', () => { reader.cancel().catch(() => {}) }, { once: true })
      let buf = ''
      for (;;) {
        const { value, done } = await reader.read()
        if (done) break
        buf += dec.decode(value, { stream: true }).replace(/\r\n/g, '\n')
        let cut
        while ((cut = buf.indexOf('\n\n')) >= 0) {
          const block = buf.slice(0, cut); buf = buf.slice(cut + 2)
          let event = 'message', data = ''
          for (const line of block.split('\n')) {
            if (line.startsWith('event:')) event = line.slice(6).trim()
            else if (line.startsWith('data:')) data += (data ? '\n' : '') + line.slice(5).replace(/^ /, '')
          }
          if (topCtrl !== ctrl || !data) continue
          let d
          try { d = JSON.parse(data) } catch { continue }
          if (event === 'top') { ticked = true; tick(d) }
          else if (event === 'down') {
            // the server's child died: it says why and tries again itself after `retry` seconds
            snap = null; busy = {}
            topWhy = String((d && d.why) || 'wb top ended') + (d && typeof d.retry === 'number' ? `; trying again in ${Math.round(d.retry)}s` : '')
            drawNow()
          }
        }
      }
    } catch (e) { why = brief(e) }
    if (topCtrl !== ctrl) return  // stopped (left, hidden, Live view off): that call's over
    topCtrl = null
    // a stream that gave nothing says why, and waits longer each time; one that ticked dials soon
    if (!ticked) { snap = null; busy = {}; topWhy = why; topFails++ }
    const wait = TOP_RETRY_MS[Math.min(ticked ? 0 : topFails - 1, TOP_RETRY_MS.length - 1)]
    drawNow()
    topTimer = setTimeout(() => { topTimer = 0; syncTop() }, wait)
  }

  WB.view('stats', {
    mount(at) {
      nowBox = el('section', 'st-now-box'); nowBox.setAttribute('aria-label', 'Now')
      histBox = el('section', 'st-hist-box'); histBox.setAttribute('aria-label', 'History')
      at.replaceChildren(nowBox, histBox)
      draw()
    },
    show() { shown = true; draw(); loadStats() },
    hide() {
      shown = false
      clearTimeout(statsTimer); statsTimer = 0
      if (statsCtrl) { statsCtrl.abort(); statsCtrl = null }
      stopTop()
    },
  })
})()
