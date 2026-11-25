# Deep workflows: writing a flow, role cards, report tasks

Read this when no shipped flow fits and you write one (`wb run start <run> --plan <file>`), or
when you start a single report task by hand.

## A flow
JSON, `"v": 1`. Keys: `name` (as a run's name), `description`, `params` (`{name: string|int|list|
json|file|runs}`; `runs` is an earlier run's final results), `budget_usd`, `budget_min`,
`context` (text every item may carry), `stages`. Each stage:

- `id`: a lowercase letter, then letters or digits, at most 8. Its items are tasks
  `<run>-<id>-<n>`.
- `card` (a role card) or `"kind": "code"`; `role` overrides the card's (significant, trivial,
  writing). A report stage returns one object to `schema` (the card's unless given) with `tools`
  read or none; a code stage changes code in its own worktree (`test`, `setup`, `ask`).
- Its inputs: `items` (a list, or `"{params.<a list param>}"`), or `from` an earlier stage,
  `split` a key of each earlier output that holds a list (one item per entry).
- `prompt`: the item's spec. Placeholders only substitute: `{item}`, `{item.<key>}`,
  `{params.<key>}`, `{context}`, `{diff}` (the code stage's change, over a code stage only),
  `{run}`, `{stage}`, `{base}`; `{{`, `}}` are braces. An object goes in as fenced JSON.
- `flow`: `pipeline` (the default: an item starts once its input is in) or `barrier` (once all
  are); `batch` N (barrier only: N inputs as one list item); `attempts` N (the same item N times).
- `keep`: `{"path": "verdict", "in": ["confirmed"]}`: the rest are filtered, kept in the report.
- `refix`: on a report over a code stage, `{"path": "must_fix", "max": 1|2}`: a non-empty list
  goes back to the coder as `wb feedback`, then the change is reviewed again.
- `on_failure`: `checkpoint` stops the run for you when an item ends with no result; a code item
  stuck or failed always does.
- `"claude": true` with `from`: a checkpoint of yours. You merge, then `wb run feed`.
- `max_items` (200): past it, items are `not_run`, named in the report.

The last stage is `{"id": "...", "final": true, "from": "<stage>"}`: yours. After a code stage
comes only a report over its `{diff}`, or the final: nothing builds on code you haven't accepted.
`wb run start` refuses a flow that breaks any of this, naming each problem.

## Role cards
Markdown with a frontmatter: `description`, `tools` (read, none, code), `schema`, `steps`,
`skills`, `ask` (0 for reports), `role`, `est` (`{in, out, min}`: one item's tokens and minutes,
for the estimate). A card's text is at most 3,000 bytes. A user's own go in
`~/.config/workbench/roles/` (`$WB_ROLES`), schemas in its `schemas/`; a card can't set a model or
permissions (dropped, said). Shipped: find, verify, judge (no tools), review, critic; build, fix.

## One report task by hand
`wb start <t> --spec <file> --report --card verify` (or `--schema verdict [--tools none]`): the
agent reads, never writes, and ends with one JSON object, checked outside its lane; one repair
round, then it fails. Its review notice carries the object; `wb accept <t>` (no `-m`) keeps it
and commits nothing.
