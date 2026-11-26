// A command line as bash reads it, and what in it sends to workbench upstream: one reading for both
// hosts. Moved out of register.tsx (#1296, 2026-11-26) so the OpenCode plugin's upstream gate
// (opencode-plugin/upstream.ts) reads a line exactly as the mod's consent gate does: wb has no
// refusal of its own behind a gh write or a git push, so OpenCode's scan, which leaves a
// `--body "$(cat <<'EOF' …)"` unread (opaque), can't be all that stands there. Which gate a simple
// command meets is the kernel's (gateOf, kernel/BOUNDARY.md); the parsing, the paths and the
// interpreter's text stay here, in TypeScript (its *Rulings*). Pure: no `$`, no I/O.
import { gateOf } from './core'
import type { Gate as CommandGate } from './core'

// The commands that hand work out: run as a command, not merely written down. #409 (2026-11-20):
// a Python patch whose string held "split it (wb next)" turned a session's workbench mode on. So
// the command is read as bash reads it (#560, the text matcher before it disagreed both ways: it
// missed `"…/bin/wb" start`, `if wb start`, `! wb start`, `echo "$(wb start)"`, `timeout 60 wb
// start`, anything after a `<<<` here-string, and `wb resume`, and took `echo foo \; wb start` for
// two commands): heredoc bodies and comments dropped, quotes and backslashes undone within a word,
// a `$(…)` or backtick inside double quotes read as the commands it runs, and only a simple
// command's own word counts, past `VAR=x`, the reserved words and the wrappers (with their
// options): `wb` or a path ending in `/wb`. Which words those are is the kernel's (Kernel.Gate's
// gateOf, `delegates`); the parsing here is the host's (kernel/BOUNDARY.md, *Rulings*).
// Where the `(` at `open` closes (the end when it never does), as bash finds it: quotes, comments
// and heredoc bodies skipped. A `gh issue create --body "$(cat <<'EOF' …)"` whose body said
// "doesn't (it can't)" or "a)" closed there, and the body's backquoted `wb patch send` after it
// was read as a send (the review of #1364, 2026-11-25).
export function closing(src: string, open: number): number {
  let depth = 0
  const tags: { tag: string; strip: boolean }[] = []
  for (let i = open; i < src.length; i++) {
    const c = src[i]
    if (c === '\\') i++
    else if (c === "'") { const j = src.indexOf("'", i + 1); i = j < 0 ? src.length : j }
    else if (c === '"') {
      let j = i + 1
      while (j < src.length && src[j] !== '"') {
        if (src[j] === '\\') j += 2
        else if (src[j] === '$' && src[j + 1] === '(') j = closing(src, j + 1) + 1
        else j++
      }
      i = j
    } else if (c === '#' && /[\s;&|()]/.test(src[i - 1] ?? ' ')) { const j = src.indexOf('\n', i); i = j < 0 ? src.length : j - 1 }
    else if (c === '<' && src[i + 1] === '<' && src[i + 2] !== '<') {
      let j = i + 2
      const strip = src[j] === '-'
      if (strip) j++
      while (src[j] === ' ' || src[j] === '\t') j++
      const m = /^(?:'([^']*)'|"([^"]*)"|\\?([^\s;&|<>()'"]+))/.exec(src.slice(j))
      if (m) tags.push({ tag: m[1] ?? m[2] ?? m[3]!, strip })
      i = j + (m ? m[0].length : 0) - 1
    } else if (c === '\n' && tags.length) { // the bodies, up to each tag's line
      let p = i + 1
      for (const h of tags) {
        while (p < src.length) {
          const e = src.indexOf('\n', p)
          const line = src.slice(p, e < 0 ? src.length : e)
          p = e < 0 ? src.length : e + 1
          if ((h.strip ? line.replace(/^\t+/, '') : line) === h.tag) break
        }
      }
      tags.length = 0
      i = p - 1
    } else if (c === '(') depth++
    else if (c === ')' && --depth === 0) return i
  }
  return src.length
}

