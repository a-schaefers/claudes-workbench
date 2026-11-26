// sw.ts runs as a classic service worker script, so its `self` is lib.webworker's WorkerGlobalScope:
// these are the ServiceWorkerGlobalScope members it uses (a module, with a typed `self`, would
// need the worker registered as a module, and the page loads it as it always has, #1323)
interface WorkerGlobalScope {
  readonly clients: Clients
  readonly registration: ServiceWorkerRegistration
  skipWaiting(): Promise<void>
  addEventListener<K extends keyof ServiceWorkerGlobalScopeEventMap>(type: K,
    listener: (this: WorkerGlobalScope, ev: ServiceWorkerGlobalScopeEventMap[K]) => any,
    options?: boolean | AddEventListenerOptions): void
}
