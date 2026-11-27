// The Settings view (#1286): the admin rows the server sends (GET /api/settings, settings.py's
// table, #1279/#1280), drawn by kind into one card per group, above the device cards. A generic
// renderer: a new setting touches only Python. The owner's click is the yes (2026-11-24, "We
// trust Claude"): the choice lights at once and the POST goes, with no confirm step and no
// warning, since the server runs wb ungated, as at the terminal; the answer's rows are drawn
// again, so a refusal puts the control back. One request at a time, clicks made meanwhile wait.
// Who changed what (#1355): a row changed within a week says by whom and when, "Recent changes"
// lists the newest, and a change made anywhere (Claude's `wb settings set`, another device) reaches
// an open page through the chat's state (`settings`, chat/settings-rev.json): the rows are read
// again with no reload, a draft kept, and a change this page didn't make is toasted on any view.
(() => {
  'use strict'
  // a card per role first, named as routes.LABELS names it (#1553: Couplings), its GPU|API on top and
  // the rest of the role under More; then the rows for every role, and the power groups
  const GROUPS = [['significant', 'Complex coding'], ['trivial', 'Trivial coding'], ['writing', 'English writing'],
    ['shared', 'Agents and roles'], ['web', 'Web app'], ['gpu', 'GPU'], ['api', 'API'],
    ['setup', 'What your agents get'], ['workflows', 'Deep workflows'], ['terminal', 'Terminal'],
    ['experimental', 'Experimental']]
  const SEG_MAX = 4                     // a segmented control up to this many choices, a <select> past it
  const OTHER = '\u0000other'           // the select's "Other model…" (never a model id: a NUL)
  const UNITS: Record<string, string[]> = { usd: ['$', '/day'] }
  // the pane's toast, when a choice needs the driver's help: named as the state names it (#1355:
  // OpenCode's user read that Claude would offer), else the story's `any`
  let who = 'your driver'
  const helpSaid = () => `${who.charAt(0).toUpperCase()}${who.slice(1)} will offer to set it up.`
  // A row with a problem or a request (a note, `help`, `ask`): a link that hands it to the driver in
  // the chat's box, the fix being a terminal's that a phone can't run (the web app's review,
  // 2026-11-26: five findings asked for it). `ask` a string is the request itself
  const askLink = (row: SettingRow, title: string, line: string) => {
    if (!(row.ask || row.help || row.kind === 'note')) return null
    const a = el('button', 'set-ask', `Ask ${who}`)
    a.type = 'button'
    const text = typeof row.ask === 'string' && row.ask ? row.ask : `Help me with Settings › ${title}: ${line}`
    a.addEventListener('click', () => WB.ask(text))
    return a
  }

  let root: HTMLElement | null = null
  let rows: SettingRow[] | null = null  // the last rows the server sent
  let why = ''                          // the last read's failure, shown while there are no rows
  let reads = 0                         // each GET's number: one that left before a write ends is dropped
  let wrote = 0                         // the GET number current when the last write finished
  // [{key, value, then?}] waiting; queue[0] is in flight while busy. `then`, a change sent once this
  // one went through: a role's API after the API model picked for it (#1558)
  interface Change { key: string; value: string | null; then?: { key: string; value: string } }
  const queue: Change[] = []
  let busy = false
  let drawing = false                   // a redraw takes the focused input away: its blur sends nothing
  let changes: SettingChange[] = []     // the newest changes, newest first (GET /api/settings's `changes`)
  let lastWrite = 0                     // when this page's last write was answered (ms): its own change isn't news
  // an action's answer to draw under its row (a run's Summary: its report, #1278's S12), by key;
  // agent-written results among it, so text alone, never markup
  const details = new Map<string, string>()
  let focusGroup = ''                   // a group to bring into view at the next draw (the runs strip's tap)
  let picking = ''                      // the row whose choice opened its needed row's list under it (#1558)
  const opened = new Set<string>()      // the role cards whose More is open: a redraw keeps it so
  // who made a change, as settings-changes.jsonl's `by` says (store.caller_kind, or the web's click)
  const BY: Record<string, string> = { web: 'the web app', claude: 'Claude', opencode: 'OpenCode', terminal: 'the terminal', agent: 'an agent' }
  const byWord = (by: string) => BY[by] || by || 'someone'
  const valueWord = (v: unknown) => v == null || v === '' ? 'off' : String(v)
  const ago = (ts: number) => {
    const s = Math.max(0, Date.now() / 1000 - ts)
    return s < 60 ? 'just now' : s < 3600 ? `${Math.floor(s / 60)} min ago` : s < 86400 ? `${Math.floor(s / 3600)} h ago` : `${Math.floor(s / 86400)} d ago`
  }

  const el = <K extends keyof HTMLElementTagNameMap>(tag: K, cls?: string, text?: unknown) => {
    const e = document.createElement(tag)
    if (cls) e.className = cls
    if (text != null) e.textContent = String(text)
    return e
  }
  // what the page shows for a key: its newest queued value over the server's (the choice lit at once)
  const pending = (key?: string) => { for (let i = queue.length - 1; i >= 0; i--) if (queue[i].key === key) return queue[i]; return null }
  const shown = (row: SettingRow) => { const p = pending(row.key); return p ? p.value : row.value }

  async function load() {
    const n = ++reads
    try {
      const r = await WB.api('/api/settings')
      const body = await r.json().catch(() => ({}))
      if (n < wrote) return              // a write's rows came after it left: they are newer
      if (r.ok && Array.isArray(body.rows)) { rows = body.rows; why = '' }
      if (r.ok && Array.isArray(body.changes)) changes = body.changes
      else why = body.error || `the server said ${r.status}`
    } catch { if (n >= wrote) why = "the computer can't be reached" }
    draw()
  }

  function send(key: string, value: string | null, then?: Change['then']) {
    const waiting = queue.findIndex((q, i) => q.key === key && (i > 0 || !busy))
    if (waiting >= 0) { queue[waiting].value = value; queue[waiting].then = then }  // the same row clicked again while it waits: its newest
    else queue.push({ key, value, then })
    draw()
    pump()
  }
  async function pump() {
    if (busy || !queue.length) return
    busy = true
    const { key, value, then } = queue[0]
    let said = '', lost = false, went = false
    try {
      // given up after the page's POST_MS, into the lost path below: with no deadline a POST that
      // hung kept its value lit as applied and every later click queued behind it (#1391)
      const r = await WB.post('/api/settings', { key, value })
      const body = await r.json().catch(() => ({}))
      if (Array.isArray(body.rows)) rows = body.rows
      if (Array.isArray(body.changes)) changes = body.changes
      if (r.ok && body.ok && typeof body.detail === 'string') details.set(key, body.detail)
      went = r.ok && !!body.ok
      // a refusal in the page's words when the server has them (`page`, settings.page_line, #1558)
      said = r.ok && body.ok ? body.said || 'Done.' : body.page || body.error || body.said || `the server said ${r.status}`
      if (r.ok && body.ok && body.help) said = `${/[.!?]$/.test(said) ? said : said + '.'} ${helpSaid()}`
    } catch { said = "The computer didn't answer: Settings shows what it says now."; lost = true }
    wrote = reads + 1
    lastWrite = Date.now()
    queue.shift()
    busy = false
    if (went && then) send(then.key, then.value)  // its toast is the next answer's
    else WB.toast(said)
    draw()                              // from the answer's rows: a refusal reverts the control
    if (lost && !queue.length) load()   // the change may have landed all the same: ask
    pump()
  }

  // ---- the kinds ----
  // a <select> of ``row``'s choices (and "Other model…" where any id goes), ``pick`` given the value
  // chosen; ``at`` the row it is drawn in (its key on the typed id's box, so a redraw finds it again)
  function selectOf(row: SettingRow, value: unknown, at: string, pick: (v: string) => void, label = row.label) {
    const choices = Array.isArray(row.choices) ? row.choices : [], lit = choices.find(c => c.value === value)
    const sel = el('select', 'set-select')
    sel.setAttribute('aria-label', label)
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
      if (sel.value !== OTHER) { pick(sel.value); return }
      const input = el('input', 'set-text')
      input.type = 'text'
      input.dataset.key = at
      input.dataset.other = '1'
      input.placeholder = 'provider/model-id, then Enter'
      input.setAttribute('aria-label', `${label}: another model's id`)
      input.autocapitalize = 'off'; input.autocomplete = 'off'; input.spellcheck = false
      input.addEventListener('keydown', e => {
        if (e.key === 'Enter') { e.preventDefault(); const v = input.value.trim(); if (v) pick(v) }
        // blurred first: draw() keeps a focused input's text and would open it again
        else if (e.key === 'Escape') { e.preventDefault(); input.blur(); draw() }
      })
      sel.after(input)
      input.focus()
    })
    return sel
  }
  // A choice that needs another row's value first (`needs`: a role's API, its api-<role> with no
  // model): its tap opens that row's list under this one, and a pick sends it, then this choice.
  // The tap was refused with a terminal command, the list 2,000 px away under another card (#1558);
  // with the list not in the rows, its row (under More) is opened and focused instead
  function needed(row: SettingRow, c: SettingChoice) {
    const want = c.needs && rows && rows.find(r => r.key === c.needs)
    if (!c.needs) return false
    if (!want) return false                              // no such row here: sent, and the server says why
    if (want.value != null && want.value !== '') return false
    if (!Array.isArray(want.choices) || (!want.choices.length && !want.other)) {
      opened.add(want.group); draw()
      const at = root && root.querySelector<HTMLElement>(`.set-row[data-key="${CSS.escape(want.key)}"]`)
      if (at) { at.scrollIntoView({ block: 'center' }); const f = at.querySelector<HTMLElement>('select, input, button'); if (f) f.focus() }
      return true
    }
    picking = picking === row.key ? '' : row.key
    draw()
    const sel = root && root.querySelector<HTMLSelectElement>(`.set-row[data-key="${CSS.escape(row.key)}"] .set-pick select`)
    if (sel) sel.focus()
    return true
  }
  function choice(row: SettingRow, box: HTMLElement) {
    const value = shown(row), choices = Array.isArray(row.choices) ? row.choices : []
    const lit = choices.find(c => c.value === value)
    if (choices.length && choices.length <= SEG_MAX && !row.other) {
      const seg = el('div', 'seg set-seg')
      seg.setAttribute('role', 'radiogroup')
      seg.setAttribute('aria-label', row.label)
      seg.style.setProperty('--n', String(choices.length))
      for (const c of choices) {
        const b = el('button', '', c.label)
        b.type = 'button'
        b.setAttribute('role', 'radio')
        b.dataset.value = c.value
        b.setAttribute('aria-checked', String(c.value === value))
        // sent even when lit: what's shown may be stale (a change made at the terminal), and a
        // click to put it back sent nothing while the setting stayed changed (#1392, 2026-11-25)
        b.addEventListener('click', () => { if (!needed(row, c)) { if (picking === row.key) picking = ''; send(row.key, c.value) } })
        if (c.needs) b.setAttribute('aria-expanded', String(picking === row.key))
        seg.append(b)
      }
      box.append(seg)
      const c = picking === row.key && choices.find(x => x.needs)
      const want = c && rows && rows.find(r => r.key === c.needs)
      if (c && want) {                   // the needed row's list, under this one (#1558)
        const pick = el('div', 'set-pick')
        pick.append(selectOf(want, shown(want), row.key, v => { picking = ''; send(want.key, v, { key: row.key, value: c.value }) }, want.label))
        box.append(pick)
      }
    } else if (choices.length || row.other) {
      box.append(selectOf(row, value, row.key, v => send(row.key, v)))
    }
    const says = lit && pending(row.key) ? lit.says : row.says
    if (says) box.append(el('p', 'small set-says', says))
  }

  function number(row: SettingRow, box: HTMLElement) {
    const value = shown(row), off = value == null || value === '' || value === 'off'
    const [pre, post] = UNITS[row.unit as string] || ['', row.unit || '']
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
    const go = (asked: boolean) => {               // empty sends nothing: off is the Off button's
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

  function action(row: SettingRow, box: HTMLElement) {
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
    const detail = details.get(row.key)
    if (detail) box.append(el('pre', 'small set-detail', detail))
  }

  function drawRow(row: SettingRow, title = '') {
    const box = el('div', 'set-row')
    box.dataset.key = row.key
    box.dataset.kind = row.kind
    const ask = (line: string) => { const a = askLink(row, title, line); if (a) box.append(a); return box }
    if (row.kind === 'info') { box.append(el('p', 'small set-info', row.text || row.label)); return ask(row.text || row.label) }
    // a line in its place, the group's other rows drawn too: with the routes unreadable the shared
    // group's note sits beside Claude coding and the rest, still the owner's to change (#1286's review)
    if (row.kind === 'note') { box.append(el('p', 'small set-note', row.text || row.label)); return ask(row.text || row.label) }
    // a role's GPU|API with no API provider: its sentence alone (the shared group's line says how to add one)
    if (row.fixed) { if (row.says) box.append(el('p', 'small set-says', row.says)); return box }
    if (row.label && row.label !== title) box.append(el('div', 'set-label', row.label))  // a role's own row: its card's name
    if (row.kind === 'choice') choice(row, box)
    else if (row.kind === 'number') number(row, box)
    else if (row.kind === 'action') action(row, box)
    else if (row.text) box.append(el('p', 'small', row.text))
    if (row.changed) box.append(el('p', 'small set-changed', `Changed by ${byWord(row.changed.by)} · ${ago(row.changed.ts)}`))
    return ask(`${row.label}: ${row.says || row.text || ''}`)
  }

  let refocus: { key?: string; tag: string; value?: string } | null = null  // a control the last redraw couldn't focus (disabled while it waits)
  function draw() {
    if (!root) return
    // an id being typed elsewhere survives the redraw: only the row it was sent for is drawn anew
    const a = document.activeElement as HTMLInputElement | null
    const typing = a && root.contains(a) && a.tagName === 'INPUT' && !pending(a.dataset.key)
      ? { key: a.dataset.key, other: a.dataset.other, value: a.value, at: a.selectionStart } : null
    // and any other control keeps the focus, found again by its row and value: the redraw took it,
    // a choice made by keyboard left the focus on the page, and the next Tab began again at the
    // first row (#1409, 2026-11-25)
    const row = !typing && a && root.contains(a) && a.closest<HTMLElement>('.set-row')
    const held = row ? { key: row.dataset.key, tag: a.tagName, value: a.dataset.value }
      : !a || a === document.body ? refocus : null
    refocus = null
    const out = el('div', 'cards set-cards')
    if (!rows) {
      if (why) {
        const c = el('div', 'card set-card'), again = el('button', 'plain set-retry', 'Try again')
        again.type = 'button'
        again.addEventListener('click', load)  // the line back, and nothing has said so (#1412)
        c.append(el('p', 'small', `Settings couldn't load: ${why}`), again)
        out.append(c)
      }
    } else {
      const known = GROUPS.map(g => g[0])
      const groups = [...GROUPS, ...[...new Set(rows.map(r => r.group))].filter(g => !known.includes(g)).map(g => [g, g])]
      for (const [g, title] of groups) {
        // `hidden`: a row that can do nothing yet (an API control with no provider), kept for `wb settings`
        const mine = rows.filter(r => r && r.group === g && !r.hidden)
        if (!mine.length) continue
        const card = el('section', 'card set-card')
        card.dataset.group = g
        card.append(el('h3', '', title))
        for (const r of mine) if (!r.more) card.append(drawRow(r, title))
        const more = mine.filter(r => r.more)
        if (more.length) {
          const fold = el('details', 'set-more')
          fold.open = opened.has(g)
          fold.append(el('summary', '', 'More'))
          fold.addEventListener('toggle', () => { if (fold.open) opened.add(g); else opened.delete(g) })
          for (const r of more) fold.append(drawRow(r, title))
          card.append(fold)
        }
        out.append(card)
      }
      if (changes.length) {
        const card = el('section', 'card set-changes')
        card.append(el('h3', '', 'Recent changes'))
        const list = el('ul', 'set-change-list')
        for (const c of changes) list.append(el('li', 'small',
          `${c.label || c.key}: ${valueWord(c.from)} → ${valueWord(c.to)}, by ${byWord(c.by)} · ${ago(c.ts)}`))
        card.append(list)
        out.append(card)
      }
    }
    drawing = true
    try { root.replaceChildren(out) } finally { drawing = false }
    if (typing && typing.key) {
      let input = root.querySelector<HTMLInputElement>(`.set-row[data-key="${CSS.escape(typing.key)}"] input${typing.other ? '[data-other]' : ''}`)
      if (!input && typing.other) {
        const sel = root.querySelector<HTMLSelectElement>(`.set-row[data-key="${CSS.escape(typing.key)}"] select`)
        if (sel) { sel.value = OTHER; sel.dispatchEvent(new Event('change')); input = sel.nextElementSibling as HTMLInputElement | null }
      }
      if (input) { input.value = typing.value; input.focus(); try { input.setSelectionRange(typing.at, typing.at) } catch {} }
    }
    if (focusGroup) {
      const card = root.querySelector(`.set-card[data-group="${CSS.escape(focusGroup)}"]`)
      if (card) { focusGroup = ''; card.scrollIntoView({ block: 'start' }) }
    }
    if (held) {
      const box = root.querySelector(`.set-row[data-key="${CSS.escape(held.key!)}"]`)
      const to = box && ((held.value != null && box.querySelector<HTMLElement>(`${held.tag}[data-value="${CSS.escape(held.value)}"]`))
        || box.querySelector<HTMLElement>(held.tag) || box.querySelector<HTMLElement>('button, select, input'))
      if (to) to.focus({ preventScroll: true })
      if (to && document.activeElement !== to) refocus = held
    }
  }

  // read again when the page takes the focus: beside the terminal it stays shown, and a change
  // made there (by the user or Claude) was never drawn (#1392, 2026-11-25)
  let showing = false
  addEventListener('focus', () => { if (showing) load() })
  // a change made anywhere: the state's `settings` moved (the first state only sets the mark)
  let rev: number | null = null
  WB.on('state', (s: ChatState) => {
    if (s.driver && s.driver.who) who = s.driver.who
    const r = s.settings, ts = r ? r.ts : 0
    if (rev === null) { rev = ts; return }
    if (ts === rev) return
    rev = ts
    if (showing) load()
    const mine = r && r.by === 'web' && (busy || Date.now() - lastWrite < 5000)
    if (r && !mine) WB.toast(`${r.label || r.key} set to ${valueWord(r.to)} by ${byWord(r.by)}`)
  })
  // the runs strip's tap (app.ts): Settings at the workflows group, where each run's row is.
  // Drawn now only when Settings shows: the tap's go('settings') shows it on a later hashchange,
  // and a draw while it was hidden spent focusGroup on a scrollIntoView that did nothing, so a
  // second visit landed at the top, the card 2,000 px down (#1527, 2026-11-26); show() draws it
  document.addEventListener('wb-settings-group', e => { focusGroup = String((e as CustomEvent).detail || ''); if (showing) draw() })
  WB.view('settings', {
    mount(r: HTMLElement) { root = r },
    show() { showing = true; load() },
    hide() { showing = false },
  })
})()
