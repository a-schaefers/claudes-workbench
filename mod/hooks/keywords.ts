// Workbench's own keywords (#1278) in what the user typed, for both hosts: the Claude Code mod
// (register.tsx) and the OpenCode plugin (opencode-plugin/deep.ts). `deepcode` anywhere, as a
// whole word, asks for one request as a deep workflow; `deepmode on` or `deepmode off`, a line of
// its own, sets the standing choice (`wb deepmode`), the user's own yes. Text inside backticks or a
// fence is talk about the word, never the word: the user can ask what `deepcode` does. Claude's own
// `ultracode` is never read here. runs.keywords is the reference and tests/fixtures/keywords.json
// holds both to it (#1278's plan put this in the kernel; kernel/BOUNDARY.md keeps parsing text out).
// Pure, and nothing runs at import.

export interface Keywords {
  readonly deepcode: boolean
  /** true for a `deepmode on` line, false for `deepmode off` (the last such line wins), null for none */
  readonly deepmode: boolean | null
}

// a fence to its close or the end of the text, then an inline span on one line (runs.FENCE)
const FENCE = /```[\s\S]*?(?:```|$)|`[^`\n]*`/g
// a letter, digit, underscore or hyphen on either side makes it another word (deepcoder, deep-code,
// deepcode-ish), as Python's \w does with Unicode (runs.DEEPCODE)
const DEEPCODE = /(?<![\p{L}\p{N}_-])deepcode(?![\p{L}\p{N}_-])/iu
// a line of its own as Python's re.M reads one, which ends only at \n (JavaScript's m flag ends one
// at \r and \u2028 too), its spaces str.isspace's (\x1c-\x1f and \x85 in, \ufeff out): the line's
// start is the text's or a \n, its end a \n or the text's (runs.DEEPMODE_LINE). With these, a
// seeded run of runs.keywords against this agreed on 300,000 texts (200,000 at random of the words,
// spaces, line ends, backticks and letters past ASCII, 100,000 built around toggle lines; #1325),
// where JavaScript's own ^, $ and \s disagreed on 26 of the first 50,000.
const WS = '[\\t\\n\\v\\f\\r \\x1c-\\x1f\\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000]'
const DEEPMODE_LINE = new RegExp(`(?:^|\\n)${WS}*deepmode${WS}+(on|off)${WS}*[.!]?${WS}*(?=\\n|$)`, 'gi')

export function keywords(text: string): Keywords {
  const bare = String(text ?? '').replace(FENCE, ' ')
  const toggles = [...bare.matchAll(DEEPMODE_LINE)]
  const last = toggles.length ? String(toggles[toggles.length - 1]?.[1] ?? '').toLowerCase() : null
  return { deepcode: DEEPCODE.test(bare), deepmode: last === null ? null : last === 'on' }
}

// Whether the text is nothing but deepmode lines: nothing else for the model, so the host answers
// it itself and starts no turn
export function onlyToggles(text: string): boolean {
  return keywords(text).deepmode !== null && !String(text ?? '').replace(DEEPMODE_LINE, '').trim()
}
