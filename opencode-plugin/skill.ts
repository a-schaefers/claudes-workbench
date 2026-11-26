// The /workbench procedure on OpenCode (#1335): one source, skill/workbench/ (SKILL.md and
// reference/*.md), two renderings. Claude Code's copy is SKILL.md itself, byte for byte, linked
// by `wb setup` (`skillFor('claude-code', …)` is the identity: Claude's path doesn't change by a
// word, #1296's open question 1). OpenCode's is generated from it here, when the plugin starts:
// the host lines (`HOST_LINES`) say what each Claude Code line becomes on OpenCode, so
// SKILL.md stays the one text anyone edits. The procedure's core is the Workbench agent's prompt
// (agent.ts) and the `workbench` skill; each reference part is a skill of its own,
// `workbench-<name>`, loaded through the skill tool when the core says to (the host-shape
// decision, #1296: "the procedure's core becomes the agent's prompt, reference parts load through
// skill.transform"), so a small driver's window holds only what it needs.
//
// A host line whose Claude Code text no longer matches fails `opencode-plugin/test/skill.test.ts`
// by name: a reword of SKILL.md changes its line here in the same commit (ARCHITECTURE.md,
// *Couplings*). Pure but for `readSkill`: nothing runs at import.
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join } from 'node:path'

export type Host = 'claude-code' | 'opencode'

// What OpenCode's rendering names that SKILL.md finds through Claude Code's skill link
export interface Where {
  readonly root: string      // workbench's code: the directory holding skill/ and bin/
  readonly wb: string        // the wb the plugin runs
  readonly onPath: boolean   // that wb was found by name on OpenCode's PATH, so a shell's `wb` is it
  readonly docs: boolean     // root holds ARCHITECTURE.md (a clone; OpenCode's Git install doesn't)
  readonly wakes: boolean    // the plugin delivers `[workbench]` wakes: its options' `wake` isn't "off" (notices.ts)
}

export const SKILL_ID = 'workbench'
export const partId = (file: string): string => `workbench-${file.replace(/^reference\//, '').replace(/\.md$/, '')}`

export interface Rule {
  readonly from: RegExp
  readonly to: string | ((w: Where) => string)
  readonly files: readonly string[] | '*'   // the source files it applies to ('*': every one)
}

// A phrase as it is written, matched across SKILL.md's line breaks: each run of spaces in it
// matches any whitespace, newlines and indents included
const phrase = (s: string): RegExp =>
  new RegExp(s.trim().split(/\s+/).map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('\\s+'), 'g')
export const line = (from: string, to: string | ((w: Where) => string), files: readonly string[] | '*' = ['SKILL.md']): Rule =>
  ({ from: phrase(from), to, files })

const SKILL = ['SKILL.md']
const CC_LINK = '`readlink -f "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills/workbench"`'

// Where the user types a command that asks them something (sudo's password, gh's login). Claude
// Code's `!` runs it in the user's terminal; OpenCode's `!` (shell mode, typed at the prompt's
// start) runs it with no terminal: a pipe, stdin closed, under a daemon in a session of its own
// (2.4.1's process-daemon, service/pipe/unix.rs and client.rs), so nothing can ask. Its terminal
// pane (`terminal.toggle`, `<leader>t`, the leader ctrl+x by default) is a real terminal, and wb
// there is the user's: no OPENCODE_SESSION_ID (#1321, finding 17; patches.PANE_OR_TERMINAL, wb's
// own hints, says the same)
export const PANE_OR_TERMINAL = "in OpenCode's terminal pane (ctrl+x t) or a terminal of their own"
export const BANG = "OpenCode's `!` runs a command with no terminal"