// The commands an unquoted heredoc's body runs: its `$(…)` and backticks (`\\` escapes)
export function expansions(body: string, info: Shell): string[][] {
  const out: string[][] = []
  for (let j = 0; j < body.length; j++) {
    if (body[j] === '\\') j++
    else if (body[j] === '$' && body[j + 1] === '(') {
      const k = closing(body, j + 1)
      out.push(...simpleCommands(body.slice(j + 2, k), info))
      j = k
    } else if (body[j] === '`') {
      const k = body.indexOf('`', j + 1)
      const end = k < 0 ? body.length : k
      out.push(...simpleCommands(body.slice(j + 1, end), info))
      j = end
    }
  }
  return out
}

// What the parser saw beside a command's words (#1364), keyed by its words: its stdin's text (a
// heredoc's body, a here-string), the files its redirects write (`>`, `>>`, `>|`, `&>`), and the
// command a pipe feeds it from. A redirect's target and a here-string are no words of the command:
// `python3 - > out.txt <<EOF` runs its heredoc, not a script named out.txt.
export type Shell = { stdin: Map<string[], string>; writes: Map<string[], string[]>; piped: Map<string[], string[]> }
export const newShell = (): Shell => ({ stdin: new Map(), writes: new Map(), piped: new Map() })

// The simple commands a bash command line runs, each as its words (quotes undone)
export function simpleCommands(src: string, info: Shell = newShell()): string[][] {
  const out: string[][] = []
  let words: string[] = []
  let word: string | null = null // null: between words
  let redirect: '' | 'write' | 'read' | 'here' = '' // what the next word is
  let pipeFrom: string[] | null = null
  const heredocs: { tag: string; strip: boolean; quoted: boolean; owner: string[] }[] = []
  const endWord = () => {
    if (word !== null) {
      if (redirect === 'write') info.writes.set(words, [...(info.writes.get(words) ?? []), word])
      else if (redirect === 'here') info.stdin.set(words, `${word}\n`)
      else if (redirect !== 'read') words.push(word)
      redirect = ''
    }
    word = null
  }
  const endCommand = () => {
    endWord()
    const done = words
    if (done.length) { if (pipeFrom) info.piped.set(done, pipeFrom); out.push(done); pipeFrom = null }
    words = []
    redirect = ''
    return done
  }
  const n = src.length
  let i = 0
  while (i < n) {
    const c = src[i]!
    if (c === '\\') {
      if (src[i + 1] !== '\n') word = (word ?? '') + (src[i + 1] ?? '') // `\;` is a `;` in a word
      i += 2
    } else if (c === "'") {
      const j = src.indexOf("'", i + 1)
      const end = j < 0 ? n : j
      word = (word ?? '') + src.slice(i + 1, end)
      i = end + 1
    } else if (c === '"') {
      let j = i + 1
      let text = ''
      while (j < n && src[j] !== '"') {
        if (src[j] === '\\') { text += src[j + 1] ?? ''; j += 2 }
        else if (src[j] === '$' && src[j + 1] === '(') {
          const k = closing(src, j + 1)
          out.push(...simpleCommands(src.slice(j + 2, k), info))
          j = k + 1
        } else if (src[j] === '`') {
          const k = src.indexOf('`', j + 1)
          const end = k < 0 ? n : k
          out.push(...simpleCommands(src.slice(j + 1, end), info))
          j = end + 1
        } else text += src[j++]
      }
      word = (word ?? '') + text
      i = j + 1
    } else if (c === '#' && word === null) { // a comment, to the line's end
      const j = src.indexOf('\n', i)
      i = j < 0 ? n : j
    } else if (c === '\n') {
      endCommand()
      i++
      for (const h of heredocs) { // their bodies start on the next line: up to the line that is the tag
        let body = ''
        while (i < n) {
          const j = src.indexOf('\n', i)
          const line = src.slice(i, j < 0 ? n : j)
          i = j < 0 ? n : j + 1
          if ((h.strip ? line.replace(/^\t+/, '') : line) === h.tag) break
          body += `${line}\n`
        }
        info.stdin.set(h.owner, body)
        if (!h.quoted) out.push(...expansions(body, info)) // text, but `$(…)` in it runs
      }
      heredocs.length = 0
    } else if (c === '<' && src.startsWith('<<<', i)) { // a here-string: its word follows
      endWord()
      redirect = 'here'
      i += 3
    } else if (c === '<' && src[i + 1] === '<') { // a heredoc: its tag, quotes undone
      endWord()
      let j = i + 2
      const strip = src[j] === '-'
      if (strip) j++
      while (src[j] === ' ' || src[j] === '\t') j++
      const m = /^(?:'([^']*)'|"([^"]*)"|\\?([^\s;&|<>()'"]+))/.exec(src.slice(j))
      if (m) heredocs.push({ tag: m[1] ?? m[2] ?? m[3]!, strip, quoted: m[3] === undefined || m[0].startsWith('\\'), owner: words })
      i = j + (m ? m[0].length : 0)
    } else if (c === '$' && src[i + 1] === '{') { // ${…}: part of the word
      const j = src.indexOf('}', i)
      const end = j < 0 ? n : j
      word = (word ?? '') + src.slice(i, end + 1)
      i = end + 1
    } else if (c === '$' && src[i + 1] === '(') { // $(…): the commands inside are commands too
      endCommand()
      i += 2
    } else if (c === '>' || c === '<' || (c === '&' && src[i + 1] === '>')) { // a redirect: its file follows
      if (c !== '&' && word !== null && /^\d+$/.test(word)) word = null // `2>`: a descriptor, no word
      endWord()
      let j = i + 1
      if (c === '&' || (c === '>' && (src[j] === '>' || src[j] === '|'))) j++
      if (c === '&' && src[j] === '>') j++
      const dup = c !== '&' && src[j] === '&' && /[\d-]/.test(src[j + 1] ?? '') // `2>&1`, `<&0`: a descriptor, no file
      if (c !== '&' && src[j] === '&') j++ // `>&file` is `&>file`
      redirect = c === '>' || c === '&' ? (dup ? 'read' : 'write') : 'read'
      i = j
    } else if (c === '|' && src[i + 1] === '|') {
      endCommand()
      i += 2
    } else if (c === '|') { // a pipe: what the command before it prints is the next one's stdin
      const done = endCommand()
      if (done.length) pipeFrom = done
      i += src[i + 1] === '&' ? 2 : 1
    } else if (';&|()`'.includes(c)) {
      endCommand()
      i++
    } else if (c === ' ' || c === '\t') {
      endWord()
      i++
    } else {
      word = (word ?? '') + c
      i++
    }
  }
  endCommand()
  return out
}

