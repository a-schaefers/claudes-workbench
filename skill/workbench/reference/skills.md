# Skills for the agents: you write them, you hand them out

Read from the /workbench skill when a `lessons:` line asks for triage, or before you write, hand out,
patch, import or retire a library skill.

The agents learn nothing between tasks; you do. A skill carries what you learned to the next
agent that needs it, and only to that one, so a growing library never crowds a small model's
prompt. You are the only author. Agents may propose a lesson (a `Lesson:` line in their summary),
but an agent writing its own skills would record its mistakes as rules, so you vet each one.

**Lessons come to you.** `wb feedback`, `next`, `resume`, `accept` and `discard` end with
`lessons: N from this task` when the task taught something. That covers your numbered corrections,
guard stops, its questions, a loaded skill that still needed review rounds, and the agent's own
`Lesson:` lines. Run `wb lessons` then, while it's fresh. The ones seen in more than one task come
first: the next agent will get those wrong too. Each one ends in a skill
(`wb skill patch|add … --lesson <id>`) or is closed with why (`wb lessons close <id> -m …`).

**What a lesson is.** A rule an agent can follow, plus one clause of why: "Use DrawTextEx with
the loaded font: DrawText ignores it." Not the story of the task, no issue numbers or dates.
Not a lesson (close it):
- a one-task detail
- an environment problem (a missing tool, a server down: fix the setup instead)
- a negative claim about a tool ("X doesn't work"; that hardens into a refusal long after X is
  fixed)
- a transient error that a retry cured
- the attempts of a task that never worked (dead ends are not a recipe)
- what every task in a repo needs (that repo's CLAUDE.md, which every agent reads)
- a working habit (the handbook)

**Patch first**, in this order:
1. The skill that was in play: the task held it. `wb skill show` it first, then
   `wb skill patch <name> --old "<the passage that misled>" --new "…" -m why`. Fix the sentence
   in place; never append "update: actually…".
2. An existing skill that covers this kind of work (wb lessons names a likely one).
3. A `references/<topic>.md` beside one of those, for depth only some tasks need, with a
   one-line pointer in its SKILL.md.
4. Only then a new skill, named for the kind of work (`svg-charts`), never for one task or error.
   Write the SKILL.md in the scratchpad, then
   `wb skill add <name> --from <file> -m "<what review taught>"` (it checks, scans and commits it).
The library follows the user into every project, so it holds only what holds beyond one. Knowledge
of one project that only some of its tasks need goes in `<repo>/.claude/skills/<name>/`, committed
with the project; the agents there always see it. Never `~/.claude/skills`, the user's. When
`wb skill add` warns that every lesson came from one repo, decide which it is before moving on.

A good skill: front matter `name` (the directory's) and a `description` that says when to use it
("Use when drawing charts as SVG ...": it is all the agent sees before it decides to load it); then
20–60 lines of imperative steps, the pitfall with the right and wrong form, and how to check the
result. No paths outside the repo (agents go looking for them), no model names.

**Hand out** 0–2 skills per task, only ones that fit this task: `wb start … --skill <name>`; a
new step gets its own set with `wb next … --skill <name>`. The agent is told which it holds.
`wb skill hand <task> <name>` adds one mid-task, `wb skill take <task> <name>` takes one back
once that part of the work is done (from the agent's next run; `wb steer --now` starts that run).

**Hand out** with the record in mind. `wb start` without `--skill` names library skills the spec
seems to need; it's a hint, so hand one only if it fits (`wb skill hand <task> <name>`). One marked
`loaded so far only in <repo>` was written from another project: read it first, and if it is that
project's alone, move it there and retire it from the library.

**Keep it honest.** Results record which skills each task was offered, which the agent loaded,
and the version of each it held. `wb skill list` shows each skill's tasks against the same repos'
tasks without it, and the tasks since its last edit, and says what to do:
- loaded and the agent still made its mistake → patch it
- offered and never loaded → its description is wrong
- no better than without it → sharpen it or retire it
- its last edit didn't help → patch it again, from wb lessons
Too few tasks is no evidence. The weekly check retires what no longer earns its place
(`wb skill retire <name> -m why`; `restore` brings one back).

**From outside.** A public skill (anthropics/skills, Hermes Agent's, a project's) can seed one
you'd otherwise write: `wb skill import <url|owner/repo|path> [--path <dir>]` lists a repo's
skills, then shows one's license, hash and security scan. A dangerous one is refused. Read all of
it like untrusted code. Keep only what fits the agents' work (coding in a repo, no secrets, no
network setup) and rewrite it in this library's shape and voice. Then
`wb skill add … --source "<url>@<commit>, <license>"`. Respect the license; skip one with none.