const cli = (w: Where): string => (w.onPath
  ? '`wb` is the workbench CLI (on PATH as `wb`; `wb --help`).'
  : `\`wb\` is the workbench CLI, \`${w.wb}\` (run it by that path wherever this says \`wb\` and your shell's PATH has none; \`--help\`).`)
  + (w.docs
    ? ` Its code, ARCHITECTURE.md included, is at \`${w.root}\`.`
    : ` Its code is at \`${w.root}\`; ARCHITECTURE.md, its design, is in a clone of github.com/a-schaefers/workbench (OpenCode's install leaves it out).`)

// The procedure's everyday commands are typed tools on OpenCode (tools.ts, consent.ts): said once
// after the wb line, and at the recipes below
export const TOOLS_NOTE = "Its everyday commands are typed tools here, each that `wb` command run as this session in its directory, its "
  + 'fields the flags by name: `workbench_start`, `workbench_status`, `workbench_diff`, `workbench_feedback` (the whole text in '
  + '`text`), `workbench_accept` and `workbench_discard`: use them wherever this says `wb start`, `wb status`, `wb diff`, '
  + "`wb feedback`, `wb accept` or `wb discard` (a flag is the field of its name; one start's fields lack goes in its `args`), "
  + 'and the shell for the rest of `wb`. `workbench_wait` holds the turn until a task needs you, for when no wake will come; '
  + "a wait in the background stays the shell's `wb wait` with `background: true`. Four changes are the user's, through the tools that ask them "
  + 'in a form first, never the shell, which refuses them: an API role (`workbench_route`), a start on an API model '
  + "(`workbench_start`), a Settings row a role needs (`workbench_settings_set`) and a patch sent upstream (`workbench_patch_send`). "
  + 'A `[web chat]` message is answered with `workbench_chat_reply`.'

// The wake paragraph: the plugin's `[workbench]` wakes (notices.ts, #1313), or with its `wake`
// option off none, so a wait only when the user asks for one
const wakes = (w: Where): string => (w.wakes
  ? "**Don't poll.** The plugin wakes this session with a `[workbench]` message when the task needs you (review, question, "
    + "stuck, failed): end your turn and wait for it; an agent's question can't wait, so it reaches you mid-turn too: answer it then."
  : "**Don't poll.** Wakes are off (the plugin's `wake` option): no `[workbench]` message will come. Tell the user it runs, "
    + 'and wait (`workbench_wait`, which returns when the task needs you) only if they ask.')