export function delegates(command: string): boolean {
  return simpleCommands(command).some(words => gateOf(words).delegates)
}

// The consent gate (#1005, 2026-11-22). The owner's rule, absolute: a user's Claude never forks,
// pushes, opens or comments on an issue or pull request at workbench upstream without the user's
// yes for that exact action. A rule in the skill is advice Claude may misread; this is the one
// place every Bash and Monitor call (and the GitHub MCP tools' calls) passes, so the yes is the
// user's own, in a dialog, each time (nothing is remembered: a yes to one pull request is no yes
// to the next). What sends: these gh forms, a gh api call that writes, a git push, and wb patch
// send (the turnkey path's one send, whose single dialog shows its title and body). Read as bash
// reads it (simpleCommands, as `delegates`), so an echo's or a heredoc's text doesn't count, and
// `bash -c '…'` and `eval` are read inside, and so is what a shell is fed on its stdin (a heredoc,
// a here-string, a pipe): `bash <<EOF … git push` once went through (the review of #1005). An
// interpreter's code (-c, -e, its stdin) is text: a send in it counts only when that code runs
// commands (`spawns`, OUTWARD_TEXT). A heredoc or quoted text any other command is handed is its
// data: a Python heredoc editing an issue body that named wb patch send was refused as a send
// three times on 2026-11-24 (#1364).
export const UPSTREAM = 'a-schaefers/workbench' // the GitHub home; patches.json's upstream when it has one (Couplings)
// Its names before: the repo was renamed from claudes-workbench (the owner, 2026-11-24) and GitHub
// redirects the old name, so a send naming it, or a clone whose origin and patches.json still say
// it, still lands upstream and still asks first
export const FORMER_UPSTREAM = ['a-schaefers/claudes-workbench']
export const PYTHON = /^python[\d.]*$/
// what runs code it is handed as text (stdin, -c, -e); a shell's (STDIN_SHELL) is read as commands
export const RUNS_CODE = /^(?:bash|sh|zsh|dash|ksh|ash|fish|python[\d.]*|node|perl|ruby|php|deno|bun)$/
export const STDIN_SHELL = /^(?:bash|sh|zsh|dash|ksh|ash|fish)$/
// An outward command in an interpreter's code, as a string or a list (`"git push"`, `['gh', 'pr']`)
export const W = String.raw`[\s'",]+`
export const SEND_TEXT = new RegExp(String.raw`(?:\bwb|-m${W}workbench[\w.]*)${W}patch${W}send\b`, 'i')
export const OUTWARD_TEXT = new RegExp(String.raw`\bgit\b[^\n]*\bpush\b|\bgh${W}(?:pr|issue|repo|api|release|workflow|run|label)\b|` +
  SEND_TEXT.source, 'i')

