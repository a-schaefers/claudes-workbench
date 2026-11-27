# Deep workflows: writing a flow, role cards, report tasks

Read this when no shipped flow fits and you write one (`wb run start <run> --plan <file>`), or
when you start a single report task by hand.

## A flow
JSON, `"v": 1`. Keys: `name` (as a run's name), `description`, `params` (`{name: string|int|list|
json|file|files|runs}`, or `{name: {type, description, default, choices, optional}}`: a param
left out takes its `default`, or is absent when `optional`; `choices` for a string or int; `runs`
is an earlier run's final results; `files` a pathspec, matched against the files committed at
the base, each an item `{file, name, stem, dir}`), `budget_usd`, `budget_min`, `context` (text
every item may carry), `command` (one lowercase word: the flow as `/<command>` in both hosts;
not on a repo's flow), `stages`. Each stage:

- `id`: a lowercase letter, then letters or digits, at most 8. Its items are tasks
  `<run>-<id>-<n>`.
- `card` (a role card) or `"kind": "code"`; `role` overrides the card's (significant, trivial,
  writing). A report stage returns one object to `schema` (the card's unless given) with `tools`
  read or none; a code stage changes code in its own worktree (`test`, `setup`, `ask`).
- Its inputs: `items` (a list, or `"{params.<k>}"` of a list, files, runs or json param, never a
  string or file one: refused), or `from` an earlier stage,
  `split` a key of each earlier output that holds a list (one item per entry).
- `prompt`: the item's spec. Placeholders only substitute: `{item}`, `{item.<key>}`,
  `{params.<key>}`, `{context}`, `{diff}` (the code stage's change) and `{report}` (its coder's
  round report: answers to a refix), both over a code stage only, `{run}`, `{stage}`, `{base}`; `{{`, `}}` are braces. An object goes in as fenced JSON.
- `flow`: `pipeline` (the default: an item starts once its input is in) or `barrier` (once all
  are); `batch` N (barrier only: N inputs as one list item, each with its key as `id`; the
  answers must be one per id, a missing one named `unjudged`); `attempts` N (the same item N times).
- `keep`: `{"path": "verdict", "in": ["confirmed"]}`: the rest are filtered, kept in the report.
- `compare: true`: a report over a code stage with `attempts`: one item per item, once its
  attempts (and an earlier stage's reviews of them) ended, its prompt's `{diffs}` every attempt's
  change, labelled a1..aN (the green ones when two or more are), each with its review; the
  `choose` card returns `{best, ranking, why}`. A stage no later one reads (best-of's reviews) is
  in `wb run report` under `results`.
- `refix`: on a report over a code stage, `{"path": "must_fix", "max": 1|2}`: a non-empty list
  goes back to the coder as `wb feedback`, then the change is reviewed again.
- `escalate: [roles]`: on a code stage: an item stuck or failed, or with its review's asks left
  past refix's max, is tried again from the base on the next role (`<key>#e1`, …); the attempt
  before is kept. The tries are one change: `wb run report` names the one to accept (the latest
  with a finished change), and you discard the rest. A paid API escalate role is in the estimate's
  "as their items come", never its figure. Shipped as `fix-escalate` (trivial, then significant).
- `on_failure`: `checkpoint` asks you when an item ends with no result; a code item stuck or
  failed always does (with `escalate`, only once its last role has). It holds only that item:
  the others still start and run, and the run ends after your `wb run resume`.
- `"claude": true` with `from`: a checkpoint of yours. You merge, then `wb run feed`.
- `max_items` (200): past it, items are `not_run`, named in the report.
- `unless_exists`: on a stage whose `items` is a `files` param, a path template
  (`tests/{item.stem}_test.py`; `{item.file|name|stem|dir}`, `{params.<k>}`): an item whose path
  exists at the base is `not_run`, "exists at base: <path>", and costs nothing.

The last stage is `{"id": "...", "final": true, "from": "<stage>"}`: yours. After a code stage
comes only a report over its `{diff}`, or the final: nothing builds on code you haven't accepted.
`wb run start` refuses a flow that breaks any of this, naming each problem; `wb run start <run>
--plan <file> --dry-run` checks it and shows each stage, its first spec and the estimate, running
nothing.

## Customizing a shipped one
`wb flow copy <name>` and `wb card copy <name> [--schema]` put a shipped flow, card or schema in
your place to edit (it records the shipped one it came from); `wb run flows` and `wb card list`
mark it `shadows shipped`, and `shipped changed since` once an update changed the shipped one:
read the new one and merge, or `wb flow reset <name>` / `wb card reset <name>` to use it again
(yours is kept as `.bak`, `.bak.2` when that is taken: the reset line names it). `wb flow diff <name>` and
`wb card diff <name>` show what a copy changed; the web app's Settings list the user's own under
Dynamic workflows, More, with Diff and Reset; its ask button arrives as a request to edit the file.

## Scheduled
`wb schedule add <name> --flow <flow> [--param k=v] [--budget-usd N] --repo DIR --at 'Sun 02:00'`:
each fire starts the run itself (`<name>-YYMMDD`, `--overnight`), with no session to drive it; its final stage (and
a `claude` checkpoint) waits for you in the morning.

## A slash command as a flow
`wb flow import X.md [--name N] [--card C]`: a command file's prompt (Claude Code's
`.claude/commands/`, OpenCode's `command/`) as a flow of yours, one task per item of
`--param items=a,b`, `$ARGUMENTS` as `{item}`; a code task, or card C's report. A positional `$1`
is refused: write that flow yourself.

## Role cards
Markdown with a frontmatter: `description`, `tools` (read, none, code), `schema`, `steps`,
`skills`, `ask` (0 for reports), `role`, `est` (`{in, out, min}`: one item's tokens and minutes,
for the estimate; without one, a paid API stage reads `unpriced`). A card's text is at most 3,000 bytes. A user's own go in
`~/.config/workbench/roles/` (`$WB_ROLES`), schemas in its `schemas/`; a card can't set a model or
permissions (dropped, said). Shipped: find, verify, judge (no tools), review, choose, critic; build, fix.

## One report task by hand
`wb start <t> --spec <file> --report --card verify` (or `--schema verdict [--tools none]`): the
agent reads, never writes, and ends with one JSON object, checked outside its lane; one repair
round, then it fails. Its review notice carries the object; `wb accept <t>` (no `-m`) keeps it
and commits nothing.