// Claude Code's lines and what each says on OpenCode. The exact lines first, then the ones that
// recur. Each must match its file (the test fails by name when one doesn't).
export const HOST_LINES: readonly Rule[] = [
  // the frontmatter: the reader is the driver, whichever model the user picked in /models
  {
    from: /description: >\n[\s\S]*?(?=\ndisable-model-invocation:)/,
    to: 'description: >\n  Workbench mode: you hand the coding of each request to OpenCode agents (on this\n'
      + "  machine's GPU, or an API model the user chose for the task's role) via the workbench `wb` CLI;\n"
      + '  you write the spec, review the diff, and accept or send feedback, so you spend tokens on specs\n'
      + "  and review instead of code generation. The Workbench agent's own instructions: /workbench\n"
      + '  switches to it.',
    files: SKILL,
  },
  line('# /workbench — an OpenCode agent writes, Claude reviews', '# /workbench — an OpenCode agent writes, you review'),
  line("The user's Claude budget is tight; that is the point of this mode.",
    "Your tokens are the user's budget, and it is tight; that is the point of this mode."),
  // the mode is the Workbench agent (the host-shape decision's "agent switch"), its prompt this text
  line('`/workbench` turns workbench mode on for the rest of this session;',
    'Workbench mode is the Workbench agent: `/workbench` switches this session to it;'),
  line('`/workbench-quit-and-hand-back-claude`. Running `/workbench` again',
    '`/workbench-quit-and-hand-back`, or switch to another agent. Running `/workbench` again'),
  line('`$ARGUMENTS`. Empty? With a `workbench-firstrun` note after this skill',
    'what the user typed after `/workbench`, if anything. Nothing? With a `workbench-firstrun` note'),
  // `wb coding`'s rows say Claude, the driver on Claude Code's path: `decide` is the key either way
  line('**Claude coding**', '**Your coding**'),
  line('"Let Claude decide" (the default) is this skill as written.', '`decide` (the default) is these instructions as written.'),
  // a rule's change is OpenCode's own update of its instruction entry (upkeep.ts, #1315), never a row
  line('a later change comes as a `[workbench]` note', 'a later change comes as an update of that context block'),
  line('a `workbench-mode` context block says the mode is still on and where this file is: read it again if its procedure is no longer in your context.',
    "these instructions are still yours: they are the Workbench agent's, sent while it is selected."),
  // plan mode is Claude Code's; OpenCode's Plan agent is another agent, without these instructions
  line('**Plans are handoff plans**, in plan mode or not:', '**Plans are handoff plans**:'),
  line('That holds in plan mode, which is read-only: read the code, `wb models` and `wb status` if you can, and start nothing until the plan is approved;',
    'That holds when the user wants the plan first: read the code, `wb models` and `wb status`, and start nothing until they approve it;'),
  line('`wb` is the workbench CLI (on PATH as `wb`; `wb --help`). Its code, ARCHITECTURE.md included, is two directories above ' + CC_LINK + '.', cli),
  line("don't go digging in raw logs unless a report says to.", "don't go digging in raw logs unless a report says to. " + TOOLS_NOTE),
  // the recipes: the tools by their fields (tools.ts; start's in consent.ts)
  { from: /```\nwb start <kebab-name> --spec[\s\S]*?```\n`--auto-fix` defaults to 2 on the GPU and 1 on API\./, files: SKILL,
    to: '`workbench_start`: `name` (kebab-case), `spec` (its whole text, or `spec_file`), `repo`, `role` (significant, trivial '
      + 'or writing) and `test` (the acceptance command); as needed `skill`, `model`, `auto_fix` (default 2 on the GPU, 1 on API), '
      + "`setup` and `image`; any other flag below in `args`, as on wb start's command line (`[\"--copy\", \"node_modules\"]`)." },
  line('Numbered, concrete changes in a file → `wb feedback <name> --message <file>` (or `--text "…"`).',
    'Numbered, concrete changes → `workbench_feedback` with the `task` and its whole `text`.'),
  line('Approved: `wb accept <name> -m "<conventional commit subject>"` commits',
    'Approved: `workbench_accept` with the `task` and its `message`, a conventional commit subject, commits'),
  line('`wb discard <name> --reason "…"` drops it', '`workbench_discard` with the `task` and its `reason` drops it'),
  // the gated changes: the tools that ask the user in a form (consent.ts, #1314)
  line("(the mod's dialog then asks them, and a \"not changed\" means no)", '(`workbench_start` then asks them in a form, and a "not changed" means no)'),
  line('change, run it as a command of its own (`wb route trivial', "change, run it through `workbench_route`, a call of its own whose `args` are wb route's words (`wb route trivial"),
  line('the mod shows them the change in a dialog', 'it shows them the change in a form'),
  line('`wb settings set <key> <value>` applies it as the web app does', '`workbench_settings_set` with the `key` and the `value` applies it as the web app does'),
  line("A role moved to API still goes through the mod's dialog, as `wb route` does:", 'A role moved to API still asks them in its form, as `workbench_route` does:'),
  line('then the one `wb patch send` it printed, as a command of its own, `--title` and `--body-file` written out. The mod asks the user to approve that send in a dialog,',
    "then the `wb patch send` it printed, through `workbench_patch_send` (`slug`, `title`, `body_file`, `draft` if they want one), never the shell. It asks the user to approve that send in a form,"),
  line('Then run the printed `wb patch send <slug> --title "…" --body-file <f>` (`--draft` if they want one); the mod\'s dialog shows the title and the body\'s start, and the user approves it there. Run it as a command of its own, the flags spelled out (`--title`, `--body-file`, never shortened, never a `$VAR`): the dialog refuses what it can\'t show. If wb says the dialog approved none (the mod isn\'t loaded), the user runs that command in a terminal of their own.',
    "Then send what it printed through `workbench_patch_send` (`slug`, `title`, `body_file`, `draft` if they want one), never the shell; its form shows the title and the body's start, and the user approves it there. It refuses what it can't show. If it says no window was open to ask in, the user runs that command in a terminal of their own.", ['reference/patching.md']),
  // agents read OpenCode's global AGENTS.md (context.py, the project-config note), as the driver does
  line("It never gets the user's `~/.claude/CLAUDE.md`, its imports or `~/.claude/rules`: those are yours. When you will review against one of those global rules,",
    "It never gets the user's `~/.claude/CLAUDE.md`, its imports or `~/.claude/rules`; it does get OpenCode's global `~/.config/opencode/AGENTS.md`, as you do. When you will review against a global rule it doesn't get,"),
  line('one whose Claude Code process has exited,', 'one whose session is gone,'),
  // a scheduled run is Claude Code headless (`wb schedule`): never this agent
  line(', and a run of yours that is headless (`claude -p`, nobody at the terminal).', '.'),
  line("Claude Code's dialogs in that turn", "OpenCode's prompts in that turn"),
  line('(a plan under ~/.claude, another repo)', '(a plan file of yours, another repo)'),
  // deep workflows (#1278): `ultracode` is Claude Code's keyword, and its run waits in the background
  line("`ultracode` is Claude's own", "`ultracode` is Claude Code's"),
  // a Claude Code Workflow beside a flow: OpenCode's driver has no Workflow tool
  { from: /With `ultracode` and `deepcode` in one prompt,[\s\S]*?never redoes it\.\n/, to: '', files: SKILL },
  line('run in the background (Bash `run_in_background`)', "run in the background (the shell tool's `background: true`)"),
  // wakes: the plugin's `[workbench]` messages (notices.ts, #1313), the mod's branch; no "without
  // the mod" branch: this prompt is the plugin's, so it is always there. With its `wake` option
  // off, none comes (`wakes`)
  line("**Don't poll.** With the workbench mod loaded the session is woken by a `[workbench]` prompt when the task needs you (review, question, stuck, failed) — end your turn and wait for it; an agent's question can't wait, so it reaches you mid-turn too: answer it then. Without the mod, run `wb wait <name>` with Bash `run_in_background: true` and act on its notification. If the context says wakes are off (the plugin's wake setting), none will come: tell the user it runs, and wait only if they ask.",
    wakes),
  // the user's two commands are the plugin's server commands (commands.ts, #1315): a quit leaves
  // the Workbench agent for Build, which closes the panel (pane.tsx, #1312), the mode's end here
  line('the mod runs them, closes the pane and tells you what it did:',
    'the plugin runs them, switches this session to the Build agent (which closes the Workbench panel) and tells you what it did:'),
  // they are the plugin's own commands wherever OpenCode runs this prompt, which only the plugin
  // gives: never words (the mod's absent branch has no OpenCode case)
  { from: /\nWithout the mod, the user may ask for the same in words; run those commands then\./, to: '', files: SKILL },
  line('once (a marketplace install can\'t keep one: Claude Code re-clones it)',
    "once (OpenCode's own install of workbench can't keep one: wb patch names the clone to make and its `bin/wb setup --opencode`)"),
  line('the login is theirs (`! gh auth login`)', `the login is theirs, typed ${PANE_OR_TERMINAL} (\`gh auth login\`; ${BANG})`),

  // reference/driving.md
  line("(`~/.claude/CLAUDE.md`, `wb notes`, the repo's CLAUDE.md)", "(`~/.config/opencode/AGENTS.md`, `wb notes`, the repo's CLAUDE.md)", ['reference/driving.md']),
  line('offer to add it to `~/.claude/CLAUDE.md`, which every session reads', 'offer to add it to `~/.config/opencode/AGENTS.md`, which every OpenCode session reads', ['reference/driving.md']),
  line("a browser tool (Claude in Chrome, the desktop app's browser, a browser MCP server)", 'a browser tool (a browser MCP server, say)', ['reference/driving.md']),
  line("(settings: SKILL.md, *Settings are the user's*)", "(settings: your Workbench instructions, *Settings are the user's*)", ['reference/driving.md']),
  line('Your prompts (a permission, an AskUserQuestion question, a plan, an MCP server\'s form)', "Your prompts (a permission, a question, an MCP server's form)", ['reference/driving.md']),
  line('the chat shows one line, "Claude is waiting for you in the terminal"', 'the chat shows one line, that you wait for them in the terminal', ['reference/driving.md']),
  line("While they're away, AskUserQuestion and ExitPlanMode are refused (the mod does it):", "While they're away, ask nothing with the question tool:", ['reference/driving.md']),
  line('a question (AskUserQuestion) or a plan (ExitPlanMode) is refused instead, so ask in the chat (`wb chat reply`) and end your turn',
    'a question or a plan goes in the chat instead (`wb chat reply`), and you end your turn', ['reference/driving.md']),
  line("and each run spends Claude's turns", 'and each run starts Claude Code headless (`claude -p`), so it needs Claude Code and spends its turns', ['reference/driving.md']),
  { from: /\n## Headless: a scheduled run\n[\s\S]*?(?=\n## )/, to: '', files: ['reference/driving.md'] },

  // the suites wb patch runs: the mod's are Claude Code's mod/ tests, never this plugin's
  line("(claude for the mod's,", "(claude for the Claude Code mod's,", ['reference/patching.md']),
  line("(the mod's, the kernel's)", "(the Claude Code mod's, the kernel's)", ['reference/patching.md']),
  line("(each directory's: the mod's,", "(each directory's: the Claude Code mod's,", ['reference/patching.md']),

  // reference/models.md
  // OpenCode's `!` can't prompt (`BANG`): sudo's password goes to a terminal
  line("with Claude Code's `!` prefix or in a terminal of their own if sudo can't ask there",
    `${PANE_OR_TERMINAL} (${BANG}, so sudo couldn't ask)`, ['reference/models.md']),
  line('Claude sets it up on request;', 'You set it up on request;', ['reference/models.md']),
  line('tell the user to restart Claude Code or run `/reload-plugins` after)',
    "OpenCode reloads the plugin by itself after it; an install OpenCode made from Git it updates itself, as wb update says)", ['reference/models.md']),

  // reference/patching.md: Claude Code's facts, OpenCode's here (#1321, finding 7)
  // the clone a patch lives in: OpenCode's own install can't keep one, and `wb patch` refuses there
  // (cli.cmd_patch); wb setup --opencode keeps a Git entry's source, so the plugin's own code stays
  // that install's until the entry names the clone (ocplugin.install)
  { from: /1\. Once, on a marketplace install: `wb patch init`[\s\S]*?`--track` gets it from `wb update` or `wb patch init`\)\./,
    to: '1. Once, a clone the user owns: patches are commits on its branch `local`. OpenCode\'s own install\n'
      + "   of workbench (from Git, in OpenCode's package cache) can't keep one, as OpenCode replaces it\n"
      + '   when it updates: `wb patch` refuses there and prints the two commands that make one, a `git\n'
      + "   clone` and that clone's `bin/wb setup --opencode`; run them on the user's yes. The plugin then\n"
      + "   runs the clone's `wb`, and its own code stays OpenCode's install while OpenCode's `plugins`\n"
      + '   entry names that install (`wb doctor` says which copy loads, from which file): for a patch\n'
      + "   under `opencode-plugin/`, that entry comes out of OpenCode's config (on the user's yes) and\n"
      + "   the clone's `bin/wb setup --opencode` runs again, which loads the clone's. Run from Claude\n"
      + "   Code's marketplace install (a machine with both), `wb patch init` (on the user's yes; it\n"
      + '   refuses while tasks run) moves it to a full clone, `~/.local/share/workbench/source` or\n'
      + "   `--dir`/`WB_SOURCE`, OpenCode's plugin too when it ran from there, and the user runs\n"
      + '   `/reload-plugins` in Claude Code. On a clone the user already owns, `wb patch init` says so:\n'
      + '   work on branch `local` there (`git -C <root> switch -c local --track origin/main`; one made\n'
      + '   without `--track` gets it from `wb update` or `wb patch init`).',
    files: ['reference/patching.md'] },
  line('copies the committed HEAD into the plugin cache), then `/reload-plugins`.',
    "copies the committed HEAD into the plugin cache), then `/reload-plugins`; one under `opencode-plugin/` reaches OpenCode through `wb update` too while OpenCode loads the plugin from this clone (step 1): a copy of what is committed, which it reloads by itself.", ['reference/patching.md']),
  // the plugin's forms hold every send upstream, as the mod's dialog does: wb patch send's
  // (consent.ts), and a gh write, a git push or a GitHub MCP tool's write (upstream.ts, #1296's
  // row 14); while they are away the form waits (no NOT_SENT_AWAY)
  { from: /The mod holds every send to\s+workbench upstream[\s\S]*?in the terminal for their answer\./,
    to: 'The plugin holds\nevery send to workbench upstream (an issue, a comment, a fork, a push, a pull request) '
      + "until\nthe user approves that one call in a form; a yes is never remembered. A pull request goes only\n"
      + 'through `workbench_patch_send`, whose form shows its title and body, and wb refuses a send its\nform '
      + "didn't approve, so never send one with gh, git or a GitHub MCP tool yourself (those ask in\nthe same "
      + 'form, as an issue or a comment through gh in step 1 does). Show them what goes public\nfirst, so the '
      + 'form only confirms what they have read. A "Don\'t send" is a no: tell them it\nwasn\'t sent and leave '
      + 'it, unless they bring it up. If they are away on the web chat, tell them\nthere what it would send: '
      + 'the form waits in the terminal for their answer.',
    files: ['reference/patching.md'] },
  // the marketplace clone's rescue is Claude Code's, met here only with both installed (#1321's
  // "checked and fine": it never fires on an OpenCode clone)
  line("It found local changes in Claude Code's clone,", "On a machine with Claude Code's install too, it found local changes in Claude Code's clone,",
    ['reference/patching.md']),
  line('nothing sent, so no dialog asks;', 'nothing sent, so no form asks;', ['reference/patching.md']),
  // gh's login asks the user (a browser, a code): OpenCode's `!` has no terminal for it (`BANG`)
  line("the login is the user's own: `! gh auth login --hostname github.com --git-protocol <ssh|https>` as wb prints it",
    `the login is the user's own, typed ${PANE_OR_TERMINAL}, never with \`!\` (${BANG}): \`gh auth login --hostname github.com --git-protocol <ssh|https>\` as wb prints it`,
    ['reference/patching.md']),
  line('Over https, `! gh auth setup-git` too,', 'Over https, `gh auth setup-git` there too,', ['reference/patching.md']),
  line("needs the token's workflow scope: `! gh auth refresh -h github.com -s workflow`.",
    "needs the token's workflow scope: `gh auth refresh -h github.com -s workflow`, typed there too.", ['reference/patching.md']),
  // Claude Code's GitHub plugin: on OpenCode, a GitHub MCP server in its own config
  { from: /- \*\*The GitHub plugin for Claude Code is optional\*\*[\s\S]*?a pull request still goes through `wb patch send`\./,
    to: "- **A GitHub MCP server is optional**, never needed for a pull request. If the user wants one,\n"
      + "  it goes in their OpenCode config's `mcp`, with a personal access token they create on GitHub\n"
      + '  and set in their own shell profile (never from `gh auth token` unless they ask for exactly\n'
      + '  that); `opencode mcp list` checks it connects. Its tools that write to upstream ask in the\n'
      + '  same form; a pull request still goes through `workbench_patch_send`.',
    files: ['reference/patching.md'] },

  // what recurs
  // (after every line above, which match Claude Code's words) the web chat's replies: chat.ts's tool (#1319)
  { from: /`wb chat reply\s+--to\s+<id>\s+--text\s+"…"`/g, to: '`workbench_chat_reply`, `to` its id', files: '*' },
  { from: /`wb chat reply\s+--to\s+user(?:\s+--text\s+"…")?`/g, to: '`workbench_chat_reply`, `to` `user`', files: '*' },
  { from: /`wb chat reply(?:\s+--text\s+"…")?`/g, to: '`workbench_chat_reply`', files: '*' },
  // what remains of the mod's dialogs where the user approves a change: the plugin's form (a
  // "dialog's" too: \b ends the word at its apostrophe)
  { from: /\b(the|a)(\s+)dialog\b/g, to: '$1$2form', files: ['SKILL.md', 'reference/models.md', 'reference/patching.md'] },
  { from: /two directories above\s+`readlink -f "\$\{CLAUDE_CONFIG_DIR:-\$HOME\/\.claude\}\/skills\/workbench"`/g, to: (w) => `at \`${w.root}\``, files: '*' },
  { from: /\s+--session\s+"\$CLAUDE_CODE_SESSION_ID"/g, to: '', files: SKILL },  // wb takes the caller's session (#1309)
  { from: /`\/workbench-quit-and-hand-back-claude`/g, to: '`/workbench-quit-and-hand-back`', files: SKILL },
  { from: /Bash\s+`run_in_background: true`/g, to: "the shell tool's `background: true`", files: '*' },
  { from: /`run_in_background: true`/g, to: '`background: true`', files: '*' },
  { from: /Bash's\s+(?:2|two)(?:\s+minutes|-minute\s+default)/g, to: "the shell tool's 2-minute timeout", files: '*' },
  { from: /\bBash call\b/g, to: 'shell call', files: '*' },
  { from: /\byour Bash\b/g, to: 'your shell', files: '*' },
  // the mod is Claude Code's; this plugin is OpenCode's (never `mod/`, nor the mod's own tests)
  { from: /\b(the|The|workbench) mod\b(?!\/|'s tests)/g, to: '$1 plugin', files: '*' },
  { from: /(?<!Claude Code )\bmod's(?! tests)/g, to: "plugin's", files: '*' },
  // the reference parts are skills of their own on OpenCode ("read" one becomes "load": `loads`)
  { from: /`reference\/(\w+)\.md`\s+beside\s+this\s+file/g, to: 'the `workbench-$1` skill', files: '*' },
  { from: /`reference\/(\w+)\.md`/g, to: 'the `workbench-$1` skill', files: '*' },
  { from: /\breference\/(\w+)\.md\b/g, to: 'the `workbench-$1` skill', files: '*' },
  { from: /Read from the \/workbench skill/g, to: 'Load this from the Workbench agent', files: '*' },
]

const applies = (rule: Rule, file: string): boolean => rule.files === '*' || rule.files.includes(file)

// "read the `workbench-x` skill" reads as "load": the skill tool is how it comes in
const loads = (text: string): string => text.replace(/\b(r|R)ead(\s+)the `workbench-/g, (_, r: string, gap: string) => `${r === 'R' ? 'L' : 'l'}oad${gap}the \`workbench-`)

// One source file as a host reads it. Claude Code's is the file itself.
export function skillFor(host: Host, file: string, text: string, where: Where): string {
  if (host === 'claude-code') return text
  let out = text
  for (const rule of HOST_LINES) {
    if (!applies(rule, file)) continue
    const to = rule.to
    out = out.replace(rule.from, typeof to === 'string' ? to : to(where))
  }
  return loads(out)
}

// How often each host line matches its files, each in the text the lines before it left (as
// skillFor applies them): a 0 is a line SKILL.md no longer has, or one an earlier line rewrote
// first (a recurring rule must come after the exact lines whose words it changes)
const ANY_WHERE: Where = { root: '/workbench', wb: '/workbench/bin/wb', onPath: true, docs: true, wakes: true }
export function hostLineMatches(texts: Readonly<Record<string, string>>, where: Where = ANY_WHERE,
                                rules: readonly Rule[] = HOST_LINES): number[] {
  const now: Record<string, string> = { ...texts }
  return rules.map((rule) => {
    let n = 0
    for (const file of Object.keys(now).filter((f) => applies(rule, f))) {
      n += now[file]!.match(new RegExp(rule.from.source, 'g'))?.length ?? 0
      now[file] = now[file]!.replace(rule.from, typeof rule.to === 'string' ? rule.to : rule.to(where))
    }
    return n
  })
}

export interface Part {
  readonly id: string
  readonly name: string
  readonly description: string
  readonly content: string
}

export interface Rendered {
  readonly prompt: string          // the Workbench agent's instructions: SKILL.md's body, rendered
  readonly skill: Part             // the `workbench` skill: the same text
  readonly parts: readonly Part[]  // one `workbench-<name>` skill per reference/*.md
}

const frontmatter = (text: string): { meta: string; body: string } => {
  const m = /^---\n([\s\S]*?)\n---\n/.exec(text)
  return m ? { meta: m[1] ?? '', body: text.slice(m[0].length) } : { meta: '', body: text }
}

// A folded `description: >` block, or a one-line `description: …`
export function description(meta: string): string {
  const m = /^description:[ \t]*>?[ \t]*(.*)\n((?:[ \t]+.*\n?)*)/m.exec(meta)
  if (!m) return ''
  return [m[1] ?? '', ...(m[2] ?? '').split('\n')].map((s) => s.trim()).filter(Boolean).join(' ')
}

const title = (text: string): string => /^# (.+)$/m.exec(text)?.[1]?.trim() ?? ''

// OpenCode's rendering of every source file: `texts` maps 'SKILL.md' and 'reference/<name>.md'
// to their text
export function render(texts: Readonly<Record<string, string>>, where: Where): Rendered {
  const main = skillFor('opencode', 'SKILL.md', texts['SKILL.md'] ?? '', where)
  const { meta, body } = frontmatter(main)
  const prompt = body.replace(/^\n+/, '')
  const parts = Object.keys(texts).filter((f) => f.startsWith('reference/')).sort().map((file): Part => {
    const content = skillFor('opencode', file, texts[file] ?? '', where)
    return { id: partId(file), name: partId(file), description: `Workbench: ${title(content)}.`, content }
  })
  return { prompt, skill: { id: SKILL_ID, name: SKILL_ID, description: description(meta), content: prompt }, parts }
}

// skill/workbench/'s files under `root`, as `render` takes them; undefined without a SKILL.md
export function readSkill(root: string): Record<string, string> | undefined {
  const dir = join(root, 'skill', 'workbench')
  const main = join(dir, 'SKILL.md')
  if (!statSync(main, { throwIfNoEntry: false })?.isFile()) return undefined
  const texts: Record<string, string> = { 'SKILL.md': readFileSync(main, 'utf8') }
  const ref = join(dir, 'reference')
  if (statSync(ref, { throwIfNoEntry: false })?.isDirectory()) {
    for (const name of readdirSync(ref).filter((n) => n.endsWith('.md')).sort()) {
      texts[`reference/${name}`] = readFileSync(join(ref, name), 'utf8')
    }
  }
  return texts
}
