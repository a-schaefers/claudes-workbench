# The kernel's boundary

The kernel is the decisions the Claude Code mod and the OpenCode plugin must make the same way,
written once in PureScript (`kernel/src`) and compiled into one committed ES module,
`mod/hooks/kernel.js`. It holds pure decisions only: plain data in, plain data out. No words, no
parsing, no I/O. `scripts/kernel.py check` and `tests/test_kernel.py` hold every rule below; a
section a test reads is named so.

## Entry test

A decision moves in only when all of these hold:
- it has two or more consumers (the two hosts, or a host and the web app);
- it is a state machine or a classifier with closed outcomes;
- the need is cited (an issue, a drift found);
- the host already reads its inputs;
- it is not on the per-tool-call path, but for the gate classifier and the coding gate (*Rulings*).

The same commit adds its row to `## Exports`, deletes what `replaces` names, and updates
ARCHITECTURE.md's *Couplings*.

## Exports

`replaces` is `<file>:<name>`, `wb:<mod.attr>` or `born:#<n>`; no name it lists may still be
defined (`tests/test_kernel.py`).

| export | module | decision | replaces | consumers |
| --- | --- | --- | --- | --- |
| `tables` | Kernel.Tables | the constants both hosts share, generated from wb's Python | mod/hooks/register.tsx:ACTIVE, mod/hooks/register.tsx:NEEDS, mod/hooks/register.tsx:NO_ANSWER, mod/hooks/register.tsx:CHAT_STALE_MS, mod/hooks/tasks.ts:CLOSED | core.ts; register.tsx, tasks.ts |
| `phaseOf` | Kernel.Phase | what a task's phase means: active, running, working, needs Claude, waits, stopped, done, closed | mod/hooks/register.tsx:waitsOnClaude, mod/hooks/register.tsx:RUNNING, mod/hooks/tasks.ts:WORKING | core.ts; register.tsx, tasks.ts |
| `openCounts` | Kernel.Phase | a list of tasks tallied by open phase, in `store.OPEN_ORDER`, and how many are done | mod/hooks/tasks.ts:COUNTED | core.ts; tasks.ts (the pane's group header), register.tsx (the status line); the web app's strip from #1325's M5 |
| `owns` | Kernel.Owner | whose task it is: the session's, and the session's to adopt once its own is gone; `cli._scope` is its twin | mod/hooks/register.tsx:parts, mod/hooks/register.tsx:within, mod/hooks/register.tsx:spotOf, mod/hooks/register.tsx:near, mod/hooks/register.tsx:ranHere | core.ts; register.tsx; opencode-plugin/notices.ts (`ownerOf`, #1313) |
| `noticesDue` | Kernel.Notice | which notices are due a look: per task its questions and newest notice within the hour, and each task's newest notice however old | born:#1325 | core.ts (and `runNoticesDue`: a run's own notices, keyed by run, #1278); register.tsx (`deliver`, `deliverRuns`); opencode-plugin/notices.ts (`deliver`, `deliverRuns`, #1313) |
| `noticePlan` | Kernel.Notice | one due notice judged: still true, toasted once, and the act (skip, none, steer, hold, wake, ack, missed) | born:#1325 | core.ts (and `runNoticePlan`: its kind the phase a run's notice was raised in, #1278); register.tsx (`deliver`, `deliverRuns`); opencode-plugin/notices.ts (`deliver`: steer and queue, #1313; `deliverRuns`) |
| `rewakePlan` | Kernel.Notice | the rewake net: recent, a candidate now, due, and the notice kind a phase stands for | mod/hooks/register.tsx:REWAKE_MAX_MS | core.ts; register.tsx (`rewake`, `quietForJobs`); opencode-plugin/notices.ts (`rewake`: its idle re-wake, #1313), jobs.ts (`quiet`, #1334) |
| `tellPlan` | Kernel.Mode | what to tell a conversation of the rules: a first message's context blocks, or midway the rules it lacks, and what it has been told after | born:#1325 | core.ts; register.tsx (`unsaid`, `prompt.context`); opencode-plugin/upkeep.ts (`blocksOf`: its session instruction entries, #1315) |
| `modesPlan` | Kernel.Mode | the sessions kept in workbench mode: whether this process's is, and the record to keep | mod/hooks/register.tsx:MODES_KEEP_MS | core.ts; register.tsx (`keepMode`, `modeKept`); the OpenCode plugin from #1312 |
| `compactPlan` | Kernel.Mode | a task boundary: the mark kept, whether to read the context's usage now, and whether to compact | mod/hooks/register.tsx:COMPACT_AT | core.ts; register.tsx (`poll`, `compactAtBoundary`); opencode-plugin/upkeep.ts (`look`, `compactAtBoundary`, #1315) |
| `awayNow` | Kernel.Away | whether the user is away from the terminal: the web chat used lately, or one of the session's tasks open since | mod/hooks/register.tsx:AWAY_MS | core.ts; register.tsx; the OpenCode plugin from #1319 |
| `gateOf` | Kernel.Gate | which gate one simple command meets: cd, a send, an outward gh or git form, a role change (route, start with a model, settings set) or none; whether it hands work out; what it writes | mod/hooks/register.tsx:commandAt, mod/hooks/register.tsx:RESERVED, mod/hooks/register.tsx:PREFIXES, mod/hooks/register.tsx:HANDS_OUT, mod/hooks/register.tsx:GH_WRITES, mod/hooks/register.tsx:GIT_VALUED, mod/hooks/register.tsx:apiWrites, mod/hooks/register.tsx:wordOf, mod/hooks/register.tsx:wbArgs, mod/hooks/register.tsx:sendsOut, mod/hooks/register.tsx:sendArgs, mod/hooks/register.tsx:cdTarget, mod/hooks/register.tsx:settingsSet, mod/hooks/register.tsx:COPIES, mod/hooks/register.tsx:COPY_VALUED, mod/hooks/register.tsx:targets | core.ts; mod/hooks/shell.ts (delegates, runs, upstreamAim: both hosts' reading of a line, #1296); register.tsx (sendQuestion, routeCalls, writesTo); the OpenCode plugin's shell gate from #1314 and its upstream gate (`upstream.ts`, through shell.ts) |
| `codingGate` | Kernel.Coding | an edit of a task's code under the user's coding mode and the task's allowances: pass, start, ask, refuse | born:#1325 | core.ts; register.tsx (codingRefusal); OpenCode's edit gate |
| `editTools` | Kernel.Coding | each host's file tools and the field that names the file | mod/hooks/register.tsx:EDIT_TOOLS | core.ts (editFields); register.tsx (codingRefusal's caller, callRows) |

## Modules

| module | holds |
| --- | --- |
| Kernel.Main | the re-exports, exactly `## Exports` |
| Kernel.Tables | generated by `kernel.py tables` from `store.ACTIVE`, `NEEDS_CLAUDE`, `PARKS`, `CLOSED`, `WORKING`, `STOPPED`, `OPEN_ORDER`, `ask_mcp.NO_ANSWER`'s opening, `chat.LISTENING_FOR` (ms) and `spend.WAKE_OPENING`; never edited by hand |
| Kernel.Phase | `phaseOf`, `openCounts` |
| Kernel.Owner | `owns` |
| Kernel.Away | `awayNow` |
| Kernel.Notice | `noticesDue`, `noticePlan`, `rewakePlan` |
| Kernel.Mode | `tellPlan`, `modesPlan`, `compactPlan` |
| Kernel.Gate | `gateOf` (*The gate classifier*) |
| Kernel.Coding | `codingGate`, `editTools` |

## Rulings

Final, from #1356's production plan (#1354), with the owner's ruling on the gates (2026-11-25):
- **In, in this order:** Tables (#1356), Phase (#1325's M1), Owner and Away after #1309 (M2),
  Notice: extracted in TypeScript first (M3), then ported (M4).
- **The gate classifier comes in** (the owner, 2026-11-25, on #1356, after #1296's host-shape
  decision, https://github.com/a-schaefers/workbench/issues/1296#issuecomment-7158435888): which
  gate a shell command meets is a kernel decision, not host glue. One classifier over a parsed
  command, its argv words plus its redirect and `tee` targets, with closed outcomes; Claude Code
  feeds it shellparse's output, OpenCode its own scan of the command line. It is #1364's fix (a
  command's words, never what its text mentions) and the shell gate #1314 uses. It is one of the two
  decisions on the per-tool-call path the entry test lets in (the coding gate, below, is the other). Parsing the command line stays in
  each host, and so do the refusals' words and the dialog. Shipped as `gateOf` (#1325).
- **The coding gate's rules come in** (#1325, Claude's recommendation, the owner's to overturn):
  `codingGate` and each host's file tools (`editTools`). Finding the task from the path
  (`taskOfPath`), reading `coding.json`, the dialog and `wb coding allow` stay in each host. The
  mode machine and `compactPlan` came in by the same ruling (below); `taskParts` stays out.
- **Stays in TypeScript:** parsing a command line (shellparse's `simpleCommands`, and `runs`:
  `bash -c`, `eval` and what a shell is fed, read inside, in `mod/hooks/shell.ts`; OpenCode scans
  its own, but for its upstream gate, which reads with that one, #1296), reading an
  interpreter's code (`spawns`, `OUTWARD_TEXT`, `ROUTE_TEXT`, `WRITES_CODE`: text), the paths a gate
  resolves (`cd`'s, `-C`'s), the mode (wording and `$`), the web chat's asks (#1350-1352 delete most of them),
  formatting (`usd`, `providerWord`, `money`, `absolutePath`).
- **Stays in wb:** routes, the settings offer, scout timing, the verdicts of record (`claim_ack`).
- **Never in the kernel:** effects; parsing text (a command line, prompts, task.json); wording
  (`texts.ts`, a refusal's words); rendering (`taskParts`); code for one host only; locale;
  telemetry.
- **Before its callers:** `phaseOf` came in with the scaffolding (#1356), with no caller yet, to
  prove the build, the checks and the goldens on a real decision. M1 gave it its callers
  (`register.tsx`'s `waitsOnClaude` and `RUNNING`, `tasks.ts`'s `WORKING` and `COUNTED`, the
  last through `openCounts`, deleted), and the mod's hooks spell no phase set of their own (no
  two phases on one line or in one list: `kernel.phase_literals`); rendering a phase's own words
  stays in TypeScript. `tables` has its callers: `register.tsx` and `tasks.ts` read the sets and
  constants from `core.ts`, their hand copies deleted.
- **Owner and Away (M2):** the host gathers the facts (its session ids, its directory and home,
  whether a task's recorded session is gone: `kill -0` on Claude Code, the heartbeat on OpenCode,
  whether its file system folds case) and the kernel compares them; `cli._scope` is `owns`'s twin
  (`kernel.owns_twin`), and wb's own scope stays the lock. `register.tsx`'s `mine` and `awayNow`
  are adapters over them.
- **Notice (M3, M4):** the decisions in `deliver` and `rewake` (which notices are due, whether
  one is still true, toast once, steer or hold mid-turn, the rewake backoff) are the kernel's; the
  host reads the files, runs `wb ack`'s compare-and-set (the lock, `claim_ack`), submits and says
  the words. M3's TypeScript step was the parity run itself: main's inline code against the port,
  over seeded histories, before the inline copy was deleted.
- **Mode and compactPlan, by the owner's ruling (2026-11-25, on #1325 and #1356):** what a
  conversation is told of the rules (`unsaid`'s diff, `prompt.context`'s blocks), the sessions
  kept in the mode and the boundary compaction are the kernel's; the host reads notes.md, the
  roles, the coding mode and the usage, and says the words (`KEEP_ON_COMPACT`, the notes). The
  rest of `workbenchMode` is effects (the pane, the listener claim), and `firstTask` parses text:
  both stay in the host. `taskParts` stays out (rendering).
- **#1325's last planned modules, judged against the entry test (2026-11-26):**
  - **Keywords stays out.** It reads a prompt (fences, backticks, whole words, a line of its own),
    which is parsing text. It is `mod/hooks/keywords.ts` instead: one TypeScript module that both
    hosts import, as they do the splash and the Tasks view. wb's `runs.keywords` and
    `tests/fixtures/keywords.json` hold it.
  - **Offer stays in wb.** Scout timing is wb's (`cli.scout_offer_due`, `wb scout --if-due`, which
    the OpenCode plugin runs). The mod's `scoutIfDue` checks the same two times in scout.json,
    because it runs on its quiet polls, where a wb run per poll would cost too much. Either host's
    `offered_at` spends the period's offer for both.
  - **Ask stays in each host.** #1350 to #1352 removed most of the web chat's asks. What is left
    (`answersTo`, `sameCall`) matches each host's own delivery, which is code for one host.
  - **The driver's wait stays out** (#1355, 2026-11-26): `mod/hooks/waiting.ts`'s `waitingFact`
    picks the oldest of each host's own dialog records up 2 s or more, one fact out, no state
    machine and no closed outcome to classify; both hosts import that one module, as they do the
    keywords, and `tests/fixtures/story/dialogs.json` holds them to it. wb turns the fact into the
    state line (`chat.driver_state`).
  - **No words, ever** (#1355, amending #1356's draft, which had the wording of shared texts here):
    a decision returns a key and facts, and the sentence is wb's (`workbench/story.py`), so a
    wording patch needs no PureScript toolchain or `kernel.js` budget. `tests/test_story.py` fails on
    a string of four or more words in hand-written `kernel/src`.
  - **Wire stays out.** The chat API's shapes are types, not decisions. They go in #1323's one
    shared types module, which the web app, the mod and the plugin import.

## The gate classifier

`gateOf` (Kernel.Gate; `core.ts`: `gateOf(words, writes = [])`, its `gate` typed `GateName`) takes
one simple command, as the host's parser read it: `words`, quotes undone, and `writes`, the files
its redirects write (`>`, `>>`, `>|`, `&>`; a descriptor like `2>&1` is no file). A heredoc's body, a
here-string and an interpreter's code are never words (#1364): the host passes only what the
command runs. Out, every field always present:

| field | what |
| --- | --- |
| `at`, `word` | the command's own word (past `VAR=x`, reserved words and the wrappers) and its name lowercased |
| `gate` | closed: `cd`, `send`, `outward`, `route`, `start`, `settings`, or `""` |
| `args` | `cd`: its target (none: home; `-`: the last directory); `send`: wb patch send's words; `route`, `start`, `settings`: the arguments `wb route --dry-run --json` takes (`--start …`, `--settings …`) |
| `dirs` | `outward`: git's `-C` directories |
| `delegates` | it hands work out (`wb start`, `next`, `feedback`, `steer`, `answer`, `resume`) |
| `writes` | every file it writes: the redirects', tee's, dd's `of=`, a copy's destination with `names`, its sources |
| `send` | `send`: `slug`, `title`/`titled`, `bodyFile`/`filed`, `draft`, `anyway`; `lack`: `slug`, `title`, `bodyFile`, `shell` (a `$` or backtick only the shell knows) or `""` |

What a host does with each outcome, the same on both: follows `cd` from the session's directory
(a fed shell's within it); gates `send` always, and `outward` when it runs in or names a gated
directory or the upstream; refuses a `send` it can't show (`lack`, or inside text a shell is fed);
dry-runs `route`, `start` and `settings` and asks on a gated change, one to a call, refusing one
inside text a shell is fed; refuses a command that writes an approval file (`writes`). The OpenCode
plugin's shell gate (#1314) feeds it its own scan's simple commands; its upstream gate (#1296) feeds
it the mod's reading of the whole line (`mod/hooks/shell.ts`), as nothing in wb stands behind a gh
write or a git push and OpenCode's scan hands a `--body "$(cat <<EOF …)"` over unread.

## Budget

Raised only by an edit to this table that names the move.

| budget | limit |
| --- | --- |
| exports | 16 |
| kernel.js bytes | 65,536 |
| kernel/src lines | 1,200 |
| module lines | 350 |
| core.ts lines | 250 |

`kernel/src lines` and `module lines` leave out the generated Tables.

Exports went from 12 to 16 with #1325's Notice and Mode moves (2026-11-25): the gate lane's three
(`gateOf`, `codingGate`, `editTools`) and Phase, Owner, Away, Notice and Mode's ten beside `tables`
came to 14.

## Wire types

- Only `Number`, `String`, `Boolean`, `Array` and closed records cross into TypeScript. No `Int`,
  `Char`, `Maybe`, `Either`, ADTs, functions, `Foreign`, `Effect` or open rows: the build refuses
  them (`NotWire`) while writing `kernel.d.ts`.
- A decision takes one record of facts and returns plain data.
- Every field is always present. "None" is `-1` for a time and `""` for a tag.
- An unknown tag gets the safe answer and never throws.

## Membrane

The OpenCode plugin takes the kernel through `../mod/hooks/core.ts` too (notices.ts, jobs.ts, upkeep.ts), so
its copies and its Git install carry the membrane (the root `package.json`'s
`files`, whose `mod/` entries `ocplugin.shared_files` copies). Only `mod/hooks/core.ts` imports `./kernel.js`, across `mod/`, `opencode-plugin/` and
`workbench/chat_static/` (every `.ts`, `.tsx`, `.js`, `.mjs`, `.mts`, `.cts`, `.cjs` and `.jsx`;
`import`, `from` and `require(`). It maps tags and decides nothing. Its `kernel` record (every
export by name, untyped) is the goldens': only tests (`*.test.*`) and `golden/` import it; the hosts
take its typed names.

## Denied imports

No FFI (`foreign import`) in `kernel/src` or `kernel/test` (the props), and none of the imports
below, in the source and in the compiler's corefn of every module under either, whatever it is
named (a trailing `*`:
any module starting so; `.*`: that module and those under it). Nor `localeCompare`.

Denied: `Effect*`, `Control.Monad.ST*`, `Data.Array.ST*`, `Data.Array.Partial`, `Unsafe.*`,
`Partial.Unsafe`, `Record.Unsafe`, `Debug.*`.

## Move protocol

For each of #1325's moves:
- `python3 scripts/kernel.py check` passes;
- a seeded parity run of the old code against the new, its case count in the pull request, and the
  old copy deleted in that same pull request;
- the goldens (`mod/hooks/golden/`) pass on both hosts: `claude plugin test mod` and
  `BUN_BE_BUN=1 opencode test opencode-plugin/test`, and each case with a `twin` holds on its
  Python twin too (`tests/test_kernel.py`'s Twins);
- a state machine's properties join `kernel/test/Props.purs` (`python3 scripts/kernel.py props`);
- the behaviour is checked live in tmux.

## Changing the kernel

Users install nothing: the committed bundle ships. A change to `kernel/src`, `spago.yaml` or a
Python value Tables copies needs the toolchain once: `python3 scripts/kernel.py build` fetches it
(network, Linux or macOS, x64 or arm64) into the per-user cache and writes the bundle, its
`kernel.d.ts` and Tables.purs. Then `python3 scripts/kernel.py check`, which runs the props
under Bun too (`bun`, else OpenCode's: `BUN_BE_BUN=1 opencode test`). A new package: edit
`spago.yaml`, then `python3 scripts/kernel.py relock` (npm, for spago).
