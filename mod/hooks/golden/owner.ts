// Kernel.Owner's owns: whose task it is, each case held on its Python twin too (cli._scope, through
// kernel.owns_twin), so the mod, the OpenCode plugin and `wb` answer alike (#1325's M2). The paths
// are made up (nothing on disk under /w), so wb's realpath leaves them as they are.
import type { Golden } from './run.js'

const at = {
  session: '', ids: ['s1', 's2'], driver: '', host: 'claude', gone: false,
  cwd: '', repo: '', subdir: '', here: '/w/me/proj', home: '/w/me', fold: false,
}
const mine = { mine: true, claims: true }
const not = { mine: false, claims: false }
const adoptable = { mine: false, claims: true }
const twin = 'kernel.owns_twin'

export default {
  module: 'Kernel.Owner',
  cases: [
    { fn: 'owns', in: { ...at, session: 's2', driver: 'claude', cwd: '/elsewhere/x' }, out: mine, twin,
      why: 'started in one of this process’s sessions: its own, wherever it ran' },
    { fn: 'owns', in: { ...at, session: 'other', driver: 'claude', cwd: '/w/me/proj', repo: '/w/me/proj' }, out: adoptable, twin,
      why: 'another live session’s task, here: not mine, but mine to adopt once it is gone' },
    { fn: 'owns', in: { ...at, session: 'other', driver: 'claude', gone: true, cwd: '/w/me/proj', repo: '/w/me/proj' }, out: mine, twin,
      why: 'its Claude Code process gone: a session in the project adopts it (the 0.90 review)' },
    { fn: 'owns', in: { ...at, session: 'other', driver: 'opencode', gone: true, cwd: '/w/me/proj', repo: '/w/me/proj' }, out: not, twin,
      why: 'another driver’s task is never adopted by directory, gone or not (#1309)' },
    { fn: 'owns', in: { ...at, session: 'oc1', ids: ['oc1'], driver: 'opencode', host: 'opencode', cwd: '/elsewhere' }, out: mine, twin,
      why: 'an OpenCode session owns its own task by its id' },
    { fn: 'owns', in: { ...at, session: 'gone1', ids: ['oc1'], driver: 'opencode', host: 'opencode', gone: true, repo: '/w/me/proj' }, out: mine, twin,
      why: 'and adopts a gone OpenCode session’s task near it' },
    { fn: 'owns', in: { ...at, session: 'other', driver: 'claude', gone: true, cwd: '/w/me/proj/sub', repo: '/w/me/proj', here: '/w/me/proj/sub/deep' }, out: mine, twin,
      why: 'a session inside the task’s spot (where wb start ran, inside the repo) adopts it' },
    { fn: 'owns', in: { ...at, session: 'other', driver: 'claude', gone: true, repo: '/w/mono', subdir: 'pkg/a', here: '/w/mono/pkg/b' }, out: not, twin,
      why: 'a monorepo’s sibling package is not the task’s spot (#695)' },
    { fn: 'owns', in: { ...at, session: 'other', driver: 'claude', gone: true, repo: '/w/mono', subdir: 'pkg/a', here: '/w/mono/pkg/a/src' }, out: mine, twin,
      why: 'inside its own package it is' },
    { fn: 'owns', in: { ...at, session: 'other', driver: 'claude', gone: true, cwd: '/w/me', repo: '/w/me/proj', here: '/w/me' }, out: mine, twin,
      why: 'in ~, a gone session’s task started in that very directory is adopted (#1403)' },
    { fn: 'owns', in: { ...at, session: 'other', driver: 'claude', gone: true, repo: '/w/me/proj', here: '/w/me' }, out: not, twin,
      why: 'in ~, nothing else is claimed by directory (the 0.90 review)' },
    { fn: 'owns', in: { ...at, session: 'other', driver: 'claude', gone: true, cwd: '/', repo: '/w/x', here: '/' }, out: mine, twin,
      why: 'in /, the same: only where wb start ran' },
    { fn: 'owns', in: { ...at, cwd: '/w/me/proj', repo: '/w/me/proj' }, out: mine, twin,
      why: 'no session (by hand, or before 0.90): by directory' },
    { fn: 'owns', in: { ...at, cwd: '/w/me/projector', repo: '/w/me/projector' }, out: not, twin,
      why: 'whole components: /w/me/projector is not inside /w/me/proj' },
    { fn: 'owns', in: { ...at, cwd: '/w/me/proj', repo: '/w/me/proj', here: '/w/me' }, out: not, twin,
      why: 'no session, and the session in ~: no one’s by directory there' },
    { fn: 'owns', in: { ...at, repo: '/w/me/proj/a' }, out: mine, twin,
      why: 'a repo inside the session’s folder of several' },
    { fn: 'owns', in: { ...at, repo: '/W/Me/Proj', fold: true }, out: mine, twin,
      why: 'on a file system that ignores case, ~/Repos is ~/repos (the 2026-11-19 audit)' },
    { fn: 'owns', in: { ...at, repo: '/W/Me/Proj' }, out: not, twin,
      why: 'where case counts, it is another directory' },
    { fn: 'owns', in: at, out: mine, twin, why: 'a task with no key at all is everyone’s' },
    { fn: 'owns', in: { ...at, repo: '/w/elsewhere', here: '' }, out: mine, twin,
      why: 'a session that knows no directory claims every task without a session' },
    { fn: 'owns', in: { ...at, session: 'other', driver: 'claude', repo: '/w/me/proj', ids: [] }, out: mine, twin,
      why: 'a session with no ids yet goes by directory, its session or not' },
    { fn: 'owns', in: { ...at, session: 'other', driver: 'claude', gone: true, cwd: '/w/me/proj/', repo: '/w/me/proj/' }, out: mine, twin,
      why: 'a trailing slash is the same directory' },
  ],
} satisfies Golden
