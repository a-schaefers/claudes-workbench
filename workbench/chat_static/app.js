/* The web chat's page. Three promises, kept the way merecat keeps them:
   - Nothing half-received. The server sends the whole thread in each `state` event, and every
     (re)connect starts with one, so a dropped connection loses nothing; replies are written
     whole by `wb chat reply`, and the paced reveal is only paint, finished instantly if the page
     is hidden or the reader prefers less motion.
   - Nothing sent twice or lost. A message goes into a local outbox with its own id first and is
     retried until the server has it; the server ignores an id it already holds.
   - Always reconnecting. A fetch-streamed event stream (it carries the token as a header, which
     EventSource can't), a heartbeat watchdog, jittered backoff, a pause while hidden and an
     instant resync on return or when the network comes back. */
(() => {
  'use strict'
  const K = { token: 'wb-chat-token', seen: 'wb-chat-seen', outbox: 'wb-chat-outbox', draft: 'wb-chat-draft', thread: 'wb-chat-thread', about: 'wb-chat-about' }
  const BACKOFF = [500, 1000, 2000, 4000, 8000, 15000, 30000]
  const DEAD_MS = 40000          // the server pings every 15 s: this long silent means the line is dead
  const CONNECT_MS = 12000       // a connection that never answers: dial again
  const HIDDEN_GRACE_MS = 60000  // a hidden page keeps its stream this long, then lets go
  const STALE_MS = 20000         // back on screen with nothing heard this long: the line is likely dead
  const POST_MS = 15000          // a send that hasn't answered by now is tried again
  const FLOW_MS = 40

  const $ = id => document.getElementById(id)
  const scroller = $('scroll'), thread = $('thread'), net = $('net'), input = $('input'),
    sendBtn = $('send'), jump = $('jump'), presence = $('presence'), presenceText = $('presence-text')

  const load = (k, d) => { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v) } catch { return d } }
  const save = (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)) } catch {} }

  // ---- the key ----
  let token = ''
  {
    // the link's fragment, or the installed app's start_url (`?token=`, from the keyed manifest)
    const m = location.hash.match(/token=([\w-]+)/) || location.search.match(/[?&]token=([\w-]+)/)
    if (m) { token = m[1]; save(K.token, token); history.replaceState(null, '', location.pathname) }
    else token = load(K.token, '')
  }
  // The app installs only from https. http://localhost is a secure context too, so Chromium
  // offered the install there as well, and one computer ended up with two apps of the same name,
  // the localhost one useless away from it (the user, 2026-11-18): over http the page has no
  // manifest, so no browser offers it
  const https = location.protocol === 'https:'
  function keyManifest() {
    // An installed app on iPhone has its own storage, not Safari's: the manifest it's installed
    // from carries the key, so it opens keyed (the user, 2026-11-18: no key to paste in the app)
    const link = document.querySelector('link[rel=manifest]')
    if (link && !https) link.remove()
    else if (link) link.href = '/manifest.webmanifest' + (token ? '?k=' + encodeURIComponent(token) : '')
  }
  keyManifest()

  // ---- state ----
  // Until the first live state, the thread this page last had, so a reload shows it at once
  let state = { messages: token ? load(K.thread, []) : [], asks: [], prefs: {}, push: {}, listener: null }
  let live = false
  let skew = 0                                  // server clock − ours, seconds
  let outbox = load(K.outbox, [])               // [{cid, text, ts, error?}]
  let seen = new Set(load(K.seen, null) || [])  // Claude's messages already shown in full
  let firstVisit = load(K.seen, null) === null  // the backlog shows whole; what comes after flows
  const nodes = new Map()                       // key → row element
  const flows = new Map()                       // message id → {text, shown, el}
  let unread = 0

  const now = () => Date.now() / 1000 + skew
  const reduced = matchMedia('(prefers-reduced-motion: reduce)')

  // ---- sticky follow (stays at the newest line unless the reader scrolls up) ----
  let follow = true, lastTop = 0, touchY = 0
  const nearEnd = () => scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 60
  const release = () => { follow = false; jump.hidden = nearEnd() }
  scroller.addEventListener('scroll', () => {
    const top = scroller.scrollTop
    if (top > lastTop + 2 && nearEnd()) follow = true       // the reader came back down
    else if (top < lastTop - 2 && !nearEnd()) follow = false // the reader went up
    lastTop = top
    jump.hidden = follow || nearEnd()
  }, { passive: true })
  scroller.addEventListener('wheel', e => { if (e.deltaY < 0) release() }, { passive: true })
  scroller.addEventListener('touchstart', e => { touchY = e.touches[0]?.clientY ?? 0 }, { passive: true })
  scroller.addEventListener('touchmove', e => {
    const y = e.touches[0]?.clientY ?? 0
    if (y > touchY + 8) release()                           // finger down = view up
    touchY = y
  }, { passive: true })
  const toEnd = force => {
    if (force || follow) { scroller.scrollTop = scroller.scrollHeight; lastTop = scroller.scrollTop; follow = true; jump.hidden = true }
  }
  jump.addEventListener('click', () => scroller.scrollTo({ top: scroller.scrollHeight, behavior: 'smooth' }))

  // ---- text → DOM, never HTML from the thread ----
  function inline(parent, text) {
    const re = /(`[^`\n]+`)|(\*\*[^*\n]+\*\*)|(https?:\/\/[^\s<>()]+[^\s<>().,;:!?'"])/g
    let i = 0, m
    while ((m = re.exec(text))) {
      if (m.index > i) parent.append(text.slice(i, m.index))
      let el
      if (m[1]) { el = document.createElement('code'); el.textContent = m[1].slice(1, -1) }
      else if (m[2]) { el = document.createElement('strong'); el.textContent = m[2].slice(2, -2) }
      else { el = document.createElement('a'); el.href = m[3]; el.textContent = m[3]; el.target = '_blank'; el.rel = 'noopener noreferrer' }
      parent.append(el); i = re.lastIndex
    }
    if (i < text.length) parent.append(text.slice(i))
  }
  function rich(el, text) {
    el.replaceChildren()
    const parts = text.split(/^```[^\n]*\n?/m)
    parts.forEach((part, n) => {
      if (n % 2) { const pre = document.createElement('pre'), c = document.createElement('code'); c.textContent = part.replace(/\n$/, ''); pre.append(c); el.append(pre); return }
      for (const block of part.split(/\n{2,}/)) {
        if (!block.trim()) continue
        const lines = block.split('\n')
        const bullet = /^\s*(?:[-*•]|\d+[.)])\s+/
        if (lines.every(l => bullet.test(l))) {
          const list = document.createElement(/^\s*\d/.test(lines[0]) ? 'ol' : 'ul')
          for (const l of lines) { const li = document.createElement('li'); inline(li, l.replace(bullet, '')); list.append(li) }
          el.append(list)
        } else {
          const p = document.createElement('p')
          lines.forEach((l, j) => { if (j) p.append(document.createElement('br')); inline(p, l) })
          el.append(p)
        }
      }
    })
  }

  // ---- the paced reveal ----
  let flowTimer = 0
  function flowTick() {
    let busy = false
    for (const [id, f] of flows) {
      const backlog = f.text.length - f.shown
      if (backlog > 0) {
        f.shown = Math.min(f.text.length, f.shown + Math.max(2, Math.ceil(backlog / 15)))
        f.el.textContent = f.text.slice(0, f.shown)
        busy = true
      } else finishFlow(id)
    }
    toEnd()
    if (!busy && !flows.size) { clearInterval(flowTimer); flowTimer = 0 }
  }
  function finishFlow(id) {
    const f = flows.get(id)
    if (!f) return
    flows.delete(id)
    f.el.classList.remove('flowing')
    rich(f.el, f.text)
    seen.add(id); save(K.seen, [...seen].slice(-500))
  }
  const finishAll = () => { for (const id of [...flows.keys()]) finishFlow(id); toEnd() }

  // ---- drawing ----
  const clock = ts => new Date(ts * 1000).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
  function dayName(ts) {
    const d = new Date(ts * 1000), t = new Date()
    const y = new Date(t); y.setDate(t.getDate() - 1)
    if (d.toDateString() === t.toDateString()) return 'Today'
    if (d.toDateString() === y.toDateString()) return 'Yesterday'
    return d.toLocaleDateString([], { weekday: 'long', month: 'short', day: 'numeric' })
  }
  const span = (cls, text) => { const s = document.createElement('span'); if (cls) s.className = cls; s.textContent = text; return s }
  const since = ts => { const s = Math.max(0, Math.round(now() - ts)); return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, '0')}s` }

  function items() {
    const onServer = new Set(state.messages.map(m => m.cid).filter(Boolean))
    const pending = outbox.filter(o => !onServer.has(o.cid))
      .map(o => ({ id: 'local-' + o.cid, cid: o.cid, from: 'user', text: o.text, ts: o.ts, local: true, error: o.error, about: o.about }))
    // permission prompts sit in the thread where they were asked
    const asks = state.asks.map(a => ({ id: a.id, from: 'ask', ts: a.ts, ask: a }))
    return [...state.messages, ...asks].sort((x, y) => x.ts - y.ts).concat(pending)
  }

  function metaFor(m) {
    const meta = document.createElement('div'); meta.className = 'meta'
    if (m.ask) {
      const a = m.ask
      const yes = { plan: 'approved', question: 'you answered' }[a.kind] || (a.by === 'setting' ? 'allowed by your setting' : 'you allowed it')
      const no = { plan: 'you asked for changes', question: 'you passed' }[a.kind] || 'you said no'
      meta.append(span('', clock(a.ts) + (a.answer === 'allow' ? ` · ${yes} ` + (a.by === 'setting' ? '' : clock(a.answered))
        : a.answer === 'deny' ? ` · ${no} ` + clock(a.answered)
        : a.answer === 'expired' ? (a.by === 'interrupted' ? ' · settled in the terminal' : ' · no answer in time: it waits in the terminal')
        : ' · Claude is waiting for your answer')))
      return meta
    }
    if (m.from === 'claude') {
      meta.append(span('', 'Claude · ' + clock(m.ts) + ' · '))
      const c = document.createElement('button'); c.textContent = 'copy'
      c.onclick = async () => {
        try { await navigator.clipboard.writeText(m.text); c.textContent = 'copied' }
        catch { c.textContent = 'copy failed' }
        setTimeout(() => { c.textContent = 'copy' }, 1500)
      }
      meta.append(c)
      return meta
    }
    if (m.local) {
      if (m.error) {
        meta.append(span('', 'Not sent: ' + m.error + ' · '))
        const b = document.createElement('button'); b.textContent = 'retry'
        b.onclick = () => { const o = outbox.find(x => x.cid === m.cid); if (o) { delete o.error; save(K.outbox, outbox) } flush(true); render() }
        const d = document.createElement('button'); d.textContent = 'delete'
        d.onclick = () => { outbox = outbox.filter(x => x.cid !== m.cid); save(K.outbox, outbox); render() }
        const ed = document.createElement('button'); ed.textContent = 'edit'
        ed.onclick = () => {
          outbox = outbox.filter(x => x.cid !== m.cid); save(K.outbox, outbox)
          input.value = m.text; save(K.draft, m.text); grow(); sendBtn.disabled = false
          render(); input.focus()
        }
        meta.append(b, span('', ' · '), ed, span('', ' · '), d)
      } else meta.append(span('', online ? 'Sending…' : 'Waiting for the connection…'))
      return meta
    }
    // one short line, one time: the receipt's once Claude has it (#103)
    if (m.received) meta.append(span('tick', '✓✓'), span('', (m.received.how === 'queued'
      ? 'Queued for Claude · ' : 'Read by Claude · ') + clock(m.received.ts)))
    else meta.append(span('tick', '✓'), span('', (state.listener ? 'Delivered · ' : 'Waiting for a Claude session · ')
      + clock(m.ts)))
    return meta
  }

  for (const chip of document.querySelectorAll('.chip')) {
    chip.addEventListener('click', () => { input.value = chip.textContent; grow(); sendBtn.disabled = false; input.focus() })
  }

  function render() {
    const list = items()
    $('intro').hidden = list.length > 0
    const want = []
    let prev = null
    for (const m of list) {
      const day = dayName(m.ts)
      if (!prev || dayName(prev.ts) !== day) want.push({ key: 'day-' + day, day })
      want.push({ key: m.ask ? m.id + ':' + (m.ask.answer || 'open') : m.local ? 'local-' + m.cid : (m.cid && nodes.has('local-' + m.cid) ? 'local-' + m.cid : m.id), m,
                  same: prev && prev.from === m.from && m.ts - prev.ts < 120 })
      prev = m
    }
    const keep = new Set(want.map(w => w.key))
    for (const [k, el] of nodes) if (!keep.has(k)) { el.remove(); nodes.delete(k) }
    let anchor = $('intro')
    for (const w of want) {
      let el = nodes.get(w.key)
      if (!el) {
        if (w.day) { el = span('day', w.day); el.className = 'day' }
        else el = makeRow(w.m)
        nodes.set(w.key, el)
      }
      if (w.m) {
        el.className = `row ${w.m.from}${w.same ? ' same' : ''}${w.m.local ? ' pending' : ''}${w.m.error ? ' failed' : ''}${w.m.ask && w.m.ask.answer ? ' done' : ''}`
        el.querySelector('.meta').replaceWith(metaFor(w.m))
      }
      if (anchor.nextSibling !== el) anchor.after(el)
      anchor = el
    }
    drawWorking(list, anchor)
    drawPresence()
    toEnd()
    if (list.length) firstVisit = false
  }

  // An ask: a permission (Allow, Deny), a finished plan (Approve, Keep planning with a note) or
  // Claude's question (its options, one or several, or the user's own words)
  function makeAsk(a) {
    const row = document.createElement('div')
    const card = document.createElement('div'); card.className = 'card'
    const head = document.createElement('div'); head.className = 'asks'
    const open = !a.answer, d = a.detail || {}
    // the decision first, when Claude said what the call is for; the exact command under it (#103)
    const purpose = typeof d.purpose === 'string' ? d.purpose.trim().replace(/\.$/, '') : ''
    head.textContent = a.kind === 'plan' ? (open ? "Claude's plan is ready" : "Claude's plan")
      : a.kind === 'question' ? (open ? 'Claude asks' : 'Claude asked')
      : purpose ? (open ? 'Allow Claude to ' : 'Claude asked to ') + purpose.charAt(0).toLowerCase() + purpose.slice(1)
        + (open ? '?' : '') + ' (' + a.tool + ')'
      : (open ? 'Claude wants to use ' : 'Claude asked to use ') + a.tool
    card.append(head)
    if (a.kind === 'plan') {
      const body = document.createElement('div'); body.className = 'plan bubble-text'
      rich(body, d.plan || '(the plan is in the terminal)')
      card.append(body)
      if (d.plan && (d.plan.length > 900 || d.plan.split('\n').length > 14)) {  // long: folded, with a way to read it all
        body.classList.add('folded')
        const more = document.createElement('button'); more.type = 'button'; more.className = 'more'; more.textContent = 'Show the whole plan'
        more.onclick = () => { body.classList.remove('folded'); more.remove() }
        card.append(more)
      }
    } else if (a.kind !== 'question') {
      const what = document.createElement('pre'); what.textContent = a.text; card.append(what)
    }
    const said = span('said', '')
    const answer = async (verdict, extra) => {
      card.querySelectorAll('button, input, textarea').forEach(b => { b.disabled = true }); said.textContent = ''
      try {
        const r = await post('/api/answer', { id: a.id, answer: verdict, ...extra })
        const j = await r.json().catch(() => ({}))
        if (r.ok && j.state) return apply(j.state)
        if (r.status === 401) return keyless('That key was not accepted.')
        said.textContent = j.error || 'not sent: try again'
      } catch { said.textContent = 'not sent: try again' }
      card.querySelectorAll('button, input, textarea').forEach(b => { b.disabled = false })
    }
    const button = (cls, text, fn) => { const b = document.createElement('button'); b.type = 'button'; b.className = cls; b.textContent = text; b.onclick = fn; return b }
    if (a.kind === 'question') {
      const picks = {}
      let submit = null
      for (const q of d.questions || []) {
        const box = document.createElement('div'); box.className = 'q'
        const qt = document.createElement('div'); qt.className = 'qt'; qt.textContent = q.question
        box.append(qt)
        const done = a.answers && a.answers[q.question]
        if (!open) { if (done) { const p = document.createElement('div'); p.className = 'picked'; p.textContent = done; box.append(p) } card.append(box); continue }
        const chosen = new Set()
        const opts = document.createElement('div'); opts.className = 'opts'
        const other = document.createElement('input'); other.className = 'other'; other.placeholder = 'Or say something else'
        const sync = () => {
          const typed = other.value.trim()
          picks[q.question] = typed ? typed : [...chosen].join(', ')
          if (submit) submit.disabled = !(d.questions || []).every(x => picks[x.question])
        }
        for (const o of q.options || []) {
          const b = button('opt', '', () => {
            if (!q.multiSelect) { chosen.clear(); opts.querySelectorAll('.opt').forEach(x => x.classList.remove('on')) }
            if (chosen.has(o.label)) { chosen.delete(o.label); b.classList.remove('on') } else { chosen.add(o.label); b.classList.add('on') }
            other.value = ''; sync()
          })
          const l = document.createElement('span'); l.className = 'label'; l.textContent = o.label
          b.append(l)
          if (o.description) { const ds = document.createElement('span'); ds.className = 'desc'; ds.textContent = o.description; b.append(ds) }
          opts.append(b)
        }
        other.addEventListener('input', () => {
          if (other.value.trim()) { chosen.clear(); opts.querySelectorAll('.opt').forEach(x => x.classList.remove('on')) }
          sync()
        })
        box.append(opts, other)
        card.append(box)
      }
      if (open) {
        submit = button('allow', 'Answer', () => answer('allow', { answers: picks }))
        submit.disabled = true
        const choices = document.createElement('div'); choices.className = 'choices'
        choices.append(button('deny', 'Skip', () => answer('deny')), submit)
        card.append(choices)
      }
    } else if (open && a.kind === 'plan') {
      const note = document.createElement('textarea'); note.className = 'note'; note.rows = 2
      note.placeholder = 'What should change? (optional)'; note.hidden = true
      const back = button('deny', 'Keep planning', () => {
        if (note.hidden) { note.hidden = false; back.textContent = 'Send back'; note.focus(); return }
        answer('deny', note.value.trim() ? { note: note.value.trim() } : {})
      })
      const choices = document.createElement('div'); choices.className = 'choices'
      choices.append(back)
      const where = document.createElement('p'); where.className = 'hint'
      where.textContent = 'To approve it, choose Yes in the terminal: Claude Code leaves plan mode only from there.'
      card.append(note, choices, where)
    } else if (open) {
      const choices = document.createElement('div'); choices.className = 'choices'
      choices.append(button('deny', 'Deny', () => answer('deny')), button('allow', 'Allow', () => answer('allow')))
      card.append(choices)
    }
    if (!open && a.note) { const n = document.createElement('div'); n.className = 'picked'; n.textContent = '“' + a.note + '”'; card.append(n) }
    card.append(said)
    if (open && document.hidden) document.title = a.kind === 'permission' ? 'Claude asks: allow?' : 'Claude needs you'
    row.append(card, document.createElement('div'))
    row.lastChild.className = 'meta'
    return row
  }

  // ---- wb shot's pairs (#99): images behind the token, so fetched with it, shown as blobs ----
  const shotOf = s => `${s.task} · step ${s.step} · round ${s.round}`
  function shotImage(s, side, fig) {
    if (!s[side].image) { fig.prepend(span('missing', 'No image: the capture failed. The code review stands on its own.')); return }
    const img = document.createElement('img')
    img.alt = `${side === 'base' ? 'Base' : 'Candidate'} of ${shotOf(s)}`
    fig.prepend(img)
    fetch(`/api/shot?task=${encodeURIComponent(s.task)}&n=${s.n}&side=${side}`,
          { headers: { Authorization: 'Bearer ' + token }, cache: 'no-store' })
      .then(r => r.ok ? r.blob() : Promise.reject(r.status))
      .then(b => { img.src = URL.createObjectURL(b) })
      .catch(() => img.replaceWith(span('missing', 'This image could not be loaded.')))
  }
  function makeShots(s) {
    const box = document.createElement('div')
    const grid = document.createElement('div'); grid.className = 'shots'
    for (const side of ['base', 'candidate']) {
      const fig = document.createElement('figure')
      const cap = document.createElement('figcaption')
      cap.append(Object.assign(document.createElement('b'), { textContent: side === 'base' ? 'Base' : 'Candidate' }),
                 span('', ' · ' + String(s[side].rev || '').slice(0, 10)))
      fig.append(cap); shotImage(s, side, fig); grid.append(fig)
    }
    const of = document.createElement('div'); of.className = 'shots-of'
    const reply = document.createElement('button'); reply.type = 'button'; reply.textContent = 'Reply about these'
    reply.onclick = () => { setAbout({ task: s.task, n: s.n, step: s.step }); input.focus() }
    of.append(span('', `${shotOf(s)} · ${s.viewport} · `), reply)
    box.append(grid, of)
    return box
  }
  // what the next message is about: a pair of screenshots, so the reply reaches its task and step
  let about = load(K.about, null)
  function setAbout(a) {
    about = a; save(K.about, a)
    $('about').hidden = !a
    $('about-text').textContent = a ? `Replying about ${a.task}, step ${a.step}` : ''
  }
  $('about-x').onclick = () => setAbout(null)
  setAbout(about)

  function makeRow(m) {
    if (m.ask) return makeAsk(m.ask)
    const row = document.createElement('div')
    const b = document.createElement('div'); b.className = 'bubble'
    if (m.about) row.append(span('about-tag', `About ${m.about.task}, step ${m.about.step}`))
    if (m.from === 'user') b.textContent = m.text
    else if (seen.has(m.id) || firstVisit || document.hidden || reduced.matches) {
      if (!seen.has(m.id) && !firstVisit && document.hidden) { unread++; document.title = `(${unread}) Workbench` }
      rich(b, m.text); seen.add(m.id); save(K.seen, [...seen].slice(-500))
    } else {
      b.classList.add('flowing'); flows.set(m.id, { text: m.text, shown: 0, el: b })
      if (!flowTimer) flowTimer = setInterval(flowTick, FLOW_MS)
    }
    row.append(b)
    if (m.shots) row.append(makeShots(m.shots))
    row.append(document.createElement('div'))
    row.lastChild.className = 'meta'
    return row
  }

  // Claude has the latest message and is mid-turn: say so, counting from when it was read
  let working = null, workTimer = 0
  function drawWorking(list, anchor) {
    const last = [...list].reverse().find(m => !m.local)
    const on = last && last.from === 'user' && last.received && state.listener && state.listener.busy
    if (!on) { if (working) { working.remove(); working = null; clearInterval(workTimer); workTimer = 0 } return }
    if (!working) {
      working = document.createElement('div'); working.className = 'working'
      const dots = document.createElement('span'); dots.className = 'dots'; dots.append(document.createElement('i'), document.createElement('i'), document.createElement('i'))
      working.append(dots, span('', ''))
      workTimer = setInterval(() => { if (working) working.lastChild.textContent = 'Claude is on it · ' + since(Number(working.dataset.from)) }, 1000)
    }
    working.dataset.from = last.received.ts
    working.lastChild.textContent = 'Claude is on it · ' + since(last.received.ts)
    if (anchor.nextSibling !== working) anchor.after(working)
  }

  function drawPresence() {
    const l = state.listener
    const where = l ? l.cwd.replace(/^\/home\/[^/]+/, '~') : ''
    presence.className = 'presence ' + (!l ? 'away' : l.busy ? 'working' : 'listening')
    presenceText.textContent = !token ? 'not connected' : !live ? 'connecting…' : !l ? 'no session listening · messages wait' :
      // the app's name above, whom you talk to here (#103: a notification opened on "Claude")
      'Claude · ' + (l.busy ? 'working in ' : 'listening in ') + where
  }

  // ---- the connection ----
  let online = navigator.onLine, conn = null, failures = 0, retryTimer = 0, lastRx = 0, hiddenTimer = 0
  let build = ''  // the page's files when this page loaded (the server's `build`)
  function banner(text, bad) {
    if (!text) { net.hidden = true; return }
    net.textContent = text; net.className = 'net' + (bad ? ' bad' : ''); net.hidden = false
  }
  function apply(s) {
    if (typeof s.now === 'number') skew = s.now - Date.now() / 1000
    if (s.build && build && s.build !== build && reloadOnce()) return  // updated: the new page
    if (s.build) build = s.build
    live = true
    save(K.thread, (s.messages || []).slice(-50))
    state = { messages: s.messages || [], asks: s.asks || [], prefs: s.prefs || {}, push: s.push || {}, listener: s.listener || null, savings: s.savings || null }
    if (!sheet.hidden) drawSettings()
    if (state.push.key && !pushSynced) { pushSynced = true; syncPush() }
    reportSeen()
    const onServer = new Set(state.messages.map(m => m.cid).filter(Boolean))
    if (outbox.some(o => onServer.has(o.cid))) { outbox = outbox.filter(o => !onServer.has(o.cid)); save(K.outbox, outbox) }
    render()
  }
  function reloadOnce() {
    // at most once a minute, so a page whose files keep changing can't reload for ever; the draft
    // and the outbox are in localStorage already
    let last = 0
    try { last = Number(sessionStorage.getItem('wb-chat-reloaded')) || 0 } catch {}
    if (Date.now() - last < 60000) return false  // carry on with this page, taking the new build
    try { sessionStorage.setItem('wb-chat-reloaded', String(Date.now())) } catch {}
    location.reload()
    return true
  }
  async function post(path, body) {  // a POST that gives up after POST_MS, so a dead line can't hold it
    const ctrl = new AbortController(), timer = setTimeout(() => ctrl.abort(), POST_MS)
    try {
      return await fetch(path, { method: 'POST', cache: 'no-store', signal: ctrl.signal,
        headers: { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
    } finally { clearTimeout(timer) }
  }
  async function connect() {
    if (conn || !token || document.hidden) return
    clearTimeout(retryTimer)
    const ctrl = new AbortController(); conn = ctrl
    lastRx = Date.now()  // the watchdog judges this dial from now, not from the last line's silence
    const dialTimer = setTimeout(() => ctrl.abort(), CONNECT_MS)
    if (failures) banner('Reconnecting…')
    try {
      const r = await fetch('/api/stream', { headers: { Authorization: 'Bearer ' + token }, signal: ctrl.signal, cache: 'no-store' })
      clearTimeout(dialTimer)
      if (r.status === 401) { conn = null; keyless('That key was not accepted.'); return }
      if (!r.ok || !r.body) throw new Error('HTTP ' + r.status)
      const reader = r.body.getReader(), dec = new TextDecoder()
      // aborting the fetch alone can leave WebKit's socket open, and the server's stream slot
      // with it: cancel the body too
      ctrl.signal.addEventListener('abort', () => { reader.cancel().catch(() => {}) }, { once: true })
      let buf = ''
      lastRx = Date.now()
      for (;;) {
        const { value, done } = await reader.read()
        if (done) break
        lastRx = Date.now()
        buf += dec.decode(value, { stream: true })
        let cut
        while ((cut = buf.indexOf('\n\n')) >= 0) {
          const block = buf.slice(0, cut); buf = buf.slice(cut + 2)
          let event = 'message', data = ''
          for (const line of block.split('\n')) {
            if (line.startsWith('event:')) event = line.slice(6).trim()
            else if (line.startsWith('data:')) data += (data ? '\n' : '') + line.slice(5).replace(/^ /, '')
          }
          if (event === 'state' && data) {
            try { apply(JSON.parse(data)) } catch {}
            if (failures) { failures = 0; banner('') }
            flush()
          }
        }
      }
    } catch (e) { /* dropped, aborted or refused: retried below */ }
    clearTimeout(dialTimer)
    if (conn === ctrl) { conn = null; retry() }
  }
  function retry() {
    if (document.hidden) return  // the return resyncs
    // offline: still try, slowly, since a phone doesn't always say when it's back online
    const base = online ? BACKOFF[Math.min(failures, BACKOFF.length - 1)] : BACKOFF[BACKOFF.length - 1]
    failures++
    const wait = Math.round(base * (0.8 + Math.random() * 0.4))
    if (!online) banner('Offline. Messages will send when the connection is back.', true)
    else if (failures > 2) banner(`Can't reach the computer. Trying again in ${Math.ceil(wait / 1000)}s…`, true)
    retryTimer = setTimeout(connect, wait)
  }
  const reconnectNow = () => { if (conn) { conn.abort(); conn = null } failures = Math.min(failures, 1); connect() }
  // A silent line counts as a failure (its own retry, backing off): reconnecting at once kept
  // failures at 1, so a server that took the call and never answered left "Reconnecting…" up for
  // ever, never the banner that says the computer can't be reached (2026-11-18)
  setInterval(() => { if (conn && Date.now() - lastRx > DEAD_MS) conn.abort() }, 5000)
  // back on screen (or from the back-forward cache) with a line that's likely dead from the sleep:
  // dial now rather than wait out the watchdog
  const freshen = () => { if (conn && Date.now() - lastRx > STALE_MS) reconnectNow() }
  addEventListener('online', () => { online = true; banner(''); reconnectNow(); flush(true); render() })
  addEventListener('offline', () => { online = false; banner('Offline. Messages will send when the connection is back.', true); render() })
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      finishAll()
      hiddenTimer = setTimeout(() => { if (document.hidden && conn) { conn.abort(); conn = null } }, HIDDEN_GRACE_MS)
    } else {
      clearTimeout(hiddenTimer)
      unread = 0; document.title = 'Workbench'
      if (!conn) { failures = 0; connect() } else freshen()
      flush(true)
    }
  })

  const letGo = () => { finishAll(); if (conn) { conn.abort(); conn = null } }
  const comeBack = () => {
    if (document.hidden || !token) return
    if (!conn) { failures = 0; connect() } else freshen()
    flush(true)
  }
  addEventListener('pagehide', letGo)
  document.addEventListener('freeze', letGo)
  addEventListener('pageshow', comeBack)
  document.addEventListener('resume', comeBack)

  // ---- the phone keyboard: keep the composer above it (iOS ignores dvh for the keyboard) ----
  // As merecatholicity.com's DM composer does it (2026-11-19, the owner: the swipe didn't put the
  // keyboard away, a tap outside didn't either, and once the composer stayed halfway up the app).
  const vv = window.visualViewport
  const typing = () => document.activeElement === input
  let kbOpen = false
  function fit() {
    if (!vv) return
    kbOpen = innerHeight - vv.height > 120     // a keyboard is tall; a toolbar showing is not
    if (kbOpen) {
      // the body is the visible part of the page: pageTop counts iOS's own scroll of the document
      // on focus as well as the visual viewport's, where offsetTop alone left the composer
      // stranded mid-screen once that scroll outlived the keyboard
      document.body.style.height = vv.height + 'px'
      document.body.style.transform = vv.pageTop ? `translateY(${vv.pageTop}px)` : ''
    } else {
      document.body.style.height = document.body.style.transform = ''
      if (scrollX || scrollY) scrollTo(0, 0)    // the page never scrolls: its thread does
    }
    document.body.classList.toggle('kb-open', kbOpen)
    toEnd()
  }
  // the keyboard takes ~300 ms to stand or fall, and iOS scrolls the page after it has: fit again
  // on a ladder, and once more when it all settles
  let ladder = []
  const settle = () => { ladder.forEach(clearTimeout); ladder = [0, 120, 300, 520, 800, 1200].map(ms => setTimeout(fit, ms)) }
  if (vv) {
    vv.addEventListener('resize', fit)
    vv.addEventListener('scroll', fit)
    addEventListener('scroll', fit, { passive: true })
    input.addEventListener('focus', settle)
    input.addEventListener('blur', settle)
    addEventListener('orientationchange', settle)
  }
  // with the keyboard up: a downward swipe outside the composer puts it away, and so does a tap
  // that lands on nothing that takes one. Measured on the screen: iOS pans the page with the
  // finger, so clientY barely moved and the swipe never counted
  const TAPPABLE = 'button, a, input, textarea, select, label, summary, [contenteditable], [role=button], .composer'
  let touch0 = null
  document.addEventListener('touchstart', e => {
    const t = e.touches[0]
    touch0 = typing() && e.touches.length === 1 && !e.target.closest('.composer')
      ? { y: t.screenY, x: t.screenX, at: Date.now(), target: e.target } : null
  }, { passive: true })
  document.addEventListener('touchmove', e => {
    const t = e.touches[0]
    if (!touch0 || !t) return
    if (t.screenY - touch0.y > 48) { input.blur(); touch0 = null }
    else if (Math.abs(t.screenY - touch0.y) > 10 || Math.abs(t.screenX - touch0.x) > 10) touch0.moved = true
  }, { passive: true })
  document.addEventListener('touchend', () => {
    const t = touch0; touch0 = null
    if (t && !t.moved && Date.now() - t.at < 500 && typing() && !t.target.closest?.(TAPPABLE)) input.blur()
  }, { passive: true })

  // ---- sending: the outbox ----
  let flushing = false, flushTimer = 0, flushTries = 0
  async function flush(now) {
    if (now) { clearTimeout(flushTimer); flushTries = 0 }
    if (flushing || !token || !outbox.length) return
    flushing = true
    let again = false
    for (const o of [...outbox]) {
      if (o.error) continue
      try {
        const r = await post('/api/send', { text: o.text, cid: o.cid, ...(o.about ? { about: { task: o.about.task, n: o.about.n } } : {}) })
        const j = await r.json().catch(() => ({}))
        if (r.ok) { if (j.state) apply(j.state); continue }
        if (r.status === 401) { keyless('That key was not accepted.'); break }
        if (r.status >= 400 && r.status < 500) { o.error = j.error || 'refused'; save(K.outbox, outbox); render(); continue }
        again = true
      } catch { again = true }
    }
    flushing = false
    if (again && outbox.some(o => !o.error)) {
      flushTimer = setTimeout(flush, BACKOFF[Math.min(flushTries++, BACKOFF.length - 1)])
    }
  }
  const newId = () => (crypto.randomUUID ? crypto.randomUUID() : Date.now().toString(36) + Math.random().toString(36).slice(2))
  function send() {
    const text = input.value.trim()
    if (!text || !token) return
    outbox.push({ cid: newId(), text, ts: now(), ...(about ? { about } : {}) }); save(K.outbox, outbox)
    setAbout(null)
    input.value = ''; save(K.draft, ''); grow(); sendBtn.disabled = true
    render(); toEnd(true); flush(true)
  }

  // ---- composer ----
  const touch = matchMedia('(pointer: coarse)').matches
  function grow() { input.style.height = ''; input.style.height = Math.min(input.scrollHeight, innerHeight * 0.38) + 'px' }
  input.value = load(K.draft, '')
  input.addEventListener('input', () => { grow(); sendBtn.disabled = !input.value.trim(); save(K.draft, input.value) })
  input.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey && !touch && !e.isComposing) { e.preventDefault(); send() } })
  $('composer').addEventListener('submit', e => { e.preventDefault(); send() })
  sendBtn.disabled = !input.value.trim(); grow()

  // ---- no key: say how to get one, or take it pasted ----
  function keyless(why, forget) {
    const kept = forget ? '' : token  // kept until a new key is given: a stray refusal must not cost the link
    token = ''
    if (conn) { conn.abort(); conn = null }
    if (kept) setTimeout(() => { if (!token && document.querySelector('.keyless')) { token = kept; document.querySelector('.keyless')?.remove(); $('composer').hidden = false; render(); connect() } }, 30000)
    banner('')
    const box = document.createElement('div'); box.className = 'keyless'
    const h = document.createElement('h2'); h.textContent = 'This page needs its key'
    const p = document.createElement('p')
    p.append((why ? why + ' ' : '') + 'On the computer, run ')
    const c = document.createElement('code'); c.textContent = 'wb chat url'
    p.append(c, ' and open the link it prints here, or paste the link below.')
    const field = document.createElement('input'); field.placeholder = 'https://…/#token=…'
    field.addEventListener('change', () => {
      const m = field.value.match(/token=([\w-]+)/) || field.value.trim().match(/^([\w-]{20,})$/)
      if (m) { token = m[1]; save(K.token, token); box.remove(); $('composer').hidden = false; render(); connect(); flush(true); keepKey(); keyManifest() }
    })
    box.append(h, p, field)
    for (const el of nodes.values()) el.remove()
    nodes.clear()
    $('intro').hidden = true
    document.querySelector('.keyless')?.remove()
    thread.append(box)
    $('composer').hidden = true
    drawPresence()
  }

  // ---- settings: the gear's sheet ----
  const gear = $('gear'), sheet = $('settings')
  const showSettings = on => { sheet.hidden = !on; gear.setAttribute('aria-expanded', String(on)); if (on) drawSettings() }
  gear.onclick = () => showSettings(sheet.hidden)
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && !sheet.hidden) showSettings(false) })
  document.addEventListener('pointerdown', e => { if (!sheet.hidden && !sheet.contains(e.target) && !gear.contains(e.target)) showSettings(false) })
  const AWAY_NOTE = {
    ask: 'Permission prompts come here for Allow or Deny. Unanswered for 30 minutes, they wait in the terminal.',
    allow: "Claude runs what it would have asked about, and each one is listed here. Your deny rules still hold, and plans and questions still come to you.",
  }
  // `wb savings` for the week, from the state (chat.week_saved)
  const tokens = n => Math.abs(n) >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : `${Math.round(n / 1e3)}K`
  function drawSaved() {
    const t = state.savings, s = t && t.score
    $('badge').hidden = !s
    if (s) {
      const n = $('badge-n'); n.className = 'badge-n ' + s.color
      n.textContent = String(s.score); n.append(Object.assign(document.createElement('small'), { textContent: ' / 100' }))
      $('badge-s').textContent = s.sentence
    }
    if (!t || !t.tasks) {
      $('saved').textContent = 'Nothing to compare yet. Once a delegated task is accepted, this shows what it saved Claude this week.'
      return
    }
    const saved = t.saved >= 0
      ? `Saved about ${tokens(t.saved)}${t.self_est > 0 ? ` (${Math.round(100 * t.saved / t.self_est)}%)` : ''}.`
      : `It cost about ${tokens(-t.saved)} more.`
    const gpu = t.wh ? ` The GPU used ${t.wh >= 1000 ? `${(t.wh / 1000).toFixed(1)} kWh` : `${Math.round(t.wh)} Wh`}${t.cost != null ? ` ($${t.cost.toFixed(2)})` : ''}.` : ''
    $('saved').textContent = `In the last 7 days, ${t.tasks} task${t.tasks === 1 ? '' : 's'}: Claude used ${tokens(t.claude)} tokens where writing them itself would have taken about ${tokens(t.self_est)} (an estimate). ${saved}${gpu}`
  }
  function drawSettings() {
    drawApp(); drawNotify(); drawSaved()
    const away = (state.prefs && state.prefs.away) || 'ask'
    sheet.querySelectorAll('[data-away]').forEach(b => b.setAttribute('aria-checked', String(b.dataset.away === away)))
    $('away-note').textContent = AWAY_NOTE[away]
  }
  sheet.querySelectorAll('[data-away]').forEach(b => {
    b.onclick = async () => {
      const was = state.prefs
      state.prefs = { ...state.prefs, away: b.dataset.away }; drawSettings()
      try {
        const r = await post('/api/prefs', { away: b.dataset.away })
        const j = await r.json().catch(() => ({}))
        if (r.ok && j.state) return apply(j.state)
      } catch {}
      state.prefs = was; drawSettings(); $('away-note').textContent = 'Not saved: try again.'
    }
  })
  $('forget').onclick = async () => {
    if (pushOn) await setPush(false)  // a device without the key can't read what a push is about
    showSettings(false); save(K.token, ''); save(K.thread, []); keyless('', true); keepKey(); keyManifest()
  }

  // ---- the app: installing it, and notifications (sw.js; webpush.py on the computer) ----
  const standalone = () => matchMedia('(display-mode: standalone)').matches || navigator.standalone === true
  const ios = /iphone|ipad|ipod/i.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1)
  const iosSafari = ios && !/CriOS|FxiOS|EdgiOS|OPiOS/.test(navigator.userAgent)
  const canPush = window.isSecureContext && 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window
  let installEvt = null, swReg = null, pushOn = false, pushBusy = false, pushNote = '', pushSynced = false
  addEventListener('beforeinstallprompt', e => { e.preventDefault(); installEvt = e; if (!sheet.hidden) drawSettings() })
  addEventListener('appinstalled', () => { installEvt = null; if (!sheet.hidden) drawSettings() })
  if (window.isSecureContext && 'serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js', { updateViaCache: 'none' }).then(r => { swReg = r; syncPush() }).catch(() => {})
  }
  async function keepKey() {  // the worker reads the key from here when a push comes
    try {
      const c = await caches.open('wb-chat')
      await c.delete('/key')  // WebKit drops a put over an entry a reloaded page wrote (2026-11-18)
      if (token) await c.put('/key', new Response(token))
    } catch {}
  }
  const b64u = buf => btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
  const unb64u = s => Uint8Array.from(atob(s.replace(/-/g, '+').replace(/_/g, '/')), c => c.charCodeAt(0))
  async function syncPush() {
    // Is this device subscribed? Told to the computer again on each load (it may have dropped it),
    // and moved to the computer's key if that changed
    const key = state.push && state.push.key
    if (!canPush || !swReg || !token || !key) return
    try {
      let sub = await swReg.pushManager.getSubscription()
      if (sub && sub.options.applicationServerKey && b64u(sub.options.applicationServerKey) !== key) {
        await sub.unsubscribe()
        sub = Notification.permission === 'granted'
          ? await swReg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: unb64u(key) }) : null
      }
      pushOn = !!sub && Notification.permission === 'granted'
      if (sub) await post('/api/push', { endpoint: sub.endpoint, on: true })
    } catch {}
    if (!sheet.hidden) drawSettings()
  }
  async function setPush(on) {
    pushBusy = true; pushNote = ''; drawSettings()
    try {
      if (on) {
        if (Notification.permission !== 'granted' && (await Notification.requestPermission()) !== 'granted') throw new Error('Notifications were not allowed.')
        let sub = await swReg.pushManager.getSubscription()
        if (!sub) sub = await swReg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: unb64u(state.push.key) })
        const r = await post('/api/push', { endpoint: sub.endpoint, on: true })
        if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || 'The computer refused it.')
        pushOn = true
      } else {
        const sub = await swReg.pushManager.getSubscription()
        if (sub) { await post('/api/push', { endpoint: sub.endpoint, on: false }).catch(() => {}); await sub.unsubscribe() }
        pushOn = false
      }
    } catch (e) { pushNote = String((e && e.message) || e) }
    pushBusy = false; drawSettings()
  }
  const para = (box, text, cls) => { const p = document.createElement('p'); if (cls) p.className = cls; p.textContent = text; box.append(p); return p }
  const HTTPS = "This page is open over plain http, so it can't be installed or send notifications. Over https it can: " +
    'on the computer, `tailscale serve --bg 1337` gives the chat an https address on your tailnet.'
  function drawApp() {
    const box = $('app'); box.replaceChildren()
    $('app-h').textContent = standalone() ? 'The app' : 'Install the app'
    if (standalone()) return para(box, "Installed ✓ — you're using the chat as an app.")
    if (!window.isSecureContext) return para(box, HTTPS, 'small')
    if (!https) return para(box, 'The app installs from the https address, so the one app works on every device: ' +
      'on the computer, `tailscale serve --bg 1337` gives the chat one on your tailnet.', 'small')
    if (installEvt) {
      const b = document.createElement('button'); b.type = 'button'; b.className = 'plain'; b.textContent = 'Install this app'
      b.onclick = async () => { installEvt.prompt(); try { await installEvt.userChoice } catch {} installEvt = null; drawSettings() }
      box.append(b); return para(box, 'Adds Claude to your home screen, with its own window.', 'small')
    }
    if (iosSafari) return para(box, "Tap the Share button ⎋ in Safari's toolbar, then Add to Home Screen. Open Claude from its new icon: on iPhone and iPad, that's what lets it send notifications.")
    if (ios) return para(box, 'On iPhone and iPad the app installs from Safari: open this page in Safari, then Share ⎋ → Add to Home Screen.')
    para(box, "Look for Install in your browser's menu or address bar; on a phone, Add to Home Screen.")
  }
  function drawNotify() {
    const box = $('notify'); box.replaceChildren()
    if (!window.isSecureContext) return para(box, 'They need the page over https too (see above).', 'small')
    if (ios && !standalone()) return para(box, 'On iPhone and iPad, notifications come only to the installed app: install it above in Safari, open it from its icon, and turn them on here.', 'small')
    if (!canPush) return para(box, "This browser can't take notifications from a page.", 'small')
    if (Notification.permission === 'denied') return para(box, "Notifications are blocked for this page: allow them in the browser's site settings, then come back here.", 'small')
    const row = document.createElement('label'); row.className = 'switch'
    const t = document.createElement('span'); t.textContent = 'When Claude answers or needs you'
    const sw = document.createElement('input'); sw.type = 'checkbox'; sw.setAttribute('role', 'switch'); sw.id = 'push'
    sw.checked = pushOn; sw.disabled = pushBusy || !swReg || !(state.push && state.push.key)
    sw.onchange = () => setPush(sw.checked)
    row.append(t, sw); box.append(row)
    para(box, pushBusy ? 'Working…' : pushNote || (pushOn ? 'On: this device hears from Claude even with the page closed, unless the page already showed it.'
      : 'Off. Nothing you and Claude write passes through the push service: the phone fetches it from your computer.'), 'small')
    if (pushOn && !pushBusy) {
      const b = document.createElement('button'); b.type = 'button'; b.className = 'plain'; b.textContent = 'Send a test'
      b.onclick = async () => {
        b.disabled = true
        try {
          const j = await (await post('/api/push', { test: true })).json()
          pushNote = j.ok ? 'Sent: it should arrive in a few seconds.' : "The push service didn't take it: turn notifications off and on again."
        } catch { pushNote = 'Not sent: try again.' }
        drawSettings()
      }
      box.append(b)
    }
  }
  // What the page showed while on screen: the computer pushes only what no page has
  const reported = new Set()
  function reportSeen() {
    if (document.hidden || !token) return
    const ids = [...state.messages.filter(m => m.from === 'claude').slice(-20).map(m => m.id),
      ...state.asks.filter(a => !a.answer).map(a => a.id)].filter(i => !reported.has(i))
    if (!ids.length) return
    ids.forEach(i => reported.add(i))
    post('/api/seen', { ids }).catch(() => ids.forEach(i => reported.delete(i)))
  }
  document.addEventListener('visibilitychange', reportSeen)

  async function firstState() {  // at once, without waiting for a stream slot to free up
    try {
      const r = await fetch('/api/state', { headers: { Authorization: 'Bearer ' + token }, cache: 'no-store' })
      if (r.ok && !live) apply(await r.json())
    } catch {}
  }

  if (!token) keyless('')
  else { render(); firstState(); connect(); flush(true); keepKey() }
})()
