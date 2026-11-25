// The Settings view (#1286): the admin rows the server sends (GET /api/settings, settings.py's
// table, #1279/#1280), drawn by kind into one card per group, above the device cards. A generic
// renderer: a new setting touches only Python. The owner's click is the yes (2026-11-24, "We
// trust Claude"): the choice lights at once and the POST goes, with no confirm step and no
// warning, since the server runs wb ungated, as at the terminal; the answer's rows are drawn
// again, so a refusal puts the control back. One request at a time, clicks made meanwhile wait.
(() => {
  'use strict'
  const GROUPS = [['shared', 'Agents and roles'], ['gpu', 'GPU'], ['api', 'API'], ['terminal', 'Terminal'],
    ['experimental', 'Experimental']]
  const SEG_MAX = 4                     // a segmented control up to this many choices, a <select> past it
  const OTHER = '\u0000other'           // the select's "Other model…" (never a model id: a NUL)
  const UNITS = { usd: ['$', '/day'] }
  const HELP_SAID = 'Claude will offer to set it up.'  // the pane's toast, when a choice needs Claude's help

  let root = null
  let rows = null                       // the last rows the server sent
  let why = ''                          // the last read's failure, shown while there are no rows
  let reads = 0                         // each GET's number: one that left before a write ends is dropped
  let wrote = 0                         // the GET number current when the last write finished
  const queue = []                      // [{key, value}] waiting; queue[0] is in flight while busy
  let busy = false
  let drawing = false                   // a redraw takes the focused input away: its blur sends nothing

  const el = (tag, cls, text) => {
    const e = document.createElement(tag)
    if (cls) e.className = cls
    if (text != null) e.textContent = String(text)
    return e
  }
  // what the page shows for a key: its newest queued value over the server's (the choice lit at once)
  const pending = key => { for (let i = queue.length - 1; i >= 0; i--) if (queue[i].key === key) return queue[i]; return null }
  const shown = row => { const p = pending(row.key); return p ? p.value : row.value }

  async function load() {
    const n = ++reads
    try {
      const r = await WB.api('/api/settings')
      const body = await r.json().catch(() => ({}))
      if (n < wrote) return              // a write's rows came after it left: they are newer
      if (r.ok && Array.isArray(body.rows)) { rows = body.rows; why = '' }
      else why = body.error || `the server said ${r.status}`
    } catch { if (n >= wrote) why = "the computer can't be reached" }
    draw()
  }

  function send(key, value) {
    const waiting = queue.findIndex((q, i) => q.key === key && (i > 0 || !busy))
    if (waiting >= 0) queue[waiting].value = value  // the same row clicked again while it waits: its newest
    else queue.push({ key, value })
    draw()
    pump()
  }
  async function pump() {
    if (busy || !queue.length) return
    busy = true
    const { key, value } = queue[0]
    let said = '', lost = false
    try {
      // given up after the page's POST_MS, into the lost path below: with no deadline a POST that
      // hung kept its value lit as applied and every later click queued behind it (#1391)
      const r = await WB.post('/api/settings', { key, value })
      const body = await r.json().catch(() => ({}))
      if (Array.isArray(body.rows)) rows = body.rows
      said = r.ok && body.ok ? body.said || 'Done.' : body.error || body.said || `the server said ${r.status}`
      if (r.ok && body.ok && body.help) said = `${/[.!?]$/.test(said) ? said : said + '.'} ${HELP_SAID}`
    } catch { said = "The computer didn't answer: Settings shows what it says now."; lost = true }
    wrote = reads + 1
    queue.shift()
    busy = false
    WB.toast(said)
    draw()                              // from the answer's rows: a refusal reverts the control
    if (lost && !queue.length) load()   // the change may have landed all the same: ask
    pump()
  }

  // ---- the kinds ----
  function choice(row, box) {
    const value = shown(row), choices = Array.isArray(row.choices) ? row.choices : []
    const lit = choices.find(c => c.value === value)
    if (choices.length && choices.length <= SEG_MAX && !row.other) {
      const seg = el('div', 'seg set-seg')
      seg.setAttribute('role', 'radiogroup')
      seg.setAttribute('aria-label', row.label)
      seg.style.setProperty('--n', choices.length)
      for (const c of choices) {
        const b = el('button', '', c.label)
        b.type = 'button'
        b.setAttribute('role', 'radio')
        b.dataset.value = c.value
        b.setAttribute('aria-checked', String(c.value === value))
        // sent even when lit: what's shown may be stale (a change made at the terminal), and a
        // click to put it back sent nothing while the setting stayed changed (#1392, 2026-11-25)
        b.addEventListener('click', () => send(row.key, c.value))
        seg.append(b)
      }
      box.append(seg)
    } else if (choices.length || row.other) {
      const sel = el('select', 'set-select')
      sel.setAttribute('aria-label', row.label)
      if (!lit) {                        // none chosen (an API role with no model), or one off the list
        const o = el('option', '', value == null || value === '' ? 'None' : value)
        o.value = value == null ? '' : String(value)
        o.disabled = value == null || value === ''
        sel.append(o)
      }
      for (const c of choices) { const o = el('option', '', c.label); o.value = c.value; sel.append(o) }
      if (row.other) { const o = el('option', '', 'Other model…'); o.value = OTHER; sel.append(o) }
      sel.value = value == null ? '' : String(value)
      sel.addEventListener('change', () => {
        if (sel.value !== OTHER) { send(row.key, sel.value); return }
        const input = el('input', 'set-text')
        input.type = 'text'
        input.dataset.key = row.key
        input.dataset.other = '1'
        input.placeholder = 'provider/model-id, then Enter'
        input.setAttribute('aria-label', `${row.label}: another model's id`)
        input.autocapitalize = 'off'; input.autocomplete = 'off'; input.spellcheck = false
        input.addEventListener('keydown', e => {
          if (e.key === 'Enter') { e.preventDefault(); const v = input.value.trim(); if (v) send(row.key, v) }
          // blurred first: draw() keeps a focused input's text and would open it again
          else if (e.key === 'Escape') { e.preventDefault(); input.blur(); draw() }
        })
        sel.after(input)
        input.focus()
      })
      box.append(sel)
    }
    const says = lit && pending(row.key) ? lit.says : row.says
    if (says) box.append(el('p', 'small set-says', says))
  }

  function number(row, box) {
    const value = shown(row), off = value == null || value === '' || value === 'off'
    const [pre, post] = UNITS[row.unit] || ['', row.unit || '']
    const line = el('div', 'set-num')
    if (pre) line.append(el('span', 'set-unit', pre))
    const input = el('input', 'set-text')
    input.type = 'text'
    input.inputMode = 'decimal'
    input.dataset.key = row.key
    input.setAttribute('aria-label', row.label)
    input.placeholder = 'off'
    const was = off ? '' : String(value)
    input.value = was
    const go = asked => {               // empty sends nothing: off is the Off button's
      const v = input.value.trim()      // Enter sends the value shown too, as a click does (#1392)
      if (!drawing && v && (asked || v !== was)) send(row.key, v)
    }
    input.addEventListener('keydown', e => {
      if (e.key === 'Enter') { e.preventDefault(); go(true) }
      else if (e.key === 'Escape') { input.value = was; input.blur() }
    })
    input.addEventListener('blur', () => go(false))
    line.append(input)
    if (post) line.append(el('span', 'set-unit', post))
    const b = el('button', 'plain set-off', 'Off')
    b.type = 'button'
    b.setAttribute('aria-pressed', String(off))
    b.addEventListener('mousedown', e => e.preventDefault())  // the input's blur doesn't send first
    b.addEventListener('click', () => send(row.key, null))
    line.append(b)
    box.append(line)
    if (row.says) box.append(el('p', 'small set-says', row.says))
  }

  function action(row, box) {
    if (row.text) box.append(el('p', 'small set-says', row.text))
    const line = el('div', 'set-actions')
    for (const c of Array.isArray(row.choices) ? row.choices : []) {
      const b = el('button', 'plain', c.label)
      b.type = 'button'
      b.dataset.value = c.value
      if (c.says) b.title = c.says
      b.disabled = !!pending(row.key)
      b.addEventListener('click', () => send(row.key, c.value))
      line.append(b)
    }
    box.append(line)
  }

  function drawRow(row) {
    const box = el('div', 'set-row')
    box.dataset.key = row.key
    box.dataset.kind = row.kind
    if (row.kind === 'info') { box.append(el('p', 'small set-info', row.text || row.label)); return box }
    // a line in its place, the group's other rows drawn too: with the routes unreadable the shared
    // group's note sits beside Claude coding and the rest, still the owner's to change (#1286's review)
    if (row.kind === 'note') { box.append(el('p', 'small set-note', row.text || row.label)); return box }
    if (row.label) box.append(el('div', 'set-label', row.label))
    if (row.kind === 'choice') choice(row, box)
    else if (row.kind === 'number') number(row, box)
    else if (row.kind === 'action') action(row, box)
    else if (row.text) box.append(el('p', 'small', row.text))
    return box
  }

  function draw() {
    if (!root) return
    // an id being typed elsewhere survives the redraw: only the row it was sent for is drawn anew
    const a = document.activeElement
    const typing = a && root.contains(a) && a.tagName === 'INPUT' && !pending(a.dataset.key)
      ? { key: a.dataset.key, other: a.dataset.other, value: a.value, at: a.selectionStart } : null
    const out = el('div', 'cards set-cards')
    if (!rows) {
      if (why) { const c = el('div', 'card set-card'); c.append(el('p', 'small', `Settings couldn't load: ${why}`)); out.append(c) }
    } else {
      const known = GROUPS.map(g => g[0])
      const groups = [...GROUPS, ...[...new Set(rows.map(r => r.group))].filter(g => !known.includes(g)).map(g => [g, g])]
      for (const [g, title] of groups) {
        const mine = rows.filter(r => r && r.group === g)
        if (!mine.length) continue
        const card = el('section', 'card set-card')
        card.dataset.group = g
        card.append(el('h3', '', title))
        for (const r of mine) card.append(drawRow(r))
        out.append(card)
      }
    }
    drawing = true
    try { root.replaceChildren(out) } finally { drawing = false }
    if (typing && typing.key) {
      let input = root.querySelector(`.set-row[data-key="${CSS.escape(typing.key)}"] input${typing.other ? '[data-other]' : ''}`)
      if (!input && typing.other) {
        const sel = root.querySelector(`.set-row[data-key="${CSS.escape(typing.key)}"] select`)
        if (sel) { sel.value = OTHER; sel.dispatchEvent(new Event('change')); input = sel.nextElementSibling }
      }
      if (input) { input.value = typing.value; input.focus(); try { input.setSelectionRange(typing.at, typing.at) } catch {} }
    }
  }

  // read again when the page takes the focus: beside the terminal it stays shown, and a change
  // made there (by the user or Claude) was never drawn (#1392, 2026-11-25)
  let showing = false
  addEventListener('focus', () => { if (showing) load() })
  WB.view('settings', {
    mount(r) { root = r },
    show() { showing = true; load() },
    hide() { showing = false },
  })
})()
