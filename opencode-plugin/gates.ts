// What the OpenCode driver may do without the user's yes, read where OpenCode asks for permission
// (#1314, 2026-11-25; the host-shape decision on #1296: `permission.hook("evaluate")` over OpenCode's
// own scan of a shell line, deny and point to the tool). The same outcomes as the Claude Code mod's
// gates (mod/hooks/register.tsx: routeRefusal, sendRefusal, routesFileRefusal, codingRefusal,
// localOnlyRefusal), OpenCode-native:
// - a shell command that changes a role on the API side, starts a task on an API model or sets a
//   role's Settings row (the kernel's gate `route`, `start`, `settings`, whose dry run says it is
//   gated), or sends a patch upstream (`send`), is denied with the tool that asks the user in a
//   form instead (consent.ts): OpenCode splits the line, the kernel classifies each simple
//   command (`gateOf`), never what a text mentions (#1364). A line OpenCode can't scan (a heredoc,
//   `$(…)`, `bash -c`) passes here, and wb's own refusal (store.under_model) is the guarantee;
// - an edit of routes.json or of an approval file (`route-ok*.json`, `send-ok*.json` in $WB_HOME),
//   or a shell command that writes one (the kernel's `writes`), is denied for every agent;
// - an edit in a task's worktree meets the coding gate (the kernel's `codingGate`): pass, start,
//   ask (the form) or refuse;
// - an edit of commands.json that drops a repo's `"local_only": true` is refused (execute.before,
//   which sees the edit's text: the permission hook sees only its path).
// Pure but for the file reads the callers hand in; nothing runs at import.
import { basename, dirname, isAbsolute, join, resolve } from 'node:path'

export type Env = Record<string, string | undefined>

// A WB_* path as store.absolute_env reads it: `~` and a relative path expanded (store.PATH_VARS)
export function expandPath(v: string, env: Env, cwd: string): string {
  const home = env.HOME ?? '~'
  const expanded = v === '~' ? home : v.startsWith('~/') ? join(home, v.slice(2)) : v
  return resolve(cwd, expanded)
}

export interface Places {
  readonly home: string      // $WB_HOME
  readonly routes: string    // routes.json (routes.path: $WB_ROUTES)
  readonly commands: string  // commands.json (context.commands_path: $WB_COMMANDS)
  readonly user: string      // $HOME
}

export function places(env: Env, cwd: string): Places {
  const user = env.HOME ?? '~'
  const conf = join(user, '.config', 'workbench')
  return {
    home: env.WB_HOME ? expandPath(env.WB_HOME, env, cwd) : join(user, '.local', 'state', 'workbench'),
    routes: env.WB_ROUTES ? expandPath(env.WB_ROUTES, env, cwd) : join(conf, 'routes.json'),
    commands: env.WB_COMMANDS ? expandPath(env.WB_COMMANDS, env, cwd) : join(conf, 'commands.json'),
    user,
  }
}

// The host's one-shot yes in $WB_HOME: Claude Code's mod writes route-ok.json and send-ok.json, this
// plugin `<stem>.<session>.json` (store.approval_name, #1363); none is any model's to write
export const APPROVAL = /^(?:route|send)-ok(?:\.[\w-]{1,128})?\.json$/

// Which protected file ``abs`` is, by its path or by real path (``real``: a link to one is it)
export function guarded(abs: string, p: Places, real: (path: string) => string = x => x): '' | 'routes' | 'approval' {
  for (const path of new Set([abs, real(abs)])) {
    if (path === p.routes || path === real(p.routes)) return 'routes'
    if (APPROVAL.test(basename(path)) && (dirname(path) === p.home || dirname(path) === real(p.home))) return 'approval'
  }
  return ''
}

