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

const plain = (t: unknown) => String(t || '').replace(/```[\s\S]*?```/g, ' ').replace(/[`*_#>]/g, '').replace(/\s+/g, ' ').trim()
const cut = (t: string, n: number) => t.length > n ? t.slice(0, n - 1) + '…' : t

// Claude's message or a waiting line, as /api/latest gives it (chat.latest), and the listener
interface Latest {
  now?: number; push?: { test?: number }
  messages?: { from: string; text: string; ts?: number; what?: string; driver?: { tool: string } }[]
  listener?: { tool?: string; driver?: { tool: string } } | null
}

// The title names the driver (#1319): a reply's own `driver`, else the listener's, else Claude
// (Claude Code's, and every row before 2.2). OpenCode's user got pushes titled "Claude"
const NAMES: Record<string, string> = { claude: 'Claude', opencode: 'OpenCode' }
const named = (d?: { tool: string } | null) => (d && NAMES[d.tool]) || ''
const driver = (s: Latest) => named(s.listener && s.listener.driver)
  || (s.listener && s.listener.tool === 'opencode' ? 'OpenCode' : '') || 'Claude'

async function what(): Promise<{ title: string; body: string }> {
  const token = await key()
  // Claude's last word only: the whole state was up to a megabyte (#755)
  const r = await fetch('/api/latest', { headers: { Authorization: 'Bearer ' + token }, cache: 'no-store' })
  if (!r.ok) throw new Error('HTTP ' + r.status)
  const s: Latest = await r.json()
  const now = s.now || Date.now() / 1000
  // Claude's last word, or the line that it waits in the terminal (#959, #1350)
  const last = (s.messages || []).filter(m => m.from === 'claude' || m.from === 'notice').at(-1)
  const test = s.push && s.push.test
  if (test && now - test < 120 && test >= (last?.ts || 0)) return { title: 'Notifications work', body: `You'll hear from ${driver(s)} here while you're away.` }
  // the command as it is: plain() would drop the `*` of `rm -rf runs/*`
  if (last && last.from === 'notice') return { title: last.text,
    body: cut(String(last.what || '').replace(/\s+/g, ' ').trim() || 'Answer it in the terminal.', 180) }
  if (last) return { title: named(last.driver) || driver(s), body: cut(plain(last.text), 180) }
  return { title: driver(s), body: 'Something new in the chat.' }
}

self.addEventListener('push', e => {
  e.waitUntil((async () => {
    let n: { title: string; body: string }
    try { n = await what() } catch { n = { title: 'Claude', body: 'Something new in the chat.' } }
    await self.registration.showNotification(n.title, {
      body: n.body, icon: '/icon-192.png', badge: '/badge-96.png', tag: 'wb-chat', renotify: true, data: { url: '/#chat' },
    } as NotificationOptions)  // renotify: lib.dom's options lack it, the browsers that take it read it
    // news the open page hasn't shown (a push is only for what no page saw): its line may be dead
    for (const c of await self.clients.matchAll({ type: 'window', includeUncontrolled: true })) c.postMessage({ kind: 'push' })
  })())
})

self.addEventListener('notificationclick', e => {
  e.notification.close()
  e.waitUntil((async () => {
    const wins = await self.clients.matchAll({ type: 'window', includeUncontrolled: true })
    // to the thread, whichever view the page was on (#1284)
    const w = wins.find(c => new URL(c.url).origin === self.location.origin)
    if (w) { w.postMessage({ kind: 'view', view: 'chat' }); return w.focus() }
    return self.clients.openWindow('/#chat')
  })())
})
