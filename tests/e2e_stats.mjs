// The Stats view's checks in a real browser (#1284's stub, for web-stats to fill). e2e_chat.mjs
// calls check(page, ctx) once per device, on a page with the key and the server up; ctx holds
// { engine, device, desktop, base, token, home, env, wb, until, check, sleep, goTo, context,
// browser } (goTo('stats') taps the tab and waits for the view). Leave the page where it was.
export async function check(page, ctx) {}
