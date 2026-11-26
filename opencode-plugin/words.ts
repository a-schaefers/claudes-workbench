// One simple command's words and the files its redirects write, from the raw text OpenCode's own
// shell scan hands the permission hook (#1314, 2026-11-25). OpenCode splits a command line into its
// simple commands (core/src/shell/parse.ts `scan`: each one's `resource`, its raw text), and the
// permission `evaluate` hook sees only those texts, never the scan's words; so this undoes one
// text's quoting, as the kernel's gate classifier takes it (kernel/BOUNDARY.md, *The gate
// classifier*: words, quotes undone, and the files its redirects write). A heredoc's body is never
// words (#1364): the words stop at the line its `<<` is on. A `$` or a backquote stays as written,
// a word only the shell knows (`dynamic`). Pure, and nothing runs at import.

export interface Simple {
  readonly words: string[]
  readonly writes: string[]
  readonly dynamic: boolean
}

const SPACE = new Set([' ', '\t', '\n', '\r'])
// a list or a group ends the simple command: OpenCode's scan hands each one apart, so one here is
// the rest of an opaque line, never this command's words
const ENDS = new Set([';', '|', '&', '(', ')'])

// the redirect operators, longest first: `>`, `>>`, `>|`, `&>`, `&>>` and `<>` write their target;
// `>&` writes it unless it names a descriptor (`2>&1`, `>&-`)
const OPS = ['&>>', '&>', '<<<', '<<-', '<<', '<>', '<&', '>&', '>|', '>>', '>', '<']
const WRITING = new Set(['>', '>>', '>|', '&>', '&>>', '<>'])

export function simpleWords(text: string): Simple {
  const words: string[] = []
  const writes: string[] = []
  let dynamic = false
  let i = 0
  let heredoc = false // a `<<` was on this line: its body starts at the next line, never words

  // one word from i, quotes undone; null at the end or at an operator
  const word = (): string | null => {
    let out = ''
    let any = false
    while (i < text.length) {
      const c = text[i]!
      if (SPACE.has(c) || ENDS.has(c) || c === '<' || c === '>') break
      if (c === '\\') {
        if (text[i + 1] === '\n') { i += 2; continue } // a line continuation
        if (i + 1 < text.length) out += text[i + 1]
        i += 2
        any = true
        continue
      }
      if (c === "'") {
        const end = text.indexOf("'", i + 1)
        const stop = end < 0 ? text.length : end
        out += text.slice(i + 1, stop)
        i = stop + 1
        any = true
        continue
      }
      if (c === '$' && text[i + 1] === "'") { // ANSI-C quoting: its escapes are the shell's to read
        const end = text.indexOf("'", i + 2)
        const stop = end < 0 ? text.length : end
        const body = text.slice(i + 2, stop)
        if (body.includes('\\')) dynamic = true
        out += body
        i = stop + 1
        any = true
        continue
      }
      if (c === '"') {
        i++
        while (i < text.length && text[i] !== '"') {
          const d = text[i]!
          if (d === '\\' && '$`"\\\n'.includes(text[i + 1] ?? '')) {
            if (text[i + 1] !== '\n') out += text[i + 1]
            i += 2
            continue
          }
          if (d === '$' || d === '`') dynamic = true
          out += d
          i++
        }
        i++
        any = true
        continue
      }
      if (c === '$' || c === '`') dynamic = true
      out += c
      i++
      any = true
    }
    return any ? out : null
  }

  while (i < text.length) {
    const c = text[i]!
    if (SPACE.has(c)) {
      if (c === '\n' && heredoc) break
      i++
      continue
    }
    if (ENDS.has(c) && !(c === '&' && text[i + 1] === '>')) break
    if (c === '#') { // a comment, to the line's end
      const nl = text.indexOf('\n', i)
      if (nl < 0 || heredoc) break
      i = nl
      continue
    }
    const op = OPS.find(o => text.startsWith(o, i))
    if (op) {
      i += op.length
      while (i < text.length && (text[i] === ' ' || text[i] === '\t')) i++
      const to = word()
      if (op === '<<' || op === '<<-') heredoc = true
      else if (to !== null && (WRITING.has(op) || (op === '>&' && !/^(?:\d+|-)$/.test(to)))) writes.push(to)
      continue
    }
    const start = i
    const w = word()
    if (w === null) { i = Math.max(i, start + 1); continue }
    // `2>file`: a descriptor's number is the redirect's, not a word
    if (/^\d+$/.test(w) && (text[i] === '>' || text[i] === '<') && i === start + w.length) continue
    words.push(w)
  }
  return { words, writes, dynamic }
}
