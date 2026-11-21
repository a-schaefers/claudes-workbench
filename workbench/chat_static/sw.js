// The web chat's service worker: notifications only. It caches nothing and answers no fetch, so
// the page always comes from the computer (another app's offline caching gave iOS installed apps
// white screens). A push carries no payload (workbench/webpush.py): the worker fetches the state
// with the key the page left it and shows the newest thing that needs the user.
'use strict'

self.addEventListener('install', () => self.skipWaiting())
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()))

async function key() {
  const r = await (await caches.open('wb-chat')).match('/key')
  return r ? r.text() : ''
}

const plain = t => String(t || '').replace(/```[\s\S]*?```/g, ' ').replace(/[`*_#>]/g, '').replace(/\s+/g, ' ').trim()
const cut = (t, n) => t.length > n ? t.slice(0, n - 1) + '…' : t

async function what() {
  const token = await key()
  const r = await fetch('/api/state', { headers: { Authorization: 'Bearer ' + token }, cache: 'no-store' })
  if (!r.ok) throw new Error('HTTP ' + r.status)
  const s = await r.json()
  const now = s.now || Date.now() / 1000
  // an ask still open, not one past the mod's wait; and only if it's newer than Claude's last
  // word: a dead session's card beat every later reply (#538)
  const open = (s.asks || []).filter(a => !a.answer && !a.stale)
  const last = (s.messages || []).filter(m => m.from === 'claude').at(-1)
  const newest = Math.max(open.at(-1)?.ts || 0, last?.ts || 0)
  const test = s.push && s.push.test
  if (test && now - test < 120 && test >= newest) return { title: 'Notifications work', body: "You'll hear from Claude here while you're away." }
  const a = open.at(-1)
  if (a && !(last && last.ts > a.ts)) {
    if (a.kind === 'plan') return { title: "Claude's plan is ready", body: 'Send it back with a note, or approve it in the terminal.' }
    if (a.kind === 'question') return { title: 'Claude asks', body: cut(plain((a.detail?.questions || [])[0]?.question), 180) }
    return { title: 'Claude wants to use ' + a.tool, body: cut(plain(a.text), 180) }
  }
  if (last) return { title: 'Claude', body: cut(plain(last.text), 180) }
  return { title: 'Claude', body: 'Something new in the chat.' }
}

self.addEventListener('push', e => {
  e.waitUntil((async () => {
    let n
    try { n = await what() } catch { n = { title: 'Claude', body: 'Something new in the chat.' } }
    await self.registration.showNotification(n.title, {
      body: n.body, icon: '/icon-192.png', badge: '/icon-192.png', tag: 'wb-chat', renotify: true, data: { url: '/' },
    })
    // news the open page hasn't shown (a push is only for what no page saw): its line may be dead
    for (const c of await self.clients.matchAll({ type: 'window', includeUncontrolled: true })) c.postMessage({ kind: 'push' })
  })())
})

self.addEventListener('notificationclick', e => {
  e.notification.close()
  e.waitUntil((async () => {
    const wins = await self.clients.matchAll({ type: 'window', includeUncontrolled: true })
    const w = wins.find(c => new URL(c.url).origin === self.location.origin)
    if (w) return w.focus()
    return self.clients.openWindow('/')
  })())
})