// Whether an interpreter's code runs commands at all (#1364): only then can a command named in its
// text run. Calls, not words: an issue body's "the system" or "spawns" is prose.
export function spawns(cmd: string, code: string): boolean {
  if (PYTHON.test(cmd)) return /\b(?:subprocess|os\.(?:system|popen|exec\w*|spawn\w*|posix_spawn\w*)|pty\.spawn|create_subprocess_\w+)\b/.test(code)
  if (/^(?:node|deno|bun)$/.test(cmd))
    return /\bchild_process\b|\b(?:exec|execFile|spawn)(?:Sync)?\s*\(|\bDeno\.(?:run|Command)\b|\bBun\.(?:spawn\w*|\$)|\$`/.test(code)
  return /\b(?:system|exec|popen|spawn|shell_exec|passthru|proc_open)\s*[('"]|\bqx\b|\bOpen3\b|`|%x/.test(code) // perl, ruby, php
}

// The code a program that runs text is handed: an interpreter's -c/-e argument, else, when it
// names no script (none, `-`, a shell's -s), its stdin: a heredoc, a here-string, or the words and
// stdin of the command piped into it (both as one line and a word to a line: echo's and printf's).
// A script the line itself wrote by a redirect (`cat > s.sh <<EOF … EOF` then `bash s.sh`) is what
// that command printed: the old backstop caught it, and #1364's first cut let it through (its
// review, 2026-11-25). undefined: none, or a script file the mod doesn't read.
export function codeFed(words: string[], i: number, cmd: string, info: Shell): string | undefined {
  if (!RUNS_CODE.test(cmd)) return undefined
  const flag = PYTHON.test(cmd) ? /^-[a-zA-Z]*c$/ : /^(?:node|bun)$/.test(cmd) ? /^(?:-e|--eval|-p|--print)$/
    : /^(?:perl|ruby)$/.test(cmd) ? /^-[a-zA-Z]*[eE]$/ : cmd === 'php' ? /^-r$/ : null
  const args = words.slice(i + 1)
  const code: string[] = []
  let script: string | undefined
  for (let j = 0; j < args.length; j++) {
    const a = args[j]!
    if (flag?.test(a)) { if (j + 1 < args.length) code.push(args[++j]!); continue }
    if (cmd === 'deno' && a === 'eval') { code.push(args[j + 1] ?? ''); break }
    if (a.startsWith('-') && a !== '-') continue
    script = a
    break
  }
  if (code.length) return code.join('\n')
  const printed = (from: string[]) => {
    const said = from.slice(gateOf(from).at + 1)
    return `${said.join(' ')}\n${said.join('\n')}\n${info.stdin.get(from) ?? ''}`
  }
  if (script !== undefined && script !== '-' && !(STDIN_SHELL.test(cmd) && args.includes('-s'))) {
    const writer = [...info.writes].find(([, to]) => to.includes(script!))
    return writer ? printed(writer[0]) : undefined
  }
  const stdin = info.stdin.get(words)
  if (stdin !== undefined) return stdin
  const from = info.piped.get(words)
  return from ? printed(from) : undefined
}

// A line's simple commands as bash runs them, `bash -c '…'` and `eval` read inside, and what a
// program that runs text is handed (#1364): a shell's stdin read as commands too (`fed`: inside
// text another program runs), an interpreter's code kept as text (`code`). `gate`: the kernel's
// reading of its words and its redirects' files (Kernel.Gate). Shared by upstreamAim, routeCalls
// and writesTo.
export type Run = { words: string[]; fed: boolean; code?: string; gate: CommandGate }
export function runs(src: string, fed = false, info: Shell = newShell()): Run[] {
  return simpleCommands(src, info).flatMap(words => {
    const gate = gateOf(words, info.writes.get(words) ?? [])
    const run: Run = { words, fed, gate }
    const i = gate.at
    const cmd = gate.word
    const c = words.findIndex((w, j) => j > i && /^-[a-z]*c$/.test(w))
    // every shell's -c (fish's, ksh's too: the old backstop caught them; the review of #1364)
    if (STDIN_SHELL.test(cmd) && c > 0 && words[c + 1] !== undefined) return [run, ...runs(words[c + 1]!, fed, info)]
    if (cmd === 'eval') return [run, ...runs(words.slice(i + 1).join(' '), fed, info)]
    const given = codeFed(words, i, cmd, info)
    if (given === undefined) return [run]
    if (STDIN_SHELL.test(cmd)) return [run, ...runs(given, true, info)]
    return [{ ...run, code: given }]
  })
}

export const inside = (p: string, dir: string) => !!dir && (p === dir || p.startsWith(`${dir}/`))

// Whether the text names a directory: as written, through `~` or $HOME, or $WB_HOME for the state's
export function mentions(text: string, dir: string, home: string, root = ''): boolean {
  if (!dir) return false
  const ways = [dir]
  if (home && inside(dir, home) && dir !== home) {
    const rest = dir.slice(home.length)
    ways.push(`~${rest}`, `$HOME${rest}`, `\${HOME}${rest}`)
  }
  if (root && inside(dir, root) && dir !== root) {
    const rest = dir.slice(root.length)
    ways.push(`$WB_HOME${rest}`, `\${WB_HOME}${rest}`)
  }
  const esc = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  return ways.some(w => new RegExp(`${esc(w)}(?=$|[/\\s'"\`;&|)])`).test(text))
}

// A git remote's URL is the upstream's (https, ssh or scp form, with or without .git)
export function originIs(url: string, slug: string | string[]): boolean {
  const u = url.trim().toLowerCase().replace(/\/+$/, '').replace(/\.git$/, '')
  return (Array.isArray(slug) ? slug : [slug]).some(one => {
    const s = one.toLowerCase()
    return u === s || u.endsWith(`/${s}`) || u.endsWith(`:${s}`)
  })
}

// why: 'send' (wb patch send), 'dir' (it names, or runs in, a gated directory), 'plugin' (it runs
// in Claude Code's plugins directory, `at`: gated when that checkout's origin is upstream), 'slug'
// (the line names the upstream's owner/repo, run `at` that directory: a developer's own clone of
// upstream there is no user's Claude). `named`: the line names the slug; `outAt`: every directory
// an outward command in it runs in, for the hook to check by real path; `shown`: the outward text
// when it isn't a command of its own (fed to a shell)
export type Aim = { why: '' | 'send' | 'dir' | 'plugin' | 'slug'; at: string; words: string[]; named: boolean; outAt: string[]; shown?: string }

// Whether a command line sends something to workbench upstream, and why. `dirs` are the gated
// directories (the patch clone, `wb patch upstream`'s worktrees, Claude Code's workbench
// marketplace clone and cache), `plugins` Claude Code's plugins directories. The slug is matched
// in the whole line, whenever any command in it is outward: `export GH_REPO=<slug> && gh issue
// create` or `gh repo set-default <slug> && …` named it outside the outward command and went
// through (the review of #1005). `cd`, `cd --` and pushd are followed from `cwd`.
export function upstreamAim(command: string, cwd: string, home: string, slug: string | string[], dirs: string[], root = '',
  plugins: string[] = []): Aim {
  let here = cwd
  let fedHere = cwd // a fed shell's cd stays in it
  const textNames = dirs.some(d => mentions(command, d, home, root))
  const named = (Array.isArray(slug) ? slug : [slug]).some(s => command.toLowerCase().includes(s.toLowerCase()))
  const outAt: string[] = []
  const inPlugins = (ps: string[]) => ps.find(p => plugins.some(d => inside(p, d)))
  let found: Aim | null = null
  let runner = ''
  const codes: string[] = []
  for (const run of runs(command)) {
    const { words, gate: g } = run
    if (!run.fed) fedHere = here
    const cmd = g.word
    if (g.gate === 'cd') {
      const to = g.args[0]
      if (to === '-') continue
      const p = absolutePath(to ?? (home || '/'), home, run.fed ? fedHere : here)
      if (run.fed) fedHere = p
      else here = p
      continue
    }
    const now = run.fed ? fedHere : here
    if (run.code !== undefined && spawns(cmd, run.code) && OUTWARD_TEXT.test(run.code)) {
      runner ||= now
      codes.push(run.code)
    }
    // wb patch send, or another outward form (Kernel.Gate); wb patch zip only writes a file the
    // user may email themselves: nothing outward (#1291)
    if (g.gate !== 'send' && g.gate !== 'outward') continue
    // fed to a shell: it runs, but the dialog shows the line, not this command of its own
    const own = run.fed ? { words: [] as string[], shown: words.map(quoted).join(' ') } : { words }
    if (g.gate === 'send') return { why: 'send', at: now, named, outAt, ...own }
    const at = [now, ...g.dirs.map(d => absolutePath(d, home, now))]
    outAt.push(...at)
    if (textNames || at.some(p => dirs.some(d => inside(p, d)))) return { why: 'dir', at: now, named, outAt, ...own }
    const plugin = inPlugins(at)
    if (!found && plugin) found = { why: 'plugin', at: plugin, named, outAt, ...own }
    if (!found && named) found = { why: 'slug', at: now, named, outAt, ...own }
  }
  if (found) return { ...found, outAt }
  // the backstop: an interpreter's code that runs commands and names an outward one
  if (runner) {
    outAt.push(runner)
    const code = codes.join('\n')
    const shown = code.split('\n').filter(l => OUTWARD_TEXT.test(l)).join('\n')
    const aim = (why: Aim['why'], at = runner): Aim => ({ why, at, words: [], named, outAt, shown })
    if (SEND_TEXT.test(code)) return aim('send')
    if (textNames || dirs.some(d => inside(runner, d))) return aim('dir')
    if (inPlugins([runner])) return aim('plugin')
    if (named) return aim('slug')
  }
  return { why: '', at: '', words: [], named, outAt }
}

export const SHOW_CHARS = 600

export const MCP_CHARS = 4000
// An MCP tool's input as the dialog shows it: up to MCP_CHARS, the rest counted
export function shownInput(input: Record<string, unknown>): string {
  const text = JSON.stringify(input, null, 1)
  return text.length > MCP_CHARS ? `${text.slice(0, MCP_CHARS)}… (${text.length - MCP_CHARS} more characters not shown)` : text
}
export const quoted = (w: string) => /^[\w@%+=:,./-]+$/.test(w) ? w : `'${w.replace(/'/g, `'\\''`)}'`

// The command as the dialog shows it: the whole line when short; else the outward command in
// full, then the line's start. Cut from the line's start, a long line hid the gh command and
// its inline --body entirely (the review of #1005)
export function shownCommand(command: string, aim: Aim): string {
  if (command.length <= SHOW_CHARS) return command
  const own = aim.shown ?? aim.words.map(quoted).join(' ')
  return `${own}\n\n(in a longer line: ${command.slice(0, SHOW_CHARS)}…)`
}

// The GitHub MCP tools (the github plugin): a call that isn't a read and names the upstream, or a
// repository of its name (a fork's), asks the same; Bash was the only gate, and patching.md
// offers that plugin (the review of #1005)
export const GITHUB_MCP = /^mcp__.*github.*__(.+)$/i
export function mcpAimsUpstream(tool: string, input: Record<string, unknown>, slug: string | string[]): boolean {
  const m = GITHUB_MCP.exec(tool)
  return !!m && githubAimsUpstream(m[1]!, input, slug)
}

// A GitHub tool by its own name (Claude Code's `mcp__<server>__<name>` and OpenCode's
// `<server>_<name>` alike, the server's part taken off): not a read, and its input names the
// upstream or a repository of its name
export function githubAimsUpstream(name: string, input: Record<string, unknown>, slug: string | string[]): boolean {
  if (/^(?:get|list|search)_/i.test(name)) return false
  const repo = String(input.repo ?? input.repository ?? '').toLowerCase()
  return (Array.isArray(slug) ? slug : [slug]).some(one => {
    const s = one.toLowerCase()
    const name = s.split('/')[1] ?? s
    return repo === s || repo === name || repo.endsWith(`/${name}`) || JSON.stringify(input).toLowerCase().includes(s)
  })
}

// A path variable as wb reads it (store.absolute_env: os.path.abspath(os.path.expanduser(v))):
// `~` expanded, relative to the directory wb runs in (the session's, where $.process.run starts
// it), `.`, `..` and repeated or trailing slashes gone. settings.json's `env` passes WB_HOME
// literally, and the mod took `~/state/wb` as given: the pane stayed empty and no wake came; a
// trailing slash made `…/workbench//tasks/`, and the Claude-coding guard never matched (#454).
export function absolutePath(v: string, home: string, cwd: string): string {
  let p = v === '~' ? home : v.startsWith('~/') ? `${home}${v.slice(1)}` : v
  if (!p.startsWith('/')) p = `${cwd}/${p}`
  const out: string[] = []
  for (const part of p.split('/')) {
    if (part === '..') out.pop()
    else if (part && part !== '.') out.push(part)
  }
  return `/${out.join('/')}`
}

// The gated directories and the upstream's names, from what the host read (register.tsx's
// gateConf, the OpenCode plugin's): the patch clone (patches.json's `source`, else WB_SOURCE, else
// ~/.local/share/workbench/source), `$WB_HOME/upstream/`, and each plugins directory's workbench
// marketplace clone and cache (Claude Code's `<config>/plugins` and its plugin cache; `plugins`
// adds a host's own, OpenCode's package caches, gated as a plugins directory is: by origin)
export type Where = { slug: string; slugs: string[]; dirs: string[]; plugins: string[] }
export function gatedWhere(o: { patches: Record<string, unknown>; home: string; cwd: string; root: string; source?: string;
  claudeConfig?: string; pluginCache?: string; plugins?: string[] }): Where {
  const { patches: conf, home, cwd, root } = o
  const given = typeof conf.source === 'string' && conf.source ? conf.source
    : o.source || `${home}/.local/share/workbench/source`
  const source = absolutePath(given, home, cwd)
  const slug = typeof conf.upstream === 'string' && conf.upstream.trim() ? conf.upstream.trim() : UPSTREAM
  const cc = o.claudeConfig || `${home}/.claude`
  const claude = [`${absolutePath(cc, home, cwd)}/plugins`, ...(o.pluginCache ? [absolutePath(o.pluginCache, home, cwd)] : [])]
  const dirs = [source, `${root}/upstream`, ...claude.flatMap(p => [`${p}/marketplaces/workbench`, `${p}/cache/workbench`])]
  // every name upstream answers to: the one in use and its former ones (FORMER_UPSTREAM)
  const slugs = [...new Set([slug, UPSTREAM, ...FORMER_UPSTREAM].map(x => x.toLowerCase()))]
  return { slug, slugs, dirs, plugins: [...claude, ...(o.plugins ?? []).map(p => absolutePath(p, home, cwd))] }
}
