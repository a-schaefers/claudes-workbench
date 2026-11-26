// The keywords (#1278): Workbench's own words in what the user wrote, read the same way on both
// hosts: the Claude Code mod (a prompt typed in the terminal, a web chat message) and the OpenCode
// plugin (opencode-plugin/keywords.ts). wb's `runs.keywords` is the reference, and
// tests/fixtures/keywords.json holds all three to it. Reading a prompt is parsing text, which never
// goes in the kernel (kernel/BOUNDARY.md, *Rulings*), so it is this one TypeScript module that both
// hosts import, as they do the splash and the Tasks view. It imports nothing and runs nothing at
// import time.
// - `deepcode`, a whole word outside backticks and fences: this one request is a deep workflow.
//   The host adds wb's rule (`wb deepmode --rule`, runs.DEEPCODE_RULE) where only the model reads
//   it. It is never Claude's `ultracode`, and it never turns that word on.
// - `deepmode on` or `deepmode off`, a line of its own (the last such line wins): the standing
//   setting. The user's own line is their yes, so the host runs `wb deepmode on|off` and tells the
//   model it is done (TOGGLED). The model then hears the change as for any other change of the
//   setting.

export interface Keywords {
  readonly deepcode: boolean
  /** the last `deepmode on|off` line: true for on, false for off; null for none */
  readonly deepmode: boolean | null
}

// runs.FENCE, runs.DEEPCODE and runs.DEEPMODE_LINE, as JavaScript spells them, so that a seeded
// run of Python against this agreed on 300,000 texts (200,000 of the words, spaces, line ends,
// backticks and letters past ASCII at random, 100,000 built around toggle lines; #1325). Python's \w is Unicode: [\p{L}\p{N}_]. Its \s is str.isspace's, not
// JavaScript's (\x1c-\x1f and \x85 in, \ufeff out): WS. Its re.M ends a line only at \n, never at
// \r or \u2028, so a line's start is the text's or a \n, and its end a \n or the text's. There is
// no lookbehind, since a host's regex engine may lack it: the character before is matched instead.
// Python's \Z is `$` without the m flag.
const WS = '[\\t\\n\\v\\f\\r \\x1c-\\x1f\\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000]'
const FENCE = /```[\s\S]*?(?:```|$)|`[^`\n]*`/g
const DEEPCODE = /(?:^|[^\p{L}\p{N}_-])deepcode(?![\p{L}\p{N}_-])/iu
const DEEPMODE_LINE = new RegExp(`(?:^|\\n)${WS}*deepmode${WS}+(on|off)${WS}*[.!]?${WS}*(?=\\n|$)`, 'gi')

export function keywords(text: unknown): Keywords {
  const bare = (typeof text === 'string' ? text : '').replace(FENCE, ' ')
  const toggles = [...bare.matchAll(DEEPMODE_LINE)]
  const last = toggles.length ? toggles[toggles.length - 1]![1]!.toLowerCase() : ''
  return { deepcode: DEEPCODE.test(bare), deepmode: last ? last === 'on' : null }
}

// The wb command a toggle line runs, and what the model is told once it ran: wb's own line, so
// the model doesn't run it a second time
export const toggleArgs = (on: boolean): string[] => ['deepmode', on ? 'on' : 'off']
export const TOGGLED = (on: boolean, out: string): string =>
  `The user's line \`deepmode ${on ? 'on' : 'off'}\` was carried out (wb deepmode ${on ? 'on' : 'off'}): ${out}`
// What the rule is read with: wb prints runs.DEEPCODE_RULE, and each host reads it once a load
export const RULE_ARGS: readonly string[] = ['deepmode', '--rule']
