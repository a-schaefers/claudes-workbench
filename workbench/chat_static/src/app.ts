/* The web chat's page. Three promises, kept the way merecat keeps them:
   - Nothing half-received. Every (re)connect starts with the whole thread in a `state` event,
     then each change comes as a `patch` of what changed (#755), so a dropped connection loses
     nothing; replies are written
     whole by `wb chat reply`, and the paced reveal is only paint, finished instantly if the page
     is hidden or the reader prefers less motion.
   - Nothing sent twice or lost. A message goes into a local outbox with its own id first and is
     retried until the server has it; the server ignores an id it already holds.
   - Always reconnecting. A fetch-streamed event stream (it carries the token as a header, which
     EventSource can't), a heartbeat watchdog, jittered backoff, a pause while hidden and an
     instant resync on return or when the network comes back. */
(() => {
  'use strict'
  const K = { token: 'wb-chat-token', seen: 'wb-chat-seen', outbox: 'wb-chat-outbox', draft: 'wb-chat-draft', thread: 'wb-chat-thread', about: 'wb-chat-about', hint: 'wb-chat-hint' }
  const BACKOFF = [500, 1000, 2000, 4000, 8000, 15000, 30000]
  let viewing: string | null = null, sweeping = false  // the photo the viewer shows; a sweep of object URLs queued (#746)
  let nearView: IntersectionObserver | null = null  // loads the thread's thumbnails as they near the view (#754)
  const nearLater = new Set<HTMLElement>()  // thumbnails seen near the view before the server's thread was in
  let drawnAt = 0  // when render() last drew the thread (performance.now()): an older nearness is stale
  const DEAD_MS = 40000          // the server pings every 15 s: this long silent means the line is dead
  const CONNECT_MS = 12000       // a connection that never answers: dial again
  const HIDDEN_GRACE_MS = 60000  // a hidden page keeps its stream this long, then lets go
  const STALE_MS = 20000         // back on screen with nothing heard this long: the line is likely dead
  const AWAY_MS = 3000           // off screen this long: a phone may have let the line die, so dial again
  const POST_MS = 15000          // a send silent this long (no headers yet, or no more of its reply) is tried again
  const FLOW_MS = 40

  const $ = <T extends HTMLElement = HTMLElement>(id: string) => document.getElementById(id) as T
  const scroller = $('scroll'), thread = $('thread'), net = $('net'), input = $<HTMLTextAreaElement>('input'),
    sendBtn = $<HTMLButtonElement>('send'), jump = $('jump'), presence = $('presence'), presenceText = $('presence-text')

  const load = (k: string, d: any): any => { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v) } catch { return d } }
  const save = (k: string, v: unknown) => { try { localStorage.setItem(k, JSON.stringify(v)); return true } catch { return false } }

  // what the chat is for (#1341), one wording: the empty state's, and over a thread the same words
  // as a hint until dismissed on this device. load and save catch a storage that throws: there the
  // hint shows on each open, and a dismissal holds until the page is left
  let hintGone = load(K.hint, false) === true
  function drawHint() { $('hint').hidden = hintGone || !$('intro').hidden || !!document.querySelector('.keyless') }
  $('hint-x').onclick = () => { hintGone = true; save(K.hint, true); drawHint() }

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
    const link = document.querySelector<HTMLLinkElement>('link[rel=manifest]')
    if (link && !https) link.remove()
    else if (link) link.href = '/manifest.webmanifest' + (token ? '?k=' + encodeURIComponent(token) : '')
  }
  keyManifest()

  // ---- state ----
  // Until the first live state, the thread this page last had, so a reload shows it at once
  let state: ChatState = { messages: token ? load(K.thread, []) : [], push: {}, listener: null }
  let live = false
  // the computer answering: not while the stream fails past its first tries, or offline. Without
  // it the header said "Claude · listening" in green under "Can't reach the computer" (#1415,
  // 2026-11-25); apart from `live`, which says only that a state came once
  let reachable = true
  let skew = 0                                  // server clock − ours, seconds
  // The outbox: one localStorage key per message (OUT + its cid), read afresh whenever it is
  // used. A single shared array was each tab's own copy saved over the others': two tabs offline,
  // and only the second tab's message ever reached the server (#199, the 2026-11-19 audit).
  const OUT = 'wb-chat-out-'
  const outItems = () => {
    const out: Outgoing[] = []
    try {
      for (let i = 0; i < localStorage.length; i++) {
        const k = localStorage.key(i)
        if (k && k.startsWith(OUT)) { const o = load(k, null); if (o && o.cid) out.push(o) }
      }
    } catch {}
    return out.sort((x, y) => x.ts - y.ts)
  }
  const keep = (o: Outgoing) => { save(OUT + o.cid, o); outbox = outItems() }
  const drop = (cid: string) => { try { localStorage.removeItem(OUT + cid) } catch {} outbox = outbox.filter(o => o.cid !== cid) }
  for (const o of load(K.outbox, [])) if (o && o.cid) save(OUT + o.cid, o)  // the one array, before #199
  try { localStorage.removeItem(K.outbox) } catch {}
  let outbox = outItems()                       // [{cid, text, ts, error?}]
  // A message the browser wouldn't store (its storage full, or denied): it stays in the composer
  // until the server has it. Send cleared the composer, the outbox read back empty, and the
  // message was gone, with no pending bubble (#245, the audit of 1.2)
  let held: Outgoing | null = null
  let seen = new Set<string>(load(K.seen, null) || [])  // Claude's messages already shown in full
  let firstVisit = load(K.seen, null) === null  // the backlog shows whole; what comes after flows
  const nodes = new Map<string, HTMLElement>()                       // key → row element
  const flows = new Map<string, { text: string; shown: number; el: HTMLElement }>()                       // message id → {text, shown, el}
  let unread = 0
  let curView: string | null = null                            // the view shown (#1284): 'chat', 'stats' or 'settings'

  const now = () => Date.now() / 1000 + skew
  const reduced = matchMedia('(prefers-reduced-motion: reduce)')

  // ---- sticky follow (stays at the newest line unless the reader scrolls up) ----
  let follow = true, lastTop = 0, lastHeight = 0, touchY = 0
  const nearEnd = () => scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 60
  const release = () => { follow = false; jump.hidden = nearEnd() }
  scroller.addEventListener('scroll', () => {
    const top = scroller.scrollTop, height = scroller.scrollHeight
    // A scroll that came with the thread's height changing is the layout's, not the reader's:
    // the saved thread's images loading after a reload grew it and moved scrollTop up 24 px, that
    // read as the reader going up, and the server's thread was then drawn with the view left mid-
    // thread, far from the newest line (#1223, under load; CI saw its photo fetched). A wheel or a
    // finger still releases at once
    if (top > lastTop + 2 && nearEnd()) follow = true       // the reader came back down
    else if (top < lastTop - 2 && !nearEnd() && height === lastHeight) follow = false // the reader went up
    lastTop = top; lastHeight = height
    jump.hidden = follow || nearEnd()
  }, { passive: true })
  scroller.addEventListener('wheel', e => { if (e.deltaY < 0) release() }, { passive: true })
  scroller.addEventListener('touchstart', e => { touchY = e.touches[0]?.clientY ?? 0 }, { passive: true })
  scroller.addEventListener('touchmove', e => {
    const y = e.touches[0]?.clientY ?? 0
    if (y > touchY + 8) release()                           // finger down = view up
    touchY = y
  }, { passive: true })
  const toEnd = (force?: boolean) => {
    // another view shown: the thread has no box to scroll, so it is put at its end on return (#1284)
    if (curView && curView !== 'chat') { if (force) follow = true; return }
    if (force || follow) { scroller.scrollTop = scroller.scrollHeight; lastTop = scroller.scrollTop; lastHeight = scroller.scrollHeight; follow = true; jump.hidden = true }
  }
  jump.addEventListener('click', () => scroller.scrollTo({ top: scroller.scrollHeight, behavior: 'smooth' }))

  // ---- text → DOM, never HTML from the thread ----
  function inline(parent: HTMLElement, text: string) {
    const re = /(`[^`\n]+`)|(\*\*[^*\n]+\*\*)|(https?:\/\/[^\s<>()]+[^\s<>().,;:!?'"])/g
    let i = 0, m: RegExpExecArray | null
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
  function rich(el: HTMLElement, text: string) {
    el.replaceChildren()
    const parts = text.split(/^```[^\n]*\n?/m)
    parts.forEach((part, n) => {
      if (n % 2) { const pre = document.createElement('pre'), c = document.createElement('code'); c.textContent = part.replace(/\n$/, ''); pre.append(c); el.append(pre); return }
      for (const block of part.split(/\n{2,}/)) {
        if (!block.trim()) continue
        // each run of bullet lines a list, each run of others a paragraph: a block was a list only
        // when every line was a bullet, so Claude's usual lead-in line, then its bullets with no
        // blank line between, showed as one paragraph of '- ' lines (#1416, 2026-11-25)
        const lines = block.split('\n')
        const bullet = /^\s*(?:[-*•]|\d+[.)])\s+/
        for (let i = 0; i < lines.length;) {
          const listed = bullet.test(lines[i])
          let j = i
          while (j < lines.length && bullet.test(lines[j]) === listed) j++
          const run = lines.slice(i, j); i = j
          if (listed) {
            const list = document.createElement(/^\s*\d/.test(run[0]) ? 'ol' : 'ul')
            for (const l of run) { const li = document.createElement('li'); inline(li, l.replace(bullet, '')); list.append(li) }
            el.append(list)
          } else {
            const p = document.createElement('p')
            run.forEach((l, k) => { if (k) p.append(document.createElement('br')); inline(p, l) })
            el.append(p)
          }
        }
      }
    })
  }

  // What a screen reader hears of the thread: each new message from Claude once, whole. The
  // thread was the live region, and every update read out each row's time and `copy` again, and
  // a reply in its reveal's growing fragments (#1408, 2026-11-25)
  function say(text: string) {
    const log = $('say')
    log.append(Object.assign(document.createElement('p'), { textContent: text.replace(/\*\*|`/g, '') }))
    while (log.childElementCount > 20) log.firstElementChild!.remove()
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
  function finishFlow(id: string) {
    const f = flows.get(id)
    if (!f) return
    flows.delete(id)
    f.el.classList.remove('flowing')
    rich(f.el, f.text)
    say(f.text)
    seen.add(id); save(K.seen, [...seen].slice(-500))
  }
  const finishAll = () => { for (const id of [...flows.keys()]) finishFlow(id); toEnd() }

  // ---- drawing ----
  const clock = (ts: number) => new Date(ts * 1000).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
  function dayName(ts: number) {
    const d = new Date(ts * 1000), t = new Date()
    const y = new Date(t); y.setDate(t.getDate() - 1)
    if (d.toDateString() === t.toDateString()) return 'Today'
    if (d.toDateString() === y.toDateString()) return 'Yesterday'
    return d.toLocaleDateString([], { weekday: 'long', month: 'short', day: 'numeric' })
  }
  // navigator.clipboard exists only in a secure context: over plain http at a LAN or tailnet
  // address it is undefined and every copy said "copy failed" (#745). There a selected textarea
  // and execCommand('copy') still work, inside the tap's user gesture.
  async function copyText(text: string) {
    if (navigator.clipboard && window.isSecureContext) {
      try { await navigator.clipboard.writeText(text); return true } catch {}
    }
    const t = document.createElement('textarea')
    t.value = text; t.readOnly = true
    t.style.cssText = 'position:fixed;top:0;left:0;width:1px;height:1px;opacity:0'
    document.body.append(t)
    try { t.select(); t.setSelectionRange(0, text.length); return document.execCommand('copy') }
    catch { return false }
    finally { t.remove() }
  }
  const span = (cls: string, text?: string | null) => { const s = document.createElement('span'); if (cls) s.className = cls; s.textContent = text as string; return s }
  const since = (ts: number) => { const s = Math.max(0, Math.round(now() - ts)); return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, '0')}s` }

  function items(): Msg[] {
    const onServer = new Set(state.messages.map(m => m.cid).filter(Boolean))
    const pending = outbox.filter(o => !onServer.has(o.cid))
      .map(o => ({ id: 'local-' + o.cid, cid: o.cid, from: 'user', text: o.text, ts: o.ts, local: true, error: o.error, about: o.about, files: o.files }))
    // a notice (a dialog waiting in the terminal) is no row: the strip at the top says it (#1355)
    return state.messages.filter(m => m.from !== 'notice').sort((x, y) => x.ts - y.ts).concat(pending)
  }

  // A reply Claude wrote to agents alone (`to` without "user", #1042) is no news for the user, as
  // the server keeps it from their pushes (chat._for_user): it read as a reply to them, counted
  // in the tab's unread, with no addressee (#1128, 2026-11-23)
  const forUser = (m: Msg) => !Array.isArray(m.to) || !m.to.length || m.to.includes('user')

  // Whom the chat talks to (#1319): the driver, Claude Code ("Claude") or OpenCode. A reply is
  // named by its own row's `driver`, the rest by the listener's; with none listening, by the
  // driver that wrote last (a row's or a waiting line's `driver`), else Claude, as before 2.2.
  // OpenCode's user read "Waiting for a Claude session" and "Read by Claude" of their own driver
  const DRIVER_NAMES: Record<string, string> = { claude: 'Claude', opencode: 'OpenCode' }
  const nameOf = (d?: Driver | null) => (d && DRIVER_NAMES[d.tool]) || ''
  function driverName() {
    const l = state.listener
    if (l) return nameOf(l.driver) || (l.tool === 'opencode' ? 'OpenCode' : 'Claude')
    for (let i = state.messages.length - 1; i >= 0; i--) {
      const m = state.messages[i]!
      if ((m.from === 'claude' || m.from === 'notice') && nameOf(m.driver)) return nameOf(m.driver)
    }
    return 'Claude'
  }
  const aSession = (n: string) => (/^[AEIOU]/.test(n) ? 'an ' : 'a ') + n + ' session'
  // who read it: the receipt's `by`, a session id, OpenCode's `ses_…` (its identifier's prefix) or
  // Claude Code's UUID; so yesterday's receipt from Claude Code never reads as today's listener's
  const readBy = (m: Msg) => typeof m.received?.by === 'string' && m.received.by
    ? (m.received.by.startsWith('ses_') ? 'OpenCode' : 'Claude') : driverName()

  function metaFor(m: Msg) {
    const meta = document.createElement('div'); meta.className = 'meta'
    if (m.from === 'notice') {  // the server leaves it out once its dialog settles: the line clears (#1350)
      meta.append(span('', clock(m.ts)))
      return meta
    }
    if (m.role === 'agent') return agentMeta(meta, m)
    if (m.from === 'claude') {
      const who = nameOf(m.driver) || 'Claude'  // its writer's; none: Claude Code's, or before 2.2
      if (forUser(m)) meta.append(span('', who + ' · ' + clock(m.ts) + ' · '))
      else meta.append(span('', who + ' to '), span('agent-name', m.to!.join(', ')), span('', ' · ' + clock(m.ts) + ' · '))
      const c = document.createElement('button'); c.textContent = 'copy'
      c.onclick = async () => {
        c.textContent = await copyText(m.text) ? 'copied' : 'copy failed'
        setTimeout(() => { c.textContent = 'copy' }, 1500)
      }
      meta.append(c)
      return meta
    }
    if (m.local) {
      if (m.error) {
        meta.append(span('', 'Not sent: ' + m.error + ' · '))
        const b = document.createElement('button'); b.textContent = 'retry'
        b.onclick = () => { const o = outbox.find(x => x.cid === m.cid); if (o) { delete o.error; keep(o) } flush(true); render() }
        const d = document.createElement('button'); d.textContent = 'delete'
        d.onclick = () => { forgetFiles(outbox.find(x => x.cid === m.cid)); drop(m.cid!); render() }
        const ed = document.createElement('button'); ed.textContent = 'edit'
        ed.onclick = () => {
          drop(m.cid!)
          if (m.about) setAbout(m.about)  // what it was about comes back with it (#843)
          for (const f of m.files || []) if (!draft.some(d => d.id === f.id)) draft.push({ ...f, mod: 0, state: 'up' })
          keepDraft(); drawTray(); pump()
          // what the box holds now is kept after the old words, never replaced: typed after the
          // refusal, it was lost, the undo history with it (2026-11-21, the audit of #958)
          const typed = input.value.trim()
          const text = !typed || typed === m.text ? m.text : m.text ? m.text + '\n\n' + input.value : input.value
          input.value = text; save(K.draft, text); grow(); armSend()
          render(); input.focus()
        }
        meta.append(b, span('', ' · '), ed, span('', ' · '), d)
      } else meta.append(span('', online ? 'Sending…' : 'Waiting for the connection…'))
      return meta
    }
    // one short line, one time: the receipt's once the driver has it (#103)
    const who = driverName()
    if (m.received) meta.append(span('tick', '✓✓'), span('', (m.received.how === 'queued'
      ? `Queued for ${readBy(m)} · ` : `Read by ${readBy(m)} · `) + clock(m.received.ts)))
    else if (m.expired) {  // unread too long: no session is given it, so never "Delivered" (#536)
      const h = Math.round((state.expire || 21600) / 3600)
      meta.append(span('', `Not passed to ${who} (over ${h} h old) · ` + clock(m.ts) + ' · '))
      const b = document.createElement('button'); b.textContent = 'send again'
      b.onclick = () => { b.disabled = true; b.dataset.cid = newId(); resend(m, b.dataset.cid) }
      meta.append(b)
    } else meta.append(span('tick', '✓'), span('', delivered(who) + clock(m.ts)))
    return meta
  }
  // an unread message's line: delivered, behind a prompt in the terminal (the driver can't read it
  // until that is answered, #1355), or waiting for a session
  const delivered = (who: string) => !state.listener ? `Waiting for ${aSession(who)} · `
    : state.driver && state.driver.state === 'waiting' ? storyText('receipt.blocked', driverKind()) + ' · ' : 'Delivered · '

  // another agent's message (#1042): its name, then the driver's receipt; never the user's to send again
  function agentMeta(meta: HTMLElement, m: Msg) {
    const who = driverName()
    meta.append(span('agent-name', m.from), span('', ' · ' + (m.received
      ? (m.received.how === 'queued' ? `Queued for ${readBy(m)} · ` : `Read by ${readBy(m)} · `) + clock(m.received.ts)
      : m.expired ? `Not passed to ${who} · ` + clock(m.ts)
      : delivered(who) + clock(m.ts))))
    return meta
  }

  for (const chip of document.querySelectorAll('.chip')) {
    chip.addEventListener('click', () => { input.value = chip.textContent as string; grow(); armSend(); input.focus() })
  }

  // The open tasks in one line over the thread (chat.open_tasks, #1016): which model each runs on,
  // GPU or API and whose, as the pane's row says it. A server without `tasks` (an older wb): none.
  // At most TASKS_SHOWN named, the running first, then those waiting on Claude, then the queued,
  // with the counts said apart: a 25-task backlog was 22 lines of "Running: …", the queued read
  // as running, and the thread a sliver on a phone (#1097, 2026-11-23). `asking` waits on Claude
  // (store.NEEDS_CLAUDE): counted running, a task blocked on its question told the away user that
  // nothing needed Claude (the #1097 review, 2026-11-23)
  const RUNNING = ['running', 'testing'], TASKS_SHOWN = 3
  const rank = (t: OpenTask) => RUNNING.includes(t.phase) ? 0 : t.phase === 'queued' ? 2 : 1
  const ranked = (tasks?: OpenTask[]) => Array.isArray(tasks) ? [...tasks].sort((a, b) => rank(a) - rank(b)) : []
  const taskOn = (t: { model?: string; via?: string; provider_name?: string }) => {  // the model it runs on, as the pane's row says it
    const short = String(t.model || '').split('/').pop()
    return t.via === 'api' ? `API ${short}${t.provider_name ? ` (${t.provider_name})` : ''}` : `GPU ${short}`
  }
  function tasksLine(tasks?: OpenTask[]) {
    const all = ranked(tasks)
    if (!all.length) return ''
    const named = all.slice(0, TASKS_SHOWN).map(t => `${t.name}${RUNNING.includes(t.phase) ? '' : ` (${t.phase})`} · ${taskOn(t)}`)
    if (all.length > TASKS_SHOWN) named.push(`+${all.length - TASKS_SHOWN} more`)
    const n = (r: number) => all.filter(t => rank(t) === r).length
    const counts = [[n(0), 'running'], [n(1), `waiting on ${driverName()}`], [n(2), 'queued']].filter(([c]) => c)
    const head = counts.length === 1 && n(0) && all.length <= TASKS_SHOWN ? 'Running'
      : counts.map(([c, what]) => `${c} ${what}`).join(', ')
    return head + ': ' + named.join('; ')
  }
  function drawTasks() {
    const el = $('tasks'), text = tasksLine(state.tasks)
    el.textContent = text
    el.hidden = !text
    if (!text) showTaskSheet(false)
    sheetNews()
  }

  // The runs strip (#1278's S12): each deep workflow not closed (chat.open_runs), over the tasks':
  // its name and flow, its phase, each stage's items done of all, and the budget spent. A tap opens
  // Settings at its group, where each run has its Stop and its Summary. A server without `runs`
  // (an older wb): none. At most RUNS_SHOWN named, the rest counted.
  const RUNS_SHOWN = 2
  const usdWord = (x: number) => x === 0 ? '$0' : x < 0.01 ? '<$0.01' : '$' + x.toFixed(2)
  function runLine(r: OpenRun) {
    const stages = Object.entries(r.stages || {}).flatMap(([id, c]) => {
      const counts = Object.values(c || {}).map(Number).filter(n => n > 0)
      const all = counts.reduce((a, b) => a + b, 0)
      return all ? [`${id} ${Number((c || {}).done || 0)}/${all}`] : []
    })
    const spent = usdWord(Number(r.spent_usd) || 0) + (typeof r.budget_usd === 'number' ? ` of ${usdWord(r.budget_usd)}` : '')
    return [`${r.run}${r.flow ? ` (${r.flow})` : ''}`, r.phase || 'running', ...stages, spent].join(' · ')
  }
  function runsLine(runs?: OpenRun[]) {
    const all = Array.isArray(runs) ? runs.filter(r => r && r.run) : []
    if (!all.length) return ''
    const named = all.slice(0, RUNS_SHOWN).map(runLine)
    if (all.length > RUNS_SHOWN) named.push(`+${all.length - RUNS_SHOWN} more`)
    return (all.length === 1 ? 'Run: ' : `${all.length} runs: `) + named.join('; ')
  }
  function drawRuns() {
    const el = $('runs'), text = runsLine(state.runs)
    el.textContent = text
    el.hidden = !text
  }
  const runsStrip = $('runs')
  const toRuns = () => { go('settings'); document.dispatchEvent(new CustomEvent('wb-settings-group', { detail: 'workflows' })) }
  runsStrip.onclick = toRuns
  runsStrip.onkeydown = e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toRuns() } }

  // ---- the task sheet (#1201): a tap on the strip lists the open tasks; a tap on one shows its
  // summary (GET /api/task: wb status's row and wb report), its Diff and Log tabs fetched when
  // opened. Nothing polls: a change in the stream's `tasks` re-fetches only the view that is open,
  // and Refresh asks again. Its report, diff and log are agent-written text, drawn with
  // textContent alone, never as markup: this page holds the user's key and changes their
  // Settings, so markup an agent wrote would be a way to both. Steering is Claude's: "Reply about this task" tags the
  // next message with the task (`about: {task}`), and Claude decides on wb steer.
  const strip = $('tasks'), tsheet = $('task-sheet'), tsBody = $('ts-body')
  let tsTask: string | null = null, tsPart = 'summary', tsDiff: { path: string } | null = null, tsSeen = '', tsAsked = 0  // tsDiff: null (the stat) or {path}
  function showTaskSheet(on: boolean | string) {  // string: `hidden`'s 'until-found', never set here
    if (on === !tsheet.hidden) return
    tsheet.hidden = !on; strip.setAttribute('aria-expanded', String(on))
    if (on) { tsTask = null; drawTaskSheet() }
  }
  strip.onclick = () => showTaskSheet(tsheet.hidden)
  strip.onkeydown = e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); showTaskSheet(tsheet.hidden) } }
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && !tsheet.hidden) showTaskSheet(false) })
  document.addEventListener('pointerdown', e => { if (!tsheet.hidden && !tsheet.contains(e.target as Node) && !strip.contains(e.target as Node)) showTaskSheet(false) })
  const mk = <K extends keyof HTMLElementTagNameMap>(tag: K, cls?: string | null, text?: string | null) => { const x = document.createElement(tag); if (cls) x.className = cls; if (text != null) x.textContent = text; return x }
  const btn = (cls: string | null, text: string | null, fn: () => void) => { const b = mk('button', cls, text); b.type = 'button'; b.onclick = fn; return b }
  const agoSecs = (s: number) => s < 60 ? `${Math.round(s)}s` : s < 3600 ? `${Math.floor(s / 60)}m` : s < 86400 ? `${Math.floor(s / 3600)}h ${Math.floor(s % 3600 / 60)}m` : `${Math.floor(s / 86400)}d`
  // the stream's `tasks` changed: the list redrawn, or the open task's view fetched again
  function sheetNews() {
    const key = JSON.stringify(state.tasks || [])
    if (key === tsSeen) return
    tsSeen = key
    if (!tsheet.hidden) drawTaskSheet({ quiet: true })
  }
  function drawTaskSheet({ quiet = false } = {}) {
    $('ts-list').hidden = !!tsTask; $('ts-one').hidden = !tsTask
    if (tsTask) return loadPart({ quiet })
    $('ts-list').replaceChildren(...ranked(state.tasks).map(t => {
      const row = btn('ts-row', null, () => { tsTask = t.name; tsPart = 'summary'; tsDiff = null; drawTaskSheet() })
      row.append(mk('b', '', t.name), mk('span', 'ts-phase', ' · ' + t.phase), mk('small', '', taskOn(t)))
      return row
    }))
  }
  $('ts-back').onclick = () => { tsTask = null; tsAsked++; drawTaskSheet() }
  $('ts-refresh').onclick = () => loadPart({ quiet: true })
  $('ts-reply').onclick = () => { setAbout({ task: tsTask }); showTaskSheet(false); input.focus() }
  for (const b of document.querySelectorAll<HTMLElement>('#ts-tabs [data-part]')) b.onclick = () => { tsPart = b.dataset.part as string; tsDiff = null; loadPart() }
  async function loadPart({ quiet = false } = {}) {  // quiet: the view stays until the new one comes
    const asked = ++tsAsked, name = tsTask
    $('ts-name').textContent = name
    for (const b of document.querySelectorAll<HTMLElement>('#ts-tabs [data-part]')) b.setAttribute('aria-selected', String(b.dataset.part === tsPart))
    if (!quiet || !tsBody.childElementCount) tsBody.replaceChildren(mk('span', 'ts-note', 'Loading…'))
    const extra = tsPart === 'diff' && tsDiff ? '&full=1' + (tsDiff.path ? '&path=' + encodeURIComponent(tsDiff.path) : '') : ''
    let r: Response | null, j!: TaskPart
    try {
      r = await fetch(`/api/task?name=${encodeURIComponent(name as string)}&part=${tsPart}${extra}`, { headers: { Authorization: 'Bearer ' + token }, cache: 'no-store' })
      j = await r.json().catch(() => ({}))
    } catch { r = null }
    if (asked !== tsAsked) return  // another view was asked for meanwhile
    if (!r) return tsBody.replaceChildren(mk('span', 'ts-note', 'Not loaded: try again'))
    if (r.status === 404 && !tsDiff) return tsBody.replaceChildren(mk('span', 'ts-note', `${name} is no longer open (accepted, discarded or parked).`))
    if (!r.ok) return tsBody.replaceChildren(mk('span', 'ts-note', j.error || `Not loaded (HTTP ${r.status}): try again`))
    tsBody.replaceChildren(...(tsPart === 'summary' ? drawSummary(j) : tsPart === 'diff' ? drawDiff(j) : drawLog(j)))
  }
  function drawSummary(j: TaskPart) {
    const out: HTMLElement[] = [mk('p', '', `${j.phase} · round ${j.round}${j.step! > 1 ? ` · step ${j.step}` : ''} · ${taskOn(j)}`)]
    const times = [j.created_at && `started ${agoSecs(j.now - j.created_at)} ago`, j.updated_at && `last change ${agoSecs(j.now - j.updated_at)} ago`].filter(Boolean)
    if (times.length) out.push(mk('p', 'ts-dim', times.join(' · ')))
    if (j.last) out.push(mk('p', '', j.last))
    out.push(mk('h4', '', `Report, round ${j.round}`), mk('pre', '', j.report))
    if (j.report_cut) out.push(mk('span', 'ts-note', `Cut here: wb report ${j.task} on the computer has it all.`))
    if (j.verify && j.verify.length) out.push(mk('h4', '', 'Regression check'), mk('pre', '', j.verify.join('\n')))
    return out
  }
  function drawDiff(j: TaskPart) {
    const out: HTMLElement[] = [], against = j.base === 'HEAD' ? 'this step' : 'base ' + j.base
    if (j.note) return [mk('span', 'ts-note', j.note)]
    if (!j.files.length) return [mk('span', 'ts-note', `No changes yet (against ${against}).`)]
    const files = () => {
      const list = mk('div', 'ts-files')
      for (const f of j.files) {
        const what = `${f.path}  ${f.added == null ? 'binary' : `+${f.added} −${f.deleted}`}`
        list.append(f.path.includes(' => ') ? mk('div', 'ts-file', what) : btn('ts-file', what, () => { tsDiff = { path: f.path }; loadPart() }))
      }
      return list
    }
    if (!tsDiff) {
      out.push(mk('pre', '', j.stat), btn('ts-link', 'Show the whole diff', () => { tsDiff = { path: '' }; loadPart() }))
      return out
    }
    if (tsDiff.path) out.push(btn('ts-link', '‹ All files', () => { tsDiff = null; loadPart() }))
    if (j.cut) {
      out.push(mk('span', 'ts-note', `${tsDiff.path || 'The whole diff'} is too long for the page: wb diff ${j.task} on the computer.${tsDiff.path ? '' : ' One file at a time:'}`))
      if (!tsDiff.path) out.push(files())
      return out
    }
    const pre = mk('pre', 'ts-diff')
    for (const line of j.diff.split('\n')) {  // each line its own span, coloured by its first character
      const cls = /^(\+\+\+|---) /.test(line) || line.startsWith('diff ') ? 'meta' : line[0] === '+' ? 'add' : line[0] === '-' ? 'del' : line.startsWith('@@') ? 'hunk' : ''
      pre.append(mk('span', cls, line + '\n'))
    }
    out.push(pre)
    if (j.lines_cut) out.push(mk('span', 'ts-note', `${j.lines_cut} long line${j.lines_cut > 1 ? 's' : ''} cut short (…).`))
    if (!tsDiff.path) out.push(btn('ts-link', 'Show the stat', () => { tsDiff = null; loadPart() }))
    return out
  }
  function drawLog(j: TaskPart) {
    if (!j.rows.length) return [mk('span', 'ts-note', 'Nothing logged yet.')]
    const pre = mk('pre', 'ts-log')
    for (const r of j.rows) pre.append(mk('span', r.ok === false ? 'del' : '', `${r.ts ? clock(r.ts) : ''} r${r.round ?? '?'} ${r.text}\n`))
    const out: HTMLElement[] = [pre]
    if (j.more) out.unshift(mk('span', 'ts-note', `The last ${j.rows.length} rows: wb log ${j.task} on the computer has the rest.`))
    return out
  }

  function render() {
    const list = items()
    $('intro').hidden = list.length > 0
    drawHint()
    const want: { key: string; day?: string; m?: Msg; same?: boolean | null }[] = []
    let prev: Msg | null = null
    for (const m of list) {
      const day = dayName(m.ts)
      if (!prev || dayName(prev.ts) !== day) want.push({ key: 'day-' + day, day })
      want.push({ key: m.local ? 'local-' + m.cid : (m.cid && nodes.has('local-' + m.cid) ? 'local-' + m.cid : m.id), m,
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
        else el = makeRow(w.m!)
        nodes.set(w.key, el)
      }
      if (w.m) {
        el.className = `row ${w.m.role === 'agent' ? 'agent' : w.m.from}${w.m.from === 'claude' && !forUser(w.m) ? ' to-agent' : ''}${w.same ? ' same' : ''}${w.m.local ? ' pending' : ''}${w.m.error ? ' failed' : ''}`
        // a row's line made anew only when it says something new: one made on every update was
        // read out again and took the focus from its buttons (#1408, #1409)
        const meta = metaFor(w.m), was = el.querySelector<HTMLElement>('.meta')!, key = meta.textContent + '\n' + (w.m.text || '')
        // and once a "send again" it waits on has settled (landed, or deleted): kept, its button
        // stayed disabled until a reload (the #1408 review)
        const waits = was.querySelector<HTMLElement>('button[data-cid]:disabled')
        if (was.dataset.said !== key || (waits && !outbox.some(o => o.cid === waits.dataset.cid))) { meta.dataset.said = key; was.replaceWith(meta) }
      }
      if (anchor.nextSibling !== el) anchor.after(el)
      anchor = el
    }
    drawWorking(list, anchor)
    drawPresence()
    sweepSoon()
    toEnd()
    if (list.length) firstVisit = false
    drawnAt = performance.now()
  }

  // ---- wb shot's pairs (#99): images behind the token, so fetched with it, shown as blobs ----
  const shotOf = (s: Shots) => `${s.task} · step ${s.step} · round ${s.round}`
  function shotImage(s: Shots, side: 'base' | 'candidate', fig: HTMLElement) {
    // a capture that exited nonzero failed, whatever it wrote (#268)
    // dropped with its archived task, months on (#821)
    const expired = 'This screenshot has expired: an archived task keeps its screenshots 90 days.'
    if (s[side].pruned) { fig.prepend(span('missing', expired)); return }
    if (!s[side].image) { fig.prepend(span('missing', `No image: the capture failed${s[side].exit != null ? ` (exit ${s[side].exit})` : ''}. The code review stands on its own.`)); return }
    const img = document.createElement('img')
    img.alt = `${side === 'base' ? 'Base' : 'Candidate'} of ${shotOf(s)}`
    fig.prepend(img)
    fetch(`/api/shot?task=${encodeURIComponent(s.task)}&n=${s.n}&side=${side}`,
          { headers: { Authorization: 'Bearer ' + token }, cache: 'no-store' })
      .then(r => r.ok ? r.blob() : Promise.reject(r.status))
      .then(b => { img.src = URL.createObjectURL(b) })
      .catch(st => img.replaceWith(span('missing', st === 410 ? expired : 'This image could not be loaded.')))
  }
  function makeShots(s: Shots) {
    const box = document.createElement('div')
    const grid = document.createElement('div'); grid.className = 'shots'
    for (const side of ['base', 'candidate'] as const) {
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
  // ---- Claude's shortlist of wb audio's takes (#397): Claude can't hear, so the user picks by
  // ear. Each take is fetched with the token and played from a blob; Keep sends the pick at once.
  function makeListen(l: Listen) {
    const box = document.createElement('div'); box.className = 'takes'
    for (const c of l.clips || []) {
      const take = document.createElement('figure'); take.className = 'take'
      const cap = document.createElement('figcaption')
      cap.append(Object.assign(document.createElement('b'), { textContent: `Take ${c.n}` }), span('', ' · ' + c.name))
      take.append(cap)
      const player = document.createElement('audio'); player.controls = true; player.preload = 'metadata'
      player.setAttribute('aria-label', `Take ${c.n}, ${c.name}`)
      take.append(player)
      fetch(`/api/audio?batch=${encodeURIComponent(l.batch)}&n=${c.n}`,
            { headers: { Authorization: 'Bearer ' + token }, cache: 'no-store' })
        .then(r => r.ok ? r.blob() : Promise.reject(r.status))
        .then(b => { player.src = URL.createObjectURL(b) })
        .catch(() => player.replaceWith(span('missing', 'This take is no longer kept.')))
      if (c.line) take.append(span('take-line', c.line))
      const keep = document.createElement('button'); keep.type = 'button'; keep.className = 'keep'; keep.textContent = 'Keep this one'
      keep.onclick = () => pick({ audio: l.batch, n: c.n, name: c.name })
      take.append(keep)
      box.append(take)
    }
    return box
  }
  // a task alone (`{task}`): from the task sheet (#1201)
  const aboutBody = (a: About) => a.audio ? { audio: a.audio, n: a.n } : a.n == null ? { task: a.task } : { task: a.task, n: a.n }
  const aboutTag = (a: About) => a.audio ? `Kept take ${a.n} · ${a.name}` : a.n == null ? `About ${a.task}` : `About ${a.task}, step ${a.step}`

  // what the next message is about: a pair of screenshots, so the reply reaches its task and step
  let about: About | null = load(K.about, null)
  function setAbout(a: About | null) {
    about = a; save(K.about, a)
    $('about').hidden = !a
    $('about-text').textContent = !a ? '' : a.audio ? aboutTag(a) : a.n == null ? `Replying about ${a.task}` : `Replying about ${a.task}, step ${a.step}`
  }
  $('about-x').onclick = () => setAbout(null)
  setAbout(about)

  function makeRow(m: Msg) {
    const row = document.createElement('div')
    const b = document.createElement('div'); b.className = 'bubble'
    if (m.about) row.append(span('about-tag', aboutTag(m.about)))
    if (m.files && m.files.length) row.append(makeFiles(m.files))
    if (m.from === 'user' || m.role === 'agent') b.textContent = m.text
    else if (seen.has(m.id) || firstVisit || document.hidden || reduced.matches) {
      if (!seen.has(m.id) && !firstVisit && document.hidden && forUser(m)) { unread++; document.title = `(${unread}) Workbench` }
      rich(b, m.text)
      if (!seen.has(m.id) && !firstVisit) say(m.text)
      seen.add(m.id); save(K.seen, [...seen].slice(-500))
    } else {
      b.classList.add('flowing'); flows.set(m.id, { text: m.text, shown: 0, el: b })
      if (!flowTimer) flowTimer = setInterval(flowTick, FLOW_MS)
    }
    if ((m.from !== 'user' && m.role !== 'agent') || m.text) row.append(b)
    if (m.shots) row.append(makeShots(m.shots))
    if (m.audio) row.append(makeListen(m.audio))
    row.append(document.createElement('div'))
    ;(row.lastChild as HTMLElement).className = 'meta'
    return row
  }

  // the driver has the latest message and is mid-turn: say so, counting from when it was read
  let working: HTMLElement | null = null, workTimer = 0
  function drawWorking(list: Msg[], anchor: HTMLElement) {
    const last = [...list].reverse().find(m => !m.local)
    const on = last && (last.from === 'user' || last.role === 'agent') && last.received && state.listener && state.listener.busy
    if (!on) { if (working) { working.remove(); working = null; clearInterval(workTimer); workTimer = 0 } return }
    if (!working) {
      working = document.createElement('div'); working.className = 'working'
      const dots = document.createElement('span'); dots.className = 'dots'; dots.append(document.createElement('i'), document.createElement('i'), document.createElement('i'))
      working.append(dots, span('', ''))
      workTimer = setInterval(() => { if (working) working.lastChild!.textContent = driverName() + ' is on it · ' + since(Number(working.dataset.from)) }, 1000)
      say(driverName() + ' is on it')  // once, not its count each second (#1408)
    }
    working.dataset.from = String(last.received!.ts)
    working.lastChild!.textContent = driverName() + ' is on it · ' + since(last.received!.ts)
    if (anchor.nextSibling !== working) anchor.after(working)
  }

  function drawPresence() {
    const l = state.listener
    const where = l ? l.cwd.replace(/^\/home\/[^/]+/, '~') : ''
    const waits = !!l && !!state.driver && state.driver.state === 'waiting'
    presence.className = 'presence ' + (live && !reachable ? 'down' : !l ? 'away' : waits ? 'waiting' : l.busy ? 'working' : 'listening')
    presenceText.textContent = !token ? 'not connected' : !live ? 'connecting…' : !reachable ? 'computer unreachable · messages wait'
      : !l ? 'no session listening · messages wait' :
      // whom you talk to here (#103: a notification opened on "Claude"), the driver's name (#1319)
      driverName() + ' · ' + (waits ? 'waiting for you in the terminal' : (l.busy ? 'working in ' : 'listening in ') + where)
    drawState()
  }

  // The state line (#1355): the driver's state at the top of the chat, the server's (chat.driver_state,
  // in the story's words), while it asks something of the user: waiting on a prompt in the
  // terminal (what it is, what to do, and for a permission the way to run with permissions granted,
  // which Copy copies), no session listening (how to start one), or the chat switched off (how
  // to turn it on: starting a driver can't make it listen then, #1526). Only the terminal answers a
  // prompt (#1350): Copy is its one button. It took the waiting line's place in the thread.
  const stateLine = $('driver-state')
  function drawState() {
    const d = state.driver
    const show = !!token && live && reachable && !!d && (d.state === 'waiting' || d.state === 'none' || d.state === 'off')
    stateLine.hidden = !show
    if (!show || !d) { stateLine.dataset.key = ''; return }
    const key = JSON.stringify([d.state, d.line, d.next, d.waiting && d.waiting.what, d.grant && d.grant.line])
    if (stateLine.dataset.key === key) return
    stateLine.dataset.key = key
    stateLine.className = 'dstate ' + d.state
    const parts: HTMLElement[] = []
    if (d.state === 'waiting' || d.state === 'off') parts.push(span('dstate-line', d.line))
    if (d.waiting && d.waiting.what) { const c = document.createElement('code'); c.className = 'dstate-what'; c.textContent = d.waiting.what; parts.push(c) }
    const next = span('dstate-next', ''); storyInto(next, d.next); parts.push(next)  // off's names `wb chat on`
    if (d.grant) {
      const g = document.createElement('span'); g.className = 'dstate-grant'
      storyInto(g, d.grant.line)
      const b = document.createElement('button'); b.type = 'button'; b.className = 'plain dstate-copy'; b.textContent = 'Copy'
      b.setAttribute('aria-label', 'Copy ' + d.grant.command)
      const cmd = d.grant.command
      b.onclick = async () => { b.textContent = await copyText(cmd) ? 'Copied' : 'Copy failed'; setTimeout(() => { b.textContent = 'Copy' }, 1500) }
      parts.push(g, b)
    }
    stateLine.replaceChildren(...parts)
    if (d.state === 'waiting' && !firstVisit) say(d.line + (d.waiting && d.waiting.what ? ' ' + d.waiting.what : ''))
  }

  // The story's sentences on the page (#1355, story.ts, generated from workbench/story.py): each
  // element with `data-story="<key>"` says that sentence for the driver listening (else the last
  // that wrote, else any driver), its `code` spans as code
  const driverKind = () => (state.driver && state.driver.driver) || 'any'
  function storyInto(el: HTMLElement, text: string) {
    el.replaceChildren(...text.split('`').map((part, i) => {
      if (i % 2 === 0) return document.createTextNode(part)
      const c = document.createElement('code'); c.textContent = part; return c
    }))
  }
  let storyFor = ''
  function drawStory() {
    const kind = driverKind()
    if (kind === storyFor) return
    storyFor = kind
    for (const el of document.querySelectorAll<HTMLElement>('[data-story]')) storyInto(el, storyText(el.dataset.story!, kind))
  }

  // ---- the connection ----
  let online = navigator.onLine, conn: AbortController | null = null, failures = 0, retryTimer = 0, lastRx = 0, hiddenTimer = 0
  let build = ''  // the page's files when this page loaded (the server's `build`)
  const HELD = "This browser's storage is full, so your message stays in the box until it's sent: keep this page open."
  function banner(text: string, bad?: boolean) {
    if (held) { text = text ? text + ' ' + HELD : HELD; bad = true }  // said for as long as it waits
    if (!text) { net.hidden = true; return }
    net.textContent = text; net.className = 'net' + (bad ? ' bad' : ''); net.hidden = false
  }
  // The server's `now` of the newest state taken. A POST's reply (`reply`) older than it is
  // skipped: the stream's patches build on what the stream sent, so a row the stream had sent
  // that an older reply lacked would never come again (#755)
  let stamp = 0
  function apply(s: Snapshot, { reply = false, quiet = false } = {}) {
    if (reply && typeof s.now === 'number' && s.now < stamp) return render()
    if (typeof s.now === 'number') { skew = s.now - Date.now() / 1000; stamp = Math.max(stamp, s.now) }
    if (s.build && build && s.build !== build && reloadOnce()) return  // updated: the new page
    if (s.build) build = s.build
    live = true; reachable = true
    if (!quiet) save(K.thread, (s.messages || []).slice(-50))  // a patch without messages: no rewrite (#755)
    state = { messages: s.messages || [], push: s.push || {}, listener: s.listener || null, savings: s.savings || null, expire: s.expire || 0, uploads: s.uploads || null, tasks: s.tasks || [], runs: s.runs || [], dictate: !!s.dictate,
      driver: s.driver || null, settings: s.settings || null }
    drawTasks(); drawRuns(); drawMic(); drawStory()
    drawShown()
    if (state.push.key && !pushSynced) { pushSynced = true; syncPush() }
    reportSeen()
    const onServer = new Set(state.messages.map(m => m.cid).filter(Boolean))
    for (const o of outItems()) if (onServer.has(o.cid)) { forgetFiles(o); drop(o.cid) }
    render()
    nearAgain()
    drawBadge()
    emit('state', state)
  }
  // A patch (chat.patch, #755): for messages, `put` (rows new or changed, by id: replaced
  // where they are, else added at the end, as the server's lists grow) and `drop` (ids gone); every
  // other field whole
  const merge = (list: Msg[], d: Patch['messages']) => {
    if (!d) return list
    const gone = new Set(d.drop || []), put = new Map((d.put || []).map(r => [r.id, r] as [string, Msg]))
    const out = list.filter(r => !gone.has(r.id)).map(r => { const n = put.get(r.id); put.delete(r.id); return n || r })
    return out.concat([...put.values()])
  }
  const applyPatch = (p: Patch) => apply({ ...p, messages: merge(state.messages, p.messages) },
    { quiet: !p.messages })
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
  async function post(path: string, body: unknown): Promise<Posted> {  // a POST that gives up after POST_MS silent, so a dead line can't hold it
    const ctrl = new AbortController()
    let timer = 0
    const pet = () => { clearTimeout(timer); timer = setTimeout(() => ctrl.abort(), POST_MS) }
    pet()
    try {
      const r = await fetch(path, { method: 'POST', cache: 'no-store', signal: ctrl.signal,
        headers: { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
      // the body is read under the timer too: cleared at the headers, a reply whose body stalled
      // (a phone changing networks between the two) held flush, and every later send, for as long
      // as TCP took to give up (#935). But re-armed at each chunk, as an upload's STALL_MS is: one
      // deadline for the whole body cut off a reply's snapshot on a slow link every time, so a
      // held message, which only its reply clears, stayed in the box for good (the review of #935)
      let text = ''
      if (r.body) {
        const reader = r.body.getReader(), dec = new TextDecoder()
        ctrl.signal.addEventListener('abort', () => { reader.cancel().catch(() => {}) }, { once: true })
        for (;;) {
          pet()
          const { value, done } = await reader.read()
          if (done) break
          text += dec.decode(value, { stream: true })
        }
        // a cancelled read can end as done rather than throw: half a reply is no reply
        if (ctrl.signal.aborted) throw new DOMException('the reply stalled', 'AbortError')
        text += dec.decode()
      } else text = await r.text()
      return { ok: r.ok, status: r.status, headers: r.headers, json: async () => JSON.parse(text) }
    } finally { clearTimeout(timer) }
  }
  // "try again later", from whatever is in front of the chat (a proxy, a tunnel): never a refusal,
  // for an upload or a send (#936)
  const transient = (status: number) => status === 408 || status === 425 || status === 429
  // a Retry-After (seconds or a date) in ms, held under BACKOFF's last step; 0 when it says nothing
  function retryAfter(r: { headers?: Headers }) {
    const v = (r.headers && r.headers.get('Retry-After')) || ''
    const ms = /^\d+$/.test(v.trim()) ? Number(v) * 1000 : Date.parse(v) - Date.now()
    return ms > 0 ? Math.min(ms, BACKOFF[BACKOFF.length - 1]) : 0
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
          if ((event === 'state' || event === 'patch') && data) {
            try { event === 'state' ? apply(JSON.parse(data)) : applyPatch(JSON.parse(data)) } catch {}
            if (failures) { failures = 0; banner(''); reshowView() }
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
    if (reachable && (!online || failures > 2)) { reachable = false; drawPresence() }
    retryTimer = setTimeout(connect, wait)
  }
  const reconnectNow = () => { if (conn) { conn.abort(); conn = null } failures = Math.min(failures, 1); connect() }
  // A silent line counts as a failure (its own retry, backing off): reconnecting at once kept
  // failures at 1, so a server that took the call and never answered left "Reconnecting…" up for
  // ever, never the banner that says the computer can't be reached (2026-11-18)
  setInterval(() => { if (conn && Date.now() - lastRx > DEAD_MS) conn.abort() }, 5000)
  // back on screen (or from the back-forward cache) with a line that's likely dead from the sleep:
  // dial now rather than wait out the watchdog
  // A phone asleep a few seconds after the last ping came back to a line heard from under
  // STALE_MS ago, kept it, and showed nothing until the watchdog gave up on it, 10 to 40 s while
  // the push had already arrived (the owner, 2026-11-20): off screen past AWAY_MS, dial again
  let hiddenAt = 0
  const freshen = () => {
    const away = hiddenAt && Date.now() - hiddenAt > AWAY_MS
    hiddenAt = 0
    if (conn && (away || Date.now() - lastRx > STALE_MS)) reconnectNow()
  }
  // another tab queued, sent or dropped a message: show it, and send it if this tab can
  addEventListener('storage', e => { if (e.key === null || e.key.startsWith(OUT)) { outbox = outItems(); render(); flush(true) } })
  addEventListener('online', () => { online = true; banner(''); reconnectNow(); flush(true); render(); reshowView() })
  addEventListener('offline', () => { online = false; reachable = false; banner('Offline. Messages will send when the connection is back.', true); render() })
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      finishAll()
      hiddenAt = Date.now()
      hiddenTimer = setTimeout(() => { if (document.hidden && conn) { conn.abort(); conn = null } }, HIDDEN_GRACE_MS)
    } else {
      clearTimeout(hiddenTimer)
      unread = 0; document.title = 'Workbench'
      if (!conn) { failures = 0; connect() } else freshen()
      flush(true)
    }
  })

  const letGo = () => { finishAll(); if (conn) { conn.abort(); conn = null } }
  // the worker's push: news this page hasn't shown, so its line is likely dead
  if ('serviceWorker' in navigator) navigator.serviceWorker.addEventListener('message', e => {
    if (e.data && e.data.kind === 'push' && !document.hidden && token) reconnectNow()
    if (e.data && e.data.kind === 'view') go(e.data.view)   // a notification's click: the thread (#1284)
  })
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
  // The page's height with no keyboard: the tallest seen at this width. Android's Chrome, under the
  // page's interactive-widget=resizes-content, shrinks innerHeight with the keyboard as well as the
  // visual viewport, so their difference never said it was up and the nav stayed over it (#1414,
  // 2026-11-25). Only while typing on a touch screen: a desktop window made shorter isn't a keyboard
  let full = innerHeight, fullW = innerWidth
  function fit() {
    if (!vv) return
    if (innerWidth !== fullW) { full = innerHeight; fullW = innerWidth }   // turned: another height
    full = Math.max(full, innerHeight)
    kbOpen = innerHeight - vv.height > 120     // a keyboard is tall; a toolbar showing is not
      || (touch && typing() && full - vv.height > 120)
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
  let ladder: number[] = []
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
  let touch0: { y: number; x: number; at: number; target: EventTarget | null; moved?: boolean } | null = null
  document.addEventListener('touchstart', e => {
    const t = e.touches[0]
    touch0 = typing() && e.touches.length === 1 && !(e.target as Element).closest('.composer')
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
    if (t && !t.moved && Date.now() - t.at < 500 && typing() && !(t.target as Element).closest?.(TAPPABLE)) input.blur()
  }, { passive: true })

  // ---- attachments (#623): picked, uploaded once by the id each got when picked, sent by id ----
  // The owner (2026-11-20, from the phone): simple, and never a file uploaded twice by a refresh.
  // The draft's files (wb-chat-files: [{id, name, size, type, mod, state: up|retry|done|refused,
  // why?}]) outlive a reload, their bytes in IndexedDB until the message carrying them lands; before
  // it sends a byte the page asks whether the computer has the id, and the computer stores an id
  // once whatever is sent again (chat.save_upload)
  const FILES = 'wb-chat-files'
  const MiB = 1 << 20
  const STALL_MS = 30000  // an upload with no progress this long is dropped and tried again
  const limits = () => state.uploads || { file: 25 * MiB, message: 50 * MiB, files: 10 }
  const fmtSize = (n: number) => n < 1024 ? n + ' B' : n < MiB ? Math.round(n / 1024) + ' KB' : (n / MiB).toFixed(n < 10 * MiB ? 1 : 0) + ' MB'
  const isPic = (t?: string) => /^image\/(png|jpeg|gif|webp|avif)$/.test(t || '')
  const extOf = (name?: string) => ((/\.([A-Za-z0-9]{1,4})$/.exec(name || '') || [])[1] || 'file')
  const bytes = new Map<string, Blob>()  // id → the picked File, while this page holds it
  const db = (() => {      // the bytes across a reload; where IndexedDB is missing, memory alone
    let opened: Promise<IDBDatabase> | null = null
    const open = () => opened || (opened = new Promise<IDBDatabase>((res, rej) => {
      const r = indexedDB.open('wb-chat', 1)
      r.onupgradeneeded = () => r.result.createObjectStore('files')
      r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error)
    }))
    const run = async (mode: IDBTransactionMode, fn: (s: IDBObjectStore) => IDBRequest) => {
      const t = (await open()).transaction('files', mode), req = fn(t.objectStore('files'))
      return new Promise<any>((res, rej) => { t.oncomplete = () => res(req.result); t.onerror = t.onabort = () => rej(t.error) })
    }
    return {
      put: (k: string, v: Blob) => run('readwrite', s => s.put(v, k)).catch(() => {}),
      get: (k: string) => run('readonly', s => s.get(k)).catch(() => undefined),
      del: (k: string) => run('readwrite', s => s.delete(k)).catch(() => {}),
    }
  })()
  let draft: DraftFile[] = (load(FILES, []) || []).filter((d: DraftFile) => d && d.id)
  const keepDraft = () => save(FILES, draft)
  const sendable = () => draft.filter(d => d.state !== 'refused')
  function armSend() { sendBtn.disabled = !(input.value.trim() || sendable().length) }

  class Refused extends Error {}
  // A server failure is tried again, but not for good: a full disk or a name it can't save
  // answered 500 or 507 to every try, and each re-sent the whole file (#742). One the server
  // calls `final`, or the SERVER_TRIES-th in a row, is a refusal, its reason on the chip.
  const SERVER_TRIES = 3
  const inflight = new Map<string, Promise<void>>(), progress = new Map<string, number>(), serverFails = new Map<string, number>()
  function upload(f: WbFile): Promise<void> {  // resolves once the computer has file f ({id, name, size, type})
    if (inflight.has(f.id)) return inflight.get(f.id)!
    const p = (async () => {
      // the check under post()'s deadline: one that never answered (a phone changing networks)
      // held this promise, so pump and flush with it, and every later message sat at Sending…
      // until a reload (#1390, 2026-11-25, as #935 for /api/send). Only its status is read
      const ctrl = new AbortController(), dog = setTimeout(() => ctrl.abort(), POST_MS)
      const r = await fetch('/api/upload/' + encodeURIComponent(f.id), { headers: { Authorization: 'Bearer ' + token }, cache: 'no-store', signal: ctrl.signal })
        .finally(() => clearTimeout(dog))
      if (r.ok) return                                   // already there: not a byte again
      if (r.status === 401) { keyless('That key was not accepted.'); throw new Error('key') }
      if (r.status !== 404) throw new Error('HTTP ' + r.status)
      const blob = bytes.get(f.id) || await db.get(f.id)
      if (!blob) throw new Refused('no longer on this device: pick it again')
      await put(f, blob)
    })().finally(() => inflight.delete(f.id))
    inflight.set(f.id, p)
    return p
  }
  function put(f: WbFile, blob: Blob) {  // XHR, for its upload progress; fetch has none
    return new Promise((resolve, reject) => {
      const x = new XMLHttpRequest()
      let dog = 0
      const pet = () => { clearTimeout(dog); dog = setTimeout(() => x.abort(), STALL_MS) }
      x.open('PUT', '/api/upload/' + encodeURIComponent(f.id))
      x.setRequestHeader('Authorization', 'Bearer ' + token)
      x.setRequestHeader('Content-Type', f.type || 'application/octet-stream')
      x.setRequestHeader('X-File-Name', encodeURIComponent(f.name))
      x.upload.onprogress = e => { pet(); if (e.lengthComputable) showProgress(f.id, e.loaded / e.total) }
      x.onload = () => {
        clearTimeout(dog)
        let j: any = {}
        try { j = JSON.parse(x.responseText) } catch {}
        if (x.status === 200) { serverFails.delete(f.id); resolve(j.upload) }
        else if (x.status === 401) { keyless('That key was not accepted.'); reject(new Error('key')) }
        else if (j.final || (x.status >= 400 && x.status < 500 && !transient(x.status))) reject(new Refused(j.error || 'refused'))
        else if (x.status >= 500) {
          const n = (serverFails.get(f.id) || 0) + 1
          serverFails.set(f.id, n)
          reject(n >= SERVER_TRIES ? new Refused(j.error || 'not saved: HTTP ' + x.status) : new Error('HTTP ' + x.status))
        } else reject(new Error('HTTP ' + x.status))
      }
      x.onerror = x.onabort = () => { clearTimeout(dog); reject(new Error('network')) }
      pet(); x.send(blob)
    })
  }
  function showProgress(id: string, frac: number) {
    progress.set(id, frac)
    const bar = tray.querySelector<HTMLElement>(`[data-id="${id}"] .bar i`)
    if (bar) bar.style.width = Math.round(frac * 100) + '%'
  }

  // the draft's uploads, one at a time; a network failure waits and tries again, a refusal stays
  let pumpTimer = 0, pumpTries = 0, pumping = false
  async function pump() {
    if (pumping || !token) return
    pumping = true; clearTimeout(pumpTimer)
    let again = false
    for (const d of draft.filter(d => d.state === 'up' || d.state === 'retry')) {
      try { await upload(d); setChip(d.id, { state: 'done' }) }
      catch (e) {
        if (e instanceof Refused) setChip(d.id, { state: 'refused', why: e.message })
        else if ((e as Error).message === 'key') break
        else { again = true; setChip(d.id, { state: 'retry' }) }
      }
    }
    pumping = false
    if (again) pumpTimer = setTimeout(pump, BACKOFF[Math.min(pumpTries++, BACKOFF.length - 1)])
    else pumpTries = 0
  }
  function setChip(id: string, change: Partial<DraftFile>) {
    const d = draft.find(x => x.id === id)
    if (!d) return  // removed meanwhile
    Object.assign(d, change)
    if (d.state !== 'refused') delete d.why
    keepDraft(); drawTray(); armSend()
  }
  function addFiles(list: File[]) {
    const lim = limits()
    for (const file of list) {
      if (draft.some(d => d.name === file.name && d.size === file.size && d.mod === file.lastModified)) continue  // the same file again
      const d: DraftFile = { id: newId(), name: file.name || 'pasted', size: file.size, type: file.type || '', mod: file.lastModified || 0, state: 'up' }
      const ok = sendable(), room = lim.message - ok.reduce((n, x) => n + x.size, 0)
      const why = ok.length >= lim.files ? `one message takes ${lim.files} files: send these first`
        : file.size > lim.file ? `too big: ${fmtSize(file.size)}, the limit is ${fmtSize(lim.file)}`
        : file.size > room ? `over the message's ${fmtSize(lim.message)}: send it in the next one` : ''
      if (why) Object.assign(d, { state: 'refused', why })
      else { bytes.set(d.id, file); db.put(d.id, file) }
      draft.push(d)
    }
    keepDraft(); drawTray(); armSend(); pump()
  }
  function unpick(id: string) {
    draft = draft.filter(d => d.id !== id)
    keepDraft(); db.del(id); bytes.delete(id); drawTray(); armSend(); sweepSoon()
  }
  const forgetFiles = (o?: { files?: WbFile[] } | null) => { for (const f of (o && o.files) || []) { db.del(f.id); bytes.delete(f.id) } }

  // the tray: one chip a file above the box, a thumbnail or the file's kind, its name, its size or
  // why it can't go, and ✕
  const tray = $('tray')
  function drawTray() {
    tray.hidden = !draft.length
    tray.replaceChildren(...draft.map(d => {
      const el = document.createElement('div'); el.className = 'att ' + d.state; el.dataset.id = d.id
      el.append(fileFace(d), fileText(d, d.state === 'refused' ? d.why
        : d.state === 'retry' ? fmtSize(d.size) + ' · waiting for the connection'
        : d.state === 'up' ? fmtSize(d.size) + ' · uploading' : fmtSize(d.size)))
      if (d.state === 'up' || d.state === 'retry') {
        const bar = document.createElement('span'), i = document.createElement('i')
        bar.className = 'bar'; i.style.width = Math.round((progress.get(d.id) || 0) * 100) + '%'; bar.append(i); el.append(bar)
      }
      const x = document.createElement('button'); x.type = 'button'; x.className = 'x'; x.textContent = '✕'
      x.setAttribute('aria-label', 'Remove ' + d.name); x.onclick = () => unpick(d.id)
      el.append(x)
      return el
    }))
  }
  function fileFace(f: Partial<WbFile>) {
    if (isPic(f.type)) {
      const img = document.createElement('img'); img.className = 'face'; img.alt = ''
      fileURL(f.id!).then(u => { img.src = u }).catch(() => {})
      return img
    }
    return span('face kind', extOf(f.name))
  }
  function fileText(f: { name: string }, line?: string) {
    const t = document.createElement('span'); t.className = 'nm'
    t.append(span('', f.name), span('', line))
    return t
  }
  // a file's bytes as a blob URL: this page's own, else IndexedDB's, else the computer's (behind the token)
  const urls = new Map<string, Promise<string>>()
  // An object URL keeps its blob alive until revoked: four photos picked and removed held four
  // full-size blobs until the page closed (#746). Each render and removal lets go of every URL no
  // chip, thread row or open viewer names, in a microtask: render runs before `urls` exists.
  function sweepSoon() {
    if (sweeping) return
    sweeping = true
    queueMicrotask(() => {
      sweeping = false
      const used = new Set(draft.map(d => d.id))
      for (const m of items()) for (const f of m.files || []) used.add(f.id)
      if (viewing) used.add(viewing)
      for (const [id, p] of urls) {
        if (used.has(id)) continue
        urls.delete(id)
        p.then(u => URL.revokeObjectURL(u), () => {})
      }
    })
  }
  // `mine`: only bytes this browser holds; the computer's are left for later (LATER)
  const LATER = new Error('later')
  function fileURL(id: string, mine?: boolean): Promise<string> {
    if (!urls.has(id)) {
      urls.set(id, (async () => {
        let b = bytes.get(id) || await db.get(id)
        if (!b) {
          if (mine) throw LATER
          // the browser's cache may answer: an upload never changes, so the server lets it keep one
          // (every photo in the thread was fetched whole again on every load, #754)
          const r = await fetch('/api/file/' + encodeURIComponent(id), { headers: { Authorization: 'Bearer ' + token } })
          if (!r.ok) throw new Error('HTTP ' + r.status)
          b = await r.blob()
        }
        return URL.createObjectURL(b)
      })().catch(e => { urls.delete(id); throw e }))
    }
    return urls.get(id)!
  }
  // A thumbnail is fetched once it scrolls near the view, not every photo in the thread at once (#754)
  // (made on first use: a thread restored at load renders before this line has run).
  // Until the server's thread is in, a thumbnail near the view loads only from this browser's own
  // bytes: the thread saved at the last visit showed a photo near its end for a frame, and it was
  // fetched whole, though the newer messages then put it far above the view (CI, 2026-11-24). One
  // left for later is looked at again once the server's thread is drawn (`nearAgain`), or at once
  // when that thread came while this browser's own store was still being asked.
  // An entry is measured at a frame and delivered as a later task, so a render can come between:
  // the saved thread's photo, measured near its end, was delivered after the server's thread had
  // put it far above and fetched whole (CI, 2026-11-24, #1223). One measured before the last
  // render is observed afresh, to be judged where that render put it.
  function whenNear(el: HTMLElement, load: () => unknown) {
    if (!('IntersectionObserver' in window)) return load()
    nearView = nearView || new IntersectionObserver(es => {
      for (const e of es) {
        if (!e.isIntersecting) continue
        nearView!.unobserve(e.target)
        if (e.time < drawnAt) nearView!.observe(e.target)
        else (e.target as Near).wbLoad()
      }
    }, { root: document.querySelector('main'), rootMargin: '600px 0px' })
    ;(el as Near).wbLoad = load; nearView.observe(el)
  }
  function nearAgain() {  // observed afresh: each is judged where the server's thread put it
    for (const el of nearLater) { nearLater.delete(el); if (el.isConnected) { nearView!.unobserve(el); nearView!.observe(el) } }
  }
  // in the thread: images as thumbnails (a tap shows one whole), other files as name and size
  function makeFiles(files: WbFile[]) {
    const box = document.createElement('div'); box.className = 'files'
    for (const f of files) {
      const b = document.createElement('button'); b.type = 'button'
      if (isPic(f.type)) {
        b.className = 'pic'; b.setAttribute('aria-label', 'Show ' + f.name)
        const img = document.createElement('img'); img.alt = f.name; b.append(img)
        whenNear(b, () => fileURL(f.id, !live).then(u => { img.src = u }).catch(e => { if (e === LATER) { nearLater.add(b); if (live) nearAgain(); return } img.remove(); b.className = 'doc'; b.append(fileFace({ name: f.name }), fileText(f, 'no longer on the computer')) }))
        b.onclick = () => { if (b.className === 'pic') view(f) }
      } else {
        b.className = 'doc'; b.setAttribute('aria-label', 'Save ' + f.name)
        b.append(fileFace(f), fileText(f, fmtSize(f.size)))
        b.onclick = () => fileURL(f.id).then(u => { const a = document.createElement('a'); a.href = u; a.download = f.name; a.click() }).catch(() => {})
      }
      box.append(b)
    }
    return box
  }
  function view(f: WbFile) {
    const v = document.createElement('div'); v.className = 'viewer'; v.setAttribute('role', 'dialog'); v.setAttribute('aria-label', f.name)
    const img = document.createElement('img'); img.alt = f.name; v.append(img)
    viewing = f.id
    fileURL(f.id).then(u => { img.src = u }).catch(() => v.remove())
    const esc = (e: KeyboardEvent) => { if (e.key === 'Escape') close() }
    const close = () => { v.remove(); removeEventListener('keydown', esc); if (viewing === f.id) viewing = null; sweepSoon() }
    v.onclick = close; addEventListener('keydown', esc)
    document.body.append(v)
  }
  // picked with the button, pasted into the box, or dropped on the page
  $('attach').onclick = () => $('pick').click()
  $('pick').onchange = e => { addFiles([...(e.target as HTMLInputElement).files!]); (e.target as HTMLInputElement).value = '' }
  input.addEventListener('paste', e => {
    const fs = [...((e.clipboardData && e.clipboardData.files) || [])]
    if (fs.length) { e.preventDefault(); addFiles(fs) }
  })
  const dragsFiles = (e: DragEvent) => [...((e.dataTransfer && e.dataTransfer.types) || [])].includes('Files')
  document.addEventListener('dragover', e => { if (dragsFiles(e)) { e.preventDefault(); document.body.classList.add('dropping') } })
  document.addEventListener('dragleave', e => { if (!e.relatedTarget) document.body.classList.remove('dropping') })
  document.addEventListener('drop', e => {
    document.body.classList.remove('dropping')
    if (dragsFiles(e)) { e.preventDefault(); if (token && !$('composer').hidden) addFiles([...e.dataTransfer!.files]) }
  })
  addEventListener('storage', e => { if (e.key === FILES) { draft = (load(FILES, []) || []).filter((d: DraftFile) => d && d.id); drawTray(); armSend() } })

  // ---- dictation (#1203): a voice clip in, its words in the box ----
  // The mic shows only while the computer has a listener (the state's `dictate`). In a secure
  // context (https, or localhost) it records with MediaRecorder: a tap starts, a tap stops; over
  // plain http there is no microphone API, so it opens the phone's recorder or a voice memo
  // (accept="audio/*" capture). The clip is uploaded as an attachment is (by its id, once), and
  // POST /api/transcribe/<id> answers its words, added to the box for the user to edit and send:
  // Claude only ever gets the text. Clips waiting for their words (wb-chat-dictate, the bytes in
  // IndexedDB beside the draft's) outlive a reload; the computer keeps each one's words, so a
  // retry never transcribes twice. A GPU listener gives way to a running task at once (409), a url
  // listener's server can be down a moment (503 `retry`), and a proxy in front of the chat answers
  // 5xx while it restarts: the clip is kept and asked for again with backoff, the mic saying what
  // it waits for. A 409 dropped the clip, so once the task let the GPU go the user had to say it
  // all again (#1203); only the listener's own 502 (it ran and failed) and a 4xx drop it.
  const DICTATE = 'wb-chat-dictate'
  const RECORD_MS = 5 * 60 * 1000  // a clip stops by itself at five minutes
  const mic = $('mic')
  let clips: WbFile[] = (load(DICTATE, []) || []).filter((d: WbFile) => d && d.id), rec: MediaRecorder | null = null, recTimer = 0
  let hearing = false, hearTimer = 0, hearTries = 0, hearAt = 0
  let waiting = ''  // what the clips wait for: 'gpu' (a task's), 'listener' (not answering), or ''
  const canRecord = () => window.isSecureContext && !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia) && typeof MediaRecorder !== 'undefined'
  function drawMic() {
    const busy = clips.length > 0
    mic.hidden = !token || !(state.dictate || rec || busy)
    mic.classList.toggle('rec', !!rec)
    mic.classList.toggle('busy', !rec && busy)
    const label = rec ? 'Stop recording' : !busy ? 'Dictate'
      : waiting === 'gpu' ? 'Waiting for the GPU…' : waiting ? 'Waiting for the listener…' : 'Transcribing…'
    mic.setAttribute('aria-label', label); mic.title = label
  }
  function note(text: string) {  // a passing word on the banner, cleared unless something else took it
    banner(text, true)
    setTimeout(() => { if (net.textContent === text) banner('') }, 6000)
  }
  function hearClip(blob: Blob, name: string) {
    const lim = limits()
    if (!blob.size) return note('That clip was empty.')
    if (blob.size > lim.file) return note(`That clip is too big: ${fmtSize(blob.size)}, the limit is ${fmtSize(lim.file)}.`)
    const d = { id: newId(), name, type: (blob.type || '').split(';')[0] || 'application/octet-stream', size: blob.size }
    bytes.set(d.id, blob); db.put(d.id, blob)
    clips.push(d); save(DICTATE, clips); drawMic(); hearAll(true)
  }
  function heard(d: WbFile, text?: string) {
    clips = clips.filter(x => x.id !== d.id); save(DICTATE, clips)
    db.del(d.id); bytes.delete(d.id); drawMic()
    if (text === undefined) return
    if (!text.trim()) return note('Nothing was heard in that clip.')
    const box = input.value
    input.value = box.trim() ? box.replace(/\s*$/, ' ') + text.trim() : text.trim()
    save(K.draft, input.value); grow(); armSend()
  }
  // the listener's own verdict on a clip, or something in the way that will pass: a task on the
  // GPU (409), a listener not answering (503 `retry`), a proxy's 5xx (no JSON error from the chat
  // server), a 408/425/429, or a 200 that isn't the chat server's (#934)
  const passing = (r: Response, j: any) => r.status === 409 || transient(r.status) || r.ok
    || (r.status >= 500 && !(r.status === 502 && typeof j.error === 'string'))
  function waitFor(why: string) {
    if (why === waiting) return
    waiting = why
    if (why === 'gpu') note('A task has the GPU: the clip waits, and its words come once it is free.')
    else if (why) note("The listener isn't answering: the clip waits, and is tried again.")
  }
  async function hearAll(now?: boolean) {
    // a state's flush comes every few seconds: it asks again only once the backoff is up
    if (hearing || !token || !clips.length || (!now && Date.now() < hearAt)) return
    hearing = true; clearTimeout(hearTimer)
    let again = false, after = 0
    const tried = new Set()
    try {
      for (const d of [...clips]) {
        tried.add(d.id)
        try {
          await upload(d)
          const r = await fetch('/api/transcribe/' + encodeURIComponent(d.id), { method: 'POST', cache: 'no-store',
            headers: { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json' }, body: '{}' })
          const j = await r.json().catch(() => ({}))
          if (r.ok && typeof j.text === 'string') { waitFor(''); heard(d, j.text) }
          else if (r.status === 401) { keyless('That key was not accepted.'); break }
          else if (passing(r, j)) {
            again = true; after = Math.max(after, retryAfter(r))
            if (r.status === 409) { waitFor('gpu'); break }  // the next clip needs the same GPU
            if (j.retry) waitFor('listener')
          }
          else { heard(d); note('Not transcribed: ' + (j.error || 'HTTP ' + r.status) + '.') }
        } catch (e) {
          if (e instanceof Refused) { heard(d); note('Not transcribed: ' + e.message + '.') }
          else if ((e as Error).message === 'key') break
          else again = true
        }
      }
    } finally {
      hearing = false
      if (!clips.length) waiting = ''
      drawMic()
      if (again) {
        const ms = Math.max(after, BACKOFF[Math.min(hearTries++, BACKOFF.length - 1)])
        hearAt = Date.now() + ms; hearTimer = setTimeout(() => hearAll(true), ms)
      } else {
        hearTries = 0; hearAt = 0
        if (clips.some(d => !tried.has(d.id))) setTimeout(() => hearAll(true))  // recorded meanwhile
      }
    }
  }
  async function record() {
    let stream: MediaStream
    try { stream = await navigator.mediaDevices.getUserMedia({ audio: true }) }
    catch (e) { return note("The microphone isn't allowed here (" + ((e as Error).name || e) + "): the keyboard's dictation works too.") }
    const kind = ['audio/webm;codecs=opus', 'audio/mp4', 'audio/ogg;codecs=opus', 'audio/webm']
      .find(t => MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(t))
    let r: MediaRecorder
    try { r = new MediaRecorder(stream, kind ? { mimeType: kind } : undefined) }
    catch (e) { stream.getTracks().forEach(t => t.stop()); return note("This browser can't record (" + ((e as Error).name || e) + ').') }
    const chunks: Blob[] = []
    r.ondataavailable = e => { if (e.data && e.data.size) chunks.push(e.data) }
    r.onstop = () => {
      clearTimeout(recTimer); stream.getTracks().forEach(t => t.stop())
      rec = null; drawMic()
      const type = r.mimeType || kind || 'audio/webm'
      const ext = /mp4/.test(type) ? 'm4a' : /ogg/.test(type) ? 'ogg' : 'webm'
      hearClip(new Blob(chunks, { type }), `voice-${new Date().toISOString().replace(/[:.]/g, '-')}.${ext}`)
    }
    rec = r; r.start(1000); drawMic()
    recTimer = setTimeout(() => { if (rec === r) r.stop() }, RECORD_MS)
  }
  mic.onclick = () => {
    if (rec) return rec.stop()
    if (canRecord()) record()
    else $('pick-voice').click()
  }
  $('pick-voice').onchange = e => { const f = (e.target as HTMLInputElement).files![0]; (e.target as HTMLInputElement).value = ''; if (f) hearClip(f, f.name || 'voice') }
  addEventListener('storage', e => { if (e.key === DICTATE) { clips = (load(DICTATE, []) || []).filter((d: WbFile) => d && d.id); drawMic() } })

  // ---- sending: the outbox ----
  let flushing = false, flushTimer = 0, flushTries = 0
  async function flush(now?: boolean) {
    if (now) { clearTimeout(flushTimer); flushTries = 0 }
    pump()  // the draft's uploads, on the same occasions
    hearAll(now)  // and the clips waiting for their words (#1203)
    outbox = outItems()  // another tab's too: whichever tab is online sends them all
    if (flushing || !token || !(outbox.length || held)) return
    flushing = true
    let again = false, wait = 0
    // in a finally: a throw from keep, render or apply left `flushing` set, and the page sent
    // nothing more until a reload (#935)
    try { for (const o of [...outbox, ...(held ? [held] : [])]) {
      if (o !== held && (o.error || !load(OUT + o.cid, null))) continue  // refused, or sent by another tab meanwhile
      let file: WbFile | null = null
      try {  // its files first, each once (#623): a refusal says which file and why
        for (const f of o.files || []) { file = f; await upload(f) }
        file = null
      } catch (e) {
        if ((e as Error).message === 'key') break
        if (!(e instanceof Refused)) { again = true; continue }
        const why = file!.name + ': ' + e.message
        if (o === held) { held = null; banner('Not sent: ' + why + '. Your message is still in the box.', true); continue }
        o.error = why; keep(o); render(); continue
      }
      try {
        const r = await post('/api/send', { text: o.text, cid: o.cid, ...(o.about ? { about: aboutBody(o.about) } : {}),
          ...(o.files ? { files: o.files.map(f => f.id) } : {}) })
        const j = await r.json().catch(() => ({}))
        // landed: the reply names it. The snapshot alone holds the newest 200, so a retry after
        // a lost reply, 205 messages later, kept its pending bubble for good (#264, the audit of 1.2)
        if (r.ok) {
          // a 200 that doesn't name it isn't the chat server's (a captive portal, a proxy's
          // page, an empty body): for a held message it cleared the box, its only copy, though
          // nothing had reached the server (#934). Tried again; the server ignores a cid it has
          if (!(j.message && j.message.cid === o.cid)) again = true
          else if (o === held) sentHeld()
          else { forgetFiles(o); drop(o.cid) }
          if (j.state) apply(j.state, { reply: true }); else render()
          continue
        }
        if (r.status === 401) { keyless('That key was not accepted.'); break }
        if (transient(r.status)) { again = true; wait = Math.max(wait, retryAfter(r)); continue }
        if (r.status >= 400 && r.status < 500) {
          if (o === held) { held = null; banner('Not sent: ' + (j.error || 'refused') + '. Your message is still in the box.', true); continue }
          o.error = j.error || 'refused'; keep(o); render(); continue
        }
        again = true
      } catch { again = true }
    } } finally {
      flushing = false
      if (again && (held || outbox.some(o => !o.error))) {
        clearTimeout(flushTimer)
        flushTimer = setTimeout(flush, Math.max(wait, BACKOFF[Math.min(flushTries++, BACKOFF.length - 1)]))
      }
    }
  }
  const newId = () => (crypto.randomUUID ? crypto.randomUUID() : Date.now().toString(36) + Math.random().toString(36).slice(2))
  function send() {
    const text = input.value.trim()
    const files = sendable().map(({ id, name, size, type }) => ({ id, name, size, type }))
    if ((!text && !files.length) || !token || held) return
    const o = { cid: newId(), text, ts: now(), ...(about ? { about } : {}), ...(files.length ? { files } : {}) }
    if (!save(OUT + o.cid, o)) {  // not stored, so not cleared: sent from here, kept till it lands
      held = o; sendBtn.disabled = true
      banner('Sending…')
      flush(true)
      return
    }
    outbox = outItems()
    setAbout(null)
    clearTray(files)
    input.value = ''; save(K.draft, ''); grow(); sendBtn.disabled = true
    render(); toEnd(true); flush(true)
  }
  // a take kept from the shortlist: sent as its own message, the box left as it was
  function pick(a: About) {
    if (!token || held) return
    const o = { cid: newId(), text: `Keep take ${a.n}: ${a.name}`, ts: now(), about: a }
    if (!save(OUT + o.cid, o)) { held = o; banner('Sending…'); flush(true); return }
    outbox = outItems()
    render(); toEnd(true); flush(true)
  }
  // an expired message, sent again as a new one through the outbox (its own cid), with its files:
  // already on the computer, so not a byte again (#623); and what it was about, a pair of
  // screenshots or a kept take: without it Claude got the words with no task or take (#843)
  function resend(m: Msg, cid: string = newId()) {
    if (!token) return
    const files = (m.files || []).map(({ id, name, size, type }) => ({ id, name, size, type }))
    const o = { cid, text: m.text, ts: now(), ...(m.about ? { about: m.about } : {}), ...(files.length ? { files } : {}) }
    if (!save(OUT + o.cid, o)) { banner('Not sent: this browser keeps no outbox here. Copy it into the box.', true); return }
    outbox = outItems()
    render(); toEnd(true); flush(true)
  }
  function sentHeld() {
    if (input.value.trim() === held!.text) { input.value = ''; save(K.draft, ''); grow(); sendBtn.disabled = true }
    if (!(held!.about && held!.about.audio)) { setAbout(null); clearTray(held!.files || []) }  // a kept take left the box as it was
    held = null; banner('')
  }
  function clearTray(sent: WbFile[]) {  // sent: their bytes stay until the message lands; refused ones go now
    const ids = new Set(sent.map(f => f.id))
    for (const d of draft) if (!ids.has(d.id) && d.state === 'refused') db.del(d.id)
    draft = draft.filter(d => !ids.has(d.id) && d.state !== 'refused')
    keepDraft(); drawTray(); armSend()
  }

  // ---- composer ----
  const touch = matchMedia('(pointer: coarse)').matches
  function grow() { input.style.height = ''; input.style.height = Math.min(input.scrollHeight, innerHeight * 0.38) + 'px' }
  input.value = load(K.draft, '')
  input.addEventListener('input', () => { grow(); armSend(); save(K.draft, input.value) })
  input.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey && !touch && !e.isComposing) { e.preventDefault(); send() } })
  $('composer').addEventListener('submit', e => { e.preventDefault(); send() })
  armSend(); grow(); drawTray(); drawMic()

  // ---- no key: say how to get one, or take it pasted ----
  function keyless(why: string, forget?: boolean) {
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
    drawHint()
    drawPresence()
  }

  // ---- settings: what the gear's sheet held, now cards in the Settings view; What it saved in Stats (#1284) ----
  const sheet = $('settings')
  // `wb savings` for the week, from the state (chat.week_saved)
  const tokens = (n: number) => Math.abs(n) >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : `${Math.round(n / 1e3)}K`
  function drawSaved() {
    const t = state.savings, s = t && t.score
    $('badge').hidden = !s
    // the badge's number and one line under it: the sentence and the paragraph said the same
    // saving twice more (#1322)
    if (s) {
      const n = $('badge-n'); n.className = 'badge-n ' + s.color
      n.textContent = String(s.score); n.append(Object.assign(document.createElement('small'), { textContent: ' / 100' }))
      $('badge-s').textContent = t!.saved >= 0 ? `~${tokens(t!.saved)} Claude tokens saved this week` : `~${tokens(-t!.saved)} more Claude tokens this week`
    }
    const api = apiSaved(t && t.api)
    if (!t || !t.tasks) {
      $('saved').textContent = api || 'Nothing to compare yet. Once a delegated task is accepted, this shows what it saved Claude this week.'
      return
    }
    const gpu = t.wh ? `The GPU used ${t.wh >= 1000 ? `${(t.wh / 1000).toFixed(1)} kWh` : `${Math.round(t.wh)} Wh`}${t.cost != null ? `, cost ${t.cost.toFixed(2)}` : ''}.` : ''  // no currency sign: the price is the user's, in theirs (#845, as #609)
    // no badge (nothing written to compare against, only discards): the saving here instead
    const saved = s ? '' : t.saved >= 0 ? `~${tokens(t.saved)} Claude tokens saved this week.` : `~${tokens(-t.saved)} more Claude tokens this week.`
    $('saved').textContent = [saved, gpu, api].filter(Boolean).join(' ')
  }
  // The week's API tasks, apart from the saving (#1022): dollars, OpenCode's unit, so a `$`;
  // a task with no price is counted, never shown as $0
  function apiSaved(a?: ApiWeek | null) {
    if (!a || !a.tasks) return ''
    // meter.usd's rule: $0.42, `<$0.01` under a cent, $0 for none (#1322)
    const usd = (x: number) => x === 0 ? '$0' : Math.abs(x) < 0.01 ? '<$0.01' : `$${x.toFixed(2)}`
    // ≈: at the catalog's prices, an estimate, unless the user set the model's (wb model price, #1135);
    // the `≈` alone says it, no "(catalog prices)" after it (#1322)
    const est = (v: ApiCost) => ((v.prices || 'catalog') === 'catalog' && v.cost ? '≈' : '') + usd(v.cost)
    const provs = Object.entries(a.by_provider || {}).filter(([, v]) => v.tasks > v.unknown + (v.plan || 0))
      .sort((x, y) => y[1].cost - x[1].cost).map(([p, v]) => `${(a.names || ({} as Record<string, string>))[p] || p} ${est(v)}`)
    const known = a.tasks - a.unknown - (a.plan || 0)
    const inside = provs.join(', ')
    const parts = [known ? `${est(a)}${inside ? ` (${inside})` : ''}` : '',
      a.unknown ? `${a.unknown} with no known cost` : '', a.plan ? `${a.plan} on plan usage` : ''].filter(Boolean)
    return `API models: ${a.tasks} task${a.tasks === 1 ? '' : 's'}, ${parts.join(', ')}.`
  }
  function drawSettings() {
    drawApp(); drawNotify()
  }
  $('forget').onclick = async () => {
    if (pushOn) await setPush(false)  // a device without the key can't read what a push is about
    go('chat'); save(K.token, ''); save(K.thread, []); keyless('', true); keepKey(); keyManifest()
  }

  // ---- the app: installing it, and notifications (sw.js; webpush.py on the computer) ----
  const standalone = () => matchMedia('(display-mode: standalone)').matches || navigator.standalone === true
  const ios = /iphone|ipad|ipod/i.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1)
  const iosSafari = ios && !/CriOS|FxiOS|EdgiOS|OPiOS/.test(navigator.userAgent)
  const canPush = window.isSecureContext && 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window
  let installEvt: BeforeInstallPromptEvent | null = null, swReg: ServiceWorkerRegistration | null = null, pushOn = false, pushEndpoint = '', pushBusy = false, pushNote = '', pushSynced = false
  addEventListener('beforeinstallprompt', e => { e.preventDefault(); installEvt = e; drawShown() })
  addEventListener('appinstalled', () => { installEvt = null; drawShown() })
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
  const b64u = (buf: ArrayBuffer) => btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
  const unb64u = (s: string) => Uint8Array.from(atob(s.replace(/-/g, '+').replace(/_/g, '/')), c => c.charCodeAt(0))
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
      pushEndpoint = pushOn ? sub!.endpoint : ''
      if (sub) await post('/api/push', { endpoint: sub.endpoint, on: true })
    } catch {}
    drawShown()
  }
  async function setPush(on: boolean) {
    pushBusy = true; pushNote = ''; drawSettings()
    try {
      if (on) {
        if (Notification.permission !== 'granted' && (await Notification.requestPermission()) !== 'granted') throw new Error('Notifications were not allowed.')
        let sub = await swReg!.pushManager.getSubscription()
        if (!sub) sub = await swReg!.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: unb64u(state.push.key!) })
        const r = await post('/api/push', { endpoint: sub.endpoint, on: true })
        if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || 'The computer refused it.')
        pushOn = true; pushEndpoint = sub.endpoint
      } else {
        const sub = await swReg!.pushManager.getSubscription()
        if (sub) { await post('/api/push', { endpoint: sub.endpoint, on: false }).catch(() => {}); await sub.unsubscribe() }
        pushOn = false; pushEndpoint = ''
      }
    } catch (e) { pushNote = String((e && (e as Error).message) || e) }
    pushBusy = false; drawSettings()
  }
  const para = (box: HTMLElement, text: string, cls?: string) => { const p = document.createElement('p'); if (cls) p.className = cls; p.textContent = text; box.append(p); return p }
  // a line whose `…` is a command to type, drawn as code: the install card showed its backticks as
  // text (the web app review's screenshots, 2026-11-25, #1323). Text nodes alone, as everywhere here
  const paraCode = (box: HTMLElement, text: string, cls?: string) => {
    const p = para(box, '', cls)
    text.split(/`([^`]+)`/).forEach((part, i) => p.append(i % 2 ? Object.assign(document.createElement('code'), { textContent: part }) : part))
    return p
  }
  const HTTPS = "This page is open over plain http, so it can't be installed or send notifications. Over https it can: " +
    'on the computer, `tailscale serve --bg 1337` gives the chat an https address on your tailnet.'
  function drawApp() {
    const box = $('app'); box.replaceChildren()
    $('app-h').textContent = standalone() ? 'The app' : 'Install the app'
    if (standalone()) return para(box, "Installed ✓ — you're using the chat as an app.")
    if (!window.isSecureContext) return paraCode(box, HTTPS, 'small')
    if (!https) return paraCode(box, 'The app installs from the https address, so the one app works on every device: ' +
      'on the computer, `tailscale serve --bg 1337` gives the chat one on your tailnet.', 'small')
    if (installEvt) {
      const b = document.createElement('button'); b.type = 'button'; b.className = 'plain'; b.textContent = 'Install this app'
      b.onclick = async () => { installEvt!.prompt(); try { await installEvt!.userChoice } catch {} installEvt = null; drawSettings() }
      box.append(b); return para(box, 'Adds Workbench to your home screen, with its own window.', 'small')
    }
    if (iosSafari) return para(box, "Tap the Share button ⎋ in Safari's toolbar, then Add to Home Screen. Open Workbench from its new icon: on iPhone and iPad, that's what lets it send notifications.")
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
    const t = document.createElement('span'); t.textContent = `When ${driverName()} answers or needs you`
    const sw = document.createElement('input'); sw.type = 'checkbox'; sw.setAttribute('role', 'switch'); sw.id = 'push'
    sw.checked = pushOn; sw.disabled = pushBusy || !swReg || !(state.push && state.push.key)
    sw.onchange = () => setPush(sw.checked)
    row.append(t, sw); box.append(row)
    para(box, pushBusy ? 'Working…' : pushNote || (pushOn ? `On: this device hears from ${driverName()} even with the page closed, unless this page showed it on screen, or you were using the chat on another device then.`
      : `Off. Nothing you and ${driverName()} write passes through the push service: the phone fetches it from your computer.`), 'small')
    if (pushOn && !pushBusy) {
      const b = document.createElement('button'); b.type = 'button'; b.className = 'plain'; b.textContent = 'Send a test'
      b.onclick = async () => {
        b.disabled = true
        try {
          const j = await (await post('/api/push', { test: true })).json()
          // why it failed, from the computer's side: it all read as the service's refusal (#539)
          const why = (j.reasons || []).length ? ` (${j.reasons[0]})` : ''
          pushNote = j.ok ? 'Sent: it should arrive in a few seconds.'
            : j.hint === 'cert' ? "The computer can't check the push service's certificate" + why + ': its Python has no CA certificates. On a Mac, run Install Certificates.command from your Python\'s folder in Applications; elsewhere install ca-certificates, or set SSL_CERT_FILE. Then restart the chat server.'
            : j.hint === 'clock' ? 'The push service refused it' + why + ": check the computer's clock is right, then turn notifications off and on again."
            : j.hint === 'unreached' ? "The computer couldn't reach the push service" + why + ': check its network, or a proxy in the way.'
            : "The push service didn't take it" + why + ': turn notifications off and on again.'
        } catch { pushNote = 'Not sent: try again.' }
        drawSettings()
      }
      box.append(b)
    }
  }
  // What the page showed while on screen, so the computer pushes only what nobody saw. Merely
  // visible counted for every device: a desktop tab left open behind the terminal silenced the
  // phone's pushes for good (#537). Now a page in use (focused, touched or typed in within
  // USING_MS) speaks for every device; one only on screen, for its own subscription alone.
  const USING_MS = 120_000
  let lastInput = 0
  const reported = new Set()  // `<all|own>:<id>`
  function reportSeen() {
    if (document.hidden || !token) return
    const using = document.hasFocus() && Date.now() - lastInput < USING_MS
    if (!using && !pushEndpoint) return
    const how = using ? 'all' : 'own'
    const ids = state.messages.filter(m => m.from === 'claude' || m.from === 'notice').slice(-20).map(m => m.id)
      .filter(i => !reported.has('all:' + i) && !reported.has(how + ':' + i))
    if (!ids.length) return
    ids.forEach(i => reported.add(how + ':' + i))
    post('/api/seen', { ids, ...(using ? {} : { endpoint: pushEndpoint }) }).catch(() => ids.forEach(i => reported.delete(how + ':' + i)))
  }
  document.addEventListener('visibilitychange', reportSeen)
  addEventListener('focus', reportSeen)
  for (const ev of ['pointerdown', 'keydown', 'wheel', 'touchstart'])
    addEventListener(ev, () => { lastInput = Date.now(); reportSeen() }, { capture: true, passive: true })

  // Using this page counts as using this device (#959): "away" was only a message sent here, so
  // the owner, who typed in the terminal and then picked up the phone with this page open, had a
  // permission prompt stay in the terminal, neither shown here nor allowed by their setting. The
  // page coming on screen (shown, focused, back from the cache) or a tap or key in it tells the
  // computer, at most once per ACTIVE_MS while it stays in use, again at once when it comes
  // back. Only a page on screen and focused: one merely open in a background tab, or visible
  // beside the terminal, says nothing. A post that failed is not tried again on a timer: a page
  // left in front on a phone put down retried until the line came back, after the user had typed
  // in the terminal, and made them away with no use at all (the #959 review). The next real use
  // reports at once instead.
  const ACTIVE_MS = 30_000
  let activeAt = 0
  // Only on the Chat view, and not a tap on the nav leaving it: Stats and Settings are the desk's
  // admin too, and a click there made the user at the terminal away, their next plan or question
  // refused into the chat (#1374, 2026-11-25). The server also leaves a localhost page out
  function reportActive(e?: Event) {
    if (curView !== 'chat' || (e && e.target && (e.target as Element).closest && (e.target as Element).closest('#tabbar'))) return
    if (document.hidden || !document.hasFocus() || !token) return
    if (Date.now() - activeAt < ACTIVE_MS) return
    activeAt = Date.now()
    post('/api/active', {}).catch(() => { activeAt = 0 })
  }
  document.addEventListener('visibilitychange', () => { if (document.hidden) activeAt = 0; else reportActive() })
  addEventListener('focus', reportActive)
  addEventListener('pageshow', reportActive)
  for (const ev of ['pointerdown', 'keydown'])
    addEventListener(ev, reportActive, { capture: true, passive: true })

  // ---- the shell (#1284): Chat, Stats and Settings, one shown, picked by the address's hash ----
  // The owner (2026-11-24): the bottom bar of merecatholicity.com on a phone; a left rail from
  // 601px. The hash is the route, so Back walks the views; anything but #stats or #settings is
  // the chat, and the key's own fragment was taken and dropped above, before any of this.
  const VIEWS = ['chat', 'stats', 'settings']
  const ROOTS: Record<string, string> = { stats: 'stats-body', settings: 'settings-rows' }
  const tabs = [...document.querySelectorAll<HTMLElement>('#tabbar .tab')]
  const chatTab = tabs.find(b => b.dataset.view === 'chat')!, tabBadge = chatTab.querySelector<HTMLElement>('.tab-badge')!
  const mods = new Map<string | null, { mod: WbView; shown: boolean }>()                         // name → {mod, shown}: stats.js, settings.js (WB.view)
  const handlers: Record<string, Set<(arg: any) => void>> = { state: new Set(), view: new Set() }
  const emit = (ev: string, arg: unknown) => { for (const fn of handlers[ev] || []) try { fn(arg) } catch (e) { console.error(e) } }
  const call = (v: { mod: WbView }, fn: 'mount' | 'show' | 'hide', ...a: unknown[]) => { try { (v.mod as Record<string, any>)[fn] && (v.mod as Record<string, any>)[fn](...a) } catch (e) { console.error(e) } }
  const modShow = (name: string | null) => { const v = mods.get(name); if (v && !v.shown && !document.hidden) { v.shown = true; call(v, 'show') } }
  const modHide = (name: string | null) => { const v = mods.get(name); if (v && v.shown) { v.shown = false; call(v, 'hide') } }
  // The view shown read again once the line is back (the network, or the stream after a failure):
  // Settings or Stats opened before it was, as a phone waking does, said it couldn't load until a
  // tab switch (#1412, 2026-11-25)
  function reshowView() { const v = mods.get(curView); if (v && v.shown) call(v, 'show') }
  function drawShown() {  // what the old sheet drew while open, for the view that shows it
    if (curView === 'settings') drawSettings()
    else if (curView === 'stats') drawSaved()
  }
  // The Chat tab's badge: Claude's messages (and waiting lines) newer than the moment the
  // thread was left, in the server's clock; held until a live state gives that clock when the
  // page opened on another view
  let leftAt: number | null = null
  function drawBadge() {
    if (curView !== 'chat' && leftAt === null && live) leftAt = stamp
    let n = 0
    if (curView !== 'chat' && leftAt !== null) {
      for (const m of state.messages) if ((m.from === 'claude' || m.from === 'notice') && m.role !== 'agent' && forUser(m) && m.ts > leftAt) n++
    }
    tabBadge.hidden = !n
    tabBadge.textContent = n > 99 ? '99+' : String(n)
    chatTab.setAttribute('aria-label', n ? `Chat, ${n > 99 ? '99+' : n} new` : 'Chat')
  }
  let chatTop = 0
  function setView(name: string) {
    if (!VIEWS.includes(name)) name = 'chat'
    if (name === curView) return
    const was = curView
    if (was === 'chat') { chatTop = scroller.scrollTop; leftAt = live ? now() : null }
    if (was) modHide(was)
    if (name !== 'chat') showTaskSheet(false)  // the task sheet is the chat's (#1201)
    curView = name
    document.body.dataset.view = name
    for (const b of tabs) b.dataset.view === name ? b.setAttribute('aria-current', 'true') : b.removeAttribute('aria-current')
    if (name === 'chat') {
      leftAt = null
      // shown again: at the newest line if it was following, else where the reader left it
      if (follow) toEnd(true)
      else { scroller.scrollTop = chatTop; lastTop = scroller.scrollTop; lastHeight = scroller.scrollHeight; jump.hidden = nearEnd() }
      reportActive()                             // the chat in use, as a tap in it is (#1374)
    }
    drawBadge()
    drawShown()
    modShow(name)
    emit('view', name)
  }
  const go = (name: string) => {  // through the hash, so Back comes here again
    name = VIEWS.includes(name) ? name : 'chat'
    if (location.hash !== '#' + name) location.hash = name
    else setView(name)
  }
  for (const b of tabs) b.addEventListener('click', () => go(b.dataset.view as string))
  addEventListener('hashchange', () => setView(location.hash.slice(1)))
  document.addEventListener('visibilitychange', () => { if (document.hidden) modHide(curView); else modShow(curView) })
  // A short word from a view (WB.toast). Hidden by its look (`.toast` without `.on`: faded, moved
  // down), never display:none: some screen readers miss a live region that starts out of the tree.
  // Up for as long as its words need, 60 ms a character and 3.5 s at least (a 236-character refusal
  // went in 3.5 s, #1563), and held while hovered or focused (the web app's review, 2026-11-26)
  const TOAST_MS = 3500, TOAST_CHAR_MS = 60
  let toastTimer = 0, toastHeld = false, toastFor = 0
  const toastEl = $('toast')
  const toastGo = () => { clearTimeout(toastTimer); if (!toastHeld) toastTimer = setTimeout(() => toastEl.classList.remove('on'), toastFor) }
  function toast(text: unknown) {
    toastEl.textContent = String(text); toastEl.classList.add('on')
    toastFor = Math.max(TOAST_MS, TOAST_CHAR_MS * toastEl.textContent.length)
    toastGo()
  }
  for (const [on, held] of [['mouseenter', true], ['mouseleave', false], ['focusin', true], ['focusout', false]] as const)
    toastEl.addEventListener(on, () => { toastHeld = held; if (held) clearTimeout(toastTimer); else if (toastEl.classList.contains('on')) toastGo() })
  // The view-module API: stats.js and settings.js build against exactly this (#1284)
  window.WB = Object.freeze({
    token: () => token,
    api: (path: string, init: WbInit = {}) => {
      const headers = new Headers(init.headers || {})
      if (token) headers.set('Authorization', 'Bearer ' + token)
      let body: any = init.body  // a plain object goes as JSON, anything else as fetch takes it
      const plain = body != null && typeof body === 'object' && !(body instanceof Blob) && !(body instanceof FormData) &&
        !(body instanceof URLSearchParams) && !(body instanceof ArrayBuffer) && !ArrayBuffer.isView(body) && !(body instanceof ReadableStream)
      if (plain) body = JSON.stringify(body)
      if ((plain || typeof body === 'string') && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
      return fetch(path, { ...init, headers, body, cache: 'no-store' })
    },
    // a JSON POST under post()'s deadline: Settings' through WB.api never gave up, so one that
    // hung showed as applied and held every later change (#1391, 2026-11-25)
    post: (path: string, body: unknown) => post(path, body),
    view: (name: string, mod: WbView) => {  // mounted once, at once; shown and hidden with its view and the page
      if (!ROOTS[name] || mods.has(name) || !mod) return
      const v = { mod, shown: false }
      mods.set(name, v)
      call(v, 'mount', $(ROOTS[name]))
      if (curView === name) modShow(name)
    },
    toast,
    // a request for the driver, from a Settings row with a problem (its Ask link): the chat's box
    // filled and focused, sent by the user; it waits like any message, so no listener is needed.
    // The words carry what it is about ("Help me with Settings › API: …"): an `about` names a
    // task or a take, which the server checks, and Settings is neither
    ask: (text: string) => {
      go('chat')
      input.value = String(text); save(K.draft, input.value); grow(); armSend(); input.focus()
    },
    on: (event: string, fn: (arg: any) => void) => { if (handlers[event] && typeof fn === 'function') handlers[event].add(fn) },
    current: () => curView || 'chat',
  })
  setView(location.hash.slice(1))

  async function firstState() {  // at once, without waiting for a stream slot to free up
    try {
      const r = await fetch('/api/state', { headers: { Authorization: 'Bearer ' + token }, cache: 'no-store' })
      if (r.ok && !live) apply(await r.json(), { reply: true })  // the stream's, if it came first, is newer
    } catch {}
  }

  if (!token) keyless('')
  else { render(); firstState(); connect(); flush(true); keepKey(); reportActive() }
})()