// A word a command writes, as a path: `~`, $HOME and $WB_HOME spelled out; '' for one only the
// shell knows (another variable, a glob) or a relative one where the directory is unknown
export function wordPath(w: string, at: string, p: Places): string {
  const v = w.replace(/^\$(?:\{WB_HOME\}|WB_HOME\b)/, p.home).replace(/^\$(?:\{HOME\}|HOME\b)/, p.user)
    .replace(/^~(?=\/|$)/, p.user)
  if (/[$`*?[]/.test(v) || (!isAbsolute(v) && !at)) return ''
  return resolve(at || '/', v)
}

// The task whose worktree holds ``path``, else '': tasks/<t>/worktree, or tasks/<t>/tree/…/worktree
// for a sibling further up (#261); register.tsx's taskOfPath, the same rule
export function taskOfPath(path: string, root: string): string {
  if (!root || !path.startsWith(`${root}/tasks/`)) return ''
  return /^([^/]+)\/(?:tree\/(?:[^/]+\/)+)?worktree\//.exec(path.slice(root.length + 7))?.[1] ?? ''
}

// --- the refusals' words (the model reads them: each says what to do instead)
export const TOOL = {
  route: 'workbench_route', start: 'workbench_start', settings: 'workbench_settings_set', send: 'workbench_patch_send',
} as const
export const VIA_TOOL = (tool: string) =>
  `workbench: not run: this change needs the user's yes, which the ${tool} tool asks for in a form; ` +
  `nothing changed. Call ${tool} with the same arguments instead.`
export const SEND_VIA_TOOL = `workbench: not sent: wb patch send goes through the ${TOOL.send} tool, which shows ` +
  'the user what it sends and asks; nothing was sent.'
export const APPROVAL_OWN = (file: string) => `workbench: not run: ${file} is the workbench plugin's form's to ` +
  "write, on the user's yes; no command and no edit writes it."
export const ROUTES_EDIT = "workbench: not edited: the roles change only through wb route, on the user's yes " +
  `(the ${TOOL.route} tool asks them; wb route shows the roles).`
// a form's no, and no form at all (consent.ts's forms and upstream.ts's)
export const NOT_SENT = 'workbench: not sent: the user did not approve it in the form. Tell them it was not ' +
  'sent; try again only if they ask for it.'
export const NO_FORM = (what: string, command: string) => `workbench: ${what}: no OpenCode window is open on ` +
  'this session to ask the user in (the form shows in the terminal UI), so nothing was asked and nothing ' +
  `changed. Tell them; once one is open, ask again. Or the user runs it in a terminal of their own: ${command}`
export const SEND_ASK = { yes: 'Send it', no: "Don't send" }
export const NOT_EDITED = (task: string) => `workbench: not edited: the user said no to editing ${task}'s code. ` +
  'Keep the work with the agent; ask again only if they ask for it.'
export const ONLY = (task: string) => `workbench: not edited: ${task} is the agent's. Coding is "Only when I say" ` +
  "(the user's choice; `wb coding` shows it). Never write a task's code yourself, not a one-line fix in review " +
  'nor `wb takeover`: guide the agent, and when it stays stuck, report the blocker to the user and keep the ' +
  'work with the agent. They say when you write code; then record their words with `wb coding allow <task> ' +
  '--edit "…" --said "<their words>"`.'

// --- the shell gate

export interface Gate {
  readonly word?: string  // the command's own word, lowercased (popd's: the directory unknown after it)
  readonly gate: string
  readonly args: ReadonlyArray<string>
  readonly writes: ReadonlyArray<{ readonly to: string }>
}
export type Classify = (words: ReadonlyArray<string>, writes: ReadonlyArray<string>) => Gate
// a dry run's verdict: whether this call is a gated change (wb route's `--dry-run --json`)
export type Dry = (args: ReadonlyArray<string>, cwd: string) => Promise<boolean>

export interface Command {
  readonly words: string[]
  readonly writes: string[]
}

// A shell call's simple commands as the permission hook should walk them: OpenCode's scan
// (core/src/shell/parse.ts) leaves a plain `cd`, `pushd` or `popd` out of the resources it hands
// the hook (a directory change is its external_directory check's, never a command's), so a cd in
// the line was never seen and a write after it was read from the wrong directory (#1314, the
// kernel-gates lane notes, 2026-11-26). Each resource is the raw text of its command in the line:
// the stretch between two holds the commands the scan dropped, and a directory change there is put
// back in its place. A resource is found where a command starts (the line's start, after a list
// operator, a group or a keyword), never inside another's words (`cd ls && ls`); one not found
// in order leaves the scan's list as it is.
const CWD_WORDS = new Set(['cd', 'pushd', 'popd', 'chdir'])
const STARTS = /(?:^|[;&|(){}\n]|(?:^|\s)(?:then|do|else|elif|if|while|until|!|time))\s*$/
export function lineCommands(line: string, resources: ReadonlyArray<string>): string[] {
  const out: string[] = []
  let at = 0
  for (const r of resources) {
    let i = r ? line.indexOf(r, at) : -1
    while (i >= 0 && !STARTS.test(line.slice(at, i))) i = line.indexOf(r, i + 1)
    if (i < 0) return [...resources]
    out.push(...dropped(line.slice(at, i)))
    out.push(r)
    at = i + r.length
  }
  return out
}

// the directory changes in a stretch of a line no resource covers, in order
function dropped(gap: string): string[] {
  return gap.split(/&&|\|\||[;|&\n(){}]/).map(x => x.trim().replace(/^(?:(?:then|do|else|elif|if|while|until|!|time)\s+)+/, ''))
    .filter(x => CWD_WORDS.has((x.split(/\s/, 1)[0] ?? '').toLowerCase()))
}

// Why a shell call is denied, or '': its simple commands in order (OpenCode's scan, each one's
// words and redirect targets, the directory changes put back: lineCommands), run from ``dir``. A
// cd or pushd moves the next commands' directory, as the mod follows it (#1125), a bare one to
// $HOME; after one only the shell can resolve (`cd "$X"`, `cd -`, a popd), a relative path is unknown.
export async function shellRefusal(commands: ReadonlyArray<Command>, dir: string, p: Places, classify: Classify,
                                   dry: Dry, real: (path: string) => string = x => x): Promise<string> {
  let here = dir
  for (const c of commands) {
    const g = classify(c.words, c.writes)
    for (const w of g.writes) {
      const to = wordPath(w.to, here, p)
      const kind = to ? guarded(to, p, real) : ''
      if (kind === 'approval') return APPROVAL_OWN(basename(to))
      if (kind === 'routes') return ROUTES_EDIT
    }
    if (g.gate === 'cd') {
      const to = g.args[0]
      here = !to ? p.user : to === '-' || /[$`]/.test(to) ? '' : wordPath(to, here, p)
      continue
    }
    if (g.word === 'popd' || g.word === 'chdir') {
      here = ''
      continue
    }
    if (g.gate === 'send') return SEND_VIA_TOOL
    if (g.gate !== 'route' && g.gate !== 'start' && g.gate !== 'settings') continue
    // a show (`wb route`, `--json`) and a dry run change nothing
    const live = !g.args.includes('--dry-run') && (g.args[0] === '--start' || g.args[0] === '--settings'
      || g.args.some(a => a !== '--json'))
    if (live && await dry(g.args, here || dir)) return VIA_TOOL(TOOL[g.gate as 'route' | 'start' | 'settings'])
  }
  return ''
}

// --- the coding gate

export type CodingVerdict = 'pass' | 'start' | 'ask' | 'refuse'
export type Coding = (facts: { mode: string; allowed: ReadonlyArray<{ used: boolean; when: boolean; started: boolean }> }) =>
  { readonly verdict: CodingVerdict | string; readonly open: number }

// The user's coding mode (coding.json `mode`, cli.coding_mode): `ask`, `only`, else `decide`
export function codingMode(text: string | undefined): string {
  try {
    const mode = JSON.parse(text ?? '').mode
    return mode === 'ask' || mode === 'only' ? mode : 'decide'
  } catch {
    return 'decide'
  }
}

// A task's allowances (tasks/<t>/coding.json `allowed`), the rows as they are
export function allowances(text: string | undefined): Record<string, unknown>[] {
  try {
    const rows = JSON.parse(text ?? '').allowed
    return Array.isArray(rows) ? rows.filter(x => x && typeof x === 'object') : []
  } catch {
    return []
  }
}

// --- local_only (#1021): only the user lifts a repo's

const marks = (text: string, value: string) => (text.match(new RegExp(`"local_only"\\s*:\\s*${value}\\b`, 'g')) ?? []).length

function localOnly(text: string): Map<string, unknown> | null {
  try {
    const repos = JSON.parse(text)?.repos
    if (!repos || typeof repos !== 'object') return new Map()
    return new Map(Object.entries(repos).filter(([, v]) => v && typeof v === 'object' && 'local_only' in (v as object))
      .map(([k, v]) => [k, (v as Record<string, unknown>).local_only]))
  } catch {
    return null
  }
}

// Why an edit of commands.json from ``now`` to ``next`` is refused, or '' (register.tsx's
// localOnlyRefusal, the same rule): one that drops a `"local_only": true`, or writes a value other
// than true the text didn't have; by the marks' count when either text isn't JSON
export function localOnlyRefusal(now: string, next: string): string {
  const before = localOnly(now), after = localOnly(next)
  let dropped: string[], lifted: string[]
  if (before && after) {
    dropped = [...before].filter(([k, v]) => v === true && after.get(k) !== true).map(([k]) => k)
    lifted = [...after].filter(([k, v]) => v !== true && before.get(k) !== v && !dropped.includes(k)).map(([k]) => k)
  } else {
    const named = before ? [...before].filter(([, v]) => v === true).map(([k]) => k) : ['a repo']
    dropped = marks(next, 'true') < marks(now, 'true') ? named : []
    lifted = marks(next, 'false') > marks(now, 'false') ? ['a repo'] : []
  }
  return localOnlyWords(dropped, lifted)
}

// A patch's lines for commands.json (patchLines): by the marks it removes and adds, as the mod
// counts them in a text that isn't JSON
export function localOnlyPatchRefusal(removed: string, added: string): string {
  return localOnlyWords(marks(removed, 'true') > marks(added, 'true') ? ['a repo'] : [],
    marks(added, 'false') > marks(removed, 'false') ? ['a repo'] : [])
}

function localOnlyWords(dropped: string[], lifted: string[]): string {
  const why = [...(dropped.length ? [`drops "local_only": true for ${dropped.join(', ')}`] : []),
    ...(lifted.length ? [`sets "local_only" to something other than true for ${lifted.join(', ')}`] : [])]
  if (!why.length) return ''
  return `workbench: not changed: this edit ${why.join(' and ')} in commands.json. ` +
    "Only the user lifts a repo's local_only (it keeps API models off that repo); tell them, and leave it as it is."
}

// The text an OpenCode edit or write leaves in a file whose text is ``now`` (edit: `oldString` →
// `newString`, every one with `replaceAll`); undefined for another tool
export function editedText(tool: string, input: Record<string, unknown>, now: string): string | undefined {
  if (tool === 'write') return String(input.content ?? '')
  if (tool !== 'edit') return undefined
  const from = String(input.oldString ?? ''), to = String(input.newString ?? '')
  if (!from) return now
  return input.replaceAll ? now.split(from).join(to) : now.replace(from, () => to)
}

// The files a patch tool's text names (`*** Add File:`, `*** Update File:`, `*** Delete File:`,
// `*** Move to:`), as written
export function patchFiles(text: string): string[] {
  const out: string[] = []
  for (const m of text.matchAll(/^\*\*\* (?:(?:Add|Update|Delete) File|Move to): (.+?)\s*$/gm)) out.push(m[1]!)
  return out
}

// A patch's lines for one file: what it removes and what it adds (for the local_only marks)
export function patchLines(text: string, file: string, resolveFile: (f: string) => string): { removed: string; added: string } {
  const removed: string[] = [], added: string[] = []
  let on = false
  for (const line of text.split('\n')) {
    const head = /^\*\*\* (?:(?:Add|Update|Delete) File|Move to): (.+?)\s*$/.exec(line)
    if (head) { on = resolveFile(head[1]!) === file; continue }
    if (!on || line.startsWith('***')) continue
    if (line.startsWith('-')) removed.push(line.slice(1))
    else if (line.startsWith('+')) added.push(line.slice(1))
  }
  return { removed: removed.join('\n'), added: added.join('\n') }
}
