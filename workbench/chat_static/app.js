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
  const K = { token: 'wb-chat-token', seen: 'wb-chat-seen', outbox: 'wb-chat-outbox', draft: 'wb-chat-draft' }
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
    const m = location.hash.match(/token=([\w-]+)/)
    if (m) { token = m[1]; save(K.token, token); history.replaceState(null, '', location.pathname + location.search) }
    else token = load(K.token, '')
  }

  // ---- state ----
  let state = { messages: [], asks: [], listener: null }
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
      .map(o => ({ id: 'local-' + o.cid, cid: o.cid, from: 'user', text: o.text, ts: o.ts, local: true, error: o.error }))
    // permission prompts sit in the thread where they were asked
    const asks = state.asks.map(a => ({ id: a.id, from: 'ask', ts: a.ts, ask: a }))
    return [...state.messages, ...asks].sort((x, y) => x.ts - y.ts).concat(pending)
  }

  function metaFor(m) {
    const meta = document.createElement('div'); meta.className = 'meta'
    if (m.ask) {
      const a = m.ask
      meta.append(span('', clock(a.ts) + (a.answer === 'allow' ? ' · you allowed it ' + clock(a.answered)
        : a.answer === 'deny' ? ' · you said no ' + clock(a.answered)
        : a.answer === 'expired' ? (a.by === 'interrupted' ? ' · stopped in the terminal' : ' · no answer in time: it waits in the terminal')
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
    meta.append(span('', clock(m.ts)))
    if (m.received) meta.append(span('tick', '✓✓'), span('', m.received.how === 'queued'
      ? 'Claude has it, reading after its current step · ' + clock(m.received.ts)
      : 'read by Claude ' + clock(m.received.ts)))
    else meta.append(span('tick', '✓'), span('', state.listener ? 'delivered' : 'waiting for a Claude session'))
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

  function makeAsk(a) {
    const row = document.createElement('div')
    const card = document.createElement('div'); card.className = 'card'
    const head = document.createElement('div'); head.className = 'asks'
    head.textContent = a.answer ? 'Claude asked to use ' + a.tool : 'Claude wants to use ' + a.tool
    const what = document.createElement('pre'); what.textContent = a.text
    card.append(head, what)
    if (!a.answer) {
      const choices = document.createElement('div'); choices.className = 'choices'
      const yes = document.createElement('button'); yes.className = 'allow'; yes.textContent = 'Allow'
      const no = document.createElement('button'); no.className = 'deny'; no.textContent = 'Deny'
      const said = span('said', '')
      const answer = async verdict => {
        yes.disabled = no.disabled = true; said.textContent = ''
        try {
          const r = await post('/api/answer', { id: a.id, answer: verdict })
          const j = await r.json().catch(() => ({}))
          if (r.ok && j.state) return apply(j.state)
          if (r.status === 401) return keyless('That key was not accepted.')
          said.textContent = j.error || 'not sent: try again'
        } catch { said.textContent = 'not sent: try again' }
        yes.disabled = no.disabled = false
      }
      yes.onclick = () => answer('allow'); no.onclick = () => answer('deny')
      choices.append(no, yes)
      card.append(choices, said)
      if (document.hidden) document.title = 'Claude asks: allow?'
    }
    row.append(card, document.createElement('div'))
    row.lastChild.className = 'meta'
    return row
  }

  function makeRow(m) {
    if (m.ask) return makeAsk(m.ask)
    const row = document.createElement('div')
    const b = document.createElement('div'); b.className = 'bubble'
    if (m.from === 'user') b.textContent = m.text
    else if (seen.has(m.id) || firstVisit || document.hidden || reduced.matches) {
      if (!seen.has(m.id) && !firstVisit && document.hidden) { unread++; document.title = `(${unread}) Claude` }
      rich(b, m.text); seen.add(m.id); save(K.seen, [...seen].slice(-500))
    } else {
      b.classList.add('flowing'); flows.set(m.id, { text: m.text, shown: 0, el: b })
      if (!flowTimer) flowTimer = setInterval(flowTick, FLOW_MS)
    }
    row.append(b, document.createElement('div'))
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
    presenceText.textContent = !token ? 'not connected' : !l ? 'no session listening · messages wait' :
      (l.busy ? 'working in ' : 'listening in ') + where
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
    state = { messages: s.messages || [], asks: s.asks || [], listener: s.listener || null }
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
      unread = 0; document.title = 'Claude'
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
  const vv = window.visualViewport
  if (vv) {
    const fit = () => {
      document.body.style.height = vv.height + 'px'
      document.body.style.transform = vv.offsetTop ? `translateY(${vv.offsetTop}px)` : ''
      document.body.classList.toggle('kb-open', innerHeight - vv.height > 120)
      toEnd()
    }
    vv.addEventListener('resize', fit)
    vv.addEventListener('scroll', fit)
  }
  // a downward swipe outside the composer puts the keyboard away
  let swipeY = null
  document.addEventListener('touchstart', e => {
    swipeY = document.activeElement === input && !e.target.closest('.composer') ? e.touches[0].clientY : null
  }, { passive: true })
  document.addEventListener('touchmove', e => {
    if (swipeY !== null && e.touches[0].clientY - swipeY > 48) { input.blur(); swipeY = null }
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
        const r = await post('/api/send', { text: o.text, cid: o.cid })
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
    outbox.push({ cid: newId(), text, ts: now() }); save(K.outbox, outbox)
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
  function keyless(why) {
    const kept = token  // kept until a new key is given: a stray refusal must not cost the link
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
      if (m) { token = m[1]; save(K.token, token); box.remove(); $('composer').hidden = false; render(); connect(); flush(true) }
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

  if (!token) keyless('')
  else { render(); connect(); flush(true) }
})()
